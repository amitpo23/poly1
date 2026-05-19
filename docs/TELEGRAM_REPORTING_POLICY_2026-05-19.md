# Telegram Reporting Policy — 2026-05-19

## Purpose

Telegram is the live operator dashboard for poly1. It must provide immediate
trade visibility without sending noisy signal/skip messages.

## Required Environment

- `TG_BOT_TOKEN`: Telegram bot token.
- `TG_CHAT_ID`: target chat id for reports and alerts.
- `TELEGRAM_TRADE_ALERTS=true`: send every live buy/fill and sell/exit.
- `TELEGRAM_REPORT_SECONDS=3600`: send a full report once per hour.
- `TELEGRAM_REPORT_SEND_ON_START=true`: send a report immediately when the
  reporter starts, then continue hourly.
- `TELEGRAM_DIRECT_NOTIFICATIONS=false`: keep ad-hoc non-trade messages off.

## What Sends Immediately

- Entry/fill events: every bought position.
- Exit events: take-profit, stop-loss, timeout, dust/close attempts where the
  position manager emits a close notification.
- Critical events: halt/error events remain allowed.

## What Does Not Send Immediately

- Skipped gates.
- Brain checks with no trade.
- Market scanner candidates.
- Shadow-only evidence.

## Hourly Report

The `telegram-reporter` service runs:

```bash
python scripts/python/telegram_report.py --daemon
```

It sends a full PnL/dashboard report every hour using the shared trade ledger
and runtime state.

## Operational Notes

- A new Telegram bot cannot message a user until the user sends `/start` to the
  bot, or until the bot is added to the target group.
- If `sendMessage` returns `chat not found`, refresh `TG_CHAT_ID` from
  Telegram `getUpdates` after `/start`.
- The server is the source of truth for Telegram env and live trade data:
  `/srv/poly1/.env`, `/srv/poly1/deploy/.env.runtime`, and
  `/srv/poly1/data/trade_log.db`.

## Verification

Verified on 2026-05-19:

- `TG_BOT_TOKEN` is present in the `telegram-reporter` container.
- `TG_CHAT_ID` is present in the `telegram-reporter` container.
- `TELEGRAM_TRADE_ALERTS=true`.
- `TELEGRAM_REPORT_SECONDS=3600`.
- `TELEGRAM_REPORT_SEND_ON_START=true`.
- Telegram API `sendMessage` returned `200 OK`.
