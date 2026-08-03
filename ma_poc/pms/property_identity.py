"""Conservative property-identity checks for vendor/API unit rosters.

Unit-producing APIs are frequently portfolio-scoped.  A syntactically valid
response is therefore not sufficient evidence that its units belong to the
configured property.  This module compares vendor metadata with the CSV
identity before an adapter accepts a roster.

The matcher deliberately favours precision:

* an exact/safe property-name match is positive evidence;
* a strong street-address match may override a branding/name variant;
* explicit phase conflicts (``Turtle Dove I`` vs ``Turtle Dove 2``) are a
  hard mismatch;
* missing metadata is ``UNKNOWN`` rather than silently treated as a match.

Callers choose how to handle ``UNKNOWN``.  Warm direct-replay paths should
require ``MATCH`` whenever configured identity is available; an in-page
capture may fail open on ``UNKNOWN`` but must always reject ``MISMATCH``.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

MATCH = "MATCH"
MISMATCH = "MISMATCH"
UNKNOWN = "UNKNOWN"

_GENERIC_NAME_TOKENS = frozenset(
    {
        "a",
        "an",
        "and",
        "apartment",
        "apartments",
        "community",
        "communities",
        "home",
        "homes",
        "of",
        "residence",
        "residences",
        "the",
        "at",
    }
)
_GENERIC_PAGE_NAME_TOKENS = _GENERIC_NAME_TOKENS | frozenset(
    {
        "availability",
        "detail",
        "details",
        "floor",
        "leasing",
        "plan",
        "plans",
        "portal",
        "property",
        "website",
    }
)

_ROMAN = {
    "i": 1,
    "ii": 2,
    "iii": 3,
    "iv": 4,
    "v": 5,
    "vi": 6,
    "vii": 7,
    "viii": 8,
    "ix": 9,
    "x": 10,
}

_STREET_MAP = {
    "avenue": "ave",
    "boulevard": "blvd",
    "circle": "cir",
    "court": "ct",
    "drive": "dr",
    "highway": "hwy",
    "lane": "ln",
    "parkway": "pkwy",
    "place": "pl",
    "road": "rd",
    "street": "st",
    "terrace": "ter",
    "trail": "trl",
}
_STREET_SUFFIXES = frozenset(set(_STREET_MAP) | set(_STREET_MAP.values()) | {"way"})
_DIRECTION_MAP = {
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
}
_UNIT_MARKERS = frozenset({"apt", "apartment", "suite", "ste", "unit", "#"})


@dataclass(frozen=True)
class IdentityDecision:
    """Result of one configured-vs-observed property comparison."""

    status: str
    evidence: tuple[str, ...] = ()
    configured_name: str = ""
    observed_name: str = ""
    configured_address: str = ""
    observed_address: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "evidence": list(self.evidence),
            "configured_name": self.configured_name or None,
            "observed_name": self.observed_name or None,
            "configured_address": self.configured_address or None,
            "observed_address": self.observed_address or None,
        }


def _text(value: Any) -> str:
    if value is None:
        return ""
    return html.unescape(str(value)).strip()


def _ascii_words(value: Any) -> list[str]:
    raw = _text(value)
    if not raw:
        return []
    raw = unicodedata.normalize("NFKD", raw)
    raw = "".join(ch for ch in raw if not unicodedata.combining(ch))
    raw = raw.casefold().replace("&", " and ")
    # Parenthetical strings are commonly PMC/vendor abbreviations, e.g.
    # ``(RDG) Ridgewood Court``.  They are not property identity.
    raw = re.sub(r"\([^)]{1,32}\)", " ", raw)
    return re.findall(r"[a-z0-9]+", raw)


def _phase_signature(value: Any) -> tuple[tuple[str, ...], int | None]:
    words = [w for w in _ascii_words(value) if w not in _GENERIC_NAME_TOKENS]
    if len(words) < 2:
        return tuple(words), None
    tail = words[-1]
    phase: int | None = None
    if tail in _ROMAN:
        phase = _ROMAN[tail]
    elif tail.isdigit() and 0 < int(tail) <= 20:
        phase = int(tail)
    if phase is None:
        return tuple(words), None
    return tuple(words[:-1]), phase


def normalized_name_tokens(value: Any) -> tuple[str, ...]:
    """Return the comparison tokens for a property name.

    A trailing Roman-numeral phase is canonicalised to its Arabic form so
    ``Phase I`` and ``Phase 1`` compare equal.
    """

    base, phase = _phase_signature(value)
    return base + ((str(phase),) if phase is not None else ())


def names_match(configured: Any, observed: Any) -> tuple[bool, str]:
    c = normalized_name_tokens(configured)
    o = normalized_name_tokens(observed)
    if not c or not o:
        return False, "name_missing"

    c_base, c_phase = _phase_signature(configured)
    o_base, o_phase = _phase_signature(observed)
    if c_base and c_base == o_base and c_phase and o_phase and c_phase != o_phase:
        return False, "phase_conflict"
    if c == o:
        return True, "name_exact"

    # Safe containment handles a longer legal/marketing name while refusing a
    # one-token brand collision (``Novi Flats`` vs ``Novi Rise``).
    c_set, o_set = set(c), set(o)
    shorter = c_set if len(c_set) <= len(o_set) else o_set
    longer = o_set if shorter is c_set else c_set
    if len(shorter) >= 2 and shorter.issubset(longer):
        return True, "name_distinctive_subset"
    return False, "name_conflict"


def _normalise_address(value: Any) -> tuple[str, tuple[str, ...]]:
    words = _ascii_words(value)
    if not words:
        return "", ()
    house = ""
    rest: list[str] = []
    for i, word in enumerate(words):
        if not house and re.fullmatch(r"\d+[a-z]?", word):
            house = word
            rest = words[i + 1 :]
            break
    if not house:
        return "", tuple(words)

    cleaned: list[str] = []
    for word in rest:
        if word in _UNIT_MARKERS:
            break
        word = _STREET_MAP.get(word, _DIRECTION_MAP.get(word, word))
        cleaned.append(word)
    # Vendor fields sometimes contain the entire postal address in one string.
    # Once a street suffix is seen, retain one optional direction and discard
    # trailing city/state/ZIP tokens before comparing the street.
    for index, word in enumerate(cleaned):
        if word not in _STREET_SUFFIXES:
            continue
        end = index + 1
        if end < len(cleaned) and cleaned[end] in _DIRECTION_MAP.values():
            end += 1
        cleaned = cleaned[:end]
        break
    return house, tuple(cleaned)


def addresses_match(configured: Any, observed: Any) -> tuple[bool, str]:
    c_num, c_words = _normalise_address(configured)
    o_num, o_words = _normalise_address(observed)
    if not c_num or not o_num:
        return False, "address_missing"
    if c_num != o_num:
        return False, "house_number_conflict"
    if c_words == o_words and c_words:
        return True, "address_exact"

    c_distinct = {w for w in c_words if w not in _STREET_SUFFIXES and w not in _DIRECTION_MAP.values()}
    o_distinct = {w for w in o_words if w not in _STREET_SUFFIXES and w not in _DIRECTION_MAP.values()}
    common = c_distinct & o_distinct
    denom = max(len(c_distinct), len(o_distinct), 1)
    if common and len(common) / denom >= 0.5:
        return True, "address_street_match"
    return False, "street_conflict"


def _zip(value: Any) -> str:
    digits = "".join(re.findall(r"\d", _text(value)))
    if len(digits) == 4:
        digits = "0" + digits
    return digits[:5]


def configured_identity_from_csv(row: Mapping[str, Any] | None) -> dict[str, str]:
    """Read the supported CSV header aliases into a stable identity dict."""

    if not row:
        return {"name": "", "address": "", "city": "", "state": "", "zip": "", "url": ""}

    def first(*keys: str) -> str:
        for key in keys:
            val = row.get(key)
            if val not in (None, ""):
                return _text(val)
        return ""

    return {
        "name": first("name", "Name", "Property Name", "proj_name"),
        "address": first("address", "Address", "street", "Street", "street_address"),
        "city": first("city", "City"),
        "state": first("state", "State"),
        "zip": first("zip", "Zip", "zip_code", "ZIP Code"),
        "url": first("website", "Website", "url", "URL"),
    }


def identity_is_configured(identity: Mapping[str, Any]) -> bool:
    return bool(_text(identity.get("name")) or _text(identity.get("address")))


def _url_name_match(configured_url: str, configured_name: str, observed_name: str) -> bool:
    """Corroborate a vendor name against the configured property URL.

    A generic page title such as ``Property Detail`` must never become
    property identity merely because the route also contains
    ``/property-detail/``.  Require the returned title to carry at least one
    distinctive token shared with the configured property name, in addition
    to appearing in the configured URL path.
    """

    if not configured_url or not configured_name or not observed_name:
        return False
    try:
        path = unquote(urlparse(configured_url).path)
    except Exception:
        return False
    path_tokens = set(_ascii_words(path)) - _GENERIC_PAGE_NAME_TOKENS
    configured = set(normalized_name_tokens(configured_name)) - _GENERIC_PAGE_NAME_TOKENS
    observed = set(normalized_name_tokens(observed_name)) - _GENERIC_PAGE_NAME_TOKENS
    if not configured or not observed or not path_tokens:
        return False
    shared = configured & observed
    if not shared:
        configured_joined = "".join(sorted(configured))
        observed_joined = "".join(sorted(observed))
        shared = {
            token
            for token in observed
            if token in configured_joined or any(item in observed_joined for item in configured)
        }
    if not shared:
        return False
    return observed.issubset(path_tokens) or "".join(sorted(observed)) in "".join(sorted(path_tokens))


def evaluate_property_identity(
    *,
    configured_name: Any = "",
    configured_address: Any = "",
    configured_city: Any = "",
    configured_state: Any = "",
    configured_zip: Any = "",
    configured_url: Any = "",
    observed_name: Any = "",
    observed_address: Any = "",
    observed_city: Any = "",
    observed_state: Any = "",
    observed_zip: Any = "",
) -> IdentityDecision:
    """Compare a vendor/API identity to the configured property identity."""

    cn, ca = _text(configured_name), _text(configured_address)
    on, oa = _text(observed_name), _text(observed_address)
    evidence: list[str] = []

    name_ok, name_reason = names_match(cn, on)
    addr_ok, addr_reason = addresses_match(ca, oa)

    # A phase conflict is stronger than a shared street address: accepting it
    # would merge two separately marketed phases at one site.
    if name_reason == "phase_conflict":
        return IdentityDecision(MISMATCH, (name_reason,), cn, on, ca, oa)
    if addr_ok:
        evidence.append(addr_reason)
    if name_ok:
        evidence.append(name_reason)
    if not name_ok and _url_name_match(_text(configured_url), cn, on):
        evidence.append("configured_url_slug_match")

    cz, oz = _zip(configured_zip), _zip(observed_zip)
    if cz and oz:
        evidence.append("zip_match" if cz == oz else "zip_conflict")

    if any(
        e in evidence
        for e in (
            "address_exact",
            "address_street_match",
            "name_exact",
            "name_distinctive_subset",
            "configured_url_slug_match",
        )
    ):
        # A different ZIP is informative but not enough to override an exact
        # street address/name (vendor feeds occasionally publish an old ZIP).
        return IdentityDecision(MATCH, tuple(evidence), cn, on, ca, oa)

    configured_present = bool(cn or ca)
    # City/state/ZIP alone identify a market, not a property. Treat that shape
    # as UNKNOWN so in-page captures can fail open while detached replays still
    # require a positive name/street match.
    observed_present = bool(on or oa)
    if not configured_present or not observed_present:
        reasons = tuple(r for r in (name_reason, addr_reason) if r.endswith("missing"))
        return IdentityDecision(UNKNOWN, reasons or ("identity_metadata_missing",), cn, on, ca, oa)

    reasons = [name_reason, addr_reason]
    if cz and oz and cz != oz:
        reasons.append("zip_conflict")
    return IdentityDecision(MISMATCH, tuple(dict.fromkeys(reasons)), cn, on, ca, oa)


def evaluate_from_csv(
    row: Mapping[str, Any] | None,
    *,
    observed_name: Any = "",
    observed_address: Any = "",
    observed_city: Any = "",
    observed_state: Any = "",
    observed_zip: Any = "",
) -> IdentityDecision:
    cfg = configured_identity_from_csv(row)
    return evaluate_property_identity(
        configured_name=cfg["name"],
        configured_address=cfg["address"],
        configured_city=cfg["city"],
        configured_state=cfg["state"],
        configured_zip=cfg["zip"],
        configured_url=cfg["url"],
        observed_name=observed_name,
        observed_address=observed_address,
        observed_city=observed_city,
        observed_state=observed_state,
        observed_zip=observed_zip,
    )


def evaluate_from_context(
    ctx: Any,
    *,
    observed_name: Any = "",
    observed_address: Any = "",
    observed_city: Any = "",
    observed_state: Any = "",
    observed_zip: Any = "",
) -> IdentityDecision:
    return evaluate_property_identity(
        configured_name=getattr(ctx, "property_name", ""),
        configured_address=getattr(ctx, "address", ""),
        configured_city=getattr(ctx, "city", ""),
        configured_state=getattr(ctx, "state", ""),
        configured_zip=getattr(ctx, "zip_code", ""),
        configured_url=getattr(ctx, "base_url", ""),
        observed_name=observed_name,
        observed_address=observed_address,
        observed_city=observed_city,
        observed_state=observed_state,
        observed_zip=observed_zip,
    )


def _address_from_mapping(value: Any) -> tuple[str, str, str, str]:
    """Return ``(street, city, state, zip)`` from a vendor location value."""

    if isinstance(value, str):
        return _text(value), "", "", ""
    if not isinstance(value, Mapping):
        return "", "", "", ""

    def first(*keys: str) -> str:
        for key in keys:
            val = value.get(key)
            if val not in (None, ""):
                return _text(val)
        return ""

    return (
        first("address", "address1", "address_1", "street", "street_address", "line1"),
        first("city", "locality"),
        first("state", "region", "state_code"),
        first("zip", "zipcode", "zip_code", "postal_code"),
    )


def sightmap_observed_identity(body: Any) -> dict[str, str]:
    """Extract property metadata from a SightMap response envelope."""

    if not isinstance(body, Mapping):
        return {"name": "", "address": "", "city": "", "state": "", "zip": ""}
    data = body.get("data") if isinstance(body.get("data"), Mapping) else body
    asset = data.get("asset") if isinstance(data, Mapping) else None
    if not isinstance(asset, Mapping):
        asset = data.get("property") if isinstance(data, Mapping) else None
    if not isinstance(asset, Mapping):
        return {"name": "", "address": "", "city": "", "state": "", "zip": ""}
    location = asset.get("location") if isinstance(asset.get("location"), Mapping) else asset
    address_value = asset.get("address")
    if isinstance(address_value, Mapping):
        location = address_value
    address, city, state, zip_code = _address_from_mapping(location)
    if isinstance(address_value, str):
        address = _text(address_value)
    return {
        "name": _text(asset.get("name") or asset.get("title") or asset.get("property_name")),
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
    }


def knock_observed_identity(body: Any) -> dict[str, str]:
    """Extract property metadata from Knock community/numeric responses."""

    if not isinstance(body, Mapping):
        return {"name": "", "address": "", "city": "", "state": "", "zip": ""}
    prop: Any = body.get("property") or body.get("data") or body
    if not isinstance(prop, Mapping):
        return {"name": "", "address": "", "city": "", "state": "", "zip": ""}
    pdata: Any = prop.get("data") if isinstance(prop.get("data"), Mapping) else prop
    location: Any = pdata.get("location") if isinstance(pdata, Mapping) else None
    if not isinstance(location, Mapping):
        location = pdata.get("address") if isinstance(pdata, Mapping) else None
    # Current Doorway responses wrap the actual street fields one level
    # deeper than older payloads::
    #
    #   data.location = {
    #       "name": "Willow Glen",
    #       "address": {"street": "1301 ...", "city": "Fort Worth", ...},
    #   }
    #
    # Passing the outer mapping to ``_address_from_mapping`` stringified the
    # nested dict and lost the structured city/state/ZIP.  Prefer that nested
    # mapping when present while retaining the older flat shape.
    address_source: Any = location
    if isinstance(location, Mapping) and isinstance(location.get("address"), Mapping):
        address_source = location.get("address")
    address, city, state, zip_code = _address_from_mapping(address_source)
    name = ""
    for candidate in (
        location.get("name") if isinstance(location, Mapping) else None,
        pdata.get("name") if isinstance(pdata, Mapping) else None,
        prop.get("name"),
        prop.get("property_name"),
    ):
        if candidate:
            name = _text(candidate)
            break
    return {"name": name, "address": address, "city": city, "state": state, "zip": zip_code}


def evaluate_observed_from_csv(
    row: Mapping[str, Any] | None, observed: Mapping[str, Any]
) -> IdentityDecision:
    return evaluate_from_csv(
        row,
        observed_name=observed.get("name", ""),
        observed_address=observed.get("address", ""),
        observed_city=observed.get("city", ""),
        observed_state=observed.get("state", ""),
        observed_zip=observed.get("zip", ""),
    )


def evaluate_observed_from_context(ctx: Any, observed: Mapping[str, Any]) -> IdentityDecision:
    return evaluate_from_context(
        ctx,
        observed_name=observed.get("name", ""),
        observed_address=observed.get("address", ""),
        observed_city=observed.get("city", ""),
        observed_state=observed.get("state", ""),
        observed_zip=observed.get("zip", ""),
    )
