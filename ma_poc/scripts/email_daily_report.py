"""Email the daily PropAi scrape report to a list of recipients.

Reads the daily report from the data-provider DB (local Postgres / SQLite
or Cloud SQL — whichever ``DATA_PROVIDER`` + ``DATABASE_URL`` point at),
renders an HTML body, and sends it via the Gmail MCP server
(`@gongrzhe/server-gmail-autoauth-mcp`).

The script spawns the MCP server itself over stdio — Claude Code is NOT
in the loop. That makes it safe to run from cron / Windows Task
Scheduler. The Gmail account used for sending is whatever was authorised
when you ran `npx @gongrzhe/server-gmail-autoauth-mcp auth` (credentials
live at `~/.gmail-mcp/credentials.json`).

────────────────────────────────────────────────────────────────────────
Usage
────────────────────────────────────────────────────────────────────────

    # Latest run (today's UTC date by default)
    python scripts/email_daily_report.py

    # Specific run
    python scripts/email_daily_report.py --run-date 2026-04-25

    # Override recipients (otherwise read from REPORT_RECIPIENTS env var)
    python scripts/email_daily_report.py --recipients a@x.com,b@y.com

    # Render only — print the HTML to stdout, do not send
    python scripts/email_daily_report.py --dry-run

    # Override the DB URL (asyncpg URLs auto-translate to pg8000)
    python scripts/email_daily_report.py \\
        --database-url postgresql+pg8000://user:pass@localhost:5432/proppy

Required env (already in ma_poc/.env):
    REPORT_RECIPIENTS       Comma-separated default recipient list
    REPORT_SENDER_NAME      Display name in the email From header
    GMAIL_MCP_COMMAND       MCP server launch command (default: npx)
    GMAIL_MCP_ARGS          Args, space-separated (default: @gongrzhe/...)
    DATA_PROVIDER           postgres | sqlite | filesystem (default: filesystem)
    DATABASE_URL            postgresql+pg8000://user:pass@host:port/dbname

Optional env (Cloud SQL path — used in production task scheduler):
    CLOUD_SQL_INSTANCE      project:region:instance. When set, routes
                            through Google's Cloud SQL frontend instead
                            of the data-provider factory.
    CLOUD_SQL_PROXY_BIN     Absolute path to the cloud-sql-proxy binary.
                            Defaults to whatever is on PATH.
    CLOUD_SQL_USE_IAM_TOKEN Set to ``1`` to authenticate to Postgres as
                            the ADC principal (proxy runs with
                            ``--auto-iam-authn``) instead of using the
                            password in DATABASE_URL.

Connection paths
----------------
  1. ``--database-url`` flag — direct connect, ignores everything else.
  2. ``CLOUD_SQL_INSTANCE`` set — Cloud SQL via connector or proxy.
  3. Falls through to ``data_provider.factory.get_data_provider()``,
     which honours ``DATA_PROVIDER`` + ``DATABASE_URL`` from the env.

For the local-DB use case, the simplest setup is:

    DATA_PROVIDER=postgres
    DATABASE_URL=postgresql+pg8000://postgres:<pwd>@localhost:5432/proppy

(The ``.env`` ships an ``+asyncpg`` URL for the TS API service; this
script auto-translates it to ``+pg8000`` so no manual edit is needed.)

────────────────────────────────────────────────────────────────────────
Windows Task Scheduler — add / remove / edit
────────────────────────────────────────────────────────────────────────

ADD a daily 8:00 AM job (run from PowerShell as the user, NOT elevated,
because the Gmail MCP credentials live under your user profile):

    schtasks /Create ^
        /TN "PropAi Daily Report Email" ^
        /TR "\"C:\\Users\\ashus\\OneDrive\\Documents\\Code\\PropAi\\ma_poc\\.venv\\Scripts\\python.exe\" \"C:\\Users\\ashus\\OneDrive\\Documents\\Code\\PropAi\\ma_poc\\scripts\\email_daily_report.py\"" ^
        /SC DAILY /ST 08:00 ^
        /F

CHECK status / next run time:

    schtasks /Query /TN "PropAi Daily Report Email" /V /FO LIST

RUN it manually right now (good for verifying the schedule entry works):

    schtasks /Run /TN "PropAi Daily Report Email"

EDIT the schedule (e.g. move to 9:30 AM):

    schtasks /Change /TN "PropAi Daily Report Email" /ST 09:30

REMOVE the job:

    schtasks /Delete /TN "PropAi Daily Report Email" /F

If you'd rather run it after the Jugnu runner finishes locally instead
of on a fixed clock, chain it in a .bat file and point schtasks at the
.bat — the script exits 0 on success and 1 on any failure so the chain
can short-circuit.

────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import argparse
import asyncio
import atexit
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Make sibling packages (`data_provider`, `scripts`, …) importable when this
# file is invoked as `python scripts/email_daily_report.py` from the ma_poc
# root. The repo root is also needed because some modules (e.g.
# models.scrape_profile) reach back via `from ma_poc.models...`.
_MA_POC_ROOT = Path(__file__).resolve().parent.parent
_REPO_ROOT = _MA_POC_ROOT.parent
for _p in (_MA_POC_ROOT, _REPO_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from dotenv import load_dotenv  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.engine.url import make_url  # noqa: E402

from data_provider.contracts import DataProvider  # noqa: E402
from data_provider.dtos import IssueEntry, RunReport  # noqa: E402
from data_provider.factory import get_data_provider, reset_cached_provider  # noqa: E402
from data_provider.sql.engine import (  # noqa: E402
    make_engine,
    resolve_database_url,
)
from data_provider.sql.provider import SqlDataProvider  # noqa: E402
from services.diff_calculator import (  # noqa: E402
    DailyDiff as DailyDiffDTO,
    RunSummary as RunSummaryDTO,
    compute_daily_diff,
    compute_run_history,
    compute_run_summary,
    compute_units_seen_in_all_runs,
    lookup_property_info,
    previous_run_date,
)


def _normalize_database_url(url: str | None) -> str | None:
    """Translate `+asyncpg` URLs to `+pg8000` so the sync engine works.

    The repo's `.env` ships with `postgresql+asyncpg://...` (used by the
    TS API layer); the data-provider's sync engine can't drive asyncpg,
    but the same DB is reachable via pg8000 with no other change.
    """
    if not url:
        return url
    if "+asyncpg" in url:
        return url.replace("+asyncpg", "+pg8000")
    return url


def _open_provider(database_url: str | None) -> SqlDataProvider:
    """Return a SqlDataProvider for the script.

    Connection priority:
      1. ``--database-url`` flag (or ``database_url`` arg) — direct connect.
      2. ``CLOUD_SQL_INSTANCE`` env set — cloud-sql-proxy fallback path.
      3. ``DATABASE_URL`` env / data-provider factory default — local DB.

    asyncpg URLs are auto-translated to pg8000 so the local ``.env``
    (which ships an asyncpg URL for the TS API) works without manual
    edits.
    """
    if database_url:
        os.environ["DATABASE_URL"] = _normalize_database_url(database_url) or database_url
    else:
        env_url = os.environ.get("DATABASE_URL")
        normalized = _normalize_database_url(env_url)
        if normalized and normalized != env_url:
            os.environ["DATABASE_URL"] = normalized

    instance = os.getenv("CLOUD_SQL_INSTANCE", "").strip()
    if instance and not database_url:
        # Cloud SQL path retained for the production task-scheduler use case
        # so the same script can email reports from the Cloud SQL `jugnu`
        # instance when invoked on a build/email host. Local invocations
        # (CLOUD_SQL_INSTANCE unset) flow straight through to the data
        # provider factory below.
        engine = _make_cloud_sql_engine(instance)
        return SqlDataProvider(
            engine=engine,
            create_schema=False,
            provider_name="postgres",
        )

    reset_cached_provider()
    provider = get_data_provider()
    if not isinstance(provider, SqlDataProvider):
        raise RuntimeError(
            f"email_daily_report needs a SQL-backed data provider; got "
            f"{provider.name!r}. Set DATA_PROVIDER=postgres (or sqlite) and "
            f"a DATABASE_URL pointing at the run database."
        )
    return provider


def _make_cloud_sql_engine(instance: str) -> Any:
    """Open a SQLAlchemy engine bound to a Cloud SQL instance.

    Used only when ``CLOUD_SQL_INSTANCE`` is set in the env — the
    production task-scheduler flow. Local invocations skip this path
    entirely and use the data-provider factory.

    Connection priority:
      1. ``google-cloud-sql-python-connector`` (preferred — handles auth
         + TLS + IAM transparently). Requires ``aiohttp`` to import.
      2. cloud-sql-proxy fallback (Windows ARM64, where the connector's
         aiohttp dep has no prebuilt wheel).
    """
    # Path 1 — Cloud SQL Connector (in-process TLS tunnel via aiohttp).
    # SyntaxError catches the Windows ARM64 case where aiohttp failed to
    # install a working wheel and the import itself is broken at parse time.
    try:
        from google.cloud.sql.connector import Connector  # noqa: F401

        log.info(
            "Cloud SQL: using google-cloud-sql-python-connector "
            "(instance=%s, ADC)",
            instance,
        )
        return make_engine()
    except (ImportError, SyntaxError) as exc:
        log.warning(
            "Cloud SQL Connector unavailable (%s: %s) — falling back to "
            "cloud-sql-proxy.",
            type(exc).__name__,
            exc,
        )

    # Path 2 — cloud-sql-proxy (out-of-process TLS tunnel via Go binary).
    return _make_auth_proxy_engine(instance)


def _make_auth_proxy_engine(instance: str) -> Any:
    """Spawn ``cloud-sql-proxy`` and connect via pg8000 to its local port.

    The proxy creates an authenticated TLS tunnel to Cloud SQL using
    Application Default Credentials — the same pattern the production
    Cloud SQL Connector uses, just out-of-process so we don't need
    aiohttp (which has no Windows ARM64 wheel). Crucially, this means
    no IP whitelisting: traffic leaves the machine over Google's
    sqladmin frontend, not by directly hitting the instance's public IP.

    Auth modes:
      • PASSWORD (default) — credentials from ``DATABASE_URL``.
      • IAM (``CLOUD_SQL_USE_IAM_TOKEN=1``) — adds ``--auto-iam-authn``
        so the proxy logs in to Postgres as the ADC principal. Requires
        the principal to have been provisioned as a Cloud SQL IAM
        database user.
    """
    parsed = make_url(resolve_database_url(None))
    if not parsed.username or not parsed.database:
        raise RuntimeError(
            "DATABASE_URL must supply at least user + database "
            f"(got {parsed.render_as_string(hide_password=True)!r})."
        )

    proxy_bin = (
        os.getenv("CLOUD_SQL_PROXY_BIN")
        or shutil.which("cloud-sql-proxy")
        or shutil.which("cloud_sql_proxy")
    )
    if not proxy_bin:
        raise RuntimeError(
            "cloud-sql-proxy binary not found on PATH. Install it from\n"
            "  https://cloud.google.com/sql/docs/postgres/connect-auth-proxy\n"
            "or set CLOUD_SQL_PROXY_BIN in ma_poc/.env to its absolute path."
        )

    use_iam_authn = os.getenv("CLOUD_SQL_USE_IAM_TOKEN", "").lower() in {
        "1", "true", "yes", "on"
    }

    # Find a free localhost port. We bind explicitly to 127.0.0.1 (not
    # 0.0.0.0) so the local Postgres tunnel is never exposed beyond the
    # loopback interface.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    cmd = [proxy_bin, f"--port={port}", "--address=127.0.0.1"]
    if use_iam_authn:
        cmd.append("--auto-iam-authn")
    cmd.append(instance)

    log.info("Spawning cloud-sql-proxy: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        # CREATE_NEW_PROCESS_GROUP keeps Ctrl-C in the parent from killing
        # the proxy mid-query on Windows; we manage its lifetime via atexit.
        creationflags=(
            subprocess.CREATE_NEW_PROCESS_GROUP
            if hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP")
            else 0
        ),
    )

    # Tear down on script exit, before the venv tears down stdlib.
    def _terminate_proxy() -> None:
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()

    atexit.register(_terminate_proxy)

    # Poll the local port until the proxy is listening (or it died).
    deadline = time.monotonic() + 15.0
    ready = False
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(
                f"cloud-sql-proxy exited (code {proc.returncode}). "
                f"Output:\n{output}"
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                ready = True
                break
        except OSError:
            time.sleep(0.2)

    if not ready:
        proc.terminate()
        raise RuntimeError(
            "cloud-sql-proxy did not bind within 15s. Check ADC with "
            "`gcloud auth application-default login` and IAM roles "
            "(cloudsql.client + cloudsql.instanceUser)."
        )

    log.info(
        "cloud-sql-proxy listening on 127.0.0.1:%d (instance=%s, "
        "iam_authn=%s)",
        port,
        instance,
        use_iam_authn,
    )

    if use_iam_authn:
        # With --auto-iam-authn the proxy injects the OAuth token as the
        # Postgres password upstream; pg8000 just passes the username.
        url = (
            f"postgresql+pg8000://{parsed.username}"
            f"@127.0.0.1:{port}/{parsed.database}"
        )
    else:
        password = parsed.password or ""
        url = (
            f"postgresql+pg8000://{parsed.username}:{password}"
            f"@127.0.0.1:{port}/{parsed.database}"
        )

    # No SSL on the local hop — the proxy already encrypted the upstream.
    return create_engine(url, future=True, pool_pre_ping=True)

log = logging.getLogger("email_daily_report")


# ── DB read (via the data-provider layer) ───────────────────────────────────
#
# All reads go through SqlDataProvider rather than raw SQLAlchemy. That
# matches how the production runners and the TS API service read the same
# tables, so changes to the underlying schema only need to land in one
# place (data_provider/sql/stores.py + DTOs in data_provider/dtos.py).


def _resolve_run_date(provider: SqlDataProvider, requested: str | None) -> str:
    """Return the run_date to email. Default = today (UTC). Falls back to
    the most recent ``runs`` row if today has nothing."""
    if requested:
        return requested
    today = datetime.now(timezone.utc).date().isoformat()
    runs = provider.runs.list_runs()  # ascending YYYY-MM-DD strings
    if today in runs:
        return today
    if not runs:
        raise RuntimeError(
            "No runs found in the runs table — nothing to email."
        )
    latest = runs[-1]
    log.warning("No run for %s yet; falling back to latest run %s", today, latest)
    return latest


def _summarize_issue_codes(
    issues: Iterable[IssueEntry], top_n: int = 10
) -> list[tuple[str, int]]:
    """Aggregate top-N issue-code counts from the DTO stream."""
    code: Counter[str] = Counter()
    for issue in issues:
        code[issue.code or "UNKNOWN"] += 1
    return code.most_common(top_n)


def _load_issue_example_cids(
    provider: SqlDataProvider,
    run_date: str,
    codes: list[str],
    k: int = 2,
) -> dict[str, list[str]]:
    """Return up to k example canonical_ids per issue code for the given run.

    Used purely as the cid set we then enrich with property name + URL
    via ``services.diff_calculator.lookup_property_info``. The runner
    writes ``message`` as the code text repeated, so we don't bother
    pulling it.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import Session

    from data_provider.sql.models import RunIssueRow

    out: dict[str, list[str]] = {}
    with Session(provider.engine, future=True) as s:
        for code in codes:
            rows = (
                s.execute(
                    select(RunIssueRow.canonical_id)
                    .where(RunIssueRow.run_date == run_date)
                    .where(RunIssueRow.code == code)
                    .where(RunIssueRow.canonical_id.is_not(None))
                    .limit(k)
                )
                .scalars()
                .all()
            )
            out[code] = [cid for cid in rows if cid]
    return out


