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
        return self.push_ok and self._enabled  # 実物も VPS 無効時は False

    push_ok = True


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


class MetaVidiqEdgeTest(MetaVidiqTest):
    """Task 3 レビュー対応: 打ち切られた学びの根拠を記録しない、旧 VPS、例外、カスタムプロンプト。"""

    def test_truncated_items_are_not_counted_as_evidence(self):
        fetched = datetime.now(timezone.utc) - timedelta(hours=1)
        items = [{"category": "title", "insight": f"学び{i:02d} " + "y" * 290, "video_ids": [], "metrics": {},
                  "evidence_ids": [f"vidiq:keyword_research:{i:016x}"]} for i in range(10)]
        path = va.cache_path("ch-meta")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"fetched_at": va._iso(fetched), "expires_at": va._iso(fetched + timedelta(hours=24)),
                                    "items": items, "evidence_ids": [i["evidence_ids"][0] for i in items]}), encoding="utf-8")
        rec, _ = self._run(FakeLedger(enabled=False))
        self.assertTrue(0 < len(rec["evidence_ids"]) < 10)
        for i, it in enumerate(items):
            if it["evidence_ids"][0] in rec["evidence_ids"]:
                self.assertIn(it["insight"], self.prompts[0])  # 根拠に数えた学びはプロンプトに全文がある
            else:
                self.assertNotIn(it["insight"], self.prompts[0])

    def test_old_vps_and_expired_vps_status(self):
        rec, notes = self._run(FakeLedger(enabled=True, vol={"insights_hint": "## t\n- x"}))  # insights_vidiq の無い旧 VPS
        self.assertEqual((rec["degraded_reason"], notes), ("unavailable", []))
        rec, _ = self._run(FakeLedger(enabled=True, vol={"insights_hint": "", "insights_vidiq": {"fresh": False, "reason": "expired"}}))
        self.assertEqual(rec["degraded_reason"], "expired")

    def test_exception_path_respects_not_configured(self):
        with patch.object(va, "load_cache", side_effect=RuntimeError("boom")):
            rec, notes = self._run(FakeLedger(enabled=False), cfg={"persona": "p"})
        self.assertEqual((rec["degraded_reason"], notes), ("not_configured", []))

    def test_custom_prompt_without_placeholder_still_gets_hint(self):
        self._write_cache(datetime.now(timezone.utc) - timedelta(hours=1))
        with patch.object(cp, "_load_master_prompt", lambda key, fallback: "Return JSON {{\"titles\": []}} count={count}"
                          if "title" in key else fallback):
            rec, _ = self._run(FakeLedger(enabled=False))
        self.assertIn("volume 1200", self.prompts[0])
        self.assertEqual(rec["evidence_ids"], [EID])

    def test_adopted_vps_meta_without_evidence_is_not_marked_as_used(self):
        vol = {"meta": {"title": "R", "description": "D", "tags": ["t"], "evidence_ids": [], "insights_degraded": False}}
        with patch.object(ap, "_load_ledger_sync", return_value=FakeLedger(enabled=True, vol=vol)), \
                patch.object(ap, "_vps_result_is_newer", return_value=True):
            ap.step_meta(5, self.folder, False)
        rec = json.loads((self.folder / ap.META_RECORD_FILE).read_text(encoding="utf-8"))
        self.assertEqual((rec["insights_degraded"], rec["degraded_reason"]), (True, "not_fetched"))

    def test_vps_unreachable_falls_back_to_mac_notification(self):
        ledger = FakeLedger(enabled=True, vol=None)  # vol を取れない・メタも届かない
        ledger.push_ok = False
        rec, notes = self._run(ledger)
        self.assertEqual(rec["degraded_reason"], "unavailable")
        self.assertEqual(len(notes), 1)  # VPS が受け取れないので Mac が通知（ローカル退避）

    def test_failed_generation_does_not_attach_new_evidence_to_old_meta(self):
        self._write_cache(datetime.now(timezone.utc) - timedelta(hours=1))
        for name, text in (("youtube_title.txt", "Old"), ("youtube_description.txt", "Old D"), ("youtube_tags.txt", "old")):
            (self.folder / name).write_text(text, encoding="utf-8")

        def failing(_cli, prompt, **_kw):
            if '"titles"' in prompt:
                raise RuntimeError("llm down")
            return self._fake_run(_cli, prompt)
        with patch.object(ap, "_load_ledger_sync", return_value=FakeLedger(enabled=False)), \
                patch.object(ap, "_load_dashboard_config", return_value=self.cfg), \
                patch.object(ap, "_load_suno_config", return_value={}), \
                patch.object(ap, "_vps_result_is_newer", return_value=False), \
                patch.object(cp, "gather_context", return_value={"songs": ["A"], "song_count": 1}), \
                patch.object(cp, "_run_claude", side_effect=failing), \
                patch.object(cp, "_load_benchmark_analysis", return_value=None), \
                patch("notify.notify") as n:
            ap.step_meta(5, self.folder, False)
        rec = json.loads((self.folder / ap.META_RECORD_FILE).read_text(encoding="utf-8"))
        self.assertEqual((rec["title"], rec["evidence_ids"], rec["generation_complete"]), ("Old", [], False))
        n.assert_not_called()

    def test_push_meta_payload_shape(self):
        import vps_ledger_sync as vls
        sent = []
        with patch.object(vls, "ensure_vol", return_value=9), patch.object(vls, "_request", lambda m, path, **kw: sent.append(kw["payload"]) or {}):
            self.assertTrue(vls.push_meta(5, "T", "D", ["a"]))
            vls.push_meta(5, "T", "D", ["a"], insights={"evidence_ids": [EID], "insights_degraded": False, "degraded_reason": None})
        with patch.object(vls, "ensure_vol", return_value=9), patch.object(vls, "_request", return_value=None):
            self.assertFalse(vls.push_meta(5, "T", "D", ["a"]))
        self.assertEqual(sent[0]["meta"], {"title": "T", "description": "D", "tags": ["a"]})
        self.assertEqual(sent[1]["meta"]["evidence_ids"], [EID])


if __name__ == "__main__":
    unittest.main()
