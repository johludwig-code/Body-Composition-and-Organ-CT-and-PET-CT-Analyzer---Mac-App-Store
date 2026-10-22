import Foundation
import Testing
@testable import BCOAKit

/// Records what the queue hands to a worker and then refuses to start one, so
/// the actor runs end to end without a process: a refused launch ends the job
/// as failed, which is also what `retryFailed` picks up again.
private final class RecordingLauncher: WorkerLauncher, @unchecked Sendable {
    struct Launch: Sendable {
        var written: WorkerJob?
        var fileMode: Int?
        var folderMode: Int?
    }

    struct Refused: Error {}

    private let lock = NSLock()
    private var recorded: [Launch] = []

    var launches: [Launch] { lock.withLock { recorded } }

    func launch(_ job: WorkerJob, jobFile: URL) throws -> WorkerProcess {
        let written = (try? Data(contentsOf: jobFile)).flatMap {
            try? JSONDecoder().decode(WorkerJob.self, from: $0)
        }
        let launch = Launch(
            written: written, fileMode: Self.mode(jobFile),
            folderMode: Self.mode(jobFile.deletingLastPathComponent()))
        lock.withLock { recorded.append(launch) }
        throw Refused()
    }

    private static func mode(_ url: URL) -> Int? {
        let attributes = try? FileManager.default.attributesOfItem(atPath: url.path)
        return (attributes?[.posixPermissions] as? NSNumber)?.intValue
    }
}

/// Stands in for `link_key` and `link_key_id` in `project_meta`.
private actor LinkKeyStore {
    var key = JSONValue.string("base64:AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=")
    var keyID = JSONValue.string("3683c115de125163")

    func removeIdentifiers() {
        key = .null
        keyID = .null
    }
}

private func temporaryProject() -> URL {
    FileManager.default.temporaryDirectory.appendingPathComponent("bcoa-queue-\(UUID().uuidString)")
}

private func waitUntil(_ queue: JobQueue, is status: JobStatus) async throws {
    for _ in 0..<500 {
        if await queue.jobs.first?.status == status { return }
        try await Task.sleep(for: .milliseconds(10))
    }
    Issue.record("the job never became \(status)")
}

@Test func aJobRetriedAfterRemoveIdentifiersRunsWithoutTheKey() async throws {
    // The key and its ID travel in the same payload, so a payload fixed at
    // enqueue stays self-consistent after Remove Identifiers and the worker's
    // link_key_mismatch check cannot catch it; only filling the key when the
    // job starts does (ADR 0024).
    let project = temporaryProject()
    defer { try? FileManager.default.removeItem(at: project) }
    let keys = LinkKeyStore()
    let launcher = RecordingLauncher()
    let queue = JobQueue(
        launcher: launcher, supervisor: WorkerSupervisor(),
        prepare: { job in
            guard job.kind == .index else { return job }
            var prepared = job
            prepared.payload["link_key"] = await keys.key
            prepared.payload["link_key_id"] = await keys.keyID
            return prepared
        })
    await queue.enqueue(WorkerJob(
        jobID: "j_scan", kind: .index, projectDir: project, resourcesDir: project,
        payload: ["mode": .string("scan")]))
    try await waitUntil(queue, is: .failed)

    await keys.removeIdentifiers()
    await queue.retryFailed()
    try await waitUntil(queue, is: .failed)

    let launches = launcher.launches
    try #require(launches.count == 2)
    #expect(launches[0].written?.payload["link_key_id"] == JSONValue.string("3683c115de125163"))
    #expect(launches[1].written?.payload["link_key"] == JSONValue.null)
    #expect(launches[1].written?.payload["link_key_id"] == JSONValue.null)
    // The queued job never holds the key, so nothing that keeps it does either.
    #expect(await queue.jobs.first?.job.payload["link_key"] == nil)
}

@Test func aJobFileIsTheOwnersOnlyAndGoneWhenTheJobEnds() async throws {
    let project = temporaryProject()
    defer { try? FileManager.default.removeItem(at: project) }
    let launcher = RecordingLauncher()
    let queue = JobQueue(launcher: launcher, supervisor: WorkerSupervisor())
    await queue.enqueue(WorkerJob(
        jobID: "j_export", kind: .export, projectDir: project, resourcesDir: project))
    try await waitUntil(queue, is: .failed)

    let launch = try #require(launcher.launches.first)
    #expect(launch.written?.jobID == "j_export")
    #expect(launch.fileMode == 0o600)
    #expect(launch.folderMode == 0o700)
    let file = project.appendingPathComponent("jobs/j_export.json")
    #expect(!FileManager.default.fileExists(atPath: file.path))
}

