#!/usr/bin/env python3
"""AgentPulse — Local read-only AI work-hours, sentinel, token & cost collector (v0.7).

Methodology (Step-Heartbeat Pure AI Execution):
- Excludes all between-turn human reading/thinking/typing time.
- Uses a 5-minute inter-step heartbeat window (HEARTBEAT_GAP_SECONDS = 300s) across
  all 9 local AI work sources (including Antigravity CLI) with NO artificial 30m cap.
- Explicitly subtracts T3 Code human-wait intervals and surfaces 3-state desktop
  sentinel tasks (`waiting_human` | `running` | `just_finished`).
- Strictly read-only on all AI client logs and SQLite databases (WAL-safe temp snapshots).
- Never scans ~/.orca; worktree merging uses only standard read-only Git metadata.

Zero external dependencies (Python 3.9+ stdlib only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_ENGINE_DIR = str(Path(__file__).resolve().parent)
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from health import (  # noqa: E402
    WORK_SOURCE_IDS,
    check_root_dir,
    combine_statuses,
    read_jsonl_checked,
    resolve_missing_source_status,
)
from ledger import LedgerError, build_usage_payload, load_ledger, resolve_data_dir, restore_ledger, valid_day  # noqa: E402
from pricing import PricingTable  # noqa: E402
from storage import sqlite_snapshot_with_status, write_json  # noqa: E402
from usage_collector import _normalize_project_path, collect_live_usage  # noqa: E402
from worktree import WorktreeResolver  # noqa: E402

# Explicit business timezone: Asia/Shanghai (UTC+8)
BIZ_TZ = timezone(timedelta(hours=8))

# Step-heartbeat pure AI execution guardrails
HEARTBEAT_GAP_SECONDS = 300.0
SINGLE_STEP_CAP_SECONDS = 900.0
MIN_TURN_SECONDS = 0.5
WORKDAY_SECONDS = 8.0 * 3600.0
LIVE_STALE_SECONDS = 90.0
SNAPSHOT_FRESH_SECONDS = 45.0
JUST_FINISHED_MIN_SECONDS = 600.0
JUST_FINISHED_WINDOW_SECONDS = 120.0

CACHE_VERSION = 5

TOOL_META: Dict[str, Dict[str, str]] = {
    "t3_antigravity": {"name": "T3 · Antigravity", "short": "T3 Agy", "color": "#7C6BF5"},
    "t3_codex":       {"name": "T3 · Codex",       "short": "T3 Codex", "color": "#10B981"},
    "t3_claude":      {"name": "T3 · Claude",      "short": "T3 Claude", "color": "#F59E0B"},
    "t3_other":       {"name": "T3 · Agent",       "short": "T3", "color": "#8B5CF6"},
    "codex":          {"name": "Codex",            "short": "Codex", "color": "#34D399"},
    "workbuddy":      {"name": "WorkBuddy",        "short": "WorkBuddy", "color": "#38BDF8"},
    "hermes":         {"name": "Hermes",           "short": "Hermes", "color": "#F472B6"},
    "antigravity":    {"name": "Antigravity",      "short": "Agy App", "color": "#A78BFA"},
    "claude_code":    {"name": "Claude Code",      "short": "Claude", "color": "#FB923C"},
    "pi":             {"name": "Pi",               "short": "Pi", "color": "#22D3EE"},
    "opencode":       {"name": "OpenCode",         "short": "OpenCode", "color": "#94A3B8"},
}

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

T3_HUMAN_WAIT_TYPES = frozenset({"approval_request", "user_input_request", "secret_request"})
T3_WAIT_ITEM_KIND = {
    "approval_request": "approval",
    "user_input_request": "user_input",
    "secret_request": "secret",
}
T3_RUNTIME_REQ_KINDS = {
    "command": "approval",
    "approval": "approval",
    "user_input": "user_input",
    "secret": "secret",
}
T3_OPEN_WAIT_STATUSES = frozenset({"pending", "requested", "open", "waiting"})
T3_CLOSED_ITEM_STATUSES = frozenset({
    "completed", "resolved", "rejected", "cancelled", "canceled",
    "expired", "failed", "error", "denied", "aborted",
})
T3_NON_AGENT_TYPES = frozenset({"user_message", "system_notice", "notification", "handoff"})
CLI_TERMINAL_STATUSES = frozenset({"DONE", "COMPLETED", "ERROR", "FAILED", "CANCELLED", "CANCELED", "ABORTED"})

VALID_PERIOD_KEYS = ("today", "yesterday", "week", "last_week", "month", "year", "all")
PERIOD_TITLES_CN = {
    "today": "今天",
    "yesterday": "昨天",
    "week": "本周",
    "last_week": "上周",
    "month": "本月",
    "year": "今年",
    "all": "全部",
}


@dataclass
class WorkSegment:
    """A single bounded pure-AI active work interval [start_ts, end_ts]."""

    tool: str
    start_ts: float
    end_ts: float
    is_active: bool = False
    run_key: str = ""
    label: str = ""
    is_turn_head: bool = True
    workspace_path: Optional[str] = None
    project_path: Optional[str] = None
    source_id: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)


@dataclass
class SentinelCandidate:
    """Candidate 3-state sentinel task before final gating and ordering."""

    source_id: str
    tool: str
    run_key: str
    label: str
    state: str  # running | waiting_human | just_finished
    started_at: float
    state_since_ts: float
    expires_at: float
    elapsed_seconds: Optional[float] = None
    workspace_path: Optional[str] = None
    project_path: Optional[str] = None
    evidence_kind: str = "heartbeat"
    wait_kind: Optional[str] = None


def make_sentinel_id(source_id: str, run_key: str) -> str:
    return hashlib.sha256(f"{source_id}\x00{run_key}".encode("utf-8")).hexdigest()


def parse_ts(value: Any) -> Optional[float]:
    """Parse ISO-8601 string or epoch sec/ms into Unix epoch float seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0 or not math.isfinite(v):
            return None
        if v > 1e14:
            return v / 1e6
        if v > 1e11:
            return v / 1e3
        return v
    if isinstance(value, str):
        s = value.strip()
        if not s or s.startswith("0001-01-01"):
            return None
        try:
            return parse_ts(float(s))
        except ValueError:
            pass
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        if "." in s:
            head, rest = s.split(".", 1)
            frac, tz = rest, ""
            for i, ch in enumerate(rest):
                if ch in "+-":
                    frac, tz = rest[:i], rest[i:]
                    break
            frac = (frac + "000000")[:6]
            s = f"{head}.{frac}{tz}"
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=BIZ_TZ)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def merge_raw_intervals(intervals: Iterable[Tuple[float, float]], bridge_gap: float = 0.5) -> List[Tuple[float, float]]:
    ivs = sorted((s, e) for s, e in intervals if e > s)
    if not ivs:
        return []
    merged: List[Tuple[float, float]] = [ivs[0]]
    for s, e in ivs[1:]:
        ps, pe = merged[-1]
        if s <= pe + bridge_gap:
            if e > pe:
                merged[-1] = (ps, e)
        else:
            merged.append((s, e))
    return merged


def subtract_exclusions(
    intervals: List[Tuple[float, float]],
    exclusions: List[Tuple[float, float]],
) -> List[Tuple[float, float]]:
    if not intervals or not exclusions:
        return intervals
    ex_merged = merge_raw_intervals(exclusions, bridge_gap=0.0)
    result: List[Tuple[float, float]] = []
    for s, e in intervals:
        cur = [(s, e)]
        for xs, xe in ex_merged:
            nxt: List[Tuple[float, float]] = []
            for cs, ce in cur:
                if xe <= cs or xs >= ce:
                    nxt.append((cs, ce))
                else:
                    if xs > cs:
                        nxt.append((cs, xs))
                    if xe < ce:
                        nxt.append((xe, ce))
            cur = nxt
            if not cur:
                break
        result.extend(cur)
    return [(s, e) for s, e in result if e - s >= 0.1]


def build_heartbeat_segments(
    tool: str,
    turn_start: float,
    step_timestamps: List[float],
    extra_spans: Optional[List[Tuple[float, float]]] = None,
    exclusions: Optional[List[Tuple[float, float]]] = None,
    run_key: str = "",
    label: str = "",
    is_active: bool = False,
    heartbeat_gap_s: float = HEARTBEAT_GAP_SECONDS,
    workspace_path: Optional[str] = None,
    project_path: Optional[str] = None,
    source_id: str = "",
) -> List[WorkSegment]:
    pts = [turn_start]
    for t in step_timestamps:
        if t >= turn_start:
            pts.append(t)
    pts.sort()

    raw_intervals: List[Tuple[float, float]] = []
    for a, b in zip(pts, pts[1:]):
        delta = b - a
        if 0.0 < delta <= heartbeat_gap_s:
            raw_intervals.append((a, b))

    if extra_spans:
        for s, e in extra_spans:
            if e > s:
                capped_e = min(e, s + SINGLE_STEP_CAP_SECONDS)
                if capped_e > s:
                    raw_intervals.append((s, capped_e))

    merged = merge_raw_intervals(raw_intervals, bridge_gap=1.0)
    if exclusions:
        merged = subtract_exclusions(merged, exclusions)

    total_sec = sum(e - s for s, e in merged)
    if total_sec < MIN_TURN_SECONDS:
        return []

    result: List[WorkSegment] = []
    for idx, (s, e) in enumerate(merged):
        result.append(
            WorkSegment(
                tool=tool,
                start_ts=s,
                end_ts=e,
                is_active=(is_active and idx == len(merged) - 1),
                run_key=run_key,
                label=label,
                is_turn_head=(idx == 0),
                workspace_path=workspace_path,
                project_path=project_path,
                source_id=source_id,
            )
        )
    return result


def split_segment_by_biz_day(seg: WorkSegment) -> List[Tuple[str, WorkSegment]]:
    if seg.end_ts <= seg.start_ts:
        return []
    out: List[Tuple[str, WorkSegment]] = []
    cur = seg.start_ts
    first_chunk = True
    while cur < seg.end_ts:
        dt = datetime.fromtimestamp(cur, tz=BIZ_TZ)
        day_str = dt.strftime("%Y-%m-%d")
        next_midnight = datetime(dt.year, dt.month, dt.day, tzinfo=BIZ_TZ) + timedelta(days=1)
        chunk_end = min(seg.end_ts, next_midnight.timestamp())
        if chunk_end > cur:
            out.append(
                (
                    day_str,
                    WorkSegment(
                        tool=seg.tool,
                        start_ts=cur,
                        end_ts=chunk_end,
                        is_active=(seg.is_active and chunk_end >= seg.end_ts),
                        run_key=seg.run_key,
                        label=seg.label,
                        is_turn_head=(seg.is_turn_head and first_chunk),
                        workspace_path=seg.workspace_path,
                        project_path=seg.project_path,
                        source_id=seg.source_id,
                    ),
                )
            )
            first_chunk = False
        cur = chunk_end
    return out


def merge_wall_seconds(segments: Iterable[WorkSegment]) -> float:
    ivs = merge_raw_intervals(((s.start_ts, s.end_ts) for s in segments), bridge_gap=0.0)
    return sum(e - s for s, e in ivs)


def _norm_opt_path(raw: Any) -> Optional[str]:
    p = _normalize_project_path(raw)
    return p if p else None


# ---------------------------------------------------------------------------
# File Parse Cache v5 (incremental mtime_ns + size cache for JSONL files)
# ---------------------------------------------------------------------------


