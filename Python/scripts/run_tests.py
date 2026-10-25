#!/usr/bin/env python3
"""CI 用のテスト実行。skip を失敗扱いにし、結果を 1 行の JSON で出す。

使い方: python3 Python/scripts/run_tests.py [--allow-skip] [--pattern 'test_*.py']
隔離は Python/tests/__init__.py が行う（設定・共有ベース・HOME を一時ディレクトリへ）。
"""
from __future__ import annotations

import argparse
import contextlib
import json
import sys
import unittest
from pathlib import Path

PY_DIR = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-skip", action="store_true", help="skip があっても失敗にしない")
    ap.add_argument("--pattern", default="test_*.py")
    ap.add_argument("--verbosity", type=int, default=1)
    args = ap.parse_args()

    sys.path.insert(0, str(PY_DIR))
    import tests as _tests  # noqa: F401  隔離を先に効かせる（tests/__init__ → _isolation）

    suite = unittest.defaultTestLoader.discover(str(PY_DIR / "tests"), pattern=args.pattern, top_level_dir=str(PY_DIR))
    with contextlib.redirect_stdout(sys.stderr):  # アプリの print を stderr へ退避し、stdout は結果 JSON の 1 行だけにする
        result = unittest.TextTestRunner(verbosity=args.verbosity, stream=sys.stderr).run(suite)
    summary = {
        "ran": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "skipped": len(result.skipped),
        "skipped_reasons": [f"{t.id()}: {r}" for t, r in result.skipped],
        "isolated_root": str(getattr(_tests, "TEST_ROOT", "") or ""),
    }
    ok = result.wasSuccessful() and (args.allow_skip or not result.skipped) and result.testsRun > 0
    summary["ok"] = ok
    print(json.dumps(summary, ensure_ascii=False))
    if result.skipped and not args.allow_skip:
        print("skip があるため失敗扱いにします（--allow-skip で緩和）", file=sys.stderr)
    if result.testsRun == 0:
        print("テストが 1 件も実行されていません（--pattern を確認）", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
