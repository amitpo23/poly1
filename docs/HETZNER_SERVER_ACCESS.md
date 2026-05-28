# HETZNER_SERVER_ACCESS.md — Agent runbook for the new production server

**Audience:** every coding agent (Claude Code, Codex, Cursor, others) that needs to interact with poly1's production server.

**Migrated:** 2026-05-28 (from Kamatera `83.229.82.193` to Hetzner `167.233.27.32`).

This doc supersedes any older references to Kamatera. If you see `83.229.82.193` or `trader@83.229.82.193` in code/docs, that is the **old** server (kept up as fallback for 24-48h after migration, then terminated by the operator).

---

## TL;DR

```bash
ssh poly1                                 # connects to 167.233.27.32 as root
ssh poly1 'cat /srv/poly1/data/HALT'      # must exist; trades blocked
ssh poly1 'cd /srv/poly1 && docker compose ps'
```

The bot is **HALT + freeze** by default. **Never** `EXECUTE=true`, `runtime_control.py live`, or `rm /srv/poly1/data/HALT` without operator explicit consent in chat.

---

## 1. SSH

| Field | Value |
|---|---|
| Host alias | `poly1` (configured in your `~/.ssh/config`) |
| HostName | `167.233.27.32` |
| User | `root` |
| Key | `~/.ssh/id_ed25519` (key fingerprint matches `poly1-deploy`) |
| Port | 22 (default) |

The alias was registered by `scripts/migrate_to_hetzner.sh` step 8.5. If `ssh poly1` fails:
```bash
grep -A5 "^Host poly1" ~/.ssh/config         # verify alias exists
ssh root@167.233.27.32 'hostname'             # fall back to raw IP
```