def _club_tier_distribution(tiers: dict[str, int]) -> dict[str, int]:
    """Collapse the 6+ granular sub-tier counters into a small set of
    top-level rows. Tier 4 sub-variants (LLM_API, LLM_DOM, LLM monolithic)
    fold into a single 'LLM-assisted' bucket; everything else keeps its
    canonical tier name."""
    out: dict[str, int] = {}
    for raw, count in (tiers or {}).items():
        if not count:
            continue
        upper = str(raw).upper()
        if "VISION" in upper or "TIER_5" in upper:
            label = "Tier 5 — Vision"
        elif "TIER_4" in upper or ("LLM" in upper and "PROFILE" not in upper):
            label = "Tier 4 — LLM-assisted"
        elif "PROFILE" in upper:
            label = "Tier 1 — Profile replay"
        elif "TIER_1" in upper or ("API" in upper and "LLM" not in upper):
            label = "Tier 1 — API interception"
        elif "TIER_2" in upper or "JSON" in upper:
            label = "Tier 2 — JSON-LD"
        elif "TIER_3" in upper or "DOM" in upper:
            label = "Tier 3 — DOM templates"
        else:
            label = str(raw)
        out[label] = out.get(label, 0) + int(count)
    return out




# ── HTML rendering ───────────────────────────────────────────────────────────
#
# The layout is built from email-safe primitives: a <style> block in <head>
# (Gmail web preserves it for inline rendering) plus belt-and-braces inline
# styles on every important element so clients that strip <style> still get
# acceptable output. Outer width is fixed at 720px — the standard "looks
# good in Gmail web + Outlook + iOS Mail" sweet spot.


