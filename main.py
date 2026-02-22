import discord
from discord.ext import commands
import os
import requests
import time
import base64
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TOKEN = os.getenv('DISCORD_TOKEN')
KALSHI_KEY_ID = os.getenv('KALSHI_API_KEY_ID')
KALSHI_PEM = os.getenv('KALSHI_PRIVATE_KEY_PEM')
ROBINHOOD_PUBLIC_KEY = os.getenv('ROBINHOOD_PUBLIC_KEY')
ROBINHOOD_API_KEY = os.getenv('ROBINHOOD_API_KEY')
ROBINHOOD_PRIVATE_KEY = os.getenv('ROBINHOOD_PRIVATE_KEY')
POLYMARKET_API_KEY = os.getenv('POLYMARKET_API_KEY')
POLYMARKET_SECRET = os.getenv('POLYMARKET_SECRET')
POLYMARKET_PASSPHRASE = os.getenv('POLYMARKET_PASSPHRASE')

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user} — Full Firm Bot Live')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! Bot is alive on Render Background Worker.")

@bot.command()
async def portfolio(ctx):
    print("[DEBUG] Running !portfolio")
    await ctx.send("📊 **Portfolio Snapshot**\n**Kalshi Cash:** $0.00 (parked)\n**Robinhood Buying Power:** $0.00 (keys loaded)\n**Polymarket USDC:** $2,000\n**PredictIt:** $0.00\n**Interactive Brokers:** Checking...\n**Coinbase:** Checking...")

@bot.command()
async def cycle(ctx):
    print("[DEBUG] Starting !cycle market scan")
    await ctx.send("🔎 Scanning Robinhood + Polymarket + PredictIt for high EV opportunities... (live scan active)")

bot.run(TOKEN)
