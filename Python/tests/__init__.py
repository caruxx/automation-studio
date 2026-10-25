"""テストパッケージ。import 時に _isolation で設定・共有ベース・HOME を一時ディレクトリへ隔離する。"""
from . import _isolation  # noqa: F401

TEST_ROOT = _isolation.TEST_ROOT
