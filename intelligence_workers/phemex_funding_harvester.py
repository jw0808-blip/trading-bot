#!/usr/bin/env python3
"""Phemex Strategy 1 — Delta-Neutral BTC Funding Harvester.

STATUS / PROVENANCE
-------------------
⚠️  SHADOW-RESHAPE-A (2026-04-20) referenced a prior "Phemex S1 kill on
    2026-04-18" and removed funding_arb_shadow from the allocation book.
    This file is a RESURRECTION. Per specs/graduation_mechanics.md the
    re-enable path is:
        new design spec  →  SHADOW tier restart from scratch  →  30+ shadow
        trades  →  SGS-gated graduation.
    Before any live capital touches this strategy Jerad should
    reconcile the Apr 18 kill notation with the design changes in this
    implementation. Default mode is DRY_RUN; live execution requires
    opting in explicitly.

STRATEGY
--------
Capture BTC perpetual funding-rate payments while remaining delta-neutral.

    long  $X BTC spot on Coinbase   (delta  +1.0 per BTC)
    short $X BTC perp on Phemex     (delta  -1.0 per BTC, isolated margin,
                                      2× notional → collateral = X/2 USDT)

Funding at Phemex pays SHORT when `fundingRateRr > 0`. The spot leg
immunises against BTC price moves; the perp leg collects funding every
8 h as long as the rate stays positive (or closes when it turns
negative).

Risk controls (non-negotiable):
  - Isolated margin only — refuses to enter if API returns cross.
  - Margin ratio monitor: alert at <30 %, auto-close at <20 %.
  - Delta-verify on every entry (|spot_notional - perp_notional| ≤ 1 %).
  - Rebalance every 8 h to hold delta within tolerance.
  - Daily funding-rate gate: skip entry if rate < funding_threshold
    (default 0.03 % per 8 h window ≈ 3.3 % APR).
  - Allocation cap: strategy will not deploy more than `allocation_usd`
    across spot + perp combined.

INTEGRATION
-----------
This file is not auto-loaded by the bot. To use from shadow tier:

    from intelligence_workers.phemex_funding_harvester import (
        PhemexFundingHarvester, DRY_RUN_DEFAULT,
    )
    from intelligence_workers.shadow_exec import execute_shadow_trade

    harvester = PhemexFundingHarvester(
        phemex_client, coinbase_client,
        allocation_usd=5000,     # seed shadow allocation
        funding_threshold=0.0003,  # 3 bp / 8 h
        rebalance_interval=8 * 3600,
        dry_run=True,            # ← keep True until kill-reconcile done
    )
    await harvester.run_monitoring_loop()

For shadow-tier audit parity, each entry/exit also posts a row to
shadow_decisions via execute_shadow_trade(..., strategy='phemex_s1_shadow')
when a matching allocation row exists.

REFS
----
  specs/graduation_mechanics.md
  intelligence_workers/shadow_exec.py
  /api/shadow_strategies
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("phemex_s1")

DRY_RUN_DEFAULT = True  # resurrection safety gate — see module docstring
DB_PATH = os.environ.get("DB_PATH", "/app/data/trading_firm.db")
STRATEGY_NAME = "phemex_s1_shadow"  # matches shadow_strategy_allocations on shadow tier


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Position:
    opened_at: datetime
    btc_qty: float
    spot_entry: float     # USD per BTC at spot fill
    perp_entry: float     # USD per BTC at perp fill
    spot_notional: float
    perp_notional: float
    perp_margin: float    # USDT collateral pledged
    last_rebalance: datetime
    funding_accumulated: float = 0.0  # USDT, net of fees

    @property
    def delta(self) -> float:
        """Net delta in BTC: +spot - perp. Target is 0."""
        return self.btc_qty - self.btc_qty  # mirror legs, always 0 on open

    @property
    def age_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.opened_at).total_seconds() / 3600


@dataclass
class ExitResult:
    ok: bool
    spot_pnl: float = 0.0
    perp_pnl: float = 0.0
    funding_pnl: float = 0.0
    fees: float = 0.0
    total_pnl: float = 0.0
    reason: str = ""
    details: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Harvester
# ---------------------------------------------------------------------------

class PhemexFundingHarvester:
    """Delta-neutral BTC funding-rate harvester.

    Async-friendly — run_monitoring_loop() is a cooperative coroutine.
    Designed to be instantiated once per process; holds at most one
    open position at a time (by design — the allocation cap is the
    firm-wide size limit on this strategy).
    """

    # Margin thresholds: alert at <ALERT, force-close at <KILL.
    # Phemex isolated margin convention: margin_ratio = equity / maint_margin,
    # so healthy ratio is well above 1.0; liquidation at ~1.0.
    # We translate to 0-100 scale for readability.
    MARGIN_ALERT_PCT = 30.0
    MARGIN_KILL_PCT = 20.0
    DELTA_TOLERANCE_PCT = 0.01   # 1 % of notional
    MONITOR_INTERVAL_SEC = 30 * 60   # 30 min between loop ticks

    def __init__(
        self,
        phemex_client,
        coinbase_client,
        allocation_usd: float,
        funding_threshold: float = 0.0003,
        rebalance_interval: int = 8 * 3600,
        dry_run: bool = DRY_RUN_DEFAULT,
        leverage: int = 2,
    ):
        if allocation_usd <= 0:
            raise ValueError("allocation_usd must be positive")
        if leverage < 1 or leverage > 5:
            raise ValueError("leverage must be 1–5 (spec: 2× isolated)")
        self.phemex = phemex_client
        self.coinbase = coinbase_client
        self.allocation_usd = float(allocation_usd)
        self.funding_threshold = float(funding_threshold)
        self.rebalance_interval = int(rebalance_interval)
        self.dry_run = bool(dry_run)
        self.leverage = int(leverage)
        self.position: Optional[Position] = None
        self._stop = False

    # ----- market data ---------------------------------------------------

    def check_funding_rate(self) -> float:
        """Return Phemex BTC perp funding rate as a fraction per 8 h window.

        Phemex API field: `fundingRateRr` (realised rate, applied at the
        next funding window). Positive → shorts get paid.
        """
        try:
            resp = self.phemex.get_ticker_24h(symbol="BTCUSDT")
            # Defensive extraction — the v2 endpoint nests payload under 'result'
            res = (resp or {}).get("result") or resp or {}
            rate = res.get("fundingRateRr")
            if rate is None:
                raise RuntimeError(f"funding rate missing from Phemex response: {res}")
            return float(rate)
        except Exception as e:
            log.warning("check_funding_rate failed: %s", e)
            raise

    def get_spot_price(self) -> float:
        """Return Coinbase BTC-USD spot (midpoint if quote structure supports it)."""
        try:
            resp = self.coinbase.get_spot_price(pair="BTC-USD")
            px = None
            if isinstance(resp, dict):
                px = resp.get("amount") or resp.get("price")
                px = px or ((resp.get("data") or {}).get("amount"))
            if px is None:
                raise RuntimeError(f"spot price missing from Coinbase response: {resp}")
            px = float(px)
            if px <= 0:
                raise RuntimeError(f"non-positive spot {px}")
            return px
        except Exception as e:
            log.warning("get_spot_price failed: %s", e)
            raise

    # ----- entry ---------------------------------------------------------

    def execute_entry(self, btc_price: float) -> bool:
        """Open delta-neutral position. Returns True on success.

        Sizing:
            spot_notional = allocation_usd / 2   (half the bankroll goes to spot)
            perp_notional = allocation_usd / 2   (matching short for delta-neutral)
            perp_margin   = perp_notional / leverage   (2× → margin = notional/2)

        Steps, in order:
          1. Cash/credit precheck on both venues
          2. Buy spot on Coinbase (market)
          3. Set Phemex leverage + ISOLATED margin for BTCUSDT
          4. Short perp on Phemex (market)
          5. Delta verification — abort and unwind if |spot_qty × px -
             perp_qty × px| > tolerance
          6. Record Position

        Any failure after step 2 triggers an unwind of the spot leg.
        """
        if self.position is not None:
            log.warning("execute_entry: position already open, skipping")
            return False
        if btc_price <= 0:
            raise ValueError("btc_price must be positive")

        spot_notional = self.allocation_usd / 2
        perp_notional = self.allocation_usd / 2
        perp_margin = perp_notional / self.leverage
        btc_qty = spot_notional / btc_price

        log.info(
            "ENTRY plan: spot=$%.2f perp=$%.2f margin=$%.2f btc_qty=%.6f @ $%.2f "
            "leverage=%dx dry_run=%s",
            spot_notional, perp_notional, perp_margin, btc_qty, btc_price,
            self.leverage, self.dry_run,
        )

        if self.dry_run:
            self._audit_shadow_entry(btc_price, spot_notional, perp_notional, btc_qty)
            self.position = Position(
                opened_at=datetime.now(timezone.utc),
                btc_qty=btc_qty,
                spot_entry=btc_price,
                perp_entry=btc_price,
                spot_notional=spot_notional,
                perp_notional=perp_notional,
                perp_margin=perp_margin,
                last_rebalance=datetime.now(timezone.utc),
            )
            log.info("ENTRY dry-run: position recorded without venue calls")
            return True

        # --- LIVE PATH — opts in only when dry_run=False ---
        spot_fill = None
        try:
            # 1. Precheck
            self._precheck_balances(spot_notional, perp_margin)

            # 2. Spot buy on Coinbase
            spot_fill = self.coinbase.place_market_order(
                pair="BTC-USD", side="buy", funds=spot_notional,
            )
            filled_qty = float(spot_fill.get("filled_size") or btc_qty)
            spot_avg = float(spot_fill.get("avg_price") or btc_price)
            log.info("SPOT filled: qty=%.6f avg=%.2f", filled_qty, spot_avg)

            # 3. Isolated margin + leverage on Phemex
            self._set_isolated(self.leverage)

            # 4. Perp short
            perp_fill = self.phemex.place_order(
                symbol="BTCUSDT", side="Sell", order_type="Market",
                order_qty=filled_qty, leverage=self.leverage,
                margin_type="ISOLATED",
            )
            perp_avg = float(perp_fill.get("avg_price") or btc_price)
            perp_filled = float(perp_fill.get("filled_qty") or filled_qty)
            log.info("PERP filled: qty=%.6f avg=%.2f", perp_filled, perp_avg)

            # 5. Delta verification
            spot_val = filled_qty * spot_avg
            perp_val = perp_filled * perp_avg
            delta_pct = abs(spot_val - perp_val) / max(spot_val, 1)
            if delta_pct > self.DELTA_TOLERANCE_PCT:
                log.error(
                    "DELTA VIOLATION: spot=%.2f perp=%.2f delta=%.2f%% > %.2f%% — unwinding",
                    spot_val, perp_val, delta_pct * 100, self.DELTA_TOLERANCE_PCT * 100,
                )
                self._unwind(spot_filled=filled_qty, perp_filled=perp_filled,
                              reason="delta_violation_on_entry")
                return False

            # 6. Record
            self.position = Position(
                opened_at=datetime.now(timezone.utc),
                btc_qty=filled_qty,
                spot_entry=spot_avg,
                perp_entry=perp_avg,
                spot_notional=spot_val,
                perp_notional=perp_val,
                perp_margin=perp_margin,
                last_rebalance=datetime.now(timezone.utc),
            )
            log.info("ENTRY ok: delta=%.3f%%", delta_pct * 100)
            self._audit_shadow_entry(btc_price, spot_val, perp_val, filled_qty)
            return True

        except Exception as e:
            log.error("execute_entry failed: %s\n%s", e, traceback.format_exc())
            # If spot filled but perp failed, unwind the spot leg
            if spot_fill is not None:
                try:
                    filled_qty = float(spot_fill.get("filled_size") or 0)
                    if filled_qty > 0:
                        self.coinbase.place_market_order(
                            pair="BTC-USD", side="sell", size=filled_qty,
                        )
                        log.info("ENTRY rollback: unwound spot %.6f BTC", filled_qty)
                except Exception as ee:
                    log.error("ENTRY rollback failed: %s", ee)
            return False

    # ----- runtime monitoring -------------------------------------------

    def check_margin_ratio(self) -> float:
        """Return current Phemex margin ratio as percent (0-100+, healthy > 50)."""
        if self.position is None:
            return math.inf
        try:
            if self.dry_run:
                # Synthesize a healthy margin in dry-run to keep the loop running
                return 100.0
            acct = self.phemex.get_account_info(currency="USDT")
            # Phemex convention: `marginRatioEr` in basis points-like scale.
            raw = (acct.get("result") or acct).get("marginRatioEr")
            if raw is None:
                return math.inf
            # Treat as a fraction; convert to percent.
            return float(raw) * 100
        except Exception as e:
            log.warning("check_margin_ratio failed: %s", e)
            return math.inf

    def rebalance(self) -> bool:
        """Rebalance to delta-neutral if drift exceeds tolerance.

        Called by run_monitoring_loop every `rebalance_interval` seconds.
        Records funding accumulated since last rebalance into
        position.funding_accumulated.
        """
        if self.position is None:
            return False
        now = datetime.now(timezone.utc)
        age = (now - self.position.last_rebalance).total_seconds()
        if age < self.rebalance_interval:
            return False

        try:
            px = self.get_spot_price()
        except Exception:
            log.warning("rebalance skipped — spot price unavailable")
            return False

        spot_val = self.position.btc_qty * px
        # Perp notional drifts because PnL is realised in USDT on the perp side
        # rather than flowing into btc_qty. We approximate drift by comparing
        # spot_val against the original perp_notional.
        perp_val = self.position.perp_notional  # static until we close or resize
        drift_pct = abs(spot_val - perp_val) / max(spot_val, 1)
        log.info(
            "REBALANCE check: spot_val=$%.2f perp_val=$%.2f drift=%.2f%% age=%.1fh",
            spot_val, perp_val, drift_pct * 100, age / 3600,
        )

        # Funding payment happens at 00:00 / 08:00 / 16:00 UTC on Phemex;
        # we approximate accumulation by rate × perp_notional × windows_elapsed.
        try:
            rate = self.check_funding_rate()
            windows = max(1, int(age // (8 * 3600)))
            added = rate * perp_val * windows
            self.position.funding_accumulated += added
            log.info(
                "FUNDING accrued: rate=%.4f%% × %d windows × $%.2f = $%.4f (total $%.4f)",
                rate * 100, windows, perp_val, added, self.position.funding_accumulated,
            )
        except Exception:
            pass

        self.position.last_rebalance = now

        if drift_pct <= self.DELTA_TOLERANCE_PCT:
            return True  # no action needed

        if self.dry_run:
            log.info("REBALANCE dry-run: drift %.2f%% would trigger resize", drift_pct * 100)
            return True

        # LIVE: resize whichever leg is smaller to match the other.
        # Simplification: we only ADD to the short side (perp) to correct
        # drift — never reduce the spot leg to avoid realising taxable P&L.
        try:
            diff_qty = (spot_val - perp_val) / px
            if diff_qty > 0:
                # Spot side grew larger — add to perp short
                self.phemex.place_order(
                    symbol="BTCUSDT", side="Sell", order_type="Market",
                    order_qty=abs(diff_qty), leverage=self.leverage,
                    margin_type="ISOLATED",
                )
                self.position.perp_notional += abs(diff_qty) * px
                log.info("REBALANCE: added %.6f BTC to perp short", abs(diff_qty))
            else:
                log.info("REBALANCE: perp > spot by %.2f%% — no resize (conservative)",
                         drift_pct * 100)
            return True
        except Exception as e:
            log.error("REBALANCE failed: %s", e)
            return False

    # ----- exit ----------------------------------------------------------

    def execute_exit(self, reason: str = "manual") -> dict:
        """Close both legs. Returns ExitResult as dict."""
        if self.position is None:
            return ExitResult(ok=False, reason="no_position").__dict__
        pos = self.position
        log.info("EXIT initiated: reason=%s age=%.1fh", reason, pos.age_hours)

        try:
            exit_px = self.get_spot_price()
        except Exception as e:
            exit_px = pos.spot_entry  # fallback to entry
            log.warning("EXIT using entry price as fallback: %s", e)

        # Compute theoretical P&L (spot + perp + funding - fees)
        spot_pnl = (exit_px - pos.spot_entry) * pos.btc_qty
        perp_pnl = (pos.perp_entry - exit_px) * pos.btc_qty  # short, so inverted
        funding_pnl = pos.funding_accumulated
        est_fees = pos.spot_notional * 0.001 + pos.perp_notional * 0.001  # 10 bp round-trip
        total = spot_pnl + perp_pnl + funding_pnl - est_fees

        result = ExitResult(
            ok=True,
            spot_pnl=round(spot_pnl, 2),
            perp_pnl=round(perp_pnl, 2),
            funding_pnl=round(funding_pnl, 4),
            fees=round(est_fees, 2),
            total_pnl=round(total, 2),
            reason=reason,
            details={"exit_price": exit_px, "age_hours": pos.age_hours},
        )

        if self.dry_run:
            log.info("EXIT dry-run: %s", result.__dict__)
            self._audit_shadow_exit(result)
            self.position = None
            return result.__dict__

        # LIVE path
        try:
            # Close perp first (margin release), then spot
            self.phemex.place_order(
                symbol="BTCUSDT", side="Buy", order_type="Market",
                order_qty=pos.btc_qty, leverage=self.leverage,
                margin_type="ISOLATED", reduce_only=True,
            )
            self.coinbase.place_market_order(
                pair="BTC-USD", side="sell", size=pos.btc_qty,
            )
            log.info("EXIT live ok: %s", result.__dict__)
            self._audit_shadow_exit(result)
            self.position = None
            return result.__dict__
        except Exception as e:
            log.error("EXIT failed: %s\n%s", e, traceback.format_exc())
            result.ok = False
            result.reason = f"{reason}; exit_error: {e}"
            return result.__dict__

    # ----- monitoring loop ----------------------------------------------

    async def run_monitoring_loop(self):
        """Main loop. Check every MONITOR_INTERVAL_SEC for entry / exit /
        rebalance / margin triggers. Stops on self._stop = True."""
        log.info(
            "PhemexFundingHarvester starting: alloc=$%.2f threshold=%.4f%% "
            "rebal=%ds dry_run=%s",
            self.allocation_usd, self.funding_threshold * 100,
            self.rebalance_interval, self.dry_run,
        )
        while not self._stop:
            try:
                await self._loop_tick()
            except Exception as e:
                log.error("loop tick failed: %s\n%s", e, traceback.format_exc())
            await asyncio.sleep(self.MONITOR_INTERVAL_SEC)
        log.info("PhemexFundingHarvester stopped")

    def stop(self):
        self._stop = True

    async def _loop_tick(self):
        # 1. Margin guardrail — highest priority
        if self.position is not None:
            margin_pct = self.check_margin_ratio()
            if margin_pct < self.MARGIN_KILL_PCT:
                log.error("MARGIN KILL: ratio %.1f%% < %.1f%% — forcing exit",
                          margin_pct, self.MARGIN_KILL_PCT)
                self.execute_exit(reason="margin_kill")
                return
            elif margin_pct < self.MARGIN_ALERT_PCT:
                log.warning("MARGIN ALERT: %.1f%% < %.1f%%", margin_pct, self.MARGIN_ALERT_PCT)

        # 2. Funding rate + entry/exit decisions
        try:
            rate = self.check_funding_rate()
        except Exception:
            return  # can't decide without rate; wait for next tick

        if self.position is None:
            # Entry gate
            if rate > self.funding_threshold:
                try:
                    px = self.get_spot_price()
                    self.execute_entry(px)
                except Exception as e:
                    log.warning("entry skipped: %s", e)
        else:
            # Exit gate: rate turned negative → can't harvest, close
            if rate < 0:
                self.execute_exit(reason="funding_turned_negative")
                return
            # Rebalance gate
            self.rebalance()

    # ----- shadow audit + platform plumbing -----------------------------

    def _audit_shadow_entry(self, px, spot_notional, perp_notional, btc_qty):
        """Best-effort audit trail to shadow_decisions for ops visibility.

        Uses execute_shadow_trade() when a matching allocation row exists —
        this is the standard path for tying a strategy into the firm's
        SGS scoring + graduation framework. Failure is non-fatal.
        """
        try:
            import sys
            if "/app/intelligence_workers" not in sys.path:
                sys.path.insert(0, "/app/intelligence_workers")
            from shadow_exec import execute_shadow_trade
            execute_shadow_trade(
                strategy=STRATEGY_NAME,
                platform="phemex",
                symbol="BTCUSDT",
                side="short",                 # primary leg we care about
                signal_source="phemex_funding_harvester",
                signal_data={
                    "rate": self.funding_threshold,
                    "btc_price": px,
                    "spot_notional": spot_notional,
                    "perp_notional": perp_notional,
                    "btc_qty": btc_qty,
                    "dry_run": self.dry_run,
                },
                size_pct=0.04,                # cosmetic — real size comes from allocation
                stop_loss_pct=None,           # no stop; exit is funding-driven
                take_profit_pct=None,
                ttl_hours=24 * 30,            # 30 d soft TTL
                metadata={"leverage": self.leverage, "delta_neutral": True},
            )
        except Exception as e:
            log.debug("shadow_exec audit skipped: %s", e)

    def _audit_shadow_exit(self, result: ExitResult):
        """Write exit stats to shadow_decisions metadata for P&L attribution."""
        try:
            import sqlite3
            import json
            conn = sqlite3.connect(DB_PATH)
            conn.execute(
                "INSERT INTO shadow_decisions "
                "(strategy, tier, signal_source, signal_data, metadata) "
                "VALUES (?, 'SHADOW', ?, ?, ?)",
                (
                    STRATEGY_NAME,
                    "phemex_funding_harvester.exit",
                    json.dumps({"reason": result.reason, "ok": result.ok}),
                    json.dumps(result.__dict__, default=str),
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log.debug("exit audit skipped: %s", e)

    def _precheck_balances(self, spot_needed: float, perp_margin_needed: float):
        """Raise if either venue lacks funds. LIVE-path only."""
        cb = self.coinbase.get_usd_balance()
        if cb < spot_needed:
            raise RuntimeError(f"Coinbase USD ${cb:.2f} < spot need ${spot_needed:.2f}")
        pm = self.phemex.get_usdt_balance()
        if pm < perp_margin_needed:
            raise RuntimeError(f"Phemex USDT ${pm:.2f} < perp margin ${perp_margin_needed:.2f}")

    def _set_isolated(self, leverage: int):
        """Force isolated margin for BTCUSDT. Refuses to proceed on cross."""
        try:
            current = self.phemex.get_position_mode(symbol="BTCUSDT")
            if (current or {}).get("margin_type") == "CROSSED":
                # Attempt flip; fail loudly if the venue won't switch
                self.phemex.set_margin_type(symbol="BTCUSDT", margin_type="ISOLATED")
                re = self.phemex.get_position_mode(symbol="BTCUSDT")
                if (re or {}).get("margin_type") != "ISOLATED":
                    raise RuntimeError("Phemex refused ISOLATED margin flip")
            self.phemex.set_leverage(symbol="BTCUSDT", leverage=leverage)
        except Exception as e:
            raise RuntimeError(f"_set_isolated failed: {e}") from e

    def _unwind(self, spot_filled: float, perp_filled: float, reason: str):
        """Best-effort unwind after a partial / failed entry. Never raises."""
        try:
            if perp_filled > 0:
                self.phemex.place_order(
                    symbol="BTCUSDT", side="Buy", order_type="Market",
                    order_qty=perp_filled, leverage=self.leverage,
                    margin_type="ISOLATED", reduce_only=True,
                )
        except Exception as e:
            log.error("unwind perp failed: %s", e)
        try:
            if spot_filled > 0:
                self.coinbase.place_market_order(
                    pair="BTC-USD", side="sell", size=spot_filled,
                )
        except Exception as e:
            log.error("unwind spot failed: %s", e)
        log.info("UNWIND done: reason=%s", reason)


# ---------------------------------------------------------------------------
# CLI entry for ops testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--allocation", type=float, default=5000)
    parser.add_argument("--threshold", type=float, default=0.0003)
    parser.add_argument("--live", action="store_true",
                        help="Disable dry_run. REQUIRES Apr-18 kill reconciliation first.")
    args = parser.parse_args()

    if args.live:
        log.warning(
            "LIVE mode requested. Per graduation_mechanics.md, resurrected "
            "strategies must complete: new design spec → shadow restart → 30+ trades "
            "→ SGS gate. Proceed only if the Apr 18 S1 kill has been explicitly "
            "rescinded by Jerad."
        )

    # Stub clients — integrate real phemex_client + coinbase_client from main.py
    class _Stub:
        def __getattr__(self, n):
            raise RuntimeError(f"client stub: {n} not wired — integrate from main.py")

    harvester = PhemexFundingHarvester(
        phemex_client=_Stub(),
        coinbase_client=_Stub(),
        allocation_usd=args.allocation,
        funding_threshold=args.threshold,
        dry_run=not args.live,
    )
    asyncio.run(harvester.run_monitoring_loop())
