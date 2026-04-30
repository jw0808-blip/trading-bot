#!/usr/bin/env python3
"""
Whale Entry Executor — Phase 4 (live).

Reads dry-run rows from `whale_signals` (produced by
whale_entry_signal_generator.py), places real Polymarket CLOB orders,
and tracks the resulting positions in the SHADOW tier under the
`whale_follow` allocation.

Gates (in order, all must pass):
  1. WHALE_FOLLOW_LIVE=1                           (master kill switch)
  2. Polymarket cash balance >= WHALE_MIN_BALANCE_USD (default $500)
  3. shadow_strategy_allocations.whale_follow.status == ACTIVE
  4. Allocation.current_balance >= our_size_usd
  5. No existing open whale_follow position on this condition_id

Sizing:
  Use signal.our_size_usd if present; else
  WHALE_ALLOC_USD * WHALE_BASE_SIZE_PCT * whale_confidence

Tracking:
  - INSERT positions tier='SHADOW' strategy='whale_follow'
  - UPDATE shadow_strategy_allocations.current_balance -= size_usd
  - UPDATE whale_signals SET executed_at, position_id, order_id, execution_status
  - INSERT shadow_decisions audit row

Subcommands:
  init        Migrate schema + ensure allocation row
  execute     Dispatch pending signals (cron entry point)
  status      Show allocation, open positions, recent decisions

Cron (every minute):
  * * * * * cd /root/trading-bot && \
            /usr/bin/python3 intelligence_workers/whale_entry_executor.py execute \
            >> /root/trading-bot/data/whale_executor.log 2>&1

Env:
  WHALE_FOLLOW_LIVE          0|1   master switch (default 0 = dry-run)
  WHALE_ALLOC_USD            $     allocation pool (default 5000)
  WHALE_BASE_SIZE_PCT        frac  base size, scaled by confidence (default 0.0025)
  WHALE_MIN_BALANCE_USD      $     kill switch on poly cash (default 500)
  DB_PATH                          sqlite path
  POLYMARKET_PK / POLYMARKET_FUNDER / POLYMARKET_API_KEY /
    POLYMARKET_API_SECRET (or POLYMARKET_SECRET) / POLYMARKET_PASSPHRASE
  POLY_WALLET_ADDRESS              proxy wallet (for on-chain USDC check)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional

import requests

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("whale_executor")


def _load_env_file(path: str = "/root/trading-bot/.env") -> None:
    """Minimal KEY=VALUE loader; existing env wins, malformed lines skipped.
    Lets the script work under bare cron without a `set -a; source .env` wrapper."""
    if not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as exc:
        log.debug(".env load failed (%s): %s", path, exc)


_load_env_file()

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
STRATEGY_NAME = "whale_follow"
PLATFORM = "polymarket"

DB_PATH = os.environ.get("DB_PATH", "/root/trading-bot/data/trading_firm.db")
if not os.path.exists(DB_PATH):
    alt = "/app/data/trading_firm.db"
    if os.path.exists(alt):
        DB_PATH = alt

WHALE_FOLLOW_LIVE = os.environ.get("WHALE_FOLLOW_LIVE", "0").strip() == "1"
WHALE_ALLOC_USD = float(os.environ.get("WHALE_ALLOC_USD", "5000"))
WHALE_BASE_SIZE_PCT = float(os.environ.get("WHALE_BASE_SIZE_PCT", "0.0025"))
MIN_BALANCE_USD = float(os.environ.get("WHALE_MIN_BALANCE_USD", "500"))

# Exit thresholds — the exit manager (Phase 5) reads these from metadata.
TP_PCT = float(os.environ.get("WHALE_TP_PCT", "0.10"))      # +10%
TP_DOLLAR = float(os.environ.get("WHALE_TP_DOLLAR", "25"))  # or +$25
SL_PCT = float(os.environ.get("WHALE_SL_PCT", "0.05"))      # -5%
SL_DOLLAR = float(os.environ.get("WHALE_SL_DOLLAR", "15"))  # or -$15
TTL_HOURS = float(os.environ.get("WHALE_TTL_HOURS", "48"))

# Polymarket has a $1 minimum-order convention; reject smaller sizings.
MIN_ORDER_USD = 1.0

POLY_WALLET = os.environ.get("POLY_WALLET_ADDRESS", "").strip()


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_columns(conn: sqlite3.Connection) -> None:
    """Additive migration: tag whale_signals with execution outcome."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(whale_signals)")}
    additions = [
        ("executed_at",      "INTEGER"),
        ("position_id",      "INTEGER"),
        ("order_id",         "TEXT"),
        ("execution_status", "TEXT"),  # EXECUTED / SKIPPED / FAILED
    ]
    for col, ddl in additions:
        if col not in existing:
            conn.execute(f"ALTER TABLE whale_signals ADD COLUMN {col} {ddl}")
            log.info("migrated whale_signals: +%s", col)
    conn.commit()


