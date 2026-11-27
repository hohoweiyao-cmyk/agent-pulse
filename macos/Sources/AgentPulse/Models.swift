import SwiftUI

// MARK: - Main Snapshot (Schema v3, backward-compatible with v0.6 / v2)

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
    let sentinelTasks: [SentinelTask]?
    let collection: CollectionSummary?
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
        case sentinelTasks = "sentinel_tasks"
        case collection
        case tokeiRef = "tokei_ref"
        case usage
        case elapsedMs = "elapsed_ms"
    }
}

// MARK: - Collection Health (v0.7)

enum CollectionHealthStatus: Codable, Equatable {
    case complete
    case partial
    case unavailable
    case notConfigured
    case unknown(String)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = (try? container.decode(String.self)) ?? ""
        switch raw {
        case "complete": self = .complete
        case "partial": self = .partial
        case "unavailable": self = .unavailable
        case "not_configured": self = .notConfigured
        default: self = .unknown(raw)
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .complete: try container.encode("complete")
        case .partial: try container.encode("partial")
        case .unavailable: try container.encode("unavailable")
        case .notConfigured: try container.encode("not_configured")
        case .unknown(let raw): try container.encode(raw)
        }
    }

    var isHealthy: Bool {
        self == .complete || self == .notConfigured
    }

    var isDegraded: Bool {
        self == .partial || self == .unavailable
    }
}

struct CollectionHealth: Codable, Equatable {
    let status: CollectionHealthStatus
    let lastSuccessTs: Double?

    enum CodingKeys: String, CodingKey {
        case status
        case lastSuccessTs = "last_success_ts"
    }
}

struct CollectionSummary: Codable, Equatable {
    let work: CollectionHealth
    let usage: CollectionHealth
    let projects: CollectionHealth
    let sources: [String: CollectionHealth]
}

enum FastFreshness: String, Equatable {
    case fresh
    case degraded
    case stale
    case unavailable
}

// MARK: - Sentinel Tasks (v0.7)

enum SentinelTaskState: Codable, Equatable {
    case running
    case waitingHuman
    case justFinished
    case unknown(String)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = (try? container.decode(String.self)) ?? ""
        switch raw {
        case "running": self = .running
        case "waiting_human": self = .waitingHuman
        case "just_finished": self = .justFinished
        default: self = .unknown(raw)
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .running: try container.encode("running")
        case .waitingHuman: try container.encode("waiting_human")
        case .justFinished: try container.encode("just_finished")
        case .unknown(let raw): try container.encode(raw)
        }
    }

    var sortPriority: Int {
        switch self {
        case .waitingHuman: return 0
        case .running: return 1
        case .justFinished: return 2
        case .unknown: return 3
        }
    }
}

enum SentinelEvidenceKind: Codable, Equatable {
    case heartbeat
    case explicitRunning
    case explicitHumanRequest
    case explicitSuccess
    case unknown(String)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = (try? container.decode(String.self)) ?? ""
        switch raw {
        case "heartbeat": self = .heartbeat
        case "explicit_running": self = .explicitRunning
        case "explicit_human_request": self = .explicitHumanRequest
        case "explicit_success": self = .explicitSuccess
        default: self = .unknown(raw)
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .heartbeat: try container.encode("heartbeat")
        case .explicitRunning: try container.encode("explicit_running")
        case .explicitHumanRequest: try container.encode("explicit_human_request")
        case .explicitSuccess: try container.encode("explicit_success")
        case .unknown(let raw): try container.encode(raw)
        }
    }
}

enum SentinelWaitKind: Codable, Equatable {
    case approval
    case userInput
    case secret
    case other
    case unknown(String)

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = (try? container.decode(String.self)) ?? ""
        switch raw {
        case "approval": self = .approval
        case "user_input": self = .userInput
        case "secret": self = .secret
        case "other": self = .other
        default: self = .unknown(raw)
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .approval: try container.encode("approval")
        case .userInput: try container.encode("user_input")
        case .secret: try container.encode("secret")
        case .other: try container.encode("other")
        case .unknown(let raw): try container.encode(raw)
        }
    }
}

