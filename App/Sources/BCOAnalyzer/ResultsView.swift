import BCOAKit
import BCOAStore
import SwiftUI

/// The runs of the project and where each series stands in them (plan §8,
/// §12). QC and the viewer start from here.
struct ResultsView: View {
    @Environment(ProjectSession.self) private var session
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow
    @State private var analyzing = false
    @State private var selection: Set<String> = []
    @State private var mode = "series"

    var body: some View {
        @Bindable var session = session
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Results & QC").font(.title2)
                if !session.runs.isEmpty {
                    Picker("Run", selection: $session.currentRunID) {
                        ForEach(session.runs) { run in
                            Text(RunText.title(run)).tag(Optional(run.id))
                        }
                    }
                    .fixedSize()
                    .onChange(of: session.currentRunID) { session.reload() }
                }
                Picker("View", selection: $mode) {
                    Text("Series").tag("series")
                    Text("Cohort").tag("cohort")
                }
                .pickerStyle(.segmented).fixedSize()
                Spacer()
                Button("Review Unreviewed (\(unreviewed.count))") { review() }
                    .disabled(unreviewed.isEmpty)
                    .help("Opens the viewer on each series without a QC decision, one after another.")
                Button("Retry Failed") { session.retryFailedStages() }
                    .disabled(!session.progress.contains { $0.hasFailure })
                Button("Analyze…") { analyzing = true }
                    .buttonStyle(.borderedProminent)
                    .disabled(session.bundledModels.isEmpty)
            }
            .padding()
            if session.bundledModels.isEmpty {
                Label("This build carries no models. Run make models before building the app.",
                      systemImage: "exclamationmark.triangle")
                    .padding(.horizontal)
            }
            if mode == "cohort", !session.progress.isEmpty {
                CohortView()
            } else if session.progress.isEmpty {
                ContentUnavailableView(
                    "No Results Yet",
                    systemImage: "checkmark.seal",
                    description: Text("Select series under Patients & Series, then choose Analyze…"))
            } else {
                Table(session.progress, selection: $selection) {
                    TableColumn("Patient") { Text($0.pseudonym).fontWeight(.medium) }
                        .width(70)
                    TableColumn("Study") { Text($0.studyDate ?? "") }
                        .width(90)
                    TableColumn("Series") { Text($0.description ?? "—") }
                    TableColumn("Convert") { StageCell(state: $0.stages["convert"]) }.width(64)
                    TableColumn("Segment") { StageCell(state: $0.stages["segment"]) }.width(64)
                    TableColumn("Metrics") { StageCell(state: $0.stages["metrics"]) }.width(64)
                    TableColumn("Viewer") { StageCell(state: $0.stages["viewer_cache"]) }.width(64)
                    TableColumn("Structures") { series in
                        Text(series.modelsWithResults.map(RunText.modelName).joined(separator: ", "))
                            .foregroundStyle(.secondary)
                    }
                    TableColumn("Flags") { series in
                        FlagsCell(flags: series.flags)
                    }
                    .width(50)
                    TableColumn("QC") { series in
                        Text(series.qcStatus.flatMap(QCStore.Status.init(rawValue:)).map(QCText.status)
                             ?? QCText.status(.unreviewed))
                            .foregroundStyle(series.qcStatus == "rejected" ? .red : .secondary)
                    }
                    .width(110)
                }
                .contextMenu(forSelectionType: String.self) { keys in
                    Button("Open Viewer") { keys.forEach(open) }
                        .disabled(keys.contains { key in
                            session.progress.first { $0.seriesKey == key }?.stages["viewer_cache"]?.status != "done"
                        })
                } primaryAction: { keys in
                    keys.forEach(open)
                }
            }
            Divider()
            Text(summary).font(.callout).foregroundStyle(.secondary)
                .padding(.horizontal).padding(.vertical, 6)
        }
        .sheet(isPresented: $analyzing) { AnalyzeSheet().environment(session) }
    }

    private var unreviewed: [String] {
        session.progress.filter {
            $0.stages["viewer_cache"]?.status == "done" && ($0.qcStatus == nil || $0.qcStatus == "unreviewed")
        }.map(\.seriesKey)
    }

    private func review() {
        guard let run = session.currentRunID, let first = unreviewed.first else { return }
        openWindow(id: "viewer", value: ViewerRequest(runID: run, seriesKey: first, reviewQueue: true))
    }

    /// Opens the viewer for a series whose cache exists; a double click on
    /// any other row does nothing.
    private func open(_ key: String) {
        guard let run = session.currentRunID,
              session.progress.first(where: { $0.seriesKey == key })?.stages["viewer_cache"]?.status == "done"
        else { return }
        openWindow(id: "viewer", value: ViewerRequest(runID: run, seriesKey: key))
    }

    private var summary: String {
        let done = session.progress.filter { $0.stages["viewer_cache"]?.status == "done" }.count
        let failed = session.progress.filter(\.hasFailure).count
        return String(localized: "\(session.progress.count) series in this run, \(done) ready for QC, \(failed) failed")
    }
}

