#!/usr/bin/env python3
"""AgentPulse — Persistent local usage ledger & multi-period aggregator.

Maintains ~/.agent-pulse/ledger.json with separate `imported_tools` (from Tokei
historical ledger) and `live_tools` (incrementally updated by AgentPulse).
Automatically seeds from legacy ~/.agent-clock/ (read-only) if present.

Reconciliation rule:
- Days within the active live scan window (or present in live_tools) are authoritative
  from `live_tools`, EXCEPT when a tool has combined T3+Codex historical records in
  `imported_tools` on older days outside the live scan window.
- Specifically: for any day >= `live_cutoff_day`, `live_tools` wins; for older days,
  `live_tools` wins if recorded, otherwise `imported_tools` is preserved intact.
  This guarantees zero double counting and zero historical data loss.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BIZ_TZ = timezone(timedelta(hours=8))
LEDGER_VERSION = 1

DATA_DIR_NAME = ".agent-pulse"
LEGACY_DATA_DIR_NAME = ".agent-clock"
_LEGACY_MIGRATE_FILES = (
    "ledger.json",
    "pricing_overrides.json",
    "quota_cache.json",
    "claude_statusline_quota.json",
    "migrated_prefs.json",
)


def resolve_data_dir(home: Optional[Path] = None, create: bool = True) -> Path:
    """Return ~/.agent-pulse, safely seeding from legacy ~/.agent-clock (read-only) on first run."""
    home_dir = home or Path.home()
    data_dir = home_dir / DATA_DIR_NAME
    legacy_dir = home_dir / LEGACY_DATA_DIR_NAME

    if not data_dir.exists() and legacy_dir.is_dir():
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            for fname in _LEGACY_MIGRATE_FILES:
                src = legacy_dir / fname
                dst = data_dir / fname
                if src.is_file() and not dst.exists():
                    shutil.copy2(src, dst)
            src_hist = legacy_dir / "history"
            dst_hist = data_dir / "history"
            if src_hist.is_dir() and not dst_hist.exists():
                shutil.copytree(src_hist, dst_hist, dirs_exist_ok=True)
        except OSError:
            pass
    elif create:
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return data_dir

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

USAGE_TOOL_META: Dict[str, Dict[str, str]] = {
    "t3_antigravity":   {"name": "T3 · Antigravity", "short": "T3 Agy",    "color": "#7C6BF5"},
    "t3_codex":         {"name": "T3 · Codex",       "short": "T3 Codex",  "color": "#10B981"},
    "t3_claude":        {"name": "T3 · Claude",      "short": "T3 Claude", "color": "#F59E0B"},
    "t3_other":         {"name": "T3 · Agent",       "short": "T3",        "color": "#8B5CF6"},
    "codex":            {"name": "Codex",            "short": "Codex",     "color": "#34D399"},
    "workbuddy":        {"name": "WorkBuddy",        "short": "WorkBuddy", "color": "#38BDF8"},
    "hermes":           {"name": "Hermes",           "short": "Hermes",    "color": "#F472B6"},
    "antigravity":      {"name": "Antigravity",      "short": "Agy App",   "color": "#A78BFA"},
    "claude_code":      {"name": "Claude Code",      "short": "Claude",    "color": "#FB923C"},
    "claude":           {"name": "Claude Code",      "short": "Claude",    "color": "#FB923C"},
    "pi":               {"name": "Pi",               "short": "Pi",        "color": "#22D3EE"},
    "opencode":         {"name": "OpenCode",         "short": "OpenCode",  "color": "#94A3B8"},
    "gemini":           {"name": "Gemini CLI",       "short": "Gemini",    "color": "#60A5FA"},
    "zcode":            {"name": "ZCode",            "short": "ZCode",     "color": "#C084FC"},
    "deepseek_harness": {"name": "DeepSeek",         "short": "DeepSeek",  "color": "#818CF8"},
    "codebuddy":        {"name": "CodeBuddy",        "short": "CodeBuddy", "color": "#2DD4BF"},
    "grok_bot":         {"name": "Grok Bot",         "short": "Grok",      "color": "#E2E8F0"},
}

# Normalize Tokei legacy tool keys to AgentPulse canonical keys
TOKEI_TOOL_MAP = {
    "claude": "claude_code",
}


def canonical_tool_key(raw_key: str) -> str:
    return TOKEI_TOOL_MAP.get(raw_key, raw_key)


def _safe_int(v: Any) -> int:
    try:
        n = int(v)
        return n if n > 0 else 0
    except (TypeError, ValueError):
        return 0


def _safe_float(v: Any) -> float:
    try:
        n = float(v)
        return n if n > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _bucket_total_tokens(b: Dict[str, Any]) -> int:
    return (
        _safe_int(b.get("in"))
        + _safe_int(b.get("out"))
        + _safe_int(b.get("cr"))
        + _safe_int(b.get("cw"))
        + _safe_int(b.get("thoughts"))
    )


def load_ledger(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {
            "version": LEDGER_VERSION,
            "updated_at": None,
            "tokei_import_meta": None,
            "imported_tools": {},
            "live_tools": {},
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("version", LEDGER_VERSION)
            raw.setdefault("imported_tools", {})
            raw.setdefault("live_tools", {})
            return raw
    except Exception:
        pass
    return {
        "version": LEDGER_VERSION,
        "updated_at": None,
        "tokei_import_meta": None,
        "imported_tools": {},
        "live_tools": {},
    }


def save_ledger(path: Path, ledger: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(ledger, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def update_ledger_with_live(
    ledger: Dict[str, Any],
    live_usage: Dict[str, Dict[str, Dict[str, Any]]],
    live_cutoff_day: str,
    now_ts: float,
) -> bool:
    """Merge live_usage into ledger['live_tools'] for days >= live_cutoff_day. Returns True if changed."""
    live_tools = ledger.setdefault("live_tools", {})
    changed = False

    # Clear stale live entries in the live scan window for tools that are actively scanned
    # so deleted/rewritten logs don't leave ghost numbers, while preserving older live days.
    for tool_key, day_map in live_usage.items():
        t_dst = live_tools.setdefault(tool_key, {})
        for day_key, bucket in day_map.items():
            if day_key < live_cutoff_day:
                continue
            if t_dst.get(day_key) != bucket:
                t_dst[day_key] = bucket
                changed = True

    if changed or not ledger.get("updated_at"):
        ledger["updated_at"] = datetime.fromtimestamp(now_ts, tz=BIZ_TZ).isoformat()
    return changed


def _reconcile_merged_tools(
    ledger: Dict[str, Any],
    live_usage: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Produce unified {tool: {day: bucket}} combining imported history + live records without double counting."""
    imported = ledger.get("imported_tools") or {}
    persisted_live = ledger.get("live_tools") or {}

    # Track which days have live codex/t3_codex records so imported `codex` doesn't double-count them
    live_codex_days = set((live_usage.get("codex") or {}).keys()) | set((live_usage.get("t3_codex") or {}).keys())
    live_codex_days |= set((persisted_live.get("codex") or {}).keys()) | set((persisted_live.get("t3_codex") or {}).keys())

    merged: Dict[str, Dict[str, Dict[str, Any]]] = {}

    # 1. Start with imported historical days
    for raw_tool, day_map in imported.items():
        if not isinstance(day_map, dict):
            continue
        tool = canonical_tool_key(raw_tool)
        dst = merged.setdefault(tool, {})
        for day_key, bucket in day_map.items():
            if not isinstance(bucket, dict):
                continue
            # If live scan covers this day for codex/t3_codex, skip imported codex on that day
            if tool == "codex" and day_key in live_codex_days:
                continue
            dst[day_key] = bucket

    # 2. Overlay persisted live_tools (days outside current live scan window)
    for tool, day_map in persisted_live.items():
        if not isinstance(day_map, dict):
            continue
        dst = merged.setdefault(tool, {})
        for day_key, bucket in day_map.items():
            if isinstance(bucket, dict):
                dst[day_key] = bucket

    # 3. Overlay fresh live_usage (authoritative for current scan window)
    for tool, day_map in live_usage.items():
        if not isinstance(day_map, dict):
            continue
        dst = merged.setdefault(tool, {})
        for day_key, bucket in day_map.items():
            if isinstance(bucket, dict):
                dst[day_key] = bucket

    return merged


