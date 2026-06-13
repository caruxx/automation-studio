#!/usr/bin/env python3
"""VPS 経由 YouTube ライブ配信の制御モジュール v2（マルチストリーム対応）。

ダッシュボード(Mac) → SSH → VPS(Ubuntu) の橋渡し:
  - live_config.json (共有ドライブ config/・PC 間共有) の読み書き（VPS 接続情報 + 配信ストリーム設定）
  - SSH 鍵の生成・パスワードによる初回登録(expect)・接続テスト
  - VPS 初期セットアップ(ffmpeg/screen 導入 + vps_live/ スクリプト配置)
  - ストリーム env の push、screen 配信の start/stop/swap、状態取得
  - ローカル動画の scp アップロード（ジョブ管理）、リモート動画の削除

データモデル v2:
  1 つの YouTube チャンネル（= group / プレフィックス例 orzz, sk）の下に複数の配信ストリーム。
  - stream.id   … 配信単位の ID（例 orzz_1）。screen セッション ytlive_<id> / env <id>.env に対応
  - stream.group… 動画プール videos/<group>/ と YouTube チャンネル（registry）への紐付け
  旧 v1 の "channels" キーは読み込み時に自動で "streams" へマイグレートする。

依存: 標準ライブラリのみ。ssh/scp/ssh-keygen/expect は macOS 同梱コマンドを使用。
セキュリティ: パスワードは鍵登録時に env 経由で expect に渡すのみで、どこにも保存しない。
"""
import json
import re
import shlex
import subprocess
import threading
import time
import uuid
from pathlib import Path

try:
    from _app_config import (
        resolve_config_dir as _resolve_config_dir,
        resolve_shared_config_dir as _resolve_shared_config_dir,
    )
    CONFIG_DIR = _resolve_config_dir()
    SHARED_CONFIG_DIR = _resolve_shared_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"
    SHARED_CONFIG_DIR = CONFIG_DIR

# ライブ設定は PC 間・app_id 間で共有（channels.json と同じ共有ドライブ config/）。
# VPS・配信ストリームは全 PC から同一のものを操作するため、per-PC 置き場だと分裂する。
LIVE_CONFIG = SHARED_CONFIG_DIR / "live_config.json"
SCRIPTS_SRC = Path(__file__).resolve().parent / "vps_live"
REMOTE_SCRIPTS = ["stream.sh", "status.py", "ytlive.sh"]

CH_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

DEFAULT_VPS = {
    "name": "",
    "host": "",
    "port": 22,
    "user": "root",
    "ssh_key": "~/.ssh/ytlive_ed25519",
    "remote_dir": "/opt/ytlive",
}

DEFAULT_STREAM = {
    "id": "",             # 配信ID（例 orzz_1）
    "group": "",          # プレフィックス＝動画プール videos/<group>/（例 orzz）
    "label": "",          # 表示名（例 orzz #1）
    "registry_id": "",    # config/channels.json の id（YouTube token / channel_id の参照用）
    "stream_key": "",
    "video": "",          # 単発ループ用動画（VPS 上のパス）。playlist があればそちら優先
    "playlist": [],       # ローテーション動画リスト（VPS 上のパス配列）
    "rotate_hours": 0.0,  # >0 なら N 時間ごとに playlist の次の動画へ差し替え（配信は継続）
    "max_hours": 0.0,     # >0 なら配信開始から N 時間で自動打ち切り
    "mode": "copy",       # copy | reencode
    "vbitrate": "4500k",
    "abitrate": "160k",
    "scale": "",          # 例 1280:720（reencode 時のみ）
    "fps": 30,
    "rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
}


# ─── 設定 ───

def _migrate_v1(cfg: dict) -> dict:
    """旧 v1（channels キー）→ v2（streams キー）変換。group=registry_id（無ければ id）。"""
    if "streams" in cfg or "channels" not in cfg:
        return cfg
    streams = []
    for ch in cfg.get("channels") or []:
        s = {**DEFAULT_STREAM, **ch}
        s["group"] = ch.get("registry_id") or ch.get("id") or ""
        streams.append(s)
    cfg = {k: v for k, v in cfg.items() if k != "channels"}
    cfg["streams"] = streams
    return cfg


