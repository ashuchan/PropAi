"""Unit tests for ma_poc.fetch.probe.stealth_probe.

Pins three invariants:

  1. Every request carries the stealth identity headers
     (``User-Agent``, ``Accept-Language``, ``Sec-Fetch-*``, optional
     ``Sec-Ch-Ua*``).
  2. The same ``property_id`` sticky-key always selects the same
     Chrome identity — so the entry-page fetch and every adapter-side
     probe present a coherent "single user session" to the bot-
     management edge.
  3. A captcha-shaped response body triggers ``captcha_provider`` —
     the canonical signal that downstream code must NOT parse the
     body as the resource it was reaching for.

Together these prevent the failure mode this helper exists to fix:
"hop bypasses stealth → bot wall → interstitial HTML → downstream
parser sees garbage → silent extraction failure".
"""

from __future__ import annotations

from typing import Any

import pytest


class _FakeResponse:
    def __init__(self, *, status: int, content: bytes) -> None:
        self.status_code = status
        self.content = content


class _CapturingAsyncClient:
    """Records every request issued; returns a queued response."""

    def __init__(self, responses: dict[tuple[str, str], _FakeResponse], *, raise_on: tuple[str, str] | None = None) -> None:
        self._responses = responses
        self.requests: list[dict[str, Any]] = []
        self._raise_on = raise_on

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: Any):
        pass

    async def request(self, method: str, url: str, data: Any = None) -> _FakeResponse:
        self.requests.append({"method": method, "url": url, "data": data, "headers": self._headers})
        if self._raise_on and (method, url) == self._raise_on:
            raise OSError("network down")
        key = (method.upper(), url)
        return self._responses.get(key, _FakeResponse(status=404, content=b""))

    @classmethod
    def make_factory(cls, responses: dict[tuple[str, str], _FakeResponse], *, raise_on: tuple[str, str] | None = None):
        instance: dict[str, _CapturingAsyncClient] = {}

        def _factory(**kwargs: Any) -> _CapturingAsyncClient:
            client = cls(responses, raise_on=raise_on)
            client._headers = kwargs.get("headers", {})  # type: ignore[attr-defined]
            instance["last"] = client
            return client

        return _factory, instance


@pytest.fixture
def _httpx_module(monkeypatch: pytest.MonkeyPatch):
    """Inject a fake httpx module into sys.modules.

    Returns a (factory, instance_dict) pair so tests can assert on the
    last client constructed (headers, request list).
    """
    factory, instance = _CapturingAsyncClient.make_factory({})

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)
    return factory, instance


# ─────────────────────────────────────────────────────────────────────
# Invariant #1 — stealth headers are applied
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stealth_probe_applies_chrome_header_set(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.fetch import probe

    responses = {("GET", "https://x.test/api"): _FakeResponse(status=200, content=b"{}")}
    factory, instance = _CapturingAsyncClient.make_factory(responses)

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)

    body, status, captcha = await probe.stealth_probe(
        "https://x.test/api",
        method="GET",
        property_id="P-001",
    )
    assert body == b"{}"
    assert status == 200
    assert captcha is None

    client = instance["last"]
    headers = client._headers  # type: ignore[attr-defined]

    # Stealth invariants: every header in chrome_header_set is present.
    assert "User-Agent" in headers
    assert "Accept-Language" in headers
    assert "Accept-Encoding" in headers and "zstd" in headers["Accept-Encoding"]
    assert headers["Sec-Fetch-Mode"] == "navigate"
    assert headers["Sec-Fetch-Dest"] == "document"
    assert headers["Sec-Fetch-User"] == "?1"
    assert headers["Upgrade-Insecure-Requests"] == "1"
    # Cold-visit Referer signal.
    assert headers.get("Referer") == "https://www.google.com/"


