#!/usr/bin/env python3
"""ブラウザ接続先がマシン別設定に保存され、チャンネル設定で上書きされないこと。"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_core  # noqa: E402

BROWSER_KEYS = ("browser_mode", "browser_profile_dir", "cdp_port")


class BrowserConfigStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.global_path = Path(self.tmp.name) / "suno_config.json"
        self.channel = {}
        patches = [
            mock.patch.object(app_core, "SUNO_CONFIG", self.global_path),
            mock.patch.object(app_core, "load_channel_config", lambda: dict(self.channel)),
            mock.patch.object(app_core, "save_channel_config", self.channel.update),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_browser_keys_are_saved_globally(self):
        app_core.save_suno_config_smart(
            {"browser_mode": "cdp", "browser_profile_dir": "", "cdp_port": 9333, "prompt": "p"})
        saved = json.loads(self.global_path.read_text(encoding="utf-8"))
        self.assertEqual({k: saved[k] for k in BROWSER_KEYS},
                         {"browser_mode": "cdp", "browser_profile_dir": "", "cdp_port": 9333})
        self.assertNotIn("prompt", saved)
        channel_suno = self.channel.get("suno") or {}
        for key in BROWSER_KEYS:
            self.assertNotIn(key, channel_suno)
        self.assertEqual(channel_suno.get("prompt"), "p")

    def test_channel_values_do_not_override_browser_keys(self):
        self.global_path.write_text(json.dumps({"browser_mode": "chrome"}), encoding="utf-8")
        self.channel["suno"] = {"browser_mode": "chromium", "cdp_port": 1, "prompt": "ch"}
        got = app_core.get_suno_config()
        self.assertEqual(got["browser_mode"], "chrome")
        self.assertNotEqual(got.get("cdp_port"), 1)
        self.assertEqual(got["prompt"], "ch")


if __name__ == "__main__":
    unittest.main()
