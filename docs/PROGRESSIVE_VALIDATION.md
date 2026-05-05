# The Dropping Problem — Validation Architecture Analysis

**Author role:** Staff Software Architect (with a senior data analyst hat for §2)
**Companion docs:** [`2026-05-05_validation_failure_RCA.md`](./2026-05-05_validation_failure_RCA.md), [`CLAUDE_VALIDATION_RECOVERY_PR1.md`](./CLAUDE_VALIDATION_RECOVERY_PR1.md)
**Status of the previous spec:** **superseded in part — see §11 for explicit revisions.**

---

## TL;DR

The previous RCA correctly identified the v1→v2 fallback alias bug as the dominant cause of 30,117 record rejections. The proposed fix recovers ~25,634 records. **It does not address a deeper structural issue: the validation layer treats identity uncertainty as data invalidity, and downstream stages discard rejected records entirely.** Even with the v1→v2 fix landed, ~2,880 records per day continue to be silently dropped — including records that carry rent, beds, baths, and sqft but happen to lack a floor-plan name. These are not corrupt records. They are real units with real data, and they should not be lost.

The right architecture treats identity confidence as a spectrum and lets records flow through identity-strength tiers, where weaker tiers contribute to floor-plan-level aggregates and become eligible for retroactive identity promotion when a stronger-identity record arrives in a future run or from a different source. This is the standard pattern in master data management (MDM) and probabilistic record linkage. The Jugnu codebase already has the bones for it — `merge_sources` with `CONFIDENCE_FLOORS`, the merge cascade with R1d–R1f tolerating null fields, the active cross-source workstream — but the validation layer cuts records off before any of that infrastructure can act.

The recommendation is a four-phase rollout: Phase 0 (the existing tactical PR, with three specific revisions), Phase 1 (tiered validation output), Phase 2 (a pending-unit staging store with cross-run reconciliation), Phase 3 (cross-source promotion). Phase 0 must still ship as planned — it is the prerequisite. Phases 1–3 are the architectural work that addresses the dropping problem properly.

---

## 1. The critique, articulated

The exact concern is that records that were *successfully scraped* — bytes fetched, page rendered, fields extracted — are being dropped at validation simply because the validator cannot compute a stable unit identity from them. Two observations follow:

First, "successfully scraped" and "publishable as a unit-level record" are not the same thing. A record can be successfully scraped and yet legitimately lack a unit identifier (because the source page is a floor-plan summary that doesn't enumerate units, or because the source emits data in a shape the extractor can only partially decode). The fact that we cannot pin a unit identity on it is a property of the source, not a defect in the data.

Second, identity is an artifact of *which sources we have looked at so far*, not a fixed attribute of the underlying physical unit. A record that today has only `floor_plan_name + sqft` may, on the next run, arrive with a natural `unit_id` from the same source — or arrive from a syndicated feed that knows the unit number. Dropping it today forecloses the possibility of identity promotion tomorrow.

The critique is therefore not "loosen the validator." It is "stop conflating identity confidence with data validity, and design for the case where identity is established progressively across runs and sources."

---

## 2. What "rejection" actually does today

A trace through the current pipeline shows that rejected records are preserved in the validation output structure but consumed by no downstream stage. They are dead in fact, even when alive in form.

`orchestrator.validate()` returns `ValidatedRecords(accepted, rejected, flagged, source_extract, ...)`. The `rejected` list holds full `RejectedRecord(raw=record, reasons=..., human_message=...)` entries — the raw record dict is still there.

But the consumers downstream of validation read only the `accepted` list. From the JUGNU_ALGORITHM Stage 8 → Stage 10 path:

- The verdict computation (`reporting/verdict.py::compute`) reads `rejected_count > accepted_count` to decide PARTIAL vs SUCCESS, but never inspects the rejected records themselves.
- The state diff stage upserts to `unit_index.json` only from `accepted`.
- The 46-key property output emit walks `accepted` to populate `units`.
- The cross-source merger (`merge_sources`, the active workstream) operates on the accepted records of multiple sources, never on the rejected ones.
- The drift detector and cross-run sanity check key off `unit_id`, which only accepted records carry.

So while a `RejectedRecord` exists in memory during the current property's processing, it is not persisted, not aggregated, not reconciled, and not eligible for any future matching. By the time the run report is written, every rejected record exists only as a count in `validate.record_rejected` events. The data is gone.

This is the dropping problem in concrete form. **The data structure preserves the records; the system architecture discards them.**

---

## 3. The conceptual confusion in `schema_gate.check()`

The validator currently performs three logically distinct checks under one function and one decision:

1. **Data validity** — is the record parseable? Are field types correct? Is rent a number, is sqft within sane bounds, does the date string parse to ISO?
2. **Identity confidence** — can we assign a stable identifier to this record so it can be tracked across runs?
3. **Publish-readiness** — should this record appear in the unit-level output that downstream consumers see?

These three concerns conflate easily because in the simplest case they coincide: a record with all required fields is data-valid AND identifiable AND publish-ready. But they decouple under realistic data conditions:

- A record with rent and sqft and beds but no floor plan and no unit number is **data-valid** (every field parses, none are absurd) but **identity-uncertain** (we can't compute a stable hash without floor plan) and arguably **not publish-ready as a unit row** but **definitely publish-ready as a floor-plan-level rent observation**.
- A record with a unit number and a floor plan but a date string of `"Spring 2026"` is **identity-strong** (unit_id present) but partially **data-invalid** (date won't parse) — and the right response is to publish it with `available_date=None` plus a placeholder flag, not to reject it outright.
- A record from a syndication feed with rent and beds and baths but no fp_name is **data-valid** and **identity-impossible-on-its-own** but **identity-eligible-by-cross-source-match** when paired with another source that has fp.

By collapsing all three concerns into a single accept/reject decision, the current `schema_gate.check()` makes choices on behalf of every downstream consumer rather than letting each consumer decide what level of identity confidence it requires. State aggregation needs strong identity, but a property-level rent distribution does not. A cross-source matcher actively *wants* to see the weakly-identified records — they are its raw material.

The architectural fix is to separate the three concerns: validate data, then *classify* identity confidence into tiers, then let downstream consumers select the tier(s) they need.

---

## 4. The industry frame: this is entity resolution, not validation

The pattern the codebase needs is documented in master data management and probabilistic record linkage literature. Three references illuminate it:

**Fellegi-Sunter probabilistic record linkage.** The 1969 paper establishing the standard framework: given two records that may or may not refer to the same entity, compute a match weight from the agreement/disagreement of their identifying fields. Records are not classified as "valid" or "invalid" — they are classified by *match probability* into linked, possible-link (review), and non-link bands. The Jugnu equivalent: the `_field_presence` map already gives us the agreement vector. We have the raw material for a probabilistic identity confidence score; we just collapse it to a binary at validation.

**Data Vault modeling (Linstedt 2002).** A data warehouse modeling pattern that explicitly separates entity identity from entity attributes. Three structures: **Hubs** (stable business keys — the unit_id when natural), **Satellites** (mutable attributes — rent, available_date, anything that changes between observations), and **Links** (relationships between hubs — unit-to-property, unit-to-floor-plan). The Jugnu translation is direct: a unit-hub keyed by natural unit_id when present and by inferred_id when not, with rent/availability satellites that can be appended even when the hub is uncertain. The pattern explicitly anticipates that hubs and satellites accumulate over time — exactly the progressive-merge case.

**Slowly Changing Dimensions Type 2 (Kimball).** The Type 2 SCD pattern preserves history by adding new rows rather than mutating existing ones, with surrogate keys that bind across changes. The relevant lesson: identity continuity is achieved through stable keys plus history rows, not through "the latest accepted record." Records that don't yet have a key get one assigned (the surrogate), then linked into the dimension when the natural key arrives.

Every one of these patterns shares a property: **records are never discarded for lack of identity. They are staged, classified, and progressively bound.** That is the right model for Jugnu.

A related industry term worth using: this is a "match candidate" — a record that cannot stand alone as a confirmed entity but is eligible to be matched. The output of validation should distinguish confirmed entities from match candidates, not merge them and not drop them.

---

## 5. The cross-run dimension

Consider a single physical apartment — call it "the unit at 123 Main, Plan A, 750 sqft, 1BR/1BA, on the 4th floor." Across runs, the records arriving for this unit may carry different identifying information depending on what the source page exposes that day:

- **Run 1.** Source emits a floor-plan summary card. The record has `floor_plan_name="A1"`, `sqft=750`, `beds=1`, `baths=1`, `rent=2400`. No unit number. Today: rejected.
- **Run 2.** Source emits the same plan card AND an "available units" sub-table with the unit listed by number. The unit-table record has `unit_id="404"`, `floor_plan_name="A1"`, `sqft=750`, `rent=2450`. Today: accepted.
- **Run 3.** Source removes the available-units sub-table (the unit got leased; the page now shows only the plan card). The record reverts to plan-card shape: `floor_plan_name="A1"`, `sqft=750`, no unit number. Today: rejected.

Under the current architecture, this unit appears in unit-level data on Run 2 and disappears on Runs 1 and 3. The continuity is broken not by anything happening to the unit, but by the source's choice of presentation. This is exactly the failure mode that Type 2 SCD was designed to prevent.

Under a progressive-merge architecture, the trajectory is different:

- **Run 1.** The plan-card record lands in a "weak identity" tier with a stable signature key `(property=123main, fp=A1, sqft=750, beds=1, baths=1)`. It is published in the floor-plan-level rent stream but not in unit-level state.
- **Run 2.** The unit-table record lands in "strong identity" with `unit_id="404"`. The system also notices a weak-identity record from Run 1 with the same signature; the strong record absorbs it. The accepted unit's history now reflects a rent change from $2,400 to $2,450.
- **Run 3.** The plan-card record lands again in weak identity. The cross-run reconciler matches it by signature against the strong record from Run 2; it inherits `unit_id="404"` (with `_identity_inherited=True` flag). The strong record's rent is updated to whatever the plan card now shows.

The unit's data is continuous across all three runs because identity is allowed to be progressively bound, not gated at the door of the validator.

This is not hypothetical for Jugnu. Per the production analysis, 2,606 records on 2026-05-05 had `floor_plan_name` only, and another 100 had `floor_plan_name + available_date`. These could be the Run 1 / Run 3 case in the example above. Without a progressive-merge architecture, every one of these is dropped on every run.

---

## 6. The cross-source dimension

The story extends to cross-source. The active `CLAUDE_XSOURCE_AND_LEARNING.md` workstream is building infrastructure for unifying records that come from different sources for the same property — the canonical case being a property where one source has the PMS API (full unit list with rent and availability) and another source has marketing-site DOM (floor plans with descriptions and photos). The cross-source merger is supposed to combine these.

But cross-source merge currently operates on validation-accepted records only. If source A's record is identity-rich and source B's record is identity-weak, source B is dropped at validation before source A can see it. The cross-source merger never gets a chance to do its job for the cohort that needs it most.

Concretely: imagine a property where the marketing-site DOM emits a beautiful floor-plan grid (gives us fp_name, sqft, but no unit numbers) and a syndication feed emits unit-level rent and available_date but with no floor plan attached (rows like `{rent: 2400, available_date: "2026-06-01", beds: 1, baths: 1, sqft: 750}`). Today both rejection paths fire: the DOM record may be retained if it has fp + sqft (post v2 migration), but the syndication record rejects with `IDENTITY_FALLBACK_INSUFFICIENT` because v2 requires fp_name. Cross-source never sees both records together.

In a progressive-merge architecture, both records flow into a tiered output. The cross-source merger sees them, recognizes that signature `(property, beds=1, baths=1, sqft=750)` matches between the DOM record and the syndication record, and can merge their fields — fp_name from DOM, rent and available_date from syndication. The result is a unit-level record that no single source could have produced alone.

This is the actual realized value of the cross-source workstream. It cannot work if validation drops the records that cross-source most needs.

---

## 7. The proposed architecture: tiered validation output

The structural change is to replace `ValidatedRecords(accepted, rejected, flagged)` with a tiered output that classifies records by identity confidence. A concrete schema:

```python
@dataclass
class ValidatedRecords:
    property_id: str

    # Tier 1 — strong identity, publish-ready as unit rows.
    # Natural unit_id present, OR v2 fallback bound with high-quality inputs.
    units_strong: list[dict[str, Any]]

    # Tier 2 — weak identity, eligible for promotion.
    # No natural unit_id, fallback could not bind (insufficient inputs),
    # but the record carries enough physical signal to be a match candidate.
    # Each carries a stable signature_key for cross-source/cross-run matching.
    units_weak: list[WeakIdentityRecord]

    # Tier 3 — observed only, contributes to property-level aggregates.
    # No identity, no useful signature. Examples: solitary rent ranges,
    # plan names without numeric attributes, "Coming Soon" placeholders
    # that lack physical signal.
    units_observed: list[dict[str, Any]]

    # Tier 4 — malformed, genuinely unparseable.
    # Type errors, absurd values, structural corruption.
    # This is the only tier where "rejection" is correct in the old sense.
    malformed: list[RejectedRecord]

    # Existing fields preserved for backward compat.
    flagged: list[FlaggedRecord]
    next_tier_requested: bool
    source_extract: Any
    identity_fallback_used_count: int


@dataclass
class WeakIdentityRecord:
    raw: dict[str, Any]
    signature_key: str           # SHA hash of (property_id, fp_name?, beds, baths, sqft_r10) — whatever we have
    signature_components: dict[str, Any]  # the field values that went into the signature
    identity_gap_reasons: list[str]  # why this is weak ("no_unit_id", "no_floor_plan_name", "sqft_only")
    field_presence: dict[str, bool]  # the existing _field_presence map, retained
```

**Classification logic** (replaces the current accept/reject in `schema_gate.check()`):

```python
def classify(record, property_id):
    # Tier 4: data validity gate. Same as today's rent/sqft/date checks for
    # absurd values, type errors, etc. Date strings that don't parse become
    # placeholders here (the F4 fix), not rejections.
    if has_unrecoverable_data_errors(record):
        return MALFORMED, reasons

    # Tier 1: natural unit_id with at least one physical signal field present.
    if has_natural_unit_id(record) and has_physical_signal(record):
        return STRONG, record

    # Tier 1 (continued): no natural id but v2 fallback can bind.
    inferred = compute_fallback_unit_id(record, property_id)
    if inferred:
        record["unit_id"] = inferred
        record["_inferred_id"] = True
        return STRONG, record

    # Tier 2: weak identity. Has SOME physical signal but not enough to bind
    # a stable unit-level identifier on its own.
    if has_any_physical_signal(record):
        sig_key, sig_components = compute_signature_key(record, property_id)
        return WEAK, WeakIdentityRecord(
            raw=record,
            signature_key=sig_key,
            signature_components=sig_components,
            identity_gap_reasons=enumerate_gaps(record),
            field_presence=field_presence(record),
        )

    # Tier 3: no useful signal at all. Property-level aggregate only.
    if has_any_meaningful_field(record):
        return OBSERVED, record

    # Tier 4: literally empty.
    return MALFORMED, ["EMPTY_RECORD"]
```

**Routing of consumers:**

- **State aggregator (unit_index, history)** reads `units_strong` only. Identity must be stable for state to be coherent. No change to the current behavior here.
- **Cross-source merger** reads `units_strong + units_weak`. Weak records are match candidates; strong records are anchors. Promotion of a weak record to strong happens at this stage when a cross-source match succeeds.
- **Floor-plan-level reports** (a new output stream) reads `units_strong + units_weak + units_observed`. Aggregates rent ranges, availability counts, and inventory by floor plan even when individual unit identities are uncertain.
- **Per-run telemetry / SLO** reads counts from all four tiers. The success-rate SLO needs revising — see §13.
- **Run report (`report.json`)** reports the four-way distribution. This is the visibility win that motivates the reclassification: today, rejected counts hide a mix of "actually invalid" and "identity-uncertain"; tomorrow, the four buckets make the distinction explicit.

The key correctness property: **no record that was successfully scraped is dropped, except when the data is genuinely malformed.** A record that lacks identity is observable; a record whose date string is `"Spring 2026"` is observable with a placeholder; a record where every field is null is malformed (and is itself worth a separate diagnostic — why did the extractor emit nothing at all?).

---

## 8. The pending-unit store

Tier 2 weak records need persistence to enable cross-run promotion. The existing `unit_index.json` is keyed by unit_id and is therefore unsuitable — weak records don't have unit_ids. A separate store keyed by signature is needed.

**Schema:**

```python
# data/pending_units.json — flat dict keyed by signature_key.
{
  "<sha16-of-signature>": {
    "property_id": "prop_123",
    "signature_components": {"fp_name": "A1", "beds": 1, "baths": 1, "sqft_r10": 750},
    "first_seen": "2026-05-01",
    "last_seen": "2026-05-05",
    "observation_count": 3,
    "field_history": [
      {"date": "2026-05-01", "rent": 2400, "available_date": null},
      {"date": "2026-05-03", "rent": 2400, "available_date": "2026-06-01"},
      {"date": "2026-05-05", "rent": 2450, "available_date": "2026-06-01"}
    ],
    "promotion_attempts": [],
    "promoted_to_unit_id": null  // set when a strong-identity record absorbs this entry
  },
  ...
}
```

**Lifecycle:**

1. **Write.** When `validate()` produces a Tier 2 record, the orchestrator (or a dedicated reconciler) writes/updates the pending entry. Append a new `field_history` row, increment `observation_count`, update `last_seen`.

2. **Read.** Whenever a Tier 1 (strong) record is processed, look up the signature in `pending_units.json`. If a match exists with `promoted_to_unit_id == null`:
   - Merge the pending entry's `field_history` into the strong record's history (rent trajectory, availability changes).
   - Set `promoted_to_unit_id` on the pending entry, link it to the strong unit's id.
   - The strong record now has a richer history than the source provided — the data we collected during the weak phase is recovered.

3. **TTL and garbage collection.** Pending entries with `last_seen` older than 90 days and no successful promotion can be archived to `pending_units_archive/`. This bounds the store size and prevents unbounded accumulation. The archive remains queryable but is not loaded into memory by default.

4. **Idempotence.** All writes are signature-keyed and re-writable. Re-running validation on the same input produces the same final state. Concurrent writers use the same `asyncio.Lock` pattern as `change_detection.StateStore`.

The store is a small addition: ~150 LoC for the storage primitive, ~80 LoC for the reconciler, plus tests. It is the smallest piece of new infrastructure that makes the progressive-merge architecture real.

---

## 9. Why we cannot simply "accept everything"

A naive reading of this analysis might suggest removing the validator entirely and accepting any record that came out of extraction. That would be wrong, for three reasons.

First, **data validity is real.** Records with `rent=-500` or `sqft=999999` or `available_date=42` are corruption indicators, not match candidates. Carrying them forward into state would propagate the corruption. The Tier 4 (malformed) bucket exists precisely to quarantine these — they should not pollute aggregates, and they should generate operational signals (probable upstream parser bug). Removing the gate entirely loses this.

Second, **identity confidence is informationally distinct from data confidence.** A perfectly clean record (rent=$2400, sqft=750, beds=1, baths=1) with no fp_name and no unit_id is data-valid but identity-uncertain. Treating it identically to a record that is both data-valid AND identity-strong (natural unit_id) would corrupt downstream consumers that depend on the distinction. State aggregation in particular cannot tolerate phantom identities.

Third, **the failure modes need to be observable.** Today's `IDENTITY_FALLBACK_INSUFFICIENT` event count, despite the underlying bug it exposed, was the diagnostic that surfaced the v1 alias issue. Removing the gate removes the signal. The right response is to keep the signal but redirect the data to a tier where it can still be useful.

The accept-everything approach is the equivalent of removing the test suite because tests are slowing you down. The progressive-merge approach is the equivalent of keeping the tests but moving the records that fail strict checks into a quarantine where they can be reviewed and recovered. The tests still fire; the data isn't lost.

---

## 10. Backward compatibility

The most consequential change is the `ValidatedRecords` schema. Direct consumers of `validated.accepted` need to be updated to read `validated.units_strong` (which has the same semantics for the strong-identity case). Two strategies for the cutover, in order of preference:

**Strategy A — dual-emit period (recommended).** For one release cycle, `ValidatedRecords` carries both the new four-tier fields AND the legacy `accepted/rejected` fields. The legacy `accepted` is populated from `units_strong`. The legacy `rejected` is populated from `malformed + units_weak` (with weak records exposed as rejected for backward consumers, with a `_was_weak=True` flag). Existing consumers keep working. New consumers (cross-source merger, pending-unit reconciler) read the new fields. After one cycle, the legacy fields are removed.

**Strategy B — single-cycle migration.** Introduce the new schema, update all known consumers in the same PR. Higher risk if any consumer is missed.

Strategy A is the right choice for Jugnu given the size of the codebase and the active workstream count.

The state file format also changes. `unit_index.json` is unaffected (still keyed by unit_id, only contains strong records). The new `pending_units.json` is additive — its absence means an empty pending store, which is the safe default. No state migration is required for existing units.

The rejected-events format changes — `validate.record_rejected` now fires only for the malformed tier. New events `validate.unit_weak_identity` and `validate.unit_observed` fire for the new tiers. Dashboards that key off the rejected count need updating; the success-rate SLO definition needs updating (§13).

---

## 11. Implications for `CLAUDE_VALIDATION_RECOVERY_PR1.md`

The previous spec is correct as far as it goes, but it cements two assumptions that the progressive-merge architecture undoes. Specific revisions:

**Revise H4.** The current invariant says "schema_gate.check() rejects a record carrying only floor_plan_name with IDENTITY_FALLBACK_INSUFFICIENT." Under progressive merge, this record routes to `units_weak` with a signature key. Replace H4 with:

> H4 — A record carrying only floor_plan_name (no other identifying fields) is classified as `units_weak`, not rejected. The record's `signature_key` is computed and a `validate.unit_weak_identity` event is emitted. It is eligible for cross-run promotion.

This is a Phase 1 invariant. For Phase 0 (the current PR), H4 stays as-is — the v1→v2 migration is structurally orthogonal to the tiering change. Mark H4 as *"Phase 0 only; replaced by Phase 1 H4-bis"* in the spec.

**Revise the §5 yield estimate.** The 2,606 + 174 + 100 = 2,880 records currently described as "still rejected (correctly)" should be reframed:

> Phase 0 (this PR) recovers 25,634 records via the v1→v2 migration. Phase 1 (tiered output) further recovers 2,880 records by routing them to `units_weak` instead of dropping. Phase 2 (cross-run reconciliation) progressively upgrades a subset of these to `units_strong` as future runs provide stronger identity. Phase 3 (cross-source promotion) does the same for syndication and DOM cross-source pairs.

The Phase 1 recoveries are not unit-level outputs — they are floor-plan-level aggregates with cross-run promotion eligibility. This distinction matters for SLO definition.

**Revise §3 anti-scope creep.** Add an explicit anti-creep entry:

> Don't try to land tiered validation output in this PR. The four-tier `ValidatedRecords` schema, the pending-unit store, and the cross-run reconciler are Phase 1 work, defined in `CLAUDE_PROGRESSIVE_VALIDATION_PHASE1.md`. This PR is Phase 0 — it must ship cleanly to enable Phase 1, but it does not anticipate Phase 1's schema. Adding stub fields for Phase 1 to ValidatedRecords now would couple the two PRs and slow Phase 0 deployment.

**Revise §9 rollout.** Add to the post-merge measurement list:

> After Phase 0 deploys and stabilises (one production cycle), measure the residual `IDENTITY_FALLBACK_INSUFFICIENT` event count. The expected residual is ~2,600–3,200 events per run, dominated by the `floor_plan_name` only and `baths + beds + rent + sqft` (no fp) signatures. This residual is the signal that Phase 1 work is needed; do not attempt to drive it to zero by tightening the v2 fallback inputs further. The right tool for the residual is Phase 1's tiered output, not Phase 0's v2 migration.

**Add F9 to the spec.** A small instrumentation addition to make Phase 1 easier to design:

> F9 — Emit `validate.identity_gap` events on Phase 0 rejections. When a record rejects with `IDENTITY_FALLBACK_INSUFFICIENT`, additionally emit a structured event with the field-presence map AND a tentative signature key (computed using whatever identifying fields ARE present). This event has no consumer in Phase 0; it exists to build a one-week dataset that informs Phase 1's signature-key design before Phase 1 starts.

F9 is ~15 LoC and adds a single test. It should land in the same PR as F1.

The four revisions above can be applied as a small follow-up commit on the same PR, or as inline edits to the existing spec file. Either is fine; the key is that they ship together.

---

## 12. Phased rollout

The full progressive-merge architecture is a multi-PR effort. Realistic phasing:

**Phase 0 — the current spec, with the §11 revisions applied.**
- v1 → v2 fallback migration (F1)
- Diagnostic event on `is_junk_unit_number` (F3)
- Date placeholder pass-through (F4)
- State migration script for v1→v2 inferred IDs (F8)
- F9 — `validate.identity_gap` events (new — see §11)

Estimated 0.75 days. Ships immediately. Recovers ~26,750 records, lifts run success rate from 84.3% to ~94%. **Prerequisite for everything else.**

**Phase 1 — tiered validation output.**
- New `ValidatedRecords` schema with `units_strong / units_weak / units_observed / malformed` tiers
- New `WeakIdentityRecord` dataclass with signature key
- `schema_gate.classify()` replaces `schema_gate.check()` (with backward-compat shim during the dual-emit period per §10 Strategy A)
- Update all consumers of `validated.accepted` to read `validated.units_strong`
- New events: `validate.unit_weak_identity`, `validate.unit_observed`
- Floor-plan-level aggregate report (`floor_plan_inventory.json`) reads weak + observed
- Test coverage for tier classification

Estimated 2–3 days. Recovers 2,880 records into observable tiers. No cross-run reconciliation yet — weak records are produced but not promoted.

**Phase 2 — pending-unit store + cross-run reconciliation.**
- `data/pending_units.json` storage primitive
- Reconciler that runs on every property after `classify()` produces weak records
- Strong records absorb matching pending entries (signature-key lookup)
- TTL and archival
- Test coverage including multi-run scenarios

Estimated 3–4 days. Begins recovering historical context for the units that flipped between weak and strong tiers across runs.

**Phase 3 — cross-source promotion.**
- `merge_sources` consumes `units_weak` from each source alongside `units_strong`
- Signature-key cross-matching across sources
- Field-confidence merging (already partially implemented via `CONFIDENCE_FLOORS` in PR #25 work) extended to cross-tier merges
- Per-property cross-source provenance ledger

Estimated 4–5 days. Realises the cross-source value for the 174-row signature (`baths + beds + rent + sqft` from syndication, paired with fp from marketing-site DOM) and similar.

**Total roadmap: ~12 engineering days across four PRs.** Phase 0 is the urgent fix. Phases 1–3 are sequenced by dependency and can ship over 2–3 weeks.

---

## 13. Yield estimate, revised

| Phase | Records recovered | Surface |
|---|---|---|
| Phase 0 (v1→v2 migration) | 25,634 | Promoted to strong identity, enter state |
| Phase 1 (weak tier) | 2,880 | Observable as floor-plan aggregates, eligible for promotion |
| Phase 2 (cross-run reconciliation) | varies (estimated 30–60% of Phase 1's weak records over 30 days) | Promoted to strong over time |
| Phase 3 (cross-source promotion) | varies (depends on cross-source coverage; ~5–15% of weak records once syndication paths exist) | Promoted to strong via cross-source field union |

**Run-level success rate trajectory:**
- Today: 84.3%
- After Phase 0: ~94% (existing unit-level publish definition)
- After Phase 1: ~94% (no change — weak records aren't unit-level publishes)

The success-rate SLO definition itself needs revision. The current SLO conflates "extracted enough unit-level rows" with "the property is healthy." A better definition has two SLOs:

- **`unit_publish_rate`** — fraction of properties that produced at least one `units_strong` row. This is the Phase 0 metric.
- **`property_observation_rate`** — fraction of properties that produced at least one row in any tier (strong, weak, or observed). This is the Phase 1 metric.

A property that produces only weak rows is a partial-data property — observable but not unit-publish-ready. Counting it as a failure under today's SLO is a category error; it conflates "we couldn't get enough data" with "we got data but couldn't pin a unit identity to it." The two are different operational signals and need different alarms.

After Phase 1, the dashboard should show both metrics. The current single-metric SLO drives the wrong remediation behaviour: it incentivises tightening identity gates to reject more records (improving the apparent rate) rather than producing useful weak/observed data.

---

## 14. Risks specific to progressive merge

**Pending-unit store growth.** Without TTL and archival, the store would grow without bound. The 90-day TTL is a starting point; the actual TTL should be calibrated against the cross-run promotion rate measured after Phase 2 deploys. If 60% of weak records are promoted within 30 days, a 60-day TTL is plenty; if only 20% are promoted within 90 days, the TTL might need to be longer with a smaller monthly archival sweep.

**Phantom unit identities.** A signature-key match is probabilistic, not certain. Two physically distinct units that happen to share `(property, fp, beds, baths, sqft_r10)` will collide in the pending store. The merge-cascade R1d–R1f rules already handle within-property collision conservatively (fail closed, append new record); the cross-run reconciler should follow the same convention. When a strong record's signature matches *multiple* pending entries, do not promote any of them — flag the ambiguity for a separate review path.

**Reverse-promotion risk.** If a previously-strong record loses its natural unit_id on a future run (source page redesign, scraper regression), the system must not de-promote it. Strong identity is monotonic — once assigned, it persists in state, even if subsequent records arrive without natural keys. The cross-run reconciler must check for strong-record continuity before considering a signature-keyed match.

**Cross-source contamination.** When source A's strong record absorbs source B's weak record, the merged result inherits provenance from both. Field-level provenance (already partially implemented in the PR #25 work) needs to extend to the tier dimension: a rent value coming from a weak record should be tagged differently from a rent value coming from a strong record. Downstream consumers may want to filter by provenance tier. This is not a new requirement — it is the existing `CONFIDENCE_FLOORS` infrastructure extended to cover cross-tier merges.

**Operational complexity of four tiers.** Engineers debugging a property's results now need to inspect four lists instead of two. The mitigation is good tooling: the run report should surface the tier distribution per property, with quick-pivot links from a property's verdict to each tier's contents. The architectural value of the four-tier model exceeds the operational cost, but the cost is real and tooling investment is required.

---

## 15. What the user critique gets exactly right

Reframed in the architectural terms above: the system today commits a category error. It treats identity uncertainty as data invalidity and discards records that are perfectly fine data but happen to lack pinnable identity. This is wrong because:

- It loses information that is observably valuable (rent ranges, availability counts, plan-level inventory).
- It forecloses cross-run identity promotion that the data warehousing literature has known how to handle since the 1990s.
- It starves the cross-source merger — Jugnu's own active workstream — of its raw material.
- It produces operational SLO signals that don't distinguish "site is broken" from "site emits non-unit-level data."

The fix is structural: separate data validity from identity confidence, classify identity into tiers, retain everything that is data-valid, and let downstream consumers select the tier they need. Industry has a name for this — entity resolution with master data management — and the patterns are mature. Jugnu has the bones for it (the merge cascade, the confidence floors, the cross-source workstream); it just needs the validator to stop being a gate and start being a classifier.

The previous spec ships first because it is a prerequisite. The phased work in §12 makes the architecture right.

---

## Appendix — how the four-tier model maps to today's signature breakdown

| Signature | Count | Today | Phase 0 (v1→v2) | Phase 1 (tiered) |
|---|---|---|---|---|
| `fp_name + sqft` only | 16,547 | rejected | strong (v2 binds) | strong |
| `baths + beds + fp_name + rent + sqft` | 3,895 | rejected | strong (v2 binds) | strong |
| `baths + beds + fp_name + sqft` | 3,389 | rejected | strong (v2 binds) | strong |
| `fp_name` only | 2,606 | rejected | rejected | **weak** (signature-key on fp + property) |
| `available_date + baths + beds + fp_name + rent` | 917 | rejected | strong (v2 binds) | strong |
| `fp_name + sqft + unit_id` | 788 | rejected (junk filter?) | rejected | strong (after Group C diagnostic) |
| `available_date + baths + beds + fp_name + rent + sqft` | 521 | rejected | strong (v2 binds) | strong |
| `baths + beds + fp_name` | 365 | rejected | strong (v2 binds) | strong |
| `baths + beds + rent + sqft` | 174 | rejected | rejected (no fp) | **weak** (signature on physical signal, eligible for cross-source fp match) |
| `<none-present>` | 114 | rejected | rejected | malformed (correctly) |
| `available_date + baths + beds + fp_name + rent + unit_id` | 111 | rejected (junk?) | rejected | strong (after Group C diagnostic) |
| `available_date + fp_name` | 100 | rejected | rejected | **weak** (signature on fp + property) |

**Phase 0 promotes 25,634 records to strong. Phase 1 retains an additional 2,880 records as weak or observed (no longer dropped). Phases 2–3 progressively promote a subset of the weak records to strong as more data accumulates.**

End of analysis.