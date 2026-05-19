# Trade Quality Improvements - 2026-05-19

This document records the post-live audit decisions from the three agent reviews.

## Decision

The trading system must measure signal quality before increasing aggressiveness.
Fast trading is still the strategy, but no live entry should be treated as high
confidence until the provider behind the signal has evidence in the journal.

Final priority order:

1. Backtest external conviction JSONL against trade outcomes.
2. Apply spread-normalized EV gates before entry.
3. Use Kelly sizing only from calibrated/internal probability, with conservative caps.
4. Log provider/source attribution for continuous feedback.

## Implemented

### Expert Arbitration

MetaBrain now has an `EvidenceRouter` layer before the ordinary consensus
fallback.

The rule is no longer "average every component".  The rule is:

1. Ignore sources that have no evidence.
2. Let a single expert lead only when it has measured reliability.
3. Block or defer when a similarly reliable expert conflicts.
4. Fall back to informed-only consensus when no solo expert exists.

Solo expert defaults:

```text
EXPERT_SOLO_MIN_PROB="0.65"
EXPERT_SOLO_MIN_WINRATE="0.65"
EXPERT_SOLO_MIN_WILSON="0.58"
EXPERT_SOLO_MIN_SAMPLES="30"
EXPERT_SOLO_MAX_AGE_SEC="3600"
```

This means a high unproven score is not an anchor.  A wallet/provider can lead
alone only after its own `signal_source` has enough resolved outcomes in
`brain_decisions`.

There is one explicit exception: externally verified wallet performance.  If a
wallet signal carries an external win-rate, trade count, and profit from a
trusted Polymarket/on-chain analytics source, it can qualify as a solo expert
before poly1 has local history for that exact wallet.

Default external-wallet proof:

```text
EXPERT_WALLET_EXTERNAL_MIN_WINRATE="0.70"
EXPERT_WALLET_EXTERNAL_MIN_TRADES="50"
EXPERT_WALLET_EXTERNAL_MIN_PROFIT_USDC="100"
```

Important operational nuance: the public Polymarket leaderboard currently
provides PnL/volume/rank but may not provide win-rate. PnL alone is stored for
auditing, but it is not enough for solo authority.  A real solo wallet trigger
requires win-rate + sample size, either from our own `brain_decisions` history
or from an external verified wallet analytics source.

`wallet_follow` now tags decisions as:

```text
wallet:<address>
```

so individual smart wallets can earn or lose trust over time.

### Provider Backtesting

Added:

```bash
python scripts/python/backtest_external_convictions.py \
  --db data/trade_log.db \
  --glob 'data/external_convictions*.jsonl' \
  --max-age-hours 24 \
  --json
```

The tool reads `external_convictions*.jsonl`, including `SHADOW_BUY_*` plans,
and matches them to terminal outcomes in `trades` by `token_id` or `market_id`.
It reports win-rate by provider/source and confidence bucket.

Initial local result:

- `public_news`: 39 signals
- 17 matched outcomes within 24h
- 0 wins, 17 losses
- win-rate: 0%
- PnL: about `-0.35898 USDC`

Operational implication: `public_news` should not receive positive Kelly
weight or elevated MetaBrain trust without new evidence.

### Spread-Normalized EV Gate

MetaBrain now calculates:

```text
raw_ev = (internal_probability - entry_price) / entry_price
```

Default gate:

```text
META_BRAIN_MIN_RAW_EV="0.04"
```

This rejects trades where the price/spread makes expected value too weak even
when the raw probability edge appears positive.

### Kelly Sizing

Added `agents/application/sizing.py`.

Entry agents can now size from binary Kelly math using the MetaBrain/internal
probability and live entry price. Defaults are conservative:

```text
KELLY_SIZING_ENABLED="true"
KELLY_FRACTION_SCALE="0.25"
MAX_AGENT_ALLOCATION_FRACTION="0.50"
KELLY_MIN_POSITION_USDC="0"
```

Important: Kelly depends on signal quality. Provider backtesting must guide
which sources are trusted before increasing position sizes.

### Outcome Logging

`brain_decisions` now has `signal_source`.

Agents write source attribution for decisions so future outcome reconciliation
can answer which provider actually predicted correctly.

### Partial Exit Policy

Partial exits are deliberately deferred at the current bankroll scale.

The mechanism remains available, but default activation now requires:

```text
MAINTAIN_PARTIAL_TAKE_PROFIT_MIN_POSITION_USDC="500.0"
```

Reason: splitting `$3-$5` positions into `$1.50-$2.50` exits creates more CLOB
friction than value. Partial exits only make sense at larger position sizes.

## Explicitly Deferred

### Correlation Tracking

Deferred. The observed loss pattern was not cross-market correlation; it was
re-entry into the same market after close. Cooldown and concentration guards are
the correct near-term fix.

### LLM Ensemble

Deferred. `position_manager` already has an Anthropic fallback, and the current
bottleneck is signal quality, not LLM availability.

### Later Optimizations

Worth revisiting after provider quality data accumulates:

- market age scoring
- near-resolution volume spike detection
- dynamic HALT thresholds
- market universe freshness scoring

## Verification

Current test suite after implementation:

```text
486 tests OK
```

Relevant commits:

- `79f12b8 feat: improve trade quality gates and exits`
- `6fc8ff9 feat: backtest external conviction providers`
- `02bf5b4 chore: defer partial exits to larger positions`
