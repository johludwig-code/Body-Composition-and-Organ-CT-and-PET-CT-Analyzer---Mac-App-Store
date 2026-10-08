import GRDB
import Testing
@testable import BCOAStore

/// The Linux suite runs every statement of IndexSQL, SelectionSQL and
/// IdentitySQL on Python's SQLite, against real data. This runs the ones that
/// need no attached catalog, on empty inputs, on the SQLite the app links, so
/// that a function or syntax that SQLite lacks fails here and not in a user's
/// project.
@Test func selectionAndIdentityStatementsRunOnTheSystemSQLite() throws {
    let db = try ProjectStore.inMemory()
    try db.write { db in
        try db.execute(sql: IndexSQL.mergeInputs)
        try db.execute(sql: SelectionSQL.inputs)
        try db.execute(sql: IdentitySQL.inputs)
        for sql in [
            SelectionSQL.makePrimary, SelectionSQL.select, SelectionSQL.deselect,
            SelectionSQL.bulkThinCT, SelectionSQL.applyAuto, SelectionSQL.saveCohort,
            SelectionSQL.applyCohort, IdentitySQL.confirmFolderLevel,
            IdentitySQL.createTypedPatient, IdentitySQL.assignStudy,
            SelectionSQL.repairPrimary, IndexSQL.patientAges,
        ] {
            try db.execute(sql: sql)
        }
    }
    let audited = try db.read { db in
        try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM audit_log")
    }
    #expect(audited == 0)
}

@Test func statementsCanBeRunTwiceOnOneConnection() throws {
    // IndexStore keeps its connection: a statement whose temp tables
    // outlived it would fail the second time.
    let db = try ProjectStore.inMemory()
    try db.write { db in
        for _ in 0..<2 {
            try db.execute(sql: IndexSQL.mergeInputs)
            try db.execute(sql: SelectionSQL.inputs)
            try db.execute(sql: IdentitySQL.inputs)
            try db.execute(sql: SelectionSQL.bulkThinCT)
            try db.execute(sql: IdentitySQL.confirmFolderLevel)
            try db.execute(sql: IdentitySQL.createTypedPatient)
            try db.execute(sql: IdentitySQL.assignStudy)
        }
    }
}
