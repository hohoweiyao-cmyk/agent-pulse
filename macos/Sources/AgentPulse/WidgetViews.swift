import AppKit
import SwiftUI

struct VisualEffectSurface: NSViewRepresentable {
    var material: NSVisualEffectView.Material = .hudWindow
    var blendingMode: NSVisualEffectView.BlendingMode = .behindWindow

    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = material
        view.blendingMode = blendingMode
        view.state = .active
        view.wantsLayer = true
        return view
    }

    func updateNSView(_ nsView: NSVisualEffectView, context: Context) {
        nsView.material = material
        nsView.blendingMode = blendingMode
        nsView.state = .active
    }
}

struct WidgetRootView: View {
    @ObservedObject var store: ClockStore
    @Environment(\.colorScheme) private var colorScheme
    @State private var hoveredDay: String?
    @State private var pulseActive = false

    private var cardWidth: CGFloat { store.isCompact ? 276 : 340 }
    private var liveEmerald: Color {
        colorScheme == .dark ? Color(hex: "#34D399") : Color(hex: "#15803D")
    }
    private var amberAccent: Color {
        colorScheme == .dark ? Color(hex: "#FBBF24") : Color(hex: "#B45309")
    }

    var body: some View {
        ZStack {
            VisualEffectSurface(material: .hudWindow, blendingMode: .behindWindow)
                .overlay(
                    LinearGradient(
                        colors: colorScheme == .dark
                            ? [Color.white.opacity(0.06), Color.clear, Color.black.opacity(0.14)]
                            : [Color.white.opacity(0.55), Color.white.opacity(0.22), Color.clear],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    )
                )

            VStack(alignment: .leading, spacing: store.isCompact ? 11 : 14) {
                headerRow

                if let day = store.displayedDay {
                    heroSection(day: day)

                    if store.isViewingToday, let agents = store.snapshot?.activeAgents, !agents.isEmpty {
                        liveAgentsStrip(agents: agents)
                    }

                    segmentedBar(tools: day.tools)

                    if store.isCompact {
                        compactLegend(tools: day.tools)
                    } else {
                        toolsBreakdownList(tools: day.tools)
                        hairlineDivider
                        sevenDayChart
                        if store.cardShowQuotaAlert,
                           let hw = store.quotaSnapshot?.highestWindow,
                           hw.usedPercent >= 70.0 {
                            quotaAlertStrip(window: hw)
                        }
                        hairlineDivider
                        footerRow
                    }
                } else if let err = store.lastError {
                    Text(err)
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)
                        .padding(.vertical, 8)
                } else {
                    HStack(spacing: 8) {
                        ProgressView().controlSize(.small)
                        Text("正在读取本机 AI 工时…")
                            .font(.system(size: 12))
                            .foregroundStyle(.secondary)
                    }
                    .padding(.vertical, 12)
                }
            }
            .padding(.horizontal, store.isCompact ? 15 : 18)
            .padding(.vertical, store.isCompact ? 14 : 16)
        }
        .frame(width: cardWidth)
        .clipShape(RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 20, style: .continuous)
                .strokeBorder(
                    LinearGradient(
                        colors: colorScheme == .dark
                            ? [Color.white.opacity(0.22), Color.white.opacity(0.08)]
                            : [Color.white.opacity(0.85), Color.black.opacity(0.10)],
                        startPoint: .topLeading,
                        endPoint: .bottomTrailing
                    ),
                    lineWidth: 0.8
                )
        )
        .onAppear {
            withAnimation(.easeInOut(duration: 1.5).repeatForever(autoreverses: true)) {
                pulseActive = true
            }
        }
    }

    // MARK: - Header

    private var headerRow: some View {
        HStack(spacing: 8) {
            let activeCount = store.snapshot?.activeAgents.count ?? 0

            HStack(spacing: 6) {
                HStack(spacing: 6) {
                    ZStack {
                        if activeCount > 0 {
                            Circle()
                                .fill(liveEmerald.opacity(pulseActive ? 0.16 : 0.38))
                                .frame(width: 14, height: 14)
                        }
                        Circle()
                            .fill(activeCount > 0 ? liveEmerald : Color.secondary.opacity(0.45))
                            .frame(width: 7, height: 7)
                    }
                    .frame(width: 14, height: 14)

                    Text("AgentPulse")
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(.primary)
                }
                .frame(height: 24)
                .overlay { WindowDragRegion() }

                if !store.isViewingToday, let day = store.displayedDay {
                    Button {
                        withAnimation(.easeOut(duration: 0.18)) {
                            store.selectedDate = nil
                        }
                    } label: {
                        HStack(spacing: 3) {
                            Text("\(day.shortDate) \(day.weekday)")
                            Image(systemName: "arrow.uturn.backward")
                                .font(.system(size: 8.5, weight: .bold))
                        }
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(Color(hex: "#7C6BF5"))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(Color(hex: "#7C6BF5").opacity(0.14), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .help("返回查看今日")
                } else if activeCount > 0 {
                    Text("\(activeCount) 在岗")
                        .font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(liveEmerald)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(liveEmerald.opacity(0.14), in: Capsule())
                }
            }

            Spacer(minLength: 4)
                .frame(height: 24)
                .overlay { WindowDragRegion() }

            HStack(spacing: 4) {
                headerIconButton(
                    symbol: store.windowLayer.symbolName,
                    active: store.windowLayer == .floating,
                    tint: Color(hex: "#F59E0B"),
                    tooltip: "当前：\(store.windowLayer.title)（点击切换桌面层 / 悬浮置顶）"
                ) {
                    store.windowLayer = store.windowLayer.toggledPrimary()
                }

                headerIconButton(
                    symbol: store.isCompact ? "rectangle.expand.vertical" : "rectangle.compress.vertical",
                    active: false,
                    tint: .primary,
                    tooltip: store.isCompact ? "展开完整看板" : "切换紧凑挂件"
                ) {
                    store.isCompact.toggle()
                }

                headerIconButton(
                    symbol: "arrow.clockwise",
                    active: false,
                    tint: .primary,
                    spinning: store.isRefreshing,
                    tooltip: "立即刷新"
                ) {
                    store.refresh()
                }
            }
        }
    }

    private func headerIconButton(
        symbol: String,
        active: Bool,
        tint: Color,
        spinning: Bool = false,
        tooltip: String,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 10.5, weight: .semibold))
                .foregroundStyle(active ? tint : .secondary)
                .rotationEffect(.degrees(spinning ? 360 : 0))
                .animation(spinning ? .linear(duration: 0.7).repeatForever(autoreverses: false) : .default, value: spinning)
                .frame(width: 24, height: 24)
                .background(
                    (active ? tint.opacity(0.15) : Color.primary.opacity(0.06)),
                    in: RoundedRectangle(cornerRadius: 7, style: .continuous)
                )
        }
        .buttonStyle(.plain)
        .help(tooltip)
    }

    // MARK: - Hero Section

    private func heroSection(day: DayStats) -> some View {
        let secs = store.displayedAgentSeconds
        let workdays = secs / (8.0 * 3600.0)
        let lev = day.leverage
        let levColor: Color = lev >= 1.4 ? liveEmerald : (lev >= 1.15 ? amberAccent : .secondary)

        return HStack(alignment: .lastTextBaseline, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(Formatters.heroHours(secs))
                        .font(.system(size: store.isCompact ? 34 : 40, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .tracking(-1.0)
                        .foregroundStyle(.primary)
                        .contentTransition(.numericText())

                    Text("h")
                        .font(.system(size: store.isCompact ? 15 : 17, weight: .semibold, design: .rounded))
                        .foregroundStyle(.secondary)
                }

                Text(Formatters.humanDurationCN(secs))
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }

            Spacer(minLength: 6)

            VStack(alignment: .trailing, spacing: 5) {
                HStack(spacing: 5) {
                    Text(String(format: "%.2f 员工日", workdays))
                        .font(.system(size: 11, weight: .semibold))
                        .monospacedDigit()
                        .foregroundStyle(.primary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.primary.opacity(0.07), in: Capsule())

                    Text(store.isCompact ? String(format: "%.2f×", lev) : String(format: "%.2f× 并行", lev))
                        .font(.system(size: 11, weight: .bold))
                        .monospacedDigit()
                        .foregroundStyle(levColor)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(levColor.opacity(lev >= 1.15 ? 0.16 : 0.08), in: Capsule())
                        .help("并行杠杆率 = AI 累计工时 ÷ 去重墙钟时长")
                }

                Text(String(format: "墙钟 %.2fh · %d 轮", day.wallHours, day.runs))
                    .font(.system(size: 10.5, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - Live Running Agents Strip

    private func liveAgentsStrip(agents: [ActiveAgent]) -> some View {
        VStack(spacing: 5) {
            ForEach(agents.prefix(store.isCompact ? 1 : 2)) { agent in
                let liveElapsed = max(0, store.nowEpoch - agent.startedAt)
                HStack(spacing: 7) {
                    Circle()
                        .fill(Color(hex: agent.color))
                        .frame(width: 6, height: 6)

                    Text(agent.shortName)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.primary)

                    if !agent.label.isEmpty {
                        Text(agent.label)
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }

                    Spacer(minLength: 4)

                    if store.isCompact && agents.count > 1 {
                        Text("+\(agents.count - 1)")
                            .font(.system(size: 10, weight: .semibold))
                            .foregroundStyle(liveEmerald)
                    }

                    Text(Formatters.liveTimer(liveElapsed))
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(liveEmerald)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 6)
                .background(
                    liveEmerald.opacity(colorScheme == .dark ? 0.14 : 0.10),
                    in: RoundedRectangle(cornerRadius: 10, style: .continuous)
                )
                .overlay(
                    RoundedRectangle(cornerRadius: 10, style: .continuous)
                        .strokeBorder(liveEmerald.opacity(0.26), lineWidth: 0.6)
                )
            }
        }
    }

    // MARK: - Segmented Bar

    private func segmentedBar(tools: [ToolBreakdown]) -> some View {
        GeometryReader { geo in
            if tools.isEmpty {
                Capsule()
                    .fill(Color.primary.opacity(0.08))
            } else {
                let gap: CGFloat = 2.5
                let visibleTools = Array(tools.prefix(6))
                let totalShare = max(0.0001, visibleTools.reduce(0) { $0 + $1.share })
                let usableWidth = max(0, geo.size.width - gap * CGFloat(max(0, visibleTools.count - 1)))

                HStack(spacing: gap) {
                    ForEach(visibleTools) { tool in
                        let w = max(4, usableWidth * CGFloat(tool.share / totalShare))
                        RoundedRectangle(cornerRadius: 3.5, style: .continuous)
                            .fill(Color(hex: tool.color))
                            .frame(width: w)
                    }
                }
            }
        }
        .frame(height: 7)
    }

    // MARK: - Compact Legend

    private func compactLegend(tools: [ToolBreakdown]) -> some View {
        HStack(spacing: 10) {
            if tools.isEmpty {
                Text("今日暂无 AI 工时记录")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
            } else {
                ForEach(tools.prefix(2)) { tool in
                    HStack(spacing: 4) {
                        Circle()
                            .fill(Color(hex: tool.color))
                            .frame(width: 6, height: 6)
                        Text(tool.shortName)
                            .font(.system(size: 10.5, weight: .medium))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        Text(Formatters.shortDuration(tool.agentSeconds))
                            .font(.system(size: 10.5, weight: .semibold))
                            .monospacedDigit()
                            .foregroundStyle(.primary)
                            .fixedSize()
                    }
                }
                if tools.count > 2 {
                    Text("+\(tools.count - 2)")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.tertiary)
                        .fixedSize()
                }
            }
            Spacer(minLength: 0)
        }
    }

    // MARK: - Tools Breakdown (Expanded)

    private func toolsBreakdownList(tools: [ToolBreakdown]) -> some View {
        VStack(spacing: 6) {
            if tools.isEmpty {
                Text("当天暂无 AI 工时记录")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.vertical, 4)
            } else {
                ForEach(tools.prefix(5)) { tool in
                    HStack(spacing: 8) {
                        Circle()
                            .fill(Color(hex: tool.color))
                            .frame(width: 7, height: 7)

                        Text(tool.displayName)
                            .font(.system(size: 12, weight: .medium))
                            .foregroundStyle(.primary)
                            .lineLimit(1)

                        Text("\(tool.runs)轮")
                            .font(.system(size: 10, weight: .medium))
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1.5)
                            .background(Color.primary.opacity(0.06), in: Capsule())

                        Spacer(minLength: 4)

                        Text(Formatters.shortDuration(tool.agentSeconds))
                            .font(.system(size: 12, weight: .semibold))
                            .monospacedDigit()
                            .foregroundStyle(.primary)

                        Text("\(Int(round(tool.share * 100)))%")
                            .font(.system(size: 10.5, weight: .medium))
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                            .frame(width: 30, alignment: .trailing)
                    }
                }
            }
        }
    }

    // MARK: - 7-Day Trend Bar Chart

    private var sevenDayChart: some View {
        let days = store.snapshot?.last7Days ?? []
        let summary = store.snapshot?.summary7d
        let maxHrs = max(1.0, days.map(\.agentHours).max() ?? 1.0)
        let selectedKey = store.displayedDay?.date

        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("近 7 天工时")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.secondary)

                Spacer()

                if let s = summary {
                    Text(String(format: "共 %.1fh · 日均 %.1fh", s.totalHours, s.avgDailyHours))
                        .font(.system(size: 10.5, weight: .medium))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }

            HStack(alignment: .bottom, spacing: 8) {
                ForEach(days) { d in
                    let isSelected = d.date == selectedKey
                    let isHovered = d.date == hoveredDay
                    let ratio = min(1.0, max(0.06, d.agentHours / maxHrs))
                    let barHeight = max(5, CGFloat(ratio) * 52)

                    Button {
                        withAnimation(.easeOut(duration: 0.16)) {
                            store.selectDate(d.date)
                        }
                    } label: {
                        VStack(spacing: 4) {
                            Text(d.agentHours >= 0.05 ? String(format: "%.1f", d.agentHours) : "0")
                                .font(.system(size: 9.5, weight: isSelected ? .bold : .medium))
                                .monospacedDigit()
                                .foregroundStyle(isSelected ? .primary : .secondary)
                                .opacity(isSelected || isHovered || d.agentHours == maxHrs ? 1.0 : 0.55)

                            ZStack(alignment: .bottom) {
                                RoundedRectangle(cornerRadius: 5, style: .continuous)
                                    .fill(Color.primary.opacity(0.05))
                                    .frame(height: 52)

                                RoundedRectangle(cornerRadius: 5, style: .continuous)
                                    .fill(barFill(isSelected: isSelected, isToday: d.isToday, isHovered: isHovered))
                                    .frame(height: barHeight)
                            }

                            Text(Formatters.shortWeekday(d.weekday, isToday: d.isToday))
                                .font(.system(size: 10, weight: isSelected || d.isToday ? .semibold : .regular))
                                .foregroundStyle(isSelected ? Color(hex: "#7C6BF5") : .secondary)
                        }
                        .frame(maxWidth: .infinity)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .onHover { hovering in
                        hoveredDay = hovering ? d.date : (hoveredDay == d.date ? nil : hoveredDay)
                    }
                    .help("\(d.date) (\(d.weekday)) · \(String(format: "%.2fh", d.agentHours)) · 并行 \(String(format: "%.2f×", d.leverage))")
                }
            }
        }
    }

    private func barFill(isSelected: Bool, isToday: Bool, isHovered: Bool) -> AnyShapeStyle {
        if isSelected {
            return AnyShapeStyle(
                LinearGradient(
                    colors: [Color(hex: "#8B7CF6"), Color(hex: "#6366F1")],
                    startPoint: .top,
                    endPoint: .bottom
                )
            )
        }
        if isHovered {
            return AnyShapeStyle(Color(hex: "#7C6BF5").opacity(0.55))
        }
        if isToday {
            return AnyShapeStyle(Color(hex: "#7C6BF5").opacity(0.42))
        }
        return AnyShapeStyle(Color.primary.opacity(colorScheme == .dark ? 0.22 : 0.18))
    }

    // MARK: - Optional Quota Alert Strip (>= 70%)

    private func quotaAlertStrip(window: HighestQuotaWindow) -> some View {
        let tint = Formatters.quotaColor(for: window.usedPercent, defaultColor: amberAccent)
        return Button {
            store.openPopover(tab: .quotas)
        } label: {
            VStack(alignment: .leading, spacing: 3.5) {
                HStack(spacing: 5) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 9))
                        .foregroundStyle(tint)
                    Text("\(window.providerName) \(window.label)")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.primary)
                    Spacer()
                    Text("\(Int(round(window.usedPercent)))%")
                        .font(.system(size: 10, weight: .bold))
                        .monospacedDigit()
                        .foregroundStyle(tint)
                }
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.primary.opacity(0.08))
                        Capsule()
                            .fill(tint)
                            .frame(width: max(5, geo.size.width * CGFloat(min(100, window.usedPercent) / 100.0)))
                    }
                }
                .frame(height: 4)
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(tint.opacity(0.11), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(tint.opacity(0.25), lineWidth: 0.6))
        }
        .buttonStyle(.plain)
        .help("点击查看订阅额度详情")
    }

    // MARK: - Footer

    private var footerRow: some View {
        let dayTok = store.displayedDayTokens
        let dayCost = store.displayedDayCostUsd
        let accentPurple = Color(hex: "#7C6BF5")

        return HStack(spacing: 8) {
            if dayTok > 0 || dayCost > 0 {
                Button {
                    store.openPopover(tab: .overview)
                } label: {
                    HStack(spacing: 5) {
                        Text("自有")
                            .font(.system(size: 8.5, weight: .bold))
                            .foregroundStyle(.white)
                            .padding(.horizontal, 4.5)
                            .padding(.vertical, 1)
                            .background(accentPurple.opacity(0.75), in: RoundedRectangle(cornerRadius: 4, style: .continuous))

                        Text("\(Formatters.compactTokens(dayTok)) tok · \(Formatters.usd(dayCost))")
                            .font(.system(size: 10, weight: .semibold))
                            .monospacedDigit()
                            .foregroundStyle(colorScheme == .dark ? Color(hex: "#E9D5FF") : accentPurple)
                            .lineLimit(1)
                    }
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3)
                    .background(accentPurple.opacity(colorScheme == .dark ? 0.18 : 0.10), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .strokeBorder(accentPurple.opacity(0.32), lineWidth: 0.6)
                    )
                }
                .buttonStyle(.plain)
                .help("AgentPulse 自有增量引擎统计（点击打开菜单栏详单）")
            } else {
                Button {
                    store.openPopover(tab: .overview)
                } label: {
                    Text("步间心跳 5m · 点击看详单")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
                .buttonStyle(.plain)
            }

            Spacer(minLength: 4)

            if store.copiedToast {
                HStack(spacing: 3) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 9.5, weight: .bold))
                    Text("已复制日报")
                        .font(.system(size: 10, weight: .semibold))
                }
                .foregroundStyle(Color(hex: "#22C55E"))
                .transition(.opacity)
            } else {
                Text(store.lastUpdatedTimeString)
                    .font(.system(size: 10, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.tertiary)

                Button {
                    store.copyMarkdownReport()
                } label: {
                    Image(systemName: "doc.on.doc")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: 22, height: 20)
                        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                }
                .buttonStyle(.plain)
                .help("复制所选日期的融合 Markdown 日报（工时 + Token + 费用）")
            }
        }
    }

    private var hairlineDivider: some View {
        Rectangle()
            .fill(Color.primary.opacity(colorScheme == .dark ? 0.10 : 0.08))
            .frame(height: 0.5)
    }
}
