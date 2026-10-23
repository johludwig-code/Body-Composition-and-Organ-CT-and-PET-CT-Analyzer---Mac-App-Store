import Foundation
import GRDB
import Testing
@testable import BCOAStore

private let organs = PipelineStore.ModelEntry(
    name: "clin_ct_organs", zipSHA256: String(repeating: "a", count: 64), labels: [8: "liver", 15: "spleen"])

/// One patient with one study and two series, the first selected.
private func project() throws -> DatabaseQueue {
    let db = try ProjectStore.inMemory()
    try db.write { db in
        try db.execute(sql: """
            INSERT INTO patients (patient_key, pseudonym) VALUES ('pt_000001', 'P0001');
            INSERT INTO studies (study_key, patient_key, study_uid, study_date) VALUES ('st_000001', 'pt_000001', '2.25.1', '2024-01-02');
            INSERT INTO series (series_key, study_key, series_uid, modality, image_count, fingerprint, selected, is_primary, catalog_part)
                VALUES ('s_000001', 'st_000001', '2.25.11', 'CT', 300, 'fp1', 1, 1, 'abcd:7'),
                       ('s_000002', 'st_000001', '2.25.12', 'CT', 60, 'fp2', 0, 0, 'abcd:8');
            """)
    }
    return db
}

private func row(_ label: Int, _ name: String) -> PipelineStore.ResultRow {
    PipelineStore.ResultRow(
        model: "clin_ct_organs", labelID: label, labelName: name, voxelCount: 100, volumeML: 0.1,
        huMean: 50, huSD: 5, huMedian: 50, huP05: 40, huP95: 60, huMin: 30, huMax: 70,
        touchesBorder: false, device: "mps", flags: [])
}

@Test func aRunIsReusedUntilItIsLockedOrItsSettingsChange() throws {
    let db = try project()
    try db.write { db in
        let now = Date(timeIntervalSince1970: 1_792_000_000)
        let first = try PipelineStore.run(models: [organs], device: "auto", settings: ["pipeline_version": "1"],
                                          versions: ["app": "0.1.0"], in: db, now: now)
        #expect(first.hasPrefix("R2026") && first.count == 16)
        #expect(try PipelineStore.run(models: [organs], device: "auto", settings: ["pipeline_version": "1"],
                                      versions: [:], in: db, now: now) == first)
        let other = try PipelineStore.run(models: [organs], device: "cpu", settings: ["pipeline_version": "1"],
                                          versions: [:], in: db, now: now)
        #expect(other != first)
        try PipelineStore.lock(first, in: db)
        let third = try PipelineStore.run(models: [organs], device: "auto", settings: ["pipeline_version": "1"],
                                          versions: [:], in: db, now: now)
        #expect(third != first && third != other)
        #expect(try PipelineStore.models(of: first, in: db) == [organs])
    }
}

@Test func theWorkersVersionsJoinTheRunOnce() throws {
    let db = try project()
    try db.write { db in
        let run = try PipelineStore.run(models: [organs], device: "auto", settings: [:],
                                        versions: ["app": "0.1.0", "chip": "Apple M4 Pro"], in: db)
        try PipelineStore.mergeVersions(["moosez": "3.2.2", "app": "9.9"], into: run, in: db)
        try PipelineStore.mergeVersions(["moosez": "4.0"], into: run, in: db)
        let text = try #require(try String.fetchOne(db, sql: "SELECT versions_json FROM runs"))
        let versions = try #require(try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: String])
        #expect(versions == ["app": "0.1.0", "chip": "Apple M4 Pro", "moosez": "3.2.2"])
    }
}

@Test func onlySelectedSeriesWithoutResultsAreAnalyzed() throws {
    let db = try project()
    try db.write { db in
        let run = try PipelineStore.run(models: [organs], device: "auto", settings: [:], versions: [:], in: db)
        #expect(try PipelineStore.seriesToAnalyze(runID: run, models: ["clin_ct_organs"], in: db) == ["s_000001"])
        try PipelineStore.storeResults([row(8, "liver"), row(15, "spleen")], runID: run, seriesKey: "s_000001", in: db)
        #expect(try PipelineStore.seriesToAnalyze(runID: run, models: ["clin_ct_organs"], in: db).isEmpty)
        // Stored again, the rows replace the old ones.
        try PipelineStore.storeResults([row(8, "liver")], runID: run, seriesKey: "s_000001", in: db)
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM results") == 1)
        #expect(try PipelineStore.catalogPart(of: "s_000001", in: db)! == ("abcd", 7))
    }
}

@Test func leftoverJobsAreInterruptedAndReturned() throws {
    let db = try project()
    try db.write { db in
        let run = try PipelineStore.run(models: [organs], device: "auto", settings: [:], versions: [:], in: db)
        try PipelineStore.recordJob(id: "j_1", runID: run, seriesKey: "s_000001", kind: "convert", status: "queued", in: db)
        try PipelineStore.updateJob(id: "j_1", status: "done", errorCode: nil, in: db)
        try PipelineStore.recordJob(id: "j_2", runID: run, seriesKey: "s_000001", kind: "segment", status: "queued", in: db)
        try PipelineStore.updateJob(id: "j_2", status: "running", errorCode: nil, in: db)
        let pending = try PipelineStore.interruptLeftovers(in: db)
        #expect(pending.map(\.kind) == ["segment"])
        #expect(try String.fetchOne(db, sql: "SELECT status FROM jobs WHERE job_id = 'j_2'") == "interrupted")
        let progress = try PipelineStore.progress(runID: run, in: db)
        #expect(progress.map(\.seriesKey) == ["s_000001"])
        #expect(progress.first?.stages["convert"]?.status == "done")
        #expect(progress.first?.stages["segment"]?.status == "interrupted")
    }
}
