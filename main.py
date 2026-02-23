import discord
from discord.ext import commands
import os
import requests
import base64
import hashlib
import hmac
import time
from nacl.signing import SigningKey
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

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

KALSHI_API_KEY_ID = os.getenv('KALSHI_API_KEY_ID')
KALSHI_PRIVATE_KEY_PEM = os.getenv('KALSHI_PRIVATE_KEY_PEM')


def get_robinhood_buying_power():
        try:
                    timestamp = str(int(time.time()))
                    path = "/api/v1/crypto/trading/accounts/"
                    method = "GET"
                    body = ""
                    message = RH_API_KEY + timestamp + path + method + body
                    private_key_bytes = base64.b64decode(RH_PRIVATE_KEY)
                    signing_key = SigningKey(private_key_bytes)
                    signed = signing_key.sign(message.encode("utf-8"))
                    signature = base64.b64encode(signed.signature).decode("utf-8")
                    headers = {
                        "x-api-key": RH_API_KEY,
                        "x-signature": signature,
                        "x-timestamp": timestamp,
                    }
                    r = requests.get(
                        "https://trading.robinhood.com/api/v1/crypto/trading/accounts/",
                        headers=headers,
                        timeout=10
                    )
                    print("[DEBUG] Robinhood status: " + str(r.status_code) + " - " + r.text[:200])
                    data = r.json()
                    buying_power = data.get('buying_power', 'N/A')
                    currency = data.get('buying_power_currency', 'USD')
                    return "$" + str(buying_power) + " " + currency
except Exception as e:
        return "Error: " + str(e)


def get_polymarket_balance():
        try:
                    timestamp = str(int(time.time()))
                    method = "GET"
                    path = "/balance"
                    secret_bytes = base64.b64decode(POLY_SECRET)
                    message = timestamp + method + path
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
                    print("[DEBUG] Polymarket status: " + str(r.status_code) + " - " + r.text[:200])
                    if r.status_code == 200:
                                    data = r.json()
                                    balance = data.get('balance', 'N/A')
                                    return "$" + str(balance) + " USDC"
        else:
                        return "Error " + str(r.status_code) + ": " + r.text[:100]
except Exception as e:
        return "Error: " + str(e)


def get_kalshi_balance():
        try:
                    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY_PEM:
                                    return "Error: Kalshi keys not loaded in env"
                                private_key = serialization.load_pem_private_key(
                        KALSHI_PRIVATE_KEY_PEM.encode('utf-8'),
                        password=None
                    )
                    timestamp_ms = str(int(time.time() * 1000))
                    method = "GET"
                    path = "/trade-api/v2/portfolio/balance"
                    msg_string = timestamp_ms + method + path
                    signature = private_key.sign(
                        msg_string.encode('utf-8'),
                        padding.PKCS1v15(),
                        hashes.SHA256()
                    )
                    sig_b64 = base64.b64encode(signature).decode('utf-8')
                    headers = {
                        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
                        "KALSHI-ACCESS-SIGNATURE": sig_b64,
                        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
                        "Content-Type": "application/json"
                    }
                    r = requests.get(
                        "https://trading.kalshi.com/trade-api/v2/portfolio/balance",
                        headers=headers,
                        timeout=10
                    )
                    print("[DEBUG] Kalshi status: " + str(r.status_code) + " - " + r.text[:200])
                    if r.status_code == 200:
                                    data = r.json()
                                    balance = data.get('balance', 0)
                                    return "$" + str(round(balance / 100, 2)) + " USD"
        else:
                        return "Error " + str(r.status_code) + ": " + r.text[:100]
except Exception as e:
        return "Error: " + str(e)


def get_polymarket_opportunities():
        try:
                    r = requests.get(
                                    "https://clob.polymarket.com/markets?active=true&closed=false",
                                    timeout=10
                    )
                    markets = r.json().get('data', [])
                    results = []
                    for m in markets[:15]:
                                    question = m.get('question', 'Unknown')
                                    tokens = m.get('tokens', [])
                                    yes_price = None
                                    no_price = None
                                    for t in tokens:
                                                        if t.get('outcome') == 'Yes':
                                                                                yes_price = float(t.get('price', 0))
elif t.get('outcome') == 'No':
                    no_price = float(t.get('price', 0))
            if yes_price is not None and no_price is not None:
                                edge = abs(yes_price - (1 - no_price))
                                ev = round(edge * 100, 2)
                                if ev > 1:
                                                        results.append(
                                                                                    "**" + question + "**\nYes: " + str(round(yes_price, 2)) +
                                                                                    " | No: " + str(round(no_price, 2)) + " | Edge: " + str(ev) + "%"
                                                        )
                                            if results:
                            return "\n\n".join(results[:5])
        return "No high EV opportunities found right now."
except Exception as e:
        return "Error: " + str(e)


@bot.event
async def on_ready():
        print('[BOT] Logged in as ' + str(bot.user) + ' - TraderJoes Firm Bot LIVE')


@bot.command()
async def ping(ctx):
        await ctx.send('Pong! TraderJoes Bot is alive on Render.')


@bot.command()
async def portfolio(ctx):
        print('[DEBUG] Running !portfolio')
    await ctx.send('Fetching live portfolio data...')
    rh = get_robinhood_buying_power()
    poly = get_polymarket_balance()
    kalshi = get_kalshi_balance()
    msg = (
                '**TraderJoes Live Portfolio Snapshot**\n'
                '**Kalshi Cash:** ' + kalshi + '\n'
                '**Robinhood Buying Power:** ' + rh + '\n'
                '**Polymarket USDC:** ' + poly + '\n'
                '**PredictIt:** Pending integration\n'
                '**Interactive Brokers:** Pending API access\n'
                '**Coinbase:** Keys needed\n'
                '**Phemex:** Keys needed'
    )
    await ctx.send(msg)


@bot.command()
async def cycle(ctx):
        print('[DEBUG] Starting !cycle market scan')
    await ctx.send('Scanning Polymarket for high EV opportunities...')
    opps = get_polymarket_opportunities()
    await ctx.send('**Top Polymarket Opportunities:**\n\n' + opps)


bot.run(TOKEN)
