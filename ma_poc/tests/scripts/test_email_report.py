"""Tests for ``scripts.email.report`` — the markdown→HTML converter, the
URL-scheme allowlist, and the recipient CRLF guard.

These three surfaces are the highest-risk parts of the email refactor:

* ``_md_to_html`` is regex-heavy and gates the visual quality of every
  report email. A converter regression would silently corrupt mail bodies
  for all five report scripts at once.
* ``_safe_link_target`` is the XSS-prevention layer for any markdown body
  that ever embeds CSV-derived strings (property names, urls).
* ``_parse_recipients`` is the SMTP-header-injection guard. A regression
  there is a security bug, not a cosmetic one.

The tests deliberately avoid touching the live transport — no
``send_html_email`` call, no Gmail API, no MCP. They exercise the
unit-pure helpers only.
"""

from __future__ import annotations

import re

import pytest

from ma_poc.scripts.email.report import (
    _inline_md,
    _md_to_html,
    _safe_link_target,
    _text_to_html,
)
from ma_poc.scripts.email.daily import _parse_recipients


# ── _md_to_html ──────────────────────────────────────────────────────────────


class TestMarkdownToHtml:
    def test_atx_headings_become_h1_h2_h3(self) -> None:
        out = _md_to_html("# T1\n\n## T2\n\n### T3\n")
        assert "<h1>T1</h1>" in out
        assert "<h2>T2</h2>" in out
        assert "<h3>T3</h3>" in out

    def test_h4_and_deeper_clamp_to_h3(self) -> None:
        # h1/h2 are reserved for the shell's own header bars, so the
        # converter caps at h3 to keep the body subordinate.
        out = _md_to_html("#### Deep\n")
        assert "<h3>Deep</h3>" in out

    def test_pipe_table_renders_with_thead_tbody(self) -> None:
        md = (
            "| Date | Total | Rate |\n"
            "|------|------:|-----:|\n"
            "| 2026-05-09 | 500 | 94.6% |\n"
            "| 2026-05-08 | 500 | 96.2% |\n"
        )
        out = _md_to_html(md)
        assert "<table>" in out
        assert "<thead>" in out
        assert "<th>Date</th>" in out
        assert "<th>Total</th>" in out
        # tbody rows present
        assert "<tbody>" in out
        assert "<td" in out and "2026-05-09" in out

    def test_pipe_table_numeric_columns_get_num_class(self) -> None:
        md = (
            "| Item | Count |\n"
            "|------|------:|\n"
            "| Foo | 42 |\n"
        )
        out = _md_to_html(md)
        # Foo (text) should not get .num; 42 should.
        assert '<td>Foo</td>' in out
        assert '<td class="num">42</td>' in out

    def test_pipe_table_skipped_when_no_separator(self) -> None:
        # Without the ``|---|---|`` separator row, the leading pipe-line is
        # just text — should fall through to a paragraph, not an empty
        # ``<table>`` wrapper.
        md = "| not a table | really |\nplain prose follows\n"
        out = _md_to_html(md)
        assert "<table>" not in out

    def test_bullet_list(self) -> None:
        md = "- one\n- two\n- three\n"
        out = _md_to_html(md)
        assert out.count("<li>") == 3
        assert "<ul>" in out and "</ul>" in out

    def test_ordered_list(self) -> None:
        md = "1. first\n2. second\n3. third\n"
        out = _md_to_html(md)
        assert out.count("<li>") == 3
        assert "<ol>" in out and "</ol>" in out

    def test_fenced_code_block_preserved_verbatim(self) -> None:
        md = "```\nleading_spaces  matter\n  and    extra ones too\n```\n"
        out = _md_to_html(md)
        assert "<pre" in out and "<code>" in out
        assert "leading_spaces  matter" in out
        # Fenced code uses ``preserved`` class so white-space wraps but
        # internal spacing is kept (see _md_to_html docstring).
        assert 'class="preserved"' in out

    def test_fenced_code_escapes_html(self) -> None:
        md = "```\n<script>alert(1)</script>\n```\n"
        out = _md_to_html(md)
        assert "<script>alert(1)</script>" not in out
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out

    def test_inline_bold_italic_code(self) -> None:
        out = _md_to_html("**bold** and *italic* and `code`.\n")
        assert "<strong>bold</strong>" in out
        assert "<em>italic</em>" in out
        assert "<code>code</code>" in out

    def test_html_metacharacters_escaped_outside_code(self) -> None:
        # Make sure raw `<` / `>` in body text don't bleed through to the
        # rendered HTML — they would corrupt the email body and risk XSS
        # on any mail client that respects raw HTML.
        out = _md_to_html("Compare a < b and c > d.\n")
        assert "<p>Compare a &lt; b and c &gt; d.</p>" in out

    def test_blank_lines_separate_paragraphs(self) -> None:
        out = _md_to_html("para one.\n\npara two.\n")
        assert "<p>para one.</p>" in out
        assert "<p>para two.</p>" in out

    def test_unterminated_fence_flushes_anyway(self) -> None:
        # Robust against truncated reports — should never lose content.
        out = _md_to_html("```\nstuff\nmore stuff\n")
        assert "stuff" in out
        assert "more stuff" in out

    def test_multiline_paragraph_collapses_to_one(self) -> None:
        out = _md_to_html("line 1\nline 2\nline 3\n")
        assert "<p>line 1 line 2 line 3</p>" in out


