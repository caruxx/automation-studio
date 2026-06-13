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
import os
import random
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

# SRT/TC 生成は app_premiere の純関数を再利用（Premiere に触れない関数のみ）
from app_premiere import generate_srt, generate_timecode, _read_file_prefix  # noqa: E402

# ── AME 目標スペック（HN_vol11.mp4 実測値に合わせる） ──────────────────
FPS = "30000/1001"          # 29.97
WIDTH, HEIGHT = 1920, 1080
GOP_FRAMES = 150            # 1 ループ素材 = 150 フレーム = 1 closed GOP（約 5.005s）
DEFAULT_CRF = 18           # 静止画ループ素材の品質。容量ノブ（28 なら約 1/4）
DEFAULT_AUDIO_BITRATE = "320k"  # AME は 317k
FADE_SEC = 20              # ラストフェードアウト
LIMITER = "alimiter=limit=0.97"

CACHE_ROOT = Path.home() / ".cache" / "ffrender"


# ── 小物 ───────────────────────────────────────────────────────────────

def _run_ff(cmd: list, label: str) -> float:
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True)
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
        capture_output=True, text=True)
    try:
        return float((r.stdout or "0").strip() or 0)
    except ValueError:
        return 0.0


def _title_from(name: str) -> str:
    noext = re.sub(r"\.[^.]+$", "", name)
    return re.sub(r"^z+_", "", noext)


