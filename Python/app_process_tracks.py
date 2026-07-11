#!/usr/bin/env python3
"""orzz. 楽曲後処理スクリプト
==========================

動画フォルダ内の MP3 に対して:
  1. サムネ画像を Claude CLI で読み取り → 楽曲名候補を JSON 生成
  2. 各 MP3 をタイトルにリネーム（z_プレフィックスは維持）
  3. original_music/ にオリジナル MP3 をバックアップ（まだ無ければ）
  4. FFmpeg で末尾無音トリム + 8 秒フェードアウト + ゲイン正規化
  5. music/ フォルダへ処理済み MP3 を出力

前提:
  - PATH 上に `claude` CLI と `ffmpeg` が存在
  - 対象フォルダ直下に MP3 とサムネ画像 (vol*.jpg / サムネイル.jpg) がある

実行例:
  python3 app_process_tracks.py /path/to/77_orzz_260416
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

FADE_SECONDS = 8
SILENCE_DURATION = 0.2
SILENCE_THRESHOLD = "-80dB"
TARGET_LUFS = -16          # YouTube BGM 向けの目安
DEFAULT_CLI = "claude"
FFMPEG_TIMEOUT = 1800


# ─── ヘルパー ───────────────────────────────────────────

def _parse_likes(filename: str):
    stem = Path(filename).stem
    ext = Path(filename).suffix
    if stem.startswith("x_"):
        return 0, filename
    m = re.match(r"^(z+)_(.+)$", stem)
    if m:
        return len(m.group(1)), m.group(2) + ext
    return 0, filename


def _is_deleted_track(path: Path) -> bool:
    return Path(path).stem.startswith("x_")


def _apply_likes(base_filename: str, count: int) -> str:
    if count <= 0:
        return base_filename
    return f"{'z' * count}_{base_filename}"


def _mp3_duration(path: Path):
    """ffprobe で MP3 の長さ（秒）を返す。失敗時は None。"""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return float(probe.stdout.strip())
    except Exception:
        return None


def _dedup_group_key(filename: str):
    """同一 Suno タイトルのテイクをまとめるグループキー。

    likes プレフィックス(z_/zz_)・末尾の衝突サフィックス(_2,_3)・拡張子を除去し NFC 正規化。
    SUNO は1回の生成で同タイトルの2テイクを作るため、DL 時に `<title>.mp3` と
    `<title>_2.mp3` として落ちる。これらを同一グループに束ねる。
    """
    _, base = _parse_likes(filename)              # likes プレフィックス除去後の base 名
    stem = os.path.splitext(base)[0]
    stem = re.sub(r"_\d+$", "", stem)             # 衝突サフィックス _2/_3 を除去
    return unicodedata.normalize("NFC", stem).strip().lower()


def _dedup_same_title_keep_shorter(mp3s, dry_run: bool = False):
    """同一タイトル（SUNO の2テイク等）のうち最も短いものを残し、長い方を削除する。

    リネーム前に実行することで、2テイクが別タイトルに化けて両方残るのを防ぐ。
    戻り値: 生き残った Path のリスト（ソート済み）。
    """
    groups = {}
    for p in mp3s:
        groups.setdefault(_dedup_group_key(p.name), []).append(p)

    if not any(len(v) > 1 for v in groups.values()):
        return sorted(mp3s)

    print("\n--- 同タイトル重複の整理（長い方を削除）---")
    survivors = []
    removed = 0
    for files in groups.values():
        if len(files) <= 1:
            survivors.extend(files)
            continue
        # duration 取得（失敗は +inf 扱い＝削除候補側へ寄せる）。同尺ならファイル名が短い方を残す。
        with_dur = [(f, (_mp3_duration(f) if _mp3_duration(f) is not None else float("inf"))) for f in files]
        with_dur.sort(key=lambda fd: (fd[1], len(fd[0].name)))
        keep, keep_dur = with_dur[0]
        survivors.append(keep)
        keep_s = "?" if keep_dur == float("inf") else f"{keep_dur:.1f}s"
        print(f" 同タイトル {len(files)}件 → 残す: {keep.name} ({keep_s})")
        for f, d in with_dur[1:]:
            dur_s = "?" if d == float("inf") else f"{d:.1f}s"
            if dry_run:
                print(f"       (dry-run) 削除予定: {f.name} ({dur_s})")
            else:
                try:
                    f.unlink()
                    print(f" 削除: {f.name} ({dur_s}, 長い方)")
                    removed += 1
                except Exception as e:
                    print(f"       ⚠️ 削除失敗 {f.name}: {e}")
                    survivors.append(f)
    print(f" 重複削除: {removed} 件削除 / 残 {len(survivors)} 件")
    return sorted(survivors)


def _load_channel_suno_settings(folder: Path) -> dict:
    """per-channel `.app_channel_config.json` の suno 設定を読む。

    APP_CHANNEL_FOLDER があればそれを優先し、無ければ動画フォルダの親をチャンネル
    フォルダとして扱う。読み取れない場合は空 dict を返し、既定挙動を維持する。
    """
    env_folder = (os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    candidates = []
    if env_folder:
        candidates.append(Path(env_folder).expanduser() / ".app_channel_config.json")
    candidates.append(folder.parent / ".app_channel_config.json")
    for path in candidates:
        try:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8")) or {}
                suno = data.get("suno") or {}
                return suno if isinstance(suno, dict) else {}
        except Exception:
            continue
    return {}


def _clean_title_key(title: str) -> str:
    stem = Path(str(title or "")).stem
    stem = re.sub(r"^z+_", "", stem)
    stem = re.sub(r"\s+", " ", stem).strip().lower()
    return unicodedata.normalize("NFC", stem)


def _collect_used_title_keys(folder: Path) -> set[str]:
    """同一 vol とチャンネル履歴から既存タイトルを集める。"""
    used: set[str] = set()
    for sub in ("music", "original_music"):
        d = folder / sub
        if d.is_dir():
            for p in d.glob("*.mp3"):
                used.add(_clean_title_key(p.stem))
    for p in folder.glob("*.mp3"):
        used.add(_clean_title_key(p.stem))

    hist_paths = []
    try:
        hist_paths.append(Path(__file__).resolve().parent.parent / "config" / ".suno_history.json")
    except Exception:
        pass
    hist_paths.append(Path.home() / ".config" / "orzz" / ".suno_history.json")

    channel_ids = {
        (os.environ.get("APP_CHANNEL_ID") or "").strip(),
        (os.environ.get("APP_CHANNEL_NAME") or "").strip(),
        folder.parent.name,
    }
    try:
        channels_path = Path(__file__).resolve().parent.parent / "config" / "channels.json"
        channels = json.loads(channels_path.read_text(encoding="utf-8")) if channels_path.exists() else []
        parent_norm = str(folder.parent.resolve())
        for ch in channels if isinstance(channels, list) else []:
            ch_folder = str(ch.get("folder") or "")
            if ch_folder and (ch_folder == parent_norm or Path(ch_folder).name == folder.parent.name):
                channel_ids.add(str(ch.get("id") or "").strip())
    except Exception:
        pass
    channel_ids = {x for x in channel_ids if x}

    for path in hist_paths:
        try:
            if not path.exists():
                continue
            state = json.loads(path.read_text(encoding="utf-8")) or {}
            entries = []
            if channel_ids:
                entries = [state.get(cid) for cid in channel_ids if isinstance(state.get(cid), dict)]
            if not entries:
                entries = [v for v in state.values() if isinstance(v, dict)]
            for entry in entries:
                for title in entry.get("titles") or []:
                    used.add(_clean_title_key(str(title)))
                for song in entry.get("songs") or []:
                    if isinstance(song, dict):
                        used.add(_clean_title_key(str(song.get("title") or "")))
        except Exception:
            continue
    used.discard("")
    return used


def _fallback_unique_title(src: Path, used_keys: set[str], planned_keys: set[str]) -> str:
    """LLM 不足/失敗時の決定的な別名。_2 テイクは Reprise 系にする。"""
    likes, base = _parse_likes(src.name)
    del likes
    stem = os.path.splitext(base)[0].strip() or src.stem
    is_take_suffix = bool(re.search(r"_\d+$", stem))
    root = re.sub(r"_\d+$", "", stem).strip() or stem
    candidates = []
    if is_take_suffix:
        candidates.extend([
            f"{root} Reprise",
            f"{root} Second Take",
            f"{root} Afterglow",
        ])
    else:
        candidates.append(root)
        candidates.extend([
            f"{root} First Take",
            f"{root} Original Take",
        ])
    for candidate in candidates:
        key = _clean_title_key(candidate)
        if key and key not in used_keys and key not in planned_keys:
            return candidate
    n = 2
    while True:
        candidate = f"{root} Reprise {n}" if is_take_suffix else f"{root} Take {n}"
        key = _clean_title_key(candidate)
        if key not in used_keys and key not in planned_keys:
            return candidate
        n += 1


def _select_unique_titles(raw_titles: list[str] | None, mp3s: list[Path], used_keys: set[str]) -> list[str]:
    """LLM 候補を同一 vol/履歴と照合し、不足分は決定的フォールバックで埋める。"""
    selected: list[str] = []
    planned: set[str] = set()
    raw_iter = iter(raw_titles or [])
    for src in mp3s:
        chosen = None
        for title in raw_iter:
            safe = re.sub(r"[\\/:*?\"<>|]", "_", str(title)).strip()
            key = _clean_title_key(safe)
            if safe and key not in used_keys and key not in planned:
                chosen = safe
                break
        if not chosen:
            chosen = _fallback_unique_title(src, used_keys, planned)
        selected.append(chosen)
        planned.add(_clean_title_key(chosen))
    return selected


def _find_thumbnail(folder: Path):
    """フォルダ内のサムネ画像を返す (vol*.jpg / サムネイル.jpg 優先)"""
    for pat in ["vol*.jpg", "サムネイル.jpg", "vol*.png"]:
        for f in sorted(folder.glob(pat)):
            return f
    return None


# JSON 抽出は app_benchmark_common に集約（D10）
from app_benchmark_common import extract_json_object as _extract_json_object


# ─── Claude CLI で楽曲タイトル提案 ───────────────────────

def _run_claude_titles(cli_cmd: str, prompt: str, allow_read: bool = False, timeout: int = 180):
    """Claude→Codex フォールバック共通ランナーで {"titles":[...]} を取得する共通ヘルパー"""
    from app_llm_runner import run_llm
    out = run_llm(prompt, cli_cmd=cli_cmd, timeout=timeout, allow_read=allow_read, label="track-titles")
    obj = _extract_json_object(out)
    if not obj or "titles" not in obj:
        raise RuntimeError(f"JSON 抽出失敗: {out[:200]}")
    titles = [str(t).strip() for t in obj.get("titles", []) if str(t).strip()]
    if not titles:
        raise RuntimeError("タイトルが空")
    return titles


CHUNK_SIZE = 10   # 1 回の Claude 呼び出しで要求する曲数（JSON 肥大化防止）


def _chunked_titles(cli_cmd: str, *, prompt_builder, allow_read: bool,
                    total_count: int, source_label: str,
                    avoid_titles: list[str] | None = None) -> list:
    """CHUNK_SIZE ずつ Claude CLI を呼び、合計 total_count 件のタイトルを集める。

    `prompt_builder(count, chunk_index, total_chunks, avoid_hint)` → プロンプト文字列
      avoid_hint には既に取得済みタイトルを渡し、重複回避を指示する
    """
    all_titles = []
    global_avoid = [str(t).strip() for t in (avoid_titles or []) if str(t).strip()]
    remaining = total_count
    chunk_index = 0
    total_chunks = (total_count + CHUNK_SIZE - 1) // CHUNK_SIZE

    while remaining > 0:
        chunk_index += 1
        n = min(CHUNK_SIZE, remaining)
        # 直近 10 件を重複回避ヒントとして渡す
        avoid_recent = global_avoid[-40:] + all_titles[-10:]
        avoid_hint = ", ".join(f'"{t}"' for t in avoid_recent) if avoid_recent else ""
        prompt = prompt_builder(n, chunk_index, total_chunks, avoid_hint)
        # タイムアウトは n に比例（10件=120s, 20件=180s...）
        timeout = max(120, 60 + n * 10)
        print(f" [{chunk_index}/{total_chunks}] Claude CLI 呼び出し ({n}件, timeout={timeout}s)... [{source_label}]")
        try:
            titles = _run_claude_titles(cli_cmd, prompt, allow_read=allow_read, timeout=timeout)
        except RuntimeError as e:
            print(f"  ⚠️ チャンク {chunk_index} 取得失敗: {e}")
            # 1チャンク失敗しても続行
            remaining -= n
            continue
        # 重複除去（既存と部分一致含む）
        new_titles = []
        lower_existing = {t.lower() for t in all_titles}
        for t in titles:
            if t.lower() not in lower_existing:
                new_titles.append(t)
                lower_existing.add(t.lower())
        print(f"     → 取得 {len(titles)}, 新規 {len(new_titles)}")
        all_titles.extend(new_titles[:n])
        remaining = total_count - len(all_titles)
        if len(titles) == 0:
            # 無限ループ回避: 連続で0件ならチャンク枠分進める
            remaining -= n

    if len(all_titles) < total_count:
        print(f"  ⚠️ 目標 {total_count} 件に対して {len(all_titles)} 件のみ取得")
    return all_titles[:total_count]


def propose_titles_from_thumbnail(cli_cmd: str, thumbnail: Path, count: int,
                                  avoid_titles: list[str] | None = None):
    """サムネ画像を読ませて count 個の英語タイトル候補を JSON で返させる（チャンク分割）"""
    print(f" Claude CLI でタイトル提案中... (サムネ: {thumbnail.name}, 目標 {count}件)")

    def _build(n, idx, total_chunks, avoid_hint):
        avoid = f"\n- Avoid these titles (already proposed): {avoid_hint}" if avoid_hint else ""
        return f"""画像ファイル '{thumbnail}' を Read ツールで読み取ってください。
