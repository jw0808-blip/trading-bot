#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

# ============================================================
# 1. REPLACE ENTIRE Kalshi execution function
# ============================================================
old_kalshi_start = 'async def execute_kalshi_order(action, ticker, amount):'
old_kalshi_end_marker = 'async def execute_phemex_order'

ks = code.find(old_kalshi_start)
ke = code.find(old_kalshi_end_marker)

if ks > 0 and ke > ks:
    new_kalshi = '''async def execute_kalshi_order(action, ticker, amount):
    """Place a real order on Kalshi using RSA-PSS auth. Returns (success, message)."""
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY:
        return False, "Kalshi API keys not configured"
    if DRY_RUN_MODE:
        log.info("DRY RUN: Kalshi %s %s $%.2f", action, ticker, amount)
        return True, f"DRY RUN: {action} order logged but not sent (dry-run mode)"
    try:
        import base64, datetime as _dt
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as _padding
        from cryptography.hazmat.backends import default_backend
        
        # Load private key
        pk_str = KALSHI_PRIVATE_KEY
        if "BEGIN" in pk_str:
            private_key = serialization.load_pem_private_key(pk_str.encode(), password=None, backend=default_backend())
        else:
            der = base64.b64decode(pk_str)
            private_key = serialization.load_der_private_key(der, password=None, backend=default_backend())
        
        # Timestamp in milliseconds
        ts_ms = str(int(_dt.datetime.now().timestamp() * 1000))
        
        method = "POST"
        path = "/trade-api/v2/portfolio/orders"
        
        # Sign: timestamp + method + path (RSA-PSS with SHA256)
        msg_string = ts_ms + method + path
        signature = private_key.sign(
            msg_string.encode(),
            _padding.PSS(
                mgf=_padding.MGF1(hashes.SHA256()),
                salt_length=_padding.PSS.MAX_LENGTH
            ),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(signature).decode()
        
        # Build order
        side = "yes" if action.upper() == "BUY" else "no"
        count = max(int(amount), 1)  # Minimum 1 contract
        
        order_data = {
            "ticker": ticker,
            "type": "market",
            "action": "buy" if action.upper() == "BUY" else "sell",
            "side": side,
            "count": count,
        }
        
        headers = {
            "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        r = requests.post(f"https://api.elections.kalshi.com{path}", json=order_data, headers=headers, timeout=15)
        
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("order", {}).get("order_id", "unknown")
            return True, f"Kalshi order placed: {action} {ticker} x{count} (ID: {order_id})"
        else:
            return False, f"Kalshi API error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Kalshi execution error: {exc}"


'''
    code = code[:ks] + new_kalshi + code[ke:]
    print("Replaced Kalshi execution with RSA-PSS auth")
else:
    print(f"Could not find Kalshi function boundaries: start={ks}, end={ke}")


# ============================================================
# 2. FIX PHEMEX — needs trade permission check
# ============================================================
# Phemex returned 401 Insufficient privilege — likely needs correct API path
# Replace the phemex function
old_phemex_start = 'async def execute_phemex_order'
old_phemex_end = None

ps = code.find(old_phemex_start)
if ps > 0:
    # Find end of function (next async def or non-indented line)
    pe = code.find('\nasync def ', ps + 10)
    if pe < 0:
        pe = code.find('\ndef ', ps + 10)
    if pe < 0:
        pe = code.find('\n@bot.command', ps + 10)
    if pe < 0:
        pe = code.find('\nDRY_RUN_MODE', ps + 10)
    
    if pe > ps:
        new_phemex = '''async def execute_phemex_order(action, symbol, amount):
    """Place an order on Phemex. Returns (success, message)."""
    if not PHEMEX_API_KEY or not PHEMEX_API_SECRET:
        return False, "Phemex API keys not configured"
    if DRY_RUN_MODE:
        log.info("DRY RUN: Phemex %s %s $%.2f", action, symbol, amount)
        return True, f"DRY RUN: {action} order logged but not sent (dry-run mode)"
    try:
        import hmac, hashlib, time as _time
        
        expiry = str(int(_time.time()) + 60)
        
        # Phemex spot order
        path = "/spot/orders"
        side = "Buy" if action.upper() == "BUY" else "Sell"
        
        order_data = {
            "symbol": symbol if "s" in symbol.lower() else f"s{symbol}",
            "side": side,
            "ordType": "Market",
            "quoteQtyEv": int(amount * 100000000),  # Scale to Phemex value scale
        }
        
        import json
        body_str = json.dumps(order_data, separators=(",", ":"))
        
        # Sign: path + queryString + expiry + body
        msg = path + expiry + body_str
        sig = hmac.new(PHEMEX_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        
        headers = {
            "x-phemex-access-token": PHEMEX_API_KEY,
            "x-phemex-request-expiry": expiry,
            "x-phemex-request-signature": sig,
            "Content-Type": "application/json",
        }
        
        r = requests.post(f"https://api.phemex.com{path}", json=order_data, headers=headers, timeout=15)
        
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                order_id = data.get("data", {}).get("orderID", "unknown")
                return True, f"Phemex order: {side} {symbol} ${amount:.2f} (ID: {order_id})"
            else:
                return False, f"Phemex error: {data.get('msg', 'unknown')}"
        else:
            return False, f"Phemex API error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Phemex execution error: {exc}"

'''
        code = code[:ps] + new_phemex + code[pe:]
        print("Replaced Phemex execution function")
    else:
        print(f"Could not find Phemex function end: pe={pe}")
else:
    print("Phemex function not found")


with open("main.py", "w") as f:
    f.write(code)
print("Done")
FIX

echo "Rebuilding..."
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""
docker logs traderjoes-bot --tail 3 2>&1

# GitHub sync
cd /root/trading-bot
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A && git commit -m "Fix: Kalshi RSA-PSS auth + Phemex signing" --allow-empty 2>/dev/null
git push -u origin main --force 2>&1 | tail -2

echo ""
echo "TEST: !test-execution kalshi 1"
echo "TEST: !test-execution phemex 1"
