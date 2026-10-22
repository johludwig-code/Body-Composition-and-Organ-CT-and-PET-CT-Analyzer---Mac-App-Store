import AppKit
import BCOAKit
import BCOAStore
import Foundation
import GRDB
import Observation
import OSLog

/// One open project: its database, its source folders, its job queue and
/// what the views show of them. The SQL and the rules live in BCOAStore and
/// BCOAKit; this type decides when they run.
@Observable
@MainActor
final class ProjectSession {
    let folder: SecurityScopedFolder
    let database: DatabaseQueue

    var sources: [SourceRow] = []
    var tree: ProjectTree = .empty
    var cohorts: [Cohort] = []
    var jobs: [QueuedJob] = []
    var results: [String: [String: JSONValue]] = [:]
    var message: String?
    /// A generation the merge could not take yet (ADR 0021 decision 11).
    var mergePending = false

    /// The models the bundle offers, and every run of the project with the
    /// state of its series (ADR 0031).
    let bundledModels: [BundledModel]
    var runs: [PipelineStore.RunInfo] = []
    var currentRunID: String?
    var progress: [PipelineStore.SeriesProgress] = []

    struct Cohort: Identifiable, Hashable {
        let id: Int64
        let label: String
    }

    /// Codes and job kinds only; never a path or an identifier (plan §13).
    nonisolated static let log = Logger(subsystem: "de.ludwig.bcoanalyzer", category: "jobs")

    @ObservationIgnored private(set) var grants: [Int: SecurityScopedFolder] = [:]
    @ObservationIgnored private var queue: JobQueue!
    @ObservationIgnored private var jobCounter = 0
    /// Pipeline jobs of this session: job → run and series.
    @ObservationIgnored var pipelineJobs: [String: (run: String, series: String)] = [:]
    /// Every result a job sent; a segmentation sends one per model.
    @ObservationIgnored var resultLists: [String: [[String: JSONValue]]] = [:]
    @ObservationIgnored private var recordedStatus: [String: JobStatus] = [:]

    var url: URL { folder.url }
    var catalogURL: URL { url.appendingPathComponent(IndexStore.catalogPath) }

    init(folder: SecurityScopedFolder) throws {
        self.folder = folder
        for directory in ["work", "viewer-cache", "exports", "logs", "jobs", "index"] {
            try FileManager.default.createDirectory(
                at: folder.url.appendingPathComponent(directory), withIntermediateDirectories: true)
        }
        // A worker reads its job file the moment it starts and the queue
        // deletes it when the job ends, so what is in jobs/ now was left by a
        // crash or a force quit, and still holds source paths (ADR 0024).
        try? JobQueue.removeLeftoverJobFiles(projectDir: folder.url)
        database = try ProjectStore.open(projectFolder: folder.url)
        bundledModels = BundledModel.load(from: BundleLayout.main.modelManifest)
        let database = database
        queue = JobQueue(
            launcher: BundledWorkerLauncher(),
            prepare: { job in try Self.prepare(job, database: database) },
            finalize: { [weak self] job in await self?.finalize(job) },
            onChange: { [weak self] job in await self?.upsert(job) },
            onEvent: { [weak self] jobID, event in await self?.handle(event, from: jobID) })
        try database.write { db in
            try IndexStore.ensureLinkKey(db)
            try IndexStore.markInterruptedSources(in: db)
        }
        resolveGrants()
        reload()
        Task { await self.atLaunch() }
    }

    // MARK: Launch

    /// ADR 0021 decision 8: merge what a crash left complete, then scan what
    /// was interrupted and what is reachable again.
    private func atLaunch() async {
        await merge()
        resumePipeline()
        #if DEBUG
        // Development only: `-BCOAAutoAnalyze clin_ct_organs` runs a batch on
        // the restored project without anyone at the keyboard, so the
        // pipeline can be checked in the real, sandboxed app.
        if let models = UserDefaults.standard.string(forKey: "BCOAAutoAnalyze"), !models.isEmpty {
            startAnalysis(models: models.split(separator: ",").map(String.init), device: "auto")
        }
        #endif
        let again = sources.filter { $0.state == "interrupted" || $0.state == "new" || $0.state == "unreachable" }
            .map(\.id)
        if !again.isEmpty { scan(again, quietly: true) }
    }

