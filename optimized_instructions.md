# CLAUDE_XSOURCE_AND_[LEARNING.md](http://LEARNING.md)

**Mission:** Implement cross-source field completion and close the LLM self-learning loop in the Jugnu pipeline. After this plan ships, every extracted unit carries field-level provenance; the cascade collects all available sources for a property, joins them by identity, and merges per-field by max confidence; the LLM acts as a one-time teacher for missing fields and saved hints get replayed deterministically on subsequent runs.

**Audience:** Claude Code, executing phase by phase against the `ashuchan/PropAi` repo. Every phase is self-contained — do not advance to phase N+1 until phase N's gate is green.

**Version:** 1.0  
**Depends on:** PR #21 merged (`compute_fallback_unit_id` available); `ENABLE_TIER_ESCALATION` feature flag respected; `services/profile_[store.py](http://store.py)::ProfileStore` available.  
**Estimated total scope:** 12 phases, ~2,000 LoC + tests, 4–6 weeks at one phase per 2–3 days.

---

## 0. Hard invariants (must hold across every phase)

These are non-negotiable. Every phase's gate script must verify the invariants it can check.

| # | Invariant | Verifiable how |
|---|---|---|
| H1 | **Merger is a pure function.** Given the same input source list, `merge_sources()` returns the same output. No I/O, no mutation of inputs. | `test_merger_pure_function` — call twice, assert identical output, assert input untouched |
| H2 | **Merge happens at most once per page.** No source can be triggered by another source's output. The planner collects all sources, then runs the merger, then evaluates. | `test_planner_emits_one_escalation_per_run` — mock a low-completeness scenario, assert exactly ONE Decision returned |
| H3 | **At most one LLM-tier call per page; at most one monolithic LLM call per property per run.** Hard cap, not soft. | `test_llm_budget_caps` — exhaust budget, assert subsequent escalations decline |
| H4 | **Detection is locked at first successful capture.** Confirmation-checks emit warnings but never re-route within a single property scrape. | `test_detection_locks_after_capture` |
| H5 | **Link-hop has bounded recursion: `max_hops=3`, `visited_urls` dedupe, calls `scrape()` not `scrape_jugnu()`.** | `test_link_hop_visited_dedupe`, `test_link_hop_max_depth` |
| H6 | **Every emitted unit dict still passes `make_unit_dict()` shape contract** when serialized to legacy output. Provenance lives in `_provenance` key only. | snapshot tests against `data/runs/*/raw_api/` payloads |
| H7 | **No adapter mutates `profile`.** All profile writes go through `services/profile_[updater.py](http://updater.py)`. | static scan: `grep -nE "profile\.[a-z_]+\s*=" ma_poc/pms/adapters/` returns zero hits |
| H8 | **Source IDs are a closed enum.** No string literals; only `SourceId` constants. | static scan: `grep -nE '"(api_floorplan\|api_unit\|json_ld\|...)"' ma_poc/services/` returns zero hits outside the enum module |
| H9 | **Stripping `_-prefixed` fields happens AFTER `state.upsert_units` AND AFTER provenance is recorded.** PR #21 invariant; do not regress. | named test `test_strip_after_upsert` |
| H10 | **Persistence to profile is best-effort. A profile-write failure never crashes a scrape.** | `test_profile_write_failure_swallowed` |

---

## 1. Phase prerequisites and ordering DAG

Phases must run in this order. Skipping a prereq breaks downstream gates.

```
Phase 1 (writers) ──┬─→ Phase 2 (primitive) ──→ Phase 3 (merger) ──→ Phase 4 (decision map)
                    │                                                        │
                    │                                                        ▼
                    │                                                  Phase 5 (intra-page)
                    │                                                        │
                    └─→ Phase 6 (eviction) ────┬───────────────────────────┘
                                                │
                                                ├──→ Phase 7  (field_patches)
                                                ├──→ Phase 8  (DOM hints)
                                                └──→ Phase 9  (cross-page) ──→ Phase 11 (preferences)
                                                                   │
                                                                   ▼
                                          Phase 10 (mapping self-validation + cost cap)
                                                                   │
                                                                   ▼
                                          Phase 12 (cluster bootstrap) ──→ Phase 13 (SLOs)
```

Phases 7, 8, 9 can be parallelized once Phase 6 ships, but each has its own gate.

---

## 2. File-path map

### New files
| Path | Purpose |
|---|---|
| `ma_poc/models/[source.py](http://source.py)` | `SourceId` enum, `FieldValue`, `ExtractedSource`, `ProvenancedUnit` types |
| `ma_poc/services/source_[merger.py](http://merger.py)` | Pure merger: `merge_sources(sources) -> list[ProvenancedUnit]` |
| `ma_poc/services/source_[planner.py](http://planner.py)` | Decision map; `plan_next_action(merged, profile, budget) -> Decision` |
| `ma_poc/services/source_[observer.py](http://observer.py)` | Records `SourceObservation` deltas to profile after every merge |
| `ma_poc/observability/source_[telemetry.py](http://telemetry.py)` | SLO computations + dashboard data |
| `scripts/gate_[xsource.py](http://xsource.py)` | Per-phase gate runner (mirrors `gate_[refactor.py](http://refactor.py)`) |
| `scripts/migrate_profiles_[xsource.py](http://xsource.py)` | One-shot migration to add new profile sections |
| `tests/services/test_source_[merger.py](http://merger.py)` | Pure-function tests for the merger |
| `tests/services/test_source_[planner.py](http://planner.py)` | Decision map tests |
| `tests/integration/test_cross_source_[e2e.py](http://e2e.py)` | The user-scenario acceptance test |
| `tests/integration/test_loop_[safeguards.py](http://safeguards.py)` | H1-H5 invariant tests |
| `tests/profile/test_field_[patches.py](http://patches.py)` | Channel-4 patch persistence + replay |
| `tests/profile/test_dom_hints_[wiring.py](http://wiring.py)` | Channel-2 hint replay + drift |
| `tests/profile/test_mapping_[eviction.py](http://eviction.py)` | Stale-mapping eviction |

### Modified files
| Path | Reason |
|---|---|
| `ma_poc/models/scrape_[profile.py](http://profile.py)` | Add `field_source_preferences`, `field_patches`, `cold_run_count`; extend `LlmFieldMapping` with `consecutive_replay_failures`, `last_replayed_at`, `source_envelope_hash`, `quality_score` |
| `ma_poc/services/profile_[updater.py](http://updater.py)` | Wire `success_count` increment on replay; populate `stats.*`; log silent early-returns; update SourceObservations |
| `ma_poc/services/llm_[extractor.py](http://extractor.py)` | Strip `$.` JSONPath prefix in `apply_saved_mapping`; harden mapping_dict assembly when LLM omits `json_paths`; add `_normalize_units` quality flag |
| `ma_poc/scripts/jugnu_[runner.py](http://runner.py)::_run_null_field_recovery` | Bind `source_api_url` from the response that recovery operated on; emit `field_patches` records |
| `ma_poc/pms/adapters/[generic.py](http://generic.py)` | Restructure cascade — collect into sources rather than return-on-hit; sub-tier 0 becomes contributor |
| `ma_poc/pms/adapters/_html_[extract.py](http://extract.py)` | Accept `hints` parameter; honor profile selectors |
| `ma_poc/pms/[scraper.py](http://scraper.py)` | Replace destructive overwrite at lines 1130-1146 with merger call; thread `visited_urls` through `_try_link_hop` |
| `ma_poc/pms/[scraper.py](http://scraper.py)::_try_link_hop` | Accumulate ALL sub-page sources; do not first-hit-wins |
| `ma_poc/observability/[events.py](http://events.py)` | New event kinds (see Phase 13) |

---

## 3. Locked definitions (do not modify after Phase 2 ships)

### 3.1 `SourceId` enum

```python
# ma_poc/models/[source.py](http://source.py)
from enum import StrEnum

class SourceId(StrEnum):
    # Deterministic API sources (per PMS detection)
    API_RENTCAFE_FLOORPLANS    = "api_rentcafe_floorplans"
    API_RENTCAFE_UNITS         = "api_rentcafe_units"
    API_ENTRATA_WIDGET         = "api_entrata_widget"
    API_APPFOLIO_LISTINGS      = "api_appfolio_listings"
    API_SIGHTMAP               = "api_sightmap"
    API_ONESITE                = "api_onesite"
    API_AVALONBAY              = "api_avalonbay"
    API_GENERIC_NARROW         = "api_generic_narrow"     # [generic.py](http://generic.py) sub-tier 1
    API_GENERIC_BROAD          = "api_generic_broad"      # [generic.py](http://generic.py) sub-tier 2

    # Deterministic page-based sources
    JSON_LD                    = "json_ld"                # generic sub-tier 3
    EMBEDDED_JSON              = "embedded_json"          # generic sub-tier 4
    DOM_CASCADE                = "dom_cascade"            # generic sub-tier 5 (no profile hints)
    DOM_PROFILE_HINTS          = "dom_profile_hints"      # Phase 8 — uses dom_hints.field_selectors

    # Self-learning replay sources (zero LLM cost)
    MAPPING_REPLAY             = "mapping_replay"         # apply_saved_mapping
    FIELD_PATCH                = "field_patch"            # Phase 7 — recovery patches
    CLUSTER_MAPPING_REPLAY     = "cluster_mapping_replay" # Phase 12 — cluster-mate mapping

    # LLM-tier sources (cost-bearing)
    LLM_API_TARGETED           = "llm_api_targeted"       # generic sub-tier 6a
    LLM_DOM_TARGETED           = "llm_dom_targeted"       # generic sub-tier 6b
    LLM_MONOLITHIC             = "llm_monolithic"         # generic sub-tier 6c
    LLM_FIELD_RECOVERY         = "llm_field_recovery"     # F2 null_field_recovery (live, not patched)

    # Last-resort defaults (never satisfy completeness alone)
    DEFAULT_AVAILABILITY       = "default_availability"   # AVAILABLE + today
```

This list is **closed** for the lifetime of this plan. Adding a new source is a deliberate change to this enum; no string literals anywhere else in the codebase.

### 3.2 `FieldValue` / `ExtractedSource` / `ProvenancedUnit`

```python
# ma_poc/models/[source.py](http://source.py) (continued)
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict

class FieldValue(BaseModel):
    """A single field's value with provenance."""
    model_config = ConfigDict(extra="forbid")
    value: str | int | float | None
    source: SourceId
    confidence: float = Field(ge=0.0, le=1.0)
    source_url: str = ""
    envelope_hash: str = ""    # sha256 of the source body — for drift detection

class ExtractedSource(BaseModel):
    """One source's contribution to the merge — a list of provenance-stamped unit dicts."""
    model_config = ConfigDict(extra="forbid")
    source_id: SourceId
    source_url: str
    envelope_hash: str
    units: list[dict[str, FieldValue]] = Field(default_factory=list)
    # Identity hints (from this source) — helps the merger link units across sources
    has_unit_ids: bool = False
    is_floor_plan_level: bool = False    # True for RentCafe/Entrata floorplan APIs

class ProvenancedUnit(dict):
    """dict[field_name, FieldValue] with helpers. Subclass of dict for legacy serialization."""
    # See ma_poc/models/[source.py](http://source.py) for full implementation in Phase 2.
```

### 3.3 Source ranking table (default, per-PMS overrides loaded from profile)

```python
# ma_poc/services/source_[planner.py](http://planner.py)
from ma_poc.models.source import SourceId

# Maps (field_group, pms_name) -> ordered list of (SourceId, base_confidence)
DEFAULT_SOURCE_RANKING: dict[str, list[tuple[SourceId, float]]] = {
    "identity": [
        (SourceId.API_RENTCAFE_UNITS, 0.95),
        (SourceId.API_SIGHTMAP, 0.95),
        (SourceId.API_ONESITE, 0.90),
        (SourceId.API_APPFOLIO_LISTINGS, 0.90),
        (SourceId.API_ENTRATA_WIDGET, 0.85),
        (SourceId.API_GENERIC_NARROW, 0.80),
        (SourceId.MAPPING_REPLAY, 0.85),
        (SourceId.LLM_API_TARGETED, 0.70),
        # below this floor (0.60), source cannot supply identity fields
    ],
    "physical": [
        (SourceId.API_RENTCAFE_FLOORPLANS, 0.95),
        (SourceId.API_ENTRATA_WIDGET, 0.95),
        (SourceId.API_GENERIC_NARROW, 0.90),
        (SourceId.API_SIGHTMAP, 0.90),
        (SourceId.MAPPING_REPLAY, 0.90),
        (SourceId.JSON_LD, 0.85),
        (SourceId.EMBEDDED_JSON, 0.80),
        (SourceId.DOM_PROFILE_HINTS, 0.75),
        (SourceId.DOM_CASCADE, 0.70),
        (SourceId.LLM_DOM_TARGETED, 0.70),
        (SourceId.LLM_API_TARGETED, 0.65),
        (SourceId.LLM_MONOLITHIC, 0.55),
        (SourceId.FIELD_PATCH, 0.60),
    ],
    "transactional": [
        (SourceId.API_RENTCAFE_UNITS, 0.95),
        (SourceId.API_SIGHTMAP, 0.95),
        (SourceId.API_ONESITE, 0.90),
        (SourceId.API_APPFOLIO_LISTINGS, 0.90),
        (SourceId.API_ENTRATA_WIDGET, 0.85),
        (SourceId.MAPPING_REPLAY, 0.85),
        (SourceId.API_GENERIC_NARROW, 0.80),
        (SourceId.JSON_LD, 0.75),
        (SourceId.DOM_CASCADE, 0.70),
        (SourceId.DOM_PROFILE_HINTS, 0.70),
        (SourceId.LLM_DOM_TARGETED, 0.65),
        (SourceId.LLM_API_TARGETED, 0.65),
        (SourceId.FIELD_PATCH, 0.60),
        (SourceId.LLM_MONOLITHIC, 0.55),
        (SourceId.DEFAULT_AVAILABILITY, 0.30),    # AVAILABLE today, last-resort
    ],
}

# Field membership in groups
FIELD_GROUP: dict[str, str] = {
    "unit_id": "identity",
    "floor_plan_name": "physical",       # also identity-relevant
    "beds": "physical",
    "baths": "physical",
    "sqft": "physical",
    "rent_low": "transactional",
    "rent_high": "transactional",
    "available_date": "transactional",
    "availability_status": "transactional",
}

CONFIDENCE_FLOORS: dict[str, float] = {
    "identity": 0.70,
    "physical": 0.50,
    "transactional": 0.55,
}
```

### 3.4 Completeness gates (decision map)

```python
# ma_poc/services/source_[planner.py](http://planner.py) (continued)
from dataclasses import dataclass

@dataclass(frozen=True)
class CompletenessReport:
    n_units: int
    pct_with_identity: float
    pct_with_physical: float
    pct_with_transactional: float
    pct_complete: float    # all three

def evaluate_completeness(units: list[ProvenancedUnit]) -> CompletenessReport:
    """Compute completeness fractions. Pure function."""
    ...

@dataclass(frozen=True)
class Decision:
    action: str    # one of: "STOP", "ESCALATE_LLM_TARGETED", "ESCALATE_LINK_HOP", "ESCALATE_LLM_MONOLITHIC", "ACCEPT_PARTIAL"
    target_field_group: str | None = None    # which axis to fill
    target_url: str | None = None             # for LINK_HOP
    rationale: str = ""

def plan_next_action(
    report: CompletenessReport,
    sources_already_run: set[SourceId],
    budget_remaining: dict[str, int],   # {"llm_targeted": 1, "link_hop": 1, "llm_monolithic": 1}
    profile_completeness_floor: dict[str, float] | None = None,
) -> Decision:
    """The decision map. Returns at most ONE escalation per call."""
    ...
```

Decision rules (locked):

