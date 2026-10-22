import Foundation
import GRDB

/// Patient → study → series as the import's tree shows it (plan §7). Plain
/// values read in one transaction, so a view never holds a database row and
/// a merge that commits meanwhile is seen whole or not at all.
public struct ProjectTree: Sendable, Equatable {
    public struct Check: Sendable, Equatable, Hashable {
        public var code: String
        public var level: String
        public var paramsJSON: String
    }

    public struct Series: Sendable, Equatable, Identifiable, Hashable {
        public var id: String { key }
        public var key: String
        public var studyKey: String
        public var seriesNumber: Int?
        public var modality: String
        public var description: String?
        public var imageCount: Int
        public var sliceCount: Int?
        public var sliceThicknessMM: Double?
        public var zExtentMM: Double?
        public var kernel: String?
        public var kernelClass: String?
        public var manufacturer: String?
        public var scannerModel: String?
        public var contrastAgent: String?
        public var orientation: String?
        public var selected: Bool
        public var primary: Bool
        public var origin: String
        public var cohortLabel: String?
        public var reasonJSON: String?
        public var autoRank: Int?
        public var gone: Bool
        public var checks: [Check]
    }

    public struct Study: Sendable, Equatable, Identifiable, Hashable {
        public var id: String { key }
        public var key: String
        public var date: String?
        public var description: String?
        public var ageYears: Double?
        public var selectionMode: String
        public var gone: Bool
        public var checks: [Check]
        public var series: [Series]
    }

    public struct Patient: Sendable, Equatable, Identifiable, Hashable {
        public var id: String { key }
        public var key: String
        public var pseudonym: String
        public var sex: String?
        public var ageAtFirstStudy: Double?
        public var idStatus: String
        public var checks: [Check]
        public var studies: [Study]
    }

    public var patients: [Patient]
    public var projectChecks: [Check]

    public var seriesCount: Int { patients.reduce(0) { $0 + $1.studies.reduce(0) { $0 + $1.series.count } } }
    public var selectedCount: Int {
        patients.reduce(0) { $0 + $1.studies.reduce(0) { $0 + $1.series.filter(\.selected).count } }
    }

    public static let empty = ProjectTree(patients: [], projectChecks: [])

    public static func load(_ db: Database) throws -> ProjectTree {
        var checks: [String: [String: [Check]]] = [:]
        for row in try Row.fetchAll(db, sql: """
            SELECT object_kind, object_key, code, level, params_json FROM index_checks
            ORDER BY object_kind, object_key, level DESC, code
            """) {
            let check = Check(code: row["code"], level: row["level"], paramsJSON: row["params_json"])
            checks[row["object_kind"], default: [:]][row["object_key"], default: []].append(check)
        }
        let cohorts = try Dictionary(
            Row.fetchAll(db, sql: "SELECT cohort_id, label FROM cohorts").map {
                ($0["cohort_id"] as Int64, $0["label"] as String)
            }, uniquingKeysWith: { first, _ in first })

        var seriesByStudy: [String: [Series]] = [:]
        for row in try Row.fetchAll(db, sql: """
            SELECT series_key, study_key, series_number, modality, description, image_count, slice_count,
                   slice_thickness_mm, z_extent_mm, kernel, kernel_class, manufacturer, scanner_model,
                   contrast_agent, orientation, selected, is_primary, selection_origin,
                   selection_cohort_id, selection_reason, auto_rank, index_state
            FROM series
            ORDER BY study_key, series_number IS NULL, series_number, series_uid, part
            """) {
            let key: String = row["series_key"]
            let cohort: Int64? = row["selection_cohort_id"]
            let series = Series(
                key: key, studyKey: row["study_key"], seriesNumber: row["series_number"],
                modality: row["modality"], description: row["description"],
                imageCount: row["image_count"], sliceCount: row["slice_count"],
                sliceThicknessMM: row["slice_thickness_mm"], zExtentMM: row["z_extent_mm"],
                kernel: row["kernel"], kernelClass: row["kernel_class"],
                manufacturer: row["manufacturer"], scannerModel: row["scanner_model"],
                contrastAgent: row["contrast_agent"], orientation: row["orientation"],
                selected: row["selected"], primary: row["is_primary"], origin: row["selection_origin"],
                cohortLabel: cohort.flatMap { cohorts[$0] }, reasonJSON: row["selection_reason"],
                autoRank: row["auto_rank"], gone: (row["index_state"] as String) == "gone",
                checks: checks["series"]?[key] ?? [])
            seriesByStudy[series.studyKey, default: []].append(series)
        }

        var studiesByPatient: [String: [Study]] = [:]
        for row in try Row.fetchAll(db, sql: """
            SELECT study_key, patient_key, study_date, description, age_years, selection_mode, index_state
            FROM studies ORDER BY patient_key, study_date IS NULL, study_date, study_key
            """) {
            let key: String = row["study_key"]
            let study = Study(
                key: key, date: row["study_date"], description: row["description"],
                ageYears: row["age_years"], selectionMode: row["selection_mode"],
                gone: (row["index_state"] as String) == "gone",
                checks: checks["study"]?[key] ?? [], series: seriesByStudy[key] ?? [])
            studiesByPatient[row["patient_key"], default: []].append(study)
        }

        let patients = try Row.fetchAll(db, sql: """
            SELECT patient_key, pseudonym, sex, age_at_first_study, id_status FROM patients
            ORDER BY length(pseudonym), pseudonym
            """).map { row -> Patient in
            let key: String = row["patient_key"]
            return Patient(
                key: key, pseudonym: row["pseudonym"], sex: row["sex"],
                ageAtFirstStudy: row["age_at_first_study"], idStatus: row["id_status"],
                checks: checks["patient"]?[key] ?? [], studies: studiesByPatient[key] ?? [])
        }
        return ProjectTree(
            patients: patients,
            projectChecks: (checks["project"]?["project"] ?? []) + (checks["source"]?.values.flatMap { $0 } ?? []))
    }
}

/// A source folder as the Sources list shows it.
public struct SourceRow: Sendable, Equatable, Identifiable {
    public var id: Int
    public var bookmark: Data
    public var displayPath: String
    public var state: String
    public var volumeKind: String
    public var indexedAt: String?
    public var summaryJSON: String

    public static func load(_ db: Database) throws -> [SourceRow] {
        try Row.fetchAll(db, sql: """
            SELECT id, bookmark, display_path, state, volume_kind, indexed_at, summary_json
            FROM sources WHERE state <> 'removed' ORDER BY id
            """).map {
            SourceRow(
                id: $0["id"], bookmark: $0["bookmark"], displayPath: $0["display_path"],
                state: $0["state"], volumeKind: $0["volume_kind"], indexedAt: $0["indexed_at"],
                summaryJSON: $0["summary_json"])
        }
    }

    /// The file count of the last scan, from the summary the merge or an
    /// unchanged scan wrote.
    public var files: Int? {
        guard let data = summaryJSON.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }
        return object["files"] as? Int
    }
}
