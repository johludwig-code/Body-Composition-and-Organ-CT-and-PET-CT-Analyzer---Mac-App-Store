import BCOAKit
import BCOAStore
import GRDB
import SwiftUI

extension ProjectSession {
    var unconfirmedPatients: Int {
        tree.patients.filter { $0.idStatus == "unconfirmed" }.count
    }

    func folderCandidates() -> [IndexStore.FolderCandidates] {
        (try? IndexStore.folderCandidates(project: database, catalog: catalogURL)) ?? []
    }

    func confirmFolderIDs(level: Int, candidates: [IndexStore.FolderCandidates]) {
        let actor = reviewer
        write { try IndexStore.confirmFolderIDs(level: level, candidates: candidates, in: $0, actor: actor) }
        reload()
    }

    var canRemoveIdentifiers: Bool {
        !jobs.contains { !$0.status.isFinished && ($0.job.exclusiveGroup == "catalog" || $0.job.kind == .export) }
    }

    func exportsWithIdentifiers() -> [String] {
        (try? database.read { try IndexStore.exportsWithIdentifiers($0) }) ?? []
    }

    /// ADR 0024 decision 9: the database's part in one transaction, then the
    /// catalog, previews, job files and the chosen exports, then a scan
    /// without a key.
    func removeIdentifiers(deleting exports: [String]) {
        guard canRemoveIdentifiers else {
            message = String(localized: "Remove Identifiers is available when indexing and exporting have finished.")
            return
        }
        do {
            try IndexStore.removeIdentifiers(from: database, actor: reviewer)
        } catch {
            message = String(localized: "The identifiers could not be removed; nothing was changed.")
            return
        }
        let files = FileManager.default
        let index = url.appendingPathComponent("index")
        for name in (try? files.contentsOfDirectory(atPath: index.path)) ?? []
        where name.hasPrefix("catalog.") || name == "previews" {
            try? files.removeItem(at: index.appendingPathComponent(name))
        }
        try? JobQueue.removeLeftoverJobFiles(projectDir: url)
        for path in exports {
            try? files.removeItem(at: url.appendingPathComponent(path))
        }
        reload()
        scan(quietly: true)
        message = String(localized: """
            The identifiers were removed. The index is built again without patient IDs; \
            new studies can no longer be added to this project.
            """)
    }

    // MARK: Selection settings (plan §7, ADR 0023)

    var selectionSettings: SelectionSettings {
        guard let text = try? database.read({ try IndexStore.meta($0, "selection_config") }),
              let object = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any]
        else { return SelectionSettings() }
        return SelectionSettings(object)
    }

    /// Saved in `project_meta` and applied by a regroup (ADR 0023).
    func save(_ settings: SelectionSettings) {
        guard let data = try? JSONSerialization.data(withJSONObject: settings.json, options: [.sortedKeys]) else {
            return
        }
        let text = String(decoding: data, as: UTF8.self)
        write { try IndexStore.setMeta($0, "selection_config", text) }
        regroup()
    }
}

/// The values of "Selection Settings…" with the worker's defaults (ADR 0023,
/// `index_payload.schema.json`). The kernel lists are not edited here; a
/// stored configuration that has them keeps them.
struct SelectionSettings: Equatable {
    var minSlices = 50
    var coverageToleranceMM = 1.0
    var thicknessTieMM = 0.02
    var thicknessFloorMM: Double?
    var kernelBeforeThickness = false
    var acceptDerivedPrimary = false
    var excludedImageTypes = [
        "LOCALIZER", "SCOUT", "PROJECTION IMAGE", "SCREEN SAVE", "REFORMATTED", "MPR", "MIP", "MINIP", "VRT",
        "CPR", "CURVED", "SECONDARY",
    ]
    var excludedTerms = [
        "topogram", "scout", "localizer", "surview", "scanogram", "dose report", "patient protocol",
        "screen save", "mip", "minip", "mpr", "vrt", "3d",
    ]
    var extra: [String: Any] = [:]

    static func == (a: SelectionSettings, b: SelectionSettings) -> Bool {
        a.json as NSDictionary == b.json as NSDictionary
    }

    init() {}

