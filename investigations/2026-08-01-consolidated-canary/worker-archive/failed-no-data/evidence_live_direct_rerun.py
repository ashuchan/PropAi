from __future__ import annotations

import asyncio

from ma_poc.fetch.contracts import FetchOutcome, FetchResult, RenderMode
from ma_poc.pms.adapters._probe import probe_get

import evidence_rerun as archived_runner


def live_fetch_for(record: dict) -> FetchResult | None:
    url = str(record.get("website") or "")
    if not url:
        return None
    response = probe_get(
        url,
        timeout=25,
        unlocker=False,
        proxies={},
        verify=True,
        retries=1,
    )
    body = str(response.text or "").encode("utf-8", "replace")
    final_url = str(getattr(response, "url", "") or url)
    return FetchResult(
        url=url,
        outcome=(
            FetchOutcome.OK
            if int(getattr(response, "status_code", 0) or 0) == 200
            else FetchOutcome.BOT_BLOCKED
        ),
        status=int(getattr(response, "status_code", 0) or 0),
        body=body,
        headers=dict(getattr(response, "headers", {}) or {}),
        render_mode=RenderMode.GET,
        final_url=final_url,
        attempts=1,
        elapsed_ms=0,
    )


if __name__ == "__main__":
    archived_runner.fetch_for = live_fetch_for
    asyncio.run(archived_runner.main())
