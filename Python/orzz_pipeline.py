"""Deprecated alias: use ``app_pipeline.py`` instead.

This shim will be removed in a future release. Existing scripts that call
``python3 orzz_pipeline.py 78`` continue to work for now, but please update
to ``python3 app_pipeline.py 78``.
"""
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
warnings.warn(
    "orzz_pipeline is deprecated; use app_pipeline.",
    DeprecationWarning, stacklevel=2,
)

from app_pipeline import *  # noqa: F401, F403
from app_pipeline import main  # explicit re-export

if __name__ == "__main__":
    main()
