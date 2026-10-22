import Foundation
import GRDB

/// What `catalog_meta` says about the index catalog, read in one short
/// read-only connection (ADR 0020, ADR 0021).
public struct CatalogStatus: Sendable, Equatable {
    public var format: String
    public var catalogID: String
    public var generation: Int
    public var complete: Bool
    public var linkKeyID: String?
}

/// The project's side of the import (ADR 0020, ADR 0021, ADR 0023, ADR 0024):
/// the link key, the merge of a catalog generation, the source states of a
/// scan that changed nothing, and the selection edits. The SQL itself is in
/// `IndexSQL`, `SelectionSQL` and `IdentitySQL`, where the Linux suite runs
/// it; this type only fills the temp tables and runs the statements in the
/// transactions the ADRs ask for.
public enum IndexStore {
    public static let catalogPath = "index/catalog.sqlite"
    public static let previewsPath = "index/previews"
    /// The catalog format the merge understands (`CATALOG_FORMAT` in the
    /// worker); another is rebuilt by the next scan, never merged.
    public static let catalogFormat = "1"

    // MARK: Project meta

    public static func meta(_ db: Database, _ key: String) throws -> String? {
        try String.fetchOne(db, sql: "SELECT value FROM project_meta WHERE key = ?", arguments: [key])
    }

    public static func setMeta(_ db: Database, _ key: String, _ value: String?) throws {
        if let value {
            try db.execute(
                sql: "INSERT OR REPLACE INTO project_meta (key, value) VALUES (?, ?)",
                arguments: [key, value])
        } else {
            try db.execute(sql: "DELETE FROM project_meta WHERE key = ?", arguments: [key])
        }
    }

    /// True once Remove Identifiers ran; the project then never gets a key
    /// again, and new studies are refused (ADR 0024, open question 26).
    public static func identifiersRemoved(_ db: Database) throws -> Bool {
        try meta(db, "identifiers_removed_at") != nil
    }

    /// The project's link key, made on first use: ADR 0024 makes it when a
    /// project is created or migrated to v2, which the v2 SQL cannot do
    /// because it needs random bytes. Nil after Remove Identifiers.
    @discardableResult
    public static func ensureLinkKey(_ db: Database) throws -> LinkHasher? {
        if let stored = try meta(db, "link_key") {
            return LinkHasher(encoded: stored)
        }
        guard try !identifiersRemoved(db) else { return nil }
        let hasher = LinkHasher.makeKey()
        try setMeta(db, "link_key", hasher.encoded)
        try setMeta(db, "link_key_id", hasher.keyID)
        return hasher
    }

    public static func linkKey(_ db: Database) throws -> LinkHasher? {
        try meta(db, "link_key").flatMap(LinkHasher.init(encoded:))
    }

    // MARK: The catalog

    /// Nil when there is no catalog, it cannot be opened, or it has no meta
    /// yet; none of these is an error at launch.
    public static func status(ofCatalogAt url: URL) -> CatalogStatus? {
        guard FileManager.default.fileExists(atPath: url.path) else { return nil }
        var configuration = Configuration()
        configuration.readonly = true
        configuration.label = "catalog"
        guard let catalog = try? DatabaseQueue(path: url.path, configuration: configuration) else {
            return nil
        }
        return try? catalog.read { db -> CatalogStatus? in
            let rows = try Row.fetchAll(db, sql: "SELECT key, value FROM catalog_meta")
            var meta: [String: String] = [:]
            for row in rows { meta[row["key"]] = row["value"] }
            guard let id = meta["catalog_id"], let format = meta["format"] else { return nil }
            return CatalogStatus(
                format: format, catalogID: id, generation: Int(meta["generation"] ?? "") ?? 0,
                complete: meta["complete"] == "1", linkKeyID: meta["link_key_id"])
        }
    }

    /// The guard of the merge, asked before attaching: a complete catalog of
    /// this format whose generation is ahead or whose ID is another.
    public static func isPending(_ status: CatalogStatus, in db: Database) throws -> Bool {
        guard status.complete, status.format == catalogFormat else { return false }
        let mergedID = try meta(db, "catalog_id") ?? ""
        let merged = Int(try meta(db, "merged_generation") ?? "0") ?? 0
        return status.catalogID != mergedID || status.generation > merged
    }

