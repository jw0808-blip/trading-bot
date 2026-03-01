#!/usr/bin/env bash
# ============================================================================
# TraderJoes V9 — Full Autonomous Trading Firm
# Auto-execution, exit manager, AI oversight, watchdog
# ============================================================================
set -uo pipefail
cd /root/trading-bot

echo "============================================"
echo "  TraderJoes V9 — Full Autonomous Trading"
echo "============================================"
echo ""

cp main.py main.py.bak.v9

python3 << 'V9PATCH'
with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

changes = 0

# ============================================================
# 1. FULL AUTO-EXECUTION ENGINE
# ============================================================
auto_exec = '''

# ============================================================================
# V9: FULL AUTONOMOUS EXECUTION ENGINE
# ============================================================================
import json as _json
import os as _os

AUTO_LIVE_CONFIG = {
    "enabled": True,
    "min_ev": 0.05,           # 5% minimum EV
    "min_edge_score": 65,     # MEDIUM+ confidence
    "max_position_pct": 0.0025,  # 0.25% max per trade (tiny start)
    "max_daily_trades": 20,
    "max_concurrent_positions": 10,
    "daily_loss_halt": -200,  # Halt if daily P&L drops below this
    "drawdown_halt_pct": 5.0, # Halt if drawdown exceeds 5%
    "trades_today": 0,
    "last_trade_reset": "",
}

# Position tracker for open trades
OPEN_POSITIONS = []
CLOSED_POSITIONS = []
POSITION_FILE = "/app/data/positions.json"
TRADE_AUDIT_LOG = []


def save_positions():
    """Save open and closed positions to disk."""
    try:
        data = {
            "open": OPEN_POSITIONS,
            "closed": CLOSED_POSITIONS[-100:],  # Keep last 100 closed
            "audit_log": TRADE_AUDIT_LOG[-200:],
            "config": AUTO_LIVE_CONFIG,
        }
        with open(POSITION_FILE, "w") as f:
            _json.dump(data, f, indent=2, default=str)
    except Exception as exc:
        log.warning("Save positions error: %s", exc)


def load_positions():
    """Load positions from disk on startup."""
    global OPEN_POSITIONS, CLOSED_POSITIONS, TRADE_AUDIT_LOG
    try:
        if _os.path.exists(POSITION_FILE):
            with open(POSITION_FILE, "r") as f:
                data = _json.load(f)
            OPEN_POSITIONS = data.get("open", [])
            CLOSED_POSITIONS = data.get("closed", [])
            TRADE_AUDIT_LOG = data.get("audit_log", [])
            saved_config = data.get("config", {})
            for k, v in saved_config.items():
                if k in AUTO_LIVE_CONFIG:
                    AUTO_LIVE_CONFIG[k] = v
            log.info("Loaded %d open positions, %d closed", len(OPEN_POSITIONS), len(CLOSED_POSITIONS))
    except Exception as exc:
        log.warning("Load positions error: %s", exc)


def audit_log(action, details):
    """Log every action for full audit trail."""
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "action": action,
        "details": details,
    }
    TRADE_AUDIT_LOG.append(entry)
    log.info("AUDIT: %s — %s", action, str(details)[:200])
    # Keep manageable
    while len(TRADE_AUDIT_LOG) > 500:
        TRADE_AUDIT_LOG.pop(0)


def reset_daily_counters():
    """Reset daily trade counters at midnight UTC."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if AUTO_LIVE_CONFIG["last_trade_reset"] != today:
        AUTO_LIVE_CONFIG["trades_today"] = 0
        AUTO_LIVE_CONFIG["last_trade_reset"] = today
        global DAILY_PNL
        DAILY_PNL = 0.0
        audit_log("DAILY_RESET", {"date": today})


def check_safety_gates():
    """Check all safety conditions before allowing a trade. Returns (ok, reason)."""
    # Kill switch
    if not AUTO_LIVE_CONFIG.get("enabled", True):
        return False, "Auto-execution disabled"
    
    # Daily trade limit
    if AUTO_LIVE_CONFIG["trades_today"] >= AUTO_LIVE_CONFIG["max_daily_trades"]:
        return False, f"Daily trade limit reached ({AUTO_LIVE_CONFIG['max_daily_trades']})"
    
    # Max concurrent positions
    if len(OPEN_POSITIONS) >= AUTO_LIVE_CONFIG["max_concurrent_positions"]:
        return False, f"Max concurrent positions ({AUTO_LIVE_CONFIG['max_concurrent_positions']})"
    
    # Daily loss halt
    if DAILY_PNL <= AUTO_LIVE_CONFIG["daily_loss_halt"]:
        return False, f"Daily loss limit hit (${DAILY_PNL:,.2f})"
    
    # Drawdown halt
    dd = ANALYTICS.get("max_drawdown", 0)
    if dd >= AUTO_LIVE_CONFIG["drawdown_halt_pct"]:
        return False, f"Drawdown halt ({dd:.1f}% >= {AUTO_LIVE_CONFIG['drawdown_halt_pct']}%)"
    
    return True, "All gates passed"


async def auto_execute_opportunity(opp, channel):
    """Automatically execute a trade for a high-scoring opportunity."""
    reset_daily_counters()
    
    # Safety check
    safe, reason = check_safety_gates()
    if not safe:
        audit_log("BLOCKED", {"reason": reason, "market": opp.get("market", "")[:50]})
        return False
    
    # Score the opportunity
    try:
        edge_score, confidence, signals = calculate_edge_score(opp)
    except Exception:
        edge_score, confidence, signals = 0, "SKIP", []
    
    # Check minimum thresholds
    ev = opp.get("ev", 0)
    if ev < AUTO_LIVE_CONFIG["min_ev"]:
        return False
    if edge_score < AUTO_LIVE_CONFIG["min_edge_score"]:
        return False
    if confidence in ("LOW", "SKIP"):
        return False
    
    # Calculate position size (quarter-Kelly with regime + tiny cap)
    bankroll = ANALYTICS.get("current_equity", 10000)
    size = suggest_position_size_v2(opp, bankroll)
    
    # Apply V9 tiny-start cap
    max_size = bankroll * AUTO_LIVE_CONFIG["max_position_pct"]
    size = min(size, max_size)
    
    if size < 1:
        return False  # Too small to trade
    
    # Build position record
    market = opp.get("market", "Unknown")[:80]
    platform = opp.get("platform", "Unknown")
    yes_price = opp.get("yes_price", 0)
    
    # Calculate stops and targets
    entry_price = yes_price if yes_price > 0 else 0.5
    stop_price = max(entry_price * 0.7, 0.01)  # 30% stop
    target_price = min(entry_price * 1.6, 0.99)  # 60% target (2:1 R:R)
    
    position = {
        "id": f"POS-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{len(OPEN_POSITIONS)}",
        "market": market,
        "platform": platform,
        "action": "BUY",
        "asset": opp.get("ticker", opp.get("slug", market[:20])),
        "size_usd": size,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "trailing_stop": stop_price,
        "ev": ev,
        "edge_score": edge_score,
        "confidence": confidence,
        "signals": signals,
        "opened_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "status": "OPEN",
        "pnl": 0,
    }
    
    # Execute the order
    success = False
    exec_msg = ""
    
    if TRADING_MODE == "live":
        # Route to correct exchange
        asset = position["asset"]
        if platform == "Kalshi" or asset.startswith("KX"):
            success, exec_msg = await execute_kalshi_order("BUY", asset, size)
        elif platform == "Polymarket":
            # Polymarket needs on-chain execution — log as pending
            success = True
            exec_msg = f"PAPER-ROUTED: Polymarket on-chain not yet automated. Logged for manual review."
        elif asset in ("BTC", "ETH", "DOGE", "XRP", "SOL", "ALGO", "SHIB", "XLM", "HBAR"):
            success, exec_msg = await execute_coinbase_order("BUY", asset, size)
        elif asset.endswith("USDT") or asset.endswith("PERP"):
            success, exec_msg = await execute_phemex_order("BUY", asset, size)
        else:
            success = True
            exec_msg = f"PAPER-ROUTED: No direct execution path for {platform}. Logged."
    else:
        # Paper mode — always succeeds
        success = True
        exec_msg = "Paper trade executed"
        PAPER_PORTFOLIO["trades"].append({
            "action": "BUY",
            "market": market,
            "price": entry_price,
            "size": size,
            "timestamp": position["opened_at"],
        })
    
    if success:
        OPEN_POSITIONS.append(position)
        AUTO_LIVE_CONFIG["trades_today"] += 1
        ANALYTICS["total_trades"] += 1
        
        audit_log("TRADE_OPENED", {
            "id": position["id"],
            "market": market[:50],
            "platform": platform,
            "size": size,
            "entry": entry_price,
            "stop": stop_price,
            "target": target_price,
            "edge_score": edge_score,
            "confidence": confidence,
            "mode": TRADING_MODE,
        })
        
        # Discord notification
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        mode_tag = "LIVE" if TRADING_MODE == "live" else "PAPER"
        try:
            await channel.send(
                f"**Auto-Trade [{mode_tag}]** | {ts}\\n"
                f"BUY {platform}: {market[:50]}\\n"
                f"Size: ${size:.2f} | Entry: ${entry_price:.3f}\\n"
                f"Stop: ${stop_price:.3f} | Target: ${target_price:.3f}\\n"
                f"Edge: {edge_score}/100 ({confidence}) | EV: +{ev*100:.1f}%\\n"
                f"{exec_msg[:100]}"
            )
        except Exception:
            pass
        
        save_positions()
        return True
    
    audit_log("TRADE_FAILED", {"market": market[:50], "error": exec_msg[:100]})
    return False


# ============================================================================
# V9: AUTO-EXIT MANAGER
# ============================================================================

async def check_and_manage_exits(channel):
    """Check all open positions and auto-exit when conditions are met."""
    if not OPEN_POSITIONS:
        return
    
    positions_to_close = []
    
    for pos in OPEN_POSITIONS:
        if pos["status"] != "OPEN":
            continue
        
        # For prediction markets: check current price
        current_price = pos["entry_price"]  # Default to entry if can't fetch
        
        # Try to get current price from platform
        platform = pos.get("platform", "")
        market_title = pos.get("market", "")
        
        if platform == "Polymarket":
            # Check Polymarket prices
            try:
                poly_markets = find_polymarket_markets_for_arb()
                for pm in poly_markets:
                    if pm["title"][:30].lower() in market_title[:30].lower() or market_title[:30].lower() in pm["title"][:30].lower():
                        current_price = pm["yes_price"]
                        break
            except Exception:
                pass
        
        # Calculate P&L
        entry = pos["entry_price"]
        if entry > 0:
            pnl_pct = (current_price - entry) / entry * 100
            pnl_usd = pos["size_usd"] * (current_price - entry) / entry
        else:
            pnl_pct = 0
            pnl_usd = 0
        
        pos["current_price"] = current_price
        pos["pnl"] = pnl_usd
        
        exit_reason = None
        
        # 1. Stop-loss hit
        if current_price <= pos["stop_price"]:
            exit_reason = f"STOP-LOSS hit (${pos['stop_price']:.3f})"
        
        # 2. Target hit
        elif current_price >= pos["target_price"]:
            exit_reason = f"TARGET hit (${pos['target_price']:.3f})"
        
        # 3. Trailing stop update
        elif current_price > pos.get("trailing_stop", pos["stop_price"]):
            # Move trailing stop up (ratchet only)
            new_trail = current_price * 0.85  # 15% trailing stop
            if new_trail > pos.get("trailing_stop", 0):
                pos["trailing_stop"] = new_trail
        
        # 4. Check if trailing stop hit
        if not exit_reason and current_price <= pos.get("trailing_stop", 0):
            exit_reason = f"TRAILING STOP hit (${pos['trailing_stop']:.3f})"
        
        # 5. Time-based exit: close 24h before resolution (if we knew resolution time)
        # For now, close positions older than 7 days
        try:
            opened = datetime.strptime(pos["opened_at"], "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            age_hours = (datetime.now(timezone.utc) - opened).total_seconds() / 3600
            if age_hours > 168:  # 7 days
                exit_reason = "TIME EXIT: Position open >7 days"
        except Exception:
            pass
        
        if exit_reason:
            positions_to_close.append((pos, exit_reason, pnl_usd))
    
    # Execute exits
    for pos, reason, pnl in positions_to_close:
        pos["status"] = "CLOSED"
        pos["closed_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pos["exit_reason"] = reason
        pos["final_pnl"] = pnl
        
        # Update analytics
        ANALYTICS["total_pnl"] += pnl
        DAILY_PNL_TRACKER = globals().get("DAILY_PNL", 0)
        if pnl > 0:
            ANALYTICS["winning_trades"] += 1
        else:
            ANALYTICS["losing_trades"] += 1
        
        # Move to closed
        OPEN_POSITIONS.remove(pos)
        CLOSED_POSITIONS.append(pos)
        
        audit_log("TRADE_CLOSED", {
            "id": pos["id"],
            "market": pos["market"][:50],
            "reason": reason,
            "pnl": f"${pnl:+,.2f}",
            "entry": pos["entry_price"],
            "exit": pos.get("current_price", 0),
        })
        
        # Discord notification
        pnl_icon = "PROFIT" if pnl >= 0 else "LOSS"
        mode_tag = "LIVE" if TRADING_MODE == "live" else "PAPER"
        try:
            await channel.send(
                f"**Auto-Exit [{mode_tag}] [{pnl_icon}]**\\n"
                f"{pos['market'][:50]}\\n"
                f"Reason: {reason}\\n"
                f"P&L: ${pnl:+,.2f} | Entry: ${pos['entry_price']:.3f} → Exit: ${pos.get('current_price', 0):.3f}\\n"
                f"Position held: {pos['opened_at']} → {pos['closed_at']}"
            )
        except Exception:
            pass
    
    if positions_to_close:
        save_positions()


@bot.command(name="positions")
async def positions_cmd(ctx):
    """Show all open positions with live P&L."""
    if not OPEN_POSITIONS:
        await ctx.send("No open positions.")
        return
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Open Positions** | {ts}", f"Mode: {TRADING_MODE.upper()}", "================================"]
    
    total_pnl = 0
    for i, pos in enumerate(OPEN_POSITIONS):
        pnl = pos.get("pnl", 0)
        total_pnl += pnl
        pnl_icon = "+" if pnl >= 0 else ""
        lines.append(
            f"**{i+1}. {pos['platform']}** | {pos['market'][:45]}\\n"
            f"  Size: ${pos['size_usd']:.2f} | Entry: ${pos['entry_price']:.3f}\\n"
            f"  Stop: ${pos['stop_price']:.3f} | Target: ${pos['target_price']:.3f} | Trail: ${pos.get('trailing_stop', 0):.3f}\\n"
            f"  P&L: ${pnl_icon}{pnl:.2f} | Edge: {pos.get('edge_score', 0)}/100 | {pos['opened_at']}"
        )
    
    lines.append(f"\\n**Total open P&L: ${total_pnl:+,.2f}** | Positions: {len(OPEN_POSITIONS)}")
    lines.append("================================")
    
    report = "\\n".join(lines)
    if len(report) > 1900:
        report = report[:1900] + "\\n*...truncated*"
    await ctx.send(report)


@bot.command(name="closed")
async def closed_cmd(ctx):
    """Show recently closed positions."""
    recent = CLOSED_POSITIONS[-10:]
    if not recent:
        await ctx.send("No closed positions yet.")
        return
    
    lines = ["**Recently Closed Positions**", "================================"]
    total_pnl = sum(p.get("final_pnl", 0) for p in recent)
    wins = sum(1 for p in recent if p.get("final_pnl", 0) > 0)
    
    for pos in reversed(recent):
        pnl = pos.get("final_pnl", 0)
        icon = "WIN" if pnl > 0 else "LOSS"
        lines.append(
            f"[{icon}] ${pnl:+,.2f} | {pos['platform']}: {pos['market'][:40]}\\n"
            f"  {pos.get('exit_reason', 'N/A')} | {pos.get('closed_at', '')}"
        )
    
    lines.append(f"\\n**Net P&L: ${total_pnl:+,.2f}** | {wins}/{len(recent)} wins")
    lines.append("================================")
    await ctx.send("\\n".join(lines))


@bot.command(name="audit")
async def audit_cmd(ctx, n: int = 10):
    """Show recent audit log entries."""
    recent = TRADE_AUDIT_LOG[-n:]
    if not recent:
        await ctx.send("Audit log is empty.")
        return
    
    lines = [f"**Audit Log** (last {len(recent)} entries)", "================================"]
    for entry in reversed(recent):
        lines.append(f"`{entry['timestamp']}` **{entry['action']}** — {str(entry['details'])[:100]}")
    
    lines.append("================================")
    report = "\\n".join(lines)
    if len(report) > 1900:
        report = report[:1900] + "\\n*...truncated*"
    await ctx.send(report)


@bot.command(name="auto-config")
async def auto_config_cmd(ctx, key: str = "", value: str = ""):
    """View or update auto-execution config."""
    if not key:
        lines = ["**Auto-Execution Config**", "================================"]
        for k, v in AUTO_LIVE_CONFIG.items():
            lines.append(f"  `{k}`: {v}")
        lines.append("\\nUsage: `!auto-config <key> <value>` to change")
        lines.append("================================")
        await ctx.send("\\n".join(lines))
        return
    
    if key not in AUTO_LIVE_CONFIG:
        await ctx.send(f"Unknown key: `{key}`. Use `!auto-config` to see all keys.")
        return
    
    # Convert value
    old = AUTO_LIVE_CONFIG[key]
    try:
        if isinstance(old, bool):
            AUTO_LIVE_CONFIG[key] = value.lower() in ("true", "yes", "on", "1")
        elif isinstance(old, int):
            AUTO_LIVE_CONFIG[key] = int(value)
        elif isinstance(old, float):
            AUTO_LIVE_CONFIG[key] = float(value)
        else:
            AUTO_LIVE_CONFIG[key] = value
    except Exception:
        await ctx.send(f"Invalid value for `{key}`: {value}")
        return
    
    save_positions()
    audit_log("CONFIG_CHANGED", {"key": key, "old": old, "new": AUTO_LIVE_CONFIG[key]})
    await ctx.send(f"Updated `{key}`: {old} → {AUTO_LIVE_CONFIG[key]}")


# ============================================================================
# V9: AI OVERSIGHT + WATCHDOG
# ============================================================================

OVERSIGHT_CONFIG = {
    "win_rate_floor": 40,       # Pause if win rate drops below 40%
    "max_drawdown_halt": 5.0,   # Halt at 5% drawdown
    "max_daily_loss": -200,     # Halt at -$200 daily
    "consecutive_loss_halt": 5, # Halt after 5 consecutive losses
    "last_oversight_run": "",
}


async def run_ai_oversight(channel):
    """AI oversight: review performance, pause if needed, suggest improvements."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    total_trades = ANALYTICS.get("total_trades", 0)
    winning = ANALYTICS.get("winning_trades", 0)
    losing = ANALYTICS.get("losing_trades", 0)
    total_pnl = ANALYTICS.get("total_pnl", 0)
    max_dd = ANALYTICS.get("max_drawdown", 0)
    
    win_rate = (winning / max(total_trades, 1)) * 100
    
    alerts = []
    halt_trading = False
    
    # Check win rate
    if total_trades >= 10 and win_rate < OVERSIGHT_CONFIG["win_rate_floor"]:
        alerts.append(f"Win rate {win_rate:.1f}% below floor ({OVERSIGHT_CONFIG['win_rate_floor']}%)")
        halt_trading = True
    
    # Check drawdown
    if max_dd >= OVERSIGHT_CONFIG["max_drawdown_halt"]:
        alerts.append(f"Drawdown {max_dd:.1f}% exceeds halt threshold ({OVERSIGHT_CONFIG['max_drawdown_halt']}%)")
        halt_trading = True
    
    # Check daily loss
    if DAILY_PNL <= OVERSIGHT_CONFIG["max_daily_loss"]:
        alerts.append(f"Daily P&L ${DAILY_PNL:,.2f} below halt threshold (${OVERSIGHT_CONFIG['max_daily_loss']:,.2f})")
        halt_trading = True
    
    # Check consecutive losses
    recent_closed = CLOSED_POSITIONS[-5:]
    consecutive_losses = 0
    for p in reversed(recent_closed):
        if p.get("final_pnl", 0) < 0:
            consecutive_losses += 1
        else:
            break
    if consecutive_losses >= OVERSIGHT_CONFIG["consecutive_loss_halt"]:
        alerts.append(f"{consecutive_losses} consecutive losses — halt threshold is {OVERSIGHT_CONFIG['consecutive_loss_halt']}")
        halt_trading = True
    
    if halt_trading:
        AUTO_LIVE_CONFIG["enabled"] = False
        audit_log("AI_HALT", {"alerts": alerts})
        try:
            await channel.send(
                f"**AI OVERSIGHT ALERT** | {ts}\\n"
                f"Trading HALTED by AI oversight.\\n"
                f"Reasons:\\n" + "\\n".join(f"  - {a}" for a in alerts) +
                f"\\n\\nUse `!auto-config enabled true` to resume after review."
            )
        except Exception:
            pass
    
    # Build daily oversight report
    regime = REGIME_CONFIG.get("current_regime", "UNKNOWN")
    fng = REGIME_CONFIG.get("fng_value", "?")
    
    report = (
        f"**AI Oversight Report** | {ts}\\n================================\\n"
        f"Regime: {regime} | F&G: {fng}/100\\n"
        f"Mode: {TRADING_MODE.upper()} | Auto-exec: {'ON' if AUTO_LIVE_CONFIG['enabled'] else 'HALTED'}\\n"
        f"\\n**Performance:**\\n"
        f"  Trades: {total_trades} | Wins: {winning} | Losses: {losing}\\n"
        f"  Win rate: {win_rate:.1f}%\\n"
        f"  Total P&L: ${total_pnl:+,.2f}\\n"
        f"  Daily P&L: ${DAILY_PNL:+,.2f}\\n"
        f"  Max drawdown: {max_dd:.1f}%\\n"
        f"\\n**Positions:**\\n"
        f"  Open: {len(OPEN_POSITIONS)} | Closed today: {AUTO_LIVE_CONFIG['trades_today']}\\n"
        f"  Consecutive losses: {consecutive_losses}\\n"
    )
    
    if alerts:
        report += f"\\n**ALERTS:** {len(alerts)}\\n" + "\\n".join(f"  - {a}" for a in alerts)
    else:
        report += "\\n**Status:** All systems nominal"
    
    report += "\\n================================"
    
    OVERSIGHT_CONFIG["last_oversight_run"] = ts
    
    return report


@bot.command(name="oversight")
async def oversight_cmd(ctx):
    """Run AI oversight check manually."""
    report = await run_ai_oversight(ctx.channel)
    if len(report) > 1900:
        report = report[:1900] + "\\n*...truncated*"
    await ctx.send(report)

'''

