"""Shared parsing helpers used by multiple adapters.

Extracted from ``scripts/entrata.py`` so that each adapter can import
lightweight helpers without depending on the full scraper engine.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit


def money_to_int(s: str) -> int | None:
    """Parse '$1,450', '1450.00', '1,450 USD' -> 1450. Returns None on failure."""
    if not s:
        return None
    # 2026-05-19: the prior ``re.sub(r"[^\d.]", "", s)`` CONCATENATED every
    # digit in the string, so a range like ``"$1,200 - $1,400"`` became
    # ``12001400`` — a plausible-looking but fabricated rent that then
    # passed every downstream guard (>1, quality gate). Take the FIRST
    # monetary token instead: single values ("$1,450", "1450.00",
    # "1,450 USD", "$1450/mo") are unchanged; a range resolves to its low
    # bound (correct for rent_low; far better than a poisoned value).
    m = re.search(r"\d[\d,]*(?:\.\d{1,2})?", s)
    if not m:
        return None
    cleaned = m.group(0).replace(",", "")
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def get_field(d: dict[str, Any], *keys: str) -> str:
    """Try multiple key names, return first non-empty string found.

    Handles nested rent/sqft objects like ``{rent: {min: 1351, max: 1351}}``
    by extracting the first numeric value from the nested dict.
    """
    for k in keys:
        v = d.get(k)
        if v is None or v == "" or v == [] or v == {}:
            continue
        if isinstance(v, list):
            continue
        if isinstance(v, dict):
            for sub_k in ("min", "low", "amount", "value", "effectiveRent", "max", "high"):
                sv = v.get(sub_k)
                if sv is not None and sv != "":
                    return str(sv)
            continue
        return str(v)
    return ""


def format_rent_range(lo: int | None, hi: int | None) -> str:
    """Format rent range from low/high integers."""
    if lo and hi and lo != hi:
        return f"${lo:,} - ${hi:,}"
    if lo:
        return f"${lo:,}"
    if hi:
        return f"${hi:,}"
    return ""


def bed_label_from(beds: int | None, name: str = "") -> str:
    """Derive human-readable bed label."""
    if beds == 0 or (isinstance(name, str) and "studio" in name.lower()):
        return "Studio"
    if beds is not None:
        return f"{beds} Bedroom"
    return ""


def rent_in_sanity_range(rent: int | None) -> bool:
    """Check if rent falls within $200-$50,000 sanity bounds."""
    if rent is None:
        return True  # null is acceptable
    return 200 <= rent <= 50000


# ── Junk deny-lists (Phase 5) ──────────────────────────────────────────────
# Floor plan names that match these patterns are CMS widget / vendor
# artefacts, not real apartment plans. Observed in the 2026-04-19 run:
# "MODULE_CONCESSIONMANAGER", "[Riedman] The Dean - Standard Lease Magnet -
# Pop-Up - Mobile -Gravity FORMS". Rejecting here prevents them reaching
# the v2 output and skewing success metrics.
_JUNK_PLAN_PATTERNS = (
    re.compile(r"^(MODULE|WIDGET|COMPONENT|CMS|PLUGIN)[_\- ]", re.I),
    re.compile(r"\b(lease\s*magnet|pop[- ]?up|gravity\s*forms?|mobile\s*form)\b", re.I),
    re.compile(r"^\[[^\]]{2,30}\].*?(magnet|pop|form|mobile)\b", re.I),  # vendor-prefixed CMS entries
)

# Unit number tokens that are obviously navigation text or stop-words, not
# real unit identifiers. Observed DOM-scan false positives: "Left", "s",
# "Right", "new". All-lowercase single-word matches only — real unit IDs
# are alphanumeric with digits.
_JUNK_UNIT_TOKENS = frozenset(
    {
        "left",
        "right",
        "up",
        "down",
        "top",
        "bottom",
        "new",
        "more",
        "view",
        "learn",
        "click",
        "here",
        "now",
        "all",
        "one",
        "any",
        "unit",
        "home",
        "page",
        "menu",
        "s",
        "a",
        "an",
        "the",
    }
)


def is_junk_floor_plan(name: Any) -> bool:
    """Return True when a floor-plan name is obviously a CMS artefact.

    Kept lenient to minimise false negatives — only catches the specific
    failure shapes observed in production. Real plan names starting with
    generic words like "Studio" or "The Reserve" still pass through.
    """
    if not name:
        return False
    s = str(name).strip()
    if not s:
        return False
    for pat in _JUNK_PLAN_PATTERNS:
        if pat.search(s):
            return True
    return False


def is_junk_unit_number(val: Any) -> bool:
    """Return True when a unit_number is a stop-word / nav token.

    Real unit identifiers contain digits or are 3+ character alphanumeric
    codes. A bare "Left" or "s" is always an extractor mistake.
    """
    if val is None or val == "":
        return False
    s = str(val).strip()
    if not s:
        return False
    # Accept anything containing a digit — that's a real unit number.
    if any(c.isdigit() for c in s):
        return False
    # Short single-word stop-words.
    if s.lower() in _JUNK_UNIT_TOKENS:
        return True
    # Single character tokens with no digits.
    if len(s) <= 1:
        return True
    return False


# ── Canonical bath / sqft regexes (PR-Parsing-hardening 2026-05-25) ──────
# Single source of truth used by adapters and the generic DOM scanner so
# that fixes to the regex don't have to be propagated across files.
#
# Regression #13 (canary 1ef1060): "1 Bathroom" / "2 Bathrooms" text on
# primeurbanproperties.com fell through the older `(?:bath|ba)\b`
# pattern (the trailing \b after the alternation rejects "bath" when
# followed by "room"). The canonical form below accepts "bath",
# "baths", "bathroom", "bathrooms", and "BA" — case-insensitive.
#
# Regression #16: "1,200 ft²" / "950 ft2" on eaglepointestates.com
# returned -1 because neither the unicode superscript nor the ASCII
# "ft2" form was matched. The canonical form below adds those plus
# "ft^2", "sf", and "square feet|foot|ft".
BATH_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*(?:bath(?:room)?s?|ba)\b",
    re.IGNORECASE,
)

SQFT_RE = re.compile(
    r"(\d[\d,]*)\s*(?:"
    r"sq\.?\s?ft\.?|sqft|"
    # "sf" alone is OK only when followed by a non-word boundary or end —
    # without the lookahead a token like "sfgate" matches "sf".
    r"s\.?f\.?(?=\b|\W|$)|"
    # ft² / ft^2 / ft2 — the unicode superscript ² isn't a \w char so
    # the trailing variants use explicit lookaheads instead of \b.
    r"ft\s*(?:²|\^2|\b2\b|2(?=\W|$))|"
    r"square\s*(?:feet|foot|ft)"
    r")",
    re.IGNORECASE,
)


# Patterns for inferring bed/bath count from a floor-plan / unit name.
# Runs in declared order; first match wins. Patterns are designed to be
# narrow enough that they don't false-positive on unrelated marketing
# copy ("Five Star View" should NOT yield beds=5). The corpus they were
# tuned against is the union of:
#   - ma_poc/config/Floorplan-comparisons.csv     (vendor CSV side)
#   - distinct units.floor_plan_name in production (DB side)
# When extending, add a regression test in
# tests/pms/adapters/test_infer_bed_bath_from_name.py rather than
# loosening an existing pattern.
_STUDIO_RE = re.compile(
    r"\b(?:studio|micro[- ]?studio|efficiency|jr\s*studio|junior\s*studio|"
    r"open[- ]?one|0\s*(?:bd|br|bed|bedroom)s?)\b",
    re.IGNORECASE,
)
# "2BR/2BA", "3 BR / 2.5 BA", "1 Bed 1 Bath" — second token is a bath
# token ("ba" / "bath").
_BED_BATH_PAIR_RE = re.compile(
    r"\b(\d)\s*(?:bd|br|bed(?:room)?s?)\s*[/\\]?\s*"
    r"(\d(?:\.\d)?)\s*(?:ba|bath(?:room)?s?)\b",
    re.IGNORECASE,
)
# Vendor CSV quirk: "1BD/1BR-1", "2BD/1BR-2", "3BD/2.5BR-3" — the second
# "BR" actually means bathroom in this notation. Surfaced from the
# Floorplan-comparisons.csv corpus. A trailing "-N" suffix is allowed
# (the vendor's plan-variant index) so it doesn't break the boundary.
_BED_BATH_VENDOR_BR_RE = re.compile(
    r"\b(\d)\s*BD\s*/\s*(\d(?:\.\d)?)\s*BR\b",
    re.IGNORECASE,
)
# Word-form pair: "One Bedroom One Bath", "Two Bedroom Two Bathroom".
_WORD_BED_BATH_PAIR_RE = re.compile(
    r"\b(one|two|three|four|five)\s*(?:bd|br|bed(?:room)?s?)\s+"
    r"(one|two|three|four|five)\s*(?:ba|bath(?:room)?s?)\b",
    re.IGNORECASE,
)
# "1x1", "2x2.5", "3 x 2" — common shorthand on RentCafe / Yardi / SightMap.
# Anchored on word boundaries so "1x12" isn't read as bed=1, bath=12.
_X_PAIR_RE = re.compile(r"(?<![\d.])(\d)\s*[xX]\s*(\d(?:\.\d)?)(?!\d)")
# "1/1", "2/2.5" — same shorthand with a slash separator. Excluded:
# "1/2/3" (looks like a date or list, not a bed/bath).
_SLASH_PAIR_RE = re.compile(r"(?<![\d./])(\d)\s*/\s*(\d(?:\.\d)?)(?!\s*/)")
# Standalone bed counts: "1BR", "2 Bed", "Three Bedroom".
_BED_ONLY_RE = re.compile(
    r"\b(\d)\s*(?:bd|br|bed(?:room)?s?)\b",
    re.IGNORECASE,
)
_WORD_BED_RE = re.compile(
    r"\b(one|two|three|four|five)\s*(?:bd|br|bed(?:room)?s?)\b",
    re.IGNORECASE,
)
# Standalone bath count: "1 Bath", "2.5 BA", "1 Bathroom", "2 Bathrooms".
# Used only when bed was already inferred by a separate pass — never
# seeds beds from a bath. Aliases the canonical ``BATH_RE`` above; kept
# as a separate name only to keep the existing infer_bed_bath callsite
# independent of the public canonical regex.
_BATH_ONLY_RE = BATH_RE
_WORD_TO_INT = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}


def infer_bed_bath_from_name(
    name: str | None,
) -> tuple[int | None, float | None]:
    """Best-effort inference of (beds, baths) from a plan / unit name.

    Returns ``(None, None)`` when nothing reliable can be inferred. The
    function never raises and never invents data — every output value is
    grounded in a literal substring of ``name``.

    Apply only as a *fallback* when the adapter / API didn't deliver
    these fields. Never overwrite a non-null source value with the
    inferred one — extraction is always more authoritative than name
    parsing.
    """
    if not name or not isinstance(name, str):
        return None, None
    s = name.strip()
    if not s:
        return None, None

    # 1. Studio / micro-studio: beds=0, baths left to its own pass.
    is_studio = bool(_STUDIO_RE.search(s))

    beds: int | None = None
    baths: float | None = None

    # 2a. Vendor "BD/BR" pair (where second BR == bath) — runs before the
    # standard pair so the vendor notation isn't mis-read as bed/bed.
    m = _BED_BATH_VENDOR_BR_RE.search(s)
    if m:
        try:
            beds = int(m.group(1))
            baths = float(m.group(2))
        except ValueError:
            beds = baths = None

    # 2b. Standard explicit bed/bath pair.
    if beds is None and baths is None:
        m = _BED_BATH_PAIR_RE.search(s)
        if m:
            try:
                beds = int(m.group(1))
                baths = float(m.group(2))
            except ValueError:
                beds = baths = None

    # 2c. Word-form pair: "One Bedroom One Bath".
    if beds is None and baths is None:
        m = _WORD_BED_BATH_PAIR_RE.search(s)
        if m:
            beds = _WORD_TO_INT.get(m.group(1).lower())
            baths_word = _WORD_TO_INT.get(m.group(2).lower())
            baths = float(baths_word) if baths_word is not None else None

    # 3. NxN shorthand. Run only if we don't have a pair already.
    if beds is None and baths is None:
        m = _X_PAIR_RE.search(s)
        if m:
            try:
                beds = int(m.group(1))
                baths = float(m.group(2))
            except ValueError:
                beds = baths = None

    # 4. N/N slash shorthand — skip on studios since "0/1" wouldn't be
    # written that way conventionally; falls through to single-bed pass.
    if beds is None and baths is None:
        m = _SLASH_PAIR_RE.search(s)
        if m:
            try:
                beds = int(m.group(1))
                baths = float(m.group(2))
            except ValueError:
                beds = baths = None

    # 5. Single-field passes.
    if beds is None:
        m = _BED_ONLY_RE.search(s)
        if m:
            try:
                beds = int(m.group(1))
            except ValueError:
                beds = None
    if beds is None:
        m = _WORD_BED_RE.search(s)
        if m:
            beds = _WORD_TO_INT.get(m.group(1).lower())
    if baths is None:
        m = _BATH_ONLY_RE.search(s)
        if m:
            try:
                baths = float(m.group(1))
            except ValueError:
                baths = None

    # 6. Studio overrides bed count if no explicit "1 BR" type pattern
    # contradicted it. Studios are sometimes labelled "Studio - 1 Bath",
    # so set beds=0 only when the bed pass found nothing.
    if is_studio and beds is None:
        beds = 0

    # 7. Sanity bounds — apartment plans realistically span 0–7 beds and
    # 0–10 baths. Anything outside is signal that the regex matched a
    # number that wasn't a bed/bath count (e.g. "Loft 12B").
    if beds is not None and not (0 <= beds <= 7):
        beds = None
    if baths is not None and not (0 <= baths <= 10):
        baths = None

    return beds, baths


def compute_floor_plan_id(
    canonical_id: str | None,
    floor_plan_name: str | None,
    beds: int | None,
    baths: float | None,
) -> str | None:
    """Deterministic 12-char id grouping units that share a floor plan.

    Returns ``None`` when there is not enough signal to identify a plan
    — specifically: when neither ``floor_plan_name`` nor any of
    ``beds``/``baths`` is set. Without one of those, two units of the
    same property would collapse to the same id even though they are
    clearly distinct plans.

    Inputs are normalised: name is lowercased + whitespace-collapsed,
    canonical_id is stringified, missing fields use the literal token
    ``"-"`` so e.g. ``(beds=None, baths=1.0)`` doesn't collide with
    ``(beds=1, baths=None)``.

    Two units with the same (canonical_id, name, beds, baths) always
    return the same id, regardless of unit_number / area / extraction
    timestamp. Per-unit area variation is intentionally NOT part of the
    key — that's the whole point of grouping unit-level rows back to
    plan-level rows.
    """
    cid = (canonical_id or "").strip()
    raw_name = floor_plan_name or ""
    name_norm = re.sub(r"\s+", " ", raw_name.strip().lower())

    # Plan-anchor signal: must have at least a name OR a bed/bath value.
    if not name_norm and beds is None and baths is None:
        return None

    beds_part = "-" if beds is None else str(int(beds))
    baths_part = "-" if baths is None else f"{float(baths):g}"
    name_part = name_norm or "-"

    payload = f"{cid}|{name_part}|{beds_part}|{baths_part}".encode()
    return hashlib.sha256(payload).hexdigest()[:12]


def parse_rent_range(rent_range: str) -> tuple[int | None, int | None]:
    """Parse "$1,200 - $1,500" / "$1,295" / "1295-1500" to (low, high) ints.

    Returns (None, None) when the string has no recognisable number. Used by
    the v2 schema transform as a fallback when an adapter emits only the
    formatted ``rent_range`` string but not the numeric low/high fields.
    """
    if not rent_range or not isinstance(rent_range, str):
        return None, None
    # Find all numeric tokens; drop thousands separators.
    nums = [
        int(float(n.replace(",", ""))) for n in re.findall(r"\d[\d,]*", rent_range) if n and n[0].isdigit()
    ]
    if not nums:
        return None, None
    # Rent sanity: reject anything outside the sane band so we don't
    # pick up bedroom counts or sqft that ended up in rent_range.
    nums = [n for n in nums if 200 <= n <= 50000]
    if not nums:
        return None, None
    if len(nums) == 1:
        return nums[0], nums[0]
    return min(nums), max(nums)


def _unwrap_name_blob(val: Any) -> str:
    """Coerce ``val`` to a clean floor-plan-name string.

    Some adapters' API parsers receive a structured floor-plan object
    (``{"name": "B06", "provider_id": "...", ...}``) rather than the
    name string itself. If we accept it as-is, the JSON repr of the dict
    ends up serialised as the floor-plan name in the XLSX output (2,534
    rows seen on 2026-05-13). Unwrap to the ``name`` field when present;
    otherwise fall back to ``str(val)`` for already-string inputs.
    """
    if val is None:
        return ""
    if isinstance(val, dict):
        # Prefer the most common name keys observed across PMS responses.
        for key in ("name", "floor_plan_name", "label", "displayName"):
            inner = val.get(key)
            if isinstance(inner, str) and inner.strip():
                return inner.strip()
        # No string-valued name field — drop the blob (better than serialising
        # the whole dict into the output).
        return ""
    s = str(val).strip()
    # 2026-05-13: a small number of adapters pre-emit json.dumps(dict) into
    # the name slot. Recognise the literal ``{"`` prefix and try one parse.
    if s.startswith("{") and "name" in s[:100]:
        try:
            import json
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return _unwrap_name_blob(parsed)
        except (json.JSONDecodeError, ValueError):
            pass
    return s


def enrich_unit_concession_fields(
    unit: dict[str, Any],
    *,
    property_concession_text: str | None = None,
) -> dict[str, Any]:
    """Backfill canonical concession + offer fields on any unit dict.

    Idempotent. Safe to call on units produced by:
      * ``make_unit_dict`` (already populated — this is a no-op)
      * Raw-dict adapters that bypass ``make_unit_dict`` (e.g.
        ``_api_parser.py``, ``_html_extract.py``, ``knock.py``,
        ``_air_communities.py``, ``_amli.py``)

    Reads source text in this priority:
      1. ``unit['concession_text']`` (canonical, already set)
      2. ``unit['concession']`` (legacy, set by adapters that pass
         ``concession=`` to make_unit_dict OR build raw dicts)
      3. ``property_concession_text`` (caller-provided fallback —
         typically ``result['concessions_text']`` from scraper.py's
         property-level banner capture)

    Whatever source wins, populates these canonical fields in-place:
      * concession_text       — raw text (canonical key)
      * concession            — legacy mirror for back-compat readers
      * concession_text_clean — de-leaked variant (clean_concession_text)
      * _concession_quality   — classifier label (clean/leak/empty)
      * concession_value      — numeric value (normalize_concession)
      * concession_source     — preserved if already set, else None
      * offer_banner          — short offer phrase
      * offer_type            — categorical (free_rent/dollar_off/...)
      * offer_target          — rent/deposit/app_fee/...
      * offer_value           — formatted string with unit ("6 weeks")
      * offer_conditions      — semicolon-delimited key:value pairs

    Returns the same dict (for chaining). Never raises — all extraction
    failures swallow silently and leave the canonical fields as None.
    """
    # Determine the source text (capture-first priority)
    text: str | None = None
    for key in ("concession_text", "concession"):
        v = unit.get(key)
        if isinstance(v, str) and v.strip():
            text = v
            break
    if text is None and property_concession_text:
        if isinstance(property_concession_text, str) and property_concession_text.strip():
            text = property_concession_text

    # Always populate the canonical keys (None when no source)
    cleaned = None
    quality = None
    derived_value = None
    if text:
        try:
            from ma_poc.core.concession_clean import (
                classify_concession_quality,
                clean_concession_text,
            )

            cleaned = clean_concession_text(text)
            quality = classify_concession_quality(text)
        except Exception:
            cleaned = text
            quality = None
        # Derive value only when caller hasn't set it
        if not unit.get("concession_value"):
            try:
                from ma_poc.core.concession_normalize import normalize_concession

                obj = normalize_concession(text)
                if isinstance(obj, dict):
                    inner = obj.get("obj") or {}
                    if isinstance(inner, dict):
                        for slot in ("free", "rr"):
                            v = inner.get(slot)
                            if isinstance(v, dict) and v.get("dollarsLow"):
                                derived_value = float(v["dollarsLow"])
                                break
            except Exception:
                pass

    # Offer taxonomy (5 fields)
    offer_fields: dict[str, Any] = {
        "offer_banner": None,
        "offer_type": None,
        "offer_target": None,
        "offer_value": None,
        "offer_conditions": None,
    }
    if text:
        try:
            from ma_poc.core.offer_extract import extract_offer

            offer_fields = extract_offer(text)
        except Exception:
            pass

    # Stamp the canonical keys (only set when not already populated by adapter)
    unit["concession"] = text or unit.get("concession") or ""
    unit["concession_text"] = text
    if "concession_text_clean" not in unit or unit.get("concession_text_clean") is None:
        unit["concession_text_clean"] = cleaned
    if "_concession_quality" not in unit or unit.get("_concession_quality") is None:
        unit["_concession_quality"] = quality
    if not unit.get("concession_value") and derived_value is not None:
        unit["concession_value"] = derived_value
    elif "concession_value" not in unit:
        unit["concession_value"] = None
    if "concession_source" not in unit:
        unit["concession_source"] = None
    # Offer fields — only set when not already populated
    for k, v in offer_fields.items():
        if unit.get(k) in (None, ""):
            unit[k] = v
    return unit


# ── Floor-plan-name URL-slug fallback (regression #12, canary 1ef1060) ──
# When extraction yields an empty / "~" / "Unknown" plan name AND the
# source URL encodes the plan as a slug, derive a readable name. Observed
# on lifeatalexis.com: `?floorplan=1-bed-1-bath-1992` produced
# floor_plan_name="~" because no DOM element carried the plan text.
#
# Recognised slug locations (case-insensitive):
#   - query param: ``?floorplan=…``, ``?floor_plan=…``, ``?plan=…``,
#                  ``?fp=…``, ``?unit_gallery=…``
#   - path segment AFTER ``/floorplans/`` or ``/floor-plans/`` (when
#     the slug isn't the literal word "floorplans" again)
_EMPTY_PLAN_NAME_TOKENS: frozenset[str] = frozenset({
    "", "~", "-", "—", "n/a", "na", "none", "null", "unknown", "tbd",
})

_PLAN_SLUG_QUERY_KEYS: tuple[str, ...] = (
    "floorplan", "floor_plan", "floorplan_name", "fp", "plan", "unit_gallery",
)

_PLAN_PATH_PREFIXES: tuple[str, ...] = (
    "/floorplans/", "/floor-plans/", "/floorplan/", "/floor-plan/",
)


def _looks_empty_plan_name(name: Any) -> bool:
    """True when ``name`` is the kind of value we want to backfill."""
    if name is None:
        return True
    if not isinstance(name, str):
        return False
    return name.strip().lower() in _EMPTY_PLAN_NAME_TOKENS


def _titleize_slug(slug: str, *, trim_trailing_id: bool = True) -> str:
    """Convert ``"1-bed-1-bath-1992"`` -> ``"1 Bed 1 Bath"``.

    Splits on hyphen / underscore, titlecases each token (digit tokens are
    preserved as-is). When ``trim_trailing_id`` is True, a trailing
    purely-numeric token of 3+ digits is dropped — that's almost always
    the per-unit id concatenated to the plan slug, not part of the plan
    name. ``"1-bed-1-bath-1992"`` -> ``"1 Bed 1 Bath"``; a 1- or 2-digit
    trailing token (e.g. "the-aspen-2") stays put.
    """
    if not slug:
        return ""
    raw = unquote(slug).strip()
    if not raw:
        return ""
    raw = raw.replace("_", "-")
    parts = [p for p in raw.split("-") if p.strip()]
    if not parts:
        return ""
    if trim_trailing_id and len(parts) > 1:
        last = parts[-1]
        if last.isdigit() and len(last) >= 3:
            parts = parts[:-1]
    out_tokens: list[str] = []
    for p in parts:
        if p.isdigit():
            out_tokens.append(p)
        else:
            out_tokens.append(p[0].upper() + p[1:].lower())
    return " ".join(out_tokens).strip()


def derive_plan_name_from_url(url: str | None) -> str:
    """Best-effort plan-name derivation from a floorplan URL.

    Looks first in the query string for any of ``_PLAN_SLUG_QUERY_KEYS``
    (``?floorplan=…`` is the canonical case). If nothing useful is there,
    scans the path for a ``/floorplans/{slug}/`` segment and uses the
    segment immediately after the prefix. Returns "" when neither yields
    a slug — caller is responsible for not overwriting a real name.
    """
    if not url or not isinstance(url, str):
        return ""
    try:
        parts = urlsplit(url)
    except (ValueError, TypeError):
        return ""
    if parts.query:
        try:
            params = parse_qs(parts.query, keep_blank_values=False)
        except (ValueError, TypeError):
            params = {}
        lc_params: dict[str, list[str]] = {k.lower(): v for k, v in params.items()}
        for key in _PLAN_SLUG_QUERY_KEYS:
            vals = lc_params.get(key)
            if vals and vals[0].strip():
                derived = _titleize_slug(vals[0])
                if derived:
                    return derived
    path = (parts.path or "").lower()
    for prefix in _PLAN_PATH_PREFIXES:
        i = path.find(prefix)
        if i < 0:
            continue
        tail = parts.path[i + len(prefix):]
        first_seg = tail.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        if first_seg and first_seg.lower() not in _PLAN_PATH_PREFIXES:
            derived = _titleize_slug(first_seg)
            if derived:
                return derived
    return ""


# ── Unit-number sqft-leak guard (regression #17, canary 1ef1060) ────────
# spearheadproperties.com renders a per-property table where the size cell
# bleeds into the unit-id slot when a generic DOM scanner walks adjacent
# <td> elements:
#   <td>1</td><td>1</td><td>623 sq ft</td><td> 8/21/2026</td>
# The scanner emits unit_number="623 sq ft" and sqft="" (the regex was
# consumed). Defensive cleanup at make_unit_dict time:
#   1. If unit_number contains a sqft signature, strip it.
#   2. If what remains is empty / whitespace, clear unit_number — better
#      to emit "" than a leaked sqft token.
def clean_unit_number(val: str) -> str:
    """Strip leaked sqft text from a unit-number string.

    Returns the cleaned string. When the input contains nothing but sqft
    text, returns "" — caller may emit the row anyway (sqft can be
    recovered separately) but the bogus identifier won't ship.
    """
    if not val or not isinstance(val, str):
        return val or ""
    s = val.strip()
    if not s:
        return ""
    if not SQFT_RE.search(s):
        return s
    cleaned = SQFT_RE.sub("", s).strip()
    cleaned = re.sub(r"[\s,;|/\-]+", " ", cleaned).strip()
    return cleaned


def make_unit_dict(
    *,
    floor_plan_name: str = "",
    bed_label: str = "",
    bedrooms: str = "",
    bathrooms: str = "",
    sqft: str = "",
    unit_number: str = "",
    floor: str = "",
    building: str = "",
    rent_range: str = "",
    rent_low: int | None = None,
    rent_high: int | None = None,
    deposit: str = "",
    concession: str = "",
    concession_text: str | None = None,
    concession_value: float | None = None,
    concession_source: str | None = None,
    availability_status: str = "AVAILABLE",
    available_units: str = "",
    availability_date: str = "",
    lease_term: str = "",
    move_in_date: str = "",
    source_api_url: str = "",
    extraction_tier: str = "",
    source_ids: dict[str, Any] | None = None,
    data_gaps: list[str] | None = None,
    data_quality_flag: str = "",
) -> dict[str, Any]:
    """Build a standard unit dict in the format expected by the pipeline.

    Emits BOTH the human-readable ``rent_range`` string AND the numeric
    ``market_rent_low`` / ``market_rent_high`` fields that the v2 schema
    transform reads. If ``rent_low`` / ``rent_high`` are not supplied but
    ``rent_range`` is, the numeric values are parsed from the string so the
    downstream transform doesn't silently drop rent.

    ``lease_term`` and ``move_in_date`` are plumbed through so parsers that
    learn to extract them don't need further format changes.
    """
    # Prefer explicit ints when passed; otherwise recover from the string.
    if rent_low is None and rent_high is None and rent_range:
        lo, hi = parse_rent_range(rent_range)
        rent_low, rent_high = lo, hi
    # Keep rent_range populated for human-readable output when ints provided
    # but no string was passed.
    if not rent_range and (rent_low or rent_high):
        rent_range = format_rent_range(rent_low, rent_high)

    # Defensive unwrap: some upstream parsers pass the raw API floor-plan
    # dict (``{"name":"B06","provider_id":"4875687"}``) instead of the
    # name string. 2026-05-13 validation pass found 2,534 such rows
    # leaking JSON-blob floor-plan names across Tier-1 API (1,151), Tier-3
    # DOM (628), Tier-1 SightMap (334), Tier-2 JSON-LD (212), and
    # MERGED_CROSS_PAGE (118). Normalising here means every adapter benefits
    # without changing per-adapter signatures.
    floor_plan_name = _unwrap_name_blob(floor_plan_name)

    # Regression #12 (canary 1ef1060): adapter emitted "~" / empty plan
    # names on lifeatalexis.com when the extractor found no plan label in
    # the DOM but the URL slug encoded one (`?floorplan=1-bed-1-bath-1992`
    # → "1 Bed 1 Bath"). Only triggers when the current name is junk AND a
    # slug is available, so well-extracted names pass through unchanged.
    if _looks_empty_plan_name(floor_plan_name):
        derived = derive_plan_name_from_url(source_api_url)
        if derived:
            floor_plan_name = derived

    # Regression #17 (canary 1ef1060): spearheadproperties.com generic DOM
    # scan emitted unit_number="623 sq ft" because adjacent <td> cells
    # (sqft column + unit column) collapsed during text extraction. Strip
    # sqft-shaped substrings from unit_number; if nothing meaningful
    # remains, clear it so we don't ship a fake identifier.
    unit_number = clean_unit_number(unit_number)

    # ── Centralised concession normalization (2026-05-24) ────────────────
    # Pre-fix: adapters emitted only the legacy ``concession`` string and
    # the cleanup (core/concession_clean + core/concession_normalize) only
    # ran at v2-output time. Cross-tier merges in _merge_fns.py preserve
    # ``concession_text``/``_value``/``_source`` (canonical) but not
    # ``concession`` (legacy) — silently dropping the offer at merge time.
    #
    # Post-fix: any text on either input (legacy ``concession=`` or
    # canonical ``concession_text=``) flows through the cleanup pipeline
    # here, so all adapters get:
    #   * ``concession`` — raw input preserved verbatim (back-compat)
    #   * ``concession_text`` — same raw text in the canonical key the
    #     merge / schema_v2 / observation report consume
    #   * ``concession_text_clean`` — de-leaked variant (JS/CSS prefix
    #     stripped); always present when source text was present
    #   * ``_concession_quality`` — classifier label (clean / partial_leak
    #     / heavy_leak / no_signal) for triage
    #   * ``concession_value`` — numeric value parsed by normalize_concession
    #     when present; preserved unchanged when caller supplied it
    #   * ``concession_source`` — caller-supplied or None
    # Caller-supplied canonical fields take precedence over derived values
    # (capture-first; never overwrite what the parser explicitly knows).
    raw_concession_text: str | None = None
    if isinstance(concession_text, str) and concession_text.strip():
        raw_concession_text = concession_text
    elif isinstance(concession, str) and concession.strip():
        raw_concession_text = concession

    cleaned_concession: str | None = None
    concession_quality: str | None = None
    derived_concession_value: float | None = None
    if raw_concession_text:
        try:
            from ma_poc.core.concession_clean import (
                classify_concession_quality,
                clean_concession_text,
            )

            cleaned_concession = clean_concession_text(raw_concession_text)
            concession_quality = classify_concession_quality(raw_concession_text)
        except Exception:
            # Cleanup is best-effort — never block unit emission.
            cleaned_concession = raw_concession_text
            concession_quality = None
        # Caller-supplied concession_value wins; only derive when absent.
        if concession_value is None:
            try:
                from ma_poc.core.concession_normalize import normalize_concession

                _obj = normalize_concession(raw_concession_text)
                if isinstance(_obj, dict):
                    # The canonical RealPage shape carries dollar value at
                    # ``obj.free.dollarsLow`` or ``obj.rr.dollarsLow``. Either
                    # surfaces as the numeric concession_value at unit level.
                    _inner = _obj.get("obj") or {}
                    if isinstance(_inner, dict):
                        for _slot in ("free", "rr"):
                            _v = (_inner.get(_slot) or {})
                            if isinstance(_v, dict) and _v.get("dollarsLow"):
                                derived_concession_value = float(_v["dollarsLow"])
                                break
            except Exception:
                pass

    final_concession_value = concession_value
    if final_concession_value is None and derived_concession_value is not None:
        final_concession_value = derived_concession_value

    # ── Offer-taxonomy extraction (2026-05-24) ───────────────────────
    # Matches the 8-column reference xlsx schema:
    #   offer_banner       (short offer-only phrase, ~20-100 chars)
    #   offer_type         (free_rent / dollar_off / waived_fee / ...)
    #   offer_target       (rent / deposit / app_fee / amenity_fee / ...)
    #   offer_value        ("6 weeks" / "$400" / "50%" / "1 month")
    #   offer_conditions   ("deadline:May 31st; unit_scope:select; ...")
    # All 5 keys always present (None when no signal).
    offer_fields: dict[str, Any] = {
        "offer_banner": None,
        "offer_type": None,
        "offer_target": None,
        "offer_value": None,
        "offer_conditions": None,
    }
    if raw_concession_text:
        try:
            from ma_poc.core.offer_extract import extract_offer

            offer_fields = extract_offer(raw_concession_text)
        except Exception:
            # Offer extraction is best-effort; never block unit emission.
            pass

    return {
        "floor_plan_name": floor_plan_name,
        "bed_label": bed_label,
        "bedrooms": bedrooms,
        "bathrooms": bathrooms,
        "sqft": sqft,
        "unit_number": unit_number,
        "floor": floor,
        "building": building,
        "rent_range": rent_range,
        "market_rent_low": rent_low,
        "market_rent_high": rent_high,
        "deposit": deposit,
        # Legacy + canonical concession fields. All populated from the
        # same source text so downstream code can read either; the merge
        # in _merge_fns.py preserves the canonical ones explicitly.
        "concession": raw_concession_text or "",
        "concession_text": raw_concession_text,
        "concession_text_clean": cleaned_concession,
        "_concession_quality": concession_quality,
        "concession_value": final_concession_value,
        "concession_source": concession_source,
        # ── Offer taxonomy (2026-05-24, matches xlsx schema) ─────────
        "offer_banner": offer_fields["offer_banner"],
        "offer_type": offer_fields["offer_type"],
        "offer_target": offer_fields["offer_target"],
        "offer_value": offer_fields["offer_value"],
        "offer_conditions": offer_fields["offer_conditions"],
        "availability_status": availability_status,
        "available_units": available_units,
        # Bug 2026-05-13: the v2 schema reader (core/schema_v2.py:242) looks
        # for ``available_date`` (short form), but every adapter has been
        # writing ``availability_date`` (long form) since the helper was
        # introduced. The reader silently returned None for ~6,900 Tier-1
        # API rows/day across RentCafe, Entrata, AvalonBay, AppFolio,
        # OneSite, and SightMap. Emit BOTH keys so a reader on either
        # convention sees the date. The reader-side fallback in
        # schema_v2.py covers the few direct-write paths in
        # ``_api_parser.py`` that bypass this helper.
        "availability_date": availability_date,
        "available_date": availability_date,
        "lease_term": lease_term,
        "move_in_date": move_in_date,
        "source_api_url": source_api_url,
        "extraction_tier": extraction_tier,
        # 2026-05-19: stable PMS-native identifiers (model_id, building_id,
        # floor_id, listing_id, etc.) for cross-run daily merge. Until now
        # the fixed kwarg set dropped them at the adapter boundary, so only
        # DERIVED ids (our hashed unit_id/floor_plan_id) survived — which
        # churn run-to-run and make day-over-day matching brittle.
        # Additive: empty {} when an adapter doesn't (yet) pass it, so no
        # behavior change; adapters populate per-PMS incrementally with
        # grounded field names (no signature churn thereafter).
        "source_ids": dict(source_ids) if source_ids else {},
        # 2026-05-23: documented data gaps. An adapter that has verified
        # (by exhausting all enrichment paths) that the OPERATOR does not
        # publish a given field stamps it here, e.g. ``data_gaps=["sqft"]``
        # + ``data_quality_flag="SQFT_NOT_PUBLISHED"``. Downstream then:
        #   - validation.schema_gate._has_area treats a documented sqft
        #     gap as "area-present" so the no_area retry trigger doesn't
        #     fire on legitimately-incomplete-but-extracted units.
        #   - reporting.verdict can distinguish "parser missed it" from
        #     "operator data gap" instead of stamping SUCCESS_PLAN_LEVEL
        #     across both. Empty list / empty string for adapters that
        #     don't (yet) flag gaps — zero behavior change.
        "data_gaps": list(data_gaps) if data_gaps else [],
        "data_quality_flag": data_quality_flag,
    }
