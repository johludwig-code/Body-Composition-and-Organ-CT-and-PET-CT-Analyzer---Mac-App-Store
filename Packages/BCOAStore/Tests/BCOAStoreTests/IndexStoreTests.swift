import CryptoKit
import Foundation
import GRDB
import Testing
@testable import BCOAStore

/// The worker's own catalog DDL, read from its source the way the Linux suite
/// reads IndexSQL from this package, so the merge here runs against the
/// tables the index job really writes.
private func catalogDDL() throws -> String {
    let source = try String(
        contentsOf: repositoryRoot.appendingPathComponent("Worker/bcoa_worker/index/catalog.py"),
        encoding: .utf8)
    let start = try #require(source.range(of: "CATALOG_DDL = \"\"\"\n"))
    let end = try #require(source.range(of: "\n\"\"\"", range: start.upperBound..<source.endIndex))
    return String(source[start.upperBound..<end.lowerBound])
}

private func reason(_ outcome: String, _ code: String) -> String {
    #"{"v": 1, "outcome": "\#(outcome)", "codes": ["\#(code)"], "params": {}}"#
}

/// A project folder in a temporary directory with a v2 database, a link key
/// and one source, and a catalog beside it written as the regroup writes one.
private struct Fixture {
    let folder: URL
    let project: DatabaseQueue
    let catalog: URL
    let keyID: String

    init() throws {
        folder = FileManager.default.temporaryDirectory
            .appendingPathComponent("bcoa-indexstore-\(UUID().uuidString)")
        try FileManager.default.createDirectory(
            at: folder.appendingPathComponent("index"), withIntermediateDirectories: true)
        project = try ProjectStore.open(projectFolder: folder)
        catalog = folder.appendingPathComponent(IndexStore.catalogPath)
        keyID = try project.write { db in
            try db.execute(sql: """
                INSERT INTO sources (id, bookmark, display_path, added_at, state)
                VALUES (1, x'00', 'Source 1', '2026-10-22T00:00:00Z', 'indexing')
                """)
            return try #require(try IndexStore.ensureLinkKey(db)).keyID
        }
    }

    func writeCatalog(generation: Int, catalogID: String = String(repeating: "ab", count: 16),
                      linkKeyID: String? = nil, complete: Bool = true) throws {
        let catalog = try DatabaseQueue(path: self.catalog.path)
        try catalog.write { db in
            if try !db.tableExists("catalog_meta") {
                try db.execute(sql: try catalogDDL())
            }
            for table in ["scans", "pending_identifiers", "cat_studies", "cat_series", "cat_checks",
                          "cat_pairs", "cat_id_candidates", "catalog_meta"] {
                try db.execute(sql: "DELETE FROM \(table)")
            }
            for (key, value) in [("format", "1"), ("catalog_id", catalogID),
                                 ("generation", String(generation)), ("complete", complete ? "1" : "0"),
                                 ("link_key_id", linkKeyID ?? keyID)] {
                try db.execute(sql: "INSERT INTO catalog_meta VALUES (?, ?)", arguments: [key, value])
            }
            try db.execute(sql: """
                INSERT INTO scans (source_id, state, started_at, finished_at, summary_json)
                VALUES (1, 'complete', '2026-10-22T00:00:00Z', '2026-10-22T00:01:00Z', '{"files": 9}')
                """)
            // Two studies of one patient; the first has a thin and a thick CT.
            for (uid, date) in [("2.25.1", "2024-01-02"), ("2.25.2", "2025-03-04")] {
                try db.execute(sql: """
                    INSERT INTO cat_studies (study_uid, pid_link, pid_state, sex, age_years, study_date, description)
                    VALUES (?, ?, 'present', 'F', 61.0, ?, 'CT Thorax')
                    """, arguments: [uid, "pid:" + String(repeating: "a1", count: 32), date])
                try db.execute(sql: """
                    INSERT INTO pending_identifiers VALUES (?, 'ID-0001', ?, 'dicom', ?)
                    """, arguments: [uid, "ACC-" + uid, generation])
            }
            let parts: [(Int, String, String, Double, Int?, Int, String)] = [
                (1, "2.25.11", "2.25.1", 1.0, 1, 1, reason("chosen", "select.chosen.only_candidate")),
                (2, "2.25.12", "2.25.1", 5.0, 2, 0, reason("eligible", "select.eligible.thicker")),
                (3, "2.25.21", "2.25.2", 3.0, 1, 1, reason("chosen", "select.chosen.only_candidate")),
            ]
            for (ref, uid, study, thickness, rank, selected, why) in parts {
                try db.execute(sql: """
                    INSERT INTO cat_series (part_ref, series_uid, part, study_uid, modality, description,
                        image_count, slice_thickness_mm, fingerprint, series_number, auto_rank,
                        auto_selected, reason_json)
                    VALUES (?, ?, 0, ?, 'CT', 'Body', 300, ?, ?, ?, ?, ?, ?)
                    """, arguments: [ref, uid, study, thickness, "fp" + uid, ref, rank, selected, why])
            }
        }
    }
}