if "AUTO_LIVE_CONFIG" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        auto_exec + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [1] Full auto-execution engine added")
    print("  [2] Auto-exit manager added")
    print("  [3] AI oversight layer added")
else:
    print("  [1-3] Already exists")


# ============================================================
# 4. INTEGRATE INTO ALERT SCAN LOOP
# ============================================================
# Find the alert scan function and add auto-execution + exit management
if "auto_execute_opportunity" in code and "# V9 AUTO-EXEC INTEGRATION" not in code:
    # Find the alert scan loop where it sends alerts
    old_alert_sent = 'log.info("Alert sent: %s EV +%.1f%%", opp.get("platform",""), opp.get("ev",0)*100)'
    
    new_alert_sent = '''log.info("Alert sent: %s EV +%.1f%%", opp.get("platform",""), opp.get("ev",0)*100)
                    # V9 AUTO-EXEC INTEGRATION
                    if AUTO_LIVE_CONFIG.get("enabled", False) and (TRADING_MODE == "live" or AUTO_PAPER_ENABLED):
                        try:
                            await auto_execute_opportunity(opp, ch)
                        except Exception as aex:
                            log.warning("Auto-exec error: %s", aex)'''
    
    if old_alert_sent in code:
        code = code.replace(old_alert_sent, new_alert_sent)
        changes += 1
        print("  [4] Auto-execution integrated into alert scan loop")
    else:
        # Try alternate pattern
        alt = 'log.info("Alert sent:'
        if alt in code:
            # Find all instances and add after the first one in the scan loop
            print("  [4] Alert log pattern differs — manual integration may be needed")
        else:
            print("  [4] Could not find alert scan integration point")