    init(_ object: [String: Any]) {
        minSlices = object["min_slices"] as? Int ?? minSlices
        coverageToleranceMM = object["coverage_tolerance_mm"] as? Double ?? coverageToleranceMM
        thicknessTieMM = object["thickness_tie_mm"] as? Double ?? thicknessTieMM
        thicknessFloorMM = object["thickness_floor_mm"] as? Double
        kernelBeforeThickness = object["kernel_before_thickness"] as? Bool ?? false
        acceptDerivedPrimary = object["accept_derived_primary"] as? Bool ?? false
        excludedImageTypes = object["excluded_image_type_values"] as? [String] ?? excludedImageTypes
        excludedTerms = object["excluded_description_terms"] as? [String] ?? excludedTerms
        let known: Set<String> = [
            "min_slices", "coverage_tolerance_mm", "thickness_tie_mm", "thickness_floor_mm",
            "kernel_before_thickness", "accept_derived_primary", "excluded_image_type_values",
            "excluded_description_terms",
        ]
        extra = object.filter { !known.contains($0.key) }
    }

    var json: [String: Any] {
        var object = extra
        object["version"] = 1
        object["min_slices"] = minSlices
        object["coverage_tolerance_mm"] = coverageToleranceMM
        object["thickness_tie_mm"] = thicknessTieMM
        object["thickness_floor_mm"] = thicknessFloorMM ?? NSNull()
        object["kernel_before_thickness"] = kernelBeforeThickness
        object["accept_derived_primary"] = acceptDerivedPrimary
        object["excluded_image_type_values"] = excludedImageTypes
        object["excluded_description_terms"] = excludedTerms
        return object
    }
}

struct SelectionSettingsSheet: View {
    @Environment(ProjectSession.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var settings = SelectionSettings()
    @State private var floorOn = false
    @State private var imageTypes = ""
    @State private var terms = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Selection Settings").font(.title2.bold())
            Text("How the automatic selection picks one CT per study. A change regroups the index; choices you made by hand stay.")
                .foregroundStyle(.secondary)
            Form {
                Stepper(value: $settings.minSlices, in: 1...2000, step: 5) {
                    Text("At least \(settings.minSlices) slice positions")
                }
                TextField("Coverage tolerance (mm)", value: $settings.coverageToleranceMM, format: .number)
                TextField("Thickness tie (mm)", value: $settings.thicknessTieMM, format: .number)
                Toggle("Treat slices thinner than a floor as equal", isOn: $floorOn)
                if floorOn {
                    TextField("Thickness floor (mm)", value: Binding(
                        get: { settings.thicknessFloorMM ?? 1.0 }, set: { settings.thicknessFloorMM = $0 }),
                              format: .number)
                }
                Toggle("Kernel class before slice thickness", isOn: $settings.kernelBeforeThickness)
                Toggle("Accept DERIVED\\PRIMARY CT (e.g. the low-dose CT of a Siemens PET/CT)",
                       isOn: $settings.acceptDerivedPrimary)
                TextField("Excluded Image Type values", text: $imageTypes, axis: .vertical)
                TextField("Excluded description terms", text: $terms, axis: .vertical)
            }
            .formStyle(.grouped)
            HStack {
                Button("Restore Defaults") { load(SelectionSettings()) }
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Save and Regroup") {
                    settings.thicknessFloorMM = floorOn ? (settings.thicknessFloorMM ?? 1.0) : nil
                    settings.excludedImageTypes = list(imageTypes).map { $0.uppercased() }
                    settings.excludedTerms = list(terms).map { $0.lowercased() }
                    session.save(settings)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(session.isIndexing)
            }
        }
        .padding(20)
        .frame(width: 560)
        .onAppear { load(session.selectionSettings) }
    }

    private func load(_ value: SelectionSettings) {
        settings = value
        floorOn = value.thicknessFloorMM != nil
        imageTypes = value.excludedImageTypes.joined(separator: ", ")
        terms = value.excludedTerms.joined(separator: ", ")
    }

    private func list(_ text: String) -> [String] {
        text.split(separator: ",").map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }.filter { !$0.isEmpty }
    }
}

