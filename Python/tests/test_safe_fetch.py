"""S04: 外部 URL 取得の検証（https 限定・内部アドレス拒否・リダイレクト再検証・形式と容量の上限）。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import safe_fetch  # noqa: E402

PUBLIC = ["93.184.216.34"]


def _resolver(table):
    def resolve(host):
        if host not in table:
            raise safe_fetch.SafeFetchError("unknown host")
        return table[host]
    return resolve


def _opener(responses):
    """responses: {url: (status, headers, body)} を返す差し替え用 opener。呼び出し履歴を記録する。"""
    calls = []

    def open_url(url, timeout):
        calls.append(url)
        status, headers, body = responses[url]
        buf = {"data": body}

        def read(n):
            out = buf["data"][:n]
            buf["data"] = buf["data"][n:]
            return out
        return status, {k.lower(): v for k, v in headers.items()}, read, (lambda: None)
    open_url.calls = calls
    return open_url


class ValidateUrlTest(unittest.TestCase):
    def test_rejects_non_https_and_internal_targets(self):
        res = _resolver({"img.example.com": PUBLIC, "evil.example.com": ["10.0.0.5"], "mixed.example.com": PUBLIC + ["127.0.0.1"]})
        for url in ("http://img.example.com/a.png", "https://127.0.0.1/a.png", "https://[::1]/a.png",
                    "https://169.254.169.254/latest/meta-data", "https://10.1.2.3/x", "https://192.168.1.1/x",
                    "https://evil.example.com/a.png", "https://mixed.example.com/a.png", "https://localhost/x",
                    "https://user:pw@img.example.com/x", "https://img.example.com:8443/x", "ftp://img.example.com/x", ""):
            with self.assertRaises(safe_fetch.SafeFetchError, msg=url):
                safe_fetch.validate_url(url, res)
        self.assertEqual(safe_fetch.validate_url("https://img.example.com/a.png", res), "https://img.example.com/a.png")

    def test_unresolvable_host_is_rejected(self):
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.validate_url("https://nope.example.com/a.png", _resolver({}))


class FetchImageTest(unittest.TestCase):
    def setUp(self):
        self.res = _resolver({"img.example.com": PUBLIC, "cdn.example.com": PUBLIC, "internal.example.com": ["10.9.9.9"]})

    def test_success_returns_bytes_and_content_type(self):
        op = _opener({"https://img.example.com/a.png": (200, {"Content-Type": "image/png"}, b"\x89PNG" + b"\x00" * 10)})
        data, ctype = safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=op)
        self.assertTrue(data.startswith(b"\x89PNG"))
        self.assertEqual(ctype, "image/png")

    def test_redirect_each_hop_is_validated(self):
        op = _opener({
            "https://img.example.com/a.png": (302, {"Location": "https://cdn.example.com/b.png"}, b""),
            "https://cdn.example.com/b.png": (200, {"Content-Type": "image/jpeg"}, b"\xff\xd8\xff" + b"\x00" * 5),
        })
        data, ctype = safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=op)
        self.assertEqual(ctype, "image/jpeg")
        self.assertEqual(op.calls, ["https://img.example.com/a.png", "https://cdn.example.com/b.png"])
        bad = _opener({"https://img.example.com/a.png": (302, {"Location": "https://internal.example.com/b.png"}, b"")})
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=bad)
        self.assertEqual(bad.calls, ["https://img.example.com/a.png"])
        bad2 = _opener({"https://img.example.com/a.png": (302, {"Location": "http://img.example.com/b.png"}, b"")})
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=bad2)

    def test_redirect_loop_is_bounded(self):
        op = _opener({"https://img.example.com/a.png": (302, {"Location": "https://img.example.com/a.png"}, b"")})
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=op, max_redirects=3)
        self.assertEqual(len(op.calls), 4)

    def test_rejects_non_image_and_oversize(self):
        op = _opener({"https://img.example.com/a.png": (200, {"Content-Type": "text/html"}, b"<html>")})
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=op)
        op = _opener({"https://img.example.com/a.png": (200, {"Content-Type": "image/png", "Content-Length": "999999"}, b"x" * 10)})
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=op, max_bytes=100)
        op = _opener({"https://img.example.com/a.png": (200, {"Content-Type": "image/png"}, b"x" * 200)})
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=op, max_bytes=100)
        op = _opener({"https://img.example.com/a.png": (500, {}, b"")})
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.fetch_image("https://img.example.com/a.png", resolver=self.res, opener=op)

    def test_internal_target_is_never_opened(self):
        op = _opener({})
        with self.assertRaises(safe_fetch.SafeFetchError):
            safe_fetch.fetch_image("https://internal.example.com/a.png", resolver=self.res, opener=op)
        self.assertEqual(op.calls, [])


if __name__ == "__main__":
    unittest.main()
