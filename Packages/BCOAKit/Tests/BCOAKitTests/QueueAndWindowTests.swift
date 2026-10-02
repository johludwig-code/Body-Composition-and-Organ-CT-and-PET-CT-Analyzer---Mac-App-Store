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
