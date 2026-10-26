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
        self.assertEqual(notify.infer_kind("[待機] vol.3 楽曲選定待ち（失敗ではありません）"), "action_required")
        self.assertEqual(notify.infer_kind("日次: 成功 5 件 / 失敗 0 件"), "info")
        self.assertEqual(notify.infer_kind("unresolved_errors=0"), "info")
        self.assertEqual(notify.infer_kind("二重投稿ガード: 既存なし（50 件照合）"), "info")
        self.assertEqual(notify.infer_kind("エラーが発生しました: 例外"), "alert")
        self.assertEqual(notify.infer_kind("トークンが期限切れです"), "alert")
        self.assertEqual(notify.infer_kind("自動更新を一時停止中です"), "action_required")

    def test_store_first_then_migrate_and_validation(self):
        with self._vps():
            r = notify.notify("ok path")
        self.assertEqual(r["delivered_to"], "vps")
        st = notify.local_status()
        self.assertEqual((st["pending"], st["migrated"]), (0, 1))  # 先に保存し、届いたら migrated
        # 不正な event は送らず拒否の印（flush の対象外）
        with self._vps():
            r = notify.notify("   ")
        self.assertEqual((r["delivered_to"], r["error"]), ("none", "empty_body"))
        with self._vps():
            r = notify.notify("x", event_id="short")
        self.assertEqual(r["error"], "invalid_event_id")
        with self._vps():
            r = notify.notify("x", vol=0)
        self.assertEqual(r["error"], "invalid_vol")
        self.assertEqual(notify.local_status()["rejected"], 3)
        self.assertEqual(len(self.posted), 1)
        # flush: 一括が 422 なら 1 件ずつ送り、拒否された event だけ除外する
        with self._vps(fail=vls.LedgerUnavailable("down")):
            notify.notify("good one", event_id="mac-good-0001")
            notify.notify("bad one", event_id="mac-bad-00001")
        calls = []
        def post(events):
            calls.append([e["event_id"] for e in events])
            if any(e["event_id"] == "mac-bad-00001" for e in events):
                raise vls.LedgerHttpError(422, "validation")
            return {"accepted": [e["event_id"] for e in events], "duplicate": []}
        with patch.multiple(vls, enabled=lambda: True, post_notify=post, resolve_channel_key=lambda: "edm"):
            out = notify.flush_local()
        self.assertEqual((out["ok"], out["migrated"], out["rejected"], out["remaining"]), (True, 1, 1, 0))
        self.assertEqual(calls[0], ["mac-good-0001", "mac-bad-00001"])
        self.assertEqual(sorted(len(c) for c in calls[1:]), [1, 1])

    def test_corrupted_queue_is_moved_aside(self):
        q = notify.queue_path()
        q.parent.mkdir(parents=True, exist_ok=True)
        q.write_bytes(b"this is not a sqlite file" * 100)
        with self._vps(fail=vls.LedgerUnavailable("down")):
            r = notify.notify("after corruption")
        self.assertEqual(r["delivered_to"], "local")
        self.assertEqual(notify.local_status()["pending"], 1)
        self.assertEqual(len(list(q.parent.glob("q.sqlite.corrupt-*"))), 1)
        self.assertEqual(oct(q.stat().st_mode & 0o777), "0o600")

    def test_digest_and_app_notify_use_notify(self):
        import app_digest
        seen = []
        with patch.object(app_digest, "build_digest", return_value={"embed": {"title": "日次", "description": "d", "fields": [{"name": "成功", "value": "3"}]}}), \
             patch.object(notify, "notify", lambda body, **kw: seen.append((body, kw)) or {"delivered_to": "vps", "event_id": "e"}):
            data = app_digest.send_digest(test=False)
        self.assertEqual(seen[0][0], "日次\nd\n成功: 3")
        self.assertEqual(seen[0][1]["kind"], "info")
        self.assertTrue(seen[0][1]["event_id"].startswith("digest-"))
        self.assertTrue(data["notify"]["sent"] and data["discord"]["sent"])
        src = (Path(__file__).resolve().parents[1] / "app_digest.py").read_text(encoding="utf-8")
        self.assertNotIn("webhook_url", src)
        # app.py の通知はスレッドで送り、呼び出し側を待たせない
        import threading, time
        import app as studio
        gate = threading.Event()
        def slow(body, **kw):
            gate.wait(5); return {"delivered_to": "vps"}
        with patch.object(notify, "notify", slow):
            t0 = time.time(); studio._send_discord_notify("hello"); elapsed = time.time() - t0
        gate.set()
        self.assertLess(elapsed, 1.0)

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
        # Discord へ送信する経路（Webhook URL への curl / urlopen / requests）が Python 側に残っていない
        import re as _re
        for f in ("app.py", "app_pipeline.py", "app_reconcile.py", "app_digest.py", "suno_auto_create.py", "routers/youtube.py"):
            text = (here / f).read_text(encoding="utf-8")
            for line in text.splitlines():
                if "webhook" in line.lower() and _re.search(r"curl|urlopen|requests\.post|httpx", line):
                    self.fail(f"{f}: Discord 送信経路が残っています: {line.strip()[:100]}")


if __name__ == "__main__":
    unittest.main()
