#!/usr/bin/env python3
"""拡張 page-hook.js の読み込みと、アダプタ経由の取得往復。SUNO へは接続しない。"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suno_fast_dl as fd  # noqa: E402

SONG_ID = "0ebfc8fd-2b5a-4c28-8aca-914d54b7ef15"
ORIGIN = "https://suno.test"

_MAKE_CIPHER = """async () => {
  const key = await crypto.subtle.generateKey({name:'AES-CTR', length:128}, false, ['encrypt','decrypt']);
  const counter = new Uint8Array(16);
  const plain = new Uint8Array(4096);
  for (let i = 0; i < plain.length; i += 1) plain[i] = i % 251;
  plain.set([0,0,0,32,0x66,0x74,0x79,0x70], 0);
  plain.set([0x6d,0x6f,0x6f,0x76], 36);
  const cipher = new Uint8Array(await crypto.subtle.encrypt({name:'AES-CTR', counter, length:128}, key, plain));
  window.__testKey = key; window.__testCounter = counter;
  return {plain: Array.from(plain), cipher: Array.from(cipher)};
}"""

_PLAY = """async (url) => {
  const response = await fetch(url);
  const buffer = await response.arrayBuffer();
  await crypto.subtle.decrypt({name:'AES-CTR', counter: window.__testCounter, length:128},
                              window.__testKey, buffer.slice(0, 1024));
}"""


class LoadHookTests(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("STUDIO_DRIVE_BASE", "APP_DRIVE_BASE", "APP_SUNO_HOOK_PATH"):
            os.environ.pop(key, None)

    def test_default_hook_is_loaded_verbatim(self):
        hook = fd.load_page_hook()
        raw = fd.DEFAULT_HOOK_PATH.read_bytes()
        self.assertEqual(hook["source"], raw.decode("utf-8"))
        self.assertEqual(hook["sha256"], hashlib.sha256(raw).hexdigest())
        self.assertEqual(hook["path"], str(fd.DEFAULT_HOOK_PATH))
        self.assertEqual(hook["message_source"], "suno-fast-decrypt-v092")

    def test_script_contains_unmodified_hook_then_adapter(self):
        hook = fd.load_page_hook()
        script = fd.build_capture_script()
        self.assertTrue(script.startswith(hook["source"]))
        self.assertIn("__sunoFastAdapterInstalled", script[len(hook["source"]):])

    def test_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            alt = Path(tmp) / "page-hook.js"
            alt.write_text("const SOURCE = 'alt-source';\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"APP_SUNO_HOOK_PATH": str(alt)}):
                hook = fd.load_page_hook()
        self.assertEqual(hook["message_source"], "alt-source")

    def test_drive_base_hook_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive_base = Path(tmp) / "_claude"
            drive_base.mkdir()
            hook_path = Path(tmp) / "Script" / "suno_fast_m4a_monomono" / "page-hook.js"
            hook_path.parent.mkdir(parents=True)
            hook_path.write_text("const SOURCE = 'drive-source';", encoding="utf-8")
            for env_values in (
                {"STUDIO_DRIVE_BASE": str(drive_base), "APP_DRIVE_BASE": "/nonexistent/_claude"},
                {"APP_DRIVE_BASE": str(drive_base)},
            ):
                with self.subTest(env_values=env_values), mock.patch.dict(os.environ, env_values):
                    hook = fd.load_page_hook()
                    self.assertEqual(hook["message_source"], "drive-source")
                    self.assertEqual(hook["path"], str(hook_path))

    def test_missing_default_candidates_are_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            drive_base = Path(tmp) / "missing" / "_claude"
            drive_hook = drive_base.parent / "Script" / "suno_fast_m4a_monomono" / "page-hook.js"
            default_hook = Path(tmp) / "missing-default.js"
            with mock.patch.dict(os.environ, {"STUDIO_DRIVE_BASE": str(drive_base)}), \
                    mock.patch.object(fd, "DEFAULT_HOOK_PATH", default_hook):
                with self.assertRaises(fd.HookLoadError) as caught:
                    fd.load_page_hook()
            self.assertIn(str(drive_hook), str(caught.exception))
            self.assertIn(str(default_hook), str(caught.exception))
            self.assertIn("APP_SUNO_HOOK_PATH", str(caught.exception))

    def test_missing_drive_hook_falls_back_to_default(self):
        with mock.patch.dict(os.environ, {"STUDIO_DRIVE_BASE": "/nonexistent/_claude"}):
            self.assertEqual(fd.load_page_hook()["path"], str(fd.DEFAULT_HOOK_PATH))

    def test_path_argument_takes_priority_over_env_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            hook_path = Path(tmp) / "page-hook.js"
            hook_path.write_text("const SOURCE = 'path-source';", encoding="utf-8")
            with mock.patch.dict(os.environ, {"APP_SUNO_HOOK_PATH": "/nonexistent/page-hook.js"}):
                hook = fd.load_page_hook(hook_path)
            self.assertEqual(hook["message_source"], "path-source")
            self.assertEqual(hook["path"], str(hook_path))

    def test_missing_env_override_does_not_fall_back(self):
        with mock.patch.dict(os.environ, {"APP_SUNO_HOOK_PATH": "/nonexistent/page-hook.js"}):
            with self.assertRaises(fd.HookLoadError) as caught:
                fd.load_page_hook()
        self.assertIn("/nonexistent/page-hook.js", str(caught.exception))

    def test_missing_file_fails_before_download(self):
        with self.assertRaises(fd.HookLoadError) as caught:
            fd.load_page_hook("/nonexistent/page-hook.js")
        self.assertIn("/nonexistent/page-hook.js", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, OSError)

    def test_invalid_utf8_raises_hook_load_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "page-hook.js"
            broken.write_bytes(b"\xff\xfe const SOURCE = 'x';")
            with self.assertRaises(fd.HookLoadError) as caught:
                fd.load_page_hook(broken)
            self.assertIn(str(broken), str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)

    def test_hook_without_source_constant_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            broken = Path(tmp) / "page-hook.js"
            broken.write_text("(function(){ /* SOURCE renamed */ })();", encoding="utf-8")
            with self.assertRaises(fd.HookLoadError) as caught:
                fd.load_page_hook(str(broken))
        self.assertIn("SOURCE", str(caught.exception))


class AdapterRoundTripTests(unittest.TestCase):
    """実フック + アダプタを同梱 Chromium へ注入し、暗号化レスポンスの取得を往復させる。"""

    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:
            raise unittest.SkipTest("Playwright Chromium を起動できません: %s" % exc)

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def setUp(self):
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        for key in ("STUDIO_DRIVE_BASE", "APP_DRIVE_BASE", "APP_SUNO_HOOK_PATH"):
            os.environ.pop(key, None)
        self.context = self._browser.new_context()
        self.addCleanup(self.context.close)
        fd.install_fast_capture(self.context)
        self.body = {"cipher": b""}
        self.context.route(ORIGIN + "/", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html><body>test</body></html>"))
        self.context.route("**/clip/*.m4a", lambda route: route.fulfill(
            status=200, content_type="audio/mp4", body=self.body["cipher"]))
        self.page = self.context.new_page()
        self.page.goto(ORIGIN + "/")
        made = self.page.evaluate(_MAKE_CIPHER)
        self.plain = bytes(made["plain"])
        self.body["cipher"] = bytes(made["cipher"])

    def _wait_state(self, session_id, wanted, timeout_ms=5000):
        waited = 0
        while waited <= timeout_ms:
            state = self.page.evaluate("(id) => window.__sunoFastGetState(id)", session_id)
            if state["status"] == wanted:
                return state
            self.page.wait_for_timeout(100)
            waited += 100
        self.fail("status=%s にならない: %r" % (wanted, state))

    def test_capture_round_trip_returns_full_plaintext(self):
        self.assertTrue(fd.ensure_fast_capture(self.page))
        self.page.evaluate("(c) => window.__sunoFastSetContext(c)",
                           {"sessionId": "s1", "songId": SONG_ID, "title": "t"})
        self.page.evaluate(_PLAY, "%s/x/clip/%s.m4a" % (ORIGIN, SONG_ID))
        state = self._wait_state("s1", "done")
        self.assertEqual(state["clipId"], SONG_ID)
        self.assertEqual(state["size"], len(self.plain))
        self.assertEqual(fd.fetch_result_bytes(self.page, "s1"), self.plain)

    def test_other_session_stays_pending(self):
        self.page.evaluate("(c) => window.__sunoFastSetContext(c)",
                           {"sessionId": "s1", "songId": SONG_ID, "title": "t"})
        self.page.evaluate(_PLAY, "%s/x/clip/%s.m4a" % (ORIGIN, SONG_ID))
        self._wait_state("s1", "done")
        other = self.page.evaluate("(id) => window.__sunoFastGetState(id)", "s2")
        self.assertEqual(other["status"], "pending")

    def test_consecutive_contexts_resolve_without_timeout(self):
        elapsed_ms = self.page.evaluate("""async (songId) => {
          const started = performance.now();
          window.__sunoFastSetContext({sessionId:'A', songId}).catch(() => {});
          const second = window.__sunoFastSetContext({sessionId:'B', songId});
          await second;
          return performance.now() - started;
        }""", SONG_ID)
        self.assertLess(elapsed_ms, 1000)

    def test_result_arriving_after_clear_is_dropped(self):
        self.page.evaluate("(c) => window.__sunoFastSetContext(c)",
                           {"sessionId": "s1", "songId": SONG_ID, "title": "t"})
        self.page.evaluate("(id) => window.__sunoFastClear(id)", "s1")
        self.page.evaluate(
            """(source) => window.postMessage({source, kind:'fast-export-result',
                 context:{sessionId:'s1'}, url:'', blob:new Blob([new Uint8Array(8)])}, '*')""",
            fd.load_page_hook()["message_source"])
        self.page.wait_for_timeout(300)
        state = self.page.evaluate("(id) => window.__sunoFastGetState(id)", "s1")
        self.assertEqual(state["status"], "pending")

    def test_no_capture_when_context_is_cleared(self):
        self.page.evaluate("(c) => window.__sunoFastSetContext(c)",
                           {"sessionId": "s1", "songId": SONG_ID, "title": "t"})
        self.page.evaluate("() => window.__sunoFastSetContext(null)")
        self.page.evaluate(_PLAY, "%s/x/clip/%s.m4a" % (ORIGIN, SONG_ID))
        self.page.wait_for_timeout(500)
        state = self.page.evaluate("(id) => window.__sunoFastGetState(id)", "s1")
        self.assertEqual(state["status"], "pending")

    def test_late_injection_into_existing_page(self):
        bare = self._browser.new_context()
        self.addCleanup(bare.close)
        bare.route(ORIGIN + "/", lambda route: route.fulfill(
            status=200, content_type="text/html", body="<html></html>"))
        page = bare.new_page()
        page.goto(ORIGIN + "/")
        self.assertTrue(fd.ensure_fast_capture(page))
        self.assertTrue(page.evaluate(
            "() => !!window.__sunoFastDecryptCaptureInstalled && !!window.__sunoFastAdapterInstalled"))


if __name__ == "__main__":
    unittest.main()
