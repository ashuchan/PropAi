"""Default factory functions for signal engine components.

Centralises all configuration (field combinations, media filters, scoring
tables) so there is exactly ONE place to change each constant.

The factory pattern (DI for _UNIT_SIGNAL_KEYS) avoids circular imports:
  qualifier.py has zero imports from pms/adapters/
  defaults.py imports _UNIT_SIGNAL_KEYS from _merge_fns and passes it in.

Invariants enforced here (verified by tests/pms/signal_engine/test_qualifier.py):
  - All FieldCombination keys are lowercase (frozenset literals below are lc)
  - MediaTypeFilter covers JS/CSS/font/image
  - blocked_ttl_days=14, min_noise_verdicts=2 match the spec
"""

from __future__ import annotations

from ma_poc.pms.signal_engine.models import SourceKind
from ma_poc.pms.signal_engine.qualifier import (
    FieldCombination,
    MediaTypeFilter,
    SourceQualifier,
)
from ma_poc.pms.signal_engine.ranker import ScoringTables, SourceRanker

# ── Scoring constants (single source of truth) ────────────────────────────────
# These replace the scattered constants in scraper.py.
# scraper.py imports these during Phase 2; the definitions there are removed
# in Phase 4 after regression tests pass.

LLM_HINT_SCORE: int = 10_000
EMBEDDED_PORTAL_SCORE: int = 10_000
PMS_PRIOR_SCORE: int = 5_000

# Shared media-type filter — used by both create_default_qualifier and
# create_rentcafe_qualifier so the blocked sets are never out of sync.
DEFAULT_MEDIA_FILTER: MediaTypeFilter = MediaTypeFilter(
    blocked_content_types=frozenset({
        "text/javascript",
        "text/css",
        "font/",
        "image/",
        "application/font",
        "application/x-font",
    }),
    blocked_url_suffixes=frozenset({
        ".js", ".css", ".woff", ".woff2", ".ttf", ".otf",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    }),
)

DEFAULT_KIND_BASE_SCORES: dict[SourceKind, int] = {
    SourceKind.PROFILE_WINNING:  10_001,   # profile:winning_page_url sentinel
    SourceKind.LLM_HINT:         10_000,   # replaces _LLM_HINT_SCORE
    SourceKind.EXTERNAL_PORTAL:  10_000,   # replaces _EMBEDDED_PORTAL_SCORE
    SourceKind.PROFILE_NAV_HINT:  9_500,
    # API/JSON sources are ranked above all navigation hints — we already have
    # the data, no additional fetch needed.  known_endpoint boost (+500) lifts
    # a recognized API endpoint to 10_000, matching LLM hints.
    SourceKind.API_RESPONSE:      9_500,   # network-captured response (was 8_000)
    SourceKind.EMBEDDED_JSON:     9_000,   # SSR JSON blobs (was 7_500)
    SourceKind.JSON_LD:           8_500,   # Schema.org structured data (was 6_000)
    SourceKind.DOM_SECTION:       5_500,
    SourceKind.PMS_PRIOR:         5_000,   # replaces _PMS_PRIOR_SCORE
    SourceKind.UNIVERSAL_PRIOR:   4_500,
    SourceKind.INTERNAL_LINK:     4_000,   # base, augmented by keyword scoring
}

DEFAULT_ANCHOR_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("availability", 100),
    ("apartments & pricing", 100),
    ("floor plans & pricing", 100),
    ("floor plan", 90),
    ("floor-plan", 90),
    ("floorplan", 85),
    ("pricing", 80),
    ("rent", 70),
    ("find your home", 88),
    ("find a home", 88),
    ("pick your home", 88),
    ("pick a home", 88),
    ("choose your home", 88),
    ("search homes", 85),
    ("search apartments", 85),
    ("view availability", 88),
    ("check availability", 88),
    ("available homes", 85),
    ("available apartments", 85),
    ("see available", 80),
    ("view floor plan", 88),
    ("browse floor plans", 88),
    ("view floorplan", 88),
    ("view floor plans", 88),
    ("apartment", 60),
    ("unit", 55),
    ("lease", 50),
    ("tour", 40),
    ("apply", 30),
    ("schedule", 20),
)

