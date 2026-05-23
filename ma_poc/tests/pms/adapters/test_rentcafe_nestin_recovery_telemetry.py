"""Per-URL telemetry + CF-challenge-shell fallback in the RentCafe Nestin
recovery path.

Two diagnostic capabilities covered here:

1. **Per-detail-page telemetry** — every detail URL fetched by
   ``recover_rentcafe_nestin_per_plan`` emits one
   ``extract.tier_attempted`` event with ``tier_key=
   "rentcafe:nestin_detail_fetch"``, carrying status + body_len +
   applyga_count + outcome. Lets the diagnostic playbook §20.12 / Q15
   answer "how did each detail fetch terminate?" from events.jsonl
   alone, with no live page re-fetch needed.

2. **CF-shell sanity check + probe_get retry** — when
   ``page.evaluate(fetch)`` returns 200 + a body that looks like a
   Cloudflare interstitial (no unit-row markers, has
   ``challenge-platform`` / ``__cf_chl_`` markers, < 5KB), the loop
   retries that URL via ``_probe_detail_url_fresh`` (curl_cffi chrome120
   with cleared clearance cookies). The replacement body's unit rows
   are parsed normally.

The motivating production case is PID 232316 Panton Mill Station: the
in-browser fetch of ``/floorplans/the-beech`` returned a CF challenge
shell on cloud egress while ``/the-linden`` cleared cleanly — only 1
of 5 expected units was emitted, with no signal in events.jsonl
explaining the loss.
"""

from __future__ import annotations

from typing import Any

import pytest

from ma_poc.pms.adapters._rentcafe_nestin import (
    _body_is_cf_challenge_shell,
    _detail_body_has_unit_signals,
    recover_rentcafe_nestin_per_plan,
)

# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


# Real-world shape from PID 232316 detail page (one applyGAClick button).
_NESTIN_DETAIL_TEMPLATE = """<!DOCTYPE html>
<html><head><link rel="icon" href="https://resource.rentcafe.com/favicon.ico"></head>
<body>
<h1>{plan_name}</h1>
<a data-selenium-id="Select_001" class="btn btn-primary" id="{unit}"
   onclick="applyGAClick('{plan_name}', '1 Bed(s)', '{sqft}', '{rent_lo}.00', '{rent_hi}.00', '{unit}' )"
   href="https://x.securecafe.com/oleapplication.aspx?id={fp_id}">Apply Now</a>
<script type="text/javascript">
  function applyGAClick(fpName, fpSize, fpSqft, fpMinRent, fpMaxRent, fpUnit) {{ }}
</script>
</body></html>
"""


def _nestin_detail(plan: str, unit: str, rent_lo: int = 1791,
                   rent_hi: int = 2358, sqft: str = "765",
                   fp_id: str = "4130104") -> str:
    return _NESTIN_DETAIL_TEMPLATE.format(
        plan_name=plan, unit=unit, rent_lo=rent_lo, rent_hi=rent_hi,
        sqft=sqft, fp_id=fp_id,
    )


_NESTIN_INDEX = """<!DOCTYPE html>
<html><head><link rel="icon" href="https://resource.rentcafe.com/favicon.ico"></head>
<body>
<a href="/floorplans/the-linden">The Linden</a>
<a href="/floorplans/the-beech">The Beech</a>
</body></html>
"""


# Cloudflare challenge interstitial — a 200 OK with no unit-row markup and
# the ``challenge-platform`` marker. Total size <5KB so it falls inside the
# CF-shell heuristic. Modeled on real CF JS-challenge HTML.
_CF_SHELL = """<!DOCTYPE html><html><head><title>Just a moment...</title>
<meta http-equiv="refresh" content="3"></head>
<body>
<script>(function(){var _cf_chl_opt = {};})();</script>
<noscript>Please enable JavaScript.</noscript>
<script src="/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page/v1?ray=abc"></script>
</body></html>
"""


class _Resp:
    """``page.evaluate(fetch)`` response shim (status_code + text)."""

    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self.text = text


