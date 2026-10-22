import AppKit
import BCOAKit
import SwiftUI

/// One plane: the CT drawn with linear scaling and the labels over it with
/// nearest neighbour, so their edges stay sharp (plan §9), in the physical
/// aspect of the voxels, with the crosshair and the orientation letters.
final class SliceNSView: NSView {
    var gray: CGImage? { didSet { needsDisplay = true } }
    var overlay: CGImage? { didSet { needsDisplay = true } }
    var pixelSize = CGSize(width: 1, height: 1) { didSet { needsDisplay = true } }
    /// Millimetres per pixel across and down.
    var spacing = CGSize(width: 1, height: 1) { didSet { needsDisplay = true } }
    var crosshair: CGPoint? { didSet { needsDisplay = true } }
    var letters = EdgeLetters(left: "", right: "", top: "", bottom: "") { didSet { needsDisplay = true } }
    var caption = "" { didSet { needsDisplay = true } }
    var zoom: CGFloat = 1 { didSet { needsDisplay = true } }

    var onClick: ((Int, Int) -> Void)?
    var onHover: ((Int, Int) -> Void)?
    var onScroll: ((Int) -> Void)?
    var onWindow: ((Double, Double) -> Void)?

    private var tracking: NSTrackingArea?
    private var lastDrag: NSPoint?
    private var scrollRemainder: CGFloat = 0

    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { false }

    /// The rectangle the slice fills: as large as fits, in physical aspect.
    var imageRect: CGRect {
        let physical = CGSize(width: pixelSize.width * spacing.width, height: pixelSize.height * spacing.height)
        guard physical.width > 0, physical.height > 0 else { return .zero }
        let scale = min(bounds.width / physical.width, bounds.height / physical.height) * zoom
        let size = CGSize(width: physical.width * scale, height: physical.height * scale)
        return CGRect(x: (bounds.width - size.width) / 2, y: (bounds.height - size.height) / 2,
                      width: size.width, height: size.height)
    }

    override func draw(_ dirtyRect: NSRect) {
        NSColor.black.setFill()
        bounds.fill()
        guard let context = NSGraphicsContext.current?.cgContext else { return }
        let rect = imageRect
        context.saveGState()
        // CGContext draws images bottom-up; the view is flipped, so flip back
        // around the image rectangle to keep row 0 at the top.
        context.translateBy(x: 0, y: rect.maxY + rect.minY)
        context.scaleBy(x: 1, y: -1)
        if let gray {
            context.interpolationQuality = .high
            context.draw(gray, in: rect)
        }
        if let overlay {
            context.interpolationQuality = .none
            context.draw(overlay, in: rect)
        }
        context.restoreGState()

        if let crosshair {
            let x = rect.minX + (crosshair.x + 0.5) / pixelSize.width * rect.width
            let y = rect.minY + (crosshair.y + 0.5) / pixelSize.height * rect.height
            NSColor.systemYellow.withAlphaComponent(0.6).setStroke()
            let path = NSBezierPath()
            path.lineWidth = 1
            path.move(to: NSPoint(x: rect.minX, y: y))
            path.line(to: NSPoint(x: rect.maxX, y: y))
            path.move(to: NSPoint(x: x, y: rect.minY))
            path.line(to: NSPoint(x: x, y: rect.maxY))
            path.stroke()
        }
        let attributes: [NSAttributedString.Key: Any] = [
            .font: NSFont.boldSystemFont(ofSize: 14), .foregroundColor: NSColor.systemOrange,
        ]
        func draw(_ text: String, at point: NSPoint) {
            (text as NSString).draw(at: point, withAttributes: attributes)
        }
        draw(letters.left, at: NSPoint(x: 6, y: bounds.midY - 8))
        draw(letters.right, at: NSPoint(x: bounds.maxX - 16, y: bounds.midY - 8))
        draw(letters.top, at: NSPoint(x: bounds.midX - 5, y: 4))
        draw(letters.bottom, at: NSPoint(x: bounds.midX - 5, y: bounds.maxY - 20))
        let small: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 11, weight: .regular),
            .foregroundColor: NSColor.white.withAlphaComponent(0.8),
        ]
        // Top left: the bottom edge holds a letter at its centre, which a
        // long caption ran into.
        (caption as NSString).draw(at: NSPoint(x: 6, y: 6), withAttributes: small)
    }

    /// The pixel of the slice under a point of the view.
    func pixel(at point: NSPoint) -> (Int, Int)? {
        let rect = imageRect
        guard rect.width > 0, rect.contains(point) else { return nil }
        let column = Int((point.x - rect.minX) / rect.width * pixelSize.width)
        let row = Int((point.y - rect.minY) / rect.height * pixelSize.height)
        return (column, row)
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let tracking { removeTrackingArea(tracking) }
        let area = NSTrackingArea(rect: bounds, options: [.mouseMoved, .activeInKeyWindow, .inVisibleRect],
                                  owner: self)
        addTrackingArea(area)
        tracking = area
    }

    override func mouseDown(with event: NSEvent) {
        if let (column, row) = pixel(at: convert(event.locationInWindow, from: nil)) { onClick?(column, row) }
    }

    override func mouseDragged(with event: NSEvent) {
        mouseDown(with: event)
    }

    override func mouseMoved(with event: NSEvent) {
        if let (column, row) = pixel(at: convert(event.locationInWindow, from: nil)) { onHover?(column, row) }
    }

    override func rightMouseDown(with event: NSEvent) {
        lastDrag = event.locationInWindow
    }

    override func rightMouseDragged(with event: NSEvent) {
        guard let last = lastDrag else { return }
        let now = event.locationInWindow
        onWindow?(Double(now.x - last.x), Double(now.y - last.y))
        lastDrag = now
    }

    override func scrollWheel(with event: NSEvent) {
        if event.modifierFlags.contains(.command) {
            zoom = max(1, min(8, zoom * (event.scrollingDeltaY > 0 ? 1.1 : 1 / 1.1)))
            return
        }
        // Trackpads send many small deltas; a slice per whole step.
        scrollRemainder += event.hasPreciseScrollingDeltas ? event.scrollingDeltaY / 6 : event.scrollingDeltaY
        let steps = Int(scrollRemainder)
        guard steps != 0 else { return }
        scrollRemainder -= CGFloat(steps)
        onScroll?(-steps)
    }

    override func magnify(with event: NSEvent) {
        zoom = max(1, min(8, zoom * (1 + event.magnification)))
    }
}

