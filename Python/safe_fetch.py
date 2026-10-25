"""外部 URL から画像を安全に取得する（S04）。

- https のみ。ホスト名解決後の全 IP が loopback / private / link-local / metadata / reserved でないことを確認する。
- リダイレクトは各 hop で同じ検証を行い、最大 5 回まで。
- 最終応答の Content-Type は image/* に限定し、容量上限（既定 5MB）を超えたら中断する。
- 名前解決と HTTP 取得は差し替え可能（テスト用）。接続時に再解決が起きる TOCTOU は許容する（内部宛 IP は接続前の検証で落とす）。
"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from typing import Callable, Iterable, Tuple
from urllib.parse import urlsplit, urljoin

DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT = 20
DEFAULT_DEADLINE = 30  # 1 URL あたりの全体の締め切り（秒）
MAX_REDIRECTS = 5
_METADATA_NETS = (ipaddress.ip_network("169.254.0.0/16"), ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd00::/8"))


class SafeFetchError(Exception):
    """取得を拒否した、または失敗した。メッセージは利用者に見せてよい範囲に留める。"""


def _is_forbidden_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if (addr.is_loopback or addr.is_private or addr.is_link_local or addr.is_reserved
            or addr.is_multicast or addr.is_unspecified):
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        return _is_forbidden_ip(str(addr.ipv4_mapped))
    return any(addr in net for net in _METADATA_NETS)


def resolve_ips(host: str) -> list[str]:
    """ホスト名を全アドレスへ解決する（IPv4 / IPv6）。失敗は SafeFetchError。"""
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise SafeFetchError(f"名前解決に失敗しました: {host}") from exc
    ips = sorted({info[4][0] for info in infos})
    if not ips:
        raise SafeFetchError(f"名前解決の結果が空です: {host}")
    return ips


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """検証済み IP へ接続し、TLS の SNI と証明書検証、Host ヘッダは元のホスト名で行う（DNS rebinding 対策）。"""

    def __init__(self, ip: str, tls_host: str, timeout: float, context: ssl.SSLContext):
        super().__init__(ip, 443, timeout=timeout, context=context)
        self._tls_host = tls_host

    def connect(self):  # noqa: D401
        sock = socket.create_connection((self.host, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self._tls_host)
        except Exception:
            sock.close()
            raise


def open_url(url: str, timeout: float, ip: str) -> Tuple[int, dict, Callable[..., bytes], Callable[[], None]]:
    """検証済み IP へ 1 hop だけ接続して取得する。戻り値: (status, headers, read(n, timeout=None), close)。
    リダイレクトは追わない。環境変数のプロキシも使わない。"""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    context = ssl.create_default_context()
    conn = _PinnedHTTPSConnection(ip, host, timeout, context)
    try:
        conn.request("GET", path, headers={"Host": host, "User-Agent": "AutomationStudio/1.0 (+safe_fetch)", "Connection": "close"})
        resp = conn.getresponse()
    except Exception:
        conn.close()
        raise
    headers = {k.lower(): v for k, v in resp.getheaders()}

    def _socket():
        # Connection: close の応答では http.client が conn.sock を None にして応答側がソケットを持つ
        raw = getattr(getattr(resp, "fp", None), "raw", None)
        return getattr(raw, "_sock", None) or conn.sock

    def read(n: int, timeout_sec: float | None = None) -> bytes:
        sock = _socket()
        if timeout_sec is not None and sock is not None:
            sock.settimeout(max(0.05, timeout_sec))
        return resp.read1(n)  # 1 回の recv 相当で返る（締め切りを細かく確認できる）

    return resp.status, headers, read, conn.close


def validate_url(url: str, resolver: Callable[[str], Iterable[str]] = resolve_ips) -> str:
    """scheme / host / 解決後 IP を検証し、正規化した URL を返す。"""
    return validate_url_ips(url, resolver)[0]


def validate_url_ips(url: str, resolver: Callable[[str], Iterable[str]] = resolve_ips) -> Tuple[str, list[str]]:
    """validate_url と同じ検証を行い、(正規化 URL, 検証済み IP 一覧) を返す。接続はこの IP に固定する。"""
    parts = urlsplit(str(url or "").strip())
    if parts.scheme.lower() != "https":
        raise SafeFetchError("https の URL だけ取得できます")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise SafeFetchError("ホスト名がありません")
    if parts.username or parts.password:
        raise SafeFetchError("認証情報付きの URL は使えません")
    if parts.port not in (None, 443):
        raise SafeFetchError("443 以外のポートは使えません")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local") or host.endswith(".internal"):
        raise SafeFetchError("内部ホストは取得できません")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    ips = [str(literal)] if literal is not None else [str(x) for x in resolver(host)]
    if not ips:
        raise SafeFetchError("名前解決の結果が空です")
    for ip in ips:
        if _is_forbidden_ip(ip):
            raise SafeFetchError("内部・予約アドレスへの取得は拒否しました")
    return parts.geturl(), ips


def fetch_image(url: str, *, max_bytes: int = DEFAULT_MAX_BYTES, timeout: float = DEFAULT_TIMEOUT,
                max_redirects: int = MAX_REDIRECTS, deadline: float = DEFAULT_DEADLINE,
                resolver: Callable[[str], Iterable[str]] = resolve_ips,
                opener: Callable[..., Tuple[int, dict, Callable[..., bytes], Callable[[], None]]] = open_url,
                clock: Callable[[], float] = time.monotonic,
                ) -> Tuple[bytes, str]:
    """画像 URL を取得して (bytes, content_type) を返す。拒否・失敗（接続・読み込みのエラー、締め切り超過を含む）は SafeFetchError。
    接続は検証済み IP に固定する（opener(url, timeout, ip)）。"""
    current, ips = validate_url_ips(url, resolver)
    started = clock()

    def _remaining() -> float:
        left = deadline - (clock() - started)
        if left <= 0:
            raise SafeFetchError("取得が締め切りを超えました")
        return min(timeout, left)

    for _ in range(max_redirects + 1):
        try:
            status, headers, read, close = opener(current, _remaining(), ips[0])
        except SafeFetchError:
            raise
        except (OSError, http.client.HTTPException, ValueError) as exc:
            raise SafeFetchError("接続に失敗しました") from exc
        try:
            if 300 <= status < 400:
                location = headers.get("location")
                if not location:
                    raise SafeFetchError("リダイレクト先がありません")
                current, ips = validate_url_ips(urljoin(current, location), resolver)
                continue
            if status != 200:
                raise SafeFetchError(f"HTTP {status}")
            ctype = (headers.get("content-type") or "").split(";")[0].strip().lower()
            if not ctype.startswith("image/"):
                raise SafeFetchError(f"画像ではありません: {ctype or 'unknown'}")
            length = headers.get("content-length")
            if length and length.isdigit() and int(length) > max_bytes:
                raise SafeFetchError("画像が大きすぎます")
            # 64KB ずつ読み、容量と締め切りの両方で打ち切る
            chunks: list[bytes] = []
            total = 0
            try:
                while True:
                    left = _remaining()
                    chunk = read(min(65536, max_bytes + 1 - total), left)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    total += len(chunk)
                    if total > max_bytes:
                        raise SafeFetchError("画像が大きすぎます")
            except SafeFetchError:
                raise
            except (OSError, http.client.HTTPException, ValueError) as exc:
                raise SafeFetchError("読み込みに失敗しました") from exc
            data = b"".join(chunks)
            if not data:
                raise SafeFetchError("空の応答です")
            return data, ctype
        finally:
            try:
                close()
            except Exception:
                pass
    raise SafeFetchError("リダイレクトが多すぎます")
