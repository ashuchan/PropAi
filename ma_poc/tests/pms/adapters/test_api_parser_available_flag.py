"""Boolean/date ``available`` key → availability_status mapping.

2026-07-11 status sweep: TIER_1_5_EMBEDDED shipped 16,400/16,568 units
(98%) with NULL availability_status. The CMS unit blobs (ResMan-fed WP
widget family — laureloaksapartmenthomes / horseshoeapartments /
syncatgreentrails, live-verified) carry availability under a boolean or
date-string ``available`` key that ``parse_api_responses`` never read —
it only consulted the *count* keys (``availableCount`` …) and explicit
``status`` strings. Observed shapes:

- ``available: false``                              → occupied
- ``available: true``                               → available now
- ``available: "!Date:2026-08-18T00:00:00.000Z"``   → available FROM
  that date (also the availability DATE the ``avail_dt`` key chain
  misses — backfilled when empty)

Explicit ``status`` keys still win; an absent key preserves the prior
(empty) behaviour so no other feed shape changes.
"""
from __future__ import annotations

from typing import Any

from ma_poc.pms.adapters._api_parser import parse_api_responses


def _unit(i: int, **extra: Any) -> dict[str, Any]:
    return {
        "id": str(100 + i),
        "baths": 1,
        "beds": 1,
        "model_id": "1x1",
        "rent": {"min": 1400, "max": 1450},
        "sqft": {"min": 700, "max": 700},
        **extra,
    }


def _parse(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return parse_api_responses(
        [{"url": "https://x.test/embedded", "body": {"units": units}}]
    )


def test_available_false_maps_unavailable() -> None:
    [u] = _parse([_unit(0, available=False)])
    assert u["availability_status"] == "UNAVAILABLE"


def test_available_true_maps_available() -> None:
    [u] = _parse([_unit(0, available=True)])
    assert u["availability_status"] == "AVAILABLE"


def test_bang_date_maps_available_and_backfills_date() -> None:
    [u] = _parse([_unit(0, available="!Date:2026-08-18T00:00:00.000Z")])
    assert u["availability_status"] == "AVAILABLE"
    assert u["availability_date"] == "2026-08-18"


def test_bang_date_does_not_clobber_explicit_avail_date() -> None:
    [u] = _parse([
        _unit(0, available="!Date:2026-08-18T00:00:00.000Z",
              availableDate="2026-07-20")
    ])
    assert u["availability_status"] == "AVAILABLE"
    assert u["availability_date"] == "2026-07-20"


def test_string_truthy_variants() -> None:
    [a, b] = _parse([_unit(0, available="true"), _unit(1, available="false")])
    assert a["availability_status"] == "AVAILABLE"
    assert b["availability_status"] == "UNAVAILABLE"


def test_absent_key_keeps_prior_empty_behaviour() -> None:
    [u] = _parse([_unit(0)])
    assert u["availability_status"] == ""


def test_explicit_status_still_wins() -> None:
    [u] = _parse([_unit(0, available=False, status="AVAILABLE")])
    assert u["availability_status"] == "AVAILABLE"


def test_available_count_fallback_unchanged() -> None:
    # count-key path (no boolean key) keeps its existing behaviour
    [u] = _parse([_unit(0, availableCount=2)])
    assert u["availability_status"] == "AVAILABLE"


def test_unparseable_string_stays_empty() -> None:
    [u] = _parse([_unit(0, available="maybe soon")])
    assert u["availability_status"] == ""
