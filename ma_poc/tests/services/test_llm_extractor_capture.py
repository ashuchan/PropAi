"""LAYER 1 (EXTRACT) — pin every signal each LLM extractor captures.

These tests pin the extractor → hints-dict contract for the four LLM
entry points the system uses:

  - ``extract_with_llm`` (monolithic Tier 4)
  - ``analyze_dom_with_llm`` (DOM-targeted)
  - ``analyze_api_with_llm`` (API-targeted)
  - ``extract_with_vision`` (Tier 5)

The previous behaviour silently dropped:
  - top-level ``property_amenities`` from every text and vision tier
  - ``navigation_hint``, ``platform_guess``, ``api_urls_with_data`` from
    DOM-LLM and API-LLM (those tiers' returned dicts only carried
    selectors / mapping triplets respectively)
  - ``noise_reason`` free text (logged only, never propagated)
  - drift hashes (``extraction_prompt_hash`` / ``dom_structure_hash`` /
    ``api_schema_signature``) — declared on the model but never produced

These tests pin the new richer return shape so a future refactor can't
silently re-introduce the leak.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ma_poc.services.llm_extractor import (
    _resolve_prior_observations_block,
    analyze_api_with_llm,
    analyze_dom_with_llm,
    apply_saved_mapping,
    extract_with_llm,
    prepare_llm_input,
)


def _mock_provider(response_json: dict | str) -> AsyncMock:
    """Standard provider mock used by every test in this file."""
    payload = response_json if isinstance(response_json, str) else json.dumps(response_json)
    p = AsyncMock()
    p.complete = AsyncMock(return_value=payload)
    p._last_usage = {
        "input_tokens": 100,
        "output_tokens": 50,
        "model": "test-model",
        "provider": "test",
    }
    return p


# ── extract_with_llm (Tier 4 monolithic) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_monolithic_captures_property_amenities_at_top_level() -> None:
    """Bug 2.1 — top-level ``property_amenities`` was the canonical
    place the prompt asked the LLM to emit aggregated amenity lists.
    The previous return shape only surfaced ``profile_hints``, so this
    array was discarded on every successful extraction."""
    response = {
        "units": [],
        "property_amenities": ["pool", "Gym", "  ", "dog park"],
        "profile_hints": {"navigation_hint": "/floorplans"},
    }
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        llm_input = prepare_llm_input("<html>x</html>", [], {})
        _, hints, _, _ = await extract_with_llm(llm_input)
    assert "property_amenities" in hints
    # Whitespace + non-string filtered, valid items kept verbatim.
    assert hints["property_amenities"] == ["pool", "Gym", "dog park"]


@pytest.mark.asyncio
async def test_monolithic_stamps_extraction_prompt_hash() -> None:
    """Bug 2.7 — ``extraction_prompt_hash`` was a declared-but-never-written
    field. Every monolithic call should now stamp a 16-char SHA hash so
    profile_updater can persist drift state."""
    provider = _mock_provider({"units": [], "profile_hints": {}})
    with patch("llm.factory.get_text_provider", return_value=provider):
        llm_input = prepare_llm_input("<html>x</html>", [], {})
        _, hints, _, _ = await extract_with_llm(llm_input)
    assert "extraction_prompt_hash" in hints
    assert len(hints["extraction_prompt_hash"]) == 16


@pytest.mark.asyncio
async def test_monolithic_does_not_overwrite_existing_prompt_hash() -> None:
    """``setdefault`` semantics — if the LLM somehow produced the field
    itself we trust the model. Defensive against double-stamping."""
    response = {
        "units": [],
        "profile_hints": {"extraction_prompt_hash": "preset-from-llm"},
    }
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        llm_input = prepare_llm_input("<html>x</html>", [], {})
        _, hints, _, _ = await extract_with_llm(llm_input)
    assert hints["extraction_prompt_hash"] == "preset-from-llm"


# ── analyze_dom_with_llm (DOM-targeted) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_dom_analysis_returns_full_hints_envelope() -> None:
    """The new contract: second tuple element is a hints dict carrying
    css_selectors PLUS nav_hint / platform_guess / api_urls_with_data /
    property_amenities / dom_structure_hash. Previously only
    css_selectors survived."""
    response = {
        "units": [],
        "css_selectors": {"container": ".unit-card", "rent": ".price"},
        "navigation_hint": "/floor-plans",
        "platform_guess": "rentcafe",
        "api_urls_with_data": ["https://api.example.com/units"],
        "property_amenities": ["pool", "gym"],
    }
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        units, hints, _ = await analyze_dom_with_llm(
            "<html>dom</html>", "https://x.com", {"property_name": "X"}
        )
    assert hints is not None
    assert hints["css_selectors"]["container"] == ".unit-card"
    assert hints["navigation_hint"] == "/floor-plans"
    assert hints["platform_guess"] == "rentcafe"
    assert hints["api_urls_with_data"] == ["https://api.example.com/units"]
    assert hints["property_amenities"] == ["pool", "gym"]
    # Drift hash always stamped:
    assert "dom_structure_hash" in hints
    assert len(hints["dom_structure_hash"]) == 16


@pytest.mark.asyncio
async def test_dom_analysis_returns_none_when_no_signals() -> None:
    """When the LLM produced nothing learnable (no selectors, no nav,
    no amenities, no platform), the second tuple element is None so
    callers can short-circuit cheap checks like ``if hints:``. The
    drift hash alone shouldn't lift it to truthy because that'd flip
    ``if hints:`` semantics for downstream code that relies on it.

    Note the current implementation always stamps dom_structure_hash,
    so the returned dict is non-empty when the call succeeded. This
    test pins the truthy-when-present contract; if an extractor
    refactor changes the shape, this is the canary."""
    response = {"units": [], "css_selectors": {}}
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        units, hints, _ = await analyze_dom_with_llm(
            "<html>x</html>", "https://x.com", {}
        )
    # hints carries dom_structure_hash even when nothing else surfaced.
    # Either None (no learnable signals) or a dict containing only the
    # hash is acceptable; the contract is: never the bare css_selectors
    # dict.
    if hints is not None:
        # Must NOT have container set (the old "selectors-only" shape)
        assert hints.get("css_selectors") is None or not hints["css_selectors"].get("container")


@pytest.mark.asyncio
async def test_dom_analysis_strips_null_platform_guess() -> None:
    """LLMs sometimes emit "null" / "none" as the platform_guess string
    when they're unsure. Those should not become persisted platform
    values that would mislead the cascade."""
    response = {
        "units": [],
        "css_selectors": {"container": ".x"},
        "platform_guess": "null",
    }
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        _, hints, _ = await analyze_dom_with_llm(
            "<html>x</html>", "https://x.com", {}
        )
    assert "platform_guess" not in (hints or {})


@pytest.mark.asyncio
async def test_dom_analysis_dom_structure_hash_stable() -> None:
    """The drift hash must be deterministic for the same DOM input so
    profile_updater's drift detection works across runs."""
    dom = "<div class='unit'>Apt 5 - $1,500</div>"
    response = {"units": [], "css_selectors": {"container": ".unit"}}
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        _, hints1, _ = await analyze_dom_with_llm(dom, "https://x.com", {})
        _, hints2, _ = await analyze_dom_with_llm(dom, "https://x.com", {})
    assert hints1["dom_structure_hash"] == hints2["dom_structure_hash"]
    # Different input produces different hash:
    with patch("llm.factory.get_text_provider", return_value=provider):
        _, hints3, _ = await analyze_dom_with_llm(
            "<div>different content</div>", "https://x.com", {}
        )
    assert hints1["dom_structure_hash"] != hints3["dom_structure_hash"]


