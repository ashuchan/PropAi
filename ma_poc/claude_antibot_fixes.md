# CLAUDE_ANTIBOT_FIXES.md

**Mission:** Close the anti-bot pipeline gaps surfaced by the 2026-05-04 production run analysis. Three small classifier/resolver fixes plus a `rentcafe_direct` path that bypasses the per-vanity Cloudflare wall by going to RentCafe's centralized aggregator API. Single mergeable PR. No feature flag — direct path activates only when a propertyId is cached or successfully resolved, and falls back to the existing pipeline on any failure (H4).

**Predecessor:** None. Motivated by `analysis_report_latest.md` (2026-05-04) and the URL-composition deep-dive that found 82% of the blocked surface is RentCafe-family on Cloudflare, 99.2% of RentCafe CAPTCHA challenges are Cloudflare, and 6 legacy `.aspx` URLs across shards 0/3/7/8 silently 403 without `BOT_BLOCKED` classification (so escalation never fires).

**Audience:** Claude Code, executing autonomously against `ashuchan/PropAi`.

**Scope estimate:** ~600 LoC production + ~700 LoC tests + ≥5 captured fixtures + 2 docs. Across ~15 files. ~1.5 days. The Phase 0 research browser session and the F2 diagnostic run are the only non-automatable steps.

