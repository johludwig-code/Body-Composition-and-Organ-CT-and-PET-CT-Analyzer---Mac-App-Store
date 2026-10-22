import BCOAKit
import SwiftUI

struct ContentView: View {
    @Environment(AppModel.self) private var model
    @Environment(\.openWindow) private var openWindow

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
        .navigationTitle(model.session?.url.lastPathComponent ?? AppIdentity.displayName)
        #if DEBUG
        // Development only: `-BCOAOpenViewer <series_key>` opens that series
        // of the newest run, so the viewer can be checked without clicks.
        .task {
            if let key = UserDefaults.standard.string(forKey: "BCOAOpenViewer"),
               let run = model.session?.currentRunID {
                openWindow(id: "viewer", value: ViewerRequest(runID: run, seriesKey: key))
            }
        }
        #endif
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
        if let session = model.session {
            Group {
                switch model.selection ?? .sources {
                case .sources: SourcesView()
                case .patients: PatientsView()
                case .queue: QueueView()
                case .results: ResultsView()
                case .export: ExportView()
                }
            }
            .environment(session)
            .alert(
                "Notice",
                isPresented: Binding(get: { session.message != nil }, set: { if !$0 { session.message = nil } }),
                presenting: session.message
            ) { _ in
                Button("OK") { session.message = nil }
            } message: { message in
                Text(message)
            }
        } else {
            ContentUnavailableView {
                Label("No Project Open", systemImage: "folder.badge.plus")
            } description: {
                Text("A project is a folder that holds the index, the results and the exports of one study.")
            } actions: {
                Button("Open or Create Project…") { model.chooseProjectFolder() }
                    .buttonStyle(.borderedProminent)
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

struct QueueView: View {
    @Environment(ProjectSession.self) private var session

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("Queue").font(.title2)
                Spacer()
                Button("Retry Failed") {
                    session.retryFailed()
                    session.retryFailedStages()
                }
                .disabled(!session.jobs.contains { $0.status == .failed })
            }
            .padding()
            Table(session.jobs) {
                TableColumn("Job") { Text(JobText.kind($0.job)) }
                TableColumn("Status") { Text(JobText.status($0.status)) }
                TableColumn("Progress") { job in
                    if let fraction = job.fraction {
                        ProgressView(value: fraction)
                    } else if job.status == .running {
                        ProgressView().controlSize(.small)
                    }
                }
                TableColumn("Message") { Text(JobText.detail($0)) }
                TableColumn("") { job in
                    if job.status == .running || job.status == .queued {
                        Button("Cancel") { session.cancel(job.id) }
                    }
                }
                .width(70)
            }
        }
    }
}

/// Job names, states and progress in the app's own words; the worker's
/// `message` is English for the log and stands in only where the app has no
/// text of its own.
enum JobText {
    static func kind(_ job: WorkerJob) -> String {
        switch job.kind {
        case .index:
            switch job.payload["mode"]?.stringValue {
            case "regroup": String(localized: "Regroup index")
            case "previews": String(localized: "Previews")
            default: String(localized: "Index source folders")
            }
        case .convert: String(localized: "Convert")
        case .segment: String(localized: "Segment")
        case .metrics: String(localized: "Metrics")
        case .viewerCache: String(localized: "Viewer cache")
        case .export: String(localized: "Export")
        case .selftest: String(localized: "Self-Test")
        case .spikeS2: String(localized: "Sandbox check")
        }
    }

    static func status(_ status: JobStatus) -> String {
        switch status {
        case .queued: String(localized: "Queued")
        case .running: String(localized: "Running")
        case .done: String(localized: "Done")
        case .failed: String(localized: "Failed")
        case .cancelled: String(localized: "Cancelled")
        case .interrupted: String(localized: "Interrupted")
        }
    }

    static func detail(_ job: QueuedJob) -> String {
        if let detail = job.detail {
            let done = detail.done.formatted()
            switch (detail.phase, detail.total) {
            case (.walk, _): return String(localized: "Looking for files: \(done) found")
            case (.read, let total?): return String(localized: "Reading files: \(done) of \(total.formatted())")
            case (.read, nil): return String(localized: "Reading files: \(done)")
            case (.group, _): return String(localized: "Grouping series")
            case (.previews, _): return String(localized: "Making previews")
            }
        }
        if let code = job.errorCode { return code }
        return job.message ?? ""
    }
}