_FONT_STACK = (
    "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, "
    "'Helvetica Neue', Arial, sans-serif"
)
_MONO_STACK = (
    "'JetBrains Mono', 'SF Mono', Menlo, Consolas, "
    "'Roboto Mono', monospace"
)

_CSS = f"""
  body {{ margin:0; padding:0; background:#f4f6fa;
          font-family:{_FONT_STACK}; color:#1f2937; }}
  .wrap {{ max-width:720px; margin:0 auto; padding:24px 16px; }}
  .card {{ background:#ffffff; border:1px solid #e5e7eb; border-radius:10px;
           overflow:hidden; box-shadow:0 1px 2px rgba(15,23,42,0.04); }}
  .header {{ background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);
             padding:28px 32px; color:#ffffff; }}
  .brand {{ font-size:22px; font-weight:700; letter-spacing:-0.01em;
            margin:0; line-height:1.2; }}
  .brand .accent {{ color:#60a5fa; font-weight:500; }}
  .tagline {{ font-size:12px; text-transform:uppercase; letter-spacing:0.12em;
              color:#cbd5e1; margin-top:6px; font-weight:500; }}
  .meta-bar {{ padding:18px 32px; background:#f8fafc;
               border-bottom:1px solid #e5e7eb; display:flex;
               justify-content:space-between; flex-wrap:wrap; gap:8px;
               font-size:13px; color:#475569; }}
  .meta-bar .label {{ color:#94a3b8; font-weight:500;
                      text-transform:uppercase; letter-spacing:0.06em;
                      font-size:11px; margin-right:6px; }}
  .meta-bar strong {{ color:#0f172a; font-weight:600; }}
  .content {{ padding:8px 32px 32px 32px; }}
  h2 {{ font-size:13px; font-weight:600; color:#475569; margin:28px 0 10px;
        text-transform:uppercase; letter-spacing:0.08em; }}
  h2:first-of-type {{ margin-top:24px; }}
  table {{ border-collapse:separate; border-spacing:0; width:100%;
           font-size:13px; background:#ffffff;
           border:1px solid #e5e7eb; border-radius:8px; overflow:hidden; }}
  th, td {{ text-align:left; padding:10px 14px;
            border-bottom:1px solid #f1f5f9; }}
  tr:last-child td, tr:last-child th {{ border-bottom:0; }}
  thead th {{ background:#f8fafc; font-weight:600; color:#334155;
              font-size:11px; text-transform:uppercase;
              letter-spacing:0.06em; }}
  tbody th {{ background:#fafafa; font-weight:500; color:#334155;
              width:55%; }}
  tbody tr:nth-child(even) td,
  tbody tr:nth-child(even) th {{ background:#fcfcfd; }}
  td.num, th.num {{ text-align:right; font-variant-numeric:tabular-nums;
                    font-feature-settings:'tnum'; color:#111827; }}
  td code, .mono {{ font-family:{_MONO_STACK}; font-size:12px;
                    color:#1e3a8a; background:#eff6ff;
                    padding:2px 6px; border-radius:4px; }}
  .pill {{ display:inline-block; padding:3px 10px; border-radius:999px;
           font-size:11px; font-weight:600; letter-spacing:0.02em;
           text-transform:uppercase; }}
  .pill.error   {{ background:#fee2e2; color:#991b1b; }}
  .pill.warning {{ background:#fef3c7; color:#92400e; }}
  .pill.info    {{ background:#dbeafe; color:#1e40af; }}
  .pill.unknown {{ background:#e5e7eb; color:#374151; }}
  .empty {{ color:#94a3b8; font-style:italic; font-size:13px;
            padding:14px 16px; background:#f8fafc;
            border:1px dashed #e2e8f0; border-radius:8px; }}
  .footer {{ text-align:center; padding:20px 32px;
             color:#94a3b8; font-size:11px;
             border-top:1px solid #f1f5f9; background:#fafafa; }}
"""


