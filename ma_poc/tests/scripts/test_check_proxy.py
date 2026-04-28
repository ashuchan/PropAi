"""Tests for check_proxy.main() exit-code logic.

Validates every branch of the ASN check without making real HTTP calls.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

# httpx is not installed in the test environment; stub it before importing
# any module that imports it at the module level.
if "httpx" not in sys.modules:
    sys.modules["httpx"] = MagicMock()

from ma_poc.scripts.check_proxy import main  # noqa: E402

_BD_URL = "http://brd-customer-hl_6785472d-zone-residential_proxy1:0owuh5392unq@brd.superproxy.io:33335"

# ── Environment guard ──────────────────────────────────────────────────────────


def test_exits_1_when_proxy_pool_urls_unset() -> None:
    with patch.dict("os.environ", {}, clear=True):
        assert main() == 1


def test_exits_1_when_proxy_pool_urls_empty_string() -> None:
    with patch.dict("os.environ", {"PROXY_POOL_URLS": "   "}, clear=True):
        assert main() == 1


# ── Network failure ────────────────────────────────────────────────────────────


def test_exits_2_when_httpx_raises() -> None:
    with patch.dict("os.environ", {"PROXY_POOL_URLS": _BD_URL}):
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = ConnectionError("proxy refused")
        with patch("ma_poc.scripts.check_proxy.httpx.Client", return_value=mock_client):
            assert main() == 2


# ── ASN checks ────────────────────────────────────────────────────────────────


def _mock_client_returning(body: dict) -> MagicMock:
    """Build a mock httpx.Client context manager that returns a JSON body."""
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json = MagicMock(return_value=body)

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.get = MagicMock(return_value=mock_resp)
    return mock_client


def test_exits_3_when_asn_is_google() -> None:
    body = {"ip": "35.186.1.1", "asn": {"asnum": 15169, "org_name": "Google LLC"}, "country": "US"}
    with patch.dict("os.environ", {"PROXY_POOL_URLS": _BD_URL}):
        with patch("ma_poc.scripts.check_proxy.httpx.Client", return_value=_mock_client_returning(body)):
            assert main() == 3


def test_exits_0_when_asn_is_not_google() -> None:
    body = {"ip": "72.14.204.99", "asn": {"asnum": 7922, "org_name": "Comcast"}, "country": "US"}
    with patch.dict("os.environ", {"PROXY_POOL_URLS": _BD_URL}):
        with patch("ma_poc.scripts.check_proxy.httpx.Client", return_value=_mock_client_returning(body)):
            assert main() == 0


# ── Malformed response shapes ──────────────────────────────────────────────────


def test_exits_4_when_ip_missing() -> None:
    body = {"asn": {"asnum": 7922}}
    with patch.dict("os.environ", {"PROXY_POOL_URLS": _BD_URL}):
        with patch("ma_poc.scripts.check_proxy.httpx.Client", return_value=_mock_client_returning(body)):
            assert main() == 4


def test_exits_4_when_asn_block_missing() -> None:
    body = {"ip": "72.14.204.99"}
    with patch.dict("os.environ", {"PROXY_POOL_URLS": _BD_URL}):
        with patch("ma_poc.scripts.check_proxy.httpx.Client", return_value=_mock_client_returning(body)):
            assert main() == 4


def test_exits_4_when_asnum_key_absent_inside_asn_block() -> None:
    body = {"ip": "72.14.204.99", "asn": {"org_name": "Comcast"}}  # no asnum key
    with patch.dict("os.environ", {"PROXY_POOL_URLS": _BD_URL}):
        with patch("ma_poc.scripts.check_proxy.httpx.Client", return_value=_mock_client_returning(body)):
            assert main() == 4


# ── PROXY_POOL_URLS comma-list: only first URL is used ────────────────────────


def test_uses_first_url_from_comma_list() -> None:
    """When PROXY_POOL_URLS contains multiple URLs, the first is used."""
    two_urls = f"{_BD_URL},http://other:secret@proxy2.example.com:33335"
    body = {"ip": "72.14.204.99", "asn": {"asnum": 7922, "org_name": "Comcast"}, "country": "US"}
    captured: list[str] = []

    def _capturing_client(proxy: str, **kwargs: object) -> MagicMock:
        captured.append(proxy)
        return _mock_client_returning(body)

    with patch.dict("os.environ", {"PROXY_POOL_URLS": two_urls}):
        with patch("ma_poc.scripts.check_proxy.httpx.Client", side_effect=_capturing_client):
            result = main()

    assert result == 0
    assert len(captured) == 1
    assert captured[0] == _BD_URL
