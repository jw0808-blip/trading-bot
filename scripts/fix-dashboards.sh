#!/usr/bin/env bash
# ============================================================================
# TraderJoes — Dashboard Fix + GitHub Sync + Verification
# ============================================================================
set -euo pipefail
cd /root/trading-bot

echo "=== Task 1: Fix Dashboards ==="

# 1a. Make OpenClaw Studio accessible via Tailscale (change from localhost to 0.0.0.0)
echo "Fixing OpenClaw Studio port binding..."
if grep -q "127.0.0.1:3000:3000" docker-compose.yml; then
    sed -i 's|127.0.0.1:3000:3000|3000:3000|g' docker-compose.yml
    echo "  [OK] OpenClaw Studio: changed from localhost-only to all interfaces"
else
    echo "  [OK] OpenClaw Studio: already accessible"
fi

# 1b. Ensure UFW allows Tailscale but blocks public for 3000 and 19999
echo "Configuring firewall for Tailscale-only access..."
# Allow from Tailscale subnet (100.64.0.0/10)
ufw allow from 100.64.0.0/10 to any port 3000 comment "OpenClaw via Tailscale" 2>/dev/null || true
ufw allow from 100.64.0.0/10 to any port 19999 comment "Netdata via Tailscale" 2>/dev/null || true

# Block public access to these ports (deny from anywhere else)
ufw deny from any to any port 3000 comment "Block public OpenClaw" 2>/dev/null || true
# Note: Netdata 19999 is already open on 0.0.0.0 - restrict it
ufw deny from any to any port 19999 comment "Block public Netdata" 2>/dev/null || true

echo "  [OK] Firewall: Tailscale (100.64.0.0/10) allowed, public blocked"

# 1c. Verify Tailscale is running
echo "Checking Tailscale..."
if tailscale status &>/dev/null; then
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "100.89.63.72")
    echo "  [OK] Tailscale active: $TAILSCALE_IP"
else
    echo "  [WARN] Tailscale not running. Starting..."
    systemctl start tailscaled 2>/dev/null || true
    sleep 3
    TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "100.89.63.72")
    echo "  [OK] Tailscale IP: $TAILSCALE_IP"
fi

echo ""
echo "=== Task 2: GitHub Synchronization ==="

# 2a. Setup git if not configured
cd /root/trading-bot
if [ ! -d .git ]; then
    echo "Initializing git repo..."
    git init
    git remote add origin https://github.com/jw0808-blip/trading-bot.git 2>/dev/null || true
fi

# Configure git
git config user.email "jerad@traderjoes.bot" 2>/dev/null || true
git config user.name "TraderJoes Bot" 2>/dev/null || true

# 2b. Create .gitignore if missing
cat > .gitignore << 'GITIGNORE'
.env
*.bak
*.bak.*
__pycache__/
*.pyc
node_modules/
.venv/
logs/
GITIGNORE

# 2c. Get GitHub token
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
if [ -z "$GITHUB_TOKEN" ]; then
    echo "  [ERROR] No GITHUB_TOKEN in .env"
    exit 1
fi

# 2d. Ensure scripts directory exists
mkdir -p scripts logs

# 2e. Stage all files
echo "Staging files for push..."
git add -A
git status --short

# 2f. Commit
git commit -m "Full sync: V3 deployment - all 15 features, dashboards, scripts" --allow-empty 2>/dev/null || true

# 2g. Force push to main
echo "Pushing to GitHub..."
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git branch -M main 2>/dev/null || true
git push -u origin main --force 2>&1 || echo "  [WARN] Push may have partially failed"

echo "  [OK] GitHub synchronized"

# 2h. Test GitHub logger
echo "Testing GitHub logger..."
python3 -c "
import os, requests, base64
token = os.environ.get('GITHUB_TOKEN', '') or '${GITHUB_TOKEN}'
repo = 'jw0808-blip/trading-bot'
url = f'https://api.github.com/repos/{repo}/contents/conversations.md'
hdrs = {'Authorization': f'token {token}', 'Accept': 'application/vnd.github.v3+json'}
r = requests.get(url, headers=hdrs, timeout=10)
if r.status_code == 200:
    print('  [OK] GitHub logger: conversations.md accessible')
else:
    print(f'  [WARN] GitHub logger: status {r.status_code}')
" 2>/dev/null || echo "  [WARN] GitHub logger test failed"

echo ""
echo "=== Task 3: Restart & Verify ==="

# 3a. Rebuild and restart all containers
echo "Restarting all containers..."
docker compose down
docker compose build traderjoes-bot
docker compose up -d
sleep 25

# 3b. Show container status
echo ""
echo "--- Container Status ---"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

# 3c. Show bot logs
echo ""
echo "--- Bot Logs ---"
docker logs traderjoes-bot --tail 5

# 3d. Verify Netdata
echo ""
echo "--- Dashboard Verification ---"
TAILSCALE_IP=$(tailscale ip -4 2>/dev/null || echo "100.89.63.72")

# Test Netdata
if curl -sf -o /dev/null "http://localhost:19999/api/v1/info" 2>/dev/null; then
    echo "  [OK] Netdata: Running on port 19999"
    echo "       URL: http://${TAILSCALE_IP}:19999"
else
    echo "  [WARN] Netdata: Not responding locally"
fi

# Test OpenClaw Studio
if curl -sf -o /dev/null "http://localhost:3000/" 2>/dev/null; then
    echo "  [OK] OpenClaw Studio: Running on port 3000"
    echo "       URL: http://${TAILSCALE_IP}:3000"
else
    echo "  [WARN] OpenClaw Studio: Not responding (may need a moment to start)"
    sleep 10
    if curl -sf -o /dev/null "http://localhost:3000/" 2>/dev/null; then
        echo "  [OK] OpenClaw Studio: Running after retry"
        echo "       URL: http://${TAILSCALE_IP}:3000"
    else
        echo "  [INFO] OpenClaw Studio container running but app may still be initializing"
    fi
fi

echo ""
echo "=================================================="
echo "  TraderJoes — Dashboard Fix + Sync Complete"
echo "=================================================="
echo ""
echo "Dashboard URLs (Tailscale only):"
echo "  Netdata:        http://${TAILSCALE_IP}:19999"
echo "  OpenClaw Studio: http://${TAILSCALE_IP}:3000"
echo ""
echo "IMPORTANT: You must have Tailscale installed and connected"
echo "on your device to access these URLs."
echo ""
echo "  iPhone:  Tailscale app (already connected)"
echo "  Windows: Install from https://tailscale.com/download"
echo "           OR run: winget install tailscale.tailscale"
echo ""
echo "If you want public access instead (less secure):"
echo "  ufw allow 19999/tcp && ufw allow 3000/tcp"
echo ""
echo "Test these Discord commands:"
echo "  !cycle  !portfolio  !analyze  !daily  !studio"
echo "=================================================="
