import CoreGraphics
import CoreText
import Foundation

/// Draws one case report as an A4 PDF (ADR 0017): the worker's content file
/// for the numbers, the viewer cache for the images, the name catalog for the
/// names. It states values and never judges them: no reference values, no
/// marks on values, no "findings", and every page carries the notice that
/// this is research software and not a medical device.
public struct CaseReportRenderer: Sendable {
    public struct ImageRecord: Sendable {
        public var caption: String
        public var plane: Plane
        public var kRange: ClosedRange<Int>
        public var page: Int
    }

    public struct Output: Sendable {
        public var pageCount: Int
        /// Texts that did not fit their frame; empty in a good report.
        public var overflows: [String]
        public var images: [ImageRecord]
        public var analysis: ReportAnalysis
    }

    public enum RenderError: Error, CustomStringConvertible {
        case refused(ReportAnalysis.Refusal)
        case pdf(String)

        public var description: String {
            switch self {
            case .refused(let refusal): "The report was not drawn: \(refusal)."
            case .pdf(let text): text
            }
        }
    }

    public var appName: String
    public var appVersion: String
    public var fonts: ReportFonts

    public init(appName: String, appVersion: String, fonts: ReportFonts = ReportFonts()) {
        self.appName = appName
        self.appVersion = appVersion
        self.fonts = fonts
    }

    /// Writes the PDF to `url` through a partial file, so a report that
    /// fails half way leaves no half a file under the final name.
    @discardableResult
    public func render(
        _ content: CaseReportContent, catalog: StructureCatalog, volume: ViewerVolume?, to url: URL
    ) throws -> Output {
        let analysis: ReportAnalysis
        do {
            analysis = try ReportAnalysis.analyse(content, catalog: catalog, volume: volume)
        } catch let refusal as ReportAnalysis.Refusal {
            throw RenderError.refused(refusal)
        }
        var builder = Builder(content: content, catalog: catalog, volume: volume, analysis: analysis,
                              style: ReportStyle(fonts), appName: appName)
        builder.build()
        let layout = builder.layout

        var mediaBox = CGRect(origin: .zero, size: ReportLayout.size)
        // Title with the case name only, no author, the notice as subject and
        // no keywords (ADR 0017 section 8).
        let info: [CFString: Any] = [
            kCGPDFContextTitle: "Case report \(content.caseName)",
            kCGPDFContextAuthor: "",
            kCGPDFContextSubject: IntendedUse.everyPage,
            kCGPDFContextCreator: "\(appName) \(appVersion)",
        ]
        let partial = url.deletingLastPathComponent().appendingPathComponent(".\(url.lastPathComponent).partial")
        try? FileManager.default.removeItem(at: partial)
        guard let context = CGContext(partial as CFURL, mediaBox: &mediaBox, info as CFDictionary) else {
            throw RenderError.pdf("The PDF could not be created in \(url.deletingLastPathComponent().lastPathComponent).")
        }
        for page in layout.pages {
            context.beginPDFPage(nil)
            for draw in page { draw(context) }
            context.endPDFPage()
        }
        context.closePDF()
        guard rename(partial.path, url.path) == 0 else {
            try? FileManager.default.removeItem(at: partial)
            throw RenderError.pdf("The PDF could not be saved: \(String(cString: strerror(errno))).")
        }
        return Output(pageCount: layout.pages.count, overflows: layout.overflows, images: builder.images,
                      analysis: analysis)
    }
}

// MARK: - The pages

private struct Builder {
    let content: CaseReportContent
    let catalog: StructureCatalog
    let volume: ViewerVolume?
    let analysis: ReportAnalysis
    let style: ReportStyle
    let appName: String
    let layout = ReportLayout()
    var images: [CaseReportRenderer.ImageRecord] = []

    init(content: CaseReportContent, catalog: StructureCatalog, volume: ViewerVolume?, analysis: ReportAnalysis,
         style: ReportStyle, appName: String) {
        self.content = content
        self.catalog = catalog
        self.volume = volume
        self.analysis = analysis
        self.style = style
        self.appName = appName
    }

