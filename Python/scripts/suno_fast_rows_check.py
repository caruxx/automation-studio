"""永続プロファイルで実 Workspace の曲行だけを列挙する。音声は保存しない。"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
import time
from uuid import UUID

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resource_lock import ResourceLock
from suno_fast_dl import iter_workspace_rows


def _click_workspace(page, name):
    """カード内の名前を完全一致で探す。カードはリンクではなく div。"""
    labels = page.get_by_text(name, exact=True)
    card = page.locator('div[role="button"]').filter(has=labels).first
    target = card if card.count() else labels.first
    if not target.count() or not target.is_visible():
        return None
    text = target.inner_text()
    match = re.search(r"([\d,]+)\s+Songs?\b", text, re.IGNORECASE)
    count = int(match.group(1).replace(",", "")) if match else None
    target.click(timeout=5000)
    return {"card_count": count}


def _open_workspace(page, name):
    page.goto("https://suno.com/me/workspaces", wait_until="domcontentloaded", timeout=60000)
    # 初回の session-recovery リダイレクトと認証後の一覧描画を待つ。
    page.wait_for_timeout(10000)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        opened = _click_workspace(page, name)
        if opened is not None:
            page.wait_for_url(re.compile(r"/create\?.*\bwid="), timeout=30000)
            page.wait_for_timeout(8000)
            return opened
        # 遅延読み込みのカードも探す。曲行や再生ボタンはクリックしない。
        page.evaluate("""() => {
            for (const node of [document.scrollingElement, ...document.querySelectorAll('div')]) {
                if (node && node.scrollHeight > node.clientHeight
                    && /auto|scroll/.test(getComputedStyle(node).overflowY)) {
                    node.scrollTop = node.scrollHeight;
                }
            }
        }""")
        archived = page.get_by_role("button", name=re.compile(r"^Archived(?:\s|$)", re.I)).first
        if archived.count() and archived.is_visible() and archived.get_attribute("aria-expanded") == "false":
            archived.click()
        page.wait_for_timeout(1000)
    raise RuntimeError(f"Workspace カードが見つかりません: {name} url={page.url}")


def main() -> int:
    parser = argparse.ArgumentParser(description="SUNO Workspace の曲行を列挙し件数を検証する")
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()
    # 指定コマンド単独でも既存 SUNO 処理と競合しないよう共通ロックを使用。
    with ResourceLock("suno-download", owner="suno-fast-rows-check"), sync_playwright() as p:
        launch_kwargs = dict(
            user_data_dir=str(Path.home() / ".config/orzz/chromium_profile"), headless=False,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run"],
            viewport={"width": 1280, "height": 900},
            ignore_default_args=["--enable-automation"], accept_downloads=False,
        )
        try:
            context = p.chromium.launch_persistent_context(**launch_kwargs)
        except Exception:
            context = p.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.add_init_script("""(() => {
                window.__rowsCheckPlayEvents = 0;
                document.addEventListener('play', () => { window.__rowsCheckPlayEvents += 1; }, true);
            })();""")
            opened = _open_workspace(page, args.workspace)
            print(f"WORKSPACE={args.workspace} CARD_SONGS={opened['card_count']}", flush=True)
            rows = iter_workspace_rows(page, status_cb=lambda message: print(message, flush=True))
            play_events = page.evaluate("() => window.__rowsCheckPlayEvents")
            if play_events != 0:
                raise RuntimeError(f"検証中に再生イベントを検出しました: {play_events}")
        finally:
            context.close()
    for row in rows:
        if str(UUID(row["song_id"])) != row["song_id"]:
            raise RuntimeError(f"UUID 形式が不正です: {row['song_id']}")
    if len({row["song_id"] for row in rows}) != len(rows):
        raise RuntimeError("song_id が重複しています")
    if rows and not any(row["title"] and row["title"] != "suno-audio" for row in rows):
        raise RuntimeError("曲名を取得できていません")
    print("PAGES=" + json.dumps(dict(sorted(Counter(row["page_no"] for row in rows).items()))))
    print("FIRST3=" + json.dumps(rows[:3], ensure_ascii=False))
    print("LAST3=" + json.dumps(rows[-3:], ensure_ascii=False))
    print(f"UUID_OK={len(rows)} UNIQUE_OK={len(rows)} PLAY_EVENTS={play_events}")
    known_counts = {"orzz_vol158": 20, "orzz_vol159": 34}
    for expected in (opened["card_count"], known_counts.get(args.workspace)):
        if expected is not None and len(rows) != expected:
            print(f"ROWS CHECK FAILED: expected={expected} rows={len(rows)}", file=sys.stderr)
            print(f"ROWS={len(rows)}")
            return 1
    print(f"ROWS={len(rows)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ROWS CHECK FAILED: {e}", file=sys.stderr)
        sys.exit(1)
