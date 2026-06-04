#!/usr/bin/env python3
"""orzz. Dashboard — Web管理画面 (共有ドライブ版)"""

import asyncio
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import gzip
import platform
import unicodedata
import time
import threading
from pathlib import Path
from datetime import datetime, timezone
from collections import deque

from fastapi import FastAPI, WebSocket, HTTPException, UploadFile, File, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional, List

# ─── パス定義（ポータブル）───
HOME = Path.home()

# ─── 設定ディレクトリ解決（v2 配布化対応・共通モジュール経由） ───
# 旧 ~/.config/orzz/ から app_id を読み、~/.config/{app_id}/ を返す。
# 起動時に旧ディレクトリから新ディレクトリへ未存在ファイルだけ自動コピー（旧は残す）。
sys.path.insert(0, str(Path(__file__).parent))
from _app_config import (
    resolve_config_dir as _resolve_config_dir,
    resolve_app_id as _resolve_app_id,
    migrate_legacy_if_needed as _migrate_legacy_if_needed,
    LEGACY_CONFIG_DIR as _LEGACY_CONFIG_DIR,
)
# マイグレーション結果をモジュール変数で保持（API で返す）
_MIGRATION_RESULT = _migrate_legacy_if_needed(verbose=True)
CONFIG_DIR = _resolve_config_dir()
CONFIG_DIR.mkdir(parents=True, exist_ok=True)

# 共有ドライブのベースパス（優先順位: ORZZ_SHARED_BASE 環境変数 → 自動検出 → スクリプトの親の親）
def find_shared_drive():
    env_path = (os.environ.get("APP_SHARED_BASE") or os.environ.get("ORZZ_SHARED_BASE"))
    if env_path:
        p = Path(env_path).expanduser()
        if p.exists():
            return p
    for pattern in [
        HOME / "Library/CloudStorage" / "GoogleDrive-*" / "共有ドライブ/DEV/_claude",
        HOME / "Google Drive" / "共有ドライブ/DEV/_claude",
        Path("/Volumes/GoogleDrive/共有ドライブ/DEV/_claude"),
    ]:
        for p in Path(pattern.parent).glob(pattern.name) if '*' in str(pattern) else [pattern]:
            if p.exists():
                return p
    return Path(__file__).parent.parent  # フォールバック: スクリプトの親の親

SHARED_BASE = find_shared_drive()
WEB_DIR = SHARED_BASE / "web"
PYTHON_DIR = SHARED_BASE / "Python"

# 設定ファイル（ローカル: PC固有の設定）
DASHBOARD_CONFIG = CONFIG_DIR / "dashboard_config.json"
SUNO_CONFIG = CONFIG_DIR / "suno_config.json"
# チャンネルレジストリは PC 間で共有する（共有ドライブ上に置く）。
# per-channel 設定は各チャンネルフォルダ内 .app_channel_config.json で既に共有済みだが、
# 「どのチャンネルが存在するか」のレジストリだけがローカルだったため別 PC に伝播しなかった。
# 旧ローカル版(LOCAL_CHANNELS_CONFIG)は起動時に共有版へマージして引き継ぐ。
SHARED_CONFIG_DIR = SHARED_BASE / "config"
CHANNELS_CONFIG = SHARED_CONFIG_DIR / "channels.json"
LOCAL_CHANNELS_CONFIG = CONFIG_DIR / "channels.json"
PROMPTS_CONFIG = CONFIG_DIR / "prompts.json"
SCHEDULE_CONFIG = CONFIG_DIR / "schedule.json"
BENCHMARK_CONFIG = SHARED_CONFIG_DIR / "benchmark_config.json"  # チャンネル横断・全体共通（PC間共有）
SCHEDULE_JOBS_FILE = CONFIG_DIR / "schedule_jobs.json"   # APScheduler 用
AUTH_TOKEN_FILE = CONFIG_DIR / "auth_token.txt"

# 統合設定スキーマのバージョン（フロントが古い時の警告用）
CONFIG_SCHEMA_VERSION = 2

# 認証関連
def _get_or_create_auth_token() -> str:
    """auth_token を取得。dashboard_config.auth_token > AUTH_TOKEN_FILE > 自動生成。"""
    cfg = load_json(DASHBOARD_CONFIG, {}) if DASHBOARD_CONFIG.exists() else {}
    tok = (cfg.get("auth_token") or "").strip()
    if tok:
        return tok
    if AUTH_TOKEN_FILE.exists():
        try:
            return AUTH_TOKEN_FILE.read_text().strip()
        except Exception:
            pass
    new = secrets.token_urlsafe(24)
    try:
        AUTH_TOKEN_FILE.write_text(new)
        AUTH_TOKEN_FILE.chmod(0o600)
    except Exception:
        pass
    return new

# 環境変数で認証 ON/OFF（既定 OFF: 既存ローカル運用を壊さない）
# ORZZ_AUTH_REQUIRED=1 で全リクエストにトークン要求（127.0.0.1 はスキップ）
AUTH_REQUIRED = (os.environ.get("APP_AUTH_REQUIRED") or os.environ.get("ORZZ_AUTH_REQUIRED")) == "1"

# スクリプト（共有ドライブ優先 → ローカルフォールバック）
def find_script(name):
    shared = SHARED_BASE / "Python" / name
    local = CONFIG_DIR / name
    if shared.exists(): return shared
    if local.exists(): return local
    return local  # フォールバック

SUNO_SCRIPT = find_script("suno_auto_create.py")
YOUTUBE_SCRIPT = find_script("app_youtube.py")
NOTIFY_SCRIPT = find_script("app_notify.sh")

# ─── パス自動検出 ───
def auto_detect_paths():
    """共有ドライブから各種パスを自動検出"""
    result = {"channel_folder": "", "yt_root": ""}

    # Google Drive の共有ドライブを探す
    cloud_storage = HOME / "Library/CloudStorage"
    if not cloud_storage.exists():
        cloud_storage = HOME / "Google Drive"

    # YT フォルダを探す
    yt_candidates = list(cloud_storage.glob("GoogleDrive-*/共有ドライブ/YT")) if cloud_storage.exists() else []
    if not yt_candidates:
        yt_candidates = [p for p in [HOME / "Google Drive/共有ドライブ/YT"] if p.exists()]

    if yt_candidates:
        yt_root = yt_candidates[0]
        result["yt_root"] = str(yt_root)
        # 最初の orzz チャンネルフォルダを探す
        for d in sorted(yt_root.iterdir()):
            if d.is_dir() and "orzz" in d.name.lower():
                result["channel_folder"] = str(d)
                break
        # チャンネルが見つからなかったら最初のチャンネルフォルダ
        if not result["channel_folder"]:
            for d in sorted(yt_root.iterdir()):
                if d.is_dir() and not d.name.startswith('.'):
                    result["channel_folder"] = str(d)
                    break

    return result

_auto_paths = auto_detect_paths()

# ─── デフォルト設定（自動検出値を使用）───
DEFAULT_DASHBOARD_CONFIG = {
    # ─── ブランド表示（v2 配布化対応・全 UI でこの値が露出する） ───
    "brand_short": "Automation Studio",  # ヘッダ・タブタイトル等の短い表記
    "brand_full": "Automation Studio",   # FastAPI title / manifest フルネーム
    "app_id": "orzz",                    # 設定ディレクトリ名（後方互換のため既定 orzz、Phase B で利用）
    "file_prefix": "vol",                # 動画ファイル名 prefix（{file_prefix}_vol{N}.mp4 等で使用）
    # ─── チャンネル基本 ───
    "channel_name": "orzz.",
    "channel_folder": _auto_paths.get("channel_folder", ""),
    "template_prproj": "260207_orzz_base.prproj",
    "template_psd": "orzz_base.psd",
    # PSD テンプレ内のレイヤー名（チャンネルごとにカスタム可）
    "psd_base_layer": "base",            # 画像差し替え対象 SO レイヤー名（WW: Chicago_Willis）
    "psd_toggle_layer": "PLAY LIST",     # 表示/非表示で 2 種類書き出すレイヤー名（WW: WORKSPACE）
    "psd_image_subdir": "image",         # 動画フォルダ内の差し替え画像置き場（{video}/image/*）
    # 文字入れ（シーンテキスト）設定（チャンネル別。空なら persona 準拠の中立生成。Harbor Notes 流用は撤去済み）
    "scene_text_enabled": True,          # サムネに英大文字フレーズ（都市名_テキスト層）を入れるか
    "scene_text_tone": "",               # トーン指定（例: "chill, lo-fi, study, cozy"）
    "scene_text_examples": [],           # 語感の参考フレーズ（完全コピー禁止・style reference）
    "scene_text_forbidden": [],          # 完全一致を避けるフレーズ（ライバルの実焼込文字等）
    "scene_text_structure": "",          # 構文ヒント（空なら verb+noun / adjective+noun）
    "export_path": "",  # 外部SSD等の書き出し先（空ならチャンネルフォルダ内）
    "channel_icon": "",  # チャンネルアイコン画像パス
    "persona": "",  # チャンネルのペルソナ設定
    "rival_channels": [],  # ライバルチャンネルURL
    "spreadsheet_channel_detail_url": "",   # Sheet 1 CSV export URL
    "spreadsheet_growth_tracking_url": "",  # Sheet 2 CSV export URL
    "auth_token": "",
    # ベンチマーク絞り込み設定（スプシ取得時のフィルタ）
    "benchmark_pinned_names": [],   # 5-8 ch をピン留め（優先採用）
    "benchmark_filter": {
        "top_n": 15,
        "min_subs": 0,
        "max_subs": None,  # 例: 500000 で小〜中規模に限定
        "exclude_names": [],
    },
}

# ─── ユーティリティ ───
def load_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default if default is not None else {}

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    # セキュリティ: 機密ファイルは owner のみ読み書き (600)
    sensitive_names = {"youtube_client_secret.json", "youtube_token.json",
                       "discord_config.json", "line_config.json", "suno_config.json"}
    if path.name in sensitive_names:
        try:
            path.chmod(0o600)
        except Exception:
            pass

def get_dashboard_config():
    config = DEFAULT_DASHBOARD_CONFIG.copy()
    config.update(load_json(DASHBOARD_CONFIG, {}))
    # アクティブチャンネル別の設定で上書き（Google Drive 同期対応）
    cc = load_channel_config()
    for k in PER_CHANNEL_KEYS:
        if k in cc:
            config[k] = cc[k]
    return config


# ─── チャンネル別設定（Google Drive 同期対応） ───
# これらのキーは <channel_folder>/.app_channel_config.json に保存される。
# 共有ドライブ上のチャンネルフォルダを介して 2 PC 間で自動同期される。
PER_CHANNEL_KEYS = {
    "persona", "rival_channels",
    "spreadsheet_channel_detail_url", "spreadsheet_growth_tracking_url",
    "benchmark_pinned_names", "benchmark_filter", "benchmark_extra_urls",
    "channel_icon", "template_prproj", "template_psd", "export_path",
    "psd_base_layer", "psd_toggle_layer", "psd_image_subdir",
    "scene_text_enabled", "scene_text_tone", "scene_text_examples",
    "scene_text_forbidden", "scene_text_structure",  # 文字入れ（シーンテキスト）設定（チャンネル別）
    "export_ignore_list",  # AME 書き出し watcher が無視する video_name のリスト（2 PC 間自動同期）
    "publish_mode",  # P3-3: 公開方式（unlisted=限定公開 / public=即時公開 / delayed=N時間後自動公開）
    "publish_delay_hours",  # P2-7: upload 後の公開ゲート（0=即時、>0=N時間後 public化）
    "reference_image_dir",  # step_bgimage: 参照画像フォルダ（空なら Picked → rival thumbs にフォールバック）
    "reference_image",  # step_bgimage: 固定参照画像（最優先。水辺プロムナード等の代表参照）
    "default_duration_sec",  # Premiere 自動配置の規定尺（秒）。未設定/0 以下なら 10800 (3h) にフォールバック
    "priority",  # U3: orchestrator policy 配分の channel 優先度（大きいほど優先。既定 100）
    "autopilot_enabled",  # U3: 自走運用 ON/OFF（保存のみ。実 scheduler 起動は別途 GO 後）
}

# グローバル維持（マシン別）: API キー、OAuth トークン、ブランド設定など
GLOBAL_ONLY_KEYS = {
    "brand_short", "brand_full", "app_id", "file_prefix",
    "channel_name", "channel_folder",   # アクティブチャンネルポインタ
    "youtube_api_key", "sheets_api_key", "auth_token",
    "script_folder",
}

_CHANNEL_CONFIG_FILENAME = ".app_channel_config.json"
_MIGRATED_CHANNEL_FOLDERS = set()


def _active_channel_folder() -> Optional[Path]:
    raw = load_json(DASHBOARD_CONFIG, {}) or {}
    f = raw.get("channel_folder")
    if not f:
        return None
    p = Path(f)
    return p if p.exists() else None


def _channel_config_path() -> Optional[Path]:
    folder = _active_channel_folder()
    if not folder:
        return None
    return folder / _CHANNEL_CONFIG_FILENAME


def load_channel_config() -> dict:
    """アクティブチャンネルの per-channel 設定を読み込む。
    ファイルが無ければ {}。初回読込時に旧グローバル設定からマイグレートする。"""
    p = _channel_config_path()
    if not p:
        return {}
    folder_key = str(p.parent)
    # 1 回だけマイグレーション: ファイルが無くてグローバルにデータがあれば移行
    if folder_key not in _MIGRATED_CHANNEL_FOLDERS:
        _MIGRATED_CHANNEL_FOLDERS.add(folder_key)
        if not p.exists():
            try:
                _migrate_channel_config_initial(p)
            except Exception as e:
                print(f"⚠ チャンネル設定マイグレート失敗: {e}")
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_channel_config(data: dict) -> bool:
    """アクティブチャンネルの per-channel 設定を保存。last_modified メタを付与。"""
    p = _channel_config_path()
    if not p:
        return False
    import getpass
    import platform as _pf
    data = {**data}
    data["_last_modified_at"] = datetime.utcnow().isoformat() + "Z"
    data["_last_modified_by"] = f"{getpass.getuser()}@{_pf.node()}"
    data["_schema_version"] = 1
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True


def _migrate_channel_config_initial(target_path: Path):
    """初回読込時のマイグレーション: 旧グローバル設定 (dashboard_config.json, suno_config.json,
    master_prompts.json, prompts.json, youtube_upload_defaults.json) から per-channel データを移植。"""
    raw_dashboard = load_json(DASHBOARD_CONFIG, {}) or {}
    seed = {}
    for k in PER_CHANNEL_KEYS:
        v = raw_dashboard.get(k)
        if v not in (None, "", [], {}):
            seed[k] = v
    # SUNO（api_key 以外）
    suno_raw = load_json(SUNO_CONFIG, {}) or {}
    suno_part = {k: v for k, v in suno_raw.items() if k != "api_key"}
    if suno_part:
        seed["suno"] = suno_part
    # master_prompts
    mp = load_json(MASTER_PROMPTS_FILE, {}) or {}
    if mp:
        seed["master_prompts"] = mp
    # prompts library
    pl = load_json(PROMPTS_CONFIG, {}) or {}
    if pl:
        seed["prompts_library"] = pl
    # youtube_upload_defaults
    yu = load_json(YT_UPLOAD_DEFAULTS_FILE, {}) or {}
    if yu:
        seed["youtube_upload_defaults"] = yu
    if not seed:
        return
    import getpass
    import platform as _pf
    seed["_last_modified_at"] = datetime.utcnow().isoformat() + "Z"
    seed["_last_modified_by"] = f"{getpass.getuser()}@{_pf.node()}"
    seed["_schema_version"] = 1
    seed["_migrated_from_global"] = True
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(seed, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"✓ チャンネル別設定をマイグレート: {target_path}")


def update_channel_config_keys(updates: dict):
    """per-channel キーを部分更新。指定キーのみ書き換え、他は保持。"""
    cc = load_channel_config()
    for k, v in updates.items():
        cc[k] = v
    save_channel_config(cc)


def split_per_channel_keys(data: dict):
    """dict を {global, per_channel} に分割。"""
    g, c = {}, {}
    for k, v in data.items():
        if k in PER_CHANNEL_KEYS:
            c[k] = v
        else:
            g[k] = v
    return g, c


def save_dashboard_config_smart(full_config: dict):
    """dashboard_config.json と per-channel config に振り分けて保存。
    既存の `save_json(DASHBOARD_CONFIG, ...)` を置き換えるラッパー。"""
    g, c = split_per_channel_keys(full_config)
    save_json(DASHBOARD_CONFIG, g)
    if c:
        update_channel_config_keys(c)


def get_file_prefix() -> str:
    """動画ファイル / フォルダの命名用 prefix。

    dashboard_config.json の file_prefix > 既定 "vol" の順。
    例: file_prefix="vol" → "78_vol_260420/vol_vol78.mp4"
        file_prefix="orzz" → "78_orzz_260420/orzz_vol78.mp4"（既存互換）
        file_prefix="mybgm" → "78_mybgm_260420/mybgm_vol78.mp4"
    半角英数 _- のみに正規化。
    """
    return sanitize_file_prefix(get_dashboard_config().get("file_prefix") or "vol")


def sanitize_file_prefix(raw: Optional[str], fallback: str = "vol") -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "", (raw or "").strip())
    return safe or fallback


VIDEO_FOLDER_RE = re.compile(r"^(\d+)_([A-Za-z0-9_-]+)_(\d{6})(?:$|[\s_].*)")


def parse_video_folder_name(name: str) -> Optional[dict]:
    """動画フォルダ名を解析する。

    対応例:
      1_WW_260327
      1_vol_260430
      6_WW_260401 のコピー
    """
    m = VIDEO_FOLDER_RE.match(name)
    if not m:
        return None
    num_s, prefix, date_s = m.groups()
    try:
        publish_date = f"20{date_s[:2]}-{date_s[2:4]}-{date_s[4:6]}"
    except Exception:
        publish_date = ""
    return {
        "num": int(num_s),
        "num_text": num_s,
        "prefix": prefix,
        "date": date_s,
        "publish_date": publish_date,
    }


def infer_file_prefix_from_folder(channel_dir: Path) -> str:
    """既存動画フォルダから最頻 prefix を推定する。"""
    counts: dict[str, int] = {}
    if not channel_dir.exists():
        return ""
    for d in channel_dir.iterdir():
        if not d.is_dir():
            continue
        info = parse_video_folder_name(d.name)
        if not info:
            continue
        p = info["prefix"]
        counts[p] = counts.get(p, 0) + 1
    if not counts:
        return ""
    return sorted(counts.items(), key=lambda x: (-x[1], x[0].lower()))[0][0]


def _norm_folder(f) -> str:
    """フォルダパスを実体ベースに正規化する（比較キー用）。
    - Google Drive は ~/Google Drive と ~/Library/CloudStorage/GoogleDrive-* の
      2 系統パスで同じ実体を指すため、resolve() で実パスに畳む。
    - macOS の FS から resolve() で得たパスは NFD（濁点が結合文字に分解）になる一方、
      JSON 由来の文字列は NFC のことが多く、見た目同一でも != になる。NFC に統一して吸収。"""
    if not f:
        return ""
    try:
        s = str(Path(f).expanduser().resolve())
    except Exception:
        s = os.path.normpath(str(f))
    return unicodedata.normalize("NFC", s)


def _yt_root_candidates() -> List[Path]:
    """チャンネルフォルダを束ねる YT ルートの候補を返す（存在するもののみ・実体で重複排除）。"""
    roots: List[Path] = []
    # 既存チャンネルの folder の親（最も確実）
    for ch in (load_json(CHANNELS_CONFIG, []) or []):
        f = ch.get("folder")
        if f:
            roots.append(Path(f).parent)
    for ch in (load_json(LOCAL_CHANNELS_CONFIG, []) or []):
        f = ch.get("folder")
        if f:
            roots.append(Path(f).parent)
    # 標準パターン（共有ドライブ/YT）
    roots += list((HOME / "Library/CloudStorage").glob("GoogleDrive-*/共有ドライブ/YT"))
    seen, out = set(), []
    for r in roots:
        if not (r.exists() and r.is_dir()):
            continue
        rs = _norm_folder(r)
        if rs not in seen:
            seen.add(rs)
            out.append(r)
    return out


def sync_channel_registry(verbose: bool = True) -> list:
    """チャンネルレジストリ(channels.json)を PC 間で同期・自己修復する。

    1) 共有版を基準に、旧ローカル版(LOCAL_CHANNELS_CONFIG)の未登録 id をマージ（初回移行）。
    2) YT フォルダを走査し、.app_channel_config.json を持つ「番号付き」フォルダで
       未登録のものを自動的にチャンネルとして登録（別 PC で作られたチャンネルを取りこぼさない）。
    結果を共有版へ書き戻して返す。
    """
    try:
        SHARED_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        if verbose:
            print(f"[registry] shared dir 作成失敗: {e} — ローカルにフォールバック")
        return load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []

    shared = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []

    # 順序を保ちつつ id 重複を排除して取り込む（共有版を正とし、先に出たものを優先）
    entries: list = []
    seen_ids: set = set()
    seen_folders: set = set()

    def _add(c: dict) -> bool:
        cid = c.get("id")
        folder_key = _norm_folder(c.get("folder", ""))
        # id 重複・同一実体フォルダ重複はスキップ（先勝ち＝既存の手書き id を温存）
        if not cid or cid in seen_ids:
            return False
        if folder_key and folder_key in seen_folders:
            return False
        seen_ids.add(cid)
        if folder_key:
            seen_folders.add(folder_key)
        entries.append(c)
        return True

    for c in shared:
        _add(c)

    # 1) 旧ローカル版のマージ（共有に無いものだけ取り込む＝共有を正とする）
    for c in (load_json(LOCAL_CHANNELS_CONFIG, []) or []):
        if _add(c) and verbose:
            print(f"[registry] ローカルから移行: {c.get('name')} ({c.get('id')})")

    # 2) YT フォルダ自動スキャンで未登録チャンネルを発見
    for yt in _yt_root_candidates():
        try:
            children = sorted(yt.iterdir(), key=lambda p: p.name)
        except Exception:
            continue
        for d in children:
            if not d.is_dir():
                continue
            # 規約: チャンネルフォルダは "<番号>.<名前>" 形式（非チャンネルフォルダを除外）
            if not re.match(r'^\d+[\.\．]', d.name):
                continue
            if not (d / _CHANNEL_CONFIG_FILENAME).exists():
                continue
            if _norm_folder(str(d)) in seen_folders:
                continue
            name = re.sub(r'^\d+[\.\．]\s*', '', d.name).strip('【】[]　 ').strip() or d.name
            base_cid = re.sub(r'[^a-zA-Z0-9]', '_', name.lower()).strip('_') or f"ch{len(entries)}"
            cid, n = base_cid, 2
            while cid in seen_ids:
                cid = f"{base_cid}_{n}"
                n += 1
            cc = load_json(d / _CHANNEL_CONFIG_FILENAME, {}) or {}
            entry = {
                "id": cid, "name": name, "folder": str(d),
                "prefix": infer_file_prefix_from_folder(d) or "",
                "template_prproj": cc.get("template_prproj", ""),
                "template_psd": cc.get("template_psd", ""),
                "youtube_url": "", "youtube_channel_id": "", "handle": "", "icon_cache": {},
            }
            if _add(entry) and verbose:
                print(f"[registry] 新規チャンネル検出: {name} ({d})")

    save_json(CHANNELS_CONFIG, entries)
    if verbose:
        print(f"[registry] synced: {len(entries)} channels -> {CHANNELS_CONFIG}")
    return entries


def migrate_benchmark_to_shared(verbose: bool = True) -> None:
    """ベンチマークのプロファイル/設定/分析を旧ローカル(CONFIG_DIR)から共有(SHARED_CONFIG_DIR)へ
    未存在のみコピー移行する（初回のみ実質コピー、以降は no-op）。
    サムネ「画像」(benchmark/thumbs)は容量が大きいためローカル維持＝移行しない。"""
    try:
        if _norm_folder(str(CONFIG_DIR)) == _norm_folder(str(SHARED_CONFIG_DIR)):
            return  # フォールバック時は同一 → 何もしない
        SHARED_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        copied = []
        # 1) 単一ファイル
        for fn in ("benchmark_profiles.json", "benchmark_config.json", "competitor_analysis_cache.json"):
            s, d = CONFIG_DIR / fn, SHARED_CONFIG_DIR / fn
            if s.exists() and not d.exists():
                shutil.copy2(s, d)
                copied.append(fn)
        # 2) benchmark 配下（scoped 分析・legacy 直下 json）。ただし thumbs(画像)は除外
        src_bench = CONFIG_DIR / "benchmark"
        if src_bench.exists():
            for s in src_bench.rglob("*"):
                rel = s.relative_to(src_bench)
                if "thumbs" in rel.parts:
                    continue  # 画像はローカル維持
                d = SHARED_CONFIG_DIR / "benchmark" / rel
                if s.is_dir():
                    d.mkdir(parents=True, exist_ok=True)
                    continue
                if not d.exists():
                    d.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(s, d)
                    copied.append(str(Path("benchmark") / rel))
        if verbose and copied:
            print(f"[benchmark] 共有へ移行: {len(copied)} 件 -> {SHARED_CONFIG_DIR}")
    except Exception as e:
        print(f"[benchmark] 移行失敗（続行）: {e}")


def folder_name_pattern() -> "re.Pattern":
    """フォルダ識別の正規表現。新形式と旧 orzz 形式の両方を認識。

    マッチ例: "78_orzz_260420", "79_vol_260427", "80_mybgm_260501"
    group(1)=番号, group(2)=prefix
    """
    return VIDEO_FOLDER_RE

def get_suno_config():
    """SUNO 設定を返す。
    - api_key / claude_cli / codex_cli はグローバル（マシン別、~/.config/{app_id}/suno_config.json）
    - prompt / mode / count / batch 等のチャンネル依存パラメータは per-channel
      （<channel_folder>/.app_channel_config.json["suno"]）でグローバルを上書き
    """
    base = load_json(SUNO_CONFIG, {
        "provider": "gemini", "model": "gemini-3-flash-preview",
        "api_key": "", "claude_cli": "claude", "codex_cli": "codex",
        "generation_mode": "styles_title_only",
        "prompt": "", "loop_count": 5, "loop_interval_sec": 180,
    })
    cc = load_channel_config()
    suno_cc = cc.get("suno") or {}
    for k, v in suno_cc.items():
        # api_key と CLI コマンドはマシン別を維持（per-channel 値があっても無視）
        if k in ("api_key", "claude_cli", "codex_cli", "provider", "model", "headless"):
            continue
        base[k] = v
    return base


# 機密キー（チャンネルファイルに書かない）
_SUNO_GLOBAL_KEYS = {"api_key", "claude_cli", "codex_cli", "provider", "model", "headless"}


def save_suno_config_smart(patch: dict):
    """SUNO 設定の部分更新を per-channel と global に振り分け保存。
    - api_key / claude_cli / codex_cli / provider / model / headless → ~/.config/{app_id}/suno_config.json
    - prompt / generation_mode / loop_count / loop_interval_sec / loop_batch / loop_instrumental_fill 等 → per-channel
    patch は変更したいキーだけを含む dict。
    """
    global_part = {}
    channel_part = {}
    for k, v in patch.items():
        if k in _SUNO_GLOBAL_KEYS:
            global_part[k] = v
        else:
            channel_part[k] = v
    if global_part:
        cur = load_json(SUNO_CONFIG, {})
        cur.update(global_part)
        save_json(SUNO_CONFIG, cur)
    if channel_part:
        cc = load_channel_config()
        suno = cc.get("suno") or {}
        suno.update(channel_part)
        cc["suno"] = suno
        save_channel_config(cc)

# ─── ベンチマーク設定（グローバル既定 + チャンネル別 override）───
DEFAULT_BENCHMARK_CONFIG = {
    "pinned_names": [],
    "filter": {
        "top_n": 15,
        "min_subs": 0,
        "max_subs": None,
        "exclude_names": [],
    },
    "spreadsheet_channel_detail_url": "",
    "spreadsheet_growth_tracking_url": "",
}

def _migrate_benchmark_config():
    """旧 dashboard_config.json から benchmark_config.json へ初回のみ移行。
    Why: 複数チャンネル運営時に分析結果・対象設定を共通利用するため。
    """
    if BENCHMARK_CONFIG.exists():
        return
    dc = load_json(DASHBOARD_CONFIG, {})
    bc = DEFAULT_BENCHMARK_CONFIG.copy()
    bc["filter"] = {**bc["filter"], **(dc.get("benchmark_filter") or {})}
    if dc.get("benchmark_pinned_names"):
        bc["pinned_names"] = list(dc["benchmark_pinned_names"])
    if dc.get("spreadsheet_channel_detail_url"):
        bc["spreadsheet_channel_detail_url"] = dc["spreadsheet_channel_detail_url"]
    if dc.get("spreadsheet_growth_tracking_url"):
        bc["spreadsheet_growth_tracking_url"] = dc["spreadsheet_growth_tracking_url"]
    save_json(BENCHMARK_CONFIG, bc)

def get_benchmark_config():
    _migrate_benchmark_config()
    bc = DEFAULT_BENCHMARK_CONFIG.copy()
    loaded = load_json(BENCHMARK_CONFIG, {})
    bc.update(loaded)
    bc["filter"] = {**DEFAULT_BENCHMARK_CONFIG["filter"], **(loaded.get("filter") or {})}
    # フォールバック: dashboard_config.json にあれば借用（移行未完了時の互換）
    dc = load_json(DASHBOARD_CONFIG, {})
    if not bc.get("spreadsheet_channel_detail_url") and dc.get("spreadsheet_channel_detail_url"):
        bc["spreadsheet_channel_detail_url"] = dc["spreadsheet_channel_detail_url"]
    if not bc.get("spreadsheet_growth_tracking_url") and dc.get("spreadsheet_growth_tracking_url"):
        bc["spreadsheet_growth_tracking_url"] = dc["spreadsheet_growth_tracking_url"]
    cc = load_channel_config()
    if "benchmark_pinned_names" in cc:
        bc["pinned_names"] = list(cc.get("benchmark_pinned_names") or [])
    if isinstance(cc.get("benchmark_filter"), dict):
        bc["filter"] = {**DEFAULT_BENCHMARK_CONFIG["filter"], **(cc.get("benchmark_filter") or {})}
    if "spreadsheet_channel_detail_url" in cc:
        bc["spreadsheet_channel_detail_url"] = cc.get("spreadsheet_channel_detail_url") or ""
    if "spreadsheet_growth_tracking_url" in cc:
        bc["spreadsheet_growth_tracking_url"] = cc.get("spreadsheet_growth_tracking_url") or ""
    return bc


def save_benchmark_config(cfg: dict):
    """ベンチマーク設定は active channel があれば per-channel に保存する。"""
    if _channel_config_path():
        cc = load_channel_config()
        cc["benchmark_pinned_names"] = list(cfg.get("pinned_names") or [])
        cc["benchmark_filter"] = {**DEFAULT_BENCHMARK_CONFIG["filter"], **(cfg.get("filter") or {})}
        cc["spreadsheet_channel_detail_url"] = cfg.get("spreadsheet_channel_detail_url") or ""
        cc["spreadsheet_growth_tracking_url"] = cfg.get("spreadsheet_growth_tracking_url") or ""
        save_channel_config(cc)
    else:
        save_json(BENCHMARK_CONFIG, cfg)

# 起動時マイグレーション
_migrate_benchmark_config()

def open_in_finder(path):
    """Finder でフォルダを開く（macOS）/ Explorer（Windows）"""
    p = Path(path)
    if not p.exists():
        return False
    system = platform.system()
    if system == "Darwin":
        subprocess.Popen(["open", str(p)])
    elif system == "Windows":
        subprocess.Popen(["explorer", str(p)])
    else:
        subprocess.Popen(["xdg-open", str(p)])
    return True

# ─── FastAPI ───
app = FastAPI(title=load_json(DASHBOARD_CONFIG, {}).get("brand_full") or DEFAULT_DASHBOARD_CONFIG["brand_full"])

# 静的ファイル
static_dir = WEB_DIR / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

# ─── 認証ミドルウェア（Sprint 5-C） ───
# ORZZ_AUTH_REQUIRED=1 のときのみ有効。loopback アドレスはスキップ。
_AUTH_PUBLIC_PATHS = {"/login.html", "/login", "/api/auth/login", "/api/auth/check",
                       "/manifest.json", "/sw.js", "/favicon.ico"}

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not AUTH_REQUIRED:
        return await call_next(request)
    path = request.url.path
    # 公開パス + 静的ファイル + WebSocket は通過
    if path in _AUTH_PUBLIC_PATHS or path.startswith("/static/") or path.startswith("/ws/"):
        return await call_next(request)
    # ローカルホスト（PC 自身からのアクセス）は通過
    client_host = (request.client.host if request.client else "") or ""
    if client_host in ("127.0.0.1", "::1", "localhost"):
        return await call_next(request)
    # トークンチェック
    expected = _get_or_create_auth_token()
    received = ""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        received = auth[7:].strip()
    if not received:
        received = request.cookies.get("orzz_token", "")
    if not secrets.compare_digest(received, expected):
        # /login.html にリダイレクト（HTML リクエストの場合のみ）
        accept = request.headers.get("accept", "")
        if "text/html" in accept and not path.startswith("/api/"):
            from fastapi.responses import RedirectResponse
            return RedirectResponse(url=f"/login.html?next={path}", status_code=302)
        return JSONResponse({"detail": "認証が必要です"}, status_code=401)
    return await call_next(request)

# バックグラウンドタスク
active_tasks = {}
task_logs = {}
task_meta = {}  # {task_id: {"started_at": iso, ...}}
_active_tasks_lock = asyncio.Lock()

_youtube_upload_queue = deque()
_youtube_queue_lock = threading.Lock()
_youtube_worker_thread = None
_youtube_current_job = None
_youtube_job_seq = 0

async def _ensure_not_running(key, err_msg):
    async with _active_tasks_lock:
        proc = active_tasks.get(key)
        if proc is not None and proc.returncode is None and proc.poll() is None:
            raise HTTPException(400, err_msg)

# ─── タスク履歴の永続化 ───
TASK_HISTORY_FILE = CONFIG_DIR / "task_history.json"

def _load_task_history():
    """サーバー起動時に前回のログを復元"""
    global task_logs, task_meta
    if not TASK_HISTORY_FILE.exists():
        return
    try:
        data = json.loads(TASK_HISTORY_FILE.read_text(encoding="utf-8"))
        task_logs.update(data.get("logs", {}))
        task_meta.update(data.get("meta", {}))
    except Exception:
        pass

def _save_task_history():
    """完了ログをファイルに保存（完了済みタスクのみ）"""
    try:
        # 実行中のタスクはスキップ
        done_logs = {}
        done_meta = {}
        for k, v in task_logs.items():
            proc = active_tasks.get(k)
            if proc and proc.returncode is None:
                continue  # まだ走っている
            done_logs[k] = v[-500:]  # 最大500行保持
            if k in task_meta:
                done_meta[k] = task_meta[k]
        data = {"logs": done_logs, "meta": done_meta}
        TASK_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        TASK_HISTORY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass

# 起動時に復元
_load_task_history()

def _stream_subprocess(proc, task_key):
    """Popen の stdout を非同期で task_logs に流し込む共通ヘルパー。
    例外を捕捉して [エラー] 行として記録する。"""
    async def _reader():
        loop = asyncio.get_event_loop()
        try:
            while True:
                try:
                    line = await loop.run_in_executor(None, proc.stdout.readline)
                except Exception as e:
                    task_logs.setdefault(task_key, []).append(f"[エラー] readline: {e}")
                    break
                if not line and proc.poll() is not None:
                    break
                if line:
                    task_logs.setdefault(task_key, []).append(line.rstrip())
            task_logs.setdefault(task_key, []).append(f"[完了] 終了コード: {proc.returncode}")
        except Exception as e:
            task_logs.setdefault(task_key, []).append(f"[エラー] read_output: {e}")
        finally:
            try:
                _save_task_history()
            except Exception:
                pass
    asyncio.create_task(_reader())


def _append_task_log(task_key: str, line: str, max_lines: int = 1000) -> None:
    logs = task_logs.setdefault(task_key, [])
    logs.append(line)
    if len(logs) > max_lines:
        task_logs[task_key] = logs[-max_lines:]


def _public_youtube_job(job: Optional[dict]) -> Optional[dict]:
    if not job:
        return None
    return {k: v for k, v in job.items() if k != "cmd"}


def _youtube_queue_snapshot() -> dict:
    with _youtube_queue_lock:
        return {
            "current": _public_youtube_job(_youtube_current_job),
            "queue": [_public_youtube_job(j) for j in list(_youtube_upload_queue)],
            "queued_count": len(_youtube_upload_queue),
        }


def _next_youtube_job_id() -> int:
    global _youtube_job_seq
    _youtube_job_seq += 1
    return _youtube_job_seq


def _youtube_worker_loop() -> None:
    global _youtube_current_job, _youtube_worker_thread
    while True:
        with _youtube_queue_lock:
            if not _youtube_upload_queue:
                _youtube_current_job = None
                _youtube_worker_thread = None
                return
            job = _youtube_upload_queue.popleft()
            job["status"] = "running"
            job["started_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            _youtube_current_job = job
            remaining = len(_youtube_upload_queue)

        _append_task_log("youtube", f"[queue] 開始 #{job['id']}: {job.get('video_name') or job.get('folder')} / 残り {remaining} 件")
        proc = None
        try:
            proc = subprocess.Popen(
                job["cmd"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            active_tasks["youtube"] = proc
            if proc.stdout:
                for line in proc.stdout:
                    if line:
                        _append_task_log("youtube", line.rstrip())
            rc = proc.wait()
            job["returncode"] = rc
            job["finished_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            job["status"] = "done" if rc == 0 else "error"
            _append_task_log("youtube", f"[queue] 完了 #{job['id']}: 終了コード {rc}")
        except Exception as e:
            job["status"] = "error"
            job["finished_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
            job["error"] = str(e)[:300]
            _append_task_log("youtube", f"[queue] エラー #{job.get('id')}: {e}")
        finally:
            if active_tasks.get("youtube") is proc:
                active_tasks.pop("youtube", None)
            try:
                _save_task_history()
            except Exception:
                pass


def _ensure_youtube_worker_locked() -> None:
    global _youtube_worker_thread
    if _youtube_worker_thread and _youtube_worker_thread.is_alive():
        return
    _youtube_worker_thread = threading.Thread(
        target=_youtube_worker_loop, daemon=True, name="youtube-upload-queue"
    )
    _youtube_worker_thread.start()


def _enqueue_youtube_upload(cmd: list[str], meta: dict, source: str = "manual") -> dict:
    with _youtube_queue_lock:
        job = {
            "id": _next_youtube_job_id(),
            "cmd": cmd,
            "status": "queued",
            "source": source,
            "folder": meta.get("folder", ""),
            "video_name": meta.get("video_name", ""),
            "video_path": meta.get("video_path", ""),
            "privacy": meta.get("privacy", ""),
            "title": meta.get("title", ""),
            "schedule": meta.get("schedule", ""),
            "enqueued_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        }
        idle = not _youtube_current_job and not _youtube_upload_queue
        if idle:
            task_logs["youtube"] = []
        _youtube_upload_queue.append(job)
        position = len(_youtube_upload_queue)
        _append_task_log("youtube", f"[queue] 受付 #{job['id']}: {job.get('video_name') or job.get('folder')} / 待ち順 {position}")
        _ensure_youtube_worker_locked()
    return {"status": "queued", "job": _public_youtube_job(job), "position": position}

# ─── API: 設定 ───
def _mask_secret(val, visible=4):
    """シークレット値をマスク（先頭N文字のみ表示）"""
    if not val or len(val) <= visible:
        return val
    return val[:visible] + "●" * min(8, len(val) - visible)

@app.get("/api/config/migration-status")
def api_config_migration_status():
    """v2 配布化マイグレーション結果。フロントの起動バナー表示で使用。

    Returns:
      {
        performed: bool,           # 今回起動でコピーが走ったか
        src: str,                  # 旧ディレクトリ
        dst: str,                  # 新ディレクトリ
        copied: [...],             # コピーしたファイル相対パス
        skipped: [{path, reason}], # スキップしたもの
        skipped_reason: str,       # 全体スキップ理由（同一/旧無し/既マイグレ済）
        copied_count: int,
        legacy_kept: bool,         # 旧ディレクトリが残っているか（ロールバック用）
        migration_log: str,        # 末尾 5 行の履歴
        app_id: str,               # 解決した app_id
      }
    """
    log_path = Path(_resolve_config_dir()) / "migration.log"
    log_tail = ""
    if log_path.exists():
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
            log_tail = "\n".join(lines[-5:])
        except Exception:
            pass
    return {
        **_MIGRATION_RESULT,
        "copied_count": len(_MIGRATION_RESULT.get("copied", [])),
        "legacy_kept": _LEGACY_CONFIG_DIR.exists(),
        "migration_log": log_tail,
        "app_id": _resolve_app_id(),
    }


@app.get("/api/config")
def api_get_config():
    dc = get_dashboard_config()
    sc = get_suno_config()
    bc = get_benchmark_config()
    chs = get_channels()
    # API レスポンスではシークレットをマスク
    safe_sc = {**sc}
    if safe_sc.get("api_key"):
        safe_sc["api_key"] = _mask_secret(safe_sc["api_key"])
    safe_dc = {**dc}
    if safe_dc.get("auth_token"):
        safe_dc["auth_token"] = _mask_secret(safe_dc["auth_token"])
    return {
        "dashboard": safe_dc,
        "suno": safe_sc,
        "benchmark": bc,
        "channels": {
            "active_folder": safe_dc.get("channel_folder", ""),
            "list": chs,
        },
        "meta": {"schema_version": CONFIG_SCHEMA_VERSION},
    }

# ─── 統合 PUT: セクション指定で透過更新 ───
class UnifiedConfigUpdate(BaseModel):
    section: str  # "dashboard" | "suno" | "benchmark"
    patch: dict

@app.put("/api/config")
def api_put_unified_config(update: UnifiedConfigUpdate):
    section = (update.section or "").strip().lower()
    patch = update.patch or {}
    if section == "dashboard":
        cfg = get_dashboard_config()
        cfg.update(patch)
        save_dashboard_config_smart(cfg)
        return {"status": "ok", "section": section, "config": cfg}
    if section == "suno":
        save_suno_config_smart(patch)
        return {"status": "ok", "section": section, "config": get_suno_config()}
    if section == "benchmark":
        cfg = get_benchmark_config()
        # filter は浅マージ
        if "filter" in patch and isinstance(patch["filter"], dict):
            cfg["filter"] = {**cfg.get("filter", {}), **patch["filter"]}
            patch = {k: v for k, v in patch.items() if k != "filter"}
        cfg.update(patch)
        save_benchmark_config(cfg)
        return {"status": "ok", "section": section, "config": cfg}
    raise HTTPException(400, f"未知の section: {section}")

# ─── API: 自動化設定（チャンネル別） ───

def _automation_config_path() -> Path:
    """現行チャンネルの自動化設定ファイル。チャンネルフォルダ直下に保存"""
    cfg = get_dashboard_config()
    ch = Path(cfg.get("channel_folder") or "")
    if not ch or not ch.exists():
        # フォールバック: ~/.config/orzz/automation_default.json
        return CONFIG_DIR / "automation_default.json"
    return ch / "_automation.json"

@app.get("/api/automation/config")
def api_get_automation_config():
    p = _automation_config_path()
    if not p.exists():
        return {"config": {}, "path": str(p)}
    try:
        return {"config": json.loads(p.read_text(encoding="utf-8")), "path": str(p)}
    except Exception as e:
        raise HTTPException(500, f"読み込み失敗: {e}")

class AutomationConfigUpdate(BaseModel):
    suno_prompt: Optional[str] = None
    suno_provider: Optional[str] = None
    suno_mode: Optional[str] = None
    suno_count: Optional[int] = None
    suno_interval: Optional[int] = None
    suno_batch: Optional[bool] = None
    suno_headless: Optional[bool] = None
    suno_diversity_threshold: Optional[float] = None
    suno_diversity_retry: Optional[int] = None
    suno_history_limit: Optional[int] = None
    dl_wait_sec: Optional[int] = None
    premiere_duration: Optional[int] = None
    premiere_auto_export: Optional[bool] = None
    meta_title_count: Optional[int] = None
    upload_privacy: Optional[str] = None
    auto_upload: Optional[bool] = None
    thumb_provider: Optional[str] = None
    thumb_prompt: Optional[str] = None
    thumb_auto: Optional[bool] = None
    auto_approval_mode: Optional[bool] = None  # ON: YouTube upload まで無停止で実行

@app.put("/api/automation/config")
def api_put_automation_config(req: AutomationConfigUpdate):
    p = _automation_config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    body = req.dict(exclude_none=True)
    p.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"status": "ok", "config": body, "path": str(p)}


@app.get("/api/config/auto-detect")
def api_auto_detect():
    """現在のPCの共有ドライブからパスを自動検出"""
    detected = auto_detect_paths()
    return {
        "channel_folder": detected.get("channel_folder", ""),
        "yt_root": detected.get("yt_root", ""),
        "shared_drive": str(SHARED_BASE),
        "config_dir": str(CONFIG_DIR),
    }

@app.post("/api/config/init")
def api_config_init():
    """設定が未初期化なら自動検出値で初期化"""
    config = get_dashboard_config()
    changed = False
    detected = auto_detect_paths()

    if not config.get("channel_folder") and detected.get("channel_folder"):
        config["channel_folder"] = detected["channel_folder"]
        changed = True

    if changed:
        save_dashboard_config_smart(config)
    return {"status": "ok", "config": config, "changed": changed}

class DashboardConfigUpdate(BaseModel):
    brand_short: Optional[str] = None
    brand_full: Optional[str] = None
    app_id: Optional[str] = None
    file_prefix: Optional[str] = None
    channel_name: Optional[str] = None
    channel_folder: Optional[str] = None
    export_path: Optional[str] = None
    persona: Optional[str] = None
    rival_channels: Optional[List[str]] = None
    spreadsheet_channel_detail_url: Optional[str] = None
    spreadsheet_growth_tracking_url: Optional[str] = None
    youtube_api_key: Optional[str] = None
    sheets_api_key: Optional[str] = None
    auth_token: Optional[str] = None
    template_prproj: Optional[str] = None
    template_psd: Optional[str] = None
    psd_base_layer: Optional[str] = None
    psd_toggle_layer: Optional[str] = None
    psd_image_subdir: Optional[str] = None
    publish_mode: Optional[str] = None  # P3-3: unlisted / public / delayed
    publish_delay_hours: Optional[float] = None  # P2-7: upload 後 N 時間で public 化（0=即時）
    reference_image_dir: Optional[str] = None    # step_bgimage: 参照画像フォルダ（空文字でクリア）
    reference_image: Optional[str] = None        # step_bgimage: 固定参照画像（空文字でクリア）
    default_duration_sec: Optional[int] = None   # Premiere 自動配置の規定尺（秒）。0/空でクリア（10800 にフォールバック）

@app.put("/api/config/dashboard")
def api_update_dashboard_config(update: DashboardConfigUpdate):
    config = get_dashboard_config()
    # スプシ URL は CSV エクスポート形式へ自動正規化（/edit?gid=N → /gviz/tq?tqx=out:csv&gid=N）
    try:
        from app_sheets import normalize_sheet_url as _norm_sheet
    except Exception:
        _norm_sheet = lambda x: x
    for k, v in update.dict(exclude_none=True).items():
        if k == "file_prefix":
            config[k] = sanitize_file_prefix(v)
        elif k in ("spreadsheet_channel_detail_url", "spreadsheet_growth_tracking_url"):
            config[k] = _norm_sheet(v) if v else v
        elif k == "reference_image_dir":
            # step_bgimage: 参照画像フォルダ
            #   - 空文字 → フィールド削除（Picked → rival thumbs フォールバックに戻す）
            #   - `~` を展開して保存
            #   - 存在しなければ warning のみ（後から作るユースケースを許容）
            raw = (v or "").strip()
            if not raw:
                config["reference_image_dir"] = ""  # 空文字を保存（save_dashboard_config_smart 経由で per-channel に空が書かれる）
            else:
                expanded = str(Path(raw).expanduser())
                config["reference_image_dir"] = expanded
                try:
                    if not Path(expanded).is_dir():
                        print(f"⚠ reference_image_dir 保存: ディレクトリが存在しません ({expanded}) — 後から作成すれば step_bgimage で参照されます")
                except Exception:
                    pass
        elif k == "reference_image":
            # step_bgimage: 固定参照画像。指定時は reference_image_dir のランダム選択より優先。
            raw = (v or "").strip()
            if not raw:
                config["reference_image"] = ""
            else:
                expanded = str(Path(raw).expanduser())
                config["reference_image"] = expanded
                try:
                    if not Path(expanded).is_file():
                        print(f"⚠ reference_image 保存: ファイルが存在しません ({expanded}) — 後から作成すれば step_bgimage で参照されます")
                except Exception:
                    pass
        elif k == "default_duration_sec":
            # Premiere 自動配置の規定尺（秒）。
            #   - 正の整数 → そのまま保存（per-channel）
            #   - 0 / 負 / 数値化失敗 → per-channel から削除（read 時に 10800 へフォールバック）
            try:
                iv = int(v) if v not in (None, "") else 0
            except (TypeError, ValueError):
                iv = 0
            if iv > 0:
                config["default_duration_sec"] = iv
            else:
                config.pop("default_duration_sec", None)
                # save_dashboard_config_smart は「キーを送らない＝変更しない」なので、
                # 永続的に削除するには per-channel ファイルを直接書き換える必要がある。
                try:
                    cc = load_channel_config()
                    if "default_duration_sec" in cc:
                        cc.pop("default_duration_sec", None)
                        save_channel_config(cc)
                except Exception as _e:
                    print(f"⚠ default_duration_sec 削除に失敗: {_e}")
        else:
            config[k] = v
    save_dashboard_config_smart(config)
    if update.file_prefix is not None and config.get("channel_folder"):
        channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
        changed = False
        for ch in channels:
            if ch.get("folder") == config.get("channel_folder"):
                ch["prefix"] = config["file_prefix"]
                changed = True
        if changed:
            save_json(CHANNELS_CONFIG, channels)
    return {"status": "ok", "config": config}


@app.get("/api/config/infer-naming")
def api_infer_naming(folder: str = ""):
    config = get_dashboard_config()
    channel_dir = Path(folder or config.get("channel_folder") or "")
    if not channel_dir.exists():
        raise HTTPException(400, "チャンネルフォルダが存在しません")
    examples = []
    counts: dict[str, int] = {}
    max_num = 0
    for d in sorted(channel_dir.iterdir()):
        if not d.is_dir():
            continue
        info = parse_video_folder_name(d.name)
        if not info:
            continue
        counts[info["prefix"]] = counts.get(info["prefix"], 0) + 1
        max_num = max(max_num, info["num"])
        if len(examples) < 8:
            examples.append(d.name)
    prefix = infer_file_prefix_from_folder(channel_dir)
    return {
        "prefix": prefix,
        "counts": counts,
        "examples": examples,
        "max_num": max_num,
        "next_num": max_num + 1,
        "matched": sum(counts.values()),
    }

class SunoConfigUpdate(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    api_key: Optional[str] = None
    claude_cli: Optional[str] = None
    codex_cli: Optional[str] = None
    generation_mode: Optional[str] = None
    prompt: Optional[str] = None
    loop_count: Optional[int] = None
    loop_interval_sec: Optional[int] = None
    loop_batch: Optional[bool] = None

@app.put("/api/config/suno")
def api_update_suno_config(update: SunoConfigUpdate):
    patch = {k: v for k, v in update.dict(exclude_none=True).items()}
    save_suno_config_smart(patch)
    return {"status": "ok", "config": get_suno_config()}

# ─── API: ベンチマーク設定（チャンネル横断） ───

@app.get("/api/benchmark/config")
def api_get_benchmark_config():
    return {"config": get_benchmark_config()}

class BenchmarkFilterPatch(BaseModel):
    top_n: Optional[int] = None
    min_subs: Optional[int] = None
    max_subs: Optional[int] = None
    exclude_names: Optional[List[str]] = None

class BenchmarkConfigUpdate(BaseModel):
    pinned_names: Optional[List[str]] = None
    filter: Optional[BenchmarkFilterPatch] = None
    spreadsheet_channel_detail_url: Optional[str] = None
    spreadsheet_growth_tracking_url: Optional[str] = None

@app.put("/api/benchmark/config")
def api_update_benchmark_config(update: BenchmarkConfigUpdate):
    cfg = get_benchmark_config()
    body = update.dict(exclude_none=True)
    if "filter" in body:
        cfg["filter"] = {**cfg.get("filter", {}), **body.pop("filter")}
    cfg.update(body)
    save_benchmark_config(cfg)
    return {"status": "ok", "config": cfg}

# ─── API: マスター設定（プロンプト + 詳細パラメータの一元管理） ───

MASTER_PROMPTS_FILE = CONFIG_DIR / "master_prompts.json"
MASTER_SETTINGS_FILE = CONFIG_DIR / "master_settings.json"

# プロンプトキー一覧と「上書き対象のスクリプト」のメモ
MASTER_PROMPT_KEYS = {
    "title_generation":      "claude_proposer.py の _TITLES_PROMPT を上書き",
    "description_generation": "claude_proposer.py の _DESCRIPTION_PROMPT を上書き",
    "tags_generation":       "claude_proposer.py の _TAGS_PROMPT を上書き",
    "competitor_analysis":   "app_competitor.py analyze_with_claude のシステム指示を上書き",
    "suno_from_analysis":    "app_competitor.py propose_suno_prompt の指示を上書き",
    "flow_from_analysis":    "app_competitor.py propose_flow_prompt の指示を上書き",
    "suno_from_persona":     "app.py /api/suno/suggest-prompt のプロンプトを上書き",
    "imitate_evolve":        "Sprint 5-A 「徹底パクリ進化」分析（imitate / avoid / evolve 3 軸）",
}

DEFAULT_MASTER_SETTINGS = {
    "suno": {
        "retry_count": 2,
        "workspace_pattern": "{channel}_vol{vol}",
        "dl_wait_sec": 30,
    },
    "flow": {
        "default_count": 4,        # 1 バッチあたりの枚数（既存 DEFAULT_COUNT="x4" の上書き）
        "batch_size": 2,           # バッチ数
        "reference_image": "",     # 参照画像のパス
    },
    "meta": {
        "title_count": 5,
        "description_target_chars": 800,
        "tags_target_count": 18,
        "fixed_tags": ["BGM", "Lounge", "Chill", "Jazz", "Instrumental"],
    },
    "remote": {
        "tunnel_url": "",  # Cloudflare Tunnel の URL（手動更新）
    },
}

def get_master_prompts():
    """ユーザーが上書きしたプロンプト群。チャンネル別 → グローバル の順でフォールバック。
    空キーはハードコード（claude_proposer.py 内の既定）にフォールバック。"""
    glob = load_json(MASTER_PROMPTS_FILE, {}) or {}
    cc = load_channel_config()
    cc_prompts = cc.get("master_prompts") or {}
    return {**glob, **cc_prompts}

def get_master_settings():
    cfg = json.loads(json.dumps(DEFAULT_MASTER_SETTINGS))  # deep copy
    loaded = load_json(MASTER_SETTINGS_FILE, {})
    if isinstance(loaded.get("suno"), dict):
        loaded["suno"].pop("diversity_hint", None)
    for section, defaults in DEFAULT_MASTER_SETTINGS.items():
        if isinstance(defaults, dict):
            cfg[section] = {**defaults, **(loaded.get(section) or {})}
    return cfg

def save_master_prompts(data):
    """master_prompts はアクティブチャンネル別に保存。
    アクティブチャンネルが無いときはグローバルへフォールバック。"""
    if _channel_config_path():
        cc = load_channel_config()
        cc["master_prompts"] = data
        save_channel_config(cc)
    else:
        save_json(MASTER_PROMPTS_FILE, data)

def save_master_settings(data):
    save_json(MASTER_SETTINGS_FILE, data)

@app.get("/api/master")
def api_get_master():
    """マスター設定 = 全部統合返却。既存の分離ファイルもそのまま含める。"""
    return {
        "prompts": {
            "values": get_master_prompts(),
            "keys": MASTER_PROMPT_KEYS,
        },
        "settings": get_master_settings(),
        "suno": get_suno_config(),
        "benchmark": get_benchmark_config(),
        "export": get_export_rules(),
        "channels": get_channels(),
        "dashboard": get_dashboard_config(),
        "meta": {"schema_version": CONFIG_SCHEMA_VERSION},
    }

class MasterUpdate(BaseModel):
    section: str  # "prompts" | "settings.suno" | "settings.flow" | "settings.meta" | "suno" | "benchmark" | "export" | "remote"
    patch: dict

@app.put("/api/master")
def api_put_master(update: MasterUpdate):
    section = (update.section or "").strip()
    patch = update.patch or {}
    if section == "prompts":
        cur = get_master_prompts()
        cur.update(patch)
        # 空文字列は削除扱い → ハードコードフォールバック
        cur = {k: v for k, v in cur.items() if v}
        save_master_prompts(cur)
        return {"status": "ok", "section": section, "config": cur}
    if section.startswith("settings."):
        sub = section.split(".", 1)[1]
        cur = get_master_settings()
        if sub not in cur:
            raise HTTPException(400, f"未知の settings サブセクション: {sub}")
        if sub == "suno":
            patch.pop("diversity_hint", None)
        cur[sub] = {**cur[sub], **patch}
        save_master_settings(cur)
        return {"status": "ok", "section": section, "config": cur}
    if section == "settings":
        cur = get_master_settings()
        for k, v in patch.items():
            if isinstance(v, dict) and k in cur:
                cur[k] = {**cur[k], **v}
            else:
                cur[k] = v
        save_master_settings(cur)
        return {"status": "ok", "section": section, "config": cur}
    if section == "suno":
        cfg = get_suno_config(); cfg.update(patch); save_json(SUNO_CONFIG, cfg)
        return {"status": "ok", "section": section, "config": cfg}
    if section == "benchmark":
        cfg = get_benchmark_config()
        if "filter" in patch and isinstance(patch["filter"], dict):
            cfg["filter"] = {**cfg.get("filter", {}), **patch.pop("filter")}
        cfg.update(patch); save_benchmark_config(cfg)
        return {"status": "ok", "section": section, "config": cfg}
    if section == "export":
        cfg = get_export_rules()
        if "rules" in patch and isinstance(patch["rules"], dict):
            cfg["rules"] = {**cfg.get("rules", {}), **patch.pop("rules")}
        cfg.update(patch); save_export_rules(cfg)
        return {"status": "ok", "section": section, "config": cfg}
    raise HTTPException(400, f"未知の section: {section}")

@app.get("/api/master/export")
def api_master_export():
    """全設定の JSON ダンプ。バックアップ・別 PC への移行用。"""
    return {
        "schema_version": CONFIG_SCHEMA_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "dashboard": get_dashboard_config(),
        "suno": get_suno_config(),
        "benchmark": get_benchmark_config(),
        "channels": get_channels(),
        "master_prompts": get_master_prompts(),
        "master_settings": get_master_settings(),
        "export_rules": get_export_rules(),
    }

class MasterImportRequest(BaseModel):
    data: dict
    overwrite: bool = False  # True なら既存ファイルを上書き、False なら patch マージ

@app.post("/api/master/import")
def api_master_import(req: MasterImportRequest):
    d = req.data or {}
    if "schema_version" not in d:
        raise HTTPException(400, "schema_version が無いインポートデータです")
    sections_applied = []
    if "dashboard" in d:
        cfg = d["dashboard"] if req.overwrite else {**get_dashboard_config(), **d["dashboard"]}
        save_dashboard_config_smart(cfg); sections_applied.append("dashboard")
    if "suno" in d:
        cfg = d["suno"] if req.overwrite else {**get_suno_config(), **d["suno"]}
        save_suno_config_smart(cfg); sections_applied.append("suno")
    if "benchmark" in d:
        cfg = d["benchmark"] if req.overwrite else {**get_benchmark_config(), **d["benchmark"]}
        save_benchmark_config(cfg); sections_applied.append("benchmark")
    if "master_prompts" in d:
        save_master_prompts(d["master_prompts"]); sections_applied.append("master_prompts")
    if "master_settings" in d:
        save_master_settings(d["master_settings"]); sections_applied.append("master_settings")
    if "export_rules" in d:
        save_export_rules(d["export_rules"]); sections_applied.append("export_rules")
    return {"status": "ok", "applied": sections_applied}


# ─── API: SUNO プロンプト雛形提案（ペルソナ起点） ───

class SunoSuggestPromptRequest(BaseModel):
    persona: Optional[str] = None
    style_hint: Optional[str] = None

@app.post("/api/suno/suggest-prompt")
def api_suno_suggest_prompt(req: SunoSuggestPromptRequest):
    """ペルソナと任意のスタイルヒントから SUNO 用プロンプト雛形を Claude CLI で生成。
    競合分析を必要としない軽量版。設定タブ内の SUNO セクションから呼び出される。
    """
    persona = (req.persona or get_dashboard_config().get("persona") or "").strip()
    if not persona:
        raise HTTPException(400, "ペルソナが空です。設定タブでペルソナを入力してください")
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    style_hint = (req.style_hint or "").strip()

    prompt = f"""You are crafting a single SUNO music generation prompt for a YouTube BGM channel.

=== Channel Persona ===
{persona[:2000]}

=== Style Hint (optional, may be empty) ===
{style_hint or '(none)'}

=== Your Task ===
Output a SINGLE SUNO prompt (roughly 150-250 characters of English).
- Lead with genre + mood + key instruments that match the persona's atmosphere
- Include BPM/tempo hint only if meaningful (e.g. "slow 70bpm")
- No vocals. Instrumental BGM only.
- Anchor one sensory scene (e.g. "rainy midnight cafe") for concrete atmosphere.
- STRICTLY FORBIDDEN: do NOT reference any real artist, band, composer, producer, label, or song/album names. SUNO rejects copyrighted names.

Respond with a SINGLE JSON object:
{{
  "prompt": "<the SUNO prompt, one line, in English>",
  "rationale": "<one English sentence explaining why this fits the persona>"
}}

Output ONLY the JSON object."""

    try:
        from app_llm_runner import run_llm, LLMError
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="suno-persona")
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:300]}")

    # JSON 抽出
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        raise HTTPException(500, f"JSON 抽出失敗: {out[:300]}")
    try:
        obj = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(500, f"JSON パース失敗: {e}")
    if not obj.get("prompt"):
        raise HTTPException(500, "prompt フィールドが空です")
    return obj

# ─── API: Finder 操作 ───

def _safe_user_path(p):
    """HOME 配下 or /Volumes 配下のみ許可。シンボリックリンク・dotfile は拒否。
    返り値: 解決済み絶対 Path。違反時は HTTPException(403)。"""
    try:
        resolved = Path(p).expanduser().resolve(strict=False)
    except Exception:
        raise HTTPException(400, "不正なパスです")
    if resolved.is_symlink():
        raise HTTPException(403, "シンボリックリンクは許可されていません")
    allowed_roots = [HOME.resolve(), Path("/Volumes").resolve()]
    if not any(str(resolved).startswith(str(root)) for root in allowed_roots):
        raise HTTPException(403, f"許可されていないパスです: {resolved}")
    # ~/Library 以下は CloudStorage だけ許可（.ssh, .config など秘密情報の保護）
    lib = (HOME / "Library").resolve()
    if str(resolved).startswith(str(lib)):
        cloud = (HOME / "Library/CloudStorage").resolve()
        app_sup = (HOME / "Library/Application Support").resolve()
        if not (str(resolved).startswith(str(cloud)) or str(resolved).startswith(str(app_sup))):
            raise HTTPException(403, "Library 配下の機密領域へのアクセスは禁止されています")
    return resolved


class FinderRequest(BaseModel):
    path: str

@app.post("/api/finder/open")
def api_finder_open(req: FinderRequest):
    safe = _safe_user_path(req.path)
    if open_in_finder(str(safe)):
        return {"status": "ok"}
    raise HTTPException(400, f"パスが存在しません: {safe}")


# ─── API: フォルダピッカー（手打ち入力の代替） ───
@app.get("/api/finder/list")
def api_finder_list(path: str = ""):
    """ディレクトリの中身を返す。ピッカー UI 用。
    path が空 or 不正なら HOME を返す。最大 200 件、超過時は truncated:true。
    """
    # ルートリスト（クイックジャンプ用）
    roots = []
    volumes = Path("/Volumes")
    if volumes.exists():
        roots.append({"label": "外部ストレージ", "path": "/Volumes", "icon": "💾"})
    roots.append({"label": "ホーム", "path": str(HOME), "icon": "🏠"})
    cloud = HOME / "Library" / "CloudStorage"
    if cloud.exists():
        roots.append({"label": "クラウド", "path": str(cloud), "icon": "☁"})
    # パス解決（空なら HOME）
    target_str = (path or "").strip() or str(HOME)
    try:
        target = _safe_user_path(target_str)
    except HTTPException:
        # 危険パスならホームに戻す
        target = HOME
    if not target.exists() or not target.is_dir():
        target = HOME
    # エントリー列挙
    entries = []
    truncated = False
    try:
        for e in sorted(target.iterdir(), key=lambda p: p.name.lower()):
            if e.name.startswith("."):
                continue
            try:
                if not e.is_dir():
                    continue
            except OSError:
                continue
            if len(entries) >= 200:
                truncated = True
                break
            child_count = 0
            try:
                child_count = sum(1 for _ in e.iterdir() if not _.name.startswith("."))
            except (PermissionError, OSError):
                pass
            entries.append({
                "name": e.name,
                "path": str(e),
                "child_count": child_count,
            })
    except PermissionError:
        raise HTTPException(403, f"このフォルダは読み取り権限がありません: {target}")
    parent = str(target.parent) if target.parent != target else None
    return {
        "path": str(target),
        "parent": parent,
        "entries": entries,
        "roots": roots,
        "truncated": truncated,
    }

class FolderCreateRequest(BaseModel):
    path: str
    open_after: bool = True

@app.post("/api/finder/create-folder")
def api_finder_create_folder(req: FolderCreateRequest):
    p = _safe_user_path(req.path)
    p.mkdir(parents=True, exist_ok=True)
    if req.open_after:
        open_in_finder(p)
    return {"status": "ok", "path": str(p), "exists": p.exists()}

class FolderDeleteRequest(BaseModel):
    path: str
    confirm_name: str  # 安全のためフォルダ名を再入力

@app.post("/api/finder/delete-folder")
def api_finder_delete_folder(req: FolderDeleteRequest):
    p = _safe_user_path(req.path)
    if not p.exists():
        raise HTTPException(404, "フォルダが存在しません")
    if p.name != req.confirm_name:
        raise HTTPException(400, "フォルダ名が一致しません（安全確認）")
    children = list(p.iterdir()) if p.is_dir() else []
    if len(children) > 0:
        raise HTTPException(400, f"フォルダが空ではありません（{len(children)}件）。先に中身を削除してください。")
    p.rmdir()
    return {"status": "ok"}

@app.get("/api/finder/browse")
def api_finder_browse(path: str = ""):
    """指定パスの子フォルダ一覧を返す"""
    if not path:
        candidates = list((HOME / "Library/CloudStorage").glob("GoogleDrive-*/共有ドライブ"))
        path = str(candidates[0]) if candidates else str(HOME)
    p = _safe_user_path(path)
    if not p.exists():
        return {"path": str(p), "entries": [], "error": "パスが存在しません"}
    entries = []
    try:
        for child in sorted(p.iterdir()):
            if child.name.startswith('.'):
                continue
            entries.append({
                "name": child.name,
                "path": str(child),
                "is_dir": child.is_dir(),
            })
    except PermissionError:
        return {"path": str(p), "entries": [], "error": "アクセス権限がありません"}
    return {"path": str(p), "parent": str(p.parent), "entries": entries}

# ─── API: SUNO ───
class SunoRunRequest(BaseModel):
    prompt: Optional[str] = None
    count: Optional[int] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    interval: Optional[int] = None   # ループ間隔（秒）
    workspace: Optional[str] = None  # 直接指定
    video_name: Optional[str] = None # 指定時は {channel}_vol{N} を自動生成
    batch: Optional[bool] = False    # Claude CLI 一括生成モード
    generation_mode: Optional[str] = None  # styles_title_only / lyrics_styles / lyrics
    headless: Optional[bool] = None
    # フロント「⚡ 生成+DL+整理」用（app.py は直接消費しないが forward 互換のため受理）
    dl_wait_sec: Optional[int] = None
    dl_min_duration: Optional[int] = None
    auto_download: Optional[bool] = None
    # 多様性制御（チャンネル別に上書き可能。未指定なら suno_auto_create 側の既定/設定を使用）
    diversity_threshold: Optional[float] = None
    diversity_retry: Optional[int] = None
    history_limit: Optional[int] = None

@app.post("/api/suno/start")
async def api_suno_start(req: SunoRunRequest):
    await _ensure_not_running("suno", "SUNO は既に実行中です")
    # ─── プロンプト確認ロジック ───
    # 暗黙のフォールバック（チャンネル設定 / prompts_library の暗黙選択 / 既定文字列）
    # でジャンル違いの曲が作られる事故を防ぐため、prompt は呼び出し側で必ず明示する。
    # vol.6→vol.8 で実際に「ジャズハウス」プロンプトが勝手に選ばれて作り直しになった。
    prompt = (req.prompt or "").strip()
    if not prompt:
        raise HTTPException(
            400,
            "prompt が空です。SUNO 起動前に必ずプロンプト本文を明示してください "
            "（暗黙のチャンネル設定フォールバックは廃止しました）。"
        )
    if len(prompt) < 10:
        raise HTTPException(
            400,
            f"prompt が短すぎます（{len(prompt)} chars）。"
            "意図したプロンプトか確認のうえ、フルテキストを送信してください。"
        )
    cmd = [sys.executable, "-u", str(SUNO_SCRIPT)]  # -u: unbuffered
    cmd += ["--prompt", prompt]
    if req.count: cmd += ["--count", str(req.count)]
    if req.interval: cmd += ["--interval", str(req.interval)]
    if req.provider: cmd += ["--provider", req.provider]
    if req.model: cmd += ["--model", req.model]

    # Workspace 名の解決
    workspace = req.workspace
    if not workspace and req.video_name:
        config = get_dashboard_config()
        channel_name = (config.get("channel_name") or "orzz").strip()
        # チャンネル名を英数字+アンダースコアに正規化（SUNO の制約に優しく）
        channel_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", channel_name).strip("_") or "orzz"
        m = re.match(r"^(\d+)_", req.video_name)
        vol = m.group(1) if m else ""
        workspace = f"{channel_slug}_vol{vol}" if vol else channel_slug
    if workspace:
        cmd += ["--workspace", workspace]
    if req.batch:
        cmd += ["--batch"]
    if req.generation_mode:
        cmd += ["--mode", req.generation_mode]
    if req.headless:
        cmd += ["--headless"]
    # 多様性制御（チャンネル別。フォームで指定があれば CLI に渡す）
    if req.diversity_threshold is not None:
        cmd += ["--diversity-threshold", str(req.diversity_threshold)]
    if req.diversity_retry is not None:
        cmd += ["--diversity-retry", str(req.diversity_retry)]
    if req.history_limit is not None:
        cmd += ["--history-limit", str(req.history_limit)]
    # 起動時ログ先頭に prompt 全文を出して、後追いでも何で作ったか確認できるようにする
    task_logs["suno"] = [
        "─" * 60,
        f"▶ SUNO 起動 prompt 確認（length={len(prompt)} chars）:",
        prompt,
        "─" * 60,
    ]
    import datetime as _dt
    task_meta["suno"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "workspace": workspace or "",
        "count": req.count,
        "interval": req.interval,
        "video_name": req.video_name or "",
        "prompt": prompt,  # 採用 prompt を meta に保存（後追い監査用）
        "prompt_length": len(prompt),
    }
    # Cloudflare Bot 判定対策: 既定で APP_KEEP_BROWSER=1 を立てる（vol.7 で実証、vol.6 で再発）
    # 明示的に "0" が立っていればユーザー意図を尊重して上書きしない
    suno_env = {**os.environ}
    if suno_env.get("APP_KEEP_BROWSER", "").strip() not in ("0", "false", "no"):
        suno_env["APP_KEEP_BROWSER"] = "1"
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=suno_env)
    active_tasks["suno"] = proc
    _stream_subprocess(proc, "suno")
    return {"status": "started"}

class SunoDownloadRequest(BaseModel):
    workspace: Optional[str] = None
    video_name: Optional[str] = None   # 指定時は {channel}_vol{N} を自動計算 + フォルダの original_music へ保存
    target_dir: Optional[str] = None   # 明示保存先（指定されれば優先）

@app.post("/api/suno/download")
async def api_suno_download(req: SunoDownloadRequest):
    await _ensure_not_running("suno", "SUNO 生成が実行中のためダウンロードできません")

    # workspace 名の解決
    workspace = req.workspace
    video_folder = None
    if req.video_name:
        config = get_dashboard_config()
        video_folder = Path(config["channel_folder"]) / req.video_name
        if not workspace:
            channel_name = (config.get("channel_name") or "orzz").strip()
            channel_slug = re.sub(r"[^A-Za-z0-9_-]+", "_", channel_name).strip("_") or "orzz"
            m = re.match(r"^(\d+)_", req.video_name)
            vol = m.group(1) if m else ""
            workspace = f"{channel_slug}_vol{vol}" if vol else channel_slug
    if not workspace:
        raise HTTPException(400, "workspace または video_name が必要です")

    # 保存先: target_dir > 動画フォルダ直下 > ~/Downloads/suno_<workspace>
    if req.target_dir:
        target_dir = req.target_dir
    elif video_folder:
        target_dir = str(video_folder)  # フォルダ直下に展開
    else:
        target_dir = str(Path.home() / "Downloads" / f"suno_{workspace}")

    cmd = [sys.executable, "-u", str(SUNO_SCRIPT),
           "--download-workspace", workspace,
           "--download-dir", target_dir]
    task_logs["suno"] = []
    import datetime as _dt
    task_meta["suno"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "workspace": workspace,
        "mode": "download",
        "target_dir": target_dir,
        "video_name": req.video_name or "",
    }
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["suno"] = proc
    _stream_subprocess(proc, "suno")
    return {"status": "started", "workspace": workspace, "target_dir": target_dir}

@app.post("/api/suno/stop")
def api_suno_stop():
    proc = active_tasks.get("suno")
    if proc and proc.returncode is None:
        proc.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}

def _parse_suno_progress(logs):
    """SUNOログから進捗を抽出: {current, total, next_wait_sec, next_remaining_sec, phase}"""
    progress = {"current": 0, "total": 0, "next_wait_sec": 0,
                "next_remaining_sec": 0, "phase": ""}
    for line in logs:
        # 曲 3/5
        m = re.search(r"曲\s+(\d+)/(\d+)", line)
        if m:
            progress["current"] = int(m.group(1))
            progress["total"] = int(m.group(2))
            progress["phase"] = "generating"
        # 次の生成まで 180 秒
        m = re.search(r"次の生成まで\s+(\d+)\s+秒", line)
        if m:
            progress["next_wait_sec"] = int(m.group(1))
            progress["phase"] = "waiting"
        # 残り 150 秒
        m = re.search(r"残り\s+(\d+)\s+秒", line)
        if m:
            progress["next_remaining_sec"] = int(m.group(1))
        # LLM呼び出し / Claude CLI 呼び出し
        if "LLM呼び出し中" in line or "Claude CLI 呼び出し中" in line:
            progress["phase"] = "llm"
        if "Create ボタンをクリック" in line:
            progress["phase"] = "submitted"
        if "[完了]" in line:
            progress["phase"] = "done"
    return progress


@app.get("/api/suno/status")
def api_suno_status():
    proc = active_tasks.get("suno")
    running = proc is not None and proc.returncode is None
    logs = task_logs.get("suno", [])
    progress = _parse_suno_progress(logs)
    meta = task_meta.get("suno", {})
    return {
        "running": running,
        "logs": logs[-50:],
        "progress": progress,
        "started_at": meta.get("started_at"),
        "meta": meta,
    }

# ─── API: チャンネル ───

# YouTube アイコン取得 TTL（秒）
ICON_CACHE_TTL_SEC = 24 * 60 * 60

def _resolve_youtube_channel_meta(url_or_id: str) -> dict:
    """YouTube URL/handle/channelId から {channel_id, name, handle, icon_url, subscribers, url} を返す。
    YouTube Data API v3 の channels.list を使用。
    """
    cfg = get_dashboard_config()
    api_key = (cfg.get("youtube_api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "YouTube Data API Key が未設定です（設定タブで入力してください）")
    import urllib.request, urllib.parse
    s = (url_or_id or "").strip()
    if not s:
        raise HTTPException(400, "URL が空です")

    cid = None
    handle = ""
    m = re.search(r"/channel/(UC[A-Za-z0-9_-]+)", s)
    if m:
        cid = m.group(1)
    elif s.startswith("UC") and len(s) >= 20:
        cid = s
    else:
        h = re.search(r"@([\w\-.]+)", s)
        if h:
            handle = "@" + h.group(1)
            qs = urllib.parse.urlencode({"part": "id", "forHandle": handle, "key": api_key})
            try:
                with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/channels?{qs}", timeout=15) as r:
                    d = json.loads(r.read())
                if d.get("items"):
                    cid = d["items"][0]["id"]
            except Exception as e:
                raise HTTPException(502, f"YouTube API エラー (handle): {e}")
    if not cid:
        raise HTTPException(400, f"channelId を解決できません: {s}")

    qs = urllib.parse.urlencode({"part": "snippet,statistics", "id": cid, "key": api_key})
    try:
        with urllib.request.urlopen(f"https://www.googleapis.com/youtube/v3/channels?{qs}", timeout=15) as r:
            d = json.loads(r.read())
    except Exception as e:
        raise HTTPException(502, f"YouTube API エラー: {e}")
    items = d.get("items") or []
    if not items:
        raise HTTPException(404, f"チャンネルが見つかりません: {cid}")
    sn = items[0].get("snippet", {})
    stat = items[0].get("statistics", {})
    thumbs = sn.get("thumbnails", {})
    icon_url = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}).get("url", "")
    return {
        "channel_id": cid,
        "name": sn.get("title", ""),
        "handle": sn.get("customUrl", "") or handle,
        "icon_url": icon_url,
        "subscribers": int(stat.get("subscriberCount", 0)) if stat.get("subscriberCount") else 0,
        "url": f"https://www.youtube.com/channel/{cid}",
    }


def _refresh_channel_icon_if_stale(ch: dict) -> dict:
    """channels.json の 1 要素について、icon_cache が空 or 24h 超なら refresh.
    YouTube Data API Key が未設定でも、YouTube ページからアイコンだけスクレイプして補完する。"""
    cache = ch.get("icon_cache") or {}
    fetched_at = cache.get("fetched_at") or ""
    is_stale = True
    if fetched_at:
        try:
            dt = datetime.fromisoformat(fetched_at.replace("Z", "+00:00"))
            age = (datetime.now(dt.tzinfo) - dt).total_seconds()
            is_stale = age > ICON_CACHE_TTL_SEC
        except Exception:
            is_stale = True
    if not is_stale and cache.get("url"):
        return ch
    yt_url = ch.get("youtube_url") or ch.get("youtube_channel_id") or ""
    if not yt_url:
        return ch
    try:
        meta = _resolve_youtube_channel_meta(yt_url)
        ch["icon_cache"] = {"url": meta.get("icon_url", ""), "fetched_at": datetime.utcnow().isoformat() + "Z"}
        ch["youtube_channel_id"] = meta.get("channel_id") or ch.get("youtube_channel_id", "")
        if not ch.get("handle"):
            ch["handle"] = meta.get("handle", "")
    except Exception as e:
        # API キー未設定や API 障害時は YouTube ページのスクレイプで補完
        try:
            scraped = resolve_channel_icon(yt_url)
        except Exception:
            scraped = ""
        if scraped:
            ch["icon_cache"] = {"url": scraped, "fetched_at": datetime.utcnow().isoformat() + "Z"}
        else:
            ch.setdefault("icon_cache", {})["error"] = str(e)[:120]
    return ch


def get_channels():
    chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    # 各チャンネルに icon_url 等の派生フィールドを補う（保存はしない＝読み取りのみ）
    for ch in chs:
        cache = ch.get("icon_cache") or {}
        ch["icon_url"] = cache.get("url", "") or ""
        ch.setdefault("youtube_url", "")
        ch.setdefault("youtube_channel_id", "")
        ch.setdefault("handle", "")
        if not ch.get("prefix"):
            ch["prefix"] = infer_file_prefix_from_folder(Path(ch.get("folder") or "")) or ""
    return chs

@app.get("/api/channels")
def api_list_channels():
    """各チャンネルの icon_cache が古ければ refresh し、icon_url を含めて返す。"""
    chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    changed = False
    for ch in chs:
        if ch.get("youtube_url") or ch.get("youtube_channel_id"):
            before = (ch.get("icon_cache") or {}).get("url", "")
            _refresh_channel_icon_if_stale(ch)
            if (ch.get("icon_cache") or {}).get("url", "") != before:
                changed = True
    if changed:
        save_json(CHANNELS_CONFIG, chs)
    # 返却用に派生フィールドを追加
    for ch in chs:
        cache = ch.get("icon_cache") or {}
        ch["icon_url"] = cache.get("url", "")
        ch.setdefault("youtube_url", "")
        ch.setdefault("youtube_channel_id", "")
        ch.setdefault("handle", "")
        if not ch.get("prefix"):
            ch["prefix"] = infer_file_prefix_from_folder(Path(ch.get("folder") or "")) or ""
    return {"channels": chs}

class ChannelResolveRequest(BaseModel):
    url: str

@app.post("/api/channels/resolve-url")
def api_channels_resolve_url(req: ChannelResolveRequest):
    """YouTube チャンネル URL or handle or channelId からメタ情報を返す（保存しない）"""
    return _resolve_youtube_channel_meta(req.url)

class ChannelCreate(BaseModel):
    name: str
    folder: str
    prefix: str = ""
    template_prproj: str = ""
    template_psd: str = ""
    create_folder: bool = True
    open_in_finder: bool = True
    youtube_url: str = ""


def _create_empty_channel_config(folder: Path, req: ChannelCreate) -> None:
    """新規チャンネルに旧グローバル設定が流れ込まないよう初期 config を置く。"""
    if not folder.exists():
        return
    p = folder / _CHANNEL_CONFIG_FILENAME
    if p.exists():
        return
    seed = {
        "persona": "",
        "rival_channels": [],
        "reference_image_dir": "",
        "reference_image": "",
        "benchmark_pinned_names": [],
        "benchmark_filter": DEFAULT_BENCHMARK_CONFIG["filter"],
        "spreadsheet_channel_detail_url": "",
        "spreadsheet_growth_tracking_url": "",
        "template_prproj": req.template_prproj or "",
        "template_psd": req.template_psd or "",
        "_schema_version": 1,
        "_created_at": datetime.utcnow().isoformat() + "Z",
        "_created_for_channel": req.name or "",
    }
    p.write_text(json.dumps(seed, indent=2, ensure_ascii=False), encoding="utf-8")

@app.get("/api/channels/suggest-folder")
def api_suggest_channel_folder(name: str = ""):
    """YTフォルダ配下に番号付きチャンネルフォルダ名を提案"""
    # 既存の YT フォルダを探す
    yt_candidates = list((HOME / "Library/CloudStorage").glob("GoogleDrive-*/共有ドライブ/YT"))
    if not yt_candidates:
        return {"suggestion": "", "yt_root": ""}
    yt_root = yt_candidates[0]
    # 最大番号を取得
    max_num = 0
    for d in yt_root.iterdir():
        if d.is_dir():
            m = re.match(r'^(\d+)\.', d.name)
            if m: max_num = max(max_num, int(m.group(1)))
    next_num = max_num + 1
    channel_dir_name = f"{next_num}.【{name or 'NewChannel'}】"
    return {"suggestion": str(yt_root / channel_dir_name), "yt_root": str(yt_root)}

@app.post("/api/channels")
def api_create_channel(req: ChannelCreate):
    channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    channel_id = re.sub(r'[^a-zA-Z0-9]', '_', req.name.lower()).strip('_') or f"ch{len(channels)}"
    folder = Path(req.folder)
    if req.create_folder:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "プロジェクト").mkdir(exist_ok=True)
        (folder / "素材").mkdir(exist_ok=True)
    _create_empty_channel_config(folder, req)
    inferred_prefix = infer_file_prefix_from_folder(folder)
    prefix = sanitize_file_prefix(req.prefix or inferred_prefix or req.name, fallback="vol")
    new_channel = {
        "id": channel_id, "name": req.name, "folder": str(folder),
        "prefix": prefix, "template_prproj": req.template_prproj, "template_psd": req.template_psd,
        "youtube_url": (req.youtube_url or "").strip(),
        "youtube_channel_id": "",
        "handle": "",
        "icon_cache": {},
    }
    # YouTube URL 指定があればアイコンと channel_id を取得
    if new_channel["youtube_url"]:
        try:
            meta = _resolve_youtube_channel_meta(new_channel["youtube_url"])
            new_channel["youtube_channel_id"] = meta.get("channel_id", "")
            new_channel["handle"] = meta.get("handle", "")
            new_channel["icon_cache"] = {"url": meta.get("icon_url", ""), "fetched_at": datetime.utcnow().isoformat() + "Z"}
            # 名前未指定の場合は YouTube タイトルで補完
            if not req.name.strip() and meta.get("name"):
                new_channel["name"] = meta["name"]
                new_channel["id"] = re.sub(r'[^a-zA-Z0-9]', '_', meta["name"].lower()).strip('_') or new_channel["id"]
        except HTTPException as e:
            # URL 解決に失敗してもチャンネル登録は続ける（後で再試行可能）
            new_channel["icon_cache"] = {"error": e.detail}
    channels.append(new_channel)
    save_json(CHANNELS_CONFIG, channels)
    if req.open_in_finder and folder.exists():
        open_in_finder(folder)
    return {"status": "ok", "channel": new_channel}

@app.delete("/api/channels/{channel_id}")
def api_delete_channel(channel_id: str):
    channels = [c for c in get_channels() if c["id"] != channel_id]
    save_json(CHANNELS_CONFIG, channels)
    return {"status": "ok"}


class ChannelUpdate(BaseModel):
    name: Optional[str] = None
    youtube_url: Optional[str] = None
    prefix: Optional[str] = None


@app.put("/api/channels/{channel_id}")
def api_update_channel(channel_id: str, req: ChannelUpdate):
    """既存チャンネルの YouTube URL / 名前 / prefix を更新。
    youtube_url が変わった場合はメタ情報（channel_id / handle / icon）も再取得する。"""
    channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    ch = next((c for c in channels if c.get("id") == channel_id), None)
    if not ch:
        raise HTTPException(404, "チャンネルが見つかりません")
    if req.name is not None and req.name.strip():
        ch["name"] = req.name.strip()
    if req.prefix is not None:
        new_prefix = sanitize_file_prefix(req.prefix, fallback=ch.get("prefix") or "vol")
        if new_prefix:
            ch["prefix"] = new_prefix
    if req.youtube_url is not None:
        url = req.youtube_url.strip()
        ch["youtube_url"] = url
        if url:
            try:
                meta = _resolve_youtube_channel_meta(url)
                ch["youtube_channel_id"] = meta.get("channel_id", "")
                ch["handle"] = meta.get("handle", "")
                icon_url = meta.get("icon_url", "")
                ch["icon_cache"] = {
                    "url": icon_url,
                    "fetched_at": datetime.utcnow().isoformat() + "Z",
                }
                # 名前が未設定の場合のみ YouTube タイトルで補完
                if (not ch.get("name") or ch.get("name") == "New Channel") and meta.get("name"):
                    ch["name"] = meta["name"]
            except HTTPException as e:
                ch["icon_cache"] = {"error": e.detail}
        else:
            # URL クリア時はメタ情報もクリア
            ch["youtube_channel_id"] = ""
            ch["handle"] = ""
            ch["icon_cache"] = {}
    save_json(CHANNELS_CONFIG, channels)
    return {"status": "ok", "channel": ch}

@app.put("/api/channels/active/{channel_id}")
def api_set_active_channel(channel_id: str):
    channels = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    ch = next((c for c in channels if c["id"] == channel_id), None)
    if not ch: raise HTTPException(404, "チャンネルが見つかりません")
    config = get_dashboard_config()
    folder = Path(ch["folder"])
    prefix = sanitize_file_prefix(ch.get("prefix") or infer_file_prefix_from_folder(folder) or config.get("file_prefix"), fallback="vol")
    if ch.get("prefix") != prefix:
        ch["prefix"] = prefix
        save_json(CHANNELS_CONFIG, channels)
    # チャンネル切替: グローバル設定のみ更新（per-channel キーを書き戻さない）
    raw = load_json(DASHBOARD_CONFIG, {})
    raw["channel_name"] = ch["name"]
    raw["channel_folder"] = ch["folder"]
    raw["file_prefix"] = prefix
    # per-channel キーが旧グローバルに残っていれば剥がす（マイグレーション後の片付け）
    for k in PER_CHANNEL_KEYS:
        raw.pop(k, None)
    save_json(DASHBOARD_CONFIG, raw)
    return {"status": "ok", "channel": ch}

# ─── API: 動画フォルダ ───

class VideoFolderCreate(BaseModel):
    publish_date: str
    open_in_finder: bool = True

def _resolve_template_candidates(channel_dir: Path, kind: str) -> List[Path]:
    """テンプレ検索ディレクトリを優先順に返す。
    kind: 'prproj' | 'psd'
    優先順: <channel_folder>/プロジェクト/ → <channel_folder>/Project/ → <channel_folder> 直下
    """
    candidates = []
    for sub in ("プロジェクト", "Project", "templates", "Templates", ""):
        d = channel_dir / sub if sub else channel_dir
        if d.exists() and d.is_dir():
            candidates.append(d)
    return candidates


def _find_template_file(channel_dir: Path, filename: str) -> Optional[Path]:
    """指定ファイル名のテンプレを検索ディレクトリから見つけて返す。見つからなければ None。"""
    if not filename:
        return None
    # 絶対パスで指定された場合はそのまま
    p = Path(filename)
    if p.is_absolute() and p.exists():
        return p
    for d in _resolve_template_candidates(channel_dir, ""):
        cand = d / filename
        if cand.exists():
            return cand
    return None


class PickFileRequest(BaseModel):
    extensions: List[str] = []          # ["prproj", "psd"] など。空なら任意ファイル
    initial: Optional[str] = None       # 初期表示ディレクトリ（POSIX path）
    prompt: Optional[str] = "ファイルを選択"


@app.post("/api/finder/pick-file")
def api_finder_pick_file(req: PickFileRequest):
    """macOS の choose file ダイアログを osascript で開いて選ばれたファイルの POSIX パスを返す。
    UI 側からテンプレ .prproj / .psd 選択に使う。"""
    if platform.system() != "Darwin":
        raise HTTPException(400, "macOS でのみ利用可能です")
    exts = [e.strip().lstrip(".") for e in (req.extensions or []) if e and e.strip()]
    type_clause = ""
    if exts:
        # AppleScript の of type 句は UTI / 拡張子の OR リスト
        type_clause = " of type {" + ", ".join([f'"{e}"' for e in exts]) + "}"
    initial_clause = ""
    if req.initial:
        ip = Path(req.initial).expanduser()
        if ip.exists():
            posix = str(ip).replace('"', '\\"')
            initial_clause = f' default location POSIX file "{posix}"'
    prompt = (req.prompt or "ファイルを選択").replace('"', '\\"')
    script = (
        f'POSIX path of (choose file with prompt "{prompt}"'
        f"{type_clause}{initial_clause})"
    )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "ダイアログがタイムアウトしました")
    if r.returncode != 0:
        # ユーザーがキャンセルした場合 osascript は -128 で終了
        msg = (r.stderr or "").strip()
        if "User canceled" in msg or "-128" in msg:
            return {"status": "canceled", "path": ""}
        raise HTTPException(500, f"ファイル選択失敗: {msg[:200]}")
    path = (r.stdout or "").strip()
    return {"status": "ok", "path": path, "filename": Path(path).name if path else ""}


@app.get("/api/templates/list")
def api_list_templates():
    """チャンネルフォルダ配下の Premiere / Photoshop テンプレ候補を返す。
    UI 側でセレクトボックスに表示する用。"""
    config = get_dashboard_config()
    channel_dir_str = config.get("channel_folder") or ""
    if not channel_dir_str:
        return {"channel_folder": "", "prproj": [], "psd": [], "search_dirs": []}
    channel_dir = Path(channel_dir_str)
    if not channel_dir.exists():
        return {"channel_folder": str(channel_dir), "prproj": [], "psd": [],
                "search_dirs": [], "warning": "チャンネルフォルダが存在しません"}
    search_dirs = _resolve_template_candidates(channel_dir, "")
    prproj_set: dict[str, str] = {}
    psd_set: dict[str, str] = {}
    for d in search_dirs:
        for p in d.glob("*.prproj"):
            if p.name not in prproj_set:
                prproj_set[p.name] = str(p)
        for p in d.glob("*.psd"):
            if p.name not in psd_set:
                psd_set[p.name] = str(p)
    return {
        "channel_folder": str(channel_dir),
        "search_dirs": [str(d) for d in search_dirs],
        "prproj": [{"filename": k, "path": v} for k, v in sorted(prproj_set.items())],
        "psd": [{"filename": k, "path": v} for k, v in sorted(psd_set.items())],
        "current_prproj": config.get("template_prproj", ""),
        "current_psd": config.get("template_psd", ""),
    }


@app.post("/api/videos/create")
def api_create_video_folder(req: VideoFolderCreate):
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    if not channel_dir.exists():
        raise HTTPException(400, "チャンネルフォルダが存在しません")
    max_num = 0
    for d in channel_dir.iterdir():
        if d.is_dir():
            info = parse_video_folder_name(d.name)
            if info:
                max_num = max(max_num, info["num"])
    next_num = max_num + 1
    try:
        dt = datetime.strptime(req.publish_date, "%Y-%m-%d")
        date_str = dt.strftime("%y%m%d")
    except ValueError:
        date_str = req.publish_date.replace("-", "")
    prefix = get_file_prefix()
    new_dir = channel_dir / f"{next_num}_{prefix}_{date_str}"
    new_dir.mkdir(exist_ok=True)
    vol_num = f"{next_num:02d}"
    # テンプレートコピー（未配置時は warnings に積んで UI に通知）
    tpl_prproj = config.get("template_prproj") or ""
    tpl_psd = config.get("template_psd") or ""
    warnings: List[str] = []
    created: List[str] = []

    if tpl_prproj:
        prproj_src = _find_template_file(channel_dir, tpl_prproj)
        if prproj_src:
            prproj_dst = new_dir / f"{prefix}_vol{vol_num}.prproj"
            try:
                shutil.copy2(str(prproj_src), str(prproj_dst))
                try:
                    with gzip.open(str(prproj_dst), 'rb') as f: xml = f.read()
                    # テンプレ内に残っている可能性のある旧シーケンス名 (BGMシーケンス /
                    # {prefix}_vol01 / {prefix}_vol02 等) を全部 {prefix}_vol{N} に置換。
                    # 旧コードは 'BGMシーケンス' しか置換せず、テンプレが {prefix}_vol01
                    # と命名されていた場合に置換が効かず、JSX が新シーケンスを見つけられず
                    # 「クリップが見つかりません」エラーになっていた。
                    target_name = f"{prefix}_vol{vol_num}".encode()
                    for old in (b"BGM\xe3\x82\xb7\xe3\x83\xbc\xe3\x82\xb1\xe3\x83\xb3\xe3\x82\xb9",
                                f"{prefix}_vol01".encode(),
                                f"{prefix}_vol02".encode()):
                        if old != target_name:
                            xml = xml.replace(old, target_name)
                    with gzip.open(str(prproj_dst), 'wb') as f: f.write(xml)
                except Exception:
                    pass
                created.append(prproj_dst.name)
            except Exception as e:
                warnings.append(f".prproj コピー失敗: {e}")
        else:
            warnings.append(f"Premiere テンプレが見つかりません: {tpl_prproj}（基本設定 → チャンネル → テンプレートで指定してください）")
    else:
        warnings.append("Premiere テンプレ未設定（基本設定 → チャンネル → テンプレートで指定してください）")

    if tpl_psd:
        psd_src = _find_template_file(channel_dir, tpl_psd)
        if psd_src:
            psd_dst = new_dir / f"{prefix}_vol{vol_num}.psd"
            try:
                shutil.copy2(str(psd_src), str(psd_dst))
                created.append(psd_dst.name)
            except Exception as e:
                warnings.append(f".psd コピー失敗: {e}")
        else:
            warnings.append(f"Photoshop テンプレが見つかりません: {tpl_psd}（基本設定 → チャンネル → テンプレートで指定してください）")
    else:
        warnings.append("Photoshop テンプレ未設定（基本設定 → チャンネル → テンプレートで指定してください）")

    if req.open_in_finder:
        open_in_finder(new_dir)
    return {
        "status": "ok",
        "folder": new_dir.name,
        "num": next_num,
        "path": str(new_dir),
        "created": created,
        "warnings": warnings,
    }

@app.get("/api/videos")
def api_list_videos():
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    if not channel_dir.exists():
        return {"videos": [], "error": "チャンネルフォルダが見つかりません"}
    videos = []
    # フォルダ名先頭の数値で降順ソート（文字列ソートだと "7" > "71" になる問題を回避）
    def _vol_key(p):
        info = parse_video_folder_name(p.name)
        return info["num"] if info else -1
    entries = [d for d in channel_dir.iterdir() if d.is_dir() and parse_video_folder_name(d.name)]
    entries.sort(key=_vol_key, reverse=True)
    for d in entries:
        info = parse_video_folder_name(d.name)
        if not info:
            continue
        num = info["num_text"]
        has_music = (d / "music").is_dir() and any(_is_usable_audio_file(p) for p in (d / "music").iterdir())
        has_srt = any(d.glob("subtitles_*.srt"))
        has_mp4 = _find_exported_mp4(d, d.name) is not None
        has_desc = (d / "youtube_description.txt").exists()
        has_thumb = any(d.glob("vol*.jpg")) or (d / "サムネイル.jpg").exists()
        has_upload = (d / "youtube_upload.json").exists()
        has_tags = (d / "youtube_tags.txt").exists()
        has_images_selected = (d / "selected_images.json").exists()
        # 背景画像（パイプライン STEP 3 / step_bgimage）の完了マーカー: vol{num}.png または vol{num}.jpg
        has_bg_image = (d / f"vol{num}.png").exists() or (d / f"vol{num}.jpg").exists()
        is_hidden = (d / ".hidden").exists()
        title_file = d / "youtube_title.txt"
        title = title_file.read_text(encoding="utf-8").strip() if title_file.exists() else ""
        concept_file = d / "concept.txt"
        concept = concept_file.read_text(encoding="utf-8").strip() if concept_file.exists() else ""
        publish_date = info["publish_date"]
        videos.append({
            "num": num, "name": d.name, "path": str(d),
            "prefix": info["prefix"],
            "title": title,
            "concept": concept,
            "has_music": has_music, "has_srt": has_srt, "has_mp4": has_mp4,
            "has_desc": has_desc, "has_thumb": has_thumb,
            "has_upload": has_upload, "has_tags": has_tags,
            "has_images_selected": has_images_selected,
            "has_bg_image": has_bg_image,
            "is_hidden": is_hidden,
            "publish_date": publish_date,
        })
    return {"videos": videos[:100]}


# ─── API: 全チャンネル × 全 vol の稼働ステータス（P1-7 一元ダッシュボード） ───
# UI は #p-dashboard 上部にこの集約ビューを描画する。**台帳ではなく集約**：
# 状態は依然として folder artifact から推論する（中央 ledger は P3）。

_PIPELINE_STAGES = ["plan", "suno", "rename", "premiere", "export", "meta", "upload"]

def _infer_stage_from_artifacts(folder: Path) -> dict:
    """vol フォルダのファイル群から、各 stage の done/current を推定。"""
    has_plan = (folder / "plan.json").exists()
    has_music = (folder / "music").is_dir() and any(_is_usable_audio_file(p) for p in (folder / "music").iterdir())
    has_audio_processed = (folder / "audio").is_dir() and any(_is_usable_audio_file(p) for p in (folder / "audio").iterdir())
    has_timecode = any(folder.glob("timecode*.txt"))
    has_srt = any(folder.glob("subtitles_*.srt"))
    has_mp4 = _find_exported_mp4(folder, folder.name) is not None
    has_title = (folder / "youtube_title.txt").exists()
    has_desc = (folder / "youtube_description.txt").exists()
    has_tags = (folder / "youtube_tags.txt").exists()
    has_upload = (folder / "youtube_upload.json").exists()
    done_map = {
        "plan":     has_plan,
        "suno":     has_music,
        "rename":   has_audio_processed or has_music,  # rename は music 内で完了する場合あり
        "premiere": has_timecode or has_srt,
        "export":   has_mp4,
        "meta":     has_title and has_desc and has_tags,
        "upload":   has_upload,
    }
    completed = [s for s in _PIPELINE_STAGES if done_map.get(s)]
    # 「現在 stage」= 最初の未完了 stage（plan は optional なのでスキップ）
    current = ""
    for s in _PIPELINE_STAGES:
        if s == "plan":
            continue  # plan は from-benchmark 起動時のみ
        if not done_map.get(s):
            current = s
            break
    return {
        "completed": completed,
        "current": current,
        "is_done": current == "",
    }


@app.get("/api/runs/active")
def api_runs_active():
    """全チャンネル × 直近 vol の稼働ステータスを集約。

    返却:
      channels[].channel_id / name / folder
      channels[].vols[]: {vol, name, completed[], current, is_done, publish_date,
                          is_uploaded, video_id, concept}
      channels[].quota: {used, cap, remaining}（quota.json があれば）
      history_recent[]: 直近 20 件のジョブ実行履歴
      active_jobs[]: 今 RUNNING ステータスのジョブ
    """
    import app_youtube as _ytm  # quota helpers
    out_channels = []
    for ch in get_channels():
        ch_dir = Path(ch.get("folder") or "")
        if not ch_dir.exists():
            continue
        vols = []
        # vol フォルダを vol 番号降順で最大 5 件
        try:
            entries = [d for d in ch_dir.iterdir() if d.is_dir() and parse_video_folder_name(d.name)]
        except Exception:
            entries = []
        def _vol_key(p):
            info = parse_video_folder_name(p.name)
            return info["num"] if info else -1
        entries.sort(key=_vol_key, reverse=True)
        for d in entries[:5]:
            info = parse_video_folder_name(d.name)
            if not info:
                continue
            stage_info = _infer_stage_from_artifacts(d)
            concept_file = d / "concept.txt"
            concept = concept_file.read_text(encoding="utf-8").strip() if concept_file.exists() else ""
            up_marker = d / "youtube_upload.json"
            video_id = ""
            scheduled_publish_at = ""
            published_at = ""
            privacy_now = ""
            if up_marker.exists():
                try:
                    upm = json.loads(up_marker.read_text(encoding="utf-8"))
                    video_id = upm.get("video_id", "")
                    scheduled_publish_at = upm.get("scheduled_publish_at") or ""
                    published_at = upm.get("published_at") or ""
                    privacy_now = upm.get("privacy") or ""
                except Exception:
                    pass
            vols.append({
                "vol": info["num_text"],
                "name": d.name,
                "completed": stage_info["completed"],
                "current": stage_info["current"],
                "is_done": stage_info["is_done"],
                "publish_date": info.get("publish_date", ""),
                "is_uploaded": bool(video_id),
                "video_id": video_id,
                "concept": concept,
                # P2-7: 公開ゲート状態
                "scheduled_publish_at": scheduled_publish_at,
                "published_at": published_at,
                "privacy": privacy_now,
            })
        # quota 状態
        quota_block = {}
        try:
            used = _ytm.quota_used_in_window(ch_dir)
            cap = _ytm.DEFAULT_DAILY_QUOTA_CAP
            quota_block = {
                "used": used, "cap": cap, "remaining": max(0, cap - used),
                "per_upload": _ytm.QUOTA_PER_UPLOAD,
                "window_h": _ytm.QUOTA_WINDOW_HOURS,
            }
        except Exception:
            pass
        out_channels.append({
            "channel_id": ch.get("id", ""),
            "channel_name": ch.get("name", ""),
            "channel_folder": str(ch_dir),
            "icon_url": ch.get("icon_url", ""),
            "vols": vols,
            "quota": quota_block,
        })
    # スケジューラ履歴 + 実行中ジョブ
    history_recent = list(reversed(_scheduler_history[-20:]))
    active_jobs = [h for h in history_recent if h.get("status") == "started"][:5]
    return {
        "channels": out_channels,
        "history_recent": history_recent,
        "active_jobs": active_jobs,
        "stages": _PIPELINE_STAGES,
    }

@app.get("/api/videos/{video_name}/detail")
def api_video_detail(video_name: str):
    """動画フォルダの詳細情報（書き出し状態、ファイル一覧）"""
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    folder = channel_dir / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    folder_info = parse_video_folder_name(video_name)
    m = re.match(r'^(\d+)_', video_name)
    num = folder_info["num_text"] if folder_info else (m.group(1) if m else "00")

    # 各ファイルの存在チェック
    has_music = (folder / "music").is_dir() and any(_is_usable_audio_file(p) for p in (folder / "music").iterdir())
    music_count = sum(1 for p in (folder / "music").iterdir() if _is_usable_audio_file(p)) if (folder / "music").is_dir() else 0
    has_srt = any(folder.glob("subtitles_*.srt"))
    has_prproj = any(folder.glob("*vol*.prproj"))
    has_psd = any(folder.glob("*vol*.psd"))
    has_desc = (folder / "youtube_description.txt").exists()
    has_timecode = any(folder.glob("music_time_code_info_*.txt"))

    # サムネイル
    thumb = None
    for p in [f"vol{num}.jpg", f"vol{num}.png", "サムネイル.jpg"]:
        if (folder / p).exists():
            thumb = p
            break

    # 背景画像（パイプライン STEP 3 / step_bgimage）の完了マーカー: vol{num}.png または vol{num}.jpg
    # サムネイル兼用パスと意図的に重なるが、判定軸として独立に持つ（UI ステッパー用）
    has_bg_image = (folder / f"vol{num}.png").exists() or (folder / f"vol{num}.jpg").exists()

    # MP4（書き出し済み）- チャンネルフォルダ内 / 外部パスのサブフォルダ / flat 配置を全探索
    mp4_file = None
    mp4_source = ""
    mp4_size = 0
    export_path = config.get("export_path", "")
    manual_mp4 = _read_manual_exported_mp4(folder)
    found = _find_exported_mp4(folder, video_name)
    if found is not None:
        mp4_file = str(found)
        mp4_source = "manual" if manual_mp4 is not None and found == manual_mp4 else "auto"
        try:
            mp4_size = found.stat().st_size
        except Exception:
            mp4_size = 0

    # 説明文の内容
    desc_text = ""
    if has_desc:
        desc_text = (folder / "youtube_description.txt").read_text(encoding="utf-8")

    # 公開日
    publish_date = folder_info["publish_date"] if folder_info else ""

    has_upload = (folder / "youtube_upload.json").exists()
    has_tags = (folder / "youtube_tags.txt").exists()
    upload_info = {}
    if has_upload:
        try:
            upload_info = json.loads((folder / "youtube_upload.json").read_text(encoding="utf-8"))
        except Exception:
            upload_info = {}

    # 準備状態の判定
    readiness = {
        "music": has_music,
        "thumbnail": thumb is not None,
        "bg_image": has_bg_image,
        "premiere": has_prproj,
        "srt": has_srt,
        "timecode": has_timecode,
        "mp4": mp4_file is not None,
        "description": has_desc,
        "tags": has_tags,
        "upload": has_upload,
    }
    all_ready = all([has_music, thumb is not None, has_prproj, has_srt, mp4_file is not None, has_desc])

    return {
        "num": num,
        "name": video_name,
        "path": str(folder),
        "publish_date": publish_date,
        "music_count": music_count,
        "thumbnail": thumb,
        "has_bg_image": has_bg_image,
        "has_prproj": has_prproj,
        "has_psd": has_psd,
        "readiness": readiness,
        "all_ready": all_ready,
        "mp4_file": mp4_file,
        "mp4_source": mp4_source,
        "mp4_size_mb": round(mp4_size / 1024 / 1024, 1) if mp4_size else 0,
        "description": desc_text,
        "export_path": export_path,
        "upload_info": upload_info,
    }


class VideoMp4ReferenceUpdate(BaseModel):
    path: str


@app.get("/api/videos/{video_name}/mp4-candidates")
def api_video_mp4_candidates(video_name: str, folder: Optional[str] = None):
    """動画詳細から使う MP4 候補一覧。
    folder 指定時はそのフォルダ配下も検索し、任意の書き出し済み動画を紐づけ可能にする。
    """
    config = get_dashboard_config()
    video_folder = Path(config["channel_folder"]) / video_name
    if not video_folder.exists():
        raise HTTPException(404, "動画フォルダが見つかりません")
    extra = Path(folder).expanduser() if folder else None
    current = _find_exported_mp4(video_folder, video_name)
    candidates = _collect_mp4_candidates(video_folder, video_name, extra)
    return {
        "video_name": video_name,
        "current": _mp4_candidate_payload(current, "current") if current else None,
        "candidates": candidates,
    }


@app.put("/api/videos/{video_name}/mp4-reference")
def api_put_video_mp4_reference(video_name: str, req: VideoMp4ReferenceUpdate):
    config = get_dashboard_config()
    video_folder = Path(config["channel_folder"]) / video_name
    if not video_folder.exists():
        raise HTTPException(404, "動画フォルダが見つかりません")
    payload = _write_manual_exported_mp4(video_folder, Path(req.path))
    return {"status": "ok", "video_name": video_name, "mp4": payload}


@app.delete("/api/videos/{video_name}/mp4-reference")
def api_delete_video_mp4_reference(video_name: str):
    config = get_dashboard_config()
    video_folder = Path(config["channel_folder"]) / video_name
    if not video_folder.exists():
        raise HTTPException(404, "動画フォルダが見つかりません")
    marker = video_folder / MANUAL_EXPORTED_VIDEO_FILE
    if marker.exists():
        marker.unlink()
    return {"status": "ok", "video_name": video_name}

class VideoTitleUpdate(BaseModel):
    video_name: str
    new_title: str  # YouTube用タイトル

@app.put("/api/videos/{video_name}/title")
def api_update_video_title(video_name: str, req: VideoTitleUpdate):
    """動画のYouTubeタイトルを保存"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    title_file = folder / "youtube_title.txt"
    title_file.write_text(req.new_title, encoding="utf-8")
    return {"status": "ok"}

@app.get("/api/videos/{video_name}/title")
def api_get_video_title(video_name: str):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    title_file = folder / "youtube_title.txt"
    if title_file.exists():
        return {"title": title_file.read_text(encoding="utf-8").strip()}
    m = re.match(r'^(\d+)_', video_name)
    num = m.group(1) if m else "00"
    return {"title": f"orzz. vol.{num}"}

# ─── API: タグ ───

class VideoTagsUpdate(BaseModel):
    tags: List[str]

@app.get("/api/videos/{video_name}/tags")
def api_get_video_tags(video_name: str):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    tags_file = folder / "youtube_tags.txt"
    if not tags_file.exists():
        return {"tags": []}
    lines = tags_file.read_text(encoding="utf-8").splitlines()
    return {"tags": [line.strip() for line in lines if line.strip()]}

@app.put("/api/videos/{video_name}/tags")
def api_update_video_tags(video_name: str, req: VideoTagsUpdate):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    tags_file = folder / "youtube_tags.txt"
    tags_file.write_text("\n".join([t.strip() for t in req.tags if t.strip()]), encoding="utf-8")
    return {"status": "ok", "count": len([t for t in req.tags if t.strip()])}

# ─── API: コンセプト（端的な日本語1行） ───

class VideoConceptUpdate(BaseModel):
    concept: str

@app.get("/api/videos/{video_name}/concept")
def api_get_video_concept(video_name: str):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    f = folder / "concept.txt"
    return {"concept": f.read_text(encoding="utf-8").strip() if f.exists() else ""}

@app.put("/api/videos/{video_name}/concept")
def api_update_video_concept(video_name: str, req: VideoConceptUpdate):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    text = (req.concept or "").strip()
    f = folder / "concept.txt"
    if text:
        f.write_text(text, encoding="utf-8")
    elif f.exists():
        f.unlink()
    return {"status": "ok", "concept": text}

@app.post("/api/videos/{video_name}/generate-concept")
def api_generate_video_concept(video_name: str):
    """Claude CLI でその動画の端的な日本語コンセプトを生成して concept.txt に保存。"""
    from claude_proposer import propose_concept, gather_context
    import app_competitor as _ac
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    persona = config.get("persona", "")
    channel_name = config.get("channel_name", "orzz.")
    ctx = gather_context(folder)

    benchmark_analysis = None
    try:
        cache = _ac.load_cache()
        if cache and isinstance(cache, dict):
            a = cache.get("analysis")
            benchmark_analysis = a if isinstance(a, dict) else None
    except Exception:
        benchmark_analysis = None

    try:
        concept = propose_concept(
            cli_cmd=cli_cmd, persona=persona, channel_name=channel_name,
            benchmark_analysis=benchmark_analysis, **ctx,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    if concept:
        (folder / "concept.txt").write_text(concept, encoding="utf-8")
    return {"status": "ok", "concept": concept}

# ─── API: 多言語 localizations 生成（YouTube snippet.localizations 用） ───

DEFAULT_LOCALIZATION_LANGS = ["ja", "zh-Hans", "zh-Hant", "ko", "es", "es-419", "pt-BR", "fr", "de", "it"]


class GenerateLocalizationsRequest(BaseModel):
    languages: Optional[List[str]] = None  # 未指定なら DEFAULT_LOCALIZATION_LANGS
    force: bool = False                    # True なら既存 youtube_localizations.json を無視して再生成


def _build_localizations_prompt(title: str, description: str, langs: List[str]) -> str:
    lang_list = ", ".join(langs)
    keys_block = ",".join(f'"{l}":{{"title":"...","description":"..."}}' for l in langs)
    return f"""Translate the following YouTube video metadata into these languages: {lang_list}. Return JSON only.

ORIGINAL (English):
TITLE: {title}
DESCRIPTION:
{description}

RULES:
- Translate naturally; keep the BGM-channel mood.
- Preserve Tracklist with timestamps verbatim.
- Title within ~80 chars.
- zh-Hans = Simplified Chinese, zh-Hant = Traditional Chinese.
- es = Spain Spanish, es-419 = Latin America Spanish.
- pt-BR = Brazilian Portuguese.
- In description, escape newlines as \\n inside JSON strings (literal \\n, NOT raw line breaks).

OUTPUT (JSON only, no markdown fence, no explanation, all values single-line with \\n escapes):
{{{keys_block}}}"""


@app.post("/api/videos/{video_name}/generate-localizations")
def api_generate_localizations(video_name: str, req: GenerateLocalizationsRequest):
    """Claude CLI で 10 言語翻訳して youtube_localizations.json を生成。

    入力: <vol_folder>/youtube_title.txt + youtube_description.txt
    出力: <vol_folder>/youtube_localizations.json (上書き)
    使い所: app_youtube.py が upload 時に同 JSON を読んで snippet.localizations として送信
    """
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, f"vol_folder が存在しません: {folder}")

    title_path = folder / "youtube_title.txt"
    desc_path = folder / "youtube_description.txt"
    if not title_path.exists() or not desc_path.exists():
        raise HTTPException(400, "youtube_title.txt または youtube_description.txt がありません。先に meta step / suggest API を実行してください")

    out_path = folder / "youtube_localizations.json"
    if out_path.exists() and not req.force:
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            return {"status": "skipped", "reason": "既存ファイルあり (force=true で再生成可能)",
                    "video_name": video_name, "languages": list(existing.keys()),
                    "output_path": str(out_path)}
        except Exception:
            pass  # 壊れていれば再生成

    title = title_path.read_text(encoding="utf-8").strip()
    description = desc_path.read_text(encoding="utf-8").strip()
    langs = req.languages or DEFAULT_LOCALIZATION_LANGS
    prompt = _build_localizations_prompt(title, description, langs)

    # Claude CLI 呼び出し（pipeline の _generate_scene_copy_en と同じパターン）
    suno_cfg_path = CONFIG_DIR / "suno_config.json"
    cli = "claude"
    if suno_cfg_path.exists():
        try:
            cli = (json.loads(suno_cfg_path.read_text(encoding="utf-8")).get("claude_cli") or "claude")
        except Exception:
            pass

    try:
        from app_llm_runner import run_llm, LLMError
        raw = run_llm(prompt, cli_cmd=cli, timeout=180, label="localizations")
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:200]}")
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        raise HTTPException(500, f"JSON が抽出できませんでした: {raw[:200]}")
    try:
        data = json.loads(m.group(0), strict=False)
    except json.JSONDecodeError as e:
        raise HTTPException(500, f"JSON parse 失敗: {e}; raw head: {m.group(0)[:200]}")

    out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "video_name": video_name,
        "languages": list(data.keys()),
        "output_path": str(out_path),
    }


# ─── API: 動画 mp4 の場所・情報を統一表示 ───

@app.get("/api/videos/{video_name}/mp4-info")
def api_mp4_info(video_name: str):
    """vol の mp4 をどこから検出したか、サイズ・解像度・尺・コーデックを返す。

    検出ソース判定（_find_exported_mp4 の探索順と整合）:
      - "manual_exported_video": <vol_folder>/manual_exported_video.json で紐づけ済
      - "vol_folder": <vol_folder>/*.mp4
      - "external_ssd_per_video": <export_path>/<video_name>/*.mp4
      - "external_ssd_flat": <export_path>/<prefix>_vol<num>*.mp4 (flat 配置)
    """
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, f"vol_folder が存在しません: {folder}")

    mp4 = _find_exported_mp4(folder, video_name)
    if not mp4:
        return {"present": False, "path": None, "source": None}

    mp4_str = str(mp4)
    source = "unknown"
    # manual_exported_video.json と一致するか
    manual_path = folder / MANUAL_EXPORTED_VIDEO_FILE
    if manual_path.exists():
        try:
            md = json.loads(manual_path.read_text(encoding="utf-8"))
            if md.get("path") == mp4_str:
                source = "manual_exported_video"
        except Exception:
            pass
    if source == "unknown":
        if mp4_str.startswith(str(folder) + "/"):
            source = "vol_folder"
        elif mp4_str.startswith("/Volumes/"):
            # per_video subfolder か flat か
            try:
                ext_dir = _resolve_external_export_dir()
            except Exception:
                ext_dir = None
            if ext_dir is not None and mp4.parent == (ext_dir / video_name):
                source = "external_ssd_per_video"
            else:
                source = "external_ssd_flat"

    info: dict = {
        "present": True,
        "path": mp4_str,
        "source": source,
        "size_bytes": mp4.stat().st_size,
    }
    # ffprobe があれば解像度・尺・コーデックを取得（無くても non-fatal）
    import shutil as _sh
    ffprobe = _sh.which("ffprobe")
    if ffprobe:
        try:
            proc = subprocess.run(
                [ffprobe, "-v", "error",
                 "-show_entries", "format=duration",
                 "-show_entries", "stream=width,height,codec_name,codec_type",
                 "-of", "json", mp4_str],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0:
                fd = json.loads(proc.stdout)
                for s in fd.get("streams", []):
                    if s.get("codec_type") == "video" or "width" in s:
                        info["width"] = s.get("width")
                        info["height"] = s.get("height")
                        info["resolution"] = f'{s.get("width")}x{s.get("height")}'
                        info["video_codec"] = s.get("codec_name")
                        break
                fmt = fd.get("format", {})
                if fmt.get("duration"):
                    info["duration_sec"] = float(fmt["duration"])
        except Exception:
            pass
    return info


# ─── API: メタ情報の充足状況チェック ───

@app.get("/api/videos/{video_name}/meta-status")
def api_meta_status(video_name: str):
    """動画 vol の meta データ充足状況を返す（Web UI のチェックリスト用）。
    各項目の present / ok を見て、ready_for_upload で総合判定。
    """
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, f"vol_folder が存在しません: {folder}")

    def _check_text(filename: str, min_size: int = 1) -> dict:
        p = folder / filename
        if not p.exists():
            return {"present": False, "size": 0, "ok": False}
        size = p.stat().st_size
        try:
            preview = p.read_text(encoding="utf-8").strip()[:80]
        except Exception:
            preview = ""
        return {"present": True, "size": size, "ok": size >= min_size, "preview": preview}

    title = _check_text("youtube_title.txt", 5)
    description = _check_text("youtube_description.txt", 50)
    tags = _check_text("youtube_tags.txt", 5)

    loc_path = folder / "youtube_localizations.json"
    if loc_path.exists():
        try:
            d = json.loads(loc_path.read_text(encoding="utf-8"))
            locs = {"present": True, "languages": list(d.keys()), "count": len(d), "ok": len(d) >= 5}
        except Exception as e:
            locs = {"present": True, "error": str(e), "ok": False}
    else:
        locs = {"present": False, "languages": [], "count": 0, "ok": False}

    mp4 = _find_exported_mp4(folder, folder.name)
    mp4_info: dict = {"present": mp4 is not None, "path": str(mp4) if mp4 else None}
    if mp4:
        try:
            mp4_info["size"] = mp4.stat().st_size
        except Exception:
            pass

    upload_info: dict = {"present": False, "video_id": None, "url": None}
    up_path = folder / "youtube_upload.json"
    if up_path.exists():
        try:
            ud = json.loads(up_path.read_text(encoding="utf-8"))
            upload_info = {
                "present": True,
                "video_id": ud.get("video_id"),
                "url": ud.get("url"),
                "title": ud.get("title"),
                "localizations_applied": ud.get("localizations_applied") or [],
            }
        except Exception:
            pass

    ready_for_upload = bool(title.get("ok") and description.get("ok") and tags.get("ok") and mp4_info["present"])
    return {
        "video_name": video_name,
        "title": title,
        "description": description,
        "tags": tags,
        "localizations": locs,
        "mp4": mp4_info,
        "upload": upload_info,
        "ready_for_upload": ready_for_upload,
    }


# ─── API: 動画の非表示 / 復元 ───

@app.post("/api/videos/{video_name}/hide")
def api_video_hide(video_name: str):
    """動画を一覧から非表示にする（フォルダは維持、.hidden ファイルを作成）"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    (folder / ".hidden").write_text("", encoding="utf-8")
    return {"status": "ok", "hidden": True}

@app.post("/api/videos/{video_name}/unhide")
def api_video_unhide(video_name: str):
    """非表示を解除"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    hidden_file = folder / ".hidden"
    if hidden_file.exists():
        hidden_file.unlink()
    return {"status": "ok", "hidden": False}


# ─── API: 楽曲 (メディアプレイヤー + いいね) ───

AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".flac", ".aac")

def _parse_track_likes(filename: str):
    """ファイル名先頭の 'z+_' / 'x_' を抜いていいね数・バッド状態と素の名前を返す"""
    stem = Path(filename).stem
    ext = Path(filename).suffix
    # バッド: x_ プレフィックス
    is_bad = False
    if stem.startswith("x_"):
        is_bad = True
        stem = stem[2:]
    # いいね: z+ プレフィックス
    m = re.match(r"^(z+)_(.+)$", stem)
    if m:
        return len(m.group(1)), is_bad, m.group(2) + ext
    return 0, is_bad, stem + ext


def _get_duration(filepath: Path) -> float:
    """ffprobe で MP3 の再生時間（秒）を取得"""
    try:
        proc = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(filepath)],
            capture_output=True, text=True, timeout=10,
        )
        return float(proc.stdout.strip())
    except Exception:
        return 0.0

def _apply_track_prefix(base_filename: str, likes: int, is_bad: bool) -> str:
    """base_filename に likes 数の z+_ / バッドの x_ を付与"""
    _, _, base = _parse_track_likes(base_filename)  # 既存プレフィックス除去
    prefix = ""
    if is_bad:
        prefix = "x_"
    elif likes > 0:
        prefix = f"{'z' * likes}_"
    return f"{prefix}{base}"


def _is_deleted_track_name(name: str) -> bool:
    return Path(name).stem.startswith("x_")


def _is_usable_audio_file(path: Path) -> bool:
    return (
        path.is_file()
        and path.suffix.lower() in AUDIO_EXTS
        and not path.name.startswith(".")
        and not _is_deleted_track_name(path.name)
    )


def _schedule_track_file_delete(path: Path, delay_sec: int = 5, *, folder: Optional[Path] = None, base_name: str = "") -> None:
    """× 押下後の猶予を置いて、ローカルファイルを物理削除する。
    同じ base_name のバックアップ/処理済みファイルも消し、後工程に残さない。"""
    path = Path(path)
    folder = Path(folder) if folder else None
    base_name = (base_name or _parse_track_likes(path.name)[2]).strip()

    def _worker():
        time.sleep(delay_sec)
        targets: list[Path] = [path]
        if folder and base_name:
            for d in [folder, folder / "music", folder / "original_music"]:
                if not d.is_dir():
                    continue
                for p in d.iterdir():
                    if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                        continue
                    if _parse_track_likes(p.name)[2] == base_name:
                        targets.append(p)
        seen: set[Path] = set()
        try:
            for p in targets:
                rp = p.resolve()
                if rp in seen:
                    continue
                seen.add(rp)
                if p.exists() and p.is_file() and p.suffix.lower() in AUDIO_EXTS:
                    p.unlink()
                    print(f"[track-delete] deleted after {delay_sec}s: {p}", flush=True)
        except Exception as e:
            print(f"[track-delete] failed: {path}: {e}", flush=True)

    threading.Thread(target=_worker, daemon=True, name="track-delayed-delete").start()


def _purge_stale_deleted_tracks(folder: Path, min_age_sec: int = 5) -> int:
    """過去に残った x_ 音声を掃除する。新規×直後の猶予は mtime で守る。"""
    folder = Path(folder)
    deleted = 0
    now = time.time()
    for d in [folder, folder / "music", folder / "original_music"]:
        if not d.is_dir():
            continue
        for p in list(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            if not _is_deleted_track_name(p.name):
                continue
            try:
                if now - p.stat().st_mtime < min_age_sec:
                    continue
                p.unlink()
                deleted += 1
                print(f"[track-delete] purged stale: {p}", flush=True)
            except Exception as e:
                print(f"[track-delete] purge failed: {p}: {e}", flush=True)
    return deleted


@app.get("/api/videos/{video_name}/tracks")
def api_video_tracks(video_name: str):
    """動画フォルダ直下 + music/ + original_music/ の音声ファイルを一覧"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    _purge_stale_deleted_tracks(folder)

    tracks = []
    # スキャン対象: フォルダ直下 → music/ → original_music/
    scan_dirs = [("root", folder)]
    for sub in ["music", "original_music"]:
        sp = folder / sub
        if sp.is_dir():
            scan_dirs.append((sub, sp))

    for location, d in scan_dirs:
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS:
                continue
            if p.name.startswith("."):
                continue
            likes, is_bad, base_name = _parse_track_likes(p.name)
            rel = p.relative_to(folder).as_posix()
            duration = _get_duration(p)
            tracks.append({
                "filename": p.name,
                "base_name": base_name,
                "likes": likes,
                "is_bad": is_bad,
                "duration": round(duration, 1),
                "size": p.stat().st_size,
                "location": location,
                "rel_path": rel,
            })
    return {"tracks": tracks}


@app.get("/api/videos/{video_name}/track-file/{rel_path:path}")
def api_video_track_file(video_name: str, rel_path: str):
    """音声ファイル配信 (HTML audio 用)。rel_path は フォルダ相対パス"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    # 安全: "/", ".." の除去チェック
    if ".." in rel_path.split("/"):
        raise HTTPException(400, "不正なパス")
    fp = (folder / rel_path).resolve()
    try:
        fp.relative_to(folder.resolve())
    except ValueError:
        raise HTTPException(400, "フォルダ外アクセス禁止")
    if not fp.exists() or fp.suffix.lower() not in AUDIO_EXTS:
        raise HTTPException(404, "ファイルが見つかりません")
    return FileResponse(fp, media_type="audio/mpeg")


class TrackLikeRequest(BaseModel):
    rel_path: str
    delta: int = 0              # いいね: +1 / -1
    set_to: Optional[int] = None
    set_likes: Optional[int] = None  # set_to のエイリアス
    toggle_bad: Optional[bool] = None  # True で 👎 トグル

@app.post("/api/videos/{video_name}/track-like")
def api_video_track_like(video_name: str, req: TrackLikeRequest):
    """いいね/×変更 → ファイル名をリネーム。
    × は除外マークではなく削除予約として扱い、5秒後に物理削除する。"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)

    src = (folder / req.rel_path).resolve()
    try:
        src.relative_to(folder.resolve())
    except ValueError:
        raise HTTPException(400, "フォルダ外")
    if not src.exists():
        raise HTTPException(404, "ファイルが見つかりません")

    current_likes, current_bad, base_name = _parse_track_likes(src.name)
    delete_after_sec = None

    if req.toggle_bad is not None:
        # UI の × ボタンは「除外」ではなく「削除予約」。
        # すでに x_ の場合も解除せず、そのまま削除を予約する。
        new_bad = True
        new_likes = 0
        delete_after_sec = 5
    else:
        new_bad = current_bad
        if req.set_likes is not None:
            new_likes = max(0, int(req.set_likes))
        elif req.set_to is not None:
            new_likes = max(0, int(req.set_to))
        else:
            new_likes = max(0, current_likes + int(req.delta))
        if new_likes > 0:
            new_bad = False  # いいね付けたらバッド解除

    new_name = _apply_track_prefix(base_name, new_likes, new_bad)
    if new_name == src.name:
        if delete_after_sec:
            try:
                os.utime(src, None)
            except Exception:
                pass
            _schedule_track_file_delete(src, delete_after_sec, folder=folder, base_name=base_name)
        return {"status": "unchanged", "likes": new_likes, "is_bad": new_bad,
                "filename": src.name, "rel_path": req.rel_path,
                "delete_after_sec": delete_after_sec}

    dst = src.parent / new_name
    if dst.exists() and dst.resolve() != src.resolve():
        for i in range(1, 100):
            cand = src.parent / f"{dst.stem}_{i}{dst.suffix}"
            if not cand.exists():
                dst = cand
                break

    src.rename(dst)
    if delete_after_sec:
        try:
            os.utime(dst, None)
        except Exception:
            pass
        _schedule_track_file_delete(dst, delete_after_sec, folder=folder, base_name=base_name)
    rel_new = dst.relative_to(folder).as_posix()
    return {"status": "ok", "likes": new_likes, "is_bad": new_bad,
            "filename": dst.name, "rel_path": rel_new,
            "delete_after_sec": delete_after_sec}


class BulkDeleteRequest(BaseModel):
    mode: str  # "bad" / "no_likes" / "short"
    min_duration: Optional[float] = None  # "short" 時: この秒数以下を削除

@app.post("/api/videos/{video_name}/tracks-bulk-delete")
def api_video_tracks_bulk_delete(video_name: str, req: BulkDeleteRequest):
    """条件に合う楽曲を一括削除"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)

    deleted = 0
    skipped = 0
    scan_dirs = [folder]
    for sub in ["music", "original_music"]:
        sp = folder / sub
        if sp.is_dir():
            scan_dirs.append(sp)

    for d in scan_dirs:
        for p in list(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in AUDIO_EXTS or p.name.startswith("."):
                continue
            likes, is_bad, base = _parse_track_likes(p.name)
            should_delete = False

            if req.mode == "bad" and is_bad:
                should_delete = True
            elif req.mode == "no_likes" and likes == 0 and not is_bad:
                should_delete = True
            elif req.mode == "short" and req.min_duration:
                dur = _get_duration(p)
                if dur > 0 and dur < req.min_duration:
                    should_delete = True

            if should_delete:
                try:
                    p.unlink()
                    deleted += 1
                except Exception:
                    skipped += 1

    return {"status": "ok", "deleted": deleted, "skipped": skipped}


@app.delete("/api/videos/{video_name}/track")
def api_video_track_delete(video_name: str, rel_path: str):
    """楽曲ファイルを物理削除"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    if ".." in rel_path.split("/"):
        raise HTTPException(400, "不正なパス")
    fp = (folder / rel_path).resolve()
    try:
        fp.relative_to(folder.resolve())
    except ValueError:
        raise HTTPException(400, "フォルダ外アクセス禁止")
    if not fp.exists() or fp.suffix.lower() not in AUDIO_EXTS:
        raise HTTPException(404, "ファイルが存在しません")
    try:
        fp.unlink()
        return {"status": "ok", "deleted": rel_path}
    except Exception as e:
        raise HTTPException(500, f"削除失敗: {e}")


# ─── API: 楽曲 後処理（リネーム + FFmpeg）───

PROCESS_TRACKS_SCRIPT = SHARED_BASE / "Python" / "app_process_tracks.py"

@app.post("/api/videos/{video_name}/process-tracks")
async def api_video_process_tracks(video_name: str, rename_only: bool = False):
    """Claude CLI でタイトル提案 + ffmpeg で無音トリム+フェードアウト+ゲイン正規化 → music/ に出力

    Query param `rename_only=true` でリネームのみ（ffmpeg スキップ）
    """
    await _ensure_not_running("process", "後処理が既に実行中です")
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cmd = [sys.executable, "-u", str(PROCESS_TRACKS_SCRIPT), str(folder),
           "--cli", cli_cmd]
    if rename_only:
        cmd += ["--rename-only"]
    task_logs["process"] = []
    import datetime as _dt
    task_meta["process"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "video_name": video_name,
    }
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    active_tasks["process"] = proc
    _stream_subprocess(proc, "process")
    return {"status": "started"}

@app.get("/api/process/status")
def api_process_status():
    proc = active_tasks.get("process")
    running = proc is not None and proc.returncode is None
    return {"running": running, "logs": task_logs.get("process", [])[-500:]}


# ─── API: 画像選択（JSX 連動）───

IMAGE_EXTS = (".jpg", ".jpeg", ".png")

@app.get("/api/videos/{video_name}/images")
def api_video_images(video_name: str):
    """動画フォルダ内の画像一覧を返す。サムネ判定＋選択状態も返却。"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    # 選択状態を読み込み
    sel_file = folder / "selected_images.json"
    sel = {"main": "", "sub": []}
    if sel_file.exists():
        try:
            sel = json.loads(sel_file.read_text(encoding="utf-8"))
            sel.setdefault("main", "")
            sel.setdefault("sub", [])
        except Exception:
            pass

    m = re.match(r"^(\d+)_", video_name)
    num = m.group(1) if m else ""

    images = []
    for p in sorted(folder.iterdir()):
        if not p.is_file():
            continue
        if p.suffix.lower() not in IMAGE_EXTS:
            continue
        if p.name.startswith("."):
            continue
        fn = p.name
        kind = "none"
        if sel["main"] == fn:
            kind = "main"
        elif fn in sel["sub"]:
            kind = "sub"
        is_thumb = (fn == f"vol{num}.jpg" or fn == f"vol{num}.png" or fn == "サムネイル.jpg")
        images.append({
            "filename": fn,
            "size": p.stat().st_size,
            "kind": kind,
            "is_thumb": is_thumb,
        })
    return {"images": images, "selected": sel}


@app.get("/api/videos/{video_name}/image-file/{filename:path}")
def api_video_image_file(video_name: str, filename: str):
    """動画フォルダ内の画像を配信（サムネ・背景プレビュー用）"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    # パストラバーサル防止
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "不正なファイル名")
    fp = folder / filename
    if not fp.exists() or fp.suffix.lower() not in IMAGE_EXTS:
        raise HTTPException(404, "画像が見つかりません")
    return FileResponse(fp)


class SelectedImagesUpdate(BaseModel):
    main: Optional[str] = ""
    sub: Optional[List[str]] = []

@app.put("/api/videos/{video_name}/selected-images")
def api_update_selected_images(video_name: str, req: SelectedImagesUpdate):
    """選択画像を selected_images.json に保存（JSX が読み込む）"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    data = {
        "main": (req.main or "").strip(),
        "sub": [s.strip() for s in (req.sub or []) if s and s.strip()],
    }
    (folder / "selected_images.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"status": "ok", **data}

@app.delete("/api/videos/{video_name}/selected-images")
def api_delete_selected_images(video_name: str):
    """選択状態をリセット（JSX は初期値フォールバック）"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    sel_file = folder / "selected_images.json"
    if sel_file.exists():
        sel_file.unlink()
    return {"status": "ok"}

# ─── API: Claude 提案（タイトル・説明・タグ）───

class SuggestRequest(BaseModel):
    mode: str                       # "titles" | "description" | "tags"
    count: Optional[int] = 5        # titles 用
    reference: Optional[str] = None # description 用（任意の参考文）

@app.post("/api/videos/{video_name}/suggest")
def api_video_suggest(video_name: str, req: SuggestRequest):
    """Claude CLI でタイトル/説明/タグを提案（JSON出力・API未使用）

    ベンチマーク分析キャッシュ（competitor_analysis_cache.json）が存在すれば、
    そこに含まれる viewer_needs / keywords / tag_suggestions 等を視聴者文脈として
    プロンプトに自動注入する（無ければ persona のみで動作）。
    """
    from claude_proposer import (
        propose_titles, propose_description, propose_tags, gather_context,
    )
    import app_competitor as _ac
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    persona = config.get("persona", "")
    channel_name = config.get("channel_name", "orzz.")
    ctx = gather_context(folder)

    # ベンチマーク分析キャッシュを optional 注入
    benchmark_analysis = None
    try:
        cache = _ac.load_cache()
        if cache and isinstance(cache, dict):
            benchmark_analysis = cache.get("analysis") if isinstance(cache.get("analysis"), dict) else None
    except Exception:
        benchmark_analysis = None

    try:
        if req.mode == "titles":
            titles = propose_titles(
                cli_cmd=cli_cmd, persona=persona, channel_name=channel_name,
                count=req.count or 5, benchmark_analysis=benchmark_analysis, **ctx,
            )
            return {"status": "ok", "titles": titles}
        elif req.mode == "description":
            description = propose_description(
                cli_cmd=cli_cmd, persona=persona, channel_name=channel_name,
                reference=req.reference or "", benchmark_analysis=benchmark_analysis, **ctx,
            )
            return {"status": "ok", "description": description}
        elif req.mode == "tags":
            tags = propose_tags(cli_cmd=cli_cmd, persona=persona,
                                channel_name=channel_name,
                                benchmark_analysis=benchmark_analysis, **ctx)
            return {"status": "ok", "tags": tags}
        else:
            raise HTTPException(400, f"未知のmode: {req.mode}")
    except RuntimeError as e:
        raise HTTPException(500, str(e))

# ─── API: パイプライン自動実行 ───

PIPELINE_SCRIPT = SHARED_BASE / "Python" / "app_pipeline.py"

class PipelineRunRequest(BaseModel):
    steps: List[str]  # ["suno", "rename", "premiere", "meta", "upload"]
    duration: Optional[int] = 10800
    suno_count: Optional[int] = None
    privacy: Optional[str] = "unlisted"
    # SUNO 上書き（指定があればフォーム値を pipeline に反映）
    suno_prompt: Optional[str] = None
    suno_interval: Optional[int] = None
    suno_provider: Optional[str] = None
    suno_mode: Optional[str] = None  # styles_title_only / lyrics_styles / lyrics
    suno_batch: Optional[bool] = None
    suno_headless: Optional[bool] = None
    # 多様性制御（チャンネル別。明示があれば自動化設定より優先）
    diversity_threshold: Optional[float] = None
    diversity_retry: Optional[int] = None
    history_limit: Optional[int] = None
    # DL + 整理
    dl_wait_sec: Optional[int] = None
    dl_min_duration: Optional[int] = None

@app.post("/api/videos/{video_name}/run-pipeline")
async def api_run_pipeline(video_name: str, req: PipelineRunRequest):
    """チェックした工程を自動実行"""
    await _ensure_not_running("pipeline", "パイプラインが既に実行中です")
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    # vol 番号を取得
    m = re.match(r"^(\d+)_", video_name)
    vol = m.group(1) if m else "0"

    # チャンネル別の自動化設定を既定値として取り込み（リクエストに明示があればそちらを優先）
    auto_cfg = {}
    ap = _automation_config_path()
    if ap.exists():
        try:
            auto_cfg = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:
            auto_cfg = {}
    def _pick(req_val, auto_key):
        return req_val if req_val is not None else auto_cfg.get(auto_key)
    dur = _pick(req.duration if req.duration != 10800 else None, "premiere_duration") or req.duration or 10800
    privacy = _pick(req.privacy if req.privacy != "unlisted" else None, "upload_privacy") or req.privacy or "unlisted"

    cmd = [sys.executable, "-u", str(PIPELINE_SCRIPT), vol,
           "--via-api", "--duration", str(dur),
           "--privacy", privacy]
    # --only は使えないので、pipeline.py に --steps を追加する必要がある
    # 簡易対応: 全ステップを渡す (pipeline 側で対応)
    task_logs["pipeline"] = []
    import datetime as _dt
    task_meta["pipeline"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "video_name": video_name,
        "steps": req.steps,
    }
    # steps を環境変数で渡す
    env = {**os.environ, "PYTHONUNBUFFERED": "1",
           "APP_PIPELINE_STEPS": ",".join(req.steps)}
    # SUNO 上書き（リクエスト > 自動化設定 > デフォルト の優先順位）
    def _env(key_env, req_val, auto_key, cast=str):
        v = req_val if req_val not in (None, "") else auto_cfg.get(auto_key)
        if v is not None:
            env[key_env] = cast(v) if cast is not bool else ("1" if v else "0")
    _env("APP_SUNO_PROMPT",   req.suno_prompt,   "suno_prompt")
    _env("APP_SUNO_COUNT",    req.suno_count,    "suno_count", str)
    _env("APP_SUNO_INTERVAL", req.suno_interval, "suno_interval", str)
    _env("APP_SUNO_PROVIDER", req.suno_provider, "suno_provider")
    _env("APP_SUNO_MODE",     req.suno_mode,     "suno_mode")
    _env("APP_SUNO_BATCH",    req.suno_batch,    "suno_batch", bool)
    _env("APP_SUNO_HEADLESS", req.suno_headless, "suno_headless", bool)
    _env("APP_SUNO_DIVERSITY_THRESHOLD", req.diversity_threshold, "suno_diversity_threshold", str)
    _env("APP_SUNO_DIVERSITY_RETRY",     req.diversity_retry,     "suno_diversity_retry", str)
    _env("APP_SUNO_HISTORY_LIMIT",       req.history_limit,       "suno_history_limit", str)
    _env("APP_DL_WAIT_SEC",   req.dl_wait_sec,   "dl_wait_sec", str)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=env)
    active_tasks["pipeline"] = proc
    _stream_subprocess(proc, "pipeline")
    return {"status": "started", "steps": req.steps, "vol": vol}

class CreateFromBenchmarkRequest(BaseModel):
    publish_date: str
    use_auto_approval: Optional[bool] = None  # None=設定値を採用
    duration: Optional[int] = None
    privacy: Optional[str] = None
    suno_prompt: Optional[str] = None  # 未指定なら競合分析から自動生成
    suno_count: Optional[int] = None

@app.post("/api/pipeline/create-from-benchmark")
async def api_create_from_benchmark(req: CreateFromBenchmarkRequest):
    """ベンチマーク分析から制作を単一アクションで駆動。

    1. 新 vol フォルダ作成（/api/videos/create 相当）
    2. 競合分析キャッシュから SUNO プロンプト提案（未指定時）
    3. 自動承認モードなら全工程（suno → rename → premiere → export → meta → upload）を起動
    4. OFF なら 1 の vol 情報だけ返して UI 側で個別工程を回す
    """
    await _ensure_not_running("pipeline", "パイプラインが既に実行中です")
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    if not channel_dir.exists():
        raise HTTPException(400, "チャンネルフォルダが存在しません")

    auto_cfg = {}
    ap = _automation_config_path()
    if ap.exists():
        try:
            auto_cfg = json.loads(ap.read_text(encoding="utf-8"))
        except Exception:
            auto_cfg = {}
    auto_approval = req.use_auto_approval if req.use_auto_approval is not None else bool(auto_cfg.get("auto_approval_mode"))

    vf = api_create_video_folder(VideoFolderCreate(publish_date=req.publish_date, open_in_finder=False))
    video_name = vf["folder"]

    suno_prompt = req.suno_prompt
    suno_rationale = None
    if not suno_prompt:
        try:
            import app_competitor as _ac
            cache = _ac.load_cache() or {}
            analysis = cache.get("analysis", {})
            if analysis.get("music_direction"):
                from app_competitor import propose_suno_prompt
                suno_cfg_local = get_suno_config()
                cli_cmd = suno_cfg_local.get("claude_cli") or "claude"
                proposal = propose_suno_prompt(analysis, cli_cmd=cli_cmd)
                suno_prompt = proposal.get("prompt")
                suno_rationale = proposal.get("rationale")
        except Exception as e:
            suno_rationale = f"（SUNO 自動提案失敗、既定プロンプト使用）: {e}"

    if not auto_approval:
        return {
            "status": "created",
            "video_name": video_name,
            "vol": vf["num"],
            "auto_approval": False,
            "suno_prompt": suno_prompt,
            "suno_rationale": suno_rationale,
            "hint": "自動承認モードが OFF のため、フォルダだけ作成しました。個別工程を手動で進めてください。",
        }

    steps = ["suno", "rename", "premiere", "export", "meta", "upload"]
    pipeline_req = PipelineRunRequest(
        steps=steps,
        duration=req.duration or auto_cfg.get("premiere_duration") or 10800,
        privacy=req.privacy or auto_cfg.get("upload_privacy") or "unlisted",
        suno_prompt=suno_prompt,
        suno_count=req.suno_count,
    )
    await api_run_pipeline(video_name, pipeline_req)
    return {
        "status": "started",
        "video_name": video_name,
        "vol": vf["num"],
        "auto_approval": True,
        "suno_prompt": suno_prompt,
        "suno_rationale": suno_rationale,
        "steps": steps,
    }


@app.get("/api/pipeline/status")
def api_pipeline_status():
    proc = active_tasks.get("pipeline")
    running = proc is not None and proc.returncode is None
    return {"running": running, "logs": task_logs.get("pipeline", [])[-200:],
            "meta": task_meta.get("pipeline", {})}


# ─── API: 競合分析 ───

@app.post("/api/analysis/competitors")
async def api_analysis_competitors():
    """ライバルチャンネルを YouTube API で取得 → Claude 分析 → キャッシュ"""
    await _ensure_not_running("analysis", "分析が既に実行中です")
    script = SHARED_BASE / "Python" / "app_competitor.py"
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cmd = [sys.executable, "-u", str(script), "--analyze", "--cli", cli_cmd]
    task_logs["analysis"] = []
    import datetime as _dt
    task_meta["analysis"] = {"started_at": _dt.datetime.now().isoformat()}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1,
                            env={**os.environ, "PYTHONUNBUFFERED": "1"})
    active_tasks["analysis"] = proc
    _stream_subprocess(proc, "analysis")
    return {"status": "started"}


@app.get("/api/analysis/status")
def api_analysis_status():
    proc = active_tasks.get("analysis")
    running = proc is not None and proc.returncode is None
    return {"running": running, "logs": task_logs.get("analysis", [])[-200:]}


@app.get("/api/analysis/spreadsheet-preview")
def api_spreadsheet_preview():
    """スプシ接続テスト: チャンネル数 + ホットチャンネル上位 5"""
    config = get_dashboard_config()
    detail_url = config.get("spreadsheet_channel_detail_url", "").strip()
    growth_url = config.get("spreadsheet_growth_tracking_url", "").strip()
    result = {"detail": None, "growth": None}

    if detail_url:
        try:
            from app_sheets import fetch_csv, parse_channel_detail
            rows = fetch_csv(detail_url)
            channels = parse_channel_detail(rows)
            result["detail"] = {
                "channel_count": len(channels),
                "sample_names": [c.title for c in channels[:5]],
            }
        except Exception as e:
            result["detail"] = {"error": str(e)}

    if growth_url:
        try:
            from app_sheets import fetch_csv, parse_growth_tracking, identify_hot_channels
            rows = fetch_csv(growth_url)
            entries = parse_growth_tracking(rows)
            hot = identify_hot_channels(entries, top_n=5)
            result["growth"] = {
                "channel_count": len(entries),
                "hot_channels": [
                    {"name": e.channel_name, "growth_rate": e.growth_rate,
                     "daily_views": e.daily_view_change}
                    for e in hot
                ],
            }
        except Exception as e:
            result["growth"] = {"error": str(e)}

    return result


# ─── チャンネルアイコン取得（YouTube ページから og:image 系をスクレイプ） ───
# スプシの =IMAGE() は CSV/JSON 出力で値が消えるため、URL から直接取得してキャッシュする。
_CHANNEL_ICON_CACHE_FILE = lambda: CONFIG_DIR / "channel_icon_cache.json"
_CHANNEL_ICON_TTL_SEC = 86400 * 7  # 7 日
_AVATAR_RE = re.compile(
    r'(https?://(?:[a-z0-9-]+\.)?(?:ggpht|googleusercontent)\.com/[A-Za-z0-9_\-]+=s\d+[A-Za-z0-9_\-=.@?]*)'
)


def _load_icon_cache() -> dict:
    p = _CHANNEL_ICON_CACHE_FILE()
    try:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def _save_icon_cache(cache: dict) -> None:
    try:
        _CHANNEL_ICON_CACHE_FILE().write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def _scrape_channel_icon(channel_url: str) -> str:
    """YouTube チャンネルページから アバター画像 URL を取得。失敗時は空文字。"""
    if not channel_url or "youtube.com" not in channel_url:
        return ""
    import urllib.request
    try:
        req = urllib.request.Request(
            channel_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            html = resp.read(1_500_000).decode("utf-8", errors="replace")
    except Exception:
        return ""
    m = _AVATAR_RE.search(html)
    if not m:
        return ""
    url = m.group(1)
    # 大きいサイズ希望（=s900 → そのまま、=s48 等が来たら s176 に上げる）
    return re.sub(r"=s\d+", "=s176", url)


def resolve_channel_icon(channel_url: str) -> str:
    """キャッシュ込みのアイコン URL 解決。"""
    if not channel_url:
        return ""
    cache = _load_icon_cache()
    entry = cache.get(channel_url)
    now = time.time()
    if entry and isinstance(entry, dict):
        ts = entry.get("fetched_at", 0)
        if now - ts < _CHANNEL_ICON_TTL_SEC and entry.get("icon_url"):
            return entry["icon_url"]
    # 取得して保存
    icon = _scrape_channel_icon(channel_url)
    cache[channel_url] = {"icon_url": icon, "fetched_at": now}
    _save_icon_cache(cache)
    return icon


@app.get("/api/analysis/hot-channels")
def api_hot_channels(top_n: int = 10):
    """スプシからホットチャンネルだけ取得（分析なし）。Sheet1 から最新動画サムネ等を補完。"""
    bc = get_benchmark_config()
    config = get_dashboard_config()
    growth_url = (bc.get("spreadsheet_growth_tracking_url") or config.get("spreadsheet_growth_tracking_url", "")).strip()
    detail_url = (bc.get("spreadsheet_channel_detail_url") or config.get("spreadsheet_channel_detail_url", "")).strip()
    if not growth_url:
        raise HTTPException(400, "成長トラッキングシート URL が未設定です")
    try:
        from app_sheets import (
            fetch_csv, parse_growth_tracking, identify_hot_channels,
            parse_channel_detail, match_channels, SheetFetchError,
        )
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
        hot = identify_hot_channels(entries, top_n=top_n)
        # Sheet1 で詳細補完
        details = []
        if detail_url:
            try:
                d_rows = fetch_csv(detail_url)
                details = parse_channel_detail(d_rows)
            except Exception:
                details = []
        detail_by_name = {ch.title: ch for ch in details}
        # YouTube 動画 URL から video_id を抽出してサムネ URL を組み立てる
        # （スプシに =IMAGE() の thumb 列が無い時のフォールバック）
        _vid_re = re.compile(r"(?:v=|youtu\.be/|/embed/|/shorts/)([A-Za-z0-9_-]{11})")
        def _yt_thumb(video_url: str) -> str:
            if not video_url:
                return ""
            m = _vid_re.search(video_url)
            if not m:
                return ""
            return f"https://i.ytimg.com/vi/{m.group(1)}/maxresdefault.jpg"
        out = []
        for e in hot:
            match = detail_by_name.get(e.channel_name)
            if not match and details:
                match = match_channels(e.channel_name, details)
            latest = None
            top_video = None
            channel_url = ""
            icon_url = ""
            if match:
                channel_url = match.url
                icon_url = match.icon_url or ""
                # スプシの =IMAGE() は CSV エクスポートで値が消えるため、
                # icon_url が空なら YouTube ページからスクレイプ（キャッシュ付き）
                if not icon_url and channel_url:
                    try:
                        icon_url = resolve_channel_icon(channel_url)
                    except Exception:
                        icon_url = ""
                if match.recent_videos:
                    v = match.recent_videos[0]
                    latest = {"title": v.title,
                              "thumb_url": v.thumbnail or _yt_thumb(v.url),
                              "published_at": v.publish_date, "url": v.url, "views": v.view_count}
                if match.top_videos:
                    tv = match.top_videos[0]
                    top_video = {"title": tv.title,
                                 "thumb_url": tv.thumbnail or _yt_thumb(tv.url),
                                 "url": tv.url, "views": tv.view_count}
            out.append({
                "name": e.channel_name,
                "growth_rate": e.growth_rate,
                "weekly_growth_pct": round(e.growth_rate * 7, 2),
                "recent_views_7d": e.daily_view_change * 7,
                "daily_views": e.daily_view_change,
                "daily_subs": e.daily_sub_change,
                "total_views": e.total_views,
                "subscribers": e.subscribers,
                "last_updated": e.last_updated,
                "score": round(e.score, 3),
                "channel_url": channel_url,
                "icon_url": icon_url,
                "latest_video": latest,
                "top_video": top_video,
            })
        return {"hot_channels": out}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/analysis/overview")
def api_analysis_overview():
    """ベンチマーク全体の重点指標サマリ。StatCard 用。"""
    bc = get_benchmark_config()
    config = get_dashboard_config()
    growth_url = (bc.get("spreadsheet_growth_tracking_url") or config.get("spreadsheet_growth_tracking_url", "")).strip()
    if not growth_url:
        return {"total_channels": 0, "top_growth": [], "top_recent_views": [], "updated_at": "", "configured": False}
    try:
        from app_sheets import fetch_csv, parse_growth_tracking, SheetFetchError
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    if not entries:
        return {"total_channels": 0, "top_growth": [], "top_recent_views": [], "updated_at": "", "configured": True}
    top_growth = sorted(entries, key=lambda e: e.growth_rate, reverse=True)[:3]
    top_recent = sorted(entries, key=lambda e: e.daily_view_change, reverse=True)[:3]
    updated = max((e.last_updated for e in entries if e.last_updated), default="")
    return {
        "total_channels": len(entries),
        "top_growth": [{"name": e.channel_name, "growth_rate": e.growth_rate,
                        "subscribers": e.subscribers} for e in top_growth],
        "top_recent_views": [{"name": e.channel_name, "daily_views": e.daily_view_change,
                              "subscribers": e.subscribers} for e in top_recent],
        "updated_at": updated,
        "configured": True,
    }


def _resolve_growth_url() -> str:
    """成長トラッキング（CHANNEL_TRACK + 個別 TRACK_ タブ）スプシ URL を解決。"""
    bc = get_benchmark_config()
    config = get_dashboard_config()
    return (bc.get("spreadsheet_growth_tracking_url")
            or config.get("spreadsheet_growth_tracking_url", "")).strip()


@app.get("/api/analysis/channel-list")
def api_channel_list():
    """CHANNEL_TRACK マスタの全チャンネル一覧（UI ピッカー用・15列フル）。

    各 entry に新着判定（is_new/days_tracked）を付与して返す。個別タブの時系列は
    重いので含めない（必要時に channel-timeline をオンデマンドで叩く）。
    """
    growth_url = _resolve_growth_url()
    if not growth_url:
        return {"configured": False, "channels": []}
    try:
        from app_sheets import (
            fetch_csv, parse_growth_tracking, detect_new_channels, SheetFetchError,
        )
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
        detect_new_channels(entries)  # is_new / days_tracked を副作用付与
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    chans = [{
        "name": e.channel_name,
        "subscribers": e.subscribers,
        "total_views": e.total_views,
        "daily_subs": e.daily_sub_change,
        "daily_views": e.daily_view_change,
        "growth_rate": e.growth_rate,
        "video_count": e.video_count,
        "last_post_date": e.last_post_date,
        "recent5_avg_views": e.recent5_avg_views,
        "weekly_growth_rate": e.weekly_growth_rate,
        "monthly_growth_rate": e.monthly_growth_rate,
        "tracking_start": e.tracking_start,
        "last_updated": e.last_updated,
        "is_new": e.is_new,
        "days_tracked": e.days_tracked,
    } for e in entries]
    chans.sort(key=lambda c: c["subscribers"], reverse=True)
    return {"configured": True, "count": len(chans), "channels": chans}


@app.get("/api/analysis/new-channels")
def api_new_channels(within_days: int = 14):
    """追跡開始が直近 within_days 日以内の新着チャンネル（日次登録増順）。"""
    growth_url = _resolve_growth_url()
    if not growth_url:
        return {"configured": False, "new_channels": []}
    try:
        from app_sheets import (
            fetch_csv, parse_growth_tracking, detect_new_channels, SheetFetchError,
        )
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
        new = detect_new_channels(entries, new_within_days=within_days)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"configured": True, "within_days": within_days, "count": len(new),
            "new_channels": [{
                "name": e.channel_name,
                "subscribers": e.subscribers,
                "daily_subs": e.daily_sub_change,
                "daily_views": e.daily_view_change,
                "tracking_start": e.tracking_start,
                "days_tracked": e.days_tracked,
                "last_post_date": e.last_post_date,
                "video_count": e.video_count,
            } for e in new]}


@app.get("/api/analysis/channel-timeline")
def api_channel_timeline(name: str, spreadsheet: str = ""):
    """個別 TRACK_<name> タブ（1日4回取得の時系列）をオンデマンド取得して指標化。

    spreadsheet 省略時は設定中の成長トラッキング URL を使用。個別タブはこのブックに
    のみ存在するため、旧スプシ設定のままだと「タブが見つかりません」になる（切替後に有効）。
    """
    from dataclasses import asdict
    src = (spreadsheet or _resolve_growth_url()).strip()
    if not src:
        raise HTTPException(400, "成長トラッキングシート URL が未設定です")
    if not name.strip():
        raise HTTPException(400, "チャンネル名を指定してください")
    try:
        from app_sheets import fetch_channel_timeline
        tl = fetch_channel_timeline(src, name.strip())
    except Exception as e:
        raise HTTPException(500, str(e))
    data = asdict(tl)
    if tl.error:
        # 404 ではなく 200 + error フィールドで返す（UI が個別にハンドリング）
        data["ok"] = False
    else:
        data["ok"] = True
    return data


@app.get("/api/analysis/tracking-events")
def api_tracking_events(record: int = 0):
    """日次スナップショット差分（新着ch / 新作投稿 / 急伸）を返す。

    record=1 のとき今日のスナップショットを保存してから差分を計算（日次更新フック用）。
    record=0（既定）は保存済みスナップショットを使った読み取りのみ（最新2件の差分）。
    """
    growth_url = _resolve_growth_url()
    if not growth_url:
        return {"configured": False, "events": None}
    try:
        from app_sheets import (
            fetch_csv, parse_growth_tracking, record_daily_snapshot,
            snapshot_growth, load_latest_snapshot, diff_snapshots, SheetFetchError,
        )
        try:
            rows = fetch_csv(growth_url)
        except SheetFetchError as e:
            raise HTTPException(400, str(e))
        entries = parse_growth_tracking(rows)
        if record:
            result = record_daily_snapshot(entries)
            events = result["events"]
            first_run = result["first_run"]
        else:
            # 保存せず、現在値 vs 直近保存スナップショットで差分のみ
            curr = snapshot_growth(entries)
            prev = load_latest_snapshot(before=curr["date"]) or load_latest_snapshot()
            if prev:
                events = diff_snapshots(prev, curr)
                first_run = False
            else:
                events = {"prev_date": None, "curr_date": curr["date"],
                          "new_channels": [], "dropped_channels": [],
                          "new_videos": [], "surging": []}
                first_run = True
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))
    return {"configured": True, "recorded": bool(record),
            "first_run": first_run, "events": events}


@app.get("/api/analysis/cache")
def api_analysis_cache():
    """キャッシュされた分析結果を返す"""
    try:
        import app_competitor as _ac
        data = _ac.load_cache()
        if not data:
            return {"cached": False}
        return {"cached": True, **data}
    except Exception:
        return {"cached": False}

@app.delete("/api/analysis/cache")
def api_delete_analysis_cache():
    """キャッシュ削除（再分析を強制するため。英語メタデータ生成向けの再生成等に使用）"""
    cache_file = SHARED_CONFIG_DIR / "competitor_analysis_cache.json"
    try:
        from app_channel_cache import delete_scoped_and_legacy
        return {"status": "ok", "deleted": delete_scoped_and_legacy("competitor_analysis_cache.json", cache_file)}
    except Exception:
        if cache_file.exists():
            try:
                cache_file.unlink()
                return {"status": "ok", "deleted": True}
            except Exception as e:
                raise HTTPException(500, f"削除失敗: {e}")
        return {"status": "ok", "deleted": False}


# ─── API: ベンチマーク・サムネイル軸（Phase 1） ───
# 既存 competitor_analysis_cache.json を入力に、サムネ画像を DL → Vision 分析 →
# picked リストを Flow / Image2 への参照画像として再利用する。

class BenchThumbRunRequest(BaseModel):
    per_channel_cap: Optional[int] = 8
    dl_only: Optional[bool] = False  # true なら分析スキップ
    channel_ids: Optional[List[str]] = None
    skip_unchanged: Optional[bool] = True

class BenchThumbPickedUpdate(BaseModel):
    picked: List[str]  # videoId のリスト

import threading as _bt_threading

_bt_status: dict = {"running": False, "phase": "", "msg": "", "started_at": "", "finished_at": ""}

def _bt_set_status(**kw):
    _bt_status.update(kw)

def _bt_now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")

@app.get("/api/benchmark/thumbnail")
def api_benchmark_thumbnail_get():
    """サムネ分析の現在のキャッシュを返す（画像本体は別エンドポイント）。"""
    import app_benchmark_thumbnail as _bt
    cache = _bt.load_cache()
    return {
        "status": "ok",
        "running": _bt_status.get("running", False),
        "phase": _bt_status.get("phase", ""),
        "msg": _bt_status.get("msg", ""),
        "generated_at": cache.get("generated_at", ""),
        "channels": cache.get("channels", []),
        "analysis": cache.get("analysis", {}),
        "picked": cache.get("picked", []),
    }

@app.get("/api/benchmark/thumbnail/image/{channel_id}/{video_id}")
def api_benchmark_thumbnail_image(channel_id: str, video_id: str):
    """ローカル保存したサムネ画像を返す（UI のグリッド表示用）。"""
    import app_benchmark_thumbnail as _bt
    safe_ch = "".join(c if c.isalnum() or c in "-_" else "_" for c in channel_id)[:64]
    safe_vid = "".join(c if c.isalnum() or c in "-_" else "_" for c in video_id)[:32]
    fp = _bt.THUMBS_DIR / safe_ch / f"{safe_vid}.jpg"
    if not fp.exists():
        raise HTTPException(404, "thumbnail not found")
    return FileResponse(fp, media_type="image/jpeg")

@app.post("/api/benchmark/thumbnail/run")
def api_benchmark_thumbnail_run(req: BenchThumbRunRequest = BenchThumbRunRequest()):
    """サムネ DL + Vision 分析を非同期で開始。結果は GET /api/benchmark/thumbnail で取得。"""
    if _bt_status.get("running"):
        return {"status": "already_running", **_bt_status}

    import app_benchmark_thumbnail as _bt
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    def _worker():
        _bt_set_status(running=True, phase="downloading", msg="サムネを DL 中...",
                       started_at=_bt_now_iso(), finished_at="")
        try:
            channels = _bt.download_thumbnails()
            if not channels:
                raise RuntimeError("DL 対象がありません。先に競合データ取得を実行してください。")
            cache = _bt.load_cache()
            cache["channels"] = channels
            cache["generated_at"] = _bt_now_iso()
            cache["picked"] = _bt._filter_valid_picks(cache.get("picked", []), channels)
            _bt.save_cache(cache)

            if req.dl_only:
                _bt_set_status(running=False, phase="done", msg=f"DL 完了 ({sum(len(c.get('thumbnails',[])) for c in channels)} 枚)",
                               finished_at=_bt_now_iso())
                return

            _bt_set_status(phase="analyzing", msg="Claude Vision で分析中...")
            _existing_pc = (cache.get("analysis") or {}).get("per_channel", {}) or {}
            analysis = _bt.analyze_channels(channels, cli_cmd=cli_cmd,
                                            per_channel_cap=req.per_channel_cap or 8,
                                            only_channel_ids=req.channel_ids,
                                            skip_unchanged=True if req.skip_unchanged is None else bool(req.skip_unchanged),
                                            existing_per_channel=_existing_pc)
            cache["analysis"] = analysis
            _bt.save_cache(cache)
            _bt_set_status(running=False, phase="done", msg="分析完了", finished_at=_bt_now_iso())
        except Exception as e:
            _bt_set_status(running=False, phase="error", msg=str(e)[:300], finished_at=_bt_now_iso())

    t = _bt_threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"status": "started"}

@app.put("/api/benchmark/thumbnail/picked")
def api_benchmark_thumbnail_set_picked(req: BenchThumbPickedUpdate):
    import app_benchmark_thumbnail as _bt
    cleaned = _bt.set_picked(req.picked or [])
    return {"status": "ok", "picked": cleaned, "count": len(cleaned)}

@app.get("/api/benchmark/thumbnail/picked")
def api_benchmark_thumbnail_get_picked():
    import app_benchmark_thumbnail as _bt
    return {"status": "ok", "details": _bt.get_picked_details()}


# ─── API: ベンチマーク・コンセプト軸（Phase 2） ───
# テキスト解析中心（タイトル / タグ / desc）→ themes / emotional_jobs / scene_anchors

class BenchConceptRunRequest(BaseModel):
    per_channel_cap: Optional[int] = 8
    channel_ids: Optional[List[str]] = None  # 選択ch名/id 限定（未指定=全件）
    skip_unchanged: Optional[bool] = True    # 入力指紋が一致なら再分析せず流用

_bc_status: dict = {"running": False, "phase": "", "msg": "", "started_at": "", "finished_at": ""}

def _bc_set_status(**kw):
    _bc_status.update(kw)

@app.get("/api/benchmark/concept")
def api_benchmark_concept_get():
    """コンセプト軸の現在のキャッシュを返す。"""
    import app_benchmark_concept as _bc
    cache = _bc.load_cache()
    return {
        "status": "ok",
        "running": _bc_status.get("running", False),
        "phase": _bc_status.get("phase", ""),
        "msg": _bc_status.get("msg", ""),
        "generated_at": cache.get("generated_at", ""),
        "per_channel": cache.get("per_channel", {}),
        "aggregate": cache.get("aggregate", {}),
    }

@app.post("/api/benchmark/concept/run")
def api_benchmark_concept_run(req: BenchConceptRunRequest = BenchConceptRunRequest()):
    """コンセプト分析を非同期で開始。"""
    if _bc_status.get("running"):
        return {"status": "already_running", **_bc_status}

    import app_benchmark_concept as _bc
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cfg = get_dashboard_config()
    persona = cfg.get("persona", "")

    def _worker():
        _bc_set_status(running=True, phase="analyzing",
                       msg="チャンネル別 + 横断分析を実行中...",
                       started_at=_bt_now_iso(), finished_at="")
        try:
            _bc.run_full(cli_cmd=cli_cmd,
                         per_channel_cap=req.per_channel_cap or 8,
                         self_persona=persona,
                         only_channel_ids=req.channel_ids,
                         skip_unchanged=True if req.skip_unchanged is None else bool(req.skip_unchanged))
            _bc_set_status(running=False, phase="done", msg="完了",
                           finished_at=_bt_now_iso())
        except Exception as e:
            _bc_set_status(running=False, phase="error",
                           msg=str(e)[:300], finished_at=_bt_now_iso())

    t = _bt_threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"status": "started"}


# ─── API: ベンチマーク・タイトル軸（Phase 3） ───
# タイトル群 → patterns / formulas / keywords / hooks / scaffolds

class BenchTitleRunRequest(BaseModel):
    per_channel_cap: Optional[int] = 10
    channel_ids: Optional[List[str]] = None
    skip_unchanged: Optional[bool] = True

_btitle_status: dict = {"running": False, "phase": "", "msg": "", "started_at": "", "finished_at": ""}

def _btitle_set_status(**kw):
    _btitle_status.update(kw)

@app.get("/api/benchmark/title")
def api_benchmark_title_get():
    import app_benchmark_title as _btm
    cache = _btm.load_cache()
    return {
        "status": "ok",
        "running": _btitle_status.get("running", False),
        "phase": _btitle_status.get("phase", ""),
        "msg": _btitle_status.get("msg", ""),
        "generated_at": cache.get("generated_at", ""),
        "per_channel": cache.get("per_channel", {}),
        "aggregate": cache.get("aggregate", {}),
    }

@app.post("/api/benchmark/title/run")
def api_benchmark_title_run(req: BenchTitleRunRequest = BenchTitleRunRequest()):
    if _btitle_status.get("running"):
        return {"status": "already_running", **_btitle_status}

    import app_benchmark_title as _btm
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cfg = get_dashboard_config()
    persona = cfg.get("persona", "")

    def _worker():
        _btitle_set_status(running=True, phase="analyzing",
                           msg="タイトル分析を実行中...",
                           started_at=_bt_now_iso(), finished_at="")
        try:
            _btm.run_full(cli_cmd=cli_cmd,
                          per_channel_cap=req.per_channel_cap or 10,
                          self_persona=persona,
                          only_channel_ids=req.channel_ids,
                          skip_unchanged=True if req.skip_unchanged is None else bool(req.skip_unchanged))
            _btitle_set_status(running=False, phase="done", msg="完了",
                               finished_at=_bt_now_iso())
        except Exception as e:
            _btitle_set_status(running=False, phase="error",
                               msg=str(e)[:300], finished_at=_bt_now_iso())

    t = _bt_threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"status": "started"}


# ─── API: ベンチマーク・投稿文（説明文）軸 ───
# 指定チャンネルの説明文構成 → per_channel + aggregate(recommendation_for_self)
# (a)既存メタ改善 と (b)新規投稿文構成案 へ還流。説明文は API rivals 経路のみ取得可。

class BenchDescriptionRunRequest(BaseModel):
    per_channel_cap: Optional[int] = 8
    refetch_full: Optional[bool] = True  # 軸 run 時に full 説明文を再取得（500字版の欠落を補う）
    channel_ids: Optional[List[str]] = None
    skip_unchanged: Optional[bool] = True

_bdesc_status: dict = {"running": False, "phase": "", "msg": "", "started_at": "", "finished_at": ""}

def _bdesc_set_status(**kw):
    _bdesc_status.update(kw)

@app.get("/api/benchmark/description")
def api_benchmark_description_get():
    import app_benchmark_description as _bdm
    cache = _bdm.load_cache()
    return {
        "status": "ok",
        "running": _bdesc_status.get("running", False),
        "phase": _bdesc_status.get("phase", ""),
        "msg": _bdesc_status.get("msg", ""),
        "generated_at": cache.get("generated_at", ""),
        "no_data": cache.get("no_data", False),
        "reason": cache.get("reason", ""),
        "source": cache.get("source", ""),
        "per_channel": cache.get("per_channel", {}),
        "aggregate": cache.get("aggregate", {}),
    }

@app.post("/api/benchmark/description/run")
def api_benchmark_description_run(req: BenchDescriptionRunRequest = BenchDescriptionRunRequest()):
    if _bdesc_status.get("running"):
        return {"status": "already_running", **_bdesc_status}

    import app_benchmark_description as _bdm
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    cfg = get_dashboard_config()
    persona = cfg.get("persona", "")
    refetch_full = True if req.refetch_full is None else bool(req.refetch_full)

    def _worker():
        _bdesc_set_status(running=True, phase="analyzing",
                          msg=("full 説明文を再取得して分析中..." if refetch_full else "投稿文構成を分析中..."),
                          started_at=_bt_now_iso(), finished_at="")
        try:
            result = _bdm.run_full(cli_cmd=cli_cmd,
                                   per_channel_cap=req.per_channel_cap or 8,
                                   self_persona=persona,
                                   refetch_full=refetch_full,
                                   only_channel_ids=req.channel_ids,
                                   skip_unchanged=True if req.skip_unchanged is None else bool(req.skip_unchanged))
            if result.get("no_data"):
                _bdesc_set_status(running=False, phase="done",
                                  msg=(result.get("reason") or "説明文データなし"),
                                  finished_at=_bt_now_iso())
            else:
                _bdesc_set_status(running=False, phase="done", msg="完了",
                                  finished_at=_bt_now_iso())
        except Exception as e:
            _bdesc_set_status(running=False, phase="error",
                              msg=str(e)[:300], finished_at=_bt_now_iso())

    t = _bt_threading.Thread(target=_worker, daemon=True)
    t.start()
    return {"status": "started"}


# ─── API: ベンチマーク詳細表示用（Sprint 5-A） ───
# Sheet1 を読み込み、ピン留め or hot 上位の TOP/最新動画 + メタ集計を返す

def _benchmark_target_channels():
    """benchmark_config.pinned_names + 必要なら hot ranking から対象チャンネル名を返す。"""
    bc = get_benchmark_config()
    pinned = list(bc.get("pinned_names") or [])
    if pinned:
        return pinned[:8]
    # Pin 無ければ hot top 5 をフォールバック
    try:
        hot = api_hot_channels(top_n=5)
        return [h["name"] for h in hot.get("hot_channels", [])][:5]
    except Exception:
        return []

def _load_sheet1_channels():
    """Sheet1（channel_detail）を読み込んで {title: ChannelDetail} の dict を返す。"""
    bc = get_benchmark_config()
    config = get_dashboard_config()
    detail_url = (bc.get("spreadsheet_channel_detail_url") or config.get("spreadsheet_channel_detail_url", "")).strip()
    if not detail_url:
        return {}
    from app_sheets import fetch_csv, parse_channel_detail
    try:
        rows = fetch_csv(detail_url)
        details = parse_channel_detail(rows)
        return {ch.title: ch for ch in details}
    except Exception:
        return {}

@app.get("/api/analysis/benchmark-videos")
def api_benchmark_videos():
    """適用中のベンチマーク（pinned or hot top）の TOP 動画 + 最新動画を返す。動画詳細サイドパネル用。"""
    targets = _benchmark_target_channels()
    if not targets:
        return {"channels": [], "hint": "ピン留めも hot ランキングも空です。設定タブでベンチマーク対象を指定してください"}
    detail_by_name = _load_sheet1_channels()
    out = []
    for name in targets:
        ch = detail_by_name.get(name)
        if not ch:
            # fuzzy match
            from app_sheets import match_channels
            ch = match_channels(name, list(detail_by_name.values()))
        if not ch:
            out.append({"name": name, "found": False})
            continue
        # TOP 動画 3 件 + 最新 3 件
        top_v = [{"title": v.title, "thumb_url": v.thumbnail, "url": v.url,
                  "published_at": v.publish_date, "views": v.view_count,
                  "likes": v.like_count, "comments": v.comment_count}
                 for v in (ch.top_videos or [])[:3]]
        recent_v = [{"title": v.title, "thumb_url": v.thumbnail, "url": v.url,
                     "published_at": v.publish_date, "views": v.view_count}
                    for v in (ch.recent_videos or [])[:3]]
        out.append({
            "name": ch.title,
            "url": ch.url,
            "subscribers": ch.subscribers,
            "video_count": ch.video_count,
            "description": (ch.description or "")[:300],
            "top_videos": top_v,
            "recent_videos": recent_v,
            "found": True,
        })
    return {"channels": out, "targets": targets}

@app.get("/api/analysis/posting-times")
def api_posting_times():
    """ベンチマーク先 TOP 動画の publishedAt を曜日×時刻バケットに集計。
    返却: {heatmap: [[count_by_hour]*7], total_count, weekday_labels, hour_labels}
    publish_datetime が無い行はスキップ。
    """
    targets = _benchmark_target_channels()
    detail_by_name = _load_sheet1_channels()
    heatmap = [[0] * 24 for _ in range(7)]  # 7 weekdays × 24 hours
    weekday_labels = ["月", "火", "水", "木", "金", "土", "日"]
    total = 0
    for name in targets:
        ch = detail_by_name.get(name)
        if not ch:
            from app_sheets import match_channels
            ch = match_channels(name, list(detail_by_name.values()))
        if not ch:
            continue
        for v in (ch.top_videos or []) + (ch.recent_videos or []):
            dt_str = (v.publish_datetime or v.publish_date or "").strip()
            if not dt_str:
                continue
            # 想定フォーマット: "2026/04/13 20:30" or "2026-04-13T20:30" など
            try:
                dt_norm = dt_str.replace("/", "-").replace("T", " ")
                # 時刻が無い場合は 12:00 と仮定
                if " " not in dt_norm:
                    dt_norm += " 12:00"
                dt = datetime.fromisoformat(dt_norm[:16])
                wd = dt.weekday()  # 0=月
                hr = dt.hour
                heatmap[wd][hr] += 1
                total += 1
            except Exception:
                continue
    return {
        "heatmap": heatmap,
        "total_count": total,
        "weekday_labels": weekday_labels,
        "hour_labels": [f"{h:02d}" for h in range(24)],
    }

@app.get("/api/analysis/tag-frequency")
def api_tag_frequency(top_n: int = 20):
    """Sheet1 から tags 取得は無いため、TOP 動画タイトルから単語頻度を集計（簡易）。
    将来的に YouTube Data API で snippet.tags を取れば真のタグ集計に拡張可能。
    """
    import collections
    targets = _benchmark_target_channels()
    detail_by_name = _load_sheet1_channels()
    counter = collections.Counter()
    for name in targets:
        ch = detail_by_name.get(name)
        if not ch:
            from app_sheets import match_channels
            ch = match_channels(name, list(detail_by_name.values()))
        if not ch:
            continue
        for v in (ch.top_videos or []):
            words = re.findall(r"[A-Za-z][A-Za-z'\-]{2,}|[ぁ-んァ-ン一-龥ー]{2,}", v.title or "")
            for w in words:
                w = w.lower() if w[0].isascii() else w
                # 一般的すぎる語を除外
                if w in {"the", "and", "for", "with", "music", "bgm", "vol", "playlist",
                         "vibes", "mix", "songs", "song", "edit"}: continue
                counter[w] += 1
    top = counter.most_common(top_n)
    return {"tags": [{"word": w, "count": c} for w, c in top], "source": "TOP動画タイトル単語集計（簡易）"}


# ─── API: 徹底パクリ進化（Sprint 5-A） ───

@app.post("/api/videos/{video_name}/suggest-imitate-evolve")
def api_suggest_imitate_evolve(video_name: str):
    """ベンチマーク先動画 + 自チャンネルペルソナ から「✓パクる / ✗避ける / +進化させる」3 軸を提案。
    master_prompts.imitate_evolve で上書き可能。
    """
    config = get_dashboard_config()
    persona = config.get("persona", "").strip()
    if not persona:
        raise HTTPException(400, "ペルソナ未設定（基本設定で入力してください）")
    bench = api_benchmark_videos().get("channels", [])
    if not bench:
        raise HTTPException(400, "ベンチマーク対象が空。設定タブでピン留めしてください")
    # ベンチマーク先 TOP 動画タイトル一覧
    bench_summary = []
    for ch in bench[:5]:
        if not ch.get("found"): continue
        bench_summary.append(f"\n=== {ch['name']} (登録 {ch.get('subscribers',0):,}) ===")
        for v in ch.get("top_videos", [])[:3]:
            bench_summary.append(f"  TOP: {v.get('views',0):,}回 | {v['title']} | tags=- | {v.get('published_at','')}")
        for v in ch.get("recent_videos", [])[:2]:
            bench_summary.append(f"  RECENT: {v.get('views',0):,}回 | {v['title']} | {v.get('published_at','')}")
    bench_text = "\n".join(bench_summary) or "(no data)"
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    default_prompt = f"""あなたは YouTube BGM チャンネルの戦略コンサルタントです。
オーナーは、ベンチマークチャンネルから実証済みの勝ちパターン（原則）を抽出し、それを自チャンネル独自のアイデンティティへと進化させたいと考えています。素材・タイトル・名称・ブランド固有の表現をそのまま流用してはいけません。

=== 自チャンネルのペルソナ ===
{persona[:2000]}

=== ベンチマーク動画（人気動画 + 最近の投稿）===
{bench_text}

=== タスク ===
視聴者ニーズの観点から、具体的な提案をすべて日本語で記述してください。

"imitate"（パクる要素）: 視聴者を明確に惹きつけている、実証済みの構成・タイトルパターン・サムネイルの原則・投稿時刻・感情的な訴求。
"avoid"（避ける要素）: 使い古されたパターン、自ペルソナと噛み合わないもの、視聴者の飽きを招くリスク、直接的な模倣に近すぎるもの。
"evolve"（進化させる要素）: 自ペルソナの強みを活かして、より深い未充足ニーズに応え、「まさにこれだ」と思わせる体験を生み出す方法。

次の単一の JSON オブジェクトのみで回答してください:
{{
  "imitate": ["具体的な施策1", "具体的な施策2", ...],
  "avoid": ["具体的なリスク1", "具体的なリスク2", ...],
  "evolve": ["具体的な進化案1", "具体的な進化案2", ...],
  "summary": "<戦略を1〜2文で要約した日本語>"
}}

JSON 以外は一切出力しないこと。すべての値は日本語で記述すること。"""

    prompt = (get_master_prompts().get("imitate_evolve") or "").strip() or default_prompt
    try:
        from app_llm_runner import run_llm, LLMError
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="imitate-evolve")
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:300]}")
    m = re.search(r"\{[\s\S]*\}", out or "")
    if not m:
        raise HTTPException(500, f"JSON 抽出失敗: {(out or '')[:300]}")
    try:
        obj = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(500, f"JSON パース失敗: {e}")
    return obj


@app.post("/api/videos/{video_name}/suggest-with-analysis")
def api_suggest_with_analysis(video_name: str):
    """競合分析を踏まえたタイトル・説明・タグ提案"""
    try:
        import app_competitor as _ac
        cache = _ac.load_cache()
    except Exception:
        raise HTTPException(500, "キャッシュ読み込み失敗")
    if not cache:
        raise HTTPException(400, "先に競合分析を実行してください（📊 競合分析ボタン）")

    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    persona = config.get("persona", "")

    # コンテキスト
    songs = []
    music_dir = folder / "music"
    if music_dir.is_dir():
        songs = [p.stem for p in sorted(music_dir.glob("*.mp3")) if not _is_deleted_track_name(p.name)]
    title_file = folder / "youtube_title.txt"
    current_title = title_file.read_text(encoding="utf-8").strip() if title_file.exists() else ""

    from app_competitor import propose_with_analysis
    try:
        result = propose_with_analysis(
            cache.get("analysis", {}), cache.get("competitor_data", {}),
            cli_cmd=cli_cmd, current_title=current_title,
            songs=songs, persona=persona,
            growth_summary=cache.get("growth_summary", {}),
        )
        return {"status": "ok", **result}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


def _load_analysis_cache_or_409():
    """競合分析キャッシュを読み込み、未実行なら 400、music/visual_direction 不足なら 409"""
    try:
        import app_competitor as _ac
        cache = _ac.load_cache()
    except Exception:
        raise HTTPException(500, "キャッシュ読み込み失敗")
    if not cache:
        raise HTTPException(400, "先に競合分析を実行してください（📡 競合分析ボタン）")
    return cache


def _video_context(video_name: str):
    """動画フォルダから current_title / songs / persona を取り出す共通処理"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    if not folder.exists():
        raise HTTPException(404)
    songs = []
    music_dir = folder / "music"
    if music_dir.is_dir():
        songs = [p.stem for p in sorted(music_dir.glob("*.mp3")) if not _is_deleted_track_name(p.name)]
    title_file = folder / "youtube_title.txt"
    current_title = title_file.read_text(encoding="utf-8").strip() if title_file.exists() else ""
    persona = config.get("persona", "")
    return folder, current_title, songs, persona


class SuggestFlowPromptRequest(BaseModel):
    context_hint: str = ""


@app.post("/api/videos/{video_name}/suggest-suno-prompt")
def api_suggest_suno_prompt(video_name: str):
    """競合分析の music_direction から SUNO プロンプト案を生成"""
    cache = _load_analysis_cache_or_409()
    analysis = cache.get("analysis", {})
    if not analysis.get("music_direction"):
        raise HTTPException(409, detail={"error": "analysis_outdated", "hint": "competitor analysis を再実行してください（music_direction が未生成）"})

    _, current_title, _, _ = _video_context(video_name)
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    existing_prompt = suno_cfg.get("prompt", "") or ""

    from app_competitor import propose_suno_prompt
    try:
        result = propose_suno_prompt(
            analysis, current_title=current_title,
            existing_prompt=existing_prompt, cli_cmd=cli_cmd,
        )
        return {"status": "ok", **result}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/api/videos/{video_name}/suggest-flow-prompt")
def api_suggest_flow_prompt(video_name: str, req: SuggestFlowPromptRequest):
    """競合分析の visual_direction から Flow プロンプト案を生成"""
    cache = _load_analysis_cache_or_409()
    analysis = cache.get("analysis", {})
    if not analysis.get("visual_direction"):
        raise HTTPException(409, detail={"error": "analysis_outdated", "hint": "competitor analysis を再実行してください（visual_direction が未生成）"})

    _, current_title, _, _ = _video_context(video_name)
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    from app_competitor import propose_flow_prompt
    try:
        result = propose_flow_prompt(
            analysis, current_title=current_title,
            context_hint=req.context_hint or "", cli_cmd=cli_cmd,
        )
        return {"status": "ok", **result}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


@app.post("/api/videos/{video_name}/suggest-all")
def api_suggest_all(video_name: str):
    """楽曲・サムネ・メタ の 3 提案を一気通貫で生成（同期・60-90秒想定）"""
    cache = _load_analysis_cache_or_409()
    analysis = cache.get("analysis", {})
    competitor_data = cache.get("competitor_data", {})
    if not analysis.get("music_direction") or not analysis.get("visual_direction"):
        raise HTTPException(409, detail={"error": "analysis_outdated", "hint": "competitor analysis を再実行してください（music/visual_direction が未生成）"})

    _, current_title, songs, persona = _video_context(video_name)
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    existing_prompt = suno_cfg.get("prompt", "") or ""

    from app_competitor import propose_with_analysis, propose_suno_prompt, propose_flow_prompt
    errors = {}
    result = {"meta": None, "suno": None, "flow": None}

    try:
        result["suno"] = propose_suno_prompt(
            analysis, current_title=current_title,
            existing_prompt=existing_prompt, cli_cmd=cli_cmd,
        )
    except RuntimeError as e:
        errors["suno"] = str(e)

    try:
        result["flow"] = propose_flow_prompt(
            analysis, current_title=current_title,
            context_hint="", cli_cmd=cli_cmd,
        )
    except RuntimeError as e:
        errors["flow"] = str(e)

    try:
        result["meta"] = propose_with_analysis(
            analysis, competitor_data,
            cli_cmd=cli_cmd, current_title=current_title,
            songs=songs, persona=persona,
            growth_summary=cache.get("growth_summary", {}),
        )
    except RuntimeError as e:
        errors["meta"] = str(e)

    if errors and not any(result.values()):
        raise HTTPException(500, detail={"errors": errors})
    return {"status": "ok" if not errors else "partial", "errors": errors, **result}


# ─── API: ベンチマーク管理（ピン留め + フィルタ） ───

class BenchmarkConfigUpdate(BaseModel):
    pinned_names: Optional[List[str]] = None
    top_n: Optional[int] = None
    min_subs: Optional[int] = None
    max_subs: Optional[int] = None
    exclude_names: Optional[List[str]] = None


@app.get("/api/analysis/benchmark-config")
def api_benchmark_config_get():
    """現在のベンチマークピン留め設定とフィルタを返す"""
    cfg = get_benchmark_config()
    flt = cfg.get("filter", {}) or {}
    return {
        "pinned_names": cfg.get("pinned_names", []) or [],
        "filter": {
            "top_n": int(flt.get("top_n", 15)),
            "min_subs": int(flt.get("min_subs", 0)),
            "max_subs": flt.get("max_subs"),
            "exclude_names": flt.get("exclude_names", []) or [],
        },
    }


@app.put("/api/analysis/benchmark-config")
def api_benchmark_config_put(req: BenchmarkConfigUpdate):
    """ベンチマークピン留め + フィルタを保存"""
    cfg = get_benchmark_config()
    if req.pinned_names is not None:
        cfg["pinned_names"] = [n.strip() for n in req.pinned_names if n and n.strip()][:20]
    flt = cfg.get("filter", {}) or {}
    if req.top_n is not None:
        flt["top_n"] = max(1, min(50, int(req.top_n)))
    if req.min_subs is not None:
        flt["min_subs"] = max(0, int(req.min_subs))
    if req.max_subs is not None:
        flt["max_subs"] = int(req.max_subs) if req.max_subs > 0 else None
    if req.exclude_names is not None:
        flt["exclude_names"] = [n.strip() for n in req.exclude_names if n and n.strip()]
    cfg["filter"] = flt
    save_benchmark_config(cfg)
    return {"status": "ok", "pinned_names": cfg.get("pinned_names", []), "filter": flt}


@app.get("/api/analysis/cache-info")
def api_analysis_cache_info():
    """キャッシュの鮮度情報（最終分析日時・経過日数・ソース）を返す"""
    try:
        import app_competitor as _ac
        data = _ac.load_cache()
        if not data:
            return {"cached": False, "age_days": None, "analyzed_at": None, "source": None}
        analyzed_at = data.get("analyzed_at")
        age_days = None
        if analyzed_at:
            import datetime as _dt
            try:
                t = _dt.datetime.fromisoformat(analyzed_at)
                age_days = (_dt.datetime.now() - t).days
            except Exception:
                pass
        analysis = data.get("analysis", {})
        return {
            "cached": True,
            "analyzed_at": analyzed_at,
            "age_days": age_days,
            "source": data.get("source"),
            "channel_count": len((data.get("competitor_data") or {}).get("channels", [])),
            "has_music_direction": bool(analysis.get("music_direction")),
            "has_visual_direction": bool(analysis.get("visual_direction")),
            "stale": (age_days is not None and age_days >= 7),
            "schema_outdated": not (analysis.get("music_direction") and analysis.get("visual_direction")),
        }
    except Exception:
        return {"cached": False, "age_days": None}


# ─── API: サムネ要素分析（Vision） ───

class ThumbnailAnalysisRequest(BaseModel):
    urls: Optional[List[str]] = None       # 画像URL 一覧
    uploaded_paths: Optional[List[str]] = None  # サーバ側に保存済みの画像パス（POST /api/analysis/thumbnails-upload 経由）
    context_hint: Optional[str] = None


@app.post("/api/analysis/thumbnails-upload")
async def api_thumbnails_upload(files: List[UploadFile] = File(...)):
    """画像をサーバ一時領域に保存し、分析関数に渡せるパス一覧を返す"""
    import tempfile
    dst_dir = Path(tempfile.mkdtemp(prefix="orzz_thumb_upload_"))
    saved = []
    for f in files:
        ext = Path(f.filename or "img").suffix.lower() or ".jpg"
        if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
            ext = ".jpg"
        dst = dst_dir / f"{len(saved):02d}{ext}"
        content = await f.read()
        dst.write_bytes(content)
        saved.append(str(dst))
    return {"paths": saved}


@app.post("/api/analysis/thumbnails")
def api_analyze_thumbnails(req: ThumbnailAnalysisRequest):
    """競合サムネ画像を Claude Vision で要素抽出。**コピー生成ではなく要素抽出→自チャンネルへの落とし込み**が目的。"""
    from app_competitor import analyze_thumbnail_elements
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"
    try:
        result = analyze_thumbnail_elements(
            image_paths=req.uploaded_paths or [],
            url_list=req.urls or [],
            context_hint=req.context_hint or "",
            cli_cmd=cli_cmd,
        )
        # キャッシュに統合（上書き。手動トリガのため頻度は低い想定）
        try:
            import app_competitor as _ac
            cache = _ac.load_cache() or {}
            if cache:
                if "analysis" not in cache:
                    cache["analysis"] = {}
                cache["analysis"]["thumbnail_elements"] = result
                import datetime as _dt
                cache["thumbnail_analyzed_at"] = _dt.datetime.now().isoformat()
                from app_channel_cache import save_scoped_cache
                save_scoped_cache("competitor_analysis_cache.json", SHARED_CONFIG_DIR / "competitor_analysis_cache.json", cache)
        except Exception:
            pass
        return {"status": "ok", **result}
    except RuntimeError as e:
        msg = str(e)
        if "解析対象の画像がありません" in msg:
            raise HTTPException(400, msg)
        raise HTTPException(500, msg)


# ─── API: Sheets URL 取り込み ───

class SheetImportRequest(BaseModel):
    sheet_url: str


@app.post("/api/analysis/sheets-import")
def api_sheets_import(req: SheetImportRequest):
    """Google Sheets URL から全シート/全タブを読み取り、ベンチマーク候補として返す。"""
    from app_competitor import import_benchmark_from_sheet
    try:
        result = import_benchmark_from_sheet(req.sheet_url)
        return {"status": "ok", **result}
    except RuntimeError as e:
        raise HTTPException(500, str(e))


# ─── API: ベンチマーク完成パイプライン（全自動） ───

BENCHMARK_PROFILES_FILE = SHARED_CONFIG_DIR / "benchmark_profiles.json"  # PC間共有

class BenchmarkRunRequest(BaseModel):
    sheet_a_url: Optional[str] = None   # 未指定時は dashboard_config から
    sheet_b_url: Optional[str] = None
    extra_urls: Optional[List[str]] = None
    channel_filter: Optional[List[str]] = None   # 指定時は選択チャンネルだけ分析
    skip_existing: Optional[bool] = True   # 既存の有効プロファイルは再生成しない
    force: Optional[bool] = False          # True で既存無視の強制再生成
    max_age_days: Optional[int] = None     # スキップの鮮度しきい値（None=常にスキップ）
    dry_run: Optional[bool] = False        # 生成せず内訳見積りのみ返す


class BenchmarkSourcesRequest(BaseModel):
    sheet_a_url: Optional[str] = None
    sheet_b_url: Optional[str] = None
    extra_urls: Optional[List[str]] = None


@app.post("/api/benchmark/preview-sources")
def api_benchmark_preview_sources(req: BenchmarkSourcesRequest):
    """Sheet A/B + extra_urls のチャンネル一覧だけを取得（Claude 呼ばず軽量）。
    ユーザーが分析対象を選択する前に表示するプレビュー用。"""
    cfg = get_dashboard_config()
    sheet_a = (req.sheet_a_url or cfg.get("spreadsheet_channel_detail_url") or "").strip()
    sheet_b = (req.sheet_b_url or cfg.get("spreadsheet_growth_tracking_url") or "").strip()
    extras = [u.strip() for u in (req.extra_urls or []) if u.strip()]
    if not sheet_a and not extras:
        raise HTTPException(400, "Sheet A URL またはリスト外 URL が最低 1 つ必要です")
    try:
        from app_competitor import list_benchmark_sources
        result = list_benchmark_sources(sheet_a_url=sheet_a, sheet_b_url=sheet_b, extra_urls=extras)
    except Exception as e:
        raise HTTPException(500, f"プレビュー失敗: {e}")
    return {
        "status": "ok",
        "sheet_a": sheet_a,
        "sheet_b": sheet_b,
        "extras": extras,
        "channels": result.get("channels") or [],
    }


@app.post("/api/benchmark/run-full")
async def api_benchmark_run_full(req: BenchmarkRunRequest):
    """ベンチマーク完成パイプラインを起動（バックグラウンド）。
    Sheet A/B + リスト外 URL → コメント取得 → Claude プロファイル生成 → 保存。
    channel_filter 指定時は選択チャンネルだけ分析。
    進捗は /api/benchmark/status で取得可能。
    """
    cfg = get_dashboard_config()
    sheet_a = (req.sheet_a_url or cfg.get("spreadsheet_channel_detail_url") or "").strip()
    sheet_b = (req.sheet_b_url or cfg.get("spreadsheet_growth_tracking_url") or "").strip()
    extras = [u.strip() for u in (req.extra_urls or []) if u.strip()]
    channel_filter = [n.strip() for n in (req.channel_filter or []) if n and n.strip()]
    if not sheet_a and not extras:
        raise HTTPException(400, "Sheet A URL またはリスト外 URL が最低 1 つ必要です")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    # dry_run（S3 コスト見積り）: 実行中でも即時に内訳だけ返す（生成しない＝排他不要）
    if req.dry_run:
        try:
            from app_competitor import run_full_benchmark
            return run_full_benchmark(
                sheet_a_url=sheet_a, sheet_b_url=sheet_b,
                extra_urls=extras, cli_cmd=cli_cmd,
                channel_filter=channel_filter or None,
                skip_existing=True if req.skip_existing is None else bool(req.skip_existing),
                force=bool(req.force), max_age_days=req.max_age_days, dry_run=True,
            )
        except Exception as e:
            raise HTTPException(500, f"見積り失敗: {e}")

    await _ensure_not_running("benchmark", "プロファイル生成が既に実行中です")

    task_logs["benchmark"] = []
    import datetime as _dt
    task_meta["benchmark"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "sheet_a": sheet_a,
        "sheet_b": sheet_b,
        "extra_urls": extras,
        "channel_filter": channel_filter,
        "status": "running",
    }

    def _log(msg: str):
        task_logs["benchmark"].append(msg)
        if len(task_logs["benchmark"]) > 500:
            task_logs["benchmark"] = task_logs["benchmark"][-500:]

    def _worker():
        try:
            from app_competitor import run_full_benchmark
            run_full_benchmark(
                sheet_a_url=sheet_a, sheet_b_url=sheet_b,
                extra_urls=extras, cli_cmd=cli_cmd, progress_cb=_log,
                channel_filter=channel_filter or None,
                skip_existing=True if req.skip_existing is None else bool(req.skip_existing),
                force=bool(req.force), max_age_days=req.max_age_days,
            )
            task_meta["benchmark"]["status"] = "done"
            task_meta["benchmark"]["completed_at"] = _dt.datetime.now().isoformat()
        except Exception as e:
            _log(f"❌ エラー: {e}")
            task_meta["benchmark"]["status"] = "failed"
            task_meta["benchmark"]["error"] = str(e)
        finally:
            active_tasks.pop("benchmark", None)

    import threading
    t = threading.Thread(target=_worker, daemon=True)
    active_tasks["benchmark"] = t
    t.start()
    return {"status": "started", "sheet_a": sheet_a, "sheet_b": sheet_b, "extras": extras}


@app.get("/api/benchmark/status")
def api_benchmark_status():
    t = active_tasks.get("benchmark")
    running = t is not None and (hasattr(t, "is_alive") and t.is_alive())
    return {
        "running": running,
        "logs": task_logs.get("benchmark", [])[-200:],
        "meta": task_meta.get("benchmark", {}),
    }


@app.get("/api/benchmark/context-status")
def api_benchmark_context_status():
    """ベンチマーク周辺の前提データを1箇所で確認するための軽量サマリ。

    - competitor_analysis_cache.json: 制作提案・タイトル/コンセプト/サムネ分析の共通入力
    - benchmark_profiles.json: チャンネル深掘り・融合プロンプト生成の入力
    """
    analysis = api_analysis_cache_info()
    profiles = {"generated_at": None, "count": 0}
    if BENCHMARK_PROFILES_FILE.exists():
        try:
            data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
            profiles = {
                "generated_at": data.get("generated_at"),
                "count": len(data.get("profiles") or []),
            }
        except Exception:
            profiles = {"generated_at": None, "count": 0, "error": "read_failed"}
    analysis_proc = active_tasks.get("analysis")
    benchmark_task = active_tasks.get("benchmark")
    benchmark_running = benchmark_task is not None and (
        (hasattr(benchmark_task, "is_alive") and benchmark_task.is_alive())
        or (hasattr(benchmark_task, "returncode") and benchmark_task.returncode is None)
    )
    return {
        "analysis": analysis,
        "profiles": profiles,
        "running": {
            "analysis": analysis_proc is not None and getattr(analysis_proc, "returncode", None) is None,
            "benchmark": benchmark_running,
        },
    }


# ─── API: 全体ステータス（共通ステータスバー向け） ───

_TASK_LABELS = {
    "suno": {"label": "SUNO 生成", "icon": "🎵"},
    "process": {"label": "楽曲後処理", "icon": "🎛"},
    "pipeline": {"label": "パイプライン", "icon": "🚀"},
    "analysis": {"label": "競合分析", "icon": "📊"},
    "benchmark": {"label": "ベンチマーク", "icon": "🎯"},
    "youtube": {"label": "YouTube アップロード", "icon": "📤"},
    "flow": {"label": "Flow 画像生成", "icon": "🖼"},
    "premiere": {"label": "Premiere 配置", "icon": "🎬"},
}


def _task_is_running(task_key: str) -> bool:
    obj = active_tasks.get(task_key)
    if obj is None:
        return False
    # subprocess.Popen
    if hasattr(obj, "returncode"):
        return obj.returncode is None
    # threading.Thread
    if hasattr(obj, "is_alive"):
        return bool(obj.is_alive())
    return False


def _task_progress_summary(task_key: str) -> dict:
    """タスクごとの進捗サマリ。SUNO は progress をパースして %表示に。"""
    logs = task_logs.get(task_key, [])
    meta = task_meta.get(task_key, {}) or {}
    summary = {
        "last_log": logs[-1] if logs else "",
        "started_at": meta.get("started_at"),
        "meta": meta,
    }
    if task_key == "suno":
        pg = _parse_suno_progress(logs)
        if pg and pg.get("total"):
            cur = pg.get("current", 0) or 0
            tot = pg.get("total", 0) or 0
            summary["progress"] = {
                "current": cur,
                "total": tot,
                "percent": min(100, int((cur / tot) * 100)) if tot else 0,
                "phase": pg.get("phase", ""),
            }
    return summary


@app.get("/api/status/all")
def api_status_all():
    """全タスクの状態を集約して返す（共通ステータスバー用）"""
    tasks = []
    for key, info in _TASK_LABELS.items():
        running = _task_is_running(key)
        if not running:
            # 直近完了も出せるよう meta の completed_at を確認
            meta = task_meta.get(key, {}) or {}
            if not meta.get("completed_at"):
                continue
        tasks.append({
            "key": key,
            "label": info["label"],
            "icon": info["icon"],
            "running": running,
            **_task_progress_summary(key),
        })
    # running を先頭に、完了タスクを後ろに
    tasks.sort(key=lambda x: (not x["running"], x["key"]))
    return {"tasks": tasks, "any_running": any(t["running"] for t in tasks)}


@app.get("/api/benchmark/profiles")
def api_benchmark_profiles(resolve_icons: bool = False):
    """保存済のベンチマーク・プロファイルを返す。
    Sheet A の ICON_IMAGE が空でアイコンが無いプロファイルは、
    既存キャッシュからフォールバック表示する。
    resolve_icons=true の時だけネットワーク取得を許可する。"""
    if not BENCHMARK_PROFILES_FILE.exists():
        return {"generated_at": None, "profiles": []}
    try:
        data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"読み込み失敗: {e}")
    icon_cache = _load_icon_cache()
    for p in (data.get("profiles") or []):
        if not (p.get("thumbnail") or "").strip():
            url = (p.get("url") or "").strip()
            if url:
                cached = icon_cache.get(url)
                if isinstance(cached, dict) and cached.get("icon_url"):
                    p["thumbnail"] = cached["icon_url"]
                elif resolve_icons:
                    try:
                        p["thumbnail"] = resolve_channel_icon(url)
                    except Exception:
                        pass
    return data


class DeleteProfilesRequest(BaseModel):
    channel_names: List[str]


@app.delete("/api/benchmark/profiles")
def api_benchmark_delete_profiles(req: DeleteProfilesRequest):
    """指定したチャンネル名のプロファイルを benchmark_profiles.json から削除する。
    リスト外チャンネル URL（dashboard_config.benchmark_extra_urls）にも該当があれば併せて除去。"""
    if not BENCHMARK_PROFILES_FILE.exists():
        raise HTTPException(404, "benchmark_profiles.json が存在しません")
    targets = {n.strip() for n in (req.channel_names or []) if n and n.strip()}
    if not targets:
        raise HTTPException(400, "channel_names が空です")
    try:
        data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        raise HTTPException(500, f"読み込み失敗: {e}")
    before = data.get("profiles") or []
    removed_urls: List[str] = []
    kept = []
    for p in before:
        if p.get("channel_name") in targets:
            url = (p.get("url") or "").strip()
            if url:
                removed_urls.append(url)
        else:
            kept.append(p)
    if len(kept) == len(before):
        return {"status": "noop", "removed": 0, "remaining": len(kept)}
    data["profiles"] = kept
    BENCHMARK_PROFILES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # benchmark_extra_urls にあるなら除去
    if removed_urls:
        try:
            cfg = get_dashboard_config()
            extras = cfg.get("benchmark_extra_urls") or []
            new_extras = [u for u in extras if u not in removed_urls]
            if len(new_extras) != len(extras):
                cfg["benchmark_extra_urls"] = new_extras
                save_dashboard_config_smart(cfg)
        except Exception:
            pass
    return {
        "status": "ok",
        "removed": len(before) - len(kept),
        "remaining": len(kept),
        "removed_urls": removed_urls,
    }


class AddChannelRequest(BaseModel):
    url: str


@app.post("/api/benchmark/add-channel")
def api_benchmark_add_channel(req: AddChannelRequest):
    """リスト外チャンネル URL を追加保存（次回 run-full で取り込まれる）"""
    cfg = get_dashboard_config()
    extras = cfg.get("benchmark_extra_urls") or []
    if req.url not in extras:
        extras.append(req.url)
        cfg["benchmark_extra_urls"] = extras
        save_dashboard_config_smart(cfg)
    return {"status": "ok", "extras": extras}


@app.delete("/api/benchmark/extra-channel")
def api_benchmark_remove_channel(url: str):
    cfg = get_dashboard_config()
    extras = [u for u in (cfg.get("benchmark_extra_urls") or []) if u != url]
    cfg["benchmark_extra_urls"] = extras
    save_dashboard_config_smart(cfg)
    return {"status": "ok", "extras": extras}


class BenchmarkFuseRequest(BaseModel):
    channel_names: list[str]


@app.post("/api/benchmark/suggest-persona")
async def api_benchmark_suggest_persona(req: BenchmarkFuseRequest):
    """指定したベンチマーク・チャンネル（複数）の良いところを抽出し、
    自チャンネル向けのペルソナ案（複数バリエーション）を Claude で生成する。
    BenchmarkFuseRequest を流用（channel_names のみ受け取り）。"""
    names = [n.strip() for n in (req.channel_names or []) if n and n.strip()]
    if not names:
        raise HTTPException(400, "1 件以上のチャンネルを選択してください")
    if not BENCHMARK_PROFILES_FILE.exists():
        raise HTTPException(409, "benchmark_profiles が未生成です。先にプロファイル生成を実行してください")
    data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
    all_profiles = data.get("profiles") or []
    selected = [p for p in all_profiles if p.get("channel_name") in names]
    if not selected:
        raise HTTPException(404, f"プロファイルが見つかりません: {', '.join(names)}")

    # 既存ペルソナを参考材料として渡す
    cfg = get_dashboard_config()
    current_persona = (cfg.get("persona") or "").strip()

    # Claude に渡すコンパクトな材料を構築
    materials = []
    for p in selected:
        prof = p.get("profile") or {}
        mp = prof.get("music_profile") or {}
        vp = prof.get("visual_profile") or {}
        pe = prof.get("persona") or {}
        ap = prof.get("appeal_points") or []
        materials.append({
            "channel_name": p.get("channel_name"),
            "music": {
                "genres": mp.get("genres"),
                "mood": mp.get("mood"),
                "imagery": mp.get("imagery"),
            },
            "visual": {
                "palette": vp.get("palette"),
                "atmosphere": vp.get("atmosphere"),
                "time_of_day": vp.get("time_of_day"),
            },
            "audience": {
                "age_range": pe.get("age_range"),
                "demographics": pe.get("demographics"),
                "viewing_scenes": pe.get("viewing_scenes"),
                "psychological_needs": pe.get("psychological_needs"),
            },
            "appeal_points": ap[:5] if isinstance(ap, list) else [],
        })

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    prompt = f"""You are a brand strategist for a YouTube BGM/instrumental music channel.
Your task: synthesize **3 distinct persona drafts** for our own channel by extracting the strongest elements from the benchmark channels below.

Current persona (reference, may be empty):
{current_persona or '(empty)'}

Benchmark channel materials (extract the GOOD points, do not copy verbatim):
{json.dumps(materials, ensure_ascii=False, indent=2)}

Rules:
- Output 3 persona drafts in JAPANESE.
- Each draft must be 2-4 sentences, 80-200 characters, written in a way that can be pasted directly into a YouTube channel "About" or used as a creative brief.
- Each draft should pick a DIFFERENT angle (e.g. mood/scene focus, audience focus, visual world focus).
- Translate insights into our own aesthetic — do NOT mention specific competitor channel names or trademarked elements.
- For each draft, include a short rationale (1 line, JA) explaining what was borrowed from the benchmarks.

Return ONLY a JSON object in this exact shape (no markdown fences, no prose):
{{
  "drafts": [
    {{ "label": "短い見出し（10-20字）", "persona": "ペルソナ本文（80-200字）", "rationale": "どの観点を借りたかの一文" }},
    ...
  ]
}}
"""
    try:
        from app_llm_runner import run_llm, LLMError
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=240, label="persona-suggest").strip()
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:300]}")
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        raise HTTPException(502, f"Claude 応答から JSON を抽出できませんでした: {out[:300]}")
    try:
        parsed = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(502, f"JSON 解析失敗: {e}; 応答: {out[:300]}")
    drafts = parsed.get("drafts") or []
    if not isinstance(drafts, list) or not drafts:
        raise HTTPException(502, "drafts フィールドが不正です")
    return {
        "status": "ok",
        "channel_names": [p.get("channel_name") for p in selected],
        "drafts": drafts,
    }


@app.post("/api/benchmark/fuse")
async def api_benchmark_fuse(req: BenchmarkFuseRequest):
    """指定したチャンネル（1 件以上）のプロファイルから、orzz. 向けの統合 direction と
    SUNO / Flow プロンプトを返す。1 件なら単一チャンネルの抽出→翻案、複数なら融合。
    バックグラウンドではなく同期実行（最大 7 分）。"""
    names = [n.strip() for n in (req.channel_names or []) if n and n.strip()]
    if len(names) < 1:
        raise HTTPException(400, "チャンネルを 1 件以上選択してください")
    if not BENCHMARK_PROFILES_FILE.exists():
        raise HTTPException(409, "benchmark_profiles が未生成です。先にプロファイル生成を実行してください")

    data = json.loads(BENCHMARK_PROFILES_FILE.read_text(encoding="utf-8"))
    all_profiles = data.get("profiles") or []
    selected = [p for p in all_profiles if p.get("channel_name") in names]
    missing = [n for n in names if not any(p.get("channel_name") == n for p in all_profiles)]
    if missing:
        raise HTTPException(404, f"未登録のチャンネル: {', '.join(missing)}")
    if len(selected) < 1:
        raise HTTPException(400, "有効なプロファイルがありません")

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    from app_competitor import fuse_benchmark_profiles
    try:
        result = fuse_benchmark_profiles(selected, cli_cmd=cli_cmd)
    except Exception as e:
        raise HTTPException(500, f"融合失敗: {e}")

    return {
        "status": "ok",
        "channel_names": [p.get("channel_name") for p in selected],
        "fusion": result,
    }


@app.get("/api/benchmark/extra-channels")
def api_benchmark_list_extras():
    cfg = get_dashboard_config()
    return {"extras": cfg.get("benchmark_extra_urls") or []}


# ─── API: A/B ログ（vol ごとの制作条件 + 7 日後 delta） ───

AB_LOG_FILE = CONFIG_DIR / "ab_log.json"


def _load_ab_log() -> list[dict]:
    try:
        return json.loads(AB_LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_ab_log(entries: list[dict]):
    AB_LOG_FILE.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


class ABLogCreate(BaseModel):
    video_name: str
    suno_prompt: str = ""
    flow_prompt: str = ""
    target_bpm: str = ""
    color_theme: str = ""
    benchmark_names: list[str] = []
    note: str = ""


class ABLogMeasure(BaseModel):
    views_delta_7d: int = 0
    subs_delta_7d: int = 0
    note: str = ""


@app.get("/api/ab-log")
def api_ab_log_list():
    """A/B ログ一覧（新しい順）"""
    entries = _load_ab_log()
    import datetime as _dt
    now = _dt.datetime.now()
    for e in entries:
        pub = e.get("published_at")
        if pub:
            try:
                t = _dt.datetime.fromisoformat(pub)
                e["days_since_publish"] = (now - t).days
                e["ready_to_measure"] = e["days_since_publish"] >= 7 and e.get("views_delta_7d") is None
            except Exception:
                e["days_since_publish"] = None
                e["ready_to_measure"] = False
    entries.sort(key=lambda e: e.get("published_at") or "", reverse=True)
    return {"entries": entries}


@app.post("/api/ab-log")
def api_ab_log_create(req: ABLogCreate):
    """新規エントリ追加（投稿時に制作条件を記録）"""
    entries = _load_ab_log()
    # 同一 video_name の重複は更新
    entries = [e for e in entries if e.get("video_name") != req.video_name]
    import datetime as _dt
    entries.append({
        "video_name": req.video_name,
        "published_at": _dt.datetime.now().isoformat(),
        "suno_prompt": req.suno_prompt,
        "flow_prompt": req.flow_prompt,
        "target_bpm": req.target_bpm,
        "color_theme": req.color_theme,
        "benchmark_names": req.benchmark_names,
        "note": req.note,
        "views_delta_7d": None,
        "subs_delta_7d": None,
        "measured_at": None,
    })
    _save_ab_log(entries)
    return {"status": "ok"}


@app.post("/api/ab-log/{video_name}/measure")
def api_ab_log_measure(video_name: str, req: ABLogMeasure):
    """7 日後の delta を書き込む（Channel Tracker から手動転記）"""
    entries = _load_ab_log()
    found = False
    import datetime as _dt
    for e in entries:
        if e.get("video_name") == video_name:
            e["views_delta_7d"] = int(req.views_delta_7d)
            e["subs_delta_7d"] = int(req.subs_delta_7d)
            e["measured_at"] = _dt.datetime.now().isoformat()
            if req.note:
                e["measure_note"] = req.note
            found = True
            break
    if not found:
        raise HTTPException(404, "該当エントリが A/B ログにありません")
    _save_ab_log(entries)
    return {"status": "ok"}


@app.delete("/api/ab-log/{video_name}")
def api_ab_log_delete(video_name: str):
    entries = _load_ab_log()
    before = len(entries)
    entries = [e for e in entries if e.get("video_name") != video_name]
    if len(entries) == before:
        raise HTTPException(404)
    _save_ab_log(entries)
    return {"status": "ok"}


# ─── API: チャンネルアイコン ───

@app.get("/api/channel-icon")
def api_channel_icon():
    """現在のチャンネルアイコンを返す。ローカル → channels.json の icon_cache → 404。"""
    from fastapi.responses import RedirectResponse
    config = get_dashboard_config()
    icon_path = config.get("channel_icon", "")
    if icon_path and Path(icon_path).exists():
        return FileResponse(icon_path)
    channel_dir = Path(config.get("channel_folder", ""))
    if channel_dir.exists():
        for name in ["icon.png", "icon.jpg", "channel_icon.png", "channel_icon.jpg"]:
            icon = channel_dir / name
            if icon.exists():
                return FileResponse(str(icon))
    # YouTube URL から取得した remote アイコンにリダイレクト（active channel）
    chs = get_channels()
    active = next((c for c in chs if c.get("folder") == config.get("channel_folder")), None)
    if active:
        url = (active.get("icon_cache") or {}).get("url") or active.get("icon_url", "")
        if url:
            return RedirectResponse(url, status_code=302)
    raise HTTPException(404, "アイコンが設定されていません")

# ─── API: プロンプト ───

def get_prompts():
    default = [
        {"id": "lounge", "name": "Lounge BGM", "text": "Create a sophisticated lounge BGM track. Think elegant cafe, golden hour, luxury hotel lobby vibes. Instrumental only."},
        {"id": "ambient", "name": "Ambient Chill", "text": "Create a calm ambient BGM. Ethereal pads, soft piano, nature-inspired textures. Instrumental only."},
    ]
    return load_json(PROMPTS_CONFIG, default) if PROMPTS_CONFIG.exists() else default

@app.get("/api/prompts")
def api_list_prompts():
    return {"prompts": get_prompts()}

class PromptSave(BaseModel):
    id: Optional[str] = None
    name: str
    text: str

@app.post("/api/prompts")
def api_save_prompt(req: PromptSave):
    prompts = get_prompts()
    pid = req.id or re.sub(r'[^a-zA-Z0-9]', '_', req.name.lower()).strip('_') + str(len(prompts))
    existing = next((p for p in prompts if p["id"] == pid), None)
    if existing:
        existing["name"] = req.name; existing["text"] = req.text
    else:
        prompts.append({"id": pid, "name": req.name, "text": req.text})
    save_json(PROMPTS_CONFIG, prompts)
    return {"status": "ok", "prompts": prompts}

@app.delete("/api/prompts/{prompt_id}")
def api_delete_prompt(prompt_id: str):
    save_json(PROMPTS_CONFIG, [p for p in get_prompts() if p["id"] != prompt_id])
    return {"status": "ok"}

# ─── API: スケジュール ───

@app.get("/api/schedule")
def api_get_schedule():
    schedule = load_json(SCHEDULE_CONFIG, []) if SCHEDULE_CONFIG.exists() else []
    videos = api_list_videos().get("videos", [])
    channel_dir = Path(get_dashboard_config().get("channel_folder", ""))
    for v in videos:
        if not v.get("publish_date"):
            continue
        if not any(s.get("video_num") == v["num"] for s in schedule):
            upload_json = channel_dir / v["name"] / "youtube_upload.json"
            upload_info = load_json(upload_json, {}) if upload_json.exists() else {}
            youtube_scheduled = bool(upload_info.get("schedule"))
            schedule.append({
                "video_num": v["num"], "video_name": v["name"], "date": v["publish_date"],
                "status": "exported" if v["has_mp4"] else ("ready" if v["has_music"] else "pending"),
                "uploaded": bool(upload_info.get("video_id")),
                "youtube_scheduled": youtube_scheduled,
            })
    # 既存エントリにも youtube_scheduled を補完
    for s in schedule:
        if "youtube_scheduled" not in s:
            vname = s.get("video_name", "")
            upload_json = channel_dir / vname / "youtube_upload.json" if vname else None
            upload_info = load_json(upload_json, {}) if (upload_json and upload_json.exists()) else {}
            s["youtube_scheduled"] = bool(upload_info.get("schedule"))
    return {"schedule": schedule}

# ─── API: YouTube 説明文生成 ───

class DescriptionGenerateRequest(BaseModel):
    video_folder: str
    style_reference: Optional[str] = None  # 参考にする過去の説明文

@app.get("/api/youtube-desc/references")
def api_desc_references():
    """過去の説明文を参考用にリストアップ"""
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    refs = []
    if channel_dir.exists():
        for d in sorted(channel_dir.iterdir(), reverse=True):
            desc_file = d / "youtube_description.txt"
            if desc_file.exists():
                m = re.match(r'^(\d+)_', d.name)
                num = m.group(1) if m else "?"
                text = desc_file.read_text(encoding="utf-8").strip()
                refs.append({
                    "num": num,
                    "name": d.name,
                    "text": text[:200] + "..." if len(text) > 200 else text,
                    "full_text": text,
                })
                if len(refs) >= 10:
                    break
    return {"references": refs}


def _read_matching_timecodes_until_loop(folder: Path, vol: str = "") -> tuple[str, Optional[Path]]:
    """対応する music_time_code_info_*.txt を読み、LOOP 行の直前まで返す。"""
    folder = Path(folder)
    candidates: list[Path] = []
    vol = (vol or "").strip()
    if vol:
        candidates.append(folder / f"music_time_code_info_{vol}.txt")
        try:
            candidates.append(folder / f"music_time_code_info_{int(vol)}.txt")
        except Exception:
            pass
        if vol.isdigit():
            candidates.append(folder / f"music_time_code_info_{int(vol):02d}.txt")
    candidates.extend(sorted(folder.glob("music_time_code_info_*.txt")))

    seen: set[Path] = set()
    for p in candidates:
        if p in seen or not p.exists():
            continue
        seen.add(p)
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        out = []
        for line in lines:
            if "LOOP" in line.upper():
                break
            out.append(line.rstrip())
        return "\n".join(out).strip(), p
    return "", None


@app.get("/api/youtube-desc/video-info/{video_name}")
def api_desc_video_info(video_name: str):
    """動画フォルダの情報を取得（サムネイル、タイムコード、曲リスト）"""
    config = get_dashboard_config()
    channel_dir = Path(config["channel_folder"])
    folder = channel_dir / video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")

    m = re.match(r'^(\d+)_', video_name)
    num = m.group(1) if m else "00"

    # タイムコード読み込み（対応ファイルから LOOP 直前まで）
    timecodes, tc_file = _read_matching_timecodes_until_loop(folder, num)

    # サムネイル情報
    thumb = None
    for pattern in ["サムネイル.jpg", "サムネイル.png", "thumbnail.jpg", "thumbnail.png", f"vol{num}.jpg", f"vol{num}.png"]:
        t = folder / pattern
        if t.exists():
            thumb = {"name": t.name, "path": str(t)}
            break

    # 曲リスト（LOOPまで）
    songs = []
    for line in timecodes.split("\n"):
        if "LOOP" in line:
            break
        if " - " in line:
            songs.append(line.strip())

    return {
        "num": num,
        "name": video_name,
        "timecodes": timecodes,
        "timecode_file": tc_file.name if tc_file else "",
        "songs": songs,
        "song_count": len(songs),
        "thumbnail": thumb,
    }

@app.get("/api/youtube-desc/thumbnail/{video_name}")
def api_desc_thumbnail(video_name: str):
    """サムネイル画像を返す"""
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / video_name
    m = re.match(r'^(\d+)_', video_name)
    num = m.group(1) if m else "00"
    for pattern in ["サムネイル.jpg", "サムネイル.png", "thumbnail.jpg", "thumbnail.png", f"vol{num}.jpg", f"vol{num}.png"]:
        t = folder / pattern
        if t.exists():
            return FileResponse(str(t))
    raise HTTPException(404, "サムネイルが見つかりません")

class DescriptionSaveRequest(BaseModel):
    video_name: str
    text: str

@app.post("/api/youtube-desc/save")
def api_desc_save(req: DescriptionSaveRequest):
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / req.video_name
    if not folder.exists():
        raise HTTPException(404, "フォルダが見つかりません")
    desc_file = folder / "youtube_description.txt"
    desc_file.write_text(req.text, encoding="utf-8")
    return {"status": "ok"}

# ─── API: 通知 ───
class NotifyRequest(BaseModel):
    message: str

@app.post("/api/notify/discord")
def api_notify_discord(req: NotifyRequest):
    try:
        result = subprocess.run(["bash", str(NOTIFY_SCRIPT), req.message], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise HTTPException(500, result.stderr.strip() or result.stdout.strip() or "Discord通知に失敗しました")
        return {"status": "ok", "output": result.stdout}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, str(e))

@app.post("/api/notify/line")
def api_notify_line_compat(req: NotifyRequest):
    """Backward-compatible alias. Notifications now go to Discord."""
    return api_notify_discord(req)

# ─── API: YouTube ───

# YouTube ローカリゼーション言語マスタ（BCP47 + 表示名 + ネイティブ + 国旗）
# BGM チャンネル運用で実用価値の高い 24 言語を厳選。
# UI ではこの順序でグリッド表示、JS 側もこのマスタを GET API で取得して同期。
YT_LANGUAGE_CATALOG = [
    {"code": "en",      "name": "英語",                  "native": "English",          "flag": "🇺🇸"},
    {"code": "ja",      "name": "日本語",                "native": "日本語",            "flag": "🇯🇵"},
    {"code": "zh-Hans", "name": "中国語（簡体字）",        "native": "简体中文",          "flag": "🇨🇳"},
    {"code": "zh-Hant", "name": "中国語（繁体字）",        "native": "繁體中文",          "flag": "🇹🇼"},
    {"code": "ko",      "name": "韓国語",                 "native": "한국어",            "flag": "🇰🇷"},
    {"code": "es",      "name": "スペイン語",              "native": "Español",          "flag": "🇪🇸"},
    {"code": "es-419",  "name": "スペイン語（中南米）",     "native": "Español LA",       "flag": "🌎"},
    {"code": "pt-BR",   "name": "ポルトガル語（ブラジル）", "native": "Português BR",     "flag": "🇧🇷"},
    {"code": "fr",      "name": "フランス語",              "native": "Français",         "flag": "🇫🇷"},
    {"code": "de",      "name": "ドイツ語",                "native": "Deutsch",          "flag": "🇩🇪"},
    {"code": "it",      "name": "イタリア語",              "native": "Italiano",         "flag": "🇮🇹"},
    {"code": "ru",      "name": "ロシア語",                "native": "Русский",          "flag": "🇷🇺"},
    {"code": "id",      "name": "インドネシア語",          "native": "Bahasa Indonesia", "flag": "🇮🇩"},
    {"code": "vi",      "name": "ベトナム語",              "native": "Tiếng Việt",      "flag": "🇻🇳"},
    {"code": "th",      "name": "タイ語",                  "native": "ไทย",              "flag": "🇹🇭"},
    {"code": "tr",      "name": "トルコ語",                "native": "Türkçe",           "flag": "🇹🇷"},
    {"code": "ar",      "name": "アラビア語",              "native": "العربية",          "flag": "🇸🇦"},
    {"code": "hi",      "name": "ヒンディー語",            "native": "हिन्दी",             "flag": "🇮🇳"},
    {"code": "nl",      "name": "オランダ語",              "native": "Nederlands",       "flag": "🇳🇱"},
    {"code": "pl",      "name": "ポーランド語",            "native": "Polski",           "flag": "🇵🇱"},
    {"code": "sv",      "name": "スウェーデン語",          "native": "Svenska",          "flag": "🇸🇪"},
    {"code": "fil",     "name": "フィリピノ語",            "native": "Filipino",         "flag": "🇵🇭"},
    {"code": "ms",      "name": "マレー語",                "native": "Bahasa Melayu",    "flag": "🇲🇾"},
    {"code": "uk",      "name": "ウクライナ語",            "native": "Українська",       "flag": "🇺🇦"},
    {"code": "he",      "name": "ヘブライ語",              "native": "עברית",            "flag": "🇮🇱"},
]

# YouTube アップロード詳細設定（チャンネル横断テンプレート）
YT_UPLOAD_DEFAULTS_FILE = CONFIG_DIR / "youtube_upload_defaults.json"

# app_youtube.py と同じ既定値（API/UI 用にミラー）
YT_UPLOAD_BUILTIN_DEFAULTS = {
    "category_id": "10",
    "default_language": "en",
    "default_audio_language": "en",
    "made_for_kids": False,
    "synthetic_media": True,
    "license": "youtube",
    "embeddable": True,
    "public_stats_viewable": True,
    "notify_subscribers": True,
    "localization_languages": ["ja", "zh-Hans", "zh-Hant", "ko"],
}


def _resolve_video_folder(video_name: str) -> Path:
    """video_name から動画フォルダの絶対パスを返す。"""
    config = get_dashboard_config()
    base = config.get("channel_folder")
    if not base:
        raise HTTPException(400, "channel_folder が未設定です")
    p = Path(base) / video_name
    if not p.exists():
        raise HTTPException(404, f"動画フォルダが見つかりません: {video_name}")
    return p


def _read_json_dict(p: Path, default=None) -> dict:
    if not p.exists():
        return dict(default or {})
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else dict(default or {})
    except Exception:
        return dict(default or {})


def _write_json_dict(p: Path, data: dict):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_yt_upload_defaults() -> dict:
    """YouTube アップロードのテンプレート設定を返す。
    優先順位: builtin → グローバル(legacy) → アクティブチャンネル別 (Google Drive 同期)
    """
    out = dict(YT_UPLOAD_BUILTIN_DEFAULTS)
    # 旧グローバルファイル（後方互換、マイグレーション後はチャンネル側を優先）
    out.update(_read_json_dict(YT_UPLOAD_DEFAULTS_FILE))
    # アクティブチャンネル別（あれば最優先）
    cc = load_channel_config()
    yu = cc.get("youtube_upload_defaults") or {}
    if isinstance(yu, dict):
        out.update(yu)
    return out


# ── 設定タブ用テンプレート API ──
class YouTubeUploadDefaults(BaseModel):
    category_id: Optional[str] = None
    default_language: Optional[str] = None
    default_audio_language: Optional[str] = None
    made_for_kids: Optional[bool] = None
    synthetic_media: Optional[bool] = None
    license: Optional[str] = None
    embeddable: Optional[bool] = None
    public_stats_viewable: Optional[bool] = None
    notify_subscribers: Optional[bool] = None
    localization_languages: Optional[List[str]] = None


@app.get("/api/youtube/languages")
def api_get_yt_languages():
    """ローカリゼーション言語マスタ（24言語）を返す。UI のチェックボックスグリッド用。"""
    return {"languages": YT_LANGUAGE_CATALOG}


@app.get("/api/youtube/upload-defaults")
def api_get_yt_upload_defaults():
    """テンプレート設定（チャンネル横断）を返す。"""
    return {
        "defaults": get_yt_upload_defaults(),
        "builtin": YT_UPLOAD_BUILTIN_DEFAULTS,
        "saved": YT_UPLOAD_DEFAULTS_FILE.exists(),
    }


@app.put("/api/youtube/upload-defaults")
def api_put_yt_upload_defaults(req: YouTubeUploadDefaults):
    """テンプレート設定を保存。指定キーだけ上書き。
    アクティブチャンネルがあればチャンネル別ファイル（Google Drive 同期対象）へ、
    無ければグローバルにフォールバック。"""
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    if _channel_config_path():
        cc = load_channel_config()
        current = cc.get("youtube_upload_defaults") or {}
        current.update(payload)
        cc["youtube_upload_defaults"] = current
        save_channel_config(cc)
    else:
        current = _read_json_dict(YT_UPLOAD_DEFAULTS_FILE)
        current.update(payload)
        _write_json_dict(YT_UPLOAD_DEFAULTS_FILE, current)
    return {"status": "ok", "defaults": get_yt_upload_defaults()}


# ── 動画別の上書き API ──
@app.get("/api/videos/{video_name}/youtube-overrides")
def api_get_video_yt_overrides(video_name: str):
    folder = _resolve_video_folder(video_name)
    overrides = _read_json_dict(folder / "youtube_upload_overrides.json")
    return {
        "video_name": video_name,
        "overrides": overrides,
        "effective": {**get_yt_upload_defaults(), **overrides},
    }


@app.put("/api/videos/{video_name}/youtube-overrides")
def api_put_video_yt_overrides(video_name: str, req: YouTubeUploadDefaults):
    folder = _resolve_video_folder(video_name)
    p = folder / "youtube_upload_overrides.json"
    current = _read_json_dict(p)
    payload = {k: v for k, v in req.model_dump().items() if v is not None}
    current.update(payload)
    if current:
        _write_json_dict(p, current)
    elif p.exists():
        p.unlink()
    return {
        "status": "ok",
        "overrides": current,
        "effective": {**get_yt_upload_defaults(), **current},
    }


# ── 多言語タイトル/説明（ローカライズ）API ──
class YouTubeLocalization(BaseModel):
    title: str = ""
    description: str = ""


class YouTubeLocalizationsPayload(BaseModel):
    localizations: dict = {}  # {"en": {"title":"...","description":"..."}, ...}


@app.get("/api/videos/{video_name}/youtube-localizations")
def api_get_video_yt_localizations(video_name: str):
    folder = _resolve_video_folder(video_name)
    return {
        "video_name": video_name,
        "localizations": _read_json_dict(folder / "youtube_localizations.json"),
    }


@app.put("/api/videos/{video_name}/youtube-localizations")
def api_put_video_yt_localizations(video_name: str, req: YouTubeLocalizationsPayload):
    folder = _resolve_video_folder(video_name)
    p = folder / "youtube_localizations.json"
    cleaned = {}
    for lang, entry in (req.localizations or {}).items():
        if not isinstance(entry, dict):
            continue
        t = (entry.get("title") or "").strip()
        d = (entry.get("description") or "").strip()
        if t or d:
            cleaned[str(lang)] = {"title": t, "description": d}
    if cleaned:
        _write_json_dict(p, cleaned)
    elif p.exists():
        p.unlink()
    return {"status": "ok", "localizations": cleaned}


class YouTubeTranslateRequest(BaseModel):
    languages: List[str]                # ["ja", "zh-Hans", "ko", ...] - en はソース言語なので通常スキップ
    title: Optional[str] = None         # 未指定なら youtube_title.txt
    description: Optional[str] = None   # 未指定なら youtube_description.txt
    source_language: str = "en"
    overwrite: bool = False             # True なら既存の翻訳を上書き


@app.post("/api/videos/{video_name}/youtube-translate")
async def api_translate_video_yt(video_name: str, req: YouTubeTranslateRequest):
    """Claude CLI でタイトル+説明文を指定言語に翻訳し、youtube_localizations.json に保存。
    既存の翻訳は overwrite=True でない限り保持する。"""
    folder = _resolve_video_folder(video_name)
    title = (req.title or "").strip()
    if not title:
        tf = folder / "youtube_title.txt"
        if tf.exists():
            title = tf.read_text(encoding="utf-8").strip()
    description = (req.description or "").strip()
    if not description:
        df = folder / "youtube_description.txt"
        if df.exists():
            description = df.read_text(encoding="utf-8").strip()
    if not title and not description:
        raise HTTPException(400, "翻訳対象のタイトルまたは説明文がありません")

    # 既存
    p = folder / "youtube_localizations.json"
    existing = _read_json_dict(p)

    targets = [lang for lang in (req.languages or []) if lang and lang.strip()]
    if not req.overwrite:
        targets = [lang for lang in targets if lang not in existing]
    if not targets:
        return {"status": "ok", "skipped": True, "localizations": existing,
                "message": "全言語が既に存在します（overwrite=False）"}

    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    prompt = f"""You are a YouTube localization translator for a BGM/instrumental music channel.

Translate the title and description from {req.source_language} into the following target languages:
{json.dumps(targets, ensure_ascii=False)}

Source title:
{title or '(none)'}

Source description:
{description or '(none)'}

Rules:
- Keep the SAME tone, mood, and aesthetic as the source.
- For BGM channel titles: keep the evocative scene/moment-based phrasing. Don't translate too literally.
- Preserve hashtags, URLs, and timestamps verbatim (do not translate them).
- Preserve the description's structure (paragraph breaks, bullet lists, timestamps if any).
- Title length should fit YouTube's 100-character limit.
- Use natural, native-speaker phrasing — not machine-translation style.
- Use the language's native script (e.g. zh-Hans = Simplified Chinese characters, ko = Korean Hangul).
- For "en", use English.

Return ONLY a JSON object in this exact shape (no markdown fences, no prose):
{{
  "translations": {{
    "<lang_code>": {{"title": "...", "description": "..."}},
    ...
  }}
}}
"""
    try:
        from app_llm_runner import run_llm, LLMError
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="translate-meta").strip()
    except LLMError as e:
        raise HTTPException(500, f"Claude/Codex 失敗: {str(e)[:300]}")

    # JSON 抽出（ファンスやプリアンブルを許容）
    m = re.search(r"\{[\s\S]*\}", out)
    if not m:
        raise HTTPException(502, f"Claude 応答から JSON を抽出できませんでした: {out[:300]}")
    try:
        parsed = json.loads(m.group(0))
    except Exception as e:
        raise HTTPException(502, f"JSON 解析失敗: {e}; 応答: {out[:300]}")

    translations = parsed.get("translations") or {}
    if not isinstance(translations, dict):
        raise HTTPException(502, "translations フィールドが不正です")

    merged = dict(existing)
    added = []
    for lang in targets:
        entry = translations.get(lang)
        if not isinstance(entry, dict):
            continue
        t = (entry.get("title") or "").strip()
        d = (entry.get("description") or "").strip()
        if not (t or d):
            continue
        merged[lang] = {"title": t, "description": d}
        added.append(lang)

    if merged:
        _write_json_dict(p, merged)

    return {"status": "ok", "added": added, "localizations": merged}


# ── アップロード API（既存を拡張）──
class UploadRequest(BaseModel):
    folder: Optional[str] = None
    video_name: Optional[str] = None    # folder 未指定でも video_name から解決可
    video_path: Optional[str] = None    # 手動登録 / 外部書き出し済み MP4 を直接指定
    title: Optional[str] = None
    schedule: Optional[str] = None
    privacy: Optional[str] = "private"
    tags: Optional[List[str]] = None    # 指定時は youtube_tags.txt より優先
    # 詳細設定（None なら defaults → override の順で解決）
    category_id: Optional[str] = None
    default_language: Optional[str] = None
    default_audio_language: Optional[str] = None
    made_for_kids: Optional[bool] = None
    synthetic_media: Optional[bool] = None
    license: Optional[str] = None
    embeddable: Optional[bool] = None
    public_stats_viewable: Optional[bool] = None
    notify_subscribers: Optional[bool] = None
    use_localizations: Optional[bool] = True   # False なら多言語ファイルがあっても無視


def _build_youtube_upload_command(req: UploadRequest) -> tuple[list[str], dict]:
    # folder を解決
    folder = req.folder
    if not folder and req.video_name:
        config = get_dashboard_config()
        folder = str(Path(config["channel_folder"]) / req.video_name)
    if not folder:
        raise HTTPException(400, "folder または video_name が必要です")

    # 外部 SSD や手動登録済み MP4 は channel folder 内に無いので、
    # 見つかった実体を --video-path で渡す。
    video_path_arg: Optional[str] = None
    try:
        folder_path = Path(folder)
        video_name = req.video_name or folder_path.name
        if req.video_path:
            explicit = _safe_user_path(req.video_path)
            if not _is_exported_mp4_candidate(explicit):
                raise HTTPException(400, "指定された動画ファイルが見つかりません")
            video_path_arg = str(explicit)
        else:
            found = _find_exported_mp4(folder_path, video_name)
            if found is not None:
                video_path_arg = str(found)
    except HTTPException:
        raise
    except ValueError as e:
        # 外部ボリューム未マウント等
        raise HTTPException(503, f"外部書き出し先が利用不可: {e}")
    except Exception as e:
        print(f"[upload] external path resolve failed: {e}")

    cmd = [sys.executable, "-u", str(YOUTUBE_SCRIPT), folder]  # -u: unbuffered
    if video_path_arg: cmd += ["--video-path", video_path_arg]
    # チャンネル別トークンを明示指定（ブランドアカウント運用で複数チャンネル対応）
    token_path = folder_path.parent / ".youtube_token.json"
    active_ch = _channel_registry_entry_for_folder(folder_path.parent) or _active_channel_registry_entry()
    expected_channel_id = (active_ch.get("youtube_channel_id") or "").strip()
    expected_channel_name = active_ch.get("name") or get_dashboard_config().get("channel_name", "")
    if token_path:
        if token_path.exists():
            _validate_youtube_channel_for_entry(
                token_path, active_ch, fallback_name=expected_channel_name, raise_on_mismatch=True
            )
        cmd += ["--token-file", str(token_path)]
    if expected_channel_id:
        cmd += ["--expected-channel-id", expected_channel_id]
    if expected_channel_name:
        cmd += ["--expected-channel-name", expected_channel_name]
    if req.title: cmd += ["--title", req.title]
    if req.schedule: cmd += ["--schedule", req.schedule]
    if req.privacy: cmd += ["--privacy", req.privacy]
    if req.tags: cmd += ["--tags", ",".join(req.tags)]
    if req.category_id: cmd += ["--category-id", str(req.category_id)]
    if req.default_language: cmd += ["--default-language", req.default_language]
    if req.default_audio_language: cmd += ["--default-audio-language", req.default_audio_language]
    if req.made_for_kids is not None: cmd += ["--made-for-kids", str(bool(req.made_for_kids)).lower()]
    if req.synthetic_media is not None: cmd += ["--synthetic-media", str(bool(req.synthetic_media)).lower()]
    if req.license: cmd += ["--license", req.license]
    if req.embeddable is not None: cmd += ["--embeddable", str(bool(req.embeddable)).lower()]
    if req.public_stats_viewable is not None: cmd += ["--public-stats-viewable", str(bool(req.public_stats_viewable)).lower()]
    if req.notify_subscribers is not None: cmd += ["--notify-subscribers", str(bool(req.notify_subscribers)).lower()]
    if req.use_localizations is False: cmd += ["--no-localizations"]
    return cmd, {
        "folder": str(folder_path),
        "video_name": video_name,
        "video_path": video_path_arg or "",
        "privacy": req.privacy or "",
        "title": req.title or "",
        "schedule": req.schedule or "",
    }


@app.post("/api/youtube/upload")
async def api_youtube_upload(req: UploadRequest):
    cmd, meta = _build_youtube_upload_command(req)
    result = _enqueue_youtube_upload(cmd, meta, source="web")
    return {**result, **_youtube_queue_snapshot()}


class BatchUploadRequest(BaseModel):
    video_names: List[str]
    privacy: str = "unlisted"  # "private" / "unlisted" / "public"


@app.post("/api/youtube/batch-upload")
async def api_youtube_batch_upload(req: BatchUploadRequest):
    """複数 vol を**順次**アップロード（既存 upload queue を活用）。

    bash + curl + polling の脆さ（変数名衝突、polling 漏れ）を排除し、
    一度の API 呼び出しで全部 enqueue する。queue は内部で 1 つずつ順次処理。
    quota 枯渇時は YT_QUOTA_EXHAUSTED で残りをスキップして翌日 scheduler が再開。
    """
    if not req.video_names:
        raise HTTPException(400, "video_names が空です")
    enqueued: list = []
    skipped: list = []
    for name in req.video_names:
        try:
            up_req = UploadRequest(video_name=name, privacy=req.privacy)
            cmd, meta = _build_youtube_upload_command(up_req)
            result = _enqueue_youtube_upload(cmd, meta, source="web-batch")
            enqueued.append({"video_name": name, **{k: v for k, v in result.items() if k in ("status", "job", "position")}})
        except HTTPException as e:
            skipped.append({"video_name": name, "reason": e.detail, "status_code": e.status_code})
        except Exception as e:
            skipped.append({"video_name": name, "reason": str(e), "status_code": 500})
    snapshot = _youtube_queue_snapshot()
    return {
        "status": "ok",
        "enqueued": enqueued,
        "skipped": skipped,
        **snapshot,
    }

@app.get("/api/youtube/status")
def api_youtube_status():
    proc = active_tasks.get("youtube")
    snap = _youtube_queue_snapshot()
    return {
        "running": proc is not None and proc.returncode is None,
        "logs": task_logs.get("youtube", [])[-80:],
        **snap,
    }


# ─── YouTube アップロード履歴（D-4: スプシ記録向けローカル蓄積 + CSV エクスポート） ───
YT_UPLOAD_HISTORY_FILE = CONFIG_DIR / "youtube_upload_history.jsonl"


def _read_youtube_history() -> List[dict]:
    if not YT_UPLOAD_HISTORY_FILE.exists():
        return []
    out: List[dict] = []
    try:
        for line in YT_UPLOAD_HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        return []
    return out


class YouTubeHistoryEntry(BaseModel):
    video_name: str
    vol: Optional[str] = None
    youtube_url: Optional[str] = None
    title: Optional[str] = None
    privacy: Optional[str] = None
    schedule: Optional[str] = None
    tags: Optional[List[str]] = None
    localizations: Optional[List[str]] = None
    description_chars: Optional[int] = None
    has_thumbnail: Optional[bool] = None


@app.post("/api/youtube/history")
def api_record_youtube_history(entry: YouTubeHistoryEntry):
    """アップロード結果を JSONL に追記。スプシ貼り付け用に CSV エクスポート可能。"""
    record = entry.model_dump()
    record["recorded_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    YT_UPLOAD_HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with YT_UPLOAD_HISTORY_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"status": "ok", "entry": record, "total": len(_read_youtube_history())}


@app.get("/api/youtube/history")
def api_get_youtube_history(limit: int = 200):
    items = _read_youtube_history()
    items.reverse()  # 新しい順
    return {"items": items[: max(1, min(int(limit), 1000))], "total": len(items)}


@app.get("/api/youtube/history.csv")
def api_export_youtube_history_csv():
    """CSV をダウンロード。スプシに貼り付ければそのまま記録できる。"""
    import io, csv
    items = _read_youtube_history()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "投稿日時", "vol", "動画名", "YouTube URL", "タイトル",
        "公開設定", "予約", "翻訳言語数", "言語コード", "タグ数", "サムネ", "説明文字数",
    ])
    for it in items:
        w.writerow([
            it.get("recorded_at", ""), it.get("vol", ""), it.get("video_name", ""),
            it.get("youtube_url", ""), it.get("title", ""),
            it.get("privacy", ""), it.get("schedule", ""),
            len(it.get("localizations") or []),
            ",".join(it.get("localizations") or []),
            len(it.get("tags") or []),
            "✓" if it.get("has_thumbnail") else "",
            it.get("description_chars") or 0,
        ])
    csv_text = buf.getvalue()
    # BOM つきで Excel/Google Sheets でも文字化けしない
    return Response(
        content=("﻿" + csv_text).encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="youtube_upload_history.csv"'},
    )


@app.delete("/api/youtube/history")
def api_clear_youtube_history():
    if YT_UPLOAD_HISTORY_FILE.exists():
        YT_UPLOAD_HISTORY_FILE.unlink()
    return {"status": "ok"}

# ─── API: Google Flow (画像自動生成) ───

FLOW_SCRIPT = SHARED_BASE / "Python" / "flow_automation.py"


class FlowPromptSuggestRequest(BaseModel):
    context: str                      # "vol.78 の BGM 動画サムネ、lounge jazz、雨の夜" など
    cli: Optional[str] = "claude"


class FlowGenerateRequest(BaseModel):
    prompt: Optional[str] = None
    suggest_context: Optional[str] = None   # prompt が空なら Claude CLI で生成
    video_name: Optional[str] = None        # 指定時は {channel}/{video_name}/flow に保存
    output_dir: Optional[str] = None        # 明示保存先（優先）
    reference_image: Optional[str] = None   # 参照画像パス（任意）
    project_name: Optional[str] = None
    aspect: Optional[str] = "16:9"
    count: Optional[str] = "x4"
    model: Optional[str] = "Nano Banana 2"
    resolution: Optional[str] = "2K"
    headless: Optional[bool] = False


@app.post("/api/flow/login")
async def api_flow_login():
    """Flow の Google ログインを行うブラウザを起動。ユーザが手動ログインするまで待機。"""
    await _ensure_not_running("flow", "Flow が既に実行中です")
    cmd = [sys.executable, "-u", str(FLOW_SCRIPT), "--login-only"]
    task_logs["flow"] = []
    import datetime as _dt
    task_meta["flow"] = {"started_at": _dt.datetime.now().isoformat(), "mode": "login"}
    # PYTHONUNBUFFERED は flow_automation.py の subprocess 検知に使用
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=env, stdin=subprocess.DEVNULL)
    active_tasks["flow"] = proc
    _stream_subprocess(proc, "flow")
    return {"status": "started"}


@app.get("/api/flow/login-status")
def api_flow_login_status():
    """Flow の永続化セッションの存在とログイン有効性の推定値を返す。
    - has_profile: USER_DATA_DIR が存在
    - has_session_cookies: Google アカウント関連の Cookie がある（SID 系）
    - last_modified: プロファイルディレクトリの最終更新時刻 ISO
    - status: "ok" / "stale"（30 日以上未更新）/ "missing"（プロファイル無し）
    """
    profile_dir = HOME / ".flow-playwright-profile"
    if not profile_dir.exists():
        return {
            "status": "missing",
            "has_profile": False,
            "has_session_cookies": False,
            "last_modified": None,
            "message": "Flow に未ログイン。「🔑 ログイン」ボタンで初回セットアップしてください。",
        }
    # Cookie DB から SID / SAPISID 等の存在を確認（厳密ではないが有効性の目安）
    # Chromium プロファイルのバージョンによって場所が異なる（Default/Cookies / Default/Network/Cookies）
    has_cookies = False
    cookie_db_candidates = [
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
    ]
    cookie_db = next((c for c in cookie_db_candidates if c.exists()), None)
    if cookie_db is not None:
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{cookie_db}?mode=ro", uri=True, timeout=2)
            try:
                cur = con.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%.google.com' AND name IN ('SID','SAPISID','HSID','SSID','APISID','__Secure-1PSID')"
                )
                row = cur.fetchone()
                has_cookies = bool(row and row[0] > 0)
            finally:
                con.close()
        except Exception:
            # DB ロック中（ブラウザ実行中）など → ログイン中の可能性が高いので true 扱い
            has_cookies = True
    import datetime as _dt
    try:
        mtime = profile_dir.stat().st_mtime
        last_mod = _dt.datetime.fromtimestamp(mtime).isoformat()
        age_days = (_dt.datetime.now().timestamp() - mtime) / 86400
    except Exception:
        last_mod = None
        age_days = None
    if not has_cookies:
        status = "missing"
        msg = "セッション Cookie が見つかりません。「🔑 ログイン」で再ログインしてください。"
    elif age_days is not None and age_days > 30:
        status = "stale"
        msg = f"セッションが {int(age_days)} 日前のもの。失効している可能性があります。"
    else:
        status = "ok"
        msg = "Flow にログイン済み。"
    return {
        "status": status,
        "has_profile": True,
        "has_session_cookies": has_cookies,
        "last_modified": last_mod,
        "age_days": age_days,
        "message": msg,
    }


@app.post("/api/flow/suggest-prompt")
def api_flow_suggest_prompt(req: FlowPromptSuggestRequest):
    """Claude CLI で Flow 用の画像プロンプトを生成して返す（同期 · 即時）。"""
    try:
        sys.path.insert(0, str(SHARED_BASE / "Python"))
        from flow_automation import suggest_prompt_via_claude
        prompt = suggest_prompt_via_claude(req.context, cli_cmd=req.cli or "claude")
        return {"ok": True, "prompt": prompt}
    except Exception as e:
        raise HTTPException(500, f"Claude プロンプト生成失敗: {e}")


@app.post("/api/flow/generate")
async def api_flow_generate(req: FlowGenerateRequest):
    await _ensure_not_running("flow", "Flow が既に実行中です")

    # 保存先の解決（test_flow.py の 3バッチモードと統一して Image/ 配下に保存）
    out_dir = req.output_dir
    if not out_dir and req.video_name:
        cfg = get_dashboard_config()
        out_dir = str(Path(cfg["channel_folder"]) / req.video_name / "Image")

    # プロジェクト名: 指定 > video_name > 日時
    project_name = req.project_name or req.video_name or "orzz_auto"

    cmd = [sys.executable, "-u", str(FLOW_SCRIPT),
           "--aspect", req.aspect or "16:9",
           "--count", req.count or "x4",
           "--model", req.model or "Nano Banana 2",
           "--resolution", req.resolution or "2K",
           "--project-name", project_name,
           "--no-wait"]
    if req.headless:
        cmd.append("--headless")
    if req.prompt:
        cmd += ["--prompt", req.prompt]
    elif req.suggest_context:
        cmd += ["--suggest-prompt", req.suggest_context]
    else:
        raise HTTPException(400, "prompt または suggest_context が必要です")
    if out_dir:
        cmd += ["--output-dir", out_dir]
    if req.reference_image:
        ref_p = Path(req.reference_image).expanduser()
        if not ref_p.exists():
            raise HTTPException(404, f"参照画像が見つかりません: {ref_p}")
        cmd += ["--reference-image", str(ref_p)]

    task_logs["flow"] = []
    import datetime as _dt
    task_meta["flow"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "mode": "generate",
        "video_name": req.video_name or "",
        "output_dir": out_dir or "",
        "project_name": project_name,
    }
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["flow"] = proc
    _stream_subprocess(proc, "flow")
    return {"status": "started", "output_dir": out_dir, "project_name": project_name}


@app.post("/api/flow/stop")
def api_flow_stop():
    proc = active_tasks.get("flow")
    if proc and proc.returncode is None:
        proc.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}


@app.get("/api/flow/status")
def api_flow_status():
    proc = active_tasks.get("flow")
    running = proc is not None and proc.returncode is None
    logs = task_logs.get("flow", [])
    meta = task_meta.get("flow", {})
    return {"running": running, "logs": logs[-80:], "meta": meta}


# ─── API: Flow テスト (test_flow.py, 3バッチ+WEBアップロード) ───

TEST_FLOW_SCRIPT = SHARED_BASE / "Python" / "test_flow.py"


class FlowTestRunRequest(BaseModel):
    video_name: str
    image_base64: str                       # data:image/png;base64,... または素の base64
    image_filename: Optional[str] = "flow.png"
    prompt1: Optional[str] = None           # 1バッチ目（参照画像ありで生成）
    prompt2: Optional[str] = None           # 2バッチ目（＋パネルから既存アセット採用）
    project_name: Optional[str] = None
    headless: Optional[bool] = False


@app.post("/api/flow/test-run")
async def api_flow_test_run(req: FlowTestRunRequest):
    """test_flow.py を WEB アップロード画像と共に起動。
    保存先は channel_folder/{video_name}/Image/ に自動解決される。"""
    await _ensure_not_running("flow", "Flow が既に実行中です")

    cfg = get_dashboard_config()
    channel = cfg.get("channel_folder") or ""
    if not channel:
        raise HTTPException(400, "channel_folder が未設定です（設定タブで指定してください）")
    video_folder = Path(channel) / req.video_name
    if not video_folder.exists():
        raise HTTPException(404, f"動画フォルダが存在しません: {video_folder}")

    # base64 → 一時ファイル
    import base64, datetime as _dt, tempfile
    payload = req.image_base64
    if "," in payload and payload.strip().startswith("data:"):
        payload = payload.split(",", 1)[1]
    try:
        raw = base64.b64decode(payload)
    except Exception as e:
        raise HTTPException(400, f"image_base64 のデコードに失敗: {e}")
    if not raw:
        raise HTTPException(400, "image_base64 が空です")

    ext = Path(req.image_filename or "flow.png").suffix or ".png"
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    tmp_dir = Path(tempfile.gettempdir())
    upload_path = tmp_dir / f"flow_upload_{req.video_name}_{ts}{ext}"
    upload_path.write_bytes(raw)

    # プロジェクト名未指定時は video_name をそのまま使う
    project_name = (req.project_name or req.video_name).strip()
    cmd = [
        sys.executable, "-u", str(TEST_FLOW_SCRIPT),
        "--video-name", req.video_name,
        "--flow-png", str(upload_path),
        "--project-name", project_name,
    ]
    if req.prompt1:
        cmd += ["--prompt1", req.prompt1]
    if req.prompt2:
        cmd += ["--prompt2", req.prompt2]
    if req.headless:
        cmd.append("--headless")

    task_logs["flow"] = []
    task_meta["flow"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "mode": "test-run",
        "video_name": req.video_name,
        "output_dir": str(video_folder / "Image"),
        "upload_path": str(upload_path),
        "image_bytes": len(raw),
    }
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["flow"] = proc
    _stream_subprocess(proc, "flow")
    return {
        "status": "started",
        "output_dir": str(video_folder / "Image"),
        "upload_path": str(upload_path),
    }


# ─── API: ChatGPT / Codex 画像生成（並列） ───

CODEX_IMAGEGEN_SCRIPT = SHARED_BASE / "Python" / "codex_imagegen.py"


class CodexImagegenRefImage(BaseModel):
    filename: Optional[str] = ""
    data_url: str                              # "data:image/png;base64,..." 形式


class CodexImagegenGenerateRequest(BaseModel):
    prompts: str                              # 1 ブロック 1 件・空行区切り、`...::filename` で個別ファイル名指定可
    video_name: Optional[str] = None          # 指定時は {channel}/{video_name}/Image に保存
    output_dir: Optional[str] = None          # 明示保存先（優先）
    max_parallel: Optional[int] = 4
    timeout_sec: Optional[int] = 900
    aspect_ratio: Optional[str] = "16:9"      # 16:9 / 1:1 / 9:16 / 4:3 / 3:4 → size に直接マップ
    use_benchmark_picked: Optional[bool] = False  # benchmark picked サムネを参照画像として渡す
    reference_images_b64: Optional[List[CodexImagegenRefImage]] = None  # 直接アップロードされた参照画像群
    # gpt-image-2 の追加パラメータ
    quality: Optional[str] = "medium"         # low / medium / high / auto
    n_per_prompt: Optional[int] = 1           # 1 リクエストあたりの生成枚数 (1-10)
    background: Optional[str] = "auto"        # auto / transparent / opaque
    moderation: Optional[str] = "auto"        # auto / low
    input_fidelity: Optional[str] = "high"    # edits 用: high / low
    output_format: Optional[str] = "png"      # png / jpeg / webp


class CodexImagegenBuild5ERequest(BaseModel):
    """参照画像（アップロード or picked）を Claude Vision でリアルタイム分析して 5要素を抽出 →
    Lighting / Camera / Style をバリエーション巡回した N 件のプロンプトを返す。
    参照画像が 1 枚もない場合のみ、ベンチマーク (thumbnail_aggregate) のキャッシュ文章にフォールバック。"""
    video_name: Optional[str] = None
    n: Optional[int] = 4
    concept_hint: Optional[str] = ""
    include_text_overlay: Optional[bool] = False
    filename_prefix: Optional[str] = ""
    use_benchmark_picked: Optional[bool] = True
    reference_images_b64: Optional[List[CodexImagegenRefImage]] = None


class CodexImagegenSuggestRequest(BaseModel):
    context: str
    count: Optional[int] = 4
    provider: Optional[str] = "codex"
    cli: Optional[str] = None


def _run_codex_text_prompt(cli_cmd: str, prompt: str, timeout: int = 300) -> str:
    """Run Codex CLI non-interactively and return the final assistant message."""
    import tempfile

    fd, out_path = tempfile.mkstemp(prefix="orzz_codex_prompt_", suffix=".txt")
    os.close(fd)
    try:
        proc = subprocess.run(
            [
                shutil.which(cli_cmd) or cli_cmd,
                "exec",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--output-last-message",
                out_path,
                "-",
            ],
            input=prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:500])
        try:
            text = Path(out_path).read_text(encoding="utf-8")
        except Exception:
            text = ""
        return text or proc.stdout or ""
    finally:
        try:
            Path(out_path).unlink()
        except Exception:
            pass


@app.post("/api/codex-imagegen/generate")
async def api_codex_imagegen_generate(req: CodexImagegenGenerateRequest):
    await _ensure_not_running("codex_imagegen", "Codex 画像生成が既に実行中です")

    out_dir = req.output_dir
    if not out_dir and req.video_name:
        cfg = get_dashboard_config()
        out_dir = str(Path(cfg["channel_folder"]) / req.video_name / "Image")
    if not out_dir:
        raise HTTPException(400, "output_dir または video_name が必要です")

    prompts_text = (req.prompts or "").strip()
    if not prompts_text:
        raise HTTPException(400, "prompts が空です")

    # アスペクト比 → gpt-image-2 の標準 size に直接マップ (プロンプト文への前置注入は廃止)
    aspect = (req.aspect_ratio or "16:9").strip()
    if aspect not in ("16:9", "1:1", "9:16", "4:3", "3:4"):
        aspect = "16:9"

    quality = (req.quality or "medium").strip()
    if quality not in ("low", "medium", "high", "auto"):
        quality = "medium"
    background = (req.background or "auto").strip()
    if background not in ("auto", "transparent", "opaque"):
        background = "auto"
    moderation = (req.moderation or "auto").strip()
    if moderation not in ("auto", "low"):
        moderation = "auto"
    input_fidelity = (req.input_fidelity or "high").strip()
    if input_fidelity not in ("high", "low"):
        input_fidelity = "high"
    output_format = (req.output_format or "png").strip()
    if output_format not in ("png", "jpeg", "webp"):
        output_format = "png"
    n_per_prompt = max(1, min(10, int(req.n_per_prompt or 1)))

    # 1 行 1 件のプロンプトを stdin で渡す（並列は 1〜4 に制限）
    parallel = max(1, min(4, int(req.max_parallel or 4)))
    cmd = [
        sys.executable, "-u", str(CODEX_IMAGEGEN_SCRIPT),
        "--output-dir", out_dir,
        "--max-parallel", str(parallel),
        "--timeout", str(max(60, int(req.timeout_sec or 900))),
        "--aspect", aspect,
        "--quality", quality,
        "--background", background,
        "--moderation", moderation,
        "--input-fidelity", input_fidelity,
        "--output-format", output_format,
        "--n", str(n_per_prompt),
    ]
    reference_names: list[str] = []
    if req.use_benchmark_picked:
        try:
            import app_benchmark_thumbnail as _bt
            # picked は複数選択可。Codex CLI 経路でも全枚数を分析対象として渡す
            # （実用上のキャップは 4 — Codex のプロンプト長と分析時間を考慮）
            picked_paths = _bt.get_picked_paths(limit=4)
            for p in picked_paths:
                cmd += ["--reference-image", p]
                reference_names.append(Path(p).name)
        except Exception:
            pass

    # 直接アップロードされた参照画像群を一時ファイル化して --reference-image に追加（複数選択可）
    uploaded_refs_tmp: list[Path] = []
    if req.reference_images_b64:
        import base64 as _b64
        import tempfile as _tempfile
        for idx, ref in enumerate(req.reference_images_b64[:8]):  # 安全側で 8 枚まで
            try:
                du = (ref.data_url or "").strip()
                if "," in du and du.startswith("data:"):
                    header, _, b64body = du.partition(",")
                    # ex: data:image/png;base64
                    mime = "image/png"
                    if header.startswith("data:") and ";" in header:
                        mime = header.split(":", 1)[1].split(";", 1)[0] or mime
                    ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                               "image/webp": ".webp", "image/gif": ".gif"}
                    ext = ext_map.get(mime, ".png")
                    raw = _b64.b64decode(b64body)
                else:
                    # 純粋な base64 のみのケース
                    raw = _b64.b64decode(du)
                    ext = Path(ref.filename or "").suffix or ".png"
                # 安全なファイル名
                safe_stem = re.sub(r"[^\w.\-]+", "_", Path(ref.filename or f"upload-{idx+1}").stem)[:48] or f"upload-{idx+1}"
                tf = _tempfile.NamedTemporaryFile(prefix=f"codex_ref_{safe_stem}_", suffix=ext, delete=False)
                tf.write(raw)
                tf.close()
                tmp_path = Path(tf.name)
                uploaded_refs_tmp.append(tmp_path)
                cmd += ["--reference-image", str(tmp_path)]
                reference_names.append(ref.filename or tmp_path.name)
            except Exception as e:
                # 1 枚壊れても他は処理続行
                continue

    task_logs["codex_imagegen"] = []
    import datetime as _dt
    line_count = sum(1 for ln in prompts_text.splitlines() if ln.strip() and not ln.strip().startswith("#"))
    task_meta["codex_imagegen"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "video_name": req.video_name or "",
        "output_dir": out_dir,
        "prompt_count": line_count,
        "max_parallel": parallel,
        "aspect_ratio": aspect,
        "quality": quality,
        "n_per_prompt": n_per_prompt,
        "background": background,
        "moderation": moderation,
        "input_fidelity": input_fidelity,
        "output_format": output_format,
        "reference_images": reference_names,
    }
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    # stdin に流し込んで close（codex_imagegen.py は stdin から読み取る）
    try:
        proc.stdin.write(prompts_text + "\n")
        proc.stdin.close()
    except Exception:
        pass
    active_tasks["codex_imagegen"] = proc
    _stream_subprocess(proc, "codex_imagegen")
    return {
        "status": "started",
        "output_dir": out_dir,
        "prompt_count": line_count,
    }


@app.get("/api/codex-imagegen/status")
def api_codex_imagegen_status():
    proc = active_tasks.get("codex_imagegen")
    running = proc is not None and proc.returncode is None
    logs = task_logs.get("codex_imagegen", [])
    meta = task_meta.get("codex_imagegen", {})
    return {"running": running, "logs": logs[-120:], "meta": meta}


@app.post("/api/codex-imagegen/stop")
def api_codex_imagegen_stop():
    proc = active_tasks.get("codex_imagegen")
    if proc and proc.returncode is None:
        proc.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}


# ─── API: 背景画像生成（パイプライン STEP 3 / Premiere 自動配置の直前） ───
#
# `step_bgimage` の via_api 分岐 / UI「背景画像」カード / 単発 CLI 呼び出しが共通で叩く endpoint。
# 排他制御は task key="bgimage"。並列 Codex 画像生成（"codex_imagegen"）とは別キーなので、
# UI から手動 Codex を走らせている裏でパイプラインが bgimage を走らせると 両方走ってしまうが、
# 物理出力先（vol{N}.png は動画フォルダ直下、Codex は通常 Image/ 配下）が衝突しないため許容する。

class BgImageRunRequest(BaseModel):
    video_name: str
    ref_count: Optional[int] = 3
    force: Optional[bool] = False
    timeout_sec: Optional[int] = 1800
    reference_image: Optional[str] = None


@app.post("/api/bgimage/run")
async def api_bgimage_run(req: BgImageRunRequest):
    """背景画像 vol{N}.png を生成（ベンチマーク参照 + チャンネルコンセプト）。

    `app_pipeline.py --only bgimage --via-api` および UI の「背景画像」カードから呼ばれる。
    内部的には app_pipeline.py を `--only bgimage` で起動し、subprocess の stdout を
    task_logs["bgimage"] に流し込む。via_api を立てずに起動するので無限ループしない。
    """
    await _ensure_not_running("bgimage", "背景画像生成が既に実行中です")
    config = get_dashboard_config()
    folder = Path(config["channel_folder"]) / req.video_name
    if not folder.exists():
        raise HTTPException(404, f"動画フォルダが見つかりません: {req.video_name}")

    # vol 番号抽出
    m = re.match(r"^(\d+)_", req.video_name)
    if not m:
        raise HTTPException(400, f"video_name から vol 番号が抽出できません: {req.video_name}")
    vol = m.group(1)

    # 既存ファイル検出（force=False のときの早期 ok）
    existing = list(folder.glob(f"vol{vol}.png")) + list(folder.glob(f"vol{vol}.jpg"))
    if existing and not req.force:
        # task_logs にも残しておく（UI でリプレイ可能に）
        task_logs["bgimage"] = [
            f"[skip] 既存 {existing[0].name} あり、生成スキップ（force=true で上書き再生成）",
            f"[完了] 終了コード: 0",
        ]
        import datetime as _dt
        task_meta["bgimage"] = {
            "started_at": _dt.datetime.now().isoformat(),
            "video_name": req.video_name,
            "output": existing[0].name,
            "skipped": True,
        }
        return {
            "status": "ok",
            "output": existing[0].name,
            "refs": [],
            "skipped": True,
        }

    # 子プロセス（app_pipeline.py --only bgimage） を起動。
    # via_api=False で起動するため、step_bgimage は CLI 経路を走り subprocess 内で codex_imagegen.py を叩く。
    # アクティブチャンネルの per-channel 設定（persona / rival_channels / reference_image_dir）を
    # 子プロセスに見せるため APP_CHANNEL_FOLDER を明示的に env に立てる
    # （これがないと subprocess の _load_dashboard_config は per-channel をマージしない）。
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    ch_folder = (config.get("channel_folder") or "").strip()
    if ch_folder:
        env["APP_CHANNEL_FOLDER"] = ch_folder
    ch_name = (config.get("channel_name") or "").strip()
    if ch_name:
        env["APP_CHANNEL_NAME"] = ch_name
    try:
        env["APP_BGIMAGE_REFCOUNT"] = str(max(1, int(req.ref_count or 3)))
    except (TypeError, ValueError):
        env["APP_BGIMAGE_REFCOUNT"] = "3"
    try:
        env["APP_BGIMAGE_TIMEOUT_SEC"] = str(max(60, int(req.timeout_sec or 1800)))
    except (TypeError, ValueError):
        env["APP_BGIMAGE_TIMEOUT_SEC"] = "1800"
    ref_image = (req.reference_image or config.get("reference_image") or "").strip()
    if ref_image:
        env["APP_BGIMAGE_REFERENCE_IMAGE"] = ref_image
    if req.force:
        env["APP_BGIMAGE_FORCE"] = "1"
    cmd = [
        sys.executable, "-u", str(PIPELINE_SCRIPT),
        vol, "--only", "bgimage",
    ]

    task_logs["bgimage"] = []
    import datetime as _dt
    task_meta["bgimage"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "video_name": req.video_name,
        "vol": vol,
        "ref_count": int(env["APP_BGIMAGE_REFCOUNT"]),
        "timeout_sec": int(env["APP_BGIMAGE_TIMEOUT_SEC"]),
        "reference_image": env.get("APP_BGIMAGE_REFERENCE_IMAGE", ""),
        "force": bool(req.force),
        "skipped": False,
    }
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, env=env,
    )
    active_tasks["bgimage"] = proc
    _stream_subprocess(proc, "bgimage")

    return {
        "status": "started",
        "output": f"vol{vol}.png",
        "refs": [],  # ref 一覧は子プロセスのログから事後に確認可能
        "skipped": False,
    }


@app.get("/api/bgimage/status")
def api_bgimage_status():
    """背景画像生成の進捗 + 末尾ログを返す。`step_bgimage` の via_api 分岐がポーリングする。"""
    proc = active_tasks.get("bgimage")
    running = proc is not None and proc.returncode is None
    logs = task_logs.get("bgimage", [])
    meta = task_meta.get("bgimage", {})
    return {"running": running, "logs": logs[-200:], "meta": meta}


@app.post("/api/bgimage/stop")
def api_bgimage_stop():
    proc = active_tasks.get("bgimage")
    if proc and proc.returncode is None:
        proc.terminate()
        return {"status": "stopped"}
    return {"status": "not_running"}


# ─── step_bgimage: 参照画像フォルダ（per-channel UI 連携） ───
_REF_IMG_EXTS = ("*.jpg", "*.jpeg", "*.png", "*.webp")


def _bgimage_reference_dir() -> Optional[Path]:
    """アクティブチャンネルの reference_image_dir を Path で返す。未設定 / 不在なら None。"""
    raw = (get_dashboard_config().get("reference_image_dir") or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if p.is_dir() else None


def _bgimage_reference_files(p: Path) -> list[Path]:
    files: list[Path] = []
    for pat in _REF_IMG_EXTS:
        files.extend(p.glob(pat))
    # ファイル名順で安定化（プレビュー先頭表示の再現性のため）
    files.sort(key=lambda x: x.name.lower())
    return files


@app.get("/api/bgimage/reference-dir/list")
def api_bgimage_reference_dir_list(limit: int = 6):
    """アクティブチャンネルの reference_image_dir 内の画像数とサムネ先頭 N 件のファイル名を返す。"""
    raw = (get_dashboard_config().get("reference_image_dir") or "").strip()
    if not raw:
        return {"ok": True, "configured": False, "exists": False, "count": 0, "files": [], "path": ""}
    p = Path(raw).expanduser()
    if not p.is_dir():
        return {"ok": True, "configured": True, "exists": False, "count": 0, "files": [], "path": str(p),
                "error": "ディレクトリが存在しません"}
    files = _bgimage_reference_files(p)
    return {
        "ok": True,
        "configured": True,
        "exists": True,
        "count": len(files),
        "files": [f.name for f in files[:max(1, min(int(limit or 6), 48))]],
        "path": str(p),
    }


@app.get("/api/bgimage/reference-dir/thumb/{filename:path}")
def api_bgimage_reference_dir_thumb(filename: str):
    """reference_image_dir 内の画像を返す。フォルダ外へのパス越境は拒否。"""
    p = _bgimage_reference_dir()
    if not p:
        raise HTTPException(404, "reference_image_dir 未設定 / 不在")
    # パス越境防止: realpath で resolve したものが reference_image_dir 配下であることを確認
    try:
        target = (p / filename).resolve()
        root = p.resolve()
        target.relative_to(root)
    except Exception:
        raise HTTPException(403, "範囲外のパスです")
    if not target.is_file():
        raise HTTPException(404, "ファイルが見つかりません")
    suffix = target.suffix.lower()
    media = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".webp": "image/webp",
    }.get(suffix, "application/octet-stream")
    return FileResponse(str(target), media_type=media)


@app.post("/api/bgimage/reference-dir/dry-run")
def api_bgimage_reference_dir_dry_run(count: int = 3):
    """app_pipeline.step_bgimage と同じロジックで「どの 3 枚が選ばれるか」を返す（ファイルは作らない）。
    アクティブチャンネルの reference_image_dir を最優先、無ければフォールバック理由を返す。"""
    import random as _r
    n = max(1, min(int(count or 3), 12))
    cfg = get_dashboard_config()
    raw = (cfg.get("reference_image_dir") or "").strip()
    result = {"ok": True, "source": None, "selected": [], "pool_size": 0, "path": "", "note": ""}
    if raw:
        p = Path(raw).expanduser()
        if p.is_dir():
            pool = _bgimage_reference_files(p)
            result["path"] = str(p)
            result["pool_size"] = len(pool)
            if pool:
                pool_shuffled = pool[:]
                _r.shuffle(pool_shuffled)
                picked = pool_shuffled[:n]
                result["source"] = "reference_image_dir"
                result["selected"] = [{"name": x.name, "path": str(x)} for x in picked]
                result["note"] = f"reference_image_dir から {len(picked)}/{n} 枚（プール {len(pool)} 枚）"
                return result
            result["note"] = f"reference_image_dir に画像が 0 枚 — フォールバックします: {p}"
        else:
            result["note"] = f"reference_image_dir が存在しません — フォールバックします: {p}"
    else:
        result["note"] = "reference_image_dir 未設定 — Picked → rival thumbs にフォールバックします"
    # フォールバック先の概要だけ返す（dry-run なので実選択はしない）
    fallback = {"picked_available": False, "rival_pool_size": 0}
    try:
        from app_benchmark_thumbnail import get_picked_paths  # type: ignore
        picked = get_picked_paths(limit=n) or []
        picked = [p for p in picked if Path(p).exists()]
        fallback["picked_available"] = bool(picked)
        fallback["picked_count"] = len(picked)
    except Exception:
        fallback["picked_available"] = False
    # rival thumbs プール（CONFIG_DIR/benchmark/thumbs/<UCxxx>/*.{jpg,jpeg,png}）
    try:
        pool: list[Path] = []
        for url in (cfg.get("rival_channels") or []):
            m = re.search(r"channel/(UC[A-Za-z0-9_-]+)", str(url))
            if not m:
                continue
            ch_id = m.group(1)
            bench_dir = CONFIG_DIR / "benchmark" / "thumbs" / ch_id
            if bench_dir.exists():
                pool.extend(list(bench_dir.glob("*.jpg")) + list(bench_dir.glob("*.jpeg")) + list(bench_dir.glob("*.png")))
        fallback["rival_pool_size"] = len(pool)
    except Exception:
        pass
    result["fallback"] = fallback
    return result


@app.post("/api/codex-imagegen/suggest-prompts")
def api_codex_imagegen_suggest_prompts(req: CodexImagegenSuggestRequest):
    """CLI provider で複数の画像プロンプト案を生成して返す（1 行 1 件）。"""
    provider = (req.provider or "codex").strip().lower()
    if provider not in ("codex", "claude"):
        raise HTTPException(400, f"未対応 provider: {provider}")
    default_cli = get_suno_config().get("codex_cli" if provider == "codex" else "claude_cli") or provider
    cli_cmd = (req.cli or default_cli).strip() or default_cli
    count = max(1, min(int(req.count or 4), 12))
    instruction = (
        f"次のコンテキストから、画像生成 AI 向けの英語プロンプト案を厳密に {count} 行（1 行 1 件）出力してください。"
        " 番号・記号・前置きを含めず、各行は完結した英語プロンプトのみ。"
        " 各案はバリエーション（構図 / 時間帯 / カメラアングル / 色調）を変えてください。\n\n"
        f"コンテキスト: {req.context}"
    )
    try:
        if provider == "codex":
            text = _run_codex_text_prompt(cli_cmd, instruction, timeout=300)
        else:
            # claude provider: 共通ランナーで Claude→Codex 自動フォールバック
            from app_llm_runner import run_llm
            text = run_llm(instruction, cli_cmd=cli_cmd, timeout=180, label="image-prompt-gen")
    except FileNotFoundError:
        raise HTTPException(500, f"{cli_cmd} CLI が見つかりません")
    except subprocess.TimeoutExpired:
        raise HTTPException(504, f"{provider} CLI タイムアウト")
    except RuntimeError as e:
        raise HTTPException(500, f"{provider} CLI 失敗: {str(e)[:300]}")
    text = (text or "").strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    # よくある番号付き先頭（"1. ..." / "- ..."）を剥がす
    cleaned: list[str] = []
    import re as _re
    for ln in lines:
        ln2 = _re.sub(r"^\s*(?:\d+[\.\)]\s*|[-*•]\s*)", "", ln)
        if ln2:
            cleaned.append(ln2)
    if not cleaned:
        raise HTTPException(500, "プロンプト案が空でした")
    return {"ok": True, "prompts": cleaned[:count], "raw": text}


def _b64_ref_to_tmp(ref: "CodexImagegenRefImage", idx: int) -> Optional[Path]:
    """data_url を一時ファイルに保存して Path を返す。失敗時は None。"""
    import base64 as _b64
    import tempfile as _tempfile
    try:
        du = (ref.data_url or "").strip()
        if "," in du and du.startswith("data:"):
            header, _, b64body = du.partition(",")
            mime = "image/png"
            if header.startswith("data:") and ";" in header:
                mime = header.split(":", 1)[1].split(";", 1)[0] or mime
            ext_map = {"image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
                       "image/webp": ".webp", "image/gif": ".gif"}
            ext = ext_map.get(mime, ".png")
            raw = _b64.b64decode(b64body)
        else:
            raw = _b64.b64decode(du)
            ext = Path(ref.filename or "").suffix or ".png"
        safe_stem = re.sub(r"[^\w.\-]+", "_", Path(ref.filename or f"upload-{idx+1}").stem)[:48] or f"upload-{idx+1}"
        tf = _tempfile.NamedTemporaryFile(prefix=f"codex_5e_ref_{safe_stem}_", suffix=ext, delete=False)
        tf.write(raw)
        tf.close()
        return Path(tf.name)
    except Exception:
        return None


def _build_5e_vision_prompt(image_paths: list[Path], concept_hint: str,
                            channel_name: str = "", persona: str = "") -> str:
    image_block = "\n".join(f"  - {p}" for p in image_paths)
    hint_block = concept_hint.strip() or "(指定なし → 画像から最適な被写体カテゴリを推測してください)"

    # チャンネル文脈ブロック: persona があれば使い、無ければ Claude にジャンル推測を禁止する
    persona_clean = (persona or "").strip()
    name_clean = (channel_name or "").strip()
    if persona_clean:
        channel_block = (
            f"## ユーザーのチャンネル文脈（translate_to_self はここに引き寄せる）\n"
            f"- チャンネル名: {name_clean or '(不明)'}\n"
            f"- ペルソナ／コンセプト:\n{persona_clean}\n\n"
            f"translate_to_self はこの persona の文脈に翻訳してください。"
        )
        translate_rule = "translate_to_self はチャンネルの persona 文脈に合わせた具体的翻訳を記述する"
    else:
        channel_block = (
            f"## ユーザーのチャンネル文脈\n"
            f"- チャンネル名: {name_clean or '(不明)'}\n"
            f"- ペルソナ／コンセプト: **未設定**\n\n"
            f"**重要 — ジャンル推測の禁止**: persona が未設定のため、"
            f"具体的なジャンル名（jazz / lounge / lo-fi / chill / ambient / piano / "
            f"healing / sleep など）や音楽カテゴリを **絶対に勝手に推測しないでください**。"
            f"参照画像の競合サムネが BGM 系であっても、ユーザーのチャンネルが BGM 系とは限りません。"
        )
        translate_rule = (
            "translate_to_self はジャンル中立的な汎用翻訳指針として記述する。"
            "例: 「象徴オブジェクトに置換」「色温度を自チャンネルのコア時間帯に合わせて調整」"
            "「人物を抽象シルエット化」「固有テロップを汎用ミニマル日本語1語に置換」。"
            "「Lounge Jazz BGM」「Jazz の文脈に合わせ」のような具体ジャンル名を含む文は禁止"
        )

    return f"""あなたは YouTube サムネイル分析の専門家です。
以下の参照画像を **必ず Read ツールで開いて視覚的に分析**し、要素を抽出してください。
分析を省略・推測のみで回答することは禁止です。

## 分析対象（必ず Read で読み込むこと）
{image_block}

## ユーザーが想定する被写体／文脈
{hint_block}

{channel_block}

## 抽出ルール
- 各画像を実際に見て、視覚要素を言語化してください。
- コピー目的ではありません — 「効いている抽象要素」だけを抽出。
- 「構成要素 / ライティング / あなたが視聴者の注目を引くと判断したポイント」の 3 点は重点的に抽出。
- 固有要素（人物の顔・ロゴ・商標・キャラクター・チャンネル名・特徴的な小道具・テロップ）は再現せず、
  「訴えられない程度」「元画像と同一視されない程度」に翻訳する指針として記述してください。
- {translate_rule}
- 出力はすべて自然な日本語または短い英語フレーズで。

## 出力（単一 JSON / 余計な文章なし）
```json
{{
  "subject": "被写体・主役として効いているもの（user hint があれば最優先で使用、無ければ画像から抽出）",
  "background_context": "背景とコンテキスト・空間感",
  "lighting": "ライティング・時間帯・光源方向・色温度・コントラスト",
  "style_rendering": "画風・質感・レンダリング・色調",
  "camera_composition": "カメラ視点・構図・焦点・配置パターン",
  "attention_hooks": ["クリックを誘う注目ポイント1", "..."],
  "shared_palette": ["支配色1", "支配色2"],
  "avoid": ["コピーすべきでない固有要素（顔・ロゴ・チャンネル名・テロップ等）"],
  "translate_to_self": ["元画像の要素をどう翻訳するか（ジャンル名を含めないこと）"]
}}
```"""


def _vision_to_thumbnail_axis(vobj: dict) -> dict:
    """Vision 出力 JSON を build_5element_prompts が読める形に整形。"""
    if not isinstance(vobj, dict):
        return {}
    return {
        "shared_palette": vobj.get("shared_palette"),
        "shared_composition": vobj.get("camera_composition"),
        "shared_atmosphere": vobj.get("background_context"),
        "element_extraction": {
            "subjects": [vobj.get("subject")] if vobj.get("subject") else [],
            "composition": vobj.get("camera_composition"),
            "lighting": vobj.get("lighting"),
            "color_palette": vobj.get("shared_palette"),
            "atmosphere": vobj.get("background_context"),
        },
        "recommendation_for_self": {
            "keep": [vobj.get("subject")] if vobj.get("subject") else [],
            "transform": vobj.get("translate_to_self") or [],
            "vibe_one_line": vobj.get("background_context") or "",
        },
        "viewer_hooks": vobj.get("attention_hooks") or [],
        "avoid": vobj.get("avoid") or [],
        "adaptation_hints": {
            "transform": vobj.get("translate_to_self") or [],
            "avoid": vobj.get("avoid") or [],
        },
    }


@app.post("/api/codex-imagegen/build-5element")
def api_codex_imagegen_build_5element(req: CodexImagegenBuild5ERequest):
    """参照画像（アップロード + benchmark picked）を **Claude Vision でリアルタイム分析**し、
    その結果から 5要素プロンプトを N 件構築する。
    参照画像が 1 枚もない場合のみ thumbnail.json のキャッシュ aggregate にフォールバック。"""
    import app_image_prompt as _ip
    import app_benchmark_thumbnail as _bt

    # ─── 1) 参照画像を集める（アップロード優先 → picked 追加） ───────
    ref_paths: list[Path] = []
    uploaded_tmps: list[Path] = []
    ref_names: list[str] = []

    if req.reference_images_b64:
        for idx, ref in enumerate(req.reference_images_b64[:6]):  # Vision 分析の現実的キャップ
            tmp = _b64_ref_to_tmp(ref, idx)
            if tmp:
                ref_paths.append(tmp)
                uploaded_tmps.append(tmp)
                ref_names.append(ref.filename or tmp.name)

    if req.use_benchmark_picked:
        try:
            for p in (_bt.get_picked_paths(limit=4) or []):
                pp = Path(p)
                if pp.exists() and pp not in ref_paths:
                    ref_paths.append(pp)
                    ref_names.append(pp.name)
        except Exception:
            pass

    thumbnail_axis: dict = {}
    source: str = ""
    vision_raw: str = ""
    vision_obj: dict = {}
    vision_error: str = ""

    # ─── 2) 参照画像があれば Claude Vision でリアルタイム分析（必須実行） ───
    if ref_paths:
        suno_cfg = get_suno_config()
        cli_cmd = (suno_cfg.get("claude_cli") or "claude").strip()
        # チャンネル文脈を渡す（persona が空なら Vision にジャンル推測を禁止させる）
        dash_cfg = get_dashboard_config()
        channel_name = (dash_cfg.get("channel_name") or "").strip()
        persona = (dash_cfg.get("persona") or "").strip()
        prompt = _build_5e_vision_prompt(
            ref_paths,
            (req.concept_hint or "").strip(),
            channel_name=channel_name,
            persona=persona,
        )
        try:
            vision_raw = _bt._run_claude_vision(cli_cmd, prompt, ref_paths, timeout=300)
            vision_obj = _bt._extract_json(vision_raw) or {}
            if vision_obj:
                thumbnail_axis = _vision_to_thumbnail_axis(vision_obj)
                source = f"vision_realtime({len(ref_paths)} images)"
            else:
                vision_error = "JSON 抽出失敗"
        except FileNotFoundError:
            vision_error = f"{cli_cmd} CLI が見つかりません"
        except RuntimeError as e:
            vision_error = str(e)[:200]
        except Exception as e:
            vision_error = f"{type(e).__name__}: {str(e)[:160]}"

    # ─── 3) Vision が無理だった場合のフォールバック（キャッシュ aggregate） ───
    if not thumbnail_axis:
        try:
            thumb_cache = _bt.load_cache() or {}
        except Exception:
            thumb_cache = {}
        analysis = (thumb_cache.get("analysis") or {}) if isinstance(thumb_cache, dict) else {}
        agg = analysis.get("aggregate") if isinstance(analysis, dict) else None
        if isinstance(agg, dict) and agg:
            notes = agg.get("gpt_image2_prompt_notes") or {}
            thumbnail_axis = {
                "shared_palette": agg.get("shared_palette"),
                "shared_composition": agg.get("shared_composition"),
                "shared_atmosphere": agg.get("vibe_one_line"),
                "recommendation_for_self": agg.get("recommendation_for_self") or {},
                "element_extraction": {
                    "subjects": [notes.get("subject")] if notes.get("subject") else [],
                    "composition": notes.get("camera_composition"),
                    "lighting": notes.get("lighting"),
                    "color_palette": agg.get("shared_palette"),
                    "atmosphere": notes.get("background_context"),
                },
                "avoid": notes.get("avoid"),
            }
            source = source or "thumbnail_aggregate(cache_fallback)"
        else:
            per = analysis.get("per_channel") if isinstance(analysis, dict) else None
            if isinstance(per, dict) and per:
                first = next(iter(per.values()), None) or {}
                br = first.get("five_element_breakdown") or {}
                thumbnail_axis = {
                    "shared_palette": first.get("common_palette"),
                    "shared_composition": first.get("common_composition"),
                    "element_extraction": {
                        "subjects": first.get("common_subjects") or [],
                        "composition": br.get("camera_composition"),
                        "lighting": br.get("lighting"),
                        "color_palette": first.get("common_palette"),
                        "atmosphere": br.get("background_context"),
                    },
                    "viewer_hooks": first.get("viewer_hooks"),
                    "avoid": first.get("avoid"),
                }
                source = source or "thumbnail_per_channel(cache_fallback)"
            else:
                source = source or "default_fallback"

    # ─── 4) 競合分析キャッシュ（任意・visual_direction だけ薄く参照） ───
    competitor_analysis: dict = {}
    try:
        import app_competitor as _ac
        competitor_analysis = _ac.load_cache() or {}
    except Exception:
        competitor_analysis = {}

    # ─── 5) 出力フォルダの既存ファイルをスキャンして v 番号オフセットを決定 ───
    prefix_for_scan = (req.filename_prefix or req.video_name or "").strip()
    start_index = 1
    existing_count = 0
    scanned_dir = ""
    if prefix_for_scan and req.video_name:
        try:
            cfg = get_dashboard_config()
            image_dir = Path(cfg["channel_folder"]) / req.video_name / "Image"
            if image_dir.is_dir():
                # prefix-v{NUM}.{ext} または prefix-v{NUM}-{m}.{ext} (n_per_prompt > 1 で生成された連番)
                # prefix の末尾は app_image_prompt 側で 20 文字に切られるので、同じく切ったものでマッチ
                truncated = prefix_for_scan[:20].rstrip("-") or "image"
                pat = re.compile(
                    rf"^{re.escape(truncated)}-v(\d+)(?:-\d+)?\.(?:png|jpe?g|webp)$",
                    re.IGNORECASE,
                )
                max_v = 0
                for f in image_dir.iterdir():
                    if not f.is_file():
                        continue
                    m = pat.match(f.name)
                    if m:
                        existing_count += 1
                        try:
                            max_v = max(max_v, int(m.group(1)))
                        except ValueError:
                            pass
                start_index = max_v + 1 if max_v > 0 else 1
                scanned_dir = str(image_dir)
        except Exception:
            pass

    # ─── 6) 5要素プロンプト N 件をビルド ───
    n = max(1, min(int(req.n or 4), 8))
    items = _ip.build_5element_prompts(
        thumbnail_axis=thumbnail_axis,
        competitor_analysis=competitor_analysis,
        concept_hint=(req.concept_hint or "").strip(),
        n=n,
        include_text_overlay=bool(req.include_text_overlay),
        filename_prefix=prefix_for_scan,
        start_index=start_index,
    )

    # ─── 7) 一時ファイル後始末 ───
    for t in uploaded_tmps:
        try:
            t.unlink()
        except Exception:
            pass

    return {
        "ok": True,
        "items": items,
        "as_lines": "\n\n".join(f"{it['prompt']}::{it['filename']}" for it in items),
        "reference_images": ref_names,
        "reference_count": len(ref_paths),
        "source": source,
        "start_index": start_index,
        "existing_count": existing_count,
        "scanned_dir": scanned_dir,
        "vision_raw": vision_raw[:4000] if vision_raw else "",
        "vision_extracted": vision_obj,
        "vision_error": vision_error,
    }


# ─── API: チャンネル動画横断・サムネ一括生成 ─────────

class ChannelThumbnailPlanRequest(BaseModel):
    video_names: List[str]
    max_competitors_per_video: Optional[int] = 4
    use_self_stats: Optional[bool] = True       # Phase B: 自動画 YouTube Data API 数値を取得


class ChannelThumbnailStartRequest(BaseModel):
    video_names: List[str]
    provider: Optional[str] = "codex"           # "codex" | "flow"
    n_per_video: Optional[int] = 4
    max_competitors_per_video: Optional[int] = 4
    use_self_stats: Optional[bool] = True
    concept_hint_override: Optional[str] = ""
    aspect: Optional[str] = "16:9"
    quality: Optional[str] = "medium"
    include_text_overlay: Optional[bool] = False
    background: Optional[str] = "auto"
    output_format: Optional[str] = "png"
    timeout_sec: Optional[int] = 900
    max_parallel: Optional[int] = 4
    retry_failed_only: Optional[bool] = False   # Phase C: 直前 run の失敗のみ再実行
    # 自動承認設定 (.md 仕様書 §7.4, §10)
    approval_mode: Optional[str] = "conditional"   # "manual" | "conditional" | "auto"
    score_threshold: Optional[int] = 80
    ctr_threshold: Optional[float] = 7.0
    similarity_max: Optional[int] = 25
    auto_score: Optional[bool] = True              # 生成完了後に Vision スコアリングを自動実行


class ThumbnailApproveRequest(BaseModel):
    video_name: str
    filename: str
    status: str                                     # "adopted" | "rejected" | "needs_review"
    reason: Optional[str] = ""


class ThumbnailSettingsRequest(BaseModel):
    video_name: str
    approval_mode: Optional[str] = None
    score_threshold: Optional[int] = None
    ctr_threshold: Optional[float] = None
    similarity_max: Optional[int] = None


class ThumbnailRescoreRequest(BaseModel):
    video_name: str
    filenames: Optional[List[str]] = None           # 指定なら指定分のみ。None なら全件
    approval_mode: Optional[str] = None             # 同時に承認モード上書き
    score_threshold: Optional[int] = None
    ctr_threshold: Optional[float] = None
    similarity_max: Optional[int] = None


def _ct_collect_video_context(video_name: str) -> Optional[dict]:
    """video_name から動画ディレクトリと context を集める。"""
    import app_channel_thumbnail as _ct
    cfg = get_dashboard_config()
    ch_folder = Path(cfg.get("channel_folder", "")).expanduser()
    if not ch_folder.is_dir():
        return None
    video_dir = ch_folder / video_name
    if not video_dir.is_dir():
        return None
    return _ct.read_video_context(video_dir)


def _ct_fetch_self_stats(video_ids: list[str]) -> dict:
    """video_id → YouTube Data API statistics の dict を返す。"""
    if not video_ids:
        return {}
    try:
        import app_competitor as _comp
        api_key = _comp._resolve_youtube_api_key()
        if not api_key:
            return {}
        details = _comp._fetch_video_details_by_key(video_ids, api_key)
        return {d.get("video_id"): d for d in details if d.get("video_id")}
    except Exception:
        return {}


def _ct_build_plan(video_names: list[str], *,
                   max_competitors: int = 4,
                   use_self_stats: bool = True) -> dict:
    """plan: video_names ごとに matched competitors を計算して返却（dry-run 用）。"""
    import app_channel_thumbnail as _ct
    import app_benchmark_thumbnail as _bt
    bench_cache = _bt.load_cache() or {}

    # Phase B: 自動画の数値を一括取得
    self_stats_map: dict = {}
    if use_self_stats:
        contexts = []
        for vn in video_names:
            c = _ct_collect_video_context(vn)
            if c:
                contexts.append(c)
        vids = [c["video_id"] for c in contexts if c.get("video_id")]
        self_stats_map = _ct_fetch_self_stats(vids)

    plan: list[dict] = []
    for vn in video_names:
        ctx = _ct_collect_video_context(vn)
        if not ctx:
            plan.append({
                "video_name": vn, "ok": False, "error": "video folder not found",
                "matched_competitors": [],
            })
            continue
        matched = _ct.match_competitors_by_keyword(
            self_title=ctx.get("title_extended") or ctx["title"],
            self_concept=ctx["concept"],
            benchmark_cache=bench_cache,
            top_n=max(1, min(int(max_competitors), 8)),
        )
        self_st = self_stats_map.get(ctx["video_id"], {}) if ctx.get("video_id") else {}
        peer_avg = _ct.average_views(matched)
        plan.append({
            "video_name": vn,
            "ok": True,
            "title": ctx["title"],
            "has_title": ctx.get("has_title", False),
            "has_concept": ctx.get("has_concept", False),
            "concept_excerpt": (ctx["concept"] or "")[:200],
            "tags_excerpt": (ctx.get("tags") or "")[:200],
            "video_id": ctx.get("video_id") or "",
            "self_stats": self_st,
            "matched_competitors": matched,
            "matched_count": len(matched),
            "peer_avg_views": int(peer_avg) if peer_avg else 0,
            "fallback_to_aggregate": len(matched) == 0,
        })
    return {"plan": plan, "total": len(plan)}


@app.post("/api/channel-thumbnail/plan")
def api_channel_thumbnail_plan(req: ChannelThumbnailPlanRequest):
    """dry-run: 選択動画ごとの matched_competitors を返却。UI で目視確認用。"""
    if not req.video_names:
        raise HTTPException(400, "video_names が空です")
    # benchmark cache の健全性
    try:
        import app_benchmark_thumbnail as _bt
        bc = _bt.load_cache() or {}
        chs = bc.get("channels") or []
        any_thumb = any((ch.get("thumbnails") or []) for ch in chs)
        if not any_thumb:
            raise HTTPException(409, "benchmark/thumbnail.json が空です。先にベンチマーク取込み + サムネ DL を実行してください。")
    except HTTPException:
        raise
    except Exception:
        pass
    return _ct_build_plan(
        req.video_names,
        max_competitors=req.max_competitors_per_video or 4,
        use_self_stats=bool(req.use_self_stats),
    )


@app.post("/api/channel-thumbnail/start")
async def api_channel_thumbnail_start(req: ChannelThumbnailStartRequest):
    """選択動画群を直列で Vision 分析 → 5要素プロンプト → Codex/Flow 生成。"""
    import app_channel_thumbnail as _ct
    import app_benchmark_thumbnail as _bt
    import app_image_prompt as _ip

    if not req.video_names:
        raise HTTPException(400, "video_names が空です")
    provider = (req.provider or "codex").strip().lower()
    if provider not in ("codex", "flow", "midjourney"):
        raise HTTPException(400, f"未対応 provider: {provider}")

    # provider=midjourney は token 必須
    if provider == "midjourney":
        if not _get_midjourney_token():
            raise HTTPException(400, "Midjourney プロバイダーには AceDataCloud API token が必要です。設定で保存してください。")

    # 多重起動防止
    busy_self = active_tasks.get("channel_thumbnail")
    if busy_self and not getattr(busy_self, "_ct_done", False):
        raise HTTPException(409, "channel_thumbnail バッチが既に実行中です")
    if provider in ("codex", "flow"):
        sub_busy_key = "flow" if provider == "flow" else "codex_imagegen"
        sub_proc = active_tasks.get(sub_busy_key)
        if sub_proc and sub_proc.returncode is None:
            raise HTTPException(409, f"{sub_busy_key} が既に実行中です。先に停止してください。")

    cfg = get_dashboard_config()
    ch_folder = Path(cfg.get("channel_folder", "")).expanduser()
    if not ch_folder.is_dir():
        raise HTTPException(500, f"channel_folder が無効です: {ch_folder}")

    # benchmark 健全性
    bench_cache = _bt.load_cache() or {}
    if not any((ch.get("thumbnails") or []) for ch in (bench_cache.get("channels") or [])):
        raise HTTPException(409, "benchmark/thumbnail.json が空です。先にベンチマーク取込み + サムネ DL を実行してください。")

    # 再実行モード: 直前 run のエラー動画だけに絞る
    targets = list(req.video_names)
    if req.retry_failed_only:
        prev_meta = task_meta.get("channel_thumbnail") or {}
        prev_errs = [e.get("video_name") for e in (prev_meta.get("errors") or [])]
        targets = [v for v in targets if v in prev_errs]
        if not targets:
            raise HTTPException(400, "再実行対象（直前エラー）がありません")

    import datetime as _dt
    task_logs["channel_thumbnail"] = []
    task_meta["channel_thumbnail"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "provider": provider,
        "total": len(targets),
        "done": 0,
        "current": "",
        "errors": [],
        "succeeded": [],
        "stop_requested": False,
        "params": {
            "n_per_video": req.n_per_video,
            "aspect": req.aspect,
            "quality": req.quality,
            "max_competitors": req.max_competitors_per_video,
            "use_self_stats": req.use_self_stats,
        },
    }

    async def _run_channel_thumbnail():
        meta = task_meta["channel_thumbnail"]
        logs = task_logs["channel_thumbnail"]
        # Phase B: 自動画 stats を一括 fetch
        self_stats_map: dict = {}
        if req.use_self_stats:
            try:
                ctxs = []
                for vn in targets:
                    c = _ct_collect_video_context(vn)
                    if c:
                        ctxs.append(c)
                vids = [c["video_id"] for c in ctxs if c.get("video_id")]
                self_stats_map = _ct_fetch_self_stats(vids)
                if self_stats_map:
                    logs.append(f"📊 YouTube Data API で {len(self_stats_map)} 件の自動画 stats を取得")
                elif vids:
                    logs.append(f"⚠ self_stats 取得失敗（YouTube API key 未設定？）— concept のみで継続")
            except Exception as e:
                logs.append(f"⚠ self_stats 取得例外: {e}")

        # Vision プロンプト用の persona / channel_name
        channel_name = (cfg.get("channel_name") or "").strip()
        persona = (cfg.get("persona") or "").strip()
        cli_cmd = (get_suno_config().get("claude_cli") or "claude").strip()

        for idx, vn in enumerate(targets, 1):
            if meta.get("stop_requested"):
                logs.append(f"⏹ 停止要求により中断（{idx-1}/{len(targets)} 完了）")
                break
            meta["current"] = vn
            logs.append(f"\n=== [{idx}/{len(targets)}] {vn} ===")

            ctx = _ct_collect_video_context(vn)
            if not ctx:
                msg = "video folder not found"
                logs.append(f"❌ {msg}")
                meta["errors"].append({"video_name": vn, "error": msg})
                meta["done"] = idx
                continue

            try:
                # 1) 競合マッチング (title_extended = title + tags でトークン語彙拡張)
                matched = _ct.match_competitors_by_keyword(
                    self_title=ctx.get("title_extended") or ctx["title"],
                    self_concept=ctx["concept"],
                    benchmark_cache=bench_cache,
                    top_n=max(1, min(int(req.max_competitors_per_video or 4), 8)),
                )
                logs.append(f"🎯 matched: {len(matched)} 件 " +
                            (", ".join(f"{m['channelName']}:{m['title'][:30]}" for m in matched[:3]) or "(0 件 → aggregate fallback)"))

                # 2) 参照画像 paths
                ref_paths = _ct.matched_local_paths(matched)

                # 3) concept_hint 構築（Phase B 数値統合）
                peer_avg = _ct.average_views(matched)
                self_st = self_stats_map.get(ctx.get("video_id") or "", {})
                concept_hint = _ct.build_per_video_concept_hint(
                    video_title=ctx["title"],
                    video_concept=ctx["concept"],
                    self_stats=self_st,
                    peer_avg_views=peer_avg,
                    concept_hint_override=(req.concept_hint_override or "").strip(),
                )

                # 4) Vision 分析（参照画像があるとき）
                thumbnail_axis: dict = {}
                vision_obj: dict = {}
                if ref_paths:
                    vprompt = _build_5e_vision_prompt(
                        ref_paths, concept_hint,
                        channel_name=channel_name, persona=persona,
                    )
                    try:
                        vraw = _bt._run_claude_vision(cli_cmd, vprompt, ref_paths, timeout=300)
                        vision_obj = _bt._extract_json(vraw) or {}
                        if vision_obj:
                            thumbnail_axis = _vision_to_thumbnail_axis(vision_obj)
                            logs.append(f"🧠 Vision 抽出: subject={(vision_obj.get('subject','') or '')[:70]}")
                            # Phase C: attention_hooks を Viewer resonance として強調する hint を追加
                            hooks = vision_obj.get("attention_hooks") or []
                            if isinstance(hooks, list) and hooks:
                                thumbnail_axis.setdefault("viewer_hooks", hooks)
                        else:
                            logs.append("⚠ Vision JSON 抽出失敗 → aggregate fallback")
                    except Exception as e:
                        logs.append(f"⚠ Vision 失敗 ({type(e).__name__}: {str(e)[:120]}) → aggregate fallback")
                else:
                    logs.append("⚠ 参照画像 0 → benchmark.aggregate を使用")

                # 5) aggregate fallback
                if not thumbnail_axis:
                    agg = (bench_cache.get("analysis") or {}).get("aggregate") or {}
                    if isinstance(agg, dict) and agg:
                        notes = agg.get("gpt_image2_prompt_notes") or {}
                        thumbnail_axis = {
                            "shared_palette": agg.get("shared_palette"),
                            "shared_composition": agg.get("shared_composition"),
                            "shared_atmosphere": agg.get("vibe_one_line"),
                            "recommendation_for_self": agg.get("recommendation_for_self") or {},
                            "element_extraction": {
                                "subjects": [notes.get("subject")] if notes.get("subject") else [],
                                "composition": notes.get("camera_composition"),
                                "lighting": notes.get("lighting"),
                                "color_palette": agg.get("shared_palette"),
                                "atmosphere": notes.get("background_context"),
                            },
                            "avoid": notes.get("avoid"),
                        }

                # 6) start_index 自動算出 + 5要素プロンプト N 件
                image_dir = ch_folder / vn / "Image"
                image_dir.mkdir(parents=True, exist_ok=True)
                start_idx, existing = _ct.scan_start_index(image_dir, vn)
                if existing > 0:
                    logs.append(f"📂 既存 {existing} 枚 → v{start_idx} から生成")

                n_per_video = max(1, min(int(req.n_per_video or 4), 8))
                items = _ip.build_5element_prompts(
                    thumbnail_axis=thumbnail_axis,
                    competitor_analysis={},
                    concept_hint=concept_hint,
                    n=n_per_video,
                    include_text_overlay=bool(req.include_text_overlay),
                    filename_prefix=vn,
                    start_index=start_idx,
                )

                # 7) サブプロセスキック (Codex / Flow / Midjourney REST)
                if provider == "midjourney":
                    # Midjourney は AceDataCloud REST API を直接叩く (subprocess なし)
                    import app_midjourney as _mj
                    mj_token = _get_midjourney_token()
                    mj_aspect = (req.aspect or "16:9").strip()
                    # 5要素プロンプトを (prompt, filename) ペアに変換
                    mj_prompts = [
                        (it["prompt"], it["filename"] + ".png")
                        for it in items
                    ]
                    logs.append(f"🎨 Midjourney 経由で {len(mj_prompts)} 件を生成 (mode=fast, aspect={mj_aspect})")
                    # log_fn でリアルタイムに task_logs へ書き込み
                    mj_results = _mj.imagine_batch(
                        mj_prompts, image_dir,
                        api_token=mj_token,
                        aspect_ratio=mj_aspect,
                        mode="fast",
                        timeout_sec=max(60, int(req.timeout_sec or 900)),
                        log_fn=lambda m: logs.append(f"  {m}"),
                    )
                    mj_ok = sum(1 for r in mj_results if r.get("ok"))
                    logs.append(f"  ✓ Midjourney 完了: 成功 {mj_ok}/{len(mj_results)}")
                    # quota/auth エラーは即座にバッチ全体を中断
                    if any(r.get("error_kind") in ("quota", "auth") for r in mj_results):
                        meta["stop_requested"] = True
                        logs.append("⏹ Midjourney 認証/クォータエラーのため後続をスキップ")
                elif provider == "codex":
                    prompts_text = "\n\n".join(f"{it['prompt']}::{it['filename']}" for it in items)
                    aspect = (req.aspect or "16:9").strip()
                    quality = (req.quality or "medium").strip()
                    background = (req.background or "auto").strip()
                    output_format = (req.output_format or "png").strip()
                    parallel = max(1, min(4, int(req.max_parallel or 4)))
                    cmd = [
                        sys.executable, "-u", str(CODEX_IMAGEGEN_SCRIPT),
                        "--output-dir", str(image_dir),
                        "--max-parallel", str(parallel),
                        "--timeout", str(max(60, int(req.timeout_sec or 900))),
                        "--aspect", aspect,
                        "--quality", quality,
                        "--background", background,
                        "--output-format", output_format,
                    ]
                    for rp in ref_paths[:4]:
                        cmd += ["--reference-image", str(rp)]
                    sub = subprocess.Popen(
                        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True, bufsize=1,
                    )
                    active_tasks["codex_imagegen"] = sub
                    task_meta["codex_imagegen"] = {
                        "started_at": _dt.datetime.now().isoformat(),
                        "video_name": vn,
                        "output_dir": str(image_dir),
                        "prompt_count": len(items),
                        "max_parallel": parallel,
                        "aspect_ratio": aspect,
                        "quality": quality,
                        "n_per_prompt": 1,
                        "background": background,
                        "moderation": "auto",
                        "input_fidelity": "high",
                        "output_format": output_format,
                        "reference_images": [Path(p).name for p in ref_paths[:4]],
                    }
                    task_logs["codex_imagegen"] = []
                    try:
                        sub.stdin.write(prompts_text + "\n")
                        sub.stdin.close()
                    except Exception:
                        pass
                    _stream_subprocess(sub, "codex_imagegen")
                    while sub.returncode is None:
                        if meta.get("stop_requested"):
                            try: sub.terminate()
                            except Exception: pass
                            break
                        await asyncio.sleep(2)
                    logs.append(f"  ✓ Codex rc={sub.returncode}, {len(items)} prompts (v{start_idx}〜v{start_idx+n_per_video-1})")
                else:
                    # Flow: 提案ベースで複数 prompt を直列で投げるのは現実的でないため、
                    # 5要素プロンプト全件を 1 つに連結して 1 Flow セッションで N 枚 (--count) 生成
                    combined_prompt = items[0]["prompt"]   # Flow は 1 prompt のみ受付
                    aspect = (req.aspect or "16:9").strip()
                    cmd = [
                        sys.executable, "-u", str(FLOW_SCRIPT),
                        "--aspect", aspect,
                        "--count", f"x{n_per_video}",
                        "--model", "Nano Banana 2",
                        "--resolution", "2K",
                        "--project-name", f"channelthumb_{vn}",
                        "--prompt", combined_prompt,
                        "--output-dir", str(image_dir),
                        "--no-wait",
                    ]
                    for rp in ref_paths[:1]:   # Flow は参照画像 1 枚まで
                        cmd += ["--reference-image", str(rp)]
                    sub = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, bufsize=1)
                    active_tasks["flow"] = sub
                    task_meta["flow"] = {
                        "started_at": _dt.datetime.now().isoformat(),
                        "mode": "channel_thumbnail",
                        "video_name": vn,
                        "output_dir": str(image_dir),
                        "project_name": f"channelthumb_{vn}",
                    }
                    task_logs["flow"] = []
                    _stream_subprocess(sub, "flow")
                    while sub.returncode is None:
                        if meta.get("stop_requested"):
                            try: sub.terminate()
                            except Exception: pass
                            break
                        await asyncio.sleep(2)
                    logs.append(f"  ✓ Flow rc={sub.returncode}")

                # 8) 生成完了直後の Vision スコアリング + state.json 保存
                import app_thumbnail_state as _tstate
                import app_thumbnail_scoring as _tscore

                # 生成されたファイルを検出（v{start}〜v{end} の範囲、未指定 n_per_prompt は 1）
                # mtime が run 開始後のもの限定（過去ファイルを誤検出しないため）
                run_started_epoch = _dt.datetime.fromisoformat(meta["started_at"]).timestamp()
                generated_files: list[Path] = []
                for i in range(n_per_video):
                    v_num = start_idx + i
                    base = f"{vn[:20].rstrip('-') or 'image'}-v{v_num}"
                    for ext in ("png", "jpg", "jpeg", "webp"):
                        candidates = list(image_dir.glob(f"{base}.{ext}")) + \
                                     list(image_dir.glob(f"{base}-*.{ext}"))
                        for c in candidates:
                            if c.exists() and c not in generated_files:
                                try:
                                    # この run で生成されたものだけ採用
                                    if c.stat().st_mtime >= run_started_epoch - 5:
                                        generated_files.append(c)
                                except Exception:
                                    pass

                # ─── 生成 0 件は失敗扱いに (run はサブプロセス成功でもファイル出来てなければ NG) ───
                if not generated_files:
                    # task_logs["codex_imagegen"] の末尾 8 行をエラー詳細に含める（原因特定の手がかり）
                    sub_logs = (task_logs.get("codex_imagegen") or task_logs.get("flow") or [])
                    tail = "\n".join(sub_logs[-8:]) if sub_logs else ""
                    detail = (
                        f"画像が 1 枚も生成されませんでした。"
                        f"サブプロセスは正常終了しましたが、出力ファイル {vn[:20]}-v{start_idx}.* が見つかりません。"
                    )
                    logs.append(f"❌ {vn}: {detail}")
                    if tail:
                        logs.append(f"    └ サブプロセス末尾ログ:\n{tail}")
                    meta["errors"].append({"video_name": vn, "error": detail, "sub_tail": tail[:600]})
                    _tstate.append_run(image_dir, {
                        "run_id": f"RUN_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{vn}",
                        "started_at": meta["started_at"],
                        "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
                        "generated": [],
                        "approved": [],
                        "errors": [detail],
                        "sub_tail": tail[:600],
                        "provider": provider,
                    }, video_name=vn)
                    meta["done"] = idx
                    continue

                # 状態 JSON に「生成ログ」だけ先に保存（スコア前の状態）
                base_entries = []
                for i, gf in enumerate(generated_files):
                    prompt_text = items[i % len(items)]["prompt"] if items else ""
                    base_entries.append({
                        "filename": gf.name,
                        "generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
                        "prompt": prompt_text[:1500],
                        "status": _tstate.STATUSES[0],   # "pending"
                        "matched_competitors": [
                            {"channelName": m.get("channelName"),
                             "videoId": m.get("videoId"),
                             "match_score": m.get("match_score"),
                             "viewCount": m.get("viewCount")}
                            for m in matched[:4]
                        ],
                    })
                _tstate.upsert_many(image_dir, base_entries, video_name=vn)
                # 承認設定を state に反映
                _tstate.update_settings(
                    image_dir,
                    approval_mode=req.approval_mode,
                    score_threshold=req.score_threshold,
                    ctr_threshold=req.ctr_threshold,
                    similarity_max=req.similarity_max,
                    video_name=vn,
                )

                # Vision スコアリング (auto_score=True のとき)
                eval_entries: list[dict] = []
                if req.auto_score and generated_files:
                    logs.append(f"📊 Vision スコアリング開始 ({len(generated_files)} 枚)…")
                    try:
                        scoring = _tscore.score_thumbnails(
                            generated_paths=generated_files,
                            competitor_paths=ref_paths[:4],
                            video_title=ctx["title"],
                            video_concept=ctx["concept"],
                            channel_name=channel_name,
                            persona=persona,
                            cli_cmd=cli_cmd,
                            timeout=300,
                        )
                        evals_raw = scoring.get("evaluations") or []
                        if scoring.get("error"):
                            logs.append(f"⚠ スコアリング失敗: {scoring['error']}")
                        # 自動承認判定
                        if evals_raw:
                            judged = _tscore.judge_auto_approval(
                                evals_raw,
                                mode=(req.approval_mode or "conditional"),
                                score_threshold=int(req.score_threshold or 80),
                                ctr_threshold=float(req.ctr_threshold or 7.0),
                                similarity_max=int(req.similarity_max or 25),
                            )
                            # filename ベースで base_entries にマージ
                            judged_by_name = {j["filename"]: j for j in judged}
                            for be in base_entries:
                                j = judged_by_name.get(be["filename"])
                                if j:
                                    be.update({
                                        "score_total": j.get("score_total"),
                                        "score_breakdown": j.get("score_breakdown") or {},
                                        "ctr_predict": j.get("ctr_predict"),
                                        "similarity_to_competitors": j.get("similarity"),
                                        "status": j.get("status"),
                                        "approval_reason": j.get("approval_reason"),
                                        "evaluation_comment": j.get("comment", ""),
                                        "strengths": j.get("strengths", []),
                                        "weaknesses": j.get("weaknesses", []),
                                        "evaluated_at": j.get("evaluated_at"),
                                    })
                                    eval_entries.append(be)
                            _tstate.upsert_many(image_dir, base_entries, video_name=vn)
                            counts = _tscore.status_counts(judged)
                            best = _tscore.best_of(judged)
                            logs.append(
                                f"📊 スコア: 自動承認 {counts.get('auto_approved',0)} / "
                                f"要確認 {counts.get('needs_review',0)} / "
                                f"最高 {best.get('score_total',0) if best else 0}/100 ({best.get('filename','') if best else '-'})"
                            )
                    except Exception as e:
                        logs.append(f"⚠ スコアリング例外: {type(e).__name__}: {str(e)[:160]}")

                # run_history 追記
                run_id = f"RUN_{_dt.datetime.now().strftime('%Y%m%d_%H%M%S')}_{vn}"
                _tstate.append_run(image_dir, {
                    "run_id": run_id,
                    "started_at": meta["started_at"],
                    "finished_at": _dt.datetime.now().isoformat(timespec="seconds"),
                    "generated": [p.name for p in generated_files],
                    "approved": [e["filename"] for e in eval_entries if e.get("status") == "auto_approved"],
                    "errors": [],
                    "provider": provider,
                }, video_name=vn)

                meta["succeeded"].append({
                    "video_name": vn,
                    "matched_count": len(matched),
                    "start_index": start_idx,
                    "n_generated": n_per_video,
                    "n_files_detected": len(generated_files),
                    "auto_approved": sum(1 for e in eval_entries if e.get("status") == "auto_approved"),
                    "needs_review": sum(1 for e in eval_entries if e.get("status") == "needs_review"),
                })
            except Exception as e:
                msg = f"{type(e).__name__}: {str(e)[:200]}"
                logs.append(f"❌ {msg}")
                meta["errors"].append({"video_name": vn, "error": msg})

            meta["done"] = idx

        meta["current"] = ""
        logs.append(f"\n=== バッチ完了 (成功 {len(meta['succeeded'])} / 失敗 {len(meta['errors'])}) ===")

    asyncio.create_task(_run_channel_thumbnail())
    # active_tasks に「実行中フラグ」専用のダミーを置く（subprocess.Popen ではないので _ct_done で判定）
    class _CTSentinel:
        _ct_done = False
        returncode = None
    sentinel = _CTSentinel()
    active_tasks["channel_thumbnail"] = sentinel

    # ワーカー完了で sentinel._ct_done を立てる薄いラッパー
    async def _mark_done_when_finished():
        while task_meta["channel_thumbnail"].get("done", 0) < task_meta["channel_thumbnail"].get("total", 0):
            if task_meta["channel_thumbnail"].get("stop_requested"):
                break
            await asyncio.sleep(1)
        sentinel._ct_done = True
    asyncio.create_task(_mark_done_when_finished())

    return {
        "status": "started",
        "total": len(targets),
        "provider": provider,
    }


@app.get("/api/channel-thumbnail/status")
def api_channel_thumbnail_status():
    """直列バッチの進捗を返す。"""
    return {
        "logs": task_logs.get("channel_thumbnail", [])[-300:],
        "meta": task_meta.get("channel_thumbnail", {}),
        "running": not getattr(active_tasks.get("channel_thumbnail"), "_ct_done", True),
    }


# ─── API: Midjourney (AceDataCloud) token 管理 ─────

class MidjourneyTokenRequest(BaseModel):
    token: str


def _get_midjourney_token() -> str:
    """dashboard_config.acedata_api_token を取得。"""
    cfg = get_dashboard_config()
    return (cfg.get("acedata_api_token") or "").strip()


@app.get("/api/midjourney/token-status")
def api_midjourney_token_status():
    """トークンが保存されているか + 末尾のみマスク表示。"""
    tok = _get_midjourney_token()
    if not tok:
        return {"configured": False, "preview": ""}
    return {
        "configured": True,
        "preview": (tok[:4] + "***" + tok[-4:]) if len(tok) >= 12 else "***",
    }


@app.put("/api/midjourney/token")
def api_midjourney_token_save(req: MidjourneyTokenRequest):
    """token を dashboard_config に保存。空文字なら削除。"""
    cfg = get_dashboard_config()
    tok = (req.token or "").strip()
    if tok:
        cfg["acedata_api_token"] = tok
    else:
        cfg.pop("acedata_api_token", None)
    save_dashboard_config_smart(cfg)
    return {"ok": True, "configured": bool(tok)}


@app.post("/api/midjourney/test")
def api_midjourney_test():
    """token の疎通テスト。"""
    import app_midjourney as _mj
    tok = _get_midjourney_token()
    if not tok:
        return {"ok": False, "error": "token が未設定"}
    return _mj.test_token(tok)


@app.get("/api/channel-thumbnail/readiness")
def api_channel_thumbnail_readiness():
    """ワークフロー実行に必要なベンチマーク・分析データの準備状況を返す。

    返す内容:
      - benchmark_thumbnail: thumbnail.json が存在し channels[] が空でないか
      - benchmark_concept: concept.json が存在し analysis があるか
      - competitor_analysis: competitor_analysis_cache.json が存在するか
      - persona_set: dashboard_config.persona が設定済みか
      - youtube_api_key_set: YouTube Data API key (Phase B 用)
    """
    out: dict = {
        "benchmark_thumbnail": False, "benchmark_thumbnail_channels": 0,
        "benchmark_thumbnail_picked": 0,
        "benchmark_concept": False, "benchmark_concept_channels": 0,
        "competitor_analysis": False,
        "persona_set": False,
        "youtube_api_key_set": False,
        "claude_cli_set": False,
        "next_action": "",
        "ready_for_plan": False,
    }
    try:
        import app_benchmark_thumbnail as _bt
        bc = _bt.load_cache() or {}
        chs = bc.get("channels") or []
        any_thumb = any((ch.get("thumbnails") or []) for ch in chs)
        out["benchmark_thumbnail"] = bool(any_thumb)
        out["benchmark_thumbnail_channels"] = len([c for c in chs if c.get("thumbnails")])
        out["benchmark_thumbnail_picked"] = len(bc.get("picked") or [])
    except Exception:
        pass
    try:
        import app_benchmark_concept as _bc
        d = _bc.load_cache() or {}
        per = d.get("per_channel") or {}
        out["benchmark_concept"] = bool(per)
        out["benchmark_concept_channels"] = len(per)
    except Exception:
        pass
    try:
        import app_competitor as _comp_cache
        out["competitor_analysis"] = bool(_comp_cache.load_cache())
    except Exception:
        pass

    cfg = get_dashboard_config()
    out["persona_set"] = bool((cfg.get("persona") or "").strip())
    suno_cfg = get_suno_config()
    out["claude_cli_set"] = bool((suno_cfg.get("claude_cli") or "claude").strip())

    try:
        import app_competitor as _comp
        out["youtube_api_key_set"] = bool(_comp._resolve_youtube_api_key())
    except Exception:
        pass

    # 次に何をすべきかの推奨アクション
    if not out["benchmark_thumbnail"]:
        out["next_action"] = "ベンチマーク・サムネ取込み + Vision 分析 を実行してください (画面: ベンチマーク → 取り込み)"
    elif not out["competitor_analysis"]:
        out["next_action"] = "競合分析 (Claude による buzz_patterns 抽出) を実行してください"
    else:
        out["ready_for_plan"] = True
        out["next_action"] = "対象動画を選択して「生成プランを作成」"
    return out


@app.post("/api/channel-thumbnail/stop")
def api_channel_thumbnail_stop():
    """実行中バッチに停止要求 + サブプロセス terminate。"""
    meta = task_meta.get("channel_thumbnail")
    if not meta or meta.get("done", 0) >= meta.get("total", 0):
        return {"status": "not_running"}
    meta["stop_requested"] = True
    # 現在動いているサブプロセスを止める
    for key in ("codex_imagegen", "flow"):
        sub = active_tasks.get(key)
        if sub and sub.returncode is None:
            try:
                sub.terminate()
            except Exception:
                pass
    return {"status": "stop_requested"}


# ─── API: サムネ承認状態 (thumbnail_state.json) ─────

def _resolve_image_dir(video_name: str) -> Optional[Path]:
    if not video_name:
        return None
    cfg = get_dashboard_config()
    ch = Path(cfg.get("channel_folder", "")).expanduser()
    if not ch.is_dir():
        return None
    d = ch / video_name / "Image"
    if not d.is_dir():
        # フォルダ未生成の場合は作る
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
    return d


@app.get("/api/thumbnail-state/{video_name}")
def api_thumbnail_state_get(video_name: str):
    """指定動画の thumbnail_state.json を返す。"""
    import app_thumbnail_state as _tstate
    image_dir = _resolve_image_dir(video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {video_name}")
    _tstate.cleanup_missing_files(image_dir)
    state = _tstate.load_state(image_dir, video_name=video_name)
    summary = _tstate.aggregate_summary(image_dir)
    return {"video_name": video_name, "state": state, "summary": summary}


@app.post("/api/thumbnail-state/approve")
def api_thumbnail_state_approve(req: ThumbnailApproveRequest):
    """1 件のサムネを採用 / 却下 / 要確認 に変更。"""
    import app_thumbnail_state as _tstate
    if req.status not in _tstate.STATUSES:
        raise HTTPException(400, f"未対応 status: {req.status}")
    image_dir = _resolve_image_dir(req.video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {req.video_name}")
    state = _tstate.set_status(image_dir, req.filename, req.status,
                                reason=req.reason or "",
                                video_name=req.video_name)
    return {"ok": True, "state": state.get("thumbnails", {}).get(req.filename, {})}


@app.post("/api/thumbnail-state/settings")
def api_thumbnail_state_settings(req: ThumbnailSettingsRequest):
    """承認設定（mode / 閾値）を保存。"""
    import app_thumbnail_state as _tstate
    image_dir = _resolve_image_dir(req.video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {req.video_name}")
    state = _tstate.update_settings(
        image_dir,
        approval_mode=req.approval_mode,
        score_threshold=req.score_threshold,
        ctr_threshold=req.ctr_threshold,
        similarity_max=req.similarity_max,
        video_name=req.video_name,
    )
    return {"ok": True, "state": {k: state.get(k) for k in
            ("approval_mode", "score_threshold", "ctr_threshold", "similarity_max")}}


@app.post("/api/thumbnail-state/rescore")
async def api_thumbnail_state_rescore(req: ThumbnailRescoreRequest):
    """既存生成済みサムネを再スコアリング（閾値変更後の再判定など）。"""
    import app_thumbnail_state as _tstate
    import app_thumbnail_scoring as _tscore
    import app_channel_thumbnail as _ct
    image_dir = _resolve_image_dir(req.video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {req.video_name}")

    state = _tstate.load_state(image_dir, video_name=req.video_name)
    all_thumbs = state.get("thumbnails", {}) or {}
    # 対象ファイル
    targets = (req.filenames or list(all_thumbs.keys()))
    targets = [t for t in targets if (image_dir / t).exists()]
    if not targets:
        raise HTTPException(400, "対象サムネがありません")

    # context / competitor 再構築
    ctx = _ct.read_video_context(image_dir.parent) or {}
    import app_benchmark_thumbnail as _bt
    bench_cache = _bt.load_cache() or {}
    matched = _ct.match_competitors_by_keyword(
        self_title=ctx.get("title", ""),
        self_concept=ctx.get("concept", ""),
        benchmark_cache=bench_cache,
        top_n=4,
    )
    ref_paths = _ct.matched_local_paths(matched)

    # チャンネル文脈
    cfg = get_dashboard_config()
    channel_name = (cfg.get("channel_name") or "").strip()
    persona = (cfg.get("persona") or "").strip()
    cli_cmd = (get_suno_config().get("claude_cli") or "claude").strip()

    # 設定を保存 (引数で上書きされていれば反映)
    _tstate.update_settings(
        image_dir,
        approval_mode=req.approval_mode,
        score_threshold=req.score_threshold,
        ctr_threshold=req.ctr_threshold,
        similarity_max=req.similarity_max,
        video_name=req.video_name,
    )
    state_now = _tstate.load_state(image_dir, video_name=req.video_name)

    # Vision 再スコアリング
    scoring = _tscore.score_thumbnails(
        generated_paths=[image_dir / t for t in targets],
        competitor_paths=ref_paths[:4],
        video_title=ctx.get("title", ""),
        video_concept=ctx.get("concept", ""),
        channel_name=channel_name,
        persona=persona,
        cli_cmd=cli_cmd,
        timeout=300,
    )
    if scoring.get("error"):
        return {"ok": False, "error": scoring["error"]}

    judged = _tscore.judge_auto_approval(
        scoring.get("evaluations") or [],
        mode=state_now.get("approval_mode") or "conditional",
        score_threshold=int(state_now.get("score_threshold") or 80),
        ctr_threshold=float(state_now.get("ctr_threshold") or 7.0),
        similarity_max=int(state_now.get("similarity_max") or 25),
    )

    # state 反映 (adopted は保持)
    new_entries = []
    for j in judged:
        fn = j["filename"]
        cur = all_thumbs.get(fn, {}) or {}
        # 既に手動で adopted されていたら status は変えない
        new_status = cur.get("status") if cur.get("status") in ("adopted", "rejected") else j.get("status")
        new_entries.append({
            "filename": fn,
            "score_total": j.get("score_total"),
            "score_breakdown": j.get("score_breakdown") or {},
            "ctr_predict": j.get("ctr_predict"),
            "similarity_to_competitors": j.get("similarity"),
            "evaluation_comment": j.get("comment", ""),
            "strengths": j.get("strengths", []),
            "weaknesses": j.get("weaknesses", []),
            "evaluated_at": j.get("evaluated_at"),
            "approval_reason": j.get("approval_reason"),
            "status": new_status,
        })
    _tstate.upsert_many(image_dir, new_entries, video_name=req.video_name)
    return {
        "ok": True,
        "rescored": len(new_entries),
        "evaluations": new_entries,
        "summary": _tstate.aggregate_summary(image_dir),
    }


@app.get("/api/thumbnail-state/export-csv/{video_name}")
def api_thumbnail_state_export_csv(video_name: str):
    """state を CSV で出力（採用・候補・スコア一覧）。"""
    import app_thumbnail_state as _tstate
    import csv
    import io
    image_dir = _resolve_image_dir(video_name)
    if not image_dir:
        raise HTTPException(404, f"video folder not found: {video_name}")
    state = _tstate.load_state(image_dir, video_name=video_name)
    rows = []
    for fn, t in (state.get("thumbnails") or {}).items():
        sb = t.get("score_breakdown") or {}
        rows.append({
            "filename": fn,
            "status": t.get("status", ""),
            "score_total": t.get("score_total", ""),
            "concept_fit": sb.get("concept_fit", ""),
            "trend_fit": sb.get("trend_fit", ""),
            "competitor_diff": sb.get("competitor_diff", ""),
            "past_perf": sb.get("past_perf", ""),
            "ctr_predict": t.get("ctr_predict", ""),
            "similarity": t.get("similarity_to_competitors", ""),
            "evaluation_comment": t.get("evaluation_comment", ""),
            "approval_reason": t.get("approval_reason", ""),
            "evaluated_at": t.get("evaluated_at", ""),
            "generated_at": t.get("generated_at", ""),
        })
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    csv_text = buf.getvalue()
    from fastapi.responses import Response
    safe = re.sub(r"[^\w\-.]+", "_", video_name)[:60]
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="thumbnail_state_{safe}.csv"'},
    )


# ─── API: シリーズ画像案（ベンチマーク分析駆動） ─────

class SeriesProposeRequest(BaseModel):
    count: Optional[int] = 8


class SeriesGenerateRequest(BaseModel):
    ids: List[str]                          # 提案 id（filename_slug）の配列
    provider: str = "flow"                  # "flow" | "codex"
    count_per_proposal: Optional[int] = 4   # Flow x4 / Codex は本数の目安
    aspect: Optional[str] = "16:9"
    headless: Optional[bool] = False        # Flow 用
    timeout_sec: Optional[int] = 900        # Codex 用
    max_parallel: Optional[int] = 4         # Codex 用
    use_benchmark_picked: Optional[bool] = False  # Flow 用: picked された競合サムネを参照画像として渡す


@app.post("/api/series/propose")
def api_series_propose(req: SeriesProposeRequest):
    """ベンチマーク分析 + 既存動画一覧 → Claude CLI で次に作るべき画像案 N 件を生成 → キャッシュ。"""
    import app_series as _ser

    config = get_dashboard_config()
    suno_cfg = get_suno_config()
    cli_cmd = suno_cfg.get("claude_cli") or "claude"

    # 既存動画一覧（重複回避用）
    existing: list[dict] = []
    try:
        ch_folder = Path(config.get("channel_folder", "")).expanduser()
        if ch_folder.is_dir():
            for f in sorted(ch_folder.iterdir()):
                if not f.is_dir():
                    continue
                if not re.match(r"^\d+_", f.name):
                    continue
                title = ""
                tf = f / "youtube_title.txt"
                if tf.exists():
                    try:
                        title = tf.read_text(encoding="utf-8").strip()
                    except Exception:
                        pass
                existing.append({"name": f.name, "title": title})
    except Exception:
        existing = []

    try:
        result = _ser.propose_series(
            cli_cmd=cli_cmd,
            count=max(3, min(int(req.count or 8), 16)),
            persona=config.get("persona", ""),
            channel_name=config.get("channel_name", ""),
            existing_videos=existing,
        )
    except RuntimeError as e:
        raise HTTPException(500, str(e))

    import datetime as _dt
    payload = {
        "proposals": result.get("proposals", []),
        "generated_at": _dt.datetime.now().isoformat(),
        "channel_name": config.get("channel_name", ""),
        "based_on": "competitor_analysis_cache",
    }
    _ser.save_proposals_cache(payload)
    return {"status": "ok", **payload}


@app.get("/api/series/proposals")
def api_series_proposals():
    """キャッシュ済みのシリーズ画像案を返す。"""
    import app_series as _ser
    return _ser.load_proposals_cache()


@app.delete("/api/series/proposals")
def api_series_proposals_clear():
    """キャッシュをクリア。"""
    import app_series as _ser
    _ser.save_proposals_cache({"proposals": [], "generated_at": "", "channel_name": ""})
    return {"status": "ok"}


@app.delete("/api/series/proposals/{proposal_id}")
def api_series_proposal_delete(proposal_id: str):
    """提案 1 件を削除。"""
    import app_series as _ser
    cache = _ser.load_proposals_cache()
    cache["proposals"] = [p for p in cache.get("proposals", []) if p.get("id") != proposal_id]
    _ser.save_proposals_cache(cache)
    return {"status": "ok", "remaining": len(cache["proposals"])}


@app.post("/api/series/generate")
async def api_series_generate(req: SeriesGenerateRequest):
    """選択された提案を Flow または Codex で順次生成。

    保存先: <channel_folder>/_series_drafts/{slug}/Image/
    Flow は同時実行不可なので 1 件ずつ起動 → 完了待ち → 次へ、と直列で回す。
    Codex は内部で並列なので 1 提案ずつ叩けば十分。
    """
    import app_series as _ser

    if not req.ids:
        raise HTTPException(400, "ids が空です")
    provider = (req.provider or "flow").strip().lower()
    if provider not in ("flow", "codex"):
        raise HTTPException(400, f"未対応 provider: {provider}")

    cache = _ser.load_proposals_cache()
    proposals_by_id = {p.get("id"): p for p in cache.get("proposals", [])}
    targets = [proposals_by_id[i] for i in req.ids if i in proposals_by_id]
    if not targets:
        raise HTTPException(404, "指定 id の提案が見つかりません")

    config = get_dashboard_config()
    ch_folder = Path(config.get("channel_folder", "")).expanduser()
    if not ch_folder.is_dir():
        raise HTTPException(500, f"channel_folder が無効です: {ch_folder}")

    # Flow / Codex で既に動いていたら拒否
    busy_key = "flow" if provider == "flow" else "codex_imagegen"
    proc = active_tasks.get(busy_key)
    if proc and proc.returncode is None:
        raise HTTPException(409, f"{busy_key} が既に実行中です")

    # バックグラウンドで直列実行
    task_logs["series_generate"] = []
    import datetime as _dt
    task_meta["series_generate"] = {
        "started_at": _dt.datetime.now().isoformat(),
        "provider": provider,
        "total": len(targets),
        "done": 0,
        "current": "",
        "errors": [],
    }

    async def _run_series():
        meta = task_meta["series_generate"]
        for idx, p in enumerate(targets, 1):
            slug = p.get("filename_slug") or p.get("id") or f"scene_{idx}"
            out_dir = _ser.staging_dir(ch_folder, slug)
            out_dir.mkdir(parents=True, exist_ok=True)
            meta["current"] = slug
            task_logs["series_generate"].append(f"[{idx}/{len(targets)}] {slug} → {out_dir}")

            prompt_en = p.get("image_prompt_en", "").strip()
            if not prompt_en:
                msg = f"⚠️ {slug}: image_prompt_en が空、スキップ"
                task_logs["series_generate"].append(msg)
                meta["errors"].append({"slug": slug, "error": "empty prompt"})
                continue

            try:
                if provider == "flow":
                    cmd = [sys.executable, "-u", str(FLOW_SCRIPT),
                           "--aspect", req.aspect or "16:9",
                           "--count", f"x{int(req.count_per_proposal or 4)}",
                           "--model", "Nano Banana 2",
                           "--resolution", "2K",
                           "--project-name", f"series_{slug}",
                           "--prompt", prompt_en,
                           "--output-dir", str(out_dir),
                           "--no-wait"]
                    if req.headless:
                        cmd.append("--headless")
                    # Picked benchmark サムネを参照画像として注入（先頭 1 枚）
                    if req.use_benchmark_picked:
                        try:
                            import app_benchmark_thumbnail as _bt
                            picked_paths = _bt.get_picked_paths(limit=1)
                            if picked_paths:
                                cmd += ["--reference-image", picked_paths[0]]
                                task_logs["series_generate"].append(
                                    f"  📎 reference: {Path(picked_paths[0]).name}"
                                )
                            else:
                                task_logs["series_generate"].append(
                                    "  ⚠ picked サムネ無し → 参照画像なしで生成"
                                )
                        except Exception as e:
                            task_logs["series_generate"].append(f"  ⚠ picked 参照失敗: {e}")
                    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                            text=True, bufsize=1)
                    active_tasks["flow"] = proc
                    task_meta["flow"] = {
                        "started_at": _dt.datetime.now().isoformat(),
                        "mode": "series",
                        "video_name": "",
                        "output_dir": str(out_dir),
                        "project_name": f"series_{slug}",
                    }
                    # Flow ログを task_logs["flow"] に流しつつ、終了まで待つ（直列）
                    task_logs["flow"] = []
                    _stream_subprocess(proc, "flow")
                    while proc.returncode is None:
                        await asyncio.sleep(2)
                    task_logs["series_generate"].append(f"  ✓ Flow rc={proc.returncode}")
                else:
                    # Codex: 1 行 1 件、ファイル名サジェスト付き
                    fname_seed = slug
                    prompt_line = f"{prompt_en}::{fname_seed}"
                    aspect = (req.aspect or "16:9")
                    aspect_prefix = f"Aspect ratio {aspect} (web-optimized). "
                    prompts_text = f"{aspect_prefix}{prompt_line}\n"
                    parallel = max(1, min(4, int(req.max_parallel or 4)))
                    cmd = [sys.executable, "-u", str(CODEX_IMAGEGEN_SCRIPT),
                           "--output-dir", str(out_dir),
                           "--max-parallel", str(parallel),
                           "--timeout", str(max(60, int(req.timeout_sec or 900)))]
                    if req.use_benchmark_picked:
                        try:
                            import app_benchmark_thumbnail as _bt
                            picked_paths = _bt.get_picked_paths(limit=1)
                            if picked_paths:
                                cmd += ["--reference-image", picked_paths[0]]
                                task_logs["series_generate"].append(
                                    f"  📎 image2 reference(API): {Path(picked_paths[0]).name}"
                                )
                            else:
                                task_logs["series_generate"].append(
                                    "  ⚠ picked サムネ無し → Image2 参照画像なしで生成"
                                )
                        except Exception as e:
                            task_logs["series_generate"].append(f"  ⚠ image2 picked 参照失敗: {e}")
                    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, text=True, bufsize=1)
                    active_tasks["codex_imagegen"] = proc
                    task_meta["codex_imagegen"] = {
                        "started_at": _dt.datetime.now().isoformat(),
                        "video_name": "",
                        "output_dir": str(out_dir),
                        "prompt_count": 1,
                        "max_parallel": parallel,
                        "aspect_ratio": aspect,
                    }
                    task_logs["codex_imagegen"] = []
                    try:
                        proc.stdin.write(prompts_text)
                        proc.stdin.close()
                    except Exception:
                        pass
                    _stream_subprocess(proc, "codex_imagegen")
                    while proc.returncode is None:
                        await asyncio.sleep(2)
                    task_logs["series_generate"].append(f"  ✓ Codex rc={proc.returncode}")

                # キャッシュ更新（generated フラグ）
                cache_now = _ser.load_proposals_cache()
                for cp in cache_now.get("proposals", []):
                    if cp.get("id") == p.get("id"):
                        cp["generated"] = True
                        cp["output_dir"] = str(out_dir)
                _ser.save_proposals_cache(cache_now)

            except Exception as e:
                msg = f"❌ {slug}: {e}"
                task_logs["series_generate"].append(msg)
                meta["errors"].append({"slug": slug, "error": str(e)})

            meta["done"] = idx

        meta["current"] = ""
        task_logs["series_generate"].append(f"=== 完了 (成功 {meta['done']-len(meta['errors'])} / 失敗 {len(meta['errors'])}) ===")

    asyncio.create_task(_run_series())
    return {"status": "started", "total": len(targets), "provider": provider}


@app.get("/api/series/status")
def api_series_status():
    """直列バッチの進捗を返す。"""
    return {
        "logs": task_logs.get("series_generate", [])[-200:],
        "meta": task_meta.get("series_generate", {}),
    }


# ─── API: Premiere Pro ───

PREMIERE_SCRIPT = SHARED_BASE / "Python" / "app_premiere.py"

class PremiereRunRequest(BaseModel):
    duration: Optional[int] = None
    duration_h: Optional[int] = None
    duration_m: Optional[int] = None
    duration_s: Optional[int] = None
    auto_export: bool = False
    folder: Optional[str] = None   # 動画フォルダのパス（.prproj を自動オープン）
    video_name: Optional[str] = None  # または動画フォルダ名のみ

@app.post("/api/premiere/run")
async def api_premiere_run(req: PremiereRunRequest):
    await _ensure_not_running("premiere", "Premiere 処理が既に実行中です")
    # duration 優先順位: duration_h/m/s explicit > duration explicit > per-channel default_duration_sec > 10800
    if req.duration_h is not None or req.duration_m is not None or req.duration_s is not None:
        duration = (req.duration_h or 0) * 3600 + (req.duration_m or 0) * 60 + (req.duration_s or 0)
    elif req.duration is not None:
        duration = req.duration
    else:
        _cfg = get_dashboard_config()
        _ch_default = _cfg.get("default_duration_sec")
        duration = int(_ch_default) if isinstance(_ch_default, (int, float)) and _ch_default > 0 else 10800
    cmd = [sys.executable, str(PREMIERE_SCRIPT), "--duration", str(duration)]

    # 動画フォルダ指定がある場合は .prproj を自動で開く
    folder_path = None
    if req.folder:
        folder_path = Path(req.folder)
    elif req.video_name:
        config = get_dashboard_config()
        folder_path = Path(config["channel_folder"]) / req.video_name
    if folder_path:
        if not folder_path.exists():
            raise HTTPException(404, f"動画フォルダが存在しません: {folder_path}")
        prproj = next(iter(folder_path.glob("*vol*.prproj")), None)
        if not prproj:
            raise HTTPException(404, f".prproj が見つかりません: {folder_path}")
        cmd += ["--project", str(prproj)]

    if req.auto_export:
        cmd.append("--export")
    task_logs["premiere"] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["premiere"] = proc
    _stream_subprocess(proc, "premiere")
    return {"status": "started", "duration": duration}

@app.post("/api/premiere/export")
async def api_premiere_export():
    """書き出しのみ実行"""
    await _ensure_not_running("premiere", "Premiere 処理が既に実行中です")
    cmd = [sys.executable, str(PREMIERE_SCRIPT), "--export-only"]
    task_logs["premiere"] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["premiere"] = proc
    _stream_subprocess(proc, "premiere")
    return {"status": "started"}

@app.post("/api/premiere/regenerate-srt")
async def api_premiere_regenerate_srt():
    cmd = [sys.executable, str(PREMIERE_SCRIPT), "--regenerate-srt"]
    task_logs["premiere"] = []
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    active_tasks["premiere"] = proc
    _stream_subprocess(proc, "premiere")
    return {"status": "started"}

@app.get("/api/premiere/status")
def api_premiere_status():
    proc = active_tasks.get("premiere")
    running = proc is not None and proc.returncode is None
    return {"running": running, "logs": task_logs.get("premiere", [])[-50:]}

@app.get("/api/premiere/check")
def api_premiere_check():
    """Premiere Pro 接続確認 (pymiere → AppleScript フォールバック)"""
    import subprocess as _sp
    # 1. pymiere 試行
    try:
        from pymiere import exe_utils
        is_running, pid = exe_utils.is_premiere_running()
        if not is_running:
            return {"connected": False, "reason": "Premiere Pro が起動していません"}
        from pymiere.core import eval_script
        name = eval_script('app.project.name')
        return {"connected": True, "project": name, "pid": pid, "method": "pymiere"}
    except Exception:
        pass
    # 2. ファイルポーリング方式（Premiere Link CEP パネル経由）
    proc = _sp.run(['pgrep', '-fi', 'Adobe Premiere Pro'], capture_output=True, text=True)
    if proc.returncode != 0:
        return {"connected": False, "reason": "Premiere Pro が起動していません"}
    import os as _os, time as _time, json as _json
    PING = '/tmp/pymiere_ping.txt'
    TRIGGER = '/tmp/pymiere_trigger.json'
    RESULT  = '/tmp/pymiere_result.json'
    # パネル生存確認
    if not _os.path.exists(PING) or (_time.time() - _os.path.getmtime(PING)) > 30:
        return {"connected": False, "reason": "Premiere Link が未応答です。「パネルを開く」または「Premiere を再起動」を試してください。"}
    # JSX 実行でプロジェクト名取得
    try:
        if _os.path.exists(RESULT): _os.unlink(RESULT)
        with open(TRIGGER, 'w') as f:
            _json.dump({'code': 'app.project.name || "no_project"', 'ts': _time.time()}, f)
        for _ in range(50):
            _time.sleep(0.2)
            if _os.path.exists(RESULT): break
        if _os.path.exists(RESULT):
            with open(RESULT, 'r', encoding='utf-8') as rf:
                d = _json.load(rf)
            _os.unlink(RESULT)
            name = d.get('result', 'unknown')
        else:
            name = 'unknown'
        return {"connected": True, "project": name, "method": "file_poll"}
    except Exception as e2:
        return {"connected": False, "reason": f"ファイルポーリング エラー: {e2}"}

@app.get("/api/premiere/panel-status")
def api_premiere_panel_status():
    """Premiere Link パネルの生存状態とアクティビティログを返す"""
    import os as _os, time as _t, json as _j
    PING = '/tmp/pymiere_ping.txt'
    ACTIVITY = '/tmp/pymiere_activity.json'
    alive = False
    ping_age = None
    if _os.path.exists(PING):
        ping_age = round(_t.time() - _os.path.getmtime(PING), 1)
        alive = ping_age < 30
    logs = []
    try:
        if _os.path.exists(ACTIVITY):
            with open(ACTIVITY, 'r', encoding='utf-8') as af:
                logs = _j.load(af)
    except Exception:
        pass
    return {"alive": alive, "ping_age": ping_age, "logs": logs[:15]}


@app.post("/api/premiere/reopen-panel")
def api_premiere_reopen_panel():
    """「ウィンドウ > 拡張機能 > Premiere Link」を AppleScript で自動クリックして
    パネルを開き直す。アクセシビリティ権限が必要。"""
    import sys as _sys, importlib as _il
    _sys.path.insert(0, str(SHARED_BASE / "Python"))
    try:
        _premiere_mod = _il.import_module("app_premiere")
    except Exception as e:
        raise HTTPException(500, f"app_premiere モジュール読み込み失敗: {e}")
    ok = False
    try:
        ok = bool(_premiere_mod.open_pymiere_panel())
    except Exception as e:
        raise HTTPException(500, f"パネル起動処理エラー: {e}")
    return {"ok": ok}


@app.post("/api/premiere/restart")
def api_premiere_restart():
    """Premiere Pro を安全に終了 → 再起動する。
    起動中のプロジェクトは保存ダイアログが出るため、あらかじめ保存済みであることを
    呼び出し側で確認すること。"""
    import subprocess as _sp, time as _time, os as _os
    # 実行中アプリを特定
    try:
        r = _sp.run(['osascript', '-e',
                     'tell application "System Events" to get name of every process'],
                    capture_output=True, text=True, timeout=10)
    except Exception as e:
        raise HTTPException(500, f"プロセス取得失敗: {e}")
    app_name = None
    for part in r.stdout.split(','):
        name = part.strip()
        if 'Adobe Premiere Pro' in name:
            app_name = name
            break
    if not app_name:
        return {"ok": False, "reason": "Premiere Pro が起動していません"}
    # 強制終了（保存確認なし: ハング復旧用）
    try:
        _sp.run(['pkill', '-9', '-f', 'Adobe Premiere Pro'], capture_output=True, timeout=10)
    except Exception as e:
        raise HTTPException(500, f"終了失敗: {e}")
    # 停止ファイルをクリーンアップ
    for p in ('/tmp/pymiere_trigger.json', '/tmp/pymiere_result.json', '/tmp/pymiere_ping.txt'):
        try:
            if _os.path.exists(p):
                _os.unlink(p)
        except Exception:
            pass
    # 再起動
    _time.sleep(2)
    app_path = f"/Applications/{app_name}/{app_name}.app"
    if not _os.path.exists(app_path):
        # バージョン表記違いに対応: /Applications 下から検索
        for entry in _os.listdir("/Applications"):
            if entry.startswith("Adobe Premiere Pro"):
                app_path = f"/Applications/{entry}/{entry}.app"
                if _os.path.exists(app_path):
                    break
    try:
        _sp.Popen(['open', app_path])
    except Exception as e:
        raise HTTPException(500, f"再起動失敗: {e}")
    return {"ok": True, "app": app_name, "app_path": app_path}


# ─── API: Photoshop Link (UXP) ─────────────────────────────────────────────

class PhotoshopOpenRequest(BaseModel):
    path: str

class PhotoshopSetTextRequest(BaseModel):
    layer: str
    text: str

class PhotoshopExportRequest(BaseModel):
    out_path: str
    fmt: str = "jpg"
    quality: int = 90

class PhotoshopEvalRequest(BaseModel):
    code: str
    timeout: float = 30.0

class PhotoshopReplaceSmartObjectRequest(BaseModel):
    layer: str
    image_path: str

class PhotoshopSetVisibleRequest(BaseModel):
    layer: str
    visible: bool

class PhotoshopRenderThumbsRequest(BaseModel):
    psd_path: str
    base_image: Optional[str] = None
    base_layer: str = "base"
    text_replacements: Optional[dict] = None
    toggle_layer: str = "PLAY LIST"
    out_dir: Optional[str] = None
    vol_name: Optional[str] = None
    quality: int = 90

class PhotoshopRenderForVideoRequest(BaseModel):
    """動画フォルダ（または video_name）を指定するだけで、
    チャンネル設定からレイヤー名を引いてサムネを 2 枚書き出す。"""
    video_folder: Optional[str] = None
    video_name: Optional[str] = None    # video_folder の代わりにこちらでも可
    text_replacements: Optional[dict] = None
    quality: int = 90
    # チャンネル設定の上書き（指定があれば優先）
    base_layer: Optional[str] = None
    toggle_layer: Optional[str] = None
    image_subdir: Optional[str] = None


def _import_photoshop():
    import importlib as _il
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    return _il.import_module("app_photoshop")


@app.get("/api/photoshop/check")
def api_photoshop_check():
    """Photoshop Link UXP パネルの接続状態を返す。"""
    try:
        ps = _import_photoshop()
        return ps.check_photoshop()
    except Exception as e:
        return {"connected": False, "reason": f"app_photoshop 読み込み失敗: {e}"}


@app.get("/api/photoshop/panel-status")
def api_photoshop_panel_status():
    """Photoshop プロセス状態を返す（AppleScript 経由なのでパネル不要）。"""
    import subprocess as _sp
    running = _sp.run(["pgrep", "-fi", "Adobe Photoshop"], capture_output=True).returncode == 0
    return {"alive": running, "method": "applescript_do_javascript"}


@app.post("/api/photoshop/open")
def api_photoshop_open(req: PhotoshopOpenRequest):
    try:
        ps = _import_photoshop()
        return {"ok": True, "active_document": ps.open_psd(req.path)}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"open 失敗: {e}")


@app.get("/api/photoshop/layers")
def api_photoshop_layers():
    try:
        ps = _import_photoshop()
        return {"layers": ps.list_layers()}
    except Exception as e:
        raise HTTPException(500, f"layers 取得失敗: {e}")


@app.post("/api/photoshop/set-text")
def api_photoshop_set_text(req: PhotoshopSetTextRequest):
    try:
        ps = _import_photoshop()
        ps.set_text(req.layer, req.text)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"set-text 失敗: {e}")


@app.post("/api/photoshop/export")
def api_photoshop_export(req: PhotoshopExportRequest):
    try:
        ps = _import_photoshop()
        out = ps.export_image(req.out_path, req.fmt, req.quality)
        return {"ok": True, "path": out}
    except Exception as e:
        raise HTTPException(500, f"export 失敗: {e}")


@app.post("/api/photoshop/replace-so")
def api_photoshop_replace_so(req: PhotoshopReplaceSmartObjectRequest):
    """指定レイヤー（スマートオブジェクト）の中身を画像で差し替え。"""
    try:
        ps = _import_photoshop()
        ps.replace_smart_object(req.layer, req.image_path)
        return {"ok": True}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"replace-so 失敗: {e}")


@app.post("/api/photoshop/set-visible")
def api_photoshop_set_visible(req: PhotoshopSetVisibleRequest):
    """レイヤーの表示/非表示を切り替え。"""
    try:
        ps = _import_photoshop()
        ps.set_layer_visible(req.layer, req.visible)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"set-visible 失敗: {e}")


@app.post("/api/photoshop/render-thumbs")
def api_photoshop_render_thumbs(req: PhotoshopRenderThumbsRequest):
    """サムネ 2 枚セット（PLAY LIST 表示版・非表示版）を書き出し。"""
    try:
        ps = _import_photoshop()
        return ps.render_thumbnail_set(
            req.psd_path,
            base_image=req.base_image,
            base_layer=req.base_layer,
            text_replacements=req.text_replacements,
            toggle_layer=req.toggle_layer,
            out_dir=req.out_dir,
            vol_name=req.vol_name,
            quality=req.quality,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"render-thumbs 失敗: {e}")


@app.post("/api/photoshop/render-for-video")
def api_photoshop_render_for_video(req: PhotoshopRenderForVideoRequest):
    """動画フォルダのみ指定でサムネ 2 枚生成。レイヤー名はチャンネル設定から取得。"""
    config = get_dashboard_config()
    folder_path = req.video_folder
    if not folder_path and req.video_name:
        folder_path = str(Path(config["channel_folder"]) / req.video_name)
    if not folder_path:
        raise HTTPException(400, "video_folder か video_name のいずれかが必要")
    try:
        ps = _import_photoshop()
        return ps.render_thumbnails_for_video(
            folder_path,
            base_layer=req.base_layer or config.get("psd_base_layer", "base"),
            toggle_layer=req.toggle_layer or config.get("psd_toggle_layer", "PLAY LIST"),
            image_subdir=req.image_subdir or config.get("psd_image_subdir", "image"),
            text_replacements=req.text_replacements,
            quality=req.quality,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"render-for-video 失敗: {e}")


class PhotoshopGenerateSceneTextRequest(BaseModel):
    video_name: Optional[str] = None         # アクティブチャンネル配下の vol フォルダ名
    video_folder: Optional[str] = None       # or 絶対パス
    image_path: Optional[str] = None         # 明示的な画像パス（指定時は video_name より優先）
    persona: Optional[str] = None            # 未指定時は per-channel config から取得


@app.post("/api/photoshop/generate-scene-text")
def api_photoshop_generate_scene_text(req: PhotoshopGenerateSceneTextRequest):
    """AI生成画像から シーンテキスト (英大文字 2-3 語) を自動生成。"""
    try:
        from scene_text_generator import generate_scene_text_for_image
    except Exception as e:
        raise HTTPException(500, f"scene_text_generator のインポート失敗: {e}")

    # 画像パス解決
    image_path = req.image_path
    if not image_path:
        config = get_dashboard_config()
        folder_path = req.video_folder
        if not folder_path and req.video_name:
            folder_path = str(Path(config["channel_folder"]) / req.video_name)
        if not folder_path:
            raise HTTPException(400, "image_path / video_folder / video_name のいずれかが必要")
        folder = Path(folder_path)
        # vol{N}.png を優先、無ければ image_subdir 配下の最初の画像
        cands = list(folder.glob("vol*.png")) + list(folder.glob("vol*.jpg"))
        if cands:
            image_path = str(cands[0])
        else:
            ps = _import_photoshop()
            swap = ps._find_swap_image(folder, config.get("psd_image_subdir", "image"))
            if not swap:
                raise HTTPException(404, f"画像が見つかりません: {folder}")
            image_path = str(swap)

    _cfg = get_dashboard_config()
    persona = req.persona
    if persona is None:
        persona = (_cfg.get("persona") or "").strip()

    try:
        text = generate_scene_text_for_image(
            image_path, persona=persona,
            tone=(_cfg.get("scene_text_tone") or ""),
            examples=_cfg.get("scene_text_examples") or [],
            forbidden_phrases=_cfg.get("scene_text_forbidden") or [],
            structure=(_cfg.get("scene_text_structure") or ""),
        )
        return {"status": "ok", "scene_text": text, "image_path": image_path}
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"generate-scene-text 失敗: {e}")


class SceneTextSuggestRequest(BaseModel):
    mode: str = "titles"   # "titles"（ライバルタイトル語彙・軽量） | "vision"（サムネ画像の実焼込文字・消費大）
    count: int = 6         # vision で読むサムネ枚数 / titles の参照件数上限


@app.post("/api/scene-text/suggest-from-benchmark")
def api_scene_text_suggest(req: SceneTextSuggestRequest):
    """アクティブチャンネルのベンチマーク（ライバル）から、文字入れ設定の提案を返す。

    mode=titles: ライバル動画タイトルの語彙からテーマ・トーンを推定（軽量・LLM テキスト）。
    mode=vision: ライバルサムネ画像の実際の焼込文字を読み取って抽出（Vision・消費大）。
    返却: {tone, examples:[], forbidden:[], structure}（UI が各欄に流し込み、ユーザーが選択/編集）。
    """
    import app_benchmark_thumbnail as _bt
    cfg = get_dashboard_config()
    persona = (cfg.get("persona") or "").strip()
    cli = (get_suno_config().get("claude_cli") or "claude").strip()
    mode = (req.mode or "titles").strip().lower()
    count = max(1, min(int(req.count or 6), 12))

    bc = _bt.load_cache() or {}
    items = []
    for ch in (bc.get("channels") or []):
        for t in (ch.get("thumbnails") or []):
            items.append({
                "title": (t.get("title") or "").strip(),
                "localPath": t.get("localPath") or "",
                "viewCount": int(t.get("viewCount", 0) or 0),
            })
    items.sort(key=lambda x: -x["viewCount"])   # 「効いてる」順
    if not items:
        raise HTTPException(409, "ベンチマークのサムネデータがありません。先にベンチマーク取込み + サムネ DL を実行してください。")

    json_shape = (
        '{\n'
        '  "tone": "comma-separated mood keywords (e.g. chill, lo-fi, study, cozy)",\n'
        '  "examples": ["3-6 ALL-CAPS 2-3 word sample phrases in the target register"],\n'
        '  "forbidden": ["phrases to avoid copying verbatim"],\n'
        '  "structure": "short syntax hint, e.g. verb+noun or adjective+noun"\n'
        '}'
    )

    try:
        from app_llm_runner import run_llm, run_llm_vision
        if mode == "vision":
            paths = [it["localPath"] for it in items
                     if it["localPath"] and Path(it["localPath"]).exists()][:count]
            if not paths:
                raise HTTPException(409, "ライバルサムネ画像がローカルにありません（benchmark/thumbs 未DL）。titles モードをお試しください。")
            prompt = (
                "You are designing the on-thumbnail TEXT style for a YouTube BGM channel by analyzing rival thumbnails.\n"
                f"Channel persona: {persona or '(unspecified)'}\n"
                f"For the {len(paths)} rival thumbnail images, READ the actual text burned into each image, "
                "then design a caption style that fits THIS channel and differentiates from the rivals.\n"
                "Output a JSON object exactly in this shape:\n"
                f"{json_shape}\n"
                "- examples: FRESH phrases in the same register (do NOT copy the rivals' exact text).\n"
                "- forbidden: the rivals' ACTUAL on-image phrases you read, verbatim.\n"
                "Output ONLY the JSON, nothing else."
            )
            raw = run_llm_vision(prompt, paths, cli_cmd=cli, timeout=240, label="scene-suggest-vision")
        else:
            titles = [it["title"] for it in items if it["title"]][:max(count, 20)]
            titles_joined = "\n".join(f"- {t}" for t in titles)
            prompt = (
                "You are designing the on-thumbnail TEXT style for a YouTube BGM channel by analyzing rival video titles.\n"
                f"Channel persona: {persona or '(unspecified)'}\n"
                f"Rival video titles ({len(titles)}):\n{titles_joined}\n\n"
                "Design a short English ALL-CAPS caption style that fits THIS channel and differentiates from rivals.\n"
                "Output a JSON object exactly in this shape:\n"
                f"{json_shape}\n"
                "Output ONLY the JSON, nothing else."
            )
            raw = run_llm(prompt, cli_cmd=cli, timeout=180, label="scene-suggest-titles")
        obj = _bt._extract_json(raw) or {}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"提案生成に失敗: {e}")

    def _as_list(v):
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [s.strip() for s in re.split(r"[\n,/]", v) if s.strip()]
        return []

    return {
        "ok": True,
        "mode": mode,
        "source_count": len(items),
        "tone": (str(obj.get("tone") or "")).strip(),
        "examples": _as_list(obj.get("examples")),
        "forbidden": _as_list(obj.get("forbidden")),
        "structure": (str(obj.get("structure") or "")).strip(),
    }


class PhotoshopRenderDualRequest(BaseModel):
    video_name: Optional[str] = None        # アクティブチャンネル配下の vol フォルダ名
    video_folder: Optional[str] = None      # or 絶対パス
    scene_text: Optional[str] = None        # 空 or 未指定なら vision で自動生成 + scene_en.txt キャッシュ
    base_layer: Optional[str] = None        # 既定: per-channel `psd_base_layer`
    scene_text_layer: Optional[str] = None  # 既定: per-channel `psd_text_layer` → "都市名_テキスト"
    playlist_layer: Optional[str] = None    # 既定: per-channel `psd_toggle_layer` → "PLAY LIST " (末尾スペース)
    image_subdir: Optional[str] = None      # 既定: per-channel `psd_image_subdir`
    quality: int = 90
    target_width: Optional[int] = None      # 空 or 未指定なら per-channel `psd_export_width` → 1920
    target_height: Optional[int] = None     # 空 or 未指定なら per-channel `psd_export_height` → 1080
    save_psd: Optional[bool] = None         # 空 or 未指定なら True (vol 固有 PSD の編集状態を保存)
    scene_text_font: Optional[str] = None   # 空 or 未指定なら per-channel `psd_text_font`


@app.post("/api/photoshop/render-dual-thumbnail")
def api_photoshop_render_dual_thumbnail(req: PhotoshopRenderDualRequest):
    """Harbor Notes 仕様: AI生成画像 + シーンテキスト中央配置で 2 層対立式の 2 枚出力。
    vol{N}.jpg (都市名OFF/PLAY LIST ON, Premiere背景画像用) と
    サムネイル.jpg (都市名ON/PLAY LIST OFF, YouTubeサムネ用) を生成。

    CLI の step_psd_composite と同等の挙動になるよう、未指定パラメータは per-channel config
    から自動補完される（scene_text は空なら vision で生成、サイズは 1920×1080、save_psd=True 等）。
    """
    config = get_dashboard_config()
    folder_path = req.video_folder
    if not folder_path and req.video_name:
        folder_path = str(Path(config["channel_folder"]) / req.video_name)
    if not folder_path:
        raise HTTPException(400, "video_folder か video_name のいずれかが必要")
    folder = Path(folder_path)
    if not folder.exists():
        raise HTTPException(404, f"フォルダが存在しません: {folder}")

    try:
        ps = _import_photoshop()
        # PSD 検索（vol{N}.psd or vol*.psd or テンプレ）
        psd = ps._find_video_psd(folder)
        # 差し替え画像検索 — CLI step_psd_composite と同じフォールバック順:
        # vol{N}.png → vol{N}_source.jpg → image_subdir 配下の任意の画像。
        # vol{N}.jpg は Photoshop 合成出力（PLAY LIST 焼き付き）なので候補から除外する。
        swap = None
        m = re.match(r"^(\d+)_", folder.name)
        vol_num = m.group(1) if m else None
        if vol_num:
            for cand_name in (f"vol{vol_num}.png", f"vol{vol_num}_source.jpg"):
                cand = folder / cand_name
                if cand.exists():
                    swap = cand
                    break
        if not swap:
            for cand in folder.glob("vol*.png"):
                if not cand.name.endswith("_source.jpg"):
                    swap = cand
                    break
        if not swap:
            image_subdir = req.image_subdir or config.get("psd_image_subdir", "image")
            swap = ps._find_swap_image(folder, image_subdir)
        if not swap:
            raise HTTPException(404, f"差し替え画像が見つかりません: {folder}")

        # scene_text 自動補完（空 or 未指定なら vision で生成 + scene_en.txt にキャッシュ）
        scene_text = (req.scene_text or "").strip()
        if not scene_text:
            cache_file = folder / "scene_en.txt"
            if cache_file.exists():
                try:
                    scene_text = cache_file.read_text(encoding="utf-8").strip()
                except Exception:
                    scene_text = ""
            if not scene_text:
                try:
                    persona = (config.get("persona") or "").strip()
                    scene_text = generate_scene_text_for_image(
                        str(swap), persona=persona,
                        tone=(config.get("scene_text_tone") or ""),
                        examples=config.get("scene_text_examples") or [],
                        forbidden_phrases=config.get("scene_text_forbidden") or [],
                        structure=(config.get("scene_text_structure") or ""),
                    )
                    if scene_text:
                        try:
                            cache_file.write_text(scene_text + "\n", encoding="utf-8")
                        except Exception:
                            pass
                except Exception:
                    scene_text = ""

        # サイズ自動補完
        tw = req.target_width or config.get("psd_export_width") or 1920
        th = req.target_height or config.get("psd_export_height") or 1080
        # save_psd デフォルト True
        save_psd = True if req.save_psd is None else bool(req.save_psd)
        # フォント自動補完
        scene_text_font = req.scene_text_font or config.get("psd_text_font") or None
        # レイヤー名（strip しない — 末尾スペース必須なケースあり）
        base_layer = req.base_layer or config.get("psd_base_layer") or "base"
        scene_text_layer = req.scene_text_layer or config.get("psd_text_layer") or "都市名_テキスト"
        playlist_layer = req.playlist_layer or config.get("psd_toggle_layer") or "PLAY LIST "

        return ps.render_dual_thumbnail(
            psd_path=str(psd),
            base_image=str(swap),
            scene_text=scene_text,
            out_dir=str(folder),
            base_layer=base_layer,
            scene_text_layer=scene_text_layer,
            playlist_layer=playlist_layer,
            quality=req.quality,
            target_width=int(tw),
            target_height=int(th),
            save_psd=save_psd,
            scene_text_font=scene_text_font,
        )
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise HTTPException(500, f"render-dual-thumbnail 失敗: {e}")


@app.post("/api/photoshop/eval")
def api_photoshop_eval(req: PhotoshopEvalRequest):
    """任意の ExtendScript を実行（デバッグ用）。"""
    if (os.environ.get("APP_ENABLE_PHOTOSHOP_EVAL") or os.environ.get("ORZZ_ENABLE_PHOTOSHOP_EVAL")) != "1":
        raise HTTPException(403, "Photoshop eval はデバッグ時のみ有効です")
    try:
        ps = _import_photoshop()
        return {"ok": True, "result": ps.run_jsx(req.code, timeout=req.timeout)}
    except Exception as e:
        raise HTTPException(500, f"eval 失敗: {e}")


# ─── API: 環境セットアップ ───

def _channel_youtube_token_path() -> Optional[Path]:
    """アクティブなチャンネルフォルダ内の `.youtube_token.json` を返す。
    チャンネルフォルダ未設定なら None。app_youtube.py の resolve_token_path と整合。"""
    try:
        cfg = get_dashboard_config()
        ch = (cfg.get("channel_folder") or "").strip()
        if not ch:
            return None
        return Path(ch) / ".youtube_token.json"
    except Exception:
        return None


def _active_channel_registry_entry() -> dict:
    """dashboard_config のアクティブチャンネルに対応する registry 行を返す。"""
    cfg = get_dashboard_config()
    active_folder = (cfg.get("channel_folder") or "").strip()
    active_name = (cfg.get("channel_name") or "").strip()
    for ch in get_channels():
        if active_folder and (ch.get("folder") or "") == active_folder:
            return ch
    for ch in get_channels():
        if active_name and (ch.get("name") or "") == active_name:
            return ch
    return {}


def _channel_registry_entry_for_folder(channel_folder: Path) -> dict:
    target = str(channel_folder)
    for ch in get_channels():
        if (ch.get("folder") or "") == target:
            return ch
    return {}


def _read_authenticated_youtube_channel(token_path: Path) -> dict:
    """OAuth トークンが実際に指している YouTube チャンネルを取得する。"""
    out = {"ok": False, "channel_id": "", "channel_title": "", "custom_url": "", "channels": [], "error": ""}
    if not token_path or not token_path.exists():
        out["error"] = "token file not found"
        return out
    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        import app_youtube as _yt_upload

        creds = Credentials.from_authorized_user_file(str(token_path), _yt_upload.SCOPES)
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleAuthRequest())
            token_path.write_text(creds.to_json(), encoding="utf-8")
        youtube = build("youtube", "v3", credentials=creds)
        info = _yt_upload.get_authenticated_channel_info(youtube)
        out.update(info)
        out["ok"] = bool(info.get("channel_id"))
    except Exception as e:
        out["error"] = str(e)[:240]
    return out


def _validate_youtube_channel_for_entry(
    token_path: Optional[Path],
    channel_entry: dict,
    *,
    fallback_name: str = "",
    raise_on_mismatch: bool = False,
) -> dict:
    """指定 token と registry 行の YouTube チャンネルを照合する。"""
    expected_id = ((channel_entry or {}).get("youtube_channel_id") or "").strip()
    expected_name = (channel_entry or {}).get("name") or fallback_name or ""
    actual = _read_authenticated_youtube_channel(token_path) if token_path else {
        "ok": False, "channel_id": "", "channel_title": "", "custom_url": "", "channels": [], "error": "token path not resolved"
    }
    actual_ids = {c.get("id") for c in actual.get("channels", []) if c.get("id")}
    if expected_id:
        matched = expected_id in actual_ids
        verified = bool(actual.get("ok"))
    elif expected_name and actual.get("channel_title"):
        def _norm_name(s: str) -> str:
            return re.sub(r"\s+", "", (s or "").strip().lower())
        matched = _norm_name(expected_name) == _norm_name(actual.get("channel_title", ""))
        verified = bool(actual.get("ok"))
    else:
        matched = None
        verified = False
    result = {
        "expected_channel_id": expected_id,
        "expected_channel_name": expected_name,
        "actual_channel_id": actual.get("channel_id", ""),
        "actual_channel_title": actual.get("channel_title", ""),
        "actual_channels": actual.get("channels", []),
        "verified": verified,
        "matched": matched,
        "error": actual.get("error", ""),
    }
    if raise_on_mismatch and expected_id and verified and not matched:
        raise HTTPException(
            409,
            "YouTube 認証チャンネルが違います。"
            f"期待: {expected_name} ({expected_id}) / "
            f"実際: {actual.get('channel_title') or '?'} ({actual.get('channel_id') or '?'})。"
            "このチャンネルで YouTube 再認証し、ブランドチャンネル選択画面で正しいチャンネルを選んでください。",
        )
    if raise_on_mismatch and not expected_id and verified and matched is False:
        raise HTTPException(
            409,
            "YouTube 認証チャンネルが違います。"
            f"期待: {expected_name} / "
            f"実際: {actual.get('channel_title') or '?'} ({actual.get('channel_id') or '?'})。"
            "このチャンネルに YouTube URL を登録するか、YouTube 再認証で正しいブランドチャンネルを選んでください。",
        )
    if raise_on_mismatch and expected_id and not verified:
        raise HTTPException(
            409,
            f"YouTube 認証チャンネルを確認できません: {actual.get('error') or 'unknown'}。"
            "誤アップロード防止のため停止しました。YouTube 再認証してください。",
        )
    return result


def _validate_active_youtube_channel(token_path: Optional[Path], *, raise_on_mismatch: bool = False) -> dict:
    """アクティブチャンネル登録情報と OAuth トークンの実チャンネルを照合する。"""
    ch = _active_channel_registry_entry()
    return _validate_youtube_channel_for_entry(
        token_path, ch, fallback_name=get_dashboard_config().get("channel_name", ""),
        raise_on_mismatch=raise_on_mismatch,
    )


def _read_youtube_token_status(token_path: Path) -> dict:
    """指定されたトークンファイルの妥当性を返す（中身は返さない）。"""
    out = {"path": str(token_path), "exists": token_path.exists(),
           "token_valid": False, "scopes": [], "expiry": "",
           "has_readonly": False, "has_refresh_token": False}
    if not token_path.exists():
        return out
    try:
        td = json.loads(token_path.read_text(encoding="utf-8"))
        out["scopes"] = td.get("scopes", [])
        out["expiry"] = td.get("expiry", "")
        out["has_readonly"] = "https://www.googleapis.com/auth/youtube.readonly" in out["scopes"]
        out["has_refresh_token"] = bool(td.get("refresh_token"))
        from datetime import datetime, timezone
        if out["expiry"]:
            exp = datetime.fromisoformat(out["expiry"].replace("Z", "+00:00"))
            out["token_valid"] = exp > datetime.now(timezone.utc)
    except Exception:
        pass
    return out


@app.get("/api/credentials/status")
def api_credentials_status():
    """各外部サービスの認証状態を返す（シークレット値は含まない）"""
    result = {}

    # YouTube OAuth
    yt_secret = CONFIG_DIR / "youtube_client_secret.json"
    legacy_token = CONFIG_DIR / "youtube_token.json"
    channel_token = _channel_youtube_token_path()
    dc = get_dashboard_config()

    # 「アクティブな」トークンはアクティブチャンネルの .youtube_token.json に固定。
    # レガシーグローバルトークンは表示だけ残し、チャンネル別運用では OK 判定に使わない。
    primary = channel_token
    primary_status = _read_youtube_token_status(primary) if primary else {
        "path": "", "exists": False, "token_valid": False,
        "scopes": [], "expiry": "", "has_readonly": False, "has_refresh_token": False
    }
    channel_check = _validate_active_youtube_channel(primary, raise_on_mismatch=False) if primary_status["exists"] else {
        "expected_channel_id": (_active_channel_registry_entry().get("youtube_channel_id") or ""),
        "expected_channel_name": dc.get("channel_name", ""),
        "actual_channel_id": "",
        "actual_channel_title": "",
        "actual_channels": [],
        "verified": False,
        "matched": None,
        "error": "token file not found",
    }

    yt = {
        "client_secret_exists": yt_secret.exists(),
        # 旧 UI 互換のためのフィールド（プライマリの値を入れる）
        "token_exists": primary_status["exists"],
        "token_valid": primary_status["token_valid"],
        "scopes": primary_status["scopes"],
        "expiry": primary_status["expiry"],
        "has_readonly": primary_status["has_readonly"],
        "has_refresh_token": primary_status["has_refresh_token"],
        # 新フィールド: チャンネル別 / レガシーそれぞれの状態
        "active_token_path": primary_status["path"],
        "active_channel_name": dc.get("channel_name", ""),
        "active_channel_folder": dc.get("channel_folder", ""),
        "uses_legacy_fallback": False,
        "channel_verification": channel_check,
        "channel_token": _read_youtube_token_status(channel_token) if channel_token else None,
        "legacy_token": _read_youtube_token_status(legacy_token),
    }
    result["youtube"] = yt

    # Claude CLI
    result["claude_cli"] = {
        "available": bool(shutil.which(get_suno_config().get("claude_cli") or "claude")),
        "command": get_suno_config().get("claude_cli") or "claude",
    }
    result["codex_cli"] = {
        "available": bool(shutil.which(get_suno_config().get("codex_cli") or "codex")),
        "command": get_suno_config().get("codex_cli") or "codex",
    }

    # Discord notification
    discord_cfg = CONFIG_DIR / "discord_config.json"
    result["discord"] = {"configured": discord_cfg.exists()}
    result["line"] = {"configured": False, "deprecated": True}

    # SUNO (Playwright profile)
    suno_profile = CONFIG_DIR / "chromium_profile"
    result["suno_browser"] = {"profile_exists": suno_profile.exists()}

    # ffmpeg
    result["ffmpeg"] = {"available": bool(shutil.which("ffmpeg"))}

    # Premiere (pymiere)
    try:
        __import__("pymiere")
        result["pymiere"] = {"installed": True}
    except ImportError:
        result["pymiere"] = {"installed": False}

    # Dashboard config
    result["config"] = {
        "persona_set": bool(dc.get("persona")),
        "rival_channels_count": len(dc.get("rival_channels", [])),
        "channel_folder_exists": Path(dc.get("channel_folder", "")).exists() if dc.get("channel_folder") else False,
    }

    return result


@app.post("/api/credentials/reauth-youtube")
def api_reauth_youtube():
    """YouTube OAuth を再認証。
    アクティブチャンネルのトークン（<channel_folder>/.youtube_token.json）を削除する。
    次の API 呼び出し時にブラウザで OAuth 同意画面が開くので、
    ブランドアカウントのチャンネル選択画面で正しいチャンネルを選ぶこと。"""
    deleted = []
    legacy = CONFIG_DIR / "youtube_token.json"
    channel = _channel_youtube_token_path()
    targets = [channel] if channel is not None else [legacy]
    for p in targets:
        if p is not None and p.exists():
            try:
                p.unlink()
                deleted.append(str(p))
            except Exception as e:
                print(f"[reauth] {p} 削除失敗: {e}")
    msg = "トークンを削除しました。" if deleted else "削除対象のトークンはありませんでした。"
    msg += " 次の YouTube API 呼び出し時にブラウザで再認証が求められます（ブランドアカウントの場合はチャンネル選択画面でアップロード先を選んでください）。"
    return {"status": "ok", "message": msg, "deleted": deleted}


@app.get("/api/setup/status")
def api_setup_status():
    """現在のPC の環境セットアップ状態を確認"""
    checks = {}
    # Python パッケージ
    for pkg in ["fastapi", "uvicorn", "playwright"]:
        try:
            __import__(pkg)
            checks[pkg] = True
        except ImportError:
            checks[pkg] = False
    # Playwright ブラウザ
    pw_cache = HOME / "Library/Caches/ms-playwright"
    checks["chromium"] = pw_cache.exists() and any(pw_cache.glob("chromium*"))
    # 設定ファイル
    checks["suno_config"] = SUNO_CONFIG.exists()
    _suno_cfg = get_suno_config()
    if _suno_cfg.get("provider") in ("claude", "codex"):
        # CLI プロバイダー: APIキー不要、CLIの存在で判定
        cli_key = "codex_cli" if _suno_cfg.get("provider") == "codex" else "claude_cli"
        cli_default = "codex" if _suno_cfg.get("provider") == "codex" else "claude"
        checks["suno_api_key"] = bool(shutil.which(_suno_cfg.get(cli_key) or cli_default))
    else:
        checks["suno_api_key"] = bool(_suno_cfg.get("api_key"))
    checks["youtube_secret"] = (CONFIG_DIR / "youtube_client_secret.json").exists()
    checks["discord_config"] = (CONFIG_DIR / "discord_config.json").exists()
    checks["chromium_profile"] = (CONFIG_DIR / "chromium_profile").exists()
    return {"checks": checks}

@app.post("/api/setup/run")
async def api_setup_run():
    """環境セットアップスクリプトを実行"""
    setup_script = SHARED_BASE / "Python" / "setup.sh"
    if not setup_script.exists():
        raise HTTPException(404, "setup.sh が見つかりません")
    task_logs["setup"] = []
    proc = subprocess.Popen(
        ["bash", str(setup_script)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    active_tasks["setup"] = proc
    _stream_subprocess(proc, "setup")
    return {"status": "started"}

@app.get("/api/setup/logs")
def api_setup_logs():
    return {"logs": task_logs.get("setup", [])[-50:]}

# ─── WebSocket ───
@app.websocket("/ws/logs/{task_name}")
async def ws_logs(websocket: WebSocket, task_name: str):
    await websocket.accept()
    last_idx = 0
    try:
        while True:
            logs = task_logs.get(task_name, [])
            if len(logs) > last_idx:
                for line in logs[last_idx:]: await websocket.send_text(line)
                last_idx = len(logs)
            await asyncio.sleep(0.5)
    except Exception:
        pass

# ─── フロントエンド ───
@app.get("/")
def index():
    html = WEB_DIR / "static" / "index.html"
    if html.exists():
        return FileResponse(str(html))
    return HTMLResponse("<h1>orzz. Dashboard</h1><p>static/index.html が見つかりません</p>")

@app.get("/login.html")
def login_page():
    p = WEB_DIR / "static" / "login.html"
    if p.exists():
        return FileResponse(str(p))
    raise HTTPException(404, "login.html not found")

@app.get("/manifest.json")
def manifest():
    """設定駆動の動的 manifest.json を返す（PWA 表示名がブランド設定と連動する）"""
    cfg = get_dashboard_config()
    brand_full = cfg.get("brand_full") or DEFAULT_DASHBOARD_CONFIG["brand_full"]
    brand_short = cfg.get("brand_short") or DEFAULT_DASHBOARD_CONFIG["brand_short"]
    initial = (brand_short[:1] or "A").upper()
    icon_template = (
        "data:image/svg+xml;utf8,"
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {vb}'>"
        "<rect width='{w}' height='{h}' rx='{r}' fill='%230a0a0a'/>"
        "<text x='{cx}' y='{cy}' font-size='{fs}' text-anchor='middle' "
        "fill='%233b82f6' font-family='-apple-system,sans-serif' font-weight='700'>{i}</text>"
        "</svg>"
    )
    return {
        "name": brand_full,
        "short_name": brand_short[:12],
        "description": "クリエイター向け制作パイプライン自動化ダッシュボード",
        "start_url": "/",
        "scope": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#0a0a0a",
        "theme_color": "#0a0a0a",
        "icons": [
            {
                "src": icon_template.format(vb="192 192", w=192, h=192, r=32, cx=96, cy=128, fs=96, i=initial),
                "sizes": "192x192", "type": "image/svg+xml",
            },
            {
                "src": icon_template.format(vb="512 512", w=512, h=512, r=80, cx=256, cy=340, fs=256, i=initial),
                "sizes": "512x512", "type": "image/svg+xml",
            },
        ],
    }

@app.get("/sw.js")
def service_worker():
    p = WEB_DIR / "static" / "sw.js"
    if p.exists():
        return FileResponse(str(p), media_type="application/javascript")
    raise HTTPException(404, "sw.js not found")

def _find_free_port(start=8888, end=8899, host="127.0.0.1"):
    import socket
    for p in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return None


# ─── 自動書き出しキュー（Sprint 4-E） ───────────────
import hashlib

EXPORT_QUEUE_FILE = CONFIG_DIR / "export_queue.json"
EXPORT_RULES_FILE = CONFIG_DIR / "export_rules.json"

DEFAULT_EXPORT_RULES = {
    "watcher_enabled": False,
    "scan_interval_sec": 30,
    # mp4 サイズが変化しなくなって完了とみなすまでの猶予秒数。
    # AME はフレーム解析やアトムフィックスで一時的に書き込みを止めるため、
    # 短すぎると未完了で done 判定されて次ジョブと AME 競合する。
    "size_stable_window_sec": 30,
    # mp4 完了検知の二段階タイムアウト（堅牢化）
    # Phase1: encodeSequence 後、mp4 が出現するまでの上限。
    #   AME 投入失敗（startBatch が空振り、AME がジョブ受け取らず等）を早期検知。
    "mp4_appear_timeout_sec": 300,   # 5 分
    # Phase2: mp4 出現後、サイズ安定で done を出すまでの上限（実エンコード時間の上限）
    "mp4_stable_timeout_sec": 1800,  # 30 分
    "auto_upload_after_export": False,   # ME 完了後に YouTube へ自動アップロード
    "auto_upload_privacy": "unlisted",   # 自動アップロード時の公開設定
    "rules": {
        "require_prproj": True,
        "require_music": True,
        "require_srt": True,
        "skip_if_mp4": True,
    },
}

def get_export_rules():
    cfg = DEFAULT_EXPORT_RULES.copy()
    cfg["rules"] = DEFAULT_EXPORT_RULES["rules"].copy()
    loaded = load_json(EXPORT_RULES_FILE, {})
    cfg.update({k: v for k, v in loaded.items() if k != "rules"})
    if loaded.get("rules"):
        cfg["rules"].update(loaded["rules"])
    return cfg

def save_export_rules(cfg):
    save_json(EXPORT_RULES_FILE, cfg)

def get_export_queue():
    data = load_json(EXPORT_QUEUE_FILE, {"items": []})
    if not isinstance(data, dict) or "items" not in data:
        return {"items": []}
    return data

def save_export_queue(data):
    save_json(EXPORT_QUEUE_FILE, data)


def get_export_ignore_list() -> list:
    """AME watcher が無視すべき video_name のリスト（per-channel）。
    既に別で書き出し済 / 手動非表示にしたフォルダがここに入る。"""
    cc = load_channel_config()
    raw = cc.get("export_ignore_list") or []
    if not isinstance(raw, list):
        return []
    # 重複・空文字除去
    return sorted({str(x).strip() for x in raw if isinstance(x, (str, int)) and str(x).strip()})


def add_to_export_ignore_list(video_name: str) -> list:
    if not video_name:
        return get_export_ignore_list()
    cc = load_channel_config()
    cur = list(cc.get("export_ignore_list") or [])
    if video_name not in cur:
        cur.append(video_name)
        cc["export_ignore_list"] = cur
        save_channel_config(cc)
    return get_export_ignore_list()


def remove_from_export_ignore_list(video_name: str) -> list:
    cc = load_channel_config()
    cur = list(cc.get("export_ignore_list") or [])
    if video_name in cur:
        cur = [x for x in cur if x != video_name]
        cc["export_ignore_list"] = cur
        save_channel_config(cc)
    return get_export_ignore_list()

def _fingerprint(prproj_path: str, mtime: float) -> str:
    return hashlib.sha256(f"{prproj_path}|{int(mtime)}".encode()).hexdigest()[:16]

def _scan_video_folder_for_export(folder: Path) -> dict:
    """完成判定: prproj / music / srt あり、mp4 なし。export_path も考慮。"""
    if not folder.is_dir():
        return {"completable": False}
    rules = get_export_rules()["rules"]
    name = folder.name
    prproj_files = list(folder.glob("*vol*.prproj")) + list(folder.glob("*.prproj"))
    has_prproj = bool(prproj_files)
    music_dir = folder / "music"
    has_music = music_dir.exists() and any(_is_usable_audio_file(p) for p in music_dir.iterdir())
    has_srt = bool(list(folder.glob("*.srt")))
    # MP4 は flat 配置（<export_path>/<prefix>_vol<num>.mp4）も含めて全配置を探索
    has_mp4 = _find_exported_mp4(folder, name) is not None
    completable = True
    if rules.get("require_prproj") and not has_prproj: completable = False
    if rules.get("require_music") and not has_music: completable = False
    if rules.get("require_srt") and not has_srt: completable = False
    if rules.get("skip_if_mp4") and has_mp4: completable = False
    return {
        "completable": completable,
        "video_name": name,
        "prproj_path": str(prproj_files[0]) if prproj_files else "",
        "has_prproj": has_prproj, "has_music": has_music, "has_srt": has_srt, "has_mp4": has_mp4,
        "folder": str(folder),
    }

def _scan_all_for_export(include_ignored: bool = False) -> list:
    """書き出し可能フォルダをスキャン。
    include_ignored=False なら ignore list（チャンネル別）に入っている video_name は除外。
    また pending/running 中の video_name もスキャン対象外（保存で mtime が変わって
    fingerprint が変化 → 別アイテム扱いで重複登録、を防ぐ）。
    さらに「現在のチャンネル prefix（例: WW）と一致しない」フォルダも除外。
    複数チャンネル並列運用時に、書き出しキューが他チャンネル分まで拾わないようにする。"""
    config = get_dashboard_config()
    channel_dir = Path(config.get("channel_folder", ""))
    if not channel_dir.exists():
        return []
    ignore_set = set() if include_ignored else set(get_export_ignore_list())
    if not include_ignored:
        try:
            q = get_export_queue()
            ignore_set |= {x.get("video_name", "") for x in q["items"]
                           if x.get("status") in ("pending", "running")}
            ignore_set.discard("")
        except Exception:
            pass
    # チャンネル prefix（例: "WW", "orzz" 等）。フォルダ名 "<num>_<prefix>_<date>" の prefix と
    # 一致するもののみスキャン対象とする。設定が空なら従来通り全件。
    channel_prefix = ""
    try:
        channel_prefix = (get_file_prefix() or "").strip()
    except Exception:
        pass
    # _<prefix>_ パターンで matching するための正規化
    prefix_pattern = re.compile(rf"^\d+_{re.escape(channel_prefix)}_", re.IGNORECASE) if channel_prefix else None

    out = []
    for d in sorted(channel_dir.iterdir()):
        if not d.is_dir() or d.name.startswith('.'):
            continue
        if d.name in ignore_set:
            continue
        # チャンネル prefix と一致しないフォルダはスキップ（他チャンネルの動画フォルダ対策）
        if prefix_pattern is not None and not prefix_pattern.match(d.name):
            continue
        info = _scan_video_folder_for_export(d)
        if info.get("completable"):
            out.append(info)
    return out

def _enqueue_export(info: dict, force: bool = False) -> dict:
    """キューに追加。ignore list に入っている動画は force=True でない限り追加しない。"""
    video_name = info.get("video_name", "")
    if not force and video_name in set(get_export_ignore_list()):
        return {"status": "skip", "reason": "ignored", "video_name": video_name}
    q = get_export_queue()
    prproj = info.get("prproj_path", "")
    if not prproj:
        return {"status": "skip", "reason": "prproj_missing"}
    try:
        mtime = Path(prproj).stat().st_mtime
    except Exception:
        return {"status": "skip", "reason": "prproj_stat_failed"}
    fp = _fingerprint(prproj, mtime)
    # 重複チェック: 同 video_name で pending/running 中ならスキップ。
    # Premiere で開いて保存すると prproj の mtime が変わって fingerprint も変わるため、
    # fingerprint 単独だと「同じ動画の二重登録」を防げない（既存重複バグの根本原因）。
    if not force:
        for item in q["items"]:
            if (item.get("video_name") == video_name
                and item.get("status") in ("pending", "running")):
                return {"status": "duplicate",
                        "fingerprint": item.get("fingerprint"),
                        "reason": "already_in_progress"}
    # 同 fingerprint かつ done → 同じ書き出しを再実行しない
    # （prproj の更新時刻が変われば fingerprint が変わるので新規追加される）
    for item in q["items"]:
        if item.get("fingerprint") == fp and item.get("status") == "done":
            return {"status": "skip", "reason": "already_done", "fingerprint": fp}
    item = {
        "fingerprint": fp,
        "video_name": info["video_name"],
        "prproj_path": prproj,
        "folder": info["folder"],
        "status": "pending",
        "added_at": datetime.utcnow().isoformat() + "Z",
        "started_at": "",
        "completed_at": "",
        "output_path": "",
        "error": "",
    }
    q["items"].append(item)
    save_export_queue(q)
    return {"status": "queued", "fingerprint": fp, "item": item}

def _channel_slug() -> str:
    """dashboard_config.json の channel_name をスラッグ化。多チャンネル展開時の path 用。"""
    cfg = get_dashboard_config()
    raw = (cfg.get("channel_name") or "").strip()
    if not raw:
        # フォールバック: channel_folder の basename から推定
        cf = (cfg.get("channel_folder") or "").strip()
        if cf:
            raw = Path(cf).name
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", raw).strip("_")
    return slug or "channel"


def _resolve_external_export_dir() -> Optional[Path]:
    """設定 export_path を {channel} などのテンプレートで解決する。
    返り値:
      - 設定が無ければ None（= 旧来のチャンネルフォルダ内書き出し）
      - 設定はあるがマウント未確認なら ValueError 発生（fail-fast）
    """
    cfg = get_dashboard_config()
    raw = (cfg.get("export_path") or "").strip()
    if not raw:
        return None
    resolved = raw.replace("{channel}", _channel_slug())
    p = Path(resolved).expanduser()
    # mount チェック: /Volumes/<X>/... の場合、X までの path が存在することを必須にする。
    # 親が /Volumes 配下のときは外部マウントとみなす。
    parents = list(p.parents)
    is_external_volume = any(str(par) == "/Volumes" for par in parents) or str(p).startswith("/Volumes/")
    if is_external_volume:
        # /Volumes/<NAME> が存在しなければ未マウント
        try:
            volname_parent = next((par for par in parents if str(par.parent) == "/Volumes"), None)
            if volname_parent is None and str(p.parent) == "/Volumes":
                volname_parent = p
            if volname_parent is not None and not volname_parent.exists():
                raise ValueError(f"外部ボリュームが未マウント: {volname_parent}")
        except StopIteration:
            pass
    return p


def _resolve_output_path(folder: Path, video_name: str) -> Path:
    """書き出し先解決。
    export_path 設定があれば <export_path>/<video_name>/<file>.mp4 形式。
    無ければチャンネルフォルダ直下。
    """
    m = re.match(r"^(\d+)_", video_name)
    num = m.group(1) if m else "00"
    prefix = get_file_prefix()
    fname = f"{prefix}_vol{num}.mp4"
    ext_dir = _resolve_external_export_dir()
    if ext_dir is not None:
        target = ext_dir / video_name
        target.mkdir(parents=True, exist_ok=True)
        return target / fname
    return folder / fname


MANUAL_EXPORTED_VIDEO_FILE = "manual_exported_video.json"


def _is_exported_mp4_candidate(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() == ".mp4" and path.name != "audio-spectrum01.mp4"


def _read_manual_exported_mp4(folder: Path) -> Optional[Path]:
    marker = folder / MANUAL_EXPORTED_VIDEO_FILE
    if not marker.exists():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        raw = (data.get("path") or "").strip()
    except Exception:
        return None
    if not raw:
        return None
    p = Path(raw).expanduser()
    try:
        safe = _safe_user_path(str(p))
    except HTTPException:
        return None
    if _is_exported_mp4_candidate(safe):
        return safe
    return None


def _write_manual_exported_mp4(folder: Path, path: Path) -> dict:
    safe = _safe_user_path(str(path))
    if not _is_exported_mp4_candidate(safe):
        raise HTTPException(400, "MP4 ファイルを選択してください")
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "path": str(safe),
        "filename": safe.name,
        "size": safe.stat().st_size,
        "registered_at": datetime.now().isoformat(),
    }
    (folder / MANUAL_EXPORTED_VIDEO_FILE).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return payload


def _mp4_candidate_payload(path: Path, source: str) -> dict:
    try:
        st = path.stat()
        size = st.st_size
        mtime = datetime.fromtimestamp(st.st_mtime).isoformat()
    except Exception:
        size = 0
        mtime = ""
    return {
        "path": str(path),
        "filename": path.name,
        "folder": str(path.parent),
        "source": source,
        "size": size,
        "size_mb": round(size / 1024 / 1024, 1) if size else 0,
        "mtime": mtime,
    }


def _collect_mp4_candidates(folder: Path, video_name: str, extra_folder: Optional[Path] = None) -> List[dict]:
    seen = set()
    out = []

    def add(path: Optional[Path], source: str):
        if path is None:
            return
        try:
            p = path.expanduser().resolve(strict=False)
        except Exception:
            return
        if str(p) in seen or not _is_exported_mp4_candidate(p):
            return
        seen.add(str(p))
        out.append(_mp4_candidate_payload(p, source))

    add(_read_manual_exported_mp4(folder), "manual")
    for f in folder.glob("*vol*.mp4"):
        add(f, "video_folder")
    for f in folder.glob("*.mp4"):
        add(f, "video_folder")

    try:
        ext_dir = _resolve_external_export_dir()
    except Exception:
        ext_dir = None
    if ext_dir is not None and ext_dir.exists():
        per_video = ext_dir / video_name
        if per_video.exists():
            for f in per_video.glob("*.mp4"):
                add(f, "export_path")
        m = re.match(r"^(\d+)_", video_name)
        if m:
            num = m.group(1)
            prefix = get_file_prefix()
            base = f"{prefix}_vol{num}"
            for f in ext_dir.glob(f"{base}*.mp4"):
                tail = f.stem[len(base):]
                if tail == "" or not tail[0].isdigit():
                    add(f, "export_path")

    if extra_folder is not None:
        safe = _safe_user_path(str(extra_folder))
        if not safe.exists() or not safe.is_dir():
            raise HTTPException(404, f"フォルダが見つかりません: {safe}")
        scanned = 0
        for f in safe.rglob("*.mp4"):
            add(f, "selected_folder")
            scanned += 1
            if scanned >= 200:
                break

    out.sort(key=lambda x: (x.get("source") == "manual", x.get("mtime") or ""), reverse=True)
    return out


def _find_exported_mp4(folder: Path, video_name: str) -> Optional[Path]:
    """書き出し済み MP4 を複数の配置から探索。
    1) 手動登録済み MP4: <folder>/manual_exported_video.json
    2) チャンネルフォルダ内: <folder>/*.mp4
    3) export_path のサブフォルダ: <export_path>/<video_name>/*.mp4
    4) export_path 直下の flat 配置: <export_path>/<prefix>_vol<num>{suffix}.mp4
       （旧運用との互換。vol10 が vol1 にマッチしないよう数字境界をチェック）
    """
    manual = _read_manual_exported_mp4(folder)
    if manual is not None:
        return manual
    for f in folder.glob("*vol*.mp4"):
        if _is_exported_mp4_candidate(f):
            return f
    for f in folder.glob("*.mp4"):
        if _is_exported_mp4_candidate(f):
            return f
    try:
        ext_dir = _resolve_external_export_dir()
    except Exception:
        ext_dir = None
    if ext_dir is None or not ext_dir.exists():
        return None
    per_video = ext_dir / video_name
    if per_video.exists():
        for f in per_video.glob("*vol*.mp4"):
            if _is_exported_mp4_candidate(f):
                return f
        for f in per_video.glob("*.mp4"):
            if _is_exported_mp4_candidate(f):
                return f
    m = re.match(r"^(\d+)_", video_name)
    if not m:
        return None
    num = m.group(1)
    prefix = get_file_prefix()
    base = f"{prefix}_vol{num}"
    exact = ext_dir / f"{base}.mp4"
    if _is_exported_mp4_candidate(exact):
        return exact
    for f in ext_dir.glob(f"{base}*.mp4"):
        tail = f.stem[len(base):]
        if (tail == "" or not tail[0].isdigit()) and _is_exported_mp4_candidate(f):
            return f
    return None

async def _wait_for_mp4_complete(output_path: Path,
                                  fingerprint: Optional[str] = None) -> bool:
    """mp4 完了検知を二段階で行う。
    Phase 1: 出現待ち（mp4_appear_timeout_sec、既定 5 分）。
      AME が encodeSequence を受け取って書き込みを始めるまでを待つ。
      ここで時間切れなら「AME が投入を取りこぼした」可能性が高く、即 error で次の pending へ。
    Phase 2: 安定待ち（mp4_stable_timeout_sec、既定 30 分）。
      出現後、サイズが size_stable_window_sec（既定 30 秒）変化しなければ done。

    fingerprint があれば _ame_progress に進捗（phase / size_bytes / elapsed_sec）を更新する。
    """
    rules = get_export_rules()
    stable_window = int(rules.get("size_stable_window_sec", 30))
    appear_timeout = int(rules.get("mp4_appear_timeout_sec", 300))
    stable_timeout = int(rules.get("mp4_stable_timeout_sec", 1800))

    # ── Phase 1: mp4 が出現するまで ──
    appear_start = time.time()
    while not output_path.exists():
        elapsed = time.time() - appear_start
        if elapsed > appear_timeout:
            if fingerprint:
                _ame_progress[fingerprint] = {
                    **_ame_progress.get(fingerprint, {}),
                    "size_bytes": 0,
                    "rate_bytes_per_sec": 0,
                    "elapsed_sec": int(elapsed),
                    "stable_for_sec": 0,
                    "phase": "appear_timeout",
                }
            return False
        if fingerprint:
            _ame_progress[fingerprint] = {
                "size_bytes": 0,
                "rate_bytes_per_sec": 0,
                "elapsed_sec": int(elapsed),
                "stable_for_sec": 0,
                "phase": "waiting_appear",
            }
        await asyncio.sleep(5)

    # ── Phase 2: 出現後、サイズ安定まで ──
    stable_start = time.time()
    last_size = -1
    last_change = time.time()
    samples: list[tuple[float, int]] = []
    while time.time() - stable_start < stable_timeout:
        now = time.time()
        try:
            size = output_path.stat().st_size
        except Exception:
            size = -1
        if size != last_size:
            last_size = size
            last_change = now
            samples.append((now, size))
            if len(samples) > 30:
                samples = samples[-30:]
            if fingerprint:
                if len(samples) >= 2:
                    dt = samples[-1][0] - samples[0][0]
                    db = samples[-1][1] - samples[0][1]
                    rate = (db / dt) if dt > 0 else 0
                else:
                    rate = 0
                _ame_progress[fingerprint] = {
                    "size_bytes": size,
                    "rate_bytes_per_sec": int(rate),
                    "elapsed_sec": int(now - stable_start),
                    "stable_for_sec": 0,
                    "phase": "writing",
                }
        elif size > 0 and (now - last_change) >= stable_window:
            if fingerprint:
                _ame_progress[fingerprint] = {
                    "size_bytes": size,
                    "rate_bytes_per_sec": 0,
                    "elapsed_sec": int(now - stable_start),
                    "stable_for_sec": int(now - last_change),
                    "phase": "done",
                }
            return True
        else:
            if fingerprint:
                _ame_progress.setdefault(fingerprint, {}).update({
                    "stable_for_sec": int(now - last_change),
                    "phase": "stabilizing" if size > 0 else "writing",
                })
        await asyncio.sleep(2)
    if fingerprint:
        _ame_progress[fingerprint] = {**_ame_progress.get(fingerprint, {}), "phase": "stable_timeout"}
    return False


# AME 進捗ストア（fingerprint → {size_bytes, rate_bytes_per_sec, elapsed_sec, phase}）
_ame_progress: dict[str, dict] = {}

# 実行中の subprocess を fingerprint で参照可能にする（停止/キャンセル用）
_active_export_proc: dict[str, "asyncio.subprocess.Process"] = {}


def _kill_active_proc(fingerprint: str) -> bool:
    proc = _active_export_proc.get(fingerprint)
    if not proc:
        return False
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception as e:
        print(f"[export] kill failed fp={fingerprint}: {e}")
        return False
    return True

async def _process_export_item(item: dict) -> dict:
    """単一アイテムを処理: Premiere で開いて encodeSequence → mp4 完了待ち。"""
    item["status"] = "running"
    item["started_at"] = datetime.utcnow().isoformat() + "Z"
    q = get_export_queue()
    for x in q["items"]:
        if x["fingerprint"] == item["fingerprint"]: x.update(item)
    save_export_queue(q)

    folder = Path(item["folder"])
    # 外部 SSD 設定があればマウント確認 → 未マウントなら即エラー
    try:
        output_path = _resolve_output_path(folder, item["video_name"])
    except ValueError as e:
        item["status"] = "error"
        item["error"] = f"外部書き出し先が利用不可: {e}"
        item["completed_at"] = datetime.utcnow().isoformat() + "Z"
        q = get_export_queue()
        for x in q["items"]:
            if x["fingerprint"] == item["fingerprint"]: x.update(item)
        save_export_queue(q)
        return item
    item["output_path"] = str(output_path)

    # Premiere スクリプト経由で対象 .prproj を開いてから encodeSequence
    # 外部 export_path 設定時は AME に SSD パスを直接渡し、本体側 mp4 は作らない
    cmd = [sys.executable, "-u", str(PREMIERE_SCRIPT), "--export-only", "--project", item["prproj_path"]]
    is_external = output_path.parent != folder
    if is_external:
        cmd += ["--output-path", str(output_path)]
    # サブプロセスの出力をファイルに残す（ハング時のデバッグ用）
    log_path = CONFIG_DIR / f"export_log_{item['fingerprint']}.txt"
    try:
        log_f = open(log_path, "w", encoding="utf-8")
    except Exception:
        log_f = None
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=(log_f if log_f else asyncio.subprocess.PIPE),
        stderr=asyncio.subprocess.STDOUT,
    )
    _active_export_proc[item["fingerprint"]] = proc
    # タイムアウト: クラウド DL を考慮しても 30 分で十分。それ以上はハング扱い。
    EXPORT_SUBPROC_TIMEOUT = 30 * 60
    try:
        try:
            await asyncio.wait_for(proc.wait(), timeout=EXPORT_SUBPROC_TIMEOUT)
        except asyncio.TimeoutError:
            try: proc.kill()
            except Exception: pass
            try: await proc.wait()
            except Exception: pass
            if log_f:
                try: log_f.close()
                except Exception: pass
            item["status"] = "error"
            item["error"] = f"Premiere 起動 / 書き出しがタイムアウト（{EXPORT_SUBPROC_TIMEOUT//60}分）。ログ: {log_path}"
            item["completed_at"] = datetime.utcnow().isoformat() + "Z"
            q = get_export_queue()
            for x in q["items"]:
                if x["fingerprint"] == item["fingerprint"]: x.update(item)
            save_export_queue(q)
            return item
        if log_f:
            try: log_f.close()
            except Exception: pass
        # 外部から kill された場合は returncode が負（SIGKILL=-9 等）になる
        if proc.returncode is not None and proc.returncode < 0:
            item["status"] = "error"
            item["error"] = f"キャンセル（signal={-proc.returncode}）"
            item["completed_at"] = datetime.utcnow().isoformat() + "Z"
            q = get_export_queue()
            for x in q["items"]:
                if x["fingerprint"] == item["fingerprint"]: x.update(item)
            save_export_queue(q)
            return item
        if proc.returncode != 0:
            # ログファイルから末尾を読んで error に格納
            tail = ""
            try:
                if log_path.exists():
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
            except Exception:
                pass
            item["status"] = "error"
            item["error"] = tail or f"premiere subprocess exit {proc.returncode}"
            item["completed_at"] = datetime.utcnow().isoformat() + "Z"
            q = get_export_queue()
            for x in q["items"]:
                if x["fingerprint"] == item["fingerprint"]: x.update(item)
            save_export_queue(q)
            return item
    finally:
        _active_export_proc.pop(item["fingerprint"], None)

    # mp4 完了待ち（appear/stable の二段階）
    completed = await _wait_for_mp4_complete(output_path, fingerprint=item.get("fingerprint"))
    if completed:
        item["status"] = "done"
    else:
        prog = _ame_progress.get(item.get("fingerprint", ""), {})
        phase = prog.get("phase", "")
        item["status"] = "error"
        if phase == "appear_timeout":
            item["error"] = (
                f"mp4 が出現せず（{prog.get('elapsed_sec', 0)}秒）。"
                "AME がジョブを取りこぼした可能性。AME の起動状態と書き出し先を確認してください。"
            )
        elif phase == "stable_timeout":
            item["error"] = (
                f"mp4 サイズが安定せず（{prog.get('elapsed_sec', 0)}秒）。"
                "AME ハング / ストレージ書き込み詰まりの可能性。"
            )
        else:
            item["error"] = f"mp4_timeout (phase={phase or 'unknown'})"
    item["completed_at"] = datetime.utcnow().isoformat() + "Z"
    q = get_export_queue()
    for x in q["items"]:
        if x["fingerprint"] == item["fingerprint"]: x.update(item)
    save_export_queue(q)

    # 完了したフォルダは ignore list に自動追加（watcher が再検知しないように）
    if item["status"] == "done":
        try:
            add_to_export_ignore_list(item.get("video_name", ""))
        except Exception as e:
            print(f"[export_processor] ignore-list add failed: {e}")

    # 書き出し完了後の YouTube 自動アップロード
    # 動画ファイルは output_path（外部 SSD の可能性あり）、メタは channel folder から
    if item["status"] == "done":
        try:
            rules = get_export_rules()
            if rules.get("auto_upload_after_export"):
                privacy = (rules.get("auto_upload_privacy") or "unlisted").strip() or "unlisted"
                _prepare_youtube_meta(folder)
                _kick_auto_upload(folder, privacy=privacy, video_path=str(output_path))
        except Exception as e:
            print(f"[export_processor] auto_upload error: {e}")

    return item


def _prepare_youtube_meta(folder: Path):
    """自動アップロード前にメタファイル（title/description/tags）が無ければ生成。
    タイムスタンプは対応する music_time_code_info_*.txt の LOOP 直前までを概要欄に流し込む。"""
    folder = Path(folder)
    m = re.match(r"^(\d+)_", folder.name)
    vol = m.group(1) if m else "00"

    # title
    title_file = folder / "youtube_title.txt"
    if not title_file.exists() or not title_file.read_text(encoding="utf-8").strip():
        title_text = ""
        meta_json = folder / "video_youtube_title.json"
        if meta_json.exists():
            try:
                d = json.loads(meta_json.read_text(encoding="utf-8"))
                title_text = (d.get("new_title") or d.get("title") or "").strip()
            except Exception:
                pass
        if not title_text:
            title_text = f"orzz. vol.{vol}"
        title_file.write_text(title_text, encoding="utf-8")

    # description（タイムスタンプ含む）
    desc_file = folder / "youtube_description.txt"
    if not desc_file.exists() or not desc_file.read_text(encoding="utf-8").strip():
        timestamps, _tc_file = _read_matching_timecodes_until_loop(folder, vol)
        body = [
            f"orzz. vol.{vol}",
            "",
            "Lounge & jazz BGM for relax / work / study.",
            "",
        ]
        if timestamps:
            body.append("Tracklist")
            body.append(timestamps)
            body.append("")
        body.append("#orzz #BGM #LoungeJazz #ChillMusic")
        desc_file.write_text("\n".join(body), encoding="utf-8")


def _kick_auto_upload(folder: Path, privacy: str = "unlisted", video_path: Optional[str] = None):
    """既存のメタファイルを使って YouTube アップロードを起動（非ブロッキング）。
    video_path が指定された場合、その mp4 を直接使う（外部 SSD 用）。
    folder は title/description/tags/サムネイル等メタ取得元として常に必要。
    トークンはアクティブチャンネルの `.youtube_token.json` を明示指定。
    """
    try:
        cmd, meta = _build_youtube_upload_command(
            UploadRequest(folder=str(folder), privacy=privacy, video_path=video_path)
        )
        result = _enqueue_youtube_upload(cmd, meta, source="auto")
        print(
            f"[auto_upload] queued #{result.get('job', {}).get('id')}: "
            f"folder={folder} video={video_path or '(folder内 mp4)'} privacy={privacy}"
        )
    except Exception as e:
        print(f"[auto_upload] queue failed: {e}")


_export_watcher_task = None
_export_processor_task = None

async def _export_watcher_loop():
    """定期的に channel_folder をスキャン → 完成条件マッチを enqueue"""
    while True:
        rules = get_export_rules()
        if rules.get("watcher_enabled"):
            try:
                for info in _scan_all_for_export():
                    _enqueue_export(info)
            except Exception as e:
                print(f"[export_watcher] scan error: {e}")
        await asyncio.sleep(int(rules.get("scan_interval_sec", 30)))

async def _export_processor_loop():
    """キュー先頭から順次処理。並列実行はしない（AME 1 ジョブずつ）。"""
    while True:
        try:
            q = get_export_queue()
            pending = [x for x in q["items"] if x.get("status") == "pending"]
            if pending:
                await _process_export_item(pending[0])
        except Exception as e:
            print(f"[export_processor] error: {e}")
        await asyncio.sleep(5)

@app.on_event("startup")
async def _start_export_workers():
    global _export_watcher_task, _export_processor_task
    # 起動時に running 残骸を error にリセット（前回プロセスが kill された場合の救済）
    try:
        q = get_export_queue()
        changed = False
        for x in q["items"]:
            if x.get("status") == "running":
                x["status"] = "error"
                x["error"] = "サーバー再起動で中断"
                x["completed_at"] = datetime.utcnow().isoformat() + "Z"
                changed = True
        if changed:
            save_export_queue(q)
    except Exception as e:
        print(f"[startup] running reset failed: {e}")
    if _export_watcher_task is None:
        _export_watcher_task = asyncio.create_task(_export_watcher_loop())
    if _export_processor_task is None:
        _export_processor_task = asyncio.create_task(_export_processor_loop())

# ─── API: 書き出しキュー ───

@app.get("/api/export/queue")
def api_get_export_queue():
    return get_export_queue()


@app.get("/api/ame/queue")
def api_get_ame_queue():
    """AME キューの進捗付き表示。トップバーチップ・自動化タブ詳細から呼ばれる。

    Returns:
      {
        "queue": [{...item, "progress": {size_bytes, rate_bytes_per_sec, elapsed_sec, phase}}],
        "current_index": int|null,    # running 中アイテムの 0-based index
        "total": int,                  # pending+running+done の合計（クリア前）
        "running_count": int,
        "pending_count": int,
        "done_count": int,
        "error_count": int,
        "watcher_enabled": bool
      }
    """
    q = get_export_queue()
    items = q.get("items", []) or []
    enriched = []
    current_index = None
    counts = {"pending": 0, "running": 0, "done": 0, "error": 0}
    for i, it in enumerate(items):
        st = it.get("status", "pending")
        counts[st] = counts.get(st, 0) + 1
        prog = _ame_progress.get(it.get("fingerprint", "")) or {}
        enriched.append({**it, "progress": prog})
        if st == "running" and current_index is None:
            current_index = i
    rules = get_export_rules()
    return {
        "queue": enriched,
        "current_index": current_index,
        "total": len(items),
        "running_count": counts.get("running", 0),
        "pending_count": counts.get("pending", 0),
        "done_count": counts.get("done", 0),
        "error_count": counts.get("error", 0),
        "watcher_enabled": bool(rules.get("watcher_enabled")),
    }

class ExportQueueAddRequest(BaseModel):
    video_name: str

@app.post("/api/export/queue")
def api_add_export_queue(req: ExportQueueAddRequest):
    config = get_dashboard_config()
    folder = Path(config.get("channel_folder", "")) / req.video_name
    if not folder.exists():
        raise HTTPException(404, f"動画フォルダが見つかりません: {req.video_name}")
    info = _scan_video_folder_for_export(folder)
    if not info.get("prproj_path"):
        raise HTTPException(400, "prproj が見つかりません")
    return _enqueue_export(info)

@app.delete("/api/export/queue/{fingerprint}")
def api_delete_export_queue(fingerprint: str, ignore: bool = False):
    """キューから削除。ignore=true なら video_name を ignore list へ追加して
    watcher が再検知しないようにする。"""
    q = get_export_queue()
    target_names = [x.get("video_name", "") for x in q["items"]
                    if x.get("fingerprint") == fingerprint and x.get("status") != "running"]
    before = len(q["items"])
    q["items"] = [x for x in q["items"] if x.get("fingerprint") != fingerprint or x.get("status") == "running"]
    save_export_queue(q)
    if ignore:
        for name in target_names:
            if name:
                add_to_export_ignore_list(name)
    return {"status": "ok", "removed": before - len(q["items"]),
            "ignored": target_names if ignore else []}


@app.post("/api/export/queue/{fingerprint}/cancel")
def api_cancel_export_item(fingerprint: str):
    """個別キャンセル: pending → 削除、running → subprocess を kill して error 化。
    履歴は残るので、必要なら別途 clear-done でまとめて掃除する。"""
    q = get_export_queue()
    target = next((x for x in q["items"] if x.get("fingerprint") == fingerprint), None)
    if target is None:
        raise HTTPException(404, "fingerprint not found")
    status = target.get("status")
    if status == "running":
        killed = _kill_active_proc(fingerprint)
        # subprocess の wait が完了して finally で status 更新するのを待つよりは、
        # ここで即座に error 化しておく（race しても最終的な状態は error のまま）。
        target["status"] = "error"
        target["error"] = "ユーザーがキャンセル"
        target["completed_at"] = datetime.utcnow().isoformat() + "Z"
        save_export_queue(q)
        return {"status": "ok", "action": "killed" if killed else "marked_error",
                "fingerprint": fingerprint}
    if status == "pending":
        # pending は subprocess がまだ無いので削除でよい
        q["items"] = [x for x in q["items"] if x.get("fingerprint") != fingerprint]
        save_export_queue(q)
        return {"status": "ok", "action": "removed", "fingerprint": fingerprint}
    # done / error は単に削除
    q["items"] = [x for x in q["items"] if x.get("fingerprint") != fingerprint]
    save_export_queue(q)
    return {"status": "ok", "action": "removed", "fingerprint": fingerprint}


@app.post("/api/export/queue/stop-all")
def api_stop_all_export_queue():
    """全停止: running を kill、pending を error 化。done/error は触らない。
    履歴は残るので、続けて completed をクリアしたい場合は clear-done を使う。"""
    q = get_export_queue()
    killed = []
    for x in q["items"]:
        st = x.get("status")
        if st == "running":
            _kill_active_proc(x.get("fingerprint", ""))
            x["status"] = "error"
            x["error"] = "ユーザー操作で停止"
            x["completed_at"] = datetime.utcnow().isoformat() + "Z"
            killed.append(x.get("video_name"))
        elif st == "pending":
            x["status"] = "error"
            x["error"] = "ユーザー操作で停止（実行前）"
            x["completed_at"] = datetime.utcnow().isoformat() + "Z"
            killed.append(x.get("video_name"))
    save_export_queue(q)
    return {"status": "ok", "stopped": killed}


@app.post("/api/export/queue/reset")
def api_reset_export_queue():
    """キュー完全リセット: running を kill して items を空にする。履歴も全消去。"""
    q = get_export_queue()
    for x in q["items"]:
        if x.get("status") == "running":
            _kill_active_proc(x.get("fingerprint", ""))
    save_export_queue({"items": []})
    return {"status": "ok", "cleared": len(q["items"])}


@app.post("/api/export/queue/clear-done")
def api_clear_done_export_queue(to_ignore: bool = True):
    """完了/エラー済みアイテムをキューから削除。to_ignore=true（既定）なら
    削除した video_name を ignore list へ追加して再検知を防ぐ。"""
    q = get_export_queue()
    cleared = [x for x in q["items"] if x.get("status") in ("done", "error")]
    q["items"] = [x for x in q["items"] if x.get("status") not in ("done", "error")]
    save_export_queue(q)
    ignored = []
    if to_ignore:
        for x in cleared:
            n = x.get("video_name", "")
            if n:
                add_to_export_ignore_list(n)
                ignored.append(n)
    return {"status": "ok", "removed": len(cleared), "ignored": ignored}


# ── AME 書き出し無視リスト ──
@app.get("/api/export/ignore-list")
def api_get_export_ignore_list():
    return {"items": get_export_ignore_list()}


class ExportIgnoreItem(BaseModel):
    video_name: str


@app.post("/api/export/ignore-list")
def api_add_export_ignore(req: ExportIgnoreItem):
    items = add_to_export_ignore_list(req.video_name.strip())
    # 該当 video_name の pending アイテムもキューから外す
    q = get_export_queue()
    before = len(q["items"])
    q["items"] = [x for x in q["items"]
                  if x.get("video_name") != req.video_name or x.get("status") == "running"]
    if len(q["items"]) != before:
        save_export_queue(q)
    return {"status": "ok", "items": items, "queue_removed": before - len(q["items"])}


@app.delete("/api/export/ignore-list/{video_name}")
def api_remove_export_ignore(video_name: str):
    items = remove_from_export_ignore_list(video_name)
    return {"status": "ok", "items": items}


class ExportIgnoreBulk(BaseModel):
    video_names: List[str]


@app.put("/api/export/ignore-list")
def api_replace_export_ignore(req: ExportIgnoreBulk):
    """無視リストを丸ごと置換（管理 UI 向け）。"""
    cc = load_channel_config()
    cleaned = sorted({n.strip() for n in (req.video_names or []) if n and n.strip()})
    cc["export_ignore_list"] = cleaned
    save_channel_config(cc)
    return {"status": "ok", "items": cleaned}

@app.get("/api/export/rules")
def api_get_export_rules():
    return get_export_rules()

class ExportRulesPatch(BaseModel):
    watcher_enabled: Optional[bool] = None
    scan_interval_sec: Optional[int] = None
    size_stable_window_sec: Optional[int] = None
    auto_upload_after_export: Optional[bool] = None
    auto_upload_privacy: Optional[str] = None
    rules: Optional[dict] = None

@app.put("/api/export/rules")
def api_put_export_rules(patch: ExportRulesPatch):
    cfg = get_export_rules()
    body = patch.dict(exclude_none=True)
    if "rules" in body and isinstance(body["rules"], dict):
        cfg["rules"] = {**cfg.get("rules", {}), **body.pop("rules")}
    cfg.update(body)
    save_export_rules(cfg)
    return {"status": "ok", "config": cfg}

@app.post("/api/export/scan")
def api_export_scan(include_ignored: bool = False):
    """即時スキャン: 完成条件マッチした vol 一覧を返す（enqueue はしない）。
    include_ignored=True にすると無視リスト内のフォルダも含めて返す（UI 管理用）。"""
    return {
        "completable": _scan_all_for_export(include_ignored=include_ignored),
        "ignore_list": get_export_ignore_list(),
    }


# ─── API: 認証（Sprint 5-C） ───

class LoginRequest(BaseModel):
    token: str

@app.post("/api/auth/login")
def api_auth_login(req: LoginRequest):
    """トークンを検証し、Cookie にセット。"""
    expected = _get_or_create_auth_token()
    if not secrets.compare_digest((req.token or "").strip(), expected):
        raise HTTPException(401, "トークンが一致しません")
    resp = JSONResponse({"status": "ok"})
    # Cookie は HTTPS 推奨だが、ローカル運用 + Cloudflare Tunnel で柔軟に
    resp.set_cookie("orzz_token", expected, httponly=True, samesite="lax", max_age=60*60*24*30)
    return resp

@app.get("/api/auth/check")
def api_auth_check(request: Request):
    """認証が必要かどうか + 現在の認証状態を返す（ログイン画面の判断用）"""
    if not AUTH_REQUIRED:
        return {"required": False, "authenticated": True}
    expected = _get_or_create_auth_token()
    received = request.cookies.get("orzz_token", "")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        received = auth[7:].strip()
    return {"required": True, "authenticated": secrets.compare_digest(received, expected)}

@app.post("/api/auth/logout")
def api_auth_logout():
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie("orzz_token")
    return resp

@app.post("/api/auth/regenerate-token")
def api_auth_regenerate():
    """トークンを再生成。既存セッションは全て無効化される。"""
    new = secrets.token_urlsafe(24)
    AUTH_TOKEN_FILE.write_text(new)
    try:
        AUTH_TOKEN_FILE.chmod(0o600)
    except Exception:
        pass
    return {"status": "ok", "token": new}


# ─── スケジュール（APScheduler 統合） ───

_scheduler = None
_scheduler_history = []  # 直近 50 件の実行履歴

def _scheduler_get_jobs():
    return load_json(SCHEDULE_JOBS_FILE, {"jobs": []}).get("jobs", [])

def _scheduler_save_jobs(jobs):
    save_json(SCHEDULE_JOBS_FILE, {"jobs": jobs})

def _record_history(job_id: str, status: str, detail: str = ""):
    _scheduler_history.append({
        "job_id": job_id, "status": status, "detail": detail[:200],
        "at": datetime.utcnow().isoformat() + "Z",
    })
    if len(_scheduler_history) > 50:
        _scheduler_history[:] = _scheduler_history[-50:]

def _send_discord_notify(message: str):
    """Discord 通知を非同期 fire-and-forget で送る。失敗は無視。"""
    try:
        notify_script = SHARED_BASE / "Python" / "app_notify.sh"
        if notify_script.exists():
            subprocess.Popen(["bash", str(notify_script), message],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

def _send_line_notify(message: str):
    """Backward-compatible alias for older scheduler code paths."""
    _send_discord_notify(message)

def _resolve_job_channel(job: dict) -> tuple:
    """job の channel_id をチャンネル名 + フォルダパス + id に解決。

    P1-3: グローバル `dashboard_config.json` を書き換えず、解決結果を子プロセスへ
    env (APP_CHANNEL_ID / APP_CHANNEL_FOLDER / APP_CHANNEL_NAME) で渡す。
    P2-2: id を返り値に含めることで、子プロセスに `--channel-id` を渡せるように。
    channel_id が空なら UI のアクティブチャンネルにフォールバック（id は空）。

    Returns: (channel_name: Optional[str], channel_folder: Optional[Path], channel_id: Optional[str])
    """
    channel_id = job.get("channel_id", "")
    if channel_id:
        chs = get_channels()
        ch = next((c for c in chs if c.get("id") == channel_id), None)
        if ch:
            return ch["name"], Path(ch["folder"]), ch.get("id") or ""
    # フォールバック: UI のアクティブチャンネル
    cfg = get_dashboard_config()
    folder = cfg.get("channel_folder") or ""
    name = cfg.get("channel_name") or ""
    # registry を逆引きして id を補完
    fallback_id = ""
    if folder:
        try:
            target = str(Path(folder).expanduser().resolve())
            for ch in get_channels():
                p = str(Path(ch.get("folder") or "").expanduser().resolve())
                if p == target:
                    fallback_id = ch.get("id") or ""
                    break
        except Exception:
            pass
    return (name or None), (Path(folder) if folder else None), fallback_id


def _build_job_env(ch_name: Optional[str], ch_folder: Optional[Path],
                   ch_id: Optional[str] = None) -> dict:
    """子プロセス用の env。グローバル設定を介さずチャンネルを伝達。"""
    env = {**os.environ}
    if ch_folder:
        env["APP_CHANNEL_FOLDER"] = str(ch_folder)
    if ch_name:
        env["APP_CHANNEL_NAME"] = ch_name
    if ch_id:
        env["APP_CHANNEL_ID"] = ch_id
    # 無人モード（input/sleep ハングを禁止）
    env["APP_NO_INTERACTIVE"] = "1"
    # P2-1: render queue を使う（premiere/export はシリアライズキュー経由）
    env["APP_USE_RENDER_QUEUE"] = "1"
    return env


# ─── P2-7: 公開ゲート — private upload + N 時間後に public 化 ──────
# 完全自走の前提: per-channel `publish_delay_hours` で「アップロード後 N 時間
# は private のまま、その後自動で public 化」を実現する。途中で運営者が
# YouTube Studio で確認して取消したい場合は手動で削除できる猶予時間。

async def _job_publish_now(payload: dict):
    """指定 vol を private → public に切り替える非同期ハンドラ。

    payload: {
      "id": "publish_<channel_id>_<vol>",
      "video_name": "78_vol_260420",
      "channel_folder": "/path/to/channel",
      "channel_name": "...",
    }
    """
    job_id = payload.get("id", "?")
    video_name = payload.get("video_name", "")
    ch_folder = payload.get("channel_folder", "")
    ch_name = payload.get("channel_name") or "(unknown)"
    folder = Path(ch_folder) / video_name if (ch_folder and video_name) else None
    if not folder or not folder.exists():
        _record_history(job_id, "error", f"publish: folder 不在 {folder}")
        _send_line_notify(f"❌ 公開: [{ch_name}] {video_name} - フォルダ不在")
        return
    _record_history(job_id, "started", f"[{ch_name}] {video_name} を public に切替")
    try:
        # subprocess ではなくモジュール直接呼び出し（軽量）
        sys.path.insert(0, str(SHARED_BASE / "Python"))
        from app_youtube import publish_video_to_public
        result = publish_video_to_public(folder)
    except Exception as e:
        _record_history(job_id, "error", f"publish 例外: {e}")
        _send_line_notify(f"❌ 公開: [{ch_name}] {video_name} 例外: {str(e)[:80]}")
        return
    status = result.get("status")
    vid = result.get("video_id", "")
    if status == "ok":
        url = f"https://youtu.be/{vid}"
        _record_history(job_id, "done", f"[{ch_name}] {video_name} → public ({vid})")
        _send_line_notify(f"🎉 [{ch_name}] {video_name} を public に公開しました\n{url}")
    elif status == "already_public":
        _record_history(job_id, "done", f"[{ch_name}] {video_name} は既に public")
    elif status == "retryable":
        # 30 分後に再投入
        _record_history(job_id, "error", f"transient: {result.get('error','')}")
        _send_line_notify(f"⚠ 公開: [{ch_name}] {video_name} transient エラー、30 分後に再試行")
        try:
            from apscheduler.triggers.date import DateTrigger
            import datetime as _dt
            run_at = _dt.datetime.now() + _dt.timedelta(minutes=30)
            new_id = f"{job_id}_retry_{int(time.time())}"
            payload2 = {**payload, "id": new_id}
            _scheduler.add_job(_job_publish_now, trigger=DateTrigger(run_date=run_at),
                               id=new_id, args=[payload2],
                               replace_existing=True, misfire_grace_time=300)
        except Exception as e:
            print(f"[publish] retry 登録失敗: {e}")
    else:
        _record_history(job_id, "error", f"{status}: {result.get('error','')[:200]}")
        _send_line_notify(f"❌ 公開: [{ch_name}] {video_name} 失敗 ({status})")


def _schedule_publish(*, video_name: str, channel_folder: str, channel_name: str,
                       run_at) -> str:
    """指定時刻に publish ジョブを APScheduler の DateTrigger で登録。"""
    if _scheduler is None:
        raise RuntimeError("scheduler が起動していません")
    from apscheduler.triggers.date import DateTrigger
    # チャンネル + vol 単位で id を作る（同じ vol を二重登録しないため replace_existing で上書き）
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", f"{channel_folder}_{video_name}")[-80:]
    job_id = f"publish_{safe}"
    payload = {
        "id": job_id,
        "video_name": video_name,
        "channel_folder": channel_folder,
        "channel_name": channel_name,
    }
    _scheduler.add_job(
        _job_publish_now, trigger=DateTrigger(run_date=run_at),
        id=job_id, args=[payload],
        replace_existing=True, misfire_grace_time=600,
    )
    return job_id


# ─── P2-6: auto_resume — 失敗 stage 検出 + N 分後に再投入 ────
# pipeline は失敗時に標準出力末尾に「再開: python3 app_pipeline.py <vol> --from <stage>」を
# 必ず出すので、それから stage を抽出する。

# 手動介入が必要で resume しても無意味な exit code（P1-1/P1-4/P1-5 の sentinel）
_NO_AUTO_RESUME_CODES = {75, 77, 78}


def _parse_failed_stage(stdout: str) -> str:
    """pipeline の '再開: python3 app_pipeline.py <vol> --from <stage>' から stage を抽出。"""
    if not stdout:
        return ""
    m = re.search(r"--from\s+(\w+)", stdout)
    return m.group(1) if m else ""


def _should_auto_resume(returncode: int, stage: str, attempt: int, job: dict) -> tuple:
    """auto_resume 可否。Returns (ok: bool, reason_if_not: str)."""
    if not job.get("auto_resume"):
        return False, "auto_resume が無効"
    if returncode == 0:
        return False, "成功なので resume 不要"
    if returncode in _NO_AUTO_RESUME_CODES:
        return False, f"exit code {returncode} は手動介入が必要"
    if not stage:
        return False, "失敗 stage を特定できない"
    max_attempts = int(job.get("auto_resume_max_attempts") or 3)
    if attempt >= max_attempts:
        return False, f"再投入上限到達 ({attempt}/{max_attempts})"
    return True, ""


async def _job_auto_resume(payload: dict):
    """auto_resume 専用ハンドラ。N 分後の DateTrigger で起動される。

    payload は schedule_jobs.json には保存されない（一時的なメモリ内 APScheduler ジョブ）。
    """
    job_id = payload.get("id", "?")
    vol = payload.get("vol")
    if not vol:
        _record_history(job_id, "error", "resume: vol 未指定")
        return
    ch_name, ch_dir, ch_id = _resolve_job_channel(payload)
    if not ch_dir or not ch_dir.exists():
        _record_history(job_id, "error", f"resume: channel_folder 不在: {ch_dir}")
        _send_line_notify(f"❌ auto_resume: vol.{vol} - チャンネル不在")
        return
    from_stage = (payload.get("from_stage") or "").strip() or "suno"
    attempt = int(payload.get("attempt", 1))
    # P3-1: ledger に auto_resume として記録（parent を持つ）
    run_id = ""
    parent_rid = payload.get("_parent_run_id") or payload.get("parent_run_id") or ""
    try:
        import app_run_ledger as _ledger
        run_id = _ledger.start_run(
            kind="auto_resume", channel_folder=str(ch_dir),
            channel_id=ch_id or "", channel_name=ch_name or "",
            vol=int(vol), parent_run_id=parent_rid,
            from_stage=from_stage,
            meta={"attempt": attempt},
        )
    except Exception as e:
        print(f"[ledger] start_run 失敗（無視）: {e}")
    _record_history(job_id, "started",
                    f"[{ch_name}] resume vol.{vol} from {from_stage} (attempt {attempt}, run={run_id[-12:] if run_id else '?'})")
    _send_line_notify(
        f"🔄 [{ch_name}] vol.{vol} を {from_stage} から自動再投入中（{attempt} 回目）"
    )
    cmd = [sys.executable, str(SHARED_BASE / "Python" / "app_pipeline.py"),
           str(vol), "--auto", "--from", from_stage]
    if ch_id:
        cmd += ["--channel-id", ch_id]
    else:
        cmd += ["--channel-folder", str(ch_dir)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=_build_job_env(ch_name, ch_dir, ch_id),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        stdout_str = (out or b"").decode(errors="replace")
        if proc.returncode == 0:
            _record_history(job_id, "done", f"[{ch_name}] resume vol.{vol} 完了")
            _send_line_notify(f"✅ [{ch_name}] vol.{vol} 再投入で完了")
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="done", exit_code=0,
                                       summary=f"resume vol.{vol} 完了 (attempt {attempt})")
                except Exception:
                    pass
            return
        # 再失敗 → さらに resume できるか
        next_stage = _parse_failed_stage(stdout_str) or from_stage
        ok, reason = _should_auto_resume(proc.returncode, next_stage, attempt, payload)
        if run_id:
            try:
                import app_run_ledger as _ledger
                _ledger.finish_run(run_id, status="failed",
                                   exit_code=proc.returncode,
                                   failed_stage=next_stage,
                                   summary=f"resume vol.{vol} 失敗 (attempt {attempt}, next={next_stage})")
            except Exception:
                pass
        if ok:
            payload["_parent_run_id"] = run_id
            _schedule_resume(payload, next_stage, attempt + 1)
            _send_line_notify(
                f"❌→🔄 [{ch_name}] vol.{vol} 再失敗、{int(payload.get('auto_resume_delay_min') or 30)} 分後に再々投入をスケジュール（{attempt + 1} 回目）"
            )
            _record_history(job_id, "error",
                            f"[{ch_name}] resume vol.{vol} 失敗、再々投入予約: {next_stage}")
        else:
            tail = stdout_str[-200:]
            _record_history(job_id, "error", f"resume 打ち切り ({reason}): {tail}")
            _send_line_notify(f"⛔ [{ch_name}] vol.{vol} 再投入打ち切り: {reason}")
    except Exception as e:
        _record_history(job_id, "error", f"resume 例外: {e}")
        if run_id:
            try:
                import app_run_ledger as _ledger
                _ledger.finish_run(run_id, status="failed", summary=f"例外: {str(e)[:100]}")
            except Exception:
                pass


def _schedule_resume(parent_payload: dict, from_stage: str, attempt: int) -> bool:
    """N 分後の DateTrigger で resume ジョブを APScheduler に登録（永続化なし）。"""
    if _scheduler is None:
        print("[scheduler] resume 失敗: scheduler が起動していない")
        return False
    delay_min = int(parent_payload.get("auto_resume_delay_min") or 30)
    import datetime as _dt
    run_at = _dt.datetime.now() + _dt.timedelta(minutes=delay_min)
    parent_id = parent_payload.get("id", "?")
    payload = {
        # 元のジョブ id を含めて新 id を組み立てる
        "id": f"resume_{parent_id}_{attempt}",
        "vol": parent_payload.get("vol") or parent_payload.get("_resolved_vol"),
        "channel_id": parent_payload.get("channel_id"),
        "from_stage": from_stage,
        "attempt": attempt,
        "auto_resume": True,
        "auto_resume_delay_min": delay_min,
        "auto_resume_max_attempts": int(parent_payload.get("auto_resume_max_attempts") or 3),
        "name": f"auto_resume({parent_id})",
    }
    try:
        from apscheduler.triggers.date import DateTrigger
        _scheduler.add_job(
            _job_auto_resume, trigger=DateTrigger(run_date=run_at),
            id=payload["id"], args=[payload],
            replace_existing=True, misfire_grace_time=300,
        )
        print(f"[scheduler] resume スケジュール: {payload['id']} at {run_at.isoformat()} (from={from_stage}, attempt={attempt})")
        return True
    except Exception as e:
        print(f"[scheduler] resume 登録失敗: {e}")
        return False


async def _job_vol_create(job: dict):
    """次の vol を fully-auto で作る。channel_id 指定可。

    P1-3: dashboard_config.json は読み取り専用扱い。channel_folder を直接 subprocess に渡す。
    P3-1: 実行を中央 ledger (`runs.db`) に記録。
    """
    job_id = job.get("id", "?")
    ch_name, ch_dir, ch_id = _resolve_job_channel(job)
    if not ch_dir or not ch_dir.exists():
        _record_history(job_id, "error", f"channel_folder が存在しない: {ch_dir}")
        _send_line_notify(f"❌ Automation Studio 自動: vol 作成失敗 ({job_id}) - チャンネル不在")
        return
    # 次の vol 番号を解決（グローバル config 経由ではなく、解決済み folder を直接スキャン）
    max_n = 0
    for d in ch_dir.iterdir():
        m = re.match(r"^(\d+)_", d.name)
        if m: max_n = max(max_n, int(m.group(1)))
    next_vol = max_n + 1
    # P3-1: ledger に in_progress で記録
    run_id = ""
    try:
        import app_run_ledger as _ledger
        run_id = _ledger.start_run(
            kind="vol_create", channel_folder=str(ch_dir),
            channel_id=ch_id or "", channel_name=ch_name or "",
            vol=next_vol, parent_job_id=job_id,
        )
    except Exception as e:
        print(f"[ledger] start_run 失敗（無視）: {e}")
    _record_history(job_id, "started", f"[{ch_name}] vol.{next_vol} 作成開始 (run={run_id[-12:] if run_id else '?'})")
    _send_line_notify(f"🚀 [{ch_name}] vol.{next_vol} の作成を開始 ({job.get('name', job_id)})")
    # app_pipeline.py を起動（registry 由来の id があれば --channel-id 優先）
    cmd = [sys.executable, str(SHARED_BASE / "Python" / "app_pipeline.py"),
           str(next_vol), "--from-benchmark", "--auto"]
    if ch_id:
        cmd += ["--channel-id", ch_id]
    else:
        cmd += ["--channel-folder", str(ch_dir)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=_build_job_env(ch_name, ch_dir, ch_id),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        stdout_str = (out or b"").decode(errors="replace")
        if proc.returncode == 0:
            _record_history(job_id, "done", f"[{ch_name}] vol.{next_vol} 完了")
            _send_line_notify(f"✅ [{ch_name}] vol.{next_vol} 完了")
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="done", exit_code=0,
                                       summary=f"vol.{next_vol} 完了")
                except Exception as e:
                    print(f"[ledger] finish_run 失敗: {e}")
        else:
            tail = stdout_str[-500:]
            _record_history(job_id, "error", tail)
            # P2-6: auto_resume が有効なら失敗 stage を解析して N 分後に再投入
            failed_stage = _parse_failed_stage(stdout_str)
            resume_payload = {
                **job,
                "vol": next_vol,  # 動的に解決した vol を保持
                "_resolved_vol": next_vol,
                "_parent_run_id": run_id,
            }
            ok, reason = _should_auto_resume(proc.returncode, failed_stage, 1, resume_payload)
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="failed",
                                       exit_code=proc.returncode,
                                       failed_stage=failed_stage,
                                       summary=f"vol.{next_vol} 失敗 ({failed_stage or 'unknown'})")
                except Exception as e:
                    print(f"[ledger] finish_run 失敗: {e}")
            if ok:
                _schedule_resume(resume_payload, failed_stage, attempt=2)
                _send_line_notify(
                    f"❌→🔄 [{ch_name}] vol.{next_vol} 失敗（{failed_stage}）、"
                    f"{int(job.get('auto_resume_delay_min') or 30)} 分後に自動再投入"
                )
            else:
                _send_line_notify(
                    f"❌ [{ch_name}] vol.{next_vol} 失敗 (exit={proc.returncode}) — auto_resume なし: {reason}"
                )
    except Exception as e:
        _record_history(job_id, "error", str(e)[:200])
        _send_line_notify(f"❌ [{ch_name}] vol.{next_vol} 例外: {str(e)[:80]}")
        if run_id:
            try:
                import app_run_ledger as _ledger
                _ledger.finish_run(run_id, status="failed",
                                   summary=f"例外: {str(e)[:100]}")
            except Exception:
                pass

async def _job_benchmark_refresh(job: dict):
    job_id = job.get("id", "?")
    _record_history(job_id, "started", "benchmark refresh")
    try:
        # /api/analysis/competitors を内部呼び出し
        await api_analysis_competitors()
        _record_history(job_id, "done", "")
    except Exception as e:
        _record_history(job_id, "error", str(e)[:200])

async def _job_export_window(job: dict):
    """on/off の time_of_day 指定で watcher を ON/OFF 切替"""
    job_id = job.get("id", "?")
    action = (job.get("action") or "").lower()
    cfg = get_export_rules()
    if action == "on":
        cfg["watcher_enabled"] = True
    elif action == "off":
        cfg["watcher_enabled"] = False
    else:
        _record_history(job_id, "error", f"未知の action: {action}")
        return
    save_export_rules(cfg)
    _record_history(job_id, "done", f"watcher {action}")

async def _job_spot_create(job: dict):
    """指定 vol を fully-auto で作る（複数チャンネル対応）。

    P1-3: dashboard_config.json は読み取り専用扱い。同時刻に複数チャンネルのジョブが
    走ってもグローバル状態が衝突しない。
    """
    job_id = job.get("id", "?")
    vol = job.get("vol")
    if not vol:
        _record_history(job_id, "error", "vol 未指定")
        return
    ch_name, ch_dir, ch_id = _resolve_job_channel(job)
    if not ch_dir or not ch_dir.exists():
        _record_history(job_id, "error", f"channel_folder が存在しない: {ch_dir}")
        _send_line_notify(f"❌ Automation Studio スポット: vol.{vol} 失敗 - チャンネル不在")
        return
    # P3-1: ledger 開始
    run_id = ""
    try:
        import app_run_ledger as _ledger
        run_id = _ledger.start_run(
            kind="spot_create", channel_folder=str(ch_dir),
            channel_id=ch_id or "", channel_name=ch_name or "",
            vol=int(vol), parent_job_id=job_id,
        )
    except Exception as e:
        print(f"[ledger] start_run 失敗（無視）: {e}")
    _record_history(job_id, "started", f"[{ch_name}] spot vol.{vol} (run={run_id[-12:] if run_id else '?'})")
    _send_line_notify(f"🚀 [{ch_name}] スポット vol.{vol} 開始")
    cmd = [sys.executable, str(SHARED_BASE / "Python" / "app_pipeline.py"),
           str(vol), "--from-benchmark", "--auto"]
    if ch_id:
        cmd += ["--channel-id", ch_id]
    else:
        cmd += ["--channel-folder", str(ch_dir)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            env=_build_job_env(ch_name, ch_dir, ch_id),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate()
        stdout_str = (out or b"").decode(errors="replace")
        ok = proc.returncode == 0
        if ok:
            _record_history(job_id, "done", f"[{ch_name}] vol.{vol} 完了")
            _send_line_notify(f"✅ [{ch_name}] スポット vol.{vol}")
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="done", exit_code=0,
                                       summary=f"spot vol.{vol} 完了")
                except Exception as e:
                    print(f"[ledger] finish_run 失敗: {e}")
        else:
            tail = stdout_str[-300:]
            _record_history(job_id, "error", tail)
            # P2-6: auto_resume
            failed_stage = _parse_failed_stage(stdout_str)
            resume_payload = {**job, "_resolved_vol": vol, "_parent_run_id": run_id}
            should_resume, reason = _should_auto_resume(proc.returncode, failed_stage, 1, resume_payload)
            if run_id:
                try:
                    import app_run_ledger as _ledger
                    _ledger.finish_run(run_id, status="failed",
                                       exit_code=proc.returncode,
                                       failed_stage=failed_stage,
                                       summary=f"spot vol.{vol} 失敗 ({failed_stage or 'unknown'})")
                except Exception as e:
                    print(f"[ledger] finish_run 失敗: {e}")
            if should_resume:
                _schedule_resume(resume_payload, failed_stage, attempt=2)
                _send_line_notify(
                    f"❌→🔄 [{ch_name}] スポット vol.{vol} 失敗（{failed_stage}）、"
                    f"{int(job.get('auto_resume_delay_min') or 30)} 分後に自動再投入"
                )
            else:
                _send_line_notify(
                    f"❌ [{ch_name}] スポット vol.{vol} 失敗 (exit={proc.returncode}) — auto_resume なし: {reason}"
                )
    except Exception as e:
        _record_history(job_id, "error", str(e)[:200])
        if run_id:
            try:
                import app_run_ledger as _ledger
                _ledger.finish_run(run_id, status="failed",
                                   summary=f"例外: {str(e)[:100]}")
            except Exception:
                pass

async def _job_token_health(job: dict):
    """P3-5: 全チャンネルのトークン健全性を点検し、warn 以上を Discord 通知。

    既定は日次 cron で運用想定。副作用は通知のみ（自動再認証はしない）。"""
    job_id = job.get("id", "?")
    try:
        sys.path.insert(0, str(SHARED_BASE / "Python"))
        from app_token_health import check_all
        result = check_all()
    except Exception as e:
        _record_history(job_id, "error", f"token_health 例外: {e}")
        return
    warnings = result.get("warnings", [])
    if not warnings:
        _record_history(job_id, "done", "全トークン正常")
        return
    # warn を Discord で 1 通にまとめて通知
    lines = ["⚠ トークン健全性チェック - 要対応:"]
    for w in warnings[:10]:
        lines.append(f"  • [{w.get('channel_name','?')}] {w.get('kind','?')}: {w.get('status','?')} - {w.get('message','')[:80]}")
    if len(warnings) > 10:
        lines.append(f"  ... 他 {len(warnings) - 10} 件")
    lines.append("\n対応: docs/runbook.md の「unattended-login」セクション参照")
    _send_line_notify("\n".join(lines))
    _record_history(job_id, "error" if any(w.get("status") in ("expired", "missing") for w in warnings) else "done",
                    f"{len(warnings)} 件の warn")


JOB_HANDLERS = {
    "vol_create": _job_vol_create,
    "benchmark_refresh": _job_benchmark_refresh,
    "export_window": _job_export_window,
    "spot_create": _job_spot_create,
    "publish_now": _job_publish_now,  # P2-7: 公開ゲート（DateTrigger 動的登録）
    "token_health": _job_token_health,  # P3-5: トークン期限の先回り通知
}

# ─── P2-4: 時間帯スロット配分（slot balancing） ─────
# 6+ チャンネルで同時刻に vol_create/spot_create が発火すると、render queue
# は serialized でも上流（SUNO / Claude CLI）が同時並列で過負荷になる。
# 30 分刻みで slot をずらすことで、自然と階段状の起動になり負荷を平準化する。

SLOT_GRANULARITY_MIN = 30  # 30 分単位
SLOT_BALANCED_TYPES = {"vol_create", "spot_create"}  # チャンネル重量級ジョブのみ対象
SLOT_MAX_SHIFTS = 48  # 30 分 × 48 = 24 時間分まで自動調整


# P3-4: render queue 実測から最低 gap を推定（fallback は SLOT_GRANULARITY_MIN）
def _estimate_required_gap_minutes() -> int:
    """直近 7 日の `premiere + export` 平均所要から、必要な slot gap を計算。

    Premiere は 1 セッション固定なので、上流で密集すると後段が詰まる → 平均サイクルに
    合わせて gap を動的に広げる。"""
    try:
        import app_render_queue as _rq
        s = _rq.stats(window_days=7)
        avg_p = int((s.get("by_stage_avg_sec") or {}).get("premiere") or 0)
        avg_e = int((s.get("by_stage_avg_sec") or {}).get("export") or 0)
        cycle_sec = avg_p + avg_e
        if cycle_sec > 0:
            cycle_min = (cycle_sec + 59) // 60  # 切り上げ分
            # SLOT_GRANULARITY_MIN の倍数に切り上げる
            mult = (cycle_min + SLOT_GRANULARITY_MIN - 1) // SLOT_GRANULARITY_MIN
            return max(SLOT_GRANULARITY_MIN, mult * SLOT_GRANULARITY_MIN)
    except Exception:
        pass
    return SLOT_GRANULARITY_MIN


def _channel_quota_pressure(channel_id: str) -> float:
    """channel が直近 24h で quota をどれだけ使っているか（0.0〜1.0+）。

    register 対象が date trigger の場合のみ意味がある（cron は仕様上 24h ウィンドウに
    bind しない）。"""
    if not channel_id:
        return 0.0
    try:
        chs = get_channels()
        ch = next((c for c in chs if c.get("id") == channel_id), None)
        if not ch:
            return 0.0
        cf = Path(ch.get("folder") or "")
        if not cf.exists():
            return 0.0
        import app_youtube as _yt
        used = _yt.quota_used_in_window(cf)
        cap = _yt.DEFAULT_DAILY_QUOTA_CAP
        if cap <= 0:
            return 0.0
        return used / cap
    except Exception:
        return 0.0


def _trigger_slot_key(trigger: dict) -> Optional[tuple]:
    """同一スロット判定用のキー。比較対象外（cron expr / 不正）は None。"""
    if not isinstance(trigger, dict):
        return None
    kind = trigger.get("kind", "cron")
    if kind == "cron" and not trigger.get("expr"):
        # day_of_week + hour + minute を正規化
        dow = str(trigger.get("day_of_week", "*"))
        hour = trigger.get("hour", "*")
        minute = trigger.get("minute", 0)
        try:
            hour = int(hour) if hour != "*" else hour
            minute = int(minute)
        except Exception:
            return None
        return ("cron", dow, hour, minute)
    if kind == "date":
        rd = trigger.get("run_date") or ""
        return ("date", rd[:16])  # 分単位で比較（秒以下は無視）
    return None


def _shift_trigger(trigger: dict, minutes: int) -> dict:
    """trigger を minutes 分後ろにずらした dict を返す（破壊的でない）。"""
    out = dict(trigger)
    kind = out.get("kind", "cron")
    if kind == "cron" and not out.get("expr"):
        try:
            hour = int(out.get("hour", 0))
            minute = int(out.get("minute", 0))
        except Exception:
            return out
        total = hour * 60 + minute + minutes
        new_hour = (total // 60) % 24
        new_min = total % 60
        out["hour"] = new_hour
        out["minute"] = new_min
        return out
    if kind == "date":
        try:
            rd = datetime.fromisoformat(out.get("run_date") or "")
            rd2 = rd + datetime_timedelta(minutes=minutes)
            out["run_date"] = rd2.isoformat()
        except Exception:
            pass
    return out


# datetime.timedelta を fromisoformat と区別する別名
from datetime import timedelta as datetime_timedelta


def _balance_trigger_slot(job: dict, all_jobs: list) -> tuple:
    """衝突する slot を回避するため trigger を後ろにずらす。

    P2-4: シンプルな 30 分単位の slot 探索。
    P3-4 で policy-aware 化:
      - render queue 実測 throughput から「必要 gap」を動的に推定（fallback 30 分）
      - 同チャンネルジョブが直近に占めているなら、必要 gap 以上空ける
      - quota pressure が 70% 超のチャンネルは、当面 1 日後送り（date trigger のみ）
      - 全埋まりの場合は元のまま返す（運営者の判断を待つ）

    Returns: (final_trigger: dict, shift_minutes: int, shift_count: int)
    """
    if job.get("type") not in SLOT_BALANCED_TYPES:
        return job.get("trigger") or {}, 0, 0
    base_trigger = job.get("trigger") or {}
    base_key = _trigger_slot_key(base_trigger)
    if base_key is None:
        return base_trigger, 0, 0

    # 既存ジョブの占有スロット（自分自身は除く）+ チャンネル別の占有時刻
    own_id = job.get("id")
    own_channel = job.get("channel_id") or ""
    occupied: set = set()  # 全ジョブの slot key
    same_channel_keys: list = []  # 同チャンネルジョブの slot key（gap チェック用）
    for j in all_jobs:
        if j.get("id") == own_id:
            continue
        if j.get("type") not in SLOT_BALANCED_TYPES:
            continue
        if not j.get("enabled", True):
            continue
        k = _trigger_slot_key(j.get("trigger") or {})
        if k is None:
            continue
        occupied.add(k)
        if (j.get("channel_id") or "") == own_channel and own_channel:
            same_channel_keys.append(k)

    required_gap = _estimate_required_gap_minutes()

    # P3-4: quota pressure による初期 shift
    quota_initial_shift = 0
    if base_trigger.get("kind") == "date":
        pressure = _channel_quota_pressure(own_channel)
        if pressure > 0.70:
            # 70% 超 → 24h 後送り
            quota_initial_shift = 60 * 24
            print(f"[scheduler] {own_channel} quota pressure {pressure:.0%} → +24h 自動シフト")

    def _gap_ok(shifted_key: tuple) -> bool:
        """同チャンネルの直近占有との間隔が required_gap 以上か（cron only の概算）。"""
        if shifted_key[0] != "cron" or not same_channel_keys:
            return True
        # (kind, dow, hour, minute) → 当日内分単位の比較
        try:
            _, dow, hour, minute = shifted_key
            if not isinstance(hour, int) or not isinstance(minute, int):
                return True
            target_min = hour * 60 + minute
            for k in same_channel_keys:
                if k[0] != "cron":
                    continue
                _, kdow, khour, kminute = k
                if kdow != dow or not isinstance(khour, int) or not isinstance(kminute, int):
                    continue
                kmin = khour * 60 + kminute
                if abs(target_min - kmin) < required_gap:
                    return False
        except Exception:
            return True
        return True

    # quota_initial_shift から開始して空きスロットを探す
    for n in range(quota_initial_shift // SLOT_GRANULARITY_MIN, SLOT_MAX_SHIFTS + 1):
        shift = n * SLOT_GRANULARITY_MIN
        candidate = _shift_trigger(base_trigger, shift) if shift > 0 else base_trigger
        k = _trigger_slot_key(candidate)
        if k is None:
            return base_trigger, 0, 0
        if k in occupied:
            continue
        if not _gap_ok(k):
            continue
        return candidate, shift, n

    # 上限到達 → 元のまま
    return base_trigger, 0, 0


def _make_trigger(job: dict):
    """job の trigger 定義から APScheduler トリガーを生成"""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger
    t = job.get("trigger") or {}
    kind = t.get("kind", "cron")
    if kind == "cron":
        # cron 式 or {day_of_week, hour, minute}
        if t.get("expr"):
            return CronTrigger.from_crontab(t["expr"])
        return CronTrigger(
            day_of_week=t.get("day_of_week", "*"),
            hour=t.get("hour", "*"),
            minute=t.get("minute", 0),
        )
    if kind == "date":
        return DateTrigger(run_date=datetime.fromisoformat(t["run_date"]))
    raise ValueError(f"未知の trigger kind: {kind}")

def _scheduler_register_job(job: dict):
    handler = JOB_HANDLERS.get(job.get("type"))
    if not handler:
        return False
    try:
        trigger = _make_trigger(job)
    except Exception as e:
        print(f"[scheduler] trigger 生成失敗 {job.get('id')}: {e}")
        return False
    _scheduler.add_job(handler, trigger=trigger, id=job["id"], args=[job],
                       replace_existing=True, misfire_grace_time=300)
    return True

def _scheduler_reload():
    """jobs.json を読み直して APScheduler に登録し直す"""
    if _scheduler is None:
        return
    # 既存ジョブ全削除
    for j in _scheduler.get_jobs():
        try: _scheduler.remove_job(j.id)
        except Exception: pass
    # 再登録
    for job in _scheduler_get_jobs():
        if not job.get("enabled", True):
            continue
        _scheduler_register_job(job)

@app.on_event("startup")
async def _start_scheduler():
    global _scheduler
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        _scheduler = AsyncIOScheduler(timezone="Asia/Tokyo")
        _scheduler.start()
        _scheduler_reload()
        print(f"[scheduler] started ({len(_scheduler.get_jobs())} jobs registered)")
    except ImportError:
        print("[scheduler] apscheduler 未インストール。pip install apscheduler が必要")
    except Exception as e:
        print(f"[scheduler] 起動失敗: {e}")


# ─── Render queue worker（P2-1） ────────────────────
# Premiere / Media Encoder は Mac 1 台に 1 セッションしか動かない物理制約があるため、
# 6+ チャンネル並列ジョブは render queue (SQLite) に enqueue → 1 worker が直列処理。
# 他 stage（plan/suno/rename/meta/upload）は queue を経由せず subprocess で並列実行。

_rq_worker_started = False


def _execute_render_job(job: dict) -> None:
    """1 ジョブを subprocess で実行。終了 → mark_done / mark_error。"""
    import app_render_queue as _rq
    job_id = int(job["id"])
    stage = str(job["stage"])
    ch_folder = Path(job["channel_folder"])
    video_name = job.get("video_name") or ""
    folder = ch_folder / video_name if video_name else ch_folder
    if not folder.exists():
        _rq.mark_error(job_id, f"video folder not found: {folder}")
        _send_line_notify(
            f"❌ render queue: [{job.get('channel_name','?')}] vol.{job['vol']} - フォルダ不在"
        )
        return

    cmd_base = [sys.executable, "-u", str(SHARED_BASE / "Python" / "app_premiere.py")]
    prproj = next(iter(folder.glob("*vol*.prproj")), None) or next(iter(folder.glob("*.prproj")), None)
    if stage == "premiere":
        cmd = cmd_base + ["--duration", "10800"]
        if prproj:
            cmd += ["--project", str(prproj)]
        timeout = 3600
    elif stage == "export":
        cmd = cmd_base + ["--export-only"]
        if prproj:
            cmd += ["--project", str(prproj)]
        timeout = 7200
    else:
        _rq.mark_error(job_id, f"unknown stage: {stage}")
        return

    env = {**os.environ}
    env["APP_CHANNEL_FOLDER"] = str(ch_folder)
    env["APP_CHANNEL_NAME"] = job.get("channel_name") or ""
    env["APP_NO_INTERACTIVE"] = "1"
    # worker から起動したサブプロセスが再帰的に enqueue しないよう抑止
    env["APP_USE_RENDER_QUEUE"] = "0"

    print(f"[render-queue] ▶ job#{job_id} {stage} vol.{job['vol']} "
          f"[{job.get('channel_name','?')}]", flush=True)
    try:
        proc = subprocess.run(cmd, env=env, timeout=timeout,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if proc.returncode == 0:
            _rq.mark_done(job_id)
            print(f"[render-queue] ✅ job#{job_id} done", flush=True)
        else:
            tail = (proc.stdout or b"").decode(errors="replace")[-300:]
            _rq.mark_error(job_id, f"exit={proc.returncode} {tail}")
            _send_line_notify(
                f"❌ render queue: [{job.get('channel_name','?')}] "
                f"vol.{job['vol']} {stage} 失敗 (exit={proc.returncode})"
            )
    except subprocess.TimeoutExpired:
        _rq.mark_error(job_id, f"timeout {timeout}s")
        _send_line_notify(
            f"⏰ render queue: [{job.get('channel_name','?')}] "
            f"vol.{job['vol']} {stage} タイムアウト ({timeout}s)"
        )
    except Exception as e:
        _rq.mark_error(job_id, f"exception: {e}")
        _send_line_notify(
            f"❌ render queue: [{job.get('channel_name','?')}] "
            f"vol.{job['vol']} {stage} 例外: {str(e)[:80]}"
        )


def _render_queue_worker_loop() -> None:
    """daemon thread。pending を 1 件ずつ pick → 実行 → mark を繰り返す。"""
    import app_render_queue as _rq
    print("[render-queue] worker started", flush=True)
    while True:
        try:
            job = _rq.claim_next()
            if not job:
                time.sleep(3)
                continue
            _execute_render_job(job)
        except Exception as e:
            print(f"[render-queue] worker exception: {e}", flush=True)
            time.sleep(5)


@app.on_event("startup")
async def _validate_channel_registry():
    """P2-2: dashboard_config.channel_folder が channels.json に登録されているか検証。

    canonical source は channels.json。dashboard_config の channel_folder/channel_name は
    あくまで「UI のアクティブビュー」のポインタなので、registry に無い folder を
    指していたら警告（自動修正はしない、運営者の判断を待つ）。"""
    try:
        # PC 間共有: 共有ドライブ上の registry を旧ローカル版＋YT フォルダスキャンで同期・自己修復
        try:
            sync_channel_registry(verbose=True)
        except Exception as e:
            print(f"[registry] sync 失敗（続行）: {e}")
        # PC 間共有: ベンチマークのプロファイル/設定/分析を共有ドライブへ移行（thumbs 画像は除く）
        try:
            migrate_benchmark_to_shared(verbose=True)
        except Exception as e:
            print(f"[benchmark] 移行呼び出し失敗（続行）: {e}")
        chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
        if not chs:
            print("[registry] channels.json が空。基本設定 > チャンネル管理から追加してください。")
            return
        cfg = load_json(DASHBOARD_CONFIG, {}) or {}
        active_folder = cfg.get("channel_folder") or ""
        active_name = cfg.get("channel_name") or ""
        if not active_folder:
            print(f"[registry] dashboard_config に channel_folder 未設定（{len(chs)} 件登録あり）")
            return
        # registry のいずれかと一致するか
        try:
            target = str(Path(active_folder).expanduser().resolve())
        except Exception:
            target = active_folder
        match = None
        for ch in chs:
            try:
                p = str(Path(ch.get("folder") or "").expanduser().resolve())
                if p == target:
                    match = ch
                    break
            except Exception:
                continue
        if match:
            print(f"[registry] active channel: {match.get('name')!r} (id={match.get('id')!r}) ✓")
        else:
            print(f"[registry] ⚠ dashboard_config.channel_folder が registry に未登録: {active_folder}")
            print(f"[registry] ⚠ active_name={active_name!r} → 'UI 内表示は許可するが job 実行時は --channel-id 推奨'")
    except Exception as e:
        print(f"[registry] 検証失敗: {e}")


@app.on_event("startup")
async def _init_run_ledger():
    """P3-1: run ledger の DB 初期化 + stale 回収。"""
    try:
        import app_run_ledger as _ledger
        _ledger.init_db()
        reaped = _ledger.reap_stale()
        if reaped:
            print(f"[ledger] reaped {reaped} stale in_progress runs at startup")
        print(f"[ledger] initialized at {_ledger.DB_PATH}")
    except Exception as e:
        print(f"[ledger] 起動失敗: {e}")


@app.on_event("startup")
async def _start_render_queue_worker():
    """render queue の DB 初期化 + stale 回収 + worker thread 起動。"""
    global _rq_worker_started
    if _rq_worker_started:
        return
    if os.environ.get("APP_RENDER_QUEUE_DISABLE", "").strip() in ("1", "true", "yes"):
        print("[render-queue] disabled by APP_RENDER_QUEUE_DISABLE")
        return
    try:
        import app_render_queue as _rq
        _rq.init_db()
        reaped = _rq.reap_stale_running()
        if reaped:
            print(f"[render-queue] reaped {reaped} stale running jobs at startup")
        t = _bt_threading.Thread(target=_render_queue_worker_loop, daemon=True,
                                 name="render-queue-worker")
        t.start()
        _rq_worker_started = True
    except Exception as e:
        print(f"[render-queue] 起動失敗: {e}")


# ─── API: render queue（P2-1） ───────────────────
# 6+ チャンネル並列運用時に Premiere/Export がシリアライズされる様子を可視化する。

class RenderQueueEnqueueRequest(BaseModel):
    channel_folder: str
    channel_name: Optional[str] = ""
    vol: int
    video_name: Optional[str] = ""
    stage: str  # "premiere" | "export"
    parent_run_id: Optional[str] = ""

@app.get("/api/render-queue")
def api_render_queue_list(status: Optional[str] = None, limit: int = 50):
    """直近のジョブを返す（status フィルタ可: pending / running / done / error / cancelled）。"""
    import app_render_queue as _rq
    jobs = _rq.list_jobs(status=status, limit=int(limit))
    return {
        "status": "ok",
        "jobs": jobs,
        "counts": {
            "pending": len(_rq.list_jobs("pending", limit=999)),
            "running": len(_rq.list_jobs("running", limit=999)),
        },
    }

@app.get("/api/render-queue/throughput")
def api_render_queue_throughput(days: int = 7):
    """直近 N 日のスループット統計（運用ガイドの「物理的に何チャンネルまで」根拠）。"""
    import app_render_queue as _rq
    return _rq.stats(window_days=int(days))

@app.post("/api/render-queue/enqueue")
def api_render_queue_enqueue(req: RenderQueueEnqueueRequest):
    """pipeline 以外（CLI / 手動）からも投入可能にする内部 API。"""
    import app_render_queue as _rq
    if req.stage not in _rq.ALLOWED_STAGES:
        raise HTTPException(400, f"stage must be one of {_rq.ALLOWED_STAGES}")
    jid = _rq.enqueue(
        channel_folder=req.channel_folder,
        channel_name=req.channel_name or "",
        vol=int(req.vol),
        video_name=req.video_name or "",
        stage=req.stage,
        parent_run_id=req.parent_run_id or "",
    )
    return {"status": "ok", "id": jid}

@app.post("/api/render-queue/{job_id}/cancel")
def api_render_queue_cancel(job_id: int):
    import app_render_queue as _rq
    ok = _rq.cancel(int(job_id))
    if not ok:
        raise HTTPException(409, "pending 状態のジョブだけがキャンセル可能です")
    return {"status": "ok"}

@app.post("/api/render-queue/reap")
def api_render_queue_reap():
    """stale running を error に降格（手動メンテ用）。"""
    import app_render_queue as _rq
    n = _rq.reap_stale_running()
    return {"status": "ok", "reaped": n}


# ─── API: run ledger（P3-1） ─────────────────────

@app.get("/api/runs/ledger")
def api_runs_ledger_list(
    channel_id: Optional[str] = None,
    status: Optional[str] = None,
    kind: Optional[str] = None,
    vol: Optional[int] = None,
    limit: int = 50,
):
    """中央 ledger から直近の run を返す。

    フィルタ: channel_id / status (in_progress|done|failed|cancelled|reconstructed) /
             kind (vol_create|spot_create|auto_resume|manual|reconstructed) / vol。"""
    import app_run_ledger as _ledger
    runs = _ledger.list_runs(channel_id=channel_id, status=status, kind=kind,
                             vol=vol, limit=int(limit))
    return {"status": "ok", "runs": runs, "count": len(runs)}


@app.get("/api/runs/ledger/stats")
def api_runs_ledger_stats(days: int = 7):
    """成功率 / 平均所要 / 自動再投入率の集計。運用 SLO 確認用。"""
    import app_run_ledger as _ledger
    return _ledger.stats(window_days=int(days))


@app.get("/api/runs/ledger/{run_id}")
def api_runs_ledger_get(run_id: str):
    import app_run_ledger as _ledger
    r = _ledger.get_run(run_id)
    if not r:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"status": "ok", "run": r}


@app.get("/api/runs/ledger/{run_id}/chain")
def api_runs_ledger_chain(run_id: str):
    """指定 run の祖先 + 子孫（auto_resume チェーン）を時系列で返す。"""
    import app_run_ledger as _ledger
    chain = _ledger.get_run_chain(run_id)
    if not chain:
        raise HTTPException(404, f"run not found: {run_id}")
    return {"status": "ok", "chain": chain, "count": len(chain)}


class LedgerMigrateRequest(BaseModel):
    channel_id: Optional[str] = None  # 未指定なら全チャンネルを順に処理
    apply: Optional[bool] = False     # False=dry-run, True=実際に挿入

@app.post("/api/runs/ledger/migrate")
def api_runs_ledger_migrate(req: LedgerMigrateRequest = LedgerMigrateRequest()):
    """既存 vol を ledger に reconstructed として取り込む。

    既定は dry-run（diff のみ返す）。`apply: true` で実際に挿入。"""
    import app_run_ledger as _ledger
    chs = get_channels()
    targets = chs if not req.channel_id else [c for c in chs if c.get("id") == req.channel_id]
    if not targets:
        raise HTTPException(404, "対象チャンネルが見つかりません")
    results = []
    total_would = 0
    total_inserted = 0
    for ch in targets:
        cf = ch.get("folder") or ""
        if not cf or not Path(cf).exists():
            continue
        out = _ledger.reconstruct_from_artifacts(
            cf, channel_id=ch.get("id", ""), channel_name=ch.get("name", ""),
            dry_run=not req.apply,
        )
        total_would += len(out.get("would_insert", []))
        total_inserted += out.get("inserted", 0)
        results.append({"channel": ch.get("name"), **out})
    return {
        "status": "ok",
        "dry_run": not req.apply,
        "total_would_insert": total_would,
        "total_inserted": total_inserted,
        "channels": results,
    }


@app.post("/api/runs/ledger/reap")
def api_runs_ledger_reap():
    """stale in_progress を failed に降格（手動メンテ用）。"""
    import app_run_ledger as _ledger
    n = _ledger.reap_stale()
    return {"status": "ok", "reaped": n}


# ─── API: token health（P3-5） ───────────────────

@app.get("/api/token-health")
def api_token_health():
    """全チャンネル + Playwright のトークン健全性を返す。

    UI ダッシュボードで表示する用。warning が無い場合 `warnings: []` で返る。"""
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    try:
        from app_token_health import check_all
        return check_all()
    except Exception as e:
        raise HTTPException(500, f"token health check failed: {e}")


@app.post("/api/token-health/notify")
async def api_token_health_notify():
    """手動でチェックを実行し、warn があれば Discord に通知（cron 動作の即時確認用）。"""
    job = {"id": f"token_health_manual_{int(time.time())}"}
    await _job_token_health(job)
    return {"status": "ok", "job_id": job["id"]}


# ─── API: YouTube 公開ゲート（P2-7） ───────────────
# private upload 後、N 時間経過してから自動 public 化する後段ジョブを動的登録。

class SchedulePublishRequest(BaseModel):
    video_name: str
    channel_folder: Optional[str] = None  # 未指定なら active channel
    channel_name: Optional[str] = None
    delay_hours: float

@app.post("/api/youtube/schedule-publish")
def api_youtube_schedule_publish(req: SchedulePublishRequest):
    """指定 vol を delay_hours 経過後に public 化するジョブを APScheduler に登録。"""
    if _scheduler is None:
        raise HTTPException(503, "scheduler 未起動")
    if req.delay_hours < 0:
        raise HTTPException(400, "delay_hours は 0 以上")
    ch_folder = req.channel_folder
    ch_name = req.channel_name
    if not ch_folder:
        cfg = get_dashboard_config()
        ch_folder = cfg.get("channel_folder") or ""
        ch_name = ch_name or cfg.get("channel_name") or ""
    if not ch_folder:
        raise HTTPException(400, "channel_folder が解決できません")
    folder = Path(ch_folder) / req.video_name
    if not folder.exists():
        raise HTTPException(404, f"video folder not found: {folder}")
    import datetime as _dt
    run_at = _dt.datetime.now() + _dt.timedelta(hours=req.delay_hours)
    try:
        job_id = _schedule_publish(
            video_name=req.video_name, channel_folder=ch_folder,
            channel_name=ch_name or "", run_at=run_at,
        )
    except Exception as e:
        raise HTTPException(500, f"register failed: {e}")
    # marker に scheduled_publish_at を追記（再起動時の復旧用）
    try:
        marker = folder / "youtube_upload.json"
        if marker.exists():
            d = json.loads(marker.read_text(encoding="utf-8"))
            d["scheduled_publish_at"] = run_at.isoformat()
            marker.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[publish] marker 更新失敗（無視）: {e}")
    return {"status": "ok", "job_id": job_id, "scheduled_at": run_at.isoformat()}


@app.post("/api/youtube/publish-now/{video_name}")
def api_youtube_publish_now(video_name: str):
    """指定 vol を**いま即座に** public 化（運営者の手動操作用）。"""
    cfg = get_dashboard_config()
    ch_folder = cfg.get("channel_folder") or ""
    if not ch_folder:
        raise HTTPException(400, "active channel が無い")
    folder = Path(ch_folder) / video_name
    if not folder.exists():
        raise HTTPException(404, f"video folder not found: {folder}")
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    from app_youtube import publish_video_to_public
    result = publish_video_to_public(folder)
    if result.get("status") in ("ok", "already_public"):
        return {"status": "ok", **result}
    raise HTTPException(500, f"publish failed: {result}")


class UpdateSnippetRequest(BaseModel):
    apply_localizations: bool = True


@app.post("/api/youtube/update-snippet/{video_name}")
def api_youtube_update_snippet(video_name: str, req: UpdateSnippetRequest = UpdateSnippetRequest()):
    """既存動画のタイトル/説明/タグ/言語/localizations を YouTube videos.update で更新。

    動画ファイルは再アップロードしない（quota ~50 unit）。`youtube_upload.json` から
    video_id を取得し、`youtube_title.txt` `youtube_description.txt` `youtube_tags.txt`
    `youtube_localizations.json` を読み取って snippet + localizations を反映する。
    """
    cfg = get_dashboard_config()
    ch_folder = cfg.get("channel_folder") or ""
    if not ch_folder:
        raise HTTPException(400, "active channel が無い")
    folder = Path(ch_folder) / video_name
    if not folder.exists():
        raise HTTPException(404, f"video folder not found: {folder}")
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    from app_youtube import update_video_snippet
    result = update_video_snippet(folder, apply_localizations=req.apply_localizations)
    status_code_map = {"ok": 200, "missing": 404, "retryable": 503, "error": 500}
    sc = status_code_map.get(result.get("status", "error"), 500)
    if sc != 200:
        raise HTTPException(sc, f"update-snippet failed: {result}")
    return {"status": "ok", **result}


@app.on_event("startup")
async def _recover_scheduled_publishes():
    """サーバ再起動時に、未公開の `scheduled_publish_at` を持つ vol を再登録。

    完全自走運用で「app.py を再起動 → 公開ジョブが消える」事故を防ぐ。
    過去日付の場合は now+1min で即時投入。"""
    try:
        chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    except Exception:
        chs = []
    if not chs or _scheduler is None:
        return
    import datetime as _dt
    n_recovered = 0
    for ch in chs:
        ch_folder_str = ch.get("folder") or ""
        if not ch_folder_str:
            continue
        ch_folder = Path(ch_folder_str)
        if not ch_folder.exists():
            continue
        for vol_dir in ch_folder.iterdir():
            if not vol_dir.is_dir():
                continue
            marker = vol_dir / "youtube_upload.json"
            if not marker.exists():
                continue
            try:
                d = json.loads(marker.read_text(encoding="utf-8"))
            except Exception:
                continue
            sched = d.get("scheduled_publish_at")
            already = d.get("published_at") or (d.get("privacy") == "public")
            if not sched or already:
                continue
            try:
                run_at = _dt.datetime.fromisoformat(sched)
            except Exception:
                continue
            # 過去日付なら 1 分後に即時投入
            if run_at < _dt.datetime.now():
                run_at = _dt.datetime.now() + _dt.timedelta(minutes=1)
            try:
                _schedule_publish(
                    video_name=vol_dir.name,
                    channel_folder=str(ch_folder),
                    channel_name=ch.get("name", ""),
                    run_at=run_at,
                )
                n_recovered += 1
            except Exception as e:
                print(f"[publish] 復旧失敗 {vol_dir.name}: {e}")
    if n_recovered:
        print(f"[publish] {n_recovered} 件の公開ゲートを再登録（再起動復旧）")


# ─── API: スケジュール ───

@app.get("/api/schedule/jobs")
def api_get_schedule_jobs(channel_id: Optional[str] = None):
    """登録ジョブを返す。

    P2-3: `?channel_id=<id>` で channel 別にフィルタ。
    `channel_id` が空文字 → アクティブチャンネル（dashboard_config）にフォールバック解釈はしない。
    特殊値 `__none__` → channel_id 未指定のジョブ（全チャンネル共通）のみを返す。

    各ジョブには channel_name（registry 由来の解決名）を補完する。"""
    jobs = _scheduler_get_jobs()
    # APScheduler から次回実行時刻を取得
    if _scheduler:
        next_runs = {j.id: (j.next_run_time.isoformat() if j.next_run_time else None)
                     for j in _scheduler.get_jobs()}
        for job in jobs:
            job["next_run"] = next_runs.get(job.get("id"), None)
    # チャンネル名を補完（registry 経由で id → name）
    chs = get_channels()
    name_by_id = {c.get("id"): c.get("name") for c in chs}
    for job in jobs:
        cid = job.get("channel_id") or ""
        job["channel_name"] = name_by_id.get(cid, "") if cid else ""
    # フィルタ
    if channel_id is not None:
        if channel_id == "__none__":
            jobs = [j for j in jobs if not j.get("channel_id")]
        else:
            jobs = [j for j in jobs if j.get("channel_id") == channel_id]
    return {
        "jobs": jobs,
        "scheduler_active": _scheduler is not None,
        "channels": [{"id": c.get("id"), "name": c.get("name")} for c in chs],
    }

class ScheduleJobUpsert(BaseModel):
    id: Optional[str] = None
    type: str  # vol_create | benchmark_refresh | export_window | spot_create
    name: str = ""
    enabled: bool = True
    trigger: dict  # {kind:"cron", day_of_week:"mon,fri", hour:9, minute:0} or {kind:"date", run_date:"..."} or {kind:"cron", expr:"0 7 * * *"}
    channel_id: Optional[str] = None
    vol: Optional[int] = None     # spot_create のみ
    action: Optional[str] = None  # export_window: "on" | "off"
    balance_slots: Optional[bool] = True  # P2-4: 同時刻の vol_create/spot_create を 30 分単位で自動分散
    # P2-6: auto_resume — 失敗 stage を検出し N 分後に同 vol を `--from <stage>` で再投入
    auto_resume: Optional[bool] = False
    auto_resume_delay_min: Optional[int] = 30
    auto_resume_max_attempts: Optional[int] = 3

@app.post("/api/schedule/jobs")
def api_upsert_schedule_job(req: ScheduleJobUpsert):
    if req.type not in JOB_HANDLERS:
        raise HTTPException(400, f"未知の type: {req.type}")
    jobs = _scheduler_get_jobs()
    job = req.dict(exclude_none=True)
    job.pop("balance_slots", None)  # 永続化はしない（リクエストごとの挙動制御フラグ）
    if not job.get("id"):
        job["id"] = f"{req.type}_{int(time.time())}_{secrets.token_hex(3)}"
    # P2-4: 衝突する slot は 30 分単位で自動的に後ろへずらす
    shift_min = 0
    requested_trigger = job.get("trigger") or {}
    if (req.balance_slots is None or req.balance_slots) and req.type in SLOT_BALANCED_TYPES:
        balanced, shift_min, shift_count = _balance_trigger_slot(job, jobs)
        if shift_min > 0:
            job["trigger"] = balanced
            print(f"[scheduler] slot conflict: {job.get('id')} を {shift_min} 分後ろにずらしました "
                  f"({shift_count} スロット分)")
    # 既存上書き or 追加
    found = False
    for i, j in enumerate(jobs):
        if j.get("id") == job["id"]:
            jobs[i] = job; found = True; break
    if not found:
        jobs.append(job)
    _scheduler_save_jobs(jobs)
    _scheduler_reload()
    return {
        "status": "ok",
        "job": job,
        "slot_shifted_minutes": shift_min,
        "slot_requested_trigger": requested_trigger if shift_min > 0 else None,
    }

@app.delete("/api/schedule/jobs/{job_id}")
def api_delete_schedule_job(job_id: str):
    jobs = [j for j in _scheduler_get_jobs() if j.get("id") != job_id]
    _scheduler_save_jobs(jobs)
    _scheduler_reload()
    return {"status": "ok"}

@app.post("/api/schedule/run-now/{job_id}")
async def api_schedule_run_now(job_id: str):
    job = next((j for j in _scheduler_get_jobs() if j.get("id") == job_id), None)
    if not job:
        raise HTTPException(404, "job が見つかりません")
    handler = JOB_HANDLERS.get(job.get("type"))
    if not handler:
        raise HTTPException(400, f"未知の type: {job.get('type')}")
    asyncio.create_task(handler(job))
    return {"status": "started", "job_id": job_id}

@app.get("/api/schedule/history")
def api_schedule_history():
    return {"history": list(reversed(_scheduler_history))[:50]}


# ─── U3: 自走運用パネル（読み取り + 設定保存のみ） ─────────────────────
# ⚠ 重要な安全境界: これらの API は orchestrator.evaluate(dry_run=True) など
#   **副作用ゼロ**の呼び出しと per-channel 設定保存だけを行う。
#   tick()/dispatch() は呼ばない。_scheduler.add_job も一切しない。
#   = 無人稼働（autopilot の実起動）は依然として未実装。autopilot_enabled は
#   設定値を保存するだけで、実際の定期実行は別途 GO 後に app.py へ統合する。

def _build_orchestrator_channels():
    """get_channels() から orchestrator.evaluate 用の channels を構築。
    各 vol dict に folder(絶対パス)を必ず付与（evaluate が Path() で使うため）。
    channels.json の "id" を "channel_id" に詰め替える（/api/runs/active と同形）。"""
    chans = []
    for ch in get_channels():
        folder = ch.get("folder") or ""
        ch_dir = Path(folder)
        if not folder or not ch_dir.exists():
            continue
        vols = []
        try:
            entries = [d for d in ch_dir.iterdir()
                       if d.is_dir() and parse_video_folder_name(d.name)]
        except Exception:
            entries = []
        def _vk(p):
            info = parse_video_folder_name(p.name)
            return info["num"] if info else -1
        entries.sort(key=_vk, reverse=True)
        for d in entries[:10]:
            info = parse_video_folder_name(d.name)
            if not info:
                continue
            vols.append({"vol": info["num"], "name": d.name, "folder": str(d)})
        chans.append({
            "channel_id": ch.get("id", ""),
            "channel_name": ch.get("name", ""),
            "folder": folder,
            "priority": int(ch.get("priority", 100)) if str(ch.get("priority", "")).strip() else 100,
            "autopilot_enabled": bool(ch.get("autopilot_enabled", False)),
            "vols": vols,
        })
    return chans


@app.get("/api/workers/status")
def api_workers_status():
    """自走運用の dry-run 可視化: いま各 vol で着手可能な stage 候補 + チャンネル健全性。
    ⚠ evaluate(dry_run=True) のみ。run()/dispatch()/tick() は呼ばない（副作用ゼロ）。"""
    sys.path.insert(0, str(SHARED_BASE / "Python"))
    try:
        import app_orchestrator as _orch
    except Exception as e:
        raise HTTPException(500, f"orchestrator import 失敗: {e}")
    channels = _build_orchestrator_channels()
    try:
        cands = _orch.evaluate(channels, dry_run=True)
    except Exception as e:
        raise HTTPException(500, f"evaluate 失敗: {e}")
    cand_list = [{"domain": c.domain, "vol": c.vol, "folder": c.folder,
                  "channel_id": c.channel_id} for c in cands]
    # チャンネル別の健全性 + autopilot 設定
    ch_health = []
    for ch in channels:
        cid = ch.get("channel_id", "")
        try:
            fails = _orch.consecutive_failures(cid)
            tripped = _orch.is_channel_tripped(cid)
        except Exception:
            fails, tripped = 0, False
        ch_health.append({
            "channel_id": cid, "channel_name": ch.get("channel_name", ""),
            "priority": ch.get("priority", 100),
            "autopilot_enabled": ch.get("autopilot_enabled", False),
            "consecutive_failures": fails, "tripped": tripped,
            "candidate_count": sum(1 for c in cand_list if c["channel_id"] == cid),
        })
    return {
        "status": "ok",
        "candidates": cand_list,
        "channels": ch_health,
        "breaker_threshold": getattr(_orch, "BREAKER_THRESHOLD", 3),
        "autopilot_default_stages": getattr(_orch, "AUTOPILOT_DEFAULT_STAGES", []),
        "scheduler_registered": False,  # ⚠ orchestrator tick は未登録（無人稼働しない）
    }


@app.get("/api/quota/status")
def api_quota_status():
    """各チャンネルの YouTube quota 残（policy 配分の可視化用）。読み取りのみ。"""
    import app_youtube as _ytm
    out = []
    for ch in get_channels():
        folder = ch.get("folder") or ""
        ch_dir = Path(folder)
        if not folder or not ch_dir.exists():
            continue
        try:
            used = _ytm.quota_used_in_window(ch_dir)
            cap = _ytm.DEFAULT_DAILY_QUOTA_CAP
            out.append({
                "channel_id": ch.get("id", ""), "channel_name": ch.get("name", ""),
                "used": used, "cap": cap, "remaining": max(0, cap - used),
                "per_upload": _ytm.QUOTA_PER_UPLOAD,
            })
        except Exception:
            continue
    return {"status": "ok", "channels": out}


def _save_channel_scalar(channel_id: str, updates: dict):
    """指定 channel_id の .app_channel_config.json に updates をマージ保存。
    channels.json から folder を解決（active 限定でない per-channel 保存）。"""
    chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    target = next((c for c in chs if c.get("id") == channel_id), None)
    if not target:
        raise HTTPException(404, f"channel not found: {channel_id}")
    folder = target.get("folder") or ""
    if not folder or not Path(folder).exists():
        raise HTTPException(400, f"channel folder not found: {folder}")
    p = Path(folder) / _CHANNEL_CONFIG_FILENAME
    d = load_json(p, {})
    d.update(updates)
    save_json(p, d)
    return {"status": "ok", "channel_id": channel_id, "saved": updates}


class ChannelPriorityRequest(BaseModel):
    channel_id: str
    priority: int


@app.put("/api/config/priority")
def api_set_channel_priority(req: ChannelPriorityRequest):
    """channel 優先度を per-channel 保存（orchestrator policy 配分が参照）。"""
    return _save_channel_scalar(req.channel_id, {"priority": int(req.priority)})


class AutopilotRequest(BaseModel):
    channel_id: str
    enabled: bool


@app.get("/api/workers/autopilot")
def api_get_autopilot():
    """各チャンネルの autopilot_enabled 設定を返す（保存値の読み取りのみ）。"""
    out = []
    for ch in get_channels():
        folder = ch.get("folder") or ""
        cc = {}
        if folder:
            cc = load_json(Path(folder) / _CHANNEL_CONFIG_FILENAME, {})
        out.append({
            "channel_id": ch.get("id", ""), "channel_name": ch.get("name", ""),
            "autopilot_enabled": bool(cc.get("autopilot_enabled", False)),
            "priority": int(cc.get("priority", 100)) if str(cc.get("priority", "")).strip() else 100,
        })
    return {"status": "ok", "channels": out,
            "scheduler_registered": False}  # ⚠ 保存しても実起動はしない


@app.put("/api/workers/autopilot")
def api_set_autopilot(req: AutopilotRequest):
    """autopilot ON/OFF を per-channel 保存。
    ⚠ これは設定値の保存のみ。実際の無人稼働（tick の定期実行）は未実装で、
       別途 app.py への orchestrator 統合（GO 後）が必要。ここでは scheduler に
       一切触れない（add_job しない）。"""
    return _save_channel_scalar(req.channel_id, {"autopilot_enabled": bool(req.enabled)})


def _run_uvicorn():
    """pyproject.toml の `automation-studio` コマンドエントリポイント。
    `python3 Python/app.py` 直接起動でも同関数を呼ぶ。
    """
    import uvicorn
    print(f"共有ドライブ: {SHARED_BASE}")
    print(f"Web: {WEB_DIR}")
    print(f"Config: {CONFIG_DIR}")
    # ポートは 8888 固定（フォールバックの 8889/8890... 揺れを止める）。
    # 8888 が使用中の場合は start.sh が起動前に kill する責務。
    # APP_PORT / ORZZ_PORT env で明示指定された場合のみ上書き可能。
    port_env = (os.environ.get("APP_PORT") or os.environ.get("ORZZ_PORT"))
    port = int(port_env) if port_env else 8888
    host = (os.environ.get("APP_HOST") or os.environ.get("ORZZ_HOST") or "127.0.0.1").strip()
    public_hosts = {".".join(("0", "0", "0", "0")), ":" * 2}
    if host in public_hosts and not AUTH_REQUIRED:
        print("⚠️  外部公開ホストで認証が無効です。APP_AUTH_REQUIRED=1 の併用を推奨します。")
    print(f"起動: http://{host}:{port}")
    uvicorn.run(app, host=host, port=port)  # nosec B104


if __name__ == "__main__":
    _run_uvicorn()
