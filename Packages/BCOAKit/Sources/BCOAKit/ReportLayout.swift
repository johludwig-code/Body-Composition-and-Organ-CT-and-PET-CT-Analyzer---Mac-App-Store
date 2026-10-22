import CoreGraphics
import CoreText
import Foundation

/// The fonts of the report. DejaVu Sans from the bundle's matplotlib (ADR
/// 0017 section 9), read straight from its file so that nothing is
/// registered or installed; Helvetica where no file is given, as in the
/// package's tests.
public struct ReportFonts: Sendable {
    public var regular: URL?
    public var bold: URL?

    public init(regular: URL? = nil, bold: URL? = nil) {
        self.regular = regular
        self.bold = bold
    }

    /// The DejaVu files inside the app's embedded Python, where they are.
    public static func bundled(resources: URL) -> ReportFonts {
        let folder = resources.appendingPathComponent(
            "python/lib/python3.12/site-packages/matplotlib/mpl-data/fonts/ttf")
        func file(_ name: String) -> URL? {
            let url = folder.appendingPathComponent(name)
            return FileManager.default.fileExists(atPath: url.path) ? url : nil
        }
        return ReportFonts(regular: file("DejaVuSans.ttf"), bold: file("DejaVuSans-Bold.ttf"))
    }

    func font(_ size: CGFloat, bold: Bool) -> CTFont {
        if let url = bold ? self.bold : regular,
           let descriptors = CTFontManagerCreateFontDescriptorsFromURL(url as CFURL) as? [CTFontDescriptor],
           let first = descriptors.first {
            return CTFontCreateWithFontDescriptor(first, size, nil)
        }
        return CTFontCreateWithName((bold ? "Helvetica-Bold" : "Helvetica") as CFString, size, nil)
    }
}

/// Text styles, built once per report.
struct ReportStyle {
    enum Kind { case title, heading, subheading, body, small, tiny, bold, smallBold, value }

    let fonts: [Kind: CTFont]
    static let ink = CGColor(gray: 0.1, alpha: 1)
    static let muted = CGColor(gray: 0.38, alpha: 1)
    static let rule = CGColor(gray: 0.75, alpha: 1)
    static let shade = CGColor(gray: 0.94, alpha: 1)
    static let notice = CGColor(srgbRed: 1, green: 0.97, blue: 0.86, alpha: 1)

    init(_ fonts: ReportFonts) {
        self.fonts = [
            .title: fonts.font(16, bold: true), .heading: fonts.font(12.5, bold: true),
            .subheading: fonts.font(9.5, bold: true), .body: fonts.font(8.5, bold: false),
            .small: fonts.font(7.5, bold: false), .tiny: fonts.font(6.5, bold: false),
            .bold: fonts.font(8.5, bold: true), .smallBold: fonts.font(7.5, bold: true),
            .value: fonts.font(13, bold: true),
        ]
    }

    func text(
        _ string: String, _ kind: Kind = .body, color: CGColor = ink, alignment: CTTextAlignment = .left
    ) -> NSMutableAttributedString {
        var align = alignment
        let paragraph = withUnsafeBytes(of: &align) { pointer in
            var settings = [CTParagraphStyleSetting(
                spec: .alignment, valueSize: MemoryLayout<CTTextAlignment>.size, value: pointer.baseAddress!)]
            return CTParagraphStyleCreate(&settings, settings.count)
        }
        return NSMutableAttributedString(string: string, attributes: [
            NSAttributedString.Key(kCTFontAttributeName as String): fonts[kind]!,
            NSAttributedString.Key(kCTForegroundColorAttributeName as String): color,
            NSAttributedString.Key(kCTParagraphStyleAttributeName as String): paragraph,
        ])
    }
}

/// A flow layout over A4 pages. Everything is measured with Core Text while
/// it is laid out, so the page count is known before the first page is drawn
/// ("Page x of y") and a text that does not fit its frame is recorded rather
/// than cut off silently (ADR 0017 section 11, layout).
final class ReportLayout {
    /// A4 in points.
    static let size = CGSize(width: 595.276, height: 841.89)
    let left: CGFloat = 48
    let top: CGFloat = 66
    let bottom: CGFloat = 74
    var width: CGFloat { Self.size.width - 2 * left }
    var maxY: CGFloat { Self.size.height - bottom }

    typealias Draw = (CGContext) -> Void
    private(set) var pages: [[Draw]] = [[]]
    var y: CGFloat
    var overflows: [String] = []

    init() { y = top }

    var pageIndex: Int { pages.count - 1 }

    func newPage() {
        pages.append([])
        y = top
    }

    /// A new page unless `height` still fits on this one.
    func ensure(_ height: CGFloat) {
        if y + height > maxY && y > top { newPage() }
    }

