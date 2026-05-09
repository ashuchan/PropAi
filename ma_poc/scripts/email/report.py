"""Send a generated report as a professionally-structured HTML email.

Single utility used by every script under ``scripts/reports/`` so they all
ship under the same recipients, transport (``EMAIL_TRANSPORT``), sender
identity, and visual shell.

Why this lives separately from ``_client.py``:
  ``_client.send_html_email`` is the *transport* layer — it knows about
  recipient resolution, attachment validation, and the MCP/gmail_api
  dispatcher. ``email_report`` is the *presentation* layer — it knows
  about the report shell (header, TL;DR callout, footer with run
  context). Keeping them split lets ad-hoc one-shot HTML still go
  through ``_client.send_html_email`` directly without inheriting the
  report shell, while every report script gets the same look without
  duplicating the wrapper HTML.

CLI usage
---------

The utility is also a CLI for one-off sends from the shell:

    python -m scripts.email.report \\
        --subject "Manual report" \\
        --summary "Failed properties spiked from 12 to 47 overnight." \\
        --body-md path/to/report.md \\
        --attach path/to/report.json

Library usage
-------------

    from scripts.email.report import email_report

    email_report(
        subject="PropAi Daily Report — 2026-05-09",
        summary="473/500 successes (94.6%); LLM cost $0.83; 3 carry-forwards.",
        body_md=markdown_text,                  # or body_html=..., or body_text=...
        attachments=["data/runs/2026-05-09/daily_report.html"],
    )
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Sibling-package importability — same dance as the rest of scripts/email/.
_MA_POC_ROOT = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_MA_POC_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv  # noqa: E402

from scripts.email._client import send_html_email  # noqa: E402
from scripts.email.daily import _email_transport, _parse_recipients  # noqa: E402

log = logging.getLogger("email.report")


# ── HTML shell ──────────────────────────────────────────────────────────────


_SHELL_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
       font-size: 14px; line-height: 1.55; color: #1d2333; background: #f6f7f9;
       margin: 0; padding: 18px; }
.wrap { max-width: 880px; margin: 0 auto; background: #fff; border: 1px solid #d8dee8;
        border-radius: 8px; padding: 24px 28px; }
.header { padding-bottom: 14px; border-bottom: 2px solid #d0d6e0; margin-bottom: 18px; }
.eyebrow { color: #6b7280; font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase;
           font-weight: 600; margin-bottom: 4px; }
h1.subject { font-size: 21px; margin: 0 0 6px 0; line-height: 1.3; color: #1d2333; }
.context { color: #6b7280; font-size: 12px; }
.summary { background: #f0f4fa; border-left: 4px solid #2c5282; border-radius: 4px;
           padding: 12px 14px; margin: 18px 0 22px 0; }
.summary .label { color: #2c5282; font-size: 11px; letter-spacing: 0.08em;
                  text-transform: uppercase; font-weight: 700; margin-bottom: 4px; }
.summary p { margin: 0; color: #1d2333; font-size: 14px; line-height: 1.5; }
.body { margin: 18px 0 4px 0; }
.body h1 { font-size: 18px; margin: 22px 0 8px 0; padding-top: 12px;
           border-top: 1px solid #e2e6ee; color: #1e2a44; }
.body h2 { font-size: 15px; margin: 18px 0 6px 0; color: #1e2a44; }
.body h3 { font-size: 13px; margin: 14px 0 4px 0; color: #1e2a44; }
.body p { margin: 6px 0 10px 0; }
.body ul, .body ol { margin: 6px 0 10px 0; padding-left: 22px; }
.body li { margin: 2px 0; }
.body code { font-family: "SF Mono", "Consolas", "Courier New", monospace; font-size: 12.5px;
             background: #f1f4f8; padding: 1px 5px; border-radius: 3px; }
.body pre { background: #f7f8fa; border: 1px solid #e2e6ee; border-radius: 4px;
            padding: 10px 12px; overflow-x: auto; font-family: "SF Mono", "Consolas", monospace;
            font-size: 12.5px; line-height: 1.4; white-space: pre; }
.body pre.preserved { white-space: pre-wrap; }
.body table { border-collapse: collapse; width: 100%; margin: 8px 0 12px 0; font-size: 13px; }
.body th, .body td { padding: 6px 9px; border: 1px solid #d8dee8; text-align: left;
                     vertical-align: top; }
.body th { background: #eef1f6; font-weight: 600; }
.body td.num { text-align: right; font-variant-numeric: tabular-nums; }
.body strong { color: #1d2333; }
.attachments { margin: 16px 0; padding: 10px 14px; background: #fafbfc;
               border: 1px solid #e2e6ee; border-radius: 4px; font-size: 13px; }
.attachments .label { color: #6b7280; font-size: 11px; letter-spacing: 0.08em;
                      text-transform: uppercase; font-weight: 700; margin-bottom: 4px; }
.attachments ul { margin: 4px 0 0 0; padding-left: 18px; }
.attachments li { margin: 2px 0; color: #1d2333; }
.footer { margin-top: 24px; padding-top: 12px; border-top: 1px solid #e2e6ee;
          color: #6b7280; font-size: 11px; line-height: 1.5; }
.footer .row { margin-bottom: 2px; }
.footer code { font-family: "SF Mono", "Consolas", monospace; font-size: 11px; }
"""


