import SwiftUI

// MARK: - Main Snapshot (Schema v3, backward-compatible with v2)

struct ClockSnapshot: Codable, Equatable {
    let schemaVersion: Int
    let generatedAt: String
    let generatedTs: Double
    let timezone: String
    let guardrails: Guardrails
    let today: DayStats
    let last7Days: [DayStats]
    let summary7d: Summary7D
    let days: [DayStats]
    let activeAgents: [ActiveAgent]
    let tokeiRef: TokeiRef
    let usage: UsagePayload?
    let elapsedMs: Double?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generatedAt = "generated_at"
        case generatedTs = "generated_ts"
        case timezone
        case guardrails
        case today
        case last7Days = "last_7_days"
        case summary7d = "summary_7d"
        case days
        case activeAgents = "active_agents"
        case tokeiRef = "tokei_ref"
        case usage
        case elapsedMs = "elapsed_ms"
    }
}

struct Guardrails: Codable, Equatable {
    let mode: String?
    let heartbeatGapMinutes: Int?
    let maxTurnMinutes: Int?
    let maxStepGapMinutes: Int?
    let workdayHours: Int

    enum CodingKeys: String, CodingKey {
        case mode
        case heartbeatGapMinutes = "heartbeat_gap_minutes"
        case maxTurnMinutes = "max_turn_minutes"
        case maxStepGapMinutes = "max_step_gap_minutes"
        case workdayHours = "workday_hours"
    }
}

struct DayStats: Codable, Equatable, Identifiable {
    var id: String { date }
    let date: String
    let shortDate: String
    let weekday: String
    let isToday: Bool
    let agentSeconds: Double
    let agentHours: Double
    let workdays: Double
    let wallSeconds: Double
    let wallHours: Double
    let leverage: Double
    let runs: Int
    let tools: [ToolBreakdown]
    let hourlySeconds: [Double]
    let tokens: Int?
    let costUsd: Double?
    let cacheHitRate: Double?

    enum CodingKeys: String, CodingKey {
        case date
        case shortDate = "short_date"
        case weekday
        case isToday = "is_today"
        case agentSeconds = "agent_seconds"
        case agentHours = "agent_hours"
        case workdays
        case wallSeconds = "wall_seconds"
        case wallHours = "wall_hours"
        case leverage
        case runs
        case tools
        case hourlySeconds = "hourly_seconds"
        case tokens
        case costUsd = "cost_usd"
        case cacheHitRate = "cache_hit_rate"
    }
}

struct ToolBreakdown: Codable, Equatable, Identifiable {
    var id: String { tool }
    let tool: String
    let displayName: String
    let shortName: String
    let color: String
    let agentSeconds: Double
    let agentHours: Double
    let runs: Int
    let share: Double
    let tokens: Int?
    let costUsd: Double?

    enum CodingKeys: String, CodingKey {
        case tool
        case displayName = "display_name"
        case shortName = "short_name"
        case color
        case agentSeconds = "agent_seconds"
        case agentHours = "agent_hours"
        case runs
        case share
        case tokens
        case costUsd = "cost_usd"
    }
}

struct ActiveAgent: Codable, Equatable, Identifiable {
    var id: String { "\(tool)-\(Int(startedAt))" }
    let tool: String
    let displayName: String
    let shortName: String
    let color: String
    let startedAt: Double
    let elapsedSeconds: Double
    let label: String

    enum CodingKeys: String, CodingKey {
        case tool
        case displayName = "display_name"
        case shortName = "short_name"
        case color
        case startedAt = "started_at"
        case elapsedSeconds = "elapsed_seconds"
        case label
    }
}

struct Summary7D: Codable, Equatable {
    let totalSeconds: Double
    let totalHours: Double
    let avgDailyHours: Double
    let activeDays: Int

    enum CodingKeys: String, CodingKey {
        case totalSeconds = "total_seconds"
        case totalHours = "total_hours"
        case avgDailyHours = "avg_daily_hours"
        case activeDays = "active_days"
    }
}

struct TokeiRef: Codable, Equatable {
    let available: Bool
    let todayTokens: Int
    let todayCostUsd: Double

