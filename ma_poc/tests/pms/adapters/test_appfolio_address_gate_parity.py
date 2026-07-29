"""2026-07-29 defect ``address-gate-mismatch`` — the gate outranked the consumer.

``_listing_address_of`` decides "is this string an address we can judge".
``_address_matches`` / ``_listing_address_is_judgeable`` decide "does this
address match". Those two must agree, or the first must be strictly LOOSER —
otherwise a string the matcher would have matched gets thrown away before the
matcher ever sees it, the row loses its address, and it becomes unjudgeable:
dropped under ``PUBLISHED_INDEX`` / ``WEAK_EVIDENCE``, waved through under
``OPERATOR_SCOPED``. Either way the answer is wrong.

The gate was ``is_street_address``, which requires a LEADING house number. The
consumer requires no such thing — it reads a ZIP from anywhere in the string
and fuzzy-matches ANY comma-segment that could be a street (``_street_candidates``
exists precisely because operators prefix the community name). So

    "OAK TERRACE APARTMENTS - 107, 42 THUNDERBIRD PARKWAY SW, LAKEWOOD, WA 98498"
    "W 1526 Bell St 324, Amarillo, TX 79106"
    "Apt 5, 1234 Oak Ave, Tulsa, OK 74119"

— all real live shapes — were rejected by the gate and matched by the matcher.

MEASURED, offline replay of run-2026-07-27-full-0d54ca7 (the run predates the
2026-07-28 field move, so each row was re-shaped the way HEAD's card parser
emits it today: ``floor_plan_name=""``, ``unit_name=<address>``; a PROXY, not a
live measurement):

  * 12,011 AppFolio SSR/VANITY rows, 254 properties
  * 408 rows across 20 properties lost their address at the gate
  * 36 of those, against their OWN property's ctx, the matcher would have
    said MATCH — our own inventory, discarded
  * one 294-row PMC roster is served identically to 7 WA properties; the
    broken gate blanked those rows, ``OPERATOR_SCOPED`` kept the unjudgeable
    ones, and all 7 properties got the SAME 31 rows — 200 of them at a ZIP
    that was not the property's. After the fix: 0 off-ZIP rows kept.
  * pid 44905 Oakpoint: 22 rows, 16 of them River Oaks in Yuba City (95991).
    The blanked addresses collapsed to one distinct value, so the filter
    no-op'd on ``single_address_in_response`` and shipped all 22. After: 5,
    all at 95608.

This file pins both directions of the contract, and the second half of the
defect: the per-call-site ``address_field``.
"""
from __future__ import annotations

import ast
import inspect

import pytest

import ma_poc.pms.adapters.appfolio as appfolio_mod
from ma_poc.pms.adapters._appfolio_websites_duda import (
    parse_appfolio_websites_listing,
)
from ma_poc.pms.adapters._parsing import (
    contains_street_address,
    is_street_address,
)
from ma_poc.pms.adapters.appfolio import (
    ScopeEvidence,
    _address_matches,
    _listing_address_is_judgeable,
    _listing_address_of,
    filter_listings_by_property_address,
)

CTX_ADDR = "1234 Oak Ave"
CTX_ZIP = "74119"

# (string, must the gate read an address out of it?, why)
_SHAPES: list[tuple[str, bool, str]] = [
    ("1234 Oak Ave, Tulsa, OK 74119", True, "plain address"),
    ("Apt 5, 1234 Oak Ave, Tulsa, OK 74119", True, "suffix before the house number"),
    ("Building C, 1234 Oak Ave", True, "building prefix, no ZIP"),
    (
        "OAK TERRACE APARTMENTS - 107, 1234 Oak Ave, TULSA, OK 74119",
        True,
        "community-name prefix (live: pid 58321)",
    ),
    ("W 1234 Oak Ave 324, Tulsa, OK 74119", True, "directional prefix (live: pid 12878)"),
    (
        "PLUM COURT, 1720 Valley Ave E, Apt B4, Sumner, WA 98390",
        True,
        "community prefix + mid-string apt (live: pid 58321)",
    ),
    ("#5 1234 Oak Ave", True, "hash-led suffix"),
    ("1234 Oak Ave", True, "bare street, no city/ZIP"),
    (
        "The Sherwood, 42 Thunderbird Parkway SW, Lakewood, WA 98498",
        True,
        "comma-prefixed community (live: pid 58321)",
    ),
    # ── must NOT be read as an address ──────────────────────────────────
    ("101", False, "bare unit number"),
    ("A1", False, "letter+digit unit label"),
    ("2B", False, "digit+letter unit label"),
    ("", False, "empty"),
    ("   ", False, "whitespace only"),
    ("1 Bedroom, 1 Bath", False, "plan descriptor"),
    ("2 Bed / 2 Bath", False, "plan descriptor"),
    ("550 Sqft Studio", False, "plan descriptor carrying a number"),
    ("The Aspen 2x2", False, "operator plan name"),
    ("AppFolio listing 8925", False, "the parser's own placeholder"),
    ("Penthouse", False, "no number at all"),
]


