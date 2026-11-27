#!/usr/bin/env python3
"""Comprehensive v0.7 contract & acceptance tests (isolated synthetic fixtures only)."""

from __future__ import annotations

import copy
import json
import os
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
from worktree import WorktreeResolver


NOW_DT = datetime(2026, 11, 27, 15, 0, 0, tzinfo=c.BIZ_TZ)
NOW_TS = NOW_DT.timestamp()
TODAY_STR = "2026-11-27"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")


def _strip_perf(payload: dict) -> dict:
    out = copy.deepcopy(payload)
    out.pop("elapsed_ms", None)
    return out


class TestV07Contracts(unittest.TestCase):
    def setUp(self):
        td = tempfile.TemporaryDirectory()
        self.addCleanup(td.cleanup)
        self.home = Path(td.name)

    # ------------------------------------------------------------------
    # 1. Cache v5 & Deduplication Parity (Cold == Warm == --no-cache)
    # ------------------------------------------------------------------
    def test_cache_v5_cold_warm_nocache_parity_and_v4_rebuild(self):
        # Create two Codex rollout files (one duplicate rollout + one T3 mirror) older than 105s
        t0 = NOW_DT - timedelta(minutes=25)
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        proj = self.home / "proj-a"
        proj.mkdir()

        # Two turns with multi-segments (10-min silent gap inside turn 1 -> 2 segments, still 1 run head)
        events = [
            {"timestamp": _iso(t0), "type": "session_meta",
             "payload": {"id": "s1", "originator": "codex_cli_rs", "cwd": str(proj)}},
            {"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex", "cwd": str(proj)}},
            {"timestamp": _iso(t0), "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "t1", "started_at": _iso(t0)}},
            {"timestamp": _iso(t0 + timedelta(seconds=60.1234)), "type": "event_msg",
             "payload": {"type": "item_completed", "turn_id": "t1",
                         "started_at_ms": int(t0.timestamp() * 1000),
                         "completed_at_ms": int((t0 + timedelta(seconds=60.1234)).timestamp() * 1000)}},
            # 10 min gap (dropped), then 40s work
            {"timestamp": _iso(t0 + timedelta(minutes=11)), "type": "event_msg",
             "payload": {"type": "item_completed", "turn_id": "t1",
                         "started_at_ms": int((t0 + timedelta(minutes=11)).timestamp() * 1000),
                         "completed_at_ms": int((t0 + timedelta(minutes=11, seconds=40)).timestamp() * 1000)}},
            {"timestamp": _iso(t0 + timedelta(minutes=11, seconds=40)), "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": "t1",
                         "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(minutes=11, seconds=40))}},
            {"timestamp": _iso(t0 + timedelta(minutes=12)), "type": "event_msg",
             "payload": {"type": "token_count", "info": {"total_token_usage": {
                 "input_tokens": 10000, "cached_input_tokens": 2000, "output_tokens": 500, "reasoning_output_tokens": 0
             }}}},
            # Turn 2: 600s completed 110s ago (inside cache window 110 > 105 AND inside just_finished < 120!)
            {"timestamp": _iso(NOW_DT - timedelta(seconds=710)), "type": "event_msg",
             "payload": {"type": "task_started", "turn_id": "t2", "started_at": _iso(NOW_DT - timedelta(seconds=710))}},
            {"timestamp": _iso(NOW_DT - timedelta(seconds=510)), "type": "event_msg",
             "payload": {"type": "item_completed", "turn_id": "t2",
                         "started_at_ms": int((NOW_DT - timedelta(seconds=710)).timestamp() * 1000),
                         "completed_at_ms": int((NOW_DT - timedelta(seconds=510)).timestamp() * 1000)}},
            {"timestamp": _iso(NOW_DT - timedelta(seconds=310)), "type": "event_msg",
             "payload": {"type": "item_completed", "turn_id": "t2",
                         "started_at_ms": int((NOW_DT - timedelta(seconds=510)).timestamp() * 1000),
                         "completed_at_ms": int((NOW_DT - timedelta(seconds=310)).timestamp() * 1000)}},
            {"timestamp": _iso(NOW_DT - timedelta(seconds=110)), "type": "event_msg",
             "payload": {"type": "task_complete", "turn_id": "t2",
                         "started_at": _iso(NOW_DT - timedelta(seconds=710)),
                         "completed_at": _iso(NOW_DT - timedelta(seconds=110))}},
        ]
        f1 = cx_dir / "rollout-a.jsonl"
        f2 = cx_dir / "rollout-dup.jsonl"
        f_t3 = cx_dir / "rollout-t3.jsonl"
        content = "\n".join(json.dumps(e) for e in events) + "\n"
        f1.write_text(content, encoding="utf-8")
        f2.write_text(content, encoding="utf-8")
        f_t3.write_text(
            json.dumps({"timestamp": _iso(t0), "type": "session_meta", "payload": {"originator": "T3 Code"}}) + "\n"
            + json.dumps({"timestamp": _iso(t0 + timedelta(seconds=90)), "type": "event_msg",
                          "payload": {"type": "task_complete", "turn_id": "t3only", "duration_ms": 90000}}) + "\n",
            encoding="utf-8",
        )
        old_mtime = NOW_TS - 110.0  # > LIVE_STALE_SECONDS + 15.0 (105s) so eligible for cache!
        for fp in (f1, f2, f_t3):
            os.utime(fp, (old_mtime, old_mtime))

        # Seed a legacy v4 cache to confirm auto-rebuild to v5
        ap_dir = self.home / ".agent-pulse"
        ap_dir.mkdir(mode=0o700)
        (ap_dir / "cache.json").write_text(json.dumps({"v": 4, "files": {"stale": {}}}), encoding="utf-8")
        (ap_dir / "usage_cache.json").write_text(json.dumps({"version": 4, "files": {}}), encoding="utf-8")

        cold = _strip_perf(c.collect_all(home=self.home, days=7, use_cache=True, now_ts=NOW_TS))
        cache_json = json.loads((ap_dir / "cache.json").read_text(encoding="utf-8"))
        self.assertEqual(cache_json["v"], 5)
        self.assertEqual(json.loads((ap_dir / "usage_cache.json").read_text(encoding="utf-8"))["version"], 5)

        warm = _strip_perf(c.collect_all(home=self.home, days=7, use_cache=True, now_ts=NOW_TS))
        nocache = _strip_perf(c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS))

        self.assertEqual(cold, warm)
        self.assertEqual(cold, nocache)
        # Turn 1 (2 segments = 100s, 1 run head) + Turn 2 (600s, 1 run head) = 700s, 2 runs (deduped across f1 & f2)
        self.assertAlmostEqual(cold["today"]["agent_seconds"], 700.0, delta=1.0)
        self.assertEqual(cold["today"]["runs"], 2)
        # Turn 2 is 600s completed 110s ago -> just_finished present on cold, warm, and nocache!
        jf = [t for t in cold["sentinel_tasks"] if t["state"] == "just_finished"]
        self.assertEqual(len(jf), 1)
        self.assertEqual(jf[0]["evidence"]["kind"], "explicit_success")

    # ------------------------------------------------------------------
    # 2. Per-Source Gate, Retained Fallback, Recovery & Commit-TS Concurrency
    # ------------------------------------------------------------------
    def test_source_health_gate_preserves_buckets_and_commit_ts_rejects_older(self):
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        wb_dir = self.home / ".workbuddy" / "projects" / "demo"
        wb_dir.mkdir(parents=True)
        t0 = NOW_DT - timedelta(minutes=20)

        cx_file = cx_dir / "c1.jsonl"
        cx_file.write_text("\n".join([
            json.dumps({"timestamp": _iso(t0), "type": "session_meta", "payload": {"originator": "codex_cli_rs", "cwd": str(self.home / "p1")}}),
            json.dumps({"timestamp": _iso(t0), "type": "turn_context", "payload": {"model": "gpt-5.3-codex"}}),
            json.dumps({"timestamp": _iso(t0), "type": "event_msg", "payload": {"type": "task_started", "turn_id": "t1", "started_at": _iso(t0)}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(minutes=5)), "type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", "started_at": _iso(t0), "completed_at": _iso(t0 + timedelta(minutes=5))}}),
            json.dumps({"timestamp": _iso(t0 + timedelta(minutes=5)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 50000, "output_tokens": 2000}}}}),
        ]) + "\n", encoding="utf-8")

        wb_file = wb_dir / "w1.jsonl"
        wb_file.write_text("\n".join([
            json.dumps({"timestamp": _iso(t0), "type": "message", "role": "user", "cwd": str(self.home / "p2")}),
            json.dumps({"id": "m1", "timestamp": _iso(t0 + timedelta(minutes=2)), "type": "message", "role": "assistant", "cwd": str(self.home / "p2"),
                        "providerData": {"modelName": "DeepSeek-V4.1-Flash"},
                        "message": {"usage": {"input_tokens": 10000, "output_tokens": 500}}}),
        ]) + "\n", encoding="utf-8")

        # First complete run establishes high bucket
        r1 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        self.assertEqual(r1["collection"]["work"]["status"], "complete")
        self.assertEqual(r1["collection"]["usage"]["status"], "complete")
        self.assertEqual(r1["collection"]["sources"]["work.pi"]["status"], "not_configured")
        self.assertEqual(r1["today"]["work_status"], "observed")
        self.assertTrue(r1["today"]["hourly_available"])
        high_work_sec = r1["today"]["agent_seconds"]
        self.assertAlmostEqual(high_work_sec, 420.0, delta=1.0)  # 300s codex + 120s workbuddy

        # Second run: corrupt Codex with truncated tail line, while WorkBuddy adds more tokens!
        cx_file.write_text(cx_file.read_text(encoding="utf-8") + '{"truncated": ', encoding="utf-8")
        wb_file.write_text(wb_file.read_text(encoding="utf-8") + json.dumps({
            "id": "m2", "timestamp": _iso(t0 + timedelta(minutes=3)), "type": "message", "role": "assistant",
            "cwd": str(self.home / "p2"), "providerData": {"modelName": "DeepSeek-V4.1-Flash"},
            "message": {"usage": {"input_tokens": 20000, "output_tokens": 1000}},
        }) + "\n", encoding="utf-8")

        r2 = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 15)
        self.assertEqual(r2["collection"]["sources"]["work.codex"]["status"], "unavailable")
        self.assertEqual(r2["collection"]["work"]["status"], "partial")
        self.assertEqual(r2["collection"]["sources"]["usage.workbuddy"]["status"], "complete")
        # Retained work bucket preserved exactly!
        self.assertEqual(r2["today"]["work_status"], "retained")
        self.assertTrue(r2["today"]["hourly_available"])
        self.assertAlmostEqual(r2["today"]["agent_seconds"], high_work_sec, delta=0.1)
        # Healthy WorkBuddy usage updated, while Codex usage retained!
        led2 = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        self.assertEqual(led2["live_tools"]["codex"][TODAY_STR]["in"], 50000)
        self.assertEqual(led2["live_tools"]["workbuddy"][TODAY_STR]["in"], 30000)

        # Older commit_ts scan MUST be rejected and return accepted ledger projection!
        r_stale = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 20, commit_ts=NOW_TS - 10)
        led_after_stale = lg.load_ledger(self.home / ".agent-pulse" / "ledger.json")
        self.assertEqual(led_after_stale["collection"]["commit_ts"], NOW_TS + 15)
        self.assertEqual(r_stale["usage"]["periods"]["today"]["total_tokens"], r2["usage"]["periods"]["today"]["total_tokens"])

        # Previously-seen source disappears -> unavailable (NOT not_configured!)
        cx_file.unlink()
        (self.home / ".codex" / "sessions" / "2026" / "11" / "27").rmdir()
        (self.home / ".codex" / "sessions" / "2026" / "11").rmdir()
        (self.home / ".codex" / "sessions" / "2026").rmdir()
        (self.home / ".codex" / "sessions").rmdir()
        r_missing = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 30)
        self.assertEqual(r_missing["collection"]["sources"]["work.codex"]["status"], "unavailable")

        # Recovery: restore empty codex sessions dir -> complete again!
        (self.home / ".codex" / "sessions").mkdir(parents=True)
        r_rec = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS + 45)
        self.assertEqual(r_rec["collection"]["sources"]["work.codex"]["status"], "complete")
        self.assertEqual(r_rec["collection"]["work"]["status"], "complete")

    # ------------------------------------------------------------------
    # 3. Antigravity CLI & Desktop Cross-Root Dedup + Explicit RUNNING
    # ------------------------------------------------------------------
    def test_antigravity_cli_dedup_explicit_running_and_fallback(self):
        desk_brain = self.home / ".gemini" / "antigravity" / "brain"
        cli_brain = self.home / ".gemini" / "antigravity-cli" / "brain"
        t0 = NOW_DT - timedelta(minutes=15)

        # Shared full conv ID: CLI has newer latest_event_ts -> CLI wins!
        shared_id = "11111111-2222-3333-4444-555555555555"
        d_shared = desk_brain / shared_id / ".system_generated" / "logs" / "transcript.jsonl"
        c_shared = cli_brain / shared_id / ".system_generated" / "logs" / "transcript.jsonl"
        d_shared.parent.mkdir(parents=True)
        c_shared.parent.mkdir(parents=True)
        d_shared.write_text("\n".join([
            json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": _iso(t0)}),
            json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _iso(t0 + timedelta(seconds=60))}),
        ]) + "\n", encoding="utf-8")
        c_shared.write_text("\n".join([
            json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": _iso(t0)}),
            json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _iso(t0 + timedelta(seconds=120))}),
        ]) + "\n", encoding="utf-8")
        os.utime(d_shared, (NOW_TS - 200, NOW_TS - 200))
        os.utime(c_shared, (NOW_TS - 200, NOW_TS - 200))

        # Independent Desktop session (fallback transcript_full.jsonl)
        d_only = desk_brain / "desk-only-conv-full-id" / ".system_generated" / "logs" / "transcript_full.jsonl"
        d_only.parent.mkdir(parents=True)
        d_only.write_text("\n".join([
            json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": _iso(t0)}),
            json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _iso(t0 + timedelta(seconds=90))}),
        ]) + "\n", encoding="utf-8")
        os.utime(d_only, (NOW_TS - 200, NOW_TS - 200))

        # Independent CLI session currently RUNNING (fresh mtime, explicit MODEL RUNNING)
        c_live = cli_brain / "cli-live-conv-full-id" / ".system_generated" / "logs" / "transcript.jsonl"
        c_live.parent.mkdir(parents=True)
        c_live.write_text("\n".join([
            json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": _iso(NOW_DT - timedelta(seconds=50))}),
            json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "RUNNING", "created_at": _iso(NOW_DT - timedelta(seconds=10)),
                        "model": "gemini-4", "input_tokens": 99999, "output_tokens": 9999}),
            json.dumps({"source": "SYSTEM", "type": "SYSTEM_HEARTBEAT", "status": "DONE", "created_at": _iso(NOW_DT - timedelta(seconds=5))}),
        ]) + "\n", encoding="utf-8")
        os.utime(c_live, (NOW_TS - 5, NOW_TS - 5))

        # CLI session where DONE overrides earlier RUNNING -> NOT live
        c_done = cli_brain / "cli-done-conv-full-id" / ".system_generated" / "logs" / "transcript.jsonl"
        c_done.parent.mkdir(parents=True)
        c_done.write_text("\n".join([
            json.dumps({"source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": _iso(NOW_DT - timedelta(seconds=60))}),
            json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "RUNNING", "created_at": _iso(NOW_DT - timedelta(seconds=30))}),
            json.dumps({"source": "MODEL", "type": "PLANNER_RESPONSE", "status": "DONE", "created_at": _iso(NOW_DT - timedelta(seconds=10))}),
        ]) + "\n", encoding="utf-8")
        os.utime(c_done, (NOW_TS - 10, NOW_TS - 10))

        cold = _strip_perf(c.collect_all(home=self.home, days=7, use_cache=True, now_ts=NOW_TS))
        warm = _strip_perf(c.collect_all(home=self.home, days=7, use_cache=True, now_ts=NOW_TS))
        self.assertEqual(cold, warm)

        # Total antigravity seconds: shared CLI (120) + desk_only (90) + c_live (50) + c_done (50) = 310s (NOT 370s!)
        tools = {t["tool"]: t["agent_seconds"] for t in cold["today"]["tools"]}
        self.assertAlmostEqual(tools["antigravity"], 310.0, delta=2.0)
        # Zero tokens/cost from Antigravity CLI!
        self.assertEqual(cold["today"]["tokens"], 0)
        self.assertEqual(cold["today"]["cost_usd"], 0.0)
        # Exactly 1 running sentinel task with explicit_running evidence!
        self.assertEqual(len(cold["sentinel_tasks"]), 1)
        st = cold["sentinel_tasks"][0]
        self.assertEqual(st["source_id"], "work.antigravity_cli")
        self.assertEqual(st["state"], "running")
        self.assertEqual(st["evidence"]["kind"], "explicit_running")
        self.assertEqual(cold["active_agents"][0]["task_id"], st["id"])

    # ------------------------------------------------------------------
    # 4. Sentinel 3-State Precision (T3 Zero-Work Wait, Multi-Run, 599.999/600, 119.999/120)
    # ------------------------------------------------------------------
    def test_t3_sentinel_three_states_and_boundaries(self):
        t3_db = self.home / ".t3" / "userdata" / "statev2.sqlite"
        t3_db.parent.mkdir(parents=True)
        proj = self.home / "repo-main"
        wt = self.home / "repo-wt"
        proj.mkdir()
        wt.mkdir()

        with sqlite3.connect(t3_db) as conn:
            conn.execute("CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, workspace_root TEXT)")
            conn.execute("CREATE TABLE projection_threads (thread_id TEXT PRIMARY KEY, project_id TEXT, worktree_path TEXT)")
            conn.execute("CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY, project_id TEXT, title TEXT, payload_json TEXT)")
            conn.execute("CREATE TABLE orchestration_v2_projection_nodes (node_id TEXT PRIMARY KEY, run_id TEXT, runtime_request_id TEXT, provider_turn_id TEXT)")
            conn.execute("CREATE TABLE orchestration_v2_projection_runs (run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INT, provider TEXT, provider_thread_id TEXT, status TEXT, requested_at TEXT, completed_at TEXT, payload_json TEXT)")
            conn.execute("CREATE TABLE orchestration_v2_projection_turn_items (turn_item_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, ordinal INT, type TEXT, status TEXT, updated_at TEXT, payload_json TEXT)")
            conn.execute("CREATE TABLE orchestration_v2_projection_runtime_requests (runtime_request_id TEXT PRIMARY KEY, thread_id TEXT, node_id TEXT, provider_turn_id TEXT, kind TEXT, status TEXT, created_at TEXT, resolved_at TEXT)")

            conn.execute("INSERT INTO projection_projects VALUES ('p1', ?)", (str(proj),))
            conn.execute("INSERT INTO projection_threads VALUES ('th_wait', 'p1', ?)", (str(wt),))
            conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES ('th_wait', 'p1', '零工时审批', '{}')")
            conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES ('th_multi', 'p1', '同线程双Run', '{}')")
            conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES ('th_fin', 'p1', '刚收工边界', '{}')")

            # A) Zero-work-seconds run waiting > 10 minutes via approval_request item -> MUST show waiting_human!
            w_start = NOW_DT - timedelta(minutes=12)
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                         ("r_zero_wait", "th_wait", 1, "codex", "pt_w", "running", _iso(w_start), None, "{}"))
            conn.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES (?,?,?,?,?,?,?,?)",
                         ("it_w1", "th_wait", "r_zero_wait", 1, "approval_request", "pending", _iso(w_start + timedelta(seconds=5)),
                          json.dumps({"startedAt": _iso(w_start + timedelta(seconds=5))})))

            # B) Same thread with 2 unfinished runs: req linked via node_id attaches to r_m1 ONLY; unlinked req ignored!
            m_start = NOW_DT - timedelta(minutes=2)
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                         ("r_m1", "th_multi", 1, "claude", "pt_m1", "running", _iso(m_start), None, "{}"))
            conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                         ("r_m2", "th_multi", 2, "codex", "pt_m2", "running", _iso(m_start), None, "{}"))
            conn.execute("INSERT INTO orchestration_v2_projection_nodes VALUES ('n1', 'r_m1', 'req_linked', NULL)")
            conn.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('req_linked', 'th_multi', 'n1', NULL, 'secret', 'pending', ?, NULL)",
                         (_iso(m_start + timedelta(seconds=30)),))
            conn.execute("INSERT INTO orchestration_v2_projection_runtime_requests VALUES ('req_ambig', 'th_multi', NULL, NULL, 'approval', 'pending', ?, NULL)",
                         (_iso(m_start + timedelta(seconds=40)),))

            # C) just_finished exact boundaries:
            # r_599: 599.9s (NOT >= 600) -> excluded
            # r_600: 600.0s finished 119.5s ago -> INCLUDED
            # r_120: 600.0s finished 120.0s ago -> excluded
            # r_fail: 700s failed 30s ago -> excluded
            def add_done_run(rid, dur_s, ago_s, status="completed"):
                end_t = NOW_TS - ago_s
                start_t = end_t - dur_s
                conn.execute("INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                             (rid, "th_fin", 1, "antigravity", rid, status, str(start_t), str(end_t), "{}"))
                # Add step heartbeats every 200s so full dur_s is counted
                cur = start_t
                idx = 0
                while cur < end_t:
                    nxt = min(end_t, cur + 200.0)
                    idx += 1
                    conn.execute("INSERT INTO orchestration_v2_projection_turn_items VALUES (?,?,?,?,?,?,?,?)",
                                 (f"{rid}-i{idx}", "th_fin", rid, idx, "reasoning", "completed", str(nxt),
                                  json.dumps({"startedAt": cur, "completedAt": nxt})))
                    cur = nxt

            add_done_run("r_599", 599.5, 30.0, "completed")
            add_done_run("r_600", 600.0, 119.5, "completed")
            add_done_run("r_120", 600.0, 120.0, "completed")
            add_done_run("r_fail", 700.0, 30.0, "failed")
            conn.commit()

        payload = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        tasks = payload["sentinel_tasks"]
        by_state = [(t["state"], t["tool"], t["evidence"]) for t in tasks]

        # Priority order: waiting_human first (r_m1 secret, r_zero_wait approval), then just_finished (r_600)
        # Note: r_m2 had thread_waits legacy exclusion (has_pending_human_wait) so r_m2 is not running,
        # and r_m1 gets ONLY the linked secret wait!
        states = [t["state"] for t in tasks]
        self.assertEqual(states, ["waiting_human", "waiting_human", "just_finished"])
        # Waiting tasks sorted by state_since_ts DESC: r_m1 (2m ago) before r_zero_wait (12m ago)
        self.assertEqual(tasks[0]["evidence"]["wait_kind"], "secret")
        self.assertEqual(tasks[1]["evidence"]["wait_kind"], "approval")
        self.assertEqual(tasks[1]["elapsed_seconds"], 0.0)
        self.assertEqual(tasks[1]["workspace_path"], str(wt))
        self.assertEqual(tasks[1]["project_path"], str(proj))
        # Only r_600 entered just_finished
        self.assertEqual(tasks[2]["state"], "just_finished")
        self.assertAlmostEqual(tasks[2]["elapsed_seconds"], 600.0, delta=0.5)
        self.assertEqual(payload["active_agents"], [])

    # ------------------------------------------------------------------
    # 5. Git Worktree Merging (3-Instance Fixture + Detached + Submodule + Deleted + 24h > 3600s)
    # ------------------------------------------------------------------
    def test_git_worktree_merging_project_trails_and_hourly_parallel(self):
        git_env = {
            "PATH": "/usr/bin:/bin:/opt/homebrew/bin:/usr/local/bin",
            "HOME": str(self.home),
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "t@local",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "t@local",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        main_repo = self.home / "ws" / "app"
        orca_wt = self.home / "orca-wt" / "feat-a"
        detached_wt = self.home / "orca-wt" / "detached-b"
        same_name_clone = self.home / "other" / "app"
        main_repo.mkdir(parents=True)
        same_name_clone.parent.mkdir(parents=True)
        orca_wt.parent.mkdir(parents=True)

        subprocess.run(["/usr/bin/git", "init", "-q", "-b", "main", str(main_repo)], env=git_env, check=True)
        (main_repo / "a.txt").write_text("hi\n", encoding="utf-8")
        subprocess.run(["/usr/bin/git", "-C", str(main_repo), "add", "a.txt"], env=git_env, check=True)
        subprocess.run(["/usr/bin/git", "-C", str(main_repo), "commit", "-q", "-m", "init"], env=git_env, check=True)
        subprocess.run(["/usr/bin/git", "-C", str(main_repo), "worktree", "add", "-q", "-b", "feat/alpha", str(orca_wt)], env=git_env, check=True)
        subprocess.run(["/usr/bin/git", "-C", str(main_repo), "worktree", "add", "-q", "--detach", str(detached_wt)], env=git_env, check=True)
        subprocess.run(["/usr/bin/git", "clone", "-q", str(main_repo), str(same_name_clone)], env=git_env, check=True)

        # Verify resolver directly
        resolver = WorktreeResolver()
        r_main = resolver.resolve(str(main_repo))
        r_wt = resolver.resolve(str(orca_wt))
        r_det = resolver.resolve(str(detached_wt))
        r_clone = resolver.resolve(str(same_name_clone))
        r_del = resolver.resolve(str(self.home / "deleted-wt"))

        self.assertEqual(r_wt.canonical_root, r_main.canonical_root)
        self.assertEqual(r_wt.branch, "feat/alpha")
        self.assertEqual(r_det.canonical_root, r_main.canonical_root)
        self.assertIsNone(r_det.branch)
        # Independent clone MUST NOT merge into main_repo despite identical name & remote!
        self.assertNotEqual(r_clone.canonical_root, r_main.canonical_root)
        # Deleted worktree stays at original path
        self.assertEqual(r_del.canonical_root, str(self.home / "deleted-wt"))
        self.assertFalse(r_del.verified_git)

        # Create parallel 40-min Codex sessions in main_repo and orca_wt during the SAME hour (14:00-14:40) -> hourly[14] = 4800s > 3600s!
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        h14 = datetime(2026, 11, 27, 14, 0, 0, tzinfo=c.BIZ_TZ)

        def write_40m_codex(fname, cwd, tid_prefix, in_tok, cr_tok):
            ev = [
                {"timestamp": _iso(h14), "type": "session_meta", "payload": {"originator": "codex_cli_rs", "cwd": str(cwd)}},
                {"timestamp": _iso(h14), "type": "turn_context", "payload": {"model": "gpt-5.3-codex", "cwd": str(cwd)}},
                {"timestamp": _iso(h14), "type": "event_msg", "payload": {"type": "task_started", "turn_id": tid_prefix, "started_at": _iso(h14)}},
            ]
            for m in range(4, 41, 4):
                ev.append({"timestamp": _iso(h14 + timedelta(minutes=m)), "type": "event_msg",
                           "payload": {"type": "item_completed", "turn_id": tid_prefix,
                                       "started_at_ms": int((h14 + timedelta(minutes=m - 4)).timestamp() * 1000),
                                       "completed_at_ms": int((h14 + timedelta(minutes=m)).timestamp() * 1000)}})
            ev.append({"timestamp": _iso(h14 + timedelta(minutes=40)), "type": "event_msg",
                       "payload": {"type": "task_complete", "turn_id": tid_prefix, "started_at": _iso(h14), "completed_at": _iso(h14 + timedelta(minutes=40))}})
            ev.append({"timestamp": _iso(h14 + timedelta(minutes=40)), "type": "event_msg",
                       "payload": {"type": "token_count", "info": {"total_token_usage": {
                           "input_tokens": in_tok, "cached_input_tokens": cr_tok, "output_tokens": 1000
                       }}}})
            (cx_dir / fname).write_text("\n".join(json.dumps(x) for x in ev) + "\n", encoding="utf-8")

        # Main: 100k in, 80k cached (80%); WT: 100k in, 20k cached (20%); combined = 100k cached / 200k = 50%
        write_40m_codex("m.jsonl", main_repo, "tm", 100000, 80000)
        write_40m_codex("w.jsonl", orca_wt, "tw", 100000, 20000)
        write_40m_codex("c.jsonl", same_name_clone, "tc", 10000, 0)

        payload = c.collect_all(home=self.home, days=7, use_cache=False, now_ts=NOW_TS)
        # Parallel 3x40m = 7200s in hour 14 (> 3600s!), wall clock = 2400s!
        self.assertGreater(payload["today"]["hourly_seconds"][14], 3600.0)
        self.assertAlmostEqual(payload["today"]["hourly_seconds"][14], 7200.0, delta=2.0)
        self.assertAlmostEqual(payload["today"]["wall_seconds"], 2400.0, delta=2.0)

        trails = {p["path"]: p for p in payload["usage"]["project_trails"]}
        self.assertIn(r_main.canonical_root, trails)
        self.assertIn(r_clone.canonical_root, trails)
        self.assertEqual(len(trails), 2)

        merged_today = trails[r_main.canonical_root]["periods"]["today"]
        self.assertAlmostEqual(merged_today["agent_seconds"], 4800.0, delta=2.0)
        self.assertAlmostEqual(merged_today["cache_hit_rate"], 0.5, places=3)
        branches = {s["branch"] for s in merged_today["worktree_sources"]}
        self.assertEqual(branches, {"main", "feat/alpha"})

    # ------------------------------------------------------------------
    # 6. Quota Freshness TTL & Pace Math (Spec Worked Example + Boundaries)
    # ------------------------------------------------------------------
    def test_quota_pace_worked_example_boundaries_and_unknown_reasons(self):
        # Spec worked example: 3 samples at -30m, -15m, 0m with 60, 65, 70%, resets in 60m -> r=20, p=90, watch
        t_now = 1795676400.0
        resets_at = int(t_now + 3600)
        auth_ctx = {"account_key": "acct-sha256-1", "access_token": "tok"}

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
                make_resp(70.0, resets_at),
            ]
            s1 = qc.collect_quotas(home=self.home, force=True, now_ts=t_now - 1800, allow_live=True)
            self.assertEqual(s1["providers"][0]["windows"][0]["pace"]["reason"], "insufficient_samples")
            self.assertEqual(s1["providers"][0]["windows"][0]["pace"]["sample_count"], 1)

            s2 = qc.collect_quotas(home=self.home, force=True, now_ts=t_now - 900, allow_live=True)
            self.assertEqual(s2["providers"][0]["windows"][0]["pace"]["reason"], "insufficient_samples")
            self.assertEqual(s2["providers"][0]["windows"][0]["pace"]["sample_count"], 2)

            s3 = qc.collect_quotas(home=self.home, force=True, now_ts=t_now, allow_live=True)
            w3 = s3["providers"][0]["windows"][0]
            self.assertEqual(w3["fresh_until_ts"], t_now + 360.0)
            pace = w3["pace"]
            self.assertEqual(pace["level"], "watch")
            self.assertEqual(pace["sample_count"], 3)
            self.assertAlmostEqual(pace["rate_pct_per_hour"], 20.0, places=2)
            self.assertAlmostEqual(pace["projected_used_percent"], 90.0, places=1)
            self.assertNotIn("estimated_exhaustion_ts", pace)

            # Same cache hit (force=False at t_now + 60 < 240s) MUST NOT add a 4th sample!
            s3_cached = qc.collect_quotas(home=self.home, force=False, now_ts=t_now + 60, allow_live=True)
            self.assertEqual(s3_cached["providers"][0]["windows"][0]["pace"]["sample_count"], 3)

        # Boundary tests: p == 85 -> watch, p == 100 -> risk (with estimated_exhaustion_ts == resets_at), slope 0 -> on_track
        series = []
        p_obj = {"id": "codex", "status": "ok", "fetched_at": int(t_now)}
        # Pre-seed 2 samples for exact p == 85: -30m=55, -15m=60, 0m=65 -> r=20, +1h -> p=85.0 (watch)
        series.append({
            "provider_id": "codex", "window_id": "codex_5h", "account_scope": "a1", "resets_at": resets_at,
            "samples": [{"observed_at": int(t_now - 1800), "used_percent": 55.0},
                        {"observed_at": int(t_now - 900), "used_percent": 60.0}],
        })
        w85 = {"id": "codex_5h", "used_percent": 65.0, "resets_at": resets_at, "fresh_until_ts": t_now + 360}
        res85 = qc._compute_pace_for_window(series, p_obj, w85, "a1", t_now)
        self.assertEqual(res85["level"], "watch")
        self.assertAlmostEqual(res85["projected_used_percent"], 85.0, places=1)

        # Exact p == 100: -30m=70, -15m=75, 0m=80 -> r=20, +1h -> p=100.0 (risk, exhaustion_ts == resets_at)
        series2 = [{
            "provider_id": "codex", "window_id": "codex_5h", "account_scope": "a1", "resets_at": resets_at,
            "samples": [{"observed_at": int(t_now - 1800), "used_percent": 70.0},
                        {"observed_at": int(t_now - 900), "used_percent": 75.0}],
        }]
        w100 = {"id": "codex_5h", "used_percent": 80.0, "resets_at": resets_at, "fresh_until_ts": t_now + 360}
        res100 = qc._compute_pace_for_window(series2, p_obj, w100, "a1", t_now)
        self.assertEqual(res100["level"], "risk")
        self.assertAlmostEqual(res100["estimated_exhaustion_ts"], resets_at, places=1)

        # Claude fresh reading without verified account scope -> unverified_scope
        res_unv = qc._compute_pace_for_window([], {"id": "claude", "status": "ok", "fetched_at": int(t_now)}, w85, None, t_now)
        self.assertEqual(res_unv["reason"], "unverified_scope")
        self.assertEqual(res_unv["sample_count"], 0)

        # Missing reset -> missing_reset
        w_no_reset = {"id": "codex_5h", "used_percent": 65.0, "resets_at": None, "fresh_until_ts": t_now + 360}
        self.assertEqual(qc._compute_pace_for_window([], p_obj, w_no_reset, "a1", t_now)["reason"], "missing_reset")

    # ------------------------------------------------------------------
    # 7. Subscription Billing Candidate, Hermes API Actual & Tokei Exclusion
    # ------------------------------------------------------------------
    def test_billing_subscription_candidate_hermes_actual_and_tokei_exclusion(self):
        # Codex priced ($40) + unpriced model (excluded), plus Hermes actual ($3.25) & subscription_included
        cx_dir = self.home / ".codex" / "sessions" / "2026" / "11" / "27"
        cx_dir.mkdir(parents=True)
        # 20M input tokens on a $2/1M model => exact $40.00!
        (cx_dir / "c.jsonl").write_text("\n".join([
            json.dumps({"timestamp": _iso(NOW_DT - timedelta(minutes=10)), "type": "session_meta", "payload": {"originator": "codex_cli_rs"}}),
            json.dumps({"timestamp": _iso(NOW_DT - timedelta(minutes=10)), "type": "turn_context", "payload": {"model": "test-2-per-m"}}),
            json.dumps({"timestamp": _iso(NOW_DT - timedelta(minutes=9)), "type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 20_000_000, "output_tokens": 0}}}}),
            json.dumps({"timestamp": _iso(NOW_DT - timedelta(minutes=8)), "type": "turn_context", "payload": {"model": "unpriced-mystery"}}),
            json.dumps({"timestamp": _iso(NOW_DT - timedelta(minutes=7)), "type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1_000_000, "output_tokens": 0}}}}),
        ]) + "\n", encoding="utf-8")

        # Hermes DB with verified actual ($3.25), explicit zero ($0.00), non-actual ignored, and subscription_included
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
            conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, actual_cost_usd REAL)")
            conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, timestamp REAL)")
            conn.execute("INSERT INTO sessions VALUES ('s1', 999.0)")  # MUST NOT be added!
            # Verified API actual: $3.25
            conn.execute("""INSERT INTO session_model_usage VALUES (
                's1', 'test-2-per-m', 'openai', 'https://api', 'api', 'chat',
                1000, 0, 0, 0, 0, 0.01, 3.25, 'actual', 'provider_cost_api', ?, ?
            )""", (NOW_TS - 300, NOW_TS - 120))
            # Unverified estimated: MUST NOT enter api_actual_usd
            conn.execute("""INSERT INTO session_model_usage VALUES (
                's2', 'test-2-per-m', 'openai', 'https://api', 'api', 'chat',
                1000, 0, 0, 0, 0, 5.0, 5.0, 'estimated', 'provider_cost_api', ?, ?
            )""", (NOW_TS - 300, NOW_TS - 120))
            # Included route with actual_cost_usd: MUST NOT enter api_actual_usd; enters subscription_candidate_usd ($2.00)
            conn.execute("""INSERT INTO session_model_usage VALUES (
                's3', 'test-2-per-m', 'openai', 'https://api', 'subscription_included', 'chat',
                1000000, 0, 0, 0, 0, 0.0, 9.99, 'actual', 'provider_cost_api', ?, ?
            )""", (NOW_TS - 300, NOW_TS - 120))
            conn.commit()

        tbl = pr.PricingTable({"test-2-per-m": {"in": 2.0, "out": 0.0, "cache_read": 0.0, "cache_write": 0.0}}, {})
        live_billing: dict = {}
        live_usage = uc.collect_live_usage(
            home=self.home, days=14, use_cache=False, now_ts=NOW_TS, pricing=tbl,
            billing_out=live_billing, billing_cutoff_day="2026-11-01",
        )
        # Pre-seed Tokei winning on yesterday (2026-11-26) for codex -> candidate on 2026-11-26 MUST be excluded!
        ledger_path = self.home / ".agent-pulse" / "ledger.json"
        ledger_path.parent.mkdir(mode=0o700, exist_ok=True)
        ledger_path.write_text(json.dumps({
            "version": 1,
            "imported_tools": {"codex": {"2026-11-26": {"in": 99999999, "cost": 10.0}}},
            "live_tools": {"codex": {"2026-11-26": {"in": 100, "cost_usd": 1.0}}},
            "billing_days": {"codex": {"2026-11-26": {"subscription_candidate_usd": 1.0}}},
            "projects": {},
            "work_days": {},
        }), encoding="utf-8")

        usage_payload, _ = lg.build_usage_payload(
            ledger_path=ledger_path, live_usage=live_usage, live_cutoff_day="2026-11-14",
            now_ts=NOW_TS, pricing=tbl, live_billing=live_billing, billing_cutoff_day="2026-11-01",
        )
        month_tools = {t["tool"]: t for t in usage_payload["periods"]["month"]["tools"]}
        # Codex: ONLY today's $40.00 (2026-11-26 excluded because Tokei won that day!)
        self.assertAlmostEqual(month_tools["codex"]["billing"]["subscription_candidate_usd"], 40.0, places=4)
        # Hermes: $3.25 verified API actual + $2.00 subscription_included candidate
        self.assertAlmostEqual(month_tools["hermes"]["billing"]["api_actual_usd"], 3.25, places=4)
        self.assertAlmostEqual(month_tools["hermes"]["billing"]["subscription_candidate_usd"], 2.0, places=4)

    # ------------------------------------------------------------------
    # 8. CLI --markdown --period & --date Strict Validation (exit 2, zero ledger side effects)
    # ------------------------------------------------------------------
    def test_cli_markdown_periods_and_validation_zero_side_effects(self):
        env = dict(os.environ, HOME=str(self.home), AGENTPULSE_NOW_TS=str(NOW_TS))
        script = str(ROOT / "engine" / "collector.py")

        invalid_cases = [
            ["--period", "week"],                              # --period without --markdown
            ["--markdown", "--period", "week", "--date", TODAY_STR],  # conflict
            ["--markdown", "--period", "bogus"],               # unknown period
            ["--markdown", "--date", "2026-02-29"],            # non-leap year invalid date
            ["--markdown", "--date", "2026-11-28"],            # future date
            ["--markdown", "--days", "7", "--date", "2026-11-10"],   # out of 7-day window
            ["--markdown", "--date", "not-a-date"],            # bad format
        ]
        ledger_path = self.home / ".agent-pulse" / "ledger.json"
        for cli_args in invalid_cases:
            with self.subTest(args=cli_args):
                proc = subprocess.run(
                    ["/usr/bin/python3", "-I", "-B", script, *cli_args],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
                )
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stdout, "")
                err_lines = [ln for ln in proc.stderr.splitlines() if ln.strip()]
                self.assertEqual(len(err_lines), 1)
                self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in err_lines[0]))
                self.assertFalse(ledger_path.exists(), "invalid args must not create ledger")

        # Leap year valid date check (2024-02-29 with frozen clock on 2024-03-02)
        leap_now = datetime(2024, 3, 2, 12, 0, 0, tzinfo=c.BIZ_TZ).timestamp()
        proc_leap = subprocess.run(
            ["/usr/bin/python3", "-I", "-B", script, "--days", "7", "--markdown", "--date", "2024-02-29"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dict(os.environ, HOME=str(self.home), AGENTPULSE_NOW_TS=str(leap_now)),
            text=True,
        )
        self.assertEqual(proc_leap.returncode, 0, proc_leap.stderr)
        self.assertIn("2024-02-29", proc_leap.stdout)

        # All 7 --period keys succeed
        for pk in ("today", "yesterday", "week", "last_week", "month", "year", "all"):
            proc = subprocess.run(
                ["/usr/bin/python3", "-I", "-B", script, "--markdown", "--period", pk],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, text=True,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("AI 工时与用量战报", proc.stdout)


if __name__ == "__main__":
    unittest.main()
