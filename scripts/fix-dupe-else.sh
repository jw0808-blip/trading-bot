#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

# Remove the duplicate broken else block
old = '''                exec_msg = "Polymarket not configured. Add POLYMARKET_PK to .env."
                else:
                    exec_msg = f"Polymarket not configured. Add POLYMARKET_PK to .env."'''

new = '''                exec_msg = "Polymarket not configured. Add POLYMARKET_PK to .env."'''

if old in code:
    code = code.replace(old, new)
    print("Removed duplicate else block")
else:
    print("Pattern not found")

with open("main.py", "w") as f:
    f.write(code)
FIX

docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18
docker logs traderjoes-bot --tail 3 2>&1
