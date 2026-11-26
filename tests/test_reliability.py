import json
import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "engine"), str(ROOT / "scripts")]

import collector
import import_tokei
import ledger
import pricing
import quota_collector as quota
import usage_collector as usage
from storage import sqlite_snapshot


NOW = datetime(2026, 11, 26, 18, tzinfo=ledger.BIZ_TZ)
DAY = NOW.date().isoformat()


def store_events(path, *events):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(
        dict(item, timestamp=NOW.isoformat())
    ) + "\n" for item in events), encoding="utf-8")
    os.utime(path, (NOW.timestamp(), NOW.timestamp()))


def legacy_event(tokens):
    return {"type": "event_msg", "payload": {
        "type": "token_count", "info": {"total_token_usage": tokens, "last_token_usage": tokens},
    }}


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.data = self.home / ".agent-pulse"
        self.path = self.data / "ledger.json"

    def test_private_files_and_restore_preserve_corrupt_original(self):
        self.data.mkdir(mode=0o755)
        self.path.write_text('{"live_tools":{"codex":{"2026-11-25":{"in":99}}}}')
        self.path.chmod(0o644)
        ledger.resolve_data_dir(self.home)
        ledger.build_usage_payload(self.path, {"codex": {DAY: {"in": 100}}}, DAY, NOW.timestamp())
        previous = self.data / "ledger.previous.json"
        for path, mode in ((self.data, 0o700), (self.path, 0o600), (previous, 0o600)):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)

        original = '{"live_tools":'
        self.path.write_text(original)
        with self.assertRaises(ledger.LedgerError):
            collector.collect_all(home=self.home, now_ts=NOW.timestamp())
        self.assertEqual(self.path.read_text(), original)
        retained = ledger.restore_ledger(self.path, previous)
        self.assertEqual(retained.read_text(), original)
        self.assertEqual(ledger.load_ledger(self.path)["live_tools"]["codex"]["2026-11-25"]["in"], 99)

    def test_concurrent_cli_writers_preserve_other_tool_days(self):
        self.data.mkdir()
        command = (
            "import sys; from pathlib import Path; from ledger import build_usage_payload; "
            "p, tool = sys.argv[1:]; day = '2026-11-26'; "
            "build_usage_payload(Path(p), {tool:{day:{'in':100}}}, day, 1795698000)"
        )
        children = [
            subprocess.Popen([sys.executable, "-B", "-c", command, str(self.path), f"tool-{idx}"],
                             cwd=ROOT / "engine", env=dict(os.environ, HOME=str(self.home)))
            for idx in range(5)
        ]
        for child in children:
            self.assertEqual(child.wait(timeout=10), 0)
        self.assertEqual(len(ledger.load_ledger(self.path)["live_tools"]), len(children))

    def test_invalid_tokei_input_leaves_current_ledger_unchanged(self):
        self.data.mkdir()
        original = '{"live_tools":{"codex":{"2026-11-25":{"in":99}}}}'
        self.path.write_text(original)
        source = self.home / ".tokei" / "ledger.json"
        source.parent.mkdir()
        for day, record in (("2026-11-31", {"in": 25}), (DAY, {"cost": float("inf")})):
            with self.subTest(day=day):
                source.write_text(json.dumps({"tools": {"codex": {day: record}}}))
                self.assertFalse(import_tokei.migrate_from_tokei(home=self.home)["ok"])
                self.assertEqual(self.path.read_text(), original)

    def test_legacy_invalid_day_no_longer_breaks_refresh(self):
        self.data.mkdir()
        original = '{"imported_tools":{"codex":{"unknown-date":{"in":100}}}}'
        self.path.write_text(original)
        snapshot = collector.collect_all(home=self.home, now_ts=NOW.timestamp())
        self.assertEqual(snapshot["usage"]["periods"]["all"]["total_tokens"], 0)
        self.assertEqual(snapshot["usage"]["ledger_meta"]["skipped_invalid_days"], 1)
        self.assertIn("unknown-date", ledger.load_ledger(self.path)["imported_tools"]["codex"])

    def test_opt_in_and_account_scoped_quota(self):
        now = NOW.timestamp()
        cache = {"codex": {
            "account_key": "account-a", "fetched_at": now - 10, "plan": "first",
            "limits": {"primary": {"used_percent": 65}},
        }}
        with patch.object(quota, "_codex_auth_context") as credentials, patch.object(
            quota.urllib.request, "build_opener"
        ) as network:
            client = network.return_value
            client.open.side_effect = OSError("offline fixture")
            self.assertEqual(quota.probe_codex_quota(self.home, cache, now)["status"], "unavailable")
            credentials.assert_not_called()
            client.open.assert_not_called()

            credentials.return_value = {"account_key": "account-a", "access_token": "fixture"}
            self.assertEqual(quota.probe_codex_quota(self.home, cache, now, allow_live=True)["plan"], "First")
            client.open.assert_not_called()

            credentials.return_value = {"account_key": "account-b", "access_token": "fixture"}
            self.assertEqual(quota.probe_codex_quota(self.home, cache, now, allow_live=True)["windows"], [])
            quota.probe_codex_quota(self.home, cache, now + 1, force=True, allow_live=True)
            client.open.assert_called_once()

    def test_mixed_codex_events_without_cumulative_and_partial_import(self):
        path = self.home / "fixture.jsonl"
        tokens = {"input_tokens": 100, "output_tokens": 20}
        modern = {"type": "token_usage_record", "payload": {"usage": tokens, "response_id": "fixture-response"}}
        legacy = legacy_event(tokens)
        for events in ((modern, legacy), (legacy, modern)):
            store_events(path, *events)
            rows = usage._parse_codex_file(path, pricing.PricingTable({}, {}))
            self.assertEqual(sum(row["in"] for row in rows), 100)

        imported = {"imported_tools": {"codex": {DAY: {"in": 1000}}}}
        merged = ledger._reconcile_merged_tools(imported, {"t3_codex": {DAY: {"in": 100}}})
        self.assertEqual(merged["codex"][DAY]["in"], 1000)
        self.assertNotIn(DAY, merged["t3_codex"])

    def test_price_change_recomputes_warm_cache_and_labels_unpriced_usage(self):
        path = self.home / ".codex" / "sessions" / "fixture.jsonl"
        model = "meter-fixture"
        store_events(path, {"type": "turn_context", "payload": {"model": model}},
                     legacy_event({"input_tokens": 1_000_000}))
        expected = [(1, 1.0, 0), (2, 2.0, 0), (None, 0.0, 1_000_000)]
        for rate, cost, unpriced in expected:
            rates = {} if rate is None else {model: {"in": rate}}
            table = pricing.PricingTable(rates, {})
            live = usage.collect_live_usage(home=self.home, now_ts=NOW.timestamp(), pricing=table)
            summary, _ = ledger.build_usage_payload(None, live, DAY, NOW.timestamp(), pricing=table)
            self.assertEqual(summary["periods"]["today"]["cost_usd"], cost)
            self.assertEqual(summary["periods"]["today"]["unpriced_tokens"], unpriced)
        store_events(path, legacy_event({"input_tokens": 1_000_000}))
        table = pricing.PricingTable({"gpt-5": {"in": 1}}, {})
        live = usage.collect_live_usage(home=self.home, now_ts=NOW.timestamp(), pricing=table)
        self.assertEqual(live["codex"][DAY]["unpriced_tokens"], 1_000_000)

    def test_work_history_survives_shorter_future_scans(self):
        old = NOW - timedelta(days=35)
        segment = collector.WorkSegment("codex", old.timestamp(), (old + timedelta(minutes=2)).timestamp())
        with patch.object(collector, "collect_codex", return_value=[segment]):
            first = collector.collect_all(home=self.home, now_ts=NOW.timestamp())
        second = collector.collect_all(home=self.home, now_ts=NOW.timestamp())
        for payload in (first["usage"], second["usage"]):
            self.assertEqual(payload["periods"]["all"]["agent_seconds"], 120)
            heatmap_day = next(day for day in payload["heatmap"]["days"] if day["date"] == old.date().isoformat())
            self.assertGreater(heatmap_day["agent_hours"], 0)

    def test_sqlite_snapshot_reads_committed_wal_without_touching_source(self):
        source = self.home / "fixture.sqlite"
        with sqlite3.connect(source) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE fixture(value)")
            writer.execute("INSERT INTO fixture VALUES (42)")
            writer.commit()
            wal = self.home / "fixture.sqlite-wal"
            original = (source.read_bytes(), wal.read_bytes())
            with sqlite_snapshot(source) as reader:
                self.assertEqual(reader.execute("SELECT value FROM fixture").fetchone()[0], 42)
            self.assertEqual((source.read_bytes(), wal.read_bytes()), original)
        writer.close()