    public enum MergeOutcome: Sendable, Equatable {
        case merged(generation: Int)
        case nothingPending
    }

    /// Merges the catalog's generation if it is pending (ADR 0021 decision
    /// 11: every index job and every export tries, and a repeated attempt is
    /// harmless). Throws when the merge itself fails; nothing is changed
    /// then, and the next attempt tries again.
    @discardableResult
    public static func mergeIfPending(
        project: DatabaseQueue, catalog url: URL, now: Date = Date(),
        newStudyPolicy: String = "refuse"
    ) throws -> MergeOutcome {
        guard let status = status(ofCatalogAt: url),
              try project.read({ try isPending(status, in: $0) })
        else { return .nothingPending }
        try project.writeWithoutTransaction { db in
            try attach(url, to: db)
            defer { try? db.execute(sql: "DETACH DATABASE idx") }
            let removed = try identifiersRemoved(db)
            try db.execute(sql: IndexSQL.mergeInputs)
            try db.execute(
                sql: """
                    INSERT INTO temp.merge_params (now, keep_identifiers, new_study_policy, link_key_id)
                    VALUES (?, ?, ?, ?)
                    """,
                arguments: [iso(now), removed ? 0 : 1, newStudyPolicy, try meta(db, "link_key_id")])
            // BEGIN IMMEDIATE: the merge must not wait halfway for a writer it
            // could have waited for before it started.
            try db.inTransaction(.immediate) {
                try db.execute(sql: IndexSQL.merge)
                try db.execute(sql: IndexSQL.patientAges)
                return .commit
            }
        }
        return .merged(generation: status.generation)
    }

    /// ADR 0021 decision 6: read-only where this SQLite takes URI file names,
    /// the plain path otherwise. The merge writes nothing to `idx` either way.
    static func attach(_ url: URL, to db: Database) throws {
        let options = try String.fetchSet(db, sql: "PRAGMA compile_options")
        if options.contains("USE_URI") {
            var components = URLComponents()
            components.scheme = "file"
            components.path = url.path
            components.queryItems = [URLQueryItem(name: "mode", value: "ro")]
            let uri = components.url?.absoluteString ?? url.path
            try db.execute(sql: "ATTACH DATABASE ? AS idx", arguments: [uri])
        } else {
            try db.execute(sql: "ATTACH DATABASE ? AS idx", arguments: [url.path])
        }
    }

    // MARK: Scans that changed nothing

    /// One source's entry in an index result (`index_result.schema.json`).
    public struct SourceScan: Sendable, Equatable {
        public var state: String
        public var files: Int
        public var badDirs: Int
        public var code: String?

        public init(state: String, files: Int, badDirs: Int, code: String? = nil) {
            self.state = state
            self.files = files
            self.badDirs = badDirs
            self.code = code
        }
    }

    /// A scan with `changed: false` wrote no generation, so no merge brings
    /// its sources' state in; the result does, mapped as the merge maps the
    /// catalog's scans (ADR 0020 decision 8).
    public static func applyUnchangedScan(_ sources: [Int: SourceScan], in db: Database, now: Date = Date())
        throws
    {
        for (id, scan) in sources {
            let state = switch scan.state {
            case "complete": "indexed"
            case "unreachable": "unreachable"
            default: "interrupted"
            }
            var summary: [String: Any] = ["state": scan.state, "files": scan.files, "bad_dirs": scan.badDirs]
            if let code = scan.code { summary["code"] = code }
            let json = String(
                decoding: try JSONSerialization.data(withJSONObject: summary, options: [.sortedKeys]),
                as: UTF8.self)
            try db.execute(
                sql: """
                    UPDATE sources SET state = ?, indexed_at = ?, summary_json = ?
                    WHERE id = ? AND state <> 'removed'
                    """,
                arguments: [state, iso(now), json, id])
        }
    }

    /// At launch (ADR 0021 decision 8): a source left `indexing` by a crash
    /// or a force quit is `interrupted`, and is scanned again.
    public static func markInterruptedSources(in db: Database) throws {
        try db.execute(sql: "UPDATE sources SET state = 'interrupted' WHERE state = 'indexing'")
    }

    // MARK: Selection edits

