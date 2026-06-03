#!/usr/bin/env python3
"""ベンチマーク・コンセプト軸の分析モジュール（Phase 2）。

入力:
  ~/.config/{app_id}/competitor_analysis_cache.json (competitor_data)

出力:
  ~/.config/{app_id}/benchmark/concept.json
  {
    "generated_at": "...",
    "per_channel": {
      "<channelId>": {
        "channel": "...",
        "themes": ["テーマ1", "テーマ2"],          # 繰り返し現れる題材
        "emotional_jobs": ["...", "..."],          # 動画が解決している感情ジョブ
        "scene_anchors": ["...", "..."],           # 繰り返し現れるシーン/状況
        "concept_signature": "1〜2文の要約",
        "examples": [{"title": "...", "why": "..."}]
      }
    },
    "aggregate": {
      "shared_themes": [...],
      "differentiators": [{"channel": "...", "edge": "..."}],
      "underserved_concepts": [...],
      "recommendation_for_self": {
        "focus_themes": [...],
        "avoid_themes": [...],
        "vibe_one_line": "..."
      }
    }
  }

入力に Vision は不要 → タイトル/タグ/description テキストのみで完結。
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
ANALYSIS_FILE = BENCHMARK_DIR / "concept.json"
COMPETITOR_CACHE_FILE = CONFIG_DIR / "competitor_analysis_cache.json"
ANALYSIS_FILENAME = "concept.json"

DEFAULT_CLI = "claude"
ANALYSIS_TIMEOUT = 300


# ─── キャッシュ I/O ────────────────────────────────

def _ensure_dirs() -> None:
    BENCHMARK_DIR.mkdir(parents=True, exist_ok=True)


def load_cache() -> dict:
    empty = {"per_channel": {}, "aggregate": {}, "generated_at": ""}
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
            tags = ", ".join((v.get("tags") or [])[:5])
            desc = (v.get("description") or "").strip()[:160]
            lines.append(f"  - [{views:>10,}] {title}")
            if tags:
                lines.append(f"      tags: {tags}")
            if desc:
                lines.append(f"      desc: {desc}")
        return "\n".join(lines)

    return f"""あなたは YouTube チャンネル分析のエキスパートです。
チャンネル「{ch_name}」のコンセプトを「題材・感情ジョブ・シーン」の 3 軸で抽出してください。

## 入力（タイトル / タグ / 説明文の抜粋）
### 再生数 TOP {len(top)}
{_fmt(top, "TOP")}

### 直近投稿 {len(recent)} 本
{_fmt(recent, "Recent")}

## 出力ルール
- 分析文・理由文・提案文はすべて自然な日本語で書く（英語だけの出力は禁止）
- 表面的なジャンル（「BGM」「Lofi」など）ではなく、**視聴者がどんな瞬間にこのチャンネルを開くか**を視聴者目線で抽出する
- 同一チャンネル内の動画群から **共通する題材・感情ジョブ・シーン** を取り出す
- examples は TOP の中から最も「コンセプトを象徴する」3 件、各 1 文の理由

## 出力（単一 JSON、余計な文章なし）
```json
{{
  "channel": "{ch_name}",
  "themes": ["題材1（例: 静寂の都市夜景）", "題材2"],
  "emotional_jobs": ["この動画が解決している感情ジョブ1（例: 在宅勤務の孤独を BGM で和らげる）", "ジョブ2"],
  "scene_anchors": ["繰り返し現れるシーン1（例: 雨の窓越しのカフェ）", "シーン2"],
  "concept_signature": "このチャンネルの「これ一本」を 1〜2 文で要約",
  "examples": [
    {{"title": "TOP の代表タイトル", "why": "なぜそれがこのチャンネルのコンセプトを象徴するか"}}
  ]
}}
```"""


def _build_aggregate_prompt(per_channel_results: list[dict], self_persona: str = "") -> str:
    summaries = []
    for r in per_channel_results:
        summaries.append(
            f"- {r.get('channel','?')}: themes={r.get('themes',[])}, "
            f"jobs={r.get('emotional_jobs',[])}, "
            f"scenes={r.get('scene_anchors',[])}, "
            f"signature={r.get('concept_signature','')}"
        )
    block = "\n".join(summaries) or "(no per-channel results)"
    persona_block = f"\n## 自チャンネルの軸（参考）\n{self_persona}\n" if self_persona else ""

    return f"""下記は複数 YouTube チャンネルそれぞれのコンセプト分析サマリーです。
横断的な「共通点・差別化軸・未充足コンセプト」を抽出し、自チャンネルが取るべき方向性を提案してください。

