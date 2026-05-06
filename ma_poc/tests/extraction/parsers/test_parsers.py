"""TDD tests for extraction/parsers/ — UnitParser protocol and concrete parsers.

Step 1 (RED): these tests fail until the parsers package is implemented.
"""
from __future__ import annotations

import sys
from typing import runtime_checkable, Protocol, Any

import pytest


# ---------------------------------------------------------------------------
# Protocol compliance helpers
# ---------------------------------------------------------------------------

@runtime_checkable
class _UnitParser(Protocol):
    def parse(self, body: Any, url: str) -> list[dict]: ...


# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------

class TestProtocolImport:
    """The UnitParser protocol must be importable from extraction.parsers."""

    def test_unitparser_importable(self):
        from extraction.parsers.protocol import UnitParser  # noqa: F401

    def test_unitparser_has_parse_method(self):
        from extraction.parsers.protocol import UnitParser
        # Protocol must define 'parse'
        assert hasattr(UnitParser, '__protocol_attrs__') or 'parse' in dir(UnitParser)


class TestAllParsersInRegistry:
    """All parsers must be importable via the package __init__."""

    def test_generic_parser_importable(self):
        from extraction.parsers import GenericApiParser  # noqa: F401

    def test_sightmap_parser_importable(self):
        from extraction.parsers import SightMapParser  # noqa: F401

    def test_realpage_parser_importable(self):
        from extraction.parsers import RealPageParser  # noqa: F401

    def test_all_three_in_all(self):
        import extraction.parsers as ep
        for name in ("GenericApiParser", "SightMapParser", "RealPageParser"):
            assert hasattr(ep, name), f"{name} missing from extraction.parsers"


# ---------------------------------------------------------------------------
# Protocol implementation tests
# ---------------------------------------------------------------------------

class TestImplementsProtocol:
    """Each concrete parser must satisfy the UnitParser protocol (duck-typed)."""

    def test_generic_parser_implements_protocol(self):
        from extraction.parsers.generic import GenericApiParser
        assert isinstance(GenericApiParser(), _UnitParser)

    def test_sightmap_parser_implements_protocol(self):
        from extraction.parsers.sightmap import SightMapParser
        assert isinstance(SightMapParser(), _UnitParser)

    def test_realpage_parser_implements_protocol(self):
        from extraction.parsers.realpage import RealPageParser
        assert isinstance(RealPageParser(), _UnitParser)


# ---------------------------------------------------------------------------
# Return-type tests
# ---------------------------------------------------------------------------

class TestParsersReturnList:
    """Each parser.parse() must return a list (even for empty/garbage input)."""

    def test_generic_parser_returns_list(self):
        from extraction.parsers.generic import GenericApiParser
        result = GenericApiParser().parse([], "https://example.com")
        assert isinstance(result, list)

    def test_generic_parser_returns_list_with_valid_response(self):
        from extraction.parsers.generic import GenericApiParser
        api_responses = [{"url": "https://sightmap.com/app/api/v1/123", "body": {}}]
        result = GenericApiParser().parse(api_responses, "https://example.com")
        assert isinstance(result, list)

    def test_sightmap_parser_returns_list(self):
        from extraction.parsers.sightmap import SightMapParser
        result = SightMapParser().parse({}, "https://sightmap.com/app/api/v1/123")
        assert isinstance(result, list)

    def test_sightmap_parser_returns_list_with_data(self):
        from extraction.parsers.sightmap import SightMapParser
        body = {
            "data": {
                "units": [
                    {"id": 1, "floor_plan_id": 1, "unit_number": "101", "price": 1500, "available_on": "2024-01-01"}
                ],
                "floor_plans": [
                    {"id": 1, "name": "1BR", "bedroom_count": 1, "bathroom_count": 1}
                ],
            }
        }
        result = SightMapParser().parse(body, "https://sightmap.com/app/api/v1/123")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["floor_plan_name"] == "1BR"

    def test_realpage_parser_returns_list(self):
        from extraction.parsers.realpage import RealPageParser
        result = RealPageParser().parse({}, "https://api.ws.realpage.com/units")
        assert isinstance(result, list)

    def test_realpage_parser_returns_list_with_data(self):
        from extraction.parsers.realpage import RealPageParser
        body = {
            "response": [
                {
                    "unitNumber": "101",
                    "rent": "1500",
                    "availableDate": "2024-01-01",
                }
            ]
        }
        result = RealPageParser().parse(body, "https://api.ws.realpage.com/units")
        assert isinstance(result, list)


# ---------------------------------------------------------------------------
# Never-raises tests
# ---------------------------------------------------------------------------

class TestParsersNeverRaise:
    """Parsers must never propagate exceptions — return [] on any bad input."""

    def test_generic_parser_never_raises_on_none(self):
        from extraction.parsers.generic import GenericApiParser
        result = GenericApiParser().parse(None, "https://example.com")
        assert result == []

    def test_generic_parser_never_raises_on_garbage(self):
        from extraction.parsers.generic import GenericApiParser
        result = GenericApiParser().parse("not a list at all!!!", "https://example.com")
        assert result == []

    def test_generic_parser_never_raises_on_dict(self):
        from extraction.parsers.generic import GenericApiParser
        result = GenericApiParser().parse({"garbage": True}, "https://example.com")
        assert result == []

    def test_sightmap_parser_never_raises_on_none(self):
        from extraction.parsers.sightmap import SightMapParser
        result = SightMapParser().parse(None, "https://sightmap.com")
        assert result == []

    def test_sightmap_parser_never_raises_on_garbage_string(self):
        from extraction.parsers.sightmap import SightMapParser
        result = SightMapParser().parse("not a dict", "https://sightmap.com")
        assert result == []

    def test_realpage_parser_never_raises_on_none(self):
        from extraction.parsers.realpage import RealPageParser
        result = RealPageParser().parse(None, "https://api.ws.realpage.com")
        assert result == []

    def test_realpage_parser_never_raises_on_garbage(self):
        from extraction.parsers.realpage import RealPageParser
        result = RealPageParser().parse(42, "https://api.ws.realpage.com")
        assert result == []
