import CoreGraphics
import Foundation
import PDFKit
import Testing
@testable import BCOAKit

// The case report's PDF (ADR 0017 section 11, app part), drawn from a
// phantom viewer cache and a content file made to match it. Nothing here is
// patient data: the phantom is boxes of made-up values.

private let root = URL(fileURLWithPath: #filePath)
    .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    .deletingLastPathComponent().deletingLastPathComponent()

private let catalog: StructureCatalog = {
    let data = try! Data(contentsOf: root.appendingPathComponent("App/Resources/StructureNames.json"))
    return StructureCatalog(json: data)!
}()

private func id(_ model: String, _ label: String) -> Int {
    catalog.models[model]!.first { $0.value.label == label }!.key
}

/// Boxes in an LPS grid: i towards the patient's left, j posterior, k up.
private struct Box {
    var model: String
    var label: String
    var i: ClosedRange<Int>
    var j: ClosedRange<Int>
    var k: ClosedRange<Int>
    var hu: Int16 = 40
}

private let (nx, ny, nz) = (40, 30, 64)

/// Patient's right is small i. The head sits at the top (k ≥ 54), the lungs
/// below it, then the abdomen.
private func boxes(swap: Set<String> = [], landmarks: Bool = true) -> [Box] {
    // A swapped pair has its right label where the left one belongs.
    func side(_ pair: String, _ side: String) -> String {
        "\(pair)_\(swap.contains(pair) ? (side == "right" ? "left" : "right") : side)"
    }
    var list: [Box] = [
        Box(model: "clin_ct_organs", label: "liver", i: 6...15, j: 8...20, k: 20...28),
        Box(model: "clin_ct_organs", label: "spleen", i: 27...33, j: 14...22, k: 22...27),
        Box(model: "clin_ct_organs", label: side("kidney", "right"), i: 9...13, j: 18...23, k: 14...19),
        Box(model: "clin_ct_organs", label: side("kidney", "left"), i: 27...31, j: 18...23, k: 14...19),
        Box(model: "clin_ct_organs", label: side("adrenal_gland", "right"), i: 10...12, j: 18...19, k: 20...21),
        Box(model: "clin_ct_organs", label: side("adrenal_gland", "left"), i: 28...30, j: 18...19, k: 20...21),
        Box(model: "clin_ct_organs", label: "bladder", i: 17...23, j: 8...14, k: 3...7),
        Box(model: "clin_ct_cardiac", label: "pulmonary_artery", i: 18...24, j: 8...12, k: 40...44),
        Box(model: "clin_ct_cardiac", label: "heart_ventricle_left", i: 21...27, j: 6...12, k: 34...38),
        Box(model: "clin_ct_body_composition", label: "skeletal_muscle", i: 8...32, j: 22...26, k: 16...17),
    ]
    if landmarks {
        list += [
            Box(model: "clin_ct_organs", label: "lung_upper_lobe_right", i: 5...17, j: 6...22, k: 36...48, hu: -800),
            Box(model: "clin_ct_organs", label: "lung_upper_lobe_left", i: 23...35, j: 6...22, k: 36...48, hu: -800),
            Box(model: "clin_ct_body", label: "head", i: 12...28, j: 6...24, k: 54...63),
            Box(model: "clin_ct_body", label: "body", i: 4...36, j: 4...27, k: 0...53),
        ]
    }
    return list
}

/// Writes the phantom as the worker writes a viewer cache, and returns the
/// folder and each model's voxel counts.
private func phantom(_ list: [Box]) throws -> (URL, [String: [Int: Int]]) {
    let folder = FileManager.default.temporaryDirectory.appendingPathComponent("bcoa-report-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
    let count = nx * ny * nz
    var image = [Int16](repeating: -1000, count: count)
    var labels: [String: [UInt8]] = [:]
    // The body first, so the organs inside it keep their own values.
    for box in list.sorted(by: { ($0.label == "body" ? 0 : 1) < ($1.label == "body" ? 0 : 1) }) {
        var map = labels[box.model] ?? [UInt8](repeating: 0, count: count)
        for k in box.k { for j in box.j { for i in box.i {
            let index = i + nx * (j + ny * k)
            map[index] = UInt8(id(box.model, box.label))
            image[index] = box.hu
        } } }
        labels[box.model] = map
    }
    try image.withUnsafeBytes { try Data($0).write(to: folder.appendingPathComponent("image.i16")) }
    var models: [String: Any] = [:]
    var counts: [String: [Int: Int]] = [:]
    for (model, map) in labels {
        try Data(map).write(to: folder.appendingPathComponent("labels_\(model).u8"))
        var tally: [Int: Int] = [:]
        for value in map where value != 0 { tally[Int(value), default: 0] += 1 }
        counts[model] = tally
        models[model] = ["file": "labels_\(model).u8", "present": tally.keys.sorted(), "resampled": false]
    }
    let meta: [String: Any] = [
        "version": 1, "size": [nx, ny, nz], "spacing_mm": [5.0, 5.0, 5.0], "origin_mm": [0, 0, 0],
        "direction": [1, 0, 0, 0, 1, 0, 0, 0, 1], "oblique": false, "image": ["file": "image.i16"],
        "models": models,
    ]
    try JSONSerialization.data(withJSONObject: meta).write(to: folder.appendingPathComponent("meta.json"))
    return (folder, counts)
}

/// A content file whose voxel counts are the phantom's, every label of each
/// model listed as the worker lists them.
private func content(_ counts: [String: [Int: Int]], edit: (inout [String: Any]) -> Void = { _ in }) throws
    -> CaseReportContent {
    let models = counts.keys.sorted().map { model -> [String: Any] in
        let structures = catalog.models[model]!.sorted { $0.key < $1.key }.map { labelID, entry -> [String: Any] in
            let voxels = counts[model]?[labelID] ?? 0
            let liver = entry.label == "liver"
            // In the export's spelling, as the worker writes it.
            return [
                "label_id": labelID, "label": LabelNames.sanitized(entry.label), "voxel_count": voxels,
                "volume_ml": liver ? 809.1 : Double(voxels) * 0.125, "hu_mean": voxels > 0 ? 54.25 : NSNull(),
                "hu_sd": voxels > 0 ? 12.5 : NSNull(), "hu_median": voxels > 0 ? 55.0 : NSNull(),
                "flags": voxels == 0 ? ["empty_label"] : [], "excluded": false,
                "note": voxels == 0 ? (entry.label == "vertebra_L6" ? ReportText.accessoryNote : "not found in this CT") : NSNull(),
            ]
        }
        return ["name": model, "key": String(model.dropFirst("clin_ct_".count)), "status": "ok", "structures": structures]
    }
    var object: [String: Any] = [
        "format": 1, "case": "P0001_t1_s1", "pseudonym": "P0001", "timepoint": 1, "series_index": 1,
        "date": ["kind": "days_since_first", "value": 0], "sex": "F", "age_years": 61, "series_key": "s_000001",
        "series": ["description": "Abdomen 1.0 Br40", "modality": "CT", "slice_thickness_mm": 1.0,
                   "pixel_spacing_mm": 0.75, "kernel": "Br40", "kvp": 120, "manufacturer": "SIEMENS",
                   "image_count": 300],
        "device": "mps", "qc": ["status": "accepted", "reviewed": true], "models": models,
        "run_id": "R20261002T120000", "versions": ["moosez": "3.2.2", "app": "0.1.0", "chip": "Apple M4 Pro"],
        "model_checksums": Dictionary(uniqueKeysWithValues: counts.keys.map { ($0, String(repeating: "a", count: 64)) }),
        "methods_text": "Segmentations were generated with MOOSE v3.2.2 (made-up methods text for the test).",
        "notice": IntendedUse.everyPage, "statement": IntendedUse.statement,
    ]
    edit(&object)
    return try CaseReportContent(json: try JSONSerialization.data(withJSONObject: object))
}

private let renderer = CaseReportRenderer(appName: "Body Composition and Organ CT and PET-CT Analyzer", appVersion: "0.1.0")

private func render(_ content: CaseReportContent, volume: ViewerVolume?) throws
    -> (CaseReportRenderer.Output, PDFDocument, URL) {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("\(UUID().uuidString)_\(content.pdfName)")
    let output = try renderer.render(content, catalog: catalog, volume: volume, to: url)
    return (output, try #require(PDFDocument(url: url)), url)
}

/// Page text with every run of white space as one space: PDFKit breaks lines
/// where Core Text wrapped them.
private func text(_ document: PDFDocument, _ page: Int) -> String {
    (document.page(at: page)?.string ?? "").split(whereSeparator: \.isWhitespace).joined(separator: " ")
}

private func flat(_ string: String) -> String { string.split(whereSeparator: \.isWhitespace).joined(separator: " ") }

// -- the tests --------------------------------------------------------------------

@Test func everyPageCarriesTheNoticeThePseudonymAndItsNumber() throws {
    let (folder, counts) = try phantom(boxes())
    let (output, document, _) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    #expect(output.pageCount == document.pageCount)
    #expect(document.pageCount >= 4)
    for page in 0..<document.pageCount {
        let page_ = text(document, page)
        #expect(page_.contains(flat(IntendedUse.everyPage)), "page \(page + 1)")
        #expect(page_.contains("Page \(page + 1) of \(document.pageCount)"))
        #expect(page_.contains("P0001"))
    }
    let first = text(document, 0)
    #expect(first.contains(flat(IntendedUse.statement)))
    #expect(first.contains(ReportText.notInRecord))
    #expect(output.overflows.isEmpty, "\(output.overflows)")
}

@Test func valuesStandAsTheReportFormatsThem() throws {
    let (folder, counts) = try phantom(boxes())
    let (_, document, _) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    let all = (0..<document.pageCount).map { text(document, $0) }.joined(separator: " ")
    #expect(all.contains("809.1"))
    #expect(all.contains("54.3 ± 12.5"))
    // 1.0 is a number, not a flag.
    #expect(all.contains("Slice thickness 1.00 mm"))
    #expect(all.contains("Left kidney"))
    #expect(all.contains("Pulmonary trunk and main branches"))
    // Names from the catalog, never the MOOSE words.
    #expect(!all.contains("kidney_left"))
    // Empty structures go into the list, not into empty rows.
    #expect(all.contains("Not found in this CT:"))
}

@Test func theReportJudgesNothing() throws {
    let (folder, counts) = try phantom(boxes())
    let (_, document, _) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    let all = (0..<document.pageCount).map { text(document, $0) }.joined(separator: " ")
    for word in ["Findings", "Impression", "Recommendation", "abnormal", "Abnormal", "normal range", "reference range",
                 "Signature", "Referring", "subject"] {
        #expect(!all.contains(word), "\(word)")
    }
    // "patient" only in "patient record" (ADR 0017 section 8).
    #expect(all.replacingOccurrences(of: "patient record", with: "").lowercased().contains("patient") == false)
}

@Test func theReferencesPageHoldsTheFixedText() throws {
    let (folder, counts) = try phantom(boxes())
    let (_, document, _) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    // Compared without white space: a URL wrapped after "https://" comes
    // back from PDFKit with a space where the line broke.
    func bare(_ text: String) -> String { text.filter { !$0.isWhitespace } }
    let last = bare(text(document, document.pageCount - 1))
    #expect(last.contains(bare(ReportText.referencesIntro)))
    for reference in ReportText.references {
        #expect(last.contains(bare(reference)), "\(reference.prefix(30))")
    }
    #expect(last.contains(bare(ReportText.licences(moosez: "3.2.2"))))
    #expect(last.contains("https://creativecommons.org/licenses/by/4.0/"))
}

@Test func theWeightsReleaseIsTheOneTheLockDownloads() throws {
    let lock = try String(contentsOf: root.appendingPathComponent("Models/manifest.lock.json"), encoding: .utf8)
    #expect(lock.contains("/\(ReportText.weightsRelease)/"))
}

@Test func metadataNamesTheCaseAndNothingElse() throws {
    let (folder, counts) = try phantom(boxes())
    let (_, _, url) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    let attributes = try #require(PDFDocument(url: url)?.documentAttributes)
    #expect(attributes[PDFDocumentAttribute.titleAttribute] as? String == "Case report P0001_t1_s1")
    #expect((attributes[PDFDocumentAttribute.authorAttribute] as? String ?? "").isEmpty)
    #expect(attributes[PDFDocumentAttribute.subjectAttribute] as? String == IntendedUse.everyPage)
    #expect(attributes[PDFDocumentAttribute.keywordsAttribute] == nil)
}

@Test func theMarkerInThePatientsRightIsOnTheImagesLeft() throws {
    // One label, only in the patient's right (small i).
    let (folder, _) = try phantom([
        Box(model: "clin_ct_organs", label: "liver", i: 2...8, j: 10...20, k: 10...20),
        Box(model: "clin_ct_body", label: "body", i: 0...39, j: 0...29, k: 0...40),
        Box(model: "clin_ct_body", label: "head", i: 10...30, j: 5...25, k: 50...63),
    ])
    let volume = try ViewerVolume(folder: folder)
    for plane in [Plane.coronal, .axial] {
        let image = try #require(ReportImages.render(
            volume, plane: plane, slice: 15, window: .softTissue, model: "clin_ct_organs", cutK: 40, caption: ""))
        #expect(image.letters.left == "R")
        var columns = Set<Int>()
        for row in 0..<image.height {
            for column in 0..<image.width {
                let p = (row * image.width + column) * 4
                // A grey pixel has r == g == b; the overlay tints it.
                if image.rgba[p] != image.rgba[p + 1] || image.rgba[p + 1] != image.rgba[p + 2] { columns.insert(column) }
            }
        }
        #expect(!columns.isEmpty)
        #expect(columns.allSatisfy { $0 < image.width / 2 }, "\(plane)")
    }
}

@Test func noImageReachesTheHead() throws {
    let (folder, counts) = try phantom(boxes())
    let volume = try ViewerVolume(folder: folder)
    let (output, _, _) = try render(try content(counts), volume: volume)
    let cut = try #require(output.analysis.cutK)
    // Below the head (k 54) and at the top of the lungs (k 48).
    #expect(cut == 48)
    let upright = output.images.filter { $0.plane != .axial }
    #expect(!upright.isEmpty)
    for image in output.images {
        #expect(image.kRange.upperBound <= cut, "\(image.caption)")
        #expect(image.kRange.upperBound < 54)
    }
}

@Test func withoutALandmarkThereIsNoCoronalOrSagittalImage() throws {
    // No head, no lungs, no spine; the liver alone says the CT starts below
    // the chest, so the images end at its top.
    let (folder, counts) = try phantom(boxes(landmarks: false))
    let (output, _, _) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    #expect(output.analysis.cutK == 28)

    // Nothing at all to go by: no upright image.
    let (bare, bareCounts) = try phantom([
        Box(model: "clin_ct_cardiac", label: "heart_ventricle_left", i: 21...27, j: 6...12, k: 34...38),
    ])
    let (none, _, _) = try render(try content(bareCounts), volume: try ViewerVolume(folder: bare))
    #expect(none.analysis.cutK == nil)
    #expect(none.images.allSatisfy { $0.plane == .axial })
}

@Test func aMaskThatDisagreesWithTheExportStopsTheReport() throws {
    let (folder, counts) = try phantom(boxes())
    var wrong = counts
    wrong["clin_ct_organs"]![id("clin_ct_organs", "liver")]! += 1
    #expect(throws: CaseReportRenderer.RenderError.self) {
        try render(try content(wrong), volume: try ViewerVolume(folder: folder))
    }
    do {
        _ = try render(try content(wrong), volume: try ViewerVolume(folder: folder))
    } catch CaseReportRenderer.RenderError.refused(let refusal) {
        guard case .voxelMismatch(_, let label, _, _) = refusal else { Issue.record("\(refusal)"); return }
        #expect(label == "liver")
    }
}

