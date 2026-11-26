#!/usr/bin/env python3
"""Unit tests for AgentPulse step-heartbeat collector engine (isolated fixtures only)."""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "engine"))
import collector as c


class TestAgentPulseCollector(unittest.TestCase):
    def test_midnight_split_asia_shanghai(self):
        s = datetime(2026, 11, 22, 23, 50, 0, tzinfo=c.BIZ_TZ).timestamp()
        e = datetime(2026, 11, 23, 0, 10, 0, tzinfo=c.BIZ_TZ).timestamp()
        seg = c.WorkSegment(tool="t3_antigravity", start_ts=s, end_ts=e, run_key="r1")
        chunks = c.split_segment_by_biz_day(seg)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0][0], "2026-11-22")
        self.assertAlmostEqual(chunks[0][1].duration, 600.0, places=2)
        self.assertTrue(chunks[0][1].is_turn_head)
        self.assertEqual(chunks[1][0], "2026-11-23")
        self.assertAlmostEqual(chunks[1][1].duration, 600.0, places=2)
        self.assertFalse(chunks[1][1].is_turn_head)

    def test_continuous_long_task_not_capped_at_30m(self):
        # 45 minutes of continuous autonomous work (heartbeat every 3 minutes) -> full 2700s counted!
        base = datetime(2026, 11, 23, 10, 0, 0, tzinfo=c.BIZ_TZ).timestamp()
        steps = [base + i * 180 for i in range(1, 16)]  # 15 * 3m = 45m
        segs = c.build_heartbeat_segments(tool="workbuddy", turn_start=base, step_timestamps=steps, run_key="long1")
        self.assertAlmostEqual(sum(s.duration for s in segs), 2700.0, places=1)
        self.assertEqual(sum(1 for s in segs if s.is_turn_head), 1)

    def test_silent_gap_over_5m_dropped_to_zero(self):
        # 39.3m wall span where 37.4m is silent pause -> only ~2m of actual work counted!
        base = datetime(2026, 11, 23, 9, 50, 0, tzinfo=c.BIZ_TZ).timestamp()
        # 60s work -> 2244s silent pause (dropped!) -> 60s work
        steps = [base + 30, base + 60, base + 2304, base + 2334, base + 2364]
        segs = c.build_heartbeat_segments(tool="t3_antigravity", turn_start=base, step_timestamps=steps, run_key="gap1")
        self.assertAlmostEqual(sum(s.duration for s in segs), 120.0, places=1)
        self.assertEqual(len(segs), 2)
        self.assertEqual(sum(1 for s in segs if s.is_turn_head), 1)

    def test_explicit_human_wait_subtraction_even_under_5m(self):
        # 8-min continuous run with a 2-min user_input_request in the middle -> 6m net AI work
        base = datetime(2026, 11, 23, 14, 0, 0, tzinfo=c.BIZ_TZ).timestamp()
        steps = [base + 60, base + 120, base + 240, base + 360, base + 480]
        exclusions = [(base + 120, base + 240)]  # 2-min explicit user wait
        segs = c.build_heartbeat_segments(
            tool="t3_antigravity",
            turn_start=base,
            step_timestamps=steps,
            exclusions=exclusions,
            run_key="sub1",
        )
        self.assertAlmostEqual(sum(s.duration for s in segs), 360.0, places=1)

    def test_parallel_wall_merge_and_leverage(self):
        base = datetime(2026, 11, 23, 14, 0, 0, tzinfo=c.BIZ_TZ).timestamp()
        segs = [
            c.WorkSegment(tool="t3_antigravity", start_ts=base, end_ts=base + 1800, run_key="a"),
            c.WorkSegment(tool="codex", start_ts=base, end_ts=base + 1800, run_key="b"),
        ]
        wall = c.merge_wall_seconds(segs)
        self.assertAlmostEqual(wall, 1800.0, places=1)
        agg = c.aggregate_segments(segs, days=7, now_ts=base + 2000)
        self.assertAlmostEqual(agg["today"]["agent_hours"], 1.0, places=2)
        self.assertAlmostEqual(agg["today"]["wall_hours"], 0.5, places=2)
        self.assertAlmostEqual(agg["today"]["leverage"], 2.0, places=2)

    def test_isolated_t3_step_heartbeat_human_wait_and_codex_dedup(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            now_dt = datetime(2026, 11, 23, 15, 0, 0, tzinfo=c.BIZ_TZ)
            now_ts = now_dt.timestamp()

            def iso(dt):
                return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

            t3_db = home / ".t3" / "userdata" / "statev2.sqlite"
            t3_db.parent.mkdir(parents=True)
            conn = sqlite3.connect(str(t3_db))
            conn.execute(
                """CREATE TABLE orchestration_v2_projection_runs (
                    run_id TEXT PRIMARY KEY, thread_id TEXT, ordinal INT, provider TEXT,
                    provider_thread_id TEXT, status TEXT, requested_at TEXT, completed_at TEXT,
                    payload_json TEXT
                )"""
            )
            conn.execute("CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY, title TEXT)")
            conn.execute(
                """CREATE TABLE orchestration_v2_projection_turn_items (
                    turn_item_id TEXT PRIMARY KEY, thread_id TEXT, run_id TEXT, ordinal INT,
                    type TEXT, status TEXT, updated_at TEXT, payload_json TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE orchestration_v2_projection_runtime_requests (
                    runtime_request_id TEXT PRIMARY KEY, thread_id TEXT, status TEXT,
                    created_at TEXT, resolved_at TEXT
                )"""
            )
            conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES ('th1', '步间心跳测试')")
            conn.execute("INSERT INTO orchestration_v2_projection_threads VALUES ('th2', '卡审批测试')")

            # Run 1: 40m total span with 36m silent gap -> only 4m real AI work (2m before + 2m after)
            r1_start = now_dt - timedelta(minutes=50)
            r1_end = now_dt - timedelta(minutes=10)
            conn.execute(
                "INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                ("r1", "th1", 1, "antigravity", "pt1", "completed", iso(r1_start), iso(r1_end), "{}"),
            )
            conn.execute(
                "INSERT INTO orchestration_v2_projection_turn_items VALUES (?,?,?,?,?,?,?,?)",
                ("i1", "th1", "r1", 1, "reasoning", "completed", iso(r1_start + timedelta(minutes=2)),
                 json.dumps({"startedAt": iso(r1_start), "completedAt": iso(r1_start + timedelta(minutes=2))})),
            )
            conn.execute(
                "INSERT INTO orchestration_v2_projection_turn_items VALUES (?,?,?,?,?,?,?,?)",
                ("i2", "th1", "r1", 2, "assistant_message", "completed", iso(r1_end - timedelta(minutes=2)),
                 json.dumps({"startedAt": iso(r1_end - timedelta(minutes=2)), "completedAt": iso(r1_end)})),
            )

            # Run 2: Live active T3 run (2m elapsed, recent heartbeat, no pending wait) -> active!
            r2_start = now_dt - timedelta(minutes=2)
            conn.execute(
                "INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                ("r2", "th1", 2, "antigravity", "pt2", "running", iso(r2_start), None, "{}"),
            )

            # Run 3: Running T3 run with pending runtime_request -> MUST NOT be marked active!
            r3_start = now_dt - timedelta(minutes=3)
            conn.execute(
                "INSERT INTO orchestration_v2_projection_runs VALUES (?,?,?,?,?,?,?,?,?)",
                ("r3", "th2", 1, "codex", "pt3", "running", iso(r3_start), None, "{}"),
            )
            conn.execute(
                "INSERT INTO orchestration_v2_projection_runtime_requests VALUES (?,?,?,?,?)",
                ("req1", "th2", "pending", iso(r3_start + timedelta(minutes=1)), None),
            )
            conn.commit()
            conn.close()

            # Codex JSONL: T3-originated skipped, Desktop 4m (2m + 2m with 10m gap dropped -> 2m + 0)
            cx_dir = home / ".codex" / "sessions" / "2026" / "11" / "23"
            cx_dir.mkdir(parents=True)
            (cx_dir / "rollout-t3.jsonl").write_text(
                "\n".join([
                    json.dumps({"timestamp": iso(now_dt - timedelta(minutes=25)), "type": "session_meta",
                                "payload": {"originator": "T3 Code", "cwd": "/tmp/demo"}}),
                    json.dumps({"timestamp": iso(now_dt - timedelta(minutes=20)), "type": "event_msg",
                                "payload": {"type": "task_complete", "turn_id": "t3dup", "duration_ms": 300000}}),
                ]),
                encoding="utf-8",
            )
            cx_t0 = now_dt - timedelta(minutes=30)
            (cx_dir / "rollout-desktop.jsonl").write_text(
                "\n".join([
                    json.dumps({"timestamp": iso(cx_t0), "type": "session_meta",
                                "payload": {"originator": "Codex Desktop", "cwd": "/tmp/demo"}}),
                    json.dumps({"timestamp": iso(cx_t0), "type": "event_msg",
                                "payload": {"type": "task_started", "turn_id": "cx1", "started_at": iso(cx_t0)}}),
                    json.dumps({"timestamp": iso(cx_t0 + timedelta(minutes=2)), "type": "event_msg",
                                "payload": {"type": "item_completed", "turn_id": "cx1",
                                            "started_at_ms": int(cx_t0.timestamp() * 1000),
                                            "completed_at_ms": int((cx_t0 + timedelta(minutes=2)).timestamp() * 1000)}}),
                    # 10-min silent pause before completion -> dropped!
                    json.dumps({"timestamp": iso(cx_t0 + timedelta(minutes=12)), "type": "event_msg",
                                "payload": {"type": "task_complete", "turn_id": "cx1",
                                            "started_at": iso(cx_t0), "completed_at": iso(cx_t0 + timedelta(minutes=12))}}),
                ]),
                encoding="utf-8",
            )

            payload = c.collect_all(home=home, days=7, cache_path=home / "cache.json", now_ts=now_ts)
            # Expected:
            # r1 = 240s (4m)
            # r2 = 120s (2m live)
            # r3 = 0s (pending approval from +1m to now, only 60s before wait had no step item -> 0s)
            # cx1 = 120s (2m; 10m gap dropped!)
            tools = {t["tool"]: t["agent_seconds"] for t in payload["today"]["tools"]}
            self.assertAlmostEqual(tools["t3_antigravity"], 360.0, delta=2.0)
            self.assertAlmostEqual(tools["codex"], 120.0, delta=2.0)
            self.assertEqual(len(payload["active_agents"]), 1)
            self.assertEqual(payload["active_agents"][0]["tool"], "t3_antigravity")


if __name__ == "__main__":
    unittest.main()
