import AppKit
import SwiftUI

struct PopoverRootView: View {
    @ObservedObject var store: ClockStore
    @ObservedObject var sitReminder: SitReminderManager
    @Environment(\.colorScheme) private var colorScheme

    private var accentPurple: Color { Color(hex: "#7C6BF5") }
    private var liveEmerald: Color {
        colorScheme == .dark ? Color(hex: "#34D399") : Color(hex: "#15803D")
    }
    private var amberAccent: Color {
        colorScheme == .dark ? Color(hex: "#FBBF24") : Color(hex: "#B45309")
    }
    private var cardSurface: Color {
        colorScheme == .dark ? Color.white.opacity(0.055) : Color.black.opacity(0.04)
    }
    private var cardBorder: Color {
        colorScheme == .dark ? Color.white.opacity(0.10) : Color.black.opacity(0.08)
    }

    private var dynamicWidth: CGFloat {
        switch store.selectedPopoverTab {
        case .overview:
            let toolCount = store.snapshot?.usage?.periods.stats(for: store.selectedPeriod).tools.count ?? 0
            return toolCount >= 2 ? 624 : 440
        case .dashboard:
            return 624
        case .projects, .quotas, .settings:
            return 440
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            topHeader
            segmentedTabBar
            if let error = store.lastError, store.selectedPopoverTab != .quotas {
                Label(store.snapshot == nil ? error : "\(error) 上次成功 \(store.lastUpdatedTimeString)。",
                      systemImage: "exclamationmark.circle")
                    .font(.system(size: 10.5))
                    .foregroundStyle(amberAccent)
            }
            if let skipped = store.snapshot?.usage?.ledgerMeta.skippedInvalidDays, skipped > 0,
               store.selectedPopoverTab == .overview || store.selectedPopoverTab == .dashboard {
                Label("已保留 \(skipped) 条日期无效的历史记录，未纳入统计。", systemImage: "exclamationmark.circle")
                    .font(.system(size: 10.5))
                    .foregroundStyle(amberAccent)
            }

            Group {
                switch store.selectedPopoverTab {
                case .overview:
                    DetailCardsView(store: store)
                case .dashboard:
                    DashboardViews(store: store)
                case .projects:
                    ProjectTrailViews(store: store)
                case .quotas:
                    quotasTab
                case .settings:
                    settingsTab
                }
            }
        }
        .padding(14)
        .frame(width: dynamicWidth)
        .background(VisualEffectSurface(material: .popover, blendingMode: .behindWindow))
        .animation(.easeOut(duration: 0.16), value: dynamicWidth)
    }

    // MARK: - Top Header

