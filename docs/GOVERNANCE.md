# Real vs Simulated P&L

A row in the `positions` table with `platform='Simulated'` is NOT a trade.
Headline analytics MUST exclude these rows. Simulated P&L is a research
probe, not realized return.

This rule is enforced at the source: `_SQL_REAL_TRADES` and
`_SQL_REAL_TRADES_ALL_TIERS` in `main.py` carry
`AND COALESCE(platform,'') != 'Simulated' AND COALESCE(deleted_sim, 0) = 0`.
Every analytics query that uses these fragments inherits the filter.
`/api/status` exposes both:

- `realized_pnl`, `combined_pnl`, `real_pnl_30d`, `real_pnl_all` —
  broker-fillable rows only
- `sim_pnl_30d`, `sim_pnl_all`, `sim_trades_30d` — synthetic probes,
  clearly labeled, never used for decisions

The dashboard renders both side-by-side with the sim values in amber so
no one mistakes a probe for a return.

`deleted_sim=1` is an audit-trail soft-delete marker. Use it to retire a
sim writer without losing the historical rows. Set on existing rows; the
analytics fragments will exclude them automatically.

## Promotion path

Before any strategy can graduate from SIM to PAPER, it needs:

1. A real entry function that places broker orders (Alpaca, IB, Coinbase, etc.)
2. Lifecycle management (assignment, expiry, exit reasons, MFE/MAE tracking)
3. Wired to `strategy_kill_switch` for auto-disable (see Strategy Disable Principle)
4. Backtested over realistic data (e.g. `backtester/options_replay.py` for VRP)

Before PAPER → LIVE: 30+ real PAPER trades, Sharpe > 1.0, max drawdown < 15%.

## Why this rule exists

On 2026-04-30 the 6-day return-from-trip retro reported `+$2,970` realized
P&L. Investigation traced the headline number to two sim writers
(`scan_cash_secured_puts_sim`, `_simulate_covered_calls`) that wrote rows
directly to SQLite with `platform='Simulated'` and immediate close (no
broker order placed). The actual broker-fillable P&L for the same period
was **-$2,099**. The sim contamination flipped a real loss into an
apparent gain on the dashboard.

`_simulate_covered_calls` was retired the same day (the 0.4% × cost
heuristic was pure fiction); `scan_cash_secured_puts_sim` was kept as a
probe because it queries real Alpaca options chain data, but its rows
will never count as realized return.

# Strategy Disable Principle

A strategy is not disabled until its ENTRY FUNCTION refuses to execute.
Sizing knobs (regime weights, meta_alloc multipliers) do NOT gate entries —
they only scale them. Multiplying by 0.0 still passes through the gate.

When killing a strategy:
1. Add a boolean flag check as the FIRST line of the entry function
2. Verify with grep that the flag actually gates the entry path
3. Test by attempting a paper trade and confirming it's rejected

Apr 23 "prediction disable" failed this principle and lost $1,644 over 6 days.

# Residual HISTORICAL prediction positions (held intentionally)

After the 2026-04-30 prediction kill, two HISTORICAL-tier positions stay open:

- id 193: "Will the U.S. invade Iran before 2027?" YES @ $0.575 — resolves 2026-12-31
- id 230: "NO: Will Kevin Warsh be confirmed as Fed Chair?" NO @ $0.06 — resolves 2026-10-31

Both are tagged `metadata.exempt_prediction_kill = true` in SQLite and in
`paper_portfolio.json`. The `PREDICTION_TIER_ENABLED` gate only blocks NEW
entries in `auto_execute_opportunity` — it does NOT touch existing
positions in `run_exit_manager`, which iterates over all open positions
regardless of strategy. These two will get normal exit-manager attention
and self-resolve at expiry. Manual close also remains available.

# Critical Paging

Trading-bot critical alerts go to Discord. Two delivery paths, deliberately
mixed so that one path failing does not silence the other:

