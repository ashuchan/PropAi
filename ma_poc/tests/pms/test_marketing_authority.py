from __future__ import annotations

import pytest

from ma_poc.pms.marketing_authority import (
    knock_must_defer_to_current_marketing,
    marketing_authority_rule,
)


@pytest.mark.parametrize(
    ("property_id", "provider"),
    [
        ("292744", "rentcafe"),
        ("285567", "rentcafe"),
        ("24584", "rentcafe"),
        ("10590", "rentcafe"),
        ("34303", "sightmap"),
        ("74488", "sightmap"),
    ],
)
def test_audited_broad_knock_rosters_defer_to_live_marketing(
    property_id: str, provider: str
) -> None:
    rule = marketing_authority_rule(property_id)
    assert rule is not None and rule.authority_provider == provider
    assert knock_must_defer_to_current_marketing(property_id)


@pytest.mark.parametrize("property_id", ["64945", "281928", "999999"])
def test_unaffected_knock_properties_keep_normal_fast_path(property_id: str) -> None:
    assert marketing_authority_rule(property_id) is None
    assert not knock_must_defer_to_current_marketing(property_id)
