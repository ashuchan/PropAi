# Cluster report — n_full_zero cohort

Total probed: 40

| Verdict | Fingerprint | # props | Action |
|---|---|---:|---|
| FINGERPRINT_g5_NO_UNITS | g5 | 4 | DEBUG existing g5 adapter — fingerprint matched but no units |
| WORDPRESS_BACKED | wordpress | 4 | PROBE — check wp-json/wp/v2 for custom unit endpoints |
| BLOCKED_HTTP_403 | — | 3 | DEFER — Fetcher escalation already shipped (commit 59b9102) |
| FLOORPLAN_INDEX_NO_UNITS | wordpress | 2 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FETCH_ERROR | — | 2 | TRIAGE |
| NO_FINGERPRINT_NO_API | — | 2 | PROBE — Chrome MCP rendered DOM; possible client-only React/Vue widget |
| HAS_UNIT_MARKERS_AT_2_floorplans | rentcafe,securecafe | 2 | SHIP — drill into the matched path (2_floorplans) |
| FLOORPLAN_INDEX_NO_UNITS | marketapts | 2 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FINGERPRINT_onesite_NO_UNITS | g5,onesite | 1 | DEBUG existing onesite adapter — fingerprint matched but no units |
| HAS_UNIT_MARKERS_AT_2_floorplans | 365res | 1 | SHIP — drill into the matched path (2_floorplans) |
| FINGERPRINT_entrata_NO_UNITS | cloudflare,entrata,sightmap | 1 | DEBUG existing entrata adapter — fingerprint matched but no units |
| FINGERPRINT_rentcafe_NO_UNITS | rentcafe,securecafe | 1 | DEBUG existing rentcafe adapter — fingerprint matched but no units |
| FLOORPLAN_INDEX_NO_UNITS | rentcafe,securecafe | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| WORDPRESS_BACKED | rentcafe,wordpress | 1 | PROBE — check wp-json/wp/v2 for custom unit endpoints |
| FLOORPLAN_INDEX_NO_UNITS | — | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans | rentcafe,securecafe | 1 | SHIP — drill into the matched path (2_floorplans,3_floor-plans) |
| HAS_UNIT_MARKERS_AT_2_floorplans | cloudflare,entrata,sightmap | 1 | SHIP — drill into the matched path (2_floorplans) |
| FINGERPRINT_rentcafe_NO_UNITS | rentcafe,securecafe,sightmap | 1 | DEBUG existing rentcafe adapter — fingerprint matched but no units |
| FLOORPLAN_INDEX_NO_UNITS | cloudflare,onesite | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| HAS_UNIT_MARKERS_AT_3_floor-plans | cloudflare,wordpress | 1 | SHIP — drill into the matched path (3_floor-plans) |
| FINGERPRINT_wix_NO_UNITS | wix | 1 | DEBUG existing wix adapter — fingerprint matched but no units |
| FLOORPLAN_INDEX_NO_UNITS | wix | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FINGERPRINT_onesite_NO_UNITS | onesite | 1 | DEBUG existing onesite adapter — fingerprint matched but no units |
| FINGERPRINT_appfolio_NO_UNITS | appfolio,squarespace | 1 | DEBUG existing appfolio adapter — fingerprint matched but no units |
| HAS_UNIT_MARKERS_AT_4_availability | appfolio | 1 | SHIP — drill into the matched path (4_availability) |
| HAS_UNIT_MARKERS_AT_3_floor-plans | cloudflare | 1 | SHIP — drill into the matched path (3_floor-plans) |
| WORDPRESS_BACKED | cloudflare,wordpress | 1 | PROBE — check wp-json/wp/v2 for custom unit endpoints |

---

# Cluster details

## FINGERPRINT_g5_NO_UNITS · fingerprint=g5 · 4 props

**Action:** DEBUG existing g5 adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_3_DOM: 2
  - TIER_1_KNOCK_API: 2

