"""Builds the full Chrome-equivalent request header set from an Identity.

Pure function. No I/O. Importable from anywhere.

The header set is the canonical Chrome navigation request shape. Firefox/Safari
identities get an appropriate subset (no Sec-CH-UA*). Identity internal
consistency is enforced at construction time, but this builder defends in depth:
a Firefox identity with sec_ch_ua populated is treated as if sec_ch_ua were None.
"""
from __future__ import annotations

from .stealth import Identity


def chrome_header_set(identity: Identity, *, cold_visit: bool = False) -> dict[str, str]:
    """Build a Chrome-equivalent header set for an HTTP navigation request.

    Args:
        identity: The chosen browser identity.
        cold_visit: If True, sets ``Referer: https://www.google.com/`` and
            ``Sec-Fetch-Site: cross-site``. If False (subsequent navigation),
            no Referer is set and Sec-Fetch-Site is ``same-origin``.

    Returns:
        Dict suitable for passing to httpx/curl_cffi ``headers=``.

    Note:
        ``Sec-Fetch-User: ?1`` indicates a user-initiated top-level navigation.
        All HTTP-path requests in this system are top-level navigations, so
        ``?1`` is always correct here. Do NOT copy this header set to subresource
        requests — the correct value there is to omit the header entirely.
    """
    headers: dict[str, str] = {
        "User-Agent": identity.user_agent,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Accept-Language": identity.accept_language,
        # br (Brotli) must be present — its absence from a Chrome UA is a bot signal.
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Site": "cross-site" if cold_visit else "same-origin",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "max-age=0",
    }

    # Client-hint headers — only Chrome/Edge actually send these.
    # Firefox decided not to implement Client Hints (Intent to Prototype was
    # retracted in 2021); Safari likewise does not send them. Emitting them
    # for non-Chrome identities is itself a detectable inconsistency.
    if identity.browser_family in ("chrome", "edge") and identity.sec_ch_ua is not None:
        headers["Sec-Ch-Ua"] = identity.sec_ch_ua
        headers["Sec-Ch-Ua-Mobile"] = identity.sec_ch_ua_mobile
        if identity.sec_ch_ua_platform is not None:
            headers["Sec-Ch-Ua-Platform"] = identity.sec_ch_ua_platform
        if identity.sec_ch_ua_platform_version is not None:
            headers["Sec-Ch-Ua-Platform-Version"] = identity.sec_ch_ua_platform_version

    if cold_visit:
        # Many bot-management edges weight the Referer signal — a request that
        # arrives with a Google referer is treated as more trustworthy than
        # one with no referer at all on a cold visit.
        headers["Referer"] = "https://www.google.com/"

    return headers
