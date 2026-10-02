import Testing
@testable import BCOAKit

/// A marker at the patient's left, anterior, superior corner of an LPS
/// volume: +x is left, so the largest i; +y is posterior, so the smallest j;
/// +z is superior, so the largest k (plan §9, "Tests").
private let geometry = VolumeGeometry(nx: 4, ny: 5, nz: 6, sx: 0.8, sy: 0.8, sz: 3.0)
private let marker = VolumeIndex(i: 3, j: 0, k: 5)

private func quadrant(_ plane: Plane, neurological: Bool = false) -> (right: Bool, top: Bool) {
    let mapping = PlaneMapping(plane: plane, geometry: geometry, neurological: neurological)
    let p = mapping.pixel(of: marker)
    return (p.column >= mapping.width / 2, p.row < mapping.height / 2)
}

@Test func axialShowsPatientLeftOnScreenRightAndAnteriorAtTop() {
    let q = quadrant(.axial)
    #expect(q.right)
    #expect(q.top)
}

@Test func coronalShowsPatientLeftOnScreenRightAndSuperiorAtTop() {
    let q = quadrant(.coronal)
    #expect(q.right)
    #expect(q.top)
}

@Test func sagittalShowsAnteriorOnScreenLeftAndSuperiorAtTop() {
    let q = quadrant(.sagittal)
    #expect(!q.right)
    #expect(q.top)
}

@Test func neurologicalConventionMirrorsAxialAndCoronalOnly() {
    for plane in [Plane.axial, .coronal] {
        let q = quadrant(plane, neurological: true)
        #expect(!q.right)
        #expect(q.top)
    }
    let sagittal = quadrant(.sagittal, neurological: true)
    #expect(!sagittal.right)
    #expect(sagittal.top)
}

@Test func lettersFollowThePlanTable() {
    #expect(PlaneMapping(plane: .axial, geometry: geometry).letters
        == EdgeLetters(left: "R", right: "L", top: "A", bottom: "P"))
    #expect(PlaneMapping(plane: .coronal, geometry: geometry).letters
        == EdgeLetters(left: "R", right: "L", top: "S", bottom: "I"))
    #expect(PlaneMapping(plane: .sagittal, geometry: geometry).letters
        == EdgeLetters(left: "A", right: "P", top: "S", bottom: "I"))
    #expect(PlaneMapping(plane: .axial, geometry: geometry, neurological: true).letters
        == EdgeLetters(left: "L", right: "R", top: "A", bottom: "P"))
}

@Test(arguments: Plane.allCases, [false, true])
func pixelAndVoxelAreInverse(plane: Plane, neurological: Bool) {
    let mapping = PlaneMapping(plane: plane, geometry: geometry, neurological: neurological)
    for slice in 0..<mapping.sliceCount {
        for row in 0..<mapping.height {
            for column in 0..<mapping.width {
                let v = mapping.voxel(column: column, row: row, slice: slice)
                let p = mapping.pixel(of: v)
                #expect(p.column == column && p.row == row && p.slice == slice)
            }
        }
    }
}

@Test func coronalAndSagittalAreStretchedInZ() {
    #expect(PlaneMapping(plane: .axial, geometry: geometry).physicalAspect == 1)
    #expect(PlaneMapping(plane: .coronal, geometry: geometry).physicalAspect == 3.0 / 0.8)
    #expect(PlaneMapping(plane: .sagittal, geometry: geometry).physicalAspect == 3.0 / 0.8)
}

@Test func extractedSliceHasTheMarkerWhereThePixelMappingSays() {
    var volume = [UInt8](repeating: 0, count: geometry.nx * geometry.ny * geometry.nz)
    volume[geometry.linearIndex(marker)] = 7
    for plane in Plane.allCases {
        let mapping = PlaneMapping(plane: plane, geometry: geometry)
        let p = mapping.pixel(of: marker)
        let slice = volume.withUnsafeBufferPointer { mapping.extractSlice(p.slice, from: $0) }
        #expect(slice.count == mapping.width * mapping.height)
        #expect(slice[p.row * mapping.width + p.column] == 7)
        #expect(slice.filter { $0 != 0 }.count == 1)
    }
}
