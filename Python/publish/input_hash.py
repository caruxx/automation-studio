"""投稿の冪等キーに使う input_hash（段階 3）。

動画（サイズ + 先頭/末尾 4 MiB の sha256）、サムネ（内容）、公開メタ（title / description / tags / localizations の正規化 JSON）
から sha256 を作る。予約時刻（publishAt）は含めない（時刻を変えても同じ投稿）。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

_EDGE = 4 * 1024 * 1024


def file_digest(path: Path, *, edge_bytes: int = _EDGE) -> str:
    """大きな動画でも速い指紋: サイズ + 先頭と末尾 edge_bytes の sha256。小さなファイルは全体。"""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    h.update(str(size).encode("ascii"))
    with p.open("rb") as fh:
        if size <= edge_bytes * 2:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        else:
            h.update(fh.read(edge_bytes))
            fh.seek(size - edge_bytes)
            h.update(fh.read(edge_bytes))
    return h.hexdigest()


def meta_digest(title: str, description: str, tags: list[str] | None, localizations: dict[str, Any] | None) -> str:
    payload = {"title": (title or "").strip(), "description": (description or "").strip(),
               "tags": [str(t).strip() for t in (tags or []) if str(t).strip()],
               "localizations": localizations or {}}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def compute_input_hash(*, video_path: Path, thumbnail_path: Optional[Path], title: str, description: str,
                       tags: list[str] | None, localizations: dict[str, Any] | None) -> str:
    parts = [f"video:{file_digest(Path(video_path))}"]
    if thumbnail_path and Path(thumbnail_path).is_file():
        parts.append(f"thumb:{hashlib.sha256(Path(thumbnail_path).read_bytes()).hexdigest()}")
    parts.append(f"meta:{meta_digest(title, description, tags, localizations)}")
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()