def _build_period_summary(
    merged: Dict[str, Dict[str, Dict[str, Any]]],
    start_day: str,
    end_day: str,
    work_by_day: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    total_in = total_out = total_cr = total_cw = total_thoughts = 0
    total_cost = 0.0
    active_days_set: set = set()
    tool_acc: Dict[str, Dict[str, Any]] = {}

    for tool, day_map in merged.items():
        for day_key, b in day_map.items():
            if day_key < start_day or day_key > end_day:
                continue
            b_in = _safe_int(b.get("in"))
            b_out = _safe_int(b.get("out"))
            b_cr = _safe_int(b.get("cr"))
            b_cw = _safe_int(b.get("cw"))
            b_th = _safe_int(b.get("thoughts"))
            b_cost = _safe_float(b.get("cost_usd", b.get("cost")))
            b_tok = b_in + b_out + b_cr + b_cw + b_th
            if b_tok <= 0 and b_cost <= 0:
                continue

            active_days_set.add(day_key)
            total_in += b_in
            total_out += b_out
            total_cr += b_cr
            total_cw += b_cw
            total_thoughts += b_th
            total_cost += b_cost

            t_entry = tool_acc.setdefault(
                tool,
                {
                    "in": 0,
                    "out": 0,
                    "cr": 0,
                    "cw": 0,
                    "thoughts": 0,
                    "cost_usd": 0.0,
                    "active_days": set(),
                    "models": {},
                },
            )
            t_entry["in"] += b_in
            t_entry["out"] += b_out
            t_entry["cr"] += b_cr
            t_entry["cw"] += b_cw
            t_entry["thoughts"] += b_th
            t_entry["cost_usd"] += b_cost
            t_entry["active_days"].add(day_key)

            for m_name, m_val in (b.get("models") or {}).items():
                if not isinstance(m_val, dict):
                    continue
                m_dst = t_entry["models"].setdefault(
                    m_name,
                    {"in": 0, "out": 0, "cr": 0, "cw": 0, "thoughts": 0, "cost_usd": 0.0},
                )
                m_dst["in"] += _safe_int(m_val.get("in"))
                m_dst["out"] += _safe_int(m_val.get("out"))
                m_dst["cr"] += _safe_int(m_val.get("cr", m_val.get("cached")))
                m_dst["cw"] += _safe_int(m_val.get("cw"))
                m_dst["thoughts"] += _safe_int(m_val.get("thoughts", m_val.get("reason")))
                m_dst["cost_usd"] += _safe_float(m_val.get("cost_usd", m_val.get("cost")))

    # Merge work-hours per tool in the same date range (for unified table!)
    work_seconds_total = 0.0
    work_wall_total = 0.0
    work_runs_total = 0
    tool_work_sec: Dict[str, float] = {}
    tool_work_runs: Dict[str, int] = {}

    if work_by_day:
        for day_key, wday in work_by_day.items():
            if day_key < start_day or day_key > end_day:
                continue
            work_seconds_total += float(wday.get("agent_seconds", 0.0) or 0.0)
            work_wall_total += float(wday.get("wall_seconds", 0.0) or 0.0)
            work_runs_total += int(wday.get("runs", 0) or 0)
            for wt in wday.get("tools") or []:
                tk = wt["tool"]
                tool_work_sec[tk] = tool_work_sec.get(tk, 0.0) + float(wt.get("agent_seconds", 0.0) or 0.0)
                tool_work_runs[tk] = tool_work_runs.get(tk, 0) + int(wt.get("runs", 0) or 0)
                # Ensure tools with work hours but subscription-only (0 token log) still appear in unified matrix
                tool_acc.setdefault(
                    tk,
                    {
                        "in": 0,
                        "out": 0,
                        "cr": 0,
                        "cw": 0,
                        "thoughts": 0,
                        "cost_usd": 0.0,
                        "active_days": set(),
                        "models": {},
                    },
                )

    total_tokens = total_in + total_out + total_cr + total_cw + total_thoughts
    denom_in = total_in + total_cr + total_cw
    cache_hit_rate = round(total_cr / denom_in, 4) if denom_in > 0 else 0.0

    tools_list: List[Dict[str, Any]] = []
    for tool, v in tool_acc.items():
        tok = v["in"] + v["out"] + v["cr"] + v["cw"] + v["thoughts"]
        w_sec = tool_work_sec.get(tool, 0.0)
        if tok <= 0 and v["cost_usd"] <= 0 and w_sec <= 0:
            continue
        meta = USAGE_TOOL_META.get(tool, {"name": tool, "short": tool, "color": "#94A3B8"})
        models_out = []
        for m_name, mv in sorted(
            v["models"].items(),
            key=lambda kv: (kv[1]["cost_usd"], kv[1]["in"] + kv[1]["out"] + kv[1]["cr"]),
            reverse=True,
        )[:6]:
            m_tok = mv["in"] + mv["out"] + mv["cr"] + mv["cw"] + mv["thoughts"]
            if m_tok <= 0 and mv["cost_usd"] <= 0:
                continue
            models_out.append(
                {
                    "model": m_name,
                    "input_tokens": mv["in"],
                    "output_tokens": mv["out"],
                    "cache_read_tokens": mv["cr"],
                    "cache_write_tokens": mv["cw"],
                    "reasoning_tokens": mv["thoughts"],
                    "total_tokens": m_tok,
                    "cost_usd": round(mv["cost_usd"], 4),
                }
            )
        tools_list.append(
            {
                "tool": tool,
                "display_name": meta["name"],
                "short_name": meta["short"],
                "color": meta["color"],
                "input_tokens": v["in"],
                "output_tokens": v["out"],
                "cache_read_tokens": v["cr"],
                "cache_write_tokens": v["cw"],
                "reasoning_tokens": v["thoughts"],
                "total_tokens": tok,
                "cost_usd": round(v["cost_usd"], 2),
                "active_days": len(v["active_days"]),
                "agent_seconds": round(w_sec, 1),
                "agent_hours": round(w_sec / 3600.0, 2),
                "runs": tool_work_runs.get(tool, 0),
                "share_tokens": round(tok / total_tokens, 4) if total_tokens > 0 else 0.0,
                "models": models_out,
            }
        )

    # Sort: prioritize tools with recent activity (cost + work hours + tokens)
    tools_list.sort(
        key=lambda x: (x["cost_usd"], x["agent_seconds"], x["total_tokens"]),
        reverse=True,
    )

    leverage = round(work_seconds_total / work_wall_total, 2) if work_wall_total >= 1.0 else 1.0
    return {
        "start_date": start_day,
        "end_date": end_day,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_tokens": total_cr,
        "cache_write_tokens": total_cw,
        "reasoning_tokens": total_thoughts,
        "total_tokens": total_tokens,
        "cost_usd": round(total_cost, 2),
        "cache_hit_rate": cache_hit_rate,
        "active_days": len(active_days_set),
        "agent_seconds": round(work_seconds_total, 1),
        "agent_hours": round(work_seconds_total / 3600.0, 2),
        "workdays": round(work_seconds_total / (8.0 * 3600.0), 2),
        "wall_hours": round(work_wall_total / 3600.0, 2),
        "leverage": leverage,
        "runs": work_runs_total,
        "tools": tools_list,
    }


def build_usage_payload(
    ledger_path: Optional[Path],
    live_usage: Dict[str, Dict[str, Dict[str, Any]]],
    live_cutoff_day: str,
    now_ts: float,
    work_daily_list: Optional[List[Dict[str, Any]]] = None,
    persist: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Update ledger, reconcile with imported history, and return (usage_payload, daily_usage_map)."""
    ledger = load_ledger(ledger_path) if ledger_path else {
        "version": LEDGER_VERSION,
        "updated_at": None,
        "tokei_import_meta": None,
        "imported_tools": {},
        "live_tools": {},
    }

    if persist and ledger_path:
        changed = update_ledger_with_live(ledger, live_usage, live_cutoff_day, now_ts)
        if changed or not ledger_path.is_file():
            save_ledger(ledger_path, ledger)

    merged = _reconcile_merged_tools(ledger, live_usage)

    now_dt = datetime.fromtimestamp(now_ts, tz=BIZ_TZ)
    today_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=BIZ_TZ)
    today_str = today_dt.strftime("%Y-%m-%d")
    # Rolling 7d matches AgentPulse's 7-day window cleanly
    week_start_str = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")
    month_start_str = today_dt.strftime("%Y-%m-01")
    year_start_str = today_dt.strftime("%Y-01-01")

    work_by_day: Dict[str, Dict[str, Any]] = {}
    if work_daily_list:
        for d in work_daily_list:
            work_by_day[d["date"]] = d

    # Daily usage map for all known days (for joining onto DayStats and 30d history chart)
    daily_usage_map: Dict[str, Dict[str, Any]] = {}
    for tool, day_map in merged.items():
        for day_key, b in day_map.items():
            b_in = _safe_int(b.get("in"))
            b_out = _safe_int(b.get("out"))
            b_cr = _safe_int(b.get("cr"))
            b_cw = _safe_int(b.get("cw"))
            b_th = _safe_int(b.get("thoughts"))
            b_cost = _safe_float(b.get("cost_usd", b.get("cost")))
            b_tok = b_in + b_out + b_cr + b_cw + b_th
            if b_tok <= 0 and b_cost <= 0:
                continue
            d_acc = daily_usage_map.setdefault(
                day_key,
                {
                    "in": 0,
                    "out": 0,
                    "cr": 0,
                    "cw": 0,
                    "thoughts": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "tools": {},
                },
            )
            d_acc["in"] += b_in
            d_acc["out"] += b_out
            d_acc["cr"] += b_cr
            d_acc["cw"] += b_cw
            d_acc["thoughts"] += b_th
            d_acc["total_tokens"] += b_tok
            d_acc["cost_usd"] = round(d_acc["cost_usd"] + b_cost, 4)
            t_cur = d_acc["tools"].setdefault(tool, {"tokens": 0, "cost_usd": 0.0})
            t_cur["tokens"] += b_tok
            t_cur["cost_usd"] = round(t_cur["cost_usd"] + b_cost, 4)

    for d_val in daily_usage_map.values():
        denom = d_val["in"] + d_val["cr"] + d_val["cw"]
        d_val["cache_hit_rate"] = round(d_val["cr"] / denom, 4) if denom > 0 else 0.0
        d_val["cost_usd"] = round(d_val["cost_usd"], 2)

    # 30-day history array
    history_30d: List[Dict[str, Any]] = []
    for i in range(29, -1, -1):
        dt = today_dt - timedelta(days=i)
        dk = dt.strftime("%Y-%m-%d")
        du = daily_usage_map.get(dk) or {}
        wd = work_by_day.get(dk) or {}
        history_30d.append(
            {
                "date": dk,
                "short_date": dt.strftime("%m/%d"),
                "weekday": WEEKDAY_CN[dt.weekday()],
                "is_today": dk == today_str,
                "total_tokens": int(du.get("total_tokens", 0)),
                "cost_usd": round(float(du.get("cost_usd", 0.0)), 2),
                "agent_hours": round(float(wd.get("agent_hours", 0.0)), 2),
            }
        )

    periods = {
        "today": _build_period_summary(merged, today_str, today_str, work_by_day),
        "week": _build_period_summary(merged, week_start_str, today_str, work_by_day),
        "month": _build_period_summary(merged, month_start_str, today_str, work_by_day),
        "year": _build_period_summary(merged, year_start_str, today_str, work_by_day),
    }

    # Ledger metadata (imported history stats)
    imported_tools = ledger.get("imported_tools") or {}
    imported_tool_count = sum(1 for v in imported_tools.values() if isinstance(v, dict) and v)
    max_history_days = max(
        (len(v) for v in merged.values() if isinstance(v, dict)),
        default=0,
    )
    earliest_day = min(
        (min(v.keys()) for v in merged.values() if isinstance(v, dict) and v),
        default=today_str,
    )

    usage_payload = {
        "source": "agent_pulse_native",
        "periods": periods,
        "history_30d": history_30d,
        "ledger_meta": {
            "ledger_path": str(ledger_path) if ledger_path else "",
            "imported_tokei": imported_tool_count > 0,
            "imported_tool_count": imported_tool_count,
            "max_tool_days": max_history_days,
            "earliest_day": earliest_day,
            "tokei_import_meta": ledger.get("tokei_import_meta"),
        },
    }
    return usage_payload, daily_usage_map
