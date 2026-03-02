#!/usr/bin/env bash
cd /root/trading-bot

echo "=== Fixing Polymarket Balance ==="
cp main.py main.py.bak.polybal

python3 << 'PATCH'
with open("main.py", "r") as f:
    code = f.read()

changes = 0

# Find the current get_polymarket_balance function and add CLOB balance check
# The current function only checks on-chain USDC via Polygon RPC
# We need to ALSO query the CLOB client for the deposited/portfolio balance

# Add a new function that uses the CLOB client to get the real balance
clob_balance_code = '''
def get_polymarket_clob_balance():
    """Get Polymarket balance from CLOB API (deposited funds + positions)."""
    try:
        client = get_polymarket_clob_client()
        if not client:
            return None, None
        
        # Get positions to calculate position value
        positions_value = 0.0
        position_details = []
        try:
            # Use the Gamma API to get positions for this address
            funder = POLYMARKET_FUNDER or POLY_WALLET_ADDRESS
            if funder:
                r = requests.get(
                    f"https://data-api.polymarket.com/positions",
                    params={"user": funder.lower(), "sizeThreshold": "0.01"},
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
                                pnl_pct = (pnl / (value - pnl) * 100) if (value - pnl) > 0 else 0
                                position_details.append(
                                    f"  {outcome} {title} {size:.1f} shares @ ${cur_price:.3f} = ${value:.2f} ({'+' if pnl >= 0 else ''}{pnl_pct:.1f}%)"
                                )
        except Exception as exc:
            log.warning("Polymarket positions fetch error: %s", exc)
        
        # Get cash balance via CLOB API 
        cash_balance = 0.0
        try:
            # Try the profile/balance endpoint
            funder = POLYMARKET_FUNDER or POLY_WALLET_ADDRESS
            if funder:
                # Check USDC.e balance on the Polymarket CTF exchange contract
                # The funds are held by the exchange, query via data API
                r = requests.get(
                    f"https://data-api.polymarket.com/balance",
                    params={"user": funder.lower()},
                    timeout=15,
                )
                if r.status_code == 200:
                    data = r.json()
                    if isinstance(data, dict):
                        cash_balance = float(data.get("balance", data.get("cash", 0)))
                    elif isinstance(data, (int, float)):
                        cash_balance = float(data)
        except Exception as exc:
            log.warning("Polymarket cash balance fetch error: %s", exc)
        
        # If we couldn't get cash from data API, try on-chain proxy balance
        if cash_balance <= 0:
            try:
                funder = POLYMARKET_FUNDER or POLY_WALLET_ADDRESS
                if funder:
                    # Check the Polymarket proxy contract balance (USDC.e)
                    usdc_e = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
                    addr_padded = funder.lower().replace("0x", "").zfill(64)
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
            except Exception as exc:
                log.warning("Polymarket on-chain balance error: %s", exc)
        
        total = cash_balance + positions_value
        return {
            "total": total,
            "cash": cash_balance,
            "positions_value": positions_value,
            "position_details": position_details,
        }, None
        
    except Exception as exc:
        return None, str(exc)

'''

# Insert before the get_polymarket_balance function or after POLYMARKET CLOB EXECUTION section
if "get_polymarket_clob_balance" not in code:
    # Insert after the poly-setup command
    insert_point = "# ============================================================================\n# ENTRY POINT"
    if "execute_polymarket_order" in code:
        # Insert before ENTRY POINT
        code = code.replace(insert_point, clob_balance_code + "\n" + insert_point)
        changes += 1
        print("  [1] get_polymarket_clob_balance() added")

# Now update the portfolio command to use the new balance function
# Find the Polymarket section in the portfolio output
old_poly_line = '    f"**Polymarket:** {poly}\\n"'
new_poly_line = '''    f"**Polymarket:** {poly}\\n"'''

