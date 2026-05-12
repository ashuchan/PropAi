# Unified Signal Engine — Architecture Design

**Status:** Proposed  
**Author:** Claude Code (2026-05-12)  
**Covers:** RC1 (blocked-endpoint feedback loop), RC2 (RentCafe unit keys), RC3 (LLM budget exhaustion), RC4 (JS/CSS fed to LLM), RC5 (0-byte fetch verdict)  
**Self-review:** Pass 1 (3H + 5M resolved) + Pass 2 (2M resolved) → 0H / 0M ✓

---

## 1. The Architectural Problem

RC1–RC4 are not four independent bugs. They are four symptoms of one architectural gap: **there is no single place where "is this signal worth pursuing as a unit data source?" is answered.** Six independent sites answer the same question with incompatible logic:

| Site | What it decides | File |
|---|---|---|
| `has_unit_signals()` | Is this API list unit-shaped? | `pms/adapters/_merge_fns.py:424` |
| `_is_rentcafe_response()` | Is this API RentCafe floor-plan shaped? | `pms/adapters/rentcafe.py:204` |
| `blocked_filter` tier | Drop previously-blocked URLs (no TTL) | `pms/adapters/generic.py:889` |
| `_LINK_ANCHOR_KEYWORDS` etc. | Score DOM links for hop candidates | `pms/scraper.py:1237` |
| LLM budget checks | Can I still afford an LLM call here? | `pms/adapters/generic.py:1704` |
| `source_planner.compute_budget()` | Budget allocation per maturity tier | `services/source_planner.py:450` |

Consequences:
- Fix RentCafe unit keys in `rentcafe.py` → `has_unit_signals()` still rejects them (different key set).
- A JS file passes `blocked_filter` (not blocked) → passes to LLM (no content-type gate) → LLM correctly says "no units in JavaScript".
- LLM hint fires to `/floorplans` → monolithic LLM already spent on homepage → budget exhausted on the useful page.
- An API blocked from one misclassification stays blocked forever (no TTL).

---

## 2. The Fix: `pms/signal_engine/`

A new module that provides three collaborating components behind a single contract. Every layer that touches signal evaluation — API capture, DOM link scoring, LLM budget decisions — passes through this contract.

```
SourceSignal ──► SourceQualifier ──► SourceRanker ──► ActionDecider
(any source)     (is this worth      (how important    (what do I do
                  pursuing?)          vs. others?)      with budget?)
```

### Module structure

```
pms/signal_engine/
├── __init__.py       # public API exports
├── models.py         # SourceSignal, SourceKind, RankedSignal, ExtractionAction
├── qualifier.py      # FieldCombination, MediaTypeFilter, SourceQualifier
├── ranker.py         # ScoringTables, SourceRanker
├── decider.py        # ActionDecider, ActionType, DecisionContext
└── defaults.py       # create_default_qualifier(), create_default_ranker() factories
```

---

## 3. Component 1 — `SourceSignal` (models.py)

The lingua franca. Every signal the scraper touches — API body, DOM link, JSON blob, LLM hint, profile URL — becomes a `SourceSignal` before evaluation.

```python
class SourceKind(str, Enum):
    API_RESPONSE      = "api_response"       # XHR/fetch captured during render
    EMBEDDED_JSON     = "embedded_json"      # <script type="application/json">
    JSON_LD           = "json_ld"            # JSON-LD structured data
    DOM_SECTION       = "dom_section"        # Rendered DOM section
    INTERNAL_LINK     = "internal_link"      # Same-domain navigable URL
    EXTERNAL_PORTAL   = "external_portal"    # Cross-domain leasing portal
    PMS_PRIOR         = "pms_prior"          # PMS-specific sub-path
    UNIVERSAL_PRIOR   = "universal_prior"    # Generic fallback sub-path
    LLM_HINT          = "llm_hint"           # navigation_hint from LLM analysis
    PROFILE_WINNING   = "profile_winning"    # profile.navigation.winning_page_url
    PROFILE_NAV_HINT  = "profile_nav_hint"   # profile.navigation.last_navigation_hints

@dataclass(frozen=True)
class SourceSignal:
    kind:                  SourceKind
    url:                   str | None         = None
    content_type:          str | None         = None   # "application/json", "text/javascript"
    url_suffix:            str | None         = None   # ".js", ".css" — derived from URL
    body_size_bytes:       int                = 0
    field_keys:            frozenset[str]     = field(default_factory=frozenset)
    anchor_text:           str | None         = None
    platform_tag:          str | None         = None   # "rentcafe", "sightmap", etc.
    provenance:            str                = ""
    # Profile-state at collection time (populated from ScrapeProfile):
    blocked_at:            datetime | None    = None   # from BlockedEndpoint.blocked_at
    noise_verdicts:        int                = 0      # from BlockedEndpoint.attempts
    is_known_endpoint:     bool               = False
    profile_score_override: int | None        = None   # 10001 for profile:winning_page_url

    def __post_init__(self) -> None:
        # Normalise field keys to lowercase — prevents RentCafe PascalCase miss
        object.__setattr__(self, "field_keys",
                           frozenset(k.lower() for k in self.field_keys))
```

