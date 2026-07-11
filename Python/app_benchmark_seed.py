#!/usr/bin/env python3
"""ベンチマーク seed 動画分析。

伸びた 1 本の動画を分析単位にして、突出度の正規化指標はコード側で計算し、
LLM には用途・仮説・安全判定の整理だけを任せる。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import re
import shutil
import statistics
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from _app_config import resolve_config_dir, resolve_shared_config_dir
    CONFIG_DIR = resolve_config_dir()
    SHARED_CONFIG_DIR = resolve_shared_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
    SHARED_CONFIG_DIR = CONFIG_DIR

import app_benchmark_channels as _bench
from app_benchmark_common import extract_json_object

BENCHMARK_DIR = SHARED_CONFIG_DIR / "benchmark"
SEED_ANALYSES_FILE = BENCHMARK_DIR / "seed_analyses.json"
AUDIO_DIR = BENCHMARK_DIR / "audio"


STANDARD_PROMPT = """あなたはAI BGM/作業用BGMチャンネルのベンチマーク分析担当です。

目的は、ベンチマークをコピーすることではありません。
伸びている1本の動画から、視聴者が反応した抽象パターンを抽出し、自分のチャンネルに安全に応用することです。

## 出力言語ルール（最優先）
JSON 内の説明・分析・提案・理由はすべて自然な日本語で書いてください。
検索語、ジャンル名、楽器名などで英語表記が必要な場合だけ、短い英語語句を日本語説明の中に含めても構いません。
英語だけの文章、英語だけの提案、英語だけの理由は禁止です。

## 分析にあたっての思考規律
- すべての主張に確度ラベルを付ける: [観察] データとして見えるもの / [推定] 観察から導いたもの(根拠を添える) / [憶測] 裏付けのない仮説。
- 「伸びた要因」を挙げる前に、代替説明(投稿タイミング、既存登録者、外部流入、ジャンル全体の波、単なる運)を検討し、潰せないものは要因扱いしない。
- 再生数は生の値で比較しない。チャンネル中央値との比率、公開経過日数あたり、登録者数との倍率で見る。
- 伸び方が初速型(サムネ・タイトル勝ち)か長期型(検索・関連勝ち)かを推定し、ファネル(インプレッション→CTR→維持→再訪)のどこで勝った動画かを明示する。
- 迷った要素は「真似てはいけない固有要素」側に倒す。
- 対照データが弱い場合、要因の confidence は speculative 側へ下げる。
- 見えていない CTR、維持率、流入内訳、インプレッション数を補完して断定しない。

以下の観点で分析してください。

1. seed動画候補
- チャンネル全体ではなく、この1本が seed になる理由を説明する。
- 突出度は観察データの正規化指標を根拠にする。

2. 視聴者用途
- 視聴者がどんな場面で使う動画かを推定する。
- 作業、勉強、睡眠、リラックス、カフェ、季節感、旅行気分などの観点で見る。

3. クリック前の約束
- サムネとタイトルが、視聴者にどんな気分・用途・音楽を約束しているかを分解する。

4. サムネ分析
- 主役、構図、色、光、余白、文字、スマホ視認性、チャンネル一覧での統一感を見る。

5. タイトル分析
- 用途語、感情語、場所語、時間語、言語圏への自然さを見る。

6. 音楽/映像一致
- タイトル、サムネ、音楽が同じ視聴シーンを約束しているかを見る。

7. 横展開案
- 伸びた要素を1つだけ変える派生案を3つ出す。

8. PDCA仮説
- 次の1投稿で検証する要素を1つだけ選ぶ(複数同時に変えない)。
- 効果量と検証しやすさで選定理由を説明する。
- 成功判断に見るべき数字・反応と、判定する期限を書く。

9. 安全性
- 真似てよい抽象要素と、真似てはいけない固有要素を分ける。
- 著作権、商標、既存アーティスト名、固有キャラ、再利用コンテンツのリスクを指摘する。

10. 分析の限界
- この分析で見えていないデータ(CTR、維持率、流入内訳など)を列挙する。
- そのデータが取れた場合、どの結論が変わりうるかを書く。

次の単一 JSON オブジェクトだけで返してください。前置き・コードフェンスは禁止です。
{
  "seed_video_url": "",
  "seed_video_reason": "",
  "outlier_evidence": {
    "vs_channel_median": {"value": null, "description": ""},
    "views_per_day": {"value": null, "description": ""},
    "vs_subscriber_scale": {"value": null, "description": ""}
  },
  "growth_pattern": "initial_spike | long_tail | step_change | unknown",
  "funnel_win_stage": "ctr | retention | repeat | unknown",
  "viewer_use_case": "",
  "viewer_emotion": "",
  "click_promise": "",
  "thumbnail_factors": [
    { "factor": "", "confidence": "observed | inferred | speculative", "evidence": "" }
  ],
  "title_factors": [
    { "factor": "", "confidence": "observed | inferred | speculative", "evidence": "" }
  ],
  "alternative_explanations": [
    { "explanation": "", "ruled_out": false, "how_checked": "" }
  ],
  "music_visual_fit": "",
  "safe_to_borrow": [],
  "do_not_copy": [],
  "variant_ideas": [],
  "pdca_hypothesis": {
    "changed_element": "",
    "expected_effect": "",
    "success_metrics": [],
    "judge_by": ""
  },
  "risk_notes": [],
  "analysis_limits": [],
  "additional_data_needed": []
}
"""


MUSIC_PROFILE_PROMPT = """あなたはAI BGM/作業用BGMチャンネルの音源分析担当です。

YouTube seed 動画の音声を実聴し、SUNO プロンプトへ安全に反映できる抽象的な音楽特徴だけを整理してください。

## 規律
- BPM は推定値です。必ず confidence を observed / inferred / speculative のいずれかで付けてください。
- 特定の既存曲・アーティストの旋律を特定・再現する指示は書かないでください。
- 実在するアーティスト名、バンド名、作曲家名、プロデューサー名、レーベル名、実在曲名・アルバム名は出さないでください。
- 抽象特徴（テンポ感・楽器編成・音数の密度・ミックス質感・展開）だけを記述してください。
- 聴き取れない点や自信がない点は analysis_limits に入れてください。

