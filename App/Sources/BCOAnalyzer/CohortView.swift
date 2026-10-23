import BCOAKit
import BCOAStore
import Charts
import SwiftUI

/// The cohort overview of plan §12: one structure's metric over every series
/// of the run, sortable, with robust outliers marked, and its distribution.
struct CohortView: View {
    @Environment(ProjectSession.self) private var session
    @Environment(\.openWindow) private var openWindow
    @State private var model = ""
    @State private var label = 0
    @State private var metric = "volume_ml"
    @State private var sort = [KeyPathComparator(\CohortRow.sortValue)]

    struct CohortRow: Identifiable {
        var id: String { value.seriesKey }
        var value: PipelineStore.CohortValue
        var z: Double?
        var outlier: Bool
        var sortValue: Double { value.value ?? -.infinity }
        var pseudonym: String { value.pseudonym }
    }

    var body: some View {
        let models = session.runModels
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Picker("Model", selection: $model) {
                    ForEach(models.map(\.name), id: \.self) { Text(RunText.modelName($0)).tag($0) }
                }
                .fixedSize()
                Picker("Structure", selection: $label) {
                    ForEach((models.first { $0.name == model }?.labels ?? [:]).sorted { $0.key < $1.key }, id: \.key) {
                        Text(ProjectSession.structureCatalog?.displayName(model: model, labelID: $0.key, label: $0.value)
                             ?? LabelNames.displayName($0.value)).tag($0.key)
                    }
                }
                .fixedSize()
                Picker("Metric", selection: $metric) {
                    ForEach(PipelineStore.metricColumns, id: \.self) { Text(MetricText.name($0)).tag($0) }
                }
                .fixedSize()
                Spacer()
            }
            .padding(.horizontal)
            let rows = self.rows
            if rows.count < CohortStatistics.minimumSeries {
                Text("Outliers are marked from \(CohortStatistics.minimumSeries) series on; this run has \(rows.count).")
                    .font(.caption).foregroundStyle(.secondary).padding(.horizontal)
            }
            Chart(rows.filter { $0.value.value != nil }) { row in
                PointMark(x: .value(MetricText.name(metric), row.value.value ?? 0), y: .value("Series", "all"))
                    .foregroundStyle(row.outlier ? Color.orange : Color.accentColor)
                    .symbolSize(row.outlier ? 90 : 40)
            }
            .chartYAxis(.hidden)
            .frame(height: 90)
            .padding(.horizontal)
            Table(rows.sorted(using: sort), sortOrder: $sort) {
                TableColumn("Patient", value: \.pseudonym)
                TableColumn("Study") { Text($0.value.studyDate ?? "") }
                TableColumn("Series") { Text($0.value.description ?? "—") }
                TableColumn(MetricText.name(metric), value: \.sortValue) { row in
                    Text(row.value.value.map { $0.formatted(.number.precision(.fractionLength(0...1))) } ?? "—")
                        .monospacedDigit()
                }
                TableColumn("Robust z") { row in
                    Text(row.z.map { $0.formatted(.number.precision(.fractionLength(1))) } ?? "")
                        .monospacedDigit()
                        .foregroundStyle(row.outlier ? .orange : .secondary)
                        .help(row.outlier ? String(localized: "volume_outlier: robust z beyond ±3.5") : "")
                }
            }
            .contextMenu(forSelectionType: String.self) { _ in } primaryAction: { keys in
                guard let run = session.currentRunID else { return }
                for key in keys { openWindow(id: "viewer", value: ViewerRequest(runID: run, seriesKey: key)) }
            }
        }
        .onAppear { choose(models) }
        .onChange(of: session.currentRunID) { choose(session.runModels) }
    }

    private func choose(_ models: [PipelineStore.ModelEntry]) {
        if !models.contains(where: { $0.name == model }) { model = models.first?.name ?? "" }
        if let labels = models.first(where: { $0.name == model })?.labels, labels[label] == nil {
            label = labels.keys.sorted().first ?? 0
        }
    }

    private var rows: [CohortRow] {
        guard let run = session.currentRunID else { return [] }
        let values = (try? session.database.read {
            try PipelineStore.cohort(runID: run, model: model, labelID: label, metric: metric, in: $0)
        }) ?? []
        let present = values.compactMap(\.value)
        let z = CohortStatistics.robustZ(present)
        var index = 0
        return values.map { value in
            guard value.value != nil else { return CohortRow(value: value, z: nil, outlier: false) }
            defer { index += 1 }
            let score = z[index]
            return CohortRow(value: value, z: score, outlier: (score.map(abs) ?? 0) > CohortStatistics.threshold)
        }
    }
}

extension ProjectSession {
    var runModels: [PipelineStore.ModelEntry] {
        guard let run = currentRunID else { return [] }
        return (try? database.read { try PipelineStore.models(of: run, in: $0) }) ?? []
    }
}

enum MetricText {
    static func name(_ column: String) -> String {
        switch column {
        case "volume_ml": String(localized: "Volume (mL)")
        case "hu_mean": String(localized: "Mean (HU)")
        case "hu_sd": String(localized: "SD (HU)")
        case "hu_median": String(localized: "Median (HU)")
        case "hu_p05": String(localized: "5th percentile (HU)")
        case "hu_p95": String(localized: "95th percentile (HU)")
        case "hu_min": String(localized: "Minimum (HU)")
        case "hu_max": String(localized: "Maximum (HU)")
        case "voxel_count": String(localized: "Voxels")
        default: column
        }
    }
}