/// A plane of the viewer model as a SwiftUI view.
struct SliceView: NSViewRepresentable {
    let model: ViewerModel
    let plane: Plane

    func makeNSView(context: Context) -> SliceNSView {
        let view = SliceNSView()
        view.setAccessibilityElement(true)
        view.setAccessibilityRole(.image)
        return view
    }

    func updateNSView(_ view: SliceNSView, context: Context) {
        guard let volume = model.volume, let mapping = model.mapping(plane) else {
            view.gray = nil
            view.overlay = nil
            return
        }
        let slice = model.slice(plane)
        let width = mapping.width, height = mapping.height
        view.pixelSize = CGSize(width: width, height: height)
        let spacing = mapping.pixelSpacing
        view.spacing = CGSize(width: spacing.horizontal, height: spacing.vertical)
        view.letters = mapping.letters
        view.gray = Self.image(gray: volume.graySlice(mapping, slice: slice, window: model.window),
                               width: width, height: height)
        if model.showOverlays {
            let layers = model.models.map { name in
                OverlayRenderer.Layer(
                    labels: volume.labelSlice(name, mapping: mapping, slice: slice),
                    visible: model.visible[name] ?? [],
                    highlighted: model.highlighted?.model == name ? model.highlighted?.label : nil)
            }
            view.overlay = Self.image(
                rgba: OverlayRenderer.rgba(layers: layers, width: width, height: height,
                                           opacity: model.opacity, contour: model.contour),
                width: width, height: height)
        } else {
            view.overlay = nil
        }
        let pixel = mapping.pixel(of: model.cursor)
        view.crosshair = model.showCrosshair ? CGPoint(x: pixel.column, y: pixel.row) : nil
        view.caption = "\(PlaneText.name(plane)) \(slice + 1)/\(mapping.sliceCount)"
        view.setAccessibilityLabel(String(localized: "\(PlaneText.name(plane)) slice \(slice + 1) of \(mapping.sliceCount)"))
        view.onClick = { column, row in model.click(plane, column: column, row: row) }
        view.onHover = { column, row in model.hover(plane, column: column, row: row) }
        view.onScroll = { delta in model.scroll(plane, by: delta) }
        view.onWindow = { dx, dy in model.adjustWindow(dx: dx, dy: dy) }
    }

    static func image(gray: [UInt8], width: Int, height: Int) -> CGImage? {
        guard let provider = CGDataProvider(data: Data(gray) as CFData) else { return nil }
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: width,
                       space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: 0),
                       provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
    }

    static func image(rgba: [UInt8], width: Int, height: Int) -> CGImage? {
        guard let provider = CGDataProvider(data: Data(rgba) as CFData) else { return nil }
        return CGImage(width: width, height: height, bitsPerComponent: 8, bitsPerPixel: 32,
                       bytesPerRow: width * 4, space: CGColorSpaceCreateDeviceRGB(),
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.last.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
    }
}

enum PlaneText {
    static func name(_ plane: Plane) -> String {
        switch plane {
        case .axial: String(localized: "Axial")
        case .coronal: String(localized: "Coronal")
        case .sagittal: String(localized: "Sagittal")
        }
    }
}
