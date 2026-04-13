"""
Async scraping fleet — daily coordinator for all 500 properties.

Acceptance criteria (CLAUDE.md PR-01):
- Loads properties.csv via stdlib csv with utf-8-sig (bug-hunt #10 — handles BOM
  and CRLF). pandas was originally specified, swapped here because pandas
  has no Windows ARM64 wheels and source builds need MSVC + meson.
- STABILISED: 1× daily; LEASE_UP: every 4h 8am–9pm property-local TZ (bug-hunt #11)
- MAX_CONCURRENT_BROWSERS enforced via asyncio.Semaphore (bug-hunt #2)
- Per-scrape: change-detection gate → browser → tier pipeline → vision banner → log
- One ScrapeEvent per scrape, written via EventLog
- Per-property extraction output written to data/extraction_output/{pid}/{date}.json
"""
from __future__ import annotations

import asyncio
import csv
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

from extraction.pipeline import run_extraction_pipeline
from extraction.tier5_vision import maybe_run_vision_fallback
from extraction.vision_banner import capture_banner
from extraction.vision_sample import select_for_sample, write_sample_comparison
from models.extraction_result import ExtractionResult
from models.scrape_event import ChangeDetectionResult, ScrapeEvent, ScrapeOutcome
from scraper.browser import BrowserFleet
from scraper.change_detection import ChangeDetector, StateStore
from scraper.proxy_manager import ProxyManager
from storage.event_log import EventLog, write_extraction_output

try:
    from zoneinfo import ZoneInfo

    from timezonefinder import TimezoneFinder

    _TZF: TimezoneFinder | None = TimezoneFinder()
except Exception:  # pragma: no cover
    _TZF = None
    ZoneInfo = None  # type: ignore[assignment,misc]


@dataclass
class PropertyRow:
    property_id: str
    url: str
    type: str  # "STABILISED" | "LEASE_UP"
    pms_platform: str | None = None
    zip: str | None = None
    state: str | None = None
    name: str | None = None


# Map RealPage CSV headers to internal names. Loader is tolerant of casing.
_HEADER_ALIASES: dict[str, str] = {
    "property id": "property_id",
    "property_id": "property_id",
    "id": "property_id",
    "property url": "url",
    "url": "url",
    "property type": "type",
    "type": "type",
    "pms platform": "pms_platform",
    "pms_platform": "pms_platform",
    "zip": "zip",
    "state": "state",
    "property name": "name",
    "name": "name",
}


def load_properties(csv_path: Path | str) -> list[PropertyRow]:
    """
    Load + normalize properties.csv. Tolerates the RealPage header format
    (`Property ID`, `Property URL`, `Property Type` = "Stabilized"/"Lease-Up",
    `PMS Platform`) and the CLAUDE.md internal format.
    Bug-hunt #10: encoding="utf-8-sig" handles BOM + CRLF.
    """
    rows: list[PropertyRow] = []
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            return rows
        col_map = {i: _HEADER_ALIASES.get(h.strip().lower(), h.strip().lower())
                   for i, h in enumerate(header)}
        for raw in reader:
            if not raw:
                continue
            row: dict[str, str] = {}
            for i, value in enumerate(raw):
                key = col_map.get(i)
                if key:
                    row[key] = (value or "").strip()
            raw_type = row.get("type", "").lower()
            type_norm = "LEASE_UP" if raw_type in ("lease-up", "lease up", "lease_up", "leaseup") else "STABILISED"
            pid = row.get("property_id", "")
            url = row.get("url", "")
            if not pid or not url:
                continue
            rows.append(
                PropertyRow(
                    property_id=pid,
                    url=url,
                    type=type_norm,
                    pms_platform=(row.get("pms_platform", "").lower() or None),
                    zip=row.get("zip") or None,
                    state=row.get("state") or None,
                    name=row.get("name") or None,
                )
            )
    return rows


def _local_tz_for(row: PropertyRow) -> Any:
    """Best-effort: derive local TZ from zip→lat/lng→tz. Falls back to America/New_York."""
    if _TZF is None or ZoneInfo is None:
        return ZoneInfo("America/New_York") if ZoneInfo is not None else None
    # We do not have lat/lng — fall back to state-based approximation.
    state_tz = {
        "NY": "America/New_York", "NJ": "America/New_York", "PA": "America/New_York",
        "MA": "America/New_York", "CT": "America/New_York", "FL": "America/New_York",
        "IL": "America/Chicago", "TX": "America/Chicago", "CO": "America/Denver",
        "CA": "America/Los_Angeles", "WA": "America/Los_Angeles", "OR": "America/Los_Angeles",
        "AZ": "America/Phoenix",
    }
    tz_name = state_tz.get((row.state or "").upper(), "America/New_York")
    return ZoneInfo(tz_name)


