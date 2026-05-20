"""D25 (2026-05-19) — Beacon Management WordPress admin-ajax probe.

PID 244123 theshoresoflakestclair.com canonical. The Beacon-Management
WordPress plugin family serves apartment data behind:

    POST {host}/wp-admin/admin-ajax.php
    action=beacon_property_aptmt_search

The endpoint:
  • Has no CSRF nonce
  • Infers property identity from the request host
  • Returns an HTML fragment with ``<table class="beacon-aptsearch-rslt-table">``

The probe ``_probe_beacon_ajax`` gates on the marker
``beacon_property_aptmt_search_form`` class in entry HTML, POSTs to the
admin-ajax endpoint, and parses the returned table into unit dicts.

Also covers the ``_LINK_SKIP_PATTERNS`` carve-out — pre-fix the
``/wp-admin/`` substring blanket-rejected any link whose path contained
``wp-admin``, including the AJAX endpoint.
"""
from __future__ import annotations

import pytest

# ── Beacon response-table parser (pure function) ──────────────────────────

class TestParseBeaconResponseTable:
    def test_real_world_response_yields_units(self) -> None:
        """A realistic fragment as captured live from
        ``theshoresoflakestclair.com/wp-admin/admin-ajax.php`` on 2026-05-19."""
        from ma_poc.pms.adapters.generic import _parse_beacon_response_table
        html = """
<table class="beacon-aptsearch-rslt-table avility tbdesktop newseavail">
  <thead>
    <tr class="headtr"><td>Apartment</td><td>Beds</td><td>Baths</td>
        <td>Sq. Ft.</td><td>Rent</td><td>Date Available</td><td>Action</td></tr>
  </thead>
  <tr data-singleUnit-id="475199">
    <td>#01112</td>
    <td>1</td>
    <td>1</td>
    <td>650</td>
    <td>$1,234</td>
    <td>2026-06-15</td>
    <td><a class="apply">Apply</a></td>
  </tr>
  <tr data-singleUnit-id="475200">
    <td>#01113</td>
    <td>2</td>
    <td>1.5</td>
    <td>900</td>
    <td>$1,499</td>
    <td>2026-07-01</td>
    <td>Apply</td>
  </tr>
</table>"""
        units = _parse_beacon_response_table(html)
        assert len(units) == 2
        u0 = units[0]
        assert u0["unit_id"] == "beacon_475199"
        assert u0["unit_number"] == "01112"
        assert u0["bedrooms"] == 1
        assert u0["bathrooms"] == 1
        assert u0["sqft"] == 650
        assert u0["market_rent_low"] == 1234
        assert u0["market_rent_high"] == 1234
        assert u0["available_date"] == "2026-06-15"
        assert u0["availability_status"] == "AVAILABLE"
        assert u0["extraction_tier"] == "TIER_1_API"

        u1 = units[1]
        assert u1["unit_number"] == "01113"
        assert u1["bathrooms"] == 1  # int coercion truncates 1.5
        assert u1["market_rent_low"] == 1499

    def test_no_table_returns_empty(self) -> None:
        from ma_poc.pms.adapters.generic import _parse_beacon_response_table
        assert _parse_beacon_response_table("<div>no table here</div>") == []
        assert _parse_beacon_response_table("") == []

    def test_table_with_only_header_returns_empty(self) -> None:
        """Only the thead row, no data rows → 0 units."""
        from ma_poc.pms.adapters.generic import _parse_beacon_response_table
        html = """<table class="beacon-aptsearch-rslt-table">
            <thead><tr><td>Apartment</td></tr></thead></table>"""
        assert _parse_beacon_response_table(html) == []

    def test_row_without_data_singleunit_id_is_skipped(self) -> None:
        """Rows must have ``data-singleUnit-id`` for the row regex to match —
        alert banners and the thead row don't have it.
        """
        from ma_poc.pms.adapters.generic import _parse_beacon_response_table
        html = """<table class="beacon-aptsearch-rslt-table">
          <tr class="alert-row"><td>Error message here</td></tr>
          <tr data-singleUnit-id="999"><td>#04</td><td>1</td><td>1</td>
            <td>500</td><td>$1000</td><td>now</td></tr>
        </table>"""
        units = _parse_beacon_response_table(html)
        assert len(units) == 1
        assert units[0]["unit_number"] == "04"

    def test_short_row_skipped(self) -> None:
        """A row with fewer than 5 cells is malformed and silently dropped."""
        from ma_poc.pms.adapters.generic import _parse_beacon_response_table
        html = """<table class="beacon-aptsearch-rslt-table">
          <tr data-singleUnit-id="1"><td>#04</td><td>1</td></tr>
          <tr data-singleUnit-id="2"><td>#05</td><td>1</td><td>1</td>
            <td>500</td><td>$1000</td></tr>
        </table>"""
        units = _parse_beacon_response_table(html)
        assert len(units) == 1
        assert units[0]["unit_number"] == "05"

    def test_rent_with_dollar_and_commas(self) -> None:
        """The ``_num`` cell parser must handle ``$1,234``, ``1,234.00``, and
        a bare integer the same way — all should yield 1234."""
        from ma_poc.pms.adapters.generic import _parse_beacon_response_table
        rows = []
        for i, rent_text in enumerate(["$1,234", "$1,234.00", "1234", "1,234"]):
            rows.append(
                f"""<tr data-singleUnit-id="{100 + i}"><td>#a{i}</td><td>1</td><td>1</td>
                <td>500</td><td>{rent_text}</td><td>now</td></tr>"""
            )
        html = (
            '<table class="beacon-aptsearch-rslt-table">'
            + "".join(rows)
            + "</table>"
        )
        units = _parse_beacon_response_table(html)
        assert len(units) == 4
        assert all(u["market_rent_low"] == 1234 for u in units)

    def test_unit_number_hash_prefix_stripped(self) -> None:
        from ma_poc.pms.adapters.generic import _parse_beacon_response_table
        html = """<table class="beacon-aptsearch-rslt-table">
          <tr data-singleUnit-id="1"><td>  #ABC-12  </td>
            <td>1</td><td>1</td><td>500</td><td>$900</td><td>now</td></tr>
        </table>"""
        units = _parse_beacon_response_table(html)
        assert units[0]["unit_number"] == "ABC-12"


