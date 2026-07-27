# The 231 no-fingerprint misses — profile-seeding targets (2026-07-26)

From the `2026-07-26-plancohort` canary. Of the **835 properties that did not
reach unit level**, 231 carry **no vendor fingerprint** on their landing page —
no SecureCafe, Entrata, AppFolio, RealPage, Knock, SightMap, Jonah, apts247,
ResMan, Funnel or G5 marker.

They are the residual after the big levers: SecureCafe (368) is a routing fix,
Entrata (51) a plan-index fix. This bucket has no single lever, which is exactly
what makes per-property **profile seeding** the right tool.

## First, a split that matters — 231 is not one cohort

"No fingerprint" is a property of my *landing-page scan*, not of the property.
52 of the 231 are detected as Entrata; their markers live on plan-detail pages,
which the scan never sees.

| group | n | what it needs |
|---|---|---|
| **SEED-TARGET** — no vendor AND no adapter (`generic` / `generic_plan_text` / none) | **135** | per-property profile seeding |
| **ROUTE-TARGET** — an adapter already exists | **96** | routing / existing-adapter work, *not* seeding |

Route-target by adapter:

```
      52  entrata
      12  rentmanager
       9  residentservices365
       6  wix_nopms
       3  rentcafe
       2  encoreskyline_template
       2  rentaladdress
       2  squarespace_nopms
```

**Only the 135 are genuine seeding candidates.** Seeding the 96 would paper over
a routing bug with a per-property URL — the wrong fix, and it would hide the
signal that the adapter is being missed.

## Why seeding, and what to expect

A 27-property live probe of this slice (`wf_0a47dbbe-453`, every ceiling claim
adversarially refuted) found:

- **17 of 27 recoverable (63%)** — the *hardest* slice measured, but far from the
  0% I originally assumed when arguing 90% coverage was unreachable
- **21 of 27 (78%) are served by a real vendor** hidden behind a bespoke CMS
  shell — only 6 were genuinely bespoke. Hub50House, for instance, is a custom
  WordPress theme server-side-rendering Yardi/SecureCafe data with a parallel
  SightMap JSON API; neither vendor is visible from the landing page.

So the economics: writing an adapter for a vendor that serves one or two
properties is not worth it, but the pipeline already consumes a per-property
answer at the highest priority (`pms/scraper.py`):

```python
profile_top.append((wpu, _LLM_HINT_SCORE + 1, "profile:winning_page_url"))
# "Highest possible score so it always lands first."
```

An agent finds the path **once**; every subsequent daily run replays it
deterministically. Detection does not have to be right first time — it only has
to find a working path once.

Real examples already discovered by probing this slice:

```
https://jcmliving.com/wp-json/hw/v1/floorplans/cardinal-hill     (WordPress REST, MRI feed)
https://iconpropmgtbrokerservice.appfolio.com/listings?filters…  (AppFolio tenant query)
https://www.availability.fortresstech.io/unit-availability/01b6… (FortressTech SSR portal)
https://api.ws.realpage.com/v2/property/1736910/units?availabl…  (RealPage API)
```

No vendor adapter would have located any of those.

## Method — the verification gate is not optional

`ma_poc/scripts/seed_profiles_from_probes.py` (dry-run by default):

```bash
python -m ma_poc.scripts.seed_profiles_from_probes --probes probes.json   # verify only
python -m ma_poc.scripts.seed_profiles_from_probes --probes probes.json --commit
```

**Only 53% of agent-reported URLs re-fetch to something carrying a roster** (41
of the first 78). The rest were HTTP 400/401/403 (session- or auth-bound API
calls), 429, or 200-with-no-roster (SPA shells, or the agent overstating what it
reached). `winning_page_url` occupies the *top hop slot*, so a wrong value costs
a wasted fetch on every future run — blind seeding would have poisoned 37
profiles. Every URL is therefore re-fetched and checked before it is written.

The script also refuses to overwrite a `winning_page_url` the pipeline earned
from a real extraction (that outranks a probed one); the probe URL is demoted to
`availability_links` instead.

### Practical notes

- `canonical_id == apartment_id` — the join is trivial.
- Profiles live in `gs://jugnu-canary/profiles/`, **not** the repo. Only ~9 of
  the probed properties have a local profile, so this must run where the
  profiles are.
