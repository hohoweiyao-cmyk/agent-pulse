#!/usr/bin/env python3
"""AgentPulse — Local read-only AI work-hours, token & cost collector (v0.4).

Methodology (Step-Heartbeat Pure AI Execution):
- Excludes all between-turn human reading/thinking/typing time.
- Uses a 5-minute inter-step heartbeat window (HEARTBEAT_GAP_SECONDS = 300s) across
  all 8 local AI sources with NO artificial 30-minute total-turn cap:
  * Continuous autonomous agent work lasting 45m or 2h counts in full.
  * Any silent intra-turn pause > 5m (human approval wait, sleep, stall) is dropped (0s).
- For T3 Code, explicitly subtracts logged human-wait intervals
  (`approval_request`, `user_input_request`, `secret_request`, `runtime_requests`),
  even when shorter than 5 minutes, and pauses live active status while waiting on user.

Zero external dependencies (Python 3.9+ stdlib only).
Strictly read-only on all AI client logs and SQLite databases (WAL-safe temp snapshots).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

_ENGINE_DIR = str(Path(__file__).resolve().parent)
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from ledger import LedgerError, build_usage_payload, load_ledger, resolve_data_dir, restore_ledger  # noqa: E402
from pricing import PricingTable  # noqa: E402
from storage import sqlite_snapshot, write_json  # noqa: E402
from usage_collector import collect_live_usage  # noqa: E402

# Explicit business timezone: Asia/Shanghai (UTC+8)
BIZ_TZ = timezone(timedelta(hours=8))

# Step-heartbeat pure AI execution guardrails
HEARTBEAT_GAP_SECONDS = 300.0  # Drop silent inter-step gaps > 5 minutes (0s added)
SINGLE_STEP_CAP_SECONDS = 900.0  # Sanity cap for a single opaque tool/item span with no sub-events (15m)
MIN_TURN_SECONDS = 0.5         # Filter sub-0.5s metadata echoes
WORKDAY_SECONDS = 8.0 * 3600.0 # 1 AI Workday = 8 cumulative agent hours
LIVE_STALE_SECONDS = 90.0      # JSONL mtime freshness window for live active detection

CACHE_VERSION = 4

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
T3_NON_AGENT_TYPES = frozenset({"user_message", "system_notice", "notification", "handoff"})


@dataclass
class WorkSegment:
    """A single bounded pure-AI active work interval [start_ts, end_ts]."""

    tool: str
    start_ts: float
    end_ts: float
    is_active: bool = False
    run_key: str = ""
    label: str = ""
    is_turn_head: bool = True  # Only count 1 run per turn even if split into sub-segments

    @property
    def duration(self) -> float:
        return max(0.0, self.end_ts - self.start_ts)


def parse_ts(value: Any) -> Optional[float]:
    """Parse ISO-8601 string or epoch sec/ms into Unix epoch float seconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
            return None
        if v > 1e14:  # microseconds
            return v / 1e6
        if v > 1e11:  # milliseconds
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
    """Merge overlapping or nearly touching [start, end] intervals."""
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
    """Subtract explicit human-wait exclusion intervals from active work intervals."""
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
) -> List[WorkSegment]:
    """Build pure-AI WorkSegments using a step-heartbeat window (no 30m total cap).

    - Adjacent event timestamps within `heartbeat_gap_s` (5 min) are joined into active work.
    - Any silent gap > `heartbeat_gap_s` is DROPPED (0 seconds added).
    - Optional `extra_spans` (explicit item [startedAt, completedAt] durations like a running
      shell command, capped individually at SINGLE_STEP_CAP_SECONDS) are unioned in.
    - Optional `exclusions` (explicit human approval / input waits) are cleanly subtracted.
    """
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
            )
        )
    return result


def split_segment_by_biz_day(seg: WorkSegment) -> List[Tuple[str, WorkSegment]]:
    """Split a WorkSegment at local Asia/Shanghai midnight boundaries."""
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
                    ),
                )
            )
            first_chunk = False
        cur = chunk_end
    return out


def merge_wall_seconds(segments: Iterable[WorkSegment]) -> float:
    """Compute interval-union total duration across all segments (deduplicated wall clock)."""
    ivs = merge_raw_intervals(((s.start_ts, s.end_ts) for s in segments), bridge_gap=0.0)
    return sum(e - s for s, e in ivs)


# ---------------------------------------------------------------------------
# File Parse Cache (incremental mtime + size cache for JSONL files)
# ---------------------------------------------------------------------------