**Design notes:**
- `frozen=True`: signals are facts, never mutated mid-extraction.
- `field_keys` normalised to lowercase in `__post_init__` — eliminates the PascalCase mismatch that caused RC2.
- `blocked_at` and `noise_verdicts` are read from `ScrapeProfile.api_hints.blocked_endpoints` at signal collection time. `BlockedEndpoint` already has both fields.

---

## 4. Component 2 — `SourceQualifier` (qualifier.py)

**The single place where "is this a valid unit data source?" is answered.**

Two concerns, cleanly separated:

### 4.1 MediaTypeFilter — hard gate, no exceptions

```python
@dataclass(frozen=True)
class MediaTypeFilter:
    blocked_content_types: frozenset[str]    # {"text/javascript", "text/css", "font/", ...}
    blocked_url_suffixes:  frozenset[str]    # {".js", ".css", ".woff", ".png", ...}

    def blocks(self, signal: SourceSignal) -> bool:
        ct = (signal.content_type or "").lower()
        if any(ct.startswith(b) for b in self.blocked_content_types):
            return True
        suffix = (signal.url_suffix or "").lower()
        return suffix in self.blocked_url_suffixes
```

**Fixes RC4:** Before this, JS files from `cdngeneralmvc.rentcafe.com` were passed to `TIER_6_LLM`. The LLM correctly returned no units, but the cost was $0.005 per property and the budget was consumed.

### 4.2 FieldCombination — the declarative "minimum fields" rule set

```python
@dataclass(frozen=True)
class FieldCombination:
    keys:      frozenset[str]   # normalised lowercase key names
    min_count: int              # how many must be present
    label:     str              # "unit_generic", "rentcafe_fp", "rentcafe_unit", ...
```

**All combinations defined in one place** (in `defaults.py`, constructed via factory):

```python
def create_default_qualifier(unit_signal_keys: frozenset[str]) -> SourceQualifier:
    """
    Factory takes _UNIT_SIGNAL_KEYS from _merge_fns.py as a parameter — no circular import.
    qualifier.py has zero imports from pms/adapters/.
    """
    return SourceQualifier(
        combinations=[
            # Generic unit data (≥2 keys) — wraps existing _UNIT_SIGNAL_KEYS
            # Replaces has_unit_signals() in _merge_fns.py
            FieldCombination(
                keys=unit_signal_keys,
                min_count=2,
                label="unit_generic",
            ),
            # RentCafe floor-plan level (≥3 of 6)
            # Replaces _is_rentcafe_response() 3-of-6 check in rentcafe.py
            FieldCombination(
                keys=frozenset({"floorplanname", "floorplanid", "minimumrent",
                                "maximumrent", "availableunitscount", "availabilityurl"}),
                min_count=3,
                label="rentcafe_floor_plan",
            ),
            # RentCafe unit level (≥2 of 3) — NEW, fixes RC2
            FieldCombination(
                keys=frozenset({"rentcafeapartmentid", "rentcafefloorplanid",
                                "rentcafepropertyid"}),
                min_count=2,
                label="rentcafe_unit",
            ),
            # RentCafe unit level with rent fields — alternate endpoint shape
            FieldCombination(
                keys=frozenset({"rentcafeapartmentid", "unitrent", "marketrent"}),
                min_count=2,
                label="rentcafe_unit_rent",
            ),
            # SightMap unit (≥2 of 4)
            FieldCombination(
                keys=frozenset({"unit_number", "price", "area", "available_on"}),
                min_count=2,
                label="sightmap_unit",
            ),
            # Floor-plan physical dimensions (≥3 of 8) — plan-level, no rent required
            FieldCombination(
                keys=frozenset({"beds", "bedrooms", "bathrooms", "baths", "sqft",
                                "area", "floor_plan_name", "floorplanname"}),
                min_count=3,
                label="floor_plan_physical",
            ),
        ],
        media_filter=MediaTypeFilter(
            blocked_content_types=frozenset({
                "text/javascript", "text/css", "font/", "image/",
                "application/font", "application/x-font",
            }),
            blocked_url_suffixes=frozenset({
                ".js", ".css", ".woff", ".woff2", ".ttf", ".otf",
                ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
            }),
        ),
        blocked_ttl_days=14,        # RC1: unblock after 14 days
        min_noise_verdicts=2,       # RC1: require 2 verdicts before permanent block
    )
```

