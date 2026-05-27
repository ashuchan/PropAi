# Cluster report — low_strict cohort

Total probed: 40

| Verdict | Fingerprint | # props | Action |
|---|---|---:|---|
| FLOORPLAN_INDEX_NO_UNITS | — | 4 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| HAS_UNIT_MARKERS_AT_2_floorplans | rentcafe,securecafe | 4 | SHIP — drill into the matched path (2_floorplans) |
| HAS_UNIT_MARKERS_AT_4_availability | appfolio | 2 | SHIP — drill into the matched path (4_availability) |
| HAS_UNIT_MARKERS_AT_2_floorplans | securecafe | 2 | SHIP — drill into the matched path (2_floorplans) |
| FINGERPRINT_appfolio_NO_UNITS | appfolio,wordpress | 2 | DEBUG existing appfolio adapter — fingerprint matched but no units |
| FLOORPLAN_INDEX_NO_UNITS | appfolio | 2 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FLOORPLAN_INDEX_NO_UNITS | cloudflare,sightmap | 2 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| HAS_UNIT_MARKERS_AT_2_floorplans | — | 2 | SHIP — drill into the matched path (2_floorplans) |
| FLOORPLAN_INDEX_NO_UNITS | g5,rentmanager,resman | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FLOORPLAN_INDEX_NO_UNITS | rentmanager,rentvision,resman | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability | — | 1 | SHIP — drill into the matched path (2_floorplans,3_floor-plans,4_availability) |
| FLOORPLAN_INDEX_NO_UNITS | rentmanager,resman | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FINGERPRINT_entrata_NO_UNITS | amli,entrata,sightmap | 1 | DEBUG existing entrata adapter — fingerprint matched but no units |
| FLOORPLAN_INDEX_NO_UNITS | securecafe | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FLOORPLAN_INDEX_NO_UNITS | entrata,sightmap | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FLOORPLAN_INDEX_NO_UNITS | cloudflare,entrata,wordpress | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FINGERPRINT_appfolio_NO_UNITS | appfolio,wix | 1 | DEBUG existing appfolio adapter — fingerprint matched but no units |
| FLOORPLAN_INDEX_NO_UNITS | cloudflare,entrata,sightmap | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FLOORPLAN_INDEX_NO_UNITS | entrata | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| FLOORPLAN_INDEX_NO_UNITS | onesite | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |
| HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability | cloudflare,rentmanager,resman | 1 | SHIP — drill into the matched path (2_floorplans,3_floor-plans,4_availability) |
| HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability | cloudflare,entrata,rentmanager,resman | 1 | SHIP — drill into the matched path (2_floorplans,3_floor-plans,4_availability) |
| HAS_UNIT_MARKERS_AT_2_floorplans | spherexx | 1 | SHIP — drill into the matched path (2_floorplans) |
| HAS_UNIT_MARKERS_AT_2_floorplans | entrata | 1 | SHIP — drill into the matched path (2_floorplans) |
| FINGERPRINT_onesite_NO_UNITS | onesite | 1 | DEBUG existing onesite adapter — fingerprint matched but no units |
| FINGERPRINT_g5_NO_UNITS | g5 | 1 | DEBUG existing g5 adapter — fingerprint matched but no units |
| NO_FINGERPRINT_NO_API | — | 1 | PROBE — Chrome MCP rendered DOM; possible client-only React/Vue widget |
| FLOORPLAN_INDEX_NO_UNITS | cloudflare,onesite,wordpress | 1 | PROBE DEEPER — Chrome MCP click-through to find unit pages |

---

# Cluster details

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=— · 4 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_MERGED_CROSS_PAGE: 2
  - TIER_1_API: 1
  - TIER_1_API_REPLI360: 1

