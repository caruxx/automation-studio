#!/usr/bin/env python3
"""ベンチマーク・投稿文（説明文）軸の分析モジュール。

指定チャンネル（rival_channels / 適用中ベンチマーク）の動画説明文の「構成」を横断分析し、
(a) 既存動画のメタ改善 と (b) 新規投稿文の構成案 へ還流するための軸。
タイトル軸/コンセプト軸/サムネ軸と同じ per_channel + aggregate パターン（app_benchmark_concept.py が雛形）。

入力:
  ~/.config/{app_id}/competitor_analysis_cache.json (competitor_data) … 既定の 500字版
  ※ refetch_full=True 時は app_competitor.fetch_competitor_data(rivals, desc_limit=大) で
    full 説明文を軸 run 時のみ再取得（共有キャッシュは汚さない）。

出力:
  ~/.config/{app_id}/benchmark/description.json
  {
    "generated_at": "...",
    "no_data": false,            # 説明文が取れず分析不能なら True（スプシ fallback 等）
    "reason": "",                # no_data の理由
    "source": "youtube_api_full" | "youtube_api_cache" | "competitor_cache",
    "per_channel": {
      "<channelId>": {
        "channel": "...",
        "opening_hooks": ["冒頭フックの文型"],
        "structure_blocks": ["opening","tracklist","cta","links","hashtags","boilerplate"],
        "cta_patterns": ["..."],
        "hashtag_clusters": ["#..."],
        "timestamp_usage": "tracklist のタイムコード書式の特徴",
        "link_policy": "リンク配置・誘導の方針",
        "examples": [{"snippet": "...", "why": "..."}]
      }
    },
    "aggregate": {
      "shared_structure": [...],
      "differentiators": [{"channel": "...", "edge": "..."}],
      "underserved_patterns": [...],
      "recommendation_for_self": {
        "description_template": "英語の投稿文スケルトン（プレースホルダ入り）",
        "opening_hook": "英語の冒頭フック",
        "cta_block": "英語の CTA ブロック",
        "hashtag_set": ["#en", "#tags"],
        "tone_one_line": "日本語の方針 1 行"
      }
    }
  }

分析文・per_channel は日本語、recommendation_for_self のテンプレ系（description_template /
opening_hook / cta_block / hashtag_set）は YouTube 出力メタとして英語、tone_one_line のみ日本語。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
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
ANALYSIS_FILE = BENCHMARK_DIR / "description.json"
COMPETITOR_CACHE_FILE = CONFIG_DIR / "competitor_analysis_cache.json"
ANALYSIS_FILENAME = "description.json"

DEFAULT_CLI = "claude"
ANALYSIS_TIMEOUT = 300
DESC_LIMIT_FULL = 2000   # full 再取得時の説明文上限（500字版では後半が欠落するため）
DESC_MIN_FOR_ANALYSIS = 40  # 構成分析に足る最低文字数（これ未満は no-data 扱い）


# ─── キャッシュ I/O ────────────────────────────────

def _ensure_dirs() -> None:
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)


def load_cache() -> dict:
    empty = {"per_channel": {}, "aggregate": {}, "generated_at": "", "no_data": False}
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
        d.setdefault("per_channel", {})
        d.setdefault("aggregate", {})
        d.setdefault("generated_at", "")
        d.setdefault("no_data", False)
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


def _safe_id(s: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in (s or "_"))[:64]


# ─── JSON 抽出 ─────────────────────────────────────

def _extract_json(text: str) -> Optional[dict]:
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


# ─── 競合データ取得（full 再取得 or キャッシュ） ───────

def _count_desc(competitor_data: dict) -> int:
    """説明文（DESC_MIN_FOR_ANALYSIS 以上）を持つ動画数。degrade 判定用。"""
    n = 0
    for ch in (competitor_data.get("channels") or []):
        for key in ("topByViews", "recentUploads"):
            for v in (ch.get(key) or []):
                if len((v.get("description") or "").strip()) >= DESC_MIN_FOR_ANALYSIS:
                    n += 1
    return n


def _resolve_competitor_data(refetch_full: bool) -> tuple[dict, str]:
    """投稿文軸用の competitor_data を解決し (data, source) を返す。

    refetch_full=True かつ rival_channels(API) が設定済みなら full 説明文を再取得。
    それ以外は共有キャッシュ（500字版）を使用。説明文が無ければ呼び出し側で degrade。
    """
    if refetch_full:
        try:
            import app_competitor as _ac
            cfg = _ac._load_analysis_config()
            rivals = cfg.get("rival_channels") or []
            if rivals:
                print(f"  📡 投稿文軸: full 説明文を再取得（{len(rivals)} ch / desc_limit={DESC_LIMIT_FULL}）")
                data = _ac.fetch_competitor_data(rivals, desc_limit=DESC_LIMIT_FULL)
                # channels があっても説明文が全て空/短い場合は full を採用せず、
                # 説明文を持つ共有キャッシュ（過去の500字版）へ二段フォールバックする
                # （偽 no_data 防止: 「データはあるのに投稿文なし」になる劣化を避ける）。
                if data and (data.get("channels") or []) and _count_desc(data) > 0:
                    return data, "youtube_api_full"
                print("  ⚠ full 再取得に有効な説明文が無い → 共有キャッシュにフォールバック")
            else:
                print("  ℹ rival_channels 未設定 → full 再取得せず共有キャッシュを使用")
        except Exception as e:
            print(f"  ⚠ full 再取得失敗（{e}） → 共有キャッシュにフォールバック")
    outer = _load_competitor_cache() or {}
    data = outer.get("competitor_data") or {}
    src = (outer.get("source") or "competitor_cache")
    return data, src


# ─── プロンプト構築 ────────────────────────────────

def _build_per_channel_prompt(channel: dict, per_channel_cap: int) -> str:
    ch_name = channel.get("channelName", "")
    top = (channel.get("topByViews") or [])[:per_channel_cap]
    recent = (channel.get("recentUploads") or [])[:per_channel_cap]

    def _fmt(items, label):
        if not items:
            return f"({label} なし)"
        lines = []
        for v in items:
            title = (v.get("title") or "").strip()
            views = int(v.get("viewCount") or 0)
            desc = (v.get("description") or "").strip()
            if not desc:
                continue
            lines.append(f"  ▼ [{views:>10,}] {title}")
            lines.append("    --- description ---")
            for ln in desc.splitlines():
                lines.append(f"    {ln}")
            lines.append("    --- /description ---")
        return "\n".join(lines) if lines else f"({label} に説明文なし)"

    return f"""あなたは YouTube の投稿文（動画説明欄）の構成を分析するエキスパートです。
