# T4_code_embedded_jason — 3-HAR bucket summary

Date: 2026-05-21
Input: 3 HARs from properties where production's embedded-JSON extractor
ran but produced empty / wrong output.

## Headline

Sample too small to draw any cohort-level conclusions. Each property is
its own investigation.

| Property | Verdict | Total responses | Blocked | PMS markers |
|---|---|---:|---:|---|
| `twolightkc.com` | weak_signal | 209 | 0 | engrain, entrata, realpage_oll, wordpress, yardi |
| `www.maac.com` | no_unit_signal | 7 | 3 | (none) |
| `www.theresidencescitymodern.com` | no_unit_signal | 80 | 3 | entrata, razz, rentcafe, wordpress, yardi |

## Per-property

### `twolightkc.com`

209 responses, none containing real unit data. The probe scored it 2 from
the Elise-AI chat JS bundle (same false-positive pattern as 3 properties
in T4_edge_templates). The 207 other responses are presumably the page
shell, marketing assets, and analytics calls.

**Likely failure mode:** the operator captured the homepage / marketing
page but never navigated to the actual floor-plan endpoint. The Yardi +
Entrata + RealPage markers suggest this is a multi-PMS marketing landing
with the data behind a portal click-through.

### `www.maac.com`

Only 7 responses, 3 of them blocked. Thin capture — the operator's session
hit a block early and the HAR is essentially useless. Note: MAAC is in
the [600-property grind memory](../../../../../../.claude/projects/-Users-ankur-PropAi-main/memory/project_grind600_findings_2026-05-21.md)
as "100% hit" — production extracts MAAC properties fine. So this HAR is
not representative.

### `www.theresidencescitymodern.com`

80 responses (normal session volume), 3 blocked, 0 unit-signal candidates.
Markers heavy on entrata+rentcafe+yardi+razz — but no actual unit-data
response was captured. The operator session ran but didn't hit the
floor-plans XHR.

## Recommendations

This bucket is too small to inform an adapter change. The signal it gives
is that **the production label "embedded_jason" doesn't match what these
HARs contain** — none of the three has an embedded-JSON unit payload to
critique. The captures are either thin (MAAC), wrong-URL (twolightkc, citymodern),
or both.

Action: re-request 10-20 properties from production that were actually
labeled `T4_code_embedded_jason` with the operator capturing
`/floorplans` directly, then re-analyze.