## チャンネル別サマリー
{block}
{persona_block}
## 出力ルール
- 分析文・理由文・提案文はすべて自然な日本語で書く（英語だけの出力は禁止）

## 出力（単一 JSON、余計な文章なし）
```json
{{
  "shared_themes": ["業界横断で共通する題材"],
  "differentiators": [
    {{"channel": "...", "edge": "他と差別化されている独自軸"}}
  ],
  "underserved_concepts": ["まだ誰もやっていないコンセプト"],
  "recommendation_for_self": {{
    "focus_themes": ["自チャンネルが狙うべき題材"],
    "avoid_themes": ["既に飽和しているため避けるべき題材"],
    "vibe_one_line": "自チャンネルでの 1 文サマリー"
  }}
}}
```"""


# ─── Claude CLI ────────────────────────────────────

def _run_claude(cli_cmd: str, prompt: str, timeout: int = ANALYSIS_TIMEOUT) -> str:
    # Claude→Codex フォールバック共通ランナーに委譲（全機能のバックアップ回路）
    from app_llm_runner import run_llm
    return run_llm(prompt, cli_cmd=cli_cmd, timeout=timeout, label="concept")


# ─── 分析 ─────────────────────────────────────────

def analyze_concepts(competitor_data: Optional[dict] = None,
                     cli_cmd: str = DEFAULT_CLI,
                     per_channel_cap: int = 8,
                     self_persona: str = "",
                     only_channel_ids=None,
                     skip_unchanged: bool = True) -> dict:
    """各チャンネルのコンセプト → 横断 aggregate を生成。

    only_channel_ids: 指定 ch だけ分析（未指定=全件）。skip_unchanged: 入力指紋が
    既存と一致なら再分析せず流用。既存 per_channel はマージ保持（選択外を消さない）。
    """
    from app_benchmark_common import channel_fingerprint, plan_channel, stamp_meta, normalize_ids
    if competitor_data is None:
        outer = _load_competitor_cache() or {}
        competitor_data = outer.get("competitor_data") or {}

    channels = competitor_data.get("channels") or []
    if not channels:
        raise RuntimeError("competitor_data.channels が空です。先に競合データ取得を実行してください。")

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
            print(f"  ⚠ {ch_name}: JSON 抽出失敗")
            if ch_id in existing:
                per_channel[ch_id] = existing[ch_id]
            continue
        per_channel[ch_id] = stamp_meta(obj, fp)
        n_run += 1
        print(f"  ✓ {ch_name}: コンセプト抽出完了")

    # aggregate は全 per_channel（流用含む）から再構成
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

    return {"per_channel": per_channel, "aggregate": aggregate}


def run_full(cli_cmd: str = DEFAULT_CLI, per_channel_cap: int = 8,
             self_persona: str = "", only_channel_ids=None,
             skip_unchanged: bool = True) -> dict:
    """エントリポイント: 競合キャッシュをソースに分析 → 保存。"""
    print("🧠 コンセプト軸分析を開始")
    result = analyze_concepts(cli_cmd=cli_cmd, per_channel_cap=per_channel_cap,
                              self_persona=self_persona,
                              only_channel_ids=only_channel_ids,
                              skip_unchanged=skip_unchanged)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **result,
    }
    save_cache(payload)
    print(f"✅ concept.json 保存: {ANALYSIS_FILE}")
    return payload


# ─── 外部参照 API（他軸 / 画像提案で使う） ──────────

def get_aggregate() -> dict:
    """他軸の prompt や series 提案に注入するための aggregate を返す。"""
    return load_cache().get("aggregate", {})


def get_focus_themes() -> list[str]:
    """series 提案などで「狙うべき題材」を素早く得る。"""
    agg = get_aggregate() or {}
    rec = agg.get("recommendation_for_self") or {}
    return list(rec.get("focus_themes") or [])


# ─── CLI ───────────────────────────────────────────

def _main():
    import argparse
    p = argparse.ArgumentParser(description="ベンチマーク・コンセプト分析")
    p.add_argument("--cli", default=DEFAULT_CLI)
    p.add_argument("--per-channel-cap", type=int, default=8)
    p.add_argument("--persona", default="", help="自チャンネルのペルソナ（aggregate に渡す）")
    p.add_argument("--show-focus", action="store_true",
                   help="focus_themes を 1 行 1 件で stdout 出力")
    args = p.parse_args()

    if args.show_focus:
        for x in get_focus_themes():
            print(x)
        return

    run_full(cli_cmd=args.cli, per_channel_cap=args.per_channel_cap,
             self_persona=args.persona)


if __name__ == "__main__":
    _main()
