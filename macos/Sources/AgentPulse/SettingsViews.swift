import AppKit
import SwiftUI

struct SettingsViews: View {
    @ObservedObject var store: ClockStore
    @ObservedObject var sitReminder: SitReminderManager
    @Environment(\.colorScheme) private var colorScheme

    @State private var editingToolId: String?
    @State private var feeInputText: String = ""
    @State private var feeInputError: String?

    private var accentPurple: Color { Color(hex: "#7C6BF5") }
    private var liveEmerald: Color {
        colorScheme == .dark ? Color(hex: "#34D399") : Color(hex: "#15803D")
    }
    private var amberAccent: Color {
        colorScheme == .dark ? Color(hex: "#FBBF24") : Color(hex: "#B45309")
    }
    private var errorCoral: Color {
        colorScheme == .dark ? Color(hex: "#F87171") : Color(hex: "#DC2626")
    }
    private var cardSurface: Color {
        colorScheme == .dark ? Color.white.opacity(0.055) : Color.black.opacity(0.04)
    }
    private var innerSurface: Color {
        colorScheme == .dark ? Color.white.opacity(0.045) : Color.black.opacity(0.03)
    }
    private var cardBorder: Color {
        colorScheme == .dark ? Color.white.opacity(0.10) : Color.black.opacity(0.08)
    }

    private let quickFeePresets: [(label: String, cents: Int)] = [
        ("$20", 2_000),
        ("$100", 10_000),
        ("$200", 20_000),
    ]

    var body: some View {
        ScrollView(.vertical, showsIndicators: true) {
            VStack(alignment: .leading, spacing: 10) {
                subscriptionPaybackCard
                sitReminderCard
                menuBarDisplayCard
                desktopWidgetCard
                dataAndMigrationCard
            }
            .padding(.trailing, 2)
        }
        .frame(maxHeight: 455)
    }

    // MARK: - Monthly Subscription Payback Card

    private var subscriptionPaybackCard: some View {
        let hasMonthData = store.currentMonthSubscriptionDataAvailable
        let rows = store.subscriptionRows
        let subRows = rows.filter { $0.subscriptionValueUsd != nil || $0.monthlyFeeCents != nil }
        let apiRows = rows.filter { $0.apiActualUsd != nil }
        let totalApiActual = apiRows.reduce(Decimal(0)) { $0 + ($1.apiActualUsd ?? Decimal(0)) }
        let hasCodexRow = subRows.contains { $0.tool == "codex" }
        let hasClaudeRow = subRows.contains { $0.tool == "claude_code" }

        return VStack(alignment: .leading, spacing: 9) {
            HStack(spacing: 6) {
                Image(systemName: "chart.bar.doc.horizontal")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(accentPurple)
                Text("本月订阅回本仪")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(.primary)
                Spacer()
                Text(hasMonthData ? "自然月累计" : "待本月数据")
                    .font(.system(size: 9.5, weight: .medium))
                    .foregroundStyle(hasMonthData ? AnyShapeStyle(.tertiary) : AnyShapeStyle(amberAccent))
            }

            if !hasMonthData {
                HStack(spacing: 5.5) {
                    Image(systemName: "calendar.badge.clock")
                        .font(.system(size: 10, weight: .semibold))
                        .foregroundStyle(amberAccent)
                    Text("本月尚无新数据，刷新后将自动统计本月订阅折算与实付")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(colorScheme == .dark ? amberAccent.opacity(0.95) : amberAccent)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 2)
                }
                .padding(.horizontal, 8.5)
                .padding(.vertical, 5)
                .background(amberAccent.opacity(colorScheme == .dark ? 0.12 : 0.09), in: RoundedRectangle(cornerRadius: 7.5, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 7.5, style: .continuous).strokeBorder(amberAccent.opacity(0.25), lineWidth: 0.6))
            }

            // Explicit Two-Column Layout:
            // Left: Subscription Converted Value & Payback Multiple
            // Right: Verified API Actual Cost
            HStack(alignment: .top, spacing: 8) {
                // Column 1: Subscription Value & Payback
                VStack(alignment: .leading, spacing: 6) {
                    Text("订阅折算与回本")
                        .font(.system(size: 9.5, weight: .bold))
                        .foregroundStyle(.secondary)

                    if subRows.isEmpty {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(hasMonthData ? "本月暂无订阅折算记录" : "本月尚无新数据")
                                .font(.system(size: 10.5, weight: .medium))
                                .foregroundStyle(.secondary)
                            Text(hasMonthData ? "可先设置常用订阅月费，产生用量后自动计算回本倍率" : "可先配置订阅月费，待获取本月记录后自动计算回本倍率")
                                .font(.system(size: 9.5))
                                .foregroundStyle(.tertiary)
                                .fixedSize(horizontal: false, vertical: true)
                            HStack(spacing: 5) {
                                presetToolSetupButton(tool: "codex", title: "+ 设 Codex")
                                presetToolSetupButton(tool: "claude_code", title: "+ 设 Claude")
                            }
                            .padding(.top, 2)
                        }
                        .padding(.vertical, 3)
                    } else {
                        VStack(alignment: .leading, spacing: 6) {
                            ForEach(subRows) { row in
                                subscriptionColumnRow(row, hasMonthData: hasMonthData)
                            }
                            if !hasCodexRow || !hasClaudeRow {
                                HStack(spacing: 5) {
                                    if !hasCodexRow {
                                        presetToolSetupButton(tool: "codex", title: "+ 设 Codex")
                                    }
                                    if !hasClaudeRow {
                                        presetToolSetupButton(tool: "claude_code", title: "+ 设 Claude")
                                    }
                                }
                                .padding(.top, 1)
                            }
                        }
                    }
                }
                .padding(8.5)
                .frame(maxWidth: .infinity, alignment: .topLeading)
                .background(innerSurface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.5))

