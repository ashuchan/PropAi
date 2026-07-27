"""Tests for F2 — LLM rescue routing in ma_poc/pms/scraper.py.

Uses a stub adapter and mocked rescue service.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import DetectedPMS

# ---------------------------------------------------------------------------
# Network seam — see ma_poc/conftest.py
# ---------------------------------------------------------------------------
# Two spots in scrape() fetch through the sync ``_probe.probe_get`` curl_cffi
# seam, which no amount of get_adapter / resolve_target / detect_pms patching
# intercepts:
#   * Step 4b detection rescue — fires when detection is unknown/custom.
#   * F1.5 subpage enrichment — fires when the adapter's units are missing rent
#     OR sqft, which is exactly what ``_hollow_units()`` is (area=-1, no rent),
#     so it fired in nearly every rescue test here and hit test.com live.
# Neither path is what these tests assert on: they assert that the LLM rescue
# gate fires / doesn't fire and that its output is bridged correctly. The
# enrichment must therefore stay a no-op, as it effectively was against the
# live junk response. An inert 200 page does that: ``parse_generic_plan_text``
# finds no plan rows, so the name-map stays empty and no unit is enriched (an
# enriched unit would change the hollow-units input the rescue gate keys on).

_INERT_HTML = (
    "<html><head><title>Test page</title></head>"
    "<body><p>No PMS markers, no floor plans, no rents.</p></body></html>"
)


class _InertProbeResponse:
    """Minimal curl_cffi-response stand-in (``.status_code/.text/.content``)."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.status_code = 200
        self.text = _INERT_HTML
        self.content = _INERT_HTML.encode("utf-8")
        self.headers = {"Content-Type": "text/html; charset=utf-8"}
        self.encoding = "utf-8"

    def json(self) -> Any:
        """Match curl_cffi/requests semantics for a non-JSON body."""
        raise ValueError("inert probe response is not JSON")


@pytest.fixture(autouse=True)
def _stub_probe_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """Serve every ``probe_get`` in this module an inert local page."""

    def _fake_probe_get(url: str, **_kw: Any) -> _InertProbeResponse:
        return _InertProbeResponse(url)

    monkeypatch.setattr(
        "ma_poc.pms.adapters._probe.probe_get", _fake_probe_get, raising=True
    )


def _detected(pms: str = "generic") -> DetectedPMS:
    return DetectedPMS(pms=pms, confidence=0.9, evidence=frozenset())


def _ctx(
    pms: str = "generic",
    api_responses: list | None = None,
    consecutive_failures: int = 0,
    profile: Any = None,
) -> AdapterContext:
    ctx = AdapterContext(
        base_url="https://test.com",
        detected=_detected(pms),
        profile=profile,
        expected_total_units=50,
        property_id="TEST-001",
        fetch_result=None,
    )
    ctx._api_responses = api_responses or []  # type: ignore[attr-defined]
    return ctx


def _hollow_units() -> list[dict]:
    return [
        {
            "unit_id": f"u{i}",
            "beds": None,
            "baths": None,
            "floor_plan_name": None,
            "area": -1,
            "rent_low": None,
        }
        for i in range(3)
    ]


def _good_units() -> list[dict]:
    return [
        {
            "unit_id": "101",
            "beds": 1,
            "baths": 1.0,
            "floor_plan_name": "1BR",
            "area": 750,
            "rent_low": 1200,
            "rent_high": 1200,
        }
    ]


def _api_responses() -> list[dict]:
    return [
        {
            "url": "https://test.com/api/units",
            "body": {"units": [{"beds": 1, "rent": 1200}]},
            "content_type": "application/json",
        }
    ]


