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

    def test_old_stage_names_map_to_new_and_premiere_is_never_done(self):
        _touch(self.folder / "7_test_vol7.mp4", b"m")
        self.assertTrue(ss.stage_done("export", self.folder, self.vol))
        self.assertTrue(ss.stage_done("render", self.folder, self.vol))
        self.assertFalse(ss.stage_done("premiere", self.folder, self.vol))
        _touch(self.folder / "Image" / "サムネイル.jpg", b"j")
        self.assertTrue(ss.stage_done("psd_composite", self.folder, self.vol))
        ss.mark_done("psd_composite", self.folder, self.vol)
        self.assertEqual(set(ss.load(self.folder)), {"composite"})

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
        finally:
            o._ledger = saved


if __name__ == "__main__":
    unittest.main()