    private var topHeader: some View {
        HStack(spacing: 8) {
            HStack(spacing: 7) {
                ZStack {
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(
                            LinearGradient(
                                colors: [Color(hex: "#8B7CF6"), Color(hex: "#4F46E5")],
                                startPoint: .topLeading,
                                endPoint: .bottomTrailing
                            )
                        )
                        .frame(width: 20, height: 20)
                    Image(systemName: "bolt.fill")
                        .font(.system(size: 10.5, weight: .bold))
                        .foregroundStyle(.white)
                }

                Text("AgentPulse")
                    .font(.system(size: 13.5, weight: .bold))
                    .foregroundStyle(.primary)

                Text("v\(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.6.0")")
                    .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1.5)
                    .background(Color.primary.opacity(0.06), in: Capsule())
            }

            Spacer(minLength: 4)

            Button {
                if sitReminder.isDue {
                    sitReminder.resetTimer()
                } else {
                    store.selectedPopoverTab = .settings
                }
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: sitReminder.isDue ? "figure.stand" : "timer")
                        .font(.system(size: 9.5, weight: .semibold))
                    Text(sitReminder.shortCountdownText)
                        .font(.system(size: 10, weight: .semibold))
                        .monospacedDigit()
                }
                .foregroundStyle(sitReminder.isDue ? amberAccent : Color(hex: "#38BDF8"))
                .padding(.horizontal, 8)
                .padding(.vertical, 3.5)
                .background(
                    (sitReminder.isDue ? amberAccent : Color(hex: "#38BDF8")).opacity(0.14),
                    in: Capsule()
                )
                .overlay(
                    Capsule()
                        .strokeBorder(
                            (sitReminder.isDue ? amberAccent : Color(hex: "#38BDF8")).opacity(0.28),
                            lineWidth: 0.6
                        )
                )
            }
            .buttonStyle(.plain)
            .help(sitReminder.isDue ? "点击确认已起身并重置久坐计时" : "久坐提醒倒计时（点击前往设置）")

            Button {
                store.refreshAll(forceQuotas: store.selectedPopoverTab == .quotas)
            } label: {
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .rotationEffect(.degrees((store.isRefreshing || store.isRefreshingQuotas) ? 360 : 0))
                    .animation(
                        (store.isRefreshing || store.isRefreshingQuotas)
                            ? .linear(duration: 0.7).repeatForever(autoreverses: false)
                            : .default,
                        value: store.isRefreshing || store.isRefreshingQuotas
                    )
                    .frame(width: 24, height: 24)
                    .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            }
            .buttonStyle(.plain)
            .help("立即刷新数据与额度")
            .accessibilityLabel("立即刷新数据与额度")
        }
    }

    // MARK: - 5-Tab Segmented Bar

    private var segmentedTabBar: some View {
        HStack(spacing: 3) {
            ForEach(PopoverTab.allCases) { tab in
                let isSel = store.selectedPopoverTab == tab
                Button {
                    withAnimation(.easeOut(duration: 0.14)) {
                        store.selectedPopoverTab = tab
                    }
                } label: {
                    Text(tab.title)
                        .font(.system(size: 11.5, weight: .semibold))
                        .foregroundStyle(isSel ? (colorScheme == .dark ? .white : accentPurple) : .secondary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 5.5)
                        .background(
                            isSel
                                ? accentPurple.opacity(colorScheme == .dark ? 0.30 : 0.15)
                                : Color.clear,
                            in: RoundedRectangle(cornerRadius: 7, style: .continuous)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 7, style: .continuous)
                                .strokeBorder(isSel ? accentPurple.opacity(0.38) : Color.clear, lineWidth: 0.6)
                        )
                }
                .buttonStyle(.plain)
            }
        }
        .padding(3)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(cardBorder, lineWidth: 0.6)
        )
    }

    // MARK: - TAB 4: Quotas

    private var quotasTab: some View {
        let providers = store.quotaSnapshot?.providers ?? []

        return VStack(alignment: .leading, spacing: 9) {
            if let error = store.quotaError {
                Label(providers.isEmpty ? error : "\(error) 仍在显示上次成功的额度。", systemImage: "exclamationmark.circle")
                    .font(.system(size: 10.5))
                    .foregroundStyle(amberAccent)
            }
            if providers.isEmpty && store.quotaError == nil {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("正在读取本地与订阅额度…")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 24)
                .frame(maxWidth: .infinity)
            } else if !providers.isEmpty {
                ScrollView(.vertical, showsIndicators: false) {
                    VStack(spacing: 8) {
                        ForEach(providers) { p in
                            VStack(alignment: .leading, spacing: 7) {
                                HStack(spacing: 6) {
                                    Circle()
                                        .fill(Color(hex: p.color))
                                        .frame(width: 7, height: 7)
                                    Text(p.name)
                                        .font(.system(size: 11.5, weight: .semibold))
                                        .foregroundStyle(.primary)
                                    Spacer()
                                    Text(p.fetchedAt > 0 && p.status != "unavailable" ? "\(p.plan) · \(Formatters.relativeUpdated(p.fetchedAt, now: store.nowEpoch))" : p.plan)
                                        .font(.system(size: 9.5, weight: .medium))
                                        .foregroundStyle(.secondary)
                                        .padding(.horizontal, 6.5)
                                        .padding(.vertical, 2)
                                        .background(Color.primary.opacity(0.06), in: Capsule())
                                }

                                if p.status != "ok" || p.windows.isEmpty {
                                    Text(p.statusText)
                                        .font(.system(size: 10.5))
                                        .foregroundStyle(p.status == "ok" ? liveEmerald : .secondary)
                                }
                                if !p.windows.isEmpty {
                                    ForEach(p.windows) { w in
                                        let barColor = Formatters.quotaColor(
                                            for: w.usedPercent,
                                            defaultColor: Color(hex: p.color)
                                        )
                                        VStack(alignment: .leading, spacing: 3.5) {
                                            HStack {
                                                Text(w.label)
                                                    .font(.system(size: 10.5))
                                                    .foregroundStyle(.secondary)
                                                Spacer()
                                                let resetStr = Formatters.relativeReset(w.resetsAt, now: store.nowEpoch)
                                                Text(resetStr.isEmpty ? "\(Int(round(w.usedPercent)))%" : "\(Int(round(w.usedPercent)))% · \(resetStr)")
                                                    .font(.system(size: 10, weight: .semibold))
                                                    .monospacedDigit()
                                                    .foregroundStyle(.primary)
                                            }
                                            GeometryReader { geo in
                                                ZStack(alignment: .leading) {
                                                    Capsule().fill(Color.primary.opacity(0.09))
                                                    Capsule()
                                                        .fill(barColor)
                                                        .frame(width: max(5, geo.size.width * CGFloat(min(100, max(0, w.usedPercent)) / 100.0)))
                                                }
                                            }
                                            .frame(height: 5)
                                        }
                                    }
                                }
                            }
                            .padding(10)
                            .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
                            .opacity(p.status == "unavailable" ? 0.72 : 1.0)
                        }
                    }
                }
                .frame(maxHeight: 370)
            }

            HStack {
                Toggle("联网读取 Codex 额度", isOn: $store.allowLiveCodexQuota)
                    .font(.system(size: 10.5))
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                    .help("开启后才使用本机 Codex 登录凭据向 chatgpt.com 查询额度。")
                Spacer()
                Button {
                    store.refreshQuotas(force: true)
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "arrow.clockwise")
                            .font(.system(size: 9.5, weight: .semibold))
                        Text(store.isRefreshingQuotas ? "刷新中…" : "刷新额度")
                            .font(.system(size: 10.5, weight: .semibold))
                    }
                    .foregroundStyle(colorScheme == .dark ? Color(hex: "#EDE9FE") : accentPurple)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5)
                    .background(accentPurple.opacity(0.20), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                }
                .buttonStyle(.plain)
                .disabled(store.isRefreshingQuotas)
            }
        }
    }

    // MARK: - TAB 5: Settings

    private var settingsTab: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 9) {
                // Sit Reminder Card
                VStack(alignment: .leading, spacing: 7) {
                    Text("久坐提醒")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.secondary)

                    Toggle(isOn: $sitReminder.isEnabled) {
                        Text("启用久坐原生通知提醒")
                            .font(.system(size: 11.5))
                    }
                    .toggleStyle(.switch)
                    .controlSize(.mini)

                    if sitReminder.isEnabled {
                        HStack {
                            Text("提醒间隔")
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                            Spacer()
                            HStack(spacing: 4) {
                                ForEach([30, 45, 60, 90], id: \.self) { mins in
                                    Button {
                                        sitReminder.intervalMinutes = mins
                                    } label: {
                                        Text("\(mins)m")
                                            .font(.system(size: 10, weight: .semibold))
                                            .monospacedDigit()
                                            .foregroundStyle(sitReminder.intervalMinutes == mins ? .white : .secondary)
                                            .padding(.horizontal, 7)
                                            .padding(.vertical, 3)
                                            .background(
                                                sitReminder.intervalMinutes == mins ? accentPurple : Color.primary.opacity(0.06),
                                                in: RoundedRectangle(cornerRadius: 5, style: .continuous)
                                            )
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }

                        Toggle(isOn: $sitReminder.resetOnSleep) {
                            Text("锁屏/休眠超 5 分钟自动重置计时")
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                        }
                        .toggleStyle(.switch)
                        .controlSize(.mini)

                        HStack(spacing: 6) {
                            Button {
                                sitReminder.resetTimer()
                            } label: {
                                Text("已起身，重置计时")
                                    .font(.system(size: 10, weight: .semibold))
                                    .foregroundStyle(liveEmerald)
                                    .padding(.horizontal, 8)
                                    .padding(.vertical, 4)
                                    .background(liveEmerald.opacity(0.14), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                            }
                            .buttonStyle(.plain)

                            Button {
                                sitReminder.snooze(minutes: 10)
                            } label: {
                                Text("推迟 10 分钟")
                                    .font(.system(size: 10, weight: .medium))
                                    .foregroundStyle(.secondary)
                                    .padding(.horizontal, 8)
                                    .padding(.vertical, 4)
                                    .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
                .padding(10)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))

                // Menu Bar Display
                VStack(alignment: .leading, spacing: 7) {
                    Text("顶部菜单栏展示")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.secondary)

                    Toggle(isOn: $store.menuBarShowCost) {
                        Text("显示今日估算费用（如 $0.84）")
                            .font(.system(size: 11.5))
                    }
                    .toggleStyle(.switch)
                    .controlSize(.mini)

                    Toggle(isOn: $store.menuBarShowQuotas) {
                        Text("显示常用额度徽章（Codex / Claude）")
                            .font(.system(size: 11.5))
                    }
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                }
                .padding(10)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))

                // Desktop Widget & System
                VStack(alignment: .leading, spacing: 7) {
                    Text("桌面挂件与系统")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.secondary)

                    Toggle(isOn: $store.cardShowQuotaAlert) {
                        Text("卡片底部高额度预警条（≥70% 时显示）")
                            .font(.system(size: 11.5))
                    }
                    .toggleStyle(.switch)
                    .controlSize(.mini)

                    Toggle(isOn: Binding(
                        get: { store.launchAtLogin },
                        set: { _ in store.toggleLaunchAtLogin() }
                    )) {
                        Text("登录时自动启动 AgentPulse")
                            .font(.system(size: 11.5))
                    }
                    .toggleStyle(.switch)
                    .controlSize(.mini)
                }
                .padding(10)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))

                // Data & Migration
                VStack(alignment: .leading, spacing: 7) {
                    Text("账本数据与 Tokei 迁移")
                        .font(.system(size: 10, weight: .bold))
                        .foregroundStyle(.secondary)

                    if let msg = store.migrationMessage {
                        Text(msg)
                            .font(.system(size: 10))
                            .foregroundStyle(liveEmerald)
                    }

                    HStack(spacing: 6) {
                        Button {
                            store.runTokeiImport()
                        } label: {
                            Text(store.isMigrating ? "导入中…" : "重新导入 Tokei 历史")
                                .font(.system(size: 10.5, weight: .semibold))
                                .foregroundStyle(colorScheme == .dark ? Color(hex: "#EDE9FE") : accentPurple)
                                .padding(.horizontal, 9)
                                .padding(.vertical, 5)
                                .background(accentPurple.opacity(0.20), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                        }
                        .buttonStyle(.plain)
                        .disabled(store.isMigrating)

                        Button {
                            store.openDataDirectory()
                        } label: {
                            Text("打开数据目录")
                                .font(.system(size: 10.5, weight: .medium))
                                .foregroundStyle(.primary)
                                .padding(.horizontal, 9)
                                .padding(.vertical, 5)
                                .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                        }
                        .buttonStyle(.plain)

                        Spacer()

                        Button {
                            NSApp.terminate(nil)
                        } label: {
                            Text("退出")
                                .font(.system(size: 10.5, weight: .medium))
                                .foregroundStyle(Color(hex: "#F87171"))
                                .padding(.horizontal, 8)
                                .padding(.vertical, 5)
                                .background(Color(hex: "#F87171").opacity(0.12), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(10)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
            }
        }
        .frame(maxHeight: 420)
    }
}
