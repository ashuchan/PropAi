"""Integration tests for the property-level concession capture in scraper.py.

Covers:

  * ``_capture_concession_from_html``: script/style/noscript body
    strip, sentence-extend for banner-header rows, 300-char cap,
    no-match return None.
  * Source-grep pins: the script-strip and sentence-extend code
    paths are present (catches refactor regression).
"""

from __future__ import annotations

from pathlib import Path

# ─────────────────────────────────────────────────────────────────────
# _capture_concession_from_html — text capture pipeline
# ─────────────────────────────────────────────────────────────────────


class TestCaptureFromHtml:
    def test_clean_html_capture(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = "<html><body><h1>Welcome</h1><div class='banner'>Get 2 months free rent today!</div></body></html>"
        result = _capture_concession_from_html(html)
        assert result is not None
        assert "2 months free rent" in result

    def test_script_body_does_not_leak_into_capture(self) -> None:
        """Script body is stripped BEFORE the tag-strip flatten — the
        canary's #1 bug. Capture window must not contain JS code."""
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = (
            "<html><body>"
            "<script>"
            "if (href.indexOf('?') == -1) { el.setAttribute('href', 'x'); } "
            "var PropLeadSource = function() { return 'foo'; };"
            "</script>"
            "<div class='banner'>2 months free rent on a 13-month lease!</div>"
            "</body></html>"
        )
        result = _capture_concession_from_html(html)
        assert result is not None
        assert "2 months free rent" in result
        # The JS body must not appear in the captured snippet.
        assert "indexOf" not in result
        assert "PropLeadSource" not in result
        assert "setAttribute" not in result

    def test_style_body_does_not_leak_into_capture(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = (
            "<html><body>"
            "<style>"
            ".banner { padding: 12px; background-color: #fff; !important; }"
            "</style>"
            "<div class='banner'>Save $500 today!</div>"
            "</body></html>"
        )
        result = _capture_concession_from_html(html)
        assert result is not None
        assert "$500" in result
        assert "padding" not in result
        assert "background-color" not in result

    def test_noscript_body_stripped(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = (
            "<html><body>"
            "<noscript>JavaScript is required for this experience.</noscript>"
            "<div>Limited Time Offer! Get 1 month free rent on signing!</div>"
            "</body></html>"
        )
        result = _capture_concession_from_html(html)
        assert result is not None
        assert "JavaScript" not in result

    def test_header_only_extended_forward_to_body(self) -> None:
        """Sentence-extend: header + body sentence must merge under 300
        chars when the body lives in the next sentence."""
        from ma_poc.pms.scraper import _capture_concession_from_html

        # Banner header in one sentence, body in the next — without the
        # sentence-extend fix, single-sentence pick drops the body.
        html = (
            "<html><body>"
            "<div>Limited Time Offer! Move in by 6/15 and get 1 month free rent today.</div>"
            "</body></html>"
        )
        result = _capture_concession_from_html(html)
        assert result is not None
        # The body sentence must be present alongside the header.
        assert "1 month free rent" in result
        assert "Limited Time Offer" in result

    def test_300_char_cap_respected(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        # Massive run-on text — capture must cap.
        long_text = "Get 2 months free rent! " + ("Restrictions apply. " * 50)
        html = f"<html><body><div>{long_text}</div></body></html>"
        result = _capture_concession_from_html(html)
        assert result is not None
        assert len(result) <= 300

    def test_no_match_returns_none(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        html = "<html><body>Welcome to our beautiful community.</body></html>"
        assert _capture_concession_from_html(html) is None

    def test_empty_or_invalid_input(self) -> None:
        from ma_poc.pms.scraper import _capture_concession_from_html

        assert _capture_concession_from_html("") is None
        assert _capture_concession_from_html(None) is None  # type: ignore[arg-type]
        assert _capture_concession_from_html(123) is None  # type: ignore[arg-type]

# ─────────────────────────────────────────────────────────────────────
# Source-grep pins — catch refactor regression
# ─────────────────────────────────────────────────────────────────────


def test_script_strip_pattern_present_in_scraper() -> None:
    """The script/style/noscript body-strip regex must remain in
    scraper.py. If a future refactor removes it, the JS/CSS leak
    returns and ~50% of canary captures get polluted again."""
    src = Path(__file__).resolve().parents[2] / "pms" / "scraper.py"
    text = src.read_text(encoding="utf-8")
    assert "<(script|style|noscript)" in text


def test_sentence_extend_pattern_present_in_scraper() -> None:
    """The forward sentence-extend (parts[idx + 1: idx + 3]) must
    remain so banner-header rows pick up the body sentence."""
    src = Path(__file__).resolve().parents[2] / "pms" / "scraper.py"
    text = src.read_text(encoding="utf-8")
    assert "parts[idx + 1: idx + 3]" in text or "parts[idx + 1:idx + 3]" in text
