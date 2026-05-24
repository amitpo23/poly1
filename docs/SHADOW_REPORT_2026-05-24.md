# Agent Edge Report — last 7 days
_Generated: 2026-05-24T08:02:42.995942+00:00_

## Approvals per agent (brain_decisions)

| Agent | Approvals |
|---|---:|
| market_scanner | 18894 |
| external_conviction_api | 3269 |
| scalper | 2786 |
| external_conviction_crypto_tape | 897 |
| btc_5min | 740 |
| position_manager | 212 |
| scanner_executor | 115 |
| external_conviction_divergence | 103 |
| position_manager_llm | 63 |
| external_conviction_aggregator | 60 |
| external_conviction_debate | 52 |
| btc_daily | 25 |
| external_conviction | 21 |
| crypto_5m_market_maker_shadow | 16 |
| external_conviction_openbb | 4 |

## Hypothetical 5-min edge per signal source

Computed from `decision_journal` ENTER/SHADOW_ENTER rows with `outcome_5m_json`.
PnL approximation: BUY → (best_bid / entry) − 1; SELL → ((1−best_bid) / (1−entry)) − 1.

| Signal source | Decisions | In band | With markout | Wins | Losses | Avg PnL% | In-band PnL% |
|---|---:|---:|---:|---:|---:|---:|---:|
| meta_brain | 53 | 2 | 0 | 0 | 0 | — | — |
| opportunity_factory,alphainsider_proven,crypto_tape | 47 | 16 | 16 | 7 | 9 | +6.94% | +21.44% |
| meta_brain,manifold,manifold:manifold | 8 | 4 | 1 | 0 | 1 | -2.13% | -2.13% |

## Notes

- Only sources with **with_markout ≥ 30** carry statistical weight; smaller samples are noisy.
- `in_band` = entry price in profit zone (BUY 0.40-0.49 or SELL 0.51-0.60).
- 5m markout is a proxy for short-term edge, not a substitute for closed-trade PnL.
- See `scripts/performance_tearsheet.py` for actual realised PnL.
