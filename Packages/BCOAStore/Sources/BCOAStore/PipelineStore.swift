import Foundation
import GRDB

/// The pipeline's records in `project.sqlite` (plan §6, §8; ADR 0031): runs,
/// the jobs of each series and stage, and the metrics rows. The worker never
/// writes the project; everything here comes from a job's result.
public enum PipelineStore {
    /// The four stages in the order a series goes through them.
    public static let stages = ["convert", "segment", "metrics", "viewer_cache"]

    public struct ModelEntry: Sendable, Equatable, Codable {
        public var name: String
        public var zipSHA256: String?
        public var labels: [Int: String]

        public init(name: String, zipSHA256: String?, labels: [Int: String]) {
            self.name = name
            self.zipSHA256 = zipSHA256
            self.labels = labels
        }

        var json: [String: Any] {
            [
                "name": name,
                "zip_sha256": zipSHA256 as Any,
                "labels": Dictionary(uniqueKeysWithValues: labels.map { (String($0.key), $0.value) }),
            ]
        }
    }

    // MARK: Runs

    /// The newest unlocked run with exactly these models and settings, or a
    /// new one (ADR 0031 decision 9). Settings and versions are JSON objects.
    public static func run(
        models: [ModelEntry], device: String, settings: [String: Any], versions: [String: String],
        in db: Database, now: Date = Date()
    ) throws -> String {
        let modelsJSON = try json(models.sorted { $0.name < $1.name }.map(\.json))
        let settingsJSON = try json(settings)
        let candidates = try Row.fetchAll(db, sql: """
            SELECT run_id, models_json, settings_json, device FROM runs
            WHERE locked = 0 ORDER BY created_at DESC
            """)
        for row in candidates where row["models_json"] == modelsJSON
            && row["settings_json"] == settingsJSON && row["device"] == device {
            return row["run_id"]
        }
        var id = "R" + stamp(now)
        // Two batches in one second get distinct runs.
        var suffix = 1
        while try Bool.fetchOne(db, sql: "SELECT EXISTS (SELECT 1 FROM runs WHERE run_id = ?)", arguments: [id]) == true {
            suffix += 1
            id = "R" + stamp(now) + "-\(suffix)"
        }
        try db.execute(
            sql: """
                INSERT INTO runs (run_id, created_at, versions_json, device, models_json, settings_json, locked)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
            arguments: [id, iso(now), try json(versions), device, modelsJSON, settingsJSON])
        return id
    }

    /// Adds the worker's versions from its hello, while the run is not
    /// locked: a locked run's provenance is what its export said.
    public static func mergeVersions(_ versions: [String: String], into runID: String, in db: Database) throws {
        guard let row = try Row.fetchOne(
            db, sql: "SELECT versions_json, locked FROM runs WHERE run_id = ?", arguments: [runID]),
              (row["locked"] as Int) == 0
        else { return }
        let stored = (try? JSONSerialization.jsonObject(with: Data((row["versions_json"] as String).utf8)))
            as? [String: String] ?? [:]
        // What the run recorded first stays: a run's versions are those its
        // first job ran with, and every job of it runs from the same bundle.
        let merged = stored.merging(versions) { first, _ in first }
        guard merged != stored else { return }
        try db.execute(sql: "UPDATE runs SET versions_json = ? WHERE run_id = ?",
                       arguments: [try json(merged), runID])
    }

    public static func lock(_ runID: String, in db: Database) throws {
        try db.execute(sql: "UPDATE runs SET locked = 1 WHERE run_id = ?", arguments: [runID])
    }

    public struct RunInfo: Sendable, Equatable, Identifiable {
        public var id: String
        public var createdAt: String
        public var device: String
        public var models: [String]
        public var locked: Bool
    }

    public static func runs(_ db: Database) throws -> [RunInfo] {
        try Row.fetchAll(db, sql: "SELECT * FROM runs ORDER BY created_at DESC").map { row in
            let models = ((try? JSONSerialization.jsonObject(with: Data((row["models_json"] as String).utf8)))
                as? [[String: Any]] ?? []).compactMap { $0["name"] as? String }
            return RunInfo(id: row["run_id"], createdAt: row["created_at"], device: row["device"],
                           models: models, locked: row["locked"])
        }
    }

    /// The models of a run, as it recorded them.
    public static func models(of runID: String, in db: Database) throws -> [ModelEntry] {
        guard let text = try String.fetchOne(db, sql: "SELECT models_json FROM runs WHERE run_id = ?",
                                             arguments: [runID]),
              let list = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [[String: Any]]
        else { return [] }
        return list.compactMap { entry in
            guard let name = entry["name"] as? String else { return nil }
            let labels = (entry["labels"] as? [String: String] ?? [:]).reduce(into: [Int: String]()) {
                if let id = Int($1.key) { $0[id] = $1.value }
            }
            return ModelEntry(name: name, zipSHA256: entry["zip_sha256"] as? String, labels: labels)
        }
    }

    // MARK: Jobs

    public static func recordJob(
        id: String, runID: String?, seriesKey: String?, kind: String, status: String,
        in db: Database, now: Date = Date()
    ) throws {
        try db.execute(
            sql: """
                INSERT INTO jobs (job_id, run_id, series_key, kind, status, queued_at, log_path)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (job_id) DO UPDATE SET status = excluded.status
                """,
            arguments: [id, runID, seriesKey, kind, status, iso(now), "logs/\(id).log"])
    }

    /// A status change of a recorded job; nothing for a job that is not one
    /// of the pipeline's (index, export, self-test).
    public static func updateJob(
        id: String, status: String, errorCode: String?, device: String? = nil,
        in db: Database, now: Date = Date()
    ) throws {
        switch status {
        case "running":
            try db.execute(sql: "UPDATE jobs SET status = ?, started_at = coalesce(started_at, ?) WHERE job_id = ?",
                           arguments: [status, iso(now), id])
        case "done", "failed", "cancelled":
            try db.execute(
                sql: """
                    UPDATE jobs SET status = ?, ended_at = ?, error_code = ?, device = coalesce(?, device)
                    WHERE job_id = ?
                    """,
                arguments: [status, iso(now), errorCode, device, id])
        default:
            try db.execute(sql: "UPDATE jobs SET status = ? WHERE job_id = ?", arguments: [status, id])
        }
    }

    public struct PendingStage: Sendable, Equatable {
        public var jobID: String
        public var runID: String
        public var seriesKey: String
        public var kind: String
    }

    /// At launch (plan §8): what was queued or running when the app last
    /// stopped is interrupted, and is queued again.
    public static func interruptLeftovers(in db: Database) throws -> [PendingStage] {
        let rows = try Row.fetchAll(db, sql: """
            SELECT job_id, run_id, series_key, kind FROM jobs
            WHERE status IN ('queued', 'running') AND run_id IS NOT NULL AND series_key IS NOT NULL
            ORDER BY queued_at, job_id
            """)
        try db.execute(sql: "UPDATE jobs SET status = 'interrupted' WHERE status IN ('queued', 'running')")
        return rows.map {
            PendingStage(jobID: $0["job_id"], runID: $0["run_id"], seriesKey: $0["series_key"], kind: $0["kind"])
        }
    }

    // MARK: Results

    public struct ResultRow: Sendable, Equatable {
        public var model: String
        public var labelID: Int
        public var labelName: String
        public var voxelCount: Int
        public var volumeML: Double?
        public var huMean: Double?
        public var huSD: Double?
        public var huMedian: Double?
        public var huP05: Double?
        public var huP95: Double?
        public var huMin: Double?
        public var huMax: Double?
        public var touchesBorder: Bool
        public var device: String
        public var flags: [String]

        public init(model: String, labelID: Int, labelName: String, voxelCount: Int, volumeML: Double?,
                    huMean: Double?, huSD: Double?, huMedian: Double?, huP05: Double?, huP95: Double?,
                    huMin: Double?, huMax: Double?, touchesBorder: Bool, device: String, flags: [String]) {
            self.model = model
            self.labelID = labelID
            self.labelName = labelName
            self.voxelCount = voxelCount
            self.volumeML = volumeML
            self.huMean = huMean
            self.huSD = huSD
            self.huMedian = huMedian
            self.huP05 = huP05
            self.huP95 = huP95
            self.huMin = huMin
            self.huMax = huMax
            self.touchesBorder = touchesBorder
            self.device = device
            self.flags = flags
        }
    }

    /// The rows of one series, replacing what the run held for its models.
    public static func storeResults(_ rows: [ResultRow], runID: String, seriesKey: String, in db: Database) throws {
        for model in Set(rows.map(\.model)) {
            try db.execute(sql: "DELETE FROM results WHERE run_id = ? AND series_key = ? AND model = ?",
                           arguments: [runID, seriesKey, model])
        }
        for row in rows {
            try db.execute(
                sql: """
                    INSERT INTO results (run_id, series_key, model, label_id, label_name, voxel_count,
                        volume_ml, hu_mean, hu_sd, hu_median, hu_p05, hu_p95, hu_min, hu_max,
                        touches_border, device, flags)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                arguments: [runID, seriesKey, row.model, row.labelID, row.labelName, row.voxelCount,
                            row.volumeML, row.huMean, row.huSD, row.huMedian, row.huP05, row.huP95,
                            row.huMin, row.huMax, row.touchesBorder, row.device,
                            row.flags.joined(separator: ",")])
        }
    }

    /// Selected current series of CT that have no metrics yet for every model
    /// of the run: what "Analyze Selected" queues.
    public static func seriesToAnalyze(runID: String, models: [String], in db: Database) throws -> [String] {
        let keys = try String.fetchAll(db, sql: """
            SELECT s.series_key FROM series AS s
            JOIN studies AS st USING (study_key)
            JOIN patients AS p USING (patient_key)
            WHERE s.selected = 1 AND s.index_state = 'current' AND p.id_status <> 'unconfirmed'
            ORDER BY p.pseudonym, st.study_date, s.series_key
            """)
        return try keys.filter { key in
            let done = try Set(String.fetchAll(
                db, sql: "SELECT DISTINCT model FROM results WHERE run_id = ? AND series_key = ?",
                arguments: [runID, key]))
            return !Set(models).isSubset(of: done)
        }
    }

    /// The catalog part a series was merged from: `catalog_id:part_ref`.
    public static func catalogPart(of seriesKey: String, in db: Database) throws -> (catalogID: String, partRef: Int)? {
        guard let text = try String.fetchOne(db, sql: "SELECT catalog_part FROM series WHERE series_key = ?",
                                             arguments: [seriesKey]),
              let colon = text.lastIndex(of: ":"),
              let ref = Int(text[text.index(after: colon)...])
        else { return nil }
        return (String(text[..<colon]), ref)
    }

    // MARK: Status for the views

    public struct SeriesProgress: Sendable, Equatable, Identifiable {
        public var id: String { seriesKey }
        public var seriesKey: String
        public var pseudonym: String
        public var studyDate: String?
        public var description: String?
        /// The newest job of each stage: kind → (status, error code).
        public var stages: [String: (status: String, error: String?)]
        public var modelsWithResults: [String]
        public var flags: [String]
        public var qcStatus: String?

        public static func == (a: SeriesProgress, b: SeriesProgress) -> Bool {
            a.seriesKey == b.seriesKey && a.modelsWithResults == b.modelsWithResults && a.flags == b.flags
                && a.qcStatus == b.qcStatus
                && a.stages.mapValues { "\($0.status)|\($0.error ?? "")" }
                    == b.stages.mapValues { "\($0.status)|\($0.error ?? "")" }
        }
    }

    /// Every series the run touched, with the state of each stage.
    public static func progress(runID: String, in db: Database) throws -> [SeriesProgress] {
        var stages: [String: [String: (String, String?)]] = [:]
        for row in try Row.fetchAll(db, sql: """
            SELECT series_key, kind, status, error_code FROM jobs
            WHERE run_id = ? AND series_key IS NOT NULL ORDER BY queued_at, job_id
            """, arguments: [runID]) {
            stages[row["series_key"], default: [:]][row["kind"]] = (row["status"], row["error_code"])
        }
        var models: [String: [String]] = [:]
        var flags: [String: Set<String>] = [:]
        for row in try Row.fetchAll(db, sql: """
            SELECT series_key, model, group_concat(flags) AS flags FROM results WHERE run_id = ?
            GROUP BY series_key, model ORDER BY series_key, model
            """, arguments: [runID]) {
            let key: String = row["series_key"]
            models[key, default: []].append(row["model"])
            let text: String = row["flags"] ?? ""
            flags[key, default: []].formUnion(text.split(separator: ",").map(String.init))
        }
        let qc = try Dictionary(
            Row.fetchAll(db, sql: """
                SELECT series_key, status FROM qc WHERE run_id = ? AND model IS NULL ORDER BY at, id
                """, arguments: [runID]).map { ($0["series_key"] as String, $0["status"] as String) },
            uniquingKeysWith: { _, last in last })
        let keys = Set(stages.keys).union(models.keys)
        return try Row.fetchAll(db, sql: """
            SELECT s.series_key, p.pseudonym, st.study_date, s.description FROM series AS s
            JOIN studies AS st USING (study_key) JOIN patients AS p USING (patient_key)
            ORDER BY length(p.pseudonym), p.pseudonym, st.study_date, s.series_key
            """).compactMap { row -> SeriesProgress? in
            let key: String = row["series_key"]
            guard keys.contains(key) else { return nil }
            return SeriesProgress(
                seriesKey: key, pseudonym: row["pseudonym"], studyDate: row["study_date"],
                description: row["description"],
                stages: (stages[key] ?? [:]).mapValues { (status: $0.0, error: $0.1) },
                modelsWithResults: models[key] ?? [], flags: (flags[key] ?? []).sorted(),
                qcStatus: qc[key])
        }
    }

    // MARK: Helpers

    static func json(_ value: Any) throws -> String {
        String(decoding: try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), as: UTF8.self)
    }

    static func iso(_ date: Date) -> String { ISO8601DateFormatter().string(from: date) }

    static func stamp(_ date: Date) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone(identifier: "UTC")
        formatter.dateFormat = "yyyyMMdd'T'HHmmss"
        return formatter.string(from: date)
    }
}