    struct Row {
        var model: CaseReportContent.Model
        var structure: CaseReportContent.Structure
        var entry: StructureCatalog.Entry
    }

    var moosez: String { content.versions["moosez"] ?? "(version unknown)" }

    /// Models whose sides came out swapped are not shown (ADR 0017 section 7).
    var hidden: Set<String> { Set(analysis.sides.filter { $0.value.hidden }.map(\.key)) }

    mutating func build() {
        overview()
        let rows = structureRows()
        for group in catalog.groups {
            section(group, rows: rows[group] ?? [])
        }
        definitions()
        methods()
        frame()
    }

    // -- page 1 ---------------------------------------------------------------

    mutating func overview() {
        layout.paragraph(style.text("Case report \(content.caseName)", .title), after: 2)
        layout.paragraph(style.text(
            "Automated CT segmentation and measurement with \(appName), for research.", .body,
            color: ReportStyle.muted), after: 10)

        // The notice box: the per-page notice, the full statement verbatim and
        // the sentence that the report is not part of the record.
        let notice = style.text("\(IntendedUse.everyPage)\n", .bold)
        notice.append(style.text("\(IntendedUse.statement)\n\(ReportText.notInRecord) \(ReportText.checkEveryValue)"))
        let pad: CGFloat = 8
        let height = layout.measure(notice, width: layout.width - 2 * pad) + 2 * pad
        layout.ensure(height)
        layout.fill(CGRect(x: layout.left, y: layout.y, width: layout.width, height: height),
                    color: ReportStyle.notice, stroke: ReportStyle.rule)
        layout.place(notice, x: layout.left + pad, y: layout.y + pad, width: layout.width - 2 * pad,
                     height: height - 2 * pad)
        layout.y += height + 12

        heading("Case")
        let s = content.series
        let facts: [(String, String?)] = [
            ("Pseudonym", content.pseudonym),
            ("Time point", ReportText.timepoint(content)),
            ("Sex (as recorded)", content.sex),
            ("Age at first study", content.ageYears.map { "\($0) years" }),
            ("Series", "\(content.seriesIndex) of the study: \(s.description ?? "no description")"),
            ("Modality, images", [s.modality, s.imageCount.map { "\($0) images" }].compactMap { $0 }.joined(separator: ", ")),
            ("Slice thickness", s.sliceThicknessMM.map { "\(ReportText.number($0, decimals: 2)) mm" }),
            ("Pixel spacing", s.pixelSpacingMM.map { "\(ReportText.number($0, decimals: 3)) mm" }),
            ("Kernel", s.kernel),
            ("Tube voltage", s.kvp.map { "\(ReportText.number($0, decimals: 0)) kV" }),
            ("Manufacturer", s.manufacturer),
            ("Contrast agent (as recorded)", s.contrastAgent),
            ("Computed on", content.device),
            ("Visual QC", ReportText.qc(status: content.qcStatus, reviewed: content.reviewed)),
            ("Run", content.runID),
        ]
        keyValues(facts.map { ($0.0, $0.1 ?? "not recorded") })

        heading("Segmentation")
        let modelRows = content.models.map { model -> [NSAttributedString] in
            let found = model.structures.filter { !$0.notFound && !$0.excluded && $0.voxelCount != nil }.count
            let status = hidden.contains(model.name)
                ? "not shown: left and right swapped (see quality checks)"
                : ReportText.modelStatus(model.status)
            return [
                style.text(ReportText.modelName(model.key)),
                style.text(status),
                style.text(model.status == "ok" ? "\(found) of \(model.structures.count)" : "",
                           alignment: .right),
            ]
        }
        layout.table([
            .init(title: "Model", width: 170), .init(title: "Status", width: 220),
            .init(title: "Structures found", width: layout.width - 390, alignment: .right),
        ], rows: modelRows, style: style)
        layout.y += 4
        layout.paragraph(style.text(
            "Segmentations by MOOSE \(moosez); see Methods and references. Values are shown as measured, " +
            "without reference values and without any assessment.", .small, color: ReportStyle.muted), after: 10)

        keyFigures()
        overviewImage()
    }