class SegmentCache:
    """Stores parsed non-active segments keyed by file path + (mtime_ns, size)."""

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

    def get(self, file_path: Path, now_ts: float) -> Optional[List[WorkSegment]]:
        try:
            st = file_path.stat()
        except OSError:
            return None
        if now_ts - st.st_mtime <= LIVE_STALE_SECONDS + 15.0:
            return None
        key = hashlib.sha256(os.fsencode(file_path)).hexdigest()
        self.seen.add(key)
        entry = self.files.get(key)
        if not entry:
            return None
        if entry.get("mtime_ns") != st.st_mtime_ns or entry.get("size") != st.st_size:
            return None
        segs = []
        for row in entry.get("segs", []):
            segs.append(
                WorkSegment(
                    tool=row[0],
                    start_ts=float(row[1]),
                    end_ts=float(row[2]),
                    is_active=False,
                    is_turn_head=bool(row[3]),
                )
            )
        return segs

    def put(self, file_path: Path, segs: List[WorkSegment], now_ts: float) -> None:
        try:
            st = file_path.stat()
        except OSError:
            return
        if now_ts - st.st_mtime <= LIVE_STALE_SECONDS + 15.0:
            return
        if any(s.is_active for s in segs):
            return
        key = hashlib.sha256(os.fsencode(file_path)).hexdigest()
        self.seen.add(key)
        self.files[key] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "segs": [
                [s.tool, round(s.start_ts, 3), round(s.end_ts, 3), 1 if s.is_turn_head else 0]
                for s in segs
            ],
        }
        self.dirty = True

    def save(self) -> None:
        if not self.path or (not self.dirty and self.files.keys() <= self.seen):
            return
        try:
            write_json(self.path, {"v": CACHE_VERSION, "files": {
                key: value for key, value in self.files.items() if key in self.seen
            }})
        except (OSError, ValueError):
            pass


# ---------------------------------------------------------------------------
# Source Collectors
# ---------------------------------------------------------------------------


def collect_t3_code(db_path: Path, cutoff_ts: float, now_ts: float) -> List[WorkSegment]:
    """Collect step-heartbeat intervals from T3 Code statev2.sqlite.

    Uses per-step item timestamps (`orchestration_v2_projection_turn_items`) and explicitly
    subtracts human-wait intervals (`approval_request`, `user_input_request`, `secret_request`,
    and `orchestration_v2_projection_runtime_requests`).
    """
    segs: List[WorkSegment] = []
    with sqlite_snapshot(db_path) as conn:
        if conn is None:
            return segs

        titles: Dict[str, str] = {}
        try:
            for r in conn.execute("SELECT thread_id, title FROM orchestration_v2_projection_threads"):
                if r["thread_id"] and r["title"]:
                    titles[str(r["thread_id"])] = str(r["title"])[:48]
        except Exception:
            pass

        # Load explicit human-wait runtime requests keyed by thread_id
        thread_waits: Dict[str, List[Tuple[float, float, bool]]] = {}
        try:
            for r in conn.execute(
                "SELECT thread_id, status, created_at, resolved_at FROM orchestration_v2_projection_runtime_requests"
            ):
                tid = str(r["thread_id"] or "")
                ws = parse_ts(r["created_at"])
                we = parse_ts(r["resolved_at"])
                is_pending = (we is None) and str(r["status"] or "").lower() not in ("resolved", "cancelled", "rejected")
                if tid and ws is not None:
                    end_w = we if we is not None else now_ts
                    if end_w > ws:
                        thread_waits.setdefault(tid, []).append((ws, end_w, is_pending))
        except Exception:
            pass

        # Load turn items grouped by run_id
        run_items: Dict[str, List[Tuple[str, Optional[float], Optional[float], Optional[float]]]] = {}
        try:
            for r in conn.execute(
                """
                SELECT run_id, type, updated_at,
                       json_extract(payload_json, '$.startedAt') AS started_at,
                       json_extract(payload_json, '$.completedAt') AS completed_at
                FROM orchestration_v2_projection_turn_items
                WHERE run_id IS NOT NULL
                """
            ):
                rid = str(r["run_id"] or "")
                if not rid:
                    continue
                run_items.setdefault(rid, []).append(
                    (
                        str(r["type"] or ""),
                        parse_ts(r["started_at"]),
                        parse_ts(r["completed_at"]),
                        parse_ts(r["updated_at"]),
                    )
                )
        except Exception:
            pass

        try:
            rows = conn.execute(
                """
                SELECT run_id, thread_id, provider, status, requested_at, completed_at,
                       json_extract(payload_json, '$.startedAt') AS started_at
                FROM orchestration_v2_projection_runs
                WHERE status NOT IN ('queued', 'rolled_back')
                """
            ).fetchall()
        except Exception:
            return segs

        for r in rows:
            provider = str(r["provider"] or "").lower()
            if "antigravity" in provider:
                tool = "t3_antigravity"
            elif "codex" in provider:
                tool = "t3_codex"
            elif "claude" in provider:
                tool = "t3_claude"
            else:
                tool = "t3_other"

            start_ts = parse_ts(r["started_at"]) or parse_ts(r["requested_at"])
            if start_ts is None:
                continue
            end_ts = parse_ts(r["completed_at"])
            status = str(r["status"] or "").lower()
            thread_id = str(r["thread_id"] or "")
            label = titles.get(thread_id, "")
            run_id = str(r["run_id"] or "")

            is_live_candidate = (end_ts is None) and status in ("running", "starting", "preparing")
            if not is_live_candidate and (end_ts is None or end_ts < cutoff_ts or end_ts <= start_ts):
                continue

            bound_end = now_ts if is_live_candidate else end_ts  # type: ignore[assignment]

            # Collect step heartbeats, genuine tool/reasoning spans, and explicit human-wait exclusions
            step_pts: List[float] = []
            extra_spans: List[Tuple[float, float]] = []
            exclusions: List[Tuple[float, float]] = []
            has_pending_human_wait = False

            for ws, we, is_pending in thread_waits.get(thread_id, []):
                if we >= start_ts and ws <= bound_end:
                    exclusions.append((max(ws, start_ts), min(we, bound_end)))
                    if is_pending:
                        has_pending_human_wait = True

            for itype, isa, ica, iua in run_items.get(run_id, []):
                if itype in T3_HUMAN_WAIT_TYPES:
                    ws = isa or iua
                    we = ica or iua or (now_ts if is_live_candidate else None)
                    if ws is not None and we is not None and we > ws:
                        exclusions.append((max(ws, start_ts), min(we, bound_end)))
                    if is_live_candidate and ica is None:
                        has_pending_human_wait = True
                    continue

                if itype in T3_NON_AGENT_TYPES:
                    continue

                for t in (isa, ica, iua):
                    if t is not None and start_ts <= t <= bound_end:
                        step_pts.append(t)

                # Explicit bounded execution spans (e.g. a real shell command or reasoning step)
                if itype in ("command_execution", "reasoning", "dynamic_tool", "file_change", "file_search", "web_search", "subagent", "compaction"):
                    if isa is not None and ica is not None and ica > isa and start_ts <= isa and ica <= bound_end:
                        extra_spans.append((isa, ica))

            if is_live_candidate:
                last_hb = max(step_pts) if step_pts else start_ts
                is_actively_working = (not has_pending_human_wait) and (0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS)
                if is_actively_working:
                    step_pts.append(max(now_ts, last_hb + 0.5))
                if not step_pts:
                    continue
                built = build_heartbeat_segments(
                    tool=tool,
                    turn_start=start_ts,
                    step_timestamps=step_pts,
                    extra_spans=extra_spans,
                    exclusions=exclusions,
                    run_key=f"t3:{run_id}",
                    label=label,
                    is_active=is_actively_working,
                )
            else:
                step_pts.append(bound_end)
                built = build_heartbeat_segments(
                    tool=tool,
                    turn_start=start_ts,
                    step_timestamps=step_pts,
                    extra_spans=extra_spans,
                    exclusions=exclusions,
                    run_key=f"t3:{run_id}",
                    label=label,
                    is_active=False,
                )

            for seg in built:
                if seg.end_ts >= cutoff_ts:
                    segs.append(seg)
    return segs


