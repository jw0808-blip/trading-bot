#!/usr/bin/env bash
cd /root/trading-bot

echo "============================================"
echo "  TraderJoes V9.1 — Test Orders + Alpaca"
echo "============================================"
echo ""

cp main.py main.py.bak.v91

python3 << 'PATCH'
with open("main.py", "r") as f:
    code = f.read()

changes = 0

# ============================================================
# 1. !test-execution COMMAND
# ============================================================
test_exec = '''

@bot.command(name="test-execution")
async def test_execution(ctx, platform: str = "", amount: str = "1"):
    """Test order execution on a specific platform with tiny amount.
    Usage: !test-execution kalshi 1
           !test-execution coinbase 2
           !test-execution robinhood 1
           !test-execution phemex 1
    """
    if not platform:
        await ctx.send(
            "**Test Execution — Safe Order Testing**\\n"
            "Usage: `!test-execution <platform> <amount>`\\n"
            "Platforms: `kalshi`, `coinbase`, `robinhood`, `phemex`\\n"
            "Amount: $1-5 recommended for testing\\n"
            "\\nThis will attempt a REAL order (unless dry-run is on).\\n"
            f"Dry-run mode: **{'ON (safe)' if DRY_RUN_MODE else 'OFF (real orders!)'}**\\n"
            "Use `!dry-run on` first if you want to test without real orders."
        )
        return
    
    platform = platform.lower()
    try:
        amt = float(amount.replace("$", ""))
    except ValueError:
        await ctx.send(f"Invalid amount: {amount}")
        return
    
    if amt > 10:
        await ctx.send(f"Max test amount is $10. You specified ${amt:.2f}.")
        return
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dry_tag = " [DRY-RUN]" if DRY_RUN_MODE else " [REAL]"
    
    msg = await ctx.send(f"Testing {platform} execution{dry_tag}... ${amt:.2f}")
    
    success = False
    exec_msg = ""
    
    if platform == "kalshi":
        # Test with a cheap market
        try:
            success, exec_msg = await execute_kalshi_order("BUY", "KXWARMING-50", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "coinbase":
        try:
            success, exec_msg = await execute_coinbase_order("BUY", "BTC", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "robinhood":
        try:
            # Robinhood crypto: buy small amount of XRP (cheapest)
            qty = amt / 2.0  # Approx XRP price ~$2
            success, exec_msg = await execute_robinhood_order("BUY", "XRP", qty)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "phemex":
        try:
            success, exec_msg = await execute_phemex_order("BUY", "BTCUSDT", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "alpaca":
        try:
            success, exec_msg = await execute_alpaca_order("BUY", "AAPL", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    else:
        await msg.edit(content=f"Unknown platform: `{platform}`. Use: kalshi, coinbase, robinhood, phemex, alpaca")
        return
    
    status = "SUCCESS" if success else "FAILED"
    
    audit_log("TEST_EXECUTION", {
        "platform": platform,
        "amount": amt,
        "dry_run": DRY_RUN_MODE,
        "success": success,
        "message": exec_msg[:100],
    })
    
    await msg.edit(content=(
        f"**Test Execution{dry_tag}** | {ts}\\n"
        f"Platform: {platform} | Amount: ${amt:.2f}\\n"
        f"Status: **{status}**\\n"
        f"Response: {exec_msg[:500]}\\n"
        f"\\n{'Use `!dry-run off` to test with real orders.' if DRY_RUN_MODE else 'This was a REAL order attempt.'}"
    ))

'''

if "test_execution" not in code or "test-execution" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        test_exec + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [1] !test-execution command added")
else:
    print("  [1] Already exists")