    mutating func keyFigures() {
        let figures: [(model: String, label: String)] = [
            ("clin_ct_organs", "liver"), ("clin_ct_organs", "spleen"), ("clin_ct_organs", "kidney_right"),
            ("clin_ct_organs", "kidney_left"), ("clin_ct_body_composition", "skeletal_muscle"),
            ("clin_ct_body_composition", "visceral_fat"),
        ]
        let tiles = figures.compactMap { figure -> (String, String)? in
            guard let model = content.models.first(where: { $0.name == figure.model }),
                  let structure = model.structures.first(where: { $0.label == figure.label }),
                  let entry = catalog.entry(model: model.name, labelID: structure.labelID)
            else { return nil }
            let value = model.status == "ok" && !hidden.contains(model.name) && structure.volumeML != nil
                ? "\(ReportText.number(structure.volumeML)) mL" : "–"
            return (entry.name, value)
        }
        guard !tiles.isEmpty else { return }
        heading("Volumes at a glance")
        let gap: CGFloat = 8
        let tileWidth = (layout.width - 2 * gap) / 3
        let tileHeight: CGFloat = 40
        for start in stride(from: 0, to: tiles.count, by: 3) {
            layout.ensure(tileHeight)
            for (offset, tile) in tiles[start..<min(start + 3, tiles.count)].enumerated() {
                let x = layout.left + CGFloat(offset) * (tileWidth + gap)
                layout.fill(CGRect(x: x, y: layout.y, width: tileWidth, height: tileHeight),
                            color: ReportStyle.shade)
                layout.place(style.text(tile.0, .small, color: ReportStyle.muted), x: x + 6, y: layout.y + 4,
                             width: tileWidth - 12, height: 12)
                layout.place(style.text(tile.1, .value), x: x + 6, y: layout.y + 17, width: tileWidth - 12,
                             height: 20)
            }
            layout.y += tileHeight + gap
        }
        layout.y += 4
    }

    mutating func overviewImage() {
        guard let volume, let cut = analysis.cutK,
              let model = content.models.first(where: { $0.name == "clin_ct_organs" && $0.status == "ok" }),
              !hidden.contains(model.name),
              let stats = analysis.statistics[model.name]
        else { return }
        let trunk = model.structures.filter { !["brain", "thyroid_left", "thyroid_right"].contains($0.label) }
            .compactMap { stats[$0.labelID] }
        guard let j = ReportImages.slice(through: trunk, plane: .coronal),
              let image = ReportImages.render(
                volume, plane: .coronal, slice: j, window: .softTissue, model: model.name, cutK: cut,
                caption: "Coronal, soft-tissue window (W 400 / L 40), organs model", names: names(model))
        else { return }
        // The heading moves with the image rather than stay behind alone.
        layout.ensure(blockHeight([image], legend: true) + 30)
        heading("Overview")
        place([image], legend: true)
    }

    // -- the structure pages --------------------------------------------------

    /// Every structure under its group, once: where two models segment the
    /// same structure, the dedicated model's (the one with fewer labels; the
    /// lungs model's lobes rather than the organs model's).
    func structureRows() -> [String: [Row]] {
        var chosen: [String: Row] = [:]
        for model in content.models where model.status == "ok" && !hidden.contains(model.name) {
            for structure in model.structures {
                guard let entry = catalog.entry(model: model.name, labelID: structure.labelID) else { continue }
                let row = Row(model: model, structure: structure, entry: entry)
                if let existing = chosen[structure.label], !prefers(model.name, over: existing.model.name) {
                    continue
                }
                chosen[structure.label] = row
            }
        }
        return Dictionary(grouping: chosen.values, by: \.entry.group)
            .mapValues { $0.sorted { ($0.entry.order, $0.entry.name) < ($1.entry.order, $1.entry.name) } }
    }

    func prefers(_ model: String, over other: String) -> Bool {
        let a = catalog.models[model]?.count ?? .max
        let b = catalog.models[other]?.count ?? .max
        if a != b { return a < b }
        return !model.contains("fast") && other.contains("fast")
    }

