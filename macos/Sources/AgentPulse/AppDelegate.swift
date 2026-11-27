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
            button.image = makeStatusSymbol(active: false, waiting: false, stale: false)
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

    private func makeStatusSymbol(active: Bool, waiting: Bool, stale: Bool) -> NSImage? {
        let name: String
        if stale {
            name = "clock.badge.exclamationmark"
        } else if waiting {
            name = "exclamationmark.circle.fill"
        } else if active {
            name = "bolt.circle.fill"
        } else {
            name = "clock"
        }
        let cfg = NSImage.SymbolConfiguration(pointSize: 12.0, weight: .semibold)
        let img = NSImage(systemSymbolName: name, accessibilityDescription: "AgentPulse")?
            .withSymbolConfiguration(cfg)
        img?.isTemplate = true
        return img
    }

    /// Pure formatter exposed for unit testing menu bar status presentation.
    static func formatStatusBarTitle(for store: ClockStore) -> String {
        guard store.snapshot != nil else { return " --" }
        let freshness = store.fastFreshness
        let runningCount = store.todayRunningCount
        let waitingCount = store.sentinelTasks.filter { $0.state == .waitingHuman }.count
        let hrsStr = store.todayMenuBarHoursText

        var segments: [String] = []
        if freshness == .stale || freshness == .unavailable {
            segments.append("\(hrsStr) · 离线")
        } else if waitingCount > 0 && runningCount > 0 {
            segments.append("\(hrsStr) · 待拍板 \(waitingCount) / \(runningCount)")
        } else if waitingCount > 0 {
            segments.append("\(hrsStr) · 待拍板 \(waitingCount)")
        } else if runningCount > 0 {
            segments.append("\(hrsStr) · \(runningCount)")
        } else {
            segments.append(hrsStr)
        }

        if store.menuBarShowCost && store.todayHasCostObservation {
            segments.append(store.todayEstimatedCostText)
        }
        if let quotaBadge = store.menuBarQuotaSummary {
            segments.append(quotaBadge)
        }
        return " " + segments.joined(separator: " | ")
    }

    private func updateStatusItemTitle() {
        guard let button = statusItem?.button else { return }
        guard store.snapshot != nil else {
            button.image = makeStatusSymbol(active: false, waiting: false, stale: false)
            button.title = " --"
            return
        }
        let freshness = store.fastFreshness
        let runningCount = store.todayRunningCount
        let waitingCount = store.sentinelTasks.filter { $0.state == .waitingHuman }.count
        button.image = makeStatusSymbol(
            active: runningCount > 0,
            waiting: waitingCount > 0,
            stale: freshness == .stale || freshness == .unavailable
        )
        button.title = Self.formatStatusBarTitle(for: store)
    }

    // MARK: - NSMenuDelegate (Right-Click Quick Menu)

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()

        if store.snapshot != nil {
            let costStr = store.todayEstimatedCostText
            let summaryTitle: String
            if let liveSecs = store.todayLiveSecondsIfKnown {
                summaryTitle = String(
                    format: "今日 %.2fh（%.2f 员工日 · %@ · %@）",
                    liveSecs / 3600.0,
                    liveSecs / (8.0 * 3600.0),
                    Formatters.compactTokens(store.todayTokens),
                    costStr
                )
            } else {
                summaryTitle = "今日 —（无可信工时观测 · \(Formatters.compactTokens(store.todayTokens)) · \(costStr)）"
            }
            let summary = NSMenuItem(
                title: summaryTitle,
                action: nil,
                keyEquivalent: ""
            )
            summary.isEnabled = false
            menu.addItem(summary)

            let freshness = store.fastFreshness
            if freshness == .stale || freshness == .unavailable {
                let staleItem = NSMenuItem(
                    title: "数据已暂停实时更新（最近成功 \(store.lastUpdatedTimeString)）",
                    action: nil,
                    keyEquivalent: ""
                )
                staleItem.isEnabled = false
                menu.addItem(staleItem)
            } else if freshness == .degraded {
                let degItem = NSMenuItem(
                    title: "部分数据源暂不可用（最近成功 \(store.lastUpdatedTimeString)）",
                    action: nil,
                    keyEquivalent: ""
                )
                degItem.isEnabled = false
                menu.addItem(degItem)
            }

            let tasks = store.sentinelTasks
            let waiting = tasks.filter { $0.state == .waitingHuman }
            let running = tasks.filter { $0.state == .running }
            if !waiting.isEmpty {
                let names = waiting.map(\.shortName).joined(separator: "、")
                let waitItem = NSMenuItem(title: "待拍板：\(names)", action: nil, keyEquivalent: "")
                waitItem.isEnabled = false
                menu.addItem(waitItem)
            }
            if !running.isEmpty {
                let names = running.map(\.shortName).joined(separator: "、")
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