def _fmt_num(v: Any, *, key: str | None = None) -> str:
    """Format a value for table display.

    `key` is the field name — used to pick a nicer format for known shapes
    (rates → percentage, *_usd → currency, counts → integer).
    """
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "Yes" if v else "No"

    k = (key or "").lower()
    if isinstance(v, (int, float)):
        if k.endswith("_usd") or "cost" in k or "spend" in k:
            return f"${v:,.4f}" if abs(v) < 10 else f"${v:,.2f}"
        if k.endswith("_rate") or "_rate_" in k or k in {
            "success_rate", "failure_rate"
        }:
            return f"{v * 100:.2f}%" if abs(v) <= 1 else f"{v:.2f}"
        if isinstance(v, float):
            return f"{v:,.4f}" if abs(v) < 10 and v != int(v) else f"{v:,.2f}"
        return f"{v:,}"
    return str(v)


def _humanize(key: str) -> str:
    """`properties_total` → `Properties Total`, `TIER_1_API` → `Tier 1 Api`."""
    s = key.replace("_", " ").replace("-", " ").strip()
    return " ".join(w.capitalize() if w.islower() else w.title() for w in s.split())


def _kv_table(d: dict[str, Any] | None, *, label: str = "Metric") -> str:
    if not d:
        return '<div class="empty">No data.</div>'
    rows = "".join(
        f'<tr><th style="text-align:left;padding:10px 14px;'
        f'border-bottom:1px solid #f1f5f9;background:#fafafa;'
        f'font-weight:500;color:#334155;width:55%;">'
        f"{_html_escape(_humanize(str(k)))}</th>"
        f'<td class="num" style="text-align:right;padding:10px 14px;'
        f'border-bottom:1px solid #f1f5f9;font-variant-numeric:tabular-nums;'
        f'color:#111827;font-weight:600;">'
        f"{_html_escape(_fmt_num(v, key=str(k)))}</td></tr>"
        for k, v in d.items()
    )
    return (
        '<table style="border-collapse:separate;border-spacing:0;width:100%;'
        'font-size:13px;background:#ffffff;border:1px solid #e5e7eb;'
        'border-radius:8px;overflow:hidden;">'
        f"<thead><tr>"
        f'<th style="text-align:left;padding:10px 14px;background:#f8fafc;'
        f'font-weight:600;color:#334155;font-size:11px;text-transform:uppercase;'
        f'letter-spacing:0.06em;border-bottom:1px solid #e5e7eb;">'
        f"{_html_escape(label)}</th>"
        f'<th class="num" style="text-align:right;padding:10px 14px;'
        f'background:#f8fafc;font-weight:600;color:#334155;font-size:11px;'
        f'text-transform:uppercase;letter-spacing:0.06em;'
        f'border-bottom:1px solid #e5e7eb;">Value</th>'
        f"</tr></thead><tbody>{rows}</tbody></table>"
    )


