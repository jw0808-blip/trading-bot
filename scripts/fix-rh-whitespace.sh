#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

# Also need to strip whitespace from env vars when loading
# Check if ROBINHOOD keys have .strip()
if 'ROBINHOOD_API_KEY' in code and '.strip()' not in code.split('ROBINHOOD_API_KEY')[1][:100]:
    # Find the env loading lines
    old_rh_key = 'ROBINHOOD_API_KEY     = os.getenv("ROBINHOOD_API_KEY", "")'
    new_rh_key = 'ROBINHOOD_API_KEY     = os.getenv("ROBINHOOD_API_KEY", "").strip()'
    if old_rh_key in code:
        code = code.replace(old_rh_key, new_rh_key)
        print("Added .strip() to ROBINHOOD_API_KEY")
    
    old_rh_pk = 'ROBINHOOD_PRIVATE_KEY = os.getenv("ROBINHOOD_PRIVATE_KEY", "")'
    new_rh_pk = 'ROBINHOOD_PRIVATE_KEY = os.getenv("ROBINHOOD_PRIVATE_KEY", "").strip()'
    if old_rh_pk in code:
        code = code.replace(old_rh_pk, new_rh_pk)
        print("Added .strip() to ROBINHOOD_PRIVATE_KEY")

with open("main.py", "w") as f:
    f.write(code)
print("Done")
FIX

# Check for trailing whitespace/newlines in .env
echo "Checking .env for whitespace issues..."
grep 'ROBINHOOD' .env | cat -A | head -5

docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18
docker logs traderjoes-bot --tail 3 2>&1
