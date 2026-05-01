import { useState } from "react";

const SEVERITY = {
  blocker: { label: "BLOCKER", color: "#ef4444", bg: "#1a0000", border: "#ef444433" },
  high:    { label: "HIGH",    color: "#f97316", bg: "#1a0800", border: "#f9731633" },
  medium:  { label: "MEDIUM",  color: "#eab308", bg: "#1a1400", border: "#eab30833" },
  good:    { label: "✓ GOOD",  color: "#22c55e", bg: "#001a07", border: "#22c55e33" },
} as const;

type SeverityKey = keyof typeof SEVERITY;

interface Finding {
  id: string;
  severity: SeverityKey;
  file: string;
  title: string;
  description: string;
  impact?: string;
  fix?: string;
}

const findings: Finding[] = [
  // ── BLOCKERS ─────────────────────────────────────────────────────────────
  {
    id: "B1",
    severity: "blocker",
    file: "docker/docker-compose.yml",
    title: "Duplicate top-level `networks:` and `volumes:` keys — YAML silent overwrite",
    description: `The diff appends a second \`networks:\` and \`volumes:\` block at the top level. YAML does NOT merge duplicate keys — the second block silently replaces the first. The existing network bindings for postgres, kafka, redis, etc. are overwritten with only \`paperbull-net\`. Every service that was on the old network loses connectivity on next \`docker compose up\`. The \`pgdata:\` and \`whisper_data:\` volumes are re-declared (so not lost), but any network config on the original block is destroyed.`,
    impact: "On next deploy, all non-openalgo services lose their network. Postgres unreachable → entire stack down.",
    fix: `# WRONG — adds new top-level block
networks:
  paperbull-net:
    driver: bridge
volumes:
  pgdata:
  whisper_data:
  openalgo_data:

# CORRECT — add only openalgo_data to existing volumes block,
# and add openalgo to existing paperbull-net (if it already existed)
# or add paperbull-net: to the existing networks block.
# Confirm the existing network name first with: docker network ls`,
  },
  {
    id: "B2",
    severity: "blocker",
    file: "docker/docker-compose.yml",
    title: "SQLite path doesn't reach the mounted volume",
    description: `\`DATABASE_URL: sqlite:///openalgo.db\` — three slashes = relative path, resolves to the container's working directory (typically \`/app/openalgo.db\`). The volume is mounted at \`/app/db\`. So the database is written to the container's ephemeral layer, NOT to the persistent volume. Every container restart wipes the OpenAlgo database — TOTP sessions, order history, all gone.`,
    impact: "OpenAlgo loses its entire database on every restart. Re-auth required manually each time.",
    fix: `DATABASE_URL: sqlite:////app/db/openalgo.db
# Four slashes = absolute path inside container: /app/db/openalgo.db
# This matches the volume mount: openalgo_data:/app/db`,
  },
  {
    id: "B3",
    severity: "blocker",
    file: "src/integrations/nautilus/backtester.py",
    title: "`run_signal_backtest` adds NO strategy to the engine — always returns zeros",
    description: `The function creates a BacktestEngine, adds the NSE venue, the instrument, and the OHLCV bars — then calls \`engine.run()\`. But it never adds a Strategy or Actor that reads \`signal_history\` and places orders. NautilusTrader runs an empty engine with no orders. \`engine.get_stats_pnls_formatted()\` returns an empty dict, so every metric gets the \`.get(..., 0)\` fallback. The analyst scorer writes zeros for every analyst. Backtest scoring is completely broken — the Sunday cron updates every analyst to 0% win rate and 0% return, silently.`,
    impact: "Analyst scoreboard corruption every Sunday. All analysts scored identically at zero. Signal filtering breaks.",
    fix: `A strategy class must be added that reads signal_history and generates orders.
At minimum, add a SimpleSignalStrategy actor:

class SignalReplayActor(Actor):
    def __init__(self, signal_history: list[dict], instrument_id):
        super().__init__()
        self._signals = signal_history
        self._instrument_id = instrument_id

    def on_start(self):
        for sig in self._signals:
            # Convert signal to a market order at signal date
            order = self.order_factory.market(
                instrument_id=self._instrument_id,
                order_side=OrderSide.BUY if sig["direction"] == "BUY" else OrderSide.SELL,
                quantity=Quantity.from_int(1),
            )
            self.submit_order(order)

engine.add_actor(SignalReplayActor(signal_history, instrument.id))

# Also: signal_history parameter is currently unused — dead code.`,
  },
  {
    id: "B4",
    severity: "blocker",
    file: "requirements/oss-integrations.txt",
    title: "`langgraph>=0.1,<0.2` — upper bound blocks installation",
    description: `LangGraph's current release is 0.2.x (0.2.60+ as of early 2026). The \`<0.2\` upper bound means pip will refuse to install any available version. TradingAgents 0.2.4 requires langgraph 0.2+. This means \`pip install -r requirements/oss-integrations.txt\` will fail with a dependency conflict on a fresh install. The integration never runs.`,
    impact: "TradingAgents import fails on every fresh environment. Multi-agent debate is dead on arrival.",
    fix: `# WRONG
langgraph>=0.1,<0.2

# CORRECT — langgraph is at 0.2.x, allow through 0.x line
langgraph>=0.2,<1.0`,
  },
  {
    id: "B5",
    severity: "blocker",
    file: "src/integrations/freqai_inspired/feature_pipeline.py",
    title: "NaN VIX doesn't trigger `high_vol` gate — silently allows trading in bad data",
    description: `In \`classify_regime()\`, the first check is \`if vix > 20: return "high_vol"\`. If \`india_vix\` is NaN (VIX feed unavailable, connection timeout), the comparison \`NaN > 20\` evaluates to False in Python. Execution falls through to "sideways", which is in \`ALLOWED_REGIMES\` in \`regime_gate.py\`. The system continues paper trading with garbage regime classification instead of blocking. This is the opposite of fail-safe.`,
    impact: "VIX feed outage → system silently classifies as 'sideways' → trades placed without valid regime check.",
    fix: `def classify_regime(features: FeatureSet) -> Regime:
    import math
    # Fail-safe: treat NaN/missing as high_vol (block trading)
    if math.isnan(features.india_vix) or math.isnan(features.rsi_14):
        return "high_vol"

    vix = features.india_vix
    if vix > 20:
        return "high_vol"
    # ... rest unchanged`,
  },

  // ── HIGH ─────────────────────────────────────────────────────────────────
  {
    id: "H1",
    severity: "high",
    file: "src/integrations/nautilus/backtester.py",
    title: "`RuntimeError` on missing nautilus crashes the Sunday cron — no recovery",
    description: `When nautilus is not installed, \`run_signal_backtest\` raises \`RuntimeError("nautilus_trader is not installed...")\`. The caller in \`analyst_scorer.py\` (a Sunday cron job) likely doesn't wrap this in a try/except, so the entire scoring run crashes with no Telegram alert. The analyst scoreboard is left unupdated indefinitely until the next Sunday — or forever if the install is never fixed.`,
    impact: "Silent Sunday scoring failure. No alert. Analyst scores stale indefinitely.",
    fix: `# In run_signal_backtest — return graceful fallback instead of raise:
if not _NAUTILUS_AVAILABLE:
    logger.warning("nautilus_trader unavailable — returning null backtest result")
    return {
        "ticker": ticker, "total_return_pct": None, "sharpe_ratio": None,
        "max_drawdown_pct": None, "win_rate": None, "total_trades": 0,
        "profit_factor": None, "error": "nautilus_trader not installed",
    }

# In analyst_scorer.py — also add outer try/except:
async def compute_analyst_backtest_score(analyst_id: str, ...):
    try:
        ...
    except Exception as e:
        logger.error(f"Backtest scoring failed for {analyst_id}: {e}")
        await telegram.send(f"⚠️ Sunday scorer failed: {analyst_id} — {e}")`,
  },
  {
    id: "H2",
    severity: "high",
    file: "src/integrations/freqai_inspired/feature_pipeline.py",
    title: "No input validation — DataFrame with <14/20 rows produces silent NaN features",
    description: `\`extract_features()\` requires at least 20 rows (for BB and vol_ratio rolling window). With fewer rows: vol_ratio = NaN (rolling(20) on <20 rows), RSI = NaN (<15 rows), ATR = NaN (<14 rows). These NaN values store into FeatureSet and propagate to classify_regime silently. The function never raises — callers have no idea the feature extraction failed. Since regime gate defaults to "sideways" on NaN (see B5), the system trades on corrupt data.`,
    impact: "On startup with thin historical data (new instrument, market holiday gap), regime classification is invalid.",
    fix: `def extract_features(ohlcv: pd.DataFrame, vix: float, pcr: float) -> FeatureSet:
    MIN_ROWS = 26  # MACD needs 26 + signal 9 = 35 ideally, but 26 minimum
    if len(ohlcv) < MIN_ROWS:
        raise ValueError(
            f"extract_features requires at least {MIN_ROWS} rows, got {len(ohlcv)}. "
            "Ensure sufficient historical data before calling."
        )
    if ohlcv[["open","high","low","close","volume"]].isnull().any().any():
        raise ValueError("OHLCV contains NaN values — clean data before feature extraction.")
    ...`,
  },
  {
    id: "H3",
    severity: "high",
    file: "src/integrations/nautilus/backtester.py",
    title: "No try/except around `engine.run()` — uncaught engine exceptions crash cron",
    description: `\`engine.run()\` can raise for many reasons: unsorted bar timestamps, invalid price precision, venue/instrument mismatch, memory exhaustion on large datasets. With no exception handling, any engine error propagates to the cron job. The Sunday scoring run for ALL analysts aborts on the first failure.`,
    impact: "One bad OHLCV dataset for one ticker kills the entire Sunday scoring batch.",
    fix: `try:
    engine.run()
    stats = engine.get_stats_pnls_formatted()
except Exception as e:
    logger.error(f"NautilusTrader engine.run() failed for {ticker}: {e}")
    return {
        "ticker": ticker, "total_return_pct": None, "win_rate": None,
        "total_trades": 0, "error": str(e),
    }`,
  },
  {
    id: "H4",
    severity: "high",
    file: "docker/docker-compose.yml",
    title: "Missing `start_period` — OpenAlgo flagged unhealthy before TOTP auth completes",
    description: `OpenAlgo needs ~20–30s to initialize: Flask startup, TOTP broker authentication with Angel One, WebSocket handshake. The healthcheck fires at \`interval: 30s\` with no \`start_period\`. Docker may restart the container before it's ready, creating a restart loop. Angel One rate-limits repeated auth attempts, so after a few restarts, the API key can get temporarily blocked.`,
    impact: "Cold-start restart loop. Angel One auth rate-limit triggers after 3–5 rapid restarts.",
    fix: `healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:5000/api/v1/health"]
  interval: 30s
  timeout: 10s
  retries: 3
  start_period: 60s    # ← add this. Give OpenAlgo 60s before first check.`,
  },
  {
    id: "H5",
    severity: "high",
    file: "src/integrations/freqai_inspired/feature_pipeline.py",
    title: "No try/except in `extract_features` — KeyError on missing column crashes regime gate",
    description: `If the caller passes an OHLCV DataFrame missing a column (e.g., "volume" absent on index data, or column named "vol" instead of "volume"), \`ohlcv["volume"]\` raises a KeyError. This exception propagates all the way up through \`regime_gate.is_regime_tradeable()\` to whoever calls it — likely the Laabh F&O entry logic. Uncaught KeyError in the entry gate = unhandled exception in the strategy loop.`,
    impact: "Missing column in OHLCV data (common on index data sources) crashes F&O entry logic.",
    fix: `REQUIRED_COLS = {"open", "high", "low", "close", "volume"}

def extract_features(ohlcv: pd.DataFrame, vix: float, pcr: float) -> FeatureSet:
    missing = REQUIRED_COLS - set(ohlcv.columns)
    if missing:
        raise ValueError(f"OHLCV DataFrame missing required columns: {missing}")
    ...`,
  },

  // ── MEDIUM ───────────────────────────────────────────────────────────────
  {
    id: "M1",
    severity: "medium",
    file: "src/integrations/nautilus/backtester.py",
    title: "`Price(row[\"open\"], precision=2)` — float may fail NautilusTrader constructor",
    description: `NautilusTrader's \`Price\` constructor in recent versions (1.200+) expects a \`Decimal\` or string, not a Python float. Passing a float like \`2456.75\` may raise a \`TypeError\` depending on the nautilus build. The original template explicitly used \`Price(Decimal("0.05"), precision=2)\` in the instrument definition, but \`_df_to_bars\` passes raw floats. This will silently work in some builds and break in others.`,
    impact: "Inconsistent failures during backtesting depending on nautilus_trader version.",
    fix: `from decimal import Decimal

def _df_to_bars(df: pd.DataFrame, instrument_id) -> list:
    bars = []
    bar_type = BarType(...)
    for _, row in df.iterrows():
        bar = Bar(
            bar_type=bar_type,
            open=Price(Decimal(str(row["open"])), precision=2),   # ← Decimal
            high=Price(Decimal(str(row["high"])), precision=2),
            low=Price(Decimal(str(row["low"])), precision=2),
            close=Price(Decimal(str(row["close"])), precision=2),
            volume=Quantity(int(row["volume"]), precision=0),      # ← int
            ...
        )`,
  },
  {
    id: "M2",
    severity: "medium",
    file: "src/integrations/nautilus/backtester.py",
    title: "`_df_to_bars` raises on NaT date values with no error message",
    description: `\`pd.Timestamp(row["date"]).timestamp()\` raises \`ValueError: NaT\` if the date column contains NaT (common when joining OHLCV with signal tables on missing trading days). The exception message is cryptic and gives no indication which ticker or date caused it.`,
    impact: "NaT dates (trading holidays, missing days) crash bar construction silently.",
    fix: `for idx, row in df.iterrows():
    ts = pd.Timestamp(row["date"])
    if pd.isna(ts):
        logger.warning(f"Skipping NaT row at index {idx} for {ticker}")
        continue
    ts_ns = int(ts.timestamp() * 1e9)
    bar = Bar(bar_type=bar_type, ..., ts_event=ts_ns, ts_init=ts_ns)
    bars.append(bar)`,
  },
  {
    id: "M3",
    severity: "medium",
    file: "src/integrations/freqai_inspired/feature_pipeline.py",
    title: "VWAP is computed as session cumsum — meaningless on multi-day OHLCV",
    description: `VWAP uses \`(typical * volume).cumsum() / volume.cumsum()\`. On a daily OHLCV DataFrame with 90 rows, this computes a 90-day cumulative VWAP since inception, not the intraday or rolling VWAP. The "deviation from VWAP" feature is essentially price vs 90-day cost-basis average, which is a valid feature but NOT what a technical trader means by VWAP deviation. It should be a rolling VWAP (20-day) or explicitly labelled as "cost basis deviation".`,
    impact: "Misleading feature name. The regime classifier uses a mislabelled signal.",
    fix: `# For daily data, use rolling 20-day VWAP instead of session cumsum:
rolling_window = 20
typical = (high + low + close) / 3
rolling_typical_vol = (typical * volume).rolling(rolling_window).sum()
rolling_vol = volume.rolling(rolling_window).sum()
vwap = rolling_typical_vol / rolling_vol.replace(0, np.nan)
vwap_dev = ((close.iloc[-1] - vwap.iloc[-1]) / vwap.iloc[-1]) * 100`,
  },
  {
    id: "M4",
    severity: "medium",
    file: "docker/docker-compose.yml",
    title: "Port 5000 bound to all interfaces — collision risk with other local Flask services",
    description: `\`- "5000:5000"\` binds the REST API to all host interfaces (0.0.0.0:5000). If any other service (another Flask dev server, local dev environment) is running on 5000, the binding fails silently or binds incorrectly. Since this is a personal machine, the collision risk is real. Also exposes OpenAlgo API to the local network without authentication beyond the API key.`,
    impact: "Port collision on dev machine silently fails OpenAlgo bind. System appears healthy but routes to wrong service.",
    fix: `ports:
  - "127.0.0.1:5000:5000"   # restrict to localhost only
  - "127.0.0.1:8765:8765"   # same for ZMQ`,
  },
  {
    id: "M5",
    severity: "medium",
    file: "Makefile",
    title: "`test-integration-all` runs integration tests BEFORE unit tests — wrong order",
    description: `The target runs: \`pytest tests/ -m integration -v\` then \`pytest tests/ -m "not integration"\`. Integration tests (requiring live services) run first. If they fail due to missing env vars or services, the unit test run is still executed but the output is cluttered. Standard convention is unit tests first (fast feedback), integration second.`,
    impact: "Slower CI feedback loop. Integration failures obscure unit test results.",
    fix: `test-integration-all:
\tpytest tests/ -m "not integration" -v --tb=short   # unit first
\tpytest tests/ -m integration -v --tb=short          # integration second`,
  },

  // ── GOOD ─────────────────────────────────────────────────────────────────
  {
    id: "G1",
    severity: "good",
    file: "src/integrations/nautilus/backtester.py",
    title: "`try/except ImportError` guard with `_NAUTILUS_AVAILABLE` flag",
    description: "Excellent defensive pattern. The entire nautilus import block is wrapped in a try/except, with a module-level flag. The health() function correctly reports 'down' when nautilus isn't installed. This means the integration degrades gracefully — the rest of the stack keeps running even if nautilus_trader fails to install (e.g., Rust toolchain missing on VPS).",
  },
  {
    id: "G2",
    severity: "good",
    file: "src/integrations/freqai_inspired/feature_pipeline.py",
    title: "Bollinger Band position uses `+1e-9` guard against zero-width bands",
    description: "The `(upper - lower + 1e-9)` denominator guard prevents division by zero when price is at a low-volatility equilibrium (upper == lower). This is a well-known edge case in BB calculations that many implementations miss. The guard is correct.",
  },
  {
    id: "G3",
    severity: "good",
    file: "src/integrations/freqai_inspired/feature_pipeline.py",
    title: "RSI loss denominator uses `.replace(0, np.nan)` — correct NaN propagation",
    description: "The RSI loss calculation replaces zero-loss periods with NaN before division, causing RSI to become NaN for all-up-day periods rather than returning inf or raising ZeroDivisionError. This is the correct pattern. NaN RSI then propagates safely through classify_regime's comparisons (all return False → 'sideways').",
  },
  {
    id: "G4",
    severity: "good",
    file: "requirements/oss-integrations.txt",
    title: "`pyzmq>=25.0,<27.0` correctly added (not in original template)",
    description: "The ZMQ async subscription in openalgo/client.py requires pyzmq. This was missing from the original CLAUDE-OSS-INTEGRATIONS.md template. Claude Code correctly identified the missing dependency and added it. This shows the implementation went deeper than the template.",
  },
  {
    id: "G5",
    severity: "good",
    file: "docker/docker-compose.yml",
    title: "`restart: unless-stopped` is the correct policy for OpenAlgo",
    description: "Using `unless-stopped` instead of `always` means OpenAlgo won't auto-restart if you manually stop it (e.g., for maintenance, TOTP re-auth). `always` would immediately restart it, making manual intervention harder. Correct choice.",
  },
];