@Test func aLabelWithoutANameOrWithAnotherWordStopsTheReport() throws {
    let (_, counts) = try phantom(boxes())
    let unnamed = try content(counts) { object in
        var models = object["models"] as! [[String: Any]]
        var structures = models[0]["structures"] as! [[String: Any]]
        structures.append(["label_id": 250, "label": "mystery", "voxel_count": 0])
        models[0]["structures"] = structures
        object["models"] = models
    }
    #expect(throws: ReportAnalysis.Refusal.unnamed(model: unnamed.models[0].name, label: "mystery")) {
        try ReportAnalysis.checkNames(unnamed, catalog: catalog)
    }
    let renamed = try content(counts) { object in
        var models = object["models"] as! [[String: Any]]
        var structures = models[0]["structures"] as! [[String: Any]]
        structures[0]["label"] = "something_else"
        models[0]["structures"] = structures
        object["models"] = models
    }
    #expect(throws: ReportAnalysis.Refusal.self) { try ReportAnalysis.checkNames(renamed, catalog: catalog) }
}

@Test func aLabelOnTheWrongSideIsMarkedBesideItsValue() throws {
    // The kidneys swapped; the adrenals and the lungs on their sides.
    let (folder, counts) = try phantom(boxes(swap: ["kidney"]))
    let (output, document, _) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    let check = try #require(output.analysis.sides["clin_ct_organs"])
    #expect(check.wrong.count == 2)
    #expect(check.right == 4)
    #expect(!check.hidden)
    let all = (0..<document.pageCount).map { text(document, $0) }.joined(separator: " ")
    #expect(all.contains("lies on the other side of the midline"))
    #expect(all.contains("809.1"))
}

