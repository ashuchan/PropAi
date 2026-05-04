# Anti-bot TLS vs IP diagnostic — verdict

verdict: IP_REPUTATION
generated_at: 2026-05-04T23:27:32.113656Z
sample_size: 6

## Per-URL results

| shard | url | httpx | curl_cffi | verdict |
|-------|-----|------:|----------:|---------|
| shard_7 | `http://www.rentcafe.com/onlineleasing/hampshire-village/floorplans.aspx` | 403 | 403 | IP_REPUTATION |
| shard_8 | `http://www.rentcafe.com/onlineleasing/highview-terrace/floorplans.aspx` | 403 | 403 | IP_REPUTATION |
| shard_8 | `https://villageatthegateway.securecafe.com/onlineleasing/village-at-gateways/floorplans.aspx` | 403 | 403 | IP_REPUTATION |
| shard_7 | `https://theapartmentgallery.securecafe.com/onlineleasing/st-clair-terrace0/floorplans.aspx` | 403 | 403 | IP_REPUTATION |
| shard_0 | `https://theapartmentgallery.securecafe.com/onlineleasing/cloister-gardens/floorplans.aspx` | 403 | 403 | IP_REPUTATION |
| shard_3 | `https://livebh.com/residentservices/apartmentsforrent/userlogin.aspx` | 403 | 403 | IP_REPUTATION |

## Interpretation
- TLS_FINGERPRINT — `curl_cffi --impersonate chrome120` succeeds where default `httpx` fails. DIY stealth tier (`curl_cffi`/`patchright`) is the cheap fix.
- IP_REPUTATION — both fail identically. GCP egress on Cloudflare deny lists; vendor evaluation required.
- MIXED — both fixes needed.
- NOT_REPRODUCIBLE / INCONCLUSIVE — rerun with `--retries 3`.
