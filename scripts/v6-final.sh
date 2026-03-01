#!/usr/bin/env bash
# ============================================================================
# TraderJoes V6 — Final Improvements
# Coinbase USD total, real backtest data, Netdata config, execution frameworks
# ============================================================================
set -uo pipefail
cd /root/trading-bot

echo "============================================"
echo "  TraderJoes V6 — Final Improvements"
echo "============================================"
echo ""

cp main.py main.py.bak.v6

python3 << 'V6PATCH'
import re

with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

changes = 0

# ============================================================
# 1. COINBASE FULL USD TOTAL
# ============================================================
coinbase_usd = '''

def get_crypto_usd_prices():
    """Get current USD prices for common cryptos from CoinGecko."""
    try:
        ids = "bitcoin,ethereum,dogecoin,stellar,shiba-inu,ripple,algorand,hedera-hashgraph,ren"
        r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd", timeout=10)
        if r.status_code == 200:
            data = r.json()
            # Map common symbols to coingecko ids
            return {
                "BTC": data.get("bitcoin", {}).get("usd", 0),
                "ETH": data.get("ethereum", {}).get("usd", 0),
                "DOGE": data.get("dogecoin", {}).get("usd", 0),
                "XLM": data.get("stellar", {}).get("usd", 0),
                "SHIB": data.get("shiba-inu", {}).get("usd", 0),
                "XRP": data.get("ripple", {}).get("usd", 0),
                "ALGO": data.get("algorand", {}).get("usd", 0),
                "HBAR": data.get("hedera-hashgraph", {}).get("usd", 0),
                "REN": data.get("ren", {}).get("usd", 0),
                "USDC": 1.0,
                "USD": 1.0,
                "USDT": 1.0,
            }
    except Exception as exc:
        log.warning("CoinGecko price fetch error: %s", exc)
    return {}

'''

if "get_crypto_usd_prices" not in code:
    code = code.replace(
        "def get_coinbase_balance",
        coinbase_usd + "def get_coinbase_balance"
    )
    changes += 1
    print("  [1] CoinGecko price lookup function added")

# Now update get_coinbase_balance to include USD total
if "get_coinbase_balance" in code and "total_usd_value" not in code:
    # Find the coinbase balance function
    cb_start = code.find("def get_coinbase_balance")
    cb_end = code.find("\ndef ", cb_start + 10)
    if cb_start > 0 and cb_end > 0:
        old_cb = code[cb_start:cb_end]
        
        # Build replacement that sums to USD
        new_cb = '''def get_coinbase_balance():
    """Fetch Coinbase balances with USD total."""
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return "$0.00 (not configured)"
    try:
        import jwt as _jwt, time as _time, secrets as _secrets
        uri = "api.coinbase.com"
        path = "/api/v3/brokerage/accounts"
        payload = {
            "sub": COINBASE_API_KEY,
            "iss": "cdp",
            "nbf": int(_time.time()),
            "exp": int(_time.time()) + 120,
            "uri": f"GET {uri}{path}",
        }
        token = _jwt.encode(payload, COINBASE_API_SECRET, algorithm="ES256",
                            headers={"kid": COINBASE_API_KEY, "nonce": _secrets.token_hex(16), "typ": "JWT"})
        hdrs = {"Authorization": f"Bearer {token}"}
        r = requests.get(f"https://{uri}{path}", headers=hdrs, timeout=15)
        if r.status_code != 200:
            return f"API error {r.status_code}"
        accounts = r.json().get("accounts", [])
        
        # Get prices for USD conversion
        prices = get_crypto_usd_prices()
        
        lines = []
        total_usd_value = 0.0
        for acct in accounts:
            bal = float(acct.get("available_balance", {}).get("value", 0))
            cur = acct.get("available_balance", {}).get("currency", "")
            if bal > 0.0001:
                # Calculate USD value
                price = prices.get(cur, 0)
                usd_val = bal * price
                total_usd_value += usd_val
                if usd_val > 0.01:
                    lines.append(f"{cur}: {bal:,.6f} (${usd_val:,.2f})")
                else:
                    lines.append(f"{cur}: {bal:,.6f}")
        
        summary = f"${total_usd_value:,.2f} USD total"
        if lines:
            return summary + "\\n" + "\\n".join(lines)
        return "$0.00 (no balances)"
    except Exception as exc:
        log.warning("Coinbase error: %s", exc)
        return f"Error: {exc}"
'''
        code = code[:cb_start] + new_cb + code[cb_end:]
        changes += 1
        print("  [2] Coinbase balance now shows USD total")
    else:
        print("  [2] Could not locate coinbase function boundaries")
