import Foundation

#if canImport(Darwin)
import Darwin
#elseif canImport(Glibc)
import Glibc
#endif

/// One running worker. Its stdout is the protocol; its stderr is closed
/// because the worker sends everything but the protocol to its log file.
///
/// `@unchecked Sendable` because `Process` is not Sendable; every access to
/// it goes through `lock`, and the object is shared between the queue actor
/// and the supervisor, which must be able to terminate it from the main
/// thread while the app quits.
public final class WorkerProcess: @unchecked Sendable {
    public let jobID: String
    private let process = Process()
    private let stdout = Pipe()
    private let lock = NSLock()

    public init(jobID: String, executable: URL, arguments: [String], environment: [String: String]) {
        self.jobID = jobID
        process.executableURL = executable
        process.arguments = arguments
        process.environment = environment
        process.standardOutput = stdout
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
    }

    public func start() throws {
        try lock.withLock { try process.run() }
    }

    public var isRunning: Bool { lock.withLock { process.isRunning } }
    public var processIdentifier: Int32 { lock.withLock { process.processIdentifier } }

    /// Exit status once the process has ended: 0 ok, 1 failed, 130 cancelled,
    /// 2 unusable job file. Nil while it runs.
    public var exitStatus: Int32? {
        lock.withLock { process.isRunning ? nil : process.terminationStatus }
    }

    /// Events in the order the worker sent them. The stream ends when the
    /// worker closes stdout, i.e. when it exits.
    public func events() -> AsyncThrowingStream<WorkerEvent, Error> {
        let handle = stdout.fileHandleForReading
        return AsyncThrowingStream { continuation in
            let reader = Task.detached {
                do {
                    for try await line in handle.bytes.lines where !line.isEmpty {
                        do {
                            continuation.yield(try WorkerEvent.parse(line: line))
                        } catch {
                            // A line that is not protocol means something
                            // wrote to the protocol descriptor past the
                            // worker's redirection. Report it without the
                            // line itself, which may quote patient data.
                            continuation.yield(.log(level: .warning, message: "Ignored a malformed worker line"))
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in reader.cancel() }
        }
    }

    /// SIGTERM now, SIGKILL after `grace`. The worker cleans up its scratch
    /// files on SIGTERM and exits 130.
    public func terminate(grace: Duration = .seconds(10)) async {
        guard isRunning else { return }
        lock.withLock { process.terminate() }
        let deadline = ContinuousClock.now.advanced(by: grace)
        while isRunning, ContinuousClock.now < deadline {
            try? await Task.sleep(for: .milliseconds(100))
        }
        sendKill()
    }

    /// Synchronous variant for `applicationWillTerminate`, where there is no
    /// run loop left to await on.
    public func terminateBlocking(grace: Duration = .seconds(10)) {
        guard isRunning else { return }
        lock.withLock { process.terminate() }
        let deadline = ContinuousClock.now.advanced(by: grace)
        while isRunning, ContinuousClock.now < deadline {
            Thread.sleep(forTimeInterval: 0.1)
        }
        sendKill()
    }

    private func sendKill() {
        lock.withLock {
            if process.isRunning {
                _ = kill(process.processIdentifier, SIGKILL)
            }
        }
    }
}

/// Knows every worker the app started, so that none outlives the app
/// (App Review Guideline 2.4.5(iii)).
public final class WorkerSupervisor: @unchecked Sendable {
    public static let shared = WorkerSupervisor()

    private let lock = NSLock()
    private var workers: [String: WorkerProcess] = [:]

    public init() {}

    public func register(_ worker: WorkerProcess) {
        lock.withLock { workers[worker.jobID] = worker }
    }

    public func unregister(jobID: String) {
        lock.withLock { _ = workers.removeValue(forKey: jobID) }
    }

    public var running: [WorkerProcess] {
        lock.withLock { workers.values.filter(\.isRunning) }
    }

    /// Sends SIGTERM to all at once, then waits for them together, so that
    /// quitting with three workers takes at most one grace period, not three.
    public func terminateAllBlocking(grace: Duration = .seconds(10)) {
        let all = running
        let group = DispatchGroup()
        for worker in all {
            group.enter()
            DispatchQueue.global().async {
                worker.terminateBlocking(grace: grace)
                group.leave()
            }
        }
        group.wait()
    }
}