次の単一 JSON オブジェクトだけで返してください。前置き・コードフェンスは禁止です。
{
  "bpm_estimate": {"value": 0, "confidence": "observed | inferred | speculative"},
  "tempo_feel": "",
  "key_impression": "",
  "instruments": [],
  "structure": "イントロ/ループ構成、セクションの長さ感、曲間の入り方",
  "loop_feel": "ループや曲の切れ目の自然さ・違和感",
  "density": "音数の密度(作業を邪魔しないか)",
  "mix_texture": "ミックスの質感(音圧、高音の刺さり、低音の丸さ、リバーブ感)",
  "dynamics": "展開の起伏(急なビルドアップの有無)",
  "mood": [],
  "vocal_presence": "none | humming | vocal",
  "suno_style_hints": ["SUNOのstyles欄に使える英語の短い語句を5-8個"],
  "avoid_in_suno": ["SUNOで再現時に避けるべき要素"],
  "analysis_limits": ["聴き取れなかった・自信がない点"]
}
"""


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _gemini_api_key() -> str:
    """Gemini API key を環境変数・dashboard_config・テキストファイルの順に解決する。"""
    key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if key:
        return key
    cfg = _read_json(CONFIG_DIR / "dashboard_config.json", {})
    key = (cfg.get("gemini_api_key") or "").strip() if isinstance(cfg, dict) else ""
    if key:
        return key
    fp = CONFIG_DIR / "gemini_api_key.txt"
    return fp.read_text(encoding="utf-8").strip() if fp.exists() else ""


def _gemini_model() -> str:
    """dashboard_config.gemini_model があれば採用し、無ければ既定モデルを返す。"""
    cfg = _read_json(CONFIG_DIR / "dashboard_config.json", {})
    model = (cfg.get("gemini_model") or "").strip() if isinstance(cfg, dict) else ""
    return model or "gemini-2.5-flash"


def _yt_dlp_bin() -> str:
    """yt-dlp の実行ファイルを探す。

    スタンドアロン版（~/.local/bin）を最優先。pip の Python3.9 版は
    2025.10 で更新が止まっており YouTube 側の SABR 変更で 403 になるため最後に回す。
    """
    standalone = Path.home() / ".local/bin/yt-dlp"
    if standalone.exists():
        return str(standalone)
    found = shutil.which("yt-dlp")
    if found:
        return found
    fallback = Path.home() / "Library/Python/3.9/bin/yt-dlp"
    if fallback.exists():
        return str(fallback)
    raise RuntimeError("yt-dlp が見つかりません。https://github.com/yt-dlp/yt-dlp/releases の yt-dlp_macos を ~/.local/bin/yt-dlp に配置してください")


def _agy_bin() -> str | None:
    """Antigravity CLI を任意依存として解決する。"""
    preferred = Path.home() / ".local/bin/agy"
    if preferred.exists():
        return str(preferred)
    return shutil.which("agy")


def _coerce_int(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except Exception:
        return 0


def _parse_dt(value: Any) -> _dt.datetime | None:
    if not value:
        return None
    try:
        s = str(value)
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        d = _dt.datetime.fromisoformat(s)
        return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
    except Exception:
        return None


def _elapsed_days(published_at: Any) -> int:
    d = _parse_dt(published_at)
    if not d:
        return 1
    return max(1, (_dt.datetime.now(_dt.timezone.utc) - d.astimezone(_dt.timezone.utc)).days)


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def resolve_video(url_or_id: str) -> str:
    """YouTube URL または videoId を videoId に正規化する。"""
    raw = (url_or_id or "").strip()
    if not raw:
        raise RuntimeError("動画 URL / videoId が空です")
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", raw):
        return raw
    parsed = urllib.parse.urlparse(raw)
    host = parsed.netloc.lower()
    if "youtu.be" in host:
        vid = parsed.path.strip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
            return vid
    if "youtube.com" in host:
        qs = urllib.parse.parse_qs(parsed.query)
        vid = (qs.get("v") or [""])[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", vid or ""):
            return vid
        m = re.search(r"/shorts/([A-Za-z0-9_-]{11})", parsed.path)
        if m:
            return m.group(1)
        m = re.search(r"/embed/([A-Za-z0-9_-]{11})", parsed.path)
        if m:
            return m.group(1)
    raise RuntimeError(f"videoId を解決できません: {url_or_id}")


def download_seed_audio(url_or_id: str, force: bool = False) -> Path:
    """検証用の私的分析目的で seed 動画の音声を mp3 化して保存する。"""
    video_id = resolve_video(url_or_id)
    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    out_path = AUDIO_DIR / f"{video_id}.mp3"
    if out_path.exists() and not force:
        print(f"既存 mp3 を使用: {out_path}")
        return out_path

    yt_dlp = _yt_dlp_bin()
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    out_tpl = str(AUDIO_DIR / f"{video_id}.%(ext)s")
    cmd = [
        yt_dlp,
        "-x",
        "--audio-format", "mp3",
        "--audio-quality", "0",
        "--no-playlist",
        "-o", out_tpl,
        video_url,
    ]
    print(f"yt-dlp で seed 音源を取得中: {video_id}")
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=900)
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("yt-dlp がタイムアウトしました") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "")[:300]
        raise RuntimeError(f"yt-dlp 音源取得に失敗しました: {err}")
    if not out_path.exists():
        candidates = sorted(AUDIO_DIR.glob(f"{video_id}*.mp3"))
        if candidates:
            return candidates[0]
        raise RuntimeError(f"mp3 が見つかりません: {out_path}")
    print(f"  ✓ seed 音源取得完了: {out_path}")
    return out_path


def dsp_music_profile(mp3_path: str | Path) -> dict[str, Any]:
    """ローカル mp3 から BPM・キー感・密度・音圧・明るさを実測する。"""
    import warnings

    try:
        import librosa
        import numpy as np
    except ImportError as e:
        raise RuntimeError("librosa が見つかりません。pip3 install librosa を実行してください") from e

    warnings.filterwarnings("ignore")
    path = Path(mp3_path).expanduser()
    if not path.exists():
        raise RuntimeError(f"mp3 が見つかりません: {path}")

    y, sr = librosa.load(str(path), duration=180, sr=22050)
    if y is None or len(y) == 0:
        raise RuntimeError(f"音声データを読み込めません: {path}")

    tempo, _beats = librosa.beat.beat_track(y=y, sr=sr)
    tempo_value = float(np.asarray(tempo).reshape(-1)[0]) if np.asarray(tempo).size else 0.0
    onset_env = librosa.onset.onset_strength(y=y, sr=sr)
    rms = librosa.feature.rms(y=y)[0]
    centroid = librosa.feature.spectral_centroid(y=y, sr=sr)[0]
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
    key_names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
    key_idx = int(np.argmax(np.mean(chroma, axis=1))) if chroma.size else 0
    try:
        duration_sec = float(librosa.get_duration(path=str(path)))
    except Exception:
        duration_sec = float(librosa.get_duration(y=y, sr=sr))

    onset_mean = float(np.mean(onset_env)) if onset_env.size else 0.0
    rms_mean = float(np.mean(rms)) if rms.size else 0.0
    rms_std = float(np.std(rms)) if rms.size else 0.0
    centroid_mean = float(np.mean(centroid)) if centroid.size else 0.0

    return {
        "bpm_estimate": {
            "value": round(tempo_value, 1),
            "confidence": "observed",
            "description": "librosa.beat.beat_track による冒頭3分のテンポ実測",
        },
        "key_impression": {
            "value": key_names[key_idx],
            "confidence": "observed",
            "description": "chroma_cqt の平均ピークによるキー感。長短調の断定ではありません",
        },
        "onset_density": {
            "value": round(onset_mean, 4),
            "confidence": "observed",
            "description": "onset_strength 平均。大きいほど発音イベントが多い傾向",
        },
        "rms_mean": {
            "value": round(rms_mean, 6),
            "confidence": "observed",
            "description": "RMS 平均。音圧の目安",
        },
        "rms_std": {
            "value": round(rms_std, 6),
            "confidence": "observed",
            "description": "RMS 標準偏差。音量起伏の目安",
        },
        "spectral_centroid_mean_hz": {
            "value": round(centroid_mean, 1),
            "confidence": "observed",
            "description": "spectral centroid 平均Hz。高いほど明るい質感になりやすい",
        },
        "duration_sec": {
            "value": round(duration_sec, 1),
            "confidence": "observed",
            "description": "ファイル全体の長さ",
        },
    }


def _video_id(v: dict[str, Any]) -> str:
    return (v.get("videoId") or v.get("video_id") or v.get("id") or "").strip()


def _normalize_video(v: dict[str, Any], channel: dict[str, Any] | None = None) -> dict[str, Any]:
    cid = (channel or {}).get("channel_id") or (channel or {}).get("channelId") or v.get("channelId") or ""
    cname = (channel or {}).get("channel_name") or (channel or {}).get("channelName") or v.get("channelTitle") or ""
    vid = _video_id(v)
    return {
        "videoId": vid,
        "video_id": vid,
        "title": v.get("title") or "",
        "description": v.get("description") or "",
        "tags": v.get("tags") or [],
        "publishedAt": v.get("publishedAt") or v.get("published_at") or v.get("published") or "",
        "published_at": v.get("publishedAt") or v.get("published_at") or v.get("published") or "",
        "viewCount": _coerce_int(v.get("viewCount") or v.get("views")),
        "views": _coerce_int(v.get("viewCount") or v.get("views")),
        "likeCount": _coerce_int(v.get("likeCount") or v.get("likes")),
        "commentCount": _coerce_int(v.get("commentCount") or v.get("comments_count")),
        "duration": v.get("duration") or "",
        "thumbnail": v.get("thumbnail") or "",
        "url": v.get("url") or (f"https://www.youtube.com/watch?v={vid}" if vid else ""),
        "channelId": cid,
        "channelTitle": cname,
    }


def _channel_videos(channel: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for key in ("videos", "topByViews", "recentUploads", "top_videos", "recent_videos"):
        for v in channel.get(key) or []:
            nv = _normalize_video(v, channel)
            key_id = nv.get("videoId") or nv.get("title")
            if key_id and key_id not in seen:
                rows.append(nv)
                seen.add(key_id)
    return rows


def _find_registered_video(video_id: str) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]] | None:
    channels = _bench.list_channels().get("channels") or []
    for row in channels:
        channel = _bench.get_channel(row.get("channel_id") or "")
        if not channel:
            continue
        videos = _channel_videos(channel)
        for v in videos:
            if _video_id(v) == video_id:
                return _normalize_video(v, channel), channel, videos
    return None


def _require_api_key_for_seed_fetch() -> None:
    try:
        key = _bench._api_key()
    except Exception:
        key = ""
    if not key:
        raise RuntimeError(
            "登録済みベンチマーク内に対象動画がなく、YouTube API key も未設定です。"
            "設定画面の youtube_api_key / 環境変数 YOUTUBE_API_KEY / youtube_api_key.txt のいずれかを設定してください。"
        )


def _thumb(snip: dict[str, Any]) -> str:
    thumbs = snip.get("thumbnails") or {}
    for key in ("maxres", "standard", "high", "medium", "default"):
        url = (thumbs.get(key) or {}).get("url")
        if url:
            return url
    return ""


def _video_from_api_item(item: dict[str, Any]) -> dict[str, Any]:
    sn = item.get("snippet") or {}
    st = item.get("statistics") or {}
    vid = item.get("id") or ""
    return {
        "videoId": vid,
        "video_id": vid,
        "title": sn.get("title") or "",
        "description": sn.get("description") or "",
        "tags": sn.get("tags") or [],
        "publishedAt": sn.get("publishedAt") or sn.get("publishTime") or "",
        "published_at": sn.get("publishedAt") or sn.get("publishTime") or "",
        "viewCount": _coerce_int(st.get("viewCount")),
        "views": _coerce_int(st.get("viewCount")),
        "likeCount": _coerce_int(st.get("likeCount")),
        "commentCount": _coerce_int(st.get("commentCount")),
        "duration": (item.get("contentDetails") or {}).get("duration") or "",
        "thumbnail": _thumb(sn),
        "url": f"https://www.youtube.com/watch?v={vid}",
        "channelId": sn.get("channelId") or "",
        "channelTitle": sn.get("channelTitle") or "",
    }


def _fetch_channel_videos_from_uploads(channel_id: str, uploads: str, limit: int = 30) -> list[dict[str, Any]]:
    if not uploads:
        return []
    ids: list[str] = []
    page = ""
    while len(ids) < limit:
        params: dict[str, Any] = {"part": "contentDetails", "playlistId": uploads, "maxResults": min(50, limit - len(ids))}
        if page:
            params["pageToken"] = page
        pl = _bench._yt_get("playlistItems", params, "playlistItems.list", channel_id=channel_id)
        for row in pl.get("items") or []:
            vid = ((row.get("contentDetails") or {}).get("videoId") or "").strip()
            if vid:
                ids.append(vid)
        page = pl.get("nextPageToken") or ""
        if not page:
            break
    videos: list[dict[str, Any]] = []
    for i in range(0, len(ids), 50):
        chunk = ids[i:i + 50]
        if not chunk:
            continue
        data = _bench._yt_get(
            "videos",
            {"part": "snippet,statistics,contentDetails", "id": ",".join(chunk)},
            "videos.list",
            channel_id=channel_id,
        )
        videos.extend(_video_from_api_item(v) for v in data.get("items") or [])
    return videos


def gather_video_context(video_id: str) -> dict[str, Any]:
    """対象動画と同チャンネル対照データを収集する。"""
    registered = _find_registered_video(video_id)
    if registered:
        video, channel, videos = registered
        return {
            "source": "registered_benchmark_cache",
            "video": video,
            "channel": channel,
            "channel_videos": videos,
            "subscribers": _coerce_int(channel.get("subscribers")),
        }

    _require_api_key_for_seed_fetch()
    print("登録済みベンチマークに無いため YouTube API で動画情報を取得します...")
    v_resp = _bench._yt_get(
        "videos",
        {"part": "snippet,statistics,contentDetails", "id": video_id, "maxResults": 1},
        "videos.list",
        channel_id="",
    )
    item = (v_resp.get("items") or [None])[0]
    if not item:
        raise RuntimeError(f"動画情報を取得できません: {video_id}")
    video = _video_from_api_item(item)
    channel_id = video.get("channelId") or ""

    ch_resp = _bench._yt_get(
        "channels",
        {"part": "snippet,statistics,contentDetails", "id": channel_id, "maxResults": 1},
        "channels.list",
        channel_id=channel_id,
    )
    ch_item = (ch_resp.get("items") or [None])[0] or {}
    sn = ch_item.get("snippet") or {}
    st = ch_item.get("statistics") or {}
    uploads = ((ch_item.get("contentDetails") or {}).get("relatedPlaylists") or {}).get("uploads") or ""
    channel = {
        "channel_id": channel_id,
        "channelId": channel_id,
        "channel_name": sn.get("title") or video.get("channelTitle") or "",
        "channelName": sn.get("title") or video.get("channelTitle") or "",
        "url": f"https://www.youtube.com/channel/{channel_id}",
        "subscribers": _coerce_int(st.get("subscriberCount")),
        "total_views": _coerce_int(st.get("viewCount")),
        "video_count": _coerce_int(st.get("videoCount")),
        "uploads_playlist_id": uploads,
    }
    channel_videos = _fetch_channel_videos_from_uploads(channel_id, uploads, limit=30)
    if not any(_video_id(v) == video_id for v in channel_videos):
        channel_videos.append(video)
    return {
        "source": "youtube_api",
        "video": video,
        "channel": channel,
        "channel_videos": channel_videos,
        "subscribers": _coerce_int(st.get("subscriberCount")),
    }


def compute_outlier_evidence(video: dict[str, Any], channel_videos: list[dict[str, Any]], subscribers: int) -> dict[str, Any]:
    """突出度の正規化指標をコードで計算する。"""
    views = _coerce_int(video.get("viewCount") or video.get("views"))
    peer_views = [
        _coerce_int(v.get("viewCount") or v.get("views"))
        for v in channel_videos
        if _coerce_int(v.get("viewCount") or v.get("views")) > 0
    ]
    median_views = statistics.median(peer_views) if peer_views else None
    elapsed = _elapsed_days(video.get("publishedAt") or video.get("published_at"))
    vs_channel_median = (views / median_views) if median_views else None
    views_per_day = views / max(1, elapsed)
    vs_subscriber_scale = views / max(1, _coerce_int(subscribers))
    return {
        "vs_channel_median": {
            "value": _round(vs_channel_median),
            "description": (
                f"対象動画 {views:,} 回 ÷ チャンネル動画中央値 {int(median_views):,} 回"
                if median_views else "対照動画の再生数が不足しているため算出不可"
            ),
        },
        "views_per_day": {
            "value": _round(views_per_day),
            "description": f"対象動画 {views:,} 回 ÷ 公開から {elapsed} 日",
        },
        "vs_subscriber_scale": {
            "value": _round(vs_subscriber_scale),
            "description": f"対象動画 {views:,} 回 ÷ 登録者 {max(1, _coerce_int(subscribers)):,} 人",
        },
    }


def _context_for_prompt(ctx: dict[str, Any], outlier: dict[str, Any], context_hint: str) -> str:
    video = ctx.get("video") or {}
    channel = ctx.get("channel") or {}
    rows = []
    for v in sorted(ctx.get("channel_videos") or [], key=lambda x: _coerce_int(x.get("viewCount") or x.get("views")), reverse=True)[:30]:
        rows.append({
            "videoId": _video_id(v),
            "title": v.get("title") or "",
            "views": _coerce_int(v.get("viewCount") or v.get("views")),
            "published_at": v.get("publishedAt") or v.get("published_at") or "",
        })
    observation = {
        "data_source": ctx.get("source") or "",
        "target_video": {
            "videoId": _video_id(video),
            "url": video.get("url") or "",
            "title": video.get("title") or "",
            "views": _coerce_int(video.get("viewCount") or video.get("views")),
            "likes": _coerce_int(video.get("likeCount")),
            "comments": _coerce_int(video.get("commentCount")),
            "published_at": video.get("publishedAt") or video.get("published_at") or "",
            "elapsed_days": _elapsed_days(video.get("publishedAt") or video.get("published_at")),
            "duration": video.get("duration") or "",
            "tags": video.get("tags") or [],
            "thumbnail": video.get("thumbnail") or "",
        },
        "channel": {
            "channel_id": channel.get("channel_id") or channel.get("channelId") or video.get("channelId") or "",
            "channel_name": channel.get("channel_name") or channel.get("channelName") or video.get("channelTitle") or "",
            "subscribers": _coerce_int(ctx.get("subscribers") or channel.get("subscribers")),
            "total_views": _coerce_int(channel.get("total_views")),
            "video_count": _coerce_int(channel.get("video_count")),
        },
        "computed_outlier_evidence": outlier,
        "same_channel_video_controls": rows,
        "context_hint": context_hint or "",
    }
    return json.dumps(observation, ensure_ascii=False, indent=2)


def _load_store() -> dict[str, Any]:
    data = _read_json(SEED_ANALYSES_FILE, {"analyses": {}, "updated_at": ""})
    if not isinstance(data, dict):
        data = {"analyses": {}, "updated_at": ""}
    if not isinstance(data.get("analyses"), dict):
        data["analyses"] = {}
    return data


def _save_analysis(video_id: str, analysis: dict[str, Any]) -> None:
    data = _load_store()
    data.setdefault("analyses", {})[video_id] = analysis
    data["updated_at"] = _now_iso()
    _write_json(SEED_ANALYSES_FILE, data)


def analyze_seed_video(url_or_id: str, context_hint: str = "", cli_cmd: str = "claude") -> dict[str, Any]:
    """seed 動画を分析し、結果を保存して返す。"""
    video_id = resolve_video(url_or_id)
    print(f"seed 動画を解決: {video_id}")
    ctx = gather_video_context(video_id)
    video = ctx.get("video") or {}
    channel = ctx.get("channel") or {}
    outlier = compute_outlier_evidence(video, ctx.get("channel_videos") or [], _coerce_int(ctx.get("subscribers")))
    prompt = f"""{STANDARD_PROMPT}

