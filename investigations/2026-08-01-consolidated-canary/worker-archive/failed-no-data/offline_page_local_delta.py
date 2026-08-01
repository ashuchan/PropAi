"""Offline-only delta measurement for page-local static recovery.

Every network seam returns an inert response. The script compares the 33
archived bodies accepted by the new helper with the same local scraper path
while that helper is disabled.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import sys
import urllib.request
from pathlib import Path

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.models.scrape_profile import ScrapeProfile
from ma_poc.pms import scraper as scraper_mod
from ma_poc.pms.adapters.base import AdapterContext, AdapterResult
from ma_poc.pms.detector import detect_pms

# Importing the package bootstraps every adapter before aliases are patched.
import ma_poc.pms.adapters  # noqa: F401, E402
import ma_poc.pms.adapters._probe as probe  # noqa: E402


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")


class OfflineResponse:
    status_code = 599
    status = 599
    text = ""
    content = b""
    url = ""
    headers: dict[str, str] = {}
    ok = False

    def json(self) -> dict:
        return {}

    def raise_for_status(self) -> None:
        raise RuntimeError("offline replay: network disabled")

    def __enter__(self) -> "OfflineResponse":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False


def offline_response(*_args: object, **_kwargs: object) -> OfflineResponse:
    return OfflineResponse()


class OfflineAsyncClient:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    async def __aenter__(self) -> "OfflineAsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> bool:
        return False

    async def get(self, *_args: object, **_kwargs: object) -> OfflineResponse:
        return OfflineResponse()

    async def post(self, *_args: object, **_kwargs: object) -> OfflineResponse:
        return OfflineResponse()

    async def put(self, *_args: object, **_kwargs: object) -> OfflineResponse:
        return OfflineResponse()

    async def request(self, *_args: object, **_kwargs: object) -> OfflineResponse:
        return OfflineResponse()


class OfflineSession:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass

    def __enter__(self) -> "OfflineSession":
        return self

    def __exit__(self, *_args: object) -> bool:
        return False

    get = offline_response
    post = offline_response
    put = offline_response
    request = offline_response


def disable_network() -> None:
    old_functions = [probe.probe_get, probe.probe_post]
    for name in ("probe_get_with_browser_cookies", "unlocker_get"):
        value = getattr(probe, name, None)
        if callable(value):
            old_functions.append(value)
    probe.probe_get = offline_response
    probe.probe_post = offline_response
    for name in ("probe_get_with_browser_cookies", "unlocker_get"):
        if hasattr(probe, name):
            setattr(probe, name, offline_response)

    for module in list(sys.modules.values()):
        namespace = getattr(module, "__dict__", {})
        for key, value in list(namespace.items()):
            if any(value is function for function in old_functions):
                namespace[key] = offline_response

    urllib.request.urlopen = lambda *_a, **_kw: (_ for _ in ()).throw(
        RuntimeError("offline replay: network disabled")
    )

    try:
        import httpx

        httpx.AsyncClient = OfflineAsyncClient
        httpx.Client = OfflineSession
    except Exception:
        pass
    try:
        import requests

        requests.get = offline_response
        requests.post = offline_response
        requests.request = offline_response
        requests.sessions.Session.request = offline_response
    except Exception:
        pass
    try:
        from curl_cffi import requests as curl_requests

        curl_requests.get = offline_response
        curl_requests.post = offline_response
        curl_requests.put = offline_response
        curl_requests.request = offline_response
        curl_requests.Session = OfflineSession
    except Exception:
        pass

    import ma_poc.fetch.hyperbrowser_backend as hyperbrowser_backend

    async def no_hyperbrowser(*_args: object, **_kwargs: object) -> tuple[int, str]:
        return 0, ""

    hyperbrowser_backend.hb_raw_get = no_hyperbrowser


def fetch_for(record: dict) -> FetchResult | None:
    property_id = str(record["property_id"])
    path = ROOT / "raw_all" / f"{property_id}.html.gz"
    if not path.exists():
        return None
    body = gzip.open(path, "rb").read()
    url = record.get("website") or ""
    return FetchResult(
        url=url,
        outcome=FetchOutcome.OK,
        status=200,
        body=body,
        headers={},
        render_mode=RenderMode.RENDER,
        final_url=url,
        attempts=1,
        elapsed_ms=0,
    )


async def scrape_baseline(record: dict, fetch_result: FetchResult) -> dict:
    property_id = str(record["property_id"])
    profile_path = ROOT / "profiles" / f"{property_id}.json"
    try:
        profile = (
            ScrapeProfile.model_validate_json(profile_path.read_text())
            if profile_path.exists()
            else None
        )
    except Exception:
        profile = None
    csv_row = {
        "apartmentid": property_id,
        "name": record.get("proj_name") or "",
        "address": record.get("address") or "",
        "city": record.get("city") or "",
        "state": record.get("state") or "",
        "zip": record.get("zip_code") or "",
        "website": record.get("website") or "",
    }
    budget = {
        "llm_api_calls": 0,
        "llm_dom_calls": 0,
        "llm_monolithic": 0,
        "link_hop": 0,
        "_cost_cap_usd": 0,
    }
    try:
        result = await asyncio.wait_for(
            scraper_mod.scrape(
                record.get("website") or "",
                profile=profile,
                page=None,
                fetch_result=fetch_result,
                csv_row=csv_row,
                property_id=property_id,
                shared_budget=budget,
            ),
            timeout=5,
        )
    except Exception as exc:
        return {
            "property_id": property_id,
            "units": 0,
            "plans": 0,
            "error": type(exc).__name__,
        }
    return {
        "property_id": property_id,
        "units": len(result.get("units") or []),
        "plans": len(result.get("plan_summaries") or []),
        "adapter": result.get("_adapter_used"),
        "tier": result.get("extraction_tier_used"),
    }


async def main() -> None:
    disable_network()
    ledger = json.loads((ROOT / "failed344.json").read_text())
    original = scraper_mod._try_page_local_static_recovery
    candidates: list[tuple[dict, FetchResult]] = []
    for record in ledger:
        fetch_result = fetch_for(record)
        if fetch_result is None:
            continue
        url = record.get("website") or ""
        html = (fetch_result.body or b"").decode("utf-8", errors="replace")
        context = AdapterContext(
            base_url=url,
            detected=detect_pms(url, page_html=html),
            profile=None,
            expected_total_units=None,
            property_id=str(record["property_id"]),
            fetch_result=fetch_result,
        )
        if original(context, AdapterResult()) is not None:
            candidates.append((record, fetch_result))

    scraper_mod._try_page_local_static_recovery = lambda _ctx, _previous: None
    baseline = []
    for record, fetch_result in candidates:
        baseline.append(await scrape_baseline(record, fetch_result))
    scraper_mod._try_page_local_static_recovery = original

    nonempty = [row for row in baseline if row["units"] or row["plans"]]
    nonempty_ids = {row["property_id"] for row in nonempty}
    incremental_ids = [
        str(record["property_id"])
        for record, _fetch_result in candidates
        if str(record["property_id"]) not in nonempty_ids
    ]
    failed = [row for row in baseline if row.get("error")]
    print(
        json.dumps(
            {
                "candidate_count": len(candidates),
                "baseline_nonempty_count": len(nonempty),
                "incremental_recovery_count": len(candidates) - len(nonempty),
                "incremental_recovery_ids": incremental_ids,
                "errored_or_timed_out_count": len(failed),
                "baseline_nonempty": nonempty,
                "errors": failed,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
