"""Step 3b — API-response concession capture in ``ma_poc/pms/scraper.py``.

Step 3b (landed 2026-05-24, commit cd03c25) is the fallback that pulls a
concession out of a captured API JSON body when the page-HTML banner regex
(Step 3) found nothing — the 3-17% of properties whose offer lives only in
``leasingSpecial`` / ``bannerText`` / ``specialDisplayText`` and never in the
marketing HTML.

It was dead from the day it landed. The block read::

    _captured = getattr(ctx, "_api_responses", []) or []

but ``ctx`` is not constructed until Step 6, ~350 lines further down — Step 3b
runs BEFORE detection/adapter dispatch by design. So every execution raised
``NameError: name 'ctx' is not defined``, and the block's broad
``except Exception`` swallowed it. ``ruff`` reported it the whole time as
``F821 Undefined name 'ctx'``; nothing else ever did, because a swallowed
NameError is indistinguishable from "no concession in this response".

These tests are the regression guard. They are deliberately end-to-end through
``scrape()`` rather than against a extracted helper: the bug was *wiring*, not
logic — ``extract_api_concession`` itself was always correct and always
unit-tested (``tests/core/test_api_concession_extract.py``). Re-introducing any
undefined/wrong name in that block makes the extraction silently produce
nothing again, which is exactly what ``test_step3b_*_populates_concessions_text``
below catches.

Network seam — see ``ma_poc/conftest.py``: ``scrape()`` reaches the live
internet through the sync ``_probe.probe_get`` curl_cffi seam (Step 4b
detection rescue, F1.5 subpage enrichment), which patching ``get_adapter`` /
``resolve_target`` / ``detect_pms`` does not intercept. The autouse fixture
below stubs that seam with an inert local page.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters.base import AdapterResult
from ma_poc.pms.detector import DetectedPMS, PmsName

# The offer text the API carries. Chosen to be a realistic Knock
# ``leasingSpecial`` value — see the field audit in
# ma_poc/core/api_concession_extract.py's module docstring.
API_OFFER = "APRIL SHOWERS BRING FREE RENT! Move in by 4/30 and get 6 weeks free."

# A DIFFERENT offer, present in the page HTML. Used to pin capture-first
# precedence: Step 3 (banner) must win over Step 3b (API).
HTML_OFFER = "Limited Time Offer! Move in by 6/15 and get 1 month free rent."

_INERT_HTML = (
    "<html><head><title>Test page</title></head>"
    "<body><p>No PMS markers, no floor plans, no rents.</p></body></html>"
)

_BANNER_HTML = (
    "<html><head><title>Test page</title></head>"
    f"<body><div class='banner'>{HTML_OFFER}</div></body></html>"
)


class _InertProbeResponse:
    """Minimal curl_cffi-response stand-in (``.status_code`` / ``.text``)."""

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


def _detected(pms: PmsName = "knock") -> DetectedPMS:
    """A confidently-detected PMS.

    Must not be ``unknown``/``custom``: those open the Step 4b detection
    rescue, which fans out seven ``probe_get`` calls. ``knock`` is both a
    valid ``PmsName`` and apt — ``API_OFFER`` is a real Knock
    ``leasingSpecial`` shape.
    """
    return DetectedPMS(pms=pms, confidence=0.9, evidence=[])


def _units() -> list[dict[str, Any]]:
    """Complete units — keeps the F1.5 subpage-enrichment path (which fires on
    units missing rent OR sqft) from doing anything, and keeps the LLM rescue
    gate shut."""
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


def _fetch_result(
    *,
    body: bytes | None,
    network_log: list[dict[str, Any]] | None = None,
) -> FetchResult:
    return FetchResult(
        url="https://test.com",
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={"content-type": "text/html"},
        render_mode=RenderMode.RENDER,
        final_url="https://test.com",
        attempts=1,
        elapsed_ms=100,
        network_log=network_log or [],
    )


async def _run_scrape(
    *,
    api_responses: list[dict[str, Any]] | None = None,
    fetch_result: FetchResult | None = None,
    adapter_api_responses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Drive ``scrape()`` with detection/resolution/adapter all stubbed.

    ``adapter_api_responses`` defaults to empty so that Step 9b — the LATER
    pass that scans ``adapter_result.api_responses`` — cannot fire. Without
    that isolation a Step 3b assertion would pass even with Step 3b still
    dead, because Step 9b runs the same extractor over a different source.
    """
    from ma_poc.pms import scraper as scraper_mod

    adapter_result = AdapterResult(
        units=_units(),
        tier_used="TIER_1_API",
        api_responses=adapter_api_responses or [],
    )

    with (
        patch("ma_poc.pms.scraper.get_adapter") as mock_get_adapter,
        patch("ma_poc.pms.scraper.resolve_target") as mock_resolve,
        patch("ma_poc.pms.scraper.detect_pms") as mock_detect,
        patch("ma_poc.services.llm_api_rescue.rescue_from_api_responses"),
    ):
        mock_detect.return_value = _detected()
        mock_resolve.return_value = MagicMock(
            resolved_url="https://test.com",
            final_detection=_detected(),
            original_url="https://test.com",
            hop_path=[],
            method="noop",
        )
        mock_adapter = AsyncMock()
        mock_adapter.pms_name = "knock"
        mock_adapter.extract = AsyncMock(return_value=adapter_result)
        mock_get_adapter.return_value = mock_adapter

        return await scraper_mod.scrape(
            "https://test.com",
            api_responses=api_responses,
            fetch_result=fetch_result,
            property_id="TEST-001",
        )