private func sha256(_ url: URL) throws -> String {
    SHA256.hash(data: try Data(contentsOf: url)).map { String(format: "%02x", $0) }.joined()
}

@Test func aCompleteGenerationIsMergedOnceAndTheCatalogIsNotWritten() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1)
    let before = try sha256(fixture.catalog)

    let outcome = try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
    #expect(outcome == .merged(generation: 1))
    #expect(try sha256(fixture.catalog) == before)

    try fixture.project.read { (db: Database) throws in
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM patients") == 1)
        #expect(try String.fetchOne(db, sql: "SELECT pseudonym FROM patients") == "P0001")
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM studies") == 2)
        #expect(try String.fetchAll(db, sql: "SELECT series_uid FROM series WHERE is_primary = 1 ORDER BY 1")
            == ["2.25.11", "2.25.21"])
        #expect(try String.fetchOne(db, sql: "SELECT state FROM sources WHERE id = 1") == "indexed")
        #expect(try String.fetchOne(db, sql: "SELECT value FROM project_meta WHERE key = 'merged_generation'") == "1")
        // The plain ID reaches `identifiers` and nowhere else in the project.
        #expect(try String.fetchOne(db, sql: "SELECT patient_id FROM identifiers") == "ID-0001")
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM audit_log WHERE action = 'index_merged'") == 1)
    }
    #expect(try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog) == .nothingPending)
}

@Test func anIncompleteCatalogIsNotMerged() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1, complete: false)
    #expect(try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog) == .nothingPending)
    #expect(try fixture.project.read { try Int.fetchOne($0, sql: "SELECT count(*) FROM studies") } == 0)
}

@Test func aNewerGenerationKeepsKeysAndAUsersChoice() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1)
    try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
    let thick = try #require(try fixture.project.read {
        try String.fetchOne($0, sql: "SELECT series_key FROM series WHERE series_uid = '2.25.12'")
    })
    try fixture.project.write { db in
        try IndexStore.apply(.makePrimary(thick), in: db, actor: "test")
    }
    try fixture.writeCatalog(generation: 2)
    #expect(try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
        == .merged(generation: 2))
    let after = try fixture.project.read { (db: Database) throws in
        try Row.fetchOne(db, sql: """
            SELECT s.series_key, s.is_primary, st.selection_mode,
                   (SELECT count(*) FROM audit_log WHERE action = 'selection_changed') AS audited
            FROM series AS s JOIN studies AS st USING (study_key) WHERE s.series_uid = '2.25.12'
            """)
    }
    let row = try #require(after)
    #expect(row["series_key"] as String == thick)
    #expect(row["is_primary"] as Int == 1)
    #expect(row["selection_mode"] as String == "user")
    #expect(row["audited"] as Int == 1)
}

@Test func linksOfAnotherKeyAreWorthNothing() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1, linkKeyID: "0000000000000000")
    try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
    try fixture.project.read { (db: Database) throws in
        // Without valid links each study is a patient of its own.
        #expect(try String.fetchAll(db, sql: "SELECT DISTINCT id_status FROM patients") == ["unlinked"])
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM patients") == 2)
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM patient_links") == 0)
    }
}