**In-bot path** (uses bot REST API via DISCORD_TOKEN + DISCORD_CHANNEL_ID,
or DISCORD_WEBHOOK_URL if set). Best for rules that need to query the
running bot's state or DB. Implemented as cron'd workers under
`intelligence_workers/`. Currently wired:

- `strategy_watchdog.py` (cron: every 30 min) — two rules:
  - Daily realized P&L (UTC midnight → now) < -$500 → alert once per UTC day.
    State tracked in `pager_state` table to prevent re-fire.
  - Per-strategy: 10+ closed trades in last 24h with 0% WR → alert + record
    in `strategy_kill_switch` table. Detection works today; entry-side
    enforcement (per-strategy gate consulting this table) is the next
    layer's work — see "Per-strategy auto-disable enforcement" below.

**Webhook path** (DISCORD_CRITICAL_WEBHOOK in `.env`, posts directly via
`requests.post` from a host-level cron — no bot process required). Best
for rules that must work when the bot itself is dead.
PENDING — webhook URL not yet provisioned. Will cover:

- Bot container down > 5 min (host cron checks `docker ps`)
- IB Gateway disconnected > 30 min (host cron checks `/api/status`)

## Per-strategy auto-disable enforcement

`strategy_watchdog.py` writes auto-disabled strategies to
`strategy_kill_switch (strategy, disabled, disabled_at, reason, ...)`.
The shared helper `_is_auto_disabled(strategy_name)` (defined in
`main.py` near the `PREDICTION_TIER_ENABLED` block, and inline in worker
files) reads this table and is consulted as the FIRST check in every
strategy entry function. Fail-open: any DB error returns False so a
SQLite hiccup cannot silently halt all strategies.

Wired (verified 2026-04-30 by per-strategy INSERT → entry call →
"auto-disabled" log → DELETE):

| Strategy | Entry function | File:line |
|---|---|---|
| pairs                    | `scan_pairs_opportunities`  | main.py:21345 |
| crypto_pairs             | `scan_crypto_pairs`         | main.py:21860 |
| oracle_trade             | `scan_oracle_signals`       | main.py:15729 |
| options_spread           | `scan_theta_harvest`        | main.py:16480 |
| pead                     | `run_pead_scanner`          | main.py:22916 |
| sympathy_lag             | `run_sympathy_scanner`      | main.py:23055 |
| gamma_pin                | `run_gamma_pin_scanner`     | main.py:23188 |
| liquidity_vacuum         | `run_vacuum_scanner`        | main.py:23298 |
| crypto                   | `auto_paper_execute`        | main.py:9154 (strategy from `opp`) |
| prediction               | `auto_execute_opportunity`  | main.py:10997 (also gated by `PREDICTION_TIER_ENABLED`) |
| crash_hedge_call_spread  | `check_crash_hedges` (sell_premium branch) | main.py:17254 |
| vix_fade                 | `check_crash_hedges` (UVXY branch)         | main.py:17254 |
| crash_hedge_put          | `check_crash_hedges` (buy_puts branch)     | main.py:17254 |
| iron_condor_shadow       | `run`                        | intelligence_workers/iron_condor_farm.py:77 |
| whale_follow             | `dispatch_pending` (Gate 0)  | intelligence_workers/whale_entry_executor.py:372 |

`check_crash_hedges` dispatches to three sub-strategies in one function;
gates are placed inline at each branch so disabling one does NOT silence
the others.

Some strategies have no autonomous main.py entry path:

- `kalshi_shadow`, `sec_8k_signal_shadow`, `options_flow_signal_shadow`,
  `supply_chain_ripple_shadow`, `cfo_agent` — workers publish *signals*
  to Redis/SQLite, not positions. Position creation happens elsewhere.
  If a watchdog ever auto-disables one of these, the kill must be wired
  at whichever consumer turns the signal into a position.
- `vrp_income`, `covered_call` — currently SIM_ONLY / passive bookkeeping;
  no autonomous entry path was found. Re-audit before promoting them
  beyond simulation.

## Test commands

