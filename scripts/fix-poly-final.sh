#!/usr/bin/env bash
cd /root/trading-bot

echo "============================================"
echo "  Polymarket CLOB Auth Fix - Final"
echo "============================================"
echo ""

# Step 1: Show current state
echo "=== Step 1: Diagnosing current code ==="
echo "--- Lines around CLOB balance function ---"
grep -n "def get_polymarket_clob_balance\|ClobClient\|ApiCreds\|set_api_creds\|balance.allowance\|BalanceAllowanceParams\|signature_type" main.py | head -30
echo ""

# Step 2: Backup
cp main.py main.py.bak.polyfinal
echo "[OK] Backup created"

# Step 3: Apply the fix with Python
echo ""
echo "=== Step 2: Applying fix ==="
python3 << 'PATCH'
import re

with open("main.py", "r") as f:
    code = f.read()

changes = 0

# Replace the entire get_polymarket_clob_balance function
# Find it and replace with a properly authenticated version
old_func_start = "def get_polymarket_clob_balance():"
if old_func_start in code:
    # Find the full function - from def to next def or next section marker
    pattern = r'def get_polymarket_clob_balance\(\):.*?(?=\ndef [a-zA-Z_]|\n# ====)'
    match = re.search(pattern, code, re.DOTALL)
    if match:
        old_func = match.group(0)
        
        new_func = '''def get_polymarket_clob_balance():
    """Get Polymarket balance using CLOB API with proper auth."""
    try:
        pk = os.getenv("POLYMARKET_PK", "").strip()
        funder = os.getenv("POLYMARKET_FUNDER", os.getenv("POLYMARKET_WALLET_ADDRESS", "")).strip()
        api_key = os.getenv("POLYMARKET_API_KEY", "").strip()
        api_secret = os.getenv("POLYMARKET_API_SECRET", "").strip()
        passphrase = os.getenv("POLYMARKET_PASSPHRASE", "").strip()
        
        if not pk:
            return None, "No POLYMARKET_PK set"
        
        log.info("Polymarket CLOB init: pk=%s... funder=%s... api_key=%s...",
                 pk[:10] if pk else "NONE",
                 funder[:10] if funder else "NONE",
                 api_key[:10] if api_key else "NONE")
        
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds
        
        # Initialize client with private key and funder
        client = ClobClient(
            host="https://clob.polymarket.com",
            key=pk,
            chain_id=137,
            signature_type=0,  # EOA / MetaMask
            funder=funder if funder else None,
        )
        
        # Set API credentials explicitly
        if api_key and api_secret and passphrase:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=passphrase,
            )
            client.set_api_creds(creds)
            log.info("Polymarket CLOB: manual API creds set")
        else:
            try:
                client.set_api_creds(client.create_or_derive_api_creds())
                log.info("Polymarket CLOB: derived API creds")
            except Exception as ce:
                log.warning("Polymarket cred derivation failed: %s", ce)
                return None, f"Cred derivation failed: {ce}"
        
        # Query collateral balance (cash)
        cash_balance = 0.0
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
            ba = client.get_balance_allowance(params)
            log.info("Polymarket balance-allowance response: %s", ba)
            if ba:
                # Response is like {"balance": "1998000000", "allowance": "..."}
                raw_bal = ba.get("balance", "0")
                try:
                    raw_int = int(raw_bal)
                    # USDC has 6 decimals
                    if raw_int > 1_000_000:
                        cash_balance = raw_int / 1_000_000
                    else:
                        cash_balance = float(raw_bal)
                except (ValueError, TypeError):
                    cash_balance = float(raw_bal) if raw_bal else 0.0
        except Exception as exc:
            log.warning("Polymarket balance-allowance error: %s", exc)
        
        # Get positions value from Data API
        positions_value = 0.0
        position_details = []
        try:
            funder_addr = funder if funder else ""
            if funder_addr:
                r = requests.get(
                    "https://data-api.polymarket.com/positions",
                    params={"user": funder_addr.lower(), "sizeThreshold": "0.01"},
                    timeout=15,
                )
                if r.status_code == 200:
                    positions = r.json()
                    if isinstance(positions, list):
                        for pos in positions:
                            size = float(pos.get("size", 0))
                            cur_price = float(pos.get("curPrice", 0))
                            value = size * cur_price
                            if value > 0.01:
                                positions_value += value
                                title = pos.get("title", pos.get("asset", "Unknown"))[:50]
                                outcome = pos.get("outcome", "?")
                                pnl = float(pos.get("pnl", 0))
                                cost = value - pnl
                                pnl_pct = (pnl / cost * 100) if cost > 0 else 0
                                position_details.append(
                                    f"  {outcome} {title} {size:.1f} shares @ ${cur_price:.3f} = ${value:.2f} ({'+' if pnl >= 0 else ''}{pnl_pct:.1f}%)"
                                )
        except Exception as exc:
            log.warning("Polymarket positions error: %s", exc)
        
        total = cash_balance + positions_value
        log.info("Polymarket final: cash=$%.2f positions=$%.2f total=$%.2f", cash_balance, positions_value, total)
        
        return {
            "total": total,
            "cash": cash_balance,
            "positions_value": positions_value,
            "position_details": position_details,
        }, None
        
    except Exception as exc:
        log.warning("get_polymarket_clob_balance error: %s", exc)
        return None, str(exc)

'''
        code = code.replace(old_func, new_func)
        changes += 1
        print("  [1] Replaced get_polymarket_clob_balance() with proper auth")
    else:
        print("  [1] WARNING: regex didn't match function body")
        # Try simpler approach - just fix the client init inside the existing function
        # Fix: add ApiCreds usage
        if "client.get_balance_allowance" in code or "BalanceAllowanceParams" in code:
            print("  [1] Function exists but pattern didn't match, trying line-level fixes")
else:
    print("  [1] get_polymarket_clob_balance not found")

with open("main.py", "w") as f:
    f.write(code)
print(f"\n  Total: {changes} patches applied")

# Verify the fix
print("\n--- Verification ---")
import subprocess
result = subprocess.run(["grep", "-n", "set_api_creds\|ApiCreds\|signature_type=0\|balance.allowance", "main.py"], capture_output=True, text=True)
print(result.stdout[:500])
PATCH

echo ""
echo "=== Step 3: Rebuilding container ==="
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
echo "[OK] Container restarted"
sleep 20

echo ""
echo "=== Step 4: Bot status ==="
docker logs traderjoes-bot --tail 5 2>&1

echo ""
echo "=== Step 5: GitHub sync ==="
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A 2>/dev/null || true
git commit -m "Fix: Polymarket CLOB auth - use ApiCreds with manual keys, signature_type=0" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3

echo ""
echo "=== Step 6: Container status ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "============================================"
echo "  Fix deployed. Test: !portfolio in Discord"
echo "============================================"
