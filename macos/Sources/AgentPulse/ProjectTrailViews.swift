import SwiftUI

struct ProjectTrailViews: View {
    @ObservedObject var store: ClockStore
    @Environment(\.colorScheme) private var colorScheme
    @State private var query: String = ""
    @State private var expandedProjectId: String?

    private var accentPurple: Color { Color(hex: "#7C6BF5") }
    private var amberAccent: Color {
        colorScheme == .dark ? Color(hex: "#FBBF24") : Color(hex: "#B45309")
    }
    private var cardSurface: Color {
        colorScheme == .dark ? Color.white.opacity(0.05) : Color.black.opacity(0.038)
    }
    private var cardBorder: Color {
        colorScheme == .dark ? Color.white.opacity(0.10) : Color.black.opacity(0.08)
    }

    private enum ProjectGroup: String, CaseIterable {
        case pinned, today, week, earlier, dormant

        var title: String {
            switch self {
            case .pinned: return "置顶项目"
            case .today: return "今日活跃"
            case .week: return "近 7 天"
            case .earlier: return "近 30 天"
            case .dormant: return "更早"
            }
        }
    }

    private var filteredProjects: [ProjectTrailItem] {
        let all = store.snapshot?.usage?.projects ?? []
        let q = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if q.isEmpty { return all }
        return all.filter {
            $0.name.lowercased().contains(q) || $0.displayPath.lowercased().contains(q)
        }
    }

    private func group(for item: ProjectTrailItem) -> ProjectGroup {
        if store.pinnedProjectPaths.contains(item.path) { return .pinned }
        switch item.group {
        case "today": return .today
        case "week": return .week
        case "earlier": return .earlier
        default: return .dormant
        }
    }

    var body: some View {
        let items = filteredProjects

        VStack(alignment: .leading, spacing: 9) {
            // Search Bar
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .font(.system(size: 11))
                    .foregroundStyle(.secondary)
                TextField("按项目名或路径搜索…", text: $query)
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
                Text("\(items.count) 个项目")
                    .font(.system(size: 10, weight: .medium))
                    .monospacedDigit()
                    .foregroundStyle(.tertiary)
            }
            .padding(.horizontal, 9)
            .padding(.vertical, 6.5)
            .background(cardSurface, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).strokeBorder(cardBorder, lineWidth: 0.6))

            if let error = store.projectActionError {
                Label(error, systemImage: "exclamationmark.circle")
                    .font(.system(size: 10.5))
                    .foregroundStyle(amberAccent)
            }

            if items.isEmpty {
                VStack(spacing: 6) {
                    Image(systemName: "folder")
                        .font(.system(size: 18))
                        .foregroundStyle(.tertiary)
                    Text(query.isEmpty ? "暂未采集到带目录信息的项目消耗" : "未找到匹配「\(query)」的项目")
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(.secondary)
                    Text("Codex / T3 Code / Claude Code / WorkBuddy / Pi 会自动按 cwd 归集")
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
                        ForEach(ProjectGroup.allCases, id: \.rawValue) { grp in
                            let grpItems = items.filter { group(for: $0) == grp }
                            if !grpItems.isEmpty {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(grp.title)
                                        .font(.system(size: 10, weight: .bold))
                                        .foregroundStyle(grp == .pinned ? amberAccent : .secondary)
                                        .padding(.horizontal, 2)

                                    ForEach(grpItems) { proj in
                                        projectRow(proj)
                                    }
                                }
                            }
                        }
                    }
                    .padding(.trailing, 2)
                }
                .frame(maxHeight: 410)
            }
        }
    }

    private func projectRow(_ proj: ProjectTrailItem) -> some View {
        let isPinned = store.pinnedProjectPaths.contains(proj.path)
        let isExpanded = expandedProjectId == proj.id
        let hitPct = Int(round(proj.cacheHitRate * 100))

        return VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top, spacing: 7) {
                Button {
                    withAnimation(.easeOut(duration: 0.14)) {
                        store.togglePinProject(proj.path)
                    }
                } label: {
                    Image(systemName: isPinned ? "star.fill" : "star")
                        .font(.system(size: 10.5))
                        .foregroundStyle(isPinned ? amberAccent : Color.secondary.opacity(0.55))
                        .padding(.top, 2)
                }
                .buttonStyle(.plain)
                .help(isPinned ? "取消置顶" : "置顶项目")

                Button {
                    withAnimation(.easeInOut(duration: 0.15)) {
                        expandedProjectId = isExpanded ? nil : proj.id
                    }
                } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        HStack(spacing: 6) {
                            Text(proj.name)
                                .font(.system(size: 12, weight: .bold))
                                .foregroundStyle(.primary)
                                .lineLimit(1)
                            Spacer(minLength: 4)
                            Text(Formatters.usd(proj.costUsd))
                                .font(.system(size: 12, weight: .bold, design: .monospaced))
                                .foregroundStyle(accentPurple)
                        }

                        Text(proj.displayPath)
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                            .truncationMode(.middle)

                        HStack(spacing: 5) {
                            ForEach(proj.tools.prefix(3)) { badge in
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
                            Spacer(minLength: 4)
                            Text("\(Formatters.compactTokens(proj.totalTokens)) · \(hitPct)% · \(proj.sessions)会话")
                                .font(.system(size: 9.5, design: .monospaced))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                        }
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }

            if isExpanded {
                VStack(alignment: .leading, spacing: 6) {
                    LazyVGrid(
                        columns: [GridItem(.flexible(), spacing: 8), GridItem(.flexible(), spacing: 8)],
                        alignment: .leading,
                        spacing: 4
                    ) {
                        kv("输入", Formatters.compactTokens(proj.inputTokens))
                        kv("输出", Formatters.compactTokens(proj.outputTokens))
                        kv("缓存读", Formatters.compactTokens(proj.cacheReadTokens))
                        if proj.cacheWriteTokens > 0 {
                            kv("缓存写", Formatters.compactTokens(proj.cacheWriteTokens))
                        }
                        if proj.reasoningTokens > 0 {
                            kv("推理", Formatters.compactTokens(proj.reasoningTokens))
                        }
                        kv("最近活跃", proj.lastActiveDate)
                    }

                    HStack(spacing: 6) {
                        Button {
                            store.openProjectInFinder(proj.path)
                        } label: {
                            Label("在访达中显示", systemImage: "folder")
                                .font(.system(size: 9.5, weight: .semibold))
                                .foregroundStyle(.primary)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3.5)
                                .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 5, style: .continuous))
                        }
                        .buttonStyle(.plain)

                        Button {
                            store.openProjectInTerminal(proj.path)
                        } label: {
                            Label("终端打开", systemImage: "terminal")
                                .font(.system(size: 9.5, weight: .semibold))
                                .foregroundStyle(.primary)
                                .padding(.horizontal, 7)
                                .padding(.vertical, 3.5)
                                .background(Color.primary.opacity(0.07), in: RoundedRectangle(cornerRadius: 5, style: .continuous))
                        }
                        .buttonStyle(.plain)
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