def _legacy_live_config_paths() -> list:
    """旧ローカル置き場（~/.config/<app_id>/live_config.json）の候補を mtime 降順で返す。

    以前は per-PC かつ per-app_id（チャンネル切替で分裂）だったため、全 app_id を走査する。
    """
    found = []
    base = Path.home() / ".config"
    try:
        for p in base.glob("*/live_config.json"):
            try:
                if p.resolve() != LIVE_CONFIG.resolve():
                    found.append((p.stat().st_mtime, p))
            except OSError:
                continue
    except Exception:
        pass
    return [p for _, p in sorted(found, key=lambda t: t[0], reverse=True)]


def load_live_config() -> dict:
    cfg = {}
    migrated_from_legacy = False
    if LIVE_CONFIG.exists():
        try:
            cfg = json.loads(LIVE_CONFIG.read_text(encoding="utf-8"))
        except Exception:
            cfg = {}
    if not cfg:
        # 共有側に未作成 → 旧ローカル設定（mtime 最新）から一度だけ移行。旧ファイルは残す。
        for p in _legacy_live_config_paths():
            try:
                legacy = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                continue
            if legacy and (legacy.get("streams") or legacy.get("channels") or (legacy.get("vps") or {}).get("host")):
                cfg = legacy
                migrated_from_legacy = True
                break
    migrated = ("channels" in cfg and "streams" not in cfg) or migrated_from_legacy
    cfg = _migrate_v1(cfg)
    vps = {**DEFAULT_VPS, **(cfg.get("vps") or {})}
    streams = []
    for s in cfg.get("streams") or []:
        merged = {**DEFAULT_STREAM, **s}
        if not merged.get("group"):
            merged["group"] = merged.get("registry_id") or merged.get("id") or ""
        if not isinstance(merged.get("playlist"), list):
            merged["playlist"] = []
        streams.append(merged)
    out = {"vps": vps, "streams": streams}
    if migrated:
        save_live_config(out)  # 一度だけ書き戻す（v1→v2 変換 / 旧ローカル→共有ドライブ移行）
    return out


def save_live_config(cfg: dict):
    LIVE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    LIVE_CONFIG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        LIVE_CONFIG.chmod(0o600)  # stream key を含むため
    except Exception:
        pass


def get_stream(cfg: dict, stream_id: str) -> dict:
    for s in cfg.get("streams", []):
        if s.get("id") == stream_id:
            return s
    raise KeyError(f"配信ストリームが未登録: {stream_id}")


# ─── SSH 基盤 ───

def _key_path(vps: dict) -> Path:
    return Path(vps.get("ssh_key") or DEFAULT_VPS["ssh_key"]).expanduser()


def _ssh_base(vps: dict, batch: bool = True) -> list:
    args = [
        "ssh",
        "-i", str(_key_path(vps)),
        "-p", str(vps.get("port") or 22),
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=15",
    ]
    if batch:
        args += ["-o", "BatchMode=yes"]
    args.append(f"{vps.get('user') or 'root'}@{vps.get('host') or ''}")
    return args