extension PipelineStore.SeriesProgress {
    var hasFailure: Bool {
        stages.values.contains { ["failed", "interrupted", "cancelled"].contains($0.status) }
    }
}

private struct StageCell: View {
    let state: (status: String, error: String?)?

    var body: some View {
        switch state?.status {
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            .accessibilityLabel(Text("Done"))
        case "running": ProgressView().controlSize(.small)
        case "queued": Image(systemName: "clock").foregroundStyle(.secondary)
            .accessibilityLabel(Text("Queued"))
        case "failed":
            Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
                .help(ErrorText.text(state?.error))
                .accessibilityLabel(Text("Failed: \(ErrorText.text(state?.error))"))
        case "interrupted", "cancelled":
            Image(systemName: "pause.circle").foregroundStyle(.orange)
                .accessibilityLabel(Text("Interrupted"))
        default: Text("")
        }
    }
}

private struct FlagsCell: View {
    let flags: [String]

    var body: some View {
        let shown = flags.filter { $0 != "empty_label" && $0 != "truncated" }
        if !flags.isEmpty {
            Image(systemName: shown.isEmpty ? "info.circle" : "flag.fill")
                .foregroundStyle(shown.isEmpty ? Color.secondary : Color.orange)
                .help(flags.map(FlagText.text).joined(separator: "\n"))
        }
    }
}

/// Error codes of the pipeline's jobs in the app's words.
enum ErrorText {
    static func text(_ code: String?) -> String {
        switch code {
        case "hu_implausible": String(localized: "No air in the image; check rescale slope/intercept.")
        case "convert_failed": String(localized: "dcm2niix could not convert the series.")
        case "convert_ambiguous": String(localized: "The files of the series do not form one volume.")
        case "convert_empty": String(localized: "dcm2niix wrote no volume.")
        case "too_small": String(localized: "The volume is too small to segment.")
        case "not_3d": String(localized: "The series is not one 3D volume.")
        case "catalog_changed": String(localized: "The index changed; retry after the next scan.")
        case "catalog_busy": String(localized: "The index was busy; retry.")
        case "source_changed": String(localized: "Files of the series changed or are gone; scan the source again.")
        case "source_unreadable": String(localized: "Files of the series cannot be read.")
        case "part_not_found": String(localized: "The index has no files for this series; scan again.")
        case "model_not_in_bundle": String(localized: "A model is missing from the app bundle.")
        case "labelmap_missing": String(localized: "A segmentation is missing.")
        case "watchdog_timeout": String(localized: "The worker stopped responding and was ended.")
        case "results_not_stored": String(localized: "The results could not be saved in the project.")
        case let code?: code
        case nil: String(localized: "Unknown error")
        }
    }
}

