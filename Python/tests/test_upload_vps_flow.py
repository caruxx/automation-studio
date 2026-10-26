"""段階 3: app_youtube.upload_video の VPS 投稿記録連携（update モードは再投稿しない、予約時刻は VPS、接続不能は保留、結果不明は成功にしない）。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_youtube as ay  # noqa: E402
import vps_ledger_sync as vls  # noqa: E402
from publish import upload_video as pub  # noqa: E402


class FakeLedger:
    def __init__(self, begin):
        self.begin = begin
        self.calls = []

    def upload_begin(self, vol, h):
        self.calls.append(("begin", vol, h))
        if isinstance(self.begin, Exception):
            raise self.begin
        return self.begin

    def upload_session(self, vol, h, uri, off, token=None): self.calls.append(("session", uri, off, token))
    def upload_result(self, vol, h, vid, privacy, publish_at): self.calls.append(("result", vid, privacy, publish_at)); return True
    def upload_unknown(self, vol, h, err): self.calls.append(("unknown", err)); return True
    def enabled(self): return self.begin is not None
    LedgerUnavailable = vls.LedgerUnavailable
    LedgerHttpError = vls.LedgerHttpError
    LedgerConfigError = vls.LedgerConfigError


class UploadVpsFlowTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="yt_"))
        self.folder = self.root / "12_test_261001"; self.folder.mkdir()
        (self.folder / "test_vol12.mp4").write_bytes(b"\x00" * 100)
        (self.folder / "youtube_title.txt").write_text("Title", encoding="utf-8")
        (self.folder / "youtube_description.txt").write_text("Desc", encoding="utf-8")
        self.patches = [
            patch.object(ay, "get_credentials", return_value=object()),
            patch.object(ay, "build", return_value=object()),
            patch.object(ay, "assert_expected_channel", return_value={"channel_id": "UC1", "channel_title": "ch"}),
            patch.object(ay, "wait_for_file_stable", return_value=True),
            patch.object(ay, "check_quota_before_upload", return_value=(True, 0, 10000, 10000)),
            patch.object(ay, "record_quota", lambda *a, **k: None),
            patch.object(ay, "record_upload_quota", lambda *a, **k: None),
            patch.object(ay, "quota_used_in_window", lambda *a, **k: 0),
            patch.object(ay, "_acquire_upload_lock", return_value=False),
        ]
        for p in self.patches:
            p.start()
        import app_reconcile
        self.guard = patch.object(app_reconcile, "find_existing_upload", return_value={"exists": False, "checked": {"uploads": 0}})
        self.guard.start()

    def tearDown(self):
        self.guard.stop()
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _run(self, ledger, upload_impl, schedule="2026-10-01T03:00:00Z"):
        with patch.dict(sys.modules, {"vps_ledger_sync": ledger}), patch.object(pub, "upload", upload_impl):
            return ay.upload_video(self.folder, title="Title", schedule=schedule, privacy="private")

    def test_update_mode_does_not_reupload(self):
        ledger = FakeLedger({"mode": "update", "video_id": "vidOLD", "upload_id": 1, "state": "uploaded"})
        called = []
        with patch.object(ay, "update_video_snippet", return_value={"status": "ok"}) as upd:
            vid = self._run(ledger, lambda *a, **k: called.append(1))
        self.assertEqual(vid, "vidOLD")
        self.assertEqual(called, [])
        upd.assert_called_once()
        marker = json.loads((self.folder / "youtube_upload.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["video_id"], "vidOLD")
        self.assertEqual(marker["note"], "update")

    def test_insert_uses_vps_publish_at_not_folder_date(self):
        ledger = FakeLedger({"mode": "insert", "upload_id": 2, "state": "pending", "publish_at": "2026-12-24T03:00:00", "begin_token": "t"})
        seen = {}

        def fake_upload(youtube, req, **kw):
            seen["publish_at"] = req.publish_at
            kw["on_session"]("https://upload.example/s1", 0)
            return pub.UploadResult(video_id="vidNEW", url="u", privacy="private", publish_at=req.publish_at, thumbnail_set=False, elapsed_sec=1.0)
        vid = self._run(ledger, fake_upload, schedule="2026-10-01T03:00:00Z")  # フォルダ由来の予約は使わない
        self.assertEqual(vid, "vidNEW")
        self.assertEqual(seen["publish_at"], "2026-12-24T03:00:00Z")
        kinds = [c[0] for c in ledger.calls]
        self.assertEqual(kinds, ["begin", "session", "result"])
        self.assertEqual(ledger.calls[1], ("session", "https://upload.example/s1", 0, "t"))  # begin_token を添える
        self.assertEqual(ledger.calls[-1][1:], ("vidNEW", "private", "2026-12-24T03:00:00Z"))
        marker = json.loads((self.folder / "youtube_upload.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["schedule"], "2026-12-24T03:00:00Z")
        self.assertEqual(marker["privacy"], "private")

    def test_thumbnail_failure_keeps_result_and_marker(self):
        ledger = FakeLedger({"mode": "insert", "upload_id": 2, "state": "pending", "publish_at": "2026-12-24T03:00:00", "begin_token": "t"})
        (self.folder / "サムネイル.jpg").write_bytes(b"jpg")
        order = []

        def fake_upload(youtube, req, **kw):
            order.append("upload")
            return pub.UploadResult(video_id="vidT", url="u", privacy="private", publish_at=req.publish_at, thumbnail_set=False, elapsed_sec=1.0)

        def boom_thumb(*a, **k):
            order.append("thumb")
            raise RuntimeError("thumbnails.set failed")
        with patch.object(pub, "set_thumbnail", boom_thumb):
            vid = self._run(ledger, fake_upload)
        self.assertEqual(vid, "vidT")
        self.assertEqual(order, ["upload", "thumb"])
        self.assertEqual([c[0] for c in ledger.calls][-1], "result")  # 結果はサムネより先に記録される
        self.assertTrue((self.folder / "youtube_upload.json").exists())

    def test_resume_reads_back_actual_publish_at(self):
        ledger = FakeLedger({"mode": "resume", "upload_id": 3, "state": "uploading", "publish_at": "2026-12-25T03:00:00",
                             "session_ref": "https://upload.example/old", "offset": 50, "begin_token": "t"})

        def fake_upload(youtube, req, **kw):
            return pub.UploadResult(video_id="vidR", url="u", privacy="private", publish_at=req.publish_at, thumbnail_set=False, elapsed_sec=1.0)
        with patch.object(pub, "verify_video", return_value={"privacy_status": "private", "publish_at": "2026-12-24T03:00:00Z"}):
            self._run(ledger, fake_upload)
        self.assertEqual(ledger.calls[-1], ("result", "vidR", "private", "2026-12-24T03:00:00Z"))  # セッション開始時の値を正とする

    def test_config_error_exits_1_not_retry(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(FakeLedger(vls.LedgerConfigError("channel unresolved")), lambda *a, **k: self.fail("x"))
        self.assertEqual(cm.exception.code, 1)

    def test_insert_without_vps_publish_at_refuses_scheduled_upload(self):
        ledger = FakeLedger({"mode": "insert", "upload_id": 2, "state": "pending", "publish_at": None, "begin_token": "t"})
        with self.assertRaises(SystemExit) as cm:
            self._run(ledger, lambda *a, **k: self.fail("must not upload"))
        self.assertEqual(cm.exception.code, 1)
        self.assertFalse((self.folder / "youtube_upload.json").exists())

    def test_vps_unavailable_and_conflicts(self):
        with self.assertRaises(SystemExit) as cm:
            self._run(FakeLedger(vls.LedgerUnavailable("down")), lambda *a, **k: self.fail("must not upload"))
        self.assertEqual(cm.exception.code, ay.EXIT_RETRYABLE)
        with self.assertRaises(SystemExit) as cm:
            self._run(FakeLedger(vls.LedgerHttpError(409, {"error": "upload_in_progress", "upload_id": 5})), lambda *a, **k: self.fail("x"))
        self.assertEqual(cm.exception.code, ay.EXIT_RETRYABLE)
        with self.assertRaises(SystemExit) as cm:
            self._run(FakeLedger(vls.LedgerHttpError(409, {"error": "publish_at_in_past", "publish_at": "x"})), lambda *a, **k: self.fail("x"))
        self.assertEqual(cm.exception.code, 1)

    def test_unknown_result_is_reported_and_not_marked_success(self):
        ledger = FakeLedger({"mode": "resume", "upload_id": 3, "state": "uploading", "publish_at": "2026-12-24T03:00:00",
                             "session_ref": "https://upload.example/old", "offset": 50, "begin_token": "t"})

        def fake_upload(youtube, req, **kw):
            self.assertEqual(kw["resume"], {"session_uri": "https://upload.example/old", "offset": 50})
            raise pub.UploadResultUnknown("session expired at https://upload.example/old?upload_id=abc")
        with self.assertRaises(SystemExit) as cm:
            self._run(ledger, fake_upload)
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(ledger.calls[-1][0], "unknown")
        self.assertNotIn("upload.example", ledger.calls[-1][1])  # セッション URI を VPS の記録に残さない
        self.assertFalse((self.folder / "youtube_upload.json").exists())

    def test_retryable_keeps_session_for_next_attempt(self):
        ledger = FakeLedger({"mode": "insert", "upload_id": 4, "state": "pending", "publish_at": "2026-12-24T03:00:00", "begin_token": "t"})

        def fake_upload(youtube, req, **kw):
            kw["on_session"]("https://upload.example/s2", 1024)
            raise pub.RetryableUploadError("HTTP 503", status=503)
        with self.assertRaises(SystemExit) as cm:
            self._run(ledger, fake_upload)
        self.assertEqual(cm.exception.code, ay.EXIT_RETRYABLE)
        self.assertIn(("session", "https://upload.example/s2", 1024, "t"), ledger.calls)
        self.assertFalse(any(c[0] in ("unknown", "result") for c in ledger.calls))

    def test_legacy_path_without_vps(self):
        ledger = FakeLedger(None)  # VPS 未設定

        def fake_upload(youtube, req, **kw):
            return pub.UploadResult(video_id="vidL", url="u", privacy="private", publish_at=req.publish_at, thumbnail_set=False, elapsed_sec=1.0)
        vid = self._run(ledger, fake_upload, schedule="2026-10-01T03:00:00Z")
        self.assertEqual(vid, "vidL")
        self.assertEqual([c[0] for c in ledger.calls], ["begin"])
        marker = json.loads((self.folder / "youtube_upload.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["schedule"], "2026-10-01T03:00:00Z")


if __name__ == "__main__":
    unittest.main()
