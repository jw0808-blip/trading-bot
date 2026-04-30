#!/bin/bash
# Host-level healthcheck — alerts via DISCORD_CRITICAL_WEBHOOK when IB
# Gateway has been disconnected > 30 min (debounced against transient
# hiccups). Posts auto-resolve when reconnects. Runs every 5 min via cron.
# State: /tmp/health_ib_down_since.txt (epoch seconds of first false).

set -u
ENV_FILE="/root/trading-bot/.env"
STATE_FILE="/tmp/health_ib_down_since.txt"
ALERT_MARKER="${STATE_FILE}.alerted"
DOWN_THRESHOLD_SEC=1800   # 30 min
STATUS_URL="http://localhost:8080/api/status"

WEBHOOK="$(grep -E '^DISCORD_CRITICAL_WEBHOOK=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | tr -d '"')"

post_alert() {
    local content="$1"
    if [[ -z "$WEBHOOK" ]]; then
        echo "[$(date -u +%FT%TZ)] WEBHOOK MISSING — would have posted: $content"
        return 1
    fi
    local payload
    payload=$(jq -n --arg c "$content" '{content: $c}')
    curl -sS -X POST -H "Content-Type: application/json" \
        -d "$payload" "$WEBHOOK" > /dev/null
}

now=$(date +%s)
# Hit /api/status; if the bot is down too, we can't tell — bail (the bot
# health check covers that case).
status_json=$(curl -sS --max-time 8 "$STATUS_URL" 2>/dev/null)
if [[ -z "$status_json" ]]; then
    echo "[$(date -u +%FT%TZ)] /api/status unreachable — leaving IB state untouched"
    exit 0
fi

ib_connected=$(echo "$status_json" | jq -r '.ib.connected // false')

if [[ "$ib_connected" == "true" ]]; then
    # Connected. If we had a down-state, send resolve and clear.
    if [[ -f "$STATE_FILE" ]]; then
        down_since=$(cat "$STATE_FILE")
        duration=$((now - down_since))
        rm -f "$STATE_FILE" "$ALERT_MARKER"
        if (( duration >= DOWN_THRESHOLD_SEC )); then
            post_alert "✅ **RESOLVED — IB Gateway reconnected**
Was disconnected for ~$((duration / 60)) min. Equities trading resumed."
            echo "[$(date -u +%FT%TZ)] IB resolved alert sent (down ${duration}s)"
        else
            echo "[$(date -u +%FT%TZ)] IB transient (down ${duration}s) — no alert"
        fi
    fi
    exit 0
fi

# IB disconnected.
if [[ ! -f "$STATE_FILE" ]]; then
    echo "$now" > "$STATE_FILE"
    echo "[$(date -u +%FT%TZ)] IB disconnected — state file created"
    exit 0
fi

down_since=$(cat "$STATE_FILE")
duration=$((now - down_since))
echo "[$(date -u +%FT%TZ)] IB disconnected for ${duration}s"

if (( duration >= DOWN_THRESHOLD_SEC )) && [[ ! -f "$ALERT_MARKER" ]]; then
    post_alert "@here 🟠 **HIGH — IB Gateway disconnected**
Disconnected for $((duration / 60))m (since $(date -u -d @${down_since} +%FT%TZ)).
Equities/oracle entries are blocked. Bot is still running other strategies.
Restart the IB Gateway container or re-login the upstream session."
    touch "$ALERT_MARKER"
    echo "[$(date -u +%FT%TZ)] HIGH alert sent"
fi
