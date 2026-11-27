"""Collection health tracking and checked JSONL readers for AgentPulse v0.7."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from storage import file_identity

WORK_SOURCE_IDS: Tuple[str, ...] = (
    "work.t3",
    "work.codex",
    "work.workbuddy",
    "work.hermes",
    "work.antigravity_desktop",
    "work.antigravity_cli",
    "work.claude_code",
    "work.pi",
    "work.opencode",
)

USAGE_SOURCE_IDS: Tuple[str, ...] = (
    "usage.codex",
    "usage.workbuddy",
    "usage.codebuddy",
    "usage.claude_code",
    "usage.pi",
    "usage.hermes",
    "usage.opencode",
    "usage.gemini",
    "usage.zcode",
)

PROJECT_USAGE_SOURCE_IDS: Tuple[str, ...] = (
    "usage.codex",
    "usage.workbuddy",
    "usage.codebuddy",
    "usage.claude_code",
    "usage.pi",
)

TOOL_TO_USAGE_SOURCE: Dict[str, str] = {
    "codex": "usage.codex",
    "t3_codex": "usage.codex",
    "workbuddy": "usage.workbuddy",
    "codebuddy": "usage.codebuddy",
    "claude_code": "usage.claude_code",
    "claude": "usage.claude_code",
    "pi": "usage.pi",
    "hermes": "usage.hermes",
    "opencode": "usage.opencode",
    "gemini": "usage.gemini",
    "zcode": "usage.zcode",
}

VALID_HEALTH_STATUSES = frozenset({"complete", "partial", "unavailable", "not_configured"})


@dataclass
class JsonlReadResult:
    records: List[Tuple[int, Dict[str, Any]]]
    had_error: bool = False
    identity_changed: bool = False
    permission_error: bool = False
    missing: bool = False
    stat_result: Optional[os.stat_result] = None
    error_kind: Optional[str] = None


def read_jsonl_checked(
    path: Path,
    *,
    keyword_filter: Optional[Tuple[str, ...]] = None,
) -> JsonlReadResult:
    """Read a JSONL file with pre/post identity check and one retry for incomplete tail lines.

    Every non-empty line is strictly JSON-decoded and verified to be a JSON object so corrupt
    middle lines (even with balanced `{...}` braces and missing filter keywords) are always
    detected. When `keyword_filter` is given, only matching objects are retained in `records`.
    """
    for attempt in range(2):
        try:
            st_before = path.stat()
            before = (st_before.st_dev, st_before.st_ino, st_before.st_size, st_before.st_mtime_ns, st_before.st_ctime_ns)
        except FileNotFoundError:
            return JsonlReadResult(records=[], had_error=True, missing=True, error_kind="missing")
        except PermissionError:
            return JsonlReadResult(records=[], had_error=True, permission_error=True, error_kind="permission_error")
        except OSError:
            return JsonlReadResult(records=[], had_error=True, error_kind="io_error")

        records: List[Tuple[int, Dict[str, Any]]] = []
        bad_indices: List[int] = []
        last_non_empty_idx = -1

        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for idx, raw in enumerate(fh):
                    s = raw.strip()
                    if not s:
                        continue
                    last_non_empty_idx = idx

                    try:
                        obj = json.loads(s)
                    except json.JSONDecodeError:
                        bad_indices.append(idx)
                        continue

                    if not isinstance(obj, dict):
                        bad_indices.append(idx)
                        continue

                    if keyword_filter is None or any(kw in s for kw in keyword_filter):
                        records.append((idx, obj))
        except PermissionError:
            return JsonlReadResult(records=[], had_error=True, permission_error=True, error_kind="permission_error")
        except FileNotFoundError:
            return JsonlReadResult(records=[], had_error=True, missing=True, error_kind="missing")
        except OSError:
            return JsonlReadResult(records=[], had_error=True, error_kind="io_error")

        try:
            st_after = path.stat()
            after = (st_after.st_dev, st_after.st_ino, st_after.st_size, st_after.st_mtime_ns, st_after.st_ctime_ns)
        except OSError:
            after = None
            st_after = None

        changed = (after is None) or (before != after)
        is_tail_only_error = (len(bad_indices) == 1 and bad_indices[0] == last_non_empty_idx)

        if not changed and not bad_indices:
            return JsonlReadResult(
                records=records,
                had_error=False,
                identity_changed=False,
                stat_result=st_after,
            )

        if attempt == 0 and (changed or is_tail_only_error):
            time.sleep(0.02)
            continue

        err_kind = "unstable_file" if changed else ("incomplete_tail" if is_tail_only_error else "corrupt_jsonl")
        return JsonlReadResult(
            records=records,
            had_error=True,
            identity_changed=changed,
            stat_result=st_after if not changed else None,
            error_kind=err_kind,
        )

    return JsonlReadResult(records=[], had_error=True, identity_changed=True, error_kind="unstable_file")


def combine_statuses(statuses: Sequence[str]) -> str:
    """Combine multiple component health statuses into one domain health status."""
    cleaned = [s if s in VALID_HEALTH_STATUSES else "unavailable" for s in statuses]
    if not cleaned or all(s == "not_configured" for s in cleaned):
        return "not_configured"
    active = [s for s in cleaned if s != "not_configured"]
    if all(s == "complete" for s in active):
        return "complete"
    if all(s == "unavailable" for s in active):
        return "unavailable"
    return "partial"


def resolve_missing_source_status(source_id: str, prev_sources: Dict[str, Any]) -> str:
    """A source missing now is 'unavailable' if seen_configured previously, else 'not_configured'."""
    prev = prev_sources.get(source_id)
    if isinstance(prev, dict) and prev.get("seen_configured"):
        return "unavailable"
    return "not_configured"


def check_root_dir(root: Path, source_id: str, prev_sources: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Verify whether a source root directory exists and is accessible.

    Returns (can_scan, early_status_if_cannot_scan).
    """
    try:
        if not root.exists():
            return False, resolve_missing_source_status(source_id, prev_sources)
        if not root.is_dir():
            return False, "unavailable"
        # Probe directory readability
        with os.scandir(root) as it:
            next(it, None)
        return True, None
    except PermissionError:
        return False, "unavailable"
    except FileNotFoundError:
        return False, resolve_missing_source_status(source_id, prev_sources)
    except OSError:
        return False, "unavailable"


