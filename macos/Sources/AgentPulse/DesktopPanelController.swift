import AppKit
import SwiftUI

final class DesktopWidgetPanel: NSPanel {
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
}

struct WindowDragRegion: NSViewRepresentable {
    func makeNSView(context: Context) -> NSView {
        let view = DragView()
        view.toolTip = "按住拖动卡片"
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}

    private final class DragView: NSView {
        override var mouseDownCanMoveWindow: Bool { false }

        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

        override func resetCursorRects() {
            addCursorRect(bounds, cursor: .openHand)
        }

        override func mouseDown(with event: NSEvent) {
            window?.performDrag(with: event)
        }
    }
}

@MainActor
final class DesktopPanelController: NSObject, NSWindowDelegate {
    let panel: DesktopWidgetPanel
    private let store: ClockStore
    private let hostingView: NSHostingView<AnyView>

    private enum Keys {
        static let originX = "agentPulse.window.x"
        static let originY = "agentPulse.window.y"
        static let topY = "agentPulse.window.topY"
    }

    init(store: ClockStore) {
        self.store = store
        let rootView = AnyView(WidgetRootView(store: store))
        self.hostingView = NSHostingView(rootView: rootView)

        let initialRect = NSRect(x: 0, y: 0, width: store.isCompact ? 276 : 340, height: 320)
        self.panel = DesktopWidgetPanel(
            contentRect: initialRect,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        super.init()

        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.ignoresMouseEvents = false
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.contentView = hostingView
        panel.delegate = self

        applyLayer(store.windowLayer)
        fitToContent(anchorTopRight: false)
        restoreOrPlaceDefaultPosition()

        if store.isCardVisible {
            panel.orderFrontRegardless()
        } else {
            panel.orderOut(nil)
        }
    }

    func applyLayer(_ mode: WindowLayerMode) {
        switch mode {
        case .desktop:
            let desktopIconLevel = Int(CGWindowLevelForKey(.desktopIconWindow))
            panel.level = NSWindow.Level(rawValue: desktopIconLevel + 1)
            panel.collectionBehavior = [.canJoinAllSpaces, .stationary, .ignoresCycle]
        case .floating:
            panel.level = .floating
            panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        case .normal:
            panel.level = .normal
            panel.collectionBehavior = [.managed, .participatesInCycle]
        }
        if store.isCardVisible {
            panel.orderFrontRegardless()
        }
    }

    func setVisible(_ visible: Bool) {
        if visible {
            fitToContent(anchorTopRight: true)
            panel.orderFrontRegardless()
        } else {
            panel.orderOut(nil)
        }
    }

    func fitToContent(anchorTopRight: Bool = true) {
        hostingView.layoutSubtreeIfNeeded()
        let targetWidth: CGFloat = store.isCompact ? 276 : 340
        let fit = hostingView.fittingSize
        let maxHeight = maxAllowedPanelHeight(for: panel.frame)
        let targetHeight = min(maxHeight, max(120, ceil(fit.height)))
        let targetSize = NSSize(width: targetWidth, height: targetHeight)
        var frame = panel.frame

        if anchorTopRight && frame.width > 0 && frame.height > 0 {
            let oldMaxX = frame.maxX
            let oldMaxY = frame.maxY
            frame.size = targetSize
            frame.origin.x = oldMaxX - targetSize.width
            frame.origin.y = oldMaxY - targetSize.height
        } else {
            frame.size = targetSize
        }

        frame = clampToVisibleScreen(frame)
        panel.setFrame(frame, display: true, animate: false)
    }

    func resetToDefaultPosition() {
        UserDefaults.standard.removeObject(forKey: Keys.originX)
        UserDefaults.standard.removeObject(forKey: Keys.originY)
        UserDefaults.standard.removeObject(forKey: Keys.topY)
        fitToContent(anchorTopRight: false)
        placeDefaultPosition()
        saveOrigin()
        if !store.isCardVisible {
            store.isCardVisible = true
        }
        panel.orderFrontRegardless()
    }

    func windowDidMove(_ notification: Notification) {
        saveOrigin()
    }

    private func maxAllowedPanelHeight(for frame: NSRect) -> CGFloat {
        let screens = NSScreen.screens
        let matching = screens.first { $0.frame.intersects(frame) } ?? NSScreen.main ?? screens.first
        guard let vis = matching?.visibleFrame else { return 760 }
        return max(260, vis.height - 24)
    }

    private func restoreOrPlaceDefaultPosition() {
        let defaults = UserDefaults.standard
        if defaults.object(forKey: Keys.originX) != nil,
           defaults.object(forKey: Keys.originY) != nil {
            var frame = panel.frame
            let topY = defaults.object(forKey: Keys.topY) as? Double
            // The card grows downward from its top edge as the first snapshot loads.
            frame.origin = NSPoint(
                x: defaults.double(forKey: Keys.originX),
                y: topY.map { $0 - frame.height } ?? defaults.double(forKey: Keys.originY)
            )
            frame = clampToVisibleScreen(frame)
            panel.setFrame(frame, display: true)
        } else {
            placeDefaultPosition()
        }
    }

    private func placeDefaultPosition() {
        guard let screen = NSScreen.main ?? NSScreen.screens.first else { return }
        let vis = screen.visibleFrame
        let margin: CGFloat = 20
        let size = panel.frame.size
        let origin = NSPoint(
            x: vis.maxX - size.width - margin,
            y: vis.maxY - size.height - margin
        )
        panel.setFrameOrigin(origin)
    }

    private func clampToVisibleScreen(_ frame: NSRect) -> NSRect {
        let screens = NSScreen.screens
        guard !screens.isEmpty else { return frame }
        let matching = screens.first { $0.frame.intersects(frame) } ?? NSScreen.main ?? screens[0]
        let vis = matching.visibleFrame
        var f = frame
        let pad: CGFloat = 8
        if f.maxX > vis.maxX - pad { f.origin.x = vis.maxX - pad - f.width }
        if f.minX < vis.minX + pad { f.origin.x = vis.minX + pad }
        if f.maxY > vis.maxY - pad { f.origin.y = vis.maxY - pad - f.height }
        if f.minY < vis.minY + pad { f.origin.y = vis.minY + pad }
        return f
    }

    private func saveOrigin() {
        UserDefaults.standard.set(panel.frame.origin.x, forKey: Keys.originX)
        UserDefaults.standard.set(panel.frame.origin.y, forKey: Keys.originY)
        UserDefaults.standard.set(panel.frame.maxY, forKey: Keys.topY)
    }
}
