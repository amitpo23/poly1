# poly1 Agent Matrix — 2026-05-19

## Canonical Strategy

poly1 is a fast, active trading system. No entry is allowed to be "dumb":
every live entry must pass a signal brain, a risk gate, and journal dedupe.
The operator preference is short hold time: take the profit, exit, and move to
the next opportunity.

Hard policy:

- Stop loss: 3% (`POLY1_STOP_LOSS_PCT`, default `0.03`).
- Fast profit-taking: brain may exit from 5% profit when momentum does not
  justify holding (`POLY1_FAST_TAKE_PROFIT_PCT`, default `0.05`).
- Profit cap: never hold past 25% profit (`POLY1_TAKE_PROFIT_CAP_PCT`,
  default `0.25`).
- Entry probability threshold: 52% minimum weighted probability/score across
  live agents; below this, the brain must reject the trade.
- Directional crypto edge rule: trade the chosen side only when the internal
  brain probability is above the live Polymarket entry price by at least 2
  percentage points.
- MetaBrain edge rule: when a live side price is available, weighted internal
  probability must beat that entry price by at least 2 percentage points.
- Max hold: 6h hard safety ceiling (`POLY1_MAX_HOLD_SECONDS`, default
  `21600`), not a target hold time.
- Market scan cadence: 60s default.
- Position manager cadence: 60s default; every open position gets a fresh
  brain/LLM exit re-check each minute.
- Max trading rate: up to 100 trades/hour (`MAX_TRADES_PER_HOUR=100`), still
  gated by brain, risk, liquidity, and journal checks.
- Agent allocation cap: any one live agent may receive at most 50% of wallet
  capital (`MAX_AGENT_ALLOCATION_FRACTION=0.50`).
- Telegram reporting: immediate alert on every buy/fill and sell/exit; full
  PnL/dashboard report once per hour.

## Matrix

| Agent | Identity | Strategy | Can place orders? | Shared information |
| --- | --- | --- | --- | --- |
| `meta_brain` | Final entry brain | Fuses MarketBrain, Gamma, cross-market conviction, win-rate, and velocity before main entries | No | `brain_decisions`, `position_marks`, external conviction JSONL |
| `trader` | Main LLM trader | Crowd/psychological mispricing only after MetaBrain approval | Yes | `trades`, `brain_decisions`, Gamma, Tavily |
| `market_scanner` | Opportunity router | Scans Gamma every minute, scores opportunities, routes to agents | No | `brain_decisions`, `news_signals` |
| `position_manager` | Exit brain | Re-checks every minute, exits fast by default, holds only with strong forecast, enforces 3% stop and 25% cap | Sell only | `trades`, `position_marks`, resolution sync |
| `risk_gate` | Capital guard | Blocks stale runtime, overtrading, drawdown, low balance, reserve conflicts, and >50% agent allocation | No | runtime control, trade log, wallet balance |
| `trading_supervisor` | Watchdog | Finds stale heartbeats, stuck positions, close failures, and unsafe drift | No | heartbeats, trade log |
| `scalper` | 15m crypto scalper | Mathematical UP/DOWN edge, exits via MarketBrain | Yes | `scalper_pairs`, `trades`, crypto feed |
| `btc_5min` | BTC 5m agent | Short-horizon BTC consensus with MarketBrain sanity gate | Yes | Gamma, trade log |
| `btc_daily` | BTC daily agent | Mean-reversion/fade overreaction, exits centrally | Yes | Gamma, trade log |
| `near_resolution` | Near-resolution agent | High-confidence events close to resolution with liquidity checks | Yes | Gamma, Tavily, trade log |
| `news_signal` | News ingestion | Finds material headlines and journals signals | No | `news_signals` |
| `news_shock` | News reaction trader | Acts on fresh material news before full repricing | Yes | `news_signals`, Gamma, trade log |
| `wallet_watcher` | Wallet intelligence | Tracks wallets and produces follow signals | No | `wallet_signals` |
| `wallet_follow` | Whale-follow trader | Mirrors proven wallets only when EV/drift/liquidity pass | Yes | `wallet_signals`, trade log |
| `external_conviction*` | Conviction family | Manifold, Metaculus, Kalshi, news, technical, whale, debate, aggregator | Signals; API variant can enter | JSONL outputs, `brain_decisions` |
| `settlement_reconciler` | Recovery layer | Detects resolved markets and redeemable/recoverable positions | No | settlement tables, Gamma |
| `allocator_sync` | Capital allocator | Keeps reserves aligned with performance and runtime policy | No | `.env`, allocator tables |
| `telegram_reporter` | Operator dashboard | Sends one broad hourly status report | No | trade log, wallet sync, heartbeats |

## Non-Negotiables

- Entry agents must never bypass `RiskGate`.
- Main trader must never bypass `MetaBrain`.
- Every live entry must have brain/signal approval; runtime preflight requires
  `POLY1_REQUIRE_BRAIN_APPROVAL=true` and `MARKET_BRAIN_ENABLED=true`.
- Holding is the exception, not the goal: if a position is profitable, exit
  quickly unless the brain records strong forecast/momentum evidence to hold.
- Exit behavior is centralized in `PositionManager`; entry agents should not
  invent private stop-loss/take-profit rules unless they are stricter.
- Any order attempt must create a journal row before or at execution time.
- `may_have_fired` rows block re-entry until manually reconciled.
- Runtime mode must be explicit; `freeze` means no live entries.

## Telegram

Low-noise rule: fills, close events, errors, and one full hourly dashboard.
The hourly dashboard is `scripts/python/telegram_report.py --daemon` and is
available in compose profile `monitoring` as `telegram-reporter`.

Expected command vocabulary for the operator layer:

- `/status` — runtime mode, heartbeats, last cycle.
- `/positions` — open positions, entry, current mark, MFE/MAE.
- `/agents` — agent health and last action.
- `/risk` — balance, reserves, daily loss, open-position count.
- `/pnl` — realized/unrealized PnL by agent.
- `/halt` — operator halt workflow.
