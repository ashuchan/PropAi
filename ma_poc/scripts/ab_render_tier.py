"""A/B harness for the clean 2a residential-render tier.

Measures pass rate of the real-browser render tier across 4 arms on a
walled cohort, so you can pick the browser engine + geo setting with real
numbers instead of theory:

    arm 1  chromium  geo-off      arm 2  chromium  geo-on
    arm 3  firefox   geo-off      arm 4  firefox   geo-on

All arms run HEADFUL (headless=False) — the headful-vs-headless question is
already settled (headful passes CF far better); this A/B compares the two
remaining axes. Each URL is fetched through ``ResidentialRenderProvider``
(vanilla Playwright, residential proxy, wait-not-solve, abort-on-CAPTCHA).

COST: live mode makes real BrightData **residential** requests (and, with
geo-on, state-targeted) — it costs money. It refuses to run live unless
``--live`` is passed AND the BrightData env is present. Default is
``--dry-run`` (a fake provider, no network) which validates the harness end
to end for free.

Live prerequisites:
  * BRIGHTDATA_CUSTOMER_ID / BRIGHTDATA_RESI_ZONE / BRIGHTDATA_RESI_PASSWORD
  * a display for the headful browser on a headless host: run under xvfb,
    e.g.  ``xvfb-run -a python ma_poc/scripts/ab_render_tier.py --live ...``
  * geo-on arms need a BrightData plan that supports state targeting.

Usage:
    # free dry-run — validate the harness
    python -m ma_poc.scripts.ab_render_tier --dry-run --cohort /tmp/localtest/ab_walled_cohort.json
    # real run (spends proxy $$ — needs top-up + your OK)
    xvfb-run -a python ma_poc/scripts/ab_render_tier.py --live \
        --cohort /tmp/localtest/ab_walled_cohort.json --limit 60 --concurrency 4
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

# NB: run as a MODULE from the repo root — ``python -m ma_poc.scripts.ab_render_tier``
# — NOT by path. ``ma_poc/scripts/`` contains an ``email/`` package that, when
# it lands on ``sys.path[0]`` (which running a script by path does), shadows
# the stdlib ``email`` and breaks pydantic. The ``-m`` form keeps sys.path[0]
# as the repo root, avoiding the shadow.

# Arms: engine × match_geo, all headful.
ARMS: list[dict[str, object]] = [
    {"name": "chromium/geo-off", "engine": "chromium", "match_geo": False},
    {"name": "chromium/geo-on", "engine": "chromium", "match_geo": True},
    {"name": "firefox/geo-off", "engine": "firefox", "match_geo": False},
    {"name": "firefox/geo-on", "engine": "firefox", "match_geo": True},
]


@dataclass
class UrlResult:
    pid: str
    url: str
    outcome: str
    status: int | None
    captcha: bool
    block_signature: str | None
    elapsed_ms: int


@dataclass
class ArmResult:
    name: str
    results: list[UrlResult] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        n = len(self.results) or 1
        ok = sum(1 for r in self.results if r.outcome == "OK")
        blocked = sum(1 for r in self.results if r.outcome == "BOT_BLOCKED")
        captcha = sum(1 for r in self.results if r.captcha)
        errored = sum(1 for r in self.results if r.outcome in ("TRANSIENT", "HARD_FAIL"))
        lat = sorted(r.elapsed_ms for r in self.results) or [0]
        return {
            "arm": self.name,
            "n": len(self.results),
            "pass_rate": round(ok / n, 3),
            "ok": ok,
            "blocked": blocked,
            "captcha_abort": captcha,
            "errored": errored,
            "median_ms": lat[len(lat) // 2],
        }


# ── provider factories ───────────────────────────────────────────────────────


def _make_live_provider(arm: dict[str, object]):
    """Configure the render module for this arm and return a fresh provider.

    Sets the module globals (same mechanism the unit tests use) then builds a
    NEW provider so its lazily-created pool picks up this arm's engine/headless.
    """
    from ma_poc.fetch.providers import residential_render as rr

    rr._ENGINE = str(arm["engine"])
    rr._HEADLESS = False  # headful — the whole point
    rr._MATCH_GEO = bool(arm["match_geo"])
    return rr.ResidentialRenderProvider()


def _make_direct_provider(arm: dict[str, object]):
    """Real render tier, but fetch DIRECT (no proxy) — i.e. from THIS machine's
    own IP. On a residential connection that is exactly the 2a scenario, at $0
    and without BrightData. MATCH_GEO is moot (no proxy to target)."""
    from ma_poc.fetch.providers import residential_render as rr
    from ma_poc.fetch.proxy.base import ProxyConfig, ProxyTier

    class _DirectProxy:
        def get_config(self, tier=None, canonical_id=None, state=None):  # noqa: ANN001
            return ProxyConfig(tier=ProxyTier.DIRECT)  # to_playwright() -> None -> direct

    rr._ENGINE = str(arm["engine"])
    rr._HEADLESS = False
    rr._MATCH_GEO = False
    return rr.ResidentialRenderProvider(proxy_provider=_DirectProxy())


def _make_dry_provider(arm: dict[str, object]):
    """A no-network fake with the provider interface — validates the harness.

    Deterministically labels ~55% OK, ~30% blocked, ~15% errored by URL hash
    so the aggregation/report path is exercised without spending anything.
    """
    import hashlib

    from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode

    class _Dry:
        tier_name = "DRY"

        async def fetch(self, task, profile):  # noqa: ANN001
            h = int(hashlib.sha256(f"{arm['name']}|{task.url}".encode()).hexdigest(), 16) % 100
            if h < 55:
                outcome, status, captcha, sig = FetchOutcome.OK, 200, False, None
            elif h < 85:
                outcome, status, captcha, sig = FetchOutcome.BOT_BLOCKED, 403, True, "render_abort:cloudflare"
            else:
                outcome, status, captcha, sig = FetchOutcome.TRANSIENT, None, False, None
            await asyncio.sleep(0)
            return FetchResult(
                url=task.url, outcome=outcome, status=status, body=b"x" if status == 200 else None,
                headers={}, render_mode=RenderMode.RENDER, final_url=task.url, attempts=1,
                elapsed_ms=1000 + (h * 20), captcha_detected=captcha, block_signature=sig,
            )

    return _Dry()


# ── runner ───────────────────────────────────────────────────────────────────


async def _run_arm(arm: dict[str, object], cohort: list[dict], *, mode: str, concurrency: int) -> ArmResult:
    provider = {
        "live": _make_live_provider,
        "direct": _make_direct_provider,
        "dry": _make_dry_provider,
    }[mode](arm)
    sem = asyncio.Semaphore(concurrency)
    out = ArmResult(name=str(arm["name"]))

    async def _one(row: dict) -> None:
        task = SimpleNamespace(property_id=str(row.get("pid", "")), url=row["url"])
        async with sem:
            t0 = time.time()
            try:
                res = await provider.fetch(task, SimpleNamespace())
                out.results.append(UrlResult(
                    pid=task.property_id, url=task.url, outcome=res.outcome.value,
                    status=res.status, captcha=res.captcha_detected,
                    block_signature=res.block_signature, elapsed_ms=res.elapsed_ms,
                ))
            except Exception as exc:  # provider must never raise, but be safe
                out.results.append(UrlResult(
                    pid=task.property_id, url=task.url, outcome="HARD_FAIL", status=None,
                    captcha=False, block_signature=f"harness:{type(exc).__name__}",
                    elapsed_ms=int((time.time() - t0) * 1000),
                ))

    await asyncio.gather(*[_one(r) for r in cohort])
    # Best-effort pool teardown between arms so browsers don't accumulate.
    pool = getattr(provider, "_pool", None)
    for closer in ("close", "shutdown", "aclose"):
        fn = getattr(pool, closer, None)
        if fn:
            try:
                await fn()
            except Exception:
                pass
            break
    return out


def _print_report(arms: list[ArmResult]) -> None:
    rows = [a.summary() for a in arms]
    print("\n=== 2a render-tier A/B ===")
    hdr = f"{'arm':18} {'n':>3} {'pass':>6} {'ok':>4} {'blocked':>8} {'captcha':>8} {'err':>4} {'med_ms':>7}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['arm']:18} {r['n']:>3} {r['pass_rate']:>6.1%} {r['ok']:>4} "
              f"{r['blocked']:>8} {r['captcha_abort']:>8} {r['errored']:>4} {r['median_ms']:>7}")
    best = max(rows, key=lambda r: float(r["pass_rate"]))  # type: ignore[arg-type]
    print(f"\nbest pass rate: {best['arm']} ({best['pass_rate']:.1%})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cohort", required=True, help="JSON list of {pid,url}")
    ap.add_argument("--limit", type=int, default=0, help="cap cohort size (0=all)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--live", action="store_true", help="real BrightData residential run (spends $$)")
    ap.add_argument("--direct", action="store_true",
                    help="real render tier, NO proxy — fetch from THIS machine's IP ($0). "
                         "On a residential connection this IS the 2a scenario; MATCH_GEO is moot.")
    ap.add_argument("--dry-run", action="store_true", help="no network (default)")
    ap.add_argument("--out", default="/tmp/localtest/ab_render_results.json")
    args = ap.parse_args()

    mode = "live" if (args.live and not args.dry_run) else "direct" if args.direct else "dry"
    cohort = json.loads(Path(args.cohort).read_text())
    if args.limit:
        cohort = cohort[: args.limit]

    # In direct mode MATCH_GEO does nothing (no proxy to target), so run only
    # the two engine arms — that's the meaningful chromium-vs-firefox compare.
    arms_to_run = [a for a in ARMS if not a["match_geo"]] if mode == "direct" else ARMS

    if mode == "live":
        import os

        missing = [k for k in ("BRIGHTDATA_CUSTOMER_ID", "BRIGHTDATA_RESI_ZONE", "BRIGHTDATA_RESI_PASSWORD")
                   if not os.environ.get(k)]
        if missing:
            print(f"LIVE refused — missing env: {missing}", file=sys.stderr)
            return 2
        print(f"LIVE run: {len(cohort)} urls × {len(arms_to_run)} arms through BrightData residential. "
              f"This spends proxy $$.", file=sys.stderr)
    elif mode == "direct":
        print(f"DIRECT run ($0, THIS machine's IP): {len(cohort)} urls × {len(arms_to_run)} engine arms "
              f"(chromium vs firefox, headful).", file=sys.stderr)
    else:
        print(f"DRY-RUN (no network): {len(cohort)} urls × {len(arms_to_run)} arms.", file=sys.stderr)

    arms: list[ArmResult] = []
    for arm in arms_to_run:
        arms.append(asyncio.run(_run_arm(arm, cohort, mode=mode, concurrency=args.concurrency)))

    _print_report(arms)
    Path(args.out).write_text(json.dumps(
        {"mode": mode, "arms": [a.summary() for a in arms],
         "detail": {a.name: [vars(r) for r in a.results] for a in arms}}, indent=1))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
