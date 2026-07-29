"""Property-identity assurance — does this roster actually belong to this property?

The contract, stated by Ankur 2026-07-28: **we should have what the property publishes.**

Often the marketing site is plan-only and the leasing portal holds the unit list — Casa del
Mar hands off to ProspectPortal, Grand at Westchase to a ResMan applicant route. Taking the
portal is correct there; it is the only place the roster exists. Following the marketing
site deeper is equally correct — Redwood's ``/floorplans/{plan}`` apartment table and Jonah
Digital's embedded ``units[]`` JSON are one hop in.

The line is **property-scoped vs account-wide**, not marketing vs portal::

    casadelmar.prospectportal.com/houston/casa-del-mar/conventional/   scoped    OK
    <slug>.securecafeapplicant.com/…/<property>/floorplans/<id>        scoped    OK
    <account>.appfolio.com/listings?property_list=emerson              filtered  OK
    <account>.appfolio.com/listings          (no property filter)      account   NOT OK

Every contaminated roster measured came from the last kind. Note this module does not test
the *route* — it tests the *result*, which is what makes it robust: a property-scoped
surface returns one property's units (one place, unique roster) and passes; an account-wide
surface returns the portfolio (dozens of cities, shared with sibling properties) and fails.

Why it exists
-------------
When the primary route returns nothing, the pipeline falls back to whatever roster-shaped
surface is reachable without asserting *whose* property it describes. Multi-community
portals (AppFolio account listings, management-company portals) answer with the entire
account. Replaying this module over run ``2026-07-27-full-0d54ca7`` (4,982 properties,
99,537 unit rows) flags **126 properties** — signal A alone 29, signal B alone 70,
overlapping on 27 (56 and 97 respectively before the overlap is removed).

Sizing, with the denominator each number is over. Task #73 re-derived all four from the
100 shard ``properties.json`` files; earlier passes quoted three of them side by side as
if they disagreed. They do not — they are different denominators over one flag set::

    12,067   every row in units[] over the 126 flagged properties
    12,063   the same, unit-level only (4 of those rows are is_floor_plan_level)
    11,938   rows carrying a positive rent (unit-level or plan-level)
    11,402   GOLD: unit-level rows with rent > 0 on properties whose winning tier is
             TIER_1_* and not a *_PLAN_LEVEL tier

11,402 is **12.0% of the run's 94,800 gold rows**, which is why what this module *does*
with a flag matters more than whether it flags. It annotates. See "Measured precision".

The rows are not fabricated. They are real apartments with real ``appfolio_listing_id``s,
real addresses, real rents — belonging to *other* properties. That is what makes this
worse than a parse failure: every field is correct and the attribution is wrong, so
nothing downstream can tell.

Three confirmations, live-verified, where the property's own site is unambiguous::

    40769 Madrona Estates   site publishes ONE plan (2x1.5, $2,100)   we held 294 rows
    41738 Cherry Tree       site publishes TWO plans                   we held 294 rows
    32716 Porto Bella       site publishes ONE plan (1bd, 625 sqft)    we held 262 rows

The two signals
---------------
``roster_is_foreign`` (signal B) — per-property, in-process, needs no other property.
    A property cannot be in two cities. Where unit identifiers parse as street addresses,
    more than ``MAX_DISTINCT_LOCATIONS`` distinct city+zip means the roster spans a
    portfolio, not a community. Porto Bella (one address in Norwalk CA) held units across
    35 cities including Anaheim, Santa Ana and Los Angeles.

``find_roster_collisions`` (signal A) — run-level, **after shard assembly**.
    Two properties cannot share a roster. Catches portals with no address to check.

    .. warning::
       This MUST run after shards are merged. All 22 collision groups observed in
       ``2026-07-27-full-0d54ca7`` span shards; **zero** are contained within one. A
       per-shard or in-process collision check catches none of them while appearing to work.

Measured precision — why the wiring ANNOTATES and does not demote
-----------------------------------------------------------------
Task #73, 2026-07-28. Two measurements, kept separate because they are different kinds of
evidence: an offline replay is a PROXY for behaviour, a live probe is an observation.

**Run-wide replay** (proxy; the run itself never called this code). Over the 97 signal-B
flags, the property's OWN city appears inside the roster it was flagged for **80 times
(82.5%)**; its own ZIP 82 times (84.5%); **43 of 97** flagged rosters lie entirely inside
the property's own state; the median share of a flagged roster's locations that are in
the property's own state is **0.94**. That is indistinguishable from a legitimate
single-metro scattered-site operator — the exact population this signal must not condemn.

**Live study** (plain unauthenticated static GET, ``curl_cffi impersonate=chrome``; no
CAPTCHA solving, no unlocker, no rotation). 8 flagged and 6 unflagged properties probed;
6 flagged and 5 unflagged could be settled from what the site publishes::

    FLAGGED, true positive
      32716 Porto Bella      livehappy.appfolio.com/listings = 300 cards / 44 city+zip
      46582 Danish Village   postroadmgmt account spans Wichita KS AND Pittsburgh/
                             Reading/Kutztown PA — cross-state; we held 110 rows
      44181 Highpointe       propertyGroup-scoped fetch returns 2 cards; we held 24
    FLAGGED, false or ambiguous
      19154 Conway Club      5 of the 9 condemned rows are 1900/1908/1910 S Conway Rd,
                             Orlando FL 32812 — the property's own street address
     241145 Fourth & Selden  midtownwestdetroit.com/single-family IS a scattered
                             single-family portfolio; all 31 rows are metro Detroit, MI
      62306 Riverwalk I      riverwalkapartments.com serves ONE site for the two CSV
                             rows "Riverwalk I" and "Riverwalk II" — signal A flags both
    UNFLAGGED, correctly
      41193 Village Park     20 cards, one city+zip; leasing office vs building numbers
     281174 1700 Forest      10 cards, two city+zip, one community
    UNFLAGGED, false negative
       1912 Chasewood        19 of 20 held rows are Lubbock TX; the property is Amarillo
     289568 Mariners Landing 6 of 7 held rows are San Diego; the property is National City
      21341 Kinstone         13 rows at 2550 Akers Mill Rd Atlanta + 11 at River Heights
                             Crossing Marietta — two buildings, one roster
    UNSETTLED (never folded into a rate)
      11399 Southcrest       no AppFolio tenant recoverable from the vacancies URL
      16076 San Lagos        mark-taylor.com deep path 404s today
     299097 The Lofts        no AppFolio tenant on the page

3 of 6 settled flags were false or ambiguous. That sample was chosen adversarially —
boundary cases and scattered-site candidates — so it is **not** an unbiased estimate of
precision over the flagged population; it is a demonstration that false positives exist
and are not rare. The symmetric finding matters as much: 3 of 5 settled *unflagged*
properties held a majority-foreign roster, because signal B is structurally blind below
``MAX_DISTINCT_LOCATIONS``. This is a coarse detector in both directions.

Two structural limits found while measuring, recorded rather than tuned:

* **The threshold is not comfortably clear of its flags.** An earlier note here claimed
  the confirmed-foreign rosters sat at 25-63 distinct locations, "nowhere near the
  boundary, so precision does not depend on this exact value". Re-derived: **12 of the 97
  signal-B flags sit at 7 or fewer distinct locations and 6 sit at exactly 4**, one above
  the threshold of 3, while unflagged near-misses sit at 3. Precision at the tail depends
  entirely on the exact value.
* **The city half of the location key is noisy.** ``_LOCATION_TAIL`` takes the words
  before the state, so "600 Stonebridge Drive - 607 Lenway Drive, Orlando, FL 32807"
  yields the city ``'lenway drive orlando'`` — a second triple for one place. Recounting
  every flag on ``(state, zip)`` alone gives 93 instead of 97; the 4 that vanish are
  7925, 296794, 44181 and 19154. Not changed: of the two that were probed live, dropping
  the city key removes one false positive (Conway Club) and creates one false negative
  (Highpointe, contaminated but inside one ZIP). No net evidence either way.

Signals deliberately NOT implemented
------------------------------------
Three candidate detectors were measured and rejected. They are recorded here so they are
not re-proposed:

* **Raw unit-id overlap** — flags 194 properties, nearly all false. Unit id ``"201"``
  occurs in 105 different properties and ``"101"`` in 81; two unrelated 300-unit buildings
  both numbered 101-350 "share" almost every id. Generic building numbering, not a shared
  roster.
* **IDF-weighted unit-id overlap** (id appearing in <=3 properties) — cuts to 72 but still
  flags large generic-numbered properties, because sister properties of one operator share
  a numbering convention. If overlap is ever needed, compute it on
  ``(unit_id, rent, area)`` triples; conventions collide, matching rents and areas do not.
* **~1 floor plan per unit** — contributes nothing. Measured row counts: confirmed-foreign
  properties run min 4 / p50 47 / p90 255 / max 296, while every C-only property sits
  between 20 and 50 rows (p50 26). **Zero** C-only properties have >=60 rows, and the
  smallest confirmed contamination is 149 rows. Below that it is pure false-positive
  surface: a large institutional property (Equity 2400 M, AvalonBay Dogpatch, Gables
  Columbus Center) legitimately shows ~1 unit per plan when 25 vacancies span 22 plan types.
* **~1 plan per unit AND zero UNAVAILABLE rows** — a textbook size-bias trap. On the 50
  largest rosters this separated perfectly: 22 of 22 account-wide properties matched, 0 of 27
  legitimate ones did. Portfolio-wide it collapses. At ``rows>=20`` it matches 191 properties,
  only 85 of which A or B already catch, adding 106 new — concentrated in
  ``API_RENTCAFE_SECURECAFE`` (28), ``API_SIGHTMAP`` (17), ``TIER_MERGED_CROSS_PAGE`` (8),
  ``DOM_GENERIC`` (6), families whose measured contamination rate is 0-1%. It also catches
  **zero** of the 13 guard-invisible ``DOM_APPFOLIO_SSR`` properties, whose median roster is
  3 rows. Any rule derived from the largest rosters must be re-measured across the whole
  distribution before it is believed.

What actually generalizes
-------------------------
Not a row-shape statistic — the vendor's **scope mechanism**. Probing the 50 largest rosters
(50 properties, 14 agents) produced a clean split:

===========================  =====  ==========================  =========================
vendor family                props  scope mechanism             verdict
===========================  =====  ==========================  =========================
Razz/Zeki "Vike SSR"+ResMan     23  SERVER_SIDE                 safe; FULL_RENT_ROLL 23/23
AppFolio (Duda/WP/iframe)       22  CLIENT_SIDE_FILTER 16       dangerous by default
Engrain SightMap                 3  URL_PATH (data.asset.id)    safe
Jonah Digital                    1  URL_PATH                    safe
===========================  =====  ==========================  =========================

Every Razz/Zeki property was FULL_RENT_ROLL — legitimately large because the site publishes
occupied units too, scoped server-side to one ResMan property. Every account-wide case was
AppFolio, across three different embeds (Duda "Vacancies v2" widget, the WordPress "Listings
for Appfolio" plugin, and the legacy ``Appfolio.Listing`` iframe) sharing one flaw: the
payload covers the whole management account and scoping happens in the browser.

So the durable rule is: **a payload scoped SERVER_SIDE or by URL_PATH may be trusted; a payload
scoped by CLIENT_SIDE_FILTER must never be accepted as one property's roster until the parser
applies that filter itself.** The signals below are the safety net for vendors not yet
fingerprinted, not the primary defence.

Where this is wired (2026-07-28, task #73)
------------------------------------------
ONE production caller, and it ANNOTATES::

    ma_poc/scripts/runners/jugnu.run_jugnu   apply_roster_identity(..., demote=False)

It sits between ``_merge_with_existing_properties`` and ``_write_properties_incremental``
— the assembled run, before the write. That position is the whole point and is pinned by
``tests/core/test_roster_identity_wiring.py``:

* **after the merge**, because signal A compares properties against each other and every
  collision group measured spans shards. Cross-shard it flags 56 properties; the identical
  code run per shard and summed flags **2**. A per-shard call is not a weaker version of
  this check, it is a check that does not work.
* **before the write**, so the evidence reaches ``properties.json``.
* it does not touch ``_meta["verdict"]``, so it cannot race or overwrite the verdict that
  ``compute_verdict`` derived from what the pipeline actually observed.

What it writes, and nothing else: ``_meta["roster_identity"]`` (signal, reason, locations,
parsed_rows, colliding_property_ids, ``action="ANNOTATED"``) and the ``roster_scope`` tag.
No row moves. No verdict changes. Downstream can filter, weight or audit on the evidence;
nothing downstream is told an apartment does not exist.

**Why not demote.** Task #73 measured it (see "Measured precision"): 82.5% of signal-B
flags contain the property's own city, and half the flagged properties I could settle
live were false or ambiguous — including one where 5 of 9 condemned rows sit at the
property's own street address. Demotion is all-or-nothing per property, so acting on this
signal at today's precision destroys real inventory to remove contamination. Prevention at
the adapter is the right lever and already exists for the family that dominates the flag
set: 97 of the 126 flags are ``TIER_1_DOM_APPFOLIO_SSR``, and the ``propertyGroup``
server-side scoping landed for that surface fixes the cause rather than the symptom. The
bar for raising this wiring to ``demote=True`` is a per-row attribution test — a vendor
property key, not a heuristic over addresses (task #63).

:func:`guard_property_record` (per record, signal B only) and the ``demote=True`` mode of
:func:`apply_roster_identity` are library capability with tests, **not wired**. Leaving
them unwired is the deliberate outcome of the measurement above, not an oversight; do not
"finish the job" by calling them without new evidence.

Provenance of this file (task #73 reconciliation, 2026-07-28)
------------------------------------------------------------
This module existed as two divergent copies that were not ancestors of each other —
blob ``87a8771`` (660 lines, on ``codex/fix-rentcafe-floorplan-id-identity``) and blob
``fc9a6ac`` (1,049 lines, on ``worktree-wf_7ddcc9ac-38a-2``) — and neither branch had
landed. Compared function by function on the AST with docstrings stripped: 6 definitions
byte-identical, 5 differing, 7 present only in the 1,049-line copy, **0 present only in
the 660-line copy**. The 1,049 copy is the merge base; the only behaviour it dropped is a
``state`` keyword on :func:`scope_rows_to_property` that neither copy ever read. The
660-line copy's tests were replayed against this module: 37 of 46 pass verbatim, 8 fail
only on that dead keyword, 1 encodes the deliberate plan-level change documented on
:func:`roster_is_foreign`. ``tests/core/test_roster_identity_no_second_copy.py`` fails if
a second implementation reappears.


This module is **pure** except for :func:`guard_property_record` and
:func:`apply_roster_identity`, which are documented above as mutating / copying
respectively. Nothing here performs I/O.

Validation
----------
Task #55: 40 properties blind-verified live (verifying agents were not told which bucket a
property was in) reported 0 false negatives across an 8-property sample drawn from the
"looks clean" population and 0 false positives across the flagged population.

**That result did not replicate and must not be quoted on its own.** The task #73 live
study above, probing the boundary of the flag set rather than its centre, settled 6 flags
and found 3 false or ambiguous, and settled 5 unflagged properties and found 3 holding a
majority-foreign roster. The two studies are consistent if #55 sampled the easy middle:
every #55 confirmation named here (Madrona, Cherry Tree, Porto Bella) sits at 48-61
distinct locations, while every #73 false positive sits at 4-15. The honest summary is
that this detector is reliable at high dispersion and unreliable near its threshold, which
is why the wiring annotates.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "ACTION_ANNOTATED",
    "ACTION_DEMOTED",
    "ACTION_KEY",
    "EVIDENCE_KEY",
    "MAX_DISTINCT_LOCATIONS",
    "MIN_PARSED_ADDRESSES",
    "MIN_ROWS_FOR_FINGERPRINT",
    "PLANS_KEY",
    "QUARANTINE_KEY",
    "QUARANTINE_PLANS_KEY",
    "SCOPE_AVAILABLE_ONLY",
    "SCOPE_FULL_ROLL",
    "SCOPE_KEY",
    "SCOPE_UNKNOWN",
    "UNVERIFIED_VERDICT",
    "DemotionReport",
    "RosterVerdict",
    "ScopingResult",
    "apply_roster_identity",
    "collision_verdicts_by_position",
    "find_roster_collisions",
    "guard_property_record",
    "parse_unit_locations",
    "plan_level_rows",
    "roster_fingerprint",
    "roster_is_foreign",
    "roster_scope",
    "scope_rows_to_property",
    "unit_level_rows",
]

#: Where demoted rows go. They leave ``units[]`` — they are not this property's inventory and
#: must never be counted as such — but they are retained so a demotion is auditable and
#: reversible. Silently deleting them would make the guard impossible to review.
QUARANTINE_KEY: Final[str] = "unverified_units"

#: The plan-level output channel on a V2 property record. ``promote_verified_unit_rows``
#: (``ma_poc/pms/scraper.py``) moves every row it cannot anchor to a native apartment id out
#: of ``units`` and into ``plan_summaries``, which the runner emits here. A contaminated
#: roster whose rows happen to lack an anchor therefore leaves ``units`` empty and lands
#: *entirely* in this list — where a units-only guard sees nothing at all. Detection and
#: demotion must cover both channels or the guard is bypassed by an accident of anchoring.
PLANS_KEY: Final[str] = "floor_plans"

#: Quarantine for demoted plan rows. Separate from :data:`QUARANTINE_KEY` so an audit can
#: still tell which channel a demoted row came from.
QUARANTINE_PLANS_KEY: Final[str] = "unverified_floor_plans"

#: ``_meta`` key holding the demotion evidence. Written by :func:`guard_property_record` at
#: the per-record output boundary and read by :func:`apply_roster_identity` at the run
#: boundary, so a record already demoted upstream still receives its verdict downstream
#: (by then its ``units`` are empty and the detector can no longer re-derive the finding).
EVIDENCE_KEY: Final[str] = "roster_identity"

#: The verdict a demoted property carries: an honest "we do not have this property's
#: roster", not a false success. Written ONLY in demoting mode — see :data:`ACTION_KEY`.
UNVERIFIED_VERDICT: Final[str] = "UNIT_ROUTE_UNVERIFIED"

#: Key inside the evidence bundle recording what the guard actually DID, so an auditor can
#: tell an annotation apart from a demotion without inferring it from the absence of rows.
ACTION_KEY: Final[str] = "action"
ACTION_ANNOTATED: Final[str] = "ANNOTATED"
ACTION_DEMOTED: Final[str] = "DEMOTED"

#: Property-level field recording WHAT the roster represents. Sites differ: some publish the
#: entire rent roll including occupied units, others only vacancies. We mirror the source
#: (see task #62) rather than normalizing, so consumers must be told which they hold —
#: otherwise occupancy cannot be computed and row counts are not comparable across properties.
SCOPE_KEY: Final[str] = "roster_scope"
SCOPE_FULL_ROLL: Final[str] = "FULL_ROLL"
SCOPE_AVAILABLE_ONLY: Final[str] = "AVAILABLE_ONLY"
SCOPE_UNKNOWN: Final[str] = "UNKNOWN"

#: A roster spanning more than this many distinct city+zip pairs describes a portfolio,
#: not one community. Three is deliberately permissive: genuine single-metro scattered-site
#: operators straddle a couple of adjacent municipalities and must NOT be demoted.
#:
#: An earlier note here said the confirmed-foreign rosters sat at 25-63 distinct locations,
#: "nowhere near the boundary, so precision does not depend on this exact value". Re-derived
#: over the full run (task #73): **12 of the 97 signal-B flags sit at <= 7 distinct
#: locations and 6 sit at exactly 4**, one step above this threshold, while unflagged
#: near-misses sit at 3. Precision at the tail depends entirely on this value, and the
#: false positives found live all came from that tail. Do not tune it on a hunch — the
#: measurement that would justify a change is per-row attribution, not a better cutoff.
MAX_DISTINCT_LOCATIONS: Final[int] = 3

#: Below this many address-shaped identifiers there is not enough signal to judge. A
#: property with two parseable addresses in two cities is noise, not evidence.
MIN_PARSED_ADDRESSES: Final[int] = 5

#: Rosters smaller than this are not fingerprinted — small rosters of common plan/rent
#: shapes collide by chance between unrelated properties.
MIN_ROWS_FOR_FINGERPRINT: Final[int] = 5

# "…-tacoma-wa-98498"  |  "…, TACOMA, WA 98498"  |  "… Tacoma WA 98498"
# Deliberately anchored on the state+zip tail: a bare city name is far too weak (plan names
# like "Chatham" and "Concord Point" are real floor plans at a Memphis property, not places).
_LOCATION_TAIL: Final[re.Pattern[str]] = re.compile(
    r"[-,\s]([a-z][a-z\- ]{2,20})[-,\s]+([a-z]{2})[-,\s]+(\d{5})(?:\b|$)",
    re.IGNORECASE,
)

#: Fields on a unit row that may carry an address. ``floor_plan_name`` is included because
#: the AppFolio scattered-site shape puts the full street address there — property 32716
#: stored '2045 South Haster Street #M-1, Anaheim, CA 92802' as a *floor plan name*.
_ADDRESS_BEARING_FIELDS: Final[tuple[str, ...]] = (
    "unit_id",
    "unit_name",
    "floor_plan_name",
)


@dataclass(frozen=True)
class RosterVerdict:
    """Why a roster was judged foreign, with the evidence that decided it.

    Attributes:
        signal: Which detector fired — ``"ADDRESS_DISPERSION"`` or ``"SHARED_ROSTER"``.
        reason: One-line human-readable explanation for the run report.
        locations: Distinct ``(city, state, zip)`` triples observed in the roster.
        parsed_rows: How many rows yielded a parseable address.
        colliding_property_ids: Other properties sharing this roster, when applicable.
    """

    signal: str
    reason: str
    locations: tuple[tuple[str, str, str], ...] = ()
    parsed_rows: int = 0
    colliding_property_ids: tuple[str, ...] = ()

    @property
    def location_count(self) -> int:
        """Number of distinct city+state+zip triples in the roster."""
        return len(self.locations)


def unit_level_rows(units: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return only the rows claiming to be apartments.

    Used by the two measures that must not be diluted by plan cards: the roster
    **fingerprint** (a plan card's ``name/rent/area/beds`` shape is generic and collides by
    chance between unrelated properties — see the rejected-signals note in the module
    docstring) and the roster **scope** tag (a plan card has no occupancy meaning).

    Address *dispersion* deliberately does NOT use this filter — see
    :func:`roster_is_foreign`.

    Args:
        units: Unit rows from a property record, or ``None``.

    Returns:
        A new list containing rows whose ``is_floor_plan_level`` is falsy.
    """
    return [u for u in (units or []) if not u.get("is_floor_plan_level")]


