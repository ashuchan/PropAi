"""On-Site.com leasing-portal adapter (``on-site.com``).

**Distinct from ``onesite.py``** — that adapter handles RealPage *OneSite* OLL
(``onlineleasing.realpage.com`` / ``leasing.realpage.com`` workflowstartup).
*On-Site.com* is a separate leasing product (later acquired by RealPage) whose
public application flow lives at ``on-site.com/apply/property/{id}``.

Research log (live-verified 2026-07-18, timeout-grind Surface C)
--------------------------------------------------------------
``on-site.com/apply/property/{id}`` 302-redirects to
``on-site.com/web/online_app3?property_id={id}&unit_id=0`` — a React SPA
(webpack bundles at ``cdn.on-site.com/assets/webpack/online_app3/*``). A static
GET returns a ~38-47KB shell whose rendered DOM shows ``.floor-plan-row`` cards,
but those are JS-rendered — a static body has zero ``floor-plan-row`` nodes.

**The currently offered unit roster is embedded in the shell as a React props
island** — a JS object literal with UNQUOTED keys (so ``json.loads`` fails)::

    ...unit_availability:{floorplans:[
      {id:0,name:"11L : 1 Bed, 1 Bath",abbreviation:"11L : 1 Bed, 1 Bath",
       num_bedrooms:1,bathrooms:"1 bath",sq_feet:760,starting_price:2504,
       style_id:633761,num_available:8,any_special:false,units:[
         {apartment_num:"301B",display_unit_number:"725-301B",rent:2504,
          sq_feet:760,num_bedrooms:1,bathrooms:"1 bath",
          date_available:"03/05/2026",id:5232557,style_id:633761,
          street_address:"725 Wilson Street",deposit:1000,is_waitlist:false,
          special_description:"",...}]}]}

Each active ``units:[]`` entry is a complete **unit-level** record. The same
island also carries ``unit_list:[...]``, the list the live application renders
in its unit-selection step. The parser treats that list as authoritative when
present: a raw ``units[]`` object outside it is not published. This avoids
surfacing stale/orphaned bootstrap records that the public application no
longer offers.

Live counts:
pullmansantarosa (606821) 14 fp / 11 units · sienavilla (40114) 16 fp / 10 ·
tustin-view (214988) 6 fp / 4. Per-unit ``rent`` is genuine (varies within a
plan), and ``id`` is a **stable source unit id** (no synthetic id needed).

RealPage CWS's docstring claims the on-site.com apply link "isn't a public unit
roster" — that is WRONG for this surface; it is a full public roster, one
React-island deep.

Parse strategy: unit objects are flat (only ``amenities:[]`` empty arrays, no
nested ``{}``), so a balanced-brace slice of the ``unit_availability:{...}``
object plus a per-unit field regex extracts every *active* unit robustly
without a JS engine.

Routing is flag-gated (``ENABLE_ONSITE_APPLY_ADAPTER``, default off): the
detector only emits ``onsite_apply`` for a page carrying an
``on-site.com/apply/property`` or ``/web/online_app3`` link when the flag is on,
so the default config is unchanged and a canary measures the recovery.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ma_poc.pms.adapters._parsing import (
    bed_label_from,
    make_unit_dict,
)
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult

if TYPE_CHECKING:
    from playwright.async_api import Page

log = logging.getLogger(__name__)

# On-Site property id, extracted from a marketing page's portal link. Three
# observed link shapes (2026-07-18 grind):
#   on-site.com/apply/property/{id}
#   on-site.com/web/online_app3?property_id={id}
#   on-site.com/web/online_app3/{id}
# Backslash/entity escaping tolerated (Next.js/JSON-embedded hrefs).
_ONSITE_PROPERTY_ID_RE = re.compile(
    r"on-site\.com(?:\\?/|/)(?:"
    r"apply(?:\\?/|/)property(?:\\?/|/)(\d{3,9})"
    r"|web(?:\\?/|/)online_app3\?[^\"'\\\s]*property_id=(\d{3,9})"
    r"|web(?:\\?/|/)online_app3(?:\\?/|/)(\d{3,9})"
    r")",
    re.IGNORECASE,
)

_ONLINE_APP3_URL = "https://www.on-site.com/web/online_app3?property_id={pid}&unit_id=0"


def extract_onsite_property_id(body: str) -> str | None:
    """Return the first On-Site property id linked from ``body``, or ``None``.

    Scans for an ``on-site.com`` apply/online_app3 portal link and returns its
    numeric property id. Tolerates backslash- and entity-escaped hrefs.
    """
    if not body:
        return None
    m = _ONSITE_PROPERTY_ID_RE.search(body)
    if not m:
        return None
    # Exactly one of the three alternation groups is populated per match.
    return next((g for g in m.groups() if g), None)


def _extract_balanced_object(text: str, start_key: str) -> str:
    """Return the balanced ``{...}`` object that begins at ``start_key``.

    ``start_key`` must end at the opening brace (e.g. ``"unit_availability:{"``).
    Brace counting respects double-quoted string literals (and their escapes)
    so a ``}`` inside a value does not terminate the object early. Returns
    ``""`` when the key is absent or the braces are unbalanced.
    """
    i = text.find(start_key)
    if i < 0:
        return ""
    j = i + len(start_key) - 1  # index of the opening '{'
    if j >= len(text) or text[j] != "{":
        return ""
    depth = 0
    in_str = False
    esc = False
    k = j
    n = len(text)
    while k < n:
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        else:
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return text[j : k + 1]
        k += 1
    return ""  # unbalanced


def _extract_balanced_array(text: str, start_key: str) -> str:
    """Return the balanced ``[...]`` array that begins at ``start_key``.

    On-Site's current floor-plan objects contain nested arrays and nested
    pricing objects.  Counting brackets while respecting strings keeps those
    objects intact instead of stopping at the first nested ``]``.
    """
    i = text.find(start_key)
    if i < 0:
        return ""
    j = i + len(start_key) - 1
    if j >= len(text) or text[j] != "[":
        return ""
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                return text[j : k + 1]
    return ""


def _iter_top_level_objects(array: str) -> list[str]:
    """Return the top-level object literals from a balanced JS array."""
    objects: list[str] = []
    depth = 0
    start: int | None = None
    in_str = False
    esc = False
    for i, c in enumerate(array):
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(array[start : i + 1])
                start = None
    return objects


def _top_level_only(obj: str) -> str:
    """Mask nested JS objects/arrays while retaining top-level scalar fields."""
    out: list[str] = []
    brace_depth = 0
    array_depth = 0
    in_str = False
    esc = False
    keep_string = False
    for c in obj:
        if in_str:
            out.append(c if keep_string else " ")
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            keep_string = brace_depth == 1 and array_depth == 0
            in_str = True
            out.append(c if keep_string else " ")
        elif c == "{":
            brace_depth += 1
            out.append(c if brace_depth == 1 and array_depth == 0 else " ")
        elif c == "}":
            out.append(c if brace_depth == 1 and array_depth == 0 else " ")
            brace_depth = max(0, brace_depth - 1)
        elif c == "[":
            array_depth += 1
            out.append(" ")
        elif c == "]":
            out.append(" ")
            array_depth = max(0, array_depth - 1)
        else:
            out.append(c if brace_depth == 1 and array_depth == 0 else " ")
    return "".join(out)


def _str_field(field: str, obj: str) -> str:
    m = re.search(r"\b" + field + r':"([^"]*)"', obj)
    return m.group(1) if m else ""


def _int_field(field: str, obj: str) -> int | None:
    m = re.search(r"\b" + field + r":(-?\d+)", obj)
    return int(m.group(1)) if m else None


# A flat unit object inside ``units:[...]`` — anchored on ``apartment_num``,
# which floorplan-level objects never carry. Units contain only empty
# ``amenities:[]`` arrays (no nested ``{}``), so a brace-free window is safe.
_UNIT_OBJ_RE = re.compile(r"\{[^{}]*?apartment_num:\"[^\"]*\"[^{}]*?\}")
_UNIT_LIST_RE = re.compile(r"\bunit_list:\[([^\]]*)\]")
_JS_STRING_RE = re.compile(r'"((?:\\.|[^"\\])*)"')


def _plan_names_by_style(island: str) -> dict[str, str]:
    """Build an exact top-level ``style_id -> name`` map.

    Current shells place nested ``starting_term.best_price`` objects between
    ``name`` and ``style_id``.  Masking nested values prevents their braces or
    similarly named child fields from changing the join.
    """
    floorplans = _extract_balanced_array(island, "floorplans:[")
    result: dict[str, str] = {}
    for obj in _iter_top_level_objects(floorplans):
        top_level = _top_level_only(obj)
        name = _str_field("name", top_level).strip()
        style_id = _int_field("style_id", top_level)
        if name and style_id is not None:
            result.setdefault(str(style_id), name)
    return result


def _normalize_onsite_bathrooms(label: str) -> str:
    """Convert a bounded On-Site label (including mixed halves) to a number.

    The live source currently emits ``1 bath``, ``2 bath``, ``1 1/2 bath``,
    and ``2 1/2 bath``.  A zero or non-half-step value is not promoted into a
    dwelling fact; it remains missing for downstream quality reporting.
    """
    match = re.fullmatch(
        r"\s*(\d+(?:\.\d+)?)\s*(?:(\d+)\s*/\s*(\d+))?\s+baths?\s*",
        label,
        flags=re.IGNORECASE,
    )
    if not match:
        return ""
    value = float(match.group(1))
    if match.group(2) and match.group(3):
        denominator = int(match.group(3))
        if denominator == 0:
            return ""
        value += int(match.group(2)) / denominator
    if not 0 < value <= 10 or abs(value * 2 - round(value * 2)) > 1e-9:
        return ""
    return f"{value:g}"


def _is_non_unit_application_option(
    *,
    apartment_number: str,
    display_number: str,
    plan_name: str,
    bedrooms: int | None,
    bathrooms_label: str,
    sqft: int | None,
) -> bool:
    """Identify the proven On-Site roommate-application sentinel.

    Seville at Mace Ranch currently whitelists an application choice named
    ``Roommate Add O`` under plan ``Roommate Add On``.  It carries 0 beds,
    ``0 bath``, no area, and is not a physical apartment.  Require every one
    of those source signals so a real unit with an unusual label cannot be
    removed by a broad text heuristic.
    """

    def _is_roommate_add_on(value: str) -> bool:
        return bool(
            re.fullmatch(
                r"roommate\s+add(?:\s+on|\s+o)?",
                value.strip(),
                flags=re.IGNORECASE,
            )
        )

    return (
        _is_roommate_add_on(apartment_number)
        and _is_roommate_add_on(display_number)
        and _is_roommate_add_on(plan_name)
        and bedrooms == 0
        and bathrooms_label.strip().lower() == "0 bath"
        and sqft is None
    )


def _onsite_property_metadata(body: str) -> dict[str, str]:
    """Extract the authoritative property boundary from the application shell."""
    marker = re.search(r"(?<![A-Za-z0-9_])property:\{", body)
    if marker is None:
        return {}
    obj = _extract_balanced_object(body[marker.start() :], "property:{")
    property_id = _int_field("property_id", obj)
    if property_id is None:
        return {}
    street = _str_field("street_addr", obj).strip()
    city = _str_field("city", obj).strip()
    state = _str_field("state", obj).strip()
    zip_code = _str_field("zip_code", obj).strip()
    region = " ".join(part for part in (state, zip_code) if part)
    address = ", ".join(part for part in (street, city, region) if part)
    return {
        "property_id": str(property_id),
        "property_name": _str_field("property_name", obj).strip(),
        "property_address": address,
    }


def _active_unit_identifiers(island: str) -> set[str] | None:
    """Return the On-Site application's active unit whitelist when supplied.

    ``unit_list`` is the unit identifier collection used by the public
    ``step/unit`` page.  It may contain either ``apartment_num`` or
    ``display_unit_number`` values (the latter can include a building prefix),
    so callers must compare both.  ``None`` means an older shell did not
    provide the field and preserves backwards-compatible parsing; an empty set
    means the application explicitly exposes no units.
    """
    match = _UNIT_LIST_RE.search(island)
    if match is None:
        return None
    return {
        bytes(value, "utf-8").decode("unicode_escape").strip()
        for value in _JS_STRING_RE.findall(match.group(1))
        if value.strip()
    }


def parse_onsite_online_app3(
    body: str,
    source_url: str = "",
    *,
    expected_property_id: str | None = None,
) -> list[dict[str, Any]]:
    """Parse the On-Site ``online_app3`` props island into unit dicts.

    Extracts the ``unit_availability:{floorplans:[...]}`` object, builds a
    ``style_id -> floorplan-name`` map, then emits one unit dict per active
    ``units:[]`` entry.  When the island supplies ``unit_list``, only entries
    present in that live application whitelist are emitted. Returns ``[]``
    when the island is absent or carries no available units. Never raises.
    """
    if not body:
        return []
    island = _extract_balanced_object(body, "unit_availability:{")
    if not island:
        return []

    property_metadata = _onsite_property_metadata(body)
    returned_property_id = property_metadata.get("property_id", "")
    if expected_property_id is not None and returned_property_id != str(expected_property_id):
        return []

    plan_name = _plan_names_by_style(island)
    active_identifiers = _active_unit_identifiers(island)

    units: list[dict[str, Any]] = []
    for um in _UNIT_OBJ_RE.finditer(island):
        obj = um.group(0)
        apt = _str_field("apartment_num", obj)
        if not apt:
            continue
        display_apt = _str_field("display_unit_number", obj)
        # On-Site can retain obsolete objects in its bootstrap payload after
        # they disappear from the public unit-selection page.  ``unit_list``
        # is the page's own active roster, so do not publish any object outside
        # it.  Compare both identifiers because some communities prefix the
        # display number with the building (e.g. ``725-301B`` vs ``301B``).
        if active_identifiers is not None and not ({apt, display_apt} & active_identifiers):
            continue
        rent = _int_field("rent", obj)
        sqft = _int_field("sq_feet", obj)
        beds = _int_field("num_bedrooms", obj)
        baths_raw = _str_field("bathrooms", obj)
        baths = _normalize_onsite_bathrooms(baths_raw)
        style_id = _int_field("style_id", obj)
        sid = str(style_id) if style_id is not None else ""
        plan = plan_name.get(sid, "")
        onsite_id = _int_field("id", obj)
        unit_property_id = _int_field("property_id", obj)
        date_avail = _str_field("date_available", obj)
        street = _str_field("street_address", obj)

        if _is_non_unit_application_option(
            apartment_number=apt,
            display_number=display_apt,
            plan_name=plan,
            bedrooms=beds,
            bathrooms_label=baths_raw,
            sqft=sqft,
        ):
            continue

        source_ids: dict[str, Any] = {}
        if onsite_id is not None:
            source_ids["onsite_unit_id"] = onsite_id
        if sid:
            source_ids["onsite_style_id"] = sid
        if returned_property_id:
            source_ids["onsite_property_id"] = returned_property_id
        if unit_property_id is not None:
            source_ids["onsite_unit_property_id"] = str(unit_property_id)

        unit = make_unit_dict(
            floor_plan_name=plan,
            bed_label=bed_label_from(beds, plan),
            bedrooms=str(beds) if beds is not None else "",
            bathrooms=baths,
            sqft=str(sqft) if sqft is not None else "",
            unit_number=apt,
            unit_name=display_apt or apt,
            rent_low=rent,
            rent_high=rent,
            availability_status="AVAILABLE",
            availability_date=date_avail,
            source_api_url=source_url,
            extraction_tier="TIER_1_API_ONSITE_APPLY",
            source_ids=source_ids or None,
            building=street,
        )
        if onsite_id is not None:
            unit["unit_id"] = str(onsite_id)
        if street:
            unit["address"] = street
        if baths_raw:
            unit["source_bathrooms_label"] = baths_raw
        if plan:
            unit["_floor_plan_name_provenance"] = "onsite.floorplans[].name"
        if returned_property_id:
            unit["source_property_id"] = returned_property_id
        if property_metadata.get("property_name"):
            unit["source_property_name"] = property_metadata["property_name"]
        if property_metadata.get("property_address"):
            unit["source_property_address"] = property_metadata["property_address"]
        if returned_property_id:
            unit["source_property_provenance"] = "onsite_online_app3_property_object"
            unit["source_request_payload"] = {
                "property_id": str(expected_property_id or returned_property_id),
                "unit_id": "0",
            }
        units.append(unit)
    return units


class OnSiteApplyAdapter:
    """On-Site.com (``on-site.com``) leasing-portal adapter.

    Discovers the property id from the marketing page body's ``on-site.com``
    portal link, fetches the ``online_app3`` shell via ``probe_get`` (static —
    no render, no Web-Unlocker), and parses the embedded props island into
    unit-level records.

    Tier labels:
      ``TIER_1_API_ONSITE_APPLY``           — unit-level roster parsed + admitted
      ``TIER_1_API_ONSITE_APPLY_EMPTY``     — island parsed but 0 admitted
      ``TIER_1_API_ONSITE_APPLY_NO_ID``     — no on-site.com portal link found
      ``TIER_1_API_ONSITE_APPLY_NO_DATA``   — fetched but island absent/empty
      ``TIER_1_API_ONSITE_APPLY_PROPERTY_MISMATCH`` — shell boundary rejected
    """

    pms_name: str = "onsite_apply"
    _fingerprints: list[str] = ["on-site.com/apply/property", "on-site.com/web/online_app3"]

    async def extract(self, page: Page, ctx: AdapterContext) -> AdapterResult:
        """Fetch + parse the On-Site online_app3 roster. Never raises."""
        result = AdapterResult(tier_used="TIER_1_API_ONSITE_APPLY")

        # The marketing page body carries the on-site.com portal link.
        fr = getattr(ctx, "fetch_result", None)
        raw_body = getattr(fr, "body", None) if fr is not None else None
        if isinstance(raw_body, bytes):
            raw_body = raw_body.decode("utf-8", errors="replace")
        if not isinstance(raw_body, str):
            raw_body = ""

        # The property's own URL may itself be the on-site link.
        pid = extract_onsite_property_id(raw_body) or extract_onsite_property_id(
            getattr(ctx, "base_url", "") or ""
        )
        if not pid:
            result.tier_used = "TIER_1_API_ONSITE_APPLY_NO_ID"
            result.confidence = 0.0
            result.errors.append("No on-site.com property-id link found in page body")
            return result

        url = _ONLINE_APP3_URL.format(pid=pid)
        try:
            from ma_poc.pms.adapters._probe import probe_get

            r = probe_get(url, timeout=20, unlocker=False)
            shell = getattr(r, "text", "") or ""
        except Exception as exc:  # noqa: BLE001
            result.tier_used = "TIER_1_API_ONSITE_APPLY_NO_DATA"
            result.confidence = 0.0
            result.errors.append(f"onsite-online_app3-probe-error: {type(exc).__name__}: {str(exc)[:90]}")
            return result

        property_metadata = _onsite_property_metadata(shell)
        returned_property_id = property_metadata.get("property_id", "")
        if returned_property_id != pid:
            result.tier_used = "TIER_1_API_ONSITE_APPLY_PROPERTY_MISMATCH"
            result.confidence = 0.0
            result.errors.append(
                "On-Site property boundary mismatch: "
                f"requested={pid}, returned={returned_property_id or '<missing>'}"
            )
            return result

        units = parse_onsite_online_app3(
            shell,
            source_url=url,
            expected_property_id=pid,
        )
        if not units:
            result.tier_used = "TIER_1_API_ONSITE_APPLY_NO_DATA"
            result.confidence = 0.0
            result.errors.append(f"online_app3 shell for property_id={pid} carried no available units")
            return result

        # Stage 1 validity gate (same as every API-class adapter).
        from ma_poc.extraction.post_process import post_process

        _pp = post_process(units, property_id=getattr(ctx, "property_id", None))
        if _pp.n_admitted > 0:
            result.units = _pp.admitted
            result.plan_summaries = _pp.plan_summaries
            result.winning_url = url
            result.confidence = min(0.95, 0.7 + 0.05 * _pp.n_admitted)
            result.api_responses.append(
                {
                    "url": url,
                    "status": 200,
                    "body": "<onsite-online_app3>",
                    "via": "onsite_apply_probe",
                    "requested_property_id": pid,
                    "returned_property_id": returned_property_id,
                    "returned_property_name": property_metadata.get("property_name", ""),
                    "returned_property_address": property_metadata.get("property_address", ""),
                }
            )
        else:
            result.tier_used = "TIER_1_API_ONSITE_APPLY_EMPTY"
            result.confidence = 0.0
            result.errors.append(
                f"ONSITE_APPLY_VALIDITY_REJECTED: {len(units)} parsed rows failed unit_validity"
            )
        return result

    def static_fingerprints(self) -> list[str]:
        return list(self._fingerprints)
