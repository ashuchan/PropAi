"""Tests for F2 — LLM rescue service (ma_poc/services/llm_api_rescue.py).

All LLM calls are mocked — no real network calls.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ma_poc.services.llm_api_rescue import (
    MAX_LLM_CALLS_PER_PROPERTY,
    RescueInput,
    RescueOutput,
    _build_prompt,
    _filter_candidates,
    _is_noise_url,
    _noise_blocklist_enabled,
    _rank_candidates,
    _tier_label_for,
    _trim_body,
    _url_to_pattern,
    rescue_from_api_responses,
)

FIXTURES = Path(__file__).parent / "fixtures" / "llm_rescue"

# ── Helpers ───────────────────────────────────────────────────────────────────


def _inp(**kw: Any) -> RescueInput:
    defaults: dict[str, Any] = {
        "property_id": "TEST-001",
        "property_context": {
            "name": "Test Property",
            "website": "https://test.com",
            "city": "Austin",
            "expected_units": 50,
        },
        "source_adapter": "generic",
        "api_responses": [
            {
                "url": "https://test.com/api/units",
                "body": {"units": [{"beds": 1, "rent": 1200}]},
                "content_type": "application/json",
            }
        ],
        "profile_snapshot": None,
    }
    defaults.update(kw)
    return RescueInput(**defaults)


def _good_llm_response() -> dict:
    return {
        "units": [
            {
                "unit_id": "101",
                "floor_plan_name": "1BR",
                "beds": 1,
                "baths": 1.0,
                "area": 750,
                "rent_low": 1200,
                "rent_high": 1200,
                "available_date": None,
                "lease_term": None,
                "move_in_date": None,
            },
        ],
        "winning_url": "https://test.com/api/units",
        "envelope": "$.units",
        "json_paths": {"unit_id": "unit_id", "beds": "beds", "rent_low": "rent"},
        "confidence": 0.9,
    }


# ── rescue_from_api_responses — guard conditions ──────────────────────────────


@pytest.mark.asyncio
async def test_rescue_returns_empty_when_no_api_responses() -> None:
    result = await rescue_from_api_responses(_inp(api_responses=[]))
    assert result.units == []
    assert any("no api_responses" in e for e in result.errors)


@pytest.mark.asyncio
async def test_rescue_returns_empty_when_unsupported_adapter() -> None:
    result = await rescue_from_api_responses(_inp(source_adapter="rentcafe"))
    assert result.units == []
    assert any("unsupported" in e for e in result.errors)


# ── Filter ────────────────────────────────────────────────────────────────────


def test_rescue_filter_drops_blocked_endpoints_from_profile() -> None:
    profile = {"api_hints": {"blocked_endpoints": [{"url_pattern": "/analytics/pixel"}]}}
    responses = [
        {"url": "https://site.com/analytics/pixel", "body": {"x": 1}},
        {"url": "https://site.com/api/units", "body": {"units": []}},
    ]
    kept = _filter_candidates(responses, profile)
    assert all("/analytics/pixel" not in r["url"] for r in kept)


def test_rescue_filter_drops_foreign_host_responses() -> None:
    # An empty body is dropped
    responses = [{"url": "https://maps.google.com/api", "body": None}]
    kept = _filter_candidates(responses, None)
    assert kept == []


def test_rescue_filter_keeps_known_pms_hosts() -> None:
    responses = [{"url": "https://entrata.com/api/units", "body": {"units": [{"id": 1}]}}]
    kept = _filter_candidates(responses, None)
    assert len(kept) == 1


# ── F1.1 — static noise blocklist ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://klaviyo.com/api/track",
        "https://static-forms.klaviyo.com/x",
        "https://www.googletagmanager.com/gtag/js",
        "https://places.googleapis.com/$rpc/Places/Search",
        "https://challenges.cloudflare.com/cdn-cgi/challenge",
        "https://hcaptcha.com/captcha",
        "https://cmp.osano.com/cmp.js",
        "https://app.meetelise.com/widget",
        "https://example.com/Apartments/module/widgets/property_info/",
        "https://example.com/Apartments/module/application_authentication/",
        "https://example.com/forms/api/submit",
        "https://example.com/popdown/promo",
        "https://example.com/realms/foliospace/protocol/openid-connect/auth",
    ],
)
def test_f1_1_noise_blocklist_drops_known_noise_hosts(url: str) -> None:
    """F1.1: every audited noise host/path must be classified as noise."""
    assert _is_noise_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://entrata.com/api/units",
        "https://acme.appfolio.com/api/v1/community_info/",
        "https://1234.onlineleasing.realpage.com/listings",
        "https://yardi-host.com/RentCafe/availability",
        "https://www.example.com/floorplans",
    ],
)
def test_f1_1_noise_blocklist_keeps_real_data_hosts(url: str) -> None:
    """F1.1: no PMS data host or general property URL is misclassified as noise."""
    assert _is_noise_url(url) is False


def test_f1_1_filter_drops_static_noise_when_flag_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_RESCUE_NOISE_BLOCKLIST", "true")
    responses = [
        {"url": "https://klaviyo.com/api/track", "body": {"x": 1}},
        {"url": "https://example.com/api/units", "body": {"units": [{"id": 1}]}},
    ]
    kept = _filter_candidates(responses, None)
    assert [r["url"] for r in kept] == ["https://example.com/api/units"]


def test_f1_1_filter_keeps_static_noise_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1.1: when ENABLE_RESCUE_NOISE_BLOCKLIST=false, the blocklist is skipped
    entirely. Real-data filtering still applies (non-JSON bodies dropped)."""
    monkeypatch.setenv("ENABLE_RESCUE_NOISE_BLOCKLIST", "false")
    responses = [
        {"url": "https://klaviyo.com/api/track", "body": {"x": 1}},
        {"url": "https://example.com/api/units", "body": {"units": [{"id": 1}]}},
    ]
    kept = _filter_candidates(responses, None)
    # Both kept — flag-off bypasses _is_noise_url
    assert {r["url"] for r in kept} == {
        "https://klaviyo.com/api/track",
        "https://example.com/api/units",
    }


