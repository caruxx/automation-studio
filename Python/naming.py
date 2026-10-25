#!/usr/bin/env python3
"""成果物とフォルダの命名規則（単一正本）。

全て純粋関数。ファイルシステムや設定には触れない。
- vol フォルダ: `{vol}_{prefix}_{yymmdd}`（例 `12_EDM_261031`）。prefix は `_` を含まない。
- 画像: `vol{N}.png` / `vol{N}.jpg`（サブ画像は `vol{N}-1.*`）。
- 動画: `{prefix}_vol{N}.mp4`。
- 楽曲: 採用曲は `music/{曲名}.mp3`。曲名は末尾のテイク番号 `(1)` / `（２）` を除き、ファイル名に使えない文字を落とす。
  同じ曲名が重なる場合は ` (2)` のような採番ではなく、曲名の後ろに ` -2` を付ける（テイク番号と区別する）。
"""
from __future__ import annotations

import datetime as _dt
import re
import unicodedata
from pathlib import Path
from typing import Iterable, NamedTuple, Optional

from app_track_title import public_track_title

_VOL_FOLDER_RE = re.compile(r"^(?P<vol>\d+)_(?P<prefix>[^_]+)(?:_(?P<date>\d{6}))?$")
_VOL_PREFIX_RE = re.compile(r"^\d+_([^_]+)")  # 旧コード互換: 先頭の vol と prefix だけを読む
_TRACK_NUMBER_RE = re.compile(r"^\s*(?:track\s*)?\d{1,3}\s*[-_. )]+\s*", re.I)
_UNSAFE_CHARS_RE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class VolFolder(NamedTuple):
    vol: int
    prefix: str
    date: Optional[_dt.date]


def parse_vol_folder(name: str) -> Optional[VolFolder]:
    """`12_EDM_261031` → VolFolder(12, "EDM", 2026-10-31)。

    日付の無い旧形式も vol と prefix は読む。日付部が不正（存在しない日付）なら date だけ None にする
    （folder_prefix と同じく vol / prefix は読める）。"""
    m = _VOL_FOLDER_RE.match(Path(str(name or "")).name)
    if not m:
        return None
    date = None
    if m.group("date"):
        try:
            date = _dt.datetime.strptime(m.group("date"), "%y%m%d").date()
        except ValueError:
            date = None
    return VolFolder(int(m.group("vol")), m.group("prefix"), date)


def build_vol_folder(vol: int, prefix: str, date: _dt.date) -> str:
    if int(vol) < 0:
        raise ValueError("vol は 0 以上")
    p = str(prefix or "").strip()
    if not p or "_" in p or _UNSAFE_CHARS_RE.search(p):
        raise ValueError("prefix に _ やファイル名に使えない文字は使えません")
    return f"{int(vol)}_{p}_{date.strftime('%y%m%d')}"


def folder_prefix(name: str, default: str = "vol") -> str:
    """旧コード互換: フォルダ名の先頭 `{vol}_{prefix}` から prefix を取る。無ければ default。"""
    m = _VOL_PREFIX_RE.match(Path(str(name or "")).name)
    return m.group(1) if m else default


def folder_publish_date(name: str) -> Optional[_dt.date]:
    parsed = parse_vol_folder(name)
    return parsed.date if parsed else None


def image_name(vol: int, ext: str = "png", sub: Optional[int] = None) -> str:
    base = f"vol{int(vol)}" + (f"-{int(sub)}" if sub is not None else "")
    return f"{base}.{ext.lstrip('.')}"


def video_name(prefix: str, vol) -> str:
    """`{prefix}_vol{N}.mp4`。vol は呼び出し元の表記（文字列ならそのまま）を保つ。"""
    return f"{prefix}_vol{vol}.mp4"


def suno_workspace(channel_key: str, vol: int) -> str:
    return f"{channel_key}_vol{int(vol)}"


def public_title(title: str) -> str:
    """公開用の曲名（末尾のテイク番号だけを隠す）。"""
    return public_track_title(title)


def title_from_filename(filename: str, *, strip_z: bool = True, use_stem: bool = False) -> str:
    """`03 - Silver Avenue(1).mp3` → `Silver Avenue`。

    strip_z: 先頭の `z_` / `zz_`（旧ソート用接頭辞）を除く（ffrender の旧挙動）。timeline の表示名は除かない。
    use_stem: Path().stem でファイル名部分だけを取る（timeline の旧挙動）。False なら拡張子だけを落とす
    （ffrender の旧挙動。`AC/DC` のような `/` を含む曲名も壊さない）。"""
    raw = str(filename or "")
    stem = Path(raw).stem if use_stem else re.sub(r"\.[^.]+$", "", raw)
    if strip_z:
        stem = re.sub(r"^z+_", "", stem)
    stripped = _TRACK_NUMBER_RE.sub("", stem).strip()
    return public_title(stripped or stem)


def safe_filename_stem(title: str) -> str:
    """ファイル名に使えない文字を落とし、先頭のドットと末尾の空白・ドットを除く。NFC に正規化。"""
    stem = unicodedata.normalize("NFC", _UNSAFE_CHARS_RE.sub("", str(title or "")))
    return stem.strip().lstrip(".").strip().rstrip(" .")


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", str(name)).casefold()


def music_filename(title: str, taken: Iterable[str] = ()) -> str:
    """採用曲のファイル名。テイク番号を除き、安全化し、同名があれば ` -2`, ` -3` を付ける。

    重複判定は NFC + casefold（macOS の一覧が NFD で返っても取りこぼさない）。"""
    stem = safe_filename_stem(public_title(title))
    if not stem:
        raise ValueError("曲名からファイル名を作れません")
    used = {_name_key(t) for t in taken}
    name = f"{stem}.mp3"
    n = 2
    while _name_key(name) in used:
        name = f"{stem} -{n}.mp3"
        n += 1
    return name
