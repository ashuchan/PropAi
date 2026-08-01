"""Context-to-result transport for portal hints discovered in recovery."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._pms_portal_hop import (
    _record_rentcafe_portal_hint,
    has_strict_securecafe_handoff,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import detect_pms
from ma_poc.pms.scraper import _promote_context_portal_hints


def _ctx() -> AdapterContext:
    url = "https://marketing.example/floorplans"
    return AdapterContext(
        base_url=url,
        detected=detect_pms(url),
        profile=None,
        expected_total_units=None,
        property_id="portal-hint-transport",
    )


def test_context_portal_hint_is_promoted_to_adapter_result() -> None:
    ctx = _ctx()
    result = AdapterResult()
    source = (
        "http://Tenant.SecureCafe.com/onlineleasing/property-slug/"
        "guestlogin.aspx?tracking=discard"
    )

    _record_rentcafe_portal_hint(ctx, source)
    _promote_context_portal_hints(ctx, result)

    assert result._embedded_portal_hints == [  # type: ignore[attr-defined]
        (
            "https://tenant.securecafe.com/onlineleasing/"
            "property-slug/availableunits.aspx",
            "securecafe",
        )
    ]


def test_promotion_preserves_existing_hint_and_deduplicates_same_route() -> None:
    ctx = _ctx()
    result = AdapterResult()
    canonical = (
        "https://tenant.securecafe.com/onlineleasing/"
        "property-slug/availableunits.aspx"
    )
    result._embedded_portal_hints = [  # type: ignore[attr-defined]
        ("https://sightmap.example/embed", "sightmap"),
        (canonical, "securecafe"),
    ]
    _record_rentcafe_portal_hint(ctx, canonical.upper())

    _promote_context_portal_hints(ctx, result)

    assert result._embedded_portal_hints == [  # type: ignore[attr-defined]
        ("https://sightmap.example/embed", "sightmap"),
        (canonical, "securecafe"),
    ]


def test_no_context_hint_leaves_result_untouched() -> None:
    result = AdapterResult()

    _promote_context_portal_hints(_ctx(), result)

    assert not hasattr(result, "_embedded_portal_hints")


def test_canonical_securecafe_hint_qualifies_for_render_handoff() -> None:
    ctx = _ctx()
    _record_rentcafe_portal_hint(
        ctx,
        "http://Tenant.SecureCafe.com/onlineleasing/community/floorplans.aspx",
    )

    assert has_strict_securecafe_handoff(ctx) is True


@pytest.mark.parametrize(
    ("url", "portal"),
    (
        (
            "https://tenant.securecafe.com/onlineleasing/community/"
            "availableunits.aspx?tracking=1",
            "securecafe",
        ),
        (
            "https://tenant.securecafe.com.evil.test/onlineleasing/community/"
            "availableunits.aspx",
            "securecafe",
        ),
        (
            "https://user@tenant.securecafe.com/onlineleasing/community/"
            "availableunits.aspx",
            "securecafe",
        ),
        (
            "https://tenant.securecafe.com:443/onlineleasing/community/"
            "availableunits.aspx",
            "securecafe",
        ),
        (
            "https://tenant.securecafe.com/onlineleasing/community/"
            "availableunits.aspx",
            "sightmap",
        ),
    ),
    ids=("query", "lookalike-host", "credentials", "port", "wrong-portal"),
)
def test_untrusted_dynamic_hint_cannot_redirect_retry_budget(
    url: str,
    portal: str,
) -> None:
    ctx = _ctx()
    ctx._embedded_portal_hints = [(url, portal)]  # type: ignore[attr-defined]

    assert has_strict_securecafe_handoff(ctx) is False


@pytest.mark.asyncio
async def test_portal_hint_forces_link_hop_even_with_plan_rows() -> None:
    """Non-empty plan evidence must not strand a strict render handoff."""
    from ma_poc.pms import scraper as scraper_mod

    url = "https://marketing.example/"
    portal = (
        "https://tenant.securecafe.com/onlineleasing/community/"
        "availableunits.aspx"
    )
    primary = scraper_mod._empty_result(url)
    primary.update(
        {
            "units": [
                {
                    "floor_plan_name": "A1",
                    "beds": 1,
                    "baths": 1,
                    "sqft": 750,
                    "asking_rent": 1500,
                }
            ],
            "extraction_tier_used": "TIER_3_DOM_GENERIC_PLAN_LEVEL",
            "_adapter_used": "generic",
            "_detected_pms": {"pms": "rentcafe", "confidence": 0.9},
            "_embedded_portal_hints": [(portal, "securecafe")],
        }
    )
    body = ("<html><body>floorplans</body></html>" + (" " * 600)).encode()
    fetch_result = FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={"content-type": "text/html"},
        render_mode=RenderMode.RENDER,
        final_url=url,
        attempts=1,
        elapsed_ms=10,
    )
    hop = AsyncMock(return_value=None)

    with (
        patch("ma_poc.pms.scraper.scrape", new=AsyncMock(return_value=primary)),
        patch("ma_poc.pms.scraper._try_link_hop", new=hop),
    ):
        await scraper_mod.scrape_jugnu(
            SimpleNamespace(url=url, property_id="portal-plan-test"),
            fetch_result,
        )

    hop.assert_awaited_once()
    assert hop.await_args.kwargs["embedded_portal_hints"] == [
        (portal, "securecafe")
    ]
