"""再起動時の公開予約復旧: naive/aware 混在の正規化と、古い予約の再登録抑止。"""
import datetime as dt
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import publish_schedule as ps  # noqa: E402

NOW = dt.datetime(2026, 10, 25, 15, 0, 0)


class NormalizeTest(unittest.TestCase):
    def test_naive_is_kept_as_local(self):
        self.assertEqual(ps.normalize_scheduled_local("2026-10-26T12:00:00"), dt.datetime(2026, 10, 26, 12, 0))

    def test_utc_z_and_offset_become_naive_local(self):
        z = ps.normalize_scheduled_local("2026-07-09T00:00:00Z")
        off = ps.normalize_scheduled_local("2026-07-09T00:00:00+00:00")
        self.assertIsNotNone(z)
        self.assertIsNone(z.tzinfo)
        self.assertEqual(z, off)
        expected = dt.datetime(2026, 7, 9, tzinfo=dt.timezone.utc).astimezone().replace(tzinfo=None)
        self.assertEqual(z, expected)

    def test_invalid_values(self):
        for v in ("", None, "not-a-date", "2026-13-40T00:00:00Z", 123):
            self.assertIsNone(ps.normalize_scheduled_local(v), msg=repr(v))


class PlanTest(unittest.TestCase):
    def test_future_keeps_time(self):
        kind, run_at = ps.plan_scheduled_publish_recovery("2026-10-26T12:00:00", now=NOW)
        self.assertEqual(kind, "future")
        self.assertEqual(run_at, dt.datetime(2026, 10, 26, 12, 0))

    def test_overdue_within_window_runs_one_minute_later(self):
        kind, run_at = ps.plan_scheduled_publish_recovery("2026-10-23T09:00:00", now=NOW, max_stale_days=7)
        self.assertEqual(kind, "overdue")
        self.assertEqual(run_at, NOW + dt.timedelta(minutes=1))

    def test_stale_is_not_rescheduled(self):
        kind, run_at = ps.plan_scheduled_publish_recovery("2026-07-09T00:00:00Z", now=NOW, max_stale_days=7)
        self.assertEqual(kind, "stale")
        self.assertIsNotNone(run_at)

    def test_window_boundary(self):
        exactly = (NOW - dt.timedelta(days=7)).isoformat()
        just_over = (NOW - dt.timedelta(days=7, seconds=1)).isoformat()
        self.assertEqual(ps.plan_scheduled_publish_recovery(exactly, now=NOW)[0], "overdue")
        self.assertEqual(ps.plan_scheduled_publish_recovery(just_over, now=NOW)[0], "stale")

    def test_aware_now_and_aware_value_do_not_raise(self):
        aware_now = NOW.replace(tzinfo=dt.timezone(dt.timedelta(hours=9)))
        kind, _ = ps.plan_scheduled_publish_recovery("2026-07-09T00:00:00Z", now=aware_now)
        self.assertEqual(kind, "stale")

    def test_invalid_is_skipped(self):
        self.assertEqual(ps.plan_scheduled_publish_recovery("garbage", now=NOW), ("invalid", None))

    def test_zero_window_treats_any_past_as_stale(self):
        kind, _ = ps.plan_scheduled_publish_recovery((NOW - dt.timedelta(minutes=1)).isoformat(), now=NOW, max_stale_days=0)
        self.assertEqual(kind, "stale")


if __name__ == "__main__":
    unittest.main()
