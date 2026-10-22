import Foundation

/// `viewer-cache/<series>/meta.json`, as the worker's viewer-cache job writes
/// it (ADR 0007, ADR 0031). Decoded by hand and leniently: a key added later
/// must not make every cache written before it unreadable.
public struct ViewerMeta: Sendable, Equatable {
    public struct Model: Sendable, Equatable {
        public var file: String
        public var present: [Int]
        public var resampled: Bool
    }

    public var size: (x: Int, y: Int, z: Int)
    public var spacing: (x: Double, y: Double, z: Double)
    public var origin: (x: Double, y: Double, z: Double)
    public var oblique: Bool
    public var imageFile: String
    public var models: [String: Model]

    public static func == (a: ViewerMeta, b: ViewerMeta) -> Bool {
        a.size == b.size && a.spacing == b.spacing && a.origin == b.origin && a.oblique == b.oblique
            && a.imageFile == b.imageFile && a.models == b.models
    }

    public init?(json data: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let size = object["size"] as? [Int], size.count == 3,
              let spacing = (object["spacing_mm"] as? [NSNumber])?.map(\.doubleValue), spacing.count == 3
        else { return nil }
        let origin = (object["origin_mm"] as? [NSNumber])?.map(\.doubleValue) ?? [0, 0, 0]
        self.size = (size[0], size[1], size[2])
        self.spacing = (spacing[0], spacing[1], spacing[2])
        self.origin = origin.count == 3 ? (origin[0], origin[1], origin[2]) : (0, 0, 0)
        oblique = object["oblique"] as? Bool ?? false
        imageFile = (object["image"] as? [String: Any])?["file"] as? String ?? "image.i16"
        var models: [String: Model] = [:]
        for (name, value) in object["models"] as? [String: [String: Any]] ?? [:] {
            guard let file = value["file"] as? String else { continue }
            models[name] = Model(
                file: file, present: value["present"] as? [Int] ?? [],
                resampled: value["resampled"] as? Bool ?? false)
        }
        self.models = models
    }

    public var geometry: VolumeGeometry {
        VolumeGeometry(nx: size.x, ny: size.y, nz: size.z, sx: spacing.x, sy: spacing.y, sz: spacing.z)
    }
}

/// One series' viewer cache, memory-mapped: the app reads the slices it
/// shows straight from the file and never holds a second copy of the CT.
public final class ViewerVolume: Sendable {
    public let meta: ViewerMeta
    public let geometry: VolumeGeometry
    private let image: Data
    private let labels: [String: (data: Data, wide: Bool)]

    public enum LoadError: Error {
        case meta, size(String)
    }

    public init(folder: URL) throws {
        guard let meta = ViewerMeta(json: try Data(contentsOf: folder.appendingPathComponent("meta.json"))) else {
            throw LoadError.meta
        }
        self.meta = meta
        geometry = meta.geometry
        let voxels = geometry.nx * geometry.ny * geometry.nz
        image = try Data(contentsOf: folder.appendingPathComponent(meta.imageFile), options: .alwaysMapped)
        guard image.count == voxels * 2 else { throw LoadError.size(meta.imageFile) }
        var labels: [String: (Data, Bool)] = [:]
        for (model, entry) in meta.models {
            let data = try Data(contentsOf: folder.appendingPathComponent(entry.file), options: .alwaysMapped)
            let wide = entry.file.hasSuffix(".u16")
            guard data.count == voxels * (wide ? 2 : 1) else { throw LoadError.size(entry.file) }
            labels[model] = (data, wide)
        }
        self.labels = labels
    }

    public var models: [String] { labels.keys.sorted() }

    /// Hounsfield units at one voxel.
    public func hu(at v: VolumeIndex) -> Int16 {
        let index = geometry.linearIndex(v)
        return image.withUnsafeBytes { $0.load(fromByteOffset: index * 2, as: Int16.self).littleEndian }
    }

    /// The label of one model at one voxel, 0 for background.
    public func label(_ model: String, at v: VolumeIndex) -> Int {
        guard let entry = labels[model] else { return 0 }
        let index = geometry.linearIndex(v)
        return entry.data.withUnsafeBytes {
            entry.wide ? Int($0.load(fromByteOffset: index * 2, as: UInt16.self).littleEndian) : Int($0[index])
        }
    }

