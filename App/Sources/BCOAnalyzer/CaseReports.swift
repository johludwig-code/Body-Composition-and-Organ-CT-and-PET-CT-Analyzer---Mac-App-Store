import AppKit
import BCOAKit
import BCOAStore
import SwiftUI

/// Case reports (ADR 0017): the worker writes a content file per case as
/// part of an export, the app draws the PDF beside it, and the export's audit
/// entry lists both with their SHA-256 (ADR 0017 section 10).
extension ProjectSession {
    static let structureCatalog = StructureCatalog.bundled()

    var isDrawingReports: Bool { reportsInProgress > 0 }

    /// The reports of one or more patients alone, without tables: the menu
    /// item in the results list.
    func createCaseReports(run: String, pseudonyms: [String], dates: String) {
        let names = Array(Set(pseudonyms)).sorted()
        guard !names.isEmpty else { return }
        let stamp = Date().formatted(.iso8601.year().month().day())
        let stem = names.count == 1 ? "\(names[0]) case report \(stamp)" : "Case reports \(stamp)"
        export(run: run, stem: stem, options: [
            "case_reports": .bool(true), "tables": .bool(false), "methods_text": .bool(false),
            "patients": .array(names.map { .string($0) }), "dates": .string(dates),
        ])
    }

    /// Draws the PDFs off the main actor, then hands everything to the
    /// export's lock and audit.
    func drawReports(for job: QueuedJob, artifacts: [(path: String, sha256: String)]) {
        let contents = artifacts.map(\.path).filter { $0.hasSuffix(".report.json") }
        guard job.status == .done, !contents.isEmpty, let catalog = Self.structureCatalog else {
            if Self.structureCatalog == nil && !contents.isEmpty {
                message = String(localized: "The case reports were not drawn: the structure names are missing from this build.")
            }
            finishExport(job, artifacts: artifacts)
            return
        }
        let project = url
        let renderer = CaseReportRenderer(
            appName: AppIdentity.displayName, appVersion: AppIdentity.shortVersion,
            fonts: ReportFonts.bundled(resources: BundleLayout.main.resources))
        reportsInProgress += 1
        Task {
            let drawn = await Task.detached(priority: .userInitiated) {
                CaseReportBatch.draw(project: project, contents: contents, catalog: catalog, renderer: renderer)
            }.value
            reportsInProgress -= 1
            let pdfs = drawn.compactMap { d in d.pdf.flatMap { pdf in d.sha256.map { (path: pdf, sha256: $0) } } }
            lastReports = pdfs.map { project.appendingPathComponent($0.path) }
            let failed = drawn.filter { $0.error != nil }
            for failure in failed {
                // The content file's name holds the pseudonym and nothing
                // else; the reason is the renderer's, without a path.
                Self.log.error("case report not drawn: \(failure.error ?? "", privacy: .public)")
            }
            finishExport(job, artifacts: artifacts + pdfs)
            if !failed.isEmpty {
                let names = failed.map { ($0.content as NSString).lastPathComponent }.joined(separator: ", ")
                message = String(localized: "\(failed.count) case reports were not drawn (\(names)): \(failed[0].error ?? "")")
            }
        }
    }

    func openReports() {
        guard let first = lastReports.first else { return }
        // Printing happens in Preview (ADR 0017 section 9); the app has no
        // print entitlement.
        let preview = URL(fileURLWithPath: "/System/Applications/Preview.app")
        if FileManager.default.fileExists(atPath: preview.path) {
            NSWorkspace.shared.open(lastReports, withApplicationAt: preview,
                                    configuration: NSWorkspace.OpenConfiguration())
        } else {
            NSWorkspace.shared.open(first)
        }
    }

    /// "Save a Copy…" to a folder the user chooses, with a warning when it
    /// lies on iCloud, a network volume or a removable volume (ADR 0017
    /// section 8): a report is health data under a pseudonym.
    func saveReportCopies() {
        guard !lastReports.isEmpty else { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.prompt = String(localized: "Save Copies Here")
        panel.message = String(localized: "Choose a folder for copies of the case reports.")
        guard panel.runModal() == .OK, let folder = panel.url else { return }
        if let risk = Self.storageRisk(folder) {
            let alert = NSAlert()
            alert.messageText = String(localized: "Save the reports \(risk)?")
            alert.informativeText = String(localized: "Case reports are health data under a pseudonym. Keep them where the study's data protection rules allow, on an encrypted disk.")
            alert.addButton(withTitle: String(localized: "Save Anyway"))
            alert.addButton(withTitle: String(localized: "Cancel"))
            guard alert.runModal() == .alertFirstButtonReturn else { return }
        }
        let access = folder.startAccessingSecurityScopedResource()
        defer { if access { folder.stopAccessingSecurityScopedResource() } }
        var copied = 0
        for report in lastReports {
            let target = folder.appendingPathComponent(report.lastPathComponent)
            try? FileManager.default.removeItem(at: target)
            if (try? FileManager.default.copyItem(at: report, to: target)) != nil { copied += 1 }
        }
        write { db in
            try IndexStore.audit(db, actor: reviewer, action: "copy_reports", object: "exports",
                                 details: ["files": lastReports.map(\.lastPathComponent), "copied": copied,
                                           "destination_kind": Self.storageRisk(folder) ?? "local"])
        }
        message = String(localized: "\(copied) case reports copied.")
    }

    /// Where a folder lies, when that is a place a report should not go
    /// unwarned; nil for a local, fixed disk.
    nonisolated static func storageRisk(_ folder: URL) -> String? {
        let values = try? folder.resourceValues(forKeys: [.volumeIsLocalKey, .volumeIsRemovableKey,
                                                          .volumeIsEjectableKey, .isUbiquitousItemKey])
        if values?.isUbiquitousItem == true || folder.path.contains("/Library/Mobile Documents/") {
            return String(localized: "to iCloud Drive")
        }
        if values?.volumeIsLocal == false { return String(localized: "to a network volume") }
        if values?.volumeIsRemovable == true || values?.volumeIsEjectable == true {
            return String(localized: "to a removable volume")
        }
        return nil
    }
}