# ── The core regression: Step 3b actually extracts ───────────────────────────


@pytest.mark.asyncio
async def test_step3b_api_responses_arg_populates_concessions_text() -> None:
    """A captured API body carrying ``leasingSpecial`` lands in
    ``concessions_text``.

    Pre-fix this asserted key was absent: ``NameError: ctx`` fired on the first
    statement of the block and the ``except Exception`` swallowed it.

    No page and no fetch_result → ``page_html`` is None → Step 3 (HTML banner)
    and Step 3c (rendered DOM) are both skipped, and Step 9b is starved by the
    empty ``adapter_result.api_responses``. Step 3b is the ONLY path that can
    set this field, so the assertion is unambiguous.
    """
    result = await _run_scrape(
        api_responses=[
            {
                "url": "https://test.com/api/property",
                "body": {
                    "property": {
                        "data": {"leasing": {"leasingSpecial": API_OFFER}}
                    }
                },
                "content_type": "application/json",
            }
        ]
    )

    assert result.get("concessions_text") == API_OFFER, (
        "Step 3b did not extract the concession from the captured API "
        "response. If this regressed, check that the block at "
        "'Step 3b: API-response concession capture' reads a name that is "
        "actually bound at that point in scrape() — ctx does not exist "
        "until Step 6. Run: ruff check ma_poc/pms/scraper.py"
    )
    assert result.get("concession_source") == "API_RESPONSE"


@pytest.mark.asyncio
async def test_step3b_reads_fetch_result_network_log_when_no_arg() -> None:
    """With no explicit ``api_responses``, Step 3b falls back to the RENDER
    fetch's ``network_log`` — the shape production actually supplies.

    ``network_log`` bodies are raw (often truncated) STRINGS, not parsed dicts;
    the block's body loop json-decodes them. This is the branch that carries
    the real 3-17% of properties, so it needs its own coverage: the
    ``api_responses=`` argument is a test/caller convenience.
    """
    result = await _run_scrape(
        fetch_result=_fetch_result(
            body=_INERT_HTML.encode("utf-8"),
            network_log=[
                {
                    "url": "https://test.com/api/floorplans",
                    "status": 200,
                    "content_type": "application/json",
                    # String body, exactly as the fetcher records it.
                    "body": '{"specials": [{"specialDisplayText": "'
                    + API_OFFER
                    + '"}]}',
                }
            ],
        )
    )

    assert result.get("concessions_text") == API_OFFER
    assert result.get("concession_source") == "API_RESPONSE"