def run_remote(vps: dict, command: str, *, input_text: str = None, timeout: int = 40) -> dict:
    """VPS 上でコマンドを実行。secrets は input_text(stdin) 経由で渡す（argv に載せない）。"""
    if not vps.get("host"):
        return {"rc": -1, "out": "", "err": "VPS ホストが未設定"}
    try:
        p = subprocess.run(
            _ssh_base(vps) + [command],
            input=input_text,
            capture_output=True, text=True, timeout=timeout,
        )
        return {"rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"rc": -1, "out": "", "err": f"SSH タイムアウト ({timeout}s)"}
    except Exception as e:
        return {"rc": -1, "out": "", "err": f"SSH 実行失敗: {e}"}


def test_connection(vps: dict) -> dict:
    r = run_remote(vps, "echo __OK__ && uname -sr && hostname", timeout=15)
    ok = r["rc"] == 0 and "__OK__" in r["out"]
    detail = r["out"].replace("__OK__", "").strip() if ok else (r["err"] or r["out"])
    return {"ok": ok, "detail": detail}


# ─── 初期設定（鍵生成・登録） ───

def ensure_local_key(vps: dict) -> dict:
    """SSH 鍵が無ければ生成し、公開鍵文字列を返す。"""
    key = _key_path(vps)
    pub = key.with_suffix(".pub")
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        p = subprocess.run(
            ["ssh-keygen", "-t", "ed25519", "-f", str(key), "-N", "", "-C", "ytlive-dashboard"],
            capture_output=True, text=True,
        )
        if p.returncode != 0:
            return {"ok": False, "detail": f"鍵生成失敗: {p.stderr.strip()}"}
    if not pub.exists():
        return {"ok": False, "detail": f"公開鍵が見つからない: {pub}"}
    return {"ok": True, "pubkey": pub.read_text().strip(), "key_path": str(key)}


def register_key_with_password(vps: dict, password: str) -> dict:
    """パスワード認証で公開鍵を VPS に登録（ssh-copy-id + expect）。パスワードは保存しない。"""
    key = _key_path(vps)
    pub = key.with_suffix(".pub")
    if not pub.exists():
        return {"ok": False, "detail": "公開鍵が無い（先に鍵生成）"}
    script = f"""
set timeout 45
spawn ssh-copy-id -i {shlex.quote(str(pub))} -p {int(vps.get('port') or 22)} -o StrictHostKeyChecking=accept-new -o NumberOfPasswordPrompts=1 {shlex.quote((vps.get('user') or 'root') + '@' + (vps.get('host') or ''))}
expect {{
  -nocase "password:" {{ send -- "$env(YTLIVE_PW)\\r"; exp_continue }}
  timeout {{ exit 3 }}
  eof
}}
catch wait result
exit [lindex $result 3]
"""
    import os
    env = dict(os.environ)
    env["YTLIVE_PW"] = password
    try:
        p = subprocess.run(["expect", "-c", script], capture_output=True, text=True, timeout=60, env=env)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "鍵登録タイムアウト"}
    if p.returncode != 0:
        tail = (p.stdout or "").strip().splitlines()[-3:]
        return {"ok": False, "detail": "鍵登録失敗（パスワード誤り or root パスワードログイン禁止）: " + " / ".join(tail)}
    return {"ok": True, "detail": "公開鍵を登録した"}


# ─── VPS セットアップ ───

def bootstrap_vps(vps: dict) -> dict:
    """ffmpeg/screen 導入 + ディレクトリ作成（冪等）。"""
    base = vps.get("remote_dir") or "/opt/ytlive"
    qbase = shlex.quote(base)
    script = (
        "set -e\n"
        "export DEBIAN_FRONTEND=noninteractive\n"
        "if ! command -v ffmpeg >/dev/null || ! command -v screen >/dev/null; then\n"
        "  apt-get update -qq\n"
        "  apt-get install -y -qq --no-install-recommends ffmpeg screen >/dev/null\n"
        "fi\n"
        f"mkdir -p {qbase}/videos {qbase}/channels {qbase}/scripts {qbase}/logs {qbase}/status\n"
        f"chmod 700 {qbase}/channels\n"
        "echo __BOOTSTRAP_OK__\n"
        "ffmpeg -version | head -1\n"
    )
    r = run_remote(vps, "bash -s", input_text=script, timeout=300)
    ok = r["rc"] == 0 and "__BOOTSTRAP_OK__" in r["out"]
    return {"ok": ok, "detail": (r["out"].replace("__BOOTSTRAP_OK__", "").strip() or r["err"]) if ok else (r["err"] or r["out"])}


def push_scripts(vps: dict) -> dict:
    """vps_live/ の各スクリプトを VPS に配置（stdin 経由・冪等）。"""
    base = vps.get("remote_dir") or "/opt/ytlive"
    results = []
    for name in REMOTE_SCRIPTS:
        src = SCRIPTS_SRC / name
        if not src.exists():
            results.append(f"{name}: ローカルに無い")
            continue
        dst = f"{base}/scripts/{name}"
        r = run_remote(
            vps,
            f"cat > {shlex.quote(dst)} && chmod +x {shlex.quote(dst)} && echo PUSHED",
            input_text=src.read_text(encoding="utf-8"),
            timeout=30,
        )
        results.append(f"{name}: {'OK' if r['rc'] == 0 else (r['err'] or 'NG')}")
        if r["rc"] != 0:
            return {"ok": False, "detail": " / ".join(results)}
    return {"ok": True, "detail": " / ".join(results)}


