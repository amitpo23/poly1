"""
polymarket_sync.py — fetch real fills + positions from Polymarket CLOB
and write to data/polymarket_positions.db for Grafana.

Run manually or via cron:
  docker compose run --rm trader python scripts/python/polymarket_sync.py
"""
import sqlite3, os, json, logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DB_PATH = os.environ.get("TRADE_LOG_DB", "/app/data/trade_log.db")


def _ensure_tables(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pm_trades (
            id          TEXT PRIMARY KEY,
            market_id   TEXT,
            asset_id    TEXT,
            side        TEXT,
            size        REAL,
            price       REAL,
            fee_rate    REAL,
            outcome     TEXT,
            status      TEXT,
            ts_ms       INTEGER,
            synced_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS pm_positions (
            asset_id        TEXT PRIMARY KEY,
            market_id       TEXT,
            title           TEXT,
            outcome         TEXT,
            size            REAL,
            avg_price       REAL,
            current_price   REAL,
            unrealised_pnl  REAL,
            cost_basis      REAL,
            synced_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS pm_wallet (
            id              INTEGER PRIMARY KEY CHECK (id = 1),
            usdc_balance    REAL,
            synced_at       TEXT
        );
    """)
    conn.commit()


def sync_trades(poly, conn: sqlite3.Connection) -> int:
    """Fetch last 500 trades from CLOB and upsert."""
    try:
        resp = poly.client.get_trades(
            maker_address=poly.funder or poly.address
        )
    except Exception as e:
        logger.warning(f"get_trades failed: {e}")
        return 0

    trades = resp if isinstance(resp, list) else resp.get("data", [])
    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for t in trades:
        try:
            conn.execute("""
                INSERT OR REPLACE INTO pm_trades
                  (id, market_id, asset_id, side, size, price, fee_rate,
                   outcome, status, ts_ms, synced_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, (
                t.get("id") or t.get("trade_id", ""),
                t.get("market", t.get("market_id", "")),
                t.get("asset_id", ""),
                t.get("side", ""),
                float(t.get("size", 0)),
                float(t.get("price", 0)),
                float(t.get("fee_rate_bps", 0)) / 10000,
                t.get("outcome", ""),
                t.get("status", ""),
                int(t.get("timestamp", 0)) * 1000 if t.get("timestamp") else 0,
                now,
            ))
            count += 1
        except Exception as e:
            logger.debug(f"skipping trade row: {e} — {t}")
    conn.commit()
    return count


def sync_balance(poly, conn: sqlite3.Connection) -> float:
    """Fetch USDC balance and upsert."""
    try:
        balance = poly.get_usdc_balance()
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT OR REPLACE INTO pm_wallet (id, usdc_balance, synced_at)
            VALUES (1, ?, ?)
        """, (balance, now))
        conn.commit()
        return balance
    except Exception as e:
        logger.warning(f"get_usdc_balance failed: {e}")
        return 0.0


def main() -> None:
    from agents.polymarket.polymarket import Polymarket

    poly = Polymarket(live=True)

    with sqlite3.connect(DB_PATH) as conn:
        _ensure_tables(conn)

        balance = sync_balance(poly, conn)
        logger.info(f"USDC balance: ${balance:.2f}")

        n = sync_trades(poly, conn)
        logger.info(f"Synced {n} trades from CLOB")

        # Summary per market
        rows = conn.execute("""
            SELECT market_id,
                   COUNT(*) as fills,
                   ROUND(SUM(CASE WHEN side='BUY' THEN size*price ELSE 0 END),2) as spent,
                   ROUND(SUM(CASE WHEN side='SELL' THEN size*price ELSE 0 END),2) as received
            FROM pm_trades
            WHERE status='MATCHED'
            GROUP BY market_id
            ORDER BY fills DESC
        """).fetchall()
        for r in rows:
            pnl = r[3] - r[2]
            logger.info(f"  market {r[0]}: {r[1]} fills, spent=${r[2]}, received=${r[3]}, net={pnl:+.2f}")


if __name__ == "__main__":
    main()