def ensure_allocation(conn: sqlite3.Connection) -> None:
    """Insert the whale_follow allocation row if missing. Idempotent."""
    row = conn.execute(
        "SELECT 1 FROM shadow_strategy_allocations WHERE strategy=?",
        (STRATEGY_NAME,),
    ).fetchone()
    if row:
        return
    conn.execute(
        """INSERT INTO shadow_strategy_allocations
             (strategy, initial_allocation, current_balance, status)
           VALUES (?, ?, ?, 'ACTIVE')""",
        (STRATEGY_NAME, WHALE_ALLOC_USD, WHALE_ALLOC_USD),
    )
    conn.commit()
    log.info("created allocation row: %s = $%.2f", STRATEGY_NAME, WHALE_ALLOC_USD)


# --------------------------------------------------------------------------
# Kill switch — on-chain USDC.e at the proxy wallet
# --------------------------------------------------------------------------
def get_poly_cash_balance() -> float:
    """Return USDC.e balance at POLY_WALLET_ADDRESS (proxy). 0.0 on any failure."""
    if not POLY_WALLET:
        log.warning("POLY_WALLET_ADDRESS not set — treating cash as 0")
        return 0.0
    USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    padded = POLY_WALLET.lower().replace("0x", "").zfill(64)
    payload = {
        "jsonrpc": "2.0", "method": "eth_call", "id": 1,
        "params": [{"to": USDC_E, "data": "0x70a08231" + padded}, "latest"],
    }
    for rpc in ("https://polygon-rpc.com", "https://rpc.ankr.com/polygon"):
        try:
            r = requests.post(rpc, json=payload, timeout=10)
            if r.status_code != 200:
                continue
            raw_hex = r.json().get("result", "0x0")
            if raw_hex and raw_hex != "0x":
                return int(raw_hex, 16) / 1_000_000  # USDC.e = 6 decimals
        except Exception as exc:
            log.debug("RPC %s failed: %s", rpc, exc)
            continue
    return 0.0


# --------------------------------------------------------------------------
# Polymarket order placement (sync — no asyncio for cron)
# --------------------------------------------------------------------------
def place_polymarket_order(action: str, token_id: str, size_usd: float,
                           price: float) -> tuple[bool, str, str]:
    """Place a real GTC limit order. Returns (success, message, order_id)."""
    pk = os.environ.get("POLYMARKET_PK", "").strip()
    if not pk:
        return False, "POLYMARKET_PK not configured", ""

    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
    except ImportError:
        return False, "py-clob-client not installed", ""

    funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
    api_key = os.environ.get("POLYMARKET_API_KEY", "").strip()
    # Accept either env name; .env in this repo uses POLYMARKET_SECRET.
    api_secret = (os.environ.get("POLYMARKET_API_SECRET")
                  or os.environ.get("POLYMARKET_SECRET", "")).strip()
    api_passphrase = os.environ.get("POLYMARKET_PASSPHRASE", "").strip()

    kwargs: dict[str, Any] = {
        "host": "https://clob.polymarket.com",
        "key": pk,
        "chain_id": 137,
        "signature_type": 2,
    }
    if funder:
        kwargs["funder"] = funder

    try:
        client = ClobClient(**kwargs)
        if api_key and api_secret and api_passphrase:
            client.set_api_creds(ApiCreds(
                api_key=api_key, api_secret=api_secret,
                api_passphrase=api_passphrase,
            ))
        else:
            client.set_api_creds(client.create_or_derive_api_creds())
    except Exception as exc:
        return False, f"client init/cred error: {exc}", ""

    side = BUY if action.upper() == "BUY" else SELL
    if not price or price <= 0 or price >= 1:
        return False, f"price {price} out of (0,1)", ""
    size_shares = round(size_usd / price, 2)
    if size_shares <= 0:
        return False, f"computed shares={size_shares} not positive", ""

    args = OrderArgs(price=price, size=size_shares, side=side, token_id=token_id)
    try:
        signed = client.create_order(args)
        resp = client.post_order(signed, OrderType.GTC)
    except Exception as exc:
        return False, f"post_order exception: {exc}"[:200], ""

    if isinstance(resp, dict) and resp.get("success"):
        oid = str(resp.get("orderID") or resp.get("order_id") or "")
        return True, f"GTC placed (orderID={oid or '?'})", oid
    err = ""
    if isinstance(resp, dict):
        err = str(resp.get("errorMsg") or resp.get("error") or resp)[:200]
    else:
        err = str(resp)[:200]
    return False, f"rejected: {err}", ""


