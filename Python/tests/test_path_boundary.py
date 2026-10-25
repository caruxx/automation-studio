"""S02 / S03: 動画編集プレビューと画像合成 API が登録チャンネルの vol フォルダ外に出ないことを検証する。"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 本物の設定・Drive に触れないよう、app を未 import の場合は一時ディレクトリへ固定する。
# （discover で他のテストが先に app を import 済みのときは、その環境の設定パスを使う。
#   その場合でも setUpClass のガードで本物の ~/.config 配下なら skip する。）
if "app_core" not in sys.modules:
    _TMP = tempfile.mkdtemp(prefix="asb_")
    _CFG = Path(_TMP) / "config"
    _SHARED = Path(_TMP) / "shared"
    _CFG.mkdir()
    _SHARED.mkdir()
    (_CFG / ".migration_completed").write_text("test", encoding="utf-8")  # 旧設定の移行コピーを止める
    os.environ.setdefault("APP_CONFIG_DIR", str(_CFG))
    os.environ.setdefault("STUDIO_CONFIG_DIR", str(_SHARED))
    os.environ.setdefault("APP_SHARED_BASE", str(Path(_TMP) / "drive"))
os.environ.setdefault("APP_ORCH_TICK_ENABLED", "0")
os.environ.setdefault("APP_AUTO_UPDATE_ENABLED", "0")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_core  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as studio  # noqa: E402


class PathHelperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "ch"
        self.vol = self.root / "1_QA_260929"
        (self.vol / "music").mkdir(parents=True)
        (self.vol / "clip.mp4").write_bytes(b"\x00" * 16)
        (self.vol / "music" / "a.mp3").write_bytes(b"\x00" * 16)
        (self.vol / "notes.txt").write_text("x", encoding="utf-8")
        self.outside = Path(self.tmp.name) / "secret.txt"
        self.outside.write_text("secret", encoding="utf-8")
        self.adjacent = Path(self.tmp.name) / "ch_evil" / "1_QA_260929"
        self.adjacent.mkdir(parents=True)
        (self.adjacent / "x.mp4").write_bytes(b"\x00" * 16)
        os.symlink(self.outside, self.vol / "link.mp4")

    def tearDown(self):
        self.tmp.cleanup()

    def test_path_is_within_rejects_adjacent_prefix_and_symlink_escape(self):
        self.assertTrue(app_core.path_is_within(self.vol / "clip.mp4", self.vol))
        self.assertFalse(app_core.path_is_within(self.adjacent / "x.mp4", self.vol))
        self.assertFalse(app_core.path_is_within(self.vol / "link.mp4", self.vol))
        self.assertFalse(app_core.path_is_within(self.outside, self.vol))

    def test_resolve_asset_in_folder(self):
        media = {".mp4", ".mp3"}
        ok = app_core.resolve_asset_in_folder(self.vol, "music/a.mp3", allowed_ext=media)
        self.assertEqual(ok, (self.vol / "music" / "a.mp3").resolve())
        ok_abs = app_core.resolve_asset_in_folder(self.vol, str(self.vol / "clip.mp4"), allowed_ext=media)
        self.assertEqual(ok_abs, (self.vol / "clip.mp4").resolve())
        for bad, code in (("../secret.txt", 400), (str(self.outside), 403), ("link.mp4", 403),
                          (str(self.adjacent / "x.mp4"), 403), ("notes.txt", 400), ("", 400), ("missing.mp4", 404)):
            with self.assertRaises(HTTPException, msg=bad) as ctx:
                app_core.resolve_asset_in_folder(self.vol, bad, allowed_ext=media)
            self.assertEqual(ctx.exception.status_code, code, bad)


class ApiBoundaryTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / "ch"
        cls.vol = cls.root / "2_QA_260930"
        (cls.vol / "music").mkdir(parents=True)
        (cls.vol / "clip.mp4").write_bytes(b"\x00" * 32)
        (cls.vol / "notes.txt").write_text("x", encoding="utf-8")
        cls.outside = Path(cls.tmp.name) / "secret.txt"
        cls.outside.write_text("secret", encoding="utf-8")
        os.symlink(cls.outside, cls.vol / "link.mp4")
        cls.adjacent = Path(cls.tmp.name) / "ch_evil" / "2_QA_260930"
        cls.adjacent.mkdir(parents=True)
        (cls.adjacent / "x.mp4").write_bytes(b"\x00" * 32)
        home_cfg = (Path.home() / ".config").resolve()
        for target in (Path(app_core.CHANNELS_CONFIG), Path(app_core.DASHBOARD_CONFIG)):
            if app_core.path_is_within(target, home_cfg):
                raise unittest.SkipTest("設定パスが本物の ~/.config を指しているため API テストを skip")
        channels = [{"id": "qa", "name": "QA", "folder": str(cls.root), "prefix": "QA"}]
        Path(app_core.CHANNELS_CONFIG).parent.mkdir(parents=True, exist_ok=True)
        Path(app_core.CHANNELS_CONFIG).write_text(json.dumps(channels), encoding="utf-8")
        dash = {"channel_folder": str(cls.root), "channel_name": "QA", "file_prefix": "QA"}
        Path(app_core.DASHBOARD_CONFIG).parent.mkdir(parents=True, exist_ok=True)
        Path(app_core.DASHBOARD_CONFIG).write_text(json.dumps(dash), encoding="utf-8")
        cls.client = TestClient(studio.app, raise_server_exceptions=False)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.tmp.cleanup()

    def _preview(self, **params):
        return self.client.get("/api/video-edit/preview", params=params)

    def test_preview_serves_only_vol_assets(self):
        self.assertEqual(self._preview(video_name=self.vol.name, asset="clip.mp4").status_code, 200)
        self.assertEqual(self._preview(video_name=self.vol.name, path=str(self.vol / "clip.mp4")).status_code, 200)
        self.assertEqual(self._preview(video_name=self.vol.name, asset="../secret.txt").status_code, 400)
        self.assertEqual(self._preview(video_name=self.vol.name, path=str(self.outside)).status_code, 403)
        self.assertEqual(self._preview(video_name=self.vol.name, asset="link.mp4").status_code, 403)
        self.assertEqual(self._preview(video_name=self.vol.name, path=str(self.adjacent / "x.mp4")).status_code, 403)
        self.assertEqual(self._preview(video_name=self.vol.name, asset="notes.txt").status_code, 400)
        self.assertEqual(self._preview(video_name=self.vol.name, asset="missing.mp4").status_code, 404)
        self.assertEqual(self._preview(path=str(self.vol / "clip.mp4")).status_code, 400)
        self.assertIn(self._preview(video_name="../../etc", asset="clip.mp4").status_code, (400, 404))

    def test_run_requires_video_name_and_vol_bound_input(self):
        r = self.client.post("/api/video-edit/run", json={"operation": "extract_frame", "input_path": str(self.vol / "clip.mp4"), "params": {}})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/video-edit/run", json={"operation": "extract_frame", "video_name": self.vol.name, "input_path": str(self.outside), "params": {}})
        self.assertEqual(r.status_code, 403)

    def test_image_compositor_rejects_unregistered_folder(self):
        body = {"video_folder": str(self.adjacent), "template": {"layers": []}}
        r = self.client.post("/api/image-compositor/template", json=body)
        self.assertEqual(r.status_code, 400)
        r = self.client.get("/api/image-compositor/template", params={"video_folder": str(Path(self.tmp.name))})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/image-compositor/template", json={"video_folder": str(self.vol), "template": {"layers": []}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue((self.vol / "thumbnail_layout_template.json").exists())
        r = self.client.post("/api/image-compositor/template", json={"video_name": self.vol.name, "template": {"layers": []}})
        self.assertEqual(r.status_code, 200, r.text)

    def test_thumbnail_upload_validates_images_and_returns_ids(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (8, 8), (1, 2, 3)).save(buf, "PNG")
        r = self.client.post("/api/analysis/thumbnails-upload", files=[("files", ("a.png", buf.getvalue(), "image/png"))])
        self.assertEqual(r.status_code, 200, r.text)
        uid = r.json()["upload_id"]
        self.assertTrue(uid.isalnum())
        self.assertNotIn("path", r.text)
        saved = list((Path(studio._THUMB_UPLOAD_ROOT) / uid).iterdir())
        self.assertEqual([p.name for p in saved], ["00.png"])
        r = self.client.post("/api/analysis/thumbnails-upload", files=[("files", ("evil.png", b"<html>not image</html>", "image/png"))])
        self.assertEqual(r.status_code, 400)

    def test_thumbnail_analysis_rejects_bad_urls_and_unknown_upload_ids(self):
        r = self.client.post("/api/analysis/thumbnails", json={"urls": ["http://127.0.0.1/a.png"]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/analysis/thumbnails", json={"urls": ["https://169.254.169.254/latest/meta-data"]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/analysis/thumbnails", json={"upload_ids": ["../../etc"]})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/analysis/thumbnails", json={"upload_ids": ["0123456789abcdef0123456789abcdef"]})
        self.assertEqual(r.status_code, 404)
        r = self.client.post("/api/analysis/thumbnails", json={"uploaded_paths": [str(self.outside)]})
        self.assertEqual(r.status_code, 400)  # 旧フィールドは無視され、画像なしとして 400


if __name__ == "__main__":
    unittest.main()
