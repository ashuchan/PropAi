# Annaberg / NestHub narrow production proposal

Status: discovery only. No repository source, strict ledger, or canary state was changed.

## Current gap

The configured URL is native NestHub listing 56 at Annaberg's exact address,
but the provider explicitly marks that apartment unavailable. The configured
pipeline repeats as zero unit rows plus one synthetic plan row whose $500 is
the deposit, not rent. The same official host currently publishes native
listing 602 (unit E7) in its Available Rentals SSR roster and detail page.

## Proposed fail-closed recovery

1. Add a narrow helper such as `_nesthub_public.py`; do not parse arbitrary
   property-manager portfolios.
2. Trigger only on a configured same-host `/_system/listings/{numeric_id}/...`
   NestHub detail with `resources.nesthub.com`, `.nhw-details`, exact canonical
   property identity in the scoped description/address, and an explicit
   unavailable status or otherwise-empty primary extraction.
3. Follow the exact property's same-host community link by name. Require its
   h1/address to match the configured property and its `#nh-props` widget to
   publish `data-ion=listing-widget` plus one non-empty `data-hard-filters`.
4. Follow the same-host, page-published Available Rentals link. Parse only the
   bounded SSR `.nhw-list__item > a[data-id]` roster; reject an oversized or
   non-NestHub response.
5. Select only rows whose normalized base street plus city/state/ZIP exactly
   match the configured property. Never accept all same-manager rows.
6. Fetch each selected same-host detail and require card/path/canonical native
   ID agreement, exact address, property name in the scoped `.description`,
   `For Rent`, positive card/detail-equal rent, explicit availability date,
   bedroom/bath/sqft, and a unique visible unit suffix.
7. Preserve provider floor-plan names only from the scoped sentence shape
   `The {name} is a {n} bedroom...`; for unit 602 this is exactly
   `The Chesapeake`. Do not infer a name from dimensions or URL slug.
8. Emit visible unit suffix `E7` as unit_number, the full provider address as
   `provider_unit_address`, and native ID 602 as a pending provenance key
   (e.g. `nesthub_listing_id`; do not claim cross-run stability yet).
9. Test the exact-property unavailable listing 56, same-ZIP wrong-street
   listing 601, and wrong-property/city/ZIP listing 606 as mandatory controls.
10. Use ordinary direct GET only. No LLM, render, Hyperbrowser, unlocker,
    CAPTCHA solving, FlareSolverr, proxy, or fingerprint rotation is needed.

Suggested tier: `TIER_1_PUBLIC_NESTHUB_SSR_EXACT_PROPERTY`.
