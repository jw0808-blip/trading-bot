#!/usr/bin/env bash
cd /root/trading-bot

echo "=== Polymarket Balance Fix - Profile API approach ==="
cp main.py main.py.bak.polyprofile

# First, let's discover what APIs actually work for this wallet
echo "--- Testing Polymarket APIs for funder address ---"
FUNDER=$(grep "^POLYMARKET_FUNDER=" .env | cut -d= -f2)
echo "Funder: $FUNDER"

echo ""
echo "Test 1: data-api.polymarket.com/positions"
curl -s "https://data-api.polymarket.com/positions?user=${FUNDER,,}&sizeThreshold=0.01" | python3 -c "
import json,sys
data=json.load(sys.stdin)
if isinstance(data,list):
    print(f'  Found {len(data)} positions')
    total=0
    for p in data:
        s=float(p.get('size',0)); pr=float(p.get('curPrice',0)); v=s*pr
        if v>0.01: total+=v; print(f'    {p.get(\"outcome\",\"?\")} {p.get(\"title\",\"?\")[:40]} = \${v:.2f}')
    print(f'  Total position value: \${total:.2f}')
else:
    print(f'  Response: {str(data)[:200]}')
" 2>/dev/null || echo "  Failed"

echo ""
echo "Test 2: data-api.polymarket.com/profile (user)"
curl -s "https://data-api.polymarket.com/profile?user=${FUNDER,,}" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(f'  Response type: {type(data).__name__}')
if isinstance(data,dict):
    for k,v in data.items():
        if 'bal' in k.lower() or 'cash' in k.lower() or 'collateral' in k.lower() or 'portfolio' in k.lower() or 'value' in k.lower():
            print(f'  {k}: {v}')
    print(f'  All keys: {list(data.keys())[:20]}')
print(f'  Raw: {str(data)[:300]}')
" 2>/dev/null || echo "  Failed"

echo ""
echo "Test 3: gamma-api.polymarket.com/users (address)"
curl -s "https://gamma-api.polymarket.com/users/?address=${FUNDER,,}" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(f'  Response type: {type(data).__name__}')
if isinstance(data,list) and len(data)>0:
    for item in data:
        print(f'  Keys: {list(item.keys())[:20]}')
        for k,v in item.items():
            if 'bal' in k.lower() or 'cash' in k.lower() or 'collateral' in k.lower() or 'portfolio' in k.lower() or 'value' in k.lower() or 'deposit' in k.lower():
                print(f'  {k}: {v}')
        print(f'  Raw: {str(item)[:300]}')
elif isinstance(data,dict):
    print(f'  Keys: {list(data.keys())[:20]}')
    print(f'  Raw: {str(data)[:300]}')
" 2>/dev/null || echo "  Failed"

echo ""
echo "Test 4: strapi-matic.poly.market/profiles"
curl -s "https://strapi-matic.poly.market/profiles/${FUNDER,,}" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(f'  Response type: {type(data).__name__}')
if isinstance(data,dict):
    for k,v in data.items():
        print(f'  {k}: {v}')
print(f'  Raw: {str(data)[:500]}')
" 2>/dev/null || echo "  Failed"

echo ""
echo "Test 5: data-api.polymarket.com/value"
curl -s "https://data-api.polymarket.com/value?user=${FUNDER,,}" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(f'  Raw: {str(data)[:500]}')
" 2>/dev/null || echo "  Failed"

echo ""
echo "Test 6: data-api.polymarket.com/activity"  
curl -s "https://data-api.polymarket.com/activity?user=${FUNDER,,}&limit=3" | python3 -c "
import json,sys
data=json.load(sys.stdin)
if isinstance(data,list):
    print(f'  Found {len(data)} activities')
    for a in data[:3]:
        print(f'    {a.get(\"type\",\"?\")} {a.get(\"asset\",\"?\")[:30]} amt={a.get(\"amount\",\"?\")}')
elif isinstance(data,dict):
    print(f'  Raw: {str(data)[:300]}')
" 2>/dev/null || echo "  Failed"

echo ""
echo "Test 7: POLY_WALLET_ADDRESS on-chain check"
WALLET=$(grep "^POLY_WALLET_ADDRESS=" .env | cut -d= -f2)
echo "  POLY_WALLET_ADDRESS=$WALLET"
echo "  POLYMARKET_FUNDER=$FUNDER"
if [ "$WALLET" != "$FUNDER" ]; then
    echo "  NOTE: These are DIFFERENT addresses!"
fi

echo ""
echo "Test 8: On-chain USDC.e for FUNDER address"
ADDR_PADDED=$(echo "${FUNDER,,}" | sed 's/0x//' | sed 's/^/000000000000000000000000/')
curl -s -X POST https://polygon-rpc.com -H "Content-Type: application/json" -d "{\"jsonrpc\":\"2.0\",\"method\":\"eth_call\",\"params\":[{\"to\":\"0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174\",\"data\":\"0x70a08231${ADDR_PADDED}\"},\"latest\"],\"id\":1}" | python3 -c "
import json,sys
data=json.load(sys.stdin)
result=data.get('result','0x0')
raw=int(result,16)
bal=raw/1_000_000
print(f'  USDC.e on-chain for funder: \${bal:.2f}')
" 2>/dev/null || echo "  Failed"

echo ""
echo "============================================"
echo "  API Discovery Complete"
echo "============================================"
echo "  Review output above to find which API"
echo "  returns the ~\$1,998 cash balance"
echo "============================================"
