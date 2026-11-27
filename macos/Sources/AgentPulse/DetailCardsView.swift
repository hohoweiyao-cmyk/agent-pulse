import AppKit
import SwiftUI

extension UsagePeriodKey {
    var copyReportButtonTitle: String {
        switch self {
        case .today: return "复制今日日报"
        case .yesterday: return "复制昨日日报"
        case .week: return "复制本周战报"
        case .lastWeek: return "复制上周战报"
        case .month: return "复制本月战报"
        case .year: return "复制今年战报"
        case .all: return "复制全部战报"
        }
    }

    var copiedReportToastTitle: String {
        switch self {
        case .today: return "已复制今日日报"
        case .yesterday: return "已复制昨日日报"
        case .week: return "已复制本周战报"
        case .lastWeek: return "已复制上周战报"
        case .month: return "已复制本月战报"
        case .year: return "已复制今年战报"
        case .all: return "已复制全部战报"
        }
    }
}

struct DetailCardsView: View {
    @ObservedObject var store: ClockStore
    @Environment(\.colorScheme) private var colorScheme
    @State private var openModelTools: Set<String> = []
    @State private var expandedModels: Set<String> = []

    private var accentPurple: Color { Color(hex: "#7C6BF5") }
    private var liveEmerald: Color {
        colorScheme == .dark ? Color(hex: "#34D399") : Color(hex: "#15803D")
    }
    private var amberAccent: Color {
        colorScheme == .dark ? Color(hex: "#FBBF24") : Color(hex: "#B45309")
    }
    private var cardSurface: Color {
        colorScheme == .dark ? Color.white.opacity(0.05) : Color.black.opacity(0.038)
    }
    private var innerSurface: Color {
        colorScheme == .dark ? Color.white.opacity(0.045) : Color.black.opacity(0.035)
    }
    private var cardBorder: Color {
        colorScheme == .dark ? Color.white.opacity(0.10) : Color.black.opacity(0.08)
    }

    var body: some View {
        let periodStats = store.periodStats(for: store.selectedPeriod)
        let tools = periodStats?.tools ?? []
        let useTwoColumns = tools.count >= 2
        let hourlyDay = store.hourlyDay(for: store.selectedPeriod)

        VStack(alignment: .leading, spacing: 10) {
            periodHeaderRow(periodStats: periodStats)
            compactSummaryStrip(periodStats: periodStats)

            if let day = hourlyDay {
                HourlyPulseChartView(
                    day: day,
                    compact: false,
                    titlePrefix: store.selectedPeriod == .today ? "今日 24h 昼夜脉搏" : "昨日 24h 昼夜脉搏"
                )
                .padding(.horizontal, 10)
                .padding(.vertical, 8)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
            }

            if let count = periodStats?.unpricedTokens, count > 0 {
                Text("含 \(Formatters.compactTokens(count)) 未匹配单价的 Token，未计入估算费用")
                    .font(.system(size: 10))
                    .foregroundStyle(amberAccent)
            }

            if store.snapshot == nil && store.isRefreshing {
                ProgressView("正在读取本地用量…")
                    .controlSize(.small)
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 28)
            } else if tools.isEmpty {
                let isUnobservedPeriod = periodStats == nil
                let unavailableHint = store.periodUnavailableHint(for: store.selectedPeriod)
                VStack(spacing: 6) {
                    Image(systemName: isUnobservedPeriod ? "clock.badge.exclamationmark" : "tray")
                        .font(.system(size: 18))
                        .foregroundStyle(.tertiary)
                    Text(isUnobservedPeriod ? "「\(store.selectedPeriod.title)」暂未获取到当期数据" : "「\(store.selectedPeriod.title)」暂无 AI 工具记录")
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(.secondary)
                    Text(
                        isUnobservedPeriod
                            ? (unavailableHint ?? "等待刷新获取最新记录，或切换到其他周期查看历史明细")
                            : (store.selectedPeriod == .all ? "使用 AI 工具产生对话或用量后会自动归集到这里" : "可切换到更长周期或「全部」查看历史明细")
                    )
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 12)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 30)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
            } else {
                ScrollView(.vertical, showsIndicators: true) {
                    if useTwoColumns {
                        LazyVGrid(
                            columns: [
                                GridItem(.flexible(), spacing: 10, alignment: .top),
                                GridItem(.flexible(), spacing: 10, alignment: .top)
                            ],
                            alignment: .leading,
                            spacing: 10
                        ) {
                            ForEach(tools) { tool in
                                toolDetailCard(tool)
                            }
                        }
                        .padding(.trailing, 2)
                    } else {
                        VStack(spacing: 10) {
                            ForEach(tools) { tool in
                                toolDetailCard(tool)
                            }
                        }
                    }
                }
                .frame(maxHeight: hourlyDay != nil ? 355 : 430)
            }

