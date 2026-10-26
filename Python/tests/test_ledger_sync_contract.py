"""段階 3: vps_ledger_sync の投稿記録 API が VPS の契約どおりの payload を送るか（urlopen をモック）。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import io
import json
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vps_ledger_sync as vls  # noqa: E402


class _Resp(io.BytesIO):
    status = 200
    def __enter__(self): return self
    def __exit__(self, *a): return False


class LedgerContractTest(unittest.TestCase):
    def setUp(self):
        self.settings = {"base_url": "https://vps.example", "worker_token": "wtok"}
        self.p1 = patch.object(vls, "_load_settings", return_value=self.settings); self.p1.start()
        self.p2 = patch.object(vls, "resolve_channel_key", return_value="edm"); self.p2.start()
        self.p3 = patch.object(vls, "ensure_vol", return_value=42); self.p3.start()
        vls._VOL_CACHE[("edm", 7)] = 42

    def tearDown(self):
        for p in (self.p1, self.p2, self.p3): p.stop()

    def test_begin_session_result_unknown_payloads(self):
        sent = []
        def fake_open(req, timeout=0):
            sent.append((req.get_method(), req.full_url, json.loads(req.data.decode("utf-8")), req.headers.get("Authorization")))
            return _Resp(json.dumps({"mode": "insert", "begin_token": "tok1"}).encode())
        with patch.object(vls.urllib.request, "urlopen", fake_open):
            r = vls.upload_begin(7, "a" * 64)
            vls.upload_begin(7, "a" * 64, begin_token="tok1")
            vls.upload_session(7, "a" * 64, "https://upload/s", 1024, "tok1")
            vls.upload_result(7, "a" * 64, "vid", "private", "2026-12-24T03:00:00Z")
            vls.upload_unknown(7, "a" * 64, "lost")
        self.assertEqual(r["begin_token"], "tok1")
        self.assertEqual(sent[0][0:3], ("POST", "https://vps.example/api/worker/vols/42/upload/begin", {"input_hash": "a" * 64}))
        self.assertEqual(sent[0][3], "Bearer wtok")
        self.assertEqual(sent[1][2], {"input_hash": "a" * 64, "begin_token": "tok1"})
        self.assertEqual(sent[2][1:3], ("https://vps.example/api/worker/vols/42/upload/session",
                                        {"input_hash": "a" * 64, "session_ref": "https://upload/s", "offset": 1024, "begin_token": "tok1"}))
        self.assertEqual(sent[3][2], {"input_hash": "a" * 64, "video_id": "vid", "privacy": "private", "publish_at": "2026-12-24T03:00:00Z"})
        self.assertEqual(sent[4][2], {"input_hash": "a" * 64, "error": "lost"})

    def test_http_409_and_unreachable(self):
        def http409(req, timeout=0):
            raise urllib.error.HTTPError(req.full_url, 409, "conflict", {}, io.BytesIO(json.dumps({"detail": {"error": "upload_in_progress", "upload_id": 9}}).encode()))
        with patch.object(vls.urllib.request, "urlopen", http409):
            with self.assertRaises(vls.LedgerHttpError) as cm:
                vls.upload_begin(7, "a" * 64)
            self.assertEqual(cm.exception.status, 409)
            self.assertEqual(cm.exception.detail["error"], "upload_in_progress")
            self.assertTrue(vls.upload_result(7, "a" * 64, "v", "private", None) is False)  # 4xx は握りつぶして False
        def down(req, timeout=0):
            raise urllib.error.URLError("down")
        with patch.object(vls.urllib.request, "urlopen", down):
            with self.assertRaises(vls.LedgerUnavailable):
                vls.upload_begin(7, "a" * 64)
        with patch.object(vls, "resolve_channel_key", return_value=None):
            with self.assertRaises(vls.LedgerConfigError):
                vls.upload_begin(7, "a" * 64)
        with patch.object(vls, "_load_settings", return_value=None):
            self.assertIsNone(vls.upload_begin(7, "a" * 64))
            self.assertFalse(vls.enabled())


if __name__ == "__main__":
    unittest.main()
