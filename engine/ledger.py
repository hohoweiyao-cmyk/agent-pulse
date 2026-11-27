#!/usr/bin/env python3
"""AgentPulse — Persistent local usage ledger & multi-period aggregator (v0.7).

Maintains ~/.agent-pulse/ledger.json (version 1) with separate `imported_tools`
(from Tokei historical ledger) and `live_tools` (incrementally updated by AgentPulse),
plus v0.7 fields: `collection`, `project_work_history_from`, `project_work_days`,
`billing_days`, and optional `work_days[day].hourly_seconds`.

v0.7 guarantees:
- Per-source / per-domain persistence & projection gate: partial or unavailable
  sources never overwrite confirmed ledger buckets and never bypass into reports.
- `collection.commit_ts` concurrency gate inside the ledger lock: older scans are
  rejected and projections read the accepted on-disk ledger.
- Git worktree-merged `usage.project_trails` with 4 periods (`today|week|month|all`),
  while keeping legacy `usage.projects` untouched.
- Independent `billing` attribution (`subscription_candidate_usd`, `api_actual_usd`)
  with Decimal accumulation and Tokei reconciliation exclusion for candidates.
"""

from __future__ import annotations

import json
import math
import tempfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from health import (
    PROJECT_USAGE_SOURCE_IDS,
    TOOL_TO_USAGE_SOURCE,
    WORK_SOURCE_IDS,
    build_collection_payload,
    collection_payload_from_ledger,
    combine_statuses,
    normalize_source_statuses,
)
from storage import copy_private, private_directory, protect_existing_file, state_lock, write_json
from worktree import WorktreeResolver

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
_HISTORY_FILES = ("quota_history.json", "quota_cycles.json")


def resolve_data_dir(home: Optional[Path] = None, create: bool = True) -> Path:
    """Return ~/.agent-pulse, safely seeding from legacy ~/.agent-clock (read-only) on first run."""
    home_dir = home or Path.home()
    data_dir = home_dir / DATA_DIR_NAME
    legacy_dir = home_dir / LEGACY_DATA_DIR_NAME

    if not create:
        return data_dir

    private_directory(data_dir)
    marker = data_dir / ".data-layout-v1"
    if not marker.is_file():
        with state_lock(marker):
            if not marker.is_file():
                for fname in (*_LEGACY_MIGRATE_FILES, "cache.json", "usage_cache.json"):
                    protect_existing_file(data_dir / fname)
                for name in ("history", "backups"):
                    directory = data_dir / name
                    if directory.exists():
                        private_directory(directory)
                if legacy_dir.is_dir():
                    for name in _LEGACY_MIGRATE_FILES[1:]:
                        src, dst = legacy_dir / name, data_dir / name
                        if src.is_file() and not dst.exists():
                            copy_private(src, dst)
                    for name in _HISTORY_FILES:
                        src = legacy_dir / "history" / name
                        dst = data_dir / "history" / name
                        if src.is_file() and not dst.exists():
                            private_directory(dst.parent)
                            copy_private(src, dst)
                    src, dst = legacy_dir / "ledger.json", data_dir / "ledger.json"
                    if src.is_file() and not dst.exists():
                        copy_private(src, dst)
                write_json(marker, {"version": 1})
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

TOKEI_TOOL_MAP = {
    "claude": "claude_code",
}


def canonical_tool_key(raw_key: str) -> str:
    return TOKEI_TOOL_MAP.get(raw_key, raw_key)


def _safe_int(v: Any) -> int:
    try:
        n = int(v)
        return n if n > 0 else 0
    except (TypeError, ValueError, OverflowError):
        return 0


def _safe_float(v: Any) -> float:
    if isinstance(v, bool):
        return 0.0
    try:
        n = float(v)
        return n if n > 0 and math.isfinite(n) else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def _safe_nonneg_decimal(v: Any) -> Optional[Decimal]:
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
        if not math.isfinite(f) or f < 0:
            return None
        return Decimal(str(v)) if isinstance(v, (int, str)) else Decimal(str(f))
    except Exception:
        return None


def _bucket_total_tokens(b: Dict[str, Any]) -> int:
    return (
        _safe_int(b.get("in"))
        + _safe_int(b.get("out"))
        + _safe_int(b.get("cr"))
        + _safe_int(b.get("cw"))
        + _safe_int(b.get("thoughts"))
    )


def _unpriced_tokens(bucket: Dict[str, Any], tokens: int, cost: float) -> int:
    if "unpriced_tokens" in bucket:
        return min(tokens, _safe_int(bucket["unpriced_tokens"]))
    return tokens if cost == 0 else 0


class LedgerError(ValueError):
    """The ledger could not be read, validated or restored safely."""


def valid_day(value: str) -> bool:
    try:
        return isinstance(value, str) and date.fromisoformat(value).isoformat() == value
    except (TypeError, ValueError):
        return False


def _empty_ledger() -> Dict[str, Any]:
    return {
        "version": LEDGER_VERSION,
        "updated_at": None,
        "tokei_import_meta": None,
        "imported_tools": {},
        "live_tools": {},
        "projects": {},
        "work_days": {},
    }


def load_ledger(path: Path, *, required: bool = False) -> Dict[str, Any]:
    try:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise OSError("expected a regular ledger file")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        if required:
            raise LedgerError("找不到指定的账本备份") from exc
        return _empty_ledger()
    except (OSError, UnicodeError, ValueError) as exc:
        raise LedgerError("账本无法读取，已保留原文件。请检查数据目录或恢复有效备份。") from exc

    if (
        not isinstance(raw, dict)
        or raw.get("version", LEDGER_VERSION) != LEDGER_VERSION
        or not raw.keys() & {"imported_tools", "live_tools", "projects", "work_days"}
    ):
        raise LedgerError("账本格式或版本不受支持，已保留原文件。")
    for key in ("imported_tools", "live_tools", "projects", "work_days"):
        if not isinstance(raw.setdefault(key, {}), dict):
            raise LedgerError("账本结构无效，已保留原文件。")
        day_maps = (raw[key],) if key == "work_days" else raw[key].values()
        if any(
            not isinstance(days, dict) or any(not isinstance(bucket, dict) for bucket in days.values())
            for days in day_maps
        ):
            raise LedgerError("账本日记录结构无效，已保留原文件。")
    raw.setdefault("version", LEDGER_VERSION)
    return raw


def save_ledger(path: Path, ledger: Dict[str, Any]) -> None:
    """Commit a validated transaction while the caller holds the ledger lock."""
    if path.is_file():
        copy_private(path, path.with_name("ledger.previous.json"))
    write_json(path, ledger)


def restore_ledger(path: Path, backup: Path) -> Optional[Path]:
    """Restore a validated backup and preserve the current ledger separately."""
    with state_lock(path):
        replacement = load_ledger(backup, required=True)
        for key in ("imported_tools", "live_tools", "projects", "work_days"):
            groups = (replacement[key],) if key == "work_days" else replacement[key].values()
            if any(not valid_day(day) for days in groups for day in days):
                raise LedgerError("备份包含无效日期，原账本未替换。")
        for opt_key in ("project_work_days", "billing_days"):
            opt_val = replacement.get(opt_key)
            if isinstance(opt_val, dict):
                if any(
                    not isinstance(days, dict) or any(not valid_day(day) for day in days)
                    for days in opt_val.values()
                ):
                    raise LedgerError("备份包含无效日期，原账本未替换。")
        saved = None
        if path.is_file():
            backups = path.parent / "backups"
            private_directory(backups)
            folder = Path(tempfile.mkdtemp(prefix="before-restore-", dir=backups))
            saved = folder / path.name
            copy_private(path, saved)
        write_json(path, replacement)
        return saved