def build_env_content(s: dict) -> str:
    def q(v):
        return "'" + str(v).replace("'", "'\"'\"'") + "'"
    playlist = ":".join(str(p) for p in (s.get("playlist") or []) if p)
    lines = [
        "# ytlive stream config (managed by dashboard)",
        f"GROUP={q(s.get('group', ''))}",
        f"STREAM_KEY={q(s.get('stream_key', ''))}",
        f"VIDEO={q(s.get('video', ''))}",
        f"PLAYLIST={q(playlist)}",
        f"ROTATE_SECONDS={int(float(s.get('rotate_hours') or 0) * 3600)}",
        f"MAX_SECONDS={int(float(s.get('max_hours') or 0) * 3600)}",
        f"MODE={q(s.get('mode') or 'copy')}",
        f"RTMP_URL={q(s.get('rtmp_url') or 'rtmp://a.rtmp.youtube.com/live2')}",
        f"VBITRATE={q(s.get('vbitrate') or '4500k')}",
        f"ABITRATE={q(s.get('abitrate') or '160k')}",
        f"FPS={q(s.get('fps') or 30)}",
    ]
    if s.get("scale"):
        lines.append(f"SCALE={q(s['scale'])}")
    return "\n".join(lines) + "\n"


def push_stream_env(vps: dict, s: dict) -> dict:
    sid = s.get("id") or ""
    if not CH_ID_RE.match(sid):
        return {"ok": False, "detail": f"不正な配信ID: {sid}"}
    base = vps.get("remote_dir") or "/opt/ytlive"
    dst = f"{base}/channels/{sid}.env"
    r = run_remote(
        vps,
        f"mkdir -p {shlex.quote(base + '/channels')} && cat > {shlex.quote(dst)} && chmod 600 {shlex.quote(dst)} && echo PUSHED",
        input_text=build_env_content(s),
        timeout=20,
    )
    return {"ok": r["rc"] == 0, "detail": r["err"] or r["out"]}


def remove_stream_env(vps: dict, stream_id: str) -> dict:
    if not CH_ID_RE.match(stream_id or ""):
        return {"ok": False, "detail": "不正な配信ID"}
    base = vps.get("remote_dir") or "/opt/ytlive"
    targets = " ".join(shlex.quote(p) for p in (
        f"{base}/channels/{stream_id}.env",
        f"{base}/status/{stream_id}.props",
        f"{base}/status/{stream_id}.stop",
        f"{base}/status/{stream_id}.idx",
    ))
    r = run_remote(vps, f"rm -f {targets}", timeout=15)
    return {"ok": r["rc"] == 0, "detail": r["err"] or "removed"}


# ─── 配信制御・状態 ───

def _ytlive(vps: dict, cmd: str, stream_id: str, extra: str = "", timeout: int = 30) -> dict:
    if not CH_ID_RE.match(stream_id or ""):
        return {"ok": False, "detail": f"不正な配信ID: {stream_id}"}
    base = vps.get("remote_dir") or "/opt/ytlive"
    cmdline = f"bash {shlex.quote(base + '/scripts/ytlive.sh')} {cmd} {shlex.quote(stream_id)}"
    if extra:
        cmdline += f" {shlex.quote(extra)}"
    r = run_remote(vps, cmdline, timeout=timeout)
    return {"ok": r["rc"] == 0, "detail": r["out"] or r["err"]}


def start_stream(vps: dict, stream_id: str) -> dict:
    return _ytlive(vps, "start", stream_id)


def stop_stream(vps: dict, stream_id: str) -> dict:
    return _ytlive(vps, "stop", stream_id, timeout=40)


def restart_stream(vps: dict, stream_id: str) -> dict:
    return _ytlive(vps, "restart", stream_id, timeout=60)


def swap_stream(vps: dict, stream_id: str, to_next: bool = False) -> dict:
    """配信を止めずに動画を差し替える（ffmpeg のみ再起動・同一ライブ継続）。"""
    return _ytlive(vps, "swap", stream_id, extra="next" if to_next else "", timeout=30)


