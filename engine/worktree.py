"""Standard Git metadata worktree resolver for AgentPulse v0.7.

Strictly read-only. Never scans ~/.orca or guesses by repository name, remote URL,
or path prefix. Merges a linked worktree into its parent repository ONLY when both
the linked worktree and its non-bare primary worktree are verified via:
  1. `git rev-parse --show-toplevel --absolute-git-dir --git-common-dir`
  2. `git worktree list --porcelain -z`
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

_STRIP_GIT_ENV = frozenset({
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_PREFIX",
    "GIT_SUPER_PREFIX",
    "GIT_CONFIG_PARAMETERS",
    "GIT_CONFIG_COUNT",
    "GIT_CONFIG_GLOBAL",
    "GIT_CONFIG_SYSTEM",
    "GIT_NAMESPACE",
})


@dataclass(frozen=True)
class WorktreeResolution:
    canonical_root: str
    source_path: str
    branch: Optional[str] = None
    is_linked_worktree: bool = False
    verified_git: bool = False


def _find_git_binary() -> Optional[str]:
    for cand in ("/usr/bin/git", "/opt/homebrew/bin/git", "/usr/local/bin/git"):
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            return cand
    return shutil.which("git")


def _sanitized_git_env() -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_GIT_ENV}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["LC_ALL"] = "C"
    return env


def _read_head_branch(git_dir: str) -> Optional[str]:
    """Read branch name from <git_dir>/HEAD without spawning a subprocess."""
    try:
        head_text = Path(git_dir, "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    prefix = "ref: refs/heads/"
    if head_text.startswith(prefix):
        branch = head_text[len(prefix):].strip()
        return branch or None
    return None


def _parse_worktree_porcelain_z(raw: bytes) -> List[Dict[str, Optional[str]]]:
    entries: List[Dict[str, Optional[str]]] = []
    current: Optional[Dict[str, Optional[str]]] = None
    for token_bytes in raw.split(b"\x00"):
        token = token_bytes.decode("utf-8", errors="surrogateescape")
        if not token:
            if current is not None:
                entries.append(current)
                current = None
            continue
        if token.startswith("worktree "):
            if current is not None:
                entries.append(current)
            current = {"worktree": token[9:], "branch": None, "detached": None, "bare": None}
        elif current is not None:
            if token.startswith("branch refs/heads/"):
                current["branch"] = token[len("branch refs/heads/"):]
            elif token == "detached":
                current["detached"] = "1"
                current["branch"] = None
            elif token == "bare":
                current["bare"] = "1"
    if current is not None:
        entries.append(current)
    return entries


class WorktreeResolver:
    """Per-scan cached Git worktree resolver."""

    def __init__(self, timeout: float = 1.5):
        self.git_bin = _find_git_binary()
        self.env = _sanitized_git_env()
        self.timeout = timeout
        self._cache: Dict[str, WorktreeResolution] = {}
        self._porcelain_cache: Dict[str, List[Dict[str, Optional[str]]]] = {}

    def _run_git(self, cwd: str, args: List[str]) -> Optional[bytes]:
        if not self.git_bin:
            return None
        try:
            proc = subprocess.run(
                [self.git_bin, "-C", cwd, *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=self.env,
                timeout=self.timeout,
                check=False,
            )
            if proc.returncode != 0:
                return None
            return proc.stdout
        except (OSError, subprocess.SubprocessError):
            return None

    def _verify_main_common_dir(self, worktree_path: str, expected_common_dir: str) -> bool:
        dot_git = os.path.join(worktree_path, ".git")
        if os.path.isdir(dot_git):
            return os.path.realpath(dot_git) == expected_common_dir
        out = self._run_git(worktree_path, ["rev-parse", "--absolute-git-dir", "--git-common-dir"])
        if not out:
            return False
        lines = out.decode("utf-8", errors="surrogateescape").splitlines()
        if len(lines) != 2:
            return False
        gdir = os.path.realpath(lines[0])
        cdir = os.path.realpath(os.path.join(worktree_path, lines[1]))
        return gdir == expected_common_dir and cdir == expected_common_dir

    def resolve(self, raw_path: Optional[str]) -> WorktreeResolution:
        if not isinstance(raw_path, str):
            return WorktreeResolution(canonical_root="", source_path="")
        norm = raw_path.rstrip("/")
        if not norm or not norm.startswith("/"):
            return WorktreeResolution(canonical_root="", source_path="")

        cached = self._cache.get(norm)
        if cached is not None:
            return cached

        fallback = WorktreeResolution(
            canonical_root=norm,
            source_path=norm,
            branch=None,
            is_linked_worktree=False,
            verified_git=False,
        )

        if not os.path.isdir(norm):
            self._cache[norm] = fallback
            return fallback

        out = self._run_git(norm, ["rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir"])
        if not out:
            self._cache[norm] = fallback
            return fallback

        lines = out.decode("utf-8", errors="surrogateescape").splitlines()
        if len(lines) != 3:
            self._cache[norm] = fallback
            return fallback

        toplevel = os.path.realpath(lines[0])
        git_dir = os.path.realpath(lines[1])
        common_dir = os.path.realpath(os.path.join(norm, lines[2]))

        if not os.path.isdir(toplevel):
            self._cache[norm] = fallback
            return fallback

        # Submodules and main worktrees have git_dir == common_dir.
        if git_dir == common_dir:
            branch = _read_head_branch(git_dir)
            res = WorktreeResolution(
                canonical_root=toplevel,
                source_path=toplevel,
                branch=branch,
                is_linked_worktree=False,
                verified_git=True,
            )
            self._cache[norm] = res
            self._cache.setdefault(toplevel, res)
            return res

        # Candidate linked worktree: verify BOTH current item and non-bare primary worktree.
        entries = self._porcelain_cache.get(common_dir)
        if entries is None:
            wt_raw = self._run_git(toplevel, ["worktree", "list", "--porcelain", "-z"])
            entries = _parse_worktree_porcelain_z(wt_raw) if wt_raw else []
            self._porcelain_cache[common_dir] = entries

        cur_entry: Optional[Dict[str, Optional[str]]] = None
        main_entry: Optional[Dict[str, Optional[str]]] = None

        for entry in entries:
            wpath = entry.get("worktree")
            if not wpath:
                continue
            real_wpath = os.path.realpath(wpath)
            if real_wpath == toplevel:
                cur_entry = entry
            if main_entry is None:
                if entry.get("bare") is None and os.path.isdir(real_wpath):
                    if self._verify_main_common_dir(real_wpath, common_dir):
                        main_entry = entry

        if cur_entry is not None and main_entry is not None and main_entry.get("worktree"):
            main_root = os.path.realpath(main_entry["worktree"])  # type: ignore[arg-type]
            branch = cur_entry.get("branch") if not cur_entry.get("detached") else None
            res = WorktreeResolution(
                canonical_root=main_root,
                source_path=toplevel,
                branch=branch,
                is_linked_worktree=True,
                verified_git=True,
            )
            self._cache[norm] = res
            self._cache.setdefault(toplevel, res)
            return res

        self._cache[norm] = fallback
        return fallback
