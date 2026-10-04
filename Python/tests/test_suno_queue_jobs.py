"""SUNO キューのジョブ設定を、ブラウザやチャンネルフォルダなしで検証する。"""
from __future__ import annotations

import contextlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import suno_fast_dl
import suno_queue


class LoadJobsTests(unittest.TestCase):
    def load_job(self, overrides=None, channel_config=None):
        item = {
            "vol": 1,
            "channel_folder": "/unused/channel",
            "workspace": "queue-test",
            "download_dir": "/unused/downloads",
            "prompt": "Instrumental test",
        }
        item.update(overrides or {})
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jobs.json"
            path.write_text(json.dumps([item]), encoding="utf-8")
            with mock.patch.object(suno_queue.suno, "load_config", return_value={}), \
                    mock.patch.object(suno_queue, "load_channel_suno_config",
                                      return_value=channel_config or {}):
                return suno_queue.load_jobs(path)[0]

    def test_numeric_boundaries_are_preserved(self):
        for values in (
            {"duration_seconds": 10, "weirdness": 0, "style_influence": 0, "variety": 0},
            {"duration_seconds": 360, "weirdness": 100, "style_influence": 100, "variety": 4},
        ):
            for source in ("job", "channel"):
                with self.subTest(source=source, values=values):
                    job = self.load_job(values if source == "job" else None,
                                        values if source == "channel" else None)
                    for key, value in values.items():
                        self.assertEqual(job.settings[key], value)

    def test_numeric_values_require_integers_in_range(self):
        invalid_values = {
            "duration_seconds": ("35", 35.0, True, 5, 361),
            "weirdness": ("35", 35.0, True, -1, 101),
            "style_influence": ("35", 35.0, True, -1, 101),
            "variety": ("3", 3.0, True, -1, 5),
        }
        for source in ("job", "channel"):
            for key, values in invalid_values.items():
                for value in values:
                    with self.subTest(source=source, key=key, value=value):
                        settings = {key: value}
                        with self.assertRaisesRegex(ValueError, "job 1.*" + key):
                            self.load_job(settings if source == "job" else None,
                                          settings if source == "channel" else None)

    def test_absent_numeric_values_are_optional(self):
        self.assertEqual(self.load_job().settings["workspace"], "queue-test")
        keys = ("duration_seconds", "weirdness", "style_influence", "variety")
        job = self.load_job({key: None for key in keys})
        self.assertTrue(all(job.settings.get(key) is None for key in keys))

    def test_job_numeric_values_override_channel_values(self):
        job = self.load_job({"weirdness": 0, "duration_seconds": 240},
                            {"weirdness": 50, "duration_seconds": 180})
        self.assertEqual(job.settings["weirdness"], 0)
        self.assertEqual(job.settings["duration_seconds"], 240)


class QueueLifecycleTests(unittest.TestCase):
    def run_main(self, mode="fast", hook_error=None, scheduler_error=None):
        events = []
        session = mock.Mock()
        session.context.add_init_script.side_effect = lambda script: events.append("interceptor")
        session.page.side_effect = lambda: events.append("page") or mock.Mock()
        session.close.side_effect = lambda: events.append("session_close")
        manager = mock.MagicMock()
        manager.__exit__.side_effect = lambda *args: events.append("playwright_exit") or False
        lock = mock.Mock()
        lock.release.side_effect = lambda: events.append("lock_release")
        args = SimpleNamespace(jobs_file=Path("/unused/jobs.json"), dry_run=False,
                               headless=False, browser_mode="cdp", browser_profile_dir=None,
                               cdp_port=9222)
        job = mock.Mock(state="pending", label="queue-test", index=1, vol=1)

        def capture(context):
            events.append("capture")
            self.assertIs(context, session.context)
            if hook_error is not None:
                raise hook_error

        def schedule(*args):
            events.append("scheduler")
            if scheduler_error is not None:
                raise scheduler_error
            return 0

        with contextlib.ExitStack() as stack:
            for patch in (
                mock.patch.object(suno_queue, "parse_args", return_value=args),
                mock.patch.object(suno_queue.suno, "load_config", return_value={}),
                mock.patch.object(suno_queue, "load_jobs", return_value=[job]),
                mock.patch.object(suno_queue, "ResourceLock", return_value=mock.Mock(
                    acquire=mock.Mock(return_value=lock))),
                mock.patch.object(suno_queue.atexit, "register"),
                mock.patch("playwright.sync_api.sync_playwright", return_value=manager),
                mock.patch.object(suno_queue, "open_suno_context", return_value=session),
                mock.patch.object(suno_fast_dl, "install_fast_capture", side_effect=capture),
                mock.patch.object(suno_queue, "ensure_login"),
                mock.patch.object(suno_queue, "run_scheduler", side_effect=schedule),
                mock.patch.object(suno_queue, "emit"),
                mock.patch.dict(os.environ, {"APP_SUNO_DL_MODE": mode}),
            ):
                stack.enter_context(patch)
            failed = stack.enter_context(mock.patch.object(suno_queue, "fail_job"))
            summary = stack.enter_context(mock.patch.object(suno_queue, "emit_summary"))
            result = suno_queue.main()
        return result, events, failed, summary

    def test_fast_capture_is_installed_before_page_and_scheduler(self):
        result, events, failed, summary = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(events[:4], ["interceptor", "capture", "page", "scheduler"])
        failed.assert_not_called()
        summary.assert_not_called()

    def test_legacy_skips_fast_capture(self):
        result, events, _, _ = self.run_main(mode=" Legacy ")
        self.assertEqual(result, 0)
        self.assertNotIn("capture", events)

    def test_hook_failure_stops_before_page_or_scheduler(self):
        error = suno_fast_dl.HookLoadError("missing test hook")
        result, events, failed, summary = self.run_main(hook_error=error)
        self.assertEqual(result, 1)
        self.assertNotIn("page", events)
        self.assertNotIn("scheduler", events)
        self.assertIs(failed.call_args.args[1], error)
        summary.assert_called_once()
        self.assertEqual(events[-3:], ["session_close", "playwright_exit", "lock_release"])

    def test_session_closes_before_playwright_exits_on_success_and_failures(self):
        for error, expected in ((None, 0), (RuntimeError("scheduler failed"), 1),
                                (KeyboardInterrupt(), 130)):
            with self.subTest(error=type(error).__name__):
                result, events, failed, summary = self.run_main(mode="legacy", scheduler_error=error)
                self.assertEqual(result, expected)
                self.assertEqual(events[-3:], ["session_close", "playwright_exit", "lock_release"])
                if error is not None:
                    failed.assert_called_once()
                    summary.assert_called_once()


if __name__ == "__main__":
    unittest.main()
