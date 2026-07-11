#!/usr/bin/env python3
"""Common file stability checks for Google Drive synced artifacts."""
from __future__ import annotations

import time
from pathlib import Path


def wait_for_file_stable(path: str | Path, *, checks: int = 2, interval: float = 3.0,
                         timeout: float = 60.0, min_size: int = 1) -> bool:
    """Return True after the file size is unchanged for `checks` consecutive reads."""
    p = Path(path)
    deadline = time.time() + timeout
    last_size = None
    stable = 0
    while time.time() <= deadline:
        try:
            size = p.stat().st_size
        except FileNotFoundError:
            size = -1
        if size >= min_size and size == last_size:
            stable += 1
            if stable >= checks:
                return True
        else:
            stable = 0
        last_size = size
        time.sleep(interval)
    return False
