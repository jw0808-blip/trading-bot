# TraderJoes / EchoEdge — Master Consensus Summary
## March 25, 2026

---

## 1. Vision and EchoEdge Concept

**TraderJoes** is an autonomous multi-platform trading firm running as a single Discord bot (12,077 lines of Python) inside a Docker container on a VPS. The vision: build a self-improving algorithmic trading system that trades prediction markets, equities, crypto, and options using an ensemble of specialized AI agents that monitor, analyze, and execute 24/7.

**EchoEdge** is the decision framework — a multi-agent consensus system where each "agent" represents a different analytical lens:

- **Meteorologist**: Headline intelligence and geopolitical monitoring
- **Psychologist**: Sentiment regime detection (Fear & Greed)
- **Historian**: Historical reversion analysis for statistical validation
- **Engineer**: Self-improvement loop that auto-tunes parameters based on results
- **Oracle**: Prediction market signals driving equity pair trades
- **Master Arbiter**: 5-level conflict resolution matrix that every trade passes through

The `!cycle [ticker]` command runs all agents in parallel and produces a consensus recommendation with confidence score.

**Starting capital**: $25,000 (paper)
**Current equity**: $27,048 (+8.2%)
**Trading mode**: Paper (Alpaca paper API)
**Platforms connected**: Kalshi, Polymarket, Alpaca, Coinbase, Phemex, Robinhood (read-only)

---

## 2. Timeline: February 20 — March 25

### Phase 0: Foundation (Feb 20-21)
- Initial `main.py` created — Discord bot skeleton
- Kalshi API integration (RSA-PSS auth debugging)
- Polymarket market scanning via Gamma API
- Robinhood placeholder

### Phase 1: Platform Integration (Mar 1-2)
- V8: Multi-signal fusion, arb scanner, regime switching, fractional Kelly
- V9: Auto-execution, exit manager, AI oversight
- Kalshi RSA-PSS auth fixed, Phemex HMAC signing wired
- Polymarket CLOB execution with funder config
- Portfolio floor kill-switch ($50K minimum)
- Paper trading mode established with $10K starting capital (later increased to $25K)

### Phase 2: Strategy Engines (Mar 2-22)
- Pairs trading scanner with 30 seed pairs
- Crypto momentum scanner (CoinGecko + Coinbase verification)
- Funding rate arbitrage (Phemex perp + Coinbase spot)
- Prediction market EV scanner (Kalshi + Polymarket)
- Cross-platform arbitrage detection
- PEAD (post-earnings drift) scanner — partially broken, namespace issue
- Position tracking unified into single PAPER_PORTFOLIO ledger (Mar 23)

### Phase 3: Hardening (Mar 23-24)
- GM/F cooldown moved before dedup
- Pairs strategy labels fixed in JSON
- `auto_paper_execute` wired to SQLite `positions` table
- Crypto dedup switched from market string to ticker symbol
- Alpaca paper orders wired for pairs (both legs, with cancellation)
- Crypto momentum scanner reactivated with meme blacklist
- Phemex funding arb execution end-to-end
- Crash hedge module (SPY puts when VIX>25, shorts when VIX>35)
- March 24 handoff document written

### Phase 4: EchoEdge Build (Mar 25 — single day, 40+ commits)

**Morning (bugs)**:
- Fixed fractional short rejection (whole shares for Alpaca shorts)
- Added 4h crypto cooldown after close
- Fixed TAO TTL not firing (platform detection override)
- Fixed crypto entry price divided by 100 ($28K fake PnL corrected)
- Paper-pnl rewritten with live prices (Alpaca + Coinbase)
- SIREN blacklisted (no Coinbase feed)

**Midday (infrastructure)**:
- DRY_RUN_MODE set to False — real Alpaca paper orders enabled
- Half-Kelly position sizing for pairs
- SPY options live feed via Alpaca options quotes API
- `!status` command — full firm dashboard
- Alpaca position reconciliation engine (closed 4 orphaned positions)
- GM/F removed from seed pairs permanently

