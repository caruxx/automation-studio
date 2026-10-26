"""投稿用サムネの AI 選定（段階 5）。

生成（背景候補）→ 合成（チャンネルの thumbnail_composition）→ 採点（Claude / Codex Vision）→ 判定（conditional）→ 採用。
- 候補は thumbnail_candidates/<run_id>/a<attempt>/ に置き、候補 id は "<run_id>/a<attempt>/<file>"、sha256 を記録する。
- 採点結果は、その attempt の候補集合に一致するファイル名だけを使う（候補外は ignored_evaluations に記録して捨てる）。
- 採用画像は thumbnail_selected.jpg（投稿はこれだけを使う）。動画用の vol{N}.jpg / サムネイル.jpg は変えない。
- 正本は vol フォルダの .thumbnail_selection.json（run / attempt 別の履歴と採用の確定）。
- 全不合格は品質待機（status=quality_wait、採用なし）。人は adopt() で直近 run の候補から採用できる。
- 予測 CTR は記録するだけで実測として扱わない（キー名 predicted_ctr）。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

SELECTION_FILE = ".thumbnail_selection.json"
SELECTED_FILE = "thumbnail_selected.jpg"
RUN_RECORD_FILE = "selection_record.json"
CANDIDATE_ROOT = "thumbnail_candidates"
SCHEMA_VERSION = 1
_RUN_ID_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")  # 先頭は英数字（"." / ".." を通さない）
DEFAULT_IMAGE_MODEL = "gpt-image-2"


@dataclass
class GenerateResult:
    """generate の戻り: 背景候補のパス、使った model / backend、モデルを強制できたか。infra_error は生成基盤の失敗。"""
    paths: list = field(default_factory=list)
    model: str = DEFAULT_IMAGE_MODEL
    backend: str = "unknown"
    model_enforced: bool = False
    infra_error: str = ""


@dataclass
class ScoreOutput:
    evaluations: list = field(default_factory=list)
    backend: str = "unknown"
    error: str = ""


@dataclass
class SelectionResult:
    status: str  # selected / quality_wait / generation_failure / score_failure
    run_id: str
    selected_id: Optional[str] = None
    selected_hash: Optional[str] = None
    attempts: int = 0
    reason: str = ""
    record: dict = field(default_factory=dict)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _atomic_write_json(path: Path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_copy(src: Path, dst: Path) -> None:
    fd, tmp = tempfile.mkstemp(prefix=dst.name + ".", suffix=".tmp", dir=str(dst.parent))
    os.close(fd)
    try:
        shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(folder: Path) -> dict:
    p = Path(folder) / SELECTION_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save(folder: Path, data: dict) -> None:
    data["schema_version"] = SCHEMA_VERSION
    _atomic_write_json(Path(folder) / SELECTION_FILE, data)
    run_id = str(data.get("run_id") or "")
    if run_id and _RUN_ID_RE.match(run_id) and run_id not in (".", ".."):
        # run ごとの完全な記録（次の run で現在の記録が置き換わっても失われない）
        run_dir = Path(folder) / CANDIDATE_ROOT / run_id
        if run_dir.is_dir():
            _atomic_write_json(run_dir / RUN_RECORD_FILE, {k: v for k, v in data.items() if k != "history"})


def verify(folder: Path) -> tuple[bool, str]:
    """採用が確定していて、thumbnail_selected.jpg が採用時の画像と同一（sha256 一致）で、採用 id がその run の候補集合にあるか。"""
    folder = Path(folder)
    data = load(folder)
    if data.get("status") != "selected":
        return False, f"not_selected ({data.get('status') or 'none'})"
    sel_id = data.get("selected_id")
    if not sel_id or sel_id not in (data.get("candidate_ids") or []):
        return False, "selected_id_not_in_candidates"
    if not str(sel_id).startswith(f"{data.get('run_id')}/"):
        return False, "selected_id_not_in_run"
    selected = folder / SELECTED_FILE
    if not selected.is_file():
        return False, "selected_file_missing"
    actual = sha256_file(selected)
    if actual != data.get("selected_hash"):
        return False, "selected_hash_mismatch"
    expected = (data.get("candidate_hashes") or {}).get(sel_id)
    if not expected or expected != actual:
        return False, "candidate_hash_mismatch"
    return True, "ok"


def _thresholds(score_threshold: int, ctr_threshold: float, similarity_max: int) -> dict:
    return {"score": score_threshold, "predicted_ctr": ctr_threshold, "similarity_max": similarity_max}


# 注: 計画は app_image_eval_loop.run_eval_loop の再利用だが、attempt ごとの候補集合・ハッシュ・候補外評価の排除・
# 採点失敗の区別を扱えないため独自ループにした（build_feedback / judge_auto_approval / best_of は再利用）。
def run_selection(folder: Path, vol: int, *, generate: Callable[[Path, int, int, str], GenerateResult],
                  score: Callable[[list], ScoreOutput], compose: Callable[[Path, Path], None],
                  concept_hint: str = "", thumbnail_axis: Optional[dict] = None, run_id: Optional[str] = None,
                  max_attempts: int = 3, n_per_attempt: int = 4, max_total: int = 12,
                  score_threshold: int = 80, ctr_threshold: float = 7.0, similarity_max: int = 25,
                  log: Callable[[str], None] = print) -> SelectionResult:
    """生成 → 合成 → 採点 → 判定を最大 max_attempts 回。合格の中の最高点を採用する。

    generate(attempt_dir, attempt, n, concept_hint) は背景候補を attempt_dir に作る。
    compose(background_path, out_path) はチャンネル設定でサムネを合成する。
    score(paths) は合成済みの候補を採点する（filename は basename）。
    """
    from app_image_eval_loop import build_feedback
    from app_thumbnail_scoring import STATUS_AUTO_APPROVED, best_of, judge_auto_approval

    folder = Path(folder)
    run_id = run_id or datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    if not _RUN_ID_RE.match(run_id):
        raise ValueError(f"run_id に使えない文字があります: {run_id!r}")
    run_dir = folder / CANDIDATE_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    data = load(folder)
    history = list(data.get("history") or [])
    revision = int(data.get("selection_revision") or 0)
    thresholds = _thresholds(score_threshold, ctr_threshold, similarity_max)
    candidate_ids: list[str] = []
    candidate_hashes: dict[str, str] = {}
    candidate_paths: dict[str, Path] = {}  # 候補 id → 実ファイル（id を分解し直してパスを作らない）
    score_failures = 0
    rejected: dict[str, str] = {}
    all_judged: list[dict] = []
    model = DEFAULT_IMAGE_MODEL
    backend = "unknown"
    model_enforced = False
    judge_backend = "unknown"
    total = 0
    hint = concept_hint
    axis = dict(thumbnail_axis or {})
    chosen: Optional[dict] = None
    status = "quality_wait"
    reason = ""

    for attempt in range(1, max_attempts + 1):
        if total >= max_total:
            reason = "cost_cap"
            break
        n = max(1, min(n_per_attempt, max_total - total))
        adir = run_dir / f"a{attempt}"
        bg_dir = adir / "backgrounds"
        bg_dir.mkdir(parents=True, exist_ok=True)
        gen = generate(bg_dir, attempt, n, hint)
        model, backend, model_enforced = gen.model, gen.backend, gen.model_enforced
        if gen.infra_error or not gen.paths:
            status, reason = "generation_failure", gen.infra_error or "no_images"
            history.append({"run_id": run_id, "attempt": attempt, "at": _now(), "status": status, "reason": reason})
            break
        files: dict[str, Path] = {}
        for i, bg in enumerate(gen.paths, start=1):
            out = adir / f"a{attempt}_{i}.jpg"
            try:
                compose(Path(bg), out)
            except Exception as exc:
                rejected[f"{run_id}/a{attempt}/{out.name}"] = f"compose_failed: {type(exc).__name__}"
                continue
            if out.is_file():
                files[out.name] = out
        total += len(gen.paths)
        att_ids = []
        for name, p in files.items():
            cid = f"{run_id}/a{attempt}/{name}"
            att_ids.append(cid)
            candidate_ids.append(cid)
            candidate_hashes[cid] = sha256_file(p)
            candidate_paths[cid] = p
        if not files:
            history.append({"run_id": run_id, "attempt": attempt, "at": _now(), "status": "compose_failed"})
            continue
        sc = score(list(files.values()))
        judge_backend = sc.backend or judge_backend
        if sc.error or not (sc.evaluations or []):
            # 採点基盤の失敗（LLM 障害・JSON を読めない）は品質不足ではない。再生成で費用を使わず基盤失敗として止める
            err = (sc.error or "empty_evaluations")[:300]
            for cid in att_ids:
                rejected[cid] = f"score_failed: {err}"
            history.append({"run_id": run_id, "attempt": attempt, "at": _now(), "status": "score_failed",
                            "error": err, "candidate_ids": att_ids})
            score_failures += 1
            status, reason = "score_failure", err
            break
        valid, ignored, seen_names = [], [], set()
        for e in sc.evaluations or []:
            if not isinstance(e, dict):
                ignored.append({"raw": str(e)[:80], "reason": "not_an_object"})
                continue
            raw_name = str(e.get("filename") or "")
            name = Path(raw_name).name
            if name not in files:
                ignored.append({"raw": raw_name[:200], "reason": "not_a_candidate"})  # 他 run の画像や架空の名前は採用対象にしない
                continue
            if name in seen_names:
                ignored.append({"raw": raw_name[:200], "reason": "duplicate"})  # 同じ候補の 2 件目以降は使わない
                continue
            seen_names.add(name)
            valid.append({**e, "filename": name})
        judged = judge_auto_approval(valid, mode="conditional", score_threshold=score_threshold,
                                     ctr_threshold=ctr_threshold, similarity_max=similarity_max)
        for e in judged:
            cid = f"{run_id}/a{attempt}/{e['filename']}"
            e["candidate_id"] = cid
            if e.get("status") != STATUS_AUTO_APPROVED:
                rejected[cid] = str(e.get("approval_reason") or "rejected")
        unscored = [cid for cid in att_ids if cid not in {e["candidate_id"] for e in judged}]
        for cid in unscored:
            rejected.setdefault(cid, "not_scored")
        all_judged.extend(judged)
        try:
            from app_image_compositor import contact_sheet
            contact_sheet([files[n] for n in sorted(files)], adir / "contact_sheet.jpg")
        except Exception as exc:
            log(f"  コンタクトシートを作れませんでした: {exc}")
        history.append({"run_id": run_id, "attempt": attempt, "at": _now(), "status": "scored",
                        "candidate_ids": att_ids, "ignored_evaluations": ignored,
                        "scores": {e["candidate_id"]: {"score_total": e.get("score_total"),
                                                       "predicted_ctr": e.get("ctr_predict"),
                                                       "similarity": e.get("similarity"),
                                                       "status": e.get("status")} for e in judged},
                        "judge_backend": judge_backend})
        approved = [e for e in judged if e.get("status") == STATUS_AUTO_APPROVED]
        log(f"  サムネ選定 attempt {attempt}: 候補 {len(files)} / 合格 {len(approved)}")
        if approved:
            chosen = best_of(approved)
            status = "selected"
            break
        hint, axis = build_feedback(judged, best_of(all_judged), concept_hint, axis)
    else:
        reason = reason or "all_rejected"
    if status == "quality_wait" and not reason:
        reason = "all_rejected"

    record = {
        "status": status, "run_id": run_id, "attempt": len([h for h in history if h.get("run_id") == run_id]),
        "candidate_ids": candidate_ids, "candidate_hashes": candidate_hashes, "rejected_reasons": rejected,
        "model": model, "model_enforced": model_enforced, "backend": backend, "judge_backend": judge_backend,
        "thresholds": thresholds, "decided_at": _now(), "history": history[-50:],
        "selection_revision": revision + 1,
    }
    if chosen is not None:
        cid = chosen["candidate_id"]
        src = candidate_paths[cid]
        _atomic_copy(src, folder / SELECTED_FILE)
        sel_hash = sha256_file(folder / SELECTED_FILE)
        if sel_hash != candidate_hashes.get(cid):
            raise RuntimeError("採用画像のコピーが候補と一致しません")
        record.update({"selected_id": cid, "selected_hash": sel_hash, "decided_by": "ai",
                       "predicted_ctr": chosen.get("ctr_predict"), "score_total": chosen.get("score_total")})
        _save(folder, record)
        return SelectionResult("selected", run_id, cid, sel_hash, record["attempt"], "", record)
    record.update({"selected_id": None, "selected_hash": None, "decided_by": None, "reason": reason or status})
    _save(folder, record)
    return SelectionResult(status, run_id, None, None, record["attempt"], reason or status, record)


def adopt(folder: Path, candidate_id: str, *, by: str = "human") -> SelectionResult:
    """直近 run の候補集合にある candidate_id を採用する（候補外・ハッシュ不一致は拒否）。"""
    folder = Path(folder)
    data = load(folder)
    if not data or not data.get("run_id"):
        raise ValueError("選定の記録がありません（先に thumbnail-select を実行してください）")
    if candidate_id not in (data.get("candidate_ids") or []):
        raise ValueError(f"候補外の画像は採用できません: {candidate_id}")
    src = folder / CANDIDATE_ROOT / candidate_id
    try:
        src.resolve().relative_to((folder / CANDIDATE_ROOT / data["run_id"]).resolve())
    except ValueError:
        raise ValueError("直近 run の候補ではありません")
    if not src.is_file():
        raise ValueError("候補ファイルが見つかりません")
    if sha256_file(src) != (data.get("candidate_hashes") or {}).get(candidate_id):
        raise ValueError("候補ファイルが選定時から変わっています")
    _atomic_copy(src, folder / SELECTED_FILE)
    sel_hash = sha256_file(folder / SELECTED_FILE)
    if sel_hash != (data.get("candidate_hashes") or {}).get(candidate_id):
        raise ValueError("採用画像のコピーが候補と一致しません（照合中に変わりました）")
    data.update({"status": "selected", "selected_id": candidate_id, "selected_hash": sel_hash, "decided_by": by,
                 "decided_at": _now(), "selection_revision": int(data.get("selection_revision") or 0) + 1})
    data.setdefault("history", []).append({"run_id": data["run_id"], "at": _now(), "status": "adopted", "by": by,
                                           "candidate_id": candidate_id})
    _save(folder, data)
    return SelectionResult("selected", data["run_id"], candidate_id, sel_hash, int(data.get("attempt") or 0), "", data)
