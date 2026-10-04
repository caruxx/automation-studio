#!/usr/bin/env python3
"""SUNO 数値設定のチャンネル保存・解除と CLI 引数への引き継ぎ。"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_core  # noqa: E402


class NumericConfigStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.global_path = Path(self.tmp.name) / "suno_config.json"
        self.global_path.write_text(
            json.dumps({"browser_mode": "chrome", "duration_seconds": 180}),
            encoding="utf-8",
        )
        self.global_before = self.global_path.read_bytes()
        self.channel = {}
        patches = [
            mock.patch.object(app_core, "SUNO_CONFIG", self.global_path),
            mock.patch.object(app_core, "load_channel_config", lambda: copy.deepcopy(self.channel)),
            mock.patch.object(app_core, "save_channel_config", self.channel.update),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_clear_removes_only_requested_channel_keys(self):
        self.channel.update({
            "suno": {"duration_seconds": 240, "weirdness": 0,
                     "style_influence": 75, "variety": 3, "prompt": "keep"},
            "channel_name": "keep channel",
        })

        app_core.clear_suno_channel_keys(["duration_seconds", "weirdness"])

        self.assertEqual(self.channel, {
            "suno": {"style_influence": 75, "variety": 3, "prompt": "keep"},
            "channel_name": "keep channel",
        })
        self.assertEqual(self.global_path.read_bytes(), self.global_before)

    def test_clear_missing_and_repeated_keys_preserves_other_values(self):
        self.channel["suno"] = {"weirdness": 0, "prompt": "keep"}

        app_core.clear_suno_channel_keys(["variety", "weirdness", "weirdness"])

        self.assertEqual(self.channel, {"suno": {"prompt": "keep"}})
        self.assertEqual(self.global_path.read_bytes(), self.global_before)

    def test_save_and_read_numeric_values_including_zero(self):
        app_core.save_suno_config_smart({"weirdness": 0, "variety": 3})

        self.assertEqual(self.channel, {"suno": {"weirdness": 0, "variety": 3}})
        loaded = app_core.get_suno_config()
        self.assertEqual(loaded["weirdness"], 0)
        self.assertEqual(loaded["variety"], 3)
        self.assertEqual(self.global_path.read_bytes(), self.global_before)


class NumericCliArgsTests(unittest.TestCase):
    def test_request_overrides_config_with_ordered_flags(self):
        request = {"variety": 1, "style_influence": 60,
                   "weirdness": 20, "duration_seconds": 240}
        config = {"duration_seconds": 180, "weirdness": 50,
                  "style_influence": 80, "variety": 4}

        self.assertEqual(app_core._suno_numeric_cli_args(request, config), [
            "--duration-seconds", "240", "--weirdness", "20",
            "--style-influence", "60", "--variety", "1",
        ])

    def test_none_or_missing_request_values_use_config(self):
        self.assertEqual(app_core._suno_numeric_cli_args(
            {"duration_seconds": None, "weirdness": None},
            {"duration_seconds": 210, "weirdness": 30,
             "style_influence": 70, "variety": 3},
        ), ["--duration-seconds", "210", "--weirdness", "30",
            "--style-influence", "70", "--variety", "3"])

    def test_zero_request_values_override_config(self):
        self.assertEqual(app_core._suno_numeric_cli_args(
            {"weirdness": 0, "style_influence": 0, "variety": 0},
            {"weirdness": 50, "style_influence": 50, "variety": 4},
        ), ["--weirdness", "0", "--style-influence", "0", "--variety", "0"])

    def test_zero_config_values_are_included(self):
        self.assertEqual(app_core._suno_numeric_cli_args(
            {"weirdness": None}, {"weirdness": 0, "style_influence": 0, "variety": 0},
        ), ["--weirdness", "0", "--style-influence", "0", "--variety", "0"])

    def test_unset_values_produce_no_flags(self):
        for request, config in [({}, {}),
                                ({"weirdness": None}, {"variety": None}),
                                ({"prompt": "ignore"}, {"provider": "ignore"})]:
            with self.subTest(request=request, config=config):
                self.assertEqual(app_core._suno_numeric_cli_args(request, config), [])

    def test_resolution_does_not_mutate_inputs(self):
        request = {"duration_seconds": 240, "weirdness": None}
        config = {"weirdness": 0, "variety": 2}
        request_before, config_before = dict(request), dict(config)

        app_core._suno_numeric_cli_args(request, config)

        self.assertEqual(request, request_before)
        self.assertEqual(config, config_before)


if __name__ == "__main__":
    unittest.main()
