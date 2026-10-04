# SUNO 正式 Chrome 接続 + 拡張フック直接実行 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `codex-driven-development`（このワークスペースでは superpowers:subagent-driven-development の代わり）to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** SUNO 自動化のブラウザ接続先を設定画面から選べるようにし（既定は Google Chrome 本体 + 専用プロファイル）、DL の復号フックを拡張の `page-hook.js` の直接実行へ置き換え、セレクタを現行 DOM に合わせる。

**Architecture:** 3 か所に重複しているブラウザ起動を新規 `Python/suno_browser.py` に集約し、モード（chrome / chromium / cdp）は既存のマシン別設定 `~/.config/{app_id}/suno_config.json` から読む。`suno_fast_dl.py` は移植 JS を捨て、拡張の `page-hook.js` を無改変で読み込み、`content.js` 役の小さなアダプタ JS で既存の Python 側契約（`__sunoFastSetContext` / `__sunoFastGetState` / `__sunoFastSendResult` / `__sunoFastClear`）を維持する。

**Tech Stack:** Python 3.9.6（`/usr/bin/python3`。`X | None` は `from __future__ import annotations` 必須）、Playwright sync API、FastAPI + pydantic 2.13、unittest、単一ファイル UI `web/static/index.html`。

**Spec:** `docs/superpowers/specs/2026-10-04-suno-chrome-direct-hook-design.md`

## Global Constraints

- 作業ディレクトリは `/Users/caruvi/Library/CloudStorage/GoogleDrive-abe_kota@caruvistar.jp/共有ドライブ/DEV/_claude`、ブランチ `feature/suno-chrome-direct-hook`。
- テストは `cd Python && python3 -m unittest tests.<module> -v`。新規テストは `Python/tests/` に unittest で書く。
- UI・ログ・コード・コミットに絵文字を使わない（既存行の絵文字はそのまま触らない）。
- `page-hook.js` は読み込むだけで **改変しない**。読み込み元の既定は `DEV/Script/suno_fast_m4a_monomono/page-hook.js`、環境変数 `APP_SUNO_HOOK_PATH` で上書き。
- ブラウザ起動に暗黙のフォールバックを入れない。失敗時はモード・プロファイル・原因を示して例外。
- `chrome` と `chromium` のプロファイルフォルダを共有させない。既定は `~/.config/orzz/chrome_profile` と `~/.config/orzz/chromium_profile`。
- `cdp_port` は 1024〜65535、既定 9222。`cdp` では headless を無視し、終了時にブラウザ・既存タブを閉じない。
- `Python/app.py`・`web/static/index.html`・`Python/start.sh`・`setup_launchd.sh`・`docs/music/wobble-day-production.md` には着手前からの未コミット変更がある。**それらの既存差分を消さない・書き換えない。** 実装者（Codex）はコミットしない。コミットは司令塔（Claude）が検証後に、今回の hunk だけを選んで行う。
- 対象外: 生成パラメータ、ネイティブ Instrumental、DL 設定の UI 化、Studio 書き出し、`docs/music/wobble-day-production.md`、`Python/scripts/wobble_suno_bridge.py`。
- 実際の Create（クレジット消費）・曲の削除・SUNO アカウント設定の変更は行わない。

## Review Focus

1. プロファイルパスが `~` 始まり・末尾スラッシュ・相対パスで来ても、他モードの既定フォルダとの共有を検出できること（Task 1）。
2. 古い設定ファイル（`browser_mode` 無し / 空文字 / `cdp_port` が文字列 `"9222"`）で既定値へ落ち、例外にならないこと（Task 1）。
3. `cdp` で接続先に到達できない・context が 0 個のとき、起動方法を案内する明示エラーになること。利用者の既存タブを乗っ取らないこと（Task 1）。
4. 拡張の更新で `page-hook.js` の `SOURCE` 定数が見つからない形になったとき、黙って待ち続けず DL 開始前に失敗すること（Task 5）。
5. `__sunoFastClear` 後に遅れて届いた `fast-export-result`、および別 sessionId の結果が、結果テーブルへ入り込まないこと（Task 5）。

---

## File Structure

| ファイル | 役割 |
|---|---|
| `Python/suno_browser.py`（新規） | 接続設定の解決・検証、モード別の context 取得、`BrowserSession` |
| `Python/tests/test_suno_browser.py`（新規） | 上記の単体テスト（Playwright はフェイク） |
| `Python/suno_auto_create.py` | 起動 2 か所を `open_suno_context` へ、CLI 3 フラグ追加 |
| `Python/suno_queue.py` | `launch_browser` を `open_suno_context` へ |
| `Python/app_core.py` | 3 キーをグローバル保存キーへ |
| `Python/app.py` | `SunoConfigUpdate` に 3 フィールドと検証 |
| `Python/tests/test_suno_browser_config.py`（新規） | 保存先振り分けの単体テスト |
| `web/static/index.html` | 「ブラウザ接続先」ブロック、読み込み・保存 |
| `Python/suno_fast_dl.py` | 移植 JS 削除、`load_page_hook`、アダプタ JS |
| `Python/tests/test_suno_fast_hook.py`（新規） | 読み込みの単体テスト + 実フックとアダプタの往復テスト |
| `docs/music/suno-dom-snapshot-2026-10-04.md`（新規） | 現行 DOM の記録とセレクタ突合表 |

---

### Task 1: `suno_browser.py` 接続設定の解決と context 取得

**Files:**
- Create: `Python/suno_browser.py`
- Test: `Python/tests/test_suno_browser.py`

**Interfaces:**
- Consumes: なし
- Produces:
  - `MODES = ("chrome", "chromium", "cdp")`、`DEFAULT_MODE = "chrome"`、`DEFAULT_CDP_PORT = 9222`
  - `default_profile_dir(mode: str) -> Path`
  - `class BrowserConfigError(ValueError)`、`class BrowserLaunchError(RuntimeError)`
  - `resolve_browser_settings(settings: dict) -> dict` 返り値キー: `mode: str`、`profile_dir: str`（cdp では `""`）、`cdp_port: int`、`headless: bool`
  - `apply_cli_overrides(settings: dict, mode=None, profile_dir=None, cdp_port=None) -> None`（None 以外だけ `browser_mode` / `browser_profile_dir` / `cdp_port` へ書く）
  - `class BrowserSession`: 属性 `context`、`mode: str`、`owned: bool`。メソッド `page()`（owned なら `context.pages[0]` があればそれ、なければ `new_page()`。cdp では常に `new_page()`）、`close()`（owned のときだけ `context.close()`）
  - `open_suno_context(playwright, settings: dict) -> BrowserSession`

