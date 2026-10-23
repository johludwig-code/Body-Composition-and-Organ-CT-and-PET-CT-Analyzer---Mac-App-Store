import BCOAStore
import SwiftUI

/// Patient → study → series with the automatic selection, its reasons and
/// the checks (plan §7). The selection is edited here; nothing here reads a
/// source file.
struct PatientsView: View {
    @Environment(ProjectSession.self) private var session
    @State private var filter = SeriesFilter()
    @State private var selection: Set<TreeRow.ID> = []
    @State private var cohortName = ""
    @State private var askingCohortName = false
    @State private var confirmingIDs = false
    @State private var editingSettings = false
    @State private var assigning: [String]?
    @State private var typingID: [String]?
    @AppStorage("bulkThinCTMaxMM") private var bulkMaxMM = 3.0

    var body: some View {
        let rows = TreeRow.rows(session.tree, filter: filter)
        VStack(spacing: 0) {
            FilterBar(filter: $filter)
            Divider()
            if session.tree.patients.isEmpty {
                ContentUnavailableView(
                    "No Series Yet",
                    systemImage: "person.2",
                    description: Text("Add source folders; their series appear here once they are indexed."))
            } else {
                Table(rows, children: \.children, selection: $selection) {
                    TableColumn("") { row in
                        if case .series(let series, let status) = row {
                            Toggle("", isOn: Binding(
                                get: { series.selected },
                                set: { session.apply($0 ? .select([series.key]) : .deselect([series.key])) }))
                                .labelsHidden()
                                .disabled(series.gone || status == "unconfirmed")
                                .accessibilityLabel(Text("Selected"))
                        }
                    }
                    .width(28)
                    TableColumn("") { row in
                        if case .series(let series, _) = row {
                            PreviewCell(url: session.previewURL(series), version: session.previewsVersion)
                        }
                    }
                    .width(36)
                    TableColumn("Patient / Study / Series") { row in
                        TreeTitle(row: row)
                    }
                    .width(min: 220, ideal: 320)
                    TableColumn("Modality") { row in Text(row.series?.modality ?? "") }
                        .width(60)
                    TableColumn("Slices") { row in
                        Text(row.series.map { ($0.sliceCount ?? $0.imageCount).formatted() } ?? "")
                    }
                    .width(60)
                    TableColumn("Thickness") { row in
                        Text(row.series?.sliceThicknessMM.map { mm($0) } ?? "")
                    }
                    .width(70)
                    TableColumn("Coverage") { row in
                        Text(row.series?.zExtentMM.map { mm($0) } ?? "")
                    }
                    .width(80)
                    TableColumn("Kernel") { row in
                        Text(row.series.map { kernel($0) } ?? "")
                    }
                    .width(90)
                    TableColumn("Status") { row in
                        StatusCell(row: row)
                    }
                    .width(min: 160, ideal: 220)
                    TableColumn("Checks") { row in
                        ChecksCell(checks: row.checks)
                    }
                    .width(60)
                }
                .contextMenu(forSelectionType: TreeRow.ID.self) { ids in
                    let series = TreeRow.seriesKeys(ids, in: rows)
                    Button("Select") { session.apply(.select(series)) }.disabled(series.isEmpty)
                    Button("Deselect") { session.apply(.deselect(series)) }.disabled(series.isEmpty)
                    Button("Make Primary") { session.apply(.makePrimary(series[0])) }
                        .disabled(series.count != 1)
                    Divider()
                    let studies = TreeRow.studyKeys(ids, in: rows)
                    Button("Apply Auto-Selection") { session.apply(.applyAuto(studies: studies)) }
                        .disabled(studies.isEmpty)
                    Button("Assign to Patient…") { assigning = studies }
                        .disabled(studies.isEmpty)
                    Button("Set Patient ID…") { typingID = studies }
                        .disabled(studies.isEmpty)
                }
            }
            Divider()
            HStack {
                Text(summary).font(.callout).foregroundStyle(.secondary)
                Spacer()
            }
            .padding(.horizontal)
            .padding(.vertical, 6)
        }
        .toolbar {
            ToolbarItemGroup {
                if session.unconfirmedPatients > 0 {
                    Button("Confirm Patient IDs…") { confirmingIDs = true }
                        .help("Studies without a usable patient ID wait for a folder level to be confirmed.")
                }
                Button("Selection Settings…") { editingSettings = true }
                Button("Apply Auto-Selection to All") { session.apply(.applyAuto(studies: nil)) }
                Button("Select All CT ≤ \(bulkMaxMM.formatted()) mm") {
                    session.apply(.bulkThinCT(maxMM: bulkMaxMM, studies: nil))
                }
                Menu("Cohorts") {
                    Button("Save Selection as Cohort…") { askingCohortName = true }
                    if !session.cohorts.isEmpty {
                        Divider()
                        ForEach(session.cohorts) { cohort in
                            Button("Apply “\(cohort.label)”") { session.apply(.applyCohort(id: cohort.id)) }
                        }
                    }
                }
            }
        }
        .sheet(isPresented: $confirmingIDs) { ConfirmIDsSheet().environment(session) }
        .sheet(item: Binding(get: { assigning.map(StudyList.init) }, set: { assigning = $0?.keys })) {
            AssignSheet(studies: $0.keys).environment(session)
        }
        .sheet(item: Binding(get: { typingID.map(StudyList.init) }, set: { typingID = $0?.keys })) {
            TypedIDSheet(studies: $0.keys).environment(session)
        }
        .sheet(isPresented: $editingSettings) { SelectionSettingsSheet().environment(session) }
        .alert("Save Selection as Cohort", isPresented: $askingCohortName) {
            TextField("Name", text: $cohortName)
            Button("Save") {
                let name = cohortName.trimmingCharacters(in: .whitespaces)
                if !name.isEmpty { session.apply(.saveCohort(label: name)) }
                cohortName = ""
            }
            Button("Cancel", role: .cancel) { cohortName = "" }
        } message: {
            Text("A cohort keeps the selected series and their primary flags.")
        }
    }

