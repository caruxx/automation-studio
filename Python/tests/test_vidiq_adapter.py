"""段階 6 Task 2: VidIQ MCP クライアント・呼び出し計画・正規化・キャッシュ・insights-refresh CLI。実ネットワークには出ない。"""
try:
    from . import _isolation  # noqa: F401
except ImportError:
    import _isolation  # noqa: F401
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import vidiq_adapter as va  # noqa: E402

SECRET = "test-vidiq-key-not-real"
KW_TOOL = {"name": "keyword_research", "inputSchema": {"type": "object", "properties": {"keyword": {"type": "string"}},
                                                         "required": ["keyword"]}}
OUT_TOOL = {"name": "outliers", "inputSchema": {"type": "object", "properties": {"query": {"type": "string"},
                                                                                  "limit": {"type": "integer"}},
                                                  "required": ["query", "limit"]}}


class FakeServer:
    """MCP の streamable HTTP を真似る transport。sse=True なら text/event-stream で返す。"""

    def __init__(self, tools=None, results=None, sse=False, status=200):
        self.tools = tools if tools is not None else [KW_TOOL, OUT_TOOL]
        self.results = results or {}
        self.sse = sse
        self.status = status
        self.seen = []

    def __call__(self, url, headers, body, timeout):
        msg = json.loads(body.decode("utf-8"))
        self.seen.append((headers, msg))
        if self.status != 200:
            return self.status, {}, b""
        if "id" not in msg:
            return 202, {}, b""
        method = msg["method"]
        if method == "initialize":
            result = {"protocolVersion": va.PROTOCOL_VERSION, "capabilities": {"tools": {}}, "serverInfo": {"name": "fake"}}
        elif method == "tools/list":
            result = {"tools": self.tools}
        elif method == "tools/call":
            name = msg["params"]["name"]
            r = self.results.get(name)
            result = r(msg["params"]["arguments"]) if callable(r) else (r or {"content": [{"type": "text", "text": f"{name} ok"}]})
        else:
            return 200, {"content-type": "application/json"}, json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601}}).encode()
        payload = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
        hdr = {"mcp-session-id": "sess-1"}
        if self.sse:
            hdr["content-type"] = "text/event-stream"
            other = {"jsonrpc": "2.0", "method": "notifications/progress", "params": {}}
            return 200, hdr, f"event: message\ndata: {json.dumps(other)}\n\nevent: message\ndata: {json.dumps(payload)}\n\n".encode()
        hdr["content-type"] = "application/json"
        return 200, hdr, json.dumps(payload).encode()