その画像の雰囲気・色味・シーンに合う英語の楽曲タイトルを {n} 個 提案してください。
（これは {total_chunks} チャンクのうち {idx} 回目。合計で {n * total_chunks} 件程度まで多様性を確保したい）

【Output Format — JSON ONLY】
Respond with a SINGLE JSON object, no markdown fences, no commentary.
Schema:
{{"titles": ["Title One", "Title Two", ...]}}

Rules:
- Output exactly {n} titles.
- All titles MUST be English.
- Each title should feel different (mood/theme/imagery/time-of-day).
- Avoid numbering, punctuation like colons, or explanatory text.{avoid}
- Output ONLY the JSON object.
"""
    titles = _chunked_titles(
        cli_cmd, prompt_builder=_build, allow_read=True,
        total_count=count, source_label="thumbnail", avoid_titles=avoid_titles,
    )
    print(f"  ✓ 合計 {len(titles)} 件のタイトル候補を取得")
    return titles


def propose_titles_from_persona(cli_cmd: str, channel_name: str, persona: str, count: int,
                                avoid_titles: list[str] | None = None):
    """チャンネルペルソナから count 個の英語タイトル候補を JSON で返させる（チャンク分割）"""
    persona_clean = (persona or "").strip() or "(not set)"
    print(f" Claude CLI でタイトル提案中... (ペルソナ経由, 目標 {count}件)")

    def _build(n, idx, total_chunks, avoid_hint):
        avoid = f"\n- Avoid these titles (already proposed): {avoid_hint}" if avoid_hint else ""
        return f"""You are creating English song titles for a YouTube BGM channel named "{channel_name or 'orzz.'}".

