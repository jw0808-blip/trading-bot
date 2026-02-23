import discord
from discord.ext import commands
import os
import requests
import base64
import hashlib
import hmac
import time

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

TOKEN = os.getenv('DISCORD_TOKEN')

# --- API Keys ---
RH_API_KEY = os.getenv('ROBINHOOD_API_KEY')
RH_PRIVATE_KEY = os.getenv('ROBINHOOD_PRIVATE_KEY')

POLY_API_KEY = os.getenv('POLYMARKET_API_KEY')
POLY_PASSPHRASE = os.getenv('POLYMARKET_PASSPHRASE')
POLY_SECRET = os.getenv('POLYMARKET_SECRET')

KALSHI_API_KEY = os.getenv('KALSHI_API_KEY')


def get_robinhood_buying_power():
      try:
                timestamp = str(int(time.time()))
                path = "/api/v1/crypto/trading/accounts/"
                method = "GET"
                body = ""
                message = f"{RH_API_KEY}{timestamp}{path}{method}{body}"
                private_key_bytes = base64.b64decode(RH_PRIVATE_KEY)
                signature = base64.b64encode(
                    hmac.new(private_key_bytes, message.encode("utf-8"), hashlib.sha256).digest()
                ).decode("utf-8")
                headers = {
                    "x-api-key": RH_API_KEY,
                    "x-timestamp": timestamp,
                    "x-signature": signature,
                    "Content-Type": "application/json"
                }
                url = f"https://trading.robinhood.com{path}"
                r = requests.get(url, headers=headers, timeout=10)
                data = r.json()
                buying_power = data.get('buying_power', 'N/A')
                currency = data.get('buying_power_currency', 'USD')
                return f"${buying_power} {currency}"
except Exception as e:
        return f"Error: {e}"


def get_polymarket_balance():
      try:
                timestamp = str(int(time.time()))
                method = "GET"
                path = "/balance"
                secret_bytes = base64.b64decode(POLY_SECRET)
                message = f"{timestamp}{method}{path}"
                sig = hmac.new(secret_bytes, message.encode("utf-8"), hashlib.sha256).digest()
                sig_b64 = base64.b64encode(sig).decode("utf-8")
                headers = {
                    "POLY_ADDRESS": POLY_API_KEY,
                    "POLY_SIGNATURE": sig_b64,
                    "POLY_TIMESTAMP": timestamp,
                    "POLY_PASSPHRASE": POLY_PASSPHRASE,
                    "Content-Type": "application/json"
                }
                r = requests.get("https://clob.polymarket.com/balance", headers=headers, timeout=10)
                data = r.json()
                balance = data.get('balance', 'N/A')
                return f"${balance} USDC"
except Exception as e:
        return f"Error: {e}"


def get_kalshi_balance():
      try:
                headers = {
                              "Authorization": f"Token {KALSHI_API_KEY}",
                              "Content-Type": "application/json"
                }
                r = requests.get(
                    "https://trading.kalshi.com/trade-api/v2/portfolio/balance",
                    headers=headers,
                    timeout=10
                )
                data = r.json()
                balance = data.get('balance', 0)
                return f"${balance / 100:.2f}"
except Exception as e:
        return f"Error: {e}"


def get_polymarket_opportunities():
      try:
                r = requests.get(
                              "https://clob.polymarket.com/markets?active=true&closed=false",
                              timeout=10
                )
                markets = r.json().get('data', [])
                top = [m['question'] for m in markets[:3]]
                return "\n".join([f"* {q}" for q in top]) if top else "No markets found"
except Exception as e:
        return f"Error: {e}"


@bot.event
async def on_ready():
      print(f'Logged in as {bot.user} - Full Firm Bot Live')


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
      msg = (
          "**Live Portfolio Snapshot**\n"
          f"**Kalshi Cash:** {kalshi}\n"
          f"**Robinhood Buying Power:** {rh}\n"
          f"**Polymarket USDC:** {poly}\n"
          "**PredictIt:** $0.00\n"
          "**Interactive Brokers:** Skipped\n"
          "**Coinbase:** No keys\n"
          "**Phemex:** No keys"
      )
      await ctx.send(msg)


@bot.command()
async def cycle(ctx):
      print("[DEBUG] Starting !cycle market scan")
      await ctx.send("Scanning Polymarket for live opportunities...")
      opps = get_polymarket_opportunities()
      await ctx.send(f"**Top Polymarket Markets Right Now:**\n{opps}")


bot.run(TOKEN)
