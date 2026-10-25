"""採用時のリネーム規則: テイク番号を除いた曲名で music/ に保存し、旧規則で処理済みの既存 vol も認める。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_music_catalog as catalog  # noqa: E402


class RenameRuleTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.folder = self.root / "1_review_260929"
        self.folder.mkdir()
        self.env = patch.dict(os.environ, {"APP_MUSIC_CATALOG_DB": str(self.root / "music.sqlite3")})
        self.env.start()
        self.seed = patch.object(catalog, "_seeded", True)
        self.seed.start()

    def tearDown(self):
        self.seed.stop()
        self.env.stop()
        self.temp.cleanup()

    def _register(self, titles):
        saved = []
        n = 0
        for title in titles:
            for suffix in ("", "_2"):
                name = f"{title}{suffix}.mp3"
                (self.folder / name).write_bytes(f"o{n}".encode())
                saved.append({"song_id": f"clip-{n}", "title": title, "file": name})
                n += 1
        return catalog.register_download(self.folder, saved, "review", len(saved))

    def test_take_number_is_removed_on_adoption(self):
        batch = self._register(["Silver Avenue(1)"])
        catalog.choose_take(batch, "clip-1", True)
        catalog.process_selected(self.folder, shutil.copyfile)
        self.assertEqual(sorted(p.name for p in (self.folder / "music").glob("*.mp3")), ["Silver Avenue.mp3"])
        self.assertTrue(catalog.get_review(batch)["processed"])
        # 原曲は触らない
        self.assertEqual((self.folder / "Silver Avenue(1)_2.mp3").read_bytes(), b"o1")

    def test_same_public_title_in_two_groups_gets_numbered(self):
        batch = self._register(["Blue(1)", "Blue(2)"])
        catalog.choose_take(batch, "clip-0", True)
        catalog.choose_take(batch, "clip-3", True)
        names = catalog.selected_music_names(catalog.get_review(batch)["takes"])
        self.assertEqual(sorted(names.values()), ["Blue -2.mp3", "Blue.mp3"])
        catalog.process_selected(self.folder, shutil.copyfile)
        self.assertEqual(sorted(p.name for p in (self.folder / "music").glob("*.mp3")), ["Blue -2.mp3", "Blue.mp3"])
        self.assertTrue(catalog.get_review(batch)["processed"])

    def test_legacy_processed_vol_is_still_processed(self):
        """段階 2a 以前の規則（テイク番号を残す）で処理済みの既存 vol を、未処理に戻さない。"""
        batch = self._register(["Silver Avenue(1)"])
        catalog.choose_take(batch, "clip-1", True)
        state = catalog.get_review(batch)
        (self.folder / "music").mkdir()
        (self.folder / "music" / "Silver Avenue(1).mp3").write_bytes(b"legacy")
        with catalog.transaction() as conn:
            conn.execute("UPDATE batches SET processed_revision=revision WHERE id=?", (state["id"],))
        self.assertTrue(catalog.get_review(batch)["processed"])
        self.assertIsNotNone(catalog.require_selection(self.folder, processed=True))

    def test_missing_folder_argument_is_an_error(self):
        batch = self._register(["Silver Avenue"])
        catalog.choose_take(batch, "clip-0", True)
        cwd = os.getcwd()
        os.chdir(self.folder)  # カレントが登録済み vol でも暗黙に処理しない
        try:
            for bad in ("", "   ", None):
                with self.assertRaises(ValueError):
                    catalog.process_selected(bad, shutil.copyfile)
        finally:
            os.chdir(cwd)
        self.assertFalse((self.folder / "music").exists())
        with self.assertRaises(ValueError):
            catalog.process_selected(self.root / "unregistered", shutil.copyfile)

    def test_force_rebuild_keeps_legacy_names(self):
        """旧規則で処理済みの vol を force で作り直しても、manifest が参照する旧名を保つ。"""
        batch = self._register(["Silver Avenue(1)"])
        catalog.choose_take(batch, "clip-1", True)
        state = catalog.get_review(batch)
        (self.folder / "music").mkdir()
        (self.folder / "music" / "Silver Avenue(1).mp3").write_bytes(b"legacy")
        with catalog.transaction() as conn:
            conn.execute("UPDATE batches SET processed_revision=revision WHERE id=?", (state["id"],))
        self.assertEqual(catalog.get_review(batch)["processed_naming"], "legacy")
        catalog.process_selected(self.folder, shutil.copyfile, force=True)
        self.assertEqual(sorted(p.name for p in (self.folder / "music").glob("*.mp3")), ["Silver Avenue(1).mp3"])
        self.assertTrue(catalog.get_review(batch)["processed"])

    def test_reselecting_one_group_in_legacy_vol_keeps_all_legacy_names(self):
        """旧規則の vol で 1 群だけ選び直して再処理しても、他の曲の名前は旧名のまま。"""
        batch = self._register(["Silver Avenue(1)", "Night Walk(2)"])
        catalog.choose_take(batch, "clip-1", True)
        catalog.choose_take(batch, "clip-2", True)
        state = catalog.get_review(batch)
        (self.folder / "music").mkdir()
        for name in ("Silver Avenue(1).mp3", "Night Walk(2).mp3"):
            (self.folder / "music" / name).write_bytes(b"legacy")
        with catalog.transaction() as conn:
            conn.execute("UPDATE batches SET processed_revision=revision WHERE id=?", (state["id"],))
        catalog.choose_take(batch, "clip-0", True)  # Silver Avenue を選び直す（revision が進む）
        st = catalog.get_review(batch)
        self.assertFalse(st["processed"])
        self.assertEqual(st["processed_naming"], "legacy")
        catalog.process_selected(self.folder, shutil.copyfile)
        self.assertEqual(sorted(p.name for p in (self.folder / "music").glob("*.mp3")),
                         ["Night Walk(2).mp3", "Silver Avenue(1).mp3"])
        self.assertTrue(catalog.get_review(batch)["processed"])

    def test_mixed_names_are_unprocessed_and_nfd_listing_is_processed(self):
        import unicodedata
        batch = self._register(["ガーデン(1)"])
        catalog.choose_take(batch, "clip-1", True)
        catalog.process_selected(self.folder, shutil.copyfile)
        self.assertTrue(catalog.get_review(batch)["processed"])
        # 一覧が NFD で返っても処理済み（名前を NFC で比較する）
        nfd = unicodedata.normalize("NFD", "ガーデン.mp3")
        self.assertNotEqual(nfd, "ガーデン.mp3")
        # APFS は正規化を区別しないので同じ実体、ext4（CI）では別ファイルになる。どちらでも NFD 名の実体を用意する
        (self.folder / "music" / nfd).write_bytes(b"x")
        with patch.object(catalog, "_legacy_music_name", lambda title: "x.mp3"), \
                patch.object(Path, "glob", lambda self_, pat: [self_ / nfd]):
            self.assertTrue(catalog.get_review(batch)["processed"])
        if len(list((self.folder / "music").iterdir())) > 1:
            (self.folder / "music" / nfd).unlink()  # ext4 で別ファイルになった場合だけ片付ける（APFS では同じ実体）
        # 新旧が混ざった music/ は未処理
        (self.folder / "music" / "ガーデン(1).mp3").write_bytes(b"old")
        self.assertFalse(catalog.get_review(batch)["processed"])


if __name__ == "__main__":
    unittest.main()
