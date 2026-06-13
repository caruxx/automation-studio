#!/usr/bin/env python3
"""ライブ配信ルータ v2。VPS(SSH) 経由の YouTube 24/7 マルチストリーム配信を管理する。

データモデル: group（= YouTube チャンネル / プレフィックス例 orzz, sk）の下に複数の
配信ストリーム（orzz_1, orzz_2, ...）。動画プールは VPS の videos/<group>/ を共有。

エンドポイント:
  - GET/PUT /api/live/config            … VPS 接続情報 + 配信ストリーム設定（stream key はマスク）
  - POST    /api/live/setup             … 初期設定（鍵生成→鍵登録→ffmpeg/screen 導入→スクリプト配置）
  - GET     /api/live/status            … VPS 負荷 + グループ別容量 + ストリーム別稼働（UI が 10 秒ポーリング）
  - POST    /api/live/streams/{id}/start|stop|restart|swap(?next=1)
  - GET     /api/live/streams/{id}/log
  - GET     /api/live/viewers           … 同時視聴者数（YouTube API・5 分キャッシュ・?force=1 で即時）
  - GET     /api/live/local-videos      … 配信ソース候補（チャンネルフォルダ or folder= 任意フォルダ/ファイル）
  - GET     /api/live/pick-local        … macOS ネイティブ選択ダイアログ（kind=folder|file）
  - GET     /api/live/local-thumbnails  … サムネ画像候補（同上 jpg/png）
  - POST    /api/live/upload, GET /api/live/upload/{job_id} … 再開可能アップロード（並行可）と進捗
  - GET     /api/live/uploads, DELETE /api/live/upload/{job_id} … ジョブ一覧（再アタッチ用）/ 中止
  - GET     /api/live/remote-videos / DELETE /api/live/remote-videos … VPS 動画一覧 / 削除
  - GET/PUT /api/live/broadcasts        … YouTube ライブのタイトル/説明/公開設定/AI開示 取得・更新
  - POST    /api/live/broadcasts/suggest … タイトル/説明文の LLM 提案（保存はしない）
  - POST    /api/live/thumbnail         … ライブのサムネイル設定（thumbnails.set）
実体は app_live.py（SSH 制御）と app_youtube.py（YouTube Data API）。
"""
import time as _time

from fastapi import APIRouter
from app_core import *  # noqa: F401,F403  土台シンボル(stdlib/fastapi/foundation)を全取り込み

import app_live

router = APIRouter()

MASK_PREFIX = "••••"


def _mask_stream_key(key: str) -> str:
    if not key:
        return ""
    return MASK_PREFIX + key[-4:] if len(key) > 4 else MASK_PREFIX


def _is_masked(key: str) -> bool:
    return bool(key) and ("•" in key or "●" in key)


def _registry_by_id() -> dict:
    try:
        return {c.get("id"): c for c in get_channels() if c.get("id")}
    except Exception:
        return {}


def _safe_config(cfg: dict) -> dict:
    out = {"vps": {**cfg.get("vps", {})}, "streams": []}
    for s in cfg.get("streams", []):
        c = {**s}
        c["stream_key"] = _mask_stream_key(c.get("stream_key", ""))
        out["streams"].append(c)
    return out


# ─── 設定 ───

@router.get("/api/live/config")
def api_live_get_config():
    cfg = app_live.load_live_config()
    key_path = app_live._key_path(cfg["vps"])
    pub = key_path.with_suffix(".pub")
    registry = []
    for c in get_channels():
        registry.append({
            "id": c.get("id", ""),
            "name": c.get("name", ""),
            "youtube_channel_id": c.get("youtube_channel_id", ""),
            "icon_url": c.get("icon_url", ""),
        })
    return {
        "status": "ok",
        "config": _safe_config(cfg),
        "key_exists": key_path.exists(),
        "pubkey": pub.read_text().strip() if pub.exists() else "",
        "registry_channels": registry,
    }


class LiveConfigUpdate(BaseModel):
    vps: Optional[dict] = None
    streams: Optional[List[dict]] = None
    push_env: bool = True  # 保存時に VPS の env も更新する（接続可能な場合）


