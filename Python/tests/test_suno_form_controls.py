#!/usr/bin/env python3
"""採取済みのフォームDOM断片で検証する。実SUNOには接続しない。"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suno_auto_create as suno


FORM_HTML = """
<div>
  <div role="button" aria-expanded="false"><span>More Options</span>
    <span>Variety</span></div>
  <div style="display:none">
    <div>
      <div><span>Duration</span></div>
      <div><button type="button" tabindex="0" data-selected="true">Auto</button>
        <button type="button" tabindex="0" data-selected="false">Custom</button></div>
    </div>
    <div><div><span>Weirdness</span></div>
      <div role="slider" aria-label="Weirdness" aria-valuemin="0"
        aria-valuemax="100" aria-valuenow="50" tabindex="0">Expected results</div>
      <div>50%</div></div>
    <div><div><span>Style Influence</span></div>
      <div role="slider" aria-label="Style Influence" aria-valuemin="0"
        aria-valuemax="100" aria-valuenow="50" tabindex="0">Moderate</div>
      <div>50%</div></div>
    <div><div><span>Variety</span></div>
      <div role="slider" aria-label="Variety" aria-valuemin="0"
        aria-valuemax="4" aria-valuenow="0" aria-valuetext="Off"
        tabindex="0">Exact style</div><div>Off</div></div>
    <div style="display:none"><input placeholder="Song Title (Optional)"></div>
    <input placeholder="Song Title (Optional)">
  </div>
  <div style="display:none"><div><span>Background music</span></div>
    <div><div><span>Variety</span></div>
      <div role="slider" aria-label="Variety" aria-valuemin="0"
        aria-valuemax="4" aria-valuenow="1" aria-valuetext="Normal"
        tabindex="0">Normal</div><div>Normal</div></div>
  </div>
  <button aria-label="Create song">Create</button>
