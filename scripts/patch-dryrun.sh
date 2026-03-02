#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

old = 'TRADING_MODE    = os.environ.get("TRADING_MODE", "paper")'
new = 'TRADING_MODE    = os.environ.get("TRADING_MODE", "paper")\nDRY_RUN_MODE    = True  # Safe default: dry-run on'

code = code.replace(old, new, 1)
with open("main.py", "w") as f:
    f.write(code)
print("Added DRY_RUN_MODE = True global init")
FIX

docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18
docker logs traderjoes-bot --tail 3 2>&1
