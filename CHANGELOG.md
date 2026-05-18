# Changelog

All notable changes to poly1 are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: `vMAJOR.MINOR.PATCH[-label]`.

> **Authoritative state:** `SPEC.md` · **Operational runbook:** `deploy/PREFLIGHT.md`

---

## [v0.6.0-scanner] — 2026-05-18 · HEAD

### Added
- **`market_scanner.py`** — 5-min proactive scanner daemon (`--profile scanner`).
  Fetches top-120 markets by 24h volume, runs brain gate + Tavily + Manifold
  divergence, then routes via DB:
  - `brain_decisions (approved=True)` → Trader
  - `news_signals (scanner_news_shock)` → news_shock
  - `news_signals (scanner_near_resolution)` → near_resolution
- **`AGENT_GOALS` registry** in `market_scanner.py` — canonical goal/strategy/target
  dict for all 8 active agents. Export with `--goals` CLI flag.
- **`market_brain.py` `evaluate_general_entry()`** — fast pre-LLM scoring layer
  (spread, horizon, Tavily context gates). Hard-rejects spread >15% or horizon
  outside 0.5h–168h.
- **Vibe-Trading provider** in `external_conviction.py` — integrates
  Vibe-Trading technical analysis as an additional conviction signal.
- **SPEC.md §§17–22** — added dedicated sections for every previously
  undocumented agent (Agent Registry, BTC Daily, News Shock, Wallet Follow,
  Position Manager, Market Scanner). §3 module table restructured into three
  subsections.

### Fixed
- **Scanner → news_shock direction mismatch.** Scanner was writing
  `direction='yes'/'no'`; news_shock expects `'bullish'/'bearish'`. Fixed in
  `market_scanner.py` Route 3 before writing `news_signals`.
- **news_shock status filter.** `_read_fresh_signals()` was filtering
  `status='news_signal'` only; scanner-generated rows have
  `status='scanner_news_shock'`. Query now uses `status IN ('news_signal',
  'scanner_news_shock')`.
- **near_resolution scanner blindness.** Added `_scanner_market_ids()` +
  re-sort so scanner-flagged near-resolution markets bubble to top of
  candidate list.
- **`risk_gate.py` / `near_resolution.py` / `runtime_control.py`** — minor sync
  fixes committed alongside scanner wiring.

---

## [v0.5.0-agent-hardening] — 2026-05-17

### Added
- **`external_conviction.py`** — multi-provider conviction aggregator supporting
  16 providers: `heuristic`, `public_news`, `tavily`, `http_json`,
  `polifly_browser`, `clob_whale`, `manifold`, `metaculus`, `cross_market`,
  `kalshi`, `whale_consensus`, `debate`, `nansen`, `wallet_master`,
  `polifly_enhanced`, `aggregator`. Runs shadow-only (`EXECUTE_EXTERNAL_CONVICTION=false`).
- **LLM-based exit evaluation** in `position_manager.py` — optional
  Tavily-enriched LLM exit check before closing a position.
- **External conviction entry gate** — `trade.py` checks `brain_decisions` for
  pre-approved conviction candidates before LLM call.
- **`near_resolution` smart straddle** — LLM directional analysis + straddle
  mode for high-confidence near-resolution plays.
- **Straddle improvements** — cache, tighter threshold, `no_sl` partner mode.
- **Sharper entry analysis** in `executor.py` — price context, news enrichment,
  `NO_EDGE` explicit filter, TP=10% default.
- **Tavily external validation** in `btc_daily.py`, `wallet_follow.py`,
  `near_resolution.py` — Tavily-enriched pre-entry context across agents.
- **Short-term market preference** — agents score 1h–48h horizon higher than
  long-horizon markets.
- **`tavily.py`** — shared stdlib-only helper (`tavily_headlines`,
  `tavily_confidence`). Used across 5 agents.

### Fixed
- **`btc_daily.py` anchor fix** — uses actual `candidate_mid` as price anchor
  instead of always anchoring to 0.5. Adds `max_entry_price` gate (default 0.65).
- **`wallet_follow.py` signal hardening** — drift gate, 1h signal age cap, min
  wallet trades filter (default 5 trades/30d).
- **`news_shock.py` drift gate** — 30-min signal age cap, `yes_price` stored in
  DB to enable accurate drift calculation.
- **`pre-live config fixes`** — `deb6dfb` corrects several config/env mismatches
  found in pre-live audit.
- **Forensic audit** — `docs/SESSION_2026-05-17_AUDIT.md` documents -24%
  drawdown over 8 live BTC Daily trades; root causes and policy responses.

---

## [v0.4.0-stabilization] — 2026-05-12/13

