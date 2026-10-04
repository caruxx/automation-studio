"""Submission exit status, with all browser and CLI boundaries mocked."""

import ast
import contextlib
import io
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import suno_auto_create as suno


class SubmissionExitTests(unittest.TestCase):
    def test_zero_success_with_one_target_fails(self):
        self.assertEqual(suno._exit_code_for_submission(0, 1), 1)

    def test_no_targets_succeeds(self):
        self.assertEqual(suno._exit_code_for_submission(0, 0), 0)

    def test_all_submissions_succeed(self):
        self.assertEqual(suno._exit_code_for_submission(1, 1), 0)

    def test_partial_success_remains_success(self):
        self.assertEqual(suno._exit_code_for_submission(1, 3), 0)

    def _run_mocked_browser(self, outcomes):
        session = mock.MagicMock()
        session.page.return_value.url = "https://suno.com/create"
        settings = {
            "loop_count": len(outcomes),
            "pregenerated_songs": [{"title": "test", "styles": "test"}] * len(outcomes),
            "auto_download_dir": "/unused/mocked-download",
        }
        playwright_api = types.ModuleType("playwright.sync_api")
        playwright_api.sync_playwright = mock.MagicMock()
        catalog = types.ModuleType("app_music_catalog")
        catalog.prepare_submission = lambda content: content
        catalog.mark_submitted = mock.Mock()
        catalog.review_state = mock.Mock(return_value=None)
        out = io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.dict(sys.modules, {
                "playwright.sync_api": playwright_api,
                "app_music_catalog": catalog,
            }))
            stack.enter_context(mock.patch.dict(os.environ, {
                "APP_SUNO_DL_MODE": "legacy", "APP_SUNO_NO_HOLD": "1",
                "APP_SUNO_PARALLEL_DRAFT": "0",
            }))
            replacements = {
                "SunoProgress": mock.MagicMock(),
                "resolve_browser_settings": mock.Mock(return_value={
                    "mode": "chrome", "profile_dir": "/unused/mocked-profile",
                }),
                "open_suno_context": mock.Mock(return_value=session),
                "_load_dashboard_config_for_brand": mock.Mock(return_value={}),
                "_set_status": mock.Mock(),
                "is_suno_logged_in": mock.Mock(return_value=True),
                "inject_into_suno": mock.Mock(return_value=True),
                "click_create_button": mock.Mock(side_effect=outcomes),
                "detect_bot_challenge": mock.Mock(return_value=False),
                "detect_copyright_error": mock.Mock(return_value=False),
                "_ready_poll_and_download": mock.Mock(return_value=(True, 2, 2)),
            }
            for name, replacement in replacements.items():
                stack.enter_context(mock.patch.object(suno, name, replacement))
            stack.enter_context(mock.patch.object(suno.time, "sleep"))
            stack.enter_context(contextlib.redirect_stdout(out))
            code = suno.run_browser_automation(settings)
        session.close.assert_called_once_with()
        return code, out.getvalue(), replacements["_ready_poll_and_download"]

    def test_zero_submissions_fail_after_cleanup_without_download(self):
        code, output, download = self._run_mocked_browser([RuntimeError("Create unavailable")])
        self.assertEqual(code, 1)
        self.assertIn("送信に成功した曲がありません", output)
        download.assert_not_called()

    def test_partial_submission_still_downloads_and_succeeds(self):
        code, output, download = self._run_mocked_browser([
            RuntimeError("Create unavailable"), None, RuntimeError("Create unavailable"),
        ])
        self.assertEqual(code, 0)
        self.assertNotIn("送信に成功した曲がありません", output)
        download.assert_called_once()

    def test_main_propagates_browser_result(self):
        settings = {
            "provider": "claude", "model": "test", "generation_mode": "styles_title_only",
            "loop_count": 1, "loop_interval_sec": 15, "prompt": "test",
        }
        with mock.patch.object(sys, "argv", ["suno_auto_create.py"]), \
                mock.patch.object(suno, "load_config", return_value=settings), \
                mock.patch.object(suno, "ResourceLock"), \
                mock.patch.object(suno.atexit, "register"), \
                mock.patch("shutil.which", return_value="/unused/mock-cli"), \
                mock.patch.object(suno, "run_browser_automation", return_value=1), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(suno.main(), 1)

    def test_entrypoint_preserves_return_and_sentinel_codes(self):
        tree = ast.parse(Path(suno.__file__).read_text(encoding="utf-8"))
        entry = next(node for node in tree.body if isinstance(node, ast.If)
                     and isinstance(node.test, ast.Compare)
                     and isinstance(node.test.left, ast.Name)
                     and node.test.left.id == "__name__")
        code = compile(ast.Module(body=[entry], type_ignores=[]), suno.__file__, "exec")
        cases = [
            (1, None, 1), (0, None, 0), (None, None, None),
            (None, suno.BrowserLaunchError("mock launch failure"), 1),
            (None, suno.UnattendedLoginRequired("mock login"), 75),
            (None, suno.BotChallengeDetected("mock challenge"), 75),
            (None, SystemExit(79), 79),
            (None, KeyboardInterrupt(), "interrupt"),
        ]
        for result, error, expected in cases:
            with self.subTest(result=result, error=error):
                namespace = dict(vars(suno), __name__="__main__",
                                 main=mock.Mock(return_value=result, side_effect=error))
                if expected == "interrupt":
                    with self.assertRaises(KeyboardInterrupt):
                        exec(code, namespace)
                else:
                    with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                        exec(code, namespace)
                    self.assertEqual(caught.exception.code, expected)


if __name__ == "__main__":
    unittest.main()
