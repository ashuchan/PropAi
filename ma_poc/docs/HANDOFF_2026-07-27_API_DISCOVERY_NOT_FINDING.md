# Handoff — the 602-property browser sweep isn't finding APIs

**Symptom:** sweeping the 602-property cohort one property at a time with
`scripts/diagnostics/browser_endpoint_discovery.py`, and `strict_api_proof` is
coming back empty for most of them.

**Do not conclude these properties have no API.** The current worker cannot
distinguish *"this site publishes no unit API"* from *"we navigated away before
the call fired"*. Those need opposite responses, and right now they produce an
identical record. This is the same silent-negative problem that made the
1,127-property retry cohort unanalysable.

Everything below was read out of the current source, not inferred.

---

## 0. Start here — one number already in your checkpoints splits the diagnosis

You already record `api_responses_considered` per property (it is
`len(captured)`, written at every outcome branch and into the checkpoint at
line 1257). **Bucket the completed 602 by it before changing any code:**

| bucket | meaning | fix lives in |
|---|---|---|
| `api_responses_considered == 0` | no JSON XHR was captured at all — the browser never provoked one, or we left first | §2 interaction / navigation |
| `> 0` but no strict proof | calls fired and were captured; `parse_api_responses` or `strict_listing_rows` rejected every row | §3 parsing / proof |
| `>= 80` | you hit `_MAX_CAPTURED_RESPONSES` and **silently stopped capturing** | §4 caps |

These are different bugs with different fixes. A single afternoon of code
against the wrong bucket is the expensive mistake here. One `jq` over the
checkpoint JSONL answers it.

---

## 1. What is already correct — do not rewrite it

Checked, and these are right:

* **The response handler is registered before the first `goto`** (line 654,
  first `goto` at 665). Capture is persistent for the page's whole lifetime, not
  a per-click window.
* `page.on("response", ...)` **does** see responses from child frames, so an
  iframe's XHR is captured *if it ever fires*.
* The proof discipline is sound — `strict_listing_rows` requires a real unit id
  **and** a numeric rent in the same row, and `public_plan_pricing` keeps plan
  evidence in a separate dimension where it can never be mistaken for a unit.
* Persistence is generation-guarded; the route plan is resumable; the cohort is
  read strictly.

The frame is good. What follows is the actuator.

---

## 2. Why an API that exists still never fires

### 2a. The page is abandoned before slow calls land

```python
response = await page.goto(candidate, wait_until="domcontentloaded", timeout=45_000)
...
await page.wait_for_timeout(1_500)
```

`networkidle` appears **zero times** in the file. Navigation settles on
`domcontentloaded`, then a fixed sleep, then the next `goto`. **A `goto` aborts
every in-flight request on that page.** The handler is persistent but the page is
not — an availability XHR that takes 2s on a slow SPA behind a residential proxy
is cancelled, and the property is recorded as having no API.

Residential proxy latency makes this much worse than it looks locally.

```python
# after each navigation and after each click that should trigger a fetch
try:
    await page.wait_for_load_state("networkidle", timeout=8_000)
except PlaywrightTimeoutError:
    pass          # networkidle is best-effort; never fail the property on it
```

Better still, when you know a click should produce a call, wait for the call
rather than for time:

```python
try:
    async with page.expect_response(
        lambda r: r.request.resource_type in {"xhr", "fetch"}
        and 200 <= r.status < 300,
        timeout=6_000,
    ):
        await control.click(timeout=2_500, force=True)
except PlaywrightTimeoutError:
    pass          # the click may legitimately not be a data control
```

This is the single highest-value change in this document.

### 2b. Controls inside iframes are never clicked

```python
controls = page.locator("a, button, [role='button']")
```

`page.locator` searches the **top-level document only**. Your own analysis says
the OneSite/RealPage failures are *"a shell/iframe without a visible roster"* —
so the worker structurally cannot operate the surface you already identified as
the blocker. Captured-if-fired doesn't help when nothing ever clicks the control
that fires it.

```python
for frame in page.frames:                     # includes the main frame
    controls = frame.locator("a, button, [role='button']")
    ...
```

Guard for detached frames (`frame.is_detached()`), and skip cross-origin frames
you cannot query.

### 2c. Forms are never filled — while your other lane proves they matter

`dismiss_nonessential_popups` states it plainly: *"The worker never fills or
submits forms."*

But `_prospectportal_warm_replay.py` persists `{move_in_date}` as a **runtime
placeholder**, which is a direct admission that a date is load-bearing for that
roster. Any availability surface behind a move-in date, a lease term, or a
"check availability" submit is unreachable by construction in the browser lane.

Minimum viable version: if a visible `input[type=date]` or a control matching
/move-?in/i exists, fill it with today+14d and re-run the click routine. Keep it
bounded and never submit anything that looks like a lead form (name/email/phone).

### 2d. Five clicks, spent globally