def _render_shell(
    *,
    subject: str,
    summary: str,
    body_html_inner: str,
    attachments: list[Path] | None,
    sender_name: str,
) -> str:
    """Wrap ``body_html_inner`` in the standard report shell."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    attach_html = ""
    if attachments:
        items = "\n".join(
            f"  <li>{html.escape(p.name)} <code>({p.stat().st_size // 1024} KB)</code></li>"
            for p in attachments
            if p.exists()
        )
        attach_html = (
            f'<div class="attachments">\n'
            f'  <div class="label">Attached files</div>\n'
            f'  <ul>\n{items}\n  </ul>\n'
            f'</div>'
        )

    # Cloud Run banner — only present when running inside a job.
    footer_rows = [f'<div class="row">Generated {html.escape(now)}</div>']
    cr_job = os.environ.get("CLOUD_RUN_JOB")
    cr_exec = os.environ.get("CLOUD_RUN_EXECUTION")
    cr_task = os.environ.get("CLOUD_RUN_TASK_INDEX")
    if cr_job:
        cr_line = f"Cloud Run job <code>{html.escape(cr_job)}</code>"
        if cr_exec:
            cr_line += f", execution <code>{html.escape(cr_exec)}</code>"
        if cr_task is not None:
            cr_line += f", task <code>{html.escape(cr_task)}</code>"
        footer_rows.append(f'<div class="row">{cr_line}</div>')
    transport = _email_transport()
    delegated = os.environ.get("GMAIL_DELEGATED_USER", "")
    footer_rows.append(
        f'<div class="row">Sent via <code>{html.escape(transport)}</code>'
        + (f" as <code>{html.escape(delegated)}</code>" if delegated else "")
        + "</div>"
    )

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>{html.escape(subject)}</title>
<style>{_SHELL_CSS}</style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div class="eyebrow">{html.escape(sender_name)}</div>
      <h1 class="subject">{html.escape(subject)}</h1>
    </div>
    <div class="summary">
      <div class="label">Summary</div>
      <p>{html.escape(summary)}</p>
    </div>
    {attach_html}
    <div class="body">
{body_html_inner}
    </div>
    <div class="footer">
      {chr(10).join(footer_rows)}
    </div>
  </div>
</body>
</html>
"""


# ── Lightweight markdown → HTML converter ───────────────────────────────────
#
# We avoid pulling in a full markdown library (e.g. python-markdown) because
# the reports use a tight subset: headings, paragraphs, bullet lists,
# fenced code blocks, pipe tables, and inline bold/italic/code. Hand-rolled
# coverage of that subset is ~120 lines and means one fewer runtime dep.
# Anything outside the subset falls through as escaped text inside <p>, so
# the worst-case render is "ugly but readable" — never broken HTML.


_MD_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_MD_BOLD = re.compile(r"\*\*([^*\n]+)\*\*")
_MD_ITALIC = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")

