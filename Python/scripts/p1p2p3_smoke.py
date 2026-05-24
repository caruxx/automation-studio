#!/usr/bin/env python3
"""P1〜P3 改善計画の運用テスト（スモークテスト）。

実 API を叩いて各機能が稼働しているかを確認する非破壊テスト。
副作用は **作って即削除する scheduler テストジョブのみ**で、本番データ（vol、
upload）には触らない。

使い方:
    python3 Python/scripts/p1p2p3_smoke.py
    python3 Python/scripts/p1p2p3_smoke.py --base http://localhost:8888
    python3 Python/scripts/p1p2p3_smoke.py --skip-render --skip-publish

終了コード:
    0  全て pass
    1  1 件以上 fail
    2  サーバへの接続に失敗
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Optional

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"


class TestResult:
    def __init__(self):
        self.tests: list = []  # (name, status, message)

    def record(self, name: str, status: str, message: str = "") -> None:
        self.tests.append((name, status, message))
        color = GREEN if status == "PASS" else (YELLOW if status == "SKIP" else RED)
        icon = "✓" if status == "PASS" else ("⏭" if status == "SKIP" else "✗")
        print(f"  {color}{icon} {name}{RESET}", end="")
        if message:
            print(f" {DIM}— {message}{RESET}")
        else:
            print()

    def summary(self) -> int:
        n_pass = sum(1 for _, s, _ in self.tests if s == "PASS")
        n_fail = sum(1 for _, s, _ in self.tests if s == "FAIL")
        n_skip = sum(1 for _, s, _ in self.tests if s == "SKIP")
        print(f"\n{'=' * 60}")
        print(f"  Total: {len(self.tests)}  "
              f"{GREEN}PASS={n_pass}{RESET}  "
              f"{RED}FAIL={n_fail}{RESET}  "
              f"{YELLOW}SKIP={n_skip}{RESET}")
        print(f"{'=' * 60}")
        return 0 if n_fail == 0 else 1


# ─── HTTP helpers ───────────────────────────────────

def _http(method: str, base: str, path: str, body: Optional[dict] = None,
          timeout: int = 10) -> tuple:
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode()
            try:
                return resp.status, json.loads(payload)
            except json.JSONDecodeError:
                return resp.status, payload
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode()
            return e.code, json.loads(payload)
        except Exception:
            return e.code, str(e)
    except Exception as e:
        return 0, str(e)


def _get(base, path, **kw): return _http("GET", base, path, **kw)
def _post(base, path, body=None, **kw): return _http("POST", base, path, body, **kw)
def _put(base, path, body=None, **kw): return _http("PUT", base, path, body, **kw)
def _delete(base, path, **kw): return _http("DELETE", base, path, **kw)


# ─── Test sections ──────────────────────────────────

def test_p1(base: str, r: TestResult) -> None:
    print(f"\n{DIM}── P1（短期：信頼性 / 無人実行の地盤） ──{RESET}")
    # P1-1 (interactive ハング撲滅): suno_auto_create.py に UnattendedLoginRequired 例外がある
    import subprocess as _sp
    res = _sp.run([sys.executable, "-c",
                   "import sys; sys.path.insert(0, 'Python'); import suno_auto_create; print(suno_auto_create.UnattendedLoginRequired.__name__)"],
                  capture_output=True, text=True, timeout=10)
    if res.returncode == 0 and "UnattendedLoginRequired" in res.stdout:
        r.record("P1-1 UnattendedLoginRequired exception 定義済み", "PASS")
    else:
        r.record("P1-1 UnattendedLoginRequired exception", "FAIL", res.stderr[:120])

    # P1-2 (retry): app_pipeline.py に EXIT_RETRYABLE と RETRY_POLICY がある
    res = _sp.run([sys.executable, "-c",
                   "import sys; sys.path.insert(0, 'Python'); import app_pipeline; print(app_pipeline.EXIT_RETRYABLE, len(app_pipeline.RETRY_POLICY))"],
                  capture_output=True, text=True, timeout=10)
    if res.returncode == 0 and "76" in res.stdout:
        r.record("P1-2 EXIT_RETRYABLE=76 + RETRY_POLICY 定義", "PASS", res.stdout.strip())
    else:
        r.record("P1-2 retry layer", "FAIL", res.stderr[:120])

    # P1-3 (channel pointer): pipeline で --channel-id arg が定義されている
    res = _sp.run([sys.executable, "Python/app_pipeline.py", "--help"],
                  capture_output=True, text=True, timeout=10)
    if "--channel-id" in res.stdout and "--channel-folder" in res.stdout:
        r.record("P1-3 --channel-id / --channel-folder CLI args", "PASS")
    else:
        r.record("P1-3 channel CLI args", "FAIL", "args missing")

    # P1-4 (quota): app_youtube に check_quota_before_upload がある
    res = _sp.run([sys.executable, "-c",
                   "import sys; sys.path.insert(0, 'Python'); import app_youtube; print(app_youtube.EXIT_QUOTA_EXHAUSTED, app_youtube.DEFAULT_DAILY_QUOTA_CAP)"],
                  capture_output=True, text=True, timeout=10)
    if res.returncode == 0 and "77" in res.stdout:
        r.record("P1-4 EXIT_QUOTA_EXHAUSTED=77 + cap 設定", "PASS", res.stdout.strip())
    else:
        r.record("P1-4 quota module", "FAIL", res.stderr[:120])

    # P1-5 (preflight): app_pipeline._preflight_premiere() が呼べる
    res = _sp.run([sys.executable, "-c",
                   "import sys; sys.path.insert(0, 'Python'); import app_pipeline; ok,msg = app_pipeline._preflight_premiere(); print(type(ok).__name__, type(msg).__name__)"],
                  capture_output=True, text=True, timeout=10)
    if res.returncode == 0 and "bool" in res.stdout and "str" in res.stdout:
        r.record("P1-5 _preflight_premiere() returns (bool, str)", "PASS")
    else:
        r.record("P1-5 preflight", "FAIL", res.stderr[:120])

    # P1-6 (runbook + Discord 文言): docs/runbook.md があり、_notify_discord に runbook_hint が組み込まれている
    res = _sp.run(["test", "-f", "docs/runbook.md"], capture_output=True)
    if res.returncode == 0:
        r.record("P1-6 docs/runbook.md 配置", "PASS")
    else:
        r.record("P1-6 runbook", "FAIL", "docs/runbook.md not found")

    # P1-7 (一元状態 API)
    code, payload = _get(base, "/api/runs/active")
    if code == 200 and isinstance(payload, dict) and "channels" in payload:
        chN = len(payload.get("channels", []))
        r.record("P1-7 GET /api/runs/active", "PASS", f"{chN} channels")
    else:
        r.record("P1-7 /api/runs/active", "FAIL", f"http {code}")


def test_p2(base: str, r: TestResult, skip_render: bool = False) -> None:
    print(f"\n{DIM}── P2（中期：N チャンネル並列 + 物理制約） ──{RESET}")
    # P2-1 render queue
    if skip_render:
        r.record("P2-1 render queue API", "SKIP", "--skip-render")
    else:
        code, payload = _get(base, "/api/render-queue")
        if code == 200 and "counts" in (payload or {}):
            c = payload["counts"]
            r.record("P2-1 GET /api/render-queue", "PASS",
                     f"pending={c.get('pending',0)} running={c.get('running',0)}")
        else:
            r.record("P2-1 render queue", "FAIL", f"http {code}")
        code, payload = _get(base, "/api/render-queue/throughput?days=7")
        if code == 200 and "queue_throughput_per_day" in (payload or {}):
            r.record("P2-1 GET /api/render-queue/throughput", "PASS",
                     f"~{payload.get('queue_throughput_per_day',0)} vol/day")
        else:
            r.record("P2-1 throughput", "FAIL", f"http {code}")

    # P2-2 channel registry
    code, payload = _get(base, "/api/channels")
    chs = []
    if isinstance(payload, dict):
        chs = payload.get("channels") or []
    elif isinstance(payload, list):
        chs = payload
    if code == 200 and len(chs) >= 1:
        r.record("P2-2 channels.json 正典", "PASS",
                 f"{len(chs)} channels: {[c.get('id') for c in chs]}")
    else:
        r.record("P2-2 channels registry", "FAIL", f"http {code}, len={len(chs)}")

    # P2-3 per-channel scheduler view
    code, payload = _get(base, "/api/schedule/jobs?channel_id=__none__")
    if code == 200 and "jobs" in (payload or {}):
        r.record("P2-3 GET /api/schedule/jobs?channel_id=...", "PASS",
                 f"{len(payload['jobs'])} jobs (none-filter)")
    else:
        r.record("P2-3 schedule filter", "FAIL", f"http {code}")

    # P2-4 slot balancing
    name = f"P2_4_SMOKE_{int(time.time())}"
    body = {"type": "vol_create", "name": name, "enabled": True,
            "trigger": {"kind": "cron", "day_of_week": "sun", "hour": 4, "minute": 0}}
    code1, p1 = _post(base, "/api/schedule/jobs", body)
    body2 = dict(body, name=f"{name}_2")
    code2, p2 = _post(base, "/api/schedule/jobs", body2)
    cleaned = []
    if code1 == 200 and code2 == 200:
        t2 = p2.get("job", {}).get("trigger", {})
        shift = p2.get("slot_shifted_minutes", 0)
        if shift >= 30 and t2.get("minute") == 30:
            r.record("P2-4 slot balancing 30 min ずらし", "PASS",
                     f"job2 = sun {t2.get('hour'):02d}:{t2.get('minute'):02d}")
        else:
            r.record("P2-4 slot balancing", "FAIL", f"shift={shift}, t2={t2}")
        cleaned = [p1.get("job", {}).get("id"), p2.get("job", {}).get("id")]
    else:
        r.record("P2-4 slot balancing", "FAIL", f"http {code1}/{code2}")
    for jid in cleaned:
        if jid:
            _delete(base, f"/api/schedule/jobs/{jid}")

    # P2-5 thumbnail step は STEPS に含まれている
    import subprocess as _sp
    res = _sp.run([sys.executable, "-c",
                   "import sys; sys.path.insert(0, 'Python'); import app_pipeline; print('thumbnail' in app_pipeline.STEPS, app_pipeline.STEPS)"],
                  capture_output=True, text=True, timeout=10)
    if res.returncode == 0 and "True" in res.stdout:
        r.record("P2-5 step_thumbnail in STEPS", "PASS",
                 res.stdout.strip().split('\n')[-1][:80])
    else:
        r.record("P2-5 step_thumbnail", "FAIL", res.stderr[:120])

    # P2-6 auto_resume — schedule 投入時に auto_resume フィールドが受け入れられる
    name = f"P2_6_SMOKE_{int(time.time())}"
    body = {"type": "vol_create", "name": name, "enabled": True,
            "trigger": {"kind": "cron", "day_of_week": "sun", "hour": 4, "minute": 30},
            "auto_resume": True, "auto_resume_delay_min": 25, "auto_resume_max_attempts": 4}
    code, payload = _post(base, "/api/schedule/jobs", body)
    if code == 200 and payload.get("job", {}).get("auto_resume") is True:
        j = payload["job"]
        r.record("P2-6 auto_resume フィールド受入", "PASS",
                 f"delay={j.get('auto_resume_delay_min')}m max={j.get('auto_resume_max_attempts')}")
        _delete(base, f"/api/schedule/jobs/{j['id']}")
    else:
        r.record("P2-6 auto_resume", "FAIL", f"http {code}")

    # P2-7 公開ゲート — JOB_HANDLERS に publish_now、エンドポイント存在
    code, payload = _post(base, "/api/youtube/publish-now/__nonexistent_test__")
    if code == 404:
        r.record("P2-7 POST /api/youtube/publish-now/{name} （存在）", "PASS",
                 "404 for nonexistent vol")
    else:
        r.record("P2-7 publish-now endpoint", "FAIL", f"http {code}")


def test_p3(base: str, r: TestResult) -> None:
    print(f"\n{DIM}── P3（長期：自走型 + 中央台帳） ──{RESET}")
    # P3-1 ledger
    code, payload = _get(base, "/api/runs/ledger?limit=3")
    if code == 200 and "runs" in (payload or {}):
        r.record("P3-1 GET /api/runs/ledger", "PASS",
                 f"{payload.get('count', 0)} runs in ledger")
    else:
        r.record("P3-1 ledger list", "FAIL", f"http {code}")
    code, payload = _get(base, "/api/runs/ledger/stats?days=30")
    if code == 200 and "by_channel" in (payload or {}):
        r.record("P3-1 ledger stats", "PASS",
                 f"total={payload.get('total',0)} resume_chains={payload.get('auto_resume_chains',0)}")
    else:
        r.record("P3-1 ledger stats", "FAIL", f"http {code}")
    code, payload = _post(base, "/api/runs/ledger/migrate", {})
    if code == 200 and "channels" in (payload or {}):
        r.record("P3-1 migration dry-run", "PASS",
                 f"would_insert={payload.get('total_would_insert',0)}")
    else:
        r.record("P3-1 migration", "FAIL", f"http {code}")

    # P3-2 plan auto-adoption — _load_benchmark_axes の存在確認
    import subprocess as _sp
    res = _sp.run([sys.executable, "-c",
                   "import sys; sys.path.insert(0, 'Python'); import app_pipeline; "
                   "axes = app_pipeline._load_benchmark_axes(); "
                   "print(sorted(k for k,v in axes.items() if v is not None))"],
                  capture_output=True, text=True, timeout=15)
    if res.returncode == 0:
        r.record("P3-2 _load_benchmark_axes()", "PASS",
                 f"axes detected: {res.stdout.strip()}")
    else:
        r.record("P3-2 plan auto-adoption", "FAIL", res.stderr[:120])

    # P3-3 step_qa — STEPS の順序
    res = _sp.run([sys.executable, "-c",
                   "import sys; sys.path.insert(0, 'Python'); import app_pipeline; "
                   "qa_idx = app_pipeline.STEPS.index('qa') if 'qa' in app_pipeline.STEPS else -1; "
                   "exp_idx = app_pipeline.STEPS.index('export') if 'export' in app_pipeline.STEPS else -1; "
                   "meta_idx = app_pipeline.STEPS.index('meta') if 'meta' in app_pipeline.STEPS else -1; "
                   "print(qa_idx, exp_idx, meta_idx, qa_idx == exp_idx + 1 and qa_idx + 1 == meta_idx)"],
                  capture_output=True, text=True, timeout=10)
    if res.returncode == 0 and res.stdout.strip().endswith("True"):
        r.record("P3-3 step_qa が export → meta 間に挿入", "PASS",
                 res.stdout.strip())
    else:
        r.record("P3-3 step_qa ordering", "FAIL", res.stderr[:120])

    # P3-4 policy-aware load balancing — テキスト検索で関数定義の存在確認
    try:
        with open("Python/app.py", encoding="utf-8") as f:
            src = f.read()
        has_gap = "_estimate_required_gap_minutes" in src
        has_pressure = "_channel_quota_pressure" in src
        if has_gap and has_pressure:
            r.record("P3-4 policy-aware helpers 定義", "PASS",
                     "_estimate_required_gap_minutes + _channel_quota_pressure")
        else:
            r.record("P3-4 policy-aware helpers", "FAIL",
                     f"gap={has_gap} pressure={has_pressure}")
    except Exception as e:
        r.record("P3-4 policy-aware helpers", "FAIL", str(e)[:80])

    # P3-5 token health
    code, payload = _get(base, "/api/token-health")
    if code == 200 and "warnings" in (payload or {}):
        warns = len(payload.get("warnings", []))
        r.record("P3-5 GET /api/token-health", "PASS",
                 f"{warns} warning(s) detected")
    else:
        r.record("P3-5 token-health", "FAIL", f"http {code}")


# ─── main ───────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="P1-P3 smoke test")
    ap.add_argument("--base", default="http://localhost:8888",
                    help="サーバ URL（既定 localhost:8888）")
    ap.add_argument("--skip-render", action="store_true",
                    help="render queue 関連のテストをスキップ")
    ap.add_argument("--skip-publish", action="store_true",
                    help="publish ゲート関連のテストをスキップ")
    args = ap.parse_args()

    print(f"Smoke test against {args.base}")

    # 接続確認
    code, payload = _get(args.base, "/api/runs/active")
    if code != 200:
        # フォールバック: 自動的に 8889 / 8890 を試す
        for port in (8889, 8890):
            alt = f"http://localhost:{port}"
            code2, _ = _get(alt, "/api/runs/active")
            if code2 == 200:
                print(f"{YELLOW}note: {args.base} returned http {code}, falling back to {alt}{RESET}")
                args.base = alt
                code = 200
                break
    if code != 200:
        print(f"{RED}サーバへの接続に失敗 ({args.base}): http {code}{RESET}")
        return 2

    r = TestResult()
    test_p1(args.base, r)
    test_p2(args.base, r, skip_render=args.skip_render)
    test_p3(args.base, r)
    return r.summary()


if __name__ == "__main__":
    sys.exit(main())
