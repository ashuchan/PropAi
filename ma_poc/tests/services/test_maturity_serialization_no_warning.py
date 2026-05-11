"""Regression: profile saves must not emit Pydantic serializer warnings.

The codebase has both ``models.scrape_profile`` and ``ma_poc.models.scrape_profile``
reachable on ``sys.path``. Python's module cache keys on the import name, so
each path produces a distinct module object — and a distinct
``ProfileMaturity`` class object. Pre-fix, ``profile_updater.py`` imported
``ProfileMaturity`` from one path and assigned its enum values to a field
whose schema was built against the OTHER path. Pydantic detected the class
mismatch on serialization (``PydanticSerializationUnexpectedValue``) and
emitted hundreds of warning lines a day in Cloud Logging.

Functional impact was nil — both classes serialize to the same string
(``"HOT"``, ``"WARM"``) and StrEnum equality is value-based — but the log
noise made real signals harder to spot.

Fix: at every assignment site that mutates ``profile.confidence.maturity``,
resolve the canonical enum class via ``type(profile.confidence.maturity)``
instead of the module-level import. This guarantees serializer/value class
agreement regardless of which import path the caller is using.

These tests pin both directions:

  - The ``models.scrape_profile`` (bare-path) test ran without warning
    pre-fix already (matched the writer's then-import), so it serves as
    the no-regression baseline.
  - The ``ma_poc.models.scrape_profile`` (prefixed-path) test ran with
    warnings pre-fix and is the canary that the dynamic-resolution fix
    actually works.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any

import pytest


def _is_serializer_warning(w: warnings.WarningMessage) -> bool:
    """Match Pydantic's serializer warnings without false positives.

    The runtime emits unrelated ``DeprecationWarning`` (e.g. for
    ``datetime.utcnow``); we only care about Pydantic's ``UserWarning``
    carrying ``PydanticSerializationUnexpectedValue``.
    """
    msg = str(w.message)
    return "PydanticSerialization" in msg or (
        "enum" in msg.lower() and "serialized" in msg.lower()
    )


def _drive_maturity_transitions(profile: Any, store: Any, update_fn: Any) -> None:
    """Drive 3 successful scrapes — COLD → WARM → WARM → HOT — capturing
    only Pydantic serializer warnings."""
    scrape_result = {
        "extraction_tier_used": "TIER_1_API",
        "units": [{"unit_id": "U1"}],
        "_llm_hints": {"api_urls_with_data": ["https://api.example.com/units"]},
    }
    update_fn(profile, scrape_result, 1, store)
    update_fn(profile, scrape_result, 1, store)
    update_fn(profile, scrape_result, 1, store)


def test_no_serializer_warning_when_caller_uses_bare_path(tmp_path: Path) -> None:
    """Caller imports via ``models.scrape_profile`` (bare path)."""
    from models.scrape_profile import ProfileMaturity as PM
    from models.scrape_profile import ScrapeProfile
    from services.profile_store import ProfileStore
    from services.profile_updater import update_profile_after_extraction

    store = ProfileStore(tmp_path / "profiles")
    profile = ScrapeProfile(canonical_id="bare-path-001")
    store.save(profile)

    with warnings.catch_warnings(record=True) as ws:
        warnings.simplefilter("always")
        _drive_maturity_transitions(profile, store, update_profile_after_extraction)

    serializer_warnings = [w for w in ws if _is_serializer_warning(w)]
    assert serializer_warnings == [], (
        "Pydantic emitted serializer warnings on the bare-path caller side. "
        "Failures: " + "; ".join(str(w.message)[:150] for w in serializer_warnings)
    )
    assert profile.confidence.maturity == PM.HOT
    assert profile.confidence.consecutive_successes == 3


def test_no_serializer_warning_when_caller_uses_ma_poc_path(tmp_path: Path) -> None:
    """Caller imports via ``ma_poc.models.scrape_profile`` (prefixed path).

    This is the path that fired the warning pre-fix. The dynamic-resolution
    fix in profile_updater.py + drift_detector.py must keep this clean.
    """
    from ma_poc.models.scrape_profile import ProfileMaturity as PM
    from ma_poc.models.scrape_profile import ScrapeProfile
    from ma_poc.services.profile_store import ProfileStore
    from ma_poc.services.profile_updater import update_profile_after_extraction

    store = ProfileStore(tmp_path / "profiles")
    profile = ScrapeProfile(canonical_id="ma-poc-path-001")
    store.save(profile)

    with warnings.catch_warnings(record=True) as ws:
        warnings.simplefilter("always")
        _drive_maturity_transitions(profile, store, update_profile_after_extraction)

    serializer_warnings = [w for w in ws if _is_serializer_warning(w)]
    assert serializer_warnings == [], (
        "Pydantic emitted serializer warnings on the ma_poc-path caller side — "
        "the dual-class fix in profile_updater.py / drift_detector.py "
        "regressed. Re-check that those files use "
        "``type(profile.confidence.maturity)`` to resolve the enum class, "
        "not a module-level import. Failures: "
        + "; ".join(str(w.message)[:150] for w in serializer_warnings)
    )
    assert profile.confidence.maturity == PM.HOT
    assert profile.confidence.consecutive_successes == 3


def test_drift_demotion_emits_no_serializer_warning(tmp_path: Path) -> None:
    """``apply_drift_demotion`` is the second writer that mutates maturity.

    Same dual-class risk; assert it's also clean. Drives a HOT → WARM
    demotion (the most common path) and a HOT → COLD severe demotion.
    """
    from ma_poc.models.scrape_profile import ProfileMaturity as PM
    from ma_poc.models.scrape_profile import ScrapeProfile
    from ma_poc.services.drift_detector import apply_drift_demotion
    from ma_poc.services.profile_store import ProfileStore

    store = ProfileStore(tmp_path / "profiles")

    # HOT → WARM (non-severe drift signal)
    profile_warm = ScrapeProfile(canonical_id="drift-warm-001")
    profile_warm.confidence.maturity = PM.HOT
    profile_warm.confidence.consecutive_successes = 5
    with warnings.catch_warnings(record=True) as ws:
        warnings.simplefilter("always")
        apply_drift_demotion(profile_warm, ["unit_count_drop"])
        store.save(profile_warm)
    assert [w for w in ws if _is_serializer_warning(w)] == []
    assert profile_warm.confidence.maturity == PM.WARM

    # HOT → COLD (severe drift signal)
    profile_cold = ScrapeProfile(canonical_id="drift-cold-001")
    profile_cold.confidence.maturity = PM.HOT
    profile_cold.confidence.consecutive_successes = 5
    with warnings.catch_warnings(record=True) as ws:
        warnings.simplefilter("always")
        apply_drift_demotion(profile_cold, ["all_rents_null"])
        store.save(profile_cold)
    assert [w for w in ws if _is_serializer_warning(w)] == []
    assert profile_cold.confidence.maturity == PM.COLD


def test_dynamic_class_resolution_returns_correct_class_per_instance() -> None:
    """``type(profile.confidence.maturity)`` resolves to the schema's class
    for ANY caller's import path. Pin the property the fix relies on so a
    future Pydantic-version upgrade that changes attribute-class semantics
    surfaces immediately.
    """
    from ma_poc.models.scrape_profile import ProfileMaturity as PM_B
    from ma_poc.models.scrape_profile import ScrapeProfile as SP_B
    from models.scrape_profile import ProfileMaturity as PM_A
    from models.scrape_profile import ScrapeProfile as SP_A

    # Two distinct module paths must yield two distinct class objects on
    # this codebase — that's the precondition the fix targets. If they
    # become the same (sys.path discipline tightens), the fix is still
    # correct but the dual-class probe is no longer meaningful — that's
    # a strictly better world and the test then degenerates harmlessly.
    if PM_A is PM_B:
        pytest.skip(
            "ProfileMaturity now resolves to a single class — sys.path was "
            "unified upstream. The dynamic-resolution fix remains correct."
        )

    p_a = SP_A(canonical_id="probe-a")
    p_b = SP_B(canonical_id="probe-b")
    assert type(p_a.confidence.maturity) is PM_A
    assert type(p_b.confidence.maturity) is PM_B
