import Foundation

/// What the case report checks before it draws anything (ADR 0017 sections 3
/// and 7): every structure has a name and the same MOOSE word as the catalog,
/// every mask has the voxel count the export reports, left and right lie on
/// their sides of the midline, and the level above which no coronal or
/// sagittal image may reach, so that no image shows a face.
public struct ReportAnalysis: Sendable {
    public enum Refusal: Error, Equatable, CustomStringConvertible {
        case unnamed(model: String, label: String)
        case labelMismatch(model: String, labelID: Int, report: String, catalog: String)
        case voxelMismatch(model: String, label: String, report: Int, mask: Int)

        public var description: String {
            switch self {
            case .unnamed(let model, let label):
                "\(label) of \(model) has no name in the catalog"
            case .labelMismatch(let model, let id, let report, let catalog):
                "label \(id) of \(model) is \(report) in the export but \(catalog) in the catalog"
            case .voxelMismatch(let model, let label, let report, let mask):
                "the mask of \(label) (\(model)) has \(mask) voxels, the export \(report)"
            }
        }
    }

    public struct SideCheck: Sendable, Equatable {
        /// Label ids whose centroid lies on the side opposite their name.
        public var wrong: [Int] = []
        /// Within 15 mm of the midline: counted for neither side.
        public var unchecked: [Int] = []
        public var right: Int = 0
        /// Most labels on the wrong side: the model is not shown at all.
        public var hidden: Bool { wrong.count >= 2 && wrong.count > right }
    }

    /// Model → label id → where it lies; empty without a viewer cache.
    public var statistics: [String: [Int: LabelStatistics]] = [:]
    public var geometry: VolumeGeometry?
    /// Structures whose voxel count was compared with its mask.
    public var masksChecked = 0
    /// Segmented models the viewer cache does not hold.
    public var masksMissing: [String] = []
    public var sides: [String: SideCheck] = [:]
    public var midline: String?
    /// The highest slice (k, towards superior) a coronal or sagittal image may
    /// show; nil when no landmark says where the neck is, and then no such
    /// image is drawn.
    public var cutK: Int?
    public var cutLandmark: String?

    public static let sideMargin = 15.0

    public init() {}

    /// The name check alone, which needs no images.
    public static func checkNames(_ content: CaseReportContent, catalog: StructureCatalog) throws {
        for model in content.models {
            for structure in model.structures {
                guard let entry = catalog.entry(model: model.name, labelID: structure.labelID) else {
                    throw Refusal.unnamed(model: model.name, label: structure.label)
                }
                guard LabelNames.sanitized(entry.label) == structure.label else {
                    throw Refusal.labelMismatch(
                        model: model.name, labelID: structure.labelID, report: structure.label, catalog: entry.label)
                }
            }
        }
    }

    public static func analyse(
        _ content: CaseReportContent, catalog: StructureCatalog, volume: ViewerVolume?
    ) throws -> ReportAnalysis {
        try checkNames(content, catalog: catalog)
        var analysis = ReportAnalysis()
        guard let volume, !volume.meta.oblique else { return analysis }
        analysis.geometry = volume.geometry
        analysis.statistics = Self.statistics(volume)

        for model in content.models where model.status == "ok" {
            guard let stats = analysis.statistics[model.name] else {
                analysis.masksMissing.append(model.name)
                continue
            }
            for structure in model.structures where !structure.excluded {
                guard let expected = structure.voxelCount else { continue }
                let found = stats[structure.labelID]?.count ?? 0
                guard found == expected else {
                    throw Refusal.voxelMismatch(model: model.name, label: structure.label, report: expected, mask: found)
                }
                analysis.masksChecked += 1
            }
        }
        analysis.checkSides(content, catalog: catalog)
        analysis.findCut(catalog: catalog)
        return analysis
    }

    /// One pass per model, the models side by side.
    private static func statistics(_ volume: ViewerVolume) -> [String: [Int: LabelStatistics]] {
        final class Results: @unchecked Sendable {
            let lock = NSLock()
            var byModel: [String: [Int: LabelStatistics]] = [:]
        }
        let models = volume.models
        let results = Results()
        DispatchQueue.concurrentPerform(iterations: models.count) { index in
            let stats = volume.labelStatistics(models[index])
            results.lock.lock()
            results.byModel[models[index]] = stats
            results.lock.unlock()
        }
        return results.byModel
    }