def _extract_num(folder: Path) -> str:
    m = re.match(r"^(\d+)_", folder.name)
    return m.group(1) if m else "00"


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
    返り値: (order:list[Path], main, subs, target:int, bitrate, fade, burn)"""
    vol_folder = Path(vol_folder)
    music_dir = vol_folder / "music"
    num = _extract_num(vol_folder)
    by_name = {f.name: f for f in music_dir.glob("*.mp3")}
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
        return order, main, subs, target, bitrate, fade, burn
    # 新規: cache の order.json を継承（あれば）→ 無ければ新規 shuffle
    order = resolve_order(music_dir, cache_dir)
    main, subs = select_images(vol_folder, num)
    target = int(target_override or 10800)
    data = _new_manifest(num, [f.name for f in order], main, subs, target,
                         bitrate=bitrate, fade=FADE_SEC, source="fresh")
    save_manifest(vol_folder, data)
    print(f"  構成: 新規生成→manifest 保存 ({len(order)}曲, target={target}s)")
    return order, main, subs, target, bitrate, FADE_SEC, None


# ── 2. 配置計算（JSX 準拠: target まで loop、末尾 clip を target でトリム） ─

def compute_placement(songs: list, target: float) -> list:
    """clips = [{start,end,title,path}]。set>target ならループ無し・末尾トリム。"""
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
        clips.append({"start": cursor, "end": cursor + d,
                      "title": _title_from(s.name), "path": s})
        cursor += d
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


# ── 4. 音声ビルド（concat → トリム → fade → limiter → AAC、キャッシュ付き） ─

def build_audio(clips: list, target: float, out: Path, *,
                bitrate: str = DEFAULT_AUDIO_BITRATE, cache_dir: Path,
                use_cache: bool = True, fade: float = FADE_SEC) -> Path:
    names = [c["path"].name for c in clips]
    key = hashlib.sha1(json.dumps(
        {"o": names, "t": round(target, 2), "f": fade, "b": bitrate, "lim": LIMITER},
        ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
    cached = cache_dir / f"audio_{key}.m4a"
    if use_cache and cached.exists() and probe_duration(cached) >= target - 5:
        print(f"  音声: キャッシュ再利用 {cached.name} ({cached.stat().st_size/1024/1024:.0f}MB)")
        shutil.copy2(cached, out)
        return out

    cache_dir.mkdir(parents=True, exist_ok=True)
    list_txt = cache_dir / f"concat_{key}.txt"
    with open(list_txt, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '%s'\n" % str(c["path"]).replace("'", r"'\''"))
    af = f"afade=t=out:st={target - fade}:d={fade},{LIMITER}"
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
             title: Optional[str] = None, burn: Optional[dict] = None) -> Path:
    """画像（＋常時表示の題字）を 1 closed GOP にエンコードしてキャッシュ。"""
    key = (str(img), img.stat().st_mtime_ns, crf, title or "")
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
             "-loop", "1", "-i", str(img), "-r", FPS, "-frames:v", str(GOP_FRAMES),
             "-vf", vf, "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
             "-pix_fmt", "yuv420p", "-color_range", "tv",
             "-x264-params", f"keyint={GOP_FRAMES}:min-keyint={GOP_FRAMES}:scenecut=0",
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
                  burn: Optional[dict]) -> Path:
    """blocks=[[img, title|None, dur]] を GOP→stream_loop→（複数なら TS ロスレス）連結。"""
    gop_cache: dict = {}
    if len(blocks) == 1:
        img, title, dur = blocks[0]
        gop = _img_gop(img, crf, scratch, gop_cache, title, burn)
        _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-stream_loop", "-1", "-i", str(gop), "-t", f"{dur:.3f}",
                 "-c", "copy", str(out)], "連結(単一ブロック) stream_loop")
        return out
    ts_files = []
    for idx, (img, title, dur) in enumerate(blocks):
        gop = _img_gop(img, crf, scratch, gop_cache, title, burn)
        ts = scratch / f"seg_{idx:03d}.ts"
        _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                 "-stream_loop", "-1", "-i", str(gop), "-t", f"{dur:.3f}",
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
                clips: Optional[list] = None, burn: Optional[dict] = None) -> Path:
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
    return _build_blocks(blocks, out, crf=crf, scratch=scratch, burn=burn)


# ── 7. mux ──────────────────────────────────────────────────────────────

def mux(video: Path, audio: Path, srt: Path, out: Path) -> Path:
    _run_ff(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-i", str(video), "-i", str(audio), "-i", str(srt),
             "-map", "0:v:0", "-map", "1:a:0", "-map", "2:0",
             "-c:v", "copy", "-c:a", "copy",
             "-c:s", "mov_text", "-metadata:s:s:0", "language=eng",
             "-movflags", "+faststart", "-shortest", str(out)], "mux(v+a+mov_text)")
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
    songs, main_img, subs, target, audio_bitrate, fade, burn = resolve_composition(
        vol_folder, target_override=target, cache_dir=cache_dir, bitrate=audio_bitrate)
    if not songs:
        print("❌ 曲順が空（music/ 解決に失敗）")
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
    clips = compute_placement(songs, target)
    if not clips:
        print("❌ 配置クリップが空（曲尺取得に失敗）")
        return None
    print(f"  配置: {len(clips)}クリップ (最終 {clips[-1]['start']:.0f}-{clips[-1]['end']:.0f}s)")
    srt_path = vol_folder / f"subtitles_{num}.srt"
    tc_path = vol_folder / f"music_time_code_info_{num}.txt"
    generate_srt(clips, str(srt_path))
    generate_timecode(clips, str(tc_path))

    # 4) 音声
    print("[2] 音声ビルド...")
    audio = build_audio(clips, target, scratch / "audio.m4a",
                        bitrate=audio_bitrate, cache_dir=cache_dir,
                        use_cache=use_audio_cache, fade=fade)

    # 5+6) 映像（画像区間 → ループ連結）
    print("[3] 画像区間 + ループ連結...")
    total = clips[-1]["end"]
    segments = compute_image_segments(main_img, subs, total)
    print(f"  画像区間: {len(segments)}区間 / 使用画像 {len({str(s[0]) for s in segments})}枚"
          + (f" / 曲名{len(clips)}件を焼き込み" if burn else ""))
    video = build_video(segments, scratch / "video.mp4", crf=crf, scratch=scratch,
                        clips=clips, burn=burn)

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