```
STOP_NOW           when pct_complete >= 0.90 AND pct_with_transactional >= 0.70
TARGET_GAP         when 0.50 <= pct_complete < 0.90
                       → identify the failing axis (smallest of identity/physical/transactional pct)
                       → pick best untried source for that axis from ranking table
                       → if best source is LLM tier and budget["llm_targeted"] > 0:
                           emit ESCALATE_LLM_TARGETED with target_field_group
                       → elif best source is link-hop and budget["link_hop"] > 0:
                           emit ESCALATE_LINK_HOP with target_field_group
                       → else: emit ACCEPT_PARTIAL
BROAD_RECOVERY     when pct_complete < 0.50
                       → if budget["link_hop"] > 0: emit ESCALATE_LINK_HOP
                       → elif budget["llm_monolithic"] > 0: emit ESCALATE_LLM_MONOLITHIC
                       → else: emit ACCEPT_PARTIAL

Override: profile_completeness_floor lowers the STOP threshold per-property
when a property has demonstrated it cannot reach 0.90 (e.g., auth-walled
availability page). Floor < 0.50 is rejected — don't emit garbage.
```

### 3.5 Identity-link rules for the merger

In strict precedence:

| Rank | Rule | Notes |
|---|---|---|
| 1 | `unit_id` equal across sources | Strictest. Trusted always. |
| 2 | `(floor_plan_name, beds, baths)` equal AND one side has no `unit_id` | Floor-plan-level fan-out: one A-row joins N B-rows by `floor_plan_name`. |
| 3 | `(beds, baths, sqft_rounded_to_10)` equal | Fuzzy. Allowed only when no other option. **Must emit `IDENTITY.FUZZY_LINK` event** with confidence 0.6. |
| 4 | none of the above | Keep records separate. |

Implementation must reuse `compute_fallback_unit_id` from `ma_poc/scripts/identity_[fallback.py](http://fallback.py)` for the rank-3 fingerprint.

---

## 4. Tier label conventions (additive — never break existing)

After this plan ships, `extraction_tier_used` may carry these new labels. **Do not remove existing labels.** The reporting layer treats them as additive.

| Label | When |
|---|---|
| `TIER_MERGED_DETERMINISTIC` | Multiple deterministic sources merged, no LLM used |
| `TIER_MERGED_HYBRID` | Deterministic + LLM-targeted merged |
| `TIER_MERGED_CROSS_PAGE` | Main page + at least one link-hop sub-page merged |
| `TIER_PARTIAL` | Merge result accepted below STOP threshold (verdict=PARTIAL) |

Existing single-source labels (`TIER_1_API`, `TIER_4_LLM_DOM`, etc.) continue to apply when the merge result has only one contributing source.

---

## Phase 1 — Wire the dead writers

**Goal:** Make `success_count` reflect actual replay successes, populate `stats.*`, log every silent early-return. Until this is done, no later phase can be measured. Estimated: ~80 LoC + 5 tests.

### Why first

Phase 0 audit confirmed `total_scrapes`, `total_successes`, `total_failures`, `total_llm_calls`, `total_llm_cost_usd` are never written, and `success_count` only increments on duplicate-resave (`profile_updater.py:101`), never on actual replay. Without trustworthy telemetry, every later phase's gate is blind.

### Files

#### `ma_poc/services/profile_[updater.py](http://updater.py)`

In `update_profile_after_extraction`, add stats updates at the top of the function (BEFORE the existing streak logic):

```python
# Phase 1: monotonic stats — never go backward
[profile.stats.total](http://profile.stats.total)_scrapes += 1
tier = scrape_result.get("extraction_tier_used") or ""
profile.stats.last_tier_used = tier or None
profile.stats.last_unit_count = units_extracted

if units_extracted > 0 and tier and tier != "FAILED":
    [profile.stats.total](http://profile.stats.total)_successes += 1
else:
    [profile.stats.total](http://profile.stats.total)_failures += 1

# LLM cost accounting (the AdapterResult-to-dict translator already attaches
# _llm_interactions in scraper.py:512-513; sum once here).
llm_interactions = scrape_result.get("_llm_interactions") or []
if llm_interactions:
    [profile.stats.total](http://profile.stats.total)_llm_calls += len(llm_interactions)
    [profile.stats.total](http://profile.stats.total)_llm_cost_usd += sum(
        i.get("cost_usd", 0.0) for i in llm_interactions
    )
```

In `save_llm_field_mapping`, change the silent early-return to a logged decline AND populate `discovered_at`:

```python
def save_llm_field_mapping(profile: ScrapeProfile, mapping_dict: dict) -> bool:
    """Returns True on save/upsert, False on rejection. Never raises."""
    url_pattern = mapping_dict.get("api_url_pattern", "")
    json_paths = mapping_dict.get("json_paths") or {}
    if not url_pattern:
        log.warning(
            "save_llm_field_mapping: dropped mapping with empty api_url_pattern (paths=%d)",
            len(json_paths),
        )
        return False
    if not json_paths:
        log.warning(
            "save_llm_field_mapping: dropped mapping with empty json_paths for url=%s",
            url_pattern[:80],
        )
        return False

    for existing in profile.api_hints.llm_field_mappings:
        if existing.api_url_pattern == url_pattern:
            existing.json_paths = json_paths
            existing.response_envelope = mapping_dict.get("response_envelope", existing.response_envelope)
            # NOTE: success_count is NOT incremented here. This is the
            # "re-save" path. success_count belongs to the REPLAY path
            # (see Phase 5 / [generic.py](http://generic.py) changes).
            return True

    profile.api_hints.llm_field_mappings.append(
        LlmFieldMapping(
            api_url_pattern=url_pattern,
            json_paths=json_paths,
            response_envelope=mapping_dict.get("response_envelope", ""),
        )
    )
    if len(profile.api_hints.llm_field_mappings) > _MAX_LLM_FIELD_MAPPINGS:
        profile.api_hints.llm_field_mappings = profile.api_hints.llm_field_mappings[-_MAX_LLM_FIELD_MAPPINGS:]
    return True
```

#### `ma_poc/pms/adapters/[generic.py](http://generic.py)`

At lines 552-563 (after a successful replay), add success_count increment:

```python
                                if units:
                                    replayed_units.extend(units)
                                    result.api_responses.append(resp)
                                    # Phase 1: increment success_count on the
                                    # mapping that was actually used. Mapping
                                    # may be a Pydantic model OR a dict — handle both.
                                    if hasattr(mapping, "success_count"):
                                        try:
                                            mapping.success_count += 1
                                        except Exception:
                                            pass
                                    break
```

NOTE: this mutates the mapping object in `profile.api_hints.llm_field_mappings` directly. Acceptable because (a) the profile is saved by the orchestrator after `update_profile_after_extraction` runs, (b) it's a per-property profile so no concurrent-write hazard.

### Named tests — `tests/profile/test_phase1_[writers.py](http://writers.py)`

| Test | Check |
|---|---|
| `test_total_scrapes_increments_each_run` | Two consecutive runs → `[stats.total](http://stats.total)_scrapes == 2` |
| `test_total_successes_only_on_units_present` | Run with 0 units → `total_successes` unchanged |
| `test_llm_cost_accumulates` | Run with `_llm_interactions=[{cost_usd:0.01}, {cost_usd:0.02}]` → `total_llm_cost_usd == 0.03` |
| `test_save_mapping_rejects_empty_paths_with_log` | Pass `mapping_dict` with `json_paths={}` → returns `False`, log captured |
| `test_save_mapping_rejects_empty_url_with_log` | Pass `mapping_dict` with empty `api_url_pattern` → returns `False`, log captured |
| `test_replay_increments_success_count` | Build profile with one mapping at `success_count=0`, run cascade against a captured payload that matches, assert `success_count==1` after |
| `test_resave_does_not_increment_success_count` | Call `save_llm_field_mapping` twice with same `url_pattern` → mapping count unchanged, `success_count` unchanged |

### Phase 1 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 1` passes iff:
- All 7 tests pass
- Static scan: `grep -nE "stats\.total_(scrapes|successes|failures|llm_calls)\s*\+=" ma_poc/services/profile_[updater.py](http://updater.py)` returns at least 4 hits
- After running `--limit 50` against fixture corpus, every output profile has `[stats.total](http://stats.total)_scrapes >= 1`

---

## Phase 2 — Field-provenance primitive

**Goal:** Introduce `FieldValue`, `ExtractedSource`, `ProvenancedUnit`, `SourceId` — drop-in types that adapters and the merger will use. Snapshot tests prove ZERO behavioral change to existing pipeline. Estimated: ~250 LoC + 8 tests.

### File: `ma_poc/models/[source.py](http://source.py)` (new)

Implement the types from §3.1 and §3.2. Plus serializer:

```python
def to_legacy_unit(unit: ProvenancedUnit) -> dict[str, Any]:
    """Serialize a ProvenancedUnit back to make_unit_dict() shape.

    The output dict has all the fields make_unit_dict produces, populated
    from the FieldValue.value at each provenanced field. Unset fields fall
    back to make_unit_dict defaults. Provenance is stashed under
    `_provenance` (single underscore, gets stripped at v1/v2 boundary).
    """
    legacy: dict[str, Any] = {}
    provenance: dict[str, dict[str, Any]] = {}
    for field_name, fv in unit.items():
        if not isinstance(fv, FieldValue):
            continue
        legacy[field_name] = fv.value
        provenance[field_name] = {
            "source": fv.source.value,
            "confidence": fv.confidence,
            "source_url": fv.source_url,
            "envelope_hash": fv.envelope_hash,
        }
    legacy["_provenance"] = provenance
    return legacy

def from_legacy_unit(
    unit: dict[str, Any],
    source: SourceId,
    source_url: str,
    envelope_hash: str,
    confidence: float,
) -> ProvenancedUnit:
    """Wrap a legacy unit dict (from make_unit_dict) into a ProvenancedUnit.

    Every non-empty, non-default field gets a FieldValue. Used by adapters
    to lift their existing output into the provenance system.
    """
    ...
```

`from_legacy_unit` rules:

- Skip fields whose value is `""`, `None`, `0`, `"0"`, or `-1` — these are "absent" markers per `make_unit_dict()` defaults.
- For fields with values, set `FieldValue(value=..., source=source, confidence=confidence, source_url=source_url, envelope_hash=envelope_hash)`.
- `confidence` argument is the per-source default from §3.3. Per-field confidence overrides not supported in Phase 2.

### Envelope hash

```python
import hashlib
import json

def envelope_hash_of(body: Any) -> str:
    """Stable sha256 of an API response body, used for drift detection.

    For dicts: sorted-keys JSON serialization.
    For lists: serialized as-is (order-significant, since order matters for unit ordering).
    For strings/bytes: direct hash of utf-8 bytes.
    Returns 16-char prefix to keep profile sizes reasonable.
    """
    if isinstance(body, (dict, list)):
        s = json.dumps(body, sort_keys=True, default=str)
    elif isinstance(body, bytes):
        s = body.decode("utf-8", errors="replace")
    else:
        s = str(body)
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:16]
```

### Adapter integration (Phase 2 only — additive, no behavior change)

In each adapter (`[rentcafe.py](http://rentcafe.py)`, `[entrata.py](http://entrata.py)`, `[appfolio.py](http://appfolio.py)`, `[sightmap.py](http://sightmap.py)`), after the existing `parse_*` produces legacy unit dicts, additionally produce an `ExtractedSource` and stash on `AdapterResult`:

```python
# in [rentcafe.py](http://rentcafe.py) extract():
from ma_poc.models.source import (
    ExtractedSource, SourceId, envelope_hash_of, from_legacy_unit
)

# ... existing logic ...

if all_units:
    result.units = all_units                      # legacy path unchanged
    result.winning_url = ...
    result.confidence = ...
    # Phase 2 addition: provenanced source for the merger to consume later.
    sources: list[ExtractedSource] = []
    for resp in result.api_responses:
        body = resp.get("body")
        url = resp.get("url", "")
        env_h = envelope_hash_of(body)
        # Match units to their originating response by source_api_url
        units_from_resp = [u for u in all_units if u.get("source_api_url") == url]
        if not units_from_resp:
            continue
        prov_units = [
            from_legacy_unit(u, SourceId.API_RENTCAFE_FLOORPLANS, url, env_h, 0.95)
            for u in units_from_resp
        ]
        sources.append(ExtractedSource(
            source_id=SourceId.API_RENTCAFE_FLOORPLANS,
            source_url=url,
            envelope_hash=env_h,
            units=prov_units,
            has_unit_ids=False,            # RentCafe is floor-plan-level
            is_floor_plan_level=True,
        ))
    result._sources = sources               # type: ignore[attr-defined]
```

Repeat for entrata, appfolio (set `is_floor_plan_level=True` for both — they emit floorplan-level data per the research logs), sightmap (`is_floor_plan_level=False` — sightmap joins units to floorplans).

### `ma_poc/pms/[scraper.py](http://scraper.py)` translator

After line 531 (where existing `_llm_*` keys are copied), add:

```python
# Phase 2: surface provenanced sources for the planner/merger.
adapter_sources = getattr(adapter_result, "_sources", None)
if adapter_sources:
    result["_sources"] = list(adapter_sources)
```

### Named tests — `tests/services/test_source_[primitive.py](http://primitive.py)`

| Test | Check |
|---|---|
| `test_field_value_validation` | `FieldValue(confidence=1.5)` raises; `FieldValue(confidence=0.5)` passes |
| `test_envelope_hash_is_stable` | Same body → same hash; reordered dict keys → same hash; reordered list → DIFFERENT hash |
| `test_to_legacy_round_trip` | `to_legacy_unit(from_legacy_unit(u, ...))` produces a dict where every key in `u` (with non-empty value) is present with the same value |
| `test_to_legacy_drops_provenance_at_v1_boundary` | After `to_legacy_unit`, then strip `_provenance`, output passes `make_unit_dict` shape contract |
| `test_from_legacy_skips_default_fields` | Pass unit with `availability_status="AVAILABLE"` (the make_unit_dict default), `floor_plan_name=""`, `sqft="-1"` → ProvenancedUnit has no entries for those |
| `test_source_id_enum_is_closed` | Static scan: `grep -rE '"(api_floorplan_units\|json_ld\|llm_monolithic)"' ma_poc/` returns no hits outside `models/[source.py](http://source.py)` |
| `test_adapter_emits_sources_no_behavior_change` | Run RentCafe adapter on a fixture; legacy `result.units` == previous run; `result._sources` is populated |
| `test_extracted_source_per_envelope` | Adapter with 2 captured API responses → 2 `ExtractedSource` entries, each with its own envelope_hash |

### Phase 2 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 2` passes iff:
- All 8 tests pass
- Snapshot tests in `tests/pms/adapters/` still all pass (no behavior change to adapters)
- Static scan (H8): `grep -rE '"(api_[a-z_]+|llm_[a-z_]+|dom_[a-z_]+|mapping_replay|field_patch|cluster_)"' ma_poc/services/ ma_poc/pms/` returns hits ONLY in `models/[source.py](http://source.py)` and `services/source_[planner.py](http://planner.py)`
- `mypy --strict ma_poc/models/[source.py](http://source.py)` passes

---

## Phase 3 — Pure merger function

**Goal:** Implement `merge_sources()` as a pure function. Identity-link units across sources, fan out floor-plan-level to unit-level, pick max-confidence per field. Estimated: ~250 LoC + 12 tests.

### File: `ma_poc/services/source_[merger.py](http://merger.py)` (new)

```python
"""
Pure merger — combines multiple ExtractedSources into provenanced units.

INVARIANTS (test-enforced):
  - Pure function: same inputs always produce same output
  - No I/O, no logging side-effects, no profile mutation
  - No source can trigger another source — input set is fixed before call
"""

from __future__ import annotations
from typing import Iterable
from ma_poc.models.source import (
    ExtractedSource, FieldValue, ProvenancedUnit, SourceId,
)
from ma_[poc.services](http://poc.services).source_planner import (
    DEFAULT_SOURCE_RANKING, FIELD_GROUP, CONFIDENCE_FLOORS,
)
from ma_poc.scripts.identity_fallback import compute_fallback_unit_id


def merge_sources(
    sources: Iterable[ExtractedSource],
    property_id: str,
    fuzzy_link_callback: callable | None = None,
) -> list[ProvenancedUnit]:
    """Merge sources into provenanced units. See identity-link rules in §3.5."""
    ...
```

