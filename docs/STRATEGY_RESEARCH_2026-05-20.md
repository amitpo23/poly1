# Strategy Research - 2026-05-20

Purpose: maintain a practical research backlog for poly1 after the
DecisionCouncil / shadow-probe work.  The goal is to find lawful,
testable edge sources and manipulation-detection defenses.  This is not a
playbook for spoofing, wash trading, or deceptive market activity.

## Research Rules

- Shadow first.  No strategy touches money until it produces auditable
  `decision_journal` rows and a post-shadow PnL check.
- Every strategy must price the executable book, not the displayed midpoint.
- Every strategy must model exit liquidity before entry.
- Every source gets a provider scorecard before it can become `expert_solo`.
- Manipulation research is defensive only: detect dirty books, stale quotes,
  wash-like flow, spoof-like depth, and adverse-selection traps.

## Priority Matrix

### 1. Cross-Venue Prediction Market Arbitrage

Sources:
- PMXT: multi-venue SDK, market matching, historical data, and trading across
  Polymarket/Kalshi/Limitless/etc.
- ArbEx / Arbitix / PredictWatch: commercial terminals for matched-market
  cross-venue spreads.
- ImMike/polymarket-arbitrage: Python bot for Polymarket/Kalshi matching and
  arbitrage detection.

Edge thesis:
- Same event trades at different prices on different venues.
- Retail-scale spreads can exist, but require fast matching, exact rule
  normalization, and execution-aware sizing.

Fit for poly1:
- High.  We already have market scanner, DecisionCouncil, net-EV gating, and
  provider scorecard.

Test:
- Add `cross_venue_matcher_shadow` that records matched markets, rule
  compatibility, best executable prices, fees, and synthetic hedge edge.
- Never trade unless payout definitions are equivalent.

Risks:
- False matches are worse than no trade.
- Kalshi availability/regulatory/account constraints must be handled cleanly.

### 2. External Fair Value From Liquid Underlyings

Sources:
- Instagram reel: equities/options fair-value model for Polymarket outcomes.
- Alpaca / FinancialDatasets / options chains / exchange prices.

Edge thesis:
- Polymarket can lag the liquid underlying market.
- For markets tied to stocks, indices, crypto, or options-implied outcomes,
  external fair value should dominate LLM intuition.

Fit for poly1:
- Very high.  This should become an expert source, not a weak weighted signal.

Test:
- Add `equity_options_fair_value_shadow`.
- Start with "largest company by date", stock above/below X, index close above
  X, and crypto up/down where liquid candles/options exist.
- Record fair probability, Polymarket executable ask, net EV, and later
  5/15/60 minute PnL.

Risks:
- Model overconfidence.  Bivariate/lognormal assumptions must be calibrated.
- Needs corporate-action and market-cap share-count correctness.

### 3. Orderbook Microstructure / Adverse Selection Defense

Sources:
- arXiv "Anatomy of a Decentralized Prediction Market": Polymarket book
  microstructure, depth patterns, public feed vs on-chain trade direction
  accuracy issues.
- Orderbook tick/backtesting discussions and PMXT archives.

Edge thesis:
- Most losses come from entering into stale/fragile books, not from lack of
  model cleverness.
- Our latest shadow probe confirmed this: no bid / price drift / immediate
  stop-like marks.

Fit for poly1:
- Mandatory.  This improves all strategies.

Test:
- Add `book_quality_score`: top-of-book depth, spread, bid decay, ask decay,
  quote age, imbalance, recent drift, and exitability.
- Require clean exit bid before any live entry.

Risks:
- Public book feed can mislead; prefer CLOB plus on-chain fill reconciliation.

### 4. Market-Neutral Maker / Merge Strategy

Sources:
- direkturcrypto/polymarket-terminal maker rebate MM:
  bid both sides, keep combined cost below payout, merge YES+NO back to USDC
  if both sides fill.

Edge thesis:
- Market-neutral spread capture; less dependent on direction.

Fit for poly1:
- Medium.  Interesting, but more operationally complex than our current
  directional stack.

Test:
- Shadow/sim only first.
- Need on-chain balance as source of truth, ghost-fill recovery, stuck-leg
  handling, and merge/redeem tests.

Risks:
- One-sided fills create directional exposure.
- Ghost fills / invalid txhash handling is critical.

### 5. Copy Trading / Proven Wallet Expert

Sources:
- PolyTerm whale tracking / insider scoring.
- polymarket-terminal copy trader.
- Existing `wallet_signals`, `WhaleSentimentReader`, and Expert Solo logic.

Edge thesis:
- Some wallets may have persistent informational edge in specific market
  categories.

Fit for poly1:
- High, but only if wallet proof is real.

Test:
- Build `wallet_profile_scorecard`: 30d/90d winrate, PnL, market clusters,
  hold time, entry lag, exit lag.
- Shadow follow first; never copy unverified wallets.

Risks:
- Late copying can be exit liquidity for the expert.
- Public wallet activity can be noisy or bait-like.

### 6. Sniper / Panic Dump Limit Orders

Sources:
- polymarket-terminal orderbook sniper.
- Reddit discussions around 1c/2c/3c bids and latency arb.

Edge thesis:
- Place deep passive bids to catch panic dumps or bot mistakes.

Fit for poly1:
- Low/medium.  It is less aligned with our current "fast planned entry/exit"
  style and can leave us with weird tail-risk positions.

Test:
- Shadow model only: record when a 1c/2c/3c bid would have filled and whether
  there was any exit bid or resolution value.

Risks:
- Most fills may be adverse selection.
- Must avoid markets with dispute/settlement risk.

### 7. Weather / Official Data Edge

Sources:
- Public examples of bots using NWS forecast updates vs Polymarket prices.

Edge thesis:
- Official or high-quality forecast data updates faster/more accurately than
  retail prediction-market prices.

Fit for poly1:
- Medium/high for weather markets, if we add reliable data ingestion.

Test:
- Shadow forecast model using official weather API, historical forecast error,
  and market rules.

Risks:
- Rule interpretation and station/timezone details can dominate the edge.

## Defensive Manipulation / Dirty-Market Detectors

These are allowed and recommended:

- Wash-like flow detector: repeated self/circular-looking fills, abnormal
  volume with no durable price movement.
- Spoof-like depth detector: large quote appears/disappears without fills.
- No-exit detector: cheap asks with no meaningful bid.
- Stale book detector: quote unchanged while related external market moves.
- Latency trap detector: market moved externally but Polymarket still shows old
  ask that cannot actually fill.
- Rule-risk detector: ambiguous market wording, UMA/dispute risk, settlement
  dependency.

These are not allowed:

- Spoofing, wash trading, fake liquidity, coordinated price manipulation,
  deceptive quote placement, or any attempt to mislead other traders.

## Recommended Next Implementation Order

1. Add a `book_quality_score` to DecisionCouncil and require clean exit bid.
2. Add `equity_options_fair_value_shadow` for externally-priced markets.
3. Add `cross_venue_matcher_shadow` using PMXT/allbets-style matching ideas.
4. Add wallet profile scorecard and only then let wallet evidence become solo.
5. Simulate maker/merge market-neutral strategy separately from directional
   scanner flow.

## Immediate Lesson From Current Shadow Probe

The 2026-05-20 shadow probe showed that execution plumbing works, but `meta_brain`
consensus overestimated probabilities.  Several shadow entries immediately
looked like stop-loss or no-exit situations.  Therefore, the next edge source
must be external and measurable, not another generic LLM vote.