    /// The CT slice in screen order as 8-bit gray, windowed through a lookup
    /// table, so scrolling does one table read per pixel.
    public func graySlice(_ mapping: PlaneMapping, slice: Int, window: WindowLUT) -> [UInt8] {
        image.withUnsafeBytes { raw in
            let voxels = raw.bindMemory(to: Int16.self)
            return mapping.mapSlice(slice) { window.table[Int(voxels[$0]) + 32768] }
        }
    }

    /// The label slice of one model in screen order.
    public func labelSlice(_ model: String, mapping: PlaneMapping, slice: Int) -> [UInt16] {
        guard let entry = labels[model] else {
            return [UInt16](repeating: 0, count: mapping.width * mapping.height)
        }
        return entry.data.withUnsafeBytes { raw in
            if entry.wide {
                let voxels = raw.bindMemory(to: UInt16.self)
                return mapping.mapSlice(slice) { voxels[$0] }
            }
            let voxels = raw.bindMemory(to: UInt8.self)
            return mapping.mapSlice(slice) { UInt16(voxels[$0]) }
        }
    }

    /// Voxels per label of one model, for the volumes in the label list.
    public func voxelCounts(_ model: String) -> [Int: Int] {
        guard let entry = labels[model] else { return [:] }
        var counts: [Int: Int] = [:]
        entry.data.withUnsafeBytes { raw in
            if entry.wide {
                for value in raw.bindMemory(to: UInt16.self) where value != 0 { counts[Int(value), default: 0] += 1 }
            } else {
                var histogram = [Int](repeating: 0, count: 256)
                for value in raw.bindMemory(to: UInt8.self) { histogram[Int(value)] += 1 }
                for (value, count) in histogram.enumerated() where value != 0 && count > 0 { counts[value] = count }
            }
        }
        return counts
    }

    /// Count, centroid and bounding box per label of one model, in one pass:
    /// the case report checks its voxel counts against the export, its sides
    /// against the midline and its cut levels against the face from these.
    public func labelStatistics(_ model: String) -> [Int: LabelStatistics] {
        guard let entry = labels[model] else { return [:] }
        let g = geometry
        var stats: [Int: LabelStatistics] = [:]
        func visit<T: FixedWidthInteger>(_ voxels: UnsafeBufferPointer<T>) {
            // Accumulated per label id in flat arrays: a dictionary lookup per
            // voxel would make this pass many times slower.
            let capacity = Int(voxels.max() ?? 0) + 1
            var count = [Int](repeating: 0, count: capacity)
            var sumI = [Double](repeating: 0, count: capacity)
            var sumJ = [Double](repeating: 0, count: capacity)
            var sumK = [Double](repeating: 0, count: capacity)
            var low = [VolumeIndex](repeating: VolumeIndex(i: .max, j: .max, k: .max), count: capacity)
            var high = [VolumeIndex](repeating: VolumeIndex(i: .min, j: .min, k: .min), count: capacity)
            var index = 0
            for k in 0..<g.nz {
                for j in 0..<g.ny {
                    for i in 0..<g.nx {
                        let value = Int(voxels[index])
                        index += 1
                        guard value != 0 else { continue }
                        count[value] += 1
                        sumI[value] += Double(i)
                        sumJ[value] += Double(j)
                        sumK[value] += Double(k)
                        if i < low[value].i { low[value].i = i }
                        if j < low[value].j { low[value].j = j }
                        if k < low[value].k { low[value].k = k }
                        if i > high[value].i { high[value].i = i }
                        if j > high[value].j { high[value].j = j }
                        if k > high[value].k { high[value].k = k }
                    }
                }
            }
            for value in 1..<max(capacity, 1) where count[value] > 0 {
                let n = Double(count[value])
                stats[value] = LabelStatistics(
                    count: count[value], centroid: (sumI[value] / n, sumJ[value] / n, sumK[value] / n),
                    low: low[value], high: high[value])
            }
        }
        entry.data.withUnsafeBytes { raw in
            if entry.wide { visit(raw.bindMemory(to: UInt16.self)) } else { visit(raw.bindMemory(to: UInt8.self)) }
        }
        return stats
    }
}

/// Where one label lies in the volume, in voxel indices of the LPS cache.
public struct LabelStatistics: Sendable {
    public var count: Int
    public var centroid: (i: Double, j: Double, k: Double)
    public var low: VolumeIndex
    public var high: VolumeIndex

    public init(count: Int, centroid: (i: Double, j: Double, k: Double), low: VolumeIndex, high: VolumeIndex) {
        self.count = count
        self.centroid = centroid
        self.low = low
        self.high = high
    }
}

/// A window as a table from every Int16 value to its gray level.
public struct WindowLUT: Sendable {
    public let width: Double
    public let center: Double
    public let table: [UInt8]

