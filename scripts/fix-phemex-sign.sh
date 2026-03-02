#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

# Find and replace the entire phemex function
old_start = 'async def execute_phemex_order'
ps = code.find(old_start)

# Find end of function
pe = code.find('\nasync def ', ps + 10)
if pe < 0:
    pe = code.find('\n@bot.command', ps + 10)
if pe < 0:
    pe = code.find('\nDRY_RUN_MODE', ps + 10)
# Also check for standalone function defs
if pe < 0:
    pe = code.find('\ndef ', ps + 10)

if ps > 0 and pe > ps:
    new_phemex = '''async def execute_phemex_order(action, symbol, amount):
    """Place a spot order on Phemex. Returns (success, message)."""
    if not PHEMEX_API_KEY or not PHEMEX_API_SECRET:
        return False, "Phemex API keys not configured"
    if DRY_RUN_MODE:
        log.info("DRY RUN: Phemex %s %s $%.2f", action, symbol, amount)
        return True, f"DRY RUN: {action} order logged but not sent (dry-run mode)"
    try:
        import hmac, hashlib, time as _time, json
        
        expiry = str(int(_time.time()) + 60)
        
        # Phemex spot order endpoint
        path = "/spot/orders"
        side = "Buy" if action.upper() == "BUY" else "Sell"
        
        # Symbol must start with 's' for spot
        spot_symbol = symbol if symbol.startswith("s") else f"s{symbol}"
        
        order_body = {
            "symbol": spot_symbol,
            "clOrdID": f"tj-{int(_time.time())}",
            "side": side,
            "qtyType": "ByQuote",
            "quoteQtyEv": int(amount * 100000000),
            "ordType": "Market",
            "timeInForce": "ImmediateOrCancel",
        }
        
        body_str = json.dumps(order_body, separators=(",", ":"))
        
        # Sign: path + expiry + body (concatenated, no separators)
        sign_str = path + expiry + body_str
        sig = hmac.new(
            PHEMEX_API_SECRET.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        headers = {
            "x-phemex-access-token": PHEMEX_API_KEY,
            "x-phemex-request-expiry": expiry,
            "x-phemex-request-signature": sig,
            "Content-Type": "application/json",
        }
        
        r = requests.post(f"https://api.phemex.com{path}", data=body_str, headers=headers, timeout=15)
        
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                order_id = data.get("data", {}).get("orderID", "unknown")
                return True, f"Phemex order: {side} {spot_symbol} ${amount:.2f} (ID: {order_id})"
            else:
                return False, f"Phemex error: code={data.get('code')} msg={data.get('msg', 'unknown')}"
        else:
            return False, f"Phemex API error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Phemex execution error: {exc}"

'''
    code = code[:ps] + new_phemex + code[pe+1:]
    print("Replaced Phemex function with corrected signing")
else:
    print(f"Could not find Phemex function: ps={ps}, pe={pe}")

with open("main.py", "w") as f:
    f.write(code)
FIX

echo "Rebuilding..."
docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18
docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""
docker logs traderjoes-bot --tail 3 2>&1
