"""工程の完了判定と失効（段階 2c）。

完了は「成果物がある」ではなく「done_validator が合格 かつ 記録した input_hash が現在の入力と一致」で判定する。
- 記録: vol フォルダの `.stage_state.json`（stage → {input_hash, completed_at, workflow_version}）。
- input_hash: workflow.json の depends_on にある前段工程の出力指紋（相対パス・サイズ・mtime_ns、
  小さい JSON / TXT は内容の sha256）を連結した sha256。前段の出力が変わると後段の記録は一致しなくなり失効する。
- 記録が無い vol（本段階より前に作られたもの）は validator だけで判定する（挙動を変えない）。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import exit_codes as ec

STATE_FILE = ".stage_state.json"
SCHEMA_VERSION = 1
# 入力が変わっても完了を失効させない工程。upload は再投稿ではなく「既存 video_id の更新」で扱う（段階 3）。
NO_INVALIDATE = frozenset({"upload"})
_AUDIO = (".mp3", ".wav", ".m4a")
_CONTENT_HASH_SUFFIXES = (".json", ".txt")
_CONTENT_HASH_MAX_BYTES = 1 << 20


# ─── 出力（指紋の対象） ───

def _files(root: Path, suffixes=None) -> list[Path]:
    if not root.is_dir():
        return []
    out = [p for p in root.iterdir() if p.is_file() and not p.name.startswith(".")
           and (suffixes is None or p.suffix.lower() in suffixes)]
    return sorted(out)


def _img_candidates(folder: Path, vol: int, *names: str) -> list[Path]:
    out = []
    for n in names:
        for base in (folder, folder / "Image"):
            p = base / n
            if p.is_file():
                out.append(p)
    return out


def _mp4s(folder: Path) -> list[Path]:
    return _files(folder, (".mp4",))


def stage_outputs(stage: str, folder: Path, vol: int) -> list[Path]:
    """その工程の出力ファイル（後段の input_hash の材料）。無ければ空。

    自分の出力を自分の入力に含めない（bgimage は vol{N}.png だけ。vol{N}.jpg は composite の出力）。"""
    st = ec.canonical_stage(stage) or stage
    if st == "suno":
        return _files(folder / "original_music", _AUDIO) or _files(folder / "music", _AUDIO)
    if st == "select":
        p = folder / ".music_review.json"
        return [p] if p.is_file() else []
    if st == "rename":
        return _files(folder / "music", _AUDIO)
    if st == "bgimage":
        return _img_candidates(folder, vol, f"vol{vol}.png")
    if st in ("composite", "thumbnail"):
        return _img_candidates(folder, vol, "サムネイル.jpg", f"vol{vol}.jpg")
    if st == "render":
        pat = re.compile(rf"vol{vol}(?!\d)")
        mine = [p for p in _mp4s(folder) if pat.search(p.name)]
        # 書き出しが同時に作るタイムコード・字幕（meta の説明文の材料）も出力に含める
        extras = sorted(p for p in folder.glob("music_time_code_info_*.txt") if p.is_file()) + \
                 sorted(p for p in folder.glob("subtitles_*.srt") if p.is_file())
        return (mine or _mp4s(folder)) + extras
    if st == "qa":
        # qa の出力は検査結果 + 検査した書き出しのタイムコード（内容ハッシュ。曲順の変更だけで meta が失効する）。
        # mp4 / srt は含めない（中身の同じ再 render で meta が作り直されるのを防ぐ）。
        p = folder / "qa_report.json"
        tcs = sorted(q for q in folder.glob("music_time_code_info_*.txt") if q.is_file())
        return ([p] if p.is_file() else []) + tcs
    if st == "meta":
        return [folder / n for n in ("youtube_title.txt", "youtube_description.txt", "youtube_tags.txt") if (folder / n).is_file()]
    if st == "localization":
        p = folder / "youtube_localizations.json"
        return [p] if p.is_file() else []
    if st == "upload":
        p = folder / "youtube_upload.json"
        return [p] if p.is_file() else []
    return []


def _selection_essence(path: Path) -> str:
    """.music_review.json のうち選定の意味内容だけ（PID・採点・試行回数などの揮発する値は除く）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    takes = data.get("takes") if isinstance(data, dict) else None
    chosen = []
    if isinstance(takes, list):
        for t in takes:
            if isinstance(t, dict) and t.get("selected"):
                chosen.append(str(t.get("clip_id") or t.get("file") or ""))
    essence = {"selected": sorted(chosen),
               "revision": data.get("revision") if isinstance(data, dict) else None,
               "processed_revision": data.get("processed_revision") if isinstance(data, dict) else None}
    return hashlib.sha256(json.dumps(essence, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _qa_essence(path: Path) -> str:
    """qa_report.json のうち結果だけ（checked_at などの実行時刻を除く。再検査のたびに meta が失効しないように）。"""
    data = json.loads(path.read_text(encoding="utf-8"))
    essence = {"passed": bool(data.get("passed")), "issues": data.get("issues")} if isinstance(data, dict) else data
    return hashlib.sha256(json.dumps(essence, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def _fingerprint(path: Path, folder: Path) -> str:
    """ファイルの指紋。小さな JSON / TXT は内容、それ以外はサイズと mtime（秒。Drive 同期で
    mtime の精度が落ちても失効しないが、同サイズの差し替えは mtime で検出する）。"""
    try:
        rel = str(path.resolve().relative_to(folder.resolve()))
    except (ValueError, OSError):
        rel = path.name
    try:
        st = path.stat()
    except OSError:
        return f"{rel}|missing"
    suffix = path.suffix.lower()
    if path.name == ".music_review.json":
        try:
            return f"{rel}|selection:{_selection_essence(path)}"
        except (OSError, ValueError, TypeError):
            pass
    if path.name == "qa_report.json":
        try:
            return f"{rel}|qa:{_qa_essence(path)}"
        except (OSError, ValueError, TypeError):
            pass
    if suffix in _CONTENT_HASH_SUFFIXES and st.st_size <= _CONTENT_HASH_MAX_BYTES:
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            return f"{rel}|sha256:{digest}"
        except OSError:
            pass
    return f"{rel}|{st.st_size}|{int(st.st_mtime)}"


def _stage_def(stage: str) -> dict:
    st = ec.canonical_stage(stage) or stage
    for row in ec.stages():
        if row.get("id") == st:
            return row
    return {}


def compute_input_hash(stage: str, folder: Path, vol: int) -> str:
    """depends_on の出力指紋から入力ハッシュを作る。前段が無い工程は定数。"""
    deps = list(_stage_def(stage).get("depends_on") or [])
    lines = []
    for dep in deps:
        for p in stage_outputs(dep, folder, vol):
            lines.append(f"{dep}:{_fingerprint(p, folder)}")
    payload = "\n".join(sorted(lines))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ─── done_validator（workflow.json の名前と一致させる） ───

def _has_audio(root: Path) -> bool:
    return bool(_files(root, _AUDIO))


def suno_done(folder: Path, vol: int) -> bool:
    return _has_audio(folder / "original_music") or _has_audio(folder / "music")


def selection_processed(folder: Path, vol: int) -> bool:
    """選定済みかつ後処理済み（export / upload のゲートと同じ条件）。判定できなければ False。"""
    try:
        from app_music_catalog import SelectionPending, require_selection
        require_selection(folder, processed=True)
        return True
    except SelectionPending:
        return False
    except Exception:
        return False


def rename_done(folder: Path, vol: int) -> bool:
    return _has_audio(folder / "music")


def bgimage_done(folder: Path, vol: int) -> bool:
    return bool(_img_candidates(folder, vol, f"vol{vol}.png", f"vol{vol}.jpg"))


def composite_done(folder: Path, vol: int) -> bool:
    return bool(_img_candidates(folder, vol, "サムネイル.jpg", f"vol{vol}.jpg"))


def export_done(folder: Path, vol: int) -> bool:
    return bool(_mp4s(folder))


def qa_done(folder: Path, vol: int) -> bool:
    # passed:true のみ done。passed:false は保留（人が直して qa_report.json を消すまで後段へ進めない）。
    p = folder / "qa_report.json"
    if not p.is_file():
        return False
    try:
        return bool(json.loads(p.read_text(encoding="utf-8")).get("passed", False))
    except Exception:
        return False


def meta_done(folder: Path, vol: int) -> bool:
    return (folder / "youtube_title.txt").is_file()


def localization_done(folder: Path, vol: int) -> bool:
    return (folder / "youtube_localizations.json").is_file()


def thumbnail_done(folder: Path, vol: int) -> bool:
    return composite_done(folder, vol)


def upload_done(folder: Path, vol: int) -> bool:
    return (folder / "youtube_upload.json").is_file()


def publish_verified(folder: Path, vol: int) -> bool:
    return False  # VPS 側の工程（段階 3）。Mac では判定しない。


VALIDATORS: dict[str, Callable[[Path, int], bool]] = {
    "suno_done": suno_done, "selection_processed": selection_processed, "rename_done": rename_done,
    "bgimage_done": bgimage_done, "composite_done": composite_done, "export_done": export_done,
    "qa_done": qa_done, "meta_done": meta_done, "localization_done": localization_done,
    "thumbnail_done": thumbnail_done, "upload_done": upload_done, "publish_verified": publish_verified,
}


def validator_for(stage: str) -> Optional[Callable[[Path, int], bool]]:
    name = _stage_def(stage).get("done_validator")
    return VALIDATORS.get(str(name)) if name else None


# ─── 記録 ───

def load(folder: Path) -> dict:
    p = folder / STATE_FILE
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        stages = data.get("stages") if isinstance(data, dict) else None
        return dict(stages) if isinstance(stages, dict) else {}
    except Exception as exc:
        print(f"  警告: {p} を読めません（空として扱います）: {exc}")
        return {}


def save(folder: Path, stages: dict) -> None:
    p = folder / STATE_FILE
    body = json.dumps({"schema_version": SCHEMA_VERSION, "stages": stages}, ensure_ascii=False, indent=2)
    fd, tmp = tempfile.mkstemp(prefix=".stage_state.", suffix=".tmp", dir=str(folder))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextlib.contextmanager
def _locked(folder: Path):
    """load → 更新 → save をプロセス間で排他する（batch の SUNO と画像先行が同じ vol に並行して記録しても後勝ちで消えない）。"""
    lock = folder / ".stage_state.lock"
    with open(lock, "a+") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def mark_done(stage: str, folder: Path, vol: int) -> Optional[dict]:
    """工程成功時に呼ぶ。現在の入力ハッシュを記録する。廃止工程（premiere）は記録しない。"""
    st = ec.canonical_stage(stage)
    if st is None:
        return None
    fn = validator_for(st)
    if fn is not None and not fn(folder, vol):
        # 成果物を作らずに成功した工程（例: 多言語設定の無いチャンネルの localization）は記録しない。
        # 記録すると "missing" になって毎回再投入される。古い記録も消し、従来どおり成果物と台帳で判定する。
        invalidate(st, folder, vol)
        return None
    with _locked(folder):
        stages = load(folder)
        stages[st] = {"input_hash": compute_input_hash(st, folder, vol),
                      "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                      "workflow_version": ec.WORKFLOW_VERSION}
        save(folder, stages)
        return stages[st]


def mark_attempted(stage: str, folder: Path, vol: int) -> Optional[dict]:
    """工程が失敗したとき、どの入力に対して試したかを記録する（同じ入力で毎 tick 再投入しないため）。
    完了記録（input_hash / completed_at）は変えない。"""
    st = ec.canonical_stage(stage)
    if st is None:
        return None
    with _locked(folder):
        stages = load(folder)
        rec = dict(stages.get(st) or {})
        rec["attempted_input_hash"] = compute_input_hash(st, folder, vol)
        rec["attempted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        stages[st] = rec
        save(folder, stages)
        return rec


def attempted_for_current_input(stage: str, folder: Path, vol: int) -> bool:
    """現在の入力に対して既に試行（失敗）済みか。"""
    st = ec.canonical_stage(stage) or stage
    rec = load(folder).get(st)
    return isinstance(rec, dict) and rec.get("attempted_input_hash") == compute_input_hash(st, folder, vol)


def invalidate(stage: str, folder: Path, vol: int) -> None:
    st = ec.canonical_stage(stage) or stage
    with _locked(folder):
        stages = load(folder)
        if st in stages:
            del stages[st]
            save(folder, stages)


def record(stage: str, folder: Path) -> Optional[dict]:
    st = ec.canonical_stage(stage) or stage
    return load(folder).get(st)


def evaluate(stage: str, folder: Path, vol: int) -> str:
    """1 回の読込と計算で判定する。
    - "unknown": 廃止工程（premiere）など、ここでは判定しない（呼び出し側が従来の判定へ）
    - "stale":   記録があり入力が変わっている（成果物があっても失効）
    - "done":    validator 合格 かつ（記録なし＝旧 vol、または input_hash 一致）
    - "missing": 記録はあるが validator 不合格（成果物が消えた等。台帳の done で代用しない）
    - "pending": 記録なしで validator 不合格
    """
    st = ec.canonical_stage(stage)
    if st is None:
        return "unknown"
    rec = load(folder).get(st)
    recorded = isinstance(rec, dict) and "input_hash" in rec
    # 投稿済み vol（youtube_upload.json あり）は失効させない。手直しで再書き出し・メタ再生成が連鎖し、
    # YouTube 側とローカルがずれるのを防ぐ（投稿後の更新は段階 3 の update 経路で扱う）。
    if st not in NO_INVALIDATE and not upload_done(folder, vol) and recorded \
            and rec.get("input_hash") != compute_input_hash(st, folder, vol):
        return "stale"
    fn = validator_for(st)
    if fn is None or not fn(folder, vol):
        return "missing" if recorded else "pending"
    return "done"


def stage_done(stage: str, folder: Path, vol: int) -> bool:
    """validator 合格 かつ（記録が無い＝旧 vol、または input_hash 一致）。記録があって不一致なら失効。"""
    return evaluate(stage, folder, vol) == "done"


def is_stale(stage: str, folder: Path, vol: int) -> bool:
    """記録があり、入力が変わっている（成果物の有無は見ない）。"""
    return evaluate(stage, folder, vol) == "stale"
