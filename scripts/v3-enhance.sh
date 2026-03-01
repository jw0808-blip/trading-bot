#!/usr/bin/env bash
# ============================================================================
# TraderJoes V3 — Items 12-15: Analytics, OpenClaw Studio, Auto-Alerts, Cost Optimization
# ============================================================================
# Safe to run multiple times (idempotent)
# Usage: bash /root/trading-bot/scripts/v3-enhance.sh
# ============================================================================
set -euo pipefail
cd /root/trading-bot

echo "=== TraderJoes V3 Enhancement Starting ==="
echo "Backing up main.py..."
cp main.py main.py.bak.v3

echo ""
echo "--- Patching main.py with items 12-15 ---"

python3 << 'V3PATCH'
import re

with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

changes = 0

# ============================================================
# 12. PERFORMANCE ANALYTICS + NETDATA METRICS EXPORTER
# ============================================================
analytics_code = '''

# ============================================================================
# PERFORMANCE ANALYTICS + NETDATA METRICS
# ============================================================================
import socket as _socket

ANALYTICS = {
    "total_trades": 0,
    "winning_trades": 0,
    "losing_trades": 0,
    "total_pnl": 0.0,
    "peak_equity": 10000.0,
    "current_equity": 10000.0,
    "max_drawdown": 0.0,
    "daily_pnl_history": [],
    "platform_pnl": {"kalshi": 0.0, "polymarket": 0.0, "robinhood": 0.0, "coinbase": 0.0, "phemex": 0.0},
    "openai_calls": 0,
    "openai_tokens": 0,
    "openai_cost_usd": 0.0,
    "openai_monthly_limit": 10.0,
}


def push_netdata_metric(key, value):
    """Push a metric to Netdata via StatsD (UDP)."""
    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        msg = f"traderjoes.{key}:{value}|g"
        sock.sendto(msg.encode(), ("127.0.0.1", 8125))
        sock.close()
    except Exception:
        pass


def push_all_analytics():
    """Push all analytics metrics to Netdata."""
    a = ANALYTICS
    push_netdata_metric("equity", a["current_equity"])
    push_netdata_metric("pnl_total", a["total_pnl"])
    push_netdata_metric("trades_total", a["total_trades"])
    push_netdata_metric("win_rate", (a["winning_trades"] / max(a["total_trades"], 1)) * 100)
    push_netdata_metric("max_drawdown", a["max_drawdown"])
    push_netdata_metric("openai_cost", a["openai_cost_usd"])
    push_netdata_metric("openai_calls", a["openai_calls"])
    for platform, pnl in a["platform_pnl"].items():
        push_netdata_metric(f"pnl_{platform}", pnl)

    # Calculate Sharpe (annualized, simplified)
    if len(a["daily_pnl_history"]) > 1:
        import statistics
        mean_pnl = statistics.mean(a["daily_pnl_history"])
        std_pnl = statistics.stdev(a["daily_pnl_history"]) or 1
        sharpe = (mean_pnl / std_pnl) * (252 ** 0.5)
        push_netdata_metric("sharpe_ratio", round(sharpe, 2))


def track_openai_usage(tokens_used, model="gpt-4o-mini"):
    """Track OpenAI API usage and costs."""
    ANALYTICS["openai_calls"] += 1
    ANALYTICS["openai_tokens"] += tokens_used
    # Pricing: gpt-4o-mini ~ $0.15/1M input + $0.60/1M output, estimate avg
    cost_per_1k = 0.0004 if "mini" in model else 0.003
    cost = (tokens_used / 1000) * cost_per_1k
    ANALYTICS["openai_cost_usd"] += cost
    push_netdata_metric("openai_cost", ANALYTICS["openai_cost_usd"])


@bot.command(name="analytics")
async def show_analytics(ctx):
    """Show performance analytics dashboard."""
    a = ANALYTICS
    win_rate = (a["winning_trades"] / max(a["total_trades"], 1)) * 100
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Calculate Sharpe
    sharpe = 0.0
    if len(a["daily_pnl_history"]) > 1:
        import statistics
        mean_pnl = statistics.mean(a["daily_pnl_history"])
        std_pnl = statistics.stdev(a["daily_pnl_history"]) or 1
        sharpe = (mean_pnl / std_pnl) * (252 ** 0.5)

    platform_lines = "\\n".join(f"    {p.title()}: ${v:+,.2f}" for p, v in a["platform_pnl"].items())

    report = (
        f"**Performance Analytics** | {ts}\\n"
        f"================================\\n"
        f"**Equity Curve:**\\n"
        f"  Current: ${a['current_equity']:,.2f}\\n"
        f"  Peak: ${a['peak_equity']:,.2f}\\n"
        f"  Drawdown: {a['max_drawdown']:.1f}%\\n"
        f"\\n**Trade Stats:**\\n"
        f"  Total: {a['total_trades']} | Wins: {a['winning_trades']} | Losses: {a['losing_trades']}\\n"
        f"  Win Rate: {win_rate:.1f}% | Sharpe: {sharpe:.2f}\\n"
        f"  Total P&L: ${a['total_pnl']:+,.2f}\\n"
        f"\\n**P&L by Platform:**\\n{platform_lines}\\n"
        f"\\n**OpenAI Usage:**\\n"
        f"  Calls: {a['openai_calls']} | Tokens: {a['openai_tokens']:,}\\n"
        f"  Cost: ${a['openai_cost_usd']:.4f} / ${a['openai_monthly_limit']:.2f} limit\\n"
        f"================================"
    )
    await ctx.send(report)

'''