class SegmentCache:
    """Stores parsed non-active segments keyed by file path + (mtime_ns, size) (v5)."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self.files: Dict[str, Dict[str, Any]] = {}
        self.seen: set = set()
        self.dirty = False
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("v") == CACHE_VERSION:
                    files = raw.get("files")
                    if isinstance(files, dict):
                        self.files = files
                else:
                    self.dirty = True
            except Exception:
                self.files = {}
                self.dirty = True

    def get_entry(
        self,
        file_path: Path,
        now_ts: float,
        *,
        source_id: str = "",
        require_source_rank: bool = False,
    ) -> Optional[Dict[str, Any]]:
        try:
            st = file_path.stat()
        except OSError:
            return None
        if now_ts - st.st_mtime <= LIVE_STALE_SECONDS + 15.0:
            return None
        key = hashlib.sha256(os.fsencode(file_path)).hexdigest()
        self.seen.add(key)
        entry = self.files.get(key)
        if not isinstance(entry, dict):
            return None
        if entry.get("mtime_ns") != st.st_mtime_ns or entry.get("size") != st.st_size:
            return None
        if require_source_rank:
            sr = entry.get("source_rank")
            if not (
                isinstance(sr, dict)
                and isinstance(sr.get("latest_event_ts"), (int, float))
                and isinstance(sr.get("valid_record_count"), int)
            ):
                return None

        raw_segs = entry.get("segs")
        if not isinstance(raw_segs, list):
            return None
        segs: List[WorkSegment] = []
        for row in raw_segs:
            if not isinstance(row, list) or len(row) != 8:
                return None
            segs.append(
                WorkSegment(
                    tool=str(row[0]),
                    start_ts=float(row[1]),
                    end_ts=float(row[2]),
                    is_active=False,
                    is_turn_head=bool(row[3]),
                    run_key=str(row[4] or ""),
                    label=str(row[5] or ""),
                    workspace_path=row[6] if isinstance(row[6], str) else None,
                    project_path=row[7] if isinstance(row[7], str) else None,
                    source_id=source_id,
                )
            )
        completed = entry.get("completed")
        if completed is not None and not isinstance(completed, list):
            return None
        return {
            "segs": segs,
            "completed": completed or [],
            "source_rank": entry.get("source_rank"),
        }

    def get(self, file_path: Path, now_ts: float) -> Optional[List[WorkSegment]]:
        ent = self.get_entry(file_path, now_ts)
        return ent["segs"] if ent is not None else None

    def put_entry(
        self,
        file_path: Path,
        segs: List[WorkSegment],
        now_ts: float,
        *,
        completed: Optional[List[Dict[str, Any]]] = None,
        source_rank: Optional[Dict[str, Any]] = None,
        stat_result: Optional[os.stat_result] = None,
    ) -> None:
        try:
            st = stat_result if stat_result is not None else file_path.stat()
        except OSError:
            return
        if now_ts - st.st_mtime <= LIVE_STALE_SECONDS + 15.0:
            return
        if any(s.is_active for s in segs):
            return
        key = hashlib.sha256(os.fsencode(file_path)).hexdigest()
        self.seen.add(key)
        entry: Dict[str, Any] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "segs": [
                [
                    s.tool,
                    float(s.start_ts),
                    float(s.end_ts),
                    1 if s.is_turn_head else 0,
                    s.run_key,
                    s.label,
                    s.workspace_path,
                    s.project_path,
                ]
                for s in segs
            ],
        }
        if completed:
            entry["completed"] = completed
        if source_rank is not None:
            entry["source_rank"] = source_rank
        self.files[key] = entry
        self.dirty = True

    def put(self, file_path: Path, segs: List[WorkSegment], now_ts: float) -> None:
        self.put_entry(file_path, segs, now_ts)

    def save(self) -> None:
        if not self.path or (not self.dirty and self.files.keys() <= self.seen):
            return
        try:
            write_json(
                self.path,
                {
                    "v": CACHE_VERSION,
                    "files": {key: value for key, value in self.files.items() if key in self.seen},
                },
            )
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Source Collectors
# ---------------------------------------------------------------------------


def _walk_jsonl(root: Path, *, skip_substr: str = "") -> Tuple[List[Path], int]:
    paths: List[Path] = []
    dir_err = 0

    def on_error(_exc: OSError) -> None:
        nonlocal dir_err
        dir_err += 1

    for cur_root, _, files in os.walk(root, onerror=on_error):
        if skip_substr and skip_substr in cur_root:
            continue
        for fname in files:
            if fname.endswith(".jsonl"):
                paths.append(Path(cur_root) / fname)
    return paths, dir_err


def _file_scan_status(ok_count: int, err_count: int, dir_err: int) -> str:
    if err_count == 0 and dir_err == 0:
        return "complete"
    if ok_count == 0:
        return "unavailable"
    return "partial"


def collect_t3_code(
    db_path: Path,
    cutoff_ts: float,
    now_ts: float,
    *,
    sentinels_out: Optional[List[SentinelCandidate]] = None,
    status_out: Optional[Dict[str, str]] = None,
    meta_status_out: Optional[Dict[str, str]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> List[WorkSegment]:
    """Collect step-heartbeat intervals & 3-state sentinels from T3 Code statev2.sqlite."""
    segs: List[WorkSegment] = []
    with sqlite_snapshot_with_status(db_path) as (conn, snap_status):
        if snap_status == "missing":
            if status_out is not None:
                status_out["work.t3"] = resolve_missing_source_status("work.t3", prev_sources or {})
            return segs
        if snap_status != "ok" or conn is None:
            if status_out is not None:
                status_out["work.t3"] = "unavailable"
            return segs

        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error:
            if status_out is not None:
                status_out["work.t3"] = "unavailable"
            return segs

        if "orchestration_v2_projection_runs" not in tables:
            if status_out is not None:
                status_out["work.t3"] = "unavailable"
            return segs

        had_partial = False
        meta_failed = False

        # 1) Thread titles & structured project/worktree paths
        titles: Dict[str, str] = {}
        thread_project_id: Dict[str, str] = {}
        thread_worktree_path: Dict[str, str] = {}
        project_workspace_root: Dict[str, str] = {}

        if "orchestration_v2_projection_threads" in tables:
            try:
                tcols = {r[1] for r in conn.execute('PRAGMA table_info("orchestration_v2_projection_threads")').fetchall()}
                sel = ["thread_id", "title"]
                if "project_id" in tcols:
                    sel.append("project_id")
                if "payload_json" in tcols:
                    sel.append("payload_json")
                for r in conn.execute(f"SELECT {', '.join(sel)} FROM orchestration_v2_projection_threads"):
                    tid = str(r["thread_id"] or "")
                    if not tid:
                        continue
                    if r["title"]:
                        titles[tid] = str(r["title"])[:48]
                    if "project_id" in tcols and r["project_id"]:
                        thread_project_id[tid] = str(r["project_id"])
                    if "payload_json" in tcols and r["payload_json"]:
                        try:
                            pj = json.loads(r["payload_json"])
                            if isinstance(pj, dict):
                                wp = _norm_opt_path(pj.get("worktreePath") or pj.get("worktree_path"))
                                if wp:
                                    thread_worktree_path[tid] = wp
                        except Exception:
                            pass
            except sqlite3.Error:
                meta_failed = True

        if "projection_threads" in tables:
            try:
                pcols = {r[1] for r in conn.execute('PRAGMA table_info("projection_threads")').fetchall()}
                sel = ["thread_id"]
                if "project_id" in pcols:
                    sel.append("project_id")
                if "worktree_path" in pcols:
                    sel.append("worktree_path")
                for r in conn.execute(f"SELECT {', '.join(sel)} FROM projection_threads"):
                    tid = str(r["thread_id"] or "")
                    if not tid:
                        continue
                    if "project_id" in pcols and r["project_id"] and tid not in thread_project_id:
                        thread_project_id[tid] = str(r["project_id"])
                    if "worktree_path" in pcols and r["worktree_path"]:
                        wp = _norm_opt_path(r["worktree_path"])
                        if wp:
                            thread_worktree_path[tid] = wp
            except sqlite3.Error:
                meta_failed = True

        if "projection_projects" in tables:
            try:
                prcols = {r[1] for r in conn.execute('PRAGMA table_info("projection_projects")').fetchall()}
                if "project_id" in prcols and "workspace_root" in prcols:
                    for r in conn.execute("SELECT project_id, workspace_root FROM projection_projects"):
                        pid = str(r["project_id"] or "")
                        wr = _norm_opt_path(r["workspace_root"])
                        if pid and wr:
                            project_workspace_root[pid] = wr
            except sqlite3.Error:
                meta_failed = True

        # 2) Association maps for runtime_requests -> run_id
        node_to_run: Dict[str, str] = {}
        req_to_run: Dict[str, str] = {}
        pturn_to_run: Dict[str, str] = {}
        attempt_to_run: Dict[str, str] = {}

        if "orchestration_v2_projection_nodes" in tables:
            try:
                ncols = {r[1] for r in conn.execute('PRAGMA table_info("orchestration_v2_projection_nodes")').fetchall()}
                sel = ["node_id", "run_id"]
                if "runtime_request_id" in ncols:
                    sel.append("runtime_request_id")
                if "provider_turn_id" in ncols:
                    sel.append("provider_turn_id")
                for r in conn.execute(f"SELECT {', '.join(sel)} FROM orchestration_v2_projection_nodes"):
                    rid = str(r["run_id"] or "")
                    if not rid:
                        continue
                    nid = str(r["node_id"] or "")
                    if nid:
                        node_to_run[nid] = rid
                    if "runtime_request_id" in ncols and r["runtime_request_id"]:
                        req_to_run[str(r["runtime_request_id"])] = rid
                    if "provider_turn_id" in ncols and r["provider_turn_id"]:
                        pturn_to_run[str(r["provider_turn_id"])] = rid
            except sqlite3.Error:
                pass

        if "orchestration_v2_projection_run_attempts" in tables:
            try:
                acols = {r[1] for r in conn.execute('PRAGMA table_info("orchestration_v2_projection_run_attempts")').fetchall()}
                sel = ["attempt_id", "run_id"]
                if "provider_turn_id" in acols:
                    sel.append("provider_turn_id")
                for r in conn.execute(f"SELECT {', '.join(sel)} FROM orchestration_v2_projection_run_attempts"):
                    rid = str(r["run_id"] or "")
                    if not rid:
                        continue
                    if r["attempt_id"]:
                        attempt_to_run[str(r["attempt_id"])] = rid
                    if "provider_turn_id" in acols and r["provider_turn_id"]:
                        pturn_to_run.setdefault(str(r["provider_turn_id"]), rid)
            except sqlite3.Error:
                pass

        if "orchestration_v2_projection_provider_turns" in tables:
            try:
                ptcols = {r[1] for r in conn.execute('PRAGMA table_info("orchestration_v2_projection_provider_turns")').fetchall()}
                sel = ["provider_turn_id"]
                if "node_id" in ptcols:
                    sel.append("node_id")
                if "run_attempt_id" in ptcols:
                    sel.append("run_attempt_id")
                for r in conn.execute(f"SELECT {', '.join(sel)} FROM orchestration_v2_projection_provider_turns"):
                    ptid = str(r["provider_turn_id"] or "")
                    if not ptid or ptid in pturn_to_run:
                        continue
                    rid = None
                    if "node_id" in ptcols and r["node_id"]:
                        rid = node_to_run.get(str(r["node_id"]))
                    if not rid and "run_attempt_id" in ptcols and r["run_attempt_id"]:
                        rid = attempt_to_run.get(str(r["run_attempt_id"]))
                    if rid:
                        pturn_to_run[ptid] = rid
            except sqlite3.Error:
                pass

        # 3) Turn items grouped by run_id
        run_items: Dict[str, List[Tuple[str, str, Optional[float], Optional[float], Optional[float]]]] = {}
        if "orchestration_v2_projection_turn_items" in tables:
            try:
                icols = {r[1] for r in conn.execute('PRAGMA table_info("orchestration_v2_projection_turn_items")').fetchall()}
                status_expr = "status" if "status" in icols else "''"
                pt_expr = "provider_turn_id" if "provider_turn_id" in icols else "NULL"
                for r in conn.execute(
                    f"""
                    SELECT run_id, type, {status_expr} AS status, updated_at, {pt_expr} AS provider_turn_id,
                           json_extract(payload_json, '$.startedAt') AS started_at,
                           json_extract(payload_json, '$.completedAt') AS completed_at
                    FROM orchestration_v2_projection_turn_items
                    WHERE run_id IS NOT NULL
                    """
                ):
                    rid = str(r["run_id"] or "")
                    if not rid:
                        continue
                    if r["provider_turn_id"]:
                        pturn_to_run.setdefault(str(r["provider_turn_id"]), rid)
                    run_items.setdefault(rid, []).append(
                        (
                            str(r["type"] or ""),
                            str(r["status"] or "").lower(),
                            parse_ts(r["started_at"]),
                            parse_ts(r["completed_at"]),
                            parse_ts(r["updated_at"]),
                        )
                    )
            except sqlite3.Error:
                had_partial = True

        # 4) Runtime requests (for work exclusions AND strict waiting_human detection)
        thread_waits: Dict[str, List[Tuple[float, float, bool]]] = {}
        open_runtime_reqs: List[Dict[str, Any]] = []
        if "orchestration_v2_projection_runtime_requests" in tables:
            try:
                rcols = {r[1] for r in conn.execute('PRAGMA table_info("orchestration_v2_projection_runtime_requests")').fetchall()}
                has_kind_col = "kind" in rcols
                has_node_col = "node_id" in rcols
                has_pturn_col = "provider_turn_id" in rcols
                id_col = "runtime_request_id" if "runtime_request_id" in rcols else "rowid"
                sel = [
                    f"{id_col} AS req_id",
                    "thread_id",
                    "status",
                    "created_at",
                    "resolved_at",
                    ("kind" if has_kind_col else "NULL") + " AS kind",
                    ("node_id" if has_node_col else "NULL") + " AS node_id",
                    ("provider_turn_id" if has_pturn_col else "NULL") + " AS provider_turn_id",
                ]
                for r in conn.execute(f"SELECT {', '.join(sel)} FROM orchestration_v2_projection_runtime_requests"):
                    tid = str(r["thread_id"] or "")
                    ws = parse_ts(r["created_at"])
                    we = parse_ts(r["resolved_at"])
                    st_lower = str(r["status"] or "").strip().lower()
                    is_pending_legacy = (we is None) and st_lower not in ("resolved", "cancelled", "rejected")
                    if tid and ws is not None:
                        end_w = we if we is not None else now_ts
                        if end_w > ws:
                            thread_waits.setdefault(tid, []).append((ws, end_w, is_pending_legacy))

                    # Strict waiting_human eligibility (Spec 2.3)
                    if we is None and st_lower in T3_OPEN_WAIT_STATUSES and ws is not None and tid:
                        if has_kind_col:
                            raw_kind = str(r["kind"] or "").strip().lower()
                            if raw_kind not in T3_RUNTIME_REQ_KINDS:
                                continue
                            wait_kind = T3_RUNTIME_REQ_KINDS[raw_kind]
                        else:
                            wait_kind = "other"

                        req_id = str(r["req_id"] or "")
                        nid = str(r["node_id"] or "") if r["node_id"] else ""
                        ptid = str(r["provider_turn_id"] or "") if r["provider_turn_id"] else ""
                        direct_run = req_to_run.get(req_id) or (node_to_run.get(nid) if nid else None) or (
                            pturn_to_run.get(ptid) if ptid else None
                        )
                        open_runtime_reqs.append(
                            {
                                "thread_id": tid,
                                "created_at": ws,
                                "wait_kind": wait_kind,
                                "direct_run_id": direct_run,
                                "has_direct_ref": bool(nid or ptid),
                            }
                        )
            except sqlite3.Error:
                had_partial = True

        # 5) Runs (required core table)
        try:
            rows = conn.execute(
                """
                SELECT run_id, thread_id, provider, status, requested_at, completed_at,
                       json_extract(payload_json, '$.startedAt') AS started_at
                FROM orchestration_v2_projection_runs
                WHERE status NOT IN ('queued', 'rolled_back')
                """
            ).fetchall()
        except sqlite3.Error:
            if status_out is not None:
                status_out["work.t3"] = "unavailable"
            return segs

        # Pre-index unfinished runs per thread for strict single-run fallback association
        unfinished_by_thread: Dict[str, List[Tuple[str, float]]] = {}
        parsed_runs: List[Dict[str, Any]] = []
        for r in rows:
            start_ts = parse_ts(r["started_at"]) or parse_ts(r["requested_at"])
            if start_ts is None:
                continue
            end_ts = parse_ts(r["completed_at"])
            status = str(r["status"] or "").lower()
            thread_id = str(r["thread_id"] or "")
            run_id = str(r["run_id"] or "")
            is_live_candidate = (end_ts is None) and status in ("running", "starting", "preparing")
            if is_live_candidate:
                unfinished_by_thread.setdefault(thread_id, []).append((run_id, start_ts))
            parsed_runs.append(
                {
                    "row": r,
                    "run_id": run_id,
                    "thread_id": thread_id,
                    "start_ts": start_ts,
                    "end_ts": end_ts,
                    "status": status,
                    "is_live_candidate": is_live_candidate,
                }
            )

        # Map open runtime requests to specific unfinished run_ids
        run_open_waits: Dict[str, List[Tuple[float, str]]] = {}
        for req in open_runtime_reqs:
            drid = req["direct_run_id"]
            if drid:
                run_open_waits.setdefault(drid, []).append((req["created_at"], req["wait_kind"]))
            elif not req["has_direct_ref"]:
                cands = unfinished_by_thread.get(req["thread_id"], [])
                if len(cands) == 1:
                    cand_rid, cand_start = cands[0]
                    if cand_start <= req["created_at"] <= now_ts:
                        run_open_waits.setdefault(cand_rid, []).append((req["created_at"], req["wait_kind"]))

        for prun in parsed_runs:
            r = prun["row"]
            provider = str(r["provider"] or "").lower()
            if "antigravity" in provider:
                tool = "t3_antigravity"
            elif "codex" in provider:
                tool = "t3_codex"
            elif "claude" in provider:
                tool = "t3_claude"
            else:
                tool = "t3_other"

            start_ts = prun["start_ts"]
            end_ts = prun["end_ts"]
            status = prun["status"]
            thread_id = prun["thread_id"]
            run_id = prun["run_id"]
            is_live_candidate = prun["is_live_candidate"]
            label = titles.get(thread_id, "")

            # Structured paths
            pid = thread_project_id.get(thread_id, "")
            proj_root = project_workspace_root.get(pid) if pid else None
            wt_path = thread_worktree_path.get(thread_id)
            workspace_path = wt_path or proj_root
            project_path = proj_root or wt_path

            # Check if completed run is within either work cutoff OR 120s just_finished window
            in_just_finished_window = (
                status == "completed"
                and end_ts is not None
                and 0.0 <= (now_ts - end_ts) < JUST_FINISHED_WINDOW_SECONDS
            )
            if not is_live_candidate and not in_just_finished_window:
                if end_ts is None or end_ts < cutoff_ts or end_ts <= start_ts:
                    continue

            bound_end = now_ts if is_live_candidate else end_ts  # type: ignore[assignment]
            if bound_end is None or bound_end < start_ts:
                continue

            step_pts: List[float] = []
            extra_spans: List[Tuple[float, float]] = []
            exclusions: List[Tuple[float, float]] = []
            has_pending_human_wait = False
            open_waits_for_run: List[Tuple[float, str]] = list(run_open_waits.get(run_id, []))

            for ws, we, is_pending in thread_waits.get(thread_id, []):
                if we >= start_ts and ws <= bound_end:
                    exclusions.append((max(ws, start_ts), min(we, bound_end)))
                    if is_pending:
                        has_pending_human_wait = True

            for itype, istatus, isa, ica, iua in run_items.get(run_id, []):
                if itype in T3_HUMAN_WAIT_TYPES:
                    ws = isa or iua
                    we = ica or iua or (now_ts if is_live_candidate else None)
                    if ws is not None and we is not None and we > ws:
                        exclusions.append((max(ws, start_ts), min(we, bound_end)))
                    if is_live_candidate and ica is None and istatus not in T3_CLOSED_ITEM_STATUSES:
                        has_pending_human_wait = True
                        wait_since = ws if ws is not None else start_ts
                        open_waits_for_run.append((wait_since, T3_WAIT_ITEM_KIND[itype]))
                    continue

                if itype in T3_NON_AGENT_TYPES:
                    continue

                for t in (isa, ica, iua):
                    if t is not None and start_ts <= t <= bound_end:
                        step_pts.append(t)

                if itype in (
                    "command_execution",
                    "reasoning",
                    "dynamic_tool",
                    "file_change",
                    "file_search",
                    "web_search",
                    "subagent",
                    "compaction",
                ):
                    if isa is not None and ica is not None and ica > isa and start_ts <= isa and ica <= bound_end:
                        extra_spans.append((isa, ica))

            run_key = f"t3:{run_id}"
            built: List[WorkSegment] = []

            if is_live_candidate:
                last_hb = max(step_pts) if step_pts else start_ts
                has_strict_wait = bool(open_waits_for_run)
                is_actively_working = (
                    (not has_pending_human_wait)
                    and (not has_strict_wait)
                    and (0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS)
                )
                if is_actively_working:
                    step_pts.append(max(now_ts, last_hb + 0.5))
                if step_pts:
                    built = build_heartbeat_segments(
                        tool=tool,
                        turn_start=start_ts,
                        step_timestamps=step_pts,
                        extra_spans=extra_spans,
                        exclusions=exclusions,
                        run_key=run_key,
                        label=label,
                        is_active=is_actively_working,
                        workspace_path=workspace_path,
                        project_path=project_path,
                        source_id="work.t3",
                    )

                run_ai_sec = sum(s.duration for s in built)
                if sentinels_out is not None and not had_partial:
                    if has_strict_wait:
                        open_waits_for_run.sort(key=lambda x: x[0])
                        earliest_ts, earliest_kind = open_waits_for_run[0]
                        sentinels_out.append(
                            SentinelCandidate(
                                source_id="work.t3",
                                tool=tool,
                                run_key=run_key,
                                label=label,
                                state="waiting_human",
                                started_at=start_ts,
                                state_since_ts=earliest_ts,
                                expires_at=now_ts + SNAPSHOT_FRESH_SECONDS,
                                elapsed_seconds=run_ai_sec,
                                workspace_path=workspace_path,
                                project_path=project_path,
                                evidence_kind="explicit_human_request",
                                wait_kind=earliest_kind,
                            )
                        )
                    elif is_actively_working and built:
                        exp = min(last_hb + HEARTBEAT_GAP_SECONDS, now_ts + SNAPSHOT_FRESH_SECONDS)
                        sentinels_out.append(
                            SentinelCandidate(
                                source_id="work.t3",
                                tool=tool,
                                run_key=run_key,
                                label=label,
                                state="running",
                                started_at=start_ts,
                                state_since_ts=start_ts,
                                expires_at=exp,
                                elapsed_seconds=run_ai_sec,
                                workspace_path=workspace_path,
                                project_path=project_path,
                                evidence_kind="heartbeat",
                            )
                        )
            else:
                step_pts.append(bound_end)
                built = build_heartbeat_segments(
                    tool=tool,
                    turn_start=start_ts,
                    step_timestamps=step_pts,
                    extra_spans=extra_spans,
                    exclusions=exclusions,
                    run_key=run_key,
                    label=label,
                    is_active=False,
                    workspace_path=workspace_path,
                    project_path=project_path,
                    source_id="work.t3",
                )
                if in_just_finished_window and sentinels_out is not None and not had_partial:
                    run_ai_sec = sum(s.duration for s in built)
                    if run_ai_sec >= JUST_FINISHED_MIN_SECONDS:
                        sentinels_out.append(
                            SentinelCandidate(
                                source_id="work.t3",
                                tool=tool,
                                run_key=run_key,
                                label=label,
                                state="just_finished",
                                started_at=start_ts,
                                state_since_ts=end_ts,  # type: ignore[arg-type]
                                expires_at=end_ts + JUST_FINISHED_WINDOW_SECONDS,  # type: ignore[operator]
                                elapsed_seconds=run_ai_sec,
                                workspace_path=workspace_path,
                                project_path=project_path,
                                evidence_kind="explicit_success",
                            )
                        )

            for seg in built:
                if seg.end_ts >= cutoff_ts:
                    segs.append(seg)

        if status_out is not None:
            status_out["work.t3"] = "partial" if had_partial else "complete"
        if meta_status_out is not None and meta_failed:
            meta_status_out["work.t3"] = "partial"
    return segs


def parse_codex_file_checked(
    file_path: Path, now_ts: float
) -> Tuple[bool, List[WorkSegment], List[Dict[str, Any]], List[SentinelCandidate], bool, bool, Optional[os.stat_result]]:
    """Parse one Codex JSONL rollout file with integrity checks, paths, and sentinels."""
    res = read_jsonl_checked(file_path)
    if res.permission_error or res.missing:
        return False, [], [], [], True, res.identity_changed, None

    mtime = res.stat_result.st_mtime if res.stat_result is not None else 0.0
    is_fresh = (res.stat_result is not None) and ((now_ts - mtime) <= LIVE_STALE_SECONDS)

    segs: List[WorkSegment] = []
    completed_meta: List[Dict[str, Any]] = []
    sentinels: List[SentinelCandidate] = []

    turn_starts: Dict[str, float] = {}
    turn_steps: Dict[str, List[float]] = {}
    turn_spans: Dict[str, List[Tuple[float, float]]] = {}
    turn_cwds: Dict[str, Optional[str]] = {}
    completed_turns: set = set()
    active_turn_id: Optional[str] = None
    last_event_type = ""
    session_cwd: Optional[str] = None
    current_turn_cwd: Optional[str] = None
    session_label = ""

    def emit_turn(key: str, start_ts: float, end_ts: Optional[float], active: bool) -> List[WorkSegment]:
        steps = list(turn_steps.get(key, []))
        if end_ts is not None and end_ts >= start_ts:
            steps.append(end_ts)
        cwd = turn_cwds.get(key) or session_cwd
        lbl = Path(cwd).name if cwd else session_label
        built = build_heartbeat_segments(
            tool="codex",
            turn_start=start_ts,
            step_timestamps=steps,
            extra_spans=turn_spans.get(key),
            run_key=f"codex:{key}",
            label=lbl,
            is_active=active,
            workspace_path=cwd,
            project_path=cwd,
            source_id="work.codex",
        )
        segs.extend(built)
        return built

    for line_no, obj in res.records:
        otype = obj.get("type")
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        line_ts = parse_ts(obj.get("timestamp"))

        if otype == "session_meta":
            originator = str(payload.get("originator") or "")
            if originator == "T3 Code":
                return True, [], [], [], res.had_error, res.identity_changed, res.stat_result
            cwd = _norm_opt_path(payload.get("cwd"))
            if cwd:
                session_cwd = cwd
                session_label = Path(cwd).name
            continue

        if otype == "turn_context":
            tc_cwd = _norm_opt_path(payload.get("cwd"))
            if tc_cwd:
                current_turn_cwd = tc_cwd
                if active_turn_id and active_turn_id not in turn_cwds:
                    turn_cwds[active_turn_id] = tc_cwd
            continue

        if otype == "event_msg":
            ptype = str(payload.get("type") or "")
            last_event_type = ptype
            tid = str(payload.get("turn_id") or payload.get("root_turn_id") or "")

            if ptype == "task_started":
                ts = parse_ts(payload.get("started_at")) or line_ts
                if ts is not None:
                    key = tid or f"{file_path.stem}:L{line_no}"
                    turn_starts[key] = ts
                    turn_steps.setdefault(key, []).append(ts)
                    if current_turn_cwd or session_cwd:
                        turn_cwds[key] = current_turn_cwd or session_cwd
                    active_turn_id = key
            elif ptype == "item_completed":
                key = tid or active_turn_id
                if key and key in turn_starts:
                    isa = parse_ts(payload.get("started_at_ms"))
                    ica = parse_ts(payload.get("completed_at_ms"))
                    for t in (isa, ica, line_ts):
                        if t is not None:
                            turn_steps.setdefault(key, []).append(t)
                    if isa is not None and ica is not None and ica > isa:
                        turn_spans.setdefault(key, []).append((isa, ica))
            elif ptype == "task_complete":
                key = tid or active_turn_id or f"{file_path.stem}:L{line_no}"
                completed_turns.add(key)
                end_ts = parse_ts(payload.get("completed_at")) or line_ts
                start_ts = turn_starts.get(key) or parse_ts(payload.get("started_at"))
                if start_ts is None and end_ts is not None and payload.get("duration_ms") is not None:
                    try:
                        dur_s = float(payload["duration_ms"]) / 1000.0
                        if dur_s > 0:
                            start_ts = end_ts - dur_s
                    except (TypeError, ValueError):
                        pass
                if start_ts is not None and end_ts is not None and end_ts > start_ts:
                    if key not in turn_cwds and (current_turn_cwd or session_cwd):
                        turn_cwds[key] = current_turn_cwd or session_cwd
                    emit_turn(key, start_ts, end_ts, active=False)
                    completed_meta.append({"run_key": f"codex:{key}", "completed_ts": float(end_ts)})
                active_turn_id = None
            elif ptype in ("turn_aborted", "task_aborted"):
                key = tid or active_turn_id
                if key and key in turn_starts and key not in completed_turns:
                    completed_turns.add(key)
                    emit_turn(key, turn_starts[key], line_ts, active=False)
                active_turn_id = None
            elif line_ts is not None:
                key = tid or active_turn_id
                if key and key in turn_starts:
                    turn_steps.setdefault(key, []).append(line_ts)
        elif line_ts is not None and active_turn_id and active_turn_id in turn_starts:
            turn_steps.setdefault(active_turn_id, []).append(line_ts)

    for key, start_ts in turn_starts.items():
        if key in completed_turns:
            continue
        steps = turn_steps.get(key, [])
        last_hb = max(steps) if steps else start_ts
        if key == active_turn_id and is_fresh and last_event_type not in ("task_complete", "turn_aborted", "task_aborted"):
            if 0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS:
                built = emit_turn(key, start_ts, max(now_ts, last_hb + 0.5), active=True)
                if built:
                    cwd = turn_cwds.get(key) or session_cwd
                    lbl = Path(cwd).name if cwd else session_label
                    exp = min(mtime + LIVE_STALE_SECONDS, last_hb + HEARTBEAT_GAP_SECONDS, now_ts + SNAPSHOT_FRESH_SECONDS)
                    sentinels.append(
                        SentinelCandidate(
                            source_id="work.codex",
                            tool="codex",
                            run_key=f"codex:{key}",
                            label=lbl,
                            state="running",
                            started_at=start_ts,
                            state_since_ts=start_ts,
                            expires_at=exp,
                            elapsed_seconds=sum(s.duration for s in built),
                            workspace_path=cwd,
                            project_path=cwd,
                            evidence_kind="heartbeat",
                        )
                    )
                continue
        if steps:
            emit_turn(key, start_ts, None, active=False)

    return False, segs, completed_meta, sentinels, res.had_error, res.identity_changed, res.stat_result


def parse_codex_file(file_path: Path, now_ts: float) -> Tuple[bool, List[WorkSegment]]:
    skipped, segs, _, _, _, _, _ = parse_codex_file_checked(file_path, now_ts)
    return skipped, segs


def collect_codex(
    sessions_dir: Path,
    cutoff_ts: float,
    now_ts: float,
    cache: SegmentCache,
    *,
    sentinels_out: Optional[List[SentinelCandidate]] = None,
    status_out: Optional[Dict[str, str]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> List[WorkSegment]:
    can_scan, early = check_root_dir(sessions_dir, "work.codex", prev_sources or {})
    if not can_scan:
        if status_out is not None:
            status_out["work.codex"] = early or "not_configured"
        return []

    by_turn: Dict[str, List[WorkSegment]] = {}
    completed_by_turn: Dict[str, float] = {}
    running_by_turn: Dict[str, SentinelCandidate] = {}
    scan_floor = cutoff_ts - 86400.0

    files, dir_err = _walk_jsonl(sessions_dir)
    ok_count = 0
    err_count = 0

    for fpath in files:
        try:
            if fpath.stat().st_mtime < scan_floor:
                ok_count += 1
                continue
        except OSError:
            err_count += 1
            continue

        cached_entry = cache.get_entry(fpath, now_ts, source_id="work.codex")
        if cached_entry is not None:
            file_segs = cached_entry["segs"]
            file_completed = cached_entry["completed"]
            file_sentinels: List[SentinelCandidate] = []
        else:
            _, file_segs, file_completed, file_sentinels, had_err, changed, st_res = parse_codex_file_checked(
                fpath, now_ts
            )
            if had_err or changed:
                err_count += 1
                continue
            cache.put_entry(fpath, file_segs, now_ts, completed=file_completed, stat_result=st_res)

        ok_count += 1
        grouped: Dict[str, List[WorkSegment]] = {}
        for seg in file_segs:
            grouped.setdefault(seg.run_key, []).append(seg)

        comp_map = {
            str(item["run_key"]): float(item["completed_ts"])
            for item in file_completed
            if isinstance(item, dict) and item.get("run_key") and isinstance(item.get("completed_ts"), (int, float))
        }
        run_sent_map = {s.run_key: s for s in file_sentinels if s.state == "running"}

        for rkey, rsegs in grouped.items():
            prev = by_turn.get(rkey)
            cur_dur = sum(s.duration for s in rsegs)
            prev_dur = sum(s.duration for s in prev) if prev else -1.0
            cur_active = any(s.is_active for s in rsegs)
            prev_active = any(s.is_active for s in prev) if prev else False
            if prev is None or cur_dur > prev_dur or (cur_active and not prev_active):
                by_turn[rkey] = rsegs
                if rkey in comp_map:
                    completed_by_turn[rkey] = comp_map[rkey]
                elif cur_active:
                    completed_by_turn.pop(rkey, None)
                if rkey in run_sent_map:
                    running_by_turn[rkey] = run_sent_map[rkey]
                elif rkey in running_by_turn and not cur_active:
                    running_by_turn.pop(rkey, None)

    status = _file_scan_status(ok_count, err_count, dir_err)
    if status_out is not None:
        status_out["work.codex"] = status

    out: List[WorkSegment] = []
    for rkey, rsegs in by_turn.items():
        for seg in rsegs:
            if seg.end_ts >= cutoff_ts:
                out.append(seg)
        if sentinels_out is not None and status == "complete":
            if rkey in running_by_turn:
                sentinels_out.append(running_by_turn[rkey])
            elif rkey in completed_by_turn:
                cts = completed_by_turn[rkey]
                if 0.0 <= (now_ts - cts) < JUST_FINISHED_WINDOW_SECONDS:
                    total_dur = sum(s.duration for s in rsegs)
                    if total_dur >= JUST_FINISHED_MIN_SECONDS:
                        first_seg = min(rsegs, key=lambda s: s.start_ts)
                        sentinels_out.append(
                            SentinelCandidate(
                                source_id="work.codex",
                                tool="codex",
                                run_key=rkey,
                                label=first_seg.label,
                                state="just_finished",
                                started_at=first_seg.start_ts,
                                state_since_ts=cts,
                                expires_at=cts + JUST_FINISHED_WINDOW_SECONDS,
                                elapsed_seconds=total_dur,
                                workspace_path=first_seg.workspace_path,
                                project_path=first_seg.project_path,
                                evidence_kind="explicit_success",
                            )
                        )
    return out


def parse_workbuddy_file_checked(
    file_path: Path, now_ts: float
) -> Tuple[List[WorkSegment], List[SentinelCandidate], bool, bool, Optional[os.stat_result]]:
    res = read_jsonl_checked(file_path)
    if res.permission_error or res.missing:
        return [], [], True, res.identity_changed, None

    mtime = res.stat_result.st_mtime if res.stat_result is not None else 0.0
    is_fresh = (res.stat_result is not None) and ((now_ts - mtime) <= LIVE_STALE_SECONDS)

    segs: List[WorkSegment] = []
    sentinels: List[SentinelCandidate] = []
    cur_turn_start: Optional[float] = None
    cur_steps: List[float] = []
    last_event_kind = ""
    turn_idx = 0
    label = ""
    cwd_path: Optional[str] = None

    def flush(active: bool = False) -> Tuple[List[WorkSegment], str]:
        nonlocal cur_turn_start, cur_steps, turn_idx
        built: List[WorkSegment] = []
        rkey = ""
        if cur_turn_start is not None and cur_steps:
            turn_idx += 1
            rkey = f"wb:{file_path.stem}:{turn_idx}"
            built = build_heartbeat_segments(
                tool="workbuddy",
                turn_start=cur_turn_start,
                step_timestamps=cur_steps,
                run_key=rkey,
                label=label,
                is_active=active,
                workspace_path=cwd_path,
                project_path=cwd_path,
                source_id="work.workbuddy",
            )
            segs.extend(built)
        cur_turn_start = None
        cur_steps = []
        return built, rkey

    for _, obj in res.records:
        otype = obj.get("type")
        if not cwd_path and obj.get("cwd"):
            cwd_path = _norm_opt_path(obj.get("cwd"))
        if otype == "ai-title" and not label:
            t = str(obj.get("aiTitle") or obj.get("title") or "").strip()
            if t:
                label = t[:40]
            continue
        if not label and cwd_path:
            label = Path(cwd_path).name

        ts = parse_ts(obj.get("timestamp"))
        if ts is None:
            continue

        if otype == "message":
            role = obj.get("role")
            if role == "user":
                flush(active=False)
                cur_turn_start = ts
                cur_steps = []
                last_event_kind = "user"
            elif role == "assistant":
                if cur_turn_start is not None:
                    cur_steps.append(ts)
                last_event_kind = "assistant"
        elif otype in ("reasoning", "function_call", "function_call_result"):
            if cur_turn_start is not None:
                cur_steps.append(ts)
            last_event_kind = str(otype)

    if cur_turn_start is not None:
        start_saved = cur_turn_start
        last_hb = max(cur_steps) if cur_steps else cur_turn_start
        if is_fresh and last_event_kind in ("user", "reasoning", "function_call", "function_call_result"):
            if 0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS:
                cur_steps.append(now_ts)
                built, rkey = flush(active=True)
                if built:
                    exp = min(mtime + LIVE_STALE_SECONDS, last_hb + HEARTBEAT_GAP_SECONDS, now_ts + SNAPSHOT_FRESH_SECONDS)
                    sentinels.append(
                        SentinelCandidate(
                            source_id="work.workbuddy",
                            tool="workbuddy",
                            run_key=rkey,
                            label=label,
                            state="running",
                            started_at=start_saved,
                            state_since_ts=start_saved,
                            expires_at=exp,
                            elapsed_seconds=sum(s.duration for s in built),
                            workspace_path=cwd_path,
                            project_path=cwd_path,
                            evidence_kind="heartbeat",
                        )
                    )
            else:
                flush(active=False)
        else:
            flush(active=False)

    return segs, sentinels, res.had_error, res.identity_changed, res.stat_result


def parse_workbuddy_file(file_path: Path, now_ts: float) -> List[WorkSegment]:
    segs, _, _, _, _ = parse_workbuddy_file_checked(file_path, now_ts)
    return segs


def collect_workbuddy(
    projects_dir: Path,
    cutoff_ts: float,
    now_ts: float,
    cache: SegmentCache,
    *,
    sentinels_out: Optional[List[SentinelCandidate]] = None,
    status_out: Optional[Dict[str, str]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> List[WorkSegment]:
    can_scan, early = check_root_dir(projects_dir, "work.workbuddy", prev_sources or {})
    if not can_scan:
        if status_out is not None:
            status_out["work.workbuddy"] = early or "not_configured"
        return []

    out: List[WorkSegment] = []
    local_sentinels: List[SentinelCandidate] = []
    scan_floor = cutoff_ts - 86400.0
    files, dir_err = _walk_jsonl(projects_dir)
    ok_count = 0
    err_count = 0

    for fpath in files:
        try:
            if fpath.stat().st_mtime < scan_floor:
                ok_count += 1
                continue
        except OSError:
            err_count += 1
            continue
        cached = cache.get_entry(fpath, now_ts, source_id="work.workbuddy")
        if cached is not None:
            file_segs = cached["segs"]
        else:
            file_segs, fsents, had_err, changed, st_res = parse_workbuddy_file_checked(fpath, now_ts)
            if had_err or changed:
                err_count += 1
                continue
            cache.put_entry(fpath, file_segs, now_ts, stat_result=st_res)
            local_sentinels.extend(fsents)
        ok_count += 1
        for seg in file_segs:
            if seg.end_ts >= cutoff_ts:
                out.append(seg)

    status = _file_scan_status(ok_count, err_count, dir_err)
    if status_out is not None:
        status_out["work.workbuddy"] = status
    if sentinels_out is not None and status == "complete":
        sentinels_out.extend(local_sentinels)
    return out


def collect_hermes_db(
    db_path: Path,
    cutoff_ts: float,
    now_ts: float,
    profile_tag: str = "",
    *,
    sentinels_out: Optional[List[SentinelCandidate]] = None,
    db_status_out: Optional[List[str]] = None,
    meta_failed_out: Optional[List[bool]] = None,
) -> List[WorkSegment]:
    segs: List[WorkSegment] = []
    with sqlite_snapshot_with_status(db_path) as (conn, snap_status):
        if snap_status != "ok" or conn is None:
            if db_status_out is not None:
                db_status_out.append("unavailable")
            return segs
        scan_floor = cutoff_ts - 86400.0

        titles: Dict[str, str] = {}
        session_cwds: Dict[str, str] = {}
        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error:
            if db_status_out is not None:
                db_status_out.append("unavailable")
            return segs

        if "sessions" in tables:
            try:
                scols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")').fetchall()}
                sel = ["id"]
                for col in ("title", "display_name", "source", "cwd"):
                    if col in scols:
                        sel.append(col)
                for r in conn.execute(f"SELECT {', '.join(sel)} FROM sessions"):
                    sid = str(r["id"])
                    t_parts = [
                        r[c] for c in ("title", "display_name", "source") if c in scols and r[c]
                    ]
                    t = str(t_parts[0]).strip() if t_parts else ""
                    if profile_tag:
                        t = f"{profile_tag} · {t}" if t else profile_tag
                    titles[sid] = t[:44]
                    if "cwd" in scols and r["cwd"]:
                        cpath = _norm_opt_path(r["cwd"])
                        if cpath:
                            session_cwds[sid] = cpath
            except sqlite3.Error:
                if meta_failed_out is not None:
                    meta_failed_out.append(True)

        active_leases: Dict[str, Tuple[float, float]] = {}
        if "session_turn_leases" in tables:
            try:
                for r in conn.execute(
                    "SELECT conversation_id, acquired_at, expires_at FROM session_turn_leases WHERE expires_at > ?",
                    (now_ts,),
                ):
                    acq = parse_ts(r["acquired_at"])
                    exp = parse_ts(r["expires_at"])
                    if acq is not None and exp is not None and 0.0 <= now_ts - acq <= HEARTBEAT_GAP_SECONDS:
                        active_leases[str(r["conversation_id"])] = (acq, exp)
            except sqlite3.Error:
                pass

        try:
            rows = conn.execute(
                """
                SELECT session_id, role, timestamp
                FROM messages
                WHERE timestamp >= ? AND role IN ('user', 'assistant', 'tool')
                ORDER BY session_id ASC, timestamp ASC, id ASC
                """,
                (scan_floor,),
            ).fetchall()
        except sqlite3.Error:
            if db_status_out is not None:
                db_status_out.append("unavailable")
            return segs

        cur_sid: Optional[str] = None
        cur_turn_start: Optional[float] = None
        cur_steps: List[float] = []
        turn_idx = 0

        def flush(sid: Optional[str]) -> None:
            nonlocal cur_turn_start, cur_steps, turn_idx
            if sid and cur_turn_start is not None and cur_steps:
                turn_idx += 1
                cwd = session_cwds.get(sid)
                segs.extend(
                    build_heartbeat_segments(
                        tool="hermes",
                        turn_start=cur_turn_start,
                        step_timestamps=cur_steps,
                        run_key=f"hermes:{profile_tag}:{sid}:{turn_idx}",
                        label=titles.get(sid, profile_tag),
                        is_active=False,
                        workspace_path=cwd,
                        project_path=cwd,
                        source_id="work.hermes",
                    )
                )
            cur_turn_start = None
            cur_steps = []

        for r in rows:
            sid = str(r["session_id"])
            if sid != cur_sid:
                flush(cur_sid)
                cur_sid = sid
                turn_idx = 0
            ts = parse_ts(r["timestamp"])
            if ts is None:
                continue
            role = str(r["role"] or "")
            if role == "user":
                flush(cur_sid)
                cur_turn_start = ts
                cur_steps = []
            elif role in ("assistant", "tool"):
                if cur_turn_start is not None:
                    cur_steps.append(ts)

        flush(cur_sid)

        for conv_id, (acq, lease_exp) in active_leases.items():
            cwd = session_cwds.get(conv_id)
            lbl = titles.get(conv_id, profile_tag or "Hermes")
            rkey = f"hermes-lease:{profile_tag}:{conv_id}"
            seg = WorkSegment(
                tool="hermes",
                start_ts=acq,
                end_ts=max(now_ts, acq + 0.5),
                is_active=True,
                run_key=rkey,
                label=lbl,
                is_turn_head=True,
                workspace_path=cwd,
                project_path=cwd,
                source_id="work.hermes",
            )
            segs.append(seg)
            if sentinels_out is not None:
                exp = min(lease_exp, acq + HEARTBEAT_GAP_SECONDS, now_ts + SNAPSHOT_FRESH_SECONDS)
                sentinels_out.append(
                    SentinelCandidate(
                        source_id="work.hermes",
                        tool="hermes",
                        run_key=rkey,
                        label=lbl,
                        state="running",
                        started_at=acq,
                        state_since_ts=acq,
                        expires_at=exp,
                        elapsed_seconds=seg.duration,
                        workspace_path=cwd,
                        project_path=cwd,
                        evidence_kind="heartbeat",
                    )
                )

        if db_status_out is not None:
            db_status_out.append("complete")

    return [s for s in segs if s.end_ts >= cutoff_ts]


def collect_hermes(
    hermes_root: Path,
    cutoff_ts: float,
    now_ts: float,
    *,
    sentinels_out: Optional[List[SentinelCandidate]] = None,
    status_out: Optional[Dict[str, str]] = None,
    meta_status_out: Optional[Dict[str, str]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> List[WorkSegment]:
    can_scan, early = check_root_dir(hermes_root, "work.hermes", prev_sources or {})
    if not can_scan:
        if status_out is not None:
            status_out["work.hermes"] = early or "not_configured"
        return []

    out: List[WorkSegment] = []
    local_sentinels: List[SentinelCandidate] = []
    db_statuses: List[str] = []
    meta_failed: List[bool] = []
    dir_err = 0

    main_db = hermes_root / "state.db"
    if main_db.exists():
        out.extend(
            collect_hermes_db(
                main_db, cutoff_ts, now_ts, "",
                sentinels_out=local_sentinels, db_status_out=db_statuses, meta_failed_out=meta_failed,
            )
        )
    profiles_dir = hermes_root / "profiles"
    if profiles_dir.exists():
        try:
            for pdir in sorted(profiles_dir.iterdir()):
                if not pdir.is_dir():
                    continue
                pdb = pdir / "state.db"
                if pdb.exists():
                    out.extend(
                        collect_hermes_db(
                            pdb, cutoff_ts, now_ts, pdir.name,
                            sentinels_out=local_sentinels, db_status_out=db_statuses, meta_failed_out=meta_failed,
                        )
                    )
        except OSError:
            dir_err += 1

    if dir_err > 0:
        db_statuses.append("unavailable")
    status = "complete" if not db_statuses else combine_statuses(db_statuses)
    if status_out is not None:
        status_out["work.hermes"] = status
    if meta_status_out is not None and meta_failed:
        meta_status_out["work.hermes"] = "partial"
    if sentinels_out is not None and status == "complete":
        sentinels_out.extend(local_sentinels)
    return out


def parse_antigravity_transcript_checked(
    file_path: Path,
    now_ts: float,
    *,
    is_cli: bool = False,
    source_id: str = "work.antigravity_desktop",
) -> Tuple[List[WorkSegment], List[SentinelCandidate], Dict[str, Any], bool, bool, Optional[os.stat_result]]:
    """Parse Antigravity Desktop or CLI transcript.jsonl using full conv_id and source_rank."""
    res = read_jsonl_checked(file_path)
    empty_rank = {"latest_event_ts": 0.0, "valid_record_count": 0}
    if res.permission_error or res.missing:
        return [], [], empty_rank, True, res.identity_changed, None

    mtime = res.stat_result.st_mtime if res.stat_result is not None else 0.0
    is_fresh = (res.stat_result is not None) and ((now_ts - mtime) <= LIVE_STALE_SECONDS)

    segs: List[WorkSegment] = []
    sentinels: List[SentinelCandidate] = []
    cur_turn_start: Optional[float] = None
    cur_steps: List[float] = []
    last_is_user = False
    last_non_sys_source = ""
    last_non_sys_status = ""
    last_non_sys_is_user = False
    turn_idx = 0
    full_conv_id = file_path.parent.parent.parent.name
    latest_event_ts = 0.0
    valid_record_count = 0

    def flush(active: bool = False) -> Tuple[List[WorkSegment], str]:
        nonlocal cur_turn_start, cur_steps, turn_idx
        built: List[WorkSegment] = []
        rkey = ""
        if cur_turn_start is not None and cur_steps:
            turn_idx += 1
            rkey = f"agy:{full_conv_id}:{turn_idx}"
            built = build_heartbeat_segments(
                tool="antigravity",
                turn_start=cur_turn_start,
                step_timestamps=cur_steps,
                run_key=rkey,
                label="Antigravity",
                is_active=active,
                source_id=source_id,
            )
            segs.extend(built)
        cur_turn_start = None
        cur_steps = []
        return built, rkey

    for _, obj in res.records:
        ts = parse_ts(obj.get("created_at"))
        if ts is None:
            continue
        valid_record_count += 1
        if ts > latest_event_ts:
            latest_event_ts = ts

        otype = str(obj.get("type") or "")
        source = str(obj.get("source") or "")
        status_str = str(obj.get("status") or "").upper()
        is_user = (otype == "USER_INPUT") or (source == "USER_EXPLICIT")
        is_system = (source.upper() == "SYSTEM") or otype.upper().startswith("SYSTEM")

        if not is_system:
            last_non_sys_source = source.upper()
            last_non_sys_status = status_str
            last_non_sys_is_user = is_user

        if is_user:
            flush(active=False)
            cur_turn_start = ts
            cur_steps = []
            last_is_user = True
        else:
            if cur_turn_start is not None:
                cur_steps.append(ts)
            last_is_user = False

    if cur_turn_start is not None:
        start_saved = cur_turn_start
        last_hb = max(cur_steps) if cur_steps else cur_turn_start
        within_hb = 0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS
        active_evidence: Optional[str] = None
        if is_fresh and within_hb:
            if is_cli:
                if last_non_sys_source == "MODEL" and last_non_sys_status == "RUNNING":
                    active_evidence = "explicit_running"
                elif last_non_sys_is_user and last_non_sys_status not in CLI_TERMINAL_STATUSES:
                    active_evidence = "heartbeat"
            else:
                if last_is_user:
                    active_evidence = "heartbeat"

        if active_evidence is not None:
            cur_steps.append(max(now_ts, last_hb + 0.5))
            built, rkey = flush(active=True)
            if built:
                exp = min(mtime + LIVE_STALE_SECONDS, last_hb + HEARTBEAT_GAP_SECONDS, now_ts + SNAPSHOT_FRESH_SECONDS)
                sentinels.append(
                    SentinelCandidate(
                        source_id=source_id,
                        tool="antigravity",
                        run_key=rkey,
                        label="Antigravity",
                        state="running",
                        started_at=start_saved,
                        state_since_ts=start_saved,
                        expires_at=exp,
                        elapsed_seconds=sum(s.duration for s in built),
                        evidence_kind=active_evidence,
                    )
                )
        else:
            flush(active=False)

    source_rank = {"latest_event_ts": float(latest_event_ts), "valid_record_count": int(valid_record_count)}
    return segs, sentinels, source_rank, res.had_error, res.identity_changed, res.stat_result


def parse_antigravity_transcript(file_path: Path, now_ts: float) -> List[WorkSegment]:
    segs, _, _, _, _, _ = parse_antigravity_transcript_checked(file_path, now_ts, is_cli=False)
    return segs


def _scan_antigravity_root(
    brain_dir: Path,
    source_id: str,
    is_cli: bool,
    cutoff_ts: float,
    now_ts: float,
    cache: SegmentCache,
    prev_sources: Dict[str, Any],
) -> Tuple[str, Dict[str, Tuple[Dict[str, Any], List[WorkSegment], List[SentinelCandidate]]]]:
    can_scan, early = check_root_dir(brain_dir, source_id, prev_sources)
    if not can_scan:
        return early or "not_configured", {}

    by_conv: Dict[str, Tuple[Dict[str, Any], List[WorkSegment], List[SentinelCandidate]]] = {}
    scan_floor = cutoff_ts - 86400.0
    ok_count = 0
    err_count = 0
    dir_err = 0

    try:
        entries = list(brain_dir.iterdir())
    except OSError:
        return "unavailable", {}

    for conv_dir in entries:
        try:
            if not conv_dir.is_dir() or conv_dir.name.startswith("."):
                continue
            logs = conv_dir / ".system_generated" / "logs"
            tpath = logs / "transcript.jsonl"
            if not tpath.is_file():
                tpath = logs / "transcript_full.jsonl"
            if not tpath.is_file():
                continue
            if tpath.stat().st_mtime < scan_floor:
                ok_count += 1
                continue
        except OSError:
            err_count += 1
            continue

        cached = cache.get_entry(tpath, now_ts, source_id=source_id, require_source_rank=True)
        if cached is not None:
            file_segs = cached["segs"]
            fsents: List[SentinelCandidate] = []
            srank = cached["source_rank"]
        else:
            file_segs, fsents, srank, had_err, changed, st_res = parse_antigravity_transcript_checked(
                tpath, now_ts, is_cli=is_cli, source_id=source_id
            )
            if had_err or changed:
                err_count += 1
                continue
            cache.put_entry(tpath, file_segs, now_ts, source_rank=srank, stat_result=st_res)

        ok_count += 1
        by_conv[conv_dir.name] = (srank, file_segs, fsents)

    return _file_scan_status(ok_count, err_count, dir_err), by_conv


def collect_antigravity_sources(
    desktop_brain_dir: Path,
    cli_brain_dir: Path,
    cutoff_ts: float,
    now_ts: float,
    cache: SegmentCache,
    *,
    sentinels_out: Optional[List[SentinelCandidate]] = None,
    status_out: Optional[Dict[str, str]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> List[WorkSegment]:
    """Collect Antigravity Desktop and CLI, deduplicating shared full conv_id by source_rank."""
    prev = prev_sources or {}
    desk_status, desk_convs = _scan_antigravity_root(
        desktop_brain_dir, "work.antigravity_desktop", False, cutoff_ts, now_ts, cache, prev
    )
    cli_status, cli_convs = _scan_antigravity_root(
        cli_brain_dir, "work.antigravity_cli", True, cutoff_ts, now_ts, cache, prev
    )

    if status_out is not None:
        status_out["work.antigravity_desktop"] = desk_status
        status_out["work.antigravity_cli"] = cli_status

    all_conv_ids = set(desk_convs.keys()) | set(cli_convs.keys())
    out: List[WorkSegment] = []

    for cid in sorted(all_conv_ids):
        d_item = desk_convs.get(cid)
        c_item = cli_convs.get(cid)
        if d_item is not None and c_item is not None:
            d_rank = (float(d_item[0]["latest_event_ts"]), int(d_item[0]["valid_record_count"]), 1)
            c_rank = (float(c_item[0]["latest_event_ts"]), int(c_item[0]["valid_record_count"]), 0)
            chosen_is_desk = d_rank >= c_rank
            chosen = d_item if chosen_is_desk else c_item
            chosen_status = desk_status if chosen_is_desk else cli_status
        elif d_item is not None:
            chosen = d_item
            chosen_status = desk_status
        else:
            chosen = c_item  # type: ignore[assignment]
            chosen_status = cli_status

        _, file_segs, fsents = chosen
        for seg in file_segs:
            if seg.end_ts >= cutoff_ts:
                out.append(seg)
        if sentinels_out is not None and chosen_status == "complete":
            sentinels_out.extend(fsents)

    return out


def collect_antigravity_standalone(brain_dir: Path, cutoff_ts: float, now_ts: float, cache: SegmentCache) -> List[WorkSegment]:
    _, convs = _scan_antigravity_root(brain_dir, "work.antigravity_desktop", False, cutoff_ts, now_ts, cache, {})
    out: List[WorkSegment] = []
    for _, file_segs, _ in convs.values():
        for seg in file_segs:
            if seg.end_ts >= cutoff_ts:
                out.append(seg)
    return out


def parse_claude_code_file_checked(
    file_path: Path, now_ts: float
) -> Tuple[List[WorkSegment], List[SentinelCandidate], bool, bool, Optional[os.stat_result]]:
    res = read_jsonl_checked(file_path)
    if res.permission_error or res.missing:
        return [], [], True, res.identity_changed, None

    mtime = res.stat_result.st_mtime if res.stat_result is not None else 0.0
    is_fresh = (res.stat_result is not None) and ((now_ts - mtime) <= LIVE_STALE_SECONDS)

    step_segs: List[WorkSegment] = []
    exact_fallback: List[WorkSegment] = []
    sentinels: List[SentinelCandidate] = []

    cur_turn_start: Optional[float] = None
    cur_steps: List[float] = []
    turn_idx = 0
    label = ""
    cwd_path: Optional[str] = None

    def flush(active: bool = False) -> Tuple[List[WorkSegment], str]:
        nonlocal cur_turn_start, cur_steps, turn_idx
        built: List[WorkSegment] = []
        rkey = ""
        if cur_turn_start is not None and cur_steps:
            turn_idx += 1
            rkey = f"cc:{file_path.stem}:{turn_idx}"
            built = build_heartbeat_segments(
                tool="claude_code",
                turn_start=cur_turn_start,
                step_timestamps=cur_steps,
                run_key=rkey,
                label=label,
                is_active=active,
                workspace_path=cwd_path,
                project_path=cwd_path,
                source_id="work.claude_code",
            )
            step_segs.extend(built)
        cur_turn_start = None
        cur_steps = []
        return built, rkey

    for line_no, obj in res.records:
        if not cwd_path and obj.get("cwd"):
            cwd_path = _norm_opt_path(obj.get("cwd"))
        if not label and cwd_path:
            label = Path(cwd_path).name
        ts = parse_ts(obj.get("timestamp"))
        otype = str(obj.get("type") or "")

        if otype == "system" and obj.get("subtype") == "turn_duration" and ts is not None:
            dur_ms = obj.get("durationMs")
            try:
                dur_s = float(dur_ms) / 1000.0
            except (TypeError, ValueError):
                dur_s = 0.0
            if dur_s >= MIN_TURN_SECONDS:
                capped = min(dur_s, SINGLE_STEP_CAP_SECONDS)
                exact_fallback.append(
                    WorkSegment(
                        tool="claude_code",
                        start_ts=ts - capped,
                        end_ts=ts,
                        is_active=False,
                        run_key=f"cc-exact:{file_path.stem}:{line_no}",
                        label=label,
                        is_turn_head=True,
                        workspace_path=cwd_path,
                        project_path=cwd_path,
                        source_id="work.claude_code",
                    )
                )
            continue

        if ts is None:
            continue
        if otype == "user":
            msg = obj.get("message")
            is_tool_result = False
            if isinstance(msg, dict) and isinstance(msg.get("content"), list):
                if all(isinstance(b, dict) and b.get("type") == "tool_result" for b in msg["content"] if b):
                    is_tool_result = True
            if is_tool_result:
                if cur_turn_start is not None:
                    cur_steps.append(ts)
            else:
                flush(active=False)
                cur_turn_start = ts
                cur_steps = []
        elif otype in ("assistant", "progress"):
            if cur_turn_start is not None:
                cur_steps.append(ts)

    if cur_turn_start is not None:
        start_saved = cur_turn_start
        last_hb = max(cur_steps) if cur_steps else cur_turn_start
        if is_fresh and 0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS:
            cur_steps.append(now_ts)
            built, rkey = flush(active=True)
            if built:
                exp = min(mtime + LIVE_STALE_SECONDS, last_hb + HEARTBEAT_GAP_SECONDS, now_ts + SNAPSHOT_FRESH_SECONDS)
                sentinels.append(
                    SentinelCandidate(
                        source_id="work.claude_code",
                        tool="claude_code",
                        run_key=rkey,
                        label=label,
                        state="running",
                        started_at=start_saved,
                        state_since_ts=start_saved,
                        expires_at=exp,
                        elapsed_seconds=sum(s.duration for s in built),
                        workspace_path=cwd_path,
                        project_path=cwd_path,
                        evidence_kind="heartbeat",
                    )
                )
        else:
            flush(active=False)

    final_segs = step_segs if step_segs else exact_fallback
    return final_segs, sentinels, res.had_error, res.identity_changed, res.stat_result


def parse_claude_code_file(file_path: Path, now_ts: float) -> List[WorkSegment]:
    segs, _, _, _, _ = parse_claude_code_file_checked(file_path, now_ts)
    return segs


def collect_claude_code(
    projects_dir: Path,
    cutoff_ts: float,
    now_ts: float,
    cache: SegmentCache,
    *,
    sentinels_out: Optional[List[SentinelCandidate]] = None,
    status_out: Optional[Dict[str, str]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> List[WorkSegment]:
    can_scan, early = check_root_dir(projects_dir, "work.claude_code", prev_sources or {})
    if not can_scan:
        if status_out is not None:
            status_out["work.claude_code"] = early or "not_configured"
        return []

    out: List[WorkSegment] = []
    local_sentinels: List[SentinelCandidate] = []
    scan_floor = cutoff_ts - 86400.0
    files, dir_err = _walk_jsonl(projects_dir, skip_substr="t3code-claude-title-")
    ok_count = 0
    err_count = 0

    for fpath in files:
        try:
            if fpath.stat().st_mtime < scan_floor:
                ok_count += 1
                continue
        except OSError:
            err_count += 1
            continue
        cached = cache.get_entry(fpath, now_ts, source_id="work.claude_code")
        if cached is not None:
            file_segs = cached["segs"]
        else:
            file_segs, fsents, had_err, changed, st_res = parse_claude_code_file_checked(fpath, now_ts)
            if had_err or changed:
                err_count += 1
                continue
            cache.put_entry(fpath, file_segs, now_ts, stat_result=st_res)
            local_sentinels.extend(fsents)
        ok_count += 1
        for seg in file_segs:
            if seg.end_ts >= cutoff_ts:
                out.append(seg)

    status = _file_scan_status(ok_count, err_count, dir_err)
    if status_out is not None:
        status_out["work.claude_code"] = status
    if sentinels_out is not None and status == "complete":
        sentinels_out.extend(local_sentinels)
    return out


def parse_pi_file_checked(file_path: Path) -> Tuple[List[WorkSegment], bool, bool, Optional[os.stat_result]]:
    res = read_jsonl_checked(file_path)
    if res.permission_error or res.missing:
        return [], True, res.identity_changed, None

    segs: List[WorkSegment] = []
    cur_turn_start: Optional[float] = None
    cur_steps: List[float] = []
    turn_idx = 0
    label = ""
    cwd_path: Optional[str] = None

    def flush() -> None:
        nonlocal cur_turn_start, cur_steps, turn_idx
        if cur_turn_start is not None and cur_steps:
            turn_idx += 1
            segs.extend(
                build_heartbeat_segments(
                    tool="pi",
                    turn_start=cur_turn_start,
                    step_timestamps=cur_steps,
                    run_key=f"pi:{file_path.stem}:{turn_idx}",
                    label=label,
                    is_active=False,
                    workspace_path=cwd_path,
                    project_path=cwd_path,
                    source_id="work.pi",
                )
            )
        cur_turn_start = None
        cur_steps = []

    for _, obj in res.records:
        otype = obj.get("type")
        if otype == "session" and not cwd_path and obj.get("cwd"):
            cwd_path = _norm_opt_path(obj.get("cwd"))
            if cwd_path:
                label = Path(cwd_path).name
        if otype != "message":
            continue
        ts = parse_ts(obj.get("timestamp"))
        msg = obj.get("message")
        if ts is None or not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "")
        if role == "user":
            flush()
            cur_turn_start = ts
            cur_steps = []
        elif role in ("assistant", "toolResult", "tool"):
            if cur_turn_start is not None:
                cur_steps.append(ts)
    flush()
    return segs, res.had_error, res.identity_changed, res.stat_result


def parse_pi_file(file_path: Path) -> List[WorkSegment]:
    segs, _, _, _ = parse_pi_file_checked(file_path)
    return segs


def collect_pi(
    sessions_dir: Path,
    cutoff_ts: float,
    now_ts: float,
    cache: SegmentCache,
    *,
    status_out: Optional[Dict[str, str]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> List[WorkSegment]:
    can_scan, early = check_root_dir(sessions_dir, "work.pi", prev_sources or {})
    if not can_scan:
        if status_out is not None:
            status_out["work.pi"] = early or "not_configured"
        return []

    out: List[WorkSegment] = []
    scan_floor = cutoff_ts - 86400.0
    files, dir_err = _walk_jsonl(sessions_dir)
    ok_count = 0
    err_count = 0

    for fpath in files:
        try:
            if fpath.stat().st_mtime < scan_floor:
                ok_count += 1
                continue
        except OSError:
            err_count += 1
            continue
        cached = cache.get_entry(fpath, now_ts, source_id="work.pi")
        if cached is not None:
            file_segs = cached["segs"]
        else:
            file_segs, had_err, changed, st_res = parse_pi_file_checked(fpath)
            if had_err or changed:
                err_count += 1
                continue
            cache.put_entry(fpath, file_segs, now_ts, stat_result=st_res)
        ok_count += 1
        for seg in file_segs:
            if seg.end_ts >= cutoff_ts:
                out.append(seg)

    if status_out is not None:
        status_out["work.pi"] = _file_scan_status(ok_count, err_count, dir_err)
    return out


def collect_opencode(
    db_path: Path,
    cutoff_ts: float,
    *,
    status_out: Optional[Dict[str, str]] = None,
    meta_status_out: Optional[Dict[str, str]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> List[WorkSegment]:
    segs: List[WorkSegment] = []
    with sqlite_snapshot_with_status(db_path) as (conn, snap_status):
        if snap_status == "missing":
            if status_out is not None:
                status_out["work.opencode"] = resolve_missing_source_status("work.opencode", prev_sources or {})
            return segs
        if snap_status != "ok" or conn is None:
            if status_out is not None:
                status_out["work.opencode"] = "unavailable"
            return segs

        meta_failed = False
        session_dir: Dict[str, str] = {}
        session_proj: Dict[str, str] = {}
        project_wt: Dict[str, str] = {}

        try:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        except sqlite3.Error:
            if status_out is not None:
                status_out["work.opencode"] = "unavailable"
            return segs

        if "project" in tables:
            try:
                pcols = {r[1] for r in conn.execute('PRAGMA table_info("project")').fetchall()}
                if "id" in pcols and "worktree" in pcols:
                    for r in conn.execute("SELECT id, worktree FROM project"):
                        wt = _norm_opt_path(r["worktree"])
                        if r["id"] and wt:
                            project_wt[str(r["id"])] = wt
            except sqlite3.Error:
                meta_failed = True

        if "session" in tables:
            try:
                scols = {r[1] for r in conn.execute('PRAGMA table_info("session")').fetchall()}
                sel = ["id"]
                if "directory" in scols:
                    sel.append("directory")
                if "project_id" in scols:
                    sel.append("project_id")
                for r in conn.execute(f"SELECT {', '.join(sel)} FROM session"):
                    sid = str(r["id"] or "")
                    if not sid:
                        continue
                    if "directory" in scols and r["directory"]:
                        dpath = _norm_opt_path(r["directory"])
                        if dpath:
                            session_dir[sid] = dpath
                    if "project_id" in scols and r["project_id"]:
                        session_proj[sid] = str(r["project_id"])
            except sqlite3.Error:
                meta_failed = True

        try:
            rows = conn.execute(
                "SELECT session_id, time_created, data FROM message ORDER BY session_id ASC, time_created ASC"
            ).fetchall()
        except sqlite3.Error:
            if status_out is not None:
                status_out["work.opencode"] = "unavailable"
            return segs

        cur_sid: Optional[str] = None
        cur_turn_start: Optional[float] = None
        cur_steps: List[float] = []
        turn_idx = 0

        def flush(sid: Optional[str]) -> None:
            nonlocal cur_turn_start, cur_steps, turn_idx
            if sid and cur_turn_start is not None and cur_steps:
                turn_idx += 1
                sdir = session_dir.get(sid)
                pwt = project_wt.get(session_proj.get(sid, ""))
                ws_path = sdir or pwt
                pr_path = pwt or sdir
                segs.extend(
                    build_heartbeat_segments(
                        tool="opencode",
                        turn_start=cur_turn_start,
                        step_timestamps=cur_steps,
                        run_key=f"oc:{sid}:{turn_idx}",
                        label=Path(ws_path).name if ws_path else "OpenCode",
                        is_active=False,
                        workspace_path=ws_path,
                        project_path=pr_path,
                        source_id="work.opencode",
                    )
                )
            cur_turn_start = None
            cur_steps = []

        for r in rows:
            sid = str(r["session_id"])
            if sid != cur_sid:
                flush(cur_sid)
                cur_sid = sid
                turn_idx = 0
            ts = parse_ts(r["time_created"])
            if ts is None or ts < cutoff_ts - 86400.0:
                continue
            try:
                mdata = json.loads(r["data"])
            except Exception:
                continue
            role = str(mdata.get("role") or "")
            if role == "user":
                flush(cur_sid)
                cur_turn_start = ts
                cur_steps = []
            elif role in ("assistant", "tool"):
                if cur_turn_start is not None:
                    cur_steps.append(ts)
        flush(cur_sid)

        if status_out is not None:
            status_out["work.opencode"] = "complete"
        if meta_status_out is not None and meta_failed:
            meta_status_out["work.opencode"] = "partial"

    return [s for s in segs if s.end_ts >= cutoff_ts]


def read_tokei_reference(tokei_path: Path) -> Dict[str, Any]:
    if not tokei_path.is_file():
        return {"available": False, "today_tokens": 0, "today_cost_usd": 0.0}
    try:
        data = json.loads(tokei_path.read_text(encoding="utf-8"))
    except Exception:
        return {"available": False, "today_tokens": 0, "today_cost_usd": 0.0}

    total_tok = 0
    total_cost = 0.0
    if isinstance(data, dict):
        for _, info in data.items():
            if not isinstance(info, dict):
                continue
            today = (info.get("ranges") or {}).get("today")
            if not isinstance(today, dict):
                continue
            for k in ("in", "out", "cr", "cw", "thoughts"):
                v = today.get(k)
                if isinstance(v, (int, float)) and v > 0:
                    total_tok += int(v)
            c = today.get("cost")
            if isinstance(c, (int, float)) and c > 0:
                total_cost += float(c)
    return {
        "available": True,
        "today_tokens": total_tok,
        "today_cost_usd": round(total_cost, 2),
    }


# ---------------------------------------------------------------------------
# Aggregation, Sentinel Builder & Project Work Attribution
# ---------------------------------------------------------------------------


def _build_project_work_days(segments: Iterable[WorkSegment]) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Aggregate WorkSegments into {original_source_path: {YYYY-MM-DD: {agent_seconds, tools}}}."""
    acc: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for seg in segments:
        raw_path = seg.workspace_path or seg.project_path
        if not raw_path:
            continue
        for day_str, chunk in split_segment_by_biz_day(seg):
            dur = chunk.duration
            if dur <= 0:
                continue
            p_days = acc.setdefault(raw_path, {})
            d_ent = p_days.setdefault(day_str, {"agent_seconds": 0.0, "tools": {}})
            d_ent["agent_seconds"] += dur
            t_ent = d_ent["tools"].setdefault(chunk.tool, {"agent_seconds": 0.0})
            t_ent["agent_seconds"] += dur

    out: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for ppath, dmap in acc.items():
        out_days: Dict[str, Dict[str, Any]] = {}
        for dk, ent in dmap.items():
            sec = round(ent["agent_seconds"], 1)
            if sec <= 0:
                continue
            tools_clean = {
                tk: {"agent_seconds": round(tv["agent_seconds"], 1)}
                for tk, tv in ent["tools"].items()
                if round(tv["agent_seconds"], 1) > 0
            }
            out_days[dk] = {"agent_seconds": sec, "tools": tools_clean}
        if out_days:
            out[ppath] = out_days
    return out


