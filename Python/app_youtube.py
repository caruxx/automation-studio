#!/usr/bin/env python3
"""orzz. YouTube アップロード自動化スクリプト

設定の優先順位（弱→強）:
  1. ~/.config/{app_id}/youtube_upload_defaults.json   ← チャンネル横断のテンプレート
  2. <video_folder>/youtube_upload_overrides.json      ← 動画別の上書き
  3. CLI 引数 / API 引数                                ← 最優先

タイトル・説明文の他言語ローカライズ:
  <video_folder>/youtube_localizations.json
  形式: {"en": {"title": "...", "description": "..."}, "ja": {...}}
"""

import argparse
import datetime
import json
import mimetypes
import os
import re
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

# pipeline (app_pipeline.py) と一致させる sentinel exit code。
# 76 = transient/retryable failure（403/429/quotaExceeded/5xx）。
# 上位の retry レイヤがこれを見て指数バックオフで再投入する。
EXIT_RETRYABLE = 76

# 77 = YouTube Data API の日次クオータを使い切った（24h ウィンドウで上限到達）。
# 短時間 retry では復旧しないので、pipeline は **retry せず即座に Discord 通知して停止**する。
# 翌日のスケジュール実行で自動再投入される運用を想定。
EXIT_QUOTA_EXHAUSTED = 77

# 78 = OAuth トークンが想定チャンネルと一致しない。
# ブランドアカウント選択ミスで別チャンネルにアップロードする事故を防ぐ。
EXIT_CHANNEL_MISMATCH = 78

# YouTube Data API v3 の videos.insert は 1 回 1600 unit を消費する（公式仕様）。
# デフォルト日次クオータは 10,000 unit / プロジェクト。安全マージンを取って 9600 でガード。
QUOTA_PER_UPLOAD = int(os.environ.get("APP_YT_QUOTA_PER_UPLOAD", "1600"))
DEFAULT_DAILY_QUOTA_CAP = int(os.environ.get("APP_YT_DAILY_QUOTA_CAP", "9600"))
QUOTA_WINDOW_HOURS = int(os.environ.get("APP_YT_QUOTA_WINDOW_HOURS", "24"))
QUOTA_FILENAME = ".youtube_quota.json"


def _quota_state_path(channel_folder) -> Path:
    """quota state を per-channel で保持するパス。"""
    return Path(channel_folder) / QUOTA_FILENAME


def _load_quota_state(p: Path) -> dict:
    if not p.exists():
        return {"events": []}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {"events": []}
    except Exception:
        return {"events": []}


def quota_used_in_window(channel_folder, window_hours: int = QUOTA_WINDOW_HOURS) -> int:
    """直近 window_hours の累積 quota コスト（unit）を返す。"""
    p = _quota_state_path(channel_folder)
    state = _load_quota_state(p)
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=window_hours)
    total = 0
    for ev in state.get("events", []):
        try:
            ts = datetime.datetime.fromisoformat(ev["ts"])
        except Exception:
            continue
        if ts < cutoff:
            continue
        total += int(ev.get("cost", QUOTA_PER_UPLOAD))
    return total


def record_upload_quota(channel_folder, cost: int = QUOTA_PER_UPLOAD) -> None:
    """upload 成功時に呼ぶ。state ファイルに event 追加 + 古い events を prune。"""
    p = _quota_state_path(channel_folder)
    state = _load_quota_state(p)
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=QUOTA_WINDOW_HOURS * 2)
    fresh = []
    for ev in state.get("events", []):
        try:
            ts = datetime.datetime.fromisoformat(ev["ts"])
            if ts >= cutoff:
                fresh.append(ev)
        except Exception:
            continue
    fresh.append({"ts": datetime.datetime.utcnow().isoformat(), "cost": int(cost)})
    state["events"] = fresh
    state["_last_recorded"] = datetime.datetime.utcnow().isoformat()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"⚠ quota state 書込失敗: {e}", file=sys.stderr)


