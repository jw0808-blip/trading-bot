import discord
from discord.ext import commands
import os
import requests

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TOKEN = os.getenv('DISCORD_TOKEN')

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! Bot is alive on Render Background Worker.")

@bot.command()
async def portfolio(ctx):
    print("[DEBUG] Running !portfolio")
    await ctx.send("📊 **Portfolio Snapshot**\n**Kalshi Cash:** $0.00 (API issue - parked)\n**Robinhood Buying Power:** $0.00 (placeholder)\n**Polymarket USDC:** $2,000 (deposited)")

@bot.command()
async def cycle(ctx):
    print("[DEBUG] Starting !cycle market scan")
    await ctx.send("🔎 Scanning Polymarket for high EV opportunities...")
    try:
        r = requests.get("https://gamma-api.polymarket.com/events?active=true&closed=false&limit=20")
        data = r.json()
        await ctx.send("Top Polymarket EV Opportunities (live data):\n1. Check Polymarket for current odds (EV calculation coming next update)")
    except Exception as e:
        await ctx.send("❌ Failed to fetch Polymarket markets.")

bot.run(TOKEN)