@pytest.mark.asyncio
async def test_stealth_probe_merges_extra_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Caller-supplied headers (auth keys, X-Requested-With) must override stealth defaults."""
    from ma_poc.fetch import probe

    responses = {("GET", "https://x.test/api"): _FakeResponse(status=200, content=b"{}")}
    factory, instance = _CapturingAsyncClient.make_factory(responses)

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)

    await probe.stealth_probe(
        "https://x.test/api",
        method="GET",
        property_id="P-001",
        extra_headers={
            "x-ws-authkey": "secret-key",
            "Referer": "https://x.test/source",  # override default
        },
    )
    headers = instance["last"]._headers  # type: ignore[attr-defined]
    # Custom auth header lands.
    assert headers["x-ws-authkey"] == "secret-key"
    # Caller wins on collision.
    assert headers["Referer"] == "https://x.test/source"
    # Stealth UA still present.
    assert "User-Agent" in headers


# ─────────────────────────────────────────────────────────────────────
# Invariant #2 — sticky-key identity selection
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stealth_probe_sticky_identity_by_property_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same property_id must yield the same User-Agent every time."""
    from ma_poc.fetch import probe

    responses = {("GET", "https://x.test/api"): _FakeResponse(status=200, content=b"{}")}

    user_agents: list[str] = []
    for _ in range(3):
        factory, instance = _CapturingAsyncClient.make_factory(responses)

        class _Module:
            AsyncClient = staticmethod(factory)

        import sys
        monkeypatch.setitem(sys.modules, "httpx", _Module)

        await probe.stealth_probe("https://x.test/api", property_id="P-STICKY")
        user_agents.append(instance["last"]._headers["User-Agent"])  # type: ignore[attr-defined]

    # All three calls picked the same identity.
    assert len(set(user_agents)) == 1, f"expected sticky UA, got {user_agents}"


@pytest.mark.asyncio
async def test_stealth_probe_different_property_ids_likely_different_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    """Different property_ids should distribute across the identity pool.

    Not strictly required for any single pair (the pool is 8 wide,
    hash collisions exist), but across a few dozen ids we expect
    multiple distinct identities — confirms sticky-key actually
    varies with input.
    """
    from ma_poc.fetch import probe

    responses = {("GET", "https://x.test/api"): _FakeResponse(status=200, content=b"{}")}

    user_agents: set[str] = set()
    for i in range(20):
        factory, instance = _CapturingAsyncClient.make_factory(responses)

        class _Module:
            AsyncClient = staticmethod(factory)

        import sys
        monkeypatch.setitem(sys.modules, "httpx", _Module)

        await probe.stealth_probe("https://x.test/api", property_id=f"P-{i}")
        user_agents.add(instance["last"]._headers["User-Agent"])  # type: ignore[attr-defined]

    # We expect at least a handful of distinct identities across 20 ids.
    assert len(user_agents) >= 3


# ─────────────────────────────────────────────────────────────────────
# Invariant #3 — captcha detection
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stealth_probe_detects_cloudflare_challenge(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.fetch import probe

    cf_body = b"<html><body>Just a moment... challenge-platform __cf_chl_</body></html>"
    responses = {("GET", "https://x.test/api"): _FakeResponse(status=200, content=cf_body)}
    factory, _ = _CapturingAsyncClient.make_factory(responses)

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)

    body, status, captcha = await probe.stealth_probe(
        "https://x.test/api", property_id="P-001"
    )
    assert status == 200
    assert body == cf_body  # body still returned; caller decides what to do
    assert captcha == "cloudflare"


@pytest.mark.asyncio
async def test_stealth_probe_detects_recaptcha(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.fetch import probe

    rc_body = b"<html><div class='g-recaptcha' data-sitekey='abc'></div></html>"
    responses = {("GET", "https://x.test/api"): _FakeResponse(status=200, content=rc_body)}
    factory, _ = _CapturingAsyncClient.make_factory(responses)

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)

    _, _, captcha = await probe.stealth_probe(
        "https://x.test/api", property_id="P-001"
    )
    assert captcha == "recaptcha"


# ─────────────────────────────────────────────────────────────────────
# Failure modes — never raise, always degrade gracefully
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stealth_probe_returns_none_on_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from ma_poc.fetch import probe

    factory, _ = _CapturingAsyncClient.make_factory({}, raise_on=("GET", "https://x.test/api"))

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)

    body, status, captcha = await probe.stealth_probe(
        "https://x.test/api", property_id="P-001"
    )
    assert body is None
    assert status is None
    assert captcha is None


