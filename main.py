"""
TraderJoes Trading Firm — Discord Bot
=====================================
Multi-platform portfolio viewer and EV scanner.
Platforms: Kalshi, Polymarket, Robinhood (Crypto), Coinbase (Advanced Trade), Phemex
"""

import discord
from discord.ext import commands
import os
import requests
import json
import time
import base64
import hmac
import hashlib
import uuid
import logging
import jwt as pyjwt
from datetime import datetime, timezone
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, ed25519, ec
from cryptography.hazmat.backends import default_backend

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("traderjoes")

# ---------------------------------------------------------------------------
# Discord setup
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# Environment variables
# ---------------------------------------------------------------------------
DISCORD_TOKEN       = os.environ.get("DISCORD_TOKEN")
DISCORD_CHANNEL_ID  = os.environ.get("DISCORD_CHANNEL_ID", "")

# Kalshi
KALSHI_API_KEY_ID   = os.environ.get("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY  = os.environ.get("KALSHI_PRIVATE_KEY", "")
KALSHI_BASE         = "https://api.elections.kalshi.com/trade-api/v2"

# Polymarket
POLY_WALLET_ADDRESS     = os.environ.get("POLY_WALLET_ADDRESS", "")
POLYMARKET_API_KEY      = os.environ.get("POLYMARKET_API_KEY", "")
POLYMARKET_SECRET       = os.environ.get("POLYMARKET_SECRET", "")
POLYMARKET_PASSPHRASE   = os.environ.get("POLYMARKET_PASSPHRASE", "")

# Robinhood Crypto API
ROBINHOOD_API_KEY       = os.environ.get("ROBINHOOD_API_KEY", "")
ROBINHOOD_PRIVATE_KEY   = os.environ.get("ROBINHOOD_PRIVATE_KEY", "")
ROBINHOOD_PUBLIC_KEY    = os.environ.get("ROBINHOOD_PUBLIC_KEY", "")
ROBINHOOD_BASE          = "https://trading.robinhood.com"

# Coinbase Advanced Trade (CDP API Keys - JWT auth)
COINBASE_API_KEY        = os.environ.get("COINBASE_API_KEY", "")
COINBASE_API_SECRET     = os.environ.get("COINBASE_API_SECRET", "")

# Phemex
PHEMEX_API_KEY      = os.environ.get("PHEMEX_API_KEY", "")
PHEMEX_API_SECRET   = os.environ.get("PHEMEX_API_SECRET", "")
PHEMEX_BASE         = "https://api.phemex.com"

# GitHub logging
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "jw0808-blip/trading-bot")


# ============================================================================
# KALSHI  (RSA-PSS signing — path MUST include /trade-api/v2 prefix)
# ============================================================================

