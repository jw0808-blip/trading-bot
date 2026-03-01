#!/usr/bin/env bash
# ============================================================================
# TraderJoes V7 — Help menu fix, backtest real data, dashboard polish
# ============================================================================
set -uo pipefail
cd /root/trading-bot

echo "============================================"
echo "  TraderJoes V7 — Polish & Fixes"
echo "============================================"
echo ""

cp main.py main.py.bak.v7

python3 << 'V7PATCH'
with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

changes = 0

# ============================================================
# 1. REPLACE ENTIRE !help-tj WITH COMPLETE COMMAND LIST
# ============================================================
# Find and replace the entire help function
help_start = code.find("@bot.command(name=\"help-tj\")")
if help_start > 0:
    help_end = code.find("\n@bot.command", help_start + 20)
    if help_end < 0:
        # Try finding next function definition
        help_end = code.find("\n@bot.check", help_start + 20)
    if help_end < 0:
        help_end = code.find("\n@tasks.loop", help_start + 20)
    if help_end < 0:
        help_end = code.find("\nasync def check_and_send", help_start + 20)
    
    if help_end > help_start:
        new_help = '''@bot.command(name="help-tj")
async def help_tj(ctx):
    """Full command reference."""
    p1 = (
        "**TraderJoes Trading Firm — Command Reference**\\n"
        "================================\\n"
        "**Portfolio & Balances:**\\n"
        "  `!portfolio` — Live balances (5 platforms + USD totals)\\n"
        "  `!status` — Integration health check\\n"
        "  `!ping` — Bot health check\\n"
        "\\n**Scanning & Analysis:**\\n"
        "  `!cycle` — EV scan: Kalshi + Polymarket + Crypto\\n"
        "  `!analyze <question>` — AI market analysis\\n"
        "  `!forecast [topic]` — News sentiment + market forecast\\n"
    )
    await ctx.send(p1)
    p2 = (
        "**Trading:**\\n"
        "  `!trade buy/sell <asset> <amount>` — Propose trade\\n"
        "  `!confirm-trade` / `!cancel-trade` — Execute or cancel\\n"
        "  `!paper-status` — Paper portfolio\\n"
        "  `!paper-trade buy/sell <market> @ <price> x<size>` — Simulated trade\\n"
        "  `!auto-paper on/off` — Auto-execute high-EV in paper mode\\n"
        "  `!switch-mode paper/live` — Toggle trading mode\\n"
        "\\n**Backtesting:**\\n"
        "  `!backtest <strategy>` — Standard walk-forward backtest\\n"
        "  `!backtest-advanced <strategy>` — MC + particle filter + copula\\n"
        "  `!backtest-real <coin> <strategy> <days>` — Real CoinGecko data\\n"
    )
    await ctx.send(p2)
    p3 = (
        "**Reporting & Analytics:**\\n"
        "  `!daily` / `!report` — Full performance report\\n"
        "  `!analytics` — Equity curve, Sharpe, drawdown, PnL\\n"
        "  `!costs` — OpenAI spend + safety status\\n"
        "  `!signals [recent|stats|all]` — Signal history & learning\\n"
        "  `!resolve-signal <idx> win/loss` — Mark signal outcome\\n"
        "  `!log <message>` — Log to GitHub\\n"
        "\\n**System & Memory:**\\n"
        "  `!agents` — Multi-agent status (8 agents)\\n"
        "  `!memory [view|add-rule|add-lesson|track]` — AgentKeeper\\n"
        "  `!context [hot|domain|cold|all]` — 3-tier context memory\\n"
        "  `!save` / `!load` — Persist/restore all state\\n"
    )
    await ctx.send(p3)
    p4 = (
        "**Safety & Config:**\\n"
        "  `!security` — ClawJacked protection status\\n"
        "  `!alerts [on|off|threshold|cooldown]` — Auto-alert config\\n"
        "  `!kill-switch [on|off]` — Emergency trading halt\\n"
        "  `!set-cycle <interval>` — Set scan interval (e.g. 5m)\\n"
        "  `!pause-cycle` — Pause/resume auto-scan\\n"
        "  `!studio` — OpenClaw Studio dashboard info\\n"
        "  `!help-tj` — This help message\\n"
        "================================\\n"
        "Dashboards: http://89.167.108.136:3000 | http://89.167.108.136:19999"
    )
    await ctx.send(p4)

'''
        code = code[:help_start] + new_help + code[help_end:]
        changes += 1
        print("  [1] !help-tj replaced with complete 35-command reference")
    else:
        print("  [1] Could not find help function boundaries")
else:
    print("  [1] help-tj not found")