@Test func anUnchangedScanStillSetsTheSourceState() throws {
    let fixture = try Fixture()
    try fixture.project.write { db in
        try IndexStore.applyUnchangedScan(
            [1: .init(state: "unreachable", files: 12, badDirs: 0, code: "source.empty_walk")], in: db)
    }
    try fixture.project.read { (db: Database) throws in
        #expect(try String.fetchOne(db, sql: "SELECT state FROM sources") == "unreachable")
        let summary = try #require(try String.fetchOne(db, sql: "SELECT summary_json FROM sources"))
        #expect(summary.contains("source.empty_walk"))
    }
}

@Test func theLinkKeyIsMadeOnceAndNeverAfterRemoveIdentifiers() throws {
    let db = try ProjectStore.inMemory()
    try db.write { (db: Database) throws in
        let first = try #require(try IndexStore.ensureLinkKey(db))
        #expect(try IndexStore.ensureLinkKey(db)?.keyID == first.keyID)
        #expect(try IndexStore.meta(db, "link_key_id") == first.keyID)
        try IndexStore.setMeta(db, "link_key", nil)
        try IndexStore.setMeta(db, "link_key_id", nil)
        try IndexStore.setMeta(db, "identifiers_removed_at", "2026-10-22T00:00:00Z")
        #expect(try IndexStore.ensureLinkKey(db) == nil)
    }
}

@Test func theTreeShowsWhatTheMergeWrote() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1)
    try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
    let tree = try fixture.project.read { try ProjectTree.load($0) }
    #expect(tree.patients.map(\.pseudonym) == ["P0001"])
    let studies = try #require(tree.patients.first?.studies)
    #expect(studies.map(\.date) == ["2024-01-02", "2025-03-04"])
    #expect(studies[0].series.map(\.sliceThicknessMM) == [1.0, 5.0])
    #expect(studies[0].series.map(\.primary) == [true, false])
    #expect(tree.seriesCount == 3)
    #expect(tree.selectedCount == 2)
    let sources = try fixture.project.read { try SourceRow.load($0) }
    #expect(sources.map(\.state) == ["indexed"])
    #expect(sources.first?.files == 9)
}

@Test func removeIdentifiersTakesIDsLinksKeyAndNames() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1)
    try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
    try IndexStore.removeIdentifiers(from: fixture.project, actor: "test")
    try fixture.project.read { (db: Database) throws in
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM identifiers") == 0)
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM patient_links") == 0)
        #expect(try IndexStore.linkKey(db) == nil)
        #expect(try IndexStore.identifiersRemoved(db))
        #expect(try String.fetchOne(db, sql: "SELECT display_path FROM sources") == "Source 1")
        #expect(try String.fetchOne(db, sql: "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1")
            == "remove_identifiers")
    }
    // No key is ever made again for this project.
    #expect(try fixture.project.write { try IndexStore.ensureLinkKey($0) } == nil)
}

@Test func folderIDsAreConfirmedAtTheChosenLevel() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1)
    // Both studies anonymous, in folders CASE_A and CASE_B below the source.
    let catalog = try DatabaseQueue(path: fixture.catalog.path)
    try catalog.write { db in
        try db.execute(sql: "UPDATE cat_studies SET pid_link = NULL, pid_state = 'placeholder'")
        try db.execute(sql: "DELETE FROM pending_identifiers")
        for (uid, folder) in [("2.25.1", "CASE_A"), ("2.25.2", "CASE_B")] {
            try db.execute(sql: "INSERT INTO cat_id_candidates VALUES (?, 1, 0, 'Source', ?)",
                           arguments: [uid, "folder:" + String(repeating: "50", count: 32)])
            try db.execute(sql: "INSERT INTO cat_id_candidates VALUES (?, 1, 1, ?, ?)",
                           arguments: [uid, folder, "folder:" + String(repeating: folder == "CASE_A" ? "a1" : "b2", count: 32)])
        }
    }
    try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
    let candidates = try IndexStore.folderCandidates(project: fixture.project, catalog: fixture.catalog)
    #expect(candidates.count == 2)
    #expect(candidates.map { $0.levels[1]?.label } == ["CASE_A", "CASE_B"])
    #expect(IndexStore.defaultFolderLevel(candidates) == 1)
    let before = try fixture.project.read { try Int.fetchOne($0, sql: "SELECT count(*) FROM series WHERE selected = 1") }
    #expect(before == 0)
    try fixture.project.write { try IndexStore.confirmFolderIDs(level: 1, candidates: candidates, in: $0, actor: "test") }
    try fixture.project.read { (db: Database) throws in
        #expect(try String.fetchAll(db, sql: "SELECT DISTINCT id_status FROM patients") == ["confirmed"])
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM patients") == 2)
        // Held until now, the automatic choice applies once the IDs are confirmed.
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM series WHERE selected = 1") == 2)
        #expect(try String.fetchAll(db, sql: "SELECT patient_id FROM identifiers ORDER BY 1") == ["CASE_A", "CASE_B"])
    }
    #expect(try IndexStore.folderCandidates(project: fixture.project, catalog: fixture.catalog).isEmpty)
}