@Test func leftoverJobFilesAreRemovedWhenAProjectOpens() throws {
    let project = temporaryProject()
    defer { try? FileManager.default.removeItem(at: project) }
    let jobs = project.appendingPathComponent("jobs")
    try FileManager.default.createDirectory(at: jobs, withIntermediateDirectories: true)
    try Data("{}".utf8).write(to: jobs.appendingPathComponent("j_crashed.json"))
    try JobQueue.removeLeftoverJobFiles(projectDir: project)
    #expect(try FileManager.default.contentsOfDirectory(atPath: jobs.path).isEmpty)
    // A project that never ran a job has no such folder; that is not an error.
    try JobQueue.removeLeftoverJobFiles(projectDir: project.appendingPathComponent("never-opened"))
}

/// Records the order of finalize calls and of launches.
private actor Timeline {
    var events: [String] = []
    func add(_ event: String) { events.append(event) }
}

private final class TimelineLauncher: WorkerLauncher, @unchecked Sendable {
    let timeline: Timeline
    init(timeline: Timeline) { self.timeline = timeline }

    func launch(_ job: WorkerJob, jobFile: URL) throws -> WorkerProcess {
        let timeline = timeline
        let id = job.jobID
        Task { await timeline.add("launch \(id)") }
        throw CancellationError()
    }
}

@Test func theFinalizeHookRunsBeforeTheNextJobOfTheGroup() async throws {
    let project = temporaryProject()
    defer { try? FileManager.default.removeItem(at: project) }
    let timeline = Timeline()
    let queue = JobQueue(
        launcher: TimelineLauncher(timeline: timeline), supervisor: WorkerSupervisor(),
        finalize: { finished in
            // Long enough that a second scan would start meanwhile if the
            // queue did not wait for the hook.
            try? await Task.sleep(for: .milliseconds(100))
            await timeline.add("finalize \(finished.id) \(finished.status.rawValue)")
        })
    for id in ["j_one", "j_two"] {
        await queue.enqueue(WorkerJob(
            jobID: id, kind: .index, projectDir: project, resourcesDir: project,
            payload: ["mode": .string("scan")]))
    }
    for _ in 0..<500 {
        if await queue.jobs.allSatisfy({ $0.status == .failed }) { break }
        try await Task.sleep(for: .milliseconds(10))
    }
    try await Task.sleep(for: .milliseconds(50))
    let events = await timeline.events
    #expect(events == ["launch j_one", "finalize j_one failed", "launch j_two", "finalize j_two failed"])
}

/// Counts launches per job and lets each worker "run" until told to stop.
private final class CountingLauncher: WorkerLauncher, @unchecked Sendable {
    private let lock = NSLock()
    private var counts: [String: Int] = [:]

    var launches: [String: Int] { lock.withLock { counts } }

    func launch(_ job: WorkerJob, jobFile: URL) throws -> WorkerProcess {
        lock.withLock { counts[job.jobID, default: 0] += 1 }
        // A real process that ends at once without a word: the job fails,
        // which is all this test needs.
        let worker = WorkerProcess(
            jobID: job.jobID, executable: URL(fileURLWithPath: "/usr/bin/true"), arguments: [],
            environment: [:])
        try worker.start()
        return worker
    }
}

@Test func noJobIsStartedTwiceWhenJobsAreQueuedAtOnce() async throws {
    // onChange suspends the queue at every update; two enqueues racing used
    // to start the same light job twice.
    let project = temporaryProject()
    defer { try? FileManager.default.removeItem(at: project) }
    let launcher = CountingLauncher()
    let queue = JobQueue(
        launcher: launcher, supervisor: WorkerSupervisor(),
        onChange: { _ in try? await Task.sleep(for: .milliseconds(5)) })
    await withTaskGroup(of: Void.self) { group in
        for n in 0..<8 {
            group.addTask {
                await queue.enqueue(WorkerJob(
                    jobID: "j_light_\(n)", kind: .convert, projectDir: project, resourcesDir: project))
            }
        }
    }
    for _ in 0..<500 {
        if await queue.jobs.allSatisfy(\.status.isFinished) { break }
        try await Task.sleep(for: .milliseconds(10))
    }
    #expect(await queue.jobs.allSatisfy(\.status.isFinished))
    #expect(launcher.launches.count == 8)
    #expect(launcher.launches.values.allSatisfy { $0 == 1 }, "\(launcher.launches)")
}
