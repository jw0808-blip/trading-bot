# TraderJoes Handoff — March 24, 2026

## Current State

| Metric | Value |
|---|---|
| Paper cash | $24,588.49 |
| Open positions | 8 (across 4 strategies) |
| Total deployed | $2,781.51 |
| Total equity | $27,370.00 |
| P&L from $25K start | +$2,370.00 (+9.5%) |
| Trading mode | Paper (DRY_RUN_MODE=True) |
| Bot status | Online, healthy |
| Total trades (all-time) | 150 |
| Closed positions | 71 (realized PnL: -$5.05) |

### Open Positions

| Position | Cost | Strategy | Platform | Opened |
|---|---|---|---|---|
| Will the Iranian regime fall by March 31? | $248.94 | prediction | Polymarket | Mar 18 |
| US x Iran ceasefire by March 31? | $245.02 | prediction | Polymarket | Mar 19 |
| PAIRS:PFE/MRK | $723.28 | pairs | Alpaca | Mar 19 |
| PAIRS:ADBE/CRM | $701.58 | pairs | Alpaca | Mar 19 |
| Kharg Island no longer under Iranian control? | $221.56 | prediction | Polymarket | Mar 20 |
| Will the Fed increase interest rates by 25+ bps? | $253.73 | prediction | Polymarket | Mar 23 |
| TAO $315.28 (+12.0% 24h) | $250.57 | crypto | Crypto/Coinbase | Mar 24 |
| HEDGE:SPY PUT $640 | $136.85 | crash_hedge_put | Alpaca | Mar 24 |

### By Strategy

| Strategy | Count | Deployed |
|---|---|---|
| crash_hedge_put | 1 | $136.85 |
| crypto | 1 | $250.57 |
| pairs | 2 | $1,424.86 |
| prediction | 5 | $1,069.80 |

---

## Architecture

### Single-file bot: `main.py` (~7,700 lines)

Discord bot (`discord.py` commands framework) running in Docker. All trading logic, API integrations, and state management in one file.

### Scan Loop (`alert_scan_task`, every 10 min)

```
1. check_and_send_alerts()        — Kalshi, Polymarket, crypto EV scanner
2. auto_paper_execute()            — Paper trade execution for alerts
3. scan_pairs_opportunities()      — Pairs z-score scanner (market hours)
4. run_pead_scanner()              — Post-earnings drift (market hours, min % 30 < 10)
5. check_funding_rate_arb()        — Phemex funding rates (24/7)
6. check_crash_hedges()            — SPY puts + shorts (market hours)
7. run_exit_manager()              — Unified exit for ALL positions
8. auto_resolve_expired()          — Close expired prediction markets
```

### Strategy Engines

| Strategy | Status | Entry | Exit | Notes |
|---|---|---|---|---|
| Prediction markets | Active | EV scanner → `auto_paper_execute` | Hold to resolution | Kalshi + Polymarket, YES and NO contracts |
| Pairs trading | Active | Z-score entry → Alpaca paper orders | Z-revert / Z-break / 7d TTL | 30 seed pairs, both legs submitted to Alpaca |
| Crypto momentum | Active | CoinGecko scan → `auto_paper_execute` | 4h TTL | Meme blacklist, $1 min price, $10M min vol |
| Funding rate arb | Active | Phemex rate check → Coinbase spot + Phemex perp | 24h TTL | Dual-leg: spot buy + perp short |
| Crash hedge | Active | VIX/regime check | Put: DTE expiry / Short: stop+target+7d TTL | SPY puts (VIX>25) + shorts (VIX>35) |
| PEAD | Broken | — | — | See open bugs |

### Exchanges Connected

| Exchange | Auth | Used For |
|---|---|---|
| Alpaca | API key/secret | Pairs orders (long+short), SPY options, SPY shorts, market data |
| Coinbase | JWT/ES256 | Crypto spot orders, live price quotes |
| Phemex | HMAC-SHA256 | Funding rate data, perp short orders |
| Polymarket | py-clob-client | YES and NO contract orders |
| Kalshi | RSA-PSS | Prediction market orders (YES and NO) |
| Robinhood | Ed25519 | Balance/holdings only (no active trading) |

---

## What Was Built Today (March 24)

