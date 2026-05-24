"""Unified API-response concession extractor (2026-05-24).

Pulls concession text out of any PMS API JSON response. The cross-HAR
audit (313 captured HARs, 2026-05-24) found 54 HARs with concession
signal; of those, 18 carry the text in JSON responses under PMS-
specific field names:

    Source                  Field path                                 Sample
    Knock                   property.data.leasing.terms.leasingSpecial "APRIL SHOWERS BRING FREE RENT!..."
    Knock                   property.data.doorway.leasingSpecialIsActive  True (flag only)
    G5 inventory            floorplanSpecials[].name                   "1 month free on units #111 and #21!"
    G5 inventory            hasFloorplanSpecials                       True (flag only)
    G5 marketing-center     specials[].specialDisplayText              "6 Weeks Free Rent!"
    RentCafe                bannerText / leasingSpecial /
                            offer_description                          varies
    Wix                     bannerText / promotion                     varies
    Yardi SecureCafe        bannerText                                 varies
    Repli360                leasingSpecialIsActive                     True (flag only)
    Squarespace             specials                                   varies

This module is the SHARED entry point. Per-adapter integration:

    from ma_poc.core.api_concession_extract import extract_api_concession

    api_concession = extract_api_concession(response_json)
    unit = make_unit_dict(..., concession=api_concession or "", ...)

The helper:
  * Walks the response dict recursively (depth-capped at 8 to bound
    cost on huge GraphQL responses)
  * Tries known field names case-insensitively (snake_case AND camelCase)
  * Filters out GDPR/cookie consent strings ("Special Features",
    "Targeted Advertising", "process data")
  * Returns the longest meaningful text found (length tie → first match
    in document order), or None

Boolean flags (``leasingSpecialIsActive``, ``hasFloorplanSpecials``,
``floor_plan_specials_enabled``) are treated as SIGNAL but not text;
caller can check ``has_concession_flag(body)`` separately to decide
whether to invoke a heavier discovery path (HTML probe, second API
call) when the flag is True but text is absent.
"""

from __future__ import annotations

from typing import Any

# Field name suffixes/keys we trust as real concession text fields.
# Compared case-insensitively after normalising separators.
_TEXT_FIELDS: frozenset[str] = frozenset({
    # Direct text fields
    "leasingspecial", "leasingspecialtext", "leasingspecialdescription",
    "specialdisplaytext", "specialstext", "specialdescription",
    "specialsdescription", "specialcontent", "specialcontenttext",
    "concession", "concessiontext", "concessions", "concessionstext",
    "incentive", "incentivetext", "incentivedescription",
    "promotion", "promotiontext", "promotiondescription",
    "bannertext", "promobanner", "specialbanner",
    "marketingmessage", "marketing_message",
    "offerdescription", "offertext",
    "moveinspecial", "leasespecial", "rentalspecial",
    # Container field names that often hold structured offer objects
    "floorplanspecial", "floorplanspecials", "apartmentspecial",
    "apartmentspecials", "unitspecial", "unitspecials", "specials",
    "promotions", "incentives",
})

# Inner keys to extract text from when a concession field is a dict/object
_INNER_TEXT_KEYS: tuple[str, ...] = (
    "specialDisplayText", "displayText", "text",
    "description", "details", "message",
    "name", "title", "label", "value", "content",
)

# Boolean flag field names — caller uses these to know concessions exist
# even when no text is available in the API response.
_FLAG_FIELDS: frozenset[str] = frozenset({
    "hasspecials", "hasfloorplanspecials", "hasapartmentspecials",
    "leasingspecialisactive", "leasingspecialactive",
    "floorplanspecialsenabled", "floor_plan_specials_enabled",
    "hasspecialsenabled", "hasleasespecial",
})

# Substrings that ALMOST CERTAINLY mean GDPR/cookie consent UI, not
# concession text. Used to filter false positives like
# ``BSpecialFeaturesText: "Special Features"``.
_JUNK_MARKERS: tuple[str, ...] = (
    "special features", "special purposes",
    "targeted advertising", "process data", "cookie policy",
    "browse safely", "privacy policy", "terms of use",
    "third party", "ad personalization",
    "we use cookies", "marketing cookie",
    # Wix branding text that appears under ``promotion`` on free
    # template sites — not an actual operator concession.
    "this website was built on wix", "{wix}", "create yours today",
    "powered by wix",
    # Generic UI labels that are NOT real concession content —
    # e.g. "View the available special offers below" (Yardi/SecureCafe
    # placeholder when no offer is configured).
    "view the available", "view our specials",
    "see specials below", "click for details",
)

# Maximum recursion depth — cap to bound cost on huge GraphQL bodies.
_MAX_DEPTH: int = 8

# Maximum text length to return (longer values are almost always page
# content with the offer embedded — we trim to a reasonable banner size).
_MAX_TEXT_LEN: int = 1000


def _normalize_key(k: str) -> str:
    """Lowercase + strip ``_`` / ``-`` so ``leasingSpecial``,
    ``leasing_special`` and ``leasing-special`` all hash the same."""
    return k.lower().replace("_", "").replace("-", "")


