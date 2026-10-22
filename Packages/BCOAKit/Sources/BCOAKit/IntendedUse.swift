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

    /// The line plan §13 puts on every page of the case report, the short
    /// notice with the line drawn to a medical device; the footer of every
    /// window carries it too.
    public static let notMedicalDevice = "Not a medical device; not for diagnosis or treatment decisions."
    public static let everyPage = "\(short) \(notMedicalDevice)"

    /// What the user is responsible for, in the About window and the user
    /// guide. Not a licence and not legal advice (plan §14).
    public static let responsibility = """
        Every segmentation and every number is a research measurement that a qualified \
        person must check before it is used. Results do not belong in a patient's record. \
        Ethics approval, data protection and the choice of data are the responsibility of \
        the study's investigators.
        """
}