class _FakePage:
    """Playwright Page stub. ``page.evaluate`` returns whatever the
    URL-keyed dispatch table supplies; URLs not in the table return a
    0-status empty response."""

    def __init__(self, responses: dict[str, _Resp]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def evaluate(self, _js: str, url: str) -> dict[str, Any]:
        self.calls.append(url)
        r = self._responses.get(url)
        if r is None:
            return {"status": 0, "text": ""}
        return {"status": r.status_code, "text": r.text}


# ──────────────────────────────────────────────────────────────────────
# Per-detail-page telemetry
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_telemetry_event_per_detail_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each detail URL fetched produces exactly one
    ``rentcafe:nestin_detail_fetch`` event with the correct outcome."""
    captured: list[dict[str, Any]] = []

    def _fake_emit(_kind: Any, property_id: str, **fields: Any) -> None:
        captured.append({"property_id": property_id, **fields})

    from ma_poc.observability import events as ev_mod
    monkeypatch.setattr(ev_mod, "emit", _fake_emit)

    page = _FakePage(
        {
            "https://x.com/floorplans/the-linden": _Resp(
                200, _nestin_detail("The Linden", "410", rent_lo=1791)
            ),
            "https://x.com/floorplans/the-beech": _Resp(
                200, _nestin_detail("The Beech", "406", rent_lo=1922)
            ),
        }
    )
    units, _source = await recover_rentcafe_nestin_per_plan(
        _NESTIN_INDEX,
        "https://x.com/",
        page=page,
        pid_for_log="232316",
    )

    assert len(units) == 2
    detail_events = [
        e for e in captured
        if e.get("tier_key") == "rentcafe:nestin_detail_fetch"
        and e.get("outcome") == "ok"
    ]
    assert len(detail_events) == 2, f"expected 2 detail events, got {captured}"
    assert all(e.get("property_id") == "232316" for e in detail_events)
    assert {e.get("detail_url") for e in detail_events} == {
        "https://x.com/floorplans/the-linden",
        "https://x.com/floorplans/the-beech",
    }


@pytest.mark.asyncio
async def test_telemetry_marks_silent_empty_parser_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """200 OK with body that contains no unit-row markers and isn't a
    CF shell (>5KB filler) → ``outcome=parser_silent_empty``. Demonstrates
    the diagnostic playbook §20.12 Q15 query is answerable from
    events.jsonl alone for the Nestin adapter."""
    captured: list[dict[str, Any]] = []

    def _fake_emit(_kind: Any, property_id: str, **fields: Any) -> None:
        captured.append({"property_id": property_id, **fields})

    from ma_poc.observability import events as ev_mod
    monkeypatch.setattr(ev_mod, "emit", _fake_emit)

    bare_body = "<html><body>" + ("plain content " * 500) + "</body></html>"
    assert len(bare_body) > 5000  # too big to trigger the CF-shell heuristic

    page = _FakePage(
        {"https://x.com/floorplans/empty-plan": _Resp(200, bare_body)}
    )
    landing = (
        '<html><head><link rel="icon" href="https://resource.rentcafe.com/favicon.ico">'
        '</head><body><a href="/floorplans/empty-plan">Empty</a></body></html>'
    )
    units, _src = await recover_rentcafe_nestin_per_plan(
        landing, "https://x.com/", page=page, pid_for_log="P1",
    )

    assert units == []
    silent_empty = [
        e for e in captured
        if e.get("outcome") == "parser_silent_empty"
        and e.get("tier_key") == "rentcafe:nestin_detail_fetch"
    ]
    assert len(silent_empty) == 1


# ──────────────────────────────────────────────────────────────────────
# CF challenge interstitial detection + probe_get fallback
# ──────────────────────────────────────────────────────────────────────


def test_cf_shell_heuristic_identifies_real_challenge_html() -> None:
    """The CF interstitial shell triggers the heuristic."""
    assert _body_is_cf_challenge_shell(_CF_SHELL)


def test_cf_shell_heuristic_does_not_flag_real_unit_page() -> None:
    """A real Nestin detail page must NOT be classified as a CF shell."""
    assert not _body_is_cf_challenge_shell(_nestin_detail("The Linden", "410"))


def test_unit_signal_detector_distinguishes_real_unit_page_from_shell() -> None:
    """``_detail_body_has_unit_signals`` should be the decision gate for
    "should we retry this body via probe_get?" — it must True on a real
    unit page and False on both a CF shell and an empty body."""
    assert _detail_body_has_unit_signals(_nestin_detail("X", "100"))
    assert not _detail_body_has_unit_signals(_CF_SHELL)
    assert not _detail_body_has_unit_signals("")


@pytest.mark.asyncio
async def test_cf_shell_response_triggers_probe_get_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the in-browser fetch returns a CF shell, the probe_get
    fallback runs for THAT URL with cleared clearance cookies and the
    second body's unit rows are parsed."""
    page = _FakePage(
        {"https://x.com/floorplans/the-linden": _Resp(200, _CF_SHELL)}
    )

    probe_calls: list[str] = []

    # ``*_args, **_kw`` swallow the ``ctx=`` kwarg the production caller
    # passes after the 2026-05-23 proxy_gate threading. The fake doesn't
    # need ctx to satisfy the URL-list assertion.
    async def _fake_probe_fresh(url: str, *_args, **_kw):  # type: ignore[no-untyped-def]
        probe_calls.append(url)
        from ma_poc.pms.adapters._rentcafe_nestin import _PageFetchResp
        return _PageFetchResp(200, _nestin_detail("The Linden", "410", rent_lo=1791))

    import ma_poc.pms.adapters._rentcafe_nestin as nestin_mod
    monkeypatch.setattr(nestin_mod, "_probe_detail_url_fresh", _fake_probe_fresh)

    landing = (
        '<html><head><link rel="icon" href="https://resource.rentcafe.com/favicon.ico">'
        '</head><body><a href="/floorplans/the-linden">Linden</a></body></html>'
    )
    units, _src = await recover_rentcafe_nestin_per_plan(
        landing, "https://x.com/", page=page, pid_for_log="P2",
    )

    assert probe_calls == ["https://x.com/floorplans/the-linden"]
    assert len(units) == 1
    assert units[0]["unit_number"] == "410"
    assert units[0]["market_rent_low"] == 1791
