# Anti-bot TLS vs IP diagnostic — verdict

verdict: PENDING_LIVE_RUN
generated_at: 2026-05-05T00:00:00Z
sample_size: 0

## Status

This file is a placeholder committed alongside F2's diagnostic script
(`ma_poc/scripts/diagnostics/tls_vs_ip_diagnostic.py`). The actual
verdict requires:

1. `curl_cffi` installed (`pip install curl_cffi`).
2. Network access to the 6 diagnostic URLs documented in the script.
3. **From production-equivalent egress** (Cloud Run, not a laptop) — the
   IP_REPUTATION hypothesis turns on GCP egress IP being on Cloudflare
   deny-lists, so a laptop run will produce a misleading negative.

Run:

```bash
python -m ma_poc.scripts.diagnostics.tls_vs_ip_diagnostic --retries 3
```

The script overwrites this file with the structured `verdict:` line set
to one of `TLS_FINGERPRINT | IP_REPUTATION | MIXED | NOT_REPRODUCIBLE |
INCONCLUSIVE` and a per-URL results table.

## Interpretation key (for the live verdict)

- **TLS_FINGERPRINT** — `curl_cffi --impersonate chrome120` succeeds
  where default `httpx` fails. The DIY stealth tier
  (`curl_cffi` / `patchright`) is the cheap follow-up fix.
- **IP_REPUTATION** — both fail identically. GCP egress is on
  Cloudflare deny lists; vendor proxy/unlocker evaluation is required
  before the rentcafe_direct path can recover the bot-blocked properties
  (the aggregator endpoint runs on the same Cloudflare edge, so it will
  also fail from a deny-listed egress).
- **MIXED** — both fixes needed.
- **NOT_REPRODUCIBLE / INCONCLUSIVE** — rerun with `--retries 3`. If
  still inconclusive, the diagnostic URLs may have been allow-listed
  since 2026-05-04; recapture from a fresh production run.
