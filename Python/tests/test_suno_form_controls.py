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


COOKIE_BANNER_HTML = """
<style>
  button[aria-label="Create song"] {
    position: fixed; bottom: 20px; left: 20px; width: 160px; height: 40px;
  }
  #cmp-banner-container .cmp-layer {
    display: none; position: fixed; bottom: 0; left: 0; right: 0;
    height: 120px; z-index: 10; background: white;
  }
  #cmp-banner-container .cmp-layer.cmp-visible { display: block; }
</style>
<button aria-label="Create song" onclick="window.created += 1">Create</button>
<div id="cmp-banner-container">
  <div id="cmp-first-layer"
       class="cmp-layer cmp-first-layer cmp-layout-bottom cmp-visible">
    <button id="cmp-first-layer-btn-customize">Customize</button>
    <button id="cmp-first-layer-btn-deny-all" onclick="
      window.rejected += 1;
      document.getElementById('cmp-first-layer').classList.remove('cmp-visible');
    ">Reject All</button>
    <button id="cmp-first-layer-btn-accept-all"
            onclick="window.accepted += 1">Accept All</button>
  </div>
</div>
<script>window.created = 0; window.rejected = 0; window.accepted = 0;</script>
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

    def test_cookie_banner_is_dismissed_by_reject_only(self):
        self.page.set_content(COOKIE_BANNER_HTML)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(suno._dismiss_cookie_banner(self.page))
        self.assertEqual(self.page.evaluate("[window.rejected, window.accepted, window.created]"),
                         [1, 0, 0])
        self.assertFalse(self.page.locator("#cmp-first-layer").is_visible())
        self.assertIn("Cookie 同意バナーを拒否で閉じました", out.getvalue())

    def test_cookie_banner_absent_or_hidden_does_nothing(self):
        for script in (
            "document.getElementById('cmp-banner-container').remove()",
            "document.getElementById('cmp-first-layer').style.display = 'none'",
        ):
            with self.subTest(script=script):
                self.page.set_content(COOKIE_BANNER_HTML)
                self.page.evaluate(script)
                self.assertFalse(suno._dismiss_cookie_banner(self.page))
                self.assertEqual(self.page.evaluate(
                    "[window.rejected, window.accepted, window.created]"), [0, 0, 0])

    def test_cookie_banner_without_reject_requires_manual_dismissal(self):
        self.page.set_content(COOKIE_BANNER_HTML)
        self.page.locator("#cmp-first-layer-btn-deny-all").evaluate("el => el.remove()")
        with self.assertRaises(suno.SunoSubmissionError) as caught:
            suno._dismiss_cookie_banner(self.page)
        self.assertEqual(caught.exception.step, "cookie_banner")
        self.assertIn("ブラウザで Cookie 同意バナーを閉じてから再実行", str(caught.exception))
        self.assertEqual(self.page.evaluate("window.accepted"), 0)

    def test_cookie_banner_reject_failure_requires_manual_dismissal(self):
        for script, rejected in (("el => el.disabled = true", 0),
                                 ("el => el.onclick = () => window.rejected += 1", 1)):
            with self.subTest(script=script):
                self.page.set_content(COOKIE_BANNER_HTML)
                self.page.locator("#cmp-first-layer-btn-deny-all").evaluate(script)
                with self.assertRaises(suno.SunoSubmissionError) as caught:
                    suno._dismiss_cookie_banner(self.page)
                self.assertEqual(caught.exception.step, "cookie_banner")
                self.assertIn("ブラウザで Cookie 同意バナーを閉じてから再実行", str(caught.exception))
                self.assertIsNotNone(caught.exception.__cause__)
                self.assertTrue(self.page.locator("#cmp-first-layer").is_visible())
                self.assertEqual(self.page.evaluate("[window.rejected, window.accepted]"),
                                 [rejected, 0])

    def test_create_click_dismisses_overlapping_cookie_banner(self):
        self.page.set_content(COOKIE_BANNER_HTML)
        self.assertTrue(self.page.locator('button[aria-label="Create song"]').evaluate("""el => {
          const rect = el.getBoundingClientRect();
          return !!document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2)
            .closest('#cmp-banner-container');
        }"""))
        suno.click_create_button(self.page)
        self.assertEqual(self.page.evaluate("[window.rejected, window.accepted, window.created]"),
                         [1, 0, 1])
        self.assertFalse(self.page.locator("#cmp-first-layer").is_visible())

    def test_create_click_reports_obstruction_instead_of_missing_button(self):
        self.page.set_content(COOKIE_BANNER_HTML)
        self.page.locator("#cmp-banner-container").evaluate("el => el.remove()")
        self.page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
          '<div id="overlay" style="position:fixed;inset:0;z-index:20"></div>')""")
        with self.assertRaises(Exception) as caught:
            suno.click_create_button(self.page)
        self.assertIn("Create ボタンをクリックできません", str(caught.exception))
        self.assertNotIn("見つかりません", str(caught.exception))
        self.assertIsNotNone(caught.exception.__cause__)
        self.assertIn(str(caught.exception.__cause__).splitlines()[0], str(caught.exception))
        self.assertEqual(self.page.evaluate("window.created"), 0)

    def test_create_click_reports_missing_button(self):
        self.page.set_content("<main>No controls</main>")
        with self.assertRaisesRegex(Exception, "Create ボタンが見つかりません"):
            suno.click_create_button(self.page)

    def test_inject_dismisses_cookie_banner_before_advanced_mode(self):
        self.page.set_content(COOKIE_BANNER_HTML)

        def ensure_advanced(page):
            self.assertEqual(page.evaluate("window.rejected"), 1)
            self.assertFalse(page.locator("#cmp-first-layer").is_visible())
            return True

        with mock.patch.object(suno, "_ensure_advanced_mode", side_effect=ensure_advanced):
            self.assertTrue(suno.inject_into_suno(self.page, {}))
        self.assertEqual(self.page.evaluate("[window.accepted, window.created]"), [0, 0])

    def test_cookie_banner_error_propagates_from_both_entry_points(self):
        for entry, args in ((suno.inject_into_suno, ({},)), (suno.click_create_button, ())):
            with self.subTest(entry=entry.__name__):
                self.page.set_content(COOKIE_BANNER_HTML)
                self.page.locator("#cmp-first-layer-btn-deny-all").evaluate("el => el.remove()")
                with self.assertRaises(suno.SunoSubmissionError) as caught:
                    entry(self.page, *args)
                self.assertEqual(caught.exception.step, "cookie_banner")
                self.assertIn("ブラウザで Cookie 同意バナーを閉じてから再実行", str(caught.exception))
                self.assertEqual(self.page.evaluate("[window.accepted, window.created]"), [0, 0])

    def test_submission_retries_log_cookie_failure_and_preserve_step(self):
        import app_music_catalog

        self.page.set_content(COOKIE_BANNER_HTML)
        self.page.locator("#cmp-first-layer-btn-deny-all").evaluate("el => el.remove()")
        out = io.StringIO()
        with mock.patch.object(app_music_catalog, "prepare_submission", side_effect=dict), \
                contextlib.redirect_stdout(out):
            with self.assertRaises(suno.SunoSubmissionError) as caught:
                suno._submit_song_to_suno_impl(self.page, {}, form_retries=1)
        self.assertEqual(caught.exception.step, "cookie_banner")
        self.assertIn("ブラウザで Cookie 同意バナーを閉じてから再実行", out.getvalue())
        self.assertEqual(self.page.evaluate("[window.accepted, window.created]"), [0, 0])

    def test_submission_preserves_cookie_failure_after_form_injection(self):
        import app_music_catalog

        self.page.set_content(COOKIE_BANNER_HTML)
        self.page.locator("#cmp-first-layer-btn-deny-all").evaluate("el => el.remove()")
        with mock.patch.object(app_music_catalog, "prepare_submission", side_effect=dict), \
                mock.patch.object(suno, "inject_into_suno", return_value=True):
            with self.assertRaises(suno.SunoSubmissionError) as caught:
                suno._submit_song_to_suno_impl(self.page, {})
        self.assertEqual(caught.exception.step, "cookie_banner")
        self.assertIn("ブラウザで Cookie 同意バナーを閉じてから再実行", str(caught.exception))
        self.assertEqual(self.page.evaluate("[window.accepted, window.created]"), [0, 0])

    def load_login_page(self, html):
        self.page.route("https://suno.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html))
        self.page.goto("https://suno.com/create")

    def test_login_landing_create_button_does_not_mean_logged_in(self):
        self.load_login_page("""
            <button>Create</button><a href="/login">Log in</a>
            <button>Sign Up</button>
        """)
        self.assertFalse(suno.is_suno_logged_in(self.page))

    def test_login_ui_visible_on_landing_but_not_logged_in_page(self):
        self.load_login_page('<button>Log in</button><button>Create</button>')
        self.assertTrue(suno._login_ui_visible(self.page))
        self.load_login_page('<button data-testid="profile-menu-button"></button>')
        self.assertFalse(suno._login_ui_visible(self.page))

    def test_login_wait_leaves_landing_page_untouched(self):
        requests = []

        def landing(route):
            requests.append(route.request.url)
            route.fulfill(status=200, content_type="text/html", body='<button>Log in</button>')

        self.page.route("https://suno.com/**", landing)
        self.page.goto("https://suno.com/")
        for _ in range(3):
            should_navigate = suno._login_wait_should_navigate(self.page)
            if should_navigate:
                self.page.goto("https://suno.com/create")
            self.assertFalse(should_navigate)
        self.assertEqual(requests, ["https://suno.com/"])

    def test_login_wait_only_navigates_from_unidentified_suno_page(self):
        cases = (
            ("https://suno.com/", "<main>Loading</main>", True),
            ("https://suno.com/create", "<main>Loading</main>", False),
            ("https://suno.com/sign-up", "<main>Loading</main>", False),
            ("https://suno.com/login", "<main>Loading</main>", False),
            ("https://suno.com/clerk", "<main>Loading</main>", False),
            ("https://suno.com/?redirect=accounts.google.com", "<main>Loading</main>", False),
            ("https://accounts.google.com/", "<main>Loading</main>", False),
            ("https://example.com/?next=suno.com", "<main>Loading</main>", False),
            ("https://suno.com/", '<button data-testid="profile-menu-button"></button>', False),
        )
        for url, html, expected in cases:
            with self.subTest(url=url, html=html):
                self.page.route("**/*", lambda route: route.fulfill(
                    status=200, content_type="text/html", body=html))
                self.page.goto(url)
                self.assertIs(suno._login_wait_should_navigate(self.page), expected)

    def test_login_profile_menu_confirms_logged_in_before_other_controls(self):
        for other_controls in (
            '<button aria-label="Create song">Create</button><textarea></textarea>',
            '',
            '<a href="/login">Log in</a>',
        ):
            with self.subTest(other_controls=other_controls):
                self.load_login_page(
                    '<button data-testid="profile-menu-button" '
                    'aria-label="Profile menu button"></button>' + other_controls)
                self.assertTrue(suno.is_suno_logged_in(self.page))

    def test_login_falls_back_to_create_button_or_textarea(self):
        for html in ('<button>Create</button>', '<textarea></textarea>'):
            with self.subTest(html=html):
                self.load_login_page(html)
                self.assertTrue(suno.is_suno_logged_in(self.page))

    def test_login_japanese_login_button_overrides_create_button(self):
        self.load_login_page('<button>ログイン</button><button>作成</button>')
        self.assertFalse(suno.is_suno_logged_in(self.page))

    def test_login_rejects_non_suno_url_even_with_profile_menu(self):
        self.page.route("https://example.com/**", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body='''
                <button data-testid="profile-menu-button"></button>
                <button>Create</button><textarea></textarea>
            '''))
        self.page.goto("https://example.com/create")
        self.assertFalse(suno.is_suno_logged_in(self.page))

    def test_login_controls_match_normalized_text_for_all_supported_labels(self):
        for control in (
            '<a href="/login">  LOG   IN  </a>',
            '<div role="button">  Sign\n in  </div>',
            '<button> Ｓｉｇｎ　Ｕｐ </button>',
            '<button> ログイン </button>',
            '<a href="/login"> サインイン </a>',
            '<div role="button"> 新規登録 </div>',
        ):
            with self.subTest(control=control):
                self.load_login_page(control + '<button>Create</button><textarea></textarea>')
                self.assertFalse(suno.is_suno_logged_in(self.page))

    def test_login_ignores_hidden_partial_and_noninteractive_login_text(self):
        for control in (
            '<button style="display:none">Log in</button>',
            '<a href="/login" style="visibility:hidden">Sign Up</a>',
            '<div role="button" hidden>ログイン</div>',
            '<button>Log in to continue</button>',
            '<span>Log in</span>',
        ):
            with self.subTest(control=control):
                self.load_login_page(control + '<button>Create</button>')
                self.assertTrue(suno.is_suno_logged_in(self.page))

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