def parse_codex_file(file_path: Path, now_ts: float) -> Tuple[bool, List[WorkSegment]]:
    """Parse one Codex JSONL rollout file using step-heartbeat logic. Returns (skipped_t3, segments)."""
    segs: List[WorkSegment] = []
    try:
        st = file_path.stat()
    except OSError:
        return False, segs

    is_fresh = (now_ts - st.st_mtime) <= LIVE_STALE_SECONDS
    turn_starts: Dict[str, float] = {}
    turn_steps: Dict[str, List[float]] = {}
    turn_spans: Dict[str, List[Tuple[float, float]]] = {}
    completed_turns: set = set()
    active_turn_id: Optional[str] = None
    last_event_type = ""
    session_label = ""

    def emit_turn(key: str, start_ts: float, end_ts: Optional[float], active: bool) -> None:
        steps = list(turn_steps.get(key, []))
        if end_ts is not None and end_ts >= start_ts:
            steps.append(end_ts)
        built = build_heartbeat_segments(
            tool="codex",
            turn_start=start_ts,
            step_timestamps=steps,
            extra_spans=turn_spans.get(key),
            run_key=f"codex:{key}",
            label=session_label,
            is_active=active,
        )
        segs.extend(built)

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                otype = obj.get("type")
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    payload = {}
                line_ts = parse_ts(obj.get("timestamp"))

                if otype == "session_meta":
                    originator = str(payload.get("originator") or "")
                    if originator == "T3 Code":
                        return True, []
                    cwd = str(payload.get("cwd") or "")
                    if cwd:
                        session_label = Path(cwd).name
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
                            emit_turn(key, start_ts, end_ts, active=False)
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
    except OSError:
        return False, segs

    # Unfinished turns (live active or previously interrupted)
    for key, start_ts in turn_starts.items():
        if key in completed_turns:
            continue
        steps = turn_steps.get(key, [])
        last_hb = max(steps) if steps else start_ts
        if key == active_turn_id and is_fresh and last_event_type not in ("task_complete", "turn_aborted"):
            if 0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS:
                emit_turn(key, start_ts, max(now_ts, last_hb + 0.5), active=True)
                continue
        if steps:
            emit_turn(key, start_ts, None, active=False)

    return False, segs