- [ ] **Step 1: 失敗するテストを書く**

`Python/tests/test_suno_browser.py`:

```python
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
```

- [ ] **Step 2: 失敗を確認**

Run: `cd Python && python3 -m unittest tests.test_suno_browser -v`
Expected: `ModuleNotFoundError: No module named 'suno_browser'`

- [ ] **Step 3: 実装**

`Python/suno_browser.py`:

```python
"""SUNO 自動化が使うブラウザの接続先を 1 か所で決める。

モード:
  chrome   : Google Chrome 本体を自動化専用プロファイルで起動する（既定）
  chromium : Playwright 同梱 Chromium を従来のプロファイルで起動する
  cdp      : 起動済み Chrome へリモートデバッグポートで接続する

暗黙のフォールバックはしない。プロファイルがモードごとに分かれるため、
黙って切り替わるとログイン状態が変わり原因が追えなくなる。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

MODES = ("chrome", "chromium", "cdp")
DEFAULT_MODE = "chrome"
DEFAULT_CDP_PORT = 9222
_PROFILE_ROOT = Path.home() / ".config" / "orzz"
_DEFAULT_PROFILE_NAMES = {"chrome": "chrome_profile", "chromium": "chromium_profile"}
_LAUNCH_ARGS = ["--disable-blink-features=AutomationControlled", "--no-first-run"]


class BrowserConfigError(ValueError):
    """接続設定の値が不正。"""


class BrowserLaunchError(RuntimeError):
    """ブラウザの起動または接続に失敗。"""


def default_profile_dir(mode: str) -> Path:
    return _PROFILE_ROOT / _DEFAULT_PROFILE_NAMES[mode]


def _normalize_dir(value: str) -> Path:
    return Path(value).expanduser().resolve()


def resolve_browser_settings(settings: dict) -> dict:
    mode = str(settings.get("browser_mode") or DEFAULT_MODE).strip().lower()
    if mode not in MODES:
        raise BrowserConfigError(
            "browser_mode は %s のいずれかにしてください: %r" % (" / ".join(MODES), mode))
    raw_port = settings.get("cdp_port")
    if raw_port in (None, ""):
        port = DEFAULT_CDP_PORT
    else:
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            raise BrowserConfigError("cdp_port は整数にしてください: %r" % (raw_port,))
    if not 1024 <= port <= 65535:
        raise BrowserConfigError("cdp_port は 1024〜65535 にしてください: %d" % port)
    if mode == "cdp":
        return {"mode": mode, "profile_dir": "", "cdp_port": port, "headless": False}
    raw_dir = str(settings.get("browser_profile_dir") or "").strip()
    profile = _normalize_dir(raw_dir) if raw_dir else default_profile_dir(mode)
    for other in _DEFAULT_PROFILE_NAMES:
        if other != mode and profile == _normalize_dir(str(default_profile_dir(other))):
            raise BrowserConfigError(
                "%s 用のプロファイルは %s モードで使えません: %s" % (other, mode, profile))
    return {"mode": mode, "profile_dir": str(profile), "cdp_port": port,
            "headless": bool(settings.get("headless"))}


def apply_cli_overrides(settings: dict, mode: Optional[str] = None,
                        profile_dir: Optional[str] = None,
                        cdp_port: Optional[int] = None) -> None:
    if mode is not None:
        settings["browser_mode"] = mode
    if profile_dir is not None:
        settings["browser_profile_dir"] = profile_dir
    if cdp_port is not None:
        settings["cdp_port"] = cdp_port


class BrowserSession:
    def __init__(self, context: Any, mode: str, owned: bool):
        self.context = context
        self.mode = mode
        self.owned = owned

    def page(self) -> Any:
        # cdp は利用者が開いているタブを奪わない。
        if self.owned and self.context.pages:
            return self.context.pages[0]
        return self.context.new_page()

    def close(self) -> None:
        if self.owned:
            self.context.close()


def open_suno_context(playwright: Any, settings: dict) -> BrowserSession:
    resolved = resolve_browser_settings(settings)
    mode = resolved["mode"]
    if mode == "cdp":
        endpoint = "http://127.0.0.1:%d" % resolved["cdp_port"]
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint)
        except Exception as exc:
            raise BrowserLaunchError(
                "起動済み Chrome へ接続できません (mode=cdp, %s): %s\n"
                "Chrome を次の引数で起動してください: "
                "--remote-debugging-port=%d --user-data-dir=<自動化専用フォルダ>"
                % (endpoint, exc, resolved["cdp_port"]))
        if not browser.contexts:
            raise BrowserLaunchError(
                "接続先の Chrome に利用できるウィンドウがありません (mode=cdp, %s)" % endpoint)
        return BrowserSession(browser.contexts[0], mode, owned=False)
    kwargs = dict(
        user_data_dir=resolved["profile_dir"],
        headless=resolved["headless"],
        args=list(_LAUNCH_ARGS),
        viewport={"width": 1280, "height": 900},
        ignore_default_args=["--enable-automation"],
        accept_downloads=True,
    )
    if mode == "chrome":
        kwargs["channel"] = "chrome"
    try:
        context = playwright.chromium.launch_persistent_context(**kwargs)
    except Exception as exc:
        raise BrowserLaunchError(
            "ブラウザを起動できません (mode=%s, profile=%s): %s"
            % (mode, resolved["profile_dir"], exc))
    return BrowserSession(context, mode, owned=True)
```

- [ ] **Step 4: 成功を確認**

Run: `cd Python && python3 -m unittest tests.test_suno_browser -v`
Expected: 16 tests, `OK`

- [ ] **Step 5: コミット（司令塔が実施）**

```bash
git add Python/suno_browser.py Python/tests/test_suno_browser.py
git commit -m "SUNOのブラウザ接続先を解決する共通モジュールを追加"
```

---

### Task 2: 起動 3 か所を `open_suno_context` へ切り替え、CLI フラグを追加

**Files:**
- Modify: `Python/suno_auto_create.py`（`run_browser_automation` の 2327〜2367 行付近、`_run_download_only` の 3977〜3990 行付近、`context.close()` の 2430・2443・2737・4013 行付近、`main()` の argparse と上書き処理）
- Modify: `Python/suno_queue.py`（`launch_browser` 196〜209 行、`parse_args`、`main` の 490〜517 行付近）

