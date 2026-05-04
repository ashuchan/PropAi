"""F5 / H7 — direct path output is shape-equivalent to the existing
RentCafe adapter for the same response body.

Parametrized over every captured fixture under
``ma_poc/tests/pms/adapters/fixtures/rentcafe_direct/``. Feeds the
captured body through ``RentCafeAdapter.extract`` via ``_api_responses``
and asserts canonical-key parity. Reuses the production parser — the
test fails if anyone reimplements parsing inside ``rentcafe_direct``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ma_poc.pms.adapters.base import AdapterContext
from ma_poc.pms.adapters.rentcafe import RentCafeAdapter, _is_rentcafe_response
from ma_poc.pms.detector import detect_pms

# parents[0]=rentcafe_direct, [1]=pms, [2]=tests, [3]=ma_poc
_FX = (
    Path(__file__).resolve().parents[3]
    / "tests"
    / "pms"
    / "adapters"
    / "fixtures"
    / "rentcafe_direct"
)


def _fixture_stems() -> list[str]:
    if not _FX.exists():
        return []
    out: list[str] = []
    for p in sorted(_FX.glob("*.json")):
        if p.name.endswith(".meta.json"):
            continue
        out.append(p.stem)
    return out


_FIXTURES = _fixture_stems()


def test_f5_shape_eq_fixture_count_at_least_5() -> None:
    """Drift guard — the parametrize must iterate ≥5 fixtures (per §8.2)."""
    assert len(_FIXTURES) >= 5, (
        f"Expected ≥5 captured fixtures under {_FX}; found {len(_FIXTURES)}: {_FIXTURES}"
    )


@pytest.mark.parametrize("canonical_id", _FIXTURES)
@pytest.mark.asyncio
async def test_f5_unit_shape_matches_rentcafe_adapter(canonical_id: str) -> None:
    body = json.loads((_FX / f"{canonical_id}.json").read_text(encoding="utf-8"))
    meta = json.loads((_FX / f"{canonical_id}.meta.json").read_text(encoding="utf-8"))
    assert _is_rentcafe_response(body), (
        f"Fixture {canonical_id} not RentCafe-shaped — re-capture or update detector"
    )

    ctx = AdapterContext(
        base_url=meta["capture_url"],
        detected=detect_pms(meta["capture_url"]),
        profile=None,
        expected_total_units=meta.get("expected_unit_count"),
        property_id=meta["expected_property_id"],
        property_name=meta["property_name"],
        city=meta["city"],
        state=meta.get("state", ""),
        zip_code=meta["zip"],
        pmc=meta.get("pmc", ""),
    )
    # The RentCafeAdapter looks up _api_responses on the context; the
    # direct path populates it the same way ``scrape()`` does for the
    # vanity-domain path.
    ctx._api_responses = [  # type: ignore[attr-defined]
        {"url": meta["capture_url"], "body": body, "status": 200, "headers": {}}
    ]

    # The existing adapter signature is ``extract(self, page, ctx)``. The
    # rentcafe adapter ignores the page argument entirely (it works from
    # _api_responses) so passing None is correct.
    result = await RentCafeAdapter().extract(None, ctx)  # type: ignore[arg-type]
    assert result.units, f"{canonical_id}: zero units extracted"

    # The existing adapter returns dict units shaped by
    # ``make_unit_dict`` (see ma_poc/pms/adapters/_parsing.py). H7 —
    # assert the canonical keys produced by that helper are present on
    # every unit; if a future adapter rewrite drops one, both vanity-
    # domain and direct paths break in lock-step (which is the point).
    canonical_keys = {
        "floor_plan_name",
        "bedrooms",
        "bathrooms",
        "sqft",
        "unit_number",
        "rent_range",
        "market_rent_low",
        "market_rent_high",
        "availability_status",
        "availability_date",
        "extraction_tier",
    }
    for unit in result.units:
        missing = canonical_keys - set(unit.keys())
        assert not missing, (
            f"{canonical_id}: unit missing canonical keys {missing}; "
            f"full keys = {sorted(unit.keys())}"
        )

    # Tier-used should be the canonical TIER_1_API_RENTCAFE — the direct
    # path doesn't change what the parser stamps; the runner stamps the
    # rentcafe_direct sub-tier separately.
    assert result.tier_used == "TIER_1_API_RENTCAFE"
