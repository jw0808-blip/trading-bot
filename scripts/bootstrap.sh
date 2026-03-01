#!/bin/bash
# TraderJoes EchoEdge — Hetzner VPS Full Bootstrap
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

echo "========================================="
echo " TraderJoes EchoEdge — VPS Bootstrap"
echo "========================================="

echo "[1/7] Updating system..."
apt update && apt upgrade -y
apt install -y curl wget git build-essential python3 python3-pip python3-venv jq ffmpeg ufw fail2ban

echo "[2/7] Firewall..."
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw --force enable

echo "[3/7] Fail2ban..."
systemctl enable fail2ban && systemctl start fail2ban

echo "[4/7] Docker..."
if ! command -v docker &> /dev/null; then
    curl -fsSL https://get.docker.com | sh
    systemctl enable docker && systemctl start docker
    apt install -y docker-compose-plugin 2>/dev/null || true
fi

echo "[5/7] Node.js 22..."
if ! command -v node &> /dev/null || [[ $(node -v | cut -d. -f1 | tr -d v) -lt 22 ]]; then
    curl -fsSL https://deb.nodesource.com/setup_22.x | bash -
    apt install -y nodejs
fi
mkdir -p /root/.npm-global
npm config set prefix '/root/.npm-global'
grep -q 'npm-global' /root/.bashrc || echo 'export PATH=/root/.npm-global/bin:$PATH' >> /root/.bashrc
export PATH=/root/.npm-global/bin:$PATH

echo "[6/7] OpenClaw..."
npm install -g openclaw@latest

echo "[7/7] Project setup..."
mkdir -p /opt/traderjoes
cd /opt/traderjoes
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install requests numpy scipy openai anthropic aiohttp
deactivate

SKILLS_DIR="/root/.openclaw/skills"
mkdir -p "$SKILLS_DIR"/{monte-carlo/scripts,multi-model-consensus/scripts,historian/scripts,liquidity-scan/scripts,tradingview-indicators/scripts,risk-management/scripts}

echo ""
echo "========================================="
echo " ✅ Bootstrap complete!"
echo "========================================="
echo "Server IP: $(curl -s ifconfig.me)"
echo ""
echo "NEXT: cd /opt/traderjoes && git clone https://github.com/jw0808-blip/trading-bot.git ."
echo "Then: cp .env.template .env && nano .env"
echo "Then: docker compose up -d"