### 4.3 SourceQualifier.qualify() — the single evaluation function

```python
@dataclass
class QualificationResult:
    qualifies:            bool
    reason:               str
    matched_combination:  FieldCombination | None = None

class SourceQualifier:
    combinations:         list[FieldCombination]
    media_filter:         MediaTypeFilter
    blocked_ttl_days:     int = 14
    min_noise_verdicts:   int = 2

    def qualify(self, signal: SourceSignal) -> QualificationResult:
        # Gate 1: media type — hard block (fixes RC4)
        if self.media_filter.blocks(signal):
            return QualificationResult(
                False, f"media_blocked:{signal.content_type or signal.url_suffix}"
            )

        # Gate 2: blocked endpoint TTL check (fixes RC1)
        if signal.blocked_at is not None:
            age_days = (datetime.utcnow() - signal.blocked_at).days
            if age_days < self.blocked_ttl_days and signal.noise_verdicts >= self.min_noise_verdicts:
                return QualificationResult(False, f"blocked:{age_days}d/{signal.noise_verdicts}v")
            # TTL expired OR insufficient noise verdicts → re-admit

        # Gate 3: field-combination check (API_RESPONSE, EMBEDDED_JSON only)
        if signal.kind in (SourceKind.API_RESPONSE, SourceKind.EMBEDDED_JSON):
            if not signal.field_keys:
                return QualificationResult(False, "no_field_keys")
            for combo in self.combinations:
                if len(combo.keys & signal.field_keys) >= combo.min_count:
                    return QualificationResult(True, f"match:{combo.label}", combo)
            return QualificationResult(False, "no_combination_matched")

        # All other kinds (links, hints, DOM) qualify here;
        # relative importance handled by SourceRanker
        return QualificationResult(True, f"non_api:{signal.kind}")
```

---

## 5. Component 3 — `SourceRanker` (ranker.py)

**All scoring tables live here. No scores defined anywhere else.**

