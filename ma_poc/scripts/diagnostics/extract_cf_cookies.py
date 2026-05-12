"""
Extract cf_clearance cookies from GCS cloud-run artifacts and write them
to the local PropertyCookieJar so the local canary can reuse them.

CF clearance cookies are typically valid for 30 minutes to 2 hours
(varies by site's CF security level). Reusing them allows local Playwright
to bypass Tier-1 JS challenges without solving them again.

NOTE: CF clearance cookies are IP-session-tied at the TLS layer for strict
WAF configs, but for JS-challenge (Tier-1) sites the cookie check is often
only UA+cookie without hard IP binding. Worth trying for Tier-1 sites.

Usage:
    python scripts/diagnostics/extract_cf_cookies.py \
        --run-date 2026-05-11 \
        --pids 25964 271788 260174
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

_MA_POC = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_MA_POC.parent))


def extract_from_gcs(run_date: str, pids: list[str]) -> dict[str, dict]:
    """Download events.jsonl for each shard and find cf_clearance cookies."""
    from google.cloud import storage

    client = storage.Client()
    results: dict[str, dict] = {}

    # Find which shards contain our PIDs
    for shard_idx in range(50):
        shard = f"shard_{shard_idx}"
        blob_name = f"runs/{run_date}/{shard}/events.jsonl"
        try:
            blob = client.bucket("jugnu-raw-production").blob(blob_name)
            data = blob.download_as_bytes()
        except Exception:
            continue

        for line in data.split(b"\n"):
            if not line.strip():
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            pid = ev.get("property_id", "")
            if pid not in pids:
                continue
            # Look for cookie set events
            kind = ev.get("kind", "")
            if kind == "fetch.cookies_set":
                cookies = ev.get("cookies", {})
                cf = {k: v for k, v in cookies.items()
                      if "cf_clearance" in k or "cf_bm" in k}
                if cf:
                    existing = results.setdefault(pid, {})
                    existing.update(cf)
                    log.info("pid=%s found CF cookies: %s", pid, list(cf.keys()))

    return results


def extract_from_local_events(run_date: str, pids: list[str], local_mirror: Path) -> dict[str, dict]:
    """Extract CF cookies from a locally-mirrored run."""
    results: dict[str, dict] = {}

    for shard_dir in local_mirror.iterdir():
        if not shard_dir.is_dir() or shard_dir.name.endswith("_raw"):
            continue
        ep = shard_dir / "events.jsonl"
        if not ep.exists():
            continue
        with open(ep, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                pid = ev.get("property_id", "")
                if pid not in pids:
                    continue
                # Response headers can carry Set-Cookie
                if ev.get("kind") == "fetch.completed":
                    headers = ev.get("headers") or ev.get("response_headers") or {}
                    if isinstance(headers, dict):
                        sc = headers.get("set-cookie") or headers.get("Set-Cookie") or ""
                        if "cf_clearance" in sc:
                            # Parse: cf_clearance=xxxx; path=/; ...
                            import re
                            m = re.search(r"cf_clearance=([^;]+)", sc)
                            if m:
                                results.setdefault(pid, {})["cf_clearance"] = m.group(1)
                                log.info("pid=%s extracted cf_clearance from response headers", pid)

    return results


def write_to_cookie_jar(pid: str, cookies: dict, data_dir: Path, identity_slot: int = 0) -> None:
    """Write extracted CF cookies to PropertyCookieJar format."""
    from ma_poc.fetch.cookie_jar import PropertyCookieJar

    jar = PropertyCookieJar(pid, identity_slot, base_dir=data_dir / "cookies")
    # Load existing cookies and merge
    existing = jar.load()
    for host_key in list(existing.keys()) + ["cloudflare.com"]:
        # We don't know the exact host; write to a "default" host key
        pass

    # Direct write to the jar file
    jar_path = data_dir / "cookies" / f"{pid}_{identity_slot}.json"
    jar_path.parent.mkdir(parents=True, exist_ok=True)

    existing_data: dict = {}
    if jar_path.exists():
        try:
            existing_data = json.loads(jar_path.read_text(encoding="utf-8"))
        except Exception:
            existing_data = {}

    # CF cookies are typically domain-scoped; write them for the pid's domain
    # The caller must know the domain; we use a wildcard key
    existing_data["__cf_injected__"] = cookies
    jar_path.write_text(json.dumps(existing_data, indent=2), encoding="utf-8")
    log.info("Wrote CF cookies for pid=%s to %s", pid, jar_path)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run-date", required=True, help="Run date YYYY-MM-DD")
    p.add_argument("--pids", nargs="+", required=True, help="Property IDs to extract cookies for")
    p.add_argument("--local-mirror", type=Path, default=None,
                   help="Local mirror of the run (default: try GCS)")
    p.add_argument("--data-dir", type=Path,
                   default=Path("data/canary"),
                   help="Output data dir for cookie jars")
    args = p.parse_args()

    if args.local_mirror and args.local_mirror.exists():
        log.info("Extracting from local mirror: %s", args.local_mirror)
        cookies = extract_from_local_events(args.run_date, args.pids, args.local_mirror)
    else:
        log.info("Extracting from GCS run/%s", args.run_date)
        cookies = extract_from_gcs(args.run_date, args.pids)

    if not cookies:
        log.warning("No CF cookies found for PIDs %s in run %s", args.pids, args.run_date)
        log.info("NOTE: cf_clearance is set on the browser context, not via response headers.")
        log.info("To capture it, run a cloud scrape and extract from PropertyCookieJar in GCS.")
        return

    for pid, c in cookies.items():
        write_to_cookie_jar(pid, c, args.data_dir)
        log.info("PID %s: injected %d CF cookie(s): %s", pid, len(c), list(c.keys()))

    print(f"\nExtracted CF cookies for {len(cookies)} properties.")
    print("Run local canary — cookies will be loaded by PropertyCookieJar automatically.")


if __name__ == "__main__":
    main()
