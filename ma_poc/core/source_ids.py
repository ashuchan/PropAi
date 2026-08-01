"""Source-id provenance registry — the single authority on what a
``source_ids`` key PROVES about a row's scope.

Why this module exists
----------------------
Two independent, silently-diverging whitelists answered the question "does
this ``source_ids`` key establish a PER-UNIT identity?":

  * ``core.identity._PER_UNIT_SOURCE_ID_KEYS`` — 6 keys, mints the daily-join
    unit_id anchor.
  * ``reporting.verdict.PER_UNIT_SOURCE_ID_KEYS`` — 12 keys, decides
    unit-level vs SUCCESS_PLAN_LEVEL.

They disagreed in BOTH directions, and a scan of every key any adapter
actually writes found the disagreement was the smaller problem: 10 real
per-unit keys (``realpage_cws_unit_id``, ``entrata_uid``, ``sightmap_unit_id``,
``onsite_unit_id``, …) were in NEITHER list, while three keys in the lists
(``securecafe_id``, ``entrata_unit_id``) have NO writer
anywhere in the repo. The irony worth recording: identity carried the dead
``entrata_unit_id`` while the real Entrata key ``entrata_uid`` — 2,843 rows
across 190 properties on 2026-07-12 — was missing from it.

Design rules, in force
----------------------
1. **Keyed by bare key name, repo-wide.** Scope is genuinely adapter-dependent
   in principle (``realpage_unit_id`` is plan-scoped from ``camden.py:95`` but
   would be unit-scoped from ``_amli.py:218``), but a ``source_ids`` dict on a
   unit row carries no adapter attribution and ``tier_used`` lives on the
   result, not the row — so an ``(adapter, key)`` registry is unimplementable
   at the consumer. This registry is therefore the NAMESPACE authority: one
   key name, one scope, everywhere. The ``realpage_unit_id`` conflict costs
   nothing today because ``_amli.py`` contains zero occurrences of
   ``source_ids`` — its write never reaches a ``source_ids`` dict. The
   coverage test enforces the rule going forward: a second writer at a
   conflicting scope fails CI and that adapter must rename its key.

2. **No name-shaped admission rule.** Not "endswith ``_unit_id``", not "not
   endswith ``_floor_plan_id``". TWO plan-level keys are named ``*_unit_id``
   (``camden_unit_id``, ``realpage_unit_id``). Admission is per-key and
   evidence-backed. The naive "admit every ``*_unit_id``/``*_id``" expansion
   was measured at +5,435 units / 466 props / +4 FALSE plan→unit promotions on
   2026-07-12, of which 5,415 are ``sightmap_floor_plan_id`` sold-out
   plan-presence markers (every one ``UNAVAILABLE``, ``rent_low=None``,
   ``area=-1``). That 5,415 : 12 ratio against this design's 12 real
   promotions is the whole argument.

3. **Unmeasured ⇒ excluded.** A key that is per-unit by adapter code but has
   no cardinality measurement in a real run artifact is registered
   ``UNIT_PENDING`` and appears in NEITHER derived view. Each carries its
   promotion criterion inline.

Known gap, deliberately NOT chased here
---------------------------------------
``_funnel.py:259``, ``_air_communities.py:329`` and ``_amli.py:216-219`` emit
``property_unit_id`` / ``propertyfloorplanid`` / ``entrata_unit_id`` /
``realpage_unit_id`` as **top-level** unit keys. None of those three files
contains the string ``source_ids`` and ``core/schema_v2.py:470`` does not
promote them, so they are invisible to every consumer here (zero occurrences
in any run artifact). Wiring them in needs a rename first — ``_amli.py``'s
``realpage_unit_id`` would land at a scope conflicting with ``camden.py``'s,
which is exactly the collision rule 1 forbids. Separate PR.

IDENTITY PATH AND CANARY REQUIREMENT
------------------------------------
``scripts/runners/jugnu.py`` now copies ``source_ids`` onto the output row
before calling ``assign_fallback_unit_id``. The registry is therefore a live
identity path: an admitted native per-unit key replaces an ``inferred_*``
phenotype hash, but never replaces a real unit number. This repairs the
ordering defect that previously left native IDs unavailable to identity.

This is a correctness fix, not an automatic completeness win. Each canary
must measure re-keying by adapter, property and native-key type, verify daily
stability, and separately reconcile output against the public availability
route. Do not infer a SUCCESS_PLAN_LEVEL→SUCCESS verdict solely from this
identity change.

* VERDICT. ``reporting/verdict.compute`` is called from ``jugnu.py:1820`` with
  ``units=result.get("units")`` — PRE-format ADAPTER rows — and
  ``pms/scraper.rows_are_plan_level`` likewise runs on ``res.units``. On a
  pre-format row, ``unit_has_real_anchor`` already returns True via
  ``unit_number``; junk filtering happens later, in ``_format_v2_unit``. So
  for the rows this PR was supposed to rescue, NEW == OLD == True and
  ``rows_are_plan_level`` is False under both lists. The artifacts agree:
  properties 282594 and 34195 already carry ``_meta.verdict = SUCCESS``,
  reason "all checks passed" — they were never SUCCESS_PLAN_LEVEL and were
  never in the recovery pool.

The one measured BEHAVIOURAL delta in this PR is a small regression, recorded
honestly: property 35256's row loses ``camden_unit_id`` as verdict-layer
evidence (it is genuinely PLAN-scoped). Exactly 1 row in 2026-07-12, 1 in the
canary, 0 in plancohort, with NO property-level verdict change in any of them.
See ``core.identity._is_floorplan_surrogate``.

Evidence basis
--------------
Cardinality measured by replay over three real artifact sets:
2026-07-12 (4,982 props / 110,226 units), the 2026-07-18 canary (4,982 /
106,820) and 2026-07-26-plancohort (1,127 / 11,662). "fixtures" means the
captured real-payload fixtures under ``ma_poc/tests/**/fixtures/``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class SourceIdScope(StrEnum):
    """What a ``source_ids`` key proves about the row that carries it."""

    #: Per-apartment, unique within a property, AND stable across runs —
    #: both properties MEASURED, not inferred from the key's name. The only
    #: scope admitted to both derived views.
    UNIT_STABLE = "UNIT_STABLE"
    #: Per-apartment and unique within a property, but MEASURED to rotate its
    #: value across runs (session-, pagination- or re-issue-scoped). Safe for
    #: verdict's classify-only view, poisonous for identity's daily-join
    #: anchor: a rotating anchor mints a fresh ``unit_id`` every run, so the
    #: same apartment reads as "disappeared + new" at the daily join. Three
    #: members today — see the ROTATION MEASUREMENT block below.
    UNIT_VOLATILE = "UNIT_VOLATILE"
    #: Per-apartment by adapter code, impact NOT yet measured. Excluded from
    #: every view until someone measures it (see rule 3).
    UNIT_PENDING = "UNIT_PENDING"
    #: A verbatim copy of the value already in ``unit_number``. Carries no
    #: independent evidence — can only be non-empty when the row already had a
    #: real anchor — so admitting it would let a row that
    #: ``_is_floorplan_surrogate`` deliberately demoted back in through the
    #: side door.
    UNIT_TAUTOLOGICAL = "UNIT_TAUTOLOGICAL"
    #: Shared by every apartment on a floor plan. Positive evidence of
    #: plan-level scope — ``_is_floorplan_surrogate`` scans these.
    PLAN = "PLAN"
    #: Constant within a property (or a building within one).
    PROPERTY = "PROPERTY"
    #: Timestamps, counts, names, slugs, marketing phrases. Not an id at all.
    NOT_AN_ID = "NOT_AN_ID"
    #: Named in a historical whitelist but written by NO adapter. Registered so
    #: the coverage test can prove it is still dead rather than quietly coming
    #: alive at the wrong scope.
    DEAD = "DEAD"


# ---------------------------------------------------------------------------
# The registry. One scope per key name, repo-wide (rule 1).
# ---------------------------------------------------------------------------
#
# ── UNIT_STABLE — admitted to both derived views ───────────────────────────
#
# Group 1: measured per-unit ratio 1.000 in real RUN ARTIFACTS.
#
#   sightmap_unit_id        sightmap.py:288   (u["id"])
#       16,347 rows / 530 props @ 1.000 (07-12); 12,647 / 410 @ 1.000 (canary);
#       591 / 24 @ 1.000 (plancohort). Cross-run rotation 3/9,676 = 0.03%.
#   apts247_unit_id         _apts247.py:285      1.000, 33 props (07-12)
#                                                rotation 0/49 = 0.00%
#   spherexx_unit_id        spherexx.py:311 (u["ID"])  1.000, 2 props (canary)
#   appfolio_listable_uid   _appfolio_websites_duda.py:266   1.000, 81 props
#                                                rotation 0/19 = 0.00%
#   appfolio_id             _appfolio_websites_duda.py:267, appfolio.py:817
#                                                1.000, 81 props; rotation 0/19
#   realpage_cws_unit_id    realpage_cws.py:221 (u["id"])
#       12 rows / 12 distinct / 2 props (plancohort).
#   fortresstech_unit_id    fortresstech.py:263 (it["unitId"])  1.000;
#       rotation 0/1 — the UUID is byte-identical across 07-12 and the canary.
#
# Group 2: measured per-unit ratio 1.000 in captured real FIXTURES, with the
# adapter code confirmed to read the value off a per-unit object.
#
#   onsite_unit_id          onsite_apply.py:201 (obj["id"])
#       fixtures 11/11 and 4/4 — AND 43 rows / 2 props @ 1.000 in the
#       plancohort run, so this one is in fact artifact-measured too.
#   venterra_unit_code      venterra.py:124 (u["unit_code"])   fixtures 19/19, 20/20
#   realpage_oll_unit_id    _jetengine_repeater.py:330 (UnitId= off the
#                           PER-UNIT application URL)          fixture 4/4
#   securecafe_apartment_id reinhold.py:225,239                fixtures 7/7, 18/18
#       NB: written as the bare name ``apartment_id`` before this PR — see the
#       PROPERTY entry for ``apartment_id`` below for why it had to be renamed.
#
# ── ROTATION MEASUREMENT — why UNIT_VOLATILE is not empty ──────────────────
#
# UNIT_STABLE claims TWO properties: unique-within-property AND stable across
# runs. Only the first was ever measured. Joining 2026-07-12 against the
# 2026-07-18 canary on (property, natural non-synthetic unit_id) — 12,637
# joined rows — measures the second directly: same apartment, six days apart,
# did the source id keep its value?
#
#   key                     rotated / joined    pct
#   camden_unit_id             82 /    302    27.15%   (PLAN — see below)
#   appfolio_listing_id        44 /    303    14.52%   <- UNIT_VOLATILE
#   entrata_uid                50 /  1,985     2.52%   <- UNIT_VOLATILE
#   udr_unitid                  7 /    281     2.49%   <- UNIT_VOLATILE
#   sightmap_unit_id            3 /  9,676     0.03%      UNIT_STABLE
#   apts247_unit_id             0 /     49     0.00%      UNIT_STABLE
#   appfolio_listable_uid       0 /     19     0.00%      UNIT_STABLE
#   appfolio_id                 0 /     19     0.00%      UNIT_STABLE
#   fortresstech_unit_id        0 /      1     0.00%      UNIT_STABLE
#
# ADMISSION BAR, stated so it needs no re-litigation: >1% cross-run rotation
# disqualifies a key from UNIT_STABLE. The three keys above the bar move to
# UNIT_VOLATILE — they keep verdict's classify view (which needs uniqueness
# only) and drop out of PER_UNIT_IDENTITY_KEYS (which needs stability).
#
# This matters because ``appfolio_listing_id`` was not merely admitted, it sat
# FIRST in the anchor preference order while rotating on 14.5% of rows —
# within the same order of magnitude as the 27.2% that disqualified
# ``camden_unit_id`` and sent it to PLAN. Examples of the same apartment,
# same plan, same rent, different id: prop 19712 unit '114' 7724 -> 8094;
# prop 305576 unit 'F-103' 8143 -> 7456; prop 40769 unit '01210' 7995 -> 6188.
# Had the anchor been live (it is not — see MEASURED PRODUCTION IMPACT below),
# this would have made ~20,874 rows churn their unit_id at a 14.5% rate: the
# exact daily-join instability recorded in the 2026-07-15 investigation.
#
# HONEST GAP: six admitted keys have ZERO joined rows across 07-12/canary and
# therefore NO cross-run measurement at all — spherexx_unit_id,
# realpage_cws_unit_id, onsite_unit_id, venterra_unit_code,
# realpage_oll_unit_id, securecafe_apartment_id. They are admitted on
# cardinality + adapter-code evidence only. That is weaker than the scope name
# claims; re-measure each as soon as it appears in two runs.
#
# ── UNIT_PENDING — per-unit by code, impact UNMEASURED (rule 3) ─────────────
#
# Promotion criterion for every one of these, stated so it needs no
# re-litigation: distinct-values / rows == 1.000 WITHIN property over >=50
# units in a real run artifact, then move the entry to UNIT_STABLE in a
# one-line PR and report both the gold delta AND the recovery-pool delta.
#
# ── PLAN — 20 keys, scanned by ``_is_floorplan_surrogate`` ──────────────────
#
# ``camden_unit_id`` and ``realpage_unit_id`` are the two that matter: both are
# NAMED ``*_unit_id`` and both are PLAN-scoped, which is why rule 2 exists.
#   camden_unit_id      _camden.py:251 reads ``plan.get("realPageUnitId")`` off
#       the PLAN object, directly under the comment at _camden.py:210 —
#       "Plan-level fingerprint shared across all units of this plan". Measured
#       366 rows / 129 distinct (07-12, ratio 0.353); 379 rows @ 0.335
#       (canary); and 30% of (property, plan) pairs CHANGED value between
#       07-12 and 07-18 while the plan id held. It is a rotating plan pointer.
#   realpage_unit_id    camden.py:95 reads the same field off a
#       ``suggestedFloorPlans`` entry — also a plan.
#
# ---------------------------------------------------------------------------

SOURCE_ID_SCOPES: Final[dict[str, SourceIdScope]] = {
    # ── UNIT_STABLE (13) — unique AND cross-run stable ───────────────────
    # rently.py:120 — scattered-site (single-family / BTR) home: the street
    # ADDRESS is the permanent, unique per-home identity (#29 scattered-site
    # principle, same as AppFolio scattered listings). Without this the Rently
    # searchQuery homes classified plan-level (unit_has_real_anchor=False) and
    # shipped as collapsed floor-plans despite recover_rently winning
    # (Jodeco Landing: 5 homes -> SUCCESS_PLAN_LEVEL). Live-verified 2026-07-30.
    "rently_full_address": SourceIdScope.UNIT_STABLE,
    "sightmap_unit_id": SourceIdScope.UNIT_STABLE,
    "apts247_unit_id": SourceIdScope.UNIT_STABLE,
    "spherexx_unit_id": SourceIdScope.UNIT_STABLE,
    "appfolio_listable_uid": SourceIdScope.UNIT_STABLE,
    "appfolio_id": SourceIdScope.UNIT_STABLE,
    "realpage_cws_unit_id": SourceIdScope.UNIT_STABLE,
    "fortresstech_unit_id": SourceIdScope.UNIT_STABLE,
    "onsite_unit_id": SourceIdScope.UNIT_STABLE,
    "venterra_unit_code": SourceIdScope.UNIT_STABLE,
    "realpage_oll_unit_id": SourceIdScope.UNIT_STABLE,
    "securecafe_apartment_id": SourceIdScope.UNIT_STABLE,
    # knock.py reads the provider UUID from each public Doorway unit object.
    # Live 2026-08-01 measurement across five exact GSC properties: 440/440
    # eligible rows non-empty and distinct within property; two consecutive
    # public fetches returned identical UUID sets for every property. Visible
    # labels were far less specific (Duke Manor 169 rows / 20 labels; Estes
    # Park 61 / 17), so preserving the UUID prevents proven row collapse.
    "knock_unit_id": SourceIdScope.UNIT_STABLE,
    # ── UNIT_VOLATILE — unique, but MEASURED/ASSUMED to rotate ───────────
    # Evidence in the ROTATION MEASUREMENT block above. In verdict's
    # classify view (uniqueness is enough); NOT in identity's minting view.
    "appfolio_listing_id": SourceIdScope.UNIT_VOLATILE,  # 14.52%
    "entrata_uid": SourceIdScope.UNIT_VOLATILE,  # 2.52%
    "udr_unitid": SourceIdScope.UNIT_VOLATILE,  # 2.49%
    # rently.py:118 — per-home Rently listing id. Unique per home (classify
    # evidence), but cross-run rotation UNMEASURED, so kept out of the minting
    # view; rently_full_address (UNIT_STABLE above) is the daily-join anchor.
    "rently_id": SourceIdScope.UNIT_VOLATILE,
    # entrata.py:486 — Engrain per-unit id from the modern unitsData roster.
    # Unique per apartment (classify evidence); rotation UNMEASURED, and the
    # modern rows already anchor on unit_number, so classify-only.
    "unit_id_engrain": SourceIdScope.UNIT_VOLATILE,
    # rentmanager.py:574 — public I Love Leasing availability-row id. Exact
    # Atlantico measurements found 3/3 distinct ids within each pull, but all
    # three changed for the same visible unit labels between captures. It may
    # classify rows as units; it must not mint a cross-run identity anchor.
    "iloveleasing_unit_id": SourceIdScope.UNIT_VOLATILE,
    # _html_extract.py: ManageBuilding's complete rentals index exposes one
    # numeric public detail-record id per active listing. It is unique within
    # the observed index (Le Mirage: 10/10), but a re-listing may allocate a
    # new record, so it is classify-only and never a daily-join anchor.
    "managebuilding_listing_id": SourceIdScope.UNIT_VOLATILE,
    # ── UNIT_PENDING — in NEITHER derived view ───────────────────────────
    # appfolio.py:818. Stronger than merely unmeasured: never non-empty in
    # ANY of the three artifact sets (228,708 units), so it is currently
    # unverifiable, not just unmeasured.
    "appfolio_unit_id": SourceIdScope.UNIT_PENDING,
    "rentmanager_uid": SourceIdScope.UNIT_PENDING,  # rentmanager.py:242
    "rs365_unit_guid": SourceIdScope.UNIT_PENDING,  # residentservices365.py:349
    "rentpress_unit_code": SourceIdScope.UNIT_PENDING,  # _encoreskyline_units.py:203
    # _doorloop_listings.py: native per-listing Mongo id. Three live provider
    # accounts prove within-feed uniqueness, but cross-run stability has not
    # yet been measured; visible unit_number remains the identity anchor.
    "doorloop_listing_id": SourceIdScope.UNIT_PENDING,
    # yotta.py: public GetFloorPlans rows expose a provider-native unitId.
    # Four distinct live DBAs (55/57/58/59) prove within-property uniqueness,
    # but cross-run stability has not yet been measured.
    "yotta_unit_id": SourceIdScope.UNIT_PENDING,
    # mri_prospectconnect.py: composite of provider-native building id and
    # apartment id. Two exact live properties prove within-property uniqueness
    # (Village Park 8/8; Charter Club 1/1), but the >=50-row and cross-run
    # stability bar has not been met.
    "mri_unit_id": SourceIdScope.UNIT_PENDING,
    # _betternoi_public.py: UUID/id are read from each public API unit object.
    # Two exact live properties prove within-property uniqueness, but cross-run
    # stability is not yet measured; visible unit_number remains the anchor.
    "betternoi_unit_uuid": SourceIdScope.UNIT_PENDING,
    "betternoi_unit_id": SourceIdScope.UNIT_PENDING,
    # _leaseleads_embed.py: public ``units.data`` rows expose the integrated
    # PMS unit_id.  Current live probes across Tribeca, Lumina, and Emerson
    # Park measured 64/64 non-empty and distinct within property.  Cross-run
    # stability is not yet measured, so visible unit_number remains the
    # identity anchor and this key stays pending.
    "leaseleads_unit_id": SourceIdScope.UNIT_PENDING,
    # _nesthub_public.py: numeric id bound across the first-party SSR roster,
    # detail path, and canonical URL. It is unique within the observed exact
    # property, but cross-run/re-listing stability is not yet measured; the
    # provider-visible unit suffix remains the stable identity anchor.
    "nesthub_listing_id": SourceIdScope.UNIT_PENDING,
    # _showmojo_public.py: native 10-hex listing UID, unique across the exact
    # observed provider account. Re-listing/cross-run stability is unmeasured;
    # the full provider-published address remains the identity anchor.
    "showmojo_listing_uid": SourceIdScope.UNIT_PENDING,
    # _elise_applications_recovery.py: native apartment object id. Seven live
    # exact-property probes produced 49/49 distinct values; cross-run
    # stability is not measured, so the visible unit number remains anchor.
    "elise_applications_unit_id": SourceIdScope.UNIT_PENDING,
    # entrata.py beans-map rows carry both the ordinary Entrata unit uid and a
    # separate map-listing record id. Tuscany Hills measured 13/13 distinct
    # listing ids over three repeat pulls, but cross-run/re-listing stability
    # is unmeasured; entrata_uid/unit_number already provide identity.
    "entrata_beans_listing_id": SourceIdScope.UNIT_PENDING,
    # Funnel/Nestio's public current-listings feed exposes one listing id per
    # apartment. Three exact live properties proved within-feed uniqueness,
    # but no two-run stability measurement exists yet.
    "funnel_listing_id": SourceIdScope.UNIT_PENDING,
    # ── UNIT_TAUTOLOGICAL (2) — both currently in verdict's list; REMOVED ─
    # edificecms.py:322 writes literally ``unit_no`` — the value already in
    # unit_number. Measured 1.000 unique, but uniqueness is not the objection.
    "edifice_unit_id": SourceIdScope.UNIT_TAUTOLOGICAL,
    # thinkreside.py:452 writes literally the ``unit_number`` variable — AND is
    # not even unique: property 271195 has '312' x3 and '207'/'210'/'306'/'302'
    # x2 (measured ratio 0.86). Admitting it would mint colliding anchors.
    "thinkreside_unit": SourceIdScope.UNIT_TAUTOLOGICAL,
    # ── PLAN (21) ───────────────────────────────────────────────────────
    "sightmap_floor_plan_id": SourceIdScope.PLAN,
    "securecafe_floorplan_id": SourceIdScope.PLAN,
    "rentcafe_floorplan_id": SourceIdScope.PLAN,
    "entrata_fpid": SourceIdScope.PLAN,
    "camden_floor_plan_id": SourceIdScope.PLAN,
    "camden_unit_id": SourceIdScope.PLAN,  # see block comment above
    "realpage_unit_id": SourceIdScope.PLAN,  # see block comment above
    "realpage_floorplan_id": SourceIdScope.PLAN,
    "apts247_floor_plan_id": SourceIdScope.PLAN,
    "apts247_slug": SourceIdScope.PLAN,
    "edifice_plan_id": SourceIdScope.PLAN,
    "thinkreside_plan_slug": SourceIdScope.PLAN,
    "floor_plan_id": SourceIdScope.PLAN,  # reinhold.py:226
    "floorplan_id": SourceIdScope.PLAN,  # realpage_cws.py:223
    "floorplan_code": SourceIdScope.PLAN,  # venterra.py:126
    # onsite_apply.py:195 — ``plan = plan_name.get(sid, "")``: the style id IS
    # the plan lookup key. Measured 0.279 (plancohort).
    "onsite_style_id": SourceIdScope.PLAN,
    "spherexx_floorplan_id": SourceIdScope.PLAN,
    "betternoi_floorplan_uuid": SourceIdScope.PLAN,
    "wp_plan_slug": SourceIdScope.PLAN,
    "plan_code": SourceIdScope.PLAN,
    # WRITER-LESS IN THIS PR, on purpose. The only writer,
    # scripts/diagnostics/browser_endpoint_discovery.py:373, belongs to a
    # concurrent workstream and is UNTRACKED — `git ls-files` does not list
    # it. Registering the key here is right (it IS plan-scoped, and the
    # diagnostics file must not have to invent a scope when it lands), but the
    # coverage test's stale-entry check must NOT be satisfied by a foreign
    # session's uncommitted file: with that file excluded, `stale ==
    # ['api_floorplan_id']` and the test fails when this PR is committed
    # alone. It is therefore listed in `allowed_writerless` in
    # tests/core/test_source_id_registry_coverage.py, alongside
    # `apartment_id`. Remove it from that allowlist in the diagnostics PR that
    # introduces the writer.
    "api_floorplan_id": SourceIdScope.PLAN,
    # ── PROPERTY ────────────────────────────────────────────────────────
    "partner_property_id": SourceIdScope.PROPERTY,
    "property_id": SourceIdScope.PROPERTY,
    "realpage_oll_property_id": SourceIdScope.PROPERTY,
    "host": SourceIdScope.PROPERTY,
    "appfolio_database_name": SourceIdScope.PROPERTY,
    "operator": SourceIdScope.PROPERTY,
    "property_name": SourceIdScope.PROPERTY,
    "betternoi_client_uuid": SourceIdScope.PROPERTY,
    "seo_url": SourceIdScope.PROPERTY,
    # DoorLoop groups multiple apartment listings under this native building /
    # property id. It repeats across units and must never classify a row alone.
    "doorloop_property_id": SourceIdScope.PROPERTY,
    # Exact provider boundary ids repeated across all rows for one recovered
    # property/building. They must never establish apartment identity alone.
    "entrata_property_id": SourceIdScope.PROPERTY,
    "funnel_building_id": SourceIdScope.PROPERTY,
    "funnel_community_id": SourceIdScope.PROPERTY,
    # _showmojo_public.py: every accepted row shares the one account and RHR
    # application site proved by the official manager chain. These establish
    # provider/property scope, never per-unit identity.
    "showmojo_account": SourceIdScope.PROPERTY,
    "rhr_application_site_id": SourceIdScope.PROPERTY,
    # _appfolio_websites_duda.py:273. Per-unit for scattered-site portfolios
    # but CONSTANT across every unit of one building (3/30 measured props
    # constant, min within-property ratio 0.20). Never per-unit proof alone.
    "appfolio_full_address": SourceIdScope.PROPERTY,
    # PERMANENTLY NON-ADMISSIBLE. No writer since reinhold.py was renamed to
    # ``securecafe_apartment_id`` in this PR. The bare name collides with the
    # v2 OUTPUT's PROPERTY-level ``apartment_id`` field — every record in
    # properties.json is keyed by it — so a future adapter reusing the bare
    # name for a property id would be silently promoted to a unit anchor.
    "apartment_id": SourceIdScope.PROPERTY,
    # ── NOT_AN_ID (4) ───────────────────────────────────────────────────
    "scrape_ts": SourceIdScope.NOT_AN_ID,  # _camden.py:252
    "available_count": SourceIdScope.NOT_AN_ID,  # _mark_taylor.py:407
    # Both live on the deliberate NO_AVAILABILITY_NOW placeholder row
    # (_no_availability.py:353-354) whose unit_number is "" BY CONSTRUCTION.
    # Admitting either would mint fake unit-level rows out of the operator's
    # "no availability" marker.
    "matched_phrase": SourceIdScope.NOT_AN_ID,
    "operator_published_state": SourceIdScope.NOT_AN_ID,
    # ── DEAD (2) — named in a historical whitelist, written by nobody ────
    # Sole occurrence is reporting/verdict.py's own whitelist. The REAL
    # SecureCafe key is ``securecafe_floorplan_id``, which is PLAN-level — only
    # the docstring warning kept this entry from being the #110 bug again.
    "securecafe_id": SourceIdScope.DEAD,
    # Sole writer is _amli.py:219, which writes it as a TOP-LEVEL unit key;
    # _amli.py never touches source_ids, so this can never match.
    "entrata_unit_id": SourceIdScope.DEAD,
}


# ---------------------------------------------------------------------------
# Derived views
# ---------------------------------------------------------------------------
#
# TWO views survive, deliberately, because the requirement genuinely differs:
#   * identity MINTS the daily-join key (core/state_store.py:196,
#     data_provider/sql/stores.py:578, scripts/runners/jugnu.py:3118) — it
#     needs uniqueness AND cross-run stability.
#   * verdict CLASSIFIES unit-level vs plan-level — it needs uniqueness only.
# A UNIT_VOLATILE key is safe for verdict and poisonous for identity, so the
# two views have DIFFERENT membership (three keys) and the split is exercised
# rather than notional.
# Invariant: PER_UNIT_IDENTITY_KEYS < PER_UNIT_EVIDENCE_KEYS (violated in
# BOTH directions by the two hand-maintained lists this module replaces).

#: ORDERED — ``_source_id_anchor`` returns the FIRST match, so this tuple is
#: the anchor preference order. UNIT_STABLE only: a rotating id must never
#: become a daily-join key. ``appfolio_listable_uid`` / ``appfolio_id`` /
#: ``apts247_unit_id`` keep their historical relative order to minimise
#: gratuitous unit_id churn; ``appfolio_listing_id`` used to lead this tuple
#: and is now excluded outright (14.52% cross-run rotation).
PER_UNIT_IDENTITY_KEYS: Final[tuple[str, ...]] = (
    "appfolio_listable_uid",
    "appfolio_id",
    "apts247_unit_id",
    "sightmap_unit_id",
    "spherexx_unit_id",
    "realpage_cws_unit_id",
    "fortresstech_unit_id",
    "onsite_unit_id",
    "venterra_unit_code",
    "realpage_oll_unit_id",
    "securecafe_apartment_id",
    "knock_unit_id",
    # scattered-site (Rently single-family/BTR): the street address is the
    # permanent per-home daily-join anchor (#29). Listed last — a row carrying
    # a first-party unit id above should still prefer it.
    "rently_full_address",
)

#: UNIT_STABLE u UNIT_VOLATILE. Uniqueness-within-property is enough here.
PER_UNIT_EVIDENCE_KEYS: Final[frozenset[str]] = frozenset(
    k
    for k, scope in SOURCE_ID_SCOPES.items()
    if scope in (SourceIdScope.UNIT_STABLE, SourceIdScope.UNIT_VOLATILE)
)

#: Keys whose value is shared by every apartment on a plan. Positive evidence
#: for the plan-surrogate demotion in ``core.identity._is_floorplan_surrogate``.
PLAN_LEVEL_KEYS: Final[frozenset[str]] = frozenset(
    k for k, scope in SOURCE_ID_SCOPES.items() if scope is SourceIdScope.PLAN
)


def normalize_source_id_key(key: object) -> str:
    """Canonicalise a ``source_ids`` key for registry lookup.

    Lowercases and folds ``-`` to ``_``. Adapters write both spellings; the
    registry stores the folded form only.
    """
    return str(key).lower().replace("-", "_")


def scope_of(key: object) -> SourceIdScope | None:
    """Registered scope of *key*, or ``None`` when the key is UNREGISTERED.

    ``None`` is meaningful, not an error: it means a new adapter landed a new
    key before the registry was updated. Callers must have a defined fallback
    for that case (``_is_floorplan_surrogate`` keeps the legacy suffix
    heuristic as its backstop). The coverage test in
    ``tests/core/test_source_id_registry_coverage.py`` is what should make
    ``None`` unreachable in practice.
    """
    return SOURCE_ID_SCOPES.get(normalize_source_id_key(key))
