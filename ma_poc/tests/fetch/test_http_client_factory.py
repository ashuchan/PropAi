"""S2 tests — http client factory dispatch and adapter shape parity."""
from __future__ import annotations

import inspect
import re

import pytest

from ma_poc.fetch.contracts import FetchTier
from ma_poc.fetch.http_client import (
    _AdapterResponse,
    _CurlCffiAdapter,
    _HttpxAdapter,
    make_http_client,
)


def test_s2_client_factory_dispatch() -> None:
    pytest.importorskip("curl_cffi", reason="curl_cffi not installed — DC_PROXY tier requires it")

    direct = make_http_client(FetchTier.DIRECT, proxy=None)
    assert isinstance(direct, _HttpxAdapter)

    dc = make_http_client(FetchTier.DC_PROXY, proxy="http://proxy:8080")
    assert isinstance(dc, _CurlCffiAdapter)

    res = make_http_client(FetchTier.RESIDENTIAL, proxy="http://res:8080")
    assert isinstance(res, _CurlCffiAdapter)


def test_s2_adapter_response_shape_parity() -> None:
    """Both adapters must produce _AdapterResponse with identical fields."""
    fields = {f.name for f in _AdapterResponse.__dataclass_fields__.values()}
    expected = {"status_code", "headers", "content", "final_url", "cookies"}
    assert fields == expected, f"_AdapterResponse fields drifted: {fields} != {expected}"

    for adapter_cls in (_HttpxAdapter, _CurlCffiAdapter):
        assert inspect.iscoroutinefunction(adapter_cls.request)
        assert inspect.iscoroutinefunction(adapter_cls.aclose)


def test_s2_no_cross_tier_client_leak() -> None:
    """httpx must not appear in CurlCffiAdapter; curl_cffi must not appear in HttpxAdapter."""
    src = inspect.getsource(make_http_client)
    assert "DIRECT" in src and "_HttpxAdapter" in src
    assert "_CurlCffiAdapter" in src

    httpx_src = inspect.getsource(_HttpxAdapter)
    curl_src = inspect.getsource(_CurlCffiAdapter)
    # httpx adapter must not reference curl_cffi at all
    assert "curl_cffi" not in httpx_src
    # httpx adapter MUST use httpx
    assert "httpx.AsyncClient" in httpx_src
    # curl adapter must not IMPORT or USE httpx (comments mentioning it are OK)
    assert "import httpx" not in curl_src
    assert "httpx.AsyncClient" not in curl_src


@pytest.mark.parametrize(
    "impersonate,description",
    [
        ("chrome133", "chrome133 is within ±5 of Chrome/134,135,136"),
    ],
)
def test_s2_impersonate_version_consistent_with_identities(
    impersonate: str, description: str
) -> None:
    """H10 — impersonate version must be within ±5 majors of every Chrome identity.

    Tolerance widened from ±2 to ±5: curl_cffi adds new targets on a slower
    cadence than Chrome releases (roughly one supported target per Chrome major
    series). The ±5 window keeps the TLS fingerprint recognisably modern while
    giving room for curl_cffi >=0.7.3 (supports up to chrome133) to be used
    with identities in the Chrome 134-136 range.
    """
    from ma_poc.fetch.stealth import _IDENTITIES

    target = int(impersonate.removeprefix("chrome"))
    for ident in _IDENTITIES:
        if "Chrome/" in ident.user_agent and "Edg/" not in ident.user_agent:
            m = re.search(r"Chrome/(\d+)", ident.user_agent)
            assert m, f"could not parse Chrome version from {ident.user_agent}"
            chrome_major = int(m.group(1))
            assert abs(chrome_major - target) <= 5, (
                f"Identity Chrome/{chrome_major} too far from impersonate={impersonate}; "
                f"H10 requires ±5 majors."
            )


def test_s2_default_impersonate_version_consistent_with_identities() -> None:
    """The actual _DEFAULT_IMPERSONATE constant passes the H10 check."""
    from ma_poc.fetch.http_client import _DEFAULT_IMPERSONATE
    from ma_poc.fetch.stealth import _IDENTITIES

    target = int(_DEFAULT_IMPERSONATE.removeprefix("chrome"))
    for ident in _IDENTITIES:
        if "Chrome/" in ident.user_agent and "Edg/" not in ident.user_agent:
            m = re.search(r"Chrome/(\d+)", ident.user_agent)
            assert m
            chrome_major = int(m.group(1))
            assert abs(chrome_major - target) <= 5, (
                f"Identity Chrome/{chrome_major} is >{abs(chrome_major - target)} majors "
                f"from _DEFAULT_IMPERSONATE={_DEFAULT_IMPERSONATE}. Bump one of them."
            )
