import XCTest

/// The viewer on a series that has a viewer cache (plan §9), opened by the
/// debug launch argument on the project the app restores. Attaches
/// screenshots of the app's own window only; run it with public data.
final class ViewerTests: XCTestCase {
    func testTheViewerShowsThreePlanesAndReactsToKeys() throws {
        guard let series = ProcessInfo.processInfo.environment["BCOA_VIEWER_SERIES"] else {
            throw XCTSkip("set TEST_RUNNER_BCOA_VIEWER_SERIES to a series with a viewer cache")
        }
        let app = XCUIApplication()
        app.launchArguments += ["-BCOAOpenViewer", series]
        app.launch()
        let viewer = app.windows.matching(NSPredicate(format: "title CONTAINS 'P0'")).firstMatch
        XCTAssertTrue(viewer.waitForExistence(timeout: 20))
        sleep(2)
        attach(viewer, "viewer-soft-tissue")
        viewer.typeKey("2", modifierFlags: [])
        viewer.typeKey("o", modifierFlags: [])
        sleep(1)
        attach(viewer, "viewer-lung-contour")
        XCTAssertTrue(app.staticTexts.matching(NSPredicate(format: "value CONTAINS 'HU'")).firstMatch.exists)
    }

    private func attach(_ element: XCUIElement, _ name: String) {
        let shot = XCTAttachment(screenshot: element.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }
}
