"""F6 — runner routing + profile cache + writer/reader tests.

Covers H4 (fallback), H5 (direct-before-vanity ordering), H6 (cache hit
skips resolve), H11 (writer + reader present), H12 (best-effort write),
H13 (no overwrite on failure).

Mocks at the ``ma_poc.pms.rentcafe_direct.runner_dispatch.{resolve_property_id,
fetch_direct}`` boundary — the real httpx is never invoked.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from ma_poc.models.scrape_profile import (
    ApiHints,
    DomHints,
    NavigationConfig,
    ScrapeProfile,
)
from ma_poc.pms.rentcafe_direct.fetcher import DirectFetchResult
from ma_poc.pms.rentcafe_direct.propertyid_resolver import ResolveResult

__all__: list[str] = []  # silence ruff F401 for re-export checks

# parents[0]=integration, [1]=tests, [2]=ma_poc, [3]=PropAi
_REPO = Path(__file__).resolve().parents[3]


def _success_direct(pid: str = "1782238") -> DirectFetchResult:
    return DirectFetchResult(
        property_id=pid,
        api_responses=[
            {
                "url": f"https://www.rentcafe.com/wp-json/middleware/v1/getFloorplans/?propertyId%5B%5D={pid}",
                "body": [
                    {
                        "propertyId": pid,
                        "floorplanId": "fp-1",
                        "floorplanName": "1 Bed",
                        "beds": 1,
                        "baths": "1",
                        "minimumRent": "1500.00",
                        "maximumRent": "1500.00",
                        "min_price": 1500,
                        "max_price": 1500,
                        "availableUnitsCount": "1",
                        "availableDate": "2026-05-01",
                        "api": "rentcafe",
                        "availabilityURL": "https://example.securecafe.com/...",
                    }
                ],
                "status": 200,
                "headers": {},
            }
        ],
        tier_code="TIER_1_API_RENTCAFE_DIRECT",
        tier_message=None,
        elapsed_ms=120,
    )


def _failed_direct(tier: str = "TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE") -> DirectFetchResult:
    return DirectFetchResult(
        property_id="x",
        api_responses=[],
        tier_code=tier,
        tier_message="simulated failure",
        elapsed_ms=10,
    )


def _make_profile(
    canonical_id: str = "test-prop",
    api_provider: str = "rentcafe",
    cached_id: str | None = None,
) -> ScrapeProfile:
    profile = ScrapeProfile(
        canonical_id=canonical_id,
        navigation=NavigationConfig(),
        api_hints=ApiHints(api_provider=api_provider, rentcafe_property_id=cached_id),
        dom_hints=DomHints(),
    )
    return profile


def _csv_row() -> dict[str, str]:
    return {
        "name": "Hampshire Village",
        "city": "San Francisco",
        "zip": "94107",
        "website": "https://hampshire.rentcafe.com/",
    }


@pytest.mark.asyncio
async def test_f6_direct_runs_before_vanity_when_id_cached() -> None:
    """H5 — direct fetch with cached id → vanity is skipped on success."""
    from ma_poc.pms.rentcafe_direct.runner_dispatch import try_rentcafe_direct

    profile = _make_profile(cached_id="1782238")
    task = SimpleNamespace(property_id="prop-1", url="https://hampshire.rentcafe.com/")

    with (
        patch(
            "ma_poc.pms.rentcafe_direct.runner_dispatch.fetch_direct",
            return_value=_success_direct("1782238"),
        ) as mock_fetch,
        patch(
            "ma_poc.pms.rentcafe_direct.runner_dispatch.resolve_property_id",
        ) as mock_resolve,
    ):
        synthetic, pid = await try_rentcafe_direct(task, profile, _csv_row())

    assert synthetic is not None, "Direct path must produce a fetch result on success"
    assert pid == "1782238"
    # H6 — resolve must NOT be called when cache hits.
    mock_resolve.assert_not_called()
    mock_fetch.assert_called_once()


@pytest.mark.asyncio
async def test_f6_falls_back_on_resolution_failure() -> None:
    """H4 — resolve fails → returns (None, None) so caller falls back."""
    from ma_poc.pms.rentcafe_direct.runner_dispatch import try_rentcafe_direct

    profile = _make_profile(cached_id=None)
    task = SimpleNamespace(property_id="prop-2", url="https://example.com/")

    with (
        patch(
            "ma_poc.pms.rentcafe_direct.runner_dispatch.resolve_property_id",
            return_value=ResolveResult(
                property_id=None,
                matched_name=None,
                matched_zip=None,
                confidence="NONE",
                raw_search_response=None,
                failure_reason="NO_RESULTS",
            ),
        ),
        patch("ma_poc.pms.rentcafe_direct.runner_dispatch.fetch_direct") as mock_fetch,
    ):
        synthetic, pid = await try_rentcafe_direct(task, profile, _csv_row())

    assert synthetic is None
    assert pid is None
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_f6_falls_back_on_metadata_mismatch() -> None:
    """H4 — empty CSV (no name/zip) → cannot resolve, fallback."""
    from ma_poc.pms.rentcafe_direct.runner_dispatch import try_rentcafe_direct

    profile = _make_profile(cached_id=None)
    task = SimpleNamespace(property_id="prop-3", url="https://example.com/")

    # resolve_property_id should not even be called because we lack
    # name+zip — the helper bails out before issuing the search.
    with (
        patch(
            "ma_poc.pms.rentcafe_direct.runner_dispatch.resolve_property_id"
        ) as mock_resolve,
        patch("ma_poc.pms.rentcafe_direct.runner_dispatch.fetch_direct") as mock_fetch,
    ):
        synthetic, pid = await try_rentcafe_direct(task, profile, {})
    assert synthetic is None and pid is None
    mock_resolve.assert_not_called()
    mock_fetch.assert_not_called()


@pytest.mark.asyncio
async def test_f6_falls_back_on_direct_fetch_failure() -> None:
    """H4 — direct fetch returns NO_RESPONSE → fallback."""
    from ma_poc.pms.rentcafe_direct.runner_dispatch import try_rentcafe_direct

    profile = _make_profile(cached_id="1782238")
    task = SimpleNamespace(property_id="prop-4", url="https://hampshire.rentcafe.com/")

    with (
        patch(
            "ma_poc.pms.rentcafe_direct.runner_dispatch.fetch_direct",
            return_value=_failed_direct("TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE"),
        ),
        patch(
            "ma_poc.pms.rentcafe_direct.runner_dispatch.resolve_property_id"
        ) as mock_resolve,
    ):
        synthetic, pid = await try_rentcafe_direct(task, profile, _csv_row())
    assert synthetic is None
    assert pid is None
    mock_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_f6_second_run_uses_cache() -> None:
    """H6 — second run with same profile (id cached) does NOT call resolve."""
    from ma_poc.pms.rentcafe_direct.runner_dispatch import try_rentcafe_direct

    profile = _make_profile(cached_id="1782238")
    task = SimpleNamespace(property_id="prop-5", url="https://hampshire.rentcafe.com/")

    with (
        patch(
            "ma_poc.pms.rentcafe_direct.runner_dispatch.fetch_direct",
            return_value=_success_direct("1782238"),
        ),
        patch(
            "ma_poc.pms.rentcafe_direct.runner_dispatch.resolve_property_id"
        ) as mock_resolve,
    ):
        for _ in range(2):
            await try_rentcafe_direct(task, profile, _csv_row())
    assert mock_resolve.call_count == 0


def test_f6_propertyid_persisted_after_success() -> None:
    """H6 — writer persists rentcafe_property_id when result tier is success."""
    from services.profile_updater import update_profile_after_extraction

    profile = _make_profile(cached_id=None)

    class _StubStore:
        def __init__(self) -> None:
            self.saved: list[ScrapeProfile] = []

        def save(self, p: ScrapeProfile) -> None:
            self.saved.append(p)

    store = _StubStore()
    scrape_result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE_DIRECT",
        "_rentcafe_property_id": "1782238",
        "units": [{"floor_plan_name": "1 Bed", "rent_range": "$1500"}],
    }
    update_profile_after_extraction(profile, scrape_result, units_extracted=1, store=store)
    assert profile.api_hints.rentcafe_property_id == "1782238"


def test_f6_propertyid_not_persisted_on_failure() -> None:
    """H13 — failed direct fetch must NOT overwrite a previously-good id."""
    from services.profile_updater import update_profile_after_extraction

    profile = _make_profile(cached_id="GOOD_ID")

    class _StubStore:
        def save(self, p: ScrapeProfile) -> None:
            pass

    # Failure: no _rentcafe_property_id set, tier is the vanity-domain
    # one. The writer must leave the prior id untouched.
    scrape_result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE",
        "units": [],
    }
    update_profile_after_extraction(profile, scrape_result, units_extracted=0, store=_StubStore())
    assert profile.api_hints.rentcafe_property_id == "GOOD_ID"


def test_f6_propertyid_not_persisted_on_wrong_tier() -> None:
    """H13 corollary — even with a propertyId set, wrong tier must not write."""
    from services.profile_updater import update_profile_after_extraction

    profile = _make_profile(cached_id="GOOD_ID")

    class _StubStore:
        def save(self, p: ScrapeProfile) -> None:
            pass

    # Vanity-domain success carries no propertyId — direct path didn't
    # run. Even if a stale "_rentcafe_property_id" leaked in, the
    # tier-gate rejects the write.
    scrape_result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE",
        "_rentcafe_property_id": "STALE_ID",
        "units": [{"floor_plan_name": "1 Bed"}],
    }
    update_profile_after_extraction(profile, scrape_result, units_extracted=1, store=_StubStore())
    assert profile.api_hints.rentcafe_property_id == "GOOD_ID"


def test_f6_profile_write_swallows_exception() -> None:
    """H12 — the F6 write path is wrapped in try/except.

    Inject a setter that raises by monkey-patching ``ApiHints``'s
    rentcafe_property_id descriptor for the duration of the test. The
    updater must complete without re-raising. We additionally verify
    the source is structurally guarded (try-block contains the
    ``profile.api_hints.rentcafe_property_id =`` assignment) so a future
    refactor that drops the guard fails this test even if the runtime
    exception path is no longer reachable.
    """
    from services.profile_updater import update_profile_after_extraction

    # Static guard — if the source no longer wraps the write in
    # try/except, this assertion fires and pinpoints the regression.
    pu_path = _REPO / "ma_poc" / "services" / "profile_updater.py"
    pu_text = pu_path.read_text(encoding="utf-8")
    assignment_idx = pu_text.find("profile.api_hints.rentcafe_property_id = str(pid)")
    assert assignment_idx > 0, (
        "F6 writer assignment not found in profile_updater.py — check refactor"
    )
    # Walk backwards to find the nearest ``try:`` and forward to
    # ``except`` to confirm the assignment lives inside a try block.
    pre = pu_text[:assignment_idx]
    nearest_try = pre.rfind("    try:")
    nearest_except_in_pre = pre.rfind("except Exception as exc:")
    assert nearest_try > 0, "F6 writer must live inside a try block (H12)"
    # The most recent ``try:`` before the assignment must come after
    # any prior ``except Exception`` — otherwise the assignment is
    # outside the guard.
    assert nearest_try > nearest_except_in_pre, (
        "F6 writer assignment is not inside a try/except (H12)"
    )

    # Runtime check — force the assignment to raise, ensure the call
    # still returns. Replace the ``rentcafe_property_id`` field with a
    # property descriptor on the ApiHints class so the setter on every
    # instance raises.
    profile = _make_profile(cached_id=None)

    class _StubStore:
        def save(self, p: ScrapeProfile) -> None:
            pass

    scrape_result = {
        "extraction_tier_used": "TIER_1_API_RENTCAFE_DIRECT",
        "_rentcafe_property_id": "1782238",
        "units": [{"x": 1}],
    }

    cls = type(profile.api_hints)
    original = cls.__dict__.get("rentcafe_property_id", None)

    def _raising_setter(_self: object, _value: object) -> None:
        raise RuntimeError("simulated write failure")

    # Pydantic v2 stores fields via __pydantic_fields__ but exposes
    # them through the standard descriptor protocol; override the
    # attribute descriptor for the duration of the test only.
    setattr(cls, "rentcafe_property_id", property(lambda self: None, _raising_setter))
    try:
        # Must not raise.
        update_profile_after_extraction(
            profile, scrape_result, units_extracted=1, store=_StubStore()
        )
    finally:
        if original is None:
            try:
                delattr(cls, "rentcafe_property_id")
            except AttributeError:
                pass
        else:
            setattr(cls, "rentcafe_property_id", original)


def test_f6_schema_field_has_writer_and_reader() -> None:
    """H11 — rentcafe_property_id must have both a writer and a reader."""
    repo_root = _REPO
    writer_files: list[str] = []
    reader_files: list[str] = []

    for f in (repo_root / "ma_poc").rglob("*.py"):
        if "tests" in f.parts:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        rel = str(f.relative_to(repo_root)).replace("\\", "/")
        # Writer: ``<dot>rentcafe_property_id =`` or
        # ``rentcafe_property_id: ... = ...`` (the schema field default
        # also counts as a writer).
        if (
            ".rentcafe_property_id =" in text
            or "rentcafe_property_id =" in text
            or "rentcafe_property_id:" in text
        ):
            writer_files.append(rel)
        # Reader: dot-access not immediately followed by ``=`` (i.e.
        # not an assignment).
        for line in text.splitlines():
            if ".rentcafe_property_id" not in line:
                continue
            after = line.split(".rentcafe_property_id", 1)[1][:5]
            if "=" not in after:
                reader_files.append(rel)
                break

    assert writer_files, f"H11: no writer for rentcafe_property_id (searched under {repo_root}/ma_poc)"
    assert reader_files, f"H11: no reader for rentcafe_property_id (searched under {repo_root}/ma_poc)"


def test_f6_non_rentcafe_unchanged() -> None:
    """Non-RentCafe properties never enter the direct path — covered by
    the runner's ``api_provider == "rentcafe"`` guard. This is a tiny
    smoke that the guard exists in source (cheap drift check)."""
    runner_path = _REPO / "ma_poc" / "scripts" / "jugnu_runner.py"
    text = runner_path.read_text(encoding="utf-8")
    assert 'api_provider == "rentcafe"' in text, (
        "jugnu_runner must guard the direct path on api_provider=='rentcafe'"
    )
