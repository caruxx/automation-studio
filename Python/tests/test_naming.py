"""naming.py: フォルダ・画像・動画・楽曲の命名規則の往復と、旧コードとの同値。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import datetime as dt
import re
import sys
import unittest
import unittest.mock
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
        self.assertEqual(naming.parse_vol_folder("12_EDM_991399"), naming.VolFolder(12, "EDM", None))  # 不正日付は date だけ None
        for bad in ("", "abc", "12_", "12_EDM_26103", "_EDM_261031"):
            self.assertIsNone(naming.parse_vol_folder(bad), msg=bad)
        with self.assertRaises(ValueError):
            naming.build_vol_folder(1, "a_b", dt.date(2026, 1, 1))
        with self.assertRaises(ValueError):
            naming.build_vol_folder(-1, "EDM", dt.date(2026, 1, 1))

    def test_publish_date_matches_old_regex(self):
        import datetime as _dt
        from publish_schedule import parse_publish_date_from_folder
        def old(name):
            m = re.match(r"^\d+_[^_]+_(\d{6})$", Path(name).name)
            if not m: return None
            try: return _dt.datetime.strptime(m.group(1), "%y%m%d").date()
            except Exception: return None
        for name in ("12_EDM_261031", "12_EDM", "12_EDM_991399", "12_E_D_261031", "012_EDM_260101", "12_EDM_261031\n", "x"):
            self.assertEqual(parse_publish_date_from_folder(Path(name)), old(name), msg=repr(name))

    def test_video_output_path_keeps_vol_spelling(self):
        import app_pipeline as p
        folder = Path("/tmp/x/05_EDM_261031")
        with unittest.mock.patch.object(p, "_load_dashboard_config", lambda: {}), \
                unittest.mock.patch.object(p, "_resolve_channel", lambda *a, **k: {}):
            self.assertEqual(p._resolve_local_output_path(5, folder).name, "EDM_vol05.mp4")
            self.assertEqual(p._resolve_local_output_path(7, Path("/tmp/x/noprefix")).name, "vol_vol7.mp4")


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
        import app_ffrender, app_timeline
        num = r"^\s*(?:track\s*)?\d{1,3}\s*[-_. )]+\s*"
        def old_ffrender(name):
            noext = re.sub(r"\.[^.]+$", "", name)
            noext = re.sub(r"^z+_", "", noext)
            return public_track_title(re.sub(num, "", noext, flags=re.I).strip() or noext)
        def old_timeline(name):
            stem = Path(str(name or "")).stem
            return public_track_title(re.sub(num, "", stem, flags=re.I).strip() or stem)
        names = ("01 - Silver Avenue(1).mp3", "z_Silver Avenue(1).mp3", "track 7. Last Light（２）.mp3",
                 "Silver Avenue (Live).mp3", "12.mp3", "zz_03_Blue.mp3", "z_.mp3", ".mp3", "AC/DC - Thunder.mp3", "")
        for name in names:
            self.assertEqual(app_ffrender._title_from(name), old_ffrender(name), msg=f"ffrender {name!r}")
            self.assertEqual(app_timeline.display_title(name), old_timeline(name), msg=f"timeline {name!r}")
        self.assertEqual(naming.title_from_filename("01 - Silver Avenue(1).mp3"), "Silver Avenue")
        self.assertEqual(app_timeline.display_title("z_Silver Avenue(1).mp3"), "z_Silver Avenue")  # timeline は z_ を保つ

    def test_music_filename_strips_take_number_and_dedups(self):
        self.assertEqual(naming.music_filename("Silver Avenue(1)"), "Silver Avenue.mp3")
        self.assertEqual(naming.music_filename("Silver Avenue （２）"), "Silver Avenue.mp3")
        self.assertEqual(naming.music_filename("Silver Avenue (Live)"), "Silver Avenue (Live).mp3")
        self.assertEqual(naming.music_filename('A/B:C*?"<>|'), "ABC.mp3")
        self.assertEqual(naming.music_filename("Silver Avenue(2)", taken=["silver avenue.mp3"]), "Silver Avenue -2.mp3")
        self.assertEqual(naming.music_filename("X", taken=["X.mp3", "X -2.mp3"]), "X -3.mp3")
        import unicodedata
        nfd = unicodedata.normalize("NFD", "ガーデン.mp3")
        self.assertEqual(naming.music_filename("ガーデン", taken=[nfd]), "ガーデン -2.mp3")  # NFD の一覧でも重複を検出
        self.assertEqual(naming.music_filename(".hidden. "), "hidden.mp3")
        self.assertEqual(naming.music_filename(". foo"), "foo.mp3")
        self.assertEqual(naming.music_filename("(1)"), "(1).mp3")  # テイク番号だけの曲名は元の表記を保つ（public_track_title と同じ）
        with self.assertRaises(ValueError):
            naming.music_filename("")


if __name__ == "__main__":
    unittest.main()