# Allowed URL schemes for markdown links. Reports today are operator-
# authored, but several scripts splice property names / URLs from the
# CSV into their markdown. If any of those ever contains
# ``[click me](javascript:...)`` we'd ship clickable script in the
# email — a real XSS vector against any mail client that respects
# href schemes (Gmail strips them, but Outlook/Apple Mail do not).
# Any link with a scheme outside this set is rendered as plain text
# (escaped) so the URL is visible but not clickable.
_ALLOWED_LINK_SCHEMES = ("http://", "https://", "mailto:")


def _safe_link_target(url: str) -> str | None:
    """Return ``url`` if it's safe to use as an ``href``, else ``None``.

    Accepts:
    * absolute URLs with an allow-listed scheme (``http``, ``https``, ``mailto``);
    * scheme-relative URLs (``//host/path``);
    * fragment-only links (``#section``);
    * relative paths (``./foo``, ``../bar``, ``foo/bar``) without a colon
      before the first ``/`` — the colon-before-slash check is the
      classic "no scheme" gate that catches ``javascript:foo`` and
      ``data:text/html,...`` while leaving ``foo:bar/`` (no scheme,
      just a colon-named directory) alone.
    """
    u = url.strip()
    if not u:
        return None
    lo = u.lower()
    if lo.startswith(_ALLOWED_LINK_SCHEMES):
        return u
    if u.startswith(("//", "#", "/", "./", "../")):
        return u
    # Reject anything else with a colon before a slash — that's a scheme.
    first_slash = u.find("/")
    first_colon = u.find(":")
    if first_colon != -1 and (first_slash == -1 or first_colon < first_slash):
        return None
    return u


def _render_md_link(match: re.Match[str]) -> str:
    label = match.group(1)
    raw_url = match.group(2)
    href = _safe_link_target(raw_url)
    if href is None:
        # Render as plain text so the URL stays visible but is not clickable.
        return f"{label} ({html.escape(raw_url)})"
    # Both label and href are already inside an HTML-escaped string at this
    # point (the surrounding _inline_md ran html.escape() before our regex
    # fired), so the captured groups are themselves escaped. Re-escape the
    # href into an HTML attribute context just to be belt-and-braces safe.
    return f'<a href="{html.escape(href, quote=True)}">{label}</a>'


def _inline_md(text: str) -> str:
    """Apply inline markdown transforms (bold/italic/code/link). Order matters:
    inline code first so ``*`` inside ``code`` doesn't get bolded."""
    out = html.escape(text)
    # Inline code (re-escaped output → wrap escaped text in <code>).
    out = _MD_INLINE_CODE.sub(lambda m: f"<code>{m.group(1)}</code>", out)
    out = _MD_BOLD.sub(r"<strong>\1</strong>", out)
    out = _MD_ITALIC.sub(r"<em>\1</em>", out)
    # Link: only http(s)/mailto/relative URLs become anchor tags. Anything
    # with another scheme (javascript:, data:, file:, vbscript:) renders
    # as escaped plain text — see _safe_link_target.
    out = _MD_LINK.sub(_render_md_link, out)
    return out