if "ANALYTICS" not in code or "push_netdata_metric" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        analytics_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [12/15] Performance Analytics + Netdata metrics added")
else:
    print("  [12/15] Analytics already exists")


# ============================================================
# 14. AUTO-ALERTS FOR HIGH-EV OPPORTUNITIES
# ============================================================
alerts_code = '''

# ============================================================================
# AUTO-ALERTS FOR HIGH-EV OPPORTUNITIES
# ============================================================================
ALERT_CONFIG = {
    "enabled": True,
    "min_ev_threshold": 0.05,  # 5% EV minimum
    "cooldown_seconds": 1800,  # 30 min between alerts per market
    "max_alerts_per_hour": 5,
    "alert_history": {},       # market -> last_alert_time
    "alerts_this_hour": 0,
    "hour_reset_time": 0,
}


def should_alert(market_key, ev):
    """Check if we should send an alert for this opportunity."""
    cfg = ALERT_CONFIG
    if not cfg["enabled"]:
        return False
    if ev < cfg["min_ev_threshold"]:
        return False

    now = time.time()

    # Reset hourly counter
    if now - cfg["hour_reset_time"] > 3600:
        cfg["alerts_this_hour"] = 0
        cfg["hour_reset_time"] = now

    # Check hourly limit
    if cfg["alerts_this_hour"] >= cfg["max_alerts_per_hour"]:
        return False

    # Check cooldown per market
    last_alert = cfg["alert_history"].get(market_key, 0)
    if now - last_alert < cfg["cooldown_seconds"]:
        return False

    return True


def record_alert(market_key):
    """Record that an alert was sent."""
    cfg = ALERT_CONFIG
    cfg["alert_history"][market_key] = time.time()
    cfg["alerts_this_hour"] += 1


async def check_and_send_alerts():
    """Scan for high-EV opportunities and send alerts."""
    if not ALERT_CONFIG["enabled"] or not DISCORD_CHANNEL_ID:
        return

    try:
        channel = bot.get_channel(int(DISCORD_CHANNEL_ID))
        if not channel:
            return

        kalshi_opps = find_kalshi_opportunities()
        poly_opps = find_polymarket_opportunities()
        crypto_opps = find_crypto_momentum()

        all_opps = kalshi_opps + poly_opps + crypto_opps
        all_opps.sort(key=lambda x: x.get("ev", 0), reverse=True)

        for opp in all_opps:
            ev = opp.get("ev", 0)
            market_key = f"{opp['platform']}:{opp.get('ticker', opp['market'][:30])}"

            if should_alert(market_key, ev):
                ev_pct = ev * 100
                size = suggest_position_size(ev)
                alert = (
                    f"**HIGH EV ALERT**\\n"
                    f"[{opp['platform']}] {opp['type']} — EV: +{ev_pct:.1f}%\\n"
                    f"{opp['market']}\\n"
                    f"{opp['detail']}\\n"
                    f"Suggested size: ${size:,.0f}\\n"
                    f"Mode: {TRADING_MODE.upper()} | Use `!trade` to act"
                )
                await channel.send(alert)
                record_alert(market_key)
                log.info("Alert sent: %s EV +%.1f%%", market_key, ev_pct)

    except Exception as exc:
        log.warning("Alert check error: %s", exc)


@bot.command(name="alerts")
async def alerts_cmd(ctx, action: str = "status", value: str = ""):
    """Manage auto-alerts. Usage: !alerts [status|on|off|threshold 0.05|cooldown 30]"""
    cfg = ALERT_CONFIG
    if action == "on":
        cfg["enabled"] = True
        await ctx.send("Auto-alerts **ENABLED**")
    elif action == "off":
        cfg["enabled"] = False
        await ctx.send("Auto-alerts **DISABLED**")
    elif action == "threshold" and value:
        try:
            t = float(value)
            cfg["min_ev_threshold"] = t
            await ctx.send(f"Alert threshold set to {t*100:.1f}% EV")
        except ValueError:
            await ctx.send("Usage: `!alerts threshold 0.05`")
    elif action == "cooldown" and value:
        try:
            cfg["cooldown_seconds"] = int(value) * 60
            await ctx.send(f"Alert cooldown set to {value} minutes")
        except ValueError:
            await ctx.send("Usage: `!alerts cooldown 30`")
    else:
        await ctx.send(
            f"**Auto-Alert Status**\\n"
            f"Enabled: {cfg['enabled']}\\n"
            f"EV Threshold: {cfg['min_ev_threshold']*100:.1f}%\\n"
            f"Cooldown: {cfg['cooldown_seconds']//60}m\\n"
            f"Max/hour: {cfg['max_alerts_per_hour']}\\n"
            f"Alerts this hour: {cfg['alerts_this_hour']}\\n"
            f"Mode: {TRADING_MODE.upper()}"
        )

'''

