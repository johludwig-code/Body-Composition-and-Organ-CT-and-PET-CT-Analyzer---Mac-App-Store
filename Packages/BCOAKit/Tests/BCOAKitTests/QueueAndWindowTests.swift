import Foundation
import Testing
@testable import BCOAKit

private func job(_ id: String, _ kind: JobKind, _ status: JobStatus) -> QueuedJob {
    QueuedJob(
        job: WorkerJob(jobID: id, kind: kind, projectDir: URL(fileURLWithPath: "/p"),
                       resourcesDir: URL(fileURLWithPath: "/r")),
        status: status)
}

@Test func neverTwoSegmentationsAtOnce() {
    let jobs = [job("a", .segment, .running), job("b", .segment, .queued), job("c", .index, .queued)]
    #expect(QueuePolicy.jobsToStart(jobs, paused: false) == ["c"])
}

@Test func firstQueuedSegmentationStartsWhenTheGPUIsFree() {
    let jobs = [job("a", .segment, .done), job("b", .segment, .queued), job("c", .segment, .queued)]
    #expect(QueuePolicy.jobsToStart(jobs, paused: false) == ["b"])
}

@Test func lightJobsAreLimited() {
    let jobs = [job("a", .index, .running), job("b", .export, .queued), job("c", .index, .queued)]
    #expect(QueuePolicy.jobsToStart(jobs, paused: false) == ["b"])
}

@Test func pausedQueueStartsNothing() {
    #expect(QueuePolicy.jobsToStart([job("a", .segment, .queued)], paused: true).isEmpty)
}

@Test func doneEventDecidesAndExitCodeOnlyWithoutOne() {
    #expect(QueuePolicy.finalStatus(done: .ok, exitStatus: 1) == .done)
    #expect(QueuePolicy.finalStatus(done: nil, exitStatus: 130) == .cancelled)
    #expect(QueuePolicy.finalStatus(done: nil, exitStatus: 9) == .failed)
    #expect(QueuePolicy.finalStatus(done: nil, exitStatus: nil) == .failed)
}

@Test func windowEdgesMapToBlackAndWhite() {
    let w = WindowPreset.softTissue
    #expect(Windowing.gray(w.center - w.width / 2, width: w.width, center: w.center) == 0)
    #expect(Windowing.gray(w.center + w.width / 2, width: w.width, center: w.center) == 255)
    #expect(Windowing.gray(-1000, width: w.width, center: w.center) == 0)
    #expect(Windowing.gray(3000, width: w.width, center: w.center) == 255)
    #expect(Windowing.gray(w.center, width: w.width, center: w.center) == 128)
}

@Test func presetsAreThePlansSix() {
    #expect(WindowPreset.all.map(\.name) == ["Soft Tissue", "Lung", "Bone", "Liver", "Brain", "Mediastinum"])
    #expect(WindowPreset.defaultPreset(forModel: "clin_ct_ribs") == .bone)
    #expect(WindowPreset.defaultPreset(forModel: "clin_ct_lungs") == .lung)
}

@Test func labelDisplayNamesMatchTheWorker() {
    #expect(LabelNames.displayName("kidney_left") == "Kidney left")
    #expect(LabelNames.displayName("vertebra_L3") == "Vertebra L3")
}

private func indexJob(_ id: String, _ mode: String, _ status: JobStatus) -> QueuedJob {
    QueuedJob(
        job: WorkerJob(jobID: id, kind: .index, projectDir: URL(fileURLWithPath: "/p"),
                       resourcesDir: URL(fileURLWithPath: "/r"), payload: ["mode": .string(mode)]),
        status: status)
}

@Test func scansAndRegroupsRunOneAtATime() {
    // Both write the catalog; the merge of the first must come before the
    // second starts (ADR 0021 decision 4). Previews only read it.
    let jobs = [indexJob("a", "scan", .running), indexJob("b", "regroup", .queued),
                indexJob("c", "previews", .queued)]
    #expect(QueuePolicy.jobsToStart(jobs, paused: false) == ["c"])
    let idle = [indexJob("a", "scan", .queued), indexJob("b", "scan", .queued)]
    #expect(QueuePolicy.jobsToStart(idle, paused: false) == ["a"])
}

@Test func onlyScanAndRegroupAreInTheCatalogGroup() {
    #expect(indexJob("a", "scan", .queued).job.exclusiveGroup == "catalog")
    #expect(indexJob("a", "regroup", .queued).job.exclusiveGroup == "catalog")
    #expect(indexJob("a", "previews", .queued).job.exclusiveGroup == nil)
    #expect(job("e", .export, .queued).job.exclusiveGroup == nil)
}
