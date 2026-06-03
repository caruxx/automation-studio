#!/usr/bin/env python3
"""ベンチマーク・タイトル軸の分析モジュール（Phase 3）。

入力:
  ~/.config/{app_id}/competitor_analysis_cache.json (competitor_data)

出力:
  ~/.config/{app_id}/benchmark/title.json
  {
    "generated_at": "...",
    "per_channel": {
      "<channelId>": {
        "channel": "...",
        "patterns": [...],            # 構造パターン (例: 場所+時間+感情)
        "formulas": [...],             # 具体テンプレ (例: "[Time] [Place] BGM | [Emotion]")
        "keywords": [...],             # 検索流入を生んでいる単語
        "hooks": [...],                # 視聴者心理フック
        "avg_length": int,
        "examples": [{"title": "...", "why": "..."}],
        "anti_patterns": [...]         # クリックを下げると思われる傾向
      }
    },
    "aggregate": {
      "shared_patterns": [...],
      "winning_formulas": [...],       # 横断で再現性が高そうなテンプレ
      "keyword_clusters": [{"cluster": "...", "examples": [...]}],
      "avoid_patterns": [...],
      "recommendation_for_self": {
        "title_scaffolds": [...],      # 自チャンネルが使える具体テンプレ
        "primary_keywords": [...],
        "avoid_keywords": [...],
        "tone_one_line": "..."
      }
    }
  }

入力に Vision は不要 → タイトル + 再生数のテキストのみで完結。
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
ANALYSIS_FILE = BENCHMARK_DIR / "title.json"
COMPETITOR_CACHE_FILE = CONFIG_DIR / "competitor_analysis_cache.json"
ANALYSIS_FILENAME = "title.json"

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
            likes = int(v.get("likeCount") or 0)
            lines.append(f"  - [v={views:>10,} | l={likes:>7,}] {title}")
        return "\n".join(lines)

    return f"""あなたは YouTube タイトル設計のエキスパートです。
チャンネル「{ch_name}」のタイトル群を **構造パターン・具体テンプレ・キーワード・心理フック** の 4 軸で分解してください。

## 入力（タイトル + viewCount/likeCount）
### 再生数 TOP {len(top)}
{_fmt(top, "TOP")}

### 直近投稿 {len(recent)} 本
{_fmt(recent, "Recent")}

## 抽出ルール
- 分析文・理由文・提案文はすべて自然な日本語で書く（英語だけの出力は禁止）
- patterns: 抽象構造のみ（例: 「場所 + 時間帯 + 感情ジョブ」）
- formulas: そのまま流用できる具体テンプレ。英語テンプレを扱う場合も日本語の意図が分かる形にする
- keywords: 検索流入を生んでいると推測される単語（英語検索語は日本語補足付き）
- hooks: 視聴者がクリックする心理レバー（例: 没入・郷愁・現実逃避）
- avg_length: TOP/Recent 全タイトルの平均文字数（半角=1, 全角=1）
- examples: TOP の中から「タイトル設計が秀逸」と判断する 3 件、各 1 文の理由
- anti_patterns: クリックを下げていそうな傾向（あれば。なければ空配列）

## 出力（単一 JSON、余計な文章なし）
```json
{{
  "channel": "{ch_name}",
  "patterns": ["構造1", "構造2"],
  "formulas": ["テンプレ1", "テンプレ2"],
  "keywords": ["単語1", "単語2"],
  "hooks": ["心理フック1", "心理フック2"],
  "avg_length": 50,
  "examples": [{{"title": "...", "why": "..."}}],
  "anti_patterns": []
}}
```"""


def _build_aggregate_prompt(per_channel_results: list[dict], self_persona: str = "") -> str:
    summaries = []
    for r in per_channel_results:
        summaries.append(
            f"- {r.get('channel','?')}: patterns={r.get('patterns',[])}, "
            f"formulas={r.get('formulas',[])}, keywords={r.get('keywords',[])}, "
            f"hooks={r.get('hooks',[])}, avg_len={r.get('avg_length','?')}"
        )
    block = "\n".join(summaries) or "(no per-channel results)"
    persona_block = f"\n## 自チャンネルの軸（参考）\n{self_persona}\n" if self_persona else ""

    return f"""下記は複数チャンネルそれぞれのタイトル分析サマリーです。
