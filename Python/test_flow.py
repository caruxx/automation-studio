#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Google Labs Flow（https://labs.google/fx/ja/tools/flow）自動テスト
v5: WEB アップロード参照画像 + 動画プロジェクトの Image/ 保存、2バッチ同一手順

仕様（2バッチとも同じ手順）:
 0. 新規プロジェクトを作成
 1. プロジェクト名を「テスト」に変更
 2. アスペクト比 16:9（Nano Banana 2 の既定値、通常はノーオペ）
 --- バッチ1 ---
 3. プロンプト chip をクリア
 4. 参照画像を *新規* アップロード
 5. PROMPT_1 → 生成（4枚）
 6. ↓2K × 4（連番 01-04 で Image/01/ に保存）
 --- バッチ2 ---
 7. プロンプト chip をクリア
 8. 参照画像を *新規* アップロード（バッチ1と同じファイル）
 9. PROMPT_2 → 生成（4枚）
10. ↓2K × 4（連番 01-04 で Image/02/ に保存）

WEB/Dashboard 連携:
  --video-name 78_orzz_260420    動画プロジェクト名。設定済み channel_folder 配下の
                                 {channel_folder}/{video_name}/Image/ に保存する。
  --flow-png   /tmp/upload.png   WEB からアップロードされた参照画像パス（必須）。
  --output-dir /path/to/out      保存先の明示指定（--video-name より優先）。
  --prompt1 / --prompt2          各バッチのプロンプト上書き。

DL フォールバック:
  userscript が GM_download で直接 Downloads に書き出す場合に備えて二段構え:
    (A) page.on('download') で捕捉できた分 → batch_dir/NN.ext に save_as()
    (B) 残りは ~/Downloads の 画像ファイル（png/jpg/webp）を mtime ソートで吸い上げて mv
  不足した場合は 2K ボタンを再クリックして最大 2 回リトライする。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Download,
    Page,
    TimeoutError as PWTimeoutError,
    sync_playwright,
)

# ===== 設定 =============================================================
HERE = Path(__file__).resolve().parent
FLOW_URL = "https://labs.google/fx/ja/tools/flow"

AUTH_JSON = Path(os.environ.get("AUTH_JSON", HERE / "auth.json"))

# 永続プロファイル（flow_automation.py と同じ場所を共有）
# ここに Google ログイン Cookie・localStorage などが保存され、以降はログイン不要になる
USER_DATA_DIR = Path(
    os.environ.get("USER_DATA_DIR", Path.home() / ".flow-playwright-profile")
)

USER_DOWNLOADS = Path(os.environ.get("USER_DOWNLOADS", Path.home() / "Downloads"))

# userscript (Tampermonkey 相当) を Playwright コンテキストに注入
USERSCRIPT_PATH = HERE / "flow_userscript.js"
_USERSCRIPT: str | None = None

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

DEFAULT_PROMPT_1 = (
    "参照画像と同じ人物を、雨上がりの朝の渋谷スクランブルに配置した"
    "シネマティックショット"
)
DEFAULT_PROMPT_2 = (
    "採用アセットの人物を、夜の新宿ゴールデン街、ネオンサインを背に立つ全身ショット、"
    "シネマティックな印象"
)

PROJECT_NAME = os.environ.get("PROJECT_NAME", "テスト")

# ↓2K 間は 3 秒空ける（popup が安定して開ききる時間＋XHR アップスケール）
CLICK_INTERVAL_S = float(os.environ.get("CLICK_INTERVAL_S", "3.0"))
GEN_TIMEOUT_S = int(os.environ.get("GEN_TIMEOUT_S", "300"))
DL_TIMEOUT_S = int(os.environ.get("DL_TIMEOUT_S", "240"))

SLOW_MO_MS = int(os.environ.get("SLOW_MO_MS", "120"))
HEADFUL = os.environ.get("HEADFUL", "1") == "1"

VIEWPORT = {
    "width": int(os.environ.get("VW", "1600")),
    "height": int(os.environ.get("VH", "1040")),
}


# ===== ユーティリティ ==================================================
def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[{_ts()}] {msg}", flush=True)


