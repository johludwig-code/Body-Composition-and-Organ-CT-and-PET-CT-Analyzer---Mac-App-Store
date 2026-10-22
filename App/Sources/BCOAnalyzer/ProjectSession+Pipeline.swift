import BCOAKit
import BCOAStore
import Foundation
import GRDB

/// The pipeline of ADR 0031: a batch makes or reuses a run, queues the
/// conversion of every selected CT, and each stage that ends `done` queues
/// the next. Payloads are built here from the database and the project
/// folder when a stage is queued, never stored.
extension ProjectSession {
    struct BatchPlan {
        var runID: String
        var series: [String]
    }

    /// How many series "Analyze" would queue for these models, without
    /// making a run: selected CT without results for all of them in the run
    /// it would use.
    func previewCount(models: [String], device: String) -> Int {
        (try? database.read { db in
            let run = try PipelineStore.existingRun(
                models: entries(models), device: device, settings: settings(device), in: db)
            return try PipelineStore.seriesToAnalyze(runID: run, models: models, in: db).count
        }) ?? 0
    }

    private func plan(models: [String], device: String) -> BatchPlan? {
        do {
            return try database.write { db in
                let run = try PipelineStore.run(
                    models: entries(models), device: device, settings: settings(device), versions: versions,
                    in: db)
                return BatchPlan(runID: run, series: try PipelineStore.seriesToAnalyze(runID: run, models: models, in: db))
            }
        } catch {
            message = String(localized: "The analysis could not be prepared.")
            return nil
        }
    }

    func startAnalysis(models: [String], device: String) {
        guard let plan = plan(models: models, device: device) else { return }
        guard !plan.series.isEmpty else {
            message = String(localized: "Every selected CT already has results for these models in this run.")
            return
        }
        let busy = Set(pipelineJobs.filter { entry in
            jobs.contains { $0.id == entry.key && !$0.status.isFinished }
        }.map(\.value.series))
        for series in plan.series where !busy.contains(series) {
            queueStage(.convert, run: plan.runID, series: series)
        }
        currentRunID = plan.runID
        reload()
    }

    private func entries(_ models: [String]) -> [PipelineStore.ModelEntry] {
        bundledModels.filter { models.contains($0.name) }.map {
            PipelineStore.ModelEntry(name: $0.name, zipSHA256: $0.zipSHA256, labels: $0.labels)
        }
    }

    /// What decides whether two batches are one run (ADR 0031 decision 9).
    private func settings(_ device: String) -> [String: Any] {
        ["device": device, "retry_on_cpu": false, "pipeline_version": "1",
         "dcm2niix_flags": "-z n -b y -ba y -i n -f ct"]
    }

    private var versions: [String: String] {
        ["app": AppIdentity.version, "chip": MachineInfo.chip,
         "memory_gb": String(MachineInfo.memoryGB), "macos": MachineInfo.macOS]
    }

    /// At launch: stages left queued or running are queued again (plan §8).
    func resumePipeline() {
        guard let pending = try? database.write({ try PipelineStore.interruptLeftovers(in: $0) }) else { return }
        for stage in pending {
            guard let kind = JobKind(rawValue: stage.kind) else { continue }
            queueStage(kind, run: stage.runID, series: stage.seriesKey)
        }
        reload()
    }

    /// "Retry Failed" for the pipeline: each failed series from the stage
    /// that failed.
    func retryFailedStages() {
        guard let run = currentRunID else { return }
        for series in progress {
            for kind in PipelineJobs.stages where series.stages[kind.rawValue]?.status == "failed"
                || series.stages[kind.rawValue]?.status == "interrupted"
                || series.stages[kind.rawValue]?.status == "cancelled" {
                queueStage(kind, run: run, series: series.seriesKey)
                break
            }
        }
        reload()
    }

    func queueStage(_ kind: JobKind, run: String, series: String) {
        do {
            let payload = try payload(for: kind, run: run, series: series)
            enqueue(kind, prefix: kind.rawValue, payload: payload, run: run, series: series)
        } catch {
            Self.log.error("stage \(kind.rawValue, privacy: .public) not queued: \(String(describing: type(of: error)), privacy: .public)")
            message = String(localized: "A step of the analysis could not be queued.")
        }
    }

    struct MissingCatalogPart: Error {}