/// "Confirm Patient IDs…" (ADR 0024 decision 3): anonymous studies wait
/// until a folder level is confirmed as their patient ID.
struct ConfirmIDsSheet: View {
    @Environment(ProjectSession.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var candidates: [IndexStore.FolderCandidates] = []
    @State private var level = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Confirm Patient IDs").font(.title2.bold())
            Text("These studies have no usable patient ID. Choose the folder level that names the patient; studies in the same folder become one patient. Until then they are not selected.")
                .foregroundStyle(.secondary)
            Picker("Folder level", selection: $level) {
                ForEach(levels, id: \.self) { level in
                    Text(level == 0 ? String(localized: "Source folder") : String(localized: "Level \(level)"))
                        .tag(level)
                }
            }
            .pickerStyle(.segmented)
            Table(candidates) {
                TableColumn("Study") { Text("\($0.pseudonym) · \($0.studyDate ?? "")") }
                TableColumn("Patient ID from the folder") { candidate in
                    Text(candidate.levels[level]?.label ?? String(localized: "— (no folder at this level)"))
                        .foregroundStyle(candidate.levels[level] == nil ? .secondary : .primary)
                }
            }
            .frame(minHeight: 220)
            let patients = Set(candidates.compactMap { $0.levels[level]?.link }).count
            Text("\(candidates.filter { $0.levels[level] != nil }.count) studies become \(patients) patients.")
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Confirm") {
                    session.confirmFolderIDs(level: level, candidates: candidates)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(patients == 0)
            }
        }
        .padding(20)
        .frame(width: 620)
        .onAppear {
            candidates = session.folderCandidates()
            level = IndexStore.defaultFolderLevel(candidates)
        }
    }

    private var levels: [Int] {
        Array(Set(candidates.flatMap { $0.levels.keys })).sorted()
    }
}

struct RemoveIdentifiersSheet: View {
    @Environment(ProjectSession.self) private var session
    @Environment(\.dismiss) private var dismiss
    @State private var exports: [String] = []
    @State private var deleteExports = true

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("Remove Identifiers", systemImage: "person.crop.circle.badge.xmark").font(.title2.bold())
            Text("""
                Deletes every patient ID and accession number, the links between studies and patients, \
                and the index with its folder names. Pseudonyms, results and QC stay. This cannot be undone, \
                and new studies can no longer be added to this project.
                """)
            if !exports.isEmpty {
                Toggle("Also delete the \(exports.count) exports that hold patient IDs", isOn: $deleteExports)
                Text(exports.joined(separator: "\n")).font(.caption.monospaced()).foregroundStyle(.secondary)
            }
            Text("Copies outside the project folder are beyond the app's reach. Deleting a file on APFS does not overwrite it.")
                .font(.caption).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Remove Identifiers", role: .destructive) {
                    session.removeIdentifiers(deleting: deleteExports ? exports : [])
                    dismiss()
                }
                .disabled(!session.canRemoveIdentifiers)
            }
        }
        .padding(20)
        .frame(width: 520)
        .onAppear { exports = session.exportsWithIdentifiers() }
    }
}

extension ProjectSession {
    func assign(studies: [String], to patient: String) {
        let actor = reviewer
        write { try IndexStore.assign(studies: studies, to: patient, in: $0, actor: actor) }
        reload()
    }
}

/// "Assign to Patient…": the chosen studies join an existing patient.
struct AssignSheet: View {
    @Environment(ProjectSession.self) private var session
    @Environment(\.dismiss) private var dismiss
    let studies: [String]
    @State private var patient = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Assign to Patient").font(.title2.bold())
            Text("\(studies.count) studies move to the patient chosen here. A patient left without studies is removed; its pseudonym is never used again.")
                .foregroundStyle(.secondary)
            Picker("Patient", selection: $patient) {
                ForEach(session.tree.patients) { patient in
                    Text("\(patient.pseudonym) — \(patient.studies.count) studies").tag(patient.key)
                }
            }
            HStack {
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                Button("Assign") {
                    session.assign(studies: studies, to: patient)
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
                .disabled(patient.isEmpty)
            }
        }
        .padding(20)
        .frame(width: 440)
        .onAppear { patient = session.tree.patients.first?.key ?? "" }
    }
}