**Interfaces:**
- Consumes: Task 1 の `open_suno_context`、`BrowserSession`、`BrowserLaunchError`、`apply_cli_overrides`、`resolve_browser_settings`、`MODES`
- Produces: CLI フラグ `--browser-mode {chrome,chromium,cdp}`、`--browser-profile-dir PATH`、`--cdp-port INT`（`suno_auto_create.py` と `suno_queue.py` の両方）

設定の受け渡し: `suno_auto_create.load_config()` は `~/.config/{app_id}/suno_config.json`（`app_core.SUNO_CONFIG` と同じファイル）を読むため、設定画面で保存した 3 キーは `settings` に自動で入る。`app.py` からフラグを渡す必要はない。

- [ ] **Step 1: `suno_auto_create.py` の import**

ファイル冒頭の import 群に追加:

```python
from suno_browser import BrowserLaunchError, MODES as BROWSER_MODES, apply_cli_overrides, open_suno_context, resolve_browser_settings
```

- [ ] **Step 2: `run_browser_automation` の起動部を置換**

`profile_dir = str(Path.home() / ".config/orzz/chromium_profile")` から、フォールバックを含む `context = None` 〜 「ブラウザが見つかりません」の `return` までを次へ置き換える:

```python
    browser_cfg = resolve_browser_settings(settings)

    with sync_playwright() as p:
        print("\nブラウザを起動中...")
        print(f"  接続先: {browser_cfg['mode']}")
        if browser_cfg["mode"] == "cdp":
            print(f"  ポート: {browser_cfg['cdp_port']}")
        else:
            print(f"  プロファイル: {browser_cfg['profile_dir']}")
        try:
            session = open_suno_context(p, settings)
        except BrowserLaunchError as exc:
            print(f"ブラウザを起動できませんでした: {exc}")
            return
        context = session.context
```

続く init script 登録（`context.add_init_script(...)` 4 行）は変更しない。直後の

```python
        page = context.pages[0] if context.pages else context.new_page()
```

を `page = session.page()` に置き換える。この関数内の `context.close()`（3 か所）をすべて `session.close()` に置き換える。`headless` ローカル変数が他で使われていなければ削除する。

- [ ] **Step 3: `_run_download_only` の起動部を置換**

`profile_dir = ...` と `launch_kwargs = dict(...)` 〜 `except Exception: context = ...channel="chrome"...` を次へ置き換える:

```python
    with sync_playwright() as p:
        session = open_suno_context(p, settings)
        context = session.context
```

`page = context.pages[0] if context.pages else context.new_page()` を `page = session.page()` に、この関数内の `context.close()` を `session.close()` に置き換える。ここは `BrowserLaunchError` を捕まえず送出させる（DL 専用実行は失敗を終了コードで伝える）。

- [ ] **Step 4: `main()` に CLI フラグを追加**

`parser.add_argument("--headless", ...)` の直後に追加:

```python
    parser.add_argument("--browser-mode", choices=list(BROWSER_MODES),
                        help="ブラウザ接続先（chrome=正式Chrome+専用プロファイル / chromium=同梱Chromium / cdp=起動済みChromeへ接続）")
    parser.add_argument("--browser-profile-dir", help="プロファイルフォルダ（省略時はモード別の既定）")
    parser.add_argument("--cdp-port", type=int, help="cdp モードの接続ポート（既定 9222）")
```

`if args.headless: settings["headless"] = True` の直後に追加:

```python
    apply_cli_overrides(settings, mode=args.browser_mode,
                        profile_dir=args.browser_profile_dir, cdp_port=args.cdp_port)
    try:
        resolve_browser_settings(settings)
    except ValueError as exc:
        parser.error(str(exc))
```

- [ ] **Step 5: `suno_queue.py` を置換**

`launch_browser` 関数全体を削除し、冒頭 import に `from suno_browser import MODES as BROWSER_MODES, apply_cli_overrides, open_suno_context` を追加。`parse_args` の `--headless` の後に Step 4 と同じ 3 つの `add_argument` を追加。`main()` の

```python
            context = launch_browser(playwright, args.headless)
            context.add_init_script(suno._SUNO_AUDIO_URL_INTERCEPTOR)
            page = context.pages[0] if context.pages else context.new_page()
```

を次へ置き換える:

```python
            browser_settings = suno.load_config()
            if args.headless:
                browser_settings["headless"] = True
            apply_cli_overrides(browser_settings, mode=args.browser_mode,
                                profile_dir=args.browser_profile_dir, cdp_port=args.cdp_port)
            session = open_suno_context(playwright, browser_settings)
            context = session.context
            context.add_init_script(suno._SUNO_AUDIO_URL_INTERCEPTOR)
            page = session.page()
```

`main()` 冒頭の `context = None` の隣に `session = None` を追加し、`finally` の `if context is not None: context.close()` を `if session is not None: session.close()`（try/except は維持）に置き換える。

- [ ] **Step 6: 検証**

```bash
cd Python
python3 -m py_compile suno_auto_create.py suno_queue.py
grep -n "chromium_profile\|launch_persistent_context\|context.close()" suno_auto_create.py suno_queue.py
python3 suno_auto_create.py --help | grep -A1 "browser-mode\|browser-profile-dir\|cdp-port"
python3 suno_queue.py --help | grep "browser-mode\|cdp-port"
python3 suno_auto_create.py --browser-mode cdp --cdp-port 80 --download-workspace x; echo "exit=$?"
python3 -m unittest tests.test_suno_browser tests.test_wobble_suno_bridge
```

Expected: py_compile 無出力。grep は 0 件。`--help` に 3 フラグが出る。`--cdp-port 80` は `cdp_port は 1024〜65535` を含む usage エラーで `exit=2`（ロック取得・ブラウザ起動より前に止まる）。unittest `OK`。

- [ ] **Step 7: コミット（司令塔が実施）**

```bash
git add Python/suno_auto_create.py Python/suno_queue.py
git commit -m "SUNOのブラウザ起動を共通モジュールへ集約し接続先をCLIで指定可能にする"
```

---

### Task 3: 接続先 3 キーをマシン別設定として保存・検証

**Files:**
- Modify: `Python/app_core.py`（`get_suno_config` の per-channel 無視リスト 858 行付近、`_SUNO_GLOBAL_KEYS` 865 行付近）
- Modify: `Python/app.py`（`SunoConfigUpdate` と `api_update_suno_config`、577〜594 行付近）
- Test: `Python/tests/test_suno_browser_config.py`