_SENTINEL_STATE_RANK = {
    "waiting_human": 0,
    "running": 1,
    "just_finished": 2,
}


def _finalize_sentinel_tasks(
    candidates: List[SentinelCandidate],
    segments: List[WorkSegment],
    now_ts: float,
    healthy_sources: Optional[set] = None,
    resolver: Optional[WorktreeResolver] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Produce ordered (sentinel_tasks, active_agents) with stable SHA-256 task IDs."""
    # Pre-compute total effective AI seconds per run_key from segments
    seg_dur_by_run: Dict[str, float] = {}
    seg_start_by_run: Dict[str, float] = {}
    for seg in segments:
        if not seg.run_key:
            continue
        seg_dur_by_run[seg.run_key] = seg_dur_by_run.get(seg.run_key, 0.0) + seg.duration
        prev_s = seg_start_by_run.get(seg.run_key)
        if prev_s is None or seg.start_ts < prev_s:
            seg_start_by_run[seg.run_key] = seg.start_ts

    # One row per run_key; prefer higher-priority state if duplicated
    best_by_run: Dict[str, SentinelCandidate] = {}
    for cand in candidates:
        if healthy_sources is not None and cand.source_id not in healthy_sources:
            continue
        if now_ts >= cand.expires_at:
            continue
        prev = best_by_run.get(cand.run_key)
        if prev is None or _SENTINEL_STATE_RANK.get(cand.state, 99) < _SENTINEL_STATE_RANK.get(prev.state, 99):
            best_by_run[cand.run_key] = cand

    tasks: List[Dict[str, Any]] = []
    running_agents: List[Dict[str, Any]] = []

    for cand in best_by_run.values():
        meta = TOOL_META.get(cand.tool, {"name": cand.tool, "short": cand.tool, "color": "#94A3B8"})
        tid = make_sentinel_id(cand.source_id, cand.run_key)
        elapsed_ai = cand.elapsed_seconds if cand.elapsed_seconds is not None else seg_dur_by_run.get(cand.run_key, 0.0)

        ws_path = cand.workspace_path
        pr_path = cand.project_path
        if ws_path and resolver is not None:
            wt = resolver.resolve(ws_path)
            if wt.verified_git and wt.canonical_root:
                pr_path = wt.canonical_root

        ev: Dict[str, Any] = {"kind": cand.evidence_kind}
        if cand.state == "waiting_human" and cand.wait_kind:
            ev["wait_kind"] = cand.wait_kind

        task_obj: Dict[str, Any] = {
            "id": tid,
            "source_id": cand.source_id,
            "tool": cand.tool,
            "display_name": meta["name"],
            "short_name": meta["short"],
            "color": meta["color"],
            "label": cand.label,
            "state": cand.state,
            "started_at": round(cand.started_at, 3),
            "state_since_ts": round(cand.state_since_ts, 3),
            "expires_at": round(cand.expires_at, 3),
            "elapsed_seconds": round(max(0.0, elapsed_ai), 1),
            "evidence": ev,
        }
        if pr_path:
            task_obj["project_path"] = pr_path
        if ws_path:
            task_obj["workspace_path"] = ws_path
        tasks.append(task_obj)

        if cand.state == "running":
            # Legacy active_agents preserves old started_at & elapsed_seconds semantics + task_id
            legacy_start = seg_start_by_run.get(cand.run_key, cand.started_at)
            running_agents.append(
                {
                    "task_id": tid,
                    "tool": cand.tool,
                    "display_name": meta["name"],
                    "short_name": meta["short"],
                    "color": meta["color"],
                    "started_at": round(legacy_start, 3),
                    "elapsed_seconds": round(max(0.0, now_ts - legacy_start), 1),
                    "label": cand.label,
                }
            )

    tasks.sort(
        key=lambda t: (
            _SENTINEL_STATE_RANK.get(t["state"], 99),
            -float(t["state_since_ts"]),
            t["id"],
        )
    )
    running_agents.sort(key=lambda x: x["started_at"], reverse=True)
    return tasks, running_agents


def _day_from_retained_work(
    day_str: str,
    is_today: bool,
    retained: Optional[Dict[str, Any]],
    day_u: Dict[str, Any],
) -> Dict[str, Any]:
    dt_obj = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=BIZ_TZ)
    day_u_tools = day_u.get("tools") or {}

    if retained is None:
        return {
            "date": day_str,
            "short_date": dt_obj.strftime("%m/%d"),
            "weekday": WEEKDAY_CN[dt_obj.weekday()],
            "is_today": is_today,
            "agent_seconds": 0.0,
            "agent_hours": 0.0,
            "workdays": 0.0,
            "wall_seconds": 0.0,
            "wall_hours": 0.0,
            "leverage": 1.0,
            "runs": 0,
            "tools": [],
            "hourly_seconds": [0.0] * 24,
            "hourly_available": False,
            "work_status": "unknown",
            "tokens": int(day_u.get("total_tokens", 0)),
            "cost_usd": round(float(day_u.get("cost_usd", 0.0)), 2),
            "unpriced_tokens": int(day_u.get("unpriced_tokens", 0)),
            "cache_hit_rate": round(float(day_u.get("cache_hit_rate", 0.0)), 4),
        }

    agent_sec = float(retained.get("agent_seconds", 0.0) or 0.0)
    wall_sec = float(retained.get("wall_seconds", 0.0) or 0.0)
    runs = int(retained.get("runs", 0) or 0)
    leverage = round(agent_sec / wall_sec, 2) if wall_sec >= 1.0 else 1.0

    raw_hourly = retained.get("hourly_seconds")
    if isinstance(raw_hourly, list) and len(raw_hourly) == 24:
        hourly = [round(float(x or 0.0), 1) for x in raw_hourly]
        hourly_avail = True
    else:
        hourly = [0.0] * 24
        hourly_avail = False

    tools_breakdown: List[Dict[str, Any]] = []
    for wt in retained.get("tools") or []:
        tk = wt["tool"]
        meta = TOOL_META.get(tk, {"name": wt.get("display_name", tk), "short": wt.get("short_name", tk), "color": wt.get("color", "#94A3B8")})
        sec = float(wt.get("agent_seconds", 0.0) or 0.0)
        tu = day_u_tools.get(tk) or {}
        tools_breakdown.append(
            {
                "tool": tk,
                "display_name": meta["name"],
                "short_name": meta["short"],
                "color": meta["color"],
                "agent_seconds": round(sec, 1),
                "agent_hours": round(sec / 3600.0, 2),
                "runs": int(wt.get("runs", 0) or 0),
                "share": round(sec / agent_sec, 4) if agent_sec > 0 else 0.0,
                "tokens": int(tu.get("tokens", 0)),
                "cost_usd": round(float(tu.get("cost_usd", 0.0)), 2),
                "unpriced_tokens": int(tu.get("unpriced_tokens", 0)),
            }
        )

    return {
        "date": day_str,
        "short_date": dt_obj.strftime("%m/%d"),
        "weekday": WEEKDAY_CN[dt_obj.weekday()],
        "is_today": is_today,
        "agent_seconds": round(agent_sec, 1),
        "agent_hours": round(agent_sec / 3600.0, 2),
        "workdays": round(agent_sec / WORKDAY_SECONDS, 2),
        "wall_seconds": round(wall_sec, 1),
        "wall_hours": round(wall_sec / 3600.0, 2),
        "leverage": leverage,
        "runs": runs,
        "tools": tools_breakdown,
        "hourly_seconds": hourly,
        "hourly_available": hourly_avail,
        "work_status": "retained",
        "tokens": int(day_u.get("total_tokens", 0)),
        "cost_usd": round(float(day_u.get("cost_usd", 0.0)), 2),
        "unpriced_tokens": int(day_u.get("unpriced_tokens", 0)),
        "cache_hit_rate": round(float(day_u.get("cache_hit_rate", 0.0)), 4),
    }


def aggregate_segments(
    segments: List[WorkSegment],
    days: int = 14,
    now_ts: Optional[float] = None,
    tokei_ref: Optional[Dict[str, Any]] = None,
    daily_usage_map: Optional[Dict[str, Dict[str, Any]]] = None,
    usage_payload: Optional[Dict[str, Any]] = None,
    *,
    collection_payload: Optional[Dict[str, Any]] = None,
    sentinel_tasks: Optional[List[Dict[str, Any]]] = None,
    active_agents_override: Optional[List[Dict[str, Any]]] = None,
    work_complete: bool = True,
    retained_work_days: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Aggregate WorkSegments into daily metrics, 7-day summary, usage totals, and sentinel tasks."""
    now = now_ts if now_ts is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=BIZ_TZ)
    today_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=BIZ_TZ)
    day_keys = [
        (today_dt - timedelta(days=i)).strftime("%Y-%m-%d")
        for i in range(days - 1, -1, -1)
    ]
    day_set = set(day_keys)
    by_day: Dict[str, List[WorkSegment]] = {d: [] for d in day_keys}

    active_agents: List[Dict[str, Any]] = []
    seen_active_keys: set = set()

    for seg in segments:
        if active_agents_override is None and seg.is_active and seg.run_key not in seen_active_keys:
            seen_active_keys.add(seg.run_key)
            meta = TOOL_META.get(seg.tool, {"name": seg.tool, "short": seg.tool, "color": "#94A3B8"})
            src = seg.source_id or f"work.{seg.tool}"
            active_agents.append(
                {
                    "task_id": make_sentinel_id(src, seg.run_key),
                    "tool": seg.tool,
                    "display_name": meta["name"],
                    "short_name": meta["short"],
                    "color": meta["color"],
                    "started_at": round(seg.start_ts, 3),
                    "elapsed_seconds": round(max(0.0, now - seg.start_ts), 1),
                    "label": seg.label,
                }
            )
        if work_complete:
            for day_str, chunk in split_segment_by_biz_day(seg):
                if day_str in day_set:
                    by_day[day_str].append(chunk)

    if active_agents_override is not None:
        active_agents = active_agents_override
    else:
        active_agents.sort(key=lambda x: x["started_at"], reverse=True)

    usage_by_day = daily_usage_map or {}
    retained_map = retained_work_days or {}

    daily_list: List[Dict[str, Any]] = []
    for day_str in day_keys:
        is_today = (day_str == day_keys[-1])
        day_u = usage_by_day.get(day_str) or {}

        if not work_complete:
            daily_list.append(_day_from_retained_work(day_str, is_today, retained_map.get(day_str), day_u))
            continue

        day_segs = by_day[day_str]
        agent_sec = sum(s.duration for s in day_segs)
        runs = sum(1 for s in day_segs if s.is_turn_head)

        # Preserve positive retained bucket when current complete scan has 0 for a historical day
        if agent_sec <= 0 and runs <= 0:
            ret = retained_map.get(day_str)
            if ret and (float(ret.get("agent_seconds", 0.0) or 0.0) > 0 or int(ret.get("runs", 0) or 0) > 0):
                daily_list.append(_day_from_retained_work(day_str, is_today, ret, day_u))
                continue

        wall_sec = merge_wall_seconds(day_segs)
        leverage = round(agent_sec / wall_sec, 2) if wall_sec >= 1.0 else 1.0

        tool_acc: Dict[str, Dict[str, Any]] = {}
        hourly = [0.0] * 24

        for s in day_segs:
            entry = tool_acc.setdefault(s.tool, {"seconds": 0.0, "runs": 0})
            entry["seconds"] += s.duration
            if s.is_turn_head:
                entry["runs"] += 1

            c = s.start_ts
            while c < s.end_ts:
                cdt = datetime.fromtimestamp(c, tz=BIZ_TZ)
                next_hr = datetime(cdt.year, cdt.month, cdt.day, cdt.hour, tzinfo=BIZ_TZ) + timedelta(hours=1)
                ce = min(s.end_ts, next_hr.timestamp())
                hourly[cdt.hour] += max(0.0, ce - c)
                c = ce

        day_u_tools = day_u.get("tools") or {}
        tools_breakdown = []
        for tool_key, val in sorted(tool_acc.items(), key=lambda kv: kv[1]["seconds"], reverse=True):
            meta = TOOL_META.get(tool_key, {"name": tool_key, "short": tool_key, "color": "#94A3B8"})
            sec = val["seconds"]
            tu = day_u_tools.get(tool_key) or {}
            tools_breakdown.append(
                {
                    "tool": tool_key,
                    "display_name": meta["name"],
                    "short_name": meta["short"],
                    "color": meta["color"],
                    "agent_seconds": round(sec, 1),
                    "agent_hours": round(sec / 3600.0, 2),
                    "runs": val["runs"],
                    "share": round(sec / agent_sec, 4) if agent_sec > 0 else 0.0,
                    "tokens": int(tu.get("tokens", 0)),
                    "cost_usd": round(float(tu.get("cost_usd", 0.0)), 2),
                    "unpriced_tokens": int(tu.get("unpriced_tokens", 0)),
                }
            )

        dt_obj = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=BIZ_TZ)
        daily_list.append(
            {
                "date": day_str,
                "short_date": dt_obj.strftime("%m/%d"),
                "weekday": WEEKDAY_CN[dt_obj.weekday()],
                "is_today": is_today,
                "agent_seconds": round(agent_sec, 1),
                "agent_hours": round(agent_sec / 3600.0, 2),
                "workdays": round(agent_sec / WORKDAY_SECONDS, 2),
                "wall_seconds": round(wall_sec, 1),
                "wall_hours": round(wall_sec / 3600.0, 2),
                "leverage": leverage,
                "runs": runs,
                "tools": tools_breakdown,
                "hourly_seconds": [round(x, 1) for x in hourly],
                "hourly_available": True,
                "work_status": "observed",
                "tokens": int(day_u.get("total_tokens", 0)),
                "cost_usd": round(float(day_u.get("cost_usd", 0.0)), 2),
                "unpriced_tokens": int(day_u.get("unpriced_tokens", 0)),
                "cache_hit_rate": round(float(day_u.get("cache_hit_rate", 0.0)), 4),
            }
        )

    last_7 = daily_list[-7:]
    sum_7_sec = sum(d["agent_seconds"] for d in last_7)
    active_days_7 = sum(1 for d in last_7 if d["agent_seconds"] >= 60.0)
    today_summary = daily_list[-1]

    resolved_tokei_ref = tokei_ref
    if resolved_tokei_ref is None:
        resolved_tokei_ref = {
            "available": True,
            "today_tokens": int(today_summary.get("tokens", 0)),
            "today_cost_usd": round(float(today_summary.get("cost_usd", 0.0)), 2),
        }

    payload: Dict[str, Any] = {
        "schema_version": 3,
        "generated_at": now_dt.isoformat(),
        "generated_ts": round(now, 3),
        "timezone": "Asia/Shanghai",
        "guardrails": {
            "mode": "step_heartbeat",
            "heartbeat_gap_minutes": int(HEARTBEAT_GAP_SECONDS // 60),
            "workday_hours": int(WORKDAY_SECONDS // 3600),
            "max_turn_minutes": 0,
            "max_step_gap_minutes": int(HEARTBEAT_GAP_SECONDS // 60),
        },
        "today": today_summary,
        "last_7_days": last_7,
        "summary_7d": {
            "total_seconds": round(sum_7_sec, 1),
            "total_hours": round(sum_7_sec / 3600.0, 2),
            "avg_daily_hours": round((sum_7_sec / 3600.0) / max(1, len(last_7)), 2),
            "active_days": active_days_7,
        },
        "days": daily_list,
        "active_agents": active_agents,
        "sentinel_tasks": sentinel_tasks if sentinel_tasks is not None else [],
        "tokei_ref": resolved_tokei_ref,
        "usage": usage_payload,
    }
    if collection_payload is not None:
        payload["collection"] = collection_payload
    return payload


def collect_all(
    home: Optional[Path] = None,
    days: int = 14,
    cache_path: Optional[Path] = None,
    use_cache: bool = True,
    now_ts: Optional[float] = None,
    commit_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Run all local read-only work-hours, sentinel, token & billing collectors."""
    home_dir = home or Path.home()
    now = now_ts if now_ts is not None else time.time()
    scan_commit_ts = commit_ts if commit_ts is not None else now
    now_dt = datetime.fromtimestamp(now, tz=BIZ_TZ)
    data_dir = resolve_data_dir(home_dir, create=True)
    ledger_path = data_dir / "ledger.json"
    ledger = load_ledger(ledger_path)

    prev_col = ledger.get("collection") if isinstance(ledger.get("collection"), dict) else {}
    prev_sources = prev_col.get("sources") if isinstance(prev_col.get("sources"), dict) else {}

    # Window sizing (Spec 2.4 & 2.6):
    # First complete work / project-work backfill scans up to 182 days;
    # subsequent runs scan max(days, days_elapsed_in_current_month).
    need_work_backfill = (not ledger.get("work_history_from")) or (not ledger.get("project_work_history_from"))
    work_days = max(days, 182) if need_work_backfill else max(days, now_dt.day)

    today = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_ts = (today - timedelta(days=work_days - 1)).timestamp()
    cutoff_day = (today - timedelta(days=days - 1)).date().isoformat()
    month_start_str = today.strftime("%Y-%m-01")

    if not use_cache:
        effective_cache_path = None
    else:
        effective_cache_path = cache_path if cache_path is not None else (data_dir / "cache.json")
    cache = SegmentCache(effective_cache_path)

    source_statuses: Dict[str, str] = {}
    meta_statuses: Dict[str, str] = {}
    sentinel_candidates: List[SentinelCandidate] = []
    all_segments: List[WorkSegment] = []

    def _call_work_collector(fn, source_id: str, *args, **kwargs):
        try:
            segs = fn(*args, **kwargs)
        except TypeError:
            # Allow test mocks with legacy positional signatures
            segs = fn(*args)
            source_statuses.setdefault(source_id, "complete")
        return segs

    all_segments.extend(
        _call_work_collector(
            collect_t3_code, "work.t3",
            home_dir / ".t3" / "userdata" / "statev2.sqlite", cutoff_ts, now,
            sentinels_out=sentinel_candidates, status_out=source_statuses,
            meta_status_out=meta_statuses, prev_sources=prev_sources,
        )
    )
    all_segments.extend(
        _call_work_collector(
            collect_codex, "work.codex",
            home_dir / ".codex" / "sessions", cutoff_ts, now, cache,
            sentinels_out=sentinel_candidates, status_out=source_statuses, prev_sources=prev_sources,
        )
    )
    all_segments.extend(
        _call_work_collector(
            collect_workbuddy, "work.workbuddy",
            home_dir / ".workbuddy" / "projects", cutoff_ts, now, cache,
            sentinels_out=sentinel_candidates, status_out=source_statuses, prev_sources=prev_sources,
        )
    )
    all_segments.extend(
        _call_work_collector(
            collect_hermes, "work.hermes",
            home_dir / ".hermes", cutoff_ts, now,
            sentinels_out=sentinel_candidates, status_out=source_statuses,
            meta_status_out=meta_statuses, prev_sources=prev_sources,
        )
    )
    all_segments.extend(
        collect_antigravity_sources(
            home_dir / ".gemini" / "antigravity" / "brain",
            home_dir / ".gemini" / "antigravity-cli" / "brain",
            cutoff_ts,
            now,
            cache,
            sentinels_out=sentinel_candidates,
            status_out=source_statuses,
            prev_sources=prev_sources,
        )
    )
    all_segments.extend(
        _call_work_collector(
            collect_claude_code, "work.claude_code",
            home_dir / ".claude" / "projects", cutoff_ts, now, cache,
            sentinels_out=sentinel_candidates, status_out=source_statuses, prev_sources=prev_sources,
        )
    )
    all_segments.extend(
        _call_work_collector(
            collect_pi, "work.pi",
            home_dir / ".pi" / "agent" / "sessions", cutoff_ts, now, cache,
            status_out=source_statuses, prev_sources=prev_sources,
        )
    )
    all_segments.extend(
        _call_work_collector(
            collect_opencode, "work.opencode",
            home_dir / ".local" / "share" / "opencode" / "opencode.db", cutoff_ts,
            status_out=source_statuses, meta_status_out=meta_statuses, prev_sources=prev_sources,
        )
    )

    cache.save()

    # Build base work-hours daily list + per-source-path project work days
    base_payload = aggregate_segments(all_segments, days=work_days, now_ts=now)
    observed_project_work = _build_project_work_days(all_segments)

    pricing_tbl = PricingTable.load(home=home_dir)
    live_projects: Dict[str, Dict[str, Dict[str, Any]]] = {}
    live_billing: Dict[str, Dict[str, Dict[str, float]]] = {}
    live_usage = collect_live_usage(
        home=home_dir,
        days=days,
        use_cache=use_cache,
        now_ts=now,
        pricing=pricing_tbl,
        projects_out=live_projects,
        source_statuses_out=source_statuses,
        billing_out=live_billing,
        billing_cutoff_day=month_start_str,
        prev_sources=prev_sources,
    )

    wt_resolver = WorktreeResolver()
    ledger_state: Dict[str, Any] = {}
    history_from_day = (today - timedelta(days=work_days - 1)).date().isoformat()

    usage_payload, daily_usage_map = build_usage_payload(
        ledger_path=ledger_path,
        live_usage=live_usage,
        live_cutoff_day=cutoff_day,
        now_ts=now,
        work_daily_list=base_payload["days"],
        persist=True,
        pricing=pricing_tbl,
        live_projects=live_projects,
        home=home_dir,
        work_history_from=history_from_day,
        source_statuses=source_statuses,
        project_work_meta_statuses=meta_statuses,
        observed_project_work=observed_project_work,
        project_work_history_from=history_from_day,
        live_billing=live_billing,
        billing_cutoff_day=month_start_str,
        commit_ts=scan_commit_ts,
        worktree_resolver=wt_resolver,
        out_state=ledger_state,
    )

    collection_payload = ledger_state.get("collection")
    work_complete = bool(ledger_state.get("work_complete", True))
    rejected_stale = bool(ledger_state.get("rejected_stale_commit", False))
    retained_work_days = ledger_state.get("ledger_work_days") or {}

    healthy_work_sources = {
        sid for sid in WORK_SOURCE_IDS if source_statuses.get(sid) == "complete"
    }
    if rejected_stale:
        sentinel_tasks, active_agents = [], []
    else:
        sentinel_tasks, active_agents = _finalize_sentinel_tasks(
            sentinel_candidates,
            all_segments,
            now,
            healthy_sources=healthy_work_sources,
            resolver=wt_resolver,
        )

    today_u = (usage_payload.get("periods") or {}).get("today") or {}
    native_ref = {
        "available": True,
        "today_tokens": int(today_u.get("total_tokens", 0)),
        "today_cost_usd": round(float(today_u.get("cost_usd", 0.0)), 2),
    }

    return aggregate_segments(
        all_segments,
        days=days,
        now_ts=now,
        tokei_ref=native_ref,
        daily_usage_map=daily_usage_map,
        usage_payload=usage_payload,
        collection_payload=collection_payload,
        sentinel_tasks=sentinel_tasks,
        active_agents_override=active_agents,
        work_complete=work_complete,
        retained_work_days=retained_work_days,
    )


# ---------------------------------------------------------------------------
# Markdown Reports & Unified Cost Formatting (Spec 2.2.5 & 2.6)
# ---------------------------------------------------------------------------


def format_duration_cn(seconds: float) -> str:
    if seconds < 60:
        return f"{int(round(seconds))}秒"
    mins = seconds / 60.0
    if mins < 60:
        return f"{mins:.0f}分钟"
    return f"{seconds / 3600.0:.2f}小时"


def _format_tokens_short(tokens: int) -> str:
    if tokens >= 1_000_000_000:
        return f"{tokens / 1_000_000_000.0:.2f}B"
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000.0:.2f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000.0:.1f}K"
    return str(tokens)


def format_usd(amount: float) -> str:
    if amount >= 1000:
        return f"${amount:.0f}"
    if amount >= 100:
        return f"${amount:.1f}"
    return f"${amount:.2f}"


def format_estimated_usd(amount: float, unpriced_tokens: int, *, has_observation: bool = True) -> str:
    if not has_observation:
        return "—"
    if unpriced_tokens > 0:
        return f"{format_usd(amount)}+" if amount > 0 else "未计价"
    return format_usd(amount)


def _collection_health_notice(payload: Dict[str, Any]) -> Optional[str]:
    col = payload.get("collection")
    if not isinstance(col, dict):
        return None
    domain_labels = (("work", "工时"), ("usage", "用量"), ("projects", "项目"))
    issues: List[str] = []
    for key, label in domain_labels:
        h = col.get(key)
        if not isinstance(h, dict):
            continue
        st = h.get("status")
        if st in ("partial", "unavailable"):
            st_cn = "部分可用" if st == "partial" else "不可用"
            ts = h.get("last_success_ts")
            if isinstance(ts, (int, float)):
                ts_str = datetime.fromtimestamp(float(ts), tz=BIZ_TZ).strftime("%Y-%m-%d %H:%M")
                issues.append(f"{label}（{st_cn}，末次完整成功 {ts_str}）")
            else:
                issues.append(f"{label}（{st_cn}，尚无完整成功记录）")
    if not issues:
        return None
    return f"> 数据完整性提示：当前快照存在不完整域——{'、'.join(issues)}，已回退保留历史已确认记录。"


def render_markdown_report(
    payload: Dict[str, Any],
    target_date: Optional[str] = None,
    period: Optional[str] = None,
) -> str:
    """Render a unified Markdown daily or period AI work-hours + token/cost report."""
    hb = int(HEARTBEAT_GAP_SECONDS // 60)
    notice = _collection_health_notice(payload)

    if period is not None:
        p_stats = ((payload.get("usage") or {}).get("periods") or {}).get(period)
        if not isinstance(p_stats, dict):
            raise ValueError(f"未找到周期数据：{period}")
        title_cn = PERIOD_TITLES_CN.get(period, period)
        s_date = p_stats.get("start_date", "")
        e_date = p_stats.get("end_date", "")
        range_str = s_date if s_date == e_date else f"{s_date} ~ {e_date}"

        lines = [
            f"## AI 工时与用量战报 · {title_cn}（{range_str}）",
            "",
            f"- **AI 累计工时**：**{p_stats['agent_hours']:.2f} h**（折合 **{p_stats['workdays']:.2f} 个 AI 工作日**，按 8h/日）",
            f"- **并行杠杆率**：**{p_stats['leverage']:.2f}×**（去重墙钟 {p_stats['wall_hours']:.2f} h · 共 {p_stats['runs']} 轮执行 · 活跃 {p_stats['active_days']} 天）",
        ]
        p_tok = int(p_stats.get("total_tokens", 0) or 0)
        p_cost = float(p_stats.get("cost_usd", 0.0) or 0.0)
        p_unpriced = int(p_stats.get("unpriced_tokens", 0) or 0)
        has_obs = (p_tok > 0 or p_cost > 0 or p_unpriced > 0)
        if has_obs:
            cache_pct = int(round(float(p_stats.get("cache_hit_rate", 0.0) or 0.0) * 100))
            cost_str = format_estimated_usd(p_cost, p_unpriced, has_observation=True)
            lines.append(
                f"- **Token 与估算费用**：**{_format_tokens_short(p_tok)} tokens**（估算 **{cost_str}** · 缓存命中 {cache_pct}%）"
            )
            if p_unpriced > 0:
                lines.append(f"- 另有 {_format_tokens_short(p_unpriced)} Token 未计价。")

        tools = p_stats.get("tools") or []
        total_sec = float(p_stats.get("agent_seconds", 0.0) or 0.0)
        if tools:
            lines.append("")
            lines.append("### 分工具明细（工时 + 用量）")
            lines.append("| AI 工具 | 工时 | 占比 | 轮次 | Token | 估算费用 |")
            lines.append("|---|---:|---:|---:|---:|---:|")
            for t in tools:
                t_sec = float(t.get("agent_seconds", 0.0) or 0.0)
                t_hrs = float(t.get("agent_hours", 0.0) or 0.0)
                pct = (t_sec / total_sec * 100.0) if total_sec > 0 else 0.0
                t_tok = int(t.get("total_tokens", 0) or 0)
                t_cost = float(t.get("cost_usd", 0.0) or 0.0)
                t_unp = int(t.get("unpriced_tokens", 0) or 0)
                t_has_obs = (t_tok > 0 or t_cost > 0 or t_unp > 0)
                work_str = f"{format_duration_cn(t_sec)} ({t_hrs:.2f}h)" if t_sec > 0 else "—"
                tok_str = _format_tokens_short(t_tok) if t_tok > 0 else "—"
                cost_str = format_estimated_usd(t_cost, t_unp, has_observation=t_has_obs)
                lines.append(
                    f"| {t['display_name']} | {work_str} | {pct:.0f}% | {t.get('runs', 0)} | {tok_str} | {cost_str} |"
                )
        elif p_stats.get("agent_seconds", 0) == 0 and not has_obs:
            lines.append("")
            lines.append("该周期暂无工时与用量记录。")

        lines.append("")
        lines.append(
            f"> 周期：{range_str}。口径：步间心跳 {hb}m（纯 AI 执行，剔除等人等待），自有增量 Token 引擎，T3↔Codex 双向去重。"
        )
        if notice:
            lines.append(notice)
        return "\n".join(lines)

    # Daily report (--date or default today)
    days_list = payload.get("days") or []
    day = payload["today"]
    target_idx = len(days_list) - 1
    if target_date:
        matched_idx = None
        for idx, d in enumerate(days_list):
            if d["date"] == target_date:
                day = d
                matched_idx = idx
                break
        if matched_idx is None:
            raise ValueError(f"日期不在可查询窗口内：{target_date}")
        target_idx = matched_idx

    lines = [
        f"## AI 工时与用量日报 · {day['date']} ({day['weekday']})",
        "",
        f"- **AI 累计工时**：**{day['agent_hours']:.2f} h**（折合 **{day['workdays']:.2f} 个 AI 工作日**，按 8h/日）",
        f"- **并行杠杆率**：**{day['leverage']:.2f}×**（去重墙钟 {day['wall_hours']:.2f} h · 共 {day['runs']} 轮执行）",
    ]
    day_tok = int(day.get("tokens", 0) or 0)
    day_cost = float(day.get("cost_usd", 0.0) or 0.0)
    day_unp = int(day.get("unpriced_tokens", 0) or 0)
    has_obs = (day_tok > 0 or day_cost > 0 or day_unp > 0)
    if has_obs:
        cache_pct = int(round(float(day.get("cache_hit_rate", 0.0) or 0.0) * 100))
        cost_str = format_estimated_usd(day_cost, day_unp, has_observation=True)
        lines.append(
            f"- **Token 与估算费用**：**{_format_tokens_short(day_tok)} tokens**（估算 **{cost_str}** · 缓存命中 {cache_pct}%）"
        )
        if day_unp > 0:
            lines.append(f"- 另有 {_format_tokens_short(day_unp)} Token 未计价。")

    if day.get("tools"):
        lines.append("")
        lines.append("### 分工具明细（工时 + 用量）")
        lines.append("| AI 工具 | 工时 | 占比 | 轮次 | Token | 估算费用 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for t in day["tools"]:
            pct = t["share"] * 100.0
            t_tok = int(t.get("tokens", 0) or 0)
            t_cost = float(t.get("cost_usd", 0.0) or 0.0)
            t_unp = int(t.get("unpriced_tokens", 0) or 0)
            t_has_obs = (t_tok > 0 or t_cost > 0 or t_unp > 0)
            tok_str = _format_tokens_short(t_tok) if t_tok > 0 else "—"
            cost_str = format_estimated_usd(t_cost, t_unp, has_observation=t_has_obs)
            lines.append(
                f"| {t['display_name']} | {format_duration_cn(t['agent_seconds'])} ({t['agent_hours']:.2f}h) | {pct:.0f}% | {t['runs']} | {tok_str} | {cost_str} |"
            )

    # Trailing 7-day note bounded right at target_date (only covering available days!)
    if days_list and target_idx >= 0:
        win_start = max(0, target_idx - 6)
        window_days = days_list[win_start : target_idx + 1]
        cov_count = len(window_days)
        cov_sec = sum(float(d.get("agent_seconds", 0.0) or 0.0) for d in window_days)
        cov_hrs = cov_sec / 3600.0
        cov_avg = cov_hrs / max(1, cov_count)
        cov_active = sum(1 for d in window_days if float(d.get("agent_seconds", 0.0) or 0.0) >= 60.0)
        lines.extend([
            "",
            f"> 截至 {day['date']} 近 {cov_count} 天累计 **{cov_hrs:.2f} h**（日均 {cov_avg:.2f} h，活跃 {cov_active}/{cov_count} 天）。口径：步间心跳 {hb}m（纯 AI 执行，剔除等人等待），自有增量 Token 引擎，T3↔Codex 双向去重。",
        ])
    if notice:
        lines.append(notice)
    return "\n".join(lines)


class _CNArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(f"参数错误：{message}", file=sys.stderr)
        raise SystemExit(2)


def _validate_cli_args(args: argparse.Namespace, now_ts: float) -> Optional[str]:
    """Validate CLI args strictly BEFORE touching ledger. Returns Chinese error line or None."""
    if args.period is not None and not args.markdown:
        return "参数错误：--period 仅可与 --markdown 配合使用。"
    if args.date is not None and not args.markdown:
        return "参数错误：--date 仅可与 --markdown 配合使用。"
    if args.period is not None and args.date is not None:
        return "参数错误：--period 与 --date 不能同时使用。"
    if args.period is not None and args.period not in VALID_PERIOD_KEYS:
        return f"参数错误：不支持的周期参数（仅支持 {', '.join(VALID_PERIOD_KEYS)}）。"
    if args.date is not None:
        if not valid_day(args.date):
            return f"参数错误：日期格式无效或不是真实日期（{args.date}），请使用 YYYY-MM-DD。"
        clamped_days = max(7, min(args.days, 60))
        now_dt = datetime.fromtimestamp(now_ts, tz=BIZ_TZ)
        today_d = date(now_dt.year, now_dt.month, now_dt.day)
        earliest_d = today_d - timedelta(days=clamped_days - 1)
        target_d = date.fromisoformat(args.date)
        if target_d > today_d:
            return f"参数错误：目标日期 {args.date} 不能晚于今日（{today_d.isoformat()}）。"
        if target_d < earliest_d:
            return f"参数错误：目标日期 {args.date} 超出当前 {clamped_days} 天窗口（最早 {earliest_d.isoformat()}）。"
    return None


def main() -> int:
    parser = _CNArgumentParser(description="AgentPulse local AI work-hours & usage collector")
    parser.add_argument("--days", type=int, default=14, help="Number of days to aggregate (default 14)")
    parser.add_argument("--json", action="store_true", help="Output compact JSON (default)")
    parser.add_argument("--pretty", action="store_true", help="Output pretty-printed JSON")
    parser.add_argument("--markdown", action="store_true", help="Output Markdown report")
    parser.add_argument("--date", type=str, default=None, help="Target date YYYY-MM-DD for --markdown")
    parser.add_argument("--period", type=str, default=None, help="Target period for --markdown")
    parser.add_argument("--no-cache", action="store_true", help="Bypass incremental file cache")
    parser.add_argument("--restore-ledger", type=Path, metavar="BACKUP", help="Restore a validated ledger backup")
    args = parser.parse_args()

    # Optional deterministic clock override for offline benchmarks & tests
    env_now = os.environ.get("AGENTPULSE_NOW_TS")
    forced_now: Optional[float] = None
    if env_now:
        try:
            v = float(env_now)
            if math.isfinite(v) and v > 0:
                forced_now = v
        except ValueError:
            pass
    now_ts = forced_now if forced_now is not None else time.time()

    arg_err = _validate_cli_args(args, now_ts)
    if arg_err is not None:
        print(arg_err, file=sys.stderr)
        return 2

    t0 = time.perf_counter()
    try:
        if args.restore_ledger:
            saved = restore_ledger(resolve_data_dir() / "ledger.json", args.restore_ledger)
            print("账本已恢复。" + (f"恢复前副本：{saved}" if saved else ""))
            return 0
        payload = collect_all(
            days=max(7, min(args.days, 60)),
            use_cache=(not args.no_cache),
            now_ts=now_ts,
        )
    except LedgerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("无法读写 AgentPulse 本地数据，请检查文件权限和可用磁盘空间。", file=sys.stderr)
        return 1
    payload["elapsed_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)

    if args.markdown:
        try:
            print(render_markdown_report(payload, target_date=args.date, period=args.period))
        except ValueError as exc:
            print(f"参数错误：{exc}", file=sys.stderr)
            return 2
    elif args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
