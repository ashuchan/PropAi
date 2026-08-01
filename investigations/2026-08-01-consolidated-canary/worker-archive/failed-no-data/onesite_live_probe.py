from __future__ import annotations

import asyncio
import gzip
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from ma_poc.pms.adapters.onesite import _probe_onesite_workflowstartup


ROOT = Path("/private/tmp/propai-fnd-vBkmT9")


async def main(property_id: str) -> None:
    records = {
        str(row["property_id"]): row
        for row in json.loads((ROOT / "failed344.json").read_text())
    }
    record = records[property_id]
    body = gzip.open(ROOT / "raw_all" / f"{property_id}.html.gz", "rb").read()
    context = SimpleNamespace(
        base_url=record.get("website") or "",
        property_id=property_id,
        fetch_result=SimpleNamespace(
            body=body,
            final_url=record.get("website") or "",
        ),
    )
    rows = await _probe_onesite_workflowstartup(context)
    unit_rows = sum(bool(row.get("unit_number")) for row in rows)
    print(f"{property_id}\t{len(rows)}\t{unit_rows}", flush=True)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