    /// The group a model's labels mostly belong to, for its "did not run" line.
    func mainGroup(of model: String) -> String? {
        let groups = (catalog.models[model] ?? [:]).values.map(\.group)
        return Dictionary(grouping: groups, by: { $0 }).max { $0.value.count < $1.value.count }?.key
    }

    func names(_ model: CaseReportContent.Model) -> [Int: String] {
        Dictionary(uniqueKeysWithValues: model.structures.compactMap { s in
            catalog.entry(model: model.name, labelID: s.labelID).map { (s.labelID, $0.short) }
        })
    }

    mutating func section(_ group: String, rows: [Row]) {
        var lines: [String] = []
        for model in content.models where mainGroup(of: model.name) == group {
            if hidden.contains(model.name) {
                lines.append("\(ReportText.modelName(model.key)) model: not shown, because most of its left and right labels lie on the opposite side of the midline (see quality checks).")
            } else if model.status != "ok" {
                lines.append("\(ReportText.modelName(model.key)) model: \(ReportText.modelStatus(model.status)); no values.")
            }
        }
        guard !rows.isEmpty || !lines.isEmpty else { return }
        layout.ensure(90)
        heading(group, size: .heading)
        if group == "Body composition" {
            layout.paragraph(style.text(
                "Measured at the L3 level only: the MOOSE body composition model segments a slab of slices at the third lumbar vertebra, not the whole body.",
                .small, color: ReportStyle.muted))
        }
        let found = rows.filter { !$0.structure.notFound }
        let missing = rows.filter(\.structure.notFound)
        if !found.isEmpty {
            let wrongSide = Set(analysis.sides.flatMap { model, check in check.wrong.map { "\(model)/\($0)" } })
            let cells = found.map { row -> [NSAttributedString] in
                let s = row.structure
                var notes = [s.note].compactMap { $0 }
                if wrongSide.contains("\(row.model.name)/\(s.labelID)") {
                    notes.append("lies on the other side of the midline; check the mask")
                }
                return [
                    style.text(row.entry.name),
                    style.text(ReportText.number(s.volumeML), alignment: .right),
                    style.text(ReportText.meanSD(s.huMean, s.huSD), alignment: .right),
                    style.text(ReportText.number(s.huMedian), alignment: .right),
                    style.text(notes.joined(separator: "; "), .small),
                ]
            }
            layout.table([
                .init(title: "Structure", width: 160), .init(title: "Volume (mL)", width: 62, alignment: .right),
                .init(title: "Mean ± SD (HU)", width: 86, alignment: .right),
                .init(title: "Median (HU)", width: 66, alignment: .right),
                .init(title: "Note", width: layout.width - 374),
            ], rows: cells, style: style)
            layout.y += 4
        }
        let accessory = missing.filter { $0.structure.note == ReportText.accessoryNote }
        let absent = missing.filter { $0.structure.note != ReportText.accessoryNote }
        if !absent.isEmpty {
            layout.paragraph(style.text(
                "Not found in this CT: \(absent.map(\.entry.name).joined(separator: ", ")).", .small))
        }
        if !accessory.isEmpty {
            layout.paragraph(style.text(
                "Not present, structures that only some people have: \(accessory.map(\.entry.name).joined(separator: ", ")).",
                .small))
        }
        for line in lines { layout.paragraph(style.text(line, .small)) }
        groupImages(group)
        layout.y += 8
    }

    struct ImageSpec {
        var plane: Plane
        var window: WindowPreset
        var models: [String]
        var labels: [String]?
        var caption: String
    }

