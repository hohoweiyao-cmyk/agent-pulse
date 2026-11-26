import AppKit
import SwiftUI

struct PopoverRootView: View {
    @ObservedObject var store: ClockStore
    @ObservedObject var sitReminder: SitReminderManager
    @Environment(\.colorScheme) private var colorScheme
    @State private var expandedToolId: String?
    @State private var hoveredHistoryDate: String?

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

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            topHeader
            segmentedTabBar

            Group {
                switch store.selectedPopoverTab {
                case .overview:
                    overviewTab
                case .quotas:
                    quotasTab
                case .history:
                    historyTab
                case .settings:
                    settingsTab
                }
            }
        }
        .padding(14)
        .frame(width: 372)
        .background(VisualEffectSurface(material: .popover, blendingMode: .behindWindow))
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
                    .font(.system(size: 13, weight: .bold))
                    .foregroundStyle(.primary)
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
        }
    }

    // MARK: - 4-Tab Segmented Bar

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

    // MARK: - TAB 1: Overview

    private var overviewTab: some View {
        let periodStats = store.snapshot?.usage?.periods.stats(for: store.selectedPeriod)

        return VStack(alignment: .leading, spacing: 10) {
            // Period pills
            HStack {
                HStack(spacing: 4) {
                    ForEach(UsagePeriodKey.allCases) { key in
                        let isSel = store.selectedPeriod == key
                        Button {
                            withAnimation(.easeOut(duration: 0.14)) {
                                store.selectedPeriod = key
                                expandedToolId = nil
                            }
                        } label: {
                            Text(key.title)
                                .font(.system(size: 10.5, weight: .semibold))
                                .foregroundStyle(isSel ? .primary : .secondary)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 3.5)
                                .background(
                                    isSel ? Color.primary.opacity(0.13) : Color.primary.opacity(0.05),
                                    in: RoundedRectangle(cornerRadius: 6, style: .continuous)
                                )
                        }
                        .buttonStyle(.plain)
                    }
                }

                Spacer()

                if let p = periodStats {
                    Text(periodDateBadge(p))
                        .font(.system(size: 10, weight: .medium))
                        .monospacedDigit()
                        .foregroundStyle(.tertiary)
                }
            }

            // Dual KPI Summary Cards
            HStack(spacing: 8) {
                // Left: Work Hours
                let hrs = store.selectedPeriod == .today
                    ? (store.todayLiveSeconds / 3600.0)
                    : (periodStats?.agentHours ?? 0.0)
                let wd = hrs / 8.0
                let lev = store.selectedPeriod == .today
                    ? (store.snapshot?.today.leverage ?? 1.0)
                    : (periodStats?.leverage ?? 1.0)

                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text("AI 累计工时")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("纯执行")
                            .font(.system(size: 9.5, weight: .semibold))
                            .foregroundStyle(liveEmerald)
                    }
                    Text(String(format: "%.2fh", hrs))
                        .font(.system(size: 20, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.primary)
                    HStack(spacing: 4) {
                        Text(String(format: "%.2f 员工日", wd))
                            .foregroundStyle(liveEmerald)
                            .fontWeight(.semibold)
                        Text("·")
                            .foregroundStyle(.tertiary)
                        Text(String(format: "%.2f× 并行", lev))
                            .foregroundStyle(.secondary)
                    }
                    .font(.system(size: 9.5))
                    .monospacedDigit()
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))

                // Right: Tokens & Cost
                let tok = periodStats?.totalTokens ?? store.todayTokens
                let cost = periodStats?.costUsd ?? store.todayCostUsd
                let cachePct = Int(round((periodStats?.cacheHitRate ?? 0.0) * 100))

                VStack(alignment: .leading, spacing: 3) {
                    HStack {
                        Text("Token / 估算")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Text("自有引擎")
                            .font(.system(size: 9.5, weight: .semibold))
                            .foregroundStyle(accentPurple)
                    }
                    Text(Formatters.compactTokens(tok))
                        .font(.system(size: 20, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(.primary)
                    HStack(spacing: 4) {
                        Text(Formatters.usd(cost))
                            .foregroundStyle(accentPurple)
                            .fontWeight(.semibold)
                        Text("·")
                            .foregroundStyle(.tertiary)
                        Text("缓存 \(cachePct)%")
                            .foregroundStyle(.secondary)
                    }
                    .font(.system(size: 9.5))
                    .monospacedDigit()
                }
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
            }

            // Inline Quota Quick Strip
            if let qSnap = store.quotaSnapshot {
                quotaQuickStrip(snapshot: qSnap)
            }

            // Unified Tool Matrix (Work Hours + Tokens + Cost)
            unifiedToolMatrix(tools: periodStats?.tools ?? [])

            // Bottom Action Row
            HStack(spacing: 6) {
                Button {
                    store.copyMarkdownReport()
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: store.copiedToast ? "checkmark" : "doc.on.doc")
                            .font(.system(size: 10, weight: .semibold))
                        Text(store.copiedToast ? "已复制日报" : "复制融合日报")
                            .font(.system(size: 10.5, weight: .semibold))
                    }
                    .foregroundStyle(store.copiedToast ? liveEmerald : (colorScheme == .dark ? Color(hex: "#EDE9FE") : accentPurple))
                    .padding(.horizontal, 10)
                    .padding(.vertical, 5.5)
                    .background(
                        (store.copiedToast ? liveEmerald : accentPurple).opacity(0.18),
                        in: RoundedRectangle(cornerRadius: 7, style: .continuous)
                    )
                    .overlay(
                        RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .strokeBorder((store.copiedToast ? liveEmerald : accentPurple).opacity(0.32), lineWidth: 0.6)
                    )
                }
                .buttonStyle(.plain)

                Spacer()

                Button {
                    store.isCardVisible.toggle()
                } label: {
                    Text(store.isCardVisible ? "隐藏卡片" : "显示卡片")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 9)
                        .padding(.vertical, 5.5)
                        .background(cardSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
                }
                .buttonStyle(.plain)

                Button {
                    store.windowLayer = store.windowLayer.toggledPrimary()
                } label: {
                    Image(systemName: store.windowLayer.symbolName)
                        .font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(store.windowLayer == .floating ? amberAccent : .secondary)
                        .frame(width: 26, height: 24)
                        .background(cardSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
                }
                .buttonStyle(.plain)
                .help("当前：\(store.windowLayer.title)（点击切换桌面层 / 悬浮置顶）")
            }
            .padding(.top, 2)
        }
    }

    private func quotaQuickStrip(snapshot: QuotaSnapshot) -> some View {
        let items: [(title: String, pct: Double, tint: Color)] = {
            var arr: [(String, Double, Color)] = []
            if let codex = snapshot.providers.first(where: { $0.id == "codex" }) {
                if let w5 = codex.windows.first(where: { $0.id == "codex_5h" }) {
                    arr.append(("Codex 5h", w5.usedPercent, Formatters.quotaColor(for: w5.usedPercent, defaultColor: liveEmerald)))
                }
                if let w7 = codex.windows.first(where: { $0.id == "codex_7d" }) {
                    arr.append(("Codex 7d", w7.usedPercent, Formatters.quotaColor(for: w7.usedPercent, defaultColor: accentPurple)))
                }
            }
            if let claude = snapshot.providers.first(where: { $0.id == "claude" }),
               let w7 = claude.windows.first(where: { $0.id == "claude_7d" }) ?? claude.windows.first {
                arr.append(("Claude 7d", w7.usedPercent, Formatters.quotaColor(for: w7.usedPercent, defaultColor: amberAccent)))
            }
            return Array(arr.prefix(3))
        }()

        return Group {
            if !items.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("订阅额度快览")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button {
                            withAnimation(.easeOut(duration: 0.14)) {
                                store.selectedPopoverTab = .quotas
                            }
                        } label: {
                            Text("查看全部 →")
                                .font(.system(size: 10, weight: .semibold))
                                .foregroundStyle(accentPurple)
                        }
                        .buttonStyle(.plain)
                    }

                    HStack(spacing: 8) {
                        ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                            VStack(alignment: .leading, spacing: 3) {
                                HStack {
                                    Text(item.title)
                                        .font(.system(size: 9.5, weight: .medium))
                                        .foregroundStyle(.secondary)
                                        .lineLimit(1)
                                    Spacer(minLength: 2)
                                    Text("\(Int(round(item.pct)))%")
                                        .font(.system(size: 9.5, weight: .bold))
                                        .monospacedDigit()
                                        .foregroundStyle(item.tint)
                                }
                                GeometryReader { geo in
                                    ZStack(alignment: .leading) {
                                        Capsule().fill(Color.primary.opacity(0.09))
                                        Capsule()
                                            .fill(item.tint)
                                            .frame(width: max(4, geo.size.width * CGFloat(min(100, max(0, item.pct)) / 100.0)))
                                    }
                                }
                                .frame(height: 4.5)
                            }
                            .frame(maxWidth: .infinity)
                        }
                    }
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
            }
        }
    }

    private func unifiedToolMatrix(tools: [UsageToolStats]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 4) {
                Text("AI 工具（已去重）")
                    .frame(maxWidth: .infinity, alignment: .leading)
                Text(store.selectedPeriod == .year ? "活跃" : "工时")
                    .frame(width: 48, alignment: .trailing)
                Text("Token")
                    .frame(width: 60, alignment: .trailing)
                Text("估算费用")
                    .frame(width: 56, alignment: .trailing)
            }
            .font(.system(size: 9.5, weight: .medium))
            .foregroundStyle(.tertiary)
            .padding(.horizontal, 4)

            Rectangle()
                .fill(Color.primary.opacity(0.08))
                .frame(height: 0.5)

            if tools.isEmpty {
                Text("所选周期暂无工具记录")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                    .padding(.vertical, 12)
                    .frame(maxWidth: .infinity, alignment: .center)
            } else {
                ScrollView(.vertical, showsIndicators: false) {
                    VStack(spacing: 2) {
                        ForEach(tools.prefix(8)) { tool in
                            let isExpanded = expandedToolId == tool.id
                            VStack(alignment: .leading, spacing: 4) {
                                Button {
                                    guard !tool.models.isEmpty else { return }
                                    withAnimation(.easeOut(duration: 0.14)) {
                                        expandedToolId = isExpanded ? nil : tool.id
                                    }
                                } label: {
                                    HStack(spacing: 4) {
                                        HStack(spacing: 6) {
                                            Circle()
                                                .fill(Color(hex: tool.color))
                                                .frame(width: 6.5, height: 6.5)
                                            Text(tool.displayName)
                                                .font(.system(size: 11, weight: .medium))
                                                .foregroundStyle(.primary)
                                                .lineLimit(1)
                                        }
                                        .frame(maxWidth: .infinity, alignment: .leading)

                                        Text(
                                            store.selectedPeriod == .year
                                                ? (tool.activeDays > 0 ? "\(tool.activeDays)天" : Formatters.shortDuration(tool.agentSeconds))
                                                : Formatters.shortDuration(tool.agentSeconds)
                                        )
                                        .font(.system(size: 10.5))
                                        .monospacedDigit()
                                        .foregroundStyle(.secondary)
                                        .frame(width: 48, alignment: .trailing)

                                        Text(tool.totalTokens > 0 ? Formatters.compactTokens(tool.totalTokens) : "订阅")
                                            .font(.system(size: 10.5))
                                            .monospacedDigit()
                                            .foregroundStyle(tool.totalTokens > 0 ? .primary : .secondary)
                                            .frame(width: 60, alignment: .trailing)

                                        Text(tool.costUsd > 0 ? Formatters.usd(tool.costUsd) : (tool.totalTokens == 0 ? "已含" : "$0.00"))
                                            .font(.system(size: 10.5, weight: .semibold))
                                            .monospacedDigit()
                                            .foregroundStyle(tool.totalTokens == 0 ? accentPurple : .primary)
                                            .frame(width: 56, alignment: .trailing)
                                    }
                                    .padding(.horizontal, 4)
                                    .padding(.vertical, 5.5)
                                    .background(
                                        isExpanded ? Color.primary.opacity(0.05) : Color.clear,
                                        in: RoundedRectangle(cornerRadius: 6, style: .continuous)
                                    )
                                }
                                .buttonStyle(.plain)

                                if isExpanded && !tool.models.isEmpty {
                                    VStack(alignment: .leading, spacing: 3) {
                                        ForEach(tool.models.prefix(4)) { m in
                                            HStack(spacing: 4) {
                                                Text(m.model)
                                                    .font(.system(size: 9.5, design: .monospaced))
                                                    .foregroundStyle(.secondary)
                                                    .lineLimit(1)
                                                    .truncationMode(.middle)
                                                Spacer()
                                                Text("In \(Formatters.compactTokens(m.inputTokens)) · Out \(Formatters.compactTokens(m.outputTokens))")
                                                    .font(.system(size: 9))
                                                    .monospacedDigit()
                                                    .foregroundStyle(.tertiary)
                                                Text(Formatters.usd(m.costUsd))
                                                    .font(.system(size: 9.5, weight: .semibold))
                                                    .monospacedDigit()
                                                    .foregroundStyle(.secondary)
                                                    .frame(width: 44, alignment: .trailing)
                                            }
                                        }
                                    }
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 6)
                                    .background(Color.primary.opacity(0.035), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                                }
                            }
                        }
                    }
                }
                .frame(maxHeight: 168)
            }
        }
    }

    private func periodDateBadge(_ p: PeriodStats) -> String {
        switch store.selectedPeriod {
        case .today: return p.endDate
        case .week: return "近 7 天"
        case .month: return String(p.startDate.prefix(7))
        case .year: return "\(String(p.startDate.prefix(4))) 全年"
        }
    }

    // MARK: - TAB 2: Quotas

    private var quotasTab: some View {
        let providers = store.quotaSnapshot?.providers ?? []

        return VStack(alignment: .leading, spacing: 9) {
            if providers.isEmpty {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("正在读取本地与订阅额度…")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 24)
                .frame(maxWidth: .infinity)
            } else {
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
                .frame(maxHeight: 330)
            }

            HStack {
                Text("刷新 Codex 查询与本地额度缓存")
                    .font(.system(size: 9.5))
                    .foregroundStyle(.tertiary)
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

    // MARK: - TAB 3: History Ledger

    private var historyTab: some View {
        let meta = store.snapshot?.usage?.ledgerMeta
        let hist14 = Array((store.snapshot?.usage?.history30d ?? []).suffix(14))
        let yearTools = store.snapshot?.usage?.periods.year.tools ?? []

        return VStack(alignment: .leading, spacing: 9) {
            // Imported ledger badge
            if let m = meta, m.importedTokei {
                HStack(spacing: 6) {
                    Image(systemName: "checkmark.seal.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(liveEmerald)
                    Text("已承接 Tokei 历史账本（\(m.importedToolCount) 个工具）")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(.primary)
                    Spacer()
                    Text("最长 \(m.maxToolDays) 天")
                        .font(.system(size: 10, weight: .bold))
                        .monospacedDigit()
                        .foregroundStyle(liveEmerald)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(liveEmerald.opacity(0.12), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(liveEmerald.opacity(0.25), lineWidth: 0.6))
            }

            // Metric switcher + 14d mini chart
            HStack {
                Text("近 14 天走势")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                HStack(spacing: 3) {
                    ForEach(HistoryMetric.allCases) { metric in
                        let isSel = store.selectedHistoryMetric == metric
                        Button {
                            store.selectedHistoryMetric = metric
                        } label: {
                            Text(metric.title)
                                .font(.system(size: 9.5, weight: .semibold))
                                .foregroundStyle(isSel ? .primary : .secondary)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 2.5)
                                .background(
                                    isSel ? accentPurple.opacity(0.22) : Color.primary.opacity(0.05),
                                    in: RoundedRectangle(cornerRadius: 5, style: .continuous)
                                )
                        }
                        .buttonStyle(.plain)
                    }
                }
            }

            historyBarChart(points: hist14)

            // Yearly per-tool ledger summary
            HStack {
                Text("年度账本汇总（含迁移历史）")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                Spacer()
                if let y = store.snapshot?.usage?.periods.year {
                    Text("\(Formatters.compactTokens(y.totalTokens)) · \(Formatters.usd(y.costUsd))")
                        .font(.system(size: 10, weight: .semibold))
                        .monospacedDigit()
                        .foregroundStyle(accentPurple)
                }
            }

            ScrollView(.vertical, showsIndicators: false) {
                VStack(spacing: 4) {
                    ForEach(yearTools.prefix(8)) { t in
                        HStack(spacing: 6) {
                            Circle()
                                .fill(Color(hex: t.color))
                                .frame(width: 6, height: 6)
                            Text(t.displayName)
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                            if t.activeDays > 0 {
                                Text("\(t.activeDays)天")
                                    .font(.system(size: 9.5))
                                    .foregroundStyle(.secondary)
                                    .padding(.horizontal, 5)
                                    .padding(.vertical, 1)
                                    .background(Color.primary.opacity(0.06), in: Capsule())
                            }
                            Spacer()
                            Text("\(Formatters.compactTokens(t.totalTokens)) · \(Formatters.usd(t.costUsd))")
                                .font(.system(size: 10.5, weight: .semibold))
                                .monospacedDigit()
                                .foregroundStyle(.primary)
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 5.5)
                        .background(cardSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }
                }
            }
            .frame(maxHeight: 138)

            HStack {
                Button {
                    store.openDataDirectory()
                } label: {
                    Text("打开 ~/.agent-pulse")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 9)
                        .padding(.vertical, 5)
                        .background(cardSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
                }
                .buttonStyle(.plain)

                Spacer()

                Button {
                    store.copyMarkdownReport()
                } label: {
                    Text(store.copiedToast ? "已复制日报" : "复制今日融合日报")
                        .font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(colorScheme == .dark ? Color(hex: "#EDE9FE") : accentPurple)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 5)
                        .background(accentPurple.opacity(0.20), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                }
                .buttonStyle(.plain)
            }
        }
    }

    private func historyBarChart(points: [HistoryDayPoint]) -> some View {
        let values: [Double] = points.map { pt in
            switch store.selectedHistoryMetric {
            case .hours: return pt.agentHours
            case .tokens: return Double(pt.totalTokens)
            case .cost: return pt.costUsd
            }
        }
        let maxVal = max(0.01, values.max() ?? 1.0)

        return VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .bottom, spacing: 4) {
                ForEach(Array(points.enumerated()), id: \.element.id) { idx, pt in
                    let val = values[idx]
                    let ratio = min(1.0, max(0.06, val / maxVal))
                    let isHov = hoveredHistoryDate == pt.date
                    RoundedRectangle(cornerRadius: 3, style: .continuous)
                        .fill(
                            pt.isToday
                                ? AnyShapeStyle(liveEmerald)
                                : AnyShapeStyle(accentPurple.opacity(isHov ? 1.0 : 0.72))
                        )
                        .frame(maxWidth: .infinity)
                        .frame(height: max(4, CGFloat(ratio) * 58))
                        .onHover { hovering in
                            hoveredHistoryDate = hovering ? pt.date : (hoveredHistoryDate == pt.date ? nil : hoveredHistoryDate)
                        }
                        .help("\(pt.date) (\(pt.weekday)) · \(String(format: "%.2fh", pt.agentHours)) · \(Formatters.compactTokens(pt.totalTokens)) tok · \(Formatters.usd(pt.costUsd))")
                }
            }
            .frame(height: 62, alignment: .bottom)
            .padding(.horizontal, 8)
            .padding(.top, 8)
            .padding(.bottom, 6)
            .background(cardSurface, in: RoundedRectangle(cornerRadius: 9, style: .continuous))

            if let hov = hoveredHistoryDate, let pt = points.first(where: { $0.date == hov }) {
                Text("\(pt.date) \(pt.weekday)：\(String(format: "%.2fh", pt.agentHours)) · \(Formatters.compactTokens(pt.totalTokens)) tok · \(Formatters.usd(pt.costUsd))")
                    .font(.system(size: 9.5, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - TAB 4: Settings

    private var settingsTab: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 9) {
                // Sit Reminder Card
                VStack(alignment: .leading, spacing: 7) {
                    Text("久坐提醒（已承接 Tokei 偏好）")
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
        .frame(maxHeight: 376)
    }
}
