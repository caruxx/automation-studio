#!/usr/bin/env bash
# YouTube ループ配信ラッパー v2（screen セッション内で実行される）
# usage: stream.sh <stream_id>
#
# 設定: <BASE>/channels/<id>.env を毎ループ読み込む。
#   STREAM_KEY / VIDEO / MODE / RTMP_URL / VBITRATE / ABITRATE / FPS / SCALE
#   PLAYLIST="path1:path2:..."  … ローテーション用動画リスト（コロン区切り）
#   ROTATE_SECONDS=0            … >0 なら N 秒ごとに次の動画へ差し替え（配信は継続）
#   MAX_SECONDS=0               … >0 なら配信開始から N 秒で自動打ち切り（screen 終了）
#
# 仕組み:
#   - ffmpeg をクラッシュ自動再起動付きで回し、状態を <BASE>/status/<id>.props に書く
#   - PLAYLIST があれば <BASE>/status/<id>.idx の巡回位置で再生動画を決める
#     （定時ローテ=124 / 正常終了=0 のときだけ次へ進む。異常終了は同じ動画でリトライ）
#   - 「配信を止めずに差し替え」= ytlive.sh swap が ffmpeg のみ kill →
#     このループが新しい env / idx を読んで数秒で再接続（YouTube 側は同一ライブ継続）
# 停止: <BASE>/status/<id>.stop が存在するとループを抜ける（ytlive.sh stop が作成）
set -u
CH="${1:?usage: stream.sh <stream_id>}"
BASE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENVF="$BASE/channels/$CH.env"
LOG="$BASE/logs/$CH.log"
STATUSF="$BASE/status/$CH.props"
STOPF="$BASE/status/$CH.stop"
IDXF="$BASE/status/$CH.idx"
mkdir -p "$BASE/logs" "$BASE/status"

START_TS=$(date +%s)
STARTED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
RESTARTS=0
LAST_EXIT=""
FFPID=""
STREAM_KEY=""
VIDEO=""
CUR_VIDEO=""
MODE="reencode"
MAX_SECONDS=0
ROTATE_SECONDS=0
PLAYLIST=""
IDX=0

log(){ echo "[$(date '+%F %T')] $*" >> "$LOG"; }