def check_quota_before_upload(channel_folder, cap: int = None) -> tuple:
    """upload 前のローカル quota チェック。

    Returns: (ok: bool, used_units: int, remaining_units: int, cap: int)
    ok=False の場合は呼び出し側で sys.exit(EXIT_QUOTA_EXHAUSTED) を行う想定。
    """
    cap = int(cap if cap is not None else DEFAULT_DAILY_QUOTA_CAP)
    used = quota_used_in_window(channel_folder)
    remaining = cap - used
    ok = remaining >= QUOTA_PER_UPLOAD
    return ok, used, remaining, cap


def _is_retryable_http_error(err: HttpError) -> bool:
    """YouTube API が返した HttpError を retryable / non-retryable に分類。"""
    status = getattr(err, "status_code", None) or (
        err.resp.status if getattr(err, "resp", None) is not None else None
    )
    if status in (429, 500, 502, 503, 504):
        return True
    # 403 は quotaExceeded / rateLimitExceeded のみ retryable（forbidden は permanent）
    if status == 403:
        try:
            content = err.content.decode("utf-8", errors="ignore") if isinstance(err.content, (bytes, bytearray)) else str(err.content)
        except Exception:
            content = str(err)
        for marker in ("quotaExceeded", "rateLimitExceeded", "userRateLimitExceeded"):
            if marker in content:
                return True
    return False

# 設定ディレクトリ解決（_app_config 経由で app_id 切替に追従）
sys.path.insert(0, str(Path(__file__).parent))
try:
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"

CLIENT_SECRET = CONFIG_DIR / "youtube_client_secret.json"
# グローバルトークン（後方互換 / フォールバック）。
# ブランドアカウント運用ではチャンネル別トークン（<channel_folder>/.youtube_token.json）を使う。
TOKEN_FILE = CONFIG_DIR / "youtube_token.json"
# チャンネルフォルダ内に置くトークンファイル名（Google Drive 同期で 2PC 共有）
CHANNEL_TOKEN_FILENAME = ".youtube_token.json"
UPLOAD_DEFAULTS_FILE = CONFIG_DIR / "youtube_upload_defaults.json"

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
]

# アップロード設定の既定値（ユーザーが何も設定していないとき）
BUILTIN_DEFAULTS = {
    "category_id": "10",                # 10 = Music
    "default_language": "en",           # snippet.defaultLanguage（タイトル/説明の言語）
    "default_audio_language": "en",     # snippet.defaultAudioLanguage（音声の言語）
    "made_for_kids": False,             # status.selfDeclaredMadeForKids
    "synthetic_media": True,            # status.containsSyntheticMedia（AI 生成開示）
    "license": "youtube",               # status.license: "youtube" | "creativeCommon"
    "embeddable": True,                 # status.embeddable
    "public_stats_viewable": True,      # status.publicStatsViewable
    "notify_subscribers": True,         # videos.insert の notifySubscribers パラメータ
    "localization_languages": ["ja", "zh-Hans", "zh-Hant", "ko"],  # 翻訳生成対象（UI のデフォルト）
}


_CHANNEL_CONFIG_FILENAME = ".app_channel_config.json"


def _load_channel_youtube_defaults(video_folder) -> dict:
    """video_folder の親（チャンネルフォルダ）の .app_channel_config.json から
    youtube_upload_defaults を読み込む。"""
    try:
        ch_folder = Path(video_folder).parent
        p = ch_folder / _CHANNEL_CONFIG_FILENAME
        if p.exists():
            cc = json.loads(p.read_text(encoding="utf-8"))
            yu = cc.get("youtube_upload_defaults") if isinstance(cc, dict) else None
            return yu if isinstance(yu, dict) else {}
    except Exception:
        pass
    return {}


def load_upload_defaults(video_folder=None) -> dict:
    """テンプレート設定を読み込む。優先順位:
       1. BUILTIN_DEFAULTS
       2. グローバル ~/.config/{app_id}/youtube_upload_defaults.json (legacy)
       3. <channel_folder>/.app_channel_config.json["youtube_upload_defaults"] (Google Drive 同期)
    """
    out = dict(BUILTIN_DEFAULTS)
    if UPLOAD_DEFAULTS_FILE.exists():
        try:
            out.update(json.loads(UPLOAD_DEFAULTS_FILE.read_text(encoding="utf-8")) or {})
        except Exception:
            pass
    if video_folder:
        ch_yu = _load_channel_youtube_defaults(video_folder)
        if ch_yu:
            out.update(ch_yu)
    return out


