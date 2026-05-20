# Scanner Executor Bridge - 2026-05-20

## Why This Exists

During the controlled live probe, `market_scanner` wrote many approved
`brain_decisions`, but no live trades were opened. The root cause was structural:
scanner approvals were recorded as signals only. The active live agents
(`btc_5min`, `near_resolution`, `wallet_follow`) did not consume those approvals
as execution candidates.

## New Flow

1. `market_scanner` scans Gamma, MetaBrain, Tavily, Manifold, velocity, win-rate,
   whale/news evidence.
2. If MetaBrain approves, the scanner writes a `brain_decisions` row with:
   - market/question/slug
   - CLOB token ids
   - selected side (`BUY` for outcome[0], `SELL` for outcome[1])
   - selected token id
   - estimated win probability
   - scanner-side raw EV
   - MetaBrain timing and evidence route
3. `scanner_executor` consumes only fresh approved scanner decisions.
4. It asks `DecisionCouncil` for a final deterministic review using the live
   executable entry price, source provenance, MetaBrain evidence route, and
   net EV after round-trip cost.
5. It executes only if all gates pass:
   - decision age is within `SCANNER_EXECUTOR_MAX_DECISION_AGE_SEC`
   - `meta_timing == now` when required, unless controlled wait-probe mode is
     enabled and the score is above `SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE`
   - score >= `SCANNER_EXECUTOR_MIN_SCORE`
   - `estimated_win_probability_calibrated=true`; MetaBrain score alone is
     rank-only and cannot be used as EV-bearing probability
   - execution metadata is complete
   - no active/recently closed duplicate position
   - live order book is fillable and exitable
   - live raw EV >= `SCANNER_EXECUTOR_MIN_RAW_EV`
   - live net EV passes the council threshold:
     - normal candidates: `DECISION_COUNCIL_MIN_NET_EV`
     - proven `expert_solo` candidates: `DECISION_COUNCIL_EXPERT_MIN_NET_EV`
     - thin markets: `DECISION_COUNCIL_THIN_MIN_NET_EV`
   - RiskGate and runtime control allow this agent
   - Kelly sizing returns a positive size

If any gate fails, `scanner_executor` writes a rejected brain decision with the
exact reason and a `decision_journal` row. Missing data is a skip, not a trade.

## Runtime Controls

Defaults are fail-closed:

```env
EXECUTE_SCANNER_EXECUTOR=false
SCANNER_EXECUTOR_RESERVE_USDC=0
SCANNER_EXECUTOR_POSITION_SIZE_USDC=1.0
SCANNER_EXECUTOR_MIN_SCORE=0.80
SCANNER_EXECUTOR_MIN_RAW_EV=0.04
SCANNER_EXECUTOR_MIN_NET_EV=0.03
SCANNER_EXECUTOR_ROUND_TRIP_COST_PCT=0.04
SCANNER_EXECUTOR_READ_ORDERBOOK_IN_SHADOW=true
SCANNER_EXECUTOR_REQUIRE_CALIBRATED_PROBABILITY=true
DECISION_COUNCIL_MIN_NET_EV=0.04
DECISION_COUNCIL_EXPERT_MIN_NET_EV=0.025
DECISION_COUNCIL_THIN_MIN_NET_EV=0.06
DECISION_COUNCIL_MIN_PROBABILITY=0.52
DECISION_COUNCIL_EXPERT_MIN_PROBABILITY=0.50
DECISION_COUNCIL_THIN_LIQUIDITY_USDC=5000
SCANNER_EXECUTOR_REQUIRE_TIMING_NOW=true
SCANNER_EXECUTOR_ALLOW_WAIT_WITH_HIGH_SCORE=false
SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE=0.79
SCANNER_EXECUTOR_MAX_OPEN=4
SCANNER_EXECUTOR_REENTRY_COOLDOWN_HOURS=12
EXPERT_EXTERNAL_SOLO_SOURCE_TYPES=cross_market,equity_fv,alpaca_market_data,crypto_exchange_tape
EXPERT_EXTERNAL_SOLO_MIN_CONFIDENCE=0.60
EXPERT_EXTERNAL_SOLO_MAX_AGE_SEC=300
```

`scanner_executor` is registered in `deploy/runtime_policy.json`, so
`scripts/runtime_control.py live-probe --agent scanner_executor ...` can enable
it through the same control plane as the other entry agents.

For controlled wait-probe runs, `scripts/runtime_control.py live-hour
--scanner-allow-wait --scanner-wait-min-score 0.79` also aligns
`SCANNER_EXECUTOR_MIN_SCORE` to the wait threshold. Otherwise a 0.79+ wait
candidate can pass the timing override and still be blocked by the default 0.80
executor score gate.

## Safety Notes

- `market_scanner` still never places orders.
- `scanner_executor` inserts a pending journal row before any live order.
- `scanner_executor` also writes every final ENTER/SHADOW_ENTER/REJECT into
  `decision_journal`, including live price, internal probability, raw EV, net
  EV, source, mode (`solo`/`consensus`/`blocked`), and the reason.
- Live fills are written with status `filled`, so `position_manager` manages
  them with the standard stop-loss/take-profit/timeout logic.
- Controlled `$1` probes use `MAINTAIN_MIN_EXIT_NOTIONAL_USDC=0.50`, so smart
  exits are not blocked by a `$1.00` dust threshold when the brain decides to
  leave quickly.
- Take-profit exits are labeled as profit only when the executable sell price
  clears `MAINTAIN_MIN_TAKE_PROFIT_NET_PCT` or `MAINTAIN_MIN_TAKE_PROFIT_USDC`.
  Midpoint-only profit is not enough.
- Telegram fill notifications use the existing `notify_trade` path.

## Provider Scorecard

`scripts/provider_scorecard.py` builds `data/provider_scorecard.json` from
resolved `brain_decisions.signal_source` rows:

```bash
python scripts/provider_scorecard.py --db data/trade_log.db --out data/provider_scorecard.json
```

MetaBrain's `SourceReliabilityAdvisor` can consume that file through
`PROVIDER_SCORECARD_PATH`. This gives the brain a measured reliability fallback
for providers before they have enough locally resolved live rows.
