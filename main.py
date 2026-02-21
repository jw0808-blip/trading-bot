import discord
from discord.ext import commands
import os
import time
import base64
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

print("=== BOT STARTING ===")

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

KALSHI_KEY_ID = os.getenv('KALSHI_API_KEY_ID')
KALSHI_PEM = os.getenv('KALSHI_PRIVATE_KEY_PEM')
TOKEN = os.getenv('DISCORD_TOKEN')

print(f"TOKEN length: {len(TOKEN) if TOKEN else 0}")
print(f"KALSHI_KEY_ID present: {bool(KALSHI_KEY_ID)}")
print(f"KALSHI_PEM length: {len(KALSHI_PEM) if KALSHI_PEM else 0}")

def get_kalshi_headers(method, path):
    if not KALSHI_KEY_ID or not KALSHI_PEM:
        print("ERROR: Kalshi secrets missing")
        return None
    path_for_signing = path.split('?')[0]
    timestamp = str(int(time.time() * 1000))
    msg = timestamp + method + path_for_signing
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
        return {
            "KALSHI-ACCESS-KEY": KALSHI_KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "Content-Type": "application/json"
        }
    except Exception as e:
        print(f"Signing error: {e}")
        return None

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! Bot is alive on Render Background Worker.")

bot.run(TOKEN)