# ── Skip conditions ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_quality_gate_passes() -> None:
    from ma_poc.pms import scraper as scraper_mod

    good_result = AdapterResult(units=_good_units(), tier_used="TIER_1_API")

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=good_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_no_api_responses() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result = AdapterResult(units=[], tier_used="TIER_1_API")

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        # No api_responses passed
        await scraper_mod.scrape("https://test.com", api_responses=[], property_id="TEST-001")

    mock_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_pms_is_rentcafe() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result = AdapterResult(units=[], tier_used="TIER_1_API")

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("rentcafe")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("rentcafe"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "rentcafe"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_consecutive_failures_geq_3() -> None:
    from ma_poc.pms import scraper as scraper_mod

    profile = MagicMock()
    profile.stats = MagicMock()
    profile.stats.consecutive_llm_rescue_failures = 3
    profile.model_dump = MagicMock(return_value={})

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape(
            "https://test.com",
            profile=profile,
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    mock_rescue.assert_not_called()


@pytest.mark.asyncio
async def test_scraper_skips_rescue_when_page_unreachable() -> None:
    from ma_poc.pms import scraper as scraper_mod

    unreachable_result = AdapterResult(
        units=[],
        tier_used="TIER_1_API",
        errors=["FAILED_UNREACHABLE: ERR_CONNECTION_REFUSED"],
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=unreachable_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_not_called()


# ── Invocation conditions ─────────────────────────────────────────────────────


def _make_scraper_mocks(pms_name: str, adapter_units: list, rescue_units: list):
    """Helper to set up scraper mocks and return (mock_rescue, mock_adapter)."""
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=adapter_units, tier_used="TIER_1_API")
    rescue_out = RescueOutput(
        units=rescue_units,
        tier_used=f"TIER_1_{pms_name.upper()}_LLM_RESCUE" if rescue_units else "",
        cost_usd=0.05 if rescue_units else 0.02,
        n_llm_calls=1,
        llm_field_mappings=[
            {
                "api_url_pattern": "https://test.com/api/*",
                "json_paths": {},
                "envelope": "",
                "success_count": 1,
            }
        ]
        if rescue_units
        else [],
    )
    return empty_result, rescue_out


@pytest.mark.asyncio
async def test_scraper_bridges_rescue_blocked_endpoints_into_llm_analysis_results() -> None:
    """The empty-body / no-units blocklist returned by rescue must be mirrored
    into ``result["_llm_analysis_results"]`` as ``"noise:<reason>"`` strings.

    Without this bridge, ``profile_updater.update_profile_after_extraction``
    never sees the entries — it iterates only ``_llm_analysis_results``,
    not ``adapter_result.blocked_endpoints``. The pre-fix gap meant a URL
    flagged as empty-body in run N would re-fire in run N+1 and burn
    another LLM timeout.
    """
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(
        units=[],  # rescue itself recovered nothing
        tier_used="",
        cost_usd=0.0,
        n_llm_calls=2,
        blocked_endpoints=[
            ("https://amli.com/api/units", "llm_empty_response"),
            ("https://amli.com/api/floorplans", "llm_returned_no_units"),
        ],
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out
        ),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com",
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    analysis = result.get("_llm_analysis_results") or {}
    # Both rescue blocklist URLs must be mirrored as noise:<reason> strings
    # so profile_updater.update_profile_blocklist sees them.
    assert analysis.get("https://amli.com/api/units") == "noise:llm_empty_response"
    assert analysis.get("https://amli.com/api/floorplans") == "noise:llm_returned_no_units"


@pytest.mark.asyncio
async def test_scraper_invokes_rescue_for_generic_adapter_empty_units() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result, rescue_out = _make_scraper_mocks("generic", _hollow_units(), _good_units())

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out
        ) as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_called_once()


@pytest.mark.asyncio
async def test_scraper_invokes_rescue_for_entrata_adapter_empty_units() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result, rescue_out = _make_scraper_mocks("entrata", _hollow_units(), _good_units())

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out
        ) as mock_rescue,
    ):
        mock_detect.return_value = _detected("entrata")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("entrata"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "entrata"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_called_once()


@pytest.mark.asyncio
async def test_scraper_invokes_rescue_for_appfolio_adapter_empty_units() -> None:
    from ma_poc.pms import scraper as scraper_mod

    empty_result, rescue_out = _make_scraper_mocks("appfolio", _hollow_units(), _good_units())

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out
        ) as mock_rescue,
    ):
        mock_detect.return_value = _detected("appfolio")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("appfolio"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "appfolio"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    mock_rescue.assert_called_once()


@pytest.mark.asyncio
async def test_scraper_replaces_empty_result_with_rescue_units_on_success() -> None:
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(units=_good_units(), tier_used="TIER_1_API_LLM_RESCUE", cost_usd=0.05)

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com", api_responses=_api_responses(), property_id="TEST-001"
        )

    assert result.get("_rescue_succeeded") is True
    assert result.get("extraction_tier_used") == "TIER_1_API_LLM_RESCUE"