@router.put("/api/live/config")
def api_live_put_config(req: LiveConfigUpdate):
    cfg = app_live.load_live_config()
    prev_keys = {s.get("id"): s.get("stream_key", "") for s in cfg["streams"]}

    if req.vps is not None:
        allowed = {"name", "host", "port", "user", "ssh_key", "remote_dir"}
        cfg["vps"].update({k: v for k, v in req.vps.items() if k in allowed})
        try:
            cfg["vps"]["port"] = int(cfg["vps"].get("port") or 22)
        except Exception:
            cfg["vps"]["port"] = 22

    pushed, push_errors = [], []
    removed_ids = []
    if req.streams is not None:
        new_streams = []
        seen = set()
        for s in req.streams:
            sid = (s.get("id") or "").strip()
            grp = (s.get("group") or "").strip()
            if not app_live.CH_ID_RE.match(sid):
                raise HTTPException(400, f"配信IDは英数字・ハイフン・アンダースコアのみ: {sid!r}")
            if not app_live.CH_ID_RE.match(grp):
                raise HTTPException(400, f"グループIDは英数字・ハイフン・アンダースコアのみ: {grp!r}")
            if sid in seen:
                raise HTTPException(400, f"配信IDが重複: {sid}")
            seen.add(sid)
            merged = {**app_live.DEFAULT_STREAM, **s}
            # マスク済み stream key は既存値を維持
            if _is_masked(merged.get("stream_key", "")):
                merged["stream_key"] = prev_keys.get(sid, "")
            if not isinstance(merged.get("playlist"), list):
                merged["playlist"] = []
            merged["playlist"] = [str(p) for p in merged["playlist"] if p]
            try:
                merged["rotate_hours"] = max(0.0, float(merged.get("rotate_hours") or 0))
                merged["max_hours"] = max(0.0, float(merged.get("max_hours") or 0))
            except Exception:
                raise HTTPException(400, f"rotate_hours / max_hours は数値で指定: {sid}")
            new_streams.append(merged)
        removed_ids = [sid for sid in prev_keys.keys() if sid and sid not in seen]
        cfg["streams"] = new_streams

    app_live.save_live_config(cfg)

    # 削除されたストリームは VPS 側も後始末（配信停止 + env/状態ファイル削除。ベストエフォート）
    if removed_ids and cfg["vps"].get("host"):
        for sid in removed_ids:
            app_live.stop_stream(cfg["vps"], sid)
            app_live.remove_stream_env(cfg["vps"], sid)

    # env push（ベストエフォート: VPS 不達でも保存自体は成功扱い）
    if req.push_env and req.streams is not None and cfg["vps"].get("host"):
        for s in cfg["streams"]:
            r = app_live.push_stream_env(cfg["vps"], s)
            (pushed if r["ok"] else push_errors).append(f"{s['id']}: {r['detail'] if not r['ok'] else 'OK'}")

    return {"status": "ok", "config": _safe_config(cfg), "env_pushed": pushed, "env_push_errors": push_errors}


# ─── 初期設定 ───

class LiveSetupRequest(BaseModel):
    host: Optional[str] = None
    port: Optional[int] = None
    user: Optional[str] = None
    remote_dir: Optional[str] = None
    name: Optional[str] = None
    ssh_key: Optional[str] = None
    password: Optional[str] = None  # 鍵登録のみに使用・保存しない