struct SentinelEvidence: Codable, Equatable {
    let kind: SentinelEvidenceKind
    let waitKind: SentinelWaitKind?

    enum CodingKeys: String, CodingKey {
        case kind
        case waitKind = "wait_kind"
    }
}

struct SentinelTask: Codable, Equatable, Identifiable {
    let id: String
    let sourceId: String
    let tool: String
    let displayName: String
    let shortName: String
    let color: String
    let label: String
    let state: SentinelTaskState
    let startedAt: Double
    let stateSinceTs: Double
    let expiresAt: Double
    let elapsedSeconds: Double
    let projectPath: String?
    let workspacePath: String?
    let evidence: SentinelEvidence?

    enum CodingKeys: String, CodingKey {
        case id
        case sourceId = "source_id"
        case tool
        case displayName = "display_name"
        case shortName = "short_name"
        case color
        case label
        case state
        case startedAt = "started_at"
        case stateSinceTs = "state_since_ts"
        case expiresAt = "expires_at"
        case elapsedSeconds = "elapsed_seconds"
        case projectPath = "project_path"
        case workspacePath = "workspace_path"
        case evidence
    }
}

// MARK: - Day & Guardrails Models

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

enum WorkDayStatus: Codable, Equatable {
    case observed
    case retained
    case unknown

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = (try? container.decode(String.self)) ?? ""
        switch raw {
        case "observed": self = .observed
        case "retained": self = .retained
        default: self = .unknown
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .observed: try container.encode("observed")
        case .retained: try container.encode("retained")
        case .unknown: try container.encode("unknown")
        }
    }
}

enum HourlyChartState: Equatable {
    case available
    case unavailable
    case unknown
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
    let hourlyAvailable: Bool?
    let workStatus: WorkDayStatus?
    let tokens: Int?
    let costUsd: Double?
    let unpricedTokens: Int?
    let cacheHitRate: Double?

    /// Returns true if work hours for this day have no trusted observation (must render "—" instead of 0.00h).
    var isWorkUnknown: Bool {
        workStatus == .unknown
    }

    var formattedEstimatedCost: String {
        let tok = tokens ?? 0
        let cost = costUsd ?? 0.0
        let unp = unpricedTokens ?? 0
        let hasObs = tok > 0 || cost > 0 || unp > 0
        return Formatters.estimatedUSD(cost, unpricedTokens: unp, hasObservation: hasObs, totalTokens: tok)
    }

    /// Returns true if work hours come from a retained historical ledger bucket during degraded collection.
    var isWorkRetained: Bool {
        workStatus == .retained
    }

    /// Returns true if 24-hour distribution has a trusted source (compatible with v0.6 native buckets).
    var hasHourlyDistribution: Bool {
        if workStatus == .unknown {
            return false
        }
        if let avail = hourlyAvailable {
            return avail && hourlySeconds.count == 24
        }
        return hourlySeconds.count == 24
    }

    /// Three-state hourly chart availability for WidgetViews / DetailCardsView.
    var hourlyChartState: HourlyChartState {
        if isWorkUnknown {
            return .unknown
        }
        if hasHourlyDistribution {
            return .available
        }
        return .unavailable
    }

    /// Synthesizes an explicit unknown-observation placeholder for a new Shanghai day after midnight.
    static func unknownPlaceholder(forDay dateStr: String, isToday: Bool = true) -> DayStats {
        let shortDate = dateStr.count >= 10
            ? String(dateStr.dropFirst(5)).replacingOccurrences(of: "-", with: "/")
            : dateStr
        let weekday: String = {
            let f = DateFormatter()
            f.calendar = Calendar(identifier: .gregorian)
            f.locale = Locale(identifier: "en_US_POSIX")
            f.timeZone = TimeZone(identifier: "Asia/Shanghai") ?? TimeZone(secondsFromGMT: 8 * 3600)!
            f.dateFormat = "yyyy-MM-dd"
            guard let dt = f.date(from: dateStr) else { return "" }
            var cal = Calendar(identifier: .gregorian)
            cal.timeZone = f.timeZone
            let wd = cal.component(.weekday, from: dt) // 1=Sun ... 7=Sat
            let cn = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]
            return cn[(wd - 1) % 7]
        }()
        return DayStats(
            date: dateStr,
            shortDate: shortDate,
            weekday: weekday,
            isToday: isToday,
            agentSeconds: 0,
            agentHours: 0,
            workdays: 0,
            wallSeconds: 0,
            wallHours: 0,
            leverage: 1.0,
            runs: 0,
            tools: [],
            hourlySeconds: Array(repeating: 0.0, count: 24),
            hourlyAvailable: false,
            workStatus: .unknown,
            tokens: 0,
            costUsd: 0.0,
            unpricedTokens: 0,
            cacheHitRate: 0.0
        )
    }

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
        case hourlyAvailable = "hourly_available"
        case workStatus = "work_status"
        case tokens
        case costUsd = "cost_usd"
        case unpricedTokens = "unpriced_tokens"
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
    let unpricedTokens: Int?

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
        case unpricedTokens = "unpriced_tokens"
    }
}

