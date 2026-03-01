#!/usr/bin/env bash
# TraderJoes Watchdog — runs every 5 minutes via cron
cd /root/trading-bot

# Check if bot container is running
BOT_STATUS=$(docker inspect -f '{{.State.Running}}' traderjoes-bot 2>/dev/null)

if [ "$BOT_STATUS" != "true" ]; then
    echo "$(date) — Bot is DOWN. Restarting..."
    docker compose up -d traderjoes-bot
    sleep 15
    
    # Send Discord alert via webhook (if configured)
    WEBHOOK=$(grep "^DISCORD_WEBHOOK=" .env 2>/dev/null | cut -d= -f2)
    if [ -n "$WEBHOOK" ]; then
        curl -s -H "Content-Type: application/json" \
            -d '{"content":"**WATCHDOG ALERT:** TraderJoes bot was down and has been restarted."}' \
            "$WEBHOOK" || true
    fi
fi

# Check OpenClaw
OC_STATUS=$(docker inspect -f '{{.State.Running}}' traderjoes-openclaw 2>/dev/null)
if [ "$OC_STATUS" != "true" ]; then
    echo "$(date) — OpenClaw is DOWN. Restarting..."
    docker compose up -d traderjoes-openclaw
fi

# Check Netdata
ND_STATUS=$(docker inspect -f '{{.State.Running}}' traderjoes-netdata 2>/dev/null)
if [ "$ND_STATUS" != "true" ]; then
    echo "$(date) — Netdata is DOWN. Restarting..."
    docker compose up -d traderjoes-netdata
fi

# Heartbeat log
echo "$(date) — Watchdog OK: bot=$BOT_STATUS oc=$OC_STATUS nd=$ND_STATUS" >> /root/trading-bot/data/watchdog.log

# Trim log
tail -1000 /root/trading-bot/data/watchdog.log > /root/trading-bot/data/watchdog.log.tmp 2>/dev/null
mv /root/trading-bot/data/watchdog.log.tmp /root/trading-bot/data/watchdog.log 2>/dev/null