Force-fire either watchdog rule:
```bash
docker exec traderjoes-bot python3 /app/intelligence_workers/strategy_watchdog.py \
    --force-pnl-alert --force-zerowr
```

Verify a per-strategy gate (substitute `<name>`):
```bash
sqlite3 /root/trading-bot/data/trading_firm.db \
    "INSERT OR REPLACE INTO strategy_kill_switch
     (strategy, disabled, disabled_at, reason, closed_trades_24h, win_rate_pct, operator)
     VALUES ('<name>', 1, datetime('now'), 'manual-test', 0, 0, 'test');"
# trigger the relevant entry function (or restart bot and watch logs)
sqlite3 /root/trading-bot/data/trading_firm.db \
    "DELETE FROM strategy_kill_switch WHERE strategy='<name>';"
```

Force-fire the host-level monitors (after `DISCORD_CRITICAL_WEBHOOK` is in `.env`):
```bash
echo $(($(date +%s) - 360)) > /tmp/health_bot_down_since.txt
docker stop traderjoes-bot && /root/trading-bot/scripts/healthcheck_bot.sh; docker start traderjoes-bot
```

## Testing the watchdog

Force-fire either rule without waiting for natural triggers:
```bash
docker exec traderjoes-bot python3 /app/intelligence_workers/strategy_watchdog.py \
    --force-pnl-alert --force-zerowr
```
Force fires bypass the once-per-day state, so they're safe to re-run.

# Deferred Systems

Systems that are intentionally shut down because no current strategy
depends on them. Document state explicitly so a future maintainer doesn't
spend hours diagnosing a "broken" subsystem that is, in fact, off on
purpose.

## Interactive Brokers Gateway

- **Shut down:** 2026-04-30
- **What:** `traderjoes-ib-gateway` container (Python wrapper around the
  upstream IB Gateway socket); env var `ORACLE_IB_FUTURES` flipped to `0`;
  host-level `traderjoes-healthcheck-ib` cron commented out.
- **Reason:** No strategy currently routes to IB. `oracle_futures` is
  the only IB-routed strategy and it is `PAPER_30DAY` /
  `validated_awaiting_live` — even before this shutdown, `ORACLE_IB_FUTURES=1`
  was firing zero orders because Oracle is paper-tier. The wrapper had been
  retrying a dead socket every 5 min for ~6 days, generating log noise and
  surfacing in `/api/status` as a permanent "IB disconnected" warning.
- **What is NOT affected:** equities trading via Alpaca, options sims,
  Polymarket, Kalshi, Phemex, Coinbase, the Discord bot, the watchdog,
  the host-level bot healthcheck, the PAPER tier accounting. None of
  them depend on IB.

### Restart procedure

When the first IB-routed strategy is ready for paper validation:

1. Confirm the upstream IB Gateway daemon is running (Windows side —
   re-login if it has been off for > 24h, since the session token
   expires).
2. `docker start traderjoes-ib-gateway`
3. Verify the Python wrapper is publishing heartbeats:
   `docker exec traderjoes-bot redis-cli GET ib_gateway:status` should
   return a JSON blob with `connected: true` within ~30 sec.
4. Verify `/api/status` reports `ib.connected: true`.
5. Set `ORACLE_IB_FUTURES=1` in `/root/trading-bot/.env`.
6. Re-enable the IB healthcheck cron: uncomment the `*/5 * * * *` line
   in `/etc/cron.d/traderjoes-healthcheck-ib`.
7. **Recreate** the bot container (`docker compose up -d traderjoes-bot`)
   so it picks up the new env. `docker restart` alone reuses the old env
   block.
8. Confirm a synthetic Oracle paper trade routes through IB.

### Restart trigger criteria

Don't restart until a strategy is actually ready. The trigger isn't
"IB is fixable" — it's "we have a strategy that needs IB and meets the
SIM → PAPER promotion bar." In practice this means oracle_futures
clears its 30-day paper validation, or a new IB-routed strategy
graduates from SHADOW.
