#!/usr/bin/env python3
"""Automated regression guard tests for AgentPulse v0.7 review findings (#1-#9, #11, #12)."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "scripts"))

import collector as c
import ledger as lg
import pricing as pr
import quota_collector as qc
import usage_collector as uc


NOW_DT = datetime(2026, 11, 27, 15, 0, 0, tzinfo=c.BIZ_TZ)
NOW_TS = NOW_DT.timestamp()
TODAY_STR = "2026-11-27"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _strip_perf(payload: dict) -> dict:
    out = copy.deepcopy(payload)
    out.pop("elapsed_ms", None)
    return out


def _create_hermes_db(db_path: Path, session_id: str, work_sec: float, tokens: int, base_ts: float) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, started_at REAL, ended_at REAL,
                model TEXT, input_tokens INT, output_tokens INT,
                cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT,
                estimated_cost_usd REAL, actual_cost_usd REAL
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT, role TEXT, timestamp REAL
            )"""
        )
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, 0.0, 0.0)",
            (session_id, base_ts, base_ts + work_sec, "gpt-5.3-codex", tokens, 0),
        )
        conn.execute("INSERT INTO messages (session_id, role, timestamp) VALUES (?, 'user', ?)", (session_id, base_ts))
        conn.execute(
            "INSERT INTO messages (session_id, role, timestamp) VALUES (?, 'assistant', ?)",
            (session_id, base_ts + work_sec),
        )
        conn.commit()


