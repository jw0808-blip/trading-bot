#!/usr/bin/env bash
cd /root/trading-bot

echo "=== Adding missing get_polymarket_clob_balance function ==="

python3 << 'PATCH'
with open("main.py", "r") as f:
    code = f.read()

changes = 0

if "def get_polymarket_clob_balance" not in code:
    # Insert the function right before get_polymarket_balance
    func_code = '''

def get_polymarket_clob_balance():
    """Get Polymarket balance from data API (deposited cash + positions)."""
    try:
        funder = POLYMARKET_FUNDER or POLY_WALLET_ADDRESS
        if not funder:
            return None, "No funder address"
        
        funder_lower = funder.lower()
        
        # 1. Get positions value
        positions_value = 0.0
        position_details = []
        try:
            r = requests.get(
                "https://data-api.polymarket.com/positions",
                params={"user": funder_lower, "sizeThreshold": "0.01"},
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
            log.warning("Polymarket positions fetch error: %s", exc)
        
        # 2. Get cash balance - check on-chain USDC.e for the funder address
        cash_balance = 0.0
        usdc_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        addr_padded = funder_lower.replace("0x", "").zfill(64)
        call_data = "0x70a08231" + addr_padded
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdc_e, "data": call_data}, "latest"],
            "id": 1,
        }
        for rpc in ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]:
            try:
                r = requests.post(rpc, json=payload, timeout=10)
                if r.status_code == 200:
                    result = r.json().get("result", "0x0")
                    if result and result != "0x":
                        raw = int(result, 16)
                        cash_balance = raw / 1_000_000
                        break
            except Exception:
                continue
        
        # 3. Also check native USDC
        usdc_native = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"
        payload2 = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": usdc_native, "data": call_data}, "latest"],
            "id": 1,
        }
        for rpc in ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]:
            try:
                r = requests.post(rpc, json=payload2, timeout=10)
                if r.status_code == 200:
                    result = r.json().get("result", "0x0")
                    if result and result != "0x":
                        raw = int(result, 16)
                        cash_balance += raw / 1_000_000
                        break
            except Exception:
                continue
        
        # 4. Try Polymarket profile API for internal cash balance
        try:
            r = requests.get(
                f"https://gamma-api.polymarket.com/balances",
                params={"user": funder_lower},
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    api_bal = float(data.get("balance", data.get("cashBalance", data.get("availableBalance", 0))))
                    if api_bal > cash_balance:
                        cash_balance = api_bal
                elif isinstance(data, list) and len(data) > 0:
                    for item in data:
                        if isinstance(item, dict):
                            b = float(item.get("balance", item.get("amount", 0)))
                            if b > 0:
                                cash_balance = max(cash_balance, b)
        except Exception:
            pass
        
        # 5. Try strapi profile endpoint
        try:
            r = requests.get(
                f"https://strapi-matic.poly.market/profiles/{funder_lower}",
                timeout=15,
            )
            if r.status_code == 200:
                data = r.json()
                if isinstance(data, dict):
                    profile_bal = float(data.get("collateralBalance", data.get("balance", 0)))
                    # Polymarket stores this in raw units (6 decimals for USDC)
                    if profile_bal > 1_000_000:
                        profile_bal = profile_bal / 1_000_000
                    if profile_bal > cash_balance:
                        cash_balance = profile_bal
                        log.info("Polymarket profile balance: $%.2f", profile_bal)
        except Exception:
            pass
        
        total = cash_balance + positions_value
        log.info("Polymarket CLOB balance: cash=$%.2f positions=$%.2f total=$%.2f", cash_balance, positions_value, total)
        
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

    # Insert right before def get_polymarket_balance
    marker = "def get_polymarket_balance():"
    if marker in code:
        code = code.replace(marker, func_code + marker)
        changes += 1
        print("  [1] get_polymarket_clob_balance() inserted before get_polymarket_balance()")
    else:
        print("  [1] ERROR: Could not find get_polymarket_balance marker")
else:
    print("  [1] get_polymarket_clob_balance() already exists")

with open("main.py", "w") as f:
    f.write(code)
print(f"\n  [OK] {changes} patches applied")
PATCH

echo ""
echo "=== Rebuilding ==="
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

echo ""
echo "--- Bot Logs ---"
docker logs traderjoes-bot --tail 5 2>&1

echo ""
echo "=== GitHub Sync ==="
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A 2>/dev/null || true
git commit -m "Fix: Add missing get_polymarket_clob_balance function for correct portfolio balance" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3
echo "  [OK] Done"
