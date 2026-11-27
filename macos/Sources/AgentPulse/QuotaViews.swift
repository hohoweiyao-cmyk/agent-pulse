import SwiftUI

struct QuotaViews: View {
    @ObservedObject var store: ClockStore
    @Environment(\.colorScheme) private var colorScheme

    private var accentPurple: Color { Color(hex: "#7C6BF5") }
    private var liveEmerald: Color {
        colorScheme == .dark ? Color(hex: "#34D399") : Color(hex: "#15803D")
    }
    private var amberAccent: Color {
        colorScheme == .dark ? Color(hex: "#FBBF24") : Color(hex: "#B45309")
    }
    private var riskCoral: Color {
        colorScheme == .dark ? Color(hex: "#F87171") : Color(hex: "#DC2626")
    }
    private var mutedSlate: Color {
        colorScheme == .dark ? Color(hex: "#94A3B8") : Color(hex: "#64748B")
    }
    private var cardSurface: Color {
        colorScheme == .dark ? Color.white.opacity(0.055) : Color.black.opacity(0.038)
    }
    private var innerSurface: Color {
        colorScheme == .dark ? Color.white.opacity(0.045) : Color.black.opacity(0.03)
    }
    private var cardBorder: Color {
        colorScheme == .dark ? Color.white.opacity(0.10) : Color.black.opacity(0.08)
    }

    private struct ProviderPartition {
        let freshProviders: [(provider: QuotaProvider, windows: [QuotaWindow])]
        let staleProviders: [(provider: QuotaProvider, windows: [QuotaWindow], isErroredOrUnavailable: Bool)]
    }

    private func partitionProviders(_ providers: [QuotaProvider]) -> ProviderPartition {
        var freshList: [(QuotaProvider, [QuotaWindow])] = []
        var staleList: [(QuotaProvider, [QuotaWindow], Bool)] = []

        for p in providers {
            let freshWins = p.windows.filter { store.quotaWindowIsFresh(provider: p, window: $0) }
            let staleWins = p.windows.filter { !store.quotaWindowIsFresh(provider: p, window: $0) }

            if !freshWins.isEmpty {
                freshList.append((p, freshWins))
            }
            if !staleWins.isEmpty || (freshWins.isEmpty && (p.status != "ok" || p.windows.isEmpty || store.quotaError != nil)) {
                let isErr = (p.status != "ok" || store.quotaError != nil)
                staleList.append((p, staleWins, isErr))
            }
        }
        return ProviderPartition(freshProviders: freshList, staleProviders: staleList)
    }

    var body: some View {
        let providers = store.quotaSnapshot?.providers ?? []
        let partitioned = partitionProviders(providers)

        VStack(alignment: .leading, spacing: 9) {
            if let error = store.quotaError {
                let cleanErr = error.hasSuffix("。") ? String(error.dropLast()) : error
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.system(size: 10))
                        .foregroundStyle(amberAccent)
                    Text(providers.isEmpty ? error : "\(cleanErr)，下方仅展示上次记录。")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(amberAccent)
                        .lineLimit(2)
                    Spacer(minLength: 2)
                }
                .padding(.horizontal, 9)
                .padding(.vertical, 6)
                .background(amberAccent.opacity(0.12), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(amberAccent.opacity(0.28), lineWidth: 0.6))
            }