def _read_dashboard_config() -> dict:
    """~/.config/orzz/dashboard_config.json から channel_folder を読む。
    app.py と互換の軽量ローダ（importlib で app.py を呼ぶと FastAPI 起動コストが高い）。"""
    path = Path.home() / ".config" / "orzz" / "dashboard_config.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"[warn] dashboard_config.json の読込に失敗: {e}")
    return {}


def resolve_output_dir(
    cli_output_dir: str | None,
    video_name: str | None,
) -> Path:
    """保存先ディレクトリを解決する。

    優先順位:
      1. --output-dir が明示されていればそれ
      2. --video-name があれば {channel_folder}/{video_name}/Image/
      3. env DL_ROOT
      4. HERE/downloads
    """
    if cli_output_dir:
        return Path(cli_output_dir).expanduser()

    if video_name:
        cfg = _read_dashboard_config()
        channel = cfg.get("channel_folder") or (os.environ.get("APP_CHANNEL_FOLDER") or os.environ.get("ORZZ_CHANNEL_FOLDER", ""))
        if channel:
            candidate = Path(channel) / video_name / "Image"
            log(f"video_name → 保存先を解決: {candidate}")
            return candidate
        log(f"[warn] video_name={video_name} だが channel_folder が未設定。フォールバック")

    env_root = os.environ.get("DL_ROOT")
    if env_root:
        return Path(env_root).expanduser()

    return HERE / "downloads"


# ===== userscript 注入 =================================================
def inject_userscript(context: BrowserContext) -> None:
    """flow_userscript.js + GM_polyfill を context.add_init_script で登録。
    既存ページにも evaluate で流し込む。"""
    global _USERSCRIPT
    if not USERSCRIPT_PATH.exists():
        log(f"[warn] userscript が見つかりません: {USERSCRIPT_PATH}")
        return
    try:
        script = USERSCRIPT_PATH.read_text(encoding="utf-8")
        _USERSCRIPT = GM_POLYFILL + "\n" + script
        context.add_init_script(_USERSCRIPT)
        log(f"userscript 注入: init_script ({len(script)} bytes)")
        for p in context.pages:
            try:
                if (p.url or "").startswith("http"):
                    p.evaluate(_USERSCRIPT)
            except Exception:  # noqa: BLE001
                pass
    except Exception as e:  # noqa: BLE001
        log(f"[warn] userscript 注入失敗: {e}")


def ensure_userscript_on_page(page: Page) -> None:
    """SPA 遷移や goto 後に userscript を再注入する（init_script が走らないことがある）。"""
    if not _USERSCRIPT:
        return
    try:
        page.add_script_tag(content=_USERSCRIPT)
    except Exception:  # CSP で弾かれたら evaluate
        try:
            page.evaluate(_USERSCRIPT)
        except Exception as e:  # noqa: BLE001
            log(f"[warn] userscript re-injection 失敗: {e}")


