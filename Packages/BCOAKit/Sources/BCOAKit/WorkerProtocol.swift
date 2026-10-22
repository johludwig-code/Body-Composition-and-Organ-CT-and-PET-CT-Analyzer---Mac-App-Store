import Foundation

/// Protocol version 1 between the app and the Python worker.
///
/// The JSON schemas in `/Protocol/schemas` are the contract. Every fixture in
/// `/Protocol/fixtures` must decode here and in `bcoa_worker.protocol`; a
/// change on one side without the other fails that side's tests.
public enum WorkerProtocol {
    public static let version = 1
}

public enum WorkerStage: String, Codable, Sendable, CaseIterable {
    case index, convert, check, segment, metrics
    case viewerCache = "viewer_cache"
    case export, selftest
}

public enum JobKind: String, Codable, Sendable, CaseIterable {
    case index, convert, segment, metrics
    case viewerCache = "viewer_cache"
    case export, selftest
    case spikeS2 = "spike_s2"

    /// Segmentation holds the GPU; two at once only slow each other down and
    /// can exhaust unified memory on a 16 GB Mac.
    public var needsExclusiveGPU: Bool { self == .segment || self == .selftest }
}

public enum LogLevel: String, Codable, Sendable {
    case debug, info, warning, error
}

public enum ArtifactKind: String, Codable, Sendable {
    case nifti, labelmap
    case viewerCache = "viewer_cache"
    case metrics, export, log, report, preview
}

/// Progress as numbers, sent by index jobs (ADR 0020). The app renders its own
/// localized text from it, so the event's `message` can stay English and
/// serve the log only.
public struct ProgressDetail: Codable, Sendable, Equatable {
    public enum Phase: String, Codable, Sendable, CaseIterable {
        case walk, read, group, previews
    }

    public var phase: Phase
    public var done: Int
    /// Nil while the total is not known yet, as during the walk.
    public var total: Int?

    public init(phase: Phase, done: Int, total: Int?) {
        self.phase = phase
        self.done = done
        self.total = total
    }
}

public enum DoneStatus: String, Codable, Sendable {
    case ok, failed, cancelled
}

/// Any JSON value. `result` payloads are structured per job kind and are
/// interpreted by the code that started the job, not by the protocol layer.
public enum JSONValue: Codable, Sendable, Equatable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    public subscript(key: String) -> JSONValue? {
        if case .object(let object) = self { return object[key] }
        return nil
    }
}

public enum WorkerEvent: Decodable, Sendable, Equatable {
    case hello(protocolVersion: Int, versions: [String: String])
    // `detail` defaults to nil: only index jobs send it, and every other
    // producer and test of a progress event keeps its shape.
    case progress(
        jobID: String, stage: WorkerStage, model: String?, fraction: Double?, message: String,
        detail: ProgressDetail? = nil)
    case heartbeat(jobID: String, timestamp: String)
    case log(level: LogLevel, message: String)
    case artifact(kind: ArtifactKind, path: String, sha256: String)
    case result(jobID: String, payload: [String: JSONValue])
    case error(code: String, message: String, recoverable: Bool)
    case done(jobID: String, status: DoneStatus)

