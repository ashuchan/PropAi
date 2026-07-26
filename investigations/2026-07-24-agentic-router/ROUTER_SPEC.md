# Agentic Router — scoped spec (2026-07-24)

**Goal:** add reasoning where the deterministic cascade is *blind* (COLD / misroute /
ambiguous fetch results), **without** losing the deterministic core or metric
integrity. Agent at the edges; deterministic in the middle; capture → replay so the
agent makes itself unnecessary over time.

**Non-goals (hard rules):**
1. The agent **never decides success.** Gold stays an objective gate (`unit_id + rent`
   present, `schema_gate`). The agent picks *what to try*, not *whether it worked*.
2. The agent **never runs on the WARM/HOT majority.** Only the hard tail (no confident
   deterministic route, or a route that just failed).
3. Every agent decision is **captured to the profile** → next run is deterministic + free.
4. Every agent decision is **logged** (signals in, action out, reason) for provenance/A-B.

---

## 1. Where it sits

`_process_property` today, in order:
```
change-detection → [direct-GET shortcuts: knock/rentcafe/sightmap/realpage] →
fetch (tier_escalator ladder) → scrape_jugnu (detect→adapter→tiered extract) →
[render-on-empty / plan→unit / hb-shell / link-hop] → validate → verdict
```
The router is a **policy over the fetch result + partial extraction** that decides the
*next action* at two existing decision points:
- **A. pre-extract routing** — which adapter/method to run (today: `detector` + the
  direct-GET gates, both hardcoded).
- **B. post-extract escalation** — what to do with a weak/empty result (today:
  `source_planner.plan_next_action`, rule-based, + the render/hop blocks).

We wrap BOTH with the same router; the agent is invoked only when the deterministic
layer has no confident action.

---

## 2. Signals (deterministic, cheap — no LLM)

`RouteSignals` — computed from the `FetchResult` + `DetectedPMS` + body scan. This is the
router's input and the agent's context. All fields are cheap regex/heuristic.

```python
@dataclass(frozen=True)
class RouteSignals:
    # fetch
    outcome: str            # OK / BOT_BLOCKED / TRANSIENT / DEAD_URL / ...
    status: int | None
    body_bytes: int
    content_type: str
    cf_shell: bool          # captcha_detect.classify_challenge → CF/Turnstile shell
    xhr_captured: int       # len(network_log) with unit-ish content-type
    # content
    has_rent_signal: bool   # $\d, "rent", price regex in body
    has_unit_signal: bool   # unit_number-ish tokens
    pms_fingerprints: list[str]   # doorway/sightmap/rentcafe/realpage/entrata/... in body
    embedded_portal_hints: list[str]  # SightMap/OLL/RentCafe iframe/config pointers
    known_endpoint_match: bool    # a profile known_endpoint appears in body/network_log
    # profile
    maturity: str           # COLD/WARM/HOT
    preferred_tier: str | None
    consecutive_failures: int
```

Most of this already exists scattered (`captcha_detect`, `detector.detect_pms`,
`_response_looks_like_units`, profile fields) — the spec just collects it into one struct
emitted once per property.

---

## 3. Decision contract (extends the existing `Decision`)

Reuse `services.source_planner.Decision`, widen the action menu. **Closed menu** — the
agent picks from THIS list, never free-form:

```
STOP                     # accept current result (deterministic success gate decides gold)
ROUTE_ADAPTER:<name>     # re-run extraction via a specific adapter (misroute fix)
TRY_DIRECT_GET:<pms>     # fire knock/rentcafe/sightmap/realpage direct-GET on a stored ep
ESCALATE_RENDER          # render-on-empty (existing)
ESCALATE_HB_SHELL        # HB in-page fetch of the data page (existing hb_raw_get)
ESCALATE_LINK_HOP        # existing
ESCALATE_LLM_TARGETED    # existing
MARK_VERIFIED_EMPTY      # site reached, genuinely 0 available units (a SUCCESS, not fail)
MARK_DEAD_URL            # 404/soft-404/redirect-trap → re-discovery queue
INVOKE_AGENT             # deterministic layer is unsure → hand to the agent
```

