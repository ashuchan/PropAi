# PropAi

Multifamily rent intelligence platform. Scrapes 500+ property websites daily, extracts unit-level rent and availability data, and produces structured output for analytics.

## Projects

### [ma_poc/](ma_poc/) — Scraping Pipeline

The core scraping engine with two pipeline implementations:

- **Jugnu Pipeline** (`jugnu_runner.py`) — 5-layer architecture: Fetch, Discovery, Extraction, Validation, Observability. 10 PMS-specific adapters, SQLite-backed state, SLO monitoring. *Recommended.*
- **Legacy Pipeline** (`daily_runner.py`) — 7-phase extraction with self-learning per-property profiles via `entrata.py`.

```bash
cd ma_poc
python scripts/jugnu_runner.py --csv config/properties.csv --limit 5
```

### [ma_poc/frontend/](ma_poc/frontend/) — Web UI

React frontend for viewing scrape results, property reports, and run summaries.

### [ma_poc/services/](ma_poc/services/) — API Services

Node.js backend services for data access and property management.

## Quick Start

```bash
cd ma_poc
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
playwright install chromium
cp .env.example .env     # fill in API keys

# Run scraper (5 properties)
python scripts/jugnu_runner.py --csv config/properties.csv --limit 5

# Run tests
pytest . -v --tb=short --ignore=data --ignore=config

# Check all gates
python scripts/gate_jugnu.py all
```

See [ma_poc/README.md](ma_poc/README.md) for full architecture documentation.
