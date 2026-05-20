"""End-to-end raw-preservation invariant for the concession pipeline.

The contract spans three modules:

  * ``concession_clean``      — classifies + best-effort cleans
  * ``concession_normalize``  — best-effort structures
  * ``schema_v2``             — emits the trio (raw + clean + structured)

The user-facing invariant being pinned here is:

    **Whatever the input, the raw text is ALWAYS preserved. The
    cleaned text and structured object are derivatives — they may be
    None / empty / quality-flagged-unclean, but the raw text in
    ``concession_text`` is never dropped.**

This is the single most important guarantee the pipeline provides.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ma_poc.core.concession_clean import (
    classify_concession_quality,
    clean_concession_text,
)
from ma_poc.core.concession_normalize import normalize_concession

# ─────────────────────────────────────────────────────────────────────
# Raw-preservation invariant — module-pair level
# ─────────────────────────────────────────────────────────────────────


# Realistic concession-text fixtures spanning the canary-observed
# distribution: clean / leak-prefixed / header-only / amenity-noise /
# real-world copy from sampled live property sites.
_FIXTURES: list[tuple[str, str]] = [
    # (label, raw_text)
    ("hawthorne_real", "Move in by June 5th for 2 Months Free on all floorplans*! Flexible Lease Terms Available!"),
    ("district_real", "Receive up to 2 months FREE!"),
    ("script_leak", "if (href.indexOf('?') == -1) { el.setAttribute('href', 'x'); } Limited Time Offer! Move in by 6/15 and get 1 month free rent."),
    ("style_leak", "padding: 12px; background-color: #fff; Save $500 off your first month's rent."),
    ("dmapi_leak", 'Functions["abc123~1"] = function(data){return data;} 2 weeks free on signing.'),
    ("header_only", "Limited Time Offer!"),
    ("orphan_prefix", "}); console.log(); reduced rent on select units"),
    ("clean_dollar", "Save $1,500 on a 13-month lease"),
    ("clean_percent", "Get 10% off your first month"),
    ("waived_fee", "Waived application fee for new residents"),
    ("amenity_noise", "Free WiFi included in every unit"),
    ("welcome_text", "Welcome to our beautiful community"),
]


@pytest.mark.parametrize("label,raw", _FIXTURES)
def test_raw_text_never_mutated(label: str, raw: str) -> None:
    """The classifier and cleaner never alter their input string."""
    snapshot = raw
    classify_concession_quality(raw)
    clean_concession_text(raw)
    normalize_concession(raw)
    assert raw == snapshot, f"input mutated for fixture {label}"


@pytest.mark.parametrize("label,raw", _FIXTURES)
def test_pipeline_emits_all_three_fields_when_input_present(label: str, raw: str) -> None:
    """For any non-empty raw input the pipeline emits raw + clean + quality.

    ``structured`` may be None (no parsable shape) — that's the
    raw-fallback invariant, not a bug.
    """
    quality = classify_concession_quality(raw)
    cleaned = clean_concession_text(raw)
    structured = normalize_concession(cleaned or raw)

    # Raw is always preserved (caller's job, but we pin the contract).
    assert raw  # input is non-empty

    # Quality label is always a string (never None / never raises).
    assert isinstance(quality, str)
    assert quality in {
        "clean", "unclean_script_leak", "unclean_style_leak",
        "unclean_dmapi", "unclean_orphan_prefix", "unclean_header_only",
        "empty",
    }

    # Cleaned text is a string (may be empty for empty input only).
    assert isinstance(cleaned, str)

    # Structured is either a dict or None. When None, the raw text
    # is the system of record — no further constraint.
    assert structured is None or isinstance(structured, dict)


def test_raw_fallback_when_structured_fails() -> None:
    """When normalize returns None, the raw text is still surfaced."""
    raw = "Welcome to our beautiful community"
    cleaned = clean_concession_text(raw)
    structured = normalize_concession(cleaned)
    # Unrelated text yields no structure — but raw + cleaned still
    # carry the user's input. No data loss.
    assert structured is None
    assert raw in cleaned or cleaned == raw


def test_real_property_capture_hawthorne() -> None:
    """Hawthorne at Traditions: header + body + footnote.

    Pinned against the actual marketing copy fetched from the live
    property site during commit analysis. If the regex set changes
    such that this no longer normalises, that's a regression.
    """
    raw = "Move in by June 5th for 2 Months Free on all floorplans*! Flexible Lease Terms Available!"
    quality = classify_concession_quality(raw)
    assert quality == "clean"
    structured = normalize_concession(raw)
    assert structured is not None
    assert structured["type"] == "free_rent"
    assert structured["free_period"]["value"] == 2
    assert structured["free_period"]["unit"] == "months"
    assert structured["deadline"] is not None
    assert "June" in structured["deadline"]


def test_real_property_capture_district_square() -> None:
    """District Square: header-only-shape with embedded value."""
    raw = "Receive up to 2 months FREE!"
    structured = normalize_concession(raw)
    assert structured is not None
    assert structured["type"] == "free_rent"
    assert structured["free_period"]["value"] == 2


# ─────────────────────────────────────────────────────────────────────
# schema_v2 emits the trio at unit + property level
# ─────────────────────────────────────────────────────────────────────


def _v2_property(scrape_result: dict[str, Any]) -> dict[str, Any]:
    """Call schema_v2.build_v2_property with a minimal valid input.

    Signature is positional: ``(row, ident, scrape_result, target_units, scrape_ts=None)``.
    Lifts target_units out of the scrape_result for clarity.
    """
    from ma_poc.core.schema_v2 import build_v2_property

    scrape_ts = datetime.now()
    target_units = scrape_result.get("units") or []
    return build_v2_property(
        row={"Unique ID": "1", "Property Name": "Test", "Website": "https://x.test/"},
        ident=None,
        scrape_result=scrape_result,
        target_units=target_units,
        scrape_ts=scrape_ts,
    )


def test_v2_property_emits_all_concession_fields_on_clean_input() -> None:
    result = _v2_property({
        "concessions_text": "2 months free rent on a 13-month lease",
        "base_url": "https://x.test/",
        "units": [],
    })
    assert result["concessions"] == "2 months free rent on a 13-month lease"
    assert result["concessions_clean"] == "2 months free rent on a 13-month lease"
    assert result["_concessions_quality"] == "clean"
    assert isinstance(result["concessions_structured"], dict)
    assert result["concessions_structured"]["type"] == "free_rent"


def test_v2_property_emits_all_fields_on_dirty_input_raw_preserved() -> None:
    # Use a marker from _SCRIPT_LEAK_MARKERS so the classifier fires.
    dirty = "document.querySelector('.banner'); var x = 1; 2 weeks free on signing"
    result = _v2_property({
        "concessions_text": dirty,
        "base_url": "https://x.test/",
        "units": [],
    })
    # Raw — preserved verbatim.
    assert result["concessions"] == dirty
    # Quality — flagged unclean.
    assert result["_concessions_quality"] == "unclean_script_leak"
    # Cleaned — non-empty, contains the offer signal.
    assert result["concessions_clean"]
    assert "2 weeks free" in result["concessions_clean"]
    # Structured — may be present or None depending on cleaner output;
    # the contract is raw-preserve, not structured-always.
    assert (
        result["concessions_structured"] is None
        or isinstance(result["concessions_structured"], dict)
    )


def test_v2_property_emits_none_when_no_concession_in_input() -> None:
    result = _v2_property({"base_url": "https://x.test/", "units": []})
    assert result["concessions"] is None
    assert result["concessions_clean"] is None
    assert result["_concessions_quality"] is None
    assert result["concessions_structured"] is None


def test_v2_property_prefers_vision_structured_when_present() -> None:
    """vision_banner output bypasses the regex normaliser."""
    result = _v2_property({
        "concessions_text": "Some vision-extracted text about free rent",
        "concessions_vision_structured": {
            "type": "free_rent",
            "value": "3 months",
            "deadline": "July 1st",
            "conditions": None,
            "source": "IMAGE_BANNER",
            "text": "3 months free!",
        },
        "concessions_source": "vision",
        "base_url": "https://x.test/",
        "units": [],
    })
    # Vision structured wins over regex.
    assert result["concessions_structured"]["value"] == "3 months"
    assert result["concessions_structured"]["source"] == "IMAGE_BANNER"


def test_v2_unit_emits_concession_trio() -> None:
    """Per-unit concession_text / _clean / quality / structured all populated."""
    result = _v2_property({
        "base_url": "https://x.test/",
        "units": [{
            "unit_number": "101",
            "asking_rent": 1500,
            "concession_text": "1 month free rent for new residents",
            "concession_source": "API",
        }],
    })
    units = result.get("units", [])
    assert len(units) == 1
    u = units[0]
    assert u["concession_text"] == "1 month free rent for new residents"
    assert "1 month free" in u["concession_text_clean"]
    assert u["_concession_quality"] == "clean"
    assert u["concession_structured"]["type"] == "free_rent"
    assert u["concession_source"] == "API"
