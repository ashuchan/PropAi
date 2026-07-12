"""SightMap link-hop short-circuit (_is_priced_sightmap_result).

2026-07-11 quality sweep. The SightMap embed is site-global: one successful
SightMap direct-API extraction carries the FULL priced roster across every
floor plan. When the base floorplan result is already a priced SightMap
extraction, ``_try_link_hop`` must NOT enter per-plan subpage accumulation —
each hop re-renders the page (CF-clearance) and re-fetches the same embed,
the sink that timed cltexchange.com + a 15-property cohort past the 600s
per-property wall into phantom-null geometry salvages.

These tests pin the predicate that gates the short-circuit. Guards keep it
conservative: only ``TIER_1_API_SIGHTMAP*`` non-plan-level tiers with a
MAJORITY of priced units fire it, so thin parses and the "full map, no
prices" first embed of a two-embed property still get the subpage walk.
"""

from ma_poc.pms.scraper import _is_priced_sightmap_result


def _sightmap_units(n_priced, n_unpriced, rent="$1,450"):
    us = [{"unit_id": f"p{i}", "rent_range": rent} for i in range(n_priced)]
    us += [{"unit_id": f"u{i}", "rent_range": ""} for i in range(n_unpriced)]
    return us


def test_priced_sightmap_iframe_short_circuits():
    r = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP_IFRAME",
        "units": _sightmap_units(86, 19),  # cltexchange shape: 86/105 priced
    }
    assert _is_priced_sightmap_result(r) is True


def test_sightmap_direct_priced_short_circuits():
    r = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP_DIRECT",
        "units": _sightmap_units(10, 0),
    }
    assert _is_priced_sightmap_result(r) is True


def test_plan_level_sightmap_does_not_short_circuit():
    # *_PLAN_LEVEL carries no unit-level rent — a subpage walk may enrich it.
    r = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP_IFRAME_PLAN_LEVEL",
        "units": _sightmap_units(5, 0),
    }
    assert _is_priced_sightmap_result(r) is False


def test_unpriced_full_map_embed_does_not_short_circuit():
    # Two-embed property: the homepage "full map" embed lists every unit with
    # NO prices. Must keep walking to find the priced /availability embed.
    r = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP_IFRAME",
        "units": _sightmap_units(0, 50),
    }
    assert _is_priced_sightmap_result(r) is False


def test_minority_priced_does_not_short_circuit():
    # Only 20% priced — below the majority guard, keep accumulating.
    r = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP",
        "units": _sightmap_units(10, 40),
    }
    assert _is_priced_sightmap_result(r) is False


def test_exact_majority_priced_short_circuits():
    # >= ceil(n/2) priced is enough (3 of 5).
    r = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP",
        "units": _sightmap_units(3, 2),
    }
    assert _is_priced_sightmap_result(r) is True


def test_non_sightmap_tier_never_short_circuits():
    r = {
        "extraction_tier_used": "TIER_3_DOM",
        "units": _sightmap_units(100, 0),
    }
    assert _is_priced_sightmap_result(r) is False


def test_empty_units_does_not_short_circuit():
    assert _is_priced_sightmap_result(
        {"extraction_tier_used": "TIER_1_API_SIGHTMAP_IFRAME", "units": []}
    ) is False


def test_missing_tier_does_not_short_circuit():
    assert _is_priced_sightmap_result({"units": _sightmap_units(5, 0)}) is False


def test_sentinel_rent_strings_count_as_unpriced():
    # "Call for pricing" / "TBD" are no-rent per _sightmap_unit_has_rent.
    r = {
        "extraction_tier_used": "TIER_1_API_SIGHTMAP_IFRAME",
        "units": _sightmap_units(0, 0) + [
            {"unit_id": "a", "rent_range": "Call for pricing"},
            {"unit_id": "b", "rent_range": "TBD"},
        ],
    }
    assert _is_priced_sightmap_result(r) is False