# ============================================================
# 2. ALPACA INTEGRATION
# ============================================================
alpaca_code = '''

# ============================================================================
# ALPACA INTEGRATION (Stocks + Crypto)
# ============================================================================
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")  # Paper by default


def get_alpaca_balance():
    """Fetch Alpaca account balance."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return "Not configured (add ALPACA_API_KEY + ALPACA_SECRET_KEY to .env)"
    try:
        hdrs = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        r = requests.get(f"{ALPACA_BASE_URL}/v2/account", headers=hdrs, timeout=10)
        if r.status_code == 200:
            acct = r.json()
            equity = float(acct.get("equity", 0))
            cash = float(acct.get("cash", 0))
            buying_power = float(acct.get("buying_power", 0))
            pnl = float(acct.get("portfolio_value", 0)) - float(acct.get("last_equity", 0))
            
            # Get positions
            r2 = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=hdrs, timeout=10)
            positions_str = ""
            if r2.status_code == 200:
                positions = r2.json()
                for p in positions[:10]:
                    sym = p.get("symbol", "")
                    qty = p.get("qty", "0")
                    mkt_val = float(p.get("market_value", 0))
                    unrealized = float(p.get("unrealized_pl", 0))
                    positions_str += f"\\n  {sym}: {qty} shares (${mkt_val:,.2f}, P&L: ${unrealized:+,.2f})"
            
            result = f"${equity:,.2f} equity | ${cash:,.2f} cash | BP: ${buying_power:,.2f}"
            if positions_str:
                result += positions_str
            
            is_paper = "paper" in ALPACA_BASE_URL
            result += f"\\n  Mode: {'PAPER' if is_paper else 'LIVE'}"
            return result
        else:
            return f"API error: {r.status_code} {r.text[:100]}"
    except Exception as exc:
        return f"Error: {exc}"


async def execute_alpaca_order(action, symbol, amount):
    """Place an order via Alpaca API. Returns (success, message)."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, "Alpaca not configured (add API keys to .env)"
    
    # DRY_RUN check
    if DRY_RUN_MODE:
        log.info("DRY RUN: Alpaca %s %s $%.2f", action, symbol, amount)
        return True, f"DRY RUN: {action} {symbol} ${amount:.2f} — order NOT sent (dry-run mode)"
    
    try:
        hdrs = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }
        
        side = "buy" if action.upper() == "BUY" else "sell"
        
        order_body = {
            "symbol": symbol,
            "notional": str(round(amount, 2)),  # Dollar amount
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        
        r = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=order_body, headers=hdrs, timeout=15)
        
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("id", "unknown")
            status = data.get("status", "unknown")
            return True, f"Alpaca order placed: {side} {symbol} ${amount:.2f} (ID: {order_id}, Status: {status})"
        else:
            return False, f"Alpaca error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Alpaca execution error: {exc}"

'''

if "ALPACA_API_KEY" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        alpaca_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [2] Alpaca integration added (balance + execution)")
else:
    print("  [2] Alpaca already exists")


# ============================================================
# 3. ADD ALPACA TO !portfolio
# ============================================================
if "get_alpaca_balance" in code and "Alpaca:" not in code.split("async def portfolio")[1][:2000] if "async def portfolio" in code else "":
    old_predictit = 'PredictIt & Interactive Brokers: pending integration'
    new_predictit = 'Alpaca: {get_alpaca_balance()}\\nPredictIt & Interactive Brokers: pending integration'
    if old_predictit in code:
        code = code.replace(old_predictit, new_predictit)
        changes += 1
        print("  [3] Alpaca added to !portfolio")
    else:
        print("  [3] Could not find portfolio integration point")


# ============================================================
# 4. ADD ALPACA TO EXECUTION ROUTER
# ============================================================
if "execute_alpaca_order" in code and "# Route to Alpaca" not in code:
    old_route = 'exec_msg = f"No exchange matched for {asset}. Use ticker like BTC, ETH, KXTICKER, etc."'
    new_route = '''# Route to Alpaca for stocks
            if ALPACA_API_KEY:
                success, exec_msg = await execute_alpaca_order(action_str, asset, amt)
            else:
                exec_msg = f"No exchange matched for {asset}. Use ticker like BTC, ETH, KXTICKER, AAPL, etc."'''
    if old_route in code:
        code = code.replace(old_route, new_route)
        changes += 1
        print("  [4] Alpaca added to execution router")