struct ActiveAgent: Codable, Equatable, Identifiable {
    var id: String { taskId ?? "\(tool)-\(Int(startedAt))" }
    let taskId: String?
    let tool: String
    let displayName: String
    let shortName: String
    let color: String
    let startedAt: Double
    let elapsedSeconds: Double
    let label: String

    enum CodingKeys: String, CodingKey {
        case taskId = "task_id"
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
    let projectTrails: [ProjectTrail]?
    let ledgerMeta: LedgerMeta

    enum CodingKeys: String, CodingKey {
        case source
        case periods
        case history30d = "history_30d"
        case heatmap
        case projects
        case projectTrails = "project_trails"
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
    let unpricedTokens: Int?
    let cacheHitRate: Double
    let activeDays: Int
    let agentSeconds: Double
    let agentHours: Double
    let workdays: Double
    let wallHours: Double
    let leverage: Double
    let runs: Int
    let workStatus: WorkDayStatus?
    let tools: [UsageToolStats]
    let topModels: [RankedModelStats]?

    var isWorkUnknown: Bool {
        workStatus == .unknown
    }

    var formattedEstimatedCost: String {
        let unp = unpricedTokens ?? 0
        let hasObs = totalTokens > 0 || costUsd > 0 || unp > 0
        return Formatters.estimatedUSD(costUsd, unpricedTokens: unp, hasObservation: hasObs, totalTokens: totalTokens)
    }

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
        case unpricedTokens = "unpriced_tokens"
        case cacheHitRate = "cache_hit_rate"
        case activeDays = "active_days"
        case agentSeconds = "agent_seconds"
        case agentHours = "agent_hours"
        case workdays
        case wallHours = "wall_hours"
        case leverage
        case runs
        case workStatus = "work_status"
        case tools
        case topModels = "top_models"
    }
}

struct ToolBilling: Codable, Equatable {
    let subscriptionCandidateUsd: Double?
    let apiActualUsd: Double?

    enum CodingKeys: String, CodingKey {
        case subscriptionCandidateUsd = "subscription_candidate_usd"
        case apiActualUsd = "api_actual_usd"
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
    let unpricedTokens: Int?
    let cacheHitRate: Double?
    let sessions: Int?
    let activeDays: Int
    let agentSeconds: Double
    let agentHours: Double
    let runs: Int
    let shareTokens: Double
    let models: [ModelUsageStats]
    let billing: ToolBilling?

    var effectiveCacheHitRate: Double {
        if let r = cacheHitRate { return r }
        let denom = inputTokens + cacheReadTokens + cacheWriteTokens
        return denom > 0 ? Double(cacheReadTokens) / Double(denom) : 0.0
    }

    var effectiveSessions: Int {
        sessions ?? max(runs, activeDays)
    }

