"""SRT 字幕とタイムコード（LOOP マーカー付き）の生成、file_prefix の解決。

元は app_premiere.py にあった Premiere に触れない純関数。段階 1a で Adobe 経路を
削除する際に、app_ffrender と音楽レビューが使う部分だけをこのモジュールへ移した。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from app_track_title import public_track_title


def generate_srt(clips, output_path):
    """クリップ情報から正確なSRTファイルを生成"""
    def to_srt_time(sec):
        ms = round(sec * 1000)
        h = ms // 3600000
        ms %= 3600000
        m = ms // 60000
        ms %= 60000
        s = ms // 1000
        ms %= 1000
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    with open(output_path, 'w', encoding='utf-8-sig') as f:
        for i, clip in enumerate(clips):
            if clip["end"] - clip["start"] < 0.001:
                continue
            f.write(f"{i + 1}\r\n")
            f.write(f"{to_srt_time(clip['start'])} --> {to_srt_time(clip['end'])}\r\n")
            f.write(f"{public_track_title(clip['title'])}\r\n")
            f.write("\r\n")

    print(f" SRT 生成: {output_path} ({len(clips)} エントリ)")


def generate_timecode(clips, output_path, total_songs=None):
    """タイムコード情報を生成（LOOPマーカー付き）"""
    def to_hhmmss(sec):
        sec = round(sec)
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    # 曲数を検出してループ境界を特定
    if total_songs is None:
        # ユニークなタイトルの数 = 1セット分の曲数
        seen = []
        for c in clips:
            if c["title"] in seen:
                break
            seen.append(c["title"])
        total_songs = len(seen)

    lines = []
    for i, clip in enumerate(clips):
        # ループ境界にマーカーを挿入
        if i > 0 and i % total_songs == 0:
            lines.append(f"{to_hhmmss(clip['start'])} - LOOP")
        lines.append(f"{to_hhmmss(clip['start'])} - {public_track_title(clip['title'])}")

    with open(output_path, 'w', encoding='utf-8') as f:
        for line in lines:
            f.write(line + "\n")

    print(f" タイムコード生成: {output_path} ({len(lines)} 行)")


def _resolve_orzz_config_dir() -> Path:
    """設定ディレクトリ（v2 配布化対応）"""
    try:
        import sys as _sys
        _sys.path.insert(0, str(Path(__file__).parent))
        from _app_config import resolve_config_dir as _r
        return _r()
    except Exception:
        return Path.home() / ".config" / "orzz"


def _read_file_prefix() -> str:
    """file_prefix を読む（既定 "vol"）。

    P2-2: APP_CHANNEL_FOLDER env が立っていれば、registry (channels.json) で
    フォルダ → entry を逆引きして該当チャンネルの prefix を返す（複数チャンネル
    並列実行で UI active channel の prefix が混入するのを防ぐ）。"""
    import os as _os
    import json as _j
    import re as _re
    cfg_dir = _resolve_orzz_config_dir()
    # 優先 1: env override + registry
    env_folder = (_os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    env_id = (_os.environ.get("APP_CHANNEL_ID") or "").strip()
    if env_folder or env_id:
        try:
            chs_path = cfg_dir / "channels.json"
            if chs_path.exists():
                chs = _j.loads(chs_path.read_text(encoding="utf-8")) or []
                target = ""
                if env_folder:
                    try:
                        target = str(Path(env_folder).expanduser().resolve())
                    except Exception:
                        target = env_folder
                for ch in chs:
                    if env_id and ch.get("id") == env_id:
                        raw = (ch.get("prefix") or "vol").strip()
                        return _re.sub(r"[^A-Za-z0-9_-]+", "", raw) or "vol"
                    if target:
                        try:
                            p = str(Path(ch.get("folder") or "").expanduser().resolve())
                            if p == target:
                                raw = (ch.get("prefix") or "vol").strip()
                                return _re.sub(r"[^A-Za-z0-9_-]+", "", raw) or "vol"
                        except Exception:
                            continue
        except Exception:
            pass
    # 優先 2: global dashboard_config の file_prefix（後方互換）
    try:
        cfg_path = cfg_dir / "dashboard_config.json"
        if cfg_path.exists():
            d = _j.loads(cfg_path.read_text(encoding="utf-8"))
            raw = (d.get("file_prefix") or "vol").strip()
            return _re.sub(r"[^A-Za-z0-9_-]+", "", raw) or "vol"
    except Exception:
        pass
    return "vol"