    func resolveGrants() {
        guard let rows = try? database.read({ try SourceRow.load($0) }) else { return }
        for row in rows where grants[row.id] == nil {
            // Read-only bookmarks: source DICOM is never modified (§8).
            if let folder = try? SecurityScopedFolder(bookmark: row.bookmark) {
                grants[row.id] = folder
            }
        }
    }

    func reload() {
        do {
            try database.read { db in
                sources = try SourceRow.load(db)
                tree = try ProjectTree.load(db)
                cohorts = try Row.fetchAll(db, sql: "SELECT cohort_id, label FROM cohorts ORDER BY label").map {
                    Cohort(id: $0["cohort_id"], label: $0["label"])
                }
                runs = try PipelineStore.runs(db)
                if currentRunID == nil || !runs.contains(where: { $0.id == currentRunID }) {
                    currentRunID = runs.first?.id
                }
                progress = try currentRunID.map { try PipelineStore.progress(runID: $0, in: db) } ?? []
            }
        } catch {
            message = String(localized: "The project database could not be read.")
        }
        if let status = IndexStore.status(ofCatalogAt: catalogURL) {
            mergePending = (try? database.read { try IndexStore.isPending(status, in: $0) }) ?? false
        } else {
            mergePending = false
        }
    }

    // MARK: Sources

    func addSources(_ urls: [URL]) {
        var added: [Int] = []
        let project = url.standardizedFileURL.resolvingSymlinksInPath().path
        for url in urls {
            let path = url.standardizedFileURL.resolvingSymlinksInPath().path
            // A source inside the project would index the project's own
            // catalog, which changes with every commit (ADR 0029 decision 7).
            if path == project || path.hasPrefix(project + "/") {
                message = String(localized: "A folder inside the project cannot be a source.")
                continue
            }
            if sources.contains(where: { grants[$0.id]?.url.standardizedFileURL.path == url.standardizedFileURL.path }) {
                continue
            }
            do {
                let grant = try SecurityScopedFolder(granted: url, readOnly: true)
                let id = try database.write { db -> Int in
                    try db.execute(
                        sql: """
                            INSERT INTO sources (bookmark, display_path, added_at, volume_kind)
                            VALUES (?, ?, ?, ?)
                            """,
                        arguments: [grant.bookmark, url.lastPathComponent,
                                    ISO8601DateFormatter().string(from: Date()), Self.volumeKind(url)])
                    return Int(db.lastInsertedRowID)
                }
                grants[id] = grant
                added.append(id)
            } catch {
                message = String(localized: "A source folder could not be added.")
            }
        }
        reload()
        if !added.isEmpty { scan(added) }
    }

    /// Remove Source (open question 25): the row stays as `removed` with an
    /// empty bookmark, and a regroup takes its series out of the index.
    func removeSource(_ id: Int) {
        do {
            try database.write { db in
                try db.execute(
                    sql: "UPDATE sources SET state = 'removed', bookmark = x'' WHERE id = ?", arguments: [id])
            }
            grants[id] = nil
            reload()
            regroup()
        } catch {
            message = String(localized: "The source could not be removed.")
        }
    }

    func isReachable(_ id: Int) -> Bool {
        guard let grant = grants[id] else { return false }
        return FileManager.default.fileExists(atPath: grant.url.path)
    }

    static func volumeKind(_ url: URL) -> String {
        let values = try? url.resourceValues(forKeys: [
            .volumeIsLocalKey, .volumeIsRemovableKey, .volumeIsEjectableKey,
        ])
        if values?.volumeIsLocal == false { return "network" }
        if values?.volumeIsRemovable == true || values?.volumeIsEjectable == true { return "removable" }
        return "local"
    }

