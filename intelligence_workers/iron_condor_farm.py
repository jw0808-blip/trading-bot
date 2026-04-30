"""Iron Condor Farm — SHADOW tier producer (Day 6 2026-04-19).

Simulates SPY Iron Condor at ~15-delta strikes, 45 DTE, $500 max notional.
Writes positions rows with **tier='SHADOW'** (NOT 'PAPER') so these
hypothetical condors show up under the SHADOW dashboard tab only.
Exit manager (main.py:16787) already handles the iron_condor strategy
tag for lifecycle, regardless of tier.

VIX GATE: only fires when VIX in [22, 28) — the sweet spot for premium
capture where IV is rich enough to earn the sell but not so high that
tail-risk dominates. Outside this window, logs a skip and exits 0.
Today's VIX ~17.48 means the scanner will typically SKIP; that's
correct and expected behavior — no forced entries just because a cron
tick fired.

Cron: 0 14 * * 1-5 (09:00 ET weekdays, ~30 min before market open).
See specs/shadow_tier_charter.md for graduation criteria (20+ SHADOW
entries with positive hypothetical expectancy → !promote-to-paper).
"""

import json
import os
import sys
import sqlite3
from datetime import datetime, timezone, timedelta

DB_PATH = os.environ.get("DB_PATH", "/app/data/trading_firm.db")

VIX_LOWER = 22.0
VIX_UPPER = 28.0
MAX_NOTIONAL = 500.0
DTE = 45
SHORT_DELTA_TARGET = 0.15  # ~15-delta short strikes
SHADOW_TIER = "SHADOW"     # Day 6 6F: positions rows go here, not PAPER


def _fetch_vix():
    """Cheap VIX read from yfinance — keeps the worker self-contained."""
    try:
        import yfinance as yf
        v = yf.Ticker("^VIX").history(period="1d")
        if not v.empty:
            return float(v["Close"].iloc[-1])
    except Exception as e:
        print(f"[iron_condor_farm] VIX fetch error: {e}", file=sys.stderr)
    return None


def _fetch_spy_price():
    try:
        import yfinance as yf
        s = yf.Ticker("SPY").history(period="1d")
        if not s.empty:
            return float(s["Close"].iloc[-1])
    except Exception as e:
        print(f"[iron_condor_farm] SPY fetch error: {e}", file=sys.stderr)
    return None


def _approx_15_delta_strikes(spy, vix):
    """Approximate 15-delta strike offsets using VIX-implied σ over 45 DTE.
    For 15-delta OTM strike: σ * sqrt(T/365) * z(0.15) ≈ σ_45 * 1.04
    """
    import math
    sigma_annual = vix / 100.0
    t = DTE / 365.0
    sigma_45 = sigma_annual * math.sqrt(t)
    z = 1.04  # inverse normal for 0.15 tail
    move = spy * sigma_45 * z
    short_call = round((spy + move) / 5.0) * 5.0
    short_put = round((spy - move) / 5.0) * 5.0
    long_call = short_call + 5.0
    long_put = short_put - 5.0
    return short_call, long_call, short_put, long_put


def _is_auto_disabled(strategy_name: str) -> bool:
    """Read strategy_kill_switch — see docs/GOVERNANCE.md. Fail-open."""
    try:
        c = sqlite3.connect(DB_PATH)
        row = c.execute(
            "SELECT disabled FROM strategy_kill_switch WHERE strategy=?",
            (strategy_name,),
        ).fetchone()
        c.close()
        return bool(row and row[0])
    except Exception:
        return False