@pytest.mark.parametrize(
    ("text", "want", "why"), _SHAPES, ids=[s[2] + "|" + s[0][:24] for s in _SHAPES]
)
def test_gate_reads_addresses_and_only_addresses(text: str, want: bool, why: str) -> None:
    got = _listing_address_of({"floor_plan_name": "", "unit_name": text}, "floor_plan_name")
    assert bool(got) is want, f"{text!r} ({why}): gate returned {got!r}"


@pytest.mark.parametrize("text", [s[0] for s in _SHAPES])
def test_gate_is_strictly_looser_than_the_old_anchored_test(text: str) -> None:
    """Direction 1. The new predicate may accept MORE than
    ``is_street_address``; it must never accept less, or the fix would be a
    regression dressed as a fix."""
    if is_street_address(text):
        assert contains_street_address(text), (
            f"{text!r} was accepted by the old gate and is now rejected"
        )


@pytest.mark.parametrize("text", [s[0] for s in _SHAPES])
def test_gate_never_rejects_what_the_matcher_would_accept(text: str) -> None:
    """Direction 2. THE CONTRACT. If ``_address_matches`` can say True for a
    string, the gate must hand that string to it."""
    if _address_matches(text, CTX_ADDR, CTX_ZIP, 85):
        assert _listing_address_of(
            {"floor_plan_name": "", "unit_name": text}, "floor_plan_name"
        ), (
            f"the matcher accepts {text!r} but the gate blanked it — the row "
            "loses its address and becomes unjudgeable"
        )


@pytest.mark.parametrize("bare", ["101", "A1", "2B", "", "   ", "5"])
def test_a_bare_unit_number_never_becomes_judgeable(bare: str) -> None:
    """Direction 3. The other failure mode, and the reason the gate cannot
    simply be deleted: on a normal multifamily property ``unit_name`` is a
    unit number. Judging it as an address makes the row judgeable and then
    unmatchable, which DROPS REAL INVENTORY."""
    seen = _listing_address_of({"floor_plan_name": "", "unit_name": bare}, "floor_plan_name")
    assert seen == "", f"{bare!r} must not be judged as a street address"
    assert _listing_address_is_judgeable(seen) is False


# ── the live contamination scenario, end to end ──────────────────────────
# Shape and ZIPs taken from the 294-row PMC roster that run 0d54ca7 served
# identically to 7 WA properties (pids 19712 / 41738 / 58321 / …). Every row
# is community-name-prefixed, which is exactly what the old gate could not read.
_PMC_ROSTER = [
    "COLLEGE PARK - F1, 3307 COLLEGE STREET SE, LACEY, WA 98503",
    "COLLEGE PARK - A5, 3307 COLLEGE STREET SE, LACEY, WA 98503",
    "TALISMAN - 4-103, 3724 ENSIGN RD NE, OLYMPIA, WA 98506",
    "OAK TERRACE APARTMENTS - 107, 42 THUNDERBIRD PARKWAY SW, LAKEWOOD, WA 98498",
    "WILLOW SPRINGS APTS - G-302, 608 39TH AVE S.W., PUYALLUP, WA 98373",
    "PACIFIC HEIGHTS - B104, 33314 17TH LANE S., FEDERAL WAY, WA 98003",
]


def _roster_units() -> list[dict[str, object]]:
    return [{"floor_plan_name": "", "unit_name": a} for a in _PMC_ROSTER]


@pytest.mark.parametrize(
    "evidence",
    [ScopeEvidence.PUBLISHED_INDEX, ScopeEvidence.OPERATOR_SCOPED, ScopeEvidence.WEAK_EVIDENCE],
    ids=lambda e: e.value,
)
def test_account_roster_scopes_to_this_property_only(evidence: ScopeEvidence) -> None:
    """At EVERY evidence grade, a shared PMC roster must come back as College
    Park's two rows — not the whole roster, and not zero. Before the fix the
    gate blanked all six addresses, so ``PUBLISHED_INDEX`` zeroed the property
    and ``OPERATOR_SCOPED`` kept all six (none judgeable)."""
    kept, tel = filter_listings_by_property_address(
        _roster_units(),
        "3307 College St SE",
        "98503",
        evidence=evidence,
    )
    assert [u["unit_name"] for u in kept] == _PMC_ROSTER[:2], (
        f"{evidence.value}: kept {[u['unit_name'] for u in kept]} tel={tel}"
    )
    assert tel["dropped"] == 4


