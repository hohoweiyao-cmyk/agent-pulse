import Foundation
import XCTest
@testable import AgentPulse

@MainActor
final class V07ContractTests: XCTestCase {
    private var suiteName: String!
    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "dev.agentpulse.v07.tests.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)!
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        suiteName = nil
        super.tearDown()
    }

    private func fixtureURL(_ name: String) -> URL {
        // Locate repo root from #filePath (macos/Tests/AgentPulseTests/V07ContractTests.swift)
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent() // AgentPulseTests
            .deletingLastPathComponent() // Tests
            .deletingLastPathComponent() // macos
            .deletingLastPathComponent() // repo root
            .appendingPathComponent("tests/fixtures/\(name)")
    }

    // MARK: - 1. Schema Decoding Compatibility (v0.6, v0.7, Unknown Enums)

    func testDecodeV06AndV07FixturesAndUnknownEnums() throws {
        let v06FastData = try Data(contentsOf: fixtureURL("v06_fast_v3_minimal.json"))
        let v06Fast = try JSONDecoder().decode(ClockSnapshot.self, from: v06FastData)
        XCTAssertEqual(v06Fast.schemaVersion, 3)
        XCTAssertNil(v06Fast.collection)
        XCTAssertNil(v06Fast.sentinelTasks)
        XCTAssertTrue(v06Fast.today.hasHourlyDistribution)

        let v07FastData = try Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json"))
        let v07Fast = try JSONDecoder().decode(ClockSnapshot.self, from: v07FastData)
        XCTAssertEqual(v07Fast.collection?.work.status, .complete)
        XCTAssertEqual(v07Fast.sentinelTasks?.count, 3)
        XCTAssertEqual(v07Fast.activeAgents.first?.taskId, v07Fast.sentinelTasks?[1].id)
        XCTAssertEqual(v07Fast.today.workStatus, .observed)
        XCTAssertEqual(v07Fast.today.hourlyAvailable, true)
        XCTAssertEqual(v07Fast.usage?.projectTrails?.count, 1)

        let v06Quota = try JSONDecoder().decode(QuotaSnapshot.self, from: Data(contentsOf: fixtureURL("v06_quota_v1_minimal.json")))
        XCTAssertEqual(v06Quota.providers.first?.windows.first?.freshUntilTs, nil)
        XCTAssertNil(v06Quota.providers.first?.windows.first?.pace)

        let v07Quota = try JSONDecoder().decode(QuotaSnapshot.self, from: Data(contentsOf: fixtureURL("v07_quota_v1_minimal.json")))
        XCTAssertEqual(v07Quota.providers.first?.windows.first?.pace?.level, .watch)
        XCTAssertEqual(v07Quota.providers[1].windows.first?.pace?.reason, .unverifiedScope)

        // Unknown enum values MUST NOT fail decoding!
        var raw = String(decoding: v07FastData, as: UTF8.self)
        raw = raw.replacingOccurrences(of: "\"observed\"", with: "\"future_work_status\"")
        raw = raw.replacingOccurrences(of: "\"waiting_human\"", with: "\"future_sentinel_state\"")
        raw = raw.replacingOccurrences(of: "\"approval\"", with: "\"future_wait_kind\"")
        let resilient = try JSONDecoder().decode(ClockSnapshot.self, from: Data(raw.utf8))
        XCTAssertEqual(resilient.today.workStatus, .unknown)
        XCTAssertEqual(resilient.sentinelTasks?.first?.state, .unknown("future_sentinel_state"))
        XCTAssertEqual(resilient.sentinelTasks?.first?.evidence?.waitKind, .unknown("future_wait_kind"))

        var rawQ = String(decoding: try Data(contentsOf: fixtureURL("v07_quota_v1_minimal.json")), as: UTF8.self)
        rawQ = rawQ.replacingOccurrences(of: "\"watch\"", with: "\"future_pace_level\"")
        rawQ = rawQ.replacingOccurrences(of: "\"unverified_scope\"", with: "\"future_pace_reason\"")
        let resilientQ = try JSONDecoder().decode(QuotaSnapshot.self, from: Data(rawQ.utf8))
        XCTAssertEqual(resilientQ.providers.first?.windows.first?.pace?.level, .unknown)
        XCTAssertEqual(resilientQ.providers[1].windows.first?.pace?.reason, .unknown)
    }

    // MARK: - 2. Unified Cost Formatting (Formatters.estimatedUSD)

    func testUnifiedCostFormatting() {
        XCTAssertEqual(Formatters.estimatedUSD(0.0, unpricedTokens: 5000, hasObservation: true), "未计价")
        XCTAssertEqual(Formatters.estimatedUSD(1.25, unpricedTokens: 5000, hasObservation: true), "$1.25+")
        XCTAssertEqual(Formatters.estimatedUSD(0.0, unpricedTokens: 0, hasObservation: true), "$0.00")
        XCTAssertEqual(Formatters.estimatedUSD(0.0, unpricedTokens: 0, hasObservation: false), "—")
        XCTAssertEqual(Formatters.estimatedUSD(12.34, unpricedTokens: 0, hasObservation: true), "$12.34")
    }

    // MARK: - 3. Fast Freshness, 45s Stale, Midnight Crossing, 120s Just Finished & Older Callback Rejection

    func testFreshnessLifecycleAndStaleCallbackRejection() throws {
        let store = ClockStore(defaults: defaults)
        let snap = try JSONDecoder().decode(ClockSnapshot.self, from: Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json")))
        let baseTs = snap.generatedTs // 2026-11-27 15:00:00 +0800

        store.nowEpoch = baseTs + 10
        XCTAssertTrue(store.applySnapshotIfNewer(snap))
        XCTAssertEqual(store.fastFreshness, .fresh)
        XCTAssertEqual(store.sentinelTasks.count, 3)
        XCTAssertEqual(store.todayRunningCount, 1)
        XCTAssertEqual(store.liveBonusSeconds, 10.0, accuracy: 0.01)

        // Older snapshot rejected
        let olderJson = String(decoding: try Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json")), as: UTF8.self)
            .replacingOccurrences(of: "1795762800.0", with: "1795762700.0")
        let olderSnap = try JSONDecoder().decode(ClockSnapshot.self, from: Data(olderJson.utf8))
        XCTAssertFalse(store.applySnapshotIfNewer(olderSnap))
        XCTAssertEqual(store.snapshot?.generatedTs, baseTs)

        // Reach 45s -> stale! Freezes total & clears active count & sentinels
        store.handleClockTick(baseTs + 45.0)
        XCTAssertEqual(store.fastFreshness, .stale)
        XCTAssertEqual(store.todayRunningCount, 0)
        XCTAssertEqual(store.sentinelTasks, [])
        XCTAssertEqual(store.liveBonusSeconds, 0.0)
        XCTAssertTrue(AppDelegate.formatStatusBarTitle(for: store).contains("离线"))

        // Cross Shanghai midnight (next day 00:00:01) -> stale & does NOT carry yesterday's work/tokens as today
        let nextDayTs = baseTs + 9 * 3600 + 1
        store.handleClockTick(nextDayTs)
        XCTAssertEqual(store.fastFreshness, .stale)
        XCTAssertEqual(store.todayLiveSeconds, 0.0)
        XCTAssertEqual(store.todayTokens, 0)

        // Old v0.6 snapshot works while fresh, exits live at 45s
        let v06Store = ClockStore(defaults: defaults)
        let v06Snap = try JSONDecoder().decode(ClockSnapshot.self, from: Data(contentsOf: fixtureURL("v06_fast_v3_minimal.json")))
        v06Store.nowEpoch = v06Snap.generatedTs + 5
        v06Store.applySnapshotIfNewer(v06Snap)
        XCTAssertEqual(v06Store.fastFreshness, .fresh)
        XCTAssertEqual(v06Store.todayRunningCount, 1)
        XCTAssertEqual(v06Store.sentinelTasks.first?.state, .running)
        v06Store.handleClockTick(v06Snap.generatedTs + 45.0)
        XCTAssertEqual(v06Store.fastFreshness, .stale)
        XCTAssertEqual(v06Store.todayRunningCount, 0)
    }

    // MARK: - 4. Quota Freshness, TTL Expiry, Error Clearing & Dynamic Highest Window

    func testQuotaFreshnessAndHighestWindowSelection() throws {
        let store = ClockStore(defaults: defaults)
        let q = try JSONDecoder().decode(QuotaSnapshot.self, from: Data(contentsOf: fixtureURL("v07_quota_v1_minimal.json")))
        let baseTs = q.generatedTs

        store.nowEpoch = baseTs + 10
        store.applyQuotaSnapshotIfNewer(q)
        XCTAssertEqual(store.highestFreshQuotaWindow?.providerId, "codex")
        XCTAssertEqual(store.menuBarQuotaSummary, "C5 70% · CL7 35%")

        // Codex expires at baseTs + 360; Claude still fresh -> dynamically re-selects Claude!
        store.handleClockTick(baseTs + 360.0)
        XCTAssertFalse(store.quotaWindowIsFresh(provider: q.providers[0], window: q.providers[0].windows[0]))
        XCTAssertTrue(store.quotaWindowIsFresh(provider: q.providers[1], window: q.providers[1].windows[0]))
        XCTAssertEqual(store.highestFreshQuotaWindow?.providerId, "claude")
        XCTAssertEqual(store.menuBarQuotaSummary, "CL7 35%")

        // Quota error immediately clears live badges & alerts
        store.quotaError = "offline"
        XCTAssertNil(store.highestFreshQuotaWindow)
        XCTAssertNil(store.menuBarQuotaSummary)
    }

    // MARK: - 5. Safe Sentinel Finder Jump Validation

    func testSentinelSafeDirectoryValidationAndFallback() throws {
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("ap-path-\(UUID())", isDirectory: true)
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }

        let specialDir = tmp.appendingPathComponent("dir with 'quotes';and spaces", isDirectory: true)
        try FileManager.default.createDirectory(at: specialDir, withIntermediateDirectories: true)
        let appPkg = tmp.appendingPathComponent("Fake.app", isDirectory: true)
        try FileManager.default.createDirectory(at: appPkg, withIntermediateDirectories: true)
        let regularFile = tmp.appendingPathComponent("file.txt")
        try Data("x".utf8).write(to: regularFile)
        let linkToDir = tmp.appendingPathComponent("link-dir")
        try FileManager.default.createSymbolicLink(at: linkToDir, withDestinationURL: specialDir)
        let linkToFile = tmp.appendingPathComponent("link-file")
        try FileManager.default.createSymbolicLink(at: linkToFile, withDestinationURL: regularFile)

        // Validation matrix
        XCTAssertNil(ClockStore.validatedProjectDirectory(""))
        XCTAssertNil(ClockStore.validatedProjectDirectory("relative/path"))
        XCTAssertNil(ClockStore.validatedProjectDirectory("file:///tmp"))
        XCTAssertNil(ClockStore.validatedProjectDirectory("https://example.com"))
        XCTAssertNil(ClockStore.validatedProjectDirectory("/tmp/\u{0007}bad"))
        XCTAssertNil(ClockStore.validatedProjectDirectory(regularFile.path))
        XCTAssertNil(ClockStore.validatedProjectDirectory(appPkg.path))
        XCTAssertNil(ClockStore.validatedProjectDirectory(linkToFile.path))
        XCTAssertNil(ClockStore.validatedProjectDirectory(tmp.path + "/does-not-exist"))
        XCTAssertEqual(ClockStore.validatedProjectDirectory(specialDir.path)?.path, specialDir.resolvingSymlinksInPath().path)
        XCTAssertEqual(ClockStore.validatedProjectDirectory(linkToDir.path)?.path, specialDir.resolvingSymlinksInPath().path)

        // openSentinelTask falls back from missing workspacePath to valid projectPath
        let store = ClockStore(defaults: defaults)
        var revealed: [URL] = []
        store.finderRevealer = { urls in revealed = urls }

        let fallbackTask = SentinelTask(
            id: "id1", sourceId: "work.t3", tool: "t3_codex", displayName: "T3", shortName: "T3",
            color: "#10B981", label: "demo", state: .waitingHuman, startedAt: 1, stateSinceTs: 1,
            expiresAt: 100, elapsedSeconds: 0, projectPath: specialDir.path,
            workspacePath: tmp.path + "/missing", evidence: nil
        )
        store.openSentinelTask(fallbackTask)
        XCTAssertNil(store.projectActionError)
        XCTAssertEqual(revealed.first?.path, specialDir.resolvingSymlinksInPath().path)

        // No valid path -> sets exact error "项目目录不可用"
        revealed = []
        let badTask = SentinelTask(
            id: "id2", sourceId: "work.t3", tool: "t3_codex", displayName: "T3", shortName: "T3",
            color: "#10B981", label: "demo", state: .running, startedAt: 1, stateSinceTs: 1,
            expiresAt: 100, elapsedSeconds: 0, projectPath: appPkg.path,
            workspacePath: "https://evil.example", evidence: nil
        )
        store.openSentinelTask(badTask)
        XCTAssertEqual(store.projectActionError, "项目目录不可用")
        XCTAssertTrue(revealed.isEmpty)
    }

    // MARK: - 6. Project Trail Periods & Pin Alias Merging

    func testProjectTrailRowsAndPinAliasMerging() throws {
        // Legacy pin on worktree alias path
        defaults.set(["/tmp/demo-repo-wt"], forKey: ClockStore.Keys.pinnedProjects)
        let store = ClockStore(defaults: defaults)
        let snap = try JSONDecoder().decode(ClockSnapshot.self, from: Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json")))
        store.nowEpoch = snap.generatedTs + 5
        store.applySnapshotIfNewer(snap)

        let todayRows = store.projectTrailRows(for: .today)
        XCTAssertEqual(todayRows.count, 1)
        XCTAssertEqual(todayRows[0].path, "/tmp/demo-repo")
        XCTAssertTrue(todayRows[0].isPinned) // Matched via worktree alias!

        // Unpinning removes BOTH canonical and alias paths
        store.togglePinProject("/tmp/demo-repo")
        XCTAssertFalse(store.projectTrailRows(for: .today)[0].isPinned)
        XCTAssertFalse(store.pinnedProjectPaths.contains("/tmp/demo-repo-wt"))
    }

    // MARK: - 7. Subscription Fee Validation & Decimal Payback (40/20 = 2.00x + Hermes $3.25)

    func testSubscriptionFeesAndPaybackCalculation() throws {
        // Inject invalid legacy persisted values -> must be skipped safely
        defaults.set(["codex": -5, "bad_high": 2_000_000, "bad_bool": true], forKey: ClockStore.Keys.subscriptionFeesUSDCents)
        let store = ClockStore(defaults: defaults)
        let snap = try JSONDecoder().decode(ClockSnapshot.self, from: Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json")))
        store.nowEpoch = snap.generatedTs + 5
        store.applySnapshotIfNewer(snap)

        // Initially unconfigured (invalid values ignored)
        let initialCodex = store.subscriptionRows.first { $0.tool == "codex" }
        XCTAssertNil(initialCodex?.monthlyFeeCents)
        XCTAssertNil(initialCodex?.paybackMultiple)

        // Reject invalid inputs
        XCTAssertFalse(store.setSubscriptionFee(cents: 0, for: "codex"))
        XCTAssertFalse(store.setSubscriptionFee(cents: -100, for: "codex"))
        XCTAssertFalse(store.setSubscriptionFee(cents: 1_000_001, for: "codex"))
        XCTAssertFalse(store.setSubscriptionFee(cents: 2000, for: ""))

        // Configure $20.00 (2000 cents) via t3_codex alias -> canonicalizes to codex!
        XCTAssertTrue(store.setSubscriptionFee(cents: 2000, for: "t3_codex"))
        let codexRow = store.subscriptionRows.first { $0.tool == "codex" }
        XCTAssertEqual(codexRow?.monthlyFeeCents, 2000)
        XCTAssertEqual(codexRow?.subscriptionValueUsd, Decimal(40))
        XCTAssertEqual(codexRow?.paybackMultiple, Decimal(2))

        // Hermes verified API actual ($3.25) kept separate
        let hermesRow = store.subscriptionRows.first { $0.tool == "hermes" }
        XCTAssertEqual(hermesRow?.apiActualUsd, Decimal(string: "3.25"))
        XCTAssertNil(hermesRow?.paybackMultiple)

        // Delete fee (nil) removes key
        XCTAssertTrue(store.setSubscriptionFee(cents: nil, for: "codex"))
        XCTAssertNil(store.subscriptionRows.first { $0.tool == "codex" }?.monthlyFeeCents)
    }

    // MARK: - 8. Markdown Period Copy & Failure Clipboard Preservation

    func testCopyMarkdownReportPeriodAndFailureSafety() throws {
        let store = ClockStore(defaults: defaults)
        store.selectedDate = "2026-11-20" // Different from selected period!

        var capturedArgs: [String] = []
        var writtenClipboard: String?
        let expSuccess = expectation(description: "period report copied")

        store.commandExecutor = { _, _, args, _ in
            capturedArgs = args
            return .success(Data("## AI 工时与用量战报 · 上周\n".utf8))
        }
        store.pasteboardWriter = { text in
            writtenClipboard = text
            expSuccess.fulfill()
        }

        store.copyMarkdownReport(period: .lastWeek)
        wait(for: [expSuccess], timeout: 3.0)
        XCTAssertEqual(capturedArgs, ["--days", "14", "--markdown", "--period", "last_week"])
        XCTAssertEqual(writtenClipboard, "## AI 工时与用量战报 · 上周\n")
        XCTAssertNil(store.reportError)

        // Simulate failure: clipboard MUST NOT be overwritten, reportError MUST be set, copiedToast false!
        store.copiedToast = false
        var failClipboardWriteCount = 0
        let expFail = expectation(description: "failure handled")
        store.commandExecutor = { _, _, _, _ in
            return .failure(NSError(domain: "AgentPulse", code: 2, userInfo: [NSLocalizedDescriptionKey: "参数错误：模拟失败"]))
        }
        store.pasteboardWriter = { _ in failClipboardWriteCount += 1 }
        store.copyMarkdownReport(period: .month)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            expFail.fulfill()
        }
        wait(for: [expFail], timeout: 3.0)
        XCTAssertEqual(failClipboardWriteCount, 0)
        XCTAssertEqual(store.reportError, "参数错误：模拟失败")
        XCTAssertFalse(store.copiedToast)
    }
}