@router.post("/api/live/setup")
def api_live_setup(req: LiveSetupRequest):
    """初期設定を一括実行: 鍵生成 → (パスワードがあれば)鍵登録 → 接続テスト → ffmpeg/screen 導入 → スクリプト配置。"""
    cfg = app_live.load_live_config()
    for k in ("host", "port", "user", "remote_dir", "name", "ssh_key"):
        v = getattr(req, k)
        if v is not None and str(v).strip() != "":
            cfg["vps"][k] = v
    app_live.save_live_config(cfg)
    vps = cfg["vps"]
    if not vps.get("host"):
        raise HTTPException(400, "VPS ホストを入力してください")

    steps = []

    def step(name, result):
        steps.append({"name": name, "ok": bool(result.get("ok")), "detail": result.get("detail", "")})
        return result.get("ok")

    if not step("SSH鍵の準備", app_live.ensure_local_key(vps)):
        return {"status": "error", "steps": steps}

    conn = app_live.test_connection(vps)
    if not conn["ok"] and req.password:
        if not step("公開鍵の登録(パスワード認証)", app_live.register_key_with_password(vps, req.password)):
            return {"status": "error", "steps": steps}
        conn = app_live.test_connection(vps)
    if not step("SSH接続テスト", conn):
        steps[-1]["detail"] += "（パスワードを入力して再実行すると鍵を自動登録します）"
        return {"status": "error", "steps": steps}

    if not step("ffmpeg/screen 導入 + ディレクトリ作成", app_live.bootstrap_vps(vps)):
        return {"status": "error", "steps": steps}
    if not step("配信スクリプト配置", app_live.push_scripts(vps)):
        return {"status": "error", "steps": steps}

    # 登録済みストリームの env も同期
    for s in cfg["streams"]:
        r = app_live.push_stream_env(vps, s)
        steps.append({"name": f"配信設定 push: {s['id']}", "ok": r["ok"], "detail": r.get("detail", "")})

    return {"status": "ok", "steps": steps}


# ─── 状態・操作 ───

@router.get("/api/live/status")
def api_live_status():
    cfg = app_live.load_live_config()
    vps = cfg["vps"]
    if not vps.get("host"):
        return {"status": "unconfigured", "vps": {}, "streams": [], "host": None}
    remote = app_live.fetch_status(vps)
    remote_by_id = {c.get("id"): c for c in remote.get("channels", [])} if remote.get("ok") else {}
    registry = _registry_by_id()
    streams = []
    for s in cfg["streams"]:
        reg = registry.get(s.get("registry_id") or s.get("group"), {})
        rc = remote_by_id.get(s["id"], {})
        streams.append({
            "id": s["id"],
            "group": s.get("group", ""),
            "label": s.get("label") or s["id"],
            "registry_id": s.get("registry_id", ""),
            "registry_name": reg.get("name", ""),
            "youtube_channel_id": reg.get("youtube_channel_id", ""),
            "icon_url": reg.get("icon_url", ""),
            "mode": s.get("mode", "copy"),
            "video": s.get("video", ""),
            "playlist": s.get("playlist", []),
            "rotate_hours": s.get("rotate_hours", 0),
            "max_hours": s.get("max_hours", 0),
            "has_stream_key": bool(s.get("stream_key")),
            "remote": rc or None,
            "running": bool(rc.get("running")),
            "channel_live_url": f"https://www.youtube.com/channel/{reg.get('youtube_channel_id')}/live" if reg.get("youtube_channel_id") else "",
        })
    return {
        "status": "ok" if remote.get("ok") else "vps_error",
        "vps": {"host": vps.get("host"), "name": vps.get("name", ""), "user": vps.get("user"), "port": vps.get("port"), "remote_dir": vps.get("remote_dir")},
        "vps_error": remote.get("error", "") if not remote.get("ok") else "",
        "host": remote.get("host") if remote.get("ok") else None,
        "streams": streams,
        "ts": remote.get("ts", ""),
    }


def _get_stream_or_404(cfg: dict, stream_id: str) -> dict:
    try:
        return app_live.get_stream(cfg, stream_id)
    except KeyError as e:
        raise HTTPException(404, str(e))


@router.post("/api/live/streams/{stream_id}/start")
def api_live_start(stream_id: str):
    cfg = app_live.load_live_config()
    s = _get_stream_or_404(cfg, stream_id)
    if not s.get("stream_key"):
        raise HTTPException(400, "ストリームキーが未設定です（ストリーム設定で入力してください）")
    if not s.get("video") and not s.get("playlist"):
        raise HTTPException(400, "配信動画（またはプレイリスト）が未設定です")
    # 最新設定を反映してから起動
    r = app_live.push_stream_env(cfg["vps"], s)
    if not r["ok"]:
        raise HTTPException(500, f"設定 push 失敗: {r['detail']}")
    res = app_live.start_stream(cfg["vps"], stream_id)
    if not res["ok"]:
        raise HTTPException(500, f"起動失敗: {res['detail']}")
    return {"status": "ok", "detail": res["detail"]}