@Test func aModelWithItsSidesSwappedIsNotShown() throws {
    // Kidneys and adrenals swapped: four wrong, two right (the lungs).
    let (folder, counts) = try phantom(boxes(swap: ["kidney", "adrenal_gland"]))
    let (output, document, _) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    #expect(output.analysis.sides["clin_ct_organs"]?.hidden == true)
    let all = (0..<document.pageCount).map { text(document, $0) }.joined(separator: " ")
    #expect(all.contains("Organs model: not shown"))
    // Neither in the tables nor in the tiles.
    #expect(!all.contains("809.1"))
}

@Test func withoutTheViewerCacheTheReportSaysWhatItCouldNotCheck() throws {
    let (_, counts) = try phantom(boxes())
    let (output, document, _) = try render(try content(counts), volume: nil)
    #expect(output.images.isEmpty)
    let all = (0..<document.pageCount).map { text(document, $0) }.joined(separator: " ")
    #expect(all.contains("viewer cache of this series is missing"))
    #expect(all.contains("809.1"))
}

@Test func labelsAreSpelledAsTheExportSpellsThem() {
    // The same cases as the worker's test of naming.sanitize.
    #expect(LabelNames.sanitized("vertebra_L3") == "vertebra_l3")
    #expect(LabelNames.sanitized("Legs") == "legs")
    #expect(LabelNames.sanitized(" kidney-left ") == "kidney_left")
    #expect(LabelNames.sanitized("a__b..c_") == "a_b_c")
}