    var formattedEstimatedCost: String {
        let unp = unpricedTokens ?? 0
        let hasObs = totalTokens > 0 || costUsd > 0 || unp > 0
        return Formatters.estimatedUSD(costUsd, unpricedTokens: unp, hasObservation: hasObs, totalTokens: totalTokens)
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
        case unpricedTokens = "unpriced_tokens"
        case cacheHitRate = "cache_hit_rate"
        case sessions
        case activeDays = "active_days"
        case agentSeconds = "agent_seconds"
        case agentHours = "agent_hours"
        case runs
        case shareTokens = "share_tokens"
        case models
        case billing
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
    let unpricedTokens: Int?
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

    var formattedEstimatedCost: String {
        let unp = unpricedTokens ?? 0
        let hasObs = totalTokens > 0 || costUsd > 0 || unp > 0
        return Formatters.estimatedUSD(costUsd, unpricedTokens: unp, hasObservation: hasObs, totalTokens: totalTokens)
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
        case unpricedTokens = "unpriced_tokens"
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
    let unpricedTokens: Int?
    let cacheHitRate: Double

    var effectiveUnpricedTokens: Int { unpricedTokens ?? 0 }
    var effectiveHasCostObservation: Bool {
        totalTokens > 0 || costUsd > 0 || effectiveUnpricedTokens > 0
    }
    var formattedEstimatedCost: String {
        Formatters.estimatedUSD(
            costUsd,
            unpricedTokens: effectiveUnpricedTokens,
            hasObservation: effectiveHasCostObservation,
            totalTokens: totalTokens
        )
    }

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
        case unpricedTokens = "unpriced_tokens"
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
    let unpricedTokens: Int?
    let hasCostObservation: Bool?
    let agentHours: Double
    let workStatus: WorkDayStatus?
    let sessions: Int
    let tools: [HeatmapDayTool]

    var effectiveUnpricedTokens: Int { unpricedTokens ?? 0 }
    var effectiveHasCostObservation: Bool {
        hasCostObservation ?? (totalTokens > 0 || costUsd > 0 || effectiveUnpricedTokens > 0)
    }
    /// True when work hours for this heatmap day have no trusted observation (render "—" instead of 0.00h).
    /// Legacy snapshots lacking `work_status` default to `false` for backward compatibility.
    var isWorkUnknown: Bool {
        workStatus == .unknown
    }
    var isWorkRetained: Bool {
        workStatus == .retained
    }
    var formattedEstimatedCost: String {
        Formatters.estimatedUSD(
            costUsd,
            unpricedTokens: effectiveUnpricedTokens,
            hasObservation: effectiveHasCostObservation,
            totalTokens: totalTokens
        )
    }

    enum CodingKeys: String, CodingKey {
        case date
        case shortDate = "short_date"
        case weekday
        case isToday = "is_today"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case unpricedTokens = "unpriced_tokens"
        case hasCostObservation = "has_cost_observation"
        case agentHours = "agent_hours"
        case workStatus = "work_status"
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
    let unpricedTokens: Int?
    let hasCostObservation: Bool?
    let agentHours: Double

    var effectiveUnpricedTokens: Int { unpricedTokens ?? 0 }
    var effectiveHasCostObservation: Bool {
        hasCostObservation ?? (totalTokens > 0 || costUsd > 0 || effectiveUnpricedTokens > 0)
    }
    var formattedEstimatedCost: String {
        Formatters.estimatedUSD(
            costUsd,
            unpricedTokens: effectiveUnpricedTokens,
            hasObservation: effectiveHasCostObservation,
            totalTokens: totalTokens
        )
    }

    enum CodingKeys: String, CodingKey {
        case tool
        case displayName = "display_name"
        case color
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case unpricedTokens = "unpriced_tokens"
        case hasCostObservation = "has_cost_observation"
        case agentHours = "agent_hours"
    }
}

// MARK: - Project Trail Models (Legacy + v0.7 Worktree-Merged Periods)

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
    let unpricedTokens: Int?
    let cacheHitRate: Double
    let sessions: Int
    let tools: [ProjectToolBadge]

    var effectiveUnpricedTokens: Int { unpricedTokens ?? 0 }

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
        case unpricedTokens = "unpriced_tokens"
        case cacheHitRate = "cache_hit_rate"
        case sessions
        case tools
    }
}

struct WorktreeSource: Codable, Equatable, Identifiable {
    var id: String { path }
    let path: String
    let branch: String?
}

struct ProjectPeriodStats: Codable, Equatable {
    let startDate: String
    let endDate: String
    let lastActiveDate: String
    let agentSeconds: Double?
    let inputTokens: Int
    let outputTokens: Int
    let cacheReadTokens: Int
    let cacheWriteTokens: Int
    let reasoningTokens: Int
    let totalTokens: Int
    let costUsd: Double
    let unpricedTokens: Int?
    let hasCostObservation: Bool?
    let cacheHitRate: Double
    let sessions: Int
    let tools: [ProjectToolBadge]
    let worktreeSources: [WorktreeSource]

