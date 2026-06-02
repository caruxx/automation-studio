#!/usr/bin/env python3
"""生成済みサムネ群を Claude Vision で 100点満点採点 + 自動承認判定。

総合スコア構成 (100点):
  - concept_fit (30): 対象動画コンセプトとの一致度
  - trend_fit (20): 競合・ベンチマークのトレンドと整合
  - competitor_diff (25): 競合と差別化できているか
  - past_perf (25): 自チャンネル過去動画とのシリーズ一貫性

加えて CTR 予測 (1-10%) と類似度 (0-100%) も同時抽出。

入力: 生成画像 paths + 競合 reference paths + 動画 context (title/concept/persona)
出力: [{filename, score_total, score_breakdown, ctr_predict, similarity, comment}]
"""
from __future__ import annotations

import datetime as _dt
import re
import subprocess
import shutil
from pathlib import Path
from typing import Optional


# 採点ウェイト (合計 100)
WEIGHTS = {
    "concept_fit": 30,
    "trend_fit": 20,
    "competitor_diff": 25,
    "past_perf": 25,
}

# 評価ステータス
STATUS_AUTO_APPROVED = "auto_approved"
STATUS_NEEDS_REVIEW = "needs_review"
STATUS_REJECTED = "rejected"
STATUS_PENDING = "pending"


def _build_scoring_prompt(
    generated_paths: list[Path],
    competitor_paths: list[Path],
    video_title: str,
    video_concept: str,
    channel_name: str = "",
    persona: str = "",
) -> str:
    gen_block = "\n".join(f"  - {p}" for p in generated_paths)
    comp_block = "\n".join(f"  - {p}" for p in competitor_paths) if competitor_paths else "  (なし)"
    persona_note = (
        f"## チャンネル文脈\nチャンネル名: {channel_name}\nペルソナ:\n{persona}\n"
        if persona.strip() else
        f"## チャンネル文脈\nチャンネル名: {channel_name or '(不明)'}\nペルソナ: 未設定（具体ジャンル名を勝手に推測しないこと）\n"
    )

    return f"""あなたは YouTube サムネイル評価の専門家です。
以下の **生成画像群** と **競合参照画像群** を必ず Read ツールで開いて視覚的に分析し、
各生成画像に対して 100 点満点で採点してください。

## 対象動画
- タイトル: {video_title}
- コンセプト: {(video_concept or "(未設定)")[:300]}

{persona_note}

## 評価対象（生成画像、必ず Read で読込）
{gen_block}

## 参照する競合画像（Read で読込）
{comp_block}

## 採点ルール (合計 100 点)
1. **concept_fit (30 点満点)**: 対象動画のタイトル・コンセプトとの視覚的一致度
2. **trend_fit (20 点満点)**: 競合・ベンチマークのトレンド（構図・色・ライティング）と整合しているか
3. **competitor_diff (25 点満点)**: 競合と差別化できているか（同一視されない / 固有要素のコピーが無いか）
4. **past_perf (25 点満点)**: シリーズ的一貫性 + 視認性（YouTube サムネとして縮小時に強いか）

加えて以下も抽出:
- **ctr_predict** (1.0〜15.0): 予想 CTR (%)。視認性 + 競合差別化 + コンセプト合致から推定
- **similarity** (0〜100): 競合参照画像との視覚的類似度 (%)。固有要素まで似ていれば 50% 以上、要素抽出に留まれば 10〜30%
- **comment**: 1〜2 文の評価コメント（日本語）
- **strengths**: 効いている点 (配列、最大 3 件)
- **weaknesses**: 弱点・改善点 (配列、最大 3 件)

## 出力 (単一 JSON / 余計な文章なし)
```json
{{
  "evaluations": [
    {{
      "filename": "(generated_paths のファイル名のみ)",
      "score_breakdown": {{
        "concept_fit": 28,
        "trend_fit": 18,
        "competitor_diff": 22,
        "past_perf": 24
      }},
      "score_total": 92,
      "ctr_predict": 8.2,
      "similarity": 18,
      "comment": "主役の視認性が高く...",
      "strengths": ["...", "..."],
      "weaknesses": ["..."]
    }}
  ]
}}
```

注意:
- 各画像を **必ず実際に見て**、推測でなく視覚的特徴に基づいて採点する。
- score_total は score_breakdown の単純合計 (concept_fit + trend_fit + competitor_diff + past_perf)。
- ジャンル名（jazz / lofi 等）は persona に明記が無い限り使わない。"""


