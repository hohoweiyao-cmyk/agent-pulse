#!/usr/bin/env python3
"""AgentPulse — Local read-only Token & Cost incremental collector.

Strictly read-only on all AI client logs and SQLite databases (WAL-safe temp copies).
Zero external dependencies (Python 3.9+ stdlib only).

Key guarantees:
- Explicit business timezone: Asia/Shanghai (UTC+8)
- T3 Code <-> Codex bidirectional deduplication:
  Codex sessions with originator == "T3 Code" are attributed to `t3_codex`,
  standalone Codex sessions to `codex`, matching work-hours attribution 1:1.
- Incremental file-level cache (mtime_ns + size) at ~/.agent-pulse/usage_cache.json
  so 15s polling completes in < 200ms.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from pricing import PricingTable

BIZ_TZ = timezone(timedelta(hours=8))
USAGE_CACHE_VERSION = 2


def _parse_ts(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        if v <= 0:
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
        return v if v > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


@contextlib.contextmanager
def _open_sqlite_readonly_snapshot(db_path: Path) -> Iterator[Optional[sqlite3.Connection]]:
    if not db_path.is_file():
        yield None
        return
    tmp_dir = tempfile.mkdtemp(prefix="agentpulse_u_")
    conn: Optional[sqlite3.Connection] = None
    try:
        dst = Path(tmp_dir) / db_path.name
        shutil.copy2(db_path, dst)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            if sidecar.is_file():
                try:
                    shutil.copy2(sidecar, Path(str(dst) + suffix))
                except OSError:
                    pass
        conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        yield conn
    except Exception:
        yield None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


class UsageCache:
    """Caches parsed daily token deltas per source file using mtime_ns + size."""

    def __init__(self, path: Optional[Path]):
        self.path = path
        self.dirty = False
        self.files: Dict[str, Dict[str, Any]] = {}
        if path and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if raw.get("version") == USAGE_CACHE_VERSION and isinstance(raw.get("files"), dict):
                    self.files = raw["files"]
            except Exception:
                self.files = {}

    def get(self, file_path: Path) -> Optional[List[Dict[str, Any]]]:
        try:
            st = file_path.stat()
        except OSError:
            return None
        entry = self.files.get(str(file_path))
        if entry and entry.get("mtime_ns") == st.st_mtime_ns and entry.get("size") == st.st_size:
            return entry.get("rows")
        return None

    def put(self, file_path: Path, rows: List[Dict[str, Any]]) -> None:
        try:
            st = file_path.stat()
        except OSError:
            return
        self.files[str(file_path)] = {
            "mtime_ns": st.st_mtime_ns,
            "size": st.st_size,
            "rows": rows,
        }
        self.dirty = True

    def save(self) -> None:
        if not self.path or not self.dirty:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            payload = {"version": USAGE_CACHE_VERSION, "files": self.files}
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self.path)
            self.dirty = False
        except OSError:
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
) -> None:
    bucket["in"] += inp
    bucket["out"] += out
    bucket["cr"] += cr
    bucket["cw"] += cw
    bucket["thoughts"] += thoughts
    bucket["cost_usd"] = round(bucket["cost_usd"] + cost_usd, 6)
    bucket["requests"] += requests

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
) -> None:
    for r in rows:
        day = r.get("day")
        if not day or day < cutoff_day:
            continue
        tool = r["tool"]
        day_map = result.setdefault(tool, {})
        bucket = day_map.setdefault(day, _empty_bucket())
        _add_to_bucket(
            bucket,
            model=r.get("model", "unknown"),
            inp=_safe_int(r.get("in")),
            out=_safe_int(r.get("out")),
            cr=_safe_int(r.get("cr")),
            cw=_safe_int(r.get("cw")),
            thoughts=_safe_int(r.get("thoughts")),
            cost_usd=_safe_float(r.get("cost_usd")),
            requests=_safe_int(r.get("requests") or 1),
        )


def _compact_rows(acc: Dict[Tuple[str, str, str], Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for (tool, day, model), v in acc.items():
        if v["in"] + v["out"] + v["cr"] + v["cw"] + v["thoughts"] <= 0 and v["cost_usd"] <= 0:
            continue
        rows.append(
            {
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
            }
        )
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
) -> None:
    key = (tool, day, model or "unknown")
    entry = acc.setdefault(
        key,
        {"in": 0, "out": 0, "cr": 0, "cw": 0, "thoughts": 0, "cost_usd": 0.0, "requests": 0},
    )
    entry["in"] += inp
    entry["out"] += out
    entry["cr"] += cr
    entry["cw"] += cw
    entry["thoughts"] += thoughts
    entry["cost_usd"] += cost_usd
    entry["requests"] += requests


# ---------------------------------------------------------------------------
# 1. Codex (with T3 Code <-> Codex bidirectional deduplication)
# ---------------------------------------------------------------------------


def _parse_codex_file(path: Path, pricing: PricingTable) -> List[Dict[str, Any]]:
    originator = ""
    session_id = None
    response_ids = set()
    current_model = "gpt-5"
    prev_total: Optional[Tuple[int, int, int, int, int]] = None
    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}

    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if (
                    '"token_count"' not in line
                    and '"token_usage_record"' not in line
                    and '"session_meta"' not in line
                    and '"turn_context"' not in line
                ):
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                typ = obj.get("type")
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    continue

                if typ == "session_meta":
                    session_id = payload.get("id")
                    orig = payload.get("originator")
                    if isinstance(orig, str):
                        originator = orig
                    continue

                if typ == "turn_context":
                    m = payload.get("model")
                    if isinstance(m, str) and m.strip():
                        current_model = m.strip()
                    continue

                is_response = typ == "token_usage_record"
                if is_response:
                    last = payload.get("usage")
                    total = payload.get("thread_token_usage")
                    owner = payload.get("thread_id")
                    response_id = payload.get("response_id")
                    if not isinstance(last, dict) or (owner and session_id and owner != session_id):
                        continue
                    if isinstance(response_id, str) and response_id:
                        if response_id in response_ids:
                            continue
                        response_ids.add(response_id)
                elif typ == "event_msg" and payload.get("type") == "token_count":
                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue
                    total = info.get("total_token_usage")
                    last = info.get("last_token_usage")
                else:
                    continue

                ts = _parse_ts(obj.get("timestamp"))
                if ts is None:
                    continue

                d_in = d_cr = d_cw = d_out = d_reason = 0
                if isinstance(total, dict):
                    cur_in = _safe_int(total.get("input_tokens"))
                    cur_cr = _safe_int(total.get("cached_input_tokens"))
                    cur_cw = _safe_int(total.get("cache_write_input_tokens"))
                    cur_out = _safe_int(total.get("output_tokens"))
                    cur_r = _safe_int(total.get("reasoning_output_tokens"))
                    cur_tuple = (cur_in, cur_cr, cur_cw, cur_out, cur_r)
                    if prev_total is not None:
                        if cur_tuple == prev_total:
                            continue  # Duplicate snapshot echo
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
                        d_in, d_cr, d_cw, d_out, d_reason = (_safe_int(last.get(key)) for key in (
                            "input_tokens", "cached_input_tokens", "cache_write_input_tokens",
                            "output_tokens", "reasoning_output_tokens",
                        ))
                    prev_total = cur_tuple
                elif isinstance(last, dict):
                    d_in = _safe_int(last.get("input_tokens"))
                    d_cr = _safe_int(last.get("cached_input_tokens"))
                    d_cw = _safe_int(last.get("cache_write_input_tokens"))
                    d_out = _safe_int(last.get("output_tokens"))
                    d_reason = _safe_int(last.get("reasoning_output_tokens"))

                if d_in + d_out + d_cr + d_cw + d_reason <= 0:
                    continue

                # OpenAI input_tokens includes cached_input_tokens -> separate non-cached input
                non_cached_in = max(0, d_in - d_cr - d_cw)
                # OpenAI output_tokens already includes reasoning_output_tokens
                non_reason_out = max(0, d_out - d_reason)

                cost = pricing.estimate_cost(
                    current_model,
                    input_tokens=non_cached_in,
                    output_tokens=non_reason_out,
                    cache_read_tokens=d_cr,
                    cache_write_tokens=d_cw,
                    reasoning_tokens=d_reason,
                )
                tool_key = "t3_codex" if originator == "T3 Code" else "codex"
                _acc_add(
                    acc,
                    tool=tool_key,
                    day=_ts_to_day(ts),
                    model=current_model,
                    inp=non_cached_in,
                    out=non_reason_out,
                    cr=d_cr,
                    cw=d_cw,
                    thoughts=d_reason,
                    cost_usd=cost,
                    requests=1,
                )
    except OSError:
        return []
    return _compact_rows(acc)


def collect_codex_usage(
    sessions_root: Path, cutoff_ts: float, cutoff_day: str, cache: UsageCache, pricing: PricingTable, result: Dict[str, Any]
) -> None:
    if not sessions_root.is_dir():
        return
    for path in sessions_root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                continue
        except OSError:
            continue
        rows = cache.get(path)
        if rows is None:
            rows = _parse_codex_file(path, pricing)
            cache.put(path, rows)
        _merge_rows_into_result(result, rows, cutoff_day)


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


def _parse_workbuddy_file(path: Path, tool_key: str, pricing: PricingTable) -> List[Dict[str, Any]]:
    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    seen_dedup: set = set()
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line_no, line in enumerate(fh, 1):
                if '"usage"' not in line and '"rawUsage"' not in line:
                    continue
                try:
                    item = json.loads(line)
                except Exception:
                    continue
                if not isinstance(item, dict):
                    continue
                ts = _parse_ts(item.get("timestamp") or item.get("createdAt") or item.get("ts"))
                if ts is None:
                    continue

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

                # Deduplicate repeated frames in same file
                dedup_id = message.get("id") or item.get("id") or f"{line_no}"
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
    except OSError:
        return []
    return _compact_rows(acc)


def collect_workbuddy_usage(
    root: Path, tool_key: str, cutoff_ts: float, cutoff_day: str, cache: UsageCache, pricing: PricingTable, result: Dict[str, Any]
) -> None:
    if not root.is_dir():
        return
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                continue
        except OSError:
            continue
        rows = cache.get(path)
        if rows is None:
            rows = _parse_workbuddy_file(path, tool_key, pricing)
            cache.put(path, rows)
        _merge_rows_into_result(result, rows, cutoff_day)


# ---------------------------------------------------------------------------
# 3. Claude Code
# ---------------------------------------------------------------------------


def _parse_claude_file(path: Path, pricing: PricingTable) -> List[Dict[str, Any]]:
    # Deduplicate streaming chunks by message.id, retaining the final (max output) snapshot
    by_msg: Dict[str, Tuple[float, str, int, int, int, int, int]] = {}
    fallback_seq = 0
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
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
    except OSError:
        return []

    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for ts, model, inp, out, cr, cw, thoughts in by_msg.values():
        non_reason_out = max(0, out - thoughts) if thoughts <= out else out
        cost = pricing.estimate_cost(
            model,
            input_tokens=inp,
            output_tokens=non_reason_out,
            cache_read_tokens=cr,
            cache_write_tokens=cw,
            reasoning_tokens=thoughts,
        )
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
        )
    return _compact_rows(acc)


def collect_claude_usage(
    projects_root: Path, cutoff_ts: float, cutoff_day: str, cache: UsageCache, pricing: PricingTable, result: Dict[str, Any]
) -> None:
    if not projects_root.is_dir():
        return
    for path in projects_root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                continue
        except OSError:
            continue
        rows = cache.get(path)
        if rows is None:
            rows = _parse_claude_file(path, pricing)
            cache.put(path, rows)
        _merge_rows_into_result(result, rows, cutoff_day)


# ---------------------------------------------------------------------------
# 4. Pi Agent
# ---------------------------------------------------------------------------


def _parse_pi_file(path: Path, pricing: PricingTable) -> List[Dict[str, Any]]:
    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
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
    except OSError:
        return []
    return _compact_rows(acc)


def collect_pi_usage(
    sessions_root: Path, cutoff_ts: float, cutoff_day: str, cache: UsageCache, pricing: PricingTable, result: Dict[str, Any]
) -> None:
    if not sessions_root.is_dir():
        return
    for path in sessions_root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                continue
        except OSError:
            continue
        rows = cache.get(path)
        if rows is None:
            rows = _parse_pi_file(path, pricing)
            cache.put(path, rows)
        _merge_rows_into_result(result, rows, cutoff_day)


# ---------------------------------------------------------------------------
# 5. Hermes SQLite (Main + Profiles, WAL-safe snapshot)
# ---------------------------------------------------------------------------


def collect_hermes_usage(
    hermes_root: Path, cutoff_ts: float, cutoff_day: str, pricing: PricingTable, result: Dict[str, Any]
) -> None:
    if not hermes_root.is_dir():
        return
    db_paths: List[Path] = []
    if (hermes_root / "state.db").is_file():
        db_paths.append(hermes_root / "state.db")
    profiles_dir = hermes_root / "profiles"
    if profiles_dir.is_dir():
        for prof in sorted(profiles_dir.iterdir()):
            cand = prof / "state.db"
            if cand.is_file():
                db_paths.append(cand)

    for db_path in db_paths:
        try:
            if db_path.stat().st_mtime < cutoff_ts - 86400:
                continue
        except OSError:
            continue
        with _open_sqlite_readonly_snapshot(db_path) as conn:
            if conn is None:
                continue
            try:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            except sqlite3.Error:
                continue

            acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
            used_detail_table = False

            # Prefer fine-grained usage table if present
            for tbl in ("model_usage", "token_usage", "session_usage", "usage"):
                if tbl not in tables:
                    continue
                try:
                    cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tbl}")').fetchall()}
                    if "input_tokens" not in cols and "output_tokens" not in cols:
                        continue
                    ts_col = "last_seen" if "last_seen" in cols else ("first_seen" if "first_seen" in cols else ("created_at" if "created_at" in cols else None))
                    model_col = "model" if "model" in cols else "''"
                    cr_col = "cache_read_tokens" if "cache_read_tokens" in cols else "0"
                    cw_col = "cache_write_tokens" if "cache_write_tokens" in cols else "0"
                    r_col = "reasoning_tokens" if "reasoning_tokens" in cols else "0"
                    cost_col = "actual_cost_usd" if "actual_cost_usd" in cols else ("estimated_cost_usd" if "estimated_cost_usd" in cols else "0")
                    q = f'SELECT {model_col} AS model, input_tokens, output_tokens, {cr_col} AS cr, {cw_col} AS cw, {r_col} AS r, {cost_col} AS cost_usd, {ts_col if ts_col else "NULL"} AS ts FROM "{tbl}"'
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
                    continue

            if not used_detail_table and "sessions" in tables:
                try:
                    cols = {r[1] for r in conn.execute('PRAGMA table_info("sessions")').fetchall()}
                    if "input_tokens" in cols or "output_tokens" in cols:
                        ts_col = "ended_at" if "ended_at" in cols else ("started_at" if "started_at" in cols else "created_at")
                        model_col = "model" if "model" in cols else "''"
                        cr_col = "cache_read_tokens" if "cache_read_tokens" in cols else "0"
                        cw_col = "cache_write_tokens" if "cache_write_tokens" in cols else "0"
                        r_col = "reasoning_tokens" if "reasoning_tokens" in cols else "0"
                        cost_col = "actual_cost_usd" if "actual_cost_usd" in cols else ("estimated_cost_usd" if "estimated_cost_usd" in cols else "0")
                        q = f'SELECT {model_col} AS model, input_tokens, output_tokens, {cr_col} AS cr, {cw_col} AS cw, {r_col} AS r, {cost_col} AS cost_usd, COALESCE({ts_col}, started_at) AS ts FROM sessions'
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
                    pass

            _merge_rows_into_result(result, _compact_rows(acc), cutoff_day)


# ---------------------------------------------------------------------------
# 6. OpenCode SQLite (WAL-safe snapshot)
# ---------------------------------------------------------------------------


def collect_opencode_usage(
    db_path: Path, cutoff_ts: float, cutoff_day: str, pricing: PricingTable, result: Dict[str, Any]
) -> None:
    if not db_path.is_file():
        return
    try:
        if db_path.stat().st_mtime < cutoff_ts - 86400:
            return
    except OSError:
        return
    with _open_sqlite_readonly_snapshot(db_path) as conn:
        if conn is None:
            return
        acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
        try:
            rows = conn.execute("SELECT time_created, data FROM message WHERE time_created >= ?", (int(cutoff_ts * 1000),)).fetchall()
        except sqlite3.Error:
            return
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


# ---------------------------------------------------------------------------
# 7. Gemini CLI
# ---------------------------------------------------------------------------


def _parse_gemini_file(path: Path, pricing: PricingTable) -> List[Dict[str, Any]]:
    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        return []
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
    return _compact_rows(acc)


def collect_gemini_usage(
    gemini_root: Path, cutoff_ts: float, cutoff_day: str, cache: UsageCache, pricing: PricingTable, result: Dict[str, Any]
) -> None:
    tmp_root = gemini_root / "tmp"
    if not tmp_root.is_dir():
        return
    for path in tmp_root.rglob("session-*.json"):
        try:
            if path.stat().st_mtime < cutoff_ts - 86400:
                continue
        except OSError:
            continue
        rows = cache.get(path)
        if rows is None:
            rows = _parse_gemini_file(path, pricing)
            cache.put(path, rows)
        _merge_rows_into_result(result, rows, cutoff_day)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def collect_zcode_usage(
    db_path: Path, cutoff_ts: float, cutoff_day: str, pricing: PricingTable, result: Dict[str, Any]
) -> None:
    acc: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    with _open_sqlite_readonly_snapshot(db_path) as conn:
        if conn is None:
            return
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
                    acc, tool="zcode", day=_ts_to_day(stamp / 1000), model=model,
                    inp=inp, out=out, cr=cr, cw=cw, thoughts=reason,
                    cost_usd=pricing.estimate_cost(model, inp, out, cr, cw, reason), requests=1,
                )
        except sqlite3.Error:
            return
    _merge_rows_into_result(result, _compact_rows(acc), cutoff_day)


def collect_live_usage(
    home: Optional[Path] = None,
    days: int = 14,
    cache_path: Optional[Path] = None,
    use_cache: bool = True,
    now_ts: Optional[float] = None,
    pricing: Optional[PricingTable] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Scan local AI logs for the last `days` days and return {tool: {YYYY-MM-DD: bucket}}."""
    home_dir = home or Path.home()
    now = now_ts if now_ts is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=BIZ_TZ)
    cutoff_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=BIZ_TZ) - timedelta(days=days - 1)
    cutoff_ts = cutoff_dt.timestamp()
    cutoff_day = cutoff_dt.strftime("%Y-%m-%d")

    from ledger import resolve_data_dir

    data_dir = resolve_data_dir(home_dir, create=use_cache)
    eff_cache_path = None if not use_cache else (cache_path or (data_dir / "usage_cache.json"))
    cache = UsageCache(eff_cache_path)
    pricing_tbl = pricing or PricingTable.load(home=home_dir)

    result: Dict[str, Dict[str, Dict[str, Any]]] = {}

    collect_codex_usage(home_dir / ".codex" / "sessions", cutoff_ts, cutoff_day, cache, pricing_tbl, result)
    collect_workbuddy_usage(home_dir / ".workbuddy" / "projects", "workbuddy", cutoff_ts, cutoff_day, cache, pricing_tbl, result)
    collect_workbuddy_usage(home_dir / ".codebuddy" / "projects", "codebuddy", cutoff_ts, cutoff_day, cache, pricing_tbl, result)
    collect_claude_usage(home_dir / ".claude" / "projects", cutoff_ts, cutoff_day, cache, pricing_tbl, result)
    collect_pi_usage(home_dir / ".pi" / "agent" / "sessions", cutoff_ts, cutoff_day, cache, pricing_tbl, result)
    collect_hermes_usage(home_dir / ".hermes", cutoff_ts, cutoff_day, pricing_tbl, result)
    collect_opencode_usage(home_dir / ".local" / "share" / "opencode" / "opencode.db", cutoff_ts, cutoff_day, pricing_tbl, result)
    collect_gemini_usage(home_dir / ".gemini", cutoff_ts, cutoff_day, cache, pricing_tbl, result)

    collect_zcode_usage(home_dir / ".zcode" / "cli" / "db" / "db.sqlite", cutoff_ts, cutoff_day, pricing_tbl, result)

    cache.save()
    return result