チャンネル「{ch_name}」の動画説明文を読み、**どんな構成・型で投稿文を書いているか**を抽出してください。
題材の良し悪しではなく「投稿文の組み立て方（冒頭フック・トラックリスト書式・CTA・ハッシュタグ運用・リンク配置・定型ブロック）」が対象です。

## 入力（再生数 TOP と直近投稿の説明文本文）
### 再生数 TOP {len(top)}
{_fmt(top, "TOP")}

### 直近投稿 {len(recent)} 本
{_fmt(recent, "Recent")}

## 出力ルール
- 分析文・理由文はすべて自然な日本語で書く
- 説明文の「文章の中身」ではなく「構造・型・運用パターン」を抽出する
- structure_blocks は説明文の上から下への構成順をブロック名で並べる（例: opening → tracklist → cta → links → hashtags → boilerplate）
- examples は構成が最もよく表れている説明文の冒頭抜粋（snippet は 2〜3 行、原文ママでよい）と、その狙い（why）

## 出力（単一 JSON、余計な文章なし）
```json
{{
  "channel": "{ch_name}",
  "opening_hooks": ["冒頭の引き込み文型（例: 視聴シーンへの語りかけ→効能の提示）"],
  "structure_blocks": ["opening", "tracklist", "cta", "links", "hashtags", "boilerplate"],
  "cta_patterns": ["登録/通知/コメント誘導の型"],
  "hashtag_clusters": ["#頻出ハッシュタグ群"],
  "timestamp_usage": "タイムコード/トラックリストの書式特徴（無ければ「なし」）",
  "link_policy": "リンク（SNS/プレイリスト/メンバーシップ）配置の方針",
  "examples": [
    {{"snippet": "説明文の冒頭2〜3行の抜粋", "why": "この構成がなぜ効いているか"}}
  ]
}}
```"""


def _build_aggregate_prompt(per_channel_results: list[dict], self_persona: str = "") -> str:
    summaries = []
    for r in per_channel_results:
        summaries.append(
            f"- {r.get('channel','?')}: hooks={r.get('opening_hooks',[])}, "
            f"blocks={r.get('structure_blocks',[])}, "
            f"cta={r.get('cta_patterns',[])}, "
            f"hashtags={r.get('hashtag_clusters',[])}, "
            f"timestamp={r.get('timestamp_usage','')}, link={r.get('link_policy','')}"
        )
    block = "\n".join(summaries) or "(no per-channel results)"
    persona_block = f"\n## 自チャンネルの軸（参考）\n{self_persona}\n" if self_persona else ""

    return f"""下記は複数 YouTube チャンネルの「投稿文（説明欄）の構成」分析サマリーです。