### Identity linking — concrete pseudocode

```
buckets: dict[tuple, list[(SourceId, ProvenancedUnit, int_rank)]] = {}

for src in sources:
    for unit in src.units:
        # rank-1: unit_id
        uid_fv = unit.get("unit_id")
        if src.has_unit_ids and isinstance(uid_fv, FieldValue) and uid_fv.value:
            buckets.setdefault(("uid", str(uid_fv.value)), []).append(
                (src.source_id, unit, 1))
            continue
        # rank-2: floor-plan-level
        if [src.is](http://src.is)_floor_plan_level:
            fp_fv = unit.get("floor_plan_name")
            beds_fv = unit.get("beds")
            baths_fv = unit.get("baths")
            if fp_fv and isinstance(fp_fv, FieldValue) and fp_fv.value:
                key = ("fp", str(fp_fv.value).strip().lower(),
                       _val(beds_fv), _val(baths_fv))
                buckets.setdefault(key, []).append((src.source_id, unit, 2))
                continue
        # rank-3: fuzzy (only if caller opts in via callback)
        if fuzzy_link_callback is not None:
            beds_fv, baths_fv, sqft_fv = unit.get("beds"), unit.get("baths"), unit.get("sqft")
            if all(isinstance(x, FieldValue) and x.value for x in (beds_fv, baths_fv, sqft_fv)):
                sqft_bucket = round(int(float(str(sqft_fv.value))) / 10) * 10
                key = ("fuzzy", _val(beds_fv), _val(baths_fv), str(sqft_bucket))
                buckets.setdefault(key, []).append((src.source_id, unit, 3))
                fuzzy_link_callback(unit, key, 0.6)
                continue
        # No identity → keep alone
        buckets[("alone", id(unit))] = [(src.source_id, unit, 4)]

# Fan-out: rank-2 (floor-plan-level) ⨯ rank-1 (unit-level) sharing floor_plan_name
out_units: list[ProvenancedUnit] = []

# 1. Process rank-1 buckets first; remember which floor_plan_names were absorbed
absorbed_fps: set[str] = set()
for key, entries in buckets.items():
    if key[0] != "uid":
        continue
    # Check if any entry has a floor_plan_name from a rank-2 bucket
    fp_name = _extract_fp_name(entries)
    if fp_name and ("fp", fp_name, ...) in matching_fp_buckets(buckets, fp_name):
        # Pull in the matching rank-2 entries as additional contributors
        fp_entries = pull_matching_fp(buckets, fp_name)
        merged = _merge_field_max_confidence(entries + fp_entries)
        absorbed_fps.add(fp_name)
        out_units.append(merged)
    else:
        out_units.append(_merge_field_max_confidence(entries))

# 2. Emit rank-2 floor-plan-only buckets (no matching unit-level rows)
for key, entries in buckets.items():
    if key[0] != "fp":
        continue
    fp_name = key[1]
    if fp_name in absorbed_fps:
        continue
    out_units.append(_merge_field_max_confidence(entries))

# 3. Emit fuzzy and alone buckets
for key, entries in buckets.items():
    if key[0] in ("fuzzy", "alone"):
        out_units.append(_merge_field_max_confidence(entries))

# 4. Identity fallback for units missing unit_id
for unit in out_units:
    uid = unit.get("unit_id")
    if uid is None or (isinstance(uid, FieldValue) and not uid.value):
        legacy = {k: v.value for k, v in unit.items() if isinstance(v, FieldValue)}
        fb = compute_fallback_unit_id(legacy, property_id)
        if fb:
            unit["unit_id"] = FieldValue(
                value=fb,
                source=SourceId.API_GENERIC_NARROW,    # placeholder for fallback
                confidence=0.65,
            )

return sorted(out_units, key=_unit_sort_key)
```

### `_merge_field_max_confidence`

```python
def _merge_field_max_confidence(entries: list[tuple[SourceId, ProvenancedUnit, int]]) -> ProvenancedUnit:
    """Pick max-confidence FieldValue per field, respecting confidence floors."""
    out = ProvenancedUnit()
    all_fields = set().union(*[set(u.keys()) for _, u, _ in entries])
    for field_name in all_fields:
        best: FieldValue | None = None
        for _src, unit, _rank in entries:
            fv = unit.get(field_name)
            if not isinstance(fv, FieldValue):
                continue
            group = FIELD_GROUP.get(field_name)
            if group and fv.confidence < CONFIDENCE_FLOORS[group]:
                continue
            if best is None or fv.confidence > best.confidence:
                best = fv
        if best is not None:
            out[field_name] = best
    return out
```

### Named tests — `tests/services/test_source_[merger.py](http://merger.py)`

Use real captured payloads from `data/runs/*/raw_api/`. **Do not use mocked dicts.**

| Test | Fixture | Check |
|---|---|---|
| `test_merger_pure_function` | any | Two consecutive calls → identical output. Input untouched. |
| `test_rank1_unit_id_match_merges` | sightmap fixture | Two sources both with `unit_id=305` → one output unit |
| `test_rank2_fan_out_floor_plan_to_units` | rentcafe fp + synthesized unit-level | FP "A1" + units 305/410 → 2 output units, each with FP A1's beds/baths/sqft |
| `test_max_confidence_picks_winner` | LLM (0.65) + API (0.95) for rent_low | API wins |
| `test_confidence_floor_rejects_below_threshold` | LLM monolithic 0.4 contributing unit_id | unit_id NOT in output (below identity floor 0.7) |
| `test_fuzzy_link_emits_event` | two sources, no unit_id, same beds/baths/sqft | callback called once |
| `test_no_match_keeps_separate` | two sources, disjoint identifiers | output has both, separate |
| `test_identity_fallback_when_missing` | merged unit with no unit_id but fp/beds/sqft | `unit_id` starts with `inferred_` |
| `test_serialization_back_to_legacy` | merged → `to_legacy_unit` | passes `make_unit_dict` shape |
| `test_envelope_hash_drift_does_not_link` | same unit_id, different envelope_hash | merged; both sources in `_provenance` |
| `test_strip_after_to_legacy` | `_provenance` stripped | dict shape matches `make_unit_dict()` keys exactly |
| `test_empty_sources_returns_empty` | `merge_sources([])` | returns `[]`, no exception |

### Phase 3 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 3` passes iff:
- All 12 tests pass
- `mypy --strict ma_poc/services/source_[merger.py](http://merger.py)` passes
- Static scan: `merge_sources` body contains no `import logging`, no `log.` calls, no `profile.` references — purity enforced
- Property-based test (hypothesis): random source lists never raise

---

## Phase 4 — Decision map module

**Goal:** Externalize the source ranking, completeness gates, and decision logic into `services/source_[planner.py](http://planner.py)`. Codify all thresholds; tunable via profile but never hardcoded outside this module. Estimated: ~200 LoC + 12 tests.

### File: `ma_poc/services/source_[planner.py](http://planner.py)` (new)

Implement everything in §3.3, §3.4. Key API:

```python
def evaluate_completeness(units: list[ProvenancedUnit]) -> CompletenessReport:
    """Pure function. n_units=0 returns CompletenessReport with all pcts at 0.0."""
    ...

def rank_sources_for_field_group(
    field_group: str,
    pms_name: str,
    profile_preferences: dict[str, list[SourceObservation]] | None = None,
) -> list[tuple[SourceId, float]]:
    """If profile_preferences has data, observed-winning sources float to top
    within their tier; otherwise default ranking applies."""
    ...

def plan_next_action(
    report: CompletenessReport,
    sources_already_run: set[SourceId],
    budget_remaining: dict[str, int],
    pms_name: str = "unknown",
    profile_completeness_floor: dict[str, float] | None = None,
    profile_preferences: dict[str, list[SourceObservation]] | None = None,
) -> Decision:
    """Returns at most ONE Decision per call. Hard invariants test-enforced."""
    ...
```

### Plan_next_action — concrete logic

```python
def plan_next_action(report, sources_already_run, budget_remaining, ...):
    floor = profile_completeness_floor or {}
    floor_pct_complete = max(0.50, floor.get("complete", 0.90))   # never below 0.50
    floor_pct_trans = max(0.50, floor.get("transactional", 0.70))

    # STOP
    if report.pct_complete >= floor_pct_complete and report.pct_with_transactional >= floor_pct_trans:
        return Decision(action="STOP", rationale=f"complete={report.pct_complete:.2f}>=floor")

    # Identify failing axis
    pct_by_group = {
        "identity": report.pct_with_identity,
        "physical": report.pct_with_physical,
        "transactional": report.pct_with_transactional,
    }
    failing_group = min(pct_by_group, key=pct_by_group.get)

    if 0.50 <= report.pct_complete < floor_pct_complete:
        # TARGET_GAP
        ranking = rank_sources_for_field_group(failing_group, pms_name, profile_preferences)
        for source_id, _conf in ranking:
            if source_id in sources_already_run:
                continue
            if source_id in (SourceId.LLM_API_TARGETED, SourceId.LLM_DOM_TARGETED):
                if budget_remaining.get("llm_targeted", 0) > 0:
                    return Decision(
                        action="ESCALATE_LLM_TARGETED",
                        target_field_group=failing_group,
                        rationale=f"gap in {failing_group}, best untried={source_id}",
                    )
            elif source_id == SourceId.LLM_MONOLITHIC:
                continue   # only fires in BROAD_RECOVERY
            elif source_id in (SourceId.DOM_PROFILE_HINTS, SourceId.MAPPING_REPLAY,
                               SourceId.FIELD_PATCH, SourceId.JSON_LD,
                               SourceId.EMBEDDED_JSON, SourceId.DOM_CASCADE):
                continue   # cascade should've collected these
            else:
                if budget_remaining.get("link_hop", 0) > 0:
                    return Decision(
                        action="ESCALATE_LINK_HOP",
                        target_field_group=failing_group,
                        rationale=f"gap in {failing_group}, untried API source={source_id}",
                    )
        return Decision(action="ACCEPT_PARTIAL", rationale="no untried source within budget")

    # BROAD_RECOVERY (pct_complete < 0.50)
    if budget_remaining.get("link_hop", 0) > 0 and SourceId.DOM_CASCADE in sources_already_run:
        return Decision(action="ESCALATE_LINK_HOP",
                        rationale=f"broad recovery: pct={report.pct_complete:.2f}")
    if budget_remaining.get("llm_monolithic", 0) > 0:
        return Decision(action="ESCALATE_LLM_MONOLITHIC",
                        rationale=f"broad recovery: pct={report.pct_complete:.2f}")
    return Decision(action="ACCEPT_PARTIAL", rationale="broad recovery: no budget")
```

### Named tests — `tests/services/test_source_[planner.py](http://planner.py)`

| Test | Setup | Check |
|---|---|---|
| `test_completeness_empty_units` | `evaluate_completeness([])` | n_units=0, all pcts 0.0 |
| `test_completeness_all_complete` | 10 units id+physical+transactional | pcts all 1.0 |
| `test_decision_stop_when_complete` | report.pct_complete=0.95, trans=0.80 | action=STOP |
| `test_decision_target_gap_picks_failing_axis` | pct_with_transactional=0.4 others=0.95 | action=ESCALATE_LLM_TARGETED, target=transactional |
| `test_decision_one_decision_per_call` (H2) | low-completeness scenario, 100 calls | each returns exactly one Decision |
| `test_budget_zero_blocks_escalation` (H3) | budgets all 0 | action=ACCEPT_PARTIAL |
| `test_decision_broad_recovery_prefers_link_hop` | pct=0.2, both budgets allow | action=ESCALATE_LINK_HOP |
| `test_decision_broad_recovery_falls_to_monolithic` | pct=0.2, link_hop=0, monolithic=1 | action=ESCALATE_LLM_MONOLITHIC |
| `test_profile_floor_lowers_stop_threshold` | floor={complete:0.7}, report.pct_complete=0.75 | action=STOP |
| `test_profile_floor_below_50_pct_clamped` | floor={complete:0.3} | clamps internally to 0.50 |
| `test_ranking_table_has_all_sources` | for each group | every SourceId in §3.1 appears in at least one ranking |
| `test_profile_preferences_floats_observed_winners` | profile says FIELD_PATCH won 5x for transactional | FIELD_PATCH appears before default position |

### Phase 4 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 4` passes iff:
- All 12 tests pass
- Static scan: every confidence floor and every ranking entry lives in `services/source_[planner.py](http://planner.py)` (run `grep -nE "0\.(5|6|7|8|9)\d?" ma_poc/pms/adapters/[generic.py](http://generic.py)` baseline before phase; assert no NEW hits after)
- `mypy --strict ma_poc/services/source_[planner.py](http://planner.py)` passes

---

## Phase 5 — Intra-page integration in `[generic.py](http://generic.py)`

**Goal:** Restructure the cascade in `ma_poc/pms/adapters/[generic.py](http://generic.py)`. Sub-tiers 0–5 collect into provenanced sources rather than return-on-hit. Merger runs once. LLM tiers (6a/6b/6c) gated by planner decision. Sub-tier 0 (`profile_replay`) becomes a contributor not a preempter — fixing the Channel-1 regression risk identified in audit. Estimated: ~250 LoC + adapter snapshot tests.

### Critical fix: sub-tier 0 is no longer return-on-hit

Current code (`generic.py:556-570`) preempts the adapter:

```python
if replayed_units:
    result.units = replayed_units
    result.tier_used = "TIER_1_PROFILE_MAPPING"
    return result      # ← preempts adapter
```

After Phase 5:

```python
if replayed_units:
    sources.append(ExtractedSource(
        source_id=SourceId.MAPPING_REPLAY,
        source_url=replay_url,
        envelope_hash=envelope_hash_of(replay_body),
        units=[from_legacy_unit(u, SourceId.MAPPING_REPLAY, replay_url, env_h, 0.85) for u in replayed_units],
        has_unit_ids=any(u.get("unit_number") for u in replayed_units),
        is_floor_plan_level=False,
    ))
    # Continue to sub-tier 1 — do NOT return.
```

This is the single most important behavioral change in Phase 5. A partial mapping (e.g. one that learned only `beds: $.bedroom_range`) cannot regress the adapter's native `rent_low`, `unit_id`.

### Cascade restructure — pseudocode

