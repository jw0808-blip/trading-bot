#!/usr/bin/env bash
cd /root/trading-bot

echo "=== Testing reachable endpoints for USDC.e balance ==="

docker exec traderjoes-bot python3 -c '
import requests

wallet = "0xdabb414f8bb481c2c99378d15dbae3808a3fe6f7"
usdce = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
padded = wallet.lower().replace("0x","").zfill(64)
call_data = "0x70a08231" + padded

tests = [
    ("etherscan v2 (no key)", f"https://api.etherscan.io/v2/api?chainid=137&module=account&action=tokenbalance&contractaddress={usdce}&address={wallet}&tag=latest"),
    ("etherscan v2 proxy", f"https://api.etherscan.io/v2/api?chainid=137&module=proxy&action=eth_call&to={usdce}&data={call_data}&tag=latest"),
]

for name, url in tests:
    try:
        r = requests.get(url, timeout=10)
        print(f"  {name}: {r.status_code} - {r.text[:150]}")
    except Exception as e:
        print(f"  {name}: BLOCKED - {str(e)[:80]}")
'

echo ""
echo "=== Done ==="
