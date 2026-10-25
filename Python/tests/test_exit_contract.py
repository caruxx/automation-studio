"""exit_contract: 4 入口（pipeline / orchestrator / batch / studio.py）と humanizer が workflow.json の終了コード表を共有する。"""
try:
    from . import _isolation  # noqa: F401  テスト隔離（tests パッケージとして読まれた場合）
except ImportError:
    import _isolation  # noqa: F401  テスト隔離（`discover tests` で top-level として読まれた場合）
import json
import os
import sys
import tempfile
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
        self.assertEqual(ec.status_for(2), "usage")
        self.assertEqual(ec.ledger_status_for(2), "failed")
        self.assertTrue(ec.is_blocked(79))
        self.assertFalse(ec.is_blocked(78))
        self.assertTrue(ec.is_retryable(76))
        self.assertFalse(ec.is_retryable(77))  # 24 時間待ち。すぐの再試行は無意味
        self.assertEqual(ec.retry_after_sec(77), 86400)
        self.assertIsNone(ec.retry_after_sec(76))
        self.assertEqual(ec.no_auto_resume_codes(), {75, 77, 78, 79})
        self.assertEqual(ec.sentinel_pattern(), "75|76|77|78|79")
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

    def test_evaluate_skips_vol_waiting_for_selection(self):
        import app_orchestrator as o

        class FakeLedger:
            def __init__(self, status):
                self.status = status
            def list_runs(self, channel_id="", vol=None, limit=50, **kw):
                return [{"status": self.status, "vol": vol}]

        folder = Path(tempfile.mkdtemp(prefix="orch_"))
        saved_ledger, saved_pending = o._ledger, o._selection_still_pending
        try:
            o._selection_still_pending = lambda f: True
            o._ledger = FakeLedger("waiting_selection")
            self.assertTrue(o.is_awaiting_selection(folder, 3, channel_id="ch"))
            # 選定が済んだら候補に戻る
            o._selection_still_pending = lambda f: False
            self.assertFalse(o.is_awaiting_selection(folder, 3, channel_id="ch"))
            # 直近 run が失敗なら従来どおり（待機扱いしない）
            o._selection_still_pending = lambda f: True
            o._ledger = FakeLedger("failed")
            self.assertFalse(o.is_awaiting_selection(folder, 3, channel_id="ch"))
            # evaluate は待機中の vol を候補に出さない
            o._ledger = FakeLedger("waiting_selection")
            channels = [{"channel_id": "ch", "vols": [{"vol": 3, "folder": str(folder)}]}]
            self.assertEqual(o.evaluate(channels, dry_run=True), [])
        finally:
            o._ledger, o._selection_still_pending = saved_ledger, saved_pending

    def test_selection_still_pending_uses_processed_gate(self):
        """export / upload のゲートと同じ processed=True で判定する（選定済み・後処理前も待機）。"""
        import shutil
        from unittest.mock import patch
        import app_orchestrator as o
        import app_music_catalog as catalog
        root = Path(tempfile.mkdtemp(prefix="orch_cat_"))
        folder = root / "1_review_260929"
        folder.mkdir()
        with patch.dict(os.environ, {"APP_MUSIC_CATALOG_DB": str(root / "music.sqlite3")}), \
                patch.object(catalog, "_seeded", True):
            self.assertFalse(o._selection_still_pending(folder))  # バッチ未登録は判定不能 → 候補に戻す
            saved = []
            for n, name in enumerate(["Silver Avenue.mp3", "Silver Avenue_2.mp3"]):
                (folder / name).write_bytes(f"original-{n}".encode())
                saved.append({"song_id": f"clip-{n}", "title": "Silver Avenue", "file": name})
            batch = catalog.register_download(folder, saved, "review", 2)
            self.assertTrue(o._selection_still_pending(folder))   # 未選定
            catalog.choose_take(batch, "clip-1", True)
            self.assertTrue(o._selection_still_pending(folder))   # 選定済みだが後処理前 → まだ待機
            catalog.process_selected(folder, shutil.copyfile)
            self.assertFalse(o._selection_still_pending(folder))  # 後処理済み → 候補に戻る
        shutil.rmtree(root, ignore_errors=True)

    def test_constants_outside_entry_points_match_table(self):
        """表の外に残る直書き定数が表とずれていないことを検出する（78 の二重の意味は段階 2 で解消）。"""
        import app_music_catalog as cat
        import app_youtube as yt
        self.assertEqual(cat.EXIT_SELECTION_PENDING, ec.ExitCode.SELECTION_PENDING)
        self.assertEqual(yt.EXIT_RETRYABLE, ec.ExitCode.RETRYABLE)
        self.assertEqual(yt.EXIT_QUOTA_EXHAUSTED, ec.ExitCode.QUOTA_EXHAUSTED)
        self.assertEqual(yt.EXIT_CHANNEL_MISMATCH, ec.ExitCode.PREFLIGHT_FAIL)  # 既知: 78 を共用している

    def test_batch_preflight_code(self):
        import batch_orchestrator as b
        self.assertEqual(b.EXIT_PREFLIGHT, 78)

    def test_humanizer_covers_sentinels(self):
        import error_humanizer as h
        self.assertTrue(set(h.SENTINELS) <= {int(m) for m in ec.ExitCode})
        self.assertIn(79, h.SENTINELS)
        self.assertEqual(sorted(h.SENTINELS), [75, 76, 77, 78, 79])
        self.assertEqual(h.SENTINELS[79]["code"], "music_selection_pending")

    def test_app_outcome_wording_for_blocked(self):
        import app as studio
        self.assertEqual(studio._exit_outcome(79)[1], "warn")
        self.assertIn("失敗ではありません", studio._exit_outcome(79)[0])
        label, kind, prefix = studio._exit_outcome(1)
        self.assertEqual((label, kind), ("失敗", "error"))
        self.assertNotEqual(prefix, "[待機]")
        self.assertEqual(studio._exit_outcome(78)[1], "error")

    def test_studio_exit_fields(self):
        import studio
        self.assertEqual(studio._exit_fields(79), {"reason": "selection_pending", "status": "blocked", "retryable": False})
        self.assertEqual(studio._exit_fields(0)["status"], "done")


if __name__ == "__main__":
    unittest.main()
