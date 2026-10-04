"""設定保存を実 HTML と同梱 Chromium で検証する。全通信はモック。"""
from __future__ import annotations

import json
import unittest
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://settings.test"
CONFIG = {
    "dashboard": {"channel_name": "Settings fixture", "app_id": "orzz",
                  "persona": "Saved persona", "rival_channels": ["Saved rival"]},
    "suno": {"provider": "claude", "weirdness": 20},
}


class SettingsAutosaveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch(headless=True)
        except Exception as exc:
            if getattr(cls, "_pw", None):
                cls._pw.stop()
            raise unittest.SkipTest("Playwright Chromium を起動できません: %s" % exc)

    @classmethod
    def tearDownClass(cls):
        cls._browser.close()
        cls._pw.stop()

    def setUp(self):
        self.context = self._browser.new_context(service_workers="block")
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.puts = []
        self.config_status = 200
        self.page.route("**/*", self._asset)
        self.page.route("**/api/**", self._api)

    def _asset(self, route):
        path = urlparse(route.request.url).path
        if path == "/":
            route.fulfill(content_type="text/html",
                          body=(ROOT / "web/static/index.html").read_text(encoding="utf-8"))
        elif path.startswith("/static/"):
            asset = ROOT / "web" / path.lstrip("/")
            if asset.is_file():
                route.fulfill(path=str(asset))
            else:
                route.fulfill(status=404, body="")
        else:
            route.abort()

    def _api(self, route):
        request = route.request
        path = urlparse(request.url).path
        status, body = 200, {}
        if request.method == "PUT":
            patch = request.post_data_json
            self.puts.append((path, patch))
            body = {"status": "ok", "config": {**CONFIG["dashboard"], **patch}}
        elif request.method == "GET" and path == "/api/config":
            status = self.config_status
            body = CONFIG if status == 200 else {"detail": "test load failure"}
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _load(self, fail=False):
        self.config_status = 500 if fail else 200
        self.page.goto(ORIGIN + "/", wait_until="load")
        self.page.wait_for_function("typeof _autoSaveWired !== 'undefined' && _autoSaveWired")
        if not fail:
            self.page.wait_for_function(
                "document.getElementById('cfgName').value === 'Settings fixture'")

    def _input_weirdness(self):
        self.page.locator("#cfgSunoWeirdness").evaluate("""el => {
            el.value = '37'; el.dispatchEvent(new Event('input', {bubbles:true}));
        }""")

    def _wait_autosave(self):
        with self.page.expect_request(
                lambda r: urlparse(r.url).path == "/api/config/suno" and r.method == "PUT",
                timeout=3000):
            self._input_weirdness()
        self.page.wait_for_function(
            "document.getElementById('autoSaveIndicator').textContent.includes('自動保存しました')")

    def test_input_autosaves_suno_after_config_load(self):
        self._load()
        self._wait_autosave()
        suno = [body for path, body in self.puts if path == "/api/config/suno"]
        self.assertGreaterEqual(len(suno), 1)
        self.assertEqual(suno[-1]["weirdness"], 37)
        dashboard = [body for path, body in self.puts if path == "/api/config/dashboard"][-1]
        for key in ("persona", "rival_channels", "publish_mode", "publish_delay_hours"):
            self.assertNotIn(key, dashboard)

    def test_manual_save_omits_absent_fields_and_toast_suffix(self):
        self._load()
        self.page.evaluate("saveSettings()")
        dashboard = [body for path, body in self.puts if path == "/api/config/dashboard"][-1]
        self.assertEqual(dashboard["channel_name"], "Settings fixture")
        for key in ("persona", "rival_channels", "publish_mode", "publish_delay_hours"):
            self.assertNotIn(key, dashboard)
        self.assertEqual(self.page.locator(".toast").last.inner_text(), "保存しました")

    def test_failed_load_blocks_autosave(self):
        self._load(fail=True)
        self._input_weirdness()
        self.page.wait_for_timeout(900)
        self.assertEqual(self.puts, [])

    def test_failed_load_blocks_manual_save(self):
        self._load(fail=True)
        self.page.evaluate("saveSettings()")
        self.assertEqual(self.puts, [])
        self.assertEqual(self.page.locator(".toast").last.inner_text(),
                         "設定の読み込みが完了していないため保存できません")

    def test_failed_reload_blocks_both_save_paths(self):
        self._load()
        self.config_status = 500
        self.page.evaluate("loadConfig()")
        self._input_weirdness()
        self.page.wait_for_timeout(900)
        self.page.evaluate("saveSettings()")
        self.assertEqual(self.puts, [])

    def test_present_persona_and_rivals_keep_manual_save_message(self):
        self._load()
        self.page.evaluate("""() => {
            for (const [id, value] of [['cfgPersona', 'New persona'], ['cfgRivals', 'New rival']]) {
                const el = document.createElement('textarea');
                el.id = id; el.value = value; document.body.appendChild(el);
            }
        }""")
        self.page.evaluate("saveSettings()")
        dashboard = [body for path, body in self.puts if path == "/api/config/dashboard"][-1]
        self.assertEqual(dashboard["persona"], "New persona")
        self.assertEqual(dashboard["rival_channels"], ["New rival"])
        self.assertEqual(self.page.locator(".toast").last.inner_text(),
                         "保存しました (ペルソナ: New persona..., ライバル: 1件)")

    def _remove_optional_inputs(self):
        self.page.evaluate("""() => {
            for (const id of ['cfgDefaultDurationSec', 'cfgReferenceImage', 'cfgTemplatePsd',
                              'cfgTemplatePsdCustom', 'cfgSunoPrompt', 'cfgSunoBatch',
                              'cfgSunoBrowserProfile']) document.getElementById(id).remove();
        }""")

    def _assert_removed_inputs_omitted(self):
        dashboard = [body for path, body in self.puts if path == "/api/config/dashboard"][-1]
        suno = [body for path, body in self.puts if path == "/api/config/suno"][-1]
        for key in ("default_duration_sec", "reference_image", "template_psd"):
            self.assertNotIn(key, dashboard)
        for key in ("prompt", "loop_batch", "browser_profile_dir"):
            self.assertNotIn(key, suno)

    def test_autosave_omits_removed_optional_inputs(self):
        self._load()
        self._remove_optional_inputs()
        self._wait_autosave()
        self._assert_removed_inputs_omitted()

    def test_manual_save_omits_removed_optional_inputs(self):
        self._load()
        self._remove_optional_inputs()
        self.page.evaluate("saveSettings()")
        self._assert_removed_inputs_omitted()


if __name__ == "__main__":
    unittest.main()
