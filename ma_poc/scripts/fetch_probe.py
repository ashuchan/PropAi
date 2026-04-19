"""Fetch probe — diagnostic A/B harness for L1 fetch wait strategies.

Seeds URLs from a prior run (filtered by any field in properties.json's
`_extract_result` / `_meta`) or an explicit URL list. For each (url,
strategy) pair, runs one navigation with that wait strategy, *always*
salvages whatever it can on timeout (page.content() + network_log),
and classifies the outcome.

Reusable beyond the current bug: any "pattern X is failing" investigation
becomes `fetch_probe.py --from-run <dir> --filter tier=<x> --strategies ...`.

Usage
-----
    python ma_poc/scripts/fetch_probe.py \\
        --from-run ma_poc/data/v2/runs/2026-04-19 \\
        --filter tier=generic:no_body_short_circuit \\
        --strategies networkidle:35000,domcontentloaded:15000+5s,load:20000+3s \\
        --max-urls 5

    python ma_poc/scripts/fetch_probe.py \\
        --url https://www.ariaatella.com/ \\
        --strategies domcontentloaded:15000+5s

Outputs
-------
    ma_poc/data/probe_runs/{ts}/
        {property_id}__{strategy_slug}.json   # full capture per cell
        summary.md                            # matrix
        verdict.json                          # counts per strategy
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_MA_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_MA_POC_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT.parent))

from ma_poc.fetch.browser_pool import BrowserContextPool
from ma_poc.fetch.stealth import IdentityPool

log = logging.getLogger("fetch_probe")

# ---------------------------------------------------------------------------
# Selectors — recognizable PMS-rendered unit markers. Used as a cheap proxy
# for "DOM actually rendered something extractable" without invoking an adapter.
# ---------------------------------------------------------------------------
_UNIT_SELECTORS: list[str] = [
    ".unitContainer", ".pricingWrapper", ".unit-number", ".unitNumber",
    ".floorplanName", ".rent",
    ".entrata-unit-row", ".unit-price", ".unit-availability",
    ".js-listing-card", ".listing-unit-detail-table",
    "[data-unit-id]", "[data-floor-plan]", "[data-floorplan]",
    "iframe[src*='sightmap']", "iframe[src*='rentcafe']",
]

# Body-substring heuristics for JSON payloads that look unit-bearing.
_UNIT_BODY_HINTS: tuple[str, ...] = (
    "unitNumber", "unit_number", "floorPlan", "floor_plan",
    "availability", "askingRent", "asking_rent", "pricing", "rentAmount",
)

# Keywords that suggest a link on the homepage leads to a unit-bearing page.
_UNIT_LINK_KEYWORDS: tuple[str, ...] = (
    "floorplan", "floor-plan", "floor_plan", "plans", "plan",
    "apartment", "apartments", "unit", "units",
    "pricing", "availability", "avail",
    "rental", "rentals", "rent", "residence", "residences",
    "lease", "leasing", "homes",
)


def extract_unit_link_candidates(
    html: str, base_url: str, max_n: int
) -> list[str]:
    """Pick same-host <a> links whose path/text suggests unit data.

    Score = 2x keyword hits in URL path + 1x keyword hits in link text.
    Dedup by normalized path; return the top `max_n` URLs (fragment-stripped).
    Returns [] if parsing fails or no candidates score > 0.
    """
    from urllib.parse import urljoin, urlparse

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("bs4 not installed; link-follow disabled.")
        return []

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        try:
            soup = BeautifulSoup(html, "html.parser")
        except Exception as exc:
            log.warning("link extraction failed: %s", exc)
            return []

    base_host = urlparse(base_url).netloc.lower()
    scored: dict[str, tuple[int, str]] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        abs_url = urljoin(base_url, href).split("#", 1)[0]
        parsed = urlparse(abs_url)
        if parsed.scheme not in ("http", "https"):
            continue
        if parsed.netloc.lower() != base_host:
            continue
        path = (parsed.path or "/").lower()
        text = (a.get_text(separator=" ", strip=True) or "").lower()
        score = 0
        for kw in _UNIT_LINK_KEYWORDS:
            if kw in path:
                score += 2
            if kw in text:
                score += 1
        if score == 0:
            continue
        dedup_key = (parsed.path or "/").rstrip("/").lower() or "/"
        if dedup_key in scored and scored[dedup_key][0] >= score:
            continue
        scored[dedup_key] = (score, abs_url)

    ranked = sorted(scored.values(), key=lambda t: -t[0])
    return [u for _, u in ranked[:max_n]]


@dataclass
class Strategy:
    """A wait strategy to probe."""

    wait_until: str              # "domcontentloaded" | "load" | "networkidle" | "commit"
    timeout_ms: int
    post_sleep_s: float = 0.0

    @property
    def slug(self) -> str:
        s = f"{self.wait_until}-{self.timeout_ms}ms"
        if self.post_sleep_s:
            s += f"+{self.post_sleep_s:g}s"
        return s


_STRATEGY_RE = re.compile(
    r"^(?P<wait>domcontentloaded|load|networkidle|commit):(?P<ms>\d+)(?:\+(?P<sleep>\d+(?:\.\d+)?)s)?$"
)


def parse_strategies(spec: str) -> list[Strategy]:
    """Parse `wait:ms[+sleep_s]` comma-separated spec.

    Example: `networkidle:35000,domcontentloaded:15000+5s,load:20000+3s`.

    Raises:
        ValueError: if any strategy token is malformed.
    """
    out: list[Strategy] = []
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        m = _STRATEGY_RE.match(tok)
        if not m:
            raise ValueError(
                f"Bad strategy token: {tok!r}. "
                "Expected e.g. domcontentloaded:15000+5s"
            )
        out.append(Strategy(
            wait_until=m.group("wait"),
            timeout_ms=int(m.group("ms")),
            post_sleep_s=float(m.group("sleep")) if m.group("sleep") else 0.0,
        ))
    if not out:
        raise ValueError("No strategies parsed.")
    return out


# ---------------------------------------------------------------------------
# URL seeding
# ---------------------------------------------------------------------------

def _row_matches(row: dict[str, Any], key: str, value: str) -> bool:
    """Check whether `key=value` (dotted path) holds in a properties.json row."""
    # Supported dotted paths we care about:
    #   tier                       -> row["_extract_result"]["tier_used"]
    #   verdict                    -> row["_meta"]["verdict"]
    #   carry_forward_reason       -> row["_meta"]["carry_forward_reason"]
    #   scrape_outcome             -> row["_meta"]["scrape_outcome"]
    # Or any literal dotted path starting with _meta./_extract_result./<col>.
    aliases = {
        "tier": ("_extract_result", "tier_used"),
        "verdict": ("_meta", "verdict"),
        "verdict_reason": ("_meta", "verdict_reason"),
        "carry_forward_reason": ("_meta", "carry_forward_reason"),
        "scrape_outcome": ("_meta", "scrape_outcome"),
    }
    path = aliases.get(key, tuple(key.split(".")))
    cur: Any = row
    for p in path:
        if not isinstance(cur, dict):
            return False
        cur = cur.get(p)
        if cur is None:
            return False
    return str(cur) == value


def load_urls_from_run(
    run_dir: Path, filter_expr: str | None, max_urls: int | None
) -> list[tuple[str, str]]:
    """Seed URLs from a prior run's properties.json, optionally filtered."""
    props_path = run_dir / "properties.json"
    rows = json.loads(props_path.read_text(encoding="utf-8"))
    filters: list[tuple[str, str]] = []
    if filter_expr:
        for tok in filter_expr.split(","):
            if "=" not in tok:
                raise ValueError(f"Bad filter: {tok!r}. Expected key=value.")
            k, v = tok.split("=", 1)
            filters.append((k.strip(), v.strip()))

    out: list[tuple[str, str]] = []
    for r in rows:
        if not all(_row_matches(r, k, v) for k, v in filters):
            continue
        url = r.get("website") or r.get("url")
        pid = str(r.get("apartment_id") or r.get("_meta", {}).get("canonical_id") or "unknown")
        if not url:
            continue
        out.append((pid, url))
        if max_urls and len(out) >= max_urls:
            break
    return out


