"""Seed warm profiles from agent-discovered navigation steps.

WHY THIS EXISTS
---------------
Browser-agent probing finds the exact URL that serves a property's
per-apartment roster — including surfaces no vendor adapter would ever
locate. Real examples discovered on 2026-07-25:

    https://jcmliving.com/wp-json/hw/v1/floorplans/cardinal-hill
    https://iconpropmgtbrokerservice.appfolio.com/listings?filters%5B…%5D
    https://www.availability.fortresstech.io/unit-availability/01b64b47-…
    https://api.ws.realpage.com/v2/property/1736910/units?available=true

Writing a vendor adapter for each of those is not worth it — several serve
ONE property. But the pipeline already consumes exactly this, at the highest
possible priority (``pms/scraper.py``)::

    profile_top.append((wpu, _LLM_HINT_SCORE + 1, "profile:winning_page_url"))
    # "Highest possible score so it always lands first."

So a probe-discovered URL persisted into ``profile.navigation`` is fetched
FIRST on the next run, with no adapter and no new code in the hot path. The
expensive discovery happens once; every subsequent daily run replays it
deterministically.

THE VERIFICATION GATE IS NOT OPTIONAL
-------------------------------------
``winning_page_url`` occupies the top hop slot. A wrong value there costs a
wasted fetch on every future run, and there is a real invalidation path that
has to notice and undo it. Measured on the first 78 candidates: only 41
(53%) actually re-fetch to something carrying a roster. The rest were HTTP
400/401/403 (session- or auth-bound API calls), 429 (rate-limited), or
returned 200 with no roster evidence (SPA shells, or the agent overstating
what it reached).

So every URL is re-fetched and checked for per-apartment evidence before it
is written. Unverified candidates are reported, never persisted.

USAGE
-----
    # dry run (default) — verify and report, write nothing
    python -m ma_poc.scripts.seed_profiles_from_probes --probes probes.json

    # actually persist
    python -m ma_poc.scripts.seed_profiles_from_probes --probes probes.json --commit

``probes.json`` is a list of objects carrying at least ``canonical_id`` and
one of ``deepest_url`` / ``url``; ``classification`` is honoured when present
so ceiling/blocked verdicts are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Verdicts that assert there is nothing to reach — never seed from these.
_NON_SEEDABLE = frozenset(
    {
        "TRUE_CEILING_PLAN_ONLY",
        "NO_UNIT_SURFACE",
        "BLOCKED_OR_DEAD",
        "UNCLEAR",
    }
)

#: Markup that indicates a fetched surface carries PER-APARTMENT rows.
#: Deliberately vendor-plural — the whole point is surfaces we have no
#: adapter for.
_ROSTER_MARKUP = re.compile(
    r"unit-container|AvailUnitRow|applyGAClick|fp-units-table|option-row|"
    r"unit-card|listing-item|jd-fp-unit|par-units",
    re.IGNORECASE,
)
#: JSON shape carrying a per-unit identity key.
_ROSTER_JSON = re.compile(
    r'"(?:unitId|unit_id|unitNumber|unit_number|unitName|unit_name)"\s*:',
    re.IGNORECASE,
)


def url_serves_a_roster(body: str) -> bool:
    """True when *body* shows per-apartment evidence, HTML or JSON."""
    if not body:
        return False
    return bool(_ROSTER_MARKUP.search(body) or _ROSTER_JSON.search(body))


def is_seedable_candidate(rec: dict[str, Any]) -> str | None:
    """Return the URL worth verifying for *rec*, or None.

    Rejects ceiling/blocked verdicts, non-http values, and anything with
    embedded whitespace — several agents return prose ("STEP 1 — GET …")
    in the navigation field, and prose is not a URL.
    """
    if str(rec.get("classification") or "") in _NON_SEEDABLE:
        return None
    raw = rec.get("deepest_url") or rec.get("url") or ""
    url = str(raw).strip()
    if not url.startswith("http") or " " in url:
        return None
    return url


def verify(url: str, *, timeout: int = 25) -> tuple[bool, str]:
    """Re-fetch *url* and decide whether it may be persisted.

    Returns ``(ok, reason)``. Never raises — a candidate that errors is
    simply not seedable.
    """
    from ma_poc.pms.adapters._probe import probe_get

    try:
        resp = probe_get(url, timeout=timeout, unlocker=False, proxies={}, verify=False)
    except Exception as exc:  # noqa: BLE001 — any failure means "do not seed"
        return False, f"fetch_error:{type(exc).__name__}"
    status = int(getattr(resp, "status_code", 0) or 0)
    if status != 200:
        return False, f"http_{status}"
    if not url_serves_a_roster(getattr(resp, "text", "") or ""):
        return False, "no_roster_evidence"
    return True, "ok"


def seed_profile(store: Any, canonical_id: str, url: str, *, commit: bool) -> str:
    """Persist *url* as the property's winning_page_url. Returns an outcome.

    Never downgrades a profile that already has a DIFFERENT learned winner —
    a value the pipeline earned from a real extraction outranks one we
    discovered by probing, and silently replacing it would erase learning.
    The probe URL is still recorded in ``availability_links`` so the hop
    layer can try it second.
    """
    profile = store.load(canonical_id)
    if profile is None:
        return "no_profile"

    nav = profile.navigation
    existing = getattr(nav, "winning_page_url", None)
    if existing and existing != url:
        if url in (nav.availability_links or []):
            return "already_known"
        if commit:
            nav.availability_links = [*(nav.availability_links or []), url]
            profile.updated_by = "PROBE_SEED"
            store.save(profile)
        return "added_as_availability_link"

    if existing == url:
        return "unchanged"

    if commit:
        nav.winning_page_url = url
        profile.updated_by = "PROBE_SEED"
        store.save(profile)
    return "seeded"


def run(probes: list[dict[str, Any]], store: Any, *, commit: bool) -> dict[str, Any]:
    """Verify and (optionally) persist. Returns a summary dict."""
    outcomes: dict[str, int] = {}
    rejected: list[dict[str, str]] = []
    seeded: list[dict[str, str]] = []

    for rec in probes:
        cid = str(rec.get("canonical_id") or "").strip()
        url = is_seedable_candidate(rec)
        if not url:
            outcomes["not_a_candidate"] = outcomes.get("not_a_candidate", 0) + 1
            continue
        ok, reason = verify(url)
        if not ok:
            outcomes[f"rejected:{reason}"] = outcomes.get(f"rejected:{reason}", 0) + 1
            rejected.append({"canonical_id": cid, "url": url, "reason": reason})
            continue
        if not cid:
            outcomes["verified_but_no_canonical_id"] = (
                outcomes.get("verified_but_no_canonical_id", 0) + 1
            )
            continue
        result = seed_profile(store, cid, url, commit=commit)
        outcomes[result] = outcomes.get(result, 0) + 1
        if result in {"seeded", "added_as_availability_link"}:
            seeded.append({"canonical_id": cid, "url": url, "outcome": result})

    return {
        "total": len(probes),
        "committed": commit,
        "outcomes": outcomes,
        "seeded": seeded,
        "rejected": rejected,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--probes", required=True, type=Path, help="probe results JSON (list)")
    ap.add_argument("--profiles-dir", type=Path, default=Path("config/profiles"))
    ap.add_argument(
        "--commit",
        action="store_true",
        help="actually write profiles (default is a dry run that only verifies)",
    )
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    data = json.loads(args.probes.read_text(encoding="utf-8"))
    probes = data if isinstance(data, list) else data.get("per_property") or []

    # ``services.profile_store`` imports its models with a BARE
    # ``from models.scrape_profile import ...``, so it only resolves when
    # ``ma_poc/`` itself is on sys.path — it cannot be imported as
    # ``ma_poc.services.profile_store`` from the repo root. (Same root cause
    # as the long-standing tests/services/test_phase_a_correctness.py
    # ``no_bare_imports`` failure.) Put the package dir on the path so this
    # script runs from either location rather than making the caller care.
    _pkg_root = Path(__file__).resolve().parent.parent
    if str(_pkg_root) not in sys.path:
        sys.path.insert(0, str(_pkg_root))
    from services.profile_store import ProfileStore  # noqa: PLC0415

    summary = run(probes, ProfileStore(args.profiles_dir), commit=args.commit)

    log.info("probes=%d commit=%s", summary["total"], summary["committed"])
    for k, v in sorted(summary["outcomes"].items(), key=lambda kv: -kv[1]):
        log.info("  %5d  %s", v, k)
    if not args.commit:
        log.info("\nDRY RUN — nothing written. Re-run with --commit to persist.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