def _md_to_html(md: str) -> str:
    """Convert a constrained-subset markdown string to HTML.

    Supports: ATX headings (#/##/###), bullet lists (- / *), pipe tables
    (with the ``|---|`` separator), fenced code blocks (```...```),
    inline bold/italic/code/links, and paragraphs. Anything else is
    rendered as an escaped paragraph.
    """
    lines = md.splitlines()
    out: list[str] = []
    i = 0
    in_code = False
    code_buf: list[str] = []

    def _flush_paragraph(buf: list[str]) -> None:
        if not buf:
            return
        joined = " ".join(buf).strip()
        if joined:
            out.append(f"<p>{_inline_md(joined)}</p>")
        buf.clear()

    para: list[str] = []

    while i < len(lines):
        line = lines[i]

        # Fenced code block.
        if line.strip().startswith("```"):
            if in_code:
                out.append(
                    "<pre class=\"preserved\"><code>"
                    + html.escape("\n".join(code_buf))
                    + "</code></pre>"
                )
                code_buf = []
                in_code = False
            else:
                _flush_paragraph(para)
                in_code = True
            i += 1
            continue
        if in_code:
            code_buf.append(line)
            i += 1
            continue

        stripped = line.strip()

        # Blank line ends a paragraph.
        if not stripped:
            _flush_paragraph(para)
            i += 1
            continue

        # Headings.
        m = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if m:
            _flush_paragraph(para)
            level = min(len(m.group(1)), 3)  # cap at h3 — h1/h2 used by the shell.
            out.append(f"<h{level}>{_inline_md(m.group(2))}</h{level}>")
            i += 1
            continue

        # Pipe table — header row + separator + body rows.
        if stripped.startswith("|") and stripped.endswith("|") and i + 1 < len(lines):
            sep = lines[i + 1].strip()
            if (
                sep.startswith("|")
                and sep.endswith("|")
                and re.fullmatch(r"\|[\s:|-]+\|", sep)
            ):
                _flush_paragraph(para)
                header_cells = [c.strip() for c in stripped.strip("|").split("|")]
                rows: list[list[str]] = []
                j = i + 2
                while j < len(lines):
                    row = lines[j].strip()
                    if not (row.startswith("|") and row.endswith("|")):
                        break
                    rows.append([c.strip() for c in row.strip("|").split("|")])
                    j += 1
                table_html = ["<table>", "  <thead>", "    <tr>"]
                table_html += [f"      <th>{_inline_md(c)}</th>" for c in header_cells]
                table_html += ["    </tr>", "  </thead>", "  <tbody>"]
                for row in rows:
                    table_html.append("    <tr>")
                    for c in row:
                        cls = ' class="num"' if re.fullmatch(r"[-+]?[\d.,%$()s]+", c.strip()) else ""
                        table_html.append(f"      <td{cls}>{_inline_md(c)}</td>")
                    table_html.append("    </tr>")
                table_html += ["  </tbody>", "</table>"]
                out.append("\n".join(table_html))
                i = j
                continue

        # Bullet list (consecutive ``- `` / ``* `` lines).
        if re.match(r"^[-*]\s+", stripped):
            _flush_paragraph(para)
            items: list[str] = []
            while i < len(lines):
                m2 = re.match(r"^[-*]\s+(.*)$", lines[i].strip())
                if not m2:
                    break
                items.append(f"  <li>{_inline_md(m2.group(1))}</li>")
                i += 1
            out.append("<ul>\n" + "\n".join(items) + "\n</ul>")
            continue

        # Ordered list.
        if re.match(r"^\d+\.\s+", stripped):
            _flush_paragraph(para)
            items = []
            while i < len(lines):
                m2 = re.match(r"^\d+\.\s+(.*)$", lines[i].strip())
                if not m2:
                    break
                items.append(f"  <li>{_inline_md(m2.group(1))}</li>")
                i += 1
            out.append("<ol>\n" + "\n".join(items) + "\n</ol>")
            continue

        # Default: accumulate into the current paragraph.
        para.append(stripped)
        i += 1

    _flush_paragraph(para)
    if in_code and code_buf:
        # Unterminated code fence — flush what we have.
        out.append(
            "<pre class=\"preserved\"><code>"
            + html.escape("\n".join(code_buf))
            + "</code></pre>"
        )

    return "\n".join(out)


def _text_to_html(text: str) -> str:
    """Wrap a plain-text body (e.g. ``escalation.py`` console output) so
    monospace alignment is preserved without losing readability. Used when
    the report has no native HTML/markdown form."""
    return f'<pre class="preserved">{html.escape(text.rstrip())}</pre>'


# ── Public API ──────────────────────────────────────────────────────────────


