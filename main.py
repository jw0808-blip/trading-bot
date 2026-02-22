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
    # Robinhood placeholder (we had this on Colab - full OAuth coming)
    robinhood_bp = "0.00"
    # Polymarket USDC (you have $2000 deposited)
    polymarket_usdc = "2000.00"
    await ctx.send(f"📊 **Portfolio Snapshot**\n**Kalshi Cash:** $0.00 (API issue - parked)\n**Robinhood Buying Power:** ${robinhood_bp} (placeholder)\n**Polymarket USDC:** ${polymarket_usdc}")

@bot.command()
async def cycle(ctx):
    print("[DEBUG] Starting !cycle market scan")
    await ctx.send("🔎 Scanning Polymarket for high EV opportunities...")
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get("https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50", headers=headers, timeout=15)
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
