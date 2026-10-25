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
        p = ms.policy_from({"lufs_range": [-24, -10], "max_sec": float("nan")})
        self.assertEqual((p["lufs_min"], p["lufs_max"]), (-24.0, -10.0))
        self.assertEqual(p["max_sec"], 900.0)  # NaN は既定

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
        self.assertTrue(loud["passed"])  # ラウドネスは後処理で正規化されるため減点のみ
        self.assertLess(loud["score"], ok["score"])
        silent_lufs = ms.score({"duration_sec": 300, "silence_ratio": 0.0, "lufs": -45.0}, None, pol)
        self.assertFalse(silent_lufs["passed"])  # 実質無音は不合格
        # 尺か無音率が測れなければ測定不能（自動採用しない）
        partial = ms.score({"duration_sec": 300, "silence_ratio": None, "lufs": None}, None, pol)
        self.assertFalse(partial["passed"])
        self.assertTrue(partial["unmeasured"])
        lufs_only_missing = ms.score({"duration_sec": 300, "silence_ratio": 0.0, "lufs": None}, None, pol)
        self.assertTrue(lufs_only_missing["passed"])
        self.assertFalse(lufs_only_missing["unmeasured"])
        nothing = ms.score({"duration_sec": None, "silence_ratio": None, "lufs": None}, None, pol)
        self.assertFalse(nothing["passed"])
        self.assertTrue(nothing["unmeasured"])
        raw = ms.score({"duration_sec": 300, "silence_ratio": 0.0, "lufs": -14.0}, None, {"min_sec": 10})  # 生の dict でも正規化
        self.assertTrue(raw["passed"])

    def test_measure_parses_ffmpeg_output_with_fake_binaries(self):
        """偽の ffprobe / ffmpeg スクリプトで、末尾無音の除外と Summary の LUFS 解析を検証する。"""
        import stat
        with tempfile.TemporaryDirectory() as t:
            probe = Path(t) / "ffprobe"
            probe.write_text("#!/bin/sh\necho 14.0\n")
            ff = Path(t) / "ffmpeg"
            ff.write_text("#!/bin/sh\ncat >&2 <<'EOT'\n[silencedetect] silence_start: 5.0\n[silencedetect] silence_end: 8.0 | silence_duration: 3.0\n"
                          "[silencedetect] silence_start: 12.0\n[silencedetect] silence_end: 14.0 | silence_duration: 2.0\n"
                          "[Parsed_ebur128_1] Summary:\n  Integrated loudness:\n    I:         -22.4 LUFS\n  True peak:\n    Peak:      -18.5 dBFS\nEOT\n")
            for f in (probe, ff):
                f.chmod(f.stat().st_mode | stat.S_IEXEC)
            m = ms.measure(Path(t) / "x.mp3", ffmpeg=str(ff), ffprobe=str(probe))
            self.assertEqual(m["duration_sec"], 14.0)
            self.assertAlmostEqual(m["silence_ratio"], 3.0 / 14.0, places=3)  # 末尾 2 秒は除外
            self.assertEqual(m["trailing_silence_sec"], 2.0)
            self.assertEqual((m["lufs"], m["true_peak"]), (-22.4, -18.5))
            # ffmpeg が失敗したら無音率・ラウドネスは測定不能（0 にしない）
            bad = Path(t) / "ffmpeg_bad"
            bad.write_text("#!/bin/sh\nexit 1\n")
            bad.chmod(bad.stat().st_mode | stat.S_IEXEC)
            m2 = ms.measure(Path(t) / "x.mp3", ffmpeg=str(bad), ffprobe=str(probe))
            self.assertEqual((m2["duration_sec"], m2["silence_ratio"], m2["lufs"]), (14.0, None, None))
            self.assertTrue(ms.score(m2, None, ms.policy_from(None))["unmeasured"])
            # 終了 0 でも Summary が無ければ測定不能
            nosum = Path(t) / "ffmpeg_nosum"
            nosum.write_text("#!/bin/sh\necho '[silencedetect] silence_start: 1.0' >&2\n")
            nosum.chmod(nosum.stat().st_mode | stat.S_IEXEC)
            m3 = ms.measure(Path(t) / "x.mp3", ffmpeg=str(nosum), ffprobe=str(probe))
            self.assertIsNone(m3["silence_ratio"])

    def test_effective_duration_excludes_trailing_silence(self):
        pol = ms.policy_from(None)
        r = ms.score({"duration_sec": 300.0, "silence_ratio": 0.0, "lufs": -14.0, "trailing_silence_sec": 240.0}, None, pol)
        self.assertFalse(r["passed"])  # 実質 60 秒は最小尺 120 秒未満
        self.assertEqual(r["effective_duration_sec"], 60.0)
        r = ms.score({"duration_sec": 300.0, "silence_ratio": 0.0, "lufs": -14.0, "trailing_silence_sec": 10.0}, None, pol)
        self.assertTrue(r["passed"])

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

    def test_all_failed_is_quality_block_and_attempts_are_recorded(self):
        table = {k: BAD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        with self.assertRaises(catalog.SelectionPending) as cm:
            catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        self.assertIn("品質待機", str(cm.exception))
        self.assertIn("試行 1 回目", str(cm.exception))
        self.assertFalse(catalog.get_review(self.batch)["ready"])
        self.assertEqual(len(catalog.list_scores(self.batch)), 4)  # 不合格でもスコアは残す
        with self.assertRaises(catalog.SelectionPending) as cm2:
            catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        self.assertIn("試行 2 回目", str(cm2.exception))
        self.assertEqual(catalog.ai_select_attempts(self.batch), 2)

    def test_unmeasured_is_not_adopted_and_reported_separately(self):
        none = {"duration_sec": None, "silence_ratio": None, "lufs": None, "true_peak": None}
        table = {k: none for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        with self.assertRaises(catalog.SelectionPending) as cm:
            catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        self.assertIn("測定不能", str(cm.exception))
        # 一部だけ測れた群: 測定不能のテイクは選ばれない
        table = {"Silver Avenue.mp3": none, "Silver Avenue_2.mp3": GOOD, "Last Light.mp3": GOOD, "Last Light_2.mp3": GOOD}
        r = catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        selected = {t["clip_id"] for t in catalog.get_review(self.batch)["takes"] if t["selected"]}
        self.assertIn("clip-1", selected)
        self.assertNotIn("clip-0", selected)

    def test_human_heart_during_measurement_is_not_overridden(self):
        """測定中に人がハートを付けたら、AI はその群を採用しない（人の採用を尊重）。"""
        table = {k: GOOD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        calls = []
        def measurer(path):
            calls.append(path.name)
            if path.name == "Silver Avenue_2.mp3":
                catalog.choose_take(self.batch, "clip-0", True)  # 測定の最中に人が clip-0 を採用
            return table[path.name]
        r = catalog.select_takes_ai(self.folder, measurer=measurer)
        selected = {t["clip_id"] for t in catalog.get_review(self.batch)["takes"] if t["selected"]}
        self.assertIn("clip-0", selected)   # 人の採用が残る
        self.assertNotIn("clip-1", selected)
        self.assertEqual((r["groups_human"], r["groups_selected_by_ai"]), (1, 1))

    def test_tie_prefers_first_take_in_order(self):
        table = {k: GOOD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        selected = {t["clip_id"] for t in catalog.get_review(self.batch)["takes"] if t["selected"]}
        self.assertEqual(selected, {"clip-0", "clip-2"})

    def test_partial_failure_leaves_group_unselected(self):
        table = {"Silver Avenue.mp3": GOOD, "Silver Avenue_2.mp3": BAD, "Last Light.mp3": BAD, "Last Light_2.mp3": BAD}
        r = catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        self.assertEqual((r["groups_selected_by_ai"], r["groups_failed"]), (1, 1))
        self.assertFalse(r["ready"])
        self.assertEqual(r["blocked_reason"], "quality")
        manifest = json.loads((self.folder / ".music_review.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["scores"]), 4)  # 一部不合格でも全テイクの採点がマニフェストに出る

    def test_dry_run_changes_nothing_and_never_raises(self):
        table = {k: GOOD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        r = catalog.select_takes_ai(self.folder, measurer=_measurer(table), dry_run=True)
        self.assertEqual(len(r["scores"]), 4)
        self.assertEqual(catalog.list_scores(self.batch), [])
        self.assertFalse(catalog.get_review(self.batch)["ready"])
        bad = {k: BAD for k in table}
        r = catalog.select_takes_ai(self.folder, measurer=_measurer(bad), dry_run=True)
        self.assertEqual(r["blocked_reason"], "quality")
        self.assertEqual(catalog.ai_select_attempts(self.batch), 0)  # dry-run は試行に数えない

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
        # ブリーフの形: music_selection_mode と music_selection 直下の方針、lufs_range
        (self.root / ".app_channel_config.json").write_text(json.dumps(
            {"music_selection_mode": "human", "music_selection": {"mode": "ai", "min_sec": 30, "lufs_range": [-24, -10]}}), encoding="utf-8")
        cfg = catalog.load_music_selection_config(self.folder)
        self.assertEqual(cfg["mode"], "human")  # トップレベルのキーを優先
        self.assertEqual(cfg["policy"]["min_sec"], 30)
        self.assertEqual(ms.policy_from(cfg["policy"])["lufs_min"], -24.0)
        (self.root / ".app_channel_config.json").write_text(json.dumps({"music_selection": {"mode": "bogus"}}), encoding="utf-8")
        self.assertEqual(catalog.load_music_selection_config(self.folder)["mode"], "ai")
        # vol の親の設定が、環境変数 APP_CHANNEL_FOLDER（別チャンネル）より優先される
        other = self.root / "other"
        other.mkdir()
        (other / ".app_channel_config.json").write_text(json.dumps({"music_selection_mode": "ai"}), encoding="utf-8")
        (self.root / ".app_channel_config.json").write_text(json.dumps({"music_selection_mode": "human"}), encoding="utf-8")
        with patch.dict(os.environ, {"APP_CHANNEL_FOLDER": str(other)}):
            self.assertEqual(catalog.load_music_selection_config(self.folder)["mode"], "human")

    def test_manifest_carries_scores_attempts_and_reason(self):
        table = {k: BAD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        with self.assertRaises(catalog.SelectionPending) as cm:
            catalog.select_takes_ai(self.folder, measurer=_measurer(table))
        self.assertEqual(cm.exception.reason, "quality")
        manifest = json.loads((self.folder / ".music_review.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["ai_select_attempts"], 1)
        self.assertEqual(set(manifest["scores"]), {"clip-0", "clip-1", "clip-2", "clip-3"})
        self.assertEqual(manifest["scores"]["clip-0"]["passed"], 0)
        self.assertTrue(manifest["scores"]["clip-0"]["reason"])

    def test_pipeline_gate_ai_default_adopts(self):
        import app_pipeline as p
        table = {k: GOOD for k in ("Silver Avenue.mp3", "Silver Avenue_2.mp3", "Last Light.mp3", "Last Light_2.mp3")}
        with patch("music_scoring.measure", _measurer(table)):
            self.assertIsNone(p._music_selection_gate(self.folder))  # ai 既定: 自動で採用して通過
        self.assertTrue(catalog.get_review(self.batch)["ready"])
        # processed=True のゲートは、選定済みでも後処理前なら待機し、測定はしない。理由は processing
        with patch("music_scoring.measure", side_effect=AssertionError("測り直してはいけない")):
            self.assertEqual(p._music_selection_gate(self.folder, processed=True), "awaiting_selection")
        self.assertEqual(p._LAST_SELECTION_REASON, "processing")
        # 通過したら理由は初期値に戻る
        self.assertIsNone(p._music_selection_gate(self.folder))
        self.assertEqual(p._LAST_SELECTION_REASON, "human")

    def test_select_cli_exit_codes(self):
        import subprocess
        cli = [sys.executable, str(Path(__file__).resolve().parents[1] / "app_music_select.py")]
        env = dict(os.environ)
        r = subprocess.run(cli + [str(self.root / "nope"), "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        (self.root / ".app_channel_config.json").write_text(json.dumps({"music_selection_mode": "human"}), encoding="utf-8")
        r = subprocess.run(cli + [str(self.folder), "--json"], env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 79)
        out = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual((out["mode"], out["blocked_reason"]), ("human", "human"))

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
        self.assertEqual(p._LAST_SELECTION_REASON, "quality")
        (self.root / ".app_channel_config.json").write_text(json.dumps({"music_selection_mode": "human"}), encoding="utf-8")
        self.assertEqual(p._music_selection_gate(self.folder), "awaiting_selection")
        self.assertEqual(p._LAST_SELECTION_REASON, "human")


if __name__ == "__main__":
    unittest.main()
