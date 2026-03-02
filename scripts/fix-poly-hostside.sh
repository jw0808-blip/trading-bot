#!/usr/bin/env bash
# Setup: Host-side balance checker that writes to a file mounted into Docker
cd /root/trading-bot

echo "=== Setting up host-side Polymarket balance checker ==="

# 1. Create the balance checker script (runs on HOST, not in container)
cat > /root/trading-bot/scripts/poly-balance.sh << 'BALSCRIPT'
#!/usr/bin/env bash
# Queries Polygon RPC for USDC.e balance of Polymarket proxy wallet
# Runs on the HOST (outside Docker) where RPC is not blocked

WALLET="0xdabb414f8bb481c2c99378d15dbae3808a3fe6f7"
USDCE="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
PADDED=$(echo "$WALLET" | tr '[:upper:]' '[:lower:]' | sed 's/0x//' | sed 's/^/0000000000000000000000000000000000000000000000000000000000000000/' | tail -c 64)
CALLDATA="0x70a08231000000000000000000000000${WALLET#0x}"
OUTFILE="/root/trading-bot/data/poly_cash_balance.txt"

# Try multiple RPCs
for RPC in "https://polygon-rpc.com" "https://rpc.ankr.com/polygon" "https://polygon.llamarpc.com"; do
    RESULT=$(curl -s -X POST "$RPC" \
        -H "Content-Type: application/json" \
        -d "{\"jsonrpc\":\"2.0\",\"method\":\"eth_call\",\"params\":[{\"to\":\"$USDCE\",\"data\":\"$CALLDATA\"},\"latest\"],\"id\":1}" \
        --connect-timeout 5 --max-time 10 2>/dev/null)
    
    HEX=$(echo "$RESULT" | grep -o '"result":"[^"]*"' | cut -d'"' -f4)
    
    if [ -n "$HEX" ] && [ "$HEX" != "0x" ]; then
        # Convert hex to decimal, divide by 1e6
        DEC=$(python3 -c "print(int('$HEX', 16) / 1e6)")
        echo "$DEC" > "$OUTFILE"
        echo "$(date): USDC.e balance = \$$DEC (via $RPC)" >> /root/trading-bot/data/poly_balance.log
        exit 0
    fi
done

echo "$(date): Failed to fetch balance from all RPCs" >> /root/trading-bot/data/poly_balance.log
BALSCRIPT

chmod +x /root/trading-bot/scripts/poly-balance.sh

# 2. Run it once now to populate the file
echo "--- Running balance check now ---"
bash /root/trading-bot/scripts/poly-balance.sh
echo "Balance file:"
cat /root/trading-bot/data/poly_cash_balance.txt 2>/dev/null || echo "NOT CREATED"

# 3. Install cron job (every 5 minutes, alongside watchdog)
(crontab -l 2>/dev/null | grep -v "poly-balance.sh"; echo "*/5 * * * * /root/trading-bot/scripts/poly-balance.sh") | crontab -
echo "Cron installed"

# 4. Update _get_polymarket_clob_balance to read from file
echo ""
echo "--- Updating bot to read balance from file ---"

python3 << 'PYEOF'
with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

old_func = '''def _get_polymarket_clob_balance():
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

new_func = '''def _get_polymarket_clob_balance():
    """Get USDC.e cash balance from host-side balance file."""
    try:
        bal_file = "/app/data/poly_cash_balance.txt"
        if os.path.exists(bal_file):
            with open(bal_file, "r") as f:
                val = f.read().strip()
            if val:
                bal = float(val)
                log.info("Polymarket USDC.e from balance file: $%.2f", bal)
                return bal
        log.warning("Polymarket balance file not found or empty")
        return 0.0
    except Exception as exc:
        log.warning("Polymarket balance file error: %s", exc)
        return 0.0'''

if old_func in code:
    code = code.replace(old_func, new_func)
    print("[OK] Replaced with file-based balance reader")
else:
    # Try to find and replace by function name
    lines = code.split('\n')
    start = None
    end = None
    for i, line in enumerate(lines):
        if 'def _get_polymarket_clob_balance():' in line:
            start = i
        elif start is not None and i > start + 1 and (line.startswith('def ') or line.startswith('class ')):
            end = i
            break
    if start is not None and end is not None:
        new_lines = lines[:start] + new_func.split('\n') + [''] + lines[end:]
        code = '\n'.join(new_lines)
        print(f"[OK] Replaced lines {start}-{end} with file-based reader")
    else:
        print("[ERROR] Could not find function")
        exit(1)

with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)
PYEOF

# 5. Rebuild
echo ""
echo "=== Rebuilding ==="
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

echo ""
echo "--- Balance logs ---"
docker logs traderjoes-bot 2>&1 | grep -i 'polymarket\|USDC\|balance file\|cash\|final\|clob' | tail -10

echo ""
echo "=== Done! Test with !portfolio ==="