=== 観察データ ===
{_context_for_prompt(ctx, outlier, context_hint)}

重要:
- outlier_evidence は上記の computed_outlier_evidence をそのまま反映してください。
- 似た構成なのに伸びていない動画が観察データに無い場合、代替説明と analysis_limits に明記してください。
- confidence は observed / inferred / speculative のいずれかだけを使ってください。
- growth_pattern は initial_spike / long_tail / step_change / unknown のいずれかだけを使ってください。
- funnel_win_stage は ctr / retention / repeat / unknown のいずれかだけを使ってください。
"""
    print(" Claude CLI で seed 動画分析中...")
    from app_llm_runner import run_llm
    out = run_llm(prompt, cli_cmd=cli_cmd, timeout=300, label="seed-analyze")
    obj = extract_json_object(out)
    if not obj:
        raise RuntimeError(f"JSON 抽出失敗: {out[:300]}")

    obj["outlier_evidence"] = outlier
    obj["seed_video_url"] = obj.get("seed_video_url") or video.get("url") or f"https://www.youtube.com/watch?v={video_id}"
    obj["analyzed_at"] = _now_iso()
    obj["video_id"] = video_id
    obj["video_title"] = video.get("title") or ""
    obj["channel_id"] = channel.get("channel_id") or channel.get("channelId") or video.get("channelId") or ""
    obj["channel_name"] = channel.get("channel_name") or channel.get("channelName") or video.get("channelTitle") or ""
    obj["source"] = ctx.get("source") or ""
    _save_analysis(video_id, obj)
    print("  ✓ seed 動画分析完了")
    return obj


def analyze_seed_audio(url_or_id: str, cli_cmd: str = "claude") -> dict[str, Any]:
    """Gemini で seed 動画の音源を実聴分析し、music_profile として保存する。"""
    video_id = resolve_video(url_or_id)
    video_url = f"https://www.youtube.com/watch?v={video_id}"
    obj = _analyze_seed_audio_gemini(video_id)

    data = _load_store()
    analyses = data.setdefault("analyses", {})
    existing = analyses.get(video_id)
    if not isinstance(existing, dict):
        existing = {
            "video_id": video_id,
            "seed_video_url": video_url,
        }
    existing["video_id"] = existing.get("video_id") or video_id
    existing["seed_video_url"] = existing.get("seed_video_url") or video_url
    prior = existing.get("music_profile")
    if isinstance(prior, dict) and isinstance(prior.get("dsp"), dict):
        # ローカル DSP 実測がある場合は消さず、Gemini 実聴を ai 層としてマージする
        prior["ai"] = obj
        prior["ai_source"] = "gemini_api"
        existing["music_profile"] = prior
    else:
        existing["music_profile"] = obj
        existing["music_profile"]["ai_source"] = "gemini_api"
    existing["music_profile_analyzed_at"] = _now_iso()
    analyses[video_id] = existing
    data["updated_at"] = _now_iso()
    _write_json(SEED_ANALYSES_FILE, data)
    print("  ✓ seed 音源分析完了")
    return obj


def ai_music_profile_from_file(mp3_path: str | Path) -> dict[str, Any] | None:
    """Antigravity CLI がローカル mp3 を実聴できた場合だけ音楽特徴を返す。"""
    agy = _agy_bin()
    if not agy:
        return None
    path = Path(mp3_path).expanduser()
    if not path.exists():
        print(f"  ⚠ AI 実聴対象 mp3 が見つかりません: {path}")
        return None
    prompt = f"""対象ファイル: {path.name}

