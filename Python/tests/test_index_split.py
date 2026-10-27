"""段階 7: index.html の主スクリプトと主スタイルを外部ファイルへ移した状態を保つ。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parents[2] / "web" / "static"


class IndexSplitTest(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")

    def test_main_assets_are_external_and_ordered(self):
        srcs = re.findall(r'<script src="/static/js/([^"?]+)', self.html)
        self.assertEqual(srcs[0], "app-main.js")  # 主スクリプトは他の js より先（元のインラインと同じ順序）
        self.assertIn('<link rel="stylesheet" href="/static/css/app-main.css">', self.html)
        self.assertTrue((STATIC / "js" / "app-main.js").is_file())
        self.assertTrue((STATIC / "css" / "app-main.css").is_file())

    def test_no_large_inline_blocks_remain(self):
        for body in re.findall(r"<script>(.*?)</script>", self.html, re.S):
            self.assertLess(body.count("\n"), 200)
        for body in re.findall(r"<style>(.*?)</style>", self.html, re.S):
            self.assertLess(body.count("\n"), 200)

    def test_main_script_defines_entry_points(self):
        js = (STATIC / "js" / "app-main.js").read_text(encoding="utf-8")
        for name in ("function go(", "function _restoreFromHash("):
            self.assertIn(name, js)


if __name__ == "__main__":
    unittest.main()