# --------------------------------------------------------------------------
# Audit + state mutations
# --------------------------------------------------------------------------
def audit_decision(conn: sqlite3.Connection, *, signal_id: Optional[int],
                   decision: str, skip_reason: Optional[str] = None,
                   size_usd: Optional[float] = None,
                   token_id: Optional[str] = None,
                   price: Optional[float] = None,
                   position_id: Optional[int] = None,
                   order_id: Optional[str] = None,
                   metadata: Optional[dict] = None) -> None:
    """Append to shadow_decisions. Mirrors shadow_exec._shadow_audit shape."""
    extra = dict(metadata or {})
    extra.update({
        "decision": decision,
        "skip_reason": skip_reason,
        "platform": PLATFORM,
        "token_id": token_id,
        "side": "BUY",
        "order_id": order_id,
        "live_mode": WHALE_FOLLOW_LIVE,
    })
    sig_data = {"signal_id": signal_id} if signal_id else {}
    tp_price = (price * (1 + TP_PCT)) if price else None
    sl_price = (price * (1 - SL_PCT)) if price else None
    try:
        conn.execute(
            """INSERT INTO shadow_decisions
                 (strategy, tier, trade_id, signal_source, signal_data,
                  intended_size_usd, intended_entry_price,
                  intended_exit_price_tp, intended_exit_price_sl,
                  intended_ttl_hours, simulated_fill_status, metadata)
               VALUES (?, 'SHADOW', ?, 'whale_following', ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                STRATEGY_NAME, position_id,
                json.dumps(sig_data, default=str),
                size_usd, price, tp_price, sl_price, TTL_HOURS,
                "FILLED" if decision == "EXECUTED" else "REJECTED",
                json.dumps(extra, default=str),
            ),
        )
        conn.commit()
    except Exception as exc:
        log.debug("audit_decision failed: %s", exc)


def mark_signal(conn: sqlite3.Connection, signal_id: int, status: str, *,
                position_id: Optional[int] = None,
                order_id: Optional[str] = None,
                reason: Optional[str] = None) -> None:
    note_suffix = f" [{status} @{int(time.time())}: {reason or ''}]"
    conn.execute(
        """UPDATE whale_signals
              SET executed_at=?, position_id=?, order_id=?, execution_status=?,
                  notes = COALESCE(notes,'') || ?
            WHERE id=?""",
        (int(time.time()), position_id, order_id, status, note_suffix, signal_id),
    )
    conn.commit()


def open_position(conn: sqlite3.Connection, signal: sqlite3.Row,
                  size_usd: float, price: float,
                  order_id: str) -> int:
    shares = round(size_usd / price, 4) if price > 0 else 0
    target_price = round(price * (1 + TP_PCT), 4)
    stop_price = round(price * (1 - SL_PCT), 4)
    meta = {
        "signal_id": signal["id"],
        "proxy_wallet": signal["proxy_wallet"],
        "whale_confidence": signal["whale_confidence"],
        "asset_id": signal["asset_id"],
        "outcome": signal["outcome"],
        "market_title": signal["market_title"],
        "ttl_hours": TTL_HOURS,
        "tp_pct": TP_PCT, "tp_dollar": TP_DOLLAR,
        "sl_pct": SL_PCT, "sl_dollar": SL_DOLLAR,
        "order_id": order_id,
        "live_mode": WHALE_FOLLOW_LIVE,
        "source": "whale_following",
    }
    cur = conn.execute(
        """INSERT INTO positions
             (market_id, platform, strategy, direction, size_usd, shares,
              entry_price, current_price, stop_price, target_price,
              status, tier, regime, metadata, created_at)
           VALUES (?, ?, ?, 'buy', ?, ?, ?, ?, ?, ?, 'open', 'SHADOW',
                   'prediction', ?, datetime('now'))""",
        (
            signal["condition_id"], PLATFORM, STRATEGY_NAME,
            size_usd, shares, price, price, stop_price, target_price,
            json.dumps(meta, default=str),
        ),
    )
    conn.commit()
    return cur.lastrowid


def debit_allocation(conn: sqlite3.Connection, size_usd: float) -> None:
    conn.execute(
        """UPDATE shadow_strategy_allocations
              SET current_balance = current_balance - ?,
                  last_updated = CURRENT_TIMESTAMP
            WHERE strategy = ?""",
        (size_usd, STRATEGY_NAME),
    )
    conn.commit()


# --------------------------------------------------------------------------
# Main dispatch loop
# --------------------------------------------------------------------------
def dispatch_pending(conn: sqlite3.Connection) -> dict:
    summary = {"checked": 0, "executed": 0, "skipped": 0, "failed": 0,
               "live_mode": WHALE_FOLLOW_LIVE}

    # Gate 0: strategy_kill_switch (auto-disable by watchdog).
    # Per the Strategy Disable Principle in docs/GOVERNANCE.md, an entry
    # function must consult this table before placing orders. Fail-open
    # on read errors so a transient SQLite issue does not silently halt.
    try:
        _row = conn.execute(
            "SELECT disabled FROM strategy_kill_switch WHERE strategy=?",
            (STRATEGY_NAME,),
        ).fetchone()
        if _row and (_row["disabled"] if hasattr(_row, "keys") else _row[0]):
            log.info("%s auto-disabled by strategy_kill_switch — skipping", STRATEGY_NAME)
            summary["auto_disabled"] = True
            return summary
    except Exception as _kse:
        log.warning("strategy_kill_switch read failed (%s) — proceeding", _kse)

    if not WHALE_FOLLOW_LIVE:
        log.info("WHALE_FOLLOW_LIVE=0 — DRY-RUN; no real orders will fire")

    # Gate 1: kill switch (poly cash)
    cash = get_poly_cash_balance()
    log.info("poly cash balance: $%.2f (min: $%.2f)", cash, MIN_BALANCE_USD)
    if cash < MIN_BALANCE_USD:
        msg = f"KILL-SWITCH: poly cash ${cash:.2f} < min ${MIN_BALANCE_USD:.0f}"
        log.warning(msg)
        audit_decision(conn, signal_id=None, decision="SKIPPED",
                       skip_reason=msg, metadata={"poly_cash": cash})
        summary["killswitch"] = True
        return summary

    # Gate 2: allocation row + status
    alloc = conn.execute(
        "SELECT initial_allocation, current_balance, status "
        "FROM shadow_strategy_allocations WHERE strategy=?",
        (STRATEGY_NAME,),
    ).fetchone()
    if not alloc:
        log.warning("no allocation row for '%s' — run `init` subcommand first",
                    STRATEGY_NAME)
        return summary
    if alloc["status"] != "ACTIVE":
        log.warning("allocation status=%s — not dispatching", alloc["status"])
        summary["alloc_status"] = alloc["status"]
        return summary

    rows = conn.execute(
        """SELECT id, condition_id, asset_id, market_title, outcome, side,
                  proxy_wallet, whale_confidence, our_entry_price, our_size_usd
             FROM whale_signals
            WHERE resolved = 0
              AND (executed_at IS NULL OR execution_status = 'FAILED' AND
                   executed_at < ?)
         ORDER BY created_at ASC""",
        # Allow retry of FAILED rows after 1 hour cooldown
        (int(time.time()) - 3600,),
    ).fetchall()
    summary["checked"] = len(rows)
    if not rows:
        log.info("no pending signals")
        return summary

    current_balance = float(alloc["current_balance"] or 0)

    for r in rows:
        sid = r["id"]
        cid = r["condition_id"]
        token_id = r["asset_id"]

        # Sizing
        size_usd = float(r["our_size_usd"] or 0)
        if size_usd <= 0:
            size_usd = (WHALE_ALLOC_USD * WHALE_BASE_SIZE_PCT
                        * float(r["whale_confidence"] or 0))
        size_usd = round(min(size_usd, current_balance), 2)
        if size_usd < MIN_ORDER_USD:
            reason = f"size ${size_usd:.2f} below min ${MIN_ORDER_USD:.2f}"
            mark_signal(conn, sid, "SKIPPED", reason=reason)
            audit_decision(conn, signal_id=sid, decision="SKIPPED",
                           skip_reason=reason, size_usd=size_usd,
                           token_id=token_id, price=r["our_entry_price"])
            summary["skipped"] += 1
            continue

        # Concurrency guard (idempotency)
        already = conn.execute(
            "SELECT id FROM positions WHERE market_id=? AND strategy=? AND status='open'",
            (cid, STRATEGY_NAME),
        ).fetchone()
        if already:
            reason = f"position {already['id']} already open on this market"
            mark_signal(conn, sid, "SKIPPED", reason=reason)
            summary["skipped"] += 1
            continue

        price = float(r["our_entry_price"] or 0)
        if price <= 0 or price >= 1:
            reason = f"entry price {price} out of (0,1)"
            mark_signal(conn, sid, "SKIPPED", reason=reason)
            audit_decision(conn, signal_id=sid, decision="SKIPPED",
                           skip_reason=reason, token_id=token_id)
            summary["skipped"] += 1
            continue

        side = (r["side"] or "BUY").upper()
        if side != "BUY":
            reason = f"non-BUY side '{side}' not supported"
            mark_signal(conn, sid, "SKIPPED", reason=reason)
            summary["skipped"] += 1
            continue

        # Place order (or simulate in dry-run)
        if WHALE_FOLLOW_LIVE:
            ok, msg, order_id = place_polymarket_order(side, token_id, size_usd, price)
        else:
            ok, msg, order_id = (True,
                                 f"DRY-RUN (WHALE_FOLLOW_LIVE=0): would BUY ${size_usd:.2f} @ ${price:.3f}",
                                 "DRY_RUN")

        if not ok:
            mark_signal(conn, sid, "FAILED", reason=msg)
            audit_decision(conn, signal_id=sid, decision="FAILED",
                           skip_reason=msg, size_usd=size_usd,
                           token_id=token_id, price=price)
            summary["failed"] += 1
            log.warning("FAIL signal=%d cid=%s: %s", sid, cid[:20], msg)
            continue

        position_id = open_position(conn, r, size_usd, price, order_id)
        debit_allocation(conn, size_usd)
        current_balance -= size_usd  # local mirror for next iteration's sizing
        mark_signal(conn, sid, "EXECUTED", position_id=position_id,
                    order_id=order_id, reason=msg)
        audit_decision(conn, signal_id=sid, decision="EXECUTED",
                       size_usd=size_usd, token_id=token_id, price=price,
                       position_id=position_id, order_id=order_id)
        summary["executed"] += 1
        log.info("EXEC signal=%d → pos=%d order=%s | %s",
                 sid, position_id, order_id, msg)

    log.info("DISPATCH DONE checked=%d executed=%d skipped=%d failed=%d live=%s",
             summary["checked"], summary["executed"], summary["skipped"],
             summary["failed"], WHALE_FOLLOW_LIVE)
    return summary


# --------------------------------------------------------------------------
# Subcommands
# --------------------------------------------------------------------------
def cmd_init(conn: sqlite3.Connection) -> int:
    ensure_columns(conn)
    ensure_allocation(conn)
    print(f"OK: schema migrated, allocation '{STRATEGY_NAME}' ensured "
          f"(${WHALE_ALLOC_USD:.0f} initial)")
    return 0


def cmd_execute(conn: sqlite3.Connection) -> int:
    ensure_columns(conn)
    ensure_allocation(conn)
    summary = dispatch_pending(conn)
    print(json.dumps(summary, default=str))
    return 0


def cmd_status(conn: sqlite3.Connection) -> int:
    print(f"== Whale Entry Executor — status ==")
    print(f"  WHALE_FOLLOW_LIVE: {WHALE_FOLLOW_LIVE}")
    print(f"  WHALE_ALLOC_USD:   {WHALE_ALLOC_USD}")
    print(f"  MIN_BALANCE_USD:   {MIN_BALANCE_USD}")
    print(f"  DB_PATH:           {DB_PATH}")

    cash = get_poly_cash_balance()
    print(f"\n-- Polymarket cash --\n  USDC.e on-chain: ${cash:,.2f}")

    alloc = conn.execute(
        "SELECT * FROM shadow_strategy_allocations WHERE strategy=?",
        (STRATEGY_NAME,),
    ).fetchone()
    if alloc:
        print(f"\n-- Allocation ({STRATEGY_NAME}) --")
        print(f"  initial=${alloc['initial_allocation']:.2f}  "
              f"balance=${alloc['current_balance']:.2f}  "
              f"status={alloc['status']}  "
              f"trades={alloc['trades_count']}  "
              f"wins={alloc['wins']}  losses={alloc['losses']}  "
              f"pnl=${alloc['total_pnl']:.2f}")
    else:
        print("\n-- Allocation -- (not initialized; run `init`)")

    pos = conn.execute(
        """SELECT id, market_id, size_usd, shares, entry_price, current_price,
                  stop_price, target_price, created_at
             FROM positions
            WHERE strategy=? AND status='open' AND tier='SHADOW'
         ORDER BY created_at DESC""",
        (STRATEGY_NAME,),
    ).fetchall()
    print(f"\n-- Open positions ({len(pos)}) --")
    for p in pos:
        print(f"  #{p['id']} {p['market_id'][:18]} size=${p['size_usd']:.2f} "
              f"entry={p['entry_price']:.3f} cur={p['current_price']:.3f} "
              f"sl={p['stop_price']:.3f} tp={p['target_price']:.3f} "
              f"@ {p['created_at']}")

    sigs = conn.execute(
        """SELECT id, market_title, outcome, our_size_usd, execution_status,
                  position_id, order_id, executed_at
             FROM whale_signals
            ORDER BY id DESC LIMIT 10"""
    ).fetchall()
    print(f"\n-- Recent signals (last 10) --")
    for s in sigs:
        ex = s["execution_status"] or "(unprocessed)"
        when = (datetime.fromtimestamp(s["executed_at"], tz=timezone.utc)
                .strftime("%m-%d %H:%M") if s["executed_at"] else "—")
        title = (s["market_title"] or "?")[:40]
        print(f"  #{s['id']:>4} {ex:<10} {when:<11} ${s['our_size_usd'] or 0:>5.2f} "
              f"pos={s['position_id'] or '-':<5} {s['outcome']} '{title}'")

    decs = conn.execute(
        """SELECT timestamp, simulated_fill_status, intended_size_usd, metadata
             FROM shadow_decisions
            WHERE strategy=? ORDER BY id DESC LIMIT 10""",
        (STRATEGY_NAME,),
    ).fetchall()
    print(f"\n-- Recent decisions (last 10) --")
    for d in decs:
        try:
            m = json.loads(d["metadata"] or "{}")
        except Exception:
            m = {}
        decision = m.get("decision", d["simulated_fill_status"] or "?")
        reason = m.get("skip_reason") or ""
        print(f"  {d['timestamp']}  {decision:<9} ${d['intended_size_usd'] or 0:>6.2f}  {reason}")
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Whale-Following Entry Executor (Phase 4)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init",    help="migrate schema + ensure allocation row")
    sub.add_parser("execute", help="dispatch pending whale_signals")
    sub.add_parser("status",  help="show allocation, positions, decisions")
    args = parser.parse_args(argv)

    conn = db()
    try:
        if args.command == "init":
            return cmd_init(conn)
        if args.command == "execute":
            return cmd_execute(conn)
        if args.command == "status":
            return cmd_status(conn)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