def collect_codex(sessions_dir: Path, cutoff_ts: float, now_ts: float, cache: SegmentCache) -> List[WorkSegment]:
    """Collect standalone Codex sessions, excluding originator == 'T3 Code'."""
    if not sessions_dir.is_dir():
        return []
    by_turn: Dict[str, List[WorkSegment]] = {}
    scan_floor = cutoff_ts - 86400.0

    for root, _, files in os.walk(sessions_dir):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            fpath = Path(root) / fname
            try:
                if fpath.stat().st_mtime < scan_floor:
                    continue
            except OSError:
                continue

            cached = cache.get(fpath, now_ts)
            if cached is not None:
                file_segs = cached
            else:
                _, file_segs = parse_codex_file(fpath, now_ts)
                cache.put(fpath, file_segs, now_ts)

            grouped: Dict[str, List[WorkSegment]] = {}
            for seg in file_segs:
                if seg.end_ts >= cutoff_ts:
                    grouped.setdefault(seg.run_key, []).append(seg)

            for rkey, rsegs in grouped.items():
                prev = by_turn.get(rkey)
                cur_dur = sum(s.duration for s in rsegs)
                prev_dur = sum(s.duration for s in prev) if prev else -1.0
                cur_active = any(s.is_active for s in rsegs)
                prev_active = any(s.is_active for s in prev) if prev else False
                if prev is None or cur_dur > prev_dur or (cur_active and not prev_active):
                    by_turn[rkey] = rsegs

    out: List[WorkSegment] = []
    for rsegs in by_turn.values():
        out.extend(rsegs)
    return out


def parse_workbuddy_file(file_path: Path, now_ts: float) -> List[WorkSegment]:
    """Parse a WorkBuddy JSONL session into step-heartbeat WorkSegments."""
    segs: List[WorkSegment] = []
    try:
        st = file_path.stat()
    except OSError:
        return segs
    is_fresh = (now_ts - st.st_mtime) <= LIVE_STALE_SECONDS

    cur_turn_start: Optional[float] = None
    cur_steps: List[float] = []
    last_event_kind = ""
    turn_idx = 0
    label = ""

    def flush(active: bool = False) -> None:
        nonlocal cur_turn_start, cur_steps, turn_idx
        if cur_turn_start is not None and cur_steps:
            turn_idx += 1
            segs.extend(
                build_heartbeat_segments(
                    tool="workbuddy",
                    turn_start=cur_turn_start,
                    step_timestamps=cur_steps,
                    run_key=f"wb:{file_path.stem}:{turn_idx}",
                    label=label,
                    is_active=active,
                )
            )
        cur_turn_start = None
        cur_steps = []

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                otype = obj.get("type")
                if otype == "ai-title" and not label:
                    t = str(obj.get("aiTitle") or obj.get("title") or "").strip()
                    if t:
                        label = t[:40]
                    continue
                if not label and obj.get("cwd"):
                    label = Path(str(obj["cwd"])).name

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
    except OSError:
        return segs

    if cur_turn_start is not None:
        last_hb = max(cur_steps) if cur_steps else cur_turn_start
        if is_fresh and last_event_kind in ("user", "reasoning", "function_call", "function_call_result"):
            if 0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS:
                cur_steps.append(now_ts)
                flush(active=True)
            else:
                flush(active=False)
        else:
            flush(active=False)

    return segs


def collect_workbuddy(projects_dir: Path, cutoff_ts: float, now_ts: float, cache: SegmentCache) -> List[WorkSegment]:
    if not projects_dir.is_dir():
        return []
    out: List[WorkSegment] = []
    scan_floor = cutoff_ts - 86400.0
    for root, _, files in os.walk(projects_dir):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            fpath = Path(root) / fname
            try:
                if fpath.stat().st_mtime < scan_floor:
                    continue
            except OSError:
                continue
            cached = cache.get(fpath, now_ts)
            if cached is not None:
                file_segs = cached
            else:
                file_segs = parse_workbuddy_file(fpath, now_ts)
                cache.put(fpath, file_segs, now_ts)
            for seg in file_segs:
                if seg.end_ts >= cutoff_ts:
                    out.append(seg)
    return out


