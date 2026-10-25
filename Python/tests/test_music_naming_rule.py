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
        with self.assertRaises(Exception):
            catalog.process_selected("", shutil.copyfile)


if __name__ == "__main__":
    unittest.main()