@Test func aStudyAssignedToAnotherPatientMovesAndTheEmptyPatientGoes() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1, linkKeyID: "0000000000000000")
    try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
    // Without valid links each study became a patient of its own.
    let patients = try fixture.project.read { try String.fetchAll($0, sql: "SELECT patient_key FROM patients ORDER BY 1") }
    #expect(patients.count == 2)
    let second = try #require(try fixture.project.read {
        try String.fetchOne($0, sql: "SELECT study_key FROM studies WHERE patient_key = ?", arguments: [patients[1]])
    })
    try fixture.project.write { try IndexStore.assign(studies: [second], to: patients[0], in: $0, actor: "test") }
    try fixture.project.read { (db: Database) throws in
        #expect(try Int.fetchOne(db, sql: "SELECT count(*) FROM patients") == 1)
        #expect(try Int.fetchOne(db, sql: "SELECT count(DISTINCT patient_key) FROM studies") == 1)
        #expect(try String.fetchOne(db, sql: "SELECT action FROM audit_log ORDER BY id DESC LIMIT 1") == "studies_assigned")
    }
}

@Test func aTypedIDJoinsThePatientItsLinkNamesOrMakesOne() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1, linkKeyID: "0000000000000000")
    try IndexStore.mergeIfPending(project: fixture.project, catalog: fixture.catalog)
    let studies = try fixture.project.read { try String.fetchAll($0, sql: "SELECT study_key FROM studies ORDER BY 1") }
    try fixture.project.write { db in
        try IndexStore.setTypedPatientID("  CASE-17 ", studies: [studies[0]], in: db, actor: "test")
        // The same ID for the second study: it joins the patient just made.
        try IndexStore.setTypedPatientID("CASE-17", studies: [studies[1]], in: db, actor: "test")
        #expect(throws: IndexStore.TypedIDError.notAnID) {
            try IndexStore.setTypedPatientID("anonymous", studies: [studies[0]], in: db, actor: "test")
        }
    }
    try fixture.project.read { (db: Database) throws in
        #expect(try Int.fetchOne(db, sql: "SELECT count(DISTINCT patient_key) FROM studies") == 1)
        #expect(try String.fetchOne(db, sql: """
            SELECT p.id_status FROM patients p JOIN studies s USING (patient_key) LIMIT 1
            """) == "confirmed")
        #expect(try String.fetchAll(db, sql: "SELECT patient_id FROM identifiers") == ["CASE-17"])
    }
}

@Test func unusedFilesComeFromTheCatalogOnly() throws {
    let fixture = try Fixture()
    try fixture.writeCatalog(generation: 1)
    let catalog = try DatabaseQueue(path: fixture.catalog.path)
    try catalog.write { db in
        try db.execute(sql: """
            INSERT INTO files (source_id, rel_path, size, mtime_ns, reader_version, kind, code)
            VALUES (1, CAST('junk/broken.dcm' AS BLOB), 10, 0, 1, 'unreadable', 'read.invalid_dicom'),
                   (1, CAST('scans.zip' AS BLOB), 10, 0, 1, 'archive', NULL)
            """)
        try db.execute(sql: "INSERT INTO bad_dirs VALUES (1, CAST('locked' AS BLOB), 'permission_denied')")
    }
    let entries = IndexStore.unusedEntries(catalog: fixture.catalog)
    #expect(entries.map(\.path) == ["junk/broken.dcm", "scans.zip", "locked"])
    #expect(entries.map(\.kind) == ["unreadable", "archive", "folder"])
}
