<div align="center">

# AgentPulse

**A lightweight native macOS desktop widget & menu bar app for tracking AI agent work hours, parallel leverage, token usage, estimated costs, subscription quotas, and sit reminders — 100% local-first.**

[English](README.md) | [简体中文](README.zh-CN.md)

[![macOS 13+](https://img.shields.io/badge/macOS-13.0%2B-000000?logo=apple&logoColor=white)](https://www.apple.com/macos/)
[![Swift 5.9+](https://img.shields.io/badge/Swift-5.9%2B-F05138?logo=swift&logoColor=white)](https://swift.org/)
[![Python 3.9+ stdlib](https://img.shields.io/badge/Python-3.9%2B%20stdlib%20only-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-22C55E.svg)](LICENSE)

</div>

---

## Why AgentPulse?

When you work with multiple AI coding agents in parallel — **T3 Code**, **Codex**, **Claude Code**, **Antigravity**, **WorkBuddy**, **Hermes**, **Pi**, **OpenCode**, **Gemini CLI**, **ZCode** — their activity, token consumption, and subscription quotas are scattered across local JSONL logs and SQLite databases.

**AgentPulse** brings all your AI agents' vitals into one calm, native macOS HUD and answers four questions at a glance:

1. **How long did my AI agents actually work today?** — Measured with a **Step-Heartbeat Pure AI Execution** algorithm (excluding human reading/typing/approval waits, with no artificial 30-minute turn cap).
2. **How much parallel leverage am I getting?** — Compares cumulative AI hours against deduplicated wall-clock time (e.g., two agents working 30 minutes simultaneously = **1.00h AI work**, **0.50h wall clock**, **2.00× leverage**).
3. **How many tokens and how much API-equivalent cost did I burn?** — Zero-dependency incremental local log parser + 600+ model pricing catalog + persistent multi-period ledger (`Today / Week / Month / Year`).
4. **How much subscription quota is left — and when should I stand up?** — Non-blocking slow-track quota monitor + built-in macOS sit reminder.

---

## Key Features

- **Step-Heartbeat Pure AI Execution**:
  - Measures genuine autonomous AI execution across turns using a **5-minute inter-step heartbeat window**.
  - Continuous long autonomous runs (45 min, 2 hours, etc.) are counted in full — no arbitrary 30-minute turn cap.
  - Silent intra-turn pauses > 5 minutes are dropped (`0s`), and explicit human waits in T3 Code (`approval_request`, `user_input_request`, `secret_request`) are subtracted even when shorter than 5 minutes.
- **Unified Token, Cost, Project Trail & Multi-Period Ledger**:
  - Zero-dependency Python 3.9+ incremental parser with file `mtime_ns + size` caching (`< 200ms` per scan).
  - Bundled pricing baseline (`engine/pricing.json`, 600+ models) plus custom user overrides at `~/.agent-pulse/pricing_overrides.json`.
  - Persistent local ledger at `~/.agent-pulse/ledger.json` supporting **7 time periods** (`Today / Yesterday / Week / Last Week / Month / Year / All`), a **26-week activity heatmap** with streak tracking, cross-tool model leaderboards, and **Project Trail** (`cwd`-based project cost & token tracking).
- **T3 Code ↔ Codex Bidirectional Deduplication**:
  - Sessions launched inside T3 Code (`originator == "T3 Code"`) are attributed to `T3 · Codex` and excluded from standalone `Codex` for **both** work hours and token/cost accounting — zero double counting.
- **Fast / Slow Dual-Track Architecture**:
  - **Fast track (every 15s, 100% local)**: Work hours, live running agents, incremental tokens, cost & project trail.
  - **Slow track (every 5 min, non-blocking)**: Subscription quotas for Codex (official API or local JSONL fallback) and Claude (Claude Code CLI statusline hook + Claude Desktop cache), plus snapshot display for Antigravity / Kimi / Grok.
- **Native macOS Aesthetic (SwiftUI + AppKit)**:
  - **Desktop Widget Card**: Frosted HUD glass (`340pt` full / `276pt` compact), 3 window layers (*Desktop* / *Floating* / *Normal*), draggable top header with position memory, live agent timer, interactive 7-day bar chart, optional high-quota alert bar (≥ 70%), and one-click Markdown daily report copy.
  - **Single Menu Bar Icon & Adaptive 5-Tab Popover**: Live hours, active agent count, daily cost, and quota badges. Left-click opens a dynamic-width 5-tab native `NSPopover` (**Detail Cards / Dashboard & Heatmap / Projects / Quotas / Settings**):
    - **Detail Cards**: 2-column per-tool cards showing AI work hours, sessions, Cache Hit ring, full 5-part token grid (`Input / Output / Cache Read / Cache Write / Reasoning`), inline 5h/7d quota bars, and **2-level expandable model breakdowns** with per-1M-token pricing references.
    - **Dashboard**: 26-week interactive GitHub-style heatmap (click any day for its full tool bill), active streaks, and cross-tool model cost rankings.
    - **Projects**: Searchable, pinnable project usage tracker grouped by activity recency with Finder & Terminal quick-open actions.
  - **Built-in Sit Reminder**: 30 / 45 / 60 / 90 min intervals, automatic reset after > 5 min lock/sleep, and native macOS notifications with *Reset* and *Snooze 10m* actions.
- **Strictly Read-Only & Privacy-First**:
  - Never modifies or locks any AI client database (SQLite files are read via WAL-safe temporary snapshots).
  - Zero telemetry, zero analytics, zero cloud sync. See [SECURITY.md](SECURITY.md) for the complete privacy & network audit.

---

## Supported AI Clients

| AI Client | Work Hours | Live Active | Tokens & Cost | Quota | Local Source |
|---|:---:|:---:|:---:|:---:|---|
| **T3 Code** (Antigravity / Codex / Claude) | ✅ | ✅ | ✅ (via Codex dedup) | — | `~/.t3/userdata/statev2.sqlite` |
| **Codex** (CLI / Desktop) | ✅ | ✅ | ✅ | ✅ Live / JSONL | `~/.codex/sessions/**/*.jsonl` |
| **Claude Code** | ✅ | ✅ | ✅ | ✅ Statusline / Cache | `~/.claude/projects/**/*.jsonl` |
| **WorkBuddy** | ✅ | ✅ | ✅ | — | `~/.workbuddy/projects/**/*.jsonl` |
| **Hermes** (Main + Profiles) | ✅ | ✅ | ✅ | — | `~/.hermes/state.db` & `profiles/*/state.db` |
| **Antigravity** (Standalone) | ✅ | ✅ | — | 🟡 Cached snapshot | `~/.gemini/antigravity/brain/*/.../transcript.jsonl` |
| **Pi Agent** | ✅ | — | ✅ | — | `~/.pi/agent/sessions/**/*.jsonl` |
| **OpenCode** | ✅ | — | ✅ | — | `~/.local/share/opencode/opencode.db` |
| **Gemini CLI** | — | — | ✅ | — | `~/.gemini/tmp/**/session-*.json` |
| **CodeBuddy** | — | — | ✅ | — | `~/.codebuddy/projects/**/*.jsonl` |
| **ZCode** | — | — | ✅ | — | `~/.zcode/cli/db/db.sqlite` |
| **Kimi / Grok** | — | — | — | 🟡 Cached snapshot | `~/.agent-pulse/quota_cache.json` |

> See [docs/PROVIDERS.md](docs/PROVIDERS.md) for detailed paths, schema notes, and setup instructions for each provider.

---

## Quick Start

### Prerequisites

- **macOS 13.0+** (Ventura or later, Apple Silicon or Intel)
- **Xcode Command Line Tools** (`xcode-select --install`) for Swift 5.9+
- **Python 3.9+** (macOS system `/usr/bin/python3` works out of the box — zero `pip` packages required)

### Build & Install

```bash
# 1. Clone the repository
git clone https://github.com/hohoweiyao-cmyk/agent-pulse.git
cd agent-pulse

# 2. Run the isolated unit test suite
python3 -m unittest discover -s tests -v

# 3. Build the native macOS app bundle (outputs dist/AgentPulse.app)
bash scripts/build_app.sh

# 4. Install to /Applications and launch
cp -R dist/AgentPulse.app /Applications/AgentPulse.app
open /Applications/AgentPulse.app
```

> **First-launch tip**: `build_app.sh` ad-hoc signs the local build (`codesign --force --deep --sign -`) with bundle identifier `dev.agentpulse.app` (override with `AGENTPULSE_BUNDLE_ID=...`). Drag the desktop card by holding the top-left **"AgentPulse"** title or top blank area. Left-click the menu bar icon for the 4-tab detail panel; right-click for quick actions.

### CLI Usage (Headless / Scripting)

You can run the collector engine directly from the terminal without opening the GUI:

```bash
# Print today's unified Markdown report (work hours + tokens + estimated cost)
python3 engine/collector.py --markdown

# Report for a specific date
python3 engine/collector.py --markdown --date 2026-11-25

# Pretty-printed JSON snapshot (Schema v3)
python3 engine/collector.py --days 14 --pretty

# Run slow-track quota probe manually
python3 engine/quota_collector.py --pretty --force
```

---

## Optional Integrations

### Claude Code CLI Quota (Statusline Hook)

Claude Code can pipe its official statusline JSON (containing 5-hour and 7-day rate limits) directly into AgentPulse:

```bash
/usr/bin/python3 /Applications/AgentPulse.app/Contents/Resources/quota_collector.py --claude-statusline
```

- **No custom statusline yet**: Configure the command above as your Claude Code `statusLine` command.
- **Already have a custom statusline**: Pipe the same stdin JSON to both your existing statusline script and the command above (e.g., via `tee`).
- AgentPulse only stores utilization percentage, reset timestamp, and capture time — never conversation contents. Full examples are in [docs/PROVIDERS.md](docs/PROVIDERS.md#claude-code-cli-quota-statusline).

### Custom Model Pricing Overrides

Create `~/.agent-pulse/pricing_overrides.json` to add custom models, aliases, or override rates (USD per 1M tokens):

```json
{
  "models": {
    "my-provider/custom-model": {
      "in": 1.0,
      "out": 4.0,
      "cache_read": 0.1,
      "cache_write": 1.25
    }
  },
  "aliases": {
    "custom-fast": "my-provider/custom-model"
  }
}
```

### Migrating Historical Data from Tokei

If you previously used Tokei (`~/.tokei/`), AgentPulse includes a safe, idempotent migration tool that creates a full backup before importing:

```bash
python3 scripts/import_tokei.py
```

It backs up `~/.tokei/` into `~/.agent-pulse/backups/`, imports historical daily token/cost records into `imported_tools` (without double-counting live-scanned days), seeds quota caches, and migrates sit-reminder preferences.

> **Upgrading from earlier AgentClock builds (`~/.agent-clock/`)?** On first run, AgentPulse automatically copies your existing ledger, pricing overrides, quota caches, and preferences from `~/.agent-clock/` into `~/.agent-pulse/` (leaving `~/.agent-clock/` untouched as a backup).

---

## Architecture Overview

```text
agent-pulse/
├── engine/
│   ├── collector.py          # Unified CLI entrypoint & step-heartbeat work-hours engine (Schema v3)
│   ├── usage_collector.py    # Zero-dependency incremental token & cost parser
│   ├── pricing.py            # Model pricing resolver & cost estimator
│   ├── pricing.json          # Bundled OpenRouter-based pricing catalog (600+ models)
│   ├── pricing_overrides.json# Built-in model aliases & rate overrides
│   ├── ledger.py             # Persistent ~/.agent-pulse/ledger.json & Today/Week/Month/Year aggregator
│   └── quota_collector.py    # Independent slow-track subscription quota prober
├── macos/
│   ├── Package.swift         # SwiftPM manifest (macOS 13+, zero external packages)
│   └── Sources/AgentPulse/   # Native SwiftUI + AppKit widget, 4-tab popover & sit reminder
├── scripts/
│   ├── build_app.sh          # Release build + vector icon generator + .app bundler + ad-hoc signing
│   ├── generate_icon.swift   # Programmatic CoreGraphics macOS squircle icon generator
│   └── import_tokei.py       # Backup-first, idempotent Tokei history & preference migrator
├── tests/                    # Isolated fixture unit tests (no live home directory reads)
└── docs/
    ├── ARCHITECTURE.md       # Deep dive into step-heartbeat math, deduplication & Schema v3
    └── PROVIDERS.md          # Supported clients, local paths, Claude statusline & troubleshooting
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the complete algorithm breakdown and data flow diagram.

---

## Known Limitations & Roadmap

### Current Limitations
- **UI Language**: The macOS UI and Markdown report are currently in **Simplified Chinese** (`zh-CN`).
- **Business Timezone**: Daily boundaries currently default to `Asia/Shanghai (UTC+8)`.
- **Cached-Only Quotas**: Antigravity, Kimi, and Grok quotas currently display cached snapshots only (no live polling, and stale snapshots never trigger live alerts).
- **Codex Edge Cases**: Compressed Codex archives and cross-file duplicate `response_id` deduplication are not yet covered.

### Roadmap
- [ ] Configurable timezone (system local timezone by default, with explicit override)
- [ ] English (`en-US`) UI localization & Markdown report locale toggle
- [ ] GitHub Releases `.dmg` & Homebrew Cask distribution
- [ ] Additional provider adapters (Cursor, Windsurf, Cline, Roo Code) — PRs welcome!

---

## Documentation

- [Architecture & Algorithm Deep Dive](docs/ARCHITECTURE.md)
- [Supported AI Clients & Configuration Guide](docs/PROVIDERS.md)
- [Privacy & Security Policy](SECURITY.md)
- [Contributing Guide](CONTRIBUTING.md)
- [Changelog](CHANGELOG.md)

---

## Acknowledgements & License

- UI card granularity, two-level model breakdown, activity heatmap, and project trail concepts are inspired by the open-source MIT project [cclank/tokei](https://github.com/cclank/tokei).
- Released under the [MIT License](LICENSE).
