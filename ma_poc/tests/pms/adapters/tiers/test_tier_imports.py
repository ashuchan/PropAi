"""PR-4 TDD tests — tier class imports and backward compatibility.

These tests intentionally fail (red) until the split is implemented.
"""

from __future__ import annotations


# ── Tier module imports ────────────────────────────────────────────────────


def test_tiers_package_importable() -> None:
    """The tiers package itself must be importable."""
    import ma_poc.pms.adapters.tiers  # noqa: F401


def test_blocked_filter_tier_importable() -> None:
    from ma_poc.pms.adapters.tiers.blocked_filter import BlockedFilterTier  # noqa: F401


def test_profile_replay_tier_importable() -> None:
    from ma_poc.pms.adapters.tiers.profile_replay import ProfileReplayTier  # noqa: F401


def test_api_narrow_tier_importable() -> None:
    from ma_poc.pms.adapters.tiers.api_narrow import ApiNarrowTier  # noqa: F401


def test_api_broad_tier_importable() -> None:
    from ma_poc.pms.adapters.tiers.api_broad import ApiBroadTier  # noqa: F401


def test_html_tiers_importable() -> None:
    from ma_poc.pms.adapters.tiers.html_tiers import (  # noqa: F401
        EmbeddedJsonTier,
        JsonLdTier,
        DomScanTier,
    )


def test_llm_tiers_importable() -> None:
    from ma_poc.pms.adapters.tiers.llm_tiers import (  # noqa: F401
        LlmApiTargetedTier,
        LlmDomTargetedTier,
        LlmMonolithicTier,
    )


# ── tier_orchestrator.py ───────────────────────────────────────────────────


def test_tier_orchestrator_importable() -> None:
    from ma_poc.pms.adapters.tier_orchestrator import GenericAdapter  # noqa: F401


def test_tier_orchestrator_generic_adapter_is_class() -> None:
    from ma_poc.pms.adapters.tier_orchestrator import GenericAdapter

    assert isinstance(GenericAdapter(), GenericAdapter)
    assert GenericAdapter.pms_name == "generic"


def test_tier_orchestrator_has_extract_method() -> None:
    import inspect

    from ma_poc.pms.adapters.tier_orchestrator import GenericAdapter

    assert inspect.iscoroutinefunction(GenericAdapter.extract)


# ── backward compat: generic.py re-exports ────────────────────────────────


def test_backward_compat_generic_adapter_import() -> None:
    """GenericAdapter must still be importable from ma_poc.pms.adapters.generic."""
    from ma_poc.pms.adapters.generic import GenericAdapter  # noqa: F401


def test_backward_compat_parse_generic_api_import() -> None:
    from ma_poc.pms.adapters.generic import parse_generic_api  # noqa: F401


def test_backward_compat_find_unit_list_import() -> None:
    from ma_poc.pms.adapters.generic import _find_unit_list  # noqa: F401


def test_backward_compat_generic_adapter_is_same_class() -> None:
    """The re-exported GenericAdapter must be the canonical class from tier_orchestrator."""
    from ma_poc.pms.adapters.generic import GenericAdapter as G1
    from ma_poc.pms.adapters.tier_orchestrator import GenericAdapter as G2

    assert G1 is G2, "generic.py must re-export the same class object as tier_orchestrator.py"


# ── Tier class API contracts ───────────────────────────────────────────────


def test_blocked_filter_tier_has_run_method() -> None:
    from ma_poc.pms.adapters.tiers.blocked_filter import BlockedFilterTier

    assert callable(BlockedFilterTier.run)


def test_profile_replay_tier_has_run_method() -> None:
    from ma_poc.pms.adapters.tiers.profile_replay import ProfileReplayTier

    assert callable(ProfileReplayTier.run)


def test_api_narrow_tier_has_run_method() -> None:
    from ma_poc.pms.adapters.tiers.api_narrow import ApiNarrowTier

    assert callable(ApiNarrowTier.run)


def test_api_broad_tier_has_run_method() -> None:
    from ma_poc.pms.adapters.tiers.api_broad import ApiBroadTier

    assert callable(ApiBroadTier.run)


def test_html_tier_classes_have_run_methods() -> None:
    from ma_poc.pms.adapters.tiers.html_tiers import (
        DomScanTier,
        EmbeddedJsonTier,
        JsonLdTier,
    )

    for cls in (EmbeddedJsonTier, JsonLdTier, DomScanTier):
        assert callable(cls.run), f"{cls.__name__}.run must be callable"


def test_llm_tier_classes_have_run_methods() -> None:
    import inspect

    from ma_poc.pms.adapters.tiers.llm_tiers import (
        LlmApiTargetedTier,
        LlmDomTargetedTier,
        LlmMonolithicTier,
    )

    for cls in (LlmApiTargetedTier, LlmDomTargetedTier, LlmMonolithicTier):
        assert inspect.iscoroutinefunction(cls.run), f"{cls.__name__}.run must be async"
