import SwiftUI

struct ProjectTrailViews: View {
    @ObservedObject var store: ClockStore
    @Environment(\.colorScheme) private var colorScheme
    @State private var query: String = ""
    @State private var expandedProjectId: String?

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
        colorScheme == .dark ? Color.white.opacity(0.045) : Color.black.opacity(0.03)
    }
    private var cardBorder: Color {
        colorScheme == .dark ? Color.white.opacity(0.10) : Color.black.opacity(0.08)
    }

    private var filteredRows: [ProjectTrailRow] {
        let all = store.projectTrailRows(for: store.selectedProjectPeriod)
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if q.isEmpty { return all }
        return all.filter { row in
            if row.name.lowercased().contains(q) || row.displayPath.lowercased().contains(q) {
                return true
            }
            return row.stats.worktreeSources.contains { src in
                src.path.lowercased().contains(q) || (src.branch?.lowercased().contains(q) ?? false)
            }
        }
    }

    var body: some View {
        let rows = filteredRows
        let pinnedRows = rows.filter(\.isPinned)
        let regularRows = rows.filter { !$0.isPinned }

        VStack(alignment: .leading, spacing: 9) {
            // Segmented Period Selector (今日 / 本周 / 本月 / 全部)
            periodSegmentedBar(totalCount: rows.count)

            // Search Bar
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                TextField("按项目名、路径或分支搜索…", text: $query)
                    .textFieldStyle(.plain)
                    .font(.system(size: 11.5))
                if !query.isEmpty {
                    Button {
                        query = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.system(size: 11))
                            .foregroundStyle(.tertiary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("清除搜索")
                }
                Text("\(rows.count) 个项目")
                    .font(.system(size: 10, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6.5)
            .background(cardSurface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))

            if let error = store.projectActionError {
                HStack(spacing: 6) {
                    Image(systemName: "exclamationmark.circle.fill")
                        .font(.system(size: 10.5))
                        .foregroundStyle(amberAccent)
                    Text(error)
                        .font(.system(size: 10.5, weight: .medium))
                        .foregroundStyle(.primary)
                    Spacer()
                    Button {
                        store.projectActionError = nil
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

            if rows.isEmpty {
                VStack(spacing: 6) {
                    Image(systemName: "folder")
                        .font(.system(size: 18))
                        .foregroundStyle(.tertiary)
                    Text(query.isEmpty ? "「\(store.selectedProjectPeriod.title)」暂无活跃项目记录" : "未找到匹配「\(query)」的项目")
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(.secondary)
                    Text(query.isEmpty ? "可切换「本周」「本月」或「全部」查看历史项目" : "试试项目目录名、路径或 worktree 分支名")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 34)
                .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
            } else {
                ScrollView(.vertical, showsIndicators: true) {
                    VStack(alignment: .leading, spacing: 10) {
                        if !pinnedRows.isEmpty {
                            VStack(alignment: .leading, spacing: 5) {
                                Text("置顶项目")
                                    .font(.system(size: 10, weight: .bold))
                                    .foregroundStyle(amberAccent)
                                    .padding(.horizontal, 2)
                                ForEach(pinnedRows) { row in
                                    projectRow(row)
                                }
                            }
                        }

                        if !regularRows.isEmpty {
                            VStack(alignment: .leading, spacing: 5) {
                                if !pinnedRows.isEmpty {
                                    Text("\(store.selectedProjectPeriod.title)活跃")
                                        .font(.system(size: 10, weight: .bold))
                                        .foregroundStyle(.secondary)
                                        .padding(.horizontal, 2)
                                }
                                ForEach(regularRows) { row in
                                    projectRow(row)
                                }
                            }
                        }
                    }
                    .padding(.trailing, 2)
                }
                .frame(maxHeight: 390)
            }
        }
    }

    private func periodSegmentedBar(totalCount: Int) -> some View {
        HStack(spacing: 3) {
            ForEach(ProjectPeriodKey.allCases) { period in
                let isSel = store.selectedProjectPeriod == period
                Button {
                    withAnimation(.easeOut(duration: 0.14)) {
                        store.selectedProjectPeriod = period
                    }
                } label: {
                    Text(period.title)
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(isSel ? (colorScheme == .dark ? .white : accentPurple) : .secondary)
                        .frame(maxWidth: .infinity)
                        .padding(.vertical, 5)
                        .background(
                            isSel ? accentPurple.opacity(colorScheme == .dark ? 0.30 : 0.16) : Color.clear,
                            in: RoundedRectangle(cornerRadius: 6.5, style: .continuous)
                        )
                        .overlay(
                            RoundedRectangle(cornerRadius: 6.5, style: .continuous)
                                .strokeBorder(isSel ? accentPurple.opacity(0.42) : Color.clear, lineWidth: 0.6)
                        )
                }
                .buttonStyle(.plain)
                .pointingHandCursor()
            }
        }
        .padding(3)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 9, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
    }

    private func projectRow(_ row: ProjectTrailRow) -> some View {
        let stats = row.stats
        let isExpanded = expandedProjectId == row.id
        let hasCostObs = stats.totalTokens > 0 || stats.costUsd > 0
        let costText = Formatters.estimatedUSD(stats.costUsd, unpricedTokens: 0, hasObservation: hasCostObs)
        let hitPct = Int(round(stats.cacheHitRate * 100))
        let hasWorktreeMerge = stats.worktreeSources.count > 1

        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 7) {
                Button {
                    withAnimation(.easeOut(duration: 0.14)) {
                        store.togglePinProject(row.path)
                    }
                } label: {
                    Image(systemName: row.isPinned ? "star.fill" : "star")
                        .font(.system(size: 10.5))
                        .foregroundStyle(row.isPinned ? amberAccent : Color.secondary.opacity(0.55))
                        .padding(.top, 2)
                }
                .buttonStyle(.plain)
                .pointingHandCursor()
                .help(row.isPinned ? "取消置顶" : "置顶项目")
                .accessibilityLabel(row.isPinned ? "取消置顶 \(row.name)" : "置顶 \(row.name)")

                Button {
                    withAnimation(.easeInOut(duration: 0.15)) {
                        expandedProjectId = isExpanded ? nil : row.id
                    }
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        // Line 1: Project Name + Worktree Merge Count + AI Work Duration
                        HStack(spacing: 5) {
                            Text(row.name)
                                .font(.system(size: 12, weight: .bold))
                                .foregroundStyle(.primary)
                                .lineLimit(1)

                            if hasWorktreeMerge {
                                HStack(spacing: 2.5) {
                                    Image(systemName: "arrow.triangle.branch")
                                        .font(.system(size: 8, weight: .semibold))
                                    Text("\(stats.worktreeSources.count)")
                                        .font(.system(size: 9, weight: .semibold))
                                        .monospacedDigit()
                                }
                                .foregroundStyle(.secondary)
                                .padding(.horizontal, 4.5)
                                .padding(.vertical, 1.5)
                                .background(Color.primary.opacity(0.06), in: Capsule())
                                .help("已归并 \(stats.worktreeSources.count) 个 Git worktree 目录")
                            }

                            Spacer(minLength: 4)

                            workDurationBadge(seconds: stats.agentSeconds)
                        }

                        // Line 2: Display Path + Estimated Cost
                        HStack(spacing: 6) {
                            Text(row.displayPath)
                                .font(.system(size: 9.5, design: .monospaced))
                                .foregroundStyle(.tertiary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer(minLength: 4)
                            Text(costText)
                                .font(.system(size: 11, weight: .bold, design: .monospaced))
                                .monospacedDigit()
                                .foregroundStyle(hasCostObs ? accentPurple : Color.secondary.opacity(0.6))
                        }

                        // Line 3: Tool Badges + Token / Cache / Sessions
                        HStack(spacing: 5) {
                            ForEach(stats.tools.prefix(3)) { badge in
                                HStack(spacing: 3) {
                                    Circle()
                                        .fill(Color(hex: badge.color))
                                        .frame(width: 5, height: 5)
                                    Text(badge.shortName)
                                        .font(.system(size: 9, weight: .medium))
                                        .foregroundStyle(.secondary)
                                }
                                .padding(.horizontal, 5)
                                .padding(.vertical, 1.5)
                                .background(Color.primary.opacity(0.06), in: Capsule())
                            }
                            if stats.tools.count > 3 {
                                Text("+\(stats.tools.count - 3)")
                                    .font(.system(size: 9, weight: .medium))
                                    .foregroundStyle(.tertiary)
                            }
                            Spacer(minLength: 4)
                            if stats.totalTokens > 0 {
                                Text("\(Formatters.compactTokens(stats.totalTokens)) · \(hitPct)% · \(stats.sessions)会话")
                                    .font(.system(size: 9.5, design: .monospaced))
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            } else {
                                Text(stats.sessions > 0 ? "\(stats.sessions) 会话 · 仅工时" : "仅工时归集")
                                    .font(.system(size: 9.5))
                                    .foregroundStyle(.tertiary)
                                    .lineLimit(1)
                            }
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .pointingHandCursor()
            }

            if isExpanded {
                VStack(alignment: .leading, spacing: 7) {
                    LazyVGrid(
                        columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)],
                        alignment: .leading,
                        spacing: 4
                    ) {
                        kv("AI 工时", workDurationPlainText(stats.agentSeconds))
                        kv("估算费用", costText)
                        if stats.totalTokens > 0 {
                            kv("输入", Formatters.compactTokens(stats.inputTokens))
                            kv("输出", Formatters.compactTokens(stats.outputTokens))
                            kv("缓存读", Formatters.compactTokens(stats.cacheReadTokens))
                            if stats.cacheWriteTokens > 0 {
                                kv("缓存写", Formatters.compactTokens(stats.cacheWriteTokens))
                            }
                            if stats.reasoningTokens > 0 {
                                kv("推理", Formatters.compactTokens(stats.reasoningTokens))
                            }
                        }
                        kv("最近活跃", stats.lastActiveDate)
                    }
                    .padding(8)
                    .background(innerSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))

                    // Worktree Sources Disclosure (when merged or branch info available)
                    if !stats.worktreeSources.isEmpty && (stats.worktreeSources.count > 1 || stats.worktreeSources.contains { $0.branch != nil }) {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(spacing: 4) {
                                Image(systemName: "arrow.triangle.branch")
                                    .font(.system(size: 8.5, weight: .semibold))
                                    .foregroundStyle(.secondary)
                                Text(stats.worktreeSources.count > 1 ? "已归并工作目录 (\(stats.worktreeSources.count))" : "工作目录与分支")
                                    .font(.system(size: 9.5, weight: .semibold))
                                    .foregroundStyle(.secondary)
                            }

                            ForEach(stats.worktreeSources) { src in
                                Button {
                                    store.openProjectInFinder(src.path)
                                } label: {
                                    HStack(spacing: 5) {
                                        if let branch = src.branch, !branch.isEmpty {
                                            Text(branch)
                                                .font(.system(size: 9, weight: .semibold, design: .monospaced))
                                                .foregroundStyle(accentPurple)
                                                .padding(.horizontal, 5)
                                                .padding(.vertical, 1)
                                                .background(accentPurple.opacity(0.12), in: Capsule())
                                                .lineLimit(1)
                                        }
                                        Text(abbreviateHome(src.path))
                                            .font(.system(size: 9, design: .monospaced))
                                            .foregroundStyle(.secondary)
                                            .lineLimit(1)
                                            .truncationMode(.middle)
                                        Spacer(minLength: 2)
                                        Image(systemName: "arrow.up.forward")
                                            .font(.system(size: 8))
                                            .foregroundStyle(.tertiary)
                                    }
                                    .contentShape(Rectangle())
                                }
                                .buttonStyle(.plain)
                                .pointingHandCursor()
                                .help("在访达中显示：\(src.path)")
                            }
                        }
                        .padding(8)
                        .background(innerSurface, in: RoundedRectangle(cornerRadius: 7, style: .continuous))
                    }

                    HStack(spacing: 6) {
                        Button {
                            store.openProjectInFinder(row.path)
                        } label: {
                            Label("在访达中显示", systemImage: "folder")
                                .font(.system(size: 9.5, weight: .semibold))
                                .foregroundStyle(.primary)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                        }
                        .buttonStyle(.plain)
                        .pointingHandCursor()

                        Button {
                            store.openProjectInTerminal(row.path)
                        } label: {
                            Label("终端打开", systemImage: "terminal")
                                .font(.system(size: 9.5, weight: .semibold))
                                .foregroundStyle(.primary)
                                .padding(.horizontal, 8)
                                .padding(.vertical, 4)
                                .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 6, style: .continuous))
                        }
                        .buttonStyle(.plain)
                        .pointingHandCursor()
                        Spacer()
                    }
                }
                .padding(.top, 2)
                .padding(.leading, 18)
            }
        }
        .padding(10)
        .background(cardSurface, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))
    }

    @ViewBuilder
    private func workDurationBadge(seconds: Double?) -> some View {
        if let sec = seconds, sec > 0 {
            HStack(spacing: 3) {
                Image(systemName: "bolt.fill")
                    .font(.system(size: 8))
                Text(Formatters.shortDuration(sec))
                    .font(.system(size: 10.5, weight: .bold, design: .monospaced))
                    .monospacedDigit()
            }
            .foregroundStyle(liveEmerald)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(liveEmerald.opacity(0.13), in: Capsule())
        } else if seconds == nil {
            Text("未归集")
                .font(.system(size: 9.5, weight: .medium))
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 5.5)
                .padding(.vertical, 1.5)
                .background(Color.primary.opacity(0.05), in: Capsule())
                .help("该周期暂无可归集到此项目目录的 AI 工时记录")
        } else {
            Text("—")
                .font(.system(size: 10, weight: .medium, design: .monospaced))
                .foregroundStyle(.tertiary)
        }
    }

    private func workDurationPlainText(_ seconds: Double?) -> String {
        guard let sec = seconds else { return "未归集" }
        return sec > 0 ? Formatters.shortDuration(sec) : "—"
    }

    private func abbreviateHome(_ path: String) -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if path == home { return "~" }
        if path.hasPrefix(home + "/") {
            return "~" + path.dropFirst(home.count)
        }
        return path
    }

    private func kv(_ k: String, _ v: String) -> some View {
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
}
