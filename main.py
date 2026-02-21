import discord
from discord.ext import commands
import os
import time
import base64
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

KALSHI_KEY_ID = os.getenv('KALSHI_API_KEY_ID')
KALSHI_PEM = os.getenv('KALSHI_PRIVATE_KEY_PEM')
TOKEN = os.getenv('DISCORD_TOKEN')

print("=== BOT STARTING ===")
print(f"TOKEN length: {len(TOKEN) if TOKEN else 0}")
print(f"KALSHI_KEY_ID present: {bool(KALSHI_KEY_ID)}")
print(f"KALSHI_PEM length: {len(KALSHI_PEM) if KALSHI_PEM else 0}")
print(f"KALSHI_PEM starts with -----BEGIN: {KALSHI_PEM.startswith('-----BEGIN PRIVATE KEY-----') if KALSHI_PEM else False}")

def get_kalshi_headers(method, path):
    if not KALSHI_KEY_ID or not KALSHI_PEM:
        print("ERROR: Kalshi secrets missing")
        return None
    path_for_signing = path.split('?')[0]
    timestamp = str(int(time.time() * 1000))
    msg = timestamp + method + path_for_signing
    print(f"Signing message: {msg[:50]}...")
    try:
        private_key = serialization.load_pem_private_key(KALSHI_PEM.encode(), password=None)
        if not isinstance(private_key, rsa.RSAPrivateKey):
            print("ERROR: Key is not RSAPrivateKey")
            return None
        signature_bytes = private_key.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
            hashes.SHA256()
        )
        signature = base64.b64encode(signature_bytes).decode()
        print("Signing successful")
        return {
            "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }
    except Exception as e:
        print(f"Signing error: {type(e).__name__}: {e}")
        return None

def fetch_kalshi(path):
    headers = get_kalshi_headers("GET", path)
    if not headers:
        print("Failed to get headers")
        return None
    print(f"Fetching {path}")
    r = requests.get(f"https://trading-api.kalshi.com{path}", headers=headers)
    print(f"Status code: {r.status_code}")
    print(f"Response: {r.text[:300]}...")
    return r.json() if r.status_code == 200 else None

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! Bot is alive on Render Background Worker.")

@bot.command()
async def portfolio(ctx):
    print("[DEBUG] Running !portfolio")
    data = fetch_kalshi("/trade-api/v2/portfolio/balance")
    balance = data.get('balance', 0) / 100.0 if data else 0.0
    await ctx.send(f"📊 **Portfolio Snapshot**\n**Kalshi Cash:** ${balance:,.2f}")

bot.run(TOKEN)