# ===== DL セーフティネット ============================================
class DownloadSink:
    """page.on('download') を仕込んで連番保存する。

    Playwright がキャプチャできなかった分は adopt_userscript_fallback() で
    Downloads フォルダから mtime ソート吸い上げしてリカバーする。
    """

    def __init__(self, batch_dir: Path) -> None:
        self.batch_dir = batch_dir
        self.batch_dir.mkdir(parents=True, exist_ok=True)
        self.saved: list[Path] = []
        self.started_at: float = time.time()

    def attach(self, page: Page) -> None:
        page.on("download", self._handle)

    def attach_context(self, context: BrowserContext) -> None:
        """popup タブ (userscript が window.open する) も含めて全ページで DL 捕捉。"""
        def _on_new_page(p: Page) -> None:
            p.on("download", self._handle)
        context.on("page", _on_new_page)
        for p in context.pages:
            p.on("download", self._handle)

    def _handle(self, dl: Download) -> None:
        idx = len(self.saved) + 1
        suggested = dl.suggested_filename or "file.bin"
        ext = Path(suggested).suffix or ".png"
        dest = self.batch_dir / f"{idx:02d}{ext}"
        try:
            dl.save_as(dest)
            self.saved.append(dest)
            log(f"  DL saved (A/page.on): {dest.name}  suggested={suggested}")
        except Exception as e:  # noqa: BLE001
            log(f"  [warn] dl.save_as failed: {e}")

    def wait_until(self, n: int, timeout_s: int = DL_TIMEOUT_S) -> bool:
        start = time.time()
        last_report = 0.0
        last_count = -1
        while time.time() - start < timeout_s:
            cur = len(self.saved)
            if cur >= n:
                return True
            now = time.time()
            if cur != last_count or (now - last_report) >= 5.0:
                log(f"  DL 完了待機… {cur}/{n} ({int(now - start)}s)")
                last_count = cur
                last_report = now
            time.sleep(0.5)
        return False

    def adopt_userscript_fallback(self, expected: int = 4) -> None:
        """userscript が GM_download で Downloads に直接書いた分を吸い上げ。

        ファイル名条件は段階的に緩める:
          1) `_2K_` を含む png（優先）
          2) png/jpg/jpeg/webp のいずれか（不足分を補う）
        """
        if len(self.saved) >= expected:
            return
        need = expected - len(self.saved)
        log(f"  fallback(B): Downloads から {need} 件吸い上げ")
        if not USER_DOWNLOADS.exists():
            log(f"  [warn] USER_DOWNLOADS 不在: {USER_DOWNLOADS}")
            return

        img_exts = {".png", ".jpg", ".jpeg", ".webp"}
        t_floor = self.started_at - 2.0
        all_imgs = [
            p
            for p in USER_DOWNLOADS.iterdir()
            if p.is_file()
            and p.suffix.lower() in img_exts
            and p.stat().st_mtime >= t_floor
        ]
        all_imgs.sort(key=lambda p: p.stat().st_mtime)

        picked: list[Path] = []
        preferred = [p for p in all_imgs if "_2K_" in p.name]
        picked.extend(preferred[:need])
        if len(picked) < need:
            others = [p for p in all_imgs if p not in picked]
            picked.extend(others[: need - len(picked)])

        for src in picked:
            idx = len(self.saved) + 1
            ext = src.suffix.lower() or ".png"
            dest = self.batch_dir / f"{idx:02d}{ext}"
            try:
                shutil.move(str(src), str(dest))
                self.saved.append(dest)
                log(f"  DL saved (B/userscript): {dest.name}  <- {src.name}")
            except Exception as e:  # noqa: BLE001
                log(f"  [warn] fallback mv failed {src.name}: {e}")


# ===== Flow UI ==========================================================
def open_flow(context: BrowserContext) -> Page:
    page = context.new_page()
    page.set_default_timeout(30_000)
    log(f"goto {FLOW_URL}")
    page.goto(FLOW_URL, wait_until="domcontentloaded")
    ensure_userscript_on_page(page)
    try:
        page.wait_for_selector(
            "button:has-text('新しいプロジェクト')", timeout=20_000
        )
        log("  ログイン済み、Flow トップを確認")
    except PWTimeoutError:
        log("  未ログインのようです。ブラウザ上でログインしてください（最大5分待機）…")
        page.wait_for_selector(
            "button:has-text('新しいプロジェクト')", timeout=300_000
        )
    return page


def create_project_and_rename(page: Page, name: str) -> None:
    log("step 0: 新規プロジェクトを作成")
    new_btn = page.locator("button:has-text('新しいプロジェクト')").first
    new_btn.scroll_into_view_if_needed()
    new_btn.click()
    page.wait_for_url(re.compile(r".*/project/.+"), timeout=30_000)
    page.wait_for_load_state("domcontentloaded", timeout=10_000)
    # SPA 遷移後 — init_script は再走しないので明示注入
    ensure_userscript_on_page(page)
    page.wait_for_selector("[contenteditable='true'][role='textbox']", timeout=30_000)

    log(f"step 1: プロジェクト名を「{name}」に変更")
    title_input = page.locator("input[aria-label='編集可能なテキスト']").first
    title_input.scroll_into_view_if_needed()
    title_input.click()
    mod = "Meta" if sys.platform == "darwin" else "Control"
    title_input.press(f"{mod}+a")
    title_input.press("Delete")
    title_input.type(name, delay=30)
    title_input.press("Enter")
    page.wait_for_timeout(500)


