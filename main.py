import discord
from discord.ext import commands
import os
import requests

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TOKEN = os.getenv('DISCORD_TOKEN')

# --- API Keys ---
RH_API_KEY = os.getenv('ROBINHOOD_API_KEY')
RH_PRIVATE_KEY = os.getenv('ROBINHOOD_PRIVATE_KEY')
RH_PUBLIC_KEY = os.getenv('ROBINHOOD_PUBLIC_KEY')

POLY_API_KEY = os.getenv('POLYMARKET_API_KEY')
POLY_PASSPHRASE = os.getenv('POLYMARKET_PASSPHRASE')
POLY_SECRET = os.getenv('POLYMARKET_SECRET')

KALSHI_API_KEY = os.getenv('KALSHI_API_KEY')

# --- Robinhood: Fetch buying power ---
def get_robinhood_buying_power():
        try:
                    url = "https://trading.robinhood.com/api/v1/crypto/trading/accounts/"
                    headers = {
                        "x-api-key": RH_API_KEY,
                        "Content-Type": "application/json"
                    }
                    r = requests.get(url, headers=headers, timeout=10)
                    data = r.json()
                    buying_power = data.get('buying_power', 'N/A')
                    currency = data.get('buying_power_currency', 'USD')
                    return f"${buying_power} {currency}"
except Exception as e:
        return f"Error: {e}"

# --- Polymarket: Fetch USDC balance ---
def get_polymarket_balance():
        try:
                    url = "https://clob.polymarket.com/balance"
                    headers = {
                        "POLY_ADDRESS": POLY_API_KEY,
                        "POLY_PASSPHRASE": POLY_PASSPHRASE,
                        "POLY_SECRET": POLY_SECRET,
                        "Content-Type": "application/json"
                    }
                    r = requests.get(url, headers=headers, timeout=10)
                    data = r.json()
                    balance = data.get('balance', 'N/A')
                    return f"${balance} USDC"
except Exception as e:
        return f"Error: {e}"

# --- Kalshi: Fetch balance ---
def get_kalshi_balance():
        try:
                    url = "https://trading.kalshi.com/trade-api/v2/portfolio/balance"
                    headers = {
                        "Authorization": f"Token {KALSHI_API_KEY}",
                        "Content-Type": "application/json"
                    }
                    r = requests.get(url, headers=headers, timeout=10)
                    data = r.json()
                    balance = data.get('balance', 0)
                    return f"${balance / 100:.2f}"
except Exception as e:
        return f"Error: {e}"

# --- Polymarket: Fetch open markets for cycle scan ---
def get_polymarket_opportunities():
        try:
                    url = "https://clob.polymarket.com/markets?active=true&closed=false"
                    r = requests.get(url, timeout=10)
                    markets = r.json().get('data', [])
                    top = [m['question'] for m in markets[:3]]
                    return "\n".join([f"• {q}" for q in top]) if top else "No markets found"
except Exception as e:
        return f"Error: {e}"

@bot.event
async def on_ready():
        print(f'Logged in as {bot.user} — Full Firm Bot Live')

@bot.command()
async def ping(ctx):
        await ctx.send("Pong! Bot is alive on Render Background Worker.")

@bot.command()
async def portfolio(ctx):
        print("[DEBUG] Running !portfolio")
        await ctx.send("Fetching live portfolio data...")
        rh = get_robinhood_buying_power()
        poly = get_polymarket_balance()
        kalshi = get_kalshi_balance()
        await ctx.send(
            f"📊 **Live Portfolio Snapshot**\n"
            f"**Kalshi Cash:** {kalshi}\n"
            f"**Robinhood Buying Power:** {rh}\n"
            f"**Polymarket USDC:** {poly}\n"
            f"**PredictIt:** $0.00\n"
            f"**Interactive Brokers:** Skipped\n"
            f"**Coinbase:** No keys\n"
            f"**Phemex:** No keys"
        )

@bot.command()
async def cycle(ctx):
        print("[DEBUG] Starting !cycle market scan")
        await ctx.send("🔎 Scanning Polymarket for live opportunities...")
        opps = get_polymarket_opportunities()
        await ctx.send(f"📈 **Top Polymarket Markets Right Now:**\n{opps}")

bot.run(TOKEN)
