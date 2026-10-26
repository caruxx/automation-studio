"""段階 5 Task 2: パイプラインのサムネ工程・投稿のサムネ照合・モデルの扱い・stage_state・CLI。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_pipeline as ap  # noqa: E402
import stage_state as ss  # noqa: E402
import thumbnail_selection as ts  # noqa: E402
from PIL import Image  # noqa: E402


def _img(path: Path, color=(10, 20, 30)) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 36), color).save(str(path))
    return path


class ThumbnailPipelineTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tpipe_"))
        self.folder = self.root / "7_test_261201"
        self.folder.mkdir()
        # composite の成果物（動画用）は既にある: これだけではスキップしない
        _img(self.folder / "サムネイル.jpg", (1, 1, 1))
        _img(self.folder / "vol7.jpg", (2, 2, 2))
        _img(self.folder / "vol7.png", (3, 3, 3))
        (self.folder / "youtube_title.txt").write_text("Title", encoding="utf-8")
        self.p = [patch.object(ap, "_load_dashboard_config", return_value={"channel_name": "ch", "thumbnail_composition": {}}),
                  patch.object(ap, "_gather_reference_images", return_value=[]),
                  patch.object(ap, "_build_thumbnail_prompt", return_value="base prompt"),
                  patch.object(ap, "_load_suno_config", return_value={})]
        for x in self.p:
            x.start()
        os.environ.pop("OPENAI_API_KEY", None)

    def tearDown(self):
        for x in self.p:
            x.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    def _fake_run(self, scores):
        """codex_imagegen.run と compose_candidate と score_thumbnails を差し替える。"""
        import codex_imagegen, app_image_composite, app_thumbnail_scoring
        def run(prompts, out_dir, **kw):
            self.gen_kw = kw
            res = []
            for i, (prompt, fn) in enumerate(prompts, 1):
                res.append({"ok": True, "path": str(_img(Path(out_dir) / f"{fn}.png", (i * 30, 0, 0))), "filename": fn})
            return res
        def compose(bg, out, vol, cfg):
            shutil.copyfile(bg, out)
            return out
        def score(generated_paths, **kw):
            evs = [{"filename": Path(p).name, "score_total": scores.get(Path(p).name, 40),
                    "ctr_predict": 9 if scores.get(Path(p).name, 40) >= 80 else 2, "similarity": 5} for p in generated_paths]
            return {"evaluations": evs, "error": "", "backend": "claude"}
        return [patch.object(codex_imagegen, "run", run), patch.object(app_image_composite, "compose_candidate", compose),
                patch.object(app_thumbnail_scoring, "score_thumbnails", score)]

    def _with(self, patches, fn):
        for x in patches: x.start()
        try:
            return fn()
        finally:
            for x in patches: x.stop()

    def test_existing_images_do_not_skip_and_selection_is_used_for_upload(self):
        r = self._with(self._fake_run({"a1_2.jpg": 91}), lambda: ap.step_thumbnail(7, self.folder, False))
        self.assertIs(r, True)
        rec = ts.load(self.folder)
        self.assertEqual(rec["selected_id"].split("/", 1)[1], "a1/a1_2.jpg")
        self.assertEqual((rec["backend"], rec["model"], rec["model_enforced"], rec["judge_backend"]), ("codex", "gpt-image-2", False, "claude"))
        self.assertEqual(self.gen_kw["model"], "gpt-image-2")
        self.assertTrue(ss.stage_done("thumbnail", self.folder, 7))
        import app_youtube
        thumb = app_youtube.find_thumbnail(self.folder)
        self.assertEqual(thumb.name, ts.SELECTED_FILE)
        self.assertEqual(ts.sha256_file(thumb), rec["selected_hash"])
        # 2 回目は採用済みでスキップ（再生成しない）
        r2 = self._with([patch("codex_imagegen.run", side_effect=AssertionError("must not regenerate"))], lambda: ap.step_thumbnail(7, self.folder, False))
        self.assertIs(r2, True)
        # 採用画像を差し替えると find_thumbnail は None（投稿しない）
        _img(self.folder / ts.SELECTED_FILE, (99, 99, 99))
        self.assertIsNone(app_youtube.find_thumbnail(self.folder))
        self.assertFalse(ss.stage_done("thumbnail", self.folder, 7))

    def test_all_rejected_is_quality_wait_and_not_retried_for_same_input(self):
        r = self._with(self._fake_run({}), lambda: ap.step_thumbnail(7, self.folder, False))
        self.assertEqual(r, "awaiting_thumbnail")
        self.assertEqual(ts.load(self.folder)["status"], "quality_wait")
        r2 = self._with([patch("codex_imagegen.run", side_effect=AssertionError("must not regenerate"))], lambda: ap.step_thumbnail(7, self.folder, False))
        self.assertEqual(r2, "awaiting_thumbnail")
        import app_orchestrator as o
        self.assertEqual(o._normalize_result("awaiting_thumbnail"), o.Exit.SELECTION_PENDING)
        w = o.WORKERS["thumbnail"]
        with patch.object(o, "_stage_artifact_done", return_value=True):
            w._slot_available = lambda **kw: True
            self.assertFalse(w.can_run(7, self.folder))  # 品質待機中は再投入しない
        # 人が採用すると done
        cid = ts.load(self.folder)["candidate_ids"][0]
        ts.adopt(self.folder, cid)
        self.assertIs(ap.step_thumbnail(7, self.folder, False), True)

    def test_model_is_not_silently_substituted(self):
        with patch.object(ap, "_load_dashboard_config", return_value={"image_model": "gpt-image-2.5-x"}):
            with self.assertRaises(ValueError):
                ap._thumbnail_image_backend({"image_model": "gpt-image-2.5-x"})
            r = ap.step_thumbnail(7, self.folder, False)
        self.assertIs(r, False)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-key-not-real"}):
            self.assertEqual(ap._thumbnail_image_backend({"image_model": "gpt-image-2.5-x"}), ("api", "gpt-image-2.5-x", True))
        self.assertEqual(ap._thumbnail_image_backend({}), ("codex", "gpt-image-2", False))

    def test_legacy_find_thumbnail_has_no_background_fallback(self):
        import app_youtube
        legacy = self.root / "8_legacy"
        _img(legacy / "vol8.jpg")
        self.assertIsNone(app_youtube.find_thumbnail(legacy))  # vol*.jpg（動画背景）には落ちない
        _img(legacy / "サムネイル.jpg")
        self.assertEqual(app_youtube.find_thumbnail(legacy).name, "サムネイル.jpg")

    def test_cli_dry_run_and_adopt_rejects_out_of_candidate(self):
        here = Path(__file__).resolve().parents[1]
        self._with(self._fake_run({}), lambda: ap.step_thumbnail(7, self.folder, False))
        import studio
        info = {"ok": True, "folder": str(self.folder), "channel_id": "ch"}
        import argparse, io, contextlib
        def run(**kw):
            ns = argparse.Namespace(vol="7", resolve_num=None, channel=None, adopt=None, dry_run=False, json=True)
            for k, v in kw.items(): setattr(ns, k, v)
            buf = io.StringIO()
            with patch.object(studio, "resolve_vol", return_value=info), contextlib.redirect_stdout(buf):
                code = studio.run_thumbnail_select(ns)
            return code, json.loads(buf.getvalue().strip().splitlines()[-1])
        code, out = run(adopt="nope/a1/a1_1.jpg")
        self.assertEqual((code, out["error"]), (1, "adopt_rejected"))
        cid = ts.load(self.folder)["candidate_ids"][1]
        code, out = run(adopt=cid)
        self.assertEqual((code, out["selected_id"]), (0, cid))
        code, out = run(dry_run=True)
        self.assertEqual(code, 0); self.assertEqual(out["cmd"][-4:], ["--only", "thumbnail", "--channel-id", "ch"])


if __name__ == "__main__":
    unittest.main()