### Phase 1: Bi-directional Trading

**Alpaca short selling for pairs** (`29a039f`)

Before: Pairs entry only tracked trades in PAPER_PORTFOLIO, never submitted orders. After: Both legs submit real market orders to Alpaca paper API — buy for long leg, sell for short leg. If either order fails, the position is not opened (long cancelled if short fails). Order IDs stored in position metadata.

**Polymarket NO contracts** (`29a039f`)

Scanner now generates "High-YES NO Buy" opportunities when YES > $0.65 on catalyst events. Uses `clobTokenIds[1]` for the NO token. `auto_paper_execute` and `auto_execute_opportunity` both route NO contracts correctly.

### Phase 2: Silent Activations

**PEAD scanner fix** (`2d205af`)

Fixed `ALPACA_KEY` → `ALPACA_API_KEY` in `_submit_pead_order`. Changed call site to use `sys.modules["__main__"]` lookup to work around a persistent scope issue where `globals()` doesn't see late-defined functions at runtime. Still not fully resolved — see open bugs.

**Kalshi volume + NO contracts** (`2d205af`)

Added `KALSHI_MIN_VOLUME = $50,000` threshold. New opportunity types: "Catalyst YES" (yes_price $0.02-$0.20) and "Catalyst NO" (yes_price > $0.65) on whitelisted events. `execute_kalshi_order` updated to handle `BUY_NO` action.

### Phase 3: Crypto Engines

**Crypto momentum scanner reactivated** (`33ea7dd`)

Removed the `return []` that disabled it. Added hard filters:
- Meme blacklist: SHIB, DOGE, PEPE, FLOKI, BONK, WIF, RAIN, HYPE, BOME, MEME, TRUMP, MELANIA
- Min price: $1.00 (verified via live Coinbase quote)
- Min 24h volume: $10M
- Already trading — picked up TAO on first cycle.

**Phemex funding arb wired end-to-end** (`33ea7dd`)

New `execute_phemex_perp_short()` with HMAC-SHA256 signing. When funding rate exceeds threshold: Coinbase spot buy → Phemex perp short. If perp fails, spot is unwound. Both order IDs tracked in SQLite.

### Crash Hedge Module (`31a6900`)

| Trigger | Action | Size | Exit |
|---|---|---|---|
| VIX > 25, regime elevated/extreme | Buy SPY 7-DTE put, strike 2% below spot | 0.5% portfolio | Expire at DTE |
| VIX > 35, regime extreme | Short SPY | 1% portfolio | 3% stop / 10% target / 7d TTL |

Safety: max 2 concurrent hedges, 12-hour cooldown. Live SPY price fetched for short stop/target checks.

Already triggered: VIX=26.3 → SPY PUT $640 strike, $137 position.

### Bugs Fixed Today

**GM/F cooldown was inside dedup block** (`4c6300c`)

The cooldown check only ran when the position was already open (useless). Moved before the dedup check. Increased from 30 min to 4 hours. Confirmed blocking: `PAIRS COOLDOWN: PAIRS:GM/F closed 10 min ago (need 240)`.

**Pairs strategy label "prediction"** (`4c6300c`)

PFE/MRK and ADBE/CRM were labeled `strategy: "prediction"` in JSON. Fixed to `"pairs"`. SQLite was already correct.

**`auto_paper_execute` missing `db_open_position()`** (`a6b5851`)

The main paper trade path only called `db_log_paper_trade()`, not `db_open_position()`. Trades were invisible to `!paper-pnl` which reads from the `positions` table. Added the missing call. Backfilled 4 positions.

**Crypto strategy detection** (`a6b5851`)

Was using hardcoded keyword list that missed TAO. Changed to `opp.get("platform").lower() == "crypto"`.

**Crypto dedup used full market string** (`9d00712`)

Market strings include live price (`"TAO $315.28 (+12.0% 24h)"`) which changes every cycle, defeating exact-match dedup. Now uses `"CRYPTO:TAO"` based on `opp["ticker"]`. Added LIKE fallback for legacy rows.

---

## Open Bugs

### 1. PEAD scanner: `run_pead_scanner not found in __main__ module`

**Severity:** Medium (feature broken, not a crash)

