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
        self._pages = []

    def page(self) -> Any:
        # cdp は利用者が開いているタブを奪わない。
        if self.owned and self.context.pages:
            return self.context.pages[0]
        page = self.context.new_page()
        if not self.owned:
            self._pages.append(page)
        return page

    def close(self) -> None:
        if self.owned:
            self.context.close()
        else:
            for page in self._pages:
                try:
                    page.close()
                except Exception:
                    pass
            self._pages.clear()


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