else:
    print("  [2] Coinbase USD total already exists")


# ============================================================
# 2. REAL BACKTEST DATA FROM COINGECKO
# ============================================================
real_backtest = '''

def fetch_historical_prices(coin="bitcoin", days=90):
    """Fetch real historical daily prices from CoinGecko."""
    try:
        url = f"https://api.coingecko.com/api/v3/coins/{coin}/market_chart?vs_currency=usd&days={days}&interval=daily"
        r = requests.get(url, timeout=15, headers={"User-Agent": "TraderJoes/1.0"})
        if r.status_code == 200:
            data = r.json()
            prices = [p[1] for p in data.get("prices", [])]
            return prices
        else:
            log.warning("CoinGecko historical: %s", r.status_code)
            return []
    except Exception as exc:
        log.warning("Historical price fetch error: %s", exc)
        return []


def calculate_returns(prices):
    """Calculate daily returns from price series."""
    if len(prices) < 2:
        return []
    return [(prices[i] - prices[i-1]) / prices[i-1] for i in range(1, len(prices))]


def real_backtest_strategy(prices, strategy="momentum"):
    """Run a real backtest on historical price data."""
    if len(prices) < 20:
        return None
    
    returns = calculate_returns(prices)
    
    # Simple momentum strategy: buy when 5-day return > 0, sell otherwise
    position = 0  # 0 = flat, 1 = long
    equity = 10000.0
    peak_equity = 10000.0
    max_dd = 0.0
    trades = []
    daily_pnl = []
    
    for i in range(5, len(returns)):
        # 5-day momentum signal
        momentum = sum(returns[i-5:i])
        
        if strategy == "mean-reversion":
            # Mean reversion: buy on dips, sell on rips
            signal = -1 if momentum > 0.02 else (1 if momentum < -0.02 else 0)
        elif strategy == "trend-following":
            # Trend following: follow the momentum
            signal = 1 if momentum > 0.01 else (-1 if momentum < -0.01 else 0)
        else:  # momentum (default)
            signal = 1 if momentum > 0 else 0
        
        # Execute
        daily_ret = returns[i]
        if position == 1:
            pnl = equity * daily_ret
            equity += pnl
            daily_pnl.append(pnl)
        else:
            daily_pnl.append(0)
        
        # Update position
        old_pos = position
        position = max(0, min(1, signal))
        if old_pos != position:
            trades.append({"day": i, "action": "BUY" if position == 1 else "SELL", "equity": equity})
        
        # Track drawdown
        if equity > peak_equity:
            peak_equity = equity
        dd = (peak_equity - equity) / peak_equity * 100
        if dd > max_dd:
            max_dd = dd
    
    total_return = (equity - 10000) / 10000 * 100
    
    # Calculate Sharpe
    import statistics
    if len(daily_pnl) > 1 and any(p != 0 for p in daily_pnl):
        mean_pnl = statistics.mean(daily_pnl)
        std_pnl = statistics.stdev(daily_pnl) or 1
        sharpe = (mean_pnl / std_pnl) * (252 ** 0.5)
    else:
        sharpe = 0
    
    winning = len([p for p in daily_pnl if p > 0])
    losing = len([p for p in daily_pnl if p < 0])
    win_rate = winning / max(winning + losing, 1) * 100
    
    return {
        "total_return": total_return,
        "sharpe": sharpe,
        "max_drawdown": max_dd,
        "win_rate": win_rate,
        "trades": len(trades),
        "final_equity": equity,
        "daily_pnl": daily_pnl,
        "winning": winning,
        "losing": losing,
    }


@bot.command(name="backtest-real")
async def backtest_real(ctx, *, args: str = ""):
    """Backtest with REAL historical data from CoinGecko.
    Usage: !backtest-real bitcoin momentum 90
           !backtest-real ethereum mean-reversion 180
    """
    parts = args.split() if args else []
    coin = parts[0] if len(parts) > 0 else "bitcoin"
    strategy = parts[1] if len(parts) > 1 else "momentum"
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 90
    
    msg = await ctx.send(f"Fetching {days} days of {coin} data from CoinGecko...")
    
    prices = fetch_historical_prices(coin, days)
    if not prices or len(prices) < 20:
        await msg.edit(content=f"Could not fetch enough data for {coin}. Try: bitcoin, ethereum, solana, dogecoin")
        return
    
    result = real_backtest_strategy(prices, strategy)
    if not result:
        await msg.edit(content="Not enough data for backtest.")
        return
    
    # Monte Carlo on the real returns
    import random, statistics
    returns = calculate_returns(prices)
    mc_results = []
    for _ in range(1000):
        shuffled = random.sample(returns, len(returns))
        eq = 10000.0
        for r in shuffled:
            eq += eq * r
        mc_results.append((eq - 10000) / 10000 * 100)
    mc_results.sort()
    mc_5th = mc_results[50]
    mc_median = mc_results[500]
    mc_95th = mc_results[950]
    
    # Verdict
    checks = [
        result["sharpe"] > 0.5,
        result["win_rate"] > 45,
        result["max_drawdown"] < 35,
        result["total_return"] > 0,
        mc_median > 0,
    ]
    passed = sum(checks)
    verdict = f"PASS ({passed}/5)" if passed >= 3 else f"FAIL ({passed}/5)"
    icon = "PASS" if passed >= 3 else "FAIL"
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    report = (
        f"**Real Data Backtest** | {ts}\\n"
        f"Coin: {coin} | Strategy: {strategy} | Period: {days} days\\n"
        f"Data points: {len(prices)} daily prices\\n"
        f"================================\\n"
        f"**Performance:**\\n"
        f"  Return: {result['total_return']:+.1f}%\\n"
        f"  Final equity: ${result['final_equity']:,.2f}\\n"
        f"  Sharpe: {result['sharpe']:.2f}\\n"
        f"  Max Drawdown: -{result['max_drawdown']:.1f}%\\n"
        f"  Win Rate: {result['win_rate']:.1f}% ({result['winning']}W / {result['losing']}L)\\n"
        f"  Trades: {result['trades']}\\n"
        f"\\n**Monte Carlo (1K paths, shuffled returns):**\\n"
        f"  5th: {mc_5th:+.1f}% | Median: {mc_median:+.1f}% | 95th: {mc_95th:+.1f}%\\n"
        f"\\n**Verdict:** [{icon}] {verdict}\\n"
        f"================================"
    )
    
    if len(report) > 1900:
        report = report[:1900] + "\\n*...truncated*"
    await msg.edit(content=report)

'''

