#!/bin/bash
# Host-level healthcheck — alerts via DISCORD_CRITICAL_WEBHOOK when the
# traderjoes-bot container has been down > 5 min. Posts an auto-resolve
# message when the bot returns. Runs every 5 min via cron.
# State: /tmp/health_bot_down_since.txt (epoch seconds of first observed down).

set -u
ENV_FILE="/root/trading-bot/.env"
STATE_FILE="/tmp/health_bot_down_since.txt"
ALERT_MARKER="${STATE_FILE}.alerted"
DOWN_THRESHOLD_SEC=300   # 5 min
CONTAINER="traderjoes-bot"

# Source env, but only the webhook line — avoid eval-ing the whole file.
WEBHOOK="$(grep -E '^DISCORD_CRITICAL_WEBHOOK=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r' | tr -d '"')"

post_alert() {
    local content="$1"
    if [[ -z "$WEBHOOK" ]]; then
        echo "[$(date -u +%FT%TZ)] WEBHOOK MISSING — would have posted: $content"
        return 1
    fi
    # Discord webhook accepts JSON payload {"content": "..."}.
    # Use jq to safely escape.
    local payload
    payload=$(jq -n --arg c "$content" '{content: $c}')
    curl -sS -X POST -H "Content-Type: application/json" \
        -d "$payload" "$WEBHOOK" > /dev/null
}

now=$(date +%s)
# `docker ps` filter — running container only
if docker ps --filter "name=^${CONTAINER}\$" --filter "status=running" --format '{{.Names}}' \
    | grep -qx "$CONTAINER"; then
    # Bot is up. If we had a down-state, send resolve and clear.
    if [[ -f "$STATE_FILE" ]]; then
        down_since=$(cat "$STATE_FILE")
        duration=$((now - down_since))
        rm -f "$STATE_FILE" "$ALERT_MARKER"
        # Only alert resolved if we previously alerted (i.e. duration > threshold)
        if (( duration >= DOWN_THRESHOLD_SEC )); then
            post_alert "✅ **RESOLVED — \`${CONTAINER}\` is back up**
Was down for ~$((duration / 60)) min. Trading resumed."
            echo "[$(date -u +%FT%TZ)] resolved alert sent (down ${duration}s)"
        else
            echo "[$(date -u +%FT%TZ)] transient (down ${duration}s) — no alert"
        fi
    fi
    exit 0
fi

# Bot is DOWN.
if [[ ! -f "$STATE_FILE" ]]; then
    echo "$now" > "$STATE_FILE"
    echo "[$(date -u +%FT%TZ)] container down — state file created"
    exit 0
fi

down_since=$(cat "$STATE_FILE")
duration=$((now - down_since))
echo "[$(date -u +%FT%TZ)] container down for ${duration}s"

# Already-alerted marker prevents re-pinging every 5 min.
if (( duration >= DOWN_THRESHOLD_SEC )) && [[ ! -f "$ALERT_MARKER" ]]; then
    post_alert "@here 🔴 **CRITICAL — \`${CONTAINER}\` container is DOWN**
Down for $((duration / 60))m $(( duration % 60 ))s (since $(date -u -d @${down_since} +%FT%TZ)).
Trading is **halted**. \`docker ps\` shows the container is not running.
Investigate: \`docker logs ${CONTAINER} --tail 100\`."
    touch "$ALERT_MARKER"
    echo "[$(date -u +%FT%TZ)] CRITICAL alert sent"
fi
