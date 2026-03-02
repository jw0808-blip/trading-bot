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