def update_ledger_with_live(
    ledger: Dict[str, Any],
    live_usage: Dict[str, Dict[str, Dict[str, Any]]],
    live_cutoff_day: str,
    now_ts: float,
    live_projects: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    replace_projects_window: Optional[Tuple[str, str]] = None,
) -> bool:
    """Merge live_usage & live_projects into ledger for days >= live_cutoff_day. Returns True if changed."""
    live_tools = ledger.setdefault("live_tools", {})
    changed = False

    for tool_key, day_map in live_usage.items():
        t_dst = live_tools.setdefault(tool_key, {})
        for day_key, bucket in day_map.items():
            if day_key < live_cutoff_day:
                continue
            if t_dst.get(day_key) != bucket:
                t_dst[day_key] = bucket
                changed = True

    if live_projects is not None:
        proj_store = ledger.setdefault("projects", {})
        if replace_projects_window is not None:
            win_start, win_end = replace_projects_window
            for proj_path in list(proj_store.keys()):
                p_dst = proj_store.get(proj_path)
                if not isinstance(p_dst, dict):
                    continue
                new_days = live_projects.get(proj_path) or {}
                for day_key in list(p_dst.keys()):
                    if valid_day(day_key) and win_start <= day_key <= win_end and day_key not in new_days:
                        del p_dst[day_key]
                        changed = True
                if not p_dst:
                    del proj_store[proj_path]
                    changed = True

        for proj_path, day_map in live_projects.items():
            p_dst = proj_store.setdefault(proj_path, {})
            for day_key, bucket in day_map.items():
                if day_key < live_cutoff_day:
                    continue
                if p_dst.get(day_key) != bucket:
                    p_dst[day_key] = bucket
                    changed = True

    if changed or not ledger.get("updated_at"):
        ledger["updated_at"] = datetime.fromtimestamp(now_ts, tz=BIZ_TZ).isoformat()
    return changed


def _reconcile_merged_tools_with_winners(
    ledger: Dict[str, Any],
    live_usage: Dict[str, Dict[str, Dict[str, Any]]],
) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], Dict[str, Set[str]]]:
    """Produce unified {tool: {day: bucket}} and map of {tool: set(days where Tokei won)}."""
    imported = ledger.get("imported_tools") or {}
    persisted_live = ledger.get("live_tools") or {}

    native: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for source in (persisted_live, live_usage):
        for tool, days in source.items():
            if not isinstance(days, dict):
                continue
            native.setdefault(tool, {}).update(
                {day: bucket for day, bucket in days.items() if valid_day(day)}
            )

    merged = {tool: dict(days) for tool, days in native.items()}
    tokei_won: Dict[str, Set[str]] = {}

    for raw_tool, day_map in imported.items():
        if not isinstance(day_map, dict):
            continue
        tool = canonical_tool_key(raw_tool)
        dst = merged.setdefault(tool, {})
        for day_key, bucket in day_map.items():
            if not isinstance(bucket, dict) or not valid_day(day_key):
                continue
            native_bucket = native.get(tool, {}).get(day_key, {})
            if tool == "codex":
                t3 = native.get("t3_codex", {}).get(day_key, {})
                tokens = _bucket_total_tokens(native_bucket) + _bucket_total_tokens(t3)
                if (native_bucket or t3) and tokens >= _bucket_total_tokens(bucket):
                    continue
                if t3:
                    merged["t3_codex"].pop(day_key, None)
                tokei_won.setdefault("codex", set()).add(day_key)
                tokei_won.setdefault("t3_codex", set()).add(day_key)
            elif native_bucket and _bucket_total_tokens(native_bucket) >= _bucket_total_tokens(bucket):
                continue
            else:
                tokei_won.setdefault(tool, set()).add(day_key)
            dst[day_key] = bucket

    return merged, tokei_won