def fetch_status(vps: dict) -> dict:
    base = vps.get("remote_dir") or "/opt/ytlive"
    r = run_remote(vps, f"python3 {shlex.quote(base + '/scripts/status.py')}", timeout=30)
    if r["rc"] != 0:
        return {"ok": False, "error": r["err"] or r["out"] or "status 取得失敗"}
    try:
        return json.loads(r["out"])
    except Exception as e:
        return {"ok": False, "error": f"status JSON 解析失敗: {e}", "raw": r["out"][:500]}


def fetch_log(vps: dict, stream_id: str, lines: int = 80) -> dict:
    if not CH_ID_RE.match(stream_id or ""):
        return {"ok": False, "log": "", "error": "不正な配信ID"}
    base = vps.get("remote_dir") or "/opt/ytlive"
    lines = max(1, min(int(lines or 80), 1000))
    r = run_remote(vps, f"tail -n {lines} {shlex.quote(f'{base}/logs/{stream_id}.log')} 2>/dev/null || true", timeout=20)
    return {"ok": r["rc"] == 0, "log": r["out"], "error": r["err"]}


def list_remote_videos(vps: dict, group: str = "") -> dict:
    """videos/（group 指定時は videos/<group>/）配下の動画一覧（サイズ付き）。"""
    base = vps.get("remote_dir") or "/opt/ytlive"
    sub = f"videos/{group}" if group and CH_ID_RE.match(group) else "videos"
    target = f"{base}/{sub}"
    r = run_remote(
        vps,
        f"find {shlex.quote(target)} -maxdepth 2 -type f \\( -name '*.mp4' -o -name '*.mov' -o -name '*.mkv' \\) -printf '%s\\t%p\\n' 2>/dev/null | sort -k2",
        timeout=25,
    )
    videos = []
    for line in (r["out"] or "").splitlines():
        try:
            size_s, path = line.split("\t", 1)
            videos.append({"path": path, "size_mb": round(int(size_s) / 2**20, 1)})
        except ValueError:
            continue
    return {"ok": r["rc"] == 0, "videos": videos}


def delete_remote_video(vps: dict, path: str) -> dict:
    """videos/ 配下の動画を削除（パスを正規化して配下チェック）。"""
    base = (vps.get("remote_dir") or "/opt/ytlive").rstrip("/")
    videos_root = f"{base}/videos/"
    # リモートパスはローカルで resolve できないため文字列検査（.. 禁止 + 配下チェック）
    if ".." in path or not path.startswith(videos_root):
        return {"ok": False, "detail": f"videos/ 配下のパスのみ削除可能: {path}"}
    r = run_remote(vps, f"rm -f {shlex.quote(path)} && echo DELETED", timeout=20)
    return {"ok": r["rc"] == 0 and "DELETED" in r["out"], "detail": r["err"] or r["out"]}


# ─── 動画アップロード（再開可能ジョブ・並行可） ───
#
# scp 一発ではなく「リモートの現在サイズを見て tail -c で続きから append」する方式。
# - 途中で切れても自動リトライ（進捗があれば試行カウントをリセット → 長時間の不安定回線でも完走）
# - 完了判定はサイズ完全一致のみ（早期 ok 誤判定なし）
# - ジョブはサーバー側スレッドで走るため、ブラウザのページ遷移では中断されない

_UPLOAD_JOBS: dict = {}
_UPLOAD_LOCK = threading.Lock()
_UPLOAD_MAX_STALL_ATTEMPTS = 8   # 「1バイトも進まない」試行がこの回数続いたら失敗
_UPLOAD_JOB_TTL = 6 * 3600       # 完了/失敗ジョブを一覧に残す時間


def sanitize_dest_name(name: str) -> str:
    name = Path(name or "video.mp4").name
    name = name.replace(" ", "_").replace("/", "_")
    return re.sub(r"[^\w.\-（）()【】぀-ヿ一-鿿]", "_", name)


def _remote_file_size(vps: dict, path: str) -> int:
    r = run_remote(vps, f"stat -c%s {shlex.quote(path)} 2>/dev/null || echo 0", timeout=15)
    try:
        return int((r["out"] or "0").strip().splitlines()[-1])
    except Exception:
        return 0


