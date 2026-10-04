#!/usr/bin/env python3
"""日英の採取済み曲一覧DOMを検証する。実SUNOには接続しない。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suno_fast_dl as fd


SONGS = [
    {"song_id": "0ebfc8fd-2b5a-4c28-8aca-914d54b7ef15", "title": "朝の散歩"},
    {"song_id": "27e94ef1-3a5b-4276-bd82-b0da4bd4c0be", "title": "Evening Light"},
]

ENGLISH_WORKSPACE_HTML = """
<html lang="en"><body><main>
  <button id="filters" role="combobox" aria-label="Filters"
          aria-expanded="false">Filters</button>
  <div role="listbox" style="display:none">
    <div role="option" aria-selected="false"
         aria-label="Hide disliked clips">Hide disliked clips</div>
    <div role="option" aria-selected="false"
         aria-label="Hide Stems">Hide Stems</div>
    <div role="option" aria-selected="false"
         aria-label="Hide Clips from Edit Mode">Hide Clips from Edit Mode</div>
  </div>
  <div class="clip-browser-list-scroller" style="height:150px;overflow:auto">
    <div data-testid="clip-row" aria-label="朝の散歩" style="height:100px">
      <div role="button" class="clip-image-container" aria-label="Play 朝の散歩">Play</div>
      <a href="/song/0ebfc8fd-2b5a-4c28-8aca-914d54b7ef15">朝の散歩</a>
    </div>
    <div data-testid="clip-row" aria-label="Evening Light" style="height:100px">
      <div role="button" class="clip-image-container" aria-label="Play Evening Light">Play</div>
      <a href="/song/27e94ef1-3a5b-4276-bd82-b0da4bd4c0be">Evening Light</a>
    </div>
  </div>
  <span id="song-count">2 songs</span>
  <input aria-label="Current page number" value="1">
  <button id="previous" aria-label="Previous page" disabled>Previous</button>
  <button id="next" aria-label="Next page" disabled>Next</button>