If neither works, the operator either rotated the IP (check Hetzner Console) or removed the alias (re-run migration script's step 8.5 against the current IP).

---

## 2. What lives where

| Path on server | What it is | Owner UID | Modify? |
|---|---|---|---|
| `/srv/poly1/` | **Main trading bot.** Mirrors `/Users/mymac/coding/poly1` on the operator's Mac. | `10001:10001` (trader) | Code only via `git pull` then `docker compose up -d --build` |
| `/srv/poly1/.env` | Live wallet private key + API keys (Anthropic, OpenAI, Tavily, Alpaca, Builder relayer). Mode `600`. | `10001:10001` | Never. Operator-only. |
| `/srv/poly1/deploy/.env.runtime` | 200+ runtime tuning vars (gates, thresholds, brain weights). Mode `600`. Hot-reloaded by `runtime_control.py`. | `10001:10001` | Only via `runtime_control.py`. Never edit directly. |
| `/srv/poly1/data/HALT` | Marker file. When present, `risk_gate.py` blocks all live entries. **Block contract is the file's existence; contents are operator-readable explanation.** | `10001:10001` | Never delete without operator approval in chat. |
| `/srv/poly1/data/runtime_control.json` | `{"mode": "freeze"|"live"|"live-hour"|"live-30m", ...}`. Source of truth for trading mode. | `10001:10001` | Via `runtime_control.py` only. |
| `/srv/poly1/data/trade_log.db` | SQLite ledger (4549+ trades, 77k+ decision_journal, 48k+ orderbook_snapshots). | `10001:10001` | Read-only for agents; only the bot writes. |
| `/srv/poly1/data/external_convictions_*.jsonl` | 14 signal archives. ~60 MB total. Live-written by EC services. | `10001:10001` | Read-only. |
| `/srv/poly1/data/migration_archive/` | Audit of Kamatera state at migration time (8.7 MB of patches, status, env_baks, home backups). | `10001:10001` | Read-only audit trail. |
| `/srv/swarm/` | Sister Polymarket bot. Shares deposit wallet with poly1. `BOT_MODE=dryrun` (was `live` on Kamatera, flipped down for safety). | `999:999` (swarm) | Operator-only. |
| `/srv/hermes-telegram/` | **Hotel-booking** Telegram concierge bot. Unrelated to trading. Standalone container with own Dockerfile + compose. Polling Telegram 24/7. | `root` | Operator-only. |

UID note: poly1 containers run as `trader` (UID 10001), swarm runs as `swarm` (UID 999), hermes runs as `root` inside its own container. If you create new files under `data/`, make sure ownership matches (`chown 10001:10001 path`) or the bot can't write.

---

## 3. Server-first verification (per `POLY1_WORKING_DISCIPLINE.md` rule 5 + 7)

**Before** any recommendation or change touching trading state, run this block. Treat each line as required, not optional:

```bash
ssh poly1 'ls -la /srv/poly1/data/HALT 2>&1'                                                  # HALT marker
ssh poly1 'python3 -c "import json; d=json.load(open(\"/srv/poly1/data/runtime_control.json\")); print(\"mode=\"+d.get(\"mode\",\"?\"))"'  # runtime mode
ssh poly1 'cd /srv/poly1 && git rev-parse --short HEAD && git log -1 --format=%s'             # what code is deployed
ssh poly1 'cd /srv/poly1 && docker compose ps --format "table {{.Name}}\t{{.Status}}"'         # which services are up
ssh poly1 'docker ps --filter name=hermes-telegram --format "{{.Names}} {{.Status}}"'         # hermes alive?
ssh poly1 'df -h / | tail -1 && free -h | grep Mem'                                           # disk + memory
```

Required state after migration (2026-05-28):

- HALT file: present
- Mode: `freeze`
- All poly1 services: stopped (Exited) until operator brings them up
- hermes-telegram: `Up <duration>`
- Disk: <30% used
- Memory: 720Mi-1.5Gi used out of 3.7Gi

---

## 4. Inside a running container (rule 8 — never trust local `.env.runtime`)

After 2026-05-27, container environment overrides `.env.runtime` on disk for already-running containers. To verify what a service actually sees:

```bash
ssh poly1 'docker compose exec -T trader printenv | grep RUNTIME_MODE'
ssh poly1 'docker compose exec -T scanner-executor printenv | grep SCANNER_EXECUTOR_'
ssh poly1 'docker exec -t poly1-btc5min-timed cat /app/data/HALT'
```

If a runtime gate has changed via `runtime_control.py` but you don't see the new value via `exec printenv`, the container was started before the change. Restart it: `docker compose up -d --force-recreate <service>`. **Never** during an active live window.

---

## 5. Where the secrets are (and what to do if you "find" them)

You will see secrets in plaintext if you grep around `.env` or run `docker compose config`:

- `POLYGON_WALLET_PRIVATE_KEY` (66 chars)
- `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `TAVILY_API_KEY`
- `BUILDER_API_KEY` / `BUILDER_SECRET` / `BUILDER_PASS_PHRASE`
- `ALPHAINSIDER_*` tokens
- `ALPACA_API_KEY_ID` / `ALPACA_API_SECRET_KEY`
- `POLYMARKET_DEPOSIT_WALLET` (public address, but operator-identifying)

**Never:**
- Echo secret values into chat (operator can see them; logs and screenshots cannot)
- Paste into commits, gists, pastebins, or any tool that uploads to a third-party service
- Hard-code into Python files — `.env` + `env_file` is the only path
- Send by email, Slack, or any messaging tool

**Operationally OK:**
- Display **lengths** (e.g. `awk -F= '/PRIVATE_KEY/{print $1"="length($2)"chars"}' .env`)
- Reference by name in code/docs (`POLYGON_WALLET_PRIVATE_KEY`)
- Mention which keys are set vs unset (e.g. "NEWSAPI_API_KEY is empty")

The same hygiene applies to `/srv/swarm/.env` and `/srv/hermes-telegram/.env`.

---

## 6. Bringing services up safely

The bot is currently **frozen + HALTed**. Services are not running. Before bringing anything up:

1. Confirm with operator in chat.
2. Verify HALT is still present (rule 5 above).
3. Decide the profile to launch (one of the 21 profiles in compose — see `docs/AGENTS_MAP.md`).

Safe smoke test (read-only, no trades possible):
```bash
ssh poly1 'cd /srv/poly1 && docker compose --profile dashboard up -d live-dashboard'
# → exposes :8090. Open via SSH tunnel: ssh -L 8090:127.0.0.1:8090 poly1
```

Risk-low (data writers, no trades):
```bash
ssh poly1 'cd /srv/poly1 && docker compose --profile external_conviction up -d'
# → 15 EC services start writing convictions_*.jsonl. They DO NOT execute trades —
# scanner_executor is the only thing that turns convictions into orders, and it's
# off until operator runs runtime_control.py to switch mode.
```

Anything that could trade (entry agents, scanner-executor, scalper, btc_5min, btc_daily, btc5min_timed*, news_shock) **must not** be brought up by an agent without explicit operator instruction in chat for that specific service.

---

## 7. Don't-touch list

These will destroy money, identity, or audit trail:

- `/srv/poly1/.env` — wallet key. Don't read aloud, don't modify, don't `cp` to anywhere outside the server.
- `/srv/poly1/data/HALT` — don't delete.
- `/srv/poly1/data/runtime_control.json` — don't edit directly. Use `scripts/runtime_control.py`.
- `/srv/poly1/data/trade_log.db` — don't write, don't `VACUUM`, don't restore old copies.
- `/srv/poly1/data/migration_archive/` — read-only audit trail. Don't delete.
- `/srv/poly1/docker-compose.yml` `x-ec-base` anchor — collapse-edit carefully, equivalence-check via `docker compose config` diff.
- `/srv/swarm/.env` — same wallet key as poly1's `.env`. Same rules.
- Service names in `docker-compose.yml` — operator muscle memory. Don't rename.
- `agents/application/risk_gate.py`, `execution_lock.py`, `execution_safety.py` — money-handling invariants per `CLAUDE.md`.
- `agents/polymarket/polymarket.py` `execute_market_order` + side/token mapping — paired with `prompts.py:one_best_trade`. Changing one without the other = silently wrong trades.
- `agents/application/trade_log.py` `ACTIVE_STATUSES` constant — the dedupe contract. Removing entries opens a double-fill window.
- `tenacity` retry predicate on order placement — adding `HTTPError` / `Exception` can double-submit orders (CLOB has no retract for FOK).

---

## 8. Common operations cheat sheet

```bash
# Logs of a service
ssh poly1 'docker logs poly1-trader --tail 50 --timestamps'
ssh poly1 'docker logs poly1-scanner-executor --since 1h'

# Resource usage
ssh poly1 'docker stats --no-stream | head'

# Database row counts
ssh poly1 'sqlite3 /srv/poly1/data/trade_log.db "SELECT status, COUNT(*) FROM trades GROUP BY status ORDER BY 2 DESC;"'

# Today's PnL
ssh poly1 'docker run --rm -v /srv/poly1/data:/data poly1:local python scripts/python/cli.py inspect-trades --limit 30'

# Heartbeats freshness (which agents are alive)
ssh poly1 'cd /srv/poly1/data && NOW=$(date +%s); for hb in *_heartbeat; do echo "$(( (NOW - $(stat -c %Y $hb))/60 ))min $hb"; done | sort -n | head -20'

# Live dashboard via SSH tunnel (no public port exposure)
ssh -L 8090:127.0.0.1:8090 poly1
# then in browser: http://localhost:8090
```

---

## 9. What if `ssh poly1` doesn't work

Likely causes, in order:

1. Mac's `~/.ssh/config` was regenerated without the `poly1` stanza. Re-add:
   ```
   Host poly1
       HostName 167.233.27.32
       User root
       IdentityFile ~/.ssh/id_ed25519
       ServerAliveInterval 30
       StrictHostKeyChecking accept-new
   ```
2. The Hetzner server got a new IP (rare — Hetzner Primary IPs are stable). Check Hetzner Console.
3. The operator's `id_ed25519` key got rotated. Public key on file in Hetzner Console under "Security → SSH Keys" → `poly1-deploy`.
4. Hetzner cloud firewall blocks your egress IP. Hetzner Console → Firewalls.

For all four: tell the operator. Do not attempt to bypass.

---

## 10. Cross-references

- `CLAUDE.md` — global working agreement
- `docs/POLY1_WORKING_DISCIPLINE.md` — the 8 rules this doc enforces (especially rules 5, 6, 7, 8)
- `docs/AGENTS_MAP.md` — what each module in `agents/application/` does
- `docs/SPEC.md` — module contracts and invariants
- `scripts/migrate_to_hetzner.sh` — the script that built this server (idempotent; re-runnable for a future move)
- `docs/SESSION_2026-05-28_REORG.md` — the migration + reorg session that established this layout
- `docs/MIGRATION_LOG_2026-05-06.md` — the earlier swarm-unification migration (different scope, kept for history)

---

## 11. After-action: was anything missed by this doc?

If you're a future agent and you find yourself doing something on Hetzner that this doc doesn't cover, append a section here in your session's PR. The goal is that the next agent's first question ("how do I talk to the server?") has a complete answer.
