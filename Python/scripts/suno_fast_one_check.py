"""SUNO実ワークスペースの先頭曲を検証用フォルダへ取得する。"""
import argparse
import json
from pathlib import Path
import sys
from playwright.sync_api import sync_playwright
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from resource_lock import ResourceLock
from suno_fast_dl import install_fast_capture, iter_workspace_rows, capture_song, convert_to_mp3, allocate_filename
from suno_fast_rows_check import _open_workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--target', default='/tmp/suno_fast_check')
    args = parser.parse_args()
    with ResourceLock('suno-download', owner='suno-fast-one-check'), sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            str(Path.home() / '.config/orzz/chromium_profile'), headless=False,
            args=['--disable-blink-features=AutomationControlled', '--no-first-run'],
            viewport={'width':1280, 'height':900}, ignore_default_args=['--enable-automation'])
        try:
            page = context.pages[0] if context.pages else context.new_page()
            install_fast_capture(page)
            opened = _open_workspace(page, args.workspace)
            rows = iter_workspace_rows(page)
            if opened['card_count'] is not None and len(rows) != opened['card_count']:
                raise RuntimeError('カード曲数と列挙結果が一致しません')
            page._suno_fast_row_pages = {r['song_id']:r['page_no'] for r in rows}
            row = rows[0]
            print('CAPTURE=' + json.dumps(row, ensure_ascii=False), flush=True)
            data = capture_song(page, row['song_id'], row['title'])
            dest = Path(args.target) / allocate_filename(args.target, row['title'], set())
            convert_to_mp3(data, dest)
            print('KEY_EXPORTABLE=' + str(page.evaluate('() => window.__sunoFastKeyExportable')))
            print(f'ONE OK file={dest} bytes={dest.stat().st_size}', flush=True)
        finally:
            context.close()

if __name__ == '__main__':
    main()
