# Wave 2 cluster decisions — n_full_zero_w2 (160 props)

| # | Verdict | Fingerprint | Tier (top) | Count | Action |
|---|---|---|---|---:|---|
| 1 | NO_FINGERPRINT_NO_API | — | generic:sgcaptcha_wall | 24 | FOLLOWUP: rendered DOM probe · 24p |
| 2 | HAS_UNIT_MARKERS_AT_2_floorplans | rentcafe,securecafe | TIER_1_API_RENTCAFE_SECURECAFE | 16 | SHIP_INLINE: drill into 2_floorplans · 16p |
| 3 | FLOORPLAN_INDEX_NO_UNITS | cloudflare,entrata,sightmap | TIER_1_API_ENTRATA_EMPTY | 12 | FOLLOWUP: Chrome MCP click-through · 12p |
| 4 | FLOORPLAN_INDEX_NO_UNITS | marketapts | TIER_1_DOM_MARKETAPTS | 8 | FOLLOWUP: Chrome MCP click-through · 8p |
| 5 | FETCH_ERROR | — | generic:no_body_short_circuit | 5 | DEFER (DNS / hard fetch fail) · 5p |
| 6 | FINGERPRINT_g5_NO_UNITS | g5 | TIER_3_DOM | 5 | DEBUG existing g5_NO adapter · 5p |
| 7 | FLOORPLAN_INDEX_NO_UNITS | wordpress | TIER_1_API | 4 | FOLLOWUP: Chrome MCP click-through · 4p |
| 8 | FINGERPRINT_entrata_NO_UNITS | entrata | TIER_1_API_ENTRATA_SHAPE_REJECTED | 4 | DEBUG existing entrata_NO adapter · 4p |
| 9 | HAS_UNIT_MARKERS_AT_3_floor-plans | cloudflare | TIER_1_DOM_REALPAGE_CWS | 4 | SHIP_INLINE: drill into 3_floor-plans · 4p |
| 10 | BLOCKED_HTTP_403 | — | TIER_1_API | 3 | DEFER (fetcher escalation 59b9102) · 3p |
| 11 | WORDPRESS_BACKED | — | TIER_1_API | 3 | FLAG operator-data-gap (unless Elementor body has rent text)  · 3p |
| 12 | HAS_UNIT_MARKERS_AT_2_floorplans | — | ? | 3 | SHIP_INLINE: drill into 2_floorplans · 3p |
| 13 | FINGERPRINT_wix_NO_UNITS | wix | SYNDICATION_ONLY_WIX | 3 | DEBUG existing wix_NO adapter · 3p |
| 14 | HAS_UNIT_MARKERS_AT_2_floorplans | 365res | TIER_3_DOM | 2 | SHIP_INLINE: drill into 2_floorplans · 2p |
| 15 | FLOORPLAN_INDEX_NO_UNITS | g5,rentcafe,securecafe | TIER_3_DOM | 2 | FOLLOWUP: Chrome MCP click-through · 2p |
| 16 | WORDPRESS_BACKED | cloudflare,rentmanager,wordpress | TIER_3_DOM | 2 | FLAG operator-data-gap (unless Elementor body has rent text)  · 2p |
| 17 | FINGERPRINT_rentcafe_NO_UNITS | rentcafe,securecafe | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 2 | DEBUG existing rentcafe_NO adapter · 2p |
| 18 | WORDPRESS_BACKED | wordpress | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 2 | FLAG operator-data-gap (unless Elementor body has rent text)  · 2p |
| 19 | FLOORPLAN_INDEX_NO_UNITS | rentcafe,securecafe | TIER_1_API_RENTCAFE_SECURECAFE | 2 | FOLLOWUP: Chrome MCP click-through · 2p |
| 20 | FLOORPLAN_INDEX_NO_UNITS | squarespace | TIER_1_KNOCK_API | 2 | FOLLOWUP: Chrome MCP click-through · 2p |
| 21 | FLOORPLAN_INDEX_NO_UNITS | — | TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL | 2 | FOLLOWUP: Chrome MCP click-through · 2p |
| 22 | FLOORPLAN_INDEX_NO_UNITS | wix | SYNDICATION_ONLY_WIX | 2 | FOLLOWUP: Chrome MCP click-through · 2p |
| 23 | HAS_UNIT_MARKERS_AT_4_availability | appfolio | TIER_1_API_APPFOLIO | 2 | SHIP_INLINE: drill into 4_availability · 2p |
| 24 | FINGERPRINT_squarespace_NO_UNITS | squarespace | SYNDICATION_ONLY_SQUARESPACE | 2 | DEBUG existing squarespace_NO adapter · 2p |
| 25 | FINGERPRINT_spherexx_NO_UNITS | spherexx | TIER_1_API | 1 | DEBUG existing spherexx_NO adapter · 1p |
| 26 | WORDPRESS_BACKED | cloudflare,wordpress | TIER_3_DOM | 1 | FLAG operator-data-gap (unless Elementor body has rent text)  · 1p |
| 27 | FLOORPLAN_INDEX_NO_UNITS | onesite,wordpress | TIER_1_API | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 28 | FINGERPRINT_onesite_NO_UNITS | g5,onesite | TIER_3_DOM | 1 | DEBUG existing onesite_NO adapter · 1p |
| 29 | JS_REFERENCES_API | — | TIER_1_API | 1 | FOLLOWUP: Chrome MCP network panel · 1p |
| 30 | FLOORPLAN_INDEX_NO_UNITS | rentmanager,rentvision,resman | TIER_3_DOM | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 31 | JS_REFERENCES_API | cloudflare,securecafe | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 1 | FOLLOWUP: Chrome MCP network panel · 1p |
| 32 | WORDPRESS_BACKED | rentcafe,securecafe | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 1 | FLAG operator-data-gap (unless Elementor body has rent text)  · 1p |
| 33 | HAS_UNIT_MARKERS_AT_2_floorplans | cloudflare,rentcafe,securecafe | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 1 | SHIP_INLINE: drill into 2_floorplans · 1p |
| 34 | HAS_UNIT_MARKERS_AT_2_floorplans | rentcafe,securecafe,wordpress | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 1 | SHIP_INLINE: drill into 2_floorplans · 1p |
| 35 | FLOORPLAN_INDEX_NO_UNITS | cloudflare,securecafe,wordpress | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 36 | FLOORPLAN_INDEX_NO_UNITS | g5,securecafe | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 37 | WORDPRESS_BACKED | rentcafe,securecafe,wordpress | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 1 | FLAG operator-data-gap (unless Elementor body has rent text)  · 1p |
| 38 | WORDPRESS_BACKED | rentcafe,wordpress | TIER_1_API_RENTCAFE_SHAPE_REJECTED | 1 | FLAG operator-data-gap (unless Elementor body has rent text)  · 1p |
| 39 | FINGERPRINT_securecafe_NO_UNITS | securecafe | TIER_1_API_RENTCAFE_SECURECAFE | 1 | DEBUG existing securecafe_NO adapter · 1p |
| 40 | FLOORPLAN_INDEX_NO_UNITS | cloudflare,onesite,wordpress | TIER_1_API | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 41 | HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans | securecafe,sightmap | TIER_1_API_RENTCAFE_SECURECAFE | 1 | SHIP_INLINE: drill into 2_floorplans,3_floor-plans · 1p |
| 42 | FLOORPLAN_INDEX_NO_UNITS | g5 | TIER_1_KNOCK_API | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 43 | HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability | cloudflare,rentmanager,resman,sightmap | TIER_1_KNOCK_API | 1 | SHIP_INLINE: drill into 2_floorplans,3_floor-plans,4_availability · 1p |
| 44 | HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans | securecafe | TIER_1_API_RENTCAFE_SECURECAFE | 1 | SHIP_INLINE: drill into 2_floorplans,3_floor-plans · 1p |
| 45 | HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability | wordpress | TIER_1_KNOCK_API | 1 | SHIP_INLINE: drill into 2_floorplans,3_floor-plans,4_availability · 1p |
| 46 | WORDPRESS_BACKED | essex,wordpress | TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL | 1 | FLAG operator-data-gap (unless Elementor body has rent text)  · 1p |
| 47 | HAS_UNIT_MARKERS_AT_2_floorplans | cloudflare,entrata,sightmap | ? | 1 | SHIP_INLINE: drill into 2_floorplans · 1p |
| 48 | HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability | cloudflare | TIER_MERGED_CROSS_PAGE | 1 | SHIP_INLINE: drill into 2_floorplans,3_floor-plans,4_availability · 1p |
| 49 | WORDPRESS_BACKED | appfolio,cloudflare,wordpress | TIER_MERGED_CROSS_PAGE | 1 | FLAG operator-data-gap (unless Elementor body has rent text)  · 1p |
| 50 | FLOORPLAN_INDEX_NO_UNITS | spherexx | TIER_MERGED_CROSS_PAGE | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 51 | ANTIBOT_WALL | cloudflare,datadome,rentmanager,resman,wordpress | TIER_MERGED_CROSS_PAGE | 1 | DEFER (anti-bot wall — needs separate fix) · 1p |
| 52 | FLOORPLAN_INDEX_NO_UNITS | entrata,wordpress | TIER_1_API_ENTRATA_SHAPE_REJECTED | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 53 | FINGERPRINT_onesite_NO_UNITS | onesite | TIER_1_DOM_REALPAGE_CWS | 1 | DEBUG existing onesite_NO adapter · 1p |
| 54 | HAS_UNIT_MARKERS_AT_3_floor-plans | cloudflare,onesite | TIER_1_DOM_REALPAGE_CWS | 1 | SHIP_INLINE: drill into 3_floor-plans · 1p |
| 55 | FLOORPLAN_INDEX_NO_UNITS | cloudflare,entrata,wordpress | TIER_1_API_ENTRATA_SHAPE_REJECTED | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 56 | HAS_UNIT_MARKERS_AT_2_floorplans | wix | SYNDICATION_ONLY_WIX | 1 | SHIP_INLINE: drill into 2_floorplans · 1p |
| 57 | FINGERPRINT_marketapts_NO_UNITS | marketapts | TIER_1_DOM_MARKETAPTS | 1 | DEBUG existing marketapts_NO adapter · 1p |
| 58 | FLOORPLAN_INDEX_NO_UNITS | cloudflare,marketapts | TIER_1_DOM_MARKETAPTS | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 59 | FLOORPLAN_INDEX_NO_UNITS | appfolio,cloudflare,wordpress | TIER_1_API_APPFOLIO | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 60 | FINGERPRINT_entrata_NO_UNITS | entrata,squarespace | SYNDICATION_ONLY_SQUARESPACE | 1 | DEBUG existing entrata_NO adapter · 1p |
| 61 | WORDPRESS_BACKED | cloudflare,onesite,wordpress | TIER_1_API_ONESITE_NO_RESPONSE | 1 | FLAG operator-data-gap (unless Elementor body has rent text)  · 1p |
| 62 | HAS_UNIT_MARKERS_AT_2_floorplans | cloudflare,entrata,wordpress | TIER_1_API_ENTRATA_NO_RESPONSE | 1 | SHIP_INLINE: drill into 2_floorplans · 1p |
| 63 | HAS_UNIT_MARKERS_AT_3_floor-plans | cloudflare,onesite,wordpress | TIER_1_API_ONESITE_NO_RESPONSE | 1 | SHIP_INLINE: drill into 3_floor-plans · 1p |
| 64 | FINGERPRINT_securecafe_NO_UNITS | securecafe,sightmap | TIER_1_API_SIGHTMAP_IFRAME | 1 | DEBUG existing securecafe_NO adapter · 1p |
| 65 | FLOORPLAN_INDEX_NO_UNITS | g5,onesite,wordpress | TIER_1_API_ONESITE_NO_RESPONSE | 1 | FOLLOWUP: Chrome MCP click-through · 1p |
| 66 | HAS_UNIT_MARKERS_AT_2_floorplans | securecafe,sightmap,wordpress | TIER_1_API_SIGHTMAP_IFRAME | 1 | SHIP_INLINE: drill into 2_floorplans · 1p |
| 67 | WORDPRESS_BACKED | onesite,wordpress | TIER_1_API_ONESITE_NO_RESPONSE | 1 | FLAG operator-data-gap (unless Elementor body has rent text)  · 1p |
| 68 | HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans | — | NOT_ENCORESKYLINE_TEMPLATE | 1 | SHIP_INLINE: drill into 2_floorplans,3_floor-plans · 1p |

