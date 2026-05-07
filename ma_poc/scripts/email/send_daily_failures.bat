@echo off
rem ============================================================================
rem  send_daily_failures_report.bat — daily PropAi failures email + 2 XLSX attachments.
rem
rem  What it does
rem  ------------
rem  Wraps ``ma_poc/scripts/email_daily_failures_report.py`` so Windows Task
rem  Scheduler can fire it without worrying about venv activation, working
rem  directory, or argument escaping. Reads .env via load_dotenv inside the
rem  Python script — no need to set anything in this file.
rem
rem  Data source
rem  -----------
rem  Whatever ``ma_poc/.env`` points at:
rem    * Default (DATABASE_URL → local proppy):
rem        the report runs against the locally-mirrored copy of Cloud SQL.
rem        Pair with a daily ``sync_cloud_to_local.py`` run (see the
rem        --with-sync section below) so today's run shows up.
rem    * Cloud SQL direct (uncomment CLOUD_SQL_INSTANCE in .env):
rem        the report queries Cloud SQL via cloud-sql-proxy. No mirror needed.
rem        Recommended for production scheduling — no 45-min mirror step.
rem
rem  Scheduling
rem  ----------
rem  ADD a daily 8:30 AM job (run as the user, NOT elevated):
rem
rem    schtasks /Create ^
rem        /TN "PropAi Daily Failures Report" ^
rem        /TR "C:\Users\ashus\OneDrive\Documents\Code\PropAi\ma_poc\scripts\send_daily_failures_report.bat" ^
rem        /SC DAILY /ST 08:30 ^
rem        /F
rem
rem  RUN it now (good for verifying):
rem    schtasks /Run /TN "PropAi Daily Failures Report"
rem
rem  REMOVE:
rem    schtasks /Delete /TN "PropAi Daily Failures Report" /F
rem
rem  --with-sync — mirror Cloud SQL → local first, then send the email
rem  ----------------------------------------------------------------
rem  Run with the --with-sync flag (or set WITH_SYNC=1) when the local DB
rem  is the data source. The mirror runs first; if it fails the email is
rem  skipped and the script exits non-zero so Task Scheduler reports the
rem  failure rather than silently sending a stale report.
rem
rem      send_daily_failures_report.bat --with-sync
rem
rem  All other args are forwarded to email_daily_failures_report.py:
rem      send_daily_failures_report.bat --run-date 2026-05-07
rem      send_daily_failures_report.bat --dry-run
rem      send_daily_failures_report.bat --recipients a@x.com,b@y.com
rem ============================================================================

setlocal enableextensions

set "MA_POC_ROOT=%~dp0.."
set "PY=%MA_POC_ROOT%\.venv\Scripts\python.exe"

if not exist "%PY%" (
    echo [send_daily_failures_report] Python venv not found at %PY% 1>&2
    exit /b 2
)

rem Parse --with-sync without consuming other args. Anything we don't
rem recognise is forwarded verbatim to the Python script.
set "RUN_SYNC="
if /I "%WITH_SYNC%"=="1" set "RUN_SYNC=1"
set "FORWARD_ARGS="
:parse
if "%~1"=="" goto parsed
if /I "%~1"=="--with-sync" (
    set "RUN_SYNC=1"
    shift
    goto parse
)
set "FORWARD_ARGS=%FORWARD_ARGS% %1"
shift
goto parse
:parsed

if defined RUN_SYNC (
    echo [send_daily_failures_report] Mirroring Cloud SQL ^-^> local first ^(--with-sync^)
    "%PY%" "%MA_POC_ROOT%\scripts\sync_cloud_to_local.py"
    if errorlevel 1 (
        echo [send_daily_failures_report] sync_cloud_to_local.py failed; aborting send 1>&2
        exit /b 3
    )
)

echo [send_daily_failures_report] Sending failures report ...
"%PY%" -m scripts.email.daily_failures%FORWARD_ARGS%
exit /b %errorlevel%
