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
    # 2026-05-17 — strong "go here for unit data" cues observed on
    # per-plan detail-page anchors. PID 52331 alexandriacarmel surfaces
    # ``Only N left`` / ``View Details`` / ``Apply Now`` next to each
    # per-plan card; before this addition only the URL-shape boost
    # (slugged_plan_detail) carried these anchors, so generic-anchor
    # variants without a slug-shaped URL underscored.
    ("view details", 70),
    ("apply now", 60),
    # ``only`` is a prefix marker for ``only 1 left`` / ``only 4 left`` /
    # ``only available`` — strong availability signals. Substring match
    # via the existing keyword loop is sufficient; the trailing space
    # avoids matching ``only`` inside ``only-child`` / ``only one``.
    ("only ", 75),
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
    # ResMan property-management portal (rwfmat.myresman.com etc.)
    ("myresman.com", 115),
    # Yardi-hosted leasing portals
    ("yardi.com", 110),
    # BuildingLink / ResPage
    ("respage.com", 100),
    # Buildium tenant / prospect portal
    ("buildium.com", 100),
    # G5 Listing Widget API — unit/floor-plan data endpoint for G5-platform sites
    ("g5marketingcloud.com", 110),
    ("api.g5.com", 110),
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
    # 2026-05-13 port (Commit 11): server-only Tier-1 adapters. Their
    # ``matches_response_body`` checkers gate confirm_detection; the
    # link-hop priors below give the cascade canonical sub-paths to try
    # when the entry page doesn't carry the data directly.
    "cortland": ("/floorplans", "/floor-plans", "/availability"),
    "equity": ("/apartments", "/availability", "/units"),
    "rentmanager": ("/floorplans", "/floor-plans", "/availability"),
    # 2026-05-13 port (Commit 12): browser-intercept Tier-1 adapters.
    "g5": ("/floorplans", "/floor-plans", "/availability"),
    "knock": ("/floorplans", "/floor-plans", "/availability"),
    "irvine": ("/apartments", "/availability", "/units"),
    "apts247": ("/floorplans", "/availability"),
    # 2026-05-13 port (Commit 13): REIT adapters.
    "essex": ("/apartments", "/availability"),
    "maac": ("/floorplans", "/availability", "/apartments"),
    "rentvision": ("/floorplans", "/floor-plans", "/availability"),
    # 2026-05-21 port (P1a): EncoreSkyline marketing-template adapter.
    "encoreskyline_template": ("/floorplans", "/floor-plans"),
    # 2026-05-21 port (P2a): ResMan public availability portal.
    "resman": ("/floorplans", "/floor-plans", "/availability"),
    # 2026-05-21 port (Fix 5b): Aspen Square operator drill at
    # /apartments/{state}/{city}/{community}/floor-plans/{plan-slug}/.
    "aspensquare": ("/floor-plans", "/apartments"),
    # 2026-05-21 port (Fix 5c): Repli360 / rrac — fetches its own data
    # via POST so URL priors are best-effort for link-hop discovery.
    "repli360": ("/floorplans", "/floor-plans"),
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

# ── API noise blocklists (signal-level, not unit-level) ───────────────────────
# Hosts that are definitionally noise for captured API responses — analytics
# CDNs, captcha providers, OIDC endpoints, map tiles. These hosts appear in
# XHR/fetch interceptions but carry zero unit data regardless of URL path.
# Used by ``is_api_noise_response`` which is the single gate for all LLM-rescue
# candidate filtering — llm_api_rescue imports from here, not from its own defs.
DEFAULT_API_NOISE_HOSTS: frozenset[str] = frozenset({
    # Analytics / tag managers:
    "googleapis.com",
    "go-mpulse.net",
    "googletagmanager.com",
    "doubleclick.net",
    "facebook.com",
    "hotjar.com",
    "sentry.io",
    "userway.org",
    "omni.cafe",
    # Email / marketing automation:
    "klaviyo.com",
    # CMS CDNs:
    "static.cdn-website.com",       # Duda CMS
    # Widget / tour analytics:
    "enormapps.com",
    "tour.tourbuilder.com",
    # Map tiles (never unit data):
    "places.googleapis.com",
    "maps.googleapis.com",
    # Consent managers:
    "cmp.osano.com",
    "osano.com",
    # Chatbots:
    "app.meetelise.com",
    # Hyly EliseAI chatbot family — observed on PIDs 69188 (727westmadison),
    # 16139 (chaseknollsapts), 20959 (dovevalleyapts) in cloud run 2026-05-15.
    # The `my.hy.ly/chat/ssid` iframe returns chat-config JSON (NOT unit
    # data) but was getting counted as a candidate response because no
    # noise-host entry caught it. Already blacklisted at the portal-discovery
    # layer (_PORTAL_INFRA_BLACKLIST in _html_extract.py) — adding here
    # closes the rescue-path side too.
    "my.hy.ly",
    "chat.hy.ly",
    # Captcha providers:
    "challenges.cloudflare.com",
    "hcaptcha.com",
    "recaptcha.net",
})