def is_due_now(row: PropertyRow, now_utc: datetime | None = None) -> bool:
    """
    Per-row schedule check. STABILISED → due once per local day after 02:00 local.
    LEASE_UP → due if current local time is within 08:00–21:00 and at a 4-hour slot
    boundary (window granularity = 1 hour for forgiveness).
    """
    now_utc = now_utc or datetime.now(UTC)
    tz = _local_tz_for(row)
    if tz is None:
        local = now_utc
    else:
        local = now_utc.astimezone(tz)
    if row.type == "STABILISED":
        return local.time() >= time(2, 0)
    # LEASE_UP: 08, 12, 16, 20 within window 08–21 local
    if not (time(8, 0) <= local.time() <= time(21, 0)):
        return False
    return local.hour in (8, 12, 16, 20)


class ScrapeFleet:
    """
    Top-level coordinator. Owns: BrowserFleet, EventLog, ChangeDetector,
    extraction pipeline. Enforces concurrency cap via batch sizing.
    """

    def __init__(
        self,
        properties: list[PropertyRow],
        data_dir: Path | None = None,
        max_concurrent: int | None = None,
        api_catalogue: dict[str, Any] | None = None,
        headless: bool = True,
    ) -> None:
        self.properties = properties
        self.data_dir = Path(data_dir if data_dir is not None else os.getenv("DATA_DIR", "./data"))
        self.max_concurrent = int(max_concurrent if max_concurrent is not None else os.getenv("MAX_CONCURRENT_BROWSERS", "10"))
        self.api_catalogue = api_catalogue or {}
        self.proxy_manager = ProxyManager()
        self.browser_fleet = BrowserFleet(
            proxy_manager=self.proxy_manager, data_dir=self.data_dir, headless=headless
        )
        self.event_log = EventLog(self.data_dir / "scrape_events.jsonl")
        self.state_store = StateStore(self.data_dir / "change_detection_state.json")

    async def run_once(self, only_due: bool = False) -> list[ScrapeEvent]:
        await self.browser_fleet.start()
        try:
            due = [p for p in self.properties if (not only_due) or is_due_now(p)]
            total = len(due)
            print(f"[fleet] {total} properties to scrape (concurrency={self.max_concurrent})")
            events: list[ScrapeEvent] = []
            done_count = 0

            # Process in batches equal to concurrency cap.
            # This avoids creating hundreds of coroutines that all fight for
            # the semaphore and pile up Playwright futures.
            for batch_start in range(0, total, self.max_concurrent):
                batch = due[batch_start : batch_start + self.max_concurrent]
                tasks = [self._scrape_one(p) for p in batch]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for i, r in enumerate(results):
                    done_count += 1
                    if isinstance(r, BaseException):
                        evt = ScrapeEvent(
                            event_id=str(uuid.uuid4()),
                            property_id=batch[i].property_id,
                            scrape_timestamp=datetime.now(UTC),
                            scrape_outcome=ScrapeOutcome.FAILED,
                            failure_reason=f"task_crashed: {r}",
                        )
                        await self.event_log.append(evt)
                        events.append(evt)
                        print(f"  [{done_count}/{total}] {batch[i].property_id} CRASHED: {r}")
                    else:
                        events.append(r)
                        print(f"  [{done_count}/{total}] {r.property_id} → {r.scrape_outcome}"
                              f" (tier={r.extraction_tier}, conf={r.confidence_score})")
                # Let Playwright drain between batches
                await asyncio.sleep(1.0)

            return events
        finally:
            await asyncio.sleep(0.5)
            await self.browser_fleet.stop()

    async def _scrape_one(self, row: PropertyRow) -> ScrapeEvent:
        per_property_timeout = int(os.getenv("PER_PROPERTY_TIMEOUT_S", "120"))
        try:
            return await asyncio.wait_for(
                self._scrape_one_inner(row), timeout=per_property_timeout
            )
        except TimeoutError:
            print(f"  [timeout] {row.property_id} exceeded {per_property_timeout}s")
            evt = ScrapeEvent(
                event_id=str(uuid.uuid4()),
                property_id=row.property_id,
                scrape_timestamp=datetime.now(UTC),
                scrape_outcome=ScrapeOutcome.FAILED,
                failure_reason=f"per_property_timeout ({per_property_timeout}s)",
            )
            await self.event_log.append(evt)
            return evt

    async def _scrape_one_inner(self, row: PropertyRow) -> ScrapeEvent:
        event_id = str(uuid.uuid4())
        scrape_ts = datetime.now(UTC)
        cd_result: ChangeDetectionResult = ChangeDetectionResult.INCONCLUSIVE
        outcome = ScrapeOutcome.FAILED
        tier: int | None = None
        confidence: float | None = None
        failure_reason: str | None = None
        page_load_ms: int | None = None
        raw_html_path: str | None = None
        screenshot_path: str | None = None
        banner_attempted = False
        banner_found = False
        vision_used = False
        sample_selected = False

        try:
            async with ChangeDetector(self.state_store, self.api_catalogue) as cd:
                decision, state = await cd.evaluate(row.property_id, row.url)
                cd_result = decision.overall
                if decision.skip:
                    await cd.record_skip(row.property_id, state)
                    outcome = ScrapeOutcome.SKIPPED
                    evt = ScrapeEvent(
                        event_id=event_id,
                        property_id=row.property_id,
                        scrape_timestamp=scrape_ts,
                        extraction_tier=None,
                        change_detection_result=cd_result,
                        scrape_outcome=outcome,
                        failure_reason=None,
                        page_load_ms=None,
                        proxy_used=False,
                        proxy_provider=self.proxy_manager.creds.provider if self.proxy_manager.creds else None,
                        vision_fallback_used=False,
                        banner_capture_attempted=False,
                        banner_concession_found=False,
                        accuracy_sample_selected=False,
                        confidence_score=None,
                    )
                    await self.event_log.append(evt)
                    await self._write_carryforward_output(row.property_id, scrape_ts.date(), state.carryforward_days)
                    return evt

            # Full scrape path
            session = await self.browser_fleet.scrape(
                row.property_id, row.url, pms_platform=row.pms_platform
            )
            page_load_ms = session.page_load_ms
            raw_html_path = str(session.raw_html_path) if session.raw_html_path else None
            screenshot_path = str(session.screenshot_path) if session.screenshot_path else None
            if session.failure_reason and not session.html:
                failure_reason = session.failure_reason
                outcome = ScrapeOutcome.FAILED
            else:
                result: ExtractionResult = await run_extraction_pipeline(
                    session, api_catalogue=self.api_catalogue
                )
                # Vision Tier 5 fallback if confidence < 0.6
                if not result.succeeded and (result.confidence_score < 0.6):
                    vision_result = await maybe_run_vision_fallback(session)
                    if vision_result is not None:
                        vision_used = True
                        if vision_result.confidence_score > result.confidence_score:
                            result = vision_result

                tier = int(result.tier) if result.tier is not None else None
                confidence = result.confidence_score
                outcome = ScrapeOutcome.SUCCESS if result.succeeded else ScrapeOutcome.PARTIAL

                # Banner capture: every non-skipped scrape, regardless of tier outcome.
                banner_attempted = True
                banner = await capture_banner(session)
                banner_found = banner is not None

                # Role C: deterministic accuracy sample 5–10%
                if outcome == ScrapeOutcome.SUCCESS:
                    if select_for_sample(row.property_id, scrape_ts.date()):
                        sample_selected = True
                        await write_sample_comparison(
                            self.data_dir / "extraction_output",
                            row.property_id,
                            scrape_ts.date(),
                            primary=result,
                            screenshot_path=session.screenshot_path,
                        )

                # Persist extraction output
                await write_extraction_output(
                    self.data_dir / "extraction_output",
                    row.property_id,
                    scrape_ts.date(),
                    payload={
                        "property_id": row.property_id,
                        "scrape_date": scrape_ts.date().isoformat(),
                        "extraction_tier": tier,
                        "confidence_score": confidence,
                        "units": result.raw_fields.get("units", []),
                        "banner_concession": banner,
                    },
                )

                # Record change-detection state on success
                async with ChangeDetector(self.state_store, self.api_catalogue) as cd2:
                    state2 = await self.state_store.get(row.property_id)
                    await cd2.record_full_scrape(row.property_id, state2)

        except Exception as exc:
            failure_reason = f"unhandled: {exc}"
            outcome = ScrapeOutcome.FAILED

        evt = ScrapeEvent(
            event_id=event_id,
            property_id=row.property_id,
            scrape_timestamp=scrape_ts,
            extraction_tier=tier,
            change_detection_result=cd_result,
            scrape_outcome=outcome,
            failure_reason=failure_reason,
            page_load_ms=page_load_ms,
            proxy_used=self.proxy_manager.should_use_proxy(row.url),
            proxy_provider=self.proxy_manager.creds.provider if self.proxy_manager.creds else None,
            vision_fallback_used=vision_used,
            banner_capture_attempted=banner_attempted,
            banner_concession_found=banner_found,
            accuracy_sample_selected=sample_selected,
            raw_html_path=raw_html_path,
            screenshot_path=screenshot_path,
            confidence_score=confidence,
        )
        await self.event_log.append(evt)
        return evt

    async def _write_carryforward_output(
        self, property_id: str, scrape_date: date, carryforward_days: int
    ) -> None:
        await write_extraction_output(
            self.data_dir / "extraction_output",
            property_id,
            scrape_date,
            payload={
                "property_id": property_id,
                "scrape_date": scrape_date.isoformat(),
                "extraction_tier": None,
                "confidence_score": None,
                "units": [],
                "banner_concession": None,
                "carryforward_days": carryforward_days,
                "skipped": True,
            },
        )


__all__ = ["PropertyRow", "ScrapeFleet", "load_properties", "is_due_now"]
