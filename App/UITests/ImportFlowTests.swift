import XCTest

/// The import as a person does it (plan §7): a project folder and a source
/// folder chosen in the Open panel, the scan, the tree with the automatic
/// selection. The folders come from the environment (`TEST_RUNNER_BCOA_PROJECT`,
/// `TEST_RUNNER_BCOA_SOURCE`), so the test holds no path and no data; run it
/// with public or synthetic DICOM only.
final class ImportFlowTests: XCTestCase {
    override func setUp() {
        continueAfterFailure = false
    }

    func testAFolderIsIndexedAndItsSeriesAreSelected() throws {
        let environment = ProcessInfo.processInfo.environment
        guard let project = environment["BCOA_PROJECT"], let source = environment["BCOA_SOURCE"] else {
            throw XCTSkip("set TEST_RUNNER_BCOA_PROJECT and TEST_RUNNER_BCOA_SOURCE")
        }
        let app = XCUIApplication()
        // No project restored from an earlier run.
        app.launchArguments += ["-lastProjectBookmark", ""]
        app.launch()

        app.buttons["Open or Create Project…"].firstMatch.click()
        choose(project, in: app)
        XCTAssertTrue(app.staticTexts["Source Folders"].waitForExistence(timeout: 20))

        app.typeKey("a", modifierFlags: [.command, .shift])
        choose(source, in: app)

        // The scan is queued as soon as the source is added.
        let indexed = app.staticTexts.matching(NSPredicate(format: "value BEGINSWITH 'Indexed'")).firstMatch
        XCTAssertTrue(indexed.waitForExistence(timeout: 180))
        attach(app, "sources")

        app.staticTexts["Patients & Series"].firstMatch.click()
        XCTAssertTrue(app.staticTexts["P0001"].waitForExistence(timeout: 20))
        // Open every row, so the screenshot shows the series and their status.
        app.outlines.firstMatch.click()
        app.typeKey("a", modifierFlags: .command)
        app.typeKey(XCUIKeyboardKey.rightArrow, modifierFlags: .option)
        attach(app, "patients")
        XCTAssertTrue(
            app.staticTexts.matching(NSPredicate(format: "value == 'Selected automatically'")).firstMatch
                .waitForExistence(timeout: 10))
    }

    /// Go to `path` in the frontmost Open panel and choose that folder.
    private func choose(_ path: String, in app: XCUIApplication) {
        sleep(2)
        app.typeKey("g", modifierFlags: [.command, .shift])
        sleep(1)
        app.typeText(path)
        sleep(1)
        app.typeKey(XCUIKeyboardKey.return, modifierFlags: [])
        sleep(2)
        app.typeKey(XCUIKeyboardKey.return, modifierFlags: [])
        sleep(2)
    }

    private func attach(_ app: XCUIApplication, _ name: String) {
        let shot = XCTAttachment(screenshot: app.screenshot())
        shot.name = name
        shot.lifetime = .keepAlways
        add(shot)
    }
}
