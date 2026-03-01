#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

TEMPLATE=".env.template"
ENVFILE=".env"

# Create template if missing
cat > "$TEMPLATE" << 'TMPL'
# TraderJoes Trading Firm — Environment Variables
# Run: bash scripts/setup-env.sh to configure

# Discord (REQUIRED)
DISCORD_TOKEN=
DISCORD_CHANNEL_ID=

# OpenAI (REQUIRED for !analyze)
OPENAI_API_KEY=

# Kalshi
KALSHI_API_KEY_ID=
KALSHI_PRIVATE_KEY=""

# Polymarket
POLY_WALLET_ADDRESS=
POLYMARKET_API_KEY=
POLYMARKET_SECRET=
POLYMARKET_PASSPHRASE=

# Robinhood Crypto
ROBINHOOD_API_KEY=
ROBINHOOD_PRIVATE_KEY=""
ROBINHOOD_PUBLIC_KEY=""

# Coinbase Advanced Trade
COINBASE_API_KEY=
COINBASE_API_SECRET=""

# Phemex
PHEMEX_API_KEY=
PHEMEX_API_SECRET=

# GitHub Logging
GITHUB_TOKEN=
GITHUB_REPO=jw0808-blip/trading-bot

# Trading Mode: paper or live
TRADING_MODE=paper
TMPL

echo "=== TraderJoes .env Setup ==="
echo ""

# Copy template if no .env exists
if [ ! -f "$ENVFILE" ]; then
    cp "$TEMPLATE" "$ENVFILE"
    echo "Created .env from template"
else
    echo "Existing .env found — checking for missing keys..."
    # Add any missing keys from template
    while IFS= read -r line; do
        key=$(echo "$line" | grep -oP '^[A-Z_]+=?' | tr -d '=' || true)
        if [ -n "$key" ] && ! grep -q "^${key}=" "$ENVFILE" 2>/dev/null; then
            echo "$line" >> "$ENVFILE"
            echo "  Added missing key: $key"
        fi
    done < "$TEMPLATE"
fi

echo ""

# Required keys
REQUIRED="DISCORD_TOKEN DISCORD_CHANNEL_ID"
OPTIONAL="OPENAI_API_KEY KALSHI_API_KEY_ID KALSHI_PRIVATE_KEY POLY_WALLET_ADDRESS POLYMARKET_API_KEY POLYMARKET_SECRET POLYMARKET_PASSPHRASE ROBINHOOD_API_KEY ROBINHOOD_PRIVATE_KEY COINBASE_API_KEY COINBASE_API_SECRET PHEMEX_API_KEY PHEMEX_API_SECRET TRADING_MODE"

missing_required=0
missing_optional=0

echo "--- Required Keys ---"
for key in $REQUIRED; do
    val=$(grep "^${key}=" "$ENVFILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
    if [ -z "$val" ]; then
        echo "  [MISSING] $key"
        read -sp "  Enter $key: " input
        echo ""
        if [ -n "$input" ]; then
            if echo "$input" | grep -q '[-/+]'; then
                sed -i "s|^${key}=.*|${key}=\"${input}\"|" "$ENVFILE"
            else
                sed -i "s|^${key}=.*|${key}=${input}|" "$ENVFILE"
            fi
            echo "  [SET] $key"
        else
            missing_required=$((missing_required+1))
        fi
    else
        echo "  [OK] $key"
    fi
done

echo ""
echo "--- Optional Keys ---"
for key in $OPTIONAL; do
    val=$(grep "^${key}=" "$ENVFILE" | cut -d= -f2- | tr -d '"' | tr -d "'" || true)
    if [ -z "$val" ]; then
        echo "  [EMPTY] $key (optional — set later with nano .env)"
        missing_optional=$((missing_optional+1))
    else
        echo "  [OK] $key"
    fi
done

# Validate formatting
echo ""
echo "--- Format Checks ---"
errors=0

# Check for unquoted private keys with special chars
while IFS= read -r line; do
    key=$(echo "$line" | grep -oP '^[A-Z_]+(?==)' || true)
    val=$(echo "$line" | cut -d= -f2-)
    if echo "$key" | grep -qi "PRIVATE_KEY\|SECRET"; then
        if [ -n "$val" ] && echo "$val" | grep -q '[-/+]' && ! echo "$val" | grep -q '^"'; then
            echo "  [WARN] $key has special chars but is not quoted — fixing..."
            sed -i "s|^${key}=.*|${key}=\"${val}\"|" "$ENVFILE"
        fi
    fi
done < "$ENVFILE"

# Check first line is a comment
first=$(head -1 "$ENVFILE")
if ! echo "$first" | grep -q '^#'; then
    sed -i '1s/^/# TraderJoes Trading Firm\n/' "$ENVFILE"
    echo "  [FIXED] Added comment header"
fi

echo "  [OK] Format validated"
echo ""

# Summary
total_keys=$(grep -c '^[A-Z]' "$ENVFILE" || true)
set_keys=$((total_keys - missing_required - missing_optional))
echo "=== Summary ==="
echo "  Total keys: $total_keys"
echo "  Configured: $set_keys"
echo "  Missing required: $missing_required"
echo "  Missing optional: $missing_optional"

if [ $missing_required -eq 0 ]; then
    echo ""
    echo "✅ .env is valid — ready to run!"
    echo "  Restart bot: docker compose down traderjoes-bot && docker compose build traderjoes-bot && docker compose up -d"
else
    echo ""
    echo "⚠️  Missing required keys — bot will not start without DISCORD_TOKEN"
fi
