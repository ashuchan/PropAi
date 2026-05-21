# T4_edge_templates — 14-HAR bucket summary

Date: 2026-05-21
Input: 14 HARs from properties where a known PMS template fired but
edge cases caused extraction failure.

## Headline

**This bucket is mostly thin / wrong-URL captures.** 13 of 14 properties
carry Entrata markers (92.9% — by far the most concentrated bucket on
one PMS), and 5 of 14 carry Wix markers. But only 2 of 14 have any
real extractable unit data in the HAR:

| Property | Verdict | Top URL | Why it failed in prod |
|---|---|---|---|
| `gscapts.com` | jsonld_only | homepage with 3 JSON-LD `Apartment` nodes | JSON-LD passes 6.4 type set; likely a confidence-gate issue |
| `www.encantadatwinpeaks.com` | tier1_api_exists | `doorway-api.knockrentals.com/v1/property/community/{id}` | Knock adapter should catch — investigate routing |

The other 12 are weak_signal or no_unit_signal:

| Subset | n | Cause |
|---|---:|---|
| weak_signal from Elise-AI chat bundle | 3 | False positive — the JS bundle contains "bed"/"apartment" tokens in chat training prompts |
| no_unit_signal | 9 | Manual HAR captured a page that genuinely has no unit data — usually a "Contact us for pricing" or sales-CTA page |

## The Elise-AI chat false positive

`commonsatcowanboulevard.com`, `crystalwoodsapts.com`, `www.burnsmgmt.com`
all score 2 not because the page has unit data, but because the embedded
chat widget JS bundle (`cdn.eliseai.com/@meetelise/chat` or
`cdn.skypack.dev/-/@meetelise/chat`) contains tokens like "1 bed" /
"bedroom" in its prompt templates. My probe's regex doesn't distinguish
training-prompt strings from real unit data.

**Fix for future probes:** skip JS bundles ≥500 KB by MIME-type — they
are never the unit-data response.

## The 9 no_unit_signal properties

`www.16bennett.com`, `www.broadcastcenterapts.com`, `www.districtsevensprings.com`,
`www.hayloftapartmenthomes.com`, `www.hoyttowernewark.com`,
`www.millenniumnw.com`, `www.oakhillapts.com`, `www.theheritagebyfairlawn.com`,
`www.westgate-village-townhouses.com`

All carry Entrata markers but the HAR contains no unit-shape response.
Reading the markers:
  - 5 of 9 carry **Wix** markers (Wix + Entrata combo) — this is the
    "Wix marketing site that links out to an Entrata portal" cluster.
    The Entrata portal call wasn't captured because the operator never
    clicked through.
  - 3 of 9 carry **WordPress + Entrata** — same shape, just WP instead
    of Wix.
  - 1 (`www.oakhillapts.com`) has no detected PMS markers at all.

These need a **re-capture from the operator** that walks
landing → /floorplans → into the Entrata portal page where availability
loads. The current captures stop at the marketing wrapper.

## Why this bucket name (`edge_templates`) is suspect

If 13/14 are Entrata-marked, the production label "edge_templates" suggests
production's Entrata template fired and produced edge-case output. But the
HARs don't show what the production scrape saw — only what the operator's
browser saw. The operator's browser also missed the unit data in 12/14
cases. So the "edge case" probably isn't a template logic edge case — it's
a **routing failure that affects both production AND the manual capture**:
the page links to Entrata but the link-hop isn't being followed.

## Recommendations

1. **Verify Knock routing for `www.encantadatwinpeaks.com`** — Knock API
   call is in the HAR, the existing Knock adapter should catch it.
2. **Verify JSON-LD confidence gate for `gscapts.com`** — 3 Apartment nodes
   on the homepage, should be picked up by 6.4-updated `extract_jsonld_from_html`.
3. **Add a link-hop signal for Wix/WordPress → Entrata cluster** — when a
   marketing page carries Entrata markers but doesn't expose unit data
   itself, surface the Entrata portal URL as a sub-page hint so link-hop
   captures it.
4. **Re-capture the 9 no_unit_signal HARs** — operator needs to actually
   navigate to floorplans, not just land on the property page.
5. **Probe-script improvement:** exclude JS bundles ≥500 KB from scoring
   (the Elise AI false positive).

## Per-property table

| Property | Verdict | PMS markers (top) |
|---|---|---|
| gscapts.com | jsonld_only | engrain, entrata, knock, realpage_oll |
| www.encantadatwinpeaks.com | tier1_api_exists | entrata, knock, razz, realpage_oll, rentcafe |
| commonsatcowanboulevard.com | weak (Elise FP) | entrata, realpage_oll, wordpress, yardi |
| crystalwoodsapts.com | weak (Elise FP) | entrata, realpage_oll, wordpress, yardi |
| www.burnsmgmt.com | weak (Elise FP) | entrata, knock, realpage_oll, wordpress |
| www.16bennett.com | no_unit_signal | entrata, wix |
| www.broadcastcenterapts.com | no_unit_signal | entrata, wordpress |
| www.districtsevensprings.com | no_unit_signal | appfolio, entrata, fortresstech, knock |
| www.hayloftapartmenthomes.com | no_unit_signal | entrata, wix, wordpress |
| www.hoyttowernewark.com | no_unit_signal | entrata, wix |
| www.millenniumnw.com | no_unit_signal | entrata, wix |
| www.oakhillapts.com | no_unit_signal | (none) |
| www.theheritagebyfairlawn.com | no_unit_signal | appfolio, engrain, entrata, sightmap |
| www.westgate-village-townhouses.com | no_unit_signal | entrata, wix |