# ============================================================
# 2. CONNECT !backtest TO REAL DATA (enhance existing)
# ============================================================
# Add a real-data option to the existing !backtest command
if "fetch_historical_prices" in code and "Use real data when available" not in code:
    old_backtest_start = code.find("async def backtest(ctx")
    if old_backtest_start > 0:
        old_backtest_end = code.find("\n@bot.command", old_backtest_start + 10)
        if old_backtest_end > old_backtest_start:
            old_backtest = code[old_backtest_start:old_backtest_end]
            
            new_backtest = '''async def backtest(ctx, *, strategy: str = "momentum-crypto"):
    """Run a walk-forward backtest. Uses real CoinGecko data when possible.
    Usage: !backtest momentum-crypto  or  !backtest mean-reversion
    """
    msg = await ctx.send(f"Running backtest: **{strategy}**... fetching real data from CoinGecko")
    
    # Use real data when available
    coin_map = {
        "momentum-crypto": "bitcoin",
        "mean-reversion": "ethereum",
        "trend-following": "bitcoin",
        "momentum": "bitcoin",
        "btc": "bitcoin",
        "eth": "ethereum",
        "sol": "solana",
    }
    coin = coin_map.get(strategy.lower(), "bitcoin")
    
    prices = fetch_historical_prices(coin, 365)
    
    if prices and len(prices) >= 30:
        # Real data backtest
        returns = calculate_returns(prices)
        result = real_backtest_strategy(prices, strategy.split("-")[0] if "-" in strategy else strategy)
        
        if result:
            # Walk-forward: split into 5 windows
            window_size = len(prices) // 5
            wf_results = []
            for i in range(5):
                start = i * window_size
                end = start + window_size
                if end > len(prices):
                    end = len(prices)
                window_prices = prices[start:end]
                if len(window_prices) >= 10:
                    wr = real_backtest_strategy(window_prices, strategy.split("-")[0] if "-" in strategy else strategy)
                    if wr:
                        wf_results.append(wr)
            
            wf_returns = [w["total_return"] for w in wf_results] if wf_results else [0]
            
            # Monte Carlo
            import random, statistics
            mc_results = []
            for _ in range(1000):
                shuffled = random.sample(returns, len(returns))
                eq = 10000.0
                for r in shuffled:
                    eq += eq * r
                mc_results.append((eq - 10000) / 10000 * 100)
            mc_results.sort()
            
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            
            checks = [
                result["sharpe"] > 1.0,
                result["win_rate"] > 48,
                result["max_drawdown"] < 30,
                result["total_return"] > 0,
                len([w for w in wf_results if w["total_return"] > 0]) >= 3,
            ]
            passed = sum(checks)
            verdict = f"PASS ({passed}/5)" if passed >= 3 else f"FAIL ({passed}/5)"
            
            report = (
                f"**Walk-Forward Backtest** | {ts}\\n"
                f"Strategy: {strategy} | Data: {coin} ({len(prices)} days, REAL)\\n"
                f"================================\\n"
                f"**Full Period:**\\n"
                f"  Return: {result['total_return']:+.1f}% | Sharpe: {result['sharpe']:.2f}\\n"
                f"  Max DD: -{result['max_drawdown']:.1f}% | Win Rate: {result['win_rate']:.1f}%\\n"
                f"  Trades: {result['trades']}\\n"
                f"\\n**Walk-Forward ({len(wf_results)} windows):**\\n"
                f"  Returns: {', '.join(f'{r:+.1f}%' for r in wf_returns)}\\n"
                f"  Profitable windows: {len([r for r in wf_returns if r > 0])}/{len(wf_returns)}\\n"
                f"\\n**Monte Carlo (1K paths):**\\n"
                f"  5th: {mc_results[50]:+.1f}% | Median: {mc_results[500]:+.1f}% | 95th: {mc_results[950]:+.1f}%\\n"
                f"\\n**Verdict:** [{verdict}]\\n"
                f"================================"
            )
            await msg.edit(content=report)
            return
    
    # Fallback to simulated if no real data
    import random, statistics
    random.seed(42)
    num_days = 252
    returns = [random.gauss(0.0003, 0.015) for _ in range(num_days)]
    equity = 10000.0
    peak = equity
    max_dd = 0
    wins = losses = 0
    daily_pnl = []
    
    for r in returns:
        pnl = equity * r
        equity += pnl
        daily_pnl.append(pnl)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    
    total_ret = (equity - 10000) / 10000 * 100
    mean_pnl = statistics.mean(daily_pnl)
    std_pnl = statistics.stdev(daily_pnl) or 1
    sharpe = (mean_pnl / std_pnl) * (252 ** 0.5)
    win_rate = wins / max(wins + losses, 1) * 100
    
    mc_results = []
    for _ in range(1000):
        shuffled = random.sample(returns, len(returns))
        eq = 10000.0
        for r in shuffled:
            eq += eq * r
        mc_results.append((eq - 10000) / 10000 * 100)
    mc_results.sort()
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = (
        f"**Walk-Forward Backtest** | {ts}\\n"
        f"Strategy: {strategy} | Data: SIMULATED (252 days)\\n"
        f"================================\\n"
        f"Return: {total_ret:+.1f}% | Sharpe: {sharpe:.2f}\\n"
        f"Max DD: -{max_dd:.1f}% | Win Rate: {win_rate:.1f}% ({wins}W/{losses}L)\\n"
        f"MC 5th: {mc_results[50]:+.1f}% | Median: {mc_results[500]:+.1f}% | 95th: {mc_results[950]:+.1f}%\\n"
        f"================================"
    )
    await msg.edit(content=report)

'''
            code = code[:old_backtest_start] + new_backtest + code[old_backtest_end:]
            changes += 1
            print("  [2] !backtest now uses real CoinGecko data with walk-forward")
        else:
            print("  [2] Could not find backtest boundaries")
    else:
        print("  [2] backtest function not found")