def email_report(
    *,
    subject: str,
    summary: str,
    body_html: str | None = None,
    body_md: str | None = None,
    body_text: str | None = None,
    attachments: list[str | Path] | None = None,
    recipients: list[str] | None = None,
    sender_name: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Send a report as a professionally-structured HTML email.

    Exactly one of ``body_html`` / ``body_md`` / ``body_text`` must be
    provided — the choice tells the shell how to render the report body:

    * ``body_html``: pre-rendered HTML fragment. Inserted into the shell's
      ``<div class="body">`` verbatim. Use when the report owns its own
      HTML rendering (e.g. ``reports/daily.py``).
    * ``body_md``: markdown text. Run through the in-house converter and
      inserted into the shell. Use for ``reports/health.py``,
      ``reports/floor_plan_comparison.py``.
    * ``body_text``: plain text. Wrapped in monospace ``<pre>``. Use for
      reports that just print to stdout (``reports/analysis.py``,
      ``reports/escalation.py``).

    Recipients fall back to ``REPORT_RECIPIENTS`` env var. Sender display
    name falls back to ``REPORT_SENDER_NAME`` then a default.

    Returns the standard ``{"isError": bool, "content": list[str]}`` dict
    from the underlying email transport.
    """
    load_dotenv(_MA_POC_ROOT / ".env")

    body_inputs = [b for b in (body_html, body_md, body_text) if b is not None]
    if len(body_inputs) != 1:
        raise ValueError(
            "email_report requires exactly one of body_html, body_md, body_text — "
            f"got {len(body_inputs)}."
        )

    if recipients is None:
        recipients = _parse_recipients(os.getenv("REPORT_RECIPIENTS"))
    if not recipients and not dry_run:
        log.warning(
            "email_report: no recipients (REPORT_RECIPIENTS unset, no override) "
            "— skipping send for subject=%r. Set REPORT_RECIPIENTS or pass "
            "recipients=[...] to enable.",
            subject,
        )
        return {"isError": False, "content": ["no-recipients-skipped"]}

    if sender_name is None:
        sender_name = os.getenv("REPORT_SENDER_NAME") or "PropAi Reports"

    resolved_attachments: list[Path] = []
    for raw in attachments or []:
        p = Path(raw).expanduser().resolve()
        if not p.exists():
            log.warning("email_report: attachment not found, skipping: %s", p)
            continue
        if not p.is_file():
            log.warning("email_report: attachment is not a file, skipping: %s", p)
            continue
        resolved_attachments.append(p)

    if body_html is not None:
        body_inner = body_html
    elif body_md is not None:
        body_inner = _md_to_html(body_md)
    else:
        body_inner = _text_to_html(body_text or "")

    full_html = _render_shell(
        subject=subject,
        summary=summary,
        body_html_inner=body_inner,
        attachments=resolved_attachments,
        sender_name=sender_name,
    )

    return send_html_email(
        subject=subject,
        html_body=full_html,
        recipients=recipients,
        sender_name=sender_name,
        attachments=[str(p) for p in resolved_attachments] or None,
        dry_run=dry_run,
    )


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="Send a generated report as a styled email.")
    ap.add_argument("--subject", required=True, help="Email subject line.")
    ap.add_argument("--summary", required=True,
                    help="One-paragraph TL;DR shown in the summary callout (≤ ~500 chars).")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--body-html", default=None, help="Path to a pre-rendered HTML body file.")
    src.add_argument("--body-md", default=None, help="Path to a markdown body file.")
    src.add_argument("--body-text", default=None, help="Path to a plain-text body file.")
    ap.add_argument("--attach", action="append", default=None, metavar="PATH",
                    help="Attach a file. Pass multiple times for multiple attachments.")
    ap.add_argument("--recipients", default=None,
                    help="Comma-separated. Defaults to REPORT_RECIPIENTS env var.")
    ap.add_argument("--sender-name", default=None,
                    help="From display name. Defaults to REPORT_SENDER_NAME env.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Render and print the plaintext fallback; do not send.")
    args = ap.parse_args(argv)

    body_kwargs: dict[str, Any] = {}
    if args.body_html:
        body_kwargs["body_html"] = Path(args.body_html).read_text(encoding="utf-8")
    elif args.body_md:
        body_kwargs["body_md"] = Path(args.body_md).read_text(encoding="utf-8")
    else:
        body_kwargs["body_text"] = Path(args.body_text).read_text(encoding="utf-8")

    recipients = _parse_recipients(args.recipients) if args.recipients else None

    try:
        result = email_report(
            subject=args.subject,
            summary=args.summary,
            attachments=list(args.attach) if args.attach else None,
            recipients=recipients,
            sender_name=args.sender_name,
            dry_run=args.dry_run,
            **body_kwargs,
        )
    except Exception as exc:
        log.exception("email_report failed: %s", exc)
        return 1

    return 1 if result.get("isError") else 0


# Re-export so callers can mock the underlying transport in tests.
__all__ = ["email_report", "main"]


if __name__ == "__main__":
    # Suppress unused-import lint for asyncio — kept available so callers
    # who reach into the module for asyncio plumbing don't get bitten.
    _ = asyncio
    sys.exit(main())
