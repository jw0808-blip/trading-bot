"""
IB Gateway Microservice — Executes orders from SQLite queue via Interactive Brokers.

Architecture:
- Main bot writes orders to ib_orders table (status=pending)
- This service polls ib_orders, submits to IB Gateway via ib_insync
- Fills written back to ib_fills table and ib_orders.status updated

Runs as its own Docker container. Communicates with main bot via shared SQLite.

Environment variables:
  IB_HOST       — IB Gateway hostname (default: ib-gateway)
  IB_PORT       — IB Gateway port (default: 4002 for paper, 4001 for live)
  IB_ACCOUNT    — IB account ID (e.g., DU1234567 for paper)
  IB_CLIENT_ID  — Client ID for TWS API (default: 1)
  DB_PATH       — Path to shared SQLite database
"""

import os
import sys
import time
import sqlite3
import logging
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ib-gateway")

# Configuration
IB_HOST = os.getenv("IB_HOST", "ib-gateway")
IB_PORT = int(os.getenv("IB_PORT", "4002"))  # 4002=paper, 4001=live
IB_ACCOUNT = os.getenv("IB_ACCOUNT", "")
IB_CLIENT_ID = int(os.getenv("IB_CLIENT_ID", "1"))
DB_PATH = os.getenv("DB_PATH", "/app/data/trading_firm.db")
POLL_INTERVAL = int(os.getenv("IB_POLL_INTERVAL", "5"))  # seconds


def init_db():
    """Ensure IB tables exist in shared SQLite."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS ib_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, qty REAL, order_type TEXT DEFAULT 'MKT',
            limit_price REAL, strategy TEXT, market_id TEXT,
            status TEXT DEFAULT 'pending', ib_order_id TEXT,
            error TEXT, created_at TEXT DEFAULT (datetime('now')),
            filled_at TEXT, fill_price REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ib_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ib_order_id TEXT, symbol TEXT, side TEXT, qty REAL,
            fill_price REAL, commission REAL,
            filled_at TEXT DEFAULT (datetime('now'))
        )""")
        conn.commit()
        conn.close()
        log.info("IB tables initialized in %s", DB_PATH)
    except Exception as e:
        log.error("DB init failed: %s", e)
        sys.exit(1)


