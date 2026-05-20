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
4. It executes only if all gates pass:
   - decision age is within `SCANNER_EXECUTOR_MAX_DECISION_AGE_SEC`
   - `meta_timing == now` when required, unless controlled wait-probe mode is
     enabled and the score is above `SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE`
   - score >= `SCANNER_EXECUTOR_MIN_SCORE`
   - execution metadata is complete
   - no active/recently closed duplicate position
   - live order book is fillable and exitable
   - live raw EV >= `SCANNER_EXECUTOR_MIN_RAW_EV`
   - RiskGate and runtime control allow this agent
   - Kelly sizing returns a positive size

If any gate fails, `scanner_executor` writes a rejected brain decision with the
exact reason. Missing data is a skip, not a trade.

## Runtime Controls

Defaults are fail-closed:

```env
EXECUTE_SCANNER_EXECUTOR=false
SCANNER_EXECUTOR_RESERVE_USDC=0
SCANNER_EXECUTOR_POSITION_SIZE_USDC=1.0
SCANNER_EXECUTOR_MIN_SCORE=0.80
SCANNER_EXECUTOR_MIN_RAW_EV=0.04
SCANNER_EXECUTOR_REQUIRE_TIMING_NOW=true
SCANNER_EXECUTOR_ALLOW_WAIT_WITH_HIGH_SCORE=false
SCANNER_EXECUTOR_WAIT_OVERRIDE_MIN_SCORE=0.79
SCANNER_EXECUTOR_MAX_OPEN=4
SCANNER_EXECUTOR_REENTRY_COOLDOWN_HOURS=12
```

`scanner_executor` is registered in `deploy/runtime_policy.json`, so
`scripts/runtime_control.py live-probe --agent scanner_executor ...` can enable
it through the same control plane as the other entry agents.

## Safety Notes

- `market_scanner` still never places orders.
- `scanner_executor` inserts a pending journal row before any live order.
- Live fills are written with status `filled`, so `position_manager` manages
  them with the standard stop-loss/take-profit/timeout logic.
- Telegram fill notifications use the existing `notify_trade` path.