**Interfaces:**
- Consumes: Task 1 の `resolve_browser_settings`、`BrowserConfigError`
- Produces: `PUT /api/config/suno` が `browser_mode` / `browser_profile_dir` / `cdp_port` を受け付け、`GET` 側の `get_suno_config()` がそれらを返す。不正値は HTTP 422。

- [ ] **Step 1: 失敗するテストを書く**

`Python/tests/test_suno_browser_config.py`:

```python
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
```

- [ ] **Step 2: 失敗を確認**

Run: `cd Python && python3 -m unittest tests.test_suno_browser_config -v`
Expected: 2 件とも FAIL（`browser_mode` がチャンネル側へ保存される / チャンネル値で上書きされる）。`app_core` の import や patch 対象名でエラーになる場合は、`app_core.py` の実際の関数名（`load_channel_config` / `save_channel_config` / `load_json` / `save_json`）を確認してテストの patch を合わせる。

- [ ] **Step 3: `app_core.py` を実装**

`_SUNO_GLOBAL_KEYS` を `get_suno_config` より前へ移し、両方が同じ集合を参照するようにする:

```python
# グローバル（マシン別）に保つキー。provider/model は per-channel 保存可（D11）。
# ブラウザ接続先はプロファイルパスやポートが実行マシンに依存するためマシン別。
_SUNO_GLOBAL_KEYS = {"api_key", "claude_cli", "codex_cli", "headless",
                     "browser_mode", "browser_profile_dir", "cdp_port"}
```

`get_suno_config` 内の

```python
        if k in ("api_key", "claude_cli", "codex_cli", "headless"):
            continue
```

を `if k in _SUNO_GLOBAL_KEYS:` に置き換える。`get_suno_config` と `save_suno_config_smart` の docstring のキー列挙に 3 キーを追記する。

- [ ] **Step 4: `app.py` を実装**

`SunoConfigUpdate` に追加（`duration_seconds` の下）:

```python
    browser_mode: Optional[Literal["chrome", "chromium", "cdp"]] = None
    browser_profile_dir: Optional[str] = None
    cdp_port: Optional[int] = Field(default=None, ge=1024, le=65535)
```

`Literal` が未 import なら `from typing import` 行へ追加する。`api_update_suno_config` を次にする:

```python
@app.put("/api/config/suno")
def api_update_suno_config(update: SunoConfigUpdate):
    patch = {k: v for k, v in update.dict(exclude_none=True).items()}
    if any(k in patch for k in ("browser_mode", "browser_profile_dir", "cdp_port")):
        from suno_browser import BrowserConfigError, resolve_browser_settings
        try:
            resolve_browser_settings({**get_suno_config(), **patch})
        except BrowserConfigError as exc:
            raise HTTPException(422, str(exc))
    save_suno_config_smart(patch)
    return {"status": "ok", "config": get_suno_config()}
```

`app.py` の既存の未コミット差分（fal-video / layer-motion 関連）には触れない。

- [ ] **Step 5: 成功を確認**

```bash
cd Python
python3 -m unittest tests.test_suno_browser_config tests.test_suno_browser -v
python3 -m py_compile app.py app_core.py
python3 - <<'PY'
from pydantic import ValidationError
import app
for bad in ({"browser_mode": "firefox"}, {"cdp_port": 80}):
    try:
        app.SunoConfigUpdate(**bad); print("NG accepted", bad)
    except ValidationError:
        print("OK rejected", bad)
print(app.SunoConfigUpdate(browser_mode="cdp", cdp_port=9333).dict(exclude_none=True))
PY
```

Expected: unittest `OK`、`OK rejected` が 2 行、最後に `{'browser_mode': 'cdp', 'cdp_port': 9333}`。

- [ ] **Step 6: コミット（司令塔が実施。`app.py` は今回の hunk だけ）**

```bash
git add Python/app_core.py Python/tests/test_suno_browser_config.py
# app.py は既存の未コミット差分と分けて、今回の hunk だけを git apply --cached で載せる
git commit -m "SUNOのブラウザ接続先をマシン別設定として保存し不正値を拒否する"
```

---

### Task 4: 設定画面に「ブラウザ接続先」を追加

**Files:**
- Modify: `web/static/index.html`（「SUNO 設定」カード 2916〜2980 行付近、読み込み 4385〜4397 行付近、自動保存 4750〜4764 行付近、手動保存 4967〜4980 行付近、自動保存対象 id 一覧 4797〜4799 行付近）

**Interfaces:**
- Consumes: Task 3 の `PUT /api/config/suno`（3 キー）、設定読み込みで得られる `s.browser_mode` / `s.browser_profile_dir` / `s.cdp_port`
- Produces: 要素 id `cfgSunoBrowserMode`、`cfgSunoBrowserProfile`、`cfgSunoCdpPort`、関数 `onCfgSunoBrowserModeChange()`、`sunoBrowserPatch()`

- [ ] **Step 1: マークアップを追加**

「一括生成モード」チェックボックスの `<div class="ff">...</div>` の直後、カードの閉じ `</div>` の前に追加:

```html
        <div class="ff" style="border-top:1px solid var(--border-default);padding-top:var(--space-3);margin-top:var(--space-3)">
          <label class="fl">ブラウザ接続先</label>
          <select class="fs" id="cfgSunoBrowserMode" onchange="onCfgSunoBrowserModeChange()">
            <option value="chrome">Google Chrome（自動化専用プロファイル）</option>
            <option value="chromium">同梱 Chromium（従来の方式）</option>
            <option value="cdp">起動済みの Chrome へ接続</option>
          </select>
          <div class="fh" id="cfgSunoBrowserModeHint" style="margin-top:var(--space-1)"></div>
        </div>
        <div class="ff" id="cfgSunoBrowserProfileRow">
          <label class="fl">プロファイルフォルダ</label>
          <input type="text" class="fi" id="cfgSunoBrowserProfile" placeholder="空欄で既定のフォルダを使用">
          <div class="fh">このマシンだけの設定です。チャンネルを切り替えても変わりません。</div>
        </div>
        <div class="ff" id="cfgSunoCdpPortRow" style="display:none">
          <label class="fl">接続ポート</label>
          <input type="number" class="fi" id="cfgSunoCdpPort" value="9222" min="1024" max="65535" style="width:140px">
        </div>
```

- [ ] **Step 2: JS を追加**

`onCfgSunoModeChange` 関数定義の近くに追加:

```javascript
const SUNO_BROWSER_HINTS={
  chrome:'Google Chrome を専用プロファイル（既定 ~/.config/orzz/chrome_profile）で起動します。初回だけ、開いたウィンドウで SUNO にログインしてください。',
  chromium:'Playwright 同梱の Chromium を従来のプロファイル（既定 ~/.config/orzz/chromium_profile）で起動します。',
  cdp:'Chrome を --remote-debugging-port=<ポート> --user-data-dir=<専用フォルダ> で起動しておく必要があります。普段使いのプロファイルには接続できません。終了してもブラウザは閉じません。'
};
function onCfgSunoBrowserModeChange(){
  const mode=document.getElementById('cfgSunoBrowserMode')?.value||'chrome';
  const cdp=mode==='cdp';
  const profileRow=document.getElementById('cfgSunoBrowserProfileRow');
  const portRow=document.getElementById('cfgSunoCdpPortRow');
  const hint=document.getElementById('cfgSunoBrowserModeHint');
  if(profileRow)profileRow.style.display=cdp?'none':'';
  if(portRow)portRow.style.display=cdp?'':'none';
  if(hint)hint.textContent=SUNO_BROWSER_HINTS[mode]||'';
}
function sunoBrowserPatch(){
  const modeEl=document.getElementById('cfgSunoBrowserMode');
  if(!modeEl)return {};
  const port=parseInt(document.getElementById('cfgSunoCdpPort')?.value||'9222');
  return {
    browser_mode:modeEl.value||'chrome',
    browser_profile_dir:(document.getElementById('cfgSunoBrowserProfile')?.value||'').trim(),
    cdp_port:Number.isFinite(port)?port:9222,
  };
}
```

- [ ] **Step 3: 読み込みに追加**

`if($('cfgSunoBatch'))$('cfgSunoBatch').checked=!!s.loop_batch;` の直後に追加:

```javascript
    if($('cfgSunoBrowserMode')){
      $('cfgSunoBrowserMode').value=s.browser_mode||'chrome';
      $('cfgSunoBrowserProfile').value=s.browser_profile_dir||'';
      $('cfgSunoCdpPort').value=s.cdp_port||9222;
      onCfgSunoBrowserModeChange();
    }
```

- [ ] **Step 4: 保存 2 か所と自動保存対象に追加**

自動保存（4750 行付近）と手動保存（4967 行付近）の両方で、`const sunoPatch={...};` の直後に `Object.assign(sunoPatch,sunoBrowserPatch());` を 1 行追加する。両方とも、直後の `fetch('/api/config/suno', ...)` を、失敗を握りつぶさない形に変える:

```javascript
    try{
      const sr=await fetch('/api/config/suno',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(sunoPatch)});
      if(!sr.ok){const detail=await sr.json().catch(()=>({}));toast('SUNO 設定を保存できませんでした: '+(detail.detail||('HTTP '+sr.status)),'err')}
    }catch(e){}
```

`toast` の実際の関数名と引数は `index.html` 内の既存の通知呼び出しに合わせる（同ファイルで保存失敗を表示している箇所の書き方を使う）。自動保存対象の id 配列（`'cfgSunoCount','cfgSunoInterval','cfgSunoBatch','cfgSunoInstrumentalFill',` の行）へ `'cfgSunoBrowserMode','cfgSunoBrowserProfile','cfgSunoCdpPort',` を追加する。

- [ ] **Step 5: 検証**

```bash
grep -c "cfgSunoBrowserMode" web/static/index.html
grep -n "Object.assign(sunoPatch,sunoBrowserPatch())" web/static/index.html
node -e "
const html=require('fs').readFileSync('web/static/index.html','utf8');
const scripts=[...html.matchAll(/<script(?![^>]*src)[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);
for(const [i,s] of scripts.entries()){try{new Function(s)}catch(e){console.log('script',i,e.message);process.exitCode=1}}
console.log('scripts',scripts.length,'syntax ok');"
git diff --stat web/static/index.html
```

Expected: 1 つ目は 6 以上、2 つ目は 2 行、node は `syntax ok`。画面での実動作（表示切替・保存・再読み込みで値が残ること・不正ポートでエラー表示）は司令塔が Automation Studio を起動してブラウザで確認する。

- [ ] **Step 6: コミット（司令塔が実施。今回の hunk だけ）**

```bash
git commit -m "設定画面からSUNOのブラウザ接続先を選べるようにする"
```

---

### Task 5: 拡張 `page-hook.js` の直接読み込みとアダプタ

**Files:**
- Modify: `Python/suno_fast_dl.py`（docstring、`FAST_DECRYPT_HOOK` 定数 13〜417 行を削除、`install_fast_capture` / `ensure_fast_capture` 420〜437 行）
- Test: `Python/tests/test_suno_fast_hook.py`
- Read only: `DEV/Script/suno_fast_m4a_monomono/page-hook.js`、`DEV/Script/suno_studio_test/content.js`（2000〜2025 行付近の受信処理）

**Interfaces:**
- Consumes: なし（Task 1〜4 と独立）
- Produces:
  - `DEFAULT_HOOK_PATH: Path`（`Path(__file__).resolve().parents[2] / "Script/suno_fast_m4a_monomono/page-hook.js"`）
  - `class HookLoadError(RuntimeError)`
  - `load_page_hook(path=None) -> dict` 返り値キー: `source: str`（ファイル内容そのまま）、`sha256: str`、`path: str`、`message_source: str`（フック内の `SOURCE` 定数値）
  - `build_capture_script(path=None) -> str`（フック本文 + アダプタ）
  - `install_fast_capture(target)`、`ensure_fast_capture(page) -> bool` は名前・引数を変えない
  - ページ内契約は現行と同一: `window.__sunoFastSetContext(context|null)`（Promise を返す）、`__sunoFastGetState(sessionId)`、`__sunoFastSendResult(sessionId)`、`__sunoFastClear(sessionId)`、`__sunoFastResults`

フックのメッセージ仕様（`page-hook.js` を読んで確認済み）:
- フックへ送る: `capture-context {context}`、`mse-capture-on`、`mse-capture-off`、`cancel-session {sessionId, songId}`
- フックから届く: `capture-context-ready {sessionId}`、`fast-progress {context, received, total, stage}`、`fast-export-result {context, blob, bytes, elapsedMs, url}`、`fast-error {context, error}`
- すべて `window.postMessage({source: SOURCE, kind, ...}, '*')`。`SOURCE` は現在 `'suno-fast-decrypt-v092'`。

- [ ] **Step 1: 失敗するテストを書く**

`Python/tests/test_suno_fast_hook.py`:

```python
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

    def test_missing_file_fails_before_download(self):
        with self.assertRaises(fd.HookLoadError) as caught:
            fd.load_page_hook("/nonexistent/page-hook.js")
        self.assertIn("/nonexistent/page-hook.js", str(caught.exception))

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
```

- [ ] **Step 2: 失敗を確認**

Run: `cd Python && python3 -m unittest tests.test_suno_fast_hook -v`
Expected: `AttributeError: module 'suno_fast_dl' has no attribute 'load_page_hook'` などで失敗。

- [ ] **Step 3: 読み込みを実装**

`suno_fast_dl.py` の docstring を次に変更し、`FAST_DECRYPT_HOOK` 定数（`FAST_DECRYPT_HOOK: str = r"""` から対応する `"""` まで）を **丸ごと削除** する。削除前に、その中の `window.__sunoFastSendResult` 関数本体（分割 base64 転送）を控え、Step 4 のアダプタへそのまま移す。

```python
"""SUNO の暗号化音声をページ内で復号し、Python へ分割転送する。

復号フックは拡張機能の page-hook.js を実行時に読み込み、無改変で注入する。
拡張の content.js が担う window.postMessage の送受信だけを、このファイルの
アダプタが肩代わりする。フックの複製は持たない。
"""

import base64
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

DEFAULT_HOOK_PATH = Path(__file__).resolve().parents[2] / "Script" / "suno_fast_m4a_monomono" / "page-hook.js"
_SOURCE_PATTERN = re.compile(r"""const\s+SOURCE\s*=\s*(['"])([^'"\\]+)\1""")
_hook_cache: Dict[str, dict] = {}


class HookLoadError(RuntimeError):
    """拡張の page-hook.js を読み込めない。"""


def load_page_hook(path=None) -> dict:
    hook_path = Path(path or os.environ.get("APP_SUNO_HOOK_PATH") or DEFAULT_HOOK_PATH)
    key = str(hook_path)
    try:
        raw = hook_path.read_bytes()
    except OSError as exc:
        raise HookLoadError("拡張の page-hook.js を読めません: %s (%s)" % (hook_path, exc))
    digest = hashlib.sha256(raw).hexdigest()
    cached = _hook_cache.get(key)
    if cached and cached["sha256"] == digest:
        return cached
    source = raw.decode("utf-8")
    match = _SOURCE_PATTERN.search(source)
    if not match:
        raise HookLoadError(
            "page-hook.js に SOURCE 定数が見つかりません。拡張の仕様が変わった可能性があります: %s" % hook_path)
    hook = {"source": source, "sha256": digest, "path": key, "message_source": match.group(2)}
    _hook_cache[key] = hook
    print("復号フック: %s sha256=%s" % (key, digest), flush=True)
    return hook


def build_capture_script(path=None) -> str:
    import json
    hook = load_page_hook(path)
    return hook["source"] + "\n" + _FAST_CAPTURE_ADAPTER.replace(
        "__MESSAGE_SOURCE__", json.dumps(hook["message_source"]))
```

- [ ] **Step 4: アダプタを実装**

`build_capture_script` より前に定義する。`__sunoFastSendResult` の本体は、削除した移植コードのものを一字一句そのまま使う（`window.__sunoFastChunk` を呼ぶ分割転送）。

```python
# content.js の役割のうち、フックとの postMessage 往復だけを担う。
_FAST_CAPTURE_ADAPTER = r"""
(function () {
  'use strict';
  if (window.__sunoFastAdapterInstalled) return;
  window.__sunoFastAdapterInstalled = true;

  const SOURCE = __MESSAGE_SOURCE__;
  const progressBySession = Object.create(null);
  const cleared = new Set();
  const readyWaiters = [];
  let currentContext = null;
  window.__sunoFastResults = Object.create(null);

  function post(kind, details = {}) {
    window.postMessage({ source: SOURCE, kind, ...details }, '*');
  }

  function clipIdFromUrl(url) {
    const encoded = String(url || '').match(/\/clip\/([^/?#]+?)\.(?:m4a|mp4)(?:[?#]|$)/i)?.[1];
    if (!encoded) return '';
    try {
      return decodeURIComponent(encoded).toLowerCase();
    } catch (_) {
      return encoded.toLowerCase();
    }
  }

  function setError(sessionId, error) {
    if (!sessionId || cleared.has(sessionId)) return;
    if (window.__sunoFastResults[sessionId]?.status === 'done') return;
    window.__sunoFastResults[sessionId] = {
      status: 'error',
      error: error?.message || String(error)
    };
  }

  window.addEventListener('message', (event) => {
    if (event.source !== window) return;
    const data = event.data;
    if (!data || data.source !== SOURCE) return;
    if (data.kind === 'capture-context-ready') {
      for (const waiter of readyWaiters.splice(0)) waiter(data.sessionId || '');
      return;
    }
    const sessionId = data.context?.sessionId;
    if (!sessionId || cleared.has(sessionId)) return;
    if (data.kind === 'fast-progress') {
      progressBySession[sessionId] = { received: data.received || 0, total: data.total || 0 };
    } else if (data.kind === 'fast-error') {
      setError(sessionId, data.error);
    } else if (data.kind === 'fast-export-result') {
      const url = data.url || '';
      Promise.resolve(data.blob?.arrayBuffer()).then((buffer) => {
        if (cleared.has(sessionId)) return;
        if (!buffer) throw new Error('復号結果が空です');
        const bytes = new Uint8Array(buffer);
        progressBySession[sessionId] = { received: bytes.byteLength, total: bytes.byteLength };
        window.__sunoFastResults[sessionId] = { status: 'done', bytes, size: bytes.byteLength, url };
      }).catch((error) => setError(sessionId, error));
    }
  });

  // フックが対象を受理してから解決する。直後の再生クリックと順序が入れ替わらない。
  window.__sunoFastSetContext = function (context) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('復号フックが応答しません')), 3000);
      if (!context) {
        currentContext = null;
        post('mse-capture-off');
        setTimeout(() => { clearTimeout(timer); resolve(); }, 0);
        return;
      }
      currentContext = { ...context };
      cleared.delete(currentContext.sessionId);
      readyWaiters.push((sessionId) => {
        if (sessionId !== currentContext?.sessionId) return;
        clearTimeout(timer);
        resolve();
      });
      post('mse-capture-on');
      post('capture-context', { context: currentContext });
    });
  };

  window.__sunoFastGetState = function (sessionId) {
    const result = window.__sunoFastResults[sessionId];
    const progress = progressBySession[sessionId];
    return {
      status: result?.status || 'pending',
      received: progress?.received || 0,
      total: progress?.total || 0,
      size: result?.size || 0,
      error: result?.error || '',
      clipId: clipIdFromUrl(result?.url)
    };
  };

  window.__sunoFastSendResult = async function (sessionId) {
    /* 削除した移植コードの本体をそのまま置く */
  };

  window.__sunoFastClear = function (sessionId) {
    const songId = currentContext?.sessionId === sessionId ? currentContext.songId : '';
    cleared.add(sessionId);
    post('cancel-session', { sessionId, songId });
    delete window.__sunoFastResults[sessionId];
    delete progressBySession[sessionId];
    if (currentContext?.sessionId === sessionId) currentContext = null;
  };
})();
"""
```

