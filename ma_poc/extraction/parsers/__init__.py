"""extraction.parsers — host-specific unit parsers behind UnitParser protocol.

Concrete parsers
----------------
GenericApiParser   wraps entrata.parse_api_responses()
SightMapParser     wraps entrata._parse_sightmap_payload()
RealPageParser     wraps scrape_properties._realpage_units_from_body()

All parsers implement the UnitParser protocol:
    parse(body: Any, url: str) -> list[dict]

and never raise — they return [] on any error or empty input.
"""
from __future__ import annotations

from extraction.parsers.generic import GenericApiParser
from extraction.parsers.protocol import UnitParser
from extraction.parsers.realpage import RealPageParser
from extraction.parsers.sightmap import SightMapParser

__all__ = [
    "UnitParser",
    "GenericApiParser",
    "SightMapParser",
    "RealPageParser",
]
