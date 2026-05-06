"""Per-property network capture buffer for the extraction engine.

Extracted from scripts/entrata.py — encapsulates the XHR/fetch interception
buffer that was previously inlined inside the scrape() function.

Each property scrape should use its own NetworkCapture instance (call reset()
between properties when reusing a browser context).
"""
from __future__ import annotations

from typing import Any, Callable

from extraction.engine.noise_filter import (
    _filter_entrata_widget_response,
    looks_like_availability_api,
)


class NetworkCapture:
    """Instance-scoped buffer for captured API responses.

    Usage::

        nc = NetworkCapture()
        handler = nc.create_handler()
        page.on("response", handler)
        # ... navigate ...
        units = parse_api_responses(nc.responses)
    """

    def __init__(self) -> None:
        self._api_responses: list[dict] = []
        self._seen_api_urls: set[str] = set()

    @property
    def responses(self) -> list[dict]:
        return list(self._api_responses)

    def reset(self) -> None:
        self._api_responses.clear()
        self._seen_api_urls.clear()

    def create_handler(self) -> Callable:
        """Return an async Playwright response handler bound to this capture buffer."""

        async def handle_response(response: Any) -> None:
            try:
                url = response.url
                if not looks_like_availability_api(url):
                    return

                is_widget_url = "/apartments/module/widgets/" in url.lower()
                if url in self._seen_api_urls and not is_widget_url:
                    return

                ct = (response.headers or {}).get("content-type", "").lower()
                if "json" not in ct and not url.lower().endswith(".json"):
                    return

                if not (200 <= response.status < 300):
                    return

                body = await response.json()
                self._seen_api_urls.add(url)

                if "/apartments/module/widgets/" in url.lower() and isinstance(body, dict):
                    filtered = _filter_entrata_widget_response(body)
                    if filtered is None:
                        return
                    widget_data = body.get("widget_data", {})
                    content = widget_data.get("content", {})
                    fp_list = content.get("floor_plans", {})
                    if isinstance(fp_list, dict):
                        fp_list = fp_list.get("floor_plans", [])
                    if isinstance(fp_list, list) and fp_list:
                        body = fp_list

                self._api_responses.append({"url": url, "body": body})

            except Exception as exc:
                exc_str = str(exc)
                if "was not received" not in exc_str and "Target closed" not in exc_str:
                    pass  # Swallow capture errors — never fail the scrape

        return handle_response