    func specs(_ group: String) -> [ImageSpec] {
        let soft = "soft-tissue window (W 400 / L 40)"
        let bone = "bone window (W 1800 / L 400)"
        switch group {
        case "Organs":
            return [
                ImageSpec(plane: .axial, window: .softTissue, models: ["clin_ct_organs"], labels: ["liver", "spleen"],
                          caption: "Axial through liver and spleen, \(soft)"),
                ImageSpec(plane: .axial, window: .softTissue, models: ["clin_ct_organs"],
                          labels: ["kidney_right", "kidney_left"], caption: "Axial through the kidneys, \(soft)"),
                ImageSpec(plane: .axial, window: .softTissue, models: ["clin_ct_organs"], labels: ["pancreas"],
                          caption: "Axial through the pancreas, \(soft)"),
                ImageSpec(plane: .axial, window: .softTissue, models: ["clin_ct_organs"], labels: ["bladder"],
                          caption: "Axial through the urinary bladder, \(soft)"),
            ]
        case "Lungs and airways":
            return [ImageSpec(plane: .coronal, window: .lung, models: ["clin_ct_lungs", "clin_ct_organs"],
                              labels: ReportAnalysis.lungLabels, caption: "Coronal, lung window (W 1500 / L −600)")]
        case "Digestive tract":
            return [ImageSpec(plane: .coronal, window: .softTissue, models: ["clin_ct_digestive"], labels: nil,
                              caption: "Coronal, \(soft)")]
        case "Heart and vessels":
            return [
                ImageSpec(plane: .axial, window: .mediastinum, models: ["clin_ct_cardiac"],
                          labels: ["heart_atrium_left", "heart_atrium_right", "heart_ventricle_left", "heart_ventricle_right"],
                          caption: "Axial through the heart, mediastinal window (W 350 / L 50)"),
                ImageSpec(plane: .coronal, window: .softTissue, models: ["clin_ct_cardiac"],
                          labels: ["aorta", "inferior_vena_cava"], caption: "Coronal through aorta and vena cava, \(soft)"),
            ]
        case "Muscles":
            return [ImageSpec(plane: .coronal, window: .softTissue, models: ["clin_ct_muscles"], labels: nil,
                              caption: "Coronal, \(soft)")]
        case "Spine":
            return [ImageSpec(plane: .sagittal, window: .bone, models: ["clin_ct_vertebrae", "clin_ct_fast_vertebrae"],
                              labels: nil, caption: "Sagittal through the spine, \(bone)")]
        case "Sternum and ribs":
            return [ImageSpec(plane: .coronal, window: .bone, models: ["clin_ct_ribs"], labels: nil,
                              caption: "Coronal, \(bone)")]
        case "Other bones":
            return [ImageSpec(plane: .coronal, window: .bone, models: ["clin_ct_peripheral_bones"], labels: nil,
                              caption: "Coronal, \(bone)")]
        case "Body composition":
            return [ImageSpec(plane: .axial, window: .softTissue, models: ["clin_ct_body_composition"], labels: nil,
                              caption: "Axial in the L3 slab, \(soft)")]
        default:
            return []
        }
    }

    mutating func groupImages(_ group: String) {
        guard let volume else { return }
        var drawn: [ReportImage] = []
        let shown = content.models.filter { $0.status == "ok" && !hidden.contains($0.name) }
        for spec in specs(group) {
            guard let model = spec.models.compactMap({ name in shown.first { $0.name == name } }).first,
                  let stats = analysis.statistics[model.name]
            else { continue }
            let chosen = model.structures.filter { structure in
                // The brain, the thyroid and the skull never choose a slice;
                // the images end below the neck anyway.
                !["brain", "thyroid_left", "thyroid_right", "skull"].contains(structure.label)
                    && (spec.labels?.contains(structure.label) ?? true)
            }.compactMap { stats[$0.labelID] }
            guard let slice = ReportImages.slice(through: chosen, plane: spec.plane),
                  let image = ReportImages.render(
                    volume, plane: spec.plane, slice: slice, window: spec.window, model: model.name,
                    cutK: analysis.cutK, caption: spec.caption, names: names(model))
            else { continue }
            drawn.append(image)
        }
        guard !drawn.isEmpty else { return }
        layout.y += 4
        for start in stride(from: 0, to: drawn.count, by: 2) {
            place(Array(drawn[start..<min(start + 2, drawn.count)]), legend: false)
        }
    }

    static let imageGap: CGFloat = 12
    static let captionHeight: CGFloat = 22

