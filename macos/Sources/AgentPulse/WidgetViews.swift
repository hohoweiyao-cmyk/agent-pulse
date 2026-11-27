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

// MARK: - Shared Pointer Cursor Helper

private struct PointingHandHoverModifier: ViewModifier {
    let enabled: Bool
    @State private var isPushed = false

    func body(content: Content) -> some View {
        content
            .onHover { hovering in
                guard enabled else {
                    if isPushed {
                        NSCursor.pop()
                        isPushed = false
                    }
                    return
                }
                if hovering && !isPushed {
                    NSCursor.pointingHand.push()
                    isPushed = true
                } else if !hovering && isPushed {
                    NSCursor.pop()
                    isPushed = false
                }
            }
            .onDisappear {
                if isPushed {
                    NSCursor.pop()
                    isPushed = false
                }
            }
    }
}

extension View {
    func pointingHandCursor(_ enabled: Bool = true) -> some View {
        modifier(PointingHandHoverModifier(enabled: enabled))
    }
}

// MARK: - Shared Shanghai Timestamp Formatter

enum ViewTimeFormatters {
    private static let shanghaiTimeZone = TimeZone(identifier: "Asia/Shanghai") ?? TimeZone(secondsFromGMT: 8 * 3600)!

    static func hhmm(_ epoch: Double?) -> String {
        guard let epoch, epoch.isFinite, epoch > 0 else { return "--:--" }
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        f.timeZone = shanghaiTimeZone
        return f.string(from: Date(timeIntervalSince1970: epoch))
    }

    static func monthDayHHmm(_ epoch: Double, now: Double) -> String {
        let fDay = DateFormatter()
        fDay.dateFormat = "yyyy-MM-dd"
        fDay.timeZone = shanghaiTimeZone
        let targetDate = Date(timeIntervalSince1970: epoch)
        let nowDate = Date(timeIntervalSince1970: now)
        let f = DateFormatter()
        f.timeZone = shanghaiTimeZone
        if fDay.string(from: targetDate) == fDay.string(from: nowDate) {
            f.dateFormat = "HH:mm"
        } else {
            f.dateFormat = "MM/dd HH:mm"
        }
        return f.string(from: targetDate)
    }
}

// MARK: - Shared Freshness Degradation Banner

struct FreshnessBannerView: View {
    @ObservedObject var store: ClockStore
    var compact: Bool = false
    @Environment(\.colorScheme) private var colorScheme

    private var amberAccent: Color {
        colorScheme == .dark ? Color(hex: "#FBBF24") : Color(hex: "#B45309")
    }
    private var neutralMuted: Color {
        colorScheme == .dark ? Color(hex: "#94A3B8") : Color(hex: "#64748B")
    }

    var body: some View {
        let freshness = store.fastFreshness
        if freshness != .fresh {
            let tint = freshness == .unavailable ? neutralMuted : amberAccent
            let icon = iconName(for: freshness)
            let message = bannerText(for: freshness)

            HStack(spacing: 5.5) {
                Image(systemName: icon)
                    .font(.system(size: compact ? 9.5 : 10, weight: .semibold))
                    .foregroundStyle(tint)
                Text(message)
                    .font(.system(size: compact ? 10 : 10.5, weight: .medium))
                    .foregroundStyle(colorScheme == .dark ? tint.opacity(0.95) : tint)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Spacer(minLength: 2)
                if let err = store.lastError, !compact {
                    Image(systemName: "info.circle")
                        .font(.system(size: 9.5))
                        .foregroundStyle(tint.opacity(0.75))
                        .help(err)
                }
            }
            .padding(.horizontal, compact ? 8 : 9)
            .padding(.vertical, compact ? 4.5 : 5.5)
            .background(
                tint.opacity(colorScheme == .dark ? 0.13 : 0.10),
                in: RoundedRectangle(cornerRadius: compact ? 8 : 8.5, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: compact ? 8 : 8.5, style: .continuous)
                    .strokeBorder(tint.opacity(0.28), lineWidth: 0.6)
            )
            .help(store.lastError ?? message)
            .accessibilityElement(children: .combine)
            .accessibilityLabel(message)
        }
    }

    private func iconName(for freshness: FastFreshness) -> String {
        switch freshness {
        case .degraded:
            return "exclamationmark.triangle.fill"
        case .stale:
            return "clock.badge.exclamationmark"
        case .unavailable:
            return "wifi.slash"
        case .fresh:
            return "checkmark.circle.fill"
        }
    }

    private func bannerText(for freshness: FastFreshness) -> String {
        let tsText = ViewTimeFormatters.hhmm(store.lastTrustedSuccessTimestamp)
        switch freshness {
        case .degraded:
            return "部分数据暂未读全 · 上次更新 \(tsText)"
        case .stale:
            if store.isCrossedMidnightFromSnapshot {
                return "已跨日待刷新 · 上次更新 \(tsText)"
            }
            if store.lastError != nil {
                return "刷新暂未成功 · 上次更新 \(tsText)"
            }
            return "数据待刷新 · 上次更新 \(tsText)"
        case .unavailable:
            return "暂未读到本机工时数据"
        case .fresh:
            return ""
        }
    }
}

