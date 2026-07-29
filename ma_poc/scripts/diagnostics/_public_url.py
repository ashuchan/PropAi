"""One normalization contract for the public marketing URL of a property.

The seeded ``website`` column is not guaranteed to carry a URL scheme: in the
2026-07-27 Canary run 374 of 4,982 rows are bare marketing hosts such as
``www.glenbrook-apts.com``.  The production fetch path adds the scheme itself
(``ma_poc.pms.scraper._normalize_url``), so those rows scrape and get verdicts
like any other.  Every offline consumer of a run must therefore use the same
tolerant rule, or it will reject rows the pipeline itself accepted.

Relative paths, fragments and arbitrary text remain invalid: this normalizer
only accepts host-like values with a dot.
"""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_public_url(raw_url: str) -> str | None:
    """Return an absolute public URL, adding HTTPS for a bare marketing host.

    Args:
        raw_url: The raw ``website``/``url`` value from a run row or seed CSV.

    Returns:
        An absolute ``http``/``https`` URL, or ``None`` when the value cannot
        be a public starting route (empty, relative, non-web scheme, or a
        host without a dot).
    """
    value = str(raw_url or "").strip()
    if not value or value.startswith(("/", "#")) or any(char.isspace() for char in value):
        return None
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    if parsed.scheme or parsed.netloc:
        return None
    normalized = urlparse(f"https://{value}")
    host = normalized.hostname or ""
    if not normalized.netloc or "." not in host:
        return None
    return normalized.geturl()