@Test func numbersKeepTheirPointWhateverTheRegion() {
    #expect(ReportText.number(1234.56) == "1234.6")
    #expect(ReportText.number(-0.04) == "0.0")
    #expect(ReportText.number(nil).isEmpty)
    #expect(ReportText.number(.nan).isEmpty)
    #expect(ReportText.meanSD(54.25, 12.5) == "54.3 ± 12.5")
    #expect(ReportText.number(-2.25) == "-2.3")
    #expect(ReportText.number(1.0, decimals: 2) == "1.00")
}

@Test func theLongestNameFitsItsColumn() throws {
    #expect(catalog.longestName == "Pulmonary trunk and main branches")
    // The phantom's content carries it; no text of the report was cut.
    let (folder, counts) = try phantom(boxes())
    let (output, _, _) = try render(try content(counts), volume: try ViewerVolume(folder: folder))
    #expect(output.overflows.isEmpty)
}

@Test func aContentFileOfAnotherFormatOrWithAPathForANameIsRefused() throws {
    let (_, counts) = try phantom(boxes())
    #expect(throws: CaseReportContent.DecodeError.format(2)) {
        _ = try content(counts) { $0["format"] = 2 }
    }
    #expect(throws: CaseReportContent.DecodeError.missing("case")) {
        _ = try content(counts) { $0["case"] = "../../P0001" }
    }
}

