import XCTest

/// The launch path a reviewer takes: the app opens without a licence or
/// consent screen (App Review 2.4.5(vi)) and shows the research notice.
final class LaunchTests: XCTestCase {
    func testLaunchShowsResearchNoticeWithoutAConsentScreen() {
        let app = XCUIApplication()
        app.launch()
        XCTAssertTrue(app.staticTexts["researchNotice"].waitForExistence(timeout: 10))
        XCTAssertFalse(app.buttons["Accept"].exists)
        XCTAssertFalse(app.buttons["I Agree"].exists)
    }
}
