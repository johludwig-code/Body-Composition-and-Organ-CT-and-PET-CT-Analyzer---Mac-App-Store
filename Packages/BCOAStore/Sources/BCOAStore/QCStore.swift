import Foundation
import GRDB

/// QC decisions (plan §12): a status per series and run, and labels excluded
/// one by one. Rows are appended, never changed, so the `qc` table is also
/// the history of who decided what and when; the newest row counts, and each
/// change is in the audit log with no patient data.
public enum QCStore {
    public enum Status: String, CaseIterable, Sendable {
        case unreviewed
        case accepted
        case acceptedWithComment = "accepted_with_comment"
        case rejected
    }

    public struct SeriesQC: Sendable, Equatable {
        public var status: Status
        public var reviewer: String?
        public var at: String?
        public var comment: String?
        /// model → label ids excluded, with the reason given.
        public var excluded: [String: [Int: String]]

        public static let unreviewed = SeriesQC(status: .unreviewed, reviewer: nil, at: nil, comment: nil, excluded: [:])
    }

    public static func set(
        _ status: Status, comment: String?, runID: String, seriesKey: String, reviewer: String,
        in db: Database, now: Date = Date()
    ) throws {
        let text = comment?.trimmingCharacters(in: .whitespacesAndNewlines)
        let stored = status == .accepted && !(text ?? "").isEmpty ? Status.acceptedWithComment : status
        try db.execute(
            sql: """
                INSERT INTO qc (run_id, series_key, model, label_id, status, reviewer, at, comment)
                VALUES (?, ?, NULL, NULL, ?, ?, ?, ?)
                """,
            arguments: [runID, seriesKey, stored.rawValue, reviewer, iso(now), (text ?? "").isEmpty ? nil : text])
        try IndexStore.audit(db, actor: reviewer, action: "qc_changed", object: seriesKey,
                             details: ["run_id": runID, "status": stored.rawValue], now: now)
    }

    /// Excludes one label of one model, or takes the exclusion back: its
    /// values stay empty in the export and the reason goes into the qc sheet.
    public static func exclude(
        _ excluded: Bool, model: String, labelID: Int, reason: String?, runID: String, seriesKey: String,
        reviewer: String, in db: Database, now: Date = Date()
    ) throws {
        try db.execute(
            sql: """
                INSERT INTO qc (run_id, series_key, model, label_id, status, reviewer, at, comment)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
            arguments: [runID, seriesKey, model, labelID, excluded ? "label_excluded" : "unreviewed",
                        reviewer, iso(now), reason])
        try IndexStore.audit(db, actor: reviewer, action: excluded ? "label_excluded" : "label_included",
                             object: seriesKey, details: ["run_id": runID, "model": model, "label_id": labelID],
                             now: now)
    }

    public static func current(runID: String, seriesKey: String, in db: Database) throws -> SeriesQC {
        var result = SeriesQC.unreviewed
        for row in try Row.fetchAll(db, sql: """
            SELECT model, label_id, status, reviewer, at, comment FROM qc
            WHERE run_id = ? AND series_key = ? ORDER BY at, id
            """, arguments: [runID, seriesKey]) {
            let status: String = row["status"]
            if let model: String = row["model"], let label: Int = row["label_id"] {
                if status == "label_excluded" {
                    result.excluded[model, default: [:]][label] = row["comment"] ?? ""
                } else {
                    result.excluded[model]?[label] = nil
                }
            } else if let parsed = Status(rawValue: status) {
                result.status = parsed
                result.reviewer = row["reviewer"]
                result.at = row["at"]
                result.comment = row["comment"]
            }
        }
        return result
    }

    static func iso(_ date: Date) -> String { ISO8601DateFormatter().string(from: date) }
}
