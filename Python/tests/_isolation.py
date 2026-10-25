"""テストの自己隔離（本体）。

tests/__init__.py（パッケージとして読まれた場合）と各テストモジュール冒頭の
`import _isolation`（`discover tests` で top-level として読まれた場合）の両方から呼ばれ、冪等に動く。
ここで設定ディレクトリ・共有ベース・HOME を一時ディレクトリへ向け、.migration_completed を置いて
旧設定（~/.config/orzz）の移行コピーを止める。呼び出し側が環境変数を明示した場合はそれを尊重する。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

_ISOLATED_KEYS = ("APP_CONFIG_DIR", "STUDIO_CONFIG_DIR", "APP_SHARED_BASE")


_MARK = "AS_TESTS_ISOLATED_ROOT"


def _bootstrap() -> Path | None:
    if os.environ.get(_MARK):
        return Path(os.environ[_MARK])  # 既に隔離済み（冪等）
    if "app_core" in sys.modules:
        return None  # 既に本体が読まれている（env を変えても効かない）
    if all(os.environ.get(k) for k in _ISOLATED_KEYS):
        root = Path(os.environ["APP_CONFIG_DIR"])
        marker = root / ".migration_completed"
        try:
            root.mkdir(parents=True, exist_ok=True)
            if not marker.exists():
                marker.write_text("test", encoding="utf-8")
        except OSError:
            pass
        return None
    root = Path(tempfile.mkdtemp(prefix="as-tests-"))
    cfg = root / "config"
    shared = root / "shared"
    home = root / "home"
    for d in (cfg, shared, home, root / "drive"):
        d.mkdir(parents=True, exist_ok=True)
    (cfg / ".migration_completed").write_text("test", encoding="utf-8")
    os.environ.setdefault("APP_CONFIG_DIR", str(cfg))
    os.environ.setdefault("STUDIO_CONFIG_DIR", str(shared))
    os.environ.setdefault("APP_SHARED_BASE", str(root / "drive"))
    os.environ.setdefault("ORIGINAL_HOME", os.environ.get("HOME", ""))
    os.environ["HOME"] = str(home)  # Path.home() / expanduser が本物の ~/.config を見ないようにする
    os.environ.setdefault("APP_ORCH_TICK_ENABLED", "0")
    os.environ.setdefault("APP_AUTO_UPDATE_ENABLED", "0")
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    os.environ[_MARK] = str(root)
    atexit.register(shutil.rmtree, root, True)
    return root


TEST_ROOT = _bootstrap()