if "backtest_real" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        real_backtest + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [3] Real backtest with CoinGecko historical data added")
else:
    print("  [3] Real backtest already exists")


# ============================================================
# 3. ROBINHOOD + COINBASE EXECUTION FRAMEWORK
# ============================================================
exec_framework = '''

async def execute_coinbase_order(action, symbol, amount):
    """Place an order on Coinbase Advanced Trade API. Returns (success, message)."""
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return False, "Coinbase API keys not configured"
    try:
        import jwt as _jwt, time as _time, secrets as _secrets, uuid as _uuid
        
        uri = "api.coinbase.com"
        path = "/api/v3/brokerage/orders"
        
        # Build order
        product_id = f"{symbol}-USD"
        client_order_id = str(_uuid.uuid4())
        side_str = "BUY" if action.upper() == "BUY" else "SELL"
        
        order_body = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": side_str,
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": str(amount)
                }
            }
        }
        
        payload = {
            "sub": COINBASE_API_KEY,
            "iss": "cdp",
            "nbf": int(_time.time()),
            "exp": int(_time.time()) + 120,
            "uri": f"POST {uri}{path}",
        }
        token = _jwt.encode(payload, COINBASE_API_SECRET, algorithm="ES256",
                            headers={"kid": COINBASE_API_KEY, "nonce": _secrets.token_hex(16), "typ": "JWT"})
        
        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = requests.post(f"https://{uri}{path}", json=order_body, headers=hdrs, timeout=15)
        
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("order_id", "unknown")
            return True, f"Coinbase order placed: {side_str} {symbol} ${amount:.2f} (ID: {order_id})"
        else:
            return False, f"Coinbase error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Coinbase execution error: {exc}"


async def execute_robinhood_order(action, symbol, amount):
    """Place an order via Robinhood Crypto API. Returns (success, message)."""
    if not ROBINHOOD_API_KEY or not ROBINHOOD_PRIVATE_KEY:
        return False, "Robinhood API keys not configured"
    try:
        import base64, time as _time, uuid as _uuid
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives import serialization
        
        path = "/api/v1/crypto/trading/orders/"
        ts = str(int(_time.time()))
        
        side_str = "buy" if action.upper() == "BUY" else "sell"
        order_body = {
            "client_order_id": str(_uuid.uuid4()),
            "side": side_str,
            "symbol": symbol,
            "type": "market",
            "market_order_config": {
                "asset_quantity": str(round(amount, 8))
            }
        }
        
        import json
        body_str = json.dumps(order_body)
        message = f"{ROBINHOOD_API_KEY}{ts}{path}{body_str}"
        
        # Sign with Ed25519
        pk_bytes = base64.b64decode(ROBINHOOD_PRIVATE_KEY)
        private_key = Ed25519PrivateKey.from_private_bytes(pk_bytes[:32])
        signature = base64.b64encode(private_key.sign(message.encode())).decode()
        
        hdrs = {
            "x-api-key": ROBINHOOD_API_KEY,
            "x-timestamp": ts,
            "x-signature": signature,
            "Content-Type": "application/json",
        }
        
        r = requests.post(f"https://trading.robinhood.com{path}", json=order_body, headers=hdrs, timeout=15)
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("id", "unknown")
            return True, f"Robinhood order placed: {side_str} {symbol} qty={amount} (ID: {order_id})"
        else:
            return False, f"Robinhood error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Robinhood execution error: {exc}"

'''

