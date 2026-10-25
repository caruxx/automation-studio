"""AI 選曲（select 工程）: 採点方針、ハート群の尊重、全不合格の品質待機、ゲートの ai/human 分岐。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_music_catalog as catalog  # noqa: E402
import music_scoring as ms  # noqa: E402


class ScoringTest(unittest.TestCase):
    def test_policy_defaults_and_overrides(self):
        p = ms.policy_from({"min_sec": 60, "weights": {"duration": 1, "silence": "x"}, "lufs_min": -30, "lufs_max": -40})
        self.assertEqual(p["min_sec"], 60.0)
        self.assertEqual(p["weights"]["duration"], 1.0)
        self.assertEqual(p["weights"]["silence"], ms.DEFAULT_POLICY["weights"]["silence"])
        self.assertEqual((p["lufs_min"], p["lufs_max"]), (-20.0, -8.0))  # 逆転は既定に戻す

    def test_pass_fail_boundaries(self):
        pol = ms.policy_from(None)
        ok = ms.score({"duration_sec": 300, "silence_ratio": 0.05, "lufs": -14.0}, None, pol)
        self.assertTrue(ok["passed"])
        self.assertGreater(ok["score"], 60)
        short = ms.score({"duration_sec": 30, "silence_ratio": 0.0, "lufs": -14.0}, None, pol)
        self.assertFalse(short["passed"])
        self.assertTrue(any("短い" in r for r in short["reasons"]))
        silent = ms.score({"duration_sec": 300, "silence_ratio": 0.5, "lufs": -14.0}, None, pol)
        self.assertFalse(silent["passed"])
        loud = ms.score({"duration_sec": 300, "silence_ratio": 0.0, "lufs": -3.0}, None, pol)
        self.assertFalse(loud["passed"])
        # 測れなかった項目は不合格にしない（理由だけ残す）
        partial = ms.score({"duration_sec": 300, "silence_ratio": None, "lufs": None}, None, pol)
        self.assertTrue(partial["passed"])
        self.assertEqual(len(partial["reasons"]), 2)
        nothing = ms.score({"duration_sec": None, "silence_ratio": None, "lufs": None}, None, pol)
        self.assertFalse(nothing["passed"])

    def test_prompt_fit_parsing(self):
        self.assertIsNone(ms.prompt_fit("A", "", llm=lambda q: "0.9"))
        self.assertIsNone(ms.prompt_fit("A", "p", llm=None))
        self.assertEqual(ms.prompt_fit("A", "p", llm=lambda q: "答え: 0.75"), 0.75)
        self.assertEqual(ms.prompt_fit("A", "p", llm=lambda q: "2"), 1.0)
        self.assertIsNone(ms.prompt_fit("A", "p", llm=lambda q: "わからない"))
        def boom(q): raise RuntimeError("x")
        self.assertIsNone(ms.prompt_fit("A", "p", llm=boom))
        fit = ms.score({"duration_sec": 300, "silence_ratio": 0.0, "lufs": -14.0}, 1.0, ms.policy_from(None))
        nofit = ms.score({"duration_sec": 300, "silence_ratio": 0.0, "lufs": -14.0}, 0.0, ms.policy_from(None))
        self.assertGreater(fit["score"], nofit["score"])


def _measurer(table):
    def m(path):
        return table[Path(path).name]
    return m


GOOD = {"duration_sec": 300.0, "silence_ratio": 0.02, "lufs": -14.0, "true_peak": -1.5}
BAD = {"duration_sec": 20.0, "silence_ratio": 0.6, "lufs": -30.0, "true_peak": -1.5}
BETTER = {"duration_sec": 500.0, "silence_ratio": 0.0, "lufs": -14.0, "true_peak": -1.5}


class SelectTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "1_review_260929"
        self.folder.mkdir()
        self.env = patch.dict(os.environ, {"APP_MUSIC_CATALOG_DB": str(self.root / "music.sqlite3")})
        self.env.start()
        self.seed = patch.object(catalog, "_seeded", True)
        self.seed.start()
        saved = []
        for n, (title, name) in enumerate([("Silver Avenue", "Silver Avenue.mp3"), ("Silver Avenue", "Silver Avenue_2.mp3"),
                                           ("Last Light", "Last Light.mp3"), ("Last Light", "Last Light_2.mp3")]):
            (self.folder / name).write_bytes(f"o{n}".encode())
            saved.append({"song_id": f"clip-{n}", "title": title, "file": name})
        self.batch = catalog.register_download(self.folder, saved, "review", 4)

    def tearDown(self):
        self.seed.stop()
        self.env.stop()
        self.temp.cleanup()

    def test_ai_selects_best_per_group_and_respects_hearts(self):
        catalog.choose_take(self.batch, "clip-0", True)  # Silver Avenue は人が採用
        table = {"Silver Avenue.mp3": BAD, "Silver Avenue_2.mp3": GOOD, "Last Light.mp3": GOOD, "Last Light_2.mp3": BETTER}
        r = catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        self.assertEqual((r["groups_total"], r["groups_human"], r["groups_selected_by_ai"], r["groups_failed"]), (2, 1, 1, 0))
        self.assertTrue(r["ready"])
        state = catalog.get_review(self.batch)
        selected = {t["clip_id"] for t in state["takes"] if t["selected"]}
        self.assertEqual(selected, {"clip-0", "clip-3"})  # 人の採用はそのまま、Last Light は高得点の clip-3
        self.assertNotIn("clip-0", r["scores"])  # 人の採用群は採点しない
        scores = {s["clip_id"]: s for s in catalog.list_scores(self.batch)}
        self.assertEqual(set(scores), {"clip-2", "clip-3"})
        self.assertEqual(scores["clip-3"]["passed"], 1)
        manifest = json.loads((self.folder / ".music_review.json").read_text(encoding="utf-8"))
        self.assertTrue(manifest["ready"])

    def test_all_failed_is_quality_block(self):
        table = {k: BAD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        with self.assertRaises(catalog.SelectionPending) as cm:
            catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        self.assertIn("品質待機", str(cm.exception))
        self.assertFalse(catalog.get_review(self.batch)["ready"])
        self.assertEqual(len(catalog.list_scores(self.batch)), 4)  # 不合格でもスコアは残す

    def test_partial_failure_leaves_group_unselected(self):
        table = {"Silver Avenue.mp3": GOOD, "Silver Avenue_2.mp3": BAD, "Last Light.mp3": BAD, "Last Light_2.mp3": BAD}
        r = catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        self.assertEqual((r["groups_selected_by_ai"], r["groups_failed"]), (1, 1))
        self.assertFalse(r["ready"])

    def test_dry_run_changes_nothing(self):
        table = {k: GOOD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        r = catalog.select_takes_ai(self.folder, measurer=_measurer(table), dry_run=True)
        self.assertEqual(len(r["scores"]), 4)
        self.assertEqual(catalog.list_scores(self.batch), [])
        self.assertFalse(catalog.get_review(self.batch)["ready"])

    def test_incomplete_download_is_pending(self):
        (self.folder / "Last Light_2.mp3").unlink()
        with self.assertRaises(catalog.SelectionPending):
            catalog.select_takes_ai(self.folder, measurer=_measurer({}))

    def test_channel_config_mode_and_policy(self):
        self.assertEqual(catalog.load_music_selection_config(self.folder)["mode"], "ai")  # 未設定は ai
        (self.root / ".app_channel_config.json").write_text(json.dumps(
            {"music_selection": {"mode": "human", "policy": {"min_sec": 10}, "prompt": "lofi"}}), encoding="utf-8")
        cfg = catalog.load_music_selection_config(self.folder)
        self.assertEqual((cfg["mode"], cfg["policy"]["min_sec"], cfg["prompt"]), ("human", 10, "lofi"))
        (self.root / ".app_channel_config.json").write_text(json.dumps({"music_selection": {"mode": "bogus"}}), encoding="utf-8")
        self.assertEqual(catalog.load_music_selection_config(self.folder)["mode"], "ai")

    def test_pipeline_gate_ai_then_human(self):
        import app_pipeline as p
        table = {k: GOOD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        with patch.object(catalog, "select_takes_ai", lambda folder, **kw: catalog.select_takes_ai.__wrapped__(folder, measurer=_measurer(table), **kw)) \
                if False else patch("music_scoring.measure", _measurer(table)):
            self.assertIsNone(p._music_selection_gate(self.folder))  # ai 既定: 自動で採用して通過
        self.assertTrue(catalog.get_review(self.batch)["ready"])

    def test_pipeline_gate_human_waits(self):
        import app_pipeline as p
        (self.root / ".app_channel_config.json").write_text(json.dumps({"music_selection": {"mode": "human"}}), encoding="utf-8")
        with patch("music_scoring.measure", lambda path: GOOD):
            self.assertEqual(p._music_selection_gate(self.folder), "awaiting_selection")
        self.assertFalse(catalog.get_review(self.batch)["ready"])

    def test_pipeline_gate_quality_block(self):
        import app_pipeline as p
        with patch("music_scoring.measure", lambda path: BAD):
            self.assertEqual(p._music_selection_gate(self.folder), "awaiting_selection")


if __name__ == "__main__":
    unittest.main()
