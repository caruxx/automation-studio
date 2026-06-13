#!/usr/bin/env python3
"""動画単位の競合インテリジェンス（S1: 新作ウォッチ / S2: 勝ちサムネ要素ライブラリ）。

ベンチ追跡の既存資産（TRACK_ 個別タブの VideoEvent = タイトル + 48h 初速）を
「動画単位のレコード」として蓄積し、YouTube Data API でサムネ URL を確定、
Vision で見た目要素を抽出して「初速が出た要素 vs 平均」の統計（サムネ DNA）を作る。

設計方針:
  - 収集（collect）と初速更新は LLM コストゼロ → cron 可
  - Vision 分析（analyze）は手動トリガ（既存ベンチ 4 軸の流儀。dry_run でコスト可視化）
  - YouTube Data API: channelId 解決は search.list（100 units・チャンネル毎に初回のみ、
    永続キャッシュ）、動画列挙は playlistItems.list（1 unit）。上限 10,000/日に対し軽微。
  - ストア: ~/.config/{app_id}/video_intel.json（チャンネル横断 1 ファイル）
  - DNA 出力: ~/.config/{app_id}/thumb_dna.json（サムネ生成プロンプト側が任意参照）
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).parent))
try:
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"

STORE_FILE = CONFIG_DIR / "video_intel.json"
DNA_FILE = CONFIG_DIR / "thumb_dna.json"
THUMB_CACHE_DIR = CONFIG_DIR / "video_intel_thumbs"

# Vision 抽出の要素スキーマ。DNA 集計でカテゴリ値が揃うよう選択肢を固定する。
VISION_SCHEMA_HINT = """{
  "scene": "屋内カフェ | 屋外自然 | 屋外都市 | 夜景 | 室内その他 | 抽象/イラスト空間",
  "people": "なし | 人物(後ろ姿/シルエット) | 人物(顔あり) | 動物/キャラクター",
  "subject": "主要被写体を 1 フレーズ（自由記述・日本語）",
  "composition": "中央配置 | 三分割 | 左右に余白 | 俯瞰 | クローズアップ",
  "color_tone": "暖色 | 寒色 | モノトーン | ビビッド | パステル",
  "lighting": "自然光 | 夕暮れ/ゴールデンアワー | 夜/ネオン | 暗め/ムーディ | フラット",
  "text_amount": "なし | 少(5語以内) | 多",
  "text_lang": "en | ja | mixed | none",
  "style": "実写 | 実写フィルム調 | イラスト | アニメ調 | 3DCG | AI生成調",
  "mood": "雰囲気を 1-2 語（自由記述・日本語）"
}"""


# ─── ストア ───────────────────────────────────────

def _load_store() -> dict:
    try:
        d = json.loads(STORE_FILE.read_text(encoding="utf-8"))
        d.setdefault("channel_ids", {})
        d.setdefault("records", {})
        return d
    except Exception:
        return {"channel_ids": {}, "records": {}}


def _save_store(store: dict) -> None:
    STORE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STORE_FILE.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")


def _rec_key(channel: str, title: str) -> str:
    return hashlib.sha1(f"{channel}|{_norm_title(title)}".encode("utf-8")).hexdigest()[:16]


def _norm_title(t: str) -> str:
    t = (t or "").lower().strip()
    t = re.sub(r"[\s　]+", " ", t)
    t = re.sub(r"[^\w\s぀-ヿ一-鿿]", "", t)
    return t[:80]


# ─── YouTube Data API（公開データの読み取り） ───────

def _yt_service(channel_folder: str):
    """既存の OAuth token（どのチャンネルのものでも可・公開データ読み取り）で API client を作る。

    ⚠ get_credentials(video_folder=X) は X の「親」を見る仕様（動画フォルダ前提）なので、
    チャンネルフォルダを渡すときは token_override で直接 .youtube_token.json を指定する。"""
    from app_youtube import get_credentials
    from googleapiclient.discovery import build
    token = Path(channel_folder) / ".youtube_token.json"
    creds = get_credentials(token_override=str(token) if token.exists() else None)
    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def _resolve_channel_id(youtube, ch_name: str, store: dict) -> Optional[str]:
    """チャンネル名 → channelId。search.list は 100 units と高いので永続キャッシュ必須。"""
    cached = store["channel_ids"].get(ch_name)
    if cached:
        return cached or None
    try:
        res = youtube.search().list(part="snippet", q=ch_name, type="channel",
                                    maxResults=3).execute()
        items = res.get("items", [])
        best = None
        for it in items:
            t = (it.get("snippet", {}).get("channelTitle") or "").strip()
            if _norm_title(t) == _norm_title(ch_name):
                best = it
                break
        if best is None and items:
            best = items[0]  # 完全一致が無ければ先頭（後から手動訂正可能なようキャッシュに保存）
        cid = (best or {}).get("snippet", {}).get("channelId") or \
              ((best or {}).get("id", {}) or {}).get("channelId")
        store["channel_ids"][ch_name] = cid or ""
        return cid
    except Exception as e:
        print(f"[video-intel] channelId 解決失敗 ({ch_name}): {e}")
        return None


def _fetch_recent_videos(youtube, channel_id: str, max_results: int = 25) -> list[dict]:
    """uploads playlist から直近動画（videoId / title / publishedAt / thumb）を取得（1-2 units）。"""
    try:
        ch = youtube.channels().list(part="contentDetails", id=channel_id).execute()
        items = ch.get("items", [])
        if not items:
            return []
        uploads = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        pl = youtube.playlistItems().list(part="snippet", playlistId=uploads,
                                          maxResults=min(50, max_results)).execute()
        out = []
        for it in pl.get("items", []):
            sn = it.get("snippet", {})
            vid = (sn.get("resourceId", {}) or {}).get("videoId", "")
            thumbs = sn.get("thumbnails", {}) or {}
            best = thumbs.get("maxres") or thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {}
            out.append({
                "video_id": vid,
                "title": sn.get("title", ""),
                "published_at": sn.get("publishedAt", ""),
                "thumb_url": best.get("url", "") or (f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else ""),
            })
        return out
    except Exception as e:
        print(f"[video-intel] 動画列挙失敗 ({channel_id}): {e}")
        return []


# ─── 収集（cron 可・LLM コストゼロ） ────────────────

def collect(growth_url: str, *, channel_folder: str, target_channels: Optional[list[str]] = None,
            hot_top_n: int = 10, enrich: bool = True) -> dict:
    """対象チャンネルの TRACK タブから VideoEvent を回収してレコード upsert し、
    YouTube Data API で videoId/サムネ URL を解決（enrich）する。

    target_channels 未指定時は hot-channels 上位 + ピン留めを対象にする。
    返り値: {"channels": n, "upserted": n, "enriched": n, "quota_spent_approx": n}
    """
    from app_sheets import (fetch_csv, parse_growth_tracking, identify_hot_channels,
                            fetch_channel_timeline)
    store = _load_store()
    today = datetime.date.today().isoformat()

    if target_channels is None:
        entries = parse_growth_tracking(fetch_csv(growth_url))
        hot = identify_hot_channels(entries, top_n=hot_top_n)
        target_channels = [e.channel_name for e in hot]
        # ピン留め（benchmark config）も常に対象へ
        try:
            from app_core import get_benchmark_config  # app.py 経由実行時
            pinned = (get_benchmark_config() or {}).get("pinned_names") or []
        except Exception:
            pinned = []
        for p in pinned:
            if p not in target_channels:
                target_channels.append(p)

    upserted = 0
    quota = 0
    for ch_name in target_channels:
        tl = fetch_channel_timeline(growth_url, ch_name)
        if tl.error:
            continue
        for ev in tl.new_videos or []:
            if not (ev.title or "").strip():
                continue
            key = _rec_key(ch_name, ev.title)
            rec = store["records"].get(key) or {
                "channel": ch_name, "title": ev.title,
                "video_id": "", "thumb_url": "", "published_at": "",
                "detected_date": today, "vision": None, "vision_at": "",
            }
            # 初速・ピークは常に最新値で更新（タブ側が伸びると増える）
            rec["first_v48"] = max(int(ev.first_v48 or 0), int(rec.get("first_v48") or 0))
            rec["peak_views"] = max(int(ev.peak_views or 0), int(rec.get("peak_views") or 0))
            if ev.ctr:
                rec["ctr"] = ev.ctr
            rec["last_seen"] = today
            store["records"][key] = rec
            upserted += 1

    enriched = 0
    if enrich:
        # videoId 未解決のレコードがあるチャンネルだけ API を叩く
        need = {}
        for key, rec in store["records"].items():
            if not rec.get("video_id") and rec.get("channel") in (target_channels or []):
                need.setdefault(rec["channel"], []).append(key)
        if need:
            try:
                youtube = _yt_service(channel_folder)
            except Exception as e:
                print(f"[video-intel] YouTube API 初期化失敗: {e}")
                youtube = None
            if youtube is not None:
                for ch_name, keys in need.items():
                    had_id = bool(store["channel_ids"].get(ch_name))
                    cid = _resolve_channel_id(youtube, ch_name, store)
                    if not had_id:
                        quota += 100  # search.list（初回のみ）
                    if not cid:
                        continue
                    vids = _fetch_recent_videos(youtube, cid)
                    quota += 2  # channels.list + playlistItems.list
                    by_norm = {_norm_title(v["title"]): v for v in vids}
                    for key in keys:
                        rec = store["records"][key]
                        nt = _norm_title(rec["title"])
                        hit = by_norm.get(nt)
                        if not hit:  # 前方一致のゆるいフォールバック（タブ側は省略されがち）
                            hit = next((v for n2, v in by_norm.items()
                                        if n2.startswith(nt[:30]) or nt.startswith(n2[:30])), None)
                        if hit:
                            rec.update({"video_id": hit["video_id"], "thumb_url": hit["thumb_url"],
                                        "published_at": hit["published_at"]})
                            enriched += 1

    _save_store(store)
    return {"channels": len(target_channels or []), "upserted": upserted,
            "enriched": enriched, "quota_spent_approx": quota}


# ─── Vision 分析（手動トリガ・LLM コストあり） ───────

def _download_thumb(rec_key: str, url: str) -> Optional[Path]:
    THUMB_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out = THUMB_CACHE_DIR / f"{rec_key}.jpg"
    if out.exists() and out.stat().st_size > 1000:
        return out
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out.write_bytes(r.read())
        return out if out.stat().st_size > 1000 else None
    except Exception as e:
        print(f"[video-intel] サムネDL失敗 {url}: {e}")
        return None


def analyze(*, limit: int = 8, dry_run: bool = False, cli_cmd: Optional[str] = None) -> dict:
    """未分析（vision=None かつ thumb_url あり）のレコードを Vision で要素抽出。

    dry_run=True なら対象件数だけ返して LLM を呼ばない（コスト可視化）。"""
    store = _load_store()
    pending = [(k, r) for k, r in store["records"].items()
               if r.get("vision") is None and r.get("thumb_url")]
    # 初速が大きい順に分析（情報価値の高いものから）
    pending.sort(key=lambda kr: int(kr[1].get("first_v48") or 0), reverse=True)
    targets = pending[:max(1, limit)]
    if dry_run:
        return {"pending_total": len(pending), "would_analyze": len(targets),
                "targets": [{"channel": r["channel"], "title": r["title"][:60],
                             "first_v48": r.get("first_v48", 0)} for _, r in targets]}

    from app_benchmark_common import extract_json_object
    from app_llm_runner import run_llm_vision
    done, failed = 0, 0
    for key, rec in targets:
        img = _download_thumb(key, rec["thumb_url"])
        if img is None:
            rec["vision"] = {"error": "thumb_download_failed"}
            failed += 1
            continue
        prompt = (
            "あなたは YouTube BGM チャンネルのサムネイル分析の専門家です。\n"
            f"この動画サムネイル（タイトル:『{rec['title'][:80]}』）の見た目要素を、"
            "以下の JSON スキーマで分類してください。選択肢のあるキーは必ず選択肢から選ぶこと。\n"
            f"{VISION_SCHEMA_HINT}\n"
            "JSON のみを出力。説明文は不要。"
        )
        try:
            out = run_llm_vision(prompt, [str(img)], cli_cmd=cli_cmd,
                                 timeout=180, label=f"video-intel:{rec['channel'][:12]}")
            data = extract_json_object(out)
            if isinstance(data, dict) and data.get("scene"):
                rec["vision"] = data
                rec["vision_at"] = datetime.datetime.now().isoformat()[:19]
                done += 1
            else:
                rec["vision"] = None
                failed += 1
        except Exception as e:
            print(f"[video-intel] vision 失敗 ({rec['title'][:40]}): {e}")
            failed += 1
    _save_store(store)
    return {"analyzed": done, "failed": failed,
            "pending_remaining": max(0, len(pending) - len(targets))}


# ─── S2: サムネ DNA（勝ち要素の出現率差） ─────────────

_DNA_KEYS = ["scene", "people", "composition", "color_tone", "lighting",
             "text_amount", "text_lang", "style"]


def aggregate_dna(*, min_records: int = 8) -> dict:
    """vision 済みレコードを「初速上位（中央値超え）vs 全体」で比較し、
    要素値ごとの出現率差ランキングを作る。結果は thumb_dna.json にも書き出す。"""
    store = _load_store()
    recs = [r for r in store["records"].values()
            if isinstance(r.get("vision"), dict) and not r["vision"].get("error")]
    n = len(recs)
    if n < min_records:
        return {"ok": False, "records": n,
                "hint": f"分析済みレコードが {n} 件（{min_records} 件以上で統計が安定します）"}
    v48s = sorted(int(r.get("first_v48") or 0) for r in recs)
    median = v48s[len(v48s) // 2]
    winners = [r for r in recs if int(r.get("first_v48") or 0) > median]
    ranking = []
    for key in _DNA_KEYS:
        all_counts: dict = {}
        win_counts: dict = {}
        for r in recs:
            val = str(r["vision"].get(key) or "").strip()
            if val:
                all_counts[val] = all_counts.get(val, 0) + 1
        for r in winners:
            val = str(r["vision"].get(key) or "").strip()
            if val:
                win_counts[val] = win_counts.get(val, 0) + 1
        for val, cnt in all_counts.items():
            base = cnt / n
            win = (win_counts.get(val, 0) / len(winners)) if winners else 0.0
            lift = win - base
            if cnt >= 3:  # 出現 3 回未満はノイズ
                ranking.append({"key": key, "value": val, "lift_pct": round(lift * 100, 1),
                                "win_rate_pct": round(win * 100, 1),
                                "base_rate_pct": round(base * 100, 1), "n": cnt})
    ranking.sort(key=lambda x: x["lift_pct"], reverse=True)
    out = {
        "ok": True, "records": n, "winners": len(winners), "v48_median": median,
        "generated_at": datetime.datetime.now().isoformat()[:19],
        "top_positive": ranking[:12],
        "top_negative": list(reversed(ranking[-8:])) if len(ranking) > 8 else [],
        # 生成プロンプト注入用の 1 行サマリ（英語・上位の勝ち要素のみ）
        "prompt_hint": ", ".join(
            f"{x['value']}" for x in ranking[:5] if x["lift_pct"] > 5),
    }
    try:
        DNA_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass
    return out


def load_dna() -> dict:
    try:
        return json.loads(DNA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def list_records(*, limit: int = 100) -> list[dict]:
    store = _load_store()
    recs = list(store["records"].values())
    recs.sort(key=lambda r: (r.get("last_seen") or "", int(r.get("first_v48") or 0)), reverse=True)
    return recs[:limit]


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="video intel CLI")
    ap.add_argument("--collect", action="store_true")
    ap.add_argument("--analyze", type=int, default=0, metavar="N")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dna", action="store_true")
    ap.add_argument("--growth-url", default="")
    ap.add_argument("--channel-folder", default="")
    a = ap.parse_args()
    if a.collect:
        print(json.dumps(collect(a.growth_url, channel_folder=a.channel_folder),
                         ensure_ascii=False, indent=1))
    if a.analyze:
        print(json.dumps(analyze(limit=a.analyze, dry_run=a.dry_run), ensure_ascii=False, indent=1))
    if a.dna:
        print(json.dumps(aggregate_dna(), ensure_ascii=False, indent=1))
