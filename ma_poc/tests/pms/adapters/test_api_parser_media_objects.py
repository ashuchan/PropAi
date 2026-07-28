"""CMS media-library descriptors must never become units.

Defect (run-2026-07-27-full-0d54ca7, 2026-07-28)
------------------------------------------------
Three unrelated properties — Sync at Harmony (63482, shard_1), Sync at Vinings
(7726, shard_36) and Uptown Fayetteville (78990, shard_45) — each shipped
exactly one phantom unit on TIER_1_5_EMBEDDED::

    floor_plan_name = "2023 epIQ Top 100 Badge.png"
    area = 8714      (area_raw "8714")
    beds/baths/rent/unit_name/availability = NULL

Those were 3 of only 5 rows in the whole 104,964-row run at or above 4,000 sqft.

``8714`` is the badge PNG's size in BYTES — live-verified with a plain
unauthenticated GET::

    GET https://images.myrazz.com/uc-image/
        51e2c3b7-6dbd-4388-995a-200656df50a6/2023_epiq_top_100_badge.png
    -> 200, Content-Type: image/png, Content-Length: 8714

It is identical on all three sites because all three are Razz-built and share
the one CMS asset. A media-library XHR returns file descriptors
``{name, size, mimeType, url}``; ``size`` sits in the sqft alias chain of
``parse_api_responses`` and ``name`` in the floor-plan-name chain, so every
image in the media library became a "floor plan" whose "square footage" was
its file size in bytes. ``_LIST_KEYS`` contains the generic ``"items"`` /
``"results"``, so the media response was admitted with no per-item signal gate.

These tests are pure-function — no network.
"""

from __future__ import annotations

import pytest

from ma_poc.pms.adapters._api_parser import (
    _MEDIA_FILE_EXT_RE,
    _has_dwelling_evidence,
    _is_media_file_item,
    _item_has_unit_signals,
    parse_api_responses,
)

# The exact shape the run's media XHR carried. The badge entry is the row that
# shipped as "8714 sqft" on all three properties.
_MEDIA_XHR = [
    {
        "url": "https://api.myrazz.com/v1/site/63482/media",
        "body": {
            "items": [
                {
                    "uuid": "51e2c3b7-6dbd-4388-995a-200656df50a6",
                    "name": "2023 epIQ Top 100 Badge.png",
                    "size": 8714,
                    "mimeType": "image/png",
                    "url": (
                        "https://images.myrazz.com/uc-image/"
                        "51e2c3b7-6dbd-4388-995a-200656df50a6/"
                        "2023_epiq_top_100_badge.png"
                    ),
                },
                {
                    "uuid": "fb54a923-8019-4ed6-be26-c39d638d3fdb",
                    "name": "footer.jpg",
                    "size": 214499,
                    "mimeType": "image/jpeg",
                    "url": "https://images.myrazz.com/uc-image/fb54a923/footer.jpg",
                },
            ]
        },
    }
]


def test_media_library_xhr_emits_no_units() -> None:
    """The regression pin: a media XHR must yield zero units.

    Before the fix this returned two rows, the first of which was exactly the
    production defect: floor_plan_name="2023 epIQ Top 100 Badge.png",
    sqft="8714".
    """
    units = parse_api_responses(_MEDIA_XHR)
    assert units == [], (
        "media-library descriptors leaked into units: "
        + repr([(u.get("floor_plan_name"), u.get("sqft")) for u in units])
    )


def test_badge_file_size_is_not_emitted_as_sqft() -> None:
    """Explicitly pin the production value so it can never come back."""
    units = parse_api_responses(_MEDIA_XHR)
    assert not [u for u in units if str(u.get("sqft")) == "8714"]
    assert not [
        u
        for u in units
        if str(u.get("floor_plan_name", "")).endswith(".png")
    ]


# ── The media discriminator, table-tested ──────────────────────────────────
# Rule 3 of the task brief: a regex fix must be table-tested against the cases
# it must NOT match. Over-broad matching here would delete real floor plans.

_EXT_MUST_MATCH = [
    "2023 epIQ Top 100 Badge.png",   # the production defect
    "footer.jpg",
    "hero.jpeg",
    "amenity-tour.MP4",              # case-insensitive
    "brochure.pdf",
    "site-plan.SVG",
    "banner.webp ",                  # trailing whitespace
    "floorplan-a1.PNG",
    "walkthrough.mov",
    "residences.xlsx",
]

