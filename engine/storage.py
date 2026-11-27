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
import time


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


def file_identity(path):
    """Return a tuple identifying a regular file's version, or None if missing."""
    try:
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
    except FileNotFoundError:
        return None


_file_identity = file_identity


@contextmanager
def sqlite_snapshot_with_status(source):
    """Copy a stable DB and WAL without opening or locking the live database.

    Yields (connection_or_none, status) where status is one of:
    'ok', 'missing', 'permission_error', 'unstable', 'corrupt', 'unavailable'.
    """
    try:
        st = source.stat()
    except FileNotFoundError:
        yield None, "missing"
        return
    except PermissionError:
        yield None, "permission_error"
        return
    except OSError:
        yield None, "unavailable"
        return

    if not stat.S_ISREG(st.st_mode):
        yield None, "unavailable"
        return

    wal = source.with_name(source.name + "-wal")
    with tempfile.TemporaryDirectory(prefix="agentpulse-sqlite-") as directory:
        connection = None
        status = "unavailable"
        for attempt in range(2):
            snapshot = Path(directory) / f"{attempt}.sqlite"
            try:
                before = (_file_identity(source), _file_identity(wal))
                if before[0] is None:
                    status = "missing"
                    break
                shutil.copyfile(source, snapshot)
                if before[1] is not None:
                    shutil.copyfile(wal, snapshot.with_name(snapshot.name + "-wal"))
                after = (_file_identity(source), _file_identity(wal))
                if before != after:
                    status = "unstable"
                    if attempt == 0:
                        time.sleep(0.02)
                    continue
                # Open the isolated temporary copy so SQLite can replay WAL frames
                # or open checkpointed WAL-header DBs cleanly without touching source.
                conn = sqlite3.connect(str(snapshot), timeout=2.0)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA query_only = ON")
                conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchall()
                connection = conn
                status = "ok"
                break
            except PermissionError:
                status = "permission_error"
                break
            except sqlite3.Error:
                status = "corrupt"
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                    connection = None
                continue
            except OSError:
                status = "unavailable"
                continue
        try:
            yield connection, status
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass


@contextmanager
def sqlite_snapshot(source):
    """Copy a stable DB and WAL without opening or locking the live database."""
    with sqlite_snapshot_with_status(source) as (connection, _):
        yield connection