</div>
<script>
(() => {
window.optionClicks = 0;
window.arrowKeys = [];
window.ignoredArrows = 0;
window.freezeSlider = false;
const toggle = document.querySelector('[role="button"][aria-expanded]');
toggle.addEventListener('click', () => {
  window.optionClicks += 1;
  const opened = toggle.getAttribute('aria-expanded') === 'false';
  toggle.setAttribute('aria-expanded', String(opened));
  toggle.nextElementSibling.style.display = opened ? '' : 'none';
});
document.querySelectorAll('[role="slider"]').forEach(slider => {
  slider.addEventListener('keydown', event => {
    window.arrowKeys.push(event.key);
    if (!['ArrowRight', 'ArrowLeft'].includes(event.key)) return;
    event.preventDefault();
    if (window.freezeSlider) return;
    if (window.ignoredArrows > 0) { window.ignoredArrows -= 1; return; }
    const value = Number(slider.getAttribute('aria-valuenow'));
    const low = Number(slider.getAttribute('aria-valuemin'));
    const high = Number(slider.getAttribute('aria-valuemax'));
    slider.setAttribute('aria-valuenow', String(Math.max(low, Math.min(high,
      value + (event.key === 'ArrowRight' ? 1 : -1)))));
  });
});
const custom = Array.from(document.querySelectorAll('button'))
  .find(button => button.textContent === 'Custom');
custom.addEventListener('click', () => {
  const container = custom.parentElement.parentElement;
  custom.parentElement.remove();
  container.insertAdjacentHTML('beforeend', `
    <div role="slider" aria-label="Duration" aria-valuemin="10"
      aria-valuemax="360" aria-valuenow="180" aria-valuetext="3 minutes"
      aria-disabled="false" tabindex="0">3 minutes</div>
    <input type="text" aria-label="Duration" inputmode="decimal">`);
  const field = container.querySelector('input');
  field.value = '3:00';
  field.addEventListener('blur', () => {
    const parts = field.value.split(':').map(Number);
    if (parts.length === 2) container.querySelector('[role="slider"]')
      .setAttribute('aria-valuenow', String(parts[0] * 60 + parts[1]));
  });
});
for (let i = 0; i < 17; i++) {
  const button = document.createElement('button');
  button.setAttribute('aria-label', 'More options');
  document.body.append(button);
}
})();
</script>
"""


class FormControlsTests(unittest.TestCase):
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
        env = mock.patch.dict(os.environ, {"APP_SUNO_FORM_WAIT_SCALE": "0"})
        env.start()
        self.addCleanup(env.stop)
        self.context = self._browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.page.set_content(FORM_HTML)

    def open_options(self):
        self.page.locator('[role="button"][aria-expanded]').click()

    def slider_value(self, name):
        return self.page.get_by_role("slider", name=name, exact=True).get_attribute("aria-valuenow")

    def test_more_options_ignores_song_menus_and_only_opens_once(self):
        self.assertEqual(self.page.locator('button[aria-label="More options"]').count(), 17)
        for _ in range(2):
            toggle = suno._ensure_more_options_open(self.page)
            self.assertEqual(toggle.get_attribute("aria-expanded"), "true")
        self.assertEqual(self.page.evaluate("window.optionClicks"), 1)

    def test_more_options_requires_one_visible_heading(self):
        for count in (0, 2):
            with self.subTest(count=count):
                self.page.set_content(FORM_HTML)
                self.page.locator('[role="button"][aria-expanded]').evaluate(
                    "(el, count) => count === 0 ? el.remove() : el.after(el.cloneNode(true))", count)
                with self.assertRaises(suno.SunoSubmissionError) as caught:
                    suno._ensure_more_options_open(self.page)
                self.assertEqual(caught.exception.step, "more_options")
                self.assertIn(str(count), str(caught.exception))

    def test_more_options_japanese_prefix_and_hidden_duplicate(self):
        self.page.locator('[role="button"][aria-expanded]').evaluate("""el => {
          el.textContent = '  その他のオプション\\n  Variety';
          const hidden = document.createElement('div'); hidden.style.display = 'none';
          hidden.append(el.cloneNode(true)); el.after(hidden);
          // Keep the options content immediately after the observed heading.
          el.parentElement.append(hidden);
        }""")
        self.assertEqual(suno._ensure_more_options_open(self.page).get_attribute("aria-expanded"), "true")

    def test_title_candidates_and_fill_use_only_visible_input(self):
        self.open_options()
        self.assertEqual(len(suno._title_inputs_in_create_panel(self.page)), 1)
        self.assertTrue(suno._fill_optional_title(self.page, "Local test", retries=0))
        inputs = self.page.locator('input[placeholder="Song Title (Optional)"]')
        self.assertEqual(inputs.nth(0).input_value(), "")
        self.assertEqual(inputs.nth(1).input_value(), "Local test")

    def test_ambiguous_titles_are_not_written_or_cleared(self):
        self.open_options()
        self.page.locator('input[placeholder]').nth(1).evaluate("""el => {
          el.value = 'Existing'; el.after(el.cloneNode(true));
          el.nextElementSibling.value = 'Other';
        }""")
        self.assertFalse(suno._fill_optional_title(self.page, "New title", retries=0))
        self.assertEqual(self.page.locator('input[placeholder]').evaluate_all(
            "els => els.map(el => el.value)"), ["", "Existing", "Other"])

    def test_japanese_title_placeholder_is_preserved(self):
        self.open_options()
        self.page.locator('input[placeholder]').evaluate_all(
            "els => els.forEach(el => el.setAttribute('placeholder', '曲名（任意）'))")
        self.assertTrue(suno._fill_optional_title(self.page, "試験曲", retries=0))
        self.assertEqual(self.page.locator('input[placeholder]').nth(1).input_value(), "試験曲")

    def test_sliders_move_by_unit_arrows_and_ignore_hidden_variety(self):
        self.open_options()
        for name, target, key, count in (
            ("Weirdness", 35, "ArrowLeft", 15),
            ("Style Influence", 80, "ArrowRight", 30),
            ("Variety", 3, "ArrowRight", 3),
        ):
            with self.subTest(name=name):
                self.page.evaluate("window.arrowKeys = []")
                suno._set_slider_value(self.page, name, target)
                self.assertEqual(self.slider_value(name), str(target))
                self.assertEqual(self.page.evaluate("window.arrowKeys"), [key] * count)
        self.assertEqual(self.page.locator('[role="slider"][aria-label="Variety"]').nth(1)
                         .get_attribute("aria-valuenow"), "1")

    def test_invalid_slider_values_do_not_change_controls(self):
        self.open_options()
        for name, target in (("Weirdness", 101), ("Style Influence", -1), ("Variety", 5),
                             ("Weirdness", 35.5), ("Weirdness", "35"), ("Variety", True)):
            with self.subTest(name=name, target=target):
                before = self.slider_value(name)
                with self.assertRaises(suno.SunoSubmissionError):
                    suno._set_slider_value(self.page, name, target)
                self.assertEqual(self.slider_value(name), before)
        self.assertEqual(self.page.evaluate("window.arrowKeys"), [])

    def test_slider_requires_exactly_one_visible_candidate(self):
        with self.assertRaises(suno.SunoSubmissionError):
            suno._set_slider_value(self.page, "Variety", 3)
        self.open_options()
        self.page.get_by_role("slider", name="Weirdness", exact=True).evaluate(
            "el => el.after(el.cloneNode(true))")
        with self.assertRaises(suno.SunoSubmissionError):
            suno._set_slider_value(self.page, "Weirdness", 35)
        self.assertEqual(self.page.evaluate("window.arrowKeys"), [])

    def test_slider_retries_only_remaining_difference_once(self):
        self.open_options()
        self.page.evaluate("window.ignoredArrows = 2")
        suno._set_slider_value(self.page, "Weirdness", 35)
        self.assertEqual(self.slider_value("Weirdness"), "35")
        self.assertEqual(len(self.page.evaluate("window.arrowKeys")), 17)
        self.page.evaluate("window.freezeSlider = true; window.arrowKeys = []")
        with self.assertRaises(suno.SunoSubmissionError) as caught:
            suno._set_slider_value(self.page, "Weirdness", 30)
        self.assertEqual(caught.exception.step, "slider_validation")
        for value in ("Weirdness", "30", "35"):
            self.assertIn(value, str(caught.exception))
        self.assertEqual(len(self.page.evaluate("window.arrowKeys")), 10)

    def test_numeric_options_only_touch_requested_values_and_log_them(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            suno._apply_numeric_options(self.page, {"weirdness": 35, "style_influence": None, "variety": 3})
        self.assertEqual([self.slider_value(n) for n in ("Weirdness", "Style Influence", "Variety")],
                         ["35", "50", "3"])
        self.assertIn("Weirdness を検証: 35", out.getvalue())
        self.assertIn("Variety を検証: 3", out.getvalue())
        self.assertNotIn("Style Influence", out.getvalue())

    def test_unspecified_numeric_options_do_not_open_panel(self):
        for content in ({}, {"weirdness": None, "style_influence": None, "variety": None}):
            suno._apply_numeric_options(self.page, content)
        self.assertEqual(self.page.evaluate("window.optionClicks"), 0)

    def test_custom_duration_commits_with_tab_and_preserves_return_value(self):
        self.assertEqual(suno._set_custom_duration(self.page, 241), 241)
        self.assertEqual(self.page.get_by_role("textbox", name="Duration", exact=True).input_value(), "4:01")
        self.assertEqual(self.slider_value("Duration"), "241")
        self.assertEqual(self.page.get_by_role("button", name="Custom", exact=True).count(), 0)
        suno._set_custom_duration(self.page, 240)
        self.assertEqual(self.slider_value("Duration"), "240")
        self.assertEqual(self.page.evaluate("window.optionClicks"), 1)

    def test_out_of_range_duration_is_rejected_before_input(self):
        for seconds in (5, 400, 0, -1):
            with self.subTest(seconds=seconds):
                self.page.set_content(FORM_HTML)
                self.open_options()
                with self.assertRaises(suno.SunoSubmissionError) as caught:
                    suno._set_custom_duration(self.page, seconds)
                self.assertEqual(caught.exception.step, "duration_validation")
                for value in (str(seconds), "10", "360"):
                    self.assertIn(value, str(caught.exception))
                self.assertEqual(self.page.get_by_role("textbox", name="Duration", exact=True).input_value(), "3:00")
                self.assertEqual(self.slider_value("Duration"), "180")

    def test_duration_reads_dom_bounds_and_falls_back_if_absent(self):
        self.open_options()
        self.page.get_by_role("button", name="Custom", exact=True).click()
        slider = self.page.get_by_role("slider", name="Duration", exact=True)
        slider.evaluate("el => el.setAttribute('aria-valuemax', '200')")
        with self.assertRaises(suno.SunoSubmissionError):
            suno._set_custom_duration(self.page, 241)
        slider.evaluate("el => { el.removeAttribute('aria-valuemin'); el.removeAttribute('aria-valuemax'); }")
        suno._set_custom_duration(self.page, 241)
        self.assertEqual(self.slider_value("Duration"), "241")

    def test_inject_applies_duration_and_numeric_values_with_one_open(self):
        with mock.patch.object(suno, "_ensure_advanced_mode", return_value=True):
            self.assertTrue(suno.inject_into_suno(self.page, {
                "duration_seconds": 241, "weirdness": 35, "style_influence": 80, "variety": 3}))
        self.assertEqual([self.slider_value(n) for n in ("Duration", "Weirdness", "Style Influence", "Variety")],
                         ["241", "35", "80", "3"])
        self.assertEqual(self.page.evaluate("window.optionClicks"), 1)

    def test_inject_stops_when_numeric_validation_fails(self):
        with mock.patch.object(suno, "_ensure_advanced_mode", return_value=True):
            self.assertFalse(suno.inject_into_suno(self.page, {"weirdness": 101}))


class NumericCliTests(unittest.TestCase):
    def test_invalid_cli_values_exit_before_any_browser_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.json"
            config.write_text("{}", encoding="utf-8")
            for flag, value in (("--weirdness", "101"), ("--style-influence", "-1"),
                                ("--variety", "5"), ("--duration-seconds", "5"),
                                ("--duration-seconds", "400"), ("--weirdness", "1.5")):
                with self.subTest(flag=flag, value=value):
                    result = subprocess.run(
                        [sys.executable, str(Path(suno.__file__)), "--config", str(config),
                         "--save-config", flag, value], capture_output=True, text=True, timeout=15)
                    self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                    self.assertIn(flag, result.stderr)
                    self.assertNotIn("unrecognized arguments", result.stderr)
                    self.assertEqual(config.read_text(encoding="utf-8"), "{}")


class NumericOptionTransferTests(unittest.TestCase):
    """設定の受渡を実関数で検証し、外部ブラウザと送信だけを隔離する。"""

    def test_cli_numeric_values_saved_without_browser(self):
        import json
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "suno.json"
            result = subprocess.run(
                [sys.executable, str(Path(suno.__file__)), "--config", str(config_path),
                 "--save-config", "--duration-seconds", "241", "--weirdness", "35",
                 "--style-influence", "80", "--variety", "0"],
                text=True, capture_output=True, timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            settings = json.loads(config_path.read_text(encoding="utf-8"))
            self.assertEqual(
                {key: settings[key] for key in ("duration_seconds", "weirdness", "style_influence", "variety")},
                {"duration_seconds": 241, "weirdness": 35, "style_influence": 80, "variety": 0},
            )

    def test_pregenerated_song_uses_settings_values_before_injection(self):
        import contextlib
        import io
        import app_music_catalog

        song = {"title": "Fixture", "mode": "styles_title_only", "duration_seconds": 180,
                "weirdness": 50, "style_influence": 50, "variety": 3}
        settings = dict(suno.DEFAULT_SETTINGS, loop_count=1, pregenerated_songs=[song],
                        duration_seconds=241, weirdness=35, style_influence=80, variety=0)
        page = mock.MagicMock(url="https://suno.com/create")
        session = mock.MagicMock()
        session.page.return_value = page
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("playwright.sync_api.sync_playwright"))
            stack.enter_context(mock.patch.object(suno, "open_suno_context", return_value=session))
            stack.enter_context(mock.patch.object(suno, "SunoProgress"))
            stack.enter_context(mock.patch.object(suno, "is_suno_logged_in", return_value=True))
            stack.enter_context(mock.patch.object(suno, "_load_dashboard_config_for_brand", return_value={}))
            stack.enter_context(mock.patch.object(suno, "_set_status"))
            stack.enter_context(mock.patch.object(suno.time, "sleep"))
            stack.enter_context(mock.patch.object(app_music_catalog, "prepare_submission", side_effect=dict))
            stack.enter_context(mock.patch.object(app_music_catalog, "mark_submitted"))
            injected = stack.enter_context(mock.patch.object(suno, "inject_into_suno", return_value=False))
            create = stack.enter_context(mock.patch.object(suno, "click_create_button"))
            stack.enter_context(mock.patch.dict(os.environ, {"APP_SUNO_DL_MODE": "legacy", "APP_SUNO_NO_HOLD": "1"}))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            suno.run_browser_automation(settings)
        injected.assert_called_once()
        actual = injected.call_args.args[1]
        self.assertEqual(
            {key: actual[key] for key in ("duration_seconds", "weirdness", "style_influence", "variety")},
            {"duration_seconds": 241, "weirdness": 35, "style_influence": 80, "variety": 0},
        )
        self.assertEqual(song["duration_seconds"], 180)
        self.assertEqual(song["variety"], 3)
        create.assert_not_called()

    def test_queue_job_values_override_channel_and_reach_submission(self):
        import json
        import tempfile
        import suno_queue

        with tempfile.TemporaryDirectory() as tmp:
            channel = Path(tmp) / "channel"
            channel.mkdir()
            (channel / ".app_channel_config.json").write_text(json.dumps({"suno": {
                "prompt": "Fixture prompt", "duration_seconds": 180,
                "weirdness": 50, "style_influence": 50, "variety": 3,
            }}), encoding="utf-8")
            jobs_path = Path(tmp) / "jobs.json"
            jobs_path.write_text(json.dumps([{
                "vol": 1, "channel_folder": str(channel), "workspace": "fixture", "count": 1,
                "download_dir": str(Path(tmp) / "download"), "duration_seconds": 241,
                "weirdness": 35, "style_influence": 80, "variety": 0,
            }]), encoding="utf-8")
            with mock.patch.object(suno, "load_config", return_value=dict(suno.DEFAULT_SETTINGS)):
                job = suno_queue.load_jobs(jobs_path)[0]
        expected = {"duration_seconds": 241, "weirdness": 35, "style_influence": 80, "variety": 0}
        self.assertEqual({key: job.settings[key] for key in expected}, expected)
        song = {"title": "Fixture", "duration_seconds": 120,
                "weirdness": 90, "style_influence": 20, "variety": 4}
        page = mock.Mock(url="https://suno.com/create?wid=00000000-0000-0000-0000-000000000001")
        with mock.patch.object(suno, "ensure_workspace", return_value=True), \
                mock.patch.object(suno_queue, "prepare_drafts", return_value=[song]), \
                mock.patch.object(suno, "submit_song_to_suno") as submit, \
                mock.patch.object(suno_queue.time, "sleep"), \
                mock.patch.object(suno_queue, "emit"):
            suno_queue.submit_job(page, job)
        submit.assert_called_once()
        actual = submit.call_args.args[1]
        self.assertEqual({key: actual[key] for key in expected}, expected)
        self.assertEqual(song["variety"], 4)
        self.assertEqual(job.submitted_count, 1)

    def test_queue_unspecified_settings_preserve_song_values(self):
        import suno_queue

        song = {"title": "Fixture", "duration_seconds": 241,
                "weirdness": 35, "style_influence": 80, "variety": 0}
        job = suno_queue.Job(1, 1, Path("/fixture"), "fixture", "prompt", 1,
                             "styles_title_only", Path("/fixture/download"),
                             {"weirdness": None, "style_influence": None})
        page = mock.Mock(url="https://suno.com/create?wid=00000000-0000-0000-0000-000000000001")
        with mock.patch.object(suno, "ensure_workspace", return_value=True), \
                mock.patch.object(suno_queue, "prepare_drafts", return_value=[song]), \
                mock.patch.object(suno, "submit_song_to_suno") as submit, \
                mock.patch.object(suno_queue.time, "sleep"), \
                mock.patch.object(suno_queue, "emit"):
            suno_queue.submit_job(page, job)
        submit.assert_called_once_with(page, song, form_retries=2)


if __name__ == "__main__":
    unittest.main()