@router.post("/api/live/streams/{stream_id}/stop")
def api_live_stop(stream_id: str):
    cfg = app_live.load_live_config()
    res = app_live.stop_stream(cfg["vps"], stream_id)
    if not res["ok"]:
        raise HTTPException(500, f"停止失敗: {res['detail']}")
    return {"status": "ok", "detail": res["detail"]}


@router.post("/api/live/streams/{stream_id}/restart")
def api_live_restart(stream_id: str):
    cfg = app_live.load_live_config()
    try:
        s = app_live.get_stream(cfg, stream_id)
        app_live.push_stream_env(cfg["vps"], s)
    except KeyError:
        pass
    res = app_live.restart_stream(cfg["vps"], stream_id)
    if not res["ok"]:
        raise HTTPException(500, f"再起動失敗: {res['detail']}")
    return {"status": "ok", "detail": res["detail"]}


@router.post("/api/live/streams/{stream_id}/swap")
def api_live_swap(stream_id: str, next: int = 0):
    """配信を止めずに動画を差し替える。?next=1 でプレイリストの次の動画へ。

    事前に PUT /api/live/config で video/playlist を更新（env push 済み）してから呼ぶと
    新しい設定で数秒後に再接続される（YouTube 側は同一ライブ継続）。
    """
    cfg = app_live.load_live_config()
    s = _get_stream_or_404(cfg, stream_id)
    # swap 前に最新 env を反映
    app_live.push_stream_env(cfg["vps"], s)
    res = app_live.swap_stream(cfg["vps"], stream_id, to_next=bool(next))
    if not res["ok"]:
        raise HTTPException(500, f"差し替え失敗: {res['detail']}")
    return {"status": "ok", "detail": res["detail"]}


@router.get("/api/live/streams/{stream_id}/log")
def api_live_log(stream_id: str, lines: int = 80):
    cfg = app_live.load_live_config()
    r = app_live.fetch_log(cfg["vps"], stream_id, lines)
    return {"status": "ok" if r["ok"] else "error", "log": r.get("log", ""), "error": r.get("error", "")}


# ─── 同時視聴者数（YouTube API・キャッシュ付き） ───

_VIEWERS_CACHE = {"ts": 0.0, "data": None}
_VIEWERS_TTL = 300  # 5 分


def _fetch_viewers(cfg: dict) -> dict:
    """グループ（registry）単位で active broadcast / ストリームキー / 視聴者数を取得し、
    ローカル stream_key と突き合わせて stream_id → 視聴情報 を返す。quota ≈ 3 unit/グループ。"""
    import app_youtube
    registry = _registry_by_id()
    by_stream: dict = {}
    errors = []
    # registry_id ごとに 1 回だけ YouTube API を呼ぶ
    reg_ids = {}
    for s in cfg["streams"]:
        rid = s.get("registry_id") or s.get("group")
        if rid:
            reg_ids.setdefault(rid, []).append(s)
    for rid, streams in reg_ids.items():
        reg = registry.get(rid)
        if not reg or not reg.get("folder"):
            continue
        token = Path(reg["folder"]) / ".youtube_token.json"
        if not token.exists():
            errors.append(f"{rid}: YouTube 未認証")
            continue
        bres = app_youtube.list_live_broadcasts(token, statuses=("active",))
        if bres.get("status") != "ok":
            errors.append(f"{rid}: {bres.get('error', 'broadcast 取得失敗')}")
            continue
        broadcasts = bres.get("broadcasts", [])
        if not broadcasts:
            continue
        sres = app_youtube.list_my_live_streams(token)
        key_by_ytstream = {st["id"]: st["key"] for st in sres.get("streams", [])} if sres.get("status") == "ok" else {}
        vres = app_youtube.get_live_viewers(token, [b["id"] for b in broadcasts])
        vmap = vres.get("videos", {}) if vres.get("status") == "ok" else {}
        # broadcast → 配信キー → ローカル stream
        bc_by_key = {}
        for b in broadcasts:
            k = key_by_ytstream.get(b.get("bound_stream_id", ""))
            if k:
                bc_by_key[k] = b
        for s in streams:
            b = bc_by_key.get(s.get("stream_key", ""))
            if not b:
                continue
            v = vmap.get(b["id"], {})
            by_stream[s["id"]] = {
                "video_id": b["id"],
                "title": b.get("title", "") or v.get("title", ""),
                "viewers": v.get("viewers", 0),
                "thumbnail": v.get("thumbnail", "") or b.get("thumbnail", ""),
                "watch_url": b.get("watch_url", ""),
                "actual_start": v.get("actual_start", "") or b.get("actual_start", ""),
            }
    return {"streams": by_stream, "errors": errors}


