#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

# Fix 1: Kalshi yes_price_dollars must be string
code = code.replace('"yes_price_dollars": 0.99,', '"yes_price_dollars": "0.99",')
print("Fixed Kalshi yes_price_dollars to string")

# Fix 2: Phemex signing - use correct HMAC format
old_phemex = '''        # Sign: path + queryString + expiry + body
        msg = path + expiry + body_str
        sig = hmac.new(PHEMEX_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()'''

new_phemex = '''        # Sign: path + queryString + expiry + body
        msg = path + expiry + body_str
        sig = hmac.new(PHEMEX_API_SECRET.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()'''

if old_phemex in code:
    code = code.replace(old_phemex, new_phemex)
    print("Updated Phemex HMAC encoding")

# Also check if hmac.new should be hmac.HMAC or hmac.new
# Python uses hmac.new() which is correct
# But let's also check the Phemex API path and headers
old_phemex_path = '''        path = "/spot/orders"'''
new_phemex_path = '''        path = "/spot/orders/create"'''

if old_phemex_path in code:
    code = code.replace(old_phemex_path, new_phemex_path)
    print("Fixed Phemex endpoint path")

with open("main.py", "w") as f:
    f.write(code)

print("Done")
FIX

echo ""
echo "Verifying..."
grep 'yes_price_dollars' main.py
echo ""

echo "Rebuilding..."
docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18

docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""
docker logs traderjoes-bot --tail 3 2>&1