```python
@dataclass(frozen=True)
class ScoringTables:
    """
    Replaces all of these in scraper.py:
      _LLM_HINT_SCORE, _EMBEDDED_PORTAL_SCORE, _PMS_PRIOR_SCORE
      _LINK_ANCHOR_KEYWORDS, _LINK_PATH_KEYWORDS, _LINK_HOST_KEYWORDS
      _PMS_SUB_PATH_PRIORS, _UNIVERSAL_SUB_PATH_PRIORS
    """
    kind_base_scores:  dict[SourceKind, int]
    anchor_keywords:   tuple[tuple[str, int], ...]
    path_keywords:     tuple[tuple[str, int], ...]
    host_keywords:     tuple[tuple[str, int], ...]
    pms_priors:        dict[str, tuple[str, ...]]
    universal_priors:  tuple[str, ...]

@dataclass(frozen=True)
class RankedSignal:
    signal:           SourceSignal
    composite_score:  int
    reason:           str     # "profile_override:10001", "llm_hint:10000", "anchor:availability:100"

class SourceRanker:
    tables: ScoringTables

    def rank(self, signals: Iterable[SourceSignal]) -> list[RankedSignal]:
        return sorted(
            (self._score(s) for s in signals),
            key=lambda r: r.composite_score,
            reverse=True,
        )

    def _score(self, signal: SourceSignal) -> RankedSignal:
        # Profile override wins everything
        if signal.profile_score_override is not None:
            return RankedSignal(
                signal, signal.profile_score_override,
                f"profile_override:{signal.profile_score_override}"
            )

        base = self.tables.kind_base_scores.get(signal.kind, 1000)

        # Links: augment with keyword scoring
        if signal.kind in (SourceKind.INTERNAL_LINK, SourceKind.PMS_PRIOR,
                           SourceKind.UNIVERSAL_PRIOR, SourceKind.EXTERNAL_PORTAL):
            kw_score = self._score_link(signal)
            return RankedSignal(signal, base + kw_score, f"link:{kw_score}")

        # API responses: boost known endpoints
        if signal.kind == SourceKind.API_RESPONSE:
            boost = 500 if signal.is_known_endpoint else 0
            return RankedSignal(signal, base + boost, f"api:known={signal.is_known_endpoint}")

        return RankedSignal(signal, base, f"kind_base:{signal.kind}")

    def _score_link(self, signal: SourceSignal) -> int:
        best = 0
        url    = (signal.url         or "").lower()
        anchor = (signal.anchor_text or "").lower()
        for kw, score in self.tables.anchor_keywords:
            if kw in anchor:
                best = max(best, score)
        for kw, score in self.tables.path_keywords:
            if kw in url:
                best = max(best, score)
        for kw, score in self.tables.host_keywords:
            if kw in url:
                best = max(best, score)
        return best
```

**Default kind_base_scores** (in `defaults.py`):

```python
DEFAULT_KIND_BASE_SCORES = {
    SourceKind.PROFILE_WINNING:  10_001,   # profile:winning_page_url sentinel
    SourceKind.LLM_HINT:         10_000,   # replaces _LLM_HINT_SCORE
    SourceKind.EXTERNAL_PORTAL:  10_000,   # replaces _EMBEDDED_PORTAL_SCORE
    SourceKind.PROFILE_NAV_HINT:  9_000,
    SourceKind.API_RESPONSE:      8_000,   # network-captured = data exists
    SourceKind.EMBEDDED_JSON:     7_500,
    SourceKind.JSON_LD:           6_000,
    SourceKind.DOM_SECTION:       5_500,
    SourceKind.PMS_PRIOR:         5_000,   # replaces _PMS_PRIOR_SCORE
    SourceKind.UNIVERSAL_PRIOR:   4_500,
    SourceKind.INTERNAL_LINK:     4_000,   # base, augmented by keyword scoring
}
```

**Migration:** `scraper.py` constants become thin imports from `defaults.py` during Phase 2. They are removed only after Phase 4 regression tests pass.

---

## 6. Component 4 — `ActionDecider` (decider.py)

**Budget-aware action selection. The RC3 fix lives here — the only place where "do I defer the monolithic LLM?" is answered.**