def plan_level_rows(units: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Return only the rows explicitly labelled as floor-plan cards.

    The complement of :func:`unit_level_rows`. Exists so a caller can sweep the plan
    channel without re-deriving the predicate.

    Args:
        units: Rows from a property record, or ``None``.

    Returns:
        A new list containing rows whose ``is_floor_plan_level`` is truthy.
    """
    return [u for u in (units or []) if u.get("is_floor_plan_level")]


def parse_unit_locations(
    units: Iterable[dict[str, Any]] | None,
) -> list[tuple[str, str, str]]:
    """Extract ``(city, state, zip)`` from any unit identifier shaped like an address.

    Only the first address-bearing field that parses is taken per row, so a single unit
    cannot inflate the dispersion count by repeating its address across three fields.

    Args:
        units: Rows to inspect — unit-level or plan-level. A plan card can carry a street
            address (the AppFolio scattered-site shape puts one in ``floor_plan_name``), so
            callers judging *location* pass both channels.

    Returns:
        One normalized ``(city, state, zip)`` triple per row that parsed; rows with no
        address-shaped identifier contribute nothing. Order follows the input.
    """
    out: list[tuple[str, str, str]] = []
    for unit in units or []:
        for field_name in _ADDRESS_BEARING_FIELDS:
            raw = unit.get(field_name)
            if not raw:
                continue
            match = _LOCATION_TAIL.search(str(raw))
            if match is None:
                continue
            city = re.sub(r"[\s\-]+", " ", match.group(1)).strip().lower()
            out.append((city, match.group(2).lower(), match.group(3)))
            break  # one location per row
    return out


def roster_is_foreign(
    units: Iterable[dict[str, Any]] | None,
    *,
    max_distinct_locations: int = MAX_DISTINCT_LOCATIONS,
    min_parsed_addresses: int = MIN_PARSED_ADDRESSES,
) -> RosterVerdict | None:
    """Judge whether a roster spans too many places to be one community.

    A property exists at one address. When its unit identifiers are street addresses
    scattered across many cities, the roster belongs to a management company's portfolio
    rather than to this property.

    Note this does **not** compare against the property's own city. That comparison was
    considered and rejected: a genuine scattered-site operator's units legitimately sit in
    neighbouring municipalities, and requiring the property's own city to appear would
    demote them. Dispersion alone is the safe test — and it is the test that needs no
    trustworthy input beyond the roster itself.

    **Plan-level rows are judged too** (changed 2026-07-28, task #61/#63 wiring). The
    fingerprint excludes them for a good reason — a plan card's shape is generic — but that
    reason does not transfer to an address. A card literally named
    ``"2045 South Haster Street #M-1, Anaheim, CA 92802"`` asserts a location as plainly as
    a unit does, and ``promote_verified_unit_rows`` routes every row it cannot anchor into
    the plan channel, so excluding plans let a whole contaminated roster walk through
    ``floor_plans[]`` untouched. Including them is monotone — more rows can only add
    locations, never remove one — so it can only widen the flag set, and replaying run
    ``2026-07-27-full-0d54ca7`` (4,982 properties, 5,427 plan-level rows) it widened it by
    **zero**: not one plan-level row in that run carried an address-shaped identifier. The
    change closes a structural hole at a measured false-positive cost of 0/4,982.

    Args:
        units: Rows for one property — unit-level, plan-level, or both.
        max_distinct_locations: Dispersion above which the roster is foreign.
        min_parsed_addresses: Minimum address-shaped rows required to judge at all.

    Returns:
        A :class:`RosterVerdict` when the roster is foreign, otherwise ``None``. ``None``
        means "no evidence of foreignness", **not** "verified clean" — a portal roster with
        ordinary unit numbers carries no address to test and is invisible to this signal.
        Signal A exists to catch that case.
    """
    locations = parse_unit_locations(units)
    if len(locations) < min_parsed_addresses:
        return None

    distinct = sorted(set(locations))
    if len(distinct) <= max_distinct_locations:
        return None

    cities = sorted({city for city, _, _ in distinct})
    return RosterVerdict(
        signal="ADDRESS_DISPERSION",
        reason=(
            f"roster spans {len(distinct)} distinct city+zip locations across "
            f"{len(cities)} cities ({', '.join(cities[:5])}"
            f"{', …' if len(cities) > 5 else ''}); a property exists in one place"
        ),
        locations=tuple(distinct),
        parsed_rows=len(locations),
    )


def roster_fingerprint(
    units: Iterable[dict[str, Any]] | None,
    *,
    min_rows: int = MIN_ROWS_FOR_FINGERPRINT,
) -> str | None:
    """Hash the roster's content, independent of unit identity.

    Identity is deliberately excluded from the hash. Synthetic ids are salted per property,
    so two properties holding the identical roster receive *different* ids for the same
    apartment — hashing identity would let exactly the case we are hunting slip through.
    Plan name, rent, area and beds are the content that must match.

    Args:
        units: Unit rows for one property. Plan-level rows are ignored automatically.
        min_rows: Rosters smaller than this return ``None`` — too few rows to distinguish
            a shared roster from two small properties that happen to look alike.

    Returns:
        A 16-character hex digest, or ``None`` when the roster is too small to fingerprint.
    """
    rows = unit_level_rows(units)
    if len(rows) < min_rows:
        return None
    payload = sorted(
        "|".join(
            (
                str(u.get("floor_plan_name")),
                str(u.get("rent_low")),
                str(u.get("area")),
                str(u.get("beds")),
            )
        )
        for u in rows
    )
    return hashlib.sha256(json.dumps(payload).encode()).hexdigest()[:16]


def find_roster_collisions(
    properties: Sequence[dict[str, Any]],
    *,
    id_key: str = "apartment_id",
    units_key: str = "units",
    min_rows: int = MIN_ROWS_FOR_FINGERPRINT,
) -> dict[str, RosterVerdict]:
    """Find properties sharing a roster with another property in the same run.

    Two properties cannot both own the same apartments. When a fingerprint repeats, **every**
    member of the group is untrustworthy — we cannot tell from the data which one, if any,
    the roster really belongs to, and in the measured cases it belonged to none of them
    (seven Washington communities shared one 294-unit scattered-site portfolio spanning 45
    cities, none of which was any of their rosters).

    .. warning::
       Call this on the **assembled** run, not per shard. Every collision group measured in
       ``2026-07-27-full-0d54ca7`` spans shards; a per-shard invocation returns an empty
       dict and looks like a clean bill of health.

    Args:
        properties: All property records in the run.
        id_key: Key holding the property identifier.
        units_key: Key holding the unit rows.
        min_rows: Passed through to :func:`roster_fingerprint`.

    Returns:
        Mapping of property id to its :class:`RosterVerdict`, for members of a collision
        group only. Properties with unique or unfingerprintable rosters are absent.

        .. warning::
           A ``dict`` keyed by id **cannot represent two records that share an id**, and
           ``str(None) == "None"`` makes that case common rather than exotic. Callers that
           act on a verdict — quarantining rows, for instance — must not look records up in
           this mapping; use :func:`collision_verdicts_by_position`, which is keyed by the
           record's position in ``properties`` and so names exactly one record. This
           function stays id-keyed because that is the shape the run report wants.
    """
    return {
        str(properties[i].get(id_key)): verdict
        for i, verdict in collision_verdicts_by_position(
            properties, id_key=id_key, units_key=units_key, min_rows=min_rows
        ).items()
    }


def collision_verdicts_by_position(
    properties: Sequence[dict[str, Any]],
    *,
    id_key: str = "apartment_id",
    units_key: str = "units",
    min_rows: int = MIN_ROWS_FOR_FINGERPRINT,
) -> dict[int, RosterVerdict]:
    """:func:`find_roster_collisions`, keyed by position instead of by property id.

    The position is the only key that identifies a record uniquely. Property ids do not:
    ``apartment_id`` is optional, and every record missing one stringifies to the same
    ``"None"``, so an id-keyed verdict silently addresses a whole class of records rather
    than the one that was judged.

    Args:
        properties: All property records in the run.
        id_key: Key holding the property identifier — read only to *name* the colliding
            properties in the verdict text, never to decide which record a verdict is for.
        units_key: Key holding the unit rows.
        min_rows: Passed through to :func:`roster_fingerprint`.

    Returns:
        Mapping of index-in-``properties`` to :class:`RosterVerdict`, for members of a
        collision group only.
    """
    by_fingerprint: dict[str, list[int]] = {}
    for index, prop in enumerate(properties):
        fingerprint = roster_fingerprint(prop.get(units_key), min_rows=min_rows)
        if fingerprint is None:
            continue
        by_fingerprint.setdefault(fingerprint, []).append(index)

    verdicts: dict[int, RosterVerdict] = {}
    for group in by_fingerprint.values():
        if len(group) < 2:
            continue
        # Sorted by id, then position, so the reason text reads in the same order it did
        # when this was a list of ids — the position only breaks ties between equal ids.
        ordered = sorted(group, key=lambda i: (str(properties[i].get(id_key)), i))
        for index in ordered:
            others = tuple(
                str(properties[other].get(id_key)) for other in ordered if other != index
            )
            verdicts[index] = RosterVerdict(
                signal="SHARED_ROSTER",
                reason=(
                    f"identical roster held by {len(others)} other propert"
                    f"{'y' if len(others) == 1 else 'ies'} "
                    f"({', '.join(others[:4])}{', …' if len(others) > 4 else ''}); "
                    f"at most one can be correct"
                ),
                colliding_property_ids=others,
            )
    return verdicts


@dataclass(frozen=True)
class ScopingResult:
    """Outcome of scoping an account-wide payload down to one property.

    Attributes:
        kept: Rows whose address places them at this property.
        dropped: Rows belonging to other properties in the same account.
        scopable: Whether the payload carried enough address information to judge at all.
            ``False`` means we could not tell — NOT that everything belongs here.
    """

    kept: tuple[dict[str, Any], ...] = ()
    dropped: tuple[dict[str, Any], ...] = ()
    scopable: bool = False

    @property
    def is_account_wide(self) -> bool:
        """True when the payload provably contained other properties' apartments."""
        return self.scopable and bool(self.dropped)


def scope_rows_to_property(
    units: Iterable[dict[str, Any]] | None,
    *,
    city: str | None,
    zip_code: str | None,
) -> ScopingResult:
    """Filter an account-wide listings payload down to one property's apartments.

    This is the **prevention** half of this module. Detection (:func:`roster_is_foreign`,
    :func:`find_roster_collisions`) tells you afterwards that a roster was wrong; this stops
    the wrong rows being accepted in the first place.

    It exists because two code paths hand us whole management accounts:

    * ``_appfolio_embed._to_appfolio_listings_root`` strips everything after ``/listings``,
      discarding any ``?property_list=…`` scope that the marketing page had applied;
    * when only a tenant slug is discovered, the canonical URL
      ``https://{tenant}.appfolio.com/listings`` is *constructed*, which is account-wide by
      definition.

    Either way the payload covers every property the company manages. AppFolio scattered-site
    listings carry the street address in the unit identifier
    (``10309-92nd-sw-07-tacoma-wa-98498``), so the address is what scopes them.

    Matching is on ``(city, zip)``. There is deliberately **no** ``state`` parameter: a
    5-digit ZIP already determines the state, so a state argument can only ever agree with
    the ZIP or contradict it, and it was accepted-and-ignored here for long enough that
    two call sites passed one believing it narrowed the match.

    **Street-level matching was tried and measured, and it is worse.** Across 245 AppFolio
    properties comparable on both methods, city+zip keeps 1,392 rows and exact street matching
    keeps only 792 — and on **86 of the 245** it keeps *zero* where city+zip kept real units.
    The cause is that ``properties.csv`` records the LEASING OFFICE address while apartments
    sit in several buildings with different street numbers and often different street names::

        41193 villageparkhe   on file 985 Grand Canyon Pkwy
                              units at 900 Evanston St, 944 Evanston St, 959 Grand Canyon Pkwy
        41738 cherrytree      on file 3422 86th St S — exact street match keeps 0 of 16

    So street scoping trades a small false-accept risk for a large false-drop one. City+zip is
    the tightest key that does not delete real inventory.

    The residual limit this leaves, stated plainly: city+zip proves a unit is in the right
    *city*, not at the right *property*. An account managing two communities in one city keeps
    both. Signal A (:func:`find_roster_collisions`) covers part of that case; the rest needs
    the vendor-side property key (task #63).

    Args:
        units: Rows parsed from the account-wide payload.
        city: The property's city, from the input record.
        zip_code: The property's ZIP; only the first five digits are compared.

    Returns:
        A :class:`ScopingResult`.

        What ``scopable=False`` obliges the caller to do **depends on what the caller knows
        about the payload**, and the two cases genuinely differ. This was previously stated
        as one unconditional rule ("emit nothing"), which contradicted the SSR caller in
        ``pms/adapters/appfolio._scope_ssr_by_city_and_zip`` and would, if anyone had
        implemented it there, have deleted the roster of every conventional AppFolio
        community in the run:

        * **The payload is KNOWN to be account-wide** — the two paths in the note above,
          where ``/listings`` was reconstructed and any property scope was thrown away. Here
          ``scopable=False`` means "we discarded the scope and cannot rebuild it". Do NOT
          emit everything; emit nothing and let the property be recorded as unreached. An
          honest gap beats another property's inventory.
        * **The payload's breadth is UNKNOWN** — the SSR path parses whatever page it was
          handed, which is account-wide for scattered-site operators and exactly one
          property for everyone else. Here ``scopable=False`` most often means the rows
          carry plan names rather than street addresses, i.e. an ordinary apartment
          community. Nothing was proven either way, so pass the payload through and let
          :func:`guard_property_record` at the output boundary judge it. Emitting nothing
          would delete real inventory on the strength of no evidence at all.

        ``scopable=False`` never means "everything belongs here". It means "this function
        could not tell", and the caller supplies the missing context.
    """
    rows = list(units or [])
    if not rows or not city or not zip_code:
        return ScopingResult(scopable=False)

    want = (
        re.sub(r"[\s\-]+", " ", str(city)).strip().lower(),
        str(zip_code).strip()[:5],
    )
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    seen_any_address = False

    for row in rows:
        located = parse_unit_locations([row])
        if not located:
            # No address on this row — cannot attribute it either way. Keep it only if the
            # payload as a whole proves scopable; decided after the loop.
            kept.append(row)
            continue
        seen_any_address = True
        row_city, _row_state, row_zip = located[0]
        if (row_city, row_zip) == want:
            kept.append(row)
        else:
            dropped.append(row)

    if not seen_any_address:
        return ScopingResult(scopable=False)

    return ScopingResult(kept=tuple(kept), dropped=tuple(dropped), scopable=True)


def roster_scope(units: Iterable[dict[str, Any]] | None) -> str:
    """Classify WHAT a roster represents, so consumers are not left to guess.

    Sites differ and we mirror them rather than normalizing (task #62): some publish the whole
    rent roll including occupied apartments, others only current vacancies. Both are correct
    data, but they are not the same thing — measured portfolio-wide, 14,207 gold rows (15.3%)
    are occupied units. Without this field, row counts are not comparable across properties
    and occupancy cannot be computed, because a property showing 12 units might have 12
    vacancies or be a 12-unit building.

    Occupied rows are kept, not dropped. They are real apartments at real published prices and
    are what occupancy inference needs; ``availability_status`` already carries the per-row
    truth (verified against the live sites). This adds the missing property-level statement.

    Args:
        units: Unit rows for one property. Plan-level rows are ignored automatically.

    Returns:
        ``SCOPE_FULL_ROLL`` when at least one row is UNAVAILABLE (the site published occupied
        units), ``SCOPE_AVAILABLE_ONLY`` when rows exist and all are available, or
        ``SCOPE_UNKNOWN`` when there are no rows or no availability information at all.

        Note a 100%-vacant property is reported ``AVAILABLE_ONLY``; the field states what we
        can assert from the data, not what the site's policy is.
    """
    rows = unit_level_rows(units)
    if not rows:
        return SCOPE_UNKNOWN
    statuses = [str(u.get("availability_status") or "").upper() for u in rows]
    if any(s == "UNAVAILABLE" for s in statuses):
        return SCOPE_FULL_ROLL
    if any(s == "AVAILABLE" for s in statuses):
        return SCOPE_AVAILABLE_ONLY
    return SCOPE_UNKNOWN


@dataclass(frozen=True)
class DemotionReport:
    """What a demotion pass did, for the run report.

    The counts exist so the effect on the headline metric is stated rather than discovered.
    Removing a foreign roster lowers the gold number, and that is a correction — the rows
    described other properties' apartments — but it must be reported as one.

    Row counts are read off the quarantine rather than off what this particular pass
    moved, so they are the same number whether the demotion happened here or upstream at
    the formatter.

    ``verdicts`` is keyed by property id and is therefore a **report**, not an index: two
    demoted records sharing an id (including the common ``apartment_id=None`` case, which
    stringifies to ``"None"``) collapse to one entry, and the last one wins. Never look a
    record's fate up in it — ``demoted_property_ids`` has one entry per demoted *record*,
    and the demotion itself is decided per record inside :func:`apply_roster_identity`.
    """

    demoted_property_ids: tuple[str, ...] = ()
    demoted_rows: int = 0
    demoted_plan_rows: int = 0
    annotated_property_ids: tuple[str, ...] = ()
    annotated_rows: int = 0
    verdicts: dict[str, RosterVerdict] = field(default_factory=dict)
    scope_counts: dict[str, int] = field(default_factory=dict)

    @property
    def demoted_properties(self) -> int:
        """Number of properties whose roster was moved out of ``units``."""
        return len(self.demoted_property_ids)

    @property
    def annotated_properties(self) -> int:
        """Number of properties flagged but left intact (``demote=False``).

        Disjoint from :attr:`demoted_property_ids` by construction: one pass runs in one
        mode. Reported separately so a run summary can never present an annotation count
        as rows removed from inventory.
        """
        return len(self.annotated_property_ids)


def _record_rows(
    record: dict[str, Any], units_key: str, plans_key: str
) -> list[dict[str, Any]]:
    """Every roster row a record publishes, across both output channels.

    ``promote_verified_unit_rows`` splits one adapter roster between ``units`` and the plan
    channel purely on whether each row had a native apartment anchor. Judging identity on
    one channel therefore judges an arbitrary subset. Both, always.
    """
    rows: list[dict[str, Any]] = []
    for key in (units_key, plans_key):
        value = record.get(key)
        if isinstance(value, list):
            rows.extend(r for r in value if isinstance(r, dict))
    return rows


def _write_evidence(
    meta: dict[str, Any], verdict: RosterVerdict, *, demote: bool = True
) -> None:
    """Stamp the finding + its evidence onto a record's ``_meta``, in place.

    In place because the Jugnu V2 formatter returns a dict whose ``_meta`` is the *same
    object* as the in-process result's (the Bug-A sharing contract pinned by
    ``tests/integration/contracts/test_verdict_meta_persistence.py``). Replacing the dict
    silently drops every verdict written after the formatter runs.

    Args:
        meta: The record's ``_meta`` dict.
        verdict: The finding.
        demote: When ``False`` (the wired production mode — see
            :func:`apply_roster_identity`) the evidence is written and **the verdict is
            left alone**. A heuristic must not overwrite a verdict that was computed from
            what the pipeline actually observed; doing so manufactures an absence out of
            an inference. The evidence records ``action=ANNOTATED`` so the distinction is
            in the data rather than inferred from the absence of a verdict change.
    """
    if demote:
        meta["verdict"] = UNVERIFIED_VERDICT
        meta["verdict_reason"] = f"{verdict.signal}: {verdict.reason}"
    meta[EVIDENCE_KEY] = {
        "signal": verdict.signal,
        "reason": verdict.reason,
        "locations": [list(loc) for loc in verdict.locations],
        "parsed_rows": verdict.parsed_rows,
        "colliding_property_ids": list(verdict.colliding_property_ids),
        ACTION_KEY: ACTION_DEMOTED if demote else ACTION_ANNOTATED,
    }


def _demote_record(
    record: dict[str, Any],
    verdict: RosterVerdict,
    *,
    units_key: str,
    plans_key: str,
    quarantine_key: str,
    quarantine_plans_key: str,
    demote: bool = True,
) -> tuple[int, int]:
    """Stamp the finding onto ``record`` and, when ``demote``, quarantine both channels.

    Mutates ``record``.

    Args:
        record: The property record.
        verdict: The finding.
        units_key: Key holding the apartment rows.
        plans_key: Key holding the plan-level rows.
        quarantine_key: Key demoted apartment rows are moved to.
        quarantine_plans_key: Key demoted plan rows are moved to.
        demote: When ``False``, **no row moves and no verdict changes** — only the
            evidence is written. This is the wired production mode; see
            :func:`apply_roster_identity` for the measurement that decided it.

    Returns:
        ``(demoted_unit_rows, demoted_plan_rows)``. Both zero when ``demote`` is ``False``
        or when there was nothing to move — a record already demoted upstream still gets
        its verdict re-stamped, which is what makes the pass idempotent across call sites.
    """
    unit_rows = [r for r in (record.get(units_key) or []) if isinstance(r, dict)]
    plan_rows = [r for r in (record.get(plans_key) or []) if isinstance(r, dict)]

    meta_only = not demote
    if meta_only:
        meta = record.get("_meta")
        if not isinstance(meta, dict):
            meta = {}
            record["_meta"] = meta
        _write_evidence(meta, verdict, demote=False)
        return 0, 0

    if unit_rows:
        record[units_key] = []
        record[quarantine_key] = list(record.get(quarantine_key) or []) + unit_rows
    if plan_rows:
        record[plans_key] = []
        record[quarantine_plans_key] = (
            list(record.get(quarantine_plans_key) or []) + plan_rows
        )

    meta = record.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        record["_meta"] = meta
    _write_evidence(meta, verdict)
    # A plan-level row moved out of ``units`` still counts as a demoted plan row, not a
    # demoted apartment — the headline "rows we stopped calling inventory" must not be
    # inflated by cards that were never claiming to be apartments.
    return len(unit_level_rows(unit_rows)), len(plan_level_rows(unit_rows)) + len(plan_rows)


def guard_property_record(
    record: dict[str, Any],
    *,
    units_key: str = "units",
    plans_key: str = PLANS_KEY,
    quarantine_key: str = QUARANTINE_KEY,
    quarantine_plans_key: str = QUARANTINE_PLANS_KEY,
    scope_key: str = SCOPE_KEY,
    demote: bool = True,
) -> RosterVerdict | None:
    """Guard **one** already-formatted property record, in place, at the output boundary.

    This is the per-property half of the guard: signal B (:func:`roster_is_foreign`) only,
    because signal A compares properties against each other and cannot run here. It is what
    a formatter calls at the moment ``AdapterResult.units`` / ``plan_summaries`` become the
    V2 property record, so a contaminated roster never leaves the formatter shaped like
    clean inventory.

    It also always writes the ``roster_scope`` tag, so every record — demoted or not —
    states what its roster represents.

    Args:
        record: A formatted V2 property record. **Mutated in place**, including ``_meta``
            (see :func:`_write_evidence` for why in place and not a copy).
        units_key: Key holding the apartment rows.
        plans_key: Key holding the plan-level rows.
        quarantine_key: Key demoted apartment rows are moved to.
        quarantine_plans_key: Key demoted plan rows are moved to.
        scope_key: Key the roster scope is written to.
        demote: When ``False``, annotate only — the evidence is written to ``_meta`` and
            no row moves and no verdict changes. See :func:`apply_roster_identity`.

    Returns:
        The :class:`RosterVerdict` when the record was flagged, otherwise ``None``.
    """
    verdict = roster_is_foreign(_record_rows(record, units_key, plans_key))
    if verdict is not None:
        _demote_record(
            record,
            verdict,
            units_key=units_key,
            plans_key=plans_key,
            quarantine_key=quarantine_key,
            quarantine_plans_key=quarantine_plans_key,
            demote=demote,
        )
    record[scope_key] = roster_scope(record.get(units_key))
    return verdict


def _evidence_verdict(record: dict[str, Any]) -> RosterVerdict | None:
    """Recover a verdict already stamped on ``_meta`` by an upstream boundary.

    Needed because a record demoted at the formatter arrives here with empty roster
    channels — the detector cannot re-derive the finding from data that is no longer there,
    so without this the run-level pass would silently un-report it.
    """
    meta = record.get("_meta")
    if not isinstance(meta, dict):
        return None
    ev = meta.get(EVIDENCE_KEY)
    if not isinstance(ev, dict) or not ev.get("signal"):
        return None
    locations: list[tuple[str, str, str]] = []
    for loc in ev.get("locations") or ():
        if isinstance(loc, list | tuple) and len(loc) == 3:
            locations.append((str(loc[0]), str(loc[1]), str(loc[2])))
    return RosterVerdict(
        signal=str(ev.get("signal")),
        reason=str(ev.get("reason") or ""),
        locations=tuple(locations),
        parsed_rows=int(ev.get("parsed_rows") or 0),
        colliding_property_ids=tuple(
            str(p) for p in ev.get("colliding_property_ids") or ()
        ),
    )


def apply_roster_identity(
    properties: Sequence[dict[str, Any]],
    *,
    id_key: str = "apartment_id",
    units_key: str = "units",
    plans_key: str = PLANS_KEY,
    quarantine_key: str = QUARANTINE_KEY,
    quarantine_plans_key: str = QUARANTINE_PLANS_KEY,
    scope_key: str = SCOPE_KEY,
    demote: bool = True,
) -> tuple[list[dict[str, Any]], DemotionReport]:
    """Judge every roster in the assembled run and tag every property's roster scope.

    Two separate things, deliberately done together because they are the two halves of
    "accurate data" for a roster:

    1. **A roster judged foreign is recorded as such.** With ``demote=True`` (the library
       default) ``units`` rows move to ``quarantine_key`` and ``floor_plans`` rows to
       ``quarantine_plans_key`` — retained so the demotion is auditable and reversible,
       never silently deleted — and the property's verdict becomes
       ``UNIT_ROUTE_UNVERIFIED``. With ``demote=False`` **nothing moves and no verdict
       changes**; only the evidence bundle is written.
    2. **Every roster is tagged** ``FULL_ROLL`` / ``AVAILABLE_ONLY`` / ``UNKNOWN``
       so occupied units are clearly labelled rather than silently mixed with vacancies.

    .. warning::
       **The wired production caller passes** ``demote=False``, and the bar for changing
       that is evidence this module does not have. Task #73 measured it on
       ``2026-07-27-full-0d54ca7`` plus a live static-GET study (see "Measured precision"
       in the module docstring): on the 97 signal-B flags the property's OWN city is
       present in the flagged roster **80 times (82.5%)**, 43 of the 97 rosters lie
       entirely inside the property's own state, and the median share of a flagged
       roster's locations that are in the property's own state is **0.94** — the exact
       profile of a legitimate single-metro scattered-site operator. Hand-checking 6
       flagged properties that could be settled live returned 3 true positives and 3
       false or ambiguous ones, including one (19154 Conway Club) where 5 of the 9
       condemned rows are at the property's own street address. Demotion is
       all-or-nothing per property, so acting on this signal deletes those rows too —
       manufacturing an absence out of an inference.

    Idempotent: a record already demoted by :func:`guard_property_record` at the formatter
    is recognised from its ``_meta`` evidence and re-stamped rather than re-detected, so
    running both boundaries neither double-counts rows nor loses the verdict.

    .. warning::
       Call this on the **assembled** run. The shared-roster signal compares properties
       against each other, and every collision group measured spans shards. Measured on
       ``2026-07-27-full-0d54ca7``: cross-shard signal A flags **56** properties, the same
       code run per shard flags **2**. A per-shard invocation is a partial safety net, not
       a clean bill of health.

    Args:
        properties: All property records in the run.
        id_key: Key holding the property identifier.
        units_key: Key holding the apartment rows.
        plans_key: Key holding the plan-level rows.
        quarantine_key: Key demoted apartment rows are moved to.
        quarantine_plans_key: Key demoted plan rows are moved to.
        scope_key: Key the roster scope is written to.
        demote: ``False`` annotates only — see the warning above. The production caller
            in ``ma_poc/scripts/runners/jugnu.run_jugnu`` passes ``False``.

    Returns:
        ``(new_properties, report)``. Inputs are not mutated — each property is
        shallow-copied, and so is its ``_meta``. Properties with no rows are passed through
        with a scope tag only. With ``demote=False`` the report's ``demoted_*`` fields stay
        empty and ``annotated_*`` carry the counts, so no summary can read an annotation
        as inventory removed.
    """
    # Keyed by POSITION, never by property id. ``apartment_id`` is optional and
    # ``str(None) == "None"``, so an id-keyed verdict map addresses every id-less record at
    # once: one foreign roster quarantined every other id-less property in the run
    # alongside it, destroying clean inventory. The id is still what the *report* is keyed
    # by — a human reads ids — but the demotion decision is per record.
    verdicts_at: dict[int, RosterVerdict] = collision_verdicts_by_position(
        properties, id_key=id_key, units_key=units_key
    )
    for index, prop in enumerate(properties):
        if index in verdicts_at:
            continue
        found = roster_is_foreign(_record_rows(prop, units_key, plans_key))
        if found is None:
            found = _evidence_verdict(prop)
        if found is not None:
            verdicts_at[index] = found

    out: list[dict[str, Any]] = []
    demoted_ids: list[str] = []
    annotated_ids: list[str] = []
    demoted_rows = 0
    demoted_plans = 0
    annotated_rows = 0
    scope_counts: dict[str, int] = {}
    verdicts: dict[str, RosterVerdict] = {}

    for index, prop in enumerate(properties):
        pid = str(prop.get(id_key))
        new = dict(prop)
        # Copy ``_meta`` too — this function promises not to mutate its inputs, and
        # ``_demote_record`` writes the verdict into ``_meta`` in place.
        if isinstance(new.get("_meta"), dict):
            new["_meta"] = dict(new["_meta"])
        verdict = verdicts_at.get(index)

        if verdict is not None:
            verdicts[pid] = verdict
            _demote_record(
                new,
                verdict,
                units_key=units_key,
                plans_key=plans_key,
                quarantine_key=quarantine_key,
                quarantine_plans_key=quarantine_plans_key,
                demote=demote,
            )
            if demote:
                demoted_ids.append(pid)
                # Counted from the quarantine, not from what this pass happened to move,
                # so the headline is the same whether the rows were demoted here or
                # upstream at the formatter. Otherwise wiring both boundaries reports zero.
                quarantined = list(new.get(quarantine_key) or [])
                demoted_rows += len(unit_level_rows(quarantined))
                demoted_plans += len(plan_level_rows(quarantined)) + len(
                    new.get(quarantine_plans_key) or []
                )
            else:
                annotated_ids.append(pid)
                annotated_rows += len(
                    unit_level_rows(_record_rows(new, units_key, plans_key))
                )

        scope = roster_scope(new.get(units_key))
        new[scope_key] = scope
        scope_counts[scope] = scope_counts.get(scope, 0) + 1
        out.append(new)

    return out, DemotionReport(
        demoted_property_ids=tuple(demoted_ids),
        demoted_rows=demoted_rows,
        demoted_plan_rows=demoted_plans,
        annotated_property_ids=tuple(annotated_ids),
        annotated_rows=annotated_rows,
        verdicts=verdicts,
        scope_counts=scope_counts,
    )