    enum CodingKeys: String, CodingKey {
        case available
        case todayTokens = "today_tokens"
        case todayCostUsd = "today_cost_usd"
    }
}

// MARK: - Native Usage & Ledger Models

struct UsagePayload: Codable, Equatable {
    let source: String
    let periods: UsagePeriods
    let history30d: [HistoryDayPoint]
    let heatmap: HeatmapPayload?
    let projects: [ProjectTrailItem]?
    let ledgerMeta: LedgerMeta

    enum CodingKeys: String, CodingKey {
        case source
        case periods
        case history30d = "history_30d"
        case heatmap
        case projects
        case ledgerMeta = "ledger_meta"
    }
}

struct UsagePeriods: Codable, Equatable {
    let today: PeriodStats
    let yesterday: PeriodStats?
    let week: PeriodStats
    let lastWeek: PeriodStats?
    let month: PeriodStats
    let year: PeriodStats
    let all: PeriodStats?

    enum CodingKeys: String, CodingKey {
        case today
        case yesterday
        case week
        case lastWeek = "last_week"
        case month
        case year
        case all
    }

    func stats(for key: UsagePeriodKey) -> PeriodStats {
        switch key {
        case .today: return today
        case .yesterday: return yesterday ?? today
        case .week: return week
        case .lastWeek: return lastWeek ?? week
        case .month: return month
        case .year: return year
        case .all: return all ?? year
        }
    }
}

struct PeriodStats: Codable, Equatable {
    let startDate: String
    let endDate: String
    let inputTokens: Int
    let outputTokens: Int
    let cacheReadTokens: Int
    let cacheWriteTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let costUsd: Double
    let cacheHitRate: Double
    let activeDays: Int
    let agentSeconds: Double
    let agentHours: Double
    let workdays: Double
    let wallHours: Double
    let leverage: Double
    let runs: Int
    let tools: [UsageToolStats]
    let topModels: [RankedModelStats]?

    enum CodingKeys: String, CodingKey {
        case startDate = "start_date"
        case endDate = "end_date"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case cacheHitRate = "cache_hit_rate"
        case activeDays = "active_days"
        case agentSeconds = "agent_seconds"
        case agentHours = "agent_hours"
        case workdays
        case wallHours = "wall_hours"
        case leverage
        case runs
        case tools
        case topModels = "top_models"
    }
}

struct UsageToolStats: Codable, Equatable, Identifiable {
    var id: String { tool }
    let tool: String
    let displayName: String
    let shortName: String
    let color: String
    let inputTokens: Int
    let outputTokens: Int
    let cacheReadTokens: Int
    let cacheWriteTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let costUsd: Double
    let cacheHitRate: Double?
    let sessions: Int?
    let activeDays: Int
    let agentSeconds: Double
    let agentHours: Double
    let runs: Int
    let shareTokens: Double
    let models: [ModelUsageStats]

    var effectiveCacheHitRate: Double {
        if let r = cacheHitRate { return r }
        let denom = inputTokens + cacheReadTokens + cacheWriteTokens
        return denom > 0 ? Double(cacheReadTokens) / Double(denom) : 0.0
    }

    var effectiveSessions: Int {
        sessions ?? max(runs, activeDays)
    }

    enum CodingKeys: String, CodingKey {
        case tool
        case displayName = "display_name"
        case shortName = "short_name"
        case color
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case cacheHitRate = "cache_hit_rate"
        case sessions
        case activeDays = "active_days"
        case agentSeconds = "agent_seconds"
        case agentHours = "agent_hours"
        case runs
        case shareTokens = "share_tokens"
        case models
    }
}

struct ModelUsageStats: Codable, Equatable, Identifiable {
    var id: String { model }
    let model: String
    let inputTokens: Int
    let outputTokens: Int
    let cacheReadTokens: Int
    let cacheWriteTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let costUsd: Double
    let cacheHitRate: Double?
    let rateIn: Double?
    let rateOut: Double?
    let rateCacheRead: Double?
    let rateCacheWrite: Double?

