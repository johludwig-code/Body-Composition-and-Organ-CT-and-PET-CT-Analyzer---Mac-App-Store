import BCOAKit
import BCOAStore
import Foundation
import GRDB
import Observation

/// What a viewer window shows: one series of one run, from its viewer cache
/// (plan §9). Everything the slice views draw is computed from here, so a
/// change of window, visibility or crosshair is one property and one redraw.
@Observable
@MainActor
final class ViewerModel {
    enum Layout: String, CaseIterable, Identifiable {
        case single, three, grid
        var id: String { rawValue }
    }

    let session: ProjectSession
    let runID: String
    private(set) var seriesKeys: [String] = []
    private(set) var seriesKey: String
    private(set) var volume: ViewerVolume?
    private(set) var loadError: String?
    private(set) var title = ""

    /// model → label id → name, from the run's models.
    private(set) var labelNames: [String: [Int: String]] = [:]
    /// model → label id → mL, from the run's results.
    private(set) var volumes: [String: [Int: Double]] = [:]
    var visible: [String: Set<Int>] = [:]
    var highlighted: (model: String, label: Int)?
    var opacity = 0.45
    var contour = false
    var showOverlays = true
    var neurological = false
    var showCrosshair = true
    var layout: Layout = .three
    var singlePlane: Plane = .axial
    var presetID = WindowPreset.softTissue.id
    private(set) var window = WindowLUT(width: WindowPreset.softTissue.width, center: WindowPreset.softTissue.center)
    var cursor = VolumeIndex(i: 0, j: 0, k: 0)
    var hover: VolumeIndex?
    private(set) var qc = QCStore.SeriesQC.unreviewed
    var comment = ""

    init(session: ProjectSession, runID: String, seriesKey: String) {
        self.session = session
        self.runID = runID
        self.seriesKey = seriesKey
        seriesKeys = session.progress.filter { $0.stages["viewer_cache"]?.status == "done" }.map(\.seriesKey)
        load(seriesKey)
    }

    var models: [String] { volume?.models ?? [] }

    func load(_ key: String) {
        seriesKey = key
        // The run's own cache (ADR 0031 decision 11), else one written before
        // caches were kept per run.
        var folder = session.url.appendingPathComponent(PipelineJobs.viewerCacheFolder(key, run: runID))
        if !FileManager.default.fileExists(atPath: folder.appendingPathComponent("meta.json").path) {
            folder = session.url.appendingPathComponent("viewer-cache/\(key)")
        }
        do {
            let volume = try ViewerVolume(folder: folder)
            self.volume = volume
            loadError = nil
            let g = volume.geometry
            cursor = VolumeIndex(i: g.nx / 2, j: g.ny / 2, k: g.nz / 2)
        } catch {
            volume = nil
            loadError = String(localized: "The viewer cache of this series is missing or damaged. Analyze it again.")
        }
        try? session.database.read { db in
            labelNames = Dictionary(uniqueKeysWithValues: try PipelineStore.models(of: runID, in: db).map {
                ($0.name, $0.labels)
            })
            var mL: [String: [Int: Double]] = [:]
            for row in try Row.fetchAll(db, sql: """
                SELECT model, label_id, volume_ml FROM results WHERE run_id = ? AND series_key = ?
                """, arguments: [runID, key]) {
                mL[row["model"], default: [:]][row["label_id"]] = row["volume_ml"]
            }
            volumes = mL
            qc = try QCStore.current(runID: runID, seriesKey: key, in: db)
            title = try Row.fetchOne(db, sql: """
                SELECT p.pseudonym, st.study_date, s.description FROM series AS s
                JOIN studies AS st USING (study_key) JOIN patients AS p USING (patient_key)
                WHERE s.series_key = ?
                """, arguments: [key]).map {
                    [$0["pseudonym"], $0["study_date"], $0["description"]].compactMap { $0 as String? }
                        .joined(separator: " · ")
                } ?? key
        }
        comment = qc.comment ?? ""
        for model in models where visible[model] == nil {
            visible[model] = Set(volume?.meta.models[model]?.present ?? [])
        }
        if let first = models.first, presetID == WindowPreset.softTissue.id {
            apply(WindowPreset.defaultPreset(forModel: first))
        }
    }

    // MARK: Navigation

    func step(_ delta: Int) {
        guard let index = seriesKeys.firstIndex(of: seriesKey) else { return }
        let next = index + delta
        guard seriesKeys.indices.contains(next) else { return }
        load(seriesKeys[next])
    }

    func mapping(_ plane: Plane) -> PlaneMapping? {
        volume.map { PlaneMapping(plane: plane, geometry: $0.geometry, neurological: neurological) }
    }