def _truncate(s: str, n: int) -> str:
    """Trim a long message for the example-bullet display, keeping it
    readable on the email client's narrow column."""
    s = (s or "").strip().replace("\n", " ")
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _severity_pill(sev: str) -> str:
    s = sev.lower()
    cls = s if s in {"error", "warning", "info"} else "unknown"
    palette = {
        "error":   ("#fee2e2", "#991b1b"),
        "warning": ("#fef3c7", "#92400e"),
        "info":    ("#dbeafe", "#1e40af"),
        "unknown": ("#e5e7eb", "#374151"),
    }
    bg, fg = palette[cls]
    return (
        f'<span class="pill {cls}" style="display:inline-block;'
        f"padding:3px 10px;border-radius:999px;font-size:11px;"
        f"font-weight:600;letter-spacing:0.02em;text-transform:uppercase;"
        f'background:{bg};color:{fg};">{_html_escape(sev)}</span>'
    )


def _section_title(text: str) -> str:
    return (
        f'<h2 style="font-size:13px;font-weight:600;color:#475569;'
        f"margin:28px 0 10px;text-transform:uppercase;"
        f'letter-spacing:0.08em;font-family:{_FONT_STACK};">'
        f"{_html_escape(text)}</h2>"
    )


def render_html(
    run_date: str,
    *,
    report: RunReport | None,
    run_summary: "RunSummaryDTO",
    last_runs: "list[RunSummaryDTO]",
    daily_diff: "DailyDiffDTO",
    top_codes: list[tuple[str, int]],
    issue_examples: dict[str, list[dict[str, str]]] | None = None,
    distinct_union_3: int | None = None,
    distinct_union_2: int | None = None,
) -> str:
    """Render the email body. Each section is fed by a Python dataclass
    ported from the TS service layer (``services/diff_calculator.py``)
    so totals and deltas match what the dashboard shows."""
    examples = issue_examples or {}

    # ── Run totals (RunSummary DTO from diff_calculator) ─────────────────
    rent_pct = (
        run_summary.units_with_rent / run_summary.units_extracted * 100
        if run_summary.units_extracted
        else 0.0
    )
    avail_pct = (
        run_summary.units_with_availability / run_summary.units_extracted * 100
        if run_summary.units_extracted
        else 0.0
    )
    totals_dict: dict[str, Any] = {
        "Properties": run_summary.total_properties,
        "Succeeded": run_summary.succeeded,
        "Failed": run_summary.failed,
        "Success rate": run_summary.success_rate,
        "Units extracted": run_summary.units_extracted,
        "Units with rent": (
            f"{run_summary.units_with_rent:,} ({rent_pct:.1f}%)"
            if run_summary.units_extracted
            else f"{run_summary.units_with_rent:,}"
        ),
        "Units with availability": (
            f"{run_summary.units_with_availability:,} ({avail_pct:.1f}%)"
            if run_summary.units_extracted
            else f"{run_summary.units_with_availability:,}"
        ),
    }
    totals_html = _kv_table(totals_dict, label="Metric")

    # ── Daily diff vs previous run (DailyDiff DTO) ───────────────────────
    diff_summary_dict: dict[str, Any] = {
        "Rents up": daily_diff.summary.rents_increased,
        "Rents down": daily_diff.summary.rents_decreased,
        "Newly available units": daily_diff.summary.new_available,
        "Disappeared units": daily_diff.summary.disappeared_units,
        "Became leased": daily_diff.summary.became_leased,
        "New concessions": daily_diff.summary.new_concessions,
    }
    diff_html = _kv_table(diff_summary_dict, label="Change vs " + daily_diff.previous_date)

    # ── Last 3 runs (list of RunSummary) ─────────────────────────────────
    if last_runs:
        rows = "".join(
            "<tr>"
            f'<td style="padding:10px 14px;border-bottom:1px solid #f1f5f9;color:#0f172a;font-weight:500;">'
            f'<span class="mono" style="font-family:{_MONO_STACK};font-size:12px;'
            f'color:#1e3a8a;background:#eff6ff;padding:2px 6px;border-radius:4px;">'
            f"{_html_escape(rs.date)}</span></td>"
            f'<td class="num" style="text-align:right;padding:10px 14px;'
            f"border-bottom:1px solid #f1f5f9;font-variant-numeric:tabular-nums;"
            f'color:#111827;font-weight:600;">{rs.total_properties:,}</td>'
            f'<td class="num" style="text-align:right;padding:10px 14px;'
            f"border-bottom:1px solid #f1f5f9;font-variant-numeric:tabular-nums;"
            f'color:#111827;font-weight:600;">{rs.units_extracted:,}</td>'
            f'<td class="num" style="text-align:right;padding:10px 14px;'
            f"border-bottom:1px solid #f1f5f9;font-variant-numeric:tabular-nums;"
            f'color:#111827;font-weight:600;">{rs.success_rate * 100:.1f}%</td>'
            "</tr>"
            for rs in last_runs
        )

        # Footer rows: distinct (canonical_id, unit_id) seen in the union
        # of the most recent N runs. Two windows so the user can see how
        # much new ground each extra run is covering.
        footer_rows = ""
        if distinct_union_2 is not None:
            footer_rows += (
                '<tr style="background:#f8fafc;">'
                '<td colspan="3" style="padding:10px 14px;color:#475569;'
                'font-style:italic;font-size:12px;'
                'border-top:1px solid #e5e7eb;border-bottom:0;">'
                'Units present in <strong>all of the last 2 runs</strong> '
                '(same property + bed/bath/sqft)</td>'
                f'<td class="num" style="text-align:right;padding:10px 14px;'
                f"font-variant-numeric:tabular-nums;color:#0f172a;font-weight:600;"
                f'border-top:1px solid #e5e7eb;border-bottom:0;">'
                f"{distinct_union_2:,}</td></tr>"
            )
        if distinct_union_3 is not None:
            footer_rows += (
                '<tr style="background:#f8fafc;">'
                '<td colspan="3" style="padding:10px 14px;color:#475569;'
                'font-style:italic;font-size:12px;border-bottom:0;">'
                'Units present in <strong>all of the last 3 runs</strong> '
                '(same property + bed/bath/sqft)</td>'
                f'<td class="num" style="text-align:right;padding:10px 14px;'
                f"font-variant-numeric:tabular-nums;color:#0f172a;font-weight:600;"
                f'border-bottom:0;">{distinct_union_3:,}</td></tr>'
            )

        last_runs_html = (
            '<table style="border-collapse:separate;border-spacing:0;width:100%;'
            'font-size:13px;background:#ffffff;border:1px solid #e5e7eb;'
            'border-radius:8px;overflow:hidden;">'
            "<thead><tr>"
            '<th style="text-align:left;padding:10px 14px;background:#f8fafc;'
            'font-weight:600;color:#334155;font-size:11px;text-transform:uppercase;'
            'letter-spacing:0.06em;border-bottom:1px solid #e5e7eb;">Run date</th>'
            '<th class="num" style="text-align:right;padding:10px 14px;'
            'background:#f8fafc;font-weight:600;color:#334155;font-size:11px;'
            'text-transform:uppercase;letter-spacing:0.06em;'
            'border-bottom:1px solid #e5e7eb;">Properties</th>'
            '<th class="num" style="text-align:right;padding:10px 14px;'
            'background:#f8fafc;font-weight:600;color:#334155;font-size:11px;'
            'text-transform:uppercase;letter-spacing:0.06em;'
            'border-bottom:1px solid #e5e7eb;">Units extracted</th>'
            '<th class="num" style="text-align:right;padding:10px 14px;'
            'background:#f8fafc;font-weight:600;color:#334155;font-size:11px;'
            'text-transform:uppercase;letter-spacing:0.06em;'
            'border-bottom:1px solid #e5e7eb;">Success rate</th>'
            f"</tr></thead><tbody>{rows}{footer_rows}</tbody></table>"
        )
    else:
        last_runs_html = '<div class="empty">No prior runs recorded.</div>'

    # ── Tier distribution from RunReport.tier_distribution (clubbed) ─────
    if report and report.tier_distribution:
        tiers = _club_tier_distribution(report.tier_distribution)
        tiers_html = (
            _kv_table(tiers, label="Tier")
            if tiers
            else '<div class="empty">No tier data.</div>'
        )
    else:
        tiers_html = '<div class="empty">No tier data.</div>'

    body_blocks = [
        _section_title("Run totals"),
        totals_html,
        _section_title(f"Daily diff — {daily_diff.previous_date} → {run_date}"),
        diff_html,
        _section_title("Last 3 runs"),
        last_runs_html,
        _section_title("Extraction tier distribution"),
        tiers_html,
    ]

    # ── Top 10 codes (with example messages) ─────────────────────────────
    examples = issue_examples or {}
    if top_codes:
        max_count = max(c for _, c in top_codes) or 1
        code_rows: list[str] = []
        for code, count in top_codes:
            pct = (count / max_count) * 100
            bar = (
                f'<div style="background:#e5e7eb;height:6px;border-radius:3px;'
                f'overflow:hidden;width:120px;display:inline-block;'
                f'vertical-align:middle;margin-left:10px;">'
                f'<div style="background:#3b82f6;height:6px;width:{pct:.1f}%;"></div>'
                f"</div>"
            )
            # Top row — code + count + bar
            code_rows.append(
                f'<tr><td style="padding:10px 14px;border-bottom:0;vertical-align:top;">'
                f'<span class="mono" style="font-family:{_MONO_STACK};font-size:12px;'
                f'color:#1e3a8a;background:#eff6ff;padding:2px 6px;border-radius:4px;">'
                f"{_html_escape(code)}</span></td>"
                f'<td class="num" style="text-align:right;padding:10px 14px;'
                f"border-bottom:0;font-variant-numeric:tabular-nums;"
                f'color:#111827;font-weight:600;vertical-align:top;">{count:,}{bar}</td></tr>'
            )
            # Sub-row — example properties as hyperlinked names.
            raw_examples = examples.get(code, [])
            example_links: list[str] = []
            for ex in raw_examples[:2]:
                name = _truncate(ex.get("name") or "", 80) or "(unknown property)"
                url = ex.get("url") or ""
                if url:
                    example_links.append(
                        f'<a href="{_html_escape(url)}" '
                        f'style="color:#1e40af;text-decoration:none;font-weight:500;" '
                        f'target="_blank" rel="noopener noreferrer">'
                        f"{_html_escape(name)}</a>"
                    )
                else:
                    example_links.append(
                        f'<span style="color:#475569;font-weight:500;">'
                        f"{_html_escape(name)}</span>"
                    )
            if example_links:
                bullets = "".join(
                    f'<div style="margin-top:4px;font-size:12px;'
                    f"line-height:1.45;padding-left:10px;border-left:2px solid #e2e8f0;"
                    f'margin-left:2px;">'
                    f"{link}</div>"
                    for link in example_links
                )
                code_rows.append(
                    f'<tr><td colspan="2" style="padding:0 14px 10px 14px;'
                    f'border-bottom:1px solid #f1f5f9;background:#fafbfc;">'
                    f'<div style="font-size:10px;color:#94a3b8;'
                    f"font-weight:600;text-transform:uppercase;letter-spacing:0.06em;"
                    f'margin-top:6px;margin-bottom:2px;font-family:{_FONT_STACK};">Examples</div>'
                    f"{bullets}</td></tr>"
                )
            else:
                # Close out the row border when there are no examples.
                code_rows[-1] = code_rows[-1].replace(
                    "border-bottom:0;vertical-align:top;",
                    "border-bottom:1px solid #f1f5f9;vertical-align:top;",
                )

        code_html = (
            '<table style="border-collapse:separate;border-spacing:0;width:100%;'
            'font-size:13px;background:#ffffff;border:1px solid #e5e7eb;'
            'border-radius:8px;overflow:hidden;">'
            '<thead><tr>'
            '<th style="text-align:left;padding:10px 14px;background:#f8fafc;'
            'font-weight:600;color:#334155;font-size:11px;text-transform:uppercase;'
            'letter-spacing:0.06em;border-bottom:1px solid #e5e7eb;">Issue code</th>'
            '<th class="num" style="text-align:right;padding:10px 14px;'
            'background:#f8fafc;font-weight:600;color:#334155;font-size:11px;'
            'text-transform:uppercase;letter-spacing:0.06em;'
            'border-bottom:1px solid #e5e7eb;">Count</th>'
            f"</tr></thead><tbody>{''.join(code_rows)}</tbody></table>"
        )
    else:
        code_html = (
            '<div class="empty" style="color:#166534;font-style:normal;'
            "font-size:13px;padding:14px 16px;background:#f0fdf4;"
            'border:1px solid #bbf7d0;border-radius:8px;font-weight:500;">'
            "✓ No issues recorded for this run.</div>"
        )

    generated = (report.generated_at if report else "—") or "—"
    total_issues = sum(c for _, c in (top_codes or []))

    body = "\n".join(body_blocks)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Proppy — RealPage POC daily report</title>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>{_CSS}</style>
