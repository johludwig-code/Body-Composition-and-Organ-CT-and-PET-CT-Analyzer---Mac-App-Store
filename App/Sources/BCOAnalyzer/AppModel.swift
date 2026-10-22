import AppKit
import BCOAKit
import BCOAStore
import Foundation
import Observation

enum SidebarSection: String, CaseIterable, Identifiable {
    case sources, patients, queue, results, export

    var id: String { rawValue }

    var title: LocalizedStringResource {
        switch self {
        case .sources: "Sources"
        case .patients: "Patients & Series"
        case .queue: "Queue"
        case .results: "Results & QC"
        case .export: "Export"
        }
    }

    var systemImage: String {
        switch self {
        case .sources: "folder"
        case .patients: "person.2"
        case .queue: "list.bullet.rectangle"
        case .results: "checkmark.seal"
        case .export: "square.and.arrow.up"
        }
    }
}

/// The app's state above any project: which project is open, and the
/// windows that do not belong to one.
@Observable
@MainActor
final class AppModel {
    var selection: SidebarSection? = .sources
    var session: ProjectSession?
    var lastError: String?
    var showAbout = false

    init() {
        restoreProject()
    }

    // MARK: Project

    func chooseProjectFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = String(localized: "Open Project")
        panel.message = String(localized: """
            Choose or create a folder for the project. Store projects on a FileVault-encrypted disk.
            """)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            let folder = try SecurityScopedFolder(granted: url, readOnly: false)
            try open(folder)
            UserDefaults.standard.set(folder.bookmark, forKey: Self.projectBookmarkKey)
        } catch {
            lastError = String(localized: "The project folder could not be opened.")
        }
    }

    private static let projectBookmarkKey = "lastProjectBookmark"

    private func restoreProject() {
        guard let data = UserDefaults.standard.data(forKey: Self.projectBookmarkKey),
              let folder = try? SecurityScopedFolder(bookmark: data)
        else { return }
        try? open(folder)
    }

    private func open(_ folder: SecurityScopedFolder) throws {
        session = try ProjectSession(folder: folder)
    }

    // MARK: Sources

    func addSourceFolders() {
        guard let session else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = String(localized: "Add")
        // Folder names often carry patient names, and the source's own name is
        // kept in the project (open question 25).
        panel.message = String(localized: """
            Choose folders that contain DICOM or NIfTI. They are only ever read. \
            Prefer the folder above the per-patient folders.
            """)
        guard panel.runModal() == .OK else { return }
        session.addSources(panel.urls)
        selection = .sources
    }

    // MARK: Jobs outside the import

    func runSelfTest(inference: Bool) {
        guard let session else {
            lastError = String(localized: "Open a project first; the self-test writes its log there.")
            return
        }
        var payload: [String: JSONValue] = ["inference": .bool(inference)]
        if inference { payload["inference_model"] = .string("clin_ct_organs") }
        session.enqueue(.selftest, prefix: "selftest", payload: payload)
    }

    /// Spike S2 (docs/spikes/M0.md): does a worker inherit the sandbox and
    /// the folder grants of the app?
    func runSpikeS2(source: SecurityScopedFolder) {
        session?.enqueue(.spikeS2, prefix: "spike_s2", payload: ["source_dir": .string(source.url.path)])
    }
}
