"""
Google Flow 自動化スクリプト（Playwright 版）

Flow で新規プロジェクトを作成 → Nano Banana 2 / 16:9 / x4 で画像を生成 →
全4枚を 2K でダウンロードする流れを Python で再現したもの。

## 使い方

    # 1. ログインだけ（初回・セッション切れ時）
    python3 flow_automation.py --login-only

    # 2. プロンプトを指定して生成
    python3 flow_automation.py --prompt "cinematic photo of ..."

    # 3. プロンプトをファイルから
    python3 flow_automation.py --prompt-file /tmp/prompt.txt

    # 4. Claude CLI にプロンプトを生成させてから実行
    python3 flow_automation.py --suggest-prompt "lounge jazz cafe at dusk"

    # 5. 保存先・枚数を指定
    python3 flow_automation.py --prompt "..." \\
        --output-dir "/path/to/78_orzz_260420/flow" \\
        --count x4 --aspect 16:9 --resolution 2K

## 前提

    pip install playwright
    playwright install chromium

Google Flow はログインが必要。`--user-data-dir` で永続プロファイルを使う。
初回だけ `--login-only` で手動ログインしておく。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from playwright.async_api import (
    BrowserContext,
    Download,
    Page,
    async_playwright,
)

# ───────────────────────────────────────────────────────────────
# デフォルト設定
# ───────────────────────────────────────────────────────────────

FLOW_URL = "https://labs.google/fx/ja/tools/flow"
DEFAULT_PROJECT_NAME = "orzz_auto"

DEFAULT_ASPECT = "16:9"      # 16:9 / 4:3 / 1:1 / 3:4 / 9:16
DEFAULT_COUNT = "x4"         # x1 / x2 / x3 / x4 — マスター設定 (master_settings.flow.default_count) で上書き可
DEFAULT_MODEL = "Nano Banana 2"
DEFAULT_RESOLUTION = "2K"    # 1K / 2K

def _master_flow_count() -> str:
    """master_settings.json の flow.default_count を "xN" 形式で返す。無ければ DEFAULT_COUNT。"""
    try:
        p = Path.home() / ".config/orzz/master_settings.json"
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            n = int((data.get("flow") or {}).get("default_count") or 0)
            if 1 <= n <= 8:
                return f"x{n}"
    except Exception:
        pass
    return DEFAULT_COUNT

USER_DATA_DIR = Path.home() / ".flow-playwright-profile"
DEFAULT_DOWNLOAD_DIR = Path.home() / "Downloads"
USERSCRIPT_PATH = Path(__file__).parent / "flow_userscript.js"

# 注入済みスクリプト (goto 後の再注入用)
_USERSCRIPT_CACHE: str | None = None

# DL ハンドラ (context.on('page') + page.on('download') で全ページに仕込む) 用の状態。
# userscript は GM_download / <a download> を popup タブで発火するため、
# page レベルの expect_download では受け取れない。context 全体で常時 listen する。
_DL_SAVED: list[Path] = []
_DL_OUT_DIR: Path = Path.home() / "Downloads"
_DL_PREFIX: str = "flow"
_DL_RESOLUTION: str = "2K"
_DL_TASKS: list[asyncio.Task] = []


GM_POLYFILL = """
(function(){
    const LS_PREFIX = 'gm_';
    window.GM_getValue = function(key, def){
        try {
            const v = localStorage.getItem(LS_PREFIX + key);
            if (v === null) return def;
            return JSON.parse(v);
        } catch(e) { return def; }
    };
    window.GM_setValue = function(key, value){
        try { localStorage.setItem(LS_PREFIX + key, JSON.stringify(value)); } catch(e) {}
    };
    window.GM_registerMenuCommand = function(name, fn){ return name; };
    window.GM_unregisterMenuCommand = function(id){};
    window.GM_openInTab = function(url, opts){
        return window.open(url, '_blank');
    };
    console.log('[Playwright] Tampermonkey polyfills installed');
})();
"""


# ───────────────────────────────────────────────────────────────
# 共通ヘルパ
# ───────────────────────────────────────────────────────────────

def _log(msg: str) -> None:
    print(f"[Flow] {msg}", flush=True)


def os_is_mac() -> bool:
    import platform
    return platform.system() == "Darwin"


async def close_splash_if_present(page: Page) -> None:
    close_btn = page.locator('button[aria-label="Close"], [aria-label="閉じる"]').first
    try:
        await close_btn.wait_for(state="visible", timeout=3000)
        await close_btn.click()
    except Exception:
        pass


async def ensure_logged_in(page: Page, timeout_ms: int = 300_000) -> bool:
    """ログイン状態を確認。未ログインなら入力待ち。"""
    await page.goto(FLOW_URL, wait_until="domcontentloaded")
    await close_splash_if_present(page)

    login_btn = page.get_by_role("button", name=re.compile(r"ログイン|Sign in|Log in", re.I))
    try:
        await login_btn.first.wait_for(state="visible", timeout=3000)
        _log("未ログイン状態を検出。ブラウザでログインしてください。")
        _log(f"最大 {timeout_ms // 1000}s 待機します…")
        new_project = page.get_by_role("button", name=re.compile(r"新しいプロジェクト|New project"))
        await new_project.first.wait_for(state="visible", timeout=timeout_ms)
        _log("ログイン完了を検出しました。")
        return True
    except Exception:
        try:
            new_project = page.get_by_role("button", name=re.compile(r"新しいプロジェクト|New project"))
            await new_project.first.wait_for(state="visible", timeout=10_000)
            _log("ログイン済みセッションを確認。")
            return True
        except Exception:
            _log("⚠ ダッシュボードを検出できませんでした。手動で確認してください。")
            return False


async def create_new_project(page: Page) -> None:
    new_project = page.get_by_role("button", name=re.compile(r"新しいプロジェクト|New project"))
    await new_project.first.click()
    await page.wait_for_url(re.compile(r"/project/[0-9a-f-]+"), timeout=30_000)
    # プロジェクト画面ロード直後に userscript を注入（SPA 遷移では init_script が再走しない）
    await page.wait_for_load_state("domcontentloaded", timeout=10_000)
    await _ensure_userscript_on_page(page)


async def find_existing_project(page: Page, name: str) -> bool:
    """ダッシュボード上で `name` と一致する既存プロジェクトカードを探してクリック。
    見つかったら True を返す。プロジェクト画面 (/project/<uuid>) への遷移待ちまで行う。"""
    # ダッシュボードでないなら戻す
    if "/project/" in (page.url or "") or "/edit/" in (page.url or ""):
        await page.goto(FLOW_URL, wait_until="domcontentloaded")
        await close_splash_if_present(page)

    # プロジェクトカードのリンクを集めて name 一致を探す
    # 複数戦略: textContent / aria-label / 子孫 textContent
    target_url = await page.evaluate(
        """(name) => {
            const links = [...document.querySelectorAll('a[href*="/project/"]')];
            const norm = (s) => (s || '').replace(/\\s+/g, ' ').trim();
            // 完全一致 → 部分一致 → aria-label
            for (const matcher of [
                (a) => norm(a.textContent) === name,
                (a) => norm(a.getAttribute('aria-label')) === name,
                (a) => norm(a.textContent).includes(name),
                (a) => norm(a.getAttribute('aria-label')).includes(name),
            ]) {
                for (const a of links) {
                    if (matcher(a)) return a.href;
                }
            }
            return null;
        }""",
        name,
    )
    if not target_url:
        _log(f"既存プロジェクト '{name}' は未検出 — 新規作成する")
        return False

    _log(f"既存プロジェクト '{name}' を検出: {target_url}")
    await page.goto(target_url, wait_until="domcontentloaded")
    try:
        await page.wait_for_url(re.compile(r"/project/[0-9a-f-]+"), timeout=15_000)
    except Exception:
        pass
    await _ensure_userscript_on_page(page)
    return True


async def open_or_create_project(page: Page, name: str) -> bool:
    """同名プロジェクトがあれば開き、無ければ新規作成 → リネーム。
    戻り値: True = 既存を開いた / False = 新規作成した"""
    if await find_existing_project(page, name):
        return True
    await create_new_project(page)
    await rename_project(page, name)
    return False


async def click_new_batch_button(page: Page) -> bool:
    """同一プロジェクト内で新バッチを開始するボタン (aria-label='add_2 作成')。"""
    candidates = [
        'button[aria-label*="add_2"][aria-label*="作成"]',
        'button[aria-label*="add_2"][aria-label*="Create" i]',
        'button[aria-label^="add_2"]',
    ]
    for sel in candidates:
        try:
            btn = page.locator(sel).first
            if await btn.count():
                await btn.click(timeout=3000)
                _log(f"新バッチ開始: {sel}")
                return True
        except Exception:
            continue
    return False


async def rename_project(page: Page, name: str) -> None:
    """`nav input` のプロジェクト名を直接 fill → `done 完了` ボタンで確定。
    (DevTools Recorder で確認済みセレクタ)"""
    try:
        nav_input = page.locator('nav input').first
        await nav_input.wait_for(state="visible", timeout=8000)
        await nav_input.click()
        await nav_input.fill(name)
        await asyncio.sleep(0.2)
        # 完了ボタン: nav 内の最初のボタン (aria-label="done 完了")
        done = page.locator('nav button[aria-label*="完了"], nav button[aria-label*="done" i]').first
        if await done.count():
            await done.click()
        else:
            done = page.locator('nav > div > div button').first
            if await done.count():
                await done.click()
            else:
                await page.keyboard.press("Enter")
        _log(f"プロジェクトを '{name}' にリネーム")
    except Exception as e:
        _log(f"⚠ リネーム失敗（無視して続行）: {e}")


async def upload_reference_image(page: Page, image_path: Path) -> None:
    """参照画像を Flow にアップロード。

    優先パス: プロンプト欄の「メディアを追加」→「画像をアップロード」メニュー
    から `<input type="file">` を露出させて set_input_files。
    フォールバック: ページ内の隠し `<input type="file">` を直接掴む。
    """
    if not image_path.exists():
        raise FileNotFoundError(f"参照画像が見つかりません: {image_path}")

    # 「メディアを追加」ボタン → 「画像をアップロード」メニュー経由
    # (Recorder で確認したパス。aria-label のバリエーションあり)
    add_media_candidates = [
        'button[aria-label*="メディアを追加"]',
        'button[aria-label*="Add media" i]',
        'button[aria-label*="add" i][aria-label*="media" i]',
    ]
    opened = False
    for sel in add_media_candidates:
        try:
            btn = page.locator(sel).first
            if await btn.count():
                await btn.click(timeout=3000)
                _log(f"メディア追加メニュー: {sel}")
                opened = True
                break
        except Exception:
            continue

    if opened:
        try:
            async with page.expect_file_chooser(timeout=8000) as fc_info:
                hit = await _click_by_text_variants(
                    page,
                    ["画像をアップロード", "Upload image", "Upload"],
                    roles=["menuitem", "menuitemradio", "option", "button"],
                    scope_popover=True,
                )
                if not hit:
                    raise RuntimeError("『画像をアップロード』未検出")
            file_chooser = await fc_info.value
            await file_chooser.set_files(str(image_path))
            _log(f"参照画像アップロード (file_chooser): {image_path}")
        except Exception as e:
            _log(f"  file_chooser ルート失敗: {e} — input[type=file] にフォールバック")
            file_input = page.locator('input[type="file"]').first
            await file_input.set_input_files(str(image_path))
            _log(f"参照画像アップロード (input fallback): {image_path}")
    else:
        # フォールバック: 隠れた input[type=file] に直接渡す
        file_input = page.locator('input[type="file"]').first
        await file_input.set_input_files(str(image_path))
        _log(f"参照画像アップロード (input direct): {image_path}")

    thumb = page.locator(f'img[alt*="{image_path.name}"], img[src^="blob:"]').first
    try:
        await thumb.wait_for(state="visible", timeout=60_000)
        _log("参照画像サムネの表示を確認")
    except Exception as e:
        _log(f"⚠ サムネ表示を確認できず（続行）: {e}")


async def _read_pill_aria(page: Page, model_name: str) -> str:
    """設定ピルの aria-label を返す (例: '🍌 Nano Banana 2 crop_16_9 x4')。"""
    return await page.evaluate(
        """(model) => {
            const btns = document.querySelectorAll('button[aria-label], [role="button"][aria-label]');
            for (const b of btns) {
                const a = b.getAttribute('aria-label') || '';
                if (a.includes(model)) return a;
            }
            // id^="radix-" のボタンから model を含むものをフォールバック検索
            const all = document.querySelectorAll('button[id^="radix-"]');
            for (const b of all) {
                const t = (b.textContent || '').trim();
                if (t.includes(model)) return b.getAttribute('aria-label') || t;
            }
            return '';
        }""", model_name,
    )


async def open_settings_popover(page: Page, model_name: str) -> None:
    """Radix ポップオーバーを開く。トグル式なので開いている場合は無視。"""
    # aria-label に model_name を含む button を特定
    pill = page.locator(f'button[aria-label*="{model_name}"]').first
    if not await pill.count():
        pill = page.get_by_role("button", name=re.compile(re.escape(model_name), re.I)).first
    await pill.click()
    # Radix は role 属性を付けない場合がある。data-state="open" または data-radix-popper で検出
    try:
        await page.wait_for_selector(
            '[data-radix-popper-content-wrapper], [data-state="open"][role], [role="dialog"], [role="menu"]',
            timeout=5000,
        )
    except Exception:
        pass


async def _click_by_text_variants(page: Page, variants: list[str],
                                  roles: list[str] | None = None,
                                  scope_popover: bool = True) -> str | None:
    """ポップオーバー優先 → body 全体にフォールバックで探してクリック。
    - visible かつ横幅 > 0 の button/div[role=...] 全て収集
    - 完全一致 (textContent / aria-label / title) → 部分一致 の順で試す
    - 最初にヒットしたバリアントを返す"""
    roles = roles or ["button", "menuitemradio", "menuitem", "option", "tab", "radio"]
    script = """
    (args) => {
        const {variants, roles, scope_popover} = args;
        // より広い popover 検出（role 以外にも data-state=open / data-radix-popper など）
        const popoverSel = [
            '[role="dialog"]','[role="menu"]','[role="listbox"]',
            '[data-state="open"]','[data-radix-popper-content-wrapper]',
            '.MuiPopover-root','.popover','[class*="popover" i]','[class*="popup" i]',
        ];
        let roots = [document];
        if (scope_popover) {
            const pops = document.querySelectorAll(popoverSel.join(','));
            if (pops.length) roots = [pops[pops.length - 1]];
            else return {ok: false, popover_found: false};
        }
        const selectors = ['button','[role="button"]','a', ...roles.map(r=>`[role="${r}"]`)];
        const seen = new Set();
        const els = [];
        for (const root of roots) {
            for (const sel of selectors) {
                root.querySelectorAll(sel).forEach(e=>{
                    if (seen.has(e)) return;
                    seen.add(e);
                    els.push(e);
                });
            }
        }
        const visible = els.filter(e => {
            const r = e.getBoundingClientRect();
            return r.width > 0 && r.height > 0;
        });
        const matchers = [
            (el, v) => (el.textContent || '').trim() === v,
            (el, v) => (el.getAttribute('aria-label') || '').trim() === v,
            (el, v) => (el.getAttribute('title') || '').trim() === v,
            (el, v) => (el.getAttribute('data-value') || '').trim() === v,
            (el, v) => {
                const t = (el.textContent || '').replace(/\\s+/g,' ').trim();
                return t === v;
            },
            (el, v) => (el.getAttribute('aria-label') || '').toLowerCase().includes(v.toLowerCase()),
            (el, v) => {
                const t = (el.textContent || '').trim();
                return t && (t.startsWith(v) || t.endsWith(v) || t === v);
            },
        ];
        for (const variant of variants) {
            for (const matcher of matchers) {
                for (const el of visible) {
                    if (matcher(el, variant)) {
                        el.scrollIntoView({block:'center'});
                        el.click();
                        return {ok: true, variant, role: el.getAttribute('role')||'', tag: el.tagName,
                                aria: el.getAttribute('aria-label')||'', text: (el.textContent||'').trim().slice(0,40)};
                    }
                }
            }
        }
        return {ok: false, candidates: visible.length};
    }
    """
    # まずポップオーバー内
    result = await page.evaluate(script, {"variants": variants, "roles": roles, "scope_popover": scope_popover})
    if result and result.get("ok"):
        return result.get("variant")
    # フォールバック: body 全体
    if scope_popover:
        result = await page.evaluate(script, {"variants": variants, "roles": roles, "scope_popover": False})
        if result and result.get("ok"):
            _log(f"  (body 全体にフォールバックしてヒット: {result.get('variant')})")
            return result.get("variant")
    return None


async def _click_by_exact_text(page: Page, text: str, roles: list[str] | None = None) -> bool:
    """後方互換ラッパ。"""
    return await _click_by_text_variants(page, [text], roles=roles, scope_popover=False) is not None


async def _dump_popover_html(page: Page, tag: str = "popover") -> None:
    """失敗時診断: ポップオーバー HTML + スクリーンショット + クリック候補一覧 JSON を保存。"""
    ts = int(asyncio.get_event_loop().time())
    try:
        html = await page.evaluate(
            """() => {
                const pops = document.querySelectorAll('[role="dialog"], [role="menu"], [role="listbox"]');
                const pop = pops.length ? pops[pops.length - 1] : null;
                return pop ? pop.outerHTML : document.body.outerHTML.slice(0, 80000);
            }"""
        )
        html_path = Path("/tmp") / f"flow_{tag}_{ts}.html"
        html_path.write_text(html, encoding="utf-8")
        _log(f"DOM ダンプ: {html_path}")
    except Exception as e:
        _log(f"⚠ DOM ダンプ失敗: {e}")

    try:
        candidates = await page.evaluate(
            """() => {
                const pops = document.querySelectorAll('[role="dialog"], [role="menu"], [role="listbox"]');
                const root = pops.length ? pops[pops.length - 1] : document.body;
                const sels = ['button','[role="button"]','[role="menuitem"]','[role="menuitemradio"]',
                              '[role="option"]','[role="tab"]','[role="radio"]'];
                const out = [];
                const seen = new Set();
                for (const sel of sels) {
                    root.querySelectorAll(sel).forEach(el => {
                        if (seen.has(el)) return;
                        seen.add(el);
                        const r = el.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) return;
                        out.push({
                            tag: el.tagName,
                            role: el.getAttribute('role') || '',
                            text: (el.textContent || '').trim().slice(0, 60),
                            aria: el.getAttribute('aria-label') || '',
                            title: el.getAttribute('title') || '',
                            classes: (el.className || '').toString().slice(0, 80),
                        });
                    });
                }
                return out;
            }"""
        )
        json_path = Path("/tmp") / f"flow_{tag}_{ts}.json"
        json_path.write_text(json.dumps(candidates, ensure_ascii=False, indent=2), encoding="utf-8")
        _log(f"クリック候補 {len(candidates)} 件: {json_path}")
        # 上位 10 件をログに直接出す
        for i, c in enumerate(candidates[:10]):
            label = c.get("aria") or c.get("text") or c.get("title") or "(no label)"
            _log(f"  [{i}] {c.get('role') or c.get('tag')}: {label[:50]}")
    except Exception as e:
        _log(f"⚠ 候補 JSON 失敗: {e}")

    try:
        png_path = Path("/tmp") / f"flow_{tag}_{ts}.png"
        await page.screenshot(path=str(png_path), full_page=False)
        _log(f"スクショ: {png_path}")
    except Exception as e:
        _log(f"⚠ スクショ失敗: {e}")


def _count_variants(count: str) -> list[str]:
    """x4 → ['x4','×4','⨯4','4枚','4','4 outputs','4 images']"""
    n = re.sub(r"[^0-9]", "", count) or count
    return [f"x{n}", f"×{n}", f"⨯{n}", count, f"{n}枚", n,
            f"{n} outputs", f"{n} images", f"生成枚数 {n}"]


def _aspect_variants(aspect: str) -> list[str]:
    """16:9 → ['16:9','16 : 9','16x9','ワイド','Landscape']"""
    m = re.match(r"(\d+)\s*[:xX]\s*(\d+)", aspect)
    v = [aspect]
    if m:
        a, b = m.group(1), m.group(2)
        v += [f"{a}:{b}", f"{a} : {b}", f"{a}x{b}", f"{a}×{b}"]
    return v


def _aspect_token(aspect: str) -> str:
    """16:9 → crop_16_9 (Flow の aria-label 表記)"""
    m = re.match(r"(\d+)\s*[:xX]\s*(\d+)", aspect)
    if not m:
        return ""
    return f"crop_{m.group(1)}_{m.group(2)}"


async def set_aspect_and_count(page: Page, aspect: str, count: str, model_name: str) -> None:
    """設定ピルの aria-label で現在値を検証 → 一致なら開かない / 不一致なら開いて変更。
    aria-label 例: '🍌 Nano Banana 2 crop_16_9 x4'"""
    want_aspect = _aspect_token(aspect)
    want_count = count  # "x4"

    current = await _read_pill_aria(page, model_name)
    _log(f"現在ピル aria-label: {current!r}")

    need_aspect = bool(want_aspect) and want_aspect not in current
    need_count = want_count not in current
    if not need_aspect and not need_count:
        _log(f"設定は既に {aspect} / {count} のため変更不要")
        return

    try:
        await open_settings_popover(page, model_name)
    except Exception as e:
        _log(f"⚠ 設定ポップオーバーを開けず: {e} — 既定値で続行")
        return

    await asyncio.sleep(0.25)

    # 画像タブに切替（画像 / 動画 が切り替わっているケース）
    for label in ["画像", "Image"]:
        if await _click_by_text_variants(page, [label], roles=["tab"], scope_popover=True):
            _log(f"タブ切替: {label}")
            await asyncio.sleep(0.1)
            break

    # アスペクト比（button の aria-label が "crop_16_9" を含む）
    if need_aspect:
        clicked = await page.evaluate(
            """(token) => {
                const btns = document.querySelectorAll('button[aria-label], [role="button"][aria-label]');
                for (const b of btns) {
                    const r = b.getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) continue;
                    const a = (b.getAttribute('aria-label') || '').trim();
                    if (a === token || a.includes(token)) {
                        b.scrollIntoView({block:'center'});
                        b.click();
                        return {ok: true, aria: a};
                    }
                }
                return {ok: false};
            }""", want_aspect,
        )
        if clicked and clicked.get("ok"):
            _log(f"アスペクト設定: {clicked.get('aria')}")
        else:
            # テキスト "16:9" で探す (ボタン textContent)
            hit = await _click_by_text_variants(page, [aspect, aspect.replace(":", " : ")],
                                                scope_popover=True)
            if hit:
                _log(f"アスペクト設定: {hit}")
            else:
                _log(f"⚠ アスペクト '{aspect}' を検出できず")
                await _dump_popover_html(page, "aspect_fail")
        await asyncio.sleep(0.15)

    # 枚数 (button textContent が "x4")
    if need_count:
        n = re.sub(r"[^0-9]", "", count) or count
        hit = await _click_by_text_variants(page, [count, f"x{n}", f"×{n}", n], scope_popover=True)
        if hit:
            _log(f"枚数設定: {hit}")
        else:
            _log(f"⚠ 枚数 '{count}' を検出できず")
            await _dump_popover_html(page, "count_fail")
        await asyncio.sleep(0.15)

    # 設定の反映を ピル aria-label で検証
    after = await _read_pill_aria(page, model_name)
    _log(f"設定後ピル aria-label: {after!r}")

    await page.keyboard.press("Escape")


async def type_prompt_and_submit(page: Page, prompt: str) -> None:
    """プロンプト欄は contenteditable な <p>（div.sc-9586f820-3 配下）。
    - 要素をクリック → Cmd+A → Backspace → keyboard.type
    - 送信: aria-label='arrow_forward 作成' の button
    (DevTools Recorder で確認済み)"""
    editor_sel_candidates = [
        'textarea[placeholder*="何を作成"]',  # UI バリアント (Recorder で確認)
        'textarea[placeholder*="作成しますか"]',
        'div[class*="9586f820"] p',     # Recorder 捕捉セレクタ
        'div[class*="10e688a2"] p',     # 同プロンプト領域
        '[contenteditable="true"] p',
        '[contenteditable="true"]',
        'p[contenteditable="true"]',
        'textarea',
    ]
    editor = None
    for sel in editor_sel_candidates:
        try:
            loc = page.locator(sel).last
            await loc.wait_for(state="visible", timeout=3000)
            editor = loc
            _log(f"プロンプト欄を特定: {sel}")
            break
        except Exception:
            continue
    if editor is None:
        _log("⚠ プロンプト欄の contenteditable を検出できず — role=textbox にフォールバック")
        editor = page.get_by_role("textbox").last

    await editor.click()
    await asyncio.sleep(0.1)
    mod = "Meta" if os_is_mac() else "Control"
    # 既存テキストを全削除
    await page.keyboard.press(f"{mod}+A")
    await page.keyboard.press("Backspace")
    # 入力 (delay=0 で最速)
    await page.keyboard.type(prompt, delay=0)
    await asyncio.sleep(0.15)

    # 送信: aria-label='arrow_forward 作成' または 'arrow_forward Create' / '送信' / submit type
    send_candidates = [
        'button[aria-label*="arrow_forward"][aria-label*="作成"]',
        'button[aria-label*="arrow_forward"][aria-label*="Create" i]',
        'button[aria-label*="arrow_forward"]',
        'button[aria-label="送信"]',
        'button[aria-label*="送信"]',
        'button[aria-label*="Send" i]',
        'button[aria-label*="作成"]',
        'button[aria-label*="Create" i]',
        'button[type="submit"]',
        'button:has(i:has-text("arrow_forward"))',
    ]
    clicked = False
    for sel in send_candidates:
        try:
            btn = page.locator(sel).last  # 入力エリア直下 = 最後
            if await btn.count():
                await btn.click(timeout=3000)
                _log(f"送信クリック: {sel}")
                clicked = True
                break
        except Exception:
            continue
    if not clicked:
        _log("⚠ 送信ボタン未検出 → Cmd+Enter 試行")
        await editor.focus()
        await page.keyboard.press(f"{mod}+Enter")
        await asyncio.sleep(0.2)

    # 生成開始検証
    _log("送信後の生成開始を検証…")
    started = False
    for _ in range(20):
        await asyncio.sleep(0.3)
        in_prog = await page.evaluate(
            """() => [...document.querySelectorAll('div')]
                .filter(d => d.children.length === 0 && /^\\d{1,3}%$/.test((d.textContent||'').trim()))
                .length"""
        )
        links = await page.eval_on_selector_all(
            'a[href*="/edit/"]', "els => els.length",
        )
        thumbs = await page.eval_on_selector_all(
            'button.gf-dl-thumb-btn', "els => els.length",
        )
        if in_prog > 0 or links > 0 or thumbs > 0:
            _log(f"生成開始を検出 (in_progress={in_prog}, links={links}, dl_btns={thumbs})")
            started = True
            break
    if not started:
        _log("⚠ 生成が開始されていません。Enter を追い打ち送信")
        await editor.focus()
        await page.keyboard.press("Enter")
        await asyncio.sleep(0.5)


# userscript が仕込むボタンはクラスでなくテキスト '↓ 2K' で始まるものを探す
_COUNT_DL_BUTTONS_JS = """() => {
    const all = document.querySelectorAll('button');
    let cnt = 0;
    for (const b of all) {
        const t = (b.textContent || '').trim();
        if (t.startsWith('↓ 2K') || t.startsWith('↓ 1K') || b.classList.contains('gf-dl-thumb-btn')) {
            cnt++;
        }
    }
    return cnt;
}"""


async def wait_until_thumbs_at_least(page: Page, target: int, timeout_ms: int = 300_000) -> int:
    """userscript の DL ボタン (テキスト '↓ 2K' or class `.gf-dl-thumb-btn`) が target 個に達するまで待機。
    userscript 未動作時は /edit/ リンク数でフォールバック判定。"""
    end = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_report = 0.0
    while True:
        cnt = await page.evaluate(
            """() => {
                const all = document.querySelectorAll('button');
                let dl = 0;
                for (const b of all) {
                    const t = (b.textContent || '').trim();
                    if (t.startsWith('↓ 2K') || t.startsWith('↓ 1K') || b.classList.contains('gf-dl-thumb-btn')) dl++;
                }
                const links = new Set([...document.querySelectorAll('a[href*="/edit/"]')]
                    .map(a => (a.href.match(/\\/edit\\/([a-f0-9-]+)/)||[])[1]).filter(Boolean));
                const inProgress = [...document.querySelectorAll('div')]
                    .filter(d => d.children.length === 0 && /^\\d{1,3}%$/.test((d.textContent||'').trim())).length;
                return {dl, links: links.size, inProgress};
            }"""
        )
        dl = cnt["dl"]; links = cnt["links"]; in_prog = cnt["inProgress"]
        ready = max(dl, links)
        now = asyncio.get_event_loop().time()
        if now - last_report > 3:
            _log(f"生成中… dl_btns={dl}, links={links}, in_progress={in_prog}, target={target}")
            last_report = now
        if ready >= target and in_prog == 0:
            return ready
        if now > end:
            raise TimeoutError(f"生成が完了しませんでした (dl={dl}, links={links}, target={target})")
        await asyncio.sleep(0.5)


async def wait_until_all_generated(page: Page, expected: int, timeout_ms: int = 300_000) -> list[str]:
    """後方互換: /edit/ リンク ID のリストを返す。"""
    end = asyncio.get_event_loop().time() + timeout_ms / 1000
    last_report = 0.0
    while True:
        in_progress = await page.evaluate(
            """() => [...document.querySelectorAll('div')]
                .filter(d => d.children.length === 0 && /^\\d{1,3}%$/.test((d.textContent||'').trim())).length"""
        )
        links = await page.eval_on_selector_all(
            'a[href*="/edit/"]',
            "els => [...new Set(els.map(e => e.href.match(/\\/edit\\/([a-f0-9-]+)/)?.[1]).filter(Boolean))]",
        )
        now = asyncio.get_event_loop().time()
        if now - last_report > 3:
            _log(f"生成中… in_progress={in_progress}, ready={len(links)}/{expected}")
            last_report = now
        if in_progress == 0 and len(links) >= expected:
            return links[:expected]
        if now > end:
            raise TimeoutError(f"生成が完了しませんでした (in_progress={in_progress}, links={len(links)})")
        await asyncio.sleep(0.5)


async def download_thumbs_range(page: Page, start: int, end: int,
                                out_dir: Path, resolution: str,
                                prefix: str = "flow") -> list[Path]:
    """userscript 由来 DL ボタン (テキスト '↓ 2K' or class `.gf-dl-thumb-btn`) の [start, end) を
    trusted click で発火し、context レベルの page.on('download') ハンドラに保存させる。

    重要 (添付ワーキングコードの所見):
      - userscript は `event.isTrusted === true` のクリックしか拾わない
      - Playwright `locator.click()` は CDP の Input.dispatchMouseEvent 経由で trusted を発火するので OK
      - userscript は `openAutoDownloadTab` で `window.open(?gf-auto-dl=1)` し popup で GM_download
      - そのため context.on('page') + page.on('download') を全ページに仕込んでおく必要がある
      - 各クリック後 1.5s 待って XHR アップスケールに余裕を持たせる
    """
    global _DL_PREFIX, _DL_OUT_DIR, _DL_RESOLUTION
    out_dir.mkdir(parents=True, exist_ok=True)

    # ハンドラ用の状態を更新
    _DL_OUT_DIR = out_dir
    _DL_PREFIX = prefix
    _DL_RESOLUTION = resolution
    initial = len(_DL_SAVED)

    # ボタンが target 数に達するまで待機
    cnt = 0
    for _ in range(20):
        cnt = await page.evaluate(_COUNT_DL_BUTTONS_JS)
        if cnt >= end:
            break
        await asyncio.sleep(0.5)

    if cnt >= end:
        # テキストプレフィックス + クラスの両対応で順序通りに locator
        prefix_pattern = f"^↓ {re.escape(resolution)}"
        btn_locator = page.locator(
            f'button.gf-dl-thumb-btn, button:text-matches("{prefix_pattern}")'
        )
        actual = await btn_locator.count()
        _log(f"DL ボタン検出: {actual} 個 (trusted-click range {start}..{end})")

        target_clicks = min(end, actual) - start
        for i in range(start, min(end, actual)):
            btn = btn_locator.nth(i)
            try:
                await btn.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            _log(f"[{i+1}] DL クリック (trusted)")
            try:
                # Playwright locator.click() は CDP Input.dispatchMouseEvent → trusted
                await btn.click(timeout=8000)
                # userscript の XHR (アップスケール → blob 化) に余裕を持たせる
                await page.wait_for_timeout(1500)
            except Exception as e:
                _log(f"  ⚠ click 失敗 [{i+1}]: {e}")

        # DL ハンドラが target 個保存するまで待機
        target_total = initial + target_clicks
        deadline = asyncio.get_event_loop().time() + 240
        last_report = 0.0
        while len(_DL_SAVED) < target_total and asyncio.get_event_loop().time() < deadline:
            now = asyncio.get_event_loop().time()
            if now - last_report > 5:
                _log(f"DL 完了待機… {len(_DL_SAVED) - initial}/{target_clicks}")
                last_report = now
            await asyncio.sleep(0.5)

        # 走っている _DL_TASKS の保存タスクの後始末
        if _DL_TASKS:
            done, _pending = await asyncio.wait(_DL_TASKS, timeout=5)
            _DL_TASKS.clear()

        saved = _DL_SAVED[initial:]
        if len(saved) < target_clicks:
            _log(f"⚠ DL 完了数 {len(saved)}/{target_clicks} (timeout)")
        return saved

    # フォールバック: userscript 未動作 → /edit/<id> ネイティブ UI 経由
    _log(f"⚠ userscript DL ボタン不足 ({cnt}/{end}) → /edit/ フォールバック")
    ids = await page.evaluate(
        """() => [...new Set([...document.querySelectorAll('a[href*="/edit/"]')]
            .map(a => (a.href.match(/\\/edit\\/([a-f0-9-]+)/)||[])[1]).filter(Boolean))]"""
    )
    base = re.match(r"(https://labs\.google/fx/ja/tools/flow)", page.url).group(1) if page.url else FLOW_URL
    saved: list[Path] = []
    for i, img_id in enumerate(ids[start:end], start=start):
        try:
            path = await _download_via_edit_page(page.context, base, img_id, resolution, out_dir)
            saved.append(path)
            _log(f"  [/edit/ {i+1}] saved → {path}")
        except Exception as e:
            _log(f"  ⚠ /edit/ DL 失敗 [{i+1}]: {e}")
    return saved


async def _download_via_edit_page(context: BrowserContext, base_url: str, image_id: str,
                                  resolution: str, out_dir: Path) -> Path:
    """フォールバック: /edit/<id> を開いて「ダウンロード」→ 2K。"""
    edit_url = f"{base_url}/edit/{image_id}"
    tab = await context.new_page()
    await tab.goto(edit_url, wait_until="domcontentloaded")
    await _ensure_userscript_on_page(tab)
    await asyncio.sleep(0.5)
    async with tab.expect_download(timeout=180_000) as dl_info:
        dl_btn = tab.locator('button[aria-label*="ダウンロード"], button[aria-label*="Download" i]').first
        await dl_btn.click(timeout=5000)
        await asyncio.sleep(0.3)
        hit = await _click_by_text_variants(tab, [resolution, f"{resolution} "],
                                            roles=["menuitem", "menuitemradio", "option", "button"],
                                            scope_popover=True)
        if not hit:
            await _dump_popover_html(tab, f"dl_fb_fail_{image_id[:8]}")
            raise RuntimeError(f"解像度 {resolution} メニュー未検出")
    download = await dl_info.value
    save_path = out_dir / f"flow_{image_id}_{resolution}.png"
    await download.save_as(save_path)
    await tab.close()
    return save_path


# ───────────────────────────────────────────────────────────────
# Claude CLI 経由でプロンプトを生成
# ───────────────────────────────────────────────────────────────

def suggest_prompt_via_claude(context_hint: str, cli_cmd: str = "claude", analysis: dict | None = None) -> str:
    """Claude CLI で Flow 向けの画像プロンプトを生成。
    analysis を渡すと competitor の visual_direction / viewer_needs をメタプロンプトに注入する。
    """
    analysis_section = ""
    if analysis:
        vd = (analysis or {}).get("visual_direction") or {}
        bp = (analysis or {}).get("buzz_patterns") or {}
        if vd or bp:
            analysis_section = (
                "\n=== Competitor Visual Direction (reverse-engineered) ===\n"
                f"Color palette: {json.dumps(vd.get('color_palette', []))}\n"
                f"Time of day: {vd.get('time_of_day', '')}\n"
                f"Subjects: {json.dumps(vd.get('subjects', []))}\n"
                f"Composition: {vd.get('composition', '')}\n"
                f"Atmosphere: {vd.get('atmosphere', '')}\n"
                f"Avoid: {json.dumps(vd.get('avoid', []))}\n"
                f"Viewer needs: {json.dumps(bp.get('viewer_needs', []))}\n"
                "Resonate with these signals — the thumbnail should promise exactly what these viewers are seeking.\n"
            )

    meta_prompt = (
        "You craft prompts for Google Flow (Nano Banana 2) to generate "
        "cinematic photorealistic thumbnail-grade images for a BGM YouTube channel. "
        "Constraints: 16:9 landscape, hyper-detailed, accurate anatomy, legible signage "
        "if text appears, Kodak Portra 400 aesthetic, shallow DOF, no watermarks.\n\n"
        f"Context: {context_hint}\n"
        f"{analysis_section}\n"
        "Return ONLY a single JSON object: "
        '{"prompt": "<the full English prompt, one paragraph>"}'
    )
    from app_llm_runner import run_llm
    text = run_llm(meta_prompt, cli_cmd=cli_cmd, timeout=180, label="flow-suggest")
    m = re.search(r'\{[\s\S]*\}', text)
    if not m:
        raise RuntimeError(f"Claude 応答から JSON を抽出できません: {text[:200]}")
    obj = json.loads(m.group(0))
    return obj["prompt"]


# ───────────────────────────────────────────────────────────────
# 実行モード
# ───────────────────────────────────────────────────────────────

async def _inject_userscript(context: BrowserContext) -> str | None:
    """Tampermonkey userscript を Playwright コンテキストに注入。
    戻り値をモジュール変数 `_USERSCRIPT_CACHE` に保存し、
    `_ensure_userscript_on_page` から goto 後にも再注入する。"""
    global _USERSCRIPT_CACHE
    if not USERSCRIPT_PATH.exists():
        _log(f"⚠ userscript が見つかりません: {USERSCRIPT_PATH}（スキップ）")
        return None
    try:
        script = USERSCRIPT_PATH.read_text(encoding="utf-8")
        combined = GM_POLYFILL + "\n" + script
        _USERSCRIPT_CACHE = combined
        await context.add_init_script(combined)
        for p in context.pages:
            try:
                if (p.url or "").startswith("http"):
                    await p.evaluate(combined)
                    _log(f"userscript 既存ページへ evaluate: {p.url[:60]}")
            except Exception:
                pass
        _log(f"userscript をキャッシュ ({len(script)} bytes) — goto 後に強制注入")
        return combined
    except Exception as e:
        _log(f"⚠ userscript 注入失敗: {e}")
        return None


async def _save_download(download: Download) -> None:
    """download を保存 → _DL_SAVED に追加。
    userscript GM_download / <a download> どちらでも発火 = ファイル名は元 suggested 名にせず順番で命名。"""
    n = len(_DL_SAVED) + 1
    save_path = _DL_OUT_DIR / f"{_DL_PREFIX}_{n:02d}_{_DL_RESOLUTION}.png"
    try:
        await download.save_as(str(save_path))
        _DL_SAVED.append(save_path)
        _log(f"  [DL] saved [{n}] → {save_path}")
    except Exception as e:
        _log(f"  ⚠ DL save 失敗: {e}")


def _on_download_event(download: Download) -> None:
    """page.on('download') ハンドラ。非同期 save をタスクとして起動。"""
    task = asyncio.create_task(_save_download(download))
    _DL_TASKS.append(task)


def _attach_download_to_all_pages(context: BrowserContext) -> None:
    """全 page (popup 含む) に download ハンドラを仕込む。"""
    def _on_new_page(p: Page) -> None:
        p.on("download", _on_download_event)
    context.on("page", _on_new_page)
    for p in context.pages:
        p.on("download", _on_download_event)


async def _ensure_userscript_on_page(page: Page) -> None:
    """現在のページに userscript を強制注入 (goto 直後に呼ぶ想定)。
    `add_script_tag` で <script> タグを直接挿入する。"""
    if not _USERSCRIPT_CACHE:
        return
    try:
        await page.add_script_tag(content=_USERSCRIPT_CACHE)
        _log("userscript をページに注入 (add_script_tag)")
    except Exception as e:
        # CSP で弾かれた場合は evaluate で再試行
        try:
            await page.evaluate(_USERSCRIPT_CACHE)
            _log("userscript をページに注入 (evaluate フォールバック)")
        except Exception as e2:
            _log(f"⚠ userscript 注入失敗: {e} / {e2}")


async def run_login_only(args: argparse.Namespace) -> int:
    """ログイン専用モード。ターミナル / Web subprocess の両方で安全に動く。

    終了条件（いずれか先に発生したもの）:
      1. ユーザーがブラウザウインドウを手動で閉じる ← 推奨
      2. 環境変数 APP_FLOW_LOGIN_TIMEOUT_SEC（既定 1800 秒 = 30 分）の上限
      3. ターミナルで Enter（stdin が tty の時のみ）

    旧仕様の「ログイン検知から 30 秒で自動終了」は削除しました。
    これは Google の 2 段階認証 / アカウント選択中に強制クローズされる事故の原因でした。
    ログイン完了はセッション保存のトリガにのみ使い、ブラウザはユーザーが閉じるまで残します。
    """
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    timeout_sec = int((os.environ.get("APP_FLOW_LOGIN_TIMEOUT_SEC") or os.environ.get("ORZZ_FLOW_LOGIN_TIMEOUT_SEC", "1800")))
    is_tty = sys.stdin.isatty() if hasattr(sys.stdin, "isatty") else False

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=False,
            viewport={"width": 1568, "height": 900},
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        if not args.no_userscript:
            await _inject_userscript(context)
        page = context.pages[0] if context.pages else await context.new_page()

        # ブラウザ手動クローズ検知用
        closed_flag = {"closed": False}
        context.on("close", lambda _ctx: closed_flag.update(closed=True))

        # tty なら Enter 入力もバックグラウンドで受け付け
        enter_flag = {"pressed": False}
        if is_tty:
            import threading
            def _wait_enter():
                try:
                    input()
                    enter_flag["pressed"] = True
                except (EOFError, OSError):
                    pass
            threading.Thread(target=_wait_enter, daemon=True).start()
        else:
            _log("（subprocess 起動のため Enter 待機は無効化されています）")

        _log(f"ブラウザでログインしてください（上限 {timeout_sec // 60} 分）")
        _log("ログインが終わったら、ブラウザのタブ（またはウインドウ）をご自身で閉じてください。")
        _log("自動でブラウザは閉じません — Google 認証の途中で消える事故を防ぐためです。")

        # ensure_logged_in を「バックグラウンド」で走らせて、ログイン検知時にだけ通知。
        # ブラウザのオーナーシップはユーザーに委ねる（自動クローズしない）。
        login_detected = {"value": False}
        async def _detect_login():
            try:
                ok = await ensure_logged_in(page, timeout_ms=timeout_sec * 1000)
                login_detected["value"] = bool(ok)
                if ok:
                    _log("✓ ログイン完了 — セッションを保存しました。")
                    _log("作業が終わったら、ブラウザを閉じてこの処理を終了してください。")
            except Exception as e:
                _log(f"⚠ ログイン検知中に例外: {e}")
        detect_task = asyncio.create_task(_detect_login())

        # ユーザーがウインドウを閉じる / Enter / 上限タイムアウトのいずれかまで待つ
        elapsed = 0
        while elapsed < timeout_sec:
            if closed_flag["closed"] or enter_flag["pressed"]:
                break
            await asyncio.sleep(1)
            elapsed += 1
            # 5 分ごとに残り時間を通知（ログ過多を避ける）
            if elapsed % 300 == 0:
                remain = timeout_sec - elapsed
                _log(f"待機中… 残り上限 {remain // 60} 分（ブラウザを閉じれば即終了）")

        # 検知タスクを片付け（ログイン未検知でもユーザーが閉じたなら終了でよい）
        if not detect_task.done():
            detect_task.cancel()
            try:
                await detect_task
            except (asyncio.CancelledError, Exception):
                pass

        # ユーザーがまだ閉じていないなら（タイムアウト到達）クローズ
        if not closed_flag["closed"]:
            _log(f"上限 {timeout_sec // 60} 分に到達したためブラウザを閉じます。")
            try:
                await context.close()
            except Exception:
                pass

        _log("Flow ログイン処理を終了しました。")
        return 0 if login_detected["value"] else 1


async def run_generate(args: argparse.Namespace) -> int:
    prompt = args.prompt or ""
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if not prompt:
        _log("ERROR: --prompt / --prompt-file / --suggest-prompt のいずれかが必要です。")
        return 2

    expected = int(args.count.lstrip("xX"))
    batch_size = max(1, min(args.batch_size or expected, 4))
    # expected が batch_size で割り切れないときは上側に丸める
    batches = (expected + batch_size - 1) // batch_size
    batch_count_str = f"x{batch_size}"
    out_dir = Path(args.output_dir) if args.output_dir else DEFAULT_DOWNLOAD_DIR

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=args.headless,
            viewport={"width": 1568, "height": 900},
            accept_downloads=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        if not args.no_userscript:
            await _inject_userscript(context)
        # context レベルで全 page (popup 含む) に download handler を仕込む。
        # userscript の DL は GM_download / popup タブの <a download> で発火するため
        # page.expect_download では捕捉できない。
        _DL_SAVED.clear()
        _DL_TASKS.clear()
        global _DL_OUT_DIR, _DL_RESOLUTION
        _DL_OUT_DIR = out_dir
        _DL_RESOLUTION = args.resolution
        _attach_download_to_all_pages(context)
        page = context.pages[0] if context.pages else await context.new_page()

        _log("Flow にアクセス…")
        # headless 時は短く即座に失敗、headed 時は 5 分まで人間のログインを待つ
        if args.headless:
            login_timeout = 30_000
        else:
            login_timeout = int((os.environ.get("APP_FLOW_GENERATE_LOGIN_TIMEOUT_SEC") or os.environ.get("ORZZ_FLOW_GENERATE_LOGIN_TIMEOUT_SEC", "300"))) * 1000
        login_ok = False
        try:
            login_ok = await ensure_logged_in(page, timeout_ms=login_timeout)
        except Exception as e:
            _log(f"⚠ ログイン確認中に例外: {e}")
        if not login_ok:
            if args.headless:
                _log("❌ ログインされていません（headless）。先に「🔑 ログイン」ボタンでセッションを確立してください。")
                _log("   exit_reason: NOT_LOGGED_IN_HEADLESS")
                # subprocess の出口を機械可読にする
                print(json.dumps({"ok": False, "error": "NOT_LOGGED_IN", "headless": True}, ensure_ascii=False))
            else:
                _log(f"❌ {login_timeout // 1000} 秒以内にログインが完了しませんでした。")
                _log("   ブラウザはこのまま開いておきます。手動ログイン後、もう一度「Flow で生成」を押してください。")
                _log("   exit_reason: LOGIN_TIMEOUT")
                print(json.dumps({"ok": False, "error": "LOGIN_TIMEOUT", "headless": False}, ensure_ascii=False))
                # ブラウザをすぐ閉じない（30 分まで保持）→ ユーザーが手動ログインできる
                grace_sec = int((os.environ.get("APP_FLOW_LOGIN_GRACE_SEC") or os.environ.get("ORZZ_FLOW_LOGIN_GRACE_SEC", "1800")))
                _log(f"（最大 {grace_sec // 60} 分間ブラウザを開いたままにします）")
                for _ in range(grace_sec):
                    try:
                        if not context.pages:
                            break
                    except Exception:
                        break
                    await asyncio.sleep(1)
            try:
                await context.close()
            except Exception:
                pass
            return 3
        await _ensure_userscript_on_page(page)

        _log(f"プロジェクト準備: '{args.project_name}'")
        existed = await open_or_create_project(page, args.project_name)
        _log("既存プロジェクトを開きました" if existed else "新規プロジェクトを作成しました")

        # 参照画像アップロード（任意）
        if args.reference_image:
            ref_path = Path(args.reference_image).expanduser()
            try:
                await upload_reference_image(page, ref_path)
            except Exception as e:
                _log(f"⚠ 参照画像アップロード失敗（続行）: {e}")

        _log(f"設定: {args.model} / {args.aspect} / {batch_count_str} × {batches} バッチ (合計 {expected} 枚目標)")
        await set_aspect_and_count(page, args.aspect, batch_count_str, args.model)

        all_saved: list[Path] = []
        for b in range(batches):
            if b > 0:
                _log(f"バッチ {b+1}/{batches}: 新規作成ボタン")
                if not await click_new_batch_button(page):
                    _log("⚠ 新バッチボタン未検出 — 続行してプロンプトを再入力")
                await asyncio.sleep(0.3)

            _log(f"バッチ {b+1}/{batches}: プロンプト送信 ({len(prompt)} 文字)")
            await type_prompt_and_submit(page, prompt)

            target = batch_size * (b + 1)
            _log(f"バッチ {b+1}/{batches}: {target} 枚到達を待機")
            await wait_until_thumbs_at_least(page, target)

            # userscript の .gf-dl-thumb-btn の末尾 batch_size 個を DL
            _log(f"バッチ {b+1}/{batches}: DL (range {target - batch_size}..{target})")
            batch_saved = await download_thumbs_range(
                page, start=target - batch_size, end=target,
                out_dir=out_dir, resolution=args.resolution,
                prefix=f"flow_b{b+1}",
            )
            all_saved.extend(batch_saved)
        saved = all_saved

        print(json.dumps({
            "ok": True,
            "saved": [str(p) for p in saved],
            "output_dir": str(out_dir),
            "project_url": page.url,
        }, ensure_ascii=False))

        if not args.headless and not args.no_wait:
            _log("Enter で終了…")
            try:
                input()
            except EOFError:
                pass
        await context.close()
        return 0


# ───────────────────────────────────────────────────────────────
# エントリポイント
# ───────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="Google Flow 自動化（Playwright）")
    ap.add_argument("--login-only", action="store_true", help="ログインセッション確立のみ")
    ap.add_argument("--prompt", help="プロンプト本文")
    ap.add_argument("--prompt-file", help="プロンプトを読み込むファイル")
    ap.add_argument("--suggest-prompt", help="Claude CLI でプロンプトを生成する文脈（指定すると --prompt を自動充填）")
    ap.add_argument("--output-dir", help="保存先ディレクトリ（省略時 ~/Downloads）")
    ap.add_argument("--reference-image", help="参照画像パス（指定時は Flow にアップロードしてプロンプトと一緒に送信）")
    ap.add_argument("--project-name", default=DEFAULT_PROJECT_NAME)
    ap.add_argument("--aspect", default=DEFAULT_ASPECT)
    ap.add_argument("--count", default=_master_flow_count(),
                    help=f"画像枚数（{DEFAULT_COUNT} 既定、master_settings.flow.default_count で上書き可）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--resolution", default=DEFAULT_RESOLUTION)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-wait", action="store_true", help="完了後に Enter 待ちしない")
    ap.add_argument("--no-userscript", action="store_true",
                    help="Tampermonkey userscript (flow_userscript.js) を注入しない")
    ap.add_argument("--batch-size", type=int, default=2,
                    help="1 バッチあたりの生成枚数 (1..4)。既定 2 で x2 × N バッチの高速ループ。")
    return ap


def main() -> int:
    args = build_parser().parse_args()

    if args.suggest_prompt and not args.prompt and not args.prompt_file:
        _log(f"Claude CLI でプロンプトを生成: {args.suggest_prompt}")
        try:
            args.prompt = suggest_prompt_via_claude(args.suggest_prompt)
            _log(f"生成プロンプト: {args.prompt[:100]}…")
        except Exception as e:
            _log(f"ERROR (Claude): {e}")
            return 4

    if args.login_only:
        return asyncio.run(run_login_only(args))
    return asyncio.run(run_generate(args))


if __name__ == "__main__":
    sys.exit(main())
