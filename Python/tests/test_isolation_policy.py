"""tests/_isolation の方針: 一時領域の外にある設定パスを信用せず、本物の設定へ書き込まない。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PY_DIR = Path(__file__).resolve().parents[1]
PROBE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
import tests
print(json.dumps({k: os.environ.get(k) for k in ("APP_CONFIG_DIR", "STUDIO_CONFIG_DIR", "APP_SHARED_BASE", "HOME", "ORIGINAL_HOME", "AS_TESTS_ISOLATED_ROOT")}))
"""


def _outside_temp_dir() -> Path:
    """一時領域（gettempdir）の外にあるディレクトリを、元の HOME 配下に作る（本番設定の置き場所を模す）。"""
    base = Path(os.environ.get("ORIGINAL_HOME") or Path.home())
    d = Path(tempfile.mkdtemp(prefix=".as-isolation-probe-", dir=base))
    if d.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        import shutil
        shutil.rmtree(d, ignore_errors=True)
        raise unittest.SkipTest("HOME が一時領域の中にあるため、領域外のディレクトリを用意できません")
    return d


def _probe(env_overrides: dict) -> dict:
    env = {k: v for k, v in os.environ.items()
           if k not in ("APP_CONFIG_DIR", "STUDIO_CONFIG_DIR", "APP_SHARED_BASE", "AS_TESTS_ISOLATED_ROOT", "ORIGINAL_HOME")}
    env.update(env_overrides)
    out = subprocess.run([sys.executable, "-c", PROBE, str(PY_DIR)], env=env, capture_output=True, text=True, check=True)
    return json.loads(out.stdout.strip().splitlines()[-1])


class IsolationPolicyTest(unittest.TestCase):
    def test_partial_env_outside_temp_is_overridden_and_untouched(self):
        """本番 launchd の STUDIO_CONFIG_DIR が残っていても、上書きされ、そこには何も書かれない。"""
        import shutil
        outside = _outside_temp_dir()
        try:
            home = str(outside.parent)
            before = sorted(p.name for p in outside.iterdir())
            got = _probe({"STUDIO_CONFIG_DIR": str(outside), "APP_SHARED_BASE": str(outside), "HOME": home})
            self.assertNotEqual(got["STUDIO_CONFIG_DIR"], str(outside))
            tmp = Path(tempfile.gettempdir()).resolve()
            for k in ("APP_CONFIG_DIR", "STUDIO_CONFIG_DIR", "APP_SHARED_BASE", "HOME"):
                self.assertTrue(Path(got[k]).resolve().is_relative_to(tmp), msg=f"{k}={got[k]}")
            self.assertEqual(got["ORIGINAL_HOME"], home)
            self.assertEqual(sorted(p.name for p in outside.iterdir()), before)
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_full_env_outside_temp_is_overridden(self):
        """3 つ全部が本番相当（一時領域の外）でも信用せず、上書きし、目印ファイルも書かない。"""
        import shutil
        prod = _outside_temp_dir()
        try:
            got = _probe({"APP_CONFIG_DIR": str(prod), "STUDIO_CONFIG_DIR": str(prod), "APP_SHARED_BASE": str(prod)})
            self.assertNotEqual(got["APP_CONFIG_DIR"], str(prod))
            self.assertFalse((prod / ".migration_completed").exists())
            self.assertEqual(list(prod.iterdir()), [])
        finally:
            shutil.rmtree(prod, ignore_errors=True)

    def test_full_env_inside_temp_is_kept_and_home_isolated(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / "cfg"
            cfg.mkdir()
            got = _probe({"APP_CONFIG_DIR": str(cfg), "STUDIO_CONFIG_DIR": str(cfg), "APP_SHARED_BASE": str(cfg)})
            self.assertEqual(got["APP_CONFIG_DIR"], str(cfg))
            self.assertTrue((cfg / ".migration_completed").exists())
            self.assertNotEqual(got["HOME"], os.environ.get("ORIGINAL_HOME") or os.environ["HOME"])
            self.assertTrue(Path(got["HOME"]).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))

    def test_stale_mark_is_not_trusted(self):
        got = _probe({"AS_TESTS_ISOLATED_ROOT": "/nonexistent/as-tests-old"})
        self.assertNotEqual(got["AS_TESTS_ISOLATED_ROOT"], "/nonexistent/as-tests-old")
        self.assertIsNotNone(got["APP_CONFIG_DIR"])

    def test_every_test_module_imports_isolation_first(self):
        for f in sorted((PY_DIR / "tests").glob("test_*.py")):
            head = f.read_text(encoding="utf-8")
            idx = head.find("_isolation")
            self.assertGreater(idx, -1, msg=f"{f.name} が _isolation を import していません")
            first_app_import = min([i for i in (head.find("import app"), head.find("from app")) if i != -1], default=len(head))
            self.assertLess(idx, first_app_import, msg=f"{f.name}: _isolation の import が app 系の import より後です")

    def test_app_core_before_isolation_raises(self):
        code = "import sys; sys.path.insert(0, sys.argv[1]); sys.modules['app_core'] = object(); import tests"
        env = {k: v for k, v in os.environ.items() if k not in ("AS_TESTS_ISOLATED_ROOT",)}
        out = subprocess.run([sys.executable, "-c", code, str(PY_DIR)], env=env, capture_output=True, text=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("隔離より先に import", out.stderr)


if __name__ == "__main__":
    unittest.main()