`_MAX_CLICKS = 5`, and `controls_clicked` is `nonlocal` — it accumulates across
**all four** navigation levels (warm → detail → portal → portal_detail). A
property with several plan cards exhausts the budget on the warm page and
arrives at the portal with zero clicks remaining.

Make it per level (`5` each), and rank candidates by match strength rather than
DOM order, so a weak text match cannot consume the budget.

---

## 3. If `api_responses_considered > 0` and there is still no proof

Then capture is working and the rejection is downstream. Check, in order:

1. **`parse_api_responses` doesn't recognise the shape.** It is built for known
   PMS families; a bespoke vendor payload parses to zero rows. Log the *URLs* of
   captured responses (never bodies) for a sample and eyeball which endpoint
   looked like inventory.
2. **`strict_listing_rows` rejects on rent, not identity.** It requires a numeric
   `market_rent_low`/`asking_rent`, or a parseable `rent_range`. A site showing
   *"Call for pricing"* on every unit yields real unit ids and no numbers — that
   is a **genuine** unit-without-price outcome and deserves its own status, not
   `API_NOT_FOUND_YET`. Worth a distinct classification: you have the identity,
   the price is withheld.
3. **`floorplanId`-as-`unit_number`.** Confirmed on `main` in
   `pms/adapters/rentcafe.py` — its docstring literally says *"Unit ID field:
   floorplanId (floorplan-level, not unit-level)"*. Your fix moves it to
   `source_ids`. Until that lands, some rows carry a plan id in the identity
   field, which `strict_listing_rows` will accept as real. **Land that fix
   first** — it corrupts this sweep's results in the optimistic direction.

---

## 4. Silent caps

```python
_MAX_CAPTURED_RESPONSES = 80
_MAX_CONTROLS = 60
_MAX_CAPTURE_BODY_BYTES = ...
```

`if len(captured) >= _MAX_CAPTURED_RESPONSES: return` — capture stops **silently**
at 80. An analytics-heavy marketing site can burn 80 JSON responses before the
availability call. Same for a body exceeding the byte cap: dropped, no record.

Record a per-property `capture_truncated: bool` and `bodies_dropped_oversize:
int`. A cap that isn't reported reads as "we looked and there was nothing".

---

## 5. Instrument the negative before scaling

A property that finds nothing should say what was actually tried. Add to the
checkpoint:

```
frames_seen                 int
controls_matched            int      # matched the availability text test
controls_clicked            int      # already recorded
networkidle_reached         bool
xhr_total_seen              int      # all XHR, before the JSON/status filter
xhr_json_captured           int      # == api_responses_considered today
capture_truncated           bool
forms_present               bool     # a date/availability form was visible
navigation_levels_reached   int      # warm / detail / portal / portal_detail
```

Without these, "no API" is an assertion. With them it is a measurement, and the
602 sorts itself into "never provoked a call", "provoked and rejected", and
"genuinely publishes nothing" — three populations that need three different
strategies.

This is the identical lesson from the retry funnel (#109): **an exit that emits
nothing makes a miss and a ceiling indistinguishable.**

---

## 6. Suggested order of work

1. **Bucket the already-completed properties by `api_responses_considered`.**
   Costs nothing, decides everything below.
2. **Land the `floorplanId` identity fix** — it biases this sweep optimistically.
3. **Fix the waits** (§2a) — `networkidle` + `expect_response`. Highest value.
4. **Frame traversal** (§2b) — unlocks the iframe portal cohort by name.
5. **Add the negative instrumentation** (§5).
6. **Re-run 10 known failures** and compare against their old records before
   touching the remaining cohort.
7. Only then form-fill (§2c) and re-run at scale.

Steps 3 and 4 are a few hours together and will likely move the verified rate
more than anything else in the file.

---

## 7. Cost

A browser context plus a property-sticky residential session for each of 602 is
the expensive lane. The **7 of 10 verified in local validation came from the
ProspectPortal HTTP replay lane** — the cheap one, which fans out per floorplan
and supplies a move-in date. The browser lane has not yet demonstrated a
comparable rate.

Two consequences worth weighing before the full run:

* Estimate the residential bandwidth for 602 browser sessions **first**. There
  is prior history in this project of proxy spend being discovered afterwards.
* Consider extending the HTTP-replay pattern to more PMS families before
  spending browser sessions on them. Where a warm page exposes plan ids and a
  predictable availability endpoint, replay is faster, cheaper, more reliable,
  and yields a durable template — which is the actual deliverable.

The browser lane is right for genuinely opaque sites. It should be the fallback,
not the first pass.

---

## 8. Related

* `docs/HANDOFF_2026-07-27_TO_CODEX.md` — what landed on `main` (`68ca9dd`),
  including the retry-telemetry supersession and the test-suite traps.
* #109 closed the Path-B retry funnel — same lesson as §5, applied to retries.
* #108 blocks live network egress from tests; if a discovery test needs the real
  seam, mark it `@pytest.mark.probe_seam` and mock curl_cffi underneath.
