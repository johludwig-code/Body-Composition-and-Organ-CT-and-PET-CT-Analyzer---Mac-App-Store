import GRDB
import Testing
@testable import BCOAStore

@Test func v1CreatesEveryTableOfThePlan() throws {
    let db = try ProjectStore.inMemory()
    let tables = try db.read { db in
        try String.fetchSet(db, sql: "SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    #expect(tables.isSuperset(of: [
        "sources", "patients", "identifiers", "studies", "series",
        "runs", "jobs", "results", "qc", "audit_log",
    ]))
}

@Test func noColumnCanHoldANameOrBirthDate() throws {
    let db = try ProjectStore.inMemory()
    let columns = try db.read { db in
        try String.fetchAll(db, sql: """
            SELECT p.name FROM sqlite_master m, pragma_table_info(m.name) p WHERE m.type = 'table'
            """)
    }
    for column in columns {
        #expect(!column.contains("name") || column == "label_name" || column == "display_path")
        #expect(!column.contains("birth"))
    }
}

@Test func removeIdentifiersEmptiesTheTableAndAuditsIt() throws {
    let db = try ProjectStore.inMemory()
    try db.write { db in
        try db.execute(sql: "INSERT INTO patients (patient_key, pseudonym) VALUES ('k1', 'P0001')")
        try db.execute(sql: "INSERT INTO identifiers (patient_key, patient_id) VALUES ('k1', '0042')")
        try ProjectStore.removeIdentifiers(db, actor: "JL")
    }
    let (left, audited) = try db.read { db in
        (try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM identifiers"),
         try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM audit_log WHERE action = 'remove_identifiers'"))
    }
    #expect(left == 0)
    #expect(audited == 1)
}

@Test func v2KeepsOnePrimaryPerStudyAndOnlyASelectedOne() throws {
    // The export takes the first primary of a study and assumes it is
    // selected; the schema, not each statement, makes sure of both.
    let db = try ProjectStore.inMemory()
    try db.write { db in
        try db.execute(sql: "INSERT INTO patients (patient_key, pseudonym) VALUES ('pt_000001', 'P0001')")
        try db.execute(sql: """
            INSERT INTO studies (study_key, patient_key, study_uid) VALUES ('st_000001', 'pt_000001', '2.25.1')
            """)
        try db.execute(sql: """
            INSERT INTO series (series_key, study_key, series_uid, modality, image_count, fingerprint,
                selected, is_primary)
            VALUES ('s_000001', 'st_000001', '2.25.11', 'CT', 300, 'a', 1, 1),
                   ('s_000002', 'st_000001', '2.25.12', 'CT', 300, 'b', 1, 0)
            """)
    }
    #expect(throws: DatabaseError.self) {
        try db.write { db in
            try db.execute(sql: "UPDATE series SET is_primary = 1 WHERE series_key = 's_000002'")
        }
    }
    #expect(throws: DatabaseError.self) {
        try db.write { db in
            try db.execute(sql: "UPDATE series SET selected = 0 WHERE series_key = 's_000001'")
        }
    }
    let counters = try db.read { db in
        try Int.fetchAll(db, sql: "SELECT last FROM key_counters")
    }
    #expect(counters == [0, 0, 0, 0])
}
