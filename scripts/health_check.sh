#!/bin/bash
# ============================================================================
# TraderJoes — Health Monitor
# Add to crontab: */5 * * * * /opt/traderjoes/scripts/health_check.sh
# ============================================================================

COMPOSE_FILE="/opt/traderjoes/docker-compose.yml"
LOG_FILE="/var/log/traderjoes-health.log"
DISCORD_WEBHOOK="${DISCORD_HEALTH_WEBHOOK:-}"

log() {
    echo "$(date -u '+%Y-%m-%d %H:%M:%S UTC') $1" >> "$LOG_FILE"
}

# Check if bot container is running
if ! docker compose -f "$COMPOSE_FILE" ps traderjoes-bot --format json 2>/dev/null | grep -q '"running"'; then
    log "ALERT: traderjoes-bot is DOWN — restarting..."
    docker compose -f "$COMPOSE_FILE" up -d traderjoes-bot

    # Wait and check again
    sleep 10
    if docker compose -f "$COMPOSE_FILE" ps traderjoes-bot --format json 2>/dev/null | grep -q '"running"'; then
        log "RECOVERED: traderjoes-bot restarted successfully"
        MSG="⚠️ TraderJoes bot was down but auto-recovered at $(date -u '+%H:%M UTC')"
    else
        log "CRITICAL: traderjoes-bot failed to restart"
        MSG="🔴 CRITICAL: TraderJoes bot is DOWN and could not auto-recover!"
    fi

    # Send Discord notification if webhook configured
    if [ -n "$DISCORD_WEBHOOK" ]; then
        curl -s -H "Content-Type: application/json" \
            -d "{\"content\": \"$MSG\"}" \
            "$DISCORD_WEBHOOK" > /dev/null
    fi
else
    # Only log every hour to avoid spam
    if [ "$(date +%M)" -lt "5" ]; then
        log "OK: all services running"
    fi
fi

# Check disk space
DISK_PCT=$(df / | tail -1 | awk '{print $5}' | tr -d '%')
if [ "$DISK_PCT" -gt 85 ]; then
    log "WARNING: Disk usage at ${DISK_PCT}%"
    docker image prune -f > /dev/null 2>&1
    docker container prune -f > /dev/null 2>&1
fi

# Check memory
MEM_PCT=$(free | grep Mem | awk '{printf "%.0f", $3/$2 * 100}')
if [ "$MEM_PCT" -gt 90 ]; then
    log "WARNING: Memory usage at ${MEM_PCT}%"
fi

# Rotate log if > 1MB
if [ -f "$LOG_FILE" ] && [ "$(stat -f%z "$LOG_FILE" 2>/dev/null || stat -c%s "$LOG_FILE")" -gt 1048576 ]; then
    mv "$LOG_FILE" "${LOG_FILE}.old"
fi
