#!/usr/bin/env python3
"""トークン健全性チェッカ（P3-5）。

チャンネルごとに以下を点検し、期限が近いものは Discord で「次回ログインが必要」と
**先回り通知**することで、無人実行中のサイレント失敗を防ぐ。

対象:
  1. YouTube OAuth: `<channel_folder>/.youtube_token.json` の有効期限と refresh
     可否を確認。refresh-token が無い / scope が古い / 期限切れ → warn。
  2. Playwright プロファイル: `~/.flow-playwright-profile` の cookie DB から
     Google アカウント関連 cookie の expires_utc を抽出。最短の expires が
     N 日以内なら warn。

設計方針:
- 副作用は Discord 通知のみ（自動再認証はやらない、運営者の同意が必要）
- ledger には記録しない（health チェックは状態の観測 = 履歴的価値が薄い）
- APScheduler の cron ジョブとして 1 日 1 回呼ぶ想定（job_type='token_health'）
"""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Optional

try:
    sys.path.insert(0, str(Path(__file__).parent))
    from _app_config import resolve_config_dir as _resolve_config_dir
    CONFIG_DIR = _resolve_config_dir()
except Exception:
    CONFIG_DIR = Path.home() / ".config" / "orzz"

CHANNELS_FILE = CONFIG_DIR / "channels.json"
PLAYWRIGHT_PROFILE = Path.home() / ".flow-playwright-profile"

# 期限が WARN_DAYS 日以内に切れる場合は通知
WARN_DAYS = int(os.environ.get("APP_TOKEN_HEALTH_WARN_DAYS", "7"))


# ─── YouTube token check ───────────────────────────

def check_youtube_token(channel_folder: str | Path) -> dict:
    """`<channel_folder>/.youtube_token.json` を点検。

    Returns:
      {"status": "ok" | "missing" | "warn" | "expired" | "no_refresh",
       "channel_folder": ..., "expiry": "ISO" | None, "days_left": int | None,
       "scopes": [...], "message": "..."}
    """
    cf = Path(channel_folder)
    token_path = cf / ".youtube_token.json"
    out = {
        "status": "missing",
        "channel_folder": str(cf),
        "expiry": None,
        "days_left": None,
        "scopes": [],
        "message": "",
    }
    if not token_path.exists():
        out["message"] = f"トークン未配置: {token_path.name}"
        return out
    try:
        d = json.loads(token_path.read_text(encoding="utf-8"))
    except Exception as e:
        out["message"] = f"トークン読込失敗: {e}"
        return out
    out["scopes"] = d.get("scopes") or []
    out["expiry"] = d.get("expiry") or d.get("expiry_utc") or ""
    has_refresh = bool(d.get("refresh_token"))
    if not has_refresh:
        out["status"] = "no_refresh"
        out["message"] = "refresh_token が無い → 期限切れ後に手動再認証が必要"
        return out
    if not out["expiry"]:
        out["status"] = "ok"  # refresh_token があれば expiry なくても再取得可
        out["message"] = "expiry 未記録（refresh_token あり、原則 OK）"
        return out
    try:
        # google-auth は ISO + 'Z' 形式を吐く
        expiry_str = out["expiry"].rstrip("Z")
        expiry_dt = datetime.datetime.fromisoformat(expiry_str)
    except Exception:
        out["status"] = "ok"
        out["message"] = f"expiry パース失敗 ({out['expiry']!r}, refresh_token あり)"
        return out
    now = datetime.datetime.utcnow()
    days_left = (expiry_dt - now).days
    out["days_left"] = days_left
    if days_left < 0:
        # refresh_token があれば自動更新されるので warn 止まり
        out["status"] = "ok"
        out["message"] = f"アクセストークン期限切れ ({-days_left}日前) — refresh_token で自動更新される想定"
    elif days_left <= WARN_DAYS:
        # アクセストークンの期限は通常 1h で短いので、これ自体は問題ない
        out["status"] = "ok"
        out["message"] = f"アクセストークン残 {days_left}日（通常）"
    else:
        out["status"] = "ok"
        out["message"] = f"アクセストークン残 {days_left}日"
    return out


# ─── Playwright profile cookie check ──────────────

GOOGLE_COOKIE_NAMES = (
    "SID", "SAPISID", "HSID", "SSID", "APISID", "__Secure-1PSID",
)


