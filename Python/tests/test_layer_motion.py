#!/usr/bin/env python3
"""Isolated renderer/API tests. Stub app_core; never read the user's configuration."""
from __future__ import annotations

import base64
import copy
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from types import ModuleType
from unittest import mock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image, ImageChops, ImageDraw

PYTHON_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PYTHON_DIR))
import app_layer_motion as motion


def png(color, size=(64, 48)):
    image = Image.new("RGBA", size, color)
    data = io.BytesIO()
    image.save(data, "PNG")
    return motion.PNG_PREFIX + base64.b64encode(data.getvalue()).decode("ascii")


def project():
    return {"version": 1, "width": 64, "height": 48, "duration": 1, "fps": 12,
            "background": png((15, 45, 75, 255)),
            "layers": [{"name": "dummy sprite", "data_url": png((255, 0, 0, 255), (10, 6)),
                        "x": 32, "y": 24, "width": 10, "height": 6, "opacity": 1, "visible": True,
                        "motion": {"sway_deg": 0, "bob_px": 8, "breathe_pct": 0,
                                   "period": 1, "phase": 0, "start": 0, "end": 1}}]}


class RendererTest(unittest.TestCase):
    def test_fixed_background_moving_layer_and_immutable_inputs(self):
        original = project()
        snapshot = copy.deepcopy(original)
        renderer = motion.Renderer(motion.validate_project(original))
        try:
            a, b = renderer.frame(0), renderer.frame(.25)
            self.assertEqual(a.getpixel((32, 24)), (255, 0, 0))
            self.assertEqual(b.getpixel((32, 24)), (15, 45, 75))
            self.assertEqual(b.getpixel((32, 32)), (255, 0, 0))
            for frame in (a, b):
                self.assertEqual(frame.getpixel((0, 0)), (15, 45, 75))
                self.assertEqual(frame.getpixel((63, 47)), (15, 45, 75))
            self.assertEqual(original, snapshot)
        finally:
            renderer.close()

    def test_zero_amplitudes_remain_fixed_even_with_phase(self):
        value = project()
        value["layers"][0]["motion"].update(sway_deg=0, bob_px=0, breathe_pct=0, phase=42)
        renderer = motion.Renderer(motion.validate_project(value))
        try:
            self.assertIsNone(ImageChops.difference(renderer.frame(0), renderer.frame(.4)).getbbox())
        finally:
            renderer.close()

    def test_timing_visibility_clipping_and_opacity(self):
        value = project()
        value["layers"][0].update(x=0, y=0, opacity=.5)
        value["layers"][0]["motion"].update(start=.25, end=.75, bob_px=0)
        renderer = motion.Renderer(motion.validate_project(value))
        try:
            self.assertEqual(renderer.frame(0).getpixel((0, 0)), (15, 45, 75))
            self.assertEqual(renderer.frame(.25).getpixel((0, 0)), (135, 22, 37))
            self.assertEqual(renderer.frame(.75).getpixel((0, 0)), (15, 45, 75))
            self.assertEqual(renderer.frame(.5).getpixel((10, 10)), (15, 45, 75))
        finally:
            renderer.close()
        value["layers"][0]["visible"] = False
        renderer = motion.Renderer(motion.validate_project(value))
        try:
            self.assertEqual(renderer.frame(.5).getpixel((0, 0)), (15, 45, 75))
        finally:
            renderer.close()

    def test_clockwise_rotation_phase_and_breathing(self):
        value = project()
        # A red right edge rotates to the bottom with a positive clockwise 90° angle.
        arrow = Image.new("RGBA", (10, 6), (0, 0, 0, 0))
        ImageDraw.Draw(arrow).rectangle((7, 0, 9, 5), fill=(255, 0, 0, 255))
        buffer = io.BytesIO(); arrow.save(buffer, "PNG")
        value["layers"][0]["data_url"] = motion.PNG_PREFIX + base64.b64encode(buffer.getvalue()).decode()
        value["layers"][0]["motion"].update(sway_deg=90, bob_px=0, phase=90)
        renderer = motion.Renderer(motion.validate_project(value))
        try:
            frame = renderer.frame(0)
            self.assertEqual(frame.getpixel((32, 28)), (255, 0, 0))
            self.assertEqual(frame.getpixel((32, 20)), (15, 45, 75))
        finally:
            renderer.close()
        value = project()
        value["layers"][0]["motion"].update(bob_px=0, breathe_pct=50)
        renderer = motion.Renderer(motion.validate_project(value))
        try:
            self.assertEqual(renderer.frame(0).getpixel((26, 24)), (15, 45, 75))
            self.assertEqual(renderer.frame(.25).getpixel((26, 24)), (255, 0, 0))
        finally:
            renderer.close()

    def test_reject_unsafe_or_excessive_projects(self):
        changes = [("width", 1922), ("height", 49), ("fps", 31), ("duration", 61),
                   ("duration", float("nan")), ("width", True), ("width", 10 ** 1000), ("version", 2),
                   ("background", "http://example.test/image.png"), ("background", "/tmp/file.png"),
                   ("background", motion.PNG_PREFIX + "not base64"),
                   ("layers", [project()["layers"][0]] * 17)]
        for key, bad in changes:
            with self.subTest(key=key, bad=str(bad)[:20]):
                value = project(); value[key] = bad
                with self.assertRaises(motion.InvalidProject):
                    motion.validate_project(value)
        value = project(); value["layers"][0]["motion"]["period"] = 0
        with self.assertRaises(motion.InvalidProject): motion.validate_project(value)
        value = project(); value["layers"][0]["motion"].update(start=.9, end=.2)
        with self.assertRaises(motion.InvalidProject): motion.validate_project(value)
        value = project(); value.update(width=1920, height=1080, fps=30, duration=60)
        value["layers"][0].update(width=3840, height=2160)
        with self.assertRaisesRegex(motion.InvalidProject, "処理量"): motion.validate_project(value)

    def test_16bit_png_rejected_instead_of_silently_clipped(self):
        import struct
        image = Image.frombytes("I;16", (2, 1), struct.pack("<HH", 32768, 16384))
        buffer = io.BytesIO(); image.save(buffer, "PNG")
        value = project()
        value["background"] = motion.PNG_PREFIX + base64.b64encode(buffer.getvalue()).decode()
        with self.assertRaisesRegex(motion.InvalidProject, "16-bit"):
            motion.validate_project(value)

    def test_unique_promotion_never_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as directory:
            target, temporary = Path(directory) / "existing.mp4", Path(directory) / "partial"
            target.write_bytes(b"keep"); temporary.write_bytes(b"new")
            with self.assertRaises(FileExistsError): motion.publish_unique(temporary, target)
            self.assertEqual(target.read_bytes(), b"keep")
            self.assertEqual(temporary.read_bytes(), b"new")


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.channel = self.root / "channel"
        self.channel.mkdir()
        self.name = "999_test_261002"
        self.vol = self.channel / self.name
        self.vol.mkdir()
        # A sibling existing video output must be preserved by success/cancel/failure.
        (self.vol / "existing.mp4").write_bytes(b"user video")
        stub = ModuleType("app_core")
        def resolver(name):
            if name != self.name:
                raise HTTPException(400, "invalid test video_name")
            return self.channel / name
        stub.resolve_video_folder = resolver
        name = "isolated_layer_motion_router_" + str(id(self))
        spec = importlib.util.spec_from_file_location(name, PYTHON_DIR / "routers/layer_motion.py")
        self.api = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {"app_core": stub}):
            spec.loader.exec_module(self.api)
        app = FastAPI(); app.include_router(self.api.router)
        self.client = TestClient(app)

    def tearDown(self):
        for job_id in list(self.api._jobs):
            self.client.post("/api/layer-motion/cancel", json={"job_id": job_id})
        deadline = time.monotonic() + 5
        while any(j["status"] in self.api._ACTIVE for j in self.api._jobs.values()) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertEqual((self.vol / "existing.mp4").read_bytes(), b"user video")
        self.client.close()
        self.temp.cleanup()

    def post(self, route, value=None, **extra):
        return self.client.post("/api/layer-motion/" + route,
                                json={"video_name": self.name, "project": value or project(), **extra})

    def wait(self, job_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            response = self.client.get("/api/layer-motion/status", params={"job_id": job_id})
            state = response.json()
            if state["status"] not in self.api._ACTIVE:
                return state
            time.sleep(.02)
        self.fail("job did not terminate")

    def test_save_reload_preserves_png_metadata_and_unique_names(self):
        value = project()
        value["layers"][0]["text"] = "editable text"
        value["layers"][0]["motion"].pop("end")
        first = self.post("project", value, title="同名保存").json()
        second = self.post("project", value, title="同名保存").json()
        self.assertNotEqual(first["filename"], second["filename"])
        loaded = self.client.get("/api/layer-motion/project", params={"video_name": self.name, "filename": first["filename"]})
        self.assertEqual(loaded.status_code, 200, loaded.text)
        self.assertEqual(loaded.json()["project"]["background"], value["background"])
        self.assertEqual(loaded.json()["project"]["layers"][0]["text"], "editable text")
        self.assertEqual(loaded.json()["project"]["layers"][0]["motion"]["end"], 1)
        listed = self.client.get("/api/layer-motion/projects", params={"video_name": self.name}).json()
        self.assertEqual(len(listed["projects"]), 2)
        self.assertEqual(listed["projects"][0]["title"], "同名保存")
        self.assertTrue(Path(first["path"]).is_relative_to(self.vol.resolve()))

    def test_traversal_symlinks_and_bad_project_rejected(self):
        response = self.client.post("/api/layer-motion/project", json={"video_name": "../elsewhere", "project": project()})
        self.assertEqual(response.status_code, 400)
        response = self.client.get("/api/layer-motion/project", params={"video_name": self.name, "filename": "../private.json"})
        self.assertEqual(response.status_code, 400)
        (self.vol / "layer_motion").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.post("project").status_code, 400)
        self.assertEqual(self.post("export").status_code, 400)
        (self.vol / "layer_motion").unlink()
        base = self.vol / "layer_motion"; base.mkdir()
        (base / "projects").symlink_to(self.root, target_is_directory=True)
        self.assertEqual(self.post("project").status_code, 400)
        (base / "projects").unlink(); (base / "projects").mkdir()
        filename = "motion_" + "a" * 32 + ".json"
        (base / "projects" / filename).symlink_to(self.root / "private.json")
        self.assertEqual(self.client.get("/api/layer-motion/project", params={"video_name": self.name, "filename": filename}).status_code, 400)
        value = project(); value["layers"][0]["data_url"] = "file:///private.png"
        self.assertEqual(self.post("project", value).status_code, 400)
        self.assertEqual(self.post("export", value).status_code, 400)

    def test_scope_guard_rejects_cross_channel_writes_and_reads(self):
        scope = self.client.get("/api/layer-motion/context", params={"video_name": self.name}).json()["scope"]
        saved = self.post("project", scope=scope)
        self.assertEqual(saved.status_code, 200, saved.text)
        other = self.root / "other_channel"; other.mkdir(); (other / self.name).mkdir()
        self.channel = other
        new_scope = self.client.get("/api/layer-motion/context", params={"video_name": self.name}).json()["scope"]
        self.assertNotEqual(scope, new_scope)
        self.assertEqual(self.post("project", scope=scope).status_code, 409)
        self.assertEqual(self.post("export", scope=scope).status_code, 409)
        self.assertFalse((other / self.name / "layer_motion").exists())
        params = {"video_name": self.name, "scope": scope}
        self.assertEqual(self.client.get("/api/layer-motion/projects", params=params).status_code, 409)
        params["filename"] = saved.json()["filename"]
        self.assertEqual(self.client.get("/api/layer-motion/project", params=params).status_code, 409)
        self.assertEqual(self.post("project", scope=new_scope).status_code, 200)

    def test_request_size_limit_and_missing_job(self):
        with mock.patch.object(self.api, "MAX_REQUEST_BYTES", 10):
            self.assertEqual(self.post("project").status_code, 413)
        self.assertEqual(self.client.get("/api/layer-motion/status", params={"job_id": "missing"}).status_code, 404)
        self.assertEqual(self.client.post("/api/layer-motion/cancel", json={"job_id": "missing"}).status_code, 404)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "existing ffmpeg/ffprobe required")
    def test_real_mp4_export_probe_and_channel_switch_download(self):
        response = self.post("export")
        self.assertEqual(response.status_code, 200, response.text)
        state = self.wait(response.json()["job_id"])
        self.assertEqual(state["status"], "completed", state)
        self.assertEqual(state["progress"], 100)
        output = Path(state["output_path"])
        data = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)]))
        stream = data["streams"][0]
        self.assertEqual((stream["codec_name"], stream["width"], stream["height"]), ("h264", 64, 48))
        self.assertEqual(stream["nb_frames"], "12")
        self.assertAlmostEqual(float(data["format"]["duration"]), 1, places=1)
        # Decode a real output frame; background survives lossy encode with small tolerance.
        pixels = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"])
        self.assertEqual(len(pixels), 64 * 48 * 3)
        self.assertTrue(all(abs(pixels[n] - expected) <= 8 for n, expected in enumerate((15, 45, 75))))
        other = self.root / "other_channel"; other.mkdir(); (other / self.name).mkdir()
        self.channel = other
        response = self.client.get(state["url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, output.read_bytes())
        wrong = state["url"].replace(self.name, "other")
        self.assertEqual(self.client.get(wrong).status_code, 404)
        self.assertFalse(list((self.vol / "layer_motion/exports").glob("*.partial")))

    @unittest.skipUnless(shutil.which("ffmpeg"), "existing ffmpeg required")
    def test_cancel_stops_real_process_cleans_partial_and_retry(self):
        real_render = self.api.render_mp4
        started = threading.Event()
        process_seen = []
        def slow_render(value, path, event, progress, on_process):
            def remember(process):
                if process:
                    process_seen.append(process); started.set()
                on_process(process)
            def slow_progress(value):
                progress(value)
                event.wait(.1)
            return real_render(value, path, event, slow_progress, remember)
        with mock.patch.object(self.api, "render_mp4", slow_render):
            response = self.post("export")
            jid = response.json()["job_id"]
            self.assertTrue(started.wait(5))
            self.assertEqual(self.post("export").status_code, 409)
            stop = self.client.post("/api/layer-motion/cancel", json={"job_id": jid})
            self.assertIn(stop.json()["status"], ("cancelling", "cancelled"))
            state = self.wait(jid)
            self.assertEqual(state["status"], "cancelled", state)
        self.assertIsNotNone(process_seen[0].poll())
        self.assertEqual(list((self.vol / "layer_motion/exports").iterdir()), [])
        retried = self.post("export").json()
        self.assertNotEqual(jid, retried["job_id"])
        self.assertEqual(self.wait(retried["job_id"])["status"], "completed")

    @unittest.skipUnless(shutil.which("ffmpeg"), "existing ffmpeg required")
    def test_request_id_recovers_lost_response_without_duplicate_export(self):
        request_id = str(uuid.uuid4())
        # Deliberately ignore the first response as if its transport were lost.
        self.post("export", request_id=request_id)
        lookup = self.client.get("/api/layer-motion/status", params={"request_id": request_id})
        self.assertEqual(lookup.status_code, 200, lookup.text)
        job_id = lookup.json()["job_id"]
        again = self.post("export", request_id=request_id)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(again.json()["job_id"], job_id)
        self.assertEqual(len(self.api._jobs), 1)
        self.assertEqual(self.wait(job_id)["status"], "completed")
        # Even after completion, transport retries are idempotent.
        self.assertEqual(self.post("export", request_id=request_id).json()["job_id"], job_id)
        changed = project(); changed["duration"] = 2
        self.assertEqual(self.post("export", changed, request_id=request_id).status_code, 409)
        self.assertEqual(self.post("export", request_id="invalid").status_code, 400)
        self.assertEqual(len(list((self.vol / "layer_motion/exports").glob("*.mp4"))), 1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "existing ffmpeg required")
    def test_failure_cleanup_and_retry(self):
        with mock.patch.object(self.api, "render_mp4", side_effect=RuntimeError("simulated encoder failure")):
            jid = self.post("export").json()["job_id"]
            state = self.wait(jid)
            self.assertEqual(state["status"], "failed")
            self.assertIn("simulated", state["error"])
        self.assertEqual(list((self.vol / "layer_motion/exports").iterdir()), [])
        jid = self.post("export").json()["job_id"]
        self.assertEqual(self.wait(jid)["status"], "completed")


if __name__ == "__main__":
    unittest.main()