横断的な共通構成・差別化・未活用パターンを抽出し、自チャンネルが採用すべき**投稿文テンプレート**を提案してください。

## チャンネル別サマリー
{block}
{persona_block}
## 出力ルール
- shared_structure / differentiators / underserved_patterns / tone_one_line は自然な日本語で書く
- recommendation_for_self の description_template / opening_hook / cta_block / hashtag_set は、
  そのまま YouTube に貼れる **自然な英語**で書く（プレースホルダは [TRACKLIST] のように明示）
- description_template は opening → tracklist プレースホルダ → cta → hashtags の順を含む実用スケルトン

## 出力（単一 JSON、余計な文章なし）
```json
{{
  "shared_structure": ["業界横断で共通する投稿文の構成要素"],
  "differentiators": [
    {{"channel": "...", "edge": "投稿文の組み立てで差別化されている点"}}
  ],
  "underserved_patterns": ["まだ誰もやっていない投稿文の工夫"],
  "recommendation_for_self": {{
    "description_template": "Full English description skeleton with [PLACEHOLDERS] (opening / [TRACKLIST] / CTA / hashtags)",
    "opening_hook": "English opening line that speaks to the viewer's moment",
    "cta_block": "English subscribe/notify/comment CTA block",
    "hashtag_set": ["#study", "#lofi", "#focus"],
    "tone_one_line": "自チャンネルの投稿文トーンを 1 文（日本語）"
  }}
}}
```"""


# ─── Claude CLI ────────────────────────────────────

def _run_claude(cli_cmd: str, prompt: str, timeout: int = ANALYSIS_TIMEOUT) -> str:
    # Claude→Codex フォールバック共通ランナーに委譲（全機能のバックアップ回路）
    from app_llm_runner import run_llm
    return run_llm(prompt, cli_cmd=cli_cmd, timeout=timeout, label="description")


# ─── 分析 ─────────────────────────────────────────

def analyze_descriptions(competitor_data: Optional[dict] = None,
                         cli_cmd: str = DEFAULT_CLI,
                         per_channel_cap: int = 8,
                         self_persona: str = "",
                         refetch_full: bool = True,
                         only_channel_ids=None,
                         skip_unchanged: bool = True) -> dict:
    """各チャンネルの投稿文構成 → 横断 aggregate を生成。説明文が無ければ no_data。"""
    from app_benchmark_common import channel_fingerprint, plan_channel, stamp_meta, normalize_ids
    source = "competitor_cache"
    if competitor_data is None:
        competitor_data, source = _resolve_competitor_data(refetch_full)

    channels = competitor_data.get("channels") or []
    if not channels:
        return {"per_channel": {}, "aggregate": {}, "no_data": True, "source": source,
                "reason": "competitor_data.channels が空です。先に競合データ取得（ライバルチャンネル登録 or ベンチマーク取り込み）を実行してください。"}

    desc_videos = _count_desc(competitor_data)
    if desc_videos == 0:
        return {"per_channel": {}, "aggregate": {}, "no_data": True, "source": source,
                "reason": "説明文データがありません。投稿文分析は YouTube API（ライバルチャンネル）経路でのみ可能です。スプレッドシート fallback 経路では動画説明文が取得できません。"}

    only_ids = normalize_ids(only_channel_ids)
    existing = (load_cache() or {}).get("per_channel", {}) or {}
    per_channel: dict[str, dict] = {}
    n_run = n_skip = 0

    for ch in channels:
        ch_id = ch.get("channelId") or _safe_id(ch.get("channelName", ""))
        ch_name = ch.get("channelName", "")
        fp = channel_fingerprint(ch)
        if plan_channel(ch_id, fp, existing, only_ids, skip_unchanged, ch_name) == "carry":
            if ch_id in existing:
                per_channel[ch_id] = existing[ch_id]
                n_skip += 1
            continue
        prompt = _build_per_channel_prompt(ch, per_channel_cap)
        try:
            raw = _run_claude(cli_cmd, prompt)
        except RuntimeError as e:
            print(f"  ⚠ {ch_name}: {e}")
            if ch_id in existing:
                per_channel[ch_id] = existing[ch_id]
            continue
        obj = _extract_json(raw)
        if not obj:
            print(f"  ⚠ {ch_name}: JSON 抽出失敗（説明文が空の可能性）")
            if ch_id in existing:
                per_channel[ch_id] = existing[ch_id]
            continue
        per_channel[ch_id] = stamp_meta(obj, fp)
        n_run += 1
        print(f"  ✓ {ch_name}: 投稿文構成 抽出完了")

    summaries = [{"channel": (v.get("channel") or k), **{kk: vv for kk, vv in v.items() if kk != "_meta"}}
                 for k, v in per_channel.items()]
    print(f"  ◎ 再分析 {n_run} / 流用 {n_skip} / 計 {len(per_channel)}")

    aggregate: dict = {}
    if summaries:
        try:
            agg_prompt = _build_aggregate_prompt(summaries, self_persona=self_persona)
            raw = _run_claude(cli_cmd, agg_prompt)
            aggregate = _extract_json(raw) or {}
            print("  ✓ aggregate 完了")
        except Exception as e:
            print(f"  ⚠ aggregate 失敗: {e}")

    no_data = not per_channel
    reason = "" if per_channel else "全チャンネルで投稿文構成を抽出できませんでした（説明文が空・短い可能性）。"
    return {"per_channel": per_channel, "aggregate": aggregate,
            "no_data": no_data, "reason": reason, "source": source}


def run_full(cli_cmd: str = DEFAULT_CLI, per_channel_cap: int = 8,
             self_persona: str = "", refetch_full: bool = True,
             only_channel_ids=None, skip_unchanged: bool = True) -> dict:
    """エントリポイント: 競合データ（full 再取得 or キャッシュ）をソースに分析 → 保存。"""
    print("🧠 投稿文（説明文）軸分析を開始")
    result = analyze_descriptions(cli_cmd=cli_cmd, per_channel_cap=per_channel_cap,
                                  self_persona=self_persona, refetch_full=refetch_full,
                                  only_channel_ids=only_channel_ids,
                                  skip_unchanged=skip_unchanged)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **result,
    }
    save_cache(payload)
    print(f"✅ description.json 保存: {ANALYSIS_FILE} (no_data={payload.get('no_data')}, source={payload.get('source')})")
    return payload


# ─── 外部参照 API（生成 / 他軸で使う） ──────────────

def get_aggregate() -> dict:
    """propose_* に注入する投稿文軸 aggregate を返す。"""
    return load_cache().get("aggregate", {})


def get_description_scaffolds() -> dict:
    """生成側が使う投稿文スキャフォールド（recommendation_for_self）を返す。

    {description_template, opening_hook, cta_block, hashtag_set, tone_one_line}。
    no_data または未生成なら空 dict。"""
    cache = load_cache()
    if cache.get("no_data"):
        return {}
    agg = cache.get("aggregate") or {}
    rec = agg.get("recommendation_for_self") or {}
    return rec if isinstance(rec, dict) else {}


# ─── CLI ───────────────────────────────────────────

def _main():
    import argparse
    p = argparse.ArgumentParser(description="ベンチマーク・投稿文（説明文）軸分析")
    p.add_argument("--cli", default=DEFAULT_CLI)
    p.add_argument("--per-channel-cap", type=int, default=8)
    p.add_argument("--persona", default="", help="自チャンネルのペルソナ（aggregate に渡す）")
    p.add_argument("--no-refetch", action="store_true",
                   help="full 再取得せず共有キャッシュ（500字版）で分析")
    p.add_argument("--show-scaffolds", action="store_true",
                   help="recommendation_for_self を JSON で stdout 出力")
    args = p.parse_args()

    if args.show_scaffolds:
        print(json.dumps(get_description_scaffolds(), ensure_ascii=False, indent=2))
        return

    run_full(cli_cmd=args.cli, per_channel_cap=args.per_channel_cap,
             self_persona=args.persona, refetch_full=not args.no_refetch)


if __name__ == "__main__":
    _main()
