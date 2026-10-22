import Foundation
import Testing
@testable import BCOAKit

#if DEBUG
private let optimized = false
#else
private let optimized = true
#endif

/// Plan §9: scrolling at 30 images per second at 512 × 512. One axial frame
/// is the gray slice through the window table plus the overlay of ten
/// models; the budget is 33 ms. Measured only in a release build
/// (`swift test -c release --filter ViewerSpeed`): unoptimized, the same code
/// took 460 ms on the M4 Pro, 85 times as long, and says nothing about the
/// app that ships.
@Test(.enabled(if: optimized, "a speed budget holds for optimized code only"))
func anAxialFrameOfTenModelsFitsThirtyFramesPerSecond() throws {
    let n = 512, slices = 4
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent("bcoa-speed-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: folder) }
    var image = Data(count: n * n * slices * 2)
    image.withUnsafeMutableBytes { raw in
        let values = raw.bindMemory(to: Int16.self)
        for index in values.indices { values[index] = Int16(index % 2000 - 1000) }
    }
    try image.write(to: folder.appendingPathComponent("image.i16"))
    var models: [String] = []
    for m in 0..<10 {
        var labels = Data(count: n * n * slices)
        labels.withUnsafeMutableBytes { raw in
            for index in 0..<raw.count where (index / 37 + m) % 5 == 0 { raw[index] = UInt8(1 + (index / 4096) % 19) }
        }
        try labels.write(to: folder.appendingPathComponent("labels_m\(m).u8"))
        models.append(#""m\#(m)": {"file": "labels_m\#(m).u8", "present": [1]}"#)
    }
    try Data(#"{"size": [512, 512, 4], "spacing_mm": [0.8, 0.8, 2.5], "models": {\#(models.joined(separator: ","))}}"#.utf8)
        .write(to: folder.appendingPathComponent("meta.json"))
    let volume = try ViewerVolume(folder: folder)
    let mapping = PlaneMapping(plane: .axial, geometry: volume.geometry)
    let window = WindowLUT(width: 400, center: 40)
    let visible = Set(1...19)
    let frames = 20
    let start = ContinuousClock.now
    for frame in 0..<frames {
        let slice = frame % slices
        _ = volume.graySlice(mapping, slice: slice, window: window)
        let layers = volume.models.map {
            OverlayRenderer.Layer(labels: volume.labelSlice($0, mapping: mapping, slice: slice), visible: visible)
        }
        _ = OverlayRenderer.rgba(layers: layers, width: n, height: n, opacity: 0.45, contour: false)
    }
    let perFrame = (ContinuousClock.now - start) / frames
    let budget = Duration.milliseconds(33)
    print("viewer frame: \(perFrame)")
    #expect(perFrame < budget, "\(perFrame) per frame")
}