# ── _safe_link_target ────────────────────────────────────────────────────────


class TestSafeLinkTarget:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/x",
            "http://example.com",
            "mailto:ops@example.com",
            "//cdn.example.com/asset",
            "/local/path",
            "./rel/path",
            "../up/path",
            "#section",
            "no-colon-no-slash",
        ],
    )
    def test_allowed_schemes_pass_through(self, url: str) -> None:
        assert _safe_link_target(url) == url

    @pytest.mark.parametrize(
        "url",
        [
            "javascript:alert(1)",
            "JaVaScRiPt:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "vbscript:msgbox(1)",
            "file:///etc/passwd",
            "ftp://example.com",
            "chrome://settings",
        ],
    )
    def test_blocked_schemes_return_none(self, url: str) -> None:
        assert _safe_link_target(url) is None

    def test_empty_returns_none(self) -> None:
        assert _safe_link_target("") is None
        assert _safe_link_target("   ") is None

    def test_unsafe_link_renders_as_plain_text(self) -> None:
        # Inline-md should leave the label visible but strip the href.
        out = _inline_md("[click](javascript:alert(1))")
        assert "<a " not in out
        assert "click" in out
        # The URL must still appear in the body so a careful reader can
        # see what was attempted — but escaped, never as raw HTML.
        assert "javascript:alert(1)" in out
        assert "<script>" not in out

    def test_safe_link_renders_as_anchor(self) -> None:
        out = _inline_md("[docs](https://example.com/x)")
        assert '<a href="https://example.com/x">docs</a>' in out

    def test_link_label_html_escaped(self) -> None:
        # Even inside <a>, the visible label must be escaped.
        out = _inline_md("[<b>x</b>](https://example.com)")
        assert "<b>x</b>" not in out
        assert "&lt;b&gt;x&lt;/b&gt;" in out


# ── _parse_recipients ────────────────────────────────────────────────────────


class TestParseRecipients:
    def test_empty_string(self) -> None:
        assert _parse_recipients("") == []
        assert _parse_recipients(None) == []

    def test_single_address(self) -> None:
        assert _parse_recipients("a@x.com") == ["a@x.com"]

    def test_comma_separated(self) -> None:
        assert _parse_recipients("a@x.com, b@y.com,c@z.com") == [
            "a@x.com",
            "b@y.com",
            "c@z.com",
        ]

    def test_drops_empty_segments(self) -> None:
        assert _parse_recipients("a@x.com,, ,b@y.com") == ["a@x.com", "b@y.com"]

    def test_rejects_crlf_in_address(self) -> None:
        # SMTP header injection vector — must raise, not silently sanitize.
        # Sanitizing risks accepting a partial address that bypasses
        # downstream validation; raising forces the operator to fix env.
        with pytest.raises(ValueError, match="header injection"):
            _parse_recipients("a@x.com,bad\r\nBcc: attacker@evil.com")

    def test_rejects_lone_lf(self) -> None:
        with pytest.raises(ValueError, match="header injection"):
            _parse_recipients("ok@x.com,bad\nBcc: x@y.com")

    def test_rejects_lone_cr(self) -> None:
        with pytest.raises(ValueError, match="header injection"):
            _parse_recipients("ok@x.com,bad\rBcc: x@y.com")


# ── _text_to_html ────────────────────────────────────────────────────────────


def test_text_to_html_escapes_and_preserves_layout() -> None:
    # Used for stdout-style reports (analysis.py, escalation.py). Must
    # preserve column alignment and never let HTML through.
    out = _text_to_html("Total : 500\nFailed: 27 <bad>\n")
    assert "<bad>" not in out
    assert "&lt;bad&gt;" in out
    # Monospace via <pre> so column alignment in the captured stdout
    # survives the mail render.
    assert out.startswith("<pre")
    assert out.endswith("</pre>")
