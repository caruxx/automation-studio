"""production_mode の一本化（常に ffmpeg_only）と旧設定値の警告を検証する。"""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_core


class EffectiveProductionModeTest(unittest.TestCase):
    def setUp(self):
        app_core._LEGACY_PRODUCTION_WARNED = False

    def _call(self, cfg):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mode = app_core.effective_production_mode(cfg)
        return mode, buf.getvalue()

    def test_empty_config_is_ffmpeg_only_without_warning(self):
        mode, out = self._call({})
        self.assertEqual(mode, "ffmpeg_only")
        self.assertNotIn("WARN", out)

    def test_legacy_standard_is_converted_with_single_warning(self):
        mode, out = self._call({"production_mode": "standard"})
        self.assertEqual(mode, "ffmpeg_only")
        self.assertIn("WARN legacy production setting ignored", out)
        self.assertIn("production_mode=standard", out)
        _, out2 = self._call({"production_mode": "standard"})
        self.assertNotIn("WARN", out2)

    def test_legacy_ame_engine_is_converted_with_warning(self):
        mode, out = self._call({"export_engine": "ame"})
        self.assertEqual(mode, "ffmpeg_only")
        self.assertIn("export_engine=ame", out)

    def test_ffmpeg_only_has_no_warning(self):
        mode, out = self._call({"production_mode": "ffmpeg_only", "export_engine": "ffmpeg"})
        self.assertEqual(mode, "ffmpeg_only")
        self.assertEqual(out, "")

    def test_dashboard_config_forces_ffmpeg_for_legacy_channel(self):
        legacy = {"production_mode": "standard", "export_engine": "ame"}
        with patch.object(app_core, "load_json", return_value={}), \
             patch.object(app_core, "load_channel_config", return_value=legacy):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                cfg = app_core.get_dashboard_config()
        self.assertEqual(cfg["production_mode"], "ffmpeg_only")
        self.assertEqual(cfg["export_engine"], "ffmpeg")


if __name__ == "__main__":
    unittest.main()
