import Foundation

/// A source as an index payload lists it (`index_payload.schema.json`).
public struct IndexSource: Sendable, Equatable {
    public enum Status: String, Sendable {
        case active, unavailable, removed
    }

    public var id: Int
    /// The absolute root; only an active source has one.
    public var root: URL?
    public var status: Status
    public var volumeKind: String

    public init(id: Int, root: URL?, status: Status, volumeKind: String) {
        self.id = id
        self.root = status == .active ? root : nil
        self.status = root == nil && status == .active ? .unavailable : status
        self.volumeKind = volumeKind
    }

    var json: JSONValue {
        var object: [String: JSONValue] = [
            "source_id": .number(Double(id)),
            "root": root.map { .string($0.path) } ?? .null,
            "status": .string(status.rawValue),
        ]
        if status == .active { object["volume_kind"] = .string(volumeKind) }
        return .object(object)
    }
}

/// Payloads of index jobs (ADR 0020). Without the link key: the queue's
/// prepare hook adds it when the job starts (ADR 0024), with `withLinkKey`.
public enum IndexJobs {
    public static let catalog = "index/catalog.sqlite"
    public static let previews = "index/previews"

    /// A scan of `scan` among `sources`. The settings are the stored JSON
    /// objects; an empty object takes the worker's defaults key by key.
    public static func scan(
        sources: [IndexSource], scan: [Int], mergedGeneration: Int,
        selection: JSONValue = .object([:]), identity: JSONValue = .object([:]),
        forceReread: Bool = false
    ) -> [String: JSONValue] {
        base(mode: "scan", sources: sources, scan: scan, mergedGeneration: mergedGeneration,
             selection: selection, identity: identity, forceReread: forceReread)
    }

    /// A regroup without a walk: the selection or identity settings changed,
    /// or a source was removed.
    public static func regroup(
        sources: [IndexSource], mergedGeneration: Int,
        selection: JSONValue = .object([:]), identity: JSONValue = .object([:])
    ) -> [String: JSONValue] {
        base(mode: "regroup", sources: sources, scan: [], mergedGeneration: mergedGeneration,
             selection: selection, identity: identity, forceReread: false)
    }

    /// Previews of up to 64 parts of one catalog (ADR 0025). Never a link
    /// key: previews read no identifier.
    public static func previews(
        sources: [IndexSource], catalogID: String, partRefs: [Int], sizePx: Int = 256
    ) -> [String: JSONValue] {
        [
            "mode": .string("previews"),
            "catalog": .string(catalog),
            "previews_dir": .string(previews),
            "sources": .array(sources.sorted { $0.id < $1.id }.map(\.json)),
            "parts": .array(partRefs.prefix(64).map {
                .object(["catalog_id": .string(catalogID), "part_ref": .number(Double($0))])
            }),
            "preview": .object(["size_px": .number(Double(sizePx))]),
        ]
    }

    /// Key and ID go in together or not at all; null after Remove Identifiers.
    public static func withLinkKey(
        _ payload: [String: JSONValue], key: String?, keyID: String?
    ) -> [String: JSONValue] {
        var payload = payload
        if let key, let keyID {
            payload["link_key"] = .string(key)
            payload["link_key_id"] = .string(keyID)
        } else {
            payload["link_key"] = .null
            payload["link_key_id"] = .null
        }
        return payload
    }

    /// The sources a scan of `scan` may walk: those that are active. A
    /// source that is not reachable is listed as unavailable and keeps its
    /// series.
    public static func scannable(_ sources: [IndexSource], among scan: [Int]) -> [Int] {
        let active = Set(sources.filter { $0.status == .active }.map(\.id))
        return scan.filter(active.contains)
    }

    private static func base(
        mode: String, sources: [IndexSource], scan: [Int], mergedGeneration: Int,
        selection: JSONValue, identity: JSONValue, forceReread: Bool
    ) -> [String: JSONValue] {
        [
            "mode": .string(mode),
            "catalog": .string(catalog),
            "previews_dir": .string(previews),
            "sources": .array(sources.sorted { $0.id < $1.id }.map(\.json)),
            "scan_sources": .array(scan.sorted().map { .number(Double($0)) }),
            "force_reread": .bool(forceReread),
            "merged_generation": .number(Double(mergedGeneration)),
            "selection": selection,
            "identity": identity,
            "parts": .array([]),
        ]
    }
}

/// What the app reads from an index job's result (`index_result.schema.json`).
public struct IndexResult: Sendable, Equatable {
    public struct Source: Sendable, Equatable {
        public var state: String
        public var files: Int
        public var badDirs: Int
        public var code: String?
    }

    public var changed: Bool
    public var sources: [Int: Source]
    public var studies: Int?
    public var series: Int?

    public init?(payload: [String: JSONValue]) {
        guard case .bool(let changed) = payload["changed"] else { return nil }
        self.changed = changed
        var sources: [Int: Source] = [:]
        if case .object(let entries) = payload["sources"] {
            for (key, value) in entries {
                guard let id = Int(key), case .string(let state) = value["state"] else { continue }
                sources[id] = Source(
                    state: state, files: value["files"]?.intValue ?? 0,
                    badDirs: value["bad_dirs"]?.intValue ?? 0,
                    code: value["code"]?.stringValue)
            }
        }
        self.sources = sources
        studies = payload["studies"]?.intValue
        series = payload["series"]?.intValue
    }
}

extension JSONValue {
    public var intValue: Int? {
        if case .number(let value) = self, value.rounded() == value { return Int(value) }
        return nil
    }

    public var doubleValue: Double? {
        if case .number(let value) = self { return value }
        return nil
    }

    public var stringValue: String? {
        if case .string(let value) = self { return value }
        return nil
    }

    public var boolValue: Bool? {
        if case .bool(let value) = self { return value }
        return nil
    }
}