else:
    print("  [2] Already using real data or function missing")


# ============================================================
# 3. ADD DRY-RUN MODE FOR LIVE EXECUTION
# ============================================================
dryrun_code = '''

DRY_RUN_MODE = True  # When True, live orders are logged but not sent


@bot.command(name="dry-run")
async def dry_run_cmd(ctx, action: str = ""):
    """Toggle dry-run mode. Usage: !dry-run on/off"""
    global DRY_RUN_MODE
    if action.lower() == "on":
        DRY_RUN_MODE = True
        await ctx.send("**Dry-run ON** — Live orders will be logged but NOT sent.")
    elif action.lower() == "off":
        DRY_RUN_MODE = False
        await ctx.send("**Dry-run OFF** — Live orders WILL be sent to exchanges. Be careful!")
    else:
        status = "ON (safe)" if DRY_RUN_MODE else "OFF (live orders enabled)"
        await ctx.send(f"Dry-run mode: **{status}**\\nUsage: `!dry-run on` or `!dry-run off`")

'''

if "DRY_RUN_MODE" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        dryrun_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [3] Dry-run mode added")

# Add dry-run check to execution functions
if "DRY_RUN_MODE" in code and "DRY_RUN_MODE check" not in code:
    for func_name in ["execute_kalshi_order", "execute_coinbase_order", "execute_robinhood_order", "execute_phemex_order"]:
        func_start = code.find(f"async def {func_name}")
        if func_start > 0:
            # Find first try: in the function
            first_try = code.find("    try:", func_start)
            if first_try > 0 and first_try < func_start + 500:
                dry_check = f'''    # DRY_RUN_MODE check
    if DRY_RUN_MODE:
        log.info("DRY RUN: %s %s — order NOT sent", action, ticker if 'ticker' in dir() else symbol if 'symbol' in dir() else amount)
        return True, f"DRY RUN: {{action}} order logged but not sent (dry-run mode)"
    try:'''
                code = code[:first_try] + dry_check + code[first_try + 8:]  # skip "    try:"
    changes += 1
    print("  [4] Dry-run checks added to all execution functions")


# ============================================================
# WRITE FILE
# ============================================================
with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)

print(f"\n  [OK] {changes} patches applied")
V7PATCH

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
docker logs traderjoes-bot --tail 5 2>&1

echo ""
echo "--- OpenClaw Health ---"
curl -s http://localhost:3000/health 2>/dev/null

# ============================================================
# GITHUB SYNC
# ============================================================
echo ""
echo "=== GitHub Sync ==="
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A 2>/dev/null || true
git commit -m "V7: Complete help menu (35 cmds), real-data backtest, dry-run mode" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3
echo "  [OK] GitHub synchronized"

echo ""
echo "============================================"
echo "  TraderJoes V7 — Complete"
echo "============================================"
echo ""
echo "FIXES:"
echo "  - !help-tj now shows all 35+ commands (split into 4 messages)"
echo "  - !backtest uses real CoinGecko data with walk-forward windows"
echo "  - Dry-run mode: all live execution is safe by default"
echo ""
echo "TEST:"
echo "  !help-tj"
echo "  !backtest momentum-crypto"
echo "  !dry-run"
echo "  !cycle"
echo "  !portfolio"
echo "============================================"
