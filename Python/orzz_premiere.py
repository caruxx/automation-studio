"""Deprecated alias: use ``app_premiere.py`` instead.

This shim will be removed in a future release.
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
warnings.warn(
    "orzz_premiere is deprecated; use app_premiere.",
    DeprecationWarning, stacklevel=2,
)

from app_premiere import *  # noqa: F401, F403

if __name__ == "__main__":
    # メインがあれば呼ぶ
    try:
        from app_premiere import main
        main()
    except ImportError:
        pass
