"""Regression test — ``confirm_detection`` must not demote a strong
fingerprint match just because every captured API response is third-party
noise.

``confirm_detection`` was added in commit eb18889 (2026-04-20) as a guard
against URL-based misrouting (Windsor/Mark-Taylor/Vegas case). It demotes
a detected PMS to ``unknown`` if no captured response body passes the
adapter's ``matches_response_body`` predicate.

The symmetric failure mode it created: a real RentCafe property whose
homepage loads via a JS widget, with every captured XHR going to
googletagmanager / maps.googleapis / cloudflare turnstile — none of which
match the RentCafe (Yardi) body shape, even though the page is genuinely
RentCafe. confirm_detection demotes it to ``unknown``, the generic
adapter runs, finds no rent signals on the marketing shell, and the
property fails. On 2026-05-11 this footprint was **558 RentCafe-only
failures**.

This test fails today: confirm_detection demotes RentCafe unconditionally
when all captured bodies are noise. The fix is to either preserve
high-confidence (≥ 0.85) fingerprint detections when no captured body is
a *negative* signal (i.e., none belong to a *different* PMS's shape), or
introduce a tri-state (keep | dual-run | demote) instead of the current
binary check.
"""

from __future__ import annotations


def test_confirm_detection_preserves_rentcafe_when_apis_are_noise_only() -> None:
    """When a strong RentCafe fingerprint match is established and every
    captured API response is third-party noise (CDN, analytics, captcha),
    ``confirm_detection`` should preserve the RentCafe detection.

    The captures listed below are sampled from real production traces of
    properties that succeeded prior to commit eb18889 and have been
    demoting to ``unknown`` since. None of them are a positive RentCafe
    signal but **none are a negative one either** — they are simply
    third-party page widgets unrelated to the unit-data path.

    Failing today: confirm_detection demotes any PMS whose
    ``matches_response_body`` returns False on every captured body,
    regardless of whether those bodies belonged to the PMS in the first
    place. The contract this test enforces: noise is not disconfirmation.
    """
    from ma_poc.pms.detector import (
        _STRATEGY_BY_PMS,
        DetectedPMS,
        confirm_detection,
    )

    initial = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fingerprint_matched:rentcafe", "script_src:cdngeneralmvc.rentcafe.com"],
        pms_client_account_id=None,
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )

    # Real captured-API samples from 2026-05-11 RentCafe-fingerprinted
    # failures (property 10141 wymberlycrossing.com and similar).
    noise_responses = [
        {"url": "https://www.googletagmanager.com/gtm.js?id=GTM-XXXX", "body": "ga('send','pageview')"},
        {"url": "https://maps.googleapis.com/maps/api/place/details/json", "body": '{"results":[]}'},
        {"url": "https://challenges.cloudflare.com/turnstile/v0/api.js", "body": "<html><body>cf</body></html>"},
        {"url": "https://www.hotjar.com/c/heatmap.json", "body": '{"hm":1}'},
        {"url": "https://cmp.osano.com/16A0DbT9yDNIaQkvZ/widget", "body": '{"consent":true}'},
    ]

    result = confirm_detection(initial, noise_responses)

    assert result.pms == "rentcafe", (
        f"confirm_detection demoted a high-confidence RentCafe match "
        f"(conf={initial.confidence}) → {result.pms!r} when ALL captured "
        f"API bodies were CDN/analytics/captcha noise. None of these "
        f"bodies are a *positive* signal for any other PMS — they're "
        f"just third-party widgets.\n\n"
        f"  Initial evidence:    {initial.evidence}\n"
        f"  Demotion evidence:   {result.evidence}\n\n"
        f"Production impact on 2026-05-11: 558 RentCafe-only-matched "
        f"failures sit downstream of this demotion. See "
        f"docs/run_2026_05_11_manual_analysis.md. The fix is to require "
        f"a *negative* signal (a captured body that positively matches a "
        f"different PMS's shape) before demoting, rather than treating "
        f"noise as disconfirmation."
    )


def test_confirm_detection_demotes_when_apis_match_a_different_pms() -> None:
    """Symmetric case: confirm_detection SHOULD demote when captured bodies
    positively match a different PMS than the URL fingerprint suggested.

    This is the original ``eb18889`` use case (Windsor / Mark-Taylor /
    Vegas: URL said RentCafe, captured bodies were Funnel-shaped). It
    documents the boundary we're not asking to remove — only to narrow.

    NOTE: this test is expected to PASS today. It exists alongside the
    failing test above so the fix author has both sides of the invariant
    encoded — "preserve under noise" must not regress "demote under
    cross-PMS evidence".
    """
    from ma_poc.pms.detector import (
        _STRATEGY_BY_PMS,
        DetectedPMS,
        confirm_detection,
    )

    initial = DetectedPMS(
        pms="rentcafe",
        confidence=0.9,
        evidence=["fingerprint_matched:rentcafe"],
        pms_client_account_id=None,
        recommended_strategy=_STRATEGY_BY_PMS["rentcafe"],
    )

    # Body shape lifted from a real Funnel-PMS capture: a parsed dict
    # (the runner's network log capture already JSON-decodes XHR bodies
    # before they reach detector code), with a ``results`` array whose
    # first element carries multiple Funnel-listing keys. This is the
    # canonical Funnel shape that ``_is_funnel_response_body`` accepts.
    funnel_shaped = [
        {
            "url": "https://api.funnelleasing.com/v1/units/availability",
            "body": {
                "results": [
                    {
                        "listingId": "abc-123",
                        "marketRent": 1450,
                        "availabilityDate": "2026-06-01",
                        "unit": "101",
                    }
                ]
            },
        }
    ]

    result = confirm_detection(initial, funnel_shaped)
    # We *don't* assert the exact post-demotion pms — that's an
    # implementation choice (unknown vs funnel). We only assert it is NOT
    # rentcafe, because the captures clearly contradict the URL detection.
    assert result.pms != "rentcafe", (
        "confirm_detection failed to demote a RentCafe detection when the "
        "captured API body is clearly Funnel-shaped. This is the original "
        "use case of the function (commit eb18889) and must continue to "
        "work — see the Windsor/Mark-Taylor/Vegas misrouting case in the "
        "commit message."
    )