DEFAULT_PATH_KEYWORDS: tuple[tuple[str, int], ...] = (
    ("/floor-plan", 95),
    ("/floorplan", 90),
    ("/availability", 95),
    ("/apartments-pricing", 95),
    ("/view-availability", 95),
    ("/pricing", 80),
    ("/apartments", 80),
    ("/rent", 60),
    ("/units", 85),
    ("/leasing", 50),
    ("/lease", 45),
    ("/floorplans", 90),
    ("/availabilities", 95),
    ("/conventional/", 88),
    ("/conventional", 85),
    # Apartment model pages (used by some marketing sites for floor plans)
    ("/models", 85),
    ("/find-your-home", 88),
    ("/search", 50),
    # 2026-05-13 (May-13 manual QC): patterns added based on 400-property
    # ground-truth tagging. Sync with resolver._CTA_PATH_RE.
    ("/floor-plans-and-pricing", 95),  # verbose variant
    ("/floor-plans.aspx", 90),
    ("/floorplans.aspx", 90),
    ("/plans.html", 85),
    ("/plans.asp", 85),
    ("/units-available", 88),
    ("/townhome-floorplans", 90),
    ("/vacancies", 85),
    ("/check-availability", 90),
    ("/floorplan-availability", 90),
    ("/interactive-site-map", 85),     # RentManager sitemap with embedded units
    ("/oleapplication", 70),           # Entrata Online Leasing Application
    # Pattern A: per-floor-plan detail-page navigation. Floor-plan card
    # sites like liveatsurf.com link from /floor-plans/ to /apartment/<slug>/
    # for each plan. The detail page has the actual rent. Singular
    # `/apartment/` (NOT `/apartments`) is the differentiator.
    #
    # Weight 88 (not lower) so it passes the ≥88 gate in scraper.py's
    # floor-plan-accumulation mode. _try_link_hop accumulates units across
    # per-card sub-pages only when the link scores ≥ 88 (lower-scored
    # candidates are speculative).
    ("/apartment/", 88),
    ("/home/", 65),                    # /home/<slug> per-property detail
    # Portfolio PMC site patterns — Princeton Mgmt, Beacon Mgmt etc.
    # Weight 88 to enable accumulation through portfolio site nav.
    ("/communities/", 88),
    ("/community/", 85),
    ("/property/", 85),
    ("/properties/", 85),
    ("/apartment-communities/", 88),
    ("/our-properties/", 85),
    ("/our-communities/", 85),
)

DEFAULT_HOST_KEYWORDS: tuple[tuple[str, int], ...] = (
    (".rentcafe.com", 120),
    (".appfolio.com", 120),
    (".onlineleasing.realpage.com", 120),
    ("sightmap.com", 110),
    (".entrata.com", 115),
    ("commoncf.entrata.com", 115),
    ("prospectportal.com", 115),
    # Leasing portals / online-leasing platforms
    ("securecafe.com", 120),        # ILS / SecureCafe online leasing
    ("knockrentals.com", 115),      # Knock CRM leasing portal
    ("leasehawk.com", 115),         # LeasHawk leasing CRM
    ("rentgrata.com", 80),          # Referral, lower priority
    # 2026-05-13 (May-13 manual QC + live-probe): cross-domain portals
    # observed on 98 rebrand cases + 30-property untagged sample.
    # Sync with resolver._LEASING_PORTAL_DOMAINS.
    (".securecafenet.com", 115),     # SecureCafe alt domain
    ("yottareal.com", 115),          # adaraportal.yottareal.com (Yardi product)
    ("mriprospectconnect.com", 115), # MRI Software portal
    ("showmojo.com", 110),           # ShowMojo unit-tour platform
    ("apartmentsearch.com", 105),    # CORT aggregator
    ("selftournow.com", 110),        # TouchTour / Engrain self-tour
    ("ovationco.com", 110),          # Ovation Property Management
    ("doorway.knck.io", 115),        # Knock subdomain
    ("myresman.com", 115),           # ResMan PMS portal
    ("reslisting.com", 110),         # marquette-management.reslisting.com
    ("rentcafewebsite.com", 115),    # legacy *.rentcafewebsite.com
    # 2026-05-21 grind600 findings — residents-only SPA portals
    # anchored on marketing-CMS sites. Score parity with the other
    # cross-domain portals; resolver._LEASING_PORTAL_DOMAINS skips
    # them as candidate floor-plan pages (login screen only).
    ("goprisma.com", 115),           # GoPrisma — 13/600 sites (Angular SPA)
    ("fortresstech.io", 115),        # FortressTech — 7/600 sites (UUID-scoped)
)