注意: フックの `mse-capture-on` は `captureContext` を消さないが MSE チャンクを消すため、`mse-capture-on` を先、`capture-context` を後に送る（上のとおり）。`/* 削除した移植コードの本体をそのまま置く */` の行は、控えておいた本体で必ず置き換える。置き換え忘れは `test_capture_round_trip_returns_full_plaintext` が検出する。

- [ ] **Step 5: 注入関数を更新**

```python
def install_fast_capture(page) -> None:
    """次回以降のナビゲーションで、拡張のフックとアダプタを注入する。"""
    page.add_init_script(build_capture_script())


def ensure_fast_capture(page) -> bool:
    """現ページの注入を確認し、未注入なら後注入する。"""
    check = "() => !!window.__sunoFastDecryptCaptureInstalled && !!window.__sunoFastAdapterInstalled"
    try:
        if not page.evaluate(check):
            print(" 復号フック未インストール。現ページに後注入します（既存トラフィックの一部は取り逃す可能性）")
            page.evaluate(build_capture_script())
        return bool(page.evaluate(check))
    except HookLoadError:
        raise
    except Exception as e:
        print(f" 復号フックの注入確認に失敗: {e}")
        return False
```

`HookLoadError` は握りつぶさず送出する（ファイル欠落を「注入できませんでした」に丸めない）。

- [ ] **Step 6: 削除した定数の参照を掃除**

```bash
cd Python && grep -rn "FAST_DECRYPT_HOOK\|__sunoFastKeyExportable\|suno-fast-ref" . --include=*.py
```

`FAST_DECRYPT_HOOK` を参照する箇所（`scripts/suno_fast_*_check.py` など）があれば `build_capture_script()` へ置き換える。`__sunoFastKeyExportable` は拡張のフックに無い調査用フラグなので、参照があればその読み取りを削除する。grep が 0 件になること。

- [ ] **Step 7: 成功を確認**

```bash
cd Python
python3 -m unittest tests.test_suno_fast_hook -v
python3 -m py_compile suno_fast_dl.py suno_auto_create.py scripts/suno_fast_one_check.py scripts/suno_fast_rows_check.py scripts/suno_fast_transfer_check.py scripts/suno_fast_vol_check.py
python3 -m unittest discover -s tests -v 2>&1 | tail -5
shasum -a 256 ../../Script/suno_fast_m4a_monomono/page-hook.js
git -C .. status --short ../Script 2>/dev/null; wc -l suno_fast_dl.py
```

Expected: `test_suno_fast_hook` 10 件 `OK`（Chromium を起動できない環境では往復 5 件が skip になるが、このマシンでは実行されること）。全体テスト `OK`。SHA-256 は `8a128e464396c60ee7d39187b31addf9ac926e8d365e23beb1996c2edf12fd08` のまま（フック無改変）。`suno_fast_dl.py` は約 1087 行から 800 行前後へ減る。

- [ ] **Step 8: コミット（司令塔が実施）**

```bash
git add Python/suno_fast_dl.py Python/tests/test_suno_fast_hook.py Python/scripts
git commit -m "SUNOの復号フックを移植コピーから拡張page-hook.jsの直接読み込みへ切り替える"
```

---

### Task 6: 現行 SUNO DOM の採取とセレクタ突合（読み取り専用）

**Files:**
- Create: `docs/music/suno-dom-snapshot-2026-10-04.md`
- Read only: `Python/suno_auto_create.py`（`_ensure_advanced_mode` 3380 行〜 `click_create_button` 3855 行付近、`_click_workspace_card` 1338 行〜 `ensure_workspace` 1618 行付近、`is_suno_logged_in` 2278 行、`_collect_all_song_uuids` 2167 行）、`Python/suno_fast_dl.py`（`_WORKSPACE_ROWS_DOM`、`_PLAY_BUTTON`、`_PAUSE_BUTTON`、`_clear_workspace_filters`）

**Interfaces:**
- Consumes: なし
- Produces: 突合表（Task 7 の入力）。列は「画面 / 要素 / 現行 DOM の事実 / `.py` の現行セレクタ（ファイル:行）/ 判定（一致・不一致・未確認）/ 不一致時の修正案」

実行者は Codex CLI の computer-use / chrome プラグイン。本人の Google Chrome に開いている SUNO を対象にする。

**禁止事項（指示文に必ず含める）:** Create / 作成ボタンを押さない。曲・ワークスペースの削除、名前変更、公開設定、アカウント設定の変更をしない。ダウンロードをしない。フォームへ入力した場合は送信せず元へ戻す。Cookie・トークン・認証情報を読まない・記録しない。ソースコードを変更しない。

- [ ] **Step 1: `/create` を採取**

SUNO の `/create` を開き、アドバンスド（Custom）表示にして、次の各要素について「タグ名 / role / aria-label / placeholder / data-testid / 表示テキスト / 祖先のうち識別に使える属性」を記録する: モード切替タブ（シンプル・アドバンスド・サウンド）、歌詞の種別切替（書く・プロンプト・インスト）、歌詞エディタ、Styles 入力、Exclude 入力、「その他のオプション」開閉、曲名入力、Custom 尺の入力、モデル選択、作成ボタン、ログイン済みを示す要素。表示言語（日本語 / 英語）も記録する。

- [ ] **Step 2: ワークスペース一覧と曲一覧を採取**

`/me/workspaces` でカード・アーカイブ展開の要素を、任意の既存ワークスペース（`/create?wid=...`）で曲行・曲リンク（`/song/<uuid>`）・再生 / 一時停止ボタン・ページ番号入力・次 / 前ページボタン・曲数表示・フィルター解除の要素を、Step 1 と同じ項目で記録する。再生ボタンは **押さずに** 属性だけ読む。