```python
async def extract(self, page, ctx) -> AdapterResult:
    result = AdapterResult(...)
    sources: list[ExtractedSource] = []
    profile = ctx.profile

    # 0. blocked_filter (unchanged)
    api_responses = self._apply_blocklist(api_responses, profile)

    # 1. Collect deterministic sources — NEVER return early
    sources.extend(self._collect_mapping_replay(api_responses, profile))   # MAPPING_REPLAY
    sources.extend(self._collect_api_narrow(api_responses))                 # API_GENERIC_NARROW
    sources.extend(self._collect_api_broad(api_responses))                  # API_GENERIC_BROAD
    sources.extend(self._collect_jsonld(html))                              # JSON_LD
    sources.extend(self._collect_embedded_json(html))                       # EMBEDDED_JSON
    sources.extend(self._collect_dom_profile_hints(html, profile))          # DOM_PROFILE_HINTS (Phase 8 wires)
    sources.extend(self._collect_dom_cascade(html))                         # DOM_CASCADE

    # 2. Merge → evaluate
    merged = merge_sources(sources, [ctx.property](http://ctx.property)_id, fuzzy_link_callback=_emit_fuzzy_event)
    report = evaluate_completeness(merged)

    # 3. LLM gate (existing skip rules apply)
    if skip_llm:
        return self._finalize(result, merged, sources, report)

    # 4. Plan
    budget = {"llm_targeted": 1, "link_hop": 0, "llm_monolithic": 1}
    sources_run = {s.source_id for s in sources}
    decision = plan_next_action(
        report, sources_run, budget,
        pms_name=ctx.detected.pms,
        profile_completeness_floor=getattr(profile, "rolling_completeness", None),
        profile_preferences=getattr(profile.api_hints, "field_source_preferences", None),
    )

    if decision.action == "STOP":
        return self._finalize(result, merged, sources, report)

    if decision.action == "ESCALATE_LLM_TARGETED":
        if [decision.target](http://decision.target)_field_group == "physical":
            new_sources = await self._collect_llm_dom_targeted(html, ctx)
        else:
            new_sources = await self._collect_llm_api_targeted(api_responses, ctx)
        sources.extend(new_sources)
        merged = merge_sources(sources, [ctx.property](http://ctx.property)_id, fuzzy_link_callback=_emit_fuzzy_event)
        report = evaluate_completeness(merged)
        return self._finalize(result, merged, sources, report)

    if decision.action == "ESCALATE_LLM_MONOLITHIC":
        new_sources = await self._collect_llm_monolithic(html, api_responses, ctx)
        sources.extend(new_sources)
        merged = merge_sources(sources, [ctx.property](http://ctx.property)_id, fuzzy_link_callback=_emit_fuzzy_event)
        return self._finalize(result, merged, sources, report)

    # ESCALATE_LINK_HOP / ACCEPT_PARTIAL — return for orchestrator (Phase 9 handles link-hop)
    return self._finalize(result, merged, sources, report, pending_decision=decision)
```

### `_finalize`

```python
LEGACY_TIER_FOR_SOURCE = {
    SourceId.API_GENERIC_NARROW: "TIER_1_API",
    SourceId.API_GENERIC_BROAD: "TIER_1_API",
    SourceId.JSON_LD: "TIER_2_JSONLD",
    SourceId.EMBEDDED_JSON: "TIER_1_5_EMBEDDED",
    SourceId.DOM_CASCADE: "TIER_3_DOM",
    SourceId.DOM_PROFILE_HINTS: "TIER_3_DOM",
    SourceId.MAPPING_REPLAY: "TIER_1_PROFILE_MAPPING",
    SourceId.LLM_API_TARGETED: "TIER_4_LLM_API",
    SourceId.LLM_DOM_TARGETED: "TIER_4_LLM_DOM",
    SourceId.LLM_MONOLITHIC: "TIER_4_LLM",
}

def _finalize(self, result, merged_units, sources, report, pending_decision=None):
    result.units = [to_legacy_unit(u) for u in merged_units]
    result._sources = sources
    result._completeness = report

    src_ids = {s.source_id for s in sources if s.units}
    has_llm = any(s in src_ids for s in (
        SourceId.LLM_API_TARGETED, SourceId.LLM_DOM_TARGETED, SourceId.LLM_MONOLITHIC))
    has_deterministic = any(s in src_ids for s in (
        SourceId.API_GENERIC_NARROW, SourceId.API_GENERIC_BROAD, SourceId.JSON_LD,
        SourceId.EMBEDDED_JSON, SourceId.DOM_CASCADE, SourceId.DOM_PROFILE_HINTS,
        SourceId.MAPPING_REPLAY))
    if len(src_ids) == 1:
        sole = next(iter(src_ids))
        result.tier_used = LEGACY_TIER_FOR_SOURCE.get(sole, result.tier_used)
    elif has_deterministic and not has_llm:
        result.tier_used = "TIER_MERGED_DETERMINISTIC"
    elif has_deterministic and has_llm:
        result.tier_used = "TIER_MERGED_HYBRID"

    if pending_decision is not None:
        result._pending_decision = pending_decision

    if result.units:
        result.confidence = min(0.95, 0.6 + 0.04 * len(result.units))
    else:
        result.confidence = 0.0

    return result
```

### Loop-safeguard tests — `tests/integration/test_loop_[safeguards.py](http://safeguards.py)`

These are the H1, H2, H3 invariant tests. **Must pass before Phase 5 ships.**

```python
def test_h1_merger_pure_function():
    sources = build_test_sources()
    out_a = merge_sources(sources, "test_prop")
    out_b = merge_sources(sources, "test_prop")
    assert out_a == out_b
    # Inputs untouched
    assert all(s.units == original_units(s) for s in sources)

async def test_h2_one_escalation_per_run():
    """Cascade against low-completeness fixture → planner invoked at most once per page."""
    with patch("ma_[poc.services](http://poc.services).source_planner.plan_next_action",
               wraps=plan_next_action) as planner:
        await GenericAdapter().extract(page, ctx)
    assert [planner.call](http://planner.call)_count <= 1

async def test_h3_llm_budget_caps():
    """Even on extreme low-completeness:
       - at most 1 LLM-targeted call (6a OR 6b, never both)
       - at most 1 LLM-monolithic call
       - never both LLM-targeted AND LLM-monolithic in the same run."""
    counts = collect_llm_call_counts()
    assert counts.targeted <= 1
    assert counts.monolithic <= 1
    assert not (counts.targeted == 1 and counts.monolithic == 1)
```

### Adapter snapshot tests

Re-run the existing adapter snapshot suite. EVERY `tests/pms/adapters/fixtures/<pms>/*.json` payload must produce the same legacy `make_unit_dict()` shape as before Phase 5. Merger may reorder; sort by `unit_id` before comparing.

### Phase 5 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 5` passes iff:
- H1, H2, H3 tests pass
- All adapter snapshot tests pass (units count and field values unchanged)
- Property-level test: `--limit 50 --fixture-corpus 2026-04-25` produces the same `units_extracted` per property as the pre-Phase-5 baseline (record baseline before phase starts)
- LLM cost on fixture-corpus does NOT exceed pre-Phase-5 baseline
- Acceptance test `test_user_scenario_floor_plan_plus_availability` (see §11) passes — this is the cross-source merge end-to-end test

---

## Phase 6 — Stale-mapping eviction + envelope hash drift

**Goal:** Add per-mapping failure tracking and drift detection. Stale mappings get evicted before they pollute the cascade. Estimated: ~120 LoC + 6 tests.

### Schema additions to `LlmFieldMapping`

In `ma_poc/models/scrape_[profile.py](http://profile.py)`:

```python
class LlmFieldMapping(BaseModel):
    model_config = ConfigDict(extra="ignore")

    api_url_pattern: str
    json_paths: dict[str, str] = Field(default_factory=dict)
    response_envelope: str = ""
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    success_count: int = 0

    # Phase 6 additions
    consecutive_replay_failures: int = 0
    last_replayed_at: datetime | None = None
    source_envelope_hash: str = ""
    quality_score: float = 1.0    # set by Phase 10 self-validation; <1.0 demotes confidence
```

### Replay path in `[generic.py](http://generic.py)::_collect_mapping_replay`

```python
def _collect_mapping_replay(self, api_responses, profile, ctx):
    sources: list[ExtractedSource] = []
    saved = list(getattr(profile.api_hints, "llm_field_mappings", []) or [])
    if not saved:
        return sources

    from ma_[poc.services](http://poc.services).llm_extractor import apply_saved_mapping
    from ma_poc.models.source import envelope_hash_of, ExtractedSource, SourceId, from_legacy_unit

    for mapping in saved:
        pat = mapping.api_url_pattern
        if not pat:
            continue
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if pat not in url:
                continue
            current_hash = envelope_hash_of(body)

            # Phase 6: drift detection (only when hash known)
            if mapping.source_envelope_hash and current_hash != mapping.source_envelope_hash:
                _emit(EventKind.MAPPING_DRIFT_DETECTED, [ctx.property](http://ctx.property)_id,
                      url=url[:80], saved_hash=mapping.source_envelope_hash[:8],
                      current_hash=current_hash[:8])
                mapping.consecutive_replay_failures += 1
                continue

            try:
                units = apply_saved_mapping(body, {
                    "response_envelope": mapping.response_envelope,
                    "json_paths": mapping.json_paths,
                }) or []
            except Exception:
                units = []
                mapping.consecutive_replay_failures += 1
                continue

            mapping.last_replayed_at = datetime.utcnow()

            if not units:
                mapping.consecutive_replay_failures += 1
                _emit(EventKind.MAPPING_REPLAY_EMPTY, [ctx.property](http://ctx.property)_id, url=url[:80])
                continue

            # Success
            mapping.success_count += 1
            mapping.consecutive_replay_failures = 0
            prov_units = [
                from_legacy_unit(u, SourceId.MAPPING_REPLAY, url, current_hash, 0.85 * mapping.quality_score)
                for u in units
            ]
            sources.append(ExtractedSource(
                source_id=SourceId.MAPPING_REPLAY,
                source_url=url,
                envelope_hash=current_hash,
                units=prov_units,
                has_unit_ids=any(u.get("unit_number") for u in units),
                is_floor_plan_level=False,
            ))
            break

    return sources
```

### Eviction in `services/profile_[updater.py](http://updater.py)::update_profile_after_extraction`

Append at end of function, before `[store.save](http://store.save)(profile)`:

```python
# Phase 6: evict stale mappings
EVICTION_THRESHOLD = 3
before_count = len(profile.api_hints.llm_field_mappings)
profile.api_hints.llm_field_mappings = [
    m for m in profile.api_hints.llm_field_mappings
    if m.consecutive_replay_failures < EVICTION_THRESHOLD
]
evicted = before_count - len(profile.api_hints.llm_field_mappings)
if evicted:
    [log.info](http://log.info)("Evicted %d stale mapping(s) for %s", evicted, profile.canonical_id)
```

### Persist with envelope hash — `services/profile_[updater.py](http://updater.py)::save_llm_field_mapping`

Update Phase 1's signature to accept the hash:

```python
def save_llm_field_mapping(profile, mapping_dict, source_envelope_hash: str = "") -> bool:
    ...
    profile.api_hints.llm_field_mappings.append(
        LlmFieldMapping(
            api_url_pattern=url_pattern,
            json_paths=json_paths,
            response_envelope=mapping_dict.get("response_envelope", ""),
            source_envelope_hash=source_envelope_hash,
        )
    )
    ...
```

In `[generic.py](http://generic.py)::_collect_llm_api_targeted`, when building mapping_dict:

```python
mapping_dict["_envelope_hash"] = envelope_hash_of(resp.get("body"))
```

In `update_profile_after_extraction` (where it processes `_llm_analysis_results`):

```python
if isinstance(result, dict) and result.get("api_url_pattern"):
    save_llm_field_mapping(profile, result, source_envelope_hash=result.get("_envelope_hash", ""))
```

### Migration — `scripts/migrate_profiles_[xsource.py](http://xsource.py)`

For every profile, set defaults on existing `LlmFieldMapping` entries:

- `consecutive_replay_failures = 0`
- `last_replayed_at = None`
- `source_envelope_hash = ""`   (empty hash means "unknown — skip drift check")
- `quality_score = 1.0`

When `source_envelope_hash == ""`, `_collect_mapping_replay` does NOT run drift detection. Migration cannot break existing replays.

### Named tests — `tests/profile/test_mapping_[eviction.py](http://eviction.py)`

| Test | Setup | Check |
|---|---|---|
| `test_drift_detected_increments_failures` | mapping hash A, body hash B | `failures += 1`, `MAPPING_DRIFT_DETECTED` emitted |
| `test_empty_replay_increments_failures` | mapping replay returns 0 | `failures += 1` |
| `test_successful_replay_resets_failures` | failures=2, replay returns 5 units | failures=0; success_count += 1 |
| `test_eviction_at_3_failures` | failures=3 | mapping removed |
| `test_no_drift_check_when_hash_empty` | `source_envelope_hash=""` | replay runs regardless of body hash |
| `test_drift_blocks_replay_below_threshold` | 1 prior failure, body drifts | replay skipped; failures=2; NOT yet evicted |

### Phase 6 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 6` passes iff:
- All 6 tests pass
- Migration runs successfully on `config/profiles/`; counts match input/output; defaults populated
- Static scan: every `LlmFieldMapping` mutation in `[generic.py](http://generic.py)` paired with `success_count += 1` OR `consecutive_replay_failures += 1` (never silent)

---

## Phase 7 — Channel-4 as `field_patches`

**Goal:** Convert `null_field_recovery` output into a non-destructive patch channel. Patches replay against captured API responses on subsequent runs as a low-priority source. Strip JSONPath `$.` prefix at boundary. Bind to source URL of the actually-recovered response, not `raw_apis[0]`. Estimated: ~180 LoC + 8 tests.

### Schema addition

```python
# ma_poc/models/scrape_[profile.py](http://profile.py)
from typing import Literal

class FieldPatch(BaseModel):
    """A learned field-completion hint from null_field_recovery."""
    model_config = ConfigDict(extra="ignore")

    api_url_pattern: str
    field_name: Literal["rent_low", "rent_high", "unit_id", "floor_plan_name", "beds", "baths", "sqft", "available_date"]
    json_path: str             # dot-path; NO leading $. (stripped at boundary)
    confidence: float = Field(ge=0.0, le=1.0)
    parser_fix: str | None = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)
    consecutive_replay_failures: int = 0
    success_count: int = 0
    source_envelope_hash: str = ""

class ApiHints(BaseModel):
    ...
    field_patches: list[FieldPatch] = Field(default_factory=list)

    @field_validator("field_patches", mode="before")
    @classmethod
    def cap_field_patches(cls, v):
        if isinstance(v, list) and len(v) > 50:
            return v[:50]
        return v
```

### Recovery → patches in `ma_poc/scripts/jugnu_[runner.py](http://runner.py)::_run_null_field_recovery`

Replace the in-memory-only loop body (lines 881-897). Add a **patch emission** step:

```python
for rf in recovery.recovered_fields:
    if rf.confidence < 0.85 or rf.recovered_value is None:
        continue
    # Existing in-memory patch (KEEP — Phase 7 doesn't change current run behaviour)
    if rf.field_name == "rent_low" and unit.get("rent_low") is None:
        try:
            unit["rent_low"] = _format_rent(rf.recovered_value)
        except Exception:
            pass
    # ... (existing handlers for rent_high, unit_id, floor_plan_name)

    # Phase 7 NEW: emit a FieldPatch for next-run replay
    if rf.parser_fix or rf.source_path not in (None, "", "not_present"):
        clean_path = (rf.source_path or "").lstrip("$.").lstrip(".")
        if not clean_path:
            continue
        # Bind to the SPECIFIC response that recovery operated on
        source_url = _resolve_source_url(raw_apis, fragment, source_body)
        if not source_url:
            continue
        source_body_actual = _resolve_source_body(raw_apis, source_url)
        patch_dict = {
            "api_url_pattern": source_url,
            "field_name": rf.field_name,
            "json_path": clean_path,
            "confidence": rf.confidence,
            "parser_fix": rf.parser_fix,
            "_envelope_hash": envelope_hash_of(source_body_actual),
        }
        scrape_result.setdefault("_field_patches", []).append(patch_dict)
```

### `_resolve_source_url` — replaces `raw_apis[0]` indiscriminate use

```python
def _resolve_source_url(raw_apis, fragment, fallback_body) -> str:
    """Find the URL of the API response whose body contains `fragment`.

    Iterates raw_apis; returns first whose body equals or contains fragment by
    deep equality. Falls back to raw_apis[0] only if nothing matches.
    """
    for resp in raw_apis:
        body = resp.get("body")
        if body is fragment:
            return resp.get("url", "")
        if _body_contains_fragment(body, fragment):
            return resp.get("url", "")
    return raw_apis[0].get("url", "") if raw_apis else ""

def _body_contains_fragment(body, fragment) -> bool:
    """True if `fragment` appears as an item or sub-item of `body`."""
    if isinstance(body, list):
        return fragment in body or any(_body_contains_fragment(item, fragment) for item in body)
    if isinstance(body, dict):
        if fragment in body.values():
            return True
        for v in body.values():
            if isinstance(v, (list, dict)) and _body_contains_fragment(v, fragment):
                return True
    return False
```