def _stable_remote_size(vps: dict, path: str) -> int:
    """書き込み直後はリモート側のフラッシュが終わっていないことがある → サイズが落ち着くまで待つ。"""
    s1 = _remote_file_size(vps, path)
    for _ in range(3):
        time.sleep(1.5)
        s2 = _remote_file_size(vps, path)
        if s2 == s1:
            return s2
        s1 = s2
    return s1


def _upload_worker(vps: dict, job: dict):
    src, dest, total = job["local"], job["remote"], job["local_size"]
    stall = 0
    while not job.get("cancel"):
        size = _stable_remote_size(vps, dest)
        if size > total:
            # 同名の別ファイルが居る等 → 作り直し
            run_remote(vps, f"rm -f {shlex.quote(dest)}", timeout=15)
            size = 0
        job["last_remote_size"] = size
        if size == total:
            job.update(state="done", rc=0, stderr="")
            return
        if stall >= _UPLOAD_MAX_STALL_ATTEMPTS:
            job.update(state="failed", rc=1)
            if not job["stderr"]:
                job["stderr"] = f"転送が進まないため中断（{size}/{total} bytes・{stall} 回連続で進捗なし）"
            return
        job["attempts"] += 1
        # 続きから転送（tail -c +N は 1 オリジン）。リモートは dd の絶対位置書き込みなので
        # 前試行の残りバイトが遅れて届いても重複破損しない。成功判定はサイズ一致のみ
        local_sh = f"tail -c +{size + 1} {shlex.quote(src)}"
        remote_sh = (f"dd of={shlex.quote(dest)} bs=64k seek={size} "
                     f"oflag=seek_bytes conv=notrunc status=none")
        ssh_sh = " ".join(shlex.quote(a) for a in _ssh_base(vps)) + " " + shlex.quote(remote_sh)
        if job.get("cancel"):  # stat 中に中止された場合に次の試行を始めない（kill 空振り対策）
            break
        try:
            proc = subprocess.Popen(["/bin/sh", "-c", f"{local_sh} | {ssh_sh}"],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            job["proc"] = proc
            if job.get("cancel"):  # Popen 直前に中止 → kill が proc=None で空振りした穴を塞ぐ
                proc.kill()
            _, err = proc.communicate()
        except Exception as e:
            err = str(e)
        finally:
            job["proc"] = None
        if job.get("cancel"):
            break
        new_size = _remote_file_size(vps, dest)
        job["last_remote_size"] = new_size
        if new_size == total:
            job.update(state="done", rc=0, stderr="")
            return
        job["stderr"] = ((err or "").strip() or f"転送が途中で終了（{new_size}/{total} bytes）")[-500:]
        stall = 0 if new_size > size else stall + 1  # 進捗があれば仕切り直し
        time.sleep(min(30, 3 * stall + 2))
    job.update(state="cancelled", rc=130)


def _prune_jobs():
    now = time.time()
    with _UPLOAD_LOCK:
        for jid in [j["id"] for j in _UPLOAD_JOBS.values()
                    if j["state"] != "running" and now - j["started"] > _UPLOAD_JOB_TTL]:
            _UPLOAD_JOBS.pop(jid, None)


def start_upload(vps: dict, group: str, local_path: str, dest_name: str = "") -> dict:
    if not CH_ID_RE.match(group or ""):
        return {"ok": False, "error": f"不正なグループID: {group}"}
    src = Path(local_path).expanduser()
    if not src.exists() or not src.is_file():
        return {"ok": False, "error": f"ローカル動画が見つからない: {src}"}
    base = vps.get("remote_dir") or "/opt/ytlive"
    dest = f"{base}/videos/{group}/{sanitize_dest_name(dest_name or src.name)}"
    _prune_jobs()
    with _UPLOAD_LOCK:
        # 同じ宛先への実行中ジョブがあれば使い回す（二重クリック・同時 append 事故の防止）
        for j in _UPLOAD_JOBS.values():
            if j["remote"] == dest and j["state"] == "running":
                return {"ok": True, "job_id": j["id"], "remote": dest, "dedup": True}
    mk = run_remote(vps, f"mkdir -p {shlex.quote(f'{base}/videos/{group}')}", timeout=15)
    if mk["rc"] != 0:
        return {"ok": False, "error": f"リモートディレクトリ作成失敗: {mk['err']}"}
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id, "group": group,
        "local": str(src), "remote": dest,
        "local_size": src.stat().st_size, "last_remote_size": 0,
        "state": "running", "rc": None, "attempts": 0,
        "proc": None, "cancel": False,
        "started": time.time(), "stderr": "",
    }
    with _UPLOAD_LOCK:
        _UPLOAD_JOBS[job_id] = job
    threading.Thread(target=_upload_worker, args=(vps, job), daemon=True,
                     name=f"live-upload-{job_id}").start()
    return {"ok": True, "job_id": job_id, "remote": dest}


