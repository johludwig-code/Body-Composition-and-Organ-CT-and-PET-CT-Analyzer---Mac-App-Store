import AppKit
import BCOAKit
import BCOAStore
import Foundation
import GRDB
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

struct OpenProject {
    let folder: SecurityScopedFolder
    let database: DatabaseQueue
}

@Observable
@MainActor
final class AppModel {
    var selection: SidebarSection? = .sources
    var project: OpenProject?
    var sources: [SecurityScopedFolder] = []
    var jobs: [QueuedJob] = []
    var results: [String: [String: JSONValue]] = [:]
    var lastError: String?
    var showAbout = false

    @ObservationIgnored private var queue: JobQueue!
    @ObservationIgnored private var jobCounter = 0

    init() {
        queue = JobQueue(
            launcher: BundledWorkerLauncher(),
            onChange: { [weak self] job in await self?.upsert(job) },
            onEvent: { [weak self] jobID, event in await self?.handle(event, from: jobID) })
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
        for directory in ["work", "viewer-cache", "exports", "logs", "jobs"] {
            try FileManager.default.createDirectory(
                at: folder.url.appendingPathComponent(directory), withIntermediateDirectories: true)
        }
        project = OpenProject(folder: folder, database: try ProjectStore.open(projectFolder: folder.url))
        sources = try loadSources()
    }

    // MARK: Sources

    func addSourceFolders() {
        guard let project else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = true
        panel.prompt = String(localized: "Add")
        guard panel.runModal() == .OK else { return }
        for url in panel.urls {
            do {
                // Read-only bookmarks: source DICOM is never modified (§8).
                let folder = try SecurityScopedFolder(granted: url, readOnly: true)
                try project.database.write { db in
                    try db.execute(
                        sql: "INSERT INTO sources (bookmark, display_path, added_at) VALUES (?, ?, ?)",
                        arguments: [folder.bookmark, url.lastPathComponent,
                                    ISO8601DateFormatter().string(from: Date())])
                }
                sources.append(folder)
            } catch {
                lastError = String(localized: "A source folder could not be added.")
            }
        }
    }

    private func loadSources() throws -> [SecurityScopedFolder] {
        guard let project else { return [] }
        let bookmarks = try project.database.read { db in
            try Data.fetchAll(db, sql: "SELECT bookmark FROM sources ORDER BY id")
        }
        return bookmarks.compactMap { try? SecurityScopedFolder(bookmark: $0) }
    }

    // MARK: Jobs

    private func nextJobID(_ prefix: String) -> String {
        jobCounter += 1
        let stamp = Int(Date().timeIntervalSince1970)
        return "j_\(prefix)_\(stamp)_\(jobCounter)"
    }

    func runSelfTest(inference: Bool) {
        guard let project else {
            lastError = String(localized: "Open a project first; the self-test writes its log there.")
            return
        }
        var payload: [String: JSONValue] = ["inference": .bool(inference)]
        if inference { payload["inference_model"] = .string("clin_ct_organs") }
        let job = WorkerJob(
            jobID: nextJobID("selftest"), kind: .selftest, projectDir: project.folder.url,
            resourcesDir: BundleLayout.main.resources, payload: payload)
        Task { await queue.enqueue(job) }
    }

    /// Spike S2 (docs/spikes/M0.md): does a worker inherit the sandbox and
    /// the folder grants of the app?
    func runSpikeS2(source: SecurityScopedFolder) {
        guard let project else { return }
        let job = WorkerJob(
            jobID: nextJobID("spike_s2"), kind: .spikeS2, projectDir: project.folder.url,
            resourcesDir: BundleLayout.main.resources,
            payload: ["source_dir": .string(source.url.path)])
        Task { await queue.enqueue(job) }
    }

    func cancel(_ jobID: String) {
        Task { await queue.cancel(jobID: jobID) }
    }

    private func upsert(_ job: QueuedJob) {
        if let index = jobs.firstIndex(where: { $0.id == job.id }) {
            jobs[index] = job
        } else {
            jobs.append(job)
        }
    }

    private func handle(_ event: WorkerEvent, from jobID: String) {
        if case .result(_, let payload) = event {
            results[jobID] = payload
        }
    }
}