// MARK: - Shared 24-Hour Pulse Chart (00-23)

struct HourlyPulseChartView: View {
    let day: DayStats
    var compact: Bool = true
    var titlePrefix: String? = nil

    @Environment(\.colorScheme) private var colorScheme
    @State private var hoveredHour: Int?

    private var accentPurple: Color { Color(hex: "#7C6BF5") }
    private var peakCyan: Color {
        colorScheme == .dark ? Color(hex: "#38BDF8") : Color(hex: "#0284C7")
    }
    private var nightIndigo: Color {
        colorScheme == .dark ? Color(hex: "#818CF8") : Color(hex: "#6366F1")
    }

    private var chartHeight: CGFloat { compact ? 30 : 40 }

    private var peakHour: (hour: Int, seconds: Double)? {
        guard day.hourlyChartState == .available, day.hourlySeconds.count == 24 else { return nil }
        var bestHour = 0
        var bestSec = 0.0
        for (idx, sec) in day.hourlySeconds.enumerated() where sec > bestSec {
            bestSec = sec
            bestHour = idx
        }
        return bestSec > 0 ? (bestHour, bestSec) : nil
    }

    var body: some View {
        VStack(alignment: .leading, spacing: compact ? 5 : 6) {
            HStack(spacing: 6) {
                Text(titlePrefix ?? "24h 昼夜脉搏")
                    .font(.system(size: compact ? 10.5 : 11, weight: .semibold))
                    .foregroundStyle(.secondary)

                Spacer(minLength: 4)

                trailingCaption
            }

            switch day.hourlyChartState {
            case .available:
                barsSection
            case .unavailable:
                placeholderBox(icon: "clock.badge.questionmark", text: "该日无小时分布")
            case .unknown:
                placeholderBox(icon: "clock.badge.exclamationmark", text: "暂未获取到工时观测")
            }
        }
    }

    private func placeholderBox(icon: String, text: String) -> some View {
        HStack(spacing: 6) {
            Image(systemName: icon)
                .font(.system(size: 10.5))
                .foregroundStyle(.tertiary)
            Text(text)
                .font(.system(size: 10.5, weight: .medium))
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .frame(height: chartHeight + 14)
        .background(
            Color.primary.opacity(colorScheme == .dark ? 0.04 : 0.03),
            in: RoundedRectangle(cornerRadius: 7, style: .continuous)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 7, style: .continuous)
                .strokeBorder(Color.primary.opacity(colorScheme == .dark ? 0.08 : 0.06), lineWidth: 0.5)
        )
        .accessibilityLabel(text)
    }

