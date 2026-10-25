"""楽曲タイトルの永久台帳と、SUNOの2テイクをハートで選ぶ採用台帳。

DBはチャンネル切替から独立した1台のMacの正本。JSONも各素材フォルダへ
書き出し、Fable/Claude等から同じ選択を読める。原本は変更・削除しない。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

EXIT_SELECTION_PENDING = 79
_lock = threading.RLock()
_seeded = False


class SelectionPending(RuntimeError):
    pass


def db_path():
    return Path(os.environ.get("APP_MUSIC_CATALOG_DB") or
                Path.home() / ".config/automation-studio/music_catalog.sqlite3")


def title_key(title):
    # 大文字・全角・空白・句読点の違いだけのタイトルも重複扱い。
    return "".join(c for c in unicodedata.normalize("NFKC", str(title)).casefold()
                   if c.isalnum())


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def transaction():
    with _lock:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS titles (
              key TEXT PRIMARY KEY, title TEXT NOT NULL, request_id TEXT UNIQUE,
              state TEXT NOT NULL, created_at TEXT NOT NULL, content_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS batches (
              id TEXT PRIMARY KEY, folder TEXT UNIQUE NOT NULL, workspace TEXT NOT NULL,
              expected INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
              processed_revision INTEGER NOT NULL DEFAULT -1, processing INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS takes (
              batch_id TEXT NOT NULL, clip_id TEXT NOT NULL, title TEXT NOT NULL,
              title_key TEXT NOT NULL, file TEXT NOT NULL, selected INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY(batch_id,clip_id));
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY, at TEXT NOT NULL, kind TEXT NOT NULL, detail_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS take_scores (
              batch_id TEXT NOT NULL, clip_id TEXT NOT NULL, duration_sec REAL, silence_ratio REAL,
              lufs REAL, true_peak REAL, prompt_fit REAL, score REAL NOT NULL, passed INTEGER NOT NULL,
              reason TEXT NOT NULL, scored_at TEXT NOT NULL, PRIMARY KEY(batch_id,clip_id));
            """)
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()


def _event(conn, kind, detail):
    conn.execute("INSERT INTO events(at,kind,detail_json) VALUES(?,?,?)",
                 (now(), kind, json.dumps(detail, ensure_ascii=False)))


def import_titles(titles, source="legacy"):
    with transaction() as conn:
        before = conn.total_changes
        for title in titles:
            title = str(title).strip()
            if title_key(title):
                conn.execute("INSERT OR IGNORE INTO titles VALUES(?,?,NULL,'existing',?,?)",
                             (title_key(title), title, now(), json.dumps({"source": source})))
        count = conn.total_changes - before
        _event(conn, "import_titles", {"source": source, "added": count})
    return count


def seed_existing():
    """既存チャンネル・生成ログを最初に取り込み。失敗は黙って無視しない。"""
    from _app_config import resolve_shared_config_dir, resolve_shared_base
    base, config = resolve_shared_base(), resolve_shared_config_dir()
    titles = []
    history = config / ".suno_history.json"
    if history.is_file():
        def collect(value):
            if isinstance(value, dict):
                if value.get("title"): titles.append(value["title"])
                for item in value.values(): collect(item)
            elif isinstance(value, list):
                for item in value: collect(item)
        collect(json.loads(history.read_text(encoding="utf-8")))
    roots = [base / "output"]
    channels_path = config / "channels.json"
    if channels_path.is_file():
        data = json.loads(channels_path.read_text(encoding="utf-8"))
        channels = data.get("channels", []) if isinstance(data, dict) else data
        if isinstance(channels, dict): channels = list(channels.values())
        for channel in channels:
            raw = channel.get("folder") or channel.get("channel_folder")
            if raw: roots.append(Path(raw).expanduser())
    for root in roots:
        if not root.is_dir(): continue
        # 一段のvol/生成バッチのみ。動画や画像の深いフォルダを走査しない。
        for folder in [root, *[p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]]:
            for audio_dir in [folder, folder / "music", folder / "original_music"]:
                if not audio_dir.is_dir(): continue
                for path in audio_dir.glob("*.mp3"):
                    title = re.sub(r"^(?:x_)?z+_", "", path.stem)
                    title = re.sub(r"_\d+$", "", title)
                    if not re.fullmatch(r"[0-9a-f-]{36}", title, re.I): titles.append(title)
            manifest = folder / ".suno_fast_download.json"
            if manifest.is_file():
                titles.extend(item["title"] for item in
                              json.loads(manifest.read_text(encoding="utf-8")).get("saved", [])
                              if item.get("title"))
    return import_titles(titles, "existing channels and generation logs")


