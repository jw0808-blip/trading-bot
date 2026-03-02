#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

old = 'async def dry_run_cmd(ctx, action: str = ""):'
new = '@bot.command(name="dry-run")\nasync def dry_run_cmd(ctx, action: str = ""):'

if old in code and '@bot.command(name="dry-run")' not in code:
    code = code.replace(old, new, 1)
    print("Added @bot.command decorator for dry-run")
else:
    print("Already has decorator or not found")

with open("main.py", "w") as f:
    f.write(code)
FIX

docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18
docker logs traderjoes-bot --tail 3 2>&1
