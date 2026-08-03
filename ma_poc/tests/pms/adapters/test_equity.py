"""Equity Residential adapter tests.

2026-05-20 cluster #7 — 25 props tagged TIER_1_API_EQUITY with 0
strict units in the canary feature run. Main extracts via various
paths (TIER_1_API_SIGHTMAP, TIER_3_DOM, TIER_4_LLM_DOM) — so the
property data IS recoverable, just not via the Equity-specific
adapter on the canary's fetched HTML.

These tests pin:
  * EquityAdapter.extract emits the right tier label per outcome
    (success / parse-rejected / no-blocks-found)
  * Empty-exit labels are recognized by the registry so the Path B/C
    retry hook fires and Step 8 generic fallback runs
  * The HTML parser ``parse_equity_units`` handles the real-shape
    ea5-unit blocks documented in the adapter docstring
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.equity import (
    OLL_TIER,
    EquityAdapter,
    parse_equity_units,
)
from ma_poc.scripts.runners.jugnu import _format_v2_unit

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeProbeResponse:
    """Minimal curl_cffi-response shim consumed by ``_html_for``.

    ``equity._html_for`` reads ``.status_code`` and ``.text``; the extra
    ``.content`` / ``.headers`` / ``.url`` attributes keep the shape
    honest for any sibling call site.
    """

    __slots__ = ("status_code", "text", "content", "headers", "url")

    def __init__(self, url: str, status_code: int = 404, text: str = "") -> None:
        self.url = url
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers: dict[str, str] = {}


@pytest.fixture(autouse=True)
def _stub_probe_get(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the ``_probe`` network seam for every test in this module.

    ``EquityAdapter.extract`` → ``_html_for`` re-fetches ``ctx.base_url``
    through ``probe_get`` whenever the captured body has no priced
    ``ledgerId:`` block — which is exactly the case these tests
    construct. Returning a 404 makes the refetch a deterministic no-op
    so the adapter falls back to ``ctx.fetch_result.body``: the HTML the
    test actually authored. That is the same branch the suite has always
    taken offline, minus the live request to equityapartments.com.
    """

    def _fake_probe_get(url: str, **_kw: object) -> _FakeProbeResponse:
        return _FakeProbeResponse(url)

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _fake_probe_get)


def _make_ctx(html: bytes | str | None) -> AdapterContext:
    """Minimal AdapterContext for the Equity adapter.

    EquityAdapter reads the HTML via ``_html_for(ctx, page)`` which
    pulls from ``ctx.fetch_result.body`` when available, else falls
    back to ``page.content()`` (None here)."""

    class _FetchResult:
        # The adapter reads body as bytes; encode here for realism.
        if isinstance(html, str):
            body = html.encode("utf-8")
        else:
            body = html
        final_url = "https://www.equityapartments.com/some-property"

    return AdapterContext(
        base_url="https://www.equityapartments.com/some-property",
        detected=None,  # type: ignore[arg-type]
        profile=None,
        expected_total_units=None,
        property_id="P-equity-test",
        fetch_result=_FetchResult(),  # type: ignore[arg-type]
    )


class _DummyPage:
    """No Playwright page in unit tests; the adapter handles None."""

    async def content(self) -> str:  # pragma: no cover — unused
        return ""


# A real Equity ea5-unit block shape, derived from the adapter's
# research-log docstring (Westerly propertyId 4274, unitId 175).
_REAL_EA5_BLOCK = (
    "<!-- ledgerId: 24112, buildingId: 1, unitId: 175 -->"
    '<ea5-unit value="vm.BedroomTypes[0].AvailableUnits[0]">'
    '<div class="unit"><div class="unit-expanded-card">'
    '<span class="pricing">$1,150</span>'
    '<span class="time-period">12 mo</span>'
    "0 Bed <b>/</b> 1 Bath"
    "<span>576 sq.ft.</span>"
    "Available 6/4/2026"
    '<img class="static" src="/img/plan-383881-0-1-576" alt="S3" />'
    "</div></div></ea5-unit>"
)


