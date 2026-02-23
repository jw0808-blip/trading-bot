import discord
from discord.ext import commands
import os, requests, base64, time
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN")
KALSHI_API_KEY_ID = os.environ.get("KALSHI_API_KEY_ID")
KALSHI_PRIVATE_KEY = os.environ.get("KALSHI_PRIVATE_KEY", "")
POLY_WALLET_ADDRESS = os.environ.get("POLY_WALLET_ADDRESS", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "jw0808-blip/trading-bot")

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"

def kalshi_sign(method, path):
    ts = str(int(time.time() * 1000))
    msg = ts + method.upper() + path
    try:
        key_pem = KALSHI_PRIVATE_KEY.replace("\\n", "\n")
        private_key = serialization.load_pem_private_key(key_pem.encode(), password=None, backend=default_backend())
        sig = private_key.sign(msg.encode(), padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256().digest_size), hashes.SHA256())
        return ts, base64.b64encode(sig).decode()
    except Exception as e:
        return ts, ""

def get_kalshi_balance():
    path = "/portfolio/balance"
    ts, sig = kalshi_sign("GET", path)
    headers = {"KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID, "KALSHI-ACCESS-TIMESTAMP": ts, "KALSHI-ACCESS-SIGNATURE": sig}
    try:
        r = requests.get(KALSHI_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            return f"${r.json().get('balance', 0)/100:,.2f}"
        return f"Error {r.status_code}"
    except Exception as e:
        return f"Exception: {e}"

def get_polymarket_balance():
    if not POLY_WALLET_ADDRESS:
        return "Set POLY_WALLET_ADDRESS env var"
    usdc = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    addr = POLY_WALLET_ADDRESS.lower().replace("0x","").zfill(64)
    data = "0x70a08231" + addr
    payload = {"jsonrpc":"2.0","method":"eth_call","params":[{"to":usdc,"data":data},"latest"],"id":1}
    for rpc in ["https://polygon-rpc.com","https://rpc.ankr.com/polygon"]:
        try:
            r = requests.post(rpc, json=payload, timeout=10)
            if r.status_code == 200:
                result = r.json().get("result","0x0")
                return f"${int(result,16)/1_000_000:,.2f}"
        except: continue
    return "RPC unavailable"

def log_to_github(entry):
    if not GITHUB_TOKEN: return
    api = f"https://api.github.com/repos/{GITHUB_REPO}/contents/conversations.md"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    try:
        r = requests.get(api, headers=headers, timeout=10)
        if r.status_code == 200:
            d = r.json(); current = base64.b64decode(d["content"]).decode(); sha = d["sha"]
        else:
            current = "# TraderJoes Log\n\n---\n\n"; sha = None
        new = current + entry
        p = {"message": "Bot log", "content": base64.b64encode(new.encode()).decode()}
        if sha: p["sha"] = sha
        requests.put(api, headers=headers, json=p, timeout=15)
    except: pass

@bot.event
async def on_ready():
    print(f"TraderJoes bot online as {bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("Pong! TraderJoes bot is live.")

@bot.command()
async def portfolio(ctx):
    await ctx.send("Fetching portfolio balances...")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    kalshi = get_kalshi_balance()
    poly = get_polymarket_balance()
    msg = f"**TraderJoes Portfolio** | {ts}\n**Kalshi:** {kalshi}\n**Polymarket:** {poly}"
    await ctx.send(msg)
    log_to_github(f"\n## Portfolio Snapshot - {ts}\n- Kalshi: {kalshi}\n- Polymarket: {poly}\n\n")

@bot.command()
async def log(ctx, *, message: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    log_to_github(f"\n## Manual Log - {ts}\n**Author:** {ctx.author}\n{message}\n\n")
    await ctx.send(f"Logged to conversations.md")

@bot.command()
async def cycle(ctx):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    await ctx.send(f"EV scan complete at {ts}")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