def load_video_overrides(folder) -> dict:
    """動画フォルダの上書き設定を読み込む。"""
    p = Path(folder) / "youtube_upload_overrides.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_localizations(folder) -> dict:
    """動画フォルダのローカライズ（多言語タイトル/説明）を読み込む。"""
    p = Path(folder) / "youtube_localizations.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        out = {}
        for lang, entry in data.items():
            if not isinstance(entry, dict):
                continue
            t = (entry.get("title") or "").strip()
            d = (entry.get("description") or "").strip()
            if t or d:
                out[lang] = {"title": t, "description": d}
        return out
    except Exception:
        return {}


def merge_settings(folder, **overrides) -> dict:
    """defaults → 動画別 overrides → 引数 の順でマージ。None は無視。
    defaults はチャンネル別ファイルを優先（folder を渡すことで自動解決）。"""
    s = load_upload_defaults(video_folder=folder)
    s.update(load_video_overrides(folder))
    for k, v in overrides.items():
        if v is not None:
            s[k] = v
    return s


def resolve_token_path(video_folder=None, override=None) -> Path:
    """トークンファイルのパスを決定する。
    解決順序（強→弱）:
      1. override（--token-file で明示指定された場合）
      2. <channel_folder>/.youtube_token.json （video_folder の親フォルダ）
      3. グローバル ~/.config/{app_id}/youtube_token.json （後方互換）

    1 と 2 はチャンネル別運用、3 は旧仕様。書き込み（新規認証）も同じ順序で先頭の場所に保存。
    """
    if override:
        return Path(override)
    if video_folder:
        ch = Path(video_folder).parent / CHANNEL_TOKEN_FILENAME
        # チャンネル別が既に存在すれば最優先。無くてもブランドアカウント運用前提でここに新規作成。
        return ch
    return TOKEN_FILE


def get_credentials(video_folder=None, token_override=None):
    """OAuth トークンを取得（必要なら再認証）。

    トークン保存場所はチャンネル別が既定。video_folder（動画フォルダ）から
    親（チャンネルフォルダ）を導出して `.youtube_token.json` を作成・更新する。
    Google Drive 同期で 2PC 間共有される。
    """
    token_path = resolve_token_path(video_folder=video_folder, override=token_override)
    legacy_path = TOKEN_FILE

    # 読み込み: チャンネル別パスが解決できる場合は、そのチャンネルのトークンだけを使う。
    # 旧グローバルトークンへのフォールバックは、folder/token-file なしの旧CLI運用だけに限定する。
    creds = None
    if token_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except Exception as e:
            print(f"⚠️ トークン読み込み失敗 ({token_path}): {e}")
    allow_legacy_fallback = (not token_override and not video_folder and legacy_path != token_path)
    if creds is None and allow_legacy_fallback and legacy_path.exists():
        try:
            creds = Credentials.from_authorized_user_file(str(legacy_path), SCOPES)
            print(f"ℹ️ レガシーグローバルトークンを使用: {legacy_path}")
        except Exception:
            pass

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CLIENT_SECRET.exists():
                print(f"エラー: {CLIENT_SECRET} が見つかりません")
                print("Google Cloud Console からOAuthクライアントシークレットをダウンロードして配置してください")
                sys.exit(1)
            print("🔑 OAuth 同意画面を開きます。ブランドアカウントを使う場合はアップロード先のチャンネルを選択してください。")
            print(f"   トークン保存先: {token_path}")
            sys.stdout.flush()
            flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES)
            creds = flow.run_local_server(port=0)  # 空きポートを自動選択
        # 書き込み: 解決した token_path に保存（チャンネル別運用なら GDrive 同期される）
        try:
            token_path.parent.mkdir(parents=True, exist_ok=True)
            token_path.write_text(creds.to_json())
            print(f"✅ トークン保存: {token_path}")
        except Exception as e:
            print(f"⚠️ トークン保存失敗 ({token_path}): {e}")
            # フォールバック: グローバルへ保存
            legacy_path.write_text(creds.to_json())
            print(f"   フォールバックでグローバルに保存: {legacy_path}")
    return creds


