# Session 2026-05-11 - Research Committee Brain

## Why

We reviewed the TradingAgents pattern and copied the parts that fit Poly1 safely:

- separate bull, bear, risk, and portfolio-manager views;
- structured research output that other agents can read;
- memory/reflection tables for lessons learned;
- a hard distinction between research approval and live-trading approval.

We did not import TradingAgents as a live executor. That project is useful as a
research architecture, but our live capital path must remain controlled by our
own backtest, risk gate, order adapter, and manual activation process.

## What changed

- Added `agents/application/research_committee.py`.
- Connected `scripts/python/scout.py` to write committee reports into
  `data/scout.db` table `research_reports`.
- Added `decision_reflections` to `agents/application/trade_log.py` so agents
  can store lessons against decisions and outcomes.
- Added focused tests:
  - `tests/test_research_committee.py`
  - updated `tests/test_brain_journal.py`

## Safety contract

The committee is read-only.

- It does not place orders.
- It does not edit `.env`.
- It does not allocate capital.
- It does not approve live trading.
- `approved_for_live` is intentionally hard-blocked to `0`.

Current strategy posture encoded in the committee:

- `mean_reversion`: reject live; changed variant requires new backtest evidence.
- `market_maker`: paper-trade only until execution is proven.
- `nothing_happens`: watchlist plus split-window backtest required.
- `btc_daily`: small live probe can exist outside this committee, but sizing is
  not expanded by committee output.

## Verification

Commands run:

```bash
python3 -m py_compile agents/application/research_committee.py agents/application/trade_log.py scripts/python/scout.py
python3 -m unittest tests.test_research_committee tests.test_brain_journal -v
python3 scripts/python/scout.py --db-path /tmp/poly1_scout_research_test.db --max-markets 50 --news-top-n 0 --json
python3 scripts/python/scout.py --db-path data/scout.db --max-markets 100 --news-top-n 0 --json
```

Results:

- compile passed;
- 6 focused tests passed;
- temporary scout DB wrote 1 research report;
- real `data/scout.db` wrote 1 research report.

Latest real report:

```text
bitcoin-up-or-down-on-may-11-2026
strategy: mean_reversion
final_action: reject_live_backtest_required
final_score: 0.086
risk_score: 0.64
approved_for_live: 0
```

## Operator query

```bash
sqlite3 data/scout.db "
SELECT id, created_ts, market_slug, strategy_match, final_action,
       final_score, risk_score, approved_for_live
FROM research_reports
ORDER BY id DESC
LIMIT 10;"
```

## Next steps

1. Add dashboard panels for `research_reports`.
2. Have `state_watcher.py` include new high-risk/high-score research reports in
   alerts.
3. Feed `decision_reflections` from realized trade/postmortem jobs.
4. Promote only strategy variants that pass the existing backtest gate and then
   a small live probe.