_EXT_MUST_NOT_MATCH = [
    "A1",                    # canonical plan code
    "St. Ives",              # dot followed by a space + word
    "Bldg. A",               # ditto
    "Loft 1.5",              # decimal, not an extension
    "The Ico",               # contains "ico" but no dot
    "2 Bed / 2 Bath",        # bed/bath label
    "Penthouse.Deluxe",      # dotted name, extension not in the table
    "Studio",
    "Villa D'Oro",
    "Unit 3.2",              # decimal unit number
    "1.5",
    "The Webp Collection",   # extension word, but not as an extension
    "Magnolia",
    "B2 Premier",            # a real plan name from PID 7726
]


@pytest.mark.parametrize("value", _EXT_MUST_MATCH)
def test_media_extension_regex_matches_filenames(value: str) -> None:
    assert _MEDIA_FILE_EXT_RE.search(value), f"should match: {value!r}"


@pytest.mark.parametrize("value", _EXT_MUST_NOT_MATCH)
def test_media_extension_regex_rejects_plan_names(value: str) -> None:
    assert not _MEDIA_FILE_EXT_RE.search(value), f"must NOT match: {value!r}"


# ── The second lock: dwelling evidence always wins ─────────────────────────


def test_floor_plan_with_image_name_but_real_facts_is_kept() -> None:
    """A plan whose name field is a filename is still a plan when it carries
    dwelling facts. The guard requires BOTH a media discriminator AND zero
    dwelling evidence, so this row must survive."""
    item = {
        "name": "a1-floorplan.png",
        "sqft": 750,
        "bedrooms": 1,
        "bathrooms": 1,
        "rent": 1450,
    }
    assert _has_dwelling_evidence(item) is True
    assert _is_media_file_item(item) is False


def test_unit_with_mime_typed_thumbnail_field_is_kept() -> None:
    """An item carrying a ``mimeType`` alongside real unit facts is a unit."""
    item = {
        "unitNumber": "1101",
        "mimeType": "image/png",
        "rent": 2175,
        "sqft": 1434,
    }
    assert _is_media_file_item(item) is False


def test_pure_media_descriptor_is_rejected() -> None:
    item = {
        "uuid": "51e2c3b7-6dbd-4388-995a-200656df50a6",
        "name": "2023 epIQ Top 100 Badge.png",
        "size": 8714,
        "mimeType": "image/png",
    }
    assert _has_dwelling_evidence(item) is False
    assert _is_media_file_item(item) is True


def test_size_alone_is_not_dwelling_evidence() -> None:
    """``size`` is the overloaded key at the heart of the defect (bytes on a
    file object, sqft on a plan). It must not count as evidence of a dwelling
    or the guard would never fire."""
    assert _has_dwelling_evidence({"size": 8714}) is False
    assert _has_dwelling_evidence({"sqft": 8714}) is True


def test_media_item_never_passes_the_unit_signal_walker() -> None:
    """Defence in depth: the recursive walker must not select a media array.

    ``floorPlanName`` normalises to the canonical ``floor_plan_name`` signal,
    so without the guard this media descriptor scores 1 and a walker invoked
    with ``min_signals=1`` would adopt the whole media array as inventory.
    ``floor_plan_name`` is deliberately absent from the dwelling-evidence set
    (a filename is exactly what gets misread), so the guard still fires.
    """
    media = {
        "floorPlanName": "2023 epIQ Top 100 Badge.png",
        "mimeType": "image/png",
        "size": 8714,
    }
    assert _is_media_file_item(media) is True
    assert _item_has_unit_signals(media, min_signals=1) is False
    # A genuine plan with the same single signal is still adopted.
    assert _item_has_unit_signals({"floorPlanName": "B2 Premier"}, min_signals=1)


def test_real_units_still_parse_alongside_a_media_response() -> None:
    """Corpus guard: the fix must not cost a single real row.

    Mirrors the real Sync at Harmony shape — the unit list and the media list
    arriving as two separate captured responses.
    """
    resp = [
        {
            "url": "https://api.example.com/units",
            "body": {
                "units": [
                    {
                        "unitNumber": "1101",
                        "floorPlanName": "C1",
                        "bedrooms": 3,
                        "bathrooms": 2,
                        "sqft": 1434,
                        "rent": 2175,
                    },
                    {
                        "unitNumber": "1104",
                        "floorPlanName": "A3",
                        "bedrooms": 1,
                        "bathrooms": 1,
                        "sqft": 705,
                        "rent": 1355,
                    },
                ]
            },
        },
        _MEDIA_XHR[0],
    ]
    units = parse_api_responses(resp)
    assert len(units) == 2
    assert {u["unit_number"] for u in units} == {"1101", "1104"}
    assert {u["sqft"] for u in units} == {"1434", "705"}