DEFAULT_PMS_PRIORS: dict[str, tuple[str, ...]] = {
    "rentcafe": ("/floorplans", "/availability", "/apartments"),
    "entrata": ("/floorplans", "/conventional/", "/apartments/", "/availability", "/leasing"),
    "appfolio": ("/listings", "/apartments", "/floor-plans"),
    "onesite": ("/floorplans", "/availability", "/apartments"),
    "realpage_oll": ("/floorplans", "/availability"),
    "sightmap": ("/floorplans", "/availability"),
    "avalonbay": ("/floor-plans-pricing", "/apartments"),
    "amli": ("/floor-plans", "/availability"),
    "funnel": ("/floorplans", "/availability"),
    # 2026-05-13 — Spherexx Presentation Software ("Convert"). Properties
    # using Spherexx commonly land at /interactive-site-map/ on the vanity
    # host; the actual data API lives on presentation.spherexx.app/api/unit.
    # Resolver-only fallback paths — when host detection picks "spherexx"
    # without a same-page widget, prefer these vanity sub-paths first.
    "spherexx": ("/interactive-site-map", "/floor-plans", "/availability"),
    # 2026-05-17 — Repli360/rrac popup family. The JS-rendered
    # getUnitListByFloor "View Details" anchors live on the floor-plans
    # page; prefer those sub-paths when host/HTML detection picks
    # "repli360" without the widget on the entry page.
    "repli360": ("/floor-plans", "/floorplans", "/availability"),
    # 2026-05-17 — pre-existing gap closed: both have a
    # ``matches_response_body`` checker but lacked a link-hop prior.
    # apts247: the same-origin ``/api/v1/floorplans/?api_key=`` widget +
    # api_key render on the floor-plans page. resman: the public
    # ``<client>.myresman.com/Portal/Applicants/Availability`` portal is
    # linked from the property's ``/floorplans/`` page.
    "apts247": ("/floorplans", "/floor-plans", "/availability"),
    "resman": ("/floorplans", "/availability", "/apartments"),
    # 2026-05-17 — Essex Property Trust. Per-unit /api availability
    # calls fire from the floor-plans-and-pricing page.
    "essex": ("/floor-plans-and-pricing", "/floor-plans", "/floorplans"),
    # 2026-05-18 — RentManager/iLoveLeasing. The Search_Result URL is
    # usually verbatim in the static shell; when detection picks
    # "rentmanager" without it, the floor-plans / availability / sitemap
    # sub-paths are the most likely carriers of the embedded endpoint.
    "rentmanager": ("/floorplans", "/floor-plans", "/availability", "/interactive-site-map"),
    # 2026-05-25 — Edifice CMS (Hexagon IT Solutions). The /floorplans.php
    # PHP page carries the \`\`getFloorPlan()\`\` ajax block with the
    # property UUID — that is the only URL the adapter needs. .php is
    # the canonical CMS extension; resolver also tries the bare path in
    # case the operator hides the extension.
    "edificecms": ("/floorplans.php", "/floorplans", "/floor-plans", "/availability"),
    # 2026-05-25 — ThinkRESIDE / Resite Multi Family Marketing.
    # Pattern-A themes (bns-community2019 / towncommunity) put the
    # plan index at /floorplans; Pattern-B (ascent) puts plan cards
    # on the home page but per-plan drills are also at /floorplans/{slug}.
    "thinkreside": ("/floorplans", "/floor-plans", "/availability"),
}

