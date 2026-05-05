"""Cookie-jar wrapper for provider _single_attempt calls.

Centralises the read-before / write-after cookie persistence so it cannot
drift between providers.
"""
from __future__ import annotations

from urllib.parse import urlparse

from .cookie_jar import PropertyCookieJar
from .stealth import IdentityPool


def jar_for(task: object, identity_pool: IdentityPool) -> tuple[PropertyCookieJar, str, dict[str, str]]:
    """Return (jar, host, cookies_to_send) for this task.

    Caller is responsible for invoking jar.update_from_response(host, resp.cookies)
    after a successful response.
    """
    host = urlparse(task.url).netloc  # type: ignore[union-attr]
    slot = identity_pool.current_slot(task.property_id)  # type: ignore[union-attr]
    jar = PropertyCookieJar(task.property_id, slot)  # type: ignore[union-attr]
    return jar, host, jar.cookies_for_host(host)
