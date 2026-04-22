# Service Implementations

## How to add a new implementation

1. Create folder `implementations/my-impl/`
2. Implement all 5 interfaces: `IPropertyService`, `IUnitService`, `IRunService`, `IDiffService`, `IHealthService`
3. Add a new case to `factory.ts`
4. Add any config fields to `ServiceConfig` if needed
5. Write tests in `tests/my-impl/`
6. Update this README

## Available implementations

### json-file (default)
Reads data from JSON files on disk produced by the Python scraping pipeline.
- `data/runs/{YYYY-MM-DD}/properties.json` — property + unit data
- `data/runs/{YYYY-MM-DD}/report*.json` — run reports
- `data/runs/{YYYY-MM-DD}/ledger.jsonl` — scrape ledger
- `data/runs/{YYYY-MM-DD}/issues.jsonl` — validation issues
- `data/state/property_index.json` — property tracking state
- `data/state/unit_index.json` — unit tracking state