### Added
- **Settlement reconciler** (`agents/application/settlement_reconciler.py`) —
  on-chain truth reconciliation; classifies positions as `active_managed`,
  `active_unmanaged`, `redeemable`, `lost_final`, `dust_unrecoverable`, etc.
  Writes `settlement_reconciliation` table; supervisor reads these rows.
- **Trading supervisor** (`agents/application/trading_supervisor.py`) —
  safety control-plane daemon. Checks exit-path health every 60s; writes HALT
  if `close_failed` storm or stale position_manager heartbeat detected.
- **Live stabilization preflight** (`scripts/trading_stability_preflight.py`) —
  dependency-light readiness check for `.env`, runtime control, DB state, and
  open positions before enabling any live agent.
- **Runtime control freeze guard** — `RiskGate` now blocks unless mode is
  trade-enabled and `RUNTIME_CONFIG_HASH` matches the current control file.
- **BTC Daily live probes** — first live trades; 8 fills recorded 2026-05-13.
  Postmortem in `docs/BTC_DAILY_POSTMORTEM_2026-05-17.md`.

### Fixed
- **`position_manager.py`** — writes `position_mark` even when midpoint fetch
  fails; prevents supervisor stale-evidence false alarms.
- **`executor.py` Anthropic fallback** — fix crash when `messages` is a plain
  string instead of list in Anthropic message conversion path.
- **Re-entered token positions** — `filled_positions_with_id()` now scoped to
  rows after the latest terminal close row per `token_id`, preventing old fills
  from inflating open-position counts after re-entry.
- **`has_filled_position_for_market()` re-entry bug** — stale fills from
  shadow-mode sessions no longer block markets after position_manager wrote a
  terminal close row.
- **`btc_daily.py` fill-price exit** — uses actual fill price for exit
  calculations, not model price.

---

## [v0.3.0-scalper-built] — 2026-05-05 · tagged

### Added
- **Scalper strategy C** (`agents/application/scalper.py`) — math-spread arb
  on crypto 15-min UP/DOWN pairs. FAK BUY both legs when pair ask sum <1.04.
  No LLM. Hold ≤10 min.
- **`scalper_pairs.py` / `scalper_pairs` table** — state machine for pair
  tracking (`OPEN`, `PROFIT`, `LOSS`, `RECONCILE_NEEDED`).
- **`SCALPER_LEG` status** — audit trail for each FAK attempt in `trades` table.
- **SQLite WAL mode** — `trade_log.db` upgraded to WAL for concurrent
  multi-container writes.
- **CLOB V2 migration** — deposit wallet support (`POLYMARKET_DEPOSIT_WALLET`),
  builder attribution (`POLYMARKET_BUILDER_CODE`), slippage guard
  (`POLYMARKET_MAX_SLIPPAGE`), `signature_type=3` (`POLY_1271`).
- **Streamlit dashboard** (`dashboard/`) — Live, P&L, Capital, Trades, Scalper,
  LLM Cost, Control tabs. Docker service, auto-refresh 30s.
- **Capital allocator** (`agents/application/capital_allocator.py`) —
  read-only allocation scoring across agents.

### Fixed
- Various pre-launch CLOB V2 compatibility issues.

---

## [v0.2.0-prod-prep] — 2026-05-01 · tagged

### Added
- **SQLite ledger** (`trade_log.py`) — idempotent trade log with crash recovery
  via `MAY_HAVE_FIRED`, `ACTIVE_STATUSES` dedupe contract.
- **RiskGate** — kill switch file, daily drawdown %, USDC floor, rate limit,
  LLM daily cost cap.
- **TraderDaemon** — long-running loop, SIGTERM-aware, heartbeat file,
  Telegram + Healthchecks.io notifications.
- **Docker** — multi-stage Dockerfile, `docker-compose.yml` with bind-mount
  for `data/`.
- **`deploy/deploy.sh`** + **`deploy/PREFLIGHT.md`** — VPS bootstrap and
  pre-launch checklist.

### Fixed
- **BLOCKER: side/token mapping** — `BUY → token_ids[0]` at `recommendation.price`;
  `SELL → token_ids[1]` at `1 − price`. Previously inverted, causing silently
  wrong trades.
- **MAY_HAVE_FIRED dedupe bypass** — stranded `pending` rows recovered on
  startup; block market indefinitely until operator verifies on-chain.
- **HALT/RESUME surface** — `touch data/HALT` halts all trading; `rm` resumes.

---

## [legacy] — pre-2026-05-01

Original polymarket-agents fork. Single-strategy LLM trader, no production
hardening, no ledger, no daemon.