    private var summary: String {
        let tree = session.tree
        let studies = tree.patients.reduce(0) { $0 + $1.studies.count }
        return String(localized: """
            \(tree.patients.count) patients, \(studies) studies, \(tree.seriesCount) series, \
            \(tree.selectedCount) selected
            """)
    }

    private func mm(_ value: Double) -> String {
        "\(value.formatted(.number.precision(.fractionLength(0...2)))) mm"
    }

    private func kernel(_ series: ProjectTree.Series) -> String {
        guard let kernel = series.kernel else { return "" }
        switch series.kernelClass {
        case "soft": return String(localized: "\(kernel) (soft)")
        case "sharp": return String(localized: "\(kernel) (sharp)")
        default: return kernel
        }
    }
}

/// The filters of plan §7. They hide rows; they never change the selection.
struct SeriesFilter: Equatable {
    var modality = ""
    var minSlices = 0
    var maxThicknessMM: Double?
    var text = ""
    var regex = false
    var selectedOnly = false

    func matches(_ series: ProjectTree.Series) -> Bool {
        if !modality.isEmpty, series.modality != modality { return false }
        if (series.sliceCount ?? series.imageCount) < minSlices { return false }
        if let max = maxThicknessMM, (series.sliceThicknessMM ?? .infinity) > max + 0.02 { return false }
        if selectedOnly, !series.selected { return false }
        if !text.isEmpty {
            let description = series.description ?? ""
            if regex {
                guard let pattern = try? Regex(text).ignoresCase(), description.contains(pattern) else {
                    return false
                }
            } else if !description.localizedCaseInsensitiveContains(text) {
                return false
            }
        }
        return true
    }

    var isActive: Bool { self != SeriesFilter() }
}

private struct FilterBar: View {
    @Binding var filter: SeriesFilter

    var body: some View {
        HStack(spacing: 12) {
            Picker("Modality", selection: $filter.modality) {
                Text("All").tag("")
                ForEach(["CT", "PT", "MR", "OT"], id: \.self) { Text($0).tag($0) }
            }
            .fixedSize()
            Stepper(value: $filter.minSlices, in: 0...2000, step: 10) {
                Text("At least \(filter.minSlices) slices")
            }
            .fixedSize()
            Picker("Thickness", selection: $filter.maxThicknessMM) {
                Text("Any").tag(Double?.none)
                ForEach([1.0, 2.0, 3.0, 5.0], id: \.self) { Text("≤ \($0.formatted()) mm").tag(Double?.some($0)) }
            }
            .fixedSize()
            TextField(filter.regex ? "Description (regular expression)" : "Description", text: $filter.text)
                .textFieldStyle(.roundedBorder)
                .frame(minWidth: 140)
            Toggle("Regex", isOn: $filter.regex).toggleStyle(.checkbox)
            Toggle("Selected only", isOn: $filter.selectedOnly).toggleStyle(.checkbox)
            if filter.isActive {
                Button("Clear") { filter = SeriesFilter() }
            }
        }
        .padding(.horizontal)
        .padding(.vertical, 8)
    }
}

/// One row of the tree. Patients and studies stay visible while one of their
/// series matches the filter.
enum TreeRow: Identifiable, Hashable {
    case patient(ProjectTree.Patient, children: [TreeRow])
    case study(ProjectTree.Study, patientStatus: String, children: [TreeRow])
    case series(ProjectTree.Series, patientStatus: String)

    var id: String {
        switch self {
        case .patient(let patient, _): patient.key
        case .study(let study, _, _): study.key
        case .series(let series, _): series.key
        }
    }

    var children: [TreeRow]? {
        switch self {
        case .patient(_, let children), .study(_, _, let children): children
        case .series: nil
        }
    }

    var series: ProjectTree.Series? {
        if case .series(let series, _) = self { return series }
        return nil
    }

    var checks: [ProjectTree.Check] {
        switch self {
        case .patient(let patient, _): patient.checks
        case .study(let study, _, _): study.checks
        case .series(let series, _): series.checks
        }
    }

