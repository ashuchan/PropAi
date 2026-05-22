#!/usr/bin/env python3
"""ASN / IPv6 / proxy probe for CloudFlare-gated SecureCafe endpoints.

Designed to run from a GCP Cloud Run job (the same egress path as
``jugnu-adhoc-production``) so we can answer the questions:

* **Test A** — does ``*.securecafe.com`` reject all GCP datacenter IPv4
  traffic regardless of TLS fingerprint? (ASN-gate hypothesis)
* **Test A2** — control: do brand-API hosts (Cortland, Irvine) succeed
  from the same egress? Confirms probe_get isn't fundamentally broken.
* **Test C** — same probes forced over IPv6. Many CF tenants haven't
  ported their bot-rules to v6; this is a free escape hatch if it works.
* **Test E** — does ``PROBE_PROXY_URL`` (BrightData residential) flip the
  outcome? Sanity check the proven fix.

Outputs JSONL to stdout (Cloud Logs) and uploads to
``gs://jugnu-canary/diagnostics/asn_ipv6_probe/<RUN_ID>/results.jsonl``.

Designed to be invoked as the container ENTRYPOINT of canary-introspect
with the current production image (has curl_cffi installed).
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

# ──────────────────────────────────────────────────────────────────────
# 20 SecureCafe targets that succeed locally with curl_cffi+chrome120 +
# 5 control targets on brand domains that work from GCP today.
# ──────────────────────────────────────────────────────────────────────

SC_TARGETS = [
    # (pid, marketing-host, securecafe availableunits.aspx url)
    ("238343", "canyonspringswaco.com",
     "https://canyonspringswaco.securecafe.com/onlineleasing/canyon-springs/availableunits.aspx"),
    ("21557", "thecovelamesa.com",
     "https://thecovelamesa.securecafe.com/onlineleasing/the-cove-la-mesa/availableunits.aspx"),
    ("280186", "streamliner87th.com",
     "https://streamliner87th.securecafe.com/onlineleasing/streamliner-on-87th/availableunits.aspx"),
    ("266791", "novaatgreenvalley.com",
     "https://novaatgreenvalley.securecafe.com/onlineleasing/nova-at-green-valley/availableunits.aspx"),
    ("23540", "myparkwestclub.com",
     "https://myparkwestclub.securecafe.com/onlineleasing/park-west/availableunits.aspx"),
    ("8376", "alvista23.com",
     "https://alvista23.securecafe.com/onlineleasing/alvista-23-apartments/availableunits.aspx"),
    ("17114", "arborpointeapthomes.com",
     "https://arborpointeapthomes.securecafe.com/onlineleasing/arbor-pointe-apartment-homes/availableunits.aspx"),
    ("234912", "taylorspond.ticonproperties.com",
     "https://taylorspond-ticonproperties.securecafe.com/onlineleasing/taylor-s-pond/availableunits.aspx"),
    ("219698", "camdenlakeapartments.com",
     "https://camdenlakeapartments.securecafe.com/onlineleasing/camden-lake/availableunits.aspx"),
    ("233889", "lifeatcc.com",
     "https://lifeatcc.securecafe.com/onlineleasing/canton-crossroads/availableunits.aspx"),
    ("265192", "mandolinapartments.com",
     "https://mandolinapartments.securecafe.com/onlineleasing/mandolin-apartments/availableunits.aspx"),
    ("14164", "lindenridgeapartments.com",
     "https://lindenridgeapartments.securecafe.com/onlineleasing/linden-ridge-apartments/availableunits.aspx"),
    ("1084", "theblackhawkapartments.com",
     "https://theblackhawkapartments.securecafe.com/onlineleasing/black-hawk-apartments/availableunits.aspx"),
    ("1154", "deercrestapartments.com",
     "https://deercrestapartments.securecafe.com/onlineleasing/deer-crest/availableunits.aspx"),
    ("10141", "wymberlycrossing.com",
     "https://wymberlycrossing.securecafe.com/onlineleasing/wymberly-crossing/availableunits.aspx"),
    ("10324", "doveparkapts.com",
     "https://doveparkapts.securecafe.com/onlineleasing/dove-park-apartments-0/availableunits.aspx"),
    ("11792", "trellisapts.com",
     "https://trellisapts.securecafe.com/onlineleasing/the-trellis-at-lake-highlands/availableunits.aspx"),
    ("11968", "saddlebrookapts.com",
     "https://saddlebrookapts.securecafe.com/onlineleasing/saddle-brook/availableunits.aspx"),
    ("119798", "829garfield.com",
     "https://829garfield.securecafe.com/onlineleasing/829-garfield/availableunits.aspx"),
    ("219390", "northdakota.weidner.com",
     "https://williston-northdakota-weidner.securecafe.com/onlineleasing/williston/availableunits.aspx"),
]

# Control URLs on brand-API hosts that production succeeds against with
# probe_get from the same GCP egress. If these fail in Test A, probe_get
# itself is broken (not an ASN-gate signal).
CONTROL_TARGETS = [
    ("control-cortland", "cortland.com", "https://www.cortland.com/"),
    ("control-irvine", "irvinecompanyapartments.com", "https://www.irvinecompanyapartments.com/"),
    ("control-avalonbay", "avalonbay.com", "https://www.avalonbay.com/"),
    ("control-apts247", "apts247.com", "https://www.apts247.com/"),
    ("control-google", "google.com", "https://www.google.com/"),
]


def _get_external_ip() -> dict:
    """Best-effort: discover the egress IP this container sees."""
    out = {}
    for url, key in [
        ("https://api.ipify.org?format=json", "ipv4"),
        ("https://api64.ipify.org?format=json", "ipv4_or_v6"),
    ]:
        try:
            with urllib.request.urlopen(url, timeout=8) as r:
                out[key] = json.loads(r.read())
        except Exception as e:
            out[key] = {"error": f"{type(e).__name__}: {e}"}
    # ASN lookup via ip-api (free, no key)
    try:
        if "ip" in out.get("ipv4", {}):
            with urllib.request.urlopen(
                f"http://ip-api.com/json/{out['ipv4']['ip']}", timeout=8
            ) as r:
                out["asn_lookup"] = json.loads(r.read())
    except Exception as e:
        out["asn_lookup"] = {"error": f"{type(e).__name__}: {e}"}
    return out


def _probe(url: str, *, timeout: int = 20, force_v6: bool = False) -> dict:
    """Single curl_cffi probe with chrome120 impersonation. Returns dict."""
    try:
        from curl_cffi import requests as creq
    except ImportError as exc:
        return {"exception": f"curl_cffi_import: {exc}"}

    opts: dict = {"impersonate": "chrome120", "timeout": timeout, "allow_redirects": True}
    if force_v6:
        # Force libcurl to use IPv6 only — curl_cffi exposes this via
        # the same option name as upstream curl: ``CURLOPT_IPRESOLVE = 2``.
        # If the host has no AAAA record, this will fail fast.
        opts["ipresolve"] = "ipv6"

    t0 = time.time()
    try:
        r = creq.get(url, **opts)
    except Exception as exc:
        return {
            "exception": f"{type(exc).__name__}: {str(exc)[:160]}",
            "ms": int((time.time() - t0) * 1000),
        }

    text = r.text or ""
    text_len = len(text)
    cf_markers_seen = sum(
        text.count(m) for m in (
            "challenge-platform", "cf-mitigated", "__cf_chl_",
            "_cf_chl_opt", "/cdn-cgi/challenge-platform",
        )
    )
    avail_rows = text.count("AvailUnitRow")
    return {
        "status": r.status_code,
        "body_len": text_len,
        "avail_rows": avail_rows,
        "cf_markers": cf_markers_seen,
        "ms": int((time.time() - t0) * 1000),
        # Server header is informative (cloudflare / akamai / yardi-direct)
        "server": str(r.headers.get("server", ""))[:80] if hasattr(r, "headers") else "",
    }


def _classify(probe: dict) -> str:
    """Bucket a probe result into one of (ok|cf_shell|blocked|exception|noavail)."""
    if "exception" in probe:
        return "exception"
    status = probe.get("status", 0)
    if status in (403, 429, 503):
        return f"blocked_{status}"
    if status != 200:
        return f"status_{status}"
    if probe.get("avail_rows", 0) > 0:
        return "ok"
    if probe.get("cf_markers", 0) > 0 and probe.get("body_len", 0) < 15000:
        return "cf_shell"
    return "noavail"


def main() -> int:
    run_id = os.environ.get("PROBE_RUN_ID") or datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    has_proxy = bool(os.environ.get("PROBE_PROXY_URL", "").strip())
    has_unlocker = bool(os.environ.get("WEB_UNLOCKER_KEY", "").strip())

    out_lines: list[str] = []

    def emit(rec: dict) -> None:
        rec["ts"] = datetime.now(UTC).isoformat()
        rec["run_id"] = run_id
        rec["has_proxy"] = has_proxy
        rec["has_unlocker"] = has_unlocker
        line = json.dumps(rec, default=str)
        out_lines.append(line)
        print(line, flush=True)

    emit({"test": "meta", "stage": "start", "egress": _get_external_ip()})

    # ── Test A: IPv4, no proxy override (env-var sets proxy when present)
    emit({"test": "A", "stage": "start", "n_targets": len(SC_TARGETS),
          "description": "IPv4 from GCP egress + curl_cffi chrome120"})
    for pid, host, url in SC_TARGETS:
        probe = _probe(url, force_v6=False)
        emit({"test": "A", "pid": pid, "host": host, "url": url,
              "outcome": _classify(probe), **probe})

    # ── Test A2: Control hosts (should all return 200 from GCP)
    emit({"test": "A2", "stage": "start", "n_targets": len(CONTROL_TARGETS),
          "description": "Control: brand-API hosts known to work from GCP"})
    for pid, host, url in CONTROL_TARGETS:
        probe = _probe(url, force_v6=False)
        emit({"test": "A2", "pid": pid, "host": host, "url": url,
              "outcome": _classify(probe), **probe})

    # ── Test C: IPv6 (if AAAA records exist)
    # First check whether the SC targets even have AAAA records.
    emit({"test": "C_pretest", "stage": "start",
          "description": "AAAA record presence on SC hostnames"})
    aaaa_present: list[str] = []
    for _, host, url in SC_TARGETS:
        try:
            sc_host = url.split("/", 3)[2]
            ai = socket.getaddrinfo(sc_host, 443, family=socket.AF_INET6)
            has_v6 = bool(ai)
            emit({"test": "C_pretest", "host": sc_host, "has_aaaa": has_v6,
                  "n_records": len(ai)})
            if has_v6:
                aaaa_present.append(sc_host)
        except socket.gaierror as exc:
            emit({"test": "C_pretest", "host": sc_host, "has_aaaa": False,
                  "gaierror": str(exc)})

    # Only run Test C against hosts with AAAA records; no point if there
    # are none (saves ~5 min of timeouts).
    if aaaa_present:
        emit({"test": "C", "stage": "start", "n_targets": len(aaaa_present),
              "description": "IPv6-only from GCP egress + curl_cffi chrome120"})
        for sc_host in aaaa_present:
            url = f"https://{sc_host}/"
            probe = _probe(url, force_v6=True)
            emit({"test": "C", "host": sc_host, "url": url,
                  "outcome": _classify(probe), **probe})
    else:
        emit({"test": "C", "stage": "skipped",
              "reason": "no AAAA records on any SC host"})

    # ── Summary
    counts: dict[str, int] = {}
    for line in out_lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("test") in ("A", "A2", "C") and "outcome" in rec:
            key = f"{rec['test']}:{rec['outcome']}"
            counts[key] = counts.get(key, 0) + 1
    emit({"test": "summary", "stage": "end", "counts": counts})

    # Upload to GCS for later analysis.
    target_uri = (
        f"gs://jugnu-canary/diagnostics/asn_ipv6_probe/{run_id}/results.jsonl"
    )
    try:
        from google.cloud import storage

        cl = storage.Client()
        bucket = cl.bucket("jugnu-canary")
        blob = bucket.blob(f"diagnostics/asn_ipv6_probe/{run_id}/results.jsonl")
        blob.upload_from_string("\n".join(out_lines) + "\n")
        print(f"# uploaded {len(out_lines)} lines to {target_uri}", flush=True)
    except Exception as exc:
        print(f"# GCS upload failed: {type(exc).__name__}: {exc}", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