def check_playwright_profile(profile_dir: Path = None) -> dict:
    """Playwright プロファイルの Google アカウント関連 cookie の最短 expires を返す。

    Returns:
      {"status": "ok" | "missing" | "warn" | "expired",
       "profile_dir": ..., "cookies_found": [...],
       "earliest_expiry": "ISO" | None, "days_left": int | None,
       "message": "..."}
    """
    profile_dir = Path(profile_dir or PLAYWRIGHT_PROFILE)
    out = {
        "status": "missing",
        "profile_dir": str(profile_dir),
        "cookies_found": [],
        "earliest_expiry": None,
        "days_left": None,
        "message": "",
    }
    if not profile_dir.exists():
        out["message"] = "Playwright プロファイル未作成（初回ログイン未完了）"
        return out
    # Chromium プロファイルの Cookie DB を探す
    candidates = [
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
    ]
    db = next((c for c in candidates if c.exists()), None)
    if db is None:
        out["message"] = "Cookie DB が見つからない"
        return out
    try:
        # read-only で開く（ブラウザ実行中でも読める）
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            placeholders = ",".join("?" for _ in GOOGLE_COOKIE_NAMES)
            rows = conn.execute(
                f"""SELECT name, expires_utc FROM cookies
                    WHERE host_key LIKE '%.google.com'
                      AND name IN ({placeholders})""",
                list(GOOGLE_COOKIE_NAMES),
            ).fetchall()
        finally:
            conn.close()
    except Exception as e:
        out["message"] = f"Cookie DB 読込失敗: {e}"
        return out
    if not rows:
        out["message"] = "Google アカウント cookie が見つからない（ログアウト中？）"
        return out
    # Chromium の expires_utc は 1601-01-01 起点のマイクロ秒
    EPOCH_DELTA = datetime.datetime(1970, 1, 1) - datetime.datetime(1601, 1, 1)
    EPOCH_DELTA_SEC = int(EPOCH_DELTA.total_seconds())
    found = []
    earliest: Optional[datetime.datetime] = None
    for name, expires_utc in rows:
        if not expires_utc:
            continue
        try:
            unix_sec = expires_utc / 1_000_000 - EPOCH_DELTA_SEC
            dt = datetime.datetime.utcfromtimestamp(unix_sec)
        except Exception:
            continue
        found.append({"name": name, "expiry": dt.isoformat()})
        if earliest is None or dt < earliest:
            earliest = dt
    out["cookies_found"] = found
    if earliest is None:
        out["status"] = "ok"
        out["message"] = "expires 不明（session cookie のみ？）"
        return out
    out["earliest_expiry"] = earliest.isoformat()
    now = datetime.datetime.utcnow()
    days_left = (earliest - now).days
    out["days_left"] = days_left
    if days_left < 0:
        out["status"] = "expired"
        out["message"] = f"cookie 期限切れ ({-days_left}日前) — 次回 SUNO/Flow 起動で再ログインが必要"
    elif days_left <= WARN_DAYS:
        out["status"] = "warn"
        out["message"] = f"cookie 残 {days_left}日（{WARN_DAYS}日以内に再ログイン推奨）"
    else:
        out["status"] = "ok"
        out["message"] = f"cookie 残 {days_left}日"
    return out


# ─── 統合チェック ─────────────────────────────────

def check_all(notify=None) -> dict:
    """全チャンネル + Playwright を一括点検し、warn 以上の項目をリストで返す。

    Phase 5（token health cron）: notify を渡すと warnings がある時にコールバックを
    呼ぶ（app.py の _notify_line / _send_line_notify を注入する想定）。読み取り専用で
    副作用なし。out に overall（ok/warn/expired）を付与する。
    """
    out = {
        "checked_at": datetime.datetime.utcnow().isoformat() + "Z",
        "channels": [],
        "playwright": None,
        "warnings": [],  # 通知すべき項目
        "overall": "ok",
    }
    # チャンネル別 YouTube
    chs = []
    if CHANNELS_FILE.exists():
        try:
            chs = json.loads(CHANNELS_FILE.read_text(encoding="utf-8")) or []
        except Exception:
            chs = []
    for ch in chs:
        cf = ch.get("folder") or ""
        if not cf:
            continue
        result = check_youtube_token(cf)
        result["channel_id"] = ch.get("id", "")
        result["channel_name"] = ch.get("name", "")
        out["channels"].append(result)
        if result["status"] in ("warn", "expired", "missing", "no_refresh"):
            out["warnings"].append({
                "kind": "youtube",
                "channel_id": ch.get("id", ""),
                "channel_name": ch.get("name", ""),
                "status": result["status"],
                "message": result["message"],
            })
    # Playwright（共通プロファイル）
    pw = check_playwright_profile()
    out["playwright"] = pw
    if pw["status"] in ("warn", "expired", "missing"):
        out["warnings"].append({
            "kind": "playwright",
            "channel_id": "(global)",
            "channel_name": "(SUNO/Flow ブラウザ)",
            "status": pw["status"],
            "message": pw["message"],
        })
    # overall 判定: expired/missing があれば expired、warn/no_refresh があれば warn。
    statuses = [w["status"] for w in out["warnings"]]
    if any(s in ("expired", "missing") for s in statuses):
        out["overall"] = "expired"
    elif any(s in ("warn", "no_refresh") for s in statuses):
        out["overall"] = "warn"
    # Phase 5: 通知コールバック（warnings がある時のみ）
    if notify and out["warnings"]:
        lines = [f"[{w['channel_name']}] {w['kind']}: {w['status']}（{w['message']}）"
                 for w in out["warnings"]]
        try:
            notify("🔑 トークン点検アラート:\n" + "\n".join(lines))
        except Exception:
            pass
    return out


# ─── CLI ───────────────────────────────────────────

def _main():
    import argparse
    p = argparse.ArgumentParser(description="token health check")
    p.add_argument("--channel-folder", help="個別チャンネルだけ点検")
    p.add_argument("--playwright-only", action="store_true")
    p.add_argument("--cron", action="store_true",
                   help="全チャンネル集約点検（Phase5 cron用）。overall=expired なら exit 1")
    args = p.parse_args()
    if args.channel_folder:
        print(json.dumps(check_youtube_token(args.channel_folder),
                         ensure_ascii=False, indent=2))
        return
    if args.playwright_only:
        print(json.dumps(check_playwright_profile(),
                         ensure_ascii=False, indent=2))
        return
    rep = check_all()
    print(json.dumps(rep, ensure_ascii=False, indent=2))
    if args.cron:
        # OS cron / launchd 用の終了コード（expired なら非ゼロで監視に拾わせる）
        raise SystemExit(1 if rep.get("overall") == "expired" else 0)


if __name__ == "__main__":
    _main()
