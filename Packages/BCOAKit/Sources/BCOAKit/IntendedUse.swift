import Foundation

/// The intended-use statement (plan §2 rule 5, §13). It appears in the About
/// window, the onboarding notice, the footer and every export, and is never a
/// screen to accept: App Review 2.4.5(vi) rejects licence or consent screens
/// at launch.
///
/// The exports carry the identical text from `bcoa_worker`; the test
/// `IntendedUseTests` compares both against the plan's wording.
public enum IntendedUse {
    public static let short = "For research use only. Not for clinical use."

    public static let statement = """
        Research software for the automated segmentation and quantification of \
        anatomical structures in CT data. Not intended for diagnosis, treatment \
        planning, or any other clinical decision-making.
        """
}
