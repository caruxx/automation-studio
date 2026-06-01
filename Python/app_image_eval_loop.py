#!/usr/bin/env python3
"""自己評価付き反復画像生成ループ（核ロジック）。

設計: IMAGE_EVAL_LOOP_PLAN.md。生成→ベンチ比較スコア→合格まで再生成 + 弱点フィードバック還流。

【S0 = この版】生成(generate_fn)と採点(score_fn)は**コールバック注入**で受け取り、
本モジュールは「判定・上限・中断・フィードバック還流」の純粋ロジックだけを持つ。
UI(app.py の async ワーカー)と pipeline(app_pipeline.py の同期 step)の両方から
同じ run_eval_loop() を呼べるよう、生成方式の違いを generate_fn に閉じ込める。

⚠ 安全境界:
  - judge は **conditional 固定**（auto は最高1枚を必ず合格にして品質ゲートにならない）。
  - 0枚生成 / infra_error は generation_failure として**即 break**（クォータ切れの無限ループ防止）。
  - コスト二重上限（max_total_generated / max_vision_calls）で線形増を頭打ち。
  - 合格0で max 到達しても **自動 adopt しない**（best を温存・手動承認の余地を残す）。
  - 既存 score_thumbnails / judge_auto_approval / app_thumbnail_state を再利用（無改変）。

第1版の類似度は既存 Vision スコア(score_total/ctr_predict/similarity)のみ。
CLIP/embedding の機械距離は将来追加（環境が素の Python 3.9 で torch 未導入のため見送り）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# 既定閾値は app_thumbnail_state を単一情報源として import（新定義源を作らない）
try:
    from app_thumbnail_state import (
        DEFAULT_SCORE_THRESHOLD,
        DEFAULT_CTR_THRESHOLD,
        DEFAULT_SIMILARITY_MAX,
    )
except Exception:  # pragma: no cover - import 不能環境でも本体は動く
    DEFAULT_SCORE_THRESHOLD = 80
    DEFAULT_CTR_THRESHOLD = 7.0
    DEFAULT_SIMILARITY_MAX = 25

STATUS_AUTO_APPROVED = "auto_approved"


@dataclass
class GenResult:
    """generate_fn の戻り。0枚 or infra_error は生成基盤エラーとして扱う。"""
    generated_files: list = field(default_factory=list)   # list[Path]
    infra_error: str = ""   # "" / "quota" / "auth" / "<msg>"


@dataclass
class ScoreResult:
    """score_fn の戻り。app_thumbnail_scoring.score_thumbnails の {evaluations, raw, error} を包む。"""
    evaluations: list = field(default_factory=list)   # list[dict]
    error: str = ""


@dataclass
class LoopResult:
    passed: bool = False
    attempts_used: int = 0
    total_generated: int = 0
    vision_calls: int = 0
    approved_files: list = field(default_factory=list)   # 累積 auto_approved filename
    best: Optional[dict] = None
    abort_reason: str = ""   # "" / generation_failure / cost_cap / stopped
    all_evaluations: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "passed": self.passed,
            "attempts_used": self.attempts_used,
            "total_generated": self.total_generated,
            "vision_calls": self.vision_calls,
            "approved_files": list(self.approved_files),
            "best": self.best,
            "abort_reason": self.abort_reason,
            "all_evaluations": list(self.all_evaluations),
        }


# ─── フィードバック還流（不合格周回 → 次プロンプト引数） ───
def _top_terms(terms: list, k: int = 3) -> list:
    """頻度順 dedup で上位 k 件。"""
    seen = {}
    for t in terms:
        t = (t or "").strip()
        if not t:
            continue
        seen[t] = seen.get(t, 0) + 1
    return [t for t, _ in sorted(seen.items(), key=lambda kv: -kv[1])][:k]


def build_feedback(judged: list, best: Optional[dict], concept_hint_base: str,
                   thumbnail_axis: dict, max_hint_len: int = 600) -> tuple:
    """不合格評価から次周回の (concept_hint, thumbnail_axis) を作る。

    - weaknesses(不合格) → concept_hint に " Improve on: ..." 追記（base 起点・累積膨張防止）
    - 類似度超過/コピー系 → thumbnail_axis["avoid"] に追記（次プロンプトの Avoid: 行へ）
    - best.strengths → concept_hint に " Keep: ..." 追記（効いた点を維持＝山登り）
    """
    weak_terms = []
    sim_fail = False
    for e in judged:
        if e.get("status") != STATUS_AUTO_APPROVED:
            weak_terms += (e.get("weaknesses") or [])
            reason = e.get("approval_reason") or ""
            if "類似" in reason or "similar" in reason.lower():
                sim_fail = True

    hint = concept_hint_base
    improve = _top_terms(weak_terms, 3)
    if improve:
        hint = f"{hint} Improve on: {'; '.join(improve)}"
    if best and best.get("strengths"):
        keep = _top_terms(best.get("strengths") or [], 2)
        if keep:
            hint = f"{hint} Keep: {'; '.join(keep)}"
    hint = hint[:max_hint_len]

    new_axis = dict(thumbnail_axis or {})
    if sim_fail:
        prev_avoid = list(new_axis.get("avoid") or [])
        for extra in ("copying competitor-specific motifs", "verbatim layout of reference"):
            if extra not in prev_avoid:
                prev_avoid.append(extra)
        new_axis["avoid"] = prev_avoid
    return hint, new_axis


# ─── ループ本体 ───
def run_eval_loop(
    *,
    # 注入: 生成と採点（UI=async/pipeline=同期 の違いを呼び出し側に閉じ込める）
    generate_fn: Callable[[str, dict, int], GenResult],   # (concept_hint, thumbnail_axis, attempt) -> GenResult
    score_fn: Callable[[list], ScoreResult],              # (generated_files) -> ScoreResult
    judge_fn: Callable[..., list],                        # = app_thumbnail_scoring.judge_auto_approval
    status_counts_fn: Callable[[list], dict],             # = app_thumbnail_scoring.status_counts
    best_of_fn: Callable[[list], Optional[dict]],         # = app_thumbnail_scoring.best_of
    # 評価対象の文脈
    concept_hint_base: str,
    thumbnail_axis: dict,
    # 合格基準（既定は app_thumbnail_state.DEFAULT_* を参照）
    score_threshold: int = DEFAULT_SCORE_THRESHOLD,
    ctr_threshold: float = DEFAULT_CTR_THRESHOLD,
    similarity_max: int = DEFAULT_SIMILARITY_MAX,
    required_pass: int = 1,
    # 上限（安全弁）
    max_attempts: int = 2,
    max_total_generated: int = 8,
    max_vision_calls: int = 3,
    # フィードバック ON/OFF（S2 で実効化。S0 では引数だけ用意）
    feedback: bool = True,
    # 状態・中断
    on_attempt: Optional[Callable[[dict], None]] = None,  # 周回ごとの状態通知（state upsert 等は呼び出し側）
    should_stop: Callable[[], bool] = lambda: False,
    log_fn: Callable[[str], None] = print,
) -> LoopResult:
    """合格(auto_approved>=required_pass)するまで 生成→採点→判定 を最大 max_attempts 回。

    judge は conditional 固定（auto/manual を渡されても conditional で判定）。
    停止条件は呼び出し側で status_counts の auto_approved を自前判定（judge は全件
    needs_review でも例外を投げない）。
    """
    res = LoopResult()
    concept_hint = concept_hint_base
    axis = dict(thumbnail_axis or {})
    approved_set: set = set()

    for attempt in range(1, max_attempts + 1):
        # コスト上限・中断（周回前）
        if should_stop():
            res.abort_reason = "stopped"
            log_fn(f"  ⏹ eval-loop: 中断要求で停止（attempt={attempt}）")
            break
        if res.total_generated >= max_total_generated or res.vision_calls >= max_vision_calls:
            res.abort_reason = "cost_cap"
            log_fn(f"  ⛔ eval-loop: コスト上限到達（gen={res.total_generated}/{max_total_generated}, "
                   f"vision={res.vision_calls}/{max_vision_calls}）")
            break

        res.attempts_used = attempt
        log_fn(f"  🔁 eval-loop attempt {attempt}/{max_attempts}")

        # 生成（注入）
        gen = generate_fn(concept_hint, axis, attempt)
        if gen.infra_error:
            res.abort_reason = "generation_failure"
            log_fn(f"  ⛔ eval-loop: 生成基盤エラー（{gen.infra_error}）→ 中断（再試行しない）")
            break
        files = list(gen.generated_files or [])
        if not files:
            res.abort_reason = "generation_failure"
            log_fn("  ⛔ eval-loop: 生成0枚 → 生成基盤エラー扱いで中断（クォータ切れ等）")
            break
        res.total_generated += len(files)

        # 採点（注入）
        sc = score_fn(files)
        res.vision_calls += 1
        if sc.error:
            log_fn(f"  ⚠ eval-loop: 採点失敗（{sc.error}）→ この周回はベスト更新せず次へ")
            if on_attempt:
                on_attempt({"attempt": attempt, "files": files, "evaluations": [],
                            "score_error": sc.error})
            continue

        # 判定（conditional 固定）
        judged = judge_fn(
            sc.evaluations, mode="conditional",
            score_threshold=score_threshold, ctr_threshold=ctr_threshold,
            similarity_max=similarity_max,
        )
        res.all_evaluations.extend(judged)

        # 周回の合格を自前集計（judge は合格0でも例外を投げない）
        counts = status_counts_fn(judged)
        approved_now = [e.get("filename") for e in judged
                        if e.get("status") == STATUS_AUTO_APPROVED and e.get("filename")]
        for f in approved_now:
            approved_set.add(f)
        res.approved_files = list(approved_set)
        res.best = best_of_fn(res.all_evaluations)

        log_fn(f"  📊 eval-loop attempt {attempt}: 合格 {len(approved_now)} / "
               f"判定 {sum(counts.values())}（累積合格 {len(approved_set)}）"
               f"{(' best=' + str(round(res.best.get('score_total', 0), 1))) if res.best else ''}")

        if on_attempt:
            on_attempt({"attempt": attempt, "files": files, "evaluations": judged,
                        "approved": approved_now})

        # 停止条件: 必要枚数に達したら合格 break（初合格で確定＝振動前に止める）
        if len(approved_set) >= required_pass:
            res.passed = True
            log_fn(f"  ✅ eval-loop: 合格（{len(approved_set)}/{required_pass}）→ 停止")
            break

        # 不合格 → フィードバック還流して次 attempt
        if feedback:
            concept_hint, axis = build_feedback(judged, res.best, concept_hint_base, axis)

    if not res.passed and not res.abort_reason:
        log_fn(f"  ⚠ eval-loop: {res.attempts_used} 周回で合格に届かず。best を温存（自動採用はしない）")
    return res


if __name__ == "__main__":
    # S0 ドライテスト: 生成・採点をスタブ化し、5分岐を CLI/Vision 無しで確認。
    from app_thumbnail_scoring import judge_auto_approval, status_counts, best_of

    def make_eval(fn, score, ctr, sim, strengths=None, weaknesses=None):
        return {"filename": fn, "score_total": score, "ctr_predict": ctr,
                "similarity": sim, "strengths": strengths or [], "weaknesses": weaknesses or []}

    def run_case(name, gen_seq, score_seq, **kw):
        calls = {"i": 0}
        def gen(hint, axis, attempt):
            r = gen_seq[min(calls["i"], len(gen_seq) - 1)]
            return r
        def score(files):
            s = score_seq[min(calls["i"], len(score_seq) - 1)]
            calls["i"] += 1
            return s
        res = run_eval_loop(
            generate_fn=gen, score_fn=score,
            judge_fn=judge_auto_approval, status_counts_fn=status_counts, best_of_fn=best_of,
            concept_hint_base="cozy cafe", thumbnail_axis={"avoid": []},
            log_fn=lambda m: None, **kw)
        print(f"[{name}] passed={res.passed} attempts={res.attempts_used} "
              f"gen={res.total_generated} vision={res.vision_calls} abort={res.abort_reason!r} "
              f"approved={len(res.approved_files)} best={(res.best or {}).get('score_total')}")
        return res

    print("=== app_image_eval_loop S0 dry tests ===")
    # (a) 1周目で合格
    r = run_case("a:1周合格",
        [GenResult([Path("/x/v1.png")])],
        [ScoreResult([make_eval("v1.png", 85, 8.0, 10)])],
        max_attempts=3)
    assert r.passed and r.attempts_used == 1, r
    # (b) 2周目で合格（1周目は低スコア）
    r = run_case("b:2周合格",
        [GenResult([Path("/x/v1.png")]), GenResult([Path("/x/v2.png")])],
        [ScoreResult([make_eval("v1.png", 60, 5.0, 10, weaknesses=["too dark"])]),
         ScoreResult([make_eval("v2.png", 90, 9.0, 12)])],
        max_attempts=3)
    assert r.passed and r.attempts_used == 2, r
    # (c) max 出し切り不合格 → best 温存
    r = run_case("c:max不合格best温存",
        [GenResult([Path("/x/v1.png")])],
        [ScoreResult([make_eval("v1.png", 70, 6.0, 10, weaknesses=["bland"])])],
        max_attempts=2)
    assert (not r.passed) and r.abort_reason == "" and r.best and r.best["score_total"] == 70, r
    # (d) infra_error → generation_failure 即 break
    r = run_case("d:infra_error",
        [GenResult([], infra_error="quota")],
        [ScoreResult([])],
        max_attempts=3)
    assert (not r.passed) and r.abort_reason == "generation_failure" and r.attempts_used == 1, r
    # (e) cost_cap（max_total_generated で頭打ち）
    r = run_case("e:cost_cap",
        [GenResult([Path("/x/a.png"), Path("/x/b.png"), Path("/x/c.png")])],
        [ScoreResult([make_eval("a.png", 50, 4.0, 10)])],
        max_attempts=5, max_total_generated=2)
    assert (not r.passed) and r.abort_reason == "cost_cap", r
    # フィードバック還流の単体
    hint, axis = build_feedback(
        [make_eval("v1.png", 60, 5, 40, weaknesses=["too busy", "low contrast"])
         | {"status": "needs_review", "approval_reason": "類似度が高すぎ"}],
        {"strengths": ["warm palette"], "score_total": 60},
        "cozy cafe", {"avoid": []})
    assert "Improve on:" in hint and "Keep:" in hint, hint
    assert "copying competitor-specific motifs" in axis["avoid"], axis
    print("feedback hint:", hint)
    print("feedback avoid:", axis["avoid"])
    print("=== ALL S0 DRY TESTS PASSED ===")
