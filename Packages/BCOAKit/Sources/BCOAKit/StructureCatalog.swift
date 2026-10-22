import Foundation

/// The structure names of `StructureNames.json` (ADR 0017 section 4): an
/// English name, a short form of at most 18 characters, an anatomical group
/// and an order for every label of every bundled model. The report, the
/// viewer and the lists show these names, never the MOOSE words; the export's
/// columns keep the MOOSE words, because analyses are keyed on them.
public struct StructureCatalog: Sendable {
    public struct Entry: Sendable, Equatable {
        /// The MOOSE word the label id stands for, to check against.
        public var label: String
        public var name: String
        public var short: String
        public var group: String
        public var order: Int
    }

    public let groups: [String]
    public let models: [String: [Int: Entry]]

    /// Decoded by hand and leniently: an entry without a name is left out,
    /// so the report's check finds it missing rather than the whole catalog
    /// failing to load.
    public init?(json data: Data) {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let models = object["models"] as? [String: [String: [String: Any]]]
        else { return nil }
        groups = (object["groups"] as? [[String: Any]] ?? []).compactMap { $0["name"] as? String }
        var decoded: [String: [Int: Entry]] = [:]
        for (model, labels) in models {
            var entries: [Int: Entry] = [:]
            for (id, value) in labels {
                guard let labelID = Int(id), let label = value["label"] as? String,
                      let name = value["name"] as? String, !name.isEmpty
                else { continue }
                entries[labelID] = Entry(
                    label: label, name: name, short: value["short"] as? String ?? name,
                    group: value["group"] as? String ?? "", order: value["order"] as? Int ?? 0)
            }
            decoded[model] = entries
        }
        self.models = decoded
    }

    /// The catalog the app bundles; nil only in a broken build.
    public static func bundled(_ bundle: Bundle = .main) -> StructureCatalog? {
        guard let url = bundle.url(forResource: "StructureNames", withExtension: "json"),
              let data = try? Data(contentsOf: url)
        else { return nil }
        return StructureCatalog(json: data)
    }

    public func entry(model: String, labelID: Int) -> Entry? {
        models[model]?[labelID]
    }

    /// The name to show for a label, with the mechanical name of
    /// `LabelNames` only for a model the catalog does not know.
    public func displayName(model: String, labelID: Int, label: String) -> String {
        if let entry = entry(model: model, labelID: labelID),
           entry.label == label || LabelNames.sanitized(entry.label) == label {
            return entry.name
        }
        return LabelNames.displayName(label)
    }

    /// The longest name of the catalog, which the report's layout must hold.
    public var longestName: String {
        models.values.flatMap(\.values).map(\.name).max { $0.count < $1.count } ?? ""
    }
}