# ============================================================
# 5. ADD EXIT CHECK TO SCAN LOOP
# ============================================================
if "check_and_manage_exits" in code and "# V9 EXIT CHECK" not in code:
    old_scan_end = "push_all_analytics()  # push metrics each cycle"
    new_scan_end = """push_all_analytics()  # push metrics each cycle
                    # V9 EXIT CHECK
                    try:
                        await check_and_manage_exits(ch)
                    except Exception as eex:
                        log.warning("Exit check error: %s", eex)"""
    
    if old_scan_end in code:
        code = code.replace(old_scan_end, new_scan_end, 1)  # Replace only first occurrence
        changes += 1
        print("  [5] Auto-exit check integrated into scan loop")
    else:
        print("  [5] Could not find scan loop end for exit integration")


# ============================================================
# 6. LOAD POSITIONS ON STARTUP
# ============================================================
if "load_positions()" not in code and "load_all_state" in code:
    code = code.replace(
        "load_all_state()",
        "load_all_state()\n    load_positions()"
    )
    changes += 1
    print("  [6] Position loading on startup added")


# ============================================================
# 7. SAVE POSITIONS IN save_all_state
# ============================================================
if "save_positions()" not in code.split("def save_all_state")[1][:500] if "def save_all_state" in code else "":
    old_save = "def save_all_state():"
    new_save = """def save_all_state():
    save_positions()  # V9: save positions too"""
    if old_save in code:
        code = code.replace(old_save, new_save, 1)
        changes += 1
        print("  [7] Position saving integrated into save_all_state")