    func slot(_ row: [ReportImage]) -> CGFloat {
        row.count == 1 ? min(layout.width, 300) : (layout.width - Self.imageGap) / 2
    }

    func sizes(_ row: [ReportImage]) -> [CGSize] {
        let slot = slot(row)
        let maxHeight: CGFloat = 300
        return row.map { image -> CGSize in
            var width = slot
            var height = width * CGFloat(image.aspect)
            if height > maxHeight {
                height = maxHeight
                width = height / CGFloat(image.aspect)
            }
            return CGSize(width: width, height: height)
        }
    }

    func blockHeight(_ row: [ReportImage], legend: Bool) -> CGFloat {
        let legendHeight = legend
            ? layout.measure(legendString(row[0]), width: layout.width - slot(row) - Self.imageGap) : 0
        return max(sizes(row).map(\.height).max() ?? 0, legendHeight) + Self.captionHeight
    }

    /// One or two images side by side, with letters, a 5 cm bar and captions.
    mutating func place(_ row: [ReportImage], legend: Bool) {
        let gap = Self.imageGap
        let slot = slot(row)
        let sizes = sizes(row)
        let captionHeight = Self.captionHeight
        let legendText = legend ? legendString(row[0]) : nil
        let legendWidth = layout.width - slot - gap
        let legendHeight = legendText.map { layout.measure($0, width: legendWidth) } ?? 0
        let tallest = max(sizes.map(\.height).max() ?? 0, legendHeight)
        layout.ensure(tallest + captionHeight)
        for (index, (image, size)) in zip(row, sizes).enumerated() {
            let x = layout.left + CGFloat(index) * (slot + gap) + (slot - size.width) / 2
            let rect = CGRect(x: x, y: layout.y, width: size.width, height: size.height)
            draw(image, in: rect)
            layout.place(style.text(image.caption, .tiny, color: ReportStyle.muted), x: layout.left + CGFloat(index) * (slot + gap),
                         y: layout.y + size.height + 3, width: slot, height: captionHeight - 3)
            images.append(.init(caption: image.caption, plane: image.plane, kRange: image.kRange,
                                page: layout.pageIndex + 1))
        }
        if let legendText {
            layout.place(legendText, x: layout.left + slot + gap, y: layout.y, width: legendWidth, height: legendHeight)
        }
        layout.y += tallest + captionHeight + 6
    }

    func legendString(_ image: ReportImage) -> NSAttributedString {
        let text = NSMutableAttributedString()
        for (index, item) in image.legend.enumerated() {
            let swatch = style.text("■ ", .small, color: CGColor(
                srgbRed: CGFloat(item.color.r) / 255, green: CGFloat(item.color.g) / 255,
                blue: CGFloat(item.color.b) / 255, alpha: 1))
            text.append(swatch)
            text.append(style.text(item.name + (index < image.legend.count - 1 ? "\n" : ""), .small))
        }
        return text
    }

    func draw(_ image: ReportImage, in rect: CGRect) {
        guard let cgImage = image.cgImage() else { return }
        let height = ReportLayout.size.height
        let flipped = CGRect(x: rect.minX, y: height - rect.maxY, width: rect.width, height: rect.height)
        layout.add { context in
            context.interpolationQuality = .high
            context.draw(cgImage, in: flipped)
        }
        // Orientation letters at the edges, as the viewer shows them.
        let letters = image.letters
        let size: CGFloat = 11
        let marks: [(String, CGFloat, CGFloat)] = [
            (letters.left, rect.minX + 3, rect.midY - size / 2),
            (letters.right, rect.maxX - size - 3, rect.midY - size / 2),
            (letters.top, rect.midX - size / 2, rect.minY + 3),
            (letters.bottom, rect.midX - size / 2, rect.maxY - size - 3),
        ]
        for (letter, x, y) in marks {
            layout.fill(CGRect(x: x, y: y, width: size, height: size), color: CGColor(gray: 0, alpha: 0.55))
            layout.place(style.text(letter, .smallBold, color: CGColor(gray: 1, alpha: 1), alignment: .center),
                         x: x, y: y + 1, width: size, height: size)
        }
        // A 5 cm bar along the bottom edge.
        let points = CGFloat(50 / image.spacing.horizontal) * rect.width / CGFloat(image.width)
        if points < rect.width * 0.6 {
            let barY = rect.maxY - 6
            let x0 = rect.maxX - points - 18
            layout.add { context in
                context.setStrokeColor(CGColor(gray: 1, alpha: 1))
                context.setLineWidth(1.5)
                context.move(to: CGPoint(x: x0, y: height - barY))
                context.addLine(to: CGPoint(x: x0 + points, y: height - barY))
                context.strokePath()
            }
            layout.place(style.text("5 cm", .tiny, color: CGColor(gray: 1, alpha: 1), alignment: .center),
                         x: x0, y: barY - 10, width: points, height: 9)
        }
    }