            if let repErr = store.reportError {
                reportErrorBanner(repErr)
            }

            bottomActionRow
        }
    }

    // MARK: - 7-Period Bar

    private func periodHeaderRow(periodStats: PeriodStats?) -> some View {
        HStack(spacing: 6) {
            HStack(spacing: 3) {
                ForEach(UsagePeriodKey.allCases) { key in
                    let isSel = store.selectedPeriod == key
                    Button {
                        withAnimation(.easeOut(duration: 0.14)) {
                            store.selectedPeriod = key
                        }
                    } label: {
                        Text(key.title)
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(isSel ? .primary : .secondary)
                            .padding(.horizontal, 7.5)
                            .padding(.vertical, 3.5)
                            .background(
                                isSel ? accentPurple.opacity(colorScheme == .dark ? 0.30 : 0.16) : Color.primary.opacity(0.05),
                                in: RoundedRectangle(cornerRadius: 6, style: .continuous)
                            )
                            .overlay(
                                RoundedRectangle(cornerRadius: 6, style: .continuous)
                                    .strokeBorder(isSel ? accentPurple.opacity(0.45) : Color.clear, lineWidth: 0.6)
                            )
                    }
                    .buttonStyle(.plain)
                    .pointingHandCursor()
                }
            }

            Spacer(minLength: 4)

            if let p = periodStats {
                Text(dateRangeLabel(p))
                    .font(.system(size: 10, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
            }
        }
    }

    private func dateRangeLabel(_ p: PeriodStats) -> String {
        switch store.selectedPeriod {
        case .today, .yesterday:
            return p.endDate
        case .week, .lastWeek:
            return "\(p.startDate.suffix(5)) ~ \(p.endDate.suffix(5))"
        case .month:
            return String(p.startDate.prefix(7))
        case .year:
            return "\(String(p.startDate.prefix(4))) 全年"
        case .all:
            return "\(p.startDate) 起"
        }
    }

    // MARK: - Top Compact Summary Strip

    private func compactSummaryStrip(periodStats: PeriodStats?) -> some View {
        let isWorkUnknown: Bool = {
            guard let p = periodStats else { return true }
            if p.isWorkUnknown { return true }
            if store.selectedPeriod == .today && store.isTodayWorkUnknown { return true }
            return false
        }()
        let hrs = store.selectedPeriod == .today
            ? (store.todayLiveSeconds / 3600.0)
            : (periodStats?.agentHours ?? 0.0)
        let wd = hrs / 8.0
        let lev = periodStats?.leverage ?? 1.0
        let hasPeriod = periodStats != nil
        let tok = periodStats?.totalTokens ?? 0
        let cost = periodStats?.costUsd ?? 0.0
        let unpriced = periodStats?.unpricedTokens ?? 0
        let hasCostObs = hasPeriod && (tok > 0 || cost > 0 || unpriced > 0)
        let costText = periodStats?.formattedEstimatedCost ?? "—"
        let hitPct = Int(round((periodStats?.cacheHitRate ?? 0.0) * 100))

        return HStack(spacing: 8) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("AI 有效工时")
                        .font(.system(size: 9.5, weight: .medium))
                        .foregroundStyle(.secondary)
                    HStack(alignment: .firstTextBaseline, spacing: 4) {
                        Text(isWorkUnknown ? "—" : String(format: "%.2fh", hrs))
                            .font(.system(size: 17, weight: .bold, design: .rounded))
                            .monospacedDigit()
                            .foregroundStyle(isWorkUnknown ? .secondary : .primary)
                        Text(isWorkUnknown ? "— 员工日 · —" : String(format: "%.2f 员工日 · %.2f×", wd, lev))
                            .font(.system(size: 10, weight: .semibold))
                            .monospacedDigit()
                            .foregroundStyle(isWorkUnknown ? .secondary : liveEmerald)
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity)
            .background(cardSurface, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))

            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 2) {
                    Text("合计 Token / 估算费用")
                        .font(.system(size: 9.5, weight: .medium))
                        .foregroundStyle(.secondary)
                    HStack(alignment: .firstTextBaseline, spacing: 5) {
                        Text(hasPeriod ? Formatters.compactTokens(tok) : "—")
                            .font(.system(size: 17, weight: .bold, design: .rounded))
                            .monospacedDigit()
                            .foregroundStyle(hasPeriod ? .primary : .secondary)
                        Text(costText)
                            .font(.system(size: 12, weight: .bold, design: .monospaced))
                            .foregroundStyle(hasCostObs ? accentPurple : Color.secondary.opacity(0.6))
                        if hasPeriod && hitPct > 0 {
                            Text("缓存 \(hitPct)%")
                                .font(.system(size: 9.5, weight: .medium))
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 7)
            .frame(maxWidth: .infinity)
            .background(cardSurface, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
        }
    }

    // MARK: - Tokei-Grade Individual Tool Card

    private func toolDetailCard(_ tool: UsageToolStats) -> some View {
        let tint = Color(hex: tool.color)
        let isModelsOpen = openModelTools.contains(tool.id)
        let hitPct = tool.effectiveCacheHitRate * 100.0
        let hasCostObs = tool.totalTokens > 0 || tool.costUsd > 0 || (tool.unpricedTokens ?? 0) > 0
        let freshQuota = freshInlineQuota(for: tool.tool)

        return VStack(alignment: .leading, spacing: 9) {
            // Card Header
            HStack(spacing: 6) {
                Circle()
                    .fill(tint)
                    .frame(width: 7.5, height: 7.5)
                Text(tool.displayName)
                    .font(.system(size: 12.5, weight: .bold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)

                Spacer(minLength: 4)

                if tool.agentSeconds > 0 {
                    Text(Formatters.shortDuration(tool.agentSeconds))
                        .font(.system(size: 9.5, weight: .semibold))
                        .monospacedDigit()
                        .foregroundStyle(liveEmerald)
                        .padding(.horizontal, 5.5)
                        .padding(.vertical, 2)
                        .background(liveEmerald.opacity(0.14), in: Capsule())
                }

                if tool.effectiveSessions > 0 {
                    Text("\(tool.effectiveSessions) 会话")
                        .font(.system(size: 9.5, weight: .medium))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                        .padding(.horizontal, 5.5)
                        .padding(.vertical, 2)
                        .background(Color.primary.opacity(0.06), in: Capsule())
                }

                Button {
                    store.copyToolSummary(tool, periodTitle: store.selectedPeriod.title)
                } label: {
                    Image(systemName: store.copiedToolId == tool.id ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 9.5, weight: .semibold))
                        .foregroundStyle(store.copiedToolId == tool.id ? liveEmerald : .secondary)
                        .frame(width: 20, height: 20)
                        .background(Color.primary.opacity(0.05), in: RoundedRectangle(cornerRadius: 5, style: .continuous))
                }
                .buttonStyle(.plain)
                .pointingHandCursor()
                .help("复制该工具明细")
            }

            // Hero Headline (Total Tokens + Unified Estimated API Cost)
            HStack(alignment: .lastTextBaseline, spacing: 6) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(tool.totalTokens > 0 ? Formatters.compactTokens(tool.totalTokens) : Formatters.shortDuration(tool.agentSeconds))
                        .font(.system(size: 23, weight: .bold, design: .rounded))
                        .monospacedDigit()
                        .foregroundStyle(tint)
                    Text(tool.totalTokens > 0 ? "\(store.selectedPeriod.title)总 Token" : "\(store.selectedPeriod.title) AI 工时")
                        .font(.system(size: 9.5, weight: .medium))
                        .foregroundStyle(.secondary)
                }

                Spacer(minLength: 4)

                VStack(alignment: .trailing, spacing: 1) {
                    Text(tool.formattedEstimatedCost)
                        .font(.system(size: 16, weight: .bold, design: .monospaced))
                        .monospacedDigit()
                        .foregroundStyle(hasCostObs ? Color.primary : Color.secondary.opacity(0.6))
                    Text(hasCostObs ? "按 API 价估算" : (tool.agentSeconds > 0 ? "仅统计工时" : "暂无记录"))
                        .font(.system(size: 9.5))
                        .foregroundStyle(.tertiary)
                }
            }

            // 2xN Detailed Token & Work-Hours Metric Grid
            if tool.totalTokens > 0 {
                tokenMetricGrid(tool: tool, hitPct: hitPct, tint: tint)
            }

            // Inline Fresh Subscription Quota Bars (only fresh windows)
            if let (qProvider, freshWindows) = freshQuota {
                inlineQuotaSection(provider: qProvider, windows: freshWindows)
            }

            // 2-Level Model Disclosure
            if !tool.models.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Button {
                        withAnimation(.easeInOut(duration: 0.16)) {
                            if isModelsOpen {
                                openModelTools.remove(tool.id)
                            } else {
                                openModelTools.insert(tool.id)
                            }
                        }
                    } label: {
                        HStack(spacing: 5) {
                            Image(systemName: "chart.pie.fill")
                                .font(.system(size: 9))
                                .foregroundStyle(tint)
                            Text("按模型 (\(tool.models.count))")
                                .font(.system(size: 10.5, weight: .semibold))
                                .foregroundStyle(.secondary)
                            Image(systemName: isModelsOpen ? "chevron.down" : "chevron.right")
                                .font(.system(size: 8, weight: .bold))
                                .foregroundStyle(.tertiary)
                            Spacer()
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .pointingHandCursor()

                    if isModelsOpen {
                        VStack(alignment: .leading, spacing: 5) {
                            ForEach(tool.models) { m in
                                modelExpandableRow(toolId: tool.id, model: m, tint: tint)
                            }
                        }
                        .padding(8)
                        .background(innerSurface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                    }
                }
            }
        }
        .padding(12)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(tint.opacity(colorScheme == .dark ? 0.24 : 0.20), lineWidth: 0.7)
        )
    }

    private func tokenMetricGrid(tool: UsageToolStats, hitPct: Double, tint: Color) -> some View {
        let hasCW = tool.cacheWriteTokens > 0
        let hasReason = tool.reasoningTokens > 0

        return LazyVGrid(
            columns: [
                GridItem(.flexible(), spacing: 8),
                GridItem(.flexible(), spacing: 8)
            ],
            alignment: .leading,
            spacing: 6
        ) {
            HStack(spacing: 5) {
                Text("缓存命中")
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                Spacer(minLength: 2)
                ZStack {
                    Circle()
                        .stroke(tint.opacity(0.20), lineWidth: 2.0)
                    Circle()
                        .trim(from: 0, to: CGFloat(min(1.0, max(0.0, hitPct / 100.0))))
                        .stroke(tint, style: StrokeStyle(lineWidth: 2.0, lineCap: .round))
                        .rotationEffect(.degrees(-90))
                }
                .frame(width: 12, height: 12)
                Text("\(Int(round(hitPct)))%")
                    .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(tint)
            }

            metricCell(
                icon: tool.agentSeconds > 0 ? "bolt.fill" : "calendar",
                label: tool.agentSeconds > 0 ? "有效工时" : "活跃天数",
                value: tool.agentSeconds > 0 ? Formatters.shortDuration(tool.agentSeconds) : "\(tool.activeDays) 天",
                tint: tool.agentSeconds > 0 ? liveEmerald : .secondary
            )

            metricCell(icon: "arrow.down", label: "输入", value: Formatters.compactTokens(tool.inputTokens), tint: tint)
            metricCell(icon: "arrow.up", label: "输出", value: Formatters.compactTokens(tool.outputTokens), tint: tint)
            metricCell(icon: "bolt.circle", label: "缓存读", value: Formatters.compactTokens(tool.cacheReadTokens), tint: tint)

            if hasCW {
                metricCell(icon: "square.stack.3d.up", label: "缓存写", value: Formatters.compactTokens(tool.cacheWriteTokens), tint: tint)
            }
            if hasReason {
                metricCell(icon: "brain.head.profile", label: "推理", value: Formatters.compactTokens(tool.reasoningTokens), tint: tint)
            }
            if !hasCW && !hasReason {
                metricCell(icon: "number", label: "占比", value: "\(Int(round(tool.shareTokens * 100)))%", tint: .secondary)
            }
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 8)
        .background(innerSurface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
    }

    private func metricCell(icon: String, label: String, value: String, tint: Color) -> some View {
        HStack(spacing: 4) {
            Image(systemName: icon)
                .font(.system(size: 8.5, weight: .semibold))
                .foregroundStyle(tint.opacity(0.85))
                .frame(width: 11)
            Text(label)
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Spacer(minLength: 2)
            Text(value)
                .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                .monospacedDigit()
                .foregroundStyle(.primary)
                .lineLimit(1)
        }
    }

    private func freshInlineQuota(for toolKey: String) -> (QuotaProvider, [QuotaWindow])? {
        guard let providers = store.quotaSnapshot?.providers else { return nil }
        let matched: QuotaProvider?
        if toolKey == "codex" || toolKey == "t3_codex" {
            matched = providers.first { $0.id == "codex" }
        } else if toolKey == "claude_code" || toolKey == "t3_claude" {
            matched = providers.first { $0.id == "claude" }
        } else {
            matched = nil
        }
        guard let provider = matched else { return nil }
        let freshWindows = provider.windows.filter { store.quotaWindowIsFresh(provider: provider, window: $0) }
        guard !freshWindows.isEmpty else { return nil }
        return (provider, freshWindows)
    }

    private func inlineQuotaSection(provider: QuotaProvider, windows: [QuotaWindow]) -> some View {
        Button {
            withAnimation(.easeOut(duration: 0.14)) {
                store.selectedPopoverTab = .quotas
            }
        } label: {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(windows.prefix(2)) { w in
                    let barColor = Formatters.quotaColor(for: w.effectiveThresholdPercent, defaultColor: Color(hex: provider.color))
                    let resetStr = Formatters.relativeReset(w.resetsAt, now: store.nowEpoch)
                    VStack(alignment: .leading, spacing: 2.5) {
                        HStack {
                            Text("\(provider.name) \(w.label)")
                                .font(.system(size: 9.5, weight: .medium))
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text(resetStr.isEmpty ? "\(Int(round(w.usedPercent)))%" : "\(Int(round(w.usedPercent)))% · \(resetStr)")
                                .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                                .foregroundStyle(barColor)
                        }
                        GeometryReader { geo in
                            ZStack(alignment: .leading) {
                                Capsule().fill(Color.primary.opacity(0.08))
                                Capsule()
                                    .fill(barColor)
                                    .frame(width: max(4, geo.size.width * CGFloat(min(100, max(0, w.usedPercent)) / 100.0)))
                            }
                        }
                        .frame(height: 3.5)
                    }
                }
            }
            .padding(.top, 2)
        }
        .buttonStyle(.plain)
        .pointingHandCursor()
        .help("点击查看全部订阅额度与燃尽配速")
    }

    private func modelExpandableRow(toolId: String, model: ModelUsageStats, tint: Color) -> some View {
        let key = "\(toolId)::\(model.id)"
        let isExpanded = expandedModels.contains(key)
        let hitPct = Int(round(model.effectiveCacheHitRate * 100))

        return VStack(alignment: .leading, spacing: 4) {
            Button {
                withAnimation(.easeInOut(duration: 0.15)) {
                    if isExpanded {
                        expandedModels.remove(key)
                    } else {
                        expandedModels.insert(key)
                    }
                }
            } label: {
                HStack(spacing: 5) {
                    Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                        .font(.system(size: 7, weight: .bold))
                        .foregroundStyle(.tertiary)
                        .frame(width: 8)
                    Circle()
                        .fill(tint.opacity(0.75))
                        .frame(width: 4.5, height: 4.5)
                    Text(model.model)
                        .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    Spacer(minLength: 4)
                    Text(Formatters.compactTokens(model.totalTokens))
                        .font(.system(size: 9.5, design: .monospaced))
                        .foregroundStyle(.secondary)
                    if hitPct > 0 {
                        Text("\(hitPct)%")
                            .font(.system(size: 9, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 4)
                            .padding(.vertical, 1)
                            .background(Color.primary.opacity(0.07), in: Capsule())
                    }
                    Text(model.formattedEstimatedCost)
                        .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                        .foregroundStyle(.primary)
                        .frame(minWidth: 38, alignment: .trailing)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .pointingHandCursor()

            if isExpanded {
                VStack(alignment: .leading, spacing: 4) {
                    LazyVGrid(
                        columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)],
                        alignment: .leading,
                        spacing: 3
                    ) {
                        miniKV("输入", Formatters.compactTokens(model.inputTokens))
                        miniKV("输出", Formatters.compactTokens(model.outputTokens))
                        miniKV("缓存读", Formatters.compactTokens(model.cacheReadTokens))
                        if model.cacheWriteTokens > 0 {
                            miniKV("缓存写", Formatters.compactTokens(model.cacheWriteTokens))
                        }
                        if model.reasoningTokens > 0 {
                            miniKV("推理", Formatters.compactTokens(model.reasoningTokens))
                        }
                    }

                    if let priceText = priceLine(for: model) {
                        Text(priceText)
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .padding(.top, 2)
                    }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .background(Color.primary.opacity(colorScheme == .dark ? 0.05 : 0.03), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                .overlay(alignment: .leading) {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(tint.opacity(0.75))
                        .frame(width: 2)
                }
                .padding(.leading, 8)
            }
        }
    }

    private func priceLine(for model: ModelUsageStats) -> String? {
        guard let rIn = model.rateIn, let rOut = model.rateOut else { return nil }
        var parts = ["In \(Formatters.rate(rIn))", "Out \(Formatters.rate(rOut))"]
        if let rCR = model.rateCacheRead, rCR > 0 {
            parts.append("CR \(Formatters.rate(rCR))")
        }
        if let rCW = model.rateCacheWrite, rCW > 0 {
            parts.append("CW \(Formatters.rate(rCW))")
        }
        return "当前参考单价 ($/1M) " + parts.joined(separator: " · ")
    }

    private func miniKV(_ k: String, _ v: String) -> some View {
        HStack {
            Text(k)
                .font(.system(size: 9.5))
                .foregroundStyle(.secondary)
            Spacer()
            Text(v)
                .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                .foregroundStyle(.primary)
        }
    }

    private func reportErrorBanner(_ message: String) -> some View {
        let bannerMessage = message.contains("剪贴板") ? message : "复制失败（剪贴板未改动）：\(message)"
        return HStack(spacing: 6) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 10))
                .foregroundStyle(amberAccent)
            Text(bannerMessage)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.primary)
                .lineLimit(2)
            Spacer(minLength: 4)
            Button {
                store.reportError = nil
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 8.5, weight: .bold))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel("关闭错误提示")
        }
        .padding(.horizontal, 9)
        .padding(.vertical, 5.5)
        .background(amberAccent.opacity(0.12), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(amberAccent.opacity(0.28), lineWidth: 0.6))
    }

    // MARK: - Bottom Action Bar

    private var bottomActionRow: some View {
        let isCopied = store.copiedToast && store.reportError == nil

        return HStack(spacing: 6) {
            Button {
                store.copyMarkdownReport(period: store.selectedPeriod)
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: isCopied ? "checkmark" : "doc.on.doc")
                        .font(.system(size: 10, weight: .semibold))
                    Text(isCopied ? store.selectedPeriod.copiedReportToastTitle : store.selectedPeriod.copyReportButtonTitle)
                        .font(.system(size: 10.5, weight: .semibold))
                }
                .foregroundStyle(isCopied ? liveEmerald : (colorScheme == .dark ? Color(hex: "#EDE9FE") : accentPurple))
                .padding(.horizontal, 10)
                .padding(.vertical, 5)
                .background(
                    (isCopied ? liveEmerald : accentPurple).opacity(0.18),
                    in: RoundedRectangle(cornerRadius: 7, style: .continuous)
                )
            }
            .buttonStyle(.plain)
            .pointingHandCursor()

            Text("成本按 API 价估算，非订阅实付")
                .font(.system(size: 9))
                .foregroundStyle(.tertiary)
                .lineLimit(1)

            Spacer()

            Button {
                store.isCardVisible.toggle()
            } label: {
                Text(store.isCardVisible ? "隐藏挂件" : "显示挂件")
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(.primary)
                    .padding(.horizontal, 9)
                    .padding(.vertical, 5)
                    .background(cardSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
            }
            .buttonStyle(.plain)
            .pointingHandCursor()
            .help("显示或隐藏桌面悬浮挂件")

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
            .pointingHandCursor()
            .help("当前：\(store.windowLayer.title)（点击切换桌面层 / 悬浮置顶）")
        }
    }
}
