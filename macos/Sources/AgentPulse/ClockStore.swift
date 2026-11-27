import AppKit
import Combine
import Foundation

@MainActor
final class ClockStore: ObservableObject {
    @Published var snapshot: ClockSnapshot?
    @Published var quotaSnapshot: QuotaSnapshot?
    @Published var selectedDate: String? { // nil means today
        didSet {
            onLayoutChanged?()
        }
    }
    @Published var selectedPopoverTab: PopoverTab = .overview
    @Published var selectedPeriod: UsagePeriodKey = .today
    @Published var selectedProjectPeriod: ProjectPeriodKey = .today
    @Published var selectedHistoryMetric: HistoryMetric = .cost

    @Published var isRefreshing: Bool = false
    @Published var isRefreshingQuotas: Bool = false
    @Published var isMigrating: Bool = false
    @Published var migrationMessage: String?
    @Published var lastError: String?
    @Published var quotaError: String?
    @Published var reportError: String?
    @Published var projectActionError: String?
    @Published var nowEpoch: Double = Date().timeIntervalSince1970
    @Published var copiedToast: Bool = false

    @Published var isCompact: Bool {
        didSet {
            defaults.set(isCompact, forKey: Keys.isCompact)
            onLayoutChanged?()
        }
    }

    @Published var windowLayer: WindowLayerMode {
        didSet {
            defaults.set(windowLayer.rawValue, forKey: Keys.windowLayer)
            onWindowLayerChanged?(windowLayer)
        }
    }

    @Published var isCardVisible: Bool {
        didSet {
            defaults.set(isCardVisible, forKey: Keys.isCardVisible)
            onVisibilityChanged?(isCardVisible)
        }
    }

    @Published var menuBarShowCost: Bool {
        didSet {
            defaults.set(menuBarShowCost, forKey: Keys.menuBarShowCost)
            onStatusBarPreferenceChanged?()
        }
    }

    @Published var menuBarShowQuotas: Bool {
        didSet {
            defaults.set(menuBarShowQuotas, forKey: Keys.menuBarShowQuotas)
            onStatusBarPreferenceChanged?()
        }
    }

    @Published var cardShowQuotaAlert: Bool {
        didSet {
            defaults.set(cardShowQuotaAlert, forKey: Keys.cardShowQuotaAlert)
            onLayoutChanged?()
        }
    }

    @Published var launchAtLogin: Bool = false
    @Published var allowLiveCodexQuota: Bool {
        didSet {
            defaults.set(allowLiveCodexQuota, forKey: Keys.allowLiveCodexQuota)
            quotaSnapshot = nil
            quotaError = nil
            refreshQuotas()
        }
    }
    @Published var pinnedProjectPaths: Set<String> = []
    @Published private(set) var subscriptionFeesUSDCents: [String: Int] = [:]
    @Published var copiedToolId: String?

    let sitReminder = SitReminderManager()

    var onSnapshotUpdated: ((ClockSnapshot) -> Void)?
    var onQuotaUpdated: ((QuotaSnapshot) -> Void)?
    var onStatusBarPreferenceChanged: (() -> Void)?
    var onWindowLayerChanged: ((WindowLayerMode) -> Void)?
    var onVisibilityChanged: ((Bool) -> Void)?
    var onLayoutChanged: (() -> Void)?
    var onOpenPopoverTab: ((PopoverTab) -> Void)?

    private let defaults: UserDefaults
    private var refreshTimer: Timer?
    private var quotaTimer: Timer?
    private var tickTimer: Timer?
    private var toastTask: Task<Void, Never>?
    private let workQueue = DispatchQueue(label: "dev.agentpulse.app.collector", qos: .utility)
    private let quotaQueue = DispatchQueue(label: "dev.agentpulse.app.quota", qos: .utility)

    private var fastRequestSeq: UInt64 = 0
    private var quotaRequestSeq: UInt64 = 0
    private var lastObservedFreshness: FastFreshness = .unavailable
    private var lastObservedSentinelIds: [String] = []
    private var lastObservedQuotaBadge: String?
    private var lastObservedHighestQuota: HighestQuotaWindow?
    private var lastObservedShanghaiDay: String = ""
    private var lastObservedShanghaiMonth: String = ""

    // Injectable hooks for deterministic unit testing
    var commandExecutor: ((String, URL, [String], Double) -> Result<Data, Error>)?
    var pasteboardWriter: ((String) -> Void)?
    var finderRevealer: (([URL]) -> Void)?

    enum Keys {
        static let isCompact = "agentPulse.isCompact"
        static let windowLayer = "agentPulse.windowLayer"
        static let isCardVisible = "agentPulse.isCardVisible"
        static let menuBarShowCost = "agentPulse.menuBar.showCost"
        static let menuBarShowQuotas = "agentPulse.menuBar.showQuotas"
        static let cardShowQuotaAlert = "agentPulse.card.showQuotaAlert"
        static let pinnedProjects = "agentPulse.projects.pinned"
        static let allowLiveCodexQuota = "agentPulse.quota.allowLiveCodex"
        static let subscriptionFeesUSDCents = "agentPulse.subscriptionFeesUSDCents.v1"
    }