@router.get("/api/live/viewers")
def api_live_viewers(force: int = 0):
    """全配信の同時視聴者数。5 分キャッシュ（quota 節約）。?force=1 で即時再取得。"""
    now = _time.time()
    if not force and _VIEWERS_CACHE["data"] is not None and now - _VIEWERS_CACHE["ts"] < _VIEWERS_TTL:
        return {"status": "ok", "cached": True, "age_sec": int(now - _VIEWERS_CACHE["ts"]), **_VIEWERS_CACHE["data"]}
    cfg = app_live.load_live_config()
    data = _fetch_viewers(cfg)
    _VIEWERS_CACHE["ts"] = now
    _VIEWERS_CACHE["data"] = data
    return {"status": "ok", "cached": False, "age_sec": 0, **data}


# ─── 動画（ローカル候補・アップロード・リモート一覧/削除・サムネ候補） ───

def _list_local_files(registry_id: str, exts: tuple, min_bytes: int, folder: str = "") -> dict:
    """ローカル動画/画像の候補一覧。folder 指定時は任意フォルダ（外部 SSD 等）を、
    未指定時はレジストリのチャンネルフォルダをスキャンする（直下 + 1 階層）。
    拡張子は大文字小文字を問わない（外部 SSD の .MP4/.MOV 対策）。
    folder に動画ファイルそのものを指定した場合はその 1 件を返す。"""
    single = None
    if folder:
        base = Path(folder).expanduser()
        if base.is_file():
            if base.suffix.lower() not in exts:
                return {"status": "error", "files": [],
                        "error": f"対応していない拡張子です（{' / '.join(exts)}）: {base.name}", "folder": str(base)}
            single, folder = base, base.parent
        elif not base.is_dir():
            return {"status": "error", "files": [],
                    "error": f"フォルダが見つからない: {base}（外部 SSD はマウントされているか確認）", "folder": str(base)}
        else:
            folder = base
    else:
        registry = _registry_by_id()
        reg = registry.get(registry_id)
        if not reg or not reg.get("folder"):
            return {"status": "ok", "files": [], "folder": ""}
        folder = Path(reg["folder"])
        if not folder.exists():
            return {"status": "ok", "files": [], "folder": str(folder)}
    files = []
    skipped_small = 0
    try:
        if single is not None:
            candidates = [single]
        else:
            candidates = [p for p in folder.iterdir() if p.is_file()]
            for d in folder.iterdir():
                if d.is_dir() and not d.name.startswith("."):
                    try:
                        candidates += [p for p in d.iterdir() if p.is_file()]
                    except OSError:
                        continue
            # exFAT の AppleDouble（._foo.mp4）を除外しつつ大文字小文字不問で拡張子判定
            candidates = [p for p in candidates
                          if p.suffix.lower() in exts and not p.name.startswith("._")]
        for p in candidates:
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if size < min_bytes:
                skipped_small += 1
                continue
            files.append({
                "name": p.name,
                "rel": str(p.relative_to(folder)),
                "path": str(p),
                "size_mb": round(size / 2**20, 1),
                "mtime": int(p.stat().st_mtime),
            })
    except PermissionError:
        return {"status": "error", "files": [], "folder": str(folder),
                "error": ("読み取り権限がありません。外部ドライブの場合は macOS の "
                          "システム設定 → プライバシーとセキュリティ → ファイルとフォルダ"
                          "（またはフルディスクアクセス）でこのサーバー（ターミナル/Python）に許可してください")}
    except Exception as e:
        return {"status": "error", "files": [], "error": str(e), "folder": str(folder)}
    files.sort(key=lambda v: v["mtime"], reverse=True)
    return {"status": "ok", "files": files[:60], "folder": str(folder), "skipped_small": skipped_small}