def make_health_entry(status: str, last_success_ts: Optional[float] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": status}
    if last_success_ts is not None:
        out["last_success_ts"] = round(float(last_success_ts), 3)
    return out


def seen_source_instances(source_id: str, prev_sources: Dict[str, Any]) -> set:
    """Return previously seen instance keys (e.g., Hermes main/profile state.db paths) for `source_id`."""
    prev = prev_sources.get(source_id)
    if not isinstance(prev, dict):
        return set()
    raw = prev.get("seen_instances")
    if not isinstance(raw, list):
        return set()
    return {str(x) for x in raw if isinstance(x, str) and x}


def normalize_source_statuses(
    raw_source_statuses: Dict[str, str],
    prev_collection: Optional[Dict[str, Any]],
    *,
    observed_instances: Optional[Dict[str, Iterable[str]]] = None,
) -> Dict[str, str]:
    """Normalize raw scan statuses against the latest on-disk `seen_configured` and `seen_instances`."""
    prev = prev_collection if isinstance(prev_collection, dict) else {}
    prev_sources = prev.get("sources") if isinstance(prev.get("sources"), dict) else {}
    obs_map = observed_instances or {}

    all_source_ids = WORK_SOURCE_IDS + USAGE_SOURCE_IDS
    normalized: Dict[str, str] = {}
    for sid in all_source_ids:
        prev_entry = prev_sources.get(sid) if isinstance(prev_sources.get(sid), dict) else {}
        prev_seen = bool(prev_entry.get("seen_configured"))
        prev_insts = seen_source_instances(sid, prev_sources)

        status = raw_source_statuses.get(sid)
        if status is None or status not in VALID_HEALTH_STATUSES:
            status = resolve_missing_source_status(sid, prev_sources)
        elif status == "not_configured" and prev_seen:
            status = "unavailable"

        if prev_insts and sid in obs_map:
            cur_insts = {str(x) for x in obs_map[sid] if isinstance(x, str) and x}
            if prev_insts - cur_insts:
                if status == "complete":
                    status = "partial" if cur_insts else "unavailable"
                elif status == "not_configured":
                    status = "unavailable"

        normalized[sid] = status
    return normalized


def collection_payload_from_ledger(prev_collection: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build a read-only `collection` payload strictly from the accepted on-disk ledger state.

    Used when a concurrent older `commit_ts` scan is rejected so rejected scan statuses and
    the accepted scan's `commit_ts` are never forged as `last_success_ts`.
    """
    prev = prev_collection if isinstance(prev_collection, dict) else {}
    prev_sources = prev.get("sources") if isinstance(prev.get("sources"), dict) else {}
    prev_domains = prev.get("domain_last_success_ts") if isinstance(prev.get("domain_last_success_ts"), dict) else {}
    prev_domain_statuses = prev.get("domain_statuses") if isinstance(prev.get("domain_statuses"), dict) else {}

    all_source_ids = WORK_SOURCE_IDS + USAGE_SOURCE_IDS
    sources_payload: Dict[str, Dict[str, Any]] = {}
    for sid in all_source_ids:
        prev_entry = prev_sources.get(sid) if isinstance(prev_sources.get(sid), dict) else {}
        prev_seen = bool(prev_entry.get("seen_configured"))
        prev_ts = prev_entry.get("last_success_ts")
        if not isinstance(prev_ts, (int, float)):
            prev_ts = None
        saved_status = prev_entry.get("status")
        if isinstance(saved_status, str) and saved_status in VALID_HEALTH_STATUSES:
            status = saved_status
        else:
            status = "unavailable" if prev_seen else "not_configured"
        sources_payload[sid] = make_health_entry(status, prev_ts)

    def _domain_status(key: str, fallback: str) -> str:
        s = prev_domain_statuses.get(key)
        return s if isinstance(s, str) and s in VALID_HEALTH_STATUSES else fallback

    work_status = _domain_status("work", combine_statuses([sources_payload[s]["status"] for s in WORK_SOURCE_IDS]))
    usage_status = _domain_status("usage", combine_statuses([sources_payload[s]["status"] for s in USAGE_SOURCE_IDS]))
    proj_fallback = combine_statuses(
        [sources_payload[s]["status"] for s in WORK_SOURCE_IDS]
        + [sources_payload[s]["status"] for s in PROJECT_USAGE_SOURCE_IDS]
    )
    projects_status = _domain_status("projects", proj_fallback)

    def _domain_ts(key: str) -> Optional[float]:
        v = prev_domains.get(key)
        return round(float(v), 3) if isinstance(v, (int, float)) else None

    return {
        "work": make_health_entry(work_status, _domain_ts("work")),
        "usage": make_health_entry(usage_status, _domain_ts("usage")),
        "projects": make_health_entry(projects_status, _domain_ts("projects")),
        "sources": sources_payload,
    }


def build_collection_payload(
    raw_source_statuses: Dict[str, str],
    project_work_meta_statuses: Dict[str, str],
    prev_collection: Optional[Dict[str, Any]],
    now_ts: float,
    *,
    observed_instances: Optional[Dict[str, Iterable[str]]] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Build (collection_payload, next_ledger_collection_meta).

    - Each source records `seen_configured`, `status`, `last_success_ts` (only on `complete`),
      and optional `seen_instances` (union of all ever-seen DB instances).
    - Domain `work`, `usage`, `projects` combine their constituent sources.
    """
    prev = prev_collection if isinstance(prev_collection, dict) else {}
    prev_sources = prev.get("sources") if isinstance(prev.get("sources"), dict) else {}
    prev_domains = prev.get("domain_last_success_ts") if isinstance(prev.get("domain_last_success_ts"), dict) else {}
    obs_map = observed_instances or {}

    normalized = normalize_source_statuses(
        raw_source_statuses,
        prev_collection,
        observed_instances=observed_instances,
    )

    all_source_ids = WORK_SOURCE_IDS + USAGE_SOURCE_IDS
    sources_payload: Dict[str, Dict[str, Any]] = {}
    ledger_sources: Dict[str, Dict[str, Any]] = {}

    # Preserve any unknown future source keys already in ledger
    for sid, sval in prev_sources.items():
        if isinstance(sval, dict) and sid not in all_source_ids:
            ledger_sources[sid] = dict(sval)

    for sid in all_source_ids:
        prev_entry = prev_sources.get(sid) if isinstance(prev_sources.get(sid), dict) else {}
        prev_seen = bool(prev_entry.get("seen_configured"))
        prev_ts = prev_entry.get("last_success_ts")
        if not isinstance(prev_ts, (int, float)):
            prev_ts = None
        prev_insts = seen_source_instances(sid, prev_sources)
        cur_insts = {str(x) for x in obs_map.get(sid, ()) if isinstance(x, str) and x}
        merged_insts = sorted(prev_insts | cur_insts)

        status = normalized[sid]
        seen = prev_seen or (status != "not_configured") or bool(merged_insts)
        last_ts = round(float(now_ts), 3) if status == "complete" else prev_ts

        sources_payload[sid] = make_health_entry(status, last_ts)
        entry: Dict[str, Any] = {
            "seen_configured": seen,
            "status": status,
        }
        if last_ts is not None:
            entry["last_success_ts"] = last_ts
        if merged_insts:
            entry["seen_instances"] = merged_insts
        ledger_sources[sid] = entry

    work_status = combine_statuses([sources_payload[s]["status"] for s in WORK_SOURCE_IDS])
    usage_status = combine_statuses([sources_payload[s]["status"] for s in USAGE_SOURCE_IDS])

    # Projects domain: merge work-project health (work source + metadata health) AND project-cost usage sources
    proj_components: List[str] = []
    for sid in WORK_SOURCE_IDS:
        ws = sources_payload[sid]["status"]
        ms = project_work_meta_statuses.get(sid)
        if ms and ms != "complete" and ws == "complete":
            proj_components.append(ms)
        else:
            proj_components.append(ws)
    for sid in PROJECT_USAGE_SOURCE_IDS:
        proj_components.append(sources_payload[sid]["status"])
    projects_status = combine_statuses(proj_components)

    domain_ts: Dict[str, float] = {}
    for dkey, dstatus in (("work", work_status), ("usage", usage_status), ("projects", projects_status)):
        if dstatus == "complete":
            domain_ts[dkey] = round(float(now_ts), 3)
        elif isinstance(prev_domains.get(dkey), (int, float)):
            domain_ts[dkey] = round(float(prev_domains[dkey]), 3)

    collection_payload = {
        "work": make_health_entry(work_status, domain_ts.get("work")),
        "usage": make_health_entry(usage_status, domain_ts.get("usage")),
        "projects": make_health_entry(projects_status, domain_ts.get("projects")),
        "sources": sources_payload,
    }
    ledger_collection = {
        "commit_ts": round(float(now_ts), 6),
        "sources": ledger_sources,
        "domain_statuses": {
            "work": work_status,
            "usage": usage_status,
            "projects": projects_status,
        },
        "domain_last_success_ts": domain_ts,
    }
    return collection_payload, ledger_collection
