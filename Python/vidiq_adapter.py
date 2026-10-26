"""VidIQ MCP の取り込み（段階 6、仕様 §3.7）。

- 遠隔 MCP サーバ（既定 https://mcp.vidiq.com/mcp）に JSON-RPC 2.0 で話す最小のクライアント。
  応答は application/json と text/event-stream の両方を読み、Mcp-Session-Id を引き継ぐ。
- ツール名と引数を推測で固定しない: tools/list にあり inputSchema.required を満たせる呼び出しだけ行う。
- 根拠 id は受け取ったツール結果の本文から作る（vidiq:<tool>:<sha256 先頭 16 桁>）。動画 ID は結果に現れたものだけ。
- 取得は明示実行（studio.py insights-refresh）のときだけ。期限（既定 24 時間）内は再取得しない。
- API キーは環境変数 VIDIQ_MCP_API_KEY か repo 外の設定ファイルから読み、ログ・出力・例外文に出さない。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional

DEFAULT_ENDPOINT = "https://mcp.vidiq.com/mcp"
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26")
TTL_HOURS = 24
DEFAULT_MAX_CALLS = 3
INSIGHT_MAX = 300
MAX_ITEMS = 30
SOURCE = "vidiq"
DEGRADED_REASONS = ("not_configured", "not_fetched", "expired", "fetch_failed", "unavailable")

_VIDEO_ID_RE = re.compile(
    r"(?:^|(?<=[/\s\"'(<]))(?:(?:www\.|m\.|music\.)?youtube\.com/(?:watch\?(?:[^\s\"'<>]*&)?v=|shorts/|embed/|live/)|youtu\.be/)"
    r"([A-Za-z0-9_-]{11})(?![A-Za-z0-9_-])")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ID_ONLY_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TOOL_SLUG_RE = re.compile(r"[^A-Za-z0-9_.-]")
_CATEGORY_BY_TOOL = {"keyword_research": "title", "outliers": "concept", "trending_videos": "concept",
                     "subscriber_insights": "audience", "generate_titles": "title"}
_CATEGORIES = ("title", "thumbnail", "concept", "description", "music", "posting", "audience", "other")

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, dict[str, str], bytes]]


class VidiqError(Exception):
    """取得の失敗。reason は短い分類（unauthorized / http_<code> / protocol / network / tool_error）。"""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail[:200]


# ─── 設定と API キー ───

def config_dir() -> Path:
    return Path(os.environ.get("APP_VIDIQ_DIR") or "~/.config/automation-studio/vidiq").expanduser()


def load_api_key() -> Optional[str]:
    """環境変数 VIDIQ_MCP_API_KEY、無ければ <config_dir>/../vidiq.json の api_key（権限 0600 のときだけ）。"""
    env = (os.environ.get("VIDIQ_MCP_API_KEY") or "").strip()
    if env:
        return env
    path = Path(os.environ.get("APP_VIDIQ_KEY_FILE") or (config_dir().parent / "vidiq.json")).expanduser()
    try:
        mode = path.stat().st_mode
    except OSError:
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise VidiqError("key_file_permissions", "vidiq.json は所有者だけが読める権限（chmod 600）にしてください")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    key = str(data.get("api_key") or "").strip() if isinstance(data, dict) else ""
    return key or None


def endpoint() -> str:
    return (os.environ.get("VIDIQ_MCP_ENDPOINT") or DEFAULT_ENDPOINT).strip()


def check_endpoint(url: str) -> None:
    """API キーを平文で送らない: https:// だけを許す（localhost でも http は不可。テストは transport を差し替える）。"""
    from urllib.parse import urlparse
    u = urlparse(url)
    if u.scheme == "https" and u.hostname:
        return
    raise VidiqError("insecure_endpoint", "VIDIQ_MCP_ENDPOINT は https:// にしてください")


