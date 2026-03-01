#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'PATCH'
with open("main.py", "r") as f:
    code = f.read()

# 1. Enable auto-paper by default
code = code.replace("AUTO_PAPER_ENABLED = False", "AUTO_PAPER_ENABLED = True")
print("Set AUTO_PAPER_ENABLED = True")

# 2. Verify daily report task exists and is started
if "daily_report_task" in code:
    print("Daily report task already exists")
else:
    print("WARNING: daily_report_task not found")

# 3. Make sure daily report is comprehensive
# Check if _send_daily_report exists
if "_send_daily_report" in code:
    print("_send_daily_report function exists")
else:
    print("WARNING: _send_daily_report not found")

with open("main.py", "w") as f:
    f.write(code)
print("Done")
PATCH

echo "Rebuilding..."
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20
docker logs traderjoes-bot --tail 3 2>&1
