"""generic:blocked_filter — drops API responses in profile.blocked_endpoints."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


class BlockedFilterTier:
    """Filters out API responses whose URLs are in profile.blocked_endpoints.

    Runs before any parser: saves token spend on known-bad endpoints
    (chatbot configs, analytics pixels, CMS widgets).
    """

    @staticmethod
    def run(
        api_responses: list[dict[str, Any]],
        profile: Any,
        log_attempt: Any,
    ) -> list[dict[str, Any]]:
        """Filter blocked URLs from api_responses.

        Returns the filtered list; also calls log_attempt when URLs are dropped.
        Never raises — if profile inspection fails the original list is returned.
        """
        blocked_urls: set[str] = set()
        if profile is not None:
            try:
                for be in getattr(profile.api_hints, "blocked_endpoints", []) or []:
                    pat = getattr(be, "url_pattern", None)
                    if pat:
                        blocked_urls.add(str(pat))
            except Exception:
                blocked_urls = set()

        if not blocked_urls:
            return api_responses

        before = len(api_responses)
        filtered = [r for r in api_responses if r.get("url", "") not in blocked_urls]
        dropped = before - len(filtered)
        if dropped:
            log_attempt(
                "generic:blocked_filter",
                "ran_units",
                units=0,
                reason=f"dropped {dropped} API(s) from profile.blocked_endpoints",
            )
        return filtered