/// QC flags of plan §12 and ADR 0031 in the app's words.
enum FlagText {
    static func text(_ flag: String) -> String {
        switch flag {
        case "truncated": String(localized: "A structure touches the edge of the image.")
        case "empty_label": String(localized: "A structure was not found.")
        case "thick_slices": String(localized: "Slices thicker than 3 mm.")
        case "spacing_irregular": String(localized: "Uneven slice spacing.")
        case "spacing_equalized": String(localized: "Uneven slices were resampled to even spacing by dcm2niix.")
        case "gantry_tilt", "gantry_tilt_corrected": String(localized: "Gantry tilt, corrected by dcm2niix.")
        case "converter_fallback": String(localized: "Converted with SimpleITK because dcm2niix split the series.")
        case "labelmap_resampled": String(localized: "The segmentation was resampled to the CT's grid.")
        case "device_fallback": String(localized: "The segmentation fell back from the Apple GPU to the CPU.")
        default: flag
        }
    }
}

enum RunText {
    static func title(_ run: PipelineStore.RunInfo) -> String {
        let date = ISO8601DateFormatter().date(from: run.createdAt)?
            .formatted(date: .abbreviated, time: .shortened) ?? run.createdAt
        let lock = run.locked ? String(localized: ", locked") : ""
        return String(localized: "\(date) — \(run.models.count) models\(lock)")
    }

    /// `clin_ct_body_composition` → "Body composition".
    static func modelName(_ model: String) -> String {
        LabelNames.displayName(model.replacingOccurrences(of: "clin_ct_", with: ""))
    }
}

/// The models to run, the device, and what the batch will cover.
struct AnalyzeSheet: View {
    @Environment(ProjectSession.self) private var session
    @Environment(\.dismiss) private var dismiss
    @AppStorage("analysisModels") private var stored = "clin_ct_organs"
    @AppStorage("analysisDevice") private var device = "auto"

    private var chosen: Set<String> { Set(stored.split(separator: ",").map(String.init)) }

    var body: some View {
        let offered = BundledModel.offered(session.bundledModels)
        let models = offered.map(\.name).filter(chosen.contains)
        VStack(alignment: .leading, spacing: 12) {
            Text("Analyze Selected Series").font(.title2.bold())
            Text("Each selected CT is converted, segmented with every model chosen here, measured and prepared for the viewer.")
                .foregroundStyle(.secondary)
            GroupBox("Models") {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(offered) { model in
                        Toggle(isOn: Binding(
                            get: { chosen.contains(model.name) },
                            set: { on in
                                var set = chosen
                                if on { set.insert(model.name) } else { set.remove(model.name) }
                                stored = set.sorted().joined(separator: ",")
                            })) {
                            HStack {
                                Text(RunText.modelName(model.name))
                                Text("\(model.labels.count) structures").foregroundStyle(.secondary)
                                if model.isSlow {
                                    Text("takes several times longer").font(.caption).foregroundStyle(.orange)
                                }
                            }
                        }
                    }
                    HStack {
                        Button("All") { stored = offered.map(\.name).joined(separator: ",") }
                        Button("None") { stored = "" }
                    }
                    .controlSize(.small)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            Picker("Device", selection: $device) {
                Text("Automatic (Apple GPU)").tag("auto")
                Text("CPU only (hours per CT)").tag("cpu")
            }
            .pickerStyle(.radioGroup)
            let count = session.previewCount(models: models, device: device)
            Text("\(count) selected CT series will be analyzed.")
            // Plan §8: about 250 MB per series while it is converted and
            // cached, and some MB per model; a margin of 2 GB besides.
            let needed = Int64(count) * (250 + Int64(models.count) * 60) * 1_000_000 + 2_000_000_000
            if let free = session.freeBytes, free < needed {
                Label("The project's disk has \(ByteCountFormatter.string(fromByteCount: free, countStyle: .file)) free; this batch may need about \(ByteCountFormatter.string(fromByteCount: needed, countStyle: .file)).",
                      systemImage: "externaldrive.badge.exclamationmark")
                    .foregroundStyle(.orange)
            }
            if MachineInfo.memoryGB < 16 {
                Label("MOOSE recommends at least 16 GB of memory; this Mac has \(MachineInfo.memoryGB) GB.",
                      systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
            }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Analyze") {
                    session.startAnalysis(models: models, device: device)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(models.isEmpty || count == 0)
            }
        }
        .padding(20)
        .frame(width: 520)
    }
}