    func slice(_ plane: Plane) -> Int {
        switch plane {
        case .axial: cursor.k
        case .coronal: cursor.j
        case .sagittal: cursor.i
        }
    }

    func scroll(_ plane: Plane, by delta: Int) {
        guard let mapping = mapping(plane) else { return }
        let target = max(0, min(mapping.sliceCount - 1, slice(plane) + delta))
        switch plane {
        case .axial: cursor.k = target
        case .coronal: cursor.j = target
        case .sagittal: cursor.i = target
        }
    }

    /// A click sets the crosshair, which moves the other planes to it.
    func click(_ plane: Plane, column: Int, row: Int) {
        guard let mapping = mapping(plane), (0..<mapping.width).contains(column),
              (0..<mapping.height).contains(row)
        else { return }
        cursor = mapping.voxel(column: column, row: row, slice: slice(plane))
        if let (model, label) = labels(at: cursor).first {
            highlighted = (model, label)
        }
    }

    func hover(_ plane: Plane, column: Int, row: Int) {
        guard let mapping = mapping(plane), (0..<mapping.width).contains(column),
              (0..<mapping.height).contains(row)
        else {
            hover = nil
            return
        }
        hover = mapping.voxel(column: column, row: row, slice: slice(plane))
    }

    // MARK: Window

    func apply(_ preset: WindowPreset) {
        presetID = preset.id
        window = WindowLUT(width: preset.width, center: preset.center)
    }

    /// Right-drag: horizontally the width, vertically the level, as on most
    /// reading consoles.
    func adjustWindow(dx: Double, dy: Double) {
        presetID = ""
        window = WindowLUT(width: max(1, window.width + dx * 4), center: window.center - dy * 2)
    }

    // MARK: Labels

    func labels(at v: VolumeIndex) -> [(String, Int)] {
        guard let volume else { return [] }
        return models.compactMap { model in
            let label = volume.label(model, at: v)
            return label != 0 ? (model, label) : nil
        }
    }

    func name(_ model: String, _ label: Int) -> String {
        LabelNames.displayName(labelNames[model]?[label] ?? "label_\(label)")
    }

    func toggle(_ model: String, _ label: Int) {
        if visible[model, default: []].contains(label) {
            visible[model]?.remove(label)
        } else {
            visible[model, default: []].insert(label)
        }
    }

    /// ⌥-click or I: only this label stays visible; again, all come back.
    func isolate(_ model: String, _ label: Int) {
        let only: [String: Set<Int>] = Dictionary(uniqueKeysWithValues: models.map {
            ($0, $0 == model ? [label] : [])
        })
        if visible == only {
            showAll()
        } else {
            visible = only
        }
        highlighted = (model, label)
    }

    func showAll(_ model: String? = nil) {
        for name in models where model == nil || name == model {
            visible[name] = Set(volume?.meta.models[name]?.present ?? [])
        }
    }

    func hideAll(_ model: String) {
        visible[model] = []
    }

    // MARK: QC

    func setQC(_ status: QCStore.Status) {
        let reviewer = session.reviewer
        session.write { db in
            try QCStore.set(status, comment: comment, runID: runID, seriesKey: seriesKey, reviewer: reviewer, in: db)
        }
        reloadQC()
    }

    func exclude(_ model: String, _ label: Int, _ excluded: Bool, reason: String?) {
        let reviewer = session.reviewer
        session.write { db in
            try QCStore.exclude(excluded, model: model, labelID: label, reason: reason, runID: runID,
                                seriesKey: seriesKey, reviewer: reviewer, in: db)
        }
        reloadQC()
    }

    private func reloadQC() {
        qc = (try? session.database.read { try QCStore.current(runID: runID, seriesKey: seriesKey, in: $0) })
            ?? .unreviewed
        session.reload()
    }

    // MARK: Cursor info

    /// Position in patient coordinates (LPS, mm), voxel, HU and the labels
    /// under the cursor (plan §9 info line).
    var info: String {
        guard let volume, let v = hover ?? Optional(cursor) else { return "" }
        let m = volume.meta
        let x = m.origin.x + Double(v.i) * m.spacing.x
        let y = m.origin.y + Double(v.j) * m.spacing.y
        let z = m.origin.z + Double(v.k) * m.spacing.z
        // Rounded first, then + 0, which turns −0.0 (shown as "-0.0") into 0.0.
        let mm = [x, y, z].map { (($0 * 10).rounded() / 10 + 0).formatted(.number.precision(.fractionLength(1))) }
            .joined(separator: " / ")
        let names = labels(at: v).map { name($0.0, $0.1) }.joined(separator: ", ")
        let hu = volume.hu(at: v)
        return String(localized: "\(mm) mm · voxel \(v.i), \(v.j), \(v.k) · \(Int(hu)) HU") + (names.isEmpty ? "" : " · \(names)")
    }
}
