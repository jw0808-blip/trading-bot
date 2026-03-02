#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIXKALSHI'
with open("main.py", "r") as f:
    code = f.read()

old = '''            "count": count,
        }'''

new = '''            "count": count,
            "yes_price_dollars": 0.99,
        }'''

# Only replace the first occurrence (in Kalshi function)
if old in code:
    code = code.replace(old, new, 1)
    print("Added yes_price_dollars to Kalshi order")
else:
    print("Pattern not found")

with open("main.py", "w") as f:
    f.write(code)
FIXKALSHI

echo "Rebuilding..."
docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18

echo ""
docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""
docker logs traderjoes-bot --tail 3 2>&1
echo ""
grep -A 12 "Build order" main.py | head -14
