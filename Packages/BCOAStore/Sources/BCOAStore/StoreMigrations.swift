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
        migrator.registerMigration("v2") { db in
            try db.execute(sql: v2)
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

    /// The import (M2; ADR 0020, ADR 0023, ADR 0024). Additive: nothing is
    /// dropped or renamed, so the export reads a v1 project and its v2
    /// migration alike. The import's SQL (`IndexSQL`) needs SQLite 3.33 for
    /// `UPDATE … FROM` and 3.38 for the built-in JSON functions.
    ///
    /// Still no column for a name or a birth date, and none for the path of
    /// a source file: patients are linked by HMAC (`patient_links`), and the
    /// relative paths and folder names below a source stay in the index
    /// catalog, which Remove Identifiers deletes. The paths v1 keeps are
    /// unchanged: a source's bookmark encodes the folder's absolute path and
    /// `display_path` holds its name (ADR 0020, ADR 0024), and
    /// `jobs.log_path` holds the path of a job's log inside the project
    /// folder.
    static let v2 = """
        -- Rows that no shipped version could write, but that would make the unique
        -- index below fail on a hand-made v1 file. Each study keeps the primary the
        -- export already picks: the export sees only series that are selected or
        -- have results, and takes the lowest series_key among the primaries it sees
        -- (exactly so in a project with one run). The one kept is then selected,
        -- because a primary always is.
        UPDATE series SET is_primary = 0
        WHERE is_primary = 1
          AND series_key NOT IN (
              SELECT series_key FROM (
                  SELECT series_key, row_number() OVER (PARTITION BY study_key
                      ORDER BY (selected = 1 OR series_key IN (SELECT series_key FROM results)) DESC,
                               series_key) AS n
                  FROM series WHERE is_primary = 1)
              WHERE n = 1);
        UPDATE series SET selected = 1 WHERE is_primary = 1 AND selected = 0;

        CREATE TABLE cohorts (
            cohort_id INTEGER PRIMARY KEY,
            label TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        CREATE TABLE project_meta (
            key TEXT NOT NULL PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT INTO project_meta (key, value) VALUES ('merged_generation', '0'), ('catalog_id', '');

        CREATE TABLE key_counters (
            kind TEXT NOT NULL PRIMARY KEY CHECK (kind IN ('patient', 'study', 'series', 'pseudonym')),
            last INTEGER NOT NULL
        );
        INSERT INTO key_counters (kind, last) VALUES
            ('patient', (SELECT coalesce(max(CAST(substr(patient_key, 4) AS INTEGER)), 0)
                         FROM patients WHERE patient_key GLOB 'pt_[0-9]*')),
            ('study', (SELECT coalesce(max(CAST(substr(study_key, 4) AS INTEGER)), 0)
                       FROM studies WHERE study_key GLOB 'st_[0-9]*')),
            ('series', (SELECT coalesce(max(CAST(substr(series_key, 3) AS INTEGER)), 0)
                        FROM series WHERE series_key GLOB 's_[0-9]*')),
            ('pseudonym', (SELECT coalesce(max(CAST(substr(pseudonym, 2) AS INTEGER)), 0)
                           FROM patients WHERE pseudonym GLOB 'P[0-9]*'));

        ALTER TABLE sources ADD COLUMN state TEXT NOT NULL DEFAULT 'new'
            CHECK (state IN ('new', 'indexing', 'indexed', 'interrupted', 'unreachable', 'removed'));
        ALTER TABLE sources ADD COLUMN volume_kind TEXT NOT NULL DEFAULT 'local'
            CHECK (volume_kind IN ('local', 'network', 'removable'));
        ALTER TABLE sources ADD COLUMN indexed_at TEXT;
        ALTER TABLE sources ADD COLUMN summary_json TEXT NOT NULL DEFAULT '{}';

        ALTER TABLE patients ADD COLUMN id_status TEXT NOT NULL DEFAULT 'dicom'
            CHECK (id_status IN ('dicom', 'file', 'unconfirmed', 'confirmed', 'unlinked'));

        ALTER TABLE identifiers ADD COLUMN id_source TEXT
            CHECK (id_source IN ('dicom', 'file', 'folder', 'typed') OR id_source IS NULL);

        ALTER TABLE studies ADD COLUMN age_years REAL;
        ALTER TABLE studies ADD COLUMN index_state TEXT NOT NULL DEFAULT 'current'
            CHECK (index_state IN ('current', 'gone'));
        ALTER TABLE studies ADD COLUMN selection_mode TEXT NOT NULL DEFAULT 'auto'
            CHECK (selection_mode IN ('auto', 'user'));
        CREATE INDEX studies_by_patient ON studies(patient_key);

        ALTER TABLE series ADD COLUMN index_state TEXT NOT NULL DEFAULT 'current'
            CHECK (index_state IN ('current', 'gone'));
        ALTER TABLE series ADD COLUMN catalog_part TEXT;
        ALTER TABLE series ADD COLUMN series_number INTEGER;
        ALTER TABLE series ADD COLUMN sop_class_uid TEXT;
        ALTER TABLE series ADD COLUMN scanner_model TEXT;
        ALTER TABLE series ADD COLUMN slice_count INTEGER;
        ALTER TABLE series ADD COLUMN slice_spacing_mm REAL;
        ALTER TABLE series ADD COLUMN z_extent_mm REAL;
        ALTER TABLE series ADD COLUMN orientation TEXT
            CHECK (orientation IN ('axial', 'coronal', 'sagittal', 'oblique') OR orientation IS NULL);
        ALTER TABLE series ADD COLUMN image_rows INTEGER;
        ALTER TABLE series ADD COLUMN image_columns INTEGER;
        ALTER TABLE series ADD COLUMN transfer_syntax_uid TEXT;
        ALTER TABLE series ADD COLUMN kernel_class TEXT
            CHECK (kernel_class IN ('soft', 'sharp', 'unknown') OR kernel_class IS NULL);
        ALTER TABLE series ADD COLUMN auto_rank INTEGER;
        ALTER TABLE series ADD COLUMN auto_selected INTEGER NOT NULL DEFAULT 0
            CHECK (auto_selected IN (0, 1));
        ALTER TABLE series ADD COLUMN selection_origin TEXT NOT NULL DEFAULT 'auto'
            CHECK (selection_origin IN ('auto', 'user', 'bulk_thin_ct', 'cohort'));
        ALTER TABLE series ADD COLUMN selection_cohort_id INTEGER
            REFERENCES cohorts(cohort_id) ON DELETE SET NULL;
        CREATE INDEX series_by_uid ON series(series_uid);
        CREATE UNIQUE INDEX series_by_catalog_part ON series(catalog_part) WHERE catalog_part IS NOT NULL;

        -- At most one primary per study, and a primary is always selected: the export
        -- takes the first primary of a study and assumes it is one of the selected.
        CREATE UNIQUE INDEX series_one_primary_per_study ON series(study_key) WHERE is_primary = 1;
        CREATE TRIGGER series_primary_is_selected_on_insert BEFORE INSERT ON series
            WHEN NEW.is_primary = 1 AND NEW.selected = 0
            BEGIN SELECT RAISE(ABORT, 'a primary series must be selected'); END;
        CREATE TRIGGER series_primary_is_selected_on_update BEFORE UPDATE OF is_primary, selected ON series
            WHEN NEW.is_primary = 1 AND NEW.selected = 0
            BEGIN SELECT RAISE(ABORT, 'a primary series must be selected'); END;

        -- NOT NULL spelled out: SQLite accepts NULL in a primary key that is not an
        -- INTEGER one, and a single NULL link would make every
        -- `pid_link NOT IN (SELECT link FROM main.patient_links)` of the merge NULL.
        CREATE TABLE patient_links (
            link TEXT NOT NULL PRIMARY KEY,
            patient_key TEXT NOT NULL REFERENCES patients(patient_key) ON DELETE CASCADE
        );
        CREATE INDEX patient_links_by_patient ON patient_links(patient_key);

        CREATE TABLE index_checks (
            object_kind TEXT NOT NULL CHECK (object_kind IN ('project', 'source', 'patient', 'study', 'series')),
            object_key TEXT NOT NULL,
            code TEXT NOT NULL,
            level TEXT NOT NULL CHECK (level IN ('info', 'warning')),
            params_json TEXT NOT NULL DEFAULT '{}',
            PRIMARY KEY (object_kind, object_key, code)
        );

        CREATE TABLE series_pairs (
            pet_series_key TEXT NOT NULL REFERENCES series(series_key) ON DELETE CASCADE,
            ct_series_key TEXT NOT NULL REFERENCES series(series_key) ON DELETE CASCADE,
            pet_attenuation_corrected INTEGER
                CHECK (pet_attenuation_corrected IN (0, 1) OR pet_attenuation_corrected IS NULL),
            z_overlap_mm REAL,
            PRIMARY KEY (pet_series_key, ct_series_key)
        );
        CREATE INDEX series_pairs_by_ct ON series_pairs(ct_series_key);

        CREATE TABLE cohort_series (
            cohort_id INTEGER NOT NULL REFERENCES cohorts(cohort_id) ON DELETE CASCADE,
            series_key TEXT NOT NULL REFERENCES series(series_key),
            is_primary INTEGER NOT NULL CHECK (is_primary IN (0, 1)),
            PRIMARY KEY (cohort_id, series_key)
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
