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
    @Published var selectedHistoryMetric: HistoryMetric = .cost

    @Published var isRefreshing: Bool = false
    @Published var isRefreshingQuotas: Bool = false
    @Published var isMigrating: Bool = false
    @Published var migrationMessage: String?
    @Published var lastError: String?
    @Published var nowEpoch: Double = Date().timeIntervalSince1970
    @Published var copiedToast: Bool = false

    @Published var isCompact: Bool {
        didSet {
            UserDefaults.standard.set(isCompact, forKey: Keys.isCompact)
            onLayoutChanged?()
        }
    }

    @Published var windowLayer: WindowLayerMode {
        didSet {
            UserDefaults.standard.set(windowLayer.rawValue, forKey: Keys.windowLayer)
            onWindowLayerChanged?(windowLayer)
        }
    }

    @Published var isCardVisible: Bool {
        didSet {
            UserDefaults.standard.set(isCardVisible, forKey: Keys.isCardVisible)
            onVisibilityChanged?(isCardVisible)
        }
    }

    @Published var menuBarShowCost: Bool {
        didSet {
            UserDefaults.standard.set(menuBarShowCost, forKey: Keys.menuBarShowCost)
            onStatusBarPreferenceChanged?()
        }
    }

    @Published var menuBarShowQuotas: Bool {
        didSet {
            UserDefaults.standard.set(menuBarShowQuotas, forKey: Keys.menuBarShowQuotas)
            onStatusBarPreferenceChanged?()
        }
    }

    @Published var cardShowQuotaAlert: Bool {
        didSet {
            UserDefaults.standard.set(cardShowQuotaAlert, forKey: Keys.cardShowQuotaAlert)
            onLayoutChanged?()
        }
    }

    @Published var launchAtLogin: Bool = false
    @Published var pinnedProjectPaths: Set<String> = []
    @Published var copiedToolId: String?

    let sitReminder = SitReminderManager()

    var onSnapshotUpdated: ((ClockSnapshot) -> Void)?
    var onQuotaUpdated: ((QuotaSnapshot) -> Void)?
    var onStatusBarPreferenceChanged: (() -> Void)?
    var onWindowLayerChanged: ((WindowLayerMode) -> Void)?
    var onVisibilityChanged: ((Bool) -> Void)?
    var onLayoutChanged: (() -> Void)?
    var onOpenPopoverTab: ((PopoverTab) -> Void)?

    private var refreshTimer: Timer?
    private var quotaTimer: Timer?
    private var tickTimer: Timer?
    private var toastTask: Task<Void, Never>?
    private let workQueue = DispatchQueue(label: "dev.agentpulse.app.collector", qos: .utility)
    private let quotaQueue = DispatchQueue(label: "dev.agentpulse.app.quota", qos: .utility)

    private enum Keys {
        static let isCompact = "agentPulse.isCompact"
        static let windowLayer = "agentPulse.windowLayer"
        static let isCardVisible = "agentPulse.isCardVisible"
        static let menuBarShowCost = "agentPulse.menuBar.showCost"
        static let menuBarShowQuotas = "agentPulse.menuBar.showQuotas"
        static let cardShowQuotaAlert = "agentPulse.card.showQuotaAlert"
        static let pinnedProjects = "agentPulse.projects.pinned"
    }

    static let launchAgentLabel = "dev.agentpulse.app"
    static var launchAgentURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/\(launchAgentLabel).plist")
    }

    init() {
        let defaults = UserDefaults.standard
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
        self.launchAtLogin = FileManager.default.fileExists(atPath: Self.launchAgentURL.path)
        let savedPins = defaults.stringArray(forKey: Keys.pinnedProjects) ?? []
        self.pinnedProjectPaths = Set(savedPins)
    }

    var displayedDay: DayStats? {
        guard let snap = snapshot else { return nil }
        if let target = selectedDate,
           let matched = snap.days.first(where: { $0.date == target }) {
            return matched
        }
        return snap.today
    }

    var isViewingToday: Bool {
        guard let snap = snapshot else { return true }
        return selectedDate == nil || selectedDate == snap.today.date
    }

    var liveBonusSeconds: Double {
        guard let snap = snapshot, !snap.activeAgents.isEmpty else { return 0 }
        let delta = max(0, min(60, nowEpoch - snap.generatedTs))
        return delta * Double(snap.activeAgents.count)
    }

    var displayedAgentSeconds: Double {
        guard let day = displayedDay else { return 0 }
        return day.agentSeconds + (isViewingToday ? liveBonusSeconds : 0)
    }

    var todayLiveSeconds: Double {
        guard let snap = snapshot else { return 0 }
        return snap.today.agentSeconds + liveBonusSeconds
    }

    var todayTokens: Int {
        if let u = snapshot?.usage?.periods.today.totalTokens { return u }
        if let t = snapshot?.today.tokens { return t }
        return snapshot?.tokeiRef.todayTokens ?? 0
    }

    var todayCostUsd: Double {
        if let c = snapshot?.usage?.periods.today.costUsd { return c }
        if let c = snapshot?.today.costUsd { return c }
        return snapshot?.tokeiRef.todayCostUsd ?? 0.0
    }

    var displayedDayTokens: Int {
        if isViewingToday { return todayTokens }
        return displayedDay?.tokens ?? 0
    }

    var displayedDayCostUsd: Double {
        if isViewingToday { return todayCostUsd }
        return displayedDay?.costUsd ?? 0.0
    }

    var lastUpdatedTimeString: String {
        guard let snap = snapshot else { return "--:--" }
        let dt = Date(timeIntervalSince1970: snap.generatedTs)
        let f = DateFormatter()
        f.dateFormat = "HH:mm"
        f.timeZone = TimeZone(identifier: "Asia/Shanghai")
        return f.string(from: dt)
    }

    /// Compact quota badges for the macOS menu bar (e.g. "C5 3% · CL7 3%")
    var menuBarQuotaSummary: String? {
        guard menuBarShowQuotas, let q = quotaSnapshot else { return nil }
        var parts: [String] = []
        if let codex = q.providers.first(where: { $0.id == "codex" && $0.status != "unavailable" }),
           let w5 = codex.windows.first(where: { $0.id == "codex_5h" }) ?? codex.windows.first {
            parts.append("\(w5.shortLabel) \(Int(round(w5.usedPercent)))%")
        }
        if let claude = q.providers.first(where: { $0.id == "claude" && $0.status != "unavailable" }),
           let w7 = claude.windows.first(where: { $0.id == "claude_7d" }) ?? claude.windows.first {
            parts.append("\(w7.shortLabel) \(Int(round(w7.usedPercent)))%")
        }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    func start() {
        refresh()
        refreshQuotas(force: false)

        // Fast track: every 15s (pure local read-only work hours + incremental token/cost)
        refreshTimer?.invalidate()
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 15.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refresh()
            }
        }

        // Slow track: every 300s (independent subscription quota poll)
        quotaTimer?.invalidate()
        quotaTimer = Timer.scheduledTimer(withTimeInterval: 300.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshQuotas(force: false)
            }
        }

        // 1s in-memory UI tick (live agents + sit reminder countdown)
        tickTimer?.invalidate()
        tickTimer = Timer.scheduledTimer(withTimeInterval: 1.0, repeats: true) { [weak self] _ in
            Task { @MainActor in
                guard let self else { return }
                self.sitReminder.tickOneSecond()
                if let snap = self.snapshot, !snap.activeAgents.isEmpty {
                    self.nowEpoch = Date().timeIntervalSince1970
                }
            }
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
        guard let snap = snapshot else { return }
        if date == snap.today.date || selectedDate == date {
            selectedDate = nil
        } else {
            selectedDate = date
        }
    }

    func refreshAll(forceQuotas: Bool = false) {
        refresh()
        refreshQuotas(force: forceQuotas)
    }

    func refresh() {
        guard !isRefreshing else { return }
        guard let scriptURL = locateScript(named: "collector.py", devSubpath: "engine/collector.py") else {
            lastError = "未找到采集引擎 collector.py"
            return
        }
        isRefreshing = true
        let pythonPath = locatePython()

        workQueue.async { [weak self] in
            let result = Self.runScript(pythonPath: pythonPath, scriptURL: scriptURL, args: ["--days", "14", "--json"])
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.isRefreshing = false
                self.nowEpoch = Date().timeIntervalSince1970
                switch result {
                case .success(let data):
                    do {
                        let decoded = try JSONDecoder().decode(ClockSnapshot.self, from: data)
                        let activeChanged = (self.snapshot?.activeAgents.count ?? -1) != decoded.activeAgents.count
                        let toolsChanged = (self.displayedDay?.tools.count ?? -1) != decoded.today.tools.count
                        self.snapshot = decoded
                        self.lastError = nil
                        self.onSnapshotUpdated?(decoded)
                        if activeChanged || toolsChanged {
                            self.onLayoutChanged?()
                        }
                    } catch {
                        self.lastError = "解析失败: \(error.localizedDescription)"
                    }
                case .failure(let err):
                    self.lastError = err.localizedDescription
                }
            }
        }
    }

    func refreshQuotas(force: Bool = false) {
        guard !isRefreshingQuotas else { return }
        guard let scriptURL = locateScript(named: "quota_collector.py", devSubpath: "engine/quota_collector.py") else {
            return
        }
        isRefreshingQuotas = true
        let pythonPath = locatePython()
        let args = force ? ["--json", "--force"] : ["--json"]

        quotaQueue.async { [weak self] in
            let result = Self.runScript(pythonPath: pythonPath, scriptURL: scriptURL, args: args)
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.isRefreshingQuotas = false
                if case .success(let data) = result,
                   let decoded = try? JSONDecoder().decode(QuotaSnapshot.self, from: data) {
                    self.quotaSnapshot = decoded
                    self.onQuotaUpdated?(decoded)
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

        workQueue.async { [weak self] in
            let result = Self.runScript(pythonPath: pythonPath, scriptURL: scriptURL, args: ["--json"])
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
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        NSWorkspace.shared.open(dir)
    }

    func togglePinProject(_ path: String) {
        if pinnedProjectPaths.contains(path) {
            pinnedProjectPaths.remove(path)
        } else {
            pinnedProjectPaths.insert(path)
        }
        UserDefaults.standard.set(Array(pinnedProjectPaths), forKey: Keys.pinnedProjects)
    }

    func openProjectInFinder(_ path: String) {
        let url = URL(fileURLWithPath: path)
        if FileManager.default.fileExists(atPath: url.path) {
            NSWorkspace.shared.open(url)
        }
    }

    func openProjectInTerminal(_ path: String) {
        let url = URL(fileURLWithPath: path)
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/open")
        proc.arguments = ["-a", "Terminal", url.path]
        try? proc.run()
    }

    func copyToolSummary(_ tool: UsageToolStats, periodTitle: String) {
        let hitPct = Int(round(tool.effectiveCacheHitRate * 100))
        var lines: [String] = [
            "### \(tool.displayName) · \(periodTitle)",
            "- 总 Token：\(Formatters.compactTokens(tool.totalTokens))（估算 \(Formatters.usd(tool.costUsd)) · Cache Hit \(hitPct)%）",
            "- 构成：输入 \(Formatters.compactTokens(tool.inputTokens)) / 输出 \(Formatters.compactTokens(tool.outputTokens)) / 缓存读 \(Formatters.compactTokens(tool.cacheReadTokens)) / 缓存写 \(Formatters.compactTokens(tool.cacheWriteTokens)) / 推理 \(Formatters.compactTokens(tool.reasoningTokens))",
        ]
        if tool.agentSeconds > 0 {
            lines.append("- AI 有效工时：\(Formatters.shortDuration(tool.agentSeconds))（\(tool.effectiveSessions) 会话）")
        }
        if !tool.models.isEmpty {
            lines.append("- 模型明细：")
            for m in tool.models.prefix(8) {
                let mHit = Int(round(m.effectiveCacheHitRate * 100))
                lines.append("  - `\(m.model)`: \(Formatters.compactTokens(m.totalTokens)) tok · \(mHit)% · \(Formatters.usd(m.costUsd))")
            }
        }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(lines.joined(separator: "\n"), forType: .string)
        copiedToolId = tool.id
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_500_000_000)
            if self.copiedToolId == tool.id {
                self.copiedToolId = nil
            }
        }
    }

    func copyMarkdownReport() {
        guard let scriptURL = locateScript(named: "collector.py", devSubpath: "engine/collector.py") else { return }
        let pythonPath = locatePython()
        var args = ["--days", "14", "--markdown"]
        if let target = selectedDate {
            args.append(contentsOf: ["--date", target])
        }
        workQueue.async { [weak self] in
            let result = Self.runScript(pythonPath: pythonPath, scriptURL: scriptURL, args: args)
            Task { @MainActor [weak self] in
                guard let self else { return }
                if case .success(let data) = result,
                   let text = String(data: data, encoding: .utf8),
                   !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                    let pb = NSPasteboard.general
                    pb.clearContents()
                    pb.setString(text, forType: .string)
                    self.showCopiedToast()
                }
            }
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
                "ProcessType": "Interactive"
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
        let exeDir = Bundle.main.executableURL?.deletingLastPathComponent()
        let devCandidates: [URL] = [
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(devSubpath),
            URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent("../\(devSubpath)"),
            exeDir?.appendingPathComponent("../../../\(devSubpath)"),
            exeDir?.appendingPathComponent("../../../../\(devSubpath)"),
        ].compactMap { $0?.standardizedFileURL }

        return devCandidates.first { FileManager.default.fileExists(atPath: $0.path) }
    }

    private nonisolated static func runScript(pythonPath: String, scriptURL: URL, args: [String]) -> Result<Data, Error> {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: pythonPath)
        proc.arguments = [scriptURL.path] + args
        let outPipe = Pipe()
        let errPipe = Pipe()
        proc.standardOutput = outPipe
        proc.standardError = errPipe

        do {
            try proc.run()
            let data = outPipe.fileHandleForReading.readDataToEndOfFile()
            let errData = errPipe.fileHandleForReading.readDataToEndOfFile()
            proc.waitUntilExit()
            if proc.terminationStatus == 0 {
                return .success(data)
            }
            let errMsg = String(data: errData, encoding: .utf8) ?? "exit \(proc.terminationStatus)"
            return .failure(NSError(domain: "AgentPulse", code: Int(proc.terminationStatus), userInfo: [NSLocalizedDescriptionKey: errMsg]))
        } catch {
            return .failure(error)
        }
    }
}
