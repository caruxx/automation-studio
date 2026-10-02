#!/usr/bin/env python3
"""Isolated tests for wobble_suno_bridge; no live catalog, browser, or network."""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from types import ModuleType
from unittest import mock


SCRIPT = (Path(__file__).resolve().parents[1] / "scripts/wobble_suno_bridge.py")
SPEC = importlib.util.spec_from_file_location("wobble_suno_bridge_under_test", SCRIPT)
assert SPEC and SPEC.loader
BRIDGE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE)


FAKE_CATALOG = r'''
import os
import sqlite3
import uuid

DB = os.environ["APP_MUSIC_CATALOG_DB"]

def prepare_submission(content):
    conn = sqlite3.connect(DB)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reservations "
            "(title TEXT NOT NULL UNIQUE, request_id TEXT NOT NULL)"
        )
        base = content["title"]
        title = base
        suffix = 2
        while conn.execute(
            "SELECT 1 FROM reservations WHERE title = ?", (title,)
        ).fetchone():
            title = f"{base} ({suffix})"
            suffix += 1
        request_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO reservations(title, request_id) VALUES (?, ?)",
            (title, request_id),
        )
        conn.commit()
    finally:
        conn.close()
    prepared = dict(content)
    prepared["title"] = title
    prepared["_music_request_id"] = request_id
    return prepared
'''


FAKE_RESOURCE_LOCK = r'''
import fcntl
import os
from pathlib import Path

LOCK_DIR = Path(os.environ["AUTOMATION_STUDIO_LOCK_DIR"])

class ResourceBusyError(RuntimeError):
    pass

class ResourceLock:
    def __init__(self, resource, *, owner=""):
        self.resource = resource
        self.owner = owner
        self.handle = None
        held = {
            item.strip()
            for item in os.environ.get("AUTOMATION_RESOURCE_LOCK_HELD", "").split(",")
            if item.strip()
        }
        self.inherited = resource in held

    def acquire(self, *, blocking=False):
        if self.inherited:
            return self
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        handle = (LOCK_DIR / f"{self.resource}.lock").open("a+", encoding="utf-8")
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), flags)
        except BlockingIOError as exc:
            handle.close()
            raise ResourceBusyError(f"resource busy: {self.resource}") from exc
        self.handle = handle
        return self

    def release(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()
'''


class WobbleSunoBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.fake_modules = self.root / "fake-modules"
        self.fake_modules.mkdir()
        self.test_script = self.fake_modules / "scripts/wobble_suno_bridge.py"
        self.test_script.parent.mkdir()
        self.test_script.write_bytes(SCRIPT.read_bytes())
        (self.fake_modules / "app_music_catalog.py").write_text(
            textwrap.dedent(FAKE_CATALOG), encoding="utf-8"
        )
        (self.fake_modules / "resource_lock.py").write_text(
            textwrap.dedent(FAKE_RESOURCE_LOCK), encoding="utf-8"
        )
        self.db = self.root / "catalog.sqlite3"
        self.env = dict(os.environ)
        self.env.update(
            {
                "APP_MUSIC_CATALOG_DB": str(self.db),
                "AUTOMATION_STUDIO_LOCK_DIR": str(self.root / "locks"),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": str(self.fake_modules),
            }
        )
        self.env.pop("AUTOMATION_RESOURCE_LOCK_HELD", None)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def write_json(self, name: str, value: object) -> Path:
        path = self.root / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def valid_request(self) -> dict[str, object]:
        return {
            "project": "Wobble Day",
            "request_id": "wobble-test-1",
            "mode": "generate_and_download",
            "browser_route": "official_chrome_cua",
            "authorization": {
                "generate_authorized": True,
                "authorized_audio_count": 4,
                "user_instruction": "Create four Wobble Day instrumental audio files.",
            },
            "mood": "lightly bouncing",
            "duration_seconds_per_audio": 240,
            "visual_references": {
                "selected_thumbnail_reference": "local/reference.png",
                "selection_evidence": "selected by the user in request wobble-test-1",
            },
            "count": {
                "requested_audio_count": 4,
                "requested_title_count": 2,
                "outputs_per_create_observed": 2,
                "ui_evidence": "Suno Create UI read back two outputs per Create.",
                "planned_create_count": 2,
            },
            "settings": {"instrumental": True, "lyrics": ""},
        }

    @staticmethod
    def songs() -> list[dict[str, object]]:
        return [
            {
                "title": "Soft Orbit",
                "styles": "instrumental jazzhop, brushed drums",
                "instrumental": True,
                "lyrics": "",
                "duration_seconds": 240,
            },
            {
                "title": "Soft Orbit",
                "title_fixed": True,
                "styles": "instrumental jazz, upright bass",
                "instrumental": True,
                "lyrics": "",
                "duration_seconds": 240,
            },
        ]

    def run_bridge(self, *args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-B", str(self.test_script), *args],
            input=input_text,
            text=True,
            capture_output=True,
            env=self.env,
            timeout=10,
            check=False,
        )

    def start_lock(self, request_path: Path, seconds: int = 5) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [
                sys.executable,
                "-B",
                str(self.test_script),
                "hold-lock",
                "--request",
                str(request_path),
                "--max-seconds",
                str(seconds),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env,
        )

    def finish_lock(self, process: subprocess.Popen[str]) -> dict[str, object]:
        assert process.stdin and process.stdout
        process.stdin.write("\n")
        process.stdin.flush()
        released = json.loads(process.stdout.readline())
        process.wait(timeout=5)
        self.assertEqual(process.returncode, 0)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
        return released

    def test_reserve_checks_counts_and_blocks_changed_fixed_title(self) -> None:
        request_path = self.write_json("request.json", self.valid_request())
        songs_path = self.write_json("songs.json", self.songs())
        output = self.root / "reserved.json"

        completed = self.run_bridge(
            "reserve",
            "--request",
            str(request_path),
            "--songs",
            str(songs_path),
            "--output",
            str(output),
        )
        self.assertEqual(completed.returncode, 3, completed.stderr)
        result = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(result["status"], "reserved_create_blocked")
        self.assertTrue(result["create_blocked"])
        self.assertEqual(result["title_changes"][1]["requested_title"], "Soft Orbit")
        self.assertEqual(result["title_changes"][1]["reserved_title"], "Soft Orbit (2)")
        self.assertEqual(result["count"]["ui_evidence"], self.valid_request()["count"]["ui_evidence"])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 2)

        repeated = self.run_bridge(
            "reserve",
            "--request",
            str(request_path),
            "--songs",
            str(songs_path),
            "--output",
            str(output),
        )
        self.assertEqual(repeated.returncode, 2)
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0], 2)

    def test_reserve_rejects_not_ready_request_without_catalog_write(self) -> None:
        request = self.valid_request()
        del request["count"]["ui_evidence"]
        request_path = self.write_json("request-not-ready.json", request)
        songs_path = self.write_json("songs-not-ready.json", self.songs())
        output = self.root / "not-ready-output.json"

        completed = self.run_bridge(
            "reserve",
            "--request",
            str(request_path),
            "--songs",
            str(songs_path),
            "--output",
            str(output),
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("count.ui_evidence", completed.stderr)
        self.assertFalse(output.exists())
        self.assertFalse(self.db.exists())

    def test_atomic_link_probe_fails_before_prepare_submission(self) -> None:
        request_path = self.write_json("request-probe.json", self.valid_request())
        songs_path = self.write_json("songs-probe.json", self.songs())
        output = self.root / "probe-output.json"
        calls: list[dict[str, object]] = []
        fake_catalog = ModuleType("app_music_catalog")

        def prepare_submission(song: dict[str, object]) -> dict[str, object]:
            calls.append(song)
            return song

        fake_catalog.prepare_submission = prepare_submission
        with mock.patch.dict(sys.modules, {"app_music_catalog": fake_catalog}):
            with mock.patch.object(BRIDGE.os, "link", side_effect=OSError("unsupported")):
                with self.assertRaisesRegex(BRIDGE.BridgeError, "use a local run directory"):
                    BRIDGE.reserve(str(request_path), str(songs_path), str(output))
        self.assertEqual(calls, [])
        self.assertFalse(output.exists())

    def test_generate_lock_conflict_release_and_reacquire(self) -> None:
        request = {
            "project": "Wobble Day",
            "request_id": "lock-generate",
            "mode": "generate_and_download",
            "browser_route": "official_chrome_cua",
            "authorization": {
                "generate_authorized": True,
                "authorized_audio_count": 2,
                "user_instruction": "Generate two instrumental outputs.",
            },
            "count": {"requested_audio_count": 2},
        }
        request_path = self.write_json("lock-generate.json", request)
        first = self.start_lock(request_path)
        assert first.stdout
        ready = json.loads(first.stdout.readline())
        self.assertEqual(ready["status"], "ready")
        self.assertEqual(ready["lock_mode"], "acquired_here")
        self.assertTrue(ready["lock_owned_by_process"])

        conflict = self.run_bridge(
            "hold-lock",
            "--request",
            str(request_path),
            "--max-seconds",
            "2",
        )
        self.assertEqual(conflict.returncode, 75)
        self.assertEqual(json.loads(conflict.stderr)["status"], "busy")
        released = self.finish_lock(first)
        self.assertEqual(released["reason"], "stdin_newline")
        self.assertTrue(released["lock_released_by_process"])

        third = self.start_lock(request_path)
        assert third.stdout
        self.assertEqual(json.loads(third.stdout.readline())["status"], "ready")
        self.finish_lock(third)

    def test_download_only_lock_and_timeout(self) -> None:
        download_request = {
            "project": "Wobble Day",
            "request_id": "lock-download",
            "mode": "download_only",
            "browser_route": "official_chrome_cua",
            "authorization": {"user_instruction": "Download the two existing clips."},
            "existing_clip_ids": ["clip-a", "clip-b"],
        }
        download_path = self.write_json("lock-download.json", download_request)
        completed = self.run_bridge(
            "hold-lock",
            "--request",
            str(download_path),
            "--max-seconds",
            "2",
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        events = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual([event["status"] for event in events], ["ready", "released"])
        self.assertEqual(events[1]["reason"], "stdin_eof")

        timeout_process = self.start_lock(download_path, seconds=1)
        assert timeout_process.stdout
        self.assertEqual(json.loads(timeout_process.stdout.readline())["status"], "ready")
        timed_out = json.loads(timeout_process.stdout.readline())
        timeout_process.wait(timeout=5)
        self.assertEqual(timeout_process.returncode, 0)
        self.assertEqual(timed_out["reason"], "timeout")
        for stream in (
            timeout_process.stdin,
            timeout_process.stdout,
            timeout_process.stderr,
        ):
            if stream:
                stream.close()

    def test_lock_rejects_duplicate_download_ids_and_labels_inherited_mode(self) -> None:
        request = {
            "project": "Wobble Day",
            "request_id": "lock-download-invalid",
            "mode": "download_only",
            "browser_route": "official_chrome_cua",
            "authorization": {"user_instruction": "Download the existing clip."},
            "existing_clip_ids": ["clip-a", "clip-a"],
        }
        request_path = self.write_json("lock-download-invalid.json", request)
        rejected = self.run_bridge(
            "hold-lock",
            "--request",
            str(request_path),
            "--max-seconds",
            "2",
        )
        self.assertEqual(rejected.returncode, 2)
        self.assertIn("must be unique", rejected.stderr)

        request["existing_clip_ids"] = ["clip-a"]
        request_path = self.write_json("lock-download-inherited.json", request)
        inherited_env = dict(self.env)
        inherited_env["AUTOMATION_RESOURCE_LOCK_HELD"] = "suno-browser"
        inherited = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.test_script),
                "hold-lock",
                "--request",
                str(request_path),
                "--max-seconds",
                "2",
            ],
            input="",
            text=True,
            capture_output=True,
            env=inherited_env,
            timeout=5,
            check=False,
        )
        self.assertEqual(inherited.returncode, 0, inherited.stderr)
        events = [json.loads(line) for line in inherited.stdout.splitlines()]
        self.assertEqual(events[0]["lock_mode"], "inherited_parent")
        self.assertFalse(events[0]["lock_owned_by_process"])
        self.assertFalse(events[1]["lock_released_by_process"])


if __name__ == "__main__":
    unittest.main()
