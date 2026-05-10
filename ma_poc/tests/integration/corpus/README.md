# Integration Test Corpus

This directory contains realistic, versioned HTML and JSON response bodies
captured from live PMS platforms. Corpus files are the ground truth that
extract-layer integration tests run against.

## Directory structure

```
corpus/
├── rentcafe/
│   ├── listing.html          # Full property listing page
│   ├── api_units.json        # /api/ApartmentAvailability response
│   ├── sitemap.xml           # sitemap.xml fragment
│   └── SOURCE.md             # Capture metadata (date, URL pattern)
├── entrata/
│   ├── listing.html
│   ├── widget_response.json  # Entrata widget API response
│   └── SOURCE.md
├── appfolio/
│   ├── listing.html
│   └── SOURCE.md
├── onesite/
│   ├── listing.html
│   ├── api_units.json
│   └── SOURCE.md
└── sightmap/
    ├── listing.html
    ├── api_units.json
    └── SOURCE.md
```

## How to add a new corpus entry

1. **Capture the real response.** Use `curl -L -A "Mozilla/5.0 ..." <url>`
   or the existing debug scripts (`scripts/diagnostics/`) to save the raw
   HTML/JSON exactly as the server returned it.

2. **Create the PMS sub-directory** if it does not yet exist:
   ```
   mkdir -p ma_poc/tests/integration/corpus/<pms_name>/
   ```

3. **Save the response** with a descriptive filename:
   - `listing.html` — the property availability page
   - `api_units.json` — the API endpoint response (single call)
   - `sitemap.xml` — sitemap fragment (if used by the adapter)

4. **Write a `SOURCE.md`** in the same directory:
   ```markdown
   # Corpus source: <PMS name>

   Captured: YYYY-MM-DD
   URL pattern: https://<example>.rentcafe.com/apartments/<id>/
   Notes: Any quirks or sanitisation applied to the capture.
   ```

5. **Sanitise PII.** Replace real unit numbers, tenant names, phone numbers,
   and email addresses with obviously-fake placeholders. Keep the *structure*
   intact — the goal is a realistic shape, not real data.

6. **Load in tests** via the `corpus` pytest fixture:
   ```python
   def test_extract_rentcafe(corpus):
       html = corpus.load("rentcafe/listing.html")
       api  = corpus.load_json("rentcafe/api_units.json")
   ```

## Corpus update policy

- Update a corpus file **only** when a real scrape failure reveals a
  structural gap in the existing capture (new field, changed response shape).
- Do **not** update corpus files to make a failing test pass unless the
  production site's structure genuinely changed.
- Each corpus directory has a `SOURCE.md` that records the capture date.
  When you update a file, update the date in `SOURCE.md` too.
- Tests must not silently pass against ancient HTML. If the capture is more
  than 6 months old and the adapter is failing in production, recapture.