    public enum SelectionEdit: Sendable, Equatable {
        case select([String])
        case deselect([String])
        case makePrimary(String)
        /// "Apply Auto-Selection to All", or to the studies given.
        case applyAuto(studies: [String]?)
        /// "Select All CT ≤ n mm" over the studies given, or all.
        case bulkThinCT(maxMM: Double, studies: [String]?)
        case saveCohort(label: String)
        case applyCohort(id: Int64)

        var name: String {
            switch self {
            case .select: "select"
            case .deselect: "deselect"
            case .makePrimary: "make_primary"
            case .applyAuto: "apply_auto"
            case .bulkThinCT: "bulk_thin_ct"
            case .saveCohort: "save_cohort"
            case .applyCohort: "apply_cohort"
            }
        }
    }

    /// One edit in one transaction: the statement, `repairPrimary`,
    /// `patientAges`, and the audit row `selection_changed` with counts only
    /// (ADR 0023).
    public static func apply(_ edit: SelectionEdit, in db: Database, actor: String, now: Date = Date())
        throws
    {
        try db.execute(sql: SelectionSQL.inputs)
        var targets: [String] = []
        var scope: [String]?
        var maxMM: Double?
        var label: String?
        var cohort: Int64?
        let statement: String
        switch edit {
        case .select(let keys):
            targets = keys
            statement = SelectionSQL.select
        case .deselect(let keys):
            targets = keys
            statement = SelectionSQL.deselect
        case .makePrimary(let key):
            targets = [key]
            statement = SelectionSQL.makePrimary
        case .applyAuto(let studies):
            scope = studies
            statement = SelectionSQL.applyAuto
        case .bulkThinCT(let mm, let studies):
            scope = studies
            maxMM = mm
            statement = SelectionSQL.bulkThinCT
        case .saveCohort(let name):
            label = name
            statement = SelectionSQL.saveCohort
        case .applyCohort(let id):
            cohort = id
            statement = SelectionSQL.applyCohort
        }
        for key in targets {
            try db.execute(sql: "INSERT OR IGNORE INTO temp.sel_target VALUES (?)", arguments: [key])
        }
        // repairPrimary works on sel_scope: the studies the targets live in,
        // or for whole-project edits every current study.
        if let scope {
            for key in scope {
                try db.execute(sql: "INSERT OR IGNORE INTO temp.sel_scope VALUES (?)", arguments: [key])
            }
        } else if targets.isEmpty {
            try db.execute(sql: """
                INSERT OR IGNORE INTO temp.sel_scope SELECT study_key FROM main.studies
                WHERE index_state = 'current'
                """)
        } else {
            try db.execute(sql: """
                INSERT OR IGNORE INTO temp.sel_scope
                SELECT DISTINCT study_key FROM main.series WHERE series_key IN (SELECT series_key FROM temp.sel_target)
                """)
        }
        try db.execute(
            sql: "INSERT INTO temp.sel_param (max_mm, label, now, cohort_id) VALUES (?, ?, ?, ?)",
            arguments: [maxMM, label, iso(now), cohort])
        let before = try Int.fetchOne(db, sql: "SELECT count(*) FROM series WHERE selected = 1") ?? 0
        try db.execute(sql: statement)
        try db.execute(sql: SelectionSQL.repairPrimary)
        try db.execute(sql: IndexSQL.patientAges)
        let after = try Int.fetchOne(db, sql: "SELECT count(*) FROM series WHERE selected = 1") ?? 0
        let studies = try Int.fetchOne(db, sql: "SELECT count(*) FROM temp.sel_scope") ?? 0
        let details: [String: Any] = [
            "edit": edit.name, "studies": studies, "selected_before": before, "selected_after": after,
        ]
        try audit(db, actor: actor, action: "selection_changed", object: "series", details: details, now: now)
    }

    // MARK: Helpers

    public static func audit(
        _ db: Database, actor: String, action: String, object: String?, details: [String: Any],
        now: Date = Date()
    ) throws {
        let json = String(
            decoding: try JSONSerialization.data(withJSONObject: details, options: [.sortedKeys]),
            as: UTF8.self)
        try db.execute(
            sql: "INSERT INTO audit_log (at, actor, action, object, details_json) VALUES (?, ?, ?, ?, ?)",
            arguments: [iso(now), actor, action, object, json])
    }

    static func iso(_ date: Date) -> String {
        ISO8601DateFormatter().string(from: date)
    }
}
