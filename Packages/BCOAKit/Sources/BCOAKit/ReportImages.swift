import CoreGraphics
import Foundation

/// One image of the case report: a CT slice from the viewer cache with the
/// segmentation laid over it, in the viewer's orientation (ADR 0007), so the
/// report and the viewer can never disagree about left and right.
public struct ReportImage: Sendable {
    public var caption: String
    public var plane: Plane
    public var width: Int
    public var height: Int
    /// Opaque RGBA, row by row in screen order.
    public var rgba: [UInt8]
    /// Millimetres per pixel, horizontally and vertically.
    public var spacing: (horizontal: Double, vertical: Double)
    public var letters: EdgeLetters
    /// The slices towards superior (k) the image shows, for the face check.
    public var kRange: ClosedRange<Int>
    public var legend: [(name: String, color: (r: UInt8, g: UInt8, b: UInt8))]

    /// Height over width on paper, from the voxel spacing.
    public var aspect: Double {
        Double(height) * spacing.vertical / (Double(width) * spacing.horizontal)
    }

    public func cgImage() -> CGImage? {
        guard let provider = CGDataProvider(data: Data(rgba) as CFData) else { return nil }
        return CGImage(
            width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: width * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB) ?? CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue),
            provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
    }
}

public enum ReportImages {
    public static let opacity = 0.45

    /// One slice with the labels of `model` over it. Coronal and sagittal
    /// images end at `cutK` and are not drawn without one (ADR 0017 section
    /// 3); an axial slice above it is not drawn either.
    public static func render(
        _ volume: ViewerVolume, plane: Plane, slice: Int, window: WindowPreset, model: String?,
        cutK: Int?, caption: String, names: [Int: String] = [:]
    ) -> ReportImage? {
        let mapping = PlaneMapping(plane: plane, geometry: volume.geometry)
        guard (0..<mapping.sliceCount).contains(slice) else { return nil }
        let g = volume.geometry
        let rows: Range<Int>
        let kRange: ClosedRange<Int>
        switch plane {
        case .axial:
            if let cutK, slice > cutK { return nil }
            rows = 0..<mapping.height
            kRange = slice...slice
        case .coronal, .sagittal:
            guard let cutK, cutK >= 0 else { return nil }
            // Row 0 is the top slice, k = nz − 1; keep the rows at or below the cut.
            rows = (g.nz - 1 - min(cutK, g.nz - 1))..<g.nz
            kRange = 0...min(cutK, g.nz - 1)
        }
        let lut = WindowLUT(width: window.width, center: window.center)
        let gray = volume.graySlice(mapping, slice: slice, window: lut)
        let width = mapping.width
        var visible = Set<Int>()
        var labels: [UInt16] = []
        if let model {
            labels = volume.labelSlice(model, mapping: mapping, slice: slice)
            // Only the rows the image keeps: a label above the cut is drawn
            // nowhere and must not stand in the legend either.
            visible = Set(labels[(rows.lowerBound * width)..<(rows.upperBound * width)].lazy
                .filter { $0 != 0 }.map(Int.init))
        }
        let overlay = labels.isEmpty
            ? []
            : OverlayRenderer.rgba(
                layers: [.init(labels: labels, visible: visible)], width: width, height: mapping.height,
                opacity: opacity, contour: false)
        var rgba = [UInt8](repeating: 255, count: width * rows.count * 4)
        for (outRow, row) in rows.enumerated() {
            for column in 0..<width {
                let source = row * width + column
                let target = (outRow * width + column) * 4
                var r = Double(gray[source]), gg = r, b = r
                if !overlay.isEmpty, overlay[source * 4 + 3] > 0 {
                    let a = Double(overlay[source * 4 + 3]) / 255
                    r = r * (1 - a) + Double(overlay[source * 4]) * a
                    gg = gg * (1 - a) + Double(overlay[source * 4 + 1]) * a
                    b = b * (1 - a) + Double(overlay[source * 4 + 2]) * a
                }
                rgba[target] = UInt8(r.rounded())
                rgba[target + 1] = UInt8(gg.rounded())
                rgba[target + 2] = UInt8(b.rounded())
            }
        }
        // The legend names what this slice shows, in the catalog's order.
        let legend = visible.sorted().compactMap { id -> (String, (UInt8, UInt8, UInt8))? in
            guard let name = names[id] else { return nil }
            return (name, LabelPalette.color(id))
        }
        return ReportImage(
            caption: caption, plane: plane, width: width, height: rows.count, rgba: rgba,
            spacing: (mapping.pixelSpacing.horizontal, mapping.pixelSpacing.vertical),
            letters: mapping.letters, kRange: kRange,
            legend: legend.map { (name: $0.0, color: (r: $0.1.0, g: $0.1.1, b: $0.1.2)) })
    }

    /// The slice through the centroid of the given labels, weighted by size:
    /// k for axial, j for coronal, i for sagittal.
    public static func slice(through stats: [LabelStatistics], plane: Plane) -> Int? {
        let total = stats.map(\.count).reduce(0, +)
        guard total > 0 else { return nil }
        func mean(_ value: (LabelStatistics) -> Double) -> Int {
            Int((stats.map { value($0) * Double($0.count) }.reduce(0, +) / Double(total)).rounded())
        }
        switch plane {
        case .axial: return mean(\.centroid.k)
        case .coronal: return mean(\.centroid.j)
        case .sagittal: return mean(\.centroid.i)
        }
    }
}