    func add(_ draw: @escaping Draw) { pages[pageIndex].append(draw) }
    func add(toPage page: Int, _ draw: @escaping Draw) { pages[page].append(draw) }

    // -- text --------------------------------------------------------------------

    func measure(_ text: NSAttributedString, width: CGFloat) -> CGFloat {
        let setter = CTFramesetterCreateWithAttributedString(text)
        let size = CTFramesetterSuggestFrameSizeWithConstraints(
            setter, CFRange(location: 0, length: 0), nil,
            CGSize(width: width, height: .greatestFiniteMagnitude), nil)
        return ceil(size.height) + 1
    }

    /// Text in a frame given from the top of the page. The frame is built now,
    /// and what does not fit is recorded with its words.
    @discardableResult
    func place(_ text: NSAttributedString, x: CGFloat, y: CGFloat, width: CGFloat, height: CGFloat,
               page: Int? = nil) -> Bool {
        let setter = CTFramesetterCreateWithAttributedString(text)
        let rect = CGRect(x: x, y: Self.size.height - y - height, width: width, height: height)
        let frame = CTFramesetterCreateFrame(setter, CFRange(location: 0, length: 0), CGPath(rect: rect, transform: nil), nil)
        let fits = CTFrameGetVisibleStringRange(frame).length == text.length
        if !fits { overflows.append(text.string) }
        add(toPage: page ?? pageIndex) { context in
            context.textMatrix = .identity
            CTFrameDraw(frame, context)
        }
        return fits
    }

    /// A paragraph across the text width, on the next page if it does not fit.
    func paragraph(_ text: NSAttributedString, indent: CGFloat = 0, after: CGFloat = 4) {
        let height = measure(text, width: width - indent)
        ensure(height)
        place(text, x: left + indent, y: y, width: width - indent, height: height)
        y += height + after
    }

    func rule(at y: CGFloat, page: Int? = nil, color: CGColor = ReportStyle.rule, from x: CGFloat? = nil,
              to end: CGFloat? = nil) {
        let start = x ?? left
        let stop = end ?? (left + width)
        add(toPage: page ?? pageIndex) { context in
            context.setStrokeColor(color)
            context.setLineWidth(0.5)
            context.move(to: CGPoint(x: start, y: Self.size.height - y))
            context.addLine(to: CGPoint(x: stop, y: Self.size.height - y))
            context.strokePath()
        }
    }

    func fill(_ rect: CGRect, color: CGColor, stroke: CGColor? = nil) {
        let flipped = CGRect(x: rect.minX, y: Self.size.height - rect.maxY, width: rect.width, height: rect.height)
        add { context in
            context.setFillColor(color)
            context.fill(flipped)
            if let stroke {
                context.setStrokeColor(stroke)
                context.setLineWidth(0.6)
                context.stroke(flipped)
            }
        }
    }

    // -- tables ------------------------------------------------------------------

    struct Column {
        var title: String
        var width: CGFloat
        var alignment: CTTextAlignment = .left
    }

    /// Rows that wrap their cells and grow to fit, with the header repeated
    /// on every page the table runs onto.
    func table(_ columns: [Column], rows: [[NSAttributedString]], style: ReportStyle, header: Bool = true,
               shadeFirstColumn: Bool = false) {
        let pad: CGFloat = 3
        let headers = columns.map { style.text($0.title, .smallBold, color: ReportStyle.muted, alignment: $0.alignment) }
        func heightOf(_ cells: [NSAttributedString]) -> CGFloat {
            zip(cells, columns).map { measure($0, width: $1.width - 2 * pad) }.max() ?? 0
        }
        let headerHeight = header ? heightOf(headers) + 2 * pad : 0
        func drawRow(_ cells: [NSAttributedString], height: CGFloat) {
            var x = left
            for (cell, column) in zip(cells, columns) {
                place(cell, x: x + pad, y: y + pad, width: column.width - 2 * pad, height: height - 2 * pad + 1)
                x += column.width
            }
        }
        func drawHeader() {
            guard header else { return }
            drawRow(headers, height: headerHeight)
            y += headerHeight
            rule(at: y, color: ReportStyle.muted)
        }
        let first = rows.first.map { heightOf($0) + 2 * pad } ?? 0
        ensure(headerHeight + first)
        drawHeader()
        for cells in rows {
            let height = heightOf(cells) + 2 * pad
            if y + height > maxY {
                newPage()
                drawHeader()
            }
            if shadeFirstColumn, let firstColumn = columns.first {
                fill(CGRect(x: left, y: y, width: firstColumn.width, height: height), color: ReportStyle.shade)
            }
            drawRow(cells, height: height)
            y += height
            rule(at: y)
        }
    }
}