    public init(width: Double, center: Double) {
        self.width = max(1, width)
        self.center = center
        table = (0..<65536).map { Windowing.gray(Double($0 - 32768), width: max(1, width), center: center) }
    }
}

extension PlaneMapping {
    /// Every pixel of one slice in screen order, row by row, through `value`
    /// of its voxel's linear index. Faster than `extractSlice` because the
    /// index steps along a row instead of being recomputed for each pixel.
    public func mapSlice<T>(_ slice: Int, _ value: (Int) -> T) -> [T] {
        precondition((0..<sliceCount).contains(slice), "slice out of range")
        let g = geometry
        var out = [T]()
        out.reserveCapacity(width * height)
        for row in 0..<height {
            let first = g.linearIndex(voxel(column: 0, row: row, slice: slice))
            let last = g.linearIndex(voxel(column: width - 1, row: row, slice: slice))
            let step = width > 1 ? (last - first) / (width - 1) : 0
            var index = first
            for _ in 0..<width {
                out.append(value(index))
                index += step
            }
        }
        return out
    }
}

/// Fixed colours per label id, the same for every patient (plan §9):
/// Okabe–Ito and Paul Tol's muted and light schemes, chosen to stay apart
/// for the common colour-vision deficiencies; ids beyond 24 repeat them.
public enum LabelPalette {
    public static let colors: [(r: UInt8, g: UInt8, b: UInt8)] = [
        (0xE6, 0x9F, 0x00), (0x56, 0xB4, 0xE9), (0x00, 0x9E, 0x73), (0xF0, 0xE4, 0x42),
        (0x00, 0x72, 0xB2), (0xD5, 0x5E, 0x00), (0xCC, 0x79, 0xA7), (0xCC, 0x66, 0x77),
        (0x33, 0x22, 0x88), (0xDD, 0xCC, 0x77), (0x11, 0x77, 0x33), (0x88, 0xCC, 0xEE),
        (0x88, 0x22, 0x55), (0x44, 0xAA, 0x99), (0x99, 0x99, 0x33), (0xAA, 0x44, 0x99),
        (0x77, 0xAA, 0xDD), (0xEE, 0x88, 0x66), (0xEE, 0xDD, 0x88), (0xFF, 0xAA, 0xBB),
        (0x99, 0xDD, 0xFF), (0x44, 0xBB, 0x99), (0xBB, 0xCC, 0x33), (0xAA, 0xAA, 0x00),
    ]

    public static func color(_ label: Int) -> (r: UInt8, g: UInt8, b: UInt8) {
        colors[(max(label, 1) - 1) % colors.count]
    }
}

/// Builds the RGBA overlay of one slice from the label slices of the models
/// shown (plan §9): hidden labels have alpha 0, contour mode keeps the pixels
/// whose 4-neighbourhood holds another label, and models later in `layers`
/// are drawn over earlier ones.
public enum OverlayRenderer {
    public struct Layer: Sendable {
        public var labels: [UInt16]
        public var visible: Set<Int>
        public var highlighted: Int?

        public init(labels: [UInt16], visible: Set<Int>, highlighted: Int? = nil) {
            self.labels = labels
            self.visible = visible
            self.highlighted = highlighted
        }
    }

    public static func rgba(
        layers: [Layer], width: Int, height: Int, opacity: Double, contour: Bool
    ) -> [UInt8] {
        var out = [UInt8](repeating: 0, count: width * height * 4)
        let alpha = UInt8(max(0, min(1, opacity)) * 255)
        for layer in layers {
            let labels = layer.labels
            for row in 0..<height {
                for column in 0..<width {
                    let index = row * width + column
                    let value = Int(labels[index])
                    guard value != 0, layer.visible.contains(value) else { continue }
                    if contour {
                        let left = column > 0 ? Int(labels[index - 1]) : -1
                        let right = column < width - 1 ? Int(labels[index + 1]) : -1
                        let up = row > 0 ? Int(labels[index - width]) : -1
                        let down = row < height - 1 ? Int(labels[index + width]) : -1
                        guard left != value || right != value || up != value || down != value else { continue }
                    }
                    let color = LabelPalette.color(value)
                    let strong = layer.highlighted == value
                    out[index * 4] = color.r
                    out[index * 4 + 1] = color.g
                    out[index * 4 + 2] = color.b
                    out[index * 4 + 3] = contour || strong ? 255 : alpha
                }
            }
        }
        return out
    }
}
