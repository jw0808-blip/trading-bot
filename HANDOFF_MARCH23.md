# TraderJoes Handoff — March 23, 2026

## Current State

| Metric | Value |
|---|---|
| Paper cash | $25,241.83 |
| Open positions | 5 |
| Total deployed | $2,140.37 |
| Total equity | ~$27,382 |
| Trading mode | Paper |
| Bot status | Online, healthy |

### Open Positions

| Position | Cost | Strategy | Opened |
|---|---|---|---|
| Will the Iranian regime fall by March 31? | $248.94 | prediction | Mar 18 |
| US x Iran ceasefire by March 31? | $245.02 | prediction | Mar 19 |
| PAIRS:PFE/MRK | $723.28 | pairs | Mar 19 |
| PAIRS:ADBE/CRM | $701.58 | pairs | Mar 19 |
| Kharg Island no longer under Iranian control by March 31? | $221.56 | prediction | Mar 20 |

ZEC was closed this session (TTL: crypto 72h limit). GM/F pair was opened and closed within the same cycle (Z-revert at z=-1.87, PnL -$0.37).

---

## Architecture

### Single-file bot: `main.py` (~7200 lines)

Discord bot (`discord.py` commands framework) running in Docker. All trading logic, API integrations, and state management live in one file.

### Task loops

| Loop | Interval | Purpose |
|---|---|---|
| `alert_scan_task` | 10 min | Scans Kalshi, Polymarket, pairs, crypto, arb opportunities. Runs exit manager each cycle. |
| `daily_report_task` | 24h | Posts daily summary to Discord |
| `daily_reset_task` | 24h | Resets trade counters at midnight UTC |

### Strategy engines

| Strategy | Status | Notes |
|---|---|---|
| Prediction markets (Kalshi/Polymarket) | Active | EV scanner, auto-execution via `auto_execute_opportunity` |
| Pairs trading (Alpaca data) | Active | 30 seed pairs, z-score entry/exit, 7-day TTL |
| Crypto (Coinbase/Robinhood) | Active | Spot positions, 72h TTL |
| Funding rate arb (Phemex) | Active | 24h TTL, min 0.03% rate |
| PEAD (post-earnings drift) | Broken | See open bugs |
| Momentum scanner | Disabled | Removed in `39f05f2` |

### Exchanges connected

Kalshi (RSA-PSS), Polymarket (CLOB + on-chain), Robinhood Crypto (Ed25519), Coinbase Advanced Trade (JWT/ES256), Phemex (HMAC), Alpaca (market data only for pairs)

---

## What Was Fixed Today (March 23)

### The Great Ledger Unification (`6dba424`)

**Problem:** Two completely independent position tracking systems were running in parallel:

1. `PAPER_PORTFOLIO["positions"]` — written by the scanner, pairs, arb, and TWAP engines. Exited by `run_exit_manager()`. Persisted to `paper_portfolio.json`.
2. `OPEN_POSITIONS[]` / `CLOSED_POSITIONS[]` — written by `auto_execute_opportunity()` (V9 engine). Exited by `check_and_manage_exits()`. Persisted to `positions.json`.

These never talked to each other. The V9 auto-executor deducted nothing from cash. The V9 exit manager returned nothing to cash. Result: ~$10,550 in phantom capital loss.

**Fix — 5 steps:**

1. **Foundation:** Added `CREATE TABLE IF NOT EXISTS positions` to `init_db()` with all 21 columns (was missing entirely).
2. **Unified opens:** `auto_execute_opportunity` now writes to `PAPER_PORTFOLIO["positions"]`, deducts from cash, and calls `db_open_position()` + `db_log_paper_trade()`.
3. **Unified exits:** `run_exit_manager` is the single exit path. Fetches live prices from Alpaca (pairs) and Coinbase (crypto). Returns capital + realized PnL to cash. Writes to both SQLite tables.
4. **Amputation:** Deleted `check_and_manage_exits`, `OPEN_POSITIONS`, `CLOSED_POSITIONS`, `save_positions()`, `load_positions()`, `POSITION_FILE`. Rewired `!positions` and `!closed` commands.
5. **Cash guard:** `db_load_daily_state()` no longer overwrites `PAPER_PORTFOLIO["cash"]` on startup unless cash is 0/unset.

### Startup load fix (`112f111`)

**Problem:** `on_ready()` never called `load_all_state()`. The bot always started with the hardcoded `PAPER_PORTFOLIO = {"cash": 10000.0, ...}` default and ignored `paper_portfolio.json` entirely. Cash resets, position state — all lost on every restart.