The existing `_run_null_field_recovery` loop already iterates `source_items[i]` — pass each `fragment` through to `_resolve_source_url`.

### Persistence in `services/profile_[updater.py](http://updater.py)::update_profile_after_extraction`

After the LLM analysis results block, add:

```python
# Phase 7: persist field patches
patches_payload = scrape_result.get("_field_patches", []) or []
for patch_dict in patches_payload:
    save_field_patch(profile, patch_dict)
```

```python
def save_field_patch(profile: ScrapeProfile, patch_dict: dict) -> bool:
    """Upsert a FieldPatch by (url_pattern, field_name). Never raises."""
    try:
        url = patch_dict.get("api_url_pattern", "")
        field_name = patch_dict.get("field_name", "")
        if not url or not field_name:
            log.warning("save_field_patch: dropped %s/%s", url[:60], field_name)
            return False
        for existing in profile.api_hints.field_patches:
            if existing.api_url_pattern == url and existing.field_name == field_name:
                existing.json_path = patch_dict.get("json_path", existing.json_path)
                existing.confidence = patch_dict.get("confidence", existing.confidence)
                existing.parser_fix = patch_dict.get("parser_fix", existing.parser_fix)
                return True
        profile.api_hints.field_patches.append(FieldPatch(
            api_url_pattern=url,
            field_name=field_name,
            json_path=patch_dict.get("json_path", ""),
            confidence=patch_dict.get("confidence", 0.85),
            parser_fix=patch_dict.get("parser_fix"),
            source_envelope_hash=patch_dict.get("_envelope_hash", ""),
        ))
        if len(profile.api_hints.field_patches) > 50:
            profile.api_hints.field_patches = profile.api_hints.field_patches[-50:]
        return True
    except Exception as exc:
        log.warning("save_field_patch failed: %s", exc)
        return False
```

### Replay in `[generic.py](http://generic.py)::_collect_field_patches`

```python
def _collect_field_patches(self, api_responses, profile, ctx):
    patches = list(getattr(profile.api_hints, "field_patches", []) or [])
    if not patches:
        return []
    sources: list[ExtractedSource] = []
    for resp in api_responses:
        url = resp.get("url", "")
        body = resp.get("body")
        applicable = [p for p in patches if p.api_url_pattern in url or url in p.api_url_pattern]
        if not applicable:
            continue
        env_h = envelope_hash_of(body)
        for patch in applicable:
            if patch.source_envelope_hash and patch.source_envelope_hash != env_h:
                patch.consecutive_replay_failures += 1
                continue
            items = _find_unit_list(body) or []
            patch_units = []
            for item in items:
                value = _navigate_json_path(item, patch.json_path)
                if value is None:
                    continue
                fv = FieldValue(
                    value=value,
                    source=SourceId.FIELD_PATCH,
                    confidence=patch.confidence * 0.85,    # patches start lower than live extraction
                    source_url=url,
                    envelope_hash=env_h,
                )
                patch_units.append(ProvenancedUnit({patch.field_name: fv}))
            if patch_units:
                patch.success_count += 1
                patch.consecutive_replay_failures = 0
                sources.append(ExtractedSource(
                    source_id=SourceId.FIELD_PATCH,
                    source_url=url,
                    envelope_hash=env_h,
                    units=patch_units,
                    has_unit_ids=False,
                    is_floor_plan_level=False,
                ))
            else:
                patch.consecutive_replay_failures += 1
    return sources
```

### Eviction (same threshold as Phase 6)

In `update_profile_after_extraction`:

```python
profile.api_hints.field_patches = [
    p for p in profile.api_hints.field_patches if p.consecutive_replay_failures < 3
]
```

### Named tests — `tests/profile/test_field_[patches.py](http://patches.py)`

| Test | Setup | Check |
|---|---|---|
| `test_recovery_emits_patch_record` | run `_run_null_field_recovery` | `scrape_result["_field_patches"]` has 1 entry |
| `test_patch_url_binds_to_source_response` | recovery against `raw_apis[1]` | patch's `api_url_pattern` matches `raw_apis[1].url`, NOT `raw_apis[0]` |
| `test_jsonpath_dollar_prefix_stripped` | LLM returned `source_path="$.bedroom_range"` | `json_path == "bedroom_range"` |
| `test_patch_replay_contributes_field_only` | patch replay produces ProvenancedUnit | unit has only the patched field |
| `test_patch_drift_detection` | patch hash A, body hash B | replay skipped, failures += 1 |
| `test_patch_eviction_at_3_failures` | failures=3 | removed by next update |
| `test_patch_does_not_replace_native_field` | adapter native unit_id 0.95 + patch unit_id at 0.6 | merger keeps adapter's |
| `test_recovery_e2e_no_llm_on_run2` | day 1 recovery → patch saved; day 2 same property | day 2 `_llm_interactions == []` |

### Phase 7 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 7` passes iff:
- All 8 tests pass
- E2E gate: pick 5 properties from a recent run with `field_recovery` LLM calls; re-run; LLM cost drops to zero for those 5
- Static scan: `grep -n "raw_apis\[0\]" ma_poc/scripts/jugnu_[runner.py](http://runner.py)` returns no hits in `_run_null_field_recovery`

---

## Phase 8 — DOM hints wiring + drift eviction

**Goal:** `extract_units_from_dom` reads `profile.dom_hints.field_selectors`. Hints contribute as a separate source. Empty result with hints present → emit `dom_hints.miss` + clear stale selectors after 3 consecutive misses. Estimated: ~150 LoC + 7 tests.

### File: `ma_poc/pms/adapters/_html_[extract.py](http://extract.py)`

Modify `extract_units_from_dom`:

```python
def extract_units_from_dom(
    html: str,
    base_url: str,
    hints: FieldSelectorMap | None = None,    # Phase 8
) -> list[dict]:
    """If `hints` provided and `hints.container` set, run hint-driven path FIRST.
    On miss, fall back to existing cascade. Returns same shape as before.
    """
    units: list[dict] = []
    if hints and hints.container:
        try:
            units = _extract_with_hints(html, base_url, hints)
        except Exception:
            units = []
    if not units:
        units = _extract_via_default_cascade(html, base_url)
    return units
```

Add a separate hint-only entry point so the adapter can record HINT-vs-CASCADE outcome distinctly:

```python
def extract_with_hints(
    html: str,
    base_url: str,
    hints: FieldSelectorMap,
) -> list[dict]:
    """Hint-only extraction. Returns [] if hints don't match.

    Used by Phase 8's _collect_dom_profile_hints to track hint hit/miss
    separately from cascade success.
    """
    if not hints.container:
        return []
    return _extract_with_hints(html, base_url, hints)
```

### `[generic.py](http://generic.py)::_collect_dom_profile_hints`

```python
def _collect_dom_profile_hints(self, html, profile, ctx):
    sources: list[ExtractedSource] = []
    hints = getattr(profile, "dom_hints", None)
    selectors = getattr(hints, "field_selectors", None) if hints else None
    if not selectors or not selectors.container:
        return sources

    try:
        from ma_poc.pms.adapters._html_extract import extract_with_hints
        units = extract_with_hints(html, ctx.base_url, selectors)
    except Exception:
        units = []

    if not units:
        miss_count = getattr(profile.dom_hints, "consecutive_misses", 0) + 1
        profile.dom_hints.consecutive_misses = miss_count
        _emit(EventKind.DOM_HINTS_MISS, [ctx.property](http://ctx.property)_id,
              container=str(selectors.container)[:60], misses=miss_count)
        if miss_count >= 3:
            profile.dom_hints.field_selectors = FieldSelectorMap()
            profile.dom_hints.consecutive_misses = 0
            _emit(EventKind.DOM_HINTS_EVICTED, [ctx.property](http://ctx.property)_id)
        return sources

    # Hint hit
    profile.dom_hints.consecutive_misses = 0
    env_h = envelope_hash_of(html[:5000])
    prov_units = [
        from_legacy_unit(u, SourceId.DOM_PROFILE_HINTS, ctx.base_url, env_h, 0.75)
        for u in units
    ]
    sources.append(ExtractedSource(
        source_id=SourceId.DOM_PROFILE_HINTS,
        source_url=ctx.base_url,
        envelope_hash=env_h,
        units=prov_units,
        has_unit_ids=any(u.get("unit_number") for u in units),
        is_floor_plan_level=False,
    ))
    return sources
```

### Schema addition

```python
class DomHints(BaseModel):
    ...
    consecutive_misses: int = 0
```

### Named tests — `tests/profile/test_dom_hints_[wiring.py](http://wiring.py)`

| Test | Setup | Check |
|---|---|---|
| `test_hint_hit_extracts_units` | profile with valid selectors, matching HTML | returns ExtractedSource with units |
| `test_hint_miss_increments_counter` | selectors don't match HTML | `consecutive_misses += 1` |
| `test_hint_eviction_at_3_misses` | misses=2, miss again | `field_selectors` cleared |
| `test_hint_hit_resets_misses` | misses=2, hit | reset to 0 |
| `test_no_hint_returns_no_source` | empty selectors | returns [] |
| `test_hints_dont_replace_cascade` | both DOM_PROFILE_HINTS and DOM_CASCADE produce units | merger picks higher-confidence (HINTS=0.75 > CASCADE=0.70) per field |
| `test_extract_units_from_dom_back_compat` | call without `hints` kwarg | identical to pre-Phase-8 behaviour |

### Phase 8 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 8` passes iff:
- All 7 tests pass
- E2E: 10 properties on TIER_4_LLM_DOM in last run with saved `field_selectors`. Re-run; ≥7 land on TIER_3_DOM or `TIER_MERGED_DETERMINISTIC`
- Static scan: `extract_units_from_dom` callers in `[generic.py](http://generic.py)` either pass `hints=` or use `extract_with_hints` directly

---

## Phase 9 — Cross-page merge in `[scraper.py](http://scraper.py)::_try_link_hop` (THE user-scenario phase)

**Goal:** `_try_link_hop` accumulates ALL sub-page sources rather than taking first hit. Main-page + sub-page sources go through merger. Replace destructive overwrite at `scraper.py:1130-1146` with merger call. Add `visited_urls` set to block link-hop cycles. Estimated: ~250 LoC + e2e fixture.

**This is the phase that delivers the user's explicit scenario.**

### `[scraper.py](http://scraper.py)::_try_link_hop` — current vs new

**Current behaviour** (paraphrased from your earlier dump): rank links → fetch top 3 → run `scrape()` on each → return the FIRST sub-result with units. Every other sub-result is discarded.

**New behaviour:**

```python
async def _try_link_hop(
    *,
    entry_url: str,
    entry_page_html: str,
    detected: DetectedPMS,
    profile: Any | None,
    expected_total_units: int | None,
    property_id: str,
    csv_row: dict | None,
    max_hops: int = 3,
    llm_navigation_hints: list[str] | None = None,
    visited_urls: set[str] | None = None,    # Phase 9
    target_field_group: str | None = None,    # Phase 9: from planner Decision
) -> dict[str, Any] | None:
    """Phase 9: accumulate ALL sub-page sources, do not first-hit-wins.

    Returns a merged scrape_result dict OR a `_units_empty` marker.
    """
    visited = visited_urls or set()
    visited.add(entry_url)

    candidates = _rank_links(entry_page_html, entry_url, detected, profile,
                             llm_navigation_hints=llm_navigation_hints,
                             target_field_group=target_field_group)
    candidates = [c for c in candidates if c.url not in visited][:max_hops]
    if not candidates:
        return None

    explored: dict[str, bool] = {}
    accumulated_sources: list[ExtractedSource] = []
    accumulated_telemetry: dict = {}

    for cand in candidates:
        if cand.url in visited:
            continue
        visited.add(cand.url)
        try:
            sub_fetch = await jugnu_fetch(make_task(cand.url))
            if not sub_fetch.ok():
                explored[cand.url] = False
                continue
            sub_result = await scrape(
                base_url=cand.url,
                profile=profile,
                expected_total_units=expected_total_units,
                page=None,
                fetch_result=sub_fetch,
                csv_row=csv_row,
                property_id=property_id,
            )
        except Exception as exc:
            log.warning("link-hop sub-scrape failed for %s: %s", cand.url, exc)
            explored[cand.url] = False
            continue

        sub_units = sub_result.get("units") or []
        explored[cand.url] = bool(sub_units)
        # Phase 9 KEY: accumulate sources, do NOT short-circuit on first hit
        sub_sources = sub_result.get("_sources") or []
        accumulated_sources.extend(sub_sources)
        # Capture telemetry from first sub-page that produced units, for tier label
        if sub_units and not accumulated_telemetry:
            accumulated_telemetry = {
                "_link_hop_from": entry_url,
                "_link_hop_depth": 1,
                "_link_hop_score": cand.score,
                "_link_hop_anchor": cand.anchor,
                "_winning_page_url": cand.url,
                "_raw_api_responses": sub_result.get("_raw_api_responses", []),
                "_llm_interactions": sub_result.get("_llm_interactions", []),
                "_llm_field_mappings": sub_result.get("_llm_field_mappings", []),
                "_llm_analysis_results": sub_result.get("_llm_analysis_results", {}),
                "_field_patches": sub_result.get("_field_patches", []),
            }
        else:
            # Append additional telemetry without overwriting
            accumulated_telemetry.setdefault("_raw_api_responses", []).extend(
                sub_result.get("_raw_api_responses", []))
            accumulated_telemetry.setdefault("_llm_interactions", []).extend(
                sub_result.get("_llm_interactions", []))
            accumulated_telemetry.setdefault("_llm_field_mappings", []).extend(
                sub_result.get("_llm_field_mappings", []))
            accumulated_telemetry.setdefault("_field_patches", []).extend(
                sub_result.get("_field_patches", []))

        # If we hit STOP-level completeness on accumulated sources, exit early
        if accumulated_sources:
            from ma_[poc.services](http://poc.services).source_merger import merge_sources
            from ma_[poc.services](http://poc.services).source_planner import evaluate_completeness
            merged_so_far = merge_sources(accumulated_sources, property_id)
            report_so_far = evaluate_completeness(merged_so_far)
            if report_so_far.pct_complete >= 0.90 and report_so_far.pct_with_transactional >= 0.70:
                break

    if not accumulated_sources:
        return {"_units_empty": True, "_explored_links": explored}

    # The caller (orchestrator) is responsible for merging accumulated_sources
    # with main-page sources. This function returns the accumulated sources
    # plus telemetry so the orchestrator can do a single merge.
    return {
        "_link_hop_sources": accumulated_sources,
        "_explored_links": explored,
        **accumulated_telemetry,
    }
```

### Orchestrator change in `scrape_jugnu` (lines 1130-1146)

Replace the destructive overwrite block. Current code copies sub-page extraction fields wholesale on top of entry-URL telemetry. New: merge sub-page sources WITH main-page sources, then re-finalize.

