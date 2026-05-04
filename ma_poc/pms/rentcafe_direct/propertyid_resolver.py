"""F4 — propertyId resolver via aggregator search.

Disambiguation order (top-to-bottom; first hit wins):

  1. Exact normalized-name match within same zip          → EXACT
  2. Single name-prefix (first 3 words) match in same zip → ZIP_AND_NAME_PREFIX
  3. Multiple prefix matches: tie-break on vanity_domain  → ZIP_AND_NAME_PREFIX
  4. Single same-zip candidate, any name                  → ZIP_ONLY
  5. No same-zip candidate, or ambiguous with no breaker  → NONE

The aggregator endpoint URL and response shape are populated from
Phase 0 research (see ``docs/RENTCAFE_DIRECT_RESEARCH.md``). The default
URL embedded here is a placeholder — VERIFY against research before
production runs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

# Endpoint — UPDATE from Phase 0 research before merging. The actual
# RentCafe aggregator search endpoint may differ (auth, params, host).
_AGGREGATOR_SEARCH_URL = "https://www.rentcafe.com/api/search"


@dataclass(frozen=True)
class ResolveResult:
    """Outcome of a propertyId resolution attempt.

    Attributes
    ----------
    property_id
        Resolved propertyId, or ``None`` on failure.
    matched_name
        Candidate name that won (None on failure).
    matched_zip
        Candidate zip that won (None on failure).
    confidence
        Disambiguation tier that produced the match.
    raw_search_response
        Raw aggregator payload for debugging (None when no payload was
        retrievable, e.g. network error).
    failure_reason
        Machine-readable failure label populated when ``property_id is
        None``: ``SEARCH_NETWORK_ERROR``, ``SEARCH_HTTP_ERROR_<status>``,
        ``SEARCH_PARSE_ERROR``, ``NO_RESULTS``, ``NO_ZIP_MATCH``,
        ``AMBIGUOUS_NO_TIE_BREAKER``.
    """

    property_id: str | None
    matched_name: str | None
    matched_zip: str | None
    confidence: Literal["EXACT", "ZIP_AND_NAME_PREFIX", "ZIP_ONLY", "NONE"]
    raw_search_response: dict[str, Any] | None
    failure_reason: str | None


def _normalize_name(s: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace."""
    if not s:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s.lower())).strip()


def _normalize_zip(z: str | None) -> str:
    """Take the first 5-digit segment of a zip (handles ``"94107-1234"``)."""
    if not z:
        return ""
    return z.strip().split("-")[0][:5]


def _name_prefix(s: str | None, n: int = 3) -> str:
    """First ``n`` whitespace-separated tokens of normalized name."""
    return " ".join(_normalize_name(s).split()[:n])


def _name_prefix_overlap(target: str, candidate: str, n: int = 3) -> bool:
    """True when target's first-``n``-words and candidate's first-``n``-words
    share a word-bounded prefix in either direction.

    Handles the common case where the CSV name is shorter than the
    aggregator-listed name (e.g. ``"Cloister Gardens"`` matches both
    ``"Cloister Gardens North"`` and ``"Cloister Gardens South"`` —
    the vanity_domain breaks the tie). Strict equality is also a
    match.
    """
    t = _name_prefix(target, n)
    c = _name_prefix(candidate, n)
    if not t or not c:
        return False
    if t == c:
        return True
    return c.startswith(t + " ") or t.startswith(c + " ")


