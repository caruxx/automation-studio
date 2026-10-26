"""段階 3: Mac の公開ゲート・auto_resume・vol_create 起票の停止と、_scheduler_reload が固定ジョブを消さないこと。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as studio  # noqa: E402


class FakeJob:
    def __init__(self, id): self.id = id


class FakeScheduler:
    def __init__(self, ids):
        self.jobs = {i: FakeJob(i) for i in ids}
        self.added = []
    def get_jobs(self): return list(self.jobs.values())
    def remove_job(self, id): self.jobs.pop(id)
    def add_job(self, handler, trigger=None, id=None, args=None, **kw):
        self.jobs[id] = FakeJob(id); self.added.append(id)


class MacSchedulerStage3Test(unittest.TestCase):
    def test_schedule_publish_api_is_gone(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            studio.api_youtube_schedule_publish(studio.SchedulePublishRequest(video_name="1_x", delay_hours=1))
        self.assertEqual(cm.exception.status_code, 410)
        self.assertFalse(hasattr(studio, "_job_publish_now"))
        self.assertFalse(hasattr(studio, "_recover_scheduled_publishes"))
        self.assertFalse(hasattr(studio, "_job_auto_resume"))
        self.assertNotIn("publish_now", studio.JOB_HANDLERS)

    def test_schedule_resume_is_a_noop_with_hint(self):
        before = len(studio._scheduler_history)
        self.assertFalse(studio._schedule_resume({"id": "j1", "vol": 5}, "render", 2))
        self.assertIn("run-resume", studio._scheduler_history[-1]["detail"])
        self.assertEqual(len(studio._scheduler_history), before + 1)

    def test_reload_keeps_fixed_jobs_and_skips_vps_owned(self):
        fake = FakeScheduler(["orchestrator_tick", "daily_digest", "job_a", "job_b"])
        jobs = [{"id": "job_a", "type": "vol_create", "enabled": True, "hour": 9, "minute": 0},
                {"id": "job_b", "type": "token_health", "enabled": True, "hour": 9, "minute": 0},
                {"id": "job_c", "type": "spot_create", "enabled": True, "hour": 9, "minute": 0}]
        with patch.object(studio, "_scheduler", fake), patch.object(studio, "_scheduler_get_jobs", return_value=jobs), \
             patch.object(studio, "_scheduler_register_job", side_effect=lambda job: fake.add_job(None, id=job["id"]) or True) as reg:
            studio._scheduler_reload()
        self.assertIn("orchestrator_tick", fake.jobs)
        self.assertIn("daily_digest", fake.jobs)
        self.assertIn("job_a", fake.jobs)   # vol_create は Mac が登録する（採番は VPS）
        self.assertIn("job_b", fake.jobs)
        self.assertIn("job_c", fake.jobs)
        self.assertEqual([c.args[0]["id"] for c in reg.call_args_list], ["job_a", "job_b", "job_c"])
        # jobs.json から job_b を消して reload すると、APScheduler からも外れる（固定ジョブは残る）
        with patch.object(studio, "_scheduler", fake), patch.object(studio, "_scheduler_get_jobs", return_value=[]), \
             patch.object(studio, "_scheduler_register_job", side_effect=lambda job: True):
            studio._scheduler_reload()
        self.assertNotIn("job_b", fake.jobs)
        self.assertIn("orchestrator_tick", fake.jobs)
        self.assertIn("daily_digest", fake.jobs)

    def test_allocate_next_vol_uses_vps_number_and_creates_folder(self):
        import tempfile, shutil
        ch = Path(tempfile.mkdtemp(prefix="vc_"))
        try:
            (ch / "4_test_260101").mkdir()
            seen = {}
            fake = type("L", (), {"enabled": staticmethod(lambda: True),
                                  "allocate_vol": staticmethod(lambda channel_key=None: seen.setdefault("key", channel_key) and {"id": 9, "vol_number": 5, "publish_date": "2026-12-24"}),
                                  "resolve_channel_key": staticmethod(lambda: "from-env"),
                                  "push_context": staticmethod(lambda vol, folder: None)})
            with patch.dict(sys.modules, {"vps_ledger_sync": fake}), patch.object(studio, "get_file_prefix", return_value="test"):
                vol, err = studio._allocate_next_vol(ch, "edm", "EDM")
            self.assertEqual((vol, err), (5, ""))
            self.assertEqual(seen["key"], "edm")  # ジョブのチャンネルで採番（アクティブチャンネルと混線しない）
            self.assertTrue((ch / "5_test_261224").is_dir())  # VPS の公開日でフォルダを作る
            # 同じ番号のフォルダが既にある / 番号が既存以下 → 作らず error（古いフォルダに新しい vol を走らせない）
            with patch.dict(sys.modules, {"vps_ledger_sync": fake}), patch.object(studio, "get_file_prefix", return_value="test"):
                vol, err = studio._allocate_next_vol(ch, "edm", "EDM")
            self.assertIsNone(vol); self.assertIn("既にあります", err)
            bad = type("L", (), {"enabled": staticmethod(lambda: True),
                                 "allocate_vol": staticmethod(lambda channel_key=None: {"id": 1, "vol_number": 3, "publish_date": "2026-12-25"}),
                                 "resolve_channel_key": staticmethod(lambda: None),
                                 "push_context": staticmethod(lambda vol, folder: None)})
            with patch.dict(sys.modules, {"vps_ledger_sync": bad}):
                vol, err = studio._allocate_next_vol(ch, "edm", "EDM")
            self.assertIsNone(vol)
            self.assertFalse((ch / "3_test_261225").exists())
            # VPS 未設定: 従来どおり最大 + 1（フォルダは作らない）
            legacy = type("L", (), {"enabled": staticmethod(lambda: False), "allocate_vol": staticmethod(lambda: None)})
            with patch.dict(sys.modules, {"vps_ledger_sync": legacy}):
                self.assertEqual(studio._allocate_next_vol(ch, "edm", "EDM"), (6, ""))
            # VPS 到達不能: 採番しない（二重起票しない）
            down = type("L", (), {"enabled": staticmethod(lambda: True), "resolve_channel_key": staticmethod(lambda: "edm"),
                                  "allocate_vol": staticmethod(lambda channel_key=None: (_ for _ in ()).throw(RuntimeError("down")))})
            with patch.dict(sys.modules, {"vps_ledger_sync": down}):
                vol, err = studio._allocate_next_vol(ch, "edm", "EDM")
            self.assertIsNone(vol)
        finally:
            shutil.rmtree(ch, ignore_errors=True)

    def test_orchestrator_tick_default_off(self):
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_ORCH_TICK_ENABLED", None)
            with patch.object(studio, "get_channel_registry", side_effect=AssertionError("tick must return before reading channels"), create=True):
                self.assertIsNone(studio._orchestrator_tick())  # 既定 OFF: 即 return（autopilot は VPS）


if __name__ == "__main__":
    unittest.main()