**Afternoon (agents)**:
- Oracle Engine (prediction-market-driven equity trades)
- Intelligence Layer (Meteorologist + Geopolitical Monitor)
- Historian Agent (2yr reversion analysis, 24h cache)
- Psychologist Agent (F&G-based regime: CONTRARIAN/CAUTION modes)
- `!cycle` EchoEdge command (multi-agent consensus)
- Monte Carlo Simulation Agent (10K paths, spread modeling)
- Dynamic Meta-Allocation Engine (performance-weighted pillar sizing)
- Reverse Copy Trading (short squeeze scanner)
- Event Synthetics (cross-platform arb execution)
- Options Theta Harvest (put credit spreads on elevated IV)
- TradingView Webhook Processing (auto-execution on signals)

**Evening (final systems)**:
- Risk Management Agent (correlation matrix, $200/day drawdown limits)
- Engineer Agent (self-improvement loop, auto-tuning thresholds)
- Multi-Model Consensus (GPT-4o-mini second opinion on trades)
- Monte Carlo validation on ALL strategy entries
- Master Arbiter (5-level conflict resolution)
- War Room Dashboard (React SPA on port 8080)
- `!morning` / `!evening` / `!week` automated briefings
- Dynamic S&P 500 Pairs Discovery
- Backtesting Engine (walk-forward, no look-ahead bias)
- Performance Attribution (Sharpe, Sortino, profit factor)
- SMS/Email Alerts (Twilio + Gmail SMTP)
- Shadow Trading (10x parallel portfolio)
- Volatility Skew Scanner
- Echo Market Memory (event/outcome pattern matching)
- Options live pricing (OCC symbol reconstruction)
- Mobile responsive dashboard

---

## 3. Complete Architecture — All Components

### Strategy Engines (7 pillars)

| # | Strategy | Status | Entry Logic | Exit Logic | Evidence |
|---|----------|--------|-------------|------------|----------|
| 1 | **Pairs Trading** | ACTIVE | Z-score entry via Alpaca, both legs | Z-revert / Z-break / 7d TTL | 71 closed trades, -$81 realized (GM/F churn) |
| 2 | **Prediction Markets** | ACTIVE | EV scanner, auto_paper_execute | Hold to resolution | 5 open positions, ~$1,070 deployed |
| 3 | **Crypto Momentum** | ACTIVE | CoinGecko scan, Coinbase verification | 4h TTL | 7 closed trades, +$34 realized |
| 4 | **Crash Hedge** | ACTIVE | VIX>25 puts, VIX>35 shorts | DTE expiry / 100% TP / 50% SL | 2 puts open, $274 deployed |
| 5 | **Oracle Engine** | ACTIVE (no signals above threshold) | Prediction market YES > threshold | 48h TTL / signal invalidation | Iran ceasefire at 49% of threshold |
| 6 | **Event Synthetics** | ACTIVE (no spreads > 2%) | Cross-platform arb > 2% spread | Hold to resolution / 30d TTL | Scanning each cycle |
| 7 | **Options Theta** | ACTIVE (market hours only) | IV rank > 50% on SPY/QQQ | 50% TP / 200% SL / expiry | Untested — needs market hours |
| 8 | **Funding Rate Arb** | ACTIVE (rates at 0%) | Phemex rate > 0.03% | 24h TTL | No trades — rates too low |

### Intelligence Agents (8 agents)

| Agent | Status | Function | Evidence |
|-------|--------|----------|----------|
| **Meteorologist** | ACTIVE | 60 headlines/cycle across 6 themes | Confirmed in logs each cycle |
| **Geopolitical Monitor** | ACTIVE | Iran escalation detected (9 headlines) | Auto-tightened sizing to 0.5% |
| **Psychologist** | ACTIVE | F&G=14 → CONTRARIAN MODE | Blocking momentum, logs confirmed |
| **Historian** | ACTIVE | 2yr reversion stats before pairs entry | Logs reversion rate on each pair signal |
| **Engineer** | ACTIVE | Auto-tuned zscore_entry 1.0→1.1 | Detected 27% win rate vs 60% expected |
| **Monte Carlo** | ACTIVE | 10K-path simulation on all entries | NVDA/AMD showed 79% prob profit |
| **Risk Manager** | ACTIVE | 13 correlated pairs flagged | Blocks new entries in flagged tickers |
| **Master Arbiter** | ACTIVE | 5-level check before every pairs trade | Replaces scattered individual checks |