```python
class ActionType(str, Enum):
    PARSE_API              = "parse_api"
    SEARCH_DOM             = "search_dom"
    ANALYZE_LLM_API        = "analyze_llm_api"
    ANALYZE_LLM_DOM        = "analyze_llm_dom"
    ANALYZE_LLM_MONOLITHIC = "analyze_llm_monolithic"
    HOP_TO_URL             = "hop_to_url"
    STOP                   = "stop"

@dataclass(frozen=True)
class ExtractionAction:
    action_type:   ActionType
    target:        RankedSignal | None
    rationale:     str
    budget_after:  dict[str, int | float]   # budget state AFTER this action is taken
                                            # caller adopts this, never mutates in place

@dataclass
class DecisionContext:
    ranked_signals:       list[RankedSignal]
    current_unit_count:   int
    budget:               dict[str, int | float]
    dom_analysis_result:  DOMAnalysisResult | None   # from DOM_ANALYSIS LLM tier
    hop_depth:            int

class ActionDecider:

    def decide(self, ctx: DecisionContext) -> ExtractionAction:

        # Rule 1: units found → stop
        if ctx.current_unit_count > 0:
            return ExtractionAction(ActionType.STOP, None, "units_found", ctx.budget)

        # Rule 2 (RC3): DOM analysis returned navigation_hint with 0 units,
        # AND we have a high-confidence (≥9000) hop target,
        # AND monolithic LLM budget remains.
        # → Defer monolithic LLM to the hop page instead of wasting it here.
        #
        # Guard: only defers for score ≥ 9000 (LLM_HINT, PROFILE_WINNING, EXTERNAL_PORTAL).
        # Does NOT defer for speculative PMS_PRIOR (5000) — too risky to skip monolithic
        # on the current page just because a prior path might work.
        if (ctx.dom_analysis_result is not None
                and ctx.dom_analysis_result.unit_count == 0
                and ctx.dom_analysis_result.navigation_hint is not None
                and ctx.budget.get("llm_monolithic", 0) > 0):
            high_conf_hops = [
                rs for rs in ctx.ranked_signals
                if rs.signal.kind in (SourceKind.LLM_HINT, SourceKind.PROFILE_WINNING,
                                      SourceKind.EXTERNAL_PORTAL)
                and rs.composite_score >= 9_000
            ]
            if high_conf_hops:
                return ExtractionAction(
                    ActionType.HOP_TO_URL,
                    high_conf_hops[0],
                    "dom_analysis_defer_monolithic_to_hop",
                    budget_after=ctx.budget,   # budget NOT decremented — used on hop page
                )

        # Rule 3: no signals → stop
        if not ctx.ranked_signals:
            return ExtractionAction(ActionType.STOP, None, "no_signals", ctx.budget)

        # Rule 4: dispatch top-ranked qualified signal
        top = ctx.ranked_signals[0]
        action = self._map_to_action(top.signal.kind, ctx.budget)
        new_budget = self._decrement(ctx.budget, action)
        return ExtractionAction(action, top, f"top_signal:{top.reason}", new_budget)

    def _map_to_action(self, kind: SourceKind, budget: dict) -> ActionType:
        if kind in (SourceKind.API_RESPONSE, SourceKind.EMBEDDED_JSON, SourceKind.JSON_LD):
            return ActionType.PARSE_API
        if kind == SourceKind.DOM_SECTION:
            if budget.get("llm_dom_calls", 0) > 0:
                return ActionType.ANALYZE_LLM_DOM
            return ActionType.SEARCH_DOM
        if kind in (SourceKind.LLM_HINT, SourceKind.PROFILE_WINNING, SourceKind.PROFILE_NAV_HINT,
                    SourceKind.INTERNAL_LINK, SourceKind.EXTERNAL_PORTAL,
                    SourceKind.PMS_PRIOR, SourceKind.UNIVERSAL_PRIOR):
            return ActionType.HOP_TO_URL
        return ActionType.STOP

    def _decrement(self, budget: dict, action: ActionType) -> dict:
        decrements = {
            ActionType.ANALYZE_LLM_API:        "llm_api_calls",
            ActionType.ANALYZE_LLM_DOM:        "llm_dom_calls",
            ActionType.ANALYZE_LLM_MONOLITHIC: "llm_monolithic",
            ActionType.HOP_TO_URL:             "link_hop",
        }
        b = dict(budget)
        key = decrements.get(action)
        if key and b.get(key, 0) > 0:
            b[key] -= 1
        return b
```

---

## 7. RC5 — `FetchOutcome.EMPTY_BODY` (independent of signal engine)

A 0-byte HTTP 200 response is a distinct failure mode — not a bot block, not a dead URL, not extraction failure. It gets its own outcome value and verdict string so dashboards can route it independently.

**`fetch/contracts.py`** — add one value:
```python
class FetchOutcome(StrEnum):
    OK           = "OK"
    NOT_MODIFIED = "NOT_MODIFIED"
    BOT_BLOCKED  = "BOT_BLOCKED"
    RATE_LIMITED = "RATE_LIMITED"
    TRANSIENT    = "TRANSIENT"
    HARD_FAIL    = "HARD_FAIL"
    PROXY_ERROR  = "PROXY_ERROR"
    DEAD_URL     = "DEAD_URL"
    EMPTY_BODY   = "EMPTY_BODY"    # NEW: HTTP 200 but body is empty
```

