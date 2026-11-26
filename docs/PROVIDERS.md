# Supported AI Clients & Configuration Guide

This guide lists every AI client supported by **AgentPulse**, where it reads local logs from, how to enable optional quota hooks, and how to customize pricing or migrate from Tokei.

---

## 1. Provider Data Sources

All paths below are accessed **strictly read-only**. If a client is not installed on your Mac, AgentPulse silently skips it with zero overhead.

| Client | Tool Key(s) | Local Path | Work Hours | Tokens & Cost | Quota |
|---|---|---|:---:|:---:|:---:|
| **T3 Code** | `t3_antigravity`, `t3_codex`, `t3_claude`, `t3_other` | `~/.t3/userdata/statev2.sqlite` | ✅ (`turn_items` + `runtime_requests` exclusions) | ✅ (`t3_codex` via Codex JSONL dedup) | — |
| **Codex** | `codex` | `~/.codex/sessions/**/*.jsonl` | ✅ (skips `originator == "T3 Code"`) | ✅ (`token_count` & `token_usage_record`) | ✅ Live API (`~/.codex/auth.json`) + JSONL fallback |
| **Claude Code** | `claude_code` | `~/.claude/projects/**/*.jsonl` | ✅ (skips `t3code-claude-title-*`) | ✅ (dedups streaming chunks by `message.id`) | ✅ CLI statusline hook + Desktop zstd cache |
| **WorkBuddy** | `workbuddy` | `~/.workbuddy/projects/**/*.jsonl` | ✅ | ✅ | — |
| **CodeBuddy** | `codebuddy` | `~/.codebuddy/projects/**/*.jsonl` | — | ✅ | — |
| **Hermes** | `hermes` | `~/.hermes/state.db`, `~/.hermes/profiles/*/state.db` | ✅ (incl. `session_turn_leases`) | ✅ (`model_usage` / `sessions`) | — |
| **Antigravity** | `antigravity` | `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript*.jsonl` | ✅ | — | 🟡 Cached snapshot only |
| **Pi Agent** | `pi` | `~/.pi/agent/sessions/**/*.jsonl` | ✅ | ✅ | — |
| **OpenCode** | `opencode` | `~/.local/share/opencode/opencode.db` | ✅ | ✅ | — |
| **Gemini CLI** | `gemini` | `~/.gemini/tmp/**/session-*.json` | — | ✅ | — |
| **ZCode** | `zcode` | `~/.zcode/cli/db/db.sqlite` | — | ✅ (`model_usage`) | — |
| **Kimi / Grok** | `kimi`, `grok` | `~/.agent-pulse/quota_cache.json` | — | — | 🟡 Cached snapshot only |

---

## 2. Subscription Quota Configuration

### Codex Quota
- **Automatic**: If you are signed into official Codex (`~/.codex/auth.json`) and `~/.codex/config.toml` does not set a custom non-OpenAI `model_provider`, AgentPulse polls `https://chatgpt.com/backend-api/wham/usage` at most once every 4–5 minutes.
- **Offline / Fallback**: If the network is unreachable or the API fails, AgentPulse falls back to the latest `rate_limits` entry found in `~/.codex/sessions/**/*.jsonl`, and then to `~/.agent-pulse/quota_cache.json`.
- **Custom API Provider**: When `model_provider` in `~/.codex/config.toml` is set to a custom third-party provider, AgentPulse skips the official endpoint cleanly.

### Claude Code CLI Quota (Statusline)

Claude Code exposes official 5-hour and 7-day subscription rate limits via its statusline JSON input.

#### Option A: Standalone (No existing custom statusline)
Add the following command as your Claude Code `statusLine` command in `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/usr/bin/python3 /Applications/AgentPulse.app/Contents/Resources/quota_collector.py --claude-statusline"
  }
}
```

#### Option B: Chained (Preserve your existing custom statusline)
If you already run a custom statusline script, pipe the same stdin JSON to both AgentPulse and your existing script:

```bash
#!/usr/bin/env bash
payload="$(cat)"
printf '%s' "$payload" | /usr/bin/python3 /Applications/AgentPulse.app/Contents/Resources/quota_collector.py --claude-statusline >/dev/null 2>&1 || true
printf '%s' "$payload" | /path/to/your-existing-statusline.sh
```

