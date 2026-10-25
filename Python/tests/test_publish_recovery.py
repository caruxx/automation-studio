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

    def test_utc_z_and_offset_become_naive_jst(self):
        z = ps.normalize_scheduled_local("2026-07-09T00:00:00Z")
        lower_z = ps.normalize_scheduled_local("2026-07-09T00:00:00z")
        off = ps.normalize_scheduled_local("2026-07-09T00:00:00+00:00")
        self.assertEqual(z, dt.datetime(2026, 7, 9, 9, 0))  # UTC 00:00 → JST 09:00、OS の TZ に依存しない
        self.assertEqual(z, lower_z)
        self.assertEqual(z, off)

    def test_jst_offset_is_kept_as_is(self):
        self.assertEqual(ps.normalize_scheduled_local("2026-10-26T12:00:00+09:00"), dt.datetime(2026, 10, 26, 12, 0))
        self.assertEqual(ps.normalize_scheduled_local(" 2026-10-26 "), dt.datetime(2026, 10, 26, 0, 0))

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

    def test_utc_now_is_converted_to_jst_before_compare(self):
        now_utc = dt.datetime(2026, 10, 25, 6, 0, tzinfo=dt.timezone.utc)  # = JST 15:00
        kind, run_at = ps.plan_scheduled_publish_recovery("2026-10-25T15:30:00", now=now_utc)
        self.assertEqual(kind, "future")
        self.assertEqual(run_at, dt.datetime(2026, 10, 25, 15, 30))

    def test_negative_window_is_clamped_to_zero(self):
        kind, _ = ps.plan_scheduled_publish_recovery((NOW - dt.timedelta(seconds=1)).isoformat(), now=NOW, max_stale_days=-3)
        self.assertEqual(kind, "stale")

    def test_zero_window_treats_any_past_as_stale(self):
        kind, _ = ps.plan_scheduled_publish_recovery((NOW - dt.timedelta(minutes=1)).isoformat(), now=NOW, max_stale_days=0)
        self.assertEqual(kind, "stale")


class SchedulerRunAtTest(unittest.TestCase):
    def test_naive_jst_becomes_aware_jst(self):
        aware = ps.scheduler_run_at(dt.datetime(2026, 10, 26, 12, 0))
        self.assertEqual(aware.utcoffset(), dt.timedelta(hours=9))
        self.assertEqual(aware.replace(tzinfo=None), dt.datetime(2026, 10, 26, 12, 0))
        # OS の TZ に関係なく UTC 03:00 を指す
        self.assertEqual(aware.astimezone(dt.timezone.utc), dt.datetime(2026, 10, 26, 3, 0, tzinfo=dt.timezone.utc))

    def test_aware_input_is_converted_to_jst(self):
        aware = ps.scheduler_run_at(dt.datetime(2026, 10, 26, 3, 0, tzinfo=dt.timezone.utc))
        self.assertEqual(aware.replace(tzinfo=None), dt.datetime(2026, 10, 26, 12, 0))


class OverflowTest(unittest.TestCase):
    def test_far_future_aware_value_is_invalid_not_crash(self):
        self.assertIsNone(ps.normalize_scheduled_local("9999-12-31T23:00:00Z"))
        self.assertEqual(ps.plan_scheduled_publish_recovery("9999-12-31T23:00:00Z", now=NOW), ("invalid", None))

    def test_huge_window_is_clamped(self):
        days, warn = ps.resolve_recover_max_stale_days("1000000000")
        self.assertEqual(days, ps.MAX_RECOVER_STALE_DAYS)
        self.assertIn("上限", warn)
        kind, _ = ps.plan_scheduled_publish_recovery("2026-07-09T00:00:00Z", now=NOW, max_stale_days=10**9)
        self.assertEqual(kind, "overdue")


class ResolveEnvTest(unittest.TestCase):
    def test_values(self):
        self.assertEqual(ps.resolve_recover_max_stale_days(None), (7, None))
        self.assertEqual(ps.resolve_recover_max_stale_days("  "), (7, None))
        self.assertEqual(ps.resolve_recover_max_stale_days("14"), (14, None))
        self.assertEqual(ps.resolve_recover_max_stale_days("0"), (0, None))
        days, warn = ps.resolve_recover_max_stale_days("-3")
        self.assertEqual(days, 0)
        self.assertIn("負数", warn)
        for bad in ("abc", "7.5"):
            days, warn = ps.resolve_recover_max_stale_days(bad)
            self.assertEqual(days, 7, msg=bad)
            self.assertIn("整数ではない", warn)


if __name__ == "__main__":
    unittest.main()