    static func rows(_ tree: ProjectTree, filter: SeriesFilter) -> [TreeRow] {
        tree.patients.compactMap { patient in
            let studies: [TreeRow] = patient.studies.compactMap { study in
                let series = study.series.filter(filter.matches).map {
                    TreeRow.series($0, patientStatus: patient.idStatus)
                }
                guard !series.isEmpty || (!filter.isActive && !study.series.isEmpty) else { return nil }
                return .study(study, patientStatus: patient.idStatus, children: series)
            }
            guard !studies.isEmpty else { return nil }
            return .patient(patient, children: studies)
        }
    }

    /// The series among `ids`, and the series of the studies and patients
    /// among them, as the context menu acts on them.
    static func seriesKeys(_ ids: Set<String>, in rows: [TreeRow]) -> [String] {
        var keys: [String] = []
        func walk(_ row: TreeRow, inside: Bool) {
            let hit = inside || ids.contains(row.id)
            if case .series(let series, let status) = row {
                if hit, !series.gone, status != "unconfirmed" { keys.append(series.key) }
            }
            for child in row.children ?? [] { walk(child, inside: hit) }
        }
        rows.forEach { walk($0, inside: false) }
        return keys
    }

    static func studyKeys(_ ids: Set<String>, in rows: [TreeRow]) -> [String] {
        var keys: [String] = []
        func walk(_ row: TreeRow, inside: Bool) {
            let hit = inside || ids.contains(row.id)
            if case .study(let study, _, let children) = row {
                if hit || children.contains(where: { ids.contains($0.id) }) { keys.append(study.key) }
            }
            for child in row.children ?? [] { walk(child, inside: hit) }
        }
        rows.forEach { walk($0, inside: false) }
        return keys
    }
}

private struct TreeTitle: View {
    let row: TreeRow

    var body: some View {
        switch row {
        case .patient(let patient, _):
            HStack(spacing: 6) {
                Text(patient.pseudonym).fontWeight(.semibold)
                Text(patientDetail(patient)).foregroundStyle(.secondary)
            }
        case .study(let study, _, _):
            HStack(spacing: 6) {
                Text(studyDate(study)).monospacedDigit()
                Text(study.description ?? "").foregroundStyle(.secondary)
            }
        case .series(let series, _):
            HStack(spacing: 4) {
                if series.primary {
                    Image(systemName: "star.fill").foregroundStyle(.yellow)
                        .help(CodeText.text(group: "status", code: "badge.primary"))
                        .accessibilityLabel(Text(CodeText.text(group: "status", code: "badge.primary")))
                }
                if let number = series.seriesNumber {
                    Text("#\(number)").foregroundStyle(.secondary).monospacedDigit()
                }
                Text(series.description ?? "—")
                    .strikethrough(series.gone)
            }
        }
    }

    private func patientDetail(_ patient: ProjectTree.Patient) -> String {
        var parts: [String] = []
        if let sex = patient.sex { parts.append(sex) }
        if let age = patient.ageAtFirstStudy { parts.append(String(localized: "\(Int(age)) y")) }
        if patient.idStatus == "unconfirmed" { parts.append(String(localized: "ID not confirmed")) }
        return parts.joined(separator: " · ")
    }

    private func studyDate(_ study: ProjectTree.Study) -> String {
        guard let date = study.date,
              let parsed = try? Date(date, strategy: .iso8601.year().month().day())
        else { return study.date ?? String(localized: "No date") }
        return parsed.formatted(date: .abbreviated, time: .omitted)
    }
}

private struct StatusCell: View {
    let row: TreeRow

    var body: some View {
        switch row {
        case .series(let series, let status):
            Text(CodeText.status(of: series, patientStatus: status))
                .foregroundStyle(series.selected ? .primary : .secondary)
                .help(CodeText.reason(series.reasonJSON))
        case .study(let study, _, _):
            Text(study.selectionMode == "user" ? String(localized: "Chosen by you") : "")
                .foregroundStyle(.secondary)
        case .patient:
            EmptyView()
        }
    }
}

private struct ChecksCell: View {
    let checks: [ProjectTree.Check]

    var body: some View {
        if !checks.isEmpty {
            let warning = checks.contains { $0.level == "warning" }
            Image(systemName: warning ? "exclamationmark.triangle.fill" : "info.circle")
                .foregroundStyle(warning ? .orange : .secondary)
                .help(checks.map(CodeText.check).joined(separator: "\n"))
                .accessibilityLabel(Text(checks.map(CodeText.check).joined(separator: ". ")))
        }
    }
}

/// The studies a sheet acts on, as an item for `.sheet(item:)`.
struct StudyList: Identifiable {
    var keys: [String]
    var id: String { keys.joined(separator: ",") }
}

/// The middle slice of a series, from `index/previews` (ADR 0025). `version`
/// makes the cell look again when previews arrive.
private struct PreviewCell: View {
    let url: URL?
    let version: Int

    var body: some View {
        if let url, let image = NSImage(contentsOf: url) {
            Image(nsImage: image).resizable().interpolation(.medium).aspectRatio(contentMode: .fit)
                .frame(width: 28, height: 28)
                .accessibilityLabel(Text("Preview"))
        } else {
            Color.clear.frame(width: 28, height: 28)
        }
    }
}
