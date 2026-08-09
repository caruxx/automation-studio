from __future__ import annotations

import pytest
from starlette.requests import Request

from app.rate_limit import client_ip


def make_request(*, client_host: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [
                (name.lower().encode("latin-1"), value.encode("latin-1"))
                for name, value in headers.items()
            ],
            "client": (client_host, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
            "query_string": b"",
        }
    )


def test_client_ip_ignores_forwarded_headers_by_default(monkeypatch):
    monkeypatch.delenv("TRUSTED_CLIENT_IP_HEADER", raising=False)
    request = make_request(
        client_host="192.0.2.10",
        headers={
            "x-forwarded-for": "198.51.100.20",
            "cf-connecting-ip": "2001:db8::20",
        },
    )

    assert client_ip(request) == "192.0.2.10"


@pytest.mark.parametrize(
    "header_value, expected",
    [
        ("198.51.100.20", "198.51.100.20"),
        ("2001:0db8:0:0::20", "2001:db8::20"),
    ],
)
def test_client_ip_reads_configured_valid_header(monkeypatch, header_value, expected):
    monkeypatch.setenv("TRUSTED_CLIENT_IP_HEADER", "cf-connecting-ip")
    request = make_request(
        client_host="192.0.2.10",
        headers={"cf-connecting-ip": header_value},
    )

    assert client_ip(request) == expected


def test_client_ip_falls_back_for_invalid_configured_header(monkeypatch):
    monkeypatch.setenv("TRUSTED_CLIENT_IP_HEADER", "cf-connecting-ip")
    request = make_request(
        client_host="192.0.2.10",
        headers={"cf-connecting-ip": "not-an-ip-address"},
    )

    assert client_ip(request) == "192.0.2.10"
