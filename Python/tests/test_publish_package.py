"""段階 3: 共通 upload パッケージ（input_hash、resumable upload のセッション通知・再試行・結果不明）。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from publish import __version__, input_hash as ih, upload_video as pub  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402


def _http_error(status: int, content: bytes = b"{}") -> HttpError:
    class Resp(dict):
        pass
    resp = Resp(); resp.status = status; resp.reason = "x"
    return HttpError(resp, content)


class FakeRequest:
    def __init__(self, chunks, *, fail=None, response=None):
        self.chunks = list(chunks)
        self.fail = fail
        self.response = response or {"id": "vid1"}
        self.resumable_uri = None
        self.resumable_progress = 0
        self.calls = 0

    def next_chunk(self):
        self.calls += 1
        if self.resumable_uri is None:
            self.resumable_uri = "https://upload.example/session/1"
        if self.fail and self.calls == self.fail[0]:
            raise self.fail[1]
        if self.chunks:
            frac = self.chunks.pop(0)
            self.resumable_progress = int(frac * 100)
            class St:  # noqa
                def __init__(self, f): self.f = f
                def progress(self): return self.f
            return St(frac), None
        return None, self.response


class FakeYouTube:
    def __init__(self, request):
        self.request = request
        self.thumb_calls = []
        self.insert_kwargs = None

    def videos(self):
        yt = self
        class V:
            def insert(self, **kw):
                yt.insert_kwargs = kw
                return yt.request
        return V()

    def thumbnails(self):
        yt = self
        class T:
            def set(self, **kw):
                yt.thumb_calls.append(kw)
                class C:
                    def execute(self_inner): return {}
                return C()
        return T()


class InputHashTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ih_"))
        self.video = self.root / "v.mp4"; self.video.write_bytes(b"\x00" * 1000)
        self.thumb = self.root / "t.jpg"; self.thumb.write_bytes(b"jpg")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_hash_is_stable_and_ignores_schedule_but_tracks_inputs(self):
        kw = dict(video_path=self.video, thumbnail_path=self.thumb, title="T", description="D", tags=["a", "b"], localizations={"en": {"title": "T"}})
        h1 = ih.compute_input_hash(**kw)
        self.assertEqual(h1, ih.compute_input_hash(**kw))
        self.assertEqual(len(h1), 64)
        self.assertNotEqual(h1, ih.compute_input_hash(**{**kw, "title": "T2"}))
        self.thumb.write_bytes(b"jpg2")
        self.assertNotEqual(h1, ih.compute_input_hash(**kw))
        big = self.root / "big.mp4"; big.write_bytes(b"a" * (9 * 1024 * 1024))
        d1 = ih.file_digest(big)
        with big.open("r+b") as fh:
            fh.seek(4 * 1024 * 1024 + 100); fh.write(b"z")  # 先頭/末尾 4 MiB の外の変更は指紋に出ない（サイズ + 両端）
        self.assertEqual(d1, ih.file_digest(big))
        with big.open("r+b") as fh:
            fh.seek(0); fh.write(b"z")
        self.assertNotEqual(d1, ih.file_digest(big))
        self.assertEqual(__version__, "2026-10-26.1")


class UploadTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="up_"))
        self.video = self.root / "v.mp4"; self.video.write_bytes(b"\x00" * 10)
        self.thumb = self.root / "t.jpg"; self.thumb.write_bytes(b"jpg")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _req(self, **kw):
        return pub.UploadRequest(video_path=self.video, title="x" * 120, description="d", tags=["t"], privacy="public",
                                 publish_at="2026-09-01T03:00:00Z", thumbnail_path=self.thumb, localizations={"en": {"title": "x"}}, **kw)

    def test_body_forces_private_with_publish_at_and_truncates_title(self):
        body, parts = pub.build_body(self._req())
        self.assertEqual(body["status"]["privacyStatus"], "private")
        self.assertEqual(body["status"]["publishAt"], "2026-09-01T03:00:00Z")
        self.assertEqual(len(body["snippet"]["title"]), 100)
        self.assertEqual(parts, ["snippet", "status", "localizations"])

    def test_upload_reports_session_and_sets_thumbnail(self):
        yt = FakeYouTube(FakeRequest([0.3, 0.7]))
        sessions = []
        res = pub.upload(yt, self._req(), on_session=lambda uri, off: sessions.append((uri, off)), media_factory=lambda *a, **k: object())
        self.assertEqual(res.video_id, "vid1")
        self.assertEqual(res.privacy, "private")
        self.assertEqual(res.publish_at, "2026-09-01T03:00:00Z")
        self.assertFalse(res.thumbnail_set)  # サムネは upload() では設定しない（結果記録後に set_thumbnail）
        self.assertEqual(yt.thumb_calls, [])
        pub.set_thumbnail(yt, res.video_id, self.thumb, media_factory=lambda *a, **k: object())
        self.assertEqual(yt.thumb_calls[0]["videoId"], "vid1")
        self.assertEqual(sessions[0][0], "https://upload.example/session/1")
        self.assertEqual([s[1] for s in sessions], [30, 70])
        self.assertEqual(yt.insert_kwargs["part"], "snippet,status,localizations")

    def test_retryable_and_unknown(self):
        yt = FakeYouTube(FakeRequest([0.5], fail=(2, _http_error(503))))
        with self.assertRaises(pub.RetryableUploadError):
            pub.upload(yt, self._req(), media_factory=lambda *a, **k: object())
        yt = FakeYouTube(FakeRequest([0.5], fail=(2, _http_error(403, b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}'))))
        with self.assertRaises(pub.RetryableUploadError):
            pub.upload(yt, self._req(), media_factory=lambda *a, **k: object())
        # 再開したセッションが失効（410）→ 結果不明（成功にも再試行にもしない）
        yt = FakeYouTube(FakeRequest([], fail=(1, _http_error(410))))
        with self.assertRaises(pub.UploadResultUnknown):
            pub.upload(yt, self._req(), resume={"session_uri": "https://upload.example/old", "offset": 5}, media_factory=lambda *a, **k: object())
        # 新規送信での 410 は結果不明ではなく通常の失敗
        yt = FakeYouTube(FakeRequest([], fail=(1, _http_error(410))))
        with self.assertRaises(HttpError):
            pub.upload(yt, self._req(), media_factory=lambda *a, **k: object())
        # resume は URI と offset を request に設定し、最初に状態照会させる
        req = FakeRequest([])
        yt = FakeYouTube(req)
        pub.upload(yt, self._req(), resume={"session_uri": "https://upload.example/s9", "offset": 7}, media_factory=lambda *a, **k: object())
        self.assertEqual(req.resumable_uri, "https://upload.example/s9")
        self.assertTrue(getattr(req, "_in_error_state", False))
        # 送信中の通信例外: セッションがあれば再試行（resume で結果が分かる）、無ければそのまま失敗
        import socket
        yt = FakeYouTube(FakeRequest([0.5], fail=(2, socket.timeout("timed out at https://upload.example/session/1?x=1"))))
        with self.assertRaises(pub.RetryableUploadError) as cm:
            pub.upload(yt, self._req(), media_factory=lambda *a, **k: object())
        self.assertNotIn("upload.example", str(cm.exception))  # セッション URI は例外文に残さない
        # 最初の PUT で落ちても、セッション URI は on_session で残す
        sessions = []
        yt = FakeYouTube(FakeRequest([], fail=(1, socket.timeout("timed out"))))
        with self.assertRaises(pub.RetryableUploadError):
            pub.upload(yt, self._req(), on_session=lambda u, o: sessions.append(u), media_factory=lambda *a, **k: object())
        self.assertEqual(sessions, ["https://upload.example/session/1"])
        req = FakeRequest([], fail=(1, ConnectionError("reset")))
        req.resumable_uri = None
        class NoSession(FakeRequest):
            def next_chunk(self):
                raise ConnectionError("reset before session")
        yt = FakeYouTube(NoSession([]))
        with self.assertRaises(ConnectionError):
            pub.upload(yt, self._req(), media_factory=lambda *a, **k: object())


class RealHttpRequestResumeTest(unittest.TestCase):
    """googleapiclient の本物の HttpRequest で、保存した session URI と offset からの再開（状態照会 → 続き → 完了）を確かめる。"""

    def test_resume_queries_status_then_completes(self):
        import json
        from googleapiclient.http import HttpMockSequence, HttpRequest, MediaFileUpload
        root = Path(tempfile.mkdtemp(prefix="resume_"))
        try:
            video = root / "v.mp4"; video.write_bytes(b"x" * 1000)
            http = HttpMockSequence([
                ({"status": "308", "range": "0-499"}, ""),            # 状態照会: 500 バイトまで受信済み
                ({"status": "200"}, json.dumps({"id": "vidResumed"})),  # 残りを送って完了
            ])
            media = MediaFileUpload(str(video), mimetype="video/mp4", resumable=True, chunksize=256 * 1024)
            calls = []

            class Req(HttpRequest):
                def next_chunk(self, http=None, num_retries=0):
                    calls.append((self.resumable_uri, self.resumable_progress, getattr(self, "_in_error_state", None)))
                    return super().next_chunk(http=http, num_retries=num_retries)

            class YT:
                def videos(self_inner):
                    class V:
                        def insert(self_v, **kw):
                            return Req(http, lambda resp, content: json.loads(content), "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable",
                                       method="POST", body=json.dumps(kw["body"]), headers={"content-type": "application/json"}, resumable=kw["media_body"])
                    return V()
            req = pub.UploadRequest(video_path=video, title="t", publish_at="2026-09-01T03:00:00Z")
            sessions = []
            res = pub.upload(YT(), req, resume={"session_uri": "https://upload.example/session/9", "offset": 100},
                             on_session=lambda u, o: sessions.append((u, o)), media_factory=lambda *a, **k: media)
            self.assertEqual(res.video_id, "vidResumed")
            self.assertEqual(calls[0][0], "https://upload.example/session/9")
            self.assertTrue(calls[0][2])  # 最初の呼び出しは状態照会（_in_error_state）
            self.assertEqual(sessions[0], ("https://upload.example/session/9", 500))  # 照会で offset が 500 に補正される
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
