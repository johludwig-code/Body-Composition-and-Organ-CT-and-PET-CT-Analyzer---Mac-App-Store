import Foundation

public enum JobStatus: String, Codable, Sendable, CaseIterable {
    case queued, running, done, failed, cancelled, interrupted

    public var isFinished: Bool { self == .done || self == .failed || self == .cancelled }
}

public struct QueuedJob: Sendable, Identifiable, Equatable {
    public var job: WorkerJob
    public var status: JobStatus
    public var stage: WorkerStage?
    public var fraction: Double?
    public var message: String?
    /// What an index job is doing, as numbers the app renders in its own words.
    public var detail: ProgressDetail?
    public var errorCode: String?
    public var lastHeartbeat: ContinuousClock.Instant?

    public var id: String { job.jobID }

    public init(job: WorkerJob, status: JobStatus = .queued) {
        self.job = job
        self.status = status
    }
}

/// Starts a worker for a job. A protocol so the queue can be tested with a
/// fake worker and the app can swap in another channel (XPC) later.
public protocol WorkerLauncher: Sendable {
    func launch(_ job: WorkerJob, jobFile: URL) throws -> WorkerProcess
}

public struct BundledWorkerLauncher: WorkerLauncher {
    public let layout: BundleLayout

    public init(layout: BundleLayout = .main) {
        self.layout = layout
    }

    public func launch(_ job: WorkerJob, jobFile: URL) throws -> WorkerProcess {
        let projectDir = URL(fileURLWithPath: job.projectDir)
        let worker = WorkerProcess(
            jobID: job.jobID,
            executable: layout.pythonExecutable,
            arguments: layout.workerArguments(jobFile: jobFile),
            environment: layout.workerEnvironment(projectDir: projectDir, jobID: job.jobID))
        try worker.start()
        return worker
    }
}

extension WorkerJob {
    /// Jobs of one group run one at a time (ADR 0021 decision 4): index jobs
    /// that scan or regroup all write the catalog, and the merge of one must
    /// come before the next starts. A previews job only reads it briefly and
    /// stays outside.
    public var exclusiveGroup: String? {
        guard kind == .index, case .string(let mode) = payload["mode"] else { return nil }
        return mode == "scan" || mode == "regroup" ? "catalog" : nil
    }
}

public enum QueuePolicy {
    /// Index and export jobs are I/O-bound and may run beside a segmentation;
    /// more than this many at once only contend for the disk.
    public static let maxParallelLightJobs = 2

    /// Which queued jobs to start now, in queue order. Pure, so the rules
    /// that matter (never two GPU jobs at once, never two jobs of one
    /// exclusive group) are tested without processes. A job counts as running
    /// until its finalize hook has returned.
    public static func jobsToStart(_ jobs: [QueuedJob], paused: Bool) -> [String] {
        guard !paused else { return [] }
        var gpuBusy = jobs.contains { $0.status == .running && $0.job.kind.needsExclusiveGPU }
        var lightRunning = jobs.filter { $0.status == .running && !$0.job.kind.needsExclusiveGPU }.count
        var busyGroups = Set(jobs.filter { $0.status == .running }.compactMap(\.job.exclusiveGroup))
        var start: [String] = []
        for job in jobs where job.status == .queued {
            if let group = job.job.exclusiveGroup, busyGroups.contains(group) { continue }
            if job.job.kind.needsExclusiveGPU {
                guard !gpuBusy else { continue }
                gpuBusy = true
            } else {
                guard lightRunning < maxParallelLightJobs else { continue }
                lightRunning += 1
            }
            if let group = job.job.exclusiveGroup { busyGroups.insert(group) }
            start.append(job.id)
        }
        return start
    }

    /// Status after a worker ended. The `done` event is authoritative; the
    /// exit code decides only when the worker died before sending one.
    public static func finalStatus(done: DoneStatus?, exitStatus: Int32?) -> JobStatus {
        switch done {
        case .ok: return .done
        case .cancelled: return .cancelled
        case .failed: return .failed
        case nil: return exitStatus == 130 ? .cancelled : .failed
        }
    }
}