    static let launchAgentLabel = "dev.agentpulse.app"
    static var launchAgentURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(launchAgentLabel).plist")
    }

    static let fastStaleThresholdSeconds: Double = 45.0
    static let codexQuotaDisplayTTL: Double = 360.0
    static let claudeQuotaDisplayTTL: Double = 3600.0
    static let subscriptionFeeRange = 1...1_000_000

    private static let shanghaiTimeZone = TimeZone(identifier: "Asia/Shanghai") ?? TimeZone(secondsFromGMT: 8 * 3600)!

    private static let subscriptionDisplayNames: [String: String] = [
        "codex": "Codex",
        "claude_code": "Claude Code",
        "antigravity": "Antigravity",
        "hermes": "Hermes",
        "workbuddy": "WorkBuddy",
        "codebuddy": "CodeBuddy",
        "pi": "Pi",
        "opencode": "OpenCode",
        "gemini": "Gemini CLI",
        "zcode": "ZCode",
    ]

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        self.isCompact = defaults.object(forKey: Keys.isCompact) as? Bool ?? false
        if let raw = defaults.string(forKey: Keys.windowLayer),
           let mode = WindowLayerMode(rawValue: raw) {
            self.windowLayer = mode
        } else {
            self.windowLayer = .floating
        }
        self.isCardVisible = defaults.object(forKey: Keys.isCardVisible) as? Bool ?? true
        self.menuBarShowCost = defaults.object(forKey: Keys.menuBarShowCost) as? Bool ?? true
        self.menuBarShowQuotas = defaults.object(forKey: Keys.menuBarShowQuotas) as? Bool ?? true
        self.cardShowQuotaAlert = defaults.object(forKey: Keys.cardShowQuotaAlert) as? Bool ?? false
        self.allowLiveCodexQuota = defaults.bool(forKey: Keys.allowLiveCodexQuota)
        self.launchAtLogin = FileManager.default.fileExists(atPath: Self.launchAgentURL.path)
        let savedPins = defaults.stringArray(forKey: Keys.pinnedProjects) ?? []
        self.pinnedProjectPaths = Set(savedPins)
        self.subscriptionFeesUSDCents = Self.loadValidatedSubscriptionFees(from: defaults)
        let initDay = Self.shanghaiDayString(for: self.nowEpoch)
        self.lastObservedShanghaiDay = initDay
        self.lastObservedShanghaiMonth = String(initDay.prefix(7))
    }

    // MARK: - Shanghai Date Helpers

    private static func shanghaiCalendar() -> Calendar {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = shanghaiTimeZone
        return cal
    }

    static func shanghaiDayString(for epoch: Double) -> String {
        let formatter = DateFormatter()
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = shanghaiTimeZone
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: Date(timeIntervalSince1970: epoch))
    }

    static func shanghaiMonthString(for epoch: Double) -> String {
        String(shanghaiDayString(for: epoch).prefix(7))
    }

    static func shanghaiYearString(for epoch: Double) -> String {
        String(shanghaiDayString(for: epoch).prefix(4))
    }

    static func shanghaiDayString(addingDays delta: Int, to epoch: Double) -> String {
        let cal = shanghaiCalendar()
        let base = cal.startOfDay(for: Date(timeIntervalSince1970: epoch))
        let target = cal.date(byAdding: .day, value: delta, to: base) ?? base
        return shanghaiDayString(for: target.timeIntervalSince1970)
    }

    /// Returns the Monday (YYYY-MM-DD) of the ISO week containing `epoch` in Asia/Shanghai.
    static func shanghaiWeekStartString(for epoch: Double) -> String {
        let cal = shanghaiCalendar()
        let startOfDay = cal.startOfDay(for: Date(timeIntervalSince1970: epoch))
        let weekday = cal.component(.weekday, from: startOfDay) // 1=Sun, 2=Mon ... 7=Sat
        let daysFromMonday = (weekday + 5) % 7
        let monday = cal.date(byAdding: .day, value: -daysFromMonday, to: startOfDay) ?? startOfDay
        return shanghaiDayString(for: monday.timeIntervalSince1970)
    }

    var currentShanghaiDay: String {
        Self.shanghaiDayString(for: nowEpoch)
    }

    var currentShanghaiMonth: String {
        Self.shanghaiMonthString(for: nowEpoch)
    }

    var currentShanghaiYear: String {
        Self.shanghaiYearString(for: nowEpoch)
    }

    var currentShanghaiWeekStart: String {
        Self.shanghaiWeekStartString(for: nowEpoch)
    }

    static func canonicalSubscriptionGroup(for rawTool: String) -> String {
        let trimmed = rawTool.trimmingCharacters(in: .whitespacesAndNewlines)
        switch trimmed {
        case "claude", "t3_claude": return "claude_code"
        case "t3_codex": return "codex"
        case "t3_antigravity": return "antigravity"
        default: return trimmed
        }
    }

    private static func loadValidatedSubscriptionFees(from defaults: UserDefaults) -> [String: Int] {
        guard let rawDict = defaults.dictionary(forKey: Keys.subscriptionFeesUSDCents) else {
            return [:]
        }
        var clean: [String: Int] = [:]
        for (rawKey, rawVal) in rawDict {
            let group = canonicalSubscriptionGroup(for: rawKey)
            guard !group.isEmpty else { continue }
            guard let num = rawVal as? NSNumber,
                  CFGetTypeID(num) != CFBooleanGetTypeID(),
                  num.doubleValue.rounded() == num.doubleValue else {
                continue
            }
            let cents = num.intValue
            guard subscriptionFeeRange.contains(cents) else { continue }
            clean[group] = cents
        }
        return clean
    }

    // MARK: - Fast Track Freshness & Frozen Sentinel Accessors (Phase B Contract)

    var fastFreshness: FastFreshness {
        guard let snap = snapshot else { return .unavailable }
        if lastError != nil {
            return .stale
        }
        if (nowEpoch - snap.generatedTs) >= Self.fastStaleThresholdSeconds {
            return .stale
        }
        if Self.shanghaiDayString(for: nowEpoch) != snap.today.date {
            return .stale
        }
        if let col = snap.collection {
            if col.work.status.isDegraded || col.usage.status.isDegraded || col.projects.status.isDegraded {
                return .degraded
            }
        }
        return .fresh
    }

    var sentinelTasks: [SentinelTask] {
        guard let snap = snapshot else { return [] }
        let freshness = fastFreshness
        guard freshness == .fresh || freshness == .degraded else { return [] }

        if let tasks = snap.sentinelTasks {
            let valid = tasks.filter { task in
                guard nowEpoch < task.expiresAt else { return false }
                if case .unknown = task.state { return false }
                return true
            }
            return valid.sorted { lhs, rhs in
                if lhs.state.sortPriority != rhs.state.sortPriority {
                    return lhs.state.sortPriority < rhs.state.sortPriority
                }
                if lhs.stateSinceTs != rhs.stateSinceTs {
                    return lhs.stateSinceTs > rhs.stateSinceTs
                }
                return lhs.id < rhs.id
            }
        }

        // Backward compatibility with v0.6 snapshots lacking sentinel_tasks:
        // synthesize transient running rows from active_agents while fresh.
        let exp = snap.generatedTs + Self.fastStaleThresholdSeconds
        guard nowEpoch < exp else { return [] }
        return snap.activeAgents.map { agent in
            SentinelTask(
                id: agent.taskId ?? agent.id,
                sourceId: "work.\(agent.tool)",
                tool: agent.tool,
                displayName: agent.displayName,
                shortName: agent.shortName,
                color: agent.color,
                label: agent.label,
                state: .running,
                startedAt: agent.startedAt,
                stateSinceTs: agent.startedAt,
                expiresAt: exp,
                elapsedSeconds: agent.elapsedSeconds,
                projectPath: nil,
                workspacePath: nil,
                evidence: SentinelEvidence(kind: .heartbeat, waitKind: nil)
            )
        }
    }

    var todayRunningCount: Int {
        sentinelTasks.filter { $0.state == .running }.count
    }

    var displayedDay: DayStats? {
        guard let snap = snapshot else { return nil }
        if isViewingToday {
            if isCrossedMidnightFromSnapshot {
                return DayStats.unknownPlaceholder(forDay: currentShanghaiDay, isToday: true)
            }
            return snap.today
        }
        if let target = selectedDate,
           let matched = snap.days.first(where: { $0.date == target }) {
            return matched
        }
        if isCrossedMidnightFromSnapshot {
            return DayStats.unknownPlaceholder(forDay: currentShanghaiDay, isToday: true)
        }
        return snap.today
    }

    var isViewingToday: Bool {
        selectedDate == nil || selectedDate == currentShanghaiDay
    }

    var isCrossedMidnightFromSnapshot: Bool {
        guard let snap = snapshot else { return false }
        return currentShanghaiDay != snap.today.date
    }

    var isTodayWorkUnknown: Bool {
        guard let snap = snapshot else { return true }
        if isCrossedMidnightFromSnapshot {
            return true
        }
        return snap.today.isWorkUnknown
    }

    var isDisplayedDayWorkUnknown: Bool {
        if isViewingToday {
            return isTodayWorkUnknown
        }
        return displayedDay?.isWorkUnknown ?? true
    }

    var displayedHourlyChartState: HourlyChartState {
        displayedDay?.hourlyChartState ?? .unknown
    }

    var liveBonusSeconds: Double {
        guard let snap = snapshot, fastFreshness == .fresh else { return 0 }
        let running = todayRunningCount
        guard running > 0 else { return 0 }
        let delta = max(0, min(Self.fastStaleThresholdSeconds, nowEpoch - snap.generatedTs))
        return delta * Double(running)
    }

    var displayedAgentSeconds: Double {
        guard let day = displayedDay else { return 0 }
        if isViewingToday && isCrossedMidnightFromSnapshot {
            return 0
        }
        return day.agentSeconds + (isViewingToday ? liveBonusSeconds : 0)
    }

    /// Returns nil when the displayed day has no trusted work observation (`workStatus == .unknown`).
    var displayedAgentSecondsIfKnown: Double? {
        isDisplayedDayWorkUnknown ? nil : displayedAgentSeconds
    }

    var todayLiveSeconds: Double {
        guard let snap = snapshot else { return 0 }
        if isCrossedMidnightFromSnapshot {
            return 0
        }
        return snap.today.agentSeconds + liveBonusSeconds
    }

    /// Returns nil when today has no trusted work observation (`workStatus == .unknown` or crossed midnight).
    var todayLiveSecondsIfKnown: Double? {
        isTodayWorkUnknown ? nil : todayLiveSeconds
    }

    var displayedHeroHoursText: String {
        guard let secs = displayedAgentSecondsIfKnown else { return "—" }
        return Formatters.heroHours(secs)
    }

    var todayMenuBarHoursText: String {
        guard let secs = todayLiveSecondsIfKnown else { return "—" }
        return Formatters.menuBarHours(secs)
    }

    // MARK: - Clock-Validated Period Projections (Frozen Contract for DetailCardsView / DashboardViews)

    /// Returns validated `PeriodStats` strictly covering the current Shanghai calendar period for `nowEpoch`.
    /// Prevents views from displaying yesterday's/last-week's/last-month's totals after a day/month rollover.
    func periodStats(for period: UsagePeriodKey) -> PeriodStats? {
        guard let snap = snapshot, let periods = snap.usage?.periods else { return nil }
        let curDay = currentShanghaiDay
        let curMonth = currentShanghaiMonth
        let curYear = currentShanghaiYear
        let curWeekStart = currentShanghaiWeekStart

        switch period {
        case .today:
            guard snap.today.date == curDay,
                  periods.today.startDate == curDay,
                  periods.today.endDate == curDay else {
                return nil
            }
            return periods.today
        case .yesterday:
            let curYesterday = Self.shanghaiDayString(addingDays: -1, to: nowEpoch)
            if let y = periods.yesterday, y.endDate == curYesterday {
                return y
            }
            if periods.today.endDate == curYesterday {
                return periods.today
            }
            return nil
        case .week:
            guard periods.week.startDate == curWeekStart else { return nil }
            return periods.week
        case .lastWeek:
            let prevWeekStart = Self.shanghaiWeekStartString(for: nowEpoch - 7 * 86400)
            if let lw = periods.lastWeek, lw.startDate == prevWeekStart {
                return lw
            }
            if periods.week.startDate == prevWeekStart {
                return periods.week
            }
            return nil
        case .month:
            guard String(periods.month.startDate.prefix(7)) == curMonth else { return nil }
            return periods.month
        case .year:
            guard String(periods.year.startDate.prefix(4)) == curYear else { return nil }
            return periods.year
        case .all:
            return periods.all ?? periods.year
        }
    }

    /// Returns clock-validated `DayStats` for 24h pulse charts in `DetailCardsView` (today / yesterday only).
    func hourlyDay(for period: UsagePeriodKey) -> DayStats? {
        guard let snap = snapshot else { return nil }
        switch period {
        case .today:
            if isCrossedMidnightFromSnapshot {
                return DayStats.unknownPlaceholder(forDay: currentShanghaiDay, isToday: true)
            }
            return snap.today
        case .yesterday:
            let curYesterday = Self.shanghaiDayString(addingDays: -1, to: nowEpoch)
            return snap.days.first(where: { $0.date == curYesterday })
        default:
            return nil
        }
    }

    var todayTokens: Int {
        guard !isCrossedMidnightFromSnapshot else { return 0 }
        if let u = snapshot?.usage?.periods.today.totalTokens { return u }
        if let t = snapshot?.today.tokens { return t }
        return snapshot?.tokeiRef.todayTokens ?? 0
    }

    var todayCostUsd: Double {
        guard !isCrossedMidnightFromSnapshot else { return 0.0 }
        if let c = snapshot?.usage?.periods.today.costUsd { return c }
        if let c = snapshot?.today.costUsd { return c }
        return snapshot?.tokeiRef.todayCostUsd ?? 0.0
    }

    var todayUnpricedTokens: Int {
        guard !isCrossedMidnightFromSnapshot else { return 0 }
        if let u = snapshot?.usage?.periods.today.unpricedTokens { return u }
        return snapshot?.today.unpricedTokens ?? 0
    }

    var todayHasCostObservation: Bool {
        guard snapshot != nil, !isCrossedMidnightFromSnapshot else { return false }
        return todayTokens > 0 || todayCostUsd > 0 || todayUnpricedTokens > 0
    }

    var displayedDayTokens: Int {
        if isViewingToday { return todayTokens }
        return displayedDay?.tokens ?? 0
    }

    var displayedDayCostUsd: Double {
        if isViewingToday { return todayCostUsd }
        return displayedDay?.costUsd ?? 0.0
    }

    var displayedDayUnpricedTokens: Int {
        if isViewingToday { return todayUnpricedTokens }
        return displayedDay?.unpricedTokens ?? 0
    }

    var displayedDayHasCostObservation: Bool {
        if isViewingToday { return todayHasCostObservation }
        guard let day = displayedDay else { return false }
        return (day.tokens ?? 0) > 0 || (day.costUsd ?? 0) > 0 || (day.unpricedTokens ?? 0) > 0
    }

    var lastTrustedSuccessTimestamp: Double? {
        guard let snap = snapshot else { return nil }
        if let workTs = snap.collection?.work.lastSuccessTs {
            return workTs
        }
        return snap.generatedTs
    }

    var lastUpdatedTimeString: String {
        guard let ts = lastTrustedSuccessTimestamp else { return "--:--" }
        let dt = Date(timeIntervalSince1970: ts)
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        f.timeZone = Self.shanghaiTimeZone
        return f.string(from: dt)
    }

    // MARK: - Quota Freshness & Dynamic Highest Window (Phase B Contract)

    func quotaWindowIsFresh(provider: QuotaProvider, window: QuotaWindow) -> Bool {
        guard quotaError == nil else { return false }
        guard provider.status == "ok", provider.fetchedAt > 0 else { return false }
        // Reject future observation timestamps (both vs current wall clock and vs snapshot generatedTs)
        guard Double(provider.fetchedAt) <= nowEpoch else { return false }
        if let genTs = quotaSnapshot?.generatedTs, Double(provider.fetchedAt) > genTs {
            return false
        }
        guard window.usedPercent.isFinite, window.usedPercent >= 0, window.usedPercent <= 100 else {
            return false
        }
        if let resetsAt = window.resetsAt, Double(resetsAt) <= nowEpoch {
            return false
        }
        if let freshUntil = window.freshUntilTs {
            guard freshUntil.isFinite else { return false }
            return nowEpoch < freshUntil
        }
        // Legacy v0.6 payload compatibility: apply the same TTL strategy
        let ttl: Double
        switch provider.id {
        case "codex": ttl = Self.codexQuotaDisplayTTL
        case "claude": ttl = Self.claudeQuotaDisplayTTL
        default: return false
        }
        var limit = Double(provider.fetchedAt) + ttl
        if let resetsAt = window.resetsAt {
            limit = min(limit, Double(resetsAt))
        }
        return nowEpoch < limit
    }

    var highestFreshQuotaWindow: HighestQuotaWindow? {
        guard quotaError == nil, let q = quotaSnapshot else { return nil }
        var candidates: [(used: Double, pid: String, wid: String, item: HighestQuotaWindow)] = []
        for provider in q.providers {
            for window in provider.windows {
                guard quotaWindowIsFresh(provider: provider, window: window) else { continue }
                let item = HighestQuotaWindow(
                    providerId: provider.id,
                    providerName: provider.name,
                    label: window.label,
                    shortLabel: window.shortLabel,
                    usedPercent: window.usedPercent,
                    resetsAt: window.resetsAt
                )
                candidates.append((window.usedPercent, provider.id, window.id, item))
            }
        }
        candidates.sort { lhs, rhs in
            if lhs.used != rhs.used { return lhs.used > rhs.used }
            if lhs.pid != rhs.pid { return lhs.pid < rhs.pid }
            return lhs.wid < rhs.wid
        }
        return candidates.first?.item
    }

    /// Compact quota badges for the macOS menu bar (fresh readings only).
    var menuBarQuotaSummary: String? {
        guard menuBarShowQuotas, quotaError == nil, let q = quotaSnapshot else { return nil }
        var parts: [String] = []
        if let codex = q.providers.first(where: { $0.id == "codex" }) {
            let freshWins = codex.windows.filter { quotaWindowIsFresh(provider: codex, window: $0) }
            if let w5 = freshWins.first(where: { $0.id == "codex_5h" }) ?? freshWins.first {
                parts.append("\(w5.shortLabel) \(Int(round(w5.usedPercent)))%")
            }
        }
        if let claude = q.providers.first(where: { $0.id == "claude" }) {
            let freshWins = claude.windows.filter { quotaWindowIsFresh(provider: claude, window: $0) }
            if let w7 = freshWins.first(where: { $0.id == "claude_7d" }) ?? freshWins.first {
                parts.append("\(w7.shortLabel) \(Int(round(w7.usedPercent)))%")
            }
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    // MARK: - Project Trails & Pin Alias Merging (Phase B Contract)

    private func aliasPaths(for trail: ProjectTrail) -> Set<String> {
        var paths: Set<String> = [trail.path]
        for periodStats in trail.periods.values {
            for src in periodStats.worktreeSources {
                paths.insert(src.path)
            }
        }
        return paths
    }

    private func isTrailPinned(_ trail: ProjectTrail) -> Bool {
        !pinnedProjectPaths.isDisjoint(with: aliasPaths(for: trail))
    }

    var isLegacyProjectFallbackOnlyAll: Bool {
        guard let usage = snapshot?.usage else { return false }
        return usage.projectTrails == nil && !(usage.projects?.isEmpty ?? true)
    }

    private func projectPeriodMatchesCurrentClock(_ stats: ProjectPeriodStats, for period: ProjectPeriodKey, snapshotToday: String) -> Bool {
        switch period {
        case .today:
            return snapshotToday == currentShanghaiDay
                && stats.startDate == currentShanghaiDay
                && stats.endDate == currentShanghaiDay
        case .week:
            return stats.startDate == currentShanghaiWeekStart
        case .month:
            return String(stats.startDate.prefix(7)) == currentShanghaiMonth
        case .all:
            return true
        }
    }

    func projectTrailRows(for period: ProjectPeriodKey) -> [ProjectTrailRow] {
        guard let snap = snapshot, let usage = snap.usage else { return [] }

        if let trails = usage.projectTrails {
            var rows: [ProjectTrailRow] = []
            for trail in trails {
                guard let pStats = trail.periods[period.rawValue] else { continue }
                guard projectPeriodMatchesCurrentClock(pStats, for: period, snapshotToday: snap.today.date) else {
                    continue
                }
                rows.append(
                    ProjectTrailRow(
                        path: trail.path,
                        name: trail.name,
                        displayPath: trail.displayPath,
                        period: period,
                        stats: pStats,
                        isPinned: isTrailPinned(trail)
                    )
                )
            }
            return rows.sorted { lhs, rhs in
                if lhs.isPinned != rhs.isPinned { return lhs.isPinned }
                let lSec = lhs.stats.agentSeconds ?? -1.0
                let rSec = rhs.stats.agentSeconds ?? -1.0
                if lSec != rSec { return lSec > rSec }
                if lhs.stats.lastActiveDate != rhs.stats.lastActiveDate {
                    return lhs.stats.lastActiveDate > rhs.stats.lastActiveDate
                }
                return lhs.path < rhs.path
            }
        }

        // Legacy v0.6 fallback: `usage.projects` stores multi-day cumulative totals ONLY,
        // so it must NEVER masquerade as `.today`, `.week`, or `.month` spend — project `.all` only.
        guard period == .all, let legacy = usage.projects else { return [] }
        var rows: [ProjectTrailRow] = []
        for item in legacy {
            let pStats = ProjectPeriodStats(
                startDate: item.lastActiveDate,
                endDate: item.lastActiveDate,
                lastActiveDate: item.lastActiveDate,
                agentSeconds: nil,
                inputTokens: item.inputTokens,
                outputTokens: item.outputTokens,
                cacheReadTokens: item.cacheReadTokens,
                cacheWriteTokens: item.cacheWriteTokens,
                reasoningTokens: item.reasoningTokens,
                totalTokens: item.totalTokens,
                costUsd: item.costUsd,
                unpricedTokens: item.unpricedTokens,
                hasCostObservation: item.totalTokens > 0 || item.costUsd > 0 || (item.unpricedTokens ?? 0) > 0,
                cacheHitRate: item.cacheHitRate,
                sessions: item.sessions,
                tools: item.tools,
                worktreeSources: [WorktreeSource(path: item.path, branch: nil)]
            )
            rows.append(
                ProjectTrailRow(
                    path: item.path,
                    name: item.name,
                    displayPath: item.displayPath,
                    period: period,
                    stats: pStats,
                    isPinned: pinnedProjectPaths.contains(item.path)
                )
            )
        }
        return rows.sorted { lhs, rhs in
            if lhs.isPinned != rhs.isPinned { return lhs.isPinned }
            if lhs.stats.lastActiveDate != rhs.stats.lastActiveDate {
                return lhs.stats.lastActiveDate > rhs.stats.lastActiveDate
            }
            return lhs.path < rhs.path
        }
    }

    func togglePinProject(_ path: String) {
        let trails = snapshot?.usage?.projectTrails ?? []
        if let matched = trails.first(where: { aliasPaths(for: $0).contains(path) }) {
            let aliases = aliasPaths(for: matched)
            if !pinnedProjectPaths.isDisjoint(with: aliases) {
                pinnedProjectPaths.subtract(aliases)
            } else {
                pinnedProjectPaths.insert(matched.path)
            }
        } else {
            if pinnedProjectPaths.contains(path) {
                pinnedProjectPaths.remove(path)
            } else {
                pinnedProjectPaths.insert(path)
            }
        }
        defaults.set(Array(pinnedProjectPaths), forKey: Keys.pinnedProjects)
    }

    // MARK: - Subscription Payback (Phase B Contract)

    @discardableResult
    func setSubscriptionFee(cents: Int?, for group: String) -> Bool {
        let canonical = Self.canonicalSubscriptionGroup(for: group)
        guard !canonical.isEmpty else { return false }

        if let value = cents {
            guard Self.subscriptionFeeRange.contains(value) else { return false }
            subscriptionFeesUSDCents[canonical] = value
        } else {
            subscriptionFeesUSDCents.removeValue(forKey: canonical)
        }
        defaults.set(subscriptionFeesUSDCents, forKey: Keys.subscriptionFeesUSDCents)
        return true
    }

    /// True only when the loaded snapshot's month period actually covers the current Shanghai calendar month.
    var currentMonthSubscriptionDataAvailable: Bool {
        guard let snap = snapshot, let monthPeriod = snap.usage?.periods.month else { return false }
        return String(snap.today.date.prefix(7)) == currentShanghaiMonth
            && String(monthPeriod.startDate.prefix(7)) == currentShanghaiMonth
    }

    var subscriptionRows: [SubscriptionRow] {
        var subCandidates: [String: Decimal] = [:]
        var apiActuals: [String: Decimal] = [:]
        var displayNames: [String: String] = Self.subscriptionDisplayNames

        if currentMonthSubscriptionDataAvailable,
           let monthTools = snapshot?.usage?.periods.month.tools {
            for toolStats in monthTools {
                let group = Self.canonicalSubscriptionGroup(for: toolStats.tool)
                if displayNames[group] == nil {
                    displayNames[group] = toolStats.displayName
                }
                if let billing = toolStats.billing {
                    if let cand = billing.subscriptionCandidateUsd, cand.isFinite, cand >= 0 {
                        let dec = Decimal(string: String(format: "%.6f", cand)) ?? Decimal(cand)
                        subCandidates[group, default: Decimal(0)] += dec
                    }
                    if let actual = billing.apiActualUsd, actual.isFinite, actual >= 0 {
                        let dec = Decimal(string: String(format: "%.6f", actual)) ?? Decimal(actual)
                        apiActuals[group, default: Decimal(0)] += dec
                    }
                }
            }
        }

        let allGroups = Set(subCandidates.keys)
            .union(apiActuals.keys)
            .union(subscriptionFeesUSDCents.keys)

        var rows: [SubscriptionRow] = []
        for group in allGroups {
            let feeCents = subscriptionFeesUSDCents[group]
            let subValue = subCandidates[group]
            let apiActual = apiActuals[group]

            let multiple: Decimal?
            if let fee = feeCents, fee > 0, let val = subValue {
                let monthlyFeeUSD = Decimal(fee) / Decimal(100)
                multiple = monthlyFeeUSD > 0 ? (val / monthlyFeeUSD) : nil
            } else {
                multiple = nil
            }

            rows.append(
                SubscriptionRow(
                    tool: group,
                    displayName: displayNames[group] ?? group,
                    monthlyFeeCents: feeCents,
                    subscriptionValueUsd: subValue,
                    paybackMultiple: multiple,
                    apiActualUsd: apiActual
                )
            )
        }

        return rows.sorted { lhs, rhs in
            let lConfigured = lhs.monthlyFeeCents != nil
            let rConfigured = rhs.monthlyFeeCents != nil
            if lConfigured != rConfigured { return lConfigured }
            let lVal = lhs.subscriptionValueUsd ?? lhs.apiActualUsd ?? Decimal(-1)
            let rVal = rhs.subscriptionValueUsd ?? rhs.apiActualUsd ?? Decimal(-1)
            if lVal != rVal { return lVal > rVal }
            return lhs.tool < rhs.tool
        }
    }

    // MARK: - Timers & Lifecycle

    func start() {
        refresh()
        refreshQuotas(force: false)

        // Fast track: every 15s
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 15.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refresh()
            }
        }

        // Slow track: every 300s
        quotaTimer?.invalidate()
        quotaTimer = Timer.scheduledTimer(withTimeInterval: 300.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshQuotas(force: false)
            }
        }

        // 1s in-memory UI tick: handles live timer, 45s stale boundary, 120s just_finished expiry,
        // Shanghai midnight crossing, and quota fresh_until_ts/resets_at transitions.
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.sitReminder.tickOneSecond()
                self.handleClockTick(Date().timeIntervalSince1970)
            }
        }
    }

    func handleClockTick(_ epoch: Double) {
        nowEpoch = epoch
        let curDay = currentShanghaiDay
        let curMonth = currentShanghaiMonth
        let dayOrMonthChanged = (curDay != lastObservedShanghaiDay) || (curMonth != lastObservedShanghaiMonth)

        let curFreshness = fastFreshness
        let curSentinelIds = sentinelTasks.map { "\($0.id):\($0.state)" }
        let curQuotaBadge = menuBarQuotaSummary
        let curHighest = highestFreshQuotaWindow

        let layoutChanged = dayOrMonthChanged
            || (curFreshness != lastObservedFreshness)
            || (curSentinelIds != lastObservedSentinelIds)
            || (curHighest != lastObservedHighestQuota)
        let statusBarChanged = layoutChanged
            || (curQuotaBadge != lastObservedQuotaBadge)
            || (curFreshness == .fresh && todayRunningCount > 0)

        lastObservedShanghaiDay = curDay
        lastObservedShanghaiMonth = curMonth
        lastObservedFreshness = curFreshness
        lastObservedSentinelIds = curSentinelIds
        lastObservedQuotaBadge = curQuotaBadge
        lastObservedHighestQuota = curHighest

        if layoutChanged {
            onLayoutChanged?()
        }
        if statusBarChanged {
            onStatusBarPreferenceChanged?()
        }
    }

    func stop() {
        refreshTimer?.invalidate()
        quotaTimer?.invalidate()
        tickTimer?.invalidate()
    }

    func openPopover(tab: PopoverTab = .overview) {
        selectedPopoverTab = tab
        onOpenPopoverTab?(tab)
    }

    func selectDate(_ date: String?) {
        guard snapshot != nil else { return }
        if date == currentShanghaiDay || selectedDate == date {
            selectedDate = nil
        } else {
            selectedDate = date
        }
    }

    func refreshAll(forceQuotas: Bool = false) {
        refresh()
        refreshQuotas(force: forceQuotas)
    }

    /// Apply decoded snapshot only if not older than currently accepted snapshot.
    @discardableResult
    func applySnapshotIfNewer(_ decoded: ClockSnapshot) -> Bool {
        if let current = snapshot {
            if decoded.today.date < current.today.date {
                return false
            }
            if decoded.today.date == current.today.date && decoded.generatedTs < current.generatedTs {
                return false
            }
        }
        let prevTasks = sentinelTasks.map(\.id)
        let prevTools = displayedDay?.tools.count ?? -1
        let prevFreshness = fastFreshness
        snapshot = decoded
        lastError = nil
        lastObservedShanghaiDay = currentShanghaiDay
        lastObservedShanghaiMonth = currentShanghaiMonth
        lastObservedFreshness = fastFreshness
        lastObservedSentinelIds = sentinelTasks.map { "\($0.id):\($0.state)" }
        onSnapshotUpdated?(decoded)
        if prevTasks != sentinelTasks.map(\.id) || prevTools != decoded.today.tools.count || prevFreshness != fastFreshness {
            onLayoutChanged?()
        }
        return true
    }

    func refresh() {
        guard !isRefreshing else { return }
        guard let scriptURL = locateScript(named: "collector.py", devSubpath: "engine/collector.py") else {
            lastError = "未找到采集引擎 collector.py"
            onStatusBarPreferenceChanged?()
            return
        }
        isRefreshing = true
        fastRequestSeq &+= 1
        let seq = fastRequestSeq
        let pythonPath = locatePython()
        let executor = commandExecutor ?? ScriptRunner.run

        workQueue.async { [weak self] in
            let result = executor(pythonPath, scriptURL, ["--days", "14", "--json"], 90)
            Task { @MainActor [weak self] in
                guard let self else { return }
                guard seq == self.fastRequestSeq else { return }
                self.isRefreshing = false
                self.nowEpoch = Date().timeIntervalSince1970
                switch result {
                case .success(let data):
                    do {
                        let decoded = try JSONDecoder().decode(ClockSnapshot.self, from: data)
                        _ = self.applySnapshotIfNewer(decoded)
                    } catch {
                        self.lastError = "无法读取采集结果，请检查本地数据后刷新。"
                        self.onStatusBarPreferenceChanged?()
                        self.onLayoutChanged?()
                    }
                case .failure(let err):
                    self.lastError = err.localizedDescription
                    self.onStatusBarPreferenceChanged?()
                    self.onLayoutChanged?()
                }
            }
        }
    }

    @discardableResult
    func applyQuotaSnapshotIfNewer(_ decoded: QuotaSnapshot) -> Bool {
        if let current = quotaSnapshot, decoded.generatedTs < current.generatedTs {
            return false
        }
        quotaSnapshot = decoded
        quotaError = nil
        lastObservedQuotaBadge = menuBarQuotaSummary
        lastObservedHighestQuota = highestFreshQuotaWindow
        onQuotaUpdated?(decoded)
        onLayoutChanged?()
        return true
    }

    func refreshQuotas(force: Bool = false) {
        guard !isRefreshingQuotas else { return }
        guard let scriptURL = locateScript(named: "quota_collector.py", devSubpath: "engine/quota_collector.py") else {
            quotaError = "未找到额度采集器，请重新安装 AgentPulse。"
            onStatusBarPreferenceChanged?()
            onLayoutChanged?()
            return
        }
        isRefreshingQuotas = true
        quotaRequestSeq &+= 1
        let seq = quotaRequestSeq
        let pythonPath = locatePython()
        let live = allowLiveCodexQuota
        let args = ["--json"] + (force ? ["--force"] : []) + (live ? ["--live-codex"] : [])
        let executor = commandExecutor ?? ScriptRunner.run

        quotaQueue.async { [weak self] in
            let result = executor(pythonPath, scriptURL, args, 30)
            Task { @MainActor [weak self] in
                guard let self else { return }
                guard seq == self.quotaRequestSeq else { return }
                self.isRefreshingQuotas = false
                self.nowEpoch = Date().timeIntervalSince1970
                guard self.allowLiveCodexQuota == live else {
                    self.refreshQuotas()
                    return
                }
                switch result {
                case .success(let data):
                    do {
                        let decoded = try JSONDecoder().decode(QuotaSnapshot.self, from: data)
                        _ = self.applyQuotaSnapshotIfNewer(decoded)
                    } catch {
                        self.quotaError = "额度数据格式无效，请刷新或检查本地额度文件。"
                        self.onStatusBarPreferenceChanged?()
                        self.onLayoutChanged?()
                    }
                case .failure(let error):
                    self.quotaError = error.localizedDescription
                    self.onStatusBarPreferenceChanged?()
                    self.onLayoutChanged?()
                }
            }
        }
    }

    func runTokeiImport() {
        guard !isMigrating else { return }
        guard let scriptURL = locateScript(named: "import_tokei.py", devSubpath: "scripts/import_tokei.py") else {
            migrationMessage = "未找到迁移脚本 import_tokei.py"
            return
        }
        isMigrating = true
        migrationMessage = nil
        let pythonPath = locatePython()
        let executor = commandExecutor ?? ScriptRunner.run

        workQueue.async { [weak self] in
            let result = executor(pythonPath, scriptURL, ["--json"], 90)
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.isMigrating = false
                switch result {
                case .success(let data):
                    if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                       let ok = obj["ok"] as? Bool, ok {
                        let tools = obj["imported_tools"] as? Int ?? 0
                        let days = obj["imported_days"] as? Int ?? 0
                        self.migrationMessage = "已备份并导入 \(tools) 个工具、\(days) 条历史记录"
                        self.refreshAll(forceQuotas: false)
                    } else {
                        self.migrationMessage = "未检测到可导入的 Tokei 数据"
                    }
                case .failure(let err):
                    self.migrationMessage = "导入失败：\(err.localizedDescription)"
                }
            }
        }
    }

    func openDataDirectory() {
        let dir = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".agent-pulse")
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
        } catch CocoaError.fileWriteFileExists {
            // Already initialized
        } catch {
            lastError = "无法打开数据目录，请检查目录权限。"
            return
        }
        revealInFinder([dir])
    }

    // MARK: - Safe Sentinel Finder Jump & Directory Validation (Phase B Contract)

    func openSentinelTask(_ task: SentinelTask) {
        let candidates = [task.workspacePath, task.projectPath].compactMap { $0 }
        for candidate in candidates {
            if let validated = Self.validatedProjectDirectory(candidate) {
                projectActionError = nil
                revealInFinder([validated])
                return
            }
        }
        projectActionError = "项目目录不可用"
    }

    func openProjectInFinder(_ path: String) {
        guard let directory = projectDirectory(path) else { return }
        revealInFinder([directory])
    }

    func openProjectInTerminal(_ path: String) {
        guard let directory = projectDirectory(path) else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-a", "Terminal", directory.path]
        do {
            try proc.run()
        } catch {
            projectActionError = "无法启动终端。"
        }
    }

    private func revealInFinder(_ urls: [URL]) {
        if let custom = finderRevealer {
            custom(urls)
        } else {
            NSWorkspace.shared.activateFileViewerSelecting(urls)
        }
    }

    private func projectDirectory(_ path: String) -> URL? {
        guard let directory = Self.validatedProjectDirectory(path) else {
            projectActionError = "项目目录已移动、不可访问，或不是普通目录。"
            return nil
        }
        projectActionError = nil
        return directory
    }

    nonisolated private static func isSafePathString(_ path: String) -> Bool {
        guard !path.isEmpty, path.hasPrefix("/") else { return false }
        guard !path.contains("://") else { return false }
        guard !path.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7F }) else {
            return false
        }
        return true
    }

    nonisolated static func validatedProjectDirectory(_ path: String) -> URL? {
        guard isSafePathString(path) else { return nil }
        let url = URL(fileURLWithPath: path).standardizedFileURL.resolvingSymlinksInPath().standardizedFileURL
        // Re-validate the canonical resolved path after symlink expansion (blocks control chars / bad targets).
        guard isSafePathString(url.path) else { return nil }
        let fm = FileManager.default
        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: url.path, isDirectory: &isDir), isDir.boolValue else {
            return nil
        }
        guard fm.isReadableFile(atPath: url.path) else { return nil }
        let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isPackageKey])
        return values?.isDirectory == true && values?.isPackage == false ? url : nil
    }

    // MARK: - Clipboard & Markdown Reports (Phase B Contract)

    func copyToolSummary(_ tool: UsageToolStats, periodTitle: String) {
        let hitPct = Int(round(tool.effectiveCacheHitRate * 100))
        let hasObs = tool.totalTokens > 0 || tool.costUsd > 0 || (tool.unpricedTokens ?? 0) > 0
        let estimate = Formatters.estimatedUSD(tool.costUsd, unpricedTokens: tool.unpricedTokens ?? 0, hasObservation: hasObs)
        var lines: [String] = [
            "### \(tool.displayName) · \(periodTitle)",
            "- 总 Token：\(Formatters.compactTokens(tool.totalTokens))（估算 \(estimate) · Cache Hit \(hitPct)%）",
            "- 构成：输入 \(Formatters.compactTokens(tool.inputTokens)) / 输出 \(Formatters.compactTokens(tool.outputTokens)) / 缓存读 \(Formatters.compactTokens(tool.cacheReadTokens)) / 缓存写 \(Formatters.compactTokens(tool.cacheWriteTokens)) / 推理 \(Formatters.compactTokens(tool.reasoningTokens))",
        ]
        if tool.agentSeconds > 0 {
            lines.append("- AI 有效工时：\(Formatters.shortDuration(tool.agentSeconds))（\(tool.effectiveSessions) 会话）")
        }
        if !tool.models.isEmpty {
            lines.append("- 模型明细：")
            for m in tool.models.prefix(8) {
                let mHit = Int(round(m.effectiveCacheHitRate * 100))
                let mObs = m.totalTokens > 0 || m.costUsd > 0 || (m.unpricedTokens ?? 0) > 0
                let amount = Formatters.estimatedUSD(m.costUsd, unpricedTokens: m.unpricedTokens ?? 0, hasObservation: mObs)
                lines.append("  - `\(m.model)`: \(Formatters.compactTokens(m.totalTokens)) tok · \(mHit)% · \(amount)")
            }
        }
        writeToPasteboard(lines.joined(separator: "\n"))
        copiedToolId = tool.id
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if self.copiedToolId == tool.id {
                self.copiedToolId = nil
            }
        }
    }

    func copyMarkdownReport() {
        var args = ["--days", "14", "--markdown"]
        if let target = selectedDate {
            args.append(contentsOf: ["--date", target])
        }
        runMarkdownExport(args: args)
    }

    func copyMarkdownReport(period: UsagePeriodKey) {
        let args = ["--days", "14", "--markdown", "--period", period.cliArgument]
        runMarkdownExport(args: args)
    }

    private func runMarkdownExport(args: [String]) {
        guard let scriptURL = locateScript(named: "collector.py", devSubpath: "engine/collector.py") else {
            reportError = "未找到采集引擎 collector.py，剪贴板未修改。"
            return
        }
        let pythonPath = locatePython()
        let executor = commandExecutor ?? ScriptRunner.run
        reportError = nil

        workQueue.async { [weak self] in
            let result = executor(pythonPath, scriptURL, args, 90)
            Task { @MainActor [weak self] in
                guard let self else { return }
                switch result {
                case .success(let data):
                    if let text = String(data: data, encoding: .utf8),
                       !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        self.writeToPasteboard(text)
                        self.reportError = nil
                        self.showCopiedToast()
                    } else {
                        self.reportError = "生成战报为空，已保留原剪贴板内容。"
                    }
                case .failure(let err):
                    self.reportError = err.localizedDescription
                }
            }
        }
    }

    private func writeToPasteboard(_ text: String) {
        if let custom = pasteboardWriter {
            custom(text)
        } else {
            let pb = NSPasteboard.general
            pb.clearContents()
            pb.setString(text, forType: .string)
        }
    }

    func toggleLaunchAtLogin() {
        let targetState = !launchAtLogin
        let plistURL = Self.launchAgentURL
        let fm = FileManager.default

        if targetState {
            let appBinary: String
            if Bundle.main.bundleURL.pathExtension == "app" {
                appBinary = Bundle.main.bundleURL
                    .appendingPathComponent("Contents/MacOS/AgentPulse").path
            } else {
                appBinary = "/Applications/AgentPulse.app/Contents/MacOS/AgentPulse"
            }

            let plist: [String: Any] = [
                "Label": Self.launchAgentLabel,
                "ProgramArguments": [appBinary],
                "RunAtLoad": true,
                "KeepAlive": false,
                "ProcessType": "Interactive",
            ]
            do {
                try fm.createDirectory(at: plistURL.deletingLastPathComponent(), withIntermediateDirectories: true)
                let data = try PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0)
                try data.write(to: plistURL, options: .atomic)
                launchAtLogin = true
            } catch {
                lastError = "写入开机自启失败"
            }
        } else {
            try? fm.removeItem(at: plistURL)
            launchAtLogin = false
        }
    }

    private func showCopiedToast() {
        toastTask?.cancel()
        copiedToast = true
        toastTask = Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_800_000_000)
            if !Task.isCancelled {
                copiedToast = false
            }
        }
    }

    private func locatePython() -> String {
        for candidate in ["/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"] {
            if FileManager.default.isExecutableFile(atPath: candidate) {
                return candidate
            }
        }
        return "/usr/bin/python3"
    }

    private func locateScript(named bundleFileName: String, devSubpath: String) -> URL? {
        if let res = Bundle.main.resourceURL?.appendingPathComponent(bundleFileName),
           FileManager.default.fileExists(atPath: res.path) {
            return res
        }
        if Bundle.main.bundleURL.pathExtension == "app" { return nil }
        let exeDir = Bundle.main.executableURL?.deletingLastPathComponent()
        let devCandidates: [URL] = [
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(devSubpath),
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("../\(devSubpath)"),
            exeDir?.appendingPathComponent("../../../\(devSubpath)"),
            exeDir?.appendingPathComponent("../../../../\(devSubpath)"),
        ].compactMap { $0?.standardizedFileURL }

        return devCandidates.first { FileManager.default.fileExists(atPath: $0.path) }
    }
}