    var effectiveCacheHitRate: Double {
        if let r = cacheHitRate { return r }
        let denom = inputTokens + cacheReadTokens + cacheWriteTokens
        return denom > 0 ? Double(cacheReadTokens) / Double(denom) : 0.0
    }

    enum CodingKeys: String, CodingKey {
        case model
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case cacheHitRate = "cache_hit_rate"
        case rateIn = "rate_in"
        case rateOut = "rate_out"
        case rateCacheRead = "rate_cache_read"
        case rateCacheWrite = "rate_cache_write"
    }
}

struct RankedModelStats: Codable, Equatable, Identifiable {
    var id: String { "\(tool)::\(model)" }
    let tool: String
    let displayName: String
    let shortName: String
    let color: String
    let model: String
    let inputTokens: Int
    let outputTokens: Int
    let cacheReadTokens: Int
    let cacheWriteTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let costUsd: Double
    let cacheHitRate: Double

    enum CodingKeys: String, CodingKey {
        case tool
        case displayName = "display_name"
        case shortName = "short_name"
        case color
        case model
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case cacheHitRate = "cache_hit_rate"
    }
}

struct HeatmapPayload: Codable, Equatable {
    let days: [HeatmapDay]
    let currentStreak: Int
    let longestStreak: Int
    let totalActiveDays: Int

    enum CodingKeys: String, CodingKey {
        case days
        case currentStreak = "current_streak"
        case longestStreak = "longest_streak"
        case totalActiveDays = "total_active_days"
    }
}

struct HeatmapDay: Codable, Equatable, Identifiable {
    var id: String { date }
    let date: String
    let shortDate: String
    let weekday: String
    let isToday: Bool
    let totalTokens: Int
    let costUsd: Double
    let agentHours: Double
    let sessions: Int
    let tools: [HeatmapDayTool]

    enum CodingKeys: String, CodingKey {
        case date
        case shortDate = "short_date"
        case weekday
        case isToday = "is_today"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case agentHours = "agent_hours"
        case sessions
        case tools
    }
}

struct HeatmapDayTool: Codable, Equatable, Identifiable {
    var id: String { tool }
    let tool: String
    let displayName: String
    let color: String
    let totalTokens: Int
    let costUsd: Double
    let agentHours: Double

    enum CodingKeys: String, CodingKey {
        case tool
        case displayName = "display_name"
        case color
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case agentHours = "agent_hours"
    }
}

struct ProjectTrailItem: Codable, Equatable, Identifiable {
    var id: String { path }
    let path: String
    let name: String
    let displayPath: String
    let lastActiveDate: String
    let group: String
    let inputTokens: Int
    let outputTokens: Int
    let cacheReadTokens: Int
    let cacheWriteTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let costUsd: Double
    let cacheHitRate: Double
    let sessions: Int
    let tools: [ProjectToolBadge]

    enum CodingKeys: String, CodingKey {
        case path, name
        case displayPath = "display_path"
        case lastActiveDate = "last_active_date"
        case group
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case cacheHitRate = "cache_hit_rate"
        case sessions
        case tools
    }
}

struct ProjectToolBadge: Codable, Equatable, Identifiable {
    var id: String { tool }
    let tool: String
    let displayName: String
    let shortName: String
    let color: String

    enum CodingKeys: String, CodingKey {
        case tool
        case displayName = "display_name"
        case shortName = "short_name"
        case color
    }
}

struct HistoryDayPoint: Codable, Equatable, Identifiable {
    var id: String { date }
    let date: String
    let shortDate: String
    let weekday: String
    let isToday: Bool
    let totalTokens: Int
    let costUsd: Double
    let agentHours: Double

    enum CodingKeys: String, CodingKey {
        case date
        case shortDate = "short_date"
        case weekday
        case isToday = "is_today"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case agentHours = "agent_hours"
    }
}

struct LedgerMeta: Codable, Equatable {
    let ledgerPath: String
    let importedTokei: Bool
    let importedToolCount: Int
    let maxToolDays: Int
    let earliestDay: String

