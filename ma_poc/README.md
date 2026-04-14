# MA Rent Intelligence Platform — Phase A POC

Multifamily rent intelligence pipeline that scrapes 500+ property websites daily,
extracts unit-level rent and availability data through a 7-phase extraction pipeline
with self-learning per-property profiles.

## Setup

```bash
cd ma_poc
python -m venv .venv
.venv\Scripts\activate   # Windows
pip install -r requirements.txt
playwright install chromium
cp .env.example .env     # then fill in keys
```

## Architecture

The production pipeline uses a **7-phase extraction approach** that is exploratory
and self-learning:

```
daily_runner.py → entrata.py (7-phase pipeline) → profile learning
                                                 → state tracking
                                                 → 46-key output
```

**Phases:**
1. Homepage load + full network capture
2. Noise filtering (global + per-property profile blocklists)
3. Known pattern extraction (profile mappings → API → JSON-LD → DOM)
4. Link-by-link exploration with per-page network observation
5. LLM-assisted API analysis (single API at a time, max 3 calls)
6. DOM fallback with targeted LLM → legacy LLM → Vision LLM
7. Availability defaults + profile learning persistence

### Self-Learning Profiles

Each property gets a profile at `config/profiles/{canonical_id}.json` that learns:
- **Which APIs work** — saved as `known_endpoints` and `llm_field_mappings`
- **Which APIs are noise** — saved as `blocked_endpoints` (chatbot, analytics, etc.)
- **Which pages have data** — saved as `winning_page_url` and `availability_links`
- **Which pages don't** — saved as `explored_links` (skipped on future runs)
- **CSS selectors** — saved as `field_selectors` for deterministic DOM extraction

Profile maturity (COLD → WARM → HOT) determines how much of the pipeline runs.
HOT profiles skip directly to the known-good extraction method.

## Run

```bash
# Full daily run (all properties)
python scripts/daily_runner.py --csv config/properties.csv

# Test with N properties
python scripts/daily_runner.py --csv config/properties.csv --limit 5

# Retry failed properties
python scripts/retry_runner.py --csv config/properties.csv

# Scrape a single property (debug)
python scripts/entrata.py --url https://property-website.com

# With proxy
python scripts/daily_runner.py --proxy http://user:pass@host:port
```

## Inputs

- `config/properties.csv` — property list with URL, ID, type, management company
- `config/profiles/` — per-property learned extraction profiles (auto-generated)
- `config/prompts/` — LLM prompt templates (api_analysis.txt, dom_analysis.txt, tier4_extraction.txt)
- `.env` — API keys (Azure OpenAI, Anthropic), proxy config

## Output

Production output in `data/runs/{YYYY-MM-DD}/`:
- `properties.json` — 46-key property records with nested units
- `report.json` / `report.md` — run summary
- `issues.jsonl` — validation issues

Persistent state in `data/state/`:
- `property_index.json` — tracks first_seen, last_seen, scrape status per property
- `unit_index.json` — unit history with daily diffs (new/updated/unchanged/disappeared)

## Tests

```bash
pytest . -v --tb=short --ignore=data --ignore=config
```

## Documentation

- [scripts/CLAUDE.md](scripts/CLAUDE.md) — production pipeline guide (7-phase extraction, profile system, failure modes)
- [claude-scrapper-arch.md](claude-scrapper-arch.md) — original architecture spec with implementation status
- [../CLAUDE.md](../CLAUDE.md) — BRD Phase A spec (reference, not production)
