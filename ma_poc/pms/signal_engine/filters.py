"""Centralised URL and signal filtering for the scraper pipeline.

All infrastructure/third-party API URL detection lives here so every
call site (scraper hop queue, profile persistence, signal qualification)
uses one consistent definition.  Import ``is_infra_url`` — never copy the
domain list to another module.
"""

from __future__ import annotations

__all__ = ["is_infra_url", "INFRA_API_DOMAINS"]

# Domains / substrings whose URLs are third-party infrastructure that
# (a) cannot return apartment unit data, or
# (b) cannot be replayed without the originating browser session.
#
# Matching is substring — a URL containing any of these strings is infra.
# Keep entries lowercase.
INFRA_API_DOMAINS: tuple[str, ...] = (
    # AWS
    "execute-api.",          # AWS API Gateway (*.execute-api.*.amazonaws.com)
    "amazonaws.com/",        # S3 object paths
    "s3.amazonaws.com",
    "cloudfront.net",        # AWS CloudFront CDN assets
    # Supabase
    "supabase.co",
    "supabase.io",
    # Google / Firebase
    "googleapis.com",
    "firebase.com",
    "firebaseio.com",
    # HERE Location Services (maps / geocoding — returns POIs, not apartments)
    "hereapi.com",
    "here.com/v1/",
    "browse.search.hereapi",
    # Media / virtual-tour APIs
    "matterport.com",
    # Marketing / analytics / CRO services
    "omappapi.com",
    "omappa.com",
    "theconversioncloud.com",
    "nestiolistings.com/api/",  # Funnel/Nestio neighbourhood config (not units)
    # SightMap *internal* config — the unit-data endpoint is NOT infra
    "sightmap.com/app/api/",
    # Cloudinary / media CDNs
    "cloudinary.com",
    "imgix.net",
    "fastly.net",
)


def is_infra_url(url: str) -> bool:
    """Return True when *url* is a third-party infrastructure endpoint.

    These endpoints either never serve apartment unit data or require a
    live browser session to authenticate and cannot be replayed as a hop.

    Args:
        url: Absolute URL string to test.

    Returns:
        ``True`` if the URL matches any known infra domain substring.
    """
    lower = url.lower()
    return any(domain in lower for domain in INFRA_API_DOMAINS)
