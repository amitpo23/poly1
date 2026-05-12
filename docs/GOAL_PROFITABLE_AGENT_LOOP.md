# /goal: Profitable Trading For Every Approved Agent

Date: 2026-05-12

## Goal

Build a disciplined loop that keeps improving each approved trading agent until
it either proves positive expected value or remains unfunded.

The goal is **not** "trade more". The goal is:

> Every live-funded agent must earn the right to trade by showing repeatable,
> measured profitability after costs, slippage, and errors.

No agent receives more capital because it sounds promising. It receives capital
only after evidence.

## Agent States

Each agent must be in exactly one state:

| State | Meaning | Capital |
|---|---|---|
| `research` | Idea/source exists, but no proven trade rule | $0 |
| `backtest` | Needs historical/analog validation | $0 |
| `paper` | Produces signals, no live orders | $0 |
| `live_probe` | Tiny live size to test execution reality | capped |
| `profitable` | Meets profitability criteria | eligible to scale |
| `disabled` | Negative EV, broken, or unsafe | $0 |

## Profitability Criteria

An agent is **profitable** only when all conditions are true:

- Minimum sample: at least 20 closed/live-probe trades, unless manually marked
  as a special low-frequency strategy.
- Realized PnL after costs is positive.
- Average realized PnL per trade is positive.
- Win rate is not used alone. EV must be positive:

```text
expected_value = estimated_true_probability
  - entry_price
  - slippage
  - error_margin
```

- No unresolved operational blocker:
  - OpenAI quota cannot be the reason for missing intelligence.
  - Repeated CLOB/404/FAK failures must be resolved.
  - No repeated dust/failed-exit loops.
  - No stale submitted orders blocking the agent.

## Current Agent Goal Map

| Agent | Current role | Goal loop |
|---|---|---|
| `trader` | AI/RAG generalist | Fix intelligence availability, then route only EV-positive opportunities to paper/live_probe |
| `btc_daily` | Small live directional probe | Continue small live sample; evaluate closed PnL and market regime |
| `scalper` | Shadow/research | Keep shadow until spread/slippage model shows positive EV |
| `near_resolution` | Research/live-ready gate | Improve evidence scoring; live_probe only when confidence and EV pass |
| `news_signal` | Research source, not trader | Produce reliable classified evidence; classifier failures do not count |
| `news_shock` | Event-driven trader | Needs non-OpenAI fallback or restored quota before live edge can appear |
| `wallet_watcher` | Source collector | Find wallets that actually emit fresh followable trades |
| `wallet_follow` | Copy/analog trader | Paper/live_probe only when wallet signal has positive EV and liquidity |
| `swarm_market_maker` | Small live maker probe | Add exit/settlement release so safety brake does not freeze the market forever |
| `swarm_mean_reversion` | Research/paper | Needs historical proof before capital |
| `swarm_nothing_happens` | Research/paper | Needs source reliability and EV before capital |
| `swarm_ai_decision` | Research | Blocked if AI quota unavailable; no blind live trading |

## Loop

Run this loop until every approved agent is `profitable` or `disabled`:

1. **Observe**
   - Read recent trades, fills, exits, skipped gates, brain decisions, news
     signals, wallet signals, and swarm orders.
   - Produce a per-agent status report.

2. **Diagnose**
   - Classify the blocker:
     - no candidates
     - no intelligence
     - no positive EV
     - bad liquidity/spread
     - execution failure
     - exit failure
     - insufficient sample

3. **Patch**
   - Fix infrastructure and observability blockers first.
   - Improve strategy logic only when the failure is strategic.
   - Do not lower gates just to create trades.

4. **Probe**
   - Promote only one step at a time:
     - `research -> backtest`
     - `backtest -> paper`
     - `paper -> live_probe`
     - `live_probe -> profitable`

5. **Allocate**
   - Capital allocator gives money only to agents in `live_probe` or
     `profitable`.
   - Losing or unproven agents remain at $0 or tiny probe size.

6. **Learn**
   - Every missed profit, stop loss, false positive, failed exit, and rejected
     candidate must become DB evidence for the next cycle.

## Command

Use the read-only goal checker:

```bash
python scripts/python/goal_status.py --hours 24
```

Watch loop:

```bash
python scripts/python/goal_status.py --hours 24 --watch --interval 900
```

CI/automation style:

```bash
python scripts/python/goal_status.py --hours 168 --require-all-profitable
```

`--require-all-profitable` exits non-zero until every approved agent meets the
goal. That is intentional: it keeps the loop open.

## Non-Negotiables

- Do not claim an agent is profitable from open/unrealized PnL alone.
- Do not use win rate without price/EV.
- Do not scale after one lucky trade.
- Do not let failed classifiers count as intelligence.
- Do not let "healthy container" mean "healthy strategy".
- Do not risk more capital to compensate for weak edge.