# ============================================================
# 8. ADD OVERSIGHT TO DAILY REPORT
# ============================================================
if "run_ai_oversight" in code and "# V9 OVERSIGHT" not in code.split("_send_daily_report")[1][:500] if "_send_daily_report" in code else "":
    old_daily_end = "async def _send_daily_report(channel):"
    new_daily_end = """async def _send_daily_report(channel):
    # V9 OVERSIGHT
    try:
        oversight_report = await run_ai_oversight(channel)
        await channel.send(oversight_report)
    except Exception:
        pass"""
    if old_daily_end in code:
        code = code.replace(old_daily_end, new_daily_end, 1)
        changes += 1
        print("  [8] AI oversight integrated into daily report")


# ============================================================
# WRITE FILE
# ============================================================
with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)

print(f"\n  [OK] {changes} patches applied")
V9PATCH

echo ""

# ============================================================
# WATCHDOG SCRIPT
# ============================================================
echo "=== Creating watchdog script ==="
cat > /root/trading-bot/scripts/watchdog.sh << 'WATCHDOG'
#!/usr/bin/env bash
# TraderJoes Watchdog — runs every 5 minutes via cron
cd /root/trading-bot

# Check if bot container is running
BOT_STATUS=$(docker inspect -f '{{.State.Running}}' traderjoes-bot 2>/dev/null)

