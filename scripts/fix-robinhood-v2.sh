#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

# Find the message signing line and update it
# The official Robinhood example uses: f"{api_key}{timestamp}{path}{body}"
# But some implementations use: f"{api_key}{timestamp}{method}{path}{body}"
# Let's try the exact official format with proper int timestamp

old_msg = '        message = f"{ROBINHOOD_API_KEY}{timestamp}{path}{body_str}"'
new_msg = '        message = f"{ROBINHOOD_API_KEY}{timestamp}{path}{body_str}"'

# Actually the real issue might be that timestamp needs to be int in the string
# And we need to check the symbol format - Robinhood uses "BTC-USD" not "BTC"
# Let's fix the symbol and use the exact official pattern

old_func_start = 'async def execute_robinhood_order'
rs = code.find(old_func_start)
re_ = code.find('\n@bot.command', rs + 10) if rs > 0 else -1
if re_ < 0 and rs > 0:
    re_ = code.find('\nasync def ', rs + 10)

if rs > 0 and re_ > rs:
    new_rh = '''async def execute_robinhood_order(action, symbol, amount):
    """Place a crypto order via Robinhood Crypto API. Returns (success, message)."""
    if not ROBINHOOD_API_KEY or not ROBINHOOD_PRIVATE_KEY:
        return False, "Robinhood API keys not configured"
    if DRY_RUN_MODE:
        log.info("DRY RUN: Robinhood %s %s $%.2f", action, symbol, amount)
        return True, f"DRY RUN: {action} order logged but not sent (dry-run mode)"
    try:
        import base64, json, uuid as _uuid, datetime as _dt
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        
        # Load private key — base64 decode, take first 32 bytes
        private_bytes = base64.b64decode(ROBINHOOD_PRIVATE_KEY)
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes[:32])
        
        # Build request
        base_url = "https://trading.robinhood.com"
        path = "/api/v1/crypto/trading/orders/"
        timestamp = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())
        
        side_str = "buy" if action.upper() == "BUY" else "sell"
        
        # Robinhood uses "BTC-USD" format, not just "BTC"
        rh_symbol = symbol if "-" in symbol else f"{symbol}-USD"
        
        order_body = {
            "client_order_id": str(_uuid.uuid4()),
            "side": side_str,
            "symbol": rh_symbol,
            "type": "market",
            "market_order_config": {
                "asset_quantity": str(round(amount, 8))
            }
        }
        
        body_str = json.dumps(order_body)
        
        # Sign exactly per official docs: api_key + str(timestamp) + path + body
        message = f"{ROBINHOOD_API_KEY}{timestamp}{path}{body_str}"
        signature = private_key.sign(message.encode("utf-8"))
        sig_b64 = base64.b64encode(signature).decode("utf-8")
        
        headers = {
            "x-api-key": ROBINHOOD_API_KEY,
            "x-timestamp": str(timestamp),
            "x-signature": sig_b64,
            "Content-Type": "application/json",
        }
        
        log.info("Robinhood request: %s %s %s qty=%s", side_str, rh_symbol, base_url + path, amount)
        
        r = requests.post(f"{base_url}{path}", data=body_str, headers=headers, timeout=15)
        
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("id", "unknown")
            status = data.get("state", "unknown")
            return True, f"Robinhood order: {side_str} {rh_symbol} qty={amount} (ID: {order_id}, Status: {status})"
        else:
            return False, f"Robinhood error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Robinhood execution error: {exc}"

'''
    code = code[:rs] + new_rh + code[re_+1:]
    print("Updated Robinhood with BTC-USD symbol format + int timestamp")
else:
    print(f"Could not find function: rs={rs}, re={re_}")

with open("main.py", "w") as f:
    f.write(code)
FIX

docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18
docker logs traderjoes-bot --tail 3 2>&1