**Sample props (up to 5):**
  - `1146` [Signal Pointe Apartment Homes](http://www.signalpointe.com/)
  - `43715` [The Heritage at Boca Raton](https://www.theheritageatbocaraton.com)
  - `39953` [Parker Towers](https://www.parkertowers.com/)
  - `56144` [The 101](http://www.the101kirkland.com/)

**Landing status mix:** {200: 4}

## HAS_UNIT_MARKERS_AT_2_floorplans · fingerprint=rentcafe,securecafe · 4 props

**Action:** SHIP — drill into the matched path (2_floorplans)

**Tier distribution:**
  - TIER_1_API_RENTCAFE_SECURECAFE: 2
  - TIER_1_DOM_RENTCAFE_NESTIN: 2

**Sample props (up to 5):**
  - `231522` [Oakwood Estates](https://www.oakwoodestatessiouxfalls.com/)
  - `238993` [Stony Creek](http://www.liveatstonycreek.com/)
  - `33471` [Mills Crossing](https://www.liveatmillscrossing.com)
  - `245783` [Panorama](https://www.panoramachicagoapts.com/)

**Landing status mix:** {200: 4}
**JS URL hints:**
  - `https://ai-chat-frontend.lea.ai/api/embed`
  - `https://api.rentcafe.com/rentcafeapi`

## HAS_UNIT_MARKERS_AT_4_availability · fingerprint=appfolio · 2 props

**Action:** SHIP — drill into the matched path (4_availability)

**Tier distribution:**
  - TIER_1_DOM_APPFOLIO_VANITY: 2

**Sample props (up to 5):**
  - `19927` [Country](https://www.countryapts.com/)
  - `293188` [Arborview on the River](https://www.arborviewspokane.com/)

**Landing status mix:** {200: 2}

## HAS_UNIT_MARKERS_AT_2_floorplans · fingerprint=securecafe · 2 props

**Action:** SHIP — drill into the matched path (2_floorplans)

**Tier distribution:**
  - TIER_1_API_RENTCAFE_SECURECAFE: 1
  - TIER_1_API_SIGHTMAP_IFRAME: 1

**Sample props (up to 5):**
  - `37476` [Cole](https://www.coleapts.com/cole-apartments-austin-tx/)
  - `272802` [The Residences at Water Square](https://watersquareresidences.com/)

**Landing status mix:** {200: 2}

## FINGERPRINT_appfolio_NO_UNITS · fingerprint=appfolio,wordpress · 2 props

**Action:** DEBUG existing appfolio adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_1_DOM_APPFOLIO_VANITY_PLAN_LEVEL: 2

**Sample props (up to 5):**
  - `284586` [Valencia](https://www.ardentcommunities.com/apartments/dublin/valencia?utm_source=google&utm_medium=organic&utm_campaign=gmb)
  - `252947` [Gage Crossing](https://www.ardentcommunities.com/apartments/dublin/gage-crossing)

**Landing status mix:** {200: 2}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=appfolio · 2 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_API_APPFOLIO: 2

**Sample props (up to 5):**
  - `5488` [Arcadia 4127](https://www.4127arcadia.com/)
  - `20386` [Escondido Apartments](https://www.escondido-apts.com/)

**Landing status mix:** {200: 2}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=cloudflare,sightmap · 2 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_API_EQUITY: 2

**Sample props (up to 5):**
  - `228359` [Milo](https://www.equityapartments.com/denver/congress-park/milo-apartments)
  - `255578` [Circa Fitzsimons](https://www.equityapartments.com/denver/central-park/circa-fitzsimons-apartments)

**Landing status mix:** {200: 2}

## HAS_UNIT_MARKERS_AT_2_floorplans · fingerprint=— · 2 props

**Action:** SHIP — drill into the matched path (2_floorplans)

**Tier distribution:**
  - TIER_1_API_SIGHTMAP_DIRECT: 2

**Sample props (up to 5):**
  - `69307` [Parq on Speer](http://www.parqliving.com/)
  - `242565` [One Six Six](https://onesixsixchicago.com/)

**Landing status mix:** {200: 2}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=g5,rentmanager,resman · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_3_DOM: 1

**Sample props (up to 5):**
  - `74031` [Emerald Pointe](http://www.liveatemerald.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://call-tracking-edge.g5marketingcloud.com/api/v1/phone_numbers`

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=rentmanager,rentvision,resman · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_MERGED_CROSS_PAGE: 1

**Sample props (up to 5):**
  - `36784` [Cypress Grove](https://www.cypressgroveaptliving.com/)

**Landing status mix:** {200: 1}

## HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability · fingerprint=— · 1 props

**Action:** SHIP — drill into the matched path (2_floorplans,3_floor-plans,4_availability)

**Tier distribution:**
  - TIER_3_DOM: 1

**Sample props (up to 5):**
  - `1152` [Sabal Club](https://www.sabalclub.com)

**Landing status mix:** {200: 1}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=rentmanager,resman · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_3_DOM: 1

**Sample props (up to 5):**
  - `25136` [Falls of Maplewood](https://www.fallsofmaplewood.com/)

**Landing status mix:** {200: 1}

## FINGERPRINT_entrata_NO_UNITS · fingerprint=amli,entrata,sightmap · 1 props

**Action:** DEBUG existing entrata adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_MERGED_CROSS_PAGE: 1

**Sample props (up to 5):**
  - `261770` [AMLI Broadway Park](https://www.amli.com/apartments/denver/broadway-park-apartments/amli-broadway-park)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://amli-website.cdn.prismic.io/api/v2/documents/search`

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=securecafe · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_3_DOM: 1

**Sample props (up to 5):**
  - `77168` [Des Arboles](https://www.crosskeysapts.com)

**Landing status mix:** {200: 1}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=entrata,sightmap · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_DOM_ENTRATA_PP_SSR: 1

**Sample props (up to 5):**
  - `6180` [Renew One 59](https://www.renewone59.com/)

**Landing status mix:** {200: 1}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=cloudflare,entrata,wordpress · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_DOM_ENTRATA_PP_SSR: 1

**Sample props (up to 5):**
  - `247218` [Industry Tallahassee](https://industry-tallahassee.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://api.w.org/`

## FINGERPRINT_appfolio_NO_UNITS · fingerprint=appfolio,wix · 1 props

**Action:** DEBUG existing appfolio adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_1_DOM_APPFOLIO_VANITY: 1

**Sample props (up to 5):**
  - `46581` [Villas on rock](https://www.villasonrock.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://panorama.wixapps.net/api/v1/bulklog`

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=cloudflare,entrata,sightmap · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_DOM_ENTRATA_PP_SSR: 1

**Sample props (up to 5):**
  - `35192` [Enclave on Golden Triangle](http://www.enclaveongoldentriangle.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://cottonwoodres-west.azurewebsites.net/api/propertyunits/492775`

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=entrata · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_DOM_GENERIC_PLAN_TEXT: 1

**Sample props (up to 5):**
  - `19939` [Haven at South Mountain](https://www.havenatsouthmountainapts.com/)

**Landing status mix:** {200: 1}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=onesite · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_API_ONESITE_WORKFLOW: 1

**Sample props (up to 5):**
  - `39995` [South Pointe](https://www.southpointehanahan.com/)

**Landing status mix:** {200: 1}

## HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability · fingerprint=cloudflare,rentmanager,resman · 1 props

**Action:** SHIP — drill into the matched path (2_floorplans,3_floor-plans,4_availability)

**Tier distribution:**
  - TIER_1_5_EMBEDDED: 1

**Sample props (up to 5):**
  - `243936` [Centennial Gardens](http://www.centennialgardensapts.com)

**Landing status mix:** {200: 1}

## HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability · fingerprint=cloudflare,entrata,rentmanager,resman · 1 props

**Action:** SHIP — drill into the matched path (2_floorplans,3_floor-plans,4_availability)

**Tier distribution:**
  - TIER_1_5_EMBEDDED: 1

**Sample props (up to 5):**
  - `60305` [Preserve At Willow Springs](https://www.preservethelifestyle.com/)

**Landing status mix:** {200: 1}

## HAS_UNIT_MARKERS_AT_2_floorplans · fingerprint=spherexx · 1 props

**Action:** SHIP — drill into the matched path (2_floorplans)

**Tier distribution:**
  - TIER_1_DOM_GENERIC_PLAN_TEXT: 1

**Sample props (up to 5):**
  - `42981` [Alexander Pointe](https://www.livealexanderpointefl.com/)

**Landing status mix:** {200: 1}

## HAS_UNIT_MARKERS_AT_2_floorplans · fingerprint=entrata · 1 props

**Action:** SHIP — drill into the matched path (2_floorplans)

**Tier distribution:**
  - TIER_1_API_SIGHTMAP_IFRAME: 1

**Sample props (up to 5):**
  - `251384` [Griffin 441](https://griffin441.com/)

**Landing status mix:** {200: 1}

## FINGERPRINT_onesite_NO_UNITS · fingerprint=onesite · 1 props

**Action:** DEBUG existing onesite adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_1_API: 1

**Sample props (up to 5):**
  - `21367` [4001 Midtown](https://www.live4001midtown.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://cdn-dam.realpage.com/api/v1/image/1920x1080/1ff608a20d7d8c379e51a121064cc6d79d743e37`

## FINGERPRINT_g5_NO_UNITS · fingerprint=g5 · 1 props

**Action:** DEBUG existing g5 adapter — fingerprint matched but no units

**Tier distribution:**
  - TIER_1_KNOCK_API: 1

**Sample props (up to 5):**
  - `94997` [Sparq](https://www.sparqsj.com/)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://call-tracking-edge.g5marketingcloud.com/api/v1/phone_numbers`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1j8rdtu3ie-sparq/html_forms/contact_name_phone_email_message`

## NO_FINGERPRINT_NO_API · fingerprint=— · 1 props

**Action:** PROBE — Chrome MCP rendered DOM; possible client-only React/Vue widget

**Tier distribution:**
  - TIER_1_KNOCK_API: 1

**Sample props (up to 5):**
  - `52562` [Juniper Square](https://www.dwellara.com/property/park-place-manor)

**Landing status mix:** {200: 1}

## FLOORPLAN_INDEX_NO_UNITS · fingerprint=cloudflare,onesite,wordpress · 1 props

**Action:** PROBE DEEPER — Chrome MCP click-through to find unit pages

**Tier distribution:**
  - TIER_1_API_ONESITE_WORKFLOW: 1

**Sample props (up to 5):**
  - `14295` [Timber Ridge Apartment Homes](https://www.myboulderapartment.com)

**Landing status mix:** {200: 1}
**JS URL hints:**
  - `https://api.w.org/`