    @ViewBuilder
    private var trailingCaption: some View {
        switch day.hourlyChartState {
        case .available:
            if let hour = hoveredHour, hour < day.hourlySeconds.count {
                let sec = day.hourlySeconds[hour]
                Text(String(format: "%02d:00–%02d:59 · %@", hour, hour, sec > 0 ? Formatters.shortDuration(sec) : "无活跃"))
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .foregroundStyle(sec > 0 ? accentPurple : .secondary)
                    .transition(.opacity)
            } else if let peak = peakHour {
                Text(String(format: "峰值 %02d点 · %@", peak.hour, Formatters.shortDuration(peak.seconds)))
                    .font(.system(size: 10, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            } else {
                Text("暂无活跃时段")
                    .font(.system(size: 10, weight: .medium))
                    .foregroundStyle(.tertiary)
            }
        case .unavailable:
            Text(day.shortDate)
                .font(.system(size: 10, weight: .medium))
                .monospacedDigit()
                .foregroundStyle(.tertiary)
        case .unknown:
            Text("—")
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
    }

    private var barsSection: some View {
        let buckets = day.hourlySeconds.count == 24 ? day.hourlySeconds : Array(repeating: 0.0, count: 24)
        let maxSec = buckets.max() ?? 0.0

        return VStack(spacing: 3) {
            ZStack(alignment: .bottom) {
                Rectangle()
                    .fill(Color.primary.opacity(colorScheme == .dark ? 0.12 : 0.10))
                    .frame(height: 0.6)

                HStack(alignment: .bottom, spacing: compact ? 2 : 3) {
                    ForEach(0..<24, id: \.self) { hour in
                        let sec = buckets[hour]
                        let isHovered = hoveredHour == hour
                        let isPeak = maxSec > 0 && sec >= maxSec * 0.99
                        let ratio = maxSec > 0 ? min(1.0, sec / maxSec) : 0.0
                        let barHeight: CGFloat = sec > 0
                            ? max(4, CGFloat(pow(ratio, 0.78)) * chartHeight)
                            : 2.0

                        ZStack(alignment: .bottom) {
                            Color.clear
                                .frame(height: chartHeight)

                            RoundedRectangle(cornerRadius: compact ? 1.8 : 2.2, style: .continuous)
                                .fill(barStyle(hour: hour, sec: sec, isPeak: isPeak, isHovered: isHovered))
                                .frame(height: barHeight)
                                .overlay(
                                    RoundedRectangle(cornerRadius: compact ? 1.8 : 2.2, style: .continuous)
                                        .strokeBorder(
                                            isHovered ? Color.primary.opacity(0.45) : Color.clear,
                                            lineWidth: 0.7
                                        )
                                )
                        }
                        .frame(maxWidth: .infinity)
                        .contentShape(Rectangle())
                        .onHover { hovering in
                            if hovering {
                                hoveredHour = hour
                            } else if hoveredHour == hour {
                                hoveredHour = nil
                            }
                        }
                        .help(String(format: "%@ %02d:00–%02d:59 · %@", day.date, hour, hour, sec > 0 ? Formatters.humanDurationCN(sec) : "0 分钟"))
                        .accessibilityLabel(String(format: "%02d点到%02d点59分，工时%@", hour, hour, sec > 0 ? Formatters.humanDurationCN(sec) : "0分钟"))
                    }
                }
            }
            .frame(height: chartHeight)

            HStack {
                Text("00")
                Spacer()
                Text("06")
                Spacer()
                Text("12")
                Spacer()
                Text("18")
                Spacer()
                Text("23")
            }
            .font(.system(size: 8.5, weight: .medium, design: .monospaced))
            .foregroundStyle(.tertiary)
        }
    }

    private func barStyle(hour: Int, sec: Double, isPeak: Bool, isHovered: Bool) -> AnyShapeStyle {
        if sec <= 0 {
            return AnyShapeStyle(Color.primary.opacity(isHovered ? 0.25 : (colorScheme == .dark ? 0.11 : 0.09)))
        }
        if isHovered {
            return AnyShapeStyle(
                LinearGradient(
                    colors: [peakCyan, accentPurple],
                    startPoint: .top,
                    endPoint: .bottom
                )
            )
        }
        if isPeak {
            return AnyShapeStyle(
                LinearGradient(
                    colors: [peakCyan.opacity(0.95), accentPurple],
                    startPoint: .top,
                    endPoint: .bottom
                )
            )
        }
        let isNight = hour < 6 || hour >= 22
        if isNight {
            return AnyShapeStyle(
                LinearGradient(
                    colors: [nightIndigo.opacity(0.85), nightIndigo.opacity(0.42)],
                    startPoint: .top,
                    endPoint: .bottom
                )
            )
        }
        return AnyShapeStyle(
            LinearGradient(
                colors: [Color(hex: "#9B8CFC"), accentPurple.opacity(0.65)],
                startPoint: .top,
                endPoint: .bottom
            )
        )
    }
}

// MARK: - Desktop Widget Root View

struct WidgetRootView: View {
    @ObservedObject var store: ClockStore
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    @State private var hoveredDay: String?
    @State private var hoveredSentinelId: String?
    @State private var isSentinelExpanded: Bool = false
    @State private var pulseActive = false
    @State private var errorDismissTask: Task<Void, Never>?

    private var cardWidth: CGFloat { store.isCompact ? 276 : 340 }
    private var accentPurple: Color { Color(hex: "#7C6BF5") }
    private var liveEmerald: Color {
        colorScheme == .dark ? Color(hex: "#34D399") : Color(hex: "#15803D")
    }
    private var amberAccent: Color {
        colorScheme == .dark ? Color(hex: "#FBBF24") : Color(hex: "#B45309")
    }
    private var finishedSlate: Color {
        colorScheme == .dark ? Color(hex: "#94A3B8") : Color(hex: "#475569")
    }
    private var finishedMint: Color {
        colorScheme == .dark ? Color(hex: "#5EEAD4") : Color(hex: "#0F766E")
    }

    private var sentinelDefaultLimit: Int { store.isCompact ? 1 : 2 }

    var body: some View {
        let tasks = store.sentinelTasks

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

            VStack(alignment: .leading, spacing: store.isCompact ? 10 : 12.5) {
                headerRow(tasks: tasks)

                if let day = store.displayedDay {
                    if store.fastFreshness != .fresh {
                        FreshnessBannerView(store: store, compact: true)
                    }

                    heroSection(day: day)

                    if !tasks.isEmpty {
                        sentinelSection(tasks: tasks)
                    }

                    if let actionError = store.projectActionError {
                        inlineNoticeBanner(message: actionError) {
                            store.projectActionError = nil
                        }
                    }

                    segmentedBar(tools: day.tools)

                    if store.isCompact {
                        compactLegend(tools: day.tools)
                    } else {
                        toolsBreakdownList(tools: day.tools)
                        hairlineDivider
                        HourlyPulseChartView(day: day, compact: true)
                        hairlineDivider
                        sevenDayChart
                        if store.cardShowQuotaAlert,
                           let hw = store.highestFreshQuotaWindow,
                           hw.usedPercent >= 70.0 {
                            quotaAlertStrip(window: hw)
                        }
                        if let repErr = store.reportError {
                            let bannerMsg = repErr.contains("剪贴板") ? repErr : "复制失败（剪贴板未改动）：\(repErr)"
                            inlineNoticeBanner(message: bannerMsg) {
                                store.reportError = nil
                            }
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
            syncPulseAnimation()
        }
        .onChange(of: reduceMotion) { _ in
            syncPulseAnimation()
        }
        .onChange(of: tasks.count) { count in
            if count <= sentinelDefaultLimit && isSentinelExpanded {
                isSentinelExpanded = false
                notifyPanelLayoutChanged()
            }
        }
        .onChange(of: store.isCompact) { _ in
            isSentinelExpanded = false
            notifyPanelLayoutChanged()
        }
        .onChange(of: store.projectActionError) { newVal in
            notifyPanelLayoutChanged()
            errorDismissTask?.cancel()
            guard let expected = newVal else { return }
            errorDismissTask = Task { @MainActor in
                try? await Task.sleep(nanoseconds: 4_000_000_000)
                if !Task.isCancelled && store.projectActionError == expected {
                    store.projectActionError = nil
                }
            }
        }
        .onChange(of: store.reportError) { _ in
            notifyPanelLayoutChanged()
        }
    }

    private func syncPulseAnimation() {
        if reduceMotion {
            pulseActive = false
        } else {
            withAnimation(.easeInOut(duration: 1.5).repeatForever(autoreverses: true)) {
                pulseActive = true
            }
        }
    }

    private func notifyPanelLayoutChanged() {
        DispatchQueue.main.async {
            store.onLayoutChanged?()
        }
    }

    // MARK: - Header

    private func headerRow(tasks: [SentinelTask]) -> some View {
        let freshness = store.fastFreshness
        let waitingCount = tasks.filter { $0.state == .waitingHuman }.count
        let runningCount = store.todayRunningCount
        let isStaleOrOffline = freshness == .stale || freshness == .unavailable
        let dotColor: Color = {
            if isStaleOrOffline { return amberAccent.opacity(0.85) }
            if waitingCount > 0 { return amberAccent }
            if runningCount > 0 { return liveEmerald }
            return Color.secondary.opacity(0.45)
        }()
        let shouldPulse = !reduceMotion && !isStaleOrOffline && runningCount > 0 && waitingCount == 0

        return HStack(spacing: 8) {
            HStack(spacing: 6) {
                HStack(spacing: 6) {
                    ZStack {
                        if shouldPulse {
                            Circle()
                                .fill(liveEmerald.opacity(pulseActive ? 0.14 : 0.36))
                                .frame(width: 14, height: 14)
                        }
                        Circle()
                            .fill(dotColor)
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
                        .foregroundStyle(accentPurple)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(accentPurple.opacity(0.14), in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .pointingHandCursor()
                    .help("返回查看今日")
                } else if isStaleOrOffline {
                    Text("离线")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(amberAccent)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 2)
                        .background(amberAccent.opacity(0.14), in: Capsule())
                } else if waitingCount > 0 {
                    let waitBadgeText = (!store.isCompact && runningCount > 0)
                        ? "\(waitingCount) 待拍板 · \(runningCount) 运行中"
                        : "\(waitingCount) 待拍板"
                    HStack(spacing: 3) {
                        Image(systemName: "hand.raised.fill")
                            .font(.system(size: 8.5, weight: .bold))
                        Text(waitBadgeText)
                            .font(.system(size: 10, weight: .semibold))
                            .monospacedDigit()
                            .lineLimit(1)
                    }
                    .fixedSize()
                    .foregroundStyle(amberAccent)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(amberAccent.opacity(0.15), in: Capsule())
                } else if runningCount > 0 {
                    Text("\(runningCount) 运行中")
                        .font(.system(size: 10, weight: .semibold))
                        .monospacedDigit()
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
                    spinning: store.isRefreshing && !reduceMotion,
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
        .pointingHandCursor()
        .help(tooltip)
        .accessibilityLabel(tooltip)
    }

    // MARK: - Hero Section

    private func heroSection(day: DayStats) -> some View {
        let isWorkUnknown = store.isDisplayedDayWorkUnknown || day.isWorkUnknown
        let secs = store.displayedAgentSeconds
        let workdays = secs / (8.0 * 3600.0)
        let lev = day.leverage
        let levColor: Color = isWorkUnknown
            ? .secondary
            : (lev >= 1.4 ? liveEmerald : (lev >= 1.15 ? amberAccent : .secondary))

        return HStack(alignment: .lastTextBaseline, spacing: 10) {
            VStack(alignment: .leading, spacing: 2) {
                HStack(alignment: .firstTextBaseline, spacing: 3) {
                    Text(store.displayedHeroHoursText)
                        .font(.system(size: store.isCompact ? 34 : 40, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .tracking(-1.0)
                        .foregroundStyle(isWorkUnknown ? .secondary : .primary)
                        .contentTransition(reduceMotion ? .identity : .numericText())

                    if !isWorkUnknown {
                        Text("h")
                            .font(.system(size: store.isCompact ? 15 : 17, weight: .semibold, design: .rounded))
                            .foregroundStyle(.secondary)
                    }
                }

                Text(isWorkUnknown ? "—" : Formatters.humanDurationCN(secs))
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
            }

            Spacer(minLength: 6)

            VStack(alignment: .trailing, spacing: 5) {
                HStack(spacing: 5) {
                    Text(isWorkUnknown ? "— 员工日" : String(format: "%.2f 员工日", workdays))
                        .font(.system(size: 11, weight: .semibold))
                        .monospacedDigit()
                        .foregroundStyle(isWorkUnknown ? .secondary : .primary)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(Color.primary.opacity(0.07), in: Capsule())

                    Text(
                        isWorkUnknown
                            ? (store.isCompact ? "—" : "— 并行")
                            : (store.isCompact ? String(format: "%.2f×", lev) : String(format: "%.2f× 并行", lev))
                    )
                        .font(.system(size: 11, weight: .bold))
                        .monospacedDigit()
                        .foregroundStyle(levColor)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 3)
                        .background(
                            isWorkUnknown
                                ? Color.primary.opacity(0.07)
                                : levColor.opacity(lev >= 1.15 ? 0.16 : 0.08),
                            in: Capsule()
                        )
                        .help("并行杠杆率 = AI 累计工时 ÷ 去重墙钟时长")
                }

                Text(isWorkUnknown ? "墙钟 — · — 轮" : String(format: "墙钟 %.2fh · %d 轮", day.wallHours, day.runs))
                    .font(.system(size: 10.5, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
            }
        }
    }

    // MARK: - 3-State Command Sentinel Section

    private func sentinelSection(tasks: [SentinelTask]) -> some View {
        let limit = sentinelDefaultLimit
        let hasOverflow = tasks.count > limit
        let visibleTasks = isSentinelExpanded ? tasks : Array(tasks.prefix(limit))
        let hiddenCount = max(0, tasks.count - limit)
        let needsScroll = isSentinelExpanded && visibleTasks.count > (store.isCompact ? 3 : 4)

        return VStack(alignment: .leading, spacing: 5) {
            if !store.isViewingToday {
                HStack(spacing: 5) {
                    Circle()
                        .fill(liveEmerald)
                        .frame(width: 5, height: 5)
                    Text("此刻实时")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(.secondary)
                    Text("非所选日")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(liveEmerald)
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(liveEmerald.opacity(0.12), in: Capsule())
                    Spacer(minLength: 4)
                    if hasOverflow {
                        sentinelExpandButton(hiddenCount: hiddenCount, totalCount: tasks.count)
                    }
                }
                .padding(.top, 1)
                .help("始终展示此刻正在运行或待处理的任务，不随上方历史日期切换")
            }

            if needsScroll {
                ScrollView(.vertical, showsIndicators: true) {
                    VStack(spacing: 5) {
                        ForEach(visibleTasks) { task in
                            sentinelTaskRow(task)
                        }
                    }
                    .padding(.trailing, 2)
                }
                .frame(maxHeight: store.isCompact ? 156 : 196)
            } else {
                VStack(spacing: 5) {
                    ForEach(visibleTasks) { task in
                        sentinelTaskRow(task)
                    }
                }
            }

            if store.isViewingToday && hasOverflow {
                HStack {
                    Spacer()
                    sentinelExpandButton(hiddenCount: hiddenCount, totalCount: tasks.count)
                }
            }
        }
    }

    private func sentinelExpandButton(hiddenCount: Int, totalCount: Int) -> some View {
        Button {
            withAnimation(reduceMotion ? nil : .easeOut(duration: 0.16)) {
                isSentinelExpanded.toggle()
            }
            notifyPanelLayoutChanged()
        } label: {
            HStack(spacing: 3.5) {
                Text(isSentinelExpanded ? "收起" : "+\(hiddenCount) 展开")
                    .font(.system(size: 10, weight: .semibold))
                    .monospacedDigit()
                Image(systemName: isSentinelExpanded ? "chevron.up" : "chevron.down")
                    .font(.system(size: 8, weight: .bold))
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, 7)
            .padding(.vertical, 2.5)
            .background(Color.primary.opacity(0.06), in: Capsule())
        }
        .buttonStyle(.plain)
        .pointingHandCursor()
        .help(isSentinelExpanded ? "收起多余任务" : "展开查看全部 \(totalCount) 个任务")
        .accessibilityLabel(isSentinelExpanded ? "收起任务列表" : "展开其余 \(hiddenCount) 个任务")
    }

    private func sentinelTaskRow(_ task: SentinelTask) -> some View {
        let style = sentinelVisualStyle(for: task.state)
        let isHovered = hoveredSentinelId == task.id
        let timerText = sentinelTimerText(for: task)
        let waitSubtitle = waitKindHint(task.evidence?.waitKind)
        let a11yLabel = "\(style.tag)，\(task.displayName)，\(task.label.isEmpty ? "进行中任务" : task.label)，耗时 \(timerText)"

        return Button {
            store.openSentinelTask(task)
        } label: {
            HStack(spacing: 6.5) {
                // State Indicator: Color + Symbol (not color alone)
                ZStack {
                    if style.isPulsing && !reduceMotion && store.fastFreshness == .fresh {
                        Circle()
                            .fill(style.tint.opacity(pulseActive ? 0.12 : 0.34))
                            .frame(width: 16, height: 16)
                    }
                    Image(systemName: style.symbol)
                        .font(.system(size: 9.5, weight: .bold))
                        .foregroundStyle(style.tint)
                }
                .frame(width: 16, height: 16)

                // Short State Badge
                Text(style.tag)
                    .font(.system(size: 9.5, weight: .bold))
                    .foregroundStyle(style.tint)
                    .padding(.horizontal, 5)
                    .padding(.vertical, 1.5)
                    .background(style.tint.opacity(colorScheme == .dark ? 0.18 : 0.14), in: Capsule())
                    .fixedSize()

                // Tool Dot + Name + Label
                HStack(spacing: 4.5) {
                    Circle()
                        .fill(Color(hex: task.color))
                        .frame(width: 5.5, height: 5.5)

                    Text(task.shortName)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.primary)
                        .fixedSize()

                    if !task.label.isEmpty {
                        Text(task.label)
                            .font(.system(size: 10.5))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
                .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)

                HStack(spacing: 3.5) {
                    Text(timerText)
                        .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                        .monospacedDigit()
                        .foregroundStyle(style.timerColor)

                    if isHovered {
                        Image(systemName: "arrow.up.forward")
                            .font(.system(size: 8.5, weight: .bold))
                            .foregroundStyle(style.tint.opacity(0.85))
                            .transition(.opacity)
                    }
                }
                .fixedSize()
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6)
            .background(
                style.backgroundColor.opacity(isHovered ? style.hoverBgAlpha : style.bgAlpha),
                in: RoundedRectangle(cornerRadius: 10, style: .continuous)
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10, style: .continuous)
                    .strokeBorder(style.tint.opacity(isHovered ? 0.55 : style.borderAlpha), lineWidth: isHovered ? 0.9 : 0.65)
            )
            .contentShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
        .buttonStyle(.plain)
        .pointingHandCursor()
        .onHover { hovering in
            hoveredSentinelId = hovering ? task.id : (hoveredSentinelId == task.id ? nil : hoveredSentinelId)
        }
        .help(sentinelTooltip(for: task, stateTag: style.tag, waitSubtitle: waitSubtitle))
        .accessibilityLabel(a11yLabel)
        .accessibilityHint("点击在访达中定位项目目录")
    }

    private struct SentinelVisualStyle {
        let tint: Color
        let timerColor: Color
        let backgroundColor: Color
        let bgAlpha: Double
        let hoverBgAlpha: Double
        let borderAlpha: Double
        let symbol: String
        let tag: String
        let isPulsing: Bool
    }

    private func sentinelVisualStyle(for state: SentinelTaskState) -> SentinelVisualStyle {
        switch state {
        case .waitingHuman:
            return SentinelVisualStyle(
                tint: amberAccent,
                timerColor: amberAccent,
                backgroundColor: amberAccent,
                bgAlpha: colorScheme == .dark ? 0.16 : 0.11,
                hoverBgAlpha: colorScheme == .dark ? 0.24 : 0.17,
                borderAlpha: 0.36,
                symbol: "hand.raised.fill",
                tag: "待拍板",
                isPulsing: false
            )
        case .running:
            return SentinelVisualStyle(
                tint: liveEmerald,
                timerColor: liveEmerald,
                backgroundColor: liveEmerald,
                bgAlpha: colorScheme == .dark ? 0.13 : 0.09,
                hoverBgAlpha: colorScheme == .dark ? 0.21 : 0.15,
                borderAlpha: 0.28,
                symbol: "bolt.fill",
                tag: "运行中",
                isPulsing: true
            )
        case .justFinished:
            return SentinelVisualStyle(
                tint: finishedMint,
                timerColor: finishedSlate,
                backgroundColor: finishedSlate,
                bgAlpha: colorScheme == .dark ? 0.14 : 0.08,
                hoverBgAlpha: colorScheme == .dark ? 0.22 : 0.14,
                borderAlpha: 0.24,
                symbol: "checkmark.circle.fill",
                tag: "刚收工",
                isPulsing: false
            )
        case .unknown:
            return SentinelVisualStyle(
                tint: finishedSlate,
                timerColor: finishedSlate,
                backgroundColor: finishedSlate,
                bgAlpha: 0.10,
                hoverBgAlpha: 0.16,
                borderAlpha: 0.20,
                symbol: "circle.dotted",
                tag: "状态未知",
                isPulsing: false
            )
        }
    }

    private func sentinelTimerText(for task: SentinelTask) -> String {
        switch task.state {
        case .running:
            if store.fastFreshness == .fresh, let snap = store.snapshot {
                let delta = max(0, min(ClockStore.fastStaleThresholdSeconds, store.nowEpoch - snap.generatedTs))
                return Formatters.liveTimer(task.elapsedSeconds + delta)
            }
            return Formatters.liveTimer(task.elapsedSeconds)
        case .waitingHuman, .justFinished, .unknown:
            // Frozen timer: never ticks for waiting_human or just_finished
            return Formatters.liveTimer(task.elapsedSeconds)
        }
    }

    private func waitKindHint(_ kind: SentinelWaitKind?) -> String? {
        guard let kind else { return nil }
        switch kind {
        case .approval: return "等待审批"
        case .userInput: return "等待输入"
        case .secret: return "等待凭据"
        case .other: return "等待确认"
        case .unknown: return nil
        }
    }

    private func sentinelTooltip(for task: SentinelTask, stateTag: String, waitSubtitle: String?) -> String {
        var parts = ["[\(stateTag)] \(task.displayName)"]
        if !task.label.isEmpty {
            parts.append(task.label)
        }
        if let hint = waitSubtitle {
            parts.append(hint)
        }
        if let dir = task.workspacePath ?? task.projectPath, !dir.isEmpty {
            parts.append("点击在访达中定位：\(dir)")
        } else {
            parts.append("点击尝试定位项目目录")
        }
        return parts.joined(separator: " · ")
    }

    private func inlineNoticeBanner(message: String, onDismiss: @escaping () -> Void) -> some View {
        HStack(spacing: 6) {
            Image(systemName: "exclamationmark.circle.fill")
                .font(.system(size: 10))
                .foregroundStyle(amberAccent)
            Text(message)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.primary)
                .lineLimit(2)
            Spacer(minLength: 4)
            Button(action: onDismiss) {
                Image(systemName: "xmark")
                    .font(.system(size: 8.5, weight: .bold))
                    .foregroundStyle(.secondary)
                    .frame(width: 16, height: 16)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("关闭提示")
        }
        .padding(.horizontal, 8.5)
        .padding(.vertical, 5)
        .background(amberAccent.opacity(colorScheme == .dark ? 0.14 : 0.11), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(amberAccent.opacity(0.30), lineWidth: 0.6))
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
                Text(store.isDisplayedDayWorkUnknown ? "暂未获取到工时观测" : "今日暂无 AI 工时记录")
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
                Text(store.isDisplayedDayWorkUnknown ? "暂未获取到工时观测" : "当天暂无 AI 工时记录")
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
        let allUnknown = !days.isEmpty && days.allSatisfy(\.isWorkUnknown)
        let maxHrs = max(1.0, days.filter { !$0.isWorkUnknown }.map(\.agentHours).max() ?? 1.0)
        let selectedKey = store.displayedDay?.date
        let currentToday = store.currentShanghaiDay

        return VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("近 7 天工时")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)

                Spacer()

                if allUnknown {
                    Text("—")
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(.tertiary)
                } else if let s = summary {
                    Text(String(format: "共 %.1fh · 日均 %.1fh", s.totalHours, s.avgDailyHours))
                        .font(.system(size: 10, weight: .medium))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                }
            }

            HStack(alignment: .bottom, spacing: 8) {
                ForEach(days) { d in
                    let isSelected = d.date == selectedKey
                    let isHovered = d.date == hoveredDay
                    let isActualToday = d.date == currentToday
                    let ratio = d.isWorkUnknown ? 0.06 : min(1.0, max(0.06, d.agentHours / maxHrs))
                    let barHeight: CGFloat = d.isWorkUnknown ? 5 : max(5, CGFloat(ratio) * 46)

                    Button {
                        withAnimation(reduceMotion ? nil : .easeOut(duration: 0.16)) {
                            store.selectDate(d.date)
                        }
                    } label: {
                        VStack(spacing: 4) {
                            Text(d.isWorkUnknown ? "—" : (d.agentHours >= 0.05 ? String(format: "%.1f", d.agentHours) : "0"))
                                .font(.system(size: 9.5, weight: isSelected ? .bold : .medium))
                                .monospacedDigit()
                                .foregroundStyle(isSelected ? .primary : .secondary)
                                .opacity(isSelected || isHovered || (!d.isWorkUnknown && d.agentHours == maxHrs) ? 1.0 : 0.55)

                            ZStack(alignment: .bottom) {
                                RoundedRectangle(cornerRadius: 5, style: .continuous)
                                    .fill(Color.primary.opacity(0.05))
                                    .frame(height: 46)

                                RoundedRectangle(cornerRadius: 5, style: .continuous)
                                    .fill(
                                        d.isWorkUnknown
                                            ? AnyShapeStyle(Color.primary.opacity(colorScheme == .dark ? 0.10 : 0.08))
                                            : barFill(isSelected: isSelected, isToday: isActualToday, isHovered: isHovered)
                                    )
                                    .frame(height: barHeight)
                            }

                            Text(Formatters.shortWeekday(d.weekday, isToday: isActualToday))
                                .font(.system(size: 10, weight: isSelected || isActualToday ? .semibold : .regular))
                                .foregroundStyle(isSelected ? accentPurple : .secondary)
                        }
                        .frame(maxWidth: .infinity)
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .pointingHandCursor()
                    .onHover { hovering in
                        hoveredDay = hovering ? d.date : (hoveredDay == d.date ? nil : hoveredDay)
                    }
                    .help(
                        d.isWorkUnknown
                            ? "\(d.date) (\(d.weekday)) · 暂未获取到工时观测"
                            : "\(d.date) (\(d.weekday)) · \(String(format: "%.2fh", d.agentHours)) · 并行 \(String(format: "%.2f×", d.leverage))"
                    )
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
            return AnyShapeStyle(accentPurple.opacity(0.55))
        }
        if isToday {
            return AnyShapeStyle(accentPurple.opacity(0.42))
        }
        return AnyShapeStyle(Color.primary.opacity(colorScheme == .dark ? 0.22 : 0.18))
    }

    // MARK: - Optional Fresh Quota Alert Strip (>= 70%)

    private func quotaAlertStrip(window: HighestQuotaWindow) -> some View {
        let tint = Formatters.quotaColor(for: window.usedPercent, defaultColor: amberAccent)
        let resetStr = Formatters.relativeReset(window.resetsAt, now: store.nowEpoch)

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
                        .lineLimit(1)
                    Spacer()
                    Text(resetStr.isEmpty ? "\(Int(round(window.usedPercent)))%" : "\(Int(round(window.usedPercent)))% · \(resetStr)")
                        .font(.system(size: 9.5, weight: .bold))
                        .monospacedDigit()
                        .foregroundStyle(tint)
                        .lineLimit(1)
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
        .pointingHandCursor()
        .help("点击查看订阅额度与配速详情")
    }

    // MARK: - Footer (Unified 5-State Cost, No "自有" Badge)

    private var footerRow: some View {
        let dayTok = store.displayedDayTokens
        let dayCost = store.displayedDayCostUsd
        let dayUnpriced = store.displayedDayUnpricedTokens
        let hasObs = store.displayedDayHasCostObservation
        let costLabel = Formatters.estimatedUSD(dayCost, unpricedTokens: dayUnpriced, hasObservation: hasObs)

        return HStack(spacing: 8) {
            Button {
                store.openPopover(tab: .overview)
            } label: {
                if hasObs {
                    HStack(spacing: 5) {
                        Text("\(Formatters.compactTokens(dayTok)) tok")
                            .font(.system(size: 10, weight: .semibold))
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                        Text("·")
                            .font(.system(size: 10, weight: .bold))
                            .foregroundStyle(.tertiary)
                        Text(costLabel)
                            .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                            .monospacedDigit()
                            .foregroundStyle(colorScheme == .dark ? Color(hex: "#E9D5FF") : accentPurple)
                        Image(systemName: "chevron.right")
                            .font(.system(size: 8, weight: .bold))
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 3.5)
                    .background(accentPurple.opacity(colorScheme == .dark ? 0.16 : 0.09), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    .overlay(
                        RoundedRectangle(cornerRadius: 7, style: .continuous)
                            .strokeBorder(accentPurple.opacity(0.26), lineWidth: 0.6)
                    )
                } else {
                    HStack(spacing: 4) {
                        Text("查看详单")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(.secondary)
                        Text(costLabel)
                            .font(.system(size: 10, weight: .medium, design: .monospaced))
                            .foregroundStyle(.tertiary)
                        Image(systemName: "chevron.right")
                            .font(.system(size: 8, weight: .semibold))
                            .foregroundStyle(.tertiary)
                    }
                    .padding(.horizontal, 7)
                    .padding(.vertical, 3.5)
                    .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                }
            }
            .buttonStyle(.plain)
            .pointingHandCursor()
            .help("点击打开菜单栏用量与明细面板")

            Spacer(minLength: 4)

            if store.copiedToast && store.reportError == nil {
                let copiedLabel: String = {
                    if store.isViewingToday { return "已复制今日日报" }
                    if let shortDate = store.displayedDay?.shortDate { return "已复制 \(shortDate) 日报" }
                    return "已复制日报"
                }()
                HStack(spacing: 3) {
                    Image(systemName: "checkmark")
                        .font(.system(size: 9.5, weight: .bold))
                    Text(copiedLabel)
                        .font(.system(size: 10, weight: .semibold))
                }
                .foregroundStyle(liveEmerald)
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
                .pointingHandCursor()
                .help("复制所选日期的 Markdown 日报（工时 + Token + 费用）")
                .accessibilityLabel("复制所选日期日报")
            }
        }
    }

    private var hairlineDivider: some View {
        Rectangle()
            .fill(Color.primary.opacity(colorScheme == .dark ? 0.10 : 0.08))
            .frame(height: 0.5)
    }
}