    func indexSources() -> [IndexSource] {
        let all = (try? database.read { db in
            try Row.fetchAll(db, sql: "SELECT id, state, volume_kind FROM sources ORDER BY id")
        }) ?? []
        return all.map { row in
            let id: Int = row["id"]
            let state: String = row["state"]
            if state == "removed" {
                return IndexSource(id: id, root: nil, status: .removed, volumeKind: row["volume_kind"])
            }
            let root = isReachable(id) ? grants[id]?.url : nil
            return IndexSource(id: id, root: root, status: root == nil ? .unavailable : .active,
                               volumeKind: root.map(Self.volumeKind) ?? row["volume_kind"])
        }
    }

    // MARK: Index jobs

    private var selectionConfig: JSONValue { storedJSON("selection_config") }
    private var identityConfig: JSONValue { storedJSON("identity_config") }

    private func storedJSON(_ key: String) -> JSONValue {
        guard let text = try? database.read({ try IndexStore.meta($0, key) }),
              let value = try? JSONDecoder().decode(JSONValue.self, from: Data(text.utf8))
        else { return .object([:]) }
        return value
    }

    private var mergedGeneration: Int {
        Int(((try? database.read { try IndexStore.meta($0, "merged_generation") }) ?? nil) ?? "0") ?? 0
    }

    /// Scans the sources given, or all; one job walks them all.
    func scan(_ ids: [Int]? = nil, forceReread: Bool = false, quietly: Bool = false) {
        resolveGrants()
        let listed = indexSources()
        let wanted = ids ?? listed.filter { $0.status != .removed }.map(\.id)
        let scan = IndexJobs.scannable(listed, among: wanted)
        let unreachable = Set(wanted).subtracting(scan)
        try? database.write { db in
            for id in unreachable {
                try db.execute(sql: "UPDATE sources SET state = 'unreachable' WHERE id = ? AND state <> 'removed'",
                               arguments: [id])
            }
            for id in scan {
                try db.execute(sql: "UPDATE sources SET state = 'indexing' WHERE id = ?", arguments: [id])
            }
        }
        reload()
        guard !scan.isEmpty else {
            if !quietly { message = String(localized: "No source folder is reachable. Connect the disk or share and try again.") }
            return
        }
        let payload = IndexJobs.scan(
            sources: listed, scan: scan, mergedGeneration: mergedGeneration,
            selection: selectionConfig, identity: identityConfig, forceReread: forceReread)
        enqueue(.index, prefix: "index", payload: payload)
    }

    func regroup() {
        let payload = IndexJobs.regroup(
            sources: indexSources(), mergedGeneration: mergedGeneration,
            selection: selectionConfig, identity: identityConfig)
        enqueue(.index, prefix: "regroup", payload: payload)
    }

    /// The link key goes into an index payload when its job starts, never when
    /// it is queued (ADR 0024); after Remove Identifiers both are null.
    nonisolated private static func prepare(_ job: WorkerJob, database: DatabaseQueue) throws -> WorkerJob {
        guard job.exclusiveGroup == "catalog" else { return job }
        let (key, id) = try database.read { db in
            (try IndexStore.meta(db, "link_key"), try IndexStore.meta(db, "link_key_id"))
        }
        var prepared = job
        prepared.payload = IndexJobs.withLinkKey(job.payload, key: key, keyID: id)
        return prepared
    }

    private func finalize(_ job: QueuedJob) async {
        if pipelineJobs[job.id] != nil {
            await finalizeStage(job)
            reload()
            return
        }
        let isCatalogJob = job.job.exclusiveGroup == "catalog"
        let catalogBusy = jobs.contains { $0.id != job.id && $0.status == .running && $0.job.exclusiveGroup == "catalog" }
        if isCatalogJob {
            if let payload = results[job.id], let result = IndexResult(payload: payload), !result.changed {
                let scans = result.sources.mapValues {
                    IndexStore.SourceScan(state: $0.state, files: $0.files, badDirs: $0.badDirs, code: $0.code)
                }
                write { try IndexStore.applyUnchangedScan(scans, in: $0) }
            }
            await merge()
            // A job that ended before its catalog said anything about a source
            // leaves it indexing; it is interrupted, and continues next time.
            write { db in
                try db.execute(sql: "UPDATE sources SET state = 'interrupted' WHERE state = 'indexing'")
            }
            if job.status == .failed {
                let code = job.errorCode ?? "unknown"
                Self.log.error("index job failed: \(code, privacy: .public) \(job.message ?? "", privacy: .public)")
                message = String(localized: "Indexing stopped (\(code)). The next scan continues where it stopped.")
            }
        } else if job.job.kind == .export, !catalogBusy {
            // ADR 0021 decision 11: a merge an export's read held off is tried
            // again when the export ends.
            await merge()
        }
        reload()
    }

