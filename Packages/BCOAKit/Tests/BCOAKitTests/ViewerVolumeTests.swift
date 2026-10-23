import Foundation
import Testing
@testable import BCOAKit

/// A 6 × 5 × 4 cache with distinct values everywhere, and one label block,
/// written as the worker writes it.
private func cache() throws -> URL {
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent("bcoa-cache-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let (nx, ny, nz) = (6, 5, 4)
    var image = Data()
    var labels = Data()
    for k in 0..<nz {
        for j in 0..<ny {
            for i in 0..<nx {
                var value = Int16(i + 10 * j + 100 * k).littleEndian
                withUnsafeBytes(of: &value) { image.append(contentsOf: $0) }
                labels.append(i < 2 && j < 2 && k == 0 ? 3 : 0)
            }
        }
    }
    try image.write(to: folder.appendingPathComponent("image.i16"))
    try labels.write(to: folder.appendingPathComponent("labels_test.u8"))
    try Data("""
        {"version": 1, "size": [6, 5, 4], "spacing_mm": [1.0, 2.0, 3.0], "origin_mm": [0, 0, 0],
         "direction": [1,0,0,0,1,0,0,0,1], "oblique": false, "image": {"file": "image.i16"},
         "models": {"test": {"file": "labels_test.u8", "present": [3], "resampled": false}},
         "a_key_added_later": true}
        """.utf8).write(to: folder.appendingPathComponent("meta.json"))
    return folder
}

@Test func theCacheIsReadAsTheWorkerWroteIt() throws {
    let volume = try ViewerVolume(folder: try cache())
    #expect(volume.geometry == VolumeGeometry(nx: 6, ny: 5, nz: 4, sx: 1, sy: 2, sz: 3))
    #expect(volume.hu(at: VolumeIndex(i: 4, j: 3, k: 2)) == 234)
    #expect(volume.label("test", at: VolumeIndex(i: 1, j: 1, k: 0)) == 3)
    #expect(volume.label("test", at: VolumeIndex(i: 2, j: 1, k: 0)) == 0)
    #expect(volume.voxelCounts("test") == [3: 4])
    #expect(volume.meta.models["test"]?.present == [3])
}

@Test(arguments: Plane.allCases, [false, true])
func fastSlicesEqualTheReferenceExtraction(plane: Plane, neurological: Bool) throws {
    let volume = try ViewerVolume(folder: try cache())
    let mapping = PlaneMapping(plane: plane, geometry: volume.geometry, neurological: neurological)
    let identity = WindowLUT(width: 255, center: 127.5)
    for slice in 0..<mapping.sliceCount {
        let reference: [Int16] = (0..<mapping.height).flatMap { row in
            (0..<mapping.width).map { column in volume.hu(at: mapping.voxel(column: column, row: row, slice: slice)) }
        }
        let gray = volume.graySlice(mapping, slice: slice, window: identity)
        #expect(gray == reference.map { identity.table[Int($0) + 32768] })
        let labels = volume.labelSlice("test", mapping: mapping, slice: slice)
        let expected: [UInt16] = (0..<mapping.height).flatMap { row in
            (0..<mapping.width).map {
                UInt16(volume.label("test", at: mapping.voxel(column: $0, row: row, slice: slice)))
            }
        }
        #expect(labels == expected)
    }
}

@Test func hiddenLabelsAreTransparentAndContoursKeepTheEdge() {
    // A 4 × 4 square of label 2 in a 6 × 6 slice.
    var labels = [UInt16](repeating: 0, count: 36)
    for row in 1...4 { for column in 1...4 { labels[row * 6 + column] = 2 } }
    let filled = OverlayRenderer.rgba(
        layers: [.init(labels: labels, visible: [2])], width: 6, height: 6, opacity: 0.5, contour: false)
    #expect(filled[(2 * 6 + 2) * 4 + 3] == 127)
    #expect(filled[3] == 0)
    let hidden = OverlayRenderer.rgba(
        layers: [.init(labels: labels, visible: [])], width: 6, height: 6, opacity: 0.5, contour: false)
    #expect(hidden.allSatisfy { $0 == 0 })
    let contour = OverlayRenderer.rgba(
        layers: [.init(labels: labels, visible: [2])], width: 6, height: 6, opacity: 0.5, contour: true)
    // The edge of the square stays, its inside goes.
    #expect(contour[(1 * 6 + 1) * 4 + 3] == 255)
    #expect(contour[(2 * 6 + 2) * 4 + 3] == 0)
    let color = LabelPalette.color(2)
    #expect(Array(contour[(1 * 6 + 1) * 4 ..< (1 * 6 + 1) * 4 + 3]) == [color.r, color.g, color.b])
}

@Test func paletteColoursAreDistinctAndRepeatBeyondTheList() {
    let colours = LabelPalette.colors.map { "\($0.r)-\($0.g)-\($0.b)" }
    #expect(Set(colours).count == colours.count)
    #expect(LabelPalette.color(1) == LabelPalette.color(25))
}
