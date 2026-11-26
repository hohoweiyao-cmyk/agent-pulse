import AppKit
import Foundation
import SwiftUI

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private let store = ClockStore()
    private var panelController: DesktopPanelController?
    private var statusItem: NSStatusItem?
    private var popover: NSPopover?
    private var contextMenu: NSMenu?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)

        let controller = DesktopPanelController(store: store)
        self.panelController = controller

        store.onWindowLayerChanged = { [weak controller] mode in
            controller?.applyLayer(mode)
        }
        store.onVisibilityChanged = { [weak controller] visible in
            controller?.setVisible(visible)
        }
        store.onLayoutChanged = { [weak controller] in
            DispatchQueue.main.async {
                controller?.fitToContent(anchorTopRight: true)
            }
        }
        store.onSnapshotUpdated = { [weak self] _ in
            self?.updateStatusItemTitle()
        }
        store.onQuotaUpdated = { [weak self] _ in
            self?.updateStatusItemTitle()
        }
        store.onStatusBarPreferenceChanged = { [weak self] in
            self?.updateStatusItemTitle()
        }
        store.onOpenPopoverTab = { [weak self] tab in
            self?.showPopover(tab: tab)
        }

        setupPopover()
        setupStatusItem()
        store.start()
        updateStatusItemTitle()
    }

    func applicationWillTerminate(_ notification: Notification) {
        store.stop()
    }

    private func setupPopover() {
        let pop = NSPopover()
        pop.behavior = .transient
        pop.animates = true
        pop.contentViewController = NSHostingController(
            rootView: PopoverRootView(store: store, sitReminder: store.sitReminder)
        )
        self.popover = pop
    }

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = item.button {
            button.image = makeStatusSymbol(active: false)
            button.imagePosition = .imageLeading
            button.font = NSFont.monospacedDigitSystemFont(ofSize: 12.0, weight: .medium)
            button.title = " --"
            button.target = self
            button.action = #selector(handleStatusButtonClick(_:))
            button.sendAction(on: [.leftMouseUp, .rightMouseUp])
        }
        let menu = NSMenu()
        menu.delegate = self
        self.contextMenu = menu
        self.statusItem = item
    }

    @objc private func handleStatusButtonClick(_ sender: NSStatusBarButton) {
        let event = NSApp.currentEvent
        if event?.type == .rightMouseUp || (event?.modifierFlags.contains(.control) ?? false) {
            popover?.performClose(nil)
            if let menu = contextMenu, let item = statusItem {
                item.menu = menu
                item.button?.performClick(nil)
                item.menu = nil
            }
            return
        }
        togglePopover()
    }

    private func togglePopover() {
        guard let pop = popover, let button = statusItem?.button else { return }
        if pop.isShown {
            pop.performClose(nil)
        } else {
            NSApp.activate(ignoringOtherApps: true)
            pop.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }

    private func showPopover(tab: PopoverTab) {
        store.selectedPopoverTab = tab
        guard let pop = popover, let button = statusItem?.button else { return }
        if !pop.isShown {
            NSApp.activate(ignoringOtherApps: true)
            pop.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        }
    }

    private func makeStatusSymbol(active: Bool) -> NSImage? {
        let name = active ? "bolt.circle.fill" : "clock"
        let cfg = NSImage.SymbolConfiguration(pointSize: 12.0, weight: .semibold)
        let img = NSImage(systemSymbolName: name, accessibilityDescription: "AgentPulse")?
            .withSymbolConfiguration(cfg)
        img?.isTemplate = true
        return img
    }

    private func updateStatusItemTitle() {
        guard let button = statusItem?.button else { return }
        guard let snap = store.snapshot else {
            button.image = makeStatusSymbol(active: false)
            button.title = " --"
            return
        }
        let activeCount = snap.activeAgents.count
        let hrsStr = Formatters.menuBarHours(store.todayLiveSeconds)
        button.image = makeStatusSymbol(active: activeCount > 0)

        var segments: [String] = []
        if activeCount > 0 {
            segments.append("\(hrsStr) · \(activeCount)")
        } else {
            segments.append(hrsStr)
        }
        if store.menuBarShowCost && (store.todayCostUsd > 0 || store.todayTokens > 0) {
            segments.append(Formatters.usd(store.todayCostUsd))
        }
        if let quotaBadge = store.menuBarQuotaSummary {
            segments.append(quotaBadge)
        }
        button.title = " " + segments.joined(separator: " | ")
    }

    // MARK: - NSMenuDelegate (Right-Click Quick Menu)

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        if let snap = store.snapshot {
            let summary = NSMenuItem(
                title: String(format: "今日 %.2fh（%.2f 员工日 · %@ · %@）",
                              store.todayLiveSeconds / 3600.0,
                              store.todayLiveSeconds / (8.0 * 3600.0),
                              Formatters.compactTokens(store.todayTokens),
                              Formatters.usd(store.todayCostUsd)),
                action: nil,
                keyEquivalent: ""
            )
            summary.isEnabled = false
            menu.addItem(summary)

            if !snap.activeAgents.isEmpty {
                let names = snap.activeAgents.map(\.shortName).joined(separator: "、")
                let liveItem = NSMenuItem(title: "在岗中：\(names)", action: nil, keyEquivalent: "")
                liveItem.isEnabled = false
                menu.addItem(liveItem)
            }
            menu.addItem(.separator())
        }

        let openPanel = NSMenuItem(title: "打开用量与额度详单", action: #selector(openPopoverFromMenu), keyEquivalent: "")
        openPanel.target = self
        menu.addItem(openPanel)

        let toggleVis = NSMenuItem(
            title: store.isCardVisible ? "隐藏桌面卡片" : "显示桌面卡片",
            action: #selector(toggleCardVisibility),
            keyEquivalent: ""
        )
        toggleVis.target = self
        menu.addItem(toggleVis)

        let compactItem = NSMenuItem(
            title: store.isCompact ? "切换为完整卡片" : "切换为紧凑挂件",
            action: #selector(toggleCompact),
            keyEquivalent: ""
        )
        compactItem.target = self
        menu.addItem(compactItem)

        menu.addItem(.separator())

        let layerHeader = NSMenuItem(title: "桌面卡片层级", action: nil, keyEquivalent: "")
        layerHeader.isEnabled = false
        menu.addItem(layerHeader)

        for mode in WindowLayerMode.allCases {
            let item = NSMenuItem(title: "  \(mode.title)", action: #selector(selectLayer(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = mode.rawValue
            item.state = (store.windowLayer == mode) ? .on : .off
            menu.addItem(item)
        }

        let resetPos = NSMenuItem(title: "重置卡片到屏幕右上角", action: #selector(resetPosition), keyEquivalent: "")
        resetPos.target = self
        menu.addItem(resetPos)

        menu.addItem(.separator())

        let sitReset = NSMenuItem(
            title: "久坐提醒：\(store.sitReminder.shortCountdownText)（点击重置）",
            action: #selector(resetSitTimer),
            keyEquivalent: ""
        )
        sitReset.target = self
        menu.addItem(sitReset)

        let copyItem = NSMenuItem(title: "复制今日融合日报 (Markdown)", action: #selector(copyReport), keyEquivalent: "")
        copyItem.target = self
        menu.addItem(copyItem)

        let refreshItem = NSMenuItem(title: "立即刷新数据与额度", action: #selector(refreshNow), keyEquivalent: "r")
        refreshItem.target = self
        menu.addItem(refreshItem)

        let loginItem = NSMenuItem(title: "登录时自动启动", action: #selector(toggleLaunchAtLogin), keyEquivalent: "")
        loginItem.target = self
        loginItem.state = store.launchAtLogin ? .on : .off
        menu.addItem(loginItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "退出 AgentPulse", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        menu.addItem(quitItem)
    }

    @objc private func openPopoverFromMenu() {
        showPopover(tab: .overview)
    }

    @objc private func toggleCardVisibility() {
        store.isCardVisible.toggle()
    }

    @objc private func toggleCompact() {
        store.isCompact.toggle()
    }

    @objc private func selectLayer(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String,
              let mode = WindowLayerMode(rawValue: raw) else { return }
        store.windowLayer = mode
        if !store.isCardVisible {
            store.isCardVisible = true
        }
    }

    @objc private func resetPosition() {
        panelController?.resetToDefaultPosition()
    }

    @objc private func resetSitTimer() {
        store.sitReminder.resetTimer()
    }

    @objc private func copyReport() {
        store.copyMarkdownReport()
    }

    @objc private func refreshNow() {
        store.refreshAll(forceQuotas: true)
    }

    @objc private func toggleLaunchAtLogin() {
        store.toggleLaunchAtLogin()
    }

    @objc private func quitApp() {
        NSApp.terminate(nil)
    }
}