def _read_pill_aria(page: Page) -> str:
    """モデル設定ピルの aria-label を取得（例: '🍌 Nano Banana 2 crop_16_9 x4'）。"""
    try:
        pill = page.locator("button:has-text('Nano Banana')").first
        aria = pill.get_attribute("aria-label", timeout=2_000) or ""
        if aria:
            return aria
        return pill.inner_text(timeout=1_000) or ""
    except Exception:  # noqa: BLE001
        return ""


def set_aspect_and_count(
    page: Page, ratio: str = "16:9", count: str = "x4"
) -> None:
    """アスペクト比と生成枚数を設定する。
    現在値の aria-label に両方含まれていればノーオペ。"""
    log(f"step 2: アスペクト比 {ratio} / 枚数 {count} を確認")
    aspect_token = f"crop_{ratio.replace(':', '_')}"
    count_token = count  # "x4"

    current = _read_pill_aria(page)
    log(f"  現在ピル: {current!r}")
    need_aspect = aspect_token not in current
    need_count = count_token not in current
    if not need_aspect and not need_count:
        log("  設定変更不要")
        return

    # ポップオーバーを開く
    model_btn = page.locator("button:has-text('Nano Banana')").first
    try:
        model_btn.scroll_into_view_if_needed()
        model_btn.click()
        page.wait_for_timeout(400)
    except Exception as e:  # noqa: BLE001
        log(f"  [warn] 設定ピルを開けず: {e}")
        return

    # 画像タブに切替（動画が選ばれているケース）
    for lbl in ["画像", "Image"]:
        try:
            tab = page.get_by_role("tab", name=lbl)
            if tab.count() > 0:
                tab.first.click(timeout=1_500)
                page.wait_for_timeout(150)
                break
        except Exception:  # noqa: BLE001
            continue

    # アスペクト: aria-label で crop_16_9 を含むボタンをクリック
    if need_aspect:
        try:
            clicked = page.evaluate(
                """(token) => {
                    const btns = document.querySelectorAll('button[aria-label],[role=button][aria-label]');
                    for (const b of btns) {
                        const r = b.getBoundingClientRect();
                        if (r.width === 0 || r.height === 0) continue;
                        const a = (b.getAttribute('aria-label') || '').trim();
                        if (a === token || a.includes(token)) {
                            b.scrollIntoView({block:'center'}); b.click();
                            return a;
                        }
                    }
                    return '';
                }""",
                aspect_token,
            )
            if clicked:
                log(f"  アスペクト設定: {clicked}")
            else:
                page.get_by_text(ratio, exact=True).first.click(timeout=2_500)
                log(f"  アスペクト設定(fallback): {ratio}")
        except Exception as e:  # noqa: BLE001
            log(f"  [warn] アスペクト未設定: {e}")
        page.wait_for_timeout(150)

    # 枚数: textContent が 'x4' のボタンをクリック
    if need_count:
        n = re.sub(r"[^0-9]", "", count) or count
        variants = [count, f"x{n}", f"×{n}", f"⨯{n}", n]
        hit = False
        for v in variants:
            try:
                loc = page.get_by_text(v, exact=True)
                if loc.count() > 0:
                    loc.first.click(timeout=1_500)
                    log(f"  枚数設定: {v}")
                    hit = True
                    break
            except Exception:  # noqa: BLE001
                continue
        if not hit:
            log(f"  [warn] 枚数 {count} を検出できず")
        page.wait_for_timeout(150)

    after = _read_pill_aria(page)
    log(f"  設定後ピル: {after!r}")
    page.keyboard.press("Escape")
    page.wait_for_timeout(200)


def set_aspect_ratio(page: Page, ratio: str = "16:9") -> None:
    """後方互換: アスペクト比のみ（x4 も同時に保証）。"""
    set_aspect_and_count(page, ratio=ratio, count="x4")


def clear_prompt_chips(page: Page) -> None:
    closes = page.locator("button:has-text('close')")
    cnt = closes.count()
    if cnt == 0:
        return
    log(f"  古いプロンプト chip {cnt} 個を close")
    for _ in range(cnt):
        btn = page.locator("button:has-text('close')").first
        try:
            btn.click(timeout=2_000)
        except Exception:  # noqa: BLE001
            break
        page.wait_for_timeout(200)


