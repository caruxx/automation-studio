#!/usr/bin/env python3
"""AI 選曲（select 工程）の CLI。`python3 app_music_select.py <vol フォルダ> [--dry-run] [--json]`。

チャンネル設定 music_selection（mode / policy / prompt）を読み、ハートの無いタイトル群だけ採点して採用する。
終了コード: 0 採用済み（ready） / 79 選定待ち・品質待機 / 1 失敗。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from exit_codes import ExitCode  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder", help="vol フォルダ（必須。暗黙の既定フォルダは使わない）")
    ap.add_argument("--dry-run", action="store_true", help="採点だけ行い、採用とスコア保存をしない")
    ap.add_argument("--json", action="store_true", help="結果を JSON 1 行で出す")
    args = ap.parse_args()
    folder = Path(args.folder).expanduser()
    if not folder.is_dir():
        print(json.dumps({"ok": False, "error": "vol フォルダがありません", "folder": str(folder)}, ensure_ascii=False))
        return int(ExitCode.USAGE)
    from app_music_catalog import SelectionPending, load_music_selection_config, select_takes_ai
    cfg = load_music_selection_config(folder)
    if cfg["mode"] != "ai":
        out = {"ok": False, "mode": cfg["mode"], "reason": "music_selection.mode が human のため AI 選曲しません"}
        print(json.dumps(out, ensure_ascii=False))
        return int(ExitCode.SELECTION_PENDING)
    try:
        r = select_takes_ai(folder, policy=cfg["policy"], prompt=cfg["prompt"], dry_run=args.dry_run)
    except SelectionPending as exc:
        print(json.dumps({"ok": False, "status": "blocked", "reason": "selection_pending", "message": str(exc)}, ensure_ascii=False))
        return int(ExitCode.SELECTION_PENDING)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return int(ExitCode.FAIL)
    out = {"ok": bool(r.get("ready")) or args.dry_run, "status": "done" if r.get("ready") else "blocked", **r}
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0 if (r.get("ready") or args.dry_run) else int(ExitCode.SELECTION_PENDING)


if __name__ == "__main__":
    sys.exit(main())
