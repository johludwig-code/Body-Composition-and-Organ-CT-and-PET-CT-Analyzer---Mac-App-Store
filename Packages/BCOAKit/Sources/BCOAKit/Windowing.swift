import Foundation

/// CT window presets in Hounsfield units (plan §9). Keys 1–6 select them in
/// the order listed.
public struct WindowPreset: Equatable, Sendable, Identifiable {
    public let id: String
    public let name: String
    public let width: Double
    public let center: Double

    public static let softTissue = WindowPreset(id: "soft_tissue", name: "Soft Tissue", width: 400, center: 40)
    public static let lung = WindowPreset(id: "lung", name: "Lung", width: 1500, center: -600)
    public static let bone = WindowPreset(id: "bone", name: "Bone", width: 1800, center: 400)
    public static let liver = WindowPreset(id: "liver", name: "Liver", width: 150, center: 30)
    public static let brain = WindowPreset(id: "brain", name: "Brain", width: 80, center: 40)
    public static let mediastinum = WindowPreset(id: "mediastinum", name: "Mediastinum", width: 350, center: 50)

    public static let all: [WindowPreset] = [softTissue, lung, bone, liver, brain, mediastinum]

    /// Default preset per MOOSE model, so QC of ribs opens in a bone window.
    public static func defaultPreset(forModel model: String) -> WindowPreset {
        switch model {
        case "clin_ct_ribs", "clin_ct_vertebrae", "clin_ct_peripheral_bones", "clin_ct_fast_vertebrae":
            bone
        case "clin_ct_lungs":
            lung
        case "clin_ct_cardiac":
            mediastinum
        default:
            softTissue
        }
    }
}

public enum Windowing {
    /// Linear window to 8 bit: `center − width/2` maps to 0 and
    /// `center + width/2` to 255, clamped outside.
    @inlinable
    public static func gray(_ hu: Double, width: Double, center: Double) -> UInt8 {
        let lower = center - width / 2
        let scaled = (hu - lower) / width * 255
        return UInt8(max(0, min(255, scaled.rounded())))
    }

    public static func apply(_ values: [Int16], width: Double, center: Double) -> [UInt8] {
        values.map { gray(Double($0), width: width, center: center) }
    }
}

/// `kidney_left` → "Kidney left"; must agree with
/// `bcoa_worker.naming.display_name`, and both are tested on the same names.
public enum LabelNames {
    public static func displayName(_ label: String) -> String {
        let words = label.replacingOccurrences(of: "-", with: "_")
            .split(separator: "_").map(String.init)
        guard let first = words.first else { return label }
        let capitalised = first.prefix(1).uppercased() + String(first.dropFirst())
        return ([capitalised] + Array(words.dropFirst())).joined(separator: " ")
    }
}
