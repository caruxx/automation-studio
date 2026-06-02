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


def _find_thumbnail(folder: Path):
    """フォルダ内のサムネ画像を返す (vol*.jpg / サムネイル.jpg 優先)"""
    for pat in ["vol*.jpg", "サムネイル.jpg", "vol*.png"]:
        for f in sorted(folder.glob(pat)):
            return f
    return None


def _extract_json_object(text: str):
    """Claude CLI 出力から JSON オブジェクトを抽出（フェンス除去 + 末尾カンマ補正）"""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    candidate = fence.group(1) if fence else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start < 0 or end <= start:
        return None
    blob = candidate[start:end + 1]
    try:
        return json.loads(blob)
    except json.JSONDecodeError:
        cleaned = re.sub(r",\s*([}\]])", r"\1", blob)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            return None


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
                    total_count: int, source_label: str) -> list:
    """CHUNK_SIZE ずつ Claude CLI を呼び、合計 total_count 件のタイトルを集める。

    `prompt_builder(count, chunk_index, total_chunks, avoid_hint)` → プロンプト文字列
      avoid_hint には既に取得済みタイトルを渡し、重複回避を指示する
    """
    all_titles = []
    remaining = total_count
    chunk_index = 0
    total_chunks = (total_count + CHUNK_SIZE - 1) // CHUNK_SIZE

    while remaining > 0:
        chunk_index += 1
        n = min(CHUNK_SIZE, remaining)
        # 直近 10 件を重複回避ヒントとして渡す
        avoid_hint = ", ".join(f'"{t}"' for t in all_titles[-10:]) if all_titles else ""
        prompt = prompt_builder(n, chunk_index, total_chunks, avoid_hint)
        # タイムアウトは n に比例（10件=120s, 20件=180s...）
        timeout = max(120, 60 + n * 10)
        print(f"  📦 [{chunk_index}/{total_chunks}] Claude CLI 呼び出し ({n}件, timeout={timeout}s)... [{source_label}]")
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


def propose_titles_from_thumbnail(cli_cmd: str, thumbnail: Path, count: int):
    """サムネ画像を読ませて count 個の英語タイトル候補を JSON で返させる（チャンク分割）"""
    print(f"🎨 Claude CLI でタイトル提案中... (サムネ: {thumbnail.name}, 目標 {count}件)")

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
        total_count=count, source_label="thumbnail",
    )
    print(f"  ✓ 合計 {len(titles)} 件のタイトル候補を取得")
    return titles


def propose_titles_from_persona(cli_cmd: str, channel_name: str, persona: str, count: int):
    """チャンネルペルソナから count 個の英語タイトル候補を JSON で返させる（チャンク分割）"""
    persona_clean = (persona or "").strip() or "(not set)"
    print(f"🧭 Claude CLI でタイトル提案中... (ペルソナ経由, 目標 {count}件)")

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
        total_count=count, source_label="persona",
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

    print(f"    📊 loudnorm: input_i={in_i} LUFS → target {TARGET_LUFS} LUFS (offset={target_offset})")


# ─── メイン処理 ──────────────────────────────────────────

def process_folder(folder: Path, cli_cmd: str = DEFAULT_CLI,
                   dry_run: bool = False, rename_only: bool = False):
    folder = folder.resolve()
    if not folder.is_dir():
        print(f"❌ フォルダが存在しません: {folder}")
        return 1

    if not rename_only:
        ensure_ffmpeg()

    # MP3 収集（フォルダ直下のみ）
    mp3s = sorted([
        p for p in folder.glob("*.mp3")
        if not p.name.startswith(".") and not _is_deleted_track(p)
    ])
    if not mp3s:
        print(f"⚠️ MP3 ファイルが見つかりません: {folder}")
        return 0

    print("=" * 60)
    print(f"  楽曲{'リネームのみ' if rename_only else '後処理'}: {folder.name}")
    print(f"  対象: {len(mp3s)} ファイル")
    print("=" * 60)

    # タイトル生成: サムネ優先 → 無ければチャンネルペルソナ → 失敗なら保持
    thumbnail = _find_thumbnail(folder)
    titles = None
    if thumbnail:
        print(f"  サムネ: {thumbnail.name}")
        try:
            titles = propose_titles_from_thumbnail(cli_cmd, thumbnail, len(mp3s))
            if not titles:
                print("⚠️ タイトルが 0 件。ペルソナへフォールバック")
                titles = None
        except RuntimeError as e:
            print(f"⚠️ サムネ由来のタイトル生成失敗: {e}")
            titles = None

    if not titles:
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
            titles = propose_titles_from_persona(cli_cmd, ch_name, persona, len(mp3s))
            if not titles:
                print("⚠️ ペルソナ提案も 0 件。ファイル名を保持します")
                titles = None
        except RuntimeError as e:
            print(f"⚠️ ペルソナ由来のタイトル生成失敗: {e}")
            titles = None

    # 取得数が不足しているなら明示
    if titles:
        print(f"  📊 取得済タイトル {len(titles)} 件 / 対象 {len(mp3s)} 件")
        if len(titles) < len(mp3s):
            print(f"     → {len(mp3s) - len(titles)} 件は元ファイル名を保持します")

    # 処理方針の表示
    if rename_only:
        print(f"\n📝 モード: リネームのみ（FFmpeg スキップ、music/ 出力なし）")
    else:
        print(f"\n🎛 モード: 後処理（original_music/ バックアップ + ffmpeg + music/ 出力）")

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
                # 2) 元ファイルを original_music/ に移動（コピーではなく移動）
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
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="orzz. 楽曲後処理")
    parser.add_argument("folder", help="動画フォルダのパス")
    parser.add_argument("--cli", default=DEFAULT_CLI, help="claude CLI コマンド")
    parser.add_argument("--dry-run", action="store_true", help="プレビューのみ")
    parser.add_argument("--rename-only", action="store_true",
                        help="リネームのみ実行（ffmpeg 処理スキップ）")
    args = parser.parse_args()
    sys.exit(process_folder(Path(args.folder), cli_cmd=args.cli,
                             dry_run=args.dry_run, rename_only=args.rename_only) or 0)
