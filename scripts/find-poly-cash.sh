#!/usr/bin/env bash
cd /root/trading-bot

echo "=== CLOB API is authenticated! Finding the $1,998 ==="

# The CLOB client works now. Let's try different balance queries.
docker exec traderjoes-bot python3 << 'PYEOF'
import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

pk = os.environ.get("POLYMARKET_PK", "").strip()
funder = os.environ.get("POLYMARKET_FUNDER", "").strip()
api_key = os.environ.get("POLYMARKET_API_KEY", "").strip()
api_secret = os.environ.get("POLYMARKET_API_SECRET", "").strip()
passphrase = os.environ.get("POLYMARKET_PASSPHRASE", "").strip()

print(f"PK: {pk[:10]}...")
print(f"Funder: {funder}")
print(f"API Key: {api_key[:10]}...")

client = ClobClient(
    host="https://clob.polymarket.com",
    key=pk,
    chain_id=137,
    signature_type=0,
    funder=funder,
)

creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)
client.set_api_creds(creds)

print("\n--- Test 1: COLLATERAL balance ---")
try:
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
    result = client.get_balance_allowance(params)
    print(f"  Result: {result}")
except Exception as e:
    print(f"  Error: {e}")

print("\n--- Test 2: CONDITIONAL balance ---")
try:
    params2 = BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, signature_type=0)
    result2 = client.get_balance_allowance(params2)
    print(f"  Result: {result2}")
except Exception as e:
    print(f"  Error: {e}")

print("\n--- Test 3: Get open orders ---")
try:
    orders = client.get_orders()
    print(f"  Orders: {orders}")
except Exception as e:
    print(f"  Error: {e}")

print("\n--- Test 4: Get trades ---")
try:
    trades = client.get_trades()
    print(f"  Trades count: {len(trades) if isinstance(trades, list) else trades}")
    if isinstance(trades, list) and len(trades) > 0:
        print(f"  Last trade: {trades[0]}")
except Exception as e:
    print(f"  Error: {e}")

print("\n--- Test 5: Check all client methods ---")
methods = [m for m in dir(client) if not m.startswith('_') and callable(getattr(client, m, None))]
balance_methods = [m for m in methods if 'bal' in m.lower() or 'fund' in m.lower() or 'account' in m.lower() or 'portfolio' in m.lower() or 'collateral' in m.lower()]
print(f"  Balance-related methods: {balance_methods}")
print(f"  All methods: {methods}")

print("\n--- Test 6: Direct API call to /profile ---")
import requests
try:
    # Try the profile endpoint with auth headers
    from py_clob_client.headers.headers import create_level_2_headers
    headers = create_level_2_headers(client.signer, client.creds)
    r = requests.get("https://clob.polymarket.com/profile", headers=headers, timeout=15)
    print(f"  Status: {r.status_code}")
    print(f"  Response: {r.text[:500]}")
except Exception as e:
    print(f"  Error: {e}")

PYEOF

echo ""
echo "============================================"
