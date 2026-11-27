import AppKit
import SwiftUI

struct PopoverRootView: View {
    @ObservedObject var store: ClockStore
    @ObservedObject var sitReminder: SitReminderManager
    @Environment(\.colorScheme) private var colorScheme
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var accentPurple: Color { Color(hex: "#7C6BF5") }
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
            return toolCount >= 2 ? 624 : 460
        case .dashboard:
            return 624
        case .projects, .quotas:
            return 460
        case .settings:
            return 480
        }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 11) {
            topHeader
            segmentedTabBar

            if store.selectedPopoverTab != .quotas && store.fastFreshness != .fresh && store.snapshot != nil {
                FreshnessBannerView(store: store, compact: false)
            } else if let error = store.lastError, store.snapshot == nil, store.selectedPopoverTab != .quotas {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.system(size: 10.5))
                    .foregroundStyle(amberAccent)
            }

            if let skipped = store.snapshot?.usage?.ledgerMeta.skippedInvalidDays, skipped > 0,
               store.selectedPopoverTab == .overview || store.selectedPopoverTab == .dashboard {
                Label("已跳过 \(skipped) 条日期异常的历史记录（原文件已保留）", systemImage: "exclamationmark.circle")
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
                    QuotaViews(store: store)
                case .settings:
                    SettingsViews(store: store, sitReminder: sitReminder)
                }
            }
        }
        .padding(14)
        .frame(width: dynamicWidth)
        .background(VisualEffectSurface(material: .popover, blendingMode: .behindWindow))
        .animation(reduceMotion ? nil : .easeOut(duration: 0.16), value: dynamicWidth)
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

                Text("v\(Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "0.7.0")")
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
            .pointingHandCursor()
            .help(sitReminder.isDue ? "已到久坐提醒时间，点击重置计时" : "久坐提醒倒计时，点击前往设置")

            Button {
                store.refreshAll(forceQuotas: store.selectedPopoverTab == .quotas)
            } label: {
                let isSpinning = (store.isRefreshing || store.isRefreshingQuotas) && !reduceMotion
                Image(systemName: "arrow.clockwise")
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .rotationEffect(.degrees(isSpinning ? 360 : 0))
                    .animation(
                        isSpinning ? .linear(duration: 0.7).repeatForever(autoreverses: false) : .default,
                        value: isSpinning
                    )
                    .frame(width: 24, height: 24)
                    .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
            }
            .buttonStyle(.plain)
            .pointingHandCursor()
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
                    withAnimation(reduceMotion ? nil : .easeOut(duration: 0.14)) {
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
                .pointingHandCursor()
            }
        }
        .padding(3)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .strokeBorder(cardBorder, lineWidth: 0.6)
        )
    }
}
