"""段階 5: サムネ AI 選定の中核（候補外の採用 0、全不合格は品質待機、採用画像のハッシュ一致、adopt / verify）。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import thumbnail_selection as ts  # noqa: E402
from PIL import Image  # noqa: E402


def _img(path: Path, color) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36), color).save(str(path))
    return path


class SelectionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tsel_"))
        self.folder = self.root / "9_test"
        self.folder.mkdir()
        self.calls = {"gen": 0, "score": []}

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _gen(self, colors_per_attempt, *, enforced=True, backend="api", infra=""):
        def generate(bg_dir, attempt, n, hint):
            self.calls["gen"] += 1
            if infra:
                return ts.GenerateResult([], infra_error=infra)
            cols = colors_per_attempt[min(attempt, len(colors_per_attempt)) - 1]
            return ts.GenerateResult([_img(bg_dir / f"bg{i}.png", c) for i, c in enumerate(cols[:n], 1)],
                                     model="gpt-image-2", backend=backend, model_enforced=enforced)
        return generate

    @staticmethod
    def _compose(bg, out):
        shutil.copyfile(bg, out)

    def _score(self, table, extra=None):
        """table: {attempt: {filename_index: score}}。extra: 候補外のファイル名の評価を混ぜる。"""
        def score(paths):
            self.calls["score"].append([p.name for p in paths])
            evs = []
            for p in paths:
                att = int(p.name.split("_")[0][1:])
                idx = int(p.stem.split("_")[1])
                s = table.get(att, {}).get(idx, 50)
                evs.append({"filename": p.name, "score_total": s, "ctr_predict": 9.0 if s >= 80 else 3.0, "similarity": 10,
                            "score_breakdown": {}})
            for name in (extra or []):
                evs.append({"filename": name, "score_total": 99, "ctr_predict": 20, "similarity": 0})
            return ts.ScoreOutput(evs, backend="claude")
        return score

    def test_selects_best_passing_candidate_and_hash_matches(self):
        res = ts.run_selection(self.folder, 9, generate=self._gen([[(255, 0, 0), (0, 255, 0), (0, 0, 255)]]),
                               score=self._score({1: {1: 60, 2: 92, 3: 85}}), compose=self._compose,
                               run_id="r1", n_per_attempt=3, log=lambda m: None)
        self.assertEqual(res.status, "selected")
        self.assertEqual(res.selected_id, "r1/a1/a1_2.jpg")
        self.assertEqual(ts.sha256_file(self.folder / ts.SELECTED_FILE), res.selected_hash)
        self.assertEqual(ts.verify(self.folder), (True, "ok"))
        rec = json.loads((self.folder / ts.SELECTION_FILE).read_text(encoding="utf-8"))
        for k in ("candidate_ids", "candidate_hashes", "selected_id", "rejected_reasons", "model", "backend", "selection_revision"):
            self.assertIn(k, rec)
        self.assertEqual(rec["candidate_ids"], ["r1/a1/a1_1.jpg", "r1/a1/a1_2.jpg", "r1/a1/a1_3.jpg"])
        self.assertIn("r1/a1/a1_1.jpg", rec["rejected_reasons"])
        self.assertEqual((rec["judge_backend"], rec["backend"], rec["model_enforced"]), ("claude", "api", True))
        self.assertEqual(rec["predicted_ctr"], 9.0)
        self.assertTrue((self.folder / ts.CANDIDATE_ROOT / "r1" / "a1" / "contact_sheet.jpg").is_file())
        # 改ざん → verify 不一致
        _img(self.folder / ts.SELECTED_FILE, (1, 2, 3))
        self.assertEqual(ts.verify(self.folder)[1], "selected_hash_mismatch")

    def test_out_of_candidate_evaluations_are_ignored(self):
        _img(self.folder / ts.CANDIDATE_ROOT / "old" / "a1" / "a1_9.jpg", (9, 9, 9))  # 他 run の画像
        res = ts.run_selection(self.folder, 9, generate=self._gen([[(255, 0, 0)]]),
                               score=self._score({1: {1: 40}}, extra=["a1_9.jpg", "../../old/a1/a1_9.jpg", "ghost.jpg"]),
                               compose=self._compose, run_id="r2", max_attempts=1, n_per_attempt=1, log=lambda m: None)
        self.assertEqual(res.status, "quality_wait")
        rec = ts.load(self.folder)
        self.assertEqual(rec["history"][-1]["ignored_evaluations"], ["a1_9.jpg", "a1_9.jpg", "ghost.jpg"])
        self.assertIsNone(rec["selected_id"])
        self.assertFalse((self.folder / ts.SELECTED_FILE).exists())

    def test_all_rejected_three_times_is_quality_wait_and_feedback_regenerates(self):
        res = ts.run_selection(self.folder, 9, generate=self._gen([[(1, 1, 1)] * 4, [(2, 2, 2)] * 4, [(3, 3, 3)] * 4]),
                               score=self._score({}), compose=self._compose, run_id="r3", log=lambda m: None)
        self.assertEqual((res.status, res.attempts), ("quality_wait", 3))
        self.assertEqual(self.calls["gen"], 3)
        rec = ts.load(self.folder)
        self.assertEqual(len(rec["candidate_ids"]), 12)
        self.assertEqual(len(rec["rejected_reasons"]), 12)
        self.assertEqual(ts.verify(self.folder)[0], False)
        # 人が直近 run の候補から採用できる（候補外は拒否）
        with self.assertRaises(ValueError):
            ts.adopt(self.folder, "other/a1/a1_1.jpg")
        r = ts.adopt(self.folder, "r3/a2/a2_3.jpg")
        self.assertEqual(r.status, "selected")
        self.assertEqual(ts.verify(self.folder), (True, "ok"))
        self.assertEqual(ts.load(self.folder)["decided_by"], "human")
        self.assertEqual(ts.load(self.folder)["selection_revision"], 2)

    def test_generation_failure_and_cost_cap(self):
        res = ts.run_selection(self.folder, 9, generate=self._gen([[]], infra="quota"), score=self._score({}),
                               compose=self._compose, run_id="r4", log=lambda m: None)
        self.assertEqual((res.status, res.reason), ("generation_failure", "quota"))
        res = ts.run_selection(self.folder, 9, generate=self._gen([[(1, 1, 1)] * 4] * 3), score=self._score({}),
                               compose=self._compose, run_id="r5", max_total=6, log=lambda m: None)
        self.assertEqual(res.status, "quality_wait")
        self.assertEqual(len(ts.load(self.folder)["candidate_ids"]), 6)  # 4 + 2（上限）

    def test_adopt_rejects_modified_candidate(self):
        ts.run_selection(self.folder, 9, generate=self._gen([[(1, 1, 1)]]), score=self._score({}), compose=self._compose,
                         run_id="r6", max_attempts=1, n_per_attempt=1, log=lambda m: None)
        _img(self.folder / ts.CANDIDATE_ROOT / "r6" / "a1" / "a1_1.jpg", (200, 1, 1))
        with self.assertRaises(ValueError):
            ts.adopt(self.folder, "r6/a1/a1_1.jpg")


if __name__ == "__main__":
    unittest.main()
