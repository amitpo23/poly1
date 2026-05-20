# Agent Strategy Audit

- Entry agents: 9
- Static issues: 2

| Agent | Compose | Brain | Risk | Journal | Shadow/Execute | Tests | Issues |
| --- | --- | --- | --- | --- | --- | --- | --- |
| trader | yes | yes | yes | no | yes | test_brain_journal.py, test_btc_5min.py, test_capital_allocator.py, test_executor.py, test_external_conviction.py, test_opportunity_router.py, test_polymarket_fak.py, test_runtime_control.py, test_trader.py, test_trading_policy_contract.py | brain_decision_journal_missing |
| scalper | yes | yes | no | yes | yes | test_brain_journal.py, test_capital_allocator.py, test_market_brain.py, test_market_universe.py, test_meta_brain.py, test_scalper.py, test_scalper_daemon.py, test_scalper_engine.py, test_scalper_pairs.py, test_trader.py, test_trading_policy_contract.py | risk_gate_not_detected |
| btc_daily | yes | yes | yes | yes | yes | test_btc_5min.py, test_btc_daily.py, test_market_brain.py, test_position_manager.py, test_trader.py, test_trading_policy_contract.py | none |
| btc_5min | yes | yes | yes | yes | yes | test_btc_5min.py, test_crypto_5m_market_maker_shadow.py, test_execution_quality.py, test_market_brain.py, test_market_universe.py, test_trading_policy_contract.py | none |
| near_resolution | yes | yes | yes | yes | yes | test_near_resolution.py, test_runtime_control.py, test_trader.py | none |
| news_shock | yes | yes | yes | yes | yes | test_news_shock.py, test_opportunity_router.py, test_trader.py, test_trading_policy_contract.py | none |
| wallet_follow | yes | yes | yes | yes | yes | test_meta_brain.py, test_trader.py, test_trading_policy_contract.py, test_wallet_follow.py | none |
| external_conviction_api | yes | yes | yes | yes | yes | test_backtest_external_convictions.py, test_external_conviction.py, test_trading_policy_contract.py, test_vibe_analysis.py | none |
| scanner_executor | yes | yes | yes | yes | yes | test_runtime_control.py, test_scanner_executor.py | none |

## Shadow Research Services

| Agent | Compose | Profile | Live orders |
| --- | --- | --- | --- |
| equity_options_fair_value | yes | research | no |
| crypto_5m_market_maker_shadow | yes | research | no |

This audit is static. A clean row means the wiring is visible in source; live readiness still requires runtime preflight.