    /// The statistics of a label by its MOOSE word, from the first of
    /// `models` that has it.
    public func stats(_ label: String, in models: [String], catalog: StructureCatalog) -> LabelStatistics? {
        for model in models {
            guard let id = catalog.models[model]?.first(where: { $0.value.label == label })?.key,
                  let stats = statistics[model]?[id]
            else { continue }
            return stats
        }
        return nil
    }

    static let vertebraModels = ["clin_ct_vertebrae", "clin_ct_fast_vertebrae"]
    static let lungLabels = [
        "lung_upper_lobe_right", "lung_middle_lobe_right", "lung_lower_lobe_right",
        "lung_upper_lobe_left", "lung_lower_lobe_left",
    ]

    /// The side check of ADR 0017 section 7. The midline is the trunk's
    /// centroid from the body-region model, else the spine's.
    private mutating func checkSides(_ content: CaseReportContent, catalog: StructureCatalog) {
        guard let g = geometry else { return }
        var midlineI: Double?
        if let body = stats("body", in: ["clin_ct_body"], catalog: catalog) {
            midlineI = body.centroid.i
            midline = "the centre of the trunk in the body-region model"
        } else {
            let spine = (1...12).map { "vertebra_T\($0)" } + (1...5).map { "vertebra_L\($0)" }
            let found = spine.compactMap { stats($0, in: Self.vertebraModels, catalog: catalog) }
            if !found.isEmpty {
                midlineI = found.map(\.centroid.i).reduce(0, +) / Double(found.count)
                midline = "the centre of the thoracic and lumbar spine"
            }
        }
        for model in content.models where model.status == "ok" {
            guard let stats = statistics[model.name] else { continue }
            var check = SideCheck()
            var any = false
            for structure in model.structures {
                guard let side = Self.side(of: structure.label), let s = stats[structure.labelID] else { continue }
                any = true
                guard let midlineI else {
                    check.unchecked.append(structure.labelID)
                    continue
                }
                // +x is the patient's left in LPS.
                let offset = (s.centroid.i - midlineI) * g.sx
                if abs(offset) <= Self.sideMargin {
                    check.unchecked.append(structure.labelID)
                } else if (offset > 0) == (side == "left") {
                    check.right += 1
                } else {
                    check.wrong.append(structure.labelID)
                }
            }
            if any { sides[model.name] = check }
        }
    }

    /// "left" or "right" when the MOOSE word names a side.
    public static func side(of label: String) -> String? {
        let words = label.split(separator: "_")
        if words.contains("left") { return "left" }
        if words.contains("right") { return "right" }
        return nil
    }

    /// The face rule of ADR 0017 section 3, as conservative as the landmarks
    /// allow: below the head-and-neck region of the body-region model, the top
    /// of C7 and the top of the lungs, whichever is lowest. A CT with none of
    /// them but abdominal organs starts below the chest, and its images end at
    /// the top of those organs. With no landmark at all, no coronal or
    /// sagittal image is drawn: a fixed slice number cannot know where a face
    /// is.
    private mutating func findCut(catalog: StructureCatalog) {
        var candidates: [(k: Int, landmark: String)] = []
        if let head = stats("head", in: ["clin_ct_body"], catalog: catalog) {
            candidates.append((head.low.k - 1, "below the head and neck region"))
        }
        if let c7 = stats("vertebra_C7", in: Self.vertebraModels, catalog: catalog) {
            candidates.append((c7.high.k, "at the top of the C7 vertebra"))
        }
        let lungs = Self.lungLabels.compactMap { stats($0, in: ["clin_ct_lungs", "clin_ct_organs"], catalog: catalog) }
        if let top = lungs.map(\.high.k).max() {
            candidates.append((top, "at the top of the lungs"))
        }
        if candidates.isEmpty {
            let organs = ["liver", "spleen", "kidney_right", "kidney_left"]
                .compactMap { stats($0, in: ["clin_ct_organs"], catalog: catalog) }
            if let top = organs.map(\.high.k).max() {
                candidates.append((top, "at the top of the abdominal organs"))
            }
        }
        guard let lowest = candidates.min(by: { $0.k < $1.k }), lowest.k >= 0 else { return }
        cutK = min(lowest.k, (geometry?.nz ?? 1) - 1)
        cutLandmark = lowest.landmark
    }
}