@router.get("/api/live/local-videos")
def api_live_local_videos(registry_id: str = "", folder: str = ""):
    """配信ソース候補。レジストリのチャンネルフォルダ（と直下 1 階層）から動画ファイルを探す。
    folder 指定時は任意フォルダ（外部 SSD 等・例 /Volumes/MySSD/videos）か動画ファイル単体を受け付ける。"""
    r = _list_local_files(registry_id, (".mp4", ".mov", ".m4v"), 10 * 2**20, folder=folder)
    return {"status": r["status"], "videos": r["files"], "folder": r.get("folder", ""),
            "error": r.get("error", ""), "skipped_small": r.get("skipped_small", 0)}


@router.get("/api/live/pick-local")
def api_live_pick_local(kind: str = "folder"):
    """macOS のネイティブ選択ダイアログ（osascript）でアップロード元のフォルダ/動画ファイルを選ぶ。
    ダイアログはサーバーが動いている Mac の画面に表示される（ダッシュボードと同一 Mac で操作する前提）。
    kind=folder: フォルダ 1 つ → {folder}。kind=file: 動画ファイル複数可 → {files:[...]}。
    キャンセル時は status=cancelled。"""
    if kind not in ("folder", "file"):
        raise HTTPException(400, "kind は folder か file")
    if kind == "folder":
        script = (
            'activate\n'
            'set f to choose folder with prompt "配信動画のフォルダを選択（外部 SSD は /Volumes 配下）"\n'
            'POSIX path of f'
        )
    else:
        script = (
            'activate\n'
            'set fs to choose file with prompt "アップロードする動画を選択（複数可）" '
            'of type {"public.movie"} with multiple selections allowed\n'
            'set out to ""\n'
            'repeat with f in fs\n'
            '    set out to out & POSIX path of f & linefeed\n'
            'end repeat\n'
            'return out'
        )
    try:
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return {"status": "cancelled", "error": "選択がタイムアウトしました（5分）"}
    except FileNotFoundError:
        return {"status": "error", "error": "osascript が見つかりません（macOS 以外では使えません）"}
    if r.returncode != 0:
        err = (r.stderr or "").strip()
        if "-128" in err or "canceled" in err.lower():
            return {"status": "cancelled"}
        return {"status": "error", "error": err or "ダイアログを開けませんでした"}
    if kind == "folder":
        return {"status": "ok", "folder": (r.stdout or "").strip().rstrip("/")}
    files = []
    for line in (r.stdout or "").splitlines():
        p = Path(line.strip())
        if not line.strip() or not p.is_file():
            continue
        try:
            size = p.stat().st_size
        except OSError:
            continue
        files.append({"name": p.name, "rel": p.name, "path": str(p),
                      "size_mb": round(size / 2**20, 1), "mtime": int(p.stat().st_mtime)})
    return {"status": "ok", "files": files}


@router.get("/api/live/local-thumbnails")
def api_live_local_thumbnails(registry_id: str = ""):
    """サムネイル候補。レジストリのチャンネルフォルダから jpg/png を探す。"""
    r = _list_local_files(registry_id, (".jpg", ".jpeg", ".png"), 10 * 2**10)
    return {"status": r["status"], "images": r["files"], "folder": r.get("folder", ""), "error": r.get("error", "")}


class LiveUploadRequest(BaseModel):
    group: str
    local_path: str
    dest_name: Optional[str] = None


@router.post("/api/live/upload")
def api_live_upload(req: LiveUploadRequest):
    cfg = app_live.load_live_config()
    r = app_live.start_upload(cfg["vps"], req.group, req.local_path, req.dest_name or "")
    if not r.get("ok"):
        raise HTTPException(400, r.get("error", "アップロード開始失敗"))
    return {"status": "ok", "job_id": r["job_id"], "remote": r["remote"]}


@router.get("/api/live/upload/{job_id}")
def api_live_upload_status(job_id: str):
    cfg = app_live.load_live_config()
    r = app_live.upload_status(cfg["vps"], job_id)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "ジョブ不明"))
    return {"status": "ok", **r}


@router.get("/api/live/uploads")
def api_live_uploads(group: str = ""):
    """アップロードジョブ一覧（実行中+直近完了/失敗）。ページ遷移後の進捗再表示に使う。"""
    cfg = app_live.load_live_config()
    return {"status": "ok", **app_live.list_uploads(cfg["vps"], group)}


