# Phase J4 — Layer 4 Validation & Schema

> Takes `ExtractResult` records from L3 and produces `ValidatedRecords`.
> The schema gate turns "emit then warn" into "validate then admit" —
> eliminating the 1,014 UNIT_MISSING_ID and 297 UNIT_INVALID_RENT
> warnings from the 04-17 run at their source.
>
> Reference: Jugnu architecture §4.2 L4, §3.3 (extraction → validation
> gaps), §7.1 J4 gate.

---

## Inbound handoff (from J3)

From J3 you must have:

- `ma_poc.pms.scraper.scrape()` returning a dict that includes an
  `_extract_result: ExtractResult` key.
- Adapters populate `ExtractResult.records` with unit-shaped dicts
  (not yet validated).
- Adapters populate `ExtractResult.confidence` per-tier.
- `tier_used` follows the `adapter:tier_key` namespace (Delta 3 from J3).

---

## What J4 delivers

```python
# ma_poc/validation/__init__.py
def validate(
    extract_result: ExtractResult,
    history: UnitHistory,        # from state_store
) -> ValidatedRecords: ...
```

- Schema gate per record → accepted / rejected.
- Identity fallback for records that fail natural-key identity → marked
  `inferred_id=True`, moved to accepted with a flag.
- Cross-run sanity checks (rent swing, sqft change) → flagged but
  kept unless egregious.
- If >50% of records are rejected, set `next_tier_requested=True` to
  signal L3's orchestrator to try the next tier.

---

## Module breakdown

```
ma_poc/validation/
├── __init__.py
├── contracts.py           # ValidatedRecords, RejectedRecord, FlaggedRecord
├── schema_gate.py         # Pydantic v2 validation against UnitRecord
├── identity_fallback.py   # SHA256 fallback ID when natural key missing
├── cross_run_sanity.py    # Rent swing, sqft change, unit disappearance
└── orchestrator.py        # Assembles the above into validate()
```

### Responsibilities

| Module | Owns |
|---|---|
| `schema_gate.py` | Accepting/rejecting a single record against `UnitRecord` Pydantic schema |
| `identity_fallback.py` | Computing a deterministic fallback unit_id from (floor_plan, sqft, bedrooms, bathrooms, rent_low) |
| `cross_run_sanity.py` | Comparing current record against yesterday's record from state_store |
| `orchestrator.py` | Running the three in order, tallying the result, deciding `next_tier_requested` |

---

## File creation order

1. `ma_poc/validation/__init__.py` (empty)
2. `ma_poc/validation/contracts.py` — copy from `JUGNU_CONTRACTS.md` §4
3. `ma_poc/validation/identity_fallback.py`
4. `ma_poc/validation/schema_gate.py`
5. `ma_poc/validation/cross_run_sanity.py`
6. `ma_poc/validation/orchestrator.py`
7. Populate `ma_poc/validation/__init__.py`

Tests under `tests/validation/` mirror the layout.

---

## Module specifications

### 3. `identity_fallback.py`

The single biggest source of today's validation noise
(1,014 UNIT_MISSING_ID warnings) is records with no natural unit_id.
The fallback: a deterministic SHA256 of a tuple of stable attributes.

```python
def compute_fallback_id(record: dict) -> str | None:
    """
    Compute a deterministic fallback unit_id from a unit record.
    Returns None if too few attributes are present to make a
    meaningful fingerprint.

    Hash input tuple (order matters — do not change without bumping
    a migration):
      ( normalised_floor_plan_name,
        bedrooms,
        bathrooms,
        sqft_rounded_to_10,
        rent_rounded_to_25 )

    If any of {floor_plan, bedrooms} is missing, return None — the
    fallback is not reliable enough.

    Prefix returned ID with 'inferred_' so it's distinguishable in
    reports.
    """
```

Use `hashlib.sha256` (never `hash()` — salted per process). Trim
hex digest to 12 chars; collision risk at ~200 units/property is
negligible.

**Named tests (7):**

- `test_fallback_deterministic_for_same_input`
- `test_fallback_normalises_floor_plan_whitespace_and_case`
- `test_fallback_rounds_rent_to_25` (1998 and 2002 both → same id)
- `test_fallback_rounds_sqft_to_10`
- `test_fallback_returns_none_when_floor_plan_missing`
- `test_fallback_returns_none_when_bedrooms_missing`
- `test_fallback_prefix_is_inferred`

### 4. `schema_gate.py`

Validates a record against the `UnitRecord` Pydantic v2 model. Two
paths:

1. Strict pass — record has unit_id, rent, all required fields → accept.
2. Soft pass — record is missing unit_id → call `identity_fallback`;
   if fallback returns an id, accept with `inferred_id=True`; else
   reject.

```python
@dataclass(frozen=True)
class SchemaGateResult:
    accepted: UnitRecord | None       # populated on accept
    rejection_reasons: list[str]      # populated on reject

def check(record: dict) -> SchemaGateResult: ...
```

Rejection reasons are machine-readable codes:

- `MISSING_FLOOR_PLAN`
- `MISSING_BEDROOMS`
- `INVALID_RENT_NEGATIVE`
- `INVALID_RENT_ABSURD` (>$50,000/month — catch OCR glitches)
- `INVALID_SQFT_NEGATIVE`
- `INVALID_SQFT_ABSURD` (>20,000 sqft)
- `INVALID_DATE_FORMAT`
- `IDENTITY_FALLBACK_INSUFFICIENT`

