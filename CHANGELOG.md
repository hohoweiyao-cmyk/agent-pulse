# Changelog

All notable changes to **AgentPulse** (formerly *AgentClock*) are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.4.0] - 2026-11-26

### Changed
- **Project Rebrand to AgentPulse**: Unified the app, SwiftPM target, bundle identifier (`dev.agentpulse.app`, overridable via `AGENTPULSE_BUNDLE_ID`), CLI, and documentation under **AgentPulse** to reflect its expanded all-in-one scope (AI work hours, parallel leverage, token & cost ledger, subscription quotas, and sit reminder).
- **Data Directory (`~/.agent-pulse/`) with Safe Auto-Seeding**: Moved runtime storage to `~/.agent-pulse/`. On first run, existing data from legacy `~/.agent-clock/` is automatically copied read-only without modifying or deleting the legacy directory.
- **Public Open-Source Release**: Added bilingual documentation (`README.md`, `README.zh-CN.md`), `LICENSE` (MIT), `SECURITY.md`, `CONTRIBUTING.md`, `docs/ARCHITECTURE.md`, `docs/PROVIDERS.md`, and GitHub Issue/PR templates.

## [0.3.2] - 2026-11-26

### Added
- **ZCode local usage tracking**: Reads `~/.zcode/cli/db/db.sqlite` (`model_usage`) via WAL-safe snapshots and attributes per-model tokens and estimated costs.
- **Claude Code CLI statusline quota hook**: Added `quota_collector.py --claude-statusline` to ingest 5-hour and 7-day utilization windows from Claude Code's official statusline JSON while keeping Claude Desktop cache fallback.

### Changed
- **Codex usage parser**: Added support for newer `token_usage_record` events in plain Codex JSONL logs, deduplicating same-session cumulative snapshots and repeated `response_id` entries across old/new event orders.

## [0.3.1] - 2026-11-26

### Fixed
- **Tokei migration safety**: Rejects invalid or empty Tokei ledgers without overwriting existing ledgers, returning a non-zero exit code on failure.
- **Ledger reconciliation**: Preserves imported Codex daily records on days where no live Codex or T3 Codex usage was captured.
- **Quota status clarity**: Explicitly marks snapshot-only providers (`Antigravity`, `Kimi`, `Grok`) as cached/stale and excludes stale readings from live high-quota alert banners.

## [0.3.0] - 2026-11-26

### Added
- **Native Token & Cost Engine** (`engine/usage_collector.py`, `engine/pricing.py`, `engine/ledger.py`): Zero-dependency incremental local log parser with 600+ model pricing catalog, custom `pricing_overrides.json` support, persistent ledger, `Today / Week / Month / Year` aggregation, and `T3 Code ↔ Codex` bidirectional deduplication.
- **Safe Tokei History Migrator** (`scripts/import_tokei.py`): Takes a full backup of `~/.tokei/` before idempotently importing historical daily usage, quota caches, and sit-reminder preferences.
- **Independent Slow-Track Quota Monitor** (`engine/quota_collector.py`): Polls subscription quotas every 5 minutes asynchronously (Codex live API + local JSONL fallback, Claude Desktop cache, plus cached snapshot display for Antigravity / Kimi / Grok) with silent fallback on failure.
- **Native 4-Tab Menu Bar Popover** (`PopoverViews.swift`): Left-click menu bar icon opens **Overview / Quotas / History / Settings** tabs; right-click retains the quick actions menu.
- **Built-in Sit Reminder** (`SitReminderManager.swift`): Configurable 30 / 45 / 60 / 90 min intervals, automatic reset after > 5 min sleep/lock, and native macOS notifications with Reset / Snooze 10m actions.

### Changed
- **Desktop Widget Upgrade** (`WidgetViews.swift`): Replaced passive Tokei reference footer with live native token/cost pill, synced 7-day bar chart selection with daily token/cost totals, and added an optional high-quota alert strip (≥ 70%).
- **Unified Markdown Daily Report**: Single-click copy now includes work hours, share, runs, tokens, and estimated USD cost per AI client.

## [0.2.1] - 2026-11-25

### Fixed
- Fixed dragging on the borderless desktop widget window by adding an explicit `WindowDragRegion` on the top header and blank space, and preserving top-edge window anchor across restarts.

## [0.2.0] - 2026-11-23

### Changed
- **Step-Heartbeat Pure AI Execution Algorithm**:
  - Removed the artificial 30-minute per-turn cap so continuous autonomous runs (45m / 2h+) are counted in full.
  - Dropped silent intra-turn gaps longer than 5 minutes (`0s` added) instead of inflating work hours with idle waits.
  - T3 Code collector now inspects `turn_items` and `runtime_requests` to explicitly subtract `approval_request`, `user_input_request`, and `secret_request` intervals and pauses live active status while waiting on the user.
  - Upgraded incremental segment cache to `v3`.

## [0.1.0] - 2026-11-23

### Added
- Initial release (originally named AgentClock): native macOS SwiftUI + AppKit desktop widget and menu bar app.
- Multi-source local read-only collector covering T3 Code (Antigravity / Codex / Claude), standalone Codex, WorkBuddy, Hermes (main + profiles), standalone Antigravity, Claude Code, Pi, and OpenCode.
- Desktop widget supporting 3 window layers (Desktop / Floating / Normal), Full / Compact views, live active agent timers, tool breakdown bar, interactive 7-day chart, and one-click Markdown daily report.
