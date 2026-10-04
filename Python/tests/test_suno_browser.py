#!/usr/bin/env python3
"""suno_browser の単体テスト。実ブラウザは起動しない。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suno_browser as sb  # noqa: E402


class FakeContext:
    def __init__(self, pages=None):
        self.pages = list(pages or [])
        self.closed = False
        self.new_pages = 0

    def new_page(self):
        self.new_pages += 1
        return "new-page-%d" % self.new_pages

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self, contexts):
        self.contexts = contexts
        self.closed = False

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, fail=None, cdp_contexts=None, cdp_fail=None):
        self.fail = fail
        self.cdp_contexts = cdp_contexts
        self.cdp_fail = cdp_fail
        self.launch_calls = []
        self.cdp_calls = []
        self.context = FakeContext(pages=["existing-page"])

    def launch_persistent_context(self, **kwargs):
        self.launch_calls.append(kwargs)
        if self.fail:
            raise RuntimeError(self.fail)
        return self.context

    def connect_over_cdp(self, endpoint, **kwargs):
        self.cdp_calls.append(endpoint)
        if self.cdp_fail:
            raise RuntimeError(self.cdp_fail)
        return FakeBrowser(self.cdp_contexts)


class FakePlaywright:
    def __init__(self, chromium):
        self.chromium = chromium


class ResolveTests(unittest.TestCase):
    def test_defaults(self):
        got = sb.resolve_browser_settings({})
        self.assertEqual(got["mode"], "chrome")
        self.assertEqual(got["profile_dir"], str(sb.default_profile_dir("chrome")))
        self.assertEqual(got["cdp_port"], 9222)
        self.assertFalse(got["headless"])

    def test_stale_config_values_fall_back(self):
        got = sb.resolve_browser_settings(
            {"browser_mode": "", "browser_profile_dir": None, "cdp_port": "9222", "headless": 1})
        self.assertEqual(got["mode"], "chrome")
        self.assertEqual(got["cdp_port"], 9222)
        self.assertTrue(got["headless"])

    def test_chromium_default_profile(self):
        got = sb.resolve_browser_settings({"browser_mode": "chromium"})
        self.assertEqual(got["profile_dir"], str(sb.default_profile_dir("chromium")))

    def test_invalid_mode(self):
        with self.assertRaises(sb.BrowserConfigError):
            sb.resolve_browser_settings({"browser_mode": "firefox"})

    def test_port_range(self):
        for bad in (80, 1023, 65536, "abc", 0):
            with self.assertRaises(sb.BrowserConfigError, msg=repr(bad)):
                sb.resolve_browser_settings({"browser_mode": "cdp", "cdp_port": bad})

    def test_profile_sharing_rejected_in_any_spelling(self):
        other = sb.default_profile_dir("chromium")
        home_relative = "~/" + str(other.relative_to(Path.home())) + "/"
        for spelled in (str(other), home_relative):
            with self.assertRaises(sb.BrowserConfigError, msg=spelled):
                sb.resolve_browser_settings(
                    {"browser_mode": "chrome", "browser_profile_dir": spelled})

    def test_custom_profile_is_expanded(self):
        got = sb.resolve_browser_settings(
            {"browser_mode": "chrome", "browser_profile_dir": "~/suno-auto-profile/"})
        self.assertEqual(got["profile_dir"], str(Path.home() / "suno-auto-profile"))

    def test_cdp_ignores_profile_and_headless(self):
        got = sb.resolve_browser_settings(
            {"browser_mode": "cdp", "browser_profile_dir": "/x", "headless": True, "cdp_port": 9333})
        self.assertEqual((got["profile_dir"], got["headless"], got["cdp_port"]), ("", False, 9333))

    def test_cli_overrides_only_given_values(self):
        settings = {"browser_mode": "chromium", "cdp_port": 9222}
        sb.apply_cli_overrides(settings, mode="cdp", profile_dir=None, cdp_port=9444)
        self.assertEqual(settings, {"browser_mode": "cdp", "cdp_port": 9444})


class OpenTests(unittest.TestCase):
    def test_chrome_uses_channel_and_profile(self):
        chromium = FakeChromium()
        session = sb.open_suno_context(FakePlaywright(chromium), {"browser_mode": "chrome"})
        call = chromium.launch_calls[0]
        self.assertEqual(call["channel"], "chrome")
        self.assertEqual(call["user_data_dir"], str(sb.default_profile_dir("chrome")))
        self.assertIn("--disable-blink-features=AutomationControlled", call["args"])
        self.assertEqual(call["ignore_default_args"], ["--enable-automation"])
        self.assertTrue(call["accept_downloads"])
        self.assertEqual(call["viewport"], {"width": 1280, "height": 900})
        self.assertTrue(session.owned)
        self.assertEqual(session.page(), "existing-page")

    def test_chromium_has_no_channel(self):
        chromium = FakeChromium()
        sb.open_suno_context(FakePlaywright(chromium), {"browser_mode": "chromium"})
        self.assertNotIn("channel", chromium.launch_calls[0])

    def test_no_silent_fallback(self):
        chromium = FakeChromium(fail="chrome not found")
        with self.assertRaises(sb.BrowserLaunchError) as caught:
            sb.open_suno_context(FakePlaywright(chromium), {"browser_mode": "chrome"})
        self.assertEqual(len(chromium.launch_calls), 1)
        message = str(caught.exception)
        self.assertIn("chrome", message)
        self.assertIn("chrome not found", message)
        self.assertIn(str(sb.default_profile_dir("chrome")), message)

    def test_owned_close_closes_context(self):
        chromium = FakeChromium()
        session = sb.open_suno_context(FakePlaywright(chromium), {"browser_mode": "chromium"})
        session.close()
        self.assertTrue(chromium.context.closed)

    def test_cdp_connects_and_never_closes_or_reuses_tabs(self):
        context = FakeContext(pages=["users-own-tab"])
        chromium = FakeChromium(cdp_contexts=[context])
        session = sb.open_suno_context(
            FakePlaywright(chromium), {"browser_mode": "cdp", "cdp_port": 9333})
        self.assertEqual(chromium.cdp_calls, ["http://127.0.0.1:9333"])
        self.assertFalse(session.owned)
        self.assertEqual(session.page(), "new-page-1")
        session.close()
        self.assertFalse(context.closed)

    def test_cdp_unreachable_explains_how_to_start_chrome(self):
        chromium = FakeChromium(cdp_fail="ECONNREFUSED")
        with self.assertRaises(sb.BrowserLaunchError) as caught:
            sb.open_suno_context(FakePlaywright(chromium), {"browser_mode": "cdp"})
        self.assertIn("--remote-debugging-port=9222", str(caught.exception))
        self.assertIn("--user-data-dir", str(caught.exception))

    def test_cdp_without_context_fails(self):
        chromium = FakeChromium(cdp_contexts=[])
        with self.assertRaises(sb.BrowserLaunchError):
            sb.open_suno_context(FakePlaywright(chromium), {"browser_mode": "cdp"})


if __name__ == "__main__":
    unittest.main()
