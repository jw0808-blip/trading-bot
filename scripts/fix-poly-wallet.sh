#!/usr/bin/env bash
cd /root/trading-bot

echo "=== Testing the CORRECT wallet address ==="
WALLET="0xdabb414f8bb481c2c99378d15dbae3808a3fe6f7"

echo "Test 1: Positions for $WALLET"
curl -s "https://data-api.polymarket.com/positions?user=${WALLET}&sizeThreshold=0.01" | python3 -c "
import json,sys
data=json.load(sys.stdin)
if isinstance(data,list):
    print(f'  Found {len(data)} positions')
    total=0
    for p in data:
        s=float(p.get('size',0)); pr=float(p.get('curPrice',0)); v=s*pr
        if v>0.01: total+=v; print(f'    {p.get(\"outcome\",\"?\")} {p.get(\"title\",\"?\")[:40]} = \${v:.2f}')
    print(f'  Total position value: \${total:.2f}')
" 2>/dev/null

echo ""
echo "Test 2: Value for $WALLET"
curl -s "https://data-api.polymarket.com/value?user=${WALLET}" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(f'  Raw: {str(data)[:500]}')
" 2>/dev/null

echo ""
echo "Test 3: On-chain USDC.e for $WALLET"
ADDR_PADDED=$(echo "${WALLET}" | sed 's/0x//' | sed 's/^/000000000000000000000000/')
curl -s -X POST https://polygon-rpc.com -H "Content-Type: application/json" -d "{\"jsonrpc\":\"2.0\",\"method\":\"eth_call\",\"params\":[{\"to\":\"0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174\",\"data\":\"0x70a08231${ADDR_PADDED}\"},\"latest\"],\"id\":1}" | python3 -c "
import json,sys
data=json.load(sys.stdin)
result=data.get('result','0x0')
raw=int(result,16)
bal=raw/1_000_000
print(f'  USDC.e balance: \${bal:.2f}')
" 2>/dev/null

echo ""
echo "Test 4: On-chain native USDC for $WALLET"
curl -s -X POST https://polygon-rpc.com -H "Content-Type: application/json" -d "{\"jsonrpc\":\"2.0\",\"method\":\"eth_call\",\"params\":[{\"to\":\"0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359\",\"data\":\"0x70a08231${ADDR_PADDED}\"},\"latest\"],\"id\":1}" | python3 -c "
import json,sys
data=json.load(sys.stdin)
result=data.get('result','0x0')
raw=int(result,16)
bal=raw/1_000_000
print(f'  USDC balance: \${bal:.2f}')
" 2>/dev/null

echo ""
echo "Test 5: Activity for $WALLET"
curl -s "https://data-api.polymarket.com/activity?user=${WALLET}&limit=3" | python3 -c "
import json,sys
data=json.load(sys.stdin)
if isinstance(data,list):
    print(f'  Found {len(data)} activities')
    for a in data[:3]:
        print(f'    {a.get(\"type\",\"?\")} amt={a.get(\"amount\",\"?\")} {a.get(\"title\",a.get(\"asset\",\"?\"))[:40]}')
" 2>/dev/null

echo ""
echo "============================================"
