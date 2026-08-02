"""Compact, non-secret provenance for the response that produced units."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_SENSITIVE_QUERY_PARTS = ("auth", "key", "password", "secret", "signature", "token")


def sanitise_source_url(url: str) -> str:
    """Redact credential-like query values while retaining route identity."""

    if not url:
        return ""
    try:
        p = urlsplit(url)
        query = []
        for key, value in parse_qsl(p.query, keep_blank_values=True):
            low = key.casefold()
            if any(part in low for part in _SENSITIVE_QUERY_PARTS):
                value = "<redacted>"
            query.append((key, value))
        return urlunsplit((p.scheme, p.netloc, p.path, urlencode(query), ""))
    except Exception:
        return url.split("#", 1)[0]


def response_sha256(body: Any) -> str:
    """Stable SHA-256 of a JSON/string response without persisting its body."""

    if isinstance(body, bytes):
        payload = body
    elif isinstance(body, str):
        payload = body.encode("utf-8", errors="replace")
    else:
        try:
            payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        except Exception:
            payload = repr(body).encode("utf-8", errors="replace")
    return hashlib.sha256(payload).hexdigest()


def build_unit_source_provenance(
    *,
    provider: str,
    source_url: str,
    body: Any,
    unit_count: int,
    identity: Any = None,
    response_kind: str = "unit_roster",
    status: int = 200,
) -> dict[str, Any]:
    """Describe the exact unit-producing response, without storing its body."""

    identity_dict = None
    if identity is not None:
        identity_dict = identity.to_dict() if hasattr(identity, "to_dict") else identity
    return {
        "provider": provider,
        "response_kind": response_kind,
        "source_url": sanitise_source_url(source_url),
        "response_status": int(status or 0),
        "response_sha256": response_sha256(body),
        "unit_count": int(unit_count or 0),
        "identity": identity_dict,
    }
