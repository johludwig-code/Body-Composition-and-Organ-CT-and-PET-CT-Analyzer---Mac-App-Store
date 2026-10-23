import AppKit
import BCOAKit
import BCOAStore
import SwiftUI

/// The export dialog (plan §11): every option of the export engine, with the
/// plan's defaults. The worker writes into `exports/`; the run is locked by
/// the first export (plan §6).
struct ExportView: View {
    @Environment(ProjectSession.self) private var session
    @AppStorage("export.format") private var format = "xlsx"
    @AppStorage("export.layout") private var layout = "wide"
    @AppStorage("export.rowLevel") private var rowLevel = "patient"
    @AppStorage("export.multipleStudies") private var multipleStudies = "suffix"
    @AppStorage("export.multipleSeries") private var multipleSeries = "primary"
    @AppStorage("export.qc") private var qc = "exclude"
    @AppStorage("export.identifiers") private var identifiers = "pseudonym"
    @AppStorage("export.dates") private var dates = "days_since_first"
    @AppStorage("export.methodsText") private var methodsText = true
    @AppStorage("export.masks") private var masks = false
    @AppStorage("export.package") private var package = false
    @AppStorage("export.caseReports") private var caseReports = false
    @State private var stem = ""
    @State private var confirmingIdentifiers = false
    @State private var confirmingDates = false