</head>
<body style="margin:0;padding:0;background:#f4f6fa;font-family:{_FONT_STACK};color:#1f2937;">
  <div class="wrap" style="max-width:720px;margin:0 auto;padding:24px 16px;">
    <div class="card" style="background:#ffffff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden;box-shadow:0 1px 2px rgba(15,23,42,0.04);">

      <!-- Header -->
      <div class="header" style="background:linear-gradient(135deg,#0f172a 0%,#1e3a8a 100%);padding:28px 32px;color:#ffffff;">
        <h1 class="brand" style="margin:0;font-size:22px;font-weight:700;letter-spacing:-0.01em;line-height:1.2;font-family:{_FONT_STACK};">
          Proppy <span class="accent" style="color:#60a5fa;font-weight:500;">— RealPage POC</span>
        </h1>
        <div class="tagline" style="font-size:12px;text-transform:uppercase;letter-spacing:0.12em;color:#cbd5e1;margin-top:6px;font-weight:500;font-family:{_FONT_STACK};">
          Daily Scrape Report
        </div>
      </div>

      <!-- Meta bar -->
      <div class="meta-bar" style="padding:18px 32px;background:#f8fafc;border-bottom:1px solid #e5e7eb;font-size:13px;color:#475569;font-family:{_FONT_STACK};">
        <div style="display:inline-block;margin-right:24px;">
          <span class="label" style="color:#94a3b8;font-weight:500;text-transform:uppercase;letter-spacing:0.06em;font-size:11px;margin-right:6px;">Run date</span>
          <strong style="color:#0f172a;font-weight:600;">{_html_escape(run_date)}</strong>
        </div>
        <div style="display:inline-block;margin-right:24px;">
          <span class="label" style="color:#94a3b8;font-weight:500;text-transform:uppercase;letter-spacing:0.06em;font-size:11px;margin-right:6px;">Generated</span>
          <strong style="color:#0f172a;font-weight:600;">{_html_escape(str(generated))}</strong>
        </div>
        <div style="display:inline-block;">
          <span class="label" style="color:#94a3b8;font-weight:500;text-transform:uppercase;letter-spacing:0.06em;font-size:11px;margin-right:6px;">Issues</span>
          <strong style="color:#0f172a;font-weight:600;">{total_issues:,}</strong>
        </div>
      </div>

      <!-- Content -->
      <div class="content" style="padding:8px 32px 32px 32px;">
        {body}
        {_section_title("Top 10 issue codes")}
        {code_html}
      </div>

      <!-- Footer -->
      <div class="footer" style="text-align:center;padding:20px 32px;color:#94a3b8;font-size:11px;border-top:1px solid #f1f5f9;background:#fafafa;font-family:{_FONT_STACK};">
        Sent automatically by <span class="mono" style="font-family:{_MONO_STACK};font-size:11px;color:#64748b;">ma_poc/scripts/email_daily_report.py</span>
      </div>

    </div>
  </div>