def _extract_json(text: str) -> Optional[dict]:
    """Vision レスポンスから JSON 抽出 (```json fence or 単独 JSON)。"""
    import json
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    cand = fence.group(1) if fence else text
    s, e = cand.find("{"), cand.rfind("}")
    if s < 0 or e <= s:
        return None
    blob = cand[s:e + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


def _run_claude_vision(cli_cmd: str, prompt: str, image_paths: list[Path],
                       timeout: int = 600) -> str:
    # Claude→Codex フォールバック共通ランナー(Vision)に委譲（全機能のバックアップ回路）
    from app_llm_runner import run_llm_vision
    return run_llm_vision(prompt, image_paths, cli_cmd=cli_cmd, timeout=timeout, label="score-vision")


def score_thumbnails(
    generated_paths: list[Path],
    competitor_paths: list[Path],
    video_title: str,
    video_concept: str = "",
    channel_name: str = "",
    persona: str = "",
    cli_cmd: str = "claude",
    timeout: int = 600,
) -> dict:
    """N 枚の生成画像をまとめて Vision で採点。

    Returns: {"evaluations": [...], "raw": "...", "error": "" or "..."}
    """
    gen = [p for p in generated_paths if Path(p).exists()]
    if not gen:
        return {"evaluations": [], "raw": "", "error": "no generated images"}
    comp = [p for p in competitor_paths if Path(p).exists()]
    prompt = _build_scoring_prompt(gen, comp, video_title, video_concept,
                                   channel_name=channel_name, persona=persona)
    # Read 対象: 生成 + 競合
    all_paths = gen + comp
    try:
        raw = _run_claude_vision(cli_cmd, prompt, all_paths, timeout=timeout)
    except Exception as e:
        return {"evaluations": [], "raw": "", "error": f"{type(e).__name__}: {str(e)[:200]}"}

    obj = _extract_json(raw) or {}
    evals = obj.get("evaluations") or []
    # filename を basename だけにする (Vision が full path を返した場合の保険)
    for e in evals:
        fn = e.get("filename", "")
        if "/" in fn:
            e["filename"] = Path(fn).name
        # score_total 再計算（Vision の合計ミスを防ぐ）
        sb = e.get("score_breakdown") or {}
        try:
            recomputed = sum(int(sb.get(k, 0) or 0) for k in WEIGHTS.keys())
            if abs(recomputed - int(e.get("score_total", 0) or 0)) > 5:
                e["score_total"] = recomputed
        except Exception:
            pass
        e["evaluated_at"] = _dt.datetime.now().isoformat(timespec="seconds")
    return {"evaluations": evals, "raw": raw[:4000], "error": ""}


# ─── 自動承認判定 ─────────────────────

def judge_auto_approval(
    evaluations: list[dict],
    *,
    mode: str = "conditional",
    score_threshold: int = 80,
    ctr_threshold: float = 7.0,
    similarity_max: int = 25,
) -> list[dict]:
    """評価結果から各画像の自動承認ステータスを判定。

    mode:
      - manual: 全て needs_review (採点だけ反映、自動承認しない)
      - conditional: スコア閾値 + CTR 閾値 + 類似度上限 を満たすなら auto_approved
      - auto: 各動画 (= evaluations 全体) で最高スコアを auto_approved、他は needs_review

    Returns: 同じ evaluations に status / approval_reason を付与した新 list
    """
    out = []
    if mode == "auto":
        # 最高スコアを 1 件だけ auto_approved
        best_idx = max(range(len(evaluations)),
                       key=lambda i: int((evaluations[i] or {}).get("score_total", 0) or 0),
                       default=None) if evaluations else None
        for i, e in enumerate(evaluations):
            ee = dict(e)
            if i == best_idx:
                ee["status"] = STATUS_AUTO_APPROVED
                ee["approval_reason"] = "完全自動モード: 最高スコア"
            else:
                ee["status"] = STATUS_NEEDS_REVIEW
                ee["approval_reason"] = "完全自動モード: 最高ではない"
            out.append(ee)
        return out

    if mode == "manual":
        for e in evaluations:
            ee = dict(e)
            ee["status"] = STATUS_NEEDS_REVIEW
            ee["approval_reason"] = "手動モード: 承認は手動で行う"
            out.append(ee)
        return out

    # conditional (デフォルト)
    for e in evaluations:
        ee = dict(e)
        score = int(ee.get("score_total", 0) or 0)
        ctr = float(ee.get("ctr_predict", 0) or 0)
        sim = float(ee.get("similarity", 0) or 0)
        reasons_fail = []
        if score < score_threshold:
            reasons_fail.append(f"スコア {score} が閾値 {score_threshold} 未満")
        if ctr < ctr_threshold:
            reasons_fail.append(f"CTR 予測 {ctr:.1f}% が閾値 {ctr_threshold:.1f}% 未満")
        if sim > similarity_max:
            reasons_fail.append(f"類似度 {sim:.0f}% が上限 {similarity_max}% 超過")
        if not reasons_fail:
            ee["status"] = STATUS_AUTO_APPROVED
            ee["approval_reason"] = (
                f"スコア {score}/閾値 {score_threshold} / "
                f"CTR {ctr:.1f}%/閾値 {ctr_threshold:.1f}% / "
                f"類似度 {sim:.0f}%/上限 {similarity_max}% を全てクリア"
            )
        else:
            ee["status"] = STATUS_NEEDS_REVIEW
            ee["approval_reason"] = "要確認: " + " / ".join(reasons_fail)
        out.append(ee)
    return out


# ─── ヘルパー ─────────────────────

def best_of(evaluations: list[dict]) -> Optional[dict]:
    """評価リストから最高 score_total を返す。"""
    if not evaluations:
        return None
    return max(evaluations,
               key=lambda e: int((e or {}).get("score_total", 0) or 0))


def status_counts(evaluations: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for e in evaluations:
        s = (e or {}).get("status") or STATUS_PENDING
        counts[s] = counts.get(s, 0) + 1
    return counts
