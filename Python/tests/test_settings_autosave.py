"""設定保存を実 HTML と同梱 Chromium で検証する。全通信はモック。"""
from __future__ import annotations

import json
import copy
import unittest
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://settings.test"
CONFIG = {
    "dashboard": {"channel_name": "Settings fixture", "app_id": "orzz",
                  "persona": "Saved persona", "rival_channels": ["Saved rival"]},
    "suno": {"provider": "claude", "weirdness": 20, "api_key": "sk-r●●●●●●●●"},
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
        self.config = copy.deepcopy(CONFIG)
        self.config_status = 200
        self.hold_paths = set()
        self.pending = {}
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
            body = {"status": "ok", "config": {**self.config["dashboard"], **(patch or {})}}
        elif request.method == "GET" and path == "/api/config":
            status = self.config_status
            body = self.config if status == 200 else {"detail": "test load failure"}
        if path in self.hold_paths:
            self.pending.setdefault(path, []).append(route)
            return
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    def _release(self, path, status=200, body=None):
        self.hold_paths.discard(path)
        for route in self.pending.pop(path, []):
            route.fulfill(status=status, content_type="application/json", body=json.dumps(body or {}))

    def _settings_puts(self):
        return [(path, body) for path, body in self.puts if path.startswith("/api/config/")]

    def _start_held_autosave(self):
        self.hold_paths.add("/api/config/dashboard")
        with self.page.expect_request("**/api/config/dashboard"):
            self.page.evaluate("window.testSave = _autoSaveSettingsNow(); void 0")

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
        for key in ("persona", "rival_channels", "publish_mode", "publish_delay_hours",
                    "channel_folder", "channel_name"):
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

    def _check_api_key_edits(self, manual):
        cases = [
            ("sk-r●●●●●●●●", None, None),
            ("sk-r●●●●●●●●", "sk-new-key-123", "sk-new-key-123"),
            ("sk-r●●●●●●●●", "sk-other●mask", None),
            ("plain-loaded-key", None, None),
        ]
        for loaded, entered, expected in cases:
            with self.subTest(loaded=loaded, entered=entered):
                self.config["suno"]["api_key"] = loaded
                self._load()
                self.puts.clear()
                if entered is not None:
                    self.page.locator("#cfgApiKey").evaluate("(el, value) => el.value = value", entered)
                if manual:
                    self.page.evaluate("saveSettings()")
                else:
                    self._wait_autosave()
                patch = [body for path, body in self.puts if path == "/api/config/suno"][-1]
                if expected is None:
                    self.assertNotIn("api_key", patch)
                else:
                    self.assertEqual(patch["api_key"], expected)

    def test_autosave_sends_only_new_unmasked_api_key(self):
        self._check_api_key_edits(manual=False)

    def test_manual_save_sends_only_new_unmasked_api_key(self):
        self._check_api_key_edits(manual=True)

    def test_channel_pointer_inputs_do_not_trigger_autosave(self):
        self._load()
        for selector in ("#cfgFolder", "#cfgName"):
            with self.subTest(selector=selector):
                self.puts.clear()
                self.page.locator(selector).evaluate("""el => {
                    el.value = 'partially typed';
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('blur'));
                }""")
                self.page.wait_for_timeout(900)
                self.assertEqual(self.puts, [])

    def test_switch_blocks_autosave_and_recovers_after_failure(self):
        self._load()
        switch_path = "/api/channels/active/next"
        self.hold_paths.add(switch_path)
        self._input_weirdness()  # 切替開始前に予約済みのタイマーも止める。
        with self.page.expect_request("**" + switch_path):
            self.page.evaluate("window.testSwitch = switchCh('next', true); void 0")
        self._input_weirdness()
        self.page.wait_for_timeout(900)
        self.assertEqual(self._settings_puts(), [])
        self.assertFalse(self.page.evaluate("_settingsLoaded"))
        self._release(switch_path, status=500)
        self.page.evaluate("window.testSwitch")
        self.assertTrue(self.page.evaluate("_settingsLoaded"))
        self.assertEqual(self.page.locator("#cfgSunoWeirdness").input_value(), "20")
        self._wait_autosave()

    def test_switch_network_failure_reloads_config(self):
        self._load()
        self.page.locator("#cfgSunoWeirdness").evaluate("el => el.value = '91'")
        self.page.route("**/api/channels/active/next", lambda route: route.abort())
        self.page.evaluate("switchCh('next', true)")
        self.assertTrue(self.page.evaluate("_settingsLoaded"))
        self.assertEqual(self.page.locator("#cfgSunoWeirdness").input_value(), "20")

    def test_switch_success_reloads_before_autosave_resumes(self):
        self._load()
        switch_path = "/api/channels/active/next"
        self.hold_paths.add(switch_path)
        with self.page.expect_request("**" + switch_path):
            self.page.evaluate("window.testSwitch = switchCh('next', true); void 0")
        self.assertFalse(self.page.evaluate("_settingsLoaded"))
        self.config["suno"]["weirdness"] = 63
        self._release(switch_path)
        self.page.evaluate("window.testSwitch")
        self.assertTrue(self.page.evaluate("_settingsLoaded"))
        self.assertEqual(self.page.locator("#cfgSunoWeirdness").input_value(), "63")

    def test_autosave_snapshots_suno_before_dashboard_response(self):
        self._load()
        self._start_held_autosave()
        self.page.locator("#cfgSunoWeirdness").evaluate("el => el.value = '92'")
        self._release("/api/config/dashboard")
        self.page.evaluate("window.testSave")
        patch = [body for path, body in self.puts if path == "/api/config/suno"][-1]
        self.assertEqual(patch["weirdness"], 20)

    def test_autosave_stops_between_puts_when_settings_become_unloaded(self):
        self._load()
        self._start_held_autosave()
        self.page.evaluate("_settingsLoaded = false")
        self._release("/api/config/dashboard")
        self.page.evaluate("window.testSave")
        self.assertEqual([path for path, body in self.puts], ["/api/config/dashboard"])

    def test_unknown_saved_model_is_omitted_by_both_save_paths(self):
        self.config["suno"]["model"] = "model-not-in-options"
        for manual in (False, True):
            with self.subTest(manual=manual):
                self._load()
                self.puts.clear()
                self.assertEqual(self.page.locator("#cfgSunoModel").input_value(), "")
                if manual:
                    self.page.evaluate("saveSettings()")
                else:
                    self._wait_autosave()
                patch = [body for path, body in self.puts if path == "/api/config/suno"][-1]
                self.assertNotIn("model", patch)

    def test_manual_save_omits_templates_until_list_is_loaded(self):
        self.config["dashboard"].update(template_prproj="saved.prproj", template_psd="saved.psd")
        self.hold_paths.add("/api/templates/list")
        self._load()
        self.page.evaluate("saveSettings()")
        patch = [body for path, body in self.puts if path == "/api/config/dashboard"][-1]
        self.assertNotIn("template_prproj", patch)
        self.assertNotIn("template_psd", patch)
        self._release("/api/templates/list", body={
            "prproj": [{"filename": "saved.prproj"}], "psd": [{"filename": "saved.psd"}],
        })
        self.page.wait_for_function("document.getElementById('cfgTemplatePsd').value === 'saved.psd'")
        self.puts.clear()
        self.page.evaluate("saveSettings()")
        patch = [body for path, body in self.puts if path == "/api/config/dashboard"][-1]
        self.assertEqual(patch["template_prproj"], "saved.prproj")
        self.assertEqual(patch["template_psd"], "saved.psd")

    def test_failed_template_reload_disables_template_save(self):
        self._load()
        self.page.route("**/api/templates/list", lambda route: route.fulfill(
            status=500, content_type="application/json", body='{"detail":"failed"}'))
        self.page.evaluate("loadTemplateOptions()")
        self.page.evaluate("saveSettings()")
        patch = [body for path, body in self.puts if path == "/api/config/dashboard"][-1]
        self.assertNotIn("template_prproj", patch)
        self.assertNotIn("template_psd", patch)


if __name__ == "__main__":
    unittest.main()
