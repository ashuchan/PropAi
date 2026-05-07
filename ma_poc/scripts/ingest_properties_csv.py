"""One-shot CSV → `properties` table ingest.

Bootstrapping path: run this once after standing up the DB to seed the
property catalog from `config/properties.csv`. After this, `jugnu_runner`
reads the catalog from the DB and the CSV becomes optional. Re-runs are
idempotent — existing rows are updated in place; new rows are inserted.

Usage:
    python -m scripts.ingest_properties_csv
    python -m scripts.ingest_properties_csv --csv path/to/properties.csv
    python -m scripts.ingest_properties_csv --dry-run

Design notes:
  - Reads via `CsvPropertyCatalogSource` so the column-name normalisation
    is shared with the runtime catalog path.
  - Writes directly via SQLAlchemy `dialect_insert().on_conflict_do_update`
    rather than going through `IPropertyStateStore.upsert` — that store
    re-stamps `last_seen_at` to "now", which would falsely claim a scrape
    happened. Ingest is purely catalog seeding; scrape state is left for
    the runner to populate.
  - Sets `first_seen_date = today` only for *new* rows. Existing rows keep
    whatever first_seen_date they had — that's the natural meaning of
    "first time we saw this property", not "first time we ingested it".
  - Stores anything beyond the canonical V2 columns under `extra` JSON.
    `property_type` (Stabilized / LEASE_UP) lives there because the
    `properties` table doesn't have a column for it.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

# `scripts.ingest_properties_csv` may be invoked from either the
# repo root (`python -m scripts.ingest_properties_csv`) or from inside
# `ma_poc/` directly. Make sure ma_poc/ is on sys.path either way.
_MA_POC_ROOT = Path(__file__).resolve().parent.parent
if str(_MA_POC_ROOT) not in sys.path:
    sys.path.insert(0, str(_MA_POC_ROOT))

from sqlalchemy import select  # noqa: E402

from data_provider.dtos import PropertyToScrape  # noqa: E402
from data_provider.factory import get_data_provider  # noqa: E402
from data_provider.filesystem import CsvPropertyCatalogSource  # noqa: E402
from data_provider.sql.engine import dialect_insert  # noqa: E402
from data_provider.sql.models import PropertyRow  # noqa: E402
from data_provider.sql.provider import SqlDataProvider  # noqa: E402

log = logging.getLogger(__name__)


_KNOWN_COLS = {
    "canonical_id",
    "apartment_id",
    "proj_name",
    "address",
    "city",
    "state",
    "zip_code",
    "country",
    "phone",
    "email_address",
    "website",
    "pmc",
    "website_design",
    "concessions",
    "first_seen_date",
    "last_seen_at",
    "last_scrape_status",
    "last_units_count",
}


def _build_row_payload(prop: PropertyToScrape, today: str) -> dict[str, Any]:
    """Project a PropertyToScrape DTO onto the `properties` table columns.

    Returns the column-keyed dict ready for `dialect_insert(...).values(**)`,
    minus `first_seen_date` which the upsert path sets only for new rows.
    """
    # Drop V2 fields we model explicitly so they don't double-count in `extra`.
    consumed = {
        "property_id",
        "url",
        "name",
        "Name",
        "Property Name",
        "proj_name",
        "address",
        "Address",
        "Property Address",
        "city",
        "City",
        "state",
        "State",
        "zip",
        "Zip",
        "ZIP Code",
        "zip_code",
        "phone",
        "Phone",
        "email",
        "Email",
        "email_address",
        "website",
        "Website",
        "pmc",
        "Management Company",
        "apartmentid",
        "apartment_id",
        "Unique ID",
    }
    extra: dict[str, Any] = {k: v for k, v in (prop.raw or {}).items() if k not in consumed}
    if prop.property_type is not None:
        extra["property_type"] = prop.property_type
    return {
        "canonical_id": prop.canonical_id,
        "apartment_id": prop.apartment_id,
        "proj_name": prop.proj_name,
        "address": prop.address,
        "city": prop.city,
        "state": prop.state,
        "zip_code": prop.zip_code,
        "country": prop.country,
        "phone": prop.phone,
        "email_address": prop.email_address,
        "website": prop.website or prop.url or None,
        "pmc": prop.pmc,
        "extra": extra,
    }


def ingest(
    *,
    csv_path: Path | None = None,
    provider: SqlDataProvider | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    """Read CSV → upsert into `properties`. Returns {inserted, updated, total}.

    Args:
        csv_path: Override CSV location. Defaults to the FS provider's
            standard `config/properties.csv` path.
        provider: SQL provider to write into. If None, calls
            `get_data_provider()` and asserts SqlDataProvider.
        dry_run: When True, count what would change but don't write.
    """
    # Resolve the SQL provider — ingest only makes sense against a SQL backend.
    candidate: SqlDataProvider | None = provider
    if candidate is None:
        resolved = get_data_provider()
        if not isinstance(resolved, SqlDataProvider):
            raise RuntimeError(
                f"ingest requires a SQL DataProvider but got {resolved.name}. "
                "Set DATA_PROVIDER=postgres (or sqlite) and DATABASE_URL."
            )
        candidate = resolved
    elif not isinstance(candidate, SqlDataProvider):
        raise RuntimeError(
            f"ingest requires a SQL DataProvider but got {candidate.name}. "
            "Set DATA_PROVIDER=postgres (or sqlite) and DATABASE_URL."
        )
    provider = candidate

    # Resolve the CSV path. Prefer explicit arg, else use the runtime
    # provider's catalog if it's CSV-backed (rare during ingest), else
    # default to the same conventional location FS provider uses.
    if csv_path is None:
        ma_poc_root = Path(__file__).resolve().parent.parent
        csv_path = ma_poc_root / "config" / "properties.csv"

    source = CsvPropertyCatalogSource(csv_path)
    rows = source.list_active()
    log.info("Read %d rows from %s", len(rows), csv_path)

    counts = {"inserted": 0, "updated": 0, "total": len(rows)}
    today = date.today().isoformat()

    if dry_run:
        # Match-without-write: bucket rows by canonical_id presence.
        existing = provider.property_state.all_canonical_ids()
        for prop in rows:
            if prop.canonical_id in existing:
                counts["updated"] += 1
            else:
                counts["inserted"] += 1
        return counts

    with provider.transaction():
        with provider._holder.scope() as session:
            existing_ids = set(
                session.execute(select(PropertyRow.canonical_id)).scalars().all()
            )
            for prop in rows:
                values = _build_row_payload(prop, today)
                is_new = prop.canonical_id not in existing_ids
                if is_new:
                    values["first_seen_date"] = today

                stmt = dialect_insert(provider.engine, PropertyRow).values(**values)
                # On conflict update everything except canonical_id and the
                # state-tracking columns the runner owns.
                update_cols = {
                    k: stmt.excluded[k]
                    for k in values
                    if k
                    not in {
                        "canonical_id",
                        "first_seen_date",
                        "last_seen_at",
                        "last_scrape_status",
                        "last_units_count",
                    }
                }
                stmt = stmt.on_conflict_do_update(
                    index_elements=[PropertyRow.canonical_id],
                    set_=update_cols,
                )
                session.execute(stmt)
                if is_new:
                    counts["inserted"] += 1
                else:
                    counts["updated"] += 1

    return counts


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Seed the properties table from CSV")
    parser.add_argument(
        "--csv",
        type=Path,
        default=None,
        help="Path to properties CSV (default: ma_poc/config/properties.csv)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report counts without writing to the DB",
    )
    args = parser.parse_args()

    counts = ingest(csv_path=args.csv, dry_run=args.dry_run)
    label = "DRY-RUN " if args.dry_run else ""
    print(
        f"{label}ingest complete: inserted={counts['inserted']} "
        f"updated={counts['updated']} total={counts['total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