@router.delete("/api/live/uploads")
def api_live_uploads_clear(group: str = ""):
    """完了/失敗/中止ジョブの表示をクリア（実行中ジョブは残る）。"""
    return {"status": "ok", **app_live.clear_finished_uploads(group)}


@router.delete("/api/live/upload/{job_id}")
def api_live_upload_cancel(job_id: str):
    r = app_live.cancel_upload(job_id)
    if not r.get("ok"):
        raise HTTPException(404, r.get("error", "ジョブ不明"))
    return {"status": "ok", **r}


@router.get("/api/live/remote-videos")
def api_live_remote_videos(group: str = ""):
    cfg = app_live.load_live_config()
    r = app_live.list_remote_videos(cfg["vps"], group)
    # 使用中マーク（いずれかのストリームの video / playlist に含まれるか）
    used = {}
    for s in cfg["streams"]:
        for p in [s.get("video")] + (s.get("playlist") or []):
            if p:
                used.setdefault(p, []).append(s["id"])
    videos = [{**v, "used_by": used.get(v["path"], [])} for v in r.get("videos", [])]
    return {"status": "ok" if r["ok"] else "error", "videos": videos}


class RemoteVideoDeleteRequest(BaseModel):
    path: str


@router.delete("/api/live/remote-videos")
def api_live_remote_video_delete(req: RemoteVideoDeleteRequest):
    cfg = app_live.load_live_config()
    # 使用中ガード
    for s in cfg["streams"]:
        if req.path == s.get("video") or req.path in (s.get("playlist") or []):
            raise HTTPException(400, f"この動画は配信「{s['id']}」で使用中です。先に設定から外してください")
    r = app_live.delete_remote_video(cfg["vps"], req.path)
    if not r["ok"]:
        raise HTTPException(400, f"削除失敗: {r['detail']}")
    return {"status": "ok"}


# ─── YouTube ライブ配信メタ（タイトル/説明/サムネ） ───

def _stream_token_path(s: dict) -> Optional[Path]:
    registry = _registry_by_id()
    reg = registry.get(s.get("registry_id") or s.get("group"))
    if not reg or not reg.get("folder"):
        return None
    return Path(reg["folder"]) / ".youtube_token.json"


@router.get("/api/live/broadcasts")
def api_live_broadcasts(stream_id: str):
    """ストリームの YouTube ライブ情報。キーで紐付く broadcast を先頭に、同チャンネルの他配信も返す。"""
    cfg = app_live.load_live_config()
    s = _get_stream_or_404(cfg, stream_id)
    token = _stream_token_path(s)
    if not token or not token.exists():
        return {"status": "unauthorized", "error": "YouTube 未認証（レジストリのチャンネルフォルダに .youtube_token.json が必要）", "broadcasts": []}
    import app_youtube
    res = app_youtube.list_live_broadcasts(token)
    if res.get("status") != "ok":
        return res
    # bound_stream_id → キーで該当 broadcast をマーク
    sres = app_youtube.list_my_live_streams(token)
    key_by_ytstream = {st["id"]: st["key"] for st in sres.get("streams", [])} if sres.get("status") == "ok" else {}
    items = []
    for b in res["broadcasts"]:
        b = {**b, "matches_stream": key_by_ytstream.get(b.get("bound_stream_id", "")) == s.get("stream_key", "")}
        items.append(b)
    items.sort(key=lambda b: (not b["matches_stream"], b.get("broadcast_status") != "active"))
    return {"status": "ok", "broadcasts": items}


class BroadcastUpdateRequest(BaseModel):
    stream_id: str
    video_id: str
    title: Optional[str] = None
    description: Optional[str] = None
    privacy: Optional[str] = None                     # public | unlisted | private
    contains_synthetic_media: Optional[bool] = None   # 「AIで作成」開示フラグ