> **Privacy note**: `--claude-statusline` discards `session_id` and all workspace fields, saving only `q5`, `q5_reset`, `q7`, `q7_reset`, `updated`, and `source: "cli"` into `~/.agent-pulse/claude_statusline_quota.json`. Expired reset windows are automatically dropped.

### Claude Desktop Cache (Automatic Fallback)
If you use Claude Desktop on macOS, AgentPulse also reads zstd-compressed usage cache files under `~/Library/Application Support/Claude/Cache/Cache_Data/` (using Python `zstandard`, Homebrew `zstd`, or `libzstd` via `ctypes` if available) and picks whichever reading (CLI statusline vs Desktop cache) has the newer timestamp.

### Snapshot-Only Providers (Antigravity / Kimi / Grok)
Antigravity, Kimi, and Grok currently display existing cached snapshots in `~/.agent-pulse/quota_cache.json` (for example, seeded during Tokei migration). They are explicitly marked as cached/stale in the UI and **never** trigger live high-quota warning banners.

---

## 3. Custom Model Pricing (`pricing_overrides.json`)

AgentPulse resolves pricing in three layers (later layers override earlier ones):
1. `engine/pricing.json` (bundled OpenRouter catalog, 600+ models)
2. `engine/pricing_overrides.json` (built-in aliases and overrides)
3. `~/.agent-pulse/pricing_overrides.json` (your personal overrides, preserved across app upgrades)

Rates are specified in **USD per 1,000,000 tokens**:

```json
{
  "models": {
    "custom/my-local-llm": {
      "in": 0.0,
      "out": 0.0,
      "cache_read": 0.0,
      "cache_write": 0.0
    },
    "openai/gpt-5.6-sol": {
      "in": 4.0,
      "out": 20.0,
      "cache_read": 0.4,
      "cache_write": 5.0
    }
  },
  "aliases": {
    "my-fast-alias": "custom/my-local-llm"
  }
}
```

Model resolution automatically strips provider prefixes (`provider/model`), version/tag suffixes (`:batch`, `@...`), and date suffixes (`-20260902`, `-2026-09-02`).

---

## 4. Migrating from Tokei (or Legacy AgentClock)

To import historical daily usage, quota caches, and sit-reminder preferences from `~/.tokei/`:

```bash
python3 scripts/import_tokei.py
```

Or click **"重新导入 Tokei 历史"** inside **Menu Bar Popover → Settings (设置)**.

Safety guarantees:
- Always creates a full read-only backup of `~/.tokei/` and any existing `~/.agent-pulse/ledger.json` under `~/.agent-pulse/backups/tokei-import-<timestamp>/` before writing.
- Never modifies `~/.tokei/`.
- Rejects corrupted or empty ledgers without overwriting existing AgentPulse history.
- Fully idempotent: safe to re-run at any time without double counting.

If you are upgrading from an earlier `~/.agent-clock/` installation, AgentPulse automatically seeds `~/.agent-pulse/` from `~/.agent-clock/` on first run while keeping `~/.agent-clock/` intact as a backup.

---

## 5. Troubleshooting

| Symptom | Cause & Fix |
|---|---|
| macOS warns the app is from an unidentified developer | Local builds use ad-hoc signing (`codesign --sign -`). Right-click `/Applications/AgentPulse.app` → **Open**, or run `xattr -dr com.apple.quarantine /Applications/AgentPulse.app`. |
| Desktop card cannot be dragged | Hold the top-left **"AgentPulse"** title or the blank header area next to it (not interactive buttons). |
| Claude Desktop quota not updating | Install `zstd` (`brew install zstd`) so AgentPulse can decompress Claude Desktop's cache, or configure the Claude Code CLI `--claude-statusline` hook above. |
| Want to completely uninstall AgentPulse | Quit AgentPulse, disable Launch at Login (or remove `~/Library/LaunchAgents/dev.agentpulse.app.plist`), and move `/Applications/AgentPulse.app` and `~/.agent-pulse/` to Trash. None of your AI client directories were modified. |
