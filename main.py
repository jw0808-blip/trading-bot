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
    print(f'Logged in as {bot.user} — Full Firm Bot Live')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! Bot is alive on Render Background Worker.")

@bot.command()
async def portfolio(ctx):
    await ctx.send("📊 **Portfolio Snapshot**\n**Kalshi Cash:** $0.00 (parked)\n**Robinhood Buying Power:** $0.00 (keys loaded)\n**Polymarket USDC:** $2,000\n**PredictIt:** $0.00\n**Interactive Brokers:** Checking...\n**Coinbase:** Checking...")

@bot.command()
async def cycle(ctx):
    await ctx.send("🔎 Scanning Robinhood + Polymarket + PredictIt for high EV opportunities...")

bot.run(TOKEN)
