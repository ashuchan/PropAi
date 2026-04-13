# MA Rent Intelligence Platform — Phase A POC

Implements PR-01 through PR-04 of the BRD v2.0 (Apr 2, 2026).
500-property multifamily rent intelligence pipeline with 5-tier extraction
(API → JSON-LD → DOM templates → LLM → Vision LLM) and a change-detection
gate that skips unchanged properties.

## Setup

```bash
cd ma_poc
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
playwright install chromium
cp .env.example .env     # then fill in keys
```

> **Python version note:** CLAUDE.md originally pinned versions for ~Python 3.11/3.12.
> This install bumps every dep to the current 3.14-compatible release while keeping
> exact pins. Functionally equivalent; APIs unchanged.

## Inputs

- `config/properties.csv` — 500 properties (provided by RealPage). Loader is
  tolerant of header casing and accepts the RealPage-formatted CSV directly:
  `Property ID`, `Property URL`, `Property Type` (Stabilized/Lease-Up),
  `PMS Platform`. It normalizes to internal `property_id` / `url` /
  `type` (STABILISED|LEASE_UP) / `pms_platform`.
- `config/business_rules.yaml` — Phase B fills these in; defaults shipped.
- `config/api_catalogue.json` — built by `scripts/build_api_catalogue.py` Week 1.

## Run

```bash
python -m scripts.build_api_catalogue        # Week 1: discover API patterns on 50-property seed
python -m scripts.run_phase_a                 # full daily pass over all properties
python -m scripts.validate_outputs            # post-run gate metrics
python -m scripts.smoke_test                  # 5-property end-to-end
```

## Tests

```bash
pytest . -v --tb=short --ignore=data --ignore=config
ruff check .
mypy . --strict
```

## Layout

See [CLAUDE.md](../claude.md) for the authoritative spec, repository structure,
weekly gates, bug-hunt checklist, and acceptance criteria.
