#!/usr/bin/env python3
"""AgentPulse — Independent slow-track subscription quota collector (v0.7).

Runs on its own 300s cadence (or manual trigger), completely decoupled from the
15s local work-hours/token fast track. Never raises uncaught exceptions; any
unreachable provider degrades gracefully to its last cached reading.

v0.7 additions:
- `fresh_until_ts` on each window (Codex 360s, Claude 3600s, capped at resets_at).
- `pace` estimation (`on_track | watch | risk | unknown`) persisted under
  `quota_cache.json._pace_v1` with 5-minute bucket dedup and strict scope checks.
- `highest_window` selected strictly from currently fresh windows with deterministic
  `(provider_id, window_id)` tie-breaking.

Zero external dependencies (Python 3.9+ stdlib only).
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import ctypes.util
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

_ENGINE_DIR = str(Path(__file__).resolve().parent)
if _ENGINE_DIR not in sys.path:
    sys.path.insert(0, _ENGINE_DIR)

from ledger import resolve_data_dir  # noqa: E402
from storage import state_lock, write_json  # noqa: E402

BIZ_TZ = timezone(timedelta(hours=8))

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
CODEX_QUOTA_TTL = 240.0
CODEX_FAILURE_BACKOFF = 300.0
CODEX_DISPLAY_TTL = 360.0
CLAUDE_DISPLAY_TTL = 3600.0
ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"

PACE_WINDOW_SECONDS = 6 * 3600
PACE_BUCKET_SECONDS = 300
PACE_MIN_SAMPLES = 3
PACE_MIN_SPAN_SECONDS = 1800
PACE_MAX_SAMPLES = 96


def _load_json(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _iso_to_epoch(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    if isinstance(val, (int, float)):
        v = float(val)
        if not math.isfinite(v) or v <= 0:
            return None
        return int(v / 1000.0) if v > 1e11 else int(v)
    if isinstance(val, str):
        s = val.strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            return int(datetime.fromisoformat(s).timestamp())
        except ValueError:
            return None
    return None


def _decode_jwt_claims(token: str) -> Dict[str, Any]:
    if not isinstance(token, str) or token.count(".") < 2:
        return {}
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload))
        return decoded if isinstance(decoded, dict) else {}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# 1. Codex Quota (Official live API -> local JSONL rate_limits -> cache)
# ---------------------------------------------------------------------------


def _codex_is_custom_provider(home: Path) -> bool:
    cfg_path = home / ".codex" / "config.toml"
    if not cfg_path.is_file():
        return False
    try:
        text = cfg_path.read_text(encoding="utf-8", errors="ignore")[:65536]
    except OSError:
        return False
    root = re.split(r"^\s*\[", text, maxsplit=1, flags=re.M)[0]
    hit = re.search(r'^\s*model_provider\s*=\s*["\']([^"\']+)["\']', root, re.M)
    provider = hit.group(1).strip() if hit else None
    return bool(provider) and provider != "openai"


def _codex_auth_context(home: Path) -> Dict[str, Any]:
    auth = _load_json(home / ".codex" / "auth.json")
    if not auth:
        return {}
    nested = auth.get("tokens") if isinstance(auth.get("tokens"), dict) else {}

    def pick(*names: str) -> Optional[str]:
        for src in (nested, auth):
            for n in names:
                v = src.get(n)
                if isinstance(v, str) and v:
                    return v
        return None

    access_token = pick("access_token", "accessToken")
    if not access_token:
        return {}
    id_token = pick("id_token", "idToken") or ""
    claims = _decode_jwt_claims(access_token)
    id_claims = _decode_jwt_claims(id_token)
    auth_claim = claims.get("https://api.openai.com/auth") if isinstance(claims.get("https://api.openai.com/auth"), dict) else {}
    id_auth_claim = id_claims.get("https://api.openai.com/auth") if isinstance(id_claims.get("https://api.openai.com/auth"), dict) else {}
    account_id = pick("account_id", "accountId") or auth_claim.get("chatgpt_account_id") or id_auth_claim.get("chatgpt_account_id")
    identity = account_id or claims.get("sub") or id_claims.get("sub") or access_token
    return {
        "access_token": access_token,
        "account_id": str(account_id) if account_id else None,
        "account_key": hashlib.sha256(str(identity).encode("utf-8")).hexdigest(),
    }


def _codex_window_from_live(win: Any, now: float) -> Optional[Dict[str, Any]]:
    if not isinstance(win, dict):
        return None
    used = float(win.get("used_percent", -1))
    if not math.isfinite(used) or not 0 <= used <= 100:
        return None
    reset_at = win.get("reset_at")
    if reset_at is None and win.get("reset_after_seconds") is not None:
        reset_at = int(now + float(win["reset_after_seconds"]))
    window_min = None
    if win.get("limit_window_seconds") is not None:
        window_min = int(round(float(win["limit_window_seconds"]) / 60.0))
    return {
        "used_percent": float(used),
        "window_minutes": window_min,
        "resets_at": int(reset_at) if reset_at is not None else None,
    }


def _scan_codex_local_rate_limits(home: Path) -> Optional[Tuple[Dict[str, Any], float]]:
    """Fallback: read latest rate_limits embedded inside ~/.codex/sessions/*.jsonl."""
    root = home / ".codex" / "sessions"
    if not root.is_dir():
        return None
    candidates = sorted(root.rglob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]
    for path in candidates:
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                obj = json.loads(line)
                p = obj.get("payload") or {}
                rl = p.get("rate_limits")
                if isinstance(rl, dict) and (rl.get("primary") or rl.get("secondary")):
                    ts = _iso_to_epoch(obj.get("timestamp")) or int(path.stat().st_mtime)
                    return rl, float(ts)
            except Exception:
                continue
    return None


def _format_codex_provider(limits: Dict[str, Any], plan: Optional[str], fetched_at: float, status: str, status_text: str) -> Dict[str, Any]:
    windows: List[Dict[str, Any]] = []
    primary = limits.get("primary") if isinstance(limits.get("primary"), dict) else None
    secondary = limits.get("secondary") if isinstance(limits.get("secondary"), dict) else None
    if primary and primary.get("used_percent") is not None:
        used = float(primary["used_percent"])
        windows.append(
            {
                "id": "codex_5h",
                "label": "5 小时窗口",
                "short_label": "C5",
                "used_percent": round(used, 1),
                "_raw_used_percent": used,
                "remaining_percent": round(max(0.0, 100.0 - used), 1),
                "resets_at": primary.get("resets_at"),
                "window_minutes": primary.get("window_minutes") or 300,
            }
        )
    if secondary and secondary.get("used_percent") is not None:
        used = float(secondary["used_percent"])
        windows.append(
            {
                "id": "codex_7d",
                "label": "7 天窗口",
                "short_label": "C7",
                "used_percent": round(used, 1),
                "_raw_used_percent": used,
                "remaining_percent": round(max(0.0, 100.0 - used), 1),
                "resets_at": secondary.get("resets_at"),
                "window_minutes": secondary.get("window_minutes") or 10080,
            }
        )
    return {
        "id": "codex",
        "name": "Codex",
        "color": "#10B981",
        "plan": (plan or limits.get("plan_type") or "Plus / Pro").title(),
        "status": status,
        "status_text": status_text,
        "fetched_at": int(fetched_at),
        "windows": windows,
    }


def _limits_has_unreset_window(limits: Any, now: float) -> bool:
    if not isinstance(limits, dict):
        return False
    for key in ("primary", "secondary"):
        win = limits.get(key)
        if isinstance(win, dict) and win.get("used_percent") is not None:
            reset_at = win.get("resets_at")
            if reset_at is None or (isinstance(reset_at, (int, float)) and float(reset_at) > now):
                return True
    return False


def probe_codex_quota(
    home: Path,
    cache: Dict[str, Any],
    now: float,
    force: bool = False,
    allow_live: bool = False,
    account_scope_out: Optional[Dict[str, Optional[str]]] = None,
) -> Dict[str, Any]:
    if allow_live and _codex_is_custom_provider(home):
        return {
            "id": "codex",
            "name": "Codex",
            "color": "#10B981",
            "plan": "自定义 API",
            "status": "unavailable",
            "status_text": "已切换第三方 Provider，无官方订阅额度",
            "fetched_at": int(now),
            "windows": [],
        }

    auth_ctx = _codex_auth_context(home) if allow_live else {}
    account_key = auth_ctx.get("account_key")
    saved = cache.get("codex")
    cached = saved if account_key and isinstance(saved, dict) and saved.get("account_key") == account_key else {}

    cached_fetched_at = float(cached.get("fetched_at") or 0)
    cached_within_ttl = (
        bool(cached.get("limits"))
        and cached_fetched_at > 0
        and 0.0 <= (now - cached_fetched_at) < CODEX_QUOTA_TTL
        and _limits_has_unreset_window(cached.get("limits"), now)
    )

    # Return fresh cache if within TTL, not in the future, unreset, and not forced
    if not force and cached_within_ttl:
        if account_scope_out is not None and account_key:
            account_scope_out["codex"] = account_key
        return _format_codex_provider(
            cached["limits"], cached.get("plan"), cached_fetched_at, "ok", "已实时同步"
        )

    last_fail = float(cached.get("last_failure_at") or 0)
    should_call_live = bool(account_key) and (not last_fail or now - last_fail >= CODEX_FAILURE_BACKOFF)

    if should_call_live:
        try:
            req = urllib.request.Request(CODEX_USAGE_URL)
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "AgentPulse/0.7")
            req.add_unredirected_header("Authorization", f"Bearer {auth_ctx['access_token']}")
            if auth_ctx.get("account_id"):
                req.add_unredirected_header("ChatGPT-Account-Id", auth_ctx["account_id"])
            with urllib.request.build_opener(_NoRedirect()).open(req, timeout=3.0) as resp:
                final = urlparse(resp.geturl())
                if final.scheme != "https" or final.hostname != "chatgpt.com":
                    raise ValueError("unexpected redirect")
                data = json.loads(resp.read(131072))
            rl = (data or {}).get("rate_limit") or {}
            primary = _codex_window_from_live(rl.get("primary_window"), now)
            secondary = _codex_window_from_live(rl.get("secondary_window"), now)
            if primary or secondary:
                limits = {
                    "primary": primary,
                    "secondary": secondary,
                    "plan_type": data.get("plan_type"),
                }
                cache["codex"] = {
                    "account_key": account_key,
                    "fetched_at": now,
                    "limits": limits,
                    "plan": data.get("plan_type"),
                    "source": "live",
                }
                if account_scope_out is not None and account_key:
                    account_scope_out["codex"] = account_key
                live_ok = _limits_has_unreset_window(limits, now)
                return _format_codex_provider(
                    limits,
                    data.get("plan_type"),
                    now,
                    "ok" if live_ok else "stale",
                    "实时探测" if live_ok else "窗口已重置，待新周期同步",
                )
            raise ValueError("missing Codex quota windows")
        except Exception:
            cached["last_failure_at"] = now
            cached["account_key"] = account_key
            cache["codex"] = cached

    # Local logs carry historical quota observations whose account may be unknown.
    local_rl = _scan_codex_local_rate_limits(home)
    if local_rl:
        rl_obj, rl_ts = local_rl
        if rl_ts <= now and rl_ts >= cached_fetched_at:
            return _format_codex_provider(rl_obj, rl_obj.get("plan_type"), rl_ts, "stale", "本地会话日志 · 账号未经核对")

    # Fallback 2: Cached snapshot (retains last readings and original fetched_at even after reset)
    if cached.get("limits"):
        orig_ts = float(cached.get("fetched_at") or 0)
        unreset = _limits_has_unreset_window(cached.get("limits"), now)
        msg = "上次缓存快照" if unreset else "窗口已重置，保留上次读数"
        return _format_codex_provider(
            cached["limits"], cached.get("plan"), orig_ts if orig_ts > 0 else now, "stale", msg
        )

    return {
        "id": "codex",
        "name": "Codex",
        "color": "#10B981",
        "plan": "未同步",
        "status": "unavailable",
        "status_text": "未取得当前账号额度" if allow_live else "仅本地模式，暂无会话额度记录",
        "fetched_at": int(now),
        "windows": [],
    }


# ---------------------------------------------------------------------------
# 2. Claude Quota (Claude Desktop Cache zstd -> cached snapshot)
# ---------------------------------------------------------------------------


def _zstd_decompress(data: bytes) -> Optional[bytes]:
    try:
        import zstandard  # type: ignore
        return zstandard.ZstdDecompressor().decompress(data, max_output_size=len(data) * 20)
    except Exception:
        pass
    zstd_bin = shutil.which("zstd") or (
        "/opt/homebrew/bin/zstd" if os.path.isfile("/opt/homebrew/bin/zstd") else None
    )
    if zstd_bin:
        try:
            proc = subprocess.run(
                [zstd_bin, "-d", "-c", "--no-check"],
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            if proc.returncode == 0 and proc.stdout:
                return proc.stdout
        except Exception:
            pass
    for lib_name in (ctypes.util.find_library("zstd"), "/opt/homebrew/lib/libzstd.dylib", "/usr/local/lib/libzstd.dylib"):
        if not lib_name:
            continue
        try:
            lib = ctypes.CDLL(lib_name)
            lib.ZSTD_decompress.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p, ctypes.c_size_t]
            lib.ZSTD_decompress.restype = ctypes.c_size_t
            lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
            lib.ZSTD_isError.restype = ctypes.c_uint
            out_cap = min(2 * 1024 * 1024, max(65536, len(data) * 16))
            buf = ctypes.create_string_buffer(out_cap)
            res = lib.ZSTD_decompress(buf, out_cap, data, len(data))
            if not lib.ZSTD_isError(res):
                return buf.raw[:res]
        except Exception:
            continue
    return None


def _scan_claude_desktop_cache(home: Path, now: float) -> Optional[Dict[str, Any]]:
    cache_dir = home / "Library" / "Application Support" / "Claude" / "Cache" / "Cache_Data"
    if not cache_dir.is_dir():
        return None
    horizon_ns = int(now * 1_000_000_000)
    files: List[Tuple[int, Path]] = []
    try:
        for entry in os.scandir(cache_dir):
            if not entry.is_file():
                continue
            st = entry.stat()
            if st.st_size <= 0 or st.st_size > 4 * 1024 * 1024 or st.st_mtime_ns > horizon_ns:
                continue
            files.append((st.st_mtime_ns, Path(entry.path)))
    except OSError:
        return None

    files.sort(key=lambda x: x[0], reverse=True)
    for mtime_ns, path in files[:160]:
        try:
            raw_bytes = path.read_bytes()
        except OSError:
            continue
        if b"organizations/" not in raw_bytes or b"/usage" not in raw_bytes:
            continue
        pos = raw_bytes.find(ZSTD_MAGIC)
        if pos < 0:
            continue
        decompressed = _zstd_decompress(raw_bytes[pos:])
        if not decompressed:
            continue
        try:
            payload = json.loads(decompressed)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        fh = payload.get("five_hour") if isinstance(payload.get("five_hour"), dict) else {}
        sd = payload.get("seven_day") if isinstance(payload.get("seven_day"), dict) else {}
        if fh.get("utilization") is None and sd.get("utilization") is None:
            continue
        return {
            "q5": fh.get("utilization"),
            "q5_reset": _iso_to_epoch(fh.get("resets_at")),
            "q7": sd.get("utilization"),
            "q7_reset": _iso_to_epoch(sd.get("resets_at")),
            "updated": int(mtime_ns // 1_000_000_000),
        }
    return None


def capture_claude_statusline(home: Path, payload: Any, now: float) -> None:
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return
    snapshot: Dict[str, Any] = {"updated": int(now), "source": "cli"}
    for source, key in (("five_hour", "q5"), ("seven_day", "q7")):
        window = limits.get(source)
        if not isinstance(window, dict):
            continue
        used = window.get("used_percentage")
        reset = _iso_to_epoch(window.get("resets_at"))
        if type(used) in (float, int) and 0 <= used <= 100 and reset and reset > now:
            snapshot[key] = used
            snapshot[f"{key}_reset"] = reset
    if "q5" not in snapshot and "q7" not in snapshot:
        return
    folder = resolve_data_dir(home, create=True)
    write_json(folder / "claude_statusline_quota.json", snapshot)


def probe_claude_quota(home: Path, cache: Dict[str, Any], now: float) -> Dict[str, Any]:
    fresh = _scan_claude_desktop_cache(home, now)
    cached = cache.get("claude") if isinstance(cache.get("claude"), dict) else {}
    cli = _load_json(resolve_data_dir(home, create=False) / "claude_statusline_quota.json")

    def _valid_ts(item: Dict[str, Any]) -> int:
        v = item.get("updated")
        if isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0:
            return int(v)
        return 0

    candidates = [item for item in (fresh or {}, cli, cached) if isinstance(item, dict) and (item.get("q5") is not None or item.get("q7") is not None)]
    non_future = [item for item in candidates if 0 < _valid_ts(item) <= now]
    if non_future:
        snap = max(non_future, key=_valid_ts)
    elif candidates:
        snap = max(candidates, key=_valid_ts)
    else:
        snap = {}

    if snap and snap.get("source") != "cli" and _valid_ts(snap) <= now:
        cache["claude"] = snap

    windows: List[Dict[str, Any]] = []
    if snap:
        if snap.get("q5") is not None:
            try:
                used = float(snap["q5"])
                if math.isfinite(used) and 0.0 <= used <= 100.0:
                    windows.append(
                        {
                            "id": "claude_5h",
                            "label": "5 小时窗口",
                            "short_label": "CL5",
                            "used_percent": round(used, 1),
                            "_raw_used_percent": used,
                            "remaining_percent": round(max(0.0, 100.0 - used), 1),
                            "resets_at": snap.get("q5_reset"),
                            "window_minutes": 300,
                        }
                    )
            except (TypeError, ValueError):
                pass
        if snap.get("q7") is not None:
            try:
                used = float(snap["q7"])
                if math.isfinite(used) and 0.0 <= used <= 100.0:
                    windows.append(
                        {
                            "id": "claude_7d",
                            "label": "7 天周额度",
                            "short_label": "CL7",
                            "used_percent": round(used, 1),
                            "_raw_used_percent": used,
                            "remaining_percent": round(max(0.0, 100.0 - used), 1),
                            "resets_at": snap.get("q7_reset"),
                            "window_minutes": 10080,
                        }
                    )
            except (TypeError, ValueError):
                pass

    if windows:
        updated = _valid_ts(snap) or int(now)
        not_future = (updated <= now)
        any_unreset = any(
            (w.get("resets_at") is None)
            or (isinstance(w.get("resets_at"), (int, float)) and float(w["resets_at"]) > now)
            for w in windows
        )
        is_fresh = not_future and any_unreset and (0.0 <= (now - updated) < CLAUDE_DISPLAY_TTL)
        if not not_future:
            status_text = "观测时间异常（晚于当前时钟）"
        elif not any_unreset:
            status_text = "窗口已重置，保留上次读数"
        elif snap.get("source") == "cli":
            status_text = "Claude Code 状态栏"
        elif snap is fresh:
            status_text = "桌面端缓存"
        else:
            status_text = "上次缓存快照"
        return {
            "id": "claude",
            "name": "Claude",
            "color": "#FB923C",
            "plan": "Pro / Max",
            "status": "ok" if is_fresh else "stale",
            "status_text": status_text,
            "fetched_at": updated,
            "windows": windows,
        }

    return {
        "id": "claude",
        "name": "Claude",
        "color": "#FB923C",
        "plan": "未同步",
        "status": "unavailable",
        "status_text": "打开 Claude 桌面端查看用量后自动同步",
        "fetched_at": int(now),
        "windows": [],
    }


# ---------------------------------------------------------------------------
# 3. Antigravity / Kimi / Grok (Cached & Local Status)
# ---------------------------------------------------------------------------


def probe_antigravity_quota(cache: Dict[str, Any], now: float) -> Dict[str, Any]:
    entry = cache.get("antigravity") if isinstance(cache.get("antigravity"), dict) else {}
    quota = entry.get("quota") if isinstance(entry.get("quota"), dict) else {}
    if quota.get("available") and quota.get("windows"):
        plan = quota.get("plan") or "Google AI Pro"
        windows: List[Dict[str, Any]] = []
        for w in quota.get("windows") or []:
            if not isinstance(w, dict):
                continue
            used = w.get("used_percent")
            rem = w.get("remaining_percent")
            if used is None and rem is not None:
                used = 100.0 - float(rem)
            if used is not None:
                windows.append(
                    {
                        "id": str(w.get("id") or "ag_win"),
                        "label": str(w.get("label") or w.get("id") or "配额窗口"),
                        "short_label": "AG",
                        "used_percent": round(float(used), 1),
                        "remaining_percent": round(max(0.0, 100.0 - float(used)), 1),
                        "resets_at": w.get("resets_at"),
                        "window_minutes": w.get("window_minutes"),
                    }
                )
        return {
            "id": "antigravity",
            "name": "Antigravity",
            "color": "#7C6BF5",
            "plan": str(plan),
            "status": "stale" if windows else "unavailable",
            "status_text": "仅缓存快照，未实时连接 Antigravity" if windows else "缓存缺少额度读数",
            "fetched_at": int(entry.get("fetched_at") or 0),
            "windows": windows,
        }

    return {
        "id": "antigravity",
        "name": "Antigravity",
        "color": "#7C6BF5",
        "plan": "Google AI",
        "status": "unavailable",
        "status_text": "未检测到本地配额缓存",
        "fetched_at": int(now),
        "windows": [],
    }


def probe_kimi_quota(cache: Dict[str, Any]) -> Dict[str, Any]:
    entry = cache.get("kimi") if isinstance(cache.get("kimi"), dict) else {}
    limits = entry.get("limits") if isinstance(entry.get("limits"), dict) else {}
    windows: List[Dict[str, Any]] = []
    fh = limits.get("five_hour") if isinstance(limits.get("five_hour"), dict) else None
    sub = limits.get("subscription") if isinstance(limits.get("subscription"), dict) else None
    if fh and fh.get("used_percent") is not None:
        used = float(fh["used_percent"])
        windows.append(
            {
                "id": "kimi_5h",
                "label": "5 小时窗口",
                "short_label": "K5",
                "used_percent": round(used, 1),
                "remaining_percent": round(max(0.0, 100.0 - used), 1),
                "resets_at": fh.get("resets_at"),
                "window_minutes": 300,
            }
        )
    if sub and sub.get("used_percent") is not None:
        used = float(sub["used_percent"])
        windows.append(
            {
                "id": "kimi_sub",
                "label": "订阅周期额度",
                "short_label": "KS",
                "used_percent": round(used, 1),
                "remaining_percent": round(max(0.0, 100.0 - used), 1),
                "resets_at": sub.get("resets_at"),
                "window_minutes": None,
            }
        )
    return {
        "id": "kimi",
        "name": "Kimi Code",
        "color": "#38BDF8",
        "plan": str(entry.get("plan") or "订阅制"),
        "status": "stale" if windows else "unavailable",
        "status_text": "仅缓存快照，未实时连接 Kimi" if windows else "未检测到本地配额缓存",
        "fetched_at": int(entry.get("fetched_at") or 0),
        "windows": windows,
    }


def probe_grok_quota(cache: Dict[str, Any], now: float) -> Dict[str, Any]:
    entry = cache.get("grok") if isinstance(cache.get("grok"), dict) else {}
    if entry.get("windows"):
        return {
            "id": "grok",
            "name": "Grok Bot",
            "color": "#94A3B8",
            "plan": str(entry.get("plan") or "Grok"),
            "status": "stale",
            "status_text": "仅缓存快照，未实时连接 Grok",
            "fetched_at": int(entry.get("fetched_at") or 0),
            "windows": entry["windows"],
        }
    return {
        "id": "grok",
        "name": "Grok Bot",
        "color": "#94A3B8",
        "plan": "本地凭据待刷新",
        "status": "unavailable",
        "status_text": "未检测到本地配额缓存",
        "fetched_at": int(now),
        "windows": [],
    }


# ---------------------------------------------------------------------------
# 4. Freshness & Pace Engine (v0.7)
# ---------------------------------------------------------------------------


def _window_fresh_until(provider: Dict[str, Any], window: Dict[str, Any], now: float) -> Optional[float]:
    if provider.get("status") != "ok":
        return None
    fetched_at = provider.get("fetched_at")
    if (
        not isinstance(fetched_at, (int, float))
        or not math.isfinite(float(fetched_at))
        or float(fetched_at) <= 0
        or float(fetched_at) > now
    ):
        return None
    used = window.get("_raw_used_percent", window.get("used_percent"))
    if not isinstance(used, (int, float)) or not math.isfinite(float(used)) or not (0.0 <= float(used) <= 100.0):
        return None

    pid = provider.get("id")
    if pid == "codex":
        ttl = CODEX_DISPLAY_TTL
    elif pid == "claude":
        ttl = CLAUDE_DISPLAY_TTL
    else:
        return None

    limit = float(fetched_at) + ttl
    resets_at = window.get("resets_at")
    if resets_at is not None:
        if not isinstance(resets_at, (int, float)) or not math.isfinite(float(resets_at)):
            return None
        if float(resets_at) <= now:
            return None
        limit = min(limit, float(resets_at))

    if now >= limit:
        return None
    return round(limit, 3)


def _load_pace_series(cache: Dict[str, Any]) -> List[Dict[str, Any]]:
    pace_root = cache.get("_pace_v1")
    if not isinstance(pace_root, dict):
        return []
    raw_series = pace_root.get("series")
    if not isinstance(raw_series, list):
        return []
    clean: List[Dict[str, Any]] = []
    for item in raw_series:
        if not isinstance(item, dict):
            continue
        pid = item.get("provider_id")
        wid = item.get("window_id")
        scope = item.get("account_scope")
        resets_at = item.get("resets_at")
        samples = item.get("samples")
        if (
            isinstance(pid, str)
            and isinstance(wid, str)
            and isinstance(scope, str)
            and scope
            and isinstance(resets_at, (int, float))
            and isinstance(samples, list)
        ):
            valid_samples: List[Dict[str, Any]] = []
            for s in samples:
                if not isinstance(s, dict):
                    continue
                oat = s.get("observed_at")
                up = s.get("used_percent")
                if (
                    isinstance(oat, (int, float))
                    and isinstance(up, (int, float))
                    and math.isfinite(float(oat))
                    and math.isfinite(float(up))
                    and 0.0 <= float(up) <= 100.0
                ):
                    valid_samples.append({"observed_at": int(oat), "used_percent": float(up)})
            clean.append(
                {
                    "provider_id": pid,
                    "window_id": wid,
                    "account_scope": scope,
                    "resets_at": int(resets_at),
                    "samples": valid_samples,
                }
            )
    return clean


def _normalize_samples(samples: List[Dict[str, Any]], current_observed_at: int) -> List[Dict[str, Any]]:
    floor_ts = current_observed_at - PACE_WINDOW_SECONDS
    recent = [s for s in samples if s["observed_at"] >= floor_ts]
    recent.sort(key=lambda s: s["observed_at"])

    # Keep the latest real observation inside each 5-minute bucket
    by_bucket: Dict[int, Dict[str, Any]] = {}
    for s in recent:
        b = s["observed_at"] // PACE_BUCKET_SECONDS
        prev = by_bucket.get(b)
        if prev is None or s["observed_at"] >= prev["observed_at"]:
            by_bucket[b] = s

    deduped = sorted(by_bucket.values(), key=lambda s: s["observed_at"])
    return deduped[-PACE_MAX_SAMPLES:]


def _compute_pace_for_window(
    series_list: List[Dict[str, Any]],
    provider: Dict[str, Any],
    window: Dict[str, Any],
    account_scope: Optional[str],
    now: float,
) -> Dict[str, Any]:
    fresh_until = window.get("fresh_until_ts")
    is_fresh = isinstance(fresh_until, (int, float)) and now < float(fresh_until)
    if not is_fresh:
        return {"level": "unknown", "reason": "stale", "sample_count": 0}

    resets_at = window.get("resets_at")
    if not isinstance(resets_at, (int, float)) or not math.isfinite(float(resets_at)) or float(resets_at) <= now:
        return {"level": "unknown", "reason": "missing_reset", "sample_count": 0}
    resets_int = int(resets_at)

    if not account_scope:
        return {"level": "unknown", "reason": "unverified_scope", "sample_count": 0}

    pid = str(provider["id"])
    wid = str(window["id"])
    observed_at = int(provider["fetched_at"])
    if observed_at > now:
        return {"level": "unknown", "reason": "stale", "sample_count": 0}
    used = float(window.get("_raw_used_percent", window["used_percent"]))

    # Remove rotated-window or switched-account series for this (provider_id, window_id)
    matched: Optional[Dict[str, Any]] = None
    kept: List[Dict[str, Any]] = []
    for item in series_list:
        if item["provider_id"] == pid and item["window_id"] == wid:
            if item["account_scope"] == account_scope and item["resets_at"] == resets_int:
                matched = item
                kept.append(item)
            # Else drop superseded window/account series
        else:
            kept.append(item)
    series_list[:] = kept

    if matched is None:
        matched = {
            "provider_id": pid,
            "window_id": wid,
            "account_scope": account_scope,
            "resets_at": resets_int,
            "samples": [],
        }
        series_list.append(matched)

    samples = sorted(matched["samples"], key=lambda s: s["observed_at"])
    if samples:
        last = samples[-1]
        if observed_at == last["observed_at"]:
            # Same cache hit: do not add duplicate sample
            pass
        elif observed_at < last["observed_at"] or used < float(last["used_percent"]):
            # Time rollback or usage drop: restart accumulation cleanly
            samples = [{"observed_at": observed_at, "used_percent": used}]
        else:
            samples.append({"observed_at": observed_at, "used_percent": used})
    else:
        samples = [{"observed_at": observed_at, "used_percent": used}]

    samples = _normalize_samples(samples, observed_at)
    matched["samples"] = samples

    if (
        len(samples) < PACE_MIN_SAMPLES
        or samples[-1]["observed_at"] != observed_at
        or (samples[-1]["observed_at"] - samples[0]["observed_at"]) < PACE_MIN_SPAN_SECONDS
    ):
        return {
            "level": "unknown",
            "reason": "insufficient_samples",
            "sample_count": len(samples),
        }

    first = samples[0]
    last = samples[-1]
    span_hours = (last["observed_at"] - first["observed_at"]) / 3600.0
    rate = (float(last["used_percent"]) - float(first["used_percent"])) / span_hours
    remaining_hours = max(0.0, (float(resets_int) - now) / 3600.0)
    projected = used + rate * remaining_hours

    if projected < 85.0:
        level = "on_track"
    elif projected < 100.0:
        level = "watch"
    else:
        level = "risk"

    exhaustion_ts: Optional[float] = None
    if rate > 0.0:
        ex_ts = now + ((100.0 - used) / rate) * 3600.0
        if ex_ts <= float(resets_int):
            exhaustion_ts = round(ex_ts, 1)

    pace_obj: Dict[str, Any] = {
        "level": level,
        "sample_count": len(samples),
        "rate_pct_per_hour": round(rate, 2),
        "projected_used_percent": round(projected, 1),
    }
    if exhaustion_ts is not None:
        pace_obj["estimated_exhaustion_ts"] = exhaustion_ts
    return pace_obj


def _prune_pace_series(series_list: List[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    floor_ts = int(now) - PACE_WINDOW_SECONDS
    out: List[Dict[str, Any]] = []
    for item in series_list:
        if item["resets_at"] <= now:
            continue
        samples = [s for s in item["samples"] if s["observed_at"] >= floor_ts]
        if not samples:
            continue
        item["samples"] = samples[-PACE_MAX_SAMPLES:]
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def collect_quotas(
    home: Optional[Path] = None,
    force: bool = False,
    now_ts: Optional[float] = None,
    allow_live: bool = False,
) -> Dict[str, Any]:
    home_dir = home or Path.home()
    now = now_ts if now_ts is not None else time.time()
    cache_path = resolve_data_dir(home_dir, create=True) / "quota_cache.json"
    account_scopes: Dict[str, Optional[str]] = {}

    with state_lock(cache_path):
        cache = _load_json(cache_path)
        series_list = _load_pace_series(cache)
        providers: List[Dict[str, Any]] = [
            probe_codex_quota(
                home_dir,
                cache,
                now,
                force=force,
                allow_live=allow_live,
                account_scope_out=account_scopes,
            ),
            probe_claude_quota(home_dir, cache, now),
            probe_antigravity_quota(cache, now),
            probe_kimi_quota(cache),
            probe_grok_quota(cache, now),
        ]

        # Annotate every window with fresh_until_ts and pace, then strip internal raw precision field
        for p in providers:
            scope = account_scopes.get(p["id"])
            for w in p.get("windows") or []:
                fresh_until = _window_fresh_until(p, w, now)
                if fresh_until is not None:
                    w["fresh_until_ts"] = fresh_until
                w["pace"] = _compute_pace_for_window(series_list, p, w, scope, now)
                w.pop("_raw_used_percent", None)

        pruned_series = _prune_pace_series(series_list, now)
        if pruned_series:
            cache["_pace_v1"] = {"series": pruned_series}
        elif "_pace_v1" in cache:
            cache["_pace_v1"] = {"series": []}

        write_json(cache_path, cache)

    # Extract top alert window strictly from currently fresh windows; tie-break by (provider_id, window_id)
    fresh_candidates: List[Tuple[float, str, str, Dict[str, Any]]] = []
    for p in providers:
        if p.get("status") != "ok":
            continue
        for w in p.get("windows") or []:
            fu = w.get("fresh_until_ts")
            if isinstance(fu, (int, float)) and now < float(fu):
                fresh_candidates.append(
                    (
                        -float(w["used_percent"]),
                        str(p["id"]),
                        str(w["id"]),
                        {
                            "provider_id": p["id"],
                            "provider_name": p["name"],
                            "label": w["label"],
                            "short_label": w["short_label"],
                            "used_percent": w["used_percent"],
                            "resets_at": w.get("resets_at"),
                        },
                    )
                )

    highest_win: Optional[Dict[str, Any]] = None
    if fresh_candidates:
        fresh_candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        highest_win = fresh_candidates[0][3]

    return {
        "schema_version": 1,
        "generated_at": datetime.fromtimestamp(now, tz=BIZ_TZ).isoformat(),
        "generated_ts": round(now, 3),
        "providers": providers,
        "highest_window": highest_win,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="AgentPulse slow-track quota collector")
    parser.add_argument("--json", action="store_true", help="Output compact JSON")
    parser.add_argument("--pretty", action="store_true", help="Output pretty JSON")
    parser.add_argument("--force", action="store_true", help="Bypass the freshness TTL for an enabled live check")
    parser.add_argument("--live-codex", action="store_true", help="Allow a Codex quota request using local credentials")
    parser.add_argument("--claude-statusline", action="store_true", help="Read quota windows from Claude Code status line JSON on stdin")
    args = parser.parse_args()
    env_now = os.environ.get("AGENTPULSE_NOW_TS")
    forced_now: Optional[float] = None
    if env_now:
        try:
            v = float(env_now)
            if math.isfinite(v) and v > 0:
                forced_now = v
        except ValueError:
            pass
    if args.claude_statusline:
        capture_claude_statusline(Path.home(), json.load(sys.stdin), forced_now if forced_now is not None else time.time())
        return 0

    try:
        payload = collect_quotas(force=args.force, now_ts=forced_now, allow_live=args.live_codex)
    except (OSError, ValueError, OverflowError, TypeError):
        print("无法刷新额度，请检查本地额度文件与数据目录。", file=sys.stderr)
        return 1
    if args.pretty:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