def run():
    if _is_auto_disabled("iron_condor_shadow"):
        print("[iron_condor_farm] iron_condor_shadow auto-disabled by strategy_kill_switch — skipping")
        return 0
    vix = _fetch_vix()
    spy = _fetch_spy_price()
    if vix is None or spy is None:
        print(f"[iron_condor_farm] missing vix={vix} spy={spy}", file=sys.stderr)
        return 1
    print(f"[iron_condor_farm] VIX={vix:.2f} SPY=${spy:.2f}")

    if not (VIX_LOWER <= vix < VIX_UPPER):
        print(f"[iron_condor_farm] VIX {vix:.2f} outside [{VIX_LOWER},{VIX_UPPER}) — skip")
        return 0

    sc, lc, sp, lp = _approx_15_delta_strikes(spy, vix)
    expiry = (datetime.now(timezone.utc) + timedelta(days=DTE)).strftime("%Y-%m-%d")

    # Estimated net credit: roughly width × 0.20 for 15-delta condors at moderate VIX
    width = lc - sc  # = 5
    estimated_credit = round(width * 100 * 0.20, 2)  # $100 per option point
    notional = min(MAX_NOTIONAL, estimated_credit * 5)  # cap to $500

    market_id = f"IC:SPY {sp:.0f}/{lp:.0f}P-{sc:.0f}/{lc:.0f}C exp{expiry}"
    metadata = (
        f'{{"short_call":{sc},"long_call":{lc},"short_put":{sp},"long_put":{lp},'
        f'"expiry":"{expiry}","credit":{estimated_credit},"vix":{vix:.2f},"spy":{spy:.2f}}}'
    )

    try:
        conn = sqlite3.connect(DB_PATH)
        # Dedup: skip if an iron_condor_shadow opened today already exists
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        existing = conn.execute(
            "SELECT 1 FROM positions "
            "WHERE strategy IN ('iron_condor', 'iron_condor_shadow') "
            "AND DATE(created_at)=? LIMIT 1",
            (today,),
        ).fetchone()
        if existing:
            print(f"[iron_condor_farm] already opened today — skip")
            conn.close()
            return 0

        # SHADOW-EXEC: allocation-gated. Check iron_condor_shadow balance
        # covers the condor notional ($500 cap by MAX_NOTIONAL). Log-and-
        # skip if depleted; otherwise decrement allocation and proceed.
        required_capital = notional
        alloc = conn.execute(
            "SELECT current_balance, status FROM shadow_strategy_allocations "
            "WHERE strategy='iron_condor_shadow'"
        ).fetchone()
        if not alloc:
            print(f"[iron_condor_farm] no iron_condor_shadow allocation row — skip")
            conn.close()
            return 0
        current_balance, alloc_status = alloc
        if alloc_status != "ACTIVE":
            print(f"[iron_condor_farm] iron_condor_shadow status={alloc_status} — skip")
            conn.close()
            return 0
        if float(current_balance or 0) < required_capital:
            print(f"[iron_condor_farm] iron_condor_shadow depleted (${float(current_balance or 0):.2f} < ${required_capital:.2f}), skipping")
            conn.close()
            return 0

        conn.execute(
            """INSERT INTO positions
            (market_id, strategy, platform, size_usd, status, created_at,
             shares, entry_price, metadata, tier)
            VALUES (?, 'iron_condor_shadow', 'Simulated', ?, 'open', ?, 1, ?, ?, ?)""",
            (market_id, notional, datetime.now(timezone.utc).isoformat(),
             estimated_credit, metadata, SHADOW_TIER),
        )
        # Decrement allocation
        conn.execute(
            "UPDATE shadow_strategy_allocations "
            "SET current_balance = current_balance - ?, "
            "    last_updated = CURRENT_TIMESTAMP "
            "WHERE strategy='iron_condor_shadow'",
            (required_capital,),
        )
        # Also log to shadow_decisions for graduation tracking
        conn.execute("""
            INSERT INTO shadow_decisions
            (strategy, tier, signal_source, signal_data, intended_size_usd,
             intended_entry_price, intended_ttl_hours, metadata)
            VALUES (?, 'SHADOW', ?, ?, ?, ?, ?, ?)
        """, (
            "iron_condor_shadow",
            "iron_condor_farm",
            json.dumps({
                "short_call": sc, "long_call": lc,
                "short_put": sp, "long_put": lp,
                "expiry": expiry, "vix": vix, "spy": spy,
            }),
            notional,
            estimated_credit,
            DTE * 24,
            metadata,
        ))
        conn.commit()
        conn.close()
        print(f"[iron_condor_farm] SHADOW OPENED {market_id} credit=${estimated_credit:.2f} notional=${notional:.2f} tier=SHADOW strategy=iron_condor_shadow")
        return 0
    except Exception as e:
        print(f"[iron_condor_farm] DB error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(run())
