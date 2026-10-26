"""段階 6 Task 3: メタ生成への VidIQ の注入、根拠と劣化の記録、通知の出し分け。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_pipeline as ap  # noqa: E402
import claude_proposer as cp  # noqa: E402
import vidiq_adapter as va  # noqa: E402

EID = "vidiq:keyword_research:0123456789abcdef"


class FakeLedger:
    def __init__(self, enabled=False, vol=None, key="ch-meta"):
        self._enabled, self.vol, self.key = enabled, vol, key
        self.pushed = []

    def enabled(self):
        return self._enabled

    def resolve_channel_key(self):
        return self.key

    def fetch_vol(self, vol):
        return self.vol

    def push_meta(self, vol, title, description, tags, insights=None):
        self.pushed.append({"title": title, "insights": insights})


class MetaVidiqTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="metavq_"))
        self.folder = self.root / "5_test_261201"
        self.folder.mkdir()
        self.env = patch.dict(os.environ, {"APP_VIDIQ_DIR": str(self.root / "vidiq")})
        self.env.start()
        self.prompts = []
        self.cfg = {"persona": "p", "channel_name": "ch", "vidiq_keywords": ["lofi"]}

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _fake_run(self, _cli, prompt, **_kw):
        self.prompts.append(prompt)
        if '"titles"' in prompt:
            return '{"titles": ["Night Title"]}'
        if '"tags"' in prompt:
            return '{"tags": ["jazz", "night"]}'
        return '{"opening": "Calm.", "closing": "Bye.", "description": "Calm night description"}'

    def _run(self, ledger, cfg=None):
        notes = []
        with patch.object(ap, "_load_ledger_sync", return_value=ledger), \
                patch.object(ap, "_load_dashboard_config", return_value=cfg if cfg is not None else self.cfg), \
                patch.object(ap, "_load_suno_config", return_value={}), \
                patch.object(ap, "_vps_result_is_newer", return_value=False), \
                patch.object(cp, "gather_context", return_value={"songs": ["A"], "song_count": 1}), \
                patch.object(cp, "_run_claude", side_effect=self._fake_run), \
                patch.object(cp, "_load_benchmark_analysis", return_value=None), \
                patch("notify.notify", side_effect=lambda body, **kw: notes.append((body, kw))):
            self.assertIs(ap.step_meta(5, self.folder, False), True)
        rec = json.loads((self.folder / ap.META_RECORD_FILE).read_text(encoding="utf-8"))
        return rec, notes

    def _write_cache(self, fetched):
        path = va.cache_path("ch-meta")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"source": "vidiq", "channel_key": "ch-meta", "fetched_at": va._iso(fetched),
                                    "expires_at": va._iso(fetched + timedelta(hours=24)), "evidence_ids": [EID],
                                    "items": [{"category": "title", "insight": "VidIQ keyword_research（lofi）: volume 1200",
                                               "video_ids": [], "metrics": {}, "evidence_ids": [EID]}]}), encoding="utf-8")

    def test_fresh_local_cache_is_injected_and_evidence_recorded(self):
        self._write_cache(datetime.now(timezone.utc) - timedelta(hours=1))
        rec, notes = self._run(FakeLedger(enabled=False))
        self.assertEqual((rec["evidence_ids"], rec["insights_degraded"], rec["degraded_reason"]), ([EID], False, None))
        self.assertEqual(rec["title"], "Night Title")
        self.assertEqual(len(self.prompts), 3)
        for p in self.prompts:  # タイトル・説明・タグのすべてのプロンプトに入る
            self.assertIn("Learned insights (adapt, do not copy):", p)
            self.assertIn("volume 1200", p)
        self.assertEqual(notes, [])

    def test_expired_cache_completes_degraded_and_notifies_without_vps(self):
        self._write_cache(datetime.now(timezone.utc) - timedelta(hours=30))
        rec, notes = self._run(FakeLedger(enabled=False))
        self.assertEqual((rec["insights_degraded"], rec["degraded_reason"], rec["evidence_ids"]), (True, "expired", []))
        self.assertTrue((self.folder / "youtube_title.txt").is_file())  # 劣化でもメタは完走
        self.assertNotIn("volume 1200", " ".join(self.prompts))
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0][1]["kind"], "info")
        self.assertEqual(notes[0][1]["event_id"], "vidiq-degraded:ch-meta:5:expired")

    def test_not_configured_is_quiet_and_configured_never_fetched_is_unavailable(self):
        rec, notes = self._run(FakeLedger(enabled=False), cfg={"persona": "p"})
        self.assertEqual((rec["degraded_reason"], notes), ("not_configured", []))
        rec, notes = self._run(FakeLedger(enabled=False))
        self.assertEqual(rec["degraded_reason"], "unavailable")
        self.assertEqual(len(notes), 1)

    def test_vps_mode_uses_vol_hint_and_leaves_notification_to_vps(self):
        vol = {"insights_hint": "## title\n- VPS からの学び", "meta": None,
               "insights_vidiq": {"fresh": True, "evidence_ids": [EID]}}
        ledger = FakeLedger(enabled=True, vol=vol)
        rec, notes = self._run(ledger)
        self.assertIn("VPS からの学び", self.prompts[0])
        self.assertEqual(rec["evidence_ids"], [EID])
        self.assertEqual(ledger.pushed[-1]["insights"], {"evidence_ids": [EID], "insights_degraded": False, "degraded_reason": None})
        self.prompts.clear()
        ledger = FakeLedger(enabled=True, vol={"insights_hint": "", "insights_vidiq": {"fresh": False, "reason": "not_fetched"}})
        rec, notes = self._run(ledger)
        self.assertEqual(rec["degraded_reason"], "unavailable")  # 設定済みなのに VPS に無い: 通知の対象として送る
        self.assertEqual(ledger.pushed[-1]["insights"]["degraded_reason"], "unavailable")
        self.assertEqual(notes, [])  # VPS 連携時は VPS が通知する（二重にしない）

    def test_adopted_vps_meta_writes_record(self):
        vol = {"meta": {"title": "Remote", "description": "D", "tags": ["t"], "evidence_ids": [EID],
                        "insights_degraded": False, "degraded_reason": None}}
        with patch.object(ap, "_load_ledger_sync", return_value=FakeLedger(enabled=True, vol=vol)), \
                patch.object(ap, "_vps_result_is_newer", return_value=True), \
                patch.object(cp, "_run_claude", side_effect=AssertionError("must not generate")):
            self.assertIs(ap.step_meta(5, self.folder, False), True)
        rec = json.loads((self.folder / ap.META_RECORD_FILE).read_text(encoding="utf-8"))
        self.assertEqual((rec["title"], rec["evidence_ids"], rec["insights_degraded"]), ("Remote", [EID], False))
        old = {"meta": {"title": "Old", "description": "D", "tags": ["t"]}}  # 段階 6 より前の VPS メタ
        with patch.object(ap, "_load_ledger_sync", return_value=FakeLedger(enabled=True, vol=old)), \
                patch.object(ap, "_vps_result_is_newer", return_value=True):
            ap.step_meta(5, self.folder, False)
        rec = json.loads((self.folder / ap.META_RECORD_FILE).read_text(encoding="utf-8"))
        self.assertEqual((rec["insights_degraded"], rec["degraded_reason"]), (True, "not_fetched"))

    def test_meta_record_is_not_a_stage_output(self):
        import stage_state
        self.assertNotIn(ap.META_RECORD_FILE, [p.name for p in stage_state.stage_outputs("meta", self.folder, 5)])

    def test_broken_vidiq_never_breaks_meta(self):
        with patch.object(va, "load_cache", side_effect=RuntimeError("boom")):
            rec, _ = self._run(FakeLedger(enabled=False))
        self.assertEqual((rec["insights_degraded"], rec["degraded_reason"]), (True, "unavailable"))


if __name__ == "__main__":
    unittest.main()
