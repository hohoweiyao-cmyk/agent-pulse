import SwiftUI

struct DashboardViews: View {
    @ObservedObject var store: ClockStore
    @Environment(\.colorScheme) private var colorScheme
    @State private var selectedHeatmapDate: String?

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
    private var cardBorder: Color {
        colorScheme == .dark ? Color.white.opacity(0.10) : Color.black.opacity(0.08)
    }

    var body: some View {
        let periodStats = store.periodStats(for: store.selectedPeriod)
        let heatmap = store.snapshot?.usage?.heatmap
        let isCopied = store.copiedToast && store.reportError == nil

        ScrollView(.vertical, showsIndicators: true) {
            VStack(alignment: .leading, spacing: 10) {
                // Period Picker + Streak Badge
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
                            }
                            .buttonStyle(.plain)
                            .pointingHandCursor()
                        }
                    }

                    Spacer()

                    if let hm = heatmap {
                        HStack(spacing: 4) {
                            Image(systemName: "flame.fill")
                                .font(.system(size: 9.5))
                                .foregroundStyle(Color(hex: "#FB923C"))
                            Text("连续 \(hm.currentStreak) 天 · 最长 \(hm.longestStreak) 天")
                                .font(.system(size: 10, weight: .semibold))
                                .monospacedDigit()
                                .foregroundStyle(.secondary)
                        }
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3.5)
                        .background(cardSurface, in: Capsule())
                    }
                }

                // 4 KPI Cards
                kpiRow(periodStats: periodStats, heatmap: heatmap)

                // 26-Week Interactive Heatmap + Selected Day Detail
                if let hm = heatmap {
                    heatmapCard(hm)
                }

                // Cross-Tool Top Models Leaderboard
                if let topModels = periodStats?.topModels, !topModels.isEmpty {
                    modelLeaderboardCard(topModels)
                }

                if let repErr = store.reportError {
                    let bannerMessage = repErr.contains("剪贴板") ? repErr : "复制失败（剪贴板未改动）：\(repErr)"
                    HStack(spacing: 6) {
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

                // Bottom Ledger Utilities
                HStack {
                    Button {
                        store.openDataDirectory()
                    } label: {
                        Text("打开数据目录")
                            .font(.system(size: 10.5, weight: .medium))
                            .foregroundStyle(.primary)
                            .padding(.horizontal, 9)
                            .padding(.vertical, 5)
                            .background(cardSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                            .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
                    }
                    .buttonStyle(.plain)
                    .pointingHandCursor()
                    .help("在访达中打开 ~/.agent-pulse")

                    if let start = store.snapshot?.usage?.ledgerMeta.workHistoryFrom {
                        Text("工时记录自 \(start) 起")
                            .font(.system(size: 9.5))
                            .foregroundStyle(.tertiary)
                    }

                    Spacer()

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
                        .background((isCopied ? liveEmerald : accentPurple).opacity(0.20), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .pointingHandCursor()
                }
            }
            .padding(.trailing, 2)
        }
        .frame(maxHeight: 490)
    }

    private func kpiRow(periodStats: PeriodStats?, heatmap: HeatmapPayload?) -> some View {
        let hasPeriod = periodStats != nil
        let isWorkUnknown: Bool = {
            guard let p = periodStats else { return true }
            if p.isWorkUnknown { return true }
            if store.selectedPeriod == .today && store.isTodayWorkUnknown { return true }
            return false
        }()
        let hrs = store.selectedPeriod == .today
            ? (store.todayLiveSeconds / 3600.0)
            : (periodStats?.agentHours ?? 0.0)
        let tok = periodStats?.totalTokens ?? 0
        let cost = periodStats?.costUsd ?? 0.0
        let unpriced = periodStats?.unpricedTokens ?? 0
        let hasCostObs = hasPeriod && (tok > 0 || cost > 0 || unpriced > 0)
        let hitPct = Int(round((periodStats?.cacheHitRate ?? 0.0) * 100))
        let activeDays = periodStats?.activeDays ?? 0

        return HStack(spacing: 8) {
            kpiBox(
                title: "AI 有效工时",
                value: isWorkUnknown ? "—" : String(format: "%.2fh", hrs),
                sub: isWorkUnknown ? "—" : String(format: "%.2f 员工日", hrs / 8.0),
                tint: isWorkUnknown ? .secondary : liveEmerald
            )
            kpiBox(
                title: "总 Token",
                value: hasPeriod ? Formatters.compactTokens(tok) : "—",
                sub: hasPeriod ? "缓存命中 \(hitPct)%" : "暂未获取当期数据",
                tint: hasPeriod ? Color(hex: "#38BDF8") : .secondary
            )
            kpiBox(
                title: "估算 API 成本",
                value: Formatters.estimatedUSD(cost, unpricedTokens: unpriced, hasObservation: hasCostObs),
                sub: hasPeriod
                    ? (unpriced > 0 ? "\(Formatters.compactTokens(unpriced)) Token 未计价" : "\(periodStats?.tools.count ?? 0) 个工具")
                    : "暂未获取当期数据",
                tint: hasCostObs ? accentPurple : .secondary
            )
            kpiBox(
                title: "活跃天数",
                value: hasPeriod ? "\(activeDays) 天" : "—",
                sub: heatmap.map { "累计 \($0.totalActiveDays) 天" } ?? (hasPeriod ? "累计 \(activeDays) 天" : "—"),
                tint: hasPeriod ? Color(hex: "#FB923C") : .secondary
            )
        }
    }

    private func kpiBox(title: String, value: String, sub: String, tint: Color) -> some View {
        VStack(alignment: .leading, spacing: 2.5) {
            Text(title)
                .font(.system(size: 9.5, weight: .medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
            Text(value)
                .font(.system(size: 17, weight: .bold, design: .rounded))
                .monospacedDigit()
                .foregroundStyle(tint)
                .lineLimit(1)
            Text(sub)
                .font(.system(size: 9.5))
                .monospacedDigit()
                .foregroundStyle(.tertiary)
                .lineLimit(1)
        }
        .padding(9)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
    }

    // MARK: - 26-Week Heatmap

    private func heatmapCard(_ hm: HeatmapPayload) -> some View {
        let days = hm.days
        let metric = store.selectedHistoryMetric
        let values: [Double] = days.map { metricValue($0, metric: metric) }
        let maxVal = max(0.01, values.max() ?? 1.0)
        let selectedDay: HeatmapDay? = {
            if let key = selectedHeatmapDate, let found = days.first(where: { $0.date == key }) {
                return found
            }
            return days.last
        }()

        let weeksCount = max(1, (days.count + 6) / 7)

        return VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("近 26 周活跃热力图")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.primary)
                Spacer()
                HStack(spacing: 3) {
                    ForEach(HistoryMetric.allCases) { m in
                        let isSel = store.selectedHistoryMetric == m
                        Button {
                            store.selectedHistoryMetric = m
                        } label: {
                            Text(m.title)
                                .font(.system(size: 9.5, weight: .semibold))
                                .foregroundStyle(isSel ? .primary : .secondary)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 2.5)
                                .background(
                                    isSel ? accentPurple.opacity(0.24) : Color.primary.opacity(0.05),
                                    in: RoundedRectangle(cornerRadius: 5, style: .continuous)
                                )
                        }
                        .buttonStyle(.plain)
                        .pointingHandCursor()
                    }
                }
            }

            HStack(spacing: 3.5) {
                ForEach(0..<weeksCount, id: \.self) { col in
                    VStack(spacing: 3.5) {
                        ForEach(0..<7, id: \.self) { row in
                            let idx = col * 7 + row
                            if idx < days.count {
                                let d = days[idx]
                                let val = values[idx]
                                let isSel = selectedDay?.date == d.date
                                let dayCostText = Formatters.estimatedUSD(
                                    d.costUsd,
                                    unpricedTokens: d.effectiveUnpricedTokens,
                                    hasObservation: d.effectiveHasCostObservation
                                )
                                Button {
                                    selectedHeatmapDate = d.date
                                } label: {
                                    RoundedRectangle(cornerRadius: 3, style: .continuous)
                                        .fill(heatColor(value: val, maxVal: maxVal, metric: metric))
                                        .frame(height: 14)
                                        .overlay(
                                            RoundedRectangle(cornerRadius: 3, style: .continuous)
                                                .strokeBorder(isSel ? Color.primary : Color.clear, lineWidth: 1.2)
                                        )
                                }
                                .buttonStyle(.plain)
                                .pointingHandCursor()
                                .help("\(d.date) (\(d.weekday)) · \(dayCostText) · \(Formatters.compactTokens(d.totalTokens)) tok · \(String(format: "%.2fh", d.agentHours))")
                            } else {
                                Color.clear.frame(height: 14)
                            }
                        }
                    }
                    .frame(maxWidth: .infinity)
                }
            }

            // Selected Day Detail Card
            if let d = selectedDay {
                let hasDayObs = d.effectiveHasCostObservation
                let dayCostText = Formatters.estimatedUSD(
                    d.costUsd,
                    unpricedTokens: d.effectiveUnpricedTokens,
                    hasObservation: hasDayObs
                )

                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        Text("\(d.date) \(d.weekday)")
                            .font(.system(size: 11, weight: .bold))
                            .foregroundStyle(.primary)
                        Spacer()
                        Text(String(format: "工时 %.2fh", d.agentHours))
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(liveEmerald)
                        Text("·")
                            .foregroundStyle(.tertiary)
                        Text("\(Formatters.compactTokens(d.totalTokens)) tok")
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(.primary)
                        Text("·")
                            .foregroundStyle(.tertiary)
                        Text(dayCostText)
                            .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                            .foregroundStyle(hasDayObs ? accentPurple : Color.secondary.opacity(0.6))
                    }

                    if !d.tools.isEmpty {
                        LazyVGrid(
                            columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)],
                            alignment: .leading,
                            spacing: 4
                        ) {
                            ForEach(d.tools) { dt in
                                let dtHasObs = dt.effectiveHasCostObservation
                                let dtCost = Formatters.estimatedUSD(
                                    dt.costUsd,
                                    unpricedTokens: dt.effectiveUnpricedTokens,
                                    hasObservation: dtHasObs
                                )
                                let dtMetricText: String = {
                                    if dt.totalTokens > 0 {
                                        return "\(Formatters.compactTokens(dt.totalTokens)) · \(dtCost)"
                                    }
                                    if dtHasObs {
                                        return dtCost
                                    }
                                    return String(format: "%.2fh", dt.agentHours)
                                }()
                                HStack(spacing: 5) {
                                    Circle()
                                        .fill(Color(hex: dt.color))
                                        .frame(width: 5.5, height: 5.5)
                                    Text(dt.displayName)
                                        .font(.system(size: 10, weight: .medium))
                                        .lineLimit(1)
                                    Spacer()
                                    Text(dtMetricText)
                                        .font(.system(size: 9.5, design: .monospaced))
                                        .foregroundStyle(.secondary)
                                }
                            }
                        }
                    } else {
                        Text("当天无活跃记录")
                            .font(.system(size: 10))
                            .foregroundStyle(.tertiary)
                    }
                }
                .padding(9)
                .background(Color.primary.opacity(colorScheme == .dark ? 0.045 : 0.03), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            }
        }
        .padding(11)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
    }

    private func metricValue(_ d: HeatmapDay, metric: HistoryMetric) -> Double {
        switch metric {
        case .hours: return d.agentHours
        case .tokens: return Double(d.totalTokens)
        case .cost: return d.costUsd
        }
    }

    private func heatColor(value: Double, maxVal: Double, metric: HistoryMetric) -> Color {
        if value <= 0 {
            return Color.primary.opacity(colorScheme == .dark ? 0.06 : 0.05)
        }
        let ratio = min(1.0, max(0.12, pow(value / maxVal, 0.55)))
        let base: Color = {
            switch metric {
            case .hours: return liveEmerald
            case .tokens: return Color(hex: "#38BDF8")
            case .cost: return accentPurple
            }
        }()
        return base.opacity(0.18 + ratio * 0.82)
    }

    // MARK: - Cross-Tool Top Models Leaderboard

    private func modelLeaderboardCard(_ models: [RankedModelStats]) -> some View {
        let maxCost = max(0.01, models.map(\.costUsd).max() ?? 1.0)
        let maxTok = max(1, models.map(\.totalTokens).max() ?? 1)

        return VStack(alignment: .leading, spacing: 7) {
            HStack {
                Text("模型消耗排行 · \(store.selectedPeriod.title)")
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.primary)
                Spacer()
                Text("Top \(min(models.count, 8))")
                    .font(.system(size: 9.5, weight: .medium))
                    .foregroundStyle(.tertiary)
            }

            VStack(spacing: 6) {
                ForEach(models.prefix(8)) { m in
                    let ratio = m.costUsd > 0
                        ? min(1.0, max(0.05, m.costUsd / maxCost))
                        : min(1.0, max(0.05, Double(m.totalTokens) / Double(maxTok)))
                    let tint = Color(hex: m.color)
                    let hitPct = Int(round(m.cacheHitRate * 100))
                    let hasObs = m.effectiveHasCostObservation

                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Circle()
                                .fill(tint)
                                .frame(width: 6, height: 6)
                            Text(m.model)
                                .font(.system(size: 10.5, weight: .semibold, design: .monospaced))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Text(m.shortName)
                                .font(.system(size: 9, weight: .medium))
                                .foregroundStyle(tint)
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1)
                                .background(tint.opacity(0.14), in: Capsule())
                            Spacer(minLength: 4)
                            Text(Formatters.compactTokens(m.totalTokens))
                                .font(.system(size: 9.5, design: .monospaced))
                                .foregroundStyle(.secondary)
                            if hitPct > 0 {
                                Text("\(hitPct)%")
                                    .font(.system(size: 9, design: .monospaced))
                                    .foregroundStyle(.tertiary)
                            }
                            Text(
                                Formatters.estimatedUSD(
                                    m.costUsd,
                                    unpricedTokens: m.effectiveUnpricedTokens,
                                    hasObservation: hasObs
                                )
                            )
                                .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                                .foregroundStyle(.primary)
                                .frame(minWidth: 44, alignment: .trailing)
                        }
                        GeometryReader { geo in
                            ZStack(alignment: .leading) {
                                Capsule().fill(Color.primary.opacity(0.07))
                                Capsule()
                                    .fill(tint.opacity(0.85))
                                    .frame(width: max(6, geo.size.width * CGFloat(ratio)))
                            }
                        }
                        .frame(height: 4.5)
                    }
                }
            }
        }
        .padding(11)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 11, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 11, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
    }
}