---

# Per-cluster sample props

## #1 — NO_FINGERPRINT_NO_API · fp=— · 24 props
Tier mix: {'TIER_1_API': 3, 'TIER_3_DOM': 1, 'generic:no_body_short_circuit': 2, 'TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL': 5, '?': 3, 'generic:sgcaptcha_wall': 6, 'TIER_1_API_APPFOLIO': 3, 'NOT_ENCORESKYLINE_TEMPLATE': 1}
  - `97506` [Roebling Lofts I](http://roeblinglofts.com/)
  - `284683` [Sinclair Ridge](https://sinclairridgetn.com/)
  - `283599` [Homer](https://www.66homer.com/)
  - `77725` [Red Oak Ranch](https://www.redoakranch.net/pricing.html)
  - `16072` [Park at Walker's Landing](https://www.ariseequity.com/park-at-walkers-landing#neighborhood)

## #2 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=rentcafe,securecafe · 16 props
Tier mix: {'TIER_3_DOM': 1, 'TIER_1_API_RENTCAFE_SHAPE_REJECTED': 2, 'TIER_1_API_RENTCAFE_SECURECAFE': 7, 'TIER_1_KNOCK_API': 6}
  - `239094` [Zander Place](http://www.zanderplace.com/)
  - `73715` [Sussex West](https://www.sussexwestlife.com/)
  - `64781` [Pleasant Meadows Townhomes](https://www.townhomesatpleasantmeadows.com/)
  - `5985` [Cliffs at Barton Creek](https://www.cliffsatbartoncreek.com/)
  - `8376` [Alvista 23](https://www.alvista23.com)
JS URL hints:
  - `https://api.rentcafe.com/rentcafeapi`

## #3 — FLOORPLAN_INDEX_NO_UNITS · fp=cloudflare,entrata,sightmap · 12 props
Tier mix: {'TIER_1_API': 2, '?': 2, 'TIER_1_API_ENTRATA_EMPTY': 6, 'TIER_1_API_ENTRATA_NO_RESPONSE': 2}
  - `55299` [High Grove](https://www.highgrovegeorgia.com/)
  - `42085` [Revive Apartments](https://www.reviveapartments.com/)
  - `258254` [None](https://www.14fiftyapartments.com/)
  - `44938` [None](https://www.thesunsetridgeapts.com)
  - `65287` [Trio Apartments](https://www.triomke.com/)

## #4 — FLOORPLAN_INDEX_NO_UNITS · fp=marketapts · 8 props
Tier mix: {'TIER_3_DOM': 3, 'TIER_1_DOM_MARKETAPTS': 4, 'TIER_1_API_SIGHTMAP_IFRAME': 1}
  - `14596` [Portola South Mountain](https://www.portolasouthmountain.com/)
  - `14336` [Mountain View Casitas](https://www.mountainviewcasitas.com/)
  - `289291` [Vivo Living Miamisburg](https://www.vivolivingmiamisburg.com/)
  - `69928` [The Azlee](https://www.theazleeapartments.com/)
  - `26194` [Ventana Palms](http://www.ventanapalmsapartments.com/)

## #5 — FETCH_ERROR · fp=— · 5 props
Tier mix: {'TIER_1_API': 1, 'generic:no_body_short_circuit': 4}
  - `1617` [Crossing at Riverlake](https://www.crossingatriverlake.com)
  - `42554` [Vintage Grove Apartments](http://summerwindaptsfl.com/)
  - `36551` [Deer Wood](https://www.deerwood411.com)
  - `20747` [The Citizen on Anza](https://www.liveatcitizen.com/citizenanza/)
  - `232985` [Cityscape Arts](https://cityscapearts.com/)

## #6 — FINGERPRINT_g5_NO_UNITS · fp=g5 · 5 props
Tier mix: {'TIER_3_DOM': 2, 'TIER_1_KNOCK_API': 2, 'TIER_MERGED_CROSS_PAGE': 1}
  - `68956` [266 Lofts](https://www.266lofts.com/)
  - `72374` [Alderwood Park](https://www.alderwoodparkaptliving.com/)
  - `17506` [Ten68 West](https://www.ten68west.com/)
  - `246304` [Vista Creek Apartments](https://www.vistacreekaptliving.com/)
  - `272354` [Alta](https://www.altaaptstarga.com/)
JS URL hints:
  - `https://call-tracking-edge.g5marketingcloud.com/api/v1/phone_numbers`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1o1v8i4hg9-fpi-management-livermore-ca/html_forms/contact_us_short_marketing_center`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1o1v8osjfj-fpi-management-castro-valley-ca/html_forms/contact_us_marketing_center`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1o1v8osjfj-fpi-management-castro-valley-ca/html_forms/contact_us_short_marketing_center`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1o84pyguau-targa-real-estate-services-mf-tacoma-wa/html_forms/contact_us_short_marketing_center`
  - `https://client-leads.g5marketingcloud.com/api/v1/locations/g5-cl-1o8wdjc6x7-pegasus-residential-dallas-ga/html_forms/contact_us_short_marketing_center`

## #7 — FLOORPLAN_INDEX_NO_UNITS · fp=wordpress · 4 props
Tier mix: {'TIER_1_API': 2, 'TIER_3_DOM': 1, 'TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL': 1}
  - `234930` [Southwind Prairie Apartment Homes II](http://southwindprairie.com/)
  - `43488` [Brandywine Manor](http://brandywinemanorapts.com/)
  - `254187` [Crosby Hill](https://www.residecrosbyhill.com/)
  - `266934` [Liberty Grand](https://www.libertygrandapts.com/)
JS URL hints:
  - `https://api.w.org/`

## #8 — FINGERPRINT_entrata_NO_UNITS · fp=entrata · 4 props
Tier mix: {'TIER_1_API_ENTRATA_SHAPE_REJECTED': 3, 'TIER_1_API_ENTRATA_NO_RESPONSE': 1}
  - `268954` [The Villages at Sunnybrook](https://www.thevillagesatsunnybrook.com/?utm_source=GoogleLocalListing&utm_medium=ogranic&utm_campaign=GMB)
  - `7750` [Riverloft](https://www.riverloftapartments.com/)
  - `68318` [Broadway](https://www.livethebroadway.com/)
  - `15014` [Wildwood Park](http://www.rentdittmar.com/apartment-communities/wildwood-park)

## #9 — HAS_UNIT_MARKERS_AT_3_floor-plans · fp=cloudflare · 4 props
Tier mix: {'TIER_1_DOM_REALPAGE_CWS': 4}
  - `60372` [Chelsea](https://www.thechelseaapartments.com)
  - `23145` [Shadow Glen](https://www.liveatshadowglen.com/)
  - `65566` [Bridge Park](http://www.bridgeparkliving.com/)
  - `263789` [Plum Tree](https://www.plumtreeapt.com/)
JS URL hints:
  - `https://ai-chat-frontend.lea.ai/api/embed`
  - `https://cdn-dam.realpage.com/api/v1/dimg/`

## #10 — BLOCKED_HTTP_403 · fp=— · 3 props
Tier mix: {'TIER_1_API': 1, 'generic:no_body_short_circuit': 1, 'TIER_1_API_ENTRATA_SHAPE_REJECTED': 1}
  - `53932` [Misty Hollow](http://www.landmarkrealty.org/misty-hollow/)
  - `16437` [Broadcast Center Apartments](http://www.broadcastcenterapts.com/apartments/floorplan.do?lid=en_US&pid=1917)
  - `18140` [Villas at Park La Brea](http://www.thevillasapts.com/)

## #11 — WORDPRESS_BACKED · fp=— · 3 props
Tier mix: {'TIER_1_API': 2, 'generic:no_body_short_circuit': 1}
  - `74067` [The Huntington](https://sites.google.com/site/huntingtonapartments)
  - `40640` [Niskayuna Gardens](https://www.dawnhomes.com/apartments/ny/niskayuna/niskayuna-gardens/floor-plans)
  - `280316` [Flats](https://fosheeresidential.com/properties/the-flats/)

## #12 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=— · 3 props
Tier mix: {'?': 2, 'NOT_ENCORESKYLINE_TEMPLATE': 1}
  - `63353` [None](http://www.residencesatprairiefire.com/gallery.aspx)
  - `64789` [None](http://www.spectrum270.com/gaithersburg-gaithersburg/spectrum-majestic/)
  - `64923` [Pearl Lantana](http://pearllantana.com/)

## #13 — FINGERPRINT_wix_NO_UNITS · fp=wix · 3 props
Tier mix: {'SYNDICATION_ONLY_WIX': 3}
  - `271721` [The Millennium on Monroe](https://www.millenniumnw.com/)
  - `282696` [Allen Ranch Estates](https://www.ishranchestates.com/allen-ranch)
  - `23494` [Indian Village](https://indianvillageapt.wixsite.com/home/apartments)
JS URL hints:
  - `https://panorama.wixapps.net/api/v1/bulklog`

## #14 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=365res · 2 props
Tier mix: {'TIER_3_DOM': 2}
  - `1777` [Rustic Woods](https://www.rusticwoodsapts.com/)
  - `34909` [Polo Downs](http://www.polodowns.com/)
JS URL hints:
  - `https://api.tiles.mapbox.com/mapbox-gl-js/v0`

## #15 — FLOORPLAN_INDEX_NO_UNITS · fp=g5,rentcafe,securecafe · 2 props
Tier mix: {'TIER_3_DOM': 1, 'TIER_1_API_SIGHTMAP_IFRAME': 1}
  - `273919` [Avon Commons](https://www.morgan-properties.com/apartments/ny/avon/avon-commons/)
  - `282142` [Mews at Annandale](https://www.morgan-properties.com/apartments/nj/annandale/mews-at-annandale/)
JS URL hints:
  - `https://call-tracking-edge.g5marketingcloud.com/api/v1/phone_numbers`

## #16 — WORDPRESS_BACKED · fp=cloudflare,rentmanager,wordpress · 2 props
Tier mix: {'TIER_3_DOM': 1, 'TIER_MERGED_CROSS_PAGE': 1}
  - `49102` [Parkview Apartments](https://pmsi.biz/details/?pid=30)
  - `231117` [Monroe Village Apartments](https://www.dbcliving.com/apartment/monroe-village-apartments/)
JS URL hints:
  - `https://api.w.org/`

## #17 — FINGERPRINT_rentcafe_NO_UNITS · fp=rentcafe,securecafe · 2 props
Tier mix: {'TIER_1_API_RENTCAFE_SHAPE_REJECTED': 1, 'generic:no_body_short_circuit': 1}
  - `67146` [Aria at Millenia](https://ariaatmillenia.com/)
  - `88115` [HighPoint Town Square](https://marquettemanagement.reslisting.com/highpoint-town-square/floorplans.aspx)

## #18 — WORDPRESS_BACKED · fp=wordpress · 2 props
Tier mix: {'TIER_1_API_RENTCAFE_SHAPE_REJECTED': 1, 'TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL': 1}
  - `252019` [Crawford](https://www.eltonparkcorktown.com/buildings/crawford/.)
  - `73649` [Lehigh Flats](https://galmangroup.com/property/lehigh-flats/)
JS URL hints:
  - `https://api.w.org/`
  - `https://eltonparkcorktown.fatwin.com/api/websites/resources/1`

## #19 — FLOORPLAN_INDEX_NO_UNITS · fp=rentcafe,securecafe · 2 props
Tier mix: {'TIER_1_API_RENTCAFE_SECURECAFE': 2}
  - `594` [Harbinwood](https://www.harbinwoodbyelon.com)
  - `147` [West of Eastland](https://www.westofeastlandbyelon.com)

## #20 — FLOORPLAN_INDEX_NO_UNITS · fp=squarespace · 2 props
Tier mix: {'TIER_1_KNOCK_API': 1, 'SYNDICATION_ONLY_SQUARESPACE': 1}
  - `65138` [The Thornton](https://www.thethorntonpdx.com/)
  - `253383` [Pullman Lofts](https://www.pullmansantarosa.com/)
JS URL hints:
  - `https://ai-chat-frontend.lea.ai/api/embed`

## #21 — FLOORPLAN_INDEX_NO_UNITS · fp=— · 2 props
Tier mix: {'TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL': 1, '?': 1}
  - `217960` [Villa Adrian Apartments](https://villaadrian.schattenproperties.com/overview)
  - `77682` [None](https://www.southwoodrealty.com/community/palisades-legacy-oaks-apartments/)

## #22 — FLOORPLAN_INDEX_NO_UNITS · fp=wix · 2 props
Tier mix: {'SYNDICATION_ONLY_WIX': 2}
  - `67150` [Harbor Vista at Crawford Street](https://www.liveharborvista.com/)
  - `217343` [Eagle Harbor II](https://www.liveeagleharbor.com/)
JS URL hints:
  - `https://panorama.wixapps.net/api/v1/bulklog`

## #23 — HAS_UNIT_MARKERS_AT_4_availability · fp=appfolio · 2 props
Tier mix: {'TIER_1_API_APPFOLIO': 2}
  - `47577` [The Heritage by Fairlawn](https://www.theheritagebyfairlawn.com/)
  - `35392` [Wind Chase](https://www.pearlinvestment.com/properties/wind-chase-apartments)

## #24 — FINGERPRINT_squarespace_NO_UNITS · fp=squarespace · 2 props
Tier mix: {'SYNDICATION_ONLY_SQUARESPACE': 2}
  - `298586` [Gateway Lofts Lexington](https://gatewayloftslexington.com/)
  - `59649` [801 Polaris](https://801polaris.com/)

## #33 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=cloudflare,rentcafe,securecafe · 1 props
Tier mix: {'TIER_1_API_RENTCAFE_SHAPE_REJECTED': 1}
  - `42368` [The Broadview Apartments](http://www.broadviewapartments.com/)
JS URL hints:
  - `https://api.rentcafe.com/rentcafeapi`

## #34 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=rentcafe,securecafe,wordpress · 1 props
Tier mix: {'TIER_1_API_RENTCAFE_SHAPE_REJECTED': 1}
  - `61169` [Plaza 25](https://www.plaza25apartments.com/)
JS URL hints:
  - `https://api.rentcafe.com/rentcafeapi`

## #41 — HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans · fp=securecafe,sightmap · 1 props
Tier mix: {'TIER_1_API_RENTCAFE_SECURECAFE': 1}
  - `235292` [One 65 Main](https://www.one65main.com/)

## #43 — HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability · fp=cloudflare,rentmanager,resman,sightmap · 1 props
Tier mix: {'TIER_1_KNOCK_API': 1}
  - `268883` [Sunnyside](https://www.sunnysidefl.com/)

## #44 — HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans · fp=securecafe · 1 props
Tier mix: {'TIER_1_API_RENTCAFE_SECURECAFE': 1}
  - `238181` [Ardence & Bloom](https://www.ardencebloom.com/)

## #45 — HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability · fp=wordpress · 1 props
Tier mix: {'TIER_1_KNOCK_API': 1}
  - `224184` [The Landon of Cromwell](http://www.thelandonofcromwell.com/)
JS URL hints:
  - `https://api.w.org/`

## #47 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=cloudflare,entrata,sightmap · 1 props
Tier mix: {'?': 1}
  - `26852` [None](https://www.emberwood-apts.com)

## #48 — HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans,4_availability · fp=cloudflare · 1 props
Tier mix: {'TIER_MERGED_CROSS_PAGE': 1}
  - `231107` [Crescent Village - Verona](https://www.irvinecompanyapartments.com/communities/crescent-village)

## #54 — HAS_UNIT_MARKERS_AT_3_floor-plans · fp=cloudflare,onesite · 1 props
Tier mix: {'TIER_1_DOM_REALPAGE_CWS': 1}
  - `76982` [The Pointe at Texarkana](http://www.pointetexarkana.com/)

## #56 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=wix · 1 props
Tier mix: {'SYNDICATION_ONLY_WIX': 1}
  - `23963` [Luna Bear Arcos](http://www.arcosphx.com/)
JS URL hints:
  - `https://panorama.wixapps.net/api/v1/bulklog`

## #62 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=cloudflare,entrata,wordpress · 1 props
Tier mix: {'TIER_1_API_ENTRATA_NO_RESPONSE': 1}
  - `78093` [Highland at Spring Hill](http://highlandatspringhill.com/)
JS URL hints:
  - `https://api.w.org/`

## #63 — HAS_UNIT_MARKERS_AT_3_floor-plans · fp=cloudflare,onesite,wordpress · 1 props
Tier mix: {'TIER_1_API_ONESITE_NO_RESPONSE': 1}
  - `12398` [Courtney Park](https://www.courtneyparkapthomes.com/)
JS URL hints:
  - `https://api.w.org/`

## #66 — HAS_UNIT_MARKERS_AT_2_floorplans · fp=securecafe,sightmap,wordpress · 1 props
Tier mix: {'TIER_1_API_SIGHTMAP_IFRAME': 1}
  - `282793` [Evolve at New Hope Farm](https://www.evolvenewhope.com/)
JS URL hints:
  - `https://api.w.org/`

## #68 — HAS_UNIT_MARKERS_AT_2_floorplans,3_floor-plans · fp=— · 1 props
Tier mix: {'NOT_ENCORESKYLINE_TEMPLATE': 1}
  - `12322` [The Parker](http://www.parkerplano.com/)