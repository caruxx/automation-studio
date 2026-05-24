"""Deprecated alias: use ``app_sheets.py`` instead.

This shim will be removed in a future release.
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
warnings.warn(
    "orzz_spreadsheet_fetcher is deprecated; use app_sheets.",
    DeprecationWarning, stacklevel=2,
)

from app_sheets import *  # noqa: F401, F403

if __name__ == "__main__":
    # メインがあれば呼ぶ
    try:
        from app_sheets import main
        main()
    except ImportError:
        pass