    private enum Key: String, CodingKey {
        case type, protocol_version, versions, job_id, stage, model, fraction, message, detail
        case ts, level, kind, path, sha256, payload, code, recoverable, status
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: Key.self)
        let type = try c.decode(String.self, forKey: .type)
        switch type {
        case "hello":
            self = .hello(
                protocolVersion: try c.decode(Int.self, forKey: .protocol_version),
                versions: try c.decode([String: String].self, forKey: .versions))
        case "progress":
            // `fraction` is required and may be null: null means "not
            // measurable", which the UI shows as stage progress instead.
            guard c.contains(.fraction) else {
                throw DecodingError.keyNotFound(Key.fraction, .init(
                    codingPath: c.codingPath, debugDescription: "progress without fraction"))
            }
            let fraction = try c.decodeIfPresent(Double.self, forKey: .fraction)
            if let fraction, !(0...1).contains(fraction) {
                throw DecodingError.dataCorruptedError(
                    forKey: .fraction, in: c, debugDescription: "fraction out of range")
            }
            self = .progress(
                jobID: try c.decode(String.self, forKey: .job_id),
                stage: try c.decode(WorkerStage.self, forKey: .stage),
                model: try c.decodeIfPresent(String.self, forKey: .model),
                fraction: fraction,
                message: try c.decode(String.self, forKey: .message),
                detail: try c.decodeIfPresent(ProgressDetail.self, forKey: .detail))
        case "heartbeat":
            self = .heartbeat(
                jobID: try c.decode(String.self, forKey: .job_id),
                timestamp: try c.decode(String.self, forKey: .ts))
        case "log":
            self = .log(
                level: try c.decode(LogLevel.self, forKey: .level),
                message: try c.decode(String.self, forKey: .message))
        case "artifact":
            self = .artifact(
                kind: try c.decode(ArtifactKind.self, forKey: .kind),
                path: try c.decode(String.self, forKey: .path),
                sha256: try c.decode(String.self, forKey: .sha256))
        case "result":
            self = .result(
                jobID: try c.decode(String.self, forKey: .job_id),
                payload: try c.decode([String: JSONValue].self, forKey: .payload))
        case "error":
            self = .error(
                code: try c.decode(String.self, forKey: .code),
                message: try c.decode(String.self, forKey: .message),
                recoverable: try c.decode(Bool.self, forKey: .recoverable))
        case "done":
            self = .done(
                jobID: try c.decode(String.self, forKey: .job_id),
                status: try c.decode(DoneStatus.self, forKey: .status))
        default:
            throw DecodingError.dataCorruptedError(
                forKey: .type, in: c, debugDescription: "unknown event type \(type)")
        }
    }

    public static func parse(line: some StringProtocol) throws -> WorkerEvent {
        try JSONDecoder().decode(WorkerEvent.self, from: Data(line.utf8))
    }
}

/// The job file the app writes before starting a worker. Every path the
/// worker touches comes from here.
public struct WorkerJob: Codable, Sendable, Equatable {
    public var protocolVersion: Int
    public var jobID: String
    public var kind: JobKind
    public var projectDir: String
    public var logPath: String
    public var resourcesDir: String
    public var heartbeatSeconds: Double
    public var payload: [String: JSONValue]

    private enum CodingKeys: String, CodingKey {
        case protocolVersion = "protocol_version"
        case jobID = "job_id"
        case kind
        case projectDir = "project_dir"
        case logPath = "log_path"
        case resourcesDir = "resources_dir"
        case heartbeatSeconds = "heartbeat_seconds"
        case payload
    }

    public init(
        jobID: String, kind: JobKind, projectDir: URL, resourcesDir: URL,
        heartbeatSeconds: Double = 10, payload: [String: JSONValue] = [:]
    ) {
        self.protocolVersion = WorkerProtocol.version
        self.jobID = jobID
        self.kind = kind
        self.projectDir = projectDir.path
        self.logPath = projectDir.appendingPathComponent("logs/\(jobID).log").path
        self.resourcesDir = resourcesDir.path
        self.heartbeatSeconds = heartbeatSeconds
        self.payload = payload
    }

    // Written by hand: a synthesised decoder throws on a missing key even
    // when the property has a default, and `heartbeat_seconds` is optional in
    // the schema.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        protocolVersion = try c.decode(Int.self, forKey: .protocolVersion)
        jobID = try c.decode(String.self, forKey: .jobID)
        kind = try c.decode(JobKind.self, forKey: .kind)
        projectDir = try c.decode(String.self, forKey: .projectDir)
        logPath = try c.decode(String.self, forKey: .logPath)
        resourcesDir = try c.decode(String.self, forKey: .resourcesDir)
        heartbeatSeconds = try c.decodeIfPresent(Double.self, forKey: .heartbeatSeconds) ?? 10
        payload = try c.decode([String: JSONValue].self, forKey: .payload)
    }
}
