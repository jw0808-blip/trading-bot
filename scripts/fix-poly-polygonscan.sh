#!/usr/bin/env bash
cd /root/trading-bot

echo "=== Fix: Use Polygonscan API for USDC.e balance ==="

# Test polygonscan tokenbalance endpoint from inside container
docker exec traderjoes-bot python3 -c '
import requests
wallet = "0xdabb414f8bb481c2c99378d15dbae3808a3fe6f7"
usdce = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
url = f"https://api.polygonscan.com/api?module=account&action=tokenbalance&contractaddress={usdce}&address={wallet}&tag=latest"
r = requests.get(url, timeout=10)
print("Status:", r.status_code)
print("Response:", r.text[:200])
data = r.json()
if data.get("status") == "1":
    raw = int(data["result"])
    print(f"USDC.e balance: ${raw / 1e6:,.2f}")
else:
    print("Error:", data.get("message"), data.get("result"))
'

echo ""
echo "--- Updating _get_polymarket_clob_balance to use polygonscan ---"

# Replace the _get_polymarket_clob_balance function (line 192) to use polygonscan
python3 << 'PYEOF'
with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

old_func = '''def _get_polymarket_clob_balance():
    """Get deposited USDC balance via py-clob-client."""
    if not POLY_PRIVATE_KEY:
        return 0.0
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType
        client = ClobClient(
            'https://clob.polymarket.com',
            key=POLY_PRIVATE_KEY,
            chain_id=137,
            signature_type=0,
            funder=POLY_WALLET_ADDRESS
        )
        creds = ApiCreds(
            api_key=POLYMARKET_API_KEY,
            api_secret=POLYMARKET_SECRET,
            api_passphrase=POLYMARKET_PASSPHRASE
        )
        client.set_api_creds(creds)
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
        result = client.get_balance_allowance(params)
        raw = int(result.get("balance", 0))
        return raw / 1_000_000
    except Exception as exc:
        log.warning("Polymarket CLOB balance error: %s", exc)
        return 0.0'''

new_func = '''def _get_polymarket_clob_balance():
    """Get USDC.e cash balance via Polygonscan API (works from Docker)."""
    if not POLY_WALLET_ADDRESS:
        return 0.0
    try:
        usdce = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        url = f"https://api.polygonscan.com/api?module=account&action=tokenbalance&contractaddress={usdce}&address={POLY_WALLET_ADDRESS}&tag=latest"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            if data.get("status") == "1":
                raw = int(data["result"])
                bal = raw / 1_000_000
                log.info("Polymarket USDC.e via Polygonscan: $%.2f", bal)
                return bal
            else:
                log.warning("Polygonscan error: %s", data.get("result", "unknown"))
        return 0.0
    except Exception as exc:
        log.warning("Polymarket Polygonscan balance error: %s", exc)
        return 0.0'''

if old_func in code:
    code = code.replace(old_func, new_func)
    print("[OK] Replaced _get_polymarket_clob_balance with Polygonscan version")
else:
    print("[WARN] Exact match not found, trying line-based replacement...")
    lines = code.split('\n')
    start = None
    end = None
    for i, line in enumerate(lines):
        if 'def _get_polymarket_clob_balance():' in line:
            start = i
        elif start is not None and line.startswith('def ') and i > start + 1:
            end = i
            break
    if start is not None and end is not None:
        new_lines = lines[:start] + new_func.split('\n') + lines[end:]
        code = '\n'.join(new_lines)
        print(f"[OK] Replaced lines {start}-{end} with Polygonscan version")
    else:
        print("[ERROR] Could not find function to replace")
        exit(1)

with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)
PYEOF

echo ""
echo "=== Rebuilding ==="
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

echo ""
echo "--- Balance logs ---"
docker logs traderjoes-bot 2>&1 | grep -i 'polymarket\|USDC\|polygonscan\|cash\|final' | tail -10

echo ""
echo "=== Test with !portfolio in Discord ==="