def prepare_submission(content):
    """Create前に全生成経路で予約。2テイクはこの1予約を共有する。"""
    global _seeded
    if not _seeded:
        seed_existing()
        _seeded = True
    prepared = dict(content)
    requested = str(prepared.get("title") or "Untitled Groove").strip()
    if not title_key(requested):
        requested = "Untitled Groove"
    with transaction() as conn:
        title = requested
        suffixes = ["Afterglow", "Daybreak", "Nightfall", "Drift", "Horizon", "Motion"]
        attempt = 0
        while conn.execute("SELECT 1 FROM titles WHERE key=?", (title_key(title),)).fetchone():
            title = f"{requested} {suffixes[attempt]}" if attempt < len(suffixes) else f"{requested} {attempt + 1}"
            attempt += 1
        request_id = secrets.token_hex(16)
        prepared["title"] = title
        prepared["_music_request_id"] = request_id
        conn.execute("INSERT INTO titles VALUES(?,?,?,'reserved',?,?)",
                     (title_key(title), title, request_id, now(), json.dumps(prepared, ensure_ascii=False)))
        _event(conn, "title_reserved", {"request_id": request_id, "requested": requested, "title": title})
    if title != requested:
        print(f"タイトル重複を回避: {requested} → {title}", flush=True)
    return prepared


def mark_submitted(content):
    with transaction() as conn:
        conn.execute("UPDATE titles SET state='submitted',content_json=? WHERE request_id=?",
                     (json.dumps(content, ensure_ascii=False), content["_music_request_id"]))
        _event(conn, "submitted", {"request_id": content["_music_request_id"], "title": content["title"]})


def safe_file(folder, rel):
    folder = Path(folder).resolve()
    path = (folder / rel).resolve()
    path.relative_to(folder)
    if not path.is_file() or path.suffix.lower() != ".mp3":
        raise ValueError(f"MP3が見つかりません: {rel}")
    return path


def register_download(folder, saved, workspace, expected):
    folder = Path(folder).resolve()
    batch_id = hashlib.sha256(str(folder).encode()).hexdigest()[:20]
    manifest = folder / ".music_review.json"
    portable = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    portable_selected = {t["clip_id"] for t in portable.get("takes", []) if t.get("selected")}
    with transaction() as conn:
        existed = conn.execute("SELECT 1 FROM batches WHERE id=?", (batch_id,)).fetchone()
        conn.execute("INSERT INTO batches(id,folder,workspace,expected) VALUES(?,?,?,?) "
                     "ON CONFLICT(id) DO UPDATE SET expected=excluded.expected,workspace=excluded.workspace",
                     (batch_id, str(folder), workspace, int(expected)))
        for item in saved:
            safe_file(folder, item["file"])
            title, clip = str(item["title"]), str(item["song_id"])
            old = conn.execute("SELECT file FROM takes WHERE batch_id=? AND clip_id=?", (batch_id, clip)).fetchone()
            if not old or old["file"] != item["file"]:
                conn.execute("UPDATE batches SET revision=revision+1 WHERE id=?", (batch_id,))
            conn.execute("INSERT INTO takes VALUES(?,?,?,?,?,?) ON CONFLICT(batch_id,clip_id) "
                         "DO UPDATE SET file=excluded.file",
                         (batch_id, clip, title, title_key(title), item["file"],
                          int(not existed and clip in portable_selected)))
            conn.execute("INSERT OR IGNORE INTO titles VALUES(?,?,NULL,'downloaded',?,?)",
                         (title_key(title), title, now(), json.dumps({"batch_id": batch_id})))
        _event(conn, "download_registered", {"batch_id": batch_id, "downloaded": len(saved), "expected": expected})
        _export(conn, batch_id)
    return batch_id


def _legacy_music_name(title):
    """段階 2a より前の採用ファイル名（テイク番号を残す旧規則）。既存 vol の処理済み判定にだけ使う。"""
    return re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", str(title)).strip() + ".mp3"


