#!/usr/bin/env python3
"""毎朝のDiscord運営ダイジェスト."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _app_config import resolve_shared_config_dir  # noqa: E402
from app_stock import compute_stock  # noqa: E402
from error_humanizer import humanize_error  # noqa: E402
from settings_service import config_get  # noqa: E402

CONFIG_DIR = resolve_shared_config_dir()
DISCORD_CONFIG = CONFIG_DIR / "discord_config.json"
JST = timezone(timedelta(hours=9))


def digest_enabled() -> bool:
    try:
        v = config_get("digest.enabled").get("value")
        return True if v is None else bool(v)
    except Exception:
        return True


def digest_time_jst() -> str:
    try:
        v = str(config_get("digest.time_jst").get("value") or "08:00").strip()
        hh, mm = map(int, v.split(":"))
        if 0 <= hh <= 23 and 0 <= mm <= 59:
            return f"{hh:02d}:{mm:02d}"
    except Exception:
        pass
    return "08:00"


def _date_range_jst(days_ago: int = 0) -> tuple[datetime, datetime]:
    today = datetime.now(JST).date()
    start = datetime.combine(today - timedelta(days=days_ago), datetime.min.time(), tzinfo=JST)
    end = start + timedelta(days=1)
    return start, end


def _load_ledger() -> list[dict[str, Any]]:
    try:
        import app_run_ledger as ledger
        return ledger.list_runs(limit=200)
    except Exception:
        return []


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(JST)
    except Exception:
        return None


def _yesterday_runs(runs: list[dict[str, Any]]) -> dict[str, Any]:
    start, end = _date_range_jst(1)
    items = []
    for r in runs:
        dt = _parse_dt(r.get("finished_at") or r.get("started_at") or "")
        if dt and start <= dt < end:
            items.append(r)
    return {
        "done": [r for r in items if r.get("status") in ("done", "reconstructed")],
        "failed": [r for r in items if r.get("status") == "failed"],
    }


def _today_schedule(stock: dict[str, Any]) -> list[dict[str, Any]]:
    today = datetime.now(JST).date().isoformat()
    out = []
    for ch in stock.get("channels") or []:
        for item in ch.get("items") or []:
            if item.get("publish_date") == today:
                out.append({"channel_name": ch.get("channel_name", ""), **item})
    return out


def _token_warnings() -> list[dict[str, Any]]:
    try:
        import app_token_health
        return app_token_health.check_all().get("warnings") or []
    except Exception as e:
        return [{"channel_name": "token health", "kind": "system", "status": "error", "message": str(e)}]


def _disk_status() -> dict[str, Any]:
    try:
        du = shutil.disk_usage(str(CONFIG_DIR))
        free_gb = du.free / 1024 / 1024 / 1024
        total_gb = du.total / 1024 / 1024 / 1024
        return {"path": str(CONFIG_DIR), "free_gb": round(free_gb, 1), "total_gb": round(total_gb, 1), "warning": free_gb < 20}
    except Exception as e:
        return {"path": str(CONFIG_DIR), "error": str(e), "warning": True}


def _field(name: str, lines: list[str], *, inline: bool = False) -> dict[str, Any]:
    value = "\n".join(lines).strip() or "なし"
    if len(value) > 1000:
        value = value[:997] + "..."
    return {"name": name, "value": value, "inline": inline}


def _learning_fields() -> tuple[list[dict[str, Any]], dict[str, int]]:
    try:
        import app_learning
        sections = app_learning.digest_sections()
    except Exception as e:
        return ([
            _field("収益化サマリ", [f"取得できませんでした: {e}"]),
            _field("48hレビュー", ["スキップ（学習集約の取得失敗）"], inline=True),
            _field("ライバルアラート", ["スキップ（学習集約の取得失敗）"], inline=True),
        ], {"learning_errors": 1})

    monet = sections.get("monetization") or {}
    chs = monet.get("channels") or []
    monet_lines = []
    for ch in chs[:7]:
        latest = ch.get("latest") or {}
        subs = latest.get("subscriber_count")
        pred = ch.get("subscriber_prediction") or {}
        pred_text = pred.get("date") or pred.get("status") or "-"
        analytics = ch.get("analytics") or {}
        watch = "Analytics未有効" if analytics.get("needs_reauth") else f"{analytics.get('watch_hours_28d', '-')}h/28d"
        if ch.get("status") == "ok":
            sub_text = f"{int(subs or 0):,}"
            monet_lines.append(f"- {ch.get('name')}: 登録 {sub_text} / 予測 {pred_text} / {watch}")
        else:
            monet_lines.append(f"- {ch.get('name')}: {ch.get('error') or ch.get('status')}")

    rev = sections.get("reviews") or {}
    review_lines = []
    for r in (rev.get("reviews") or [])[:6]:
        diff = r.get("diff_pct")
        mark = "+" if isinstance(diff, (int, float)) and diff >= 0 else ""
        review_lines.append(f"- {r.get('channel_name')}: 平均比 {mark}{diff}% {r.get('title','')[:36]}")
        if r.get("insight"):
            review_lines.append(f"  {r.get('insight')}")
    if not review_lines and rev.get("skipped"):
        review_lines.append(f"対象なし（{len(rev.get('skipped') or [])}ch skip）")

    rivals = sections.get("rivals") or {}
    rival_lines = []
    for a in (rivals.get("alerts") or [])[:6]:
        rival_lines.append(f"- [{a.get('channel')}] {a.get('ratio')}x {a.get('title','')[:34]}")
        if a.get("insight"):
            rival_lines.append(f"  {a.get('insight')}")
    if not rival_lines:
        rival_lines.append("検出なし")

    return ([
        _field("収益化サマリ", monet_lines),
        _field("48hレビュー", review_lines),
        _field("ライバルアラート", rival_lines),
    ], {
        "monetization_channels": len(chs),
        "reviews": len(rev.get("reviews") or []),
        "rival_alerts": len(rivals.get("alerts") or []),
    })


def _quota_field() -> dict[str, Any]:
    try:
        import app_quota
        q = app_quota.quota_summary()
        std = q.get("standard") or {}
        batch = q.get("batchGetStats") or {}
        return _field("YouTube API quota", [
            f"標準 {std.get('used', 0)}/{std.get('cap', 10000)} unit ({std.get('pct', 0)}%) / batchGetStats {batch.get('used', 0)}/{batch.get('cap', 10000)} unit ({batch.get('pct', 0)}%)",
        ], inline=True)
    except Exception as e:
        return _field("YouTube API quota", [f"取得できませんでした: {e}"], inline=True)


def _genre_radar_field() -> dict[str, Any]:
    try:
        import app_genre_radar
        return _field("参入候補ジャンル TOP5", app_genre_radar.digest_lines(5))
    except Exception as e:
        return _field("参入候補ジャンル TOP5", [f"取得できませんでした: {e}"])


def _posting_strategy_field() -> dict[str, Any] | None:
    try:
        import app_posting_strategy
        lines = app_posting_strategy.digest_lines(5)
        return _field("週次 投稿戦略提案", lines) if lines else None
    except Exception as e:
        return _field("週次 投稿戦略提案", [f"取得できませんでした: {e}"])


def _reconcile_field() -> tuple[dict[str, Any], dict[str, int]]:
    try:
        import app_reconcile
        report = app_reconcile.run_reconcile_all(limit=50, write_report=True)
        summary = report.get("summary") or {}
        return _field("整合性", [app_reconcile.digest_line()], inline=True), {
            "reconcile_drift": int(summary.get("drift_count") or 0),
            "reconcile_errors": int(summary.get("error_count") or 0),
        }
    except Exception as e:
        return _field("整合性", [f"取得できませんでした: {e}"], inline=True), {
            "reconcile_drift": 0,
            "reconcile_errors": 1,
        }


def build_digest(*, test: bool = False) -> dict[str, Any]:
    stock = compute_stock()
    runs = _load_ledger()
    yr = _yesterday_runs(runs)
    today_items = _today_schedule(stock)
    failed = [r for r in runs if r.get("status") == "failed"][:8]
    token_warnings = _token_warnings()
    disk = _disk_status()
    stock_warnings = stock.get("warnings") or []
    learning_fields, learning_counts = _learning_fields()
    posting_field = _posting_strategy_field()
    reconcile_field, reconcile_counts = _reconcile_field()

    title = f"{'【テスト】' if test else ''}Automation Studio 日次ダイジェスト"
    fields = [
        _field("昨日の投稿結果", [
            f"成功 {len(yr['done'])} 件 / 失敗 {len(yr['failed'])} 件",
            *[f"- {r.get('channel_name') or r.get('channel_id') or '-'} vol.{r.get('vol')} {r.get('status')}" for r in (yr["done"] + yr["failed"])[:6]],
        ]),
        _field("今日の予約", [
            *[f"- {x.get('channel_name') or '-'} vol.{x.get('vol')} {x.get('publish_date')}" for x in today_items[:10]],
        ]),
        _field("未解決エラー", [
            *[f"- {humanize_error(exit_code=r.get('exit_code'), message=r.get('summary') or '', stage=r.get('failed_stage') or '', channel_name=r.get('channel_name') or '', vol=r.get('vol')).get('plain_ja')} / 次: {humanize_error(exit_code=r.get('exit_code'), message=r.get('summary') or '', stage=r.get('failed_stage') or '').get('next_action')}" for r in failed],
        ]),
        _field("トークン期限予兆", [
            *[f"- {w.get('channel_name') or w.get('kind')}: {w.get('status')} {w.get('message')}" for w in token_warnings[:8]],
        ]),
        _field("ディスク", [
            f"空き {disk.get('free_gb', '-')} GB / {disk.get('total_gb', '-')} GB",
            f"場所: {disk.get('path', '')}",
        ], inline=True),
        _field("素材在庫警告", [
            *[f"- {c.get('channel_name')}: {c.get('stock_days')}日分（警告しきい値 {c.get('warn_days')}日）" for c in stock_warnings],
        ], inline=True),
        _quota_field(),
        reconcile_field,
        _genre_radar_field(),
        *([posting_field] if posting_field else []),
        *learning_fields,
    ]
    warn_count = (
        len(failed) + len(token_warnings) + len(stock_warnings)
        + (1 if disk.get("warning") else 0)
        + int(reconcile_counts.get("reconcile_drift") or 0)
        + int(reconcile_counts.get("reconcile_errors") or 0)
    )
    embed = {
        "title": title,
        "description": f"{datetime.now(JST).strftime('%Y-%m-%d %H:%M')} JST 時点 / 要確認 {warn_count} 件",
        "color": 0xF59E0B if warn_count else 0x22C55E,
        "fields": fields,
        "footer": {"text": "Automation Studio H-B digest"},
    }
    return {
        "status": "ok",
        "test": test,
        "embed": embed,
        "stock": stock,
        "counts": {
            "yesterday_done": len(yr["done"]),
            "yesterday_failed": len(yr["failed"]),
            "today_schedule": len(today_items),
            "unresolved_errors": len(failed),
            "token_warnings": len(token_warnings),
            "stock_warnings": len(stock_warnings),
            **reconcile_counts,
            **learning_counts,
        },
    }


def send_digest(*, test: bool = False) -> dict[str, Any]:
    data = build_digest(test=test)
    cfg = json.loads(DISCORD_CONFIG.read_text(encoding="utf-8"))
    webhook = (cfg.get("webhook_url") or "").strip()
    if not webhook:
        raise RuntimeError("Discord Webhook URL が未設定です")
    payload = {"embeds": [data["embed"]], "username": cfg.get("username") or "Automation Studio"}
    if cfg.get("avatar_url"):
        payload["avatar_url"] = cfg["avatar_url"]
    r = subprocess.run(
        ["curl", "-fsS", "-X", "POST", webhook, "-H", "Content-Type: application/json",
         "-d", json.dumps(payload, ensure_ascii=False)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
    )
    data["discord"] = {
        "sent": r.returncode == 0,
        "status_code": 204 if r.returncode == 0 else None,
        "stderr": (r.stderr or "")[-500:],
    }
    if r.returncode != 0:
        raise RuntimeError(f"Discord送信失敗: {r.stderr.strip() or r.stdout.strip() or r.returncode}")
    return data


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--send", action="store_true")
    p.add_argument("--test", action="store_true")
    args = p.parse_args()
    out = send_digest(test=args.test) if args.send else build_digest(test=args.test)
    print(json.dumps(out, ensure_ascii=False, indent=2))
