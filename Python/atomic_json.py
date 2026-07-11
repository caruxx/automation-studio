#!/usr/bin/env python3
"""Locked, atomic JSON persistence for Automation Studio.

Missing files and corrupt files are intentionally different states.  A corrupt
file is never overwritten: it is copied aside for recovery and the write is
aborted with ``CorruptJsonError``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


class CorruptJsonError(ValueError):
    """Raised when an existing JSON file cannot be decoded."""

    def __init__(self, path: Path, detail: str, backup_path: Path | None = None):
        self.path = Path(path)
        self.detail = detail
        self.backup_path = Path(backup_path) if backup_path else None
        suffix = f"; backup={self.backup_path}" if self.backup_path else ""
        super().__init__(f"corrupt JSON: {self.path}: {detail}{suffix}")


def _lock_path(path: Path) -> Path:
    # Google Drive/File Provider 配下に lock ファイルを増やさず、絶対パスごとに固定。
    lock_root = Path(os.environ.get("AUTOMATION_JSON_LOCK_DIR") or "/tmp/automation_studio_json_locks")
    digest = hashlib.sha256(str(path.expanduser().absolute()).encode("utf-8")).hexdigest()
    return lock_root / f"{digest}.lock"


@contextmanager
def file_lock(path: Path, *, exclusive: bool, timeout: float = 10.0) -> Iterator[None]:
    """Acquire a per-target advisory lock without changing the target file."""
    path = Path(path)
    lp = _lock_path(path)
    lp.parent.mkdir(parents=True, exist_ok=True)
    mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    deadline = time.monotonic() + max(0.0, float(timeout))
    with lp.open("a+", encoding="utf-8") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"JSON lock timeout: {path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _decode(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CorruptJsonError(path, f"{exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    except UnicodeDecodeError as exc:
        raise CorruptJsonError(path, str(exc)) from exc


def load_json(path: Path, default: Any = None, *, lock_timeout: float = 10.0) -> Any:
    """Load JSON; return default only for a missing file, never for corruption."""
    path = Path(path)
    if not path.exists():
        return default if default is not None else {}
    with file_lock(path, exclusive=False, timeout=lock_timeout):
        if not path.exists():
            return default if default is not None else {}
        return _decode(path)


def _corrupt_backup_path(path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return path.with_name(f"{path.name}.corrupt-{stamp}.bak")


def _backup_corrupt(path: Path, exc: CorruptJsonError) -> CorruptJsonError:
    backup = _corrupt_backup_path(path)
    shutil.copy2(path, backup)
    return CorruptJsonError(path, exc.detail, backup)


def atomic_write_json(
    path: Path,
    data: Any,
    *,
    indent: int | None = 2,
    ensure_ascii: bool = False,
    lock_timeout: float = 10.0,
    validate_existing: bool = True,
) -> Any:
    """Atomically replace a JSON file while holding its per-file lock.

    Returns the previous decoded value (or ``{}`` when the file was missing).
    If the existing file is corrupt, a byte-for-byte recovery copy is created
    and the save is aborted.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path, exclusive=True, timeout=lock_timeout):
        old: Any = {}
        if path.exists() and validate_existing:
            try:
                old = _decode(path)
            except CorruptJsonError as exc:
                raise _backup_corrupt(path, exc) from exc

        payload = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
        tmp = Path(tmp_name)
        try:
            if path.exists():
                try:
                    os.fchmod(fd, path.stat().st_mode & 0o777)
                except OSError:
                    pass
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        return old
