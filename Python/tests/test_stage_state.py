"""stage_state: done_validator + input_hash による完了判定と、前段の出力変更による失効。"""
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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import exit_codes as ec  # noqa: E402
import stage_state as ss  # noqa: E402


def _touch(p: Path, data: bytes = b"x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


class StageStateTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="stage_state_"))
        self.folder = self.root / "7_test"
        self.folder.mkdir()
        self.vol = 7

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_validators_match_workflow_json(self):
        names = {s["done_validator"] for s in ec.stages()}
        self.assertEqual(names - set(ss.VALIDATORS), set())
        for st in ec.stage_order():
            self.assertIsNotNone(ss.validator_for(st), st)

    def test_legacy_vol_without_record_is_done_by_artifacts_only(self):
        self.assertFalse(ss.stage_done("meta", self.folder, self.vol))
        _touch(self.folder / "youtube_title.txt", b"t")
        self.assertTrue(ss.stage_done("meta", self.folder, self.vol))
        self.assertFalse(ss.is_stale("meta", self.folder, self.vol))
        self.assertFalse((self.folder / ss.STATE_FILE).exists())

    def test_mark_done_records_hash_and_invalidates_when_input_changes(self):
        music = _touch(self.folder / "music" / "01 song.mp3", b"a" * 10)
        _touch(self.folder / "qa_report.json", json.dumps({"passed": True}).encode())
        _touch(self.folder / "youtube_title.txt", b"title")
        rec = ss.mark_done("meta", self.folder, self.vol)  # meta は rename + qa に依存
        self.assertEqual(rec["workflow_version"], ec.WORKFLOW_VERSION)
        self.assertTrue(ss.stage_done("meta", self.folder, self.vol))
        data = json.loads((self.folder / ss.STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(data["schema_version"], 1)
        self.assertIn("meta", data["stages"])
        # 前段 rename の出力（music/）を差し替える → meta の完了が失効する（成果物はそのまま）
        music.write_bytes(b"b" * 11)
        os.utime(music, ns=(1, 1))
        self.assertTrue((self.folder / "youtube_title.txt").exists())
        self.assertTrue(ss.is_stale("meta", self.folder, self.vol))
        self.assertFalse(ss.stage_done("meta", self.folder, self.vol))
        # qa_report.json は内容ハッシュ: 同内容の書き直しでは失効しない
        ss.mark_done("meta", self.folder, self.vol)
        self.assertTrue(ss.stage_done("meta", self.folder, self.vol))
        (self.folder / "qa_report.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
        self.assertTrue(ss.stage_done("meta", self.folder, self.vol))
        (self.folder / "qa_report.json").write_text(json.dumps({"passed": True, "note": 1}), encoding="utf-8")
        self.assertFalse(ss.stage_done("meta", self.folder, self.vol))
        # 再実行して記録し直せば done に戻る。invalidate で記録を消せば旧 vol 扱い（成果物のみ）
        ss.mark_done("meta", self.folder, self.vol)
        self.assertTrue(ss.stage_done("meta", self.folder, self.vol))
        ss.invalidate("meta", self.folder, self.vol)
        self.assertIsNone(ss.record("meta", self.folder))
        self.assertTrue(ss.stage_done("meta", self.folder, self.vol))

    def test_old_stage_names_map_to_new_and_premiere_is_unknown(self):
        _touch(self.folder / "7_test_vol7.mp4", b"m")
        self.assertTrue(ss.stage_done("export", self.folder, self.vol))
        self.assertTrue(ss.stage_done("render", self.folder, self.vol))
        self.assertEqual(ss.evaluate("premiere", self.folder, self.vol), "unknown")
        self.assertIsNone(ss.mark_done("premiere", self.folder, self.vol))
        self.assertFalse((self.folder / ss.STATE_FILE).exists())
        _touch(self.folder / "Image" / "サムネイル.jpg", b"j")
        self.assertTrue(ss.stage_done("psd_composite", self.folder, self.vol))
        ss.mark_done("psd_composite", self.folder, self.vol)
        self.assertEqual(set(ss.load(self.folder)), {"composite"})

    def test_composite_and_render_invalidate_on_upstream_change_but_not_own_output(self):
        png = _touch(self.folder / "vol7.png", b"p" * 10)
        jpg = _touch(self.folder / "vol7.jpg", b"j" * 10)
        _touch(self.folder / "サムネイル.jpg", b"t")
        ss.mark_done("composite", self.folder, self.vol)
        # 自分の出力（vol7.jpg）を書き直しても失効しない
        jpg.write_bytes(b"J" * 20)
        os.utime(jpg, (10_000, 10_000))
        self.assertEqual(ss.evaluate("composite", self.folder, self.vol), "done")
        # 前段 bgimage の出力（vol7.png）が変わると失効
        png.write_bytes(b"P" * 11)
        os.utime(png, (20_000, 20_000))
        self.assertEqual(ss.evaluate("composite", self.folder, self.vol), "stale")
        # render: composite + rename に依存。mp4 自身の変更では失効しない。mtime は秒単位
        _touch(self.folder / "music" / "a.mp3", b"a" * 5)
        mp4 = _touch(self.folder / "x_vol7.mp4", b"m")
        _touch(self.folder / "x_vol71.mp4", b"other")
        self.assertEqual([p.name for p in ss.stage_outputs("render", self.folder, self.vol)], ["x_vol7.mp4"])
        ss.mark_done("render", self.folder, self.vol)
        mp4.write_bytes(b"mm")
        self.assertEqual(ss.evaluate("render", self.folder, self.vol), "done")
        os.utime(jpg, ns=(10_000_000_000_500, 10_000_000_000_500))  # 同じ秒内の mtime 変化（Drive の精度落ち）
        self.assertEqual(ss.evaluate("render", self.folder, self.vol), "done")
        os.utime(jpg, (30_000, 30_000))
        self.assertEqual(ss.evaluate("render", self.folder, self.vol), "stale")

    def test_select_input_ignores_volatile_review_fields_and_audio_ignores_mtime(self):
        review = self.folder / ".music_review.json"
        state = {"processing": 123, "revision": 2, "processed_revision": 2, "scores": {"a": 1}, "ai_select_attempts": 0,
                 "takes": [{"clip_id": "c1", "selected": True}, {"clip_id": "c2", "selected": False}]}
        _touch(review, json.dumps(state).encode())
        mp3 = _touch(self.folder / "original_music" / "c1.mp3", b"x" * 8)
        _touch(self.folder / "music" / "c1.mp3", b"y" * 9)
        ss.mark_done("rename", self.folder, self.vol)  # rename は select に依存
        state["processing"] = 456; state["scores"] = {"a": 9}; state["ai_select_attempts"] = 3
        review.write_text(json.dumps(state), encoding="utf-8")
        os.utime(mp3, (1, 1))
        self.assertEqual(ss.evaluate("rename", self.folder, self.vol), "done")
        state["takes"][1]["selected"] = True
        review.write_text(json.dumps(state), encoding="utf-8")
        self.assertEqual(ss.evaluate("rename", self.folder, self.vol), "stale")

    def test_qa_requires_passed_true(self):
        _touch(self.folder / "qa_report.json", json.dumps({"passed": False}).encode())
        self.assertFalse(ss.stage_done("qa", self.folder, self.vol))
        _touch(self.folder / "qa_report.json", b"{broken")
        self.assertFalse(ss.stage_done("qa", self.folder, self.vol))

    def test_broken_state_file_is_treated_as_empty_and_save_is_atomic(self):
        (self.folder / ss.STATE_FILE).write_text("{not json", encoding="utf-8")
        self.assertEqual(ss.load(self.folder), {})
        _touch(self.folder / "youtube_title.txt", b"t")
        ss.mark_done("meta", self.folder, self.vol)
        self.assertEqual(set(ss.load(self.folder)), {"meta"})
        self.assertEqual([p.name for p in self.folder.glob(".stage_state.*.tmp")], [])

    def test_orchestrator_uses_stage_state_and_stale_beats_ledger(self):
        import app_orchestrator as o

        class LedgerAllDone:
            def list_runs(self, channel_id=None, vol=None, limit=30, **kw):
                return [{"status": "done", "meta_json": ""}]
        saved = o._ledger
        o._ledger = LedgerAllDone()
        try:
            _touch(self.folder / "music" / "a.mp3", b"a")
            _touch(self.folder / "qa_report.json", json.dumps({"passed": True}).encode())
            _touch(self.folder / "youtube_title.txt", b"t")
            self.assertTrue(o._stage_artifact_done("meta", self.folder, self.vol, channel_id="ch"))
            ss.mark_done("meta", self.folder, self.vol)
            (self.folder / "music" / "a.mp3").write_bytes(b"changed")
            os.utime(self.folder / "music" / "a.mp3", ns=(5, 5))
            # 台帳に done があっても、入力が変わった工程は失効
            self.assertFalse(o._stage_artifact_done("meta", self.folder, self.vol, channel_id="ch"))
            # 成果物が無くても台帳 done なら従来どおり補完（記録が無い場合）
            self.assertTrue(o._stage_artifact_done("thumbnail", self.folder, self.vol, channel_id="ch"))
            # 廃止工程 premiere は従来の判定（mp4 の有無）に戻す
            o._ledger = type("L", (), {"list_runs": staticmethod(lambda **kw: [])})()
            self.assertFalse(o._stage_artifact_done("premiere", self.folder, self.vol, channel_id="ch"))
            _touch(self.folder / "7_test_vol7.mp4", b"m")
            self.assertTrue(o._stage_artifact_done("premiere", self.folder, self.vol, channel_id="ch"))
        finally:
            o._ledger = saved

    def test_upload_worker_requires_qa_validator_and_ignores_upload_invalidation(self):
        import app_orchestrator as o
        saved = o._ledger
        # 台帳に full-pipeline の done run があっても、qa は validator（passed:true）必須
        o._ledger = type("L", (), {"list_runs": staticmethod(lambda **kw: [{"status": "done", "meta_json": ""}])})()
        try:
            w = o.UploadWorker()
            w._slot_available = lambda **kw: True
            self.assertFalse(w.can_run(self.vol, self.folder))
            _touch(self.folder / "qa_report.json", json.dumps({"passed": False}).encode())
            self.assertFalse(w.can_run(self.vol, self.folder))
            _touch(self.folder / "qa_report.json", json.dumps({"passed": True}).encode())
            self.assertTrue(w.can_run(self.vol, self.folder))
            # 投稿済み marker があれば候補外。メタを手で直して入力が変わっても upload は失効しない
            _touch(self.folder / "youtube_title.txt", b"t")
            _touch(self.folder / "youtube_upload.json", b"{}")
            ss.mark_done("upload", self.folder, self.vol)
            (self.folder / "youtube_title.txt").write_text("edited", encoding="utf-8")
            self.assertEqual(ss.evaluate("upload", self.folder, self.vol), "done")
            self.assertFalse(w.can_run(self.vol, self.folder))
            self.assertTrue(o._stage_artifact_done("upload", self.folder, self.vol, channel_id="ch"))
        finally:
            o._ledger = saved

    def test_stage_worker_run_records_done_and_forces_stale_regeneration(self):
        import app_orchestrator as o
        seen = {}

        def fake_composite(vol, folder, via_api, **kw):
            seen["force"] = os.environ.get("APP_IMAGE_COMPOSITE_FORCE")
            _touch(folder / "vol7.jpg", b"j")
            return True
        saved = o._pipe.STEP_FUNCS.get("psd_composite")
        o._pipe.STEP_FUNCS["psd_composite"] = fake_composite
        try:
            w = o.StepWorker(domain="psd_composite", stages=["psd_composite"], domain_label="image")
            png = _touch(self.folder / "vol7.png", b"p")
            self.assertEqual(w.run(self.vol, self.folder), o.Exit.OK)
            self.assertIsNone(seen["force"])
            self.assertIsNotNone(ss.record("composite", self.folder))  # autopilot 経路でも完了を記録
            self.assertEqual(ss.evaluate("composite", self.folder, self.vol), "done")
            png.write_bytes(b"pp"); os.utime(png, (77_777, 77_777))
            self.assertEqual(ss.evaluate("composite", self.folder, self.vol), "stale")
            self.assertEqual(w.run(self.vol, self.folder), o.Exit.OK)
            self.assertEqual(seen["force"], "1")  # 失効 → 強制再生成
            self.assertNotIn("APP_IMAGE_COMPOSITE_FORCE", os.environ)
            self.assertEqual(ss.evaluate("composite", self.folder, self.vol), "done")  # 記録が更新され再投入が止まる
        finally:
            if saved is None:
                o._pipe.STEP_FUNCS.pop("psd_composite", None)
            else:
                o._pipe.STEP_FUNCS["psd_composite"] = saved


if __name__ == "__main__":
    unittest.main()


class QAWorkerStaleTest(unittest.TestCase):
    def test_qa_worker_recandidates_when_render_changed(self):
        import app_orchestrator as o
        root = Path(tempfile.mkdtemp(prefix="qa_stale_"))
        try:
            folder = root / "5_x"; folder.mkdir()
            w = o.WORKERS["qa"]
            w._slot_available = lambda **kw: True
            mp4 = _touch(folder / "x_vol5.mp4", b"m")
            self.assertTrue(w.can_run(5, folder))
            _touch(folder / "qa_report.json", json.dumps({"passed": True}).encode())
            ss.mark_done("qa", folder, 5)
            self.assertFalse(w.can_run(5, folder))  # 検査済み
            mp4.write_bytes(b"re-rendered"); os.utime(mp4, (55_555, 55_555))
            self.assertTrue(w.can_run(5, folder))  # 再 render → 再検査
        finally:
            shutil.rmtree(root, ignore_errors=True)


class PipelineStaleForceTest(unittest.TestCase):
    def test_stale_stage_sets_force_env_for_regenerating_steps(self):
        import app_pipeline as p
        root = Path(tempfile.mkdtemp(prefix="stale_force_"))
        try:
            folder = root / "3_x"; folder.mkdir()
            self.assertIsNone(p._stale_force_env("composite", folder, 3))  # 記録なし → 失効ではない
            png = _touch(folder / "vol3.png", b"p"); _touch(folder / "vol3.jpg", b"j")
            ss.mark_done("composite", folder, 3)
            self.assertIsNone(p._stale_force_env("psd_composite", folder, 3))
            png.write_bytes(b"pp"); os.utime(png, (99_999, 99_999))
            self.assertEqual(p._stale_force_env("psd_composite", folder, 3), "APP_IMAGE_COMPOSITE_FORCE")
            self.assertEqual(p._stale_force_env("composite", folder, 3), "APP_IMAGE_COMPOSITE_FORCE")
            # 強制 env の無い工程（thumbnail）は失効しても None（再実行はされる）
            _touch(folder / "youtube_title.txt", b"t")
            ss.mark_done("thumbnail", folder, 3)
            png.write_bytes(b"ppp"); os.utime(png, (88_888, 88_888))
            self.assertIsNone(p._stale_force_env("thumbnail", folder, 3))
        finally:
            shutil.rmtree(root, ignore_errors=True)
