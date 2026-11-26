import AppKit
import Combine
import Foundation
import UserNotifications

@MainActor
final class SitReminderManager: NSObject, ObservableObject {
    @Published var isEnabled: Bool {
        didSet {
            UserDefaults.standard.set(isEnabled, forKey: Keys.enabled)
            if isEnabled {
                resetTimer()
                requestNotificationPermissionIfNeeded()
            }
        }
    }

    @Published var intervalMinutes: Int {
        didSet {
            let clamped = max(1, min(180, intervalMinutes))
            if clamped != intervalMinutes {
                intervalMinutes = clamped
                return
            }
            UserDefaults.standard.set(intervalMinutes, forKey: Keys.interval)
            resetTimer()
        }
    }

    @Published var resetOnSleep: Bool {
        didSet {
            UserDefaults.standard.set(resetOnSleep, forKey: Keys.resetOnSleep)
        }
    }

    @Published private(set) var remainingSeconds: Int = 3600
    @Published private(set) var isDue: Bool = false

    private var sleepStartedAt: Date?
    private var lastNotificationAt: Date?
    private var observers: [NSObjectProtocol] = []

    private enum Keys {
        static let enabled = "agentPulse.sit.enabled"
        static let interval = "agentPulse.sit.intervalMinutes"
        static let resetOnSleep = "agentPulse.sit.resetOnSleep"
        static let migratedSeeded = "agentPulse.sit.migratedSeeded"
    }

    private enum NotificationIds {
        static let category = "AGENT_PULSE_SIT_REMINDER"
        static let actionReset = "SIT_ACTION_RESET"
        static let actionSnooze = "SIT_ACTION_SNOOZE"
    }

    override init() {
        let defaults = UserDefaults.standard

        // One-time seed from migrated_prefs.json if available (supports legacy ~/.agent-clock fallback)
        if !defaults.bool(forKey: Keys.migratedSeeded) {
            let home = FileManager.default.homeDirectoryForCurrentUser
            let candidates = [
                home.appendingPathComponent(".agent-pulse/migrated_prefs.json"),
                home.appendingPathComponent(".agent-clock/migrated_prefs.json")
            ]
            if let prefsURL = candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }),
               let data = try? Data(contentsOf: prefsURL),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                if let on = obj["sit_reminder_enabled"] as? Bool {
                    defaults.set(on, forKey: Keys.enabled)
                }
                if let mins = obj["sit_reminder_interval_minutes"] as? Int, mins >= 15 {
                    defaults.set(mins, forKey: Keys.interval)
                }
                defaults.set(true, forKey: Keys.migratedSeeded)
            }
        }

        let enabled = defaults.object(forKey: Keys.enabled) as? Bool ?? true
        let rawInterval = defaults.object(forKey: Keys.interval) as? Int ?? 60
        let clampedInterval = max(1, min(180, rawInterval))
        let resetSleep = defaults.object(forKey: Keys.resetOnSleep) as? Bool ?? true

        self.isEnabled = enabled
        self.intervalMinutes = clampedInterval
        self.resetOnSleep = resetSleep
        self.remainingSeconds = clampedInterval * 60

        super.init()
        setupSleepObservers()
        setupNotificationCategory()
        if isEnabled {
            requestNotificationPermissionIfNeeded()
        }
    }

    deinit {
        let nc = NSWorkspace.shared.notificationCenter
        for obs in observers {
            nc.removeObserver(obs)
        }
    }

    var shortCountdownText: String {
        if !isEnabled { return "已关闭" }
        if isDue { return "该起身了" }
        let mins = max(1, Int(ceil(Double(remainingSeconds) / 60.0)))
        return "\(mins)m 后起身"
    }

    func tickOneSecond() {
        guard isEnabled else { return }
        if remainingSeconds > 0 {
            remainingSeconds -= 1
            if remainingSeconds == 0 {
                triggerReminder()
            }
        }
    }

    func resetTimer() {
        isDue = false
        remainingSeconds = max(60, intervalMinutes * 60)
    }

    func snooze(minutes: Int = 10) {
        isDue = false
        remainingSeconds = max(60, minutes * 60)
    }

    private func triggerReminder() {
        isDue = true
        let now = Date()
        if let last = lastNotificationAt, now.timeIntervalSince(last) < 30 {
            return
        }
        lastNotificationAt = now

        NSSound(named: NSSound.Name("Glass"))?.play()

        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        let content = UNMutableNotificationContent()
        content.title = "久坐提醒 · 起身活动一下"
        content.body = "你已连续专注 \(intervalMinutes) 分钟，喝口水、拉伸一下再继续吧。"
        content.sound = .default
        content.categoryIdentifier = NotificationIds.category

        let request = UNNotificationRequest(
            identifier: "agent-pulse-sit-\(Int(now.timeIntervalSince1970))",
            content: content,
            trigger: nil
        )
        UNUserNotificationCenter.current().add(request)
    }

    private func setupSleepObservers() {
        let nc = NSWorkspace.shared.notificationCenter
        let onSleep: @Sendable (Notification) -> Void = { [weak self] _ in
            Task { @MainActor in
                self?.sleepStartedAt = Date()
            }
        }
        let onWake: @Sendable (Notification) -> Void = { [weak self] _ in
            Task { @MainActor in
                guard let self, self.resetOnSleep else { return }
                if let sleptAt = self.sleepStartedAt, Date().timeIntervalSince(sleptAt) >= 300 {
                    self.resetTimer()
                }
                self.sleepStartedAt = nil
            }
        }
        observers.append(nc.addObserver(forName: NSWorkspace.willSleepNotification, object: nil, queue: .main, using: onSleep))
        observers.append(nc.addObserver(forName: NSWorkspace.screensDidSleepNotification, object: nil, queue: .main, using: onSleep))
        observers.append(nc.addObserver(forName: NSWorkspace.didWakeNotification, object: nil, queue: .main, using: onWake))
        observers.append(nc.addObserver(forName: NSWorkspace.screensDidWakeNotification, object: nil, queue: .main, using: onWake))
    }

    private func setupNotificationCategory() {
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        let resetAction = UNNotificationAction(
            identifier: NotificationIds.actionReset,
            title: "已起身，重置计时",
            options: []
        )
        let snoozeAction = UNNotificationAction(
            identifier: NotificationIds.actionSnooze,
            title: "推迟 10 分钟",
            options: []
        )
        let cat = UNNotificationCategory(
            identifier: NotificationIds.category,
            actions: [resetAction, snoozeAction],
            intentIdentifiers: [],
            options: []
        )
        center.setNotificationCategories([cat])
    }

    private func requestNotificationPermissionIfNeeded() {
        guard Bundle.main.bundleURL.pathExtension == "app" else { return }
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }
}

extension SitReminderManager: UNUserNotificationCenterDelegate {
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let actionId = response.actionIdentifier
        Task { @MainActor in
            if actionId == NotificationIds.actionSnooze {
                self.snooze(minutes: 10)
            } else {
                self.resetTimer()
            }
        }
        completionHandler()
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .sound])
    }
}