# ─────────────────────────────────────────────────────────────────────
# Parser behavior
# ─────────────────────────────────────────────────────────────────────


def test_parse_equity_units_extracts_real_block_shape() -> None:
    """Sanity — the real-shape block from the adapter docstring parses
    out beds/baths/sqft/rent/available_date/floor_plan_name."""
    html = f"<html><body>{_REAL_EA5_BLOCK}</body></html>"
    units = parse_equity_units(html, "https://www.equityapartments.com/x")
    assert len(units) == 1
    u = units[0]
    # Beds=0 (studio), baths=1, sqft=576, rent=$1150
    assert u["bedrooms"] in {"0", "Studio"} or "studio" in u.get("bed_label", "").lower()
    assert u["bathrooms"] == "1"
    assert u["sqft"] == "576"
    assert "1,150" in u["rent_range"] or "1150" in u["rent_range"]
    # Plan name from img alt
    assert "S3" in (u.get("floor_plan_name") or "")
    assert u["unit_number"] == "175"
    assert u["unit_id"] == "1:175"
    assert u["building"] == "1"
    assert u["source_ids"] == {
        "equity_building_unit_id": "1:175",
        "equity_unit_id": "175",
        "equity_building_id": "1",
        "equity_ledger_id": "24112",
    }
    assert u["source_property_id"] == "24112"
    assert u["source_property_provenance"] == ("equity_unit_comment.ledgerId")
    assert u["source_response_provenance"] == ("equity_server_rendered_ea5_unit")


def test_parse_equity_units_empty_html_returns_empty() -> None:
    assert parse_equity_units("", "https://x.com/") == []
    assert parse_equity_units("<html><body>no units here</body></html>", "x") == []


def test_parse_equity_units_dedups_multiple_blocks() -> None:
    """Two ea5-unit blocks → two units (different unitIds)."""
    block2 = _REAL_EA5_BLOCK.replace("unitId: 175", "unitId: 176").replace("$1,150", "$1,400")
    html = f"<html><body>{_REAL_EA5_BLOCK}{block2}</body></html>"
    units = parse_equity_units(html, "x")
    assert len(units) == 2


def test_building_composite_preserves_colliding_public_unit_numbers() -> None:
    block2 = _REAL_EA5_BLOCK.replace("buildingId: 1", "buildingId: 2")
    units = parse_equity_units(_REAL_EA5_BLOCK + block2, "x")

    assert [unit["unit_number"] for unit in units] == ["175", "175"]
    assert [unit["unit_id"] for unit in units] == ["1:175", "2:175"]
    assert len({unit["unit_id"] for unit in units}) == 2
    assert all(unit["source_ids"]["equity_ledger_id"] == "24112" for unit in units)


def test_current_403_control_fixture_preserves_exact_identity_and_values() -> None:
    html = (FIXTURES / "equity_village_del_mar_7797_unit.html").read_text(encoding="utf-8")
    units = parse_equity_units(
        html,
        "https://www.equityapartments.com/san-diego/del-mar/the-village-at-del-mar-heights-apartments",
    )

    assert len(units) == 1
    unit = units[0]
    assert unit["unit_id"] == "01:033"
    assert unit["unit_number"] == "033"
    assert unit["building"] == "01"
    assert unit["floor_plan_name"] == "1 Bedroom A"
    assert unit["bedrooms"] == "1"
    assert unit["bathrooms"] == "1"
    assert unit["sqft"] == "700"
    assert unit["market_rent_low"] == 3298
    assert unit["lease_term"] == "12 mo"
    assert unit["availability_date"] == "2026-08-21"
    assert unit["source_ids"]["equity_ledger_id"] == "29855"