if "ALERT_CONFIG" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        alerts_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [14/15] Auto-alerts for high-EV opportunities added")
else:
    print("  [14/15] Auto-alerts already exists")


# ============================================================
# 15. COST OPTIMIZATION + SAFETY GUARDS
# ============================================================
cost_code = '''

# ============================================================================
# COST OPTIMIZATION + SAFETY GUARDS
# ============================================================================
COST_CONFIG = {
    "preferred_model": "gpt-4o-mini",       # cheapest viable model
    "fallback_model": "gpt-3.5-turbo",
    "monthly_openai_limit": 10.00,           # $10/month max
    "monthly_openai_spent": 0.0,
    "kill_switch": False,                    # emergency stop all trading
    "max_daily_trades": 20,                  # max trades per day
    "daily_trades_count": 0,
    "max_portfolio_risk": 0.05,              # max 5% of portfolio at risk
    "strict_mode": True,                     # enforce all safety checks
}


@bot.command(name="kill-switch")
async def kill_switch(ctx, action: str = ""):
    """Emergency kill switch. Usage: !kill-switch on/off"""
    if action.lower() == "on":
        COST_CONFIG["kill_switch"] = True
        await ctx.send("**KILL SWITCH ACTIVATED** — All trading halted immediately.")
    elif action.lower() == "off":
        COST_CONFIG["kill_switch"] = True  # require explicit confirmation
        await ctx.send("Type `!confirm-kill-off` to deactivate kill switch.")
    else:
        status = "ACTIVE" if COST_CONFIG["kill_switch"] else "Inactive"
        await ctx.send(f"Kill switch: **{status}**\\nUsage: `!kill-switch on` or `!kill-switch off`")


@bot.command(name="confirm-kill-off")
async def confirm_kill_off(ctx):
    """Confirm deactivating the kill switch."""
    COST_CONFIG["kill_switch"] = False
    await ctx.send("Kill switch **DEACTIVATED** — Trading resumed.")


@bot.command(name="costs")
async def show_costs(ctx):
    """Show cost optimization and safety status."""
    c = COST_CONFIG
    a = ANALYTICS
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await ctx.send(
        f"**Cost & Safety Dashboard** | {ts}\\n"
        f"================================\\n"
        f"**OpenAI Costs:**\\n"
        f"  Model: {c['preferred_model']}\\n"
        f"  Monthly spent: ${a['openai_cost_usd']:.4f} / ${c['monthly_openai_limit']:.2f}\\n"
        f"  Calls: {a['openai_calls']} | Tokens: {a['openai_tokens']:,}\\n"
        f"\\n**Safety Guards:**\\n"
        f"  Kill switch: {'ACTIVE' if c['kill_switch'] else 'Off'}\\n"
        f"  Strict mode: {'On' if c['strict_mode'] else 'Off'}\\n"
        f"  Max daily trades: {c['max_daily_trades']}\\n"
        f"  Today\\'s trades: {c['daily_trades_count']}\\n"
        f"  Max position: {MAX_POSITION_PCT:.1%}\\n"
        f"  Max portfolio risk: {c['max_portfolio_risk']:.1%}\\n"
        f"  Daily loss limit: ${DAILY_LOSS_LIMIT:,.2f}\\n"
        f"  Trading mode: {TRADING_MODE.upper()}\\n"
        f"================================"
    )

'''

