import XCTest

/// Plan §13: the intended use on the start screen and in About, and the line
/// that the app is not a medical device; none of it a screen to accept
/// (App Review 2.4.5(vi)).
@MainActor
final class NoticeTests: XCTestCase {
    func testTheStartScreenAndAboutStateTheIntendedUse() {
        let app = XCUIApplication()
        app.launchArguments += ["-lastProjectBookmark", ""]
        app.launch()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "value CONTAINS 'Not a medical device'"))
            .firstMatch.waitForExistence(timeout: 10))
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "value BEGINSWITH 'Research software'"))
            .firstMatch.exists)
        XCTAssertFalse(app.buttons["Accept"].exists)
        XCTAssertFalse(app.buttons["I Agree"].exists)
        attach(app.windows.firstMatch, "start")
        app.buttons["About and Licenses"].click()
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "value CONTAINS 'neither made nor endorsed'"))
            .firstMatch.waitForExistence(timeout: 5))
        attach(app.windows.firstMatch, "about")
    }

    private func attach(_ element: XCUIElement, _ name: String) {
        let shot = XCTAttachment(screenshot: element.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }
}