このディレクトリに追加された対象 mp3 を、実際に音声処理できた場合のみ分析してください。
実際に音声を処理できた場合のみ、次の単一 JSON で回答してください:
{{
  "listened": true,
  "instruments": [],
  "structure": "",
  "loop_feel": "",
  "mix_texture": "",
  "dynamics": "",
  "mood": [],
  "vocal_presence": "none|humming|vocal",
  "suno_style_hints": []
}}

聴けない場合、または音声を処理できた確証がない場合は、次だけを返してください:
{{"listened": false}}

禁止事項:
- 外部ツールのインストール
- ファイルのダウンロード
- ネットワークアクセス
- ファイル名やメタデータからの推測
- 実在アーティスト名、実在曲名、既存曲の旋律再現指示

JSON オブジェクトのみを出力してください。"""
    cmd = [agy, "--add-dir", str(path.parent), "-p", prompt]
    print(f" Antigravity CLI でローカル音源の実聴を試行: {path.name}")
    try:
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=300)
    except subprocess.TimeoutExpired:
        print("  ⚠ Antigravity CLI がタイムアウトしたため AI 実聴をスキップ")
        return None
    except Exception as e:
        print(f"  ⚠ Antigravity CLI 実行失敗のため AI 実聴をスキップ: {e}")
        return None
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "")[:300]
        print(f"  ⚠ Antigravity CLI が失敗したため AI 実聴をスキップ: {msg}")
        return None
    obj = extract_json_object((proc.stdout or "") + "\n" + (proc.stderr or ""))
    if not obj or obj.get("listened") is not True:
        print("  ⚠ AI が実聴できたと確認できないため、AI 音源分析は採用しません")
        return None
    return obj


def _metric_value(dsp: dict[str, Any], key: str, default: Any = "") -> Any:
    value = dsp.get(key)
    if isinstance(value, dict):
        return value.get("value", default)
    return value if value is not None else default


def _seed_context_for_music(video_id: str) -> dict[str, str]:
    row = (_load_store().get("analyses") or {}).get(video_id) or {}
    return {
        "viewer_use_case": str(row.get("viewer_use_case") or ""),
        "click_promise": str(row.get("click_promise") or ""),
        "video_title": str(row.get("video_title") or ""),
        "channel_name": str(row.get("channel_name") or ""),
    }


def _local_style_hints_from_dsp(dsp: dict[str, Any]) -> dict[str, list[str]]:
    bpm = _metric_value(dsp, "bpm_estimate", 0)
    key = _metric_value(dsp, "key_impression", "")
    centroid = float(_metric_value(dsp, "spectral_centroid_mean_hz", 0) or 0)
    onset = float(_metric_value(dsp, "onset_density", 0) or 0)
    rms_std = float(_metric_value(dsp, "rms_std", 0) or 0)
    hints = [
        f"{bpm} BPM steady instrumental groove" if bpm else "steady instrumental groove",
        f"{key} key impression" if key else "clear tonal center",
        "moderate onset density" if onset >= 1.0 else "sparse gentle arrangement",
        "bright clean mix" if centroid >= 1800 else "warm rounded mix",
        "smooth low-dynamic background flow" if rms_std < 0.03 else "gentle dynamic movement",
    ]
    return {
        "suno_style_hints": [h for h in hints if h][:8],
        "avoid_in_suno": [
            "sudden build-ups",
            "harsh high frequencies",
            "artist or song imitation",
        ],
    }


def _suno_hints_from_dsp(video_id: str, dsp: dict[str, Any], cli_cmd: str = "claude") -> dict[str, list[str]]:
    ctx = _seed_context_for_music(video_id)
    prompt = f"""あなたは「ベンチマーク楽曲の実測データ」と「視聴者心理」を、SUNO AI が最も理解しやすい styles 記述へ翻訳する専門家です。