async def resolve_property_id(
    property_name: str,
    city: str,
    zip_code: str,
    vanity_domain: str | None = None,
    *,
    timeout_s: float = 15.0,
    client: httpx.AsyncClient | None = None,
) -> ResolveResult:
    """Resolve (name, city, zip, vanity_domain) → RentCafe propertyId.

    Parameters
    ----------
    property_name : str
        Property name from the CSV row.
    city : str
        City from the CSV row (used to scope the search query).
    zip_code : str
        Zip from the CSV row. Normalized to first 5 digits.
    vanity_domain : str | None
        Per-property vanity domain (e.g. ``windsorprop.rentcafe.com``)
        used as a tie-breaker when multiple same-zip candidates share a
        name prefix.
    timeout_s : float
        HTTP timeout for the search call.
    client : httpx.AsyncClient | None
        Optional client to reuse (e.g. test injection). When None a
        single-shot client is created and closed in the ``finally``.

    Returns
    -------
    ResolveResult
        ``property_id`` populated on success, ``None`` with a
        ``failure_reason`` on failure. Never raises.
    """
    target_zip = _normalize_zip(zip_code)
    target_name_norm = _normalize_name(property_name)

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)

    try:
        try:
            resp = await client.get(
                _AGGREGATOR_SEARCH_URL,
                params={"q": f"{property_name} {city} {target_zip}".strip()},
            )
        except (httpx.NetworkError, httpx.TimeoutException):
            return ResolveResult(None, None, None, "NONE", None, "SEARCH_NETWORK_ERROR")
        except Exception:
            # Defensive — never raise from this resolver. Any unanticipated
            # exception is reported as a network error so the runner falls
            # back (H4).
            return ResolveResult(None, None, None, "NONE", None, "SEARCH_NETWORK_ERROR")

        if resp.status_code != 200:
            return ResolveResult(
                None, None, None, "NONE", None, f"SEARCH_HTTP_ERROR_{resp.status_code}"
            )

        try:
            payload = resp.json()
        except Exception:
            return ResolveResult(None, None, None, "NONE", None, "SEARCH_PARSE_ERROR")

        # Adapt to actual shape from Phase 0 — assumes
        # {"results": [{"property_id", "name", "zip", "vanity_domain"}]}.
        candidates: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            raw = payload.get("results", [])
            if isinstance(raw, list):
                candidates = [c for c in raw if isinstance(c, dict)]

        if not candidates:
            return ResolveResult(None, None, None, "NONE", payload, "NO_RESULTS")

        same_zip = [
            c for c in candidates if _normalize_zip(c.get("zip", "")) == target_zip
        ]
        if not same_zip:
            return ResolveResult(None, None, None, "NONE", payload, "NO_ZIP_MATCH")

        # 1. EXACT — same zip, exact normalized-name match.
        for c in same_zip:
            if _normalize_name(c.get("name", "")) == target_name_norm:
                return ResolveResult(
                    str(c["property_id"]),
                    c.get("name"),
                    _normalize_zip(c.get("zip", "")),
                    "EXACT",
                    payload,
                    None,
                )

        # 2. ZIP_AND_NAME_PREFIX — single same-zip candidate whose name
        # shares a word-bounded prefix with the target. Handles the
        # ``"Cloister Gardens"`` → ``"Cloister Gardens North/South"``
        # case where the CSV name is shorter than the aggregator entry.
        prefix_matches = [
            c
            for c in same_zip
            if _name_prefix_overlap(property_name, c.get("name", ""))
        ]
        if len(prefix_matches) == 1:
            c = prefix_matches[0]
            return ResolveResult(
                str(c["property_id"]),
                c.get("name"),
                _normalize_zip(c.get("zip", "")),
                "ZIP_AND_NAME_PREFIX",
                payload,
                None,
            )

        # 3. Multiple prefix matches — vanity_domain tie-breaker.
        if len(prefix_matches) > 1 and vanity_domain:
            vd_low = vanity_domain.lower()
            vd_match = [
                c
                for c in prefix_matches
                if vd_low in (c.get("vanity_domain", "") or "").lower()
            ]
            if len(vd_match) == 1:
                c = vd_match[0]
                return ResolveResult(
                    str(c["property_id"]),
                    c.get("name"),
                    _normalize_zip(c.get("zip", "")),
                    "ZIP_AND_NAME_PREFIX",
                    payload,
                    None,
                )
            return ResolveResult(
                None, None, None, "NONE", payload, "AMBIGUOUS_NO_TIE_BREAKER"
            )

        # 4. ZIP_ONLY — single same-zip candidate regardless of name.
        if len(same_zip) == 1:
            c = same_zip[0]
            return ResolveResult(
                str(c["property_id"]),
                c.get("name"),
                _normalize_zip(c.get("zip", "")),
                "ZIP_ONLY",
                payload,
                None,
            )

        # Multiple same-zip candidates, no name-prefix match, no vanity_domain breaker.
        return ResolveResult(
            None, None, None, "NONE", payload, "AMBIGUOUS_NO_TIE_BREAKER"
        )
    finally:
        if own_client:
            await client.aclose()