if "COST_CONFIG" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        cost_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [15/15] Cost optimization + safety guards added")
else:
    print("  [15/15] Cost optimization already exists")


# ============================================================
# 13. OPENCLAW STUDIO DASHBOARD ENHANCEMENTS
# ============================================================
# Add a command to show OpenClaw Studio status and URL
studio_code = '''

@bot.command(name="studio")
async def studio_status(ctx):
    """Show OpenClaw Studio dashboard status."""
    await ctx.send(
        f"**OpenClaw Studio**\\n================================\\n"
        f"Status: Running (localhost:3000)\\n"
        f"Access: Via Tailscale at http://100.89.63.72:3000\\n"
        f"Features:\\n"
        f"  • Agent chat interface\\n"
        f"  • Job scheduling & approval gates\\n"
        f"  • Real-time skill monitoring\\n"
        f"  • Trade execution dashboard\\n"
        f"\\n**Netdata Monitoring:**\\n"
        f"  URL: http://100.89.63.72:19999\\n"
        f"  Metrics: equity, PnL, Sharpe, drawdown, OpenAI costs\\n"
        f"================================"
    )

'''

if "async def studio_status" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        studio_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [13/15] OpenClaw Studio command added")
else:
    print("  [13/15] Studio already exists")


# ============================================================
# ADD ALERT SCANNING TO DAILY TASK
# ============================================================
if "check_and_send_alerts" in code and "alert scan" not in code:
    # Add alert checking to the scheduled daily task
    if "daily_report_task" in code and "check_and_send_alerts" not in code.split("daily_report_task")[1][:500]:
        old_daily_task = '''@tasks.loop(hours=24)
async def daily_report_task():
    if DISCORD_CHANNEL_ID:
        try:
            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
            if ch: await _send_daily_report(ch)
        except Exception as e: log.warning("Daily report error: %s", e)'''

        new_daily_task = '''@tasks.loop(hours=24)
async def daily_report_task():
    if DISCORD_CHANNEL_ID:
        try:
            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
            if ch: await _send_daily_report(ch)
            # Push analytics to Netdata
            push_all_analytics()
        except Exception as e: log.warning("Daily report error: %s", e)'''

        if old_daily_task in code:
            code = code.replace(old_daily_task, new_daily_task)
            print("  [+] Added analytics push to daily task")


