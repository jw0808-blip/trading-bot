import os
import discord
import requests
import hashlib
import hmac
import time
import json
import base64
import asyncio
from datetime import datetime
from discord.ext import commands
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

# ─── CONFIG ────────────────────────────────────────────────────────────────────
DISCORD_TOKEN        = os.environ.get("DISCORD_TOKEN")
KALSHI_API_KEY_ID    = os.environ.get("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY   = os.environ.get("KALSHI_PRIVATE_KEY", "")
POLY_API_KEY         = os.environ.get("POLYMARKET_API_KEY")
POLY_SECRET          = os.environ.get("POLYMARKET_SECRET")
POLY_PASSPHRASE      = os.environ.get("POLYMARKET_PASSPHRASE")
POLY_ADDRESS         = os.environ.get("POLY_ADDRESS")
ROBINHOOD_TOKEN      = os.environ.get("ROBINHOOD_TOKEN")
PREDICTIT_TOKEN      = os.environ.get("PREDICTIT_TOKEN")
COINBASE_API_KEY     = os.environ.get("COINBASE_API_KEY")
COINBASE_API_SECRET  = os.environ.get("COINBASE_API_SECRET")
GITHUB_TOKEN         = os.environ.get("GITHUB_TOKEN")
DISCORD_LOG_CHANNEL  = os.environ.get("DISCORD_LOG_CHANNEL_ID")

# ─── KALSHI ─────────────────────────────────────────────────────────────────── 
# FIX #1: correct domain  api.elections.kalshi.com
# FIX #2: PSS padding instead of PKCS1v15
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

def kalshi_request(method, path, body=None):
    """Sign and execute a Kalshi API request using RSA-PSS."""
    timestamp_ms = str(int(time.time() * 1000))
    body_str = json.dumps(body) if body else ""
    msg = timestamp_ms + method.upper() + path + body_str

    try:
        pem = KALSHI_PRIVATE_KEY.replace("\\n", "\n").encode()
        private_key = serialization.load_pem_private_key(pem, password=None, backend=default_backend())
        sig = private_key.sign(
            msg.encode("utf-8"),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=hashes.SHA256.digest_size
            ),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(sig).decode()
    except Exception as e:
        return {"error": f"Signing failed: {e}"}

    headers = {
        "Content-Type": "application/json",
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-SIGNATURE": sig_b64,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
    }
    url = KALSHI_BASE + path
    try:
        if method.upper() == "GET":
            resp = requests.get(url, headers=headers, timeout=10)
        else:
            resp = requests.post(url, headers=headers, json=body, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.HTTPError as e:
        return {"error": f"HTTP {resp.status_code}: {resp.text}"}
    except Exception as e:
        return {"error": str(e)}


def get_kalshi_balance():
    """Get Kalshi balance in dollars."""
    data = kalshi_request("GET", "/portfolio/balance")
    if "error" in data:
        return None, data["error"]
    cents = data.get("balance", 0)
    return cents / 100.0, None


# ─── POLYMARKET ──────────────────────────────────────────────────────────────
# FIX: CLOB has no /balance endpoint — balance lives on Polygon blockchain
# Query USDC balance via Polygon RPC

USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"  # USDC on Polygon
POLYGON_RPC   = "https://polygon-rpc.com"

def get_polymarket_balance():
    """Get Polymarket USDC balance from Polygon blockchain."""
    if not POLY_ADDRESS:
        return None, "POLY_ADDRESS not set"
    try:
        # ERC-20 balanceOf call
        addr_padded = POLY_ADDRESS.lower().replace("0x", "").zfill(64)
        data = "0x70a08231" + addr_padded  # balanceOf(address)
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": USDC_CONTRACT, "data": data}, "latest"],
            "id": 1
        }
        resp = requests.post(POLYGON_RPC, json=payload, timeout=10)
        result = resp.json().get("result", "0x0")
        balance_raw = int(result, 16)
        balance_usdc = balance_raw / 1e6  # USDC has 6 decimals
        return balance_usdc, None
    except Exception as e:
        return None, str(e)


# ─── ROBINHOOD ───────────────────────────────────────────────────────────────
def get_robinhood_balance():
    if not ROBINHOOD_TOKEN:
        return None, "No token"
    try:
        headers = {"Authorization": f"Bearer {ROBINHOOD_TOKEN}"}
        resp = requests.get("https://api.robinhood.com/accounts/", headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            results = data.get("results", [])
            if results:
                cash = float(results[0].get("cash", 0))
                return cash, None
        return None, f"HTTP {resp.status_code}"
    except Exception as e:
        return None, str(e)


# ─── PREDICTIT ───────────────────────────────────────────────────────────────
def get_predictit_balance():
    if not PREDICTIT_TOKEN:
        return None, "No token"
    try:
        headers = {"Authorization": f"Bearer {PREDICTIT_TOKEN}"}
        resp = requests.get("https://www.predictit.org/api/account/wallet/balance",
                            headers=headers, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            return float(data.get("balance", 0)), None
        return None, f"HTTP {resp.status_code}"
    except Exception as e:
        return None, str(e)


# ─── COINBASE ────────────────────────────────────────────────────────────────
def get_coinbase_balance():
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return None, "No keys"
    try:
        timestamp = str(int(time.time()))
        message = timestamp + "GET" + "/v2/accounts" + ""
        sig = hmac.new(
            COINBASE_API_SECRET.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        headers = {
            "CB-ACCESS-KEY": COINBASE_API_KEY,
            "CB-ACCESS-SIGN": sig,
            "CB-ACCESS-TIMESTAMP": timestamp,
            "CB-VERSION": "2016-02-18",
        }
        resp = requests.get("https://api.coinbase.com/v2/accounts",
                            headers=headers, timeout=10)
        if resp.status_code == 200:
            accounts = resp.json().get("data", [])
            usd = sum(float(a["balance"]["amount"])
                      for a in accounts
                      if a.get("currency") in ("USD", "USDC"))
            return usd, None
        return None, f"HTTP {resp.status_code}"
    except Exception as e:
        return None, str(e)


# ─── GITHUB LOGGER ───────────────────────────────────────────────────────────
def log_to_github(entry: str):
    """Append an entry to conversations.md in the GitHub repo."""
    if not GITHUB_TOKEN:
        return
    try:
        api = "https://api.github.com/repos/jw0808-blip/trading-bot/contents/conversations.md"
        headers = {
            "Authorization": f"token {GITHUB_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }
        r = requests.get(api, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            current = base64.b64decode(data["content"]).decode("utf-8")
            sha = data["sha"]
        else:
            current = "# TraderJoes AI Conversation Log\n\nAll bot activity is automatically logged here.\n\n---\n\n"
            sha = None

        new_content = current + entry + "\n"
        encoded = base64.b64encode(new_content.encode()).decode()
        payload = {
            "message": f"Bot log: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')}",
            "content": encoded
        }
        if sha:
            payload["sha"] = sha
        requests.put(api, headers=headers, json=payload, timeout=15)
    except Exception:
        pass


# ─── DISCORD BOT ─────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    print(f"✅ TraderJoes bot online as {bot.user}")
    await log_event("🤖 Bot started", "TraderJoes Trading Firm bot is online and ready.")


async def log_event(title: str, detail: str):
    """Log an event to Discord #ai-logs and GitHub."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    md_entry = f"### {ts} — {title}\n{detail}\n"
    
    # GitHub
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, log_to_github, md_entry)
    
    # Discord
    if DISCORD_LOG_CHANNEL:
        try:
            ch = bot.get_channel(int(DISCORD_LOG_CHANNEL))
            if ch:
                embed = discord.Embed(title=title, description=detail,
                                      color=0x00ff88, timestamp=datetime.utcnow())
                await ch.send(embed=embed)
        except Exception:
            pass


@bot.command(name="ping")
async def ping(ctx):
    await ctx.send("🏓 Pong! TraderJoes bot is live.")


@bot.command(name="portfolio")
async def portfolio(ctx):
    """Show live portfolio balances across all platforms."""
    msg = await ctx.send("⏳ Fetching live balances...")
    
    balances = {}
    errors = {}

    kalshi_bal, kalshi_err = get_kalshi_balance()
    if kalshi_bal is not None:
        balances["Kalshi"] = f"${kalshi_bal:,.2f}"
    else:
        errors["Kalshi"] = kalshi_err

    poly_bal, poly_err = get_polymarket_balance()
    if poly_bal is not None:
        balances["Polymarket"] = f"${poly_bal:,.2f} USDC"
    else:
        errors["Polymarket"] = poly_err

    rh_bal, rh_err = get_robinhood_balance()
    if rh_bal is not None:
        balances["Robinhood"] = f"${rh_bal:,.2f}"
    else:
        errors["Robinhood"] = rh_err

    pi_bal, pi_err = get_predictit_balance()
    if pi_bal is not None:
        balances["PredictIt"] = f"${pi_bal:,.2f}"
    else:
        errors["PredictIt"] = pi_err

    cb_bal, cb_err = get_coinbase_balance()
    if cb_bal is not None:
        balances["Coinbase"] = f"${cb_bal:,.2f}"
    else:
        errors["Coinbase"] = cb_err

    embed = discord.Embed(
        title="💼 TraderJoes Portfolio",
        color=0x00ff88,
        timestamp=datetime.utcnow()
    )
    
    total = 0.0
    for platform, bal in balances.items():
        embed.add_field(name=platform, value=f"✅ {bal}", inline=True)
        try:
            total += float(bal.replace("$","").replace(",","").replace(" USDC",""))
        except Exception:
            pass

    for platform, err in errors.items():
        embed.add_field(name=platform, value=f"❌ {err}", inline=True)

    embed.add_field(name="─────────────", value=f"**Total: ${total:,.2f}**", inline=False)
    embed.set_footer(text="TraderJoes Trading Firm | 21 Sub-Bots")

    await msg.edit(content="", embed=embed)

    # Log to GitHub and #ai-logs
    detail = "\n".join([f"- {k}: {v}" for k, v in balances.items()])
    if errors:
        detail += "\n**Errors:**\n" + "\n".join([f"- {k}: {v}" for k, v in errors.items()])
    await log_event("📊 Portfolio Snapshot", detail)


@bot.command(name="log")
async def manual_log(ctx, *, message: str):
    """Manually log a conversation snippet to GitHub and #ai-logs."""
    ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = f"### {ts} — Manual Log by {ctx.author.name}\n{message}\n"
    
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, log_to_github, entry)
    
    await ctx.send(f"✅ Logged to conversations.md and GitHub!")
    await log_event(f"📝 Manual Log by {ctx.author.name}", message)


@bot.command(name="cycle")
async def cycle(ctx):
    """Run EV scan across all platforms."""
    await ctx.send("🔄 Running EV cycle scan across all platforms... (stub - connect your 21 sub-bots here)")
    await log_event("🔄 EV Cycle Scan", f"Triggered by {ctx.author.name}")


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
