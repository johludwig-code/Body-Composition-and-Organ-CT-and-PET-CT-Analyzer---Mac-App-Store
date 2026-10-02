import Foundation
import Testing
@testable import BCOAKit

/// BCOAnalyzer/ — found from this file so the fixtures are shared with the
/// Python suite instead of copied.
let projectRoot = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent()

private func fixtures(_ folder: String) throws -> [URL] {
    let directory = projectRoot.appendingPathComponent("Protocol/fixtures/\(folder)")
    return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil)
        .filter { $0.pathExtension == "json" }
        .sorted { $0.lastPathComponent < $1.lastPathComponent }
}

@Test func everyEventFixtureDecodes() throws {
    let files = try fixtures("events")
    #expect(files.count >= 8)
    for file in files {
        let line = try String(contentsOf: file, encoding: .utf8)
        _ = try WorkerEvent.parse(line: line.trimmingCharacters(in: .whitespacesAndNewlines))
    }
}

@Test func everyJobFixtureDecodes() throws {
    for file in try fixtures("jobs") {
        let job = try JSONDecoder().decode(WorkerJob.self, from: Data(contentsOf: file))
        #expect(job.protocolVersion == WorkerProtocol.version)
    }
}

@Test func progressWithNullFractionIsStageProgress() throws {
    let event = try WorkerEvent.parse(
        line: #"{"type":"progress","job_id":"j_1","stage":"segment","model":"clin_ct_organs","fraction":null,"message":"Model 2 of 5"}"#)
    #expect(event == .progress(jobID: "j_1", stage: .segment, model: "clin_ct_organs", fraction: nil, message: "Model 2 of 5"))
}

@Test func progressWithoutFractionIsRefused() {
    #expect(throws: (any Error).self) {
        try WorkerEvent.parse(line: #"{"type":"progress","job_id":"j","stage":"index","message":""}"#)
    }
}

@Test func unknownEventTypeIsRefused() {
    #expect(throws: (any Error).self) {
        try WorkerEvent.parse(line: #"{"type":"nonsense"}"#)
    }
}

@Test func jobWrittenByTheAppUsesTheSchemaKeys() throws {
    let job = WorkerJob(
        jobID: "j_0001", kind: .spikeS2,
        projectDir: URL(fileURLWithPath: "/tmp/Study.bcoaproj"),
        resourcesDir: URL(fileURLWithPath: "/Applications/X.app/Contents/Resources"),
        payload: ["source_dir": .string("/tmp/Source")])
    let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(job)) as? [String: Any]
    #expect(Set(object?.keys.map { $0 } ?? []) == [
        "protocol_version", "job_id", "kind", "project_dir", "log_path",
        "resources_dir", "heartbeat_seconds", "payload",
    ])
    #expect(object?["kind"] as? String == "spike_s2")
    #expect(object?["log_path"] as? String == "/tmp/Study.bcoaproj/logs/j_0001.log")
}

@Test func jobWithoutHeartbeatDecodesWithDefault() throws {
    let raw = #"{"protocol_version":1,"job_id":"j_1","kind":"index","project_dir":"/p","log_path":"/p/l","resources_dir":"/r","payload":{}}"#
    let job = try JSONDecoder().decode(WorkerJob.self, from: Data(raw.utf8))
    #expect(job.heartbeatSeconds == 10)
}

@Test func intendedUseMatchesTheWorker() throws {
    let python = try String(
        contentsOf: projectRoot.appendingPathComponent("Worker/bcoa_worker/intended_use.py"), encoding: .utf8)
    #expect(python.contains(IntendedUse.short))
    let joined = python.components(separatedBy: "\n")
        .map { $0.trimmingCharacters(in: .whitespaces) }
        .filter { $0.hasPrefix("\"") }
        .map { $0.trimmingCharacters(in: CharacterSet(charactersIn: "\"")) }
        .joined()
    #expect(joined.contains(IntendedUse.statement))
}