</body>
</html>
"""


# ── Gmail MCP send ───────────────────────────────────────────────────────────


async def _send_via_mcp(
    *,
    recipients: list[str],
    subject: str,
    html_body: str,
    sender_name: str | None,
) -> dict[str, Any]:
    """Spawn the Gmail MCP server over stdio and call its `send_email` tool."""
    # Lazy imports — keeps the rest of the script importable even if `mcp`
    # isn't installed yet (e.g. during initial setup).
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    cmd = os.getenv("GMAIL_MCP_COMMAND", "npx")
    args_raw = os.getenv("GMAIL_MCP_ARGS", "@gongrzhe/server-gmail-autoauth-mcp")
    args = [a for a in args_raw.split(" ") if a]

    params = StdioServerParameters(command=cmd, args=args, env=None)

    # The autoauth server's `send_email` tool accepts:
    #   to: list[str]       (required)
    #   subject: str        (required)
    #   body: str           (required — used as plain text)
    #   htmlBody: str       (optional — HTML alternative)
    #   mimeType: "text/plain" | "text/html" | "multipart/alternative"
    #   cc, bcc, attachments (optional, unused here)
    payload: dict[str, Any] = {
        "to": recipients,
        "subject": subject,
        "body": _html_to_plain(html_body),
        "htmlBody": html_body,
        "mimeType": "multipart/alternative",
    }
    if sender_name:
        # Some builds of the server honour a `from` display-name override.
        # If the running version ignores it, the Gmail account's default
        # display name is used — harmless either way.
        payload["from"] = sender_name

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            tool_names = {t.name for t in tools.tools}
            if "send_email" not in tool_names:
                raise RuntimeError(
                    f"Gmail MCP server did not expose send_email. "
                    f"Tools available: {sorted(tool_names)}"
                )
            result = await session.call_tool("send_email", payload)

    # ToolCallResult → flatten into something printable.
    out: dict[str, Any] = {"isError": bool(getattr(result, "isError", False))}
    contents: list[str] = []
    for c in getattr(result, "content", []) or []:
        text = getattr(c, "text", None)
        if text:
            contents.append(text)
    out["content"] = contents
    return out


def _html_to_plain(html: str) -> str:
    """Crude HTML → text fallback for mail clients that ignore the HTML part.
    Good enough for the daily-report shape; we don't need a full parser."""
    import re

    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.S | re.I)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</(p|h[1-6]|tr|li)>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Entrypoint ───────────────────────────────────────────────────────────────


