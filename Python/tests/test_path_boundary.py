"""S02 / S03: 動画編集プレビューと画像合成 API が登録チャンネルの vol フォルダ外に出ないことを検証する。"""
try:
    from . import _isolation  # noqa: F401  テスト隔離（tests パッケージとして読まれた場合）
except ImportError:
    import _isolation  # noqa: F401  テスト隔離（`discover tests` で top-level として読まれた場合）
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

# 設定・Drive の隔離は _isolation が行う（APP_CONFIG_DIR 等と HOME を一時ディレクトリへ）。
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
        # 本物の設定を守るガード: 設定パスが環境変数で指定した隔離ディレクトリ配下にあり、
        # かつ実ユーザーの ~/.config 配下でないことを確認する（tests/__init__.py が隔離を用意する）。
        real_config = (Path(os.environ.get("ORIGINAL_HOME") or "") / ".config") if os.environ.get("ORIGINAL_HOME") else None
        isolated = [Path(v) for k in ("APP_CONFIG_DIR", "STUDIO_CONFIG_DIR") if (v := os.environ.get(k))]
        for target in (Path(app_core.CHANNELS_CONFIG), Path(app_core.DASHBOARD_CONFIG)):
            if real_config is not None and app_core.path_is_within(target, real_config):
                raise unittest.SkipTest(f"設定パス {target} が実ユーザーの ~/.config 配下のため API テストを skip（本物の設定を守る）")
            if not any(app_core.path_is_within(target, d) for d in isolated):
                raise unittest.SkipTest(f"設定パス {target} が APP_CONFIG_DIR / STUDIO_CONFIG_DIR 配下でないため API テストを skip")
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

    def test_run_rejects_internal_keys_and_filter_injection(self):
        import routers.video_edit as ve
        base = {"video_name": self.vol.name, "input_path": str(self.vol / "clip.mp4")}
        for op, params, code in (
            ("extract_frame", {"time": 1, "_cleanup": [str(self.outside)]}, 400),
            ("convert", {"resolution": "1920:1080[a];movie=/etc/passwd[b];[a][b]overlay"}, 400),
            ("convert", {"format": "mp4;rm -rf /"}, 400),
            ("burn_overlay", {"overlay_type": "text", "text": "HI", "position": "0:0;movie=x"}, 400),
            ("bogus", {}, 400),
        ):
            r = self.client.post("/api/video-edit/run", json={**base, "operation": op, "params": params})
            self.assertEqual(r.status_code, code, (op, params, r.text))
        cleaned = ve._sanitize_params("convert", {"resolution": "1280x720", "fps": "30", "format": "MP4", "unknown": 1})
        self.assertEqual(cleaned, {"resolution": "1280x720", "fps": 30.0, "format": "mp4"})

    def test_burn_overlay_text_cannot_inject_filters(self):
        import routers.video_edit as ve
        import app_video_edit as ave
        evil = "x'[t];movie=" + str(self.outside) + "[m];[t][m]overlay,drawtext=text='y"
        params = ve._sanitize_params("burn_overlay", {"overlay_type": "text", "text": evil, "position": "center", "size": 40, "opacity": 1})
        params["input_path"] = str(self.vol / "clip.mp4")
        out = self.vol / "out.mp4"
        cmd = ave.build_command("burn_overlay", params, out)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("textfile=", vf)
        self.assertIn(":expansion=none:", vf)
        self.assertNotIn("movie=", vf)
        self.assertNotIn("'", Path(params["_cleanup"][0]).name)
        self.assertNotIn(str(self.outside), " ".join(cmd))
        text_file = Path(params["_cleanup"][0])
        self.assertEqual(text_file.read_text(encoding="utf-8"), evil)
        text_file.unlink()
        with self.assertRaises(HTTPException):
            ve._sanitize_params("trim", {"start": "nan"})
        with self.assertRaises(HTTPException):
            ve._sanitize_params("loop_to_duration", {"duration": "inf"})
        self.assertFalse(ve._sanitize_params("trim", {"reencode": "false"})["reencode"])
        with self.assertRaises(HTTPException):
            ve._sanitize_params("loop_to_duration", {"duration": 1e6})
        pct = ve._sanitize_params("burn_overlay", {"overlay_type": "text", "text": "50%OFF"})
        pct["input_path"] = str(self.vol / "clip.mp4")
        cmd2 = ave.build_command("burn_overlay", pct, self.vol / "out2.mp4")
        tf = Path(pct["_cleanup"][0]); self.assertEqual(tf.read_text(encoding="utf-8"), "50%OFF"); tf.unlink()

    def test_exported_mp4_allowed_only_for_reading(self):
        import routers.video_edit as ve
        ext_dir = Path(self.tmp.name) / "ssd"
        ext_dir.mkdir()
        exported = ext_dir / "QA_vol2.mp4"
        exported.write_bytes(b"\x00" * 8)
        with unittest.mock.patch.object(ve, "_find_exported_mp4", return_value=exported), \
             unittest.mock.patch.object(ve, "_resolve_external_export_dir", return_value=ext_dir), \
             unittest.mock.patch.object(ve, "get_dashboard_config", return_value={"channel_folder": str(self.root)}):
            self.assertEqual(ve._resolve_media(self.vol, self.vol.name, str(exported), allow_exported=True), exported.resolve())
            with self.assertRaises(HTTPException):
                ve._resolve_media(self.vol, self.vol.name, str(exported))
            other = ext_dir / "QA_vol3.mp4"
            other.write_bytes(b"\x00" * 8)
            with unittest.mock.patch.object(ve, "_find_exported_mp4", return_value=other):
                with self.assertRaises(HTTPException):
                    ve._resolve_media(self.vol, self.vol.name, str(other), allow_exported=True)
        # export_path の外（別領域）にある同名 mp4 は許可しない
        stray = Path(self.tmp.name) / "elsewhere" / "QA_vol2.mp4"
        stray.parent.mkdir(); stray.write_bytes(b"\x00" * 8)
        with unittest.mock.patch.object(ve, "_find_exported_mp4", return_value=stray), \
             unittest.mock.patch.object(ve, "_resolve_external_export_dir", return_value=ext_dir), \
             unittest.mock.patch.object(ve, "get_dashboard_config", return_value={"channel_folder": str(self.root)}):
            with self.assertRaises(HTTPException):
                ve._resolve_media(self.vol, self.vol.name, str(stray), allow_exported=True)

    def test_render_dual_thumbnail_refuses_symlinked_vol_output(self):
        import io
        from PIL import Image
        Image.new("RGB", (64, 64), (9, 9, 9)).save(self.vol / "vol2.png")
        victim = Path(self.tmp.name) / "victim_vol.jpg"
        victim.write_bytes(b"orig")
        os.symlink(victim, self.vol / "vol2.jpg")
        try:
            r = self.client.post("/api/image-compositor/render-dual-thumbnail", json={"video_name": self.vol.name, "scene_text": "HI"})
            self.assertEqual(r.status_code, 400, r.text)
            self.assertEqual(victim.read_bytes(), b"orig")
        finally:
            (self.vol / "vol2.jpg").unlink(missing_ok=True)
            (self.vol / "vol2.png").unlink(missing_ok=True)

    def test_image_compositor_rejects_escaping_subdir_and_symlinked_output(self):
        r = self.client.post("/api/image-compositor/render-dual-thumbnail", json={"video_name": self.vol.name, "image_subdir": "../../"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/image-compositor/render-dual-thumbnail", json={"video_name": self.vol.name, "image_subdir": str(Path(self.tmp.name))})
        self.assertEqual(r.status_code, 400)
        target = Path(self.tmp.name) / "victim.jpg"
        target.write_bytes(b"orig")
        os.symlink(target, self.vol / "linked.jpg")
        png = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
        r = self.client.post("/api/image-compositor/save-canvas", json={"video_name": self.vol.name, "filename": "linked.jpg", "data_url": png})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(target.read_bytes(), b"orig")
        r = self.client.post("/api/image-compositor/save-canvas", json={"video_name": self.vol.name, "filename": "fresh.png", "data_url": png})
        self.assertEqual(r.status_code, 200, r.text)

    def test_scene_text_generate_rejects_arbitrary_paths(self):
        r = self.client.post("/api/scene-text/generate", json={"image_path": str(self.outside)})
        self.assertIn(r.status_code, (400, 403))
        r = self.client.post("/api/scene-text/generate", json={"video_folder": str(self.adjacent)})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/scene-text/generate", json={"video_name": self.vol.name, "image_path": "../secret.txt"})
        self.assertEqual(r.status_code, 400)

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
