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
