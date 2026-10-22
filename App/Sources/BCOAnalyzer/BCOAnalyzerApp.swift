import AppKit
import BCOAKit
import SwiftUI

@main
struct BCOAnalyzerApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            ContentView()
                .environment(model)
                .frame(minWidth: 960, minHeight: 600)
        }
        .commands {
            CommandGroup(replacing: .appInfo) {
                Button("About \(AppIdentity.displayName)") { model.showAbout = true }
            }
            CommandGroup(after: .newItem) {
                Button("Open or Create Project…") { model.chooseProjectFolder() }
                    .keyboardShortcut("o")
                Button("Add Source Folders…") { model.addSourceFolders() }
                    .keyboardShortcut("a", modifiers: [.command, .shift])
                    .disabled(model.session == nil)
                Divider()
                Button("Remove Identifiers…") { model.showRemoveIdentifiers = true }
                    .disabled(model.session == nil)
            }
        }

        // One window per series, opened from Results & QC (plan §1: the
        // viewer opens as a detail view or a window of its own).
        WindowGroup("Viewer", id: "viewer", for: ViewerRequest.self) { $request in
            if let request {
                ViewerWindow(request: request).environment(model)
            }
        }

        Settings {
            SettingsView()
                .environment(model)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    /// App Review 2.4.5(iii): no process may outlive the app. Every worker
    /// gets SIGTERM, then SIGKILL after ten seconds.
    func applicationWillTerminate(_ notification: Notification) {
        WorkerSupervisor.shared.terminateAllBlocking()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

enum AppIdentity {
    /// Read from the bundle so that renaming the app (ADR 0001) touches
    /// Info.plist and the worker's APP_NAME only.
    static var displayName: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleDisplayName") as? String
            ?? Bundle.main.object(forInfoDictionaryKey: "CFBundleName") as? String
            ?? "BCO Analyzer"
    }

    /// The marketing version alone: what the export's methods text cites.
    static var shortVersion: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    }

    static var version: String {
        let short = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
        let build = Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "0"
        return "\(short) (\(build))"
    }
}
