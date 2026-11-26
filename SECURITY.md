# Security & Privacy Policy

**AgentPulse** is designed from the ground up as a **100% local-first, strictly read-only** desktop telemetry tool for your own Mac.

## Privacy Guarantees

- **Zero Telemetry / Analytics**: AgentPulse does not phone home, collect usage analytics, or transmit conversation logs, prompts, project names, or token counts anywhere.
- **No Modification of External AI Clients**: Every AI client directory (`~/.t3/`, `~/.codex/`, `~/.claude/`, `~/.workbuddy/`, `~/.hermes/`, `~/.gemini/`, `~/.pi/`, `~/.local/share/opencode/`, `~/.zcode/`, `~/.codebuddy/`, `~/.tokei/`, and legacy `~/.agent-clock/`) is accessed **strictly read-only**.
- **WAL-Safe SQLite Snapshots**: Live SQLite databases (`statev2.sqlite`, `state.db`, `opencode.db`, `db.sqlite`) are copied along with their `-wal`/`-shm` sidecars into a private temporary directory, queried read-only, and deleted immediately afterward — ensuring AgentPulse never locks or corrupts an active AI client.
- **Minimal Storage**: AgentPulse only stores aggregated daily numeric statistics (durations, token counts, estimated USD cost, and quota percentages) — never message contents, prompts, or credentials.

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
- Optional one-time migration only (`scripts/import_tokei.py`): `~/.tokei/*` and `~/Library/Preferences/com.tokei.app.plist`

### Files Written (Owned by AgentPulse Only)
- `~/.agent-pulse/cache.json` — incremental work-hours file parse cache
- `~/.agent-pulse/usage_cache.json` — incremental token/cost file parse cache
- `~/.agent-pulse/ledger.json` — persistent daily token & cost ledger
- `~/.agent-pulse/quota_cache.json` — cached subscription quota readings (personal account emails are stripped before caching)
- `~/.agent-pulse/claude_statusline_quota.json` — optional Claude Code CLI statusline quota snapshot (utilization & reset timestamps only)
- `~/.agent-pulse/backups/` — full safety backups created by `scripts/import_tokei.py`
- `~/Library/LaunchAgents/dev.agentpulse.app.plist` — created **only** if you explicitly enable "Launch at Login" in Settings/Menu

---

## Network Egress Audit

AgentPulse makes **at most one** optional outbound HTTPS request, strictly inside the slow-track quota prober (`engine/quota_collector.py`):

| Destination | Purpose | Trigger & Safeguards |
|---|---|---|
| `https://chatgpt.com/backend-api/wham/usage` | Fetch Codex 5-hour & 7-day subscription rate limits | Runs at most once per 4–5 minutes (or on manual refresh) **only** if `~/.codex/auth.json` has an active token and `~/.codex/config.toml` does not use a custom third-party `model_provider`. Uses `add_unredirected_header` for `Authorization` and verifies final host is `https://chatgpt.com` (3s timeout, 5m failure backoff). |

No other network connections are made by any part of the Python engine or Swift macOS application.

---

## Reporting a Vulnerability

If you discover a security or privacy issue (such as credential leakage, unsafe temporary file handling, or unintended write behavior):

1. Please **do not** disclose sensitive exploit details in a public issue before a fix is available.
2. Open a private security advisory via GitHub's **Security → Advisories → Report a vulnerability** tab on this repository (or contact the maintainer directly).
3. Include reproduction steps, affected file paths, and expected vs. actual behavior. We aim to respond promptly and prioritize fixes for any privacy or data-safety regression.