The function is defined at module top-level (line ~7300), imports fine via `hasattr(main, 'run_pead_scanner')`, and exists in `globals()` at import time. But at runtime inside the Discord event loop, both `globals().get()` and `sys.modules["__main__"]` lookups fail. The function isn't in the `__main__` namespace when the bot is running, despite being defined there.

**Theories:** Possibly discord.py's `@tasks.loop` decorator runs the coroutine in a different namespace, or there's a module-loading race condition. The same pattern works for `run_exit_manager` (defined at line ~7100) but not for `run_pead_scanner` (line ~7300).

**Next step:** Move the entire PEAD block (lines 7123-7300) above `alert_scan_task` (line ~3100). This is the nuclear option but would eliminate any ordering/loading issue.

### 2. `datetime.utcnow()` deprecation warnings

**Severity:** Low

Used in `db_save_daily_state()` and `db_load_daily_state()`. Should be `datetime.now(timezone.utc)`.

### 3. TAO position still labeled `strategy: "prediction"` in JSON

**Severity:** Low (cosmetic)

The first TAO position in `paper_portfolio.json` was created before the strategy detection fix. It shows as `prediction` in the JSON but `crypto` in SQLite. Will self-correct when the position closes and new ones open with the fixed code.

---

## What to Watch Tomorrow (March 25)

### PFE/MRK and ADBE/CRM hit 7-day TTL on March 26

Both pairs opened March 19. The exit manager will auto-close them Wednesday with live Alpaca prices. PFE/MRK z-score is still at -1.41 (not reverted). ADBE/CRM at -1.61. If z-scores revert before TTL, they'll exit early via Z-REVERT.

### SPY Put hedge expires ~March 28-31

The $640 strike put was opened today. It has a 7-DTE expiry window. If VIX drops below 25 and regime returns to normal, the put expires worthless (cost: $137). If market drops, it should gain value.

### March 31 — Three Iran prediction markets expire

Total exposure: $715.52. `auto_resolve_expired` will close them with 5% salvage value if not manually resolved. Watch for actual outcomes.

### Crash hedge cooldown

The 12-hour cooldown means no new hedges until ~05:00 UTC March 25. If VIX stays above 25 after that, another put may be opened.

### TAO crypto position (72h TTL → exits Mar 27)

First crypto momentum trade since scanner reactivation. Exit manager will close at 72h or 4h TTL (crypto TTL is 4h in EXIT_CONFIG). Check if it exits prematurely.

### Verify crypto dedup holds

The TAO dedup fix was tested for one cycle. Monitor overnight to confirm no duplicate crypto positions open.

---

## Key Files

| File | Purpose |
|---|---|
| `main.py` | Everything — bot, strategies, APIs, state management (~7,700 lines) |
| `/app/data/paper_portfolio.json` | Primary state: cash, positions, trades |
| `/app/data/trading_firm.db` | SQLite: `positions`, `paper_trades`, `daily_state`, `resolutions` |
| `/app/data/analytics.json` | Trade analytics, equity tracking |
| `docker-compose.yml` | Bot + Redis + Netdata + OpenClaw containers |
| `HANDOFF_MARCH24.md` | This document |

## Startup Sequence

1. `on_ready()` fires
2. `init_redis()` — connect to Redis signal bus
3. `init_db()` — create all SQLite tables including `positions`
4. `load_all_state()` — load `paper_portfolio.json`, analytics, signals, memory
5. `db_load_daily_state()` — restore daily trade count/PnL (cash only if zero)
6. SQLite backfill — only if `paper_portfolio.json` had no positions
7. Start `daily_report_task` and `alert_scan_task` loops

## Commits Today (March 24)

```
31a6900 feat: add crash hedge module — SPY puts and directional shorts
9d00712 fix: crypto dedup uses ticker symbol, not full market string with price
a6b5851 fix: auto_paper_execute now writes to SQLite positions table
33ea7dd feat: reactivate crypto momentum scanner, wire Phemex funding arb execution
2d205af feat: fix PEAD scanner NameError, add Kalshi volume filter + NO contracts
29a039f feat: wire Alpaca paper orders for pairs, add Polymarket NO contracts
4c6300c fix: move pairs cooldown before dedup check, increase to 4 hours
```