def submit_prompt(page: Page, prompt: str) -> None:
    log(f"prompt: {prompt[:40]}…")
    box = page.locator("[contenteditable='true'][role='textbox']").first
    box.scroll_into_view_if_needed()
    box.click()
    mod = "Meta" if sys.platform == "darwin" else "Control"
    box.press(f"{mod}+a")
    box.press("Delete")
    box.type(prompt, delay=12)
    send = (
        page.locator("button")
        .filter(has_text="arrow_forward")
        .filter(has_text="作成")
    )
    send.first.scroll_into_view_if_needed()
    send.first.click()


def wait_for_n_results(page: Page, n: int, timeout_s: int = GEN_TIMEOUT_S) -> None:
    """生成完了と userscript 由来 '↓ 2K' ボタン生成の両方を待つ。

    判定:
      - 生成完了: a[href*="/edit/"] が n 個以上
      - DL 可能: button.gf-dl-thumb-btn（または text '↓ 2K'）が n 個以上
    userscript 未注入時は /edit/ リンク数の時点で再注入を試みる。
    """
    log(f"待機: 生成結果 {n} 枚を揃えるまで…")
    start = time.time()
    reinjected = False
    last_edit = -1
    last_btn = -1
    while time.time() - start < timeout_s:
        edit_cnt = page.locator('a[href*="/edit/"]').count()
        btn_cnt = page.locator("button.gf-dl-thumb-btn, button:has-text('↓ 2K')").count()
        if edit_cnt != last_edit or btn_cnt != last_btn:
            log(f"  状態: /edit/={edit_cnt} 2Kボタン={btn_cnt}")
            last_edit, last_btn = edit_cnt, btn_cnt
        if btn_cnt >= n:
            log(f"  ok: 2K ボタン {btn_cnt} 個検出")
            return
        # 生成完了 (/edit/ は揃った) のに 2K ボタンが 0 → userscript 未動作
        if edit_cnt >= n and btn_cnt == 0 and not reinjected:
            log("  [recover] 生成完了したが userscript 未動作。再注入を試行")
            ensure_userscript_on_page(page)
            reinjected = True
            time.sleep(1.5)
            continue
        time.sleep(1.5)
    raise TimeoutError(
        f"生成結果が時間内に揃いませんでした (最終: /edit/={last_edit} 2K={last_btn})"
    )


def download_via_edit_page(
    context: BrowserContext, sink: DownloadSink, image_id: str
) -> bool:
    """フォールバック: /edit/<id>?gf-auto-dl=1 を直接 open し、
    popup ではなく普通の new_page で userscript の auto-dl を発火する。

    - popup でない通常タブなので popup blocker の影響を受けない
    - page.on('download') はハンドラ仕込み済 (attach_context) なので自動で保存される
    """
    base = "https://labs.google/fx/ja/tools/flow"
    edit_url = f"{base}/edit/{image_id}?gf-auto-dl=1"
    log(f"  [fallback-edit] open {edit_url[-60:]}")
    before = len(sink.saved)
    tab = context.new_page()
    try:
        tab.goto(edit_url, wait_until="domcontentloaded", timeout=30_000)
        ensure_userscript_on_page(tab)
        ok = sink.wait_until(before + 1, timeout_s=120)
        return ok
    except Exception as e:  # noqa: BLE001
        log(f"  [warn] fallback-edit 失敗 {image_id[:8]}: {e}")
        return False
    finally:
        try:
            tab.close()
        except Exception:  # noqa: BLE001
            pass


def _collect_edit_ids(page: Page) -> list[str]:
    """現在のプロジェクト画面から a[href*="/edit/"] の image_id 一覧（重複排除、出現順）。"""
    try:
        return page.evaluate(
            """() => {
                const seen = new Set();
                const out = [];
                for (const a of document.querySelectorAll('a[href*="/edit/"]')) {
                    const m = (a.href || '').match(/\\/edit\\/([a-f0-9-]+)/);
                    if (!m) continue;
                    if (seen.has(m[1])) continue;
                    seen.add(m[1]);
                    out.push(m[1]);
                }
                return out;
            }"""
        ) or []
    except Exception:  # noqa: BLE001
        return []