def test_no_kept_row_sits_at_another_zip() -> None:
    """The contamination signature the filter exists to kill: one roster, many
    properties, every property getting the same rows at foreign ZIPs."""
    per_property = {}
    for ctx_addr, ctx_zip in [
        ("3307 College St SE", "98503"),
        ("3724 Ensign Rd NE", "98506"),
        ("42 Thunderbird Pkwy SW", "98498"),
    ]:
        kept, _ = filter_listings_by_property_address(
            _roster_units(), ctx_addr, ctx_zip, evidence=ScopeEvidence.OPERATOR_SCOPED
        )
        names = [str(u["unit_name"]) for u in kept]
        assert names, f"{ctx_addr} was zeroed"
        for n in names:
            assert n.endswith(ctx_zip), f"{ctx_addr}: kept a row at another ZIP — {n!r}"
        per_property[ctx_zip] = set(names)
    assert not set.intersection(*per_property.values()), (
        f"the same row was claimed by more than one property: {per_property}"
    )


# ── second half of the defect: the per-call-site address_field ───────────


def _duda_units() -> list[dict[str, object]]:
    """Two real-shaped DUDA collection records where the operator publishes a
    plan name AND the full address."""
    values = [
        {
            "data": {
                "full_address": "1234 Oak Ave, Tulsa, OK 74119",
                "unit_template_name": "The Aspen 2x2",
                "market_rent": "1500", "bedrooms": "2", "bathrooms": "2",
                "square_feet": "980", "id": 1, "listable_uid": "u1",
                "address_address2": "#5",
            }
        },
        {
            "data": {
                "full_address": "9999 Elsewhere Blvd, Amarillo, TX 79106",
                "unit_template_name": "The Birch 1x1",
                "market_rent": "1100", "bedrooms": "1", "bathrooms": "1",
                "square_feet": "700", "id": 2, "listable_uid": "u2",
                "address_address2": "#7",
            }
        },
    ]
    units = [parse_appfolio_websites_listing(v, "https://x/collection") for v in values]
    assert all(u is not None for u in units)
    return [u for u in units if u is not None]


def test_duda_parser_puts_the_plan_name_in_floor_plan_name_and_the_address_in_unit_name() -> None:
    """The premise. If this ever flips back, the call-site assertion below is
    the thing that must be revisited — not silently."""
    units = _duda_units()
    assert units[0]["floor_plan_name"] == "The Aspen 2x2"
    assert units[0]["unit_name"] == "1234 Oak Ave, Tulsa, OK 74119"


def test_duda_roster_scopes_when_the_filter_reads_unit_name() -> None:
    """THE DUDA DEFECT. With the default ``address_field='floor_plan_name'``
    the filter judged "The Aspen 2x2" as an address, matched nothing, and
    demoted the property to ZERO units — the filter deleting the property it
    was asked to scope. Reading ``unit_name`` keeps the right row."""
    kept, tel = filter_listings_by_property_address(
        _duda_units(), CTX_ADDR, CTX_ZIP, address_field="unit_name"
    )
    assert [u["unit_name"] for u in kept] == ["1234 Oak Ave, Tulsa, OK 74119"]
    assert tel["dropped"] == 1


def _filter_call_kwargs(first_arg_name: str) -> dict[str, str]:
    """The ``address_field`` each production call site actually passes, read
    out of the AST so a future edit cannot drift away from this test."""
    tree = ast.parse(inspect.getsource(appfolio_mod))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
        if name != "filter_listings_by_property_address":
            continue
        if not node.args or not isinstance(node.args[0], ast.Name):
            continue
        if node.args[0].id != first_arg_name:
            continue
        return {
            kw.arg: ast.unparse(kw.value)
            for kw in node.keywords
            if kw.arg is not None
        }
    raise AssertionError(f"no filter call site taking {first_arg_name!r} found")


def test_duda_call_site_names_unit_name_explicitly() -> None:
    """The DUDA path must NOT ride the default. Its rows carry a real plan
    name in ``floor_plan_name``, so ``_listing_address_of`` returns the plan
    name and never reaches the gated ``unit_name`` fallback."""
    assert _filter_call_kwargs("duda_units").get("address_field") == "'unit_name'"


@pytest.mark.parametrize("call", ["ssr_units", "vanity_units"])
def test_ssr_and_vanity_call_sites_keep_the_gated_fallback(call: str) -> None:
    """…and the SSR / VANITY paths must NOT. Their card parser blanks
    ``floor_plan_name`` unconditionally, so the default is inert and every row
    routes through the gate. Naming ``unit_name`` here would bypass the shape
    test and hand the filter whatever the address regex captured, mis-capture
    included."""
    assert "address_field" not in _filter_call_kwargs(call)
