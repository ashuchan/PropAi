# Jugnu Bug Hunt Checklist

Work through each item. For each: confirm absence or fix and re-test.

## Fetch layer (J1)

- [ ] `fetch()` never raises on any input (fuzz: "", "not a url", "javascript:", unicode)
- [ ] Proxy-pool `pick()` returns None when empty; caller doesn't crash
- [ ] Rate-limiter `acquire()` eventually returns; no deadlock under fuzz
- [ ] Conditional cache: deleting SQLite mid-run does not crash
- [ ] Robots fetch timeout does not stall the fetcher (5s cap)
- [ ] CAPTCHA detector no false-positive on common marketing pages
- [ ] Browser context closed in all code paths including exceptions
- [ ] Every `fetch.*` event carries a `property_id`

## Discovery layer (J2)

- [ ] Scheduler does not yield the same task twice
- [ ] Frontier deduplicates URLs
- [ ] DLQ retries escalate from hourly to daily at 6h mark
- [ ] Carry-forward fires on fetch hard-fail AND empty-records AND validation-reject
- [ ] Sitemap consumer caps child-file follow at 10
- [ ] Change detector is pure: no side effects

## Extraction layer (J3)

- [ ] `detect_pms()` never raises (fuzz: None, "", binary)
- [ ] `detect_pms()` is deterministic
- [ ] `get_adapter()` never returns None (unknown -> generic)
- [ ] Resolver does not navigate > 5 hops
- [ ] Orchestrator: on SSL error, no adapter called
- [ ] LLM/Vision only inside generic adapter, not specific-PMS adapters
- [ ] No PMS string literal outside its adapter/detector/resolver
- [ ] `tier_used` follows `<adapter>:<tier_key>` format

## Validation layer (J4)

- [ ] Schema gate never raises on malformed input
- [ ] Identity fallback uses `hashlib.sha256`, never `hash()`
- [ ] Rent bounds reject negative and >$50K
- [ ] Cross-run sanity flags but does not reject
- [ ] `next_tier_requested` only when reject ratio strictly >50%

## Observability (J5)

- [ ] `emit()` never raises (swallows all exceptions)
- [ ] Event ledger append-only; truncated line on prior crash tolerated
- [ ] Cost ledger thread-safe with threading.Lock
- [ ] Replay tool handles missing HTML gracefully

## Profile (J6)

- [ ] v1 profiles load under v2 schema without error (`extra="ignore"`)
- [ ] `schema_version` field present and set to "v2"
- [ ] LRU caps on explored_links (50), blocked_endpoints (50), llm_field_mappings (20)

## Report (J7)

- [ ] Verdict computation is pure and deterministic
- [ ] Run report writes both JSON and markdown
- [ ] SLO section present in run report

## Integration (J8)

- [ ] 46-key output schema preserved
- [ ] State files backward-compatible
- [ ] Never-fail contract: no single property crashes the run
- [ ] `model_dump(mode="json")` used for all serialisation (not `.dict()`)
- [ ] `hashlib.sha256` used everywhere (never `hash()`)

## Cross-cutting

- [ ] No layer imports a higher layer
- [ ] No `print()` in new code
- [ ] All async contexts have `finally` blocks
- [ ] All `json.loads()` wrapped in try/except
- [ ] Pydantic v2 `model_dump(mode="json")` used (not `.dict()`)
