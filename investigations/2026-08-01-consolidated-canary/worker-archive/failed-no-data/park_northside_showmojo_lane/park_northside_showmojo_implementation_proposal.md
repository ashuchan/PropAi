# Park Northside / ShowMojo narrow production proposal

Status: discovery only; no repository source was edited and no ledger admission was made.

## Observed current path

`parknorthsiderva.com` identifies Park Northside at 1601 Roane St, Richmond VA
23222 and names Dobrin as manager. Dobrin's official `All Properties` page
embeds `showmojo.com/fea92db007/listings/mapsearch`. That current
provider roster contains 13 rows which pass every fail-closed boundary
below. The configured production scrape currently emits zero units, so this is
a navigation/adapter gap, not an authoritative recovery yet.

## Narrow implementation

1. Add a small public-HTML helper such as `_showmojo_public.py`; do not make
   ShowMojo a portfolio-wide generic parser.
2. Trigger it only after an official chain is proven: exact configured property
   identity -> explicit `Managed by` manager link -> same-manager listings page
   -> one published ShowMojo iframe/account.
3. Fetch only that published account's `listings/mapsearch` pages, bounded to
   five pages, ordinary direct GET, no render, proxy, unlocker, CAPTCHA solving,
   fingerprint rotation, or LLM.
4. Require every row to have one 10-hex ShowMojo UID, detail/form UID agreement,
   positive provider rent, explicit provider availability text, exact canonical
   city/state/ZIP, and the canonical property name in the row description.
5. Fail closed on any mixed account/iframe/manager chain. Never fall back to all
   same-manager rows. Deduplicate by UID.
6. Emit the full provider street address as native unit identity and UID as
   `source_ids.showmojo_listing_uid`. Preserve availability text. Leave
   `floor_plan_name` blank because ShowMojo does not publish it; do not infer a
   plan name from square footage.
7. Test three same-account controls: Graystone (wrong brand/ZIP), Lakeview
   (wrong brand/ZIP), and Thomas St (Park Northside template spill, wrong ZIP).
8. Add a configured-route E2E asserting the official hop telemetry and exactly
   13 native/priced/availability-qualified rows with zero portfolio
   contamination.

Suggested tier: `TIER_1_PUBLIC_SHOWMOJO_OFFICIAL_MANAGER_CHAIN`.
