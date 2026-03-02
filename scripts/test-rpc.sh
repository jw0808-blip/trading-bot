#!/usr/bin/env bash
cd /root/trading-bot

echo "=== Testing RPC connectivity from container ==="
docker exec traderjoes-bot python3 /app/test_rpc.py 2>&1 || echo "test script missing, creating..."

# Create test script inside container
docker exec traderjoes-bot bash -c 'cat > /app/test_rpc.py << PYRPC
import requests

rpcs = [
    ("polygon-rpc.com", "https://polygon-rpc.com"),
    ("ankr", "https://rpc.ankr.com/polygon"),
    ("polygonscan", "https://api.polygonscan.com/api?module=proxy&action=eth_call&to=0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174&data=0x70a082310000000000000000000000000xdabb414f8bb481c2c99378d15dbae3808a3fe6f7&tag=latest"),
    ("data-api", "https://data-api.polymarket.com/value?user=0xdabb414f8bb481c2c99378d15dbae3808a3fe6f7"),
    ("gamma-api", "https://gamma-api.polymarket.com/markets?limit=1"),
    ("clob", "https://clob.polymarket.com/time"),
]

for name, url in rpcs:
    try:
        if name in ("polygon-rpc.com", "ankr"):
            r = requests.post(url, json={"jsonrpc":"2.0","method":"eth_chainId","params":[],"id":1}, timeout=5)
        else:
            r = requests.get(url, timeout=5)
        print(f"  {name}: {r.status_code} - {r.text[:80]}")
    except Exception as e:
        err = str(e)
        if "ProxyError" in err or "Connection" in err:
            print(f"  {name}: BLOCKED")
        else:
            print(f"  {name}: ERROR - {err[:80]}")
PYRPC'

echo ""
echo "--- Running test ---"
docker exec traderjoes-bot python3 /app/test_rpc.py

echo ""
echo "=== Done ==="
