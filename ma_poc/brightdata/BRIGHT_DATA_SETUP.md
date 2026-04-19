# Bright Data Setup — human steps

**Audience:** You (the operator), not Claude Code.
**Purpose:** The one-time setup in Bright Data's dashboard that Claude Code cannot do. Every step below is a click in a browser, a credit-card entry, or a credential copy-paste.
**Time:** ~45 minutes start to finish, plus up to 48 hours of Bright Data compliance review on first signup.

**Pair with:** `CLAUDE_BRIGHTDATA.md` — the code-side integration. Do this document first; Claude Code's handoff depends on the credentials it produces.

---

## 1. What you're creating

Two proxy zones in Bright Data, each with its own credentials, feeding into GCP Secret Manager:

```
Bright Data account
  ├─ Zone: jugnu_dc_{env}          (Datacenter network type)
  │    ├─ Username: brd-customer-{YOUR_ID}-zone-jugnu_dc_{env}
  │    └─ Password: <generated>
  └─ Zone: jugnu_resi_{env}        (Residential network type)
       ├─ Username: brd-customer-{YOUR_ID}-zone-jugnu_resi_{env}
       └─ Password: <generated>
```

Do this once for `staging`, then repeat for `prod`. Keeping separate zones per env gives you independent usage reporting, separate spending limits, and clean separation when debugging.

---

## 2. Account creation

1. Go to **https://brightdata.com** and click **Start free trial**
2. Create an account using your work email (avoid personal addresses — makes it harder to attribute spend later)
3. Add a credit card. You will NOT be charged during trial, but the card is required to access residential proxies at all. The free trial gives you ~$5 of credit — enough to validate the integration but not enough for a real scrape run
4. Complete the KYC (Know Your Customer) flow. This takes between 5 minutes and 48 hours depending on how Bright Data's compliance team triages your application:
   - Expect questions about what you're scraping (answer: "publicly available multifamily property listings for market intelligence")
   - Expect questions about whether you'll respect robots.txt, rate limits, and ToS (answer: yes)
   - Have a business website URL ready — personal accounts get delayed reviews
5. **Alternative to KYC: install Bright Data's SSL cert.** If KYC is blocking and you want to start development, Bright Data lets you skip compliance review by installing their root cert on your development machine. This is fine for dev, but production still requires KYC. Cert install instructions are at https://docs.brightdata.com/general/account/ssl-certificate
6. Once approved, log into the dashboard at **https://brightdata.com/cp**

---

## 3. Find your customer ID

This is the one global identifier for your account, used in every proxy username.

1. In the dashboard, click your **profile icon** (top right) → **Account settings**
2. The customer ID is shown in the format `hl_xxxxxxxx` — note it down as `BRIGHTDATA_CUSTOMER_ID`

Do not confuse customer ID with API token. The customer ID is for proxy URLs; the API token is for Bright Data's management API (which you won't use in v1).

---

## 4. Create the datacenter zone

Datacenter proxies are cheap, fast, and cover the "most of our target sites" case.

1. Left sidebar → **Proxies & Scraping Infra** → **Add proxy zone**
2. Proxy network type: **Datacenter**
3. Name the zone: `jugnu_dc_staging` (for staging) — this name becomes part of the proxy username
4. IP type: **Shared** (exclusive/dedicated is ~10× the price; skip for POC)
5. Country mix: leave **default** (any country). The code will add country targeting via username parameters; restricting here would double-restrict
6. Save & Activate

After save, Bright Data drops you on the zone's **Overview** tab. Four things matter:

| Field in dashboard | What to record |
|---|---|
| Zone name (top of page) | `BRIGHTDATA_DC_ZONE` value (e.g., `jugnu_dc_staging`) |
| "Zone password" field | `BRIGHTDATA_DC_PASSWORD` value — click the copy icon |
| Host | `brd.superproxy.io` (global; same for all zones) |
| Port | `33335` for HTTP/HTTPS |

Do not commit these values to git. Paste them in a scratch file locally; they'll move to Secret Manager in step 8.

---

## 5. Create the residential zone

Residential proxies are expensive but route through real consumer devices. Used as escalation tier when datacenter is blocked.