@pytest.mark.asyncio
async def test_scraper_records_cost_even_on_rescue_failure() -> None:
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(units=[], tier_used="", cost_usd=0.03, errors=["llm_returned_no_units"])

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com", api_responses=_api_responses(), property_id="TEST-001"
        )

    assert result.get("_rescue_cost_usd", 0) > 0


@pytest.mark.asyncio
async def test_scraper_increments_failure_counter_on_rescue_failure() -> None:
    from ma_poc.models.scrape_profile import ScrapeProfile
    from ma_poc.services.profile_updater import update_rescue_counter

    profile = ScrapeProfile(canonical_id="TEST-001")
    assert profile.stats.consecutive_llm_rescue_failures == 0
    updated = update_rescue_counter(profile, rescue_succeeded=False)
    assert updated.stats.consecutive_llm_rescue_failures == 1


@pytest.mark.asyncio
async def test_scraper_resets_failure_counter_on_rescue_success() -> None:
    from ma_poc.models.scrape_profile import ScrapeProfile
    from ma_poc.services.profile_updater import update_rescue_counter

    profile = ScrapeProfile(canonical_id="TEST-001")
    profile.stats.consecutive_llm_rescue_failures = 2
    updated = update_rescue_counter(profile, rescue_succeeded=True)
    assert updated.stats.consecutive_llm_rescue_failures == 0


@pytest.mark.asyncio
async def test_scraper_emits_all_three_rescue_events() -> None:
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(units=_good_units(), tier_used="TIER_1_API_LLM_RESCUE", cost_usd=0.05)

    emitted_kinds: list[str] = []

    def fake_emit(kind: Any, pid: str, **kw: Any) -> Any:
        emitted_kinds.append(kind.value if hasattr(kind, "value") else str(kind))
        return MagicMock()

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses", return_value=rescue_out),
        patch("ma_poc.observability.events.emit", side_effect=fake_emit),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape("https://test.com", api_responses=_api_responses(), property_id="TEST-001")

    assert "extract.llm_rescue_attempted" in emitted_kinds
    assert "extract.llm_rescue_succeeded" in emitted_kinds


# ── F1.3 (Bug 2) — gate on adapter_name, expanded allow-list ────────────────