def selected_music_names(takes):
    """採用テイク → music/ のファイル名（naming.music_filename。テイク番号を除き、同名は ` -2` で採番）。

    takes の順（title_key, clip_id）で決定的に採番する。戻り値は {clip_id: filename}。"""
    from naming import music_filename
    names = {}
    for take in takes:
        if not take.get("selected"):
            continue
        names[take["clip_id"]] = music_filename(take["title"], taken=names.values())
    return names


def _state(conn, batch_id):
    row = conn.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
    if not row: raise ValueError("楽曲バッチが見つかりません")
    state = dict(row)
    # 子プロセスが落ちた後の選択ロックを回収する。
    if state["processing"] > 1:
        try:
            os.kill(state["processing"], 0)
        except ProcessLookupError:
            conn.execute("UPDATE batches SET processing=0 WHERE id=?", (batch_id,))
            state["processing"] = 0
    takes = [dict(r) for r in conn.execute("SELECT * FROM takes WHERE batch_id=? ORDER BY title_key,clip_id", (batch_id,))]
    groups = {}
    for take in takes:
        try: safe_file(state["folder"], take["file"]); take["available"] = True
        except (ValueError, OSError): take["available"] = False
        groups.setdefault(take["title_key"], []).append(take)
    state["takes"] = takes
    state["group_count"] = len(groups)
    state["selected_count"] = sum(sum(t["selected"] for t in group) == 1 for group in groups.values())
    state["download_complete"] = bool(groups) and len(takes) == state["expected"] and all(
        len(group) == 2 and all(t["available"] for t in group) for group in groups.values())
    state["ready"] = state["download_complete"] and all(sum(t["selected"] for t in group) == 1 for group in groups.values())
    state["processed"] = state["ready"] and state["processed_revision"] == state["revision"]
    from naming import _name_key
    music = Path(state["folder"]) / "music"
    actual = {p.name for p in music.glob("*.mp3")} if music.is_dir() else set()
    actual_keys = {_name_key(n) for n in actual}
    # music/ がどの規則で作られているか（"new" / "legacy" / None）。選び直しで revision が進んでも、
    # 一度旧規則で処理した vol は旧名を保つ必要があるため、全テイクの旧名と照合して判定する。
    state["processed_naming"] = None
    if state["processed_revision"] >= 0 and actual_keys:
        legacy_all = {_name_key(_legacy_music_name(t["title"])) for t in takes}
        try:
            new_all = {_name_key(n) for n in selected_music_names(takes).values()}
        except ValueError:
            new_all = set()
        legacy_only = legacy_all - new_all
        if actual_keys & legacy_only:
            state["processed_naming"] = "legacy"
        elif actual_keys & new_all:
            state["processed_naming"] = "new"
    if state["processed"]:
        try:
            names = set(selected_music_names(takes).values())
        except ValueError:
            names = set()
        legacy = {_legacy_music_name(t["title"]) for t in takes if t["selected"]}  # 旧規則で処理済みの既存 vol も認める
        if names and actual_keys == {_name_key(n) for n in names}:
            matched = actual
        elif actual_keys == {_name_key(n) for n in legacy}:
            matched = actual
        else:
            matched = None
        state["processed"] = matched is not None and all((music / name).stat().st_size > 0 for name in matched)
    return state


def _export(conn, batch_id):
    state = _state(conn, batch_id)
    path = Path(state["folder"]) / ".music_review.json"
    part = path.with_suffix(".json.part")
    part.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    part.replace(path)


def review_state(folder):
    folder = Path(folder).resolve()
    with transaction() as conn:
        row = conn.execute("SELECT id FROM batches WHERE folder=?", (str(folder),)).fetchone()
        if row: return _state(conn, row["id"])
    manifest = folder / ".suno_fast_download.json"
    if not manifest.is_file(): return None
    data = json.loads(manifest.read_text(encoding="utf-8"))
    # 旧版で既に後処理して原本を移した動画は、勝手に新方式へ移行しない。
    if not (folder / ".music_review.json").exists() and any(
            not (folder / t["file"]).is_file() for t in data.get("saved", [])):
        if (folder / "music").is_dir() and any((folder / "music").glob("*.mp3")):
            return None
    batch_id = register_download(folder, data.get("saved", []), data.get("workspace", folder.name), data.get("expected", 0))
    return get_review(batch_id)


