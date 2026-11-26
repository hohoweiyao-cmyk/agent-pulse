# Security & Privacy Policy

**AgentPulse** is designed from the ground up as a **100% local-first, strictly read-only** desktop telemetry tool for your own Mac.

## Privacy Guarantees

- **Zero Telemetry / Analytics**: AgentPulse does not phone home, collect usage analytics, or transmit conversation logs, prompts, project names, or token counts anywhere.
- **No Modification of External AI Clients**: Every AI client directory (`~/.t3/`, `~/.codex/`, `~/.claude/`, `~/.workbuddy/`, `~/.hermes/`, `~/.gemini/`, `~/.pi/`, `~/.local/share/opencode/`, `~/.zcode/`, `~/.codebuddy/`, `~/.tokei/`, and legacy `~/.agent-clock/`) is accessed **strictly read-only**.
- **SQLite Snapshots**: AgentPulse copies the database and its WAL into a private temporary directory, checks whether either file changed during the copy, and queries the snapshot read-only. A persistently changing source can be skipped for that refresh. Copied databases can contain conversation data during processing.
- **Local Metadata**: The ledger stores daily usage and work hours, model names, project paths and quota snapshots. Active session titles can appear in the desktop widget. The work-hours cache omits titles and uses hashed file paths. Codex login tokens are used in memory only when you enable live quota.

---

## Exact File System Footprint

### Files Read (Read-Only)
- `~/.t3/userdata/statev2.sqlite`
- `~/.codex/sessions/**/*.jsonl`, `~/.codex/config.toml`, `~/.codex/auth.json` (used solely for optional Codex quota check)
- `~/.claude/projects/**/*.jsonl`
- `~/Library/Application Support/Claude/Cache/Cache_Data/*` (local zstd-compressed usage cache from Claude Desktop)
- `~/.workbuddy/projects/**/*.jsonl`, `~/.codebuddy/projects/**/*.jsonl`
- `~/.hermes/state.db`, `~/.hermes/profiles/*/state.db`
- `~/.gemini/antigravity/brain/*/.system_generated/logs/transcript*.jsonl`, `~/.gemini/tmp/**/session-*.json`
- `~/.pi/agent/sessions/**/*.jsonl`
- `~/.local/share/opencode/opencode.db`
- `~/.zcode/cli/db/db.sqlite`
- On request, Tokei migration reads its ledger, quota snapshots and history, pricing overrides, and `~/Library/Preferences/com.tokei.app.plist`.

### Files Written (Owned by AgentPulse Only)
- `~/.agent-pulse/cache.json` — incremental work-hours file parse cache
- `~/.agent-pulse/usage_cache.json` — incremental token/cost file parse cache
- `~/.agent-pulse/ledger.json` and `ledger.previous.json` — current and previous ledger with daily usage, hours and project paths
- `~/.agent-pulse/quota_cache.json` — provider quota snapshots. Live Codex data is associated with a hash of the account identity.
- `~/.agent-pulse/claude_statusline_quota.json` — optional Claude Code CLI statusline quota snapshot (utilization & reset timestamps only)
- `~/.agent-pulse/backups/` — source ledger and affected AgentPulse files from Tokei imports, plus pre-restore copies
- `~/Library/LaunchAgents/dev.agentpulse.app.plist` — created **only** if you explicitly enable "Launch at Login" in Settings/Menu

AgentPulse creates its data directory with mode `0700` and writes files with mode `0600`. The upgrade restricts access to the existing data directory and known state files. Local records use macOS file permissions. Disk encryption depends on the Mac's settings. The rolling ledger holds one previous copy. Import and restore backups remain until removed by the user.

---

## Network Egress Audit

AgentPulse makes **at most one** optional outbound HTTPS request, strictly inside the slow-track quota prober (`engine/quota_collector.py`):

| Destination | Purpose | Trigger & Safeguards |
|---|---|---|
| `https://chatgpt.com/backend-api/wham/usage` | Fetch Codex 5-hour & 7-day subscription rate limits | Disabled by default. Enable live Codex quota in the app or use `--live-codex` on the CLI. The endpoint receives the local Codex access token and account ID when available. Requests reject redirects, use a 3s network timeout and respect a 5-minute failure backoff. |

No other network connections are made by any part of the Python engine or Swift macOS application.

---

## Reporting a Vulnerability

If you discover a security or privacy issue (such as credential leakage, unsafe temporary file handling, or unintended write behavior):

1. Please **do not** disclose sensitive exploit details in a public issue before a fix is available.
2. Use GitHub's **Security → Advisories → Report a vulnerability** when private reporting is available. If the button is absent, open an issue asking the maintainer for a private reporting channel and omit vulnerability details.
3. Include reproduction steps, affected file paths, and expected vs. actual behavior. We aim to respond promptly and prioritize fixes for any privacy or data-safety regression.
