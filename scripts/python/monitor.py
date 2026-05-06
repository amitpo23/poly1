"""Unified monitor for both Polymarket bots.

Reads from:
- ~/coding/poly1/data/trade_log.db        (poly1)
- ~/Desktop/poly/bot/data/swarm.db         (swarm)
- ~/coding/poly1/data/heartbeat            (poly1 liveness)
- ~/Desktop/poly/bot/logs/bot.log          (swarm liveness)

Read-only. Safe to run any time, even when bots are live.

Usage:
    python ~/Desktop/poly/monitor.py            # one shot
    python ~/Desktop/poly/monitor.py --watch    # auto-refresh every 10s
    python ~/Desktop/poly/monitor.py --once --json   # machine-readable

If you want live wallet balances (requires web3 + RPC; read-only),
set POLY1_WALLET_ADDRESS and SWARM_WALLET_ADDRESS env vars.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

POLY1_DB = Path(os.path.expanduser("~/coding/poly1/data/trade_log.db"))
POLY1_HB = Path(os.path.expanduser("~/coding/poly1/data/heartbeat"))
SWARM_DB = Path(os.path.expanduser("~/Desktop/poly/bot/data/swarm.db"))
SWARM_LOG = Path(os.path.expanduser("~/Desktop/poly/bot/logs/bot.log"))

C_RESET = "\033[0m"
C_DIM = "\033[2m"
C_GREEN = "\033[32m"
C_RED = "\033[31m"
C_YELLOW = "\033[33m"
C_BOLD = "\033[1m"


def _heartbeat_age(path: Path) -> tuple[float | None, str]:
    if not path.exists():
        return None, f"{C_DIM}no heartbeat file{C_RESET}"
    age = time.time() - path.stat().st_mtime
    if age < 90:
        color = C_GREEN
    elif age < 600:
        color = C_YELLOW
    else:
        color = C_RED
    return age, f"{color}{int(age)}s ago{C_RESET}"


def _log_age(path: Path) -> tuple[float | None, str]:
    if not path.exists():
        return None, f"{C_DIM}no log{C_RESET}"
    age = time.time() - path.stat().st_mtime
    if age < 90:
        color = C_GREEN
    elif age < 1800:
        color = C_YELLOW
    else:
        color = C_RED
    return age, f"{color}{_human_age(age)}{C_RESET}"


def _human_age(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h ago"
    return f"{seconds / 86400:.1f}d ago"


def _color_pnl(value: float) -> str:
    if value > 0:
        return f"{C_GREEN}+${value:.2f}{C_RESET}"
    if value < 0:
        return f"{C_RED}-${abs(value):.2f}{C_RESET}"
    return f"$0.00"


def _connect_ro(path: Path) -> sqlite3.Connection:
    """Open a SQLite DB for dashboard reads without taking file locks.

    The sister swarm DB often sits outside this repo, and SQLite's normal
    locking can fail in sandboxed/read-only contexts. immutable=1 is correct
    for a monitor snapshot: it avoids lock files and never writes.
    """
    return sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)


def _tail_lines(path: Path, limit: int = 8) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [line.rstrip("\n") for line in lines[-limit:]]


def poly1_state() -> dict:
    out: dict = {"name": "poly1", "db_present": POLY1_DB.exists()}
    age, age_str = _heartbeat_age(POLY1_HB)
    out["heartbeat_age_s"] = age
    out["heartbeat"] = age_str
    if not out["db_present"]:
        out["error"] = "trade_log.db not found"
        return out

    midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    midnight_iso = midnight.isoformat()

    try:
        with closing(_connect_ro(POLY1_DB)) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            # by status
            counts = {}
            for row in cur.execute(
                "SELECT status, COUNT(*) AS n FROM trades GROUP BY status"
            ):
                counts[row["status"]] = row["n"]
            out["counts_total"] = counts

            # today's by status
            counts_today = {}
            for row in cur.execute(
                "SELECT status, COUNT(*) AS n FROM trades WHERE ts >= ? GROUP BY status",
                (midnight_iso,),
            ):
                counts_today[row["status"]] = row["n"]
            out["counts_today"] = counts_today

            # active positions (pending/submitted/filled/may_have_fired)
            row = cur.execute(
                "SELECT COUNT(*) AS n, SUM(size_usdc) AS total FROM trades "
                "WHERE status IN ('pending','submitted','filled','may_have_fired')"
            ).fetchone()
            out["active_count"] = int(row["n"] or 0)
            out["active_capital"] = float(row["total"] or 0.0)

            # last 5 trades for context
            recent = []
            for row in cur.execute(
                "SELECT ts, status, side, price, size_usdc, confidence, market_id, error "
                "FROM trades ORDER BY id DESC LIMIT 5"
            ):
                recent.append(dict(row))
            out["recent"] = recent
    except sqlite3.Error as e:
        out["error"] = f"sqlite: {e}"
    return out


def swarm_state() -> dict:
    out: dict = {"name": "swarm", "db_present": SWARM_DB.exists()}
    age, age_str = _log_age(SWARM_LOG)
    out["heartbeat_age_s"] = age
    out["heartbeat"] = age_str
    if not out["db_present"]:
        out["error"] = "swarm.db not found"
        return out

    midnight_ms = int(
        datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
    )

    try:
        with closing(_connect_ro(SWARM_DB)) as con:
            con.row_factory = sqlite3.Row
            cur = con.cursor()

            totals = {}
            for table in ("agent_state", "fills", "pending_orders",
                          "pnl_events", "nh_journal"):
                row = cur.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
                totals[table] = int(row["n"] or 0)
            out["table_counts"] = totals

            # PnL by agent today
            pnl_today = {}
            for row in cur.execute(
                "SELECT agent, SUM(pnl) AS pnl, COUNT(*) AS n "
                "FROM pnl_events WHERE ts_ms >= ? GROUP BY agent",
                (midnight_ms,),
            ):
                pnl_today[row["agent"]] = {
                    "pnl": float(row["pnl"] or 0.0),
                    "events": int(row["n"] or 0),
                }
            out["pnl_today_by_agent"] = pnl_today
            out["daily_pnl"] = sum(v["pnl"] for v in pnl_today.values())

            # fills today
            fills_today = {}
            for row in cur.execute(
                "SELECT agent, COUNT(*) AS n, SUM(size) AS sz "
                "FROM fills WHERE ts_ms >= ? GROUP BY agent",
                (midnight_ms,),
            ):
                fills_today[row["agent"]] = {
                    "fills": int(row["n"] or 0),
                    "size": float(row["sz"] or 0.0),
                }
            out["fills_today_by_agent"] = fills_today

            pending_by_status = {}
            for row in cur.execute(
                "SELECT status, COUNT(*) AS n FROM pending_orders GROUP BY status"
            ):
                pending_by_status[row["status"]] = int(row["n"] or 0)
            out["pending_by_status"] = pending_by_status

            unreconciled = []
            for row in cur.execute(
                "SELECT p.id, p.agent, p.market_id, p.side, p.outcome, "
                "p.price_cents, p.size_usd, p.order_id, p.updated_ms, p.note "
                "FROM pending_orders p "
                "LEFT JOIN fills f ON f.order_id = p.order_id "
                "WHERE p.status='submitted' AND f.order_id IS NULL "
                "AND COALESCE(p.order_id,'') NOT LIKE 'dry_%' "
                "ORDER BY p.updated_ms DESC LIMIT 10"
            ):
                unreconciled.append(dict(row))
            out["submitted_unreconciled"] = unreconciled
            out["submitted_unreconciled_count"] = len(unreconciled)
            out["submitted_unreconciled_usd"] = sum(
                float(r.get("size_usd") or 0.0) for r in unreconciled
            )

            # agent state — pull NothingHappens open positions
            nh_open = []
            last_state_ms = None
            for row in cur.execute(
                "SELECT agent, payload, updated_ms FROM agent_state"
            ):
                updated_ms = row["updated_ms"]
                if updated_ms and (last_state_ms is None or updated_ms > last_state_ms):
                    last_state_ms = updated_ms
                if row["agent"] == "nothing_happens":
                    try:
                        payload = json.loads(row["payload"])
                        positions = payload.get("positions", {})
                        for cid, pos in positions.items():
                            nh_open.append({
                                "slug": pos.get("slug"),
                                "size_usd": pos.get("size_usd"),
                                "no_entry": pos.get("no_entry_price"),
                                "filled": pos.get("filled"),
                                "end": (pos.get("end_date_iso") or "")[:10],
                            })
                    except (json.JSONDecodeError, AttributeError):
                        continue
            out["nh_open_positions"] = nh_open
            out["last_state_ms"] = last_state_ms
            out["last_state_utc"] = (
                datetime.fromtimestamp(last_state_ms / 1000, tz=timezone.utc).isoformat()
                if last_state_ms else None
            )

            recent_pending = []
            for row in cur.execute(
                "SELECT id, agent, market_id, side, outcome, price_cents, size_usd, "
                "status, order_id, created_ms, updated_ms, note "
                "FROM pending_orders ORDER BY id DESC LIMIT 5"
            ):
                recent_pending.append(dict(row))
            out["recent_pending"] = recent_pending

            recent_nh = []
            for row in cur.execute(
                "SELECT id, agent, market_id, slug, no_price_quoted, no_price_filled, "
                "rejected_count, opened_at_ms, last_check_ms, unrealized_pnl "
                "FROM nh_journal ORDER BY id DESC LIMIT 5"
            ):
                recent_nh.append(dict(row))
            out["recent_nh_journal"] = recent_nh

            # last 5 fills
            recent = []
            for row in cur.execute(
                "SELECT ts_ms, agent, side, outcome, price, size, fee "
                "FROM fills ORDER BY id DESC LIMIT 5"
            ):
                recent.append(dict(row))
            out["recent_fills"] = recent
    except sqlite3.Error as e:
        out["error"] = f"sqlite: {e}"
    out["recent_log"] = _tail_lines(SWARM_LOG, limit=8)
    return out


def render(p1: dict, sw: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = []
    lines.append(f"{C_BOLD}╭── Polymarket monitor — {now} ──╮{C_RESET}")
    lines.append("")

    # poly1
    lines.append(f"{C_BOLD}poly1{C_RESET}  (~/coding/poly1)   heartbeat: {p1['heartbeat']}")
    if "error" in p1:
        lines.append(f"  {C_RED}{p1['error']}{C_RESET}")
    else:
        ct = p1.get("counts_today", {})
        skip_dr = ct.get("skipped_dry_run", 0)
        sub = ct.get("submitted", 0)
        fil = ct.get("filled", 0)
        skg = ct.get("skipped_gate", 0)
        skd = ct.get("skipped_dedupe", 0)
        fail = ct.get("failed", 0)
        mhf = ct.get("may_have_fired", 0)
        lines.append(
            f"  today: submitted={sub} filled={fil} dry_run={skip_dr} "
            f"skip_gate={skg} skip_dedupe={skd} failed={fail}"
        )
        if mhf:
            lines.append(f"  {C_RED}⚠ MAY_HAVE_FIRED rows: {mhf} — verify on-chain{C_RESET}")
        lines.append(
            f"  active positions: {p1['active_count']}  "
            f"capital deployed: ${p1['active_capital']:.2f}"
        )
        if p1.get("recent"):
            lines.append(f"  {C_DIM}recent (last 5):{C_RESET}")
            for r in p1["recent"]:
                ts = (r["ts"] or "")[:19].replace("T", " ")
                size = f"${r['size_usdc']:.2f}" if r["size_usdc"] is not None else "  -  "
                price = f"{r['price']:.3f}" if r["price"] is not None else "  -  "
                conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else " -  "
                err = (r["error"] or "")[:30]
                lines.append(
                    f"    {ts}  {r['status']:<18} {(r['side'] or '-'):<5}"
                    f" p={price} sz={size} c={conf}  {err}"
                )

    lines.append("")

    # swarm
    lines.append(f"{C_BOLD}swarm{C_RESET}  (~/Desktop/poly/bot)   last log: {sw['heartbeat']}")
    if "error" in sw:
        lines.append(f"  {C_RED}{sw['error']}{C_RESET}")
    else:
        if sw.get("heartbeat_age_s") is None or sw.get("heartbeat_age_s", 0) > 1800:
            lines.append(
                f"  {C_RED}OFFLINE/STALE: no fresh swarm log in "
                f"{_human_age(sw.get('heartbeat_age_s') or 0)}{C_RESET}"
            )
        if sw.get("last_state_utc"):
            lines.append(f"  last DB state: {sw['last_state_utc']}")
        if sw.get("table_counts"):
            tc = sw["table_counts"]
            lines.append(
                f"  rows: fills={tc.get('fills', 0)} pending={tc.get('pending_orders', 0)} "
                f"pnl_events={tc.get('pnl_events', 0)} nh_journal={tc.get('nh_journal', 0)}"
            )
        daily = sw.get("daily_pnl", 0.0)
        lines.append(f"  daily PnL: {_color_pnl(daily)}")
        if sw.get("pending_by_status"):
            bits = " ".join(
                f"{status}={count}"
                for status, count in sorted(sw["pending_by_status"].items())
            )
            lines.append(f"  pending orders: {bits}")
        for agent, info in sw.get("pnl_today_by_agent", {}).items():
            lines.append(
                f"    {agent:<20} pnl={_color_pnl(info['pnl'])}  events={info['events']}"
            )
        for agent, info in sw.get("fills_today_by_agent", {}).items():
            lines.append(
                f"    {agent:<20} fills={info['fills']}  size=${info['size']:.2f}"
            )
        nh = sw.get("nh_open_positions", [])
        if nh:
            lines.append(f"  NothingHappens open positions ({len(nh)}):")
            for p in nh[:5]:
                filled = "FILLED" if p["filled"] else "pending"
                lines.append(
                    f"    {p['slug'][:40]:<40} ${p['size_usd']:.0f} "
                    f"NO@{p['no_entry']:.4f} {filled} ends {p['end']}"
                )
        if sw.get("recent_fills"):
            lines.append(f"  {C_DIM}recent fills (last 5):{C_RESET}")
            for r in sw["recent_fills"]:
                ts = datetime.fromtimestamp(r["ts_ms"] / 1000, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                lines.append(
                    f"    {ts}  {r['agent']:<16} {r['side']:<4} {r['outcome']:<5} "
                    f"p={r['price']:.4f} sz={r['size']:.2f} fee={r['fee']:.4f}"
                )
        if sw.get("recent_pending"):
            lines.append(f"  {C_DIM}recent pending/order ledger:{C_RESET}")
            for r in sw["recent_pending"]:
                ts = datetime.fromtimestamp(r["updated_ms"] / 1000, tz=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                lines.append(
                    f"    {ts}  {r['agent']:<16} {r['status']:<9} "
                    f"{r['side']:<4} {r['outcome']:<3} "
                    f"p={float(r['price_cents'] or 0):.4f} sz=${r['size_usd']:.2f}"
                )

    lines.append("")
    lines.append(f"{C_DIM}Read-only. ~/Desktop/poly/OPERATIONS.md is the source of truth.{C_RESET}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--watch", action="store_true", help="auto-refresh every 10s")
    p.add_argument("--interval", type=int, default=10, help="refresh seconds for --watch")
    p.add_argument("--once", action="store_true", help="single snapshot then exit (default)")
    p.add_argument("--json", action="store_true", help="emit JSON for piping")
    args = p.parse_args()

    if args.json:
        snapshot = {"poly1": poly1_state(), "swarm": swarm_state(),
                    "ts": datetime.now(timezone.utc).isoformat()}
        # strip ANSI from the heartbeat strings for JSON consumers
        for b in ("poly1", "swarm"):
            if snapshot[b].get("heartbeat"):
                import re
                snapshot[b]["heartbeat"] = re.sub(r"\x1b\[[0-9;]*m", "", snapshot[b]["heartbeat"])
        print(json.dumps(snapshot, default=str, indent=2))
        return 0

    if args.watch:
        try:
            while True:
                p1 = poly1_state()
                sw = swarm_state()
                # Clear screen + home
                sys.stdout.write("\033[2J\033[H")
                sys.stdout.write(render(p1, sw) + "\n")
                sys.stdout.flush()
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0

    p1 = poly1_state()
    sw = swarm_state()
    print(render(p1, sw))
    return 0


if __name__ == "__main__":
    sys.exit(main())
