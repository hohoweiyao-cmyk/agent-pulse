"""Private, atomic storage for AgentPulse's own files."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile


def private_directory(path):
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("AgentPulse 数据目录必须是当前用户拥有的目录")
    if stat.S_IMODE(info.st_mode) != 0o700:
        path.chmod(0o700)


def protect_existing_file(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise OSError("AgentPulse 数据文件必须是当前用户拥有的普通文件")
    if stat.S_IMODE(info.st_mode) != 0o600:
        path.chmod(0o600)


@contextmanager
def state_lock(path):
    # The lock file stays in place so every process locks the same inode.
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


@contextmanager
def _atomic_file(target, mode):
    with tempfile.NamedTemporaryFile(
        mode, dir=target.parent, prefix="." + target.name + "-", delete=False,
        **({"encoding": "utf-8"} if mode == "w" else {}),
    ) as stream:
        try:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            os.replace(stream.name, target)
        finally:
            if os.path.exists(stream.name):
                os.unlink(stream.name)


def write_json(target, value):
    with _atomic_file(target, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def copy_private(source, target):
    with source.open("rb") as original, _atomic_file(target, "wb") as copy:
        shutil.copyfileobj(original, copy)


def _file_identity(path):
    try:
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except FileNotFoundError:
        return None


@contextmanager
def sqlite_snapshot(source):
    """Copy a stable DB and WAL without opening or locking the live database."""
    if not source.is_file():
        yield None
        return
    wal = source.with_name(source.name + "-wal")
    with tempfile.TemporaryDirectory(prefix="agentpulse-sqlite-") as directory:
        connection = None
        for attempt in range(2):
            snapshot = Path(directory) / f"{attempt}.sqlite"
            try:
                before = (_file_identity(source), _file_identity(wal))
                if before[0] is None:
                    break
                shutil.copyfile(source, snapshot)
                if before[1] is not None:
                    shutil.copyfile(wal, snapshot.with_name(snapshot.name + "-wal"))
                if before != (_file_identity(source), _file_identity(wal)):
                    continue
                connection = sqlite3.connect(snapshot.as_uri() + "?mode=ro", uri=True, timeout=2.0)
                connection.row_factory = sqlite3.Row
                break
            except (OSError, sqlite3.Error):
                continue
        try:
            yield connection
        finally:
            if connection is not None:
                connection.close()
