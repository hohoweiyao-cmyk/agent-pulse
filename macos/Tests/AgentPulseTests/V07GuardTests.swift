import Foundation
import XCTest
@testable import AgentPulse

@MainActor
final class V07GuardTests: XCTestCase {
    private var suiteName: String!
    private var defaults: UserDefaults!

    override func setUp() {
        super.setUp()
        suiteName = "dev.agentpulse.v07.guards.\(UUID().uuidString)"
        defaults = UserDefaults(suiteName: suiteName)!
    }

    override func tearDown() {
        defaults.removePersistentDomain(forName: suiteName)
        defaults = nil
        suiteName = nil
        super.tearDown()
    }

    private func fixtureURL(_ name: String) -> URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .appendingPathComponent("tests/fixtures/\(name)")
    }

    // MARK: - #9 Future Observation Timestamp Rejection & Reset Freshness Revocation

    func test09QuotaFutureObservationRejectedAndResetRevokesFreshness() throws {
        let store = ClockStore(defaults: defaults)
        let nowTs: Double = 1795762800.0 // 2026-11-27 15:00:00 +0800
        store.nowEpoch = nowTs

        // Claude 96% with future fetchedAt (+600s, simulated clock rollback) vs valid Codex 50%
        let futureClaude = QuotaProvider(
            id: "claude", name: "Claude", color: "#FB923C", plan: "Pro",
            status: "ok", statusText: "桌面端缓存", fetchedAt: Int(nowTs + 600),
            windows: [
                QuotaWindow(
                    id: "claude_5h", label: "5 小时窗口", shortLabel: "CL5",
                    usedPercent: 96.0, remainingPercent: 4.0,
                    resetsAt: Int(nowTs + 7200), windowMinutes: 300,
                    freshUntilTs: nowTs + 4200, pace: nil
                )
            ]
        )
        let validCodex = QuotaProvider(
            id: "codex", name: "Codex", color: "#10B981", plan: "Pro",
            status: "ok", statusText: "实时探测", fetchedAt: Int(nowTs - 30),
            windows: [
                QuotaWindow(
                    id: "codex_5h", label: "5 小时窗口", shortLabel: "C5",
                    usedPercent: 50.0, remainingPercent: 50.0,
                    resetsAt: Int(nowTs + 3600), windowMinutes: 300,
                    freshUntilTs: nowTs + 330, pace: nil
                )
            ]
        )
        let snap = QuotaSnapshot(
            schemaVersion: 1,
            generatedAt: "2026-11-27T15:00:00+08:00",
            generatedTs: nowTs,
            providers: [futureClaude, validCodex],
            highestWindow: nil
        )
        store.applyQuotaSnapshotIfNewer(snap)

        XCTAssertFalse(store.quotaWindowIsFresh(provider: futureClaude, window: futureClaude.windows[0]))
        XCTAssertTrue(store.quotaWindowIsFresh(provider: validCodex, window: validCodex.windows[0]))
        XCTAssertEqual(store.highestFreshQuotaWindow?.providerId, "codex")
        XCTAssertEqual(store.menuBarQuotaSummary, "C5 50%")

        // After reset time: live eligibility revoked while snapshot still preserves last reading & original fetchedAt
        store.handleClockTick(nowTs + 3605)
        XCTAssertFalse(store.quotaWindowIsFresh(provider: validCodex, window: validCodex.windows[0]))
        XCTAssertNil(store.highestFreshQuotaWindow)
        XCTAssertEqual(store.quotaSnapshot?.providers[1].fetchedAt, Int(nowTs - 30))
        XCTAssertEqual(store.quotaSnapshot?.providers[1].windows[0].usedPercent, 50.0)
    }

    // MARK: - #10 Already-Stale Midnight Crossing, Month Rollover & v0.6 Legacy Project Fallback

    func test10StaleBeforeMidnightTriggersCallbacksAndClearsCrossDayAndMonthProjections() throws {
        let store = ClockStore(defaults: defaults)
        var raw = String(decoding: try Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json")), as: UTF8.self)
        // Shift snapshot generated_ts to 2026-11-27 23:59:00 +0800 (1795795140.0)
        let preMidnightSnapTs: Double = 1795795140.0
        raw = raw.replacingOccurrences(of: "1795762800.0", with: String(preMidnightSnapTs))
        let snap = try JSONDecoder().decode(ClockSnapshot.self, from: Data(raw.utf8))

        // Configure $20/mo subscription for codex (November fixture has $40 candidate => 2.00x, Hermes $3.25)
        XCTAssertTrue(store.setSubscriptionFee(cents: 2000, for: "codex"))
        store.nowEpoch = preMidnightSnapTs + 10
        XCTAssertTrue(store.applySnapshotIfNewer(snap))
        XCTAssertEqual(store.subscriptionRows.first(where: { $0.tool == "codex" })?.paybackMultiple, Decimal(2))
        XCTAssertEqual(store.subscriptionRows.first(where: { $0.tool == "hermes" })?.apiActualUsd, Decimal(string: "3.25"))
        XCTAssertEqual(store.projectTrailRows(for: .today).count, 1)

        // Become stale BEFORE midnight (23:59:50 +0800)
        store.handleClockTick(preMidnightSnapTs + 50.0)
        XCTAssertEqual(store.fastFreshness, .stale)
        let beforeMidnightTitle = AppDelegate.formatStatusBarTitle(for: store)
        XCTAssertTrue(beforeMidnightTitle.contains("0.5h · 离线"))
        XCTAssertTrue(beforeMidnightTitle.contains("$1.25"))

        var statusCallbacks = 0
        var layoutCallbacks = 0
        store.onStatusBarPreferenceChanged = { statusCallbacks += 1 }
        store.onLayoutChanged = { layoutCallbacks += 1 }

        // Cross Shanghai midnight to 2026-11-28 00:00:01 +0800 (already stale!)
        let afterMidnightTs = preMidnightSnapTs + 61.0
        store.handleClockTick(afterMidnightTs)
        XCTAssertGreaterThanOrEqual(statusCallbacks, 1, "Crossing Shanghai midnight MUST notify status bar even when already stale")
        XCTAssertGreaterThanOrEqual(layoutCallbacks, 1, "Crossing Shanghai midnight MUST notify layout even when already stale")

        let afterMidnightTitle = AppDelegate.formatStatusBarTitle(for: store)
        XCTAssertFalse(afterMidnightTitle.contains("0.5h"))
        XCTAssertFalse(afterMidnightTitle.contains("$1.25"))
        XCTAssertTrue(afterMidnightTitle.contains("离线"))

        // Today projections must NOT leak 2026-11-27
        XCTAssertEqual(store.displayedDay?.date, "2026-11-28")
        XCTAssertEqual(store.displayedHourlyChartState, .unknown)
        XCTAssertTrue(store.projectTrailRows(for: .today).isEmpty)
        XCTAssertNil(store.periodStats(for: .today))
        XCTAssertEqual(store.hourlyDay(for: .today)?.date, "2026-11-28")
        // Yesterday projection cleanly maps to 2026-11-27
        XCTAssertEqual(store.periodStats(for: .yesterday)?.endDate, "2026-11-27")
        XCTAssertEqual(store.hourlyDay(for: .yesterday)?.date, "2026-11-27")
        // Still November -> month payback still available on 11/28
        XCTAssertTrue(store.currentMonthSubscriptionDataAvailable)

        // Advance across month boundary to 2026-12-01 00:00:01 +0800 (+3 days)
        statusCallbacks = 0
        layoutCallbacks = 0
        let dec1Ts = afterMidnightTs + 3 * 86400.0
        store.handleClockTick(dec1Ts)
        XCTAssertGreaterThanOrEqual(statusCallbacks, 1)
        XCTAssertGreaterThanOrEqual(layoutCallbacks, 1)
        XCTAssertFalse(store.currentMonthSubscriptionDataAvailable)
        XCTAssertNil(store.periodStats(for: .month))
        XCTAssertTrue(store.projectTrailRows(for: .month).isEmpty)

        // Subscription rows must NOT show November's $40 / 2.00x or Hermes $3.25!
        let decCodex = store.subscriptionRows.first(where: { $0.tool == "codex" })
        XCTAssertEqual(decCodex?.monthlyFeeCents, 2000)
        XCTAssertNil(decCodex?.subscriptionValueUsd)
        XCTAssertNil(decCodex?.paybackMultiple)
        XCTAssertNil(store.subscriptionRows.first(where: { $0.tool == "hermes" }))
    }

    func test10LegacyV06ProjectsNeverMasqueradeAsTodayOrMonth() throws {
        let store = ClockStore(defaults: defaults)
        // Build a v0.6-style payload with `usage.projects` present and `usage.project_trails` absent
        let v07Data = try Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json"))
        var obj = try JSONSerialization.jsonObject(with: v07Data) as! [String: Any]
        var usage = obj["usage"] as! [String: Any]
        usage.removeValue(forKey: "project_trails")
        usage["projects"] = [
            [
                "path": "/tmp/legacy-multi-day",
                "name": "legacy-multi-day",
                "display_path": "/tmp/legacy-multi-day",
                "last_active_date": "2026-11-27",
                "group": "today",
                "input_tokens": 500_000,
                "output_tokens": 10_000,
                "cache_read_tokens": 0,
                "cache_write_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 510_000,
                "cost_usd": 88.88,
                "cache_hit_rate": 0.0,
                "sessions": 12,
                "tools": [],
            ]
        ]
        obj["usage"] = usage
        let mutatedData = try JSONSerialization.data(withJSONObject: obj)
        let snap = try JSONDecoder().decode(ClockSnapshot.self, from: mutatedData)
        store.nowEpoch = snap.generatedTs + 5
        store.applySnapshotIfNewer(snap)

        XCTAssertTrue(store.isLegacyProjectFallbackOnlyAll)
        XCTAssertTrue(store.projectTrailRows(for: .today).isEmpty)
        XCTAssertTrue(store.projectTrailRows(for: .week).isEmpty)
        XCTAssertTrue(store.projectTrailRows(for: .month).isEmpty)
        XCTAssertEqual(store.projectTrailRows(for: .all).count, 1)
        XCTAssertEqual(store.projectTrailRows(for: .all).first?.stats.costUsd, 88.88)
    }

    // MARK: - #11 Unknown Work Status vs Observed Zero vs Retained Non-Zero

    func test11UnknownWorkStatusVsObservedZeroVsRetainedNonZero() throws {
        let baseData = try Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json"))

        func decodeVariant(workStatus: String, hourlyAvailable: Bool, agentSec: Double) throws -> ClockSnapshot {
            var obj = try JSONSerialization.jsonObject(with: baseData) as! [String: Any]
            var today = obj["today"] as! [String: Any]
            today["work_status"] = workStatus
            today["hourly_available"] = hourlyAvailable
            today["agent_seconds"] = agentSec
            today["agent_hours"] = agentSec / 3600.0
            obj["today"] = today
            obj["days"] = [today]
            obj["last_7_days"] = [today]
            obj["sentinel_tasks"] = []
            obj["active_agents"] = []
            return try JSONDecoder().decode(ClockSnapshot.self, from: try JSONSerialization.data(withJSONObject: obj))
        }

        // 1) Unknown (corrupt first scan, no prior bucket)
        let sUnk = ClockStore(defaults: defaults)
        let snapUnk = try decodeVariant(workStatus: "unknown", hourlyAvailable: false, agentSec: 0)
        sUnk.nowEpoch = snapUnk.generatedTs + 5
        sUnk.applySnapshotIfNewer(snapUnk)
        XCTAssertTrue(sUnk.isTodayWorkUnknown)
        XCTAssertTrue(sUnk.isDisplayedDayWorkUnknown)
        XCTAssertEqual(sUnk.displayedHourlyChartState, .unknown)
        XCTAssertNil(sUnk.displayedAgentSecondsIfKnown)
        XCTAssertEqual(sUnk.displayedHeroHoursText, "—")
        XCTAssertEqual(sUnk.todayMenuBarHoursText, "—")
        XCTAssertFalse(AppDelegate.formatStatusBarTitle(for: sUnk).contains("0.0h"))
        XCTAssertTrue(AppDelegate.formatStatusBarTitle(for: sUnk).contains("—"))

        // 2) Trusted empty (observed 0h)
        let sZero = ClockStore(defaults: defaults)
        let snapZero = try decodeVariant(workStatus: "observed", hourlyAvailable: true, agentSec: 0)
        sZero.nowEpoch = snapZero.generatedTs + 5
        sZero.applySnapshotIfNewer(snapZero)
        XCTAssertFalse(sZero.isTodayWorkUnknown)
        XCTAssertEqual(sZero.displayedHourlyChartState, .available)
        XCTAssertEqual(sZero.displayedAgentSecondsIfKnown, 0.0)
        XCTAssertEqual(sZero.displayedHeroHoursText, "0.00")
        XCTAssertEqual(sZero.todayMenuBarHoursText, "0.0h")

        // 3) Retained non-zero without hourly distribution
        let sRet = ClockStore(defaults: defaults)
        let snapRet = try decodeVariant(workStatus: "retained", hourlyAvailable: false, agentSec: 5400)
        sRet.nowEpoch = snapRet.generatedTs + 5
        sRet.applySnapshotIfNewer(snapRet)
        XCTAssertFalse(sRet.isTodayWorkUnknown)
        XCTAssertEqual(sRet.displayedDay?.isWorkRetained, true)
        XCTAssertEqual(sRet.displayedHourlyChartState, .unavailable)
        XCTAssertEqual(sRet.displayedHeroHoursText, "1.50")
    }

    // MARK: - #12 Heatmap & Project Periods Decode Unpriced Tokens (4 States)

    func test12HeatmapAndProjectPeriodsDecodeUnpricedFourStates() throws {
        let json = """
        {
          "days": [
            {"date":"2026-11-24","short_date":"11/24","weekday":"周二","is_today":false,"total_tokens":1000000,"cost_usd":0.0,"unpriced_tokens":1000000,"has_cost_observation":true,"agent_hours":0.0,"sessions":1,"tools":[{"tool":"codex","display_name":"Codex","color":"#34D399","total_tokens":1000000,"cost_usd":0.0,"unpriced_tokens":1000000,"has_cost_observation":true,"agent_hours":0.0}]},
            {"date":"2026-11-25","short_date":"11/25","weekday":"周三","is_today":false,"total_tokens":1000000,"cost_usd":1.5,"unpriced_tokens":500000,"has_cost_observation":true,"agent_hours":0.0,"sessions":1,"tools":[]},
            {"date":"2026-11-26","short_date":"11/26","weekday":"周四","is_today":false,"total_tokens":100000,"cost_usd":0.0,"unpriced_tokens":0,"has_cost_observation":true,"agent_hours":0.0,"sessions":1,"tools":[]},
            {"date":"2026-11-27","short_date":"11/27","weekday":"周五","is_today":true,"total_tokens":0,"cost_usd":0.0,"unpriced_tokens":0,"has_cost_observation":false,"agent_hours":0.0,"sessions":0,"tools":[]}
          ],
          "current_streak": 1,
          "longest_streak": 1,
          "total_active_days": 3
        }
        """
        let hm = try JSONDecoder().decode(HeatmapPayload.self, from: Data(json.utf8))
        let formatted = hm.days.map {
            Formatters.estimatedUSD($0.costUsd, unpricedTokens: $0.effectiveUnpricedTokens, hasObservation: $0.effectiveHasCostObservation)
        }
        XCTAssertEqual(formatted, ["未计价", "$1.50+", "$0.00", "—"])
        XCTAssertEqual(hm.days[0].tools[0].effectiveUnpricedTokens, 1_000_000)

        let ppJson = """
        {"start_date":"2026-11-25","end_date":"2026-11-27","last_active_date":"2026-11-25","agent_seconds":null,"input_tokens":1000000,"output_tokens":0,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":0,"total_tokens":1000000,"cost_usd":1.5,"unpriced_tokens":500000,"has_cost_observation":true,"cache_hit_rate":0.0,"sessions":1,"tools":[],"worktree_sources":[]}
        """
        let pp = try JSONDecoder().decode(ProjectPeriodStats.self, from: Data(ppJson.utf8))
        XCTAssertEqual(pp.effectiveUnpricedTokens, 500_000)
        XCTAssertTrue(pp.effectiveHasCostObservation)
        XCTAssertEqual(Formatters.estimatedUSD(pp.costUsd, unpricedTokens: pp.effectiveUnpricedTokens, hasObservation: pp.effectiveHasCostObservation), "$1.50+")
    }

    // MARK: - #13 Symlink Target Containing Control Characters Rejected + Fallback

    func test13SymlinkToControlCharDirRejectedAndFallsBackToProjectPath() throws {
        let tmp = FileManager.default.temporaryDirectory.appendingPathComponent("ap-sym-\(UUID())", isDirectory: true)
        try FileManager.default.createDirectory(at: tmp, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: tmp) }

        let badTarget = tmp.appendingPathComponent("evil\u{0007}dir", isDirectory: true)
        try FileManager.default.createDirectory(at: badTarget, withIntermediateDirectories: true)
        let safeParent = tmp.appendingPathComponent("safe-project", isDirectory: true)
        try FileManager.default.createDirectory(at: safeParent, withIntermediateDirectories: true)

        let cleanLinkToBad = tmp.appendingPathComponent("clean-workspace-link")
        try FileManager.default.createSymbolicLink(at: cleanLinkToBad, withDestinationURL: badTarget)

        // Direct validation MUST reject the symlink after resolving to control-char target
        XCTAssertNil(ClockStore.validatedProjectDirectory(cleanLinkToBad.path))

        let store = ClockStore(defaults: defaults)
        var revealed: [URL] = []
        store.finderRevealer = { urls in revealed = urls }

        // Task with bad symlink workspacePath + valid projectPath -> rejects symlink, falls back to safeParent ONLY
        let taskWithFallback = SentinelTask(
            id: "sym1", sourceId: "work.t3", tool: "t3_codex", displayName: "T3", shortName: "T3",
            color: "#10B981", label: "sym", state: .running, startedAt: 1, stateSinceTs: 1,
            expiresAt: 100, elapsedSeconds: 10, projectPath: safeParent.path,
            workspacePath: cleanLinkToBad.path, evidence: nil
        )
        store.openSentinelTask(taskWithFallback)
        XCTAssertNil(store.projectActionError)
        XCTAssertEqual(revealed.count, 1)
        XCTAssertEqual(revealed.first?.path, safeParent.resolvingSymlinksInPath().path)

        // Both workspacePath and projectPath resolve to control-char dir -> reveals NOTHING and sets error
        revealed = []
        let taskNoFallback = SentinelTask(
            id: "sym2", sourceId: "work.t3", tool: "t3_codex", displayName: "T3", shortName: "T3",
            color: "#10B981", label: "sym", state: .running, startedAt: 1, stateSinceTs: 1,
            expiresAt: 100, elapsedSeconds: 10, projectPath: cleanLinkToBad.path,
            workspacePath: cleanLinkToBad.path, evidence: nil
        )
        store.openSentinelTask(taskNoFallback)
        XCTAssertTrue(revealed.isEmpty)
        XCTAssertEqual(store.projectActionError, "项目目录不可用")
    }

    // MARK: - Round-2 #5 Offline Quota updated=0 & 69.96% Unrounded Alert Threshold

    func testR2_05QuotaUpdatedZeroRejectedAnd6996ThresholdPrecision() throws {
        let store = ClockStore(defaults: defaults)
        let nowTs: Double = 1795762800.0
        store.nowEpoch = nowTs

        // 1) Offline Claude cache with fetchedAt=0, 97% -> MUST NOT be fresh or enter highest/badge
        let zeroUpdatedJson = """
        {
          "schema_version": 1,
          "generated_at": "2026-11-27T15:00:00+08:00",
          "generated_ts": \(nowTs),
          "providers": [
            {
              "id": "claude", "name": "Claude", "color": "#FB923C", "plan": "Pro",
              "status": "stale", "status_text": "缺少有效观测时间", "fetched_at": 0,
              "windows": [
                {"id":"claude_5h","label":"5 小时窗口","short_label":"CL5","used_percent":97.0,"raw_used_percent":97.0,"remaining_percent":3.0,"resets_at":\(Int(nowTs + 3600)),"window_minutes":300}
              ]
            }
          ],
          "highest_window": null
        }
        """
        let snapZero = try JSONDecoder().decode(QuotaSnapshot.self, from: Data(zeroUpdatedJson.utf8))
        store.applyQuotaSnapshotIfNewer(snapZero)
        XCTAssertFalse(store.quotaWindowIsFresh(provider: snapZero.providers[0], window: snapZero.providers[0].windows[0]))
        XCTAssertNil(store.highestFreshQuotaWindow)
        XCTAssertNil(store.activeAlertQuotaWindow)
        XCTAssertNil(store.menuBarQuotaSummary)

        // 2) Fresh Claude 69.96% (rounded display 70.0%) vs Codex 69.98% -> 69.98% ranks first, NEITHER triggers >= 70% alert!
        let sub70Json = """
        {
          "schema_version": 1,
          "generated_at": "2026-11-27T15:01:00+08:00",
          "generated_ts": \(nowTs + 60),
          "providers": [
            {
              "id": "claude", "name": "Claude", "color": "#FB923C", "plan": "Pro",
              "status": "ok", "status_text": "Claude Code 状态栏", "fetched_at": \(Int(nowTs + 30)),
              "windows": [
                {"id":"claude_5h","label":"5 小时窗口","short_label":"CL5","used_percent":70.0,"raw_used_percent":69.96,"remaining_percent":30.0,"resets_at":\(Int(nowTs + 7200)),"window_minutes":300,"fresh_until_ts":\(nowTs + 3630)}
              ]
            },
            {
              "id": "codex", "name": "Codex", "color": "#10B981", "plan": "Pro",
              "status": "ok", "status_text": "实时探测", "fetched_at": \(Int(nowTs + 30)),
              "windows": [
                {"id":"codex_5h","label":"5 小时窗口","short_label":"C5","used_percent":70.0,"raw_used_percent":69.98,"remaining_percent":30.0,"resets_at":\(Int(nowTs + 7200)),"window_minutes":300,"fresh_until_ts":\(nowTs + 390)}
              ]
            }
          ],
          "highest_window": {"provider_id":"codex","provider_name":"Codex","label":"5 小时窗口","short_label":"C5","used_percent":70.0,"raw_used_percent":69.98,"resets_at":\(Int(nowTs + 7200))}
        }
        """
        store.nowEpoch = nowTs + 65
        let snapSub70 = try JSONDecoder().decode(QuotaSnapshot.self, from: Data(sub70Json.utf8))
        store.applyQuotaSnapshotIfNewer(snapSub70)
        XCTAssertEqual(snapSub70.providers[0].windows[0].effectiveThresholdPercent, 69.96, accuracy: 0.0001)
        XCTAssertFalse(snapSub70.providers[0].windows[0].isAboveAlertThreshold)
        XCTAssertEqual(store.highestFreshQuotaWindow?.providerId, "codex")
        XCTAssertEqual(store.highestFreshQuotaWindow?.effectiveThresholdPercent ?? 0, 69.98, accuracy: 0.0001)
        XCTAssertFalse(store.highestFreshQuotaWindow?.isAboveAlertThreshold ?? true)
        XCTAssertNil(store.activeAlertQuotaWindow)

        // 3) Exact 70.00% -> triggers alert!
        let at70Json = sub70Json
            .replacingOccurrences(of: "\"generated_ts\": \(nowTs + 60)", with: "\"generated_ts\": \(nowTs + 120)")
            .replacingOccurrences(of: "69.98", with: "70.0")
        store.nowEpoch = nowTs + 125
        let snapAt70 = try JSONDecoder().decode(QuotaSnapshot.self, from: Data(at70Json.utf8))
        store.applyQuotaSnapshotIfNewer(snapAt70)
        XCTAssertTrue(store.highestFreshQuotaWindow?.isAboveAlertThreshold ?? false)
        XCTAssertEqual(store.activeAlertQuotaWindow?.providerId, "codex")
    }

    // MARK: - Round-2 #6 Future Month Rollback & Incomplete Last-Week Rejection

    func testR2_06ClockRollbackRejectsFutureMonthAndIncompleteLastWeek() throws {
        let store = ClockStore(defaults: defaults)
        let data = try Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json"))
        let snapFri = try JSONDecoder().decode(ClockSnapshot.self, from: data) // today=2026-11-27 (Fri), week=11/23..11/27, month=11/01..11/27 ($40)
        XCTAssertTrue(store.setSubscriptionFee(cents: 2000, for: "codex"))
        store.nowEpoch = snapFri.generatedTs + 5
        store.applySnapshotIfNewer(snapFri)
        XCTAssertEqual(store.subscriptionRows.first(where: { $0.tool == "codex" })?.paybackMultiple, Decimal(2))

        // 1) Roll back Shanghai clock 1 day to 2026-11-26 15:00 +0800 -> MUST reject 11/27 future month/week/today/projects/payback!
        store.handleClockTick(snapFri.generatedTs - 86400.0)
        XCTAssertEqual(store.currentShanghaiDay, "2026-11-26")
        XCTAssertNil(store.periodStats(for: .today))
        XCTAssertNil(store.periodStats(for: .week))
        XCTAssertNil(store.periodStats(for: .month))
        XCTAssertNil(store.periodStats(for: .year))
        XCTAssertNil(store.periodStats(for: .all))
        XCTAssertFalse(store.currentMonthSubscriptionDataAvailable)
        let rolledCodex = store.subscriptionRows.first(where: { $0.tool == "codex" })
        XCTAssertNil(rolledCodex?.subscriptionValueUsd)
        XCTAssertNil(rolledCodex?.paybackMultiple)
        XCTAssertTrue(store.projectTrailRows(for: .month).isEmpty)
        XCTAssertNotNil(store.periodUnavailableHint(for: .month))

        // 2) Keep Friday 2026-11-27 snapshot until Monday 2026-11-30 15:00 +0800 (+3 days) -> lastWeek MUST NOT return partial 23..27!
        let monTs = snapFri.generatedTs + 3 * 86400.0
        store.handleClockTick(monTs)
        XCTAssertEqual(store.currentShanghaiDay, "2026-11-30")
        XCTAssertNil(store.periodStats(for: .lastWeek))
        let lwHint = store.periodUnavailableHint(for: .lastWeek) ?? ""
        XCTAssertTrue(lwHint.contains("2026-11-27") && lwHint.contains("2026-11-29"))

        // 3) Complete Sunday 2026-11-29 snapshot (week covers 2026-11-23..2026-11-29) on Monday 2026-11-30 -> valid lastWeek & export!
        var obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let sunTs = snapFri.generatedTs + 2 * 86400.0
        obj["generated_ts"] = sunTs
        var todayObj = obj["today"] as! [String: Any]
        todayObj["date"] = "2026-11-29"
        obj["today"] = todayObj
        var usageObj = obj["usage"] as! [String: Any]
        var periodsObj = usageObj["periods"] as! [String: Any]
        var weekObj = periodsObj["week"] as! [String: Any]
        weekObj["start_date"] = "2026-11-23"
        weekObj["end_date"] = "2026-11-29"
        periodsObj["week"] = weekObj
        usageObj["periods"] = periodsObj
        obj["usage"] = usageObj
        let snapSun = try JSONDecoder().decode(ClockSnapshot.self, from: try JSONSerialization.data(withJSONObject: obj))

        let storeSun = ClockStore(defaults: defaults)
        storeSun.nowEpoch = monTs
        storeSun.applySnapshotIfNewer(snapSun)
        let validLW = storeSun.periodStats(for: .lastWeek)
        XCTAssertNotNil(validLW)
        XCTAssertEqual(validLW?.startDate, "2026-11-23")
        XCTAssertEqual(validLW?.endDate, "2026-11-29")
        XCTAssertNil(storeSun.periodUnavailableHint(for: .lastWeek))
    }

    // MARK: - Round-2 #7 HeatmapDay Decodes Unknown Work Status

    func testR2_07HeatmapDayDecodesUnknownWorkStatusAndLegacyDefaultsFalse() throws {
        let json = """
        {
          "days": [
            {"date":"2026-11-24","short_date":"11/24","weekday":"周二","is_today":false,"total_tokens":0,"cost_usd":0.0,"unpriced_tokens":0,"has_cost_observation":false,"agent_hours":0.0,"work_status":"observed","sessions":0,"tools":[]},
            {"date":"2026-11-25","short_date":"11/25","weekday":"周三","is_today":false,"total_tokens":0,"cost_usd":0.0,"unpriced_tokens":0,"has_cost_observation":false,"agent_hours":1.5,"work_status":"retained","sessions":0,"tools":[]},
            {"date":"2026-11-26","short_date":"11/26","weekday":"周四","is_today":false,"total_tokens":0,"cost_usd":0.0,"unpriced_tokens":0,"has_cost_observation":false,"agent_hours":0.0,"sessions":0,"tools":[]},
            {"date":"2026-11-27","short_date":"11/27","weekday":"周五","is_today":true,"total_tokens":0,"cost_usd":0.0,"unpriced_tokens":0,"has_cost_observation":false,"agent_hours":0.0,"work_status":"unknown","sessions":0,"tools":[]}
          ],
          "current_streak": 0,
          "longest_streak": 0,
          "total_active_days": 0
        }
        """
        let hm = try JSONDecoder().decode(HeatmapPayload.self, from: Data(json.utf8))
        XCTAssertFalse(hm.days[0].isWorkUnknown)
        XCTAssertFalse(hm.days[1].isWorkUnknown)
        XCTAssertTrue(hm.days[1].isWorkRetained)
        XCTAssertFalse(hm.days[2].isWorkUnknown) // legacy without work_status
        XCTAssertTrue(hm.days[3].isWorkUnknown)
    }

    // MARK: - Round-2 #8 Sub-cent ($0.00175) & Zero-Priced Mixed Cost Formatting

    func testR2_08SubcentAndZeroPricedMixedCostFormattingAllSurfaces() throws {
        // Direct formatter matrix: sub-cent mixed, zero-priced mixed, pure unpriced, confirmed zero, no observation
        XCTAssertEqual(Formatters.estimatedUSD(0.00175, unpricedTokens: 1000, hasObservation: true, totalTokens: 2000), "$0.00+")
        XCTAssertEqual(Formatters.estimatedUSD(0.00175, unpricedTokens: 1000, hasObservation: true), "$0.00+")
        XCTAssertEqual(Formatters.estimatedUSD(0.0, unpricedTokens: 1000, hasObservation: true, totalTokens: 2000), "$0.00+")
        XCTAssertEqual(Formatters.estimatedUSD(0.0, unpricedTokens: 1000, hasObservation: true, totalTokens: 1000), "未计价")
        XCTAssertEqual(Formatters.estimatedUSD(0.0, unpricedTokens: 0, hasObservation: true, totalTokens: 1000), "$0.00")
        XCTAssertEqual(Formatters.estimatedUSD(0.0, unpricedTokens: 0, hasObservation: false, totalTokens: 0), "—")

        // Verify menu bar + ClockStore + HeatmapDay + ProjectPeriodStats consistency for sub-cent & mixed zero-priced
        let baseData = try Data(contentsOf: fixtureURL("v07_fast_v3_minimal.json"))
        var obj = try JSONSerialization.jsonObject(with: baseData) as! [String: Any]
        var usage = obj["usage"] as! [String: Any]
        var periods = usage["periods"] as! [String: Any]
        var pToday = periods["today"] as! [String: Any]
        pToday["total_tokens"] = 2000
        pToday["cost_usd"] = 0.00175
        pToday["unpriced_tokens"] = 1000
        periods["today"] = pToday
        usage["periods"] = periods
        obj["usage"] = usage
        let snapSub = try JSONDecoder().decode(ClockSnapshot.self, from: try JSONSerialization.data(withJSONObject: obj))

        let store = ClockStore(defaults: defaults)
        store.nowEpoch = snapSub.generatedTs + 5
        store.applySnapshotIfNewer(snapSub)
        XCTAssertEqual(store.todayEstimatedCostText, "$0.00+")
        XCTAssertTrue(AppDelegate.formatStatusBarTitle(for: store).contains("$0.00+"))
        XCTAssertEqual(snapSub.usage?.periods.today.formattedEstimatedCost, "$0.00+")

        // Heatmap & ProjectPeriodStats mixed zero-priced (cost=0.0, total=2000, unpriced=1000 -> "$0.00+")
        let hmJson = """
        {"date":"2026-11-27","short_date":"11/27","weekday":"周五","is_today":true,"total_tokens":2000,"cost_usd":0.0,"unpriced_tokens":1000,"has_cost_observation":true,"agent_hours":0.0,"work_status":"observed","sessions":1,"tools":[{"tool":"codex","display_name":"Codex","color":"#34D399","total_tokens":2000,"cost_usd":0.00175,"unpriced_tokens":1000,"has_cost_observation":true,"agent_hours":0.0}]}
        """
        let hmDay = try JSONDecoder().decode(HeatmapDay.self, from: Data(hmJson.utf8))
        XCTAssertEqual(hmDay.formattedEstimatedCost, "$0.00+")
        XCTAssertEqual(hmDay.tools[0].formattedEstimatedCost, "$0.00+")

        let ppJson = """
        {"start_date":"2026-11-27","end_date":"2026-11-27","last_active_date":"2026-11-27","agent_seconds":null,"input_tokens":2000,"output_tokens":0,"cache_read_tokens":0,"cache_write_tokens":0,"reasoning_tokens":0,"total_tokens":2000,"cost_usd":0.0,"unpriced_tokens":1000,"has_cost_observation":true,"cache_hit_rate":0.0,"sessions":1,"tools":[],"worktree_sources":[]}
        """
        let pp = try JSONDecoder().decode(ProjectPeriodStats.self, from: Data(ppJson.utf8))
        XCTAssertEqual(pp.formattedEstimatedCost, "$0.00+")
    }
}