    var effectiveUnpricedTokens: Int { unpricedTokens ?? 0 }
    var effectiveHasCostObservation: Bool {
        hasCostObservation ?? (totalTokens > 0 || costUsd > 0 || effectiveUnpricedTokens > 0)
    }
    var formattedEstimatedCost: String {
        Formatters.estimatedUSD(
            costUsd,
            unpricedTokens: effectiveUnpricedTokens,
            hasObservation: effectiveHasCostObservation,
            totalTokens: totalTokens
        )
    }

    init(
        startDate: String,
        endDate: String,
        lastActiveDate: String,
        agentSeconds: Double?,
        inputTokens: Int,
        outputTokens: Int,
        cacheReadTokens: Int,
        cacheWriteTokens: Int,
        reasoningTokens: Int,
        totalTokens: Int,
        costUsd: Double,
        unpricedTokens: Int? = nil,
        hasCostObservation: Bool? = nil,
        cacheHitRate: Double,
        sessions: Int,
        tools: [ProjectToolBadge],
        worktreeSources: [WorktreeSource]
    ) {
        self.startDate = startDate
        self.endDate = endDate
        self.lastActiveDate = lastActiveDate
        self.agentSeconds = agentSeconds
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.cacheReadTokens = cacheReadTokens
        self.cacheWriteTokens = cacheWriteTokens
        self.reasoningTokens = reasoningTokens
        self.totalTokens = totalTokens
        self.costUsd = costUsd
        self.unpricedTokens = unpricedTokens
        self.hasCostObservation = hasCostObservation
        self.cacheHitRate = cacheHitRate
        self.sessions = sessions
        self.tools = tools
        self.worktreeSources = worktreeSources
    }

    enum CodingKeys: String, CodingKey {
        case startDate = "start_date"
        case endDate = "end_date"
        case lastActiveDate = "last_active_date"
        case agentSeconds = "agent_seconds"
        case inputTokens = "input_tokens"
        case outputTokens = "output_tokens"
        case cacheReadTokens = "cache_read_tokens"
        case cacheWriteTokens = "cache_write_tokens"
        case reasoningTokens = "reasoning_tokens"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case unpricedTokens = "unpriced_tokens"
        case hasCostObservation = "has_cost_observation"
        case cacheHitRate = "cache_hit_rate"
        case sessions
        case tools
        case worktreeSources = "worktree_sources"
    }
}

struct ProjectTrail: Codable, Equatable, Identifiable {
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
    let unpricedTokens: Int?
    let cacheHitRate: Double
    let sessions: Int
    let tools: [ProjectToolBadge]
    let periods: [String: ProjectPeriodStats]

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
        case unpricedTokens = "unpriced_tokens"
        case cacheHitRate = "cache_hit_rate"
        case sessions
        case tools
        case periods
    }
}

enum ProjectPeriodKey: String, CaseIterable, Identifiable, Codable {
    case today
    case week
    case month
    case all

    var id: String { rawValue }

    var title: String {
        switch self {
        case .today: return "今日"
        case .week: return "本周"
        case .month: return "本月"
        case .all: return "全部"
        }
    }
}

struct ProjectTrailRow: Equatable, Identifiable {
    var id: String { path }
    let path: String
    let name: String
    let displayPath: String
    let period: ProjectPeriodKey
    let stats: ProjectPeriodStats
    let isPinned: Bool
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
    let unpricedTokens: Int?
    let agentHours: Double
    let workStatus: WorkDayStatus?

    var isWorkUnknown: Bool { workStatus == .unknown }

