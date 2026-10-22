import BCOAStore
import SwiftUI

/// Source folders, their index state and the scan (plan §7 step 1).
struct SourcesView: View {
    @Environment(AppModel.self) private var model
    @Environment(ProjectSession.self) private var session
    @State private var selection: SourceRow.ID?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Source Folders").font(.title2)
                Spacer()
                Button("Scan All") { session.scan() }
                    .disabled(session.sources.isEmpty || session.isIndexing)
                Button("Add Source Folders…") { model.addSourceFolders() }
                    .buttonStyle(.borderedProminent)
            }
            .padding()
            if let job = session.indexProgress {
                HStack {
                    ProgressView().controlSize(.small)
                    Text(JobText.detail(job)).foregroundStyle(.secondary)
                    Spacer()
                    Button("Cancel") { session.cancel(job.id) }
                }
                .padding(.horizontal)
                .padding(.bottom, 8)
            }
            if session.mergePending && !session.isIndexing {
                Label("Index not yet applied: waiting for the export to finish.", systemImage: "clock")
                    .foregroundStyle(.secondary)
                    .padding(.horizontal)
                    .padding(.bottom, 8)
            }
            if session.sources.isEmpty {
                ContentUnavailableView(
                    "No Source Folders",
                    systemImage: "folder",
                    description: Text("Add folders that contain DICOM. They are only ever read."))
            } else {
                Table(session.sources, selection: $selection) {
                    // The folder name only; full paths often contain patient
                    // names and are kept out of the interface where possible.
                    TableColumn("Folder") { source in
                        Label(source.displayPath, systemImage: icon(source))
                    }
                    TableColumn("State") { source in
                        Text(CodeText.sourceState(source))
                            .foregroundStyle(source.state == "indexed" ? .primary : .secondary)
                    }
                    TableColumn("Files") { source in
                        Text(source.files.map { $0.formatted() } ?? "–")
                    }
                    .width(80)
                    TableColumn("") { source in
                        if source.volumeKind == "network" {
                            Text("Indexing a network folder takes longer than a local one.")
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                .contextMenu(forSelectionType: SourceRow.ID.self) { ids in
                    Button("Scan") { session.scan(Array(ids)) }
                        .disabled(session.isIndexing)
                    Button("Scan and Read Every File Again") { session.scan(Array(ids), forceReread: true) }
                        .disabled(session.isIndexing)
                    Divider()
                    Button("Remove Source") {
                        for id in ids { session.removeSource(id) }
                    }
                    .disabled(session.isIndexing)
                }
            }
            if !session.tree.projectChecks.isEmpty {
                Divider()
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(session.tree.projectChecks, id: \.self) { check in
                        Label(CodeText.check(check),
                              systemImage: check.level == "warning" ? "exclamationmark.triangle" : "info.circle")
                            .font(.callout)
                    }
                }
                .padding()
            }
        }
    }

    private func icon(_ source: SourceRow) -> String {
        switch source.volumeKind {
        case "network": "network"
        case "removable": "externaldrive"
        default: "folder"
        }
    }
}
