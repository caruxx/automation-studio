"""既存CLI入口でWorkspace取得を検証する。モード指定もそのまま渡す。"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--target', default='/tmp/suno_fast_vol')
    args = parser.parse_args()
    cli = Path(__file__).resolve().parents[1] / 'suno_auto_create.py'
    mode = os.environ.get('APP_SUNO_DL_MODE', 'fast').strip().lower()
    env = dict(os.environ, APP_KEEP_BROWSER='0', PYTHONUNBUFFERED='1')
    print(f'CLI MODE={mode}', flush=True)
    subprocess.run([sys.executable, str(cli), '--download-workspace', args.workspace,
                    '--download-dir', args.target], env=env, check=True, timeout=900)
    if mode != 'legacy':
        report = json.loads((Path(args.target) / '.suno_fast_download.json').read_text())
        if report['failed'] or len(report['saved']) != report['expected']:
            raise RuntimeError('取得件数が不足しています')
        print(f"VOL OK count={len(report['saved'])}")


if __name__ == '__main__':
    main()
