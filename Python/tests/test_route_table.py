"""段階 7: app.py を routers/ へ分けても、ルート表（メソッド・パス・関数名と登録順）が変わらないことを確かめる。

ルートを意図して追加・変更したときは、次で固定値を作り直して差分を確認してから commit する:
    python Python/tests/test_route_table.py --update
"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import json
import os
import sys
import unittest
from pathlib import Path

os.environ.setdefault("APP_ORCH_TICK_ENABLED", "0")
os.environ.setdefault("APP_AUTO_UPDATE_ENABLED", "0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "route_table.json"


def current_routes() -> list:
    import app

    def walk(routes):
        for r in routes:
            if type(r).__name__ == "_IncludedRouter":  # FastAPI 0.16x は include_router をまとめて 1 件にする
                yield from walk(r.original_router.routes)
                continue
            ep = getattr(r, "endpoint", None)
            yield [",".join(sorted(getattr(r, "methods", None) or [])), getattr(r, "path", ""),
                   getattr(ep, "__name__", type(r).__name__), getattr(ep, "__module__", "")]
    return list(walk(app.app.routes))


NEW_ROUTERS = ("local_tools", "meta_tools", "proposals", "drafts", "prompts", "setup_tools", "run_ledger", "publish_gate")


class RouteTableTest(unittest.TestCase):
    def test_route_table_matches_fixture(self):
        expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
        actual = current_routes()
        self.assertEqual(len(actual), len(expected))
        for i, (a, e) in enumerate(zip(actual, expected)):
            self.assertEqual(a, e, f"route #{i} differs")

    def test_new_router_get_endpoints_respond(self):
        """段階 7 で移した router の GET を実際に呼ぶ（500 にならないこと）。パス引数は存在しない値で埋める。"""
        import re
        import app
        from fastapi.testclient import TestClient
        called = 0
        with TestClient(app.app) as client:  # 起動処理（台帳 DB の初期化など）も通す
            for methods, path, name, module in current_routes():
                if module.replace("routers.", "") not in NEW_ROUTERS or "GET" not in methods.split(","):
                    continue
                url = re.sub(r"\{[^}]+\}", "no-such-item", path)
                with self.subTest(path=path):
                    r = client.get(url)
                    self.assertLess(r.status_code, 500, f"{path} -> {r.status_code} {r.text[:200]}")
                    called += 1
        self.assertGreater(called, 5)


if __name__ == "__main__":
    if "--update" in sys.argv:
        FIXTURE.write_text(json.dumps(current_routes(), ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
        print(f"updated {FIXTURE}")
    else:
        unittest.main()
