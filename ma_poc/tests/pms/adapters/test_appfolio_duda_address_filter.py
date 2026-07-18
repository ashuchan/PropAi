"""DUDA path address-filter wiring (87b837b follow-up).

The orchestrator ``filter_listings_by_property_address`` is already
test-covered. This module pins the *wiring* into the DUDA path so
a future refactor doesn't silently re-introduce the 87b837b
contamination (axiomproperties.com 455 units, gbatx.com 440 units).
"""
from __future__ import annotations

import re
from pathlib import Path

_APPFOLIO_PATH = (
    Path(__file__).resolve().parents[3] / "pms" / "adapters" / "appfolio.py"
)


def test_duda_path_invokes_address_filter() -> None:
    """The TIER_1_API_APPFOLIO_DUDA branch must call
    filter_listings_by_property_address on duda_units before the
    final emit — same as the VANITY path."""
    src = _APPFOLIO_PATH.read_text(encoding="utf-8")
    # Find the DUDA-emit block
    duda_idx = src.find('result.tier_used = "TIER_1_API_APPFOLIO_DUDA"')
    assert duda_idx > 0, "Could not find DUDA emit block in appfolio.py"
    # Look BACKWARDS from the emit for the filter call within a
    # reasonable window (the filter must run before the emit).
    window_start = max(0, duda_idx - 2000)
    window = src[window_start:duda_idx]
    assert "filter_listings_by_property_address" in window, (
        "DUDA path no longer calls filter_listings_by_property_address "
        "before the emit. This was the 87b837b contamination fix — "
        "axiomproperties/gbatx leaked the full PMC inventory without it."
    )
    assert "appfolio-duda-address-filter" in window, (
        "DUDA path must emit telemetry tagged "
        "'appfolio-duda-address-filter' so run reports can quantify "
        "the contamination filter activations."
    )


def test_duda_filter_fires_unconditionally() -> None:
    """2026-07-18: the DUDA address filter must fire even when property_group
    IS set. The URL-level propertyGroup filter proved unreliable — 94 props
    leaked the whole PMC despite it — so the address filter is the safety net
    and must NOT be gated on ``not duda_property_group``."""
    src = _APPFOLIO_PATH.read_text(encoding="utf-8")
    assert not re.search(
        r"if\s+duda_units\s+and\s+not\s+duda_property_group",
        src,
    ), (
        "DUDA address filter must NOT be gated on `not duda_property_group` "
        "— propertyGroup URL-scoping is unreliable; always run the address filter."
    )
    assert "if duda_units:" in src, "DUDA filter must fire whenever duda_units exist"


def test_duda_filter_passes_ctx_address_and_zip() -> None:
    """The filter signature requires (units, ctx_address, ctx_zip).
    Both must be read from the AdapterContext."""
    src = _APPFOLIO_PATH.read_text(encoding="utf-8")
    duda_idx = src.find('result.tier_used = "TIER_1_API_APPFOLIO_DUDA"')
    window = src[max(0, duda_idx - 2000):duda_idx]
    assert 'getattr(ctx, "address"' in window, "DUDA filter must read ctx.address"
    assert 'getattr(ctx, "zip_code"' in window, "DUDA filter must read ctx.zip_code"
