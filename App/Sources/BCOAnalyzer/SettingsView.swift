import BCOAKit
import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        TabView {
            GeneralPane()
                .tabItem { Label("General", systemImage: "gearshape") }
            SelfTestPane()
                .tabItem { Label("Self-Test", systemImage: "stethoscope") }
            DiagnosticsPane()
                .tabItem { Label("Diagnostics", systemImage: "ladybug") }
        }
        .frame(width: 560, height: 420)
        .environment(model)
    }
}

/// The reviewer's initials go into the audit log and the QC records (plan
/// §12); they are the user's own, never a patient's.
struct GeneralPane: View {
    @AppStorage("reviewerInitials") private var reviewer = ""
    @AppStorage("bulkThinCTMaxMM") private var bulkMaxMM = 3.0

    var body: some View {
        Form {
            TextField("Reviewer initials", text: $reviewer)
            Text("Recorded with every selection change and QC decision.")
                .font(.caption).foregroundStyle(.secondary)
            Stepper(value: $bulkMaxMM, in: 0.5...10, step: 0.5) {
                Text("“Select All CT” takes slices up to \(bulkMaxMM.formatted()) mm")
            }
        }
        .padding()
    }
}

/// Plan §8: checks model checksums and MPS, and optionally runs a tiny
/// inference on a synthetic volume.
struct SelfTestPane: View {
    @Environment(AppModel.self) private var model
    @State private var withInference = false

    var body: some View {
        Form {
            Toggle("Include a test inference (takes about a minute)", isOn: $withInference)
            Button("Run Self-Test") { model.runSelfTest(inference: withInference) }
                .disabled(model.session == nil)
            ResultList(kind: .selftest)
        }
        .padding()
    }
}

/// Spike S2 lives here until it has a Go; then this pane goes.
struct DiagnosticsPane: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Form {
            Section("Spike S2: worker inherits sandbox and folder access") {
                let grants = model.session.map { session in
                    session.sources.compactMap { session.grants[$0.id] }
                } ?? []
                if grants.isEmpty {
                    Text("Add a source folder first.").foregroundStyle(.secondary)
                }
                ForEach(grants) { source in
                    Button("Run with “\(source.url.lastPathComponent)”") { model.runSpikeS2(source: source) }
                }
            }
            ResultList(kind: .spikeS2)
        }
        .padding()
    }
}

private struct ResultList: View {
    @Environment(AppModel.self) private var model
    let kind: JobKind

    var body: some View {
        if let session = model.session {
            ForEach(session.jobs.filter { $0.job.kind == kind }) { job in
                VStack(alignment: .leading, spacing: 2) {
                    Text("\(JobText.status(job.status)) — \(job.message ?? job.errorCode ?? "")")
                    if let payload = session.results[job.id],
                       let data = try? JSONEncoder.pretty.encode(payload),
                       let text = String(data: data, encoding: .utf8) {
                        Text(text).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                    }
                }
            }
        }
    }
}

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}