    var body: some View {
        @Bindable var session = session
        Form {
            Section("Run") {
                if session.runs.isEmpty {
                    Text("Nothing to export yet: analyze series first.").foregroundStyle(.secondary)
                } else {
                    Picker("Run", selection: $session.currentRunID) {
                        ForEach(session.runs) { Text(RunText.title($0)).tag(Optional($0.id)) }
                    }
                    if session.runs.count > 1 {
                        Text("One export holds one run; versions and settings never mix in a table.")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            Section("Table") {
                Picker("Format", selection: $format) {
                    Text("Excel workbook (XLSX)").tag("xlsx")
                    Text("CSV for R and Python (comma, decimal point)").tag("csv")
                    Text("CSV for Excel with European settings (semicolon, decimal comma)").tag("csv_excel")
                }
                Picker("Layout", selection: $layout) {
                    Text("Wide: one row per patient").tag("wide")
                    Text("Long: one row per patient, series, model and structure").tag("long")
                }
                if layout == "wide" {
                    Picker("Rows", selection: $rowLevel) {
                        Text("Patient").tag("patient")
                        Text("Study").tag("study")
                        Text("Series").tag("series")
                    }
                    Picker("Several studies per patient", selection: $multipleStudies) {
                        Text("Columns per time point (__t1, __t2, …)").tag("suffix")
                        Text("First study only").tag("first")
                        Text("Last study only").tag("last")
                        Text("One row per study").tag("per_study")
                    }
                    Picker("Several series per study", selection: $multipleSeries) {
                        Text("Primary series only").tag("primary")
                        Text("All, with columns per series (__s1, __s2, …)").tag("all")
                    }
                }
            }
            Section("Content") {
                Picker("QC", selection: $qc) {
                    Text("Leave out rejected series and excluded structures").tag("exclude")
                    Text("Include everything, with a QC status column").tag("include")
                }
                Picker("Patients identified by", selection: $identifiers) {
                    Text("Pseudonym").tag("pseudonym")
                    Text("Patient ID").tag("patient_id")
                    Text("Pseudonym and patient ID").tag("both")
                }
                Picker("Dates", selection: $dates) {
                    Text("Days since the first study").tag("days_since_first")
                    Text("Year only").tag("year")
                    Text("Full date (ISO 8601)").tag("iso")
                }
                Toggle("Methods text with references", isOn: $methodsText)
                Toggle("Segmentations as NIfTI masks", isOn: $masks)
                Toggle("Reproducibility package for a publication's supplement", isOn: $package)
                    .help("A ZIP with the run's provenance, the long table, labels, QC, the methods text and the model licences; pseudonyms and days only, no images.")
                Toggle("Case report per segmented series (PDF)", isOn: $caseReports)
                    .help("For research documentation: values, QC status, images that show no face, methods and licences. Not a medical report; it does not belong in the patient record.")
                if caseReports {
                    Text("Reports name a case by its pseudonym only, whatever the identifier setting, and leave out rejected series.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
            Section("File") {
                TextField("Name", text: $stem, prompt: Text(defaultStem))
                HStack {
                    Spacer()
                    Button("Show Exports in Finder") {
                        NSWorkspace.shared.activateFileViewerSelecting([session.url.appendingPathComponent("exports")])
                    }
                    Button("Export") {
                        if identifiers != "pseudonym" {
                            confirmingIdentifiers = true
                        } else if caseReports && dates == "iso" {
                            confirmingDates = true
                        } else {
                            export()
                        }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(session.currentRunID == nil || session.isExporting || !validStem)
                }
                if session.isExporting {
                    HStack {
                        ProgressView().controlSize(.small)
                        Text(session.isDrawingReports ? "Drawing case reports…" : "Exporting…")
                    }
                }
                if let last = session.lastExport {
                    Text(last).font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                }
                if !session.lastReports.isEmpty && !session.isExporting {
                    HStack {
                        Text("\(session.lastReports.count) case reports").foregroundStyle(.secondary)
                        Spacer()
                        Button("Save a Copy…") { session.saveReportCopies() }
                        Button("Open in Preview") { session.openReports() }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .confirmationDialog("Export patient IDs?", isPresented: $confirmingIdentifiers) {
            Button("Export with Patient IDs", role: .destructive) {
                if caseReports && dates == "iso" { confirmingDates = true } else { export() }
            }
        } message: {
            Text("The file will hold plain patient IDs. Keep it on an encrypted disk, and never send it outside the institution's rules for identifiable data.")
        }
        // ADR 0017 section 8: the full date only after a warning.
        .confirmationDialog("Case reports with full study dates?", isPresented: $confirmingDates) {
            Button("Export with Full Dates", role: .destructive) { export() }
        } message: {
            Text("A study date with a pseudonym can identify a person. Days since the first study are enough for most research; choose full dates only where the data protection rules of the study allow them.")
        }
    }

    private var defaultStem: String {
        let date = Date().formatted(.iso8601.year().month().day())
        return "\(session.url.deletingPathExtension().lastPathComponent) \(date) \(layout)"
    }

    /// The worker accepts letters, digits, space, dot, dash and underscore.
    private var name: String { stem.trimmingCharacters(in: .whitespaces).isEmpty ? defaultStem : stem }

    private var validStem: Bool {
        name.range(of: #"^[A-Za-z0-9][A-Za-z0-9._ -]{0,99}$"#, options: .regularExpression) != nil
            && !name.hasSuffix(".")
    }

    private func export() {
        guard let run = session.currentRunID else { return }
        let options: [String: JSONValue] = [
            "format": .string(format), "layout": .string(layout), "row_level": .string(rowLevel),
            "multiple_studies": .string(multipleStudies), "multiple_series": .string(multipleSeries),
            "qc": .string(qc), "identifiers": .string(identifiers), "dates": .string(dates),
            "methods_text": .bool(methodsText), "masks": .bool(masks),
            "reproducibility_package": .bool(package), "case_reports": .bool(caseReports),
        ]
        session.export(run: run, stem: name, options: options)
    }
}

extension ProjectSession {
    var isExporting: Bool {
        isDrawingReports || jobs.contains { $0.job.kind == .export && !$0.status.isFinished }
    }

    func export(run: String, stem: String, options: [String: JSONValue]) {
        enqueue(.export, prefix: "export", payload: [
            "run_id": .string(run), "stem": .string(stem), "options": .object(options),
        ])
    }

    /// After an export: the run is locked (plan §6) and the export audited
    /// with its scope, files and their SHA-256 (plan §12).
    func finishExport(_ job: QueuedJob, artifacts: [(path: String, sha256: String)]) {
        guard job.status == .done, let run = job.job.payload["run_id"]?.stringValue else {
            // The code only: the worker's message may quote a file name.
            Self.log.error("export failed: \(job.errorCode ?? "unknown", privacy: .public)")
            lastExport = String(localized: "The export failed: \(ErrorText.text(job.errorCode)).")
            lastReports = []
            return
        }
        if !artifacts.contains(where: { $0.path.hasSuffix("_case_report.pdf") }) { lastReports = [] }
        let reviewer = self.reviewer
        let options = job.job.payload["options"]
        write { db in
            try PipelineStore.lock(run, in: db)
            var details: [String: Any] = [
                "run_id": run, "files": artifacts.map { ["path": $0.path, "sha256": $0.sha256] },
            ]
            if case .object(let object) = options {
                details["options"] = object.compactMapValues { value -> Any? in
                    switch value {
                    case .string(let text): text
                    case .bool(let flag): flag
                    default: nil
                    }
                }
            }
            try IndexStore.audit(db, actor: reviewer, action: "export", object: "run", details: details)
        }
        lastExport = String(localized: "Exported \(artifacts.count) files to exports/: \(artifacts.map(\.path).joined(separator: ", "))")
    }
}
