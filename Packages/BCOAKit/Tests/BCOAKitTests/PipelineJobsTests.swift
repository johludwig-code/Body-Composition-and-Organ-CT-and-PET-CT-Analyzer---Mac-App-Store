import Foundation
import Testing
@testable import BCOAKit

private func fixture(_ name: String) throws -> WorkerJob {
    let url = projectRoot.appendingPathComponent("Protocol/fixtures/jobs/\(name).json")
    return try JSONDecoder().decode(WorkerJob.self, from: Data(contentsOf: url))
}

@Test func theStagesFollowEachOther() {
    #expect(PipelineJobs.next(after: .convert) == .segment)
    #expect(PipelineJobs.next(after: .segment) == .metrics)
    #expect(PipelineJobs.next(after: .metrics) == .viewerCache)
    #expect(PipelineJobs.next(after: .viewerCache) == nil)
    #expect(PipelineJobs.next(after: .export) == nil)
}

@Test func eachPayloadHasTheFixturesKeys() throws {
    let sources = [IndexSource(id: 1, root: URL(fileURLWithPath: "/Volumes/Share/Cohort A"), status: .active,
                               volumeKind: "network")]
    let built: [(String, [String: JSONValue])] = [
        ("convert", PipelineJobs.convert(seriesKey: "s_000003", catalogID: String(repeating: "3f", count: 16),
                                         partRef: 15, sources: sources)),
        ("segment", PipelineJobs.segment(seriesKey: "s_0003", run: "R20261002T120000", models: ["clin_ct_organs"],
                                         device: "auto")),
        ("metrics", PipelineJobs.metrics(seriesKey: "s_000003", run: "R20261002T120000", models: ["clin_ct_organs"],
                                         device: "mps", fallback: false, flags: ["thick_slices"])),
        ("viewer_cache", PipelineJobs.viewerCache(seriesKey: "s_000003", run: "R20261002T120000",
                                                  models: ["clin_ct_organs"])),
    ]
    for (name, payload) in built {
        let job = try fixture(name)
        #expect(Set(payload.keys) == Set(job.payload.keys), "\(name)")
    }
    // The fixtures are what the app would write for the same series.
    #expect(built[2].1 == (try fixture("metrics")).payload)
    #expect(built[3].1 == (try fixture("viewer_cache")).payload)
    #expect(built[1].1["retry_on_cpu"] == .bool(false))
}

@Test func theManifestIsReadAndHelpersAreNotOffered() throws {
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent("bcoa-manifest-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let manifest = folder.appendingPathComponent("manifest.json")
    try Data("""
        {"models": [
          {"name": "clin_ct_body_composition", "labels": {"1": "skeletal_muscle"}, "requires": ["clin_ct_fast_vertebrae"]},
          {"name": "clin_ct_fast_vertebrae", "labels": {"1": "vertebra_C1"}, "requires": []},
          {"name": "clin_ct_organs", "labels": {"8": "liver", "15": "spleen"}, "zip_sha256": "aa", "licence": "CC-BY-4.0"}
        ]}
        """.utf8).write(to: manifest)
    let models = BundledModel.load(from: manifest)
    #expect(models.map(\.name) == ["clin_ct_body_composition", "clin_ct_fast_vertebrae", "clin_ct_organs"])
    #expect(BundledModel.offered(models).map(\.name) == ["clin_ct_body_composition", "clin_ct_organs"])
    #expect(models.last?.labels == [8: "liver", 15: "spleen"])
    #expect(models.first?.isSlow == true)
}

@Test func theMachineSaysWhatItIs() {
    #expect(!MachineInfo.chip.isEmpty)
    #expect(MachineInfo.memoryGB > 0)
    #expect(MachineInfo.macOS.split(separator: ".").count == 3)
}