    // -- definitions and checks ---------------------------------------------

    mutating func definitions() {
        layout.newPage()
        heading("Definitions and quality checks", size: .heading)
        layout.table([.init(title: "Value", width: 120), .init(title: "Definition", width: layout.width - 120)],
                     rows: ReportText.definitions.map { [style.text($0.term, .bold), style.text($0.text)] },
                     style: style)
        layout.y += 10
        heading("Notes beside the values")
        layout.table([.init(title: "Note", width: 190), .init(title: "Meaning", width: layout.width - 190)],
                     rows: ReportText.flags.map { [style.text($0.flag), style.text($0.text)] }, style: style)
        layout.y += 10

        heading("Checks of this report")
        var checks: [(String, String)] = [
            ("Visual QC", ReportText.qc(status: content.qcStatus, reviewed: content.reviewed)),
        ]
        if volume == nil {
            checks.append(("Masks", "Not checked: the viewer cache of this series is missing. Open the series in the viewer to rebuild it, then create the report again."))
        } else {
            var text = "The voxel counts of \(analysis.masksChecked) structures agree with their masks."
            if !analysis.masksMissing.isEmpty {
                text += " Not checked, because the viewer cache holds no mask: "
                    + analysis.masksMissing.map { name in
                        ReportText.modelName(content.models.first { $0.name == name }?.key ?? name)
                    }
                        .joined(separator: ", ") + "."
            }
            checks.append(("Masks", text))
        }
        for model in content.models {
            guard let check = analysis.sides[model.name] else { continue }
            let name = ReportText.modelName(model.key)
            let label: (Int) -> String = { id in
                catalog.entry(model: model.name, labelID: id)?.name ?? "label \(id)"
            }
            var text: String
            if check.hidden {
                text = "Most left and right labels lie on the opposite side of the midline; the model's values are not shown."
            } else if check.wrong.isEmpty {
                text = "Left and right agree with the anatomy for \(check.right) label\(check.right == 1 ? "" : "s")."
            } else {
                text = "On the other side of the midline: \(check.wrong.map(label).joined(separator: ", ")); marked beside the value."
            }
            if !check.unchecked.isEmpty {
                text += " Within 15 mm of the midline, not checked: \(check.unchecked.map(label).joined(separator: ", "))."
            }
            checks.append(("Sides, \(name)", text))
        }
        if analysis.geometry != nil {
            checks.append(("Midline", analysis.midline.map { "Taken as \($0)." } ?? "No midline found; the sides were not checked."))
        }
        if volume != nil {
            checks.append(("Images", analysis.cutK != nil
                ? "Coronal and sagittal images end \(analysis.cutLandmark ?? "below the neck"), so that no image shows a face; brain and thyroid appear in the tables only. The images show what the numbers rest on; they are not for reading the CT."
                : "No coronal or sagittal images: no landmark showed where the neck is, so the face could not be kept out."))
        } else {
            checks.append(("Images", "None: the viewer cache of this series is missing."))
        }
        layout.table([.init(title: "Check", width: 120), .init(title: "Result", width: layout.width - 120)],
                     rows: checks.map { [style.text($0.0, .bold), style.text($0.1)] }, style: style)
    }

    // -- methods and references ---------------------------------------------

