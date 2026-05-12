# Trading Postmortem - 2026-05-12

Scope: deep review of the live/paper trading activity recorded in `data/trade_log.db`
and `swarm/swarm.db` up to 2026-05-12.

This document is intentionally blunt. The system had infrastructure, agents,
wallet connectivity, and dashboards, but it did not yet have a reliably
profitable trading loop.

## Executive Summary

We did not make money because the system optimized for "agents running" before it
proved repeatable edge.

The main failures were:

1. Entries were allowed before exit mechanics were robust.
2. Several positions were too small to exit cleanly after price moved against them.
3. Profit-taking was mostly static, not opportunity-aware.
4. Some exits failed hundreds of times instead of escalating to a different exit path.
5. AI/news intelligence was unavailable or neutral for most relevant moments.
6. The swarm market maker bought inventory but did not complete a full maker lifecycle.
7. Crypto 15m scalping was structurally weak under spread, slippage, and tiny size.
8. We measured activity more than expected value.

The result: the system produced logs, candidates, and some orders, but not a
disciplined loop of:

candidate -> EV estimate -> liquid entry -> monitored position -> intelligent exit -> learning update

## Data Snapshot

Trade DB snapshot:

- Database: `data/trade_log.db`
- Snapshot used: `/tmp/poly1_trade_log_snapshot.db`
- Integrity check: OK
- Total `trades` rows: 2,295

Important row counts:

| Status | Rows | Notes |
| --- | ---: | --- |
| `close_failed` | 844 | Exit attempts repeatedly failed, especially FAK orders. |
| `scalper_leg` | 383 | Mostly scalper activity/noise, not completed profitable loops. |
| `closed_dust` | 365 | Positions below exit notional repeatedly retried. |
| `failed` | 343 | Includes 404s, stale prices, missing liquidity. |
| `skipped_dedupe` | 269 | Duplicate protection prevented repeated exposure. |
| `btc_daily_open` | 27 | BTC daily entries/open attempts. |
| `filled` | 10 | Main trader filled rows. |
| `closed_stop_loss` | 9 | Most realized exits were defensive. |
| `closed_take_profit` | 2 | Very few clean take-profit exits. |

Brain decision summary:

| Agent / Brain | Decision | Rows | Meaning |
| --- | --- | ---: | --- |
| `scalper` | rejected `too_close_to_expiry` | 2,833 | The scalper saw many markets that were structurally too late. |
| `scalper` | rejected `edge_score_too_low` | 1,841 | The scalper usually did not find edge. |
| `position_manager` | approved `timeout` | 556 | Exits were often time-based, not thesis-based. |
| `position_manager` | `stop_loss` | 377 | Risk exits dominated profit exits. |
| `scalper` | approved | 57 | Few approvals, not enough proven edge. |
| `position_manager` | `take_profit` | 8 | Profit-taking was rare. |

## Position-Level Findings

### btc_daily

BTC daily was the least broken agent, but still not clearly profitable.

Observed closed/open summary:

| Market | Cost | Proceeds | Approx Result | Notes |
| --- | ---: | ---: | ---: | --- |
| `2175538` | $9.00 | $9.4386 | +$0.4386 | One mixed but net-positive token group. |
| `2223007` token A | $6.00 | $2.34 | -$3.66 | Large loss relative to tiny winners. |
| `2223007` token B | $3.00 | $3.12 | +$0.12 | Small take-profit. |
| `2214715` | $6.00 | $5.7276 | -$0.2724 | Small realized loss. |
| `2166361` | $6.00 | no exit | open/unrecovered | Open capital, not realized profit. |

What went right:

- It produced some winners.
- It did not fail as completely as scalper/trader.
- It can be used as a small live-probe candidate.

What went wrong:

- The average win size was too small compared with the largest loss.
- Static stop/take-profit logic did not maximize temporary gains.
- Some positions remained open or turned into repeated dust handling.

Required change:

- Keep btc_daily small until it proves positive EV after fees/spread.
- Add trailing profit protection once a position has moved +4% to +8%.
- Stop using fixed winrate as the target. Use EV and payoff ratio.

