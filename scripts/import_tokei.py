#!/usr/bin/env python3
"""AgentPulse — Safe, idempotent migration tool from Tokei (~/.tokei).

Always takes a read-only full backup of ~/.tokei/ (and existing ~/.agent-pulse/
state) before writing anything to ~/.agent-pulse/. Never modifies ~/.tokei/.

Migrates:
1. ~/.tokei/ledger.json -> ~/.agent-pulse/ledger.json (`imported_tools`, preserving
   all daily token/cost/model breakdowns with zero double counting).
2. Quota baselines (codex_quota_cache, claude_quota_swift_cache, provider_quota_cache,
   kimi_quota_cache) -> ~/.agent-pulse/quota_cache.json (only seeding missing/older keys).
3. Historical quota files (quota_history.json, quota_cycles.json) -> ~/.agent-pulse/history/.
4. User pricing overrides -> ~/.agent-pulse/pricing_overrides.json (if absent).
5. macOS Tokei preferences (sitReminderOn, sitReminderInterval, menuBarQuota*) ->
   ~/.agent-pulse/migrated_prefs.json.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT = SCRIPT_DIR.parent
for _p in (str(SCRIPT_DIR), str(ROOT / "engine")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ledger import LEDGER_VERSION, canonical_tool_key, load_ledger, resolve_data_dir  # noqa: E402

BIZ_TZ = timezone(timedelta(hours=8))


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


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _save_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _normalize_tokei_day(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    inp = _safe_int(raw.get("in"))
    out = _safe_int(raw.get("out"))
    cr = _safe_int(raw.get("cr", raw.get("cached")))
    cw = _safe_int(raw.get("cw"))
    thoughts = _safe_int(raw.get("thoughts", raw.get("reason")))
    cost = _safe_float(raw.get("cost"))

    # Some Tokei tools (e.g. gemini) record inclusive `in` where `cached` is a subset
    if raw.get("cached") is not None and raw.get("cr") is None and inp >= cr:
        inp = max(0, inp - cr)

    models_out: Dict[str, Dict[str, Any]] = {}
    raw_models = raw.get("models")
    if isinstance(raw_models, dict):
        for m_name, mv in raw_models.items():
            if not isinstance(mv, dict):
                continue
            m_in = _safe_int(mv.get("in"))
            m_out = _safe_int(mv.get("out"))
            m_cr = _safe_int(mv.get("cr", mv.get("cached")))
            m_cw = _safe_int(mv.get("cw"))
            m_th = _safe_int(mv.get("thoughts", mv.get("reason")))
            m_cost = _safe_float(mv.get("cost"))
            if mv.get("cached") is not None and mv.get("cr") is None and m_in >= m_cr:
                m_in = max(0, m_in - m_cr)
            if m_in + m_out + m_cr + m_cw + m_th <= 0 and m_cost <= 0:
                continue
            models_out[str(m_name)] = {
                "in": m_in,
                "out": m_out,
                "cr": m_cr,
                "cw": m_cw,
                "thoughts": m_th,
                "cost_usd": round(m_cost, 6),
                "requests": _safe_int(mv.get("requests") or 1),
            }

    # If top-level tokens were zero (some schema versions keep counts inside models), sum models
    if inp + out + cr + cw + thoughts == 0 and models_out:
        for mv in models_out.values():
            inp += mv["in"]
            out += mv["out"]
            cr += mv["cr"]
            cw += mv["cw"]
            thoughts += mv["thoughts"]
            if cost <= 0:
                cost += mv["cost_usd"]

    if inp + out + cr + cw + thoughts <= 0 and cost <= 0:
        return None

    return {
        "in": inp,
        "out": out,
        "cr": cr,
        "cw": cw,
        "thoughts": thoughts,
        "cost_usd": round(cost, 6),
        "requests": _safe_int(raw.get("requests") or raw.get("sessions") or 1),
        "models": models_out,
        "source": "tokei_import",
    }


def migrate_from_tokei(home: Optional[Path] = None, skip_backup: bool = False) -> Dict[str, Any]:
    home_dir = home or Path.home()
    tokei_dir = home_dir / ".tokei"
    ac_dir = resolve_data_dir(home_dir, create=True)

    now_dt = datetime.now(tz=BIZ_TZ)
    ts_tag = now_dt.strftime("%Y%m%d-%H%M%S")

    if not tokei_dir.is_dir():
        return {
            "ok": False,
            "error": f"未找到 Tokei 数据目录：{tokei_dir}",
            "imported_tools": 0,
            "imported_days": 0,
        }

    try:
        tokei_ledger = json.loads((tokei_dir / "ledger.json").read_text(encoding="utf-8"))
        raw_tools = tokei_ledger.get("tools") if isinstance(tokei_ledger, dict) else None
        if not isinstance(raw_tools, dict) or not raw_tools:
            raise ValueError("缺少有效的工具账本")
        if any(not isinstance(days, dict) or any(not isinstance(day, dict) for day in days.values())
               for days in raw_tools.values()):
            raise ValueError("工具日记录格式错误")
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": f"无法读取 Tokei 账本：{exc}"}

    # 1. Backup ~/.tokei and existing ~/.agent-pulse/ledger.json
    backup_dir: Optional[Path] = None
    if not skip_backup:
        backup_dir = ac_dir / "backups" / f"tokei-import-{ts_tag}"
        backup_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(tokei_dir, backup_dir / "tokei", dirs_exist_ok=True)
        existing_ledger = ac_dir / "ledger.json"
        if existing_ledger.is_file():
            shutil.copy2(existing_ledger, backup_dir / "agent-pulse-ledger-before.json")

    # 2. Import ledger.json (idempotent replacement of `imported_tools`, untouched `live_tools`)

    ac_ledger_path = ac_dir / "ledger.json"
    ac_ledger = load_ledger(ac_ledger_path)
    imported_tools: Dict[str, Dict[str, Dict[str, Any]]] = {}
    total_days = 0
    tool_summary: Dict[str, int] = {}

    for raw_tool, raw_days in sorted(raw_tools.items()):
        if not isinstance(raw_days, dict):
            continue
        canon = canonical_tool_key(raw_tool)
        dst_days = imported_tools.setdefault(canon, {})
        for day_key, day_val in sorted(raw_days.items()):
            norm = _normalize_tokei_day(day_val)
            if norm is None:
                continue
            dst_days[str(day_key)] = norm
            total_days += 1
        if dst_days:
            tool_summary[canon] = len(dst_days)

    if not total_days:
        return {"ok": False, "error": "Tokei 账本没有可导入的日记录"}

    ac_ledger["version"] = LEDGER_VERSION
    ac_ledger["imported_tools"] = imported_tools
    ac_ledger["tokei_import_meta"] = {
        "imported_at": now_dt.isoformat(),
        "backup_dir": str(backup_dir) if backup_dir else None,
        "tools_count": len(tool_summary),
        "days_count": total_days,
        "tools_days": tool_summary,
    }
    _save_json_atomic(ac_ledger_path, ac_ledger)

    # 3. Seed quota_cache.json from Tokei caches (only if newer than existing AgentPulse cache)
    quota_cache_path = ac_dir / "quota_cache.json"
    q_cache = _load_json(quota_cache_path)

    t_codex = _load_json(tokei_dir / "codex_quota_cache.json")
    if t_codex.get("limits") and float(t_codex.get("fetched_at") or 0) > float((q_cache.get("codex") or {}).get("fetched_at") or 0):
        q_cache["codex"] = {
            "fetched_at": t_codex.get("fetched_at"),
            "limits": t_codex.get("limits"),
            "plan": (t_codex.get("limits") or {}).get("plan_type") or t_codex.get("plan"),
            "source": "tokei_import",
        }

    t_claude_swift = _load_json(tokei_dir / "claude_quota_swift_cache.json")
    t_claude_snap = t_claude_swift.get("snapshot") if isinstance(t_claude_swift.get("snapshot"), dict) else {}
    if t_claude_snap and int(t_claude_snap.get("updated") or 0) > int((q_cache.get("claude") or {}).get("updated") or 0):
        q_cache["claude"] = {
            "q5": t_claude_snap.get("q5"),
            "q5_reset": t_claude_snap.get("q5Reset"),
            "q7": t_claude_snap.get("q7"),
            "q7_reset": t_claude_snap.get("q7Reset"),
            "updated": t_claude_snap.get("updated"),
        }

    t_prov = _load_json(tokei_dir / "provider_quota_cache.json")
    t_ag = ((t_prov.get("providers") or {}).get("antigravity")) if isinstance(t_prov.get("providers"), dict) else None
    if isinstance(t_ag, dict) and isinstance(t_ag.get("quota"), dict):
        if int(t_ag.get("fetched_at") or 0) > int((q_cache.get("antigravity") or {}).get("fetched_at") or 0):
            # Strip personal email before caching
            clean_quota = {k: v for k, v in t_ag["quota"].items() if k != "account"}
            q_cache["antigravity"] = {
                "fetched_at": t_ag.get("fetched_at"),
                "quota": clean_quota,
            }

    t_kimi = _load_json(tokei_dir / "kimi_quota_cache.json")
    if isinstance(t_kimi.get("limits"), dict):
        if float(t_kimi.get("fetched_at") or 0) > float((q_cache.get("kimi") or {}).get("fetched_at") or 0):
            q_cache["kimi"] = {
                "fetched_at": t_kimi.get("fetched_at"),
                "limits": t_kimi.get("limits"),
                "plan": t_kimi.get("plan"),
            }

    _save_json_atomic(quota_cache_path, q_cache)

    # 4. Copy historical quota files & pricing overrides
    hist_dir = ac_dir / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    for fname in ("quota_history.json", "quota_cycles.json"):
        src = tokei_dir / fname
        if src.is_file():
            shutil.copy2(src, hist_dir / fname)

    dst_overrides = ac_dir / "pricing_overrides.json"
    src_overrides = tokei_dir / "pricing_overrides.json"
    if src_overrides.is_file() and not dst_overrides.is_file():
        shutil.copy2(src_overrides, dst_overrides)

    # 5. Extract non-sensitive Tokei user preferences (sit reminder & menu bar quota flags)
    plist_path = home_dir / "Library" / "Preferences" / "com.tokei.app.plist"
    migrated_prefs: Dict[str, Any] = {
        "sit_reminder_enabled": True,
        "sit_reminder_interval_minutes": 60,
        "menu_bar_show_cost": True,
        "menu_bar_show_quotas": True,
    }
    if plist_path.is_file():
        try:
            pref = plistlib.loads(plist_path.read_bytes())
            if isinstance(pref, dict):
                if isinstance(pref.get("sitReminderOn"), bool):
                    migrated_prefs["sit_reminder_enabled"] = pref["sitReminderOn"]
                if isinstance(pref.get("sitReminderInterval"), (int, float)) and pref["sitReminderInterval"] >= 15:
                    migrated_prefs["sit_reminder_interval_minutes"] = int(pref["sitReminderInterval"])
                migrated_prefs["menu_bar_quota_claude_week"] = bool(pref.get("menuBarQuotaClaudeWeek", True))
                migrated_prefs["menu_bar_quota_grok"] = bool(pref.get("menuBarQuotaGrok", False))
        except Exception:
            pass
    _save_json_atomic(ac_dir / "migrated_prefs.json", migrated_prefs)

    return {
        "ok": True,
        "backup_dir": str(backup_dir) if backup_dir else None,
        "imported_tools": len(tool_summary),
        "imported_days": total_days,
        "tools_days": tool_summary,
        "migrated_prefs": migrated_prefs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate historical usage and preferences from Tokei to AgentPulse")
    parser.add_argument("--no-backup", action="store_true", help="Skip creating backup copy (for isolated tests)")
    parser.add_argument("--json", action="store_true", help="Output JSON summary")
    args = parser.parse_args()

    try:
        res = migrate_from_tokei(skip_backup=args.no_backup)
    except (OSError, ValueError, OverflowError) as exc:
        res = {"ok": False, "error": str(exc)}
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        if res.get("ok"):
            print(f"迁移成功：共导入 {res['imported_tools']} 个工具、{res['imported_days']} 条历史日记录。")
            if res.get("backup_dir"):
                print(f"备份目录：{res['backup_dir']}")
            for k, v in sorted((res.get("tools_days") or {}).items()):
                print(f"  - {k}: {v} 天")
        else:
            print(f"迁移失败：{res.get('error')}", file=sys.stderr)
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
