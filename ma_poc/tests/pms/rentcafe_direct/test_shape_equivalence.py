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


@pytest.fixture(autouse=True)
def _securecafe_portal_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ONE branch: the SecureCafe portal is unreachable.

    RentCafe floorplan payloads intentionally have no synthetic unit number
    (PR #110), so when the plan→SecureCafe drill is enabled the adapter tries a
    detail recovery before returning the correctly-classified plan rows. These
    fixture tests verify parser shape only, so the recovery seam must not
    attempt a live request.

    WHAT THIS PINS: ``probe_get`` returns 404 ⇒ ``_find_all_securecafe_bases``
    finds nothing on an empty body ⇒ ``bases`` stays empty ⇒ the probe returns
    ``[]`` ⇒ the adapter falls through to the plan rows and stamps
    ``TIER_1_API_RENTCAFE``. The tier assertion at the bottom of this file is
    load-bearing on that: a SUCCESSFUL portal would stamp
    ``TIER_1_API_RENTCAFE_SECURECAFE_FROM_PLAN`` and fail the test.

    WHAT IT DOES **NOT** COVER: any successful portal response — i.e. the
    replace-instead-of-merge hazard and the acceptance guard. Those live in
    ``test_securecafe_from_plan.py``; do not read this fixture's green as
    evidence about them.

    Note this is a NARROWING, not the actual network guard. The repo-level
    ``_block_live_network`` in ``ma_poc/conftest.py`` (whose
    ``UnstubbedNetworkCall`` derives from ``BaseException`` so a blanket
    ``except Exception`` cannot swallow it) is what actually bites; removing
    this fixture yields 5 failed / 1 passed, all ``UnstubbedNetworkCall``.
    """

    class _NoInventoryResponse:
        status_code = 404
        text = ""
        content = b""
        headers: dict[str, str] = {}

    def _no_inventory(*_args: object, **_kwargs: object) -> _NoInventoryResponse:
        return _NoInventoryResponse()

    monkeypatch.setattr("ma_poc.pms.adapters._probe.probe_get", _no_inventory)


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

    # 2026-07-27: the key-PRESENCE check above passed happily through PR #110,
    # which changed the VALUE of ``unit_number`` on every one of these rows from
    # a RentCafe floorplanId to "". Nothing in the suite asserted that. Assert
    # the actual post-#110 shape here so a regression to "plan id in
    # unit_number" (false gold: a plan id is shared by every apartment on the
    # plan, so plan rows read as unit-level and recovery skips them) fails a
    # test rather than shipping.
    for unit in result.units:
        assert unit["unit_number"] == "", (
            f"{canonical_id}: a RentCafe plan row must carry an EMPTY "
            f"unit_number, got {unit['unit_number']!r}. A floorplanId must "
            f"never go back into unit_number (PR #110)."
        )
        sids = unit.get("source_ids") or {}
        assert sids.get("rentcafe_floorplan_id"), (
            f"{canonical_id}: the plan id must still be captured as provenance "
            f"in source_ids['rentcafe_floorplan_id']; got {sids!r}"
        )

    # Tier-used should be the canonical TIER_1_API_RENTCAFE — the direct
    # path doesn't change what the parser stamps; the runner stamps the
    # rentcafe_direct sub-tier separately.
    assert result.tier_used == "TIER_1_API_RENTCAFE"