    enum CodingKeys: String, CodingKey {
        case date
        case shortDate = "short_date"
        case weekday
        case isToday = "is_today"
        case totalTokens = "total_tokens"
        case costUsd = "cost_usd"
        case unpricedTokens = "unpriced_tokens"
        case agentHours = "agent_hours"
        case workStatus = "work_status"
    }
}

struct LedgerMeta: Codable, Equatable {
    let ledgerPath: String
    let importedTokei: Bool
    let importedToolCount: Int
    let maxToolDays: Int
    let earliestDay: String
    let workHistoryFrom: String?
    let projectWorkHistoryFrom: String?
    let skippedInvalidDays: Int?

    enum CodingKeys: String, CodingKey {
        case ledgerPath = "ledger_path"
        case importedTokei = "imported_tokei"
        case importedToolCount = "imported_tool_count"
        case maxToolDays = "max_tool_days"
        case earliestDay = "earliest_day"
        case workHistoryFrom = "work_history_from"
        case projectWorkHistoryFrom = "project_work_history_from"
        case skippedInvalidDays = "skipped_invalid_days"
    }
}

// MARK: - Subscription Payback Row (v0.7)

struct SubscriptionRow: Equatable, Identifiable {
    var id: String { tool }
    let tool: String
    let displayName: String
    let monthlyFeeCents: Int?
    let subscriptionValueUsd: Decimal?
    let paybackMultiple: Decimal?
    let apiActualUsd: Decimal?
}

// MARK: - Slow-Track Quota Models (v1 + v0.7 Freshness & Pace)

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

enum QuotaPaceLevel: Codable, Equatable {
    case onTrack
    case watch
    case risk
    case unknown

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = (try? container.decode(String.self)) ?? ""
        switch raw {
        case "on_track": self = .onTrack
        case "watch": self = .watch
        case "risk": self = .risk
        default: self = .unknown
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .onTrack: try container.encode("on_track")
        case .watch: try container.encode("watch")
        case .risk: try container.encode("risk")
        case .unknown: try container.encode("unknown")
        }
    }
}

enum QuotaPaceReason: Codable, Equatable {
    case stale
    case missingReset
    case unverifiedScope
    case insufficientSamples
    case unknown

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        let raw = (try? container.decode(String.self)) ?? ""
        switch raw {
        case "stale": self = .stale
        case "missing_reset": self = .missingReset
        case "unverified_scope": self = .unverifiedScope
        case "insufficient_samples": self = .insufficientSamples
        default: self = .unknown
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .stale: try container.encode("stale")
        case .missingReset: try container.encode("missing_reset")
        case .unverifiedScope: try container.encode("unverified_scope")
        case .insufficientSamples: try container.encode("insufficient_samples")
        case .unknown: try container.encode("unknown")
        }
    }
}

struct QuotaPace: Codable, Equatable {
    let level: QuotaPaceLevel
    let reason: QuotaPaceReason?
    let sampleCount: Int
    let ratePctPerHour: Double?
    let projectedUsedPercent: Double?
    let estimatedExhaustionTs: Double?

    enum CodingKeys: String, CodingKey {
        case level
        case reason
        case sampleCount = "sample_count"
        case ratePctPerHour = "rate_pct_per_hour"
        case projectedUsedPercent = "projected_used_percent"
        case estimatedExhaustionTs = "estimated_exhaustion_ts"
    }

    var summaryText: String {
        switch level {
        case .onTrack:
            if let rate = ratePctPerHour, rate == 0 {
                return "近期读数持平，配速平稳"
            }
            return "照近期配速，重置前额度充裕"
        case .watch:
            return "照近期配速，重置前将接近上限"
        case .risk:
            return "照近期配速，可能在重置前用尽"
        case .unknown:
            switch reason {
            case .stale: return "读数已过期，暂无法预测配速"
            case .missingReset: return "缺少重置时间，暂无法预测配速"
            case .unverifiedScope: return "当前账号范围未核对，暂不预测配速"
            case .insufficientSamples: return "同一额度周期样本不足"
            case .unknown, .none: return "暂无配速预测"
            }
        }
    }
}

struct QuotaWindow: Codable, Equatable, Identifiable {
    let id: String
    let label: String
    let shortLabel: String
    let usedPercent: Double
    let rawUsedPercent: Double?
    let remainingPercent: Double
    let resetsAt: Int?
    let windowMinutes: Int?
    let freshUntilTs: Double?
    let pace: QuotaPace?