    private func payload(for kind: JobKind, run: String, series: String) throws -> [String: JSONValue] {
        let models = try database.read { try PipelineStore.models(of: run, in: $0) }.map(\.name)
        let device = try database.read { db in
            try String.fetchOne(db, sql: "SELECT device FROM runs WHERE run_id = ?", arguments: [run])
        } ?? "auto"
        switch kind {
        case .convert:
            guard let part = try database.read({ try PipelineStore.catalogPart(of: series, in: $0) }) else {
                throw MissingCatalogPart()
            }
            resolveGrants()
            return PipelineJobs.convert(seriesKey: series, catalogID: part.catalogID, partRef: part.partRef,
                                        sources: indexSources())
        case .segment:
            return PipelineJobs.segment(seriesKey: series, models: models, device: device)
        case .metrics:
            let used = try database.read { db in
                try String.fetchOne(db, sql: """
                    SELECT device FROM jobs WHERE run_id = ? AND series_key = ? AND kind = 'segment'
                      AND status = 'done' ORDER BY ended_at DESC LIMIT 1
                    """, arguments: [run, series])
            } ?? device
            return PipelineJobs.metrics(seriesKey: series, models: models, device: used, fallback: false,
                                        flags: conversionFlags(series))
        case .viewerCache:
            return PipelineJobs.viewerCache(seriesKey: series, models: models)
        default:
            return [:]
        }
    }

    /// The QC flags of the conversion, from `convert.json` beside the CT: it
    /// outlives the app, unlike the job's result.
    private func conversionFlags(_ series: String) -> [String] {
        let file = url.appendingPathComponent(PipelineJobs.work(series)).appendingPathComponent("convert.json")
        guard let data = try? Data(contentsOf: file),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [] }
        return object["qc_flags"] as? [String] ?? []
    }

    func finalizeStage(_ job: QueuedJob) async {
        guard let (run, series) = pipelineJobs[job.id] else { return }
        var device: String?
        if job.job.kind == .segment {
            device = resultLists[job.id]?.compactMap { $0["device"]?.stringValue }.last
        }
        if job.status == .done, job.job.kind == .metrics {
            let rows = (resultLists[job.id]?.last?["rows"]).map(Self.resultRows) ?? []
            guard store(rows, run: run, series: series) else {
                write {
                    try PipelineStore.updateJob(id: job.id, status: "failed", errorCode: "results_not_stored", in: $0)
                }
                return
            }
        }
        let code = job.errorCode ?? (job.status == .failed ? "failed" : nil)
        write {
            try PipelineStore.updateJob(id: job.id, status: job.status.rawValue, errorCode: code, device: device, in: $0)
        }
        if job.status == .failed {
            Self.log.error("\(job.job.kind.rawValue, privacy: .public) failed: \(code ?? "", privacy: .public)")
        }
        guard job.status == .done else { return }
        if let next = PipelineJobs.next(after: job.job.kind) {
            queueStage(next, run: run, series: series)
        } else {
            // Plan §8: labelmaps and metrics stay; the converted CT goes,
            // because it can be made again from the DICOM files.
            let ct = url.appendingPathComponent(PipelineJobs.ct(series))
            try? FileManager.default.removeItem(at: ct)
        }
    }

    /// Synchronous, like `write`: inside an async function GRDB's async
    /// overload would be chosen.
    private func store(_ rows: [PipelineStore.ResultRow], run: String, series: String) -> Bool {
        (try? database.write { try PipelineStore.storeResults(rows, runID: run, seriesKey: series, in: $0) }) != nil
    }

    static func resultRows(_ value: JSONValue) -> [PipelineStore.ResultRow] {
        guard case .array(let rows) = value else { return [] }
        return rows.compactMap { row in
            guard let model = row["model"]?.stringValue, let id = row["label_id"]?.intValue,
                  let name = row["label_name"]?.stringValue, let count = row["voxel_count"]?.intValue
            else { return nil }
            let flags: [String] = {
                if case .array(let list) = row["flags"] { return list.compactMap(\.stringValue) }
                return []
            }()
            return PipelineStore.ResultRow(
                model: model, labelID: id, labelName: name, voxelCount: count,
                volumeML: row["volume_ml"]?.doubleValue, huMean: row["hu_mean"]?.doubleValue,
                huSD: row["hu_sd"]?.doubleValue, huMedian: row["hu_median"]?.doubleValue,
                huP05: row["hu_p05"]?.doubleValue, huP95: row["hu_p95"]?.doubleValue,
                huMin: row["hu_min"]?.doubleValue, huMax: row["hu_max"]?.doubleValue,
                touchesBorder: row["touches_border"]?.boolValue ?? false,
                device: row["device"]?.stringValue ?? "unknown", flags: flags)
        }
    }
}