横断的な「再現性が高い勝ちテンプレ」と自チャンネル向けの具体スキャフォールドを抽出してください。

## チャンネル別サマリー
{block}
{persona_block}
## 出力ルール
- 分析文・理由文・提案文はすべて自然な日本語で書く
- title_scaffolds は **そのまま埋めれば 1 本分のタイトルになる具体テンプレ** を 3〜5 件
- primary_keywords は検索ボリュームと self_persona の親和性を踏まえて 5〜10 件
- avoid_keywords は飽和 / 自軸とずれる 3〜5 件
- tone_one_line は自チャンネルが取るべきタイトルの「声色」を 1 文で

## 出力（単一 JSON、余計な文章なし）
```json
{{
  "shared_patterns": ["業界横断で共通する構造"],
  "winning_formulas": ["再現性が高い具体テンプレ"],
  "keyword_clusters": [
    {{"cluster": "クラスタ名（例: time_of_day）", "examples": ["midnight", "late night"]}}
  ],
  "avoid_patterns": ["陳腐化 / クリックを下げる傾向"],
  "recommendation_for_self": {{
    "title_scaffolds": ["[シーン] [時間帯] BGM | [感情ジョブ]"],
    "primary_keywords": ["..."],
    "avoid_keywords": ["..."],
    "tone_one_line": "..."
  }}
}}
```"""


# ─── Claude CLI ────────────────────────────────────

def _run_claude(cli_cmd: str, prompt: str, timeout: int = ANALYSIS_TIMEOUT) -> str:
    # Claude→Codex フォールバック共通ランナーに委譲（全機能のバックアップ回路）
    from app_llm_runner import run_llm
    return run_llm(prompt, cli_cmd=cli_cmd, timeout=timeout, label="title")


# ─── 分析 ─────────────────────────────────────────

def analyze_titles(competitor_data: Optional[dict] = None,
                   cli_cmd: str = DEFAULT_CLI,
                   per_channel_cap: int = 10,
                   self_persona: str = "",
                   only_channel_ids=None,
                   skip_unchanged: bool = True) -> dict:
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
        print(f"  ✓ {ch_name}: タイトル分析完了")

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


def run_full(cli_cmd: str = DEFAULT_CLI, per_channel_cap: int = 10,
             self_persona: str = "", only_channel_ids=None,
             skip_unchanged: bool = True) -> dict:
    print("✏️ タイトル軸分析を開始")
    result = analyze_titles(cli_cmd=cli_cmd, per_channel_cap=per_channel_cap,
                            self_persona=self_persona,
                            only_channel_ids=only_channel_ids,
                            skip_unchanged=skip_unchanged)
    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        **result,
    }
    save_cache(payload)
    print(f"✅ title.json 保存: {ANALYSIS_FILE}")
    return payload


# ─── 外部参照 API（他軸 / メタ提案で使う） ──────────

def get_aggregate() -> dict:
    return load_cache().get("aggregate", {})


def get_title_scaffolds() -> list[str]:
    """propose_titles などで具体テンプレを差し込む用。"""
    agg = get_aggregate() or {}
    rec = agg.get("recommendation_for_self") or {}
    return list(rec.get("title_scaffolds") or [])


def get_primary_keywords() -> list[str]:
    agg = get_aggregate() or {}
    rec = agg.get("recommendation_for_self") or {}
    return list(rec.get("primary_keywords") or [])


# ─── CLI ───────────────────────────────────────────

def _main():
    import argparse
    p = argparse.ArgumentParser(description="ベンチマーク・タイトル分析")
    p.add_argument("--cli", default=DEFAULT_CLI)
    p.add_argument("--per-channel-cap", type=int, default=10)
    p.add_argument("--persona", default="")
    p.add_argument("--show-scaffolds", action="store_true")
    args = p.parse_args()

    if args.show_scaffolds:
        for x in get_title_scaffolds():
            print(x)
        return

    run_full(cli_cmd=args.cli, per_channel_cap=args.per_channel_cap,
             self_persona=args.persona)


if __name__ == "__main__":
    _main()