# ─── MCP クライアント ───

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """転送を追わない（追うと Authorization ヘッダが転送先へそのまま渡る）。3xx は呼び出し側で失敗にする。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _urllib_transport(url: str, headers: dict[str, str], body: bytes, timeout: float) -> tuple[int, dict[str, str], bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""
    except Exception as exc:  # 接続不能・タイムアウト
        raise VidiqError("network", type(exc).__name__) from None


def _parse_sse(body: bytes, want_id: int) -> Optional[dict]:
    data_lines: list[str] = []
    found = None
    for raw in body.decode("utf-8", "replace").splitlines() + [""]:
        if raw.startswith("data:"):
            data_lines.append(raw[5:].lstrip())
        elif raw == "":
            if data_lines:
                try:
                    msg = json.loads("\n".join(data_lines))
                except ValueError:
                    msg = None
                if isinstance(msg, dict) and msg.get("id") == want_id:
                    found = msg
                data_lines = []
    return found


@dataclass
class McpHttpClient:
    url: str
    token: str
    timeout: float = 30.0
    transport: Optional[Transport] = None
    session_id: Optional[str] = None
    protocol_version: str = PROTOCOL_VERSION
    _next_id: int = 1
    _initialized: bool = False

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
             "Authorization": f"Bearer {self.token}", "User-Agent": "automation-studio-vidiq/1"}
        if self._initialized:
            h["MCP-Protocol-Version"] = self.protocol_version
        if self.session_id:
            h["Mcp-Session-Id"] = self.session_id
        return h

    def _post(self, message: dict) -> tuple[int, dict[str, str], bytes]:
        send = self.transport or _urllib_transport
        status, headers, body = send(self.url, self._headers(), json.dumps(message).encode("utf-8"), self.timeout)
        sid = headers.get("mcp-session-id")
        if sid:
            self.session_id = sid
        if status in (401, 403):
            raise VidiqError("unauthorized", f"HTTP {status}")
        if 300 <= status < 400:
            raise VidiqError(f"http_{status}", "redirect is not followed")
        if status >= 400:
            raise VidiqError(f"http_{status}")
        return status, headers, body

    def request(self, method: str, params: Optional[dict] = None) -> dict:
        rid = self._next_id
        self._next_id += 1
        _, headers, body = self._post({"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}})
        ctype = headers.get("content-type", "")
        if "text/event-stream" in ctype:
            msg = _parse_sse(body, rid)
        else:
            try:
                msg = json.loads(body.decode("utf-8")) if body else None
            except ValueError:
                msg = None
        if not isinstance(msg, dict):
            raise VidiqError("protocol", f"{method}: no response")
        if msg.get("error"):
            err = msg["error"] if isinstance(msg["error"], dict) else {}
            raise VidiqError("rpc_error", f"{method}: {err.get('code')}")
        result = msg.get("result")
        if not isinstance(result, dict):
            raise VidiqError("protocol", f"{method}: result is not an object")
        return result

    def initialize(self) -> dict:
        res = self.request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                          "clientInfo": {"name": "automation-studio", "version": "1"}})
        negotiated = str(res.get("protocolVersion") or PROTOCOL_VERSION)
        if negotiated not in SUPPORTED_PROTOCOL_VERSIONS:
            raise VidiqError("protocol_version", negotiated[:32])
        self.protocol_version = negotiated
        self._initialized = True
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return res

    def list_tools(self) -> list[dict]:
        tools: list[dict] = []
        cursor = None
        for _ in range(10):  # ページ送りの上限
            res = self.request("tools/list", {"cursor": cursor} if cursor else {})
            tools.extend(t for t in (res.get("tools") or []) if isinstance(t, dict) and t.get("name"))
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self.request("tools/call", {"name": name, "arguments": arguments})


# ─── 呼び出しの計画 ───

_JSON_TYPES = {"string": (str,), "integer": (int,), "number": (int, float), "boolean": (bool,),
               "array": (list,), "object": (dict,)}


def _required_ok(tool: dict, arguments: dict) -> bool:
    """必須引数がそろい、スキーマが型を宣言している引数は型も合う。"""
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if not all(k in arguments for k in required):
        return False
    for k, v in arguments.items():
        typ = props.get(k, {}).get("type") if isinstance(props.get(k), dict) else None
        allowed = _JSON_TYPES.get(typ) if isinstance(typ, str) else None
        if allowed is None:
            continue
        if isinstance(v, bool) and typ in ("integer", "number"):
            return False
        if not isinstance(v, allowed):
            return False
    return True


def _single_required_string(tool: dict) -> Optional[str]:
    schema = tool.get("inputSchema") if isinstance(tool.get("inputSchema"), dict) else {}
    required = schema.get("required") if isinstance(schema.get("required"), list) else []
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    if len(required) != 1:
        return None
    prop = props.get(required[0])
    if isinstance(prop, dict) and prop.get("type") == "string":
        return str(required[0])
    return None


def plan_calls(tools: list[dict], channel_cfg: dict, max_calls: int = DEFAULT_MAX_CALLS) -> tuple[list[dict], list[dict]]:
    """(呼ぶもの [{tool, arguments, category}], 飛ばしたもの [{tool, reason}])。"""
    by_name = {str(t.get("name")): t for t in tools}
    planned: list[dict] = []
    skipped: list[dict] = []
    requests = channel_cfg.get("vidiq_requests")
    keywords_raw = channel_cfg.get("vidiq_keywords")
    if requests not in (None, "", []) and not isinstance(requests, list):
        return [], [{"tool": None, "reason": "invalid_config"}]  # 文字列などは呼ばない（1 文字ずつ有料で呼ぶ事故を防ぐ）
    if not requests and keywords_raw not in (None, "", []) and not isinstance(keywords_raw, list):
        return [], [{"tool": "keyword_research", "reason": "invalid_config"}]
    seen: set[str] = set()
    if isinstance(requests, list) and requests:
        for req in requests:
            if not isinstance(req, dict):
                skipped.append({"tool": None, "reason": "invalid_request"})
                continue
            name = str(req.get("tool") or "")
            args = req.get("arguments") if isinstance(req.get("arguments"), dict) else None
            if name not in by_name:
                skipped.append({"tool": name, "reason": "tool_not_listed"})
            elif args is None or not _required_ok(by_name[name], args):
                skipped.append({"tool": name, "reason": "required_arguments_missing"})
            else:
                sig = json.dumps([name, args], sort_keys=True, ensure_ascii=False)
                if sig in seen:
                    skipped.append({"tool": name, "reason": "duplicate"})
                    continue
                seen.add(sig)
                cat = req.get("category") if req.get("category") in _CATEGORIES else _CATEGORY_BY_TOOL.get(name, "other")
                planned.append({"tool": name, "arguments": dict(args), "category": cat})
    else:
        keywords = list(dict.fromkeys(k.strip() for k in (keywords_raw or []) if isinstance(k, str) and k.strip()))
        if keywords:
            tool = by_name.get("keyword_research")
            field = _single_required_string(tool) if tool else None
            if tool is None:
                skipped.append({"tool": "keyword_research", "reason": "tool_not_listed"})
            elif field is None:
                skipped.append({"tool": "keyword_research", "reason": "schema_unsupported"})
            else:
                planned.extend({"tool": "keyword_research", "arguments": {field: kw}, "category": "title"} for kw in keywords)
    if len(planned) > max_calls:
        skipped.extend({"tool": p["tool"], "reason": "max_calls"} for p in planned[max_calls:])
        planned = planned[:max_calls]
    return planned, skipped


# ─── 正規化 ───

def _content_text(result: dict) -> str:
    parts = []
    for c in result.get("content") or []:
        if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str):
            parts.append(c["text"])
    return "\n".join(parts)


def _structured(result: dict, text: str) -> Any:
    sc = result.get("structuredContent")
    if sc is not None:
        return sc
    try:
        return json.loads(text) if text.strip()[:1] in ("{", "[") else None
    except ValueError:
        return None


def _has_content(obj: Any) -> bool:
    """空の dict / list / 空文字 / None だけで構成されていないか。"""
    if isinstance(obj, dict):
        return any(_has_content(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_content(v) for v in obj)
    if isinstance(obj, str):
        return bool(obj.strip())
    return obj is not None


def _walk_video_ids(obj: Any, out: list[str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in ("video_id", "videoId") and isinstance(v, str) and _ID_ONLY_RE.match(v):
                out.append(v)
            else:
                _walk_video_ids(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _walk_video_ids(v, out)


def evidence_id(tool: str, result: dict) -> str:
    canonical = json.dumps({"content": result.get("content"), "structuredContent": result.get("structuredContent")},
                           sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    slug = (_TOOL_SLUG_RE.sub("_", tool) or "tool")[:64]
    return f"{SOURCE}:{slug}:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()[:16]}"


def normalize(call: dict, result: dict) -> Optional[dict]:
    """1 回のツール結果を insights の 1 件にする。本文が空・エラーなら None（根拠の無い学びは作らない）。"""
    if result.get("isError"):
        return None
    text = _content_text(result)
    data = _structured(result, text)
    if not text.strip() and data is None:
        return None
    if data is not None and not _has_content(data):
        return None  # {} や [] など中身の無い結果は分析として採用しない（根拠も作らない）
    ids: list[str] = list(dict.fromkeys(_VIDEO_ID_RE.findall(text)))
    walked: list[str] = []
    _walk_video_ids(data, walked)
    for v in walked:
        if v not in ids:
            ids.append(v)
    metrics: dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if len(metrics) >= 20:
                break
            if isinstance(v, float) and not math.isfinite(v):
                continue  # NaN / Infinity は JSON 列に保存できない
            if isinstance(v, (int, float, bool)) or (isinstance(v, str) and len(v) <= 200):
                metrics[_CTRL_RE.sub("", str(k))[:64]] = _CTRL_RE.sub("", v) if isinstance(v, str) else v
            elif isinstance(v, list):
                metrics[f"{str(k)[:58]}_count"] = len(v)
    elif isinstance(data, list):
        metrics["result_count"] = len(data)
    label = ", ".join(f"{v}" for v in call.get("arguments", {}).values() if isinstance(v, (str, int, float)))[:60]
    head = f"VidIQ {call['tool']}" + (f"（{label}）" if label else "") + ": "
    body_src = text if text.strip() and data is None else json.dumps(data, ensure_ascii=False)
    body = " ".join(_CTRL_RE.sub(" ", body_src).split())
    insight = (_CTRL_RE.sub("", head) + body)[:INSIGHT_MAX]
    return {"category": call.get("category") or "other", "insight": insight, "video_ids": ids[:50],
            "metrics": metrics, "evidence_ids": [evidence_id(call["tool"], result)]}


# ─── キャッシュ ───

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: Any) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def cache_path(channel_key: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", channel_key)[:64] or "default"
    return config_dir() / f"{safe}.json"


def load_cache(channel_key: str) -> dict:
    try:
        data = json.loads(cache_path(channel_key).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(channel_key: str, data: dict) -> None:
    path = cache_path(channel_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".vidiq-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def freshness(cache: dict, now: Optional[datetime] = None) -> tuple[bool, Optional[str]]:
    """(期限内か, 期限外の理由 not_fetched / expired)。"""
    now = now or _now()
    exp = _parse(cache.get("expires_at")) if cache else None
    if not cache or not cache.get("items") or exp is None:
        return False, "not_fetched"
    if exp <= now:
        return False, "expired"
    return True, None


HINT_MAX_ITEMS = 20
HINT_MAX_CHARS = 2000


def format_hint_with_evidence(items: list[dict]) -> tuple[str, list[str]]:
    """VPS の insights_hint と同じ書式（カテゴリ見出し + 箇条書き）の本文と、本文に全文が入った学びの根拠 id。"""
    lines = ["以下の学びはコピーではなく抽象パターンとして応用する。"]
    used: list[str] = []
    prev = None
    for it in (items if isinstance(items, list) else [])[:HINT_MAX_ITEMS]:
        if not isinstance(it, dict):
            continue
        trial = list(lines)
        if it.get("category") != prev:
            trial.append(f"\n## {it.get('category')}")
        trial.append(f"- {str(it.get('insight') or '').strip()}")
        if len("\n".join(trial).strip()) > HINT_MAX_CHARS:
            break  # 全文が入らない学びは渡さない（根拠にも数えない）
        lines = trial
        prev = it.get("category")
        ev = it.get("evidence_ids")
        for e in ev if isinstance(ev, list) else []:
            if isinstance(e, str) and e not in used:
                used.append(e)
    return "\n".join(lines).strip(), used


def format_hint(items: list[dict]) -> str:
    return format_hint_with_evidence(items)[0]


def is_configured(channel_cfg: dict) -> bool:
    """VidIQ を呼ぶ設定があるか（list の設定だけを数える。文字列などは未設定と同じ扱い）。"""
    req, kw = channel_cfg.get("vidiq_requests"), channel_cfg.get("vidiq_keywords")
    return bool((isinstance(req, list) and req) or (isinstance(kw, list) and kw))


def public_view(data: dict) -> dict:
    """CLI 出力用（原文 raw を除く）。"""
    return {k: v for k, v in data.items() if k != "raw"}


# ─── 取得 ───

def refresh(channel_key: str, channel_cfg: dict, *, client_factory: Optional[Callable[[str, str], Any]] = None,
            now: Optional[datetime] = None, max_calls: int = DEFAULT_MAX_CALLS, force: bool = False) -> dict:
    """VidIQ から取り直してローカルキャッシュに保存する。戻り値は CLI と VPS 送信に使う辞書（raw を含む）。
    失敗しても例外を外に出さず、ok=False と degraded_reason を返す（既存のキャッシュは消さない）。"""
    now = now or _now()
    cache = load_cache(channel_key)
    fresh, _ = freshness(cache, now)
    if fresh and not force:
        return {**cache, "ok": True, "cached": True}
    if not is_configured(channel_cfg):
        return {"ok": False, "source": SOURCE, "channel_key": channel_key, "degraded_reason": "not_configured",
                "error": "vidiq_keywords / vidiq_requests が未設定です"}
    try:
        token = load_api_key()
    except VidiqError as exc:
        return {"ok": False, "source": SOURCE, "channel_key": channel_key, "degraded_reason": "not_configured",
                "error": exc.detail or exc.reason}
    if not token:
        return {"ok": False, "source": SOURCE, "channel_key": channel_key, "degraded_reason": "not_configured",
                "error": "VidIQ の API キーがありません（VIDIQ_MCP_API_KEY か vidiq.json）"}
    factory = client_factory or (lambda url, tok: McpHttpClient(url, tok))
    calls: list[dict] = []
    items: list[dict] = []
    raw: list[dict] = []
    try:
        check_endpoint(endpoint())
        client = factory(endpoint(), token)
        client.initialize()
        tools = client.list_tools()
        planned, skipped = plan_calls(tools, channel_cfg, max_calls)
        for s in skipped:
            calls.append({**s, "ok": False})
        for call in planned:
            try:
                result = client.call_tool(call["tool"], call["arguments"])
            except VidiqError as exc:
                calls.append({"tool": call["tool"], "arguments": call["arguments"], "ok": False, "reason": exc.reason})
                if exc.reason == "unauthorized":
                    break
                continue
            item = normalize(call, result)
            if item is None:
                calls.append({"tool": call["tool"], "arguments": call["arguments"], "ok": False,
                              "reason": "tool_error" if result.get("isError") else "empty_result"})
                continue
            calls.append({"tool": call["tool"], "arguments": call["arguments"], "ok": True,
                          "evidence_id": item["evidence_ids"][0]})
            items.append(item)
            raw.append({"evidence_id": item["evidence_ids"][0], "tool": call["tool"],
                        "content": result.get("content"), "structuredContent": result.get("structuredContent")})
    except VidiqError as exc:
        return {"ok": False, "source": SOURCE, "channel_key": channel_key, "degraded_reason": "fetch_failed",
                "error": exc.reason, "calls": calls}
    if not items:
        return {"ok": False, "source": SOURCE, "channel_key": channel_key, "degraded_reason": "fetch_failed",
                "error": "no_usable_results", "calls": calls}
    items = items[:MAX_ITEMS]
    video_ids: list[str] = []
    evidence: list[str] = []
    for it in items:
        video_ids.extend(v for v in it["video_ids"] if v not in video_ids)
        evidence.extend(e for e in it["evidence_ids"] if e not in evidence)
    data = {"source": SOURCE, "channel_key": channel_key, "fetched_at": _iso(now),
            "expires_at": _iso(now + timedelta(hours=TTL_HOURS)), "ttl_hours": TTL_HOURS,
            "items": items, "video_ids": video_ids[:200], "evidence_ids": evidence, "metrics": {"calls": len(calls)},
            "calls": calls, "raw": raw}
    _write_cache(channel_key, data)
    return {**data, "ok": True, "cached": False}


def vps_payload(data: dict) -> dict:
    """VPS の取り込み API に送る形（原文は送らない）。"""
    return {"channel_key": data["channel_key"], "fetched_at": data["fetched_at"], "ttl_hours": int(data.get("ttl_hours") or TTL_HOURS),
            "items": [{k: it[k] for k in ("category", "insight", "video_ids", "metrics", "evidence_ids")} for it in data["items"]]}
