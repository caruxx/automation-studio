#!/usr/bin/env python3
"""VPS 上の全配信ストリーム + ホストの稼働状況を 1 つの JSON で stdout に出す。

ダッシュボード(Mac)が SSH 経由で `python3 <BASE>/scripts/status.py` を呼ぶ前提。
依存: 標準ライブラリのみ（Ubuntu 標準の python3 で動く）。
v2: プレイリスト/ローテーション/自動打切りの状態、グループ別ディスク使用量を追加。
"""
import calendar
import json
import os
import re
import subprocess
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent


def read_props(p: Path) -> dict:
    d = {}
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except Exception:
        pass
    return d


def read_env(p: Path) -> dict:
    """シェル env ファイルの簡易パース（KEY='value' / KEY=value）。"""
    d = {}
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
                v = v[1:-1]
            d[k.strip()] = v
    except Exception:
        pass
    return d


def pid_alive(pid_s: str, must_contain: str = "") -> bool:
    try:
        pid = int(pid_s)
        os.kill(pid, 0)
        if must_contain:
            cmd = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace")
            return must_contain in cmd
        return True
    except Exception:
        return False


def screen_sessions() -> set:
    try:
        out = subprocess.run(["screen", "-ls"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        out = ""
    return set(re.findall(r"\d+\.(ytlive_[A-Za-z0-9_-]+)", out))


def default_iface() -> str:
    try:
        out = subprocess.run(["ip", "route", "get", "1.1.1.1"], capture_output=True, text=True, timeout=5).stdout
        m = re.search(r"\bdev\s+(\S+)", out)
        if m:
            return m.group(1)
    except Exception:
        pass
    for cand in ("eth0", "ens3", "enp1s0"):
        if Path(f"/sys/class/net/{cand}").exists():
            return cand
    return "lo"


def tx_bytes(iface: str) -> int:
    try:
        return int(Path(f"/sys/class/net/{iface}/statistics/tx_bytes").read_text())
    except Exception:
        return 0


def dir_size(p: Path) -> int:
    total = 0
    try:
        for f in p.rglob("*"):
            try:
                if f.is_file():
                    total += f.stat().st_size
            except OSError:
                continue
    except Exception:
        pass
    return total


def group_disk() -> dict:
    """videos/<group>/ ごとの使用バイト数。"""
    out = {}
    vdir = BASE / "videos"
    if vdir.exists():
        for d in sorted(vdir.iterdir()):
            if d.is_dir():
                out[d.name] = dir_size(d)
    return out


def host_metrics() -> dict:
    load = [0.0, 0.0, 0.0]
    try:
        load = [float(x) for x in Path("/proc/loadavg").read_text().split()[:3]]
    except Exception:
        pass
    mem = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            mem[k.strip()] = int(v.strip().split()[0])  # kB
    except Exception:
        pass
    try:
        st = os.statvfs(str(BASE))
        disk_total = st.f_blocks * st.f_frsize / 2**30
        disk_used = (st.f_blocks - st.f_bfree) * st.f_frsize / 2**30
        disk_free = st.f_bavail * st.f_frsize / 2**30
    except Exception:
        disk_total = disk_used = disk_free = 0
    iface = default_iface()
    t1 = tx_bytes(iface)
    time.sleep(1)
    t2 = tx_bytes(iface)
    return {
        "load": load,
        "nproc": os.cpu_count() or 1,
        "mem_used_mb": (mem.get("MemTotal", 0) - mem.get("MemAvailable", 0)) // 1024,
        "mem_total_mb": mem.get("MemTotal", 0) // 1024,
        "disk_used_gb": round(disk_used, 1),
        "disk_total_gb": round(disk_total, 1),
        "disk_free_gb": round(disk_free, 1),
        "tx_mbps": round(max(0, t2 - t1) * 8 / 1e6, 2),
        "iface": iface,
        "ffmpeg_installed": subprocess.run(["which", "ffmpeg"], capture_output=True).returncode == 0,
        "group_disk": group_disk(),
    }


def _to_int(s, default=0):
    try:
        return int(str(s).strip())
    except Exception:
        return default


def stream_status(envf: Path, sessions: set, now: int) -> dict:
    ch = envf.stem
    props = read_props(BASE / "status" / f"{ch}.props")
    env = read_env(envf)
    ffpid = props.get("ffmpeg_pid") or ""
    ff_alive = pid_alive(ffpid, "ffmpeg") if ffpid else False
    session_up = f"ytlive_{ch}" in sessions
    running = session_up and ff_alive
    uptime_sec = None
    if running and props.get("started_at"):
        try:
            t0 = calendar.timegm(time.strptime(props["started_at"], "%Y-%m-%dT%H:%M:%SZ"))
            uptime_sec = max(0, now - t0)
        except Exception:
            pass
    playlist = [p for p in (env.get("PLAYLIST") or "").split(":") if p]
    cur_video = props.get("video") or env.get("VIDEO", "")
    max_until = _to_int(props.get("max_until"), 0)
    remaining = max(0, max_until - now) if (max_until and session_up) else None
    return {
        "id": ch,
        "group": env.get("GROUP", ""),
        "running": running,
        "session_up": session_up,
        "ffmpeg_alive": ff_alive,
        "uptime_sec": uptime_sec,
        "restarts": _to_int(props.get("restarts")),
        "last_exit": props.get("last_exit", ""),
        "video": cur_video,
        "video_exists": bool(cur_video) and Path(cur_video).exists(),
        "configured_video": env.get("VIDEO", ""),
        "playlist": playlist,
        "playlist_idx": _to_int(props.get("playlist_idx")),
        "mode": env.get("MODE", "copy"),
        "has_stream_key": bool(env.get("STREAM_KEY")),
        "rotate_seconds": _to_int(env.get("ROTATE_SECONDS")),
        "max_seconds": _to_int(env.get("MAX_SECONDS")),
        "max_remaining_sec": remaining,
        "stopped_by_user": (BASE / "status" / f"{ch}.stop").exists(),
        "updated_at": props.get("updated_at", ""),
    }


def main():
    now = int(time.time())
    sessions = screen_sessions()
    streams = []
    ch_dir = BASE / "channels"
    if ch_dir.exists():
        for envf in sorted(ch_dir.glob("*.env")):
            streams.append(stream_status(envf, sessions, now))
    print(json.dumps({
        "ok": True,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "base": str(BASE),
        "host": host_metrics(),
        "channels": streams,  # 互換キー名（中身はストリーム単位）
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
