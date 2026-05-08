"""
scripts/smoke_test.py — Jugnu import-sanity smoke test (offline, no network).

Checks:
  1. L1 Fetch layer imports cleanly
  2. L2 Discovery layer imports cleanly
  3. L3 PMS adapter layer imports cleanly
  4. L4 Validation layer imports cleanly
  5. L5 Observability layer imports cleanly

Exits 0 if all 5 pass, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

CHECKS = [
    ("L1 Fetch",        "ma_poc.fetch.contracts",             "FetchTier"),
    ("L2 Discovery",    "ma_poc.discovery.contracts",         "CrawlTask"),
    ("L3 PMS",          "ma_poc.pms.contracts",               "ExtractResult"),
    ("L4 Validation",   "ma_poc.validation.contracts",        "ValidatedRecords"),
    ("L5 Observability","ma_poc.observability.events",        "emit"),
]


def main() -> int:
    passed = 0
    for label, module, attr in CHECKS:
        try:
            import importlib
            mod = importlib.import_module(module)
            if not hasattr(mod, attr):
                raise AttributeError(f"{module} has no attribute {attr!r}")
            print(f"PASS  {label} ({module}.{attr})")
            passed += 1
        except Exception as exc:
            print(f"FAIL  {label}: {exc}")

    print(f"\nSMOKE TEST: {passed}/{len(CHECKS)} PASSED")
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    sys.exit(main())