@Test func theCatalogNamesEveryLabelOfEveryModelInTheLock() throws {
    let lock = try JSONSerialization.jsonObject(
        with: Data(contentsOf: root.appendingPathComponent("Models/manifest.lock.json"))) as! [String: Any]
    let models = lock["models"] as! [String: [String: Any]]
    for (model, entry) in models {
        let labels = try #require(entry["labels"] as? [String: String], "\(model)")
        for (labelID, label) in labels {
            let named = try #require(catalog.entry(model: model, labelID: Int(labelID)!), "\(model) \(label)")
            #expect(named.label == label)
            #expect(named.short.count <= 18)
        }
    }
}

/// The reports of a real project's export, when one is named: the
/// robustness run points this at the public case (never at patient data).
@Test(.enabled(if: ProcessInfo.processInfo.environment["BCOA_REPORT_PROJECT"] != nil))
func reportsOfARealProject() throws {
    let project = URL(fileURLWithPath: ProcessInfo.processInfo.environment["BCOA_REPORT_PROJECT"]!)
    let out = URL(fileURLWithPath: ProcessInfo.processInfo.environment["BCOA_REPORT_OUT"]
        ?? FileManager.default.temporaryDirectory.path)
    let files = FileManager.default.enumerator(at: project.appendingPathComponent("exports"), includingPropertiesForKeys: nil)?
        .compactMap { $0 as? URL }.filter { $0.lastPathComponent.hasSuffix(".report.json") } ?? []
    #expect(!files.isEmpty)
    let fonts = ReportFonts(
        regular: root.appendingPathComponent("build/runtime/python/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans.ttf"),
        bold: root.appendingPathComponent("build/runtime/python/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf/DejaVuSans-Bold.ttf"))
    let real = CaseReportRenderer(appName: "Body Composition and Organ CT and PET-CT Analyzer", appVersion: "test", fonts: fonts)
    for file in files {
        let content = try CaseReportContent(json: Data(contentsOf: file))
        var cache = project.appendingPathComponent(PipelineJobs.viewerCacheFolder(content.seriesKey, run: content.runID))
        if !FileManager.default.fileExists(atPath: cache.appendingPathComponent("meta.json").path) {
            cache = project.appendingPathComponent("viewer-cache/\(content.seriesKey)")
        }
        let volume = try? ViewerVolume(folder: cache)
        let start = Date()
        let url = out.appendingPathComponent(content.pdfName)
        let output = try real.render(content, catalog: catalog, volume: volume, to: url)
        let seconds = Date().timeIntervalSince(start)
        let size = (try FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        print("[report] \(content.caseName): \(output.pageCount) pages, \(output.images.count) images, \(String(format: "%.2f", seconds)) s, \(size / 1024) KiB, masks checked \(output.analysis.masksChecked), cut \(output.analysis.cutLandmark ?? "none")")
        #expect(output.overflows.isEmpty, "\(output.overflows)")
        let document = try #require(PDFDocument(url: url))
        let all = (0..<document.pageCount).map { text(document, $0) }.joined(separator: " ")
        for page in 0..<document.pageCount {
            #expect(text(document, page).contains(flat(IntendedUse.everyPage)))
        }
        // Every value of the content file stands in the text as formatted,
        // but for a structure a dedicated model segments as well: the lungs
        // model's lobes stand there instead of the organs model's.
        let dedicated = Set(content.models.filter { $0.name == "clin_ct_lungs" && $0.status == "ok" }
            .flatMap { $0.structures.map(\.label) })
        for model in content.models where model.status == "ok" {
            for structure in model.structures where !structure.notFound && !structure.excluded {
                if model.name == "clin_ct_organs" && dedicated.contains(structure.label) { continue }
                if let volume = structure.volumeML {
                    #expect(all.contains(ReportText.number(volume)), "\(model.name) \(structure.label)")
                }
            }
        }
    }
}

@Test func aBatchDrawsEachReportBesideItsContentAndOnlyThere() throws {
    let project = FileManager.default.temporaryDirectory.appendingPathComponent("bcoa-project-\(UUID().uuidString)")
    let reports = project.appendingPathComponent("exports/cohort_reports")
    try FileManager.default.createDirectory(at: reports, withIntermediateDirectories: true)
    let (folder, counts) = try phantom(boxes())
    let cache = project.appendingPathComponent("viewer-cache/R20261002T120000")
    try FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true)
    try FileManager.default.moveItem(at: folder, to: cache.appendingPathComponent("s_000001"))
    var object = try JSONSerialization.jsonObject(with: Data(contentsOf: try {
        // The same content the other tests draw, as a file.
        let url = reports.appendingPathComponent("P0001_t1_s1.report.json")
        _ = try content(counts) { object in
            try? JSONSerialization.data(withJSONObject: object).write(to: url)
        }
        return url
    }())) as! [String: Any]
    object["case"] = "P0002_t1_s1"
    object["pseudonym"] = "P0002"
    try JSONSerialization.data(withJSONObject: object).write(to: reports.appendingPathComponent("P0002_t1_s1.report.json"))

    let drawn = CaseReportBatch.draw(
        project: project,
        contents: ["exports/cohort_reports/P0001_t1_s1.report.json", "exports/cohort_reports/P0002_t1_s1.report.json",
                   "exports/../project.sqlite.report.json", "exports/cohort_reports/missing.report.json"],
        catalog: catalog, renderer: renderer)
    #expect(drawn[0].pdf == "exports/cohort_reports/P0001_t1_s1_case_report.pdf")
    #expect(drawn[0].error == nil)
    #expect(drawn[0].pages > 3)
    let data = try Data(contentsOf: project.appendingPathComponent(drawn[0].pdf!))
    #expect(drawn[0].sha256?.count == 64)
    #expect(!data.isEmpty)
    #expect(drawn[1].error == nil)
    // A path out of exports/ and a missing file are errors, not crashes.
    #expect(drawn[2].pdf == nil && drawn[2].error != nil)
    #expect(drawn[3].pdf == nil && drawn[3].error != nil)
    #expect(try FileManager.default.contentsOfDirectory(atPath: reports.path).filter { $0.hasPrefix(".") }.isEmpty)
}