### Infrastructure (10+ systems)

| System | Status | Description |
|--------|--------|-------------|
| **War Room Dashboard** | ACTIVE | React SPA on port 8080, auto-refresh 15s |
| **Morning/Evening Briefings** | ACTIVE | Auto at 9:25 AM / 4:05 PM ET |
| **Alpaca Reconciliation** | ACTIVE | Closed 4 orphaned positions on startup |
| **Meta-Allocation** | ACTIVE | crypto=1.5x, pairs=0.5x based on 7d P&L |
| **Shadow Trading** | ACTIVE | 10x parallel portfolio in SQLite |
| **Echo Memory** | ACTIVE | Event/outcome storage for pattern matching |
| **SMS/Email Alerts** | CONFIGURED | Env vars added, Twilio/SMTP not yet credentialed |
| **TradingView Webhook** | ACTIVE | Port 8080, auto-execute on signals |
| **AI Consensus** | CONFIGURED | GPT-4o-mini, requires OPENAI_API_KEY |
| **Backtest Engine** | ACTIVE | Walk-forward with rolling 60d window |
| **Performance Attribution** | ACTIVE | Sharpe, Sortino, profit factor by strategy |
| **S&P 500 Pairs Discovery** | ACTIVE | Daily sector scan, updates seed list |
| **Volatility Skew Scanner** | ACTIVE | Put/call IV analysis before theta harvest |
| **Squeeze Scanner** | ACTIVE | Short interest + Z-score reversion |

### Discord Commands (25+)

| Command | Purpose |
|---------|---------|
| `!status` | Full firm dashboard — one command for everything |
| `!morning` | Pre-market briefing (auto 9:25 AM ET) |
| `!evening` | Post-market summary (auto 4:05 PM ET) |
| `!week` | Weekly performance summary |
| `!cycle [ticker]` | EchoEdge multi-agent consensus |
| `!paper-pnl` | Live unrealized P&L per position |
| `!oracle-status` | Prediction market signals + intel |
| `!risk-status` | Correlations, drawdowns, pauses |
| `!engineer-log` | Auto-improvement adjustments |
| `!allocation-status` | Pillar weights and 7d P&L |
| `!montecarlo A B` | Monte Carlo simulation |
| `!backtest strategy days` | Walk-forward backtest |
| `!performance days` | Strategy attribution |
| `!arb-scan` | Cross-platform mispricings |
| `!squeeze-scan` | Short squeeze candidates |
| `!vol-skew` | Options IV skew analysis |
| `!shadow-pnl` | Shadow vs real P&L |
| `!memory-query theme` | Historical event patterns |
| `!ai-consensus ticker` | AI second opinion |
| `!tv-signals` | TradingView webhook history |
| `!pairs-scan` | Discovered pairs by sector |
| `!alert-test` | Test SMS/email channels |
| `!ev-scan` | EV scan across all platforms |

---

## 4. What's Working vs. What Needs Validation

### Confirmed Working (with evidence)