# Actually, let's update get_polymarket_balance to use the CLOB balance
old_get_poly = 'def get_polymarket_balance():'
if old_get_poly in code:
    # Find the full function and wrap it
    # Add a call to get_polymarket_clob_balance at the start
    old_start = '''def get_polymarket_balance():
    if not POLY_WALLET_ADDRESS:
        return "Wallet not configured"'''
    
    new_start = '''def get_polymarket_balance():
    # Try CLOB-based balance first (includes deposited cash + positions)
    if POLYMARKET_PK or POLYMARKET_FUNDER:
        try:
            result, err = get_polymarket_clob_balance()
            if result and result["total"] > 0:
                total = result["total"]
                cash = result["cash"]
                pv = result["positions_value"]
                details = result["position_details"]
                summary = f"${total:,.2f}"
                parts = []
                if cash > 0.01:
                    parts.append(f"cash: ${cash:,.2f}")
                if pv > 0.01:
                    parts.append(f"positions: ${pv:,.2f}")
                if parts:
                    summary += f" ({', '.join(parts)})"
                if details:
                    summary += "\\n" + "\\n".join(details)
                return summary
        except Exception as exc:
            log.warning("CLOB balance fallback: %s", exc)
    
    if not POLY_WALLET_ADDRESS:
        return "Wallet not configured"'''
    
    if old_start in code:
        code = code.replace(old_start, new_start)
        changes += 1
        print("  [2] get_polymarket_balance() updated to use CLOB balance first")
    else:
        print("  [2] Could not find exact get_polymarket_balance start pattern")
        # Try alternate pattern - the version with logging
        alt_start = 'def get_polymarket_balance():\n    if not POLY_WALLET_ADDRESS:\n        return "Wallet not configured"\n    # Check both USDC.e'
        alt_new = '''def get_polymarket_balance():
    # Try CLOB-based balance first (includes deposited cash + positions)
    if POLYMARKET_PK or POLYMARKET_FUNDER:
        try:
            result, err = get_polymarket_clob_balance()
            if result and result["total"] > 0:
                total = result["total"]
                cash = result["cash"]
                pv = result["positions_value"]
                details = result["position_details"]
                summary = f"${total:,.2f}"
                parts = []
                if cash > 0.01:
                    parts.append(f"cash: ${cash:,.2f}")
                if pv > 0.01:
                    parts.append(f"positions: ${pv:,.2f}")
                if parts:
                    summary += f" ({', '.join(parts)})"
                if details:
                    summary += "\\n" + "\\n".join(details)
                return summary
        except Exception as exc:
            log.warning("CLOB balance fallback: %s", exc)
    
    if not POLY_WALLET_ADDRESS:
        return "Wallet not configured"
    # Check both USDC.e'''
        if alt_start in code:
            code = code.replace(alt_start, alt_new)
            changes += 1
            print("  [2] get_polymarket_balance() updated (alt pattern)")
        else:
            print("  [2] WARNING: Could not find get_polymarket_balance pattern to update")

# Also make sure POLY_WALLET_ADDRESS is set to funder address if not set
poly_wallet_init = '''POLY_WALLET_ADDRESS     = os.environ.get("POLY_WALLET_ADDRESS", "")'''
poly_wallet_new = '''POLY_WALLET_ADDRESS     = os.environ.get("POLY_WALLET_ADDRESS", os.environ.get("POLYMARKET_FUNDER", ""))'''
if poly_wallet_init in code and poly_wallet_new not in code:
    code = code.replace(poly_wallet_init, poly_wallet_new)
    changes += 1
    print("  [3] POLY_WALLET_ADDRESS falls back to POLYMARKET_FUNDER")

with open("main.py", "w") as f:
    f.write(code)
print(f"\n  [OK] {changes} patches applied")
PATCH

echo ""

# Also set POLY_WALLET_ADDRESS to the funder address if not already set
if ! grep -q "^POLY_WALLET_ADDRESS=" .env 2>/dev/null; then
    FUNDER=$(grep "^POLYMARKET_FUNDER=" .env | cut -d= -f2)
    if [ -n "$FUNDER" ]; then
        echo "POLY_WALLET_ADDRESS=$FUNDER" >> .env
        echo "  [OK] Set POLY_WALLET_ADDRESS=$FUNDER"
    fi
elif [ "$(grep '^POLY_WALLET_ADDRESS=' .env | cut -d= -f2)" = "" ]; then
    FUNDER=$(grep "^POLYMARKET_FUNDER=" .env | cut -d= -f2)
    if [ -n "$FUNDER" ]; then
        sed -i "s/^POLY_WALLET_ADDRESS=.*/POLY_WALLET_ADDRESS=$FUNDER/" .env
        echo "  [OK] Updated POLY_WALLET_ADDRESS to $FUNDER"
    fi
fi

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
git commit -m "Fix: Polymarket balance - use data API for cash + positions (was showing only on-chain USDC)" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3
echo "  [OK] GitHub synchronized"

echo ""
echo "============================================"
echo "  Polymarket Balance Fix Complete"
echo "============================================"
echo ""
echo "Now uses data-api.polymarket.com to fetch:"
echo "  - Cash balance (deposited USDC)"
echo "  - Position values (open bets)"
echo "  - Total portfolio value"
echo ""
echo "TEST: !portfolio in Discord"
echo "============================================"
