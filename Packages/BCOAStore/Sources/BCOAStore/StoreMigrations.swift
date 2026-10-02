import Foundation
import GRDB

/// The project database schema (plan §6, documented in docs/schema.md).
///
/// Migrations are append-only: a migration that has shipped is never edited,
/// because a project folder created with it must still open. Raw SQL rather
/// than the table-builder API so that the schema reads the same here, in
/// docs/schema.md and in `sqlite3 project.sqlite .schema`.
public enum StoreMigrations {
    public static var migrator: DatabaseMigrator {
        var migrator = DatabaseMigrator()
        migrator.registerMigration("v1") { db in
            try db.execute(sql: v1)
        }
        return migrator
    }

    /// Patient names and birth dates have no column anywhere. Plain-text
    /// identifiers live only in `identifiers`, which "Remove Identifiers"
    /// empties for good (plan §13).
    static let v1 = """
        CREATE TABLE sources (
            id INTEGER PRIMARY KEY,
            bookmark BLOB NOT NULL,
            display_path TEXT NOT NULL,
            added_at TEXT NOT NULL
        );

        CREATE TABLE patients (
            patient_key TEXT PRIMARY KEY,
            pseudonym TEXT NOT NULL UNIQUE,
            sex TEXT CHECK (sex IN ('F', 'M', 'O') OR sex IS NULL),
            age_at_first_study REAL
        );

        CREATE TABLE identifiers (
            patient_key TEXT PRIMARY KEY REFERENCES patients(patient_key) ON DELETE CASCADE,
            patient_id TEXT,
            accession_numbers TEXT
        );

        CREATE TABLE studies (
            study_key TEXT PRIMARY KEY,
            patient_key TEXT NOT NULL REFERENCES patients(patient_key) ON DELETE CASCADE,
            study_uid TEXT NOT NULL UNIQUE,
            study_date TEXT,
            description TEXT
        );

        CREATE TABLE series (
            series_key TEXT PRIMARY KEY,
            study_key TEXT NOT NULL REFERENCES studies(study_key) ON DELETE CASCADE,
            series_uid TEXT NOT NULL,
            part INTEGER NOT NULL DEFAULT 0,
            modality TEXT NOT NULL,
            description TEXT,
            image_count INTEGER NOT NULL,
            slice_thickness_mm REAL,
            pixel_spacing_mm REAL,
            kernel TEXT,
            manufacturer TEXT,
            kvp REAL,
            contrast_agent TEXT,
            image_type TEXT,
            frame_of_reference_uid TEXT,
            fingerprint TEXT NOT NULL,
            selected INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK (is_primary IN (0, 1)),
            selection_reason TEXT,
            UNIQUE (series_uid, part)
        );
        CREATE INDEX series_by_study ON series(study_key);

        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            versions_json TEXT NOT NULL,
            device TEXT NOT NULL,
            models_json TEXT NOT NULL,
            settings_json TEXT NOT NULL,
            locked INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1))
        );

        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            run_id TEXT REFERENCES runs(run_id),
            series_key TEXT REFERENCES series(series_key),
            kind TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN
                ('queued', 'running', 'done', 'failed', 'cancelled', 'interrupted')),
            queued_at TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            device TEXT,
            error_code TEXT,
            log_path TEXT
        );
        CREATE INDEX jobs_by_status ON jobs(status);

        CREATE TABLE results (
            run_id TEXT NOT NULL REFERENCES runs(run_id),
            series_key TEXT NOT NULL REFERENCES series(series_key) ON DELETE CASCADE,
            model TEXT NOT NULL,
            label_id INTEGER NOT NULL,
            label_name TEXT NOT NULL,
            voxel_count INTEGER NOT NULL,
            volume_ml REAL,
            hu_mean REAL,
            hu_sd REAL,
            hu_median REAL,
            hu_p05 REAL,
            hu_p95 REAL,
            hu_min REAL,
            hu_max REAL,
            touches_border INTEGER NOT NULL CHECK (touches_border IN (0, 1)),
            device TEXT NOT NULL,
            flags TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (run_id, series_key, model, label_id)
        );

        CREATE TABLE qc (
            id INTEGER PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES runs(run_id),
            series_key TEXT NOT NULL REFERENCES series(series_key) ON DELETE CASCADE,
            model TEXT,
            label_id INTEGER,
            status TEXT NOT NULL CHECK (status IN
                ('unreviewed', 'accepted', 'accepted_with_comment', 'rejected', 'label_excluded')),
            reviewer TEXT,
            at TEXT NOT NULL,
            comment TEXT
        );
        CREATE INDEX qc_by_series ON qc(run_id, series_key);

        CREATE TABLE audit_log (
            id INTEGER PRIMARY KEY,
            at TEXT NOT NULL,
            actor TEXT NOT NULL,
            action TEXT NOT NULL,
            object TEXT,
            details_json TEXT
        );
        """
}

/// Opens or creates `project.sqlite` inside a project folder.
public enum ProjectStore {
    public static let fileName = "project.sqlite"

    public static func open(projectFolder: URL) throws -> DatabaseQueue {
        var configuration = Configuration()
        configuration.foreignKeysEnabled = true
        configuration.label = "project"
        let queue = try DatabaseQueue(
            path: projectFolder.appendingPathComponent(fileName).path, configuration: configuration)
        try StoreMigrations.migrator.migrate(queue)
        return queue
    }

    public static func inMemory() throws -> DatabaseQueue {
        var configuration = Configuration()
        configuration.foreignKeysEnabled = true
        let queue = try DatabaseQueue(configuration: configuration)
        try StoreMigrations.migrator.migrate(queue)
        return queue
    }

    /// "Remove Identifiers" (plan §13): irreversible, and audited.
    public static func removeIdentifiers(_ db: Database, actor: String, at: Date = Date()) throws {
        let count = try Int.fetchOne(db, sql: "SELECT COUNT(*) FROM identifiers") ?? 0
        // Set before the delete: the rows must not survive in free pages of
        // the file either.
        try db.execute(sql: "PRAGMA secure_delete = ON")
        try db.execute(sql: "DELETE FROM identifiers")
        try db.execute(
            sql: "INSERT INTO audit_log (at, actor, action, object, details_json) VALUES (?, ?, ?, ?, ?)",
            arguments: [ISO8601DateFormatter().string(from: at), actor, "remove_identifiers",
                        "identifiers", #"{"rows":\#(count)}"#])
    }
}
