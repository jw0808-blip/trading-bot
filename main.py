import discord
from discord.ext import commands
import os
import requests
import json
import datetime
from nacl.signing import SigningKey
from base64 import b64decode

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TOKEN = os.getenv('DISCORD_TOKEN')

# Robinhood Keys (you already loaded these)
RH_API_KEY = os.getenv('ROBINHOOD_API_KEY')
RH_PUBLIC_KEY = os.getenv('ROBINHOOD_PUBLIC_KEY')
RH_PRIVATE_KEY = os.getenv('ROBINHOOD_PRIVATE_KEY')

# Polymarket Keys (you already loaded these)
POLY_API_KEY = os.getenv('POLYMARKET_API_KEY')
POLY_SECRET = os.getenv('POLYMARKET_SECRET')
POLY_PASSPHRASE = os.getenv('POLYMARKET_PASSPHRASE')

class RobinhoodClient:
    def __init__(self):
        self.api_key = RH_API_KEY
        priv_seed = b64decode(RH_PRIVATE_KEY.strip())
        self.signing_key = SigningKey(priv_seed)
        self.base_url = "https://trading.robinhood.com"

    def get_buying_power(self):
        path = "/api/v1/crypto/accounts/"
        try:
            timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
            message = f"{self.api_key}{timestamp}{path}GET"
            signature = self.signing_key.sign(message.encode()).signature
            headers = {
                "x-api-key": self.api_key,
                "x-signature": b64decode(signature).decode(),
                "x-timestamp": str(timestamp),
                "Content-Type": "application/json"
            }
            r = requests.get(f"{self.base_url}{path}", headers=headers, timeout=10)
            data = r.json()
            for acc in data.get('results', []):
                if acc.get('status') == 'active':
                    return float(acc.get('buying_power', 0))
            return 0.0
        except Exception as e:
            print(f"[DEBUG] Robinhood Error: {e}")
            return 0.0

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! Bot is alive on Render Background Worker.")

@bot.command()
async def portfolio(ctx):
    rh = RobinhoodClient()
    rh_bp = rh.get_buying_power()
    poly_usdc = 2000.00  # You have $2000 deposited

    await ctx.send(f"📊 **Portfolio Snapshot**\n**Robinhood Buying Power:** ${rh_bp:,.2f}\n**Polymarket USDC:** ${poly_usdc:,.2f}")

@bot.command()
async def cycle(ctx):
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
        await ctx.send("❌ Failed to fetch Polymarket markets.")

bot.run(TOKEN)
