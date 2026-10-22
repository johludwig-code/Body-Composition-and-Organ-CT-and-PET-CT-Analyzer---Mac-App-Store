import Foundation
import GRDB
import Testing
@testable import BCOAStore

@Test func theNewestDecisionCountsAndEveryOneIsAudited() throws {
    let db = try ProjectStore.inMemory()
    try db.write { db in
        try db.execute(sql: """
            INSERT INTO patients (patient_key, pseudonym) VALUES ('pt_000001', 'P0001');
            INSERT INTO studies (study_key, patient_key, study_uid) VALUES ('st_000001', 'pt_000001', '2.25.1');
            INSERT INTO series (series_key, study_key, series_uid, modality, image_count, fingerprint)
                VALUES ('s_000001', 'st_000001', '2.25.11', 'CT', 300, 'fp');
            INSERT INTO runs VALUES ('R1', '2026-10-22T00:00:00Z', '{}', 'auto', '[]', '{}', 0);
            """)
        let t0 = Date(timeIntervalSince1970: 1_800_000_000)
        #expect(try QCStore.current(runID: "R1", seriesKey: "s_000001", in: db) == .unreviewed)
        try QCStore.set(.accepted, comment: "  ", runID: "R1", seriesKey: "s_000001", reviewer: "JL", in: db, now: t0)
        try QCStore.exclude(true, model: "clin_ct_organs", labelID: 15, reason: "Spleen cut off",
                            runID: "R1", seriesKey: "s_000001", reviewer: "JL", in: db, now: t0 + 1)
        var qc = try QCStore.current(runID: "R1", seriesKey: "s_000001", in: db)
        #expect(qc.status == .accepted)
        #expect(qc.excluded == ["clin_ct_organs": [15: "Spleen cut off"]])
        // A comment turns an acceptance into one with comment.
        try QCStore.set(.accepted, comment: "Motion at the dome", runID: "R1", seriesKey: "s_000001",
                        reviewer: "JL", in: db, now: t0 + 2)
        try QCStore.exclude(false, model: "clin_ct_organs", labelID: 15, reason: nil,
                            runID: "R1", seriesKey: "s_000001", reviewer: "JL", in: db, now: t0 + 3)
        qc = try QCStore.current(runID: "R1", seriesKey: "s_000001", in: db)
        #expect(qc.status == .acceptedWithComment && qc.comment == "Motion at the dome")
        #expect(qc.excluded["clin_ct_organs"]?.isEmpty ?? true)
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM qc") == 4)
        let actions = try String.fetchAll(db, sql: "SELECT action FROM audit_log ORDER BY id")
        #expect(actions == ["qc_changed", "label_excluded", "qc_changed", "label_included"])
    }
}