# ─────────────────────────────────────────────────────────────────────
# Telemetry — HOP_CAPTCHA_DETECTED fires with the supplied context
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stealth_probe_emits_hop_captcha_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the response body matches a CAPTCHA shape, stealth_probe must
    emit ``HOP_CAPTCHA_DETECTED`` with the caller-supplied
    ``telemetry_context`` so production aggregation can break down rates
    by hop class (specials_probe / realpage_cws_probe / beacon_ajax_probe).
    """
    from ma_poc.fetch import probe
    from ma_poc.observability.events import EventKind

    cf_body = b"<html>Just a moment... challenge-platform __cf_chl_</html>"
    responses = {("GET", "https://x.test/api"): _FakeResponse(status=200, content=cf_body)}
    factory, _ = _CapturingAsyncClient.make_factory(responses)

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)

    captured: list[tuple] = []

    def _fake_emit(kind, pid, **data):
        captured.append((kind, pid, data))

    monkeypatch.setattr("ma_poc.observability.events.emit", _fake_emit)

    await probe.stealth_probe(
        "https://x.test/api",
        property_id="P-001",
        telemetry_context="realpage_cws_probe",
    )

    hop_events = [(p, d) for (k, p, d) in captured if k == EventKind.HOP_CAPTCHA_DETECTED]
    assert len(hop_events) == 1
    pid, payload = hop_events[0]
    assert pid == "P-001"
    assert payload["provider"] == "cloudflare"
    assert payload["context"] == "realpage_cws_probe"
    assert payload["url"] == "https://x.test/api"
    assert payload["status"] == 200


@pytest.mark.asyncio
async def test_stealth_probe_does_not_emit_when_no_captcha(monkeypatch: pytest.MonkeyPatch) -> None:
    """Clean responses must not emit HOP_CAPTCHA_DETECTED — keeps the
    counter measuring what it claims to measure."""
    from ma_poc.fetch import probe
    from ma_poc.observability.events import EventKind

    responses = {("GET", "https://x.test/api"): _FakeResponse(status=200, content=b"<html>clean</html>")}
    factory, _ = _CapturingAsyncClient.make_factory(responses)

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)

    captured: list[tuple] = []

    def _fake_emit(kind, pid, **data):
        captured.append((kind, pid, data))

    monkeypatch.setattr("ma_poc.observability.events.emit", _fake_emit)

    await probe.stealth_probe(
        "https://x.test/api",
        property_id="P-001",
        telemetry_context="beacon_ajax_probe",
    )

    hop_events = [e for e in captured if e[0] == EventKind.HOP_CAPTCHA_DETECTED]
    assert hop_events == []


@pytest.mark.asyncio
async def test_stealth_probe_post_with_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """POST + form data path (Beacon AJAX shape)."""
    from ma_poc.fetch import probe

    responses = {("POST", "https://x.test/wp-admin/admin-ajax.php"): _FakeResponse(status=200, content=b"<table/>")}
    factory, instance = _CapturingAsyncClient.make_factory(responses)

    class _Module:
        AsyncClient = staticmethod(factory)

    import sys
    monkeypatch.setitem(sys.modules, "httpx", _Module)

    body, status, captcha = await probe.stealth_probe(
        "https://x.test/wp-admin/admin-ajax.php",
        method="POST",
        property_id="P-001",
        data={"action": "beacon_property_aptmt_search"},
        extra_headers={"X-Requested-With": "XMLHttpRequest"},
    )
    assert status == 200
    assert body == b"<table/>"
    # POST shape recorded.
    last_req = instance["last"].requests[0]
    assert last_req["method"] == "POST"
    assert last_req["data"] == {"action": "beacon_property_aptmt_search"}
    # Both stealth + custom headers landed.
    assert "User-Agent" in last_req["headers"]
    assert last_req["headers"]["X-Requested-With"] == "XMLHttpRequest"
