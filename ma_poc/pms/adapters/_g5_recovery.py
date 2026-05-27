"""G5 universal-recovery path (2026-05-24).

When the primary adapter returns 0 units AND the homepage HTML has a
g5-cl-* URN, try the G5 GraphQL API explicitly. Closes the misroute
cases where the detector picked Knock/RentCafe/RealPage but the
property's real data lives behind the G5 inventory API.

Pattern observed in the prod-vs-canary gap (2026-05-23):
  * 3 of 13 Knock-misrouted P1 props in the TIER_1_API cohort had
    g5-cl-* URNs in their homepage HTML
  * 1 of 18 FAILED P1 props also had a G5 URN
  * Several SightMap and TIER_3_DOM cohort props additionally surface
    G5 URNs when their primary adapters fail

Wired into ``recover_universal_embed`` as the 6th recovery path
(after appfolio / leaseleads / portal_hop / generic_dom / sightmap_subpage).

Net effect: any property whose body contains a g5-cl-* URN gets the
G5 adapter as a free try, regardless of detector choice.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Page

    from ma_poc.pms.adapters.base import AdapterContext

log = logging.getLogger(__name__)


def _decode_body(raw: object) -> str:
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        try:
            return raw.decode("utf-8", "replace")
        except Exception:
            return ""
    if isinstance(raw, str):
        return raw
    return ""


def _homepage_html(ctx: "AdapterContext") -> str:
    fr = getattr(ctx, "fetch_result", None)
    if fr is None:
        return ""
    return _decode_body(getattr(fr, "body", None))


async def recover_g5(
    page: "Page | None",
    ctx: "AdapterContext",
) -> list[dict[str, str]]:
    """Run G5Adapter as a recovery path when:
      * Other adapters returned 0 units (caller's responsibility)
      * Homepage HTML contains at least one g5-cl-* URN candidate

    Returns ``[]`` when no G5 URN is present in the body, or when
    G5Adapter itself yields no units (including all candidate URNs
    returning empty apartmentComplex). Never raises.

    The G5Adapter already does URN-candidate retry + curl_cffi-with-
    httpx-fallback as of commit 642c41b, so this wrapper is small.
    """
    body = _homepage_html(ctx)
    if not body or "g5-cl" not in body.lower():
        return []

    # Cheap early-exit: only fire if we have at least one URN candidate
    # to try. find_g5_urn_candidates is pure-string-regex (no I/O).
    try:
        from ma_poc.pms.adapters.g5 import find_g5_urn_candidates

        if not find_g5_urn_candidates(body):
            return []
    except Exception:
        return []

    try:
        from ma_poc.pms.adapters.g5 import G5Adapter

        sm = G5Adapter()
        result = await sm.extract(page, ctx)  # type: ignore[arg-type]
    except Exception as exc:
        log.warning("g5-recovery: G5Adapter raised err=%s", exc)
        return []

    units = list(result.units or [])
    if not units:
        return []

    # Mark the recovery path so reporting can distinguish this from the
    # primary G5 detection. Preserve any tier the adapter set itself
    # (e.g. TIER_2_API_G5_APOLLO when Apollo fallback ran).
    for u in units:
        if isinstance(u, dict) and not u.get("extraction_tier"):
            u["extraction_tier"] = "TIER_1_API_G5_RECOVERY"

    return units
