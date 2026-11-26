# Contributing to AgentPulse

Thank you for your interest in contributing to **AgentPulse**! Contributions of all kinds — bug reports, new AI client adapters, pricing updates, localization, and documentation improvements — are warmly welcomed.

## Core Design Principles

Before opening a PR, please keep these non-negotiable guardrails in mind:

1. **Strictly Read-Only on Client Data**: Never write to or lock any AI client's logs or databases. Use `sqlite_snapshot` for queries.
2. **Zero External Dependencies**:
   - The Python engine (`engine/`) uses **Python 3.9+ standard library only** (`sqlite3`, `json`, `urllib`, `ctypes`, etc.). Do not add third-party `pip` dependencies.
   - The macOS app (`macos/`) uses native **SwiftUI + AppKit + UserNotifications** only. Do not add heavy SwiftPM dependencies unless discussed first.
3. **Zero Silent Double Counting**: If a client can be invoked through an orchestrator (like Codex inside T3 Code), deduplicate both work hours and token accounting consistently.
4. **Isolated Unit Tests Only**: Tests in `tests/` must use `tempfile.TemporaryDirectory()` fixtures and **never** read the developer's real `$HOME` directory.

---

## Development Setup

### Requirements
- macOS 13.0+
- Xcode Command Line Tools (`swift --version` ≥ 5.9)
- Python 3.9+

### Common Commands

```bash
# Run all Python unit tests
python3 -m unittest discover -s tests -v

# Test the CLI collector directly (read-only)
python3 engine/collector.py --days 14 --pretty
python3 engine/collector.py --markdown

# Build the Swift executable (debug)
swift build --package-path macos

# Run the native subprocess and directory regression tests
swift test --package-path macos

# Build full Release .app bundle (dist/AgentPulse.app)
bash scripts/build_app.sh
```

---

## Adding a New AI Client / Provider

Adding a new AI tool typically touches 2–4 small places:

1. **Work Hours (`engine/collector.py`)**:
   - Add tool display metadata to `TOOL_META` (`name`, `short`, `color`).
   - Implement a `collect_<tool>()` function returning `List[WorkSegment]` built via `build_heartbeat_segments(...)`.
   - Use `SegmentCache` for JSONL/log files or `open_sqlite_readonly_snapshot()` for SQLite databases.
   - Wire it into `collect_all()`.
2. **Tokens & Cost (`engine/usage_collector.py` & `engine/ledger.py`)**:
   - Add tool display metadata to `USAGE_TOOL_META` in `engine/ledger.py`.
   - Implement `collect_<tool>_usage()` using `UsageCache` (for files) or `_open_sqlite_readonly_snapshot()` (for SQLite).
   - Separate non-cached input (`in`), cache read (`cr`), cache write (`cw`), non-reasoning output (`out`), and reasoning (`thoughts`) consistently so `PricingTable.estimate_cost()` is accurate.
   - Wire it into `collect_live_usage()`.
3. **Unit Tests (`tests/`)**:
   - Add isolated fixture tests in `tests/test_collector.py` and/or `tests/test_usage_and_merge.py` covering normal parsing, edge cases, and deduplication.
4. **Documentation**:
   - Document the local path, schema assumptions, and capabilities in [docs/PROVIDERS.md](docs/PROVIDERS.md) and the summary tables in `README.md` / `README.zh-CN.md`.

---

## Pull Request Checklist

The Verify workflow runs Python tests, native tests and the local app build on Apple Silicon and Intel runners. Repository administrators can require both Verify jobs before merging to the default branch. Tag releases from a verified commit.

- [ ] `python3 -m unittest discover -s tests -v` passes completely.
- [ ] `bash scripts/build_app.sh` succeeds without warnings or errors (if `macos/` or `scripts/` changed).
- [ ] No personal paths (`/Users/...`), API keys, tokens, or real local logs are committed.
- [ ] Relevant documentation (`README.md`, `README.zh-CN.md`, `docs/PROVIDERS.md`, `CHANGELOG.md`) is updated.