@pytest.mark.asyncio
async def test_f1_3_rescue_fires_when_detection_is_unknown_but_adapter_resolves_to_generic() -> None:
    """F1.3: detection.pms = 'unknown' (e.g. F0.2 demoted) but the adapter
    resolves to GenericAdapter. Pre-fix, the gate was on ``pms_name`` and
    locked rescue out of every demoted property. Post-fix, ``adapter_name``
    is in {generic,entrata,appfolio,onesite,amli} so rescue fires."""
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(
        units=_good_units(),
        tier_used="TIER_1_GENERIC_LLM_RESCUE",
        cost_usd=0.05,
        n_llm_calls=1,
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses",
            return_value=rescue_out,
        ) as mock_rescue,
    ):
        mock_detect.return_value = _detected("unknown")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("unknown"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"  # unknown → generic
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape(
            "https://test.com",
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    mock_rescue.assert_called_once()


@pytest.mark.asyncio
async def test_f1_3_rescue_fires_for_onesite_adapter() -> None:
    """F1.3: ``onesite`` was added to the rescue allow-list per the May-8
    plan. Without this, ~50 OneSite properties never see the LLM rescue."""
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API_ONESITE")
    rescue_out = RescueOutput(
        units=_good_units(),
        tier_used="TIER_1_ONESITE_LLM_RESCUE",
        cost_usd=0.05,
        n_llm_calls=1,
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses",
            return_value=rescue_out,
        ) as mock_rescue,
    ):
        mock_detect.return_value = _detected("onesite")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("onesite"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "onesite"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        await scraper_mod.scrape(
            "https://test.com",
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    mock_rescue.assert_called_once()


# ── F1.2 — captcha_detected on fetch_result skips rescue ────────────────────


@pytest.mark.asyncio
async def test_f1_2_rescue_skipped_when_fetch_result_flagged_as_captcha() -> None:
    """F1.2: when the fetch_result carried captcha_detected=True, the rescue
    gate must short-circuit even if all other conditions are met. The
    rescue can't extract from interstitial HTML.

    Uses the real ``FetchResult`` type (not a stub) to prove the gate is
    correctly wired against the production dataclass — the previous stub
    masked a slots+frozen issue where ``getattr(..., 'captcha_detected',
    False)`` always returned False on real FetchResult instances."""
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
    from ma_poc.pms import scraper as scraper_mod

    real_fetch_result = FetchResult(
        url="https://test.com",
        outcome=FetchOutcome.OK,
        status=200,
        body=b"<html>...cf challenge...</html>",
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://test.com",
        attempts=1,
        elapsed_ms=100,
        captcha_detected=True,
    )

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses") as mock_rescue,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com",
            api_responses=_api_responses(),
            property_id="TEST-001",
            fetch_result=real_fetch_result,
        )

    mock_rescue.assert_not_called()
    assert result.get("_rescue_skipped_reason") == "captcha_detected"


@pytest.mark.asyncio
async def test_f1_2_real_fetchresult_default_captcha_detected_is_false() -> None:
    """F1.2 regression: a default-constructed FetchResult must have
    ``captcha_detected=False`` so the rescue gate doesn't false-positive.
    Catches the case where someone removes the field default."""
    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

    fr = FetchResult(
        url="https://test.com",
        outcome=FetchOutcome.OK,
        status=200,
        body=b"normal body",
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://test.com",
        attempts=1,
        elapsed_ms=100,
    )
    assert fr.captcha_detected is False


def test_f1_2_orchestrator_forwards_captcha_flag_from_network_log() -> None:
    """F1.2: the per-network-log-entry captcha_detected flag captured by
    the fetcher must survive the orchestrator's network_log → _api_responses
    rebuild. Without this propagation the rescue's _filter_candidates can't
    drop interstitial XHR captures.

    Exercises the rebuild loop in scrape() directly so the assertion is
    independent of full pipeline mocking."""
    import asyncio

    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
    from ma_poc.pms import scraper as scraper_mod

    fr = FetchResult(
        url="https://test.com",
        outcome=FetchOutcome.OK,
        status=200,
        body=b"<html></html>",
        headers={},
        render_mode=RenderMode.RENDER,
        final_url="https://test.com",
        attempts=1,
        elapsed_ms=100,
        network_log=[
            {
                "url": "https://test.com/api/units",
                "status": 200,
                "content_type": "application/json",
                "body": '{"units": []}',
                "captcha_detected": True,
            },
            {
                "url": "https://test.com/api/floorplans",
                "status": 200,
                "content_type": "application/json",
                "body": '{"floorplans": []}',
                "captcha_detected": False,
            },
        ],
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        captured_ctx: dict[str, Any] = {}

        async def _capture(_page: Any, ctx: AdapterContext) -> AdapterResult:
            captured_ctx["api_responses"] = list(getattr(ctx, "_api_responses", []))
            return AdapterResult(units=_good_units(), tier_used="TIER_1_API")

        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = _capture
        mock_get_adapter.return_value = mock_adapter

        asyncio.run(
            scraper_mod.scrape(
                "https://test.com",
                api_responses=None,  # force the network_log → _api_responses rebuild
                property_id="TEST-001",
                fetch_result=fr,
            )
        )

    responses = captured_ctx["api_responses"]
    by_url = {r["url"]: r for r in responses}
    assert by_url["https://test.com/api/units"]["captcha_detected"] is True
    assert by_url["https://test.com/api/floorplans"]["captcha_detected"] is False


# ── F1.4 — envelope → response_envelope normalization at bridge ─────────────


@pytest.mark.asyncio
async def test_f1_4_bridge_normalizes_envelope_to_response_envelope() -> None:
    """F1.4: rescue emits ``envelope`` but profile_updater reads
    ``response_envelope``. The bridge in scraper.py renames the key so
    persisted LlmFieldMappings replay correctly."""
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(
        units=_good_units(),
        tier_used="TIER_1_GENERIC_LLM_RESCUE",
        winning_url="https://test.com/api/v2/units",
        cost_usd=0.05,
        n_llm_calls=1,
        llm_field_mappings=[
            {
                "api_url_pattern": "https://test.com/api/v2/units",
                "envelope": "$.data.results",
                "json_paths": {"unit_number": "id"},
                "source_adapter": "generic",
                "created_at": "2026-05-09T00:00:00",
                "success_count": 1,
            }
        ],
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses",
            return_value=rescue_out,
        ),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com",
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    analysis = result.get("_llm_analysis_results") or {}
    mapping = analysis.get("https://test.com/api/v2/units")
    assert isinstance(mapping, dict), f"Expected mapping dict, got {mapping!r}"
    assert mapping.get("response_envelope") == "$.data.results", (
        "F1.4 must rename 'envelope' to 'response_envelope' so "
        "profile_updater.save_llm_field_mapping persists it correctly"
    )
    assert "envelope" not in mapping, (
        "Old key must be removed after normalization to avoid double storage"
    )


@pytest.mark.asyncio
async def test_f1_4_bridge_preserves_response_envelope_when_already_correct() -> None:
    """F1.4: if rescue ever emits ``response_envelope`` directly (forward-
    compatible), the bridge must NOT clobber it with an empty value."""
    from ma_poc.pms import scraper as scraper_mod
    from ma_poc.services.llm_api_rescue import RescueOutput

    empty_result = AdapterResult(units=_hollow_units(), tier_used="TIER_1_API")
    rescue_out = RescueOutput(
        units=_good_units(),
        tier_used="TIER_1_GENERIC_LLM_RESCUE",
        winning_url="https://test.com/api/v2/units",
        cost_usd=0.05,
        n_llm_calls=1,
        llm_field_mappings=[
            {
                "api_url_pattern": "https://test.com/api/v2/units",
                "response_envelope": "$.payload",
                "json_paths": {},
                "source_adapter": "generic",
                "created_at": "2026-05-09T00:00:00",
                "success_count": 1,
            }
        ],
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch(
            "ma_poc.services.llm_api_rescue.rescue_from_api_responses",
            return_value=rescue_out,
        ),
    ):
        mock_detect.return_value = _detected("generic")
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected("generic"),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "generic"
        mock_adapter.extract = AsyncMock(return_value=empty_result)
        mock_get_adapter.return_value = mock_adapter

        result = await scraper_mod.scrape(
            "https://test.com",
            api_responses=_api_responses(),
            property_id="TEST-001",
        )

    mapping = (result.get("_llm_analysis_results") or {}).get(
        "https://test.com/api/v2/units"
    )
    assert isinstance(mapping, dict)
    assert mapping.get("response_envelope") == "$.payload"