def test_current_403_fixture_survives_source_to_final_format() -> None:
    source = parse_equity_units(
        (FIXTURES / "equity_village_del_mar_7797_unit.html").read_text(encoding="utf-8"),
        "https://www.equityapartments.com/village",
    )[0]

    final = _format_v2_unit(
        source,
        datetime(2026, 8, 2, 12, tzinfo=UTC),
        property_id="7797",
    )

    assert final["unit_id"] == "01:033"
    assert final["building"] == "01"
    assert final["floor_plan_name"] == "1 Bedroom A"
    assert final["beds"] == 1
    assert final["baths"] == 1.0
    assert final["area"] == 700
    assert final["rent_low"] == 3298
    assert final["lease_term"] == 12
    assert final["lease_term_raw"] == "12 mo"
    assert final["available_date"] == "2026-08-21"
    assert final["source_ids"]["equity_building_unit_id"] == "01:033"


# ─────────────────────────────────────────────────────────────────────
# Adapter tier-label contract (the cluster #7 fix)
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_equity_emits_bare_success_label_on_real_data() -> None:
    """Regression guard — when ea5-unit blocks ARE present and post-
    process admits them, the adapter must keep the bare ``TIER_1_API_EQUITY``
    label so Path B/C does NOT retry on success."""
    html = f"<html><body>{_REAL_EA5_BLOCK}</body></html>"
    adapter = EquityAdapter()
    ctx = _make_ctx(html)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.tier_used == OLL_TIER, f"real data must keep bare success label; got {result.tier_used!r}"
    assert len(result.units) >= 1
    assert result.api_responses[0]["via"] == "equity_server_rendered_html"
    assert result.api_responses[0]["rows"] == 1


@pytest.mark.asyncio
async def test_equity_emits_no_response_when_no_ea5_blocks() -> None:
    """Cluster #7 pattern — equityapartments.com URL returned a body
    with no ea5-unit blocks (legacy .aspx redirect, Cloudflare
    challenge, or genuine no-availability). Adapter must emit
    ``TIER_1_API_EQUITY_NO_RESPONSE`` so retry/fallback can engage."""
    html = "<html><body><h1>Welcome</h1><p>This page doesn't have any unit blocks.</p></body></html>"
    adapter = EquityAdapter()
    ctx = _make_ctx(html)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    assert result.units == []
    assert result.tier_used == f"{OLL_TIER}_NO_RESPONSE", (
        f"expected _NO_RESPONSE label; got {result.tier_used!r}"
    )


@pytest.mark.asyncio
async def test_equity_emits_empty_when_blocks_fail_validity() -> None:
    """Edge case — ea5-unit blocks were parsed but post_process
    rejected them all (e.g. malformed rent/beds). Adapter must emit
    ``TIER_1_API_EQUITY_EMPTY``."""
    # Construct an ea5-unit block missing both rent AND any physical
    # dimension — should be rejected by the validity gate.
    bad_block = (
        "<!-- ledgerId: 1, buildingId: 1, unitId: BAD -->"
        '<ea5-unit value="">'
        '<div class="unit"><div class="unit-expanded-card">'
        # No pricing, no beds, no sqft — should be rejected.
        "</div></div></ea5-unit>"
    )
    html = f"<html><body>{bad_block}</body></html>"
    adapter = EquityAdapter()
    ctx = _make_ctx(html)
    result = await adapter.extract(_DummyPage(), ctx)  # type: ignore[arg-type]
    # Either parse_equity_units returns [] (no rent → drop early) or
    # post_process rejects. Either way, the tier_used must signal
    # empty-exit so retry/fallback runs.
    assert result.units == []
    assert result.tier_used in {
        f"{OLL_TIER}_NO_RESPONSE",
        f"{OLL_TIER}_EMPTY",
    }, f"expected empty-exit label, got {result.tier_used!r}"


@pytest.mark.asyncio
async def test_equity_empty_labels_in_empty_exit_registry() -> None:
    """Both empty-exit labels must be recognized by ``is_empty_exit``
    so the Path B/C retry hook in scraper.py fires on them."""
    from ma_poc.pms.empty_exit import is_empty_exit

    assert is_empty_exit(f"{OLL_TIER}_NO_RESPONSE") is True
    assert is_empty_exit(f"{OLL_TIER}_EMPTY") is True
    # Bare success label must NOT trigger retry.
    assert is_empty_exit(OLL_TIER) is False


def test_equity_static_fingerprints_nonempty() -> None:
    assert EquityAdapter().static_fingerprints()