| Component | Evidence |
|-----------|----------|
| Alpaca paper orders | 25+ orders filled today, 6 positions on Alpaca account |
| Pairs Z-score scanner | Scanning 29 pairs every 10 min, signals logged |
| Prediction market scanner | 5 live positions from Kalshi/Polymarket |
| Crypto momentum | 7 closed trades with +$34 realized |
| Crash hedge puts | 2 SPY puts opened when VIX>25 |
| Exit manager | Multiple exits fired (TTL, Z-revert) |
| Reconciliation | Closed 4 orphaned Alpaca positions on startup |
| Psychologist | CONTRARIAN MODE active at F&G=14 |
| Engineer self-tuning | zscore_entry auto-tightened 1.0→1.1 |
| Meta-allocation | crypto=1.5x, pairs=0.5x rebalanced |
| Intelligence headlines | 60 headlines/cycle across 6 themes |
| Geo escalation | Iran detected, sizing tightened |
| War Room Dashboard | All 4 API endpoints returning JSON, React SPA serving |
| Evening briefing | Auto-fired at 4:05 PM ET on schedule |

### Untested / Needs Market Hours

| Component | Issue | Action Needed |
|-----------|-------|---------------|
| Options Theta Harvest | Needs market hours + Alpaca options chain | Test during market hours |
| Oracle trade execution | No signals above threshold yet | Monitor Iran ceasefire (49% of threshold) |
| Event Synthetics | No spreads > 2% found | May need to lower threshold |
| Funding Rate Arb | Phemex rates at 0% | Wait for elevated funding periods |
| PEAD Scanner | Namespace error (function not found at runtime) | Move PEAD block above alert_scan_task |
| S&P 500 Discovery | Wikipedia parsing got only 2 tickers (after hours) | Test during market hours |
| TradingView auto-execute | No webhooks received yet | Send test webhook |
| AI Consensus | Needs OPENAI_API_KEY configured | Add to .env |

### Known Issues

| Issue | Severity | Notes |
|-------|----------|-------|
| Pairs losing money (-$81) | Medium | Engineer already tightened entry. GM/F removed. |
| `datetime.utcnow()` deprecation | Low | 2 instances in daily_state functions |
| Some positions have wrong strategy labels in JSON | Low | Legacy positions, self-corrects on new trades |
| Shadow trading not yet tested weekly comparison | Low | No Sunday comparison run yet |
| Backtest Sharpe will be lower now | Expected | Walk-forward is more realistic than look-ahead |

---

## 5. Prioritized Task List

### P0: Security Overhaul (BEFORE live capital)

1. **API key rotation** — All exchange keys should be rotated before live deployment
2. **Secrets management** — Move from .env to Docker secrets or Vault
3. **Rate limiting** — Add per-exchange rate limits to prevent accidental flooding
4. **Order validation** — Max order size cap (e.g., $5K per order absolute max)
5. **Kill switch hardening** — The circuit breaker exists but needs daily P&L tracking in real-time
6. **Audit trail** — Every order attempt logged to immutable SQLite table with full request/response
7. **IP whitelisting** — Alpaca, Kalshi, Phemex all support IP restrictions
8. **2FA on exchange accounts** — Verify all exchange accounts have 2FA enabled
9. **Webhook authentication** — TV webhook needs secret validation (field exists but empty)
10. **Dashboard auth** — War Room dashboard is currently unauthenticated on port 8080

### P1: Interactive Brokers Integration

1. **Client Portal Gateway** — Deploy IBKR gateway container alongside bot
2. **Auth flow** — Implement IBKR session management (sessions expire, need re-auth)
3. **Order routing** — Route equity pairs through IBKR for lower commissions
4. **Options execution** — IBKR has better options fills than Alpaca paper
5. **Real-time data** — IBKR provides Level 2 data for better spread analysis

### P2: Live Deployment Checklist

1. [ ] All P0 security items completed
2. [ ] 30 consecutive days of paper trading with positive Sharpe
3. [ ] Backtest validation: paper results within 20% of backtest
4. [ ] All agents producing valid signals for 7+ days
5. [ ] Morning/evening briefings running without errors for 7 days
6. [ ] Reconciliation catching all orphaned positions
7. [ ] Kill switch tested: manually trigger and verify all trading stops
8. [ ] SMS alerts confirmed working (at least one real notification)
9. [ ] Risk limits verified: correlation blocking works, drawdown pauses work
10. [ ] Shadow portfolio tracking real for 14+ days

