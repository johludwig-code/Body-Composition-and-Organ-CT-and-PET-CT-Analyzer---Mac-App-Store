import Foundation
import Testing
@testable import BCOAKit

private func sources() -> [IndexSource] {
    [
        IndexSource(id: 2, root: URL(fileURLWithPath: "/Volumes/Share/Cohort B"), status: .active,
                    volumeKind: "network"),
        IndexSource(id: 1, root: URL(fileURLWithPath: "/Users/r/Cohort A"), status: .active,
                    volumeKind: "local"),
        IndexSource(id: 3, root: URL(fileURLWithPath: "/gone"), status: .removed, volumeKind: "local"),
    ]
}

@Test func aScanPayloadHasWhatTheSchemaRequires() throws {
    let payload = IndexJobs.withLinkKey(
        IndexJobs.scan(sources: sources(), scan: [2, 1], mergedGeneration: 7),
        key: "base64:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=", keyID: "3683c115de125163")
    for key in ["mode", "catalog", "previews_dir", "sources", "scan_sources", "force_reread",
                "link_key", "link_key_id", "merged_generation", "selection", "identity"] {
        #expect(payload[key] != nil, "\(key)")
    }
    #expect(payload["scan_sources"] == .array([.number(1), .number(2)]))
    guard case .array(let listed) = payload["sources"] else { Issue.record("no sources"); return }
    #expect(listed.map { $0["source_id"] } == [.number(1), .number(2), .number(3)])
    // A removed source is listed without its root and without a volume kind.
    #expect(listed[2]["root"] == .null)
    #expect(listed[2]["volume_kind"] == nil)
    #expect(listed[1]["volume_kind"] == .string("network"))
}

@Test func theFixtureIsWhatTheAppWouldWrite() throws {
    // The scan fixture is the contract both suites parse; the app's own
    // payload for the same sources must agree with it key for key.
    let url = projectRoot.appendingPathComponent("Protocol/fixtures/jobs/index_scan.json")
    let fixture = try JSONDecoder().decode(WorkerJob.self, from: Data(contentsOf: url))
    let built = IndexJobs.scan(sources: [], scan: [1], mergedGeneration: 7)
    #expect(Set(fixture.payload.keys).isSuperset(of: Set(built.keys)))
}

@Test func withoutAKeyBothAreNull() {
    let payload = IndexJobs.withLinkKey([:], key: nil, keyID: "3683c115de125163")
    #expect(payload["link_key"] == .null)
    #expect(payload["link_key_id"] == .null)
}

@Test func anActiveSourceWithoutARootIsUnavailable() {
    let source = IndexSource(id: 1, root: nil, status: .active, volumeKind: "local")
    #expect(source.status == .unavailable)
    #expect(IndexJobs.scannable([source], among: [1]).isEmpty)
}

@Test func theResultsOfBothFixturesDecode() throws {
    for name in ["result_index", "result_index_unchanged"] {
        let url = projectRoot.appendingPathComponent("Protocol/fixtures/events/\(name).json")
        let line = try String(contentsOf: url, encoding: .utf8)
        guard case .result(_, let payload) = try WorkerEvent.parse(line: line.trimmingCharacters(in: .whitespacesAndNewlines)) else {
            Issue.record("not a result"); continue
        }
        let result = try #require(IndexResult(payload: payload))
        #expect(result.changed == (name == "result_index"))
        #expect(result.sources[1]?.state == "complete")
    }
}

@Test func aPreviewsPayloadMatchesTheFixtureAndHoldsNoKey() throws {
    let url = projectRoot.appendingPathComponent("Protocol/fixtures/jobs/index_previews.json")
    let fixture = try JSONDecoder().decode(WorkerJob.self, from: Data(contentsOf: url))
    let built = IndexJobs.previews(sources: [], catalogID: String(repeating: "3f", count: 16), partRefs: Array(1...70))
    #expect(Set(built.keys) == Set(fixture.payload.keys))
    #expect(built["link_key"] == nil)
    if case .array(let parts) = built["parts"] { #expect(parts.count == 64) } else { Issue.record("no parts") }
    #expect(WorkerJob(jobID: "j_p", kind: .index, projectDir: projectRoot, resourcesDir: projectRoot,
                      payload: built).exclusiveGroup == nil)
}
