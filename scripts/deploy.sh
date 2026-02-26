#!/bin/bash
# ============================================================================
# TraderJoes — Deploy to Hetzner VPS
# Run from your Mac: bash scripts/deploy.sh [server_ip]
# ============================================================================

SERVER_IP="${1:-89.167.108.136}"
REMOTE_DIR="/opt/traderjoes"

echo "========================================="
echo " Deploying TraderJoes to $SERVER_IP"
echo "========================================="

echo "[1/4] Syncing files..."
rsync -avz --progress \
    --exclude '.env' \
    --exclude '.git' \
    --exclude '__pycache__' \
    --exclude '*.pyc' \
    --exclude '.venv' \
    --exclude 'node_modules' \
    ./ root@$SERVER_IP:$REMOTE_DIR/

echo ""
echo "[2/4] Deploying OpenClaw skills..."
SKILLS_DIR="/root/.openclaw/skills"
ssh root@$SERVER_IP "mkdir -p $SKILLS_DIR/{monte-carlo/scripts,multi-model-consensus/scripts,historian/scripts,liquidity-scan/scripts,tradingview-indicators/scripts,risk-management/scripts}"

for skill in monte-carlo multi-model-consensus historian liquidity-scan tradingview-indicators risk-management; do
    if [ -d "skills/$skill" ]; then
        echo "  -> $skill"
        scp skills/$skill/SKILL.md root@$SERVER_IP:$SKILLS_DIR/$skill/SKILL.md 2>/dev/null
        if [ -d "skills/$skill/scripts" ]; then
            scp skills/$skill/scripts/*.py root@$SERVER_IP:$SKILLS_DIR/$skill/scripts/ 2>/dev/null
        fi
    fi
done
ssh root@$SERVER_IP "chmod +x $SKILLS_DIR/*/scripts/*.py 2>/dev/null"

echo ""
echo "[3/4] Rebuilding containers..."
ssh root@$SERVER_IP "cd $REMOTE_DIR && docker compose build --no-cache traderjoes-bot"

echo ""
echo "[4/4] Restarting services..."
ssh root@$SERVER_IP "cd $REMOTE_DIR && docker compose up -d"

echo ""
echo "========================================="
echo " Done! Deployment complete."
echo "========================================="