目的は既存曲のコピーではありません。実測された抽象特徴(テンポ・密度・質感)と、視聴者がこの音楽を使う場面・感情を、SUNO が確実に解釈できる英語短句に変換します。
実在アーティスト名、実在曲名、レーベル名、既存曲の旋律再現指示は禁止です。

=== DSP 実測値(ベンチマーク楽曲) ===
{json.dumps(dsp, ensure_ascii=False, indent=2)}

=== 視聴者心理(seed 分析) ===
viewer_use_case: {ctx.get('viewer_use_case') or '(none)'}
click_promise: {ctx.get('click_promise') or '(none)'}

=== テンポの判断(重要) ===
librosa の BPM 実測は倍テンポ誤検出(実際の体感の2倍)がよくあります。
実測 BPM が視聴シーン(例: 家事・睡眠・リラックス)に対して不自然に速い場合は半分の値を体感テンポとして採用し、tempo_note にその判断を書いてください。

=== styles の作り方(SUNO が理解しやすい形式) ===
- 各句は 2〜5 語の英語。文章・説明・日本語は禁止。カンマ区切りで並べたとき SUNO のスタイルタグとして機能する具体語だけ。
- 次の 5 スロットを必ずカバーする:
  1. ジャンルアンカー: 例 "celtic folk instrumental"
  2. テンポ: 採用した体感 BPM を明記 例 "gentle 65 BPM lilt"
  3. 楽器 2〜3 句: 実測の密度・明るさと矛盾しない編成 例 "tin whistle melody", "soft acoustic guitar"
  4. 感情スロット(視聴者心理→SUNOの感情語): viewer の感情ベネフィットを音の形容に翻訳 例 "uplifting yet calm", "fresh motivated mood"
  5. シーンスロット(視聴者心理→SUNOの情景語): 視聴シーンを音像の情景に翻訳 例 "sunny morning kitchen atmosphere", "spring cleaning energy"