    enum CodingKeys: String, CodingKey {
        case ledgerPath = "ledger_path"
        case importedTokei = "imported_tokei"
        case importedToolCount = "imported_tool_count"
        case maxToolDays = "max_tool_days"
        case earliestDay = "earliest_day"
    }
}

// MARK: - Slow-Track Quota Models

struct QuotaSnapshot: Codable, Equatable {
    let schemaVersion: Int
    let generatedAt: String
    let generatedTs: Double
    let providers: [QuotaProvider]
    let highestWindow: HighestQuotaWindow?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case generatedAt = "generated_at"
        case generatedTs = "generated_ts"
        case providers
        case highestWindow = "highest_window"
    }
}

struct QuotaProvider: Codable, Equatable, Identifiable {
    let id: String
    let name: String
    let color: String
    let plan: String
    let status: String
    let statusText: String
    let fetchedAt: Int
    let windows: [QuotaWindow]

    enum CodingKeys: String, CodingKey {
        case id, name, color, plan, status
        case statusText = "status_text"
        case fetchedAt = "fetched_at"
        case windows
    }
}

struct QuotaWindow: Codable, Equatable, Identifiable {
    let id: String
    let label: String
    let shortLabel: String
    let usedPercent: Double
    let remainingPercent: Double
    let resetsAt: Int?
    let windowMinutes: Int?

    enum CodingKeys: String, CodingKey {
        case id, label
        case shortLabel = "short_label"
        case usedPercent = "used_percent"
        case remainingPercent = "remaining_percent"
        case resetsAt = "resets_at"
        case windowMinutes = "window_minutes"
    }
}

struct HighestQuotaWindow: Codable, Equatable {
    let providerId: String
    let providerName: String
    let label: String
    let shortLabel: String
    let usedPercent: Double
    let resetsAt: Int?

    enum CodingKeys: String, CodingKey {
        case providerId = "provider_id"
        case providerName = "provider_name"
        case label
        case shortLabel = "short_label"
        case usedPercent = "used_percent"
        case resetsAt = "resets_at"
    }
}

// MARK: - UI Enums

enum PopoverTab: String, CaseIterable, Identifiable {
    case overview
    case dashboard
    case projects
    case quotas
    case settings

    var id: String { rawValue }

    var title: String {
        switch self {
        case .overview: return "明细"
        case .dashboard: return "大屏"
        case .projects: return "项目"
        case .quotas: return "额度"
        case .settings: return "设置"
        }
    }
}

enum UsagePeriodKey: String, CaseIterable, Identifiable {
    case today
    case yesterday
    case week
    case lastWeek
    case month
    case year
    case all

    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: return "今天"
        case .yesterday: return "昨天"
        case .week: return "本周"
        case .lastWeek: return "上周"
        case .month: return "本月"
        case .year: return "今年"
        case .all: return "全部"
        }
    }
}

enum HistoryMetric: String, CaseIterable, Identifiable {
    case hours
    case tokens
    case cost

    var id: String { rawValue }

    var title: String {
        switch self {
        case .hours: return "工时"
        case .tokens: return "Token"
        case .cost: return "费用"
        }
    }
}

enum WindowLayerMode: String, CaseIterable {
    case desktop
    case floating
    case normal

    var title: String {
        switch self {
        case .desktop: return "贴附桌面层"
        case .floating: return "悬浮置顶"
        case .normal: return "正常窗口"
        }
    }

    var shortLabel: String {
        switch self {
        case .desktop: return "桌面层"
        case .floating: return "置顶"
        case .normal: return "常规"
        }
    }

    var symbolName: String {
        switch self {
        case .desktop: return "rectangle.on.rectangle"
        case .floating: return "pin.fill"
        case .normal: return "macwindow"
        }
    }

    func toggledPrimary() -> WindowLayerMode {
        self == .floating ? .desktop : .floating
    }
}

// MARK: - Formatters

enum Formatters {
    static func heroHours(_ seconds: Double) -> String {
        String(format: "%.2f", seconds / 3600.0)
    }