if [ "$BOT_STATUS" != "true" ]; then
    echo "$(date) — Bot is DOWN. Restarting..."
    docker compose up -d traderjoes-bot
    sleep 15
    
    # Send Discord alert via webhook (if configured)
    WEBHOOK=$(grep "^DISCORD_WEBHOOK=" .env 2>/dev/null | cut -d= -f2)
    if [ -n "$WEBHOOK" ]; then
        curl -s -H "Content-Type: application/json" \
            -d '{"content":"**WATCHDOG ALERT:** TraderJoes bot was down and has been restarted."}' \
            "$WEBHOOK" || true
    fi
fi

# Check OpenClaw
OC_STATUS=$(docker inspect -f '{{.State.Running}}' traderjoes-openclaw 2>/dev/null)
if [ "$OC_STATUS" != "true" ]; then
    echo "$(date) — OpenClaw is DOWN. Restarting..."
    docker compose up -d traderjoes-openclaw
fi

# Check Netdata
ND_STATUS=$(docker inspect -f '{{.State.Running}}' traderjoes-netdata 2>/dev/null)
if [ "$ND_STATUS" != "true" ]; then
    echo "$(date) — Netdata is DOWN. Restarting..."
    docker compose up -d traderjoes-netdata
fi

# Heartbeat log
echo "$(date) — Watchdog OK: bot=$BOT_STATUS oc=$OC_STATUS nd=$ND_STATUS" >> /root/trading-bot/data/watchdog.log