@router.put("/api/live/broadcasts")
def api_live_broadcast_update(req: BroadcastUpdateRequest):
    cfg = app_live.load_live_config()
    s = _get_stream_or_404(cfg, req.stream_id)
    token = _stream_token_path(s)
    if not token or not token.exists():
        raise HTTPException(401, "YouTube 未認証（チャンネルフォルダに .youtube_token.json が必要）")
    if req.privacy is not None and req.privacy not in ("public", "unlisted", "private"):
        raise HTTPException(400, f"公開設定は public / unlisted / private のいずれか: {req.privacy}")
    import app_youtube
    res = app_youtube.update_live_video_meta(
        token, req.video_id, title=req.title, description=req.description,
        privacy=req.privacy, contains_synthetic_media=req.contains_synthetic_media)
    if res.get("status") != "ok":
        raise HTTPException(500, res.get("error", "更新失敗"))
    return res


class BroadcastSuggestRequest(BaseModel):
    stream_id: str
    current_title: str = ""
    current_description: str = ""


@router.post("/api/live/broadcasts/suggest")
def api_live_broadcast_suggest(req: BroadcastSuggestRequest):
    """配信のタイトル/説明文を LLM で提案する（保存はしない・UI の入力欄に反映するだけ）。"""
    cfg = app_live.load_live_config()
    s = _get_stream_or_404(cfg, req.stream_id)
    registry = _registry_by_id()
    reg = registry.get(s.get("registry_id") or s.get("group")) or {}
    ch_name = reg.get("name") or s.get("label") or s.get("group") or ""
    vids = [Path(p).stem for p in ([s.get("video")] + (s.get("playlist") or [])) if p]
    prompt = f"""あなたは YouTube の 24時間ライブ配信（BGM・音楽ラジオ系）のメタデータ担当です。
以下の情報から、このライブ配信にふさわしいタイトルと説明文を 1 案作ってください。

チャンネル名: {ch_name}
配信ラベル: {s.get('label') or s.get('id') or ''}
配信する動画ファイル名（曲調のヒント）: {', '.join(vids[:10]) or '不明'}
現在のタイトル: {req.current_title or '（未設定）'}
現在の説明文:
{req.current_description or '（未設定）'}

要件:
- 言語はチャンネル名・現在のタイトルに合わせる（日本語なら日本語、英語なら英語）
- タイトル: 24時間ライブと分かる表現（24/7 / Live / Radio 等）+ 音楽ジャンルや雰囲気。全角換算60文字以内
- 説明文: 2〜6行。配信内容 → 想定シーン（作業用・睡眠用など）→ 最後にハッシュタグ3〜5個
- 絵文字は使わない
- 出力は次の JSON だけを返す（コードフェンス・前置き・後書き禁止）:
{{"title": "...", "description": "..."}}"""
    from app_llm_runner import run_llm
    try:
        out = run_llm(prompt, timeout=240, label="live-meta-suggest")
    except Exception as e:
        raise HTTPException(500, f"LLM 実行失敗: {e}")
    m = re.search(r"\{.*\}", out or "", re.S)
    if not m:
        raise HTTPException(500, f"LLM 出力から JSON を抽出できませんでした: {(out or '')[:200]}")
    try:
        data = json.loads(m.group(0))
    except Exception:
        raise HTTPException(500, f"LLM 出力の JSON が壊れています: {m.group(0)[:200]}")
    title = str(data.get("title") or "").strip()
    description = str(data.get("description") or "").strip()
    if not title:
        raise HTTPException(500, "LLM がタイトルを返しませんでした")
    return {"status": "ok", "title": title, "description": description}


class ThumbnailSetRequest(BaseModel):
    stream_id: str
    video_id: str
    image_path: str


@router.post("/api/live/thumbnail")
def api_live_thumbnail(req: ThumbnailSetRequest):
    """ライブ配信（video_id）のサムネイルをローカル画像で設定（quota 50 unit）。"""
    cfg = app_live.load_live_config()
    s = _get_stream_or_404(cfg, req.stream_id)
    token = _stream_token_path(s)
    if not token or not token.exists():
        raise HTTPException(401, "YouTube 未認証（チャンネルフォルダに .youtube_token.json が必要）")
    p = Path(req.image_path).expanduser()
    if not p.exists():
        raise HTTPException(400, f"画像が見つからない: {p}")
    import app_youtube
    res = app_youtube.set_video_thumbnail(token, req.video_id, str(p))
    if res.get("status") != "ok":
        raise HTTPException(500, res.get("error", "サムネイル設定失敗"))
    return res