                // Column 2: Verified API Actual Spend
                VStack(alignment: .leading, spacing: 6) {
                    Text("已核实 API 实付")
                        .font(.system(size: 9.5, weight: .bold))
                        .foregroundStyle(.secondary)

                    if apiRows.isEmpty {
                        VStack(alignment: .leading, spacing: 3) {
                            Text(hasMonthData ? "暂无可核实的 API 实付" : "本月尚无新数据")
                                .font(.system(size: 10.5, weight: .medium))
                                .foregroundStyle(.secondary)
                            Text(hasMonthData ? "仅统计接口返回的真实扣费，不含按价表估算的费用" : "刷新获取本月记录后，将自动统计接口返回的真实扣费")
                                .font(.system(size: 9.5))
                                .foregroundStyle(.tertiary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        .padding(.vertical, 4)
                    } else {
                        VStack(alignment: .leading, spacing: 5) {
                            Text(Formatters.usd(totalApiActual))
                                .font(.system(size: 16, weight: .bold, design: .monospaced))
                                .monospacedDigit()
                                .foregroundStyle(.primary)

                            ForEach(apiRows) { row in
                                if let actual = row.apiActualUsd {
                                    HStack(spacing: 4) {
                                        Text(row.displayName)
                                            .font(.system(size: 10, weight: .medium))
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                        Spacer(minLength: 2)
                                        Text(Formatters.usd(actual))
                                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                                            .monospacedDigit()
                                            .foregroundStyle(.primary)
                                    }
                                }
                            }
                        }
                    }
                }
                .padding(8.5)
                .frame(width: 166, alignment: .topLeading)
                .background(innerSurface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.5))
            }

            // Inline Fee Editor (opens cleanly below the two columns for generous typing room)
            if let activeTool = editingToolId {
                let row = subRows.first(where: { $0.tool == activeTool }) ?? SubscriptionRow(
                    tool: activeTool,
                    displayName: activeTool == "claude_code" ? "Claude Code" : (activeTool == "codex" ? "Codex" : activeTool),
                    monthlyFeeCents: nil,
                    subscriptionValueUsd: nil,
                    paybackMultiple: nil,
                    apiActualUsd: nil
                )
                feeEditorPanel(for: row)
            }