const files = [...new Set(findings.map((f) => f.file))];

export function CodeReviewPage() {
  const [selected, setSelected] = useState<string | null>(null);
  const [filterFile, setFilterFile] = useState("All");
  const [filterSev, setFilterSev] = useState("All");

  const counts = {
    blocker: findings.filter((f) => f.severity === "blocker").length,
    high:    findings.filter((f) => f.severity === "high").length,
    medium:  findings.filter((f) => f.severity === "medium").length,
    good:    findings.filter((f) => f.severity === "good").length,
  };

  const filtered = findings.filter(
    (f) =>
      (filterFile === "All" || f.file === filterFile) &&
      (filterSev === "All" || f.severity === filterSev)
  );

  return (
    <div
      style={{
        minHeight: "100vh",
        background: "#080810",
        fontFamily: "'JetBrains Mono', 'Fira Mono', monospace",
        color: "#e2e8f0",
        fontSize: 13,
      }}
    >
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500&family=Syne:wght@700;800&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { width: 3px; background: #0a0a0a; }
        ::-webkit-scrollbar-thumb { background: #222; }
        .cr-card { border: 1px solid #1a1a2a; transition: all 0.15s; cursor: pointer; }
        .cr-card:hover { border-color: #2a2a3e; }
        .cr-card.open { border-color: #334155; }
        .cr-pill { cursor: pointer; transition: all 0.1s; }
        .cr-pill:hover { opacity: 0.8; }
        pre { overflow-x: auto; white-space: pre; }
      `}</style>

      <div style={{ maxWidth: 900, margin: "0 auto", padding: "28px 20px" }}>
        {/* Header */}
        <div style={{ marginBottom: 28 }}>
          <div
            style={{
              fontSize: 10,
              letterSpacing: "0.2em",
              color: "#7c3aed",
              textTransform: "uppercase",
              marginBottom: 8,
            }}
          >
            Code Review · PR #5 · ashuchan/Laabh
          </div>
          <h1
            style={{
              fontFamily: "'Syne', sans-serif",
              fontSize: 26,
              fontWeight: 800,
              color: "#f8fafc",
              lineHeight: 1.15,
              marginBottom: 10,
            }}
          >
            OSS Integrations
            <br />
            <span style={{ color: "#ef4444" }}>❌ REQUEST CHANGES</span>
          </h1>
          <p style={{ color: "#64748b", lineHeight: 1.75, maxWidth: 640 }}>
            Reviewed against CLAUDE-OSS-INTEGRATIONS.md. Focused on{" "}
            <strong style={{ color: "#94a3b8" }}>
              fail-safety, exception handling, and correctness
            </strong>
            . Files reviewed:{" "}
            <code style={{ color: "#7c3aed" }}>docker-compose.yml</code>,{" "}
            <code style={{ color: "#7c3aed" }}>feature_pipeline.py</code>,{" "}
            <code style={{ color: "#7c3aed" }}>backtester.py</code>,{" "}
            <code style={{ color: "#7c3aed" }}>requirements/*.txt</code>,{" "}
            <code style={{ color: "#7c3aed" }}>Makefile</code>.
          </p>

          {/* Score cards */}
          <div style={{ display: "flex", gap: 12, marginTop: 20, flexWrap: "wrap" }}>
            {(
              [
                { sev: "blocker" as SeverityKey, label: "Blockers", n: counts.blocker },
                { sev: "high"    as SeverityKey, label: "High",     n: counts.high },
                { sev: "medium"  as SeverityKey, label: "Medium",   n: counts.medium },
                { sev: "good"    as SeverityKey, label: "Approved",  n: counts.good },
              ] as const
            ).map((s) => {
              const sv = SEVERITY[s.sev];
              return (
                <div
                  key={s.sev}
                  style={{
                    padding: "10px 18px",
                    borderRadius: 8,
                    background: sv.bg,
                    border: `1px solid ${sv.border}`,
                    cursor: "pointer",
                    opacity: filterSev === s.sev ? 1 : 0.7,
                    outline: filterSev === s.sev ? `1px solid ${sv.color}` : "none",
                  }}
                  onClick={() => setFilterSev(filterSev === s.sev ? "All" : s.sev)}
                >
                  <div
                    style={{
                      fontFamily: "'Syne', sans-serif",
                      fontWeight: 800,
                      fontSize: 24,
                      color: sv.color,
                    }}
                  >
                    {s.n}
                  </div>
                  <div style={{ fontSize: 11, color: sv.color, opacity: 0.8 }}>
                    {s.label}
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* File filter pills */}
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap", marginBottom: 20 }}>
          {["All", ...files].map((f) => (
            <span
              key={f}
              className="cr-pill"
              onClick={() => setFilterFile(f === filterFile ? "All" : f)}
              style={{
                padding: "3px 10px",
                borderRadius: 3,
                fontSize: 10,
                background: filterFile === f ? "#7c3aed33" : "#111",
                color: filterFile === f ? "#a78bfa" : "#475569",
                border: `1px solid ${filterFile === f ? "#7c3aed55" : "#1e1e2e"}`,
                fontFamily: "'JetBrains Mono', monospace",
                cursor: "pointer",
              }}
            >
              {f === "All" ? "All files" : f.split("/").pop()}
            </span>
          ))}
        </div>

        {/* Findings */}
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {filtered.map((f) => {
            const sv = SEVERITY[f.severity];
            const isOpen = selected === f.id;
            return (
              <div
                key={f.id}
                className={`cr-card${isOpen ? " open" : ""}`}
                style={{ borderRadius: 8, background: "#0a0a14", overflow: "hidden" }}
                onClick={() => setSelected(isOpen ? null : f.id)}
              >
                {/* Header row */}
                <div
                  style={{
                    padding: "12px 16px",
                    display: "flex",
                    gap: 12,
                    alignItems: "flex-start",
                  }}
                >
                  <span
                    style={{
                      padding: "2px 8px",
                      borderRadius: 3,
                      fontSize: 10,
                      fontWeight: 600,
                      background: sv.bg,
                      color: sv.color,
                      border: `1px solid ${sv.border}`,
                      whiteSpace: "nowrap",
                      minWidth: 68,
                      textAlign: "center",
                      marginTop: 1,
                    }}
                  >
                    {f.id} · {sv.label}
                  </span>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 500, color: "#f1f5f9", lineHeight: 1.4 }}>
                      {f.title}
                    </div>
                    <div
                      style={{
                        fontSize: 11,
                        color: "#475569",
                        marginTop: 3,
                        fontFamily: "'JetBrains Mono', monospace",
                      }}
                    >
                      {f.file}
                    </div>
                  </div>
                  <span style={{ color: "#334155", fontSize: 10, marginTop: 2 }}>
                    {isOpen ? "▲" : "▼"}
                  </span>
                </div>

                {/* Expanded */}
                {isOpen && (
                  <div style={{ borderTop: "1px solid #1a1a2a", padding: "16px" }}>
                    <p style={{ color: "#94a3b8", lineHeight: 1.8, marginBottom: 14 }}>
                      {f.description}
                    </p>

                    {f.impact && (
                      <div
                        style={{
                          padding: "8px 12px",
                          background: sv.bg,
                          border: `1px solid ${sv.border}`,
                          borderRadius: 5,
                          marginBottom: 14,
                        }}
                      >
                        <span
                          style={{
                            color: sv.color,
                            fontSize: 10,
                            letterSpacing: "0.1em",
                            textTransform: "uppercase",
                          }}
                        >
                          Impact{"  "}
                        </span>
                        <span style={{ color: sv.color, opacity: 0.85 }}>{f.impact}</span>
                      </div>
                    )}

                    {f.fix && (
                      <div>
                        <div
                          style={{
                            fontSize: 10,
                            color: "#22c55e88",
                            letterSpacing: "0.1em",
                            textTransform: "uppercase",
                            marginBottom: 6,
                          }}
                        >
                          Fix
                        </div>
                        <pre
                          style={{
                            padding: "12px 14px",
                            background: "#060610",
                            border: "1px solid #1e1e2e",
                            borderRadius: 5,
                            fontSize: 11,
                            lineHeight: 1.7,
                            color: "#7dd3fc",
                            overflowX: "auto",
                          }}
                        >
                          {f.fix}
                        </pre>
                      </div>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>

        {/* Summary verdict */}
        <div
          style={{
            marginTop: 28,
            padding: "18px 20px",
            background: "#1a0000",
            border: "1px solid #ef444433",
            borderRadius: 10,
          }}
        >
          <div
            style={{
              fontFamily: "'Syne', sans-serif",
              fontWeight: 800,
              fontSize: 16,
              color: "#ef4444",
              marginBottom: 10,
            }}
          >
            ❌ Request Changes — Do Not Merge
          </div>
          <div style={{ color: "#fca5a5", lineHeight: 1.9, fontSize: 12 }}>
            <strong>4 blockers must be fixed before merge.</strong> The two most critical:
            <br />
            <br />
            1. <strong>B3 (backtester.py)</strong> — The Sunday analyst scoring cron runs an
            empty engine with no strategy. Every analyst gets scored 0% win rate, 0% return. The
            scoring system is broken and will corrupt the analyst scoreboard silently on first
            Sunday run.
            <br />
            <br />
            2. <strong>B1 (docker-compose.yml)</strong> — Duplicate YAML top-level keys overwrite
            the existing network bindings. On next deploy, all non-openalgo services lose network
            connectivity. The entire stack goes down.
            <br />
            <br />
            3. <strong>B5 (feature_pipeline.py)</strong> — NaN VIX falls through to "sideways"
            instead of blocking trades. Fail-safe logic is inverted — the system trades more
            aggressively when data is unavailable.
            <br />
            <br />
            4. <strong>B4 (requirements)</strong> — langgraph version pin blocks installation.
            TradingAgents debate never starts.
          </div>
        </div>

        {/* Merge checklist */}
        <div
          style={{
            marginTop: 16,
            padding: "16px 20px",
            background: "#0a1a0a",
            border: "1px solid #22c55e22",
            borderRadius: 10,
          }}
        >
          <div
            style={{
              fontFamily: "'Syne', sans-serif",
              fontWeight: 700,
              fontSize: 13,
              color: "#22c55e",
              marginBottom: 10,
            }}
          >
            ✅ Merge Checklist (after fixing blockers)
          </div>
          {[
            "B1: Merge openalgo into existing networks/volumes blocks in docker-compose.yml",
            "B2: Fix SQLite path to sqlite:////app/db/openalgo.db (4 slashes)",
            "B3: Add SignalReplayActor to backtester.py — use signal_history to place orders",
            "B4: Fix langgraph pin to >=0.2,<1.0",
            "B5: Add math.isnan(vix) check → return 'high_vol' in classify_regime()",
            "H1: Replace RuntimeError with graceful fallback dict in run_signal_backtest()",
            "H2: Add minimum row count validation (MIN_ROWS=26) in extract_features()",
            "H3: Wrap engine.run() in try/except with Telegram alert on failure",
            "H4: Add start_period: 60s to OpenAlgo healthcheck",
            "H5: Add required column check in extract_features()",
            "M1: Wrap Price() args in Decimal(str(...)) for type safety",
            "M2: Skip NaT rows in _df_to_bars with a warning log",
            "M3: Switch VWAP to rolling 20-day instead of session cumsum",
          ].map((item, i) => (
            <div
              key={i}
              style={{ display: "flex", gap: 10, marginBottom: 6, alignItems: "flex-start" }}
            >
              <span style={{ color: "#22c55e", minWidth: 14, marginTop: 1 }}>□</span>
              <span style={{ color: "#94a3b8", lineHeight: 1.6, fontSize: 12 }}>{item}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