### scalper

The scalper should not be live in its current form.

Closed scalper outcomes:

| Market | Cost | Proceeds | Approx Result | Exit |
| --- | ---: | ---: | ---: | --- |
| BTC 15m | $2.50 | $2.2880 | -$0.2120 | stop loss |
| BTC 15m | $2.50 | $2.2880 | -$0.2120 | stop loss |
| ETH 15m | $2.50 | $2.2833 | -$0.2167 | stop loss |
| BTC 15m | $2.50 | $2.3543 | -$0.1457 | timeout |
| XRP 15m | $2.50 | $2.2880 | -$0.2120 | stop loss |

Every closed scalper loop lost money.

What went right:

- The scalper brain rejected many weak candidates.
- It correctly identified that many markets were too close to expiry or had weak edge.

What went wrong:

- Crypto 15m markets are too sensitive to spread and timing.
- Tiny position size makes slippage and exit constraints dominate.
- The strategy needs either maker-only logic or much stronger directional edge.
- A taker-style scalper is fighting the venue mechanics.

Required change:

- Keep scalper in shadow/paper only.
- Rebuild as maker-only spread capture or retire it.
- Do not give it live capital until backtest + paper + live probe all show positive EV after spread.

### trader

The main trader created exposure without enough proof that the entry price had edge.

Observed closed outcomes:

| Market | Cost | Proceeds | Approx Result | Notes |
| --- | ---: | ---: | ---: | --- |
| `566187` | $5.4065 | $4.2940 | -$1.1125 | Stop-loss exit. |
| `566188` | $6.3328 | $4.2140 | -$2.1188 | Stop-loss exit. |
| `653788` | $3.6500 | $0.0030 | -$3.6470 | Timeout/resolution-like loss. |
| `572733` | $1.3741 | $0.0000 | -$1.3741 | 552 failed close attempts before loss. |
| `566228` | $5.1054 | no exit | open/unrecovered | High entry around 0.997, poor upside. |

What went right:

- Duplicate protection later prevented some repeated exposure.
- The logs preserved enough data to diagnose failure modes.

What went wrong:

- The agent entered markets where price/edge was not proven.
- It sometimes averaged into similar exposure before exits were reliable.
- A high-price entry near 0.997 had almost no upside and meaningful downside.
- Exit failures were allowed to loop instead of escalating.

Required change:

- No live entries without OpportunityRouter approval.
- Reject high-price entries unless they are near-resolution arbitrage with verified payout.
- Add maximum exit-failure count and fallback path.

### swarm

The swarm system was alive, but not yet a profitable trading system.

Swarm DB summary:

| Status | Agent | Rows | Size USD |
| --- | --- | ---: | ---: |
| `cleared` | `market_maker` | 1,792 | 1,856 |
| `failed` | `market_maker` | 479 | 1,715 |
| `filled` | `market_maker` | 1 | 1 |
| `failed` | `nothing_happens` | 3 | 15 |
| `cleared` | `nothing_happens` | 1 | 5 |

Recent filled swarm order:

- Agent: `market_maker`
- Side: BUY YES
- Size: about $1
- Price: around 33.5-34 cents
- State: one open inventory position

What went right:

- The market maker could place and reconcile at least one real fill.
- Pending-order tracking prevented uncontrolled duplicate live orders.

What went wrong:

- It did not complete the full lifecycle: quote -> fill -> manage inventory -> exit/settle -> PnL update.
- It repeatedly skipped because a submitted order existed in `pending_orders`.
- AI decision logic was blocked by OpenAI quota errors and returned confidence 0.
- It was not really market-making both sides; it mostly became inventory holding.

Required change:

- Add explicit inventory manager for swarm fills.
- Add cancellation/requote logic.
- Add exit/settlement PnL journal.
- Keep AI-dependent swarm decisions in research mode until model quota and fallback are fixed.

## Micro-Level Root Causes

### 1. Positions were too small for reliable exits

`closed_dust` happened 365 times. This means the position manager repeatedly found
positions below `min_exit_notional=1.0`.

