#!/usr/bin/env bash
cd /root/trading-bot

# Fix the indentation: replace the badly indented V9 exit check block
python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

old = """            push_all_analytics()  # push metrics each cycle
                    # V9 EXIT CHECK
                    try:
                        await check_and_manage_exits(ch)
                    except Exception as eex:
                        log.warning("Exit check error: %s", eex)"""

new = """            push_all_analytics()  # push metrics each cycle
            # V9 EXIT CHECK
            try:
                ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
                if ch:
                    await check_and_manage_exits(ch)
            except Exception as eex:
                log.warning("Exit check error: %s", eex)"""

if old in code:
    code = code.replace(old, new)
    print("Fixed V9 exit check indentation")
else:
    print("Pattern not found — checking alt")
    # Try line by line fix
    lines = code.split("\n")
    for i, line in enumerate(lines):
        if "# V9 EXIT CHECK" in line:
            print(f"Found V9 EXIT CHECK at line {i+1}: '{line}'")
            break

with open("main.py", "w") as f:
    f.write(code)
FIX

echo "Rebuilding..."
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

echo ""
docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""
docker logs traderjoes-bot --tail 5 2>&1
