#!/usr/bin/env python3
"""AgentPulse — Local read-only Token, Cost & Billing incremental collector (v0.7).

Strictly read-only on all AI client logs and SQLite databases (WAL-safe temp copies).
Zero external dependencies (Python 3.9+ stdlib only).

Key guarantees:
- Explicit business timezone: Asia/Shanghai (UTC+8)
- T3 Code <-> Codex bidirectional deduplication:
  Codex sessions with originator == "T3 Code" are attributed to `t3_codex`,
  standalone Codex sessions to `codex`, matching work-hours attribution 1:1.
- Per-source health tracking (`complete | partial | unavailable | not_configured`)
  with checked JSONL reading (pre/post file identity + 1 retry on truncated tail).
- Incremental file-level cache (v5, mtime_ns + size) at ~/.agent-pulse/usage_cache.json.
- Independent billing attribution (`subscription_candidate_usd` and Hermes verified
  `api_actual_usd` using Decimal arithmetic) without altering legacy `cost_usd`.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from health import (
    check_root_dir,
    combine_statuses,
    read_jsonl_checked,
    resolve_missing_source_status,
    seen_source_instances,
)
from pricing import PricingTable
from storage import sqlite_snapshot_with_status, write_json

BIZ_TZ = timezone(timedelta(hours=8))
USAGE_CACHE_VERSION = 5

_HERMES_ACTUAL_COST_SOURCES = frozenset({"provider_cost_api", "provider_generation_api"})
_HERMES_INCLUDED_MODES = frozenset({"subscription_included", "included"})
_HERMES_API_BILLING_MODES = frozenset({
    "api",
    "api_key",
    "payg",
    "pay_as_you_go",
    "byok",
    "direct_api",
    "metered",
    "credits",
    "openrouter",
})


def _normalize_project_path(raw: Any) -> str:
    if not isinstance(raw, str):
        return ""
    s = raw.rstrip("/")
    if not s or not s.startswith("/") or s in ("/", "/tmp", "/private/tmp", "/var/tmp"):
        return ""
    return s


def _parse_ts(value: Any) -> Optional[float]:
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
            return _parse_ts(float(s))
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


def _ts_to_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=BIZ_TZ).strftime("%Y-%m-%d")


def _safe_int(val: Any) -> int:
    try:
        v = int(val)
        return v if v > 0 else 0
    except (TypeError, ValueError):
        return 0


def _safe_float(val: Any) -> float:
    try:
        v = float(val)
        return v if v > 0 and math.isfinite(v) else 0.0
    except (TypeError, ValueError):
        return 0.0


def _safe_nonneg_decimal(val: Any) -> Optional[Decimal]:
    if val is None or isinstance(val, bool):
        return None
    try:
        f = float(val)
        if not math.isfinite(f) or f < 0:
            return None
        return Decimal(str(val)) if isinstance(val, (int, str)) else Decimal(str(f))
    except Exception:
        return None


def _add_billing_decimal(
    billing_acc: Dict[str, Dict[str, Dict[str, Decimal]]],
    tool: str,
    day: str,
    field: str,
    amount: Decimal,
) -> None:
    t_map = billing_acc.setdefault(tool, {})
    d_map = t_map.setdefault(day, {})
    d_map[field] = d_map.get(field, Decimal("0")) + amount


def _is_valid_day_str(val: Any) -> bool:
    if not isinstance(val, str) or len(val) != 10:
        return False
    try:
        return datetime.strptime(val, "%Y-%m-%d").strftime("%Y-%m-%d") == val
    except ValueError:
        return False


def _is_nonneg_int(val: Any) -> bool:
    return type(val) is int and val >= 0


def _is_nonneg_finite_num(val: Any) -> bool:
    return type(val) in (int, float) and math.isfinite(float(val)) and float(val) >= 0.0


def _is_valid_usage_cache_row(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    tool = row.get("tool")
    day = row.get("day")
    model = row.get("model")
    if not (isinstance(tool, str) and tool and _is_valid_day_str(day) and isinstance(model, str) and model):
        return False
    for int_key in ("in", "out", "cr", "cw", "thoughts", "requests", "sessions"):
        if int_key not in row or not _is_nonneg_int(row[int_key]):
            return False
    if "cost_usd" not in row or not _is_nonneg_finite_num(row["cost_usd"]):
        return False
    if "sub_cand" in row and not _is_nonneg_finite_num(row["sub_cand"]):
        return False
    if "project" in row and not isinstance(row["project"], str):
        return False
    if "unpriced_tokens" in row and not _is_nonneg_int(row["unpriced_tokens"]):
        return False
    if "dedup_id" in row and not (isinstance(row["dedup_id"], str) and row["dedup_id"]):
        return False
    if "session_id" in row and not (isinstance(row["session_id"], str) and row["session_id"]):
        return False
    return True


class UsageCache:
    """Caches parsed daily token deltas per source file using mtime_ns + size (v5)."""

    def __init__(self, path: Optional[Path], pricing_signature: str = ""):
        self.path = path
        self.pricing_signature = pricing_signature
        self.dirty = False
        self.files: Dict[str, Dict[str, Any]] = {}
        self.seen: set = set()
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if (
                    isinstance(raw, dict)
                    and raw.get("version") == USAGE_CACHE_VERSION
                    and raw.get("pricing_signature") == self.pricing_signature
                    and isinstance(raw.get("files"), dict)
                ):
                    self.files = raw["files"]
                else:
                    self.dirty = True
            except Exception:
                self.files = {}
                self.dirty = True

    def get(self, file_path: Path, *, require_codex_dedup: bool = False) -> Optional[List[Dict[str, Any]]]:
        try:
            st = file_path.stat()
        except OSError:
            return None
        key = hashlib.sha256(os.fsencode(file_path)).hexdigest()
        self.seen.add(key)
        entry = self.files.get(key)
        if (
            isinstance(entry, dict)
            and type(entry.get("mtime_ns")) is int
            and type(entry.get("size")) is int
            and entry["mtime_ns"] == st.st_mtime_ns
            and entry["size"] == st.st_size
            and isinstance(entry.get("rows"), list)
        ):
            if require_codex_dedup and entry.get("codex_dedup") is not True:
                self.dirty = True
                return None
            rows = entry["rows"]
            if all(_is_valid_usage_cache_row(r) for r in rows):
                return rows
            self.dirty = True
        return None

    def put(
        self,
        file_path: Path,
        rows: List[Dict[str, Any]],
        *,
        stat_result: Optional[os.stat_result] = None,
        codex_dedup: bool = False,
    ) -> None:
        try:
            st = stat_result if stat_result is not None else file_path.stat()
        except OSError:
            return
        key = hashlib.sha256(os.fsencode(file_path)).hexdigest()
        self.seen.add(key)
        entry: Dict[str, Any] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "rows": rows,
        }
        if codex_dedup:
            entry["codex_dedup"] = True
        self.files[key] = entry
        self.dirty = True

    def save(self) -> None:
        if not self.path or (not self.dirty and self.files.keys() <= self.seen):
            return
        try:
            write_json(
                self.path,
                {
                    "version": USAGE_CACHE_VERSION,
                    "pricing_signature": self.pricing_signature,
                    "files": {key: value for key, value in self.files.items() if key in self.seen},
                },
            )
        except (OSError, ValueError):
            pass


def _empty_bucket() -> Dict[str, Any]:
    return {
        "in": 0,
        "out": 0,
        "cr": 0,
        "cw": 0,
        "thoughts": 0,
        "cost_usd": 0.0,
        "requests": 0,
        "sessions": 0,
        "models": {},
    }


def _add_to_bucket(
    bucket: Dict[str, Any],
    model: str,
    inp: int,
    out: int,
    cr: int,
    cw: int,
    thoughts: int,
    cost_usd: float,
    requests: int = 1,
    sessions: int = 0,
) -> None:
    bucket["in"] += inp
    bucket["out"] += out
    bucket["cr"] += cr
    bucket["cw"] += cw
    bucket["thoughts"] += thoughts
    bucket["cost_usd"] = round(bucket["cost_usd"] + cost_usd, 6)
    bucket["requests"] += requests
    bucket["sessions"] = bucket.get("sessions", 0) + sessions

    mkey = (model or "unknown").strip() or "unknown"
    m = bucket["models"].setdefault(
        mkey,
        {"in": 0, "out": 0, "cr": 0, "cw": 0, "thoughts": 0, "cost_usd": 0.0, "requests": 0},
    )
    m["in"] += inp
    m["out"] += out
    m["cr"] += cr
    m["cw"] += cw
    m["thoughts"] += thoughts
    m["cost_usd"] = round(m["cost_usd"] + cost_usd, 6)
    m["requests"] += requests


def _merge_rows_into_result(
    result: Dict[str, Dict[str, Dict[str, Any]]],
    rows: List[Dict[str, Any]],
    cutoff_day: str,
    projects_out: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    billing_acc: Optional[Dict[str, Dict[str, Dict[str, Decimal]]]] = None,
    billing_cutoff_day: Optional[str] = None,
    pricing: Optional[PricingTable] = None,
) -> None:
    b_floor = billing_cutoff_day or cutoff_day
    for r in rows:
        if not isinstance(r, dict):
            continue
        day = r.get("day")
        tool = r.get("tool")
        if not isinstance(day, str) or not day or not isinstance(tool, str) or not tool:
            continue

        # Billing candidate accumulation can span from month start without altering legacy cutoff_day
        if billing_acc is not None and day >= b_floor:
            sub_cand = r.get("sub_cand")
            if sub_cand is not None:
                dec = _safe_nonneg_decimal(sub_cand)
                if dec is not None:
                    _add_billing_decimal(billing_acc, tool, day, "subscription_candidate_usd", dec)

        if day < cutoff_day:
            continue

        inp = _safe_int(r.get("in"))
        out = _safe_int(r.get("out"))
        cr = _safe_int(r.get("cr"))
        cw = _safe_int(r.get("cw"))
        thoughts = _safe_int(r.get("thoughts"))
        cost_usd = _safe_float(r.get("cost_usd"))
        reqs = _safe_int(r.get("requests") or 1)
        sess = _safe_int(r.get("sessions"))
        model = str(r.get("model") or "unknown")

        day_map = result.setdefault(tool, {})
        bucket = day_map.setdefault(day, _empty_bucket())
        _add_to_bucket(
            bucket,
            model=model,
            inp=inp,
            out=out,
            cr=cr,
            cw=cw,
            thoughts=thoughts,
            cost_usd=cost_usd,
            requests=reqs,
            sessions=sess,
        )

        proj = r.get("project")
        if projects_out is not None and isinstance(proj, str) and proj:
            p_days = projects_out.setdefault(proj, {})
            pb = p_days.setdefault(
                day,
                {
                    "in": 0,
                    "out": 0,
                    "cr": 0,
                    "cw": 0,
                    "thoughts": 0,
                    "cost_usd": 0.0,
                    "unpriced_tokens": 0,
                    "sessions": 0,
                    "tools": [],
                },
            )
            pb["in"] += inp
            pb["out"] += out
            pb["cr"] += cr
            pb["cw"] += cw
            pb["thoughts"] += thoughts
            pb["cost_usd"] = round(pb["cost_usd"] + cost_usd, 6)
            row_tok = inp + out + cr + cw + thoughts
            if "unpriced_tokens" in r:
                row_unp = min(row_tok, _safe_int(r.get("unpriced_tokens")))
            elif pricing is not None:
                row_unp = row_tok if (cost_usd == 0.0 and row_tok > 0 and pricing.resolve(model) is None) else 0
            else:
                row_unp = row_tok if (cost_usd == 0.0 and row_tok > 0) else 0
            pb["unpriced_tokens"] = pb.get("unpriced_tokens", 0) + row_unp
            pb["sessions"] += sess
            if tool not in pb["tools"]:
                pb["tools"].append(tool)


def _compact_rows(
    acc: Dict[Tuple[str, str, str], Dict[str, Any]],
    project: str = "",
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    seen_session_days: set = set()
    for (tool, day, model), v in acc.items():
        if v["in"] + v["out"] + v["cr"] + v["cw"] + v["thoughts"] <= 0 and v["cost_usd"] <= 0:
            continue
        s_key = (tool, day)
        sess_inc = 0
        if s_key not in seen_session_days:
            seen_session_days.add(s_key)
            sess_inc = 1
        row: Dict[str, Any] = {
            "tool": tool,
            "day": day,
            "model": model,
            "in": v["in"],
            "out": v["out"],
            "cr": v["cr"],
            "cw": v["cw"],
            "thoughts": v["thoughts"],
            "cost_usd": round(v["cost_usd"], 6),
            "requests": v["requests"],
            "sessions": sess_inc,
        }
        if v.get("has_sub_cand"):
            row["sub_cand"] = round(float(v["sub_cand"]), 6)
        if project:
            row["project"] = project
        rows.append(row)
    return rows


def _acc_add(
    acc: Dict[Tuple[str, str, str], Dict[str, Any]],
    tool: str,
    day: str,
    model: str,
    inp: int,
    out: int,
    cr: int,
    cw: int,
    thoughts: int,
    cost_usd: float,
    requests: int = 1,
    sub_cand_usd: Optional[float] = None,
) -> None:
    key = (tool, day, model or "unknown")
    entry = acc.setdefault(
        key,
        {
            "in": 0,
            "out": 0,
            "cr": 0,
            "cw": 0,
            "thoughts": 0,
            "cost_usd": 0.0,
            "requests": 0,
            "sub_cand": Decimal("0"),
            "has_sub_cand": False,
        },
    )
    entry["in"] += inp
    entry["out"] += out
    entry["cr"] += cr
    entry["cw"] += cw
    entry["thoughts"] += thoughts
    entry["cost_usd"] += cost_usd
    entry["requests"] += requests
    if sub_cand_usd is not None:
        dec = _safe_nonneg_decimal(sub_cand_usd)
        if dec is not None:
            entry["sub_cand"] += dec
            entry["has_sub_cand"] = True


def _walk_matching_files(root: Path, suffix: str, prefix: str = "") -> Tuple[List[Path], int]:
    """Walk `root` collecting files matching suffix/prefix; counts directory permission/OS errors."""
    paths: List[Path] = []
    dir_errors = 0

    def on_error(_exc: OSError) -> None:
        nonlocal dir_errors
        dir_errors += 1

    for cur_root, _, files in os.walk(root, onerror=on_error):
        for fname in files:
            if suffix and not fname.endswith(suffix):
                continue
            if prefix and not fname.startswith(prefix):
                continue
            paths.append(Path(cur_root) / fname)
    return paths, dir_errors


def _summarize_file_scan_health(files_ok: int, files_err: int, dir_err: int) -> str:
    if files_err == 0 and dir_err == 0:
        return "complete"
    if files_ok == 0:
        return "unavailable"
    return "partial"


# ---------------------------------------------------------------------------
# 1. Codex (with T3 Code <-> Codex bidirectional deduplication)
# ---------------------------------------------------------------------------

CodexTokens = Tuple[int, int, int, int, int]


@dataclass(frozen=True)
class CodexUsageEvent:
    timestamp: float
    model: str
    turn: int
    tokens: CodexTokens
    total: Optional[CodexTokens]
    response: bool
    turn_id: str = ""
    response_id: str = ""
    project_cwd: str = ""


def _codex_mirrored_events(events: List[CodexUsageEvent], response_totals: set) -> set:
    """Pair a delta-only response with at most one nearby legacy mirror."""
    legacy = defaultdict(deque)
    ordered = sorted(enumerate(events), key=lambda entry: entry[1].timestamp)
    for idx, event in ordered:
        if not event.response and (event.turn, event.total) not in response_totals:
            legacy[event.turn, event.model, event.tokens].append((idx, event.timestamp))

    duplicates = set()
    for _, event in ordered:
        if not event.response or event.total is not None:
            continue
        candidates = legacy.get((event.turn, event.model, event.tokens))
        if candidates is None:
            continue
        while candidates and candidates[0][1] < event.timestamp - 2:
            candidates.popleft()
        if candidates and candidates[0][1] <= event.timestamp + 2:
            duplicates.add(candidates.popleft()[0])
    return duplicates


def _parse_codex_file_checked(
    path: Path, pricing: PricingTable
) -> Tuple[List[Dict[str, Any]], bool, bool, Optional[os.stat_result]]:
    res = read_jsonl_checked(
        path,
        keyword_filter=(
            '"token_count"',
            '"token_usage_record"',
            '"session_meta"',
            '"turn_context"',
            '"task_started"',
            '"task_complete"',
            '"turn_aborted"',
            '"task_aborted"',
            '"turn_id"',
            '"payload"',
        ),
    )
    if res.permission_error or res.missing:
        return [], True, res.identity_changed, None

    originator = ""
    session_cwd = ""
    pending_turn_cwd = ""
    active_turn_id = ""
    current_turn_id = ""
    turn_cwds: Dict[str, str] = {}
    session_id: Optional[str] = None
    response_ids = set()
    current_model = "unknown"
    prev_total: Optional[CodexTokens] = None
    turn = 0
    events: List[CodexUsageEvent] = []
    response_totals: set = set()
    struct_error = False

    for _, obj in res.records:
        typ = obj.get("type")
        raw_payload = obj.get("payload")
        if raw_payload is not None and not isinstance(raw_payload, dict):
            struct_error = True
            continue
        payload = raw_payload if isinstance(raw_payload, dict) else {}

        if typ == "session_meta":
            sid = payload.get("id")
            if isinstance(sid, str) and sid.strip():
                session_id = sid.strip()
            orig = payload.get("originator")
            if isinstance(orig, str):
                originator = orig
            cwd = _normalize_project_path(payload.get("cwd"))
            if cwd:
                session_cwd = cwd
            continue

        if typ == "turn_context":
            turn += 1
            m = payload.get("model")
            if isinstance(m, str) and m.strip():
                current_model = m.strip()
            tc_tid = str(payload.get("turn_id") or payload.get("root_turn_id") or "")
            if tc_tid:
                current_turn_id = tc_tid
            tc_cwd = _normalize_project_path(payload.get("cwd"))
            if tc_cwd:
                if tc_tid:
                    turn_cwds[tc_tid] = tc_cwd
                elif active_turn_id:
                    turn_cwds[active_turn_id] = tc_cwd
                else:
                    pending_turn_cwd = tc_cwd
            continue

        if typ == "event_msg" and payload.get("type") in ("task_started", "task_complete", "turn_aborted", "task_aborted"):
            ptype = str(payload.get("type") or "")
            tid = str(payload.get("turn_id") or payload.get("root_turn_id") or "")
            if ptype == "task_started":
                active_turn_id = tid or f"turn-{turn + 1}"
                current_turn_id = tid or active_turn_id
                if pending_turn_cwd and active_turn_id not in turn_cwds:
                    turn_cwds[active_turn_id] = pending_turn_cwd
                pending_turn_cwd = ""
            else:
                if tid:
                    current_turn_id = tid
                active_turn_id = ""
                pending_turn_cwd = ""
            continue

        is_response = typ == "token_usage_record"
        response_id_str = ""
        if is_response:
            last = payload.get("usage")
            total = payload.get("thread_token_usage")
            if (last is not None and not isinstance(last, dict)) or (total is not None and not isinstance(total, dict)):
                struct_error = True
                continue
            owner = payload.get("thread_id")
            if isinstance(owner, str) and owner.strip() and not session_id:
                session_id = owner.strip()
            response_id = payload.get("response_id")
            if not isinstance(last, dict) or (owner and session_id and owner != session_id):
                continue
            if isinstance(response_id, str) and response_id:
                if response_id in response_ids:
                    continue
                response_ids.add(response_id)
                response_id_str = response_id
        elif typ == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info")
            if info is None:
                continue
            if not isinstance(info, dict):
                struct_error = True
                continue
            total = info.get("total_token_usage")
            last = info.get("last_token_usage")
            if (total is not None and not isinstance(total, dict)) or (last is not None and not isinstance(last, dict)):
                struct_error = True
                continue
        else:
            continue

        ts = _parse_ts(obj.get("timestamp"))
        if ts is None:
            struct_error = True
            continue

        d_in = d_cr = d_cw = d_out = d_reason = 0
        cur_tuple = None
        if isinstance(total, dict):
            cur_in = _safe_int(total.get("input_tokens"))
            cur_cr = _safe_int(total.get("cached_input_tokens"))
            cur_cw = _safe_int(total.get("cache_write_input_tokens"))
            cur_out = _safe_int(total.get("output_tokens"))
            cur_r = _safe_int(total.get("reasoning_output_tokens"))
            cur_tuple = (cur_in, cur_cr, cur_cw, cur_out, cur_r)
            if is_response:
                response_totals.add((turn, cur_tuple))
            if prev_total is not None:
                if cur_tuple == prev_total:
                    continue
                if (
                    cur_in >= prev_total[0]
                    and cur_cr >= prev_total[1]
                    and cur_cw >= prev_total[2]
                    and cur_out >= prev_total[3]
                    and cur_r >= prev_total[4]
                ):
                    d_in = cur_in - prev_total[0]
                    d_cr = cur_cr - prev_total[1]
                    d_cw = cur_cw - prev_total[2]
                    d_out = cur_out - prev_total[3]
                    d_reason = cur_r - prev_total[4]
                elif isinstance(last, dict):
                    d_in = _safe_int(last.get("input_tokens"))
                    d_cr = _safe_int(last.get("cached_input_tokens"))
                    d_cw = _safe_int(last.get("cache_write_input_tokens"))
                    d_out = _safe_int(last.get("output_tokens"))
                    d_reason = _safe_int(last.get("reasoning_output_tokens"))
            else:
                d_in, d_cr, d_cw, d_out, d_reason = cur_tuple
            if is_response:
                d_in, d_cr, d_cw, d_out, d_reason = (
                    _safe_int(last.get(key))
                    for key in (
                        "input_tokens",
                        "cached_input_tokens",
                        "cache_write_input_tokens",
                        "output_tokens",
                        "reasoning_output_tokens",
                    )
                )
            prev_total = cur_tuple
        elif isinstance(last, dict):
            d_in = _safe_int(last.get("input_tokens"))
            d_cr = _safe_int(last.get("cached_input_tokens"))
            d_cw = _safe_int(last.get("cache_write_input_tokens"))
            d_out = _safe_int(last.get("output_tokens"))
            d_reason = _safe_int(last.get("reasoning_output_tokens"))

        if d_in + d_out + d_cr + d_cw + d_reason <= 0:
            continue
        ev_turn_id = str(payload.get("turn_id") or payload.get("root_turn_id") or current_turn_id or "")
        ev_cwd = (
            (turn_cwds.get(ev_turn_id) if ev_turn_id else None)
            or (turn_cwds.get(active_turn_id) if active_turn_id else None)
            or pending_turn_cwd
            or session_cwd
        )
        events.append(
            CodexUsageEvent(
                ts,
                current_model,
                turn,
                (d_in, d_cr, d_cw, d_out, d_reason),
                cur_tuple,
                is_response,
                turn_id=ev_turn_id,
                response_id=response_id_str,
                project_cwd=ev_cwd,
            )
        )

    duplicates = _codex_mirrored_events(events, response_totals)
    tool_key = "t3_codex" if originator == "T3 Code" else "codex"
    rows: List[Dict[str, Any]] = []
    seen_session_days: set = set()
    occ_counts: Dict[Tuple[Any, ...], int] = {}

    for index, event in enumerate(events):
        if index in duplicates:
            continue
        inp, cr, cw, out, reasoning = event.tokens
        non_cached_in = max(0, inp - cr - cw)
        non_reason_out = max(0, out - reasoning)
        tot_tok = non_cached_in + non_reason_out + cr + cw + reasoning
        rates = pricing.resolve(event.model)
        cost = pricing.estimate_cost(event.model, non_cached_in, non_reason_out, cr, cw, reasoning)
        sub_cand = cost if rates is not None else None
        unpriced = tot_tok if (cost == 0.0 and rates is None) else 0
        day = _ts_to_day(event.timestamp)

        s_key = (tool_key, day)
        sess_inc = 0
        if s_key not in seen_session_days:
            seen_session_days.add(s_key)
            sess_inc = 1

        row: Dict[str, Any] = {
            "tool": tool_key,
            "day": day,
            "model": event.model or "unknown",
            "in": non_cached_in,
            "out": non_reason_out,
            "cr": cr,
            "cw": cw,
            "thoughts": reasoning,
            "cost_usd": round(cost, 6),
            "unpriced_tokens": unpriced,
            "requests": 1,
            "sessions": sess_inc,
        }
        if sub_cand is not None:
            row["sub_cand"] = round(float(sub_cand), 6)
        if event.project_cwd:
            row["project"] = event.project_cwd

        if session_id:
            row["session_id"] = session_id
            if event.response_id:
                dedup_raw = f"resp|{tool_key}|{session_id}|{event.response_id}"
            elif event.total is not None:
                base_sig = (
                    "tot",
                    tool_key,
                    session_id,
                    event.turn_id,
                    round(event.timestamp, 3),
                    event.model,
                    event.total,
                )
                occ = occ_counts.get(base_sig, 0)
                occ_counts[base_sig] = occ + 1
                dedup_raw = f"tot|{tool_key}|{session_id}|{event.turn_id}|{round(event.timestamp, 3)}|{event.model}|{event.total}|{occ}"
            else:
                base_sig = (
                    "ev",
                    tool_key,
                    session_id,
                    event.turn_id,
                    round(event.timestamp, 3),
                    event.model,
                    event.tokens,
                )
                occ = occ_counts.get(base_sig, 0)
                occ_counts[base_sig] = occ + 1
                dedup_raw = f"ev|{tool_key}|{session_id}|{event.turn_id}|{round(event.timestamp, 3)}|{event.model}|{event.tokens}|{occ}"
            row["dedup_id"] = hashlib.sha256(dedup_raw.encode("utf-8")).hexdigest()[:32]
        rows.append(row)

    had_err = res.had_error or struct_error
    return rows, had_err, res.identity_changed, res.stat_result


def _parse_codex_file(path: Path, pricing: PricingTable) -> List[Dict[str, Any]]:
    rows, _, _, _ = _parse_codex_file_checked(path, pricing)
    return rows


def collect_codex_usage(
    sessions_root: Path,
    cutoff_ts: float,
    cutoff_day: str,
    cache: UsageCache,
    pricing: PricingTable,
    result: Dict[str, Any],
    projects_out: Optional[Dict[str, Any]] = None,
    billing_acc: Optional[Dict[str, Dict[str, Dict[str, Decimal]]]] = None,
    billing_cutoff_day: Optional[str] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> str:
    can_scan, early = check_root_dir(sessions_root, "usage.codex", prev_sources or {})
    if not can_scan:
        return early or "not_configured"

    files, dir_err = _walk_matching_files(sessions_root, ".jsonl")
    files.sort(key=lambda p: str(p))
    files_ok = 0
    files_err = 0
    seen_event_ids: set = set()
    seen_session_days: set = set()

    for path in files:
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                files_ok += 1
                continue
        except OSError:
            files_err += 1
            continue
        rows = cache.get(path, require_codex_dedup=True)
        if rows is None:
            rows, had_err, changed, st_res = _parse_codex_file_checked(path, pricing)
            if had_err or changed:
                files_err += 1
                continue
            cache.put(path, rows, stat_result=st_res, codex_dedup=True)
        files_ok += 1

        deduped_rows: List[Dict[str, Any]] = []
        for r in rows:
            did = r.get("dedup_id")
            if did:
                if did in seen_event_ids:
                    continue
                seen_event_ids.add(did)
            sid = r.get("session_id")
            sess_key = (r["tool"], r["day"], f"sid:{sid}" if sid else f"file:{path}")
            row_copy = dict(r)
            if sess_key in seen_session_days:
                row_copy["sessions"] = 0
            else:
                seen_session_days.add(sess_key)
                row_copy["sessions"] = 1
            deduped_rows.append(row_copy)

        if deduped_rows:
            _merge_rows_into_result(
                result,
                deduped_rows,
                cutoff_day,
                projects_out=projects_out,
                billing_acc=billing_acc,
                billing_cutoff_day=billing_cutoff_day,
                pricing=pricing,
            )
    return _summarize_file_scan_health(files_ok, files_err, dir_err)


# ---------------------------------------------------------------------------
# 2. WorkBuddy & CodeBuddy
# ---------------------------------------------------------------------------


def _pick_int(d: Dict[str, Any], *keys: str) -> int:
    for k in keys:
        if k in d and d[k] is not None:
            v = _safe_int(d[k])
            if v > 0:
                return v
    return 0


def _detail_int(d: Any, *keys: str) -> int:
    if not isinstance(d, dict):
        return 0
    return _pick_int(d, *keys)


def _parse_workbuddy_file_checked(
    path: Path, tool_key: str, pricing: PricingTable
) -> Tuple[List[Dict[str, Any]], bool, bool, Optional[os.stat_result]]:
    res = read_jsonl_checked(
        path,
        keyword_filter=('"usage"', '"rawUsage"', '"cwd"'),
    )
    if res.permission_error or res.missing:
        return [], True, res.identity_changed, None

    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    seen_dedup: set = set()
    project_cwd = ""

    for line_no, item in res.records:
        if not project_cwd:
            project_cwd = _normalize_project_path(item.get("cwd"))
        message = item.get("message") if isinstance(item.get("message"), dict) else {}
        provider = item.get("providerData") or message.get("providerData") or {}
        if not isinstance(provider, dict):
            provider = {}

        sources = [
            x
            for x in (message.get("usage"), provider.get("usage"), provider.get("rawUsage"), item.get("usage"))
            if isinstance(x, dict) and x
        ]
        if not sources:
            continue

        ts = _parse_ts(item.get("timestamp") or item.get("createdAt") or item.get("ts"))
        if ts is None:
            continue

        selected = None
        inp_total = out_total = 0
        for src in sources:
            i_v = _pick_int(src, "input_tokens", "inputTokens", "input", "prompt_tokens")
            o_v = _pick_int(src, "output_tokens", "outputTokens", "output", "completion_tokens")
            if i_v + o_v > 0:
                selected = src
                inp_total = i_v
                out_total = o_v
                break
        if selected is None:
            continue

        cr = 0
        cw = 0
        thoughts = 0
        total_candidates: List[int] = []
        for src in sources:
            cr = max(
                cr,
                _pick_int(
                    src,
                    "cache_read_input_tokens",
                    "cacheReadInputTokens",
                    "cache_read",
                    "cacheRead",
                    "cached_tokens",
                    "cachedTokens",
                    "prompt_cache_hit_tokens",
                ),
                _detail_int(src.get("inputTokensDetails"), "cached_tokens", "cachedTokens"),
                _detail_int(src.get("input_tokens_details"), "cached_tokens", "cachedTokens"),
                _detail_int(src.get("prompt_tokens_details"), "cached_tokens", "cachedTokens"),
            )
            cw = max(
                cw,
                _pick_int(
                    src,
                    "cache_creation_input_tokens",
                    "cacheCreationInputTokens",
                    "cache_write_input_tokens",
                    "cacheWriteInputTokens",
                    "prompt_cache_write_tokens",
                    "cache_write",
                    "cacheWrite",
                ),
            )
            thoughts = max(
                thoughts,
                _pick_int(src, "reasoning_tokens", "reasoningTokens", "thinking_tokens"),
                _detail_int(src.get("output_tokens_details"), "reasoning_tokens", "thinking_tokens"),
                _detail_int(src.get("outputTokensDetails"), "reasoningTokens", "thinkingTokens"),
            )
            tot = _pick_int(src, "total_tokens", "totalTokens", "total")
            if tot > 0:
                total_candidates.append(tot)

        dedup_id = message.get("id") or item.get("id") or f"{line_no + 1}"
        dedup_key = (str(dedup_id), int(ts))
        if dedup_key in seen_dedup:
            continue
        seen_dedup.add(dedup_key)

        inclusive_input = any(tot == inp_total + out_total for tot in total_candidates)
        if inclusive_input or cr + cw <= inp_total:
            non_cached_in = max(0, inp_total - cr - cw)
        else:
            non_cached_in = inp_total

        non_reason_out = max(0, out_total - thoughts) if thoughts <= out_total else out_total

        model = (
            provider.get("modelName")
            or provider.get("model")
            or provider.get("modelId")
            or message.get("model")
            or item.get("model")
            or "unknown"
        )
        if not isinstance(model, str):
            model = "unknown"

        cost = _safe_float(selected.get("cost")) or _safe_float(provider.get("cost"))
        if cost <= 0:
            cost = pricing.estimate_cost(
                model,
                input_tokens=non_cached_in,
                output_tokens=non_reason_out,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                reasoning_tokens=thoughts,
            )

        _acc_add(
            acc,
            tool=tool_key,
            day=_ts_to_day(ts),
            model=model,
            inp=non_cached_in,
            out=non_reason_out,
            cr=cr,
            cw=cw,
            thoughts=thoughts,
            cost_usd=cost,
            requests=1,
        )
    return _compact_rows(acc, project=project_cwd), res.had_error, res.identity_changed, res.stat_result


def _parse_workbuddy_file(path: Path, tool_key: str, pricing: PricingTable) -> List[Dict[str, Any]]:
    rows, _, _, _ = _parse_workbuddy_file_checked(path, tool_key, pricing)
    return rows


def collect_workbuddy_usage(
    root: Path,
    tool_key: str,
    cutoff_ts: float,
    cutoff_day: str,
    cache: UsageCache,
    pricing: PricingTable,
    result: Dict[str, Any],
    projects_out: Optional[Dict[str, Any]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> str:
    source_id = f"usage.{tool_key}"
    can_scan, early = check_root_dir(root, source_id, prev_sources or {})
    if not can_scan:
        return early or "not_configured"

    files, dir_err = _walk_matching_files(root, ".jsonl")
    files_ok = 0
    files_err = 0
    for path in files:
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                files_ok += 1
                continue
        except OSError:
            files_err += 1
            continue
        rows = cache.get(path)
        if rows is None:
            rows, had_err, changed, st_res = _parse_workbuddy_file_checked(path, tool_key, pricing)
            if had_err or changed:
                files_err += 1
                continue
            cache.put(path, rows, stat_result=st_res)
        files_ok += 1
        _merge_rows_into_result(result, rows, cutoff_day, projects_out=projects_out, pricing=pricing)
    return _summarize_file_scan_health(files_ok, files_err, dir_err)


# ---------------------------------------------------------------------------
# 3. Claude Code
# ---------------------------------------------------------------------------


def _parse_claude_file_checked(
    path: Path, pricing: PricingTable
) -> Tuple[List[Dict[str, Any]], bool, bool, Optional[os.stat_result]]:
    res = read_jsonl_checked(path, keyword_filter=('"usage"', '"cwd"'))
    if res.permission_error or res.missing:
        return [], True, res.identity_changed, None

    by_msg: Dict[str, Tuple[float, str, int, int, int, int, int]] = {}
    fallback_seq = 0
    project_cwd = ""

    for _, obj in res.records:
        if not project_cwd:
            project_cwd = _normalize_project_path(obj.get("cwd"))
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        usage = msg.get("usage")
        if not isinstance(usage, dict):
            continue
        ts = _parse_ts(obj.get("timestamp"))
        if ts is None:
            continue

        inp = _safe_int(usage.get("input_tokens"))
        out = _safe_int(usage.get("output_tokens"))
        cr = _safe_int(usage.get("cache_read_input_tokens"))
        cw = _safe_int(usage.get("cache_creation_input_tokens"))
        thoughts = _detail_int(usage.get("output_tokens_details"), "thinking_tokens", "reasoning_tokens")
        if inp + out + cr + cw + thoughts <= 0:
            continue

        model = str(msg.get("model") or "claude")
        msg_id = msg.get("id") or obj.get("requestId")
        if not msg_id:
            fallback_seq += 1
            msg_id = f"seq-{fallback_seq}"
        msg_id = str(msg_id)
        prev = by_msg.get(msg_id)
        if prev is None or out >= prev[3]:
            by_msg[msg_id] = (ts, model, inp, out, cr, cw, thoughts)

    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for ts, model, inp, out, cr, cw, thoughts in by_msg.values():
        non_reason_out = max(0, out - thoughts) if thoughts <= out else out
        rates = pricing.resolve(model)
        cost = pricing.estimate_cost(
            model,
            input_tokens=inp,
            output_tokens=non_reason_out,
            cache_read_tokens=cr,
            cache_write_tokens=cw,
            reasoning_tokens=thoughts,
        )
        sub_cand = cost if rates is not None else None
        _acc_add(
            acc,
            tool="claude_code",
            day=_ts_to_day(ts),
            model=model,
            inp=inp,
            out=non_reason_out,
            cr=cr,
            cw=cw,
            thoughts=thoughts,
            cost_usd=cost,
            requests=1,
            sub_cand_usd=sub_cand,
        )
    return _compact_rows(acc, project=project_cwd), res.had_error, res.identity_changed, res.stat_result


def _parse_claude_file(path: Path, pricing: PricingTable) -> List[Dict[str, Any]]:
    rows, _, _, _ = _parse_claude_file_checked(path, pricing)
    return rows


def collect_claude_usage(
    projects_root: Path,
    cutoff_ts: float,
    cutoff_day: str,
    cache: UsageCache,
    pricing: PricingTable,
    result: Dict[str, Any],
    projects_out: Optional[Dict[str, Any]] = None,
    billing_acc: Optional[Dict[str, Dict[str, Dict[str, Decimal]]]] = None,
    billing_cutoff_day: Optional[str] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> str:
    can_scan, early = check_root_dir(projects_root, "usage.claude_code", prev_sources or {})
    if not can_scan:
        return early or "not_configured"

    files, dir_err = _walk_matching_files(projects_root, ".jsonl")
    files_ok = 0
    files_err = 0
    for path in files:
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                files_ok += 1
                continue
        except OSError:
            files_err += 1
            continue
        rows = cache.get(path)
        if rows is None:
            rows, had_err, changed, st_res = _parse_claude_file_checked(path, pricing)
            if had_err or changed:
                files_err += 1
                continue
            cache.put(path, rows, stat_result=st_res)
        files_ok += 1
        _merge_rows_into_result(
            result,
            rows,
            cutoff_day,
            projects_out=projects_out,
            billing_acc=billing_acc,
            billing_cutoff_day=billing_cutoff_day,
            pricing=pricing,
        )
    return _summarize_file_scan_health(files_ok, files_err, dir_err)


# ---------------------------------------------------------------------------
# 4. Pi Agent
# ---------------------------------------------------------------------------


def _parse_pi_file_checked(
    path: Path, pricing: PricingTable
) -> Tuple[List[Dict[str, Any]], bool, bool, Optional[os.stat_result]]:
    res = read_jsonl_checked(path, keyword_filter=('"usage"', '"session"'))
    if res.permission_error or res.missing:
        return [], True, res.identity_changed, None

    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    project_cwd = ""

    for _, obj in res.records:
        if not project_cwd and obj.get("type") == "session":
            project_cwd = _normalize_project_path(obj.get("cwd"))
        msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
        usage = msg.get("usage") or obj.get("usage")
        if not isinstance(usage, dict):
            continue
        ts = _parse_ts(obj.get("timestamp") or msg.get("timestamp"))
        if ts is None:
            continue

        inp = _pick_int(usage, "input", "input_tokens", "inputTokens")
        out = _pick_int(usage, "output", "output_tokens", "outputTokens")
        cr = _pick_int(usage, "cacheRead", "cache_read", "cache_read_input_tokens")
        cw = _pick_int(usage, "cacheWrite", "cache_write", "cache_creation_input_tokens")
        thoughts = _pick_int(usage, "reasoning", "thoughts", "thinking_tokens")
        if inp + out + cr + cw + thoughts <= 0:
            continue

        model = str(msg.get("model") or obj.get("model") or "unknown")
        cost_obj = usage.get("cost")
        cost = 0.0
        if isinstance(cost_obj, dict):
            cost = _safe_float(cost_obj.get("total"))
        elif isinstance(cost_obj, (int, float)):
            cost = _safe_float(cost_obj)
        if cost <= 0:
            cost = pricing.estimate_cost(
                model,
                input_tokens=inp,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_write_tokens=cw,
                reasoning_tokens=thoughts,
            )
        _acc_add(
            acc,
            tool="pi",
            day=_ts_to_day(ts),
            model=model,
            inp=inp,
            out=out,
            cr=cr,
            cw=cw,
            thoughts=thoughts,
            cost_usd=cost,
            requests=1,
        )
    return _compact_rows(acc, project=project_cwd), res.had_error, res.identity_changed, res.stat_result


def _parse_pi_file(path: Path, pricing: PricingTable) -> List[Dict[str, Any]]:
    rows, _, _, _ = _parse_pi_file_checked(path, pricing)
    return rows


def collect_pi_usage(
    sessions_root: Path,
    cutoff_ts: float,
    cutoff_day: str,
    cache: UsageCache,
    pricing: PricingTable,
    result: Dict[str, Any],
    projects_out: Optional[Dict[str, Any]] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
) -> str:
    can_scan, early = check_root_dir(sessions_root, "usage.pi", prev_sources or {})
    if not can_scan:
        return early or "not_configured"

    files, dir_err = _walk_matching_files(sessions_root, ".jsonl")
    files_ok = 0
    files_err = 0
    for path in files:
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                files_ok += 1
                continue
        except OSError:
            files_err += 1
            continue
        rows = cache.get(path)
        if rows is None:
            rows, had_err, changed, st_res = _parse_pi_file_checked(path, pricing)
            if had_err or changed:
                files_err += 1
                continue
            cache.put(path, rows, stat_result=st_res)
        files_ok += 1
        _merge_rows_into_result(result, rows, cutoff_day, projects_out=projects_out, pricing=pricing)
    return _summarize_file_scan_health(files_ok, files_err, dir_err)


# ---------------------------------------------------------------------------
# 5. Hermes SQLite (Main + Profiles, WAL-safe snapshot)
# ---------------------------------------------------------------------------


def collect_hermes_usage(
    hermes_root: Path,
    cutoff_ts: float,
    cutoff_day: str,
    pricing: PricingTable,
    result: Dict[str, Any],
    billing_acc: Optional[Dict[str, Dict[str, Dict[str, Decimal]]]] = None,
    billing_cutoff_day: Optional[str] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
    observed_instances_out: Optional[Dict[str, set]] = None,
) -> str:
    can_scan, early = check_root_dir(hermes_root, "usage.hermes", prev_sources or {})
    if not can_scan:
        return early or "not_configured"

    db_entries: List[Tuple[str, Path]] = []
    discovered_instances: set = set()
    if (hermes_root / "state.db").exists():
        db_entries.append(("", hermes_root / "state.db"))
        discovered_instances.add("state.db")
    profiles_dir = hermes_root / "profiles"
    dir_err = 0
    if profiles_dir.exists():
        try:
            for prof in sorted(profiles_dir.iterdir()):
                if not prof.is_dir():
                    continue
                cand = prof / "state.db"
                if cand.exists():
                    db_entries.append((prof.name, cand))
                    discovered_instances.add(f"profiles/{prof.name}/state.db")
        except OSError:
            dir_err += 1

    if observed_instances_out is not None:
        observed_instances_out["usage.hermes"] = set(discovered_instances)

    prev_insts = seen_source_instances("usage.hermes", prev_sources or {})
    missing_prev_insts = prev_insts - discovered_instances

    if not db_entries and dir_err == 0 and not missing_prev_insts:
        return "complete"

    b_floor = billing_cutoff_day or cutoff_day
    db_statuses: List[str] = []
    seen_actual_keys: set = set()

    for profile_tag, db_path in db_entries:
        with sqlite_snapshot_with_status(db_path) as (conn, snap_status):
            if snap_status != "ok" or conn is None:
                db_statuses.append("unavailable")
                continue
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error:
                db_statuses.append("unavailable")
                continue

            acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            used_detail_table = False
            db_failed = False

            # First check Hermes v2 `session_model_usage` (supports billing_mode & verified API actual cost)
            if "session_model_usage" in tables:
                try:
                    cols = {r[1] for r in conn.execute('PRAGMA table_info("session_model_usage")').fetchall()}
                    if "input_tokens" in cols or "output_tokens" in cols or "actual_cost_usd" in cols:
                        sel_parts = [
                            "rowid AS _rid",
                            ("session_id" if "session_id" in cols else "''") + " AS session_id",
                            ("model" if "model" in cols else "''") + " AS model",
                            ("billing_provider" if "billing_provider" in cols else "''") + " AS billing_provider",
                            ("billing_base_url" if "billing_base_url" in cols else "''") + " AS billing_base_url",
                            ("billing_mode" if "billing_mode" in cols else "''") + " AS billing_mode",
                            ("task" if "task" in cols else "''") + " AS task",
                            ("input_tokens" if "input_tokens" in cols else "0") + " AS input_tokens",
                            ("output_tokens" if "output_tokens" in cols else "0") + " AS output_tokens",
                            ("cache_read_tokens" if "cache_read_tokens" in cols else "0") + " AS cr",
                            ("cache_write_tokens" if "cache_write_tokens" in cols else "0") + " AS cw",
                            ("reasoning_tokens" if "reasoning_tokens" in cols else "0") + " AS r",
                            ("actual_cost_usd" if "actual_cost_usd" in cols else "NULL") + " AS actual_cost_usd",
                            ("estimated_cost_usd" if "estimated_cost_usd" in cols else "NULL") + " AS estimated_cost_usd",
                            ("cost_status" if "cost_status" in cols else "''") + " AS cost_status",
                            ("cost_source" if "cost_source" in cols else "''") + " AS cost_source",
                            ("first_seen" if "first_seen" in cols else "NULL") + " AS first_seen",
                            ("last_seen" if "last_seen" in cols else "NULL") + " AS last_seen",
                        ]
                        q = f'SELECT {", ".join(sel_parts)} FROM "session_model_usage"'
                        rows = conn.execute(q).fetchall()
                        if rows:
                            used_detail_table = True
                        for row in rows:
                            last_ts = _parse_ts(row["last_seen"])
                            fallback_ts = last_ts if last_ts is not None else _parse_ts(row["first_seen"])
                            inp = _safe_int(row["input_tokens"])
                            out = _safe_int(row["output_tokens"])
                            cr = _safe_int(row["cr"])
                            cw = _safe_int(row["cw"])
                            reason = _safe_int(row["r"])
                            model = str(row["model"] or "hermes")

                            # 1) Verified API actual & subscription_included candidate (strictly per spec 2.6)
                            if billing_acc is not None and last_ts is not None:
                                b_day = _ts_to_day(last_ts)
                                if b_day >= b_floor:
                                    bmode = str(row["billing_mode"] or "").strip().lower()
                                    cstatus = str(row["cost_status"] or "").strip().lower()
                                    csource = str(row["cost_source"] or "").strip().lower()

                                    # Unique key prevents double-counting detail rows
                                    uniq_key = (
                                        profile_tag,
                                        str(row["session_id"] or ""),
                                        model,
                                        str(row["billing_provider"] or ""),
                                        str(row["billing_base_url"] or ""),
                                        bmode,
                                        str(row["task"] or ""),
                                        str(row["first_seen"] or ""),
                                        str(row["last_seen"] or ""),
                                        int(row["_rid"]),
                                    )
                                    if uniq_key not in seen_actual_keys:
                                        seen_actual_keys.add(uniq_key)
                                        if (
                                            cstatus == "actual"
                                            and csource in _HERMES_ACTUAL_COST_SOURCES
                                            and bmode in _HERMES_API_BILLING_MODES
                                        ):
                                            actual_dec = _safe_nonneg_decimal(row["actual_cost_usd"])
                                            if actual_dec is not None:
                                                _add_billing_decimal(
                                                    billing_acc, "hermes", b_day, "api_actual_usd", actual_dec
                                                )
                                        elif bmode == "subscription_included":
                                            if (
                                                inp + out + cr + cw + reason > 0
                                                and pricing.resolve(model) is not None
                                            ):
                                                cand = pricing.estimate_cost(model, inp, out, cr, cw, reason)
                                                cand_dec = _safe_nonneg_decimal(cand)
                                                if cand_dec is not None:
                                                    _add_billing_decimal(
                                                        billing_acc,
                                                        "hermes",
                                                        b_day,
                                                        "subscription_candidate_usd",
                                                        cand_dec,
                                                    )

                            # 2) Legacy usage bucket (tokens & estimated cost)
                            if fallback_ts is None or fallback_ts < cutoff_ts:
                                continue
                            if inp + out + cr + cw + reason <= 0:
                                continue
                            cost = _safe_float(row["actual_cost_usd"]) or _safe_float(row["estimated_cost_usd"])
                            if cost <= 0:
                                cost = pricing.estimate_cost(model, inp, out, cr, cw, reason)
                            _acc_add(acc, "hermes", _ts_to_day(fallback_ts), model, inp, out, cr, cw, reason, cost, 1)
                except sqlite3.Error:
                    db_failed = True

            # Fallback fine-grained usage tables for older Hermes schemas
            if not used_detail_table and not db_failed:
                for tbl in ("model_usage", "token_usage", "session_usage", "usage"):
                    if tbl not in tables:
                        continue
                    try:
                        cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
                        if "input_tokens" not in cols and "output_tokens" not in cols:
                            continue
                        ts_col = (
                            "last_seen"
                            if "last_seen" in cols
                            else ("first_seen" if "first_seen" in cols else ("created_at" if "created_at" in cols else None))
                        )
                        model_col = "model" if "model" in cols else "''"
                        cr_col = "cache_read_tokens" if "cache_read_tokens" in cols else "0"
                        cw_col = "cache_write_tokens" if "cache_write_tokens" in cols else "0"
                        r_col = "reasoning_tokens" if "reasoning_tokens" in cols else "0"
                        cost_col = (
                            "actual_cost_usd"
                            if "actual_cost_usd" in cols
                            else ("estimated_cost_usd" if "estimated_cost_usd" in cols else "0")
                        )
                        q = (
                            f'SELECT {model_col} AS model, input_tokens, output_tokens, {cr_col} AS cr, '
                            f'{cw_col} AS cw, {r_col} AS r, {cost_col} AS cost_usd, '
                            f'{ts_col if ts_col else "NULL"} AS ts FROM "{tbl}"'
                        )
                        rows = conn.execute(q).fetchall()
                        for row in rows:
                            ts = _parse_ts(row["ts"])
                            if ts is None or ts < cutoff_ts:
                                continue
                            inp = _safe_int(row["input_tokens"])
                            out = _safe_int(row["output_tokens"])
                            cr = _safe_int(row["cr"])
                            cw = _safe_int(row["cw"])
                            reason = _safe_int(row["r"])
                            if inp + out + cr + cw + reason <= 0:
                                continue
                            model = str(row["model"] or "hermes")
                            cost = _safe_float(row["cost_usd"])
                            if cost <= 0:
                                cost = pricing.estimate_cost(model, inp, out, cr, cw, reason)
                            _acc_add(acc, "hermes", _ts_to_day(ts), model, inp, out, cr, cw, reason, cost, 1)
                        used_detail_table = True
                        break
                    except sqlite3.Error:
                        db_failed = True
                        break

            if not used_detail_table and not db_failed and "sessions" in tables:
                try:
                    cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")').fetchall()}
                    if "input_tokens" in cols or "output_tokens" in cols:
                        ts_col = "ended_at" if "ended_at" in cols else ("started_at" if "started_at" in cols else "created_at")
                        model_col = "model" if "model" in cols else "''"
                        cr_col = "cache_read_tokens" if "cache_read_tokens" in cols else "0"
                        cw_col = "cache_write_tokens" if "cache_write_tokens" in cols else "0"
                        r_col = "reasoning_tokens" if "reasoning_tokens" in cols else "0"
                        cost_col = (
                            "actual_cost_usd"
                            if "actual_cost_usd" in cols
                            else ("estimated_cost_usd" if "estimated_cost_usd" in cols else "0")
                        )
                        q = (
                            f"SELECT {model_col} AS model, input_tokens, output_tokens, {cr_col} AS cr, "
                            f"{cw_col} AS cw, {r_col} AS r, {cost_col} AS cost_usd, "
                            f"COALESCE({ts_col}, started_at) AS ts FROM sessions"
                        )
                        for row in conn.execute(q).fetchall():
                            ts = _parse_ts(row["ts"])
                            if ts is None or ts < cutoff_ts:
                                continue
                            inp = _safe_int(row["input_tokens"])
                            out = _safe_int(row["output_tokens"])
                            cr = _safe_int(row["cr"])
                            cw = _safe_int(row["cw"])
                            reason = _safe_int(row["r"])
                            if inp + out + cr + cw + reason <= 0:
                                continue
                            model = str(row["model"] or "hermes")
                            cost = _safe_float(row["cost_usd"])
                            if cost <= 0:
                                cost = pricing.estimate_cost(model, inp, out, cr, cw, reason)
                            _acc_add(acc, "hermes", _ts_to_day(ts), model, inp, out, cr, cw, reason, cost, 1)
                except sqlite3.Error:
                    db_failed = True

            if db_failed:
                db_statuses.append("unavailable")
            else:
                db_statuses.append("complete")
                _merge_rows_into_result(result, _compact_rows(acc), cutoff_day)

    if dir_err > 0 or missing_prev_insts:
        db_statuses.append("unavailable")
    return combine_statuses(db_statuses)


# ---------------------------------------------------------------------------
# 6. OpenCode SQLite (WAL-safe snapshot)
# ---------------------------------------------------------------------------


def collect_opencode_usage(
    db_path: Path,
    cutoff_ts: float,
    cutoff_day: str,
    pricing: PricingTable,
    result: Dict[str, Any],
    prev_sources: Optional[Dict[str, Any]] = None,
) -> str:
    with sqlite_snapshot_with_status(db_path) as (conn, snap_status):
        if snap_status == "missing":
            return resolve_missing_source_status("usage.opencode", prev_sources or {})
        if snap_status != "ok" or conn is None:
            return "unavailable"
        acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        try:
            rows = conn.execute(
                "SELECT time_created, data FROM message WHERE time_created >= ?",
                (int(cutoff_ts * 1000),),
            ).fetchall()
        except sqlite3.Error:
            return "unavailable"
        for row in rows:
            ts = _parse_ts(row["time_created"])
            if ts is None or ts < cutoff_ts:
                continue
            try:
                data = json.loads(row["data"]) if row["data"] else {}
            except Exception:
                continue
            if not isinstance(data, dict) or data.get("role") != "assistant":
                continue
            tok = data.get("tokens")
            if not isinstance(tok, dict):
                continue
            inp = _safe_int(tok.get("input"))
            out = _safe_int(tok.get("output"))
            reason = _safe_int(tok.get("reasoning"))
            cache_obj = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            cr = _safe_int(cache_obj.get("read"))
            cw = _safe_int(cache_obj.get("write"))
            if inp + out + cr + cw + reason <= 0:
                continue
            model = str(data.get("modelID") or data.get("model") or "opencode")
            cost = _safe_float(data.get("cost"))
            if cost <= 0:
                cost = pricing.estimate_cost(model, inp, out, cr, cw, reason)
            _acc_add(acc, "opencode", _ts_to_day(ts), model, inp, out, cr, cw, reason, cost, 1)
        _merge_rows_into_result(result, _compact_rows(acc), cutoff_day)
        return "complete"


# ---------------------------------------------------------------------------
# 7. Gemini CLI
# ---------------------------------------------------------------------------


def _parse_gemini_file_checked(
    path: Path, pricing: PricingTable
) -> Tuple[List[Dict[str, Any]], bool, bool, Optional[os.stat_result]]:
    for attempt in range(2):
        try:
            st_b = path.stat()
            before = (st_b.st_dev, st_b.st_ino, st_b.st_size, st_b.st_mtime_ns, st_b.st_ctime_ns)
            raw_text = path.read_text(encoding="utf-8", errors="replace")
            st_a = path.stat()
            after = (st_a.st_dev, st_a.st_ino, st_a.st_size, st_a.st_mtime_ns, st_a.st_ctime_ns)
        except OSError:
            return [], True, False, None
        changed = before != after
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            if attempt == 0:
                time.sleep(0.02)
                continue
            return [], True, changed, None
        if changed:
            if attempt == 0:
                time.sleep(0.02)
                continue
            return [], True, True, None

        acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        messages = data.get("messages") if isinstance(data, dict) else None
        if not isinstance(messages, list):
            return [], False, False, st_a
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            tok = msg.get("tokens")
            if not isinstance(tok, dict):
                continue
            ts = _parse_ts(msg.get("timestamp"))
            if ts is None:
                continue
            inp_total = _safe_int(tok.get("input"))
            out = _safe_int(tok.get("output"))
            cr = _safe_int(tok.get("cached"))
            thoughts = _safe_int(tok.get("thoughts"))
            if inp_total + out + cr + thoughts <= 0:
                continue
            non_cached_in = max(0, inp_total - cr)
            model = str(msg.get("model") or "gemini")
            cost = pricing.estimate_cost(
                model,
                input_tokens=non_cached_in,
                output_tokens=out,
                cache_read_tokens=cr,
                cache_write_tokens=0,
                reasoning_tokens=thoughts,
            )
            _acc_add(acc, "gemini", _ts_to_day(ts), model, non_cached_in, out, cr, 0, thoughts, cost, 1)
        return _compact_rows(acc), False, False, st_a
    return [], True, True, None


def _parse_gemini_file(path: Path, pricing: PricingTable) -> List[Dict[str, Any]]:
    rows, _, _, _ = _parse_gemini_file_checked(path, pricing)
    return rows


def collect_gemini_usage(
    gemini_root: Path,
    cutoff_ts: float,
    cutoff_day: str,
    cache: UsageCache,
    pricing: PricingTable,
    result: Dict[str, Any],
    prev_sources: Optional[Dict[str, Any]] = None,
) -> str:
    tmp_root = gemini_root / "tmp"
    can_scan, early = check_root_dir(tmp_root, "usage.gemini", prev_sources or {})
    if not can_scan:
        return early or "not_configured"

    files, dir_err = _walk_matching_files(tmp_root, ".json", prefix="session-")
    files_ok = 0
    files_err = 0
    for path in files:
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                files_ok += 1
                continue
        except OSError:
            files_err += 1
            continue
        rows = cache.get(path)
        if rows is None:
            rows, had_err, changed, st_res = _parse_gemini_file_checked(path, pricing)
            if had_err or changed:
                files_err += 1
                continue
            cache.put(path, rows, stat_result=st_res)
        files_ok += 1
        _merge_rows_into_result(result, rows, cutoff_day)
    return _summarize_file_scan_health(files_ok, files_err, dir_err)


# ---------------------------------------------------------------------------
# 8. ZCode SQLite (WAL-safe snapshot)
# ---------------------------------------------------------------------------


def collect_zcode_usage(
    db_path: Path,
    cutoff_ts: float,
    cutoff_day: str,
    pricing: PricingTable,
    result: Dict[str, Any],
    prev_sources: Optional[Dict[str, Any]] = None,
) -> str:
    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    with sqlite_snapshot_with_status(db_path) as (conn, snap_status):
        if snap_status == "missing":
            return resolve_missing_source_status("usage.zcode", prev_sources or {})
        if snap_status != "ok" or conn is None:
            return "unavailable"
        try:
            rows = conn.execute(
                """SELECT model_id, input_tokens, output_tokens, reasoning_tokens,
                          cache_creation_input_tokens, cache_read_input_tokens,
                          COALESCE(NULLIF(completed_at, 0), started_at) AS timestamp_ms
                   FROM model_usage WHERE COALESCE(NULLIF(completed_at, 0), started_at) >= ?""",
                (int(cutoff_ts * 1000),),
            )
            for model, inp, out, reason, cw, cr, stamp in rows:
                inp, out, reason, cw, cr = map(_safe_int, (inp, out, reason, cw, cr))
                reason = min(reason, out)
                inp = max(0, inp - cr - cw)
                out -= reason
                if not (inp or out or reason or cw or cr):
                    continue
                model = str(model or "unknown")
                _acc_add(
                    acc,
                    tool="zcode",
                    day=_ts_to_day(stamp / 1000),
                    model=model,
                    inp=inp,
                    out=out,
                    cr=cr,
                    cw=cw,
                    thoughts=reason,
                    cost_usd=pricing.estimate_cost(model, inp, out, cr, cw, reason),
                    requests=1,
                )
        except sqlite3.Error:
            return "unavailable"
    _merge_rows_into_result(result, _compact_rows(acc), cutoff_day)
    return "complete"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def collect_live_usage(
    home: Optional[Path] = None,
    days: int = 14,
    cache_path: Optional[Path] = None,
    use_cache: bool = True,
    now_ts: Optional[float] = None,
    pricing: Optional[PricingTable] = None,
    projects_out: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    source_statuses_out: Optional[Dict[str, str]] = None,
    billing_out: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
    billing_cutoff_day: Optional[str] = None,
    prev_sources: Optional[Dict[str, Any]] = None,
    observed_instances_out: Optional[Dict[str, set]] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Scan local AI logs for the last `days` days and return {tool: {YYYY-MM-DD: bucket}}."""
    home_dir = home or Path.home()
    now = now_ts if now_ts is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=BIZ_TZ)
    today_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=BIZ_TZ)
    cutoff_dt = today_dt - timedelta(days=days - 1)
    cutoff_ts = cutoff_dt.timestamp()
    cutoff_day = cutoff_dt.strftime("%Y-%m-%d")

    # Extend file-mtime scan floor to month start when billing needs full current month
    scan_cutoff_ts = cutoff_ts
    if billing_cutoff_day:
        try:
            b_dt = datetime.strptime(billing_cutoff_day, "%Y-%m-%d").replace(tzinfo=BIZ_TZ)
            scan_cutoff_ts = min(cutoff_ts, b_dt.timestamp())
        except ValueError:
            pass

    from ledger import resolve_data_dir

    data_dir = resolve_data_dir(home_dir, create=use_cache)
    eff_cache_path = None if not use_cache else (cache_path or (data_dir / "usage_cache.json"))
    pricing_tbl = pricing or PricingTable.load(home=home_dir)
    cache = UsageCache(eff_cache_path, pricing_tbl.signature)

    result: Dict[str, Dict[str, Dict[str, Any]]] = {}
    billing_acc: Optional[Dict[str, Dict[str, Dict[str, Decimal]]]] = {} if billing_out is not None else None
    statuses: Dict[str, str] = {}

    statuses["usage.codex"] = collect_codex_usage(
        home_dir / ".codex" / "sessions",
        scan_cutoff_ts,
        cutoff_day,
        cache,
        pricing_tbl,
        result,
        projects_out=projects_out,
        billing_acc=billing_acc,
        billing_cutoff_day=billing_cutoff_day,
        prev_sources=prev_sources,
    )
    statuses["usage.workbuddy"] = collect_workbuddy_usage(
        home_dir / ".workbuddy" / "projects",
        "workbuddy",
        cutoff_ts,
        cutoff_day,
        cache,
        pricing_tbl,
        result,
        projects_out=projects_out,
        prev_sources=prev_sources,
    )
    statuses["usage.codebuddy"] = collect_workbuddy_usage(
        home_dir / ".codebuddy" / "projects",
        "codebuddy",
        cutoff_ts,
        cutoff_day,
        cache,
        pricing_tbl,
        result,
        projects_out=projects_out,
        prev_sources=prev_sources,
    )
    statuses["usage.claude_code"] = collect_claude_usage(
        home_dir / ".claude" / "projects",
        scan_cutoff_ts,
        cutoff_day,
        cache,
        pricing_tbl,
        result,
        projects_out=projects_out,
        billing_acc=billing_acc,
        billing_cutoff_day=billing_cutoff_day,
        prev_sources=prev_sources,
    )
    statuses["usage.pi"] = collect_pi_usage(
        home_dir / ".pi" / "agent" / "sessions",
        cutoff_ts,
        cutoff_day,
        cache,
        pricing_tbl,
        result,
        projects_out=projects_out,
        prev_sources=prev_sources,
    )
    statuses["usage.hermes"] = collect_hermes_usage(
        home_dir / ".hermes",
        cutoff_ts,
        cutoff_day,
        pricing_tbl,
        result,
        billing_acc=billing_acc,
        billing_cutoff_day=billing_cutoff_day,
        prev_sources=prev_sources,
        observed_instances_out=observed_instances_out,
    )
    statuses["usage.opencode"] = collect_opencode_usage(
        home_dir / ".local" / "share" / "opencode" / "opencode.db",
        cutoff_ts,
        cutoff_day,
        pricing_tbl,
        result,
        prev_sources=prev_sources,
    )
    statuses["usage.gemini"] = collect_gemini_usage(
        home_dir / ".gemini",
        cutoff_ts,
        cutoff_day,
        cache,
        pricing_tbl,
        result,
        prev_sources=prev_sources,
    )
    statuses["usage.zcode"] = collect_zcode_usage(
        home_dir / ".zcode" / "cli" / "db" / "db.sqlite",
        cutoff_ts,
        cutoff_day,
        pricing_tbl,
        result,
        prev_sources=prev_sources,
    )

    for tool_days in result.values():
        for bucket in tool_days.values():
            for name, model in bucket["models"].items():
                missing_price = model["cost_usd"] == 0 and pricing_tbl.resolve(name) is None
                model["unpriced_tokens"] = (
                    sum(model[key] for key in ("in", "out", "cr", "cw", "thoughts")) if missing_price else 0
                )
            bucket["unpriced_tokens"] = sum(model["unpriced_tokens"] for model in bucket["models"].values())

    if source_statuses_out is not None:
        source_statuses_out.update(statuses)

    if billing_out is not None and billing_acc is not None:
        for tool, day_map in billing_acc.items():
            dst_tool = billing_out.setdefault(tool, {})
            for day, vals in day_map.items():
                entry: Dict[str, float] = {}
                if "subscription_candidate_usd" in vals:
                    entry["subscription_candidate_usd"] = round(float(vals["subscription_candidate_usd"]), 6)
                if "api_actual_usd" in vals:
                    entry["api_actual_usd"] = round(float(vals["api_actual_usd"]), 6)
                if entry:
                    dst_tool[day] = entry

    cache.save()
    return result