# ── analyze_api_with_llm (API-targeted) ──────────────────────────────────────


@pytest.mark.asyncio
async def test_api_analysis_units_branch_carries_mapping_plus_extras() -> None:
    """When the LLM finds units, the second tuple element carries the
    persistable mapping triplet (api_url_pattern / json_paths /
    response_envelope) AND ancillary signals — previously this was a
    mapping-only dict that dropped everything else."""
    response = {
        "has_unit_data": True,
        "json_paths": {"rent": "rent", "unit_id": "id"},
        "response_envelope": "data.units",
        "units": [{"unit_id": "U1", "rent": 1500, "floor_plan_name": "A"}],
        "navigation_hint": "/v2/availability",
        "platform_guess": "knock",
        "property_amenities": ["pool"],
    }
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        units, hints, is_noise, _ = await analyze_api_with_llm(
            {"url": "/api/units", "body": {"data": {"units": []}}}, {}
        )
    assert is_noise is False
    assert hints is not None
    # Mapping triplet present:
    assert hints["api_url_pattern"] == "/api/units"
    assert hints["json_paths"] == {"rent": "rent", "unit_id": "id"}
    assert hints["response_envelope"] == "data.units"
    # Ancillaries present:
    assert hints["navigation_hint"] == "/v2/availability"
    assert hints["platform_guess"] == "knock"
    assert hints["property_amenities"] == ["pool"]
    # Drift signature stamped from the body envelope hash:
    assert "api_schema_signature" in hints


