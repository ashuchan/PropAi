"""Generic HTML email sender.

Thin wrapper around the Gmail MCP send path used by ``email_daily_report.py``
and ``email_merge_analysis.py``. Hand it an HTML string + subject and it
ships through the same authenticated Gmail MCP server, to the same
``REPORT_RECIPIENTS`` (or whatever you pass).

CLI usage
---------

    # Pipe HTML in, send to REPORT_RECIPIENTS
    cat report.html | python scripts/email_html.py --subject "PropAi note"

    # Read HTML from a file
    python scripts/email_html.py --subject "PropAi note" --html-file report.html

    # Override recipients
    python scripts/email_html.py --subject "Hi" --html-file r.html \
        --recipients a@x.com,b@y.com

    # Render only — print plaintext fallback to stdout, do not send
    python scripts/email_html.py --subject "Hi" --html-file r.html --dry-run

    # Attach one or more files (Gmail previews .csv / .xlsx as Sheets)
    python scripts/email_html.py --subject "Daily failures" --html-file r.html \
        --attach data/runs/2026-05-07/scraped_units.xlsx \
        --attach data/runs/2026-05-07/failed_properties.xlsx

Library usage
-------------

    from scripts.email_html import send_html_email

    send_html_email(
        subject="PropAi LLM analysis — 2026-05-05",
        html_body="<html>…</html>",
        recipients=None,    # None → REPORT_RECIPIENTS env var
        attachments=[
            "data/runs/2026-05-05/scraped_units.xlsx",
            "data/runs/2026-05-05/failed_properties.xlsx",
        ],
    )

Env (already in ma_poc/.env):
    REPORT_RECIPIENTS, REPORT_SENDER_NAME, GMAIL_MCP_COMMAND, GMAIL_MCP_ARGS
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

_MA_POC_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_MA_POC_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv  # noqa: E402

# Reuse the daily-report helpers — single source of truth for the MCP send.
from scripts.email_daily_report import (  # noqa: E402
    _html_to_plain,
    _parse_recipients,
    _send_via_mcp,
)

log = logging.getLogger("email_html")


def send_html_email(
    *,
    subject: str,
    html_body: str,
    recipients: list[str] | None = None,
    sender_name: str | None = None,
    attachments: list[str | Path] | None = None,
    dry_run: bool = False,
) -> dict:
    """Send ``html_body`` as an HTML email.

    Parameters
    ----------
    subject:
        Email subject line.
    html_body:
        Full HTML document (or fragment — Gmail renders both).
    recipients:
        List of addresses. If ``None``, falls back to ``REPORT_RECIPIENTS``
        from env.
    sender_name:
        Display name in the From header. ``None`` → ``REPORT_SENDER_NAME``
        env var, then ``"PropAi Daily Reports"``.
    attachments:
        Optional list of file paths to attach. Each path must exist and be
        readable by the user that runs the Gmail MCP server (the MCP
        process reads files directly from disk; we never inline base64
        ourselves). Mixed ``str``/``Path`` is fine — both are coerced.
    dry_run:
        If True, write the plaintext fallback to stdout and skip the send.

    Returns
    -------
    dict with ``isError`` (bool) and ``content`` (list[str]) — the
    flattened MCP tool result. ``{"isError": False, "content": ["dry-run"]}``
    when ``dry_run=True``.
    """
    load_dotenv(_MA_POC_ROOT / ".env")

    if recipients is None:
        recipients = _parse_recipients(os.getenv("REPORT_RECIPIENTS"))
    if not recipients and not dry_run:
        raise RuntimeError(
            "No recipients. Pass recipients=[...] or set REPORT_RECIPIENTS in .env."
        )

    if sender_name is None:
        sender_name = os.getenv("REPORT_SENDER_NAME") or "PropAi Daily Reports"

    # Validate attachment paths up-front so a missing file fails fast with
    # a useful error, instead of surfacing as an opaque MCP tool error.
    resolved_attachments: list[str] = []
    for raw in attachments or []:
        p = Path(raw).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"Attachment not found: {p}")
        if not p.is_file():
            raise IsADirectoryError(f"Attachment must be a file, not a directory: {p}")
        resolved_attachments.append(str(p))

    if dry_run:
        sys.stdout.write(_html_to_plain(html_body))
        sys.stdout.write("\n")
        log.info(
            "Dry run — would send to: %s, attachments: %s",
            ", ".join(recipients) or "(none)",
            resolved_attachments or "(none)",
        )
        return {"isError": False, "content": ["dry-run"]}

    result = asyncio.run(
        _send_via_mcp(
            recipients=recipients,
            subject=subject,
            html_body=html_body,
            sender_name=sender_name,
            attachments=resolved_attachments or None,
        )
    )

    if result.get("isError"):
        log.error("Gmail MCP returned an error: %s", result.get("content"))
    else:
        log.info(
            "Sent %r to %d recipient(s). MCP response: %s",
            subject, len(recipients), result.get("content") or "(empty)",
        )
    return result


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="Send a generic HTML email.")
    parser.add_argument("--subject", required=True, help="Email subject line.")
    src = parser.add_mutually_exclusive_group()
    src.add_argument("--html-file", default=None,
                     help="Path to an HTML file. If omitted, reads from stdin.")
    src.add_argument("--html", default=None,
                     help="HTML body inline (small messages only).")
    parser.add_argument("--recipients", default=None,
                        help="Comma-separated. Defaults to REPORT_RECIPIENTS env var.")
    parser.add_argument("--sender-name", default=None,
                        help="From display name. Defaults to REPORT_SENDER_NAME env.")
    parser.add_argument("--attach", action="append", default=None, metavar="PATH",
                        help="File path to attach. Pass multiple times for multiple "
                             "attachments. Each path must be readable by the MCP server.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plaintext fallback and skip the send.")
    args = parser.parse_args(argv)

    if args.html is not None:
        html_body = args.html
    elif args.html_file:
        html_body = Path(args.html_file).read_text(encoding="utf-8")
    else:
        html_body = sys.stdin.read()
    if not html_body.strip():
        log.error("Empty HTML body. Pass --html, --html-file, or pipe via stdin.")
        return 1

    recipients = _parse_recipients(args.recipients) if args.recipients else None

    try:
        result = send_html_email(
            subject=args.subject,
            html_body=html_body,
            recipients=recipients,
            sender_name=args.sender_name,
            attachments=list(args.attach) if args.attach else None,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        log.exception("Send failed: %s", exc)
        return 1

    return 1 if result.get("isError") else 0


if __name__ == "__main__":
    sys.exit(main())