DEFAULT_UNIVERSAL_PRIORS: tuple[str, ...] = (
    "/floorplans",
    "/floor-plans",
    "/Floor-plans.aspx",
    "/floorplans.aspx",
    "/availability",
    "/models",
    "/view-availability",
    "/apartments",
    "/units",
    "/leasing",
)


# ── Qualifier toggle flags ────────────────────────────────────────────────────
# Set False to disable a combination without deleting its definition.
# Disabled combinations still appear in the code for reference and easy
# re-enablement; they are simply excluded from the active list at runtime.
_QUALIFIER_UNIT_GENERIC_ENABLED: bool = False      # too broad — admits any 2 unit keys
_QUALIFIER_RENTCAFE_FP_ENABLED: bool = False       # RentCafe-path only; use create_rentcafe_qualifier()
_QUALIFIER_RENTCAFE_UNIT_RENT_ENABLED: bool = False  # same
_QUALIFIER_SIGHTMAP_UNIT_ENABLED: bool = False     # unit_number+price alone is too weak


def create_default_qualifier() -> SourceQualifier:
    """Build the canonical SourceQualifier.

    Active combinations require cross-group field coverage (bed + bath + area
    OR bed + bath + floor-plan name).  Looser combinations are defined below
    but disabled via the ``_QUALIFIER_*_ENABLED`` flags — flip to True to
    re-enable without a code deletion.  RentCafe-specific combinations live in
    create_rentcafe_qualifier().
    """
    # _lc_unit_keys used only when _QUALIFIER_UNIT_GENERIC_ENABLED is True.
    if _QUALIFIER_UNIT_GENERIC_ENABLED:
        from ma_poc.pms.adapters._merge_fns import _UNIT_SIGNAL_KEYS as _USK
        _lc_unit_keys: frozenset[str] = frozenset(k.lower() for k in _USK)
    else:
        _lc_unit_keys = frozenset()

    # ── Disabled combinations (kept for reference) ────────────────────────────
    _disabled: list = []
    if _QUALIFIER_UNIT_GENERIC_ENABLED:
        _disabled.append(FieldCombination(
            keys=_lc_unit_keys,
            min_count=2,
            label="unit_generic",
        ))
    if _QUALIFIER_RENTCAFE_FP_ENABLED:
        _disabled.append(FieldCombination(
            keys=frozenset({
                "floorplanname", "floorplanid", "minimumrent",
                "maximumrent", "availableunitscount", "availabilityurl",
            }),
            min_count=3,
            label="rentcafe_floor_plan",
        ))
    if _QUALIFIER_RENTCAFE_UNIT_RENT_ENABLED:
        _disabled.append(FieldCombination(
            keys=frozenset({"rentcafeapartmentid", "unitrent", "marketrent"}),
            min_count=2,
            label="rentcafe_unit_rent",
        ))
    if _QUALIFIER_SIGHTMAP_UNIT_ENABLED:
        _disabled.append(FieldCombination(
            keys=frozenset({"unit_number", "price", "area", "available_on"}),
            min_count=2,
            label="sightmap_unit",
        ))

    return SourceQualifier(
        combinations=_disabled + [
            # ── Floor plan: bed + bath + area ─────────────────────────────────
            # Cross-group: requires ≥1 bed key AND ≥1 bath key AND ≥1 area key.
            # A response with three bed/bath synonyms but no sqft/area is NOT
            # admitted — it carries no meaningful unit data.
            FieldCombination(
                keys=frozenset({
                    "beds", "bedrooms",
                    "baths", "bathrooms",
                    "sqft", "area",
                }),
                min_count=3,
                label="floor_plan_bed_bath_area",
                required_groups=(
                    frozenset({"beds", "bedrooms"}),
                    frozenset({"baths", "bathrooms"}),
                    frozenset({"sqft", "area"}),
                ),
            ),
            # ── Floor plan: bed + bath + floor plan name ──────────────────────
            # Cross-group: requires ≥1 bed key AND ≥1 bath key AND a floor plan
            # name field. Catches plan-level APIs that separate rent from layout.
            FieldCombination(
                keys=frozenset({
                    "beds", "bedrooms",
                    "baths", "bathrooms",
                    "floor_plan_name", "floorplanname",
                }),
                min_count=3,
                label="floor_plan_bed_bath_name",
                required_groups=(
                    frozenset({"beds", "bedrooms"}),
                    frozenset({"baths", "bathrooms"}),
                    frozenset({"floor_plan_name", "floorplanname"}),
                ),
            ),
        ],
        media_filter=DEFAULT_MEDIA_FILTER,
        blocked_ttl_days=14,
        min_noise_verdicts=2,
    )