The practical result:

- A $1 or $2.50 position can become impossible or uneconomic to close after price drops.
- Even when the bot detects a stop or take-profit, the order may not be executable.
- The system creates noise instead of clean PnL.

Fix:

- Block entries unless `entry_size * worst_case_exit_price >= min_exit_notional * safety_buffer`.
- For current settings, many agents should not trade below roughly $3-$5 per position.
- Dust positions should be marked once and throttled, not retried endlessly.

### 2. Exit execution failed repeatedly

The largest concrete failure:

- `close_failed`: 844 rows
- Common error: `no orders found to match with FAK order`
- One market had 552 failed close attempts before ending as a loss.

This means the bot often wanted to exit but could not actually cross the book.

Fix:

- After 1-3 failed FAK exits, switch to a fallback:
  - maker limit exit,
  - wider taker limit within max slippage,
  - partial exit,
  - or mark as stuck and alert.
- Never allow hundreds of identical exit failures.

### 3. Profit-taking was static and too rare

Only two rows were clean `closed_take_profit`.

The position manager mostly produced:

- stop-loss
- timeout
- hold

That is not enough. A Polymarket position can show temporary profit and then fade.
If we do not track maximum favorable excursion, the bot cannot know it missed a
profitable exit.

Fix:

- Store MFE/MAE per position:
  - max favorable excursion
  - max adverse excursion
  - highest observed bid/mid
  - profit available at best bid
- Add trailing exit:
  - if unrealized profit reaches +5%, arm protection;
  - if it falls by 2%-3% from peak, exit;
  - if liquidity is thin, prefer earlier maker exit.

### 4. Entry recommendations were stale versus live ask

Several failed rows show live ask above recommended price.

Example pattern:

- recommended price: 0.50
- live ask: 0.55 or 0.62
- result: skipped/failed

This is good risk control, but it also means the research pipeline was not fast
or precise enough.

Fix:

- The final decision must be made using live orderbook data.
- Research may propose candidates, but execution must recompute EV at the current ask.

### 5. AI and news brains were mostly unavailable or neutral

News signal status:

- `news_signal neutral`: 148
- `classifier_failed neutral`: 55
- only one skipped bullish row with materiality around 0.7

Swarm AI decision:

- OpenAI quota/rate-limit produced confidence 0 and SKIP.

Fix:

- Add deterministic fallback classifiers.
- Treat AI-unavailable as "paper only", not as live-trade approval.
- Add source-specific intelligence per domain:
  - politics,
  - sports,
  - crypto,
  - near-resolution,
  - macro/news shock.

### 6. 404s and orderbook mismatches were not just noise

There were 272 failed rows with status code 404. That indicates agents were
trying to query markets or endpoints that did not resolve correctly.

Fix:

- Any market with repeated 404/no-orderbook must be quarantined.
- Router should reject markets with broken CLOB/Gamma mapping.

## Macro-Level Root Causes

### 1. We treated agents as traders before they proved edge

The architecture had many agents, but not enough promotion discipline.

Correct lifecycle:

research -> backtest -> paper -> live probe -> scaled live

Most agents should remain research-only until they prove positive EV.

### 2. The target should not be winrate

A 60% winrate can lose money if entries are too expensive.

The core metric must be:

```text
expected_value = estimated_true_probability
  - entry_price
  - slippage
  - fees_or_execution_penalty
  - model_error_margin
```

Trade only when EV is positive with a real margin of safety.

### 3. The system lacked an OpportunityRouter as the capital gate

The router should decide:

- trade
- paper
- reject
- backtest_required

Inputs should include:

- scout candidates,
- news/Tavily/RSS,
- CLOB liquidity,
- historical analogs,
- DB outcomes,
- research committee,
- current wallet exposure,
- agent-specific track record.

Until this router is authoritative, agents will keep generating fragmented trades.

### 4. Too many strategies were running without enough capital per strategy

With $20-$80 total, we cannot safely run many tiny live agents.

Tiny allocations create:

- dust,
- poor exits,
- weak statistical sample,
- high operational overhead.

