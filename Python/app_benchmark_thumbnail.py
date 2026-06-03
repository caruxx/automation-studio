#!/usr/bin/env python3
"""ベンチマーク・サムネイル軸の分析モジュール。

Phase 1 (サムネ軸):
  1. competitor_analysis_cache.json から videoId を拾い、サムネを
     ローカル `~/.config/{app_id}/benchmark/thumbs/<channel>/<videoId>.jpg`
     にダウンロード
  2. Claude Vision で「per-channel + aggregate」の 2 段で分析
  3. picked リスト (videoId 集合) を保持
     → Flow / Image2 への `--reference-image` ソースとして流用可能

I/O:
  ANALYSIS_FILE = ~/.config/{app_id}/benchmark/thumbnail.json
  THUMBS_DIR    = ~/.config/{app_id}/benchmark/thumbs/<channel_id>/<video_id>.jpg
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Optional

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"

BENCHMARK_DIR = CONFIG_DIR / "benchmark"
THUMBS_DIR = BENCHMARK_DIR / "thumbs"
ANALYSIS_FILE = BENCHMARK_DIR / "thumbnail.json"
COMPETITOR_CACHE_FILE = CONFIG_DIR / "competitor_analysis_cache.json"
ANALYSIS_FILENAME = "thumbnail.json"

DEFAULT_CLI = "claude"
ANALYSIS_TIMEOUT = 600
DL_TIMEOUT = 15

# サムネ取得候補（maxres → hq → mq の順でフォールバック）
_THUMB_QUALITIES = ("maxresdefault", "hqdefault", "mqdefault")


# ─── キャッシュ I/O ────────────────────────────────

def _ensure_dirs() -> None:
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)
    THUMBS_DIR.mkdir(parents=True, exist_ok=True)


def load_cache() -> dict:
    empty = {"channels": [], "analysis": {}, "picked": [], "generated_at": ""}
    try:
        from app_channel_cache import load_scoped_cache
        d = load_scoped_cache(ANALYSIS_FILENAME, ANALYSIS_FILE, empty)
    except Exception:
        d = None
    if d is None:
        if not ANALYSIS_FILE.exists():
            return empty
        try:
            d = json.loads(ANALYSIS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return empty
    try:
        if not isinstance(d, dict):
            return empty
        d.setdefault("channels", [])
        d.setdefault("analysis", {})
        d.setdefault("picked", [])
        d.setdefault("generated_at", "")
        return d
    except Exception:
        return empty


def save_cache(payload: dict) -> None:
    _ensure_dirs()
    try:
        from app_channel_cache import save_scoped_cache
        save_scoped_cache(ANALYSIS_FILENAME, ANALYSIS_FILE, payload)
    except Exception:
        ANALYSIS_FILE.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _load_competitor_cache() -> Optional[dict]:
    try:
        from app_channel_cache import load_scoped_cache
        d = load_scoped_cache("competitor_analysis_cache.json", COMPETITOR_CACHE_FILE, None)
        return d if isinstance(d, dict) else None
    except Exception:
        if not COMPETITOR_CACHE_FILE.exists():
            return None
        try:
            d = json.loads(COMPETITOR_CACHE_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else None
        except Exception:
            return None


# ─── ダウンロード ─────────────────────────────────

def _safe_id(s: str) -> str:
    """ファイルシステム安全なフォルダ名に整形（channelId 用）。"""
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (s or "_"))[:64]


def _download_one(video_id: str, dst: Path) -> Optional[str]:
    """1 つの videoId からサムネを DL。成功した quality 名を返す。失敗時は None。"""
    if dst.exists() and dst.stat().st_size > 0:
        return "cached"
    dst.parent.mkdir(parents=True, exist_ok=True)
    last_err = None
    for q in _THUMB_QUALITIES:
        url = f"https://i.ytimg.com/vi/{video_id}/{q}.jpg"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=DL_TIMEOUT) as r:
                data = r.read()
            # maxres は存在しないと 404 で例外、hq/mq は YouTube が
            # 「動画なし」の灰色 120x90 を返すので最低サイズで判定
            if len(data) < 1500:
                last_err = f"{q}: too small ({len(data)} bytes)"
                continue
            dst.write_bytes(data)
            return q
        except Exception as e:
            last_err = f"{q}: {e}"
            continue
    print(f"  ⚠ thumb DL 失敗: {video_id} ({last_err})")
    return None


def download_thumbnails(competitor_data: Optional[dict] = None) -> list[dict]:
    """competitor_analysis_cache.json から DL 対象を抽出 → ローカル保存。

    Returns:
      channels: [
        {
          "channelId": str, "channelName": str,
          "thumbnails": [
            {"videoId": str, "title": str, "viewCount": int, "likeCount": int,
             "publishedAt": str, "category": "topByViews"|"recentUploads",
             "localPath": str, "quality": str},
            ...
          ]
        }, ...
      ]
    """
    _ensure_dirs()
    if competitor_data is None:
        outer = _load_competitor_cache() or {}
        competitor_data = outer.get("competitor_data") or {}

    channels_out: list[dict] = []
    for ch in competitor_data.get("channels", []):
        ch_id = ch.get("channelId") or _safe_id(ch.get("channelName", ""))
        ch_name = ch.get("channelName", "")
        ch_dir = THUMBS_DIR / _safe_id(ch_id)
        ch_dir.mkdir(parents=True, exist_ok=True)

        # 重複排除しつつ category を付ける（top と recent で重複する videoId が多い）
        seen: set[str] = set()
        items: list[dict] = []
        for category in ("topByViews", "recentUploads"):
            for v in ch.get(category, []) or []:
                vid = v.get("videoId")
                if not vid or vid in seen:
                    continue
                seen.add(vid)
                items.append({**v, "category": category})

        thumbs: list[dict] = []
        for v in items:
            vid = v["videoId"]
            dst = ch_dir / f"{vid}.jpg"
            quality = _download_one(vid, dst)
            if not quality:
                continue
            thumbs.append({
                "videoId": vid,
                "title": v.get("title", ""),
                "viewCount": int(v.get("viewCount", 0) or 0),
                "likeCount": int(v.get("likeCount", 0) or 0),
                "publishedAt": v.get("publishedAt", ""),
                "category": v["category"],
                "localPath": str(dst),
                "quality": quality,
            })
        channels_out.append({
            "channelId": ch_id,
            "channelName": ch_name,
            "thumbnails": thumbs,
        })
        print(f"  ✓ {ch_name}: {len(thumbs)} 枚 DL 済")

    return channels_out


# ─── Vision 分析 ─────────────────────────────────

def _extract_json(text: str) -> Optional[dict]:
    """```json ブロック or 単独 JSON を抽出。"""
    import re
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    s, e = candidate.find("{"), candidate.rfind("}")
    if s < 0 or e <= s:
        return None
    blob = candidate[s:e + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def _build_per_channel_prompt(ch_name: str, items: list[dict]) -> str:
    image_block = "\n".join(
        f"  - {t['localPath']}\n      title: {t.get('title','')}\n      views: {t.get('viewCount',0):,} | likes: {t.get('likeCount',0):,}"
        for t in items
    )
    return f"""あなたは YouTube サムネイル分析の専門家です。
チャンネル「{ch_name}」のサムネ画像を Read ツールで読んで、視覚要素 + 視聴者フックを抽出してください。

## 重要な制約
- 分析文・理由文・提案文はすべて自然な日本語で書く（英語だけの出力は禁止）
- コピー目的ではなく「要素抽出と他チャンネルへの翻訳指針」を出すこと
- views / likes と画像の対応を踏まえて「効いている要素」を特定すること

## 分析対象
{image_block}

## 出力（単一 JSON / 余計な文章なし）
```json
{{
  "channel": "{ch_name}",
  "common_palette": ["支配色1", "支配色2"],
  "common_composition": "構図パターンの傾向（1〜2文）",
  "common_subjects": ["主要被写体1", "主要被写体2"],
  "common_atmosphere": "雰囲気/温度感（1文）",
  "typography": "テキスト要素の傾向（あれば）",
  "five_element_breakdown": {{
    "subject": "被写体・主役として効いているもの",
    "background_context": "背景とコンテキスト",
    "lighting": "ライティング・時間帯・陰影",
    "style_rendering": "画風・質感・レンダリング",
    "camera_composition": "カメラ視点・構図・焦点"
  }},
  "viewer_hooks": ["クリックを誘う心理フック1", "心理フック2"],
  "strong_examples": [
    {{"videoId": "<localPath末尾のID>", "why": "なぜ強いか1〜2文"}}
  ],
  "avoid": ["コピーすべきでない具体要素"]
}}
```"""


def _build_aggregate_prompt(per_channel_results: list[dict]) -> str:
    summaries = []
    for r in per_channel_results:
        summaries.append(
            f"- {r.get('channel','?')}: palette={r.get('common_palette',[])}, "
            f"composition={r.get('common_composition','')}, "
            f"subjects={r.get('common_subjects',[])}, "
            f"hooks={r.get('viewer_hooks',[])}"
        )
    block = "\n".join(summaries) or "(no per-channel results)"
    return f"""下記は複数チャンネルそれぞれのサムネ分析サマリーです。
横断的な共通点・分岐点・自チャンネルが取り入れるべき方向性を抽出してください。

## チャンネル別サマリー
{block}

## 出力ルール
- 分析文・理由文・提案文はすべて自然な日本語で書く（英語だけの出力は禁止）

## 出力（単一 JSON / 余計な文章なし）
```json
{{
  "shared_palette": ["業界横断で共通する支配色"],
  "shared_composition": "業界横断で共通する構図",
  "differentiators": [
    {{"channel": "...", "edge": "他と差別化されている強み"}}
  ],
  "underserved_visual": ["まだ誰もやっていない視覚的方向性"],
  "recommendation_for_self": {{
    "keep": ["取り入れるべき抽象要素"],
    "transform": ["翻訳すべき要素（具体→抽象）"],
    "vibe_one_line": "自チャンネルでの1文サマリー",
    "gpt_image2_prompt_seed": "被写体 / 背景 / ライティング / スタイル / カメラ構図の5要素で再構成した英語の短い生成方針"
  }},
  "gpt_image2_prompt_notes": {{
    "subject": "何を描くべきか",
    "background_context": "どこに置くべきか",
    "lighting": "どんな光にすべきか",
    "style_rendering": "どんな画風・質感にすべきか",
    "camera_composition": "どんな視点・構図にすべきか",
    "avoid": ["コピー・商標・ロゴ・既存サムネ固有の要素"]
  }}
}}
```"""


def _run_claude_vision(cli_cmd: str, prompt: str, image_paths: list[Path],
                       timeout: int = ANALYSIS_TIMEOUT) -> str:
    # Claude→Codex フォールバック共通ランナー(Vision)に委譲（全機能のバックアップ回路）
    from app_llm_runner import run_llm_vision
    return run_llm_vision(prompt, image_paths, cli_cmd=cli_cmd, timeout=timeout, label="thumbnail-vision")


def analyze_channels(channels: list[dict], cli_cmd: str = DEFAULT_CLI,
                     per_channel_cap: int = 8,
                     only_channel_ids=None, skip_unchanged: bool = True,
                     existing_per_channel: dict = None) -> dict:
    """各チャンネルを Vision 分析 → 集約。

    only_channel_ids: 選択 ch だけ分析。skip_unchanged: サムネ集合の指紋が既存と
    一致なら Vision を再実行せず流用（Vision はコスト最大なので効果大）。
    Returns:
      {"per_channel": {channelId: {...}}, "aggregate": {...}}
    """
    from app_benchmark_common import channel_fingerprint, plan_channel, stamp_meta, normalize_ids
    only_ids = normalize_ids(only_channel_ids)
    existing = existing_per_channel or {}
    per_channel: dict[str, dict] = {}
    n_run = n_skip = 0

    for ch in channels:
        ch_id = ch.get("channelId") or _safe_id(ch.get("channelName", ""))
        ch_name = ch.get("channelName", "")
        thumbs = ch.get("thumbnails") or []
        if not thumbs:
            continue
        # views 上位を per_channel_cap 件まで
        items = sorted(thumbs, key=lambda t: t.get("viewCount", 0), reverse=True)[:per_channel_cap]
        fp = channel_fingerprint({"topByViews": items})
        if plan_channel(ch_id, fp, existing, only_ids, skip_unchanged, ch_name) == "carry":
            if ch_id in existing:
                per_channel[ch_id] = existing[ch_id]
                n_skip += 1
            continue
        prompt = _build_per_channel_prompt(ch_name, items)
        paths = [Path(t["localPath"]) for t in items]
        try:
            raw = _run_claude_vision(cli_cmd, prompt, paths)
        except RuntimeError as e:
            print(f"  ⚠ {ch_name}: {e}")
            if ch_id in existing:
                per_channel[ch_id] = existing[ch_id]
            continue
        obj = _extract_json(raw)
        if not obj:
            print(f"  ⚠ {ch_name}: JSON 抽出失敗")
            if ch_id in existing:
                per_channel[ch_id] = existing[ch_id]
            continue
        per_channel[ch_id] = stamp_meta(obj, fp)
        n_run += 1
        print(f"  ✓ {ch_name}: 分析完了")

    summaries = [{"channel": (v.get("channel") or k), **{kk: vv for kk, vv in v.items() if kk != "_meta"}}
                 for k, v in per_channel.items()]
    print(f"  ◎ Vision再分析 {n_run} / 流用 {n_skip} / 計 {len(per_channel)}")

    aggregate: dict = {}
    if summaries:
        try:
            agg_prompt = _build_aggregate_prompt(summaries)
            # aggregate は画像不要 → テキスト共通ランナー（Claude→Codex）
            from app_llm_runner import run_llm
            aggregate = _extract_json(run_llm(agg_prompt, cli_cmd=cli_cmd, timeout=ANALYSIS_TIMEOUT, label="thumb-aggregate")) or {}
            print("  ✓ aggregate 分析完了")
        except Exception as e:
            print(f"  ⚠ aggregate 失敗: {e}")

    return {"per_channel": per_channel, "aggregate": aggregate}


# ─── トップレベル: ダウンロード + 分析 + 保存 ───────────

def run_full(cli_cmd: str = DEFAULT_CLI, per_channel_cap: int = 8,
             only_channel_ids=None, skip_unchanged: bool = True) -> dict:
    """既存の競合キャッシュをソースに、サムネ DL + 分析 + 保存を実行。"""
    print("📥 サムネ DL 開始")
    channels = download_thumbnails()
    if not channels:
        raise RuntimeError("DL 対象がありません。先に競合データ取得を実行してください。")

    print("🧠 Claude Vision で分析中")
    existing_pc = (load_cache().get("analysis") or {}).get("per_channel", {}) or {}
    analysis = analyze_channels(channels, cli_cmd=cli_cmd, per_channel_cap=per_channel_cap,
                                only_channel_ids=only_channel_ids, skip_unchanged=skip_unchanged,
                                existing_per_channel=existing_pc)

    existing = load_cache()
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "channels": channels,
        "analysis": analysis,
        # picked は前回値を引き継ぐ（DL対象から消えた videoId は除外）
        "picked": _filter_valid_picks(existing.get("picked", []), channels),
    }
    save_cache(payload)
    print(f"✅ thumbnail.json 保存: {ANALYSIS_FILE}")
    return payload


def _filter_valid_picks(picked: list[str], channels: list[dict]) -> list[str]:
    valid: set[str] = set()
    for ch in channels:
        for t in ch.get("thumbnails", []):
            vid = t.get("videoId")
            if vid:
                valid.add(vid)
    return [p for p in picked if p in valid]


# ─── Picked 管理（Flow / Image2 への参照渡し用） ───────

def get_picked() -> list[str]:
    return list(load_cache().get("picked", []))


def set_picked(video_ids: list[str]) -> list[str]:
    cache = load_cache()
    valid = set()
    for ch in cache.get("channels", []):
        for t in ch.get("thumbnails", []):
            if t.get("videoId"):
                valid.add(t["videoId"])
    cleaned = [v for v in video_ids if v in valid]
    cache["picked"] = cleaned
    save_cache(cache)
    return cleaned


def get_picked_paths(limit: Optional[int] = None) -> list[str]:
    """picked リストに対応するローカル画像ファイルパスを返す。
    Flow `--reference-image` などに直接渡せる形式。"""
    cache = load_cache()
    picked = set(cache.get("picked", []))
    paths: list[str] = []
    for ch in cache.get("channels", []):
        for t in ch.get("thumbnails", []):
            if t.get("videoId") in picked:
                p = t.get("localPath")
                if p and Path(p).exists():
                    paths.append(p)
    if limit is not None:
        return paths[:limit]
    return paths


def get_picked_details() -> list[dict]:
    """picked のメタ情報を返す（UI 表示・他軸での参照用）。"""
    cache = load_cache()
    picked = set(cache.get("picked", []))
    out: list[dict] = []
    for ch in cache.get("channels", []):
        for t in ch.get("thumbnails", []):
            if t.get("videoId") in picked:
                out.append({
                    "channelId": ch.get("channelId"),
                    "channelName": ch.get("channelName"),
                    **t,
                })
    return out


# ─── CLI ───────────────────────────────────────────

def _main():
    import argparse
    p = argparse.ArgumentParser(description="ベンチマーク・サムネイル分析")
    p.add_argument("--cli", default=DEFAULT_CLI, help="claude CLI コマンド")
    p.add_argument("--per-channel-cap", type=int, default=8)
    p.add_argument("--dl-only", action="store_true", help="DL のみ（分析は走らせない）")
    p.add_argument("--show-picked", action="store_true", help="picked の画像パスを stdout に")
    args = p.parse_args()

    if args.show_picked:
        for x in get_picked_paths():
            print(x)
        return

    if args.dl_only:
        chs = download_thumbnails()
        cache = load_cache()
        cache["channels"] = chs
        cache["generated_at"] = datetime.now().isoformat(timespec="seconds")
        save_cache(cache)
        print(f"✅ DL 完了: {sum(len(c.get('thumbnails',[])) for c in chs)} 枚")
        return

    run_full(cli_cmd=args.cli, per_channel_cap=args.per_channel_cap)


if __name__ == "__main__":
    _main()