/// The batch queue. Persisting the jobs is the caller's business through
/// `onChange`; after a crash the caller re-enqueues jobs it had recorded as
/// running with status `.interrupted` (plan §8).
public actor JobQueue {
    public typealias ChangeHandler = @Sendable (QueuedJob) async -> Void
    public typealias EventHandler = @Sendable (String, WorkerEvent) async -> Void
    /// Completes a job's payload just before its file is written. What must
    /// be current when the worker starts goes in here, never into the job
    /// that is queued: the link key above all, which Remove Identifiers
    /// deletes while a failed, paused or interrupted job may still wait to be
    /// run (ADR 0024). A handler that throws fails the job without starting it.
    public typealias PrepareHandler = @Sendable (WorkerJob) async throws -> WorkerJob
    /// Runs after a job's worker has exited and before the queue starts the
    /// next job of its exclusive group, with the job's final status: the
    /// merge of the catalog an index job wrote goes here (ADR 0021 decision
    /// 4). The job counts as running until the handler returns.
    public typealias FinalizeHandler = @Sendable (QueuedJob) async -> Void

    public private(set) var jobs: [QueuedJob] = []
    public private(set) var paused = false

    private let launcher: WorkerLauncher
    private let supervisor: WorkerSupervisor
    private let watchdog: Duration
    private let prepare: PrepareHandler
    private let finalize: FinalizeHandler
    private let onChange: ChangeHandler
    private let onEvent: EventHandler
    private var workers: [String: WorkerProcess] = [:]

    public init(
        launcher: WorkerLauncher,
        supervisor: WorkerSupervisor = .shared,
        watchdog: Duration = .seconds(300),
        prepare: @escaping PrepareHandler = { $0 },
        finalize: @escaping FinalizeHandler = { _ in },
        onChange: @escaping ChangeHandler = { _ in },
        onEvent: @escaping EventHandler = { _, _ in }
    ) {
        self.launcher = launcher
        self.supervisor = supervisor
        self.watchdog = watchdog
        self.prepare = prepare
        self.finalize = finalize
        self.onChange = onChange
        self.onEvent = onEvent
    }

    public func enqueue(_ job: WorkerJob, status: JobStatus = .queued) async {
        jobs.append(QueuedJob(job: job, status: status == .interrupted ? .queued : status))
        await onChange(jobs[jobs.count - 1])
        await schedule()
    }

    public func pause() { paused = true }

    public func resume() async {
        paused = false
        await schedule()
    }

    public func cancel(jobID: String) async {
        guard let index = jobs.firstIndex(where: { $0.id == jobID }) else { return }
        switch jobs[index].status {
        case .queued, .interrupted:
            await update(index) { $0.status = .cancelled }
        case .running:
            await workers[jobID]?.terminate()
        default:
            break
        }
    }

    public func retryFailed() async {
        for index in jobs.indices where jobs[index].status == .failed {
            await update(index) {
                $0.status = .queued
                $0.errorCode = nil
                $0.message = nil
            }
        }
        await schedule()
    }

    private func update(_ index: Int, _ change: (inout QueuedJob) -> Void) async {
        change(&jobs[index])
        await onChange(jobs[index])
    }

    private func schedule() async {
        for id in QueuePolicy.jobsToStart(jobs, paused: paused) {
            guard let index = jobs.firstIndex(where: { $0.id == id }) else { continue }
            await update(index) {
                $0.status = .running
                $0.lastHeartbeat = .now
            }
            let job = jobs[index].job
            Task { await self.run(job) }
        }
    }

    private func run(_ job: WorkerJob) async {
        var done: DoneStatus?
        var exitStatus: Int32?
        do {
            // Prepared now, not when queued: a payload fixed at enqueue keeps
            // whatever key it was given, and its key and key ID still agree
            // after Remove Identifiers, so the worker could not tell.
            let prepared = try await prepare(job)
            let jobFile = try write(prepared)
            let worker = try launcher.launch(prepared, jobFile: jobFile)
            workers[job.jobID] = worker
            supervisor.register(worker)
            let guardTask = Task { await self.watch(jobID: job.jobID) }
            defer { guardTask.cancel() }
            for try await event in worker.events() {
                if case .done(_, let status) = event { done = status }
                await apply(event, to: job.jobID)
                await onEvent(job.jobID, event)
            }
            while worker.isRunning { try? await Task.sleep(for: .milliseconds(50)) }
            exitStatus = worker.exitStatus
            if done == nil, let index = jobs.firstIndex(where: { $0.id == job.jobID }),
               jobs[index].errorCode == nil {
                // Died without a word: an import error before the protocol
                // existed, a crash, a kill. The exit status is all there is.
                let code = "worker_exit_\(exitStatus.map(String.init) ?? "unknown")"
                await update(index) { $0.errorCode = code }
            }
        } catch {
            exitStatus = nil
            // The type only: an error's text can quote a path.
            if let index = jobs.firstIndex(where: { $0.id == job.jobID }), jobs[index].errorCode == nil {
                await update(index) {
                    $0.errorCode = "worker_not_started"
                    $0.message = String(describing: type(of: error))
                }
            }
        }
        // The worker read its file when it started. The file holds absolute
        // source roots and, for an index job, the link key, so it goes now,
        // whatever the outcome, and before the next job starts (ADR 0024).
        try? FileManager.default.removeItem(at: Self.jobFile(for: job))
        await finish(job.jobID, done: done, exitStatus: exitStatus)
    }

    private func write(_ job: WorkerJob) throws -> URL {
        let directory = URL(fileURLWithPath: job.projectDir).appendingPathComponent("jobs")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        // Owner only, because a project folder may sit on a share that other
        // users can read. Set on every write: the folder may predate this rule.
        // The folder's mode also covers the moment between the atomic write and
        // the file's own mode. A volume that keeps no modes (some SMB servers,
        // exFAT) refuses them; the job still runs there, and its file is still
        // deleted when it ends.
        try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: directory.path)
        let file = Self.jobFile(for: job)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(job).write(to: file, options: .atomic)
        try? FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
        return file
    }

    static func jobFile(for job: WorkerJob) -> URL {
        URL(fileURLWithPath: job.projectDir).appendingPathComponent("jobs/\(job.jobID).json")
    }

    /// At launch, before anything is queued: a crash or a force quit leaves
    /// job files behind, and each still holds absolute source roots and
    /// perhaps the link key. Jobs are re-queued from the project database,
    /// never from these files.
    public static func removeLeftoverJobFiles(projectDir: URL) throws {
        let directory = projectDir.appendingPathComponent("jobs")
        guard FileManager.default.fileExists(atPath: directory.path) else { return }
        for name in try FileManager.default.contentsOfDirectory(atPath: directory.path) {
            try FileManager.default.removeItem(at: directory.appendingPathComponent(name))
        }
    }

    private func apply(_ event: WorkerEvent, to jobID: String) async {
        guard let index = jobs.firstIndex(where: { $0.id == jobID }) else { return }
        switch event {
        case .progress(_, let stage, _, let fraction, let message, let detail):
            await update(index) {
                $0.stage = stage
                $0.fraction = fraction
                $0.message = message
                $0.detail = detail
                $0.lastHeartbeat = .now
            }
        case .heartbeat:
            jobs[index].lastHeartbeat = .now
        case .error(let code, let message, _):
            await update(index) {
                $0.errorCode = code
                $0.message = message
            }
        default:
            break
        }
    }

    /// A worker that sends neither heartbeat nor progress for `watchdog` is
    /// hanging (a deadlocked Dask scheduler, an MPS kernel that never
    /// returns); it is terminated so the batch can go on.
    private func watch(jobID: String) async {
        while !Task.isCancelled {
            try? await Task.sleep(for: .seconds(30))
            guard let job = jobs.first(where: { $0.id == jobID }), job.status == .running else { return }
            if let last = job.lastHeartbeat, ContinuousClock.now - last > watchdog {
                if let index = jobs.firstIndex(where: { $0.id == jobID }) {
                    await update(index) { $0.errorCode = "watchdog_timeout" }
                }
                await workers[jobID]?.terminate()
                return
            }
        }
    }

    private func finish(_ jobID: String, done: DoneStatus?, exitStatus: Int32?) async {
        workers[jobID] = nil
        supervisor.unregister(jobID: jobID)
        if let index = jobs.firstIndex(where: { $0.id == jobID }) {
            let watchdogFired = jobs[index].errorCode == "watchdog_timeout"
            let status = watchdogFired ? .failed : QueuePolicy.finalStatus(done: done, exitStatus: exitStatus)
            var finished = jobs[index]
            finished.status = status
            // Still `.running` in `jobs` while the hook runs, so that a
            // schedule() triggered meanwhile by another job's end cannot start
            // the next job of the group before the merge is done.
            await finalize(finished)
            if let index = jobs.firstIndex(where: { $0.id == jobID }) {
                await update(index) { $0.status = status }
            }
        }
        await schedule()
    }
}