def load_urls_from_file(path: Path) -> list[tuple[str, str]]:
    """Load `id,url` pairs (or bare URLs) from a text file, one per line."""
    out: list[tuple[str, str]] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "," in line:
            pid, url = [p.strip() for p in line.split(",", 1)]
        else:
            pid, url = f"url{i:03d}", line
        out.append((pid, url))
    return out


def load_api_patterns(catalogue_path: Path) -> list[re.Pattern[str]]:
    """Compile the url_regex list from api_catalogue.json."""
    try:
        data = json.loads(catalogue_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not load api_catalogue: %s", exc)
        return []
    patterns = []
    for entry in data.get("patterns", []):
        rx = entry.get("url_regex")
        if rx:
            try:
                patterns.append(re.compile(rx, re.IGNORECASE))
            except re.error as exc:
                log.warning("Bad regex %s: %s", rx, exc)
    return patterns


# ---------------------------------------------------------------------------
# Probe core
# ---------------------------------------------------------------------------

@dataclass
class ProbeCapture:
    property_id: str
    url: str
    strategy: str
    started_at: str
    # Page origin within the property. "home" = initial URL, "L1:<slug>" = a
    # link discovered from the homepage body under --follow-links.
    page_key: str = "home"
    parent_url: str | None = None
    # Wall-clock milestones relative to t0, populated via page events.
    t_domcontentloaded_ms: int | None = None
    t_load_ms: int | None = None
    t_networkidle_ms: int | None = None   # only set if strategy demanded it
    # Navigation outcome.
    nav_completed: bool = False
    nav_timeout: bool = False
    nav_error: str | None = None
    elapsed_ms: int = 0
    # Salvage.
    final_url: str | None = None
    http_status: int | None = None
    body_bytes: int = 0
    body_snippet: str = ""
    selector_hits: dict[str, int] = field(default_factory=dict)
    # Network capture (summary — full bodies not persisted for size).
    network_log_count: int = 0
    network_log_content_types: dict[str, int] = field(default_factory=dict)
    api_catalogue_matches: int = 0
    api_matches_with_unit_hints: int = 0
    network_sample: list[dict[str, Any]] = field(default_factory=list)
    # Classification (terminal).
    classification: str = "UNKNOWN"


def _classify(cap: ProbeCapture) -> str:
    """Map capture → terminal verdict. Order matters; first match wins."""
    if cap.nav_error and not cap.nav_completed and cap.body_bytes == 0:
        if "ssl" in (cap.nav_error or "").lower() or "dns" in (cap.nav_error or "").lower():
            return "HARD_FAIL"
        if cap.nav_timeout:
            return "TIMEOUT_NO_SALVAGE"
        return "HARD_FAIL"
    has_dom = cap.body_bytes >= 5000
    sel_hit = sum(cap.selector_hits.values()) > 0
    api_hit = cap.api_matches_with_unit_hints > 0
    if has_dom and (sel_hit or api_hit):
        return "RECOVERABLE"
    if has_dom and not (sel_hit or api_hit):
        return "DOM_NO_UNIT_MARKERS"
    return "DOM_EMPTY"


async def probe_once(
    pool: BrowserContextPool,
    identities: IdentityPool,
    property_id: str,
    url: str,
    strategy: Strategy,
    api_patterns: list[re.Pattern[str]],
    page_key: str = "home",
    parent_url: str | None = None,
) -> tuple[ProbeCapture, str]:
    """Run a single (url, strategy) probe and always salvage what's possible.

    Returns (capture, full_html). The full HTML is returned so the caller can
    run link-extraction on it without also persisting the full body to disk.
    """
    cap = ProbeCapture(
        property_id=property_id, url=url, strategy=strategy.slug,
        started_at=datetime.now(timezone.utc).isoformat(),
        page_key=page_key, parent_url=parent_url,
    )
    full_html = ""
    identity = identities.pick(sticky_key=property_id)
    page = await pool.acquire(identity, proxy=None)

    t0 = time.monotonic()

    def _mark(attr: str) -> None:
        if getattr(cap, attr) is None:
            setattr(cap, attr, int((time.monotonic() - t0) * 1000))

    # Load-state milestones — these fire regardless of goto's wait_until.
    page.on("domcontentloaded", lambda _p: _mark("t_domcontentloaded_ms"))
    page.on("load", lambda _p: _mark("t_load_ms"))

    network_log: list[dict[str, Any]] = []

    async def _on_response(response: Any) -> None:
        try:
            ct = response.headers.get("content-type", "")
            if not any(t in ct for t in ("json", "xml", "html", "text")):
                return
            try:
                body = await response.body()
            except Exception:
                body = b""
            body_text = body.decode("utf-8", errors="replace")[:262_144]
            network_log.append({
                "url": response.url,
                "status": response.status,
                "content_type": ct,
                "body_size": len(body),
                "body": body_text,
            })
        except Exception:
            pass

    page.on("response", lambda r: asyncio.create_task(_on_response(r)))

    try:
        try:
            resp = await page.goto(
                url, wait_until=strategy.wait_until, timeout=strategy.timeout_ms,
            )
            cap.nav_completed = True
            cap.http_status = resp.status if resp else None
            if strategy.wait_until == "networkidle":
                _mark("t_networkidle_ms")
        except Exception as exc:
            cap.nav_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            cap.nav_timeout = "TimeoutError" in type(exc).__name__

        # Optional post-load settle — sites often finish hydration after load.
        if strategy.post_sleep_s > 0:
            try:
                await asyncio.sleep(strategy.post_sleep_s)
            except Exception:
                pass

        # --- Always salvage ---
        try:
            cap.final_url = page.url
        except Exception:
            pass
        try:
            html = await page.content()
            full_html = html
            body_bytes = html.encode("utf-8", errors="replace")
            cap.body_bytes = len(body_bytes)
            cap.body_snippet = html[:2048]
        except Exception as exc:
            cap.nav_error = (cap.nav_error or "") + f" | content(): {type(exc).__name__}"

        for sel in _UNIT_SELECTORS:
            try:
                cnt = await page.locator(sel).count()
            except Exception:
                cnt = 0
            if cnt > 0:
                cap.selector_hits[sel] = cnt

    finally:
        cap.elapsed_ms = int((time.monotonic() - t0) * 1000)
        await pool.release(page)

    # Network log summary
    cap.network_log_count = len(network_log)
    ct_breakdown: dict[str, int] = {}
    for entry in network_log:
        ct = entry["content_type"].split(";")[0].strip() or "unknown"
        ct_breakdown[ct] = ct_breakdown.get(ct, 0) + 1
        if any(p.search(entry["url"]) for p in api_patterns):
            cap.api_catalogue_matches += 1
            if any(h in entry["body"] for h in _UNIT_BODY_HINTS):
                cap.api_matches_with_unit_hints += 1
                if len(cap.network_sample) < 5:
                    cap.network_sample.append({
                        "url": entry["url"],
                        "status": entry["status"],
                        "content_type": entry["content_type"],
                        "body_size": entry["body_size"],
                        "body_preview": entry["body"][:400],
                    })
    cap.network_log_content_types = ct_breakdown

    cap.classification = _classify(cap)
    return cap, full_html


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def _slugify(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", s).strip("-") or "x"


# Classification priority — lower is "better". Used to roll up per-property
# results across multiple pages (home + L1 links).
_CLASS_RANK: dict[str, int] = {
    "RECOVERABLE": 0,
    "DOM_NO_UNIT_MARKERS": 1,
    "DOM_EMPTY": 2,
    "TIMEOUT_NO_SALVAGE": 3,
    "HARD_FAIL": 4,
    "HARNESS_ERROR": 5,
    "UNKNOWN": 6,
}


def _rollup(caps: list[ProbeCapture]) -> ProbeCapture | None:
    """Pick the best capture from a group (lowest class rank, ties → highest
    combined selector + api signal, then largest body)."""
    if not caps:
        return None
    return min(
        caps,
        key=lambda c: (
            _CLASS_RANK.get(c.classification, 99),
            -(sum(c.selector_hits.values()) + c.api_matches_with_unit_hints),
            -c.body_bytes,
        ),
    )


def write_outputs(
    out_dir: Path,
    urls: list[tuple[str, str]],
    strategies: list[Strategy],
    cells: dict[tuple[str, str, str], ProbeCapture],
) -> None:
    """Write per-cell JSON, property×strategy rollup matrix, and verdict."""
    out_dir.mkdir(parents=True, exist_ok=True)

    # Per-cell full captures. Key is (pid, page_key, strategy).
    for (pid, page_key, strat), cap in cells.items():
        fname = f"{_slugify(pid)}__{_slugify(page_key)}__{_slugify(strat)}.json"
        (out_dir / fname).write_text(
            json.dumps(asdict(cap), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    strat_slugs = [s.slug for s in strategies]

    # Rollup lookup: (pid, strat) -> best capture across all pages.
    rollups: dict[tuple[str, str], ProbeCapture] = {}
    # All captures grouped per (pid, strat) for the detail section.
    grouped: dict[tuple[str, str], list[ProbeCapture]] = {}
    for (pid, page_key, strat), cap in cells.items():
        grouped.setdefault((pid, strat), []).append(cap)
    for key, caps in grouped.items():
        best = _rollup(caps)
        if best is not None:
            rollups[key] = best

    # summary.md — matrix of rollups, then per-property detail tables.
    lines: list[str] = []
    lines.append("# Fetch Probe Summary\n")
    lines.append("## Rollup (best page per property × strategy)\n")
    lines.append("| property_id | url | " + " | ".join(strat_slugs) + " |")
    lines.append("|---|---|" + "|".join(["---"] * len(strat_slugs)) + "|")
    for pid, url in urls:
        row = [f"| {pid} | {url[:60]} |"]
        for s in strat_slugs:
            c = rollups.get((pid, s))
            if c is None:
                row.append(" – |")
            else:
                signal = sum(c.selector_hits.values()) + c.api_matches_with_unit_hints
                row.append(
                    f" `{c.classification}` via `{c.page_key}` "
                    f"(sel={sum(c.selector_hits.values())},"
                    f"api={c.api_matches_with_unit_hints}/{c.api_catalogue_matches},"
                    f"signal={signal},body={c.body_bytes}B) |"
                )
        lines.append("".join(row))

    lines.append("\n## Per-property detail\n")
    for pid, url in urls:
        lines.append(f"### {pid} — {url}\n")
        lines.append("| page_key | strategy | class | body | sel | api-hints/total | elapsed |")
        lines.append("|---|---|---|---|---|---|---|")
        property_caps = [c for key, c in cells.items() if key[0] == pid]
        property_caps.sort(key=lambda c: (c.page_key != "home", c.page_key, c.strategy))
        for c in property_caps:
            lines.append(
                f"| `{c.page_key}` | `{c.strategy}` | `{c.classification}` | "
                f"{c.body_bytes}B | {sum(c.selector_hits.values())} | "
                f"{c.api_matches_with_unit_hints}/{c.api_catalogue_matches} | "
                f"{c.elapsed_ms}ms |"
            )
        lines.append("")

    (out_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # verdict.json — per-strategy counts after rollup.
    verdict: dict[str, Any] = {
        "total_urls": len(urls),
        "strategies": {},
    }
    for s in strategies:
        counts: dict[str, int] = {}
        recoverable: list[dict[str, str]] = []
        for (pid, _url) in urls:
            c = rollups.get((pid, s.slug))
            if c is None:
                counts["MISSING"] = counts.get("MISSING", 0) + 1
                continue
            counts[c.classification] = counts.get(c.classification, 0) + 1
            if c.classification == "RECOVERABLE":
                recoverable.append({
                    "pid": pid,
                    "winning_url": c.url,
                    "page_key": c.page_key,
                })
        verdict["strategies"][s.slug] = {
            "counts_by_rollup": counts,
            "recoverable": recoverable,
        }
    (out_dir / "verdict.json").write_text(
        json.dumps(verdict, indent=2), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _link_page_key(link_url: str, idx: int) -> str:
    """Stable page-key for a discovered link, e.g. `L1:floorplans`."""
    from urllib.parse import urlparse
    path = urlparse(link_url).path.strip("/").lower() or "root"
    # Collapse long paths for readable summaries.
    slug = _slugify(path)[:40] or "root"
    return f"L1:{idx:02d}:{slug}"


async def _probe_with_fallback(
    pool: BrowserContextPool,
    identities: IdentityPool,
    pid: str,
    url: str,
    strategy: Strategy,
    api_patterns: list[re.Pattern[str]],
    page_key: str,
    parent_url: str | None,
) -> tuple[ProbeCapture, str]:
    """probe_once wrapper that converts hard harness errors into a capture."""
    try:
        return await probe_once(
            pool, identities, pid, url, strategy, api_patterns,
            page_key=page_key, parent_url=parent_url,
        )
    except Exception as exc:
        log.exception("probe failed hard: %s/%s/%s", pid, page_key, strategy.slug)
        cap = ProbeCapture(
            property_id=pid, url=url, strategy=strategy.slug,
            started_at=datetime.now(timezone.utc).isoformat(),
            page_key=page_key, parent_url=parent_url,
            nav_error=f"{type(exc).__name__}: {exc}",
            classification="HARNESS_ERROR",
        )
        return cap, ""


async def run_probe(
    urls: list[tuple[str, str]],
    strategies: list[Strategy],
    out_dir: Path,
    api_patterns: list[re.Pattern[str]],
    concurrency: int,
    follow_links: int,
) -> None:
    pool = BrowserContextPool(max_contexts=concurrency)
    identities = IdentityPool()
    cells: dict[tuple[str, str, str], ProbeCapture] = {}

    try:
        # Strategies are tested serially per URL so the comparison isn't
        # polluted by per-URL proxy / server-load variance between parallel
        # runs. URLs themselves run concurrently up to `concurrency`.
        sem = asyncio.Semaphore(concurrency)

        async def _do_one_url(pid: str, url: str) -> None:
            async with sem:
                for strategy in strategies:
                    log.info("probe: %s [%s] home %s", pid, strategy.slug, url)
                    home_cap, home_html = await _probe_with_fallback(
                        pool, identities, pid, url, strategy, api_patterns,
                        page_key="home", parent_url=None,
                    )
                    cells[(pid, "home", strategy.slug)] = home_cap
                    write_outputs(out_dir, urls, strategies, cells)

                    if follow_links <= 0 or home_cap.body_bytes == 0 or not home_html:
                        continue
                    if home_cap.classification == "RECOVERABLE":
                        # Homepage already extractable; no point spending the
                        # budget on link-follows for this strategy.
                        continue

                    links = extract_unit_link_candidates(
                        home_html, home_cap.final_url or url, follow_links,
                    )
                    log.info("probe: %s [%s] discovered %d candidate link(s)",
                             pid, strategy.slug, len(links))
                    for idx, link_url in enumerate(links, start=1):
                        pk = _link_page_key(link_url, idx)
                        log.info("probe: %s [%s] %s %s",
                                 pid, strategy.slug, pk, link_url)
                        link_cap, _ = await _probe_with_fallback(
                            pool, identities, pid, link_url, strategy,
                            api_patterns, page_key=pk, parent_url=url,
                        )
                        cells[(pid, pk, strategy.slug)] = link_cap
                        write_outputs(out_dir, urls, strategies, cells)

        await asyncio.gather(*(_do_one_url(p, u) for p, u in urls))
    finally:
        await pool.close()

    write_outputs(out_dir, urls, strategies, cells)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--from-run", type=Path, help="Prior run dir (contains properties.json).")
    src.add_argument("--urls-file", type=Path, help="Text file, one URL per line (or `id,url`).")
    src.add_argument("--url", help="Single URL to probe.")
    ap.add_argument("--filter", help="k=v[,k=v...] applied to properties.json rows. Keys: tier, verdict, carry_forward_reason, scrape_outcome, or dotted path.")
    ap.add_argument("--strategies", required=True, help="Comma-separated <wait>:<ms>[+<sleep>s].")
    ap.add_argument("--max-urls", type=int, default=None)
    ap.add_argument("--follow-links", type=int, default=0,
                    help="After probing the homepage, follow up to N same-host links whose path/text scores highest for unit keywords. 0 disables.")
    ap.add_argument("--concurrency", type=int, default=3)
    ap.add_argument("--out-dir", type=Path, default=None)
    ap.add_argument("--api-catalogue", type=Path,
                    default=_MA_POC_ROOT / "config" / "api_catalogue.json")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    if args.from_run:
        urls = load_urls_from_run(args.from_run, args.filter, args.max_urls)
    elif args.urls_file:
        urls = load_urls_from_file(args.urls_file)
        if args.max_urls:
            urls = urls[: args.max_urls]
    else:
        urls = [("single", args.url)]

    if not urls:
        log.error("No URLs after filtering.")
        return 2

    strategies = parse_strategies(args.strategies)
    api_patterns = load_api_patterns(args.api_catalogue)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = args.out_dir or (_MA_POC_ROOT / "data" / "probe_runs" / ts)
    log.info("probing %d url(s) × %d strategy(ies) -> %s",
             len(urls), len(strategies), out_dir)

    asyncio.run(run_probe(
        urls, strategies, out_dir, api_patterns,
        args.concurrency, args.follow_links,
    ))
    log.info("done. outputs in %s", out_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
