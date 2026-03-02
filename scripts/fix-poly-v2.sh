#!/usr/bin/env bash
cd /root/trading-bot

echo "=== Fixing host-side balance checker ==="

# 1. Update the balance script with working RPCs
cat > /root/trading-bot/scripts/poly-balance.sh << 'BALSCRIPT'
#!/usr/bin/env bash
WALLET="dabb414f8bb481c2c99378d15dbae3808a3fe6f7"
USDCE="0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
CALLDATA="0x70a08231000000000000000000000000${WALLET}"
PAYLOAD="{\"jsonrpc\":\"2.0\",\"method\":\"eth_call\",\"params\":[{\"to\":\"${USDCE}\",\"data\":\"${CALLDATA}\"},\"latest\"],\"id\":1}"
OUTFILE="/root/trading-bot/data/poly_cash_balance.txt"

mkdir -p /root/trading-bot/data

for RPC in \
  "https://polygon-bor-rpc.publicnode.com" \
  "https://1rpc.io/matic" \
  "https://polygon.drpc.org" \
  "https://rpc-mainnet.matic.quiknode.pro" \
  "https://polygon.gateway.tenderly.co"
do
    RESULT=$(curl -s -X POST "$RPC" \
        -H "Content-Type: application/json" \
        -d "$PAYLOAD" \
        --connect-timeout 5 --max-time 10 2>/dev/null)

    if echo "$RESULT" | grep -q '"result"'; then
        HEX=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('result','0x0'))" 2>/dev/null)
        BAL=$(python3 -c "print(int('$HEX', 16) / 1e6)" 2>/dev/null)
        if [ -n "$BAL" ] && [ "$BAL" != "0.0" ]; then
            echo "$BAL" > "$OUTFILE"
            exit 0
        fi
    fi
done
BALSCRIPT

chmod +x /root/trading-bot/scripts/poly-balance.sh

# 2. Run it now
mkdir -p /root/trading-bot/data
bash /root/trading-bot/scripts/poly-balance.sh
echo "Balance file contents:"
cat /root/trading-bot/data/poly_cash_balance.txt

# 3. Make sure data dir is mounted in docker-compose
if ! grep -q "./data:/app/data" docker-compose.yml; then
    echo "Adding data volume mount to docker-compose.yml..."
    # Find the volumes section under traderjoes-bot service and add the mount
    python3 -c "
import re
with open('docker-compose.yml', 'r') as f:
    content = f.read()

# Check if there's already a volumes section with other mounts
if './data:/app/data' not in content:
    # Add after existing volume mount line
    if '- ./.env:/app/.env' in content:
        content = content.replace('- ./.env:/app/.env', '- ./.env:/app/.env\n      - ./data:/app/data')
    elif 'volumes:' in content:
        content = content.replace('volumes:', 'volumes:\n      - ./data:/app/data', 1)
    with open('docker-compose.yml', 'w') as f:
        f.write(content)
    print('Added data volume mount')
else:
    print('Data volume mount already exists')
"
fi

# 4. Rebuild with the volume mount
echo ""
echo "=== Rebuilding ==="
docker compose down traderjoes-bot 2>&1 | tail -3
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

# 5. Verify the file is visible inside container
echo ""
echo "--- File visible in container? ---"
docker exec traderjoes-bot cat /app/data/poly_cash_balance.txt 2>&1

echo ""
echo "--- Balance logs ---"
docker logs traderjoes-bot 2>&1 | grep -i 'polymarket\|USDC\|balance file\|cash\|final' | tail -10

echo ""
echo "=== Done! Test with !portfolio ==="
