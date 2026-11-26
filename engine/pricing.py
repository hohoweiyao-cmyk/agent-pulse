#!/usr/bin/env python3
"""AgentPulse — Model pricing resolver & cost calculator (MIT pricing baseline).

Zero external dependencies (Python 3.9+ stdlib only).
Loads bundled pricing.json + pricing_overrides.json and optional user overrides
at ~/.agent-pulse/pricing_overrides.json (with read-only fallback to ~/.agent-clock/).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

_DATE_SUFFIX_RE = re.compile(r"-(20\d{6}|20\d{2}-\d{2}-\d{2})$")
_VERSION_CLEAN_RE = re.compile(r"[:@].*$")


def _safe_float(val: Any, default: float = 0.0) -> float:
    try:
        v = float(val)
        return v if v >= 0 and math.isfinite(v) else default
    except (TypeError, ValueError):
        return default


class PricingTable:
    """Resolves model identifiers to per-1M-token USD rates."""

    def __init__(self, models: Dict[str, Dict[str, float]], aliases: Dict[str, str]):
        self.models: Dict[str, Dict[str, float]] = {}
        self.short_index: Dict[str, str] = {}
        self.aliases: Dict[str, str] = {}

        for k, v in (models or {}).items():
            if not isinstance(k, str) or not isinstance(v, dict):
                continue
            key = k.strip().lower()
            entry = {
                "in": _safe_float(v.get("in", v.get("input", 0.0))),
                "out": _safe_float(v.get("out", v.get("output", 0.0))),
                "cache_read": _safe_float(v.get("cache_read", v.get("cr", 0.0))),
                "cache_write": _safe_float(v.get("cache_write", v.get("cw", 0.0))),
            }
            self.models[key] = entry
            if "/" in key:
                short = key.split("/", 1)[1]
                self.short_index.setdefault(short, key)
            else:
                self.short_index.setdefault(key, key)

        for k, target in (aliases or {}).items():
            if isinstance(k, str) and isinstance(target, str):
                self.aliases[k.strip().lower()] = target.strip().lower()

        normalized = json.dumps([self.models, self.aliases, self.short_index], sort_keys=True)
        self.signature = hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    @classmethod
    def load(cls, home: Optional[Path] = None, engine_dir: Optional[Path] = None) -> "PricingTable":
        base_dir = engine_dir or Path(__file__).resolve().parent
        home_dir = home or Path.home()

        models: Dict[str, Dict[str, Any]] = {}
        aliases: Dict[str, str] = {}

        def merge_file(path: Path) -> None:
            if not path.is_file():
                return
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return
            if not isinstance(data, dict):
                return
            if isinstance(data.get("models"), dict):
                for mk, mv in data["models"].items():
                    if isinstance(mv, dict):
                        models[mk] = mv
            if isinstance(data.get("aliases"), dict):
                for ak, av in data["aliases"].items():
                    if isinstance(av, str):
                        aliases[ak] = av

        merge_file(base_dir / "pricing.json")
        merge_file(base_dir / "pricing_overrides.json")
        merge_file(home_dir / ".agent-clock" / "pricing_overrides.json")
        merge_file(home_dir / ".agent-pulse" / "pricing_overrides.json")
        return cls(models=models, aliases=aliases)

    def resolve(self, model_name: Optional[str]) -> Optional[Dict[str, float]]:
        if not model_name or not isinstance(model_name, str):
            return None
        raw = model_name.strip().lower()
        if not raw or raw in ("unknown", "auto", "default"):
            return None

        candidates = [raw]
        cleaned = _VERSION_CLEAN_RE.sub("", raw)
        if cleaned != raw:
            candidates.append(cleaned)

        # Strip provider prefixes like "proxy/claude-...", "openai/...", "anthropic/..."
        for cand in list(candidates):
            if "/" in cand:
                tail = cand.rsplit("/", 1)[1]
                candidates.append(tail)
                if tail.startswith("models/"):
                    candidates.append(tail[7:])

        # Strip date suffixes like -20260902 or -2026-09-02
        for cand in list(candidates):
            stripped = _DATE_SUFFIX_RE.sub("", cand)
            if stripped != cand:
                candidates.append(stripped)

        # Normalize underscores / dots variants
        for cand in list(candidates):
            if "_" in cand:
                candidates.append(cand.replace("_", "-"))

        seen = set()
        for cand in candidates:
            if not cand or cand in seen:
                continue
            seen.add(cand)
            if cand in self.aliases:
                target = self.aliases[cand]
                if target in self.models:
                    return self.models[target]
                if target in self.short_index:
                    return self.models[self.short_index[target]]
            if cand in self.models:
                return self.models[cand]
            if cand in self.short_index:
                return self.models[self.short_index[cand]]

        # Prefix fallback: match longest known short model name that is a prefix of candidate
        base_tail = raw.rsplit("/", 1)[-1]
        best_key: Optional[str] = None
        best_len = 0
        for short_name, full_key in self.short_index.items():
            if len(short_name) >= 6 and len(short_name) > best_len:
                if base_tail.startswith(short_name) or short_name.startswith(base_tail):
                    best_key = full_key
                    best_len = len(short_name)
        if best_key:
            return self.models[best_key]

        return None

    def estimate_cost(
        self,
        model_name: Optional[str],
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        reasoning_tokens: int = 0,
    ) -> float:
        rates = self.resolve(model_name)
        if not rates:
            return 0.0

        # Pricing JSON values are USD per 1M tokens.
        in_tok = max(0, int(input_tokens or 0))
        out_tok = max(0, int(output_tokens or 0))
        cr_tok = max(0, int(cache_read_tokens or 0))
        cw_tok = max(0, int(cache_write_tokens or 0))
        r_tok = max(0, int(reasoning_tokens or 0))

        # Reasoning tokens are billed at output rate if not already inside output_tokens.
        cost = (
            in_tok * rates["in"]
            + (out_tok + r_tok) * rates["out"]
            + cr_tok * rates["cache_read"]
            + cw_tok * rates["cache_write"]
        ) / 1_000_000.0
        return round(max(0.0, cost), 6)