```python
# Replace lines 1126-1149
hop_result = await _try_link_hop(
    entry_url=base_url,
    entry_page_html=entry_html,
    detected=detected,
    profile=profile,
    expected_total_units=expected_total_units,
    property_id=property_id,
    csv_row=csv_row,
    max_hops=3,
    llm_navigation_hints=result.get("_llm_navigation_hints"),
    visited_urls={base_url},   # Phase 9: dedupe
    target_field_group=_extract_target_from_pending_decision(result),
)

if hop_result and hop_result.get("_link_hop_sources"):
    # Phase 9: merge accumulated sub-page sources with main-page sources
    main_sources = result.get("_sources") or []
    sub_sources = hop_result.get("_link_hop_sources") or []
    all_sources = list(main_sources) + list(sub_sources)

    from ma_[poc.services](http://poc.services).source_merger import merge_sources
    merged = merge_sources(all_sources, property_id, fuzzy_link_callback=_emit_fuzzy_event)

    from ma_poc.models.source import to_legacy_unit
    result["units"] = [to_legacy_unit(u) for u in merged]
    result["_sources"] = all_sources
    # Tier label: TIER_MERGED_CROSS_PAGE if any source contributed from sub-page
    has_subpage_contributor = any(s.source_url != base_url for s in all_sources if s.units)
    if has_subpage_contributor and result.get("units"):
        result["extraction_tier_used"] = "TIER_MERGED_CROSS_PAGE"

    # Telemetry — additive, never destructive
    for k in (
        "_raw_api_responses", "_llm_interactions", "_llm_field_mappings",
        "_llm_analysis_results", "_field_patches",
    ):
        if k in hop_result:
            existing = result.get(k)
            if isinstance(existing, list):
                existing.extend(hop_result[k] or [])
            elif isinstance(existing, dict):
                existing.update(hop_result[k] or {})
            else:
                result[k] = hop_result[k]
    for k in ("_link_hop_from", "_link_hop_depth", "_link_hop_score", "_link_hop_anchor",
              "_winning_page_url"):
        if k in hop_result and k not in result:
            result[k] = hop_result[k]
    result["_link_hop_success"] = True

elif hop_result and hop_result.get("_units_empty"):
    result["_explored_links"] = hop_result.get("_explored_links") or {}
```

### `_rank_links` accepts `target_field_group` (Phase 9 enhancement)

When the planner returned `target_field_group="transactional"`, the link ranker should boost candidates whose anchor/URL contains "availability", "available", "lease", "rent", "apply" — and de-prioritize floor-plan pages. Conversely for `target_field_group="physical"`, boost "floor-plan", "model", "layout", "unit-type". Use `target_field_group=None` to mean "use default ranking" (the current behaviour).

```python
TARGET_KEYWORDS: dict[str, list[str]] = {
    "transactional": ["availability", "available", "lease", "apply", "pricing", "rent"],
    "physical":      ["floor-plan", "floor_plan", "floorplan", "model", "layout", "unit-type", "apartments"],
    "identity":      ["units", "available-units"],
}
```

### Loop-safeguard tests (H4, H5 invariants)

```python
async def test_h4_detection_locks_after_capture():
    # Confirm that subsequent confirm_detection() calls within one scrape
    # don't change the routed adapter even if confidence shifts.
    ...

async def test_h5_link_hop_visited_dedupe():
    """Build a fixture: entry page links to /a, /a links to entry page.
    Run scrape_jugnu. Assert /a fetched at most once, entry URL never re-fetched
    by link-hop."""
    fetch_count: dict[str, int] = {}
    with patch("ma_poc.fetch.fetch", side_effect=count_fetches(fetch_count)):
        await scrape_jugnu(task=task_for(entry), fetch_result=initial_fetch, page=None)
    assert fetch_count[entry_url] == 1   # initial fetch only
    assert fetch_count["/a"] == 1

async def test_h5_link_hop_max_depth():
    """Ensure max_hops=3 is the cap; never more than 3 sub-pages fetched."""
    fetch_count = 0
    with patch_fetch_counter():
        await _try_link_hop(...)
    assert fetch_count <= 3
```

### THE user-scenario acceptance test — `tests/integration/test_cross_source_[e2e.py](http://e2e.py)::test_user_scenario_floor_plan_plus_availability`

```python
async def test_user_scenario_floor_plan_plus_availability(tmp_profile_dir):
    """The exact scenario the user described:
       - Floor plans page produces beds/baths/sqft, no rent/avail
       - Availability page produces unit_id/rent/avail, no beds/baths/sqft
       - Expected: merged output has both, deterministic only, zero LLM calls.
    """
    # Fixture: entry URL with two captured pages
    fixture = load_fixture("cross_source/floor_plan_plus_availability")
    # entry page: floor-plans.json captures
    # /availability sub-page: availability.json captures

    task = CrawlTask(property_id="cs_001", url=fixture.entry_url, ...)
    fetch_result = build_fetch_result(fixture.entry_html, fixture.captured_apis)

    result = await scrape_jugnu(
        task=task, fetch_result=fetch_result,
        page=None, profile=None, expected_total_units=2,
        csv_row=fixture.csv_row,
    )

    units = result["units"]
    assert len(units) == 2

    by_id = {u["unit_number"]: u for u in units}
    assert "305" in by_id
    assert "410" in by_id

    for unit_no in ("305", "410"):
        u = by_id[unit_no]
        assert u["floor_plan_name"] == "A1"          # from FP page
        assert u["bedrooms"] == "1"                   # from FP page
        assert u["bathrooms"] == "1"                  # from FP page
        assert u["sqft"] == "750"                     # from FP page
        assert u["market_rent_low"] is not None       # from availability page
        assert u["availability_date"]                  # from availability page

    # Provenance check
    assert "_provenance" in by_id["305"]
    prov = by_id["305"]["_provenance"]
    assert prov["floor_plan_name"]["source"].startswith("api_")
    assert "floor-plans" in prov["floor_plan_name"]["source_url"]
    assert prov["market_rent_low"]["source"].startswith("api_")
    assert "availability" in prov["market_rent_low"]["source_url"]

    # Verdict and tier
    assert result["_meta"]["verdict"] == "SUCCESS"
    assert result["extraction_tier_used"] == "TIER_MERGED_CROSS_PAGE"

    # COST: zero LLM calls — this is deterministic across both pages
    assert result["_extract_result"].llm_cost_usd == 0.0
    assert len(result.get("_llm_interactions", [])) == 0
```

### Phase 9 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 9` passes iff:
- `test_user_scenario_floor_plan_plus_availability` passes (THE acceptance test)
- H4, H5 tests pass
- Property-level: at least 5 properties from `data/runs/2026-04-25/` that previously had partial-coverage units now produce complete units (defined by `pct_complete>=0.90`)
- Static scan: `grep -nE "result\[\".*\"\] = hop_result\[" ma_poc/pms/[scraper.py](http://scraper.py)` returns NO hits (no destructive overwrites remaining)

### Fixture requirement

`tests/integration/fixtures/cross_source/floor_plan_plus_availability/`:
- `entry.html` — main page with link to `/availability`
- `floor_plans.json` — captured API response, floorplan-level
- `availability.json` — captured API response, unit-level
- `csv_row.json` — CSV metadata

These must be REAL captures from a fixture property (`data/runs/*/raw_api/` is the source). Synthetic dicts are not accepted.

---

## Phase 10 — Mapping self-validation on save + first-run cost cap

**Goal:** Refuse to save mappings whose replay produces fewer units than the LLM's original output. COLD properties get budget=1 LLM tier per run, escalating across runs. Estimated: ~150 LoC + 6 tests.

### Self-validation in `services/profile_[updater.py](http://updater.py)::save_llm_field_mapping`

When saving, immediately attempt replay against the same body. If quality is below threshold, lower `quality_score` (which the cascade then uses to demote in confidence ranking).

```python
def save_llm_field_mapping(
    profile: ScrapeProfile,
    mapping_dict: dict,
    source_envelope_hash: str = "",
    expected_unit_count: int | None = None,    # Phase 10 — set by caller from llm_units count
    body_for_validation: Any = None,            # Phase 10 — pass body to test replay
) -> bool:
    ...
    quality_score = 1.0
    if body_for_validation is not None and expected_unit_count is not None:
        from ma_[poc.services](http://poc.services).llm_extractor import apply_saved_mapping
        try:
            replayed = apply_saved_mapping(body_for_validation, {
                "response_envelope": mapping_dict.get("response_envelope", ""),
                "json_paths": json_paths,
            }) or []
        except Exception:
            replayed = []
        if expected_unit_count > 0:
            ratio = len(replayed) / expected_unit_count
            if ratio < 0.8:
                # Demote — record the mapping but at lower quality
                quality_score = max(0.4, ratio)
                log.warning(
                    "Mapping for %s saved at quality_score=%.2f (replay produced %d/%d units)",
                    url_pattern[:80], quality_score, len(replayed), expected_unit_count,
                )

    profile.api_hints.llm_field_mappings.append(LlmFieldMapping(
        api_url_pattern=url_pattern,
        json_paths=json_paths,
        response_envelope=mapping_dict.get("response_envelope", ""),
        source_envelope_hash=source_envelope_hash,
        quality_score=quality_score,
    ))
    ...
```

The cascade's replay confidence (Phase 6) is already `0.85 * mapping.quality_score`, so a mapping at `quality_score=0.5` contributes at confidence 0.425 — below the physical floor of 0.50, effectively retired without being deleted.

### First-run cost cap — `services/source_[planner.py](http://planner.py)::compute_budget`

```python
def compute_budget(
    profile: ScrapeProfile,
    is_cold: bool,
) -> dict[str, int]:
    """Per-property LLM budget for this run.

    HOT/WARM properties get full budget. COLD properties get 1 LLM tier per
    run, with the type rotating across runs to avoid getting stuck on the
    same failing path:
      - cold_run_count % 3 == 0: llm_targeted only
      - cold_run_count % 3 == 1: link_hop only
      - cold_run_count % 3 == 2: llm_monolithic only
    """
    if not is_cold:
        return {"llm_targeted": 1, "llm_monolithic": 1, "link_hop": 3}

    n = profile.confidence.cold_run_count or 0
    if n % 3 == 0:
        return {"llm_targeted": 1, "llm_monolithic": 0, "link_hop": 1}
    if n % 3 == 1:
        return {"llm_targeted": 0, "llm_monolithic": 0, "link_hop": 3}
    return {"llm_targeted": 0, "llm_monolithic": 1, "link_hop": 1}
```

### Schema addition

```python
class ExtractionConfidence(BaseModel):
    ...
    cold_run_count: int = 0    # Phase 10 — tracks COLD-property LLM tier rotation
```

In `update_profile_after_extraction`, increment `cold_run_count` for COLD profiles:

```python
if profile.confidence.maturity == ProfileMaturity.COLD:
    profile.confidence.cold_run_count += 1
else:
    profile.confidence.cold_run_count = 0    # reset when promoted out of COLD
```

### Named tests — `tests/profile/test_self_[validation.py](http://validation.py)`

| Test | Setup | Check |
|---|---|---|
| `test_full_replay_keeps_quality_score_1` | LLM produced 5 units; replay also produces 5 | `quality_score == 1.0` |
| `test_partial_replay_demotes_quality_score` | LLM produced 5 units; replay produces 3 | `quality_score == 0.6` |
| `test_zero_replay_minimum_quality_score` | replay produces 0 of 5 | `quality_score == 0.4` (floor) |
| `test_low_quality_below_floor_excluded_from_merge` | mapping with `quality_score=0.5`, confidence becomes `0.85*0.5=0.425` | unit fields below physical floor (0.5) → not in merge output |
| `test_cold_budget_rotates` | profile with `cold_run_count=0` | budget allows llm_targeted; run again with cold_run_count=1 → budget allows link_hop only |
| `test_warm_budget_full` | profile WARM | budget allows all three |

### Phase 10 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 10` passes iff:
- All 6 tests pass
- E2E: pick a fresh COLD property; run 3 times back-to-back; assert exactly 1 LLM tier ran in each run; total cost across 3 runs ≤ 3× single-run cap

---

## Phase 11 — Profile-learned source preferences

**Goal:** Every merge writes `SourceObservation` deltas to the profile. The planner reads them on next run, floats observed-winning sources to top of ranking. Self-learning loop closure for sources. Estimated: ~150 LoC + 5 tests.

### Schema additions

```python
# ma_poc/models/scrape_[profile.py](http://profile.py)
class SourceObservation(BaseModel):
    """Per-source telemetry — how often this source contributed the winning value."""
    model_config = ConfigDict(extra="ignore")
    source_id: str    # SourceId.value (string form, since it's a closed enum)
    field_group: str  # "identity" | "physical" | "transactional"
    contribution_count: int = 0
    last_contributed_at: datetime | None = None
    avg_confidence_when_won: float = 0.0
    consecutive_failures: int = 0    # contributed nothing this run

class ApiHints(BaseModel):
    ...
    # Phase 11
    source_observations: list[SourceObservation] = Field(default_factory=list)

    @field_validator("source_observations", mode="before")
    @classmethod
    def cap_source_observations(cls, v):
        # Cap at 20 entries (3 field_groups x ~7 sources max in practice)
        if isinstance(v, list) and len(v) > 20:
            return v[-20:]
        return v
```

### Recording deltas — `services/source_[observer.py](http://observer.py)` (new)

```python
"""
Records SourceObservation deltas after every merge.

Pure, side-effect-free until the final list-mutation step on the profile.
Called once per scrape, after the final merge has been computed.
"""

from collections import defaultdict
from datetime import datetime
from ma_poc.models.source import SourceId, FieldValue
from ma_[poc.services](http://poc.services).source_planner import FIELD_GROUP


def record_source_observations(
    profile,
    merged_units: list,
) -> None:
    """For each field in each merged unit, identify which source won and bump
    its SourceObservation. Best-effort — never raises.
    """
    try:
        # Aggregate wins: source_id -> field_group -> (count, sum_confidence)
        wins: dict[tuple[str, str], list[float]] = defaultdict(list)
        for unit in merged_units:
            for field_name, fv in unit.items():
                if not isinstance(fv, FieldValue):
                    continue
                group = FIELD_GROUP.get(field_name)
                if group is None:
                    continue
                key = (fv.source.value, group)
                wins[key].append(fv.confidence)

        # Upsert observations
        existing = {(o.source_id, o.field_group): o for o in profile.api_hints.source_observations}
        now = datetime.utcnow()
        for (source_id, group), confidences in wins.items():
            obs = existing.get((source_id, group))
            if obs is None:
                obs = SourceObservation(source_id=source_id, field_group=group)
                profile.api_hints.source_observations.append(obs)
            obs.contribution_count += len(confidences)
            obs.last_contributed_at = now
            # Running avg confidence
            n_prior = obs.contribution_count - len(confidences)
            new_avg = (obs.avg_confidence_when_won * n_prior + sum(confidences)) / max(obs.contribution_count, 1)
            obs.avg_confidence_when_won = new_avg
            obs.consecutive_failures = 0

        # Increment failures on observations whose source ran but contributed nothing
        # (caller passes sources_run_this_scrape via profile.confidence.last_sources_run)
        sources_run = getattr(profile.confidence, "last_sources_run", []) or []
        for source_id in sources_run:
            for group in ("identity", "physical", "transactional"):
                key = (source_id, group)
                if key in wins:
                    continue
                obs = existing.get(key)
                if obs is not None:
                    obs.consecutive_failures += 1

        # Cap (defensive — schema validator also caps)
        if len(profile.api_hints.source_observations) > 20:
            profile.api_hints.source_observations = profile.api_hints.source_observations[-20:]
    except Exception as exc:
        log.warning("record_source_observations failed: %s", exc)
```

### Use in `services/profile_[updater.py](http://updater.py)::update_profile_after_extraction`

After Phase 7's `field_patches` save, before final eviction:

```python
# Phase 11: record source observations
sources = scrape_result.get("_sources") or []
profile.confidence.last_sources_run = list({s.source_id.value for s in sources})

merged_provenanced = scrape_result.get("_merged_provenanced") or []
if merged_provenanced:
    from ma_[poc.services](http://poc.services).source_observer import record_source_observations
    record_source_observations(profile, merged_provenanced)
```

(`_merged_provenanced` is the in-memory merged list — needs to be threaded through `_finalize` in Phase 5 / `scrape_jugnu` in Phase 9. Add to legacy result dict alongside `_sources`.)

### Reading prefs in `services/source_[planner.py](http://planner.py)::rank_sources_for_field_group`

