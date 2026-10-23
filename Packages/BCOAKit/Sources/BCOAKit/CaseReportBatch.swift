import CryptoKit
import Foundation

/// The PDFs of one export: every content file the worker announced is drawn
/// beside itself, from the run's viewer cache (ADR 0017 section 10). A
/// report that cannot be drawn names its reason and stops no other.
public enum CaseReportBatch {
    public struct Drawn: Sendable, Equatable {
        /// Project-relative, as the worker announced it.
        public var content: String
        public var pdf: String?
        public var sha256: String?
        public var pages: Int
        public var error: String?
    }

    /// The pattern a report's file name must match before anything is
    /// written under it (ADR 0017 section 8).
    public static let pdfPattern = #"^P\d{4,}_t\d+_s\d+_case_report\.pdf$"#

    public static func draw(
        project: URL, contents: [String], catalog: StructureCatalog, renderer: CaseReportRenderer
    ) -> [Drawn] {
        contents.map { path in
            var drawn = Drawn(content: path, pdf: nil, sha256: nil, pages: 0, error: nil)
            do {
                // Only below exports/: the path came from the worker, and the
                // app writes nothing anywhere else on its word.
                let file = project.appendingPathComponent(path).standardizedFileURL
                let exports = project.appendingPathComponent("exports").standardizedFileURL.path + "/"
                guard file.path.hasPrefix(exports), path.hasSuffix(".report.json") else {
                    throw CaseReportRenderer.RenderError.pdf("not a report of this project's exports")
                }
                let content = try CaseReportContent(json: Data(contentsOf: file))
                guard content.pdfName.range(of: pdfPattern, options: .regularExpression) != nil else {
                    throw CaseReportRenderer.RenderError.pdf("not a report name: \(content.pdfName)")
                }
                let volume = viewerCache(project: project, content: content)
                let target = file.deletingLastPathComponent().appendingPathComponent(content.pdfName)
                let output = try renderer.render(content, catalog: catalog, volume: volume, to: target)
                drawn.pages = output.pageCount
                drawn.pdf = (path as NSString).deletingLastPathComponent + "/" + content.pdfName
                drawn.sha256 = SHA256.hash(data: try Data(contentsOf: target))
                    .map { String(format: "%02x", $0) }.joined()
            } catch {
                drawn.error = "\(error)"
            }
            return drawn
        }
    }

    /// The run's own cache, else the one a project from before per-run
    /// caches kept per series; the mask check refuses one that belongs to
    /// another run.
    public static func viewerCache(project: URL, content: CaseReportContent) -> ViewerVolume? {
        for folder in [PipelineJobs.viewerCacheFolder(content.seriesKey, run: content.runID),
                       "viewer-cache/\(content.seriesKey)"] {
            let url = project.appendingPathComponent(folder)
            if FileManager.default.fileExists(atPath: url.appendingPathComponent("meta.json").path),
               let volume = try? ViewerVolume(folder: url) {
                return volume
            }
        }
        return nil
    }
}
