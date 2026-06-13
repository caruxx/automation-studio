#!/usr/bin/env bash
# 配信の起動/停止/再起動/差し替え（screen セッション管理）
# usage: ytlive.sh start|stop|restart <stream_id>
#        ytlive.sh swap <stream_id> [next]
#   swap      … ffmpeg のみ終了 → wrapper が最新 env で数秒後に再接続（配信は同一ライブのまま継続）
#   swap next … プレイリストの巡回位置を +1 してから差し替え（次の動画へ）
set -u
CMD="${1:?usage: ytlive.sh start|stop|restart|swap <stream_id>}"
CH="${2:?stream id required}"
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SES="ytlive_$CH"
PROPS="$BASE/status/$CH.props"

session_up(){ screen -ls 2>/dev/null | grep -q "[0-9]\.$SES[[:space:]]"; }

ffmpeg_pid(){
  local pid
  pid="$(sed -n 's/^ffmpeg_pid=//p' "$PROPS" 2>/dev/null | head -1)"
  # pid 取り違え防止: cmdline に ffmpeg を含む場合のみ採用（timeout 経由でも cmdline に含まれる）
  if [ -n "$pid" ] && grep -aq ffmpeg "/proc/$pid/cmdline" 2>/dev/null; then
    echo "$pid"
  fi
}

case "$CMD" in
  start)
    rm -f "$BASE/status/$CH.stop"
    if session_up; then echo "already_running"; exit 0; fi
    screen -dmS "$SES" bash "$BASE/scripts/stream.sh" "$CH"
    sleep 1
    if session_up; then echo "started"; else echo "start_failed"; exit 1; fi
    ;;
  stop)
    touch "$BASE/status/$CH.stop"
    PID="$(ffmpeg_pid)"
    [ -n "$PID" ] && kill "$PID" 2>/dev/null
    for _ in $(seq 1 10); do session_up || break; sleep 1; done
    session_up && screen -S "$SES" -X quit 2>/dev/null
    echo "stopped"
    ;;
  restart)
    "$0" stop "$CH" >/dev/null 2>&1
    sleep 1
    "$0" start "$CH"
    ;;
  swap)
    if ! session_up; then echo "not_running"; exit 1; fi
    if [ "${3:-}" = "next" ]; then
      IDXF="$BASE/status/$CH.idx"
      CUR="$(cat "$IDXF" 2>/dev/null || echo 0)"
      case "$CUR" in (*[!0-9]*|"") CUR=0;; esac
      echo $((CUR + 1)) > "$IDXF"
    fi
    PID="$(ffmpeg_pid)"
    if [ -n "$PID" ]; then
      kill "$PID" 2>/dev/null   # wrapper が最新 env / idx で数秒後に再接続する
      echo "swapped"
    else
      echo "swap_scheduled"     # ffmpeg 未起動（設定不足等）→ 次ループで新設定が反映される
    fi
    ;;
  *)
    echo "unknown command: $CMD" >&2
    exit 2
    ;;
esac