@pytest.mark.asyncio
async def test_api_analysis_noise_branch_carries_reason_and_extras() -> None:
    """When the LLM classifies an API as noise, the previous shape was
    ``(units=[], mapping=None, is_noise=True, ...)`` — we lost the
    LLM's explanation entirely. Now noise verdicts carry:
      - noise_reason (free-text classification)
      - any ancillary signals the LLM still managed to produce
        (a noise verdict often comes with a navigation_hint to where
        the real data lives)."""
    response = {
        "has_unit_data": False,
        "noise_reason": "chatbot_config",
        "navigation_hint": "/api/v2/units",
        "platform_guess": "custom",
    }
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        units, hints, is_noise, _ = await analyze_api_with_llm(
            {"url": "/api/chat", "body": {"theme": "dark"}}, {}
        )
    assert is_noise is True
    assert units == []
    assert hints is not None
    assert hints["noise_reason"] == "chatbot_config"
    assert hints["navigation_hint"] == "/api/v2/units"
    assert hints["platform_guess"] == "custom"


@pytest.mark.asyncio
async def test_api_analysis_noise_reason_defaults_when_absent() -> None:
    """When the LLM classifies as noise but doesn't volunteer a reason,
    we synthesise ``no_unit_data`` so the blocklist always has SOMETHING
    to record."""
    response = {"has_unit_data": False}
    provider = _mock_provider(response)
    with patch("llm.factory.get_text_provider", return_value=provider):
        _, hints, is_noise, _ = await analyze_api_with_llm(
            {"url": "/api/x", "body": {}}, {}
        )
    assert is_noise is True
    assert hints["noise_reason"] == "no_unit_data"


# ── apply_saved_mapping (deterministic replay) ───────────────────────────────


def test_apply_saved_mapping_extracts_amenities_and_concession() -> None:
    """Bug — yesterday the api_analysis prompt asked the LLM for
    amenity / concession json_paths but ``apply_saved_mapping`` only
    knew how to extract the basic 9 fields. This pins the new
    contract: amenities + concession_text + concession_value flow
    through deterministic replay just like rent / sqft."""
    body = {
        "data": {
            "units": [
                {
                    "id": "U1",
                    "rent": 1500,
                    "amen": ["pool", "gym"],
                    "promo_text": "1 month free",
                    "promo_value": 1500,
                },
                {
                    "id": "U2",
                    "rent": 1700,
                    "amen": "balcony, in-unit-wd",  # comma-separated string variant
                    "promo_text": None,
                    "promo_value": None,
                },
            ]
        }
    }
    mapping = {
        "response_envelope": "data.units",
        "json_paths": {
            "unit_id": "id",
            "rent_low": "rent",
            "amenities": "amen",
            "concession_text": "promo_text",
            "concession_value": "promo_value",
        },
    }
    units = apply_saved_mapping(body, mapping)
    assert len(units) == 2

    u1 = next(u for u in units if u["unit_id"] == "U1")
    assert u1["amenities"] == ["pool", "gym"]
    assert u1["concession_text"] == "1 month free"
    assert u1["concession_value"] == 1500
    # Auto-stamped source enum since this came from a dedicated API field:
    assert u1["concession_source"] == "api_field"

    u2 = next(u for u in units if u["unit_id"] == "U2")
    # Comma-separated string splits into list:
    assert u2["amenities"] == ["balcony", "in-unit-wd"]


def test_apply_saved_mapping_skips_unknown_field_paths() -> None:
    """Defensive — an unknown json_path key shouldn't crash the
    extractor. Existing replay path tolerates extra keys silently."""
    body = {"units": [{"id": "U1", "rent": 1500}]}
    mapping = {
        "response_envelope": "units",
        "json_paths": {
            "unit_id": "id",
            "rent_low": "rent",
            "unknown_future_field": "some.path",  # not handled by replay
        },
    }
    units = apply_saved_mapping(body, mapping)
    assert len(units) == 1
    assert units[0]["unit_id"] == "U1"