def _reconcile_merged_tools(
    ledger: Dict[str, Any],
    live_usage: Dict[str, Dict[str, Dict[str, Any]]],
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    merged, _ = _reconcile_merged_tools_with_winners(ledger, live_usage)
    return merged


def _build_period_summary(
    merged: Dict[str, Dict[str, Dict[str, Any]]],
    start_day: str,
    end_day: str,
    work_by_day: Optional[Dict[str, Dict[str, Any]]] = None,
    pricing: Optional[Any] = None,
    billing_by_tool: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    tokei_won: Optional[Dict[str, Set[str]]] = None,
    work_complete: bool = True,
) -> Dict[str, Any]:
    total_in = total_out = total_cr = total_cw = total_thoughts = 0
    total_cost = 0.0
    active_days_set: set = set()
    tool_acc: Dict[str, Dict[str, Any]] = {}

    def ensure_tool(tk: str) -> Dict[str, Any]:
        return tool_acc.setdefault(
            tk,
            {
                "in": 0,
                "out": 0,
                "cr": 0,
                "cw": 0,
                "thoughts": 0,
                "cost_usd": 0.0,
                "sessions": 0,
                "active_days": set(),
                "models": {},
            },
        )

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
            b_sess = _safe_int(b.get("sessions"))
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

            t_entry = ensure_tool(tool)
            t_entry["in"] += b_in
            t_entry["out"] += b_out
            t_entry["cr"] += b_cr
            t_entry["cw"] += b_cw
            t_entry["thoughts"] += b_th
            t_entry["cost_usd"] += b_cost
            t_entry["unpriced_tokens"] = t_entry.get("unpriced_tokens", 0) + _unpriced_tokens(b, b_tok, b_cost)
            t_entry["sessions"] += b_sess if b_sess > 0 else 1
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
                model_cost = _safe_float(m_val.get("cost_usd", m_val.get("cost")))
                m_dst["cost_usd"] += model_cost
                m_dst["unpriced_tokens"] = m_dst.get("unpriced_tokens", 0) + _unpriced_tokens(
                    m_val, _bucket_total_tokens(m_val), model_cost
                )

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
            if _safe_float(wday.get("agent_seconds")) >= 60:
                active_days_set.add(day_key)
            work_wall_total += float(wday.get("wall_seconds", 0.0) or 0.0)
            work_runs_total += int(wday.get("runs", 0) or 0)
            for wt in wday.get("tools") or []:
                tk = wt["tool"]
                tool_work_sec[tk] = tool_work_sec.get(tk, 0.0) + float(wt.get("agent_seconds", 0.0) or 0.0)
                tool_work_runs[tk] = tool_work_runs.get(tk, 0) + int(wt.get("runs", 0) or 0)
                ensure_tool(tk)

    # Billing aggregation (subscription_candidate_usd & verified api_actual_usd)
    tool_billing_dec: Dict[str, Dict[str, Decimal]] = {}
    won_map = tokei_won or {}
    if billing_by_tool:
        for tool, day_map in billing_by_tool.items():
            if not isinstance(day_map, dict):
                continue
            won_days = won_map.get(tool, set())
            for day_key, b_entry in day_map.items():
                if not isinstance(b_entry, dict) or not valid_day(day_key):
                    continue
                if day_key < start_day or day_key > end_day:
                    continue
                if "subscription_candidate_usd" in b_entry and day_key not in won_days:
                    dec = _safe_nonneg_decimal(b_entry.get("subscription_candidate_usd"))
                    if dec is not None:
                        tb = tool_billing_dec.setdefault(tool, {})
                        tb["subscription_candidate_usd"] = tb.get("subscription_candidate_usd", Decimal("0")) + dec
                if "api_actual_usd" in b_entry:
                    dec = _safe_nonneg_decimal(b_entry.get("api_actual_usd"))
                    if dec is not None:
                        tb = tool_billing_dec.setdefault(tool, {})
                        tb["api_actual_usd"] = tb.get("api_actual_usd", Decimal("0")) + dec
                        ensure_tool(tool)

    total_tokens = total_in + total_out + total_cr + total_cw + total_thoughts
    denom_in = total_in + total_cr + total_cw
    cache_hit_rate = round(total_cr / denom_in, 4) if denom_in > 0 else 0.0

    tools_list: List[Dict[str, Any]] = []
    cross_models: List[Dict[str, Any]] = []
    for tool, v in tool_acc.items():
        tok = v["in"] + v["out"] + v["cr"] + v["cw"] + v["thoughts"]
        w_sec = tool_work_sec.get(tool, 0.0)
        b_dec = tool_billing_dec.get(tool)
        has_verified_actual = bool(b_dec and "api_actual_usd" in b_dec)
        if tok <= 0 and v["cost_usd"] <= 0 and w_sec <= 0 and not has_verified_actual:
            continue
        meta = USAGE_TOOL_META.get(tool, {"name": tool, "short": tool, "color": "#94A3B8"})
        t_denom = v["in"] + v["cr"] + v["cw"]
        t_hit = round(v["cr"] / t_denom, 4) if t_denom > 0 else 0.0
        t_runs = tool_work_runs.get(tool, 0)
        t_sessions = v["sessions"] if v["sessions"] > 0 else max(t_runs, len(v["active_days"]))

        models_out = []
        for m_name, mv in sorted(
            v["models"].items(),
            key=lambda kv: (kv[1]["cost_usd"], kv[1]["in"] + kv[1]["out"] + kv[1]["cr"]),
            reverse=True,
        )[:20]:
            m_tok = mv["in"] + mv["out"] + mv["cr"] + mv["cw"] + mv["thoughts"]
            if m_tok <= 0 and mv["cost_usd"] <= 0:
                continue
            m_denom = mv["in"] + mv["cr"] + mv["cw"]
            m_hit = round(mv["cr"] / m_denom, 4) if m_denom > 0 else 0.0
            rates = pricing.resolve(m_name) if pricing is not None else None
            m_obj = {
                "model": m_name,
                "input_tokens": mv["in"],
                "output_tokens": mv["out"],
                "cache_read_tokens": mv["cr"],
                "cache_write_tokens": mv["cw"],
                "reasoning_tokens": mv["thoughts"],
                "total_tokens": m_tok,
                "cost_usd": round(mv["cost_usd"], 6),
                "unpriced_tokens": mv.get("unpriced_tokens", 0),
                "cache_hit_rate": m_hit,
                "rate_in": round(rates["in"], 4) if rates else None,
                "rate_out": round(rates["out"], 4) if rates else None,
                "rate_cache_read": round(rates["cache_read"], 4) if rates else None,
                "rate_cache_write": round(rates["cache_write"], 4) if rates else None,
            }
            models_out.append(m_obj)
            cross_models.append(
                {
                    "tool": tool,
                    "display_name": meta["name"],
                    "short_name": meta["short"],
                    "color": meta["color"],
                    "model": m_name,
                    "input_tokens": mv["in"],
                    "output_tokens": mv["out"],
                    "cache_read_tokens": mv["cr"],
                    "cache_write_tokens": mv["cw"],
                    "reasoning_tokens": mv["thoughts"],
                    "total_tokens": m_tok,
                    "cost_usd": round(mv["cost_usd"], 6),
                    "unpriced_tokens": mv.get("unpriced_tokens", 0),
                    "cache_hit_rate": m_hit,
                }
            )

        tool_obj: Dict[str, Any] = {
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
            "cost_usd": round(v["cost_usd"], 6),
            "unpriced_tokens": v.get("unpriced_tokens", 0),
            "cache_hit_rate": t_hit,
            "sessions": t_sessions,
            "active_days": len(v["active_days"]),
            "agent_seconds": round(w_sec, 1),
            "agent_hours": round(w_sec / 3600.0, 2),
            "runs": t_runs,
            "share_tokens": round(tok / total_tokens, 4) if total_tokens > 0 else 0.0,
            "models": models_out,
        }
        if b_dec:
            b_payload: Dict[str, float] = {}
            if "subscription_candidate_usd" in b_dec:
                b_payload["subscription_candidate_usd"] = round(float(b_dec["subscription_candidate_usd"]), 6)
            if "api_actual_usd" in b_dec:
                b_payload["api_actual_usd"] = round(float(b_dec["api_actual_usd"]), 6)
            if b_payload:
                tool_obj["billing"] = b_payload
        tools_list.append(tool_obj)

    tools_list.sort(
        key=lambda x: (x["cost_usd"], x["agent_seconds"], x["total_tokens"]),
        reverse=True,
    )
    cross_models.sort(key=lambda x: (x["cost_usd"], x["total_tokens"]), reverse=True)

    leverage = round(work_seconds_total / work_wall_total, 2) if work_wall_total >= 1.0 else 1.0
    has_work_in_period = (work_seconds_total > 0.0 or work_runs_total > 0)
    if work_complete:
        period_work_status = "observed"
    elif has_work_in_period:
        period_work_status = "retained"
    else:
        period_work_status = "unknown"
    return {
        "start_date": start_day,
        "end_date": end_day,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "cache_read_tokens": total_cr,
        "cache_write_tokens": total_cw,
        "reasoning_tokens": total_thoughts,
        "total_tokens": total_tokens,
        "cost_usd": round(total_cost, 6),
        "unpriced_tokens": sum(t["unpriced_tokens"] for t in tools_list),
        "cache_hit_rate": cache_hit_rate,
        "active_days": len(active_days_set),
        "agent_seconds": round(work_seconds_total, 1),
        "agent_hours": round(work_seconds_total / 3600.0, 2),
        "workdays": round(work_seconds_total / (8.0 * 3600.0), 2),
        "wall_hours": round(work_wall_total / 3600.0, 2),
        "leverage": leverage,
        "runs": work_runs_total,
        "work_status": period_work_status,
        "tools": tools_list,
        "top_models": cross_models[:12],
    }


def _compute_streaks(active_dates: set, today_dt: datetime) -> Tuple[int, int]:
    if not active_dates:
        return 0, 0
    sorted_days = sorted(active_dates)
    longest = 1
    run = 1
    prev_dt = datetime.strptime(sorted_days[0], "%Y-%m-%d")
    for ds in sorted_days[1:]:
        cur_dt = datetime.strptime(ds, "%Y-%m-%d")
        if (cur_dt - prev_dt).days == 1:
            run += 1
            if run > longest:
                longest = run
        else:
            run = 1
        prev_dt = cur_dt

    today_str = today_dt.strftime("%Y-%m-%d")
    yest_str = (today_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    anchor = today_dt if today_str in active_dates else (today_dt - timedelta(days=1) if yest_str in active_dates else None)
    current = 0
    if anchor is not None:
        cur = anchor
        while cur.strftime("%Y-%m-%d") in active_dates:
            current += 1
            cur -= timedelta(days=1)
    return current, longest


def _abbreviate_path(path_str: str, home_dir: Optional[Path]) -> str:
    if home_dir is not None:
        h = str(home_dir).rstrip("/")
        if path_str == h:
            return "~"
        if path_str.startswith(h + "/"):
            return "~" + path_str[len(h):]
        try:
            hr = str(home_dir.resolve()).rstrip("/")
            if path_str == hr:
                return "~"
            if path_str.startswith(hr + "/"):
                return "~" + path_str[len(hr):]
        except OSError:
            pass
    return path_str


def _combine_usage_projects(
    ledger: Dict[str, Any],
    live_projects: Optional[Dict[str, Dict[str, Dict[str, Any]]]],
    replace_projects_window: Optional[Tuple[str, str]] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    combined: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for proj_path, day_map in (ledger.get("projects") or {}).items():
        if isinstance(day_map, dict):
            combined[proj_path] = {dk: bv for dk, bv in day_map.items() if valid_day(dk)}
    if live_projects is not None and replace_projects_window is not None:
        win_start, win_end = replace_projects_window
        for proj_path in list(combined.keys()):
            new_days = live_projects.get(proj_path) or {}
            dmap = combined[proj_path]
            for dk in list(dmap.keys()):
                if win_start <= dk <= win_end and dk not in new_days:
                    del dmap[dk]
            if not dmap:
                del combined[proj_path]
    if live_projects:
        for proj_path, day_map in live_projects.items():
            dst = combined.setdefault(proj_path, {})
            for dk, bv in day_map.items():
                if valid_day(dk):
                    dst[dk] = bv
    return combined


def _build_projects_list(
    ledger: Dict[str, Any],
    live_projects: Optional[Dict[str, Dict[str, Dict[str, Any]]]],
    today_dt: datetime,
    home_dir: Optional[Path],
    replace_projects_window: Optional[Tuple[str, str]] = None,
) -> List[Dict[str, Any]]:
    combined = _combine_usage_projects(ledger, live_projects, replace_projects_window=replace_projects_window)

    today_str = today_dt.strftime("%Y-%m-%d")
    week_floor = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")
    month_floor = (today_dt - timedelta(days=30)).strftime("%Y-%m-%d")

    out: List[Dict[str, Any]] = []
    for proj_path, day_map in combined.items():
        if not day_map:
            continue
        p_in = p_out = p_cr = p_cw = p_th = p_sess = p_unpriced = 0
        p_cost = 0.0
        tool_keys: List[str] = []
        last_day = ""
        for dk, b in day_map.items():
            b_in = _safe_int(b.get("in"))
            b_out = _safe_int(b.get("out"))
            b_cr = _safe_int(b.get("cr"))
            b_cw = _safe_int(b.get("cw"))
            b_th = _safe_int(b.get("thoughts"))
            b_cost = _safe_float(b.get("cost_usd", b.get("cost")))
            b_tok = b_in + b_out + b_cr + b_cw + b_th
            if b_tok <= 0 and b_cost <= 0:
                continue
            p_in += b_in
            p_out += b_out
            p_cr += b_cr
            p_cw += b_cw
            p_th += b_th
            p_cost += b_cost
            p_unpriced += _unpriced_tokens(b, b_tok, b_cost)
            p_sess += _safe_int(b.get("sessions")) or 1
            if dk > last_day:
                last_day = dk
            for tk in b.get("tools") or []:
                if isinstance(tk, str) and tk not in tool_keys:
                    tool_keys.append(tk)

        total_tok = p_in + p_out + p_cr + p_cw + p_th
        if total_tok <= 0 and p_cost <= 0:
            continue
        denom = p_in + p_cr + p_cw
        hit = round(p_cr / denom, 4) if denom > 0 else 0.0

        if last_day >= today_str:
            grp = "today"
        elif last_day >= week_floor:
            grp = "week"
        elif last_day >= month_floor:
            grp = "earlier"
        else:
            grp = "dormant"

        tools_badges = []
        for tk in tool_keys:
            meta = USAGE_TOOL_META.get(tk, {"name": tk, "short": tk, "color": "#94A3B8"})
            tools_badges.append(
                {
                    "tool": tk,
                    "display_name": meta["name"],
                    "short_name": meta["short"],
                    "color": meta["color"],
                }
            )

        out.append(
            {
                "path": proj_path,
                "name": Path(proj_path).name or proj_path,
                "display_path": _abbreviate_path(proj_path, home_dir),
                "last_active_date": last_day,
                "group": grp,
                "input_tokens": p_in,
                "output_tokens": p_out,
                "cache_read_tokens": p_cr,
                "cache_write_tokens": p_cw,
                "reasoning_tokens": p_th,
                "total_tokens": total_tok,
                "cost_usd": round(p_cost, 6),
                "unpriced_tokens": p_unpriced,
                "cache_hit_rate": hit,
                "sessions": p_sess,
                "tools": tools_badges,
            }
        )

    out.sort(key=lambda x: (x["last_active_date"], x["cost_usd"], x["total_tokens"]), reverse=True)
    return out


def _build_project_trails(
    usage_projects_combined: Dict[str, Dict[str, Dict[str, Any]]],
    work_projects_combined: Dict[str, Dict[str, Dict[str, Any]]],
    today_dt: datetime,
    home_dir: Optional[Path],
    resolver: Optional[WorktreeResolver] = None,
) -> List[Dict[str, Any]]:
    """Merge verified linked worktrees into parent projects and project 4 periods (`today|week|month|all`)."""
    wt_resolver = resolver or WorktreeResolver()
    today_str = today_dt.strftime("%Y-%m-%d")
    this_mon = today_dt - timedelta(days=today_dt.weekday())
    week_start_str = this_mon.strftime("%Y-%m-%d")
    month_start_str = today_dt.strftime("%Y-%m-01")
    week_floor = (today_dt - timedelta(days=6)).strftime("%Y-%m-%d")
    month_floor = (today_dt - timedelta(days=30)).strftime("%Y-%m-%d")

    all_raw_paths = set(usage_projects_combined.keys()) | set(work_projects_combined.keys())
    if not all_raw_paths:
        return []

    groups: Dict[str, List[Tuple[str, Any]]] = {}
    for raw_path in sorted(all_raw_paths):
        res = wt_resolver.resolve(raw_path)
        canon = res.canonical_root or raw_path
        groups.setdefault(canon, []).append((raw_path, res))

    # Determine earliest valid date across all projects for "all" period start_date
    all_dates: List[str] = []
    for day_map in usage_projects_combined.values():
        all_dates.extend(dk for dk in day_map if valid_day(dk))
    for day_map in work_projects_combined.values():
        all_dates.extend(dk for dk in day_map if valid_day(dk))
    global_earliest = min(all_dates) if all_dates else today_str

    period_defs = (
        ("today", today_str, today_str),
        ("week", week_start_str, today_str),
        ("month", month_start_str, today_str),
        ("all", global_earliest, today_str),
    )

    trails: List[Dict[str, Any]] = []
    for canon_root, members in groups.items():
        periods_out: Dict[str, Dict[str, Any]] = {}

        for p_key, p_start, p_end in period_defs:
            p_in = p_out = p_cr = p_cw = p_th = p_sess = p_unpriced = 0
            p_cost = 0.0
            p_work_sec = 0.0
            has_work = False
            has_usage = False
            last_day = ""
            tool_keys: List[str] = []
            sources_map: Dict[str, Optional[str]] = {}

            for raw_path, res in members:
                member_active = False

                for dk, b in (usage_projects_combined.get(raw_path) or {}).items():
                    if not valid_day(dk) or dk < p_start or dk > p_end:
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
                    has_usage = True
                    member_active = True
                    p_in += b_in
                    p_out += b_out
                    p_cr += b_cr
                    p_cw += b_cw
                    p_th += b_th
                    p_cost += b_cost
                    p_unpriced += _unpriced_tokens(b, b_tok, b_cost)
                    p_sess += _safe_int(b.get("sessions")) or 1
                    if dk > last_day:
                        last_day = dk
                    for tk in b.get("tools") or []:
                        if isinstance(tk, str) and tk not in tool_keys:
                            tool_keys.append(tk)

                for dk, wb in (work_projects_combined.get(raw_path) or {}).items():
                    if not valid_day(dk) or dk < p_start or dk > p_end:
                        continue
                    w_sec = _safe_float(wb.get("agent_seconds"))
                    if w_sec <= 0:
                        continue
                    has_work = True
                    member_active = True
                    p_work_sec += w_sec
                    if dk > last_day:
                        last_day = dk
                    w_tools = wb.get("tools") or {}
                    if isinstance(w_tools, dict):
                        for tk, tv in w_tools.items():
                            if isinstance(tk, str) and isinstance(tv, dict) and _safe_float(tv.get("agent_seconds")) > 0:
                                if tk not in tool_keys:
                                    tool_keys.append(tk)

                if member_active:
                    src_key = res.source_path or raw_path
                    if src_key not in sources_map or (sources_map[src_key] is None and res.branch is not None):
                        sources_map[src_key] = res.branch if res.verified_git else None

            if not has_usage and not has_work:
                continue

            total_tok = p_in + p_out + p_cr + p_cw + p_th
            denom = p_in + p_cr + p_cw
            hit = round(p_cr / denom, 4) if denom > 0 else 0.0

            tools_badges = []
            for tk in tool_keys:
                meta = USAGE_TOOL_META.get(tk, {"name": tk, "short": tk, "color": "#94A3B8"})
                tools_badges.append(
                    {
                        "tool": tk,
                        "display_name": meta["name"],
                        "short_name": meta["short"],
                        "color": meta["color"],
                    }
                )

            wt_sources = [
                {"path": sp, "branch": br}
                for sp, br in sorted(sources_map.items(), key=lambda kv: kv[0])
            ]

            periods_out[p_key] = {
                "start_date": p_start,
                "end_date": p_end,
                "last_active_date": last_day,
                "agent_seconds": round(p_work_sec, 1) if has_work else None,
                "input_tokens": p_in,
                "output_tokens": p_out,
                "cache_read_tokens": p_cr,
                "cache_write_tokens": p_cw,
                "reasoning_tokens": p_th,
                "total_tokens": total_tok,
                "cost_usd": round(p_cost, 6),
                "unpriced_tokens": p_unpriced,
                "has_cost_observation": has_usage,
                "cache_hit_rate": hit,
                "sessions": p_sess,
                "tools": tools_badges,
                "worktree_sources": wt_sources,
            }

        all_p = periods_out.get("all")
        if not all_p:
            continue

        last_day_all = all_p["last_active_date"]
        if last_day_all >= today_str:
            grp = "today"
        elif last_day_all >= week_floor:
            grp = "week"
        elif last_day_all >= month_floor:
            grp = "earlier"
        else:
            grp = "dormant"

        trails.append(
            {
                "path": canon_root,
                "name": Path(canon_root).name or canon_root,
                "display_path": _abbreviate_path(canon_root, home_dir),
                "last_active_date": last_day_all,
                "group": grp,
                "input_tokens": all_p["input_tokens"],
                "output_tokens": all_p["output_tokens"],
                "cache_read_tokens": all_p["cache_read_tokens"],
                "cache_write_tokens": all_p["cache_write_tokens"],
                "reasoning_tokens": all_p["reasoning_tokens"],
                "total_tokens": all_p["total_tokens"],
                "cost_usd": all_p["cost_usd"],
                "unpriced_tokens": all_p["unpriced_tokens"],
                "cache_hit_rate": all_p["cache_hit_rate"],
                "sessions": all_p["sessions"],
                "tools": all_p["tools"],
                "periods": periods_out,
            }
        )

    # Stable multi-pass sort: path asc -> last_active_date desc -> agent_seconds desc
    trails.sort(key=lambda x: x["path"])
    trails.sort(key=lambda x: x["last_active_date"], reverse=True)
    trails.sort(
        key=lambda x: x["periods"]["all"]["agent_seconds"] if x["periods"]["all"]["agent_seconds"] is not None else -1.0,
        reverse=True,
    )
    return trails


def build_usage_payload(
    ledger_path: Optional[Path],
    live_usage: Dict[str, Dict[str, Dict[str, Any]]],
    live_cutoff_day: str,
    now_ts: float,
    work_daily_list: Optional[List[Dict[str, Any]]] = None,
    persist: bool = True,
    pricing: Optional[Any] = None,
    live_projects: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    home: Optional[Path] = None,
    work_history_from: Optional[str] = None,
    source_statuses: Optional[Dict[str, str]] = None,
    project_work_meta_statuses: Optional[Dict[str, str]] = None,
    observed_project_work: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    project_work_history_from: Optional[str] = None,
    live_billing: Optional[Dict[str, Dict[str, Dict[str, float]]]] = None,
    billing_cutoff_day: Optional[str] = None,
    commit_ts: Optional[float] = None,
    worktree_resolver: Optional[WorktreeResolver] = None,
    out_state: Optional[Dict[str, Any]] = None,
    observed_instances: Optional[Dict[str, Set[str]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """Update ledger with per-source/domain & commit_ts gates, reconcile, and build projections."""
    observed_work: Dict[str, Dict[str, Any]] = {}
    for day in work_daily_list or []:
        if day["agent_seconds"] > 0 or day["runs"] > 0:
            rec = {key: day[key] for key in ("agent_seconds", "agent_hours", "wall_seconds", "runs", "tools")}
            hs = day.get("hourly_seconds")
            if isinstance(hs, list) and len(hs) == 24:
                rec["hourly_seconds"] = [round(float(x), 1) for x in hs]
            observed_work[day["date"]] = rec

    now_dt = datetime.fromtimestamp(now_ts, tz=BIZ_TZ)
    today_dt = datetime(now_dt.year, now_dt.month, now_dt.day, tzinfo=BIZ_TZ)
    today_str = today_dt.strftime("%Y-%m-%d")

    def _compute_gates(
        prev_col: Dict[str, Any],
    ) -> Tuple[
        Optional[Dict[str, str]],
        bool,
        bool,
        bool,
        bool,
        str,
        str,
        Dict[str, Dict[str, Dict[str, Any]]],
        Optional[Dict[str, Dict[str, Dict[str, Any]]]],
        Dict[str, Dict[str, Any]],
        Dict[str, Dict[str, Dict[str, Any]]],
        Dict[str, Dict[str, Dict[str, float]]],
    ]:
        if source_statuses is None:
            norm_statuses = None
            proj_u_ok = True
            proj_u_has_complete = True
            w_ok = True
            pw_ok = True
            w_status = "complete"
            pw_status = "complete"
        else:
            norm_statuses = normalize_source_statuses(
                source_statuses,
                prev_col,
                observed_instances=observed_instances,
            )
            proj_usage_status = combine_statuses([norm_statuses.get(s, "not_configured") for s in PROJECT_USAGE_SOURCE_IDS])
            proj_u_ok = proj_usage_status in ("complete", "not_configured")
            proj_u_has_complete = proj_usage_status == "complete"
            w_status = combine_statuses([norm_statuses.get(s, "not_configured") for s in WORK_SOURCE_IDS])
            w_ok = w_status in ("complete", "not_configured")

            meta_map = project_work_meta_statuses or {}
            pw_components: List[str] = []
            for sid in WORK_SOURCE_IDS:
                ws = norm_statuses.get(sid, "not_configured")
                ms = meta_map.get(sid)
                if ms and ms != "complete" and ws == "complete":
                    pw_components.append(ms)
                else:
                    pw_components.append(ws)
            pw_status = combine_statuses(pw_components)
            pw_ok = pw_status in ("complete", "not_configured")

        def _allows_tool(tool_key: str) -> bool:
            if norm_statuses is None:
                return True
            sid = TOOL_TO_USAGE_SOURCE.get(tool_key)
            if sid is None:
                return True
            return norm_statuses.get(sid) == "complete"

        g_live_usage = {tk: dmap for tk, dmap in live_usage.items() if _allows_tool(tk)}
        g_live_projects = live_projects if proj_u_ok else None
        g_obs_work = observed_work if w_ok else {}
        g_obs_pw = (observed_project_work or {}) if pw_ok else {}
        g_live_billing: Dict[str, Dict[str, Dict[str, float]]] = {}
        if live_billing:
            for tk, dmap in live_billing.items():
                if _allows_tool(tk):
                    g_live_billing[tk] = dmap
        return (
            norm_statuses,
            proj_u_ok,
            proj_u_has_complete,
            w_ok,
            pw_ok,
            w_status,
            pw_status,
            g_live_usage,
            g_live_projects,
            g_obs_work,
            g_obs_pw,
            g_live_billing,
        )

    effective_commit_ts = commit_ts if commit_ts is not None else now_ts
    rejected_stale_commit = False
    collection_payload: Optional[Dict[str, Any]] = None
    normalized_statuses: Optional[Dict[str, str]] = None
    proj_usage_complete = False
    proj_usage_has_complete = False
    work_complete = True
    gated_live_usage: Dict[str, Dict[str, Dict[str, Any]]] = {}
    gated_live_projects: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None
    gated_observed_work: Dict[str, Dict[str, Any]] = {}
    gated_observed_project_work: Dict[str, Dict[str, Dict[str, Any]]] = {}
    gated_live_billing: Dict[str, Dict[str, Dict[str, float]]] = {}

    if persist and ledger_path:
        private_directory(ledger_path.parent)
        with state_lock(ledger_path):
            ledger = load_ledger(ledger_path)
            prev_col = ledger.get("collection") if isinstance(ledger.get("collection"), dict) else {}
            existing_commit_ts = prev_col.get("commit_ts")

            if (
                commit_ts is not None
                and isinstance(existing_commit_ts, (int, float))
                and float(commit_ts) < float(existing_commit_ts)
            ):
                rejected_stale_commit = True
                work_complete = False
                if source_statuses is not None:
                    # Strictly reflect accepted on-disk ledger collection state without mutating disk
                    collection_payload = collection_payload_from_ledger(prev_col)
                    normalized_statuses = {
                        sid: entry["status"]
                        for sid, entry in (collection_payload.get("sources") or {}).items()
                    }
            else:
                (
                    normalized_statuses,
                    proj_usage_complete,
                    proj_usage_has_complete,
                    work_complete,
                    project_work_complete,
                    work_status_norm,
                    project_work_status_norm,
                    gated_live_usage,
                    gated_live_projects,
                    gated_observed_work,
                    gated_observed_project_work,
                    gated_live_billing,
                ) = _compute_gates(prev_col)

                replace_win = (
                    (live_cutoff_day, today_str)
                    if (proj_usage_complete and proj_usage_has_complete and gated_live_projects is not None)
                    else None
                )
                changed = update_ledger_with_live(
                    ledger,
                    gated_live_usage,
                    live_cutoff_day,
                    now_ts,
                    live_projects=gated_live_projects,
                    replace_projects_window=replace_win,
                )

                if work_complete:
                    for day, record in gated_observed_work.items():
                        if ledger["work_days"].get(day) != record:
                            ledger["work_days"][day] = record
                            changed = True
                    # Advance work_history_from ONLY when at least one configured work source completed
                    # cleanly AND trusted work observation actually landed in the ledger.
                    has_trusted_work = bool(gated_observed_work) or bool(ledger.get("work_days"))
                    if (
                        work_history_from
                        and not ledger.get("work_history_from")
                        and work_status_norm == "complete"
                        and has_trusted_work
                    ):
                        ledger["work_history_from"] = work_history_from
                        changed = True

                if project_work_complete and gated_observed_project_work is not None:
                    if gated_observed_project_work:
                        pw_store = ledger.setdefault("project_work_days", {})
                        if not isinstance(pw_store, dict):
                            pw_store = {}
                            ledger["project_work_days"] = pw_store
                        for ppath, dmap in gated_observed_project_work.items():
                            p_dst = pw_store.setdefault(ppath, {})
                            if not isinstance(p_dst, dict):
                                p_dst = {}
                                pw_store[ppath] = p_dst
                            for dk, rec in dmap.items():
                                if _safe_float(rec.get("agent_seconds")) <= 0:
                                    continue
                                if p_dst.get(dk) != rec:
                                    p_dst[dk] = rec
                                    changed = True
                    # Advance project_work_history_from ONLY when project-work sources completed cleanly
                    # AND at least one trusted project-path work record has been observed.
                    has_trusted_project_work = bool(gated_observed_project_work) or bool(ledger.get("project_work_days"))
                    if (
                        project_work_history_from
                        and not ledger.get("project_work_history_from")
                        and project_work_status_norm == "complete"
                        and has_trusted_project_work
                    ):
                        ledger["project_work_history_from"] = project_work_history_from
                        changed = True

                if gated_live_billing:
                    b_store = ledger.setdefault("billing_days", {})
                    if not isinstance(b_store, dict):
                        b_store = {}
                        ledger["billing_days"] = b_store
                    b_floor = billing_cutoff_day or live_cutoff_day
                    for tk, dmap in gated_live_billing.items():
                        t_dst = b_store.setdefault(tk, {})
                        if not isinstance(t_dst, dict):
                            t_dst = {}
                            b_store[tk] = t_dst
                        for dk, b_entry in dmap.items():
                            if dk < b_floor:
                                continue
                            if t_dst.get(dk) != b_entry:
                                t_dst[dk] = b_entry
                                changed = True

                if source_statuses is not None:
                    collection_payload, next_ledger_col = build_collection_payload(
                        source_statuses,
                        project_work_meta_statuses or {},
                        prev_col,
                        now_ts,
                        observed_instances=observed_instances,
                    )
                    next_ledger_col["commit_ts"] = round(float(effective_commit_ts), 6)
                    if ledger.get("collection") != next_ledger_col:
                        ledger["collection"] = next_ledger_col
                        changed = True
                elif commit_ts is not None:
                    next_col = dict(prev_col)
                    next_col["commit_ts"] = round(float(effective_commit_ts), 6)
                    if ledger.get("collection") != next_col:
                        ledger["collection"] = next_col
                        changed = True

                if changed or not ledger_path.is_file():
                    save_ledger(ledger_path, ledger)
    else:
        ledger = load_ledger(ledger_path) if ledger_path else _empty_ledger()
        prev_col = ledger.get("collection") if isinstance(ledger.get("collection"), dict) else {}
        (
            normalized_statuses,
            proj_usage_complete,
            proj_usage_has_complete,
            work_complete,
            _,
            _,
            _,
            gated_live_usage,
            gated_live_projects,
            gated_observed_work,
            gated_observed_project_work,
            gated_live_billing,
        ) = _compute_gates(prev_col)
        if source_statuses is not None:
            collection_payload, _ = build_collection_payload(
                source_statuses,
                project_work_meta_statuses or {},
                prev_col,
                now_ts,
                observed_instances=observed_instances,
            )

    # When commit was rejected as stale, projections MUST read ONLY accepted on-disk ledger data!
    proj_live_usage = {} if rejected_stale_commit else gated_live_usage
    proj_live_projects = None if rejected_stale_commit else gated_live_projects
    proj_observed_work = {} if rejected_stale_commit else gated_observed_work
    proj_observed_pw = {} if rejected_stale_commit else (gated_observed_project_work or {})
    proj_live_billing = {} if rejected_stale_commit else gated_live_billing

    merged, tokei_won = _reconcile_merged_tools_with_winners(ledger, proj_live_usage)

    yesterday_str = (today_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    this_mon = today_dt - timedelta(days=today_dt.weekday())
    week_start_str = this_mon.strftime("%Y-%m-%d")
    last_week_start_str = (this_mon - timedelta(days=7)).strftime("%Y-%m-%d")
    last_week_end_str = (this_mon - timedelta(days=1)).strftime("%Y-%m-%d")
    month_start_str = today_dt.strftime("%Y-%m-01")
    year_start_str = today_dt.strftime("%Y-01-01")

    work_by_day = {day: value for day, value in ledger["work_days"].items() if valid_day(day)}
    work_by_day.update(proj_observed_work)

    # Combine persisted + gated billing_days
    billing_by_tool: Dict[str, Dict[str, Dict[str, Any]]] = {}
    raw_b = ledger.get("billing_days")
    if isinstance(raw_b, dict):
        for tk, dmap in raw_b.items():
            if isinstance(dmap, dict):
                billing_by_tool[tk] = {dk: dict(bv) for dk, bv in dmap.items() if valid_day(dk) and isinstance(bv, dict)}
    for tk, dmap in proj_live_billing.items():
        dst = billing_by_tool.setdefault(tk, {})
        for dk, bv in dmap.items():
            if valid_day(dk):
                dst[dk] = dict(bv)

    # Daily usage map for all known days
    daily_usage_map: Dict[str, Dict[str, Any]] = {}
    for tool, day_map in merged.items():
        for day_key, b in day_map.items():
            b_in = _safe_int(b.get("in"))
            b_out = _safe_int(b.get("out"))
            b_cr = _safe_int(b.get("cr"))
            b_cw = _safe_int(b.get("cw"))
            b_th = _safe_int(b.get("thoughts"))
            b_cost = _safe_float(b.get("cost_usd", b.get("cost")))
            b_sess = _safe_int(b.get("sessions")) or 1
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
                    "unpriced_tokens": 0,
                    "sessions": 0,
                    "tools": {},
                },
            )
            d_acc["in"] += b_in
            d_acc["out"] += b_out
            d_acc["cr"] += b_cr
            d_acc["cw"] += b_cw
            d_acc["thoughts"] += b_th
            d_acc["total_tokens"] += b_tok
            d_acc["cost_usd"] = round(d_acc["cost_usd"] + b_cost, 6)
            unp_b = _unpriced_tokens(b, b_tok, b_cost)
            d_acc["unpriced_tokens"] += unp_b
            d_acc["sessions"] += b_sess
            t_cur = d_acc["tools"].setdefault(tool, {"tokens": 0, "cost_usd": 0.0, "unpriced_tokens": 0})
            t_cur["tokens"] += b_tok
            t_cur["cost_usd"] = round(t_cur["cost_usd"] + b_cost, 6)
            t_cur["unpriced_tokens"] += unp_b

    for d_val in daily_usage_map.values():
        denom = d_val["in"] + d_val["cr"] + d_val["cw"]
        d_val["cache_hit_rate"] = round(d_val["cr"] / denom, 4) if denom > 0 else 0.0
        d_val["cost_usd"] = round(d_val["cost_usd"], 6)

    effective_work_complete = work_complete and (not rejected_stale_commit)

    def _day_work_status(day_key: str, wd_entry: Dict[str, Any]) -> str:
        has_wd = bool(wd_entry)
        if effective_work_complete:
            if has_wd and day_key not in proj_observed_work and (
                float(wd_entry.get("agent_seconds", 0.0) or 0.0) > 0 or int(wd_entry.get("runs", 0) or 0) > 0
            ):
                return "retained"
            return "observed"
        return "retained" if has_wd else "unknown"

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
                "cost_usd": round(float(du.get("cost_usd", 0.0)), 6),
                "unpriced_tokens": int(du.get("unpriced_tokens", 0)),
                "agent_hours": round(float(wd.get("agent_hours", 0.0)), 2),
                "work_status": _day_work_status(dk, wd),
            }
        )

    all_active_dates = set(daily_usage_map.keys()) | {
        dk for dk, wd in work_by_day.items() if float(wd.get("agent_seconds", 0.0) or 0.0) >= 60.0
    }
    current_streak, longest_streak = _compute_streaks(all_active_dates, today_dt)
    heatmap_days: List[Dict[str, Any]] = []
    for i in range(181, -1, -1):
        dt = today_dt - timedelta(days=i)
        dk = dt.strftime("%Y-%m-%d")
        du = daily_usage_map.get(dk) or {}
        wd = work_by_day.get(dk) or {}
        day_tools_map: Dict[str, Dict[str, Any]] = {}
        for tk, tv in (du.get("tools") or {}).items():
            meta = USAGE_TOOL_META.get(tk, {"name": tk, "short": tk, "color": "#94A3B8"})
            t_tok = int(tv.get("tokens", 0))
            t_cost = round(float(tv.get("cost_usd", 0.0)), 6)
            t_unp = int(tv.get("unpriced_tokens", 0))
            day_tools_map[tk] = {
                "tool": tk,
                "display_name": meta["name"],
                "color": meta["color"],
                "total_tokens": t_tok,
                "cost_usd": t_cost,
                "unpriced_tokens": t_unp,
                "has_cost_observation": bool(t_tok > 0 or t_cost > 0 or t_unp > 0),
                "agent_hours": 0.0,
            }
        for wt in wd.get("tools") or []:
            tk = wt["tool"]
            meta = USAGE_TOOL_META.get(tk, {"name": tk, "short": tk, "color": "#94A3B8"})
            ent = day_tools_map.setdefault(
                tk,
                {
                    "tool": tk,
                    "display_name": meta["name"],
                    "color": meta["color"],
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                    "unpriced_tokens": 0,
                    "has_cost_observation": False,
                    "agent_hours": 0.0,
                },
            )
            ent["agent_hours"] = round(float(wt.get("agent_hours", 0.0)), 2)

        day_tools_sorted = sorted(
            day_tools_map.values(),
            key=lambda x: (x["cost_usd"], x["agent_hours"], x["total_tokens"]),
            reverse=True,
        )[:6]
        d_tok = int(du.get("total_tokens", 0))
        d_cost = round(float(du.get("cost_usd", 0.0)), 6)
        d_unp = int(du.get("unpriced_tokens", 0))
        heatmap_days.append(
            {
                "date": dk,
                "short_date": dt.strftime("%m/%d"),
                "weekday": WEEKDAY_CN[dt.weekday()],
                "is_today": dk == today_str,
                "total_tokens": d_tok,
                "cost_usd": d_cost,
                "unpriced_tokens": d_unp,
                "has_cost_observation": bool(d_tok > 0 or d_cost > 0 or d_unp > 0),
                "agent_hours": round(float(wd.get("agent_hours", 0.0)), 2),
                "work_status": _day_work_status(dk, wd),
                "sessions": int(du.get("sessions", 0)),
                "tools": day_tools_sorted,
            }
        )

    earliest_candidates = [
        *work_by_day,
        *(min(days) for days in merged.values() if days),
        *(min(days) for days in billing_by_tool.values() if days),
        today_str,
    ]
    earliest_day = min(earliest_candidates)

    periods = {
        "today": _build_period_summary(
            merged, today_str, today_str, work_by_day, pricing=pricing, billing_by_tool=billing_by_tool, tokei_won=tokei_won, work_complete=effective_work_complete
        ),
        "yesterday": _build_period_summary(
            merged, yesterday_str, yesterday_str, work_by_day, pricing=pricing, billing_by_tool=billing_by_tool, tokei_won=tokei_won, work_complete=effective_work_complete
        ),
        "week": _build_period_summary(
            merged, week_start_str, today_str, work_by_day, pricing=pricing, billing_by_tool=billing_by_tool, tokei_won=tokei_won, work_complete=effective_work_complete
        ),
        "last_week": _build_period_summary(
            merged, last_week_start_str, last_week_end_str, work_by_day, pricing=pricing, billing_by_tool=billing_by_tool, tokei_won=tokei_won, work_complete=effective_work_complete
        ),
        "month": _build_period_summary(
            merged, month_start_str, today_str, work_by_day, pricing=pricing, billing_by_tool=billing_by_tool, tokei_won=tokei_won, work_complete=effective_work_complete
        ),
        "year": _build_period_summary(
            merged, year_start_str, today_str, work_by_day, pricing=pricing, billing_by_tool=billing_by_tool, tokei_won=tokei_won, work_complete=effective_work_complete
        ),
        "all": _build_period_summary(
            merged, earliest_day, today_str, work_by_day, pricing=pricing, billing_by_tool=billing_by_tool, tokei_won=tokei_won, work_complete=effective_work_complete
        ),
    }

    imported_tools = ledger.get("imported_tools") or {}
    imported_tool_count = sum(1 for v in imported_tools.values() if isinstance(v, dict) and v)
    max_history_days = max(
        (len(v) for v in merged.values() if isinstance(v, dict)),
        default=0,
    )

    eff_home = home or Path.home()
    proj_replace_win = (
        (live_cutoff_day, today_str)
        if (proj_usage_complete and proj_usage_has_complete and proj_live_projects is not None and not rejected_stale_commit)
        else None
    )
    projects_list = _build_projects_list(
        ledger, proj_live_projects, today_dt, eff_home, replace_projects_window=proj_replace_win
    )

    # Combined usage & work projects for worktree-merged project_trails
    usage_projects_combined = _combine_usage_projects(
        ledger, proj_live_projects, replace_projects_window=proj_replace_win
    )

    work_projects_combined: Dict[str, Dict[str, Dict[str, Any]]] = {}
    raw_pw = ledger.get("project_work_days")
    if isinstance(raw_pw, dict):
        for proj_path, day_map in raw_pw.items():
            if isinstance(day_map, dict):
                work_projects_combined[proj_path] = {
                    dk: bv for dk, bv in day_map.items() if valid_day(dk) and isinstance(bv, dict)
                }
    for proj_path, day_map in proj_observed_pw.items():
        dst = work_projects_combined.setdefault(proj_path, {})
        for dk, bv in day_map.items():
            if valid_day(dk):
                dst[dk] = bv

    project_trails = _build_project_trails(
        usage_projects_combined,
        work_projects_combined,
        today_dt,
        eff_home,
        resolver=worktree_resolver,
    )

    bad_days = sum(
        not valid_day(day)
        for tool_groups in (
            ledger["imported_tools"],
            ledger["live_tools"],
            ledger["projects"],
            {"work": ledger["work_days"]},
        )
        for days in tool_groups.values()
        for day in days
    )

    if out_state is not None:
        out_state["collection"] = collection_payload
        out_state["rejected_stale_commit"] = rejected_stale_commit
        out_state["work_complete"] = effective_work_complete
        out_state["ledger_work_days"] = work_by_day
        if normalized_statuses is not None:
            out_state["source_statuses"] = normalized_statuses

    usage_payload = {
        "source": "agent_pulse_native",
        "periods": periods,
        "history_30d": history_30d,
        "heatmap": {
            "days": heatmap_days,
            "current_streak": current_streak,
            "longest_streak": longest_streak,
            "total_active_days": len(all_active_dates),
        },
        "projects": projects_list,
        "project_trails": project_trails,
        "ledger_meta": {
            "ledger_path": str(ledger_path) if ledger_path else "",
            "imported_tokei": imported_tool_count > 0,
            "imported_tool_count": imported_tool_count,
            "max_tool_days": max_history_days,
            "earliest_day": earliest_day,
            "work_history_from": ledger.get("work_history_from"),
            "project_work_history_from": ledger.get("project_work_history_from"),
            "skipped_invalid_days": bad_days,
            "tokei_import_meta": ledger.get("tokei_import_meta"),
        },
    }
    return usage_payload, daily_usage_map