def download_four_2k(page: Page, sink: DownloadSink, offset: int = 0) -> None:
    """生成された画像 (a[href*="/edit/<id>"]) を image_id 単位で
    /edit/<id>?gf-auto-dl=1 に 1 枚ずつ順次遷移して DL する。

    trusted click + popup 連発は Chromium の popup blocker や focus 競合で
    不安定（2/4 しか通らない症状が発生）。このメソッドは popup を介さず、
    Playwright 自身が new_page で開くため確実に 1 枚ずつ処理できる。
    """
    expected = 4
    ids = _collect_edit_ids(page)
    log(f"step 2KDL: image_ids 総数={len(ids)} / offset={offset}")
    target_ids = ids[offset : offset + expected]
    if len(target_ids) < expected:
        log(f"  [warn] 対象 image_id 不足: {len(target_ids)}/{expected}")

    before = len(sink.saved)
    sink.started_at = time.time()

    for i, img_id in enumerate(target_ids):
        log(f"  DL [{offset + i + 1}] {img_id[:8]}…")
        ok = download_via_edit_page(page.context, sink, img_id)
        if not ok:
            log(f"  [warn] {img_id[:8]} DL タイムアウト — 次へ")

    # Safety net: userscript が Downloads に書いた分を吸い上げ
    sink.adopt_userscript_fallback(expected=before + len(target_ids))

    got = len(sink.saved) - before
    if got < len(target_ids):
        log(
            f"  [warn] 期待 {len(target_ids)} 件 / 実 {got} 件。"
            "Chrome のダウンロード設定を確認してください。"
        )
    else:
        log(f"  ok: {got} 枚 DL 完了")


def upload_brand_new_reference(page: Page, png_path: Path) -> None:
    log(f"step 5: 参照画像 新規アップロード ({png_path.name})")
    add = (
        page.locator("button")
        .filter(has_text="add_2")
        .filter(has_text="作成")
        .first
    )
    add.scroll_into_view_if_needed()
    add.click()
    page.wait_for_timeout(600)

    try:
        with page.expect_file_chooser(timeout=5_000) as fc_info:
            upload_label = page.get_by_text(
                re.compile(r"(画像をアップロード|アップロード|Upload|ファイルを選択)", re.I)
            )
            if upload_label.count() > 0:
                upload_label.first.click()
            else:
                raise PWTimeoutError("no visible upload button")
        fc_info.value.set_files(str(png_path))
    except PWTimeoutError:
        file_input = page.locator("input[type='file']").first
        file_input.set_input_files(str(png_path))
    page.wait_for_timeout(2500)
    log("  upload 完了")


def reuse_asset(page: Page, index: int = 0) -> None:
    log(f"step 9: ＋パネルから既存アセットを採用 index={index}")
    add = (
        page.locator("button")
        .filter(has_text="add_2")
        .filter(has_text="作成")
        .first
    )
    add.scroll_into_view_if_needed()
    add.click()
    page.wait_for_timeout(800)

    candidates = [
        page.locator("[role='dialog'] img").nth(index),
        page.locator("[role='menu'] img").nth(index),
        page.locator("[data-testid*='asset']").nth(index),
        page.locator("[role='listbox'] img").nth(index),
    ]
    for cand in candidates:
        try:
            if cand.count() == 0:
                continue
            cand.scroll_into_view_if_needed()
            cand.click(timeout=3_000)
            page.wait_for_timeout(600)
            log("  既存アセットを採用")
            return
        except Exception:  # noqa: BLE001
            continue

    log("  [warn] 既存アセットを特定できませんでした。UI を確認してください。")
    page.keyboard.press("Escape")


# ===== バッチ実行 =======================================================
def run_batch(
    page: Page,
    batch_dir: Path,
    prompt: str,
    offset: int,
    context: BrowserContext | None = None,
) -> DownloadSink:
    sink = DownloadSink(batch_dir)
    if context is not None:
        sink.attach_context(context)  # popup タブ (userscript) も含めて DL 捕捉
    else:
        sink.attach(page)
    submit_prompt(page, prompt)
    wait_for_n_results(page, offset + 4)
    download_four_2k(page, sink, offset=offset)
    return sink