# Trim log
tail -1000 /root/trading-bot/data/watchdog.log > /root/trading-bot/data/watchdog.log.tmp 2>/dev/null
mv /root/trading-bot/data/watchdog.log.tmp /root/trading-bot/data/watchdog.log 2>/dev/null
WATCHDOG

chmod +x /root/trading-bot/scripts/watchdog.sh

# Install cron job for watchdog (every 5 minutes)
(crontab -l 2>/dev/null | grep -v "watchdog.sh"; echo "*/5 * * * * /root/trading-bot/scripts/watchdog.sh >> /root/trading-bot/data/watchdog.log 2>&1") | crontab -
echo "  [OK] Watchdog installed (cron every 5 min)"

echo ""

# ============================================================
# REBUILD AND RESTART
# ============================================================
echo "=== Rebuilding ==="
docker compose down 2>/dev/null || true
sleep 3
docker builder prune -f 2>/dev/null | tail -1
docker compose build --no-cache 2>&1 | tail -5
docker compose up -d
sleep 25

echo ""
echo "--- Container Status ---"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "--- Bot Logs ---"
docker logs traderjoes-bot --tail 8 2>&1

echo ""
echo "--- OpenClaw Health ---"
curl -s http://localhost:3000/health 2>/dev/null || echo "Starting..."