def _is_junk_text(value: str) -> bool:
    """True when the string is obviously GDPR/cookie UI, branding, or
    a generic placeholder (not a real concession).

    Wix branding ("This website was built on Wix...") and Yardi/
    SecureCafe empty-state placeholders ("View the available special
    offers below") are explicitly filtered regardless of length —
    those are NEVER real offers and consistently false-positive on
    the concession-field name match.
    """
    if not value:
        return True
    v = value.lower().strip()
    # Always-junk markers (filtered regardless of length): branding,
    # consent UI, placeholder copy. These NEVER carry real offer text.
    _ALWAYS_JUNK = (
        "this website was built on wix", "{wix}", "powered by wix",
        "view the available", "view our specials",
        "see specials below", "click for details",
    )
    for marker in _ALWAYS_JUNK:
        if marker in v:
            return True
    # Length-gated junk match: short strings that include cookie/UI
    # consent markers. Real concessions tend to be longer, so a short
    # string with these markers is overwhelmingly likely to be UI.
    if len(v) < 80:
        for marker in _JUNK_MARKERS:
            if marker in v:
                return True
    # Lone bare labels (UI column headers)
    if len(v) < 16 and v in ("special features", "special purposes",
                              "promotions", "specials", "promotion",
                              "incentive", "concession", "concessions",
                              "offer", "offers"):
        return True
    return False


def _coerce_to_text(value: Any) -> str | None:
    """Pull a meaningful text string out of any value shape.

    * string → return as-is (after junk filter)
    * dict → look for inner text keys (description / name / etc.)
    * list → recurse into the first dict/string element with content
    * other → None
    """
    if value in (None, "", [], {}, False):
        return None
    if isinstance(value, str):
        s = value.strip()
        if _is_junk_text(s):
            return None
        if len(s) < 4:
            return None
        return s[:_MAX_TEXT_LEN]
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        # Prefer inner text keys in priority order
        for ik in _INNER_TEXT_KEYS:
            iv = value.get(ik)
            if isinstance(iv, str) and iv.strip():
                s = iv.strip()
                if not _is_junk_text(s) and len(s) >= 4:
                    return s[:_MAX_TEXT_LEN]
        return None
    if isinstance(value, list):
        for item in value[:5]:  # cap list traversal
            t = _coerce_to_text(item)
            if t:
                return t
        return None
    return None


def extract_api_concession(body: Any, *, max_depth: int = _MAX_DEPTH) -> str | None:
    """Walk an API response dict/list looking for concession text.

    Returns the LONGEST meaningful text found across all matching fields
    (ties broken by document order — first match wins). Returns ``None``
    when nothing meaningful is present.

    Args:
        body: Parsed JSON (dict / list / scalar). Pre-decoded — pass the
            result of ``json.loads`` or a SDK response's ``.json()``.
        max_depth: Recursion cap. Default 8 — enough for nested GraphQL
            (apartmentComplex.apartments[].floorplan.floorplanSpecials)
            without blowing up on degenerate payloads.

    Examples:
        Knock:    {"property": {"data": {"leasing": {"terms":
                  {"leasingSpecial": "APRIL SHOWERS..."}}}}}
                  → "APRIL SHOWERS..."
        G5:       {"floorplanSpecials": [{"name": "1 month free..."}]}
                  → "1 month free..."
        G5 mkt:   {"specials": [{"specialDisplayText": "6 Weeks Free!"}]}
                  → "6 Weeks Free!"
        RentCafe: {"bannerText": "Save $500..."}  → "Save $500..."
    """
    if body in (None, "", [], {}):
        return None

    candidates: list[str] = []

    def _walk(obj: Any, depth: int) -> None:
        if depth >= max_depth:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not isinstance(k, str):
                    continue
                key_norm = _normalize_key(k)
                if key_norm in _TEXT_FIELDS:
                    text = _coerce_to_text(v)
                    if text:
                        candidates.append(text)
                if isinstance(v, (dict, list)):
                    _walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:20]:  # cap list traversal
                _walk(item, depth + 1)

    _walk(body, 0)

    if not candidates:
        return None

    # Prefer the longest meaningful candidate (more context); ties go to
    # the first found (document order). De-dup case-insensitively.
    seen: set[str] = set()
    deduped: list[str] = []
    for c in candidates:
        key = c.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)

    deduped.sort(key=lambda s: (-len(s), candidates.index(s)))
    return deduped[0]


def has_concession_flag(body: Any, *, max_depth: int = _MAX_DEPTH) -> bool:
    """True when a boolean-flag concession field is set to True.

    Used by adapters to know whether to invoke a heavier discovery path
    (HTML banner probe, second API call) when the API tells us
    concessions exist but doesn't expose the text.

    Returns False when no flag is present OR all flags are False.
    """
    if body in (None, "", [], {}):
        return False

    found = [False]  # mutable flag for nested closure

    def _walk(obj: Any, depth: int) -> None:
        if depth >= max_depth or found[0]:
            return
        if isinstance(obj, dict):
            for k, v in obj.items():
                if not isinstance(k, str):
                    continue
                key_norm = _normalize_key(k)
                if key_norm in _FLAG_FIELDS:
                    # Accept True OR string "true" / "1"
                    if v is True:
                        found[0] = True
                        return
                    if isinstance(v, str) and v.strip().lower() in ("true", "1", "yes"):
                        found[0] = True
                        return
                if isinstance(v, (dict, list)):
                    _walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj[:20]:
                _walk(item, depth + 1)

    _walk(body, 0)
    return found[0]