            // Mandatory Disclosure Note
            Text("适合包月订阅工具估算回本进度；若同一工具混用订阅与按量 API，建议清空月费以免高估，右侧 API 实付仍会单独统计。")
                .font(.system(size: 9.5))
                .foregroundStyle(.tertiary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(10.5)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
    }

    private func presetToolSetupButton(tool: String, title: String) -> some View {
        Button {
            editingToolId = tool
            feeInputText = ""
            feeInputError = nil
        } label: {
            Text(title)
                .font(.system(size: 9.5, weight: .semibold))
                .foregroundStyle(accentPurple)
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(accentPurple.opacity(0.14), in: Capsule())
        }
        .buttonStyle(.plain)
        .pointingHandCursor()
    }

    private func subscriptionColumnRow(_ row: SubscriptionRow, hasMonthData: Bool) -> some View {
        let isEditing = editingToolId == row.tool
        let hasFee = row.monthlyFeeCents != nil

        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 5) {
                Text(row.displayName)
                    .font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(.primary)
                    .lineLimit(1)

                Spacer(minLength: 2)

                if let mult = row.paybackMultiple {
                    let isPaidBack = mult >= Decimal(1)
                    Text(Formatters.paybackMultiple(mult))
                        .font(.system(size: 10, weight: .bold, design: .monospaced))
                        .monospacedDigit()
                        .foregroundStyle(isPaidBack ? liveEmerald : accentPurple)
                        .padding(.horizontal, 5.5)
                        .padding(.vertical, 1.5)
                        .background(
                            (isPaidBack ? liveEmerald : accentPurple).opacity(0.14),
                            in: Capsule()
                        )
                } else if hasFee {
                    Text(hasMonthData ? "待用量" : "待新数据")
                        .font(.system(size: 9, weight: .medium))
                        .foregroundStyle(.tertiary)
                }
            }

