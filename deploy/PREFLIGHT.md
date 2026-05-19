# PREFLIGHT — checklist before flipping `EXECUTE=true`

Run through every box. Each must be true before live trading. **If any item fails, do not enable execution.**

## Configuration

- [ ] `.env` exists at `/srv/poly1/.env` with permissions `600`
- [ ] `POLYGON_WALLET_PRIVATE_KEY` set, wallet known to operator
- [ ] `POLYMARKET_FUNDER` set if using a Privy/Magic (Google/email login) account — must equal the proxy address shown on `polymarket.com/settings`
- [ ] `OPENAI_API_KEY` set, account has positive credit balance
- [ ] `OPENAI_MODEL` set (recommended live quality default: `gpt-4o`)
- [ ] `ANTHROPIC_MODEL` set for fallback when OpenAI quota/rate limits fail
- [ ] `STARTING_BALANCE_USDC` set to the wallet's actual balance
- [ ] `MAX_POSITION_FRACTION` ≤ 0.05 for the first $50 run
- [ ] `MIN_CONFIDENCE` ≥ 0.65
- [ ] `MAX_DAILY_LOSS_PCT` ≤ 0.10
- [ ] `MIN_USDC_FLOOR` set to wallet floor (e.g., 10)
- [ ] `MAX_DAILY_TOKEN_USD` set (recommended: 5)
- [ ] `TG_BOT_TOKEN` and `TG_CHAT_ID` set
- [ ] `HEALTHCHECK_URL` set (Healthchecks.io or equivalent)

## Wallet & on-chain

- [ ] Wallet (proxy in POLY_PROXY mode, EOA otherwise) has the intended collateral balance on Polygon mainnet:
      pUSD `0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB` for proxy mode, USDC.e `0x2791…84174` for EOA mode
- [ ] In **EOA mode only**: wallet has ≥ 0.5 MATIC for gas. POLY_PROXY orders are gasless via Polymarket's relayer.
- [ ] **CTF / USDC approvals** (one-time, irreversible on-chain tx, ~$0.10 gas):
      - **POLY_PROXY mode**: SKIP. Polymarket auto-sets allowances when the proxy is deployed; `_init_approvals` is a no-op when `POLYMARKET_FUNDER` is set.
      - **EOA mode** (legacy): run once per wallet:
        ```
        docker compose run --rm trader python -c \
          "from agents.polymarket.polymarket import Polymarket; p = Polymarket(live=True); p._init_approvals(True)"
        ```
        Record the resulting tx hashes in your operator notes.

## End-to-end dry runs

- [ ] **Stage 0** (local): `docker compose run --rm trader python deploy/run.py` runs at least one full dry-run cycle, `data/trade_log.db` contains `skipped_dry_run` rows
- [ ] **Stage 1** (VPS): `docker compose run --rm trader python deploy/run.py` completes one cycle, no errors
- [ ] **Stage 1.5** (shadow mode): 24h with `EXECUTE=false`. Inspect the journal:
      ```
      docker compose run --rm trader python scripts/python/cli.py inspect-trades --limit 50
      ```
      Verify: trades target *different* markets across cycles, `token_id` matches `side` semantics, `confidence` is non-degenerate, and a reasonable count (3+) of `skipped_dry_run` rows accumulated.

## Operations

- [ ] `docker logs poly1 --tail 50` shows JSON heartbeat lines, no stack traces
- [ ] Telegram test message received (start-up notify)
- [ ] Healthchecks.io shows green ping
- [ ] Kill switch test: `touch /srv/poly1/data/HALT`, wait one cycle, confirm `skipped_gate` row appears, then `rm /srv/poly1/data/HALT`
- [ ] `docker compose down && docker compose up -d` — heartbeat resumes within 2 minutes
- [ ] `df -h` reports < 30% used; `docker system df` reasonable
- [ ] Daily backup cron added on VPS:
      `0 * * * * sqlite3 /srv/poly1/data/trade_log.db ".backup /srv/poly1/data/trade_log.bak"`
- [ ] Off-VPS rsync running on operator machine:
      `*/30 * * * * rsync -az trader@<vps>:/srv/poly1/data/ ~/poly1-backups/`

## Strategy validation gate (Stage 3, before scaling capital)

- [ ] After 24h of `EXECUTE=true` with $50 capital and `MAX_POSITION_FRACTION=0.05`:
  - [ ] ≥ 4 trades attempted (live, not dry-run rows)
  - [ ] PnL ≥ -5% of starting balance
  - [ ] Hit rate ≥ 50% on resolved trades (resolved trades may take days; defer this gate if too few resolved)
- [ ] If trade count is low: extend or relax `MIN_CONFIDENCE`
- [ ] If PnL < -5%: STOP, lower `MAX_POSITION_FRACTION` to 0.02, analyze the journal before continuing

## Scale to $200

Only after Stage 3 passes its gates.
