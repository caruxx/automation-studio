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
        self.assertNotIn("job_a", fake.jobs)   # vol_create は Mac では登録しない
        self.assertNotIn("job_c", fake.jobs)
        self.assertIn("job_b", fake.jobs)
        self.assertEqual([c.args[0]["id"] for c in reg.call_args_list], ["job_b"])

    def test_orchestrator_tick_default_off(self):
        import os
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("APP_ORCH_TICK_ENABLED", None)
            with patch.object(studio, "get_channel_registry", side_effect=AssertionError("tick must return before reading channels"), create=True):
                self.assertIsNone(studio._orchestrator_tick())  # 既定 OFF: 即 return（autopilot は VPS）


if __name__ == "__main__":
    unittest.main()
