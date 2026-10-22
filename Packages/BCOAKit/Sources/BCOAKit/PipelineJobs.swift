import Foundation

/// A model the bundle carries, as `Scripts/fetch_models.py` wrote it into
/// `Resources/models/manifest.json`.
public struct BundledModel: Sendable, Equatable, Identifiable, Hashable {
    public var id: String { name }
    public var name: String
    public var labels: [Int: String]
    public var zipSHA256: String?
    public var licence: String?
    /// Models this one runs first (the body composition model crops with the
    /// fast vertebra model); they are bundled but not offered on their own.
    public var requires: [String]

    public init(name: String, labels: [Int: String], zipSHA256: String?, licence: String?, requires: [String]) {
        self.name = name
        self.labels = labels
        self.zipSHA256 = zipSHA256
        self.licence = licence
        self.requires = requires
    }

    /// The models offered to the user: every bundled model that no other
    /// bundled model needs as its helper.
    public static func offered(_ all: [BundledModel]) -> [BundledModel] {
        let helpers = Set(all.flatMap(\.requires))
        return all.filter { !helpers.contains($0.name) }
    }

    /// Two models are run with test-time mirroring, as MOOSE runs them, and
    /// take several times longer (open question 16).
    public var isSlow: Bool { name == "clin_ct_digestive" || name == "clin_ct_body_composition" }

    public static func load(from manifest: URL) -> [BundledModel] {
        guard let data = try? Data(contentsOf: manifest),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let models = object["models"] as? [[String: Any]]
        else { return [] }
        return models.compactMap { entry in
            guard let name = entry["name"] as? String else { return nil }
            let labels = (entry["labels"] as? [String: String] ?? [:]).reduce(into: [Int: String]()) {
                if let id = Int($1.key) { $0[id] = $1.value }
            }
            return BundledModel(
                name: name, labels: labels, zipSHA256: entry["zip_sha256"] as? String,
                licence: entry["licence"] as? String, requires: entry["requires"] as? [String] ?? [])
        }
        .sorted { $0.name < $1.name }
    }
}

/// Payloads of the four pipeline stages (ADR 0031), built from what the
/// project knows when the job is queued; none is stored.
public enum PipelineJobs {
    public static let stages: [JobKind] = [.convert, .segment, .metrics, .viewerCache]

    /// The stage after `kind`, nil after the last.
    public static func next(after kind: JobKind) -> JobKind? {
        guard let index = stages.firstIndex(of: kind), index + 1 < stages.count else { return nil }
        return stages[index + 1]
    }

    public static func work(_ seriesKey: String) -> String { "work/\(seriesKey)" }
    public static func ct(_ seriesKey: String) -> String { "work/\(seriesKey)/ct.nii" }
    public static func labelmap(_ seriesKey: String, model: String) -> String {
        "work/\(seriesKey)/labels/\(model).nii.gz"
    }

    public static func convert(
        seriesKey: String, catalogID: String, partRef: Int, sources: [IndexSource]
    ) -> [String: JSONValue] {
        [
            "series_key": .string(seriesKey),
            "catalog": .string(IndexJobs.catalog),
            "catalog_id": .string(catalogID),
            "part_ref": .number(Double(partRef)),
            "sources": .array(sources.sorted { $0.id < $1.id }.map(\.json)),
        ]
    }

    /// `retry_on_cpu` is off: on a Mac the CPU takes hours for a whole CT
    /// and most of its memory (open question 15).
    public static func segment(seriesKey: String, models: [String], device: String) -> [String: JSONValue] {
        [
            "series_key": .string(seriesKey),
            "input_nifti": .string(ct(seriesKey)),
            "models": .array(models.map { .string($0) }),
            "device": .string(device),
            "retry_on_cpu": .bool(false),
        ]
    }

    public static func metrics(
        seriesKey: String, models: [String], device: String, fallback: Bool, flags: [String]
    ) -> [String: JSONValue] {
        [
            "series_key": .string(seriesKey),
            "ct": .string(ct(seriesKey)),
            "labelmaps": labelmaps(seriesKey, models),
            "devices": .object(Dictionary(uniqueKeysWithValues: models.map {
                ($0, JSONValue.object(["device": .string(device), "fallback": .bool(fallback)]))
            })),
            "flags": .array(flags.map { .string($0) }),
        ]
    }

    public static func viewerCache(seriesKey: String, models: [String]) -> [String: JSONValue] {
        [
            "series_key": .string(seriesKey),
            "ct": .string(ct(seriesKey)),
            "labelmaps": labelmaps(seriesKey, models),
        ]
    }

    private static func labelmaps(_ seriesKey: String, _ models: [String]) -> JSONValue {
        .object(Dictionary(uniqueKeysWithValues: models.map { ($0, .string(labelmap(seriesKey, model: $0))) }))
    }
}

/// The hardware and system a run records (plan §12 provenance).
public enum MachineInfo {
    public static var chip: String {
        sysctlString("machdep.cpu.brand_string") ?? "unknown hardware"
    }

    public static var memoryGB: Int {
        Int((Double(ProcessInfo.processInfo.physicalMemory) / 1_073_741_824).rounded())
    }

    public static var macOS: String {
        let v = ProcessInfo.processInfo.operatingSystemVersion
        return "\(v.majorVersion).\(v.minorVersion).\(v.patchVersion)"
    }

    private static func sysctlString(_ name: String) -> String? {
        var size = 0
        guard sysctlbyname(name, nil, &size, nil, 0) == 0, size > 0 else { return nil }
        var buffer = [UInt8](repeating: 0, count: size)
        guard sysctlbyname(name, &buffer, &size, nil, 0) == 0 else { return nil }
        return String(decoding: buffer.prefix { $0 != 0 }, as: UTF8.self)
    }
}