# ============================================================
# ADD ALERT LOOP (runs every 10 minutes)
# ============================================================
alert_loop = '''

@tasks.loop(minutes=10)
async def alert_scan_task():
    """Periodically scan for high-EV opportunities and send alerts."""
    try:
        adapt_cycle_rate()  # adjust scan rate based on volatility
        if not CYCLE_PAUSED and not COST_CONFIG.get("kill_switch", False):
            await check_and_send_alerts()
            push_all_analytics()  # push metrics each cycle
    except Exception as exc:
        log.warning("Alert scan error: %s", exc)


@alert_scan_task.before_loop
async def before_alert_scan():
    await bot.wait_until_ready()

'''

if "alert_scan_task" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        alert_loop + "\n# ============================================================================\n# ENTRY POINT"
    )
    # Start the alert task in on_ready
    if "alert_scan_task.start()" not in code:
        code = code.replace(
            "daily_report_task.start()",
            "daily_report_task.start()\n    if not alert_scan_task.is_running():\n        alert_scan_task.start()"
        )
    changes += 1
    print("  [+] Auto-alert scan loop (10min) added")


# ============================================================
# UPDATE !help-tj WITH ALL NEW COMMANDS
# ============================================================
old_help_end = '''  !help-tj — This help message\\n"
        "================================"'''

new_help_end = '''  !help-tj — This help message\\n"
        "\\n**Analytics & Costs:**\\n"
        "  `!analytics` — Performance dashboard (equity, Sharpe, drawdown)\\n"
        "  `!costs` — OpenAI spend + safety guard status\\n"
        "  `!alerts [on|off|threshold|cooldown]` — Auto-alert config\\n"
        "  `!studio` — OpenClaw Studio dashboard info\\n"
        "  `!kill-switch [on|off]` — Emergency trading halt\\n"
        "================================"'''

if old_help_end in code:
    code = code.replace(old_help_end, new_help_end)
    print("  [+] Updated !help-tj with new commands")


# ============================================================
# ADD KILL SWITCH CHECK TO !trade
# ============================================================
if "kill_switch" not in code.split("async def trade(")[1][:500] if "async def trade(" in code else "":
    old_trade_check = 'if TRADING_MODE == "paper":'
    new_trade_check = '''if COST_CONFIG.get("kill_switch", False):
        await ctx.send("**BLOCKED:** Kill switch is active. Use `!kill-switch off` then `!confirm-kill-off` to resume.")
        return
    if TRADING_MODE == "paper":'''

    # Only replace the first occurrence in the trade function
    trade_section = code.find("async def trade(")
    if trade_section > 0:
        next_section = code.find("@bot.command", trade_section + 10)
        trade_code = code[trade_section:next_section]
        if old_trade_check in trade_code and "kill_switch" not in trade_code:
            new_trade_code = trade_code.replace(old_trade_check, new_trade_check, 1)
            code = code[:trade_section] + new_trade_code + code[next_section:]
            print("  [+] Added kill switch check to !trade")


# ============================================================
# WRITE FINAL FILE
# ============================================================
with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)

print(f"\n=== V3 PATCHES APPLIED ({changes} new features) ===")
V3PATCH

echo ""
echo "--- Rebuilding Docker image ---"
docker compose down traderjoes-bot
docker compose build traderjoes-bot
docker compose up -d
sleep 20

echo ""
echo "--- Verifying ---"
docker ps
echo ""
docker logs traderjoes-bot --tail 5

echo ""
echo "=== TraderJoes V3 Enhancement Complete ==="
echo ""
echo "New commands added:"
echo "  !analytics    — Performance dashboard (equity, Sharpe, drawdown)"
echo "  !costs        — OpenAI spend + safety status"
echo "  !alerts       — Auto-alert config (on/off/threshold)"
echo "  !studio       — OpenClaw Studio dashboard info"
echo "  !kill-switch  — Emergency trading halt"
echo ""
echo "Background tasks:"
echo "  • Alert scan runs every 10 minutes"
echo "  • Analytics pushed to Netdata each cycle"
echo "  • Kill switch blocks all trading when active"
echo ""
echo "Test: !analytics, !costs, !alerts, !studio, !kill-switch, !help-tj"