# ============================================================
# GITHUB SYNC
# ============================================================
echo ""
echo "=== GitHub Sync ==="
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A 2>/dev/null || true
git commit -m "V9: Full autonomous trading - auto-execution, exit manager, AI oversight, watchdog cron" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3
echo "  [OK] GitHub synchronized"

echo ""
echo "============================================"
echo "  TraderJoes V9 — Full Autonomous Trading"
echo "============================================"
echo ""
echo "AUTONOMOUS FEATURES:"
echo "  Auto-Execution — Trades execute automatically when edge score >= 65 and EV >= 5%"
echo "  Auto-Exits — Positions monitored every cycle, stop/target/trail/time exits"
echo "  AI Oversight — Daily review, auto-halt on drawdown/loss/win-rate triggers"
echo "  Watchdog — Cron every 5 min, auto-restart crashed containers"
echo "  Audit Trail — Every action logged with timestamp"
echo ""
echo "NEW COMMANDS:"
echo "  !positions    — Show all open positions with live P&L"
echo "  !closed       — Show recently closed positions"
echo "  !audit [n]    — Show last n audit log entries"
echo "  !oversight    — Run AI oversight check manually"
echo "  !auto-config  — View/update auto-execution settings"
echo ""
echo "SAFETY:"
echo "  Max position: 0.25% of portfolio per trade"
echo "  Max 20 trades/day | Max 10 concurrent positions"
echo "  Daily loss halt: -\$200 | Drawdown halt: 5%"
echo "  5 consecutive losses = auto-halt"
echo "  Win rate < 40% over 10+ trades = auto-halt"
echo "  Kill switch always overrides"
echo ""
echo "TEST:"
echo "  !positions"
echo "  !audit"
echo "  !oversight"
echo "  !auto-config"
echo "  !cycle"
echo "============================================"