    /// Unrounded percentage for exact threshold & ranking checks (falls back to `usedPercent` on legacy snapshots).
    var effectiveThresholdPercent: Double {
        if let raw = rawUsedPercent, raw.isFinite {
            return raw
        }
        return usedPercent
    }

    /// True only when exact unrounded utilization reaches the 70% alert threshold (`>= 70.0`).
    var isAboveAlertThreshold: Bool {
        effectiveThresholdPercent >= 70.0
    }

    init(
        id: String,
        label: String,
        shortLabel: String,
        usedPercent: Double,
        rawUsedPercent: Double? = nil,
        remainingPercent: Double,
        resetsAt: Int?,
        windowMinutes: Int?,
        freshUntilTs: Double?,
        pace: QuotaPace?
    ) {
        self.id = id
        self.label = label
        self.shortLabel = shortLabel
        self.usedPercent = usedPercent
        self.rawUsedPercent = rawUsedPercent
        self.remainingPercent = remainingPercent
        self.resetsAt = resetsAt
        self.windowMinutes = windowMinutes
        self.freshUntilTs = freshUntilTs
        self.pace = pace
    }

    enum CodingKeys: String, CodingKey {
        case id, label
        case shortLabel = "short_label"
        case usedPercent = "used_percent"
        case rawUsedPercent = "raw_used_percent"
        case remainingPercent = "remaining_percent"
        case resetsAt = "resets_at"
        case windowMinutes = "window_minutes"
        case freshUntilTs = "fresh_until_ts"
        case pace
    }
}

struct HighestQuotaWindow: Codable, Equatable {
    let providerId: String
    let providerName: String
    let label: String
    let shortLabel: String
    let usedPercent: Double
    let rawUsedPercent: Double?
    let resetsAt: Int?

    /// Unrounded percentage for exact threshold & ranking checks (falls back to `usedPercent` on legacy snapshots).
    var effectiveThresholdPercent: Double {
        if let raw = rawUsedPercent, raw.isFinite {
            return raw
        }
        return usedPercent
    }

    /// True only when exact unrounded utilization reaches the 70% alert threshold (`>= 70.0`).
    var isAboveAlertThreshold: Bool {
        effectiveThresholdPercent >= 70.0
    }

    init(
        providerId: String,
        providerName: String,
        label: String,
        shortLabel: String,
        usedPercent: Double,
        rawUsedPercent: Double? = nil,
        resetsAt: Int?
    ) {
        self.providerId = providerId
        self.providerName = providerName
        self.label = label
        self.shortLabel = shortLabel
        self.usedPercent = usedPercent
        self.rawUsedPercent = rawUsedPercent
        self.resetsAt = resetsAt
    }

    enum CodingKeys: String, CodingKey {
        case providerId = "provider_id"
        case providerName = "provider_name"
        case label
        case shortLabel = "short_label"
        case usedPercent = "used_percent"
        case rawUsedPercent = "raw_used_percent"
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

    var cliArgument: String {
        switch self {
        case .lastWeek: return "last_week"
        default: return rawValue
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

    static func usd(_ decimal: Decimal) -> String {
        let val = NSDecimalNumber(decimal: decimal).doubleValue
        return String(format: "$%.2f", val)
    }

    static func paybackMultiple(_ decimal: Decimal) -> String {
        let val = NSDecimalNumber(decimal: decimal).doubleValue
        return String(format: "%.2f×", val)
    }

    static func estimatedUSD(
        _ amount: Double,
        unpricedTokens: Int,
        hasObservation: Bool = true,
        totalTokens: Int? = nil,
        hasPricedCost: Bool? = nil
    ) -> String {
        guard hasObservation else { return "—" }
        guard unpricedTokens > 0 else { return usd(amount) }
        let priced: Bool
        if let explicit = hasPricedCost {
            priced = explicit
        } else {
            priced = amount > 0 || (totalTokens.map { $0 > unpricedTokens } ?? false)
        }
        return priced ? "\(usd(amount))+" : "未计价"
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
