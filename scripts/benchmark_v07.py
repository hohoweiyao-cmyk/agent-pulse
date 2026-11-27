#!/usr/bin/env python3
"""Reproducible offline performance benchmark for AgentPulse v0.7 vs baseline (9aea07e).

Usage:
  /usr/bin/python3 -B scripts/benchmark_v07.py \
      --fixture /tmp/ap-bench-fixture \
      --baseline /path/to/9aea07e \
      --candidate /path/to/v07 \
      --runs 20

Notes:
- Freezes identical `now` across baseline and candidate runs via a tiny bootstrap
  so baseline `9aea07e` (which predates AGENTPULSE_NOW_TS) and v0.7 use the exact
  same clock and run completely offline (network blocked in bootstrap).
- Copies an identical initial ledger/home per run into a fresh isolated tempdir.
- Measures outer wall-clock latency, internal `elapsed_ms` (when JSON), and peak
  RSS (MB) p50/p95 across all shared and v0.7-new scenarios.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BIZ_TZ = timezone(timedelta(hours=8))
FROZEN_NOW_DT = datetime(2026, 11, 27, 15, 0, 0, tzinfo=BIZ_TZ)
FROZEN_NOW_TS = FROZEN_NOW_DT.timestamp()

_BOOTSTRAP_CODE = r"""
import os, sys, time, json, runpy, resource, socket

frozen = float(os.environ["AP_BENCH_NOW"])
orig_time = time.time
time.time = lambda: frozen

# Disable outbound network so slow-track benchmark is strictly offline
def _blocked_connect(*args, **kwargs):
    raise OSError("network disabled during benchmark")
socket.socket.connect = _blocked_connect

script_path = sys.argv[1]
sys.argv = [script_path] + sys.argv[2:]
t0 = time.perf_counter()
exit_code = 0
try:
    runpy.run_path(script_path, run_name="__main__")
except SystemExit as exc:
    exit_code = int(exc.code) if isinstance(exc.code, int) else (0 if exc.code is None else 1)
outer_ms = (time.perf_counter() - t0) * 1000.0
ru = resource.getrusage(resource.RUSAGE_SELF)
# On macOS ru_maxrss is in bytes; on Linux it is in KB
rss_mb = (ru.ru_maxrss / (1024.0 * 1024.0)) if sys.platform == "darwin" else (ru.ru_maxrss / 1024.0)
meta_path = os.environ["AP_BENCH_META"]
with open(meta_path, "w", encoding="utf-8") as fh:
    json.dump({"outer_ms": round(outer_ms, 2), "rss_mb": round(rss_mb, 2), "exit_code": exit_code}, fh)