    mutating func methods() {
        layout.newPage()
        heading("Methods and references", size: .heading)
        heading("Methods (the export's paragraph)")
        layout.paragraph(style.text(content.methodsText.isEmpty ? "Not recorded." : content.methodsText), after: 8)

        heading("Versions")
        keyValues(content.versions.sorted { $0.key.lowercased() < $1.key.lowercased() }.map { ($0.key, $0.value) })
        heading("Model archives (SHA-256)")
        keyValues(content.models.map { ($0.name, content.modelChecksums[$0.name] ?? "not recorded") }, small: true)

        heading("References")
        layout.paragraph(style.text(ReportText.referencesIntro), after: 4)
        for reference in ReportText.references {
            layout.paragraph(superscript18(style.text(reference)), indent: 8, after: 3)
        }
        layout.y += 6
        heading("Software and licenses")
        layout.paragraph(style.text(ReportText.licences(moosez: moosez)), after: 8)
        heading("Intended use")
        layout.paragraph(style.text(IntendedUse.everyPage, .bold), after: 2)
        layout.paragraph(style.text(IntendedUse.statement))
        layout.paragraph(style.text(ReportText.notInRecord))
    }

    /// The 18 of 18F raised, as the approved page sets it.
    func superscript18(_ text: NSMutableAttributedString) -> NSMutableAttributedString {
        let string = text.string as NSString
        var range = string.range(of: "18F")
        while range.location != NSNotFound {
            text.addAttribute(NSAttributedString.Key(kCTSuperscriptAttributeName as String), value: 1,
                              range: NSRange(location: range.location, length: 2))
            let next = range.location + range.length
            range = string.range(of: "18F", range: NSRange(location: next, length: string.length - next))
        }
        return text
    }

    // -- pieces -----------------------------------------------------------------

    func heading(_ text: String, size: ReportStyle.Kind = .subheading) {
        layout.ensure(40)
        layout.y += size == .heading ? 4 : 2
        layout.paragraph(style.text(text, size), after: 4)
    }

    func keyValues(_ pairs: [(String, String)], small: Bool = false) {
        layout.table([.init(title: "", width: 150), .init(title: "", width: layout.width - 150)],
                     rows: pairs.map { [style.text($0.0, .bold), style.text($0.1, small ? .small : .body)] },
                     style: style, header: false)
        layout.y += 8
    }

    /// Header and footer on every page, now that the page count is known.
    func frame() {
        let total = layout.pages.count
        let qc = "Visual QC: \(ReportText.qc(status: content.qcStatus, reviewed: content.reviewed))"
        let rightWidth: CGFloat = 150
        for page in 0..<total {
            let header = "Case \(content.pseudonym) · time point \(content.timepoint) · series \(content.seriesIndex)"
            layout.place(style.text(header, .smallBold), x: layout.left, y: 30, width: layout.width - rightWidth,
                         height: 12, page: page)
            layout.place(style.text("Research case report", .small, color: ReportStyle.muted, alignment: .right),
                         x: layout.left + layout.width - rightWidth, y: 30, width: rightWidth, height: 12, page: page)
            layout.rule(at: 46, page: page)

            let footerY = ReportLayout.size.height - 58
            layout.rule(at: footerY, page: page)
            layout.place(style.text(IntendedUse.everyPage, .smallBold), x: layout.left, y: footerY + 6,
                         width: layout.width - rightWidth - 10, height: 22, page: page)
            layout.place(style.text(qc, .tiny, color: ReportStyle.muted), x: layout.left, y: footerY + 30,
                         width: layout.width - rightWidth - 10, height: 12, page: page)
            layout.place(style.text("Page \(page + 1) of \(total)", .small, alignment: .right),
                         x: layout.left + layout.width - rightWidth, y: footerY + 6, width: rightWidth, height: 12,
                         page: page)
            layout.place(style.text(content.pseudonym, .tiny, color: ReportStyle.muted, alignment: .right),
                         x: layout.left + layout.width - rightWidth, y: footerY + 20, width: rightWidth, height: 10,
                         page: page)
        }
    }
}