Channel persona / concept:
{persona_clean}

Generate {n} distinct, evocative English song titles that fit this channel's world and mood.
(This is chunk {idx} of {total_chunks}. Aim for variety across chunks.)

Rules:
- Output exactly {n} titles.
- All titles MUST be English.
- Each title should feel different (mood/theme/imagery/time-of-day).
- Avoid numbering, punctuation like colons, or explanatory text.{avoid}
- Titles should evoke scenery, atmosphere, or emotion that matches the channel persona.

Respond with a SINGLE JSON object, no markdown fences, no commentary:
{{"titles": ["Title One", "Title Two", ...]}}
"""
    titles = _chunked_titles(
        cli_cmd, prompt_builder=_build, allow_read=False,
        total_count=count, source_label="persona", avoid_titles=avoid_titles,
    )
    print(f"  ✓ 合計 {len(titles)} 件のタイトル候補を取得")
    return titles


def _load_channel_context(folder: Path):
    """チャンネル名・ペルソナを読み取る。

    P2-2: APP_CHANNEL_FOLDER env が立っていれば、そちらの per-channel config から
    取得して global dashboard_config を無視。複数チャンネル並列で取り違えを防ぐ。"""
    import os as _os
    ch_name, persona = "", ""
    # 優先 1: env override（pipeline / job 経由）
    env_folder = (_os.environ.get("APP_CHANNEL_FOLDER") or "").strip()
    if env_folder:
        env_name = (_os.environ.get("APP_CHANNEL_NAME") or "").strip()
        if env_name:
            ch_name = env_name
        per = Path(env_folder) / ".app_channel_config.json"
        if per.exists():
            try:
                pc = json.loads(per.read_text(encoding="utf-8")) or {}
                if not ch_name:
                    ch_name = str(pc.get("channel_name") or "").strip()
                persona = str(pc.get("persona") or "").strip()
            except Exception:
                pass
        if ch_name or persona:
            return ch_name, persona
    # 優先 2: global dashboard_config（UI active channel）
    cfg_path = Path.home() / ".config" / "orzz" / "dashboard_config.json"
    if cfg_path.exists():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            ch_name = str(data.get("channel_name") or "").strip()
            persona = str(data.get("persona") or "").strip()
        except Exception:
            pass
    return ch_name, persona


# ─── FFmpeg 処理 ─────────────────────────────────────────

def ensure_ffmpeg():
    if not shutil.which("ffmpeg"):
        print("❌ ffmpeg がインストールされていません。")
        print("  brew install ffmpeg")
        sys.exit(1)


def process_mp3(src: Path, dst: Path):
    """末尾無音トリム + 8 秒フェードアウト + two-pass loudnorm 正規化 → dst に書き出す"""
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(src)],
        capture_output=True, text=True, timeout=30,
    )
    try:
        duration = float(probe.stdout.strip())
    except Exception:
        raise RuntimeError(f"duration 取得失敗: {probe.stderr}")

    fade_start = max(0, duration - FADE_SECONDS)

    pre_filter = (
        f"silenceremove=stop_periods=-1:stop_duration={SILENCE_DURATION}:"
        f"stop_threshold={SILENCE_THRESHOLD},"
        f"afade=t=out:st={fade_start}:d={FADE_SECONDS}"
    )

    # ── 1st pass: measure ──
    pass1_filter = (
        f"{pre_filter},"
        f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11:print_format=json"
    )
    res1 = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", str(src),
         "-af", pass1_filter, "-f", "null", "-"],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT,
    )
    if res1.returncode != 0:
        raise RuntimeError(f"loudnorm 1st pass 失敗:\n{res1.stderr}")

    # JSON ブロックを stderr 末尾から抽出（{ で始まり } で閉じる、ネスト無し）
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", res1.stderr, re.DOTALL)
    if not m:
        raise RuntimeError(f"loudnorm measured 値を抽出不可:\n{res1.stderr[-2000:]}")
    try:
        measured = json.loads(m.group(0))
    except Exception as e:
        raise RuntimeError(f"loudnorm JSON parse 失敗: {e}\n{m.group(0)}")

    in_i = measured.get("input_i", "0")
    in_tp = measured.get("input_tp", "0")
    in_lra = measured.get("input_lra", "0")
    in_thresh = measured.get("input_thresh", "-70")
    target_offset = measured.get("target_offset", "0")

    # ── 2nd pass: apply ──
    # loudnorm の後段に alimiter (-1dBFS) を直列に置き、Premiere 側で
    # 万一マスター/トラックゲインで持ち上がっても source 側で確実に -1 dBFS を超えないようにする。
    pass2_filter = (
        f"{pre_filter},"
        f"loudnorm=I={TARGET_LUFS}:TP=-1.5:LRA=11"
        f":measured_I={in_i}:measured_LRA={in_lra}:measured_TP={in_tp}"
        f":measured_thresh={in_thresh}:offset={target_offset}"
        f":linear=true:print_format=summary,"
        f"alimiter=limit=-1.0dB:level=disabled"
    )
    res2 = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-nostats", "-i", str(src),
         "-af", pass2_filter,
         "-c:a", "libmp3lame", "-b:a", "192k", str(dst)],
        capture_output=True, text=True, timeout=FFMPEG_TIMEOUT,
    )
    if res2.returncode != 0:
        raise RuntimeError(f"loudnorm 2nd pass 失敗:\n{res2.stderr}")

    print(f" loudnorm: input_i={in_i} LUFS → target {TARGET_LUFS} LUFS (offset={target_offset})")


# ─── 公開前整備: AI透かし除去 + ID3タグ付与 + ファイル名正規化 ──────────

def _filesafe(title: str) -> str:
    """タイトルをファイル名に使える形へ（コロン等を _ に）。NFC でなく元文字保持。"""
    return re.sub(r'[\\/:*?"<>|]', "_", title).strip()


def _vol_from_folder(folder: Path):
    """フォルダ名先頭の連番(例 '2_sk_260604' → 2)を vol 番号として返す。取れなければ None。"""
    m = re.match(r"^(\d+)_", folder.name)
    return int(m.group(1)) if m else None


def _load_tag_draft(folder: Path, draft_path: Path | None):
    """整備で参照する draft（title/genre 対応の元）を読む。

    優先: 明示の draft_path → folder 内の *_draft.json / draft.json。
    無ければ {} を返す（フォールバック genre のみで進む）。
    戻り値: filesafe(title).lower() → kind('bossa'/'rnb' 等) の dict と、
            filesafe(title).lower() → 正式 title の dict。
    """
    candidates = []
    if draft_path:
        candidates.append(Path(draft_path))
    # フォルダ直下の draft 候補（_draft.json で終わるもの優先）。
    # 複数形 *_drafts.json（generate_mixed_drafts の suno_mix_drafts.json 等）も拾う。
    candidates += (sorted(folder.glob("*_draft.json"))
                   + sorted(folder.glob("*_drafts.json"))
                   + sorted(folder.glob("draft.json")))
    data = None
    used = None
    for c in candidates:
        try:
            if c and c.exists():
                data = json.loads(c.read_text(encoding="utf-8"))
                used = c
                break
        except Exception:
            continue
    # {"songs":[...]} 形式（generate_mixed_drafts 保存形）も list に展開して受理する。
    if isinstance(data, dict) and isinstance(data.get("songs"), list):
        data = data["songs"]
    kind_map, title_map = {}, {}
    if isinstance(data, list):
        for s in data:
            if not isinstance(s, dict):
                continue
            t = str(s.get("title") or "").strip()
            if not t:
                continue
            key = _filesafe(t).lower()
            title_map[key] = t
            g = str(s.get("genre") or "").strip().lower()
            if g:
                kind_map[key] = g
    return kind_map, title_map, used


def _probe_format_tags(path: Path) -> dict:
    """ffprobe で format_tags を小文字キー dict で返す。失敗時 {}。"""
    try:
        pr = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags",
             "-of", "json", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        tags = (json.loads(pr.stdout).get("format", {}) or {}).get("tags", {}) or {}
        return {k.lower(): v for k, v in tags.items()}
    except Exception:
        return {}


def _normalize_original_names(folder: Path, music_set: set, dry_run: bool = False):
    """original_music/ のファイル名を music/ の正式名に揃える（末尾 _数字 のみ除去）。

    /tmp/sukima_rename_orig.py 相当。music_set を正解集合とし、衝突サフィックス(_2/_3)を
    1回だけ除去して一致するものだけ rename。'The X_15' のような _数字付き正式名を
    music_set に含むものは music_set 側一致で skip されるため誤除去されない。
    """
    orig = folder / "original_music"
    if not orig.is_dir():
        return {"renamed": 0, "skipped": 0, "unknown": []}
    renamed, skipped, unknown = 0, 0, []
    for f in sorted(orig.glob("*.mp3")):
        if f.name in music_set:
            skipped += 1
            continue
        cand = re.sub(r"_\d+(\.mp3)$", r"\1", f.name)
        dst = orig / cand
        if cand in music_set and not dst.exists():
            if dry_run:
                print(f"    (dry-run) rename: {f.name} → {cand}")
            else:
                f.rename(dst)
                print(f"    ✓ rename: {f.name} → {cand}")
            renamed += 1
        elif dst.exists():
            print(f"    ⚠ 衝突skip: {f.name}（{cand} 既存）")
            unknown.append(f.name)
        else:
            print(f"    ? music/ に無い: {f.name}")
            unknown.append(f.name)
    return {"renamed": renamed, "skipped": skipped, "unknown": unknown}


def _apply_metadata_tags(folder: Path, *, album: str | None = None,
                         artist: str = "SUKIMA",
                         genre_default: str = "Bossa Nova",
                         genre_by_kind: dict | None = None,
                         draft_path: Path | None = None,
                         dry_run: bool = False) -> bool:
    """公開前整備: AI透かし(suno comment)除去 + ID3タグ付与 + ファイル名正規化。

    /tmp/sukima_tag.py + sukima_fix_genre.py + sukima_rename_orig.py を統合移植。

    フロー:
      1. music/*.mp3 ごとに ffmpeg -map_metadata -1（suno透かし comment 除去）
         + title=正式名 / artist / album / genre(曲別) + -c copy（再エンコード無し）で
         music_tagged/ に出力。
      2. ffprobe で全曲「suno 透かし消失 & title 付与」を検証。
      3. 全曲 OK のときだけ music → music_raw_pre_tag に退避し music_tagged → music へ
         原子入替（rename）。1曲でも失敗したら music は無傷のまま music_tagged を残す。
      4. 入替後、original_music/ のファイル名も music/ の正式名へ正規化。

    既存 music を破壊しない設計（検証前に元 music を一切上書きしない）。
    戻り値: 入替まで成功したら True、検証失敗 or 対象無しは False。
    """
    genre_by_kind = genre_by_kind or {}
    music = folder / "music"
    if not music.is_dir():
        print(f"  ⚠ music/ が無いため整備スキップ: {folder.name}")
        return False
    mp3s = sorted([p for p in music.glob("*.mp3") if not p.name.startswith(".")])
    if not mp3s:
        print(f"  ⚠ music/ に MP3 が無いため整備スキップ: {folder.name}")
        return False

    if not album:
        vol = _vol_from_folder(folder)
        album = f"SUKIMA vol.{vol}" if vol is not None else "SUKIMA"

    kind_map, title_map, draft_used = _load_tag_draft(folder, draft_path)
    print(f" 整備(タグ付与+透かし除去): {folder.name}")
    print(f"     album='{album}' artist='{artist}' genre_default='{genre_default}'")
    if draft_used:
        print(f"     draft={draft_used.name}（title/genre {len(title_map)}件マッチ用）")
    else:
        print(f"     draft 無し → title=既存ファイル名 / genre={genre_default} 固定")

    out = folder / "music_tagged"
    if out.exists():
        if dry_run:
            print(f"    (dry-run) 既存 music_tagged/ を再利用せず削除予定")
        else:
            shutil.rmtree(out)
    if not dry_run:
        out.mkdir()

    results = []
    for f in mp3s:
        base = re.sub(r"_\d+$", "", f.stem)          # 末尾 _2/_3 除去
        key = base.strip().lower()
        title = title_map.get(key) or base           # draft マッチ無しは base をタイトルに
        matched = key in title_map
        kind = kind_map.get(key, "")
        genre = genre_by_kind.get(kind, genre_default)
        new_fname = _filesafe(title) + ".mp3"
        out_path = out / new_fname
        if dry_run:
            print(f"    (dry-run) {f.name} → {new_fname}  title='{title}' genre='{genre}' match={matched}")
            results.append({"src": f.name, "verified": True, "title": title})
            continue
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
               "-i", str(f), "-map_metadata", "-1",
               "-metadata", f"title={title}",
               "-metadata", f"artist={artist}",
               "-metadata", f"album={album}",
               "-metadata", f"genre={genre}",
               "-c", "copy", str(out_path)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
        verified = False
        title_set = comment_left = None
        if out_path.exists():
            tags_lc = _probe_format_tags(out_path)
            comment_left = tags_lc.get("comment")
            title_set = tags_lc.get("title")
            has_suno = any("suno" in str(v).lower() for v in tags_lc.values())
            verified = (not has_suno) and (title_set == title)
        results.append({
            "src": f.name, "matched_draft": matched, "title": title, "genre": genre,
            "out": new_fname, "ffmpeg_ok": r.returncode == 0,
            "comment_left": comment_left, "title_set": title_set, "verified": verified,
            "stderr": r.stderr[:200] if r.returncode != 0 else "",
        })
        print(f"    {'✓' if verified else '✗'} {f.name} → {new_fname}  genre='{genre}' verified={verified}")

    verified_n = sum(1 for x in results if x.get("verified"))
    print(f"\n  結果: {verified_n}/{len(results)} verified")

    if dry_run:
        print("  (dry-run) 入替しません。")
        return True

    all_ok = len(results) > 0 and all(x.get("verified") for x in results)
    if not all_ok:
        print("  ❌ 検証失敗の曲あり → music は無傷のまま。music_tagged/ に出力済み。")
        return False

    raw = folder / "music_raw_pre_tag"
    if raw.exists():
        print(f"  ⚠ {raw.name} が既存 → 入替中止（手動確認を）。music_tagged/ に出力済み。")
        return False
    music.rename(raw)
    out.rename(music)
    print(f" 入替完了: 旧music→music_raw_pre_tag, music_tagged→music")

    # original_music/ のファイル名も新 music/ の正式名へ正規化
    music_set = set(p.name for p in music.glob("*.mp3"))
    rn = _normalize_original_names(folder, music_set, dry_run=False)
    if rn["renamed"] or rn["unknown"]:
        print(f" original_music 正規化: renamed={rn['renamed']} unknown={rn['unknown']}")
    return True


# ─── メイン処理 ──────────────────────────────────────────

def process_folder(folder: Path, cli_cmd: str = DEFAULT_CLI,
                   dry_run: bool = False, rename_only: bool = False,
                   no_dedup: bool = False, keep_names: bool = False,
                   skip_tagging: bool = True, album: str | None = None,
                   artist: str = "SUKIMA", genre: str | None = None,
                   genre_by_kind: dict | None = None,
                   draft_path: Path | None = None):
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"❌ フォルダが存在しません: {folder}")
        return 1

    if not rename_only:
        ensure_ffmpeg()

    suno_settings = _load_channel_suno_settings(folder)
    keep_both_takes = bool(suno_settings.get("keep_both_takes", False))
    used_title_keys = _collect_used_title_keys(folder) if keep_both_takes else set()
    avoid_title_hint = sorted(used_title_keys) if keep_both_takes else []

    # MP3 収集（フォルダ直下のみ）
    mp3s = sorted([
        p for p in folder.glob("*.mp3")
        if not p.name.startswith(".") and not _is_deleted_track(p)
    ])
    source_from_original = False
    if keep_both_takes and not mp3s:
        original_dir = folder / "original_music"
        if original_dir.is_dir():
            mp3s = sorted([
                p for p in original_dir.glob("*.mp3")
                if not p.name.startswith(".") and not _is_deleted_track(p)
            ])
            source_from_original = bool(mp3s)
    if not mp3s:
        # フォルダ直下にも music/ にも 1 曲も無いなら、この vol は素材ゼロ。
        # 静かに成功を返すと後段 step が空のまま走り切ってしまう（vol131 で実例）
        music_dir = folder / "music"
        music_mp3s = sorted(music_dir.glob("*.mp3")) if music_dir.is_dir() else []
        if not music_mp3s:
            print(f"❌ MP3 が 1 件もありません（フォルダ直下・music/ とも空）: {folder}")
            return 1
        if not skip_tagging:
            print(f"フォルダ直下MP3なし → 既存 music/ の公開前整備のみ実行: {folder.name}")
            if rename_only:
                ensure_ffmpeg()
            ok = _apply_metadata_tags(
                folder,
                album=album,
                artist=artist,
                genre_default=(genre or "Bossa Nova"),
                genre_by_kind=genre_by_kind,
                draft_path=draft_path,
                dry_run=dry_run,
            )
            return 0 if ok else 1
        print(f"⚠️ MP3 ファイルが見つかりません: {folder}")
        # 0曲は成功ではない: 静かに OK を返すと後段 step が空のまま進み、
        # 生ファイル名 (_2 付き) の動画が出来上がる事故になる（vol128/131 で実例）
        return 1

    # ── 後処理の順序: ① 同タイトル重複（SUNO の2テイク）の長い方を削除 ──
    #    リネーム/フェードより前に行う。リネーム後はタイトルが別々に化けて束ねられなくなるため。
    if keep_both_takes:
        print("  設定: suno.keep_both_takes=true → 同タイトル2テイクを両方採用")
        if source_from_original:
            print("  入力: original_music/ の原本を保持したまま music/ 出力計画を作成")
    elif not no_dedup:
        mp3s = _dedup_same_title_keep_shorter(mp3s, dry_run=dry_run)
        if not mp3s:
            print("⚠️ 重複削除後に対象が 0 件になりました")
            return 0

    print("=" * 60)
    print(f"  楽曲{'リネームのみ' if rename_only else '後処理'}: {folder.name}")
    print(f"  対象: {len(mp3s)} ファイル")
    print("=" * 60)

    # タイトル生成: サムネ優先 → 無ければチャンネルペルソナ → 失敗なら保持
    # keep_names=True なら既存ファイル名を維持（タイトル再生成を一切スキップ）
    if keep_names:
        print(" --keep-names: 既存ファイル名を維持（タイトル再生成スキップ）")
    thumbnail = None if keep_names else _find_thumbnail(folder)
    titles = None
    if thumbnail:
        print(f"  サムネ: {thumbnail.name}")
        try:
            titles = propose_titles_from_thumbnail(
                cli_cmd, thumbnail, len(mp3s), avoid_titles=avoid_title_hint,
            )
            if not titles:
                print("⚠️ タイトルが 0 件。ペルソナへフォールバック")
                titles = None
        except RuntimeError as e:
            print(f"⚠️ サムネ由来のタイトル生成失敗: {e}")
            titles = None

    if not titles and not keep_names:
        # フォールバック: チャンネルペルソナから提案
        ch_name, persona = _load_channel_context(folder)
        # ペルソナ空なら既定コンセプトを使用（ハードコード最終フォールバック）
        if not persona:
            persona = ("AI-generated instrumental BGM channel. "
                       "Mood: lounge, chill, jazz, cinematic, golden hour, night drive, "
                       "elegant cafe, luxury hotel lobby. Instrumental only, no vocals.")
            print(f"  ⚙️ ペルソナ未設定 → 既定コンセプトを使用")
        if not ch_name:
            ch_name = "orzz."
        print(f"  ⚙️ フォールバック: チャンネルペルソナから提案")
        print(f"     channel={ch_name}, persona={persona[:80]}...")
        try:
            titles = propose_titles_from_persona(
                cli_cmd, ch_name, persona, len(mp3s), avoid_titles=avoid_title_hint,
            )
            if not titles:
                print("⚠️ ペルソナ提案も 0 件。ファイル名を保持します")
                titles = None
        except RuntimeError as e:
            print(f"⚠️ ペルソナ由来のタイトル生成失敗: {e}")
            titles = None

    # 取得数が不足しているなら明示
    if titles:
        print(f" 取得済タイトル {len(titles)} 件 / 対象 {len(mp3s)} 件")
        if len(titles) < len(mp3s):
            print(f"     → {len(mp3s) - len(titles)} 件は元ファイル名を保持します")
    if keep_both_takes and not keep_names:
        titles = _select_unique_titles(titles, mp3s, used_title_keys)
        print(f" 一意化済タイトル {len(titles)} 件 / 対象 {len(mp3s)} 件（同一vol・履歴重複を回避）")

    # 処理方針の表示
    if rename_only:
        print(f"\n モード: リネームのみ（FFmpeg スキップ、music/ 出力なし）")
    else:
        print(f"\n モード: 後処理（original_music/ バックアップ + ffmpeg + music/ 出力）")

    # プレビュー + 一意化
    print("\n--- 処理プレビュー ---")
    plans = []
    used_names = set()
    for i, src in enumerate(mp3s):
        likes, base = _parse_likes(src.name)
        if titles and i < len(titles):
            safe_title = re.sub(r"[\\/:*?\"<>|]", "_", titles[i]).strip()
            new_base = f"{safe_title}.mp3" if safe_title else base
        else:
            new_base = base
        new_name = _apply_likes(new_base, likes)
        # 重複回避: 同名が計画済ならサフィックス
        counter = 2
        while new_name in used_names:
            stem, ext = os.path.splitext(new_base)
            new_name = _apply_likes(f"{stem}_{counter}{ext}", likes)
            counter += 1
        used_names.add(new_name)
        plans.append({"src": src, "new_name": new_name, "likes": likes})
        print(f"  [{i+1:02d}] ♥{likes} {src.name}")
        print(f"       → {new_name}")

    if dry_run:
        print("\n(dry-run) 実行しません。")
        return 0

    success = 0

    if rename_only:
        # リネームのみ: フォルダ直下で rename（衝突回避）
        for i, plan in enumerate(plans):
            src: Path = plan["src"]
            new_name: str = plan["new_name"]
            dst = folder / new_name
            print(f"\n[{i+1}/{len(plans)}] {src.name}")
            try:
                if dst.resolve() == src.resolve():
                    print(f"  ─ 既に一致: {new_name}")
                    success += 1
                    continue
                if dst.exists():
                    # 既存ファイルを退避
                    backup = folder / f"__old_{dst.name}"
                    if backup.exists():
                        backup.unlink()
                    dst.rename(backup)
                src.rename(dst)
                print(f"  ✓ renamed: {new_name}")
                success += 1
            except Exception as e:
                # スタックや ffmpeg stderr を含めて全文出力（黙って次に進まない）
                import traceback
                print(f"  ❌ 失敗: {e}")
                print(traceback.format_exc())
    else:
        # 後処理: original_music に移動 + ffmpeg → music 出力（ルート直下は空になる）
        original_dir = folder / "original_music"
        music_dir = folder / "music"
        original_dir.mkdir(exist_ok=True)
        music_dir.mkdir(exist_ok=True)
        for i, plan in enumerate(plans):
            src: Path = plan["src"]
            new_name: str = plan["new_name"]
            print(f"\n[{i+1}/{len(plans)}] {src.name}")
            backup_path = original_dir / new_name
            try:
                # 1) ffmpeg 処理 → music/ に出力（元ファイルから直接処理）
                out_path = music_dir / new_name
                process_mp3(src, out_path)
                print(f"  ✓ processed: music/{new_name}")
                # 2) 原本保全。keep_both_takes では original_music/ の原本名をそのまま残す。
                if keep_both_takes:
                    if src.parent == original_dir:
                        print(f"  ✓ kept original: original_music/{src.name}")
                    else:
                        original_path = original_dir / src.name
                        if not original_path.exists():
                            shutil.copy2(src, original_path)
                            print(f"  ✓ copied original: original_music/{src.name}")
                        src.unlink()
                        print(f"  ✓ removed source: {src.name}")
                else:
                    if backup_path.exists():
                        backup_path.unlink()  # 同名が既に存在なら上書き
                    src.rename(backup_path)
                    print(f"  ✓ moved: original_music/{new_name}")
                # ルート直下には残さない
                success += 1
            except Exception as e:
                # スタックや ffmpeg stderr を含めて全文出力（黙って次に進まない）
                import traceback
                print(f"  ❌ 失敗: {e}")
                print(traceback.format_exc())

    print("\n" + "=" * 60)
    print(f"  完了: {success}/{len(plans)}")
    print("=" * 60)

    # ── 公開前整備（透かし除去 + ID3タグ + ファイル名正規化）──
    # skip_tagging=True（既定）の間は完全に従来挙動。明示有効化（apply-tags）時のみ作動。
    # rename_only は music/ を作らないので整備対象外。
    if not skip_tagging and not rename_only:
        try:
            _apply_metadata_tags(
                folder,
                album=album,
                artist=artist,
                genre_default=(genre or "Bossa Nova"),
                genre_by_kind=genre_by_kind,
                draft_path=draft_path,
                dry_run=dry_run,
            )
        except Exception as e:
            import traceback
            print(f"  ⚠ 整備（タグ付与）でエラー（後処理本体は完了済み）: {e}")
            print(traceback.format_exc())

    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="orzz. 楽曲後処理")
    parser.add_argument("folder", help="動画フォルダのパス")
    parser.add_argument("--cli", default=DEFAULT_CLI, help="claude CLI コマンド")
    parser.add_argument("--dry-run", action="store_true", help="プレビューのみ")
    parser.add_argument("--rename-only", action="store_true",
                        help="リネームのみ実行（ffmpeg 処理スキップ）")
    parser.add_argument("--no-dedup", action="store_true",
                        help="同タイトル重複（SUNO の2テイク）の長い方削除をスキップ")
    parser.add_argument("--keep-names", action="store_true",
                        help="既存ファイル名を維持（サムネ/ペルソナからのタイトル再生成をスキップ）")
    parser.add_argument("--apply-tags", action="store_true",
                        help="後処理後に公開前整備（透かし除去+ID3タグ+ファイル名正規化）を実行。"
                             "未指定（既定）は従来挙動で整備しない。")
    parser.add_argument("--album", default=None,
                        help="ID3 album（未指定はフォルダ連番から 'SUKIMA vol.{N}' を自動生成）")
    parser.add_argument("--artist", default="SUKIMA", help="ID3 artist（既定 SUKIMA）")
    parser.add_argument("--genre", default=None,
                        help="ID3 genre のフォールバック（draft マッチ無し曲に使用。既定 Bossa Nova）")
    parser.add_argument("--genre-map", default=None,
                        help='draft の kind→genre 対応の JSON（例 \'{"bossa":"Bossa Nova","rnb":"R&B/Soul"}\'）')
    parser.add_argument("--draft", default=None,
                        help="title/genre 対応用 draft JSON（未指定はフォルダ内 *_draft.json を探索）")
    args = parser.parse_args()
    genre_by_kind = None
    if args.genre_map:
        try:
            gm = json.loads(args.genre_map)
            genre_by_kind = {str(k).lower(): str(v) for k, v in gm.items()} if isinstance(gm, dict) else None
        except Exception as e:
            print(f"⚠ --genre-map の JSON parse 失敗（無視）: {e}")
    sys.exit(process_folder(Path(args.folder), cli_cmd=args.cli,
                             dry_run=args.dry_run, rename_only=args.rename_only,
                             no_dedup=args.no_dedup, keep_names=args.keep_names,
                             skip_tagging=(not args.apply_tags),
                             album=args.album, artist=args.artist, genre=args.genre,
                             genre_by_kind=genre_by_kind,
                             draft_path=(Path(args.draft) if args.draft else None)) or 0)