</main>
<script>
(() => {
  const button = document.getElementById('filters');
  const baseLabel = button.getAttribute('aria-label');
  const menu = document.querySelector('[role="listbox"]');
  window.filterClicks = [];
  window.selectCount = count => {
    menu.querySelectorAll('[role="option"]').forEach((option, index) => {
      option.setAttribute('aria-selected', String(index < count));
    });
    button.setAttribute('aria-label', count ? `${baseLabel} (${count})` : baseLabel);
  };
  button.addEventListener('click', () => {
    const opened = button.getAttribute('aria-expanded') !== 'true';
    button.setAttribute('aria-expanded', String(opened));
    menu.style.display = opened ? '' : 'none';
  });
  menu.querySelectorAll('[role="option"]').forEach((option, index) => {
    option.addEventListener('click', () => {
      window.filterClicks.push(index);
      option.setAttribute('aria-selected', String(option.getAttribute('aria-selected') !== 'true'));
      const count = menu.querySelectorAll('[aria-selected="true"]').length;
      button.setAttribute('aria-label', count ? `${baseLabel} (${count})` : baseLabel);
    });
  });
})();
</script></body></html>
"""

JAPANESE_WORKSPACE_HTML = (
    ENGLISH_WORKSPACE_HTML.replace('lang="en"', 'lang="ja"')
    .replace("Play 朝の散歩", "朝の散歩を再生")
    .replace("Play Evening Light", "Evening Lightを再生")
    .replace("2 songs", "2曲")
    .replace("Current page number", "現在のページ番号")
    .replace("Previous page", "前のページ")
    .replace("Next page", "次のページ")
    .replace("Filters", "フィルター")
    .replace("Hide disliked clips", "低く評価したクリップを非表示")
    .replace("Hide Stems", "ステムを非表示")
    .replace("Hide Clips from Edit Mode", "編集モードのクリップを非表示")
)
WORKSPACES = (("ja", JAPANESE_WORKSPACE_HTML), ("en", ENGLISH_WORKSPACE_HTML))


class WorkspaceRowsTests(unittest.TestCase):
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
        self.context = self._browser.new_context()
        self.addCleanup(self.context.close)
        self.context.route("**/*", lambda route: route.abort())
        self.page = self.context.new_page()
        self.page.set_default_timeout(1000)
        self.page.set_content(JAPANESE_WORKSPACE_HTML)

    def assert_workspace(self, html):
        self.page.set_content(html)
        state = self.page.evaluate(fd._WORKSPACE_ROWS_DOM, "read")
        self.assertEqual(state["expected"], 2)
        self.assertEqual(state["rows"], SONGS)
        self.assertEqual(state["page_no"], 1)
        self.assertTrue(state["previous_present"])
        self.assertTrue(state["next_present"])
        self.assertFalse(state["previous"])
        self.assertFalse(state["next"])
        self.assertEqual(state["client"], 150)
        self.assertEqual(state["maximum"], 50)

    def test_japanese_workspace_dom(self):
        self.assert_workspace(JAPANESE_WORKSPACE_HTML)

    def test_english_workspace_dom(self):
        self.assert_workspace(ENGLISH_WORKSPACE_HTML)

    def test_song_count_formats(self):
        for label, expected in (("1 song", 1), ("2 songs", 2), ("1,234 songs", 1234),
                                ("2曲", 2), ("2 曲", 2), ("1,234曲", 1234),
                                ("1,234 曲", 1234)):
            with self.subTest(label=label):
                self.page.locator("#song-count").evaluate("(el, text) => el.textContent = text", label)
                self.assertEqual(self.page.evaluate(fd._WORKSPACE_ROWS_DOM, "read")["expected"], expected)

    def test_play_pause_selectors_in_both_languages(self):
        for lang, html in WORKSPACES:
            with self.subTest(lang=lang):
                self.page.set_content(html)
                for song in SONGS:
                    row = fd._song_row(self.page, song["song_id"])
                    self.assertEqual(row.locator(fd._PLAY_BUTTON).count(), 1)
                    self.assertEqual(row.locator(fd._PAUSE_BUTTON).count(), 0)
                    paused = song["title"] + "を一時停止" if lang == "ja" else "Pause " + song["title"]
                    row.locator(".clip-image-container").evaluate(
                        "(el, label) => el.setAttribute('aria-label', label)", paused)
                    self.assertEqual(row.locator(fd._PAUSE_BUTTON).count(), 1)
                    self.assertEqual(row.locator(fd._PLAY_BUTTON).count(), 0)

    def set_row_title_and_button(self, song_id, title, label):
        row = fd._song_row(self.page, song_id)
        row.evaluate("(el, title) => el.setAttribute('aria-label', title)", title)
        row.locator(".clip-image-container").evaluate(
            "(el, label) => el.setAttribute('aria-label', label)", label)
        return row

    def test_row_playback_buttons_match_whole_title(self):
        cases = (
            ("Play of Light", "Play of Lightを一時停止", 0, 1),
            ("Pause for Breath", "Pause for Breathを再生", 1, 0),
            ("朝を再生", "Pause 朝を再生", 0, 1),
            ("朝を一時停止", "Play 朝を一時停止", 1, 0),
        )
        for title, label, plays, pauses in cases:
            with self.subTest(label=label):
                row = self.set_row_title_and_button(SONGS[0]["song_id"], title, label)
                self.assertEqual(fd._row_play_button(row).count(), plays)
                self.assertEqual(fd._row_pause_button(row).count(), pauses)

    def test_row_playback_buttons_escape_title_and_ignore_english_case(self):
        title = 'Light "[Moon]" \\ Night'
        for label, plays, pauses in (("pLaY " + title.upper(), 1, 0),
                                     ("pAuSe " + title.upper(), 0, 1)):
            with self.subTest(label=label):
                row = self.set_row_title_and_button(SONGS[0]["song_id"], title, label)
                self.assertEqual(fd._row_play_button(row).count(), plays)
                self.assertEqual(fd._row_pause_button(row).count(), pauses)

    def test_row_playback_buttons_fallback_only_without_title(self):
        row = fd._song_row(self.page, SONGS[0]["song_id"])
        for title in (None, ""):
            for label, plays, pauses in (("Unknownを再生", 1, 0), ("Pause Unknown", 0, 1)):
                with self.subTest(title=title, label=label):
                    row.evaluate("(el, title) => title === null ? el.removeAttribute('aria-label') : el.setAttribute('aria-label', title)", title)
                    row.locator(".clip-image-container").evaluate(
                        "(el, label) => el.setAttribute('aria-label', label)", label)
                    self.assertEqual(fd._row_play_button(row).count(), plays)
                    self.assertEqual(fd._row_pause_button(row).count(), pauses)
        row = self.set_row_title_and_button(SONGS[0]["song_id"], "Known", "Play Different")
        self.assertEqual(fd._row_play_button(row).count(), 0)
        with mock.patch.object(row, "get_attribute", side_effect=RuntimeError("unavailable")):
            self.assertEqual(fd._row_play_button(row).count(), 1)

    def test_song_title_leaf_is_not_a_song_count(self):
        for (lang, html), title in zip(WORKSPACES, ("3曲", "10 songs")):
            with self.subTest(lang=lang, title=title):
                self.page.set_content(html)
                row = fd._song_row(self.page, SONGS[0]["song_id"])
                row.locator('a[href*="/song/"]').evaluate("""(el, title) => {
                    const leaf = document.createElement('span');
                    leaf.textContent = title;
                    el.replaceChildren(leaf);
                }""", title)
                state = self.page.evaluate(fd._WORKSPACE_ROWS_DOM, "read")
                self.assertEqual(state["rows"][0]["title"], title)
                self.assertEqual(state["expected"], 2)

    def test_stopped_single_song_does_not_prime_from_title(self):
        song_id = SONGS[0]["song_id"]
        title = "Pause for Breath"
        self.set_row_title_and_button(song_id, title, title + "を再生")
        fd._song_row(self.page, SONGS[1]["song_id"]).evaluate("el => el.remove()")
        with mock.patch.object(fd, "ensure_fast_capture", return_value=True), \
                mock.patch.object(fd, "register_transfer_binding"), \
                mock.patch.object(fd, "_capture_play", return_value=b"fixture audio") as capture:
            self.assertEqual(fd.capture_song(self.page, song_id, title), b"fixture audio")
            self.assertEqual(capture.call_count, 1)
            self.assertEqual(capture.call_args.args[2], song_id)

    def test_paused_song_is_not_selected_as_playable_alternate(self):
        song_id = SONGS[0]["song_id"]
        self.set_row_title_and_button(song_id, "Target", "Targetを一時停止")
        self.set_row_title_and_button(SONGS[1]["song_id"], "Play of Light", "Play of Lightを一時停止")
        with mock.patch.object(fd, "ensure_fast_capture", return_value=True), \
                mock.patch.object(fd, "register_transfer_binding"), \
                mock.patch.object(fd, "_capture_play", return_value=b"fixture audio") as capture:
            with self.assertRaisesRegex(RuntimeError, "再取得の準備に必要な別曲が表示されていません"):
                fd.capture_song(self.page, song_id, "Target")
            capture.assert_not_called()

    def test_iter_workspace_rows_in_both_languages(self):
        for lang, html in WORKSPACES:
            with self.subTest(lang=lang):
                self.page.set_content(html)
                messages = []
                self.assertEqual(fd.iter_workspace_rows(self.page, status_cb=messages.append),
                                 [dict(song, page_no=1) for song in SONGS])
                self.assertIn("page=1 rows=2 total=2 expected=2", messages)

    def test_clear_selected_filters_in_both_languages(self):
        for lang, html in WORKSPACES:
            for count in (1, 3):
                with self.subTest(lang=lang, count=count):
                    self.page.set_content(html)
                    self.page.evaluate("count => window.selectCount(count)", count)
                    self.assertEqual(fd.iter_workspace_rows(self.page),
                                     [dict(song, page_no=1) for song in SONGS])
                    self.assertEqual(self.page.locator('[role="option"][aria-selected="true"]').count(), 0)
                    self.assertEqual(self.page.evaluate("window.filterClicks"), list(range(count)))
                    self.assertEqual(self.page.locator("#filters").get_attribute("aria-expanded"), "false")

    def test_duplicate_filter_buttons_raise(self):
        self.page.locator("#filters").evaluate("el => el.after(el.cloneNode(true))")
        with self.assertRaisesRegex(RuntimeError, "曲一覧のFiltersを一意に特定できません"):
            fd._clear_workspace_filters(self.page)

    def test_filter_count_mismatch_raises(self):
        self.page.locator("#filters").evaluate("el => el.setAttribute('aria-label', 'フィルター (1)')")
        with self.assertRaisesRegex(RuntimeError, "Filters件数と選択済み項目が一致しません"):
            fd._clear_workspace_filters(self.page)

    def test_missing_count_and_terminal_evidence_raise(self):
        self.page.locator("#song-count").evaluate("el => el.remove()")
        self.page.locator("#next").evaluate("el => el.remove()")
        with self.assertRaisesRegex(RuntimeError, "一覧の終端を確認できません"):
            fd.iter_workspace_rows(self.page)

    def test_missing_count_with_disabled_next_preserves_existing_behavior(self):
        self.page.locator("#song-count").evaluate("el => el.remove()")
        self.assertEqual(fd.iter_workspace_rows(self.page),
                         [dict(song, page_no=1) for song in SONGS])

    def test_count_mismatch_raises(self):
        for lang, html in WORKSPACES:
            with self.subTest(lang=lang):
                self.page.set_content(html)
                self.page.locator("#song-count").evaluate(
                    "(el, text) => el.textContent = text", "3曲" if lang == "ja" else "3 songs")
                with self.assertRaisesRegex(RuntimeError, "画面総曲数と取得件数が一致しません: expected=3 rows=2"):
                    fd.iter_workspace_rows(self.page)


if __name__ == "__main__":
    unittest.main()