def get_authenticated_channel_info(youtube) -> dict:
    """現在の OAuth トークンが指している YouTube チャンネルを返す。"""
    resp = youtube.channels().list(part="id,snippet", mine=True, maxResults=10).execute()
    items = resp.get("items") or []
    channels = []
    for it in items:
        sn = it.get("snippet") or {}
        channels.append({
            "id": it.get("id") or "",
            "title": sn.get("title") or "",
            "custom_url": sn.get("customUrl") or "",
        })
    primary = channels[0] if channels else {"id": "", "title": "", "custom_url": ""}
    return {
        "channel_id": primary.get("id", ""),
        "channel_title": primary.get("title", ""),
        "custom_url": primary.get("custom_url", ""),
        "channels": channels,
    }


def assert_expected_channel(youtube, expected_channel_id=None, expected_channel_name=None) -> dict:
    expected = (expected_channel_id or "").strip()
    expected_name = (expected_channel_name or "").strip()
    info = get_authenticated_channel_info(youtube)
    actual_ids = {c.get("id") for c in info.get("channels", []) if c.get("id")}
    if expected and expected not in actual_ids:
        actual = ", ".join(
            f"{c.get('title') or '?'} ({c.get('id') or '?'})"
            for c in info.get("channels", [])
        ) or "取得できませんでした"
        label = expected_channel_name or expected
        print(
            "\n[YT_CHANNEL_MISMATCH] OAuth トークンのチャンネルが一致しません\n"
            f"  期待: {label} ({expected})\n"
            f"  実際: {actual}\n"
            "このチャンネルを WEB で選び直し、YouTube 再認証で正しいブランドチャンネルを選択してください。",
            flush=True,
        )
        sys.exit(EXIT_CHANNEL_MISMATCH)
    if not expected and expected_name and info.get("channel_title"):
        norm = lambda s: re.sub(r"\s+", "", (s or "").strip().lower())
        if norm(expected_name) != norm(info.get("channel_title", "")):
            print(
                "\n[YT_CHANNEL_MISMATCH] OAuth トークンのチャンネル名が一致しません\n"
                f"  期待: {expected_name}\n"
                f"  実際: {info.get('channel_title') or '?'} ({info.get('channel_id') or '?'})\n"
                "このチャンネルに YouTube URL を登録するか、WEB で YouTube 再認証して正しいブランドチャンネルを選択してください。",
                flush=True,
            )
            sys.exit(EXIT_CHANNEL_MISMATCH)
    return info


def find_video_file(folder):
    folder = Path(folder)
    for f in sorted(folder.glob("*vol*.mp4")):
        return f
    for f in sorted(folder.glob("*.mp4")):
        if f.name != "audio-spectrum01.mp4":
            return f
    return None


def find_thumbnail(folder):
    """サムネイル画像を解決。優先順位:
      1. サムネイル.jpg / サムネイル.png （日本語名の明示指定）
      2. プロジェクトフォルダ直下の thumbnail.jpg / thumbnail.png
      3. vol*.jpg / vol*.png （旧仕様の vol-prefix 命名）
    """
    folder = Path(folder)
    for name in ("サムネイル.jpg", "サムネイル.jpeg", "サムネイル.png", "thumbnail.jpg", "thumbnail.jpeg", "thumbnail.png"):
        p = folder / name
        if p.exists():
            return p
    for pattern in ("vol*.jpg", "vol*.jpeg", "vol*.png"):
        for f in folder.glob(pattern):
            return f
    return None


def load_description(folder):
    desc_file = Path(folder) / "youtube_description.txt"
    if desc_file.exists():
        return desc_file.read_text(encoding="utf-8").strip()
    return ""


