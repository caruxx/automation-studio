#!/usr/bin/env python3
"""ループ連結方式レンダラー（Premiere/AME 代替の ffmpeg 書き出しエンジン）。

設計の肝（プロトタイプ検証済 2026-06-13, 11_HN で実測）:
- BGM 動画のビデオは実質「静止画」(スペクトラム廃止 jsx_bundle.py:1042) → 全フレームを
  AME で再エンコードするのは丸ごと無駄。静止画を **1 GOP だけエンコード**して
  `-stream_loop -c copy` で目標尺まで連結すれば、映像組み立ては数秒で終わる。
- 音声は曲を concat → 目標尺トリム → ラスト 20s フェード → ハードリミッタ → AAC。
- 字幕(SRT) / チャプター(TC) は曲尺から決定的に生成（Premiere 不要）。
- 出力はローカル scratch で組み立て → 最終 mp4 を 1 回だけ宛先へコピー（Drive sync storm 回避）。

実測（11_HN_260523 / 3h / 静止画1枚）:
  映像組み立て ≈ 5.4s / 音声ビルド ≈ 332s(エンコード律速・キャッシュで再利用) /
  A/V ズレ 0.000s / 継ぎ目破綻なし（全フレーム decode 検証）/ 容量 1.2GB(AME 約6.4GB より小)。
  → 初回フル ≈ 6 分 (vs AME 24-40 分)、画像差し替え再レンダ ≈ 11 秒（音声キャッシュ再利用）。

CLI:
  python3 app_ffrender.py --vol-folder <folder> [--duration 10800] [--output-path <mp4>]
  exit 0=成功 / 1=失敗
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# SRT/TC 生成は app_premiere の純関数を再利用（Premiere に触れない関数のみ）
from app_premiere import generate_srt, generate_timecode, _read_file_prefix  # noqa: E402
from app_timeline import TIMELINE_NAME, load as load_vol_timeline  # noqa: E402

# ── AME 目標スペック（HN_vol11.mp4 実測値に合わせる） ──────────────────
FPS = "30000/1001"          # 29.97
WIDTH, HEIGHT = 1920, 1080
GOP_FRAMES = 150            # 1 ループ素材 = 150 フレーム = 1 closed GOP（約 5.005s）
DEFAULT_CRF = 18           # 静止画ループ素材の品質。容量ノブ（28 なら約 1/4）
DEFAULT_AUDIO_BITRATE = "320k"  # AME は 317k
FADE_SEC = 20              # ラストフェードアウト
# alimiter's default ``level=true`` raises the limited signal back to 0 dBFS.
# Keep it disabled so ``limit=0.97`` is an actual output ceiling.
LIMITER = "alimiter=limit=0.97:level=false"

CACHE_ROOT = Path.home() / ".cache" / "ffrender"
FFMPEG_TIMEOUT = int(os.environ.get("APP_FFRENDER_TIMEOUT_SEC") or 21600)
FFPROBE_TIMEOUT = int(os.environ.get("APP_FFPROBE_TIMEOUT_SEC") or 30)


# ── 小物 ───────────────────────────────────────────────────────────────

def _run_ff(cmd: list, label: str) -> float:
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT)
    dt = time.time() - t0
    if r.returncode != 0:
        raise RuntimeError(f"[{label}] ffmpeg 失敗 (rc={r.returncode})\n"
                           f"cmd: {' '.join(str(c) for c in cmd[:12])} ...\n"
                           f"{(r.stderr or '')[-2000:]}")
    print(f"  ✓ {label}: {dt:.1f}s")
    return dt


def probe_duration(path) -> float:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, timeout=FFPROBE_TIMEOUT)
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def _title_from(name: str) -> str:
    noext = re.sub(r"\.[^.]+$", "", name)
    noext = re.sub(r"^z+_", "", noext)
    return re.sub(r"^\s*(?:track\s*)?\d{1,3}\s*[-_. )]+\s*", "", noext, flags=re.I).strip() or noext


def _finite_float(value, default: float = 0.0, *, minimum: Optional[float] = None,
                  maximum: Optional[float] = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    if not math.isfinite(number):
        number = float(default)
    if minimum is not None:
        number = max(minimum, number)
    if maximum is not None:
        number = min(maximum, number)
    return number


def _extract_num(folder: Path) -> str:
    m = re.match(r"^(\d+)_", folder.name)
    return m.group(1) if m else "00"


def _load_channel_ffrender(vol_folder: Path) -> dict:
    """チャンネル設定の `ffrender` ブロックを返す。無ければ空。
    静止画チャンネル固有のSE/画面効果/曲順ポリシーはここでチャンネル別に持つ。"""
    try:
        p = Path(vol_folder).parent / ".app_channel_config.json"
        if not p.exists():
            return {}
        cc = json.loads(p.read_text(encoding="utf-8"))
        cfg = cc.get("ffrender") if isinstance(cc, dict) else None
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        print(f"  ⚠ channel ffrender 設定読み込み失敗（既定を使用）: {e}")
        return {}


def _load_channel_visualizer(vol_folder: Path) -> dict:
    """Return the channel-level ``visualizer`` block.

    It intentionally lives beside ``ffrender`` in .app_channel_config.json so
    settings_catalog's ``channel.visualizer.*`` keys and the renderer share the
    same storage contract.
    """
    try:
        p = Path(vol_folder).parent / ".app_channel_config.json"
        if not p.exists():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        cfg = data.get("visualizer") if isinstance(data, dict) else None
        return cfg if isinstance(cfg, dict) else {}
    except Exception as e:
        print(f"  ⚠ channel visualizer 設定読み込み失敗（既定を使用）: {e}")
        return {}


def _resolve_channel_asset(vol_folder: Path, value: str, *, num: str = "") -> Optional[Path]:
    """チャンネルフォルダ基準の素材パスを解決。`vol{num}.jpg` 等の簡易置換も対応。"""
    if not value:
        return None
    value = str(value)
    if num:
        value = value.replace("{num}", num)
    p = Path(value).expanduser()
    if p.is_absolute():
        return p
    vol_folder = Path(vol_folder)
    channel_folder = vol_folder.parent
    for base in (vol_folder, channel_folder):
        cand = base / value
        if cand.exists():
            return cand
    return channel_folder / value


# ── 1. 曲順（JSX 準拠: z 優先・z 数降順、normal はシャッフル） ────────────

def order_songs(music_dir: Path, *, seed: Optional[int] = None) -> list:
    """music/*.mp3 を JSX(listMp3s) と同じ規則で並べる。
    normal 群はシャッフル（JSX は Math.random）。順序は呼び出し側で order.json に固定する。"""
    files = [f for f in music_dir.glob("*.mp3")]
    zf = [f for f in files if re.match(r"^z+_", f.name)]
    nf = [f for f in files if not re.match(r"^z+_", f.name)]
    zf.sort(key=lambda p: -len(re.match(r"^(z+)_", p.name).group(1)))
    rng = random.Random(seed)
    rng.shuffle(nf)
    return zf + nf


def _set_hash(music_dir: Path) -> str:
    """music セットの同一性ハッシュ（ファイル名 + サイズ）。新規 SUNO で変わる。"""
    items = sorted((f.name, f.stat().st_size) for f in music_dir.glob("*.mp3"))
    h = hashlib.sha1(json.dumps(items, ensure_ascii=False).encode("utf-8")).hexdigest()
    return h[:16]


def resolve_order(music_dir: Path, cache_dir: Path) -> list:
    """order.json があり set が一致すれば再利用（再レンダで曲順を固定）。無ければ新規生成して保存。"""
    set_h = _set_hash(music_dir)
    order_json = cache_dir / "order.json"
    by_name = {f.name: f for f in music_dir.glob("*.mp3")}
    if order_json.exists():
        try:
            d = json.loads(order_json.read_text(encoding="utf-8"))
            if d.get("set_hash") == set_h and all(n in by_name for n in d.get("order", [])):
                ordered = [by_name[n] for n in d["order"] if n in by_name]
                if len(ordered) == len(by_name):
                    print(f"  曲順: order.json 再利用 ({len(ordered)}曲)")
                    return ordered
        except Exception:
            pass
    ordered = order_songs(music_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    order_json.write_text(json.dumps(
        {"set_hash": set_h, "order": [f.name for f in ordered]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  曲順: 新規生成して保存 ({len(ordered)}曲)")
    return ordered


# ── レンダーマニフェスト（構成の正本。著作権差し替え/レガシーブリッジの土台） ──
# vol フォルダに保存し、曲順・使用画像・尺・音声設定を「正本」として固定する。
# - 通常レンダ: 無ければ新規生成して保存。あれば再利用（曲順固定 → 再レンダ高速）。
# - 著作権修正: order を差し替え/削除して再レンダ（Premiere 不要）。
# - レガシーブリッジ: Premiere の配置済みタイムラインを読んで manifest 化。

MANIFEST_NAME = ".ffrender_manifest.json"
MANIFEST_VERSION = 1


def _manifest_path(vol_folder: Path) -> Path:
    return Path(vol_folder) / MANIFEST_NAME


def load_manifest(vol_folder: Path) -> Optional[dict]:
    p = _manifest_path(vol_folder)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"  ⚠ manifest 読み込み失敗（無視して再生成）: {e}")
    return None


def save_manifest(vol_folder: Path, data: dict) -> None:
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _manifest_path(vol_folder).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _new_manifest(num: str, order_names: list, main: Optional[Path], subs: list,
                  target: float, *, bitrate: str, fade: float, source: str,
                  clips: Optional[list] = None, burn: Optional[dict] = None) -> dict:
    return {
        "version": MANIFEST_VERSION,
        "num": num,
        "source": source,                # "fresh" | "premiere" | "edited"
        "target": int(round(target)),
        "fps": FPS,
        "resolution": [WIDTH, HEIGHT],
        "audio": {"bitrate": bitrate, "fade_sec": fade,
                  "order": list(order_names)},
        "images": {"main": main.name if main else "",
                   "subs": [s.name for s in subs]},
        "burn_titles": burn,             # None=焼き込み無し / {mode,font,...}=曲名常時焼き込み
        "clips": clips or [],            # 参照用スナップショット（再計算可）
    }


def derive_order_from_clips(clips: list, music_dir: Path) -> list:
    """Premiere タイムラインのクリップ列（start 昇順）から 1 ループ分の曲順(Path)を導く。
    clips: [{start,end,title,path}]。path(getMediaPath) を最優先、無ければ title で music/ を探す。"""
    by_name = {f.name: f for f in music_dir.glob("*.mp3")}
    by_title = {}
    for f in music_dir.glob("*.mp3"):
        by_title.setdefault(_title_from(f.name), f)
    order, seen = [], set()
    for c in sorted(clips, key=lambda x: x["start"]):
        key = c.get("title") or c.get("path") or ""
        if key in seen:                  # 2 周目に入った → 1 ループ完了
            break
        seen.add(key)
        f = None
        mp = c.get("path") or ""
        if mp and Path(mp).name in by_name:
            f = by_name[Path(mp).name]
        elif c.get("title") in by_title:
            f = by_title[c["title"]]
        if f:
            order.append(f)
        else:
            print(f"  ⚠ timeline の曲 '{c.get('title')}' を music/ で解決できず（スキップ）")
    return order


_PREMIERE_READ_JSX = r'''
(function(){
  var p=app.project; if(!p) return "ERR|no_project";
  var seqs=p.sequences; var n=seqs?seqs.numSequences:0;
  if(!n) return "ERR|no_sequence";
  var best=null,bestEnd=-1;
  for(var i=0;i<n;i++){var s=seqs[i];var e=0;try{e=s.end.seconds;}catch(_){}
    try{for(var t=0;t<s.audioTracks.numTracks;t++){var tr=s.audioTracks[t];if(tr&&tr.clips)
      for(var c=0;c<tr.clips.numItems;c++){var cl=tr.clips[c];if(cl&&cl.end&&cl.end.seconds>e)e=cl.end.seconds;}}}catch(_){}
    if(e>bestEnd){bestEnd=e;best=s;}}
  if(!best) return "ERR|no_best";
  var out=["SEQ|"+best.name+"|"+bestEnd.toFixed(3)];
  function dump(tracks,kind){for(var t=0;t<tracks.numTracks;t++){var tr=tracks[t];if(!tr||!tr.clips)continue;
    for(var c=0;c<tr.clips.numItems;c++){var cl=tr.clips[c];var nm="",mp="";
      try{nm=cl.projectItem?cl.projectItem.name:"";}catch(_){}
      try{mp=(cl.projectItem&&cl.projectItem.getMediaPath)?cl.projectItem.getMediaPath():"";}catch(_){}
      out.push(kind+"|"+cl.start.seconds.toFixed(3)+"|"+cl.end.seconds.toFixed(3)+"|"+nm+"|"+mp);}}}
  dump(best.audioTracks,"A"); dump(best.videoTracks,"V");
  return out.join("\n");
})()
'''


def build_manifest_from_premiere(vol_folder: Path, *, target_override: Optional[float] = None) -> dict:
    """【レガシーブリッジ】開いている Premiere の配置済みタイムラインを Premiere Link 経由で
    読み、manifest 化して vol フォルダに保存する。Premiere 起動 + Premiere Link パネル必須。"""
    import app_premiere as ap
    vol_folder = Path(vol_folder)
    num = _extract_num(vol_folder)
    music_dir = vol_folder / "music"
    print("  Premiere Link でタイムラインを読み取り中...")
    res = ap._file_eval_script(_PREMIERE_READ_JSX, timeout=60)
    if res.startswith("ERR|"):
        raise RuntimeError(f"Premiere タイムライン読取失敗: {res} "
                           "（プロジェクトを開き、配置を済ませてから実行）")
    aclips, vclips, seq_end, seq_name = [], [], 0.0, ""
    for line in res.split("\n"):
        parts = line.split("|")
        if parts[0] == "SEQ":
            seq_name = parts[1]
            seq_end = float(parts[2])
        elif parts[0] in ("A", "V") and len(parts) >= 5:
            rec = {"start": float(parts[1]), "end": float(parts[2]),
                   "title": _title_from(parts[3]), "path": parts[4]}
            (aclips if parts[0] == "A" else vclips).append(rec)
    if not aclips:
        raise RuntimeError("音声クリップが読めません（配置が未実行の可能性）")
    order = derive_order_from_clips(aclips, music_dir)
    if not order:
        raise RuntimeError("曲順を music/ に解決できませんでした")
    # 画像: V トラックの distinct なメディアを start 順に main/subs へ
    seen, imgs = set(), []
    for c in sorted(vclips, key=lambda x: x["start"]):
        nm = Path(c["path"]).name if c.get("path") else c.get("title")
        if nm and nm not in seen and (vol_folder / nm).exists():
            seen.add(nm)
            imgs.append(vol_folder / nm)
    main = imgs[0] if imgs else select_images(vol_folder, num)[0]
    subs = imgs[1:]
    target = target_override or seq_end
    print(f"  読取: seq='{seq_name}' / 音声{len(aclips)}クリップ→{len(order)}曲(1ループ) / "
          f"画像{len(imgs)}枚 / 尺={target:.0f}s")
    data = _new_manifest(num, [f.name for f in order], main, subs, target,
                         bitrate=DEFAULT_AUDIO_BITRATE, fade=FADE_SEC,
                         source="premiere", clips=aclips[:50])
    save_manifest(vol_folder, data)
    print(f"  manifest 保存: {_manifest_path(vol_folder).name}")
    return data


def edit_manifest_order(vol_folder: Path, swaps: list, removes: list) -> dict:
    """manifest の曲順を編集（著作権差し替え/削除）。swaps=[(対象, 新ファイル名)] / removes=[対象]。
    対象はファイル名 or 曲名で照合。"""
    m = load_manifest(vol_folder)
    if not m:
        raise RuntimeError("manifest がありません。先に通常レンダか --from-premiere で作成してください")
    order = m["audio"]["order"]
    music_dir = Path(vol_folder) / "music"

    def _idx(target: str) -> Optional[int]:
        for i, n in enumerate(order):
            if n == target or _title_from(n) == _title_from(target):
                return i
        return None

    for old, newf in swaps:
        nf = Path(newf).name
        if not (music_dir / nf).exists():
            raise RuntimeError(f"差し替え先が music/ にありません: {nf}")
        i = _idx(old)
        if i is None:
            raise RuntimeError(f"差し替え対象が manifest に見つかりません: {old}")
        print(f"  swap: {order[i]} → {nf}")
        order[i] = nf
    for rm in removes:
        i = _idx(rm)
        if i is None:
            raise RuntimeError(f"削除対象が manifest に見つかりません: {rm}")
        print(f"  remove: {order.pop(i)}")
    m["audio"]["order"] = order
    m["source"] = "edited"
    save_manifest(vol_folder, m)
    return m


def resolve_composition(vol_folder: Path, *, target_override: Optional[float],
                        cache_dir: Path, bitrate: str):
    """構成(曲順/画像/尺/音声設定/焼き込み)を解決。manifest 優先 → 無ければ新規生成して保存。
    返り値: (order:list[Path], main, subs, target:int, bitrate, fade, burn, audio_specs)
    audio_specs は timeline 由来の場合のみ、除外・欠落解決後の order と同じ順番で返す。"""
    vol_folder = Path(vol_folder)
    music_dir = vol_folder / "music"
    num = _extract_num(vol_folder)
    by_name = {f.name: f for f in music_dir.glob("*.mp3")}
    timeline_path = vol_folder / TIMELINE_NAME
    if timeline_path.exists():
        timeline = load_vol_timeline(vol_folder)
        excluded = set(timeline.get("excluded") or [])
        order, audio_specs, missing = [], [], []
        for clip in timeline.get("audio_clips", []):
            if clip.get("id") in excluded: continue
            raw = Path(str(clip.get("path") or "")); p = raw if raw.is_absolute() else vol_folder / raw
            if not p.is_file() and clip.get("filename") in by_name:
                p = by_name[clip["filename"]]
            if p.is_file():
                spec = dict(clip)
                spec["gain_db"] = _finite_float(spec.get("gain_db"), 0.0, minimum=-60.0, maximum=12.0)
                # Keep the first authored transition value.  It is suppressed only
                # for the very first placement, then reused at the loop boundary.
                spec["crossfade_sec"] = _finite_float(
                    spec.get("crossfade_sec", timeline.get("audio_crossfade_sec", 0)),
                    0.0, minimum=0.0)
                spec["crossfade_curve"] = str(spec.get("crossfade_curve") or
                                                timeline.get("audio_crossfade_curve") or "equal_power")
                order.append(p)
                audio_specs.append(spec)
            else:
                missing.append(clip.get("filename") or clip.get("path"))
        if missing: print(f"  ⚠ vol_timeline.json の曲が見つかりません: {missing}")
        visuals = timeline.get("visual_segments") or []
        imgs = [(Path(str(x.get("image_path") or "")) if Path(str(x.get("image_path") or "")).is_absolute() else vol_folder / str(x.get("image_path") or "")) for x in visuals]
        imgs = [p for p in imgs if p.is_file()]
        main, subs = (imgs[0], imgs[1:]) if imgs else select_images(vol_folder, num)
        target = int(target_override or timeline.get("total_duration") or timeline.get("target_duration") or 10800)
        print(f"  構成: vol_timeline.json 使用 ({len(order)}曲, {len(visuals)}映像区間, target={target}s)")
        return order, main, subs, target, bitrate, FADE_SEC, None, audio_specs
    m = load_manifest(vol_folder)
    if m:
        order, missing = [], []
        for n in m["audio"]["order"]:
            (order.append(by_name[n]) if n in by_name else missing.append(n))
        if missing:
            print(f"  ⚠ manifest の曲が music/ に不在: {missing}")
        main = (vol_folder / m["images"]["main"]) if m["images"].get("main") else None
        if main and not main.exists():
            main = None
        subs = [vol_folder / s for s in m["images"].get("subs", []) if (vol_folder / s).exists()]
        if not main and not subs:
            main, subs = select_images(vol_folder, num)
        target = int(target_override or m.get("target") or 10800)
        bitrate = m["audio"].get("bitrate", bitrate)
        fade = float(m["audio"].get("fade_sec", FADE_SEC))
        burn = m.get("burn_titles")
        print(f"  構成: manifest 使用 (source={m.get('source')}, {len(order)}曲, target={target}s, "
              f"焼込={burn['mode'] if burn else '無'})")
        return order, main, subs, target, bitrate, fade, burn, []
    # 新規: cache の order.json を継承（あれば）→ 無ければ新規 shuffle
    order = resolve_order(music_dir, cache_dir)
    main, subs = select_images(vol_folder, num)
    target = int(target_override or 10800)
    data = _new_manifest(num, [f.name for f in order], main, subs, target,
                         bitrate=bitrate, fade=FADE_SEC, source="fresh")
    save_manifest(vol_folder, data)
    print(f"  構成: 新規生成→manifest 保存 ({len(order)}曲, target={target}s)")
    return order, main, subs, target, bitrate, FADE_SEC, None, []


# ── 2. 配置計算（JSX 準拠: target まで loop、末尾 clip を target でトリム） ─

def compute_placement(songs: list, target: float, audio_specs: Optional[list] = None) -> list:
    """clips = [{start,end,title,path,gain_db,crossfade_*}]。

    timeline metadata は曲ループと同じ周期で対応させ、クロスフェード後の
    絶対尺が target になるよう最後のクリップをトリムする。"""
    if not songs or target <= 0:
        return []
    audio_specs = audio_specs or []
    durs = {s: probe_duration(s) for s in songs}
    clips, cursor, i = [], 0.0, 0
    guard = 0
    while cursor < target and guard < 100000:
        guard += 1
        s = songs[i % len(songs)]
        d = durs[s]
        if d <= 0:
            i += 1
            continue
        spec = audio_specs[i % len(audio_specs)] if audio_specs else {}
        gain_db = _finite_float(spec.get("gain_db"), 0.0, minimum=-60.0, maximum=12.0)
        crossfade = (0.0 if not clips else
                     _finite_float(spec.get("crossfade_sec"), 0.0, minimum=0.0))
        # Leave headroom on both inputs.  Besides being safer for acrossfade, this
        # guarantees that every loop iteration advances the cursor even when a
        # malformed/API-authored fade is as long as (or longer than) the clip.
        crossfade = min(crossfade, d * 0.9, cursor * 0.9)
        start = max(0.0, cursor - crossfade)
        clips.append({"start": start, "end": start + d,
                      "title": _title_from(s.name), "path": s,
                      "gain_db": gain_db, "crossfade_sec": crossfade,
                      "crossfade_curve": str(spec.get("crossfade_curve") or "equal_power")})
        cursor = start + d
        i += 1
    if clips and clips[-1]["end"] > target:
        clips[-1]["end"] = target
    return clips


# ── 3. 画像選択（JSX 準拠: selected_images.json → vol{N}/vol{N}-1 fallback） ─

def _find_image(folder: Path, base: str) -> Optional[Path]:
    for ext in (".jpg", ".png"):
        p = folder / f"{base}{ext}"
        if p.exists():
            return p
    return None


def select_images(folder: Path, num: str) -> tuple:
    """(main, subs[]) を返す。selected_images.json 優先、無ければ vol{N}(メイン)/vol{N}-1(サブ)。"""
    sel = folder / "selected_images.json"
    if sel.exists():
        try:
            d = json.loads(sel.read_text(encoding="utf-8"))
            main = folder / d["main"] if d.get("main") and (folder / d["main"]).exists() else None
            subs = [folder / s for s in (d.get("sub") or []) if (folder / s).exists()]
            if main or subs:
                return main, subs
        except Exception:
            pass
    main = _find_image(folder, f"vol{num}")
    sub = _find_image(folder, f"vol{num}-1")
    return main, ([sub] if sub else [])


def compute_image_segments(main: Optional[Path], subs: list, total: float) -> list:
    """JSX の 3 分割（0-5 main / 5-30 sub||main / 30-End sub群N等分）を (img,start,end) で返す。"""
    pt1, pt2 = 5.0, 30.0
    segs = []
    if not main and not subs:
        raise RuntimeError("画像が見つかりません（selected_images.json / vol{N} どちらも無し）")
    sub0 = subs[0] if subs else None
    # 1) 0-5 main
    if total > 0:
        segs.append([main or sub0, 0.0, min(total, pt1)])
    # 2) 5-30 sub||main
    if total > pt1:
        segs.append([sub0 or main, pt1, min(total, pt2)])
    # 3) 30-End: subs を N 等分、無ければ sub||main 1 本
    if total > pt2:
        tail = subs if subs else [sub0 or main]
        n = len(tail)
        if n <= 1:
            segs.append([tail[0], pt2, total])
        else:
            per = (total - pt2) / n
            for qi in range(n):
                s = pt2 + per * qi
                e = total if qi == n - 1 else pt2 + per * (qi + 1)
                segs.append([tail[qi], s, e])
    # 同一画像で隣接する区間はマージ（無駄な再エンコード/連結を減らす）
    merged = []
    for img, s, e in segs:
        if merged and merged[-1][0] == img:
            merged[-1][2] = e
        else:
            merged.append([img, s, e])
    return merged


def resolve_timeline_image_segments(items: list, vol_folder: Path, total: float, *,
                                    fallback_image: Optional[Path] = None,
                                    offset: float = 0.0,
                                    global_fade: float = 0.0) -> list:
    """Resolve saved V1 rows into contiguous ``[path,start,end,fade]`` blocks.

    The editor model can lag behind a newly selected render target (or contain a
    missing image).  Holding the nearest usable still across uncovered ranges is
    preferable to silently shortening the final video at mux time.  ``offset`` is
    used when an audio intro exists but no separate intro video was built.
    """
    total = _finite_float(total, 0.0, minimum=0.0)
    offset = _finite_float(offset, 0.0, minimum=0.0)
    global_fade = _finite_float(global_fade, 0.0, minimum=0.0)
    if total <= 0:
        return []

    fallback = Path(fallback_image) if fallback_image else None
    if fallback and not fallback.is_file():
        fallback = None
    resolved = []
    for item in items or []:
        raw = Path(str(item.get("image_path") or ""))
        image = raw if raw.is_absolute() else Path(vol_folder) / raw
        if not image.is_file():
            continue
        start = offset + _finite_float(item.get("start"), 0.0, minimum=0.0)
        end = offset + _finite_float(item.get("end"), 0.0, minimum=0.0)
        start = min(total, start)
        end = min(total, end)
        if end <= start:
            continue
        fade = _finite_float(item.get("crossfade_sec"), global_fade, minimum=0.0)
        resolved.append([image, start, end, fade])
    resolved.sort(key=lambda row: (row[1], row[2]))

    if not resolved:
        return [[fallback, 0.0, total, 0.0]] if fallback else []

    output = []
    cursor = 0.0
    held_image = fallback or resolved[0][0]
    for image, start, end, fade in resolved:
        if start > cursor:
            output.append([held_image or image, cursor, start, 0.0])
            cursor = start
        # V1 is a single lane.  If malformed rows overlap, keep the already
        # established interval and trim the later row to the next free point.
        start = max(start, cursor)
        if end <= start:
            continue
        output.append([image, start, end, fade])
        cursor = end
        held_image = image
        if cursor >= total:
            break
    if not output:
        return [[held_image, 0.0, total, 0.0]]
    if output[-1][2] < total:
        output[-1][2] = total
    return output


# ── 4. 音声ビルド（concat → トリム → fade → limiter → AAC、キャッシュ付き） ─

def build_audio(clips: list, target: float, out: Path, *,
                bitrate: str = DEFAULT_AUDIO_BITRATE, cache_dir: Path,
                use_cache: bool = True, fade: float = FADE_SEC,
                intro_audio: Optional[Path] = None) -> Path:
    names = [c["path"].name for c in clips]
    intro_sig = None
    if intro_audio:
        intro_audio = Path(intro_audio)
        intro_sig = [str(intro_audio), intro_audio.stat().st_size, intro_audio.stat().st_mtime_ns]
    key = hashlib.sha1(json.dumps(
        {"o": names, "intro": intro_sig, "t": round(target, 2), "f": fade, "b": bitrate, "lim": LIMITER},
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    cached = cache_dir / f"audio_{key}.m4a"
    if use_cache and cached.exists() and probe_duration(cached) >= target - 5:
        print(f"  音声: キャッシュ再利用 {cached.name} ({cached.stat().st_size/1024/1024:.0f}MB)")
        shutil.copy2(cached, out)
        return out

    cache_dir.mkdir(parents=True, exist_ok=True)
    list_txt = cache_dir / f"concat_{key}.txt"
    with open(list_txt, "w", encoding="utf-8") as f:
        if intro_audio:
            f.write("file '%s'\n" % str(intro_audio).replace("'", r"'\''"))
        for c in clips:
            f.write("file '%s'\n" % str(c["path"]).replace("'", r"'\''"))
    fade = max(0.0, min(float(fade), max(0.0, target - 0.05)))
    af = f"afade=t=out:st={max(0.0, target - fade)}:d={fade},{LIMITER}" if fade else LIMITER
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(list_txt),
             "-t", str(target), "-af", af,
             "-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", bitrate,
             str(out)], "音声ビルド(concat→fade→limiter→AAC)")
    if use_cache:
        try:
            shutil.copy2(out, cached)
        except Exception:
            pass
    return out


def build_audio_mp3_copy(clips: list, target: float, out: Path, *,
                         cache_dir: Path, use_cache: bool = True,
                         intro_audio: Optional[Path] = None) -> Path:
    """処理済みMP3を再エンコードせず連結する高速モード。
    前段 app_process_tracks.py / 素材側で loudnorm・フェード・リミッター済みのチャンネル向け。"""
    names = [c["path"].name for c in clips]
    intro_sig = None
    if intro_audio:
        intro_audio = Path(intro_audio)
        intro_sig = [str(intro_audio), intro_audio.stat().st_size, intro_audio.stat().st_mtime_ns]
    key = hashlib.sha1(json.dumps(
        {"mode": "mp3_copy", "o": names, "intro": intro_sig, "t": round(target, 2)},
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    cached = cache_dir / f"audio_{key}.mp3"
    if use_cache and cached.exists() and probe_duration(cached) >= target - 5:
        print(f"  音声: MP3 copy キャッシュ再利用 {cached.name} ({cached.stat().st_size/1024/1024:.0f}MB)")
        shutil.copy2(cached, out)
        return out

    cache_dir.mkdir(parents=True, exist_ok=True)
    list_txt = cache_dir / f"concat_mp3_{key}.txt"
    with open(list_txt, "w", encoding="utf-8") as f:
        if intro_audio:
            f.write("file '%s'\n" % str(intro_audio).replace("'", r"'\''"))
        for c in clips:
            f.write("file '%s'\n" % str(c["path"]).replace("'", r"'\''"))
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(list_txt),
             "-t", str(target), "-c:a", "copy", str(out)],
            "音声ビルド(MP3 concat copy)")
    if use_cache:
        try:
            shutil.copy2(out, cached)
        except Exception:
            pass
    return out


def build_audio_crossfades(clips: list, transitions: list, target: float, out: Path, *,
                           bitrate: str, fade: float = FADE_SEC,
                           intro_audio: Optional[Path] = None) -> Path:
    """Per-placement gain/crossfade graph, followed by the legacy tail fade/limiter.

    This path intentionally bypasses the concat cache: authored gain values are
    part of every input and mp3 stream-copy cannot represent them.  `transitions`
    remains as a compatibility fallback; current callers attach metadata to clips.
    """
    if not clips:
        raise RuntimeError("音声クリップが空です")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    input_offset = 0
    if intro_audio:
        cmd += ["-i", str(intro_audio)]
        input_offset = 1
    for clip in clips:
        cmd += ["-i", str(clip["path"])]

    normalize_audio = "aresample=48000,aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo"
    filters = []
    if intro_audio:
        filters.append(f"[0:a]asetpts=PTS-STARTPTS,{normalize_audio}[introa]")
    durations = []
    for i, clip in enumerate(clips):
        transition = transitions[i % len(transitions)] if transitions else {}
        duration = max(0.001, float(clip["end"]) - float(clip["start"]))
        durations.append(duration)
        gain_db = _finite_float(clip.get("gain_db", transition.get("gain_db")), 0.0,
                                minimum=-60.0, maximum=12.0)
        source_index = i + input_offset
        filters.append(
            f"[{source_index}:a]atrim=duration={duration:.6f},asetpts=PTS-STARTPTS,"
            f"{normalize_audio},volume={gain_db:.3f}dB[ag{i}]"
        )

    current = "ag0"
    chain_duration = durations[0]
    for i in range(1, len(clips)):
        clip = clips[i]
        transition = transitions[i % len(transitions)] if transitions else {}
        requested = _finite_float(clip.get("crossfade_sec", transition.get("crossfade_sec")),
                                  0.0, minimum=0.0)
        crossfade = min(requested, max(0.0, chain_duration - 0.001),
                        max(0.0, durations[i] - 0.001))
        outlabel = f"ac{i}"
        if crossfade <= 0:
            filters.append(f"[{current}][ag{i}]concat=n=2:v=0:a=1[{outlabel}]")
            chain_duration += durations[i]
        else:
            curve = str(clip.get("crossfade_curve", transition.get("crossfade_curve")) or "equal_power")
            c1, c2 = (("tri", "tri") if curve == "linear" else ("qsin", "qsin"))
            filters.append(f"[{current}][ag{i}]acrossfade=d={crossfade:.3f}:c1={c1}:c2={c2}[{outlabel}]")
            chain_duration += durations[i] - crossfade
        current = outlabel
    if intro_audio:
        filters.append(f"[introa][{current}]concat=n=2:v=0:a=1[withintro]")
        current = "withintro"
    fade = max(0.0, min(_finite_float(fade, FADE_SEC, minimum=0.0), max(0.0, target - 0.05)))
    tail = f"afade=t=out:st={max(0.0, target - fade):.3f}:d={fade:.3f},{LIMITER}" if fade else LIMITER
    filters.append(f"[{current}]{tail}[audioout]")
    cmd += ["-filter_complex", ";".join(filters), "-map", "[audioout]",
            "-t", f"{target:.3f}", "-ar", "48000", "-ac", "2",
            "-c:a", "aac", "-b:a", bitrate, str(out)]
    _run_ff(cmd, "音声ビルド(個別ゲイン/クロスフェード)")
    return out


# ── 5. ループ素材エンコード（画像[＋題字] ごとに 1 GOP）＋ 6. 連結 ──────

BURN_FONT_DEFAULT = "/Library/Fonts/Arial Unicode.ttf"  # ♪ + 日本語も出る universal フォント


def _default_burn(mode: str = "always") -> dict:
    """曲名焼き込みの既定スタイル（channel 設定/manifest で上書き可）。"""
    return {"mode": mode, "font": BURN_FONT_DEFAULT, "fontsize": 52,
            "x": "72", "y": "h-th-80", "color": "white", "border": 4, "prefix": "♪ "}


def _load_channel_burn(vol_folder: Path) -> dict:
    """チャンネル設定（vol フォルダの親 .app_channel_config.json）の `burn_titles` ブロックを返す。
    フォント/サイズ/色/位置/接頭辞などをここで定義 → _default_burn に上書きマージする想定。無ければ {}。"""
    try:
        p = Path(vol_folder).parent / ".app_channel_config.json"
        if not p.exists():
            return {}
        cc = json.loads(p.read_text(encoding="utf-8"))
        b = cc.get("burn_titles") if isinstance(cc, dict) else None
        return {k: v for k, v in b.items() if k != "mode"} if isinstance(b, dict) else {}
    except Exception as e:
        print(f"  ⚠ channel burn 設定読み込み失敗（既定を使用）: {e}")
        return {}


def _drawtext_clause(title: str, burn: dict, scratch: Path) -> str:
    """drawtext 句を組み立て。題字は textfile 渡し（コロン/記号/日本語のエスケープ回避）。"""
    body = (burn.get("prefix", "") + title)
    tf = scratch / f"title_{hashlib.sha1(body.encode('utf-8')).hexdigest()[:10]}.txt"
    tf.write_text(body, encoding="utf-8")
    font = burn.get("font", BURN_FONT_DEFAULT)
    return (f"drawtext=fontfile='{font}':textfile='{tf}':fontsize={burn.get('fontsize',52)}:"
            f"fontcolor={burn.get('color','white')}:borderw={burn.get('border',4)}:"
            f"bordercolor=black@0.85:shadowcolor=black@0.55:shadowx=2:shadowy=2:"
            f"x={burn.get('x','72')}:y={burn.get('y','h-th-80')}")


def _img_gop(img: Path, crf: int, scratch: Path, cache: dict,
             title: Optional[str] = None, burn: Optional[dict] = None,
             loop_frames: int = GOP_FRAMES) -> Path:
    """画像（＋常時表示の題字）を 1 closed GOP にエンコードしてキャッシュ。"""
    loop_frames = max(1, int(loop_frames or GOP_FRAMES))
    key = (str(img), img.stat().st_mtime_ns, crf, title or "", loop_frames)
    if key in cache:
        return cache[key]
    out = scratch / f"loop_{hashlib.sha1(str(key).encode()).hexdigest()[:12]}.mp4"
    # JPEG はフルレンジ(yuvj/pc)で decode → out_range=tv で limited 化（AME tv/bt709 と一致）
    vf = (f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase:out_range=tv,"
          f"crop={WIDTH}:{HEIGHT},setsar=1")
    if title and burn:
        vf += "," + _drawtext_clause(title, burn, scratch)
    vf += ",format=yuv420p"
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-loop", "1", "-i", str(img), "-r", FPS, "-frames:v", str(loop_frames),
             "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-color_range", "tv",
             "-x264-params", f"keyint={loop_frames}:min-keyint={loop_frames}:scenecut=0:bframes=0",
             "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
             str(out)], f"ループ素材 enc ({img.name}{' +題字' if title else ''})")
    cache[key] = out
    return out


def _seg_at(t: float, segs: list):
    for img, s, e in segs:
        if s <= t < e:
            return img
    return segs[-1][0]


def _title_at(t: float, clips: list) -> str:
    for c in clips:
        if c["start"] <= t < c["end"]:
            return c["title"]
    return ""


def _build_blocks(blocks: list, out: Path, *, crf: int, scratch: Path,
                  burn: Optional[dict], loop_frames: int = GOP_FRAMES) -> Path:
    """blocks=[[img, title|None, dur]] を GOP→stream_loop→（複数なら TS ロスレス）連結。"""
    gop_cache: dict = {}
    if len(blocks) == 1:
        img, title, dur = blocks[0]
        gop = _img_gop(img, crf, scratch, gop_cache, title, burn, loop_frames=loop_frames)
        frames = max(1, round(float(dur) * 30000 / 1001))
        _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-stream_loop", "-1", "-i", str(gop), "-frames:v", str(frames),
                 "-c", "copy", "-video_track_timescale", "30000", str(out)],
                "連結(単一ブロック) stream_loop")
        return out
    ts_files = []
    for idx, (img, title, dur) in enumerate(blocks):
        gop = _img_gop(img, crf, scratch, gop_cache, title, burn, loop_frames=loop_frames)
        ts = scratch / f"seg_{idx:03d}.ts"
        frames = max(1, round(float(dur) * 30000 / 1001))
        _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-stream_loop", "-1", "-i", str(gop), "-frames:v", str(frames),
                 "-c", "copy", "-bsf:v", "h264_mp4toannexb", "-f", "mpegts", str(ts)],
                f"ブロック→TS[{idx}] {dur:.0f}s" + (f" «{title[:18]}»" if title else ""))
        ts_files.append(ts)
    concat_txt = scratch / "video_concat.txt"
    concat_txt.write_text("".join(f"file '{p}'\n" for p in ts_files), encoding="utf-8")
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(concat_txt),
             "-c", "copy", str(out)], "TS ロスレス連結 → mp4")
    return out


def build_video(segments: list, out: Path, *, crf: int, scratch: Path,
                clips: Optional[list] = None, burn: Optional[dict] = None,
                loop_frames: int = GOP_FRAMES) -> Path:
    """画像区間 segments=[(img,s,e)] を連結して out を作る。
    burn 指定時は曲 clips の境界でも分割し、各ブロックに曲名を**常時焼き込む**。
    最後のブロックを +2s 余らせ、mux 時に -shortest で音声尺へロック（末尾無音/フレーム不足回避）。

    単一ブロック: stream_loop -c copy 一発（3h 実測で継ぎ目破綻ゼロ）。
    複数ブロック: mp4 直結は境界で DTS 非単調になるため MPEG-TS 中間でロスレス連結。"""
    total = max(e for _, _, e in segments)
    if not burn or not clips:
        blocks = [[img, None, (e - s)] for img, s, e in segments]
    else:
        # 画像境界 ∪ 曲境界 で細分し、各ブロック (画像, 曲名) を確定
        bset = {0.0, total}
        for _, s, e in segments:
            bset.update((s, e))
        for c in clips:
            bset.update((c["start"], min(c["end"], total)))
        bounds = sorted(b for b in bset if 0.0 <= b <= total)
        blocks = []
        for i in range(len(bounds) - 1):
            b0, b1 = bounds[i], bounds[i + 1]
            if b1 - b0 < 0.05:
                continue
            blocks.append([_seg_at(b0 + 1e-3, segments), _title_at(b0 + 1e-3, clips), (b1 - b0)])
    if not blocks:
        raise RuntimeError("映像ブロックが空")
    blocks[-1][2] += 2.0
    return _build_blocks(blocks, out, crf=crf, scratch=scratch, burn=burn, loop_frames=loop_frames)


def build_video_with_crossfades(segments: list, out: Path, *, crf: int) -> Path:
    """Timeline image segments with per-boundary xfade. Used only by saved timelines."""
    if len(segments) < 2 or not any(float(x[3] or 0) > 0 for x in segments[1:]):
        return build_video([(x[0], x[1], x[2]) for x in segments], out, crf=crf,
                           scratch=out.parent, loop_frames=GOP_FRAMES)
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    durations=[]
    for idx,(img,s,e,fade_in) in enumerate(segments):
        # xfade overlaps its inputs; extend each incoming segment by that overlap so
        # the authored absolute end time remains unchanged.
        d=max(.1,float(e)-float(s) + (float(fade_in or 0) if idx else 0)); durations.append(d)
        cmd += ["-loop","1","-t",f"{d:.3f}","-i",str(img)]
    filters=[]
    for i in range(len(segments)):
        filters.append(f"[{i}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p,settb=AVTB[v{i}]")
    current="v0"; elapsed=durations[0]
    for i in range(1,len(segments)):
        requested=max(0.0,float(segments[i][3] or 0)); outlabel=f"x{i}"
        if requested <= 0:
            filters.append(f"[{current}][v{i}]concat=n=2:v=1:a=0[{outlabel}]")
            elapsed += durations[i]
        else:
            fade=max(0.01,min(requested,durations[i-1]-.01,durations[i]-.01))
            offset=max(0,elapsed-fade)
            filters.append(f"[{current}][v{i}]xfade=transition=fade:duration={fade:.3f}:offset={offset:.3f}[{outlabel}]")
            elapsed += durations[i]-fade
        current=outlabel
    cmd += ["-filter_complex",";".join(filters),"-map",f"[{current}]","-r",FPS,"-c:v","libx264","-preset","medium","-crf",str(crf),"-pix_fmt","yuv420p",str(out)]
    _run_ff(cmd,"タイムライン映像クロスフェード")
    return out


def _image_vf(prefix: str) -> str:
    return (f"[{prefix}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase:out_range=tv,"
            f"crop={WIDTH}:{HEIGHT},setsar=1,format=yuv420p[{prefix}v]")


def build_intro_dissolve(from_img: Path, to_img: Path, duration: float, out: Path, *,
                         crf: int, scratch: Path, transition: str = "fade") -> Optional[Path]:
    """冒頭SE用の短いディゾルブ映像を生成する。
    `duration` 全体を使って from_img → to_img に溶けるため、SEと視覚効果が同期する。"""
    if duration <= 0.05 or not from_img or not to_img:
        return None
    duration = max(0.1, duration)
    vf = (_image_vf("0") + ";" + _image_vf("1") + ";"
          f"[0v][1v]xfade=transition={transition}:duration={duration:.3f}:offset=0,"
          "format=yuv420p[v]")
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-loop", "1", "-t", f"{duration:.3f}", "-i", str(from_img),
             "-loop", "1", "-t", f"{duration:.3f}", "-i", str(to_img),
             "-filter_complex", vf, "-map", "[v]", "-r", FPS, "-t", f"{duration:.3f}",
             "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-color_range", "tv",
             "-x264-params", f"keyint={GOP_FRAMES}:min-keyint={GOP_FRAMES}:scenecut=0",
             "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
             str(out)], f"冒頭ディゾルブ ({from_img.name} → {to_img.name}, {duration:.2f}s)")
    return out


def concat_videos_lossless(parts: list, out: Path, *, scratch: Path) -> Path:
    """同スペックの mp4 群を MPEG-TS 経由でロスレス連結する。"""
    ts_files = []
    for idx, p in enumerate(parts):
        ts = scratch / f"preconcat_{idx:02d}.ts"
        _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(p), "-c", "copy", "-bsf:v", "h264_mp4toannexb",
                 "-f", "mpegts", str(ts)], f"動画→TS[{idx}] {Path(p).name}")
        ts_files.append(ts)
    concat_txt = scratch / "video_preconcat.txt"
    concat_txt.write_text("".join(f"file '{p}'\n" for p in ts_files), encoding="utf-8")
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(concat_txt),
             "-c", "copy", "-video_track_timescale", "30000", str(out)],
            "冒頭映像 + 本編映像 連結")
    return out


def _shift_clips(clips: list, offset: float) -> list:
    shifted = []
    for c in clips:
        d = dict(c)
        d["start"] = c["start"] + offset
        d["end"] = c["end"] + offset
        shifted.append(d)
    return shifted


def _shift_timed_tracks(tracks: list, offset: float, item_key: str) -> list:
    """Return a copy of V2+/T1+ tracks shifted behind an external intro."""
    offset = _finite_float(offset, 0.0, minimum=0.0)
    if not offset:
        return tracks or []
    shifted_tracks = []
    for track in tracks or []:
        shifted_track = dict(track)
        shifted_items = []
        for item in track.get(item_key) or []:
            shifted_item = dict(item)
            shifted_item["start"] = _finite_float(item.get("start"), 0.0) + offset
            if item.get("end") is not None:
                shifted_item["end"] = _finite_float(item.get("end"), 0.0) + offset
            shifted_items.append(shifted_item)
        shifted_track[item_key] = shifted_items
        shifted_tracks.append(shifted_track)
    return shifted_tracks


def generate_display_timecode(clips: list, output_path: Path, cfg: dict, *, total_songs: Optional[int] = None) -> None:
    """説明欄向けの見せ方だけを調整したタイムコードを生成する。
    例: 冒頭SE後の1曲目を出さず、2曲目を 00:00:00 として表示する。"""
    def to_hhmmss(sec: float) -> str:
        sec = max(0, round(sec))
        h = sec // 3600
        m = (sec % 3600) // 60
        s = sec % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    skip_initial = max(0, int(cfg.get("skip_initial_clips") or 0))
    zero_at = cfg.get("zero_at_clip_index")
    if zero_at is None:
        zero_at = skip_initial
    zero_at = max(0, int(zero_at))
    include_loop = bool(cfg.get("include_loop_markers", True))
    include_repeats = bool(cfg.get("include_repeated_tracks", True))
    if total_songs is None:
        seen = []
        for c in clips:
            if c["title"] in seen:
                break
            seen.append(c["title"])
        total_songs = len(seen)

    origin = clips[zero_at]["start"] if clips and zero_at < len(clips) else 0.0
    lines = []
    for i, clip in enumerate(clips):
        if i < skip_initial:
            continue
        if not include_repeats and total_songs and i >= total_songs:
            break
        if include_loop and total_songs and i > 0 and i % total_songs == 0:
            lines.append(f"{to_hhmmss(clip['start'] - origin)} - LOOP")
        lines.append(f"{to_hhmmss(clip['start'] - origin)} - {clip['title']}")

    Path(output_path).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f" タイムコード生成(表示調整): {output_path} ({len(lines)} 行, origin=clip#{zero_at})")


# ── 7. mux ──────────────────────────────────────────────────────────────

def mux(video: Path, audio: Optional[Path], srt: Path, out: Path) -> Path:
    if audio is None:
        _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-i", str(video), "-i", str(srt),
                 "-map", "0:v:0", "-map", "1:0", "-c:v", "copy",
                 "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
                 "-movflags", "+faststart", str(out)], "mux(v+mov_text / A1消音)")
        return out
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(video), "-i", str(audio), "-i", str(srt),
             "-map", "0:v:0", "-map", "1:a:0", "-map", "2:0",
             "-c:v", "copy", "-c:a", "copy",
             "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
             "-movflags", "+faststart", "-shortest", str(out)], "mux(v+a+mov_text)")
    return out


def build_hidden_video(duration: float, out: Path, *, crf: int) -> Path:
    """V1非表示時の黒背景。映像ストリーム自体は mux に必要なため残す。"""
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "lavfi", "-i", f"color=c=black:s={WIDTH}x{HEIGHT}:r={FPS}",
             "-t", f"{max(0.1, duration):.3f}", "-an", "-c:v", "libx264",
             "-preset", "medium", "-crf", str(crf), "-pix_fmt", "yuv420p", str(out)],
            "V1非表示（黒背景）")
    return out


def resolve_visualizer_config(channel_cfg: dict, timeline_cfg: Optional[dict]) -> dict:
    """Resolve safe renderer values; a vol timeline shallow-overrides channel defaults."""
    cfg = {
        "enabled": False, "pattern": "bars", "position": "bottom-center",
        "margin": 64, "width_percent": 60, "height_px": 180,
        "color1": "#ffffff", "color2": "#66ccff", "opacity": 0.7,
    }
    if isinstance(channel_cfg, dict):
        cfg.update(channel_cfg)
    if isinstance(timeline_cfg, dict):
        cfg.update(timeline_cfg)
    cfg["pattern"] = str(cfg.get("pattern") or "bars").lower()
    if cfg["pattern"] not in {"bars", "mirror", "wave", "circle"}:
        cfg["pattern"] = "bars"
    cfg["position"] = str(cfg.get("position") or "bottom-center").lower()
    cfg["margin"] = int(_finite_float(cfg.get("margin"), 64, minimum=0, maximum=500))
    cfg["width_percent"] = _finite_float(cfg.get("width_percent"), 60, minimum=5, maximum=100)
    cfg["height_px"] = int(_finite_float(cfg.get("height_px"), 180, minimum=24, maximum=1080))
    cfg["opacity"] = _finite_float(cfg.get("opacity"), .7, minimum=0, maximum=1)
    # Renderer geometry is authoritative.  Keep authored settings where possible,
    # but never let an overlay escape the fixed 1920x1080 output frame.
    original = (cfg["width_percent"], cfg["height_px"], cfg["margin"])
    width = min(WIDTH, max(16, int(round(WIDTH * cfg["width_percent"] / 100 / 2) * 2)))
    height = min(HEIGHT, max(16, int(round(cfg["height_px"] / 2) * 2)))
    cfg["width_percent"] = width / WIDTH * 100
    cfg["height_px"] = height
    position = cfg["position"]
    horizontal_edge = position.endswith(("-left", "-right"))
    vertical_edge = position.startswith(("top-", "bottom-"))
    max_margin_x = max(0, WIDTH - width) if horizontal_edge else cfg["margin"]
    max_margin_y = max(0, HEIGHT - height) if vertical_edge else cfg["margin"]
    cfg["margin"] = min(cfg["margin"], max_margin_x, max_margin_y)
    clamped = (cfg["width_percent"], cfg["height_px"], cfg["margin"])
    # 偶数丸め由来の±1px差は警告対象にしない（実クランプ時のみ通知）
    if any(abs(a - b) > (WIDTH / 100 * .11 if i == 0 else 1.01)
           for i, (a, b) in enumerate(zip(original, clamped))):
        print("WARNING: ビジュアライザー配置を1920x1080フレーム内にクランプしました "
              f"(width={width}, height={height}, margin={cfg['margin']})")
    return cfg


def _visualizer_xy(position: str, margin: int) -> tuple[str, str]:
    parts = position.split("-")
    vert = parts[0] if len(parts) > 1 else "middle"
    horiz = parts[-1] if len(parts) > 1 else "center"
    xs = {"left": str(margin), "center": "(W-w)/2", "right": f"W-w-{margin}"}
    ys = {"top": str(margin), "middle": "(H-h)/2", "center": "(H-h)/2",
          "bottom": f"H-h-{margin}"}
    # The expression clamp is a final safety net for direct callers which did
    # not pass through resolve_visualizer_config().
    x = xs.get(horiz, xs["center"])
    y = ys.get(vert, ys["bottom"])
    return f"max(0\\,min(W-w\\,{x}))", f"max(0\\,min(H-h\\,{y}))"


def apply_audio_visualizer(video: Path, audio: Optional[Path], cfg: dict, out: Path,
                           *, crf: int) -> Path:
    """Render an audio-reactive transparent visualizer over the complete video."""
    if not cfg.get("enabled") or audio is None:
        return video
    width = max(16, int(round(WIDTH * float(cfg["width_percent"]) / 100 / 2) * 2))
    height = max(16, int(round(int(cfg["height_px"]) / 2) * 2))
    pattern = cfg["pattern"]
    color1 = str(cfg.get("color1") or "#ffffff").replace("#", "0x")
    color2 = (color1 if str(cfg.get("color_mode") or "single") == "single" else
              str(cfg.get("color2") or color1).replace("#", "0x"))
    opacity = float(cfg["opacity"])
    x, y = _visualizer_xy(cfg["position"], int(cfg["margin"]))
    duration = probe_duration(video)
    normalized = "aformat=channel_layouts=stereo,dynaudnorm=f=250:g=15:p=0.95:m=30"
    # Transparent grid lines at the edge of each of 48 cells create stable
    # inter-bar gaps after nearest-neighbour expansion.  No Lanczos blur is used.
    gap = max(2, int(round(width / 400)))
    # プレビュー（Canvas描画）の滑らかさに合わせるための措置
    if pattern == "wave":
        source = (f"[1:a]{normalized},showwaves=s={width}x{height}:mode=p2p:"
                  f"rate={FPS}:colors={color1}|{color2},dilation,gblur=sigma=0.6,format=rgba,"
                  f"colorchannelmixer=aa={opacity}[viz]")
    elif pattern == "circle":
        side = min(width, height)
        source = (f"[1:a]{normalized},avectorscope=s={side}x{side}:r={FPS}:"
                  f"mode=lissajous:draw=line:scale=sqrt:rc=0:gc=200:bc=255,format=rgba,"
                  f"colorchannelmixer=aa={opacity},scale={width}:{height}:force_original_aspect_ratio=decrease[viz]")
    elif pattern == "mirror":
        half = max(8, height // 2)
        half_gap = max(1, int(round(width / 400)))
        source = (f"[1:a]{normalized},showfreqs=s=48x{half}:mode=bar:ascale=log:fscale=log:"
                  f"win_size=2048:averaging=2:colors={color1}|{color2},"
                  f"scale={width}:{half}:flags=neighbor,format=rgba,colorkey=black:0.08:0,"
                  f"drawgrid=w=iw/48:h=ih:t={half_gap}:c=black@0:replace=1,"
                  f"colorchannelmixer=aa={opacity}[top];"
                  f"[top]split[a][b];[b]vflip[c];[a][c]vstack[viz]")
    else:
        source = (f"[1:a]{normalized},showfreqs=s=48x{height}:mode=bar:ascale=log:fscale=log:"
                  f"win_size=2048:averaging=2:colors={color1}|{color2},"
                  f"scale={width}:{height}:flags=neighbor,format=rgba,colorkey=black:0.08:0,"
                  f"drawgrid=w=iw/48:h=ih:t={gap}:c=black@0:replace=1,"
                  f"colorchannelmixer=aa={opacity}[viz]")
    graph = f"{source};[0:v][viz]overlay=x={x}:y={y}:shortest=1[v]"
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(video), "-i", str(audio), "-filter_complex", graph,
             "-map", "[v]", "-t", f"{duration:.3f}", "-an", "-r", FPS,
             "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-video_track_timescale", "30000", str(out)],
            f"オーディオビジュアライザー焼き込み ({pattern})")
    return out


def _now_playing_font(cfg: dict) -> Path:
    explicit = Path(str(cfg.get("font_path") or "")).expanduser()
    if explicit.is_file():
        return explicit
    requested = re.sub(r"[^\w]+", "", str(cfg.get("font_name") or "").casefold())
    candidates = []
    for root in (Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home()/"Library/Fonts"):
        if root.is_dir():
            candidates.extend(p for p in root.rglob("*") if p.suffix.lower() in {".ttf", ".otf", ".ttc"})
    if requested:
        exact = next((p for p in candidates if re.sub(r"[^\w]+", "", p.stem.casefold()) == requested), None)
        partial = next((p for p in candidates if requested in re.sub(r"[^\w]+", "", p.stem.casefold())), None)
        if exact or partial:
            return exact or partial
    hiragino = next((p for p in sorted(candidates) if "hiragino" in p.name.lower()), None)
    if hiragino:
        return hiragino
    return Path(BURN_FONT_DEFAULT)


def apply_now_playing(video: Path, clips: list, cfg: dict, out: Path, *, crf: int,
                      scratch: Path, chunk_safe: bool = False) -> Path:
    """Burn per-song titles in one drawtext pass. Disabled configs preserve legacy output."""
    if not cfg.get("enabled") or not clips:
        return video
    total = max(0.0, probe_duration(video))
    margin=int(cfg.get("margin",64)); pos=str(cfg.get("position") or "bottom-center")
    xs={"left":str(margin),"center":"(w-text_w)/2","right":f"w-text_w-{margin}"}; ys={"top":str(margin),"middle":"(h-text_h)/2","center":"(h-text_h)/2","bottom":f"h-text_h-{margin}"}
    pv=pos.split("-"); vert=pv[0] if len(pv)>1 else "middle"; horiz=pv[-1] if len(pv)>1 else "center"
    font=_now_playing_font(cfg); clauses=[]; mode=str(cfg.get("mode") or "intro")
    intro=max(.1,float(cfg.get("intro_seconds") or 8)); fi=max(0,float(cfg.get("fade_in") or 0)); fo=max(0,float(cfg.get("fade_out") or 0)); opacity=max(0,min(1,float(cfg.get("opacity",1))))
    for i,c in enumerate(clips):
        start=max(0.0, min(total, float(c["start"])))
        end=max(0.0, min(total, float(c["end"] if mode=="always" else min(c["end"],float(c["start"])+intro))))
        if end <= start: continue
        title=str(c.get("title") or Path(str(c.get("path") or "")).stem)
        tf=scratch/f"now_playing_{i:03d}.txt"; tf.write_text(title,encoding="utf-8")
        alpha=f"{opacity}"
        if fi or fo:
            alpha=(f"{opacity}*if(lt(t,{start+fi:.3f}),(t-{start:.3f})/{max(fi,.001):.3f},"
                   f"if(gt(t,{end-fo:.3f}),({end:.3f}-t)/{max(fo,.001):.3f},1))")
        color=str(cfg.get("color") or "#ffffff").replace("#","0x")
        border=str(cfg.get("border_color") or "#000000").replace("#","0x")
        raw_x=xs.get(horiz,xs['center']); raw_y=ys.get(vert,ys['bottom'])
        clauses.append(f"drawtext=fontfile='{font}':textfile='{tf}':fontsize={int(cfg.get('size',48))}:fontcolor={color}:alpha='{alpha}':borderw={int(cfg.get('border_width',2))}:bordercolor={border}:x=max(0\\,min(w-text_w\\,{raw_x})):y=max(0\\,min(h-text_h\\,{raw_y})):enable='between(t,{start:.3f},{end:.3f})'")
    if not clauses: return video
    cmd = ["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(video),"-vf",",".join(clauses),"-an","-c:v","libx264","-preset","medium","-crf",str(crf),"-pix_fmt","yuv420p"]
    if chunk_safe:
        cmd += ["-bf","0","-g",str(GOP_FRAMES),"-sc_threshold","0","-video_track_timescale","30000"]
    _run_ff(cmd + [str(out)],"Now Playing 曲名焼き込み")
    return out


def apply_video_tracks(video: Path, tracks: list, vol_folder: Path, out: Path, *, crf: int) -> Path:
    """Overlay V2+ segments over V1. Higher numbered tracks are composited last."""
    items = []
    for track in (tracks or [])[1:]:
        for seg in track.get("segments") or []:
            raw = Path(str(seg.get("image_path") or "")); path = raw if raw.is_absolute() else vol_folder / raw
            if path.is_file() and float(seg.get("end") or 0) > float(seg.get("start") or 0):
                items.append((path, float(seg.get("start") or 0), float(seg.get("end") or 0)))
    if not items: return video
    cmd = ["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(video)]
    for path, _, _ in items: cmd += ["-loop","1","-i",str(path)]
    filters=[]; current="0:v"
    for i, (_, start, end) in enumerate(items, 1):
        filters.append(f"[{i}:v]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,format=rgba[v{i}]")
        nxt=f"ov{i}"; filters.append(f"[{current}][v{i}]overlay=(W-w)/2:(H-h)/2:enable='between(t,{start:.3f},{end:.3f})'[{nxt}]"); current=nxt
    cmd += ["-filter_complex",";".join(filters),"-map",f"[{current}]","-t",f"{probe_duration(video):.3f}","-an","-c:v","libx264","-preset","medium","-crf",str(crf),"-pix_fmt","yuv420p",str(out)]
    _run_ff(cmd,"複数映像トラック合成")
    return out


def apply_text_tracks(video: Path, tracks: list, out: Path, *, crf: int,
                      scratch: Path, chunk_safe: bool = False) -> Path:
    """Burn free-text clips from T1+ during their authored time ranges."""
    clauses=[]
    total = max(0.0, probe_duration(video))
    skipped_unedited = 0
    for ti, track in enumerate(tracks or []):
        for ci, clip in enumerate(track.get("clips") or []):
            if clip.get("kind") != "free_text":
                continue
            raw_text = str(clip.get("text") or "")
            text = raw_text.strip()
            if not text or text == "テキストを入力":
                skipped_unedited += 1
                continue
            start=max(0.0, min(total, float(clip.get("start") or 0)))
            end=max(0.0, min(total, float(clip.get("end") or 0)))
            if end <= start: continue
            tf=scratch/f"free_text_{ti:02d}_{ci:03d}.txt"; tf.write_text(raw_text,encoding="utf-8")
            pos=str(clip.get("position") or "center").split("-"); vert=pos[0] if len(pos)>1 else "center"; horiz=pos[-1] if len(pos)>1 else "center"
            margin = int(_finite_float(clip.get("margin"), 64.0, minimum=0.0))
            xs={"left":str(margin),"center":"(w-text_w)/2","right":f"w-text_w-{margin}"}; ys={"top":str(margin),"center":"(h-text_h)/2","middle":"(h-text_h)/2","bottom":f"h-text_h-{margin}"}
            font=_now_playing_font(clip); color=str(clip.get("color") or "#ffffff").replace("#","0x"); border=str(clip.get("border_color") or "#000000").replace("#","0x")
            font_size=max(1,int(_finite_float(clip.get("size"),48.0,minimum=1.0)))
            border_width=int(_finite_float(clip.get("border_width"),2.0,minimum=0.0))
            opacity=_finite_float(clip.get("opacity"),1.0,minimum=0.0,maximum=1.0)
            fade_in=_finite_float(clip.get("fade_in"),0.0,minimum=0.0); fade_out=_finite_float(clip.get("fade_out"),0.0,minimum=0.0)
            duration=end-start
            if str(clip.get("effect") or "none") == "typewriter":
                speed = _typewriter_speed(clip, len(raw_text), start, end)
                steps = _typewriter_steps(raw_text, start, end, speed)
                fixed_x, fixed_y = _fixed_text_origin(raw_text, font, font_size, horiz, vert, margin)
                fade_in = 0.0
                if fade_out > duration:
                    fade_out = duration
                for step_index, (prefix_len, step_start, step_end) in enumerate(steps):
                    prefix_file = scratch / f"free_text_{ti:02d}_{ci:03d}_type_{step_index:03d}.txt"
                    prefix_file.write_text(raw_text[:prefix_len], encoding="utf-8")
                    alpha = f"{opacity}"
                    if fade_out:
                        alpha = (f"{opacity}*if(gt(t,{end-fade_out:.3f}),"
                                 f"({end:.3f}-t)/{max(fade_out,.001):.3f},1)")
                    clauses.append(f"drawtext=fontfile='{font}':textfile='{prefix_file}':fontsize={font_size}:fontcolor={color}:alpha='{alpha}':borderw={border_width}:bordercolor={border}:x={fixed_x}:y={fixed_y}:enable='between(t,{step_start:.3f},{step_end:.3f})'")
                continue
            if fade_in+fade_out>duration and fade_in+fade_out>0:
                scale=duration/(fade_in+fade_out); fade_in*=scale; fade_out*=scale
            alpha=f"{opacity}"
            if fade_in or fade_out:
                alpha=(f"{opacity}*if(lt(t,{start+fade_in:.3f}),(t-{start:.3f})/{max(fade_in,.001):.3f},"
                       f"if(gt(t,{end-fade_out:.3f}),({end:.3f}-t)/{max(fade_out,.001):.3f},1))")
            raw_x=xs.get(horiz,xs['center']); raw_y=ys.get(vert,ys['center'])
            clauses.append(f"drawtext=fontfile='{font}':textfile='{tf}':fontsize={font_size}:fontcolor={color}:alpha='{alpha}':borderw={border_width}:bordercolor={border}:x=max(0\\,min(w-text_w\\,{raw_x})):y=max(0\\,min(h-text_h\\,{raw_y})):enable='between(t,{start:.3f},{end:.3f})'")
    if skipped_unedited:
        print(f"⚠ 未編集のテキストクリップ {skipped_unedited} 件をスキップ")
    if not clauses: return video
    cmd = ["ffmpeg","-y","-hide_banner","-loglevel","error","-i",str(video),"-vf",",".join(clauses),"-an","-c:v","libx264","-preset","medium","-crf",str(crf),"-pix_fmt","yuv420p"]
    if chunk_safe:
        cmd += ["-bf","0","-g",str(GOP_FRAMES),"-sc_threshold","0","-video_track_timescale","30000"]
    _run_ff(cmd + [str(out)],"自由テキスト焼き込み")
    return out


def _typewriter_speed(clip: dict, text_length: int, start: float, end: float) -> float:
    speed = _finite_float(clip.get("effect_speed"), 12.0, minimum=1.0, maximum=60.0)
    available = max(0.001, end - start - 0.3)
    required = text_length / available if text_length else speed
    if required > speed:
        print(f"  ⚠ タイプライター速度を {speed:.2f} → {required:.2f} 文字/秒に自動調整")
        speed = required
    return speed


def _typewriter_steps(text: str, start: float, end: float, speed: float) -> list:
    """Return at most 150 prefix windows; the first character appears at start+1/speed."""
    length = len(text)
    if not length:
        return []
    count = min(150, length)
    prefix_lengths = [max(1, math.ceil(length * i / count)) for i in range(1, count + 1)]
    result = []
    for index, prefix_len in enumerate(prefix_lengths):
        step_start = min(end, start + prefix_len / speed)
        next_len = prefix_lengths[index + 1] if index + 1 < len(prefix_lengths) else None
        step_end = min(end, start + next_len / speed) if next_len else end
        if step_end > step_start:
            result.append((prefix_len, step_start, step_end))
    return result


def _fixed_text_origin(text: str, font: Path, font_size: int, horiz: str,
                       vert: str, margin: int) -> tuple[str, str]:
    try:
        from PIL import Image, ImageDraw, ImageFont
        pil_font = ImageFont.truetype(str(font), font_size)
        box = ImageDraw.Draw(Image.new("L", (1, 1))).multiline_textbbox(
            (0, 0), text, font=pil_font, spacing=0)
        width, height = max(1, box[2] - box[0]), max(1, box[3] - box[1])
        x = margin if horiz == "left" else (WIDTH - width) / 2 if horiz == "center" else WIDTH - width - margin
        y = margin if vert == "top" else (HEIGHT - height) / 2 if vert in {"center", "middle"} else HEIGHT - height - margin
        return f"{max(0, min(WIDTH-width, x)):.1f}", f"{max(0, min(HEIGHT-height, y)):.1f}"
    except Exception as exc:
        print(f"  ⚠ Pillowで全文サイズを取得できないため左寄せにフォールバック: {exc}")
        return str(margin), str(margin if vert == "top" else max(margin, (HEIGHT-font_size)//2))


def build_typewriter_sound(tracks: list, target: float, out: Path) -> Optional[Path]:
    events = []
    for track in tracks or []:
        for clip in track.get("clips") or []:
            text = str(clip.get("text") or "")
            if (clip.get("kind") != "free_text" or str(clip.get("effect") or "none") != "typewriter"
                    or clip.get("type_sound") is not True or not text):
                continue
            start = max(0.0, _finite_float(clip.get("start"), 0.0))
            end = min(target, _finite_float(clip.get("end"), 0.0))
            speed = _typewriter_speed(clip, len(text), start, end)
            volume = _finite_float(clip.get("type_sound_volume"), 0.5, minimum=0.0, maximum=1.0)
            last = -1.0
            for index, char in enumerate(text, 1):
                when = start + index / speed
                if char.isspace() or when >= end or when - last < 0.05:
                    continue
                events.append((when, volume, index))
                last = when
    if not events:
        return None
    # A hard global 20-hit/s ceiling also covers overlapping text clips.
    limited_events = []
    for event in sorted(events):
        if not limited_events or event[0] - limited_events[-1][0] >= 0.05:
            limited_events.append(event)
    events = limited_events
    import numpy as np
    sample_rate = 48000
    audio = np.zeros(max(1, int(math.ceil(target * sample_rate))), dtype=np.float32)
    rng = np.random.default_rng(0xA710)
    for when, volume, seed in events:
        duration = float(rng.uniform(0.015, 0.025)); size = int(duration * sample_rate)
        t = np.arange(size, dtype=np.float32) / sample_rate
        pitch = float(rng.uniform(2000.0, 4000.0)); level = volume * float(rng.uniform(0.9, 1.1))
        noise = rng.standard_normal(size).astype(np.float32)
        click = (0.58 * np.sin(2*np.pi*pitch*t) + 0.42 * noise) * np.exp(-t * 210.0) * level
        offset = int(when * sample_rate); room = min(size, len(audio) - offset)
        if room > 0: audio[offset:offset+room] += click[:room]
    peak = float(np.max(np.abs(audio)))
    if peak > 0.98: audio *= 0.98 / peak
    pcm = (audio * 32767.0).astype("<i2")
    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(sample_rate); wav.writeframes(pcm.tobytes())
    print(f"  タイプ音: {len(events)}打を合成")
    return out


def mix_typewriter_sound(audio: Optional[Path], sound: Path, target: float,
                         out: Path, *, bitrate: str) -> Path:
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if audio is None:
        cmd += ["-i", str(sound), "-t", str(target), "-af", LIMITER]
    else:
        cmd += ["-i", str(audio), "-i", str(sound), "-filter_complex",
                f"[0:a][1:a]amix=inputs=2:duration=longest:normalize=0,{LIMITER}[a]", "-map", "[a]", "-t", str(target)]
    cmd += ["-ar", "48000", "-ac", "2", "-c:a", "aac", "-b:a", bitrate, str(out)]
    _run_ff(cmd, "タイプ音ミックス(AAC)")
    return out


def _timeline_change_ranges(segments: list, clips: list, now_cfg: dict,
                            text_tracks: list, total: float) -> list:
    """Return merged ranges where pixels vary from the plain V1 still.

    Segment fades, now-playing bands and authored text are deliberately kept
    small; the gaps between them can therefore use the GOP stamp/copy path.
    """
    ranges = []
    for idx, seg in enumerate(segments):
        if idx:
            fade = _finite_float(seg[3] if len(seg) > 3 else 0, 0, minimum=0)
            boundary = _finite_float(seg[1], 0, minimum=0)
            if fade:
                ranges.append((max(0.0, boundary - fade), min(total, boundary)))
    if now_cfg.get("enabled"):
        mode = str(now_cfg.get("mode") or "intro")
        intro = max(.1, _finite_float(now_cfg.get("intro_seconds"), 8, minimum=.1))
        for clip in clips or []:
            start = max(0.0, float(clip["start"]))
            end = min(total, float(clip["end"]) if mode == "always" else start + intro)
            if end > start:
                ranges.append((start, end))
    for track in text_tracks or []:
        for clip in track.get("clips") or []:
            if clip.get("kind") != "free_text" or not str(clip.get("text") or "").strip():
                continue
            start = max(0.0, _finite_float(clip.get("start"), 0))
            end = min(total, _finite_float(clip.get("end"), 0))
            if end > start:
                ranges.append((start, end))
    merged = []
    for start, end in sorted(ranges):
        if merged and start <= merged[-1][1] + .001:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _slice_timed_tracks(tracks: list, start: float, end: float) -> list:
    sliced = []
    for track in tracks or []:
        row = dict(track); row["clips"] = []
        for clip in track.get("clips") or []:
            cs, ce = float(clip.get("start") or 0), float(clip.get("end") or 0)
            if ce <= start or cs >= end:
                continue
            item = dict(clip)
            item["start"] = max(cs, start) - start
            item["end"] = min(ce, end) - start
            row["clips"].append(item)
        sliced.append(row)
    return sliced


def _slice_song_clips(clips: list, start: float, end: float) -> list:
    result = []
    for clip in clips or []:
        cs, ce = float(clip["start"]), float(clip["end"])
        if ce <= start or cs >= end:
            continue
        item = dict(clip)
        # Preserve the original song origin relative to this chunk. Clamping
        # would restart an intro-only title at every later chunk of the song.
        item["start"] = cs - start
        item["end"] = ce - start
        result.append(item)
    return result


def _render_v1_slice(segments: list, start: float, end: float, out: Path, *,
                     crf: int, scratch: Path, loop_frames: int,
                     gop_cache: Optional[dict] = None) -> Path:
    """Render one V1 slice; only a slice intersecting a dissolve is encoded."""
    duration = max(.04, end - start)
    incoming = None
    for idx, seg in enumerate(segments):
        if not idx:
            continue
        boundary = float(seg[1]); fade = _finite_float(seg[3] if len(seg) > 3 else 0, 0, minimum=0)
        if fade and start < boundary and end > boundary - fade:
            incoming = (segments[idx - 1][0], seg[0], boundary, fade)
            break
    if incoming:
        before, after, boundary, fade = incoming
        progress = max(0.0, min(1.0, (start - (boundary - fade)) / fade))
        rate = 1.0 / fade
        vf = (_image_vf("0") + ";" + _image_vf("1") + ";"
              f"[0v][1v]blend=all_expr='A*(1-min(1,max(0,{progress:.9f}+T*{rate:.9f})))+"
              f"B*min(1,max(0,{progress:.9f}+T*{rate:.9f}))',format=yuv420p[v]")
        _run_ff(["ffmpeg","-y","-hide_banner","-loglevel","error",
                 "-loop","1","-t",f"{duration:.3f}","-i",str(before),
                 "-loop","1","-t",f"{duration:.3f}","-i",str(after),
                 "-filter_complex",vf,"-map","[v]","-r",FPS,"-t",f"{duration:.3f}",
                 "-an","-c:v","libx264","-preset","medium","-crf",str(crf),
                 "-pix_fmt","yuv420p","-bf","0","-g",str(GOP_FRAMES),
                 "-sc_threshold","0","-video_track_timescale","30000",str(out)],
                f"変化区間 enc ディゾルブ {start:.2f}-{end:.2f}s")
        return out
    image = _seg_at(start + .001, [(x[0], x[1], x[2]) for x in segments])
    # Reuse the same closed-GOP stamp as the legacy fast path. Stream-copy can
    # stop on packet boundaries inside the GOP; keeping predicted frames avoids
    # turning every repeated frame into a very large I-frame.
    cache = gop_cache if gop_cache is not None else {}
    gop = _img_gop(image, crf, scratch, cache, loop_frames=loop_frames)
    frames = max(1, round(duration * 30000 / 1001))
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-stream_loop", "-1", "-i", str(gop), "-frames:v", str(frames),
             "-an", "-c:v", "copy", "-video_track_timescale", "30000", str(out)],
            f"静止区間 stamp copy {start:.2f}-{end:.2f}s")
    return out


def build_timeline_video_hybrid(segments: list, clips: list, now_cfg: dict,
                                text_tracks: list, out: Path, *, crf: int,
                                scratch: Path, loop_frames: int = GOP_FRAMES) -> Path:
    """Stamp/copy static gaps and encode only timeline pixel-change ranges."""
    total = max(float(x[2]) for x in segments)
    changes = _timeline_change_ranges(segments, clips, now_cfg, text_tracks, total)
    bounds = {0.0, total}
    for start, end in changes:
        bounds.update((start, end))
    for seg in segments:
        bounds.update((max(0.0, float(seg[1])), min(total, float(seg[2]))))
    frame_rate = 30000 / 1001
    # Snap every absolute boundary to the shared frame grid. Using differences
    # between rounded absolute frame numbers prevents per-chunk rounding drift.
    bounds = sorted({round(x * frame_rate) / frame_rate for x in bounds if 0 <= x <= total})
    parts = []
    gop_cache = {}
    for idx, (start, end) in enumerate(zip(bounds, bounds[1:])):
        if end - start < .02:
            continue
        midpoint = (start + end) / 2
        dynamic = any(cs - .001 <= midpoint < ce + .001 for cs, ce in changes)
        base = scratch / f"hybrid_base_{idx:03d}.mp4"
        _render_v1_slice(segments, start, end, base, crf=crf, scratch=scratch,
                         loop_frames=loop_frames, gop_cache=gop_cache)
        part = base
        if dynamic:
            local_clips = _slice_song_clips(clips, start, end)
            part = apply_now_playing(part, local_clips, now_cfg,
                                     scratch / f"hybrid_title_{idx:03d}.mp4",
                                     crf=crf, scratch=scratch, chunk_safe=True)
            # Authored text is the top-most layer. Keep this order aligned with
            # the full-encode path: V* -> visualizer -> Now Playing -> T*.
            local_text = _slice_timed_tracks(text_tracks, start, end)
            part = apply_text_tracks(part, local_text, scratch / f"hybrid_text_{idx:03d}.mp4",
                                     crf=crf, scratch=scratch, chunk_safe=True)
        parts.append(part)
    if not parts:
        raise RuntimeError("ハイブリッド映像チャンクが空")
    print(f"  ハイブリッド: {len(parts)} chunks / 変化区間 {sum(e-s for s,e in changes):.2f}s / 全体 {total:.2f}s")
    concat_txt = scratch / "hybrid_concat.txt"
    concat_txt.write_text("".join(f"file '{Path(p)}'\n" for p in parts), encoding="utf-8")
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", str(concat_txt),
             "-map", "0:v:0", "-an", "-c:v", "copy",
             "-video_track_timescale", "30000", str(out)],
            "ハイブリッドチャンク MP4 ロスレス連結")
    return out


# ── メインオーケストレーション ─────────────────────────────────────────

def render(vol_folder: Path, *, target: Optional[float] = None, output_path: Optional[Path] = None,
           scratch: Optional[Path] = None, crf: int = DEFAULT_CRF,
           audio_bitrate: str = DEFAULT_AUDIO_BITRATE, use_audio_cache: bool = True,
           burn_mode: Optional[str] = None) -> Optional[Path]:
    """target=None なら manifest 尺 → 既定 10800 の順で解決（--duration 明示時のみ上書き）。
    burn_mode: None=manifest 継承 / "none"=焼き込み無し / "always"=曲名常時焼き込み。"""
    vol_folder = Path(vol_folder)
    num = _extract_num(vol_folder)
    music_dir = vol_folder / "music"
    if not music_dir.exists() or not any(music_dir.glob("*.mp3")):
        print(f"❌ music/ に mp3 がありません: {music_dir}")
        return None

    cache_dir = CACHE_ROOT / re.sub(r"[^A-Za-z0-9_.-]+", "_", vol_folder.name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if scratch is None:
        scratch = Path("/tmp/ffrender") / re.sub(r"[^A-Za-z0-9_.-]+", "_", vol_folder.name)
    scratch.mkdir(parents=True, exist_ok=True)

    t_all = time.time()
    print("=" * 60)
    print(f"  ffrender: {vol_folder.name}")
    print(f"  scratch={scratch}  cache={cache_dir}")
    print("=" * 60)

    # 0) 構成解決（manifest 優先 → 無ければ新規生成して manifest 保存）
    songs, main_img, subs, target, audio_bitrate, fade, burn, audio_specs = resolve_composition(
        vol_folder, target_override=target, cache_dir=cache_dir, bitrate=audio_bitrate)
    if not songs:
        print("❌ 曲順が空（music/ 解決に失敗）")
        return None
    ffr_cfg = _load_channel_ffrender(vol_folder)
    if ffr_cfg.get("video_crf") is not None:
        try:
            crf = int(ffr_cfg.get("video_crf"))
        except Exception:
            print(f"  ⚠ video_crf が不正です（既定を使用）: {ffr_cfg.get('video_crf')}")
    if ffr_cfg.get("audio_bitrate"):
        audio_bitrate = str(ffr_cfg.get("audio_bitrate"))
    loop_seconds = float(ffr_cfg.get("loop_seconds") or (GOP_FRAMES * 1001 / 30000))
    loop_frames = max(1, int(round(loop_seconds * 30000 / 1001)))
    print(f"  映像設定: crf={crf} / loop={loop_seconds:.2f}s ({loop_frames} frames)")
    intro_audio = None
    intro_sec = 0.0
    intro_cfg = ffr_cfg.get("intro_visual") if isinstance(ffr_cfg.get("intro_visual"), dict) else {}
    if ffr_cfg.get("song_order"):
        print(f"  曲順ポリシー: {ffr_cfg.get('song_order')} ({len(songs)}曲)")
    audio_mode = str(ffr_cfg.get("audio_mode") or "aac_encode").strip().lower()
    if audio_mode:
        print(f"  音声モード: {audio_mode}")
    if ffr_cfg.get("intro_sound"):
        cand = _resolve_channel_asset(vol_folder, str(ffr_cfg.get("intro_sound")), num=num)
        if cand and cand.exists():
            intro_audio = cand
            intro_sec = probe_duration(cand)
            print(f"  冒頭SE: {cand.name} ({intro_sec:.2f}s)")
        else:
            print(f"  ⚠ 冒頭SEが見つかりません: {ffr_cfg.get('intro_sound')}")
            intro_audio = None
            intro_sec = 0.0
    if intro_sec >= target:
        print(f"❌ 冒頭SEが目標尺以上です: intro={intro_sec:.2f}s target={target:.2f}s")
        return None
    # 焼き込みの最終決定: 既定 → channel 設定（フォント/サイズ/色/位置）→ Premiere 由来位置、で合成。
    # mode は burn_mode 明示優先、無ければ manifest 継承。結果は manifest に永続化。
    m = load_manifest(vol_folder) or {}
    existing = m.get("burn_titles")
    if burn_mode == "none":
        mode = None
    elif burn_mode:
        mode = burn_mode
    elif existing:
        mode = existing.get("mode", "always")
    else:
        mode = None
    if mode:
        burn = _default_burn(mode)
        burn.update(_load_channel_burn(vol_folder))          # channel 設定（フォント/サイズ/色/接頭辞/位置）
        m["burn_titles"] = burn
        save_manifest(vol_folder, m)
        print(f"  曲名焼き込み: mode={burn['mode']} / font={Path(burn['font']).name} / "
              f"x={burn['x']} y={burn['y']} / size={burn['fontsize']}")
    else:
        burn = None
        if existing is not None:
            m["burn_titles"] = None
            save_manifest(vol_folder, m)

    # 1) 配置 → 2) SRT/TC（vol フォルダに既存と同名で出力）
    print("[1] 配置 + 字幕/チャプター生成...")
    song_target = max(0.0, target - intro_sec)
    timeline_data = load_vol_timeline(vol_folder) if (vol_folder / TIMELINE_NAME).exists() else None
    visualizer_cfg = resolve_visualizer_config(
        _load_channel_visualizer(vol_folder),
        timeline_data.get("visualizer") if isinstance(timeline_data, dict) else None)
    track_states = timeline_data.get("track_states") if isinstance(timeline_data, dict) else {}
    track_states = track_states if isinstance(track_states, dict) else {}
    audio_muted = bool((track_states.get("A1") or {}).get("muted"))
    v1_hidden = bool((track_states.get("V1") or {}).get("hidden"))
    song_clips = compute_placement(songs, song_target, audio_specs)
    if not song_clips:
        print("❌ 配置クリップが空（曲尺取得に失敗）")
        return None
    clips = _shift_clips(song_clips, intro_sec) if intro_sec else song_clips
    print(f"  配置: {len(clips)}クリップ (SE {intro_sec:.2f}s / 最終 {clips[-1]['start']:.0f}-{clips[-1]['end']:.0f}s)")
    srt_path = vol_folder / f"subtitles_{num}.srt"
    tc_path = vol_folder / f"music_time_code_info_{num}.txt"
    generate_srt(clips, str(srt_path))
    chapter_cfg = ffr_cfg.get("chapter_display") if isinstance(ffr_cfg.get("chapter_display"), dict) else {}
    if chapter_cfg:
        generate_display_timecode(clips, tc_path, chapter_cfg, total_songs=len(songs))
    else:
        generate_timecode(clips, str(tc_path))

    # 4) 音声
    print("[2] 音声ビルド...")
    has_crossfade = any(_finite_float(x.get("crossfade_sec"), 0.0, minimum=0.0) > 0 for x in song_clips[1:])
    has_gain = any(abs(_finite_float(x.get("gain_db"), 0.0, minimum=-60.0, maximum=12.0)) > 1e-9 for x in song_clips)
    needs_filtered_audio = has_gain or has_crossfade or intro_audio is not None
    if audio_muted:
        audio = None
        print("  A1消音: 音声ストリームを書き出しから除外")
    elif needs_filtered_audio:
        if audio_mode == "mp3_copy":
            reasons = []
            if has_gain: reasons.append("音量ゲイン")
            if has_crossfade: reasons.append("クロスフェード")
            if intro_audio: reasons.append("冒頭SE")
            print(f"  {' / '.join(reasons)} があるため mp3_copy を使わず AAC 再エンコードします")
        audio = build_audio_crossfades(song_clips, audio_specs, target, scratch / "audio.m4a",
                                       bitrate=audio_bitrate, fade=fade, intro_audio=intro_audio)
    elif audio_mode == "mp3_copy":
        audio = build_audio_mp3_copy(song_clips, target, scratch / "audio.mp3",
                                     cache_dir=cache_dir,
                                     use_cache=use_audio_cache,
                                     intro_audio=intro_audio)
    else:
        audio = build_audio(song_clips, target, scratch / "audio.m4a",
                            bitrate=audio_bitrate, cache_dir=cache_dir,
                            use_cache=use_audio_cache, fade=fade,
                            intro_audio=intro_audio)

    sound_tracks = []
    if timeline_data:
        sound_tracks = [
            t for t in _shift_timed_tracks(timeline_data.get("text_tracks") or [], intro_sec, "clips")
            if not (track_states.get(str(t.get("id") or "")) or {}).get("hidden")
        ]
    type_sound = build_typewriter_sound(sound_tracks, target, scratch / "typewriter_clicks.wav")
    if type_sound:
        if audio_mode == "mp3_copy" and audio is not None:
            print("  タイプ音のミックスが必要なため mp3_copy から AAC 再エンコードへフォールバック")
        if audio_muted:
            print("  A1消音: タイプ音のみを音声トラックに使用")
        audio = mix_typewriter_sound(None if audio_muted else audio, type_sound, target,
                                     scratch / "audio_with_typewriter.m4a", bitrate=audio_bitrate)

    # 5+6) 映像（画像区間 → ループ連結）
    print("[3] 画像区間 + ループ連結...")
    total = clips[-1]["end"]
    intro_video = None
    if intro_audio and intro_cfg.get("enabled", True):
        from_name = str(intro_cfg.get("from") or "サムネイル.jpg")
        to_name = str(intro_cfg.get("to") or (main_img.name if main_img else f"vol{num}.jpg"))
        from_img = _resolve_channel_asset(vol_folder, from_name, num=num)
        to_img = main_img if to_name == "main" else _resolve_channel_asset(vol_folder, to_name, num=num)
        if not from_img or not from_img.is_file():
            from_img = main_img if main_img and main_img.is_file() else to_img
        if not to_img or not to_img.is_file():
            to_img = main_img if main_img and main_img.is_file() else from_img
        if from_img and from_img.exists() and to_img and to_img.exists():
            intro_video = build_intro_dissolve(
                from_img, to_img, intro_sec, scratch / "intro_dissolve.mp4",
                crf=crf, scratch=scratch,
                transition=str(intro_cfg.get("transition") or "fade"))
        else:
            print(f"  ⚠ 冒頭ディゾルブ画像が見つかりません: from={from_name} to={to_name}")

    hybrid_applied = False
    if intro_video:
        rest_total = max(0.1, total - intro_sec)
        has_saved_v1 = bool(timeline_data and timeline_data.get("visual_segments"))
        if has_saved_v1:
            segments = resolve_timeline_image_segments(
                timeline_data["visual_segments"], vol_folder, rest_total,
                fallback_image=main_img,
                global_fade=_finite_float(timeline_data.get("crossfade_sec"), 0.0, minimum=0.0))
        else:
            segments = compute_image_segments(main_img, subs, rest_total)
        print(f"  画像区間: 冒頭ディゾルブ + 本編{len(segments)}区間 / 使用画像 {len({str(s[0]) for s in segments})}枚"
              + (f" / 曲名{len(song_clips)}件を焼き込み" if burn and not has_saved_v1 else ""))
        if has_saved_v1:
            rest_video = build_video_with_crossfades(
                segments, scratch / "video_rest.mp4", crf=crf)
        else:
            rest_video = build_video(segments, scratch / "video_rest.mp4", crf=crf, scratch=scratch,
                                     clips=song_clips, burn=burn, loop_frames=loop_frames)
        video = concat_videos_lossless([intro_video, rest_video], scratch / "video.mp4", scratch=scratch)
    else:
        if timeline_data and timeline_data.get("visual_segments"):
            segments = resolve_timeline_image_segments(
                timeline_data["visual_segments"], vol_folder, total,
                fallback_image=main_img,
                # If the separate intro visual was intentionally disabled (or
                # could not be built), keep authored V1 content song-relative.
                offset=intro_sec if intro_audio else 0.0,
                global_fade=_finite_float(timeline_data.get("crossfade_sec"), 0.0, minimum=0.0))
        else:
            segments = compute_image_segments(main_img, subs, total)
        print(f"  画像区間: {len(segments)}区間 / 使用画像 {len({str(s[0]) for s in segments})}枚"
              + (f" / 曲名{len(clips)}件を焼き込み" if burn else ""))
        if timeline_data:
            timeline_segments = [x if len(x)==4 else [x[0],x[1],x[2],0] for x in segments]
            prepared_text_tracks = [
                t for t in _shift_timed_tracks(timeline_data.get("text_tracks") or [], intro_sec, "clips")
                if not (track_states.get(str(t.get("id") or "")) or {}).get("hidden")]
            legacy_timeline = str(os.environ.get("APP_FFRENDER_LEGACY_TIMELINE") or "").lower() in {
                "1", "true", "yes", "on"}
            active_upper_video = any(
                track.get("segments") and
                not (track_states.get(str(track.get("id") or "")) or {}).get("hidden")
                for track in (timeline_data.get("video_tracks") or [])[1:])
            if visualizer_cfg.get("enabled"):
                print("  ビジュアライザーONのためスタンプ無効（全編エンコード）")
                video = build_video_with_crossfades(timeline_segments, scratch / "video.mp4", crf=crf)
            elif v1_hidden:
                # The black/transparent V1 replacement is applied below; do
                # not bake titles into a base that is about to be replaced.
                video = build_video_with_crossfades(timeline_segments, scratch / "video.mp4", crf=crf)
            elif active_upper_video:
                # V2+ must be composited before every text layer. The current
                # hybrid builder owns V1 chunks only, so use the full base path
                # rather than overlaying V2 over already-burned titles.
                print("  V2以降あり: 文字最前面を保つため全編ベースを使用")
                video = build_video_with_crossfades(timeline_segments, scratch / "video.mp4", crf=crf)
            elif legacy_timeline:
                print("  タイムライン映像: legacy 全編エンコード比較モード")
                video = build_video_with_crossfades(timeline_segments, scratch / "video.mp4", crf=crf)
            else:
                hybrid_now_cfg = dict(timeline_data.get("now_playing") or {})
                if (track_states.get("T1") or {}).get("hidden"):
                    hybrid_now_cfg["enabled"] = False
                video = build_timeline_video_hybrid(
                    timeline_segments, clips, hybrid_now_cfg,
                    prepared_text_tracks, scratch / "video.mp4", crf=crf,
                    scratch=scratch, loop_frames=loop_frames)
                hybrid_applied = True
        else:
            video = build_video(segments, scratch / "video.mp4", crf=crf, scratch=scratch,
                                clips=clips, burn=burn, loop_frames=loop_frames)

    if timeline_data:
        if v1_hidden:
            video = build_hidden_video(total, scratch / "video_v1_hidden.mp4", crf=crf)
        video_tracks = _shift_timed_tracks(timeline_data.get("video_tracks") or [], intro_sec, "segments")
        for track in video_tracks:
            if (track_states.get(str(track.get("id") or "")) or {}).get("hidden"):
                track["segments"] = []
        text_tracks = [t for t in _shift_timed_tracks(timeline_data.get("text_tracks") or [], intro_sec, "clips")
                       if not (track_states.get(str(t.get("id") or "")) or {}).get("hidden")]
        video = apply_video_tracks(video, video_tracks, vol_folder, scratch / "video_tracks.mp4", crf=crf)

    if visualizer_cfg.get("enabled"):
        if audio is None:
            print("  ⚠ ビジュアライザーONですがA1消音のため焼き込みをスキップ")
        else:
            video = apply_audio_visualizer(video, audio, visualizer_cfg,
                                           scratch / "video_visualizer.mp4", crf=crf)

    # Final compositing contract (back to front): V1 -> V2+ -> visualizer ->
    # Now Playing -> authored T1/T2/... text. Hybrid chunks already contain the
    # final two layers in this same order and never run when a visualizer/V2+ is
    # active, so they must not be applied twice here.
    if timeline_data and not hybrid_applied:
        if not (track_states.get("T1") or {}).get("hidden"):
            video = apply_now_playing(video, clips, timeline_data.get("now_playing") or {},
                                      scratch / "video_now_playing.mp4", crf=crf, scratch=scratch)
        video = apply_text_tracks(video, text_tracks, scratch / "video_text_tracks.mp4",
                                  crf=crf, scratch=scratch)

    # 7) mux
    print("[4] mux...")
    final_scratch = scratch / f"final_vol{num}.mp4"
    mux(video, audio, srt_path, final_scratch)

    # 宛先へ 1 回だけコピー（Drive sync storm 回避）
    if output_path is None:
        prefix = _read_file_prefix()
        output_path = vol_folder / f"{prefix}_vol{num}.mp4"
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[5] 宛先へコピー: {output_path}")
    shutil.copy2(final_scratch, output_path)

    # 検証
    dur = probe_duration(output_path)
    adur = probe_duration(audio)
    size_mb = output_path.stat().st_size / 1024 / 1024
    print("=" * 60)
    print(f"  完了: {output_path.name}  {size_mb:.0f}MB  {dur:.1f}s ({dur/3600:.2f}h)")
    print(f"  A/V ズレ={abs(dur - adur):.3f}s / 総時間={time.time() - t_all:.1f}s")
    print(f"  SRT={srt_path.name} / TC={tc_path.name}")
    print("=" * 60)
    if abs(dur - target) > 2.0:
        print(f"  ⚠ 尺が target と {abs(dur - target):.1f}s ずれています")
    return output_path


def _parse_swap(s: str) -> tuple:
    """'対象=新ファイル.mp3' を (対象, 新ファイル) に。"""
    if "=" not in s:
        raise argparse.ArgumentTypeError(f"--swap は '対象=新ファイル.mp3' 形式: {s}")
    old, newf = s.split("=", 1)
    return old.strip(), newf.strip()


def main():
    ap = argparse.ArgumentParser(description="ループ連結方式レンダラー（AME 代替 ffmpeg 書き出し）")
    ap.add_argument("--vol-folder", required=True, help="vol フォルダ（music/ と画像を含む）")
    ap.add_argument("--duration", type=int, default=None,
                    help="目標尺(秒)。未指定なら manifest 尺 → 既定 10800。明示時のみ上書き")
    ap.add_argument("--output-path", default=None, help="出力 mp4 フルパス（未指定なら vol フォルダ内 {prefix}_vol{N}.mp4）")
    ap.add_argument("--scratch", default=None, help="作業ディレクトリ（既定 /tmp/ffrender/<vol>）")
    ap.add_argument("--crf", type=int, default=DEFAULT_CRF, help="ループ素材 CRF（容量ノブ。既定 18／28 で約1/4）")
    ap.add_argument("--audio-bitrate", default=DEFAULT_AUDIO_BITRATE)
    ap.add_argument("--no-audio-cache", action="store_true", help="音声キャッシュを使わない")
    # 構成ソース / 編集
    ap.add_argument("--from-premiere", action="store_true",
                    help="【レガシーブリッジ】開いている Premiere の配置済みタイムラインを読んで manifest 化してから書き出し")
    ap.add_argument("--swap", action="append", default=[], type=_parse_swap, metavar="対象=新.mp3",
                    help="【著作権修正】manifest の曲を差し替え（複数可）。例: --swap 'Old Title=New.mp3'")
    ap.add_argument("--remove", action="append", default=[], metavar="対象",
                    help="【著作権修正】manifest から曲を削除（複数可）")
    ap.add_argument("--show-manifest", action="store_true", help="manifest を表示して終了")
    ap.add_argument("--no-render", action="store_true", help="manifest 操作のみ（書き出ししない）")
    ap.add_argument("--burn-titles", choices=["none", "always"], default=None,
                    help="曲名焼き込み（常時表示）。未指定なら manifest 継承。none=焼き込み無し")
    args = ap.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("❌ ffmpeg / ffprobe が見つかりません（brew install ffmpeg）")
        sys.exit(1)

    vol = Path(args.vol_folder).expanduser()

    if args.show_manifest:
        m = load_manifest(vol)
        print(json.dumps(m, ensure_ascii=False, indent=2) if m else "(manifest なし)")
        sys.exit(0)

    try:
        # 構成ソースの確定（manifest を作る/編集する段）
        if args.from_premiere:
            build_manifest_from_premiere(vol, target_override=args.duration)
        if args.swap or args.remove:
            edit_manifest_order(vol, args.swap, args.remove)

        if args.no_render:
            print(" --no-render: manifest 操作のみ完了")
            sys.exit(0)

        out = render(vol,
                     target=args.duration,
                     output_path=Path(args.output_path).expanduser() if args.output_path else None,
                     scratch=Path(args.scratch).expanduser() if args.scratch else None,
                     crf=args.crf, audio_bitrate=args.audio_bitrate,
                     use_audio_cache=not args.no_audio_cache,
                     burn_mode=args.burn_titles)
    except Exception as e:
        print(f"❌ ffrender 失敗: {e}")
        sys.exit(1)
    sys.exit(0 if out and out.exists() else 1)


if __name__ == "__main__":
    main()