- 密度・音圧の実測が低ければ "sparse arrangement" "soft dynamics" のような抑制語も足す。
- 抽象的なマーケ語("viral", "catchy", "high quality")は禁止。音として聞こえるものだけを書く。

次の単一 JSON だけを返してください:
{{
  "suno_style_hints": ["5スロットをカバーする英語短句を6-9個"],
  "avoid_in_suno": ["SUNOで再現時に避けるべき要素を3-6個(英語短句)"],
  "tempo_note": "採用BPMと倍テンポ判断の理由(日本語1文)"
}}
"""
    try:
        from app_llm_runner import run_llm
        out = run_llm(prompt, cli_cmd=cli_cmd, timeout=180, label="seed-audio-dsp-hints")
        obj = extract_json_object(out)
        if obj:
            hints = _compact_items(obj.get("suno_style_hints"), 9)
            avoid = _compact_items(obj.get("avoid_in_suno"), 6)
            if hints:
                result = {"suno_style_hints": hints, "avoid_in_suno": avoid}
                note = str(obj.get("tempo_note") or "").strip()
                if note:
                    result["tempo_note"] = note
                return result
    except Exception as e:
        print(f"  ⚠ DSP 由来 SUNO ヒント生成に失敗。ローカル規則にフォールバック: {e}")
    return _local_style_hints_from_dsp(dsp)


def _call_adjudication_engine(engine: str, prompt: str, timeout: int = 360) -> dict[str, Any] | None:
    """claude / codex のどちらか一方だけを呼び、JSON オブジェクトを返す。"""
    try:
        from app_llm_runner import _resolve_clis, _run_claude_cli, _run_codex_cli

        claude_cli, codex_cli = _resolve_clis(None)
        if engine == "claude":
            rc, out, err = _run_claude_cli(claude_cli, prompt, timeout)
        elif engine == "codex":
            rc, out, err = _run_codex_cli(codex_cli, prompt, timeout)
        else:
            raise RuntimeError(f"unknown engine: {engine}")
        if rc != 0 or not str(out or "").strip():
            msg = (err or out or "").strip().replace("\n", " ")[:240]
            raise RuntimeError(f"{engine} rc={rc}: {msg}")
        obj = extract_json_object(out)
        if not isinstance(obj, dict):
            raise RuntimeError(f"{engine} JSON 抽出失敗: {str(out)[:240]}")
        return obj
    except Exception as e:
        print(f"  ⚠ {engine} 調停に失敗: {e}")
        return None


def _normalize_adjudication_result(obj: dict[str, Any], engines: list[str]) -> dict[str, Any]:
    result = dict(obj or {})
    try:
        if result.get("bpm_final") not in ("", None):
            result["bpm_final"] = float(result.get("bpm_final"))
    except Exception:
        pass
    result["instruments_final"] = _compact_items(result.get("instruments_final"), 12)
    result["suno_style_hints"] = _compact_items(result.get("suno_style_hints"), 9)
    result["avoid_in_suno"] = _compact_items(result.get("avoid_in_suno"), 6)
    discrepancies = result.get("discrepancies")
    result["discrepancies"] = discrepancies if isinstance(discrepancies, list) else []
    result["engines"] = engines
    return result


def _adjudication_prompt(video_id: str, dsp: dict[str, Any], ai: dict[str, Any]) -> str:
    ctx = _seed_context_for_music(video_id)
    return f"""あなたはベンチマーク楽曲分析の調停者です。DSP実測(librosa)と Gemini実聴は、それぞれ得意・不得意があります:
- DSP: BPMは倍/半分テンポ誤検出があり得る。キー感は長短調を区別できない。楽器は識別できない。数値の再現性は高い。
- Gemini実聴: 楽器・質感・展開の言語化に強い。BPMの数値精度は低め(体感ベース)。

両者の結果を項目ごとに突き合わせ、矛盾点は根拠を挙げて議論し、最終値を確定してください。
特にBPM: DSP実測値、その1/2値、Gemini体感値の3候補から、楽器編成・密度・視聴シーン(家事/睡眠など)との整合で最も合理的な値を選ぶこと。

=== DSP 実測 ===
{json.dumps(dsp, ensure_ascii=False, indent=2)}

=== Gemini 実聴 ===
{json.dumps(ai, ensure_ascii=False, indent=2)}

=== 視聴者文脈 ===
viewer_use_case: {ctx.get('viewer_use_case') or '(none)'}
click_promise: {ctx.get('click_promise') or '(none)'}

次の単一JSONだけを返す:
{{
  "bpm_final": 0,
  "bpm_rationale": "3候補からの選定理由(日本語1-2文)",
  "key_final": "",
  "instruments_final": [],
  "density_final": "",
  "mix_texture_final": "",
  "dynamics_final": "",
  "discrepancies": [
    {{"topic": "", "dsp_view": "", "gemini_view": "", "verdict": "", "reason": ""}}
  ],
  "suno_style_hints": ["5スロット(ジャンル/テンポbpm_final明記/楽器2-3/感情=視聴者心理/シーン=視聴者心理)をカバーする英語短句6-9個。各句2-5語"],
  "avoid_in_suno": ["英語短句3-6個"]
}}
"""


def _adjudicate_music_profile(video_id: str, dsp: dict[str, Any], ai: dict[str, Any]) -> dict[str, Any] | None:
    """DSP 実測と Gemini 実聴を LLM で突き合わせ、最終音楽特徴を確定する。"""
    if not isinstance(dsp, dict) or not dsp or not isinstance(ai, dict) or not ai:
        return None

    prompt = _adjudication_prompt(video_id, dsp, ai)
    engine_order = ["claude", "codex"]
    first: dict[str, Any] | None = None
    used: list[str] = []
    for engine in engine_order:
        first = _call_adjudication_engine(engine, prompt)
        if first:
            used.append(engine)
            break
    if not first:
        print("  ⚠ DSP×Gemini 調停は全エンジン失敗のためスキップ")
        return None

    other = "codex" if used[0] == "claude" else "claude"
    review_prompt = f"""あなたはベンチマーク楽曲分析の調停レビュー担当です。

Pass 1 の結論と生データを照合し、誤りがあれば修正した最終JSONを、正しければ同じJSONを返してください。
前置き・コードフェンスは禁止です。

=== 生データと調停指示 ===
{prompt}