# ── _probe_beacon_ajax (network probe) ─────────────────────────────────────
#
# These tests are ``async def`` so pytest-asyncio (auto mode per pyproject.toml)
# runs them on its own managed event loop. Using ``asyncio.run()`` inside a
# sync test closes the loop on exit, which breaks any subsequently-running test
# that calls ``asyncio.get_event_loop()`` (e.g. ``test_entrata_tier_label_downgrade``).

class TestProbeBeaconAjax:
    async def test_returns_empty_when_marker_absent(self) -> None:
        """The gate must short-circuit on HTML that doesn't show the Beacon
        marker class — no httpx call is made."""
        from ma_poc.pms.adapters.generic import _probe_beacon_ajax
        out = await _probe_beacon_ajax("<html>no beacon here</html>", "https://x.com/")
        assert out == []

    async def test_returns_empty_on_invalid_base_url(self) -> None:
        from ma_poc.pms.adapters.generic import _probe_beacon_ajax
        html = '<form class="beacon_property_aptmt_search_form"></form>'
        # Missing scheme + netloc → bail
        out = await _probe_beacon_ajax(html, "/just/a/path")
        assert out == []

    async def test_constructs_correct_admin_ajax_url(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The probe builds the admin-ajax URL from the property base host,
        not from any URL in HTML — verified by stubbing ``httpx.AsyncClient``
        and capturing the URL it was asked to POST to.
        """
        captured_url: dict[str, str] = {}

        class _StubResp:
            status_code = 200
            text = ""  # empty → empty parse → empty list
            content = b""

        class _StubClient:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            async def __aenter__(self) -> _StubClient:
                return self

            async def __aexit__(self, *_args: object) -> None:
                return None

            async def request(self, _method: str, url: str, **_kwargs: object) -> _StubResp:
                # 2026-05-21 (stealth-on-hops): Beacon probe now routes
                # through ``stealth_probe`` which calls
                # ``client.request("POST", url, data=...)`` rather than
                # ``client.post(url, ...)``. Stub both shapes so the
                # test stays valid regardless of which API the probe
                # uses internally.
                captured_url["url"] = url
                return _StubResp()

            async def post(self, url: str, **_kwargs: object) -> _StubResp:
                captured_url["url"] = url
                return _StubResp()

        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", _StubClient)

        from ma_poc.pms.adapters.generic import _probe_beacon_ajax
        html = '<form class="beacon_property_aptmt_search_form"></form>'
        await _probe_beacon_ajax(
            html, "https://theshoresoflakestclair.com/floor-plan/"
        )
        assert (
            captured_url.get("url")
            == "https://theshoresoflakestclair.com/wp-admin/admin-ajax.php"
        )

    async def test_non_200_returns_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        class _StubResp:
            status_code = 500
            text = "internal server error"
            content = b"internal server error"

        class _StubClient:
            def __init__(self, *_a: object, **_k: object) -> None:
                pass

            async def __aenter__(self) -> _StubClient:
                return self

            async def __aexit__(self, *_a: object) -> None:
                return None

            async def request(self, *_a: object, **_k: object) -> _StubResp:
                return _StubResp()

            async def post(self, *_a: object, **_k: object) -> _StubResp:
                return _StubResp()

        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", _StubClient)

        from ma_poc.pms.adapters.generic import _probe_beacon_ajax
        html = '<div class="beacon_property_aptmt_search_form">x</div>'
        out = await _probe_beacon_ajax(html, "https://x.example.com/")
        assert out == []

    async def test_200_with_table_yields_units(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: marker present + 200 + populated table → units."""
        table_html = """
<table class="beacon-aptsearch-rslt-table">
  <tr data-singleUnit-id="100"><td>#01</td><td>1</td><td>1</td>
    <td>500</td><td>$999</td><td>2026-06-01</td></tr>
</table>"""

        class _StubResp:
            status_code = 200
            text = table_html
            content = table_html.encode("utf-8")

        class _StubClient:
            def __init__(self, *_a: object, **_k: object) -> None:
                pass

            async def __aenter__(self) -> _StubClient:
                return self

            async def __aexit__(self, *_a: object) -> None:
                return None

            async def request(self, *_a: object, **_k: object) -> _StubResp:
                return _StubResp()

            async def post(self, *_a: object, **_k: object) -> _StubResp:
                return _StubResp()

        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", _StubClient)

        from ma_poc.pms.adapters.generic import _probe_beacon_ajax
        html = '<form class="beacon_property_aptmt_search_form"></form>'
        out = await _probe_beacon_ajax(
            html, "https://x.example.com/property/y/"
        )
        assert len(out) == 1
        assert out[0]["unit_number"] == "01"
        assert out[0]["market_rent_low"] == 999

    async def test_network_error_returns_empty(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Probe must never raise — any httpx exception → empty list."""
        class _ExplodingClient:
            def __init__(self, *_a: object, **_k: object) -> None:
                pass

            async def __aenter__(self) -> _ExplodingClient:
                return self

            async def __aexit__(self, *_a: object) -> None:
                return None

            async def request(self, *_a: object, **_k: object) -> None:
                raise ConnectionError("network down")

            async def post(self, *_a: object, **_k: object) -> None:
                raise ConnectionError("network down")

        import httpx as _httpx
        monkeypatch.setattr(_httpx, "AsyncClient", _ExplodingClient)

        from ma_poc.pms.adapters.generic import _probe_beacon_ajax
        html = '<div class="beacon_property_aptmt_search_form">x</div>'
        out = await _probe_beacon_ajax(html, "https://x.example.com/")
        assert out == []


# ── _LINK_SKIP_PATTERNS carve-out ─────────────────────────────────────────

class TestWpAdminAjaxCarveOut:
    """The ``/wp-admin/`` link-skip pattern was added 2026-05-16 to drop
    Yardi marketing-shell asset noise. The 2026-05-19 carve-out lets
    ``wp-admin/admin-ajax.php?action=*`` URLs through so future synthesis
    paths can queue Beacon-style AJAX endpoints as hop candidates.

    The carve-out is in-place inside ``_rank_internal_links`` — we don't
    expose the skip-check as a callable, so we exercise the ranker against
    a synthetic HTML page that contains both an asset-noise URL (should be
    dropped) and an AJAX URL (should be admitted).
    """

    def test_admin_ajax_with_action_param_is_ranked(self) -> None:
        """The carve-out admits ``wp-admin/admin-ajax.php?action=*`` URLs past
        the skip filter. Anchor text needs to score positively for the URL to
        survive the downstream ``score > 0`` gate — using "view availability"
        (the typical anchor on a Beacon site's apartment-search button).
        """
        from ma_poc.pms.scraper import _rank_internal_links
        html = """<html><body>
          <a href="/wp-content/themes/foo/style.css">style</a>
          <a href="/wp-admin/edit.php">edit</a>
          <a href="/wp-admin/admin-ajax.php?action=beacon_property_aptmt_search">view availability</a>
        </body></html>"""
        ranked = _rank_internal_links(html, "https://theshoresoflakestclair.com/")
        urls = [t[0] for t in ranked]
        # The AJAX URL must survive the skip filter + scoring
        assert any(
            "wp-admin/admin-ajax.php" in u and "action=" in u for u in urls
        ), f"AJAX URL was filtered. ranked={ranked}"
        # The asset-noise + admin/edit URLs must NOT survive
        assert not any("/wp-content/" in u for u in urls)
        assert not any(
            "/wp-admin/edit.php" in u and "action=" not in u for u in urls
        )

    def test_admin_ajax_without_action_still_skipped(self) -> None:
        """A bare ``wp-admin/admin-ajax.php`` link with no ``action=`` query
        is the WordPress AJAX endpoint shell — useless without an action and
        could come from theme-bundled JS configs. Keep skipping."""
        from ma_poc.pms.scraper import _rank_internal_links
        html = """<html><body>
          <a href="/wp-admin/admin-ajax.php">bare ajax</a>
        </body></html>"""
        ranked = _rank_internal_links(html, "https://x.example.com/")
        urls = [t[0] for t in ranked]
        assert not any("wp-admin/admin-ajax.php" in u for u in urls)