```python
def rank_sources_for_field_group(
    field_group: str,
    pms_name: str,
    profile_preferences: list[SourceObservation] | None = None,
) -> list[tuple[SourceId, float]]:
    default = list(DEFAULT_SOURCE_RANKING.get(field_group, []))
    if not profile_preferences:
        return default

    # Filter prefs to this field_group, sort by contribution_count desc
    prefs_for_group = [p for p in profile_preferences if p.field_group == field_group]
    if not prefs_for_group:
        return default
    prefs_for_group.sort(key=lambda p: p.contribution_count, reverse=True)

    # Float observed winners to the top, preserving their relative order;
    # then append any default-ranked sources not already promoted.
    observed_ids = []
    for p in prefs_for_group:
        try:
            sid = SourceId(p.source_id)
        except ValueError:
            continue    # stale source_id from older schema, ignore
        observed_ids.append(sid)

    promoted: list[tuple[SourceId, float]] = []
    seen: set[SourceId] = set()
    for sid in observed_ids:
        # Find the source's base confidence in the default ranking; if not present, default 0.7
        base_conf = next((c for s, c in default if s == sid), 0.7)
        promoted.append((sid, base_conf))
        seen.add(sid)
    for sid, conf in default:
        if sid not in seen:
            promoted.append((sid, conf))
    return promoted
```

### Named tests — `tests/profile/test_source_[observations.py](http://observations.py)`

| Test | Setup | Check |
|---|---|---|
| `test_observation_recorded_for_winning_source` | merged unit has FIELD_PATCH winning rent_low | observation `(field_patch, transactional)` has `contribution_count=1` |
| `test_observation_avg_confidence_running` | 2 wins at 0.8 and 0.6 | `avg_confidence_when_won == 0.7` |
| `test_consecutive_failures_increments_when_no_win` | source ran but contributed nothing | observation `consecutive_failures += 1` |
| `test_planner_floats_observed_winners` | profile shows FIELD_PATCH won 5x in transactional | rank_sources puts FIELD_PATCH at position 0 in transactional ranking |
| `test_observations_capped_at_20` | profile has 25 SourceObservations | persistence keeps only most-recent 20 |

### Phase 11 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 11` passes iff:
- All 5 tests pass
- E2E: 100 properties run twice. Second run's planner ranking diverges from default for at least 30% of properties (signal that profile prefs are taking effect)
- Cap test: any single profile in fixture corpus has ≤20 source_observations entries after multiple runs

---

## Phase 12 — Cluster bootstrap from `client_account_id`

**Goal:** On COLD property, query similar properties (same PMS client_account_id) for high-replay-count mappings; try them BEFORE any LLM call. Highest-leverage savings at 50K scale. Estimated: ~250 LoC + 6 tests.

### Populator step in detector

`pms/[detector.py](http://detector.py)::confirm_detection` already extracts `pms_client_account_id` for some PMSes. Phase 12 adds a step in the orchestrator (`scrape_jugnu` in `[scraper.py](http://scraper.py)`) to populate `profile.cluster_key` from this value:

```python
# In scrape_jugnu, after detection:
detected_pms = result.get("_detected_pms", {})
client_account_id = detected_pms.get("pms_client_account_id")
if client_account_id and profile is not None:
    profile.cluster_key = client_account_id
```

`profile.cluster_key` is a top-level field on `ScrapeProfile` (already exists per audit findings — empty for greenfield work).

### Cluster store query — `services/cluster_[store.py](http://store.py)` (new)

```python
"""
Cluster lookup: find HOT profiles sharing a client_account_id.
"""

from __future__ import annotations
from typing import Iterable
from ma_poc.models.scrape_profile import ScrapeProfile, LlmFieldMapping, ProfileMaturity


def find_cluster_mates(
    store,                          # ProfileStore
    cluster_key: str,
    self_property_id: str,
    max_mates: int = 5,
) -> list[ScrapeProfile]:
    """Returns up to N HOT profiles with same cluster_key, excluding self."""
    if not cluster_key:
        return []
    mates: list[ScrapeProfile] = []
    for prof in store.iter_profiles_by_cluster_key(cluster_key):
        if prof.canonical_id == self_property_id:
            continue
        if prof.confidence.maturity != [ProfileMaturity.HOT](http://ProfileMaturity.HOT):
            continue
        mates.append(prof)
        if len(mates) >= max_mates:
            break
    return mates


def collect_top_cluster_mappings(
    mates: list[ScrapeProfile],
    min_success_count: int = 3,
) -> list[LlmFieldMapping]:
    """Aggregate mappings across mates, sort by total success_count desc.

    Dedup by api_url_pattern: if two mates have the same pattern, take the one
    with higher success_count. Returns top mappings (caller decides cap).
    """
    by_pattern: dict[str, LlmFieldMapping] = {}
    for mate in mates:
        for m in mate.api_hints.llm_field_mappings:
            if m.success_count < min_success_count:
                continue
            existing = by_pattern.get(m.api_url_pattern)
            if existing is None or m.success_count > existing.success_count:
                by_pattern[m.api_url_pattern] = m
    out = sorted(by_pattern.values(), key=lambda m: m.success_count, reverse=True)
    return out
```

### `ProfileStore.iter_profiles_by_cluster_key` (new)

Add to `services/profile_[store.py](http://store.py)`. Implementation depends on backend (SQL/JSON/etc):

```python
def iter_profiles_by_cluster_key(self, cluster_key: str) -> Iterable[ScrapeProfile]:
    """Yields HOT profiles matching cluster_key. Bounded query (LIMIT 100)."""
    # SQL: SELECT * FROM scrape_profiles WHERE cluster_key=? AND maturity='HOT' LIMIT 100
    # JSON-backed: scan all profiles, filter
    ...
```

### New cascade source — `_collect_cluster_mapping_replay` in `[generic.py](http://generic.py)`

```python
def _collect_cluster_mapping_replay(self, api_responses, profile, ctx):
    """Phase 12: try mappings from cluster-mate properties.

    Only fires when:
      - profile.cluster_key is non-empty
      - profile.confidence.maturity == COLD
      - main profile's mapping_replay produced no hits
    """
    if not getattr(profile, "cluster_key", None):
        return []
    if profile.confidence.maturity != ProfileMaturity.COLD:
        return []

    from ma_[poc.services](http://poc.services).cluster_store import find_cluster_mates, collect_top_cluster_mappings
    mates = find_cluster_mates(ctx.profile_store, profile.cluster_key, profile.canonical_id, max_mates=5)
    if not mates:
        return []

    candidates = collect_top_cluster_mappings(mates, min_success_count=3)[:10]
    if not candidates:
        return []

    sources: list[ExtractedSource] = []
    from ma_[poc.services](http://poc.services).llm_extractor import apply_saved_mapping
    from ma_poc.models.source import envelope_hash_of, ExtractedSource, SourceId, from_legacy_unit

    for mapping in candidates:
        for resp in api_responses:
            url = resp.get("url", "")
            body = resp.get("body")
            if mapping.api_url_pattern not in url:
                continue
            try:
                units = apply_saved_mapping(body, {
                    "response_envelope": mapping.response_envelope,
                    "json_paths": mapping.json_paths,
                }) or []
            except Exception:
                continue
            if not units:
                continue
            env_h = envelope_hash_of(body)
            prov_units = [
                from_legacy_unit(u, SourceId.CLUSTER_MAPPING_REPLAY, url, env_h, 0.75)
                # 0.75 — lower than MAPPING_REPLAY (0.85) because cluster-mate is less specific
                for u in units
            ]
            sources.append(ExtractedSource(
                source_id=SourceId.CLUSTER_MAPPING_REPLAY,
                source_url=url,
                envelope_hash=env_h,
                units=prov_units,
                has_unit_ids=any(u.get("unit_number") for u in units),
                is_floor_plan_level=False,
            ))
            _emit(EventKind.CLUSTER_MAPPING_HIT, [ctx.property](http://ctx.property)_id,
                  pattern=mapping.api_url_pattern[:80],
                  source_property=mapping.source_property if hasattr(mapping, "source_property") else "?")
            break    # one match per mapping is enough

    return sources
```

### Add `CLUSTER_MAPPING_REPLAY` to ranking tables (Phase 12 update to §3.3)

In all three ranking groups, just below `MAPPING_REPLAY`:

```python
"physical": [
    ...
    (SourceId.MAPPING_REPLAY, 0.90),
    (SourceId.CLUSTER_MAPPING_REPLAY, 0.75),    # Phase 12 — cluster-mate
    ...
],
```

### Hook into Phase 5 cascade

Insert `_collect_cluster_mapping_replay` AFTER `_collect_mapping_replay` in `[generic.py](http://generic.py)`:

```python
sources.extend(self._collect_mapping_replay(api_responses, profile, ctx))
sources.extend(self._collect_cluster_mapping_replay(api_responses, profile, ctx))    # Phase 12
sources.extend(self._collect_field_patches(api_responses, profile, ctx))
sources.extend(self._collect_api_narrow(api_responses))
...
```

### Named tests — `tests/profile/test_cluster_[bootstrap.py](http://bootstrap.py)`

| Test | Setup | Check |
|---|---|---|
| `test_cluster_lookup_returns_only_hot_mates` | 3 profiles same cluster_key (1 HOT, 1 WARM, 1 COLD) | `find_cluster_mates` returns only the HOT one |
| `test_cluster_excludes_self` | self profile + 2 mates | `find_cluster_mates` returns 2, not 3 |
| `test_cluster_mapping_dedup_by_pattern` | 3 mates with same `api_url_pattern` (success_counts 5/8/3) | `collect_top_cluster_mappings` returns 1, the success_count=8 one |
| `test_cluster_replay_only_for_cold` | WARM profile with cluster_key | `_collect_cluster_mapping_replay` returns empty |
| `test_cluster_replay_lower_confidence_than_local` | both local mapping (0.85) and cluster mapping (0.75) replay successfully | merger picks local fields |
| `test_cluster_no_cluster_key_returns_empty` | profile without `cluster_key` | returns empty |

### Phase 12 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 12` passes iff:
- All 6 tests pass
- E2E: a property cluster of 10+ HOT properties (e.g., a Brookfield site with shared backend). Onboard one new COLD property; first-run LLM cost should be near-zero (cluster mappings cover the deterministic API)
- Static scan: `iter_profiles_by_cluster_key` is the ONLY query path that scans by cluster_key — no inline scanning elsewhere

---

## Phase 13 — SLO + per-source telemetry

**Goal:** New event kinds, dashboards, source-contribution metrics. Without these, regressions in Phases 1-12 land silently. Estimated: ~120 LoC + 4 tests.

### New event kinds — `ma_poc/observability/[events.py](http://events.py)`

```python
class EventKind(StrEnum):
    ...
    # Phase 6 / 7 / 8
    MAPPING_DRIFT_DETECTED        = "mapping.drift_detected"
    MAPPING_REPLAY_EMPTY          = "mapping.replay_empty"
    DOM_HINTS_MISS                = "dom_hints.miss"
    DOM_HINTS_EVICTED             = "dom_hints.evicted"
    FIELD_PATCH_HIT               = "field_patch.hit"
    FIELD_PATCH_DRIFT             = "field_patch.drift"
    # Phase 5 / 9
    IDENTITY_FUZZY_LINK           = "identity.fuzzy_link"
    PLANNER_DECISION              = "planner.decision"
    SOURCE_CONTRIBUTED            = "source.contributed"
    SOURCES_MERGED                = "sources.merged"
    # Phase 12
    CLUSTER_MAPPING_HIT           = "cluster.mapping_hit"
    # Phase 13 — periodic
    SLO_REPORT                    = "[slo.report](http://slo.report)"
```

Every emission point already implemented in earlier phases. Phase 13 just locks them in the enum and adds the `SOURCES_MERGED` summary event:

```python
# Add to _finalize() in [generic.py](http://generic.py)
_emit(EventKind.SOURCES_MERGED, [ctx.property](http://ctx.property)_id,
      n_sources=len(sources),
      n_units=len(merged_units),
      pct_complete=report.pct_complete,
      sources_used=[s.source_id.value for s in sources if s.units],
      tier_label=result.tier_used)
```

### File: `ma_poc/observability/source_[telemetry.py](http://telemetry.py)` (new)

```python
"""
SLO computations from event stream.

Reads events.jsonl (or queries event store), produces per-source contribution
rates, eviction rates, fuzzy-link rates, gate trigger distribution.
"""

from collections import Counter
from dataclasses import dataclass, field

@dataclass
class SourceContributionReport:
    n_runs: int
    contribution_rates: dict[str, float] = field(default_factory=dict)    # source_id -> fraction of runs where it contributed
    eviction_rates: dict[str, float] = field(default_factory=dict)        # mapping/patch/dom_hints
    fuzzy_link_rate: float = 0.0
    planner_decision_distribution: dict[str, float] = field(default_factory=dict)
    cluster_hit_rate: float = 0.0
    avg_pct_complete: float = 0.0


def compute_slo_report(events_iter) -> SourceContributionReport:
    """Aggregate over a window of SOURCES_MERGED + drift/eviction events."""
    n_runs = 0
    source_appearances = Counter()
    mapping_drift = 0
    mapping_evictions = 0
    fuzzy_links = 0
    decision_counts = Counter()
    cluster_hits = 0
    pct_complete_sum = 0.0

    for ev in events_iter:
        if ev.kind == "sources.merged":
            n_runs += 1
            for s in [ev.data](http://ev.data).get("sources_used", []):
                source_appearances[s] += 1
            pct_complete_sum += [ev.data](http://ev.data).get("pct_complete", 0.0)
        elif ev.kind == "mapping.drift_detected":
            mapping_drift += 1
        elif ev.kind == "identity.fuzzy_link":
            fuzzy_links += 1
        elif ev.kind == "planner.decision":
            decision_counts[[ev.data](http://ev.data).get("action", "?")] += 1
        elif ev.kind == "cluster.mapping_hit":
            cluster_hits += 1

    if n_runs == 0:
        return SourceContributionReport(n_runs=0)

    return SourceContributionReport(
        n_runs=n_runs,
        contribution_rates={src: count / n_runs for src, count in source_appearances.items()},
        eviction_rates={
            "mapping_drift_per_run": mapping_drift / n_runs,
            "fuzzy_link_per_run": fuzzy_links / n_runs,
        },
        fuzzy_link_rate=fuzzy_links / n_runs,
        planner_decision_distribution={a: c / max(sum(decision_counts.values()), 1) for a, c in decision_counts.items()},
        cluster_hit_rate=cluster_hits / n_runs,
        avg_pct_complete=pct_complete_sum / n_runs,
    )
```

### Dashboards (downstream — out of scope for Claude Code)

Wire `compute_slo_report` to whatever dashboarding exists (Grafana/Looker/etc). Out of code scope for this plan; just expose the report function.

### Named tests — `tests/observability/test_source_[telemetry.py](http://telemetry.py)`

| Test | Setup | Check |
|---|---|---|
| `test_empty_events_returns_zero_report` | empty iterator | `n_runs=0`, all rates 0.0 |
| `test_contribution_rates_computed` | 10 runs, FIELD_PATCH appeared in 7 | `contribution_rates["field_patch"] == 0.7` |
| `test_planner_decision_distribution_normalized` | 10 STOPs, 5 ESCALATEs | distribution sums to 1.0 |
| `test_fuzzy_link_rate_per_run` | 100 runs, 5 fuzzy links | `fuzzy_link_rate == 0.05` |

### Phase 13 gate

`scripts/gate_[xsource.py](http://xsource.py) phase 13` passes iff:
- All 4 tests pass
- E2E: emit `SOURCES_MERGED` from a 50-property fixture run; `compute_slo_report` over the resulting events produces non-zero contribution rates for at least 3 source IDs

---

## 11. Cross-cutting tests (the safety net)

These tests live in `tests/integration/test_loop_[safeguards.py](http://safeguards.py)` and `tests/integration/test_cross_source_[e2e.py](http://e2e.py)`. **Every gate from Phase 5 onward must run them.** No phase passes without all of them green.