def get_review(batch_id):
    with transaction() as conn: return _state(conn, batch_id)


def list_reviews():
    with transaction() as conn:
        return [_state(conn, r["id"]) for r in conn.execute("SELECT id FROM batches ORDER BY rowid DESC")]


def choose_take(batch_id, clip_id, selected):
    with transaction() as conn:
        batch = _state(conn, batch_id)
        if batch["processing"]: raise SelectionPending("後処理中です。完了後に選び直してください")
        take = next((t for t in batch["takes"] if t["clip_id"] == clip_id), None)
        if not take or not take["available"]: raise ValueError("選択対象のMP3がありません")
        if selected:
            conn.execute("UPDATE takes SET selected=0 WHERE batch_id=? AND title_key=?", (batch_id, take["title_key"]))
        conn.execute("UPDATE takes SET selected=? WHERE batch_id=? AND clip_id=?", (int(bool(selected)), batch_id, clip_id))
        conn.execute("UPDATE batches SET revision=revision+1 WHERE id=?", (batch_id,))
        _event(conn, "heart_changed", {"batch_id": batch_id, "clip_id": clip_id, "selected": bool(selected)})
        _export(conn, batch_id)
        return _state(conn, batch_id)


def require_selection(folder, processed=False):
    state = review_state(folder)
    if state and (not state["ready"] or (processed and not state["processed"])):
        raise SelectionPending(f"楽曲の選択待ち: {state['selected_count']}/{state['group_count']}タイトル。"
                               f"/static/music-review.html?batch={state['id']} でハートを選び、後処理してください")
    return state


# ─── AI 選曲（select 工程） ───
MUSIC_SELECTION_MODES = ("ai", "human")