class TestV07Guardrails(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = Path(td.name)

    # ------------------------------------------------------------------
    # #1a: Corrupt middle JSONL line (balanced braces, missing keyword)
    # ------------------------------------------------------------------
    def test_01a_codex_middle_corrupt_line_blocks_underestimate_and_recovers(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        t0 = NOW_DT - timedelta(minutes=20)
        f = cx_dir / "rollout-1.jsonl"

        head_lines = [
            json.dumps({"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "s-mid", "originator": "codex_cli_rs", "cwd": str(self.home / "p")}}),
            json.dumps({"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}}),
            json.dumps({"timestamp": _iso(t0), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1", "started_at": _iso(t0)}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(seconds=60)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(seconds=60))}}),
        ]
        mid_good = json.dumps({"timestamp": _iso(t0 + timedelta(seconds=61)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 4000, "output_tokens": 0}}}})
        tail_good = json.dumps({"timestamp": _iso(t0 + timedelta(seconds=62)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "output_tokens": 0}}}})

        f.write_text("\n".join(head_lines + [mid_good, tail_good]) + "\n", encoding="utf-8")
        r1 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        self.assertEqual(r1["collection"]["usage"]["status"], "complete")
        self.assertEqual(r1["usage"]["periods"]["today"]["total_tokens"], 5000)
        ledger_before = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        bucket_before = copy.deepcopy(ledger_before["live_tools"]["codex"][TODAY_STR])
        work_before = copy.deepcopy(ledger_before["work_days"][TODAY_STR])

        # Corrupt middle line: intact `{...}` braces, missing filter keyword & invalid JSON, valid tail!
        corrupt_mid = '{"timestamp":"broken", bad_mid_metering_line}'
        f.write_text("\n".join(head_lines + [corrupt_mid, tail_good]) + "\n", encoding="utf-8")
        r2 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 15)
        self.assertNotEqual(r2["collection"]["sources"]["usage.codex"]["status"], "complete")
        self.assertNotEqual(r2["collection"]["sources"]["work.codex"]["status"], "complete")
        ledger_after = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        self.assertEqual(ledger_after["live_tools"]["codex"][TODAY_STR], bucket_before)
        self.assertEqual(ledger_after["work_days"][TODAY_STR], work_before)
        self.assertEqual(r2["usage"]["periods"]["today"]["total_tokens"], 5000)

        # Also test valid JSON with bad metering structure (`info` is string instead of dict)
        corrupt_struct = json.dumps({"timestamp": _iso(t0 + timedelta(seconds=61)), "type": "event_msg", "payload": {"type": "token_count", "info": "corrupt_struct"}})
        f.write_text("\n".join(head_lines + [corrupt_struct, tail_good]) + "\n", encoding="utf-8")
        r2b = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 20)
        self.assertNotEqual(r2b["collection"]["sources"]["usage.codex"]["status"], "complete")
        self.assertEqual(lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")["live_tools"]["codex"][TODAY_STR], bucket_before)

        # Restore healthy source with updated 6,000 tokens -> trusted correction accepted
        mid_updated = json.dumps({"timestamp": _iso(t0 + timedelta(seconds=61)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 5000, "output_tokens": 0}}}})
        f.write_text("\n".join(head_lines + [mid_updated, tail_good]) + "\n", encoding="utf-8")
        r3 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 30)
        self.assertEqual(r3["collection"]["sources"]["usage.codex"]["status"], "complete")
        self.assertEqual(r3["usage"]["periods"]["today"]["total_tokens"], 6000)

    # ------------------------------------------------------------------
    # #1b: Missing previously-seen Hermes profile state.db
    # ------------------------------------------------------------------
    def test_01b_hermes_missing_seen_profile_db_degrades_and_recovers(self):
        hermes_root = self.home / ".hermes"
        main_db = hermes_root / "state.db"
        sub_db = hermes_root / "profiles" / "sub" / "state.db"
        base_ts = NOW_TS - 1200
        _create_hermes_db(main_db, "h-main", 30.0, 1000, base_ts)
        _create_hermes_db(sub_db, "h-sub", 40.0, 4000, base_ts + 200)

        r1 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        self.assertEqual(r1["collection"]["sources"]["work.hermes"]["status"], "complete")
        self.assertEqual(r1["collection"]["sources"]["usage.hermes"]["status"], "complete")
        self.assertAlmostEqual(r1["today"]["agent_seconds"], 70.0, delta=0.5)
        self.assertEqual(r1["usage"]["periods"]["today"]["total_tokens"], 5000)
        led1 = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        old_work = copy.deepcopy(led1["work_days"][TODAY_STR])
        old_usage = copy.deepcopy(led1["live_tools"]["hermes"][TODAY_STR])

        # Move sub profile state.db away while keeping its directory and healthy main DB
        moved_away = self.home / "moved_sub_state.db"
        shutil.move(str(sub_db), str(moved_away))

        r2 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 15)
        self.assertEqual(r2["collection"]["sources"]["work.hermes"]["status"], "partial")
        self.assertEqual(r2["collection"]["sources"]["usage.hermes"]["status"], "partial")
        led2 = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        self.assertEqual(led2["work_days"][TODAY_STR], old_work)
        self.assertEqual(led2["live_tools"]["hermes"][TODAY_STR], old_usage)
        self.assertAlmostEqual(r2["today"]["agent_seconds"], 70.0, delta=0.5)
        self.assertEqual(r2["usage"]["periods"]["today"]["total_tokens"], 5000)

        # Restore sub DB with updated 50s / 6,000 tokens -> complete & updated to 80s / 7,000 tokens
        moved_away.unlink()
        _create_hermes_db(sub_db, "h-sub-2", 50.0, 6000, base_ts + 300)
        r3 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 30)
        self.assertEqual(r3["collection"]["sources"]["work.hermes"]["status"], "complete")
        self.assertEqual(r3["collection"]["sources"]["usage.hermes"]["status"], "complete")
        self.assertAlmostEqual(r3["today"]["agent_seconds"], 80.0, delta=0.5)
        self.assertEqual(r3["usage"]["periods"]["today"]["total_tokens"], 7000)

    # ------------------------------------------------------------------
    # #2: Bidirectional concurrent commit_ts & in-lock gate normalization
    # ------------------------------------------------------------------
    def test_02_concurrent_commit_ts_bidirectional_race_consistency(self):
        ledger_path = self.home / ".agent-pulse" / "ledger.json"
        t1 = NOW_TS
        t2 = NOW_TS + 10.0

        def work_day(sec: float) -> list:
            return [{
                "date": TODAY_STR,
                "agent_seconds": sec,
                "agent_hours": round(sec / 3600.0, 2),
                "wall_seconds": sec,
                "runs": 1,
                "tools": [{"tool": "codex", "agent_seconds": sec, "agent_hours": round(sec / 3600.0, 2), "runs": 1}],
                "hourly_seconds": [0.0] * 14 + [sec] + [0.0] * 9,
            }]

        # Direction A: Both scans started before Codex was recorded on disk.
        # Earlier scan (t1) commits Codex complete with 100s first.
        st1: dict = {}
        lg.build_usage_payload(
            ledger_path=ledger_path, live_usage={}, live_cutoff_day=TODAY_STR, now_ts=t1,
            work_daily_list=work_day(100.0), persist=True,
            source_statuses={"work.codex": "complete", "work.workbuddy": "complete"},
            commit_ts=t1, out_state=st1,
        )
        # Later scan (t2) had stale pre-lock view where work.codex was `not_configured` and brings only 20s!
        st2: dict = {}
        u2, _ = lg.build_usage_payload(
            ledger_path=ledger_path, live_usage={}, live_cutoff_day=TODAY_STR, now_ts=t2,
            work_daily_list=work_day(20.0), persist=True,
            source_statuses={"work.codex": "not_configured", "work.workbuddy": "complete"},
            commit_ts=t2, out_state=st2,
        )
        disk_a = lg.load_ledger(ledger_path)
        self.assertEqual(disk_a["work_days"][TODAY_STR]["agent_seconds"], 100.0)
        self.assertEqual(u2["periods"]["today"]["agent_seconds"], 100.0)
        self.assertEqual(st2["collection"]["sources"]["work.codex"]["status"], "unavailable")
        self.assertEqual(st2["collection"]["work"]["status"], "partial")
        self.assertEqual(st2["collection"]["work"]["last_success_ts"], round(t1, 3))
        self.assertFalse(st2["work_complete"])

        # Direction B: Reverse-order commit. Newer scan (t3) commits Codex unavailable FIRST;
        # rejected older scan (t_old < t3) passes Codex complete -> MUST NOT report complete or borrow t3!
        ledger_path_b = self.home / ".agent-pulse" / "ledger_b.json"
        t_ok = NOW_TS - 60.0
        t_old = NOW_TS + 5.0
        t3 = NOW_TS + 20.0
        # Seed prior success at t_ok
        lg.build_usage_payload(
            ledger_path=ledger_path_b, live_usage={}, live_cutoff_day=TODAY_STR, now_ts=t_ok,
            work_daily_list=work_day(100.0), persist=True,
            source_statuses={"work.codex": "complete"}, commit_ts=t_ok,
        )
        # Newer scan commits unavailable at t3
        lg.build_usage_payload(
            ledger_path=ledger_path_b, live_usage={}, live_cutoff_day=TODAY_STR, now_ts=t3,
            work_daily_list=work_day(20.0), persist=True,
            source_statuses={"work.codex": "unavailable"}, commit_ts=t3,
        )
        # Stale scan with t_old < t3 tries to commit complete with 50s -> REJECTED!
        st_rej: dict = {}
        u_rej, _ = lg.build_usage_payload(
            ledger_path=ledger_path_b, live_usage={}, live_cutoff_day=TODAY_STR, now_ts=t_old,
            work_daily_list=work_day(50.0), persist=True,
            source_statuses={"work.codex": "complete"}, commit_ts=t_old, out_state=st_rej,
        )
        self.assertTrue(st_rej["rejected_stale_commit"])
        self.assertEqual(lg.load_ledger(ledger_path_b)["work_days"][TODAY_STR]["agent_seconds"], 100.0)
        self.assertEqual(u_rej["periods"]["today"]["agent_seconds"], 100.0)
        self.assertEqual(st_rej["collection"]["sources"]["work.codex"]["status"], "unavailable")
        self.assertEqual(st_rej["collection"]["sources"]["work.codex"]["last_success_ts"], round(t_ok, 3))
        self.assertNotEqual(st_rej["collection"]["sources"]["work.codex"].get("last_success_ts"), round(t3, 3))
        self.assertEqual(st_rej["collection"]["work"]["last_success_ts"], round(t_ok, 3))

    # ------------------------------------------------------------------
    # #3: Empty HOME and global-only work do not prematurely end 182d backfill
    # ------------------------------------------------------------------
    def test_03_empty_home_and_no_project_work_do_not_lock_182d_backfill(self):
        # Part A: Empty HOME first run -> neither marker set
        r_empty = c.collect_all(home=self.home, days=14, use_cache=False, now_ts=NOW_TS)
        led0 = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        self.assertIsNone(led0.get("work_history_from"))
        self.assertIsNone(led0.get("project_work_history_from"))

        # Drop 40-day-old Codex log (with original 40-day-old mtime, 70s)
        old_dt = NOW_DT - timedelta(days=40)
        old_day = old_dt.strftime("%Y-%m-%d")
        cx_dir = self.home / ".codex" / "sessions" / old_dt.strftime("%Y/%m/%d")
        cx_dir.mkdir(parents=True)
        proj = self.home / "old-proj"
        proj.mkdir()
        old_file = cx_dir / "old-rollout.jsonl"
        old_file.write_text("\n".join([
            json.dumps({"timestamp": _iso(old_dt), "type": "session_meta", "payload": {"id": "s-old", "originator": "codex_cli_rs", "cwd": str(proj)}}),
            json.dumps({"timestamp": _iso(old_dt), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-old", "started_at": _iso(old_dt)}}),
            json.dumps({"timestamp": _iso(old_dt + timedelta(seconds=70)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-old", "started_at": _iso(old_dt), "completed_at": _iso(old_dt + timedelta(seconds=70))}}),
        ]) + "\n", encoding="utf-8")
        os.utime(old_file, (old_dt.timestamp(), old_dt.timestamp()))

        r_backfill = c.collect_all(home=self.home, days=14, use_cache=False, now_ts=NOW_TS + 15)
        self.assertAlmostEqual(r_backfill["usage"]["periods"]["all"]["agent_seconds"], 70.0, delta=0.5)
        led1 = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        self.assertIn(old_day, led1["work_days"])
        self.assertIsNotNone(led1.get("work_history_from"))
        self.assertIsNotNone(led1.get("project_work_history_from"))

        # Part B: Separate HOME with global Antigravity work ONLY (no project paths)
        td_b = tempfile.TemporaryDirectory()
        self.addCleanup(td_b.cleanup)
        home_b = Path(td_b.name)
        agy_log = home_b / ".gemini" / "antigravity" / "brain" / "conv-1" / ".system_generated" / "logs" / "transcript.jsonl"
        agy_log.parent.mkdir(parents=True)
        t0 = NOW_DT - timedelta(minutes=15)
        agy_log.write_text("\n".join([
            json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": _iso(t0)}),
            json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _iso(t0 + timedelta(seconds=90))}),
        ]) + "\n", encoding="utf-8")

        c.collect_all(home=home_b, days=14, use_cache=False, now_ts=NOW_TS)
        led_b1 = lg.load_ledger(home_b / ".agent-pulse" / "ledger.json")
        self.assertIsNotNone(led_b1.get("work_history_from"))
        self.assertIsNone(led_b1.get("project_work_history_from"))

        # Now add a 40-day-old Codex log with a project path -> project backfill MUST still run!
        cx_b = home_b / ".codex" / "sessions" / old_dt.strftime("%Y/%m/%d")
        cx_b.mkdir(parents=True)
        proj_b = home_b / "old-proj-b"
        proj_b.mkdir()
        f_b = cx_b / "old-b.jsonl"
        f_b.write_text("\n".join([
            json.dumps({"timestamp": _iso(old_dt), "type": "session_meta", "payload": {"id": "sb", "originator": "codex_cli_rs", "cwd": str(proj_b)}}),
            json.dumps({"timestamp": _iso(old_dt), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "tb", "started_at": _iso(old_dt)}}),
            json.dumps({"timestamp": _iso(old_dt + timedelta(seconds=70)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "tb", "started_at": _iso(old_dt), "completed_at": _iso(old_dt + timedelta(seconds=70))}}),
        ]) + "\n", encoding="utf-8")
        os.utime(f_b, (old_dt.timestamp(), old_dt.timestamp()))

        r_b2 = c.collect_all(home=home_b, days=14, use_cache=False, now_ts=NOW_TS + 15)
        led_b2 = lg.load_ledger(home_b / ".agent-pulse" / "ledger.json")
        self.assertIsNotNone(led_b2.get("project_work_history_from"))
        trails = {t["path"]: t for t in r_b2["usage"]["project_trails"]}
        self.assertIn(str(proj_b), trails)
        self.assertAlmostEqual(trails[str(proj_b)]["periods"]["all"]["agent_seconds"], 70.0, delta=0.5)

    # ------------------------------------------------------------------
    # #4: Corrupt v5 usage_cache.json falls back cleanly matching --no-cache
    # ------------------------------------------------------------------
    def test_04_corrupt_v5_usage_cache_falls_back_cleanly_matching_nocache(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        t0 = NOW_DT - timedelta(minutes=20)
        f = cx_dir / "c.jsonl"
        f.write_text("\n".join([
            json.dumps({"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "sc4", "originator": "codex_cli_rs", "cwd": str(self.home / "p")}}),
            json.dumps({"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(seconds=30)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 12000, "output_tokens": 500}}}}),
        ]) + "\n", encoding="utf-8")

        # Seed clean v5 cache and capture --no-cache baseline
        baseline = _strip_perf(c.collect_all(home=self.home, days=7, use_cache=True, now_ts=NOW_TS))
        ucache_path = self.home / ".agent-pulse" / "usage_cache.json"
        raw_cache = json.loads(ucache_path.read_text(encoding="utf-8"))
        self.assertEqual(raw_cache["version"], 5)
        self.assertTrue(raw_cache["files"])

        for bad_rows in ([None], [{"day": TODAY_STR}], [{"tool": "codex", "day": TODAY_STR, "model": "m", "in": "bad"}]):
            corrupted = copy.deepcopy(raw_cache)
            for k in corrupted["files"]:
                corrupted["files"][k]["rows"] = bad_rows
            ucache_path.write_text(json.dumps(corrupted), encoding="utf-8")

            env = dict(os.environ, HOME=str(self.home), AGENTPULSE_NOW_TS=str(NOW_TS))
            script = str(ROOT / "engine" / "collector.py")
            p_fast = subprocess.run(["/usr/bin/python3", "-I", "-B", script, "--days", "7", "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
            p_md = subprocess.run(["/usr/bin/python3", "-I", "-B", script, "--days", "7", "--markdown", "--period", "today"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
            p_nc = subprocess.run(["/usr/bin/python3", "-I", "-B", script, "--days", "7", "--json", "--no-cache"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)

            self.assertEqual(p_fast.returncode, 0, p_fast.stderr)
            self.assertEqual(p_md.returncode, 0, p_md.stderr)
            self.assertEqual(p_nc.returncode, 0, p_nc.stderr)
            self.assertEqual(_strip_perf(json.loads(p_fast.stdout)), _strip_perf(json.loads(p_nc.stdout)))
            self.assertEqual(_strip_perf(json.loads(p_fast.stdout))["usage"]["periods"]["today"]["total_tokens"], baseline["usage"]["periods"]["today"]["total_tokens"])

    # ------------------------------------------------------------------
    # #5: Duplicate Codex rollouts deduplicate tokens & subscription candidates
    # ------------------------------------------------------------------
    def test_05_duplicate_codex_rollouts_dedup_tokens_and_subscription_candidate(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        t0 = NOW_DT - timedelta(minutes=25)
        tbl = pr.PricingTable({"gpt-5.3-codex": {"in": 1.75, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}}, {})

        # Session S1: Event 1 = 1M tokens ($1.75), 40s work
        s1_e1 = [
            {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "sess-1", "originator": "codex_cli_rs"}},
            {"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
            {"timestamp": _iso(t0), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-1", "started_at": _iso(t0)}},
            {"timestamp": _iso(t0 + timedelta(seconds=40)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-1", "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(seconds=40))}},
            {"timestamp": _iso(t0 + timedelta(seconds=41)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}},
        ]
        # Full duplicate rollout
        (cx_dir / "r1-orig.jsonl").write_text("\n".join(json.dumps(x) for x in s1_e1) + "\n", encoding="utf-8")
        (cx_dir / "r1-copy.jsonl").write_text("\n".join(json.dumps(x) for x in s1_e1) + "\n", encoding="utf-8")

        # Partial overlap rollout: repeats Event 1 + adds Event 2 (500k more tokens = $0.875)
        s1_e2 = s1_e1 + [
            {"timestamp": _iso(t0 + timedelta(seconds=90)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1_500_000, "output_tokens": 0}}}},
        ]
        (cx_dir / "r1-partial.jsonl").write_text("\n".join(json.dumps(x) for x in s1_e2) + "\n", encoding="utf-8")

        # Different session S2 with identical timestamp, turn_id, model, and amount (1M tokens = $1.75) -> MUST NOT dedup!
        s2_e1 = copy.deepcopy(s1_e1)
        s2_e1[0]["payload"]["id"] = "sess-2"
        s2_e1[2]["payload"]["turn_id"] = "turn-2"
        s2_e1[3]["payload"]["turn_id"] = "turn-2"
        (cx_dir / "r2-other-sess.jsonl").write_text("\n".join(json.dumps(x) for x in s2_e1) + "\n", encoding="utf-8")

        # Expected total: S1 (1M + 500k = 1.5M, $2.625) + S2 (1M, $1.75) = 2.5M tokens, $4.375!
        for mode_use_cache in (True, True, False):
            billing: dict = {}
            live = uc.collect_live_usage(
                home=self.home, days=7, use_cache=mode_use_cache, now_ts=NOW_TS, pricing=tbl,
                billing_out=billing, billing_cutoff_day="2026-11-01",
            )
            self.assertEqual(live["codex"][TODAY_STR]["in"], 2_500_000)
            self.assertAlmostEqual(billing["codex"][TODAY_STR]["subscription_candidate_usd"], 4.375, places=4)

        # Also check pure duplicate case alone (1M tokens, $1.75, 40s work)
        td2 = tempfile.TemporaryDirectory()
        self.addCleanup(td2.cleanup)
        h2 = Path(td2.name)
        d2 = h2 / ".codex" / "sessions" / "2026" / "11" / "27"
        d2.mkdir(parents=True)
        (d2 / "a.jsonl").write_text("\n".join(json.dumps(x) for x in s1_e1) + "\n", encoding="utf-8")
        (d2 / "b.jsonl").write_text("\n".join(json.dumps(x) for x in s1_e1) + "\n", encoding="utf-8")
        with patch.object(pr.PricingTable, "load", return_value=tbl):
            cold = _strip_perf(c.collect_all(home=h2, days=7, use_cache=True, now_ts=NOW_TS))
            warm = _strip_perf(c.collect_all(home=h2, days=7, use_cache=True, now_ts=NOW_TS))
            nc = _strip_perf(c.collect_all(home=h2, days=7, use_cache=False, now_ts=NOW_TS))
        self.assertEqual(cold, warm)
        self.assertEqual(cold, nc)
        self.assertAlmostEqual(cold["today"]["agent_seconds"], 40.0, delta=0.5)
        self.assertEqual(cold["usage"]["periods"]["today"]["total_tokens"], 1_000_000)
        codex_tool = next(t for t in cold["usage"]["periods"]["month"]["tools"] if t["tool"] == "codex")
        self.assertAlmostEqual(codex_tool["billing"]["subscription_candidate_usd"], 1.75, places=4)

    # ------------------------------------------------------------------
    # #6: Hermes API actual positive billing_mode whitelist
    # ------------------------------------------------------------------
    def test_06_hermes_billing_mode_strict_api_whitelist(self):
        h_db = self.home / ".hermes" / "state.db"
        h_db.parent.mkdir(parents=True)
        with sqlite3.connect(h_db) as conn:
            conn.execute("""CREATE TABLE session_model_usage (
                session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
                billing_mode TEXT, task TEXT, input_tokens INT, output_tokens INT,
                cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT,
                estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,
                cost_source TEXT, first_seen REAL, last_seen REAL
            )""")
            rows = [
                # Explicit API $3.25 -> INCLUDED
                ("s1", "m1", "openai", "https://api", "api", "chat", 1000, 0, 0, 0, 0, 0.0, 3.25, "actual", "provider_cost_api", NOW_TS - 300, NOW_TS - 120),
                # Explicit API zero $0.00 -> INCLUDED
                ("s2", "m1", "openai", "https://api", "api_key", "chat", 1000, 0, 0, 0, 0, 0.0, 0.0, "actual", "provider_cost_api", NOW_TS - 300, NOW_TS - 120),
                # NULL billing_mode $9.00 -> EXCLUDED!
                ("s3", "m1", "openai", "https://api", None, "chat", 1000, 0, 0, 0, 0, 0.0, 9.0, "actual", "provider_cost_api", NOW_TS - 300, NOW_TS - 120),
                # Unknown enum billing_mode $11.00 -> EXCLUDED!
                ("s4", "m1", "openai", "https://api", "mystery_route", "chat", 1000, 0, 0, 0, 0, 0.0, 11.0, "actual", "provider_cost_api", NOW_TS - 300, NOW_TS - 120),
                # subscription_included -> EXCLUDED from api_actual, INCLUDED in subscription_candidate ($2.00)
                ("s5", "m1", "openai", "https://api", "subscription_included", "chat", 1_000_000, 0, 0, 0, 0, 0.0, 7.77, "actual", "provider_cost_api", NOW_TS - 300, NOW_TS - 120),
            ]
            conn.executemany("INSERT INTO session_model_usage VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            conn.commit()

        tbl = pr.PricingTable({"m1": {"in": 2.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}}, {})
        billing: dict = {}
        uc.collect_live_usage(
            home=self.home, days=7, use_cache=False, now_ts=NOW_TS, pricing=tbl,
            billing_out=billing, billing_cutoff_day="2026-11-01",
        )
        self.assertAlmostEqual(billing["hermes"][TODAY_STR]["api_actual_usd"], 3.25, places=4)
        self.assertAlmostEqual(billing["hermes"][TODAY_STR]["subscription_candidate_usd"], 2.0, places=4)

        # Verify explicit $0.00 alone is preserved as 0.0 (not dropped)
        td_z = tempfile.TemporaryDirectory()
        self.addCleanup(td_z.cleanup)
        hz = Path(td_z.name)
        db_z = hz / ".hermes" / "state.db"
        db_z.parent.mkdir(parents=True)
        with sqlite3.connect(db_z) as conn:
            conn.execute("""CREATE TABLE session_model_usage (
                session_id TEXT, model TEXT, billing_provider TEXT, billing_base_url TEXT,
                billing_mode TEXT, task TEXT, input_tokens INT, output_tokens INT,
                cache_read_tokens INT, cache_write_tokens INT, reasoning_tokens INT,
                estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT,
                cost_source TEXT, first_seen REAL, last_seen REAL
            )""")
            conn.execute(
                "INSERT INTO session_model_usage VALUES ('sz','m1','p','u','api','t',100,0,0,0,0,0.0,0.0,'actual','provider_cost_api',?,?)",
                (NOW_TS - 200, NOW_TS - 100),
            )
            conn.commit()
        billing_z: dict = {}
        uc.collect_live_usage(home=hz, days=7, use_cache=False, now_ts=NOW_TS, pricing=tbl, billing_out=billing_z, billing_cutoff_day="2026-11-01")
        self.assertIn("api_actual_usd", billing_z["hermes"][TODAY_STR])
        self.assertEqual(billing_z["hermes"][TODAY_STR]["api_actual_usd"], 0.0)

    # ------------------------------------------------------------------
    # #7: T3 waiting_human whitelist + runtime_request run/thread/window checks
    # ------------------------------------------------------------------
    def test_07_t3_waiting_human_whitelist_and_runtime_req_window_checks(self):
        t3_db = self.home / ".t3" / "userdata" / "statev2.sqlite"
        t3_db.parent.mkdir(parents=True)
        t_start = NOW_DT - timedelta(minutes=2)

        with sqlite3.connect(t3_db) as conn:
            conn.execute("CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT, payload_json TEXT)")
            conn.execute("CREATE TABLE orchestration_v2_projection_nodes (node_id TEXT PRIMARY KEY, run_id TEXT, runtime_request_id TEXT, provider_turn_id TEXT)")
            conn.execute("CREATE TABLE orchestration_v2_projection_runs (run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INT, provider TEXT, provider_thread_id TEXT, status TEXT, requested_at TEXT, completed_at TEXT, payload_json TEXT)")
            conn.execute("CREATE TABLE orchestration_v2_projection_turn_items (turn_item_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, ordinal INT, type TEXT, status TEXT, updated_at TEXT, payload_json TEXT)")
            # Legacy table WITHOUT `kind` column!
            conn.execute("CREATE TABLE orchestration_v2_projection_runtime_requests (runtime_request_id TEXT PRIMARY KEY, thread_id TEXT, node_id TEXT, provider_turn_id TEXT, status TEXT, created_at TEXT, resolved_at TEXT)")

            for tid in ("th_unk", "th_rev", "th_legacy", "th_late", "th_two", "th_excl"):
                conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?, '', ?, '{}')", (tid, tid))

            # 1) Unknown status on approval_request -> MUST NOT be waiting_human
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('r_unk','th_unk',1,'codex','p','running',?,NULL,'{}')", (_iso(t_start),))
            conn.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES ('i_unk','th_unk','r_unk',1,'approval_request','unknown_state',?,?)",
                         (_iso(t_start + timedelta(seconds=10)), json.dumps({"startedAt": _iso(t_start + timedelta(seconds=10))})))

            # 2) Revoked status -> MUST NOT be waiting_human
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('r_rev','th_rev',1,'codex','p','running',?,NULL,'{}')", (_iso(t_start),))
            conn.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES ('i_rev','th_rev','r_rev',1,'approval_request','revoked',?,?)",
                         (_iso(t_start + timedelta(seconds=10)), json.dumps({"startedAt": _iso(t_start + timedelta(seconds=10))})))

            # 3) Legacy table without `kind` column -> valid waiting_human with wait_kind='other'
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('r_leg','th_legacy',1,'codex','p','running',?,NULL,'{}')", (_iso(t_start),))
            conn.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('req_leg','th_legacy',NULL,NULL,'pending',?,NULL)", (_iso(t_start + timedelta(seconds=20)),))

            # 4) Direct-linked runtime request with late/future created_at > now_ts (and pre-run created_at < start_ts) -> MUST NOT be waiting_human!
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('r_late','th_late',1,'codex','p','running',?,NULL,'{}')", (_iso(t_start),))
            conn.execute("INSERT INTO orchestration_v2_projection_nodes VALUES ('n_late1','r_late','req_future',NULL)")
            conn.execute("INSERT INTO orchestration_v2_projection_nodes VALUES ('n_late2','r_late','req_early',NULL)")
            conn.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('req_future','th_late','n_late1',NULL,'pending',?,NULL)", (_iso(NOW_DT + timedelta(seconds=60)),))
            conn.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('req_early','th_late','n_late2',NULL,'pending',?,NULL)", (_iso(t_start - timedelta(seconds=30)),))

            # 5) Same thread with 2 runs: direct-linked valid req attaches ONLY to r_two_a; unlinked req attaches to NEITHER
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('r_two_a','th_two',1,'claude','p1','running',?,NULL,'{}')", (_iso(t_start),))
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('r_two_b','th_two',2,'codex','p2','running',?,NULL,'{}')", (_iso(t_start),))
            conn.execute("INSERT INTO orchestration_v2_projection_nodes VALUES ('n_two','r_two_a','req_two_direct',NULL)")
            conn.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('req_two_direct','th_two','n_two',NULL,'pending',?,NULL)", (_iso(t_start + timedelta(seconds=15)),))
            conn.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('req_two_unlinked','th_two',NULL,NULL,'pending',?,NULL)", (_iso(t_start + timedelta(seconds=25)),))

            # 6) Completed run with 60s wait exclusion inside 100s run -> work seconds MUST stay 40s!
            ex_s = NOW_DT - timedelta(minutes=10)
            ex_e = ex_s + timedelta(seconds=100)
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES ('r_excl','th_excl',1,'codex','p','completed',?,?,?)",
                         (_iso(ex_s), _iso(ex_e), json.dumps({"startedAt": _iso(ex_s)})))
            conn.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('req_excl','th_excl',NULL,NULL,'resolved',?,?)",
                         (_iso(ex_s + timedelta(seconds=20)), _iso(ex_s + timedelta(seconds=80))))
            conn.commit()

        payload = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        waiting = [t for t in payload["sentinel_tasks"] if t["state"] == "waiting_human"]
        waiting_labels = sorted(t["label"] for t in waiting)
        self.assertEqual(waiting_labels, ["th_legacy", "th_two"])
        leg_task = next(t for t in waiting if t["label"] == "th_legacy")
        self.assertEqual(leg_task["evidence"]["wait_kind"], "other")
        two_tasks = [t for t in waiting if t["label"] == "th_two"]
        self.assertEqual(len(two_tasks), 1)
        self.assertEqual(two_tasks[0]["tool"], "t3_claude")
        # Verify work-hours exclusion remained intact for r_excl (40s)
        segs = c.collect_t3_code(t3_db, NOW_TS - 3600, NOW_TS)
        excl_sec = sum(s.duration for s in segs if s.run_key == "t3:r_excl")
        self.assertAlmostEqual(excl_sec, 40.0, delta=0.5)

    # ------------------------------------------------------------------
    # #8: Codex per-turn cwd isolation (ledger, project_trails, sentinel)
    # ------------------------------------------------------------------
    def test_08_codex_turn_cwd_isolation_ledger_trails_and_sentinel(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        proj_a = self.home / "proj-A"
        proj_b = self.home / "proj-B"
        proj_a.mkdir()
        proj_b.mkdir()
        t0 = NOW_DT - timedelta(minutes=10)

        events = [
            {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "s-cwd", "originator": "codex_cli_rs", "cwd": str(proj_a)}},
            # Turn 1: task_started THEN turn_context(cwd=B) -> 30s attributed to B!
            {"timestamp": _iso(t0), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1", "started_at": _iso(t0)}},
            {"timestamp": _iso(t0 + timedelta(seconds=1)), "type": "turn_context", "payload": {"model": "gpt-5.3-codex", "cwd": str(proj_b)}},
            {"timestamp": _iso(t0 + timedelta(seconds=30)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(seconds=30))}},
            # Turn 2: task_started with NO turn_context cwd -> 40s attributed to session default A!
            {"timestamp": _iso(t0 + timedelta(minutes=1)), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t2", "started_at": _iso(t0 + timedelta(minutes=1))}},
            {"timestamp": _iso(t0 + timedelta(minutes=1, seconds=40)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t2", "started_at": _iso(t0 + timedelta(minutes=1)), "completed_at": _iso(t0 + timedelta(minutes=1, seconds=40))}},
            # Turn 3: live active turn with turn_context(cwd=B) -> sentinel MUST point to B!
            {"timestamp": _iso(NOW_DT - timedelta(seconds=20)), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t3-live", "started_at": _iso(NOW_DT - timedelta(seconds=20))}},
            {"timestamp": _iso(NOW_DT - timedelta(seconds=19)), "type": "turn_context", "payload": {"model": "gpt-5.3-codex", "cwd": str(proj_b)}},
            {"timestamp": _iso(NOW_DT - timedelta(seconds=5)), "type": "event_msg", "payload": {"type": "item_completed", "turn_id": "t3-live"}},
        ]
        f = cx_dir / "cwd-test.jsonl"
        f.write_text("\n".join(json.dumps(x) for x in events) + "\n", encoding="utf-8")
        os.utime(f, (NOW_TS - 5, NOW_TS - 5))

        payload = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        led = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        # Raw ledger project_work_days: A=40s, B=30s + 20s(live) = 50s
        self.assertAlmostEqual(led["project_work_days"][str(proj_a)][TODAY_STR]["agent_seconds"], 40.0, delta=1.0)
        self.assertAlmostEqual(led["project_work_days"][str(proj_b)][TODAY_STR]["agent_seconds"], 50.0, delta=1.0)

        # Project trails today: A=40s, B=50s
        trails = {t["path"]: t for t in payload["usage"]["project_trails"]}
        self.assertAlmostEqual(trails[str(proj_a)]["periods"]["today"]["agent_seconds"], 40.0, delta=1.0)
        self.assertAlmostEqual(trails[str(proj_b)]["periods"]["today"]["agent_seconds"], 50.0, delta=1.0)

        # Sentinel Finder path for active Turn 3 points to B!
        running = [t for t in payload["sentinel_tasks"] if t["state"] == "running"]
        self.assertEqual(len(running), 1)
        self.assertEqual(running[0]["workspace_path"], str(proj_b))
        self.assertEqual(running[0]["project_path"], str(proj_b))

    # ------------------------------------------------------------------
    # #9: Quota Pace raw precision, future timestamp rejection, reset retention
    # ------------------------------------------------------------------
    def test_09_quota_pace_raw_precision_future_rejection_and_reset_retention(self):
        t_now = 1795676400.0
        # 45 minutes to reset (2700s): 60 -> 65 -> 69.96 over 30m => rate=19.92/h, projected=84.90% -> on_track!
        resets_at = int(t_now + 2700)
        auth_ctx = {"account_key": "acct-raw", "access_token": "tok"}

        def make_resp(used_pct: float, reset_epoch: int):
            body = json.dumps({
                "plan_type": "pro",
                "rate_limit": {
                    "primary_window": {
                        "used_percent": used_pct,
                        "limit_window_seconds": 18000,
                        "reset_at": reset_epoch,
                    }
                },
            }).encode("utf-8")

            class _Resp:
                def __enter__(self): return self
                def __exit__(self, *a): pass
                def geturl(self): return qc.CODEX_USAGE_URL
                def read(self, n=-1): return body

            return _Resp()

        with patch.object(qc, "_codex_auth_context", return_value=auth_ctx), patch.object(
            qc.urllib.request, "build_opener"
        ) as opener:
            opener.return_value.open.side_effect = [
                make_resp(60.0, resets_at),
                make_resp(65.0, resets_at),
                make_resp(69.96, resets_at),
            ]
            qc.collect_quotas(home=self.home, force=True, now_ts=t_now - 1800, allow_live=True)
            qc.collect_quotas(home=self.home, force=True, now_ts=t_now - 900, allow_live=True)
            s3 = qc.collect_quotas(home=self.home, force=True, now_ts=t_now, allow_live=True)
            w3 = s3["providers"][0]["windows"][0]
            self.assertEqual(w3["pace"]["level"], "on_track")
            self.assertAlmostEqual(w3["pace"]["projected_used_percent"], 84.9, places=1)
            self.assertNotIn("_raw_used_percent", w3)

        # Future observation time (clock rolled back 10 minutes) on Claude 96% -> MUST NOT be ok or highest_window!
        data_dir = self.home / ".agent-pulse"
        data_dir.mkdir(mode=0o700, exist_ok=True)
        future_obs = int(t_now + 600)
        (data_dir / "claude_statusline_quota.json").write_text(json.dumps({
            "updated": future_obs,
            "source": "cli",
            "q5": 96.0,
            "q5_reset": int(t_now + 7200),
        }), encoding="utf-8")
        s_future = qc.collect_quotas(home=self.home, force=False, now_ts=t_now, allow_live=False)
        claude_p = next(p for p in s_future["providers"] if p["id"] == "claude")
        self.assertEqual(claude_p["status"], "stale")
        self.assertNotIn("fresh_until_ts", claude_p["windows"][0])
        self.assertIsNone(s_future["highest_window"])

        # Window reset retention: valid observation before reset, then refresh AFTER reset!
        orig_obs = int(t_now - 300)
        reset_ts = int(t_now + 100)
        (data_dir / "claude_statusline_quota.json").write_text(json.dumps({
            "updated": orig_obs,
            "source": "cli",
            "q5": 77.5,
            "q5_reset": reset_ts,
        }), encoding="utf-8")
        s_pre = qc.collect_quotas(home=self.home, force=False, now_ts=t_now, allow_live=False)
        self.assertEqual(s_pre["highest_window"]["provider_id"], "claude")

        s_post = qc.collect_quotas(home=self.home, force=False, now_ts=float(reset_ts + 60), allow_live=False)
        claude_post = next(p for p in s_post["providers"] if p["id"] == "claude")
        self.assertEqual(claude_post["status"], "stale")
        self.assertEqual(claude_post["fetched_at"], orig_obs)
        self.assertEqual(len(claude_post["windows"]), 1)
        self.assertAlmostEqual(claude_post["windows"][0]["used_percent"], 77.5, places=1)
        self.assertNotIn("fresh_until_ts", claude_post["windows"][0])
        self.assertEqual(claude_post["windows"][0]["pace"]["reason"], "stale")
        self.assertIsNone(s_post["highest_window"])

    # ------------------------------------------------------------------
    # #11: Unknown work status vs observed zero vs retained non-zero
    # ------------------------------------------------------------------
    def test_11_unknown_work_status_vs_observed_zero_vs_retained_nonzero(self):
        # 1) Unknown: first scan with ONLY corrupt Codex file and no prior bucket
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        (cx_dir / "bad.jsonl").write_text("{corrupt_json\n", encoding="utf-8")

        p_unk = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        self.assertEqual(p_unk["today"]["work_status"], "unknown")
        self.assertFalse(p_unk["today"]["hourly_available"])
        self.assertEqual(p_unk["usage"]["periods"]["today"]["work_status"], "unknown")
        md_unk_day = c.render_markdown_report(p_unk)
        md_unk_period = c.render_markdown_report(p_unk, period="today")
        self.assertIn("**—**", md_unk_day)
        self.assertNotIn("**0.00 h**", md_unk_day)
        self.assertIn("**—**", md_unk_period)
        self.assertNotIn("**0.00 h**", md_unk_period)

        # 2) Observed zero: healthy empty Codex directory
        (cx_dir / "bad.jsonl").unlink()
        p_zero = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 15)
        self.assertEqual(p_zero["today"]["work_status"], "observed")
        self.assertTrue(p_zero["today"]["hourly_available"])
        self.assertEqual(p_zero["usage"]["periods"]["today"]["work_status"], "observed")
        md_zero_day = c.render_markdown_report(p_zero)
        md_zero_period = c.render_markdown_report(p_zero, period="today")
        self.assertIn("**0.00 h**", md_zero_day)
        self.assertIn("**0.00 h**", md_zero_period)

        # 3) Retained non-zero: good bucket then corrupt source
        t0 = NOW_DT - timedelta(minutes=10)
        good_f = cx_dir / "good.jsonl"
        good_f.write_text("\n".join([
            json.dumps({"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "sg", "originator": "codex_cli_rs"}}),
            json.dumps({"timestamp": _iso(t0), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "tg", "started_at": _iso(t0)}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(seconds=180)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "tg", "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(seconds=180))}}),
        ]) + "\n", encoding="utf-8")
        c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 30)
        good_f.write_text("{broken_again\n", encoding="utf-8")
        p_ret = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 45)
        self.assertEqual(p_ret["today"]["work_status"], "retained")
        self.assertAlmostEqual(p_ret["today"]["agent_seconds"], 180.0, delta=0.5)
        md_ret = c.render_markdown_report(p_ret)
        self.assertIn("**0.05 h**", md_ret)
        self.assertIn("数据完整性提示", md_ret)

    # ------------------------------------------------------------------
    # #12: Heatmap & project_trails unpriced_tokens 4 states
    # ------------------------------------------------------------------
    def test_12_heatmap_and_project_trails_unpriced_four_states(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "25"
        cx_dir.mkdir(parents=True)
        hist_dt = NOW_DT - timedelta(days=2)
        hist_day = hist_dt.strftime("%Y-%m-%d")

        p_unpriced = self.home / "proj-unpriced"
        p_mixed = self.home / "proj-mixed"
        p_zero = self.home / "proj-zero"
        p_work_only = self.home / "proj-work-only"
        for d in (p_unpriced, p_mixed, p_zero, p_work_only):
            d.mkdir()

        # 1) Pure unpriced (1M unpriced tokens, $0.00)
        (cx_dir / "u1.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(hist_dt), "type": "session_meta", "payload": {"id": "su1", "originator": "codex_cli_rs", "cwd": str(p_unpriced)}}),
            json.dumps({"timestamp": _iso(hist_dt), "type": "turn_context", "payload": {"model": "mystery-unpriced"}}),
            json.dumps({"timestamp": _iso(hist_dt + timedelta(seconds=10)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}}),
        ]) + "\n", encoding="utf-8")

        # 2) Mixed priced ($1.50) + unpriced (500k)
        (cx_dir / "u2.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(hist_dt), "type": "session_meta", "payload": {"id": "su2", "originator": "codex_cli_rs", "cwd": str(p_mixed)}}),
            json.dumps({"timestamp": _iso(hist_dt), "type": "turn_context", "payload": {"model": "priced-3-per-m"}}),
            json.dumps({"timestamp": _iso(hist_dt + timedelta(seconds=10)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 500_000, "output_tokens": 0}}}}),
            json.dumps({"timestamp": _iso(hist_dt + timedelta(seconds=20)), "type": "turn_context", "payload": {"model": "mystery-unpriced"}}),
            json.dumps({"timestamp": _iso(hist_dt + timedelta(seconds=30)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 500_000, "output_tokens": 0}}}}),
        ]) + "\n", encoding="utf-8")

        # 3) Confirmed zero (priced $0.00 free model, 100k tokens)
        (cx_dir / "u3.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(hist_dt), "type": "session_meta", "payload": {"id": "su3", "originator": "codex_cli_rs", "cwd": str(p_zero)}}),
            json.dumps({"timestamp": _iso(hist_dt), "type": "turn_context", "payload": {"model": "free-zero-model"}}),
            json.dumps({"timestamp": _iso(hist_dt + timedelta(seconds=10)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100_000, "output_tokens": 0}}}}),
        ]) + "\n", encoding="utf-8")

        # 4) Work-only project (no token/cost observation)
        (cx_dir / "u4.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(hist_dt), "type": "session_meta", "payload": {"id": "su4", "originator": "codex_cli_rs", "cwd": str(p_work_only)}}),
            json.dumps({"timestamp": _iso(hist_dt), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "tw4", "started_at": _iso(hist_dt)}}),
            json.dumps({"timestamp": _iso(hist_dt + timedelta(seconds=60)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "tw4", "started_at": _iso(hist_dt), "completed_at": _iso(hist_dt + timedelta(seconds=60))}}),
        ]) + "\n", encoding="utf-8")

        tbl = pr.PricingTable(
            {
                "priced-3-per-m": {"in": 3.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0},
                "free-zero-model": {"in": 0.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0},
            },
            {},
        )
        with patch.object(pr.PricingTable, "load", return_value=tbl):
            payload = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)

        # Check heatmap day + tool row for hist_day (1.5M unpriced total, $1.50 priced)
        hm_days = {d["date"]: d for d in payload["usage"]["heatmap"]["days"]}
        hday = hm_days[hist_day]
        self.assertEqual(hday["unpriced_tokens"], 1_500_000)
        self.assertAlmostEqual(hday["cost_usd"], 1.50, places=2)
        self.assertTrue(hday["has_cost_observation"])
        h_codex = next(t for t in hday["tools"] if t["tool"] == "codex")
        self.assertEqual(h_codex["unpriced_tokens"], 1_500_000)
        self.assertTrue(h_codex["has_cost_observation"])

        # Idle day (today) -> no observation
        today_hm = hm_days[TODAY_STR]
        self.assertEqual(today_hm["unpriced_tokens"], 0)
        self.assertFalse(today_hm["has_cost_observation"])

        # Check all 4 project states in project_trails[].periods.week
        trails = {t["path"]: t["periods"]["week"] for t in payload["usage"]["project_trails"]}
        # Pure unpriced
        self.assertEqual(trails[str(p_unpriced)]["unpriced_tokens"], 1_000_000)
        self.assertEqual(trails[str(p_unpriced)]["cost_usd"], 0.0)
        self.assertTrue(trails[str(p_unpriced)]["has_cost_observation"])
        # Mixed
        self.assertEqual(trails[str(p_mixed)]["unpriced_tokens"], 500_000)
        self.assertAlmostEqual(trails[str(p_mixed)]["cost_usd"], 1.50, places=2)
        self.assertTrue(trails[str(p_mixed)]["has_cost_observation"])
        # Confirmed zero
        self.assertEqual(trails[str(p_zero)]["unpriced_tokens"], 0)
        self.assertEqual(trails[str(p_zero)]["cost_usd"], 0.0)
        self.assertTrue(trails[str(p_zero)]["has_cost_observation"])
        # Work-only (no usage observation)
        self.assertEqual(trails[str(p_work_only)]["unpriced_tokens"], 0)
        self.assertEqual(trails[str(p_work_only)]["cost_usd"], 0.0)
        self.assertFalse(trails[str(p_work_only)]["has_cost_observation"])

    # ------------------------------------------------------------------
    # Round-2 #1: Valid JSON with bad metering token field preserves old bucket
    # ------------------------------------------------------------------
    def test_r2_01_bad_token_field_in_valid_json_preserves_old_bucket_and_recovers(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        t0 = NOW_DT - timedelta(minutes=20)
        f = cx_dir / "rollout-bad-field.jsonl"

        base_head = [
            {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "s-bf", "originator": "codex_cli_rs", "cwd": str(self.home / "proj-bf")}},
            {"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
            # Explicitly empty info event & omitted optional fields must stay valid
            {"timestamp": _iso(t0 + timedelta(seconds=5)), "type": "event_msg", "payload": {"type": "token_count", "info": None}},
            {"timestamp": _iso(t0 + timedelta(seconds=10)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000}}}},
        ]
        good_second = {
            "timestamp": _iso(t0 + timedelta(seconds=20)),
            "type": "event_msg",
            "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 4000, "output_tokens": 0}}},
        }
        f.write_text("\n".join(json.dumps(x) for x in base_head + [good_second]) + "\n", encoding="utf-8")

        r1 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        self.assertEqual(r1["collection"]["sources"]["usage.codex"]["status"], "complete")
        self.assertEqual(r1["usage"]["periods"]["today"]["total_tokens"], 5000)
        old_bucket = copy.deepcopy(lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")["live_tools"]["codex"][TODAY_STR])

        # Corrupt ONLY the second event's input_tokens with bad string / bool / float / negative
        for bad_val in ("broken", True, 1.5, -10, ""):
            bad_second = copy.deepcopy(good_second)
            bad_second["payload"]["info"]["last_token_usage"]["input_tokens"] = bad_val
            f.write_text("\n".join(json.dumps(x) for x in base_head + [bad_second]) + "\n", encoding="utf-8")
            r_bad = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 15)
            self.assertIn(r_bad["collection"]["sources"]["usage.codex"]["status"], ("partial", "unavailable"), f"bad_val={bad_val!r}")
            self.assertEqual(r_bad["usage"]["periods"]["today"]["total_tokens"], 5000, f"bad_val={bad_val!r}")
            self.assertEqual(
                lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")["live_tools"]["codex"][TODAY_STR],
                old_bucket,
                f"bad_val={bad_val!r}",
            )

        # Recover with updated 5,000 second event -> 6,000 total
        recovered_second = copy.deepcopy(good_second)
        recovered_second["payload"]["info"]["last_token_usage"]["input_tokens"] = 5000
        f.write_text("\n".join(json.dumps(x) for x in base_head + [recovered_second]) + "\n", encoding="utf-8")
        r_ok = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 30)
        self.assertEqual(r_ok["collection"]["sources"]["usage.codex"]["status"], "complete")
        self.assertEqual(r_ok["usage"]["periods"]["today"]["total_tokens"], 6000)

    # ------------------------------------------------------------------
    # Round-2 #2: Codex cross-file cumulative order & cross-format mirrors
    # ------------------------------------------------------------------
    def test_r2_02_codex_cross_file_cumulative_order_and_cross_format_mirror(self):
        t0 = NOW_DT - timedelta(minutes=25)
        tbl = pr.PricingTable({"gpt-5.3-codex": {"in": 1.75, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}}, {})

        # Subtest A: full file (1M + 3M) + tail-only copy (3M) in BOTH filename orders!
        full_events = [
            {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "sess-cum", "originator": "codex_cli_rs"}},
            {"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
            {"timestamp": _iso(t0 + timedelta(seconds=10)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}},
            {"timestamp": _iso(t0 + timedelta(seconds=50)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 3_000_000, "output_tokens": 0}}}},
        ]
        tail_events = [
            {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "sess-cum", "originator": "codex_cli_rs"}},
            {"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
            {"timestamp": _iso(t0 + timedelta(seconds=50)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 3_000_000, "output_tokens": 0}}}},
        ]
        for names in (("a-full.jsonl", "z-tail.jsonl"), ("z-full.jsonl", "a-tail.jsonl")):
            with tempfile.TemporaryDirectory() as td:
                h = Path(td)
                d = h / ".codex" / "sessions" / "2026" / "11" / "27"
                d.mkdir(parents=True)
                (d / names[0]).write_text("\n".join(json.dumps(x) for x in full_events) + "\n", encoding="utf-8")
                (d / names[1]).write_text("\n".join(json.dumps(x) for x in tail_events) + "\n", encoding="utf-8")
                with patch.object(pr.PricingTable, "load", return_value=tbl):
                    cold = _strip_perf(c.collect_all(home=h, days=7, use_cache=True, now_ts=NOW_TS))
                    warm = _strip_perf(c.collect_all(home=h, days=7, use_cache=True, now_ts=NOW_TS))
                    nc = _strip_perf(c.collect_all(home=h, days=7, use_cache=False, now_ts=NOW_TS))
                self.assertEqual(cold, warm, f"order={names}")
                self.assertEqual(cold, nc, f"order={names}")
                self.assertEqual(cold["usage"]["periods"]["today"]["total_tokens"], 3_000_000, f"order={names}")
                codex_m = next(t for t in cold["usage"]["periods"]["month"]["tools"] if t["tool"] == "codex")
                self.assertAlmostEqual(codex_m["billing"]["subscription_candidate_usd"], 5.25, places=4, msg=f"order={names}")

        # Subtest B: legacy token_count and new token_usage_record split across TWO files (both with & without thread_token_usage)
        legacy_file_events = [
            {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "sess-mirror", "originator": "codex_cli_rs"}},
            {"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
            {"timestamp": _iso(t0 + timedelta(seconds=20)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "last_token_usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}},
        ]
        for include_thread_total in (True, False):
            new_payload = {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "response_id": "resp-split-1", "thread_id": "sess-mirror"}
            if include_thread_total:
                new_payload["thread_token_usage"] = {"input_tokens": 1_000_000, "output_tokens": 0}
            new_file_events = [
                {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "sess-mirror", "originator": "codex_cli_rs"}},
                {"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
                {"timestamp": _iso(t0 + timedelta(seconds=20)), "type": "token_usage_record", "payload": new_payload},
            ]
            for names in (("a-old.jsonl", "z-new.jsonl"), ("z-old.jsonl", "a-new.jsonl")):
                with tempfile.TemporaryDirectory() as td:
                    h = Path(td)
                    d = h / ".codex" / "sessions" / "2026" / "11" / "27"
                    d.mkdir(parents=True)
                    (d / names[0]).write_text("\n".join(json.dumps(x) for x in legacy_file_events) + "\n", encoding="utf-8")
                    (d / names[1]).write_text("\n".join(json.dumps(x) for x in new_file_events) + "\n", encoding="utf-8")
                    with patch.object(pr.PricingTable, "load", return_value=tbl):
                        cold = _strip_perf(c.collect_all(home=h, days=7, use_cache=True, now_ts=NOW_TS))
                        warm = _strip_perf(c.collect_all(home=h, days=7, use_cache=True, now_ts=NOW_TS))
                        nc = _strip_perf(c.collect_all(home=h, days=7, use_cache=False, now_ts=NOW_TS))
                    self.assertEqual(cold, warm)
                    self.assertEqual(cold, nc)
                    self.assertEqual(cold["usage"]["periods"]["today"]["total_tokens"], 1_000_000)
                    codex_m = next(t for t in cold["usage"]["periods"]["month"]["tools"] if t["tool"] == "codex")
                    self.assertAlmostEqual(codex_m["billing"]["subscription_candidate_usd"], 1.75, places=4)

        # Subtest C: two same-amount independent events in one session + duplicate file copy -> counted twice, not once or 4x
        with tempfile.TemporaryDirectory() as td:
            h = Path(td)
            d = h / ".codex" / "sessions" / "2026" / "11" / "27"
            d.mkdir(parents=True)
            indep_events = [
                {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "sess-indep", "originator": "codex_cli_rs"}},
                {"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}},
                {"timestamp": _iso(t0 + timedelta(seconds=10)), "type": "token_usage_record", "payload": {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "response_id": "r-indep-1"}},
                {"timestamp": _iso(t0 + timedelta(seconds=10)), "type": "token_usage_record", "payload": {"usage": {"input_tokens": 1_000_000, "output_tokens": 0}, "response_id": "r-indep-2"}},
            ]
            (d / "indep-1.jsonl").write_text("\n".join(json.dumps(x) for x in indep_events) + "\n", encoding="utf-8")
            (d / "indep-2-copy.jsonl").write_text("\n".join(json.dumps(x) for x in indep_events) + "\n", encoding="utf-8")
            with patch.object(pr.PricingTable, "load", return_value=tbl):
                cold = _strip_perf(c.collect_all(home=h, days=7, use_cache=True, now_ts=NOW_TS))
                warm = _strip_perf(c.collect_all(home=h, days=7, use_cache=True, now_ts=NOW_TS))
                nc = _strip_perf(c.collect_all(home=h, days=7, use_cache=False, now_ts=NOW_TS))
            self.assertEqual(cold, warm)
            self.assertEqual(cold, nc)
            self.assertEqual(cold["usage"]["periods"]["today"]["total_tokens"], 2_000_000)
            codex_m = next(t for t in cold["usage"]["periods"]["month"]["tools"] if t["tool"] == "codex")
            self.assertAlmostEqual(codex_m["billing"]["subscription_candidate_usd"], 3.50, places=4)

    # ------------------------------------------------------------------
    # Round-2 #3: Late same-turn cwd attribution & v0.6 upgrade project dedup
    # ------------------------------------------------------------------
    def test_r2_03_late_turn_cwd_attribution_and_v06_upgrade_project_reconciliation(self):
        t0 = NOW_DT - timedelta(minutes=15)
        tbl = pr.PricingTable({"gpt-5.3-codex": {"in": 1.75, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}}, {})

        # Part A: session cwd A, turn t1 emits usage BEFORE turn_context(turn_id=t1, cwd=B)
        proj_a = self.home / "proj-late-A"
        proj_b = self.home / "proj-late-B"
        proj_a.mkdir()
        proj_b.mkdir()
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        late_events = [
            {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "s-late-cwd", "originator": "codex_cli_rs", "cwd": str(proj_a)}},
            {"timestamp": _iso(t0), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-late", "started_at": _iso(t0)}},
            {"timestamp": _iso(t0 + timedelta(seconds=10)), "type": "event_msg", "payload": {"type": "token_count", "turn_id": "t-late", "info": {"total_token_usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}},
            {"timestamp": _iso(t0 + timedelta(seconds=20)), "type": "turn_context", "payload": {"turn_id": "t-late", "model": "gpt-5.3-codex", "cwd": str(proj_b)}},
            {"timestamp": _iso(t0 + timedelta(seconds=30)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-late", "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(seconds=30))}},
        ]
        (cx_dir / "late-cwd.jsonl").write_text("\n".join(json.dumps(x) for x in late_events) + "\n", encoding="utf-8")

        with patch.object(pr.PricingTable, "load", return_value=tbl):
            p_late = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        trails_a = {t["path"]: t for t in p_late["usage"]["project_trails"]}
        self.assertNotIn(str(proj_a), trails_a)
        self.assertIn(str(proj_b), trails_a)
        self.assertAlmostEqual(trails_a[str(proj_b)]["periods"]["today"]["agent_seconds"], 30.0, delta=0.5)
        self.assertEqual(trails_a[str(proj_b)]["periods"]["today"]["total_tokens"], 1_000_000)
        self.assertAlmostEqual(trails_a[str(proj_b)]["periods"]["today"]["cost_usd"], 1.75, places=4)

        # Part B: Simulate realistic v0.6 ledger upgrade (v0.6 placed 1M tokens in proj-A for today,
        # plus historical proj-hist 30 days ago outside the 7-day scan window)
        with tempfile.TemporaryDirectory() as td_up:
            h_up = Path(td_up)
            up_a = h_up / "proj-up-A"
            up_b = h_up / "proj-up-B"
            up_hist = h_up / "proj-hist-old"
            for d in (up_a, up_b, up_hist):
                d.mkdir()
            hist_day = (NOW_DT - timedelta(days=30)).strftime("%Y-%m-%d")

            # Seed realistic v0.6 ledger (no `collection`, no `project_work_days`, stale today attribution on up_a)
            data_dir = h_up / ".agent-pulse"
            data_dir.mkdir(mode=0o700)
            v06_ledger = {
                "version": 1,
                "updated_at": _iso(NOW_DT - timedelta(minutes=1)),
                "tokei_import_meta": None,
                "imported_tools": {},
                "live_tools": {
                    "codex": {
                        TODAY_STR: {
                            "in": 1_000_000, "out": 0, "cr": 0, "cw": 0, "thoughts": 0,
                            "cost_usd": 1.75, "requests": 1, "sessions": 1,
                            "models": {"gpt-5.3-codex": {"in": 1_000_000, "out": 0, "cr": 0, "cw": 0, "thoughts": 0, "cost_usd": 1.75, "requests": 1}},
                        }
                    }
                },
                "projects": {
                    str(up_a): {
                        TODAY_STR: {"in": 1_000_000, "out": 0, "cr": 0, "cw": 0, "thoughts": 0, "cost_usd": 1.75, "sessions": 1, "tools": ["codex"]}
                    },
                    str(up_hist): {
                        hist_day: {"in": 200_000, "out": 0, "cr": 0, "cw": 0, "thoughts": 0, "cost_usd": 0.35, "sessions": 1, "tools": ["codex"]}
                    },
                },
                "work_days": {},
            }
            (data_dir / "ledger.json").write_text(json.dumps(v06_ledger), encoding="utf-8")

            # Place Codex log where turn_context(cwd=up_b) precedes usage
            cx_up = h_up / ".codex" / "sessions" / "2026" / "11" / "27"
            cx_up.mkdir(parents=True)
            up_events = [
                {"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "s-up", "originator": "codex_cli_rs", "cwd": str(up_a)}},
                {"timestamp": _iso(t0), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-up", "started_at": _iso(t0)}},
                {"timestamp": _iso(t0 + timedelta(seconds=1)), "type": "turn_context", "payload": {"turn_id": "t-up", "model": "gpt-5.3-codex", "cwd": str(up_b)}},
                {"timestamp": _iso(t0 + timedelta(seconds=20)), "type": "event_msg", "payload": {"type": "token_count", "turn_id": "t-up", "info": {"total_token_usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}},
                {"timestamp": _iso(t0 + timedelta(seconds=30)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-up", "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(seconds=30))}},
            ]
            (cx_up / "up.jsonl").write_text("\n".join(json.dumps(x) for x in up_events) + "\n", encoding="utf-8")

            with patch.object(pr.PricingTable, "load", return_value=tbl):
                p_up = c.collect_all(home=h_up, days=7, use_cache=False, now_ts=NOW_TS)

            # Global today = 1M / $1.75
            self.assertEqual(p_up["usage"]["periods"]["today"]["total_tokens"], 1_000_000)
            self.assertAlmostEqual(p_up["usage"]["periods"]["today"]["cost_usd"], 1.75, places=4)

            # Legacy usage.projects: ONLY up_b (1M) + preserved out-of-window up_hist (200k); up_a reconciled away!
            legacy_projs = {p["path"]: p for p in p_up["usage"]["projects"]}
            self.assertNotIn(str(up_a), legacy_projs)
            self.assertIn(str(up_b), legacy_projs)
            self.assertEqual(legacy_projs[str(up_b)]["total_tokens"], 1_000_000)
            self.assertIn(str(up_hist), legacy_projs)
            self.assertEqual(legacy_projs[str(up_hist)]["total_tokens"], 200_000)

            # New usage.project_trails: ONLY up_b (today=1M/$1.75/30s) + up_hist (all=200k); NO duplicate on up_a!
            up_trails = {t["path"]: t for t in p_up["usage"]["project_trails"]}
            self.assertNotIn(str(up_a), up_trails)
            self.assertEqual(up_trails[str(up_b)]["periods"]["today"]["total_tokens"], 1_000_000)
            self.assertAlmostEqual(up_trails[str(up_b)]["periods"]["today"]["cost_usd"], 1.75, places=4)
            self.assertEqual(up_trails[str(up_hist)]["periods"]["all"]["total_tokens"], 200_000)

    # ------------------------------------------------------------------
    # Round-2 #4: OverflowError on huge integer (10**309) in v5 caches
    # ------------------------------------------------------------------
    def test_r2_04_overflow_huge_int_in_v5_caches_falls_back_cleanly(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        t0 = NOW_DT - timedelta(minutes=20)
        f = cx_dir / "overflow-test.jsonl"
        f.write_text("\n".join([
            json.dumps({"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "s-ovf", "originator": "codex_cli_rs", "cwd": str(self.home / "p")}}),
            json.dumps({"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}}),
            json.dumps({"timestamp": _iso(t0), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t-ovf", "started_at": _iso(t0)}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(seconds=30)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t-ovf", "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(seconds=30))}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(seconds=31)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 12000, "output_tokens": 500}}}}),
        ]) + "\n", encoding="utf-8")
        os.utime(f, (NOW_TS - 300, NOW_TS - 300))

        # Seed valid v5 usage_cache.json and cache.json
        c.collect_all(home=self.home, days=7, use_cache=True, now_ts=NOW_TS)
        ucache_path = self.home / ".agent-pulse" / "usage_cache.json"
        scache_path = self.home / ".agent-pulse" / "cache.json"
        raw_uc = json.loads(ucache_path.read_text(encoding="utf-8"))
        raw_sc = json.loads(scache_path.read_text(encoding="utf-8"))

        huge_int = 10 ** 309
        for k in raw_uc["files"]:
            raw_uc["files"][k]["rows"][0]["cost_usd"] = huge_int
        ucache_path.write_text(json.dumps(raw_uc), encoding="utf-8")

        for k in raw_sc["files"]:
            if raw_sc["files"][k].get("segs"):
                raw_sc["files"][k]["segs"][0][1] = huge_int
        scache_path.write_text(json.dumps(raw_sc), encoding="utf-8")

        env = dict(os.environ, HOME=str(self.home), AGENTPULSE_NOW_TS=str(NOW_TS))
        script = str(ROOT / "engine" / "collector.py")
        p_fast = subprocess.run(["/usr/bin/python3", "-I", "-B", script, "--days", "7", "--json"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)
        p_nc = subprocess.run(["/usr/bin/python3", "-I", "-B", script, "--days", "7", "--json", "--no-cache"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True)

        self.assertEqual(p_fast.returncode, 0, p_fast.stderr)
        self.assertEqual(p_nc.returncode, 0, p_nc.stderr)
        self.assertEqual(_strip_perf(json.loads(p_fast.stdout)), _strip_perf(json.loads(p_nc.stdout)))

    # ------------------------------------------------------------------
    # Round-2 #5: Offline Claude updated=0 never fabricates now; 69.96% raw precision
    # ------------------------------------------------------------------
    def test_r2_05_offline_quota_updated_zero_and_raw_precision_6996(self):
        t_now = 1795676400.0
        data_dir = self.home / ".agent-pulse"
        data_dir.mkdir(mode=0o700, exist_ok=True)

        # Case A: Offline Claude cache with 97%, future reset, updated=0 -> MUST be stale, fetched_at=0, highest_window=None
        (data_dir / "quota_cache.json").write_text(json.dumps({
            "claude": {"q5": 97.0, "q5_reset": int(t_now + 3600), "updated": 0}
        }), encoding="utf-8")
        q_zero = qc.collect_quotas(home=self.home, force=False, now_ts=t_now, allow_live=False)
        claude_zero = next(p for p in q_zero["providers"] if p["id"] == "claude")
        self.assertEqual(claude_zero["status"], "stale")
        self.assertEqual(claude_zero["fetched_at"], 0)
        self.assertNotIn("fresh_until_ts", claude_zero["windows"][0])
        self.assertIsNone(q_zero["highest_window"])

        # Case B: Statusline 69.96% -> display used_percent=70.0, raw_used_percent=69.96 (< 70.0!)
        qc.capture_claude_statusline(
            self.home,
            {"rate_limits": {"five_hour": {"used_percentage": 69.96, "resets_at": int(t_now + 7200)}}},
            t_now - 30,
        )
        q_6996 = qc.collect_quotas(home=self.home, force=False, now_ts=t_now, allow_live=False)
        claude_6996 = next(p for p in q_6996["providers"] if p["id"] == "claude")
        self.assertEqual(claude_6996["status"], "ok")
        self.assertEqual(claude_6996["windows"][0]["used_percent"], 70.0)
        self.assertAlmostEqual(claude_6996["windows"][0]["raw_used_percent"], 69.96, places=4)
        self.assertIsNotNone(q_6996["highest_window"])
        self.assertAlmostEqual(q_6996["highest_window"]["raw_used_percent"], 69.96, places=4)
        self.assertLess(q_6996["highest_window"]["raw_used_percent"], 70.0)

        # Case C: Statusline 70.00% -> raw_used_percent=70.0 (>= 70.0!)
        qc.capture_claude_statusline(
            self.home,
            {"rate_limits": {"five_hour": {"used_percentage": 70.0, "resets_at": int(t_now + 7200)}}},
            t_now - 10,
        )
        q_70 = qc.collect_quotas(home=self.home, force=False, now_ts=t_now, allow_live=False)
        self.assertAlmostEqual(q_70["highest_window"]["raw_used_percent"], 70.0, places=4)
        self.assertGreaterEqual(q_70["highest_window"]["raw_used_percent"], 70.0)

    # ------------------------------------------------------------------
    # Round-2 #7: 26-week heatmap work_status=unknown aligns with today & report
    # ------------------------------------------------------------------
    def test_r2_07_heatmap_work_status_unknown_aligns_with_today_and_report(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        (cx_dir / "bad.jsonl").write_text("{corrupt_json\n", encoding="utf-8")

        p_unk = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        self.assertEqual(p_unk["today"]["work_status"], "unknown")
        self.assertEqual(p_unk["usage"]["periods"]["today"]["work_status"], "unknown")
        hm_today = p_unk["usage"]["heatmap"]["days"][-1]
        self.assertEqual(hm_today["date"], TODAY_STR)
        self.assertEqual(hm_today["work_status"], "unknown")
        self.assertEqual(p_unk["usage"]["history_30d"][-1]["work_status"], "unknown")
        md_day = c.render_markdown_report(p_unk)
        self.assertIn("**—**", md_day)
        self.assertNotIn("**0.00 h**", md_day)

        # Clean empty scan -> heatmap work_status becomes observed
        (cx_dir / "bad.jsonl").unlink()
        p_obs = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 15)
        self.assertEqual(p_obs["today"]["work_status"], "observed")
        self.assertEqual(p_obs["usage"]["heatmap"]["days"][-1]["work_status"], "observed")

    # ------------------------------------------------------------------
    # Round-2 #8: Sub-cent ($0.00175) & zero-priced mixed cost formatting
    # ------------------------------------------------------------------
    def test_r2_08_subcent_and_zero_priced_mixed_cost_formatting_all_surfaces(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        t0 = NOW_DT - timedelta(minutes=20)
        p_subcent = self.home / "proj-subcent"
        p_mixzero = self.home / "proj-mixzero"
        p_pure_unp = self.home / "proj-pure-unp"
        p_zero_only = self.home / "proj-zero-only"
        for d in (p_subcent, p_mixzero, p_pure_unp, p_zero_only):
            d.mkdir()

        # 1) Sub-cent mixed: 1,000 priced ($0.00175) + 1,000 unpriced -> "$0.00+"
        (cx_dir / "subcent.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(t0), "type": "session_meta", "payload": {"id": "s-sc", "originator": "codex_cli_rs", "cwd": str(p_subcent)}}),
            json.dumps({"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "priced-175"}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(seconds=5)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "output_tokens": 0}}}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(seconds=10)), "type": "turn_context", "payload": {"model": "mystery-unpriced"}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(seconds=15)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "output_tokens": 0}}}}),
        ]) + "\n", encoding="utf-8")

        # 2) Mixed zero-priced (on yesterday): 1,000 free ($0) + 1,000 unpriced -> "$0.00+"
        y_dt = NOW_DT - timedelta(days=1)
        y_day = y_dt.strftime("%Y-%m-%d")
        (cx_dir / "mixzero.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(y_dt), "type": "session_meta", "payload": {"id": "s-mz", "originator": "codex_cli_rs", "cwd": str(p_mixzero)}}),
            json.dumps({"timestamp": _iso(y_dt), "type": "turn_context", "payload": {"model": "free-zero"}}),
            json.dumps({"timestamp": _iso(y_dt + timedelta(seconds=5)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "output_tokens": 0}}}}),
            json.dumps({"timestamp": _iso(y_dt + timedelta(seconds=10)), "type": "turn_context", "payload": {"model": "mystery-unpriced"}}),
            json.dumps({"timestamp": _iso(y_dt + timedelta(seconds=15)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "output_tokens": 0}}}}),
        ]) + "\n", encoding="utf-8")

        # 3) Pure unpriced (2 days ago) & Confirmed zero (3 days ago)
        d2 = NOW_DT - timedelta(days=2)
        d3 = NOW_DT - timedelta(days=3)
        (cx_dir / "pure-unp.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(d2), "type": "session_meta", "payload": {"id": "s-pu", "originator": "codex_cli_rs", "cwd": str(p_pure_unp)}}),
            json.dumps({"timestamp": _iso(d2), "type": "turn_context", "payload": {"model": "mystery-unpriced"}}),
            json.dumps({"timestamp": _iso(d2 + timedelta(seconds=5)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "output_tokens": 0}}}}),
        ]) + "\n", encoding="utf-8")
        (cx_dir / "zero-only.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(d3), "type": "session_meta", "payload": {"id": "s-zo", "originator": "codex_cli_rs", "cwd": str(p_zero_only)}}),
            json.dumps({"timestamp": _iso(d3), "type": "turn_context", "payload": {"model": "free-zero"}}),
            json.dumps({"timestamp": _iso(d3 + timedelta(seconds=5)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000, "output_tokens": 0}}}}),
        ]) + "\n", encoding="utf-8")

        tbl = pr.PricingTable(
            {
                "priced-175": {"in": 1.75, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0},
                "free-zero": {"in": 0.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0},
            },
            {},
        )
        with patch.object(pr.PricingTable, "load", return_value=tbl):
            payload = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)

        # Verify sub-cent precision preserved (0.00175) across today, period, heatmap, projects, trails!
        self.assertAlmostEqual(payload["today"]["cost_usd"], 0.00175, places=6)
        self.assertAlmostEqual(payload["usage"]["periods"]["today"]["cost_usd"], 0.00175, places=6)
        hm_map = {d["date"]: d for d in payload["usage"]["heatmap"]["days"]}
        self.assertAlmostEqual(hm_map[TODAY_STR]["cost_usd"], 0.00175, places=6)
        projs = {p["path"]: p for p in payload["usage"]["projects"]}
        self.assertAlmostEqual(projs[str(p_subcent)]["cost_usd"], 0.00175, places=6)
        trails = {t["path"]: t["periods"]["all"] for t in payload["usage"]["project_trails"]}
        self.assertAlmostEqual(trails[str(p_subcent)]["cost_usd"], 0.00175, places=6)

        # Verify formatting across all 5 states
        self.assertEqual(c.format_estimated_usd(0.00175, 1000, has_observation=True, total_tokens=2000), "$0.00+")
        self.assertEqual(c.format_estimated_usd(0.0, 1000, has_observation=True, total_tokens=2000), "$0.00+")
        self.assertEqual(c.format_estimated_usd(0.0, 1000, has_observation=True, total_tokens=1000), "未计价")
        self.assertEqual(c.format_estimated_usd(0.0, 0, has_observation=True, total_tokens=1000), "$0.00")
        self.assertEqual(c.format_estimated_usd(0.0, 0, has_observation=False, total_tokens=0), "—")

        # Verify CLI Markdown reports for today (sub-cent mixed) & yesterday (zero-priced mixed) & d2 (pure unpriced)
        md_today = c.render_markdown_report(payload)
        md_today_p = c.render_markdown_report(payload, period="today")
        md_yest = c.render_markdown_report(payload, target_date=y_day)
        md_d2 = c.render_markdown_report(payload, target_date=d2.strftime("%Y-%m-%d"))
        self.assertIn("**$0.00+**", md_today)
        self.assertIn("**$0.00+**", md_today_p)
        self.assertIn("**$0.00+**", md_yest)
        self.assertIn("**未计价**", md_d2)


if __name__ == "__main__":
    unittest.main()