- Expected yield on 135 at the measured rates: roughly 63% have a reachable
  surface, of which ~53% produce a directly re-fetchable URL — so **order
  45–85 properties**, not 135. Worth doing because it is permanent and costs no
  proxy spend, but it is not the biggest lever on the board.

## SEED-TARGET list — 135 properties

By tier:

```
      45  TIER_1_DOM_GENERIC_PLAN_TEXT
      24  TIER_3_DOM
      16  TIER_3_PLAN_TEXT
      14  TIER_3_DOM_GENERIC
      10  TIER_MERGED_CROSS_PAGE
       7  TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_LEVEL
       6  generic:no_body_short_circuit
       4  TIER_3_PLAN_TEXT_PLAN_LEVEL
```

| apartment_id | property | tier | url |
|---|---|---|---|
| `42981` | Alexander Pointe | `TIER_1_5_EMBEDDED_PLAN_LEVEL` | https://www.livealexanderpointefl.com/ |
| `3148` | Aven | `TIER_1_5_EMBEDDED_PLAN_LEVEL` | https://www.avenliving.com |
| `246962` | Portola Bridge Creek | `TIER_1_API` | http://www.bridgecreekapthomes.com/ |
| `304556` | Prospect Flats | `TIER_1_API_PLAN_LEVEL` | https://www.liveprospectflats.com/ |
| `4709` | 1020 at Winter Springs | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.1020atwinterspringsfl.com/ |
| `23396` | 6151 Winthrop Apartments | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.6151winthrop.com/ |
| `45986` | Aaron Lake Apartments | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.pedcorhomes.com/Apartments/Home/419 |
| `7445` | Advenir at Walden Lake | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.advenirliving.com/waldenlake |
| `26527` | Aquila Park | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://finelivingapts.com/ |
| `37930` | Autumn Oaks | `TIER_1_DOM_GENERIC_PLAN_TEXT` | www.autumnoaksapt.com |
| `299136` | Bearfoot Landing | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.bearfootlandingapts.com/ |
| `22459` | Colonial Court | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.colonialcourtapts.com |
| `98129` | Colonial Crossing Apartments | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://colonialcrossingapts.com/ |
| `253126` | Conklin Townhomes | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://conklintowns.com/ |
| `27349` | Country Village of Sherman | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.countryvillageapthomes.com/ |
| `52934` | Davis Creek | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://daviscreekapartments.com/ |
| `68781` | Fieldcrest Apartments | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.fieldcrestapt.com/ |
| `40515` | Forge Homestead | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.deanzaproperties.com/south-bay-area/forge-h… |
| `60985` | Greenwood Village | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.corsamanagement.com/properties/greenwood-vi… |
| `49769` | Grey Parc Apartments | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.greyparcofrossville.com/ |
| `78561` | Harlow | `TIER_1_DOM_GENERIC_PLAN_TEXT` | www.harlowvegas.com |
| `18187` | KRC Reserve | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.krcreserveapts.com/ |
| `242976` | Kelly Farms | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.kellyfarms.com/?fbclid=IwAR3Ht2UTDJGEhVJqHMl… |
| `266934` | Liberty Grand | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.libertygrandapts.com/ |
| `994` | Magnolia Ridge | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.liveatmagnoliaridgeapartments.com/ |
| `53773` | Mallard Cove | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://mallardcove.org/ |
| `39093` | Mark I Apartments | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://hattiesburgmarkapartments.com/mark1/ |
| `50379` | Millbrook Pointe | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.millbrookpointeapartments.com/ |
| `77245` | Pembrook Place | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.pembrookplaceapartments.com/ |
| `236447` | Prescott at West Hills | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.prescottwesthills.com/ |
| `2382` | Quincy Ridge | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://quincyridgeapts.com/ |
| `27165` | Regatta Apartments Homes | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.regatta-apts.net/ |
| `12618` | Retreat at Peachtree City | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.retreatatptc.com/ |
| `11727` | Rise Bedford Lake | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.risebedfordlake.com/ |
| `49248` | Sandpiper | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.sandpiperapartmentssaltlakecity.com/ |
| `46036` | Serengeti Springs | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.serengetisprings.com |
| `69613` | Spectrum on Spring | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.spectrumonspring.com/ |
| `36268` | Stoney Creek | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.stoneycreekapthomes.com/ |
| `22763` | Summerset at International Crossin | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.jfmanagementgroup.com/summerset/ |
| `19171` | Sunbay | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.sunbayapts-wp.com |
| `232788` | Sussex Manor | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.sussexmanorapts.com/ |
| `40750` | The Abigail | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.theabigailapts.com/ |
| `283566` | The Standard at Franz | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.hpifranz.com/ |
| `252377` | The Timbers | `TIER_1_DOM_GENERIC_PLAN_TEXT` | http://www.timberssapulpa.com/ |
| `280355` | Town Center Apartments | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.mhtowncenter.com/ |
| `58697` | Vargos On The Lake | `TIER_1_DOM_GENERIC_PLAN_TEXT` | www.vargosonthelake.com |
| `46336` | Westbury Square | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.westburyportlandtx.com/ |
| `42571` | Westwood Village | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://westwoodvillageapthomes.com/en/ |
| `43551` | Willow Trail | `TIER_1_DOM_GENERIC_PLAN_TEXT` | https://www.willowtrailjax.com/contact-us/ |
| `292317` | Alders Grove | `TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_` | https://aldersgroveliving.com/ |
| `1259` | Ashton Parc | `TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_` | http://www.ashtonparc.com/ |
| `74528` | Grand Oaks | `TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_` | https://grandoaks-apartments.com/ |
| `16982` | Pleasant View Gardens | `TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_` | http://www.pleasantviewgardensnj.com/ |
| `278316` | The Gateway at Maverick Square | `TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_` | https://www.gatewayatmavericksq.com/ |
| `52761` | The Westmoor | `TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_` | www.thewestmoor.com |
| `72732` | The Willows | `TIER_1_DOM_GENERIC_PLAN_TEXT_PLAN_` | http://www.centralmngt.com/Search?Community=15 |
| `74364` | Wilmington Pointe | `TIER_1_DOM_RENTMANAGER_VANITY` | www.wilmingtonpointe.com |
| `59649` | 801 Polaris | `TIER_2_JSONLD` | https://801polaris.com/ |
| `234915` | Fox Glen | `TIER_2_JSONLD` | www.livefoxglen.com |
| `282648` | Apollo Ridge | `TIER_3_DOM` | https://www.apolloridgeapts.com/ |
| `540` | Bridgepoint I | `TIER_3_DOM` | https://www.hgliving.com/apartments/fl/jacksonville/bri… |
| `244274` | Cornerstone | `TIER_3_DOM` | http://www.cornerstoneaptsharlingen.com/ |
| `12550` | Cuestas | `TIER_3_DOM` | https://www.liveatcuestas.com/ |
| `235871` | Elmtree Park Apartments | `TIER_3_DOM` | https://www.hgliving.com/apartments/in/indianapolis/elm… |
| `239121` | Enclave | `TIER_3_DOM` | http://www.theenclaveaustin.com/ |
| `35971` | Grand Palms Apartments | `TIER_3_DOM` | https://www.grandpalms.com/ |
| `227334` | Grand by Gehry | `TIER_3_DOM` | https://www.relatedrentals.com/apartment-rentals/los-an… |
| `10298` | Jaxon | `TIER_3_DOM` | https://www.jaxonliving.com/ |
| `9133` | Lane at Towne Crossing | `TIER_3_DOM` | https://www.hgliving.com/apartments/tx/mesquite/lane-at… |
| `281911` | Laurel Glen | `TIER_3_DOM` | https://www.liveatlaurelglen.com/ |
| `18684` | Le Mirage | `TIER_3_DOM` | https://lemirageapts.managebuilding.com/Resident/public… |
| `73649` | Lehigh Flats | `TIER_3_DOM` | https://galmangroup.com/property/lehigh-flats/ |
| `10179` | Marion | `TIER_3_DOM` | https://www.livethemarion.com/#hero |
| `65394` | One Hill South | `TIER_3_DOM` | https://www.relatedrentals.com/apartment-rentals/washin… |
| `12285` | Parks at Treepoint | `TIER_3_DOM` | http://www.parksattreepoint.com/ |
| `12370` | Rockbrook Creek I | `TIER_3_DOM` | https://www.hgliving.com/apartments/tx/lewisville/rockb… |
| `48769` | Spring Gate | `TIER_3_DOM` | https://www.hgliving.com/apartments/fl/panama-city/spri… |
| `23788` | The Aspect | `TIER_3_DOM` | https://www.theaspectaustin.com/ |
| `217738` | The District at Cypress Water (Sco | `TIER_3_DOM` | http://www.thedistrictatcypresswaters.com/ |
| `29597` | The Life at Jackson Square | `TIER_3_DOM` | https://www.thelifeatjacksonsquare.com/ |
| `283391` | Valley at Mill Creek | `TIER_3_DOM` | https://www.thevalleyatmillcreek.com/ |
| `3106` | Woodhollow | `TIER_3_DOM` | http://www.woodhollowwaco.com/ |
| `11797` | Wray North Dallas | `TIER_3_DOM` | https://www.wraynorthdallas.com/wray-north-dallas-tx/ |
| `275900` | Aria | `TIER_3_DOM_GENERIC` | https://www.liveatthearia.com/ |
| `13189` | Briar Glen Village | `TIER_3_DOM_GENERIC` | https://www.onmarkliving.com/briar-glen-village/ |
| `243379` | Brookwood | `TIER_3_DOM_GENERIC` | http://bjbtally.com/brookwood-apartments/ |
| `29154` | Cardinal Hill | `TIER_3_DOM_GENERIC` | https://jcmliving.com/apartment-rentals/nj/chatham/card… |
| `21976` | Century Square Townhomes | `TIER_3_DOM_GENERIC` | https://princetonmanagement.com/communities/century-squ… |
| `14795` | Emerson Apartments | `TIER_3_DOM_GENERIC` | https://www.emersonaustin.com/ |
| `58319` | Fountainview Townhomes | `TIER_3_DOM_GENERIC` | https://www.shaoolmgt.com/property/fountainview-townhomes/ |
| `243194` | Fox Hill Apartments | `TIER_3_DOM_GENERIC` | http://www.foxhillapthomes.com/ |
| `68184` | Hub50House | `TIER_3_DOM_GENERIC` | https://www.hub50house.com |
| `222122` | Lincoln Meadows | `TIER_3_DOM_GENERIC` | https://princetonmanagement.com/communities/lincoln-mea… |
| `261781` | One University Drive South | `TIER_3_DOM_GENERIC` | https://oneuds.com/ |
| `35901` | Promenade Oaks | `TIER_3_DOM_GENERIC` | https://promenadeoaks-apartments.com/ |
| `54983` | Village on the Green | `TIER_3_DOM_GENERIC` | https://jcmliving.com/apartment-rentals/nj/tuckerton/vi… |
| `48708` | Windpoint Apartments | `TIER_3_DOM_GENERIC` | https://karademas.org/properties/windpoint |
| `258497` | Casa Blanca I | `TIER_3_DOM_PLAN_LEVEL` | https://www.grandviewmanagementaz.com/casa-blanca-cooli… |
| `4124` | Tramor at Cannon Place | `TIER_3_DOM_PLAN_LEVEL` | https://tramor.com/property/tramor-cannon-place/ |
| `67546` | 223 E Town | `TIER_3_PLAN_TEXT` | https://223etown.com/ |
| `13345` | Bellevue Heights | `TIER_3_PLAN_TEXT` | https://www.bellevue-heights.com/ |
| `43488` | Brandywine Manor | `TIER_3_PLAN_TEXT` | http://brandywinemanorapts.com/ |
| `41777` | Bristle Pointe Apartments | `TIER_3_PLAN_TEXT` | https://www.kromerinvestments.com/apartments/bristle-po… |
| `446` | Cottages @ Sanford | `TIER_3_PLAN_TEXT` | https://www.cottagesatsanford.com/ |
| `38298` | Courtyard | `TIER_3_PLAN_TEXT` | http://courtyardgretna.com/ |
| `38532` | Glenbrook at Rocky Hill | `TIER_3_PLAN_TEXT` | www.glenbrook-apts.com |
| `221097` | Landry Apartment Homes | `TIER_3_PLAN_TEXT` | http://www.liveatlandry.com |
| `37819` | Modena at Mallard | `TIER_3_PLAN_TEXT` | https://www.modenaatmallard.com/ |
| `32155` | Montelago | `TIER_3_PLAN_TEXT` | http://www.montelagoapts.com/ |
| `4868` | Oakstone | `TIER_3_PLAN_TEXT` | https://www.sylispm.com/oakstone-apartment-homes/ |
| `233581` | Riverside North Apartments | `TIER_3_PLAN_TEXT` | http://www.riversidenorth-in.com/ |
| `37805` | The Gentry's Landing | `TIER_3_PLAN_TEXT` | http://www.gentryslanding.com/ |
| `73923` | The Vineyards Apartments | `TIER_3_PLAN_TEXT` | http://www.vineyardsapt.com/ |
| `220345` | Vestawood Apartments | `TIER_3_PLAN_TEXT` | https://www.vestawood.com/ |
| `22155` | Woodway Apartments | `TIER_3_PLAN_TEXT` | https://www.sylispm.com/woodway-apartments/ |
| `233289` | Elevation 1659 | `TIER_3_PLAN_TEXT_PLAN_LEVEL` | https://elevation1659apts.com/ |
| `232870` | Sorrento Apartments | `TIER_3_PLAN_TEXT_PLAN_LEVEL` | www.SorrentoApartmentsfl.com |
| `251190` | The Carlaw | `TIER_3_PLAN_TEXT_PLAN_LEVEL` | https://liveatthecarlaw.com/ |
| `74067` | The Huntington | `TIER_3_PLAN_TEXT_PLAN_LEVEL` | https://sites.google.com/site/huntingtonapartments |
| `234940` | 1010 On the Rhine | `TIER_MERGED_CROSS_PAGE` | http://1010ontherhine.com |
| `56182` | Acorn Acres | `TIER_MERGED_CROSS_PAGE` | https://www.liveatacornacres.com/ |
| `98` | Bent Oak Apartments | `TIER_MERGED_CROSS_PAGE` | https://www.dbcliving.com/apartment/bent-oak-apartments/ |
| `7931` | Lamonte Park | `TIER_MERGED_CROSS_PAGE` | https://www.lamonteparktownhomes.com/ |
| `67598` | Lofts At Little Creek | `TIER_MERGED_CROSS_PAGE` | http://www.loftsatlittlecreek.com/ |
| `231117` | Monroe Village Apartments | `TIER_MERGED_CROSS_PAGE` | https://www.dbcliving.com/apartment/monroe-village-apar… |
| `3902` | Pelham Apartments | `TIER_MERGED_CROSS_PAGE` | https://pelhamgreenville.com/home |
| `21303` | Rosemeade | `TIER_MERGED_CROSS_PAGE` | http://www.rosemeadema.com/ |
| `118780` | The Lofts Hillside at Little Creek | `TIER_MERGED_CROSS_PAGE` | https://www.loftsatlittlecreek.com/ |
| `44163` | Wycliff West | `TIER_MERGED_CROSS_PAGE` | https://www.rentwycliffwest.com/ |
| `244349` | Albany Landing | `generic:no_body_short_circuit` | http://albanylandingapts.com/ |
| `21293` | Beach Villas | `generic:no_body_short_circuit` | www.beachvillasapts.com |
| `47878` | Port Royal At Spring Hill | `generic:no_body_short_circuit` | https://www.portroyaltn.com/ |
| `44157` | Quality Hill | `generic:no_body_short_circuit` | http://www.liveatqualityhill.com/ |
| `254955` | The Club at Enclave | `generic:no_body_short_circuit` | https://www.clubatenclave.com/ |
| `294610` | Virginia Flats | `generic:no_body_short_circuit` | https://krcapartments.com/property-detail/virginia-flats/ |

## ROUTE-TARGET list — 96 properties (adapter exists; do NOT seed)

| apartment_id | property | tier | url |
|---|---|---|---|
| `269721` | Skyview Lofts | `TIER_1_API_APPFOLIO_EMPTY_PLAN_LEV` | https://skyview-lofts.com/ |
| `77558` | Arriv√© Seattle | `TIER_3_DOM_GENERIC` | http://arriveseattle.com/ |
| `267793` | The Carson | `ENCORESKYLINE_NO_PLAN_LINKS_PLAN_L` | https://www.carsonphilly.com/ |
| `303963` | Allure at Edinburgh | `TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEV` | https://www.allurechesapeake.com/ |
| `39378` | Andante | `TIER_1_DOM_ENTRATA_PP_SSR` | www.andanteapts.biz |
| `38836` | Braewood | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.braewoodutah.com/ |
| `12459` | Bridge Lane | `TIER_1_DOM_ENTRATA_PP_SSR` | http://www.bridgelaneapartments.com/Home.aspx |
| `5967` | Bryan Hill | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.bryanhillapartments.com/ |
| `18271` | Bryn Mawr | `TIER_1_API_ENTRATA_NO_RESPONSE_PLA` | http://www.brynmawrapartments.com/ |
| `40398` | Canyon Springs | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.canyonspringsfresno.com/ |
| `26716` | Colony Oaks | `TIER_1_API_ENTRATA_NO_RESPONSE` | http://www.colonyoaksapartments.com/ |
| `48344` | Copper Commons | `TIER_1_DOM_ENTRATA_PP_SSR` | http://www.coppercommons.com/ |
| `218956` | Coronado Villas | `TIER_1_DOM_ENTRATA_PP_SSR` | https://coronadovillas.tokolaproperties.com/ |
| `240132` | Deerfield Place | `TIER_1_DOM_ENTRATA_PP_SSR` | http://www.deerfieldplaceutica.com/ |
| `239312` | Eland Downe Townhouses | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.liveeland.com/ |
| `16060` | Ellyn Crossing | `TIER_1_API_ENTRATA_NO_RESPONSE_PLA` | https://www.ellyncrossing.com/?utm_source=Google%20Loca… |
| `230737` | Emerald Downs | `TIER_1_API_ENTRATA_NO_RESPONSE` | https://www.emerald-downs-apts.com/ |
| `36926` | Fenestra at the Square | `TIER_1_DOM_ENTRATA_PP_SSR` | www.fenestraapts.com |
| `64551` | Festival Park | `TIER_3_DOM_GENERIC` | www.festival-park.com |
| `70238` | Grace Apartments | `TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEV` | http://www.graceapartmentscr.com/ |
| `63498` | Hanover Montrose | `TIER_1_DOM_ENTRATA_PP_SSR` | http://www.hanovermontrose.com/ |
| `77834` | Hanover Northgate | `TIER_3_DOM_GENERIC` | https://www.hanovernorthgate.com/ |
| `63465` | Hanover Southampton | `TIER_1_DOM_ENTRATA_PP_SSR` | http://www.hanoversouthampton.com/Apartments/module/pro… |
| `229671` | Heather Glen | `TIER_1_DOM_ENTRATA_PP_SSR` | www.liveheatherglen.com |
| `75936` | Keystone | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.livekeystone.com/ |
| `40942` | Los Altos | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.rentatlosaltos.com/ |
| `244533` | Lynn Hill | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.marylandmanagement.com/linthicum/lynn-hill-… |
| `234186` | Middle Branch Apartments and Townh | `TIER_1_DOM_ENTRATA_PP_SSR` | http://www.middlebranchmanor.com |
| `77796` | Modera Edgewater | `TIER_1_API_ENTRATA_NO_RESPONSE_PLA` | https://www.moderaedgewatermiami.com/ |
| `64369` | Modera South Lake Union | `TIER_1_API_ENTRATA_NO_RESPONSE` | http://www.moderasouthlakeunion.com/ |
| `291653` | Parkway Apartments | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.parkwayidahofalls.com/ |
| `1632` | Pavilions Apartments | `TIER_3_DOM_GENERIC` | www.pavilionsapartments.com |
| `37065` | Pointe Inverness | `TIER_3_DOM_GENERIC` | https://www.ptinverness.com/ |
| `37847` | Preserve at Research Park | `TIER_1_DOM_ENTRATA_PP_SSR` | www.preserveresearchpark.com |
| `39154` | ReNew on Ridgewood | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.renewonridgewood.com/ |
| `63940` | Riverhouse | `TIER_3_DOM_GENERIC` | http://www.riverhouselittlerock.com/ |
| `263992` | Rivermont Park | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.rivermontparkapts.com/ |
| `19914` | Rustic Oaks Apts. | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.rusticoaksapartments.com/ |
| `77391` | South Beach by Logan | `TIER_1_DOM_ENTRATA_PP_SSR` | www.southbeachbylogan.com |
| `238698` | Stone Creek Apartments | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.stonecreekgf.com/ |
| `48052` | Summerville Station | `TIER_1_API_ENTRATA_NO_RESPONSE` | https://www.summervillestationapts.com/ |
| `280569` | Sweetwater | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.sweetwateraddis.com/ |
| `300768` | Tabor View Lofts | `TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEV` | https://www.livetaborview.com/ |
| `75081` | The Falls at Mesa Point | `TIER_1_API_ENTRATA_NO_RESPONSE` | https://www.live-mesapoint.com/ |
| `240761` | The Pearl | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.ptlamgmt.com/eugene/the-pearl/conventional/ |
| `46267` | The Ridge | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.theridgeokc.com/ |
| `47750` | The Ridge At Chestnut Hill | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.theridgeatchestnuthillapts.com/ |
| `245290` | The Slate at 96 | `TIER_1_DOM_ENTRATA_PP_SSR` | https://www.slate96.com/ |
| `262769` | The Standard at Champions | `TIER_1_API_ENTRATA_NO_RESPONSE` | https://www.hpithestandard.com/ |
| `243358` | The Villas of Castleton | `TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEV` | http://www.castletonvillas.com/ |
| `5497` | Tides on Twain | `TIER_1_API_ENTRATA_NO_RESPONSE` | https://www.tidesontwainapartments.com/ |
| `32371` | Villas de Fontenelle | `TIER_1_DOM_ENTRATA_PP_SSR` | http://www.villasdefontenelle.com/ |
| `38285` | Warwick | `TIER_1_DOM_ENTRATA_PP_SSR` | www.liveatwarwick.com/ |
| `261603` | Wellspring | `TIER_1_DOM_ENTRATA_PP_SSR_PLAN_LEV` | https://www.wellspringpecos.com/ |
| `68952` | Wy' East Pointe | `TIER_1_API_ENTRATA_NO_RESPONSE` | https://www.livewyeast.com/ |
| `1352` | Pinebrook | `TIER_3_DOM_GENERIC` | https://www.northbrookandpinebrookridgeland.com/ |
| `13756` | Ellis Midtown | `TIER_1_DOM_MARKETAPTS_B_PLAN_LEVEL` | www.ellismidtown.com |
| `229374` | Avoca | `TIER_1_API_ONESITE_NO_RESPONSE_PLA` | https://www.avocaapartments.com/ |
| `289291` | Vivo Living Miamisburg | `TIER_3_DOM_GENERIC` | https://www.vivolivingmiamisburg.com/ |
| `34195` | Mill Valley Estates | `TIER_1_API_REALPAGE_CWS_UNITS` | http://www.millvalleyapts.com/ |
| `4170` | Beechwood | `TIER_3_DOM_GENERIC` | www.beechwoodnc.net |
| `218586` | Cedar Ridge | `TIER_1_DOM_RENTALADDRESS_PLAN_LEVE` | https://cedarridgeapts.rentaladdress.com/ |
| `36175` | Chocolate Works | `TIER_1_API_RENTCAFE_SHAPE_REJECTED` | https://chocolateworks-living.com/ |
| `25734` | Los Feliz Summit | `TIER_1_API_RENTCAFE_NO_RESPONSE_PL` | https://anchorpacifica.com/residential-properties/los-f… |
| `18158` | Madrid | `TIER_1_API_RENTCAFE_SHAPE_REJECTED` | https://anchorpacifica.com/residential-properties/madri… |
| `39281` | Brandywine Hundred | `TIER_3_DOM_GENERIC` | www.brandywine100.com |
| `19558` | Henry On The Park | `TIER_3_DOM_GENERIC` | https://www.henryonthepark.com/ |
| `97533` | Hudson Park River Club | `TIER_3_DOM_GENERIC` | https://www.livehudsonpark.com/ |
| `1494` | Hunters Glen | `TIER_3_DOM_GENERIC` | https://www.huntersglenapthomes.com/ |
| `239348` | International City Chalets | `TIER_3_DOM_GENERIC` | https://www.internationalcityapts.com/ |
| `281149` | Irondale at Wharton | `TIER_3_DOM_GENERIC` | http://irondaleatwharton.com/ |
| `218187` | Lafayette Townhomes | `TIER_3_DOM_GENERIC` | https://apartmentsniagara.com/property-detail/?ID=46 |
| `49102` | Parkview Apartments | `TIER_3_DOM_GENERIC` | https://pmsi.biz/details/?pid=30 |
| `57136` | Parkway Lofts | `TIER_3_DOM_GENERIC` | http://www.parkwaylofts.com/ |
| `49598` | Shelard Village | `TIER_3_DOM_GENERIC` | https://shelardvillage.com/ |
| `14522` | The Charles at Bexley | `TIER_3_DOM_GENERIC` | https://www.thecharlesatbexley.com/ |
| `26162` | The Drake | `TIER_3_DOM_GENERIC` | http://www.thedrakeapts.com/ |
| `217605` | Bennett Pointe | `TIER_3_DOM_GENERIC` | https://www.livebennettpointe.com/ |
| `60939` | Greenarch | `TIER_3_DOM_GENERIC` | http://www.greenarchtulsa.com/ |
| `34909` | Polo Downs | `TIER_3_DOM_GENERIC` | http://www.polodowns.com/ |
| `39573` | Prime at Gardenside | `TIER_3_DOM_GENERIC` | https://www.liveprimegardenside.com/ |
| `1777` | Rustic Woods | `TIER_3_DOM_GENERIC` | https://www.rusticwoodsapts.com/ |
| `63462` | Telfair Lofts | `TIER_3_DOM_GENERIC` | https://apartmentssugarlandtexas.com/ |
| `65677` | The Vue at Creve Coeur | `TIER_3_DOM_GENERIC` | http://www.cthevue.com/ |
| `16196` | Village Square Apartments | `TIER_3_DOM_GENERIC` | http://www.villagesquarewheaton.com/Home/Index/36189 |
| `16377` | Westshore | `TIER_3_DOM_GENERIC` | www.westshoretampabay.com/ |
| `274954` | Hurston | `TIER_1_API_SPHEREXX_NO_RESPONSE_PL` | https://www.hurstonjacksonville.com/ |
| `68505` | 30Sixty Apartments | `SYNDICATION_ONLY_SQUARESPACE_PLAN_` | https://www.30sixtyapts.com/ |
| `241432` | Cricket Flats | `SYNDICATION_ONLY_SQUARESPACE_PLAN_` | http://cricketflats.com/ |
| `51921` | Deer Run | `TIER_3_DOM_GENERIC` | https://www.liveatdeerrunapts.com/ |
| `34523` | Constellation Ranch | `SYNDICATION_ONLY_WIX_PLAN_LEVEL` | https://www.constellationranchtx.com/ |
| `217343` | Eagle Harbor II | `SYNDICATION_ONLY_WIX_PLAN_LEVEL` | https://www.liveeagleharbor.com/ |
| `67150` | Harbor Vista at Crawford Street | `SYNDICATION_ONLY_WIX_PLAN_LEVEL` | https://www.liveharborvista.com/ |
| `23963` | Luna Bear Arcos | `SYNDICATION_ONLY_WIX_PLAN_LEVEL` | http://www.arcosphx.com/ |
| `240745` | The Allure at Jefferson I | `SYNDICATION_ONLY_WIX_PLAN_LEVEL` | https://www.liveallureva.com/ |
| `69203` | The Marq | `SYNDICATION_ONLY_WIX_PLAN_LEVEL` | https://www.livemarqvb.com/ |

---

Machine-readable: `seed_targets.json`, `route_targets.json`, `none231.csv` in the
session scratchpad. Source: `gs://jugnu-canary/runs/2026-07-26-plancohort/`.
See also `HANDOVER_2026-07-26_PLAN_LEVEL_RECOVERY.md`.
