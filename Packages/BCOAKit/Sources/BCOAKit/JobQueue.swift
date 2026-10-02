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

public enum QueuePolicy {
    /// Index and export jobs are I/O-bound and may run beside a segmentation;
    /// more than this many at once only contend for the disk.
    public static let maxParallelLightJobs = 2

    /// Which queued jobs to start now, in queue order. Pure, so the rule that
    /// matters (never two GPU jobs at once) is tested without processes.
    public static func jobsToStart(_ jobs: [QueuedJob], paused: Bool) -> [String] {
        guard !paused else { return [] }
        var gpuBusy = jobs.contains { $0.status == .running && $0.job.kind.needsExclusiveGPU }
        var lightRunning = jobs.filter { $0.status == .running && !$0.job.kind.needsExclusiveGPU }.count
        var start: [String] = []
        for job in jobs where job.status == .queued {
            if job.job.kind.needsExclusiveGPU {
                if !gpuBusy {
                    gpuBusy = true
                    start.append(job.id)
                }
            } else if lightRunning < maxParallelLightJobs {
                lightRunning += 1
                start.append(job.id)
            }
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

    public private(set) var jobs: [QueuedJob] = []
    public private(set) var paused = false

    private let launcher: WorkerLauncher
    private let supervisor: WorkerSupervisor
    private let watchdog: Duration
    private let onChange: ChangeHandler
    private let onEvent: EventHandler
    private var workers: [String: WorkerProcess] = [:]

    public init(
        launcher: WorkerLauncher,
        supervisor: WorkerSupervisor = .shared,
        watchdog: Duration = .seconds(300),
        onChange: @escaping ChangeHandler = { _ in },
        onEvent: @escaping EventHandler = { _, _ in }
    ) {
        self.launcher = launcher
        self.supervisor = supervisor
        self.watchdog = watchdog
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
        do {
            let jobFile = try write(job)
            let worker = try launcher.launch(job, jobFile: jobFile)
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
            await finish(job.jobID, done: done, exitStatus: worker.exitStatus)
        } catch {
            await finish(job.jobID, done: done, exitStatus: nil)
        }
    }

    private func write(_ job: WorkerJob) throws -> URL {
        let directory = URL(fileURLWithPath: job.projectDir).appendingPathComponent("jobs")
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let file = directory.appendingPathComponent("\(job.jobID).json")
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        try encoder.encode(job).write(to: file, options: .atomic)
        return file
    }

    private func apply(_ event: WorkerEvent, to jobID: String) async {
        guard let index = jobs.firstIndex(where: { $0.id == jobID }) else { return }
        switch event {
        case .progress(_, let stage, _, let fraction, let message):
            await update(index) {
                $0.stage = stage
                $0.fraction = fraction
                $0.message = message
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
            await update(index) {
                $0.status = watchdogFired ? .failed : QueuePolicy.finalStatus(done: done, exitStatus: exitStatus)
            }
        }
        await schedule()
    }
}