            if providers.isEmpty && store.quotaError == nil {
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("正在读取本地与订阅额度…")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                }
                .padding(.vertical, 28)
                .frame(maxWidth: .infinity)
            } else if !providers.isEmpty {
                ScrollView(.vertical, showsIndicators: true) {
                    VStack(alignment: .leading, spacing: 10) {
                        // Section 1: Fresh Live Quota Readings
                        if !partitioned.freshProviders.isEmpty {
                            VStack(alignment: .leading, spacing: 6) {
                                HStack(spacing: 5) {
                                    Circle()
                                        .fill(liveEmerald)
                                        .frame(width: 5.5, height: 5.5)
                                    Text("实时额度")
                                        .font(.system(size: 10, weight: .bold))
                                        .foregroundStyle(.secondary)
                                }
                                .padding(.horizontal, 2)

                                ForEach(partitioned.freshProviders, id: \.provider.id) { entry in
                                    providerCard(
                                        provider: entry.provider,
                                        windows: entry.windows,
                                        isStaleSection: false
                                    )
                                }
                            }
                        }

                        // Section 2: Stale / Last Observed / Unavailable Detail Area
                        if !partitioned.staleProviders.isEmpty {
                            VStack(alignment: .leading, spacing: 6) {
                                HStack(spacing: 5) {
                                    Image(systemName: "clock.arrow.circlepath")
                                        .font(.system(size: 9.5, weight: .semibold))
                                        .foregroundStyle(amberAccent)
                                    Text("上次记录与未就绪项")
                                        .font(.system(size: 10, weight: .bold))
                                        .foregroundStyle(.secondary)
                                }
                                .padding(.horizontal, 2)
                                .padding(.top, partitioned.freshProviders.isEmpty ? 0 : 2)

                                ForEach(partitioned.staleProviders, id: \.provider.id) { entry in
                                    providerCard(
                                        provider: entry.provider,
                                        windows: entry.windows,
                                        isStaleSection: true
                                    )
                                }
                            }
                        }
                    }
                    .padding(.trailing, 2)
                }
                .frame(maxHeight: 380)
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
                .pointingHandCursor(!store.isRefreshingQuotas)
                .disabled(store.isRefreshingQuotas)
            }
        }
    }

    private func providerCard(
        provider p: QuotaProvider,
        windows: [QuotaWindow],
        isStaleSection: Bool
    ) -> some View {
        let observedText = observationTimestampText(provider: p)

        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Circle()
                    .fill(Color(hex: p.color).opacity(isStaleSection ? 0.55 : 1.0))
                    .frame(width: 7, height: 7)
                Text(p.name)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(isStaleSection ? .secondary : .primary)

                if isStaleSection && !windows.isEmpty {
                    Text("上次记录")
                        .font(.system(size: 9, weight: .bold))
                        .foregroundStyle(amberAccent)
                        .padding(.horizontal, 5.5)
                        .padding(.vertical, 1.5)
                        .background(amberAccent.opacity(0.14), in: Capsule())
                }

                Spacer()

                Text(observedText)
                    .font(.system(size: 9.5, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 6.5)
                    .padding(.vertical, 2)
                    .background(Color.primary.opacity(0.06), in: Capsule())
                    .lineLimit(1)
            }

            if isStaleSection && (p.status != "ok" || windows.isEmpty) {
                Text(p.statusText)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
            }

            if !windows.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(windows) { w in
                        quotaWindowRow(provider: p, window: w, isStale: isStaleSection)
                    }
                }
            }
        }
        .padding(10.5)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(isStaleSection ? cardBorder : Color(hex: p.color).opacity(0.22), lineWidth: 0.65)
        )
        .opacity(isStaleSection ? 0.86 : 1.0)
    }

    private func quotaWindowRow(provider p: QuotaProvider, window w: QuotaWindow, isStale: Bool) -> some View {
        let rawBarColor = Formatters.quotaColor(for: w.usedPercent, defaultColor: Color(hex: p.color))
        let barColor = isStale ? mutedSlate : rawBarColor
        let resetStr = Formatters.relativeReset(w.resetsAt, now: store.nowEpoch)

        return VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                Text(w.label)
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(isStale ? .secondary : .primary)
                Spacer()
                Text(resetStr.isEmpty ? "\(Int(round(w.usedPercent)))%" : "\(Int(round(w.usedPercent)))% · \(resetStr)")
                    .font(.system(size: 10, weight: .semibold, design: .monospaced))
                    .monospacedDigit()
                    .foregroundStyle(isStale ? .secondary : .primary)
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

            if let pace = w.pace {
                paceRow(pace: pace, isStale: isStale)
            }
        }
    }

    private func paceRow(pace: QuotaPace, isStale: Bool) -> some View {
        let style = paceBadgeStyle(level: isStale ? .unknown : pace.level)
        let summary = isStale ? "读数已过期，暂无法预测配速" : pace.summaryText

        return VStack(alignment: .leading, spacing: 3.5) {
            HStack(alignment: .center, spacing: 5.5) {
                HStack(spacing: 3) {
                    Image(systemName: style.symbol)
                        .font(.system(size: 8.5, weight: .bold))
                    Text(style.label)
                        .font(.system(size: 9, weight: .bold))
                }
                .foregroundStyle(style.tint)
                .padding(.horizontal, 5.5)
                .padding(.vertical, 1.5)
                .background(style.tint.opacity(colorScheme == .dark ? 0.16 : 0.12), in: Capsule())
                .fixedSize()

                Text(summary)
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)

                Spacer(minLength: 2)

                if let rate = pace.ratePctPerHour, pace.level != .unknown, !isStale {
                    Text(String(format: "%.1f%%/h", rate))
                        .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                        .foregroundStyle(.tertiary)
                        .fixedSize()
                }
            }

            if let exhaustTs = pace.estimatedExhaustionTs, exhaustTs.isFinite, exhaustTs > 0, !isStale {
                HStack(spacing: 4) {
                    Image(systemName: "hourglass.bottomhalf.filled")
                        .font(.system(size: 8.5))
                        .foregroundStyle(style.tint)
                    Text("预计用尽：\(ViewTimeFormatters.monthDayHHmm(exhaustTs, now: store.nowEpoch))")
                        .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                        .foregroundStyle(style.tint)
                    if let projected = pace.projectedUsedPercent, projected.isFinite {
                        Text(String(format: "· 重置前约 %.0f%%", projected))
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(.tertiary)
                    }
                }
                .padding(.leading, 2)
            }
        }
        .padding(.horizontal, 7.5)
        .padding(.vertical, 5.5)
        .background(innerSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
    }

    private struct PaceStyle {
        let tint: Color
        let symbol: String
        let label: String
    }

    private func paceBadgeStyle(level: QuotaPaceLevel) -> PaceStyle {
        switch level {
        case .onTrack:
            return PaceStyle(tint: liveEmerald, symbol: "checkmark.circle.fill", label: "正常")
        case .watch:
            return PaceStyle(tint: amberAccent, symbol: "exclamationmark.circle.fill", label: "留意")
        case .risk:
            return PaceStyle(tint: riskCoral, symbol: "flame.fill", label: "预警")
        case .unknown:
            return PaceStyle(tint: mutedSlate, symbol: "minus.circle", label: "暂缺")
        }
    }

    private func observationTimestampText(provider p: QuotaProvider) -> String {
        guard p.fetchedAt > 0 else { return p.plan }
        let hhmm = ViewTimeFormatters.hhmm(Double(p.fetchedAt))
        let rel = Formatters.relativeUpdated(p.fetchedAt, now: store.nowEpoch)
        if p.status == "unavailable" {
            return "\(p.plan) · 记录于 \(hhmm)"
        }
        return "\(p.plan) · \(hhmm) (\(rel))"
    }
}
