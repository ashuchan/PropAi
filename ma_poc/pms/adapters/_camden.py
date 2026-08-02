"""Deprecated Camden landing-page preview parser.

The landing-page ``suggestedFloorPlans`` array is not a physical-unit roster.
It contains one representative per plan plus a list of other public labels;
their rents, dates and native ids live only on each exact floor-plan detail
page.  The former parser crossed every label with the representative values,
which fabricated Camden rows and misassigned multi-community ids.

Host/blob detection remains for compatibility.  Unit extraction deliberately
returns no rows; the registered :mod:`ma_poc.pms.adapters.camden` adapter owns
the bounded, identity-bound detail walk.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

_CAMDEN_HOST_RE = re.compile(r"(?:^|\.)camdenliving\.com$", re.IGNORECASE)


def is_camden_host(url: str | None) -> bool:
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").casefold().rstrip(".")
    except (TypeError, ValueError):
        return False
    return bool(_CAMDEN_HOST_RE.search(host))


def detect_camden_next_data(html_or_blob: Any) -> bool:
    if isinstance(html_or_blob, dict):
        plans = (
            html_or_blob.get("props", {})
            .get("pageProps", {})
            .get("suggestedFloorPlans")
        )
        return isinstance(plans, list)
    if not isinstance(html_or_blob, str) or len(html_or_blob) < 1000:
        return False
    return 'id="__NEXT_DATA__"' in html_or_blob and "suggestedFloorPlans" in html_or_blob


def parse_camden_next_data(
    html_or_blob: Any,
    source_url: str = "",
) -> list[dict[str, Any]]:
    """Never expand Camden's representative preview into physical rows."""

    del html_or_blob, source_url
    return []