# URL path fragments that are always noise regardless of host.
# These identify tag-managers, PWA manifests, auth flows, and CDN static bundles.
DEFAULT_API_NOISE_PATH_FRAGMENTS: frozenset[str] = frozenset({
    # Tag / analytics paths:
    "/tag-manager/",
    "/mapsjs/",
    "/gen_204",
    "/analytics/",
    "/gtag/",
    "/pixel",
    "/beacon",
    # Form / widget endpoints with no unit data:
    "/tour/availabilities",
    "/html_forms/",
    "/yext_reviews/",
    "/blurb/v1/",
    "/popdown/",
    "/forms/api/",
    "/speculations/rules/",
    # Entrata CMS module endpoints that are config-only (not unit data):
    # NOTE: /Apartments/module/widgets/ is INTENTIONALLY excluded from this
    # blocklist — it IS the primary Entrata floor-plan/availability data endpoint
    # (confirmed by production traces on PIDs 257356 and 252511). Blocking it
    # causes all intercepted Entrata widget responses to be silently discarded.
    "/Apartments/module/application_authentication/",
    "/Apartments/module/property_info/",
    # Map / auth:
    "$rpc/",
    "/realms/",
    "/openid-connect/",
    "/recaptcha",
    # Static asset bundles — CDN packs from AppFolio, RentCafe, Entrata etc:
    "/packs/",
    "/packs-test/",
    "/assets/application-",     # webpack fingerprinted bundles
    "/assets/vendor-",
    "/fonts/",
    # PWA / service worker:
    "/manifest.json",
    "/service-worker",
    "/sw.js",
})


def is_api_noise_response(url: str, content_type: str | None = None) -> bool:
    """Return True when the URL/content-type combination is definitionally noise.

    Single gate for LLM-rescue candidate filtering — replaces the scattered
    ``_RESCUE_NOISE_HOSTS``, ``_RESCUE_NOISE_PATH_FRAGMENTS``, and
    ``_STATIC_CONTENT_TYPES`` constants that used to live in llm_api_rescue.py.

    Check order (stops at first True):
      1. Content-type media check via ``DEFAULT_MEDIA_FILTER.blocks_raw()``
         (JS, CSS, fonts, images, text/html are never JSON unit data).
      2. Known-noise host match — subdomain-aware.
      3. Known-noise path fragment match.
    """
    if not url:
        return True
    if DEFAULT_MEDIA_FILTER.blocks_raw(url, content_type):
        return True
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    for h in DEFAULT_API_NOISE_HOSTS:
        if host == h or host.endswith("." + h):
            return True
    url_lower = url.lower()
    for frag in DEFAULT_API_NOISE_PATH_FRAGMENTS:
        if frag.lower() in url_lower:
            return True
    return False


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
                    "beds", "bedrooms", "no_of_bedroom",
                    "baths", "bathrooms", "no_of_bathroom", "no_of_bath",
                    "sqft", "area", "square_footage",
                }),
                min_count=3,
                label="floor_plan_bed_bath_area",
                required_groups=(
                    frozenset({"beds", "bedrooms", "no_of_bedroom"}),
                    frozenset({"baths", "bathrooms", "no_of_bathroom", "no_of_bath"}),
                    frozenset({"sqft", "area", "square_footage"}),
                ),
            ),
            # ── Floor plan: bed + bath + floor plan name ──────────────────────
            # Cross-group: requires ≥1 bed key AND ≥1 bath key AND a floor plan
            # name field. Catches plan-level APIs that separate rent from layout.
            FieldCombination(
                keys=frozenset({
                    "beds", "bedrooms", "no_of_bedroom",
                    "baths", "bathrooms", "no_of_bathroom", "no_of_bath",
                    "floor_plan_name", "floorplanname", "floorplan-name",
                }),
                min_count=3,
                label="floor_plan_bed_bath_name",
                required_groups=(
                    frozenset({"beds", "bedrooms", "no_of_bedroom"}),
                    frozenset({"baths", "bathrooms", "no_of_bathroom", "no_of_bath"}),
                    frozenset({"floor_plan_name", "floorplanname", "floorplan-name"}),
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
