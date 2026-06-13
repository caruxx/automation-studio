#!/usr/bin/env python3
"""app_core — orzz. Dashboard の共通土台（D9: app.py 段階分割の第1段で抽出）。

パス/設定定数・config ローダ・チャンネル別設定・ベンチ設定・認証ヘルパ・
共有可変グローバル（active_tasks/task_logs/task_meta/_youtube_* など）・
タスク履歴/サブプロセス/youtube アップロードキューのヘルパを保持する。

依存方向（一方向 DAG）: _app_config → app_core → routers/* → app.py。
app_core は app.py や routers を import しない（循環なし）。
app=FastAPI インスタンス・middleware・全ルート・startup hooks・pydantic モデルは
app.py 側に残る。本モジュールは末尾で __all__ を自動生成し、単一アンダースコア名も
含めて全土台シンボルを export する（app.py は `from app_core import *` で取り込む）。
"""
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
# パイプライン本体スクリプト（複数ドメイン=pipeline/images が参照する共有定数。D9）
PIPELINE_SCRIPT = PYTHON_DIR / "app_pipeline.py"
# Premiere 連携スクリプト（premiere/render-queue の両ドメインが参照する共有定数。D9）
PREMIERE_SCRIPT = PYTHON_DIR / "app_premiere.py"

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
# PC 非依存の運用設定は共有ドライブ config/ に置く（live_config.json と同方針）。
# prompts/master_prompts はチャンネル運用資産、discord は通知先で全 PC 共通。
PROMPTS_CONFIG = SHARED_CONFIG_DIR / "prompts.json"
DISCORD_CONFIG = SHARED_CONFIG_DIR / "discord_config.json"
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
    # channel_folder（GLOBAL_ONLY）を現マシンへ解決。他マシンで保存された値でも自機で有効化し、
    # 各所の `ch.get("folder")==config.get("channel_folder")` 比較を揃える。保存先はローカルのみ＝安全。
    if config.get("channel_folder"):
        config["channel_folder"] = _resolve_to_current_host(config["channel_folder"])
    return config


