import Foundation

/// Volume axes in canonical LPS, as the worker writes the viewer cache:
/// index i runs along +x (towards the patient's left), j along +y
/// (posterior), k along +z (superior); i varies fastest in memory.
public struct VolumeIndex: Hashable, Sendable {
    public var i: Int
    public var j: Int
    public var k: Int

    public init(i: Int, j: Int, k: Int) {
        self.i = i
        self.j = j
        self.k = k
    }
}

public struct VolumeGeometry: Equatable, Sendable {
    public var nx: Int, ny: Int, nz: Int
    /// Millimetres along x, y, z.
    public var sx: Double, sy: Double, sz: Double

    public init(nx: Int, ny: Int, nz: Int, sx: Double, sy: Double, sz: Double) {
        precondition(nx > 0 && ny > 0 && nz > 0, "empty volume")
        precondition(sx > 0 && sy > 0 && sz > 0, "spacing must be positive")
        self.nx = nx
        self.ny = ny
        self.nz = nz
        self.sx = sx
        self.sy = sy
        self.sz = sz
    }

    public func linearIndex(_ v: VolumeIndex) -> Int { v.i + nx * (v.j + ny * v.k) }
}

public enum Plane: String, CaseIterable, Sendable {
    case axial, coronal, sagittal
}

/// Orientation letters at the four edges of a displayed slice.
public struct EdgeLetters: Equatable, Sendable {
    public var left: String, right: String, top: String, bottom: String
}

/// How one plane of an LPS volume is laid out on screen (plan §9).
///
/// Radiological convention: the patient's right is on the screen's left. With
/// LPS data that needs no flip in x at all — +x already points to the
/// patient's left, which is the screen's right. Only the vertical axis of the
/// coronal and sagittal planes is reversed, because +z points up in the
/// patient and rows count down on screen.
public struct PlaneMapping: Equatable, Sendable {
    public let plane: Plane
    public let geometry: VolumeGeometry
    public let neurological: Bool

    public init(plane: Plane, geometry: VolumeGeometry, neurological: Bool = false) {
        self.plane = plane
        self.geometry = geometry
        self.neurological = neurological
    }

    public var width: Int {
        switch plane {
        case .axial, .coronal: geometry.nx
        case .sagittal: geometry.ny
        }
    }

    public var height: Int {
        switch plane {
        case .axial: geometry.ny
        case .coronal, .sagittal: geometry.nz
        }
    }

    public var sliceCount: Int {
        switch plane {
        case .axial: geometry.nz
        case .coronal: geometry.ny
        case .sagittal: geometry.nx
        }
    }

    /// Millimetres per screen pixel horizontally and vertically. Coronal and
    /// sagittal are stretched in z by `sz / sx`; drawing them square makes a
    /// 5 mm-slice CT look squashed to a third of its height.
    public var pixelSpacing: (horizontal: Double, vertical: Double) {
        switch plane {
        case .axial: (geometry.sx, geometry.sy)
        case .coronal: (geometry.sx, geometry.sz)
        case .sagittal: (geometry.sy, geometry.sz)
        }
    }

    public var physicalAspect: Double { pixelSpacing.vertical / pixelSpacing.horizontal }

    /// Only axial and coronal mirror in neurological convention; a sagittal
    /// view has no left and right to swap.
    private var mirrored: Bool { neurological && plane != .sagittal }

    public func voxel(column: Int, row: Int, slice: Int) -> VolumeIndex {
        let c = mirrored ? width - 1 - column : column
        switch plane {
        case .axial: return VolumeIndex(i: c, j: row, k: slice)
        case .coronal: return VolumeIndex(i: c, j: slice, k: geometry.nz - 1 - row)
        case .sagittal: return VolumeIndex(i: slice, j: c, k: geometry.nz - 1 - row)
        }
    }

    public func pixel(of v: VolumeIndex) -> (column: Int, row: Int, slice: Int) {
        let raw: (Int, Int, Int)
        switch plane {
        case .axial: raw = (v.i, v.j, v.k)
        case .coronal: raw = (v.i, geometry.nz - 1 - v.k, v.j)
        case .sagittal: raw = (v.j, geometry.nz - 1 - v.k, v.i)
        }
        let column = mirrored ? width - 1 - raw.0 : raw.0
        return (column, raw.1, raw.2)
    }

    public var letters: EdgeLetters {
        switch plane {
        case .axial:
            mirrored
                ? EdgeLetters(left: "L", right: "R", top: "A", bottom: "P")
                : EdgeLetters(left: "R", right: "L", top: "A", bottom: "P")
        case .coronal:
            mirrored
                ? EdgeLetters(left: "L", right: "R", top: "S", bottom: "I")
                : EdgeLetters(left: "R", right: "L", top: "S", bottom: "I")
        case .sagittal:
            EdgeLetters(left: "A", right: "P", top: "S", bottom: "I")
        }
    }

    /// Copies one slice out of the memory-mapped volume, row by row in
    /// screen order. Generic over the voxel type: CT is Int16, labelmaps are
    /// UInt8 or UInt16.
    public func extractSlice<T>(_ slice: Int, from volume: UnsafeBufferPointer<T>) -> [T] {
        precondition(volume.count == geometry.nx * geometry.ny * geometry.nz, "volume size mismatch")
        precondition((0..<sliceCount).contains(slice), "slice out of range")
        var out = [T]()
        out.reserveCapacity(width * height)
        for row in 0..<height {
            for column in 0..<width {
                out.append(volume[geometry.linearIndex(voxel(column: column, row: row, slice: slice))])
            }
        }
        return out
    }
}
