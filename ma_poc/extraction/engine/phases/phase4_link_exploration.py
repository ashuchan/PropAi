"""Phase 4 — Link-by-Link Exploration with Network Observation.

Navigates prioritized links one at a time, observing which new APIs fire per
page. Collects LLM candidates for Phase 5. Also runs Phase 4.5 (LLM-guided
navigation for COLD profiles with 0 units after crawl).

Extracted from scripts/entrata.py scrape() Phase 4 / Phase 4.5 block.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from extraction.engine.noise_filter import filter_network_noise
from extraction.engine.phase_context import PhaseContext

_SCRIPTS_DIR = Path(__file__).resolve().parents[4] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from entrata import (  # type: ignore[import]
    MAX_CRAWL_PAGES,
    detect_leasing_portals,
    explore_link_with_observation,
    parse_api_responses,
    prioritize_links,
    probe_entrata_api,
    extract_embedded_json,
    parse_jsonld,
    parse_dom,
)


class Phase4LinkExploration:
    """Crawl prioritized links, observe new APIs, collect LLM candidates."""

    async def run(self, ctx: PhaseContext) -> None:
        if ctx.units:
            return

        # Profile skip: if HOT profile says only LLM/Vision works, skip crawl
        _profile_skip_crawl = (
            ctx.skip_to_tier is not None
            and ctx.skip_to_tier >= 4
            and not ctx.run_full_cascade
        )
        if _profile_skip_crawl:
            return

        crawl_queue = prioritize_links(
            ctx.internal_links,
            ctx.anchor_text_links,
            ctx.profile,
            ctx.base_url,
            property_city=ctx.property_city,
        )

        print(f"\n{'=' * 65}")
        print(f"  PHASE 4: Link Exploration — {len(crawl_queue)} links to visit")
        print(f"{'=' * 65}")
        for link in crawl_queue[:5]:
            print(f"     {link}")
        if len(crawl_queue) > 5:
            print(f"     ... and {len(crawl_queue) - 5} more")

        visited: set[str] = set()
        link_idx = 0

        while link_idx < len(crawl_queue) and len(visited) < MAX_CRAWL_PAGES:
            target = crawl_queue[link_idx]
            link_idx += 1
            if target in visited:
                continue
            visited.add(target)

            print(f"\n{'─' * 65}")
            print(f"  PHASE 4 [{len(visited)}/{MAX_CRAWL_PAGES}]: {target}")

            _is_cold = True
            if ctx.profile is not None:
                _conf = getattr(ctx.profile, "confidence", None)
                _mat = getattr(_conf, "maturity", "COLD") if _conf else "COLD"
                _is_cold = _mat == "COLD"

            link_units, new_resps, llm_cands = await explore_link_with_observation(
                ctx.page,
                target,
                ctx.api_responses,
                ctx.base_url,
                deep_click=_is_cold,
            )

            if new_resps:
                new_resps = filter_network_noise(new_resps, ctx.profile)

            if link_units:
                ctx.units = link_units
                ctx.explored_links_status[target] = True
                ctx.extraction_tier_used = "TIER_5_5_EXPLORATORY"
                ctx.winning_page_url = target
                print(f"  ✅ PHASE 4 (Exploration): {len(ctx.units)} units from {target[:80]}")
                break
            else:
                ctx.explored_links_status[target] = False
                ctx.llm_candidates.extend(llm_cands)
                if llm_cands:
                    print(f"    + {len(llm_cands)} promising APIs for LLM analysis")

            # Entrata API probe on each sub-page
            if not ctx.units:
                entrata_blobs = await probe_entrata_api(ctx.page, ctx.base_url)
                if entrata_blobs:
                    ctx.api_responses.extend(entrata_blobs)
                    units = parse_api_responses(entrata_blobs)
                    if units:
                        ctx.units = units
                        ctx.extraction_tier_used = "TIER_4_ENTRATA_API"
                        ctx.explored_links_status[target] = True
                        print(f"  ✅ TIER 4 (Entrata API probe): {len(units)} units/floor plans")
                        break

            # Leasing portal detection
            if not ctx.units:
                portal_urls = await detect_leasing_portals(ctx.page)
                for portal_url in portal_urls[:1]:
                    try:
                        from extraction.engine.browser_session import _goto_robust
                        await _goto_robust(ctx.page, portal_url, timeout_ms=30000)
                        await asyncio.sleep(2.0)
                        portal_units = parse_api_responses(ctx.api_responses)
                        if portal_units:
                            ctx.units = portal_units
                            ctx.extraction_tier_used = "TIER_5_PORTAL"
                            ctx.explored_links_status[target] = True
                            print(f"  ✅ TIER 5 (Portal): {len(ctx.units)} units")
                            break
                        portal_blobs = await extract_embedded_json(ctx.page)
                        if portal_blobs:
                            ctx.api_responses.extend(portal_blobs)
                            units = parse_api_responses(portal_blobs)
                            if units:
                                ctx.units = units
                                ctx.extraction_tier_used = "TIER_5_PORTAL"
                                ctx.explored_links_status[target] = True
                                print(f"  ✅ TIER 5 (Portal embedded): {len(units)} units")
                                break
                        units = await parse_jsonld(ctx.page)
                        if units:
                            ctx.units = units
                            ctx.extraction_tier_used = "TIER_5_PORTAL"
                            ctx.explored_links_status[target] = True
                            print(f"  ✅ TIER 5 (Portal JSON-LD): {len(units)} items")
                            break
                        units = await parse_dom(ctx.page, portal_url)
                        if units:
                            ctx.units = units
                            ctx.extraction_tier_used = "TIER_5_PORTAL"
                            ctx.explored_links_status[target] = True
                            print(f"  ✅ TIER 5 (Portal DOM): {len(units)} units")
                            break
                    except Exception as e:
                        print(f"  ⚠ Portal error: {e}")

            if ctx.units:
                break

        if not ctx.units:
            print(
                f"\n  ↳ Phase 4: explored {len(visited)} pages, "
                f"collected {len(ctx.llm_candidates)} LLM candidates"
            )

        ctx.property_links_crawled = list(visited)

        # Phase 4.5: LLM-guided navigation for COLD profiles with no candidates
        if (
            not ctx.units
            and not ctx.llm_candidates
            and not ctx.page_unreachable
            and len(visited) < MAX_CRAWL_PAGES
        ):
            await self._run_phase_4_5(ctx, visited)

    async def _run_phase_4_5(self, ctx: PhaseContext, visited: set[str]) -> None:
        _is_cold = True
        if ctx.profile is not None:
            _c = getattr(ctx.profile, "confidence", None)
            _m = getattr(_c, "maturity", "COLD") if _c else "COLD"
            _is_cold = _m == "COLD"

        if not _is_cold:
            return

        print(f"\n{'=' * 65}")
        print("  PHASE 4.5: LLM Navigation Discovery")
        print(f"{'=' * 65}")
        try:
            screenshot = await ctx.page.screenshot(full_page=True)
            from services.vision_extractor import suggest_navigation

            nav_suggestions = await suggest_navigation(
                screenshot=screenshot,
                property_context={
                    "property_name": ctx.property_name,
                    "website": ctx.base_url,
                },
                property_id=ctx.property_id,
            )

            if nav_suggestions:
                print(f"  LLM suggested {len(nav_suggestions)} navigation actions")
                for suggestion in nav_suggestions[:3]:
                    action = suggestion.get("action", "")
                    if action == "navigate":
                        target_url = suggestion.get("url", "")
                        if target_url and target_url not in visited:
                            print(f"    Navigate → {target_url[:80]}")
                            visited.add(target_url)
                            link_units, new_resps, llm_cands = await explore_link_with_observation(
                                ctx.page,
                                target_url,
                                ctx.api_responses,
                                ctx.base_url,
                                deep_click=True,
                            )
                            if new_resps:
                                new_resps = filter_network_noise(new_resps, ctx.profile)
                            if link_units:
                                ctx.units = link_units
                                ctx.extraction_tier_used = "TIER_4_5_LLM_NAV"
                                ctx.winning_page_url = target_url
                                print(f"  PHASE 4.5: {len(ctx.units)} units from {target_url[:80]}")
                                break
                            ctx.llm_candidates.extend(llm_cands)
                    elif action == "click":
                        selector = suggestion.get("selector", "")
                        text = suggestion.get("text", "")
                        if selector:
                            try:
                                print(f"    Click → {text!r} ({selector})")
                                api_before = len(ctx.api_responses)
                                await ctx.page.click(selector, timeout=5000)
                                await asyncio.sleep(2.0)
                                new_apis = ctx.api_responses[api_before:]
                                if new_apis:
                                    new_apis = filter_network_noise(new_apis, ctx.profile)
                                    from entrata import _response_looks_like_units  # type: ignore
                                    click_units = parse_api_responses(new_apis)
                                    if click_units:
                                        ctx.units = click_units
                                        ctx.extraction_tier_used = "TIER_4_5_LLM_NAV"
                                        print(f"  PHASE 4.5: {len(ctx.units)} units from click")
                                        break
                                    ctx.llm_candidates.extend(
                                        [r for r in new_apis if _response_looks_like_units(r.get("body"))]
                                    )
                            except Exception as e:
                                print(f"    Click failed: {e}")
            else:
                print("  No navigation suggestions from LLM")
        except ImportError:
            print("  (vision_extractor.suggest_navigation not available)")
        except Exception as e:
            print(f"  Phase 4.5 error: {e}")