def _parse_recipients(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [r.strip() for r in raw.split(",") if r.strip()]


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # Load ma_poc/.env even when the script is launched from elsewhere
    # (Task Scheduler runs with %CD% = system32 unless we set it).
    load_dotenv(_MA_POC_ROOT / ".env")

    parser = argparse.ArgumentParser(description="Email PropAi daily scrape report.")
    parser.add_argument(
        "--run-date",
        default=None,
        help="YYYY-MM-DD. Defaults to today (UTC), falling back to the latest run.",
    )
    parser.add_argument(
        "--recipients",
        default=None,
        help="Comma-separated override. Defaults to REPORT_RECIPIENTS env var.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Render HTML to stdout and skip the Gmail MCP send.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help=(
            "Override DATABASE_URL. asyncpg URLs are auto-translated to "
            "pg8000 (the local ma_poc/.env ships +asyncpg for the TS API; "
            "this script needs the sync driver)."
        ),
    )
    args = parser.parse_args(argv)

    recipients = _parse_recipients(args.recipients) or _parse_recipients(
        os.getenv("REPORT_RECIPIENTS")
    )
    if not recipients and not args.dry_run:
        log.error("No recipients. Set REPORT_RECIPIENTS in .env or pass --recipients.")
        return 1

    sender_name = os.getenv("REPORT_SENDER_NAME") or "PropAi Daily Reports"

    # ── Data assembly ──
    # Engine is built locally so the cloud-sql-proxy fallback works on
    # Windows ARM64; once it's open every read goes through either the
    # data-provider DTOs (run reports, issues) or the diff_calculator
    # module — which is the Python port of services/src/services/
    # DiffService.ts + RunService.ts. Same source of truth as the UI,
    # no Node/HTTP hop.
    from sqlalchemy.orm import Session

    provider = _open_provider(args.database_url)
    engine = provider.engine
    log.info("Data provider: %s", provider.name)
    try:
        run_date = _resolve_run_date(provider, args.run_date)
        report = provider.runs.read_report(run_date)
        top_codes = _summarize_issue_codes(provider.runs.read_issues(run_date), top_n=10)

        # Run summaries + daily diff — ported from DiffService.ts.
        run_dates_asc = provider.runs.list_runs()
        prev_date = previous_run_date(run_dates_asc, run_date)
        last_n_dates = run_dates_asc[-3:]
        last_2_dates = run_dates_asc[-2:]
        with Session(engine, future=True) as s:
            run_summary = compute_run_summary(s, run_date)
            last_runs = compute_run_history(s, last_n_dates)
            daily_diff = compute_daily_diff(s, run_date, prev_date)
            common_3 = compute_units_seen_in_all_runs(s, last_n_dates)
            common_2 = compute_units_seen_in_all_runs(s, last_2_dates)

            # Issue example enrichment: cid → {name, url} via property_state.
            top_code_keys = [code for code, _ in top_codes]
            example_cids = _load_issue_example_cids(provider, run_date, top_code_keys, k=2)
            all_cids: list[str] = [c for cids in example_cids.values() for c in cids]
            cid_info = lookup_property_info(s, all_cids)
        issue_examples: dict[str, list[dict[str, str]]] = {
            code: [
                {"cid": cid, **cid_info.get(cid, {"name": cid, "url": ""})}
                for cid in cids
            ]
            for code, cids in example_cids.items()
        }
    finally:
        provider.close()

    log.info(
        "Loaded run_date=%s prev_date=%s properties=%s "
        "units[total=%s rent=%s avail=%s] "
        "diff[up=%s down=%s new=%s gone=%s]",
        run_date,
        prev_date,
        run_summary.total_properties,
        run_summary.units_extracted,
        run_summary.units_with_rent,
        run_summary.units_with_availability,
        daily_diff.summary.rents_increased,
        daily_diff.summary.rents_decreased,
        daily_diff.summary.new_available,
        daily_diff.summary.disappeared_units,
    )

    html = render_html(
        run_date,
        report=report,
        run_summary=run_summary,
        last_runs=last_runs,
        daily_diff=daily_diff,
        top_codes=top_codes,
        issue_examples=issue_examples,
        distinct_union_3=common_3,
        distinct_union_2=common_2,
    )
    subject = f"Proppy daily report — {run_date}"

    if args.dry_run:
        sys.stdout.write(html)
        sys.stdout.write("\n")
        log.info("Dry run — would send to: %s", ", ".join(recipients) or "(none)")
        return 0

    try:
        result = asyncio.run(
            _send_via_mcp(
                recipients=recipients,
                subject=subject,
                html_body=html,
                sender_name=sender_name,
            )
        )
    except Exception as exc:
        log.exception("Gmail MCP send failed: %s", exc)
        return 1

    if result.get("isError"):
        log.error("Gmail MCP returned an error: %s", result.get("content"))
        return 1

    log.info(
        "Sent daily report for %s to %d recipient(s). MCP response: %s",
        run_date,
        len(recipients),
        result.get("content") or "(empty)",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())