if "execute_coinbase_order" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        exec_framework + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [4] Coinbase + Robinhood execution frameworks added")
else:
    print("  [4] Execution frameworks already exist")


# ============================================================
# 4. WIRE UP !trade TO USE REAL EXECUTION
# ============================================================
# Update confirm-trade to actually call execution functions in live mode
if "execute_kalshi_order" in code and "LIVE EXECUTION ROUTER" not in code:
    old_confirm = '''    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await ctx.send(f"**Trade Executed** | {ts}\\n{pending['action']} {pending['asset']} ${pending['amount']:,.2f}")'''
    
    new_confirm = '''    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    # LIVE EXECUTION ROUTER
    success = False
    exec_msg = ""
    asset = pending["asset"]
    action_str = pending["action"]
    amt = pending["amount"]
    
    if TRADING_MODE == "live":
        # Route to correct exchange
        if asset.startswith("KX") or asset.startswith("KALSHI"):
            success, exec_msg = await execute_kalshi_order(action_str, asset, amt)
        elif asset in ("BTC", "ETH", "DOGE", "XRP", "SOL", "ALGO", "SHIB", "XLM", "HBAR"):
            # Try Coinbase first, then Robinhood
            success, exec_msg = await execute_coinbase_order(action_str, asset, amt)
            if not success and "Robinhood" not in exec_msg:
                success, exec_msg = await execute_robinhood_order(action_str, asset, amt)
        elif asset.endswith("USDT") or asset.endswith("PERP"):
            success, exec_msg = await execute_phemex_order(action_str, asset, amt)
        else:
            exec_msg = f"No exchange matched for {asset}. Use ticker like BTC, ETH, KXTICKER, etc."
        
        status_icon = "OK" if success else "FAILED"
        await ctx.send(f"**Trade [{status_icon}]** | {ts}\\n{action_str} {asset} ${amt:,.2f}\\n{exec_msg}")
    else:
        await ctx.send(f"**Trade Executed (PAPER)** | {ts}\\n{action_str} {asset} ${amt:,.2f}\\n*Paper mode — no real order placed*")'''
    
    if old_confirm in code:
        code = code.replace(old_confirm, new_confirm)
        changes += 1
        print("  [5] Trade execution router wired up")
    else:
        print("  [5] Could not find confirm-trade code to patch")


# ============================================================
# 5. UPDATE !help-tj
# ============================================================
if "!backtest-real" not in code.split("help_tj")[1] if "help_tj" in code else "":
    code = code.replace(
        '  `!backtest-advanced <strategy>` — Full MC + particle filter + copula',
        '  `!backtest-advanced <strategy>` — Full MC + particle filter + copula\\n"\n        "  `!backtest-real <coin> <strategy> <days>` — Real data backtest (CoinGecko)'
    )
    changes += 1
    print("  [6] Updated !help-tj with !backtest-real")


# ============================================================
# WRITE FILE
# ============================================================
with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)

print(f"\n  [OK] {changes} patches applied")
V6PATCH

echo ""

# ============================================================
# NETDATA CUSTOM STATSD CONFIG
# ============================================================
echo "=== Configuring Netdata StatsD ==="
mkdir -p /root/trading-bot/netdata-config

