"""vps_runs（Mac 側 runs クライアント）と studio.py の run / run-status / run-resume / run-cancel。VPS は呼ばず opener を差し替える。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vps_runs  # noqa: E402
import exit_codes  # noqa: E402


class _Res:
    def __init__(self, body, status=200):
        self._body = json.dumps(body).encode("utf-8")
        self.status = status
    def read(self):
        return self._body
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


def _opener(handler):
    def open_fn(req, timeout=0):
        return handler(req)
    return open_fn


SETTINGS = {"base_url": "https://vps.example", "ai_token": "ai-test-token-not-real"}


class ClientTest(unittest.TestCase):
    def test_not_configured(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = Path(t) / "worker.json"
            with patch.dict(os.environ, {"APP_WORKER_CONFIG": str(cfg)}):
                with self.assertRaises(vps_runs.VpsRunsError) as cm:
                    vps_runs.load_settings()
                self.assertEqual(cm.exception.code, "vps_not_configured")
                cfg.write_text(json.dumps({"base_url": "https://v", "worker_token": "w"}), encoding="utf-8")
                with self.assertRaises(vps_runs.VpsRunsError):
                    vps_runs.load_settings()  # worker_token だけでは使わない（ai_token 必須）
                cfg.write_text(json.dumps({"base_url": "https://v/", "ai_token": "a"}), encoding="utf-8")
                self.assertEqual(vps_runs.load_settings(), {"base_url": "https://v", "ai_token": "a"})
                cfg.write_text("[]", encoding="utf-8")
                with self.assertRaises(vps_runs.VpsRunsError):
                    vps_runs.load_settings()

    def test_create_run_sends_workflow_version_and_request_id(self):
        seen = {}
        def handler(req):
            seen["url"] = req.full_url
            seen["method"] = req.get_method()
            seen["auth"] = req.get_header("Authorization")
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _Res({"id": 7, "state": "queued", "attempts": 1, "request_id": seen["body"]["request_id"]})
        r = vps_runs.create_run("edm", 12, request_id="req-fixed-0001", from_step="rename", settings=SETTINGS, opener=_opener(handler))
        self.assertEqual((r["id"], r["state"], r["_status"]), (7, "queued", 200))
        self.assertEqual(seen["url"], "https://vps.example/api/runs")
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["auth"], "Bearer ai-test-token-not-real")
        self.assertEqual(seen["body"]["workflow_version"], exit_codes.WORKFLOW_VERSION)
        self.assertEqual(seen["body"]["request_id"], "req-fixed-0001")
        self.assertEqual(seen["body"]["from_step"], "rename")
        r2 = vps_runs.create_run("edm", 12, request_id="req-fixed-0001", settings=SETTINGS, opener=_opener(handler))
        self.assertEqual(r2["request_id"], "req-fixed-0001")  # 固定の request_id は同じまま送られる
        auto = vps_runs.new_request_id()
        self.assertTrue(auto.startswith("req-") and len(auto) > 20)

    def test_http_errors_become_codes(self):
        def handler(req):
            raise urllib.error.HTTPError(req.full_url, 409, "conflict", {}, io.BytesIO(
                json.dumps({"detail": {"error": "workflow_version_mismatch", "expected": "x", "got": "y"}}).encode()))
        with self.assertRaises(vps_runs.VpsRunsError) as cm:
            vps_runs.get_run(1, settings=SETTINGS, opener=_opener(handler))
        self.assertEqual((cm.exception.code, cm.exception.status), ("workflow_version_mismatch", 409))
        def h404(req):
            raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, io.BytesIO(b'{"detail":"Run not found"}'))
        with self.assertRaises(vps_runs.VpsRunsError) as cm:
            vps_runs.cancel_run(9, settings=SETTINGS, opener=_opener(h404))
        self.assertEqual(cm.exception.code, "not_found")
        def hdown(req):
            raise urllib.error.URLError("refused")
        with self.assertRaises(vps_runs.VpsRunsError) as cm:
            vps_runs.list_runs(settings=SETTINGS, opener=_opener(hdown))
        self.assertEqual(cm.exception.code, "vps_unreachable")

    def test_list_query_and_resume_payload(self):
        seen = {}
        def handler(req):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode("utf-8")) if req.data else None
            return _Res({"items": [], "total": 0} if "/api/runs?" in req.full_url else {"id": 3, "state": "queued"})
        vps_runs.list_runs(channel_key="edm", state="blocked", limit=5, settings=SETTINGS, opener=_opener(handler))
        self.assertIn("channel_key=edm", seen["url"])
        self.assertIn("state=blocked", seen["url"])
        vps_runs.resume_run(3, from_step="export", settings=SETTINGS, opener=_opener(handler))
        self.assertEqual(seen["body"], {"from_step": "export"})
        vps_runs.resume_run(3, settings=SETTINGS, opener=_opener(handler))
        self.assertEqual(seen["body"], {})


class StudioIntentTest(unittest.TestCase):
    def _args(self, **kw):
        base = dict(vol=None, resolve_num=None, channel="edm", request_id=None, run_intent="pipeline", from_step=None,
                    run_id=None, state=None, json=True)
        base.update(kw)
        return Namespace(**base)

    def test_run_status_resume_cancel_exit_codes(self):
        import studio
        calls = []
        def fake_request(method, path, *, payload=None, query=None, **kw):
            calls.append((method, path, payload, query))
            if path == "/api/runs" and method == "POST":
                return {"id": 11, "state": "queued", "attempts": 1, "_status": 200}
            if path.endswith("/resume"):
                return {"id": 11, "state": "queued", "attempts": 2, "_status": 200}
            if path.endswith("/cancel"):
                return {"id": 11, "state": "cancelled", "_status": 200}
            if path == "/api/runs/11":
                return {"id": 11, "state": "blocked", "blocked_reason": "selection_pending", "_status": 200}
            if path == "/api/runs/12":
                return {"id": 12, "state": "failed", "last_error": "x", "_status": 200}
            return {"items": [], "total": 0, "_status": 200}
        out = io.StringIO()
        with patch("vps_runs.request", fake_request), patch("sys.stdout", out):
            self.assertEqual(studio.run_vps_runs(self._args(intent_name="run", vol="12", request_id="req-fixed-0002")), 0)
            self.assertEqual(studio.run_vps_runs(self._args(intent_name="run-status", run_id=11)), 79)   # blocked は待機
            self.assertEqual(studio.run_vps_runs(self._args(intent_name="run-status", run_id=12)), 1)    # failed
            self.assertEqual(studio.run_vps_runs(self._args(intent_name="run-resume", run_id=11, from_step="rename")), 0)
            self.assertEqual(studio.run_vps_runs(self._args(intent_name="run-cancel", run_id=11)), 0)
            self.assertEqual(studio.run_vps_runs(self._args(intent_name="run-resume")), 2)             # --run-id なし
            self.assertEqual(studio.run_vps_runs(self._args(intent_name="run", vol=None)), 2)
        lines = [json.loads(l) for l in out.getvalue().strip().splitlines()]
        self.assertEqual(lines[0]["id"], 11)
        self.assertTrue(all("_status" not in l for l in lines))
        self.assertEqual(calls[0][2]["request_id"], "req-fixed-0002")
        self.assertEqual(calls[0][2]["channel_key"], "edm")
        self.assertEqual(calls[3][2], {"from_step": "rename"})

    def test_main_via_argparse_sends_pipeline_intent(self):
        """argparse を通した経路: --intent の格納先が位置引数 intent と衝突しない。"""
        import studio
        calls = []
        def fake_request(method, path, *, payload=None, query=None, **kw):
            calls.append((method, path, payload))
            return {"id": 21, "state": "queued", "attempts": 1, "intent": "pipeline", "_status": 201}
        out = io.StringIO()
        with patch("vps_runs.request", fake_request), patch("sys.stdout", out), \
                patch.object(studio, "get_active_channel_info", lambda: {"id": "edm"}):
            with patch("sys.argv", ["studio.py", "run", "--vol", "12", "--json"]):
                self.assertEqual(studio.main(), 0)
            with patch("sys.argv", ["studio.py", "run", "--vol", "12", "--intent", "qa", "--request-id", "req-fixed-0003", "--json"]):
                self.assertEqual(studio.main(), 0)
            with patch("sys.argv", ["studio.py", "run", "--vol", "abc", "--json"]):
                self.assertEqual(studio.main(), 2)
            with patch("sys.argv", ["studio.py", "run", "--vol", "12", "--request-id", "short", "--json"]):
                self.assertEqual(studio.main(), 2)
            with patch("sys.argv", ["studio.py", "run", "--vol", "12", "--json", "--dry-run"]):
                self.assertEqual(studio.main(), 0)  # 送らない
            with patch("sys.argv", ["studio.py", "run-cancel", "--run-id", "5", "--json", "--dry-run"]):
                self.assertEqual(studio.main(), 0)
        self.assertEqual(calls[0][2]["intent"], "pipeline")
        self.assertEqual(calls[1][2]["intent"], "qa")
        self.assertEqual(calls[1][2]["request_id"], "req-fixed-0003")
        self.assertEqual(len(calls), 2)
        lines = [json.loads(l) for l in out.getvalue().strip().splitlines()]
        self.assertEqual((lines[0]["intent"], lines[0]["run_intent"]), ("run", "pipeline"))
        dry = [l for l in lines if l.get("dry_run")]
        self.assertEqual(len(dry), 2)
        self.assertEqual((dry[0]["method"], dry[0]["path"], dry[0]["payload"]["intent"]), ("POST", "/api/runs", "pipeline"))
        self.assertEqual(dry[1]["path"], "/api/runs/5/cancel")
        self.assertNotIn("ai_token", json.dumps(dry))

    def test_cli_not_configured_exits_2(self):
        with tempfile.TemporaryDirectory() as t:
            env = dict(os.environ, APP_WORKER_CONFIG=str(Path(t) / "nope.json"))
            r = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "studio.py"), "run-status", "--run-id", "1", "--json"],
                               env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, r.stderr[-400:])
            last = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertEqual(last["error"], "vps_not_configured")


if __name__ == "__main__":
    unittest.main()