def collect_hermes_db(db_path: Path, cutoff_ts: float, now_ts: float, profile_tag: str = "") -> List[WorkSegment]:
    """Collect step-heartbeat turn spans & active leases from one Hermes state.db."""
    segs: List[WorkSegment] = []
    with sqlite_snapshot(db_path) as conn:
        if conn is None:
            return segs
        scan_floor = cutoff_ts - 86400.0

        titles: Dict[str, str] = {}
        try:
            for r in conn.execute("SELECT id, title, display_name, source FROM sessions"):
                t = str(r["title"] or r["display_name"] or r["source"] or "").strip()
                if profile_tag:
                    t = f"{profile_tag} · {t}" if t else profile_tag
                titles[str(r["id"])] = t[:44]
        except Exception:
            pass

        active_leases: Dict[str, float] = {}
        try:
            for r in conn.execute(
                "SELECT conversation_id, acquired_at, expires_at FROM session_turn_leases WHERE expires_at > ?",
                (now_ts,),
            ):
                acq = parse_ts(r["acquired_at"])
                if acq is not None and 0.0 <= now_ts - acq <= HEARTBEAT_GAP_SECONDS:
                    active_leases[str(r["conversation_id"])] = acq
        except Exception:
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
        except Exception:
            return segs

        cur_sid: Optional[str] = None
        cur_turn_start: Optional[float] = None
        cur_steps: List[float] = []
        turn_idx = 0

        def flush(sid: Optional[str]) -> None:
            nonlocal cur_turn_start, cur_steps, turn_idx
            if sid and cur_turn_start is not None and cur_steps:
                turn_idx += 1
                segs.extend(
                    build_heartbeat_segments(
                        tool="hermes",
                        turn_start=cur_turn_start,
                        step_timestamps=cur_steps,
                        run_key=f"hermes:{profile_tag}:{sid}:{turn_idx}",
                        label=titles.get(sid, profile_tag),
                        is_active=False,
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

        for conv_id, acq in active_leases.items():
            segs.append(
                WorkSegment(
                    tool="hermes",
                    start_ts=acq,
                    end_ts=max(now_ts, acq + 0.5),
                    is_active=True,
                    run_key=f"hermes-lease:{profile_tag}:{conv_id}",
                    label=titles.get(conv_id, profile_tag or "Hermes"),
                    is_turn_head=True,
                )
            )

    return [s for s in segs if s.end_ts >= cutoff_ts]


def collect_hermes(hermes_root: Path, cutoff_ts: float, now_ts: float) -> List[WorkSegment]:
    out: List[WorkSegment] = []
    main_db = hermes_root / "state.db"
    if main_db.is_file():
        out.extend(collect_hermes_db(main_db, cutoff_ts, now_ts, ""))
    profiles_dir = hermes_root / "profiles"
    if profiles_dir.is_dir():
        for pdir in sorted(profiles_dir.iterdir()):
            pdb = pdir / "state.db"
            if pdb.is_file():
                out.extend(collect_hermes_db(pdb, cutoff_ts, now_ts, pdir.name))
    return out


def parse_antigravity_transcript(file_path: Path, now_ts: float) -> List[WorkSegment]:
    """Parse standalone Antigravity transcript.jsonl into step-heartbeat WorkSegments."""
    segs: List[WorkSegment] = []
    try:
        st = file_path.stat()
    except OSError:
        return segs
    is_fresh = (now_ts - st.st_mtime) <= LIVE_STALE_SECONDS

    cur_turn_start: Optional[float] = None
    cur_steps: List[float] = []
    last_is_user = False
    turn_idx = 0
    conv_id = file_path.parent.parent.parent.name[:12]

    def flush(active: bool = False) -> None:
        nonlocal cur_turn_start, cur_steps, turn_idx
        if cur_turn_start is not None and cur_steps:
            turn_idx += 1
            segs.extend(
                build_heartbeat_segments(
                    tool="antigravity",
                    turn_start=cur_turn_start,
                    step_timestamps=cur_steps,
                    run_key=f"agy:{conv_id}:{turn_idx}",
                    label="Antigravity",
                    is_active=active,
                )
            )
        cur_turn_start = None
        cur_steps = []

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                ts = parse_ts(obj.get("created_at"))
                if ts is None:
                    continue
                otype = str(obj.get("type") or "")
                source = str(obj.get("source") or "")
                if otype == "USER_INPUT" or source == "USER_EXPLICIT":
                    flush(active=False)
                    cur_turn_start = ts
                    cur_steps = []
                    last_is_user = True
                else:
                    if cur_turn_start is not None:
                        cur_steps.append(ts)
                    last_is_user = False
    except OSError:
        return segs

    if cur_turn_start is not None:
        last_hb = max(cur_steps) if cur_steps else cur_turn_start
        if is_fresh and last_is_user and 0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS:
            cur_steps.append(now_ts)
            flush(active=True)
        else:
            flush(active=False)
    return segs


def collect_antigravity_standalone(brain_dir: Path, cutoff_ts: float, now_ts: float, cache: SegmentCache) -> List[WorkSegment]:
    if not brain_dir.is_dir():
        return []
    out: List[WorkSegment] = []
    scan_floor = cutoff_ts - 86400.0
    for conv_dir in brain_dir.iterdir():
        if not conv_dir.is_dir() or conv_dir.name.startswith("."):
            continue
        logs = conv_dir / ".system_generated" / "logs"
        tpath = logs / "transcript.jsonl"
        if not tpath.is_file():
            tpath = logs / "transcript_full.jsonl"
        if not tpath.is_file():
            continue
        try:
            if tpath.stat().st_mtime < scan_floor:
                continue
        except OSError:
            continue
        cached = cache.get(tpath, now_ts)
        if cached is not None:
            file_segs = cached
        else:
            file_segs = parse_antigravity_transcript(tpath, now_ts)
            cache.put(tpath, file_segs, now_ts)
        for seg in file_segs:
            if seg.end_ts >= cutoff_ts:
                out.append(seg)
    return out


def parse_claude_code_file(file_path: Path, now_ts: float) -> List[WorkSegment]:
    """Parse Claude Code JSONL using step-heartbeat logic (with turn_duration fallback)."""
    try:
        st = file_path.stat()
    except OSError:
        return []
    is_fresh = (now_ts - st.st_mtime) <= LIVE_STALE_SECONDS

    step_segs: List[WorkSegment] = []
    exact_fallback: List[WorkSegment] = []

    cur_turn_start: Optional[float] = None
    cur_steps: List[float] = []
    turn_idx = 0
    label = ""

    def flush(active: bool = False) -> None:
        nonlocal cur_turn_start, cur_steps, turn_idx
        if cur_turn_start is not None and cur_steps:
            turn_idx += 1
            step_segs.extend(
                build_heartbeat_segments(
                    tool="claude_code",
                    turn_start=cur_turn_start,
                    step_timestamps=cur_steps,
                    run_key=f"cc:{file_path.stem}:{turn_idx}",
                    label=label,
                    is_active=active,
                )
            )
        cur_turn_start = None
        cur_steps = []

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for line_no, raw in enumerate(f):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not label and obj.get("cwd"):
                    label = Path(str(obj["cwd"])).name
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
    except OSError:
        return []

    if cur_turn_start is not None:
        last_hb = max(cur_steps) if cur_steps else cur_turn_start
        if is_fresh and 0.0 <= now_ts - last_hb <= HEARTBEAT_GAP_SECONDS:
            cur_steps.append(now_ts)
            flush(active=True)
        else:
            flush(active=False)

    return step_segs if step_segs else exact_fallback


def collect_claude_code(projects_dir: Path, cutoff_ts: float, now_ts: float, cache: SegmentCache) -> List[WorkSegment]:
    if not projects_dir.is_dir():
        return []
    out: List[WorkSegment] = []
    scan_floor = cutoff_ts - 86400.0
    for root, _, files in os.walk(projects_dir):
        if "t3code-claude-title-" in root:
            continue
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            fpath = Path(root) / fname
            try:
                if fpath.stat().st_mtime < scan_floor:
                    continue
            except OSError:
                continue
            cached = cache.get(fpath, now_ts)
            if cached is not None:
                file_segs = cached
            else:
                file_segs = parse_claude_code_file(fpath, now_ts)
                cache.put(fpath, file_segs, now_ts)
            for seg in file_segs:
                if seg.end_ts >= cutoff_ts:
                    out.append(seg)
    return out


def parse_pi_file(file_path: Path) -> List[WorkSegment]:
    segs: List[WorkSegment] = []
    cur_turn_start: Optional[float] = None
    cur_steps: List[float] = []
    turn_idx = 0
    label = ""

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
                )
            )
        cur_turn_start = None
        cur_steps = []

    try:
        with file_path.open("r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                otype = obj.get("type")
                if otype == "session" and not label and obj.get("cwd"):
                    label = Path(str(obj["cwd"])).name
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
    except OSError:
        return segs
    flush()
    return segs


def collect_pi(sessions_dir: Path, cutoff_ts: float, now_ts: float, cache: SegmentCache) -> List[WorkSegment]:
    if not sessions_dir.is_dir():
        return []
    out: List[WorkSegment] = []
    scan_floor = cutoff_ts - 86400.0
    for root, _, files in os.walk(sessions_dir):
        for fname in files:
            if not fname.endswith(".jsonl"):
                continue
            fpath = Path(root) / fname
            try:
                if fpath.stat().st_mtime < scan_floor:
                    continue
            except OSError:
                continue
            cached = cache.get(fpath, now_ts)
            if cached is not None:
                file_segs = cached
            else:
                file_segs = parse_pi_file(fpath)
                cache.put(fpath, file_segs, now_ts)
            for seg in file_segs:
                if seg.end_ts >= cutoff_ts:
                    out.append(seg)
    return out


def collect_opencode(db_path: Path, cutoff_ts: float) -> List[WorkSegment]:
    segs: List[WorkSegment] = []
    with sqlite_snapshot(db_path) as conn:
        if conn is None:
            return segs
        try:
            rows = conn.execute(
                "SELECT session_id, time_created, data FROM message ORDER BY session_id ASC, time_created ASC"
            ).fetchall()
        except Exception:
            return segs

        cur_sid: Optional[str] = None
        cur_turn_start: Optional[float] = None
        cur_steps: List[float] = []
        turn_idx = 0

        def flush(sid: Optional[str]) -> None:
            nonlocal cur_turn_start, cur_steps, turn_idx
            if sid and cur_turn_start is not None and cur_steps:
                turn_idx += 1
                segs.extend(
                    build_heartbeat_segments(
                        tool="opencode",
                        turn_start=cur_turn_start,
                        step_timestamps=cur_steps,
                        run_key=f"oc:{sid}:{turn_idx}",
                        label="OpenCode",
                        is_active=False,
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

    return [s for s in segs if s.end_ts >= cutoff_ts]


def read_tokei_reference(tokei_path: Path) -> Dict[str, Any]:
    """Read-only peek at Tokei's last_usage.json for today's token/cost sanity check."""
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
# Aggregation & Snapshot Builder
# ---------------------------------------------------------------------------


def aggregate_segments(
    segments: List[WorkSegment],
    days: int = 14,
    now_ts: Optional[float] = None,
    tokei_ref: Optional[Dict[str, Any]] = None,
    daily_usage_map: Optional[Dict[str, Dict[str, Any]]] = None,
    usage_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Aggregate WorkSegments into daily metrics, 7-day summary, usage totals, and live active list."""
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
        if seg.is_active and seg.run_key not in seen_active_keys:
            seen_active_keys.add(seg.run_key)
            meta = TOOL_META.get(seg.tool, {"name": seg.tool, "short": seg.tool, "color": "#94A3B8"})
            active_agents.append(
                {
                    "tool": seg.tool,
                    "display_name": meta["name"],
                    "short_name": meta["short"],
                    "color": meta["color"],
                    "started_at": round(seg.start_ts, 3),
                    "elapsed_seconds": round(max(0.0, now - seg.start_ts), 1),
                    "label": seg.label,
                }
            )
        for day_str, chunk in split_segment_by_biz_day(seg):
            if day_str in day_set:
                by_day[day_str].append(chunk)

    active_agents.sort(key=lambda x: x["started_at"], reverse=True)
    usage_by_day = daily_usage_map or {}

    daily_list: List[Dict[str, Any]] = []
    for day_str in day_keys:
        day_segs = by_day[day_str]
        agent_sec = sum(s.duration for s in day_segs)
        wall_sec = merge_wall_seconds(day_segs)
        leverage = round(agent_sec / wall_sec, 2) if wall_sec >= 1.0 else 1.0
        runs = sum(1 for s in day_segs if s.is_turn_head)

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

        day_u = usage_by_day.get(day_str) or {}
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
                }
            )

        dt_obj = datetime.strptime(day_str, "%Y-%m-%d").replace(tzinfo=BIZ_TZ)
        daily_list.append(
            {
                "date": day_str,
                "short_date": dt_obj.strftime("%m/%d"),
                "weekday": WEEKDAY_CN[dt_obj.weekday()],
                "is_today": (day_str == day_keys[-1]),
                "agent_seconds": round(agent_sec, 1),
                "agent_hours": round(agent_sec / 3600.0, 2),
                "workdays": round(agent_sec / WORKDAY_SECONDS, 2),
                "wall_seconds": round(wall_sec, 1),
                "wall_hours": round(wall_sec / 3600.0, 2),
                "leverage": leverage,
                "runs": runs,
                "tools": tools_breakdown,
                "hourly_seconds": [round(x, 1) for x in hourly],
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

    return {
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
        "tokei_ref": resolved_tokei_ref,
        "usage": usage_payload,
    }


def collect_all(
    home: Optional[Path] = None,
    days: int = 14,
    cache_path: Optional[Path] = None,
    use_cache: bool = True,
    now_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Run all local read-only work-hours & token/cost collectors and return the unified JSON payload."""
    home_dir = home or Path.home()
    now = now_ts if now_ts is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=BIZ_TZ)
    data_dir = resolve_data_dir(home_dir, create=True)
    ledger_path = data_dir / "ledger.json"
    ledger = load_ledger(ledger_path)
    work_days = days if ledger.get("work_history_from") else max(days, 182)
    today = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    cutoff_ts = (today - timedelta(days=work_days - 1)).timestamp()
    cutoff_day = (today - timedelta(days=days - 1)).date().isoformat()
    if not use_cache:
        effective_cache_path = None
    else:
        effective_cache_path = cache_path if cache_path is not None else (data_dir / "cache.json")
    cache = SegmentCache(effective_cache_path)

    all_segments: List[WorkSegment] = []
    all_segments.extend(collect_t3_code(home_dir / ".t3" / "userdata" / "statev2.sqlite", cutoff_ts, now))
    all_segments.extend(collect_codex(home_dir / ".codex" / "sessions", cutoff_ts, now, cache))
    all_segments.extend(collect_workbuddy(home_dir / ".workbuddy" / "projects", cutoff_ts, now, cache))
    all_segments.extend(collect_hermes(home_dir / ".hermes", cutoff_ts, now))
    all_segments.extend(collect_antigravity_standalone(home_dir / ".gemini" / "antigravity" / "brain", cutoff_ts, now, cache))
    all_segments.extend(collect_claude_code(home_dir / ".claude" / "projects", cutoff_ts, now, cache))
    all_segments.extend(collect_pi(home_dir / ".pi" / "agent" / "sessions", cutoff_ts, now, cache))
    all_segments.extend(collect_opencode(home_dir / ".local" / "share" / "opencode" / "opencode.db", cutoff_ts))

    cache.save()

    # Build base work-hours daily list first so ledger can unify work-hours + token/cost per tool
    base_payload = aggregate_segments(all_segments, days=work_days, now_ts=now)

    # Native incremental Token, Cost & Project collection (zero runtime dependency on ~/.tokei)
    pricing_tbl = PricingTable.load(home=home_dir)
    live_projects: Dict[str, Dict[str, Dict[str, Any]]] = {}
    live_usage = collect_live_usage(
        home=home_dir,
        days=days,
        use_cache=use_cache,
        now_ts=now,
        pricing=pricing_tbl,
        projects_out=live_projects,
    )
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
        work_history_from=(today - timedelta(days=work_days - 1)).date().isoformat(),
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
    )


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


def render_markdown_report(payload: Dict[str, Any], target_date: Optional[str] = None) -> str:
    """Render a unified Markdown daily AI work-hours + token/cost report."""
    day = payload["today"]
    if target_date:
        for d in payload.get("days", []):
            if d["date"] == target_date:
                day = d
                break

    lines = [
        f"## AI 工时与用量日报 · {day['date']} ({day['weekday']})",
        "",
        f"- **AI 累计工时**：**{day['agent_hours']:.2f} h**（折合 **{day['workdays']:.2f} 个 AI 工作日**，按 8h/日）",
        f"- **并行杠杆率**：**{day['leverage']:.2f}×**（去重墙钟 {day['wall_hours']:.2f} h · 共 {day['runs']} 轮执行）",
    ]
    day_tok = int(day.get("tokens", 0) or 0)
    day_cost = float(day.get("cost_usd", 0.0) or 0.0)
    if day_tok > 0 or day_cost > 0:
        cache_pct = int(round(float(day.get("cache_hit_rate", 0.0) or 0.0) * 100))
        lines.append(
            f"- **Token 与估算费用**：**{_format_tokens_short(day_tok)} tokens**（估算 **${day_cost:.2f}** · 缓存命中 {cache_pct}%）"
        )
        if day.get("unpriced_tokens"):
            lines.append(f"- 另有 {_format_tokens_short(day['unpriced_tokens'])} Token 未计价。")

    if day["tools"]:
        lines.append("")
        lines.append("### 分工具明细（工时 + 用量）")
        lines.append("| AI 工具 | 工时 | 占比 | 轮次 | Token | 估算费用 |")
        lines.append("|---|---:|---:|---:|---:|---:|")
        for t in day["tools"]:
            pct = t["share"] * 100.0
            t_tok = int(t.get("tokens", 0) or 0)
            t_cost = float(t.get("cost_usd", 0.0) or 0.0)
            tok_str = _format_tokens_short(t_tok) if t_tok > 0 else "—"
            cost_str = f"${t_cost:.2f}" if t_cost > 0 else "—"
            lines.append(
                f"| {t['display_name']} | {format_duration_cn(t['agent_seconds'])} ({t['agent_hours']:.2f}h) | {pct:.0f}% | {t['runs']} | {tok_str} | {cost_str} |"
            )

    s7 = payload["summary_7d"]
    hb = int(HEARTBEAT_GAP_SECONDS // 60)
    lines.extend([
        "",
        f"> 近 7 天累计 **{s7['total_hours']:.2f} h**（日均 {s7['avg_daily_hours']:.2f} h，活跃 {s7['active_days']}/7 天）。口径：步间心跳 {hb}m（纯 AI 执行，剔除等人等待），自有增量 Token 引擎，T3↔Codex 双向去重。",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentPulse local AI work-hours & usage collector")
    parser.add_argument("--days", type=int, default=14, help="Number of days to aggregate (default 14)")
    parser.add_argument("--json", action="store_true", help="Output compact JSON (default)")
    parser.add_argument("--pretty", action="store_true", help="Output pretty-printed JSON")
    parser.add_argument("--markdown", action="store_true", help="Output Markdown daily report")
    parser.add_argument("--date", type=str, default=None, help="Target date YYYY-MM-DD for --markdown")
    parser.add_argument("--no-cache", action="store_true", help="Bypass incremental file cache")
    parser.add_argument("--restore-ledger", type=Path, metavar="BACKUP", help="Restore a validated ledger backup")
    args = parser.parse_args()

    t0 = time.perf_counter()
    try:
        if args.restore_ledger:
            saved = restore_ledger(resolve_data_dir() / "ledger.json", args.restore_ledger)
            print("账本已恢复。" + (f"恢复前副本：{saved}" if saved else ""))
            return 0
        payload = collect_all(days=max(7, min(args.days, 60)), use_cache=(not args.no_cache))
    except LedgerError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("无法读写 AgentPulse 本地数据，请检查文件权限和可用磁盘空间。", file=sys.stderr)
        return 1
    payload["elapsed_ms"] = round((time.perf_counter() - t0) * 1000.0, 1)

    if args.markdown:
        print(render_markdown_report(payload, target_date=args.date))
    elif args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