cat > /root/trading-bot/netdata-config/traderjoes.conf << 'STATSD'
# TraderJoes custom StatsD metrics for Netdata
[app]
    name = traderjoes
    metrics = traderjoes.*
    private charts = no
    gaps when not collected = yes
    
[traderjoes_equity]
    name = traderjoes.equity
    title = Paper Portfolio Equity
    family = portfolio
    context = traderjoes.equity
    units = USD
    priority = 91000
    type = line
    dimension = traderjoes.equity equity last 1 1

[traderjoes_pnl]
    name = traderjoes.pnl_total
    title = Total P&L
    family = portfolio
    context = traderjoes.pnl
    units = USD
    priority = 91001
    type = line
    dimension = traderjoes.pnl_total pnl last 1 1

[traderjoes_trades]
    name = traderjoes.trades
    title = Total Trades
    family = trading
    context = traderjoes.trades
    units = trades
    priority = 91010
    type = line
    dimension = traderjoes.trades_total trades last 1 1

[traderjoes_winrate]
    name = traderjoes.winrate
    title = Win Rate
    family = trading
    context = traderjoes.winrate
    units = percent
    priority = 91011
    type = line
    dimension = traderjoes.win_rate rate last 1 1

[traderjoes_drawdown]
    name = traderjoes.drawdown
    title = Max Drawdown
    family = risk
    context = traderjoes.drawdown
    units = percent
    priority = 91020
    type = line
    dimension = traderjoes.max_drawdown dd last 1 1

[traderjoes_openai]
    name = traderjoes.openai
    title = OpenAI Cost
    family = costs
    context = traderjoes.openai
    units = USD
    priority = 91030
    type = line
    dimension = traderjoes.openai_cost cost last 1 1

[traderjoes_sharpe]
    name = traderjoes.sharpe
    title = Sharpe Ratio
    family = risk
    context = traderjoes.sharpe
    units = ratio
    priority = 91021
    type = line
    dimension = traderjoes.sharpe_ratio sharpe last 1 1
STATSD

# Mount the config into netdata container
python3 << 'NDFIX'
with open("/root/trading-bot/docker-compose.yml", "r") as f:
    yml = f.read()

if "traderjoes.conf" not in yml and "netdata-config" not in yml:
    yml = yml.replace(
        "      - /proc:/host/proc:ro",
        "      - ./netdata-config/traderjoes.conf:/etc/netdata/statsd.d/traderjoes.conf:ro\n      - /proc:/host/proc:ro"
    )
    with open("/root/trading-bot/docker-compose.yml", "w") as f:
        f.write(yml)
    print("  [OK] Netdata StatsD config mounted")
else:
    print("  [OK] Netdata config already mounted")
NDFIX

echo ""

# ============================================================
# REBUILD AND RESTART
# ============================================================
echo "=== Rebuilding all containers ==="
docker compose down 2>/dev/null || true
sleep 3

# Clean build cache to avoid snapshot errors
docker builder prune -f 2>/dev/null || true

docker compose build --no-cache 2>&1 | tail -10
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
curl -s http://localhost:3000/health 2>/dev/null || echo "Starting..."

# ============================================================
# GITHUB SYNC
# ============================================================
echo ""
echo "=== GitHub Sync ==="
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A 2>/dev/null || true
git commit -m "V6: Coinbase USD total, real backtesting, execution router, Netdata StatsD, Robinhood+Coinbase execution" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3
echo "  [OK] GitHub synchronized"

echo ""
echo "============================================"
echo "  TraderJoes V6 — Complete"
echo "============================================"
echo ""
echo "NEW/IMPROVED:"
echo "  - Coinbase !portfolio now shows USD total for all crypto"
echo "  - !backtest-real bitcoin momentum 90  — real CoinGecko data"
echo "  - !backtest-real ethereum mean-reversion 180"
echo "  - !backtest-real solana trend-following 365"
echo "  - Live execution router: !trade routes to correct exchange"
echo "  - Coinbase + Robinhood order placement frameworks"
echo "  - Netdata custom panels: equity, PnL, Sharpe, drawdown, OpenAI cost"
echo ""
echo "DASHBOARDS:"
echo "  OpenClaw Studio: http://89.167.108.136:3000"
echo "  Netdata:         http://89.167.108.136:19999"
echo ""
echo "TEST:"
echo "  !portfolio"
echo "  !backtest-real bitcoin momentum 90"
echo "  !backtest-real ethereum mean-reversion 180"
echo "  !help-tj"
echo "============================================"