**Named tests (9):**

- `test_schema_accepts_full_valid_record`
- `test_schema_accepts_record_missing_unit_id_via_fallback`
- `test_schema_rejects_missing_floor_plan`
- `test_schema_rejects_missing_bedrooms`
- `test_schema_rejects_negative_rent`
- `test_schema_rejects_absurd_rent_50k`
- `test_schema_rejects_date_in_wrong_format`
- `test_schema_inferred_id_flagged_on_accept`
- `test_schema_reports_multiple_reasons` (record with both missing floor plan AND bad rent → both reasons in list)

### 5. `cross_run_sanity.py`

```python
@dataclass(frozen=True)
class SanityFlags:
    rent_swing_pct: float | None     # current vs last accepted
    sqft_changed: bool
    floor_plan_changed: bool
    flags: list[str]                 # human-readable

def check(
    unit: UnitRecord,
    history: UnitHistory,            # last accepted record for this unit_id
) -> SanityFlags: ...
```

Rules:
- Rent swing >50% → flag `rent_swing_>50pct`.
- Rent swing >20% → flag `rent_swing_>20pct` (warn only).
- sqft differs by >5% from last run → flag `sqft_changed`.
- floor_plan_name changed entirely → flag `floor_plan_renamed`.

Flagged records are still **accepted** — flags feed into L5 and the
per-property report, not the accept/reject decision. Only the schema
gate can reject.

**Named tests (6):**

- `test_sanity_no_history_returns_no_flags` (new unit)
- `test_sanity_rent_swing_50pct_flagged`
- `test_sanity_rent_swing_20pct_warns_not_rejects`
- `test_sanity_sqft_changed_flagged`
- `test_sanity_floor_plan_rename_flagged`
- `test_sanity_identical_to_history_no_flags`

### 6. `orchestrator.py`

```python
def validate(
    extract_result: ExtractResult,
    history: UnitHistory,
) -> ValidatedRecords:
    """Run schema gate, identity fallback, cross-run sanity on
    extract_result.records. Tally, decide next_tier_requested.

    Emits events:
      - validate.record_accepted (per accept)
      - validate.record_rejected (per reject, with reasons)
      - validate.record_flagged (per flag set)
      - validate.identity_fallback (per inferred_id used)
      - validate.next_tier_requested (once, if triggered)
    """
```

`next_tier_requested = True` iff both:
- `len(rejected) > 0`
- `len(rejected) / (len(rejected) + len(accepted)) > 0.5`

**Named tests (8):**

- `test_validate_all_accept_no_next_tier`
- `test_validate_majority_reject_requests_next_tier`
- `test_validate_exactly_half_reject_does_not_request_next_tier` (strict >0.5)
- `test_validate_flags_do_not_count_as_rejects`
- `test_validate_preserves_source_extract_reference`
- `test_validate_inferred_ids_counted` (populates `identity_fallback_used_count`)
- `test_validate_emits_events_per_record`
- `test_validate_never_raises_on_malformed_record` (feed a record missing every field)

---

## Integration with L3

In `ma_poc/pms/scraper.py`, after calling an adapter:

```python
extract_result = await adapter.extract(page, ctx)
validated = validate(extract_result, history)

if validated.next_tier_requested and has_more_tiers:
    # L3 orchestrator loops to the next tier
    continue

return validated
```

The loop lives in L3, not L4 — L4 is stateless; it just reports
whether the current extraction was good enough.

---

## Refactoring / code-quality checklist

- [ ] No file over 300 lines.
- [ ] `mypy --strict ma_poc/validation/` clean.
- [ ] `ruff check ma_poc/validation/` clean.
- [ ] Coverage ≥ 90% on `ma_poc/validation/` (it's pure logic — easy
      to test comprehensively).
- [ ] No network calls in this package.
- [ ] No Playwright imports in this package.
- [ ] `validate()` is a pure function of its inputs (no hidden state,
      no global mutation).
- [ ] `UnitHistory` is an injected parameter, not a module-level
      import from state_store (testability).

---

## Gate — `scripts/gate_jugnu.py phase 4`

Passes iff:

- All ~30 tests pass.
- Static analysis clean.
- Coverage ≥ 90%.
- **Observable check:** re-run the baseline on a small sample
  (--limit 50) and assert:
    - `UNIT_MISSING_ID` validation issues drop by ≥95% vs J0 baseline
      (identity fallback absorbs them).
    - `UNIT_INVALID_RENT` validation issues drop by ≥95% vs J0
      baseline (rejected at source, not flagged after).
    - Total records accepted is within 2% of what J0's pipeline
      would have accepted (we're not dropping real data).

---

## Outbound handoff (to J5 Observability)

- **Function** `ma_poc.validation.validate()` — stable, pure,
  deterministic.
- **Contract** `ValidatedRecords` populated with `identity_fallback_used_count`
  and `next_tier_requested`.
- **Event names** `validate.*` fixed — J5's ledger consumes these.
- **Baseline delta:** record in `docs/JUGNU_BASELINE.md`:
    - `UNIT_MISSING_ID` rate: baseline → J4-observed.
    - `UNIT_INVALID_RENT` rate: baseline → J4-observed.

Commit: `Jugnu J4: schema gate + identity fallback + cross-run sanity`.

---

*Next: `PHASE_J5_OBSERVABILITY.md`.*
