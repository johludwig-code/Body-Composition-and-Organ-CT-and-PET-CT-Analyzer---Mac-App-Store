import BCOAKit
import BCOAStore
import SwiftUI

/// What opens a viewer window: a series of a run.
struct ViewerRequest: Codable, Hashable {
    var runID: String
    var seriesKey: String
}

/// The viewer window (plan §9): planes in radiological convention, the label
/// tree, the cursor's information and the QC bar, all of it by keyboard too.
struct ViewerWindow: View {
    @Environment(AppModel.self) private var app
    let request: ViewerRequest

    var body: some View {
        if let session = app.session {
            ViewerView(model: ViewerModel(session: session, runID: request.runID, seriesKey: request.seriesKey))
                .frame(minWidth: 1000, minHeight: 640)
        } else {
            ContentUnavailableView("No Project Open", systemImage: "folder")
        }
    }
}

struct ViewerView: View {
    @State var model: ViewerModel
    @State private var search = ""
    @FocusState private var commentFocused: Bool

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            Divider()
            HSplitView {
                planes
                    .frame(minWidth: 600)
                labelList
                    .frame(minWidth: 240, idealWidth: 280, maxWidth: 380)
            }
            Divider()
            HStack {
                Text(model.info).font(.system(.callout, design: .monospaced)).foregroundStyle(.secondary)
                    .lineLimit(1)
                Spacer()
            }
            .padding(.horizontal).padding(.vertical, 4)
            Divider()
            qcBar
            Divider()
            ResearchFooter()
        }
        .navigationTitle(model.title)
        .focusable()
        .focusEffectDisabled()
        .onKeyPress(phases: .down) { press in commentFocused ? .ignored : key(press) }
    }

    // MARK: Parts

    private var toolbar: some View {
        HStack(spacing: 12) {
            Picker("Layout", selection: $model.layout) {
                Text("1×1").tag(ViewerModel.Layout.single)
                Text("1×3").tag(ViewerModel.Layout.three)
                Text("2×2").tag(ViewerModel.Layout.grid)
            }
            .pickerStyle(.segmented).fixedSize()
            if model.layout == .single {
                Picker("Plane", selection: $model.singlePlane) {
                    ForEach(Plane.allCases, id: \.self) { Text(PlaneText.name($0)).tag($0) }
                }
                .fixedSize()
            }
            Menu("Window") {
                ForEach(Array(WindowPreset.all.enumerated()), id: \.element.id) { index, preset in
                    Button("\(preset.name) (W \(Int(preset.width)) / L \(Int(preset.center)))") { model.apply(preset) }
                        .keyboardShortcut(KeyEquivalent(Character("\(index + 1)")), modifiers: [])
                }
            }
            .fixedSize()
            Text("W \(Int(model.window.width)) L \(Int(model.window.center))").monospacedDigit()
                .foregroundStyle(.secondary)
            Slider(value: $model.opacity, in: 0...1) { Text("Opacity") }.frame(width: 140)
            Toggle("Contour", isOn: $model.contour).toggleStyle(.checkbox)
            Toggle("Overlays", isOn: $model.showOverlays).toggleStyle(.checkbox)
            Toggle("Crosshair", isOn: $model.showCrosshair).toggleStyle(.checkbox)
            Toggle("Neurological", isOn: $model.neurological).toggleStyle(.checkbox)
                .help("Mirror axial and coronal planes left to right; the letters follow.")
            Spacer()
            if model.volume?.meta.oblique == true {
                Label("Oblique acquisition", systemImage: "exclamationmark.triangle").foregroundStyle(.orange)
            }
        }
        .padding(.horizontal).padding(.vertical, 6)
    }

    @ViewBuilder private var planes: some View {
        if let error = model.loadError {
            ContentUnavailableView("No Viewer Data", systemImage: "eye.slash", description: Text(error))
        } else {
            switch model.layout {
            case .single:
                SliceView(model: model, plane: model.singlePlane)
            case .three:
                HStack(spacing: 2) {
                    ForEach(Plane.allCases, id: \.self) { SliceView(model: model, plane: $0) }
                }
            case .grid:
                VStack(spacing: 2) {
                    HStack(spacing: 2) {
                        SliceView(model: model, plane: .axial)
                        SliceView(model: model, plane: .coronal)
                    }
                    HStack(spacing: 2) {
                        SliceView(model: model, plane: .sagittal)
                        Color.black.overlay(legend)
                    }
                }
            }
        }
    }

    private var legend: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(model.labels(at: model.cursor), id: \.1) { model_, label in
                HStack {
                    swatch(label)
                    Text(model.name(model_, label)).foregroundStyle(.white)
                }
            }
        }
        .padding()
    }

    private var labelList: some View {
        VStack(spacing: 0) {
            TextField("Search structures", text: $search).textFieldStyle(.roundedBorder).padding(8)
            List {
                ForEach(model.models, id: \.self) { name in
                    Section {
                        ForEach(labels(of: name), id: \.self) { label in
                            labelRow(name, label)
                        }
                    } header: {
                        HStack {
                            Text(RunText.modelName(name))
                            Spacer()
                            Button("Show All") { model.showAll(name) }.buttonStyle(.link)
                            Button("Hide All") { model.hideAll(name) }.buttonStyle(.link)
                        }
                    }
                }
            }
        }
    }

    private func labels(of name: String) -> [Int] {
        let all = (model.labelNames[name] ?? [:]).keys.sorted()
        guard !search.isEmpty else { return all }
        return all.filter { model.name(name, $0).localizedCaseInsensitiveContains(search) }
    }

    private func labelRow(_ name: String, _ label: Int) -> some View {
        let present = model.volume?.meta.models[name]?.present.contains(label) ?? false
        let excluded = model.qc.excluded[name]?[label] != nil
        let highlighted = model.highlighted?.model == name && model.highlighted?.label == label
        return HStack(spacing: 6) {
            Toggle("", isOn: Binding(
                get: { model.visible[name]?.contains(label) ?? false },
                set: { _ in model.toggle(name, label) }))
                .labelsHidden().toggleStyle(.checkbox).disabled(!present)
            swatch(label)
            Text(model.name(name, label))
                .foregroundStyle(present ? .primary : .secondary)
                .strikethrough(excluded)
                .fontWeight(highlighted ? .bold : .regular)
            Spacer()
            if let mL = model.volumes[name]?[label] {
                Text("\(mL.formatted(.number.precision(.fractionLength(1)))) mL")
                    .monospacedDigit().foregroundStyle(.secondary)
            } else if !present {
                Text("not found").font(.caption).foregroundStyle(.secondary)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture {
            if NSEvent.modifierFlags.contains(.option) {
                model.isolate(name, label)
            } else {
                model.highlighted = (name, label)
            }
        }
        .contextMenu {
            Button("Show Only This") { model.isolate(name, label) }
            Divider()
            if excluded {
                Button("Include in Export Again") { model.exclude(name, label, false, reason: nil) }
            } else {
                Button("Exclude from Export") {
                    model.exclude(name, label, true, reason: model.comment.isEmpty ? nil : model.comment)
                }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private func swatch(_ label: Int) -> some View {
        let color = LabelPalette.color(label)
        return RoundedRectangle(cornerRadius: 2)
            .fill(Color(red: Double(color.r) / 255, green: Double(color.g) / 255, blue: Double(color.b) / 255))
            .frame(width: 12, height: 12)
            .accessibilityHidden(true)
    }

    private var qcBar: some View {
        HStack(spacing: 10) {
            Text("QC").font(.headline)
            Text(QCText.status(model.qc.status)).foregroundStyle(.secondary)
                .help(model.qc.reviewer.map { String(localized: "by \($0)") } ?? "")
            Button("Accept (A)") { model.setQC(.accepted) }
            Button("Reject (R)") { model.setQC(.rejected) }
            TextField("Comment (F)", text: $model.comment)
                .textFieldStyle(.roundedBorder).frame(minWidth: 200)
                .focused($commentFocused)
                .onSubmit {
                    model.setQC(.acceptedWithComment)
                    commentFocused = false
                }
            Spacer()
            Button("Previous (P)") { model.step(-1) }
            Button("Next (N)") { model.step(1) }
        }
        .padding(.horizontal).padding(.vertical, 6)
    }

    // MARK: Keyboard (plan §9)

    private func key(_ press: KeyPress) -> KeyPress.Result {
        let plane: Plane = model.layout == .single ? model.singlePlane : .axial
        switch press.key {
        case .upArrow: model.scroll(plane, by: -1)
        case .downArrow: model.scroll(plane, by: 1)
        case .pageUp: model.scroll(plane, by: -10)
        case .pageDown: model.scroll(plane, by: 10)
        default:
            switch press.characters.lowercased() {
            case "1", "2", "3", "4", "5", "6":
                let index = Int(press.characters)! - 1
                model.apply(WindowPreset.all[index])
            case "s": model.showOverlays.toggle()
            case "o": model.contour.toggle()
            case "c": model.showCrosshair.toggle()
            case "[": model.opacity = max(0, model.opacity - 0.1)
            case "]": model.opacity = min(1, model.opacity + 0.1)
            case "i":
                if let highlighted = model.highlighted { model.isolate(highlighted.model, highlighted.label) }
            case "a": model.setQC(.accepted)
            case "r": model.setQC(.rejected)
            case "f": commentFocused = true
            case "n": model.step(1)
            case "p": model.step(-1)
            default: return .ignored
            }
        }
        return .handled
    }
}

enum QCText {
    static func status(_ status: QCStore.Status) -> String {
        switch status {
        case .unreviewed: String(localized: "Unreviewed")
        case .accepted: String(localized: "Accepted")
        case .acceptedWithComment: String(localized: "Accepted with comment")
        case .rejected: String(localized: "Rejected")
        }
    }
}