**Fix:** `load_all_state()` now runs first in `on_ready()`. SQLite backfill of positions only triggers when JSON had no positions (fresh deploy scenario).

### Earlier fixes this week

- `39f05f2` — 30-min minimum hold for pairs, disabled momentum scanner
- `fddc90f` — Fixed ALPACA env var names, datetime conflict in cooldown logic
- `0d58eca` — Store entry prices on pairs open for live PnL calculation
- `a2bc344` — Live price PnL on exit (Alpaca for pairs, Coinbase for crypto)
- `e333f9f` — GM/F 30-min cooldown after close to prevent rapid re-entry

---

## Open Bugs

### 1. PEAD scanner: `name 'run_pead_scanner' is not defined`

**Severity:** Medium (feature broken, not a crash — caught by try/except)

**Symptom:** Every 10-min cycle when `minute % 30 < 10`, the log shows:
```
PEAD error: name 'run_pead_scanner' is not defined
```

**Root cause:** Unclear. The function is defined at module top-level (line ~7110), confirmed importable via `hasattr(main, 'run_pead_scanner')`. The error appeared on older container instances and seemed to resolve after force-recreate, but has recurred. May be a stale `.pyc` cache issue or a conditional import failure in the PEAD dependencies (`pytz`, `yfinance`).

**Impact:** No PEAD trades are executing. The scanner is enabled (`PEAD_ENABLED = True`) but never runs.

### 2. Pairs positions have `strategy: "prediction"` label

**Severity:** Low (cosmetic, functional impact minimal)

Pairs positions opened by the pairs scanner get `strategy: "prediction"` in the paper portfolio instead of `strategy: "pairs"`. The exit manager still handles them correctly because it checks for `long_leg`/`short_leg` fields, but the `!positions` display and any strategy-filtered queries will miscategorize them.

### 3. SQLite `positions` vs `paper_trades` table drift

**Severity:** Low (legacy, shrinking)

The `paper_trades` table currently has 6 open rows while `positions` has 4. These were created by different code paths before unification. Going forward both tables are written by every open/close, but old rows won't reconcile. Not worth a migration — they'll age out as positions close.

### 4. `datetime.utcnow()` deprecation warnings

**Severity:** Low

```
DeprecationWarning: datetime.datetime.utcnow() is deprecated
```

Used in `db_save_daily_state()` and `db_load_daily_state()`. Should be replaced with `datetime.now(timezone.utc)` but not urgent.

---

## What to Watch This Week

### March 31 — Three prediction markets expire

The three Iran-related positions all have "by March 31" deadlines:
- Iranian regime fall ($248.94)
- US x Iran ceasefire ($245.02)
- Kharg Island control ($221.56)

Total exposure: $715.52. These will need manual resolution or the `auto_resolve_expired` function will close them with 5% salvage value. Watch for actual outcomes and consider closing before expiry if the market moves.

### Pairs positions (PFE/MRK, ADBE/CRM) — 7-day TTL

Both opened March 19, so they hit the 7-day TTL on March 26. The exit manager will auto-close them with live Alpaca prices. PFE/MRK currently showing z=-1.53 (signal still active). ADBE/CRM showing EV of 22.5%.

### GM/F re-entry pattern

GM/F pair keeps getting opened and immediately closed (z-score reverts too fast). The 30-min cooldown (`e333f9f`) was supposed to fix this but the pair is still triggering. May need to increase the cooldown or add the pair to an exclusion list.

### Cash accounting validation

After the ledger unification, verify over the next few days that:
- `cash + sum(open position costs) ≈ starting balance + realized PnL`
- No phantom cash leaks on position close
- `paper_portfolio.json` survives bot restarts with correct values

---

## Key Files

| File | Purpose |
|---|---|
| `main.py` | Everything — bot, strategies, APIs, state management |
| `/app/data/paper_portfolio.json` | Primary state: cash, positions, trades |
| `/app/data/trading_firm.db` | SQLite: `positions`, `paper_trades`, `daily_state`, `resolutions` |
| `/app/data/analytics.json` | Trade analytics, equity tracking |
| `docker-compose.yml` | Bot + Redis + Netdata + OpenClaw containers |

## Startup Sequence

1. `on_ready()` fires
2. `init_redis()` — connect to Redis signal bus
3. `init_db()` — create SQLite tables if missing (including `positions`)
4. `load_all_state()` — load `paper_portfolio.json`, analytics, signals, memory
5. `db_load_daily_state()` — restore daily trade count/PnL (cash only if zero)
6. SQLite backfill — only if `paper_portfolio.json` had no positions
7. Start `daily_report_task` and `alert_scan_task` loops