    static func menuBarHours(_ seconds: Double) -> String {
        let hrs = seconds / 3600.0
        if hrs < 0.1 && seconds >= 60 {
            return "\(Int(round(seconds / 60.0)))m"
        }
        return String(format: "%.1fh", hrs)
    }

    static func humanDurationCN(_ seconds: Double) -> String {
        let s = max(0, Int(round(seconds)))
        if s < 60 {
            return "\(s) 秒"
        }
        let mins = s / 60
        let hrs = mins / 60
        let remMins = mins % 60
        if hrs == 0 {
            return "\(mins) 分钟"
        }
        if remMins == 0 {
            return "\(hrs) 小时"
        }
        return "\(hrs) 小时 \(remMins) 分钟"
    }

    static func shortDuration(_ seconds: Double) -> String {
        if seconds <= 0 {
            return "—"
        }
        if seconds < 60 {
            return "\(Int(round(seconds)))s"
        }
        if seconds < 3600 {
            return "\(Int(round(seconds / 60.0)))m"
        }
        return String(format: "%.2fh", seconds / 3600.0)
    }

    static func liveTimer(_ seconds: Double) -> String {
        let s = max(0, Int(seconds))
        let mins = s / 60
        let secs = s % 60
        return String(format: "%02d:%02d", mins, secs)
    }

    static func compactTokens(_ tokens: Int) -> String {
        if tokens >= 1_000_000_000 {
            return String(format: "%.2fB", Double(tokens) / 1_000_000_000.0)
        }
        if tokens >= 1_000_000 {
            return String(format: "%.2fM", Double(tokens) / 1_000_000.0)
        }
        if tokens >= 1_000 {
            return String(format: "%.0fK", Double(tokens) / 1_000.0)
        }
        return "\(tokens)"
    }

    static func usd(_ amount: Double) -> String {
        if amount >= 1000 {
            return String(format: "$%.0f", amount)
        }
        if amount >= 100 {
            return String(format: "$%.1f", amount)
        }
        return String(format: "$%.2f", amount)
    }

    static func rate(_ amount: Double) -> String {
        if amount == 0 { return "$0" }
        if amount < 0.1 { return String(format: "$%.3f", amount) }
        return String(format: "$%.2f", amount)
    }

    static func shortWeekday(_ weekday: String, isToday: Bool) -> String {
        if isToday { return "今" }
        return weekday.replacingOccurrences(of: "周", with: "")
    }

    static func relativeReset(_ resetsAt: Int?, now: Double = Date().timeIntervalSince1970) -> String {
        guard let target = resetsAt else { return "" }
        let diff = Double(target) - now
        if diff <= 0 { return "即将重置" }
        let mins = Int(diff / 60.0)
        if mins < 60 { return "\(max(1, mins))m 后重置" }
        let hrs = mins / 60
        let remM = mins % 60
        if hrs < 24 {
            return remM > 0 ? "\(hrs)h \(remM)m 后重置" : "\(hrs)h 后重置"
        }
        let days = hrs / 24
        return "\(days) 天后重置"
    }

    static func relativeUpdated(_ epoch: Int, now: Double = Date().timeIntervalSince1970) -> String {
        let diff = max(0, Int(now - Double(epoch)))
        if diff < 60 { return "刚刚" }
        if diff < 3600 { return "\(diff / 60)分钟前" }
        if diff < 86400 { return "\(diff / 3600)小时前" }
        return "\(diff / 86400)天前"
    }

    static func quotaColor(for usedPercent: Double, defaultColor: Color = Color(hex: "#34D399")) -> Color {
        if usedPercent >= 90 { return Color(hex: "#F87171") }
        if usedPercent >= 70 { return Color(hex: "#FBBF24") }
        return defaultColor
    }
}

extension Color {
    init(hex: String) {
        let cleaned = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        var value: UInt64 = 0
        Scanner(string: cleaned).scanHexInt64(&value)
        let r = Double((value >> 16) & 0xFF) / 255.0
        let g = Double((value >> 8) & 0xFF) / 255.0
        let b = Double(value & 0xFF) / 255.0
        self.init(red: r, green: g, blue: b)
    }
}
