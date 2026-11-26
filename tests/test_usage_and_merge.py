#!/usr/bin/env python3
"""Isolated unit tests for AgentPulse engine (pricing, usage, ledger, migration, quotas)."""

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "engine"))
sys.path.insert(0, str(ROOT / "scripts"))

import collector as c
import ledger as lg
import pricing as pr
import quota_collector as qc
import usage_collector as uc
import import_tokei as it


class TestAgentPulseEngine(unittest.TestCase):
    def test_codex_new_records_and_legacy_mirrors(self):
        now = datetime(2026, 11, 26, 18, tzinfo=uc.BIZ_TZ)
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.jsonl"
            for legacy_first in (False, True):
                usage = {"input_tokens": 100, "cached_input_tokens": 20,
                         "output_tokens": 30, "reasoning_output_tokens": 10}
                new = {"type": "token_usage_record", "payload": {
                    "usage": usage, "thread_token_usage": usage, "response_id": "r1"}}
                old = {"type": "event_msg", "payload": {"type": "token_count", "info": {
                    "total_token_usage": usage, "last_token_usage": usage}}}
                events = [old, new] if legacy_first else [new, old]
                events += [new, {"type": "token_usage_record", "payload": {
                    "usage": usage, "response_id": "r2"}}]
                path.write_text("\n".join(json.dumps(dict(event, timestamp=now.isoformat())) for event in events))
                rows = uc._parse_codex_file(path, pr.PricingTable({}, {}))
                self.assertEqual(sum(row["in"] for row in rows), 160)
                self.assertEqual(sum(row["requests"] for row in rows), 2)

    def test_zcode_scan_from_model_usage(self):
        now = datetime(2026, 11, 26, 18, tzinfo=uc.BIZ_TZ)
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            path = home / ".zcode/cli/db/db.sqlite"
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as conn:
                conn.execute("CREATE TABLE model_usage (model_id TEXT, input_tokens INTEGER, "
                             "output_tokens INTEGER, reasoning_tokens INTEGER, "
                             "cache_creation_input_tokens INTEGER, cache_read_input_tokens INTEGER, "
                             "completed_at INTEGER, started_at INTEGER)")
                conn.execute("INSERT INTO model_usage VALUES ('test',100,30,10,5,20,?,0)",
                             (int(now.timestamp() * 1000),))
            row = uc.collect_live_usage(home=home, now_ts=now.timestamp(), use_cache=False)["zcode"]["2026-11-26"]
            self.assertEqual([row[k] for k in ("in", "out", "cr", "cw", "thoughts")], [75, 20, 20, 5, 10])

    def test_claude_statusline_stores_only_unexpired_windows(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            qc.capture_claude_statusline(home, {"rate_limits": {
                "five_hour": {"used_percentage": 23.5, "resets_at": 2000},
                "seven_day": {"used_percentage": 80, "resets_at": 500},
            }, "session_id": "ignored"}, 1000)
            path = home / ".agent-pulse/claude_statusline_quota.json"
            self.assertEqual(set(json.loads(path.read_text())), {"q5", "q5_reset", "updated", "source"})
            result = qc.probe_claude_quota(home, {}, 1010)
            self.assertEqual(result["windows"][0]["used_percent"], 23.5)
            self.assertFalse(qc.probe_claude_quota(home, {}, 2000)["windows"])

    def test_pricing_resolution_and_cost(self):
        tbl = pr.PricingTable(
            models={
                "openai/gpt-6.2-sol": {"in": 2.0, "out": 10.0, "cache_read": 0.2, "cache_write": 0.0},
                "anthropic/claude-fable-5.2": {"in": 3.0, "out": 15.0, "cache_read": 0.3, "cache_write": 3.75},
            },
            aliases={"gpt-6.2": "openai/gpt-6.2-sol"},
        )
        # Exact, short, prefixed, date-suffixed, and aliased resolution
        self.assertIsNotNone(tbl.resolve("gpt-6.2-sol"))
        self.assertIsNotNone(tbl.resolve("gpt-6.2"))
        self.assertIsNotNone(tbl.resolve("proxy/claude-fable-5.2-20261111"))

        # 500K input + 100K output + 1M cache_read on gpt-6.2-sol
        # = 0.5*2 + 0.1*10 + 1.0*0.2 = 1.0 + 1.0 + 0.2 = 2.20
        cost = tbl.estimate_cost("gpt-6.2-sol", input_tokens=500_000, output_tokens=100_000, cache_read_tokens=1_000_000)
        self.assertAlmostEqual(cost, 2.20, places=4)

    def test_codex_usage_t3_dedup_and_delta_accounting(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            now_dt = datetime(2026, 11, 26, 16, 0, 0, tzinfo=uc.BIZ_TZ)
            now_ts = now_dt.timestamp()
            iso = (now_dt - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S+08:00")
            iso2 = (now_dt - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%S+08:00")

            codex_dir = home / ".codex" / "sessions" / "2026" / "11" / "26"
            codex_dir.mkdir(parents=True)

            # Session A: originated from T3 Code (should attribute to `t3_codex`, NOT `codex`)
            f_t3 = codex_dir / "t3-codex.jsonl"
            f_t3.write_text(
                "\n".join(
                    [
                        json.dumps({"timestamp": iso, "type": "session_meta", "payload": {"originator": "T3 Code"}}),
                        json.dumps({"timestamp": iso, "type": "turn_context", "payload": {"model": "gpt-6.2-sol"}}),
                        json.dumps({
                            "timestamp": iso, "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {"total_token_usage": {"input_tokens": 10000, "cached_input_tokens": 4000, "output_tokens": 500, "reasoning_output_tokens": 100}},
                            },
                        }),
                        # Exact duplicate echo should be ignored
                        json.dumps({
                            "timestamp": iso, "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {"total_token_usage": {"input_tokens": 10000, "cached_input_tokens": 4000, "output_tokens": 500, "reasoning_output_tokens": 100}},
                            },
                        }),
                        # Incremental update (+5000 in, +2000 cached, +300 out)
                        json.dumps({
                            "timestamp": iso2, "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {"total_token_usage": {"input_tokens": 15000, "cached_input_tokens": 6000, "output_tokens": 800, "reasoning_output_tokens": 100}},
                            },
                        }),
                    ]
                ) + "\n",
                encoding="utf-8",
            )

            # Session B: standalone Codex CLI (should attribute to `codex`)
            f_cli = codex_dir / "cli-codex.jsonl"
            f_cli.write_text(
                "\n".join(
                    [
                        json.dumps({"timestamp": iso, "type": "session_meta", "payload": {"originator": "codex_cli_rs"}}),
                        json.dumps({"timestamp": iso, "type": "turn_context", "payload": {"model": "gpt-6.2-sol"}}),
                        json.dumps({
                            "timestamp": iso, "type": "event_msg",
                            "payload": {
                                "type": "token_count",
                                "info": {"total_token_usage": {"input_tokens": 2000, "cached_input_tokens": 0, "output_tokens": 200, "reasoning_output_tokens": 0}},
                            },
                        }),
                    ]
                ) + "\n",
                encoding="utf-8",
            )

            tbl = pr.PricingTable({"openai/gpt-6.2-sol": {"in": 2.0, "out": 10.0, "cache_read": 0.2, "cache_write": 0.0}}, {})
            res = uc.collect_live_usage(home=home, days=7, use_cache=False, now_ts=now_ts, pricing=tbl)

            self.assertIn("t3_codex", res)
            self.assertIn("codex", res)
            t3_b = res["t3_codex"]["2026-11-26"]
            # Total input 15000 - cached 6000 = 9000 non-cached, 6000 cr, 700 non-reason out, 100 thoughts
            self.assertEqual(t3_b["in"], 9000)
            self.assertEqual(t3_b["cr"], 6000)
            self.assertEqual(t3_b["out"], 700)
            self.assertEqual(t3_b["thoughts"], 100)
            cli_b = res["codex"]["2026-11-26"]
            self.assertEqual(cli_b["in"], 2000)
            self.assertEqual(cli_b["out"], 200)

    def test_tokei_migration_idempotent_and_no_double_count(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            tokei_dir = home / ".tokei"
            tokei_dir.mkdir(parents=True)

            # Synthetic Tokei ledger with historical day + today
            fake_ledger = {
                "v": 1,
                "tools": {
                    "codex": {
                        "2026-06-01": {"in": 100000, "out": 10000, "cr": 50000, "cw": 0, "cost": 1.25},
                        "2026-11-26": {"in": 999999, "out": 99999, "cr": 0, "cw": 0, "cost": 99.0},
                    },
                    "claude": {
                        "2026-09-10": {"in": 50000, "out": 5000, "cr": 20000, "cw": 1000, "cost": 0.80},
                    },
                },
            }
            (tokei_dir / "ledger.json").write_text(json.dumps(fake_ledger), encoding="utf-8")

            r1 = it.migrate_from_tokei(home=home, skip_backup=False)
            self.assertTrue(r1["ok"])
            self.assertEqual(r1["imported_tools"], 2)
            self.assertEqual(r1["imported_days"], 3)
            self.assertTrue(Path(r1["backup_dir"]).is_dir())

            # Re-run migration: must be idempotent
            r2 = it.migrate_from_tokei(home=home, skip_backup=True)
            self.assertEqual(r2["imported_days"], 3)

            # Live usage for today (2026-11-26) should override imported codex on today without double counting!
            now_ts = datetime(2026, 11, 26, 18, 0, 0, tzinfo=uc.BIZ_TZ).timestamp()
            live = {
                "t3_codex": {
                    "2026-11-26": {
                        "in": 1000, "out": 200, "cr": 500, "cw": 0, "thoughts": 0,
                        "cost_usd": 0.12, "requests": 1, "models": {},
                    }
                }
            }
            usage_payload, daily_map = lg.build_usage_payload(
                ledger_path=home / ".agent-pulse" / "ledger.json",
                live_usage=live,
                live_cutoff_day="2026-11-13",
                now_ts=now_ts,
                persist=True,
            )
            # Today must reflect ONLY live (1700 tok, $0.12), NOT imported $99
            self.assertEqual(usage_payload["periods"]["today"]["total_tokens"], 1700)
            self.assertAlmostEqual(usage_payload["periods"]["today"]["cost_usd"], 0.12, places=2)
            # Year must still preserve 2026-06-01 codex ($1.25) + 2026-09-10 claude_code ($0.80) + today ($0.12) = $2.17
            self.assertAlmostEqual(usage_payload["periods"]["year"]["cost_usd"], 2.17, places=2)
            self.assertIn("2026-06-01", daily_map)
            self.assertIn("2026-09-10", daily_map)

    def test_invalid_tokei_ledger_preserves_existing_history(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            source = home / ".tokei" / "ledger.json"
            source.parent.mkdir()
            target = home / ".agent-pulse" / "ledger.json"
            target.parent.mkdir()
            original = '{"imported_tools":{"codex":{"2026-11-25":{"in":100}}}}'
            target.write_text(original)
            for content in ("{broken", "{}", '{"tools":{"codex":{}}}'):
                with self.subTest(content=content):
                    source.write_text(content)
                    self.assertFalse(it.migrate_from_tokei(home=home, skip_backup=True)["ok"])
                    self.assertEqual(target.read_text(), original)

    def test_recent_codex_import_survives_missing_live_day(self):
        day = "2026-11-25"
        imported = {"imported_tools": {"codex": {day: {"in": 100}}}}
        merged = lg._reconcile_merged_tools(imported, {"t3_codex": {"2026-11-26": {"in": 200}}})
        self.assertEqual(merged["codex"][day]["in"], 100)

    def test_cached_provider_quotas_do_not_raise_live_alerts(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            cache = home / ".agent-pulse" / "quota_cache.json"
            cache.parent.mkdir()
            window = {"label": "test", "short_label": "TEST", "used_percent": 99}
            cache.write_text(json.dumps({
                "antigravity": {"fetched_at": 1, "quota": {"available": True, "windows": [window]}},
                "grok": {"fetched_at": 999, "windows": [window]},
                "kimi": {"limits": {"five_hour": window}},
            }))
            snapshot = qc.collect_quotas(home=home, force=True, now_ts=1000)
            providers = {provider["id"]: provider for provider in snapshot["providers"]}
            for provider in ("antigravity", "grok", "kimi"):
                self.assertEqual(providers[provider]["status"], "stale")
            self.assertEqual(providers["kimi"]["fetched_at"], 0)
            self.assertIsNone(snapshot["highest_window"])

    def test_quota_collector_graceful_fallback_offline(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            now_ts = datetime(2026, 11, 26, 19, 0, 0, tzinfo=qc.BIZ_TZ).timestamp()
            ac_dir = home / ".agent-pulse"
            ac_dir.mkdir(parents=True)
            # Pre-seed cached codex + claude quotas
            (ac_dir / "quota_cache.json").write_text(
                json.dumps({
                    "codex": {
                        "fetched_at": now_ts - 60,
                        "plan": "pro",
                        "limits": {
                            "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": int(now_ts + 3600)},
                            "secondary": {"used_percent": 34.0, "window_minutes": 10080, "resets_at": int(now_ts + 86400)},
                        },
                    },
                    "claude": {"q5": 5, "q7": 15, "updated": int(now_ts - 120)},
                }),
                encoding="utf-8",
            )
            snap = qc.collect_quotas(home=home, force=False, now_ts=now_ts)
            self.assertEqual(snap["schema_version"], 1)
            by_id = {p["id"]: p for p in snap["providers"]}
            self.assertEqual(by_id["codex"]["status"], "ok")
            self.assertEqual(by_id["codex"]["windows"][0]["used_percent"], 12.0)
            self.assertEqual(by_id["claude"]["windows"][1]["used_percent"], 15.0)
            self.assertEqual(snap["highest_window"]["used_percent"], 34.0)

    def test_collector_v3_independent_without_tokei_dir(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            now_dt = datetime(2026, 11, 26, 15, 0, 0, tzinfo=c.BIZ_TZ)
            now_ts = now_dt.timestamp()
            # No ~/.tokei directory exists at all; only a WorkBuddy log exists
            wb_dir = home / ".workbuddy" / "projects" / "demo"
            wb_dir.mkdir(parents=True)
            ts_ms = int((now_dt - timedelta(minutes=5)).timestamp() * 1000)
            (wb_dir / "s1.jsonl").write_text(
                json.dumps({
                    "id": "m1",
                    "timestamp": ts_ms,
                    "type": "message",
                    "role": "assistant",
                    "providerData": {"modelName": "DeepSeek-V4.1-Flash"},
                    "message": {"usage": {"input_tokens": 50000, "output_tokens": 1000, "cache_read_input_tokens": 40000}},
                }) + "\n",
                encoding="utf-8",
            )
            payload = c.collect_all(home=home, days=7, use_cache=False, now_ts=now_ts)
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["usage"]["source"], "agent_pulse_native")
            self.assertEqual(payload["usage"]["periods"]["today"]["total_tokens"], 51000)
            self.assertEqual(payload["today"]["tokens"], 51000)
            # Legacy compatibility field populated from native engine (no ~/.tokei needed!)
            self.assertTrue(payload["tokei_ref"]["available"])
            self.assertEqual(payload["tokei_ref"]["today_tokens"], 51000)

    def test_legacy_agent_clock_dir_auto_migrates_safely(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            legacy = home / ".agent-clock"
            legacy.mkdir(parents=True)
            legacy_ledger = {
                "version": 1,
                "imported_tools": {"codex": {"2026-10-01": {"in": 1000, "out": 200, "cr": 0, "cw": 0, "thoughts": 0, "cost_usd": 0.5}}},
                "live_tools": {},
            }
            (legacy / "ledger.json").write_text(json.dumps(legacy_ledger), encoding="utf-8")
            resolved = lg.resolve_data_dir(home=home, create=True)
            self.assertEqual(resolved, home / ".agent-pulse")
            self.assertTrue((resolved / "ledger.json").is_file())
            self.assertEqual(json.loads((resolved / "ledger.json").read_text())["imported_tools"]["codex"]["2026-10-01"]["in"], 1000)
            # Legacy directory must remain untouched as a safety backup
            self.assertTrue((legacy / "ledger.json").is_file())

    def test_v05_seven_periods_model_rates_heatmap_and_projects(self):
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            # 2026-11-26 is Thursday -> Mon this week = 2026-11-23, Last week = 2026-11-16..2026-11-22
            now_dt = datetime(2026, 11, 26, 18, 0, 0, tzinfo=uc.BIZ_TZ)
            now_ts = now_dt.timestamp()

            def iso(dt):
                return dt.strftime("%Y-%m-%dT%H:%M:%S+08:00")

            # Today: Codex session inside project A (with T3 Code originator)
            cx_dir = home / ".codex" / "sessions" / "2026" / "11" / "26"
            cx_dir.mkdir(parents=True)
            (cx_dir / "s_today.jsonl").write_text(
                "\n".join([
                    json.dumps({"timestamp": iso(now_dt - timedelta(minutes=10)), "type": "session_meta",
                                "payload": {"originator": "T3 Code", "cwd": str(home / "dev" / "alpha-app")}}),
                    json.dumps({"timestamp": iso(now_dt - timedelta(minutes=10)), "type": "turn_context",
                                "payload": {"model": "gpt-6.2-sol"}}),
                    json.dumps({"timestamp": iso(now_dt - timedelta(minutes=5)), "type": "event_msg",
                                "payload": {"type": "token_count", "info": {"total_token_usage": {
                                    "input_tokens": 100000, "cached_input_tokens": 80000,
                                    "output_tokens": 4000, "reasoning_output_tokens": 1000
                                }}}}),
                ]) + "\n",
                encoding="utf-8",
            )

            # Yesterday: Claude Code session inside project B
            cc_dir = home / ".claude" / "projects" / "beta"
            cc_dir.mkdir(parents=True)
            yest_dt = now_dt - timedelta(days=1)
            (cc_dir / "c_yest.jsonl").write_text(
                json.dumps({
                    "timestamp": iso(yest_dt),
                    "cwd": str(home / "dev" / "beta-service"),
                    "message": {
                        "id": "msg_1",
                        "model": "claude-sonnet-4.5",
                        "usage": {"input_tokens": 20000, "output_tokens": 2000, "cache_read_input_tokens": 60000, "cache_creation_input_tokens": 5000},
                    },
                }) + "\n",
                encoding="utf-8",
            )

            # Last week (2026-11-18): Codex CLI session in project A
            lw_dt = datetime(2026, 11, 18, 12, 0, 0, tzinfo=uc.BIZ_TZ)
            (cx_dir / "s_lastweek.jsonl").write_text(
                "\n".join([
                    json.dumps({"timestamp": iso(lw_dt), "type": "session_meta",
                                "payload": {"originator": "codex_cli_rs", "cwd": str(home / "dev" / "alpha-app")}}),
                    json.dumps({"timestamp": iso(lw_dt), "type": "turn_context",
                                "payload": {"model": "gpt-6.2-sol"}}),
                    json.dumps({"timestamp": iso(lw_dt), "type": "event_msg",
                                "payload": {"type": "token_count", "info": {"total_token_usage": {
                                    "input_tokens": 50000, "cached_input_tokens": 25000,
                                    "output_tokens": 2000, "reasoning_output_tokens": 0
                                }}}}),
                ]) + "\n",
                encoding="utf-8",
            )

            tbl = pr.PricingTable(
                models={
                    "openai/gpt-6.2-sol": {"in": 2.0, "out": 10.0, "cache_read": 0.2, "cache_write": 0.0},
                    "anthropic/claude-sonnet-4.5": {"in": 3.0, "out": 15.0, "cache_read": 0.3, "cache_write": 3.75},
                },
                aliases={},
            )
            live_projects = {}
            live_usage = uc.collect_live_usage(
                home=home, days=14, use_cache=False, now_ts=now_ts, pricing=tbl, projects_out=live_projects
            )
            usage_payload, _ = lg.build_usage_payload(
                ledger_path=home / ".agent-pulse" / "ledger.json",
                live_usage=live_usage,
                live_cutoff_day="2026-11-13",
                now_ts=now_ts,
                persist=True,
                pricing=tbl,
                live_projects=live_projects,
                home=home,
            )

            periods = usage_payload["periods"]
            for pk in ("today", "yesterday", "week", "last_week", "month", "year", "all"):
                self.assertIn(pk, periods)

            # Today: t3_codex only, 1 session, 80% cache hit, pricing rates populated
            t_tools = periods["today"]["tools"]
            self.assertEqual(len(t_tools), 1)
            self.assertEqual(t_tools[0]["tool"], "t3_codex")
            self.assertEqual(t_tools[0]["sessions"], 1)
            self.assertAlmostEqual(t_tools[0]["cache_hit_rate"], 0.8, places=3)
            m0 = t_tools[0]["models"][0]
            self.assertAlmostEqual(m0["rate_in"], 2.0, places=3)
            self.assertAlmostEqual(m0["rate_out"], 10.0, places=3)
            self.assertAlmostEqual(m0["rate_cache_read"], 0.2, places=3)

            # Yesterday: claude_code only
            self.assertEqual(periods["yesterday"]["tools"][0]["tool"], "claude_code")
            # Last week: standalone codex only
            self.assertEqual(periods["last_week"]["tools"][0]["tool"], "codex")
            # All period has cross-tool top_models
            self.assertGreaterEqual(len(periods["all"]["top_models"]), 2)

            # Heatmap: 182 days, 2-day current streak (today + yesterday), 3 total active days
            hm = usage_payload["heatmap"]
            self.assertEqual(len(hm["days"]), 182)
            self.assertEqual(hm["current_streak"], 2)
            self.assertEqual(hm["longest_streak"], 2)
            self.assertEqual(hm["total_active_days"], 3)

            # Projects trail: alpha-app (today, 2 sessions across t3_codex + codex) and beta-service (week)
            projs = {p["name"]: p for p in usage_payload["projects"]}
            self.assertIn("alpha-app", projs)
            self.assertIn("beta-service", projs)
            self.assertEqual(projs["alpha-app"]["group"], "today")
            self.assertEqual(projs["alpha-app"]["sessions"], 2)
            self.assertEqual(projs["alpha-app"]["display_path"], "~/dev/alpha-app")
            self.assertEqual(projs["beta-service"]["group"], "week")


if __name__ == "__main__":
    unittest.main()