Better:

- 1-2 live probes,
- 1-3 paper probes,
- everything else research.

### 5. Strategy-market fit was weak

The scalper tried to trade short crypto markets where spread and timing dominate.

Better candidates:

- near-resolution mispricings,
- news shock before repricing,
- maker-only spread capture,
- specialized sports/politics/crypto domain agents,
- wallet-follow only after wallet signal quality is proven.

## What Should Have Happened

Before live trading:

1. Every live agent should pass backtest and paper checks.
2. Every entry should pass EV, liquidity, and exitability checks.
3. Entry size should be large enough to exit after adverse movement.
4. The bot should know the exact exit path before entry.
5. Profit monitoring should track temporary gains.
6. Exit failure should escalate quickly.
7. AI-unavailable should downgrade to paper/reject.
8. Swarm fills should have inventory exit/settlement logic.

During trading:

1. Let research agents produce many candidates.
2. Let OpportunityRouter choose only the strongest 1-3.
3. Use live orderbook data at final execution.
4. Enter only when EV remains positive after spread/slippage.
5. Track MFE/MAE continuously.
6. Take partial profit or trail after +5%.
7. Stop trading a strategy after repeated negative expectancy.

After trading:

1. Write every trade outcome back into the DB.
2. Store why each decision was made.
3. Compare predicted probability against actual outcome.
4. Penalize agents that produce bad EV, even if they occasionally win.
5. Promote only strategies with repeatable positive EV.

## Required Engineering Changes

Priority order:

1. **Position Profit Monitor**
   - Track MFE/MAE, peak bid, peak mid, and missed-profit events.
   - Trigger trailing exits after temporary profit.

2. **Minimum Exitable Notional Gate**
   - Block entries that can become unexitable dust.
   - Convert repeated `closed_dust` rows into throttled state.

3. **Exit Failure Escalation**
   - Maximum 3 identical FAK failures.
   - Then maker fallback, wider controlled taker, partial exit, or stuck alert.

4. **OpportunityRouter as Capital Gate**
   - No live capital unless router approves positive EV.
   - Agents without proof stay in research/paper.

5. **Swarm Inventory Manager**
   - Manage filled orders, exits, settlement, and PnL.
   - Prevent "filled then frozen" behavior.

6. **AI Fallback Classifier**
   - If OpenAI/Tavily classification fails, use deterministic fallback.
   - AI failure must not produce live approval.

7. **Agent Promotion Ledger**
   - Record each agent's state:
     - research,
     - backtest,
     - paper,
     - live_probe,
     - live_scaled,
     - demoted.

8. **Market Quarantine**
   - Quarantine markets with repeated 404/no-orderbook/price mismatch.

## Agent-Specific Recommendation

| Agent | Current Recommendation | Reason |
| --- | --- | --- |
| `btc_daily` | small live probe only | Some wins, but payoff ratio still weak. |
| `scalper` | paper/shadow | Closed loops all lost money. |
| `trader` | paper until router-gated | Entered weak/expensive markets and had bad exits. |
| `news_shock` | research/paper | Signals mostly neutral/classifier failed. |
| `near_resolution` | research/paper | Candidate discovery works, confidence still conservative. |
| `wallet_follow` | research | No fresh wallet signal flow. |
| `swarm_market_maker` | live probe only after inventory manager | Current loop can buy but not fully manage inventory. |
| `swarm_ai_decision` | research | Blocked by AI quota/fallback issue. |

## Bottom Line

The system did not fail because we lacked agents.

It failed because live capital reached agents before the full trading loop was
strict enough:

```text
edge -> EV -> liquidity -> exitable size -> controlled entry -> smart exit -> learning
```

The next version should be narrower, stricter, and more capital-aware.

Recommended next live setup:

- Keep only `btc_daily` and one repaired `swarm_market_maker` as tiny live probes.
- Keep `scalper`, `trader`, `news_shock`, `near_resolution`, and `wallet_follow`
  in paper/research until they produce positive EV evidence.
- Build the OpportunityRouter and Position Profit Monitor before scaling.