@pytest.mark.asyncio
async def test_step3b_picks_longest_meaningful_text_across_responses() -> None:
    """Multiple captures → the longest meaningful offer wins, and junk
    (Wix branding / Yardi empty-state placeholders) is filtered out."""
    result = await _run_scrape(
        api_responses=[
            {
                "url": "https://test.com/api/a",
                "body": {"bannerText": "This website was built on Wix."},
            },
            {"url": "https://test.com/api/b", "body": {"promotion": "1 month free"}},
            {"url": "https://test.com/api/c", "body": {"leasingSpecial": API_OFFER}},
        ]
    )

    assert result.get("concessions_text") == API_OFFER


# ── Precedence and isolation ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_step3b_does_not_override_html_banner() -> None:
    """Capture-first: the marketing-page banner (Step 3) is authoritative when
    present, so Step 3b must not overwrite it."""
    result = await _run_scrape(
        fetch_result=_fetch_result(body=_BANNER_HTML.encode("utf-8")),
        api_responses=[
            {"url": "https://test.com/api/x", "body": {"leasingSpecial": API_OFFER}}
        ],
    )

    captured = result.get("concessions_text") or ""
    # Substring, not equality: Step 3 stores the enclosing CLAUSE WINDOW
    # (±200 chars, sentence-extended) rather than the bare match, so it also
    # sweeps in adjacent visible text such as the <title>. That is Step 3's
    # documented behaviour and not what this test is pinning.
    assert HTML_OFFER in captured
    assert API_OFFER not in captured
    # Step 3 does not tag a source; only 3b/3c do.
    assert result.get("concession_source") != "API_RESPONSE"


@pytest.mark.asyncio
async def test_step3b_leaves_field_unset_when_no_concession_present() -> None:
    """No concession field anywhere → the key stays absent rather than being
    set to an empty string (downstream uses ``.get()`` truthiness)."""
    result = await _run_scrape(
        api_responses=[
            {
                "url": "https://test.com/api/units",
                "body": {"units": [{"beds": 1, "rent": 1200}]},
            }
        ]
    )

    assert not result.get("concessions_text")


@pytest.mark.asyncio
async def test_step3b_survives_malformed_bodies() -> None:
    """Non-dict entries, ``None`` bodies and non-JSON strings are skipped, not
    raised on — the good capture later in the list still wins."""
    result = await _run_scrape(
        api_responses=[
            "not-a-dict",  # type: ignore[list-item]
            {"url": "https://test.com/api/a", "body": None},
            {"url": "https://test.com/api/b", "body": "<html>not json</html>"},
            {"url": "https://test.com/api/c", "body": b"\xff\xfe not json either"},
            {"url": "https://test.com/api/d", "body": {"leasingSpecial": API_OFFER}},
        ]
    )

    assert result.get("concessions_text") == API_OFFER


# ── Static guard for the whole bug class ─────────────────────────────────────


def test_scraper_has_no_undefined_names() -> None:
    """``ruff``'s F821 must stay clean on scraper.py.

    This is the check that would have caught the dead block on day one. The
    file has known, accepted E402/B007 findings, so this asserts on F821
    alone — an undefined name inside a broad ``try/except`` is invisible at
    runtime and silently disables whatever feature it sits in.
    """
    repo_root = Path(__file__).resolve().parents[3]
    target = repo_root / "ma_poc" / "pms" / "scraper.py"
    config = repo_root / "ma_poc" / "pyproject.toml"
    assert target.exists(), target

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--config",
                str(config),
                "--select",
                "F821",
                "--output-format",
                "concise",
                str(target),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        pytest.skip(f"ruff unavailable: {exc}")

    findings = [ln for ln in proc.stdout.splitlines() if "F821" in ln]
    assert not findings, (
        "F821 undefined name(s) in scraper.py — inside a broad try/except "
        "these are swallowed at runtime and silently kill the feature:\n"
        + "\n".join(findings)
    )
