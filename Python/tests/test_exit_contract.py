"""exit_contract: 4 入口（pipeline / orchestrator / batch / studio.py）と humanizer が workflow.json の終了コード表を共有する。"""
import json
import sys
import unittest
from pathlib import Path

PY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PY))
import exit_codes as ec  # noqa: E402


class WorkflowJsonTest(unittest.TestCase):
    def test_json_builtin_and_enum_agree(self):
        self.assertEqual(ec.builtin_matches_workflow(), [])
        data = json.loads((PY / "workflow.json").read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertEqual({r["exit_code"] for r in data["exit_reasons"]}, {int(m) for m in ec.ExitCode})

    def test_status_and_flags(self):
        self.assertEqual(ec.status_for(0), "done")
        self.assertEqual(ec.status_for(1), "failed")
        self.assertEqual(ec.status_for(79), "blocked")
        self.assertTrue(ec.is_blocked(79))
        self.assertFalse(ec.is_blocked(78))
        self.assertTrue(ec.is_retryable(76))
        self.assertTrue(ec.is_retryable(77))
        self.assertFalse(ec.is_retryable(75))
        self.assertTrue(ec.needs_human(75))
        self.assertTrue(ec.needs_human(78))
        self.assertFalse(ec.needs_human(79))
        self.assertEqual(ec.reason_for(78), "preflight_fail")
        self.assertEqual(ec.code_for("selection_pending"), 79)
        self.assertEqual(ec.ledger_status_for(0), "done")
        self.assertEqual(ec.ledger_status_for(79), "waiting_selection")
        self.assertEqual(ec.ledger_status_for(75), "failed")

    def test_unknown_code_is_generic_failure(self):
        d = ec.describe(123)
        self.assertEqual((d["reason"], d["status"], d["known"]), ("failed", "failed", False))
        self.assertFalse(ec.is_blocked(123))

    def test_broken_json_falls_back_to_builtin(self):
        wf = ec.load_workflow(Path("/nonexistent/workflow.json"))
        self.assertEqual(wf["workflow_version"], "builtin")
        self.assertEqual(len(wf["exit_reasons"]), len(ec.ExitCode))


class EntryPointsTest(unittest.TestCase):
    def test_pipeline_constants(self):
        import app_pipeline as p
        self.assertEqual((p.EXIT_UNATTENDED, p.EXIT_RETRYABLE, p.EXIT_QUOTA_EXHAUSTED, p.EXIT_PREFLIGHT_FAIL, p.EXIT_SELECTION_PENDING),
                         (75, 76, 77, 78, 79))

    def test_orchestrator_exit_and_wait(self):
        import app_orchestrator as o
        self.assertIs(o.Exit, ec.ExitCode)
        self.assertEqual(o._normalize_result("awaiting_selection"), o.Exit.SELECTION_PENDING)
        self.assertEqual(o._normalize_result("retryable"), o.Exit.RETRYABLE)
        self.assertEqual(o._normalize_result(False), o.Exit.FAIL)
        worker = next(iter(o.WORKERS.values())) if isinstance(getattr(o, "WORKERS", None), dict) and o.WORKERS else None
        if worker is None:
            self.skipTest("WORKERS が空")
        self.assertEqual(worker.on_fail(o.Exit.SELECTION_PENDING, "suno"), o.Action.WAIT)
        self.assertEqual(worker.on_fail(o.Exit.PREFLIGHT_FAIL, "export"), o.Action.NOTIFY)
        self.assertEqual(worker.on_fail(o.Exit.RETRYABLE, "upload"), o.Action.RETRY)
        self.assertEqual(worker.on_fail(o.Exit.OK, "qa"), o.Action.DONE)

    def test_consecutive_failures_ignores_waiting(self):
        import app_orchestrator as o

        class FakeLedger:
            def list_runs(self, channel_id="", limit=20):
                return [{"status": "waiting_selection"}, {"status": "failed"}, {"status": "failed"},
                        {"status": "waiting_selection"}, {"status": "done"}, {"status": "failed"}]
        saved = o._ledger
        o._ledger = FakeLedger()
        try:
            self.assertEqual(o.consecutive_failures("ch"), 2)
        finally:
            o._ledger = saved

    def test_batch_preflight_code(self):
        import batch_orchestrator as b
        self.assertEqual(b.EXIT_PREFLIGHT, 78)

    def test_humanizer_covers_sentinels(self):
        import error_humanizer as h
        self.assertTrue(set(h.SENTINELS) <= {int(m) for m in ec.ExitCode})
        self.assertIn(79, h.SENTINELS)
        self.assertEqual(h.SENTINELS[79]["code"], "music_selection_pending")

    def test_studio_exit_fields(self):
        import studio
        self.assertEqual(studio._exit_fields(79), {"reason": "selection_pending", "status": "blocked", "retryable": False})
        self.assertEqual(studio._exit_fields(0)["status"], "done")


if __name__ == "__main__":
    unittest.main()
