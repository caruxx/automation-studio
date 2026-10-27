"""段階 7: index.html の主スクリプトと主スタイルを外部ファイルへ移した状態を保つ。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import re
import shutil
import subprocess
import tempfile
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


    def test_handler_check_covers_external_main_script(self):
        """check_onclick_refs.js が app-main.js 内のテンプレートの onclick も検査する（外部化で検査が外れないこと）。"""
        checker = Path(__file__).resolve().parents[1] / "scripts" / "check_onclick_refs.js"
        node = shutil.which("node")
        self.assertIsNotNone(node, "node が必要です（CI の Inline handler references と同じ）")
        with tempfile.TemporaryDirectory() as tmp:
            dst = Path(tmp) / "static"
            shutil.copytree(STATIC, dst, ignore=shutil.ignore_patterns("*.png", "*.jpg", "*.mp4"))
            ok = subprocess.run([node, str(checker), str(dst / "index.html")], capture_output=True, text=True)
            self.assertEqual(ok.returncode, 0, ok.stdout + ok.stderr)
            js = dst / "js" / "app-main.js"
            text = js.read_text(encoding="utf-8")
            planted = re.sub(r"onclick=\\?\"go\(", "onclick=\"goTypoPlanted(", text, count=1)
            if planted == text:
                planted = text.replace("onclick=\"", "onclick=\"goTypoPlanted();", 1)
            self.assertNotEqual(planted, text)
            js.write_text(planted, encoding="utf-8")
            bad = subprocess.run([node, str(checker), str(dst / "index.html")], capture_output=True, text=True)
            self.assertEqual(bad.returncode, 1, bad.stdout)
            self.assertIn("goTypoPlanted", bad.stdout)


if __name__ == "__main__":
    unittest.main()