def create_default_ranker(fuzzy_threshold: int = 80) -> SourceRanker:
    """Build the canonical SourceRanker with all scoring tables.

    Args:
        fuzzy_threshold: Minimum ``rapidfuzz.fuzz.partial_ratio`` score (0–100)
            for an anchor-text keyword to contribute any score at all.
            Defaults to 80 — tight enough to suppress unrelated text while
            admitting near-matches like "floor plans" → "floor plan".
    """
    return SourceRanker(
        tables=ScoringTables(
            kind_base_scores=DEFAULT_KIND_BASE_SCORES,
            anchor_keywords=DEFAULT_ANCHOR_KEYWORDS,
            path_keywords=DEFAULT_PATH_KEYWORDS,
            host_keywords=DEFAULT_HOST_KEYWORDS,
            pms_priors=DEFAULT_PMS_PRIORS,
            universal_priors=DEFAULT_UNIVERSAL_PRIORS,
        ),
        fuzzy_threshold=fuzzy_threshold,
    )


def create_rentcafe_qualifier() -> SourceQualifier:
    """Build a SourceQualifier scoped to RentCafe-specific API shapes.

    Used by rentcafe._is_rentcafe_response() to delegate its field-combination
    checks to the qualifier, keeping all FieldCombination definitions in one
    place (defaults.py).

    Deliberately excludes the ``unit_generic`` combination — that would admit
    any generic unit-shaped API, defeating RentCafe-specific routing. The
    ``api=rentcafe`` value sentinel is a content check (not a key check) and
    stays in _is_rentcafe_response() alongside this qualifier.
    """
    return SourceQualifier(
        combinations=[
            FieldCombination(
                keys=frozenset({
                    "floorplanname", "floorplanid", "minimumrent",
                    "maximumrent", "availableunitscount", "availabilityurl",
                }),
                min_count=3,
                label="rentcafe_floor_plan",
            ),
            FieldCombination(
                keys=frozenset({
                    "rentcafeapartmentid",
                    "rentcafefloorplanid",
                    "rentcafepropertyid",
                }),
                min_count=2,
                label="rentcafe_unit",
            ),
            FieldCombination(
                keys=frozenset({
                    "rentcafeapartmentid",
                    "unitrent",
                    "marketrent",
                }),
                min_count=2,
                label="rentcafe_unit_rent",
            ),
        ],
        media_filter=DEFAULT_MEDIA_FILTER,
        blocked_ttl_days=14,
        min_noise_verdicts=2,
    )