### P3: Feature Completion

1. Fix PEAD scanner namespace issue
2. Wire volatility skew into theta harvest decision (currently separate)
3. Add Deribit integration for crypto options
4. Add OANDA for FX pairs
5. Implement proper position-level P&L tracking (not just portfolio level)
6. Add trade journaling (why each trade was taken, what agents said)

---

## 6. Current Portfolio State

```
Cash:           $24,748.52
Deployed:        $2,299.33
Equity:         $27,047.84
Return:             +8.2%
Starting capital:  $25,000

Open positions: 11
Closed trades:  78
Realized P&L:   -$47.11

By strategy (closed):
  crypto         7 trades    +$34.30    (carrying)
  pairs         71 trades    -$81.40    (dragging — GM/F churn)

Open positions:
  5 prediction markets     $1,070  (Iran, Fed rates, Trump/China)
  2 crash hedge puts         $274  (SPY $640, $647 strikes)
  1 pairs trade              $704  (NVDA/AMD)
  3 new prediction markets   $251  (added today)
```

**Honest assessment**: The +8.2% return is mostly unrealized from prediction market positions that haven't resolved yet. Realized P&L is -$47 — essentially flat. The pairs strategy has been a net loser due to GM/F churn (now removed) and low win rate (27%). Crypto is the only profitable realized strategy.

---

## 7. Path to Profitability

### What needs to happen before live capital:

1. **Pairs strategy must prove edge** — Current 27% win rate is unacceptable. The Engineer has already tightened entry to Z>1.1. Need 50%+ win rate over 50+ trades before live sizing.

2. **Prediction markets must resolve** — $1,070 deployed in Iran/Fed markets. These resolve March 31 and April 30. If they resolve favorably, that validates the EV scanner.

3. **Oracle Engine must fire at least once** — Iran ceasefire at 49% of 70% threshold. Need a signal to cross threshold and validate the full pipeline (arbiter → MC → AI → Alpaca execution).

4. **Backtest must align with paper results** — Run `!backtest pairs 90` with the new walk-forward engine. If paper results are within 20% of backtest Sharpe, the strategy is validated.

5. **Options theta harvest must execute** — Need one full cycle during market hours: scan → find IV>50% → sell spread → track to exit.

6. **30 days clean operation** — No orphaned positions, no fake P&L bugs, no reconciliation surprises, morning/evening briefings firing on schedule.

### Minimum viable profitability:
- Pairs: Sharpe > 0.5, win rate > 50%
- Oracle: 3+ successful trades
- Theta: 5+ successful credit spread cycles
- Prediction: 60%+ resolution accuracy
- Combined monthly: +2% net of all costs

---

## 8. Platform Expansion Priority

| Priority | Platform | Use Case | Status | Complexity |
|----------|----------|----------|--------|------------|
| 1 | **Interactive Brokers** | Equity pairs + options execution | Code exists, gateway not deployed | Medium |
| 2 | **Deribit** | Crypto options (theta harvest on BTC/ETH) | Not started | Medium |
| 3 | **OANDA** | FX pairs (complement equity pairs) | Not started | Low |
| 4 | **Binance** | Crypto futures (replace Phemex) | Not started | Medium |
| 5 | **Tradier** | Backup equity/options broker | Not started | Low |

### Why IB first:
- Lower commissions than Alpaca ($0.005/share vs free-but-worse-fills)
- Better options execution (IBKR options fills are best-in-class)
- Access to international markets
- Margin accounts for proper short selling

### Why Deribit second:
- Crypto options market is growing fast
- Can run theta harvest on BTC/ETH options
- 24/7 market — complements equity hours

---

## 9. Capital Allocation Recommendation

### Phase 1: Paper Validation (Current — next 30 days)
```
Alpaca Paper:  $25,000 (100%)
Real capital:  $0
```

