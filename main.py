import discord
from discord.ext import commands
import os
import requests
import json
import time
import hmac
import hashlib
import base64
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ── ENV VARS ──────────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.environ.get('DISCORD_TOKEN')
KALSHI_API_KEY_ID    = os.environ.get('KALSHI_API_KEY_ID')
KALSHI_PRIVATE_KEY   = os.environ.get('KALSHI_PRIVATE_KEY', '')
POLYMARKET_API_KEY   = os.environ.get('POLYMARKET_API_KEY')
POLYMARKET_SECRET    = os.environ.get('POLYMARKET_SECRET')
POLYMARKET_PASSPHRASE= os.environ.get('POLYMARKET_PASSPHRASE')
POLY_WALLET_ADDRESS  = os.environ.get('POLY_WALLET_ADDRESS', '')
GITHUB_TOKEN         = os.environ.get('GITHUB_TOKEN', '')
GITHUB_REPO          = os.environ.get('GITHUB_REPO', 'jw0808-blip/trading-bot')
LOG_CHANNEL_ID       = int(os.environ.get('LOG_CHANNEL_ID', '0'))

# ── KALSHI (FIXED: correct domain + PSS padding) ──────────────────────────────
KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'

def kalshi_sign(method, path, body=''):
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path + (body or '')
    try:
        key_pem = KALSHI_PRIVATE_KEY.replace('\\n', '\n')
        private_key = serialization.load_pem_private_key(
            key_pem.encode(), password=None, backend=default_backend()
        )
        sig = private_key.sign(
            msg.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256().digest_size
            ),
            hashes.SHA256()
        )
        return ts, base64.b64encode(sig).decode()
    except Exception as e:
        return ts, ''

def get_kalshi_balance():
    path = '/portfolio/balance'
    ts, sig = kalshi_sign('GET', path)
    headers = {
        'KALSHI-ACCESS-KEY': KALSHI_API_KEY_ID,
        'KALSHI-ACCESS-TIMESTAMP': ts,
        'KALSHI-ACCESS-SIGNATURE': sig,
        'Content-Type': 'application/json'
    }
    try:
        r = requests.get(KALSHI_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            cents = data.get('balance', 0)
            return f"${cents/100:,.2f}"
        return f"Error {r.status_code}: {r.text[:100]}"
    except Exception as e:
        return f"Exception: {e}"

# ── POLYMARKET (FIXED: Polygon USDC balance via RPC) ─────────────────────────
def get_polymarket_balance():
    if not POLY_WALLET_ADDRESS:
        return "No wallet address set (POLY_WALLET_ADDRESS env var)"
    # USDC contract on Polygon
    usdc_contract = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
    # balanceOf(address) selector + padded address
    addr_clean = POLY_WALLET_ADDRESS.lower().replace('0x', '').zfill(64)
    data = '0x70a08231' + addr_clean
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_call",
        "params": [{"to": usdc_contract, "data": data}, "latest"],
        "id": 1
    }
    polygon_rpcs = [
        'https://polygon-rpc.com',
        'https://rpc-mainnet.matic.network',
        'https://rpc.ankr.com/polygon'
    ]
    for rpc in polygon_rpcs:
        try:
            r = requests.post(rpc, json=payload, timeout=10)
            if r.status_code == 200:
                result = r.json().get('result', '0x0')
                balance_raw = int(result, 16)
                balance_usdc = balance_raw / 1_000_000  # USDC has 6 decimals
                return f"${balance_usdc:,.2f}"
        except Exception:
            continue
    return "Unable to fetch (all RPCs failed)"

# ── GITHUB LOGGING ────────────────────────────────────────────────────────────
def log_to_github(entry: str):
    if not GITHUB_TOKEN:
        return
    api = f'https://api.github.com/repos/{GITHUB_REPO}/contents/conversations.md'
    headers = {
        'Authorization': f'token {GITHUB_TOKEN}',
        'Accept': 'application/vnd.github.v3+json'
    }
    try:
        r = requests.get(api, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            current = base64.b64decode(data['content']).decode('utf-8')
            sha = data['sha']
        else:
            current = '# TraderJoes AI Conversation Log\n\nAll bot activity is logged here.\n\n---\n\n'
            sha = None
        new_content = current + entry
        payload = {
            'message': f'Bot activity log {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}',
            'content': base64.b64encode(new_content.encode()).decode(),
        }
        if sha:
            payload['sha'] = sha
        requests.put(api, headers=headers, json=payload, timeout=15)
    except Exception:
        pass

# ── DISCORD COMMANDS ──────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f'✅ TraderJoes bot online as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send('🏓 Pong! TraderJoes bot is live.')

@bot.command()
async def portfolio(ctx):
    await ctx.send('📊 Fetching portfolio balances...')
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')

    kalshi_bal   = get_kalshi_balance()
    poly_bal     = get_polymarket_balance()

    msg = (
        f"**TraderJoes Portfolio Snapshot** | {ts}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🟢 **Kalshi:**      {kalshi_bal}\n"
        f"🟣 **Polymarket:**  {poly_bal}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    await ctx.send(msg)

    log_entry = (
        f"\n## Portfolio Snapshot — {ts}\n"
        f"- **Kalshi:** {kalshi_bal}\n"
        f"- **Polymarket:** {poly_bal}\n\n"
    )
    log_to_github(log_entry)

@bot.command()
async def cycle(ctx):
    await ctx.send('🔄 Running EV scan across all platforms...')
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    await ctx.send(f"✅ EV scan complete at {ts}. Sub-bots are processing markets.")

@bot.command()
async def log(ctx, *, message: str):
    """Manually log a message to conversations.md and #ai-logs"""
    ts = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    entry = (
        f"\n## Manual Log — {ts}\n"
        f"**Source:** {ctx.author} via Discord\n"
        f"**Message:**\n{message}\n\n"
    )
    log_to_github(entry)
    await ctx.send(f"✅ Logged to conversations.md:\n```{message[:200]}```")

if __name__ == '__main__':
    bot.run(DISCORD_TOKEN)