**Out of scope:**
- Replacing the existing `rentcafe.py` parser. F5/F6 reuse it via `_is_rentcafe_response` and `RentCafeAdapter.extract`.
- Generalizing propertyId resolution to Entrata / OneSite / AppFolio.
- Vendor proxy / unlocker evaluation (F2 informs that decision; doesn't make it).
- Modifying the L1 escalation ladder, L3 cascade, validation orchestrator, identity-fallback, schema gate.
- LLM-based propertyId resolution.

---

## 1. What this PR does and does NOT do

**Does:**
1. **F1** — Adds AppFolio `/listings/rental_applications/*` to the resolver path blacklist. Eliminates the 10-property reCAPTCHA cluster.
2. **F2** — One-off TLS-fingerprint vs IP-reputation diagnostic; commits verdict to `docs/ANTIBOT_TLS_VERDICT.md`. No production code change.
3. **F3** — Fetch classifier: HTTP 403 with empty body or Cloudflare server header → `BOT_BLOCKED` (currently `FAILED_NO_DATA`, suppressing escalation).
4. **F4** — `propertyid_resolver.py`: (property_name, city, zip, vanity_domain) → RentCafe propertyId via aggregator search.
5. **F5** — `rentcafe_direct/fetcher.py`: given propertyId, fetch rentcafe.com floorplans API directly; classify failures.
6. **F6** — Wire direct path into the runner: when `profile.api_provider == "rentcafe"` and a propertyId is cached or resolvable, try direct first; fall back to vanity-domain on any failure. Adds `rentcafe_property_id: str | None` to `ScrapeProfile.api_hints` with writer in `profile_updater.py` and reader in `jugnu_runner.py`.
7. **F7** — Production smoke: run direct path against 50 known-blocked RentCafe properties from the 2026-05-04 set; ≥30 must succeed. Output committed to `data/smoke/rentcafe_direct_smoke.json`.

**Does not:** decide proxy vendor, add tier labels beyond the documented `TIER_1_API_RENTCAFE_DIRECT_*` taxonomy, change cascade ordering, add caches outside the profile.

---

## 2. Hard invariants

| #   | Invariant                                                                                                                                                                                              | Where verified                                                                  |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------- |
| H1  | Resolver path blacklist is a single named constant. F1 adds entries; does not duplicate.                                                                                                                | `test_f1_blacklist_single_source_of_truth` (static scan)                        |
| H2  | F2 produces `docs/ANTIBOT_TLS_VERDICT.md` with header `verdict: <TLS_FINGERPRINT|IP_REPUTATION|MIXED|NOT_REPRODUCIBLE>` before F3 ships.                                                                | `test_f3_requires_f2_verdict_present`; gate runner check                        |
| H3  | Fetch classifier returns `(SOFT_FAIL, "BOT_BLOCKED")` for HTTP 403 with empty body OR Cloudflare server header (`server: cloudflare`, `cf-ray`, `cf-mitigated`).                                          | `test_f3_403_empty_body_classified_bot_blocked`, `test_f3_cf_server_header_classified_bot_blocked` |
| H4  | If propertyId resolution fails OR direct fetch fails for any reason, runner falls back to existing vanity-domain pipeline. Direct-path failure is invisible downstream — no records dropped that the existing pipeline would have produced. | `test_f6_falls_back_on_resolution_failure`, `test_f6_falls_back_on_metadata_mismatch`, `test_f6_falls_back_on_direct_fetch_failure` |
| H5  | When `profile.api_provider == "rentcafe"` AND cached propertyId exists, direct fetch runs BEFORE vanity-domain fetch; vanity is skipped on direct success.                                              | `test_f6_direct_runs_before_vanity_when_id_cached`                              |
| H6  | propertyId persisted to `profile.api_hints.rentcafe_property_id` after successful direct fetch. Second run for same property does NOT call `resolve_property_id` (cache hit).                            | `test_f6_propertyid_persisted_after_success`, `test_f6_second_run_uses_cache`   |
| H7  | Direct path output unit shape is byte-identical to existing `rentcafe.py` adapter output for the same response body. Achieved by reusing `_is_rentcafe_response` and `RentCafeAdapter.extract`, NOT by reimplementing parsing. | `test_f5_unit_shape_matches_rentcafe_adapter` (parametrized over fixtures)      |
| H8  | Failure taxonomy: `TIER_1_API_RENTCAFE_DIRECT`, `_LIST_EMPTY`, `_NO_RESPONSE`, `_SHAPE_REJECTED`, `_PROPERTY_ID_RESOLUTION_FAILED`, `_PROPERTY_ID_MISMATCH`. Exact strings exposed as `TIER_CODES` frozenset. | `test_f5_failure_classification_taxonomy`                                       |
| H9  | F7 smoke: ≥30 of 50 properties from the 2026-05-04 RentCafe-blocked set produce ≥1 unit via direct path.                                                                                                 | `test_f7_smoke_threshold_30_of_50`                                              |
| H10 | No LLM imports in `ma_poc/pms/rentcafe_direct/`. No RentCafe-specific strings (`rentcafe.com`, `securecafe.com`, `RentCafe`, `rentcafe_property_id`) outside the permitted file set in §6.              | gate runner static scan                                                         |
| H11 | New schema field has both writer AND reader. `rentcafe_property_id` grep-verifiable in `profile_updater.py` (writer) and `jugnu_runner.py` (reader).                                                    | `test_f6_schema_field_has_writer_and_reader` (static scan)                      |
| H12 | Profile writes for propertyId are best-effort: `try/except`, log on failure, never raise.                                                                                                               | `test_f6_profile_write_swallows_exception`                                       |
| H13 | A failed direct fetch must NOT overwrite a previously-good propertyId in the profile.                                                                                                                   | `test_f6_propertyid_not_persisted_on_failure`                                    |
| H14 | F3 does NOT misclassify legitimate 403 responses (login walls etc.) as `BOT_BLOCKED`. Discriminator: body length ≥64 bytes AND no Cloudflare header.                                                     | `test_f3_legitimate_403_login_wall_not_misclassified`                           |

**Ordering constraint:** F2's verdict file must exist before F3 tests pass (H2). Within this PR: run F2 diagnostic locally, commit the verdict doc, then write F3.

---

## 3. Fix list

| #  | File(s)                                                                                              | Fix                                                                                          | Tests / LoC                          |
| -- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- | ------------------------------------ |
| F1 | `ma_poc/pms/resolver.py`                                                                             | Add AppFolio rental_applications regex to existing blacklist                                  | 2 tests, ~30 LoC                     |
| F2 | `ma_poc/scripts/diagnostics/tls_vs_ip_diagnostic.py` (NEW); `docs/ANTIBOT_TLS_VERDICT.md` (NEW)      | One-shot diagnostic; commits structured verdict                                               | 2 logic tests, ~150 LoC              |
| F3 | `ma_poc/fetch/fetcher.py`                                                                            | Silent-403 / Cloudflare-header → `BOT_BLOCKED`                                                | 5 tests, ~25 LoC                     |
| F4 | `ma_poc/pms/rentcafe_direct/propertyid_resolver.py` (NEW)                                            | Aggregator-search resolver: EXACT / ZIP_AND_NAME_PREFIX / ZIP_ONLY / NONE                     | 6 tests, ~130 LoC                    |
| F5 | `ma_poc/pms/rentcafe_direct/fetcher.py` (NEW)                                                        | propertyId → aggregator API responses; classify failures                                       | 5 tests + parametrized shape-eq, ~120 LoC |
| F6 | `ma_poc/models/scrape_profile.py`, `services/profile_updater.py`, `scripts/jugnu_runner.py`         | Add field; persist on success; route direct-before-vanity                                      | 8 tests, ~80 LoC                     |
| F7 | `ma_poc/scripts/smoke_rentcafe_direct.py` (NEW)                                                      | Manual run against 50-property subset                                                          | 1 threshold test, ~80 LoC            |

Plus: `docs/RENTCAFE_DIRECT_RESEARCH.md` (manual, gates F4-F7), ≥5 captured fixtures under `tests/pms/adapters/fixtures/rentcafe_direct/`, and `ma_poc/scripts/gate_antibot_fixes.py`.

---

## 4. File map

| File                                                                       | Status   | What                                                                          |
| -------------------------------------------------------------------------- | -------- | ----------------------------------------------------------------------------- |
| `ma_poc/pms/resolver.py`                                                   | PATCH    | One regex entry added to existing blacklist constant                          |
| `ma_poc/fetch/fetcher.py`                                                  | PATCH    | `_is_silent_block` + `_has_cloudflare_signature` helpers; classifier branch   |
| `ma_poc/models/scrape_profile.py`                                          | PATCH    | Add `rentcafe_property_id: str \| None = None` to `api_hints`                  |
| `ma_poc/services/profile_updater.py`                                       | PATCH    | Best-effort write of propertyId after successful direct fetch                  |
| `ma_poc/scripts/jugnu_runner.py`                                           | PATCH    | Routing: try direct before vanity when conditions met                         |
| `ma_poc/pms/rentcafe_direct/__init__.py`                                   | NEW      | Public surface: `fetch_direct`, `resolve_property_id`                         |
| `ma_poc/pms/rentcafe_direct/propertyid_resolver.py`                        | NEW      | F4                                                                            |
| `ma_poc/pms/rentcafe_direct/fetcher.py`                                    | NEW      | F5                                                                            |
| `ma_poc/scripts/diagnostics/tls_vs_ip_diagnostic.py`                       | NEW      | F2                                                                            |
| `ma_poc/scripts/smoke_rentcafe_direct.py`                                  | NEW      | F7                                                                            |
| `ma_poc/scripts/gate_antibot_fixes.py`                                     | NEW      | Gate runner (mirrors `gate_pr25.py`)                                          |
| `docs/ANTIBOT_TLS_VERDICT.md`                                              | NEW      | F2 output (committed)                                                         |
| `docs/RENTCAFE_DIRECT_RESEARCH.md`                                         | NEW      | Phase 0 research (manual)                                                     |
| `tests/pms/adapters/fixtures/rentcafe_direct/*.json`                       | NEW      | ≥5 captured aggregator response bodies                                        |
| `tests/pms/adapters/fixtures/rentcafe_direct/*.meta.json`                  | NEW      | Paired metadata per fixture                                                   |
| `tests/resolver/test_path_blacklist.py`                                    | NEW      | F1 tests                                                                      |
| `tests/diagnostics/test_tls_vs_ip_diagnostic.py`                           | NEW      | F2 logic tests                                                                |
| `tests/fetch/test_silent_403_classification.py`                            | NEW      | F3 tests                                                                      |
| `tests/pms/rentcafe_direct/test_propertyid_resolver.py`                    | NEW      | F4 tests                                                                      |
| `tests/pms/rentcafe_direct/test_fetcher.py`                                | NEW      | F5 fetcher tests                                                              |
| `tests/pms/rentcafe_direct/test_shape_equivalence.py`                      | NEW      | F5 H7 invariant test                                                          |
| `tests/integration/test_rentcafe_direct_routing.py`                        | NEW      | F6 tests                                                                      |
| `tests/integration/test_rentcafe_direct_smoke.py`                          | NEW      | F7 threshold test                                                             |

---

## 5. Detailed fixes

### F1 — AppFolio path blacklist

**Locate** the existing constant:

```bash
grep -rn "scheduletour\|_BLACKLISTED_PATHS\|RESOLVER_PATH_BLOCK" ma_poc/pms/ ma_poc/services/
```

Most likely `ma_poc/pms/resolver.py`. Add the entry to that constant; do not create a second list (H1).

```python
# Before — exact regex may differ; preserve existing alternation
_BLACKLISTED_PATH_RE = re.compile(
    r"/(scheduletour|scheduleatour|schedule-tour|tour|contact|apply|book)/?(?:$|[?#])",
    re.IGNORECASE,
)

# After — add AppFolio rental_applications as a second alternation
_BLACKLISTED_PATH_RE = re.compile(
    r"/(scheduletour|scheduleatour|schedule-tour|tour|contact|apply|book)/?(?:$|[?#])"
    r"|/listings/rental_applications/",
    re.IGNORECASE,
)
```

If the resolver doesn't already export a public predicate, add `is_blacklisted_path(url) -> bool` so tests don't depend on regex internals.

#### Tests — `tests/resolver/test_path_blacklist.py`

```python
"""F1 — AppFolio rental_applications added to resolver path blacklist."""
from __future__ import annotations
from pathlib import Path
import pytest

from ma_poc.pms.resolver import is_blacklisted_path

_REPO = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("url,expected", [
    # F1 — must be blacklisted
    ("https://example.appfolio.com/listings/rental_applications/new", True),
    ("https://prop.appfolio.com/listings/rental_applications/", True),
    ("https://prop.appfolio.com/listings/rental_applications/new?source=cta", True),
    ("https://PROP.APPFOLIO.COM/Listings/Rental_Applications/New", True),
    # Existing entries unaffected
    ("https://example.com/scheduletour", True),
    ("https://example.com/contact", True),
    # Negative
    ("https://example.com/listings/", False),
    ("https://example.com/listings/abc-123/", False),
    ("https://example.com/floorplans", False),
])
def test_f1_appfolio_rental_application_path_excluded(url, expected):
    assert is_blacklisted_path(url) is expected


def test_f1_blacklist_single_source_of_truth():
    """H1: 'rental_applications' must appear in exactly one production file."""
    matches = []
    for f in (_REPO / "ma_poc").rglob("*.py"):
        if "tests" in f.parts or "scripts" in f.parts:
            continue
        if "rental_applications" in f.read_text(encoding="utf-8"):
            matches.append(str(f.relative_to(_REPO)))
    assert matches == ["ma_poc/pms/resolver.py"], (
        f"rental_applications appears in multiple production files: {matches}"
    )
```

---

### F2 — TLS-fingerprint vs IP-reputation diagnostic

**Symptom.** Six legacy `.aspx` URLs across shards 0/3/7/8 return 403 without challenge. Two competing hypotheses imply different fixes; the diagnostic discriminates.

**Script — `ma_poc/scripts/diagnostics/tls_vs_ip_diagnostic.py`:**

```python
#!/usr/bin/env python3
"""F2 — TLS-fingerprint vs IP-reputation diagnostic.

Runs two fetches against each of 6 known-silent-403 .aspx URLs from the
2026-05-04 production run from the same egress:
  A) httpx default TLS    (control)
  B) curl_cffi --impersonate chrome120

Result matrix:
  | A   | B   | per-URL verdict     |
  |-----|-----|---------------------|
  | 403 | 200 | TLS_FINGERPRINT     |
  | 403 | 403 | IP_REPUTATION       |
  | 200 | 200 | NOT_REPRODUCIBLE    |
  | 200 | 403 | UNEXPECTED          |

Aggregate: ≥4 of one kind → that verdict. Mixed (≥2 of each) → MIXED.
Else INCONCLUSIVE (rerun with --retries 3).

Outputs `docs/ANTIBOT_TLS_VERDICT.md` with structured `verdict:` header.

Usage:
    python -m ma_poc.scripts.diagnostics.tls_vs_ip_diagnostic [--retries N]
"""
from __future__ import annotations
import argparse, asyncio, datetime as dt, sys
from collections import Counter
from pathlib import Path

import httpx
try:
    from curl_cffi import requests as curl_cffi_requests
except ImportError:
    curl_cffi_requests = None

REPO_ROOT = Path(__file__).resolve().parents[3]
VERDICT_PATH = REPO_ROOT / "docs" / "ANTIBOT_TLS_VERDICT.md"

DIAGNOSTIC_URLS: list[tuple[str, str]] = [
    ("shard_7", "http://www.rentcafe.com/onlineleasing/hampshire-village/floorplans.aspx"),
    ("shard_8", "http://www.rentcafe.com/onlineleasing/highview-terrace/floorplans.aspx"),
    ("shard_8", "https://villageatthegateway.securecafe.com/onlineleasing/village-at-gateways/floorplans.aspx"),
    ("shard_7", "https://theapartmentgallery.securecafe.com/onlineleasing/st-clair-terrace0/floorplans.aspx"),
    ("shard_0", "https://theapartmentgallery.securecafe.com/onlineleasing/cloister-gardens/floorplans.aspx"),
    ("shard_3", "https://livebh.com/residentservices/apartmentsforrent/userlogin.aspx"),
]


async def _fetch_httpx(url: str, timeout: float = 20.0) -> int:
    async with httpx.AsyncClient(follow_redirects=True, timeout=timeout) as c:
        try:
            r = await c.get(url)
            return r.status_code
        except Exception:
            return -1


def _fetch_curl_cffi(url: str, timeout: float = 20.0) -> int:
    if curl_cffi_requests is None:
        return -2
    try:
        r = curl_cffi_requests.get(url, impersonate="chrome120",
                                    timeout=timeout, allow_redirects=True)
        return r.status_code
    except Exception:
        return -1


def _classify_pair(a: int, b: int) -> str:
    if a < 200 or b < 200:
        return "ERROR"
    if a == 403 and b in (200, 301, 302):
        return "TLS_FINGERPRINT"
    if a == 403 and b == 403:
        return "IP_REPUTATION"
    if a in (200, 301, 302) and b in (200, 301, 302):
        return "NOT_REPRODUCIBLE"
    if a in (200, 301, 302) and b == 403:
        return "UNEXPECTED"
    return f"OTHER(a={a},b={b})"


def _aggregate_verdict(verdicts: list[str]) -> str:
    counts = Counter(verdicts)
    if counts.get("TLS_FINGERPRINT", 0) >= 4:
        return "TLS_FINGERPRINT"
    if counts.get("IP_REPUTATION", 0) >= 4:
        return "IP_REPUTATION"
    if counts.get("TLS_FINGERPRINT", 0) >= 2 and counts.get("IP_REPUTATION", 0) >= 2:
        return "MIXED"
    if counts.get("NOT_REPRODUCIBLE", 0) >= 4:
        return "NOT_REPRODUCIBLE"
    return "INCONCLUSIVE"


def _write_verdict(per_url: list[dict], aggregate: str) -> None:
    VERDICT_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Anti-bot TLS vs IP diagnostic — verdict",
        "",
        f"verdict: {aggregate}",
        f"generated_at: {dt.datetime.utcnow().isoformat()}Z",
        f"sample_size: {len(per_url)}",
        "",
        "## Per-URL results",
        "",
        "| shard | url | httpx | curl_cffi | verdict |",
        "|-------|-----|------:|----------:|---------|",
    ]
    for r in per_url:
        lines.append(f"| {r['shard']} | `{r['url']}` | {r['a']} | {r['b']} | {r['verdict']} |")
    lines += [
        "",
        "## Interpretation",
        "- TLS_FINGERPRINT — `curl_cffi --impersonate chrome120` succeeds where default `httpx` fails. DIY stealth tier (`curl_cffi`/`patchright`) is the cheap fix.",
        "- IP_REPUTATION — both fail identically. GCP egress on Cloudflare deny lists; vendor evaluation required.",
        "- MIXED — both fixes needed.",
        "- NOT_REPRODUCIBLE / INCONCLUSIVE — rerun with `--retries 3`.",
    ]
    VERDICT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


async def main_async(retries: int = 1) -> int:
    if curl_cffi_requests is None:
        print("FATAL: install curl_cffi (pip install curl_cffi)", file=sys.stderr)
        return 2
    per_url: list[dict] = []
    for shard, url in DIAGNOSTIC_URLS:
        a_results, b_results = [], []
        for _ in range(retries):
            a_results.append(await _fetch_httpx(url))
            b_results.append(_fetch_curl_cffi(url))
        a = Counter(a_results).most_common(1)[0][0]
        b = Counter(b_results).most_common(1)[0][0]
        v = _classify_pair(a, b)
        per_url.append({"shard": shard, "url": url, "a": a, "b": b, "verdict": v})
        print(f"  [{shard}] httpx={a} curl_cffi={b} -> {v}    {url}")
    aggregate = _aggregate_verdict([r["verdict"] for r in per_url])
    _write_verdict(per_url, aggregate)
    print(f"\nAggregate verdict: {aggregate}")
    print(f"Written to: {VERDICT_PATH}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--retries", type=int, default=1)
    sys.exit(asyncio.run(main_async(retries=p.parse_args().retries)))


if __name__ == "__main__":
    main()
```

#### Tests — `tests/diagnostics/test_tls_vs_ip_diagnostic.py`

```python
"""F2 — diagnostic logic tests (no network)."""
from __future__ import annotations
import importlib
import pytest

mod = importlib.import_module("ma_poc.scripts.diagnostics.tls_vs_ip_diagnostic")


@pytest.mark.parametrize("a,b,expected", [
    (403, 200, "TLS_FINGERPRINT"),
    (403, 403, "IP_REPUTATION"),
    (200, 200, "NOT_REPRODUCIBLE"),
    (200, 403, "UNEXPECTED"),
    (-1, 200, "ERROR"),
])
def test_f2_classify_pair(a, b, expected):
    assert mod._classify_pair(a, b) == expected


@pytest.mark.parametrize("verdicts,expected", [
    (["TLS_FINGERPRINT"] * 5 + ["IP_REPUTATION"], "TLS_FINGERPRINT"),
    (["IP_REPUTATION"] * 6, "IP_REPUTATION"),
    (["TLS_FINGERPRINT"] * 3 + ["IP_REPUTATION"] * 3, "MIXED"),
    (["NOT_REPRODUCIBLE"] * 5 + ["TLS_FINGERPRINT"], "NOT_REPRODUCIBLE"),
    (["TLS_FINGERPRINT", "IP_REPUTATION", "ERROR", "OTHER(a=429,b=429)"], "INCONCLUSIVE"),
])
def test_f2_aggregate_verdict(verdicts, expected):
    assert mod._aggregate_verdict(verdicts) == expected
```

The diagnostic-against-live-URLs run is NOT a pytest target — manual invocation produces the verdict doc.

---

### F3 — Silent-403 → BOT_BLOCKED classification

**Locate:**

```bash
grep -rn "FetchOutcome\.\|BOT_BLOCKED\|_classify_fetch" ma_poc/fetch/
```

**Fix in `ma_poc/fetch/fetcher.py`:**

```python
_CLOUDFLARE_HEADER_TOKENS = ("cf-ray", "cf-mitigated", "cf-cache-status")


def _has_cloudflare_signature(headers: Mapping[str, str]) -> bool:
    """Case-insensitive header check for Cloudflare edge."""
    lower = {k.lower(): v for k, v in headers.items()}
    if lower.get("server", "").lower() == "cloudflare":
        return True
    return any(t in lower for t in _CLOUDFLARE_HEADER_TOKENS)


def _is_silent_block(status_code: int, headers: Mapping[str, str],
                     body: bytes | str | None) -> bool:
    """A 403 with empty/short body OR Cloudflare-header signature is a silent bot block."""
    if status_code != 403:
        return False
    if _has_cloudflare_signature(headers):
        return True
    if body is None:
        return True
    if isinstance(body, bytes):
        return len(body) < 64
    return len(body.strip()) < 64


def _classify_fetch_outcome(status_code, headers, body, error):
    # ... existing branches unchanged ...

    # NEW: silent-403 / Cloudflare-edge detection BEFORE the generic HTTP_<status> fallthrough.
    # Must run AFTER the existing _looks_like_captcha (which handles 200-status interstitials).
    if _is_silent_block(status_code, headers, body):
        return FetchOutcome.SOFT_FAIL, "BOT_BLOCKED"

    return FetchOutcome.SOFT_FAIL, f"HTTP_{status_code}"
```

**Critical (H14):** legitimate 403 responses with substantive body content (login walls) must not be misclassified. The 64-byte threshold is the discriminator; the Cloudflare-header check is the more reliable signal.

#### Tests — `tests/fetch/test_silent_403_classification.py`

```python
"""F3 — silent-403 / Cloudflare-edge → BOT_BLOCKED."""
from __future__ import annotations
from pathlib import Path
import pytest

from ma_poc.fetch.fetcher import (
    _classify_fetch_outcome, _has_cloudflare_signature, _is_silent_block, FetchOutcome,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
VERDICT_PATH = REPO_ROOT / "docs" / "ANTIBOT_TLS_VERDICT.md"


def test_f3_requires_f2_verdict_present():
    """H2 ordering: F2 must have run before F3 ships."""
    assert VERDICT_PATH.exists(), (
        f"Run `python -m ma_poc.scripts.diagnostics.tls_vs_ip_diagnostic` first. "
        f"Expected {VERDICT_PATH}"
    )
    head = VERDICT_PATH.read_text(encoding="utf-8").splitlines()[:5]
    assert any(ln.strip().startswith("verdict:") for ln in head)


@pytest.mark.parametrize("headers,expected", [
    ({"server": "cloudflare"}, True),
    ({"Server": "Cloudflare"}, True),
    ({"server": "nginx", "cf-ray": "abc123"}, True),
    ({"cf-mitigated": "challenge"}, True),
    ({"server": "nginx"}, False),
    ({}, False),
])
def test_f3_cloudflare_signature_detection(headers, expected):
    assert _has_cloudflare_signature(headers) is expected


@pytest.mark.parametrize("status,headers,body,expected", [
    (403, {"server": "cloudflare"}, b"", True),
    (403, {"cf-ray": "abc"}, b"<html>some content</html>", True),
    (403, {"server": "nginx"}, None, True),
    (403, {"server": "nginx"}, b"", True),
    (403, {"server": "nginx"}, b"   ", True),
    # H14 — legitimate 403 with substantive body
    (403, {"server": "nginx"}, b"<html><form action='/login'>" + b"X"*200 + b"</form></html>", False),
    # 200 unaffected
    (200, {"server": "cloudflare"}, b"", False),
    # 503 handled elsewhere
    (503, {"server": "cloudflare"}, b"", False),
])
def test_f3_is_silent_block_table(status, headers, body, expected):
    assert _is_silent_block(status, headers, body) is expected


def test_f3_403_empty_body_classified_bot_blocked():
    outcome, sig = _classify_fetch_outcome(403, {"server": "nginx"}, b"", None)
    assert outcome == FetchOutcome.SOFT_FAIL
    assert sig == "BOT_BLOCKED"


def test_f3_cf_server_header_classified_bot_blocked():
    _, sig = _classify_fetch_outcome(403, {"server": "cloudflare"}, b"", None)
    assert sig == "BOT_BLOCKED"


def test_f3_legitimate_403_login_wall_not_misclassified():
    body = b"<html><body><h1>Sign in</h1>" + b"X"*500 + b"</body></html>"
    _, sig = _classify_fetch_outcome(403, {"server": "nginx"}, body, None)
    assert sig != "BOT_BLOCKED"
```

---

### F4 — propertyId resolver

**Phase 0 prerequisite (manual):** before writing F4, run a browser session against `https://www.rentcafe.com` and document the search endpoint behavior in `docs/RENTCAFE_DIRECT_RESEARCH.md`. Required sections: ≥5 sample properties (id + name + city + zip + vanity_domain + propertyId), search endpoint method + URL + params + response shape + sample request/response, floorplans endpoint URL pattern + envelope shape + auth required, disambiguation strategy (test by querying for a name that occurs in multiple zips), known failure modes.

If the research finds the search endpoint requires auth or is itself Cloudflare-protected, flag the spec as blocked — F4-F7 depend on a callable search endpoint.

**Module — `ma_poc/pms/rentcafe_direct/propertyid_resolver.py`:**

```python
"""F4 — propertyId resolver via aggregator search.

Disambiguation:
  1. Exact name within same zip                                  → EXACT
  2. Name prefix (first 3 words) within same zip                 → ZIP_AND_NAME_PREFIX
  3. Multiple same-zip candidates, prefer match on vanity_domain → ZIP_AND_NAME_PREFIX
  4. Single result in same zip regardless of name                → ZIP_ONLY
  5. No same-zip result                                          → NONE
"""
from __future__ import annotations
import re
from dataclasses import dataclass
from typing import Literal

import httpx

# Endpoint — UPDATE from Phase 0 research before merging
_AGGREGATOR_SEARCH_URL = "https://www.rentcafe.com/api/search"  # ← VERIFY


@dataclass(frozen=True)
class ResolveResult:
    property_id: str | None
    matched_name: str | None
    matched_zip: str | None
    confidence: Literal["EXACT", "ZIP_AND_NAME_PREFIX", "ZIP_ONLY", "NONE"]
    raw_search_response: dict | None
    failure_reason: str | None  # populated when property_id is None


def _normalize_name(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", (s or "").lower())).strip()


def _normalize_zip(z: str) -> str:
    return (z or "").strip().split("-")[0][:5]


def _name_prefix(s: str, n: int = 3) -> str:
    return " ".join(_normalize_name(s).split()[:n])


async def resolve_property_id(
    property_name: str,
    city: str,
    zip_code: str,
    vanity_domain: str | None = None,
    *,
    timeout_s: float = 15.0,
    client: httpx.AsyncClient | None = None,
) -> ResolveResult:
    target_zip = _normalize_zip(zip_code)
    target_name_norm = _normalize_name(property_name)
    target_prefix = _name_prefix(property_name)

    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)

    try:
        try:
            resp = await client.get(
                _AGGREGATOR_SEARCH_URL,
                params={"q": f"{property_name} {city} {target_zip}".strip()},
            )
        except (httpx.NetworkError, httpx.TimeoutException):
            return ResolveResult(None, None, None, "NONE", None, "SEARCH_NETWORK_ERROR")

        if resp.status_code != 200:
            return ResolveResult(None, None, None, "NONE", None,
                                 f"SEARCH_HTTP_ERROR_{resp.status_code}")

        try:
            payload = resp.json()
        except Exception:
            return ResolveResult(None, None, None, "NONE", None, "SEARCH_PARSE_ERROR")

        # Adapt to actual shape from Phase 0; assumes results: [{property_id, name, zip, vanity_domain}]
        candidates = payload.get("results", []) if isinstance(payload, dict) else []
        if not candidates:
            return ResolveResult(None, None, None, "NONE", payload, "NO_RESULTS")

        same_zip = [c for c in candidates if _normalize_zip(c.get("zip", "")) == target_zip]
        if not same_zip:
            return ResolveResult(None, None, None, "NONE", payload, "NO_ZIP_MATCH")

        # 1. EXACT
        for c in same_zip:
            if _normalize_name(c.get("name", "")) == target_name_norm:
                return ResolveResult(str(c["property_id"]), c.get("name"),
                                     _normalize_zip(c.get("zip", "")),
                                     "EXACT", payload, None)

        # 2. ZIP_AND_NAME_PREFIX (single match)
        prefix_matches = [c for c in same_zip
                         if _name_prefix(c.get("name", "")) == target_prefix]
        if len(prefix_matches) == 1:
            c = prefix_matches[0]
            return ResolveResult(str(c["property_id"]), c.get("name"),
                                 _normalize_zip(c.get("zip", "")),
                                 "ZIP_AND_NAME_PREFIX", payload, None)

        # 3. Multiple prefix matches — vanity_domain disambiguation
        if len(prefix_matches) > 1 and vanity_domain:
            vd_low = vanity_domain.lower()
            vd_match = [c for c in prefix_matches
                       if vd_low in (c.get("vanity_domain", "") or "").lower()]
            if len(vd_match) == 1:
                c = vd_match[0]
                return ResolveResult(str(c["property_id"]), c.get("name"),
                                     _normalize_zip(c.get("zip", "")),
                                     "ZIP_AND_NAME_PREFIX", payload, None)
            return ResolveResult(None, None, None, "NONE", payload, "AMBIGUOUS_NO_TIE_BREAKER")

        # 4. ZIP_ONLY
        if len(same_zip) == 1:
            c = same_zip[0]
            return ResolveResult(str(c["property_id"]), c.get("name"),
                                 _normalize_zip(c.get("zip", "")),
                                 "ZIP_ONLY", payload, None)

        return ResolveResult(None, None, None, "NONE", payload, "AMBIGUOUS_NO_TIE_BREAKER")
    finally:
        if own_client:
            await client.aclose()
```

#### Tests — `tests/pms/rentcafe_direct/test_propertyid_resolver.py`

Six tests covering EXACT match, zip disambiguation of same-name properties, NO_RESULTS, NO_ZIP_MATCH, vanity_domain tie-breaking, and HTTP error classification. Pattern uses `unittest.mock.AsyncMock` to mock `httpx.AsyncClient.get`. Captured search-response fixtures live under `tests/pms/adapters/fixtures/rentcafe_direct/search_responses/`. (Test code follows the same shape as the F5 tests below; see CLAUDE_XSOURCE_PR25_FIXUP.md for the project's preferred test-mocking conventions.)

---

### F5 — Direct fetcher

**Module — `ma_poc/pms/rentcafe_direct/fetcher.py`:**

```python
"""F5 — direct fetcher for RentCafe aggregator API.

Given a propertyId, calls the rentcafe.com floorplans/availability API and
returns responses in the same envelope shape `_api_responses` uses elsewhere.
The existing `rentcafe.py` adapter parses output unchanged (H7).
"""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ma_poc.pms.adapters.rentcafe import _is_rentcafe_response, _unwrap_rentcafe_list

# UPDATE from Phase 0 research
_AGGREGATOR_FLOORPLANS_URL = (
    "https://www.rentcafe.com/wp-json/middleware/v1/getFloorplans/?propertyId[]={pid}"
)

TIER_CODES: frozenset[str] = frozenset({
    "TIER_1_API_RENTCAFE_DIRECT",
    "TIER_1_API_RENTCAFE_DIRECT_LIST_EMPTY",
    "TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE",
    "TIER_1_API_RENTCAFE_DIRECT_SHAPE_REJECTED",
    "TIER_1_API_RENTCAFE_DIRECT_PROPERTY_ID_RESOLUTION_FAILED",
    "TIER_1_API_RENTCAFE_DIRECT_PROPERTY_ID_MISMATCH",
})


@dataclass(frozen=True)
class DirectFetchResult:
    property_id: str
    api_responses: list[dict[str, Any]]
    tier_code: str
    tier_message: str | None
    elapsed_ms: int


async def fetch_direct(
    property_id: str,
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 20.0,
) -> DirectFetchResult:
    started = time.monotonic()
    own_client = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s, follow_redirects=True)
    url = _AGGREGATOR_FLOORPLANS_URL.format(pid=property_id)

    def _ms() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        try:
            resp = await client.get(url)
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            return DirectFetchResult(property_id, [],
                                     "TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE",
                                     f"network_error: {type(e).__name__}", _ms())

        if resp.status_code != 200:
            return DirectFetchResult(property_id, [],
                                     "TIER_1_API_RENTCAFE_DIRECT_NO_RESPONSE",
                                     f"http_{resp.status_code}", _ms())

        try:
            body = resp.json()
        except Exception:
            return DirectFetchResult(property_id, [],
                                     "TIER_1_API_RENTCAFE_DIRECT_SHAPE_REJECTED",
                                     "non_json", _ms())

        if not _is_rentcafe_response(body):
            return DirectFetchResult(property_id, [],
                                     "TIER_1_API_RENTCAFE_DIRECT_SHAPE_REJECTED",
                                     "envelope_did_not_match", _ms())

        items = _unwrap_rentcafe_list(body) or []
        api_response = {"url": url, "body": body, "status": 200,
                       "headers": dict(resp.headers)}

        if not items:
            # Legitimate empty — populate api_responses so the parser sees the empty list
            return DirectFetchResult(property_id, [api_response],
                                     "TIER_1_API_RENTCAFE_DIRECT_LIST_EMPTY",
                                     "floorplans_list_empty", _ms())

        return DirectFetchResult(property_id, [api_response],
                                 "TIER_1_API_RENTCAFE_DIRECT", None, _ms())
    finally:
        if own_client:
            await client.aclose()
```

#### Tests — `tests/pms/rentcafe_direct/test_fetcher.py`

Five tests: success returns `api_responses`, NO_RESPONSE on timeout, SHAPE_REJECTED on non-RentCafe body, LIST_EMPTY populates `api_responses` with 1 entry (empty IS the answer), and the `TIER_CODES` frozenset contains all 6 codes from H8.

#### Shape-equivalence test — `tests/pms/rentcafe_direct/test_shape_equivalence.py`

```python
"""F5 / H7 — direct path output shape-equivalent to existing rentcafe.py adapter.

Parametrized over every captured fixture. Feeds the captured body through
RentCafeAdapter.extract via _api_responses and asserts canonical-key parity.
"""
from __future__ import annotations
import json
from pathlib import Path
import pytest

from ma_poc.pms.adapters.rentcafe import RentCafeAdapter, _is_rentcafe_response
from ma_poc.pms.adapters.base import AdapterContext  # adapt actual import

_FX = Path(__file__).resolve().parents[3] / "tests" / "pms" / "adapters" / "fixtures" / "rentcafe_direct"
_FIXTURES = [p.stem for p in sorted(_FX.glob("*.json")) if not p.name.endswith(".meta.json")]


@pytest.mark.parametrize("canonical_id", _FIXTURES)
def test_f5_unit_shape_matches_rentcafe_adapter(canonical_id: str):
    body = json.loads((_FX / f"{canonical_id}.json").read_text(encoding="utf-8"))
    meta = json.loads((_FX / f"{canonical_id}.meta.json").read_text(encoding="utf-8"))
    assert _is_rentcafe_response(body), \
        f"Fixture {canonical_id} not RentCafe-shaped — re-capture or update detector"

    ctx = AdapterContext(
        base_url=meta["capture_url"],
        property_id=meta["expected_property_id"],
        property_name=meta["property_name"],
        city=meta["city"], state=meta.get("state", ""),
        zip_code=meta["zip"], pmc=meta.get("pmc", ""),
    )
    ctx._api_responses = [{"url": meta["capture_url"], "body": body, "status": 200}]

    result = RentCafeAdapter().extract(ctx)
    assert result.units, f"{canonical_id}: zero units"
    canonical_keys = {"unit_id", "unit_number", "floor_plan_name", "beds", "baths",
                      "sqft", "rent", "available_date"}
    for unit in result.units:
        present = canonical_keys & set(unit.keys())
        assert present == canonical_keys, \
            f"{canonical_id}: missing keys {canonical_keys - present}"
```

---

### F6 — Profile field + runner integration

**Schema patch — `ma_poc/models/scrape_profile.py`:**

```python
class ApiHints(BaseModel):
    # ... existing fields ...
    rentcafe_property_id: str | None = None  # NEW
```

`ConfigDict(extra="ignore")` on the model (already set per project conventions) means legacy profiles default to `None`. No migration needed.

**Writer — `ma_poc/services/profile_updater.py`:**

```python
def update_profile_after_extraction(profile, result):
    # ... existing logic ...

    # NEW: persist rentcafe propertyId on direct-path success.
    # Best-effort (H12) — never raise. Only writes on success tiers (H13).
    try:
        pid = getattr(result, "_rentcafe_property_id", None)
        tier = getattr(result, "tier_used", "")
        if pid and tier in ("TIER_1_API_RENTCAFE_DIRECT", "TIER_1_API_RENTCAFE_DIRECT_LIST_EMPTY"):
            profile.api_hints.rentcafe_property_id = pid
    except Exception as e:
        logger.warning("failed to persist rentcafe_property_id: %s", e)
```

**Runner integration — `ma_poc/scripts/jugnu_runner.py`:**

```python
async def _process_property(prop_row, profile):
    fetch_result = None

    # NEW: rentcafe_direct dispatch (H4, H5)
    if profile and profile.api_provider == "rentcafe":
        cached_id = profile.api_hints.rentcafe_property_id

        if cached_id is None:
            resolve_r = await resolve_property_id(
                property_name=prop_row.get("Property Name", ""),
                city=prop_row.get("City", ""),
                zip_code=prop_row.get("ZIP Code", ""),
                vanity_domain=_extract_domain(prop_row.get("Website")),
            )
            cached_id = resolve_r.property_id

        if cached_id is not None:
            direct_r = await fetch_direct(cached_id)
            if direct_r.tier_code in (
                "TIER_1_API_RENTCAFE_DIRECT",
                "TIER_1_API_RENTCAFE_DIRECT_LIST_EMPTY",
            ):
                fetch_result = _direct_to_fetch_result(direct_r)
                fetch_result._rentcafe_property_id = cached_id

    # EXISTING: vanity-domain fallback (H4) — unconditional on direct-path failure
    if fetch_result is None:
        fetch_result = await _existing_vanity_fetch(prop_row, profile)

    # EXISTING: L3 cascade unchanged
    return scrape_jugnu(fetch_result, profile, ...)
```

#### Tests — `tests/integration/test_rentcafe_direct_routing.py`

Eight tests covering H4/H5/H6/H11/H12/H13:

- `test_f6_direct_runs_before_vanity_when_id_cached` — H5
- `test_f6_falls_back_on_resolution_failure` — H4 (resolve fails)
- `test_f6_falls_back_on_metadata_mismatch` — H4 (resolve returns ZIP_ONLY with wrong name)
- `test_f6_falls_back_on_direct_fetch_failure` — H4 (direct fetch returns NO_RESPONSE)
- `test_f6_non_rentcafe_unchanged` — direct path bypassed entirely for non-rentcafe
- `test_f6_propertyid_persisted_after_success` — H6
- `test_f6_second_run_uses_cache` — H6 (second run does NOT call resolve)
- `test_f6_propertyid_not_persisted_on_failure` — H13 (existing id not overwritten)
- `test_f6_profile_write_swallows_exception` — H12 (best-effort)
- `test_f6_schema_field_has_writer_and_reader` — H11 (static scan)

The H11 test is a static scan:

```python
def test_f6_schema_field_has_writer_and_reader():
    repo = Path(__file__).resolve().parents[3]
    writer_files, reader_files = [], []
    for f in (repo / "ma_poc").rglob("*.py"):
        if "tests" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        rel = str(f.relative_to(repo))
        if "rentcafe_property_id =" in text or ".rentcafe_property_id =" in text:
            writer_files.append(rel)
        # Reader: dot-access not followed by `=`
        for line in text.splitlines():
            if ".rentcafe_property_id" in line and "=" not in line.split(".rentcafe_property_id")[1][:5]:
                reader_files.append(rel)
                break
    assert writer_files, "H11: no writer for rentcafe_property_id"
    assert reader_files, "H11: no reader for rentcafe_property_id"
```

---

### F7 — Production smoke validation

**CLI — `ma_poc/scripts/smoke_rentcafe_direct.py`:** runs the direct path against 50 properties from `bot_blocked_properties_latest.json` (filtered to RentCafe-family URL classification per the analysis methodology). Inputs: that JSON file + the production property metadata. Output: `data/smoke/rentcafe_direct_smoke.json` with one entry per property:

```json
{
  "canonical_id": "...",
  "resolution": "EXACT|ZIP_AND_NAME_PREFIX|ZIP_ONLY|NONE|CACHED",
  "direct_tier": "TIER_1_API_RENTCAFE_DIRECT|...",
  "units_extracted": <int>,
  "elapsed_ms": <int>
}
```

#### Test — `tests/integration/test_rentcafe_direct_smoke.py`

```python
"""F7 — smoke threshold (H9): ≥30 of 50 produce ≥1 unit via direct path."""
from __future__ import annotations
import json
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SMOKE = REPO_ROOT / "data" / "smoke" / "rentcafe_direct_smoke.json"


def test_f7_smoke_threshold_30_of_50():
    assert SMOKE.exists(), (
        f"Run `python -m ma_poc.scripts.smoke_rentcafe_direct` first. Expected {SMOKE}"
    )
    data = json.loads(SMOKE.read_text(encoding="utf-8"))
    assert isinstance(data, list) and len(data) == 50, \
        f"Expected exactly 50 entries; got {len(data) if isinstance(data, list) else type(data).__name__}"
    successes = sum(1 for r in data if r["units_extracted"] > 0)
    failure_modes = Counter(r["direct_tier"] for r in data if r["units_extracted"] == 0)
    assert successes >= 30, (
        f"H9 threshold not met: {successes}/50. Failure modes: {dict(failure_modes)}"
    )
```

---

## 6. Gate runner

`ma_poc/scripts/gate_antibot_fixes.py`:

```python
#!/usr/bin/env python3
"""Anti-bot + rentcafe_direct gate runner."""
from __future__ import annotations
import argparse, re, subprocess, sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPOC = REPO_ROOT / "ma_poc"

FIX_TESTS: dict[str, list[str]] = {
    "F1": ["tests/resolver/test_path_blacklist.py"],
    "F2": ["tests/diagnostics/test_tls_vs_ip_diagnostic.py"],
    "F3": ["tests/fetch/test_silent_403_classification.py"],
    "F4": ["tests/pms/rentcafe_direct/test_propertyid_resolver.py"],
    "F5": ["tests/pms/rentcafe_direct/test_fetcher.py",
           "tests/pms/rentcafe_direct/test_shape_equivalence.py"],
    "F6": ["tests/integration/test_rentcafe_direct_routing.py"],
    "F7": ["tests/integration/test_rentcafe_direct_smoke.py"],
}

# H10 — files PERMITTED to mention RentCafe-specific strings
RENTCAFE_PERMITTED = {
    "ma_poc/pms/adapters/rentcafe.py",
    "ma_poc/pms/detector.py",
    "ma_poc/pms/rentcafe_direct/__init__.py",
    "ma_poc/pms/rentcafe_direct/propertyid_resolver.py",
    "ma_poc/pms/rentcafe_direct/fetcher.py",
    "ma_poc/scripts/diagnostics/tls_vs_ip_diagnostic.py",
    "ma_poc/scripts/smoke_rentcafe_direct.py",
    "ma_poc/scripts/jugnu_runner.py",
    "ma_poc/services/profile_updater.py",
    "ma_poc/models/scrape_profile.py",
}
RENTCAFE_TOKENS = ("rentcafe.com", "securecafe.com", "RentCafe", "rentcafe_property_id")


def _check_static_invariants() -> tuple[bool, list[str]]:
    log: list[str] = []
    ok = True

    # H1
    offenders = []
    for f in MAPOC.rglob("*.py"):
        if "tests" in f.parts or "scripts" in f.parts:
            continue
        if "rental_applications" in f.read_text(encoding="utf-8"):
            offenders.append(str(f.relative_to(REPO_ROOT)))
    if offenders != ["ma_poc/pms/resolver.py"]:
        log.append(f"  H1 FAIL: rental_applications in {offenders}"); ok = False
    else:
        log.append("  H1 (single source of truth): PASS")

    # H2
    verdict = REPO_ROOT / "docs" / "ANTIBOT_TLS_VERDICT.md"
    if not verdict.exists():
        log.append(f"  H2 FAIL: verdict missing at {verdict}"); ok = False
    else:
        head = verdict.read_text(encoding="utf-8").splitlines()[:5]
        if any(ln.strip().startswith("verdict:") for ln in head):
            log.append("  H2 (verdict structured): PASS")
        else:
            log.append("  H2 FAIL: missing structured `verdict:` header"); ok = False

    # H10 — no LLM imports in rentcafe_direct
    forbidden = re.compile(r"^\s*(import openai|import anthropic|from\s+ma_poc\.services\.llm)",
                           re.MULTILINE)
    rcd = MAPOC / "pms" / "rentcafe_direct"
    if rcd.exists():
        leak = next((f for f in rcd.rglob("*.py")
                     if forbidden.search(f.read_text(encoding="utf-8"))), None)
        if leak:
            log.append(f"  H10 FAIL: LLM import in {leak.relative_to(REPO_ROOT)}"); ok = False
        else:
            log.append("  H10 (no LLM imports): PASS")

    # H10 — no RentCafe string leakage
    leakers = []
    for f in MAPOC.rglob("*.py"):
        rel = str(f.relative_to(REPO_ROOT))
        if rel in RENTCAFE_PERMITTED or "tests" in f.parts:
            continue
        text = f.read_text(encoding="utf-8")
        for tok in RENTCAFE_TOKENS:
            if tok in text:
                leakers.append((rel, tok)); break
    if leakers:
        for rel, tok in leakers:
            log.append(f"  H10 FAIL: {tok!r} in {rel}")
        ok = False
    else:
        log.append("  H10 (no string leakage): PASS")

    # H11 — writer + reader
    pu = MAPOC / "services" / "profile_updater.py"
    rn = MAPOC / "scripts" / "jugnu_runner.py"
    has_writer = pu.exists() and "rentcafe_property_id" in pu.read_text(encoding="utf-8")
    has_reader = rn.exists() and "rentcafe_property_id" in rn.read_text(encoding="utf-8")
    if has_writer and has_reader:
        log.append("  H11 (writer + reader): PASS")
    else:
        log.append(f"  H11 FAIL: writer={has_writer} reader={has_reader}"); ok = False

    return ok, log


def _run_pytest(targets: list[str]) -> tuple[bool, list[str]]:
    if not targets:
        return True, []
    cmd = [sys.executable, "-m", "pytest", "--tb=short", "-q"] + [str(MAPOC / t) for t in targets]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT)
    out = [r.stdout[-2500:] if r.stdout else ""]
    if r.returncode != 0:
        out.append(r.stderr[-1500:] if r.stderr else "")
        return False, out
    return True, out


def run_fix(fix: str) -> bool:
    print(f"\n{'='*60}\nFix {fix}\n{'='*60}")
    ok, lines = _run_pytest(FIX_TESTS.get(fix, []))
    for ln in lines: print(ln)
    if not ok:
        print(f"Fix {fix}: FAIL"); return False
    print(f"Fix {fix}: PASS"); return True


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["phase", "all", "static"])
    p.add_argument("phase", nargs="?", default=None)
    args = p.parse_args()

    if args.command == "static":
        ok, lines = _check_static_invariants()
        for ln in lines: print(ln)
        sys.exit(0 if ok else 1)

    if args.command == "phase":
        if args.phase not in FIX_TESTS:
            print(f"Unknown fix '{args.phase}'. Valid: {list(FIX_TESTS)}", file=sys.stderr)
            sys.exit(1)
        sys.exit(0 if run_fix(args.phase) else 1)

    # `all`
    print("\n" + "="*60 + "\nStatic invariants\n" + "="*60)
    ok_static, lines = _check_static_invariants()
    for ln in lines: print(ln)
    if not ok_static:
        sys.exit(1)
    for fix in ["F1", "F2", "F3", "F4", "F5", "F6", "F7"]:
        if not run_fix(fix):
            sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
```

---

## 7. Definition of Done

```bash
# All fix-level tests + static invariants
python ma_poc/scripts/gate_antibot_fixes.py all

# No regression
pytest . --ignore=data --ignore=config

# Type checking
mypy --strict ma_poc/pms/rentcafe_direct
```

All three exit 0. `docs/ANTIBOT_TLS_VERDICT.md` and `docs/RENTCAFE_DIRECT_RESEARCH.md` are committed and complete. F7 smoke output (`data/smoke/rentcafe_direct_smoke.json`) is committed with ≥30/50 successes.

---

## 8. Self-review checklist

After Claude Code reports the implementation as done, walk this checklist before opening the PR. The point is to catch the failure modes that pass tests but drift from spec intent. Every item has a specific verification — no vibes. This is meant to be runnable by a fresh Claude session pointed at the diff, the spec, and a checkout of the branch.

### 8.1 Spec-vs-code conformance

The most common failure mode is "tests pass, but the implementation took a shortcut the spec rules out." Run these greps in order.

```bash
# (a) H7 — direct path REUSES rentcafe.py parser, doesn't reimplement.
# Should print exactly the import line. If anything else shows up, the
# implementer wrote their own parser.
grep -n "from ma_poc.pms.adapters.rentcafe import" ma_poc/pms/rentcafe_direct/fetcher.py

# (b) H8 — exact tier-code strings. Common drift: TIER_1_API_RENTCAFE_DIRECT
# renamed to TIER_1_API_RENTCAFE_DIRECT_OK, _SUCCESS etc.
grep -E "TIER_1_API_RENTCAFE_DIRECT" ma_poc/pms/rentcafe_direct/fetcher.py

# (c) Disambiguation order matches §5/F4. Read propertyid_resolver.py top-to-
# bottom and confirm the four branches fire in EXACT, ZIP_AND_NAME_PREFIX,
# ZIP_ONLY, NONE order.

# (d) H4 — fallback is unconditional. Look at jugnu_runner._process_property:
# the `if fetch_result is None: ... vanity ...` branch must run on every
# direct-path failure path. If the implementer added an early-return that
# skips vanity for some failure tier, the test passes but H4 is broken.
grep -A3 "fetch_result is None" ma_poc/scripts/jugnu_runner.py
```

### 8.2 Hard-invariant audit

Walk the H1-H14 table from §2 and verify each invariant has BOTH a test referenced AND that test actually asserts the invariant. Common failure: a test named `test_f6_propertyid_persisted` whose assertion is unrelated to persistence.

```bash
# Print invariant test names and verify each maps to a real assertion
pytest --collect-only -q ma_poc/tests/ | grep -E "test_(f1|f2|f3|f4|f5|f6|f7)_"
```

For each test, open it and confirm the assertion enforces what the H-row claims. Pay attention to:

- **H4 fallback tests** — confirm BOTH `_existing_vanity_fetch.assert_called_once()` AND that no records were dropped.
- **H6 cache test** — confirm `resolve_property_id.assert_not_called()`, not just that fetch_direct was called.
- **H7 shape test** — confirm parametrization actually iterates ≥5 fixtures (not silently empty because the fixture path is wrong).
- **H11 writer/reader test** — confirm both lists are non-empty (not just one).
- **H12 best-effort test** — confirm the test ACTUALLY raises in the writer path. If it only mocks something that returns None, the test is theatre.
- **H13 no-overwrite test** — confirm the test starts with a value other than what would be written, so an erroneous overwrite would be visible.

### 8.3 Project-convention conformance

```bash
# Pydantic v2 — no .dict() in new code
grep -rn "\.dict()" ma_poc/pms/rentcafe_direct/ ma_poc/scripts/diagnostics/ \
                   ma_poc/scripts/smoke_rentcafe_direct.py
# Expected: empty

# Hashing convention — sha256[:16], not hash() or md5
grep -rEn "hashlib\.(md5|sha1)\(|^[^#]*\bhash\(" ma_poc/pms/rentcafe_direct/
# Expected: empty

# Async hygiene — context.close() not browser.close()
grep -rn "browser\.close()" ma_poc/pms/rentcafe_direct/
# Expected: empty (precautionary; no Playwright in this code)

# Static invariants gate
python ma_poc/scripts/gate_antibot_fixes.py static

# Best-effort profile writes — every write to profile.api_hints inside try/except
grep -B2 -A6 "rentcafe_property_id\s*=" ma_poc/services/profile_updater.py
# Verify each assignment is inside a try block
```

### 8.4 Spec invariants the gate doesn't enforce

Read these by hand:

- **F4 vanity_domain disambiguation.** Spec says `len(prefix_matches) > 1 AND vanity_domain` triggers vanity disambiguation. Confirm in `propertyid_resolver.py`: (a) the check is `> 1` not `>= 1`, (b) substring match is case-insensitive, (c) the candidate's `vanity_domain` field is being checked, not the request's.
- **F5 `_LIST_EMPTY` populates `api_responses`.** Empty IS the answer. Confirm `fetch_direct` returns `api_responses=[api_response]` (non-empty containing the one captured response), not `api_responses=[]`.
- **F6 routing precedence.** Read `_process_property` and trace by hand: when `cached_id is not None` and direct fetch succeeds, does the code unconditionally skip vanity? Or did the implementer add a "but also check vanity in parallel" speculation? Spec is direct-OR-vanity, never both.
- **F7 smoke threshold gate is real.** Open `data/smoke/rentcafe_direct_smoke.json` and count successes manually. Confirm the test isn't pinned to `≥0` or skipped via `@pytest.mark.skipif`.

### 8.5 Drift-mode checklist

| Drift                                                                                                                                        | How to check                                                            |
| -------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------- |
| Implementer added LLM call to "improve" propertyId resolution                                                                                | `grep -rE "openai\|anthropic\|llm" ma_poc/pms/rentcafe_direct/`         |
| Implementer added in-memory propertyId cache outside profile                                                                                 | `grep -rE "_PROPERTY_ID_CACHE\|@functools\.lru_cache" ma_poc/pms/rentcafe_direct/` |
| Implementer renamed tier sub-codes ("simpler names")                                                                                         | confirm `TIER_CODES` frozenset matches H8 exactly                       |
| Implementer dropped the EXACT confidence tier ("ZIP_AND_NAME_PREFIX is enough")                                                              | `grep -c "EXACT" ma_poc/pms/rentcafe_direct/propertyid_resolver.py` ≥ 2 |
| Implementer made the fallback conditional on tier-code allow-list                                                                            | trace `_process_property` by hand                                       |
| Implementer changed F2 diagnostic to use a smaller/different URL set                                                                         | confirm `DIAGNOSTIC_URLS` has exactly the 6 entries from §5/F2          |
| Implementer made F3 silent-403 detection broader (e.g., any non-2xx empty body)                                                              | read `_is_silent_block` — only `status_code == 403` should trigger      |
| Implementer added `except Exception` around `_process_property` swallowing all errors                                                        | `grep -E "except Exception" ma_poc/scripts/jugnu_runner.py`             |
| Implementer made the smoke test read fewer than 50 properties                                                                                | confirm `len(data) == 50` assertion in F7 test                          |
| Implementer hardcoded the smoke output to `units_extracted > 0` for all entries                                                              | spot-check 5 random entries by eye                                      |

### 8.6 Schema-writer-reader audit (project-wide H11 pattern)

Per the user-memory invariant: "Schema → writer → reader — confirm ALL THREE before merging."

```bash
# Schema declaration
grep -n "rentcafe_property_id" ma_poc/models/scrape_profile.py
# Expected: 1 line, default = None

# Writer (must populate the field)
grep -n "rentcafe_property_id\s*=" ma_poc/services/profile_updater.py
# Expected: ≥1 assignment, inside try/except

# Reader (must consume the field)
grep -n "\.rentcafe_property_id" ma_poc/scripts/jugnu_runner.py
# Expected: ≥1 read access (no `=` immediately after)

# All three present? If not, the field is dead weight.
```

### 8.7 PR description checklist

The PR description must:

- List F1-F7 with the H-numbers each verifies.
- Quote the F2 verdict from `docs/ANTIBOT_TLS_VERDICT.md` (TLS_FINGERPRINT / IP_REPUTATION / MIXED / NOT_REPRODUCIBLE / INCONCLUSIVE).
- Quote the F7 smoke result, e.g., `33/50; failure modes: PROPERTY_ID_RESOLUTION_FAILED=12, SHAPE_REJECTED=5`.
- NOT claim a properties-recovered uplift number — that requires a follow-up production run.
- Link the analysis report and the prior-conversation summary that motivated the spec.

### 8.8 If the F2 verdict was IP_REPUTATION

The strategic premise is that `rentcafe_direct` (F4-F6) bypasses the per-vanity Cloudflare wall by hitting `rentcafe.com` from the same Cloud Run egress. If F2 verdict is `IP_REPUTATION`, that egress is on Cloudflare's deny list, and the aggregator endpoint may itself be blocked. Phase 0 research should have caught this (the research script must successfully reach the aggregator). If it didn't, F4-F6 will look fine in tests (mocked) and fail in F7 smoke.

Specifically check:

- Did the F7 smoke run actually call the aggregator from production-equivalent egress (Cloud Run, not your laptop)?
- Is `PROPERTY_ID_RESOLUTION_FAILED` the dominant failure mode in `data/smoke/rentcafe_direct_smoke.json`? If so, the verdict and the spec are inconsistent and the PR shouldn't merge until egress is resolved.

If F2 verdict was `TLS_FINGERPRINT`, F4-F6 are still valuable, but a `curl_cffi`-based fetcher (replacing `httpx` in F5) is a worthwhile follow-up — note as `# PR-FUTURE-WORK:` in the PR description.

---

## 9. Anti-scope creep

`# PR-FUTURE-WORK:` and move on if tempted by:

- Generalizing propertyId resolution to other PMSes.
- Replacing `rentcafe.py` parser with one tailored to the aggregator (the shape-equivalence test exists specifically to confirm reuse is correct).
- LLM-based propertyId resolution as a fallback. Resolver should hit ≥80% deterministically; if not, the fix is better disambiguation logic.
- Caching propertyId at process level. Profile is the only correct cache.
- Treating `LIST_EMPTY` as a failure and falling back. Empty IS the answer.
- Generalizing `_is_silent_block` to non-403 status codes.
- Adding more entries to the path blacklist beyond AppFolio rental_applications.

---

## 10. Rollout

Single PR. After merge:

1. Manual production run, 10 shards, full portfolio.
2. Compare bot-blocked counts and per-property fetch tier outcomes vs the 2026-05-04 baseline.

Expected deltas (informational, not gates):
- AppFolio reCAPTCHA cluster: 10 → 0.
- Properties classified `BOT_BLOCKED` at DIRECT tier: increases (silent-403s now classified).
- Properties escalating to higher fetch tier: increases by the silent-403 count.
- RentCafe-family properties extracting via direct path: ~150-200 (depends on smoke success rate scaled to portfolio).

Watch the bot-block delta and rentcafe_direct success rate for the first week. If F2 verdict was `IP_REPUTATION` and aggregator access is blocked from production egress, the rentcafe_direct path will silently produce zero successes in production despite tests passing — re-verify smoke output is from production-equivalent egress before declaring rollout successful.