            HStack(spacing: 5) {
                if let subVal = row.subscriptionValueUsd {
                    Text("折算 \(Formatters.usd(subVal))")
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .monospacedDigit()
                        .foregroundStyle(.secondary)
                } else {
                    Text(hasMonthData ? "折算 $0.00" : "折算 —")
                        .font(.system(size: 10, weight: .medium, design: .monospaced))
                        .foregroundStyle(.tertiary)
                }

                Spacer(minLength: 2)

                Button {
                    toggleFeeEditor(for: row)
                } label: {
                    HStack(spacing: 2.5) {
                        if let cents = row.monthlyFeeCents {
                            Text("月费 \(formatCentsUSD(cents))")
                                .font(.system(size: 9.5, weight: .semibold, design: .monospaced))
                        } else {
                            Text("设月费")
                                .font(.system(size: 9.5, weight: .semibold))
                        }
                        Image(systemName: isEditing ? "chevron.up" : "pencil")
                            .font(.system(size: 8, weight: .semibold))
                    }
                    .foregroundStyle(hasFee ? .primary : accentPurple)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 2)
                    .background(
                        (hasFee ? Color.primary.opacity(0.07) : accentPurple.opacity(0.14)),
                        in: Capsule()
                    )
                }
                .buttonStyle(.plain)
                .pointingHandCursor()
                .help(hasFee ? "修改或清空 \(row.displayName) 月费" : "配置 \(row.displayName) 月费以计算回本倍率")
            }
        }
        .padding(.vertical, 2)
    }

    private func feeEditorPanel(for row: SubscriptionRow) -> some View {
        VStack(alignment: .leading, spacing: 6.5) {
            HStack(spacing: 5) {
                Text("设置「\(row.displayName)」月费 (USD)")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.primary)
                Spacer()
                Button {
                    editingToolId = nil
                    feeInputError = nil
                } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 8.5, weight: .bold))
                        .foregroundStyle(.secondary)
                        .frame(width: 18, height: 18)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("关闭月费编辑")
            }

            // Quick Presets ($20 / $100 / $200) + Clear
            HStack(spacing: 5) {
                ForEach(quickFeePresets, id: \.cents) { preset in
                    let isSelected = row.monthlyFeeCents == preset.cents
                    Button {
                        applyPresetFee(cents: preset.cents, for: row.tool)
                    } label: {
                        Text(preset.label)
                            .font(.system(size: 10, weight: .semibold, design: .monospaced))
                            .foregroundStyle(isSelected ? .white : .primary)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3.5)
                            .background(
                                isSelected ? accentPurple : Color.primary.opacity(0.07),
                                in: RoundedRectangle(cornerRadius: 6, style: .continuous)
                            )
                    }
                    .buttonStyle(.plain)
                    .pointingHandCursor()
                }

                Spacer()

                if row.monthlyFeeCents != nil {
                    Button {
                        clearFee(for: row.tool)
                    } label: {
                        Text("清空月费")
                            .font(.system(size: 10, weight: .medium))
                            .foregroundStyle(errorCoral)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3.5)
                            .background(errorCoral.opacity(0.11), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                    }
                    .buttonStyle(.plain)
                    .pointingHandCursor()
                }
            }

            // Custom USD 2-decimal Input + Save
            let isBlankToClear = row.monthlyFeeCents != nil && feeInputText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            HStack(spacing: 6) {
                HStack(spacing: 4) {
                    Text("$")
                        .font(.system(size: 11, weight: .semibold, design: .monospaced))
                        .foregroundStyle(.secondary)
                    TextField(row.monthlyFeeCents != nil ? "如 20.00（留空可清空）" : "例如 20.00", text: $feeInputText)
                        .textFieldStyle(.plain)
                        .font(.system(size: 11, design: .monospaced))
                        .onSubmit {
                            saveCustomFee(for: row)
                        }
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 5)
                .background(Color.primary.opacity(0.06), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                .overlay(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .strokeBorder(feeInputError != nil ? errorCoral.opacity(0.75) : cardBorder, lineWidth: 0.7)
                )

                Button {
                    saveCustomFee(for: row)
                } label: {
                    Text(isBlankToClear ? "清空" : "保存")
                        .font(.system(size: 10.5, weight: .semibold))
                        .foregroundStyle(.white)
                        .padding(.horizontal, 11)
                        .padding(.vertical, 5)
                        .background(isBlankToClear ? errorCoral : accentPurple, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                }
                .buttonStyle(.plain)
                .pointingHandCursor()
            }

            if let err = feeInputError {
                Label(err, systemImage: "exclamationmark.circle.fill")
                    .font(.system(size: 9.5, weight: .medium))
                    .foregroundStyle(errorCoral)
            }
        }
        .padding(8.5)
        .background(accentPurple.opacity(colorScheme == .dark ? 0.10 : 0.06), in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(accentPurple.opacity(0.28), lineWidth: 0.65))
    }

    private func toggleFeeEditor(for row: SubscriptionRow) {
        if editingToolId == row.tool {
            editingToolId = nil
            feeInputError = nil
        } else {
            editingToolId = row.tool
            feeInputError = nil
            if let cents = row.monthlyFeeCents {
                feeInputText = String(format: "%.2f", Double(cents) / 100.0)
            } else {
                feeInputText = ""
            }
        }
    }

    private func applyPresetFee(cents: Int, for tool: String) {
        if store.setSubscriptionFee(cents: cents, for: tool) {
            feeInputText = String(format: "%.2f", Double(cents) / 100.0)
            feeInputError = nil
            editingToolId = nil
        } else {
            feeInputError = "月费保存失败，请检查输入范围。"
        }
    }

    private func clearFee(for tool: String) {
        if store.setSubscriptionFee(cents: nil, for: tool) {
            feeInputText = ""
            feeInputError = nil
            editingToolId = nil
        } else {
            feeInputError = "清空月费失败，请重试。"
        }
    }

    private func saveCustomFee(for row: SubscriptionRow) {
        let trimmed = feeInputText.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            if row.monthlyFeeCents != nil {
                clearFee(for: row.tool)
            } else {
                feeInputError = "请选择上方常用档位，或输入月费金额"
            }
            return
        }
        guard let cents = Self.parseUSDToCents(trimmed) else {
            feeInputError = "请输入 $0.01 ~ $10,000.00 的有效金额（最多两位小数）"
            return
        }
        if store.setSubscriptionFee(cents: cents, for: row.tool) {
            feeInputText = String(format: "%.2f", Double(cents) / 100.0)
            feeInputError = nil
            editingToolId = nil
        } else {
            feeInputError = "月费超出允许范围（$0.01 ~ $10,000.00）"
        }
    }

    private func formatCentsUSD(_ cents: Int) -> String {
        if cents % 100 == 0 {
            return "$\(cents / 100)"
        }
        return String(format: "$%.2f", Double(cents) / 100.0)
    }

    /// Strict USD parser (up to 2 decimal places, exact integer cents, no floating point drift).
    private static func parseUSDToCents(_ raw: String) -> Int? {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.hasPrefix("$") {
            s.removeFirst()
            s = s.trimmingCharacters(in: .whitespacesAndNewlines)
        }
        guard !s.isEmpty else { return nil }
        let parts = s.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 1 || parts.count == 2 else { return nil }

        let wholeStr = String(parts[0])
        let fracStr = parts.count == 2 ? String(parts[1]) : ""

        guard !wholeStr.isEmpty || !fracStr.isEmpty else { return nil }
        guard wholeStr.allSatisfy(\.isASCII) && wholeStr.allSatisfy(\.isNumber) else { return nil }
        guard fracStr.count <= 2, fracStr.allSatisfy(\.isASCII) && fracStr.allSatisfy(\.isNumber) else { return nil }

        let whole = wholeStr.isEmpty ? 0 : (Int(wholeStr) ?? -1)
        guard whole >= 0, whole <= 10_000 else { return nil }

        let fracCents: Int
        switch fracStr.count {
        case 0: fracCents = 0
        case 1: fracCents = (Int(fracStr) ?? 0) * 10
        case 2: fracCents = Int(fracStr) ?? 0
        default: return nil
        }

        let totalCents = whole * 100 + fracCents
        guard ClockStore.subscriptionFeeRange.contains(totalCents) else { return nil }
        return totalCents
    }

    // MARK: - Sit Reminder Card

    private var sitReminderCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("久坐提醒")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(.secondary)

            Toggle(isOn: $sitReminder.isEnabled) {
                Text("开启久坐提醒通知")
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
                            .pointingHandCursor()
                        }
                    }
                }

                Toggle(isOn: $sitReminder.resetOnSleep) {
                    Text("锁屏或休眠超 5 分钟自动重置")
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
                    .pointingHandCursor()

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
                    .pointingHandCursor()
                }
            }
        }
        .padding(10)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
    }

    // MARK: - Menu Bar Display Card

    private var menuBarDisplayCard: some View {
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
    }

    // MARK: - Desktop Widget & System Card

    private var desktopWidgetCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("桌面挂件与系统")
                .font(.system(size: 10, weight: .bold))
                .foregroundStyle(.secondary)

            Toggle(isOn: $store.cardShowQuotaAlert) {
                Text("挂件底部显示高额度预警（≥70% 时出现）")
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
    }

    // MARK: - Data & Migration Card

    private var dataAndMigrationCard: some View {
        VStack(alignment: .leading, spacing: 7) {
            Text("本地数据与历史导入")
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
                .pointingHandCursor(!store.isMigrating)
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
                .pointingHandCursor()

                Spacer()

                Button {
                    NSApp.terminate(nil)
                } label: {
                    Text("退出")
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(errorCoral)
                        .padding(.horizontal, 8)
                        .padding(.vertical, 5)
                        .background(errorCoral.opacity(0.12), in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                }
                .buttonStyle(.plain)
                .pointingHandCursor()
            }
        }
        .padding(10)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
    }
}