**`fetch/fetcher.py`** — after reading `body_text`:
```python
if outcome == FetchOutcome.OK and (not body_text or len(body_text) < 16):
    # Retry once with domcontentloaded (less strict than networkidle)
    body_text = await _retry_with_domcontentloaded(page)
    if not body_text or len(body_text) < 16:
        return FetchResult(
            outcome=FetchOutcome.EMPTY_BODY,
            body=None, body_bytes=0,
            error_signature="EMPTY_BODY_200",
            ...
        )
```

**`pms/scraper.py`** — in the short-circuit verdict mapping:
```python
_OUTCOME_TO_VERDICT = {
    ...
    FetchOutcome.EMPTY_BODY: "FAILED_FETCH_EMPTY",   # new distinct verdict
}
```

The `generic:no_body_short_circuit` tier already fires for any non-OK outcome, so `EMPTY_BODY` is handled automatically once added to the enum.

---

## 8. Self-Review Summary

### Pass 1 findings and resolutions

| ID | Issue | Resolution |
|---|---|---|
| H1 | `_UNIT_SIGNAL_KEYS` duplication risk if qualifier copies it | `unit_generic` FieldCombination receives `_UNIT_SIGNAL_KEYS` via factory DI — same object, no copy |
| H2 | Replacing `has_unit_signals()` in Phase 1 too eagerly | `has_unit_signals()` preserved in `_merge_fns.py`; qualifier runs alongside in Phase 1, replaces in Phase 2 |
| H3 | Profile state consistency during mid-extraction | Documented: signals collected once per property at run start; profile state is consistent throughout |
| M1 | Circular import: `qualifier.py` → `_merge_fns.py` → `generic.py` → `qualifier.py` | `create_default_qualifier(unit_signal_keys)` factory in `defaults.py` — `qualifier.py` has zero imports from `pms/adapters/` |
| M2 | Phase 0 hotfixes must not wait for engine module | Phase 0 changes are independent one-file PRs; engine module not required |
| M3 | Link URL suffix not derived | `url_suffix` derived from URL at `SourceSignal` construction time via `Path(url).suffix` |
| M4 | Test plan not defined | Concrete test cases defined (see §9) |
| M5 | Budget mutation semantics | `ExtractionAction.budget_after` carries the NEXT budget state; caller adopts it — no in-place mutation |

### Pass 2 findings and resolutions

| ID | Issue | Resolution |
|---|---|---|
| M6 | RC3 rule could defer LLM for speculative PMS_PRIOR hops | Guard: only defers for `composite_score ≥ 9_000` (LLM_HINT / PROFILE_WINNING / EXTERNAL_PORTAL only) |
| M7 | Field key normalisation inconsistency | `SourceSignal.__post_init__` normalises all `field_keys` to lowercase unconditionally |

**Pass 2 verdict: 0 HIGH / 0 MEDIUM ✓**

---

## 9. Test Plan

### `tests/pms/signal_engine/test_qualifier.py` (7 cases)

| # | Case | Expected |
|---|---|---|
| Q1 | JS file (`content_type="text/javascript"`) | `qualifies=False`, reason starts `media_blocked:` |
| Q2 | API with `{rent, sqft, unitNumber, beds}` | `qualifies=True`, `matched_combination.label="unit_generic"` |
| Q3 | API with `{rentCafeApartmentId, rentCafeFloorplanId}` | `qualifies=True`, `matched_combination.label="rentcafe_unit"` |
| Q4 | API with `{floorplanname, minimumrent, maximumrent, floorplanid}` | `qualifies=True`, `matched_combination.label="rentcafe_floor_plan"` |
| Q5 | Blocked endpoint, `blocked_at` < 14 days, `noise_verdicts=2` | `qualifies=False`, reason starts `blocked:` |
| Q6 | Blocked endpoint, `blocked_at` > 14 days (TTL expired) | `qualifies=True` (re-admitted) |
| Q7 | Blocked endpoint, `noise_verdicts=1` (< min_noise_verdicts=2) | `qualifies=True` (not enough evidence to block) |