def load_title(folder):
    tf = Path(folder) / "youtube_title.txt"
    if tf.exists():
        t = tf.read_text(encoding="utf-8").strip()
        if t:
            return t
    return ""


def load_tags(folder):
    """youtube_tags.txt（改行 or カンマ区切り）を配列で返す。無ければ既定タグ。"""
    tf = Path(folder) / "youtube_tags.txt"
    if tf.exists():
        raw = tf.read_text(encoding="utf-8")
        parts = [t.strip() for t in raw.replace(",", "\n").splitlines() if t.strip()]
        if parts:
            return parts
    return ["BGM", "Lounge", "Chill", "Relax", "Study", "Work",
            "AI Music", "SUNO", "orzz"]


def extract_vol_number(folder):
    m = re.match(r"^(\d+)_", Path(folder).name)
    return m.group(1) if m else "00"


def upload_video(folder, title=None, schedule=None, privacy="private", tags=None,
                 video_path=None,
                 # 新規: 詳細設定（None ならデフォルト→override→組み込み既定の順で解決）
                 default_language=None, default_audio_language=None,
                 made_for_kids=None, synthetic_media=None,
                 license_type=None, embeddable=None, public_stats_viewable=None,
                 notify_subscribers=None, category_id=None,
                 use_localizations=True,
                 token_file=None,
                 expected_channel_id=None, expected_channel_name=None):
    folder = Path(folder)
    vol_num = extract_vol_number(folder)

    if video_path:
        video_file = Path(video_path)
        if not video_file.exists():
            print(f"エラー: --video-path で指定された mp4 が存在しません: {video_file}")
            sys.exit(1)
    else:
        video_file = find_video_file(folder)
        if not video_file:
            print(f"エラー: {folder} 内にMP4ファイルが見つかりません")
            sys.exit(1)

    thumbnail = find_thumbnail(folder)
    description = load_description(folder)

    # タイトル: 引数 > youtube_title.txt > 既定
    if not title:
        title = load_title(folder) or f"orzz. vol.{vol_num}"

    # タグ: 引数 > youtube_tags.txt > 既定タグ
    if not tags:
        tags = load_tags(folder)

    # 設定マージ（defaults → 動画別 → 引数）
    s = merge_settings(
        folder,
        category_id=category_id,
        default_language=default_language,
        default_audio_language=default_audio_language,
        made_for_kids=made_for_kids,
        synthetic_media=synthetic_media,
        license=license_type,
        embeddable=embeddable,
        public_stats_viewable=public_stats_viewable,
        notify_subscribers=notify_subscribers,
    )

    # ローカライズ（多言語タイトル・説明）
    localizations = load_localizations(folder) if use_localizations else {}

    # 事前 quota チェック（ローカル記録ベース）。 24h 内の累積コストが上限を超えそうなら
    # API を叩かずに sentinel exit で抜け、上位の retry 層 / scheduler に判断を委ねる。
    channel_folder = folder.parent
    ok, used, remaining, cap = check_quota_before_upload(channel_folder)
    if not ok:
        print(
            f"\n[YT_QUOTA_EXHAUSTED] used={used} / cap={cap} (per-upload={QUOTA_PER_UPLOAD}, "
            f"window={QUOTA_WINDOW_HOURS}h, channel={channel_folder.name})\n"
            f"翌日の scheduler 実行で自動再投入されます（手動再開なら ~24h 後）。",
            flush=True,
        )
        sys.exit(EXIT_QUOTA_EXHAUSTED)
    print(f"\n📊 quota: used={used} / cap={cap} (残 {remaining} unit ≈ {remaining // QUOTA_PER_UPLOAD} upload 分)")

    file_size_mb = video_file.stat().st_size / 1024 / 1024
    print(f"動画: {video_file.name} ({file_size_mb:.1f} MB)")
    print(f"タイトル: {title}")
    print(f"サムネイル: {thumbnail.name if thumbnail else 'なし'}")
    print(f"説明文: {len(description)} 文字")
    print(f"タグ: {len(tags)}件 ({', '.join(tags[:5])}{'...' if len(tags)>5 else ''})")
    print(f"公開設定: {privacy}")
    print(f"カテゴリID: {s['category_id']} | 言語: {s['default_language']}/{s['default_audio_language']}")
    print(f"AI 生成開示: {s['synthetic_media']} | 子供向け: {s['made_for_kids']} | ライセンス: {s['license']}")
    print(f"埋め込み: {s['embeddable']} | 統計公開: {s['public_stats_viewable']} | 登録者通知: {s['notify_subscribers']}")
    if localizations:
        print(f"ローカライズ: {len(localizations)} 言語 ({', '.join(localizations.keys())})")
    if schedule:
        print(f"公開予約: {schedule}")
    sys.stdout.flush()

    print("\n認証中...")
    sys.stdout.flush()
    creds = get_credentials(video_folder=folder, token_override=token_file)
    youtube = build("youtube", "v3", credentials=creds)
    ch_info = assert_expected_channel(
        youtube,
        expected_channel_id=expected_channel_id,
        expected_channel_name=expected_channel_name,
    )
    print("✅ 認証OK")
    if ch_info.get("channel_id"):
        print(f"アップロード先チャンネル: {ch_info.get('channel_title') or '?'} ({ch_info.get('channel_id')})")
    sys.stdout.flush()

    snippet = {
        "title": title,
        "description": description,
        "tags": tags,
        "categoryId": str(s["category_id"]),
        "defaultLanguage": s["default_language"],
        "defaultAudioLanguage": s["default_audio_language"],
    }
    status = {
        "privacyStatus": privacy,
        "selfDeclaredMadeForKids": bool(s["made_for_kids"]),
        "containsSyntheticMedia": bool(s["synthetic_media"]),
        "license": s["license"] if s["license"] in ("youtube", "creativeCommon") else "youtube",
        "embeddable": bool(s["embeddable"]),
        "publicStatsViewable": bool(s["public_stats_viewable"]),
    }
    body = {"snippet": snippet, "status": status}
    if localizations:
        body["localizations"] = localizations

    parts = ["snippet", "status"]
    if localizations:
        parts.append("localizations")

    # 予約公開は privacyStatus=private + publishAt が正しい使い方
    if schedule:
        body["status"]["privacyStatus"] = "private"
        body["status"]["publishAt"] = schedule

    # チャンクサイズ: 10MB（進捗を細かく表示）
    chunk_size = 10 * 1024 * 1024
    media = MediaFileUpload(
        str(video_file),
        mimetype="video/mp4",
        resumable=True,
        chunksize=chunk_size,
    )

    print(f"\nアップロード開始... ({file_size_mb:.0f} MB / チャンク {chunk_size // 1024 // 1024}MB)")
    sys.stdout.flush()

    import time
    start_time = time.time()

    request = youtube.videos().insert(
        part=",".join(parts),
        body=body,
        media_body=media,
        notifySubscribers=bool(s["notify_subscribers"]),
    )

    response = None
    try:
        while response is None:
            status_obj, response = request.next_chunk()
            if status_obj:
                pct = int(status_obj.progress() * 100)
                elapsed = time.time() - start_time
                uploaded_mb = status_obj.progress() * file_size_mb
                speed = uploaded_mb / elapsed if elapsed > 0 else 0
                remaining = (file_size_mb - uploaded_mb) / speed if speed > 0 else 0
                remaining_min = int(remaining // 60)
                remaining_sec = int(remaining % 60)
                print(f"  📤 {pct}% ({uploaded_mb:.0f}/{file_size_mb:.0f} MB) | {speed:.1f} MB/s | 残り約 {remaining_min}分{remaining_sec:02d}秒")
                sys.stdout.flush()
    except HttpError as e:
        # 403 quotaExceeded / 429 / 5xx は上位 retry 層に回す
        if _is_retryable_http_error(e):
            status = getattr(e.resp, "status", "?") if getattr(e, "resp", None) else "?"
            print(f"\n🔁 [RETRYABLE_UPLOAD_ERROR] HTTP {status} — pipeline retry layer へ委譲します", flush=True)
            sys.exit(EXIT_RETRYABLE)
        raise

    video_id = response["id"]
    total_time = time.time() - start_time
    print(f"\n✅ アップロード完了: https://youtu.be/{video_id}")
    print(f"  所要時間: {int(total_time//60)}分{int(total_time%60):02d}秒")
    sys.stdout.flush()

    # quota 消費を per-channel に記録（次回 upload 前のガードに使う）
    try:
        record_upload_quota(channel_folder, cost=QUOTA_PER_UPLOAD)
        used_after = quota_used_in_window(channel_folder)
        print(f"  📊 quota 消費記録: 累計 {used_after} unit / 24h")
    except Exception as e:
        print(f"  ⚠️ quota 記録失敗: {e}")

    if thumbnail:
        print(f"サムネイル設定中: {thumbnail.name}")
        sys.stdout.flush()
        thumb_mime = mimetypes.guess_type(str(thumbnail))[0] or "image/jpeg"
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail), mimetype=thumb_mime),
        ).execute()
        print("✅ サムネイル設定完了")
        sys.stdout.flush()

    # アップロード完了マーカーを書き出し（ダッシュボード用）
    try:
        import datetime
        marker = {
            "video_id": video_id,
            "url": f"https://youtu.be/{video_id}",
            "title": title,
            "privacy": privacy,
            "schedule": schedule,
            "uploaded_at": datetime.datetime.now().isoformat(),
            "settings": s,
            "localizations_applied": list(localizations.keys()),
        }
        (Path(folder) / "youtube_upload.json").write_text(
            json.dumps(marker, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        print(f"  ⚠️ マーカー書き出し失敗: {e}")
        sys.stdout.flush()

    return video_id


def publish_video_to_public(folder, token_override=None) -> dict:
    """すでにアップロード済みの動画を private → public に切り替える（公開ゲート用）。

    P2-7: pipeline は publish_delay_hours が設定されているチャンネルでは private で
    upload し、APScheduler が N 時間後にこの関数を呼ぶ。

    Args:
      folder: vol フォルダ。`youtube_upload.json` から video_id を読む。
      token_override: トークンファイルの明示パス（None なら通常解決）

    Returns:
      {"status": "ok|already_public|missing|error", "video_id": ..., "previous_privacy": ..., ...}
    """
    folder = Path(folder)
    marker = folder / "youtube_upload.json"
    if not marker.exists():
        return {"status": "missing", "error": f"youtube_upload.json が無い: {folder}"}
    try:
        upload = json.loads(marker.read_text(encoding="utf-8"))
    except Exception as e:
        return {"status": "error", "error": f"marker 読込失敗: {e}"}
    video_id = upload.get("video_id")
    if not video_id:
        return {"status": "error", "error": "video_id が marker に無い"}
    creds = get_credentials(video_folder=folder, token_override=token_override)
    youtube = build("youtube", "v3", credentials=creds)
    # 現在の privacyStatus を確認（既に public なら no-op）
    try:
        cur = youtube.videos().list(part="status", id=video_id).execute()
        items = cur.get("items", [])
        if not items:
            return {"status": "error", "error": f"video_id {video_id} が見つからない（削除済 or 別アカウント）"}
        prev_priv = items[0].get("status", {}).get("privacyStatus", "")
        if prev_priv == "public":
            # marker も更新して冪等化
            upload["published_at"] = datetime.datetime.now().isoformat()
            upload["privacy"] = "public"
            marker.write_text(json.dumps(upload, ensure_ascii=False, indent=2), encoding="utf-8")
            return {"status": "already_public", "video_id": video_id, "previous_privacy": prev_priv}
    except HttpError as e:
        if _is_retryable_http_error(e):
            return {"status": "retryable", "error": f"HttpError {e}"}
        return {"status": "error", "error": f"HttpError {e}"}
    # public へ切替
    try:
        body = {"id": video_id, "status": {"privacyStatus": "public"}}
        resp = youtube.videos().update(part="status", body=body).execute()
        new_priv = resp.get("status", {}).get("privacyStatus", "")
    except HttpError as e:
        if _is_retryable_http_error(e):
            return {"status": "retryable", "error": f"HttpError {e}"}
        return {"status": "error", "error": f"HttpError {e}"}
    upload["published_at"] = datetime.datetime.now().isoformat()
    upload["privacy"] = new_priv or "public"
    marker.write_text(json.dumps(upload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "status": "ok",
        "video_id": video_id,
        "previous_privacy": prev_priv,
        "new_privacy": new_priv or "public",
    }


def _parse_tristate(value):
    """CLI 用: "true"/"false"/"" → True/False/None"""
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    return None  # 空文字はデフォルトに従う


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="orzz. YouTube アップローダー")
    parser.add_argument("folder", nargs="?", help="動画フォルダのパス")
    parser.add_argument("--title", "-t", help="動画タイトル")
    parser.add_argument("--schedule", "-s", help="公開予約 (ISO 8601: 2026-04-15T09:00:00Z)")
    parser.add_argument("--privacy", "-p", default="private",
                        choices=["private", "unlisted", "public"])
    parser.add_argument("--tags", help="カンマ区切りのタグ（省略時は youtube_tags.txt を使用）")
    parser.add_argument("--auth-only", action="store_true", help="認証のみ実行")
    parser.add_argument("--video-path", help="アップロードする mp4 のフルパス（外部 SSD 用）。指定無しなら folder 内を検索")
    # 詳細設定（None なら defaults → override の順で解決）
    parser.add_argument("--category-id", help="動画カテゴリID（例: 10=Music）")
    parser.add_argument("--default-language", help="タイトル/説明の言語 (例: ja, en)")
    parser.add_argument("--default-audio-language", help="音声言語 (例: ja, en)")
    parser.add_argument("--made-for-kids", help="子供向け (true/false)")
    parser.add_argument("--synthetic-media", help="AI 生成・改変コンテンツ開示 (true/false)")
    parser.add_argument("--license", choices=["youtube", "creativeCommon"], help="ライセンス")
    parser.add_argument("--embeddable", help="埋め込み許可 (true/false)")
    parser.add_argument("--public-stats-viewable", help="統計の公開 (true/false)")
    parser.add_argument("--notify-subscribers", help="登録者へ通知 (true/false)")
    parser.add_argument("--no-localizations", action="store_true",
                        help="youtube_localizations.json があっても適用しない")
    parser.add_argument("--token-file", help="OAuth トークンファイルの明示パス。"
                        "未指定なら <video_folder>/../.youtube_token.json（チャンネル別）→ "
                        "グローバル ~/.config/{app_id}/youtube_token.json の順で解決")
    parser.add_argument("--expected-channel-id", help="OAuth トークンがこの YouTube channelId と一致しない場合はアップロードしない")
    parser.add_argument("--expected-channel-name", help="エラー表示用のチャンネル名")
    args = parser.parse_args()

    if args.auth_only:
        # auth-only でも folder があればチャンネル別トークンを使う
        get_credentials(video_folder=args.folder, token_override=args.token_file)
        print("認証完了。トークンを保存しました。")
        sys.exit(0)

    if not args.folder:
        parser.print_help()
        sys.exit(1)

    cli_tags = None
    if args.tags:
        cli_tags = [t.strip() for t in args.tags.split(",") if t.strip()]
    upload_video(
        args.folder, title=args.title, schedule=args.schedule,
        privacy=args.privacy, tags=cli_tags, video_path=args.video_path,
        category_id=args.category_id,
        default_language=args.default_language,
        default_audio_language=args.default_audio_language,
        made_for_kids=_parse_tristate(args.made_for_kids),
        synthetic_media=_parse_tristate(args.synthetic_media),
        license_type=args.license,
        embeddable=_parse_tristate(args.embeddable),
        public_stats_viewable=_parse_tristate(args.public_stats_viewable),
        notify_subscribers=_parse_tristate(args.notify_subscribers),
        use_localizations=not args.no_localizations,
        token_file=args.token_file,
        expected_channel_id=args.expected_channel_id,
        expected_channel_name=args.expected_channel_name,
    )