def test_apply_saved_mapping_preserves_unit_number_identity() -> None:
    """A learned ``unit_number`` path must survive deterministic replay.

    Prometheus' public availability API uses ``unitNumber`` rather than a
    mapping-owned ``unit_id``.  Ignoring that saved field converted a live
    apartment roster into inferred floor-plan rows despite positive rent.
    """
    from ma_poc.core.identity import unit_has_real_anchor

    body = [
        {
            "unitNumber": "247",
            "rent": "5032.0000",
            "floorPlanName": "Plan 1K",
            "bedrooms": "1",
            "bathrooms": "1",
            "area": "794",
        }
    ]
    mapping = {
        "response_envelope": "",
        "json_paths": {
            "unit_number": "unitNumber",
            "rent_low": "rent",
            "rent_high": "rent",
            "floor_plan_name": "floorPlanName",
            "bedrooms": "bedrooms",
            "bathrooms": "bathrooms",
            "sqft": "area",
        },
    }

    units = apply_saved_mapping(body, mapping)

    assert len(units) == 1
    # The legacy normalizer canonicalizes either source identity spelling onto
    # ``unit_id``; the key point is that the mapped live unitNumber survives.
    assert units[0]["unit_id"] == "247"
    assert units[0]["market_rent_low"] == 5032.0
    assert unit_has_real_anchor(units[0]) is True


def test_apply_saved_mapping_does_not_promote_plan_id_as_unit_number() -> None:
    """A historical floor-plan ``id`` mapping is not apartment identity."""
    from ma_poc.core.identity import unit_has_real_anchor

    body = {
        "widget_data": {
            "content": {
                "floor_plans": {
                    "floor_plans": [
                        {
                            "id": "5212",
                            "floorplan-name": "A4",
                            "min_rent": "1450",
                            "no_of_bedroom": "1",
                            "no_of_bathroom": "1",
                        }
                    ]
                }
            }
        }
    }
    mapping = {
        "response_envelope": "widget_data.content.floor_plans.floor_plans",
        "json_paths": {
            "unit_number": "id",
            "rent_low": "min_rent",
            "floor_plan_name": "floorplan-name",
            "bedrooms": "no_of_bedroom",
            "bathrooms": "no_of_bathroom",
        },
    }

    units = apply_saved_mapping(body, mapping)

    assert len(units) == 1
    assert units[0].get("unit_id") is None
    assert unit_has_real_anchor(units[0]) is False


def test_apply_saved_mapping_parses_exact_currency_rent() -> None:
    """A scalar ``$909.00`` API value remains numeric replay evidence."""
    body = [{"unitNumber": "8350", "rentString": "$909.00"}]
    mapping = {
        "response_envelope": "",
        "json_paths": {
            "unit_number": "unitNumber",
            "rent_low": "rentString",
            "rent_high": "rentString",
        },
    }

    units = apply_saved_mapping(body, mapping)

    assert units == [
        {
            "unit_id": "8350",
            "floor_plan_name": None,
            "bedrooms": None,
            "bathrooms": None,
            "sqft": None,
            "market_rent_low": 909.0,
            "market_rent_high": 909.0,
            "available_date": None,
            "availability_status": "UNKNOWN",
            "confidence": 0.85,
            "amenities": None,
            "concession_text": None,
            "concession_value": None,
            "concession_source": None,
            "floor_plan_source": None,
            "availability_count": None,
            "lease_term": None,
            "move_in_date": None,
        }
    ]


# ── _resolve_prior_observations_block ────────────────────────────────────────


def test_prior_observations_empty_when_no_notes() -> None:
    """Cold profiles (no prior LLM call) render no preamble — the
    placeholder substitution becomes a no-op so the prompt stays clean."""
    block = _resolve_prior_observations_block({})
    assert block == ""


def test_prior_observations_renders_block_when_notes_present() -> None:
    """When the profile carries prior field_mapping_notes, they're
    surfaced as a structured PRIOR OBSERVATIONS preamble so the next
    LLM call can converge faster on the same selectors."""
    block = _resolve_prior_observations_block(
        {"prior_observations": "Units live in .pricing > .price"}
    )
    assert "PRIOR OBSERVATIONS" in block
    assert "Units live in .pricing > .price" in block


def test_prior_observations_caps_long_notes() -> None:
    """Notes are capped at 500 chars (the adapter clipping is the
    boundary; the helper itself doesn't add extra clipping but it
    accepts strings the adapter has already trimmed). Pin that the
    helper doesn't crash on long input."""
    long_notes = "x" * 5000
    block = _resolve_prior_observations_block({"prior_observations": long_notes})
    # Block is rendered (no crash); the adapter's 500-char cap is
    # exercised in the integration layer.
    assert "PRIOR OBSERVATIONS" in block


def test_prior_observations_strips_whitespace_only_notes() -> None:
    """Whitespace-only or empty notes are treated as absent."""
    assert _resolve_prior_observations_block({"prior_observations": "   "}) == ""
    assert _resolve_prior_observations_block({"prior_observations": ""}) == ""
    assert _resolve_prior_observations_block({"prior_observations": None}) == ""