raise SystemExit(exit_code)
"""


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def generate_synthetic_fixture(fixture_dir: Path) -> Dict[str, Any]:
    """Generate a realistic multi-source isolated HOME fixture with Git worktree + SQLite + JSONL."""
    if fixture_dir.exists():
        shutil.rmtree(fixture_dir)
    fixture_dir.mkdir(parents=True)

    # 1) Create a real local Git repo + linked worktree so worktree resolution is exercised
    repos_dir = fixture_dir / "repos"
    main_repo = repos_dir / "pulse-demo"
    wt_repo = repos_dir / "pulse-demo-wt"
    main_repo.mkdir(parents=True)
    git_env = {
        "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
        "HOME": str(fixture_dir),
        "GIT_AUTHOR_NAME": "Bench",
        "GIT_AUTHOR_EMAIL": "bench@local",
        "GIT_COMMITTER_NAME": "Bench",
        "GIT_COMMITTER_EMAIL": "bench@local",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main", str(main_repo)], env=git_env, check=True)
    (main_repo / "README.md").write_text("# demo\n", encoding="utf-8")
    subprocess.run(["/usr/bin/git", "-C", str(main_repo), "add", "README.md"], env=git_env, check=True)
    subprocess.run(["/usr/bin/git", "-C", str(main_repo), "commit", "-q", "-m", "init"], env=git_env, check=True)
    subprocess.run(
        ["/usr/bin/git", "-C", str(main_repo), "worktree", "add", "-q", "-b", "feat/bench", str(wt_repo)],
        env=git_env,
        check=True,
    )

    # 2) Codex sessions across 20 days (40 JSONL files, ~15 turns each)
    for d_off in range(20):
        day_dt = FROZEN_NOW_DT - timedelta(days=d_off, hours=2)
        cdir = fixture_dir / ".codex" / "sessions" / day_dt.strftime("%Y/%m/%d")
        cdir.mkdir(parents=True, exist_ok=True)
        for s_idx in range(2):
            f = cdir / f"rollout-{d_off}-{s_idx}.jsonl"
            cwd = str(wt_repo if s_idx == 1 else main_repo)
            lines = [
                json.dumps({"timestamp": _iso(day_dt), "type": "session_meta",
                            "payload": {"id": f"sess-{d_off}-{s_idx}", "originator": "codex_cli_rs", "cwd": cwd}}),
                json.dumps({"timestamp": _iso(day_dt), "type": "turn_context",
                            "payload": {"model": "gpt-5.3-codex", "cwd": cwd}}),
            ]
            tot_in = 0
            tot_out = 0
            for t_idx in range(5):
                t0 = day_dt + timedelta(minutes=t_idx * 4)
                tid = f"turn-{d_off}-{s_idx}-{t_idx}"
                lines.append(json.dumps({"timestamp": _iso(t0), "type": "event_msg",
                                         "payload": {"type": "task_started", "turn_id": tid, "started_at": _iso(t0)}}))
                lines.append(json.dumps({"timestamp": _iso(t0 + timedelta(seconds=45)), "type": "event_msg",
                                         "payload": {"type": "item_completed", "turn_id": tid,
                                                     "started_at_ms": int(t0.timestamp() * 1000),
                                                     "completed_at_ms": int((t0 + timedelta(seconds=45)).timestamp() * 1000)}}))
                tot_in += 12000
                tot_out += 800
                lines.append(json.dumps({"timestamp": _iso(t0 + timedelta(seconds=60)), "type": "event_msg",
                                         "payload": {"type": "token_count", "info": {
                                             "total_token_usage": {
                                                 "input_tokens": tot_in, "cached_input_tokens": tot_in // 2,
                                                 "output_tokens": tot_out, "reasoning_output_tokens": 100,
                                             }
                                         }}}))
                lines.append(json.dumps({"timestamp": _iso(t0 + timedelta(seconds=90)), "type": "event_msg",
                                         "payload": {"type": "task_complete", "turn_id": tid,
                                                     "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(seconds=90))}}))
            f.write_text("\n".join(lines) + "\n", encoding="utf-8")
            ts_mtime = (day_dt + timedelta(minutes=30)).timestamp()
            os.utime(f, (ts_mtime, ts_mtime))

    # 3) Claude Code & WorkBuddy JSONL across 14 days
    for d_off in range(14):
        day_dt = FROZEN_NOW_DT - timedelta(days=d_off, hours=3)
        cc_dir = fixture_dir / ".claude" / "projects" / "demo"
        cc_dir.mkdir(parents=True, exist_ok=True)
        ccf = cc_dir / f"cc-{d_off}.jsonl"
        cc_lines = []
        for idx in range(6):
            t0 = day_dt + timedelta(minutes=idx * 3)
            cc_lines.append(json.dumps({"timestamp": _iso(t0), "type": "user", "cwd": str(main_repo), "message": {"role": "user", "content": "hi"}}))
            cc_lines.append(json.dumps({
                "timestamp": _iso(t0 + timedelta(seconds=40)), "type": "assistant", "cwd": str(main_repo),
                "message": {"id": f"m-{d_off}-{idx}", "model": "claude-sonnet-4.5",
                            "usage": {"input_tokens": 4000, "output_tokens": 600, "cache_read_input_tokens": 8000, "cache_creation_input_tokens": 500}},
            }))
        ccf.write_text("\n".join(cc_lines) + "\n", encoding="utf-8")
        os.utime(ccf, ((day_dt + timedelta(minutes=20)).timestamp(), (day_dt + timedelta(minutes=20)).timestamp()))

        wb_dir = fixture_dir / ".workbuddy" / "projects" / "demo"
        wb_dir.mkdir(parents=True, exist_ok=True)
        wbf = wb_dir / f"wb-{d_off}.jsonl"
        wb_lines = [
            json.dumps({"timestamp": _iso(day_dt), "type": "message", "role": "user", "cwd": str(main_repo)}),
            json.dumps({"id": f"w-{d_off}", "timestamp": _iso(day_dt + timedelta(seconds=55)), "type": "message", "role": "assistant",
                        "cwd": str(main_repo), "providerData": {"modelName": "DeepSeek-V4.1-Flash"},
                        "message": {"usage": {"input_tokens": 9000, "output_tokens": 500, "cache_read_input_tokens": 4000}}}),
        ]
        wbf.write_text("\n".join(wb_lines) + "\n", encoding="utf-8")
        os.utime(wbf, ((day_dt + timedelta(minutes=5)).timestamp(), (day_dt + timedelta(minutes=5)).timestamp()))

    # 4) T3 SQLite DB
    t3_db = fixture_dir / ".t3" / "userdata" / "statev2.sqlite"
    t3_db.parent.mkdir(parents=True)
    with sqlite3.connect(t3_db) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, title TEXT, workspace_root TEXT)")
        conn.execute("CREATE TABLE projection_threads (thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT, worktree_path TEXT)")
        conn.execute("CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE orchestration_v2_projection_runs (run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INT, provider TEXT, provider_thread_id TEXT, status TEXT, requested_at TEXT, completed_at TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE orchestration_v2_projection_turn_items (turn_item_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, ordinal INT, type TEXT, status TEXT, updated_at TEXT, payload_json TEXT)")
        conn.execute("CREATE TABLE orchestration_v2_projection_runtime_requests (runtime_request_id TEXT PRIMARY KEY, thread_id TEXT, node_id TEXT, provider_turn_id TEXT, kind TEXT, status TEXT, created_at TEXT, resolved_at TEXT)")
        conn.execute("INSERT INTO projection_projects VALUES ('p1', 'demo', ?)", (str(main_repo),))
        conn.execute("INSERT INTO projection_threads VALUES ('th1', 'p1', '主线开发', ?)", (str(wt_repo),))
        conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES ('th1', 'p1', '主线开发', '{}')")
        for idx in range(20):
            r_start = FROZEN_NOW_DT - timedelta(days=idx % 10, hours=1, minutes=idx * 2)
            r_end = r_start + timedelta(minutes=4)
            rid = f"t3run-{idx}"
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                         (rid, "th1", idx, "antigravity", f"pt-{idx}", "completed", _iso(r_start), _iso(r_end), "{}"))
            conn.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES (?,?,?,?,?,?,?,?)",
                         (f"item-{idx}", "th1", rid, 1, "reasoning", "completed", _iso(r_end),
                          json.dumps({"startedAt": _iso(r_start), "completedAt": _iso(r_end)})))
        conn.commit()

    # 5) Hermes SQLite DB with session_model_usage
    hermes_db = fixture_dir / ".hermes" / "state.db"
    hermes_db.parent.mkdir(parents=True)
    with sqlite3.connect(hermes_db) as conn:
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, title TEXT, display_name TEXT, source TEXT, cwd TEXT)")
        conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)")
        conn.execute("CREATE TABLE session_turn_leases (conversation_id TEXT, acquired_at REAL, expires_at REAL)")
        conn.execute("""CREATE TABLE session_model_usage (
            session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
            billing_mode TEXT, task TEXT, input_tokens INT, output_tokens INT,
            cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT,
            estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,
            cost_source TEXT, first_seen REAL, last_seen REAL
        )""")
        conn.execute("INSERT INTO sessions VALUES ('hs1', 'Hermes任务', 'Hermes', 'cli', ?)", (str(main_repo),))
        h0 = (FROZEN_NOW_DT - timedelta(hours=2)).timestamp()
        conn.execute("INSERT INTO messages VALUES (1, 'hs1', 'user', ?)", (h0,))
        conn.execute("INSERT INTO messages VALUES (2, 'hs1', 'assistant', ?)", (h0 + 90,))
        conn.execute("""INSERT INTO session_model_usage VALUES (
            'hs1', 'gpt-5.3-codex', 'openai', 'https://api.openai.com', 'api', 'chat',
            10000, 1000, 0, 0, 0, 0.5, 3.25, 'actual', 'provider_cost_api', ?, ?
        )""", (h0, h0 + 90))
        conn.commit()

    # 6) Antigravity Desktop + CLI transcripts
    for root_rel, conv_id in ((".gemini/antigravity/brain", "conv-desktop-001"), (".gemini/antigravity-cli/brain", "conv-cli-001")):
        tpath = fixture_dir / root_rel / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        tpath.parent.mkdir(parents=True)
        t0 = FROZEN_NOW_DT - timedelta(hours=1)
        tpath.write_text("\n".join([
            json.dumps({"step_index": 1, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": _iso(t0)}),
            json.dumps({"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _iso(t0 + timedelta(seconds=120))}),
        ]) + "\n", encoding="utf-8")
        os.utime(tpath, ((t0 + timedelta(seconds=120)).timestamp(), (t0 + timedelta(seconds=120)).timestamp()))

    # 7) Initial quota cache (offline)
    data_dir = fixture_dir / ".agent-pulse"
    data_dir.mkdir(mode=0o700)
    (data_dir / ".data-layout-v1").write_text('{"version":1}', encoding="utf-8")
    (data_dir / "quota_cache.json").write_text(json.dumps({
        "claude": {"q5": 15, "q5_reset": int(FROZEN_NOW_TS + 7200), "q7": 30, "q7_reset": int(FROZEN_NOW_TS + 86400), "updated": int(FROZEN_NOW_TS - 120)},
    }), encoding="utf-8")
    # Seeded ledger for 14-day steady-state scenarios (saved outside .agent-pulse for clean copy)
    seeded_ledger = {
        "version": 1,
        "updated_at": _iso(FROZEN_NOW_DT - timedelta(minutes=5)),
        "work_history_from": (FROZEN_NOW_DT - timedelta(days=181)).strftime("%Y-%m-%d"),
        "project_work_history_from": (FROZEN_NOW_DT - timedelta(days=181)).strftime("%Y-%m-%d"),
        "imported_tools": {},
        "live_tools": {},
        "projects": {},
        "work_days": {},
    }
    (fixture_dir / "_seeded_ledger.json").write_text(json.dumps(seeded_ledger), encoding="utf-8")

    # Compute sample stats
    total_files = 0
    total_bytes = 0
    for r, dirs, files in os.walk(fixture_dir):
        if ".git" in dirs:
            dirs.remove(".git")
        for fn in files:
            p = Path(r) / fn
            if p.is_file():
                total_files += 1
                total_bytes += p.stat().st_size
    return {"files": total_files, "bytes": total_bytes}


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = int(round((pct / 100.0) * (len(s) - 1)))
    idx = max(0, min(idx, len(s) - 1))
    return round(s[idx], 2)


def _run_once(script_path: Path, args: List[str], home_dir: Path) -> Tuple[float, Optional[float], float, int]:
    with tempfile.NamedTemporaryFile(prefix="ap-bench-meta-", suffix=".json", delete=False) as tf:
        meta_path = Path(tf.name)
    try:
        env = {
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "HOME": str(home_dir),
            "AGENTPULSE_NOW_TS": str(FROZEN_NOW_TS),
            "AP_BENCH_NOW": str(FROZEN_NOW_TS),
            "AP_BENCH_META": str(meta_path),
        }
        proc = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", "-c", _BOOTSTRAP_CODE, str(script_path), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            timeout=60,
            check=False,
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.is_file() and meta_path.stat().st_size else {}
        outer_ms = float(meta.get("outer_ms", 0.0))
        rss_mb = float(meta.get("rss_mb", 0.0))
        elapsed_ms = None
        if proc.returncode == 0 and "--json" in args:
            try:
                parsed = json.loads(proc.stdout.decode("utf-8"))
                if isinstance(parsed, dict) and isinstance(parsed.get("elapsed_ms"), (int, float)):
                    elapsed_ms = float(parsed["elapsed_ms"])
            except Exception:
                pass
        return outer_ms, elapsed_ms, rss_mb, proc.returncode
    finally:
        if meta_path.exists():
            meta_path.unlink()


def _bench_scenario(
    checkout: Path,
    fixture_dir: Path,
    scenario: str,
    runs: int,
) -> Dict[str, Any]:
    collector = checkout / "engine" / "collector.py"
    quota = checkout / "engine" / "quota_collector.py"
    seeded_ledger = fixture_dir / "_seeded_ledger.json"

    outer_vals: List[float] = []
    elapsed_vals: List[float] = []
    rss_vals: List[float] = []

    for run_idx in range(runs):
        with tempfile.TemporaryDirectory(prefix=f"ap-bench-{scenario}-{run_idx}-") as td:
            lab_home = Path(td) / "home"
            shutil.copytree(fixture_dir, lab_home, symlinks=True)
            data_dir = lab_home / ".agent-pulse"
            ledger_file = data_dir / "ledger.json"
            if ledger_file.exists():
                ledger_file.unlink()
            for cf in ("cache.json", "usage_cache.json"):
                if (data_dir / cf).exists():
                    (data_dir / cf).unlink()

            if scenario != "first_use_182d" and seeded_ledger.is_file():
                shutil.copyfile(seeded_ledger, ledger_file)
                os.chmod(ledger_file, 0o600)

            if scenario == "first_use_182d":
                outer, el, rss, rc = _run_once(collector, ["--days", "14", "--json"], lab_home)
            elif scenario == "fast_14d_cold":
                outer, el, rss, rc = _run_once(collector, ["--days", "14", "--json"], lab_home)
            elif scenario == "fast_14d_warm":
                # Warm up caches once, then measure warm hit
                _, _, _, rc0 = _run_once(collector, ["--days", "14", "--json"], lab_home)
                if rc0 != 0:
                    raise RuntimeError(f"warmup failed for {checkout} {scenario}")
                outer, el, rss, rc = _run_once(collector, ["--days", "14", "--json"], lab_home)
            elif scenario == "fast_14d_nocache":
                outer, el, rss, rc = _run_once(collector, ["--days", "14", "--json", "--no-cache"], lab_home)
            elif scenario == "quota_offline":
                outer, el, rss, rc = _run_once(quota, ["--json"], lab_home)
            elif scenario == "markdown_daily":
                outer, el, rss, rc = _run_once(collector, ["--days", "14", "--markdown"], lab_home)
            elif scenario == "markdown_period_month":
                outer, el, rss, rc = _run_once(collector, ["--days", "14", "--markdown", "--period", "month"], lab_home)
            else:
                raise ValueError(f"unknown scenario: {scenario}")

            if rc != 0:
                raise RuntimeError(f"benchmark failed: {checkout} {scenario} rc={rc}")
            outer_vals.append(outer)
            if el is not None:
                elapsed_vals.append(el)
            rss_vals.append(rss)

    return {
        "runs": runs,
        "outer_ms_p50": _percentile(outer_vals, 50),
        "outer_ms_p95": _percentile(outer_vals, 95),
        "elapsed_ms_p50": _percentile(elapsed_vals, 50) if elapsed_vals else None,
        "elapsed_ms_p95": _percentile(elapsed_vals, 95) if elapsed_vals else None,
        "rss_mb_p50": _percentile(rss_vals, 50),
        "rss_mb_p95": _percentile(rss_vals, 95),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentPulse v0.7 offline performance benchmark")
    parser.add_argument("--fixture", type=Path, required=True, help="Synthetic fixture directory (created if missing)")
    parser.add_argument("--baseline", type=Path, required=True, help="Baseline checkout path (9aea07e)")
    parser.add_argument("--candidate", type=Path, required=True, help="Candidate checkout path (v0.7)")
    parser.add_argument("--runs", type=int, default=20, help="Number of isolated runs per scenario (default 20)")
    parser.add_argument("--regen-fixture", action="store_true", help="Force regenerate synthetic fixture")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional path to write full JSON report")
    args = parser.parse_args()

    fixture_dir = args.fixture.resolve()
    if args.regen_fixture or not fixture_dir.exists() or not any(fixture_dir.iterdir()):
        sample_stats = generate_synthetic_fixture(fixture_dir)
    else:
        total_files = 0
        total_bytes = 0
        for r, dirs, files in os.walk(fixture_dir):
            if ".git" in dirs:
                dirs.remove(".git")
            for fn in files:
                p = Path(r) / fn
                if p.is_file():
                    total_files += 1
                    total_bytes += p.stat().st_size
        sample_stats = {"files": total_files, "bytes": total_bytes}

    shared_scenarios = (
        "first_use_182d",
        "fast_14d_cold",
        "fast_14d_warm",
        "fast_14d_nocache",
        "quota_offline",
        "markdown_daily",
    )
    v07_only_scenarios = ("markdown_period_month",)

    report: Dict[str, Any] = {
        "machine": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "fixture": {
            "path": str(fixture_dir),
            "files": sample_stats["files"],
            "bytes": sample_stats["bytes"],
            "frozen_now_ts": FROZEN_NOW_TS,
        },
        "runs": args.runs,
        "shared": {},
        "v07_only": {},
        "all_gates_passed": True,
    }

    for sc in shared_scenarios:
        base_res = _bench_scenario(args.baseline.resolve(), fixture_dir, sc, args.runs)
        cand_res = _bench_scenario(args.candidate.resolve(), fixture_dir, sc, args.runs)
        limit_ms = round(max(base_res["outer_ms_p95"] * 1.25, base_res["outer_ms_p95"] + 250.0), 2)
        passed = cand_res["outer_ms_p95"] <= limit_ms
        if sc == "fast_14d_warm" and cand_res["outer_ms_p95"] >= 3000.0:
            passed = False
        if sc == "first_use_182d" and cand_res["outer_ms_p95"] >= 15000.0:
            passed = False
        if not passed:
            report["all_gates_passed"] = False
        report["shared"][sc] = {
            "baseline": base_res,
            "candidate": cand_res,
            "p95_budget_ms": limit_ms,
            "passed": passed,
        }

    for sc in v07_only_scenarios:
        report["v07_only"][sc] = _bench_scenario(args.candidate.resolve(), fixture_dir, sc, args.runs)

    if args.json_out:
        args.json_out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"# AgentPulse v0.7 性能基线对比（runs={args.runs}）")
    print(f"- 机器：`{report['machine']['platform']}` ({report['machine']['machine']}, Python {report['machine']['python']})")
    print(f"- 合成夹具：{sample_stats['files']} 文件 / {sample_stats['bytes']:,} bytes（frozen now={int(FROZEN_NOW_TS)}，离线）")
    print("")
    print("| 场景 | Baseline p50/p95 (ms) | v0.7 p50/p95 (ms) | v0.7 elapsed p95 | RSS p95 (MB) | 预算 p95 (ms) | 结论 |")
    print("|---|---:|---:|---:|---:|---:|---|")
    for sc, entry in report["shared"].items():
        b, c = entry["baseline"], entry["candidate"]
        el_str = f"{c['elapsed_ms_p95']:.1f}" if c["elapsed_ms_p95"] is not None else "—"
        verdict = "PASS" if entry["passed"] else "FAIL"
        print(
            f"| {sc} | {b['outer_ms_p50']:.1f} / {b['outer_ms_p95']:.1f} | "
            f"{c['outer_ms_p50']:.1f} / {c['outer_ms_p95']:.1f} | {el_str} | "
            f"{b['rss_mb_p95']:.1f} → {c['rss_mb_p95']:.1f} | {entry['p95_budget_ms']:.1f} | {verdict} |"
        )
    print("")
    print("| v0.7 新增场景 | p50/p95 (ms) | RSS p95 (MB) |")
    print("|---|---:|---:|")
    for sc, c in report["v07_only"].items():
        print(f"| {sc} | {c['outer_ms_p50']:.1f} / {c['outer_ms_p95']:.1f} | {c['rss_mb_p95']:.1f} |")

    return 0 if report["all_gates_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