# ─── チャンネル別設定（Google Drive 同期対応） ───
# これらのキーは <channel_folder>/.app_channel_config.json に保存される。
# 共有ドライブ上のチャンネルフォルダを介して 2 PC 間で自動同期される。
PER_CHANNEL_KEYS = {
    "persona", "rival_channels",
    "spreadsheet_channel_detail_url", "spreadsheet_growth_tracking_url",
    "benchmark_pinned_names", "benchmark_filter", "benchmark_extra_urls",
    "channel_icon", "template_prproj", "template_psd", "export_path",
    "export_engine",  # 書き出しエンジン: "ame"(Premiere/AME・既定) / "ffmpeg"(app_ffrender ループ連結方式・静止画チャンネル向け)
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
    p = Path(_resolve_to_current_host(f))  # 他マシンで保存された channel_folder を現マシンに解決
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


_SHARED_MARKER = "/共有ドライブ/"


def _shared_drive_root() -> Path:
    """現マシンの <共有ドライブ> ルート（SHARED_BASE = <共有ドライブ>/DEV/_claude の 2 つ上）。"""
    return SHARED_BASE.parent.parent


def _rel_under_shared(folder) -> Optional[str]:
    """folder 文字列から '/共有ドライブ/' 以降の相対部分を NFC で返す（無ければ None）。
    2 台の Mac（/Users/abe_kota… と /Users/asobimori…）でホーム名が違っても
    共有ドライブ以降は共通なので、これをホーム非依存キー／移植の基点に使う。"""
    if not folder:
        return None
    s = unicodedata.normalize("NFC", str(folder))
    idx = s.find(_SHARED_MARKER)
    if idx < 0:
        return None
    return s[idx + len(_SHARED_MARKER):]


def _resolve_to_current_host(folder):
    """folder を現マシンの共有ドライブパスへ解決（ホーム非依存）。
    '/共有ドライブ/' 以降を現マシンの共有ドライブ root に付け替える。
    マーカー無し（外部 SSD /Volumes/… 等）や解決不能時は原文を返す。
    ⚠ 解決値は『表示・FS アクセス用』。共有 channels.json への保存は原文を使うこと
      （解決値を書き戻すと他マシンのパスを壊す）。"""
    if not folder:
        return folder
    rel = _rel_under_shared(folder)
    if rel is None:
        return folder
    root = _shared_drive_root()
    # R5 防御: root が共有ドライブ配下でなければ（find_shared_drive フォールバック等）解決を諦める
    if _SHARED_MARKER.strip("/") not in unicodedata.normalize("NFC", str(root)):
        return folder
    return str(root / rel)


def _norm_folder(f) -> str:
    """フォルダパスを比較キーに正規化する。
    - 共有ドライブ配下なら『共有ドライブ/<相対>』(NFC) を返し、ホーム名差
      (/Users/abe_kota vs /Users/asobimori) を吸収する（2 台 Mac の重複増殖を防ぐ）。
    - 共有ドライブ外は従来どおり resolve() で実パスに畳み NFC 統一。"""
    if not f:
        return ""
    rel = _rel_under_shared(f)
    if rel is not None:
        return unicodedata.normalize("NFC", "共有ドライブ/" + rel)
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


def migrate_shared_settings(verbose: bool = True) -> None:
    """PC 非依存の運用設定（discord_config / prompts / master_prompts）を
    旧ローカル置き場 ~/.config/<app_id>/ から共有 config/ へ未存在のみ移行する。

    旧置き場は per-PC かつ per-app_id（チャンネル切替で分裂）だったため、
    全 app_id ディレクトリを走査して mtime 最新を採用する。旧ファイルは残す。
    """
    try:
        if _norm_folder(str(CONFIG_DIR)) == _norm_folder(str(SHARED_CONFIG_DIR)):
            return  # フォールバック時は同一 → 何もしない
        SHARED_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        for fn in ("discord_config.json", "prompts.json", "master_prompts.json"):
            dst = SHARED_CONFIG_DIR / fn
            if dst.exists():
                continue
            candidates = []
            for p in (Path.home() / ".config").glob(f"*/{fn}"):
                try:
                    candidates.append((p.stat().st_mtime, p))
                except OSError:
                    continue
            for _, src in sorted(candidates, key=lambda t: t[0], reverse=True):
                try:
                    if not json.loads(src.read_text(encoding="utf-8")):
                        continue  # 空データはスキップして次候補へ
                    shutil.copy2(src, dst)
                    if fn == "discord_config.json":
                        try:
                            dst.chmod(0o600)
                        except Exception:
                            pass
                    if verbose:
                        print(f"[shared-config] 共有へ移行: {fn} ← {src}")
                    break
                except Exception:
                    continue
    except Exception as e:
        print(f"[shared-config] 移行失敗（続行）: {e}")


def folder_name_pattern() -> "re.Pattern":
    """フォルダ識別の正規表現。新形式と旧 orzz 形式の両方を認識。

    マッチ例: "78_orzz_260420", "79_vol_260427", "80_mybgm_260501"
    group(1)=番号, group(2)=prefix
    """
    return VIDEO_FOLDER_RE

def get_suno_config():
    """SUNO 設定を返す。
    - api_key / claude_cli / codex_cli / headless はグローバル（マシン別、~/.config/{app_id}/suno_config.json）
    - provider / model は per-channel を反映（D11: ch ごとに LLM を選べる。per-ch 未設定なら
      グローバル suno_config の値がフォールバックとして残る）
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
        # api_key / CLI コマンド / headless はマシン別（実行環境依存）なので per-channel を無視。
        # provider / model はあえて除外しない＝per-channel 値で base を上書き（D11）。
        if k in ("api_key", "claude_cli", "codex_cli", "headless"):
            continue
        base[k] = v
    return base


# グローバル（マシン別）に保つキー。provider/model は per-channel 保存可（D11）。
_SUNO_GLOBAL_KEYS = {"api_key", "claude_cli", "codex_cli", "headless"}


def save_suno_config_smart(patch: dict):
    """SUNO 設定の部分更新を per-channel と global に振り分け保存。
    - api_key / claude_cli / codex_cli / headless → ~/.config/{app_id}/suno_config.json（マシン別）
    - provider / model / prompt / generation_mode / loop_count / loop_interval_sec / loop_batch / loop_instrumental_fill 等 → per-channel（D11: provider/model も ch 別）
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



# ─── D9: youtube/queue/schedule ドメインが共有するヘルパ群を app.py から昇格 ───
# (channel registry / mp4 export 検出 / youtube upload command / upload defaults 等)

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


def get_channels():
    chs = load_json(CHANNELS_CONFIG, []) if CHANNELS_CONFIG.exists() else []
    # 各チャンネルに icon_url 等の派生フィールドを補う（保存はしない＝読み取りのみ）
    for ch in chs:
        cache = ch.get("icon_cache") or {}
        ch["icon_url"] = cache.get("url", "") or ""
        ch.setdefault("youtube_url", "")
        ch.setdefault("youtube_channel_id", "")
        ch.setdefault("handle", "")
        # 表示・FS アクセス用に現マシンのパスへ解決（保存はしない＝原文は channels.json に維持）
        if ch.get("folder"):
            ch["folder"] = _resolve_to_current_host(ch["folder"])
        if not ch.get("prefix"):
            ch["prefix"] = infer_file_prefix_from_folder(Path(ch.get("folder") or "")) or ""
    return chs

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


def _read_json_dict(p: Path, default=None) -> dict:
    if not p.exists():
        return dict(default or {})
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else dict(default or {})
    except Exception:
        return dict(default or {})


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


def _active_channel_registry_entry() -> dict:
    """dashboard_config のアクティブチャンネルに対応する registry 行を返す。"""
    cfg = get_dashboard_config()
    active_folder = (cfg.get("channel_folder") or "").strip()
    active_name = (cfg.get("channel_name") or "").strip()
    for ch in get_channels():
        if active_folder and _norm_folder(ch.get("folder") or "") == _norm_folder(active_folder):
            return ch
    for ch in get_channels():
        if active_name and (ch.get("name") or "") == active_name:
            return ch
    return {}


def _channel_registry_entry_for_folder(channel_folder: Path) -> dict:
    target = _norm_folder(str(channel_folder))
    for ch in get_channels():
        if _norm_folder(ch.get("folder") or "") == target:
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


# ─── 公開シンボル: 土台の全名（単一 _ 含む / dunder 除く）を自動 export ───
__all__ = [n for n in dir() if not n.startswith('__')]
