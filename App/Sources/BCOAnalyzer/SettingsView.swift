import BCOAKit
import SwiftUI

struct SettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        TabView {
            SelfTestPane()
                .tabItem { Label("Self-Test", systemImage: "stethoscope") }
            DiagnosticsPane()
                .tabItem { Label("Diagnostics", systemImage: "ladybug") }
        }
        .frame(width: 560, height: 420)
        .environment(model)
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
                .disabled(model.project == nil)
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
                if model.sources.isEmpty {
                    Text("Add a source folder first.").foregroundStyle(.secondary)
                }
                ForEach(model.sources) { source in
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
        ForEach(model.jobs.filter { $0.job.kind == kind }) { job in
            VStack(alignment: .leading, spacing: 2) {
                Text("\(job.status.rawValue.capitalized) — \(job.message ?? job.errorCode ?? "")")
                if let payload = model.results[job.id],
                   let data = try? JSONEncoder.pretty.encode(payload),
                   let text = String(data: data, encoding: .utf8) {
                    Text(text).font(.system(.caption, design: .monospaced)).textSelection(.enabled)
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
