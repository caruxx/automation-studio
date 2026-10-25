"""naming.py: フォルダ・画像・動画・楽曲の命名規則の往復と、旧コードとの同値。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import datetime as dt
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import naming  # noqa: E402


class VolFolderTest(unittest.TestCase):
    def test_roundtrip(self):
        name = naming.build_vol_folder(12, "EDM", dt.date(2026, 10, 31))
        self.assertEqual(name, "12_EDM_261031")
        parsed = naming.parse_vol_folder(name)
        self.assertEqual(parsed, naming.VolFolder(12, "EDM", dt.date(2026, 10, 31)))
        self.assertEqual(naming.folder_publish_date(name), dt.date(2026, 10, 31))

    def test_legacy_without_date_and_invalid(self):
        self.assertEqual(naming.parse_vol_folder("3_QA"), naming.VolFolder(3, "QA", None))
        self.assertIsNone(naming.folder_publish_date("3_QA"))
        for bad in ("", "abc", "12_", "12_EDM_991399", "12_EDM_26103", "_EDM_261031"):
            self.assertIsNone(naming.parse_vol_folder(bad), msg=bad)
        with self.assertRaises(ValueError):
            naming.build_vol_folder(1, "a_b", dt.date(2026, 1, 1))
        with self.assertRaises(ValueError):
            naming.build_vol_folder(-1, "EDM", dt.date(2026, 1, 1))

    def test_folder_prefix_matches_old_regex(self):
        for name in ("12_EDM_261031", "3_QA", "7_orzz._260702", "noprefix", "12_"):
            m = re.match(r"^\d+_([^_]+)", name)
            self.assertEqual(naming.folder_prefix(name), m.group(1) if m else "vol")
        self.assertEqual(naming.folder_prefix("/x/y/12_EDM_261031"), "EDM")


class ArtifactNamesTest(unittest.TestCase):
    def test_image_video_workspace(self):
        self.assertEqual(naming.image_name(5), "vol5.png")
        self.assertEqual(naming.image_name(5, "jpg", sub=1), "vol5-1.jpg")
        self.assertEqual(naming.video_name("EDM", 5), "EDM_vol5.mp4")
        self.assertEqual(naming.video_name("EDM", "05"), "EDM_vol05.mp4")  # 呼び出し元の表記を保つ
        self.assertEqual(naming.suno_workspace("edm", 5), "edm_vol5")


class TitleTest(unittest.TestCase):
    def test_title_from_filename_matches_old_helpers(self):
        from app_track_title import public_track_title
        old = lambda name: public_track_title(re.sub(r"^\s*(?:track\s*)?\d{1,3}\s*[-_. )]+\s*", "",
                                                      re.sub(r"^z+_", "", Path(name).stem), flags=re.I).strip() or Path(name).stem)
        for name in ("01 - Silver Avenue(1).mp3", "z_Silver Avenue(1).mp3", "track 7. Last Light（２）.mp3",
                     "Silver Avenue (Live).mp3", "12.mp3", "zz_03_Blue.mp3"):
            self.assertEqual(naming.title_from_filename(name), old(name), msg=name)
        self.assertEqual(naming.title_from_filename("01 - Silver Avenue(1).mp3"), "Silver Avenue")

    def test_music_filename_strips_take_number_and_dedups(self):
        self.assertEqual(naming.music_filename("Silver Avenue(1)"), "Silver Avenue.mp3")
        self.assertEqual(naming.music_filename("Silver Avenue （２）"), "Silver Avenue.mp3")
        self.assertEqual(naming.music_filename("Silver Avenue (Live)"), "Silver Avenue (Live).mp3")
        self.assertEqual(naming.music_filename('A/B:C*?"<>|'), "ABC.mp3")
        self.assertEqual(naming.music_filename("Silver Avenue(2)", taken=["silver avenue.mp3"]), "Silver Avenue -2.mp3")
        self.assertEqual(naming.music_filename("X", taken=["X.mp3", "X -2.mp3"]), "X -3.mp3")
        self.assertEqual(naming.music_filename("(1)"), "(1).mp3")  # テイク番号だけの曲名は元の表記を保つ（public_track_title と同じ）
        with self.assertRaises(ValueError):
            naming.music_filename("")


if __name__ == "__main__":
    unittest.main()
