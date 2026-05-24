"""Pure API-parsing utilities for Jugnu PMS adapters.

Extracted from scripts/entrata.py and scripts/scrape_properties.py so these
functions have a Jugnu-native home and the legacy scripts can be retired.
No Playwright or network dependencies — pure data transforms only.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from typing import Any

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────

TARGET_JSONLD_TYPES: frozenset[str] = frozenset({
    "Apartment",
    "ApartmentUnit",
    "ApartmentComplex",
    "Offer",
    "FloorPlan",
    "Residence",
    "SingleFamilyResidence",
})

_RENT_MIN = 200
_RENT_MAX = 50_000

_UNIT_ID_KEYS: tuple[str, ...] = (
    "unit_number", "unitNumber", "UnitNumber",
    "unit_id", "unitId", "unit_name", "unitName",
    "label", "id", "ID",
)

_RENT_KEYS: frozenset[str] = frozenset({
    "price", "minRent", "askingRent", "rent", "monthlyRent",
    "minPrice", "startingPrice", "base_rent", "baseRent",
    "display_price", "displayPrice", "monthly_rent",
    "rentTerms", "pricing", "market_rent",
})

# ── Low-level helpers ──────────────────────────────────────────────────────────


def _get(d: dict, *keys: str) -> str:
    """Try multiple key names, return first non-empty string found.

    Unwraps nested rent/sqft objects like ``{rent: {min: 1351, max: 1351}}``.
    Also unwraps name-shaped dicts ``{name: "...", provider_id: "..."}`` —
    some upstream APIs (Morgan Properties / RentCafe family) return the
    floor-plan-name field as a dict; without unwrap we serialise the whole
    dict and emit ``floor_plan_name='{"name":"One Bedroom","provider_id":...}'``
    See PID 161 Hickory Mill 2026-05-14.
    """
    for k in keys:
        v = d.get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, list):
            continue
        if isinstance(v, dict):
            # Try name-shaped subkeys first (B3 fix), then rent/sqft subkeys.
            for sub_k in ("name", "label", "title", "display_name", "displayName"):
                sv = v.get(sub_k)
                if isinstance(sv, str) and sv:
                    return sv
            for sub_k in ("min", "low", "amount", "value", "effectiveRent", "max", "high"):
                sv = v.get(sub_k)
                if sv is not None and sv != "":
                    return str(sv)
            continue
        return str(v)
    return ""


def _unwrap_name(v: Any) -> str:
    """Coerce a possibly-dict-shaped name field to a plain string.

    Some PMS APIs (RealPage via Morgan Properties is a confirmed case) emit
    ``floor_plan`` / ``floorPlanName`` as a dict ``{"name": "...", "provider_id":
    "..."}``. A naive ``str(v)`` then serialises the whole dict and the row
    surfaces with ``floor_plan_name`` set to a stringified JSON object.
    This helper extracts the human-readable name when present.
    """
    if v is None:
        return ""
    if isinstance(v, dict):
        for k in ("name", "label", "title", "display_name", "displayName"):
            sv = v.get(k)
            if isinstance(sv, str) and sv:
                return sv
        return ""
    return str(v) if v != "" else ""


def _money_to_int(s: Any) -> int | None:
    """Parse '$1,450', '1450.00', '1,450 USD' -> 1450. Returns None on failure.

    For range strings like ``"$1,200 - $1,400"`` returns the LOW value.
    See ``ma_poc/pms/adapters/_parsing.py::money_to_int`` for the
    rationale (project_run_2026_05_11 deposit->rent leakage fix).
    """
    if s is None:
        return None
    m = re.search(r"\d[\d,]*(?:\.\d{1,2})?", str(s))
    if not m:
        return None
    cleaned = m.group(0).replace(",", "")
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def _find_list(obj: Any, keys: tuple[str, ...]) -> list:
    """Return the first non-empty list found at any of the given keys in obj."""
    if not isinstance(obj, dict):
        return []
    for k in keys:
        v = obj.get(k)
        if isinstance(v, list) and v:
            return v
    return []


# Canonical unit-signal keys for the recursive walker. After
# normalize_field_key() collapses vendor variants (camelCase / PascalCase /
# snake_case / vendor abbreviations) these are the field-names that count.
# An item with >= 2 canonical signals is treated as unit-shaped.
_CANON_UNIT_SIGNAL_KEYS: frozenset[str] = frozenset({
    "rent", "min_rent", "max_rent",
    "sqft",
    "bedrooms", "bathrooms",
    "floor_plan_name",
    "unit_number", "unit_id",
    "available_date",
})

# Defensive cap on the recursive walker. The Razz blob is ~1 MB but well-
# structured; pathological pages (mis-rendered SPAs, infinitely nested
# logger payloads) can dwarf that. Capping nodes visited keeps the worst
# case bounded.
_WALK_MAX_NODES = 50_000


def _item_has_unit_signals(item: Any, min_signals: int = 2) -> bool:
    """True iff ``item`` is a dict whose keys (normalised) include at least
    ``min_signals`` distinct canonical unit signal keys.

    Skips keys starting with '@' (Schema.org reserved). Uses
    normalize_field_key from floor_plan_signals to collapse vendor variants
    so ``monthlyRent``/``minRent``/``rentTerms``/``floorPlanName``/
    ``bedroomCount``/``squareFootage``/``SquareFeet`` all map to canonical
    keys without per-call regex work.
    """
    if not isinstance(item, dict):
        return False
    try:
        from ma_poc.pms.signal_engine.floor_plan_signals import (
            normalize_field_key as _norm,
        )
    except Exception:
        return False
    found: set[str] = set()
    for k, v in item.items():
        if not isinstance(k, str) or k.startswith("@"):
            continue
        # Treat dict/list values as "present" if they are non-empty —
        # nested ``rent: {min, max}`` and ``rentTerms: [...]`` count as
        # the rent signal even though the value isn't a scalar.
        if v in (None, "", [], {}):
            continue
        canon = _norm(k)
        if canon in _CANON_UNIT_SIGNAL_KEYS:
            found.add(canon)
            if len(found) >= min_signals:
                return True
    return False


#: Foreign-key field names on a UNIT row that reference a sibling PLAN
#: row's primary key. When a candidate list's items carry one of these
#: fields AND there's another sibling list whose ``id`` values match,
#: the FK-bearing list is the authoritative unit list and the referenced
#: list provides plan-level enrichment. Origin: Knock RentManager API
#: (PID 253774 on 2026-05-18) returns ``{layouts: [60 plans], units: [30
#: instances]}`` where only ``units`` carry ``displayPrice`` and
#: ``availableOn`` but our pre-fix walker picked ``layouts`` (longer
#: list) and shipped 60 plan-level rows with null rent / null
#: availability. Adding a new vendor's FK key here extends the pair
#: detector automatically.
_PLAN_FK_KEYS: tuple[str, ...] = (
    "layoutId", "layout_id", "layoutid",
    "floorPlanId", "floor_plan_id", "floorplanid", "floorplanId",
    "planId", "plan_id",
    "fpId", "fp_id",
    "unitTypeId", "unit_type_id", "unittypeid",
    "modelId", "model_id",
    "templateId", "template_id",
)

#: Per-unit signal keys that mark a list as the AUTHORITATIVE unit list
#: (rather than a plan-summary list). A list whose items carry any of
#: these keys has per-apartment data the plan-list doesn't have. Match
#: is on raw key name — vendor variants are common but case-sensitive
#: comparison is enough because the same vendor uses one spelling
#: throughout one response.
_PER_UNIT_SIGNAL_KEYS: tuple[str, ...] = (
    # availability date / status
    "available_date", "availableOn", "available_on", "availableDate",
    "moveInDate", "move_in_date", "readyDate", "ready_date",
    "available", "leased", "occupied", "reserved", "noticeGiven",
    # per-unit rent (vs plan-level rent range)
    "displayPrice", "display_price", "price", "monthlyRent",
    "monthly_rent", "askingRent", "asking_rent",
    # per-unit identity
    "unitNumber", "unit_number", "unitId", "unit_id",
)


def _list_item_keys(items: list, sample: int = 5) -> set[str]:
    """Union of dict-keys across the first ``sample`` items of ``items``.

    Used by the FK-pair detector to decide which list is plan-shaped vs
    unit-shaped without scanning the entire list. 5 items is enough to
    surface vendor key patterns; vendor APIs are uniform within a
    response.
    """
    keys: set[str] = set()
    for item in items[:sample]:
        if isinstance(item, dict):
            keys.update(k for k in item.keys() if isinstance(k, str))
    return keys


def _detect_plan_unit_pair(
    walker_lists: list[list[dict]],
) -> tuple[list[dict], list[dict], str] | None:
    """Detect a plan-list / unit-list pair joined by foreign key.

    A unit-list is identified by:
        - items carry one of :data:`_PLAN_FK_KEYS` (foreign-key reference)
        - items carry at least one :data:`_PER_UNIT_SIGNAL_KEYS` field
          (per-unit data the plan-list can't have)

    A plan-list is identified by:
        - items have an ``id`` field whose values match the unit-list's
          FK values for >=2 distinct references (avoids accidental match
          on unrelated lists that happen to have ``id``)

    Returns ``(plan_list, unit_list, fk_key)`` on detection, ``None``
    otherwise. When multiple unit-list / plan-list candidates exist,
    picks the pair with the highest match-count.

    Generic by design: any vendor that ships the
    ``{plans: [...], units-with-planId: [...]}`` shape benefits without
    a per-vendor parser. Tested vendors:
        - Knock RentManager (``layoutId``, PID 253774, 2026-05-18)
        - (extension point — add coverage as new vendors surface)
    """
    if len(walker_lists) < 2:
        return None

    # Categorise each candidate list as plan-shaped / unit-shaped /
    # neither. A list can be BOTH if items carry both an ``id`` and an
    # FK to another list — pick the side that produces more matches.
    candidates_as_unit: list[tuple[list[dict], str]] = []
    candidates_as_plan: list[tuple[list[dict], set]] = []

    for lst in walker_lists:
        if not lst:
            continue
        keys = _list_item_keys(lst)
        # Unit-shape: has an FK key AND a per-unit signal.
        fk_key = next((k for k in _PLAN_FK_KEYS if k in keys), None)
        if fk_key and any(k in keys for k in _PER_UNIT_SIGNAL_KEYS):
            candidates_as_unit.append((lst, fk_key))
        # Plan-shape: items have an ``id`` field. We're permissive here —
        # the cross-reference count filters false positives below.
        if "id" in keys:
            ids = {
                it.get("id") for it in lst
                if isinstance(it, dict) and it.get("id") is not None
            }
            if ids:
                candidates_as_plan.append((lst, ids))

    if not candidates_as_unit or not candidates_as_plan:
        return None

    # For each (unit, plan) combination, count how many unit FKs resolve
    # to a plan id. The best-scoring pair wins. Require >=2 matches so
    # we don't false-positive on a single-row coincidence.
    best: tuple[int, list[dict], list[dict], str] | None = None
    for unit_list, fk_key in candidates_as_unit:
        for plan_list, plan_ids in candidates_as_plan:
            if plan_list is unit_list:
                continue
            match_count = 0
            for u in unit_list:
                if not isinstance(u, dict):
                    continue
                fk_val = u.get(fk_key)
                if fk_val is not None and fk_val in plan_ids:
                    match_count += 1
            if match_count >= 2 and (best is None or match_count > best[0]):
                best = (match_count, plan_list, unit_list, fk_key)

    if best is None:
        return None
    _, plan_list, unit_list, fk_key = best
    return plan_list, unit_list, fk_key


#: Fields the row-picker (``parse_api_responses`` body around line 1112)
#: walks when filling ``unit_number``. When we append an unmatched plan
#: as a plan-summary row, the picker would otherwise read the plan's
#: database PK (``id``) or a vendor label as a unit-number, lifting the
#: row to ``unit`` instead of ``plan`` at the classify boundary. The
#: stripper below clears every alias the picker reads so the resulting
#: row has ``unit_number=None`` and partitions to plan-summary correctly.
_UNIT_NUMBER_PICKER_KEYS: tuple[str, ...] = (
    "unitNumber", "unit_number", "unitId", "unit_id", "UnitNumber",
    "label", "display_unit_number", "unitCode", "unit_code",
    "id",
)

#: Plan-side bookkeeping fields that would only add noise downstream and
#: never feed the v2 unit schema. Stripped from both merged units and
#: unmatched plan-summary rows.
_PLAN_BOOKKEEPING_DROP: tuple[str, ...] = (
    "createdAt", "modifiedAt", "deletedAt", "deletedByVendor",
    "integrationId", "images", "description",
)


def _merge_units_with_plans(
    unit_list: list[dict],
    plan_list: list[dict],
    fk_key: str,
) -> list[dict]:
    """Enrich each unit dict with fields from its referenced plan.

    Unit-side fields ALWAYS win (per-unit data is authoritative; the
    plan's ``area`` may be wrong for a specific unit on plans with
    slight size variations across buildings). The plan supplies values
    only for keys missing or empty on the unit. Returns a new list — the
    input dicts are not mutated.

    **Unmatched-plan preservation (Gap 1, 2026-05-19):** plans whose
    ``id`` is NOT referenced by any unit's foreign key are appended to
    the returned list as plan-shape rows (no per-apartment identity).
    The downstream classifier in :mod:`ma_poc.extraction.classify`
    partitions them into ``plan_summaries`` and the v2 formatter ships
    them under ``floor_plans[]`` (cf. playbook §8.18). This prevents
    silent loss of plans without current available units (PID 268552:
    7 plans / 2 had units → pre-Gap-1 shipped 2, post-Gap-1 ships 8 = 2
    units + 6 plan summaries; PID 253774: 60 plans / 30 had units →
    ships 30 units + 30 plan summaries instead of silently dropping
    the 30 plan rows).

    The fix preserves the original bfeda6c intent — the 30 plans
    without rent on PID 253774 still don't ship as ``units`` (no rent,
    no availability, no natural identity → classifier returns "plan");
    they just stop being silently dropped.
    """
    plan_index: dict[Any, dict] = {
        p["id"]: p for p in plan_list
        if isinstance(p, dict) and p.get("id") is not None
    }
    referenced_plan_ids: set[Any] = set()
    out: list[dict] = []
    for u in unit_list:
        if not isinstance(u, dict):
            continue
        fk_val = u.get(fk_key)
        plan = plan_index.get(fk_val) if fk_val is not None else None
        if plan is not None:
            referenced_plan_ids.add(fk_val)
        if not plan:
            out.append(dict(u))
            continue
        # plan first, unit second so the unit's value overrides on
        # overlapping keys. Skip the plan's ``id`` (it's the FK from
        # the unit's perspective, not the unit's own id) and skip the
        # plan's bookkeeping fields that would just be noise.
        merged = dict(plan)
        merged.pop("id", None)
        for skip in _PLAN_BOOKKEEPING_DROP:
            merged.pop(skip, None)
        # Promote the plan's ``name`` into ``planName`` (if the plan
        # doesn't already supply one) BEFORE the unit overlay runs.
        # Knock's unit row carries its own ``name`` field (the unit
        # number, e.g. "4103") which would otherwise clobber the
        # plan's ``name`` (e.g. "1x1A6 Greater HeightsC") and the
        # downstream picker would silently end up with the unit
        # number in ``floor_plan_name``. ``planName`` is the second
        # alias the picker tries (after ``layoutName``) so the plan
        # identity survives the overlay.
        plan_name_seed = plan.get("name") if isinstance(plan, dict) else None
        if plan_name_seed and "planName" not in merged:
            merged["planName"] = plan_name_seed
        # Now overlay the unit. ``v not in (None, "")`` keeps the plan
        # value when the unit has a blank.
        for k, v in u.items():
            if v not in (None, ""):
                merged[k] = v
        # Knock-shape canonicalisation: when a unit row has a foreign
        # key + a ``name`` field, ``name`` is the per-apartment unit
        # number (e.g. "4103"), NOT the floor-plan name. The merged
        # plan-row already carries the floor-plan name in ``layoutName``
        # / ``planName``. Promote the unit's ``name`` to the canonical
        # ``unit_number`` key so the downstream picker doesn't have to
        # know about Knock's specific conventions. ``unit_number`` is
        # the first key the picker checks, so this beats any other
        # vendor-key fallback that might pick up ``id`` etc.
        unit_name = u.get("name") if isinstance(u, dict) else None
        if unit_name not in (None, "") and "unit_number" not in u:
            merged["unit_number"] = unit_name
        out.append(merged)

    # Gap 1 (2026-05-19): append unmatched plans as plan-shape rows so
    # plans without current available units still surface downstream as
    # plan_summaries. See class docstring for rationale.
    for plan in plan_list:
        if not isinstance(plan, dict):
            continue
        plan_id = plan.get("id")
        if plan_id is None or plan_id in referenced_plan_ids:
            continue
        plan_row = dict(plan)
        # Strip every alias the row-picker would walk for ``unit_number``
        # so the resulting v2 row has no natural identity and the
        # classifier in extraction/classify.py routes it to plan_summaries.
        for k in _UNIT_NUMBER_PICKER_KEYS:
            plan_row.pop(k, None)
        for skip in _PLAN_BOOKKEEPING_DROP:
            plan_row.pop(skip, None)
        # Promote ``name`` -> ``planName`` so the picker's floor-plan-name
        # cascade reads it; without this the plan's name would fall
        # through to the generic ``name`` alias and could be misread
        # downstream as a unit number on some vendors.
        plan_name_seed = plan.get("name")
        if plan_name_seed and "planName" not in plan_row:
            plan_row["planName"] = plan_name_seed
            plan_row.pop("name", None)
        out.append(plan_row)
    return out


def find_unit_arrays(blob: Any, min_signals: int = 2) -> list[list[dict]]:
    """Recursively walk ``blob`` and return every list whose first dict-typed
    item satisfies :func:`_item_has_unit_signals`.

    Designed for SSR JSON blobs that bury inventory under vendor-specific
    paths (Razz: ``initialStoreState.$inventory.units`` 4 levels deep; some
    Wix / custom CMSes go even deeper). Returns each candidate list in
    discovery order. Caller is responsible for deduplicating units between
    lists if multiple matches are returned.

    Node-visit cap (``_WALK_MAX_NODES``) bounds the worst case on
    pathological / mis-rendered payloads.
    """
    if blob is None:
        return []
    results: list[list[dict]] = []
    visited = 0
    stack: list[Any] = [blob]
    while stack:
        node = stack.pop()
        visited += 1
        if visited > _WALK_MAX_NODES:
            break
        if isinstance(node, list):
            # If this list itself is unit-shaped, capture it; do NOT recurse
            # into its items (each item would just yield itself again).
            if node and any(
                _item_has_unit_signals(it, min_signals) for it in node[:5]
            ):
                results.append([it for it in node if isinstance(it, dict)])
                continue
            # Otherwise, look inside for nested unit arrays.
            for child in node:
                if isinstance(child, (dict, list)):
                    stack.append(child)
        elif isinstance(node, dict):
            for v in node.values():
                if isinstance(v, (dict, list)):
                    stack.append(v)
    return results


def _extract_rent(u: dict) -> tuple[int | None, int | None]:
    """Extract (rent_low, rent_high) from a unit/floorplan dict.

    Handles flat keys, nested dicts ``{rent: {min: X, max: Y}}``, and
    nested lists ``{rentTerms: [{rent: 1200, term: 12}]}``.
    """
    _LO_KEYS = (
        "price", "minRent", "askingRent", "rent", "monthlyRent",
        "minPrice", "startingPrice", "base_rent", "baseRent",
        "display_price", "monthly_rent", "market_rent", "rentTerms", "pricing",
    )
    _HI_KEYS = ("maxRent", "price_max", "max_price", "maxPrice", "rent_max")

    lo: int | None = None
    hi: int | None = None

    for k in _LO_KEYS:
        v = u.get(k)
        if v is None:
            continue
        if isinstance(v, dict):
            lo = _money_to_int(
                v.get("min") or v.get("low") or v.get("amount")
                or v.get("effectiveRent") or v.get("value")
            )
            hi = _money_to_int(v.get("max") or v.get("high"))
            if lo:
                break
            continue
        if isinstance(v, list) and v and isinstance(v[0], dict):
            best = None
            for term in v:
                r = _money_to_int(term.get("rent") or term.get("price") or term.get("amount"))
                if r and (best is None or r < best):
                    best = r
            if best:
                lo = best
                break
            continue
        lo = _money_to_int(v)
        if lo:
            break

    for k in _HI_KEYS:
        v = u.get(k)
        if v is not None:
            hi = _money_to_int(v)
            if hi:
                break
    if hi is None:
        hi = lo

    return (lo, hi)


# ── JSON-LD helpers ────────────────────────────────────────────────────────────


def _jsonld_type_matches(item: dict) -> bool:
    t = item.get("@type")
    if isinstance(t, list):
        return any(isinstance(x, str) and x in TARGET_JSONLD_TYPES for x in t)
    return isinstance(t, str) and t in TARGET_JSONLD_TYPES


def _jsonld_floor_size(item: dict) -> str:
    fs = item.get("floorSize")
    if isinstance(fs, dict):
        # Schema.org QuantitativeValue: ``{"@type":"QuantitativeValue",
        # "minValue":910,"maxValue":910,"unitCode":"SQFT"}``. Pick min
        # (single-value plans have min==max). Observed on Entrata
        # "ProspectPortal CMS" deployments — Riviera at West Village
        # (PID 12064), Royale (PID 21092), and on prospectportal.com
        # direct-host properties more broadly. Before this, _read_num
        # returned ``""`` and every Entrata-CMS plan-level extract
        # shipped sqft=0.
        v = fs.get("value", "")
        if v in (None, ""):
            v = fs.get("minValue", "")
        if v in (None, ""):
            v = fs.get("maxValue", "")
        return str(v) if v not in (None, "") else ""
    if fs in (None, ""):
        return ""
    return str(fs)


def _walk_jsonld(node: Any, out: list[dict]) -> None:
    """JSON-LD may nest matching items inside @graph, itemListElement, etc."""
    if isinstance(node, dict):
        if _jsonld_type_matches(node):
            out.append(node)
        for v in node.values():
            _walk_jsonld(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_jsonld(v, out)


def _jsonld_item_has_unit_signal(item: dict) -> bool:
    """True if a JSON-LD node has offers/pricing/rooms data — i.e. unit-level.

    Two signal-detection passes. The first uses Schema.org canonical keys
    (``offers``, ``numberOfRooms``, ``floorSize``); the second runs the
    item's keys through ``normalize_field_key`` so PMS-vendor variants
    (``floorPlanName``, ``numberOfBedrooms``, ``monthlyRent``,
    ``bedroomCount``, ``squareFootage`` …) also count as signals. Without
    the second pass, ApartmentComplex blocks that ship vendor-specific
    keys are rejected as "no unit signal" even when the data is plainly
    present (PID 290347 29washington.com observed 2026-05-14).
    """
    offers = item.get("offers")
    if isinstance(offers, dict) and (
        offers.get("price") or offers.get("lowPrice") or offers.get("highPrice")
    ):
        return True
    if isinstance(offers, list) and offers:
        return True
    num_rooms = item.get("numberOfRooms")
    if isinstance(num_rooms, dict):
        num_rooms = num_rooms.get("value")
    try:
        if num_rooms not in (None, "") and float(num_rooms) > 0:
            return True
    except (TypeError, ValueError):
        pass
    if item.get("floorSize"):
        return True
    t = item.get("@type")
    t_list = t if isinstance(t, list) else [t]
    if any(
        x in ("Apartment", "FloorPlan", "Residence", "Offer", "SingleFamilyResidence")
        for x in t_list
        if isinstance(x, str)
    ):
        return True
    # Second pass: any key that normalises to a canonical unit signal counts.
    # Picks up Schema.org camelCase + PMS-vendor variants without modifying
    # the item or breaking downstream extractors that read Schema.org keys
    # directly.
    try:
        from ma_poc.pms.signal_engine.floor_plan_signals import (
            normalize_field_key as _norm_key,
        )
    except Exception:
        return False
    _unit_signal_canon = {
        "rent", "min_rent", "max_rent",
        "sqft",
        "bedrooms", "bathrooms",
        "floor_plan_name",
        "unit_number", "unit_id",
        "available_date",
    }
    for k, v in item.items():
        if not isinstance(k, str) or k.startswith("@"):
            continue
        if v in (None, ""):
            continue
        if _norm_key(k) in _unit_signal_canon:
            return True
    return False


# ── Quality checks ─────────────────────────────────────────────────────────────


def _is_low_signal_units(units: list[dict]) -> bool:
    """True if every unit is missing both a rent and a unit_number/unit_id."""
    if not units:
        return True
    for u in units:
        has_rent = bool(
            u.get("rent_range") or u.get("market_rent_low")
            or u.get("market_rent_high") or u.get("rent")
        )
        has_id = bool(
            (u.get("unit_number") or "").strip() or (u.get("unit_id") or "").strip()
        )
        if has_rent or has_id:
            return False
    return True


def _units_below_expected(
    units: list[dict], expected: int | None, floor_ratio: float = 0.2
) -> bool:
    """True if units is materially smaller than the known expected count."""
    if not expected or expected <= 0 or not units:
        return False
    if len(units) >= max(1, int(expected * floor_ratio)):
        return False
    return all(
        not (u.get("rent_range") or u.get("market_rent_low") or u.get("rent"))
        for u in units
    )


# ── SightMap dedicated parser ──────────────────────────────────────────────────


def parse_sightmap_payload(body: Any, url: str) -> list[dict]:
    """SightMap floorplan+unit join — adapter-compatible output shape."""
    units_out: list[dict] = []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        return units_out

    raw_units = data.get("units") or []
    raw_fps = data.get("floor_plans") or []
    if not isinstance(raw_units, list) or not raw_units:
        return units_out

    fp_by_id: dict[str, dict] = {}
    for fp in raw_fps if isinstance(raw_fps, list) else []:
        if isinstance(fp, dict) and fp.get("id") is not None:
            fp_by_id[str(fp["id"])] = fp

    for u in raw_units:
        if not isinstance(u, dict):
            continue
        fp = fp_by_id.get(str(u.get("floor_plan_id") or ""), {})

        price = u.get("price")
        price_i: int | None = None
        if isinstance(price, (int, float)) and price > 0:
            price_i = int(price)
        else:
            price_i = _money_to_int(str(u.get("display_price") or ""))

        area = u.get("area")
        if isinstance(area, (int, float)) and area > 0:
            sqft = str(int(area))
        else:
            sqft = str(u.get("display_area") or "").strip()

        beds = fp.get("bedroom_count")
        baths = fp.get("bathroom_count")
        # B3 (2026-05-16): unwrap dict-shaped names — see PID 161 Hickory Mill.
        name = _unwrap_name(fp.get("name")) or _unwrap_name(fp.get("filter_label")) or ""

        if beds == 0 or (isinstance(name, str) and "studio" in name.lower()):
            bed_label = "Studio"
        elif beds is not None:
            bed_label = f"{beds} Bedroom"
        else:
            bed_label = ""

        units_out.append({
            "floor_plan_name": str(name),
            "bed_label": bed_label,
            "bedrooms": str(beds) if beds is not None else "",
            "bathrooms": str(baths) if baths is not None else "",
            "sqft": sqft,
            "unit_number": str(u.get("unit_number") or u.get("label") or ""),
            "floor": str(u.get("floor_id") or ""),
            "building": str(u.get("building") or ""),
            "rent_range": f"${price_i:,}" if price_i else str(u.get("display_price") or ""),
            "deposit": "",
            "concession": str(u.get("specials_description") or ""),
            "availability_status": "AVAILABLE",
            "available_units": "1",
            "availability_date": str(u.get("available_on") or u.get("display_available_on") or ""),
            "source_api_url": url,
            "extraction_tier": "TIER_1_API_SIGHTMAP",
        })
    return units_out


# ── RealPage dedicated parser ──────────────────────────────────────────────────


def _to_iso_date(s: Any) -> str | None:
    """Best-effort ISO date parse from common formats."""
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    import re as _re
    m = _re.match(r"^(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = _re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
    if m:
        mm, dd, yy = m.group(1), m.group(2), m.group(3)
        if len(yy) == 2:
            yy = "20" + yy
        return f"{int(yy):04d}-{int(mm):02d}-{int(dd):02d}"
    try:
        from datetime import datetime
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date().isoformat()
    except (ValueError, TypeError):
        return None


def realpage_units_from_body(body: Any, source_url: str) -> list[dict]:
    """Parse RealPage API responses (api.ws.realpage.com).

    Handles two endpoint shapes:
      /floorplans → {response: {floorplans: [...]}}
      /units      → {response: [{unitNumber, rent, availableDate, ...}]}

    Returns records in the internal shape (unit_id / market_rent_low /
    market_rent_high). Use ``realpage_units_to_adapter_shape`` for the
    adapter-compatible output.
    """
    out: list[dict] = []
    if not isinstance(body, dict):
        return out
    resp = body.get("response")
    if resp is None:
        return out

    if isinstance(resp, dict) and "floorplans" in resp:
        for fp in resp.get("floorplans") or []:
            if not isinstance(fp, dict):
                continue
            # B3 (2026-05-16): unwrap dict-shaped name fields.
            fp_name = _unwrap_name(fp.get("name"))
            fp_id = str(fp.get("id") or fp_name or "")
            beds = fp.get("bedRooms") or fp.get("bedrooms")
            sqft = fp.get("sqft") or fp.get("squareFeet")
            sqft_v = int(sqft) if isinstance(sqft, (int, float)) and sqft > 0 else None
            lo = _money_to_int(fp.get("minRent") or fp.get("rentMin"))
            hi = _money_to_int(fp.get("maxRent") or fp.get("rentMax")) or lo
            out.append({
                "unit_id": fp_id,
                "market_rent_low": lo,
                "market_rent_high": hi,
                "available_date": None,
                "lease_link": None,
                "concessions": None,
                "amenities": None,
                "floorplan_image_url": fp.get("imageUrl") or fp.get("image") or None,
                "_sqft": sqft_v,
                "_floor_plan": fp_name or fp_id,
                "_bedrooms": beds,
            })
        return out

    if isinstance(resp, list):
        for u in resp:
            if not isinstance(u, dict):
                continue
            uid = str(u.get("unitNumber") or u.get("unit_number") or u.get("unitId") or "")
            if not uid:
                continue
            lo = _money_to_int(u.get("rent") or u.get("minRent") or u.get("bestPrice"))
            hi = _money_to_int(u.get("maxRent")) or lo
            if lo is not None and (lo < _RENT_MIN or lo > _RENT_MAX):
                continue
            sqft_v = u.get("sqft") or u.get("squareFeet")
            out.append({
                "unit_id": uid,
                "market_rent_low": lo,
                "market_rent_high": hi,
                "available_date": _to_iso_date(u.get("availableDate") or u.get("available_date")),
                "lease_link": u.get("applyOnlineUrl") or None,
                "concessions": u.get("concessions") or u.get("specials") or None,
                "amenities": None,
                "floorplan_image_url": (
                    u.get("floorPlanImage") or u.get("floorplanImage") or u.get("imageUrl") or None
                ),
                "_sqft": int(sqft_v) if isinstance(sqft_v, (int, float)) and sqft_v > 0 else None,
                # B3 (2026-05-16): unwrap dict-shaped floorPlanName.
                "_floor_plan": _unwrap_name(u.get("floorPlanName")) or _unwrap_name(u.get("floorplanName")) or "",
                "_bedrooms": u.get("bedrooms") or u.get("bedRooms"),
            })
        return out

    return out


def realpage_units_to_adapter_shape(body: Any, url: str) -> list[dict]:
    """RealPage /units parser translated to the adapter output shape."""
    internal = realpage_units_from_body(body, url) or []
    out: list[dict] = []
    for u in internal:
        lo = u.get("market_rent_low")
        hi = u.get("market_rent_high")
        if lo is not None and hi is not None and lo != hi:
            rent_range = f"${lo:,} - ${hi:,}"
        elif lo is not None:
            rent_range = f"${lo:,}"
        else:
            rent_range = ""
        beds = u.get("_bedrooms")
        name = u.get("_floor_plan") or ""
        if beds == 0 or (not beds and "studio" in str(name).lower()):
            bed_label = "Studio"
        elif beds not in (None, ""):
            bed_label = f"{beds} Bedroom"
        else:
            bed_label = ""
        sqft = u.get("_sqft")
        out.append({
            "floor_plan_name": str(name),
            "bed_label": bed_label,
            "bedrooms": str(beds) if beds not in (None, "") else "",
            "bathrooms": "",
            "sqft": str(sqft) if sqft is not None else "",
            "unit_number": str(u.get("unit_id") or ""),
            "floor": "",
            "building": "",
            "rent_range": rent_range,
            "deposit": "",
            "concession": str(u.get("concessions") or ""),
            "availability_status": "AVAILABLE",
            "available_units": "",
            "availability_date": str(u.get("available_date") or ""),
            "source_api_url": url,
            "extraction_tier": "TIER_1_API",
        })
    return out


# ── Main generic API parser ────────────────────────────────────────────────────

_LIST_KEYS = (
    "floorPlans", "floor_plans", "FloorPlans",
    "units", "Units", "apartments", "Apartments",
    "availabilities", "Availabilities", "results", "items",
)


def _emit_signal_inspection(
    resp: dict,
    data: Any,
    candidates: list,
    qualifier_admitted: bool,
    qualifier_reason: str,
    property_id: str | None = None,
) -> None:
    """Emit a per-response key-classification trace for offline alias-table tuning.

    Captures the response URL + observed keys + which keys normalize_field_key
    matched into canonical signal keys + which keys LOOK unit-shaped but
    didn't normalize (alias-table miss candidates). Weekly aggregation over
    `keys_unmatched_unit_shape` produces the canonical "missing aliases" report.

    Sampled by SIGNAL_INSPECTION_SAMPLE_RATE env (default 10%) — sample key is
    ``property_id + scrape_date`` so the same property is always or never
    sampled on a given day. Always-on for properties on the FAILED_NO_DATA
    allowlist (when present in env).

    Bug #10 fix (2026-05-16): the original implementation read
    ``resp.get("property_id")`` but API-response dicts carry only ``url`` /
    ``body`` (per the L1 fetcher's network_log shape) — never property_id.
    The fallback ``"unknown"`` collapsed every response into one SHA bucket,
    so the sample either fired for everything or nothing on a given day. On
    the 2026-05-16 cloud run, 0 of an expected ~500 SIGNAL_INSPECTION events
    were emitted across 4982 properties. The new ``property_id`` parameter
    lets the caller plumb the canonical_id from AdapterContext so the sample
    is genuinely per-property.

    Best-effort — telemetry must never break extraction.
    """
    try:
        import hashlib
        import os
        import datetime as _dt

        # Deterministic sample. Default 10% by SHA-256(property_id + date).
        sample_rate = float(os.getenv("SIGNAL_INSPECTION_SAMPLE_RATE", "0.10"))
        # Bug #10 fix: prefer explicit caller-supplied property_id; fall back
        # to the (always-missing) resp key for back-compat with older callers
        # before finally defaulting to "unknown".
        pid = str(property_id or resp.get("property_id") or "unknown")
        today = _dt.datetime.utcnow().strftime("%Y-%m-%d")
        h = int(hashlib.sha256(f"{pid}|{today}".encode()).hexdigest()[:8], 16)
        if (h % 1000) >= int(sample_rate * 1000):
            return

        # Collect a sample of dict-items from the candidate list (max 3),
        # falling back to data if no candidates were resolved.
        sample_items: list[dict] = []
        if candidates:
            for c in candidates[:3]:
                if isinstance(c, dict):
                    sample_items.append(c)
        elif isinstance(data, dict):
            sample_items.append(data)

        # Unit-shaped key heuristic: a key NAME that contains any of these
        # tokens almost certainly represents per-unit data even if the alias
        # table missed it.
        _UNIT_SHAPE_TOKENS = (
            "bed", "bath", "rent", "price", "sqft", "sq_ft", "sqf", "size",
            "area", "unit", "floor", "plan", "available", "vacant",
        )

        from ma_poc.pms.signal_engine.floor_plan_signals import (
            normalize_field_key as _norm,
        )

        observed: list[str] = []
        matched: dict[str, str] = {}
        unmatched_unit_shape: list[str] = []
        for item in sample_items:
            for k in item.keys():
                if not isinstance(k, str) or k.startswith("@"):
                    continue
                if k not in observed:
                    observed.append(k)
                canon = _norm(k)
                if canon in _CANON_UNIT_SIGNAL_KEYS:
                    matched.setdefault(k, canon)
                else:
                    kl = k.lower()
                    if any(tok in kl for tok in _UNIT_SHAPE_TOKENS):
                        if k not in unmatched_unit_shape:
                            unmatched_unit_shape.append(k)

        from ma_poc.observability.events import EventKind, emit
        emit(
            EventKind.SIGNAL_INSPECTION,
            pid,
            url=str(resp.get("url") or "")[:300],
            source_kind="api",
            item_count=len(candidates) if isinstance(candidates, list) else 0,
            keys_observed=observed[:20],
            keys_matched=matched,
            keys_unmatched_unit_shape=unmatched_unit_shape[:20],
            qualifier_admitted=bool(qualifier_admitted),
            qualifier_reason=str(qualifier_reason)[:80],
        )
    except Exception:
        pass  # never let telemetry mask extraction


def parse_api_responses(
    api_responses: list[dict],
    *,
    property_id: str | None = None,
) -> list[dict]:
    """Parse captured API JSON into normalised unit/floor-plan records.

    Handles SightMap (dedicated parser), generic REST, and GraphQL-style
    responses with 50+ key-name variants. Output is adapter-compatible
    (``floor_plan_name``, ``bed_label``, ``rent_range``, etc.).

    Bug #10 fix (2026-05-16): accepts ``property_id`` for
    ``_emit_signal_inspection`` deterministic per-PID sampling. Callers
    should pass ``ctx.property_id`` when available; legacy callers that
    omit it still work (signal-inspection bucket falls back to "unknown",
    matching pre-fix behaviour rather than breaking the parser).
    """
    units: list[dict] = []
    seen: set[str] = set()
    skipped_no_fields = 0

    for resp in api_responses:
        url = resp["url"]
        data = resp["body"]

        host = urllib.parse.urlparse(url).netloc.lower()
        if "sightmap.com" in host:
            sm_units = parse_sightmap_payload(data, url)
            for u in sm_units:
                key = u["unit_number"] or f"{u['floor_plan_name']}|{u['sqft']}|{u['rent_range']}"
                if key and key not in seen:
                    seen.add(key)
                    units.append(u)
            if sm_units:
                continue

        candidates: list[dict] = []
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = _find_list(data, _LIST_KEYS)
            if not candidates:
                inner = data.get("data") if isinstance(data.get("data"), dict) else None
                if not inner and isinstance(data.get("response"), dict):
                    inner = data.get("response")
                if isinstance(inner, dict):
                    candidates = _find_list(inner, _LIST_KEYS)
                    if not candidates:
                        for v in inner.values():
                            if isinstance(v, dict):
                                candidates = _find_list(v, _LIST_KEYS)
                                if candidates:
                                    break

        # Fallback: recursive search for ANY array whose items have >= 2
        # canonical unit-signal keys (after vendor-variant normalisation).
        # Picks up CMS inventory blobs that bury units under non-standard
        # paths (Razz / MyRazz: ``initialStoreState.$inventory.units`` 4
        # levels deep; observed 678 units on PID 246710 8181medcenter).
        # Only runs when the keyed walker found nothing — the keyed walker
        # is cheaper and produces cleaner matches when its key names hit.
        if not candidates and isinstance(data, (dict, list)):
            try:
                walker_lists = find_unit_arrays(data, min_signals=2)
            except Exception:
                walker_lists = []
            if walker_lists:
                # Knock-shape detector (2026-05-19): when the response
                # holds BOTH a plan list and a unit list joined by a
                # foreign key (``layoutId`` / ``floorPlanId`` / etc.),
                # the unit list is authoritative — it's the one with
                # ``displayPrice`` / ``availableOn`` while the plan list
                # only has area / beds / baths. Pre-fix the "largest
                # wins" rule below picked the plan list (60 plans vs 30
                # units on Knock PID 253774) and shipped 60 plan-level
                # rows with null rent / null availability.
                try:
                    pair = _detect_plan_unit_pair(walker_lists)
                except Exception:
                    pair = None
                if pair is not None:
                    plan_list, unit_list, fk_key = pair
                    candidates = _merge_units_with_plans(
                        unit_list, plan_list, fk_key
                    )
                else:
                    # Generic fallback: largest list wins. It's almost
                    # always the per-unit inventory rather than a 5-row
                    # floor-plan summary that would also pass the signal
                    # gate.
                    walker_lists.sort(key=len, reverse=True)
                    candidates = walker_lists[0]

        # RC-TRACE (2026-05-15 PM): sampled trace event for offline alias
        # analysis. Emits keys_observed + keys_matched + keys_unmatched_unit_shape
        # for ~10% of properties (deterministic by SHA-256(pid|date)). The
        # `keys_unmatched_unit_shape` field is the gold output: vendor key
        # names that look unit-shaped (contain "bed"/"bath"/"rent"/"sqft"/etc.)
        # but didn't normalize to a canonical alias. Weekly aggregation
        # surfaces the top N misses as alias-table candidates. Never raises.
        _emit_signal_inspection(
            resp,
            data,
            candidates if isinstance(candidates, list) else [],
            qualifier_admitted=bool(candidates),
            qualifier_reason=("admitted_via_keyed_walker_or_fallback"
                              if candidates else "no_candidates_resolved"),
            property_id=property_id,  # Bug #10 fix: per-PID sampling
        )

        for item in candidates:
            if not isinstance(item, dict):
                continue

            name = _get(item,
                # Knock RentManager denormalises the plan name onto each
                # unit dict as ``layoutName`` — picked before the generic
                # ``name`` alias because on Knock the unit's own ``name``
                # field carries the UNIT number ("4103"), not the plan.
                "layoutName", "layout_name",
                "planName", "plan_name",
                "floorPlanName", "floor_plan_name",
                "FloorPlanName", "floorplan_name", "floorplan-name",
                "unitType", "unit_type",
                # Generic last-resort: many vendors put the plan name in
                # ``name``. Only safe because the unit_number picker
                # below also accepts ``name`` and beats us to it for
                # vendors where ``name`` is the unit identifier.
                "name", "title",
            )
            rent_lo = _get(item,
                "minRent", "rent_min", "min_rent", "startingFrom",
                "starting_rent", "askingRent", "rent", "minPrice",
                "startingPrice", "MinRent", "price", "base_rent",
                "baseRent", "display_price", "displayPrice",
                "monthlyRent", "monthly_rent",
            )
            rent_hi = _get(item,
                "maxRent", "rent_max", "max_rent", "maxAskingRent",
                "endingAt", "MaxRent", "max_price", "maxPrice", "price_max",
            )
            beds = _get(item,
                "bedrooms", "beds", "bedroom_count", "numBedrooms",
                "bd", "Bedrooms", "BedroomCount", "bedroomCount",
                "num_bedrooms", "no_of_bedroom", "no_of_bedrooms",
            )
            baths = _get(item,
                "bathrooms", "baths", "bathroom_count", "numBathrooms",
                "ba", "Bathrooms", "BathroomCount", "bathroomCount",
            )
            sqft = _get(item,
                "sqft", "squareFeet", "square_feet", "minSqft", "size",
                "SquareFeet", "Sqft", "sqftMin", "area", "square_footage",
                "squareFootage", "display_area", "displayArea",
            )
            sqft_max = _get(item, "maxSqft", "sqftMax", "squareFeetMax", "max_area")
            avail = _get(item,
                "availableCount", "available_count", "numAvailable",
                "unitsAvailable", "AvailableCount", "units_available",
            )
            avail_dt = _get(item,
                "availableDate", "available_date", "moveInDate", "moveInReady",
                "availableFrom", "AvailableDate", "NextAvailDate",
                "available_on", "availableOn", "display_available_on", "readyDate",
            )
            status = _get(item, "status", "availability_status", "leaseStatus", "Status", "unit_status")
            # ``id`` is intentionally last so it only fires when no specific
            # unit_number key matched — guards against picking a row's
            # database PK on JSON shapes where ``id`` is non-unit semantics.
            # Razz/MyRazz uses ``id`` directly as the unit identifier (e.g.
            # ``{id: '21110', rent: {...}, sqft: {...}}``).
            unit_num = _get(item,
                "unitNumber", "unit_number", "unitId", "unit_id", "UnitNumber",
                "label", "display_unit_number", "unitCode", "unit_code",
                "id",
            )
            floor_num = _get(item, "floor", "floorNumber", "FloorNumber", "floor_id", "floorId")
            building = _get(item, "building", "buildingName", "BuildingName", "building_name")
            plan_type = _get(item, "floorPlanType", "type", "bedBath", "BedBath")
            deposit = _get(item, "deposit", "securityDeposit", "SecurityDeposit", "security_deposit")
            concession = _get(item,
                "concession", "special", "promotion", "Concession", "Special",
                "specials_description", "specialsDescription",
            )

            if not any([name, beds, sqft, rent_lo]):
                skipped_no_fields += 1
                continue

            dedup_key = unit_num or f"{name}|{beds}|{sqft}|{rent_lo}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            lo_i = _money_to_int(rent_lo)
            hi_i = _money_to_int(rent_hi)
            if lo_i is not None and hi_i is not None and lo_i != hi_i:
                rent_display = f"${lo_i:,} - ${hi_i:,}"
            elif lo_i is not None:
                rent_display = f"${lo_i:,}"
            else:
                rent_display = ""

            sqft_display = sqft
            if sqft and sqft_max and sqft != sqft_max:
                sqft_display = f"{sqft} - {sqft_max}"

            if beds == "0" or (not beds and "studio" in (name or "").lower()):
                bed_label = "Studio"
            elif beds:
                bed_label = f"{beds} Bedroom"
            else:
                bed_label = plan_type or "?"

            units.append({
                "floor_plan_name": name,
                "bed_label": bed_label,
                "bedrooms": beds,
                "bathrooms": baths,
                "sqft": sqft_display,
                "unit_number": unit_num,
                "floor": floor_num,
                "building": building,
                "rent_range": rent_display,
                "deposit": deposit,
                "concession": concession,
                "availability_status": status or ("AVAILABLE" if (avail and avail != "0") else ""),
                "available_units": avail,
                "availability_date": avail_dt,
                "source_api_url": url,
                "extraction_tier": "TIER_1_API",
            })

    # Stub filter: when at least one row carries real per-apartment
    # data (unit_number OR rent_range), drop rows that have NEITHER. The
    # filter exists to suppress phantom-empty noise (parser quirks that
    # emit zero-information rows alongside real units).
    #
    # Gap 1 (2026-05-19): plan-summary rows emitted by
    # ``_merge_units_with_plans`` for unmatched plans intentionally lack
    # ``unit_number`` (so the downstream classifier routes them to
    # ``plan_summaries``). When the source plan also has null rent, the
    # row lacks ``rent_range`` too — but it still carries useful
    # plan-level structure (``floor_plan_name`` + ``bedrooms`` / ``sqft``).
    # Keep such rows so they reach ``extraction.classify`` and ship as
    # ``floor_plans[]`` per playbook §8.18, instead of being silently
    # dropped on the way out.
    def _is_real_unit_or_plan(u: dict) -> bool:
        if u.get("unit_number") or u.get("rent_range"):
            return True
        fp_name = u.get("floor_plan_name") or ""
        has_dim = bool(u.get("bedrooms")) or bool(u.get("sqft"))
        return bool(fp_name) and has_dim

    has_real = any(_is_real_unit_or_plan(u) for u in units)
    stub_count = 0
    if has_real:
        before = len(units)
        units = [u for u in units if _is_real_unit_or_plan(u)]
        stub_count = before - len(units)

    log.debug(
        "parse_api_responses: %d APIs → %d units (skipped: %d no-fields, %d stubs)",
        len(api_responses), len(units), skipped_no_fields, stub_count,
    )
    return units


__all__ = [
    "TARGET_JSONLD_TYPES",
    "_RENT_MIN",
    "_RENT_MAX",
    "_UNIT_ID_KEYS",
    "_RENT_KEYS",
    "_get",
    "_money_to_int",
    "_find_list",
    "_extract_rent",
    "_jsonld_type_matches",
    "_jsonld_floor_size",
    "_walk_jsonld",
    "_jsonld_item_has_unit_signal",
    "_is_low_signal_units",
    "_units_below_expected",
    "parse_sightmap_payload",
    "realpage_units_from_body",
    "realpage_units_to_adapter_shape",
    "parse_api_responses",
]