### `tests/pms/signal_engine/test_ranker.py` (4 cases)

| # | Case | Expected |
|---|---|---|
| R1 | `SourceKind.LLM_HINT` signal | `composite_score = 10_000` |
| R2 | `SourceKind.API_RESPONSE` with `is_known_endpoint=True` | `composite_score = 8_500` |
| R3 | `INTERNAL_LINK` with anchor "floor plans" | `composite_score = 4_000 + 90 = 4_090` |
| R4 | Three mixed signals → sorted descending | `PROFILE_WINNING > LLM_HINT > INTERNAL_LINK` |

### `tests/pms/signal_engine/test_decider.py` (4 cases)

| # | Case | Expected |
|---|---|---|
| D1 | DOM_ANALYSIS: nav_hint="/floorplans", 0 units, `llm_monolithic=1`, high-conf hop (LLM_HINT score 10000) | `action=HOP_TO_URL`, `budget_after["llm_monolithic"]=1` (not decremented) |
| D2 | DOM_ANALYSIS: nav_hint="/floorplans", 0 units, `llm_monolithic=0` | Does NOT defer; picks next best signal |
| D3 | `current_unit_count=10` | `action=STOP`, rationale `"units_found"` |
| D4 | `ranked_signals=[]` | `action=STOP`, rationale `"no_signals"` |

---

## 10. Delivery Phases

The full engine ships as **one PR** with phases implemented and tested sequentially. A final review cycle runs after all phases pass.

### Phase 0 — Hotfixes (no new module, independent one-file changes)

| Fix | File | Lines changed | Tests |
|---|---|---|---|
| **RC5** `EMPTY_BODY` outcome + `FAILED_FETCH_EMPTY` verdict | `fetch/contracts.py`, `fetch/fetcher.py`, `pms/scraper.py` | ~15 | `tests/fetch/test_response_classifier.py` |
| **RC2** RentCafe unit-level keys in `_is_rentcafe_response()` | `pms/adapters/rentcafe.py:218` | 5 | `tests/pms/adapters/test_rentcafe.py` |
| **RC1** TTL check using existing `blocked_at` field | `pms/adapters/generic.py:~889` | 4 | `tests/pms/adapters/test_generic.py` |
| **RC4** JS/CSS filter before LLM bundling | `pms/adapters/generic.py` Phase 5 bundle | 3 | `tests/pms/adapters/test_generic.py` |

Phase 0 gate: `pytest tests/fetch/ tests/pms/adapters/test_rentcafe.py tests/pms/adapters/test_generic.py -q --tb=short` exits 0.

### Phase 1 — `SourceQualifier`

- Create `pms/signal_engine/__init__.py`, `models.py`, `qualifier.py`, `defaults.py`
- Replace `blocked_filter` URL-drop logic with `SourceQualifier.qualify()` (still drops non-qualifiers)
- `api_narrow` calls `SourceQualifier.qualify()` alongside existing `has_unit_signals()` — if qualifier passes additional combinations (RentCafe unit), the signal proceeds
- `has_unit_signals()` preserved in `_merge_fns.py` throughout Phase 1 (used in merge/dedup contexts)

Phase 1 gate: `pytest tests/pms/signal_engine/ tests/pms/adapters/ -q --tb=short` exits 0.

### Phase 2 — `SourceRanker`

- Create `pms/signal_engine/ranker.py`
- All scoring tables in `scraper.py` become imports from `defaults.py` (no value changes)
- `_try_link_hop()` in `scraper.py` delegates link scoring to `SourceRanker.rank()`
- `_augment_ranked_with_hints()` removed; logic absorbed into `SourceRanker`

Phase 2 gate: `pytest tests/pms/ -q --tb=short` exits 0. Scoring constants in `scraper.py` now import from `defaults.py` — verify via `grep _LLM_HINT_SCORE pms/scraper.py` shows only the import, not a definition.

### Phase 3 — `ActionDecider` + RC3