class DownloadLoginTests(unittest.TestCase):
    def run_download(self, settings, logged_in, ready_poll=False):
        session = mock.MagicMock()
        page = session.page.return_value
        calls = []
        page.goto.side_effect = lambda *args, **kwargs: calls.append("goto")

        def check_login(actual_page):
            self.assertIs(actual_page, page)
            calls.append("login")
            return logged_in

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch("playwright.sync_api.sync_playwright"))
            stack.enter_context(mock.patch.object(suno, "open_suno_context", return_value=session))
            stack.enter_context(mock.patch.object(suno, "is_suno_logged_in", side_effect=check_login))
            download = stack.enter_context(mock.patch.object(
                suno, "download_workspace_tracks", side_effect=lambda *args: calls.append("download")))
            poll = stack.enter_context(mock.patch.object(
                suno, "_ready_poll_and_download", side_effect=lambda *args: calls.append("poll")))
            stack.enter_context(mock.patch.object(suno, "SunoProgress"))
            stack.enter_context(mock.patch.object(suno.time, "sleep"))
            stack.enter_context(mock.patch.dict(os.environ, {
                "APP_SUNO_DL_MODE": "legacy", "APP_SUNO_READY_POLL": "1" if ready_poll else "0",
            }))
            if logged_in:
                suno._run_download_only("fixture", "/fixture", settings)
            else:
                with self.assertRaises(suno.UnattendedLoginRequired) as caught:
                    suno._run_download_only("fixture", "/fixture", settings)
                message = str(caught.exception)
                resolved = suno.resolve_browser_settings(settings)
                self.assertIn(resolved["mode"], message)
                self.assertIn(str(resolved["cdp_port"]) if resolved["mode"] == "cdp"
                              else resolved["profile_dir"], message)
                self.assertIn("そのブラウザで SUNO にログインしてから再実行", message)
                download.assert_not_called()
                poll.assert_not_called()
        page.goto.assert_called_once_with(
            "https://suno.com/create", wait_until="domcontentloaded", timeout=30000)
        session.close.assert_called_once_with()
        return calls

    def test_download_requires_login_for_every_connection_mode(self):
        for mode in ("chrome", "chromium", "cdp"):
            for ready_poll in (False, True):
                with self.subTest(mode=mode, ready_poll=ready_poll):
                    calls = self.run_download(
                        {"browser_mode": mode, "cdp_port": 9333, "expected_ready": 2},
                        logged_in=False, ready_poll=ready_poll)
                    self.assertEqual(calls, ["goto", "login"])

    def test_download_checks_login_before_both_download_paths(self):
        for ready_poll, expected in ((False, "download"), (True, "poll")):
            with self.subTest(ready_poll=ready_poll):
                calls = self.run_download({"expected_ready": 2}, logged_in=True, ready_poll=ready_poll)
                self.assertEqual(calls, ["goto", "login", expected])


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
