"""段階 3a: Mac の notify（VPS outbox へ送る。届かなければローカル SQLite に退避し、flush で同じ event_id のまま移管）。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notify  # noqa: E402
import vps_ledger_sync as vls  # noqa: E402


class NotifyTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="nq_"))
        os.environ["APP_NOTIFY_QUEUE"] = str(self.root / "q.sqlite")
        self.posted = []

    def tearDown(self):
        os.environ.pop("APP_NOTIFY_QUEUE", None)
        shutil.rmtree(self.root, ignore_errors=True)

    def _vps(self, *, enabled=True, fail=None):
        def post(events):
            if fail:
                raise fail
            self.posted.append(events)
            return {"accepted": [e["event_id"] for e in events], "duplicate": []}
        return patch.multiple(vls, enabled=lambda: enabled, post_notify=post, resolve_channel_key=lambda: "edm")

    def test_infer_kind(self):
        self.assertEqual(notify.infer_kind("❌ [ch] vol.3 失敗"), "alert")
        self.assertEqual(notify.infer_kind("⚠️ ログインが必要です"), "action_required")
        self.assertEqual(notify.infer_kind("vol.3 の作成を開始"), "info")

    def test_sends_to_vps_with_channel_and_never_raises(self):
        with self._vps():
            r = notify.notify("hello", vol=3)
        self.assertEqual(r["delivered_to"], "vps")
        self.assertEqual(self.posted[0][0]["channel_key"], "edm")
        self.assertEqual(self.posted[0][0]["vol"], 3)
        self.assertTrue(self.posted[0][0]["event_id"].startswith("mac-"))
        self.assertEqual(notify.local_status()["pending"], 0)
        # 到達不能 → ローカル退避（例外は出ない）
        with self._vps(fail=vls.LedgerUnavailable("down")):
            r = notify.notify("❌ failed thing", vol=4)
        self.assertEqual((r["delivered_to"], r["kind"]), ("local", "alert"))
        self.assertEqual(notify.local_status()["pending"], 1)
        # VPS 未設定 → ローカル退避
        with self._vps(enabled=False):
            r = notify.notify("x")
        self.assertEqual(r["error"], "vps_not_configured")
        self.assertEqual(notify.local_status()["pending"], 2)
        # ローカルも壊れていても例外を出さない
        with self._vps(fail=RuntimeError("down")), patch.object(notify, "_store_local", side_effect=OSError("disk")):
            r = notify.notify("y")
        self.assertEqual(r["delivered_to"], "none")

    def test_flush_migrates_same_event_id_and_dedupes(self):
        with self._vps(fail=vls.LedgerUnavailable("down")):
            first = notify.notify("queued one", event_id="mac-fixed-0001")
            notify.notify("queued two")
        self.assertEqual(notify.local_status()["pending"], 2)
        with self._vps(enabled=False):
            self.assertEqual(notify.flush_local()["error"], "vps_not_configured")
        with self._vps():
            out = notify.flush_local()
        self.assertEqual((out["migrated"], out["remaining"]), (2, 0))
        self.assertIn("mac-fixed-0001", [e["event_id"] for e in self.posted[0]])  # 同じ event_id のまま移管
        # 2 回目は VPS が duplicate を返しても migrated 扱いで消える（自動昇格ではなく明示操作）
        with self._vps(fail=vls.LedgerUnavailable("down")):
            notify.notify("three", event_id="mac-fixed-0003")
        def dup_post(events):
            return {"accepted": [], "duplicate": [e["event_id"] for e in events]}
        with patch.multiple(vls, enabled=lambda: True, post_notify=dup_post, resolve_channel_key=lambda: "edm"):
            out = notify.flush_local()
        self.assertEqual((out["migrated"], out["duplicate"], out["remaining"]), (0, 1, 0))
        # flush 中に落ちても残る
        with self._vps(fail=vls.LedgerUnavailable("down")):
            notify.notify("four")
            out = notify.flush_local()
        self.assertFalse(out["ok"]); self.assertEqual(out["remaining"], 1)

    def test_post_notify_payload_contract(self):
        import io
        sent = []
        class R(io.BytesIO):
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
        def opener(req, timeout=0):
            sent.append((req.full_url, json.loads(req.data.decode())))
            return R(json.dumps({"accepted": ["mac-1"], "duplicate": []}).encode())
        with patch.object(vls, "_load_settings", return_value={"base_url": "https://vps.example", "worker_token": "w"}), \
             patch.object(vls.urllib.request, "urlopen", opener):
            r = vls.post_notify([{"event_id": "mac-1", "kind": "alert", "body": "b", "run_id": None, "vol": 2, "channel_key": "edm"}])
        self.assertEqual(r["accepted"], ["mac-1"])
        self.assertEqual(sent[0][0], "https://vps.example/api/worker/notify")
        self.assertEqual(sent[0][1], {"events": [{"event_id": "mac-1", "kind": "alert", "body": "b", "vol": 2, "channel_key": "edm"}]})

    def test_call_sites_delegate_to_notify(self):
        import app_pipeline, app_reconcile
        seen = []
        with patch.object(notify, "notify", lambda body, **kw: seen.append((body, kw)) or {"delivered_to": "vps"}):
            app_pipeline._notify_line("❌ pipeline failed")
            r = app_reconcile.notify_duplicate_skip("7", "EDM")
        self.assertTrue(seen[0][0].startswith("❌ pipeline failed"))
        self.assertEqual(seen[1][1]["kind"], "alert")
        self.assertTrue(r["sent"])
        here = Path(__file__).resolve().parents[1]
        src = (here / "app_notify.sh").read_text(encoding="utf-8")
        self.assertNotIn("webhook_url", src)
        self.assertNotIn("curl", src)
        self.assertIn("notify.py", src)
        env = {**os.environ, "APP_NOTIFY_PYTHON": sys.executable}
        proc = subprocess.run(["bash", str(here / "app_notify.sh"), "wrapper test"], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(notify.local_status()["pending"], 1)  # VPS 未設定なので退避される


if __name__ == "__main__":
    unittest.main()