def kalshi_sign(method, path):
    """Sign a Kalshi API request. path should NOT include the base URL prefix."""
    ts = str(int(time.time() * 1000))
    # The signature message is: timestamp + METHOD + /trade-api/v2 + path
    full_path = "/trade-api/v2" + path
    msg = ts + method.upper() + full_path
    try:
        key_pem = KALSHI_PRIVATE_KEY.replace("\\n", "\n")
        private_key = serialization.load_pem_private_key(
            key_pem.encode(), password=None, backend=default_backend()
        )
        sig = private_key.sign(
            msg.encode(),
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return ts, base64.b64encode(sig).decode()
    except Exception as exc:
        log.warning("Kalshi sign error: %s", exc)
        return ts, ""


def get_kalshi_balance():
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY:
        return "Keys not configured"
    path = "/portfolio/balance"
    ts, sig = kalshi_sign("GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(KALSHI_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            cents = r.json().get("balance", 0)
            return f"${cents / 100:,.2f}"
        return f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as exc:
        return f"Error: {exc}"


def get_kalshi_events(limit=20):
    path = "/events"
    ts, sig = kalshi_sign("GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(
            KALSHI_BASE + path,
            headers=headers,
            params={"limit": limit, "status": "open"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("events", [])
    except Exception as exc:
        log.warning("Kalshi events fetch error: %s", exc)
    return []


def get_kalshi_markets_for_event(event_ticker):
    path = "/markets"
    ts, sig = kalshi_sign("GET", path)
    headers = {
        "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": sig,
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(
            KALSHI_BASE + path,
            headers=headers,
            params={"event_ticker": event_ticker, "status": "open"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("markets", [])
    except Exception as exc:
        log.warning("Kalshi markets fetch error: %s", exc)
    return []


# ============================================================================
# POLYMARKET
# ============================================================================

def _get_polymarket_usdc_onchain():
    """Check on-chain USDC balance (cash not in positions)."""
    if not POLY_WALLET_ADDRESS:
        return 0.0, []
    usdc_contracts = [
        ("USDC.e", "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"),
        ("USDC",   "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"),
    ]
    addr_padded = POLY_WALLET_ADDRESS.lower().replace("0x", "").zfill(64)
    rpcs = ["https://polygon-rpc.com", "https://rpc.ankr.com/polygon"]
    total = 0.0
    details = []
    for label, contract in usdc_contracts:
        call_data = "0x70a08231" + addr_padded
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": contract, "data": call_data}, "latest"],
            "id": 1,
        }
        for rpc in rpcs:
            try:
                r = requests.post(rpc, json=payload, timeout=10)
                if r.status_code == 200:
                    result = r.json().get("result", "0x0")
                    if result and result != "0x":
                        raw = int(result, 16)
                        bal = raw / 1_000_000
                        total += bal
                        if bal > 0.01:
                            details.append(f"{label}: ${bal:,.2f}")
                        break
            except Exception:
                continue
    return total, details


def _get_polymarket_positions():
    """Fetch trading positions from Polymarket Data API."""
    if not POLY_WALLET_ADDRESS:
        return 0.0, []
    try:
        r = requests.get(
            "https://data-api.polymarket.com/positions",
            params={
                "user": POLY_WALLET_ADDRESS,
                "sizeThreshold": 0.01,
                "limit": 100,
                "sortBy": "CURRENT",
                "sortDirection": "DESC",
            },
            timeout=15,
        )
        if r.status_code == 200:
            positions = r.json()
            if not isinstance(positions, list):
                positions = []
            total_value = 0.0
            pos_details = []
            for p in positions:
                size = float(p.get("size", 0))
                cur_price = float(p.get("curPrice", 0))
                current_val = float(p.get("currentValue", 0))
                title = p.get("title", "Unknown")
                outcome = p.get("outcome", "?")
                cash_pnl = float(p.get("cashPnl", 0))
                pct_pnl = float(p.get("percentPnl", 0))
                if size > 0.01:
                    total_value += current_val
                    pnl_str = f"+${cash_pnl:,.2f}" if cash_pnl >= 0 else f"-${abs(cash_pnl):,.2f}"
                    pct_str = f"+{pct_pnl:.1f}%" if pct_pnl >= 0 else f"{pct_pnl:.1f}%"
                    # Truncate long titles
                    short_title = title[:45] + "..." if len(title) > 45 else title
                    pos_details.append(
                        f"  {outcome} {short_title}\n"
                        f"    {size:,.1f} shares @ ${cur_price:.3f} = ${current_val:,.2f} ({pnl_str} / {pct_str})"
                    )
            log.info("Polymarket Data API: %d positions, total value $%.2f", len(positions), total_value)
            return total_value, pos_details
        else:
            log.warning("Polymarket Data API HTTP %d: %s", r.status_code, r.text[:200])
    except Exception as exc:
        log.warning("Polymarket Data API error: %s", exc)
    return 0.0, []


def get_polymarket_balance():
    if not POLY_WALLET_ADDRESS:
        return "Wallet not configured"

    # 1) Fetch positions value from Data API
    positions_value, _ = _get_polymarket_positions()

    # 2) Fetch on-chain USDC (uninvested cash)
    cash_value, cash_details = _get_polymarket_usdc_onchain()

    total = positions_value + cash_value
    parts = []
    if positions_value > 0.01:
        parts.append(f"positions: ${positions_value:,.2f}")
    if cash_value > 0.01:
        parts.append(f"cash: ${cash_value:,.2f}")

    if parts:
        return f"${total:,.2f} ({', '.join(parts)})"
    if total < 0.01 and positions_value == 0 and cash_value == 0:
        return "$0.00 (no positions or cash)"
    return f"${total:,.2f}"


def get_polymarket_positions_detail():
    """Return formatted position details for portfolio display."""
    if not POLY_WALLET_ADDRESS:
        return ""
    _, pos_details = _get_polymarket_positions()
    if pos_details:
        return "\n".join(pos_details)
    return ""


def get_polymarket_markets(limit=20):
    """Fetch current, active markets from Polymarket Gamma API with CLOB fallback."""
    # --- Primary: Gamma API ---
    try:
        r = requests.get(
            "https://gamma-api.polymarket.com/markets",
            params={
                "limit": limit,
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
                "active": "true",
            },
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            markets = data if isinstance(data, list) else []
            if markets:
                log.info("Gamma API returned %d markets", len(markets))
                return markets
            log.warning("Gamma API returned empty list, trying CLOB fallback")
    except Exception as exc:
        log.warning("Gamma API failed (%s), trying CLOB fallback", exc)

    # --- Fallback: CLOB API ---
    try:
        log.info("Using CLOB API fallback for Polymarket markets")
        r = requests.get(
            "https://clob.polymarket.com/markets",
            params={"limit": limit, "active": "true"},
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            log.info("CLOB fallback returned %d markets", len(markets))
            return markets
    except Exception as exc:
        log.warning("CLOB API fallback also failed: %s", exc)
    return []


# ============================================================================
# ROBINHOOD (Crypto Trading API - Ed25519 auth)
# ============================================================================

def _robinhood_sign(method, path, body=""):
    if not ROBINHOOD_API_KEY or not ROBINHOOD_PRIVATE_KEY:
        return {}
    try:
        ts = int(datetime.now(timezone.utc).timestamp())
        message = f"{ROBINHOOD_API_KEY}{ts}{path}{method}{body}"
        private_bytes = base64.b64decode(ROBINHOOD_PRIVATE_KEY)
        priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(private_bytes[:32])
        signature = priv_key.sign(message.encode("utf-8"))
        sig_b64 = base64.b64encode(signature).decode("utf-8")
        return {
            "x-api-key": ROBINHOOD_API_KEY,
            "x-timestamp": str(ts),
            "x-signature": sig_b64,
            "Content-Type": "application/json",
        }
    except Exception as exc:
        log.warning("Robinhood sign error: %s", exc)
        return {}


def get_robinhood_balance():
    if not ROBINHOOD_API_KEY:
        return "Keys not configured"
    path = "/api/v1/crypto/trading/accounts/"
    headers = _robinhood_sign("GET", path)
    if not headers:
        return "Signing failed"
    try:
        r = requests.get(ROBINHOOD_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            data = r.json()
            bp = data.get("buying_power", "0")
            currency = data.get("buying_power_currency", "USD")
            return f"${float(bp):,.2f} {currency} (buying power)"
        return f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as exc:
        return f"Error: {exc}"


def get_robinhood_holdings():
    if not ROBINHOOD_API_KEY:
        return ""
    path = "/api/v1/crypto/trading/holdings/"
    headers = _robinhood_sign("GET", path)
    if not headers:
        return ""
    try:
        r = requests.get(ROBINHOOD_BASE + path, headers=headers, timeout=10)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if not results:
                return "  No crypto holdings"
            lines = []
            for h in results:
                code = h.get("asset_code", "?")
                qty  = h.get("total_quantity", "0")
                lines.append(f"  {code}: {qty}")
            return "\n".join(lines)
    except Exception:
        pass
    return ""


# ============================================================================
# COINBASE ADVANCED TRADE (CDP API Keys - JWT / ES256 auth)
# ============================================================================

def _coinbase_build_jwt(method, path):
    """Build a JWT for Coinbase Advanced Trade using CDP API keys (ES256)."""
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return None
    try:
        uri = f"{method.upper()} api.coinbase.com{path}"
        now = int(time.time())
        payload = {
            "sub": COINBASE_API_KEY,
            "iss": "coinbase-cloud",
            "aud": ["cdp_service"],
            "nbf": now,
            "exp": now + 120,
            "uri": uri,
        }
        secret = COINBASE_API_SECRET.replace("\\n", "\n")
        headers = {
            "kid": COINBASE_API_KEY,
            "nonce": hashlib.sha256(os.urandom(16)).hexdigest(),
            "typ": "JWT",
        }
        token = pyjwt.encode(payload, secret, algorithm="ES256", headers=headers)
        return token
    except Exception as exc:
        log.warning("Coinbase JWT build error: %s", exc)
        return None


def get_coinbase_balance():
    if not COINBASE_API_KEY:
        return "Keys not configured"
    path = "/api/v3/brokerage/accounts"
    token = _coinbase_build_jwt("GET", path)
    if not token:
        return "JWT build failed"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(
            "https://api.coinbase.com" + path, headers=headers, timeout=10
        )
        if r.status_code == 200:
            accounts = r.json().get("accounts", [])
            total_usd = 0.0
            holdings = []
            for acct in accounts:
                bal = acct.get("available_balance", {})
                value = float(bal.get("value", "0"))
                currency = bal.get("currency", "")
                if value > 0.001:
                    holdings.append(f"  {currency}: {value:,.6f}")
                    if currency == "USD":
                        total_usd += value
            summary = f"${total_usd:,.2f} USD"
            if holdings:
                summary += " + crypto"
            return summary
        return f"HTTP {r.status_code}: {r.text[:120]}"
    except Exception as exc:
        return f"Error: {exc}"


def get_coinbase_holdings_detail():
    if not COINBASE_API_KEY:
        return ""
    path = "/api/v3/brokerage/accounts"
    token = _coinbase_build_jwt("GET", path)
    if not token:
        return ""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.get(
            "https://api.coinbase.com" + path, headers=headers, timeout=10
        )
        if r.status_code == 200:
            accounts = r.json().get("accounts", [])
            lines = []
            for acct in accounts:
                bal = acct.get("available_balance", {})
                value = float(bal.get("value", "0"))
                currency = bal.get("currency", "")
                if value > 0.001:
                    lines.append(f"  {currency}: {value:,.6f}")
            return "\n".join(lines) if lines else "  No holdings"
    except Exception:
        pass
    return ""


# ============================================================================
# PHEMEX  (HMAC-SHA256 - try multiple balance endpoints)
# ============================================================================

def _phemex_sign(path, query_string="", body=""):
    if not PHEMEX_API_KEY or not PHEMEX_API_SECRET:
        return {}
    expiry = str(int(time.time()) + 60)
    to_sign = path + query_string + expiry + body
    try:
        signature = hmac.new(
            PHEMEX_API_SECRET.encode("utf-8"),
            to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "x-phemex-access-token": PHEMEX_API_KEY,
            "x-phemex-request-expiry": expiry,
            "x-phemex-request-signature": signature,
            "Content-Type": "application/json",
        }
    except Exception as exc:
        log.warning("Phemex sign error: %s", exc)
        return {}


def get_phemex_balance():
    if not PHEMEX_API_KEY:
        return "Keys not configured"

    # Try multiple endpoints - Phemex has different paths for different account types
    endpoints = [
        ("/g-accounts/accountPositions", "currency=USDT"),
        ("/accounts/accountPositions", "currency=USDT"),
        ("/accounts/positions", "currency=USDT"),
        ("/spot/wallets", "currency=USDT"),
    ]

    last_error = ""
    for path, query in endpoints:
        headers = _phemex_sign(path, query)
        if not headers:
            return "Signing failed"
        url = f"{PHEMEX_BASE}{path}"
        if query:
            url += f"?{query}"
        try:
            r = requests.get(url, headers=headers, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("code") == 0:
                    acct = data.get("data", {})

                    # Format: {"data": {"account": {"totalBalanceRv": "..."}}}
                    if isinstance(acct, dict) and "account" in acct:
                        account = acct["account"]
                        total = account.get("totalBalanceRv",
                                account.get("accountBalanceRv",
                                account.get("accountBalanceEv", 0)))
                        try:
                            total_f = float(total)
                        except (ValueError, TypeError):
                            total_f = 0.0
                        if "accountBalanceEv" in account and "totalBalanceRv" not in account:
                            total_f = total_f / 1e8
                        return f"${total_f:,.2f} USDT"

                    # Format: {"data": [{"balanceEv": ..., "currency": "USDT"}]}
                    if isinstance(acct, list):
                        for wallet in acct:
                            if wallet.get("currency") == "USDT":
                                bal = wallet.get("balanceRv", wallet.get("balanceEv", 0))
                                try:
                                    bal_f = float(bal)
                                except (ValueError, TypeError):
                                    bal_f = 0.0
                                if "balanceEv" in wallet and "balanceRv" not in wallet:
                                    bal_f = bal_f / 1e8
                                return f"${bal_f:,.2f} USDT"

                    return f"Connected (raw: {json.dumps(data)[:100]})"

            if r.status_code == 404:
                last_error = f"404 on {path}"
                continue
            last_error = f"HTTP {r.status_code}: {r.text[:80]}"
        except Exception as exc:
            log.warning("Phemex endpoint %s error: %s", path, exc)
            last_error = str(exc)
            continue

    return f"Endpoints failed: {last_error}"


# ============================================================================
# GITHUB CONVERSATION LOGGER
# ============================================================================

def log_to_github(entry):
    if not GITHUB_TOKEN:
        return False
    api_url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/conversations.md"
    hdrs = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        r = requests.get(api_url, headers=hdrs, timeout=10)
        if r.status_code == 200:
            d = r.json()
            current = base64.b64decode(d["content"]).decode("utf-8")
            sha = d["sha"]
        else:
            current = "# TraderJoes Conversation Log\n\n---\n\n"
            sha = None
        new_content = current + entry
        payload = {
            "message": f"Log {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC",
            "content": base64.b64encode(new_content.encode()).decode(),
        }
        if sha:
            payload["sha"] = sha
        resp = requests.put(api_url, headers=hdrs, json=payload, timeout=15)
        return resp.status_code in (200, 201)
    except Exception as exc:
        log.warning("GitHub log error: %s", exc)
        return False


# ============================================================================
# EV CALCULATION HELPERS
# ============================================================================

def calc_ev(yes_price, implied_prob):
    if yes_price <= 0 or yes_price >= 1:
        return 0.0
    return (implied_prob * (1 - yes_price)) - ((1 - implied_prob) * yes_price)


def find_kalshi_opportunities():
    opportunities = []
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY:
        return opportunities
    try:
        events = get_kalshi_events(limit=10)
        for event in events[:5]:
            ticker = event.get("event_ticker", "")
            title  = event.get("title", ticker)
            markets = get_kalshi_markets_for_event(ticker)
            for mkt in markets:
                yes_price = mkt.get("yes_ask", 0) / 100.0 if mkt.get("yes_ask") else 0
                no_price  = mkt.get("no_ask", 0)  / 100.0 if mkt.get("no_ask") else 0
                yes_bid   = mkt.get("yes_bid", 0)  / 100.0 if mkt.get("yes_bid") else 0

                if yes_price <= 0 or no_price <= 0:
                    continue

                total = yes_price + no_price
                if total < 0.98:
                    spread_ev = 1.0 - total
                    opportunities.append({
                        "platform": "Kalshi",
                        "market": mkt.get("title", title)[:60],
                        "ticker": mkt.get("ticker", ""),
                        "type": "Arb (Yes+No < $1)",
                        "ev": spread_ev,
                        "detail": f"Yes ${yes_price:.2f} + No ${no_price:.2f} = ${total:.2f}",
                    })

                if yes_bid > 0 and yes_price > 0:
                    spread = yes_price - yes_bid
                    if spread >= 0.05:
                        opportunities.append({
                            "platform": "Kalshi",
                            "market": mkt.get("title", title)[:60],
                            "ticker": mkt.get("ticker", ""),
                            "type": "Wide Spread",
                            "ev": spread,
                            "detail": f"Bid ${yes_bid:.2f} / Ask ${yes_price:.2f} (spread ${spread:.2f})",
                        })
            time.sleep(0.3)
    except Exception as exc:
        log.warning("Kalshi scan error: %s", exc)
    return opportunities


def find_polymarket_opportunities():
    opportunities = []
    try:
        markets = get_polymarket_markets(limit=30)
        for mkt in markets:
            question = mkt.get("question", mkt.get("title", "Unknown"))[:60]
            condition_id = mkt.get("condition_id", "")

            # Gamma API returns outcomePrices as a JSON string like "[\"0.95\",\"0.05\"]"
            outcome_prices_raw = mkt.get("outcomePrices", "")
            tokens = mkt.get("tokens", [])

            yes_price = 0.0
            no_price = 0.0

            # Try outcomePrices first (Gamma API format)
            if outcome_prices_raw:
                try:
                    if isinstance(outcome_prices_raw, str):
                        prices = json.loads(outcome_prices_raw)
                    else:
                        prices = outcome_prices_raw
                    if len(prices) >= 2:
                        yes_price = float(prices[0])
                        no_price = float(prices[1])
                except (json.JSONDecodeError, ValueError, IndexError):
                    pass

            # Fall back to tokens array (CLOB API format)
            if yes_price <= 0 and len(tokens) >= 2:
                yes_price = float(tokens[0].get("price", 0))
                no_price = float(tokens[1].get("price", 0))

            if yes_price <= 0 or no_price <= 0:
                continue

            # Extract volume and liquidity info
            vol_24h = 0.0
            total_vol = 0.0
            liquidity = 0.0
            try:
                vol_24h = float(mkt.get("volume24hr", 0) or 0)
                total_vol = float(mkt.get("volume", 0) or 0)
                liquidity = float(mkt.get("liquidityClob", 0) or 0)
            except (ValueError, TypeError):
                pass

            def _fmt_vol(v):
                if v >= 1_000_000:
                    return f"${v/1_000_000:.1f}M"
                if v >= 1_000:
                    return f"${v/1_000:.0f}K"
                return f"${v:,.0f}"

            vol_str = f"Vol 24h: {_fmt_vol(vol_24h)}" if vol_24h > 0 else ""
            liq_str = f"Liq: {_fmt_vol(liquidity)}" if liquidity > 0 else ""
            extra = " | ".join(filter(None, [vol_str, liq_str]))

            total = yes_price + no_price
            if total < 0.98:
                detail = f"Yes ${yes_price:.3f} + No ${no_price:.3f} = ${total:.3f}"
                if extra:
                    detail += f" | {extra}"
                opportunities.append({
                    "platform": "Polymarket",
                    "market": question,
                    "ticker": condition_id[:20],
                    "type": "Arb (Yes+No < $1)",
                    "ev": 1.0 - total,
                    "detail": detail,
                })

            if 0.02 < yes_price < 0.10:
                detail = f"YES @ ${yes_price:.3f} / NO @ ${no_price:.3f}"
                if extra:
                    detail += f" | {extra}"
                opportunities.append({
                    "platform": "Polymarket",
                    "market": question,
                    "ticker": condition_id[:20],
                    "type": "Low-Price YES",
                    "ev": yes_price,
                    "detail": detail,
                })
    except Exception as exc:
        log.warning("Polymarket scan error: %s", exc)
    return opportunities


# ============================================================================
# DISCORD BOT COMMANDS
# ============================================================================

@bot.event
async def on_ready():
    log.info("TraderJoes bot online as %s", bot.user)


@bot.command()
async def ping(ctx):
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! Latency: {latency}ms - TraderJoes is live.")


@bot.command()
async def portfolio(ctx):
    msg = await ctx.send("Fetching balances from all platforms...")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    kalshi   = get_kalshi_balance()
    poly     = get_polymarket_balance()
    robinhood = get_robinhood_balance()
    coinbase  = get_coinbase_balance()
    phemex    = get_phemex_balance()

    rh_holdings = get_robinhood_holdings()
    cb_holdings = get_coinbase_holdings_detail()
    poly_holdings = get_polymarket_positions_detail()

    report = (
        f"**TraderJoes Portfolio** | {ts}\n"
        f"================================\n"
        f"**Kalshi:** {kalshi}\n"
        f"**Polymarket:** {poly}\n"
    )
    if poly_holdings:
        report += f"{poly_holdings}\n"

    report += f"**Robinhood Crypto:** {robinhood}\n"
    if rh_holdings:
        report += f"{rh_holdings}\n"

    report += f"**Coinbase:** {coinbase}\n"
    if cb_holdings:
        report += f"{cb_holdings}\n"

    report += (
        f"**Phemex:** {phemex}\n"
        f"================================\n"
        f"*PredictIt & Interactive Brokers: pending integration*"
    )

    await msg.edit(content=report)

    log_entry = (
        f"\n## Portfolio Snapshot -- {ts}\n"
        f"- Kalshi: {kalshi}\n"
        f"- Polymarket: {poly}\n"
        f"- Robinhood: {robinhood}\n"
        f"- Coinbase: {coinbase}\n"
        f"- Phemex: {phemex}\n\n---\n"
    )
    log_to_github(log_entry)


@bot.command()
async def cycle(ctx):
    msg = await ctx.send("Running EV scan across prediction markets...")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    kalshi_opps = find_kalshi_opportunities()
    poly_opps   = find_polymarket_opportunities()

    all_opps = kalshi_opps + poly_opps
    all_opps.sort(key=lambda x: x.get("ev", 0), reverse=True)

    if not all_opps:
        await msg.edit(
            content=(
                f"**EV Scan Complete** | {ts}\n"
                f"No strong opportunities found this cycle.\n"
                f"Scanned: Kalshi ({len(kalshi_opps)} found), "
                f"Polymarket ({len(poly_opps)} found)"
            )
        )
        return

    report = f"**EV Scan Results** | {ts}\n================================\n"
    for i, opp in enumerate(all_opps[:10], 1):
        ev_pct = opp["ev"] * 100
        report += (
            f"**{i}. [{opp['platform']}] {opp['type']}** -- EV: +{ev_pct:.1f}%\n"
            f"   {opp['market']}\n"
            f"   {opp['detail']}\n\n"
        )
    report += (
        f"================================\n"
        f"Total: {len(all_opps)} opportunities | "
        f"Kalshi: {len(kalshi_opps)} | Polymarket: {len(poly_opps)}"
    )

    if len(report) > 1900:
        report = report[:1900] + "\n*...truncated*"

    await msg.edit(content=report)

    log_entry = (
        f"\n## EV Scan -- {ts}\n"
        f"Found {len(all_opps)} opportunities\n\n"
    )
    for opp in all_opps[:10]:
        log_entry += (
            f"- **[{opp['platform']}] {opp['type']}** EV: +{opp['ev']*100:.1f}%\n"
            f"  {opp['market']} -- {opp['detail']}\n"
        )
    log_entry += "\n---\n"
    log_to_github(log_entry)


@bot.command(name="log")
async def manual_log(ctx, *, message: str):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    entry = (
        f"\n## Manual Log -- {ts}\n"
        f"**Author:** {ctx.author}\n\n"
        f"{message}\n\n---\n"
    )
    success = log_to_github(entry)
    if success:
        await ctx.send("Logged to conversations.md")
    else:
        await ctx.send("Failed to log -- check GITHUB_TOKEN")


@bot.command()
async def status(ctx):
    checks = {
        "Kalshi":           bool(KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY),
        "Polymarket":       bool(POLY_WALLET_ADDRESS),
        "Robinhood":        bool(ROBINHOOD_API_KEY and ROBINHOOD_PRIVATE_KEY),
        "Coinbase":         bool(COINBASE_API_KEY and COINBASE_API_SECRET),
        "Phemex":           bool(PHEMEX_API_KEY and PHEMEX_API_SECRET),
        "GitHub Logger":    bool(GITHUB_TOKEN),
        "Discord Channel":  bool(DISCORD_CHANNEL_ID),
    }
    lines = ["**TraderJoes Integration Status**\n"]
    for name, ok in checks.items():
        icon = "OK" if ok else "MISSING"
        lines.append(f"[{icon}] {name}")
    await ctx.send("\n".join(lines))


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set -- cannot start bot")
        raise SystemExit(1)
    bot.run(DISCORD_TOKEN)
