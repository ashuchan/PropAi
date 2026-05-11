"""RC1 + RC4 — Blocked endpoint TTL and JS/CSS media filter tests.

RC1: Blocked endpoints with expired TTL or insufficient noise verdicts
     are re-admitted (not dropped) from api_responses.

RC4: JS/CSS content-type responses are filtered before the LLM tier,
     preventing wasted budget on non-data media.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


# ── RC1: TTL logic ─────────────────────────────────────────────────────────────

def _make_blocked_endpoint(
    url: str,
    attempts: int = 2,
    blocked_at_days_ago: int = 5,
) -> object:
    """Create a minimal BlockedEndpoint-like object."""
    class _BE:
        url_pattern = url
        blocked_at = datetime.now(timezone.utc) - timedelta(days=blocked_at_days_ago)

    _BE.attempts = attempts
    return _BE()


def _should_block(be: object, ttl_days: int = 14, min_verdicts: int = 2) -> bool:
    """Replicate the RC1 blocking decision logic."""
    attempts = int(getattr(be, "attempts", 1) or 1)
    if attempts < min_verdicts:
        return False
    blocked_at = getattr(be, "blocked_at", None)
    if blocked_at is not None:
        try:
            _now = datetime.now(timezone.utc)
            _ba = blocked_at
            if _ba.tzinfo is None:
                _ba = _ba.replace(tzinfo=timezone.utc)
            age_days = (_now - _ba).days
            if age_days >= ttl_days:
                return False
        except Exception:
            pass
    return True


def test_rc1_blocked_within_ttl_is_dropped() -> None:
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=2, blocked_at_days_ago=3)
    assert _should_block(be) is True


def test_rc1_blocked_ttl_expired_is_readmitted() -> None:
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=2, blocked_at_days_ago=15)
    assert _should_block(be) is False


def test_rc1_insufficient_noise_verdicts_readmitted() -> None:
    """Only 1 verdict — not enough evidence to block permanently."""
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=1, blocked_at_days_ago=2)
    assert _should_block(be) is False


def test_rc1_exactly_min_verdicts_within_ttl_is_blocked() -> None:
    """Exactly 2 verdicts within TTL → still blocked."""
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=2, blocked_at_days_ago=5)
    assert _should_block(be) is True


def test_rc1_exactly_at_ttl_boundary_is_readmitted() -> None:
    """14 days old exactly → re-admitted (age_days >= ttl_days)."""
    be = _make_blocked_endpoint("https://example.com/api/chat", attempts=3, blocked_at_days_ago=14)
    assert _should_block(be) is False


# ── RC4: Media-type filter ─────────────────────────────────────────────────────

_BLOCKED_CT_PREFIXES = (
    "text/javascript", "text/css", "font/", "image/",
    "application/font", "application/x-font",
)
_BLOCKED_URL_SUFFIXES = (
    ".js", ".css", ".woff", ".woff2", ".ttf", ".otf",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
)


def _is_non_data_response(r: dict) -> bool:
    ct = (r.get("content_type") or r.get("headers", {}).get("content-type") or "").lower()
    if any(ct.startswith(p) for p in _BLOCKED_CT_PREFIXES):
        return True
    url_lower = (r.get("url") or "").lower().split("?")[0]
    return any(url_lower.endswith(sfx) for sfx in _BLOCKED_URL_SUFFIXES)


def test_rc4_javascript_content_type_blocked() -> None:
    resp = {"url": "https://cdn.rentcafe.com/app.js", "content_type": "text/javascript"}
    assert _is_non_data_response(resp) is True


def test_rc4_css_content_type_blocked() -> None:
    resp = {"url": "https://cdn.example.com/styles.css", "content_type": "text/css"}
    assert _is_non_data_response(resp) is True


def test_rc4_image_content_type_blocked() -> None:
    resp = {"url": "https://example.com/banner.png", "content_type": "image/png"}
    assert _is_non_data_response(resp) is True


def test_rc4_js_url_suffix_blocked_even_without_content_type() -> None:
    resp = {"url": "https://cdngeneralmvc.rentcafe.com/bundle.min.js", "content_type": None}
    assert _is_non_data_response(resp) is True


def test_rc4_woff_font_blocked() -> None:
    resp = {"url": "https://fonts.example.com/font.woff2", "content_type": "font/woff2"}
    assert _is_non_data_response(resp) is True


def test_rc4_json_api_passes_through() -> None:
    resp = {"url": "https://api.example.com/units", "content_type": "application/json"}
    assert _is_non_data_response(resp) is False


def test_rc4_plain_api_url_no_content_type_passes() -> None:
    resp = {"url": "https://example.com/api/v1/floorplans", "content_type": None}
    assert _is_non_data_response(resp) is False


def test_rc4_js_suffix_with_query_string_blocked() -> None:
    """Query params should not fool the suffix check."""
    resp = {"url": "https://cdn.example.com/app.js?v=123", "content_type": None}
    assert _is_non_data_response(resp) is True
