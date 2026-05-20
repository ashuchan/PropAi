"""Cluster #4 fix — bootstrap profile BEFORE the L1 fetch so the tier
escalator can engage on Cloudflare-walled / bot-blocked properties.

Background (2026-05-20, feature_fail_1429 grind cluster #4):
``generic:no_body_short_circuit`` on 64 Bucket-A properties. Live-probe
of the top 2 (tidesateastchase, liveatpalmhaven) showed HTTP 403 with
``cf-mitigated: challenge`` — Cloudflare bot-walls. The tier escalator
in ``ma_poc.fetch.fetcher.Fetcher.fetch`` gates on ``profile is not
None`` at line 197 (single-tier DIRECT path when no profile is
supplied; escalator path when one is). The runner used to bootstrap
the profile only AFTER the fetch (at the L3 step), so first-run
properties had ``profile_for_dispatch=None`` at fetch time → no
escalation → BOT_BLOCKED → no_body_short_circuit.

This file pins the runner-side wiring intent via source inspection
(testing the full ``_process_property`` flow requires too much setup
to be reliable). The complementary behavioral tests are
``test_top_level_fetch_profile_arg.py`` (verifies ``fetch(task,
profile)`` propagation) and ``test_tier_escalator.py`` (verifies the
escalator fires on BOT_BLOCKED when profile is supplied).
"""

from __future__ import annotations

import re
from pathlib import Path

_RUNNER_SRC_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "runners" / "jugnu.py"
)


def _read_runner_source() -> str:
    return _RUNNER_SRC_PATH.read_text(encoding="utf-8")


def test_runner_bootstraps_profile_before_jugnu_fetch() -> None:
    """The fix: before calling jugnu_fetch in the H4 fallback, the
    runner must bootstrap a COLD profile when ``profile_for_dispatch``
    is None — otherwise the escalator gate locks out residential proxy
    on Cloudflare-walled properties. Look for the exact ordering pattern
    so a future refactor that reorders these steps fails this test."""
    src = _read_runner_source()
    # The H4 block must:
    #   1. check `profile_for_dispatch is None and hasattr(profile_store, "bootstrap")`
    #   2. call profile_store.bootstrap(...)
    #   3. THEN call jugnu_fetch with profile=profile_for_dispatch
    # Build a regex that requires all 3 in sequence within one block.
    pattern = re.compile(
        r"profile_for_dispatch\s+is\s+None.*?"
        r"profile_store\.bootstrap.*?"
        r"jugnu_fetch\(\s*task,\s*profile\s*=\s*profile_for_dispatch\s*\)",
        re.DOTALL,
    )
    assert pattern.search(src), (
        "Expected the H4 fallback in scripts/runners/jugnu.py to:\n"
        "  1) check `profile_for_dispatch is None` AND `hasattr(profile_store, 'bootstrap')`,\n"
        "  2) call `profile_store.bootstrap(...)`,\n"
        "  3) call `jugnu_fetch(task, profile=profile_for_dispatch)`.\n"
        "If this test fails after a runner refactor, verify the "
        "cluster-#4 fix is still wired — otherwise Cloudflare-walled "
        "properties will silently regress to no_body_short_circuit "
        "without residential-proxy escalation."
    )


def test_runner_does_not_call_jugnu_fetch_without_profile_kwarg() -> None:
    """Regression guard — every ``jugnu_fetch(task)`` call site in the
    runner must now pass ``profile=`` so the escalator gate engages.
    A bare ``jugnu_fetch(task)`` call without the kwarg is the exact
    bug shape we just fixed."""
    src = _read_runner_source()
    # Match `jugnu_fetch(...)` calls; reject any whose args don't
    # include `profile=`.
    pattern = re.compile(r"jugnu_fetch\(([^)]*)\)", re.DOTALL)
    matches = pattern.findall(src)
    assert matches, "no jugnu_fetch() call sites found at all"
    bare_calls = [m.strip() for m in matches if "profile" not in m]
    assert not bare_calls, (
        f"runner has {len(bare_calls)} jugnu_fetch() call(s) missing "
        f"the profile= kwarg; the escalator gate won't engage and "
        f"Cloudflare-walled props will regress to no_body_short_circuit. "
        f"Offending args: {bare_calls!r}"
    )


def test_l3_bootstrap_still_runs_as_idempotent_safety_net() -> None:
    """The L3 step still calls ``profile_store.bootstrap`` as a safety
    net for any code path that didn't go through the H4 fallback (e.g.
    a future direct-dispatch path). Confirms the original L3 bootstrap
    line still exists even after our upstream addition."""
    src = _read_runner_source()
    # The L3 bootstrap is the second occurrence of the bootstrap idiom;
    # at minimum the runner source should mention bootstrap >= 2 times.
    n_bootstrap = src.count("profile_store.bootstrap(")
    assert n_bootstrap >= 2, (
        f"Expected at least 2 profile_store.bootstrap() call sites "
        f"in the runner (H4-fallback upstream + L3 safety-net), "
        f"found {n_bootstrap}."
    )
