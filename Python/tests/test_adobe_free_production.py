"""Adobeなしの素材生成と、チャンネル設定・既存構成との分離を検証する。"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as studio
import app_core
import app_image_composite as images
import app_pipeline as pipeline
import settings_service


class AdobeFreeProductionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.channel = Path(self.temp.name)
        self.folder = self.channel / "1_QA_260929"
        self.folder.mkdir()
        self.source = self.folder / "vol1.png"
        Image.new("RGB", (640, 480), (24, 36, 52)).save(self.source)
        self.config = {"production_mode": "ffmpeg_only", "export_engine": "ame",
                       "scene_text_enabled": False,
                       "thumbnail_composition": {"title": "After Hours", "darken": 0.1}}

    def tearDown(self):
        self.temp.cleanup()

    def test_images_render_without_adobe_and_preserve_original(self):
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with patch.object(pipeline, "_load_dashboard_config", return_value=self.config):
            self.assertTrue(pipeline.step_psd_composite(1, self.folder, False))
        for name in ["vol1.jpg", "サムネイル.jpg"]:
            with Image.open(self.folder / name) as img:
                self.assertEqual(img.size, (1920, 1080))
                img.verify()
        self.assertNotEqual((self.folder / "vol1.jpg").read_bytes(),
                            (self.folder / "サムネイル.jpg").read_bytes())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)
        self.assertFalse(list(self.folder.glob("*.psd")))
        self.assertFalse(list(self.folder.glob("*.prproj")))

    def test_manual_output_is_reused_and_force_keeps_backup(self):
        images.compose_images(self.folder, 1, self.config)
        thumb = self.folder / "サムネイル.jpg"
        before = thumb.read_bytes()
        with patch.object(images, "render_dual_thumbnail", side_effect=AssertionError("must reuse")):
            self.assertTrue(images.compose_images(self.folder, 1, self.config))
        updated = {**self.config, "thumbnail_composition": {"title": "Next Session"}}
        images.compose_images(self.folder, 1, updated, force=True)
        self.assertNotEqual(before, thumb.read_bytes())
        backups = list((self.folder / ".image_previous").rglob("サムネイル.jpg"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), before)

    def test_failed_second_write_restores_both_previous_images(self):
        images.compose_images(self.folder, 1, self.config)
        outputs = [self.folder / "vol1.jpg", self.folder / "サムネイル.jpg"]
        before = [p.read_bytes() for p in outputs]
        replace = os.replace
        def fail_second(src, dst):
            if Path(dst).name == "サムネイル.jpg":
                raise OSError("simulated write failure")
            return replace(src, dst)
        with patch.object(images.os, "replace", side_effect=fail_second):
            with self.assertRaises(OSError):
                images.compose_images(self.folder, 1,
                    {**self.config, "thumbnail_composition": {"title": "Changed"}}, force=True)
        self.assertEqual([p.read_bytes() for p in outputs], before)
        self.assertFalse(list(self.folder.glob(".*.part")))

    def test_missing_original_cannot_overwrite_existing_background(self):
        bg = self.folder / "vol1.jpg"
        bg.write_bytes(b"manual background")
        self.source.unlink()
        with self.assertRaises(ValueError):
            images.compose_images(self.folder, 1, self.config)
        self.assertEqual(bg.read_bytes(), b"manual background")

    def test_mode_forces_ffmpeg_and_skips_adobe_dispatch(self):
        with patch.object(pipeline, "_load_dashboard_config", return_value=self.config), \
             patch.object(pipeline, "_music_selection_gate", return_value=None), \
             patch.object(pipeline, "_enqueue_and_wait", side_effect=AssertionError("Adobe queue")), \
             patch.object(pipeline, "_api_post", side_effect=AssertionError("Adobe API")), \
             patch.object(pipeline, "_run_ffrender_export", return_value=True) as render:
            self.assertEqual(pipeline._export_engine(), "ffmpeg")
            self.assertTrue(pipeline.step_premiere(1, self.folder, True))
            self.assertTrue(pipeline.step_export(1, self.folder, True))
            render.assert_called_once()
            self.assertNotIn("PSD", pipeline._step_label("psd_composite"))
        with patch.object(pipeline, "_load_dashboard_config", return_value={"export_engine": "ame"}):
            self.assertEqual(pipeline._export_engine(), "ame")
            self.assertIn("PSD", pipeline._step_label("psd_composite"))

    def test_registration_seed_has_no_adobe_templates(self):
        req = studio.ChannelCreate(name="QA", folder=str(self.channel), production_mode="ffmpeg_only",
                                  template_prproj="old.prproj", template_psd="old.psd")
        studio._create_empty_channel_config(self.channel, req)
        seed = json.loads((self.channel / ".app_channel_config.json").read_text())
        self.assertEqual(seed["export_engine"], "ffmpeg")
        self.assertEqual(seed["template_prproj"], "")
        self.assertEqual(seed["template_psd"], "")
        self.assertFalse(seed["scene_text_enabled"])
        self.assertIn("production_mode", app_core.PER_CHANNEL_KEYS)
        self.assertIn("thumbnail_composition", app_core.PER_CHANNEL_KEYS)
        self.assertEqual(studio.ChannelCreate(name="legacy", folder="unused").production_mode, "standard")

    def test_video_creation_does_not_copy_stale_adobe_templates(self):
        cfg = {**self.config, "channel_folder": str(self.channel),
               "template_prproj": "old.prproj", "template_psd": "old.psd"}
        for name in ["old.prproj", "old.psd"]:
            (self.channel / name).write_bytes(b"must not copy")
        with patch.object(studio, "get_dashboard_config", return_value=cfg), \
             patch.object(studio, "get_file_prefix", return_value="QA"), \
             patch("vps_ledger_sync.ensure_vol"), patch("vps_ledger_sync.push_context"):
            result = studio.api_create_video_folder(studio.VideoFolderCreate(
                publish_date="2026-09-29", open_in_finder=False))
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["created"], [])
        self.assertFalse(list(Path(result["path"]).iterdir()))

    def test_dashboard_update_cannot_enable_ame_in_adobe_free_mode(self):
        with patch.object(studio, "get_dashboard_config", return_value=dict(self.config)), \
             patch.object(studio, "save_dashboard_config_smart") as save:
            result = studio.api_update_dashboard_config(studio.DashboardConfigUpdate(export_engine="ame"))
        self.assertEqual(result["config"]["export_engine"], "ffmpeg")
        self.assertEqual(save.call_args.args[0]["production_mode"], "ffmpeg_only")
        with self.assertRaises(ValidationError):
            studio.ChannelCreate(name="bad", folder="unused", production_mode="unknown")
        item = settings_service.find_setting("channel.production_mode")
        with self.assertRaises(ValueError):
            settings_service._parse_value("unknown", item)

    def test_settings_read_uses_ffmpeg_even_after_legacy_config_edit(self):
        with patch.object(app_core, "load_json", return_value={"channel_folder": ""}), \
             patch.object(app_core, "load_channel_config", return_value=self.config):
            self.assertEqual(app_core.get_dashboard_config()["export_engine"], "ffmpeg")


if __name__ == "__main__":
    unittest.main()