write_status(){
  local max_until=""
  [ "${MAX_SECONDS:-0}" -gt 0 ] && max_until=$((START_TS + MAX_SECONDS))
  {
    echo "channel=$CH"
    echo "wrapper_pid=$$"
    echo "ffmpeg_pid=${FFPID:-}"
    echo "started_at=$STARTED_AT"
    echo "restarts=$RESTARTS"
    echo "last_exit=${LAST_EXIT:-}"
    echo "video=${CUR_VIDEO:-${VIDEO:-}}"
    echo "mode=${MODE:-copy}"
    echo "playlist_idx=${IDX:-0}"
    echo "max_seconds=${MAX_SECONDS:-0}"
    echo "rotate_seconds=${ROTATE_SECONDS:-0}"
    echo "max_until=${max_until}"
    echo "updated_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  } > "$STATUSF.tmp" && mv "$STATUSF.tmp" "$STATUSF"
}

cleanup(){
  log "wrapper 終了シグナル受信"
  [ -n "$FFPID" ] && kill "$FFPID" 2>/dev/null
  FFPID=""
  write_status
  exit 0
}
trap cleanup TERM INT HUP

log "=== wrapper 開始 (pid $$) ==="
rm -f "$STOPF"
[ -f "$IDXF" ] && IDX=$(cat "$IDXF" 2>/dev/null || echo 0)
case "$IDX" in (*[!0-9]*|"") IDX=0;; esac
write_status

while :; do
  # 自動打ち切り判定
  NOW=$(date +%s)
  ELAPSED=$((NOW - START_TS))
  if [ "${MAX_SECONDS:-0}" -gt 0 ] && [ "$ELAPSED" -ge "$MAX_SECONDS" ]; then
    log "MAX_SECONDS(${MAX_SECONDS}s) 到達 → 配信を自動終了"
    touch "$STOPF"
    break
  fi

  if [ ! -f "$ENVF" ]; then
    log "設定ファイルが無い: $ENVF"
    write_status
    sleep 10
    [ -f "$STOPF" ] && break
    continue
  fi
  # shellcheck disable=SC1090
  STREAM_KEY="" VIDEO="" PLAYLIST="" MAX_SECONDS=0 ROTATE_SECONDS=0
  source "$ENVF"
  MODE="${MODE:-reencode}"
  RTMP_URL="${RTMP_URL:-rtmp://a.rtmp.youtube.com/live2}"
  case "${MAX_SECONDS:-0}" in (*[!0-9]*|"") MAX_SECONDS=0;; esac
  case "${ROTATE_SECONDS:-0}" in (*[!0-9]*|"") ROTATE_SECONDS=0;; esac

  # プレイリストがあれば巡回位置で再生動画を決定（VIDEO は単発時のフォールバック）
  CUR_VIDEO="$VIDEO"
  if [ -n "$PLAYLIST" ]; then
    IFS=':' read -r -a PL <<< "$PLAYLIST"
    if [ "${#PL[@]}" -gt 0 ]; then
      CUR_VIDEO="${PL[$((IDX % ${#PL[@]}))]}"
    fi
  fi

  if [ -z "$STREAM_KEY" ] || [ -z "$CUR_VIDEO" ] || [ ! -f "$CUR_VIDEO" ]; then
    log "設定不足: STREAM_KEY または動画を確認 (video=${CUR_VIDEO:-未設定})"
    write_status
    sleep 10
    [ -f "$STOPF" ] && break
    continue
  fi

  # ログローテーション（10MB 超で直近 1MB を残す）
  if [ -f "$LOG" ] && [ "$(stat -c%s "$LOG" 2>/dev/null || echo 0)" -gt 10485760 ]; then
    tail -c 1048576 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
  fi

  if [ "$MODE" = "reencode" ]; then
    FPS="${FPS:-30}"
    VB="${VBITRATE:-4500k}"
    GOP=$((FPS * 2))
    ARGS=(-re -stream_loop -1 -i "$CUR_VIDEO"
      -c:v libx264 -preset "${PRESET:-veryfast}"
      -b:v "$VB" -maxrate "$VB" -bufsize "${BUFSIZE:-9000k}"
      -pix_fmt yuv420p -r "$FPS" -g "$GOP" -keyint_min "$GOP" -sc_threshold 0
      -x264-params "keyint=${GOP}:min-keyint=${GOP}:scenecut=0"
      -force_key_frames "expr:gte(t,n_forced*2)")
    [ -n "${SCALE:-}" ] && ARGS+=(-vf "scale=${SCALE}")
    ARGS+=(-c:a aac -b:a "${ABITRATE:-160k}" -ar 48000)
  else
    # copy: 再エンコード無し（CPU ほぼゼロ）。入力は H.264+AAC 前提。
    ARGS=(-fflags +genpts -re -stream_loop -1 -i "$CUR_VIDEO" -c copy)
  fi

  # ffmpeg の定時終了タイマー: ローテ間隔と MAX 残り時間の小さい方
  T=0
  [ "$ROTATE_SECONDS" -gt 0 ] && [ -n "$PLAYLIST" ] && T=$ROTATE_SECONDS
  if [ "$MAX_SECONDS" -gt 0 ]; then
    REM=$((MAX_SECONDS - ELAPSED))
    [ "$REM" -lt 1 ] && REM=1
    if [ "$T" -eq 0 ] || [ "$REM" -lt "$T" ]; then T=$REM; fi
  fi

  # -y: RTMP では無害。ローカルパス出力のドライラン時に上書き拒否で止まらないようにする
  log "ffmpeg 起動 (mode=$MODE video=$(basename "$CUR_VIDEO") idx=$IDX restarts=$RESTARTS timer=${T}s)"
  if [ "$T" -gt 0 ]; then
    timeout -s TERM "$T" ffmpeg -y -hide_banner -loglevel warning -nostdin "${ARGS[@]}" -f flv "$RTMP_URL/$STREAM_KEY" >> "$LOG" 2>&1 &
  else
    ffmpeg -y -hide_banner -loglevel warning -nostdin "${ARGS[@]}" -f flv "$RTMP_URL/$STREAM_KEY" >> "$LOG" 2>&1 &
  fi
  FFPID=$!
  write_status
  wait "$FFPID"
  LAST_EXIT=$?
  FFPID=""
  RESTARTS=$((RESTARTS + 1))

  # 124 = timeout による定時終了（ローテ/打切り）、0 = 正常終了 → 次の動画へ
  # それ以外（ネットワーク断・kill 等）は同じ動画でリトライ（swap next は idx を直接更新）
  if [ "$LAST_EXIT" = "124" ] || [ "$LAST_EXIT" = "0" ]; then
    IDX=$((IDX + 1))
    echo "$IDX" > "$IDXF"
    log "ffmpeg 定時終了 code=$LAST_EXIT → 次の動画へ (idx=$IDX)"
  else
    # swap で idx が外部更新された場合は取り込む
    [ -f "$IDXF" ] && IDX=$(cat "$IDXF" 2>/dev/null || echo "$IDX")
    case "$IDX" in (*[!0-9]*|"") IDX=0;; esac
    log "ffmpeg 終了 code=$LAST_EXIT"
  fi
  write_status
  [ -f "$STOPF" ] && { log "stop 指示により終了"; break; }
  sleep "${RESTART_DELAY:-5}"
done

write_status
log "=== wrapper 終了 ==="