---

## 4. The router (deterministic first, agent only on `INVOKE_AGENT`)

```python
def route(signals, profile) -> Decision:
    # 1. HOT/WARM confident replay — the majority, free, reproducible
    if profile.maturity in ("WARM","HOT") and profile.preferred_route_confident:
        return replay(profile)                     # deterministic
    # 2. strong deterministic signal → known rule
    if signals.cf_shell and signals.pms_fingerprints:  return TRY_DIRECT_GET or ESCALATE_HB_SHELL
    if signals.known_endpoint_match:                    return TRY_DIRECT_GET
    if signals.outcome == "DEAD_URL":                   return MARK_DEAD_URL
    if reached and not signals.has_unit_signal and verified_empty_rule: return MARK_VERIFIED_EMPTY
    # 3. plan_next_action for post-extract weak results (existing rules)
    d = plan_next_action(report, ...)
    if d.action != "UNCERTAIN":                         return d
    # 4. hard tail only → agent
    return Decision(action="INVOKE_AGENT")
```

**Agent invocation (only step 4):** one LLM call, structured output = a `Decision` from
the closed menu above, given `RouteSignals` + a **trimmed body sample** (the rent/unit
section, ≤8KB) + the **list of methods actually available for this property** (which
adapters/endpoints exist). Prompt is classification, not generation: *"Given these
signals, which ONE action recovers unit-level data? Pick from the menu. If the page
genuinely has no available units, MARK_VERIFIED_EMPTY. Do not claim success."*

---

## 5. Capture → replay (kills the non-determinism)

On an agent decision that yields units, `profile_updater` writes:
- `profile.confidence.preferred_route` = the chosen action (+ adapter/endpoint)
- bump `consecutive_successes`; promote maturity
Next run: step 1 replays it deterministically, no agent. **The agent's ROI is measured by
how fast the tail shrinks** (agent-invocations/run should trend →0 for a fixed catalog).

---

## 6. Guardrails / cost bounds

- **Budget:** ≤1 agent call per property, gated behind the existing `cost_ledger`; hard cap
  agent-invocations/run (e.g. 15% of catalog) — overflow → deterministic best-effort.
- **Success stays deterministic:** `schema_gate` + gold definition unchanged. The agent
  cannot set a verdict; it can only choose a method whose OUTPUT is then gated normally.
- **Determinism of the metric:** because success + capture are deterministic, a re-run of
  the SAME catalog after profiles warm is bit-reproducible. Only first-touch of a novel
  prop is agent-nondeterministic (and logged).
- **Provenance:** every agent decision → an `Event(kind="route.agent_decision", payload=
  {signals, action, reason})` in the ledger.

---

## 7. A/B measurement (prove it pays before generalizing)

Scoped cohort: the **COLD-fail + misroute set** (memory: ~700 LLM/generic misroutes,
~400 timeout/dead misroutes). Run twice on the SAME set:
- **Control:** current deterministic pipeline (flag off).
- **Treatment:** router on (`ENABLE_AGENT_ROUTER`, default off).

Report, per arm: gold%, plan-level%, verified-empty%, dead-url%, **agent-calls/prop**,
**LLM $ / prop**, p95 latency. Ship only if treatment gold% > control by a real margin
AND cost/latency are bounded. If not, we've spent one small experiment and learned the
deterministic router is already near-ceiling on that cohort.

---

## 8. Phased rollout

1. **Signals struct + deterministic router wrapping the existing gates** — no LLM yet;
   should reproduce today's behaviour exactly (regression gate). Proves the seam.
2. **Add `INVOKE_AGENT` on the hard tail only**, closed-menu classifier, capture-to-profile.
3. **A/B on the misroute cohort.** Measure. Decide.
4. If it wins: widen the menu / cohort. If not: keep the deterministic router (still a
   cleaner refactor of the scattered gates) and drop the agent.

**Net:** the deterministic core, the gold gate, and reproducibility are untouched; the
agent is a bounded, logged, self-erasing layer on the tail — and step 1 is valuable
(a real routing refactor) even if the agent never ships.