def test_f1_1_blocklist_default_is_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default behaviour: when env var is unset, blocklist is enabled."""
    monkeypatch.delenv("ENABLE_RESCUE_NOISE_BLOCKLIST", raising=False)
    assert _noise_blocklist_enabled() is True


def test_f1_1_blocklist_flag_accepts_truthy_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for val in ("false", "FALSE", "0", "no", "off"):
        monkeypatch.setenv("ENABLE_RESCUE_NOISE_BLOCKLIST", val)
        assert _noise_blocklist_enabled() is False, val
    for val in ("true", "1", "yes", "on", "anything-else"):
        monkeypatch.setenv("ENABLE_RESCUE_NOISE_BLOCKLIST", val)
        assert _noise_blocklist_enabled() is True, val


def test_f1_1_subdomain_match_works() -> None:
    """``static-forms.klaviyo.com`` should match the ``klaviyo.com`` registered host."""
    assert _is_noise_url("https://static-forms.klaviyo.com/x") is True
    assert _is_noise_url("https://api.subdomain.googletagmanager.com/x") is True


# ── F1.2 — captcha_detected gate ──────────────────────────────────────────────


def test_f1_2_filter_drops_captcha_detected_responses() -> None:
    """F1.2: a response captured during a captcha challenge is dropped before
    reaching the LLM, regardless of body shape."""
    responses = [
        {
            "url": "https://example.com/api/units",
            "body": {"units": [{"id": 1}]},
            "captcha_detected": True,
        },
        {
            "url": "https://example.com/api/units2",
            "body": {"units": [{"id": 2}]},
        },
    ]
    kept = _filter_candidates(responses, None)
    assert [r["url"] for r in kept] == ["https://example.com/api/units2"]


def test_f1_2_filter_captcha_detected_false_keeps_response() -> None:
    """F1.2: explicit captcha_detected=False is treated identically to absence."""
    responses = [
        {
            "url": "https://example.com/api/units",
            "body": {"units": [{"id": 1}]},
            "captcha_detected": False,
        },
    ]
    kept = _filter_candidates(responses, None)
    assert len(kept) == 1


# ── Ranking ───────────────────────────────────────────────────────────────────


def test_rescue_rank_prefers_availability_url_pattern() -> None:
    a = {"url": "https://x.com/availability", "body": {}}
    b = {"url": "https://x.com/config", "body": {}}
    ranked = _rank_candidates([b, a])
    assert ranked[0]["url"] == a["url"]


def test_rescue_rank_prefers_unit_shaped_body() -> None:
    a = {"url": "https://x.com/a", "body": {"units": [{"rent": 1200}]}}
    b = {"url": "https://x.com/b", "body": {"meta": "stuff"}}
    ranked = _rank_candidates([b, a])
    assert ranked[0]["url"] == a["url"]


def test_rescue_rank_breaks_ties_by_url_length() -> None:
    short = {"url": "https://x.com/a", "body": {}}
    long_ = {"url": "https://x.com/a/b/c/d/e", "body": {}}
    ranked = _rank_candidates([long_, short])
    assert ranked[0]["url"] == short["url"]


# ── Trim ──────────────────────────────────────────────────────────────────────


def test_rescue_trim_preserves_unit_array_truncates_to_200() -> None:
    big_body = {"units": [{"rent": i, "beds": 1} for i in range(300)]}
    trimmed = _trim_body(big_body)
    units = trimmed.get("units", [])
    assert len(units) <= 200


def test_rescue_trim_drops_nav_marketing_keys() -> None:
    body = {"units": [{"rent": 1000}], "nav": {"links": []}, "footer": "text", "gallery": []}
    # Only trim if over budget — make it large enough to trigger
    large_body = dict(body)
    large_body["_padding"] = "x" * 60000
    trimmed = _trim_body(large_body)
    assert "nav" not in trimmed
    assert "footer" not in trimmed
    assert "gallery" not in trimmed


def test_rescue_trim_sets_truncation_sentinel() -> None:
    large_body = {"units": [{"rent": i} for i in range(300)], "_padding": "x" * 60000}
    trimmed = _trim_body(large_body)
    assert trimmed.get("_jugnu_truncated") is True


def test_rescue_trim_returns_deep_copy_never_mutates() -> None:
    original = {"units": [{"rent": 1000}], "nav": "nav-data"}
    _trim_body(original)
    # Original must be untouched
    assert "nav" in original


# ── Prompt ────────────────────────────────────────────────────────────────────


def test_rescue_prompt_substitutes_placeholders_preserves_json_schema_braces() -> None:
    template = 'Property: {{property_name}}\nSchema: {"units": []}\nBody: {{bodies_as_json}}'
    inp = _inp()
    result = _build_prompt(template, inp, "https://test.com/api", {"x": 1})
    assert "{{property_name}}" not in result
    assert "Test Property" in result
    # JSON schema braces must survive
    assert '{"units": []}' in result


# ── LLM call cap ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_respects_max_llm_calls_cap() -> None:
    responses = [{"url": f"https://test.com/api/{i}", "body": {"units": []}} for i in range(5)]
    call_count = 0

    async def fake_call(prompt: str, pid: str) -> tuple[dict, float, str]:
        nonlocal call_count
        call_count += 1
        return (
            {"units": [], "winning_url": None, "envelope": "", "json_paths": {}, "confidence": 0.0},
            0.01,
            "",
        )

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=fake_call):
        await rescue_from_api_responses(_inp(api_responses=responses))

    assert call_count <= MAX_LLM_CALLS_PER_PROPERTY


# ── Retry on JSON decode error ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_retries_on_json_decode_error_once() -> None:
    import json as _json

    call_count = 0

    async def fake_call(prompt: str, pid: str) -> tuple[dict, float, str]:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise _json.JSONDecodeError("bad", "", 0)
        return _good_llm_response(), 0.01, ""

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=fake_call):
        await rescue_from_api_responses(_inp())

    assert call_count >= 1


@pytest.mark.asyncio
async def test_rescue_stops_retrying_after_second_json_decode_error() -> None:
    async def always_fail(prompt: str, pid: str) -> tuple[dict, float, str]:
        return {}, 0.0, ""

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=always_fail):
        result = await rescue_from_api_responses(_inp())
    assert result.units == []


# ── Empty-body blocklist (2026-05-06) ────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_blocks_url_when_llm_returns_empty_body() -> None:
    """When _call_llm returns ({}, 0, "") — the empty-body sentinel — the
    candidate URL pattern is added to blocked_endpoints with reason
    ``llm_empty_response`` so subsequent runs skip it via the per-property
    profile filter.
    """
    async def empty_body(prompt: str, pid: str) -> tuple[dict, float, str]:
        return {}, 0.0, ""

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=empty_body):
        result = await rescue_from_api_responses(
            _inp(api_responses=[
                {"url": "https://amli.com/api/empty?cursor=1", "body": {"units": [{"x": 1}]}},
            ])
        )

    assert result.units == []
    # Distinct reason — must NOT be misclassified as llm_returned_no_units.
    reasons = {r for _, r in result.blocked_endpoints}
    assert reasons == {"llm_empty_response"}
    # URL pattern is stored — query string stripped — so the same endpoint
    # with a different cursor matches the blocklist on the next run.
    blocked_urls = [u for u, _ in result.blocked_endpoints]
    assert blocked_urls == ["https://amli.com/api/empty"]


@pytest.mark.asyncio
async def test_rescue_empty_body_blocklist_collapses_query_variants() -> None:
    """A flapping endpoint that returns empty bodies under multiple cursor
    values should result in ONE pattern entry, not N raw-URL entries."""
    async def empty_body(prompt: str, pid: str) -> tuple[dict, float, str]:
        return {}, 0.0, ""

    responses = [
        {"url": "https://amli.com/api/units?cursor=1", "body": {"units": [{"x": 1}]}},
        {"url": "https://amli.com/api/units?cursor=2", "body": {"units": [{"x": 2}]}},
    ]
    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=empty_body):
        result = await rescue_from_api_responses(_inp(api_responses=responses))

    # Both candidates produced the same URL pattern → 2 entries with the
    # same key. update_profile_blocklist will dedupe these by URL pattern.
    blocked_urls = {u for u, _ in result.blocked_endpoints}
    assert blocked_urls == {"https://amli.com/api/units"}


@pytest.mark.asyncio
async def test_rescue_distinguishes_empty_body_from_no_units_classification() -> None:
    """``llm_returned_no_units`` is reserved for the case where the LLM
    actually classified the body as noise. ``llm_empty_response`` is for the
    case where the upstream returned nothing. They must NOT be conflated.
    """
    async def llm_says_no_units(prompt: str, pid: str) -> tuple[dict, float, str]:
        return {"units": []}, 0.005, '{"units":[]}'

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=llm_says_no_units):
        result = await rescue_from_api_responses(_inp())

    assert result.units == []
    reasons = {r for _, r in result.blocked_endpoints}
    assert reasons == {"llm_returned_no_units"}
    assert "llm_empty_response" not in reasons


# ── Quality gate rejection ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_rejects_llm_output_that_fails_quality_gate() -> None:
    hollow_response = {
        "units": [
            {
                "unit_id": None,
                "floor_plan_name": None,
                "beds": None,
                "baths": None,
                "area": -1,
                "rent_low": None,
                "rent_high": None,
                "available_date": None,
                "lease_term": None,
                "move_in_date": None,
            }
        ],
        "winning_url": "https://test.com/api",
        "envelope": "$.units",
        "json_paths": {},
        "confidence": 0.1,
    }

    async def fake_call(prompt: str, pid: str) -> tuple[dict, float, str]:
        return hollow_response, 0.01, ""

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=fake_call):
        result = await rescue_from_api_responses(_inp())

    assert result.units == []
    assert any("quality_gate" in ep[1] for ep in result.blocked_endpoints)


# ── Tier labels ───────────────────────────────────────────────────────────────


def test_rescue_tier_label_generic_returns_TIER_1_API_LLM_RESCUE() -> None:
    assert _tier_label_for("generic") == "TIER_1_API_LLM_RESCUE"


def test_rescue_tier_label_entrata_returns_TIER_1_API_ENTRATA_LLM_RESCUE() -> None:
    assert _tier_label_for("entrata") == "TIER_1_API_ENTRATA_LLM_RESCUE"


def test_rescue_tier_label_appfolio_returns_TIER_1_API_APPFOLIO_LLM_RESCUE() -> None:
    assert _tier_label_for("appfolio") == "TIER_1_API_APPFOLIO_LLM_RESCUE"


# ── Persistence ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_persists_json_paths_to_llm_field_mappings() -> None:
    async def fake_call(prompt: str, pid: str) -> tuple[dict, float, str]:
        return _good_llm_response(), 0.01, ""

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=fake_call):
        result = await rescue_from_api_responses(_inp())

    assert len(result.llm_field_mappings) == 1
    mapping = result.llm_field_mappings[0]
    assert "api_url_pattern" in mapping
    assert "json_paths" in mapping
    assert "envelope" in mapping


@pytest.mark.asyncio
async def test_rescue_persists_noise_urls_to_blocked_endpoints() -> None:
    async def fake_call(prompt: str, pid: str) -> tuple[dict, float, str]:
        return (
            {"units": [], "winning_url": None, "envelope": "", "json_paths": {}, "confidence": 0.0},
            0.01,
            "",
        )

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=fake_call):
        result = await rescue_from_api_responses(_inp())

    assert len(result.blocked_endpoints) >= 1
    assert any("llm_returned_no_units" in ep[1] for ep in result.blocked_endpoints)


# ── Cost accumulation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_cost_accumulates_across_retries() -> None:
    call_count = 0

    async def fake_call(prompt: str, pid: str) -> tuple[dict, float, str]:
        nonlocal call_count
        call_count += 1
        return (
            {"units": [], "winning_url": None, "envelope": "", "json_paths": {}, "confidence": 0.0},
            0.05,
            "",
        )

    responses = [
        {"url": "https://test.com/api/1", "body": {"units": [{"beds": 1}]}},
        {"url": "https://test.com/api/2", "body": {"units": [{"beds": 2}]}},
    ]
    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=fake_call):
        result = await rescue_from_api_responses(_inp(api_responses=responses))

    assert result.cost_usd > 0


# ── Never raises ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rescue_never_raises_on_internal_exception() -> None:
    async def exploding_call(prompt: str, pid: str) -> tuple[dict, float, str]:
        raise RuntimeError("unexpected kaboom")

    with patch("ma_poc.services.llm_api_rescue._call_llm", side_effect=exploding_call):
        # Must not raise
        result = await rescue_from_api_responses(_inp())
    assert isinstance(result, RescueOutput)


# ── URL pattern helpers ───────────────────────────────────────────────────────


def test_rescue_url_to_pattern_replaces_numeric_ids() -> None:
    url = "https://api.entrata.com/properties/12345/units"
    pattern = _url_to_pattern(url)
    assert "12345" not in pattern
    assert "*" in pattern


def test_rescue_url_to_pattern_strips_query_string() -> None:
    url = "https://api.example.com/units?token=abc&page=1"
    pattern = _url_to_pattern(url)
    assert "token" not in pattern
    assert "page" not in pattern