def get_pending_orders():
    """Fetch all pending orders from SQLite."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM ib_orders WHERE status='pending' ORDER BY created_at ASC").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("Error fetching pending orders: %s", e)
        return []


def update_order_status(order_id, status, ib_order_id=None, error=None, fill_price=None):
    """Update an order's status in SQLite."""
    try:
        conn = sqlite3.connect(DB_PATH)
        if status == "filled":
            conn.execute(
                "UPDATE ib_orders SET status=?, ib_order_id=?, fill_price=?, filled_at=datetime('now') WHERE id=?",
                (status, ib_order_id, fill_price, order_id))
        elif status == "error":
            conn.execute(
                "UPDATE ib_orders SET status=?, error=? WHERE id=?",
                (status, error, order_id))
        else:
            conn.execute(
                "UPDATE ib_orders SET status=?, ib_order_id=? WHERE id=?",
                (status, ib_order_id, order_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Error updating order %d: %s", order_id, e)


def record_fill(ib_order_id, symbol, side, qty, fill_price, commission=0):
    """Record a fill in the ib_fills table."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO ib_fills (ib_order_id, symbol, side, qty, fill_price, commission) VALUES (?,?,?,?,?,?)",
            (ib_order_id, symbol, side, qty, fill_price, commission))
        conn.commit()
        conn.close()
        log.info("FILL recorded: %s %s %s qty=%.2f @ $%.2f", ib_order_id, side, symbol, qty, fill_price)
    except Exception as e:
        log.warning("Error recording fill: %s", e)


def connect_ib():
    """Connect to IB Gateway via ib_insync. Returns IB instance or None."""
    try:
        from ib_insync import IB, Stock, MarketOrder, LimitOrder
        ib = IB()
        ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID, timeout=15)
        log.info("Connected to IB Gateway at %s:%d (account: %s)", IB_HOST, IB_PORT, IB_ACCOUNT or "auto")
        if IB_ACCOUNT:
            # Verify account
            accounts = ib.managedAccounts()
            if IB_ACCOUNT not in accounts:
                log.error("Account %s not found. Available: %s", IB_ACCOUNT, accounts)
                ib.disconnect()
                return None
        return ib
    except ImportError:
        log.error("ib_insync not installed. Run: pip install ib_insync")
        return None
    except Exception as e:
        log.warning("IB connection failed: %s", e)
        return None


def submit_order(ib, order_row):
    """Submit a single order to IB. Returns (success, ib_order_id, fill_price)."""
    from ib_insync import Stock, MarketOrder, LimitOrder

    symbol = order_row["symbol"]
    side = order_row["side"].upper()  # BUY or SELL
    qty = order_row["qty"]
    order_type = (order_row.get("order_type") or "MKT").upper()
    limit_price = order_row.get("limit_price")

    # Create contract
    contract = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(contract)

    # Create order
    action = "BUY" if side in ("BUY", "LONG") else "SELL"
    if order_type == "LMT" and limit_price:
        order = LimitOrder(action, qty, limit_price)
    else:
        order = MarketOrder(action, qty)

    # Account
    if IB_ACCOUNT:
        order.account = IB_ACCOUNT

    log.info("Submitting to IB: %s %s %.2f %s", action, symbol, qty, order_type)

    try:
        trade = ib.placeOrder(contract, order)
        ib.sleep(2)  # Wait for initial fill

        # Check status
        if trade.orderStatus.status in ("Filled", "Submitted", "PreSubmitted"):
            ib_oid = str(trade.order.orderId)
            avg_fill = trade.orderStatus.avgFillPrice or 0

            if trade.orderStatus.status == "Filled":
                # Record fill
                commission = sum(f.commission for f in trade.fills) if trade.fills else 0
                record_fill(ib_oid, symbol, action, qty, avg_fill, commission)
                return True, ib_oid, avg_fill
            else:
                # Order submitted but not yet filled — mark as submitted
                return True, ib_oid, 0
        else:
            error = f"IB status: {trade.orderStatus.status}"
            log.warning("Order not accepted: %s %s — %s", action, symbol, error)
            return False, None, 0

    except Exception as e:
        log.warning("IB order error: %s %s — %s", action, symbol, e)
        return False, None, 0


def check_fills(ib, submitted_orders):
    """Check for fills on previously submitted (not yet filled) orders."""
    try:
        open_trades = ib.openTrades()
        for trade in open_trades:
            if trade.orderStatus.status == "Filled":
                ib_oid = str(trade.order.orderId)
                # Find matching order in our tracking
                for order_id, tracked_oid in submitted_orders.items():
                    if tracked_oid == ib_oid:
                        avg_fill = trade.orderStatus.avgFillPrice or 0
                        commission = sum(f.commission for f in trade.fills) if trade.fills else 0
                        record_fill(ib_oid, trade.contract.symbol, trade.order.action,
                                    trade.orderStatus.filled, avg_fill, commission)
                        update_order_status(order_id, "filled", ib_oid, fill_price=avg_fill)
                        log.info("Delayed fill: order %d → %s filled @ $%.2f", order_id, ib_oid, avg_fill)
                        del submitted_orders[order_id]
                        break
    except Exception as e:
        log.warning("Fill check error: %s", e)


def main_loop():
    """Main polling loop: connect to IB, process pending orders."""
    init_db()

    if not IB_ACCOUNT:
        log.error("IB_ACCOUNT not set. Add to .env and restart.")
        log.info("Entering standby mode — will retry connection every 60s")

    ib = None
    submitted_orders = {}  # {our_order_id: ib_order_id} for tracking pending fills
    reconnect_delay = 10

    while True:
        try:
            # Connect if needed
            if ib is None or not ib.isConnected():
                if IB_ACCOUNT:
                    log.info("Connecting to IB Gateway...")
                    ib = connect_ib()
                    if ib:
                        reconnect_delay = 10
                    else:
                        log.info("IB connection failed, retrying in %ds", reconnect_delay)
                        time.sleep(reconnect_delay)
                        reconnect_delay = min(reconnect_delay * 2, 300)
                        continue
                else:
                    time.sleep(60)
                    continue

            # Process pending orders
            pending = get_pending_orders()
            for order_row in pending:
                order_id = order_row["id"]
                log.info("Processing order %d: %s %s %.2f",
                         order_id, order_row["side"], order_row["symbol"], order_row["qty"])

                update_order_status(order_id, "submitting")

                success, ib_oid, fill_price = submit_order(ib, order_row)

                if success and fill_price > 0:
                    update_order_status(order_id, "filled", ib_oid, fill_price=fill_price)
                    log.info("Order %d FILLED: %s @ $%.2f", order_id, ib_oid, fill_price)
                elif success:
                    update_order_status(order_id, "submitted", ib_oid)
                    submitted_orders[order_id] = ib_oid
                    log.info("Order %d SUBMITTED: %s (awaiting fill)", order_id, ib_oid)
                else:
                    update_order_status(order_id, "error", error="IB submission failed")
                    log.warning("Order %d FAILED", order_id)

            # Check for delayed fills on submitted orders
            if submitted_orders:
                check_fills(ib, submitted_orders)

            # Heartbeat
            if ib and ib.isConnected():
                ib.sleep(0)  # Process IB events

        except KeyboardInterrupt:
            log.info("Shutting down IB Gateway...")
            if ib and ib.isConnected():
                ib.disconnect()
            break
        except Exception as e:
            log.warning("Main loop error: %s", e)
            ib = None  # Force reconnect

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    log.info("IB Gateway Microservice starting")
    log.info("  Host: %s:%d | Account: %s | Client ID: %d",
             IB_HOST, IB_PORT, IB_ACCOUNT or "(not set)", IB_CLIENT_ID)
    log.info("  DB: %s | Poll interval: %ds", DB_PATH, POLL_INTERVAL)
    main_loop()