=== Pass 1 の結論 ===
{json.dumps(first, ensure_ascii=False, indent=2)}
"""
    second = _call_adjudication_engine(other, review_prompt)
    if second:
        used.append(other)
        return _normalize_adjudication_result(second, used)
    return _normalize_adjudication_result(first, used)


def _analyze_seed_audio_gemini(video_id: str) -> dict[str, Any]:
    """Gemini API で seed 音源を実聴分析し、保存せず music_profile JSON だけ返す。"""
    key = _gemini_api_key()
    if not key:
        raise RuntimeError("GEMINI_API_KEY / dashboard_config.gemini_api_key / gemini_api_key.txt のいずれかを設定してください")

    video_url = f"https://www.youtube.com/watch?v={video_id}"
    model = _gemini_model()
    api_url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{urllib.parse.quote(model, safe='-_.')}:generateContent?key={urllib.parse.quote(key, safe='')}"
    )
    body = {
        "contents": [
            {
                "parts": [
                    {
                        "file_data": {"file_uri": video_url},
                        "video_metadata": {"start_offset": "0s", "end_offset": "180s"},
                    },
                    {"text": MUSIC_PROFILE_PROMPT},
                ]
            }
        ]
    }
    req = urllib.request.Request(
        api_url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print(f"Gemini で seed 音源分析中: {video_id} ({model})")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        detail = ""
        try:
            if hasattr(e, "read"):
                detail = e.read().decode("utf-8", errors="replace")[:300]
        except Exception:
            detail = ""
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"Gemini 音源分析に失敗しました: {e}{suffix}") from e

    texts: list[str] = []
    for cand in resp.get("candidates") or []:
        content = cand.get("content") or {}
        for part in content.get("parts") or []:
            txt = part.get("text")
            if txt:
                texts.append(str(txt))
    raw = "\n".join(texts).strip()
    obj = extract_json_object(raw)
    if not obj:
        raise RuntimeError(f"Gemini 音源分析 JSON 抽出失敗: {raw[:300]}")
    return obj


def analyze_seed_audio_local(url_or_id: str, file_path: str | Path | None = None, cli_cmd: str = "claude") -> dict[str, Any]:
    """mp3 を取得/利用し、DSP + Gemini 実聴 + 任意調停で music_profile として保存する。"""
    video_id = resolve_video(url_or_id)
    mp3 = Path(file_path).expanduser() if file_path else AUDIO_DIR / f"{video_id}.mp3"
    if not mp3.exists():
        if file_path:
            raise RuntimeError(f"指定 mp3 が見つかりません: {mp3}")
        mp3 = download_seed_audio(video_id)

    print(f"ローカル mp3 を DSP 分析中: {mp3}")
    dsp = dsp_music_profile(mp3)
    ai = None
    if _gemini_api_key():
        try:
            ai = _analyze_seed_audio_gemini(video_id)
        except Exception as e:
            print(f"  ⚠ Gemini 実聴に失敗。DSP のみで続行: {e}")
    resolved = _adjudicate_music_profile(video_id, dsp, ai) if ai else None
    if resolved:
        hint_obj = {
            "suno_style_hints": _compact_items(resolved.get("suno_style_hints"), 9),
            "avoid_in_suno": _compact_items(resolved.get("avoid_in_suno"), 6),
            "tempo_note": str(resolved.get("bpm_rationale") or "").strip(),
        }
    else:
        hint_obj = _suno_hints_from_dsp(video_id, dsp, cli_cmd=cli_cmd)

    music_profile = {
        "source": "local_mp3",
        "file": str(mp3),
        "dsp": dsp,
        "ai": ai,
        "ai_source": "gemini_api" if ai else "",
        "resolved": resolved,
        "suno_style_hints": hint_obj.get("suno_style_hints") or [],
        "avoid_in_suno": hint_obj.get("avoid_in_suno") or [],
        "analyzed_at": _now_iso(),
    }
    if hint_obj.get("tempo_note"):
        music_profile["tempo_note"] = hint_obj.get("tempo_note")
    data = _load_store()
    analyses = data.setdefault("analyses", {})
    existing = analyses.get(video_id)
    if not isinstance(existing, dict):
        existing = {
            "video_id": video_id,
            "seed_video_url": f"https://www.youtube.com/watch?v={video_id}",
        }
    existing["video_id"] = existing.get("video_id") or video_id
    existing["seed_video_url"] = existing.get("seed_video_url") or f"https://www.youtube.com/watch?v={video_id}"
    existing["music_profile"] = music_profile
    existing["music_profile_analyzed_at"] = music_profile["analyzed_at"]
    analyses[video_id] = existing
    data["updated_at"] = _now_iso()
    _write_json(SEED_ANALYSES_FILE, data)
    print("  ✓ ローカル seed 音源分析完了")
    return music_profile


def list_seed_analyses() -> dict[str, Any]:
    data = _load_store()
    return {"status": "ok", "analyses": data.get("analyses") or {}, "updated_at": data.get("updated_at", "")}


def get_seed_analysis(video_id: str) -> dict[str, Any] | None:
    try:
        vid = resolve_video(video_id)
    except Exception:
        return None
    return (_load_store().get("analyses") or {}).get(vid)


def _compact_items(value: Any, limit: int) -> list[str]:
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()][:limit]
    if isinstance(value, str) and value.strip():
        return [value.strip()][:limit]
    return []


def seed_prompt_hint(max_seeds: int = 2) -> str:
    """LLM プロンプトへ注入する seed 動画分析の短い運用ブロックを返す。

    seed 不在や読み込み失敗で企画生成を止めないため、例外はすべて握る。
    """
    try:
        data = _load_store()
        analyses = data.get("analyses") or {}
        if isinstance(analyses, dict):
            rows = [v for v in analyses.values() if isinstance(v, dict)]
        elif isinstance(analyses, list):
            rows = [v for v in analyses if isinstance(v, dict)]
        else:
            rows = []
        if not rows:
            return ""
        rows.sort(key=lambda r: r.get("analyzed_at") or "", reverse=True)
        n = max(1, int(max_seeds or 2))
        out = [
            "以下は seed 動画分析。コピーではなく抽象パターンだけを応用する。",
            "次の1本では pdca_hypothesis.changed_element を1つだけ検証し、do_not_copy の要素は絶対に使わない。",
        ]
        for idx, r in enumerate(rows[:n], 1):
            hyp = r.get("pdca_hypothesis") or {}
            safe = _compact_items(r.get("safe_to_borrow"), 3)
            ng = _compact_items(r.get("do_not_copy"), 3)
            variants = _compact_items(r.get("variant_ideas"), 3)
            risks = _compact_items(r.get("risk_notes"), 2)
            out.extend([
                f"[seed {idx}] {r.get('video_title') or '(title unknown)'} / {r.get('channel_name') or '(channel unknown)'}",
                f"- viewer_use_case: {r.get('viewer_use_case') or ''}",
                f"- click_promise: {r.get('click_promise') or ''}",
                f"- safe_to_borrow: {', '.join(safe) if safe else ''}",
                f"- do_not_copy: {', '.join(ng) if ng else ''}",
                f"- variant_ideas: {', '.join(variants) if variants else ''}",
                f"- changed_element: {hyp.get('changed_element') or ''}",
                f"- expected_effect: {hyp.get('expected_effect') or ''}",
                f"- risk_notes: {', '.join(risks) if risks else ''}",
            ])
        return "\n".join(out).strip()
    except Exception:
        return ""


def seed_music_profile_hint(max_seeds: int = 1) -> str:
    """SUNO プロンプトへ注入する seed 音源分析の短い運用ブロックを返す。

    music_profile 不在や読み込み失敗で企画生成を止めないため、例外はすべて握る。
    """
    try:
        data = _load_store()
        analyses = data.get("analyses") or {}
        if isinstance(analyses, dict):
            rows = [v for v in analyses.values() if isinstance(v, dict) and isinstance(v.get("music_profile"), dict)]
        elif isinstance(analyses, list):
            rows = [v for v in analyses if isinstance(v, dict) and isinstance(v.get("music_profile"), dict)]
        else:
            rows = []
        if not rows:
            return ""
        rows.sort(key=lambda r: r.get("music_profile_analyzed_at") or r.get("analyzed_at") or "", reverse=True)
        n = max(1, int(max_seeds or 1))
        out = [
            "以下はベンチマーク seed 動画の音源分析。旋律のコピーではなく、テンポ感・楽器編成・密度・質感の抽象特徴だけを寄せる。",
        ]
        for idx, r in enumerate(rows[:n], 1):
            mp = r.get("music_profile") or {}
            resolved = mp.get("resolved") if isinstance(mp.get("resolved"), dict) else {}
            dsp = mp.get("dsp") or {}
            ai = mp.get("ai") or {}
            bpm_rationale = ""
            if resolved:
                bpm_text = str(resolved.get("bpm_final") or "")
                tempo_feel = f"resolved BPM {bpm_text}" if bpm_text else ""
                key_text = str(resolved.get("key_final") or "")
                instruments = _compact_items(resolved.get("instruments_final"), 8)
                density_text = str(resolved.get("density_final") or "")
                rms_text = ""
                centroid_text = ""
                mix_texture = str(resolved.get("mix_texture_final") or "")
                dynamics = str(resolved.get("dynamics_final") or "")
                bpm_rationale = str(resolved.get("bpm_rationale") or "").strip()
            elif isinstance(dsp, dict) and dsp:
                bpm = dsp.get("bpm_estimate") or {}
                key = dsp.get("key_impression") or {}
                onset = dsp.get("onset_density") or {}
                rms_mean = dsp.get("rms_mean") or {}
                rms_std = dsp.get("rms_std") or {}
                centroid = dsp.get("spectral_centroid_mean_hz") or {}
                bpm_text = f"{_metric_value(dsp, 'bpm_estimate')} (observed)"
                key_text = f"{_metric_value(dsp, 'key_impression')} (キー感・observed)"
                density_text = f"{onset.get('value') if isinstance(onset, dict) else onset} / {onset.get('description') if isinstance(onset, dict) else ''}"
                rms_text = (
                    f"mean {rms_mean.get('value') if isinstance(rms_mean, dict) else rms_mean}, "
                    f"std {rms_std.get('value') if isinstance(rms_std, dict) else rms_std}"
                )
                centroid_text = f"{centroid.get('value') if isinstance(centroid, dict) else centroid} Hz"
                instruments = _compact_items((ai or {}).get("instruments") if isinstance(ai, dict) else [], 8)
                mix_texture = (ai or {}).get("mix_texture") if isinstance(ai, dict) else ""
                dynamics = (ai or {}).get("dynamics") if isinstance(ai, dict) else ""
                tempo_feel = f"DSP実測 BPM {bpm_text}, {key_text}"
            else:
                bpm = mp.get("bpm_estimate") or {}
                bpm_text = ""
                if isinstance(bpm, dict):
                    bpm_text = f"{bpm.get('value') or ''} ({bpm.get('confidence') or ''})".strip()
                else:
                    bpm_text = str(bpm or "")
                tempo_feel = mp.get("tempo_feel") or ""
                key_text = mp.get("key_impression") or ""
                density_text = mp.get("density") or ""
                rms_text = ""
                centroid_text = ""
                instruments = _compact_items(mp.get("instruments"), 8)
                mix_texture = mp.get("mix_texture") or ""
                dynamics = mp.get("dynamics") or ""
            hints = _compact_items(mp.get("suno_style_hints"), 8)
            avoid = _compact_items(mp.get("avoid_in_suno"), 6)
            block = [
                f"[music seed {idx}] {r.get('video_title') or r.get('video_id') or '(title unknown)'}",
                f"- bpm_estimate: {bpm_text}",
                f"- tempo_feel: {tempo_feel}",
            ]
            if bpm_rationale:
                block.append(f"- bpm_rationale: {bpm_rationale}")
            block.extend([
                f"- key_impression: {key_text}",
                f"- instruments: {', '.join(instruments) if instruments else ''}",
                f"- density: {density_text}",
            ])
            if rms_text:
                block.append(f"- rms_dynamics: {rms_text}")
            if centroid_text:
                block.append(f"- brightness: {centroid_text}")
            block.extend([
                f"- mix_texture: {mix_texture}",
                f"- dynamics: {dynamics}",
                f"- suno_style_hints: {', '.join(hints) if hints else ''}",
                f"- avoid_in_suno: {', '.join(avoid) if avoid else ''}",
            ])
            out.extend(block)
        return "\n".join(out).strip()
    except Exception:
        return ""


def _print_list() -> None:
    data = list_seed_analyses()
    rows = data.get("analyses") or {}
    for vid, analysis in sorted(rows.items(), key=lambda x: (x[1].get("analyzed_at") or ""), reverse=True):
        hyp = analysis.get("pdca_hypothesis") or {}
        print(f"{vid}\t{analysis.get('video_title','')}\t{analysis.get('analyzed_at','')}\t{hyp.get('changed_element','')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="ベンチマーク seed 動画分析")
    parser.add_argument("video_url_or_id", nargs="?", help="YouTube URL または videoId")
    parser.add_argument("--context", default="", help="分析時に渡す補足文脈")
    parser.add_argument("--cli", default="claude", help="LLM CLI コマンド")
    parser.add_argument("--list", action="store_true", help="保存済み seed 分析一覧を表示")
    parser.add_argument("--audio", action="store_true", help="seed 動画の音源を精密分析（mp3 自動取得 + DSP + Gemini + 調停）")
    parser.add_argument("--audio-gemini", action="store_true", help="Gemini 単体の音源実聴分析を実行（旧 --audio 相当）")
    parser.add_argument("--full", action="store_true", help="seed 動画分析後に音源分析も実行")
    parser.add_argument("--fetch-audio", action="store_true", help="yt-dlp で seed 動画の mp3 を取得してパスを表示")
    parser.add_argument("--file", default="", help="音源分析に使うローカル mp3 パス")
    args = parser.parse_args()

    if args.list:
        _print_list()
        return 0
    if not args.video_url_or_id:
        parser.error("video_url_or_id または --list が必要です")
    if args.fetch_audio:
        path = download_seed_audio(args.video_url_or_id)
        print(str(path))
        return 0
    if args.audio_gemini:
        result = analyze_seed_audio(args.video_url_or_id, cli_cmd=args.cli)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    if args.audio:
        result = analyze_seed_audio_local(args.video_url_or_id, file_path=args.file or None, cli_cmd=args.cli)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    result = analyze_seed_video(args.video_url_or_id, context_hint=args.context, cli_cmd=args.cli)
    if args.full:
        try:
            result["music_profile"] = analyze_seed_audio_local(args.video_url_or_id, file_path=args.file or None, cli_cmd=args.cli)
        except Exception as e:
            print(f"  ⚠ 音源分析は失敗しましたが seed 分析結果は保存済みです: {e}")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
