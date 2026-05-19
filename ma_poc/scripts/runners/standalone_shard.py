"""standalone_shard.py — Cloud Run task entry for the standalone detail extractor.

Lightweight sibling of shard_entry.py. Does NOT invoke the jugnu runner —
it runs ma_poc.standalone.detail_unit_extractor.run() over this task's slice
of a URL list and uploads per-shard results to GCS. Zero pipeline coupling
(no DB, no PG sync) — this is the all-456 detail-page crawl validation.

Env consumed:
  CLOUD_RUN_TASK_INDEX  (auto) — this task's index
  CLOUD_RUN_TASK_COUNT  (auto) — total tasks
  URLS_GCS_URI          (required) — gs:// URI of newline-delimited URL list
  BUCKET_NAME           (required) — output bucket
  RUN_DATE              (optional) — YYYY-MM-DD; defaults UTC today
  CONCURRENCY           (optional) — per-shard browser concurrency (default 6)
  RESULT_PREFIX         (optional) — run-dir name (default standalone456)
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
from datetime import date
from pathlib import Path

_script_dir = Path(__file__).resolve().parent
_ma_poc_root = _script_dir.parent.parent
_app_root = _ma_poc_root.parent
for _p in (_app_root, _ma_poc_root):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from ma_poc.standalone.detail_unit_extractor import run  # noqa: E402
from ma_poc.storage import gcs  # noqa: E402


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"Required env {name!r} not set")
    return v


def main() -> None:
    idx = int(os.environ.get("CLOUD_RUN_TASK_INDEX", "0"))
    cnt = int(os.environ.get("CLOUD_RUN_TASK_COUNT", "1"))
    urls_uri = _require("URLS_GCS_URI")
    bucket = _require("BUCKET_NAME")
    run_date = os.environ.get("RUN_DATE") or date.today().isoformat()
    conc = int(os.environ.get("CONCURRENCY", "6"))
    prefix = os.environ.get("RESULT_PREFIX", "standalone456")

    local = Path("/tmp/urls_all.txt")
    gcs.download_object(urls_uri, local)
    all_urls = [u.strip() for u in local.read_text().split() if u.strip()]

    size = math.ceil(len(all_urls) / cnt)
    shard = all_urls[idx * size : min((idx + 1) * size, len(all_urls))]
    print(
        f"[standalone_shard] task {idx}/{cnt} — {len(shard)} of "
        f"{len(all_urls)} urls, conc={conc}",
        file=sys.stderr,
    )
    if not shard:
        print("[standalone_shard] empty shard — exit 0", file=sys.stderr)
        sys.exit(0)

    results = asyncio.run(run(shard, concurrency=conc))

    out = Path(f"/tmp/shard_{idx}_results.jsonl")
    with out.open("w") as f:
        for r in results:
            f.write(
                json.dumps(
                    {
                        "url": r.url,
                        "klass": r.klass,
                        "phase": r.phase,
                        "n": r.n_units,
                        "units": r.units,
                        "sig": r.signal,
                        "err": r.error,
                    }
                )
                + "\n"
            )

    from collections import Counter

    summary = dict(Counter(r.klass for r in results))
    summary["_shard"] = idx
    summary["_n_props"] = len(results)
    sp = Path(f"/tmp/shard_{idx}_summary.json")
    sp.write_text(json.dumps(summary))

    dest = f"gs://{bucket}/runs/{run_date}-{prefix}/shard_{idx}/"
    gcs.upload_object(out, dest + "results.jsonl")
    gcs.upload_object(sp, dest + "summary.json")
    print(f"[standalone_shard] uploaded → {dest} :: {summary}", file=sys.stderr)
    sys.exit(0)


if __name__ == "__main__":
    main()
