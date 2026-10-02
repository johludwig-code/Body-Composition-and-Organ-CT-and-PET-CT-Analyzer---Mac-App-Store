import BCOAKit
import SwiftUI

struct ContentView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        NavigationSplitView {
            List(SidebarSection.allCases, selection: $model.selection) { section in
                Label {
                    Text(section.title)
                } icon: {
                    Image(systemName: section.systemImage)
                }
                .tag(section)
            }
            .navigationSplitViewColumnWidth(min: 180, ideal: 210)
        } detail: {
            VStack(spacing: 0) {
                detail
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                Divider()
                ResearchFooter()
            }
        }
        .navigationTitle(model.project?.folder.url.lastPathComponent ?? AppIdentity.displayName)
        .sheet(isPresented: $model.showAbout) { AboutView() }
        .alert(
            "Something went wrong",
            isPresented: Binding(get: { model.lastError != nil }, set: { if !$0 { model.lastError = nil } }),
            presenting: model.lastError
        ) { _ in
            Button("OK") { model.lastError = nil }
        } message: { message in
            Text(message)
        }
    }

    @ViewBuilder private var detail: some View {
        if model.project == nil {
            ContentUnavailableView {
                Label("No Project Open", systemImage: "folder.badge.plus")
            } description: {
                Text("A project is a folder that holds the index, the results and the exports of one study.")
            } actions: {
                Button("Open or Create Project…") { model.chooseProjectFolder() }
                    .buttonStyle(.borderedProminent)
            }
        } else {
            switch model.selection ?? .sources {
            case .sources: SourcesView()
            case .queue: QueueView()
            case .patients, .results, .export:
                ContentUnavailableView(
                    "Not Built Yet",
                    systemImage: "hammer",
                    description: Text("This part follows after the M0 spikes (docs/PLAN.md §17)."))
            }
        }
    }
}

/// The research notice in every window (plan §2 rule 5). Informational
/// only; App Review 2.4.5(vi) forbids making the user accept it.
struct ResearchFooter: View {
    var body: some View {
        Text(IntendedUse.short)
            .font(.footnote)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
            .accessibilityLabel(Text(IntendedUse.statement))
            .accessibilityIdentifier("researchNotice")
    }
}

struct SourcesView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        VStack(alignment: .leading) {
            HStack {
                Text("Source Folders").font(.title2)
                Spacer()
                Button("Add Source Folders…") { model.addSourceFolders() }
            }
            if model.sources.isEmpty {
                ContentUnavailableView(
                    "No Source Folders",
                    systemImage: "folder",
                    description: Text("Add folders that contain DICOM. They are only ever read."))
            } else {
                List(model.sources) { source in
                    // The folder name only; full paths often contain patient
                    // names and are kept out of the interface where possible.
                    Label(source.url.lastPathComponent, systemImage: "folder")
                }
            }
        }
        .padding()
    }
}

struct QueueView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        Table(model.jobs) {
            TableColumn("Job") { Text($0.job.kind.rawValue) }
            TableColumn("Status") { Text($0.status.rawValue.capitalized) }
            TableColumn("Progress") { job in
                if let fraction = job.fraction {
                    ProgressView(value: fraction)
                } else if job.status == .running {
                    ProgressView().controlSize(.small)
                }
            }
            TableColumn("Message") { Text($0.message ?? "") }
            TableColumn("") { job in
                if job.status == .running || job.status == .queued {
                    Button("Cancel") { model.cancel(job.id) }
                }
            }
            .width(70)
        }
    }
}