def load_music_selection_config(folder):
    """チャンネル設定 `.app_channel_config.json` の選曲設定。既定は mode=ai。

    受け付ける形（ブリーフの形を優先）:
      - `music_selection_mode: "ai" | "human"`（トップレベル）
      - `music_selection: {mode, prompt, policy: {...}}`。`music_selection` 直下の min_sec / max_sec /
        max_silence_ratio / lufs_range / lufs_min / lufs_max / weights も policy として読む。
    APP_CHANNEL_FOLDER があればそれを、無ければ動画フォルダの親をチャンネルフォルダとして読む。"""
    folder = Path(folder)
    env_folder = (os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    candidates = []
    if env_folder:
        candidates.append(Path(env_folder).expanduser() / ".app_channel_config.json")
    candidates.append(folder.parent / ".app_channel_config.json")
    data = {}
    for path in candidates:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8")) or {}
                break
        except Exception:
            continue
    block = data.get("music_selection") if isinstance(data.get("music_selection"), dict) else {}
    mode = str(data.get("music_selection_mode") or block.get("mode") or "ai").strip().lower()
    if mode not in MUSIC_SELECTION_MODES:
        mode = "ai"
    policy_keys = ("min_sec", "max_sec", "max_silence_ratio", "lufs_range", "lufs_min", "lufs_max", "weights")
    policy = {k: v for k, v in block.items() if k in policy_keys}
    nested = block.get("policy") if isinstance(block.get("policy"), dict) else {}
    policy.update(nested)
    prompt = str(block.get("prompt") or "")
    return {"mode": mode, "policy": policy, "prompt": prompt}


def choose_take_ai(batch_id, clip_id):
    """AI の採用。1 つのトランザクション内で「後処理中でない」「その群にまだ採用が無い」を確認してから採用する。

    戻り値: "selected"（採用した）/ "human"（測定中に人が採用していた）/ "processing"（後処理中で見送り）/
    "unavailable"（対象テイクが無い）。人の採用は上書きしない。"""
    with transaction() as conn:
        batch = _state(conn, batch_id)
        if batch["processing"]:
            return "processing"
        take = next((t for t in batch["takes"] if t["clip_id"] == clip_id), None)
        if not take or not take["available"]:
            return "unavailable"
        if any(t["selected"] for t in batch["takes"] if t["title_key"] == take["title_key"]):
            return "human"
        conn.execute("UPDATE takes SET selected=1 WHERE batch_id=? AND clip_id=?", (batch_id, clip_id))
        conn.execute("UPDATE batches SET revision=revision+1 WHERE id=?", (batch_id,))
        _event(conn, "ai_selected", {"batch_id": batch_id, "clip_id": clip_id})
        _export(conn, batch_id)
        return "selected"


def ai_select_attempts(batch_id):
    """AI 選曲の試行回数（品質待機・測定不能で止まった回数。段階 3 の再生成判断に使う）。"""
    with transaction() as conn:
        rows = conn.execute("SELECT detail_json FROM events WHERE kind='ai_select_failed'").fetchall()
    n = 0
    for r in rows:
        try:
            if json.loads(r["detail_json"]).get("batch_id") == batch_id:
                n += 1
        except (TypeError, ValueError):
            continue
    return n


def _upsert_score(conn, batch_id, clip_id, m, fit, result):
    conn.execute("INSERT INTO take_scores VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(batch_id,clip_id) DO UPDATE SET "
                 "duration_sec=excluded.duration_sec,silence_ratio=excluded.silence_ratio,lufs=excluded.lufs,"
                 "true_peak=excluded.true_peak,prompt_fit=excluded.prompt_fit,score=excluded.score,passed=excluded.passed,"
                 "reason=excluded.reason,scored_at=excluded.scored_at",
                 (batch_id, clip_id, m.get("duration_sec"), m.get("silence_ratio"), m.get("lufs"), m.get("true_peak"),
                  fit, float(result["score"]), int(bool(result["passed"])), "; ".join(result["reasons"]), now()))


def list_scores(batch_id):
    with transaction() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM take_scores WHERE batch_id=? ORDER BY clip_id", (batch_id,))]


def select_takes_ai(folder, *, policy=None, measurer=None, llm=None, prompt="", dry_run=False):
    """ハートの無いタイトル群だけ AI が採点して 1 テイクを選ぶ。

    - 既にハートが付いた群は人の採用として触らない（採用は choose_take_ai で再確認してから行う）。
    - 群内の合格テイクのうち最高点を採用。合格が無い群は未選定のまま残す。測定不能のテイクは採用しない。
    - 全ての対象群が不合格なら、試行回数を台帳（events: ai_select_failed）に記録し、
      SelectionPending（品質待機、または全てが測定不能なら測定不能）を上げる。dry_run では例外にしない。
    戻り値: {batch_id, groups_total, groups_selected_by_ai, groups_human, groups_failed, groups_unmeasured,
             ready, blocked_reason, attempt, scores{clip_id: {...}}}"""
    import music_scoring
    state = review_state(folder)
    if state is None:
        raise ValueError("楽曲バッチが登録されていません（SUNO のダウンロードが先です）")
    if not state["download_complete"]:
        raise SelectionPending(f"ダウンロードが未完了のため選曲できません: {len(state['takes'])}/{state['expected']}")
    measurer = measurer or music_scoring.measure
    pol = music_scoring.policy_from(policy)
    folder = Path(state["folder"])
    groups = {}
    for take in state["takes"]:
        groups.setdefault(take["title_key"], []).append(take)
    result = {"batch_id": state["id"], "groups_total": len(groups), "groups_selected_by_ai": 0,
              "groups_human": 0, "groups_failed": 0, "groups_unmeasured": 0, "groups_skipped": 0, "scores": {},
              "dry_run": bool(dry_run), "blocked_reason": None, "attempt": ai_select_attempts(state["id"])}
    failed_titles = []
    for key, takes in groups.items():
        if any(t["selected"] for t in takes):
            result["groups_human"] += 1
            continue
        best = None
        all_unmeasured = True
        for t in takes:
            m = measurer(folder / t["file"])
            fit = music_scoring.prompt_fit(t["title"], prompt, llm) if llm else None
            r = music_scoring.score(m, fit, pol)
            result["scores"][t["clip_id"]] = {**m, "prompt_fit": fit, **r, "title": t["title"]}
            if not dry_run:
                with transaction() as conn:
                    _upsert_score(conn, state["id"], t["clip_id"], m, fit, r)
            if not r["unmeasured"]:
                all_unmeasured = False
            if r["passed"] and (best is None or r["score"] > best[1]):
                best = (t, r["score"])  # 同点は takes の順（title_key, clip_id）で先のものを保つ
        if best is None:
            result["groups_failed"] += 1
            if all_unmeasured:
                result["groups_unmeasured"] += 1
            failed_titles.append(takes[0]["title"])
            continue
        outcome = "selected" if dry_run else choose_take_ai(state["id"], best[0]["clip_id"])
        if outcome == "selected":
            result["groups_selected_by_ai"] += 1
        elif outcome == "human":
            result["groups_human"] += 1  # 測定中に人が採用した
        else:
            result["groups_skipped"] += 1  # 後処理中、または対象テイクが消えた
    ai_targets = result["groups_selected_by_ai"] + result["groups_failed"]
    if not dry_run:
        final = get_review(state["id"])
        result["ready"] = bool(final["ready"])
    else:
        result["ready"] = False
    if ai_targets and result["groups_failed"] == ai_targets:
        if result["groups_unmeasured"] == result["groups_failed"]:
            result["blocked_reason"] = "unmeasured"
            message = ("測定不能: 楽曲を測れませんでした（ffmpeg / ffprobe を確認してください）。"
                       "ハートで手動採用するか、環境を直して再実行してください")
        else:
            result["blocked_reason"] = "quality"
            message = ("品質待機: 全ての候補が不合格です（" + ", ".join(failed_titles[:5]) +
                       "）。再生成するか、ハートで手動採用してください")
        if not dry_run:
            with transaction() as conn:
                _event(conn, "ai_select_failed", {"batch_id": state["id"], "attempt": result["attempt"] + 1,
                                                  "reason": result["blocked_reason"], "failed_titles": failed_titles})
            result["attempt"] += 1
            raise SelectionPending(f"{message}（試行 {result['attempt']} 回目）")
        result["message"] = message
    return result


def process_selected(folder, processor, dry_run=False, force=False):
    if folder is None or not str(folder).strip():
        raise ValueError("vol フォルダが指定されていません（暗黙の既定フォルダは使いません）")
    state = require_selection(folder)
    if state is None:
        raise ValueError(f"楽曲バッチが登録されていません: {folder}")
    if state["processed"] and not force: return 0
    if dry_run: return 0
    folder = Path(folder).resolve()
    with transaction() as conn:
        state = _state(conn, state["id"])
        if not state["ready"] or state["processing"]: raise SelectionPending("選択待ち、または後処理中です")
        conn.execute("UPDATE batches SET processing=? WHERE id=?", (os.getpid(), state["id"]))
    music = folder / "music"
    stage = folder / (".music_selected_" + secrets.token_hex(6))
    try:
        stage.mkdir()
        # 既存の未管理音声は上書きしない。
        if music.exists() and state["processed_revision"] < 0 and any(music.iterdir()):
            raise ValueError("music/に既存の音声があります。既存動画を保護するため後処理を停止しました")
        if state.get("processed_naming") == "legacy":
            # 旧規則で処理済みの既存 vol を force で作り直すときは、ffrender の manifest 等が参照する旧名を保つ
            names = {t["clip_id"]: _legacy_music_name(t["title"]) for t in state["takes"] if t["selected"]}
        else:
            names = selected_music_names(state["takes"])  # テイク番号 (1) 等を除いた公開用の曲名で保存する
        for take in state["takes"]:
            if not take["selected"]: continue
            name = names[take["clip_id"]]
            if (stage / name).exists(): raise ValueError("採用タイトルのファイル名が重複しています")
            processor(safe_file(folder, take["file"]), stage / name)
            if not (stage / name).is_file() or not (stage / name).stat().st_size:
                raise ValueError("後処理済み音声が出力されていません")
        if music.exists():
            backup = folder / ".music_previous" / secrets.token_hex(8)
            backup.parent.mkdir(exist_ok=True)
            music.rename(backup)
            try: stage.rename(music)
            except BaseException: backup.rename(music); raise
        else: stage.rename(music)
        with transaction() as conn:
            conn.execute("UPDATE batches SET processed_revision=?,processing=0 WHERE id=?", (state["revision"], state["id"]))
            _event(conn, "selected_processed", {"batch_id": state["id"], "revision": state["revision"]})
            _export(conn, state["id"])
    finally:
        if stage.exists(): shutil.rmtree(stage)
        with transaction() as conn:
            conn.execute("UPDATE batches SET processing=0 WHERE id=?", (state["id"],))
    return 0
