#!/usr/bin/env python3
"""Strategy Watchdog — in-bot paging rules.

Two rules:
  1. Daily realized P&L (UTC midnight to now) below floor → Discord alert,
     fires once per UTC day.
  2. Per-strategy: 10+ closed trades in last 24h with 0% win rate → Discord
     alert + record auto-disable in `strategy_kill_switch` table.

Cron: every 30 min via `/etc/cron.d/traderjoes-strategy-watchdog`.

Discord delivery reuses the proven status_ping.py pattern (DISCORD_TOKEN +
DISCORD_CHANNEL_ID via Bot REST API; falls back to DISCORD_WEBHOOK_URL if
present).

CLI flags (for synthetic testing):
  --pnl-floor=N       override daily P&L floor (default -500)
  --min-trades=N      override zero-WR min closed-trade count (default 10)
  --force-pnl-alert   force daily-PnL alert path regardless of state
  --force-zerowr      force zero-WR alert path on a synthetic strategy
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
from datetime import datetime, timezone

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("strategy_watchdog")

DB_PATH = os.environ.get("DB_PATH", "/app/data/trading_firm.db")
if not os.path.exists(DB_PATH):
    DB_PATH = "/root/trading-bot/data/trading_firm.db"

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID", "")

DEFAULT_PNL_FLOOR = -500.0
DEFAULT_MIN_TRADES_ZERO_WR = 10


def ensure_schema(db_path=DB_PATH):
    c = sqlite3.connect(db_path)
    c.execute("""
        CREATE TABLE IF NOT EXISTS pager_state (
            rule_name TEXT NOT NULL,
            fire_date TEXT NOT NULL,
            fired_at  TEXT NOT NULL,
            payload   TEXT,
            PRIMARY KEY (rule_name, fire_date)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS strategy_kill_switch (
            strategy           TEXT PRIMARY KEY,
            disabled           INTEGER NOT NULL DEFAULT 0,
            disabled_at        TEXT,
            reason             TEXT,
            closed_trades_24h  INTEGER,
            win_rate_pct       REAL,
            operator           TEXT
        )
    """)
    c.commit()
    c.close()


def post_to_discord(msg: str) -> bool:
    if DISCORD_WEBHOOK_URL:
        try:
            r = requests.post(DISCORD_WEBHOOK_URL, json={"content": msg}, timeout=10)
            if r.status_code < 300:
                return True
            log.warning("Webhook %d: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("Webhook failed: %s", e)

    if DISCORD_TOKEN and DISCORD_CHANNEL_ID:
        try:
            url = f"https://discord.com/api/v10/channels/{DISCORD_CHANNEL_ID}/messages"
            r = requests.post(
                url,
                headers={
                    "Authorization": f"Bot {DISCORD_TOKEN}",
                    "Content-Type": "application/json",
                    "User-Agent": "TraderJoesStrategyWatchdog (1.0)",
                },
                json={"content": msg},
                timeout=10,
            )
            if r.status_code < 300:
                return True
            log.warning("Bot API %d: %s", r.status_code, r.text[:200])
        except Exception as e:
            log.warning("Bot API failed: %s", e)

    log.warning("No Discord credentials — printing only:\n%s", msg)
    return False


def already_fired_today(rule: str, db_path=DB_PATH) -> bool:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    c = sqlite3.connect(db_path)
    row = c.execute(
        "SELECT 1 FROM pager_state WHERE rule_name=? AND fire_date=?",
        (rule, today),
    ).fetchone()
    c.close()
    return row is not None


def mark_fired(rule: str, payload: str = "", db_path=DB_PATH) -> None:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).isoformat()
    c = sqlite3.connect(db_path)
    c.execute(
        "INSERT OR REPLACE INTO pager_state (rule_name, fire_date, fired_at, payload) "
        "VALUES (?, ?, ?, ?)",
        (rule, today, now, payload),
    )
    c.commit()
    c.close()


def daily_realized_pnl(db_path=DB_PATH) -> tuple[int, float]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    c = sqlite3.connect(db_path)
    row = c.execute(
        "SELECT COUNT(*), COALESCE(SUM(realized_pnl), 0) "
        "FROM positions "
        "WHERE strftime('%Y-%m-%d', closed_at) = ? "
        "AND realized_pnl IS NOT NULL",
        (today,),
    ).fetchone()
    c.close()
    return int(row[0] or 0), float(row[1] or 0)


def per_strategy_24h(db_path=DB_PATH):
    """Return [(strategy, n_closed, wr_pct, total_pnl), ...] for last 24h."""
    c = sqlite3.connect(db_path)
    rows = c.execute("""
        SELECT strategy,
               COUNT(*) as n,
               SUM(CASE WHEN realized_pnl > 0 THEN 1.0 ELSE 0 END) * 100.0 / COUNT(*) as wr_pct,
               SUM(realized_pnl) as pnl
          FROM positions
         WHERE closed_at > datetime('now', '-24 hours')
           AND realized_pnl IS NOT NULL
         GROUP BY strategy
         ORDER BY n DESC
    """).fetchall()
    c.close()
    return [(s, int(n), float(wr or 0), float(p or 0)) for (s, n, wr, p) in rows]


def disable_strategy(strategy: str, n: int, wr: float, reason: str, db_path=DB_PATH) -> None:
    now = datetime.now(timezone.utc).isoformat()
    c = sqlite3.connect(db_path)
    c.execute("""
        INSERT INTO strategy_kill_switch
          (strategy, disabled, disabled_at, reason, closed_trades_24h, win_rate_pct, operator)
        VALUES (?, 1, ?, ?, ?, ?, 'auto-watchdog')
        ON CONFLICT(strategy) DO UPDATE SET
          disabled=1, disabled_at=excluded.disabled_at, reason=excluded.reason,
          closed_trades_24h=excluded.closed_trades_24h,
          win_rate_pct=excluded.win_rate_pct,
          operator=excluded.operator
    """, (strategy, now, reason, n, wr))
    c.commit()
    c.close()


def check_daily_pnl(pnl_floor: float, force: bool = False) -> bool:
    """Returns True if alert was sent."""
    n, pnl = daily_realized_pnl()
    rule = "daily_pnl_floor"

    if force:
        msg = (
            f"🔴 **WATCHDOG TEST: daily P&L floor**\n"
            f"Synthetic test fire — current realized: ${pnl:+.2f} from {n} closed trades today.\n"
            f"(Threshold: ${pnl_floor:.2f}; this fire is forced via `--force-pnl-alert`.)"
        )
        ok = post_to_discord(msg)
        log.info("FORCED daily_pnl alert: posted=%s pnl=$%.2f n=%d", ok, pnl, n)
        return ok

    if pnl < pnl_floor:
        if already_fired_today(rule):
            log.info("daily_pnl already alerted today (pnl=$%.2f, floor=$%.2f)", pnl, pnl_floor)
            return False
        msg = (
            f"🔴 **DAILY P&L FLOOR BREACHED**\n"
            f"Realized P&L today: **${pnl:+,.2f}** from {n} closed trades.\n"
            f"Floor: ${pnl_floor:,.2f}.\n"
            f"Investigate which strategy is bleeding."
        )
        ok = post_to_discord(msg)
        if ok:
            mark_fired(rule, payload=f"pnl={pnl:.2f},n={n}")
        log.info("daily_pnl alert: posted=%s pnl=$%.2f n=%d", ok, pnl, n)
        return ok

    log.info("daily_pnl OK: $%.2f (floor $%.2f, %d trades)", pnl, pnl_floor, n)
    return False


def check_zero_wr(min_trades: int, force: bool = False) -> int:
    """Returns count of strategies that triggered an alert."""
    rows = per_strategy_24h()
    alerted = 0

    if force:
        msg = (
            f"🔴 **WATCHDOG TEST: zero-WR auto-disable**\n"
            f"Synthetic strategy `__test_synthetic__` simulated: 12 trades, 0.0% WR over 24h.\n"
            f"This is a forced test fire (`--force-zerowr`) — no real strategy disabled."
        )
        ok = post_to_discord(msg)
        log.info("FORCED zerowr alert: posted=%s", ok)
        return 1 if ok else 0

    for strategy, n, wr, pnl in rows:
        if n >= min_trades and wr == 0.0:
            rule = f"zerowr:{strategy}"
            if already_fired_today(rule):
                log.info("zerowr %s already alerted today (n=%d, wr=%.1f%%)", strategy, n, wr)
                continue
            disable_strategy(
                strategy, n, wr,
                reason=f"0% WR over {n} closed trades in 24h (P&L ${pnl:+,.2f})"
            )
            msg = (
                f"🔴 **STRATEGY AUTO-DISABLED: `{strategy}`**\n"
                f"Last 24h: **{n}** closed trades, **0.0%** win rate, P&L **${pnl:+,.2f}**.\n"
                f"Recorded in `strategy_kill_switch` table (operator=`auto-watchdog`).\n"
                f"Entry-side enforcement: per-strategy gates pending (see GOVERNANCE.md)."
            )
            ok = post_to_discord(msg)
            if ok:
                mark_fired(rule, payload=f"n={n},wr={wr:.1f},pnl={pnl:.2f}")
                alerted += 1
            log.info("zerowr alert: strategy=%s posted=%s n=%d wr=%.1f%%", strategy, ok, n, wr)

    if not alerted:
        log.info("zerowr OK: %d strategies checked, none tripped", len(rows))
    return alerted


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pnl-floor", type=float, default=DEFAULT_PNL_FLOOR)
    ap.add_argument("--min-trades", type=int, default=DEFAULT_MIN_TRADES_ZERO_WR)
    ap.add_argument("--force-pnl-alert", action="store_true")
    ap.add_argument("--force-zerowr", action="store_true")
    args = ap.parse_args()

    ensure_schema()

    pnl_alert = check_daily_pnl(args.pnl_floor, force=args.force_pnl_alert)
    zwr_alerts = check_zero_wr(args.min_trades, force=args.force_zerowr)

    print(f"watchdog: pnl_alert={pnl_alert} zerowr_alerts={zwr_alerts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