def kw_result(args):
    return {"content": [{"type": "text", "text": json.dumps({
        "keyword": args["keyword"], "search_volume": 1200, "competition": "low",
        "top_videos": [{"video_id": "dQw4w9WgXcQ", "title": "x"}, {"videoId": "abcdefghijk"}]})}]}


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="vidiq_"))
        self.env = patch.dict(os.environ, {"APP_VIDIQ_DIR": str(self.dir / "vidiq"), "VIDIQ_MCP_API_KEY": SECRET,
                                           "APP_VIDIQ_KEY_FILE": str(self.dir / "none.json")})
        self.env.start()
        self.now = datetime(2026, 10, 27, 1, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def _factory(self, server):
        return lambda url, tok: va.McpHttpClient(url, tok, transport=server)

    def test_client_handles_json_and_sse_and_keeps_session(self):
        for sse in (False, True):
            server = FakeServer(sse=sse)
            c = va.McpHttpClient("https://example.invalid/mcp", SECRET, transport=server)
            c.initialize()
            self.assertEqual([t["name"] for t in c.list_tools()], ["keyword_research", "outliers"])
            self.assertEqual(c.call_tool("keyword_research", {"keyword": "a"})["content"][0]["text"], "keyword_research ok")
            first, later = server.seen[0][0], server.seen[-1][0]
            self.assertNotIn("Mcp-Session-Id", first)
            self.assertEqual(later["Mcp-Session-Id"], "sess-1")
            self.assertEqual(later["MCP-Protocol-Version"], va.PROTOCOL_VERSION)
            self.assertEqual(later["Authorization"], f"Bearer {SECRET}")
            self.assertEqual(server.seen[1][1]["method"], "notifications/initialized")

    def test_unauthorized_and_rpc_errors(self):
        c = va.McpHttpClient("u", SECRET, transport=FakeServer(status=401))
        with self.assertRaises(va.VidiqError) as cm:
            c.initialize()
        self.assertEqual(cm.exception.reason, "unauthorized")
        self.assertNotIn(SECRET, str(cm.exception) + cm.exception.detail)
        c2 = va.McpHttpClient("u", SECRET, transport=FakeServer())
        with self.assertRaises(va.VidiqError) as cm2:
            c2.request("nope/method")
        self.assertEqual(cm2.exception.reason, "rpc_error")

    def test_plan_uses_only_listed_tools_and_satisfiable_arguments(self):
        planned, skipped = va.plan_calls([KW_TOOL, OUT_TOOL], {"vidiq_keywords": ["lofi jazz", " ", "night"]})
        self.assertEqual([p["arguments"] for p in planned], [{"keyword": "lofi jazz"}, {"keyword": "night"}])
        planned, skipped = va.plan_calls([OUT_TOOL], {"vidiq_keywords": ["a"]})
        self.assertEqual((planned, skipped), ([], [{"tool": "keyword_research", "reason": "tool_not_listed"}]))
        two_req = {"name": "keyword_research", "inputSchema": {"properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                                                                "required": ["a", "b"]}}
        self.assertEqual(va.plan_calls([two_req], {"vidiq_keywords": ["a"]})[1][0]["reason"], "schema_unsupported")
        req = {"vidiq_requests": [{"tool": "outliers", "arguments": {"query": "jazz"}},
                                  {"tool": "outliers", "arguments": {"query": "jazz", "limit": 5}, "category": "audience"},
                                  {"tool": "made_up", "arguments": {}}, "bad"]}
        planned, skipped = va.plan_calls([KW_TOOL, OUT_TOOL], req)
        self.assertEqual(planned, [{"tool": "outliers", "arguments": {"query": "jazz", "limit": 5}, "category": "audience"}])
        self.assertEqual([s["reason"] for s in skipped], ["required_arguments_missing", "tool_not_listed", "invalid_request"])
        planned, skipped = va.plan_calls([KW_TOOL], {"vidiq_keywords": list("abcde")}, max_calls=3)
        self.assertEqual((len(planned), [s["reason"] for s in skipped]), (3, ["max_calls", "max_calls"]))

    def test_normalize_builds_evidence_from_actual_result(self):
        call = {"tool": "keyword_research", "arguments": {"keyword": "lofi"}, "category": "title"}
        res = kw_result({"keyword": "lofi"})
        item = va.normalize(call, res)
        self.assertEqual(item["evidence_ids"], [va.evidence_id("keyword_research", res)])
        self.assertRegex(item["evidence_ids"][0], r"^vidiq:keyword_research:[0-9a-f]{16}$")
        self.assertEqual(item["video_ids"], ["dQw4w9WgXcQ", "abcdefghijk"])
        self.assertEqual(item["metrics"], {"keyword": "lofi", "search_volume": 1200, "competition": "low", "top_videos_count": 2})
        self.assertLessEqual(len(item["insight"]), va.INSIGHT_MAX)
        self.assertTrue(item["insight"].startswith("VidIQ keyword_research（lofi）: "))
        text = {"content": [{"type": "text", "text": "Outlier https://www.youtube.com/watch?v=AAAAAAAAAAA and https://youtu.be/BBBBBBBBBBB"}]}
        self.assertEqual(va.normalize({"tool": "outliers", "arguments": {}}, text)["video_ids"], ["AAAAAAAAAAA", "BBBBBBBBBBB"])
        self.assertIsNone(va.normalize(call, {"isError": True, "content": [{"type": "text", "text": "x"}]}))
        self.assertIsNone(va.normalize(call, {"content": []}))
        other = va.normalize(call, kw_result({"keyword": "other"}))
        self.assertNotEqual(other["evidence_ids"], item["evidence_ids"])

    def test_refresh_writes_cache_and_skips_while_fresh(self):
        server = FakeServer(results={"keyword_research": kw_result})
        cfg = {"vidiq_keywords": ["lofi", "night"]}
        data = va.refresh("ch1", cfg, client_factory=self._factory(server), now=self.now)
        self.assertTrue(data["ok"])
        self.assertEqual(data["expires_at"], "2026-10-28T01:00:00Z")
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["evidence_ids"], [i["evidence_ids"][0] for i in data["items"]])
        path = va.cache_path("ch1")
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertNotIn(SECRET, path.read_text(encoding="utf-8"))
        calls_before = len(server.seen)
        again = va.refresh("ch1", cfg, client_factory=self._factory(server), now=self.now + timedelta(hours=23))
        self.assertTrue(again["cached"])
        self.assertEqual(len(server.seen), calls_before)  # 期限内は呼ばない（クレジットを使わない）
        forced = va.refresh("ch1", cfg, client_factory=self._factory(server), now=self.now + timedelta(hours=1), force=True)
        self.assertFalse(forced["cached"])
        self.assertEqual(va.freshness(va.load_cache("ch1"), self.now + timedelta(hours=26)), (False, "expired"))

    def test_refresh_degraded_paths_keep_old_cache(self):
        self.assertEqual(va.refresh("ch2", {}, now=self.now)["degraded_reason"], "not_configured")
        with patch.dict(os.environ, {"VIDIQ_MCP_API_KEY": ""}):
            r = va.refresh("ch2", {"vidiq_keywords": ["a"]}, now=self.now)
        self.assertEqual(r["degraded_reason"], "not_configured")
        good = va.refresh("ch2", {"vidiq_keywords": ["a"]}, client_factory=self._factory(FakeServer(results={"keyword_research": kw_result})), now=self.now)
        self.assertTrue(good["ok"])
        bad = va.refresh("ch2", {"vidiq_keywords": ["a"]}, client_factory=self._factory(FakeServer(status=401)),
                         now=self.now + timedelta(hours=30))
        self.assertEqual((bad["ok"], bad["degraded_reason"], bad["error"]), (False, "fetch_failed", "unauthorized"))
        empty = va.refresh("ch2", {"vidiq_keywords": ["a"]}, client_factory=self._factory(FakeServer(tools=[OUT_TOOL])),
                           now=self.now + timedelta(hours=30))
        self.assertEqual(empty["error"], "no_usable_results")
        self.assertEqual(va.load_cache("ch2")["fetched_at"], good["fetched_at"])  # 失敗しても前回の取得を消さない

    def test_key_file_requires_owner_only_permissions(self):
        key_file = self.dir / "vidiq.json"
        key_file.write_text(json.dumps({"api_key": "from-file"}), encoding="utf-8")
        with patch.dict(os.environ, {"VIDIQ_MCP_API_KEY": "", "APP_VIDIQ_KEY_FILE": str(key_file)}):
            os.chmod(key_file, 0o644)
            with self.assertRaises(va.VidiqError):
                va.load_api_key()
            os.chmod(key_file, 0o600)
            self.assertEqual(va.load_api_key(), "from-file")

    def test_vps_payload_excludes_raw(self):
        data = va.refresh("ch3", {"vidiq_keywords": ["a"]}, client_factory=self._factory(FakeServer(results={"keyword_research": kw_result})), now=self.now)
        p = va.vps_payload(data)
        self.assertEqual(set(p), {"channel_key", "fetched_at", "ttl_hours", "items"})
        self.assertEqual(set(p["items"][0]), {"category", "insight", "video_ids", "metrics", "evidence_ids"})
        self.assertNotIn("raw", va.public_view(data))

    def _cli(self, **kw):
        import studio
        args = argparse.Namespace(channel=kw.get("channel", "ch9"), force=kw.get("force", False),
                                  max_calls=kw.get("max_calls"), dry_run=kw.get("dry_run", False))
        cfg = kw.get("cfg", {"vidiq_keywords": ["lofi"]})
        out = io.StringIO()
        with patch.object(studio.settings_service, "config_get",
                          lambda key, channel_id="": {"value": cfg.get(key.split(".", 1)[1])}), redirect_stdout(out):
            code = studio.run_insights_refresh(args)
        return code, json.loads(out.getvalue().strip().splitlines()[-1]), out.getvalue()

    def test_cli_exit_codes_vps_push_and_no_secret_in_output(self):
        import vps_ledger_sync
        server = FakeServer(results={"keyword_research": kw_result})
        real_refresh = va.refresh
        with patch.object(va, "refresh", lambda *a, **k: real_refresh(*a, **{**k, "client_factory": self._factory(server)})), \
                patch.object(vps_ledger_sync, "enabled", return_value=True), \
                patch.object(vps_ledger_sync, "push_vidiq_insights", return_value={"inserted": 1, "expires_at": "x"}) as push:
            code, obj, raw = self._cli()
        self.assertEqual(code, 0)
        self.assertTrue(obj["ok"] and obj["vps"]["pushed"])
        self.assertEqual(push.call_args.args[0]["channel_key"], "ch9")
        self.assertNotIn(SECRET, raw)
        self.assertNotIn("raw", obj)
        with patch.object(vps_ledger_sync, "enabled", return_value=True), \
                patch.object(vps_ledger_sync, "push_vidiq_insights", side_effect=vps_ledger_sync.LedgerUnavailable("down")):
            code, obj, _ = self._cli()  # キャッシュは期限内だが VPS に届かなければ失敗として返す
        self.assertEqual((code, obj["ok"], obj["vps"]["pushed"]), (1, False, False))
        code, obj, _ = self._cli(cfg={}, channel="ch10")
        self.assertEqual((code, obj["degraded_reason"]), (1, "not_configured"))
        self.assertEqual(self._cli(channel="")[0], 2)
        self.assertEqual(self._cli(max_calls=0)[0], 2)
        code, obj, _ = self._cli(dry_run=True, channel="ch11")
        self.assertEqual((code, obj["dry_run"], obj["fresh"], obj["reason"]), (0, True, False, "not_fetched"))


class AdapterHardeningTest(unittest.TestCase):
    """Task 2 レビュー対応: 転送でキーを渡さない、平文の接続先を拒否、設定の型、重複、値の洗浄。"""

    def test_redirect_is_not_followed_and_key_not_forwarded(self):
        import http.server
        import threading
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
                self.end_headers()

            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        try:
            c = va.McpHttpClient(f"http://127.0.0.1:{srv.server_port}/mcp", SECRET, timeout=5)
            with self.assertRaises(va.VidiqError) as cm:
                c.initialize()
            self.assertEqual(cm.exception.reason, "http_302")
        finally:
            srv.shutdown()
        self.assertEqual([p for p, _ in seen], ["/mcp"])  # 転送先 /stolen には行かない

    def test_insecure_endpoint_is_rejected_before_sending_key(self):
        sent = []
        with patch.dict(os.environ, {"VIDIQ_MCP_ENDPOINT": "http://vidiq.example/mcp", "VIDIQ_MCP_API_KEY": SECRET,
                                     "APP_VIDIQ_DIR": tempfile.mkdtemp(prefix="vq_")}):
            r = va.refresh("chx", {"vidiq_keywords": ["a"]},
                           client_factory=lambda u, t: sent.append(u) or va.McpHttpClient(u, t, transport=FakeServer()))
        self.assertEqual((r["ok"], r["error"], sent), (False, "insecure_endpoint", []))
        va.check_endpoint("https://mcp.vidiq.com/mcp")
        va.check_endpoint("http://127.0.0.1:8000/mcp")

    def test_protocol_version_is_negotiated(self):
        server = FakeServer()
        c = va.McpHttpClient("u", SECRET, transport=server)
        orig = server.__call__

        def older(url, headers, body, timeout):
            status, hdr, data = orig(url, headers, body, timeout)
            msg = json.loads(body.decode())
            if msg.get("method") == "initialize":
                d = json.loads(data.decode())
                d["result"]["protocolVersion"] = "2025-03-26"
                data = json.dumps(d).encode()
            return status, hdr, data
        c.transport = older
        c.initialize()
        c.list_tools()
        self.assertEqual(server.seen[-1][0]["MCP-Protocol-Version"], "2025-03-26")
        bad = va.McpHttpClient("u", SECRET, transport=lambda u, h, b, t: (200, {"content-type": "application/json"}, json.dumps(
            {"jsonrpc": "2.0", "id": 1, "result": {"protocolVersion": "1999-01-01"}}).encode()))
        with self.assertRaises(va.VidiqError) as cm:
            bad.initialize()
        self.assertEqual(cm.exception.reason, "protocol_version")

    def test_string_config_duplicates_and_types_are_not_called(self):
        self.assertEqual(va.plan_calls([KW_TOOL], {"vidiq_keywords": "lofi jazz"}), ([], [{"tool": "keyword_research", "reason": "invalid_config"}]))
        self.assertEqual(va.plan_calls([KW_TOOL], {"vidiq_requests": "x"})[1][0]["reason"], "invalid_config")
        planned, skipped = va.plan_calls([KW_TOOL], {"vidiq_keywords": ["a", "a", " a "]})
        self.assertEqual(len(planned), 1)
        req = {"vidiq_requests": [{"tool": "outliers", "arguments": {"query": "j", "limit": 5}},
                                  {"tool": "outliers", "arguments": {"limit": 5, "query": "j"}},
                                  {"tool": "outliers", "arguments": {"query": 1, "limit": 5}},
                                  {"tool": "outliers", "arguments": {"query": "j", "limit": True}}]}
        planned, skipped = va.plan_calls([OUT_TOOL], req)
        self.assertEqual(len(planned), 1)
        self.assertEqual([s["reason"] for s in skipped], ["duplicate", "required_arguments_missing", "required_arguments_missing"])

    def test_normalize_scrubs_values_and_only_takes_youtube_ids(self):
        call = {"tool": "keyword_research", "arguments": {"keyword": "a"}, "category": "title"}
        res = {"content": [{"type": "text", "text": '{"score": NaN, "big": Infinity, "name": "x\\u0000y", "ok": 1}'}]}
        item = va.normalize(call, res)
        self.assertEqual(item["metrics"], {"name": "xy", "ok": 1})
        self.assertNotIn("\x00", item["insight"])
        text = {"content": [{"type": "text", "text": "https://example.com/page?v=hello_world1 https://www.youtube.com/watch?list=x&v=CCCCCCCCCCC"}]}
        self.assertEqual(va.normalize(call, text)["video_ids"], ["CCCCCCCCCCC"])
        fake = {"content": [{"type": "text", "text": "evilyoutube.com/watch?v=AAAAAAAAAAA youtube.com/shorts/DDDDDDDDDDD"}]}
        self.assertEqual(va.normalize(call, fake)["video_ids"], ["DDDDDDDDDDD"])
        self.assertFalse(va.is_configured({"vidiq_keywords": "lofi"}))
        self.assertEqual(va.format_hint_with_evidence([{"category": "t", "insight": "a", "evidence_ids": "vidiq:x:1"}])[1], [])


if __name__ == "__main__":
    unittest.main()