- Create `pms/signal_engine/decider.py`
- RC3 defer-monolithic rule wired into `generic.py`'s tier dispatch
- Wraps existing cascade — does not reorder tiers, only gates the monolithic LLM call

Phase 3 gate: `pytest tests/pms/ tests/integration/contracts/ tests/integration/extract/ -q --tb=short` exits 0. Run canary `manifest_2026_05_12_root_cause_analysis.csv` — confirm no REGRESSED outcomes.

### Phase 4 — Full integration + cleanup

- Tier cascade in `generic.py` becomes a loop driven by `ActionDecider.decide()` outputs
- Scattered constants in `scraper.py` (`_LLM_HINT_SCORE`, `_LINK_ANCHOR_KEYWORDS`, etc.) removed
- `_UNIT_SIGNAL_KEYS` import chain consolidated: `_merge_fns.py` → `defaults.py` → qualifier
- `pms/adapters/rentcafe.py:_is_rentcafe_response()` delegates to `SourceQualifier`

Phase 4 gate: `pytest tests/ -q --tb=no --ignore=tests/integration/e2e` exits 0. `ruff check` + `mypy --strict` on all changed files exit 0.

### Final review cycle

After all phases pass:
1. `pytest tests/ -q --tb=no --ignore=tests/integration/e2e` — all green
2. `ruff check pms/signal_engine/ pms/adapters/generic.py pms/scraper.py fetch/fetcher.py` — 0 errors
3. `mypy --strict pms/signal_engine/` — 0 errors
4. Canary run against `manifest_2026_05_12_root_cause_analysis.csv` — REGRESSED=0
5. Integration smoke: `pytest tests/integration/ -q --tb=line` — all green
6. Code quality checklist (senior review):
   - No scattered scoring constants remain outside `signal_engine/defaults.py`
   - No `_is_rentcafe_response()`-style PMS-specific qualification outside `signal_engine/qualifier.py`
   - `ActionDecider.decide()` is the only place where "defer monolithic LLM" logic lives
   - All `SourceSignal` construction normalises `field_keys` to lowercase
   - `budget_after` is always a new dict (not a mutation of the input budget)

---

## 11. What This Does NOT Change

- **Tier names in events and reports** (`TIER_1_API`, `TIER_3_DOM`, etc.) — preserved for backward compatibility with existing dashboards and test assertions.
- **`_merge_fns.py` merge/dedup logic** (R1a–R1f identity ladder) — unchanged; this is about identity resolution, not signal qualification.
- **Per-tier parsing implementations** — `rentcafe.py` unit mapping, `sightmap.py` geometry parser, `_html_extract.py` DOM regex — none of these change. Only the qualification and ranking of signals changes.
- **`source_planner.compute_budget()`** — HOT/WARM/COLD budget allocation unchanged. `ActionDecider` consumes the budget but does not redefine it.
- **Existing tests** — the engine is introduced alongside existing code in Phases 1–3. No existing test assertion changes until Phase 4.

---

## 12. Key Invariants to Verify in Final Review

| Invariant | How to verify |
|---|---|
| `SourceSignal.field_keys` is always lowercase | `grep -n "SourceSignal(" pms/ -r` → each constructor call or `assert all(k == k.lower() for k in sig.field_keys)` in tests |
| `budget_after` is always a fresh dict | `grep -n "budget_after" pms/signal_engine/decider.py` → always `dict(budget)` copy |
| `ActionDecider` is the ONLY site with "defer monolithic LLM" logic | `grep -rn "llm_monolithic" pms/` → only `decider.py` and `source_planner.py` |
| No `_LLM_HINT_SCORE` defined outside `defaults.py` (Phase 4) | `grep -rn "_LLM_HINT_SCORE\s*=" pms/` → only `defaults.py` |
| `_UNIT_SIGNAL_KEYS` defined in exactly one place | `grep -rn "_UNIT_SIGNAL_KEYS\s*=" pms/` → only `_merge_fns.py` |
| All `FieldCombination` definitions in `defaults.py` | `grep -rn "FieldCombination(" pms/` → only `defaults.py` |
| `MediaTypeFilter` is the only JS/CSS gate | `grep -rn "text/javascript" pms/` → only `signal_engine/defaults.py` |