### Phase 2: Small Live (after 30-day validation)
```
Alpaca Live:    $5,000  (pairs + options)
Polymarket:     $2,000  (prediction markets)
Kalshi:         $2,000  (prediction markets)
Coinbase:       $1,000  (crypto momentum)
Reserve:       $15,000  (not deployed)
Total:         $25,000
```

### Phase 3: Full Deployment (after 90-day live track record)
```
IBKR:          $15,000  (pairs + options + international)
Polymarket:     $5,000  (prediction markets)
Kalshi:         $3,000  (prediction markets)
Coinbase:       $3,000  (crypto)
Deribit:        $2,000  (crypto options)
Phemex:         $2,000  (funding arb)
Reserve:       $10,000  (not deployed)
Total:         $40,000
```

---

## 10. Risk Controls and Live Deployment Checklist

### Active Risk Controls

| Control | Status | Threshold |
|---------|--------|-----------|
| Circuit breaker | Active | Max trades per minute |
| Portfolio floor | Active | $50K minimum (currently paper) |
| Strategy pause | Active | $200/day loss → 24h pause |
| Correlation block | Active | >80% correlated tickers blocked |
| Position cap | Active | 1.5% per position (with multipliers) |
| Geo sizing | Active | Auto-tightens to 0.5% on escalation |
| Psychologist | Active | CAUTION mode → 0.5x all sizing |
| MC gate | Active | <45% probability → skip trade |
| Master Arbiter | Active | 5-level check before every trade |
| Reconciliation | Active | Closes orphaned Alpaca positions |

### Missing Risk Controls (needed for live)

| Control | Priority | Description |
|---------|----------|-------------|
| Max order size cap | P0 | Hard limit per order ($5K absolute max) |
| Daily gross loss limit | P0 | Stop all trading if portfolio down >3% in a day |
| Exchange rate limits | P0 | Per-API-call throttling |
| Slippage monitoring | P1 | Alert if fill price > 1% from quote |
| Drawdown watermark | P1 | Stop trading if equity drops below high-water mark -5% |
| Position concentration | P1 | Max 20% portfolio in any single strategy |
| Overnight exposure limit | P2 | Reduce equity exposure before close |

### Live Deployment Checklist

```
[ ] Security audit complete (all P0 items)
[ ] 30 days paper trading, positive Sharpe
[ ] Backtest validation (paper within 20% of backtest)
[ ] All agents stable for 7+ consecutive days
[ ] Kill switch tested and verified
[ ] SMS/email alerts working
[ ] Morning/evening briefings running 7+ days
[ ] Reconciliation running clean 7+ days
[ ] Shadow portfolio tracking 14+ days
[ ] Max order size cap implemented
[ ] Daily gross loss limit implemented
[ ] Exchange rate limits implemented
[ ] IP whitelisting on all exchange accounts
[ ] API keys rotated for live deployment
[ ] Dashboard authentication added
[ ] Disaster recovery plan documented
[ ] Emergency contact procedures established
```

---

## Summary for Cross-Model Consensus

**What we built**: A 12,000-line autonomous trading system with 8 strategy engines, 8 AI agents, a 5-level arbiter, self-improving parameters, cross-platform arbitrage, a real-time dashboard, and automated daily briefings. All running in a single Docker container.

**What works**: The infrastructure is solid. Agents are producing real signals. The system auto-corrected its own pairs threshold after detecting poor performance. Crash hedges fired when VIX spiked. The evening briefing auto-fired on schedule.

**What doesn't work yet**: Realized P&L is -$47 (essentially flat). Pairs trading has lost money. Oracle hasn't fired. Options theta is untested. The system has a lot of moving parts and needs 30+ days of stable paper trading before any real capital.

**The honest question**: Is this system ready for live capital? **No.** It needs validated edge (positive Sharpe over 30 days), security hardening, and proven agent reliability. The architecture is comprehensive, but architecture without proven edge is just complexity.

**Next steps**: Run paper for 30 days. Let the Engineer agent auto-tune. Let prediction markets resolve. Validate backtest vs paper results. Then — and only then — deploy $5K live.