1. **Add proxy zone** again
2. Proxy network type: **Residential**
3. Name: `jugnu_resi_staging`
4. IP type: **Shared, Rotating** (unless you've already decided on dedicated per the BRD)
5. Country targeting: set to **United States** as default — your Phase 1 target list is US-only
6. Advanced settings — skip everything else; the defaults are fine for v1

**Before saving, a warning will appear about KYC.** If you haven't completed compliance review, Bright Data will either:
- Lock the zone as "pending verification" — you can save it but traffic won't flow
- Offer the "install SSL certificate" workaround for dev access

Either path is fine for development. Production must be KYC-verified.

7. Save & Activate
8. Same overview tab pattern as datacenter — record:
   - `BRIGHTDATA_RESI_ZONE` (zone name, e.g., `jugnu_resi_staging`)
   - `BRIGHTDATA_RESI_PASSWORD`

---

## 6. Test the credentials manually

Before you hand them to Claude Code, verify they work. Open a terminal:

```bash
# Datacenter test
curl --proxy brd.superproxy.io:33335 \
     --proxy-user 'brd-customer-YOUR_CUSTOMER_ID-zone-jugnu_dc_staging:YOUR_DC_PASSWORD' \
     "https://geo.brdtest.com/welcome.txt?product=dc"
# Should return a plain-text blob saying you're connected, with an IP

# Residential test
curl --proxy brd.superproxy.io:33335 \
     --proxy-user 'brd-customer-YOUR_CUSTOMER_ID-zone-jugnu_resi_staging-country-us:YOUR_RESI_PASSWORD' \
     "https://geo.brdtest.com/welcome.txt?product=resi"
# Should return a different IP, this time in a US consumer ISP range
```

If you get `407 Proxy Authentication Required`: the credentials are wrong. Re-copy them from the dashboard — the most common mistake is grabbing the username from an old zone.

If you get a socket-level connection error: your machine can't reach `brd.superproxy.io:33335`. Some corporate networks block outbound on non-standard ports. Try from a mobile hotspot to isolate.

If residential fails but datacenter works: KYC isn't complete. Wait for compliance review, or install the SSL cert.

---

## 7. Set a spending limit

Critical for a POC — a misconfigured loop can burn through the residential zone faster than any alert catches.

1. Dashboard → **Billing** → **Usage limits**
2. Set a **monthly spend cap** per zone:
   - Datacenter: $20/month (headroom above the arch doc estimate)
   - Residential: $30/month (headroom above $5-15 estimate after optimization)
3. Set an **alert threshold** at 75% of cap — Bright Data will email you when you hit it
4. Also configure **daily caps** if the UI offers them. A daily cap is cheaper insurance than catching a runaway run at end of month

Test that alerts work: set the threshold to $0.10 temporarily, run a real scrape, verify the email lands. Then raise the threshold. Untested alerts are worse than no alerts — you'll trust them at the worst moment.

---

## 8. Store credentials in GCP Secret Manager

The Terraform setup (`CLAUDE_TERRAFORM.md` §4.5) creates two secret slots per environment:

- `openrouter-api-key-{env}` (already in the handoff)
- `proxy-credentials-{env}` (already in the handoff)

Bright Data introduces three new slots. Add them now:

```bash
# Assumes you're authenticated: gcloud auth login && gcloud config set project <project>

# Staging
echo -n "hl_xxxxxxxx" | gcloud secrets create brightdata-customer-id-staging --data-file=- --project=<staging-project>
echo -n "jugnu_dc_staging" | gcloud secrets create brightdata-dc-zone-staging --data-file=- --project=<staging-project>
echo -n "<dc password>" | gcloud secrets create brightdata-dc-password-staging --data-file=- --project=<staging-project>
echo -n "jugnu_resi_staging" | gcloud secrets create brightdata-resi-zone-staging --data-file=- --project=<staging-project>
echo -n "<resi password>" | gcloud secrets create brightdata-resi-password-staging --data-file=- --project=<staging-project>

# Grant worker SA access to each secret
for secret in brightdata-customer-id brightdata-dc-zone brightdata-dc-password brightdata-resi-zone brightdata-resi-password; do
  gcloud secrets add-iam-policy-binding ${secret}-staging \
    --member="serviceAccount:jugnu-worker-staging@<staging-project>.iam.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" \
    --project=<staging-project>
done
```

**Zone names aren't secret, but the convention is to treat them as such.** Rotating a zone name means updating Terraform + secret; keeping it in one place (Secret Manager) makes the rotation a single-location change.

Repeat the entire block for prod with a separate Bright Data account (strongly recommended) or separate zones in the same account.

---

## 9. Tell Terraform to wire the new secrets into Cloud Run

`CLAUDE_TERRAFORM.md` §4.5 will need to grow five new secret slots and five corresponding `env` blocks with `value_source.secret_key_ref` in the Cloud Run job definition. This is a small Terraform edit, not a new handoff — add it as a PR when you apply these secrets.

The Cloud Run container ends up with these env vars at runtime, populated by Secret Manager:

- `BRIGHTDATA_CUSTOMER_ID`
- `BRIGHTDATA_DC_ZONE`
- `BRIGHTDATA_DC_PASSWORD`
- `BRIGHTDATA_RESI_ZONE`
- `BRIGHTDATA_RESI_PASSWORD`

These are exactly the names `ma_poc/fetch/proxy/brightdata.py` reads — don't rename one without the other.

---

## 10. Local dev setup for yourself

For running `jugnu_runner.py` on your laptop against the same staging zones:

1. Create `.env` at repo root (verify it's in `.gitignore`)
2. Paste the five credential values from your scratch file
3. Load them via `python-dotenv` — the existing runner already does this (`load_dotenv()` is in `jugnu_runner.py`)

Do not share `.env` files. If a teammate needs to run locally, they go through their own Bright Data setup or get a separate user account on yours (Bright Data supports team seats).

---

## 11. A few things that will trip you up

**The "Zone password" is not the "API token" is not the "Customer ID" is not the "Account password".** Four different strings, all in the dashboard, all named confusingly. Zone password is the only one the proxy code needs.

**Residential proxy TLS certs look wrong.** When you first point a browser at a residential proxy, it will complain about the cert. This is expected — Bright Data terminates TLS at the proxy. The integration handles this by setting `ignore_https_errors=True` on Playwright contexts that route through residential. Don't let the cert warning lead you down a rabbit hole.

**Usage billing is hourly, not real-time.** If you burn through $20 in a 10-minute runaway, the dashboard may not reflect it for up to an hour. Set daily caps in addition to monthly caps.

**Session IDs don't pin IPs forever.** Bright Data documents: datacenter IPs stay for 1 minute of idle time, residential for 5 minutes, and ISP for 7 minutes. Use keep-alive requests to extend sessions, or use the min_ttl parameter (1-60 minutes) for guaranteed residential session durations. If you see the same property bouncing between IPs within a single scrape, that's why — your fetches took longer than the idle window.

**Bright Data blocks direct Google/Bing requests through their proxy.** You can't use `curl --proxy ... https://google.com` as a smoke test; it will 403. Use `https://geo.brdtest.com/welcome.txt` instead — that's their supported test endpoint.

---

## 12. Checklist before handing off to Claude Code

Don't start Claude Code on `CLAUDE_BRIGHTDATA.md` until all of the below are true:

- [ ] Account created, KYC completed or SSL cert installed
- [ ] Customer ID noted
- [ ] Datacenter zone `jugnu_dc_staging` exists, password noted
- [ ] Residential zone `jugnu_resi_staging` exists, password noted
- [ ] Manual `curl` test through both zones succeeds from your laptop
- [ ] Manual `curl` test through both zones succeeds from a Cloud Run task (simplest way: `gcloud run jobs execute` a throwaway job running the curl command — confirms the GCP egress actually reaches Bright Data)
- [ ] Spending caps set on both zones
- [ ] Alert threshold tested and confirmed delivering to your inbox
- [ ] Five Secret Manager entries created in the staging project
- [ ] Worker SA has `secretAccessor` role on all five
- [ ] Local `.env` set up for dev work

Once the checklist is complete, Claude Code can execute `CLAUDE_BRIGHTDATA.md` confidently — every env var it looks for will be populated.

---

## 13. Repeat for prod, differently

For production, do **not** reuse the staging zones. Instead:

1. Create a second Bright Data account under your company billing, or add separate zones (`jugnu_dc_prod`, `jugnu_resi_prod`) to the same account
2. Separate prod zones get separate spending caps — typically 5-10× staging
3. Separate prod zones get separate secret entries in the prod project's Secret Manager
4. Consider Bright Data's **Enterprise tier** at prod volume — it unlocks volume discounts, better support, and dedicated account management. Worth a call to their sales team before you're paying list price at production scale

Staging credentials should never work against prod infrastructure and vice versa. Zone naming conventions make this hard to violate accidentally.
