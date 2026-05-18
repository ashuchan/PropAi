"""Vendored from origin/main @47feb17 (2026-05-18): main's prod-validated
generic DOM/JSON-LD extraction engine. Isolated subpackage — intra-imports
rewritten to stay in-package so it does NOT touch our branch's
_html_extract/_api_parser/_daily_runner_parsers (no clobber, no merge).
External dep: ma_poc.observability.events (shared, stable). Entry point:
extract_units_from_dom(html, base_url, hints=None) -> (units, mode).
"""