def _job_view(job: dict, remote_size: int = None) -> dict:
    total = job["local_size"] or 1
    rs = job["local_size"] if job["state"] == "done" else (
        remote_size if remote_size is not None else job.get("last_remote_size", 0))
    return {
        "job_id": job["id"], "group": job["group"],
        "name": Path(job["local"]).name, "remote": job["remote"], "local": job["local"],
        "state": job["state"], "running": job["state"] == "running", "rc": job["rc"],
        "pct": min(round(rs / total * 100, 1), 100.0),
        "remote_size": rs, "local_size": job["local_size"],
        "attempts": job["attempts"], "elapsed_sec": int(time.time() - job["started"]),
        "error": job["stderr"] if job["state"] == "failed" else "",
    }


def upload_status(vps: dict, job_id: str) -> dict:
    with _UPLOAD_LOCK:
        job = _UPLOAD_JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": "ジョブが見つからない"}
    remote_size = None
    if job["state"] == "running":
        remote_size = _remote_file_size(vps, job["remote"])
        job["last_remote_size"] = remote_size
    return {"ok": True, **_job_view(job, remote_size)}


def list_uploads(vps: dict, group: str = "") -> dict:
    """グループの全アップロードジョブ（実行中+直近完了/失敗）。実行中分は 1 回の SSH でまとめて進捗取得。"""
    _prune_jobs()
    with _UPLOAD_LOCK:
        jobs = sorted([j for j in _UPLOAD_JOBS.values() if not group or j["group"] == group],
                      key=lambda j: j["started"])
    running = [j for j in jobs if j["state"] == "running"]
    if running and vps.get("host"):
        loop = " ; ".join(
            f'printf "%s\\t" {shlex.quote(j["remote"])}; stat -c%s {shlex.quote(j["remote"])} 2>/dev/null || echo 0'
            for j in running)
        r = run_remote(vps, loop, timeout=20)
        sizes = {}
        for line in (r["out"] or "").splitlines():
            if "\t" in line:
                pth, _, sz = line.rpartition("\t")
                try:
                    sizes[pth] = int(sz)
                except ValueError:
                    pass
        for j in running:
            if j["remote"] in sizes:
                j["last_remote_size"] = sizes[j["remote"]]
    return {"ok": True, "jobs": [_job_view(j) for j in jobs]}


def clear_finished_uploads(group: str = "") -> dict:
    """完了/失敗/中止ジョブを一覧表示から消す（実行中は対象外・TTL を待たず手動掃除）。"""
    with _UPLOAD_LOCK:
        ids = [j["id"] for j in _UPLOAD_JOBS.values()
               if j["state"] != "running" and (not group or j["group"] == group)]
        for jid in ids:
            _UPLOAD_JOBS.pop(jid, None)
    return {"ok": True, "cleared": len(ids)}


def cancel_upload(job_id: str) -> dict:
    with _UPLOAD_LOCK:
        job = _UPLOAD_JOBS.get(job_id)
    if not job:
        return {"ok": False, "error": "ジョブが見つからない"}
    if job["state"] != "running":
        return {"ok": True, "state": job["state"]}
    job["cancel"] = True
    # worker が stat / バックオフ中だと proc が None のことがある → 短時間リトライで確実に殺す
    for _ in range(10):
        p = job.get("proc")
        if p:
            try:
                p.kill()
            except Exception:
                pass
        if job["state"] != "running":
            break
        time.sleep(0.3)
    return {"ok": True, "state": job["state"]}