# ===== メイン ===========================================================
def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Google Flow で 2 バッチ（参照画像アップロード→生成→2K×4DL）を実行",
    )
    ap.add_argument(
        "--video-name",
        default=os.environ.get("VIDEO_NAME"),
        help="動画プロジェクト名 (例: 78_orzz_260420)。channel_folder/{video_name}/Image に保存",
    )
    ap.add_argument(
        "--flow-png",
        default=os.environ.get("FLOW_PNG"),
        help="参照画像パス（WEB アップロードの一時ファイル等）",
    )
    ap.add_argument(
        "--output-dir",
        default=os.environ.get("DL_ROOT"),
        help="保存先の明示指定。--video-name より優先",
    )
    ap.add_argument("--prompt1", default=os.environ.get("PROMPT_1", DEFAULT_PROMPT_1))
    ap.add_argument("--prompt2", default=os.environ.get("PROMPT_2", DEFAULT_PROMPT_2))
    ap.add_argument("--project-name", default=PROJECT_NAME)
    ap.add_argument("--headless", action="store_true")
    return ap.parse_args()


def main() -> int:
    args = parse_args()

    if not args.flow_png:
        log("[error] --flow-png が必要です（WEB からアップロードされた参照画像パス）")
        return 2
    flow_png = Path(args.flow_png).expanduser()
    if not flow_png.exists():
        log(f"[error] 参照画像が見つかりません: {flow_png}")
        return 2

    dl_root = resolve_output_dir(args.output_dir, args.video_name)
    dl_root.mkdir(parents=True, exist_ok=True)

    log(f"DL_ROOT       = {dl_root}")
    log(f"VIDEO_NAME    = {args.video_name or '(未指定)'}")
    log(f"FLOW_PNG      = {flow_png}")
    log(f"USER_DATA_DIR = {USER_DATA_DIR} (exists={USER_DATA_DIR.exists()})")
    log(f"USER_DOWNLOADS= {USER_DOWNLOADS}")

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    headful = HEADFUL and not args.headless
    launch_args = [
        # userscript が window.open('?gf-auto-dl=1') で popup を連続オープンする
        "--disable-popup-blocking",
        "--disable-features=BlockInsecurePrivateNetworkRequests",
    ]
    with sync_playwright() as p:
        # 永続プロファイルで起動 — 一度ログインすれば次回以降は不要
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            headless=not headful,
            slow_mo=SLOW_MO_MS,
            viewport=VIEWPORT,
            accept_downloads=True,
            locale="ja-JP",
            args=launch_args,
        )

        # 互換のため storage_state を取り込む（古い auth.json が残っている場合のみ）
        if AUTH_JSON.exists():
            try:
                import json as _json
                state = _json.loads(AUTH_JSON.read_text(encoding="utf-8"))
                if state.get("cookies"):
                    context.add_cookies(state["cookies"])
                    log("  auth.json から cookies を取り込み（初回移行）")
            except Exception as e:  # noqa: BLE001
                log(f"  [warn] auth.json 読込失敗: {e}")

        # Tampermonkey userscript を最初に注入（↓ 2K ボタンはこれが生成する）
        inject_userscript(context)

        page = open_flow(context)

        create_project_and_rename(page, args.project_name)
        set_aspect_and_count(page, ratio="16:9", count="x4")

        # バッチ1: chip クリア → 参照画像アップロード → PROMPT_1 → 4枚DL
        clear_prompt_chips(page)
        upload_brand_new_reference(page, flow_png)
        sink1 = run_batch(page, dl_root / "01", args.prompt1, offset=0, context=context)

        # バッチ2: chip クリア → 参照画像アップロード（同じファイル）→ PROMPT_2 → 4枚DL
        clear_prompt_chips(page)
        upload_brand_new_reference(page, flow_png)
        sink2 = run_batch(page, dl_root / "02", args.prompt2, offset=4, context=context)

        log("=== 完了 ===")
        for name, sink in [("01", sink1), ("02", sink2)]:
            files = sorted(sink.batch_dir.glob("*"))
            log(f"  {name}: saved={len(sink.saved)} / on_disk={len(files)}")

        context.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