### 11.1 Loop-safeguard tests (H1-H5)

```python
# tests/integration/test_loop_[safeguards.py](http://safeguards.py)

# H1: merger purity
def test_merger_pure_function():
    sources = build_test_sources_from_fixture("rentcafe/35593_2026-04-14")
    out_a = merge_sources(sources, "test_prop")
    out_b = merge_sources(sources, "test_prop")
    assert out_a == out_b
    assert all(s.units == original_units(s) for s in sources)

# H2: at most one planner call per page
async def test_one_planner_call_per_page():
    with patch("ma_[poc.services](http://poc.services).source_planner.plan_next_action",
               wraps=plan_next_action) as planner:
        await GenericAdapter().extract(page=fixture_page, ctx=fixture_ctx)
    assert [planner.call](http://planner.call)_count <= 1

# H3: LLM budget caps
async def test_llm_budget_caps():
    counts = await collect_llm_call_counts_for_fixture("low_completeness")
    assert counts.targeted <= 1
    assert counts.monolithic <= 1
    assert not (counts.targeted == 1 and counts.monolithic == 1)

# H4: detection lock
async def test_detection_locks_after_capture():
    """Confirm that subsequent confirm_detection() calls don't change adapter routing."""
    adapter_calls = []
    with patch("ma_poc.pms.adapters.registry.get_adapter",
               side_effect=track_calls(adapter_calls)):
        await scrape_jugnu(task=task, fetch_result=fr, page=None, profile=None)
    # Should have exactly one adapter type used per scrape
    adapter_types = {c.pms_name for c in adapter_calls}
    assert len(adapter_types) <= 2    # main + generic_fallback at most

# H5: link-hop dedupe + max-depth
async def test_link_hop_visited_dedupe():
    fetch_count: dict[str, int] = {}
    with patch_fetch_counter(fetch_count):
        await scrape_jugnu(task=task_for(entry_url),
                           fetch_result=initial_fetch, page=None, profile=None)
    assert fetch_count[entry_url] == 1    # initial only

async def test_link_hop_max_depth():
    fetch_count = 0
    with patch_fetch_counter_global() as ctr:
        await _try_link_hop(entry_url=fixture_entry, ..., max_hops=3, visited_urls={fixture_entry})
    assert ctr.value <= 3
```

### 11.2 The user-scenario acceptance test

`tests/integration/test_cross_source_[e2e.py](http://e2e.py)::test_user_scenario_floor_plan_plus_availability` — see Phase 9 for the full body. Required fixture: `tests/integration/fixtures/cross_source/floor_plan_plus_availability/`.

### 11.3 No-regression gate

Every phase from Phase 5 onward MUST run, in addition to its own tests:

```bash
pytest tests/pms/adapters/ -v                  # all adapter snapshots
pytest tests/integration/test_loop_[safeguards.py](http://safeguards.py) -v
pytest tests/integration/test_cross_source_[e2e.py](http://e2e.py) -v
```

If any of these fails, the phase does not advance.

---

## 12. Gate runner — `scripts/gate_[xsource.py](http://xsource.py)`

Mirrors `scripts/gate_[refactor.py](http://refactor.py)`. Single entry point; per-phase command.

```python
#!/usr/bin/env python3
"""Cross-source + self-learning gate runner.

Usage:
    scripts/gate_[xsource.py](http://xsource.py) phase <N>
    scripts/gate_[xsource.py](http://xsource.py) all
"""
from __future__ import annotations
import argparse
import subprocess
import sys
from pathlib import Path

PHASE_TESTS: dict[int, list[str]] = {
    1: ["tests/profile/test_phase1_[writers.py](http://writers.py)"],
    2: ["tests/services/test_source_[primitive.py](http://primitive.py)", "tests/pms/adapters/"],
    3: ["tests/services/test_source_[merger.py](http://merger.py)"],
    4: ["tests/services/test_source_[planner.py](http://planner.py)"],
    5: [
        "tests/integration/test_loop_[safeguards.py](http://safeguards.py)",
        "tests/pms/adapters/",
        "tests/integration/test_cross_source_[e2e.py](http://e2e.py)::test_user_scenario_floor_plan_plus_availability",
    ],
    6: ["tests/profile/test_mapping_[eviction.py](http://eviction.py)"],
    7: ["tests/profile/test_field_[patches.py](http://patches.py)"],
    8: ["tests/profile/test_dom_hints_[wiring.py](http://wiring.py)"],
    9: [
        "tests/integration/test_loop_[safeguards.py](http://safeguards.py)",
        "tests/integration/test_cross_source_[e2e.py](http://e2e.py)",
    ],
    10: ["tests/profile/test_self_[validation.py](http://validation.py)"],
    11: ["tests/profile/test_source_[observations.py](http://observations.py)"],
    12: ["tests/profile/test_cluster_[bootstrap.py](http://bootstrap.py)"],
    13: ["tests/observability/test_source_[telemetry.py](http://telemetry.py)"],
}

# Static-scan checks per phase. Each returns True on pass.
STATIC_SCANS: dict[int, list[tuple[str, str]]] = {
    1: [
        ("stats_writers_present",
         r'grep -nE "stats\.total_(scrapes|successes|failures|llm_calls)\s*\+=" ma_poc/services/profile_[updater.py](http://updater.py) | wc -l'),
    ],
    2: [
        ("source_id_enum_closed",
         r'grep -rE \'"(api_[a-z_]+|llm_[a-z_]+|dom_[a-z_]+|mapping_replay|field_patch|cluster_)"\' ma_poc/services/ ma_poc/pms/'
         r' | grep -v "models/[source.py](http://source.py)" | grep -v "services/source_[planner.py](http://planner.py)" | wc -l'),
    ],
    3: [
        ("merger_purity",
         r'grep -nE "(import logging|log\.|profile\.)" ma_poc/services/source_[merger.py](http://merger.py) | wc -l'),
    ],
    7: [
        ("no_raw_apis_index_zero",
         r'grep -n "raw_apis\[0\]" ma_poc/scripts/jugnu_[runner.py](http://runner.py) | grep "_run_null_field_recovery" | wc -l'),
    ],
    9: [
        ("no_destructive_overwrite",
         r'grep -nE \'result\["[^"]+"\] = hop_result\[\' ma_poc/pms/[scraper.py](http://scraper.py) | wc -l'),
    ],
}

# Expected static-scan results (zero or non-zero based on what we WANT)
STATIC_SCAN_EXPECTATIONS: dict[tuple[int, str], int] = {
    (1, "stats_writers_present"): 4,        # at least 4 hits
    (2, "source_id_enum_closed"): 0,        # zero hits outside enum module
    (3, "merger_purity"): 0,                # zero impurity calls
    (7, "no_raw_apis_index_zero"): 0,       # zero hits in null_field_recovery
    (9, "no_destructive_overwrite"): 0,     # zero destructive overwrites
}


def run_phase(phase: int) -> int:
    """Returns 0 on pass, non-zero on fail."""
    print(f"=== Phase {phase} gate ===")

    # 1. Run pytest
    tests = PHASE_TESTS.get(phase, [])
    if tests:
        cmd = ["pytest", "-v", *tests, "--ignore=data", "--ignore=config"]
        rc = [subprocess.run](http://subprocess.run)(cmd).returncode
        if rc != 0:
            print(f"FAIL: pytest returned {rc}")
            return rc

    # 2. Run static scans
    scans = STATIC_SCANS.get(phase, [])
    for name, command in scans:
        result = [subprocess.run](http://subprocess.run)(command, shell=True, capture_output=True, text=True)
        try:
            count = int(result.stdout.strip())
        except ValueError:
            count = 0
        expected = STATIC_SCAN_EXPECTATIONS.get((phase, name))
        if expected is None:
            continue
        if expected == 0 and count != 0:
            print(f"FAIL: static scan {name} expected 0 hits, got {count}")
            return 2
        if expected > 0 and count < expected:
            print(f"FAIL: static scan {name} expected ≥{expected} hits, got {count}")
            return 3
        print(f"  PASS: {name} (count={count}, expected={expected})")

    # 3. Cross-cutting tests on Phase 5+
    if phase >= 5:
        rc = [subprocess.run](http://subprocess.run)([
            "pytest", "-v",
            "tests/integration/test_loop_[safeguards.py](http://safeguards.py)",
        ]).returncode
        if rc != 0:
            print(f"FAIL: cross-cutting loop-safeguard tests returned {rc}")
            return rc

    print(f"=== Phase {phase} gate: PASS ===")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("phase")
    p.add_argument("number", type=int)
    sub.add_parser("all")

    args = parser.parse_args()

    if args.cmd == "all":
        for n in range(1, 14):
            rc = run_phase(n)
            if rc != 0:
                return rc
        return 0
    return run_phase(args.number)


if __name__ == "__main__":
    sys.exit(main())
```

---

## 13. Migration runbook

`scripts/migrate_profiles_[xsource.py](http://xsource.py)`:

```python
"""One-shot migration to add Phase 6/7/10/11/12 schema fields to existing profiles.

Run BEFORE starting Phase 6 in production. Idempotent — safe to re-run.

Adds defaults (no behavior change until cascade reads them):
  - LlmFieldMapping.consecutive_replay_failures = 0
  - LlmFieldMapping.last_replayed_at = None
  - LlmFieldMapping.source_envelope_hash = ""
  - LlmFieldMapping.quality_score = 1.0
  - ApiHints.field_patches = []                          (Phase 7)
  - ApiHints.source_observations = []                    (Phase 11)
  - DomHints.consecutive_misses = 0                      (Phase 8)
  - ExtractionConfidence.cold_run_count = 0              (Phase 10)
  - ExtractionConfidence.last_sources_run = []           (Phase 11)
  - ScrapeProfile.cluster_key = ""                       (Phase 12 — populator runs separately)
"""
```

Migration must be idempotent and never delete fields. The Pydantic v2 schema with `extra="ignore"` is forward-compatible — existing profiles load with defaults.

---

## 14. Appendix: tier label evolution

| Pre-plan | After this plan |
|---|---|
| `TIER_1_API_RENTCAFE` | unchanged when only RentCafe API contributed |
| `TIER_1_API_RENTCAFE_NO_RESPONSE` | unchanged (failure stamp from Phase 0) |
| `TIER_1_PROFILE_MAPPING` | unchanged when MAPPING_REPLAY was sole contributor; supplanted by `TIER_MERGED_*` when merged with adapter |
| `TIER_4_LLM_API` | unchanged when LLM_API_TARGETED was sole contributor; `TIER_MERGED_HYBRID` when merged with deterministic |
| (new) `TIER_MERGED_DETERMINISTIC` | multiple deterministic sources merged, no LLM |
| (new) `TIER_MERGED_HYBRID` | deterministic + LLM-targeted merged |
| (new) `TIER_MERGED_CROSS_PAGE` | main page + ≥1 link-hop sub-page merged |
| (new) `TIER_PARTIAL` | merger result accepted below STOP threshold (verdict=PARTIAL) |

The reporting layer must accept all old + new labels. Existing dashboards that filter by `extraction_tier_used.startswith("TIER_1_API")` continue to work for single-source extractions; cross-page and merged extractions surface new prefixes.

---

## 15. Appendix: event kinds reference

All `EventKind` additions in this plan, grouped by phase:

| Event | Phase | Emitted from |
|---|---|---|
| `MAPPING_DRIFT_DETECTED` | 6 | `_collect_mapping_replay` on hash mismatch |
| `MAPPING_REPLAY_EMPTY` | 6 | `_collect_mapping_replay` on zero units after replay |
| `FIELD_PATCH_HIT` | 7 | `_collect_field_patches` on successful contribution |
| `FIELD_PATCH_DRIFT` | 7 | `_collect_field_patches` on hash mismatch |
| `DOM_HINTS_MISS` | 8 | `_collect_dom_profile_hints` on no match |
| `DOM_HINTS_EVICTED` | 8 | `_collect_dom_profile_hints` after 3 misses |
| `IDENTITY_FUZZY_LINK` | 5 | `merge_sources` callback when rank-3 fingerprint fires |
| `PLANNER_DECISION` | 5 | `plan_next_action` on every call (with action label) |
| `SOURCE_CONTRIBUTED` | 5 | per source after merge — too noisy to emit per-field; emit once per source |
| `SOURCES_MERGED` | 13 | `_finalize` summary event |
| `CLUSTER_MAPPING_HIT` | 12 | `_collect_cluster_mapping_replay` on contribution |
| `SLO_REPORT` | 13 | scheduled job (not implementation scope) |

Event payloads are dicts; standardize the keys: `property_id` (always), `pms_name`, `url`, `pattern`, `count`, `pct_complete`, `tier_label`. Avoid free-form strings — keep keys grep-able.

---

## 16. Final checklist before merging Phase 13

- [ ] All 13 phases' gates green
- [ ] Migration script run on staging profile store; profile counts match input
- [ ] Cross-cutting loop-safeguard tests green
- [ ] User-scenario acceptance test green
- [ ] 50-property regression run: `units_extracted` per property ≥ baseline; `llm_cost_usd` per property ≤ baseline
- [ ] Cluster bootstrap E2E: at least one COLD onboarding with near-zero LLM cost
- [ ] SLO report generated from a real run, manually inspected — non-zero contribution rates for ≥3 source IDs
- [ ] `extraction_tier_used` labels documented in the reporting layer
- [ ] No new `_*` fields leak into v1/v2 boundary output (confirm `_provenance` strip happens before serialize)

---

## 17. What this plan deliberately does NOT do

- **No rewrite of fetch tier escalation.** The escalation ladder (`escalation_ladder_[plan.md](http://plan.md)`) is orthogonal; this plan operates on whatever bytes the fetcher returns.
- **No new PMS adapters.** AvalonBay, OneSite REST, REIT custom stacks remain pending.
- **No LLM prompt engineering.** Phase 0 noted that `mapping_dict` starvation comes from the LLM omitting `json_paths`. Mitigating this is a separate prompt-design effort. Phase 7 (field_patches) sidesteps it entirely for the recovery path; Phase 10 (self-validation) limits the damage when it happens; deeper fix is out of scope.
- **No syndication fallback tier.** [Apartments.com](http://Apartments.com) / Zillow scraping for no-PMS properties is a separate plan.
- **No parent-child data model.** Multi-phase oversize properties not addressed here.

These are tracked separately. Do not let scope creep contaminate this plan's gates.

---

## 18. Execution principles for Claude Code

1. **Read this entire spec before starting.** It's intentionally self-contained.
2. **Do not advance phases without a green gate.** `gate_[xsource.py](http://xsource.py) phase N` returning 0 is the only criterion.
3. **Use real captured payloads from `data/runs/*/raw_api/`** for all fixtures. Synthetic dicts are not accepted.
4. **Use `python str.replace()` over `sed` for multiline edits in heredocs.** Per project convention.
5. **Pydantic v2: `model.model_dump(mode="json")`, never `.dict()`.**
6. **Async patterns: `asyncio.Semaphore` for concurrency, `asyncio.Lock` for state-file writes, `context.close()` not `browser.close()`.**
7. **No PMS-specific strings outside their adapter module or the detector.** Enforced by Phase 5 static scan.
8. **No LLM imports in adapters except `[generic.py](http://generic.py)`.** Enforced by static scan.
9. **`hashlib.sha256` for all hashing. 16-char prefix unless full hash needed.**
10. **Best-effort profile writes** — never crash a scrape on persistence failure (H10).
11. **If a phase test fails, write a research log entry** explaining the failure mode before iterating; do not blindly tweak thresholds.
12. **Each phase's PR description must reference the specific invariants it satisfies (H1-H10) and which gates it passes.**

---

End of spec. Estimated: 12 phases, ~2,000 LoC + tests, 4–6 weeks at one phase per 2–3 days. Sequencing the phases as written keeps any single PR's diff bounded to 3 files + 1 test file in most cases, with the hardest reviews (Phase 5, Phase 9) standing alone.