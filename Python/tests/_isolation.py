"""テストの自己隔離（本体）。

tests/__init__.py（パッケージとして読まれた場合）と各テストモジュール冒頭の
`import _isolation`（`discover tests` で top-level として読まれた場合）の両方から呼ばれ、冪等に動く。

方針: 一時領域（tempfile.gettempdir() 配下）の外にある設定パスは一切信用しない。
- APP_CONFIG_DIR / STUDIO_CONFIG_DIR / APP_SHARED_BASE の 3 つが全て一時領域内なら、その指定を使う。
- 1 つでも未指定、または一時領域の外（本番 launchd の値が残っている等）なら、3 つとも一時ディレクトリで上書きする。
- HOME は常に一時ディレクトリへ差し替え、元の値を ORIGINAL_HOME に記録する（Path.home() / expanduser が本物の ~/.config を見ない）。
- .migration_completed は一時領域内にだけ書く。
- 本体（app_core）が隔離より先に import されていたら例外で止める（黙って実設定で走らせない）。
"""
from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path

ISOLATED_KEYS = ("APP_CONFIG_DIR", "STUDIO_CONFIG_DIR", "APP_SHARED_BASE")
_MARK = "AS_TESTS_ISOLATED_ROOT"


def _within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _temp_root() -> Path:
    return Path(tempfile.gettempdir()).resolve()


def _env_is_isolated() -> bool:
    """3 キーが全て指定され、全て一時領域内か。"""
    return all(os.environ.get(k) and _within(Path(os.environ[k]), _temp_root()) for k in ISOLATED_KEYS)


def _bootstrap() -> Path:
    mark = os.environ.get(_MARK)
    if mark and Path(mark).is_dir() and _env_is_isolated() \
            and all(_within(Path(os.environ[k]), Path(mark)) for k in ISOLATED_KEYS) \
            and _within(Path(os.environ.get("HOME", "")), Path(mark)):
        return Path(mark)  # 既に隔離済み（冪等）。残骸の変数だけでは信用しない
    if "app_core" in sys.modules:
        raise RuntimeError(
            "tests/_isolation: app_core が隔離より先に import されています。"
            "テストモジュールの冒頭で _isolation を import するか、python Python/scripts/run_tests.py で実行してください。")

    root = Path(tempfile.mkdtemp(prefix="as-tests-")).resolve()
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    if _env_is_isolated():
        cfg = Path(os.environ["APP_CONFIG_DIR"])
    else:
        cfg = root / "config"
        shared = root / "shared"
        drive = root / "drive"
        for d in (cfg, shared, drive):
            d.mkdir(parents=True, exist_ok=True)
        os.environ["APP_CONFIG_DIR"] = str(cfg)
        os.environ["STUDIO_CONFIG_DIR"] = str(shared)
        os.environ["APP_SHARED_BASE"] = str(drive)
    try:
        cfg.mkdir(parents=True, exist_ok=True)
        marker = cfg / ".migration_completed"
        if _within(cfg, _temp_root()) and not marker.exists():
            marker.write_text("test", encoding="utf-8")  # 旧設定（~/.config/orzz）の移行コピーを止める
    except OSError:
        pass
    os.environ["ORIGINAL_HOME"] = os.environ.get("HOME", "")
    os.environ["HOME"] = str(home)
    os.environ.setdefault("APP_ORCH_TICK_ENABLED", "0")
    os.environ.setdefault("APP_AUTO_UPDATE_ENABLED", "0")
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    os.environ[_MARK] = str(root)
    atexit.register(shutil.rmtree, root, True)
    return root


TEST_ROOT = _bootstrap()
