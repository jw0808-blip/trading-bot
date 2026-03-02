#!/usr/bin/env bash
# Fix Polymarket balance: check USDC.e at proxy wallet address
set -uo pipefail
cd /root/trading-bot

echo "=== Fixing Polymarket cash balance ==="

# The $1,998 is USDC.e (0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174)
# at the proxy wallet (0xdabb414f8bb481c2c99378d15dbae3808a3fe6f7)
# The bot was only checking USDC native or the wrong address.

# Find and update get_polymarket_clob_balance to check USDC.e at proxy wallet
python3 << 'PYEOF'
with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

# Find the get_polymarket_clob_balance function and replace it
old_func_start = "def get_polymarket_clob_balance():"
if old_func_start not in code:
    print("ERROR: get_polymarket_clob_balance not found!")
    exit(1)

# Find the full function (up to the next def or class at same indent level)
start_idx = code.index(old_func_start)
# Find end of function - next function def at same indent level
search_from = start_idx + len(old_func_start)
next_def = code.find("\ndef get_polymarket_balance(", search_from)
if next_def == -1:
    next_def = code.find("\ndef ", search_from + 100)
if next_def == -1:
    print("ERROR: Could not find end of function")
    exit(1)

old_func = code[start_idx:next_def]
print(f"Found function ({len(old_func)} chars)")
print(f"First 200 chars: {old_func[:200]}")

new_func = '''def get_polymarket_clob_balance():
    """Get Polymarket balance: USDC.e on-chain + position values from Data API."""
    import requests
    from web3 import Web3

    cash = 0.0
    positions_val = 0.0
    proxy_wallet = os.environ.get("POLYMARKET_FUNDER", os.environ.get("POLY_WALLET_ADDRESS", "")).strip()

    if not proxy_wallet:
        log.warning("No Polymarket wallet address configured")
        return 0.0

    # --- 1. Cash: USDC.e balance at proxy wallet (on-chain) ---
    try:
        POLYGON_RPC = "https://polygon-rpc.com"
        USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf(address) = 0x70a08231 + padded address
        padded = proxy_wallet.lower().replace("0x", "").zfill(64)
        call_data = "0x70a08231" + padded

        rpc_payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": USDC_E, "data": call_data}, "latest"],
            "id": 1,
        }
        r = requests.post(POLYGON_RPC, json=rpc_payload, timeout=10)
        if r.status_code == 200:
            result_hex = r.json().get("result", "0x0")
            raw = int(result_hex, 16)
            cash = raw / 1e6  # USDC.e has 6 decimals
            log.info("Polymarket USDC.e on-chain: $%.2f (wallet=%s)", cash, proxy_wallet[:10])
    except Exception as exc:
        log.warning("Polymarket USDC.e check error: %s", exc)

    # --- 2. Positions: Data API ---
    try:
        r2 = requests.get(
            f"https://data-api.polymarket.com/value?user={proxy_wallet}",
            timeout=10,
        )
        if r2.status_code == 200:
            val = r2.json()
            if isinstance(val, (int, float)):
                positions_val = float(val)
            elif isinstance(val, dict):
                positions_val = float(val.get("value", 0))
            log.info("Polymarket positions value: $%.2f", positions_val)
    except Exception as exc:
        log.warning("Polymarket positions check error: %s", exc)

    total = cash + positions_val
    log.info("Polymarket final: cash=$%.2f positions=$%.2f total=$%.2f", cash, positions_val, total)
    return total

'''

code = code[:start_idx] + new_func + code[next_def:]

# Also update get_polymarket_balance to use the new function and show cash breakdown
# Find where portfolio displays Polymarket
old_display = 'poly_bal = get_polymarket_balance()'
if old_display in code:
    # The display function should call our CLOB balance instead
    code = code.replace(old_display, 'poly_bal = get_polymarket_clob_balance()')
    print("Updated portfolio to use get_polymarket_clob_balance()")

with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)

print("\n[OK] Polymarket balance function updated")
print("  - Checks USDC.e at proxy wallet via Polygon RPC")
print("  - Checks position values via Data API")
print("  - Returns cash + positions total")
PYEOF

echo ""
echo "=== Rebuilding ==="
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

echo ""
echo "--- Bot Logs ---"
docker logs traderjoes-bot --tail 10 2>&1

echo ""
echo "--- Polymarket Balance Check ---"
docker logs traderjoes-bot 2>&1 | grep -i 'polymarket\|USDC\.e\|cash\|final' | tail -10

echo ""
echo "=== Done ==="
echo "Test with !portfolio in Discord"