    /// Synchronous on purpose: inside an async function GRDB's async
    /// overload would be chosen, and these writes are a few rows.
    func write(_ updates: (Database) throws -> Void) {
        do {
            try database.write(updates)
        } catch {
            message = String(localized: "The project database could not be written.")
        }
    }

    private func merge() async {
        let database = database
        let catalog = catalogURL
        let outcome = await Task.detached {
            Result { try IndexStore.mergeIfPending(project: database, catalog: catalog) }
        }.value
        if case .failure = outcome {
            message = String(localized: "The index could not be applied yet; it is tried again after the next job.")
        }
        reload()
    }

    // MARK: Selection

    var reviewer: String {
        let stored = UserDefaults.standard.string(forKey: "reviewerInitials") ?? ""
        return stored.isEmpty ? "user" : stored
    }

    func apply(_ edit: IndexStore.SelectionEdit) {
        do {
            try database.write { db in try IndexStore.apply(edit, in: db, actor: reviewer) }
        } catch {
            message = String(localized: "The selection could not be saved.")
        }
        reload()
    }

    // MARK: Jobs

    /// Queues a job; a pipeline job (with `run` and `series`) is recorded in
    /// `jobs` first, so that a crash before it starts still leaves its row.
    @discardableResult
    func enqueue(
        _ kind: JobKind, prefix: String, payload: [String: JSONValue],
        run: String? = nil, series: String? = nil
    ) -> String {
        jobCounter += 1
        let stamp = Int(Date().timeIntervalSince1970)
        let id = "j_\(prefix)_\(stamp)_\(jobCounter)"
        if let run, let series {
            pipelineJobs[id] = (run, series)
            recordedStatus[id] = .queued
            write {
                try PipelineStore.recordJob(id: id, runID: run, seriesKey: series, kind: kind.rawValue,
                                            status: "queued", in: $0)
            }
        }
        let job = WorkerJob(
            jobID: id, kind: kind, projectDir: url, resourcesDir: BundleLayout.main.resources,
            payload: payload)
        Task { await queue.enqueue(job) }
        return id
    }

    func cancel(_ jobID: String) {
        Task { await queue.cancel(jobID: jobID) }
    }

    func retryFailed() {
        Task { await queue.retryFailed() }
    }

    private func upsert(_ job: QueuedJob) {
        if let index = jobs.firstIndex(where: { $0.id == job.id }) {
            jobs[index] = job
        } else {
            jobs.append(job)
        }
        // A pipeline job's state is written when it changes, not with every
        // progress line; its end is written by the finalize hook.
        if pipelineJobs[job.id] != nil, recordedStatus[job.id] != job.status, !job.status.isFinished {
            recordedStatus[job.id] = job.status
            write { try PipelineStore.updateJob(id: job.id, status: job.status.rawValue, errorCode: nil, in: $0) }
        }
    }

    private func handle(_ event: WorkerEvent, from jobID: String) {
        switch event {
        case .result(_, let payload):
            results[jobID] = payload
            resultLists[jobID, default: []].append(payload)
        case .hello(_, let versions):
            if let run = pipelineJobs[jobID]?.run {
                write { try PipelineStore.mergeVersions(versions, into: run, in: $0) }
            }
        default:
            break
        }
    }

    var isIndexing: Bool {
        jobs.contains { $0.job.kind == .index && ($0.status == .running || $0.status == .queued) }
    }

    var indexProgress: QueuedJob? {
        jobs.last { $0.job.kind == .index && $0.status == .running }
    }
}
