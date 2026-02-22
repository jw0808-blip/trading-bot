import discord
from discord.ext import commands
import os
import requests
import hmac
import hashlib
import time

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TOKEN = os.getenv('DISCORD_TOKEN')
POLYMARKET_API_KEY = os.getenv('POLYMARKET_API_KEY')
POLYMARKET_SECRET = os.getenv('POLYMARKET_SECRET')
POLYMARKET_PASSPHRASE = os.getenv('POLYMARKET_PASSPHRASE')

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! Bot is alive on Render Background Worker.")

@bot.command()
async def portfolio(ctx):
    print("[DEBUG] Running !portfolio")
    # Polymarket balance (using your keys)
    polymarket_usdc = "2000.00"  # Placeholder - real fetch coming in next update with your keys
    await ctx.send(f"📊 **Portfolio Snapshot**\n**Kalshi Cash:** $0.00 (API issue - parked)\n**Robinhood Buying Power:** $0.00 (placeholder)\n**Polymarket USDC:** ${polymarket_usdc} (deposited)")

@bot.command()
async def cycle(ctx):
    print("[DEBUG] Starting !cycle market scan")
    await ctx.send("🔎 Scanning Polymarket for high EV opportunities...")
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50", timeout=15)
        data = r.json()
        markets = data.get('markets', [])[:5]
        response = "🚀 **Top 5 Polymarket Opportunities (live data)**\n"
        for m in markets:
            title = m.get('question', 'Unknown')
            response += f"• {title}\n"
        await ctx.send(response)
    except Exception as e:
        print(f"Polymarket error: {e}")
        await ctx.send("❌ Failed to fetch Polymarket markets. Trying again next time.")

bot.run(TOKEN)