**Sample props (up to 5):**
  - `255637` [Tuscany at Gabriella Pointe](https://www.rentanapt.com/apartments/az/gilbert/e-warner-rd/)
  - `72121` [Mango Tree](https://www.mangotreeapt.com/)
  - `225389` [Evergreen at River Oaks I](https://www.evergreenatriveroaks.com/)
  - `39789` [Nash Springs](https://www.nashspringsapts.com/?utm_source=obl&utm_medium=organic)

**Landing status mix:** {200: 4}
**JS URL hints:**
  - `https://call-tracking-edge.g5marketingcloud.com/api/v1/phone_numbers`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1mydyxiomg-mariman-company-santa-ana-ca/html_forms/contact_us_short_marketing_center`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1o2aplnsww-pegasus-residential-lake-charles-la/html_forms/contact_us_short_marketing_center`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1ol97v80uy-management-support-gilbert-az/html_forms/contact_us_short_knock`

## WORDPRESS_BACKED · fingerprint=wordpress · 4 props

**Action:** PROBE — check wp-json/wp/v2 for custom unit endpoints

**Tier distribution:**
  - TIER_1_API: 3
  - TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL: 1

**Sample props (up to 5):**
  - `255903` [Elements](https://www.elements-apartments.com/)
  - `232583` [Custer Crossing Apartments](https://princetonmanagement.com/communities/custer-crossing-apartments/)
  - `21976` [Century Square Townhomes](https://princetonmanagement.com/communities/century-square-townhomes/)
  - `63577` [1045 on the Park](http://www.1045onthepark.com/)

**Landing status mix:** {200: 4}
**JS URL hints:**
  - `https://api.w.org/`

## BLOCKED_HTTP_403 · fingerprint=— · 3 props

**Action:** DEFER — Fetcher escalation already shipped (commit 59b9102)

**Tier distribution:**
  - generic:sgcaptcha_wall: 2
  - TIER_1_API_RENTCAFE_SHAPE_REJECTED: 1

**Sample props (up to 5):**
  - `268888` [Artthaus Jack London](https://arthaus.mov/building-community.php?slug=arthaus-jack-london)
  - `224563` [Highlands at Huckleberry II](https://www.highlandsapartmentsva.com/)
  - `67154` [Southern Pine Apartments](https://www.southernpineapts.com)

**Landing status mix:** {403: 3}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=wordpress · 2 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_API: 1
  - NOT_ENCORESKYLINE_TEMPLATE: 1

**Sample props (up to 5):**
  - `24360` [Wildwood Manor Apartments](https://www.wildwoodmanorapts.com/)
  - `2948` [Crystal Woods of Alexandria](https://www.crystalwoodsapts.com)

**Landing status mix:** {200: 2}
**JS URL hints:**
  - `https://api.w.org/`

## FETCH_ERROR · fingerprint=— · 2 props

**Action:** TRIAGE

**Tier distribution:**
  - generic:no_body_short_circuit: 2

**Sample props (up to 5):**
  - `275900` [Aria](https://www.liveatthearia.com/)
  - `263498` [The Julington](https://risejulington.com/)

**Landing status mix:** {0: 2}

## NO_FINGERPRINT_NO_API · fingerprint=— · 2 props

**Action:** PROBE — Chrome MCP rendered DOM; possible client-only React/Vue widget

**Tier distribution:**
  - generic:no_body_short_circuit: 1
  - ?: 1

**Sample props (up to 5):**
  - `12880` [Rockridge Park](https://www.villaserenacommunities.com/rockridge-park/)
  - `226992` [](https://www.forestridgebloomington.com/forest-ridge-bloomington-in/)

**Landing status mix:** {404: 2}

## HAS_UNIT_MARKERS_AT_2_floorplans · fingerprint=rentcafe,securecafe · 2 props

**Action:** SHIP — drill into the matched path (2_floorplans)

**Tier distribution:**
  - TIER_1_API_RENTCAFE_SHAPE_REJECTED: 1
  - TIER_1_KNOCK_API: 1

**Sample props (up to 5):**
  - `239536` [Cheateau Vincennes](http://www.chateauvincennesapts.com/)
  - `294552` [Red Hawk Laurel Grey](https://www.redhawklaurelgrey.com/)

**Landing status mix:** {200: 2}
**JS URL hints:**
  - `https://ai-chat-frontend.lea.ai/api/embed`

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=marketapts · 2 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_DOM_MARKETAPTS: 2

**Sample props (up to 5):**
  - `18752` [Brookstone Apartments](https://www.liveatbrookstoneapartments.com/?utm_source=GMB&utm_medium=organic)
  - `28391` [Hill Country Villas](http://www.hillcountryvillasapartments.com/)

**Landing status mix:** {200: 2}

## FINGERPRINT_onesite_NO_UNITS · fingerprint=g5,onesite · 1 props

**Action:** DEBUG existing onesite adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_3_DOM: 1

**Sample props (up to 5):**
  - `12368` [Ballantyne](https://www.rentanapt.com/ballantyne)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://call-tracking-edge.g5marketingcloud.com/api/v1/phone_numbers`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1hxzladnoc-ballantyne-apartments/html_forms/contact-name-phone-email-only`

## HAS_UNIT_MARKERS_AT_2_floorplans · fingerprint=365res · 1 props

**Action:** SHIP — drill into the matched path (2_floorplans)

**Tier distribution:**
  - TIER_3_DOM: 1

**Sample props (up to 5):**
  - `217605` [Bennett Pointe](https://www.livebennettpointe.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://api.tiles.mapbox.com/mapbox-gl-js/v0`

## FINGERPRINT_entrata_NO_UNITS · fingerprint=cloudflare,entrata,sightmap · 1 props

**Action:** DEBUG existing entrata adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_1_API: 1

**Sample props (up to 5):**
  - `20551` [Rise at the Preserve](https://www.riseatthepreserve.com/?utm_source=google_business&utm_medium=profile&utm_campaign=google)

**Landing status mix:** {200: 1}

## FINGERPRINT_rentcafe_NO_UNITS · fingerprint=rentcafe,securecafe · 1 props

**Action:** DEBUG existing rentcafe adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_1_API_RENTCAFE_SHAPE_REJECTED: 1

**Sample props (up to 5):**
  - `58341` [Heritage Park](http://heritageparkolympia.com/)

**Landing status mix:** {200: 1}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=rentcafe,securecafe · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_3_DOM: 1

**Sample props (up to 5):**
  - `42593` [Harbor Tower Apartments](http://www.harbortowerapartments.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://www.bing.com/api/maps/mapcontrol`

## WORDPRESS_BACKED · fingerprint=rentcafe,wordpress · 1 props

**Action:** PROBE — check wp-json/wp/v2 for custom unit endpoints

**Tier distribution:**
  - TIER_1_API_RENTCAFE_SHAPE_REJECTED: 1

**Sample props (up to 5):**
  - `58546` [Lake Park Estates](https://wright-weber.com/property/LakePark)

**Landing status mix:** {200: 1}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=— · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - ?: 1

**Sample props (up to 5):**
  - `218893` [](https://www.voltaatvoyager.com/volta-at-voyager-colorado-springs-colorado/)

**Landing status mix:** {404: 1}

## HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans · fingerprint=rentcafe,securecafe · 1 props

**Action:** SHIP — drill into the matched path (2_floorplans,3_floor-plans)

**Tier distribution:**
  - TIER_1_KNOCK_API: 1

**Sample props (up to 5):**
  - `42345` [SouthRidge](https://www.southridgekc.com/)

**Landing status mix:** {200: 1}

## HAS_UNIT_MARKERS_AT_2_floorplans · fingerprint=cloudflare,entrata,sightmap · 1 props

**Action:** SHIP — drill into the matched path (2_floorplans)

**Tier distribution:**
  - ?: 1

**Sample props (up to 5):**
  - `12550` [](https://www.liveatcuestas.com/)

**Landing status mix:** {200: 1}

## FINGERPRINT_rentcafe_NO_UNITS · fingerprint=rentcafe,securecafe,sightmap · 1 props

**Action:** DEBUG existing rentcafe adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_MERGED_CROSS_PAGE: 1

**Sample props (up to 5):**
  - `13977` [Mizner Park Apartments](https://www.gables.com/communities/florida/boca-raton/mizner-park-apartments/)

**Landing status mix:** {200: 1}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=cloudflare,onesite · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_MERGED_CROSS_PAGE: 1

**Sample props (up to 5):**
  - `284987` [Orchard Ridge](https://www.liveatorchardridge.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://api.thinkresite.dev/neighborhoods/65e79477b6020bf81112b6c9`
  - `https://forms.thinkresite.dev/api/submit/danger`

## HAS_UNIT_MARKERS_AT_3_floor-plans · fingerprint=cloudflare,wordpress · 1 props

**Action:** SHIP — drill into the matched path (3_floor-plans)

**Tier distribution:**
  - TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL: 1

**Sample props (up to 5):**
  - `42180` [The Courtyard at Jefferson](http://www.courtyardatjefferson.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://api.w.org/`

## FINGERPRINT_wix_NO_UNITS · fingerprint=wix · 1 props

**Action:** DEBUG existing wix adapter — fingerprint matched but no units

**Tier distribution:**
  - SYNDICATION_ONLY_WIX: 1

**Sample props (up to 5):**
  - `263732` [Hoyt Tower](https://www.hoyttowernewark.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://panorama.wixapps.net/api/v1/bulklog`

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=wix · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - SYNDICATION_ONLY_WIX: 1

**Sample props (up to 5):**
  - `34523` [Constellation Ranch](https://www.constellationranchtx.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://panorama.wixapps.net/api/v1/bulklog`

## FINGERPRINT_onesite_NO_UNITS · fingerprint=onesite · 1 props

**Action:** DEBUG existing onesite adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_1_DOM_REALPAGE_CWS: 1

**Sample props (up to 5):**
  - `36777` [Huntington Woods](https://www.HuntingtonWoodsapts.com)

**Landing status mix:** {200: 1}

## FINGERPRINT_appfolio_NO_UNITS · fingerprint=appfolio,squarespace · 1 props

**Action:** DEBUG existing appfolio adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_1_API_APPFOLIO: 1

**Sample props (up to 5):**
  - `19955` [Brookside](https://www.brooksidejohnsoncreek.com/)

**Landing status mix:** {200: 1}

## HAS_UNIT_MARKERS_AT_4_availability · fingerprint=appfolio · 1 props

**Action:** SHIP — drill into the matched path (4_availability)

**Tier distribution:**
  - TIER_1_API_APPFOLIO: 1

**Sample props (up to 5):**
  - `284175` [SCS Athens](https://www.livescs.com/property/scs-athens)

**Landing status mix:** {200: 1}

## HAS_UNIT_MARKERS_AT_3_floor-plans · fingerprint=cloudflare · 1 props

**Action:** SHIP — drill into the matched path (3_floor-plans)

**Tier distribution:**
  - TIER_1_DOM_REALPAGE_CWS: 1

**Sample props (up to 5):**
  - `232495` [Inverness Apartments](https://www.invernessapthome.com)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://cdn-dam.realpage.com/api/v1/dimg-crop/1200x0/0b2c536b7d7e78adc17587a132f9725a`
  - `https://cdn-dam.realpage.com/api/v1/dimg-crop/1200x0/0b57d87fa8fbbda00200776defc44f60`
  - `https://cdn-dam.realpage.com/api/v1/image/`

## WORDPRESS_BACKED · fingerprint=cloudflare,wordpress · 1 props

**Action:** PROBE — check wp-json/wp/v2 for custom unit endpoints

**Tier distribution:**
  - TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL: 1

**Sample props (up to 5):**
  - `30237` [Forest View](https://venterraliving.com/apartments/forest-view/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://api.w.org/`