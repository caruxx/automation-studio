#!/usr/bin/env python3
"""Cross-process locks for Automation Studio's single-instance resources."""
from __future__ import annotations

import fcntl
import os
import re
import time
from pathlib import Path


LOCK_DIR = Path(os.environ.get("AUTOMATION_STUDIO_LOCK_DIR") or "/tmp/automation_studio_locks")
RESOURCE_ALIASES = {
    "suno": "suno-browser",
    "suno-auto": "suno-browser",
    "suno-download": "suno-browser",
    "suno-browser": "suno-browser",
    "premiere": "premiere-ame",
    "place-images": "premiere-ame",
    "export": "premiere-ame",
    "pipeline-from-premiere": "premiere-ame",
    "premiere-ame": "premiere-ame",
    "psd": "photoshop",
    "psd_composite": "photoshop",
    "photoshop": "photoshop",
}


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.:-]+", "_", str(value or "resource").strip())
    return value.strip("._-") or "resource"


def canonical_resource(name: str) -> str:
    return RESOURCE_ALIASES.get(str(name or "").strip(), safe_name(name))


def held_resources() -> set[str]:
    raw = os.environ.get("AUTOMATION_RESOURCE_LOCK_HELD") or ""
    return {canonical_resource(x) for x in raw.split(",") if x.strip()}


def env_with_held_resource(env: dict[str, str] | None, resource: str) -> dict[str, str]:
    out = dict(os.environ if env is None else env)
    held = {x for x in (out.get("AUTOMATION_RESOURCE_LOCK_HELD") or "").split(",") if x}
    held.add(canonical_resource(resource))
    out["AUTOMATION_RESOURCE_LOCK_HELD"] = ",".join(sorted(held))
    return out


class ResourceBusyError(RuntimeError):
    def __init__(self, resource: str, holder: str = ""):
        self.resource = canonical_resource(resource)
        self.holder = holder.strip()
        detail = f" ({self.holder})" if self.holder else ""
        super().__init__(f"resource busy: {self.resource}{detail}")


class ResourceLock:
    """An idempotently releasable flock handle."""

    def __init__(self, resource: str, *, owner: str = ""):
        self.resource = canonical_resource(resource)
        self.owner = owner or "automation-studio"
        self.handle = None
        self.inherited = self.resource in held_resources()

    @property
    def path(self) -> Path:
        return LOCK_DIR / f"{safe_name(self.resource)}.lock"

    def acquire(self, *, blocking: bool = False) -> "ResourceLock":
        if self.inherited:
            return self
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            handle.seek(0)
            holder = handle.read().strip()
            handle.close()
            raise ResourceBusyError(self.resource, holder) from exc
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} owner={self.owner} acquired_at={time.time()}\n")
        handle.flush()
        os.fsync(handle.fileno())
        self.handle = handle
        return self

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> "ResourceLock":
        return self.acquire(blocking=False)

    def __exit__(self, *_exc) -> None:
        self.release()


def acquire_resource(resource: str, *, owner: str = "", blocking: bool = False) -> ResourceLock:
    return ResourceLock(resource, owner=owner).acquire(blocking=blocking)
