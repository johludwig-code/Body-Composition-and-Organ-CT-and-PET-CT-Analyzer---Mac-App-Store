import Foundation

/// The case report's fixed texts and number format (ADR 0017 sections 5, 6
/// and 8). The report is always in US English, whatever the app's or the
/// system's language, so these are plain literals and never go through a
/// String Catalog that a translation could reach.
public enum ReportText {
    public static let notInRecord = "This document does not belong in the patient record."
    public static let checkEveryValue = """
        Every value is an automated research measurement that a qualified person must check \
        against the images before it is used.
        """

    /// The release of the weights in the archives' download URLs; a test
    /// holds it to `Models/manifest.lock.json`.
    public static let weightsRelease = "moosez-v.3.1.3"

    public static let referencesIntro = """
        Segmentations were produced with MOOSE. If you use these results, please cite the three \
        works the MOOSE authors ask for:
        """

    /// The three works in the order the approved page prints them; "18F"
    /// stands as the renderer sets it, with the 18 raised.
    public static let references = [
        """
        [1] Shiyam Sundar LK, Yu J, Muzik O, et al. Fully automated, semantic segmentation of \
        whole-body 18F-FDG PET/CT images based on data-centric artificial intelligence. \
        J Nucl Med. 2022;63(12):1941-1948. doi:10.2967/jnumed.122.264063
        """,
        """
        [2] Ferrara D, Pires M, Gutschmayer S, et al. Sharing a whole-/total-body [18F]FDG-PET/CT \
        dataset with CT-derived segmentations: an ENHANCE.PET initiative. Sci Data. 2026. \
        doi:10.1038/s41597-026-07218-y
        """,
        """
        [3] Isensee F, Jaeger PF, Kohl SAA, Petersen J, Maier-Hein KH. nnU-Net: a self-configuring \
        method for deep learning-based biomedical image segmentation. Nat Methods. \
        2021;18(2):203-211. doi:10.1038/s41592-020-01008-z
        """,
    ]

    /// ADR 0017 section 6, with MOOSE's version from the run's provenance.
    /// The URL after "CC BY 4.0" is part of the attribution CC BY 4.0
    /// §3(a)(1)(C) asks for and must stay.
    public static func licences(moosez: String) -> String {
        """
        MOOSE \(moosez) (package moosez), source: https://github.com/ENHANCE-PET/MOOSE. MOOSE \
        code: Apache License 2.0 (https://www.apache.org/licenses/LICENSE-2.0). Model weights \
        © the MOOSE authors (release \(weightsRelease)), licensed under CC BY 4.0 \
        (https://creativecommons.org/licenses/by/4.0/), provided as is, without warranties. \
        Modified for this app: optimizer and training state removed from each checkpoint; \
        network weights unchanged. nnU-Net v2 (package nnunetv2) is licensed under the Apache \
        License 2.0.
        """
    }

    public static let definitions: [(term: String, text: String)] = [
        ("Volume (mL)", "Number of voxels in the segmentation mask times the volume of one voxel, in millilitres."),
        ("Mean ± SD (HU)", "Mean and standard deviation of the CT values inside the mask, in Hounsfield units."),
        ("Median (HU)", "Median of the CT values inside the mask, in Hounsfield units."),
        ("Empty cell", "No value: the note beside it, or the section, says why."),
        ("L3 level", "Body composition is measured only in a slab of slices at the third lumbar vertebra, as the MOOSE body composition model defines it; it is not a whole-body value."),
        ("Time point", "Studies of the case in the order of their dates, 1 being the first."),
    ]

    /// The worker's note for an empty L6 vertebra or 13th rib.
    public static let accessoryNote = "not found, an accessory structure that only some people have"

    /// The QC flags in plain words, as the notes beside the values use them.
    public static let flags: [(flag: String, text: String)] = [
        ("cut off at the image edge, volume incomplete", "The mask touches the border of the image, so part of the structure lies outside the scanned volume."),
        ("not found in this CT", "The model found no voxel of this structure; it may lie outside the scanned volume."),
        (accessoryNote, "An L6 vertebra or a 13th rib exists only in some people; its absence is anatomy."),
        ("volume unusual for this project; check the mask", "The volume lies far from the other series of this project (robust z-score above 3.5); this says nothing about the person, only that the mask deserves a look."),
        ("segmentation resampled to the CT grid", "The model's output had another grid than the CT and was resampled with nearest neighbour before measuring."),
        ("excluded in QC", "A reviewer excluded the structure; its values are left out."),
    ]

    /// The QC status as the footer and page 1 state it.
    public static func qc(status: String, reviewed: Bool) -> String {
        switch status {
        case "accepted": "accepted in visual review"
        case "rejected": "rejected in visual review"
        default: reviewed ? "partly reviewed (structures excluded), not accepted" : "not visually reviewed"
        }
    }

    public static func modelStatus(_ status: String) -> String {
        switch status {
        case "ok": "segmented"
        case "failed": "failed"
        case "excluded": "rejected in QC"
        default: "did not run"
        }
    }

    public static func modelName(_ key: String) -> String {
        switch key {
        case "organs": "Organs"
        case "lungs": "Lungs"
        case "cardiac": "Heart and vessels"
        case "digestive": "Digestive tract"
        case "muscles": "Muscles"
        case "vertebrae": "Vertebrae"
        case "fast_vertebrae": "Vertebrae (fast)"
        case "ribs": "Ribs and sternum"
        case "peripheral_bones": "Peripheral bones"
        case "body": "Body regions"
        case "body_composition": "Body composition"
        default: LabelNames.displayName(key)
        }
    }

    /// The time point as the report states it, in the form the export chose
    /// (ADR 0017 section 8): days by default, the year or the date only when
    /// the export asked for them.
    public static func timepoint(_ content: CaseReportContent) -> String {
        let value = content.dateValue
        switch content.dateKind {
        case "year": return "\(content.timepoint) (\(value ?? "year unknown"))"
        case "iso": return "\(content.timepoint) (\(value ?? "date unknown"))"
        default: return "\(content.timepoint) (\(value.map { "day \($0)" } ?? "day unknown"))"
        }
    }

    // -- numbers ---------------------------------------------------------------

    private static let posix = Locale(identifier: "en_US_POSIX")

    /// One decimal, a decimal point and no thousands separator, whatever the
    /// system region (ADR 0017 section 5).
    public static func number(_ value: Double?, decimals: Int = 1) -> String {
        guard let value, value.isFinite else { return "" }
        // Half away from zero, as a spreadsheet rounds the same cell: printf
        // alone would turn 54.25 into 54.2.
        let scale = pow(10, Double(decimals))
        let rounded = (value * scale).rounded(.toNearestOrAwayFromZero) / scale
        let text = String(format: "%.\(decimals)f", locale: posix, rounded)
        return text == "-0.0" || text == "-0" ? String(text.dropFirst()) : text
    }

    public static func meanSD(_ mean: Double?, _ sd: Double?) -> String {
        guard let mean else { return "" }
        guard let sd else { return number(mean) }
        return "\(number(mean)) ± \(number(sd))"
    }
}
