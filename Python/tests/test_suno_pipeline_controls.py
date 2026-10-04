"""Pipeline SUNO control flags and browser-profile migration exclusions."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import _app_config
import app_pipeline as pipeline


class SunoPipelineControlTests(unittest.TestCase):
    def test_control_flags_keep_zero(self):
        config = {"weirdness": 0, "style_influence": 0, "variety": 0}
        self.assertEqual(pipeline._suno_control_cli_args(config.get), [
            "--weirdness", "0", "--style-influence", "0", "--variety", "0",
        ])

    def test_control_flags_omit_none_and_missing_values(self):
        self.assertEqual(pipeline._suno_control_cli_args({}.get), [])
        self.assertEqual(pipeline._suno_control_cli_args({
            "weirdness": None, "style_influence": 60, "variety": None,
        }.get), ["--style-influence", "60"])

    def test_control_flag_names(self):
        self.assertEqual(pipeline._suno_control_cli_args({
            "weirdness": 35, "style_influence": 75, "variety": 4,
        }.get), ["--weirdness", "35", "--style-influence", "75", "--variety", "4"])

    def test_direct_cli_resolves_controls_like_duration(self):
        channel = {"prompt": "test prompt", "duration_seconds": 240,
                   "weirdness": 0, "style_influence": None, "variety": 0}
        global_config = {"duration_seconds": 180, "weirdness": 50,
                         "style_influence": 60, "variety": 4}
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.dict(os.environ, {"APP_SUNO_AUTO_DOWNLOAD": "0"}, clear=True), \
             mock.patch.object(pipeline, "_load_channel_suno_config", return_value=channel), \
             mock.patch.object(pipeline, "_load_suno_config", return_value=global_config), \
             mock.patch.object(pipeline, "_load_dashboard_config", return_value={}), \
             mock.patch.object(pipeline, "_run", return_value=True) as run, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(pipeline.step_suno(1, Path(temp), False))
        command = run.call_args.args[0]
        for flag, value in (("--duration-seconds", "240"), ("--weirdness", "0"),
                            ("--style-influence", "60"), ("--variety", "0")):
            with self.subTest(flag=flag):
                self.assertIn(flag, command)
                self.assertEqual(command[command.index(flag) + 1], value)


class SunoProfileMigrationTests(unittest.TestCase):
    def test_browser_profiles_are_not_migrated(self):
        with tempfile.TemporaryDirectory() as temp:
            source, destination = Path(temp) / "old", Path(temp) / "new"
            source.mkdir()
            (source / "settings.json").write_text("{}", encoding="utf-8")
            for name in ("chrome_profile", "chromium_profile"):
                (source / name).mkdir()
                (source / name / "profile-data").write_text("profile", encoding="utf-8")
            with mock.patch.object(_app_config, "LEGACY_CONFIG_DIR", source), \
                 mock.patch.object(_app_config, "resolve_config_dir", return_value=destination):
                result = _app_config.migrate_legacy_if_needed()
            self.assertTrue(result["performed"])
            self.assertTrue((destination / "settings.json").is_file())
            self.assertFalse((destination / "chrome_profile").exists())
            self.assertFalse((destination / "chromium_profile").exists())


if __name__ == "__main__":
    unittest.main()