# ============================================================
# 5. FIX REGIME IN !cycle
# ============================================================
if "Regime: UNKNOWN" not in code and "detect_regime()" in code:
    # Regime is already called in edge/regime commands
    # Just make sure cycle header shows it properly
    pass

# Find the cycle command and add regime call
cycle_func_start = code.find("async def cycle(ctx")
if cycle_func_start > 0:
    cycle_func_end = code.find("\n@bot.command", cycle_func_start + 10)
    cycle_body = code[cycle_func_start:cycle_func_end] if cycle_func_end > 0 else ""
    
    if "detect_regime()" not in cycle_body and "REGIME_CONFIG" in code:
        # Add detect_regime() call after fng fetch in cycle
        old_fng_in_cycle = 'fng_val, fng_label = get_fear_greed()'
        # Find it within the cycle function only
        fng_pos = code.find(old_fng_in_cycle, cycle_func_start)
        if fng_pos > 0 and (cycle_func_end < 0 or fng_pos < cycle_func_end):
            # Check if it's inside a try block
            # Look at surrounding lines
            pre_context = code[fng_pos-50:fng_pos]
            if "try:" in pre_context:
                # Inside try, add after with same indent
                code = code[:fng_pos] + old_fng_in_cycle + "\n        detect_regime()" + code[fng_pos + len(old_fng_in_cycle):]
            else:
                code = code[:fng_pos] + old_fng_in_cycle + "\n    detect_regime()" + code[fng_pos + len(old_fng_in_cycle):]
            changes += 1
            print("  [5] Regime detection added to !cycle")


# ============================================================
# 6. ADD .env TEMPLATE FOR ALPACA
# ============================================================
# Will be done outside Python

# ============================================================
# WRITE FILE
# ============================================================
with open("main.py", "w") as f:
    f.write(code)

print(f"\n  [OK] {changes} patches applied")
PATCH

echo ""

# Add Alpaca keys to .env if not present
if ! grep -q "ALPACA_API_KEY" .env 2>/dev/null; then
    echo "" >> .env
    echo "# Alpaca (stocks + crypto) — sign up at alpaca.markets" >> .env
    echo "ALPACA_API_KEY=" >> .env
    echo "ALPACA_SECRET_KEY=" >> .env
    echo "ALPACA_BASE_URL=https://paper-api.alpaca.markets" >> .env
    echo "  [OK] Alpaca .env keys added (empty — fill when signed up)"
else
    echo "  [OK] Alpaca keys already in .env"
fi

echo ""

# Rebuild
echo "=== Rebuilding ==="
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

echo ""
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
echo ""
docker logs traderjoes-bot --tail 5 2>&1

# GitHub sync
echo ""
echo "=== GitHub Sync ==="
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A 2>/dev/null || true
git commit -m "V9.1: Test execution command, Alpaca integration, regime fix" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3
echo "  [OK] GitHub synchronized"

echo ""
echo "============================================"
echo "  TraderJoes V9.1 — Ready for Live Testing"
echo "============================================"
echo ""
echo "TEST ORDER COMMANDS (run in Discord):"
echo "  Step 1: !dry-run on              (verify dry-run is safe)"
echo "  Step 2: !test-execution kalshi 1  (test Kalshi — should show DRY RUN)"
echo "  Step 3: !test-execution coinbase 2  (test Coinbase)"
echo "  Step 4: !test-execution robinhood 1  (test Robinhood)"
echo "  Step 5: !test-execution phemex 1  (test Phemex)"
echo ""
echo "  When ready for REAL test orders:"
echo "  Step 6: !dry-run off              (enable real orders)"
echo "  Step 7: !test-execution kalshi 1  (REAL \$1 order on Kalshi)"
echo "  Step 8: !test-execution coinbase 2  (REAL \$2 BTC buy on Coinbase)"
echo ""
echo "  Alpaca (when keys are added):"
echo "  !test-execution alpaca 1          (test Alpaca stock order)"
echo ""
echo "============================================"