- [ ] **Step 3: 突合表を作る**

`.py` の各セレクタについて、採取した DOM で 1 要素に解決できるかを判定する。特に次を明記する:
- `suno_fast_dl._PLAY_BUTTON` / `_PAUSE_BUTTON` は `aria-label^="Play "` / `"Pause "`（英語のみ）。日本語表示での実際の aria-label
- `_WORKSPACE_ROWS_DOM` の `[data-testid="clip-row"]`、ページ番号 `aria-label`、曲数表示 `^([\d,]+)\s+songs?$`（英語のみ）の日本語表示での実際
- 作成ボタン・曲名 placeholder・「その他のオプション」の日英表記

- [ ] **Step 4: 検証（司令塔）**

`docs/music/suno-dom-snapshot-2026-10-04.md` が存在し、上の全要素が表に載っていること。`git status --short` でこのファイル以外に変更が無いこと。Codex の実行ログに Create・削除・DL の操作が無いこと。

- [ ] **Step 5: 本人へ報告（ゲート）**

不一致の一覧と修正案を本人へ示し、Task 7 で直す範囲の承認を得る。不一致が 0 件なら Task 7 は実施しない。

- [ ] **Step 6: コミット（司令塔が実施）**

```bash
git add docs/music/suno-dom-snapshot-2026-10-04.md
git commit -m "SUNOの現行DOMを記録しPlaywrightセレクタと突合する"
```

---

### Task 7: 突合で不一致だったセレクタの修正

**Files:**
- Modify: Task 6 の表で「不一致」かつ本人が承認した行に載っている `Python/suno_auto_create.py` / `Python/suno_fast_dl.py` の該当行のみ
- Test: 該当セレクタが JS 文字列内にある場合は `Python/tests/test_suno_fast_hook.py` と同じ方式のローカル HTML テストを `Python/tests/test_suno_selectors.py` に追加

**Interfaces:**
- Consumes: Task 6 の突合表
- Produces: 関数名・引数・返り値は変えない（セレクタと候補文字列だけを変える）

このタスクの具体的な変更行は Task 6 の結果で決まる。実装指示は Task 6 完了後に、突合表の該当行（現行セレクタ・採取した DOM の事実・修正後セレクタ）をそのまま貼って発行する。指示に含める固定ルール:

- 既存の日英両対応の書き方（`_ui_text_match` と候補配列、`aria-label` の正規表現）を踏襲し、英語側を消さずに日本語側を足す。
- 採取した DOM の HTML 断片をテストのフィクスチャにし、修正前は該当要素を取れず失敗、修正後は 1 要素に解決することを unittest で示す。
- 「一致」と判定された行は変更しない。リファクタリングをしない。

- [ ] **Step 1: 不一致ごとに、採取 DOM 断片をフィクスチャにした失敗テストを書く**
- [ ] **Step 2: `cd Python && python3 -m unittest tests.test_suno_selectors -v` で失敗を確認**
- [ ] **Step 3: 表の修正案どおりにセレクタを修正**
- [ ] **Step 4: 同コマンドで成功、`python3 -m unittest discover -s tests` が `OK`、`python3 -m py_compile suno_auto_create.py suno_fast_dl.py` が無出力**
- [ ] **Step 5: コミット（司令塔が実施）** `git commit -m "SUNOの現行UIに合わせて不一致だったセレクタを修正する"`

---

### Task 8: 実機検証（司令塔が実施。Create はしない）

**Files:** なし（検証記録は本人への報告に含める）

- [ ] **Step 1: 全体テスト**

```bash
cd Python && python3 -m unittest discover -s tests -v 2>&1 | tail -6
```

Expected: `OK`

- [ ] **Step 2: 設定画面**

Automation Studio を起動し、設定画面の「SUNO 設定」で接続先を切り替えて表示（プロファイル欄 / ポート欄）が入れ替わること、保存後の再読み込みで値が残ること、`~/.config/{app_id}/suno_config.json` に 3 キーが入りチャンネルの `.app_channel_config.json` に入らないことを確認する。

- [ ] **Step 3: 接続 3 モード**

`chrome`: `python3 suno_auto_create.py --browser-mode chrome --download-workspace <既存ワークスペース> --download-dir <scratchpad>` を実行。初回は開いた Chrome で **本人が SUNO にログイン** する（認証情報の入力は本人が行う）。ログに `接続先: chrome` とプロファイルパスが出ること。
`chromium`: 同コマンドを `--browser-mode chromium` で実行し、従来プロファイルで動くこと。
`cdp`: Chrome を `--remote-debugging-port=9222 --user-data-dir=<専用フォルダ>` で起動してから `--browser-mode cdp` で実行し、終了後もその Chrome が開いたままであること。未起動時は案内つきのエラーになること。

- [ ] **Step 4: DL の実ファイル検証**

Step 3 の `chrome` 実行のログに `復号フック: .../page-hook.js sha256=8a128e46...` が出ること。保存された各ファイルについて `ffprobe` で音声ストリームと実尺、`ffmpeg -v error -i <file> -f null -` で全デコード成功、`.suno_fast_download.json` の `expected` と `saved` 件数の一致、`failed` が空であることを確認する。

- [ ] **Step 5: フォーム投入の読み戻し（Create 直前まで）**

`submit_song_to_suno` の Create クリック手前までを実行する検証スクリプトを scratchpad に置き、歌詞・Styles・曲名・Custom 尺を投入して画面の値を読み戻し、原稿と一致することを確認する。Create は押さない。

- [ ] **Step 6: 本人へ報告**

各 Step の結果、未実施の項目（実 Create）、push の有無を報告する。実 Create での通し確認を行うかは本人に確認する。

---

## Self-Review 結果

- Spec 1（接続）→ Task 1・2、Spec 2（設定: 保存・API・受け渡し・UI）→ Task 2・3・4、Spec 3（フック直接実行・アダプタ）→ Task 5、Spec 4（DOM 突合）→ Task 6・7、Spec 5（エラー処理）→ Task 1・5 のテスト、Spec 6（検証）→ 各 Task と Task 8。
- Spec では `/api/suno/start` 等から設定を渡すとしていたが、`suno_auto_create.load_config()` が同じ設定ファイルを直接読むため、`app.py` からのフラグ受け渡しは不要（Task 2 に明記）。
- Task 7 の具体的な変更行は Task 6 の採取結果に依存するため、Task 6 完了後に指示を確定する（ゲートとして明記）。
