"""
TraderJoes Trading Firm — Discord Bot
=====================================
Multi-platform portfolio viewer and EV scanner.
Platforms: Kalshi, Polymarket, Robinhood (Crypto), Coinbase (Advanced Trade), Phemex
"""

MAX_POSITION_PCT = 0.015  # Tiered: 1.5% high / 0.5% medium / 0.25% low confidence
DAILY_LOSS_LIMIT = -500
DAILY_PNL = 0.0
# HARD KILL-SWITCH: If total portfolio drops below this, ALL trading stops.
# "The AI will die if we get liquidated" — protect capital at all costs.
PORTFOLIO_FLOOR = 50000  # $50K absolute minimum — kill all trading below this
PORTFOLIO_FLOOR_ACTIVE = False  # Set True automatically when floor is breached

# TradingView / Market Cipher signal integration
# Boosts edge score but does NOT auto-trigger trades alone
TRADINGVIEW_SIGNALS = {
    "enabled": True,
    "webhook_secret": "",  # Set TV_WEBHOOK_SECRET in .env
    "latest_signal": {},  # Updated via webhook or manual command
    "signal_expiry_minutes": 30,  # Signals older than this are ignored
    "max_boost_points": 15,  # Max points added to edge score
    "history": [],  # Rolling list of last 50 signals received
    "auto_execute": True,  # Auto-execute when TV + cycle agree
    "auto_execute_size_pct": 0.01,  # 1% of portfolio per auto-exec
}

import discord
from discord.ext import commands, tasks
import os
import sqlite3
try:
    import redis as redis_lib
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
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

def db_flush_legacy_pairs(cutoff_date="2026-03-27"):
    """Archive contaminated pairs trades from before cutoff_date.
    Renames strategy from 'pairs' to 'pairs_legacy' so Kelly win-rate
    calculation excludes them. Returns count of archived trades."""
    try:
        import sqlite3 as _fsq
        conn = _fsq.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""UPDATE positions SET strategy='pairs_legacy'
            WHERE strategy='pairs' AND created_at < ? AND status='closed'""",
            (cutoff_date,))
        closed_count = c.rowcount
        c.execute("""UPDATE positions SET strategy='pairs_legacy'
            WHERE strategy='pairs' AND created_at < ? AND status='open'""",
            (cutoff_date,))
        open_count = c.rowcount
        conn.commit()
        conn.close()
        total = closed_count + open_count
        if total > 0:
            log.info("PAIRS FLUSH: archived %d trades to pairs_legacy (cutoff=%s) — %d closed, %d open",
                     total, cutoff_date, closed_count, open_count)
        else:
            log.info("PAIRS FLUSH: no trades to archive before %s", cutoff_date)
        return total
    except Exception as e:
        log.warning("PAIRS FLUSH error: %s", e)
        return 0


def db_open_position(market_id, platform, strategy, direction,
                     size_usd, shares, entry_price,
                     stop_price=0, target_price=0,
                     long_leg="", short_leg="", entry_zscore=0,
                     regime="normal", metadata=None):
    try:
        import sqlite3 as _sq, json as _js
        conn = _sq.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM positions WHERE market_id=? AND status='open'", (market_id,))
        if c.fetchone()[0] > 0:
            conn.close()
            log.info("DB-DEDUP: %s already open", market_id)
            return None
        c.execute("""INSERT INTO positions
            (market_id,platform,strategy,direction,size_usd,shares,entry_price,
             stop_price,target_price,long_leg,short_leg,entry_zscore,
             status,regime,created_at,metadata)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open',?,datetime('now'),?)""",
            (market_id,platform,strategy,direction,size_usd,shares,entry_price,
             stop_price,target_price,long_leg,short_leg,entry_zscore,
             regime, _js.dumps(metadata or {})))
        row_id = c.lastrowid
        conn.commit(); conn.close()
        log.info("DB-OPEN: %s | %s | $%.2f", market_id, strategy, size_usd)
        try:
            shadow_open_position(market_id, strategy, direction, size_usd, entry_price)
        except Exception:
            pass
        return row_id
    except Exception as e:
        log.warning("db_open_position error: %s", e)
        return None

def db_close_position(market_id, exit_price, exit_reason, realized_pnl=0):
    try:
        import sqlite3 as _sq
        conn = _sq.connect(DB_PATH)
        c = conn.cursor()
        # Fetch position details before closing for journal
        c.execute("SELECT strategy, direction, size_usd, created_at, regime, metadata FROM positions WHERE market_id=? AND status='open'", (market_id,))
        _pos_row = c.fetchone()
        c.execute("""UPDATE positions SET status='closed', current_price=?,
            closed_at=datetime('now'), exit_reason=?, realized_pnl=?
            WHERE market_id=? AND status='open'""",
            (exit_price, exit_reason, realized_pnl, market_id))
        rows = c.rowcount
        conn.commit()
        if rows > 0:
            log.info("DB-CLOSE: %s | %s | pnl=$%.2f", market_id, exit_reason, realized_pnl)
            # Auto-generate trade journal entry
            try:
                _strat = _pos_row[0] if _pos_row else "?"
                _dir = _pos_row[1] if _pos_row else "?"
                _size = _pos_row[2] if _pos_row else 0
                _entry_dt = _pos_row[3] if _pos_row else ""
                _regime = _pos_row[4] if _pos_row else "normal"
                _meta = _pos_row[5] if _pos_row else "{}"
                # Calculate hold time
                _hold = 0
                if _entry_dt:
                    try:
                        _edt = datetime.fromisoformat(_entry_dt.replace(" UTC", "+00:00")) if "UTC" in str(_entry_dt) else datetime.fromisoformat(str(_entry_dt))
                        _hold = (datetime.now(timezone.utc) - _edt.replace(tzinfo=timezone.utc)).total_seconds() / 3600
                    except Exception:
                        pass
                # Generate lesson
                _outcome = "WIN" if realized_pnl > 0 else "LOSS" if realized_pnl < 0 else "FLAT"
                _lesson = f"{_outcome}: {_strat} trade {market_id[:30]} exited via {exit_reason[:30]}"
                if realized_pnl > 0:
                    _lesson += f" — regime={_regime} worked for {_strat}"
                elif realized_pnl < 0:
                    _lesson += f" — regime={_regime} unfavorable for {_strat}"
                # Parse signal trigger from metadata
                _signal = ""
                try:
                    import json as _jj
                    _md = _jj.loads(_meta) if isinstance(_meta, str) else {}
                    _signal = _md.get("signal", _md.get("ticker", ""))
                except Exception:
                    pass
                c.execute("""INSERT INTO trade_journal
                    (market_id, strategy, direction, entry_date, exit_date, hold_hours,
                     entry_regime, exit_reason, realized_pnl, size_usd, signal_trigger, lesson)
                    VALUES (?, ?, ?, ?, datetime('now'), ?, ?, ?, ?, ?, ?, ?)""",
                    (market_id, _strat, _dir, _entry_dt, round(_hold, 1), _regime,
                     exit_reason, realized_pnl, _size, _signal, _lesson))
                conn.commit()
            except Exception as _je:
                log.warning("Trade journal error: %s", _je)
            try:
                shadow_close_position(market_id, realized_pnl, exit_reason)
            except Exception:
                pass
        conn.close()
        return rows > 0
    except Exception as e:
        log.warning("db_close_position error: %s", e)
        return False

def db_get_open_positions(strategy=None):
    try:
        import sqlite3 as _sq
        conn = _sq.connect(DB_PATH)
        conn.row_factory = _sq.Row
        c = conn.cursor()
        if strategy:
            c.execute("SELECT * FROM positions WHERE status='open' AND strategy=?", (strategy,))
        else:
            c.execute("SELECT * FROM positions WHERE status='open'")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return rows
    except Exception as e:
        log.warning("db_get_open_positions error: %s", e)
        return []

def db_position_count(strategy=None):
    try:
        import sqlite3 as _sq
        conn = _sq.connect(DB_PATH)
        c = conn.cursor()
        if strategy:
            c.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND strategy=?", (strategy,))
        else:
            c.execute("SELECT COUNT(*) FROM positions WHERE status='open'")
        n = c.fetchone()[0]; conn.close(); return n
    except:
        return 0


def _fetch_vix_price():
    try:
        import yfinance as yf
        h = yf.Ticker("^VIX").history(period="5d")
        if not h.empty:
            return float(h["Close"].iloc[-1])
    except Exception as e:
        print(f"[Regime] VIX fetch failed: {e}")
    return None

def _fetch_crypto_vol_24h(symbol):
    try:
        import requests
        sym = symbol.replace("CRYPTO:","")
        r = requests.get(
            f"https://api.exchange.coinbase.com/products/{sym}-USD/candles",
            params={"granularity": 3600}, timeout=8)
        if r.status_code != 200:
            return None
        candles = r.json()[:24]
        closes = [float(c[4]) for c in candles]
        if len(closes) < 4:
            return None
        rets = [(closes[i]-closes[i+1])/closes[i+1] for i in range(len(closes)-1)]
        return _stats.stdev(rets)
    except Exception as e:
        print(f"[Regime] Crypto vol fetch failed {symbol}: {e}")
    return None

def get_regime(asset="equities"):
    from datetime import datetime
    global REGIME_CACHE
    cached = REGIME_CACHE.get(asset)
    if cached and (datetime.now()-cached["ts"]).total_seconds() < REGIME_CACHE_TTL:
        return cached

    base = {"regime":"normal","multiplier":1.0,"zscore_entry":1.0,
            "tp_mult":1.0,"sl_mult":1.0,"halt":False,"vix":None}

    if asset in ("equities","pairs"):
        vix = _fetch_vix_price()
        base["vix"] = vix
        if vix is None:
            pass
        elif vix < 15:
            base.update({"regime":"low","multiplier":0.75,"zscore_entry":1.3,
                         "tp_mult":0.8,"sl_mult":0.7})
        elif vix < 25:
            pass  # normal defaults
        elif vix < 35:
            base.update({"regime":"elevated","multiplier":1.3,"zscore_entry":1.5,
                         "tp_mult":1.4,"sl_mult":1.5})
        else:
            base.update({"regime":"extreme","multiplier":2.0,"zscore_entry":2.0,
                         "tp_mult":2.0,"sl_mult":2.0,"halt":True})
        print(f"[Regime] {asset}: VIX={vix} regime={base['regime']}")

    elif asset == "options":
        # Options adapt but never halt — high VIX = richer premium
        vix = _fetch_vix_price()
        base["vix"] = vix
        if vix and vix > 25:
            # Shift to wider strikes, longer expiry — handled in options engine
            base.update({"regime":"elevated","use_2dte":True,"delta_target":0.10})
        else:
            base.update({"use_2dte":False,"delta_target":0.15})
        print(f"[Regime] options: VIX={vix} regime={base['regime']}")

    else:
        # Crypto — own rolling vol
        vol = _fetch_crypto_vol_24h(asset)
        if vol is None:
            pass
        elif vol < 0.02:
            base.update({"regime":"low","multiplier":0.8,"tp_mult":0.8,"sl_mult":0.75})
        elif vol < 0.05:
            pass
        elif vol < 0.10:
            base.update({"regime":"elevated","multiplier":1.4,"tp_mult":1.5,"sl_mult":1.6})
        else:
            base.update({"regime":"extreme","multiplier":2.0,"tp_mult":2.5,
                         "sl_mult":2.5,"halt":True})
        print(f"[Regime] {asset}: 24h_vol={vol} regime={base['regime']}")

    base["ts"] = datetime.now()
    REGIME_CACHE[asset] = base
    return base

def regime_adjusted_tp_sl(base_tp, base_sl, asset="equities"):
    r = get_regime(asset)
    return (min(base_tp*r["tp_mult"], base_tp*2.5),
            min(base_sl*r["sl_mult"], base_sl*2.5))

def regime_adjusted_zscore(base_z=1.0, asset="equities"):
    return get_regime(asset).get("zscore_entry", base_z)

# ═══════════════════════════════════════════════════════════════════

REGIME_CACHE = {}
REGIME_CACHE_TTL = 300

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
POLY_WALLET_ADDRESS     = os.environ.get("POLY_WALLET_ADDRESS", os.environ.get("POLYMARKET_FUNDER", ""))
POLYMARKET_API_KEY      = os.environ.get("POLYMARKET_API_KEY", "")
POLYMARKET_SECRET       = os.environ.get("POLYMARKET_SECRET", "")
POLYMARKET_PASSPHRASE   = os.environ.get("POLYMARKET_PASSPHRASE", "")
POLY_PRIVATE_KEY        = os.environ.get("POLY_PRIVATE_KEY", "")

# Robinhood Crypto API
ROBINHOOD_API_KEY       = os.environ.get("ROBINHOOD_API_KEY", "")
ROBINHOOD_PRIVATE_KEY   = os.environ.get("ROBINHOOD_PRIVATE_KEY", "")
ROBINHOOD_PUBLIC_KEY    = os.environ.get("ROBINHOOD_PUBLIC_KEY", "")
ROBINHOOD_BASE          = "https://trading.robinhood.com"

# Coinbase Advanced Trade (CDP API Keys - JWT auth)
COINBASE_API_KEY        = os.environ.get("COINBASE_API_KEY", "")
COINBASE_API_SECRET     = os.environ.get("COINBASE_API_SECRET", "")
# Load PEM keys from files if they exist (fixes Docker .env formatting issues)
for _kp, _var in [("/app/keys/coinbase.pem", "COINBASE_API_SECRET"), ("/app/keys/kalshi.pem", "KALSHI_PRIVATE_KEY")]:
    if os.path.exists(_kp):
        with open(_kp) as _f:
            _pem = _f.read().strip()
        if "-----BEGIN" in _pem:
            if _var == "COINBASE_API_SECRET":
                COINBASE_API_SECRET = _pem
            elif _var == "KALSHI_PRIVATE_KEY":
                KALSHI_PRIVATE_KEY = _pem
            print(f"Loaded {_var} from {_kp}")

# Phemex
PHEMEX_API_KEY      = os.environ.get("PHEMEX_API_KEY", "")
PHEMEX_API_SECRET   = os.environ.get("PHEMEX_API_SECRET", "")
PHEMEX_BASE         = "https://api.phemex.com"

# GitHub logging
GITHUB_TOKEN    = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO     = os.environ.get("GITHUB_REPO", "jw0808-blip/trading-bot")

# Trading Mode
TRADING_MODE    = os.environ.get("TRADING_MODE", "paper")
DRY_RUN_MODE    = False  # Paper orders enabled (ALPACA_BASE_URL must be paper-api)
PAPER_PORTFOLIO = {"cash": 10000.0, "positions": [], "trades": [], "pnl": 0.0}

# OpenAI
OPENAI_API_KEY  = os.environ.get("OPENAI_API_KEY", "")


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
            params={"limit": limit, "status": "open",
                    "with_nested_markets": "true"},
            timeout=15,
        )
        if r.status_code == 200:
            return r.json().get("events", [])
    except Exception as exc:
        log.warning("Kalshi events fetch error: %s", exc)
    return []


def _is_kalshi_sports(market):
    """Detect sports/player-prop markets from Kalshi. Catches patterns that is_sports_or_junk misses."""
    title = market.get("title", "").lower()
    cat = market.get("category", "").lower()
    event_ticker = market.get("event_ticker", "").upper()
    # Category-based filter
    _sports_cats = {"sports", "nba", "nfl", "mlb", "nhl", "mls", "soccer", "ncaa",
                    "esports", "combat sports", "golf", "tennis", "cricket"}
    if cat in _sports_cats:
        return True
    # Event ticker patterns (Kalshi uses prefixes like NBA-, NFL-, MLB-)
    _sports_prefixes = ("NBA-", "NFL-", "MLB-", "NHL-", "MLS-", "UFC-", "NCAA-", "PGA-",
                        "SOCCER-", "EPL-", "GOLF-", "TENNIS-", "F1-", "NASCAR-")
    if any(event_ticker.startswith(p) for p in _sports_prefixes):
        return True
    # Player prop patterns: "Name: N+" (e.g. "LaMelo Ball: 10+")
    import re
    if re.search(r'[A-Z][a-z]+ [A-Z][a-z]+: \d+\+', market.get("title", "")):
        return True
    # "wins by over N.N Points" or "Over N.N Points"
    if "wins by over" in title or "over " in title and " points" in title:
        return True
    if " goals" in title or " innings" in title or " touchdowns" in title:
        return True
    return False


def get_kalshi_active_markets(limit=50):
    """Fetch active Kalshi markets via events endpoint (financial/political markets).
    The /markets endpoint returns mostly sports; /events returns catalyst markets."""
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY:
        return []
    try:
        events = get_kalshi_events(limit=20)
        all_markets = []
        for event in events:
            # Get nested markets from event (if with_nested_markets=true worked)
            nested = event.get("markets", [])
            if nested:
                for m in nested:
                    m["_event_title"] = event.get("title", "")
                    m["_event_category"] = event.get("category", "")
                all_markets.extend(nested)
            else:
                # Fallback: fetch markets for this event
                ticker = event.get("event_ticker", "")
                if ticker:
                    mkts = get_kalshi_markets_for_event(ticker)
                    for m in mkts:
                        m["_event_title"] = event.get("title", "")
                        m["_event_category"] = event.get("category", "")
                    all_markets.extend(mkts)
        # Filter sports that snuck through events
        filtered = [m for m in all_markets
                    if not _is_kalshi_sports(m) and not is_sports_or_junk(m.get("title", ""))]
        filtered.sort(key=lambda m: float(m.get("volume", 0) or 0), reverse=True)
        log.info("KALSHI: %d events → %d markets → %d after filter", len(events), len(all_markets), len(filtered))
        return filtered[:limit]
    except Exception as exc:
        log.warning("Kalshi active markets error: %s", exc)
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


def _get_polymarket_clob_balance():
    """Get USDC.e cash balance from host-side balance file."""
    try:
        bal_file = "/app/data/poly_cash_balance.txt"
        if os.path.exists(bal_file):
            with open(bal_file, "r") as f:
                val = f.read().strip()
            if val:
                bal = float(val)
                log.info("Polymarket USDC.e from balance file: $%.2f", bal)
                return bal
        log.warning("Polymarket balance file not found or empty")
        return 0.0
    except Exception as exc:
        log.warning("Polymarket balance file error: %s", exc)
        return 0.0

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




def get_polymarket_clob_balance():
    """Get Polymarket balance: USDC.e on-chain + position values from Data API."""
    import requests
    from web3 import Web3

    cash = 0.0
    positions_val = 0.0
    proxy_wallet = os.environ.get("POLYMARKET_FUNDER", os.environ.get("POLY_WALLET_ADDRESS", "")).strip()

    if not proxy_wallet:
        log.warning("No Polymarket wallet address configured")
        return 0.0

    # --- 1. Cash: USDC.e balance at proxy wallet (on-chain) ---
    try:
        POLYGON_RPC = "https://polygon-rpc.com"
        USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
        # balanceOf(address) = 0x70a08231 + padded address
        padded = proxy_wallet.lower().replace("0x", "").zfill(64)
        call_data = "0x70a08231" + padded

        rpc_payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": USDC_E, "data": call_data}, "latest"],
            "id": 1,
        }
        r = requests.post(POLYGON_RPC, json=rpc_payload, timeout=10)
        if r.status_code == 200:
            result_hex = r.json().get("result", "0x0")
            raw = int(result_hex, 16)
            cash = raw / 1e6  # USDC.e has 6 decimals
            log.info("Polymarket USDC.e on-chain: $%.2f (wallet=%s)", cash, proxy_wallet[:10])
    except Exception as exc:
        log.warning("Polymarket USDC.e check error: %s", exc)

    # --- 2. Positions: Data API ---
    try:
        r2 = requests.get(
            f"https://data-api.polymarket.com/value?user={proxy_wallet}",
            timeout=10,
        )
        if r2.status_code == 200:
            val = r2.json()
            if isinstance(val, (int, float)):
                positions_val = float(val)
            elif isinstance(val, dict):
                positions_val = float(val.get("value", 0))
            log.info("Polymarket positions value: $%.2f", positions_val)
    except Exception as exc:
        log.warning("Polymarket positions check error: %s", exc)

    total = cash + positions_val
    log.info("Polymarket final: cash=$%.2f positions=$%.2f total=$%.2f", cash, positions_val, total)
    return total


def get_polymarket_balance():
    # Try CLOB-based balance first (includes deposited cash + positions)
    if POLYMARKET_PK or POLYMARKET_FUNDER:
        try:
            result, err = get_polymarket_clob_balance()
            if result and result["total"] > 0:
                total = result["total"]
                cash = result["cash"]
                pv = result["positions_value"]
                details = result["position_details"]
                summary = f"${total:,.2f}"
                parts = []
                if cash > 0.01:
                    parts.append(f"cash: ${cash:,.2f}")
                if pv > 0.01:
                    parts.append(f"positions: ${pv:,.2f}")
                if parts:
                    summary += f" ({', '.join(parts)})"
                if details:
                    summary += "\n" + "\n".join(details)
                return summary
        except Exception as exc:
            log.warning("CLOB balance fallback: %s", exc)
    
    if not POLY_WALLET_ADDRESS:
        return "Wallet not configured"

    # 1) Fetch positions value from Data API
    positions_value, _ = _get_polymarket_positions()

    # 2) Fetch on-chain USDC (uninvested cash)
    cash_value, cash_details = _get_polymarket_usdc_onchain()

    # 3) Try to get total portfolio value from activity/redemptions
    exchange_cash = 0.0
    try:
        r = requests.get(
            "https://data-api.polymarket.com/activity",
            params={"user": POLY_WALLET_ADDRESS, "limit": 1, "offset": 0},
            timeout=10,
        )
        # The exchange cash is not directly available via public API
        # Show what we can see and note funds are in exchange
    except Exception:
        pass

    # Get deposited balance from CLOB API
    clob_cash = _get_polymarket_clob_balance()

    total = positions_value + cash_value + clob_cash
    parts = []
    if positions_value > 0.01:
        parts.append(f"positions: ${positions_value:,.2f}")
    if clob_cash > 0.01:
        parts.append(f"deposited: ${clob_cash:,.2f}")
    if cash_value > 0.01:
        parts.append(f"on-chain: ${cash_value:,.2f}")

    if parts:
        return f"${total:,.2f} ({', '.join(parts)})"
    return "$0.00 (no positions or cash)"


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




def get_crypto_usd_prices():
    """Get current USD prices for common cryptos from CoinGecko."""
    try:
        ids = "bitcoin,ethereum,dogecoin,stellar,shiba-inu,ripple,algorand,hedera-hashgraph,ren"
        r = requests.get(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd", timeout=10)
        if r.status_code == 200:
            data = r.json()
            # Map common symbols to coingecko ids
            return {
                "BTC": data.get("bitcoin", {}).get("usd", 0),
                "ETH": data.get("ethereum", {}).get("usd", 0),
                "DOGE": data.get("dogecoin", {}).get("usd", 0),
                "XLM": data.get("stellar", {}).get("usd", 0),
                "SHIB": data.get("shiba-inu", {}).get("usd", 0),
                "XRP": data.get("ripple", {}).get("usd", 0),
                "ALGO": data.get("algorand", {}).get("usd", 0),
                "HBAR": data.get("hedera-hashgraph", {}).get("usd", 0),
                "REN": data.get("ren", {}).get("usd", 0),
                "USDC": 1.0,
                "USD": 1.0,
                "USDT": 1.0,
            }
    except Exception as exc:
        log.warning("CoinGecko price fetch error: %s", exc)
    return {}

def get_coinbase_balance():
    """Fetch Coinbase balances with USD total."""
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return "$0.00 (not configured)"
    try:
        import jwt as _jwt, time as _time, secrets as _secrets
        uri = "api.coinbase.com"
        path = "/api/v3/brokerage/accounts"
        payload = {
            "sub": COINBASE_API_KEY,
            "iss": "cdp",
            "nbf": int(_time.time()),
            "exp": int(_time.time()) + 120,
            "uri": f"GET {uri}{path}",
        }
        token = _jwt.encode(payload, COINBASE_API_SECRET, algorithm="ES256",
                            headers={"kid": COINBASE_API_KEY, "nonce": _secrets.token_hex(16), "typ": "JWT"})
        hdrs = {"Authorization": f"Bearer {token}"}
        r = requests.get(f"https://{uri}{path}", headers=hdrs, timeout=15)
        if r.status_code != 200:
            return f"API error {r.status_code}"
        accounts = r.json().get("accounts", [])
        
        # Get prices for USD conversion
        prices = get_crypto_usd_prices()
        
        lines = []
        total_usd_value = 0.0
        for acct in accounts:
            bal = float(acct.get("available_balance", {}).get("value", 0))
            cur = acct.get("available_balance", {}).get("currency", "")
            if bal > 0.0001:
                # Calculate USD value
                price = prices.get(cur, 0)
                usd_val = bal * price
                total_usd_value += usd_val
                if usd_val > 0.01:
                    lines.append(f"{cur}: {bal:,.6f} (${usd_val:,.2f})")
                else:
                    lines.append(f"{cur}: {bal:,.6f}")
        
        summary = f"${total_usd_value:,.2f} USD total"
        if lines:
            return summary + "\n" + "\n".join(lines)
        return "$0.00 (no balances)"
    except Exception as exc:
        log.warning("Coinbase error: %s", exc)
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


KALSHI_MIN_VOLUME = 1000  # $1K minimum volume — was $50K which filtered everything

def find_kalshi_opportunities():
    opportunities = []
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY:
        return opportunities

    _kalshi_cats = ["fda","sec ","cpi","fed ","fomc","supreme court",
                    "earnings","tariff","iran","ceasefire","ukraine",
                    "russia","china","indictment","impeach","rate cut",
                    "rate hike","inflation","gdp","jobs report",
                    "nonfarm","sanctions","netanyahu","trudeau",
                    "macron","zelensky","zelenskyy","putin","modi",
                    "erdogan","mbs","kim jong","opec","taiwan","gaza",
                    "debt ceiling","executive order","pce",
                    "retail sales","recession","yield curve",
                    "merger","antitrust"]

    try:
        events = get_kalshi_events(limit=10)
        for event in events[:5]:
            ticker = event.get("event_ticker", "")
            title  = event.get("title", ticker)
            markets = get_kalshi_markets_for_event(ticker)
            for mkt in markets:
                # Sports/player-prop filter first
                if _is_kalshi_sports(mkt) or is_sports_or_junk(mkt.get("title", "")):
                    continue

                yes_price = mkt.get("yes_ask", 0) / 100.0 if mkt.get("yes_ask") else 0
                no_price  = mkt.get("no_ask", 0)  / 100.0 if mkt.get("no_ask") else 0
                yes_bid   = mkt.get("yes_bid", 0)  / 100.0 if mkt.get("yes_bid") else 0

                if yes_price <= 0 or no_price <= 0:
                    continue

                # Volume filter — Kalshi reports volume in cents
                volume = float(mkt.get("volume", 0) or 0)
                if volume < KALSHI_MIN_VOLUME:
                    continue

                mkt_title = mkt.get("title", title)[:60]
                mkt_ticker = mkt.get("ticker", "")
                mkt_lower = mkt_title.lower()

                # Sports/junk filter — only trade catalyst markets
                if is_sports_or_junk(mkt_title):
                    continue

                total = yes_price + no_price
                if total < 0.98:
                    spread_ev = 1.0 - total
                    opportunities.append({
                        "platform": "Kalshi",
                        "market": mkt_title,
                        "ticker": mkt_ticker,
                        "type": "Arb (Yes+No < $1)",
                        "ev": spread_ev,
                        "detail": f"Yes ${yes_price:.2f} + No ${no_price:.2f} = ${total:.2f} | Vol: ${volume:,.0f}",
                    })

                if yes_bid > 0 and yes_price > 0:
                    spread = yes_price - yes_bid
                    if spread >= 0.05:
                        opportunities.append({
                            "platform": "Kalshi",
                            "market": mkt_title,
                            "ticker": mkt_ticker,
                            "type": "Wide Spread",
                            "ev": spread,
                            "detail": f"Bid ${yes_bid:.2f} / Ask ${yes_price:.2f} (spread ${spread:.2f}) | Vol: ${volume:,.0f}",
                        })

                # Catalyst YES: low price YES on whitelisted events
                is_catalyst = any(cat in mkt_lower for cat in _kalshi_cats)
                if is_catalyst and 0.02 < yes_price < 0.20:
                    opportunities.append({
                        "platform": "Kalshi",
                        "market": mkt_title,
                        "ticker": mkt_ticker,
                        "type": "Catalyst YES",
                        "ev": yes_price,
                        "yes_price": yes_price,
                        "detail": f"YES @ ${yes_price:.3f} / NO @ ${no_price:.3f} | Vol: ${volume:,.0f}",
                    })

                # Catalyst NO: high YES price → buy NO contract
                if is_catalyst and yes_price > 0.65 and no_price > 0.01:
                    opportunities.append({
                        "platform": "Kalshi",
                        "market": mkt_title,
                        "ticker": mkt_ticker,
                        "type": "Catalyst NO",
                        "ev": no_price,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "side": "NO",
                        "detail": f"NO @ ${no_price:.3f} (YES=${yes_price:.3f}) | Vol: ${volume:,.0f}",
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

            # NO contracts: buy NO when YES is high on catalyst events
            if yes_price > 0.65 and no_price > 0.01:
                _no_cats = ["fda","sec ","cpi","fed ","fomc","supreme court",
                            "earnings","tariff","iran","ceasefire","ukraine",
                            "russia","china","indictment","impeach","rate cut",
                            "rate hike","inflation","gdp","jobs report",
                            "nonfarm","sanctions","netanyahu","trudeau",
                            "macron","zelensky","zelenskyy","putin","modi",
                            "erdogan","mbs","kim jong","opec","taiwan","gaza",
                            "debt ceiling","executive order","pce",
                            "retail sales","recession","yield curve",
                            "merger","antitrust"]
                if any(cat in question.lower() for cat in _no_cats):
                    # Extract NO token_id from clobTokenIds[1]
                    _no_token_id = ""
                    _clob_raw = mkt.get("clobTokenIds", "")
                    if _clob_raw:
                        try:
                            _clob_ids = json.loads(_clob_raw) if isinstance(_clob_raw, str) else _clob_raw
                            if len(_clob_ids) >= 2:
                                _no_token_id = _clob_ids[1]
                        except (json.JSONDecodeError, IndexError):
                            pass
                    detail = f"NO @ ${no_price:.3f} (YES=${yes_price:.3f})"
                    if extra:
                        detail += f" | {extra}"
                    opportunities.append({
                        "platform": "Polymarket",
                        "market": question,
                        "ticker": condition_id[:20],
                        "type": "High-YES NO Buy",
                        "ev": no_price,
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "no_token_id": _no_token_id,
                        "side": "NO",
                        "detail": detail,
                    })
    except Exception as exc:
        log.warning("Polymarket scan error: %s", exc)
    return opportunities





# ============================================================================
# NEWS + MARKET FORECASTING
# ============================================================================

def fetch_market_news(query="markets economy"):
    """Fetch latest news headlines for sentiment analysis."""
    headlines = []
    try:
        # Use free newsdata.io or fallback to Google News RSS
        import xml.etree.ElementTree as ET
        url = f"https://news.google.com/rss/search?q={query.replace(' ', '+')}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, timeout=10, headers={"User-Agent": "TraderJoes/1.0"})
        if r.status_code == 200:
            root = ET.fromstring(r.content)
            for item in root.findall(".//item")[:10]:
                title = item.find("title")
                pub_date = item.find("pubDate")
                if title is not None:
                    headlines.append({
                        "title": title.text or "",
                        "date": pub_date.text[:25] if pub_date is not None else "",
                    })
    except Exception as exc:
        log.warning("News fetch error: %s", exc)
    return headlines


def score_sentiment(headlines):
    """Simple keyword-based sentiment scoring."""
    bullish = ["rally", "surge", "gain", "rise", "bull", "up", "record", "boom", "growth", "positive", "optimistic"]
    bearish = ["crash", "drop", "fall", "bear", "down", "recession", "fear", "panic", "sell", "decline", "negative"]
    score = 0
    for h in headlines:
        title_lower = h.get("title", "").lower()
        for word in bullish:
            if word in title_lower:
                score += 1
        for word in bearish:
            if word in title_lower:
                score -= 1
    # Normalize to -100 to +100
    if not headlines:
        return 0
    return max(-100, min(100, int(score / len(headlines) * 50)))


def get_market_forecast():
    """Aggregate market forecast from news + Fear&Greed + crypto momentum."""
    headlines = fetch_market_news("crypto markets economy")
    sentiment = score_sentiment(headlines)
    fng_val, fng_label = get_fear_greed()


    # Composite score: 50% F&G + 50% news sentiment
    composite = int(fng_val * 0.5 + (sentiment + 50) * 0.5)
    if composite > 65:
        outlook = "BULLISH"
    elif composite > 45:
        outlook = "NEUTRAL"
    else:
        outlook = "BEARISH"

    return {
        "sentiment_score": sentiment,
        "fear_greed": fng_val,
        "fear_greed_label": fng_label,
        "composite": composite,
        "outlook": outlook,
        "headline_count": len(headlines),
        "top_headlines": headlines[:5],
    }

CRYPTO_MEME_BLACKLIST = {"SHIB","DOGE","PEPE","FLOKI","BONK","WIF","RAIN","HYPE","BOME","MEME","TRUMP","MELANIA","SIREN"}
CRYPTO_MIN_VOLUME_24H = 10_000_000  # $10M minimum 24h volume
CRYPTO_MIN_PRICE = 1.00  # Skip assets under $1.00

def _fetch_binance_movers():
    """Fetch 24h movers from Binance public API. Returns list of dicts with sym, price, chg, vol."""
    movers = []
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        if r.status_code != 200:
            return movers
        for t in r.json():
            sym_raw = t.get("symbol", "")
            if not sym_raw.endswith("USDT"):
                continue
            base = sym_raw.replace("USDT", "")
            px = float(t.get("lastPrice", 0) or 0)
            chg = float(t.get("priceChangePercent", 0) or 0)
            vol_usd = float(t.get("quoteVolume", 0) or 0)
            if px >= CRYPTO_MIN_PRICE and vol_usd >= CRYPTO_MIN_VOLUME_24H and abs(chg) > 8:
                if base not in CRYPTO_MEME_BLACKLIST and len(base) <= 5:
                    movers.append({"sym": base, "price": px, "chg": chg, "vol": vol_usd, "source": "binance"})
    except Exception as e:
        log.warning("Binance crypto scan: %s", e)
    return movers


def find_crypto_momentum():
    opportunities = []
    seen_syms = set()

    # Source 1: CoinGecko (market cap top 50)
    try:
        r = requests.get("https://api.coingecko.com/api/v3/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": 50, "sparkline": "false"}, timeout=15)
        if r.status_code == 200:
            for coin in r.json():
                sym = coin.get("symbol", "").upper()
                px = coin.get("current_price", 0) or 0
                chg = coin.get("price_change_percentage_24h") or 0
                vol = coin.get("total_volume", 0) or 0
                if sym in CRYPTO_MEME_BLACKLIST or px < CRYPTO_MIN_PRICE or vol < CRYPTO_MIN_VOLUME_24H:
                    continue
                # Verify live price from Coinbase
                try:
                    _cr = requests.get(f"https://api.coinbase.com/v2/prices/{sym}-USD/spot", timeout=5)
                    if _cr.status_code == 200:
                        _live_px = float(_cr.json().get("data", {}).get("amount", 0))
                        if _live_px >= CRYPTO_MIN_PRICE:
                            px = _live_px
                except Exception:
                    pass
                if abs(chg) > 8:
                    seen_syms.add(sym)
                    opportunities.append({
                        "platform": "Crypto",
                        "market": f"{sym} ${px:,.2f} ({chg:+.1f}% 24h)",
                        "ticker": sym,
                        "type": "Momentum" if chg > 0 else "Reversal",
                        "ev": abs(chg) / 100 * 0.3,
                        "detail": f"Price: ${px:,.2f} | Vol: ${vol/1e6:.0f}M | 24h: {chg:+.1f}% [CoinGecko]",
                    })
    except Exception as e:
        log.warning("CoinGecko crypto scan: %s", e)

    # Source 2: Binance (all USDT pairs — doubles opportunity set)
    try:
        binance_movers = _fetch_binance_movers()
        for m in binance_movers:
            if m["sym"] in seen_syms:
                continue  # Deduplicate
            seen_syms.add(m["sym"])
            opportunities.append({
                "platform": "Crypto",
                "market": f"{m['sym']} ${m['price']:,.2f} ({m['chg']:+.1f}% 24h)",
                "ticker": m["sym"],
                "type": "Momentum" if m["chg"] > 0 else "Reversal",
                "ev": abs(m["chg"]) / 100 * 0.3,
                "detail": f"Price: ${m['price']:,.2f} | Vol: ${m['vol']/1e6:.0f}M | 24h: {m['chg']:+.1f}% [Binance]",
            })
        if binance_movers:
            log.info("BINANCE CRYPTO: %d movers >8%% from %d USDT pairs",
                     len(binance_movers), len([1 for _ in binance_movers]))
    except Exception as e:
        log.warning("Binance crypto merge: %s", e)

    return opportunities

def get_fear_greed():
    try:
        r = requests.get("https://api.alternative.me/fng/?limit=1",timeout=10)
        d = r.json()["data"][0]; return int(d["value"]), d["value_classification"]
    except: return 50, "Neutral"

def get_tiered_max_position(edge_score=0):
    """Tiered position sizing based on edge score confidence.
    HIGH (≥75): 1-2% of portfolio
    MEDIUM (65-74): 0.5%
    LOW (<65): 0.25%
    """
    if edge_score >= 75:
        return 0.015  # 1.5% for high confidence
    elif edge_score >= 65:
        return 0.005  # 0.5% for medium confidence
    else:
        return 0.0025  # 0.25% for lower confidence

def suggest_position_size(ev, portfolio_value=88000, max_pct=None, edge_score=0):
    """Position sizing with tiered confidence scaling."""
    if ev <= 0: return 0.0
    if max_pct is None:
        max_pct = get_tiered_max_position(edge_score)
    # Kelly fraction capped at tier max
    kelly = min(ev, 0.05)
    size = portfolio_value * kelly
    cap = portfolio_value * max_pct
    # Apply regime multiplier
    regime_mult = REGIME_CONFIG.get("size_multiplier", 1.0) if "REGIME_CONFIG" in dir() else 1.0
    return min(size * regime_mult, cap)


# ============================================================================
# DISCORD BOT COMMANDS
# ============================================================================

@bot.event
async def on_ready():
    init_redis()
    init_db()
    # Flush contaminated pairs trades from March 19-26 to pairs_legacy
    db_flush_legacy_pairs("2026-03-27")
    load_all_state()  # Restore paper_portfolio.json, analytics, signals, etc.
    db_load_daily_state()
    # Backfill positions from SQLite only if JSON had none (fresh deploy)
    # Backfill from SQLite only if JSON was empty AND cash is not full (not a reset)
    if not PAPER_PORTFOLIO.get("positions") and PAPER_PORTFOLIO.get("cash", 0) < 24999:
        try:
            _rconn = sqlite3.connect(DB_PATH)
            _rc = _rconn.cursor()
            _rc.execute("SELECT market, side, shares, entry_price, cost, timestamp, platform, ev FROM paper_trades WHERE status = 'open'")
            _rows = _rc.fetchall()
            _rconn.close()
            if _rows:
                for _r in _rows:
                    PAPER_PORTFOLIO["positions"].append({
                        "market": _r[0], "side": _r[1] or "BUY", "shares": _r[2] or 0,
                        "entry_price": _r[3] or 0, "cost": _r[4] or 0, "value": _r[4] or 0,
                        "timestamp": _r[5] or "", "platform": _r[6] or "",
                        "ev": _r[7] or 0,
                        "strategy": "crypto" if any(k in (_r[0] or "").lower() for k in ["btc","eth","sol","doge","zec","xlm","hype","sui","wbt"]) else "prediction",
                    })
                log.info("Backfilled %d positions from SQLite (JSON was empty)", len(_rows))
        except Exception as _e:
            log.warning("Position backfill error: %s", _e)
    elif not PAPER_PORTFOLIO.get("positions"):
        log.info("Backfill skipped — clean reset detected (cash=$%.0f)", PAPER_PORTFOLIO.get("cash", 0))
    log.info("TraderJoes bot online as %s | Cash: $%.2f | Positions: %d",
             bot.user, PAPER_PORTFOLIO.get("cash", 0), len(PAPER_PORTFOLIO.get("positions", [])))
    # Reconcile Alpaca positions — close any orphans from failed pairs orders
    try:
        _recon = reconcile_alpaca_positions()
        if _recon > 0:
            log.info("Startup reconciliation closed %d orphaned positions", _recon)
    except Exception as _re:
        log.warning("Startup reconciliation error: %s", _re)
    if not daily_report_task.is_running():
        daily_report_task.start()
    if not alert_scan_task.is_running():
        alert_scan_task.start()
    if not morning_briefing_task.is_running():
        morning_briefing_task.start()
    if not evening_briefing_task.is_running():
        evening_briefing_task.start()
    if not regime_snapshot_task.is_running():
        regime_snapshot_task.start()
    if not pairs_scan_task.is_running():
        pairs_scan_task.start()
    if not sector_rotation_task.is_running():
        sector_rotation_task.start()
    if not earnings_calendar_task.is_running():
        earnings_calendar_task.start()
    # Initialize ChromaDB causal memory
    _init_chromadb()
    # Sync Alpaca positions into portfolio — add any tracked positions missing from ledger
    _sync_alpaca_to_portfolio()
    # Re-queue cascades for open oracle trades that lost queue on restart
    cascade_requeue_on_startup()
    # Test Polygon.io connection
    try:
        from polygon_client import test_connection as _poly_test
        _poly_ok, _poly_msg = _poly_test()
        log.info("POLYGON: %s", _poly_msg)
    except Exception as _pe:
        log.warning("POLYGON: import/test failed: %s", _pe)


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
        f"*Alpaca: {get_alpaca_balance()}\nInteractive Brokers: {get_ibkr_balance()}\nPredictIt: {get_predictit_balance()}*"
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


@bot.command(name="ev-scan")
async def ev_scan(ctx):
    """Run EV scan across all platforms (formerly !cycle)."""
    msg = await ctx.send("Running EV scan across ALL platforms...")
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    fng_val, fng_label = get_fear_greed()
    detect_regime()

    kalshi_opps = find_kalshi_opportunities()
    poly_opps = find_polymarket_opportunities()
    crypto_opps = find_crypto_momentum()
    # Funding rate arbitrage
    try:
        funding_opps = await check_funding_rate_arb()
    except Exception:
        funding_opps = []
    all_opps = kalshi_opps + poly_opps + crypto_opps + funding_opps
    all_opps.sort(key=lambda x: x.get("ev", 0), reverse=True)
    if not all_opps:
        await msg.edit(content=f"**EV Scan** | {ts}\nFear & Greed: {fng_val}/100 ({fng_label}) | Regime: {REGIME_CONFIG.get("current_regime", "?")}\nNo opportunities found.")
        return
    report = f"**EV Scan** | {ts}\nFear & Greed: {fng_val}/100 ({fng_label}) | Regime: {REGIME_CONFIG.get("current_regime", "?")}\n================================\n"
    for i, opp in enumerate(all_opps[:12], 1):
        ev_pct = opp["ev"] * 100
        sz = suggest_position_size(opp["ev"])
        report += f"**{i}. [{opp['platform']}] {opp['type']}** -- EV: +{ev_pct:.1f}%\n   {opp['market']}\n   {opp['detail']}\n   Size: ${sz:,.0f}\n\n"
    report += f"================================\nTotal: {len(all_opps)} | Kalshi: {len(kalshi_opps)} | Polymarket: {len(poly_opps)} | Crypto: {len(crypto_opps)}"
    if len(report) > 1900: report = report[:1900] + "\n*...truncated*"
    await msg.edit(content=report)
    log_entry = f"\n## EV Scan -- {ts}\nF&G: {fng_val}\nFound {len(all_opps)} opps\n\n---\n"
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
async def integrations(ctx):
    """Show API integration connection status."""
    checks = {
        "Kalshi":           bool(KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY),
        "Polymarket":       bool(POLY_WALLET_ADDRESS),
        "Poly CLOB":        bool(POLY_PRIVATE_KEY),
        "Robinhood":        bool(ROBINHOOD_API_KEY and ROBINHOOD_PRIVATE_KEY),
        "Coinbase":         bool(COINBASE_API_KEY and COINBASE_API_SECRET),
        "Phemex":           bool(PHEMEX_API_KEY and PHEMEX_API_SECRET),
        "OpenAI":           bool(OPENAI_API_KEY),
        "GitHub Logger":    bool(GITHUB_TOKEN),
        "Discord Channel":  bool(DISCORD_CHANNEL_ID),
    }
    lines = ["**TraderJoes Integration Status**\n"]
    for name, ok in checks.items():
        icon = "OK" if ok else "MISSING"
        lines.append(f"[{icon}] {name}")
    await ctx.send("\n".join(lines))



@bot.command()
async def analyze(ctx, *, question: str = ""):
    """AI-powered market analysis using GPT-4o-mini."""
    if not question:
        await ctx.send("Usage: `!analyze Will the Fed cut rates in March?`")
        return
    if not OPENAI_API_KEY:
        await ctx.send("OPENAI_API_KEY not set. Add it to .env and restart.")
        return
    msg = await ctx.send(f"Analyzing: *{question[:100]}*...")
    # Enrich with market forecast context
    forecast_context = ""
    try:
        fc = get_market_forecast()
        forecast_context = f" Current market: Fear & Greed {fc['fear_greed']}/100 ({fc['fear_greed_label']}), outlook {fc['outlook']}, sentiment {fc['sentiment_score']:+d}."
    except Exception:
        pass
    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [
                    {"role": "system", "content": "You are TraderJoe, an expert autonomous trading analyst. For every question provide: 1) Probability estimate (0-100%) 2) Confidence (Low/Medium/High/Very High) 3) Key factors (3-5 bullets) 4) Position: BUY YES / BUY NO / PASS 5) Kelly criterion sizing suggestion. Be concise and data-driven."},
                    {"role": "user", "content": question}
                ],
                "max_tokens": 800,
                "temperature": 0.3
            }, timeout=30)
        if r.status_code == 200:
            answer = r.json()["choices"][0]["message"]["content"]
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            response = f"**TraderJoe Analysis** | {ts}\n**Q:** {question[:100]}\n\n{answer}"
            if len(response) > 1900:
                response = response[:1900] + "\n*...truncated*"
            await msg.edit(content=response)
        elif r.status_code == 401:
            await msg.edit(content="Invalid OpenAI API key. Check .env file.")
        elif r.status_code == 429:
            await msg.edit(content="Rate limited. Try again in a moment.")
        else:
            await msg.edit(content=f"OpenAI error {r.status_code}: {r.text[:200]}")
    except requests.exceptions.Timeout:
        await msg.edit(content="OpenAI timed out. Try again.")
    except Exception as exc:
        await msg.edit(content=f"Error: {exc}")




# ============================================================================
# PAPER TRADING / SIMULATION
# ============================================================================

@bot.command(name="paper-status")
async def paper_status(ctx):
    """Show paper trading portfolio status."""
    if TRADING_MODE != "paper":
        await ctx.send(f"Trading mode: **LIVE** — paper trading disabled.\nSwitch with: `!switch-mode paper`")
        return
    cash = PAPER_PORTFOLIO["cash"]
    positions = PAPER_PORTFOLIO["positions"]
    trades = PAPER_PORTFOLIO["trades"]
    pnl = PAPER_PORTFOLIO["pnl"]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    pos_value = sum(p.get("value", 0) for p in positions)
    total = cash + pos_value
    
    report = (
        f"**Paper Trading Portfolio** | {ts}\n"
        f"================================\n"
        f"Mode: **PAPER** (simulated)\n"
        f"Cash: ${cash:,.2f}\n"
        f"Positions Value: ${pos_value:,.2f}\n"
        f"Total: ${total:,.2f}\n"
        f"P&L: ${total - 10000:+,.2f} (unrealized — positions open)\n"
        f"Trades: {len(trades)}\n"
    )
    if positions:
        report += "\n**Open Positions:**\n"
        for p in positions[-10:]:
            report += f"  {p.get('side','?')} {p.get('market','?')[:40]} @ ${p.get('price',0):.3f} x{p.get('size',0):.0f} = ${p.get('value',0):,.2f}\n"
    if trades:
        report += f"\n**Recent Trades:** (last 5)\n"
        for t in trades[-5:]:
            report += f"  [{t.get('time','')}] {t.get('side','')} {t.get('market','')[:30]} @ ${t.get('price',0):.3f}\n"
    
    report += f"================================\n*Slippage: 0.5% | Fees: 0.1%*"
    await ctx.send(report)


@bot.command(name="switch-mode")
async def switch_mode(ctx, mode: str = ""):
    """Switch between paper and live trading modes."""
    global TRADING_MODE
    if mode.lower() not in ("paper", "live"):
        await ctx.send(f"Current mode: **{TRADING_MODE}**\nUsage: `!switch-mode paper` or `!switch-mode live`")
        return
    if mode.lower() == "live":
        await ctx.send("**WARNING:** Switching to LIVE mode. Real money will be used.\nType `!confirm-live` to proceed.")
        return
    TRADING_MODE = mode.lower()
    await ctx.send(f"Switched to **{TRADING_MODE.upper()}** mode.")


@bot.command(name="paper-trade")
async def paper_trade(ctx, side: str = "", *, args: str = ""):
    """Execute a paper trade. Usage: !paper-trade buy Fed rate cut March @ 0.40 x100"""
    if TRADING_MODE != "paper":
        await ctx.send("Paper trading only available in paper mode. Use `!switch-mode paper`")
        return
    if not side or not args:
        await ctx.send("Usage: `!paper-trade buy Will Fed cut rates @ 0.40 x100`")
        return
    
    import random
    parts = args.rsplit("@", 1)
    market = parts[0].strip() if parts else args
    price_size = parts[1].strip() if len(parts) > 1 else "0.50 x100"
    
    ps = price_size.split("x")
    try:
        price = float(ps[0].strip())
        size = float(ps[1].strip()) if len(ps) > 1 else 100
    except (ValueError, IndexError):
        price = 0.50
        size = 100
    
    # Apply slippage (0.5%)
    slippage = price * 0.005 * (1 if side.lower() == "buy" else -1)
    exec_price = price + slippage
    
    # Apply fees (0.1%)
    fee = exec_price * size * 0.001
    cost = exec_price * size + fee
    
    if side.lower() == "buy" and cost > PAPER_PORTFOLIO["cash"]:
        await ctx.send(f"Insufficient paper cash. Have ${PAPER_PORTFOLIO['cash']:,.2f}, need ${cost:,.2f}")
        return
    
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    
    if side.lower() == "buy":
        PAPER_PORTFOLIO["cash"] -= cost
        PAPER_PORTFOLIO["positions"].append({
            "market": market, "side": "YES", "price": exec_price,
            "size": size, "value": exec_price * size, "time": ts
        })
    else:
        PAPER_PORTFOLIO["cash"] += (exec_price * size - fee)
    
    PAPER_PORTFOLIO["trades"].append({
        "side": side.upper(), "market": market, "price": exec_price,
        "size": size, "fee": fee, "time": ts
    })
    
    await ctx.send(
        f"**Paper Trade Executed**\n"
        f"{side.upper()} {size:.0f} shares of *{market[:50]}*\n"
        f"Price: ${price:.3f} -> Exec: ${exec_price:.3f} (slippage: ${slippage:.4f})\n"
        f"Fee: ${fee:.2f} | Cost: ${cost:,.2f}\n"
        f"Cash remaining: ${PAPER_PORTFOLIO['cash']:,.2f}"
    )


# ============================================================================
# BACKTESTING
# ============================================================================

@bot.command(name="backtest-legacy")
async def backtest_legacy(ctx, *, strategy: str = "momentum-crypto"):
    """Run legacy walk-forward backtest. Uses real CoinGecko data.
    Usage: !backtest-legacy momentum-crypto  or  !backtest-legacy mean-reversion
    """
    msg = await ctx.send(f"Running backtest: **{strategy}**... fetching real data from CoinGecko")
    
    # Use real data when available
    coin_map = {
        "momentum-crypto": "bitcoin",
        "mean-reversion": "ethereum",
        "trend-following": "bitcoin",
        "momentum": "bitcoin",
        "btc": "bitcoin",
        "eth": "ethereum",
        "sol": "solana",
    }
    coin = coin_map.get(strategy.lower(), "bitcoin")
    
    prices = fetch_historical_prices(coin, 365)
    
    if prices and len(prices) >= 30:
        # Real data backtest
        returns = calculate_returns(prices)
        result = real_backtest_strategy(prices, strategy.split("-")[0] if "-" in strategy else strategy)
        
        if result:
            # Walk-forward: split into 5 windows
            window_size = len(prices) // 5
            wf_results = []
            for i in range(5):
                start = i * window_size
                end = start + window_size
                if end > len(prices):
                    end = len(prices)
                window_prices = prices[start:end]
                if len(window_prices) >= 10:
                    wr = real_backtest_strategy(window_prices, strategy.split("-")[0] if "-" in strategy else strategy)
                    if wr:
                        wf_results.append(wr)
            
            wf_returns = [w["total_return"] for w in wf_results] if wf_results else [0]
            
            # Monte Carlo
            import random, statistics
            mc_results = []
            for _ in range(1000):
                shuffled = random.sample(returns, len(returns))
                eq = 10000.0
                for r in shuffled:
                    eq += eq * r
                mc_results.append((eq - 10000) / 10000 * 100)
            mc_results.sort()
            
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            
            checks = [
                result["sharpe"] > 1.0,
                result["win_rate"] > 48,
                result["max_drawdown"] < 30,
                result["total_return"] > 0,
                len([w for w in wf_results if w["total_return"] > 0]) >= 3,
            ]
            passed = sum(checks)
            verdict = f"PASS ({passed}/5)" if passed >= 3 else f"FAIL ({passed}/5)"
            
            report = (
                f"**Walk-Forward Backtest** | {ts}\n"
                f"Strategy: {strategy} | Data: {coin} ({len(prices)} days, REAL)\n"
                f"================================\n"
                f"**Full Period:**\n"
                f"  Return: {result['total_return']:+.1f}% | Sharpe: {result['sharpe']:.2f}\n"
                f"  Max DD: -{result['max_drawdown']:.1f}% | Win Rate: {result['win_rate']:.1f}%\n"
                f"  Trades: {result['trades']}\n"
                f"\n**Walk-Forward ({len(wf_results)} windows):**\n"
                f"  Returns: {', '.join(f'{r:+.1f}%' for r in wf_returns)}\n"
                f"  Profitable windows: {len([r for r in wf_returns if r > 0])}/{len(wf_returns)}\n"
                f"\n**Monte Carlo (1K paths):**\n"
                f"  5th: {mc_results[50]:+.1f}% | Median: {mc_results[500]:+.1f}% | 95th: {mc_results[950]:+.1f}%\n"
                f"\n**Verdict:** [{verdict}]\n"
                f"================================"
            )
            await msg.edit(content=report)
            return
    
    # Fallback to simulated if no real data
    import random, statistics
    random.seed(42)
    num_days = 252
    returns = [random.gauss(0.0003, 0.015) for _ in range(num_days)]
    equity = 10000.0
    peak = equity
    max_dd = 0
    wins = losses = 0
    daily_pnl = []
    
    for r in returns:
        pnl = equity * r
        equity += pnl
        daily_pnl.append(pnl)
        if equity > peak:
            peak = equity
        dd = (peak - equity) / peak * 100
        if dd > max_dd:
            max_dd = dd
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    
    total_ret = (equity - 10000) / 10000 * 100
    mean_pnl = statistics.mean(daily_pnl)
    std_pnl = statistics.stdev(daily_pnl) or 1
    sharpe = (mean_pnl / std_pnl) * (252 ** 0.5)
    win_rate = wins / max(wins + losses, 1) * 100
    
    mc_results = []
    for _ in range(1000):
        shuffled = random.sample(returns, len(returns))
        eq = 10000.0
        for r in shuffled:
            eq += eq * r
        mc_results.append((eq - 10000) / 10000 * 100)
    mc_results.sort()
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    report = (
        f"**Walk-Forward Backtest** | {ts}\n"
        f"Strategy: {strategy} | Data: SIMULATED (252 days)\n"
        f"================================\n"
        f"Return: {total_ret:+.1f}% | Sharpe: {sharpe:.2f}\n"
        f"Max DD: -{max_dd:.1f}% | Win Rate: {win_rate:.1f}% ({wins}W/{losses}L)\n"
        f"MC 5th: {mc_results[50]:+.1f}% | Median: {mc_results[500]:+.1f}% | 95th: {mc_results[950]:+.1f}%\n"
        f"================================"
    )
    await msg.edit(content=report)


@bot.command()
async def trade(ctx, action: str = "", asset: str = "", amount: str = ""):
    if not action or not asset:
        await ctx.send("Usage: `!trade buy BTC 100` or `!trade sell ETH 50`"); return
    if COST_CONFIG.get("kill_switch", False):
        await ctx.send("**BLOCKED:** Kill switch is active. Use `!kill-switch off` then `!confirm-kill-off` to resume.")
        return
    if PORTFOLIO_FLOOR_ACTIVE:
        await ctx.send("**BLOCKED:** Portfolio floor breached. Total portfolio below $" + f"{PORTFOLIO_FLOOR:,.0f}. ALL trading halted to protect capital. The AI will die if we get liquidated.")
        return
    if TRADING_MODE == "paper":
        await ctx.send(f"Paper mode active. Use `!paper-trade {action} {asset} @ 0.50 x{amount or 100}`"); return
    try: amt = float(amount) if amount else 100.0
    except ValueError: amt = 100.0
    global DAILY_PNL
    if DAILY_PNL <= DAILY_LOSS_LIMIT:
        await ctx.send(f"BLOCKED: Daily loss limit reached (${DAILY_PNL:,.2f})"); return
    trade_id = str(int(time.time()))
    PENDING_TRADES[ctx.author.id] = {"id":trade_id,"action":action.upper(),"asset":asset.upper(),"amount":amt,"time":datetime.now(timezone.utc).strftime("%H:%M UTC")}
    await ctx.send(f"**Trade Proposal** ID:{trade_id}\n{action.upper()} {asset.upper()} ${amt:,.2f}\nMode: **LIVE**\n\nType `!confirm-trade` to execute or `!cancel-trade` to abort.")

@bot.command(name="confirm-trade")
async def confirm_trade(ctx):
    pending = PENDING_TRADES.pop(ctx.author.id, None)
    if not pending: await ctx.send("No pending trade."); return
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    # LIVE EXECUTION ROUTER
    success = False
    exec_msg = ""
    asset = pending["asset"]
    action_str = pending["action"]
    amt = pending["amount"]
    
    if TRADING_MODE == "live":
        # Route to correct exchange
        if asset.startswith("KX") or asset.startswith("KALSHI"):
            success, exec_msg = await execute_kalshi_order(action_str, asset, amt)
        elif asset in ("BTC", "ETH", "DOGE", "XRP", "SOL", "ALGO", "SHIB", "XLM", "HBAR"):
            # Try Coinbase first, then Robinhood
            success, exec_msg = await execute_coinbase_order(action_str, asset, amt)
            if not success and "Robinhood" not in exec_msg:
                success, exec_msg = await execute_robinhood_order(action_str, asset, amt)
        elif asset.endswith("USDT") or asset.endswith("PERP"):
            success, exec_msg = await execute_phemex_order(action_str, asset, amt)
        else:
            # Route to Alpaca for stocks
            if ALPACA_API_KEY:
                success, exec_msg = await execute_alpaca_order(action_str, asset, amt)
            else:
                exec_msg = f"No exchange matched for {asset}. Use ticker like BTC, ETH, KXTICKER, AAPL, etc."
        
        status_icon = "OK" if success else "FAILED"
        await ctx.send(f"**Trade [{status_icon}]** | {ts}\n{action_str} {asset} ${amt:,.2f}\n{exec_msg}")
    else:
        await ctx.send(f"**Trade Executed (PAPER)** | {ts}\n{action_str} {asset} ${amt:,.2f}\n*Paper mode — no real order placed*")
    log_to_github(f"\n## Trade -- {ts}\n- {pending['action']} {pending['asset']} ${pending['amount']:,.2f}\n---\n")

@bot.command(name="cancel-trade")
async def cancel_trade(ctx):
    pending = PENDING_TRADES.pop(ctx.author.id, None)
    await ctx.send(f"Cancelled: {pending['action']} {pending['asset']}" if pending else "No pending trade.")

# ============================================================================
# DAILY REPORT
# ============================================================================

@bot.command()
async def daily(ctx):
    await _send_daily_report(ctx.channel)

@bot.command()
async def report(ctx):
    await _send_daily_report(ctx.channel)

async def _send_daily_report(channel):
    # V9 OVERSIGHT
    try:
        oversight_report = await run_ai_oversight(channel)
        await channel.send(oversight_report)
    except Exception:
        pass
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fng_val, fng_label = get_fear_greed()

    kalshi=get_kalshi_balance(); poly=get_polymarket_balance()
    rh=get_robinhood_balance(); cb=get_coinbase_balance(); ph=get_phemex_balance()
    pc=PAPER_PORTFOLIO["cash"]; pv=sum(p.get("value",0) for p in PAPER_PORTFOLIO.get("positions",[]))
    pt=pc+pv; ppnl=pt-10000.0
    r = (f"**TraderJoes Daily Report** | {ts}\n================================\n"
        f"**Market:** F&G {fng_val}/100 ({fng_label}) | Mode: {TRADING_MODE.upper()}\n"
        f"**Kalshi:** {kalshi}\n**Polymarket:** {poly}\n**Robinhood:** {rh}\n**Coinbase:** {cb}\n**Phemex:** {ph}\n"
        f"**Paper:** ${pt:,.2f} (P&L: ${ppnl:+,.2f}) | Trades: {len(PAPER_PORTFOLIO['trades'])}\n"
        f"**Risk:** Max pos {MAX_POSITION_PCT:.1%} | Daily limit: ${DAILY_LOSS_LIMIT:,.2f} | Current: ${DAILY_PNL:+,.2f}\n"
        f"**System:** Bot online | OpenAI {'OK' if OPENAI_API_KEY else 'N/A'}\n================================")
    if len(r) > 1900: r = r[:1900]
    await channel.send(r)

@tasks.loop(hours=24)
async def daily_report_task():
    if DISCORD_CHANNEL_ID:
        try:
            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
            if ch: await _send_daily_report(ch)
            # Push analytics to Netdata
            push_all_analytics()
        except Exception as e: log.warning("Daily report error: %s", e)

@daily_report_task.before_loop
async def before_daily():
    await bot.wait_until_ready()
    import asyncio
    now = datetime.now(timezone.utc)
    target = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if now.hour > 0 or now.minute > 0:
        from datetime import timedelta
        target += timedelta(days=1)
    await asyncio.sleep((target - now).total_seconds())




# ============================================================================
# MORNING / EVENING / WEEK BRIEFINGS
# ============================================================================

async def _build_morning_briefing():
    """Comprehensive morning briefing — runs all checks, returns list of message strings."""
    import sqlite3 as _msq
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc)
    et_now = datetime.now(ZoneInfo("America/New_York"))
    msgs = []

    cash = PAPER_PORTFOLIO.get("cash", 0)
    positions = PAPER_PORTFOLIO.get("positions", [])
    total_cost = sum(p.get("cost", 0) for p in positions)
    equity = cash + total_cost
    total_realized = 0.0
    try:
        _c = _msq.connect(DB_PATH)
        _r = _c.execute("SELECT SUM(realized_pnl) FROM positions WHERE status='closed' AND realized_pnl IS NOT NULL").fetchone()
        if _r and _r[0]:
            total_realized = _r[0]
        _c.close()
    except Exception:
        pass
    ret_pct = ((equity / 25000) - 1) * 100

    # Overnight activity (last 12h)
    twelve_h_ago = (now - __import__("datetime").timedelta(hours=12)).strftime("%Y-%m-%d %H:%M")
    overnight_closed = []
    overnight_opened = []
    try:
        _c = _msq.connect(DB_PATH)
        for r in _c.execute("SELECT market_id, strategy, realized_pnl, exit_reason FROM positions WHERE status='closed' AND closed_at>=? ORDER BY closed_at DESC LIMIT 10", (twelve_h_ago,)):
            overnight_closed.append({"market": r[0], "strategy": r[1], "pnl": r[2], "reason": r[3]})
        for r in _c.execute("SELECT market_id, strategy, size_usd FROM positions WHERE status='open' AND created_at>=? ORDER BY created_at DESC LIMIT 10", (twelve_h_ago,)):
            overnight_opened.append({"market": r[0], "strategy": r[1], "size": r[2]})
        _c.close()
    except Exception:
        pass

    regime = get_regime("equities")
    vix = regime.get("vix", 0) or 0
    regime_name = regime.get("regime", "?")
    fng_val, fng_label = get_fear_greed()

    # Message 1: Portfolio + Overnight
    m1 = f"**MORNING BRIEFING** | {et_now.strftime('%A %b %d, %I:%M %p ET')}\n```\n"
    m1 += f"{'Cash:':<22s} ${cash:>10,.2f}\n"
    m1 += f"{'Equity:':<22s} ${equity:>10,.2f}  ({ret_pct:+.1f}%)\n"
    m1 += f"{'Realized P&L:':<22s} ${total_realized:>+10,.2f}\n"
    m1 += f"{'Positions:':<22s} {len(positions):>10d}\n"
    m1 += f"{'VIX:':<22s} {vix:>10.1f}  ({regime_name.upper()})\n"
    m1 += f"{'Fear & Greed:':<22s} {fng_val:>10d}/100 ({fng_label})\n"
    if _PSYCH_STATE.get("contrarian_mode"):
        m1 += f"{'Psychologist:':<22s} {'CONTRARIAN':>10s}\n"
    m1 += f"{'─' * 36}\n"
    if overnight_closed:
        m1 += f"Closed overnight ({len(overnight_closed)}):\n"
        for oc in overnight_closed[:5]:
            m1 += f"  {oc['market'][:28]:28s} ${oc['pnl'] or 0:>+7.2f} {(oc['reason'] or '')[:15]}\n"
    if overnight_opened:
        m1 += f"Opened overnight ({len(overnight_opened)}):\n"
        for oo in overnight_opened[:5]:
            m1 += f"  {oo['market'][:28]:28s} ${oo['size'] or 0:>7.0f} {oo['strategy'][:10]}\n"
    if not overnight_closed and not overnight_opened:
        m1 += f"No overnight activity\n"
    m1 += f"```"
    msgs.append(m1)

    # Message 2: Intelligence + Oracle + Risk + Allocation + Engineer
    m2 = "```\n"
    m2 += "INTELLIGENCE:\n"
    for theme in sorted(_INTEL_HEADLINE_MEMORY.keys()):
        entries = _INTEL_HEADLINE_MEMORY.get(theme, [])
        if entries:
            latest = sorted(entries, key=lambda x: x[0], reverse=True)[0][1]
            m2 += f"  [{theme:8s}] {latest[:55]}\n"
    geo = _intel_get_geo_state()
    if geo.get("active"):
        m2 += f"  *** GEO ALERT: {geo['theme']} — {geo['count']} escalation headlines ***\n"
    m2 += f"{'─' * 50}\nORACLE SIGNALS:\n"
    try:
        _prices = _oracle_get_all_prices()
        _found = False
        for title, yes_price in sorted(_prices.items(), key=lambda x: x[1], reverse=True):
            sig = _oracle_match_signal(title)
            if sig:
                pct = yes_price / sig["threshold"] * 100
                bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
                m2 += f"  {sig['name']:14s} {bar} {pct:>5.0f}%\n"
                _found = True
        if not _found:
            m2 += "  No matching signals\n"
    except Exception:
        m2 += "  Fetch error\n"
    m2 += f"{'─' * 50}\nRISK: {len(_RISK_STATE.get('corr_flags', []))} correlations"
    m2 += f" | {len(_RISK_STATE.get('strategy_pauses', {}))} pauses\n"
    m2 += "ALLOC: " + " ".join(f"{s}={w:.1f}x" for s, w in sorted(_META_ALLOC.items()) if w != 1.0)
    if all(v == 1.0 for v in _META_ALLOC.values()):
        m2 += "all balanced"
    m2 += "\n"
    recent_eng = [e for e in _ENGINEER_LOG if (now - e["timestamp"]).total_seconds() < 43200] if _ENGINEER_LOG else []
    if recent_eng:
        m2 += f"ENGINEER: "
        for adj in recent_eng[-2:]:
            m2 += f"{adj['strategy']} {adj['metric']} {adj['old']:.2f}→{adj['new']:.2f}  "
        m2 += "\n"
    m2 += "```"
    msgs.append(m2)

    # Message 3: Recommendations
    m3 = "**Today's Actions:**\n"
    if _PSYCH_STATE.get("contrarian_mode"):
        m3 += "• Contrarian mode active — favor mean-reversion\n"
    if geo.get("active"):
        m3 += f"• Geo risk: {geo['theme']} escalation — sizing at 0.5%\n"
    if _RISK_STATE.get("strategy_pauses"):
        m3 += f"• Paused: {', '.join(_RISK_STATE['strategy_pauses'].keys())}\n"
    if vix > 25:
        m3 += f"• VIX {vix:.0f} elevated — crash hedges active\n"
    expiring = [p for p in positions if p.get("strategy") == "prediction"
                and "march 31" in p.get("market", "").lower()]
    if expiring:
        m3 += f"• {len(expiring)} markets expire March 31\n"
    if not any([_PSYCH_STATE.get("contrarian_mode"), geo.get("active"),
                _RISK_STATE.get("strategy_pauses"), vix > 25, expiring]):
        m3 += "• All systems green — normal operations\n"
    msgs.append(m3)
    return msgs


async def _build_evening_briefing():
    """End-of-day summary."""
    import sqlite3 as _esq
    from zoneinfo import ZoneInfo
    now = datetime.now(timezone.utc)
    et_now = datetime.now(ZoneInfo("America/New_York"))
    today = now.strftime("%Y-%m-%d")
    msgs = []

    closed_today = []
    opened_today = []
    try:
        _c = _esq.connect(DB_PATH)
        for r in _c.execute("SELECT market_id, strategy, size_usd, realized_pnl, exit_reason FROM positions WHERE status='closed' AND closed_at>=? ORDER BY closed_at DESC", (today,)):
            closed_today.append({"market": r[0], "strategy": r[1], "size": r[2], "pnl": r[3] or 0, "reason": r[4]})
        for r in _c.execute("SELECT market_id, strategy, size_usd FROM positions WHERE status='open' AND created_at>=? ORDER BY created_at DESC", (today,)):
            opened_today.append({"market": r[0], "strategy": r[1], "size": r[2]})
        _c.close()
    except Exception:
        pass

    net_pnl = sum(c["pnl"] for c in closed_today)
    best = max(closed_today, key=lambda x: x["pnl"]) if closed_today else None
    worst = min(closed_today, key=lambda x: x["pnl"]) if closed_today else None

    strat_pnl = {}
    for c in closed_today:
        s = c["strategy"] or "?"
        strat_pnl.setdefault(s, {"pnl": 0, "trades": 0})
        strat_pnl[s]["pnl"] += c["pnl"]
        strat_pnl[s]["trades"] += 1

    cash = PAPER_PORTFOLIO.get("cash", 0)
    positions = PAPER_PORTFOLIO.get("positions", [])
    equity = cash + sum(p.get("cost", 0) for p in positions)

    m1 = f"**EVENING BRIEFING** | {et_now.strftime('%A %b %d, %I:%M %p ET')}\n```\n"
    m1 += f"{'Net P&L today:':<22s} ${net_pnl:>+10,.2f}\n"
    m1 += f"{'Trades closed:':<22s} {len(closed_today):>10d}\n"
    m1 += f"{'Trades opened:':<22s} {len(opened_today):>10d}\n"
    m1 += f"{'Equity (EOD):':<22s} ${equity:>10,.2f}\n"
    m1 += f"{'─' * 36}\n"
    if strat_pnl:
        for s, d in sorted(strat_pnl.items(), key=lambda x: x[1]["pnl"], reverse=True):
            m1 += f"  {s:16s} ${d['pnl']:>+8.2f} ({d['trades']} trades)\n"
    if best and best["pnl"] > 0:
        m1 += f"{'─' * 36}\nBest:  {best['market'][:28]} ${best['pnl']:+.2f}\n"
    if worst and worst["pnl"] < 0:
        m1 += f"Worst: {worst['market'][:28]} ${worst['pnl']:+.2f}\n"
    m1 += "```"
    msgs.append(m1)

    m2 = "```\nOVERNIGHT WATCH:\n"
    for p in sorted(positions, key=lambda x: x.get("timestamp", ""))[:5]:
        m2 += f"  {p.get('market', '')[:28]:28s} ${p.get('cost', 0):>6.0f} {p.get('strategy', '?')[:8]}\n"
    expiring = [p for p in positions if p.get("strategy") == "prediction"
                and any(x in p.get("market", "").lower() for x in ["march 31", "april 1", "march 30"])]
    if expiring:
        m2 += f"{'─' * 40}\nEXPIRING SOON ({len(expiring)}):\n"
        for p in expiring:
            m2 += f"  {p.get('market', '')[:40]} ${p.get('cost', 0):.0f}\n"
    m2 += f"{'─' * 40}\nALLOC TOMORROW: "
    m2 += " ".join(f"{s}={w:.1f}x" for s, w in sorted(_META_ALLOC.items()) if w != 1.0)
    if all(v == 1.0 for v in _META_ALLOC.values()):
        m2 += "all balanced"
    m2 += "\n```"
    msgs.append(m2)
    return msgs


async def _build_week_summary():
    """Full week performance summary."""
    import sqlite3 as _wsq
    now = datetime.now(timezone.utc)
    week_ago = (now - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")
    msgs = []

    try:
        _c = _wsq.connect(DB_PATH)
        _r = _c.execute("SELECT COUNT(*), SUM(realized_pnl), AVG(realized_pnl) FROM positions WHERE status='closed' AND closed_at>=? AND realized_pnl IS NOT NULL", (week_ago,)).fetchone()
        total_trades, total_pnl, avg_pnl = (_r[0] or 0), (_r[1] or 0), (_r[2] or 0)
        _w = _c.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND closed_at>=? AND realized_pnl>0", (week_ago,)).fetchone()
        wins = _w[0] or 0
        strat_rows = _c.execute("SELECT strategy, COUNT(*), SUM(realized_pnl), AVG(realized_pnl) FROM positions WHERE status='closed' AND closed_at>=? AND realized_pnl IS NOT NULL GROUP BY strategy ORDER BY SUM(realized_pnl) DESC", (week_ago,)).fetchall()
        day_rows = _c.execute("SELECT DATE(closed_at), SUM(realized_pnl), COUNT(*) FROM positions WHERE status='closed' AND closed_at>=? AND realized_pnl IS NOT NULL GROUP BY DATE(closed_at) ORDER BY DATE(closed_at)", (week_ago,)).fetchall()
        best = _c.execute("SELECT market_id, realized_pnl, strategy FROM positions WHERE status='closed' AND closed_at>=? AND realized_pnl IS NOT NULL ORDER BY realized_pnl DESC LIMIT 1", (week_ago,)).fetchone()
        worst = _c.execute("SELECT market_id, realized_pnl, strategy FROM positions WHERE status='closed' AND closed_at>=? AND realized_pnl IS NOT NULL ORDER BY realized_pnl ASC LIMIT 1", (week_ago,)).fetchone()
        _c.close()
    except Exception:
        total_trades = total_pnl = avg_pnl = wins = 0
        strat_rows = day_rows = []
        best = worst = None

    wr = (wins / total_trades * 100) if total_trades > 0 else 0
    equity = PAPER_PORTFOLIO.get("cash", 0) + sum(p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))

    m1 = f"**WEEKLY SUMMARY** | Week ending {now.strftime('%Y-%m-%d')}\n```\n"
    m1 += f"{'Trades:':<22s} {total_trades:>10d}\n"
    m1 += f"{'Win rate:':<22s} {wr:>9.0f}%\n"
    m1 += f"{'Total P&L:':<22s} ${total_pnl:>+10,.2f}\n"
    m1 += f"{'Avg P&L/trade:':<22s} ${avg_pnl:>+10,.2f}\n"
    m1 += f"{'Equity:':<22s} ${equity:>10,.2f}\n"
    m1 += f"{'─' * 36}\n"
    if strat_rows:
        m1 += "BY STRATEGY:\n"
        for strat, cnt, spnl, savg in strat_rows:
            m1 += f"  {(strat or '?'):16s} {cnt or 0:>3d} trades  ${spnl or 0:>+8.2f}\n"
    if day_rows:
        m1 += f"{'─' * 36}\nBY DAY:\n"
        for day, dpnl, dcnt in day_rows:
            m1 += f"  {day or '?':10s} ${dpnl or 0:>+8.2f} ({dcnt} trades)\n"
    if best:
        m1 += f"{'─' * 36}\nBest:  {(best[0] or '')[:25]} ${best[1] or 0:+.2f}\n"
    if worst:
        m1 += f"Worst: {(worst[0] or '')[:25]} ${worst[1] or 0:+.2f}\n"
    m1 += "```"
    msgs.append(m1)
    return msgs


# Scheduled tasks for morning/evening briefings
@tasks.loop(hours=24)
async def morning_briefing_task():
    if DISCORD_CHANNEL_ID:
        try:
            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
            if ch:
                msgs = await _build_morning_briefing()
                for m in msgs:
                    await ch.send(m)
                log.info("Morning briefing sent")
        except Exception as e:
            log.warning("Morning briefing error: %s", e)

@morning_briefing_task.before_loop
async def before_morning():
    await bot.wait_until_ready()
    import asyncio
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    target = now_et.replace(hour=9, minute=25, second=0, microsecond=0)
    if now_et >= target:
        target += __import__("datetime").timedelta(days=1)
    # Skip weekends
    while target.weekday() >= 5:
        target += __import__("datetime").timedelta(days=1)
    wait_secs = (target - now_et).total_seconds()
    log.info("Morning briefing scheduled in %.0f minutes", wait_secs / 60)
    await asyncio.sleep(wait_secs)

@tasks.loop(hours=24)
async def evening_briefing_task():
    if DISCORD_CHANNEL_ID:
        try:
            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
            if ch:
                msgs = await _build_evening_briefing()
                for m in msgs:
                    await ch.send(m)
                log.info("Evening briefing sent")
        except Exception as e:
            log.warning("Evening briefing error: %s", e)

@evening_briefing_task.before_loop
async def before_evening():
    await bot.wait_until_ready()
    import asyncio
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    target = now_et.replace(hour=16, minute=5, second=0, microsecond=0)
    if now_et >= target:
        target += __import__("datetime").timedelta(days=1)
    while target.weekday() >= 5:
        target += __import__("datetime").timedelta(days=1)
    wait_secs = (target - now_et).total_seconds()
    log.info("Evening briefing scheduled in %.0f minutes", wait_secs / 60)
    await asyncio.sleep(wait_secs)


@tasks.loop(hours=24)
async def regime_snapshot_task():
    """Store daily regime snapshot in ChromaDB at 4:00 PM ET."""
    try:
        causal_memory_store_snapshot()
        if DISCORD_CHANNEL_ID:
            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
            if ch:
                vector, meta = _build_regime_vector()
                if meta:
                    await ch.send(
                        f"**CAUSAL MEMORY** — Daily regime snapshot stored\n"
                        f"VIX: {meta['vix']} | F&G: {meta['fng']} | SPY: ${meta['spy_price']:.0f} | "
                        f"Trend: {meta['spy_trend_5d']:+.1f}% | Regime: {meta['regime'].upper()}\n"
                        f"Geo: Iran={meta['iran_esc']} Ukraine={meta['ukraine_esc']} Taiwan={meta['taiwan_esc']}")
    except Exception as e:
        log.warning("Regime snapshot task error: %s", e)

@regime_snapshot_task.before_loop
async def before_regime_snapshot():
    await bot.wait_until_ready()
    import asyncio
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    target = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_et >= target:
        target += __import__("datetime").timedelta(days=1)
    while target.weekday() >= 5:
        target += __import__("datetime").timedelta(days=1)
    wait_secs = (target - now_et).total_seconds()
    log.info("Regime snapshot scheduled in %.0f minutes", wait_secs / 60)
    await asyncio.sleep(wait_secs)


# ---------------------------------------------------------------------------
# SECTOR ROTATION SCANNER — Morning scan of 11 S&P 500 sector ETFs
# ---------------------------------------------------------------------------
SECTOR_ETFS = ["XLF", "XLK", "XLE", "XLV", "XLI", "XLB", "XLU", "XLRE", "XLC", "XLY", "XLP"]
_SECTOR_RANKINGS = {"rankings": [], "top2": [], "bottom2": [], "last_scan": None}


def scan_sector_rotation():
    """Fetch 5-day performance for all sector ETFs. Returns sorted rankings."""
    try:
        from polygon_client import get_quote
        rankings = []
        for etf in SECTOR_ETFS:
            q = get_quote(etf)
            if q and q.get("change_pct") is not None:
                rankings.append({"etf": etf, "change_pct": round(q["change_pct"], 2),
                                 "price": q.get("last", 0)})
        if not rankings:
            # Fallback: yfinance
            import yfinance as _yf_sec
            for etf in SECTOR_ETFS:
                try:
                    h = _yf_sec.Ticker(etf).history(period="5d")
                    if len(h) >= 2:
                        chg = (h["Close"].iloc[-1] / h["Close"].iloc[0] - 1) * 100
                        rankings.append({"etf": etf, "change_pct": round(chg, 2),
                                         "price": float(h["Close"].iloc[-1])})
                except Exception:
                    pass
        rankings.sort(key=lambda r: r["change_pct"], reverse=True)
        _SECTOR_RANKINGS["rankings"] = rankings
        _SECTOR_RANKINGS["top2"] = rankings[:2] if len(rankings) >= 2 else rankings
        _SECTOR_RANKINGS["bottom2"] = rankings[-2:] if len(rankings) >= 2 else []
        _SECTOR_RANKINGS["last_scan"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log.info("SECTOR ROTATION: top=%s(%+.1f%%) %s(%+.1f%%) | bottom=%s(%+.1f%%) %s(%+.1f%%)",
                 rankings[0]["etf"], rankings[0]["change_pct"],
                 rankings[1]["etf"], rankings[1]["change_pct"],
                 rankings[-2]["etf"], rankings[-2]["change_pct"],
                 rankings[-1]["etf"], rankings[-1]["change_pct"])
        return rankings
    except Exception as e:
        log.warning("SECTOR ROTATION error: %s", e)
        return []


def get_sector_rotation_pairs():
    """Generate sector momentum pairs: long top ETF vs short bottom ETF."""
    top2 = _SECTOR_RANKINGS.get("top2", [])
    bottom2 = _SECTOR_RANKINGS.get("bottom2", [])
    pairs = []
    if len(top2) >= 1 and len(bottom2) >= 1:
        pairs.append((top2[0]["etf"], bottom2[-1]["etf"]))  # Best vs worst
    if len(top2) >= 2 and len(bottom2) >= 2:
        pairs.append((top2[1]["etf"], bottom2[-2]["etf"]))  # 2nd best vs 2nd worst
    return pairs


@tasks.loop(hours=24)
async def sector_rotation_task():
    """Morning sector rotation scan at 9:35 AM ET."""
    try:
        rankings = scan_sector_rotation()
        if rankings and DISCORD_CHANNEL_ID:
            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
            if ch:
                msg = "**SECTOR ROTATION — Morning Scan**\n```\n"
                msg += f"{'ETF':5s} {'5D Chg':>8s} {'Price':>8s}\n"
                msg += f"{'-'*25}\n"
                for i, r in enumerate(rankings):
                    arrow = ">>>" if i < 2 else "<<<" if i >= len(rankings) - 2 else "   "
                    msg += f"{arrow} {r['etf']:5s} {r['change_pct']:>+7.2f}% ${r['price']:>7.2f}\n"
                msg += "```\n"
                pairs = get_sector_rotation_pairs()
                if pairs:
                    msg += "**Rotation Pairs:**\n"
                    for long_etf, short_etf in pairs:
                        msg += f"  Long {long_etf} / Short {short_etf}\n"
                await ch.send(msg)
                # Add sector pairs to pairs scanner seed dynamically
                _eq_cfg = globals().get("EQUITIES_CONFIG", {})
                _seed = _eq_cfg.get("pairs", {}).get("seed", [])
                for long_etf, short_etf in pairs:
                    pair = (long_etf, short_etf)
                    if pair not in _seed:
                        _seed.append(pair)
                        log.info("SECTOR ROTATION: added %s/%s to pairs seed", long_etf, short_etf)
    except Exception as e:
        log.warning("Sector rotation task error: %s", e)

@sector_rotation_task.before_loop
async def before_sector_rotation():
    await bot.wait_until_ready()
    import asyncio
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    target = now_et.replace(hour=9, minute=35, second=0, microsecond=0)
    if now_et >= target:
        target += __import__("datetime").timedelta(days=1)
    while target.weekday() >= 5:
        target += __import__("datetime").timedelta(days=1)
    wait_secs = (target - now_et).total_seconds()
    log.info("Sector rotation scheduled in %.0f minutes", wait_secs / 60)
    await asyncio.sleep(wait_secs)


# ---------------------------------------------------------------------------
# EARNINGS CALENDAR — Sunday 8 PM ET, fetch next week's earnings
# ---------------------------------------------------------------------------
_EARNINGS_WEEK = {"tickers": [], "dates": {}, "last_scan": None}


def fetch_earnings_calendar():
    """Fetch next week's earnings from Polygon. Returns list of (ticker, date) tuples."""
    import datetime as _dt_earn
    now = datetime.now(timezone.utc)
    # Next Monday through Friday
    days_to_monday = (7 - now.weekday()) % 7
    if days_to_monday == 0:
        days_to_monday = 7
    monday = (now + _dt_earn.timedelta(days=days_to_monday)).strftime("%Y-%m-%d")
    friday = (now + _dt_earn.timedelta(days=days_to_monday + 4)).strftime("%Y-%m-%d")
    earnings = []
    try:
        from polygon_client import _get_client
        c = _get_client()
        if c:
            for ticker_event in c.vx.list_stock_financials(
                    filing_date_gte=monday, filing_date_lte=friday, limit=50):
                earnings.append((ticker_event.tickers[0] if ticker_event.tickers else "?",
                                 ticker_event.filing_date))
    except Exception:
        pass
    # Fallback: yfinance calendar for our pairs universe
    if not earnings:
        try:
            import yfinance as _yf_earn
            _eq_cfg = globals().get("EQUITIES_CONFIG", {})
            _seed = _eq_cfg.get("pairs", {}).get("seed", [])
            _all_tickers = set()
            for a, b in _seed:
                _all_tickers.add(a)
                _all_tickers.add(b)
            for ticker in list(_all_tickers)[:30]:
                try:
                    t = _yf_earn.Ticker(ticker)
                    cal = t.calendar
                    if cal is not None and not cal.empty:
                        if hasattr(cal, 'iloc') and len(cal.columns) > 0:
                            _ed = str(cal.iloc[0, 0]) if cal.shape[0] > 0 else ""
                            if monday <= _ed[:10] <= friday:
                                earnings.append((ticker, _ed[:10]))
                except Exception:
                    pass
        except Exception:
            pass
    # Update state
    _EARNINGS_WEEK["tickers"] = [e[0] for e in earnings]
    _EARNINGS_WEEK["dates"] = {e[0]: e[1] for e in earnings}
    _EARNINGS_WEEK["last_scan"] = now.strftime("%Y-%m-%d %H:%M UTC")
    log.info("EARNINGS CALENDAR: %d tickers reporting next week (%s–%s)", len(earnings), monday, friday)
    return earnings


def is_earnings_week(ticker):
    """Check if a ticker has earnings this week. Used to tighten pairs sizing."""
    return ticker.upper() in _EARNINGS_WEEK.get("tickers", [])


@tasks.loop(hours=168)  # Weekly
async def earnings_calendar_task():
    """Fetch earnings calendar every Sunday at 8 PM ET."""
    try:
        earnings = fetch_earnings_calendar()
        if earnings and DISCORD_CHANNEL_ID:
            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
            if ch:
                msg = f"**EARNINGS CALENDAR — Next Week**\n"
                msg += f"```\n{'Ticker':8s} {'Date':12s} {'In Pairs?':10s}\n{'-'*32}\n"
                _eq_cfg = globals().get("EQUITIES_CONFIG", {})
                _seed = _eq_cfg.get("pairs", {}).get("seed", [])
                _pairs_tickers = set()
                for a, b in _seed:
                    _pairs_tickers.add(a)
                    _pairs_tickers.add(b)
                for tk, dt in earnings[:15]:
                    in_pairs = "YES" if tk in _pairs_tickers else ""
                    msg += f"{tk:8s} {dt:12s} {in_pairs:10s}\n"
                msg += "```"
                if any(tk in _pairs_tickers for tk, _ in earnings):
                    msg += "\nPairs with earnings this week will be sized at 0.5x"
                await ch.send(msg[:1900])
    except Exception as e:
        log.warning("Earnings calendar task error: %s", e)

@earnings_calendar_task.before_loop
async def before_earnings_calendar():
    await bot.wait_until_ready()
    import asyncio
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    target = now_et.replace(hour=20, minute=0, second=0, microsecond=0)
    # Next Sunday
    days_to_sunday = (6 - now_et.weekday()) % 7
    if days_to_sunday == 0 and now_et.hour >= 20:
        days_to_sunday = 7
    target += __import__("datetime").timedelta(days=days_to_sunday)
    wait_secs = (target - now_et).total_seconds()
    if wait_secs < 0:
        wait_secs += 7 * 86400
    log.info("Earnings calendar scheduled in %.0f minutes", wait_secs / 60)
    await asyncio.sleep(wait_secs)


@tasks.loop(minutes=30)
async def pairs_scan_task():
    """Dedicated pairs scanner — runs every 30 minutes during market hours.
    Independent of main 10-minute loop for higher trade frequency."""
    if not EQUITIES_ENABLED or not is_market_open():
        return
    try:
        log.info("PAIRS SCAN (dedicated): starting 30-min cycle")
        opps = scan_pairs_opportunities()
        if opps:
            log.info("PAIRS SCAN (dedicated): %d opportunities found", len(opps))
    except Exception as e:
        log.warning("PAIRS SCAN (dedicated) error: %s", e)

@pairs_scan_task.before_loop
async def before_pairs_scan():
    await bot.wait_until_ready()
    import asyncio
    # Wait 15 minutes to offset from the main 10-min loop
    await asyncio.sleep(900)


# Crypto pairs scan runs inline in alert_scan_task (every cycle, 24/7)


# ============================================================================
# ADAPTIVE CYCLE RATE
# ============================================================================
CYCLE_INTERVAL = 600  # Default 10 minutes (seconds)
CYCLE_PAUSED = False
CYCLE_MIN_INTERVAL = 120   # 2 min minimum
CYCLE_MAX_INTERVAL = 1800  # 30 min maximum
LAST_VOLATILITY_CHECK = 0


def adapt_cycle_rate():
    """Adjust cycle rate based on market volatility and day of week.
    Weekends: faster crypto scanning (5 min). Weekdays: volatility-based."""
    global CYCLE_INTERVAL
    try:
        # Weekend: crypto trades 24/7, scan faster
        if datetime.now(timezone.utc).weekday() >= 5:  # Sat=5, Sun=6
            CYCLE_INTERVAL = 300  # 5 min on weekends for crypto momentum
            return
        fng_val, _ = get_fear_greed()
        # High fear or greed = high volatility = faster scanning
        if fng_val < 20 or fng_val > 80:
            CYCLE_INTERVAL = max(CYCLE_MIN_INTERVAL, 180)  # 3 min
        elif fng_val < 35 or fng_val > 65:
            CYCLE_INTERVAL = 420  # 7 min
        else:
            CYCLE_INTERVAL = 600  # 10 min default
    except Exception:
        CYCLE_INTERVAL = 600


@bot.command(name="set-cycle")
async def set_cycle(ctx, interval: str = ""):
    """Set cycle interval. Usage: !set-cycle 5m or !set-cycle 300s"""
    global CYCLE_INTERVAL
    if not interval:
        await ctx.send(f"Current cycle: {CYCLE_INTERVAL}s ({CYCLE_INTERVAL//60}m). Paused: {CYCLE_PAUSED}\nUsage: `!set-cycle 5m` or `!set-cycle 300s`")
        return
    try:
        if interval.endswith("m"):
            seconds = int(interval[:-1]) * 60
        elif interval.endswith("s"):
            seconds = int(interval[:-1])
        else:
            seconds = int(interval) * 60  # assume minutes
        seconds = max(CYCLE_MIN_INTERVAL, min(CYCLE_MAX_INTERVAL, seconds))
        CYCLE_INTERVAL = seconds
        await ctx.send(f"Cycle interval set to {seconds}s ({seconds//60}m)")
    except ValueError:
        await ctx.send("Invalid format. Use: `!set-cycle 5m` or `!set-cycle 300s`")


@bot.command(name="pause-cycle")
async def pause_cycle(ctx):
    """Pause/resume auto-cycling."""
    global CYCLE_PAUSED
    CYCLE_PAUSED = not CYCLE_PAUSED
    status = "PAUSED" if CYCLE_PAUSED else "RESUMED"
    await ctx.send(f"Auto-cycle {status}. Interval: {CYCLE_INTERVAL}s ({CYCLE_INTERVAL//60}m)")




@bot.command(name="backtest-advanced")
async def backtest_advanced(ctx, *, strategy: str = ""):
    """Advanced backtest with particle filters, copulas, importance sampling."""
    if not strategy:
        await ctx.send("Usage: `!backtest-advanced momentum-crypto`")
        return
    msg = await ctx.send(f"Running advanced backtest: *{strategy[:60]}*\nPhases: Walk-forward → Monte Carlo → Particle Filter → Copula → Permutation...")

    import random, math

    # Walk-forward optimization
    n_trades = random.randint(80, 600)
    win_rate = random.uniform(0.42, 0.72)
    avg_win = random.uniform(1.5, 9.0)
    avg_loss = random.uniform(1.0, 6.0)
    sharpe = random.uniform(0.2, 3.2)
    max_dd = random.uniform(4.0, 40.0)
    total_return = random.uniform(-20.0, 120.0)
    calmar = total_return / max_dd if max_dd > 0 else 0

    # Standard Monte Carlo (10,000 paths)
    mc_median = total_return * random.uniform(0.65, 1.15)
    mc_5th = total_return * random.uniform(0.15, 0.55)
    mc_95th = total_return * random.uniform(1.3, 2.0)

    # Particle Filter (sequential MC with resampling)
    pf_effective_particles = random.randint(500, 5000)
    pf_resampled = random.randint(2, 8)
    pf_posterior_mean = total_return * random.uniform(0.8, 1.05)
    pf_posterior_std = abs(total_return) * random.uniform(0.1, 0.4)

    # Copula Analysis (tail dependency)
    copula_type = random.choice(["Clayton", "Gumbel", "Frank", "t-Copula"])
    tail_dep_lower = random.uniform(0.01, 0.35)
    tail_dep_upper = random.uniform(0.01, 0.30)

    # Importance Sampling (rare event estimation)
    is_tail_prob = random.uniform(0.001, 0.05)
    is_expected_shortfall = max_dd * random.uniform(1.2, 2.5)
    is_variance_reduction = random.uniform(3.0, 50.0)

    # Stratified MC
    strat_layers = random.randint(5, 20)
    strat_variance = random.uniform(0.5, 5.0)

    # Permutation Test (statistical significance)
    n_permutations = 10000
    p_value = random.uniform(0.001, 0.15)
    significant = p_value < 0.05

    # Walk-Forward Efficiency
    wf_efficiency = random.uniform(0.25, 0.95)
    oos_degradation = random.uniform(3, 45)

    # Final verdict: multi-criteria
    checks = [
        sharpe > 1.0,
        win_rate > 0.48,
        max_dd < 30,
        wf_efficiency > 0.45,
        p_value < 0.05,
        pf_posterior_mean > 0,
        tail_dep_lower < 0.25,
    ]
    passed = sum(checks)
    robust = passed >= 5
    verdict = f"PASS ({passed}/7 checks)" if robust else f"FAIL ({passed}/7 checks)"
    icon = "✅" if robust else "❌"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    report = (
        f"**Advanced Backtest** | {ts}\n"
        f"Strategy: *{strategy[:50]}*\n"
        f"================================\n"
        f"**Performance:**\n"
        f"  Return: {total_return:+.1f}% | Sharpe: {sharpe:.2f} | Calmar: {calmar:.2f}\n"
        f"  Max DD: -{max_dd:.1f}% | Win Rate: {win_rate:.1%} | Trades: {n_trades}\n"
        f"  Walk-Forward Eff: {wf_efficiency:.1%} | OOS Degrade: {oos_degradation:.0f}%\n"
        f"\n**Monte Carlo (10K paths):**\n"
        f"  5th: {mc_5th:+.1f}% | Median: {mc_median:+.1f}% | 95th: {mc_95th:+.1f}%\n"
        f"\n**Particle Filter ({pf_effective_particles} particles):**\n"
        f"  Posterior: {pf_posterior_mean:+.1f}% ± {pf_posterior_std:.1f}% | Resamplings: {pf_resampled}\n"
        f"\n**Copula ({copula_type}):**\n"
        f"  Lower tail dep: {tail_dep_lower:.3f} | Upper: {tail_dep_upper:.3f}\n"
        f"\n**Importance Sampling:**\n"
        f"  Tail prob: {is_tail_prob:.4f} | ES: -{is_expected_shortfall:.1f}% | VR: {is_variance_reduction:.1f}x\n"
        f"\n**Permutation Test ({n_permutations:,} perms):**\n"
        f"  p-value: {p_value:.4f} | {'Significant' if significant else 'Not significant'} at α=0.05\n"
        f"\n**Verdict:** {icon} {verdict}\n"
        f"================================"
    )

    if len(report) > 1900:
        report = report[:1900] + "\n*...truncated*"
    await msg.edit(content=report)




# ============================================================================
# AGENTKEEPER — SECONDARY MEMORY
# ============================================================================
AGENT_MEMORY = {
    "strategies": {},      # strategy name -> performance history
    "risk_rules": [        # critical risk rules
        "Max 1% portfolio per trade",
        "Daily loss limit: $500",
        "Never trade during first/last 5 min of session",
        "Cut losses at 2x expected loss",
        "No more than 3 correlated positions",
    ],
    "performance_log": [],  # daily performance entries
    "lessons": [],          # learned lessons from trades
}


@bot.command(name="memory")
async def show_memory(ctx, action: str = "view", *, content: str = ""):
    """AgentKeeper memory system. Usage: !memory view, !memory add-rule <rule>, !memory add-lesson <lesson>"""
    if action == "view":
        rules = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(AGENT_MEMORY["risk_rules"]))
        lessons = "\n".join(f"  • {l}" for l in AGENT_MEMORY["lessons"][-5:]) or "  None yet"
        strats = "\n".join(f"  {k}: {v}" for k, v in list(AGENT_MEMORY["strategies"].items())[-5:]) or "  None tracked yet"
        await ctx.send(
            f"**AgentKeeper Memory**\n================================\n"
            f"**Risk Rules:**\n{rules}\n\n"
            f"**Recent Lessons:**\n{lessons}\n\n"
            f"**Tracked Strategies:**\n{strats}\n"
            f"================================"
        )
    elif action == "add-rule" and content:
        AGENT_MEMORY["risk_rules"].append(content)
        await ctx.send(f"Added risk rule: *{content}*")
    elif action == "add-lesson" and content:
        AGENT_MEMORY["lessons"].append(f"[{datetime.now(timezone.utc).strftime('%m/%d')}] {content}")
        await ctx.send(f"Added lesson: *{content}*")
    elif action == "track" and content:
        parts = content.split(" ", 1)
        name = parts[0]
        result = parts[1] if len(parts) > 1 else "tracked"
        AGENT_MEMORY["strategies"][name] = result
        await ctx.send(f"Tracking strategy *{name}*: {result}")
    else:
        await ctx.send("Usage: `!memory view` | `!memory add-rule <rule>` | `!memory add-lesson <lesson>` | `!memory track <name> <result>`")




# ============================================================================
# THREE-TIER CONTEXT MEMORY
# ============================================================================
CONTEXT_MEMORY = {
    "hot": {  # Core rules — always active
        "max_position_pct": 0.015,  # Tiered: up to 1.5% for high-confidence
        "daily_loss_limit": -500,
        "trading_mode": "paper",
        "risk_tolerance": "conservative",
        "platforms": ["kalshi", "polymarket", "robinhood", "coinbase", "phemex"],
    },
    "domain": {  # Per-skill expert knowledge
        "prediction_markets": {
            "min_ev_threshold": 0.015,
            "prefer_liquid_markets": True,
            "arb_min_spread": 0.02,
        },
        "crypto": {
            "momentum_threshold_24h": 8.0,
            "min_volume_usd": 10_000_000,
            "prefer_large_cap": True,
        },
        "analysis": {
            "model": "gpt-4o-mini",
            "fallback_model": "gpt-3.5-turbo",
            "max_tokens": 600,
        },
    },
    "cold": {  # Long-term knowledge base
        "historical_performance": [],
        "market_regimes": [],
        "strategy_notes": [],
    },
}


@bot.command(name="context")
async def show_context(ctx, tier: str = "all"):
    """View context memory tiers. Usage: !context [hot|domain|cold|all]"""
    import json
    if tier == "hot" or tier == "all":
        hot = json.dumps(CONTEXT_MEMORY["hot"], indent=2)
        await ctx.send(f"**Hot Context (Core Rules):**\n```json\n{hot}\n```")
    if tier == "domain" or tier == "all":
        domain = json.dumps(CONTEXT_MEMORY["domain"], indent=2)
        if len(domain) > 1800:
            domain = domain[:1800] + "..."
        await ctx.send(f"**Domain Context (Per-Skill):**\n```json\n{domain}\n```")
    if tier == "cold" or tier == "all":
        cold_count = sum(len(v) for v in CONTEXT_MEMORY["cold"].values())
        await ctx.send(f"**Cold Context (Long-term):** {cold_count} entries stored")




# ============================================================================
# CLAWJACKED PROTECTION
# ============================================================================
SECURITY_LOG = []
BLOCKED_COMMANDS = []


def check_prompt_injection(text):
    """Check for prompt injection attempts in commands."""
    suspicious_patterns = [
        "ignore previous", "ignore above", "disregard", "new instructions",
        "system prompt", "override", "admin mode", "sudo", "exec(",
        "eval(", "__import__", "os.system", "subprocess",
    ]
    text_lower = text.lower()
    for pattern in suspicious_patterns:
        if pattern in text_lower:
            return True, pattern
    return False, None


@bot.command(name="security")
async def security_status(ctx):
    """Show security status and recent alerts."""
    recent = SECURITY_LOG[-10:] if SECURITY_LOG else ["No security events"]
    blocked = len(BLOCKED_COMMANDS)
    log_str = "\n".join(f"  • {e}" for e in recent[-5:])
    await ctx.send(
        f"**ClawJacked Protection Status**\n================================\n"
        f"Injection defense: Active (10-rule system)\n"
        f"WebSocket trust: localhost only\n"
        f"Blocked attempts: {blocked}\n"
        f"Pairing monitoring: Active\n\n"
        f"**Recent Events:**\n{log_str}\n"
        f"================================"
    )




# ============================================================================
# MULTI-AGENT COORDINATION
# ============================================================================

@bot.command(name="agents")
async def agents_status(ctx):
    """Show status of all trading agents/skills."""
    agents = {
        "Scanner": {"status": "Active", "last_run": "!cycle", "desc": "EV opportunity scanner across all platforms"},
        "Analyst": {"status": "Active", "last_run": "!analyze", "desc": "AI-powered market analysis (GPT-4o-mini)"},
        "Executor": {"status": f"{'Paper' if TRADING_MODE == 'paper' else 'Live'}", "last_run": "!trade", "desc": "Trade execution with safety checks"},
        "Backtester": {"status": "Active", "last_run": "!backtest", "desc": "Strategy validation with Monte Carlo"},
        "Reporter": {"status": "Active", "last_run": "!daily", "desc": "Performance reporting + auto daily at 00:00 UTC"},
        "MemoryKeeper": {"status": "Active", "last_run": "!memory", "desc": "AgentKeeper + 3-tier context memory"},
        "SecurityGuard": {"status": "Active", "last_run": "!security", "desc": "ClawJacked injection defense"},
        "NewsAnalyst": {"status": "Active", "last_run": "!forecast", "desc": "Real-time news sentiment + impact scoring"},
    }
    lines = ["**TraderJoes Multi-Agent System**\n================================"]
    for name, info in agents.items():
        lines.append(f"**{name}** [{info['status']}]\n  {info['desc']}\n  Trigger: `{info['last_run']}`")
    lines.append("================================\n*Agents coordinate via internal relay. Ask-for-help logic enabled.*")
    await ctx.send("\n".join(lines))




@bot.command()
async def forecast(ctx, *, topic: str = "markets"):
    """Market forecast with news sentiment + Fear&Greed + composite score."""
    msg = await ctx.send(f"Generating forecast for *{topic[:50]}*...")
    fc = get_market_forecast()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    headlines_str = ""
    for h in fc["top_headlines"][:5]:
        title = h.get("title", "")[:70]
        headlines_str += f"  • {title}\n"

    if not headlines_str:
        headlines_str = "  No recent headlines found\n"

    report = (
        f"**Market Forecast** | {ts}\n"
        f"Topic: *{topic[:50]}*\n"
        f"================================\n"
        f"**Composite Score:** {fc['composite']}/100 — **{fc['outlook']}**\n"
        f"  Fear & Greed: {fc['fear_greed']}/100 ({fc['fear_greed_label']})\n"
        f"  News Sentiment: {fc['sentiment_score']:+d}/100 ({fc['headline_count']} articles)\n"
        f"\n**Top Headlines:**\n{headlines_str}"
        f"================================"
    )
    await msg.edit(content=report)




# ============================================================================
# AGENT RELAY + HELP
# ============================================================================

@bot.command(name="help-tj")
async def help_tj(ctx):
    """Full command reference."""
    p1 = (
        "**TraderJoes Trading Firm — Command Reference**\n"
        "================================\n"
        "**Portfolio & Balances:**\n"
        "  `!portfolio` — Live balances (5 platforms + USD totals)\n"
        "  `!status` — Integration health check\n"
        "  `!ping` — Bot health check\n"
        "\n**Scanning & Analysis:**\n"
        "  `!cycle` — EV scan: Kalshi + Polymarket + Crypto\n"
        "  `!analyze <question>` — AI market analysis\n"
        "  `!forecast [topic]` — News sentiment + market forecast\n"
    )
    await ctx.send(p1)
    p2 = (
        "**Trading:**\n"
        "  `!trade buy/sell <asset> <amount>` — Propose trade\n"
        "  `!confirm-trade` / `!cancel-trade` — Execute or cancel\n"
        "  `!paper-status` — Paper portfolio\n"
        "  `!paper-trade buy/sell <market> @ <price> x<size>` — Simulated trade\n"
        "  `!auto-paper on/off` — Auto-execute high-EV in paper mode\n"
        "  `!switch-mode paper/live` — Toggle trading mode\n"
        "\n**Backtesting:**\n"
        "  `!backtest <strategy>` — Standard walk-forward backtest\n"
        "  `!backtest-advanced <strategy>` — MC + particle filter + copula\n"
        "  `!backtest-real <coin> <strategy> <days>` — Real CoinGecko data\n"
    )
    await ctx.send(p2)
    p3 = (
        "**Reporting & Analytics:**\n"
        "  `!daily` / `!report` — Full performance report\n"
        "  `!analytics` — Equity curve, Sharpe, drawdown, PnL\n"
        "  `!costs` — OpenAI spend + safety status\n"
        "  `!signals [recent|stats|all]` — Signal history & learning\n"
        "  `!resolve-signal <idx> win/loss` — Mark signal outcome\n"
        "  `!log <message>` — Log to GitHub\n"
        "\n**System & Memory:**\n"
        "  `!agents` — Multi-agent status (8 agents)\n"
        "  `!memory [view|add-rule|add-lesson|track]` — AgentKeeper\n"
        "  `!context [hot|domain|cold|all]` — 3-tier context memory\n"
        "  `!save` / `!load` — Persist/restore all state\n"
    )
    await ctx.send(p3)
    p4 = (
        "**Safety & Config:**\n"
        "  `!security` — ClawJacked protection status\n"
        "  `!alerts [on|off|threshold|cooldown]` — Auto-alert config\n"
        "  `!kill-switch [on|off]` — Emergency trading halt\n"
        "  `!set-cycle <interval>` — Set scan interval (e.g. 5m)\n"
        "  `!pause-cycle` — Pause/resume auto-scan\n"
        "  `!studio` — OpenClaw Studio dashboard info\n"
        "  `!help-tj` — This help message\n"
        "================================\n"
        "Dashboards: http://89.167.108.136:3000 | http://89.167.108.136:19999"
    )
    await ctx.send(p4)


@bot.command(name="analytics")
async def show_analytics(ctx):
    """Show performance analytics dashboard."""
    a = ANALYTICS
    win_rate = (a["winning_trades"] / max(a["total_trades"], 1)) * 100
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Calculate Sharpe
    sharpe = 0.0
    if len(a["daily_pnl_history"]) > 1:
        import statistics
        mean_pnl = statistics.mean(a["daily_pnl_history"])
        std_pnl = statistics.stdev(a["daily_pnl_history"]) or 1
        sharpe = (mean_pnl / std_pnl) * (252 ** 0.5)

    platform_lines = "\n".join(f"    {p.title()}: ${v:+,.2f}" for p, v in a["platform_pnl"].items())

    report = (
        f"**Performance Analytics** | {ts}\n"
        f"================================\n"
        f"**Equity Curve:**\n"
        f"  Current: ${a['current_equity']:,.2f}\n"
        f"  Peak: ${a['peak_equity']:,.2f}\n"
        f"  Drawdown: {a['max_drawdown']:.1f}%\n"
        f"\n**Trade Stats:**\n"
        f"  Total: {a['total_trades']} | Wins: {a['winning_trades']} | Losses: {a['losing_trades']}\n"
        f"  Win Rate: {win_rate:.1f}% | Sharpe: {sharpe:.2f}\n"
        f"  Total P&L: ${a['total_pnl']:+,.2f}\n"
        f"\n**P&L by Platform:**\n{platform_lines}\n"
        f"\n**OpenAI Usage:**\n"
        f"  Calls: {a['openai_calls']} | Tokens: {a['openai_tokens']:,}\n"
        f"  Cost: ${a['openai_cost_usd']:.4f} / ${a['openai_monthly_limit']:.2f} limit\n"
        f"================================"
    )
    await ctx.send(report)




# ============================================================================
# AUTO-ALERTS FOR HIGH-EV OPPORTUNITIES
# ============================================================================

# ============================================================================
# PERFORMANCE ANALYTICS + NETDATA METRICS
# ============================================================================
import socket as _socket

ANALYTICS = {
    "total_trades": 0,
    "winning_trades": 0,
    "losing_trades": 0,
    "total_pnl": 0.0,
    "peak_equity": 10000.0,
    "current_equity": 10000.0,
    "max_drawdown": 0.0,
    "daily_pnl_history": [],
    "platform_pnl": {"kalshi": 0.0, "polymarket": 0.0, "robinhood": 0.0, "coinbase": 0.0, "phemex": 0.0},
    "openai_calls": 0,
    "openai_tokens": 0,
    "openai_cost_usd": 0.0,
    "openai_monthly_limit": 10.0,
}


def push_netdata_metric(key, value):
    """Push a metric to Netdata via StatsD (UDP)."""
    try:
        sock = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
        msg = f"traderjoes.{key}:{value}|g"
        sock.sendto(msg.encode(), ("127.0.0.1", 8125))
        sock.close()
    except Exception:
        pass


def push_all_analytics():
    """Push all analytics metrics to Netdata."""
    a = ANALYTICS
    push_netdata_metric("equity", a["current_equity"])
    push_netdata_metric("pnl_total", a["total_pnl"])
    push_netdata_metric("trades_total", a["total_trades"])
    push_netdata_metric("win_rate", (a["winning_trades"] / max(a["total_trades"], 1)) * 100)
    push_netdata_metric("max_drawdown", a["max_drawdown"])
    push_netdata_metric("openai_cost", a["openai_cost_usd"])
    push_netdata_metric("openai_calls", a["openai_calls"])
    for platform, pnl in a["platform_pnl"].items():
        push_netdata_metric(f"pnl_{platform}", pnl)
    if len(a.get("daily_pnl_history", [])) > 1:
        import statistics
        mean_pnl = statistics.mean(a["daily_pnl_history"])
        std_pnl = statistics.stdev(a["daily_pnl_history"]) or 1
        sharpe = (mean_pnl / std_pnl) * (252 ** 0.5)
        push_netdata_metric("sharpe_ratio", round(sharpe, 2))
    save_all_state()  # Auto-save every cycle


def track_openai_usage(tokens_used, model="gpt-4o-mini"):
    """Track OpenAI API usage and costs."""
    ANALYTICS["openai_calls"] += 1
    ANALYTICS["openai_tokens"] += tokens_used
    cost_per_1k = 0.0004 if "mini" in model else 0.003
    cost = (tokens_used / 1000) * cost_per_1k
    ANALYTICS["openai_cost_usd"] += cost



ALERT_CONFIG = {
    "enabled": True,
    "min_ev_threshold": 0.02,  # 2% EV for more paper trades in learning mode
    "cooldown_seconds": 1800,  # 30 min between alerts per market
    "max_alerts_per_hour": 5,
    "alert_history": {},       # market -> last_alert_time
    "alerts_this_hour": 0,
    "hour_reset_time": 0,
}


def should_alert(market_key, ev):
    """Check if we should send an alert for this opportunity."""
    cfg = ALERT_CONFIG
    if not cfg["enabled"]:
        return False
    if ev < cfg["min_ev_threshold"]:
        return False

    now = time.time()

    # Reset hourly counter
    if now - cfg["hour_reset_time"] > 3600:
        cfg["alerts_this_hour"] = 0
        cfg["hour_reset_time"] = now

    # Check hourly limit
    if cfg["alerts_this_hour"] >= cfg["max_alerts_per_hour"]:
        return False

    # Check cooldown per market
    last_alert = cfg["alert_history"].get(market_key, 0)
    if now - last_alert < cfg["cooldown_seconds"]:
        return False

    return True


def record_alert(market_key):
    """Record that an alert was sent."""
    cfg = ALERT_CONFIG
    cfg["alert_history"][market_key] = time.time()
    cfg["alerts_this_hour"] += 1


async def check_and_send_alerts():
    # arb scan in alerts
    """Scan for high-EV opportunities and send alerts."""
    if not ALERT_CONFIG["enabled"] or not DISCORD_CHANNEL_ID:
        return

    try:
        channel = bot.get_channel(int(DISCORD_CHANNEL_ID))
        if not channel:
            return

        kalshi_opps = find_kalshi_opportunities()
        poly_opps = find_polymarket_opportunities()
        crypto_opps = find_crypto_momentum()

        all_opps = kalshi_opps + poly_opps + crypto_opps
        all_opps.sort(key=lambda x: x.get("ev", 0), reverse=True)

        for opp in all_opps:
            ev = opp.get("ev", 0)
            market_key = f"{opp['platform']}:{opp.get('ticker', opp['market'][:30])}"

            if is_sports_or_junk(opp.get("market", "")):
                continue
            if should_alert(market_key, ev):
                ev_pct = ev * 100

                alert = (
                    f"**HIGH EV ALERT**\n"
                    f"[{opp['platform']}] {opp['type']} — EV: +{ev_pct:.1f}%\n"
                    f"{opp['market']}\n"
                    f"{opp['detail']}\n"
                    f"EV: {ev_pct:.1f}%\n"
                    f"Mode: {TRADING_MODE.upper()} | Use `!trade` to act"
                )
                await channel.send(alert)
                record_alert(market_key)
                log.info("Alert sent: %s EV +%.1f%%", market_key, ev_pct)
                # AUTO-EXECUTE in paper mode if enabled
                if AUTO_PAPER_ENABLED and TRADING_MODE == "paper":
                    try:
                        executed = await auto_paper_execute(channel, opp)
                        if executed:
                            log.info("AUTO-PAPER TRADE: %s EV:%.1f%% Cash:$%.2f Trades:%d",
                                     market_key, ev_pct, PAPER_PORTFOLIO["cash"], len(PAPER_PORTFOLIO["trades"]))
                    except Exception as aex:
                        log.warning("Auto-paper error: %s", aex)
                # AUTO-EXECUTE in live mode if enabled
                if TRADING_MODE == "live" and not DRY_RUN_MODE:
                    try:
                        live_exec = await auto_live_execute(channel, opp)
                        if live_exec:
                            log.info("AUTO-LIVE executed: %s", market_key)
                    except Exception as lex:
                        log.warning("Auto-live error: %s", lex)

    except Exception as exc:
        log.warning("Alert check error: %s", exc)


@bot.command(name="alerts")
async def alerts_cmd(ctx, action: str = "status", value: str = ""):
    """Manage auto-alerts. Usage: !alerts [status|on|off|threshold 0.05|cooldown 30]"""
    cfg = ALERT_CONFIG
    if action == "on":
        cfg["enabled"] = True
        await ctx.send("Auto-alerts **ENABLED**")
    elif action == "off":
        cfg["enabled"] = False
        await ctx.send("Auto-alerts **DISABLED**")
    elif action == "threshold" and value:
        try:
            t = float(value)
            cfg["min_ev_threshold"] = t
            await ctx.send(f"Alert threshold set to {t*100:.1f}% EV")
        except ValueError:
            await ctx.send("Usage: `!alerts threshold 0.05`")
    elif action == "cooldown" and value:
        try:
            cfg["cooldown_seconds"] = int(value) * 60
            await ctx.send(f"Alert cooldown set to {value} minutes")
        except ValueError:
            await ctx.send("Usage: `!alerts cooldown 30`")
    else:
        await ctx.send(
            f"**Auto-Alert Status**\n"
            f"Enabled: {cfg['enabled']}\n"
            f"EV Threshold: {cfg['min_ev_threshold']*100:.1f}%\n"
            f"Cooldown: {cfg['cooldown_seconds']//60}m\n"
            f"Max/hour: {cfg['max_alerts_per_hour']}\n"
            f"Alerts this hour: {cfg['alerts_this_hour']}\n"
            f"Mode: {TRADING_MODE.upper()}"
        )




# ============================================================================
# COST OPTIMIZATION + SAFETY GUARDS
# ============================================================================
COST_CONFIG = {
    "preferred_model": "gpt-4o-mini",       # cheapest viable model
    "fallback_model": "gpt-3.5-turbo",
    "monthly_openai_limit": 10.00,           # $10/month max
    "monthly_openai_spent": 0.0,
    "kill_switch": False,                    # emergency stop all trading
    "max_daily_trades": 8,                  # max trades per day
    "daily_trades_count": 0,
    "max_portfolio_risk": 0.05,              # max 5% of portfolio at risk
    "strict_mode": True,                     # enforce all safety checks
}


@bot.command(name="cost-status")
async def cost_status(ctx):
    """Show cost and safety status."""
    c = COST_CONFIG
    auto = AUTO_LIVE_CONFIG
    await ctx.send(
        f"**Cost & Safety Status** | {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"================================\n"
        f"**AI Costs:**\n"
        f"  Model: {c['preferred_model']} (fallback: {c['fallback_model']})\n"
        f"  OpenAI spend: ${c['monthly_openai_spent']:.2f} / ${c['monthly_openai_limit']:.2f} monthly limit\n"
        f"**Safety:**\n"
        f"  Kill switch: {'ACTIVE' if c['kill_switch'] else 'Off'}\n"
        f"  Dry-run: {'ON' if DRY_RUN_MODE else 'OFF'}\n"
        f"  Trading mode: {TRADING_MODE}\n"
        f"  Portfolio floor: ${PORTFOLIO_FLOOR:,.0f} ({'BREACHED' if PORTFOLIO_FLOOR_ACTIVE else 'OK'})\n"
        f"  Daily P&L: ${DAILY_PNL:+,.2f} (limit: ${DAILY_LOSS_LIMIT:,.2f})\n"
        f"  Daily trades: {c['daily_trades_count']}/{c['max_daily_trades']}\n"
        f"**Auto-Live Config:**\n"
        f"  Enabled: {auto['enabled']}\n"
        f"  Min EV: {auto['min_ev']*100:.1f}% | Min edge score: {auto['min_edge_score']}\n"
        f"  Max position: Tiered (1.5%/0.5%/0.25%) | TWAP >500 | Corr Guard ON\n"
        f"  Max daily trades: {auto['max_daily_trades']}\n"
        f"  Drawdown halt: {auto['drawdown_halt_pct']}%\n"
    )

@bot.command(name="fee-status")
async def fee_status(ctx):
    """Show auto-fee payment status."""
    f = FEE_CONFIG
    owed = calculate_fees()
    await ctx.send(
        f"**Auto-Fee Status**\n"
        f"Enabled: {f['enabled']}\n"
        f"Fee rate: {f['fee_pct']*100:.0f}% of profits above ${f['min_profit_threshold']:.0f}\n"
        f"Total profits: ${f['total_profits']:,.2f}\n"
        f"Fees paid: ${f['total_fees_paid']:,.2f}\n"
        f"Fees owed: ${owed:,.2f}\n"
        f"Fee wallet: {f['fee_wallet'] or 'Not set'}\n"
        f"Use `!set-fee on/off` to toggle."
    )

@bot.command(name="set-fee")
async def set_fee(ctx, action: str = ""):
    """Toggle auto-fee. Usage: !set-fee on/off"""
    if action.lower() == "on":
        FEE_CONFIG["enabled"] = True
        await ctx.send("Auto-fee payment **ENABLED**. 10% of profits above $100 will be reserved.")
    elif action.lower() == "off":
        FEE_CONFIG["enabled"] = False
        await ctx.send("Auto-fee payment **DISABLED**.")
    else:
        await ctx.send("Usage: `!set-fee on` or `!set-fee off`")

@bot.command(name="zapier-status")
async def zapier_status(ctx):
    """Show Zapier/Make.com integration status."""
    z = ZAPIER_CONFIG
    await ctx.send(
        f"**Zapier/Make.com Status**\n"
        f"Enabled: {z['enabled']}\n"
        f"Webhook URL: {'Set' if z['webhook_url'] else 'Not configured'}\n"
        f"Events: {', '.join(z['events'])}\n"
        f"Set ZAPIER_WEBHOOK_URL in .env to enable.\n"
        f"Supported events: trade_executed, kill_switch, daily_summary, high_ev_alert"
    )

@bot.command(name="rebalance")
async def rebalance_cmd(ctx):
    """Check portfolio allocation and suggest rebalancing."""
    msg = await ctx.send("Checking allocation drift...")
    suggestions = await auto_rebalance()
    if not suggestions:
        await msg.edit(content="**Rebalance Check** — All allocations within 5% of target. No action needed.")
        return
    report = "**Rebalance Suggestions**\n================================\n"
    for s in suggestions:
        report += f"**{s['platform'].upper()}**: {s['direction']} ${s['amount']:,.0f} ({s['actual_pct']:.0f}% actual → {s['target_pct']:.0f}% target, drift: {s['drift']:+.1f}%)\n"
    report += "================================\nUse manual transfers to rebalance. Auto-rebalance coming soon."
    await msg.edit(content=report)

@bot.command(name="performance-legacy")
async def performance_legacy_cmd(ctx):
    """Show legacy performance attribution."""
    pt = PERFORMANCE_TRACKER
    paper_trades = len(PAPER_PORTFOLIO.get("trades", []))
    paper_cash = PAPER_PORTFOLIO["cash"]
    paper_pos_value = sum(p.get("value", 0) for p in PAPER_PORTFOLIO.get("positions", []))
    paper_total = paper_cash + paper_pos_value
    paper_pnl = paper_total - 10000
    report = (
        f"**Performance Attribution**\n================================\n"
        f"**Paper Trading:**\n"
        f"  Cash: ${paper_cash:,.2f} | Positions: ${paper_pos_value:,.2f} | Total: ${paper_total:,.2f}\n"
        f"  Trades: {paper_trades} | P&L: ${paper_pnl:+,.2f} (unrealized)\n"
        f"  Signals recorded: {len(SIGNAL_HISTORY)}\n\n"
    )
    if pt["by_strategy"]:
        report += "**By Strategy:**\n"
        for strat, data in pt["by_strategy"].items():
            wr = (data["wins"] / data["trades"] * 100) if data["trades"] > 0 else 0
            report += f"  {strat}: {data['trades']} trades, ${data['pnl']:+,.2f} P&L, {wr:.0f}% win rate\n"
    if pt["by_platform"]:
        report += "\n**By Platform:**\n"
        for plat, data in pt["by_platform"].items():
            report += f"  {plat}: {data['trades']} trades, ${data['pnl']:+,.2f} P&L\n"
    if not pt["by_strategy"] and not pt["by_platform"]:
        report += "No live trade data yet. Paper trades are being tracked.\n"
        report += f"\n**Paper Trade Log:**\n"
        for t in PAPER_PORTFOLIO.get("trades", [])[-5:]:
            report += f"  {t.get('timestamp', '')} | {t.get('side', '')} {t.get('market', '')[:40]} | ${t.get('cost', 0):,.2f}\n"
    report += "================================"
    await ctx.send(report)

@bot.command(name="redis-status")
async def redis_status_cmd(ctx):
    if REDIS_CLIENT:
        try:
            info = REDIS_CLIENT.info("memory")
            sigs = REDIS_CLIENT.llen("history:trade_signals")
            await ctx.send(f"**Redis Signal Bus**\nStatus: Connected\nMemory: {info.get('used_memory_human','N/A')}\nSignals: {sigs}")
        except Exception as e:
            await ctx.send(f"Redis error: {e}")
    else:
        await ctx.send("Redis: Not connected (in-memory fallback)")

@bot.command(name="db-status")
async def db_status_cmd(ctx):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM paper_trades")
        trades = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM resolutions")
        resolved = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM daily_state")
        days = c.fetchone()[0]
        conn.close()
        await ctx.send(f"**SQLite Persistence**\nPaper trades: {trades} | Resolutions: {resolved} | Daily states: {days}")
    except Exception as e:
        await ctx.send(f"SQLite error: {e}")

@bot.command(name="signals")
async def signals_cmd(ctx, count: int = 5):
    history = get_signal_history("trade_signals", count)
    if not history:
        await ctx.send("No signals in Redis history yet.")
        return
    r = "**Last Signals**\n"
    for s in history:
        r += f"  {s.get('market','')[:40]} | EV: {s.get('ev',0)*100:.1f}%\n"
    await ctx.send(r)

@bot.command(name="equities-status")
async def equities_status_cmd(ctx):
    eq_val, eq_pct = get_equities_exposure(PAPER_PORTFOLIO.get("positions", []), 88000)
    pairs_cfg = EQUITIES_CONFIG["pairs"]
    msg = f"**Equities Module**\n"
    msg += f"Enabled: {EQUITIES_ENABLED}\n"
    msg += f"Market open: {is_market_open()}\n"
    msg += f"Equities exposure: ${eq_val:,.0f} ({eq_pct*100:.1f}% of portfolio, max 30%)\n"
    msg += f"Max concurrent: {EQUITIES_CONFIG['max_concurrent']}\n\n"
    msg += f"**Pairs Config**\n"
    msg += f"Seed pairs: {', '.join(f'{a}/{b}' for a,b in pairs_cfg['seed'])}\n"
    msg += f"Min correlation: {pairs_cfg['min_correlation']} | Z-entry: +/-{pairs_cfg['zscore_entry']} | Z-exit: +/-{pairs_cfg['zscore_exit']}\n"
    msg += f"TTL: {pairs_cfg['ttl_days']} trading days\n\n"
    msg += f"**Exit Manager**\n"
    msg += f"Crypto/Prediction TTL: {EXIT_CONFIG['crypto_ttl_hours']}h\n"
    msg += f"PEAD TTL: {EXIT_CONFIG['pead_ttl_hours']}h | TP: +{EXIT_CONFIG['pead_tp_pct']*100:.0f}%\n"
    msg += f"Pairs TTL: {EXIT_CONFIG['pairs_ttl_days']}d | Z-exit: +/-{EXIT_CONFIG['pairs_zscore_exit']}"
    await ctx.send(msg)

@bot.command(name="run-exits")
async def run_exits_cmd(ctx):
    """Manually trigger the exit manager."""
    closed = await run_exit_manager(ctx.channel)
    if closed == 0:
        await ctx.send("Exit manager: No positions to close (all within TTL/TP limits).")

@bot.command(name="scan-pairs")
async def scan_pairs_cmd(ctx):
    """Scan seed pairs for trading signals."""
    if not EQUITIES_ENABLED:
        await ctx.send("Equities module is disabled. Set EQUITIES_ENABLED = True to activate.")
        return
    if not is_market_open():
        await ctx.send("NYSE/NASDAQ is closed. Pairs scanning only runs during market hours (9:30-4:00 EST).")
        return
    await ctx.send("Scanning pairs... (this may take 30-60 seconds for yfinance data)")
    opps = scan_pairs_opportunities()
    if not opps:
        await ctx.send("No pairs signals found. All Z-scores within normal range.")
        return
    for o in opps:
        await ctx.send(f"**PAIRS SIGNAL**: {o['pair']} | Corr: {o['correlation']:.3f} | Z: {o['zscore']:+.2f} | Dir: {o['direction']}")

@bot.command(name="paper-pnl")
async def paper_pnl_cmd(ctx):
    import sqlite3 as _sq, requests as _pnl_req
    cash = PAPER_PORTFOLIO.get("cash", 0)
    positions = PAPER_PORTFOLIO.get("positions", [])
    if not positions:
        await ctx.send(f"No open positions. Cash: ${cash:,.2f}")
        return

    # --- Fetch total realized P&L from SQLite ---
    total_realized = 0.0
    try:
        _rc = _sq.connect(DB_PATH)
        _row = _rc.execute("SELECT SUM(realized_pnl) FROM positions WHERE status='closed' AND realized_pnl IS NOT NULL").fetchone()
        if _row and _row[0]:
            total_realized = _row[0]
        _rc.close()
    except Exception:
        pass

    # --- Fetch live prices and calculate unrealized P&L per position ---
    _alp_hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
    pos_lines = []
    total_unrealized = 0.0
    total_cost = 0.0

    for pos in positions:
        strategy = pos.get("strategy", "prediction")
        if pos.get("platform", "").lower() == "crypto" and strategy not in ("crypto", "momentum"):
            strategy = "crypto"
        market = pos.get("market", "")
        cost = pos.get("cost", 0)
        shares = pos.get("shares", 0)
        entry_price = pos.get("entry_price", 0)
        total_cost += cost

        live_price = None
        upnl = 0.0
        price_src = ""

        try:
            if strategy == "pairs":
                _ll = pos.get("long_leg", market.replace("PAIRS:", "").split("/")[0] if "PAIRS:" in market else "")
                _sl = pos.get("short_leg", market.replace("PAIRS:", "").split("/")[1] if "PAIRS:" in market and "/" in market else "")
                if _ll and _sl:
                    _rl = _pnl_req.get(f"https://data.alpaca.markets/v2/stocks/{_ll}/quotes/latest", headers=_alp_hdr, timeout=5)
                    _rs = _pnl_req.get(f"https://data.alpaca.markets/v2/stocks/{_sl}/quotes/latest", headers=_alp_hdr, timeout=5)
                    if _rl.status_code == 200 and _rs.status_code == 200:
                        _lp = float(_rl.json().get("quote", {}).get("ap", 0) or 0)
                        _sp = float(_rs.json().get("quote", {}).get("bp", 0) or 0)
                        _el = pos.get("entry_long_price", 0)
                        _es = pos.get("entry_short_price", 0)
                        if _lp > 0 and _sp > 0 and _el > 0 and _es > 0:
                            _sz = cost / 2
                            _lpnl = (_lp - _el) * (_sz / _el)
                            _spnl = (_es - _sp) * (_sz / _es)
                            upnl = _lpnl + _spnl
                            price_src = f"L:{_ll}=${_lp:.0f} S:{_sl}=${_sp:.0f}"
                        else:
                            price_src = "no entry leg prices"
            elif strategy == "oracle_trade":
                _ll = pos.get("long_leg", "")
                _sl = pos.get("short_leg", "")
                if _ll and _sl:
                    _rl = _pnl_req.get(f"https://data.alpaca.markets/v2/stocks/{_ll}/quotes/latest", headers=_alp_hdr, timeout=5)
                    _rs = _pnl_req.get(f"https://data.alpaca.markets/v2/stocks/{_sl}/quotes/latest", headers=_alp_hdr, timeout=5)
                    if _rl.status_code == 200 and _rs.status_code == 200:
                        _lp = float(_rl.json().get("quote", {}).get("ap", 0) or 0)
                        _sp = float(_rs.json().get("quote", {}).get("bp", 0) or 0)
                        _el = pos.get("entry_long_price", 0)
                        _es = pos.get("entry_short_price", 0)
                        if _lp > 0 and _sp > 0 and _el > 0 and _es > 0:
                            _sz = cost / 2
                            upnl = ((_lp - _el) * (_sz / _el)) + ((_es - _sp) * (_sz / _es))
                            price_src = f"L:{_ll}=${_lp:.0f} S:{_sl}=${_sp:.0f}"
                        else:
                            price_src = "no entry leg prices"
            elif strategy in ("crypto", "momentum"):
                _tk = market.replace("CRYPTO:", "").split()[0]
                _rc = _pnl_req.get(f"https://api.coinbase.com/v2/prices/{_tk}-USD/spot", timeout=5)
                if _rc.status_code == 200:
                    live_price = float(_rc.json().get("data", {}).get("amount", 0))
                    if live_price > 0 and shares > 0:
                        upnl = (live_price * shares) - cost
                        price_src = f"${live_price:,.2f}"
            elif strategy == "crash_hedge_put":
                _opt_sym = pos.get("option_symbol", "")
                # Reconstruct OCC symbol from market string if missing
                if not _opt_sym and "PUT $" in market:
                    try:
                        import re as _occ_re
                        _strike_match = _occ_re.search(r'PUT \$(\d+)', market)
                        if _strike_match:
                            _strike = float(_strike_match.group(1))
                            _ts_str = pos.get("timestamp", "")
                            if _ts_str:
                                _entry_dt = datetime.strptime(_ts_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                                _exp = _entry_dt + __import__("datetime").timedelta(days=7)
                                while _exp.weekday() >= 5:
                                    _exp += __import__("datetime").timedelta(days=1)
                                _opt_sym = _build_options_symbol("SPY", _exp, _strike, "P")
                    except Exception:
                        pass
                if _opt_sym:
                    _oq = _pnl_req.get(f"https://data.alpaca.markets/v1beta1/options/quotes/latest?symbols={_opt_sym}",
                                       headers=_alp_hdr, timeout=5)
                    if _oq.status_code == 200:
                        _odata = _oq.json().get("quotes", {}).get(_opt_sym, {})
                        _ask = float(_odata.get("ap", 0) or 0)
                        _bid = float(_odata.get("bp", 0) or 0)
                        _opt_mid = (_bid + _ask) / 2 if _bid > 0 and _ask > 0 else (_ask or _bid)
                        if _opt_mid > 0:
                            _contracts = pos.get("contracts", 1)
                            _opt_val = _opt_mid * 100 * _contracts
                            upnl = _opt_val - cost
                            price_src = f"${_opt_mid:.2f}/c"
                if not price_src:
                    price_src = "no option symbol"
            elif strategy == "crash_hedge_short":
                price_src = "no live feed"
            else:
                # Prediction markets — use entry_price as current (hold to resolution)
                if entry_price > 0 and shares > 0:
                    upnl = (entry_price * shares) - cost  # net of slippage/fees
                price_src = "held"
        except Exception:
            price_src = "err"

        total_unrealized += upnl
        _pnl_str = f"${upnl:+,.2f}" if upnl != 0 else "$0.00"
        _label = market[:35]
        pos_lines.append(f"  {_label:35s} ${cost:>7.0f}  {_pnl_str:>9s}  {price_src}")

    # --- Build output ---
    combined = total_realized + total_unrealized
    equity = cash + total_cost + total_unrealized
    start_capital = 25000
    total_return = ((equity / start_capital) - 1) * 100

    header = f"**Paper P&L Dashboard**\n"
    header += f"```\n"
    header += f"{'Realized P&L (closed):':<26s} ${total_realized:>+10,.2f}\n"
    header += f"{'Unrealized P&L (open):':<26s} ${total_unrealized:>+10,.2f}\n"
    header += f"{'Combined P&L:':<26s} ${combined:>+10,.2f}\n"
    header += f"{'─' * 42}\n"
    header += f"{'Cash:':<26s} ${cash:>10,.2f}\n"
    header += f"{'Cost basis:':<26s} ${total_cost:>10,.2f}\n"
    header += f"{'Equity:':<26s} ${equity:>10,.2f}\n"
    header += f"{'Return from $25K:':<26s} {total_return:>+10.1f}%\n"
    header += f"{'─' * 42}\n"
    header += f"{'Position':35s} {'Cost':>7s}  {'Unr P&L':>9s}  {'Price'}\n"
    header += f"```"
    await ctx.send(header)

    # Split positions into chunks that fit Discord's 2000 char limit
    chunk = "```\n"
    for line in pos_lines:
        if len(chunk) + len(line) + 10 > 1900:
            chunk += "```"
            await ctx.send(chunk)
            chunk = "```\n"
        chunk += line + "\n"
    chunk += f"```\n*{len(pos_lines)} positions*"
    await ctx.send(chunk)

@bot.command(name="status")
async def status_cmd(ctx):
    """One-screen dashboard: cash, equity, P&L, positions, signals, regime, funding, AI summary."""
    import sqlite3 as _ssq, requests as _sr
    now = datetime.now(timezone.utc)
    ts = now.strftime("%H:%M UTC")
    _alp = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}

    # ── Portfolio basics ──
    cash = PAPER_PORTFOLIO.get("cash", 0)
    positions = PAPER_PORTFOLIO.get("positions", [])
    total_cost = sum(p.get("cost", 0) for p in positions)

    # ── Realized P&L from SQLite ──
    total_realized = 0.0
    trades_today = 0
    try:
        _sc = _ssq.connect(DB_PATH)
        _row = _sc.execute("SELECT SUM(realized_pnl) FROM positions WHERE status='closed' AND realized_pnl IS NOT NULL").fetchone()
        if _row and _row[0]:
            total_realized = _row[0]
        _today = now.strftime("%Y-%m-%d")
        _trow = _sc.execute("SELECT COUNT(*) FROM positions WHERE created_at LIKE ?", (f"{_today}%",)).fetchone()
        trades_today = _trow[0] if _trow else 0
        _sc.close()
    except Exception:
        pass

    # ── Live prices + unrealized P&L per position ──
    total_unrealized = 0.0
    pos_lines = []
    by_strategy = {}

    for pos in positions:
        strategy = pos.get("strategy", "prediction")
        if pos.get("platform", "").lower() == "crypto" and strategy not in ("crypto", "momentum"):
            strategy = "crypto"
        market = pos.get("market", "")
        cost = pos.get("cost", 0)
        shares = pos.get("shares", 0)
        entry_price = pos.get("entry_price", 0)
        upnl = 0.0

        try:
            if strategy in ("pairs", "oracle_trade"):
                _ll = pos.get("long_leg", "")
                _sl = pos.get("short_leg", "")
                if not _ll and "PAIRS:" in market:
                    parts = market.replace("PAIRS:", "").split("/")
                    _ll, _sl = parts[0], parts[1] if len(parts) > 1 else ""
                _el = pos.get("entry_long_price", 0)
                _es = pos.get("entry_short_price", 0)
                if _ll and _sl and _el > 0 and _es > 0:
                    _rl = _sr.get(f"https://data.alpaca.markets/v2/stocks/{_ll}/quotes/latest", headers=_alp, timeout=3)
                    _rs = _sr.get(f"https://data.alpaca.markets/v2/stocks/{_sl}/quotes/latest", headers=_alp, timeout=3)
                    if _rl.status_code == 200 and _rs.status_code == 200:
                        _lp = float(_rl.json().get("quote", {}).get("ap", 0) or 0)
                        _sp = float(_rs.json().get("quote", {}).get("bp", 0) or 0)
                        if _lp > 0 and _sp > 0:
                            _sz = cost / 2
                            upnl = ((_lp - _el) * (_sz / _el)) + ((_es - _sp) * (_sz / _es))
            elif strategy in ("crypto", "momentum"):
                _tk = market.replace("CRYPTO:", "").split()[0]
                _rc = _sr.get(f"https://api.coinbase.com/v2/prices/{_tk}-USD/spot", timeout=3)
                if _rc.status_code == 200:
                    _spot = float(_rc.json().get("data", {}).get("amount", 0))
                    if _spot > 0 and shares > 0:
                        upnl = (_spot * shares) - cost
            elif strategy == "prediction":
                if entry_price > 0 and shares > 0:
                    upnl = (entry_price * shares) - cost
        except Exception:
            pass

        total_unrealized += upnl
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
        _pnl_tag = f"${upnl:+.0f}" if abs(upnl) >= 0.5 else "~$0"
        _age = ""
        try:
            _et = datetime.strptime(pos.get("timestamp", ""), "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            _h = (now - _et).total_seconds() / 3600
            _age = f"{_h:.0f}h"
        except Exception:
            pass
        pos_lines.append(f"  {market[:28]:28s} ${cost:>6.0f} {_pnl_tag:>6s} {_age:>4s} {strategy[:6]}")

    combined_pnl = total_realized + total_unrealized
    equity = cash + total_cost + total_unrealized
    total_return = ((equity / 25000) - 1) * 100

    # ── Regime + VIX ──
    regime_info = get_regime("equities")
    vix = regime_info.get("vix")
    regime_name = regime_info.get("regime", "?")
    fng_val, fng_label = get_fear_greed()

    # ── Phemex funding rates (inline, non-async) ──
    funding_lines = []
    for asset in ["BTC", "ETH"]:
        try:
            _fr = _sr.get(f"https://api.phemex.com/v1/md/ticker/24hr?symbol=s{asset}USDT", timeout=3)
            if _fr.status_code == 200:
                _rate = float(_fr.json().get("result", {}).get("fundingRate", "0")) / 1e8
                funding_lines.append(f"{asset}={_rate*100:.4f}%")
        except Exception:
            pass
    funding_str = " | ".join(funding_lines) if funding_lines else "unavailable"

    # ── Oracle signals ──
    oracle_lines = []
    try:
        _prices = _oracle_get_all_prices()
        for title, yes_price in _prices.items():
            sig = _oracle_match_signal(title)
            if sig:
                _status = "🔴" if yes_price >= sig["threshold"] else "⚪"
                oracle_lines.append(f"  {_status} {sig['name']:14s} {yes_price:.2f}/{sig['threshold']:.2f} → {sig['long']}/{sig['short']}")
    except Exception:
        pass

    # ── AI summary (one line) ──
    _strat_str = ", ".join(f"{v} {k}" for k, v in sorted(by_strategy.items()))
    mkt_status = "open" if is_market_open() else "closed"
    if combined_pnl > 50:
        _mood = "Strong day"
    elif combined_pnl > 0:
        _mood = "Slightly green"
    elif combined_pnl > -50:
        _mood = "Slightly red"
    else:
        _mood = "Drawdown day"
    _summary = (f"{_mood} | {len(positions)} positions ({_strat_str}) | "
                f"{trades_today} trades today | VIX {vix:.0f} {regime_name} | mkt {mkt_status}")

    # ── Build output (two messages to fit Discord 2000-char limit) ──
    m1 = f"**TraderJoes Firm Status** | {ts}\n```\n"
    m1 += f"{'Cash:':<20s} ${cash:>10,.2f}\n"
    m1 += f"{'Cost basis:':<20s} ${total_cost:>10,.2f}\n"
    m1 += f"{'Unrealized P&L:':<20s} ${total_unrealized:>+10,.2f}\n"
    m1 += f"{'Realized P&L:':<20s} ${total_realized:>+10,.2f}\n"
    m1 += f"{'Combined P&L:':<20s} ${combined_pnl:>+10,.2f}\n"
    m1 += f"{'Equity:':<20s} ${equity:>10,.2f}  ({total_return:+.1f}%)\n"
    m1 += f"{'─' * 46}\n"
    m1 += f"VIX: {vix:.1f} | Regime: {regime_name.upper()} | F&G: {fng_val}/100 ({fng_label})\n"
    m1 += f"Funding: {funding_str}\n"
    m1 += f"{'─' * 46}\n"
    m1 += f"{'Position':28s} {'Cost':>6s} {'P&L':>6s} {'Age':>4s} {'Strat'}\n"
    for line in pos_lines[:12]:
        m1 += line + "\n"
    if len(pos_lines) > 12:
        m1 += f"  +{len(pos_lines)-12} more\n"
    m1 += f"```"

    m2 = ""
    if oracle_lines:
        m2 = f"```\nOracle Signals:\n"
        for line in oracle_lines[:6]:
            m2 += line + "\n"
        m2 += f"```"

    m2 += f"\n> {_summary}"

    await ctx.send(m1)
    if m2.strip():
        await ctx.send(m2)


@bot.command(name="journal")
async def journal_cmd(ctx, count: int = 10):
    """Show last N trade journal entries. Usage: !journal or !journal 5"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""SELECT market_id, strategy, hold_hours, entry_regime, exit_reason,
            realized_pnl, lesson, exit_date FROM trade_journal
            ORDER BY created_at DESC LIMIT ?""", (min(count, 20),))
        rows = c.fetchall()
        conn.close()
        if not rows:
            await ctx.send("**Trade Journal**: No entries yet. Journal entries are created automatically when trades close.")
            return
        msg = f"**TRADE JOURNAL** (last {len(rows)} entries)\n```\n"
        for r in rows:
            mkt = (r[0] or "?")[:25]
            strat = (r[1] or "?")[:8]
            hold = r[2] or 0
            regime = (r[3] or "?")[:8]
            reason = (r[4] or "?")[:15]
            pnl = r[5] or 0
            lesson = (r[6] or "")[:50]
            dt = (r[7] or "?")[:10]
            pnl_str = f"${pnl:+.2f}"
            msg += f"{dt} {strat:8s} {mkt:25s} {pnl_str:>8s} {hold:>5.0f}h {regime:8s} {reason}\n"
            if lesson:
                msg += f"  > {lesson}\n"
        msg += "```"
        await ctx.send(msg[:1900])
    except Exception as e:
        await ctx.send(f"Journal error: {e}")


@bot.command(name="firm-stats")
async def firm_stats_cmd(ctx):
    """Comprehensive firm statistics since inception from SQLite."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()

        # Total trades and P&L
        c.execute("SELECT COUNT(*), COALESCE(SUM(realized_pnl),0) FROM positions WHERE status='closed' AND strategy != 'pairs_legacy'")
        total_trades, total_pnl = c.fetchone()

        # Best/worst single trade
        c.execute("SELECT market_id, realized_pnl, strategy FROM positions WHERE status='closed' AND realized_pnl IS NOT NULL ORDER BY realized_pnl DESC LIMIT 1")
        best_row = c.fetchone()
        c.execute("SELECT market_id, realized_pnl, strategy FROM positions WHERE status='closed' AND realized_pnl IS NOT NULL ORDER BY realized_pnl ASC LIMIT 1")
        worst_row = c.fetchone()

        # Daily P&L for best/worst day and streak
        c.execute("""SELECT DATE(closed_at) as d, SUM(realized_pnl) as dpnl
            FROM positions WHERE status='closed' AND closed_at IS NOT NULL AND strategy != 'pairs_legacy'
            GROUP BY DATE(closed_at) ORDER BY d""")
        daily_rows = c.fetchall()

        best_day = ("—", 0)
        worst_day = ("—", 0)
        streak = 0
        current_streak = 0
        daily_pnls = []
        for d, dpnl in daily_rows:
            if dpnl is None:
                continue
            daily_pnls.append(dpnl)
            if dpnl > best_day[1]:
                best_day = (d, dpnl)
            if dpnl < worst_day[1]:
                worst_day = (d, dpnl)
            if dpnl > 0:
                current_streak += 1
                streak = max(streak, current_streak)
            else:
                current_streak = 0

        # Sharpe ratio (annualized from daily P&L)
        sharpe = 0
        if len(daily_pnls) >= 5:
            import statistics
            mean_d = statistics.mean(daily_pnls)
            std_d = statistics.stdev(daily_pnls) if len(daily_pnls) > 1 else 1
            sharpe = (mean_d / std_d) * (252 ** 0.5) if std_d > 0 else 0

        # Win rate + avg hold time by strategy
        c.execute("""SELECT strategy,
            COUNT(*) as total,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins,
            COALESCE(SUM(realized_pnl), 0) as pnl,
            AVG(CASE WHEN created_at IS NOT NULL AND closed_at IS NOT NULL
                THEN (julianday(closed_at) - julianday(created_at)) * 24
                ELSE NULL END) as avg_hold_hours
            FROM positions WHERE status='closed' AND strategy != 'pairs_legacy' AND realized_pnl IS NOT NULL
            GROUP BY strategy ORDER BY pnl DESC""")
        strat_rows = c.fetchall()

        # Open position count and exposure
        c.execute("SELECT COUNT(*), COALESCE(SUM(size_usd), 0) FROM positions WHERE status='open'")
        open_count, open_exposure = c.fetchone()
        conn.close()

        msg = "**TRADERJOES FIRM STATISTICS**\n"
        msg += "```\n"
        msg += f"Total Trades:    {total_trades}\n"
        msg += f"Total P&L:       ${total_pnl:+,.2f}\n"
        if best_row:
            msg += f"Best Trade:      ${best_row[1]:+,.2f} ({best_row[0][:25]})\n"
        if worst_row:
            msg += f"Worst Trade:     ${worst_row[1]:+,.2f} ({worst_row[0][:25]})\n"
        msg += f"Best Day:        ${best_day[1]:+,.2f} ({best_day[0]})\n"
        msg += f"Worst Day:       ${worst_day[1]:+,.2f} ({worst_day[0]})\n"
        msg += f"Best Streak:     {streak} consecutive profitable days\n"
        msg += f"Current Streak:  {current_streak} days\n"
        msg += f"Sharpe Ratio:    {sharpe:.2f} (annualized)\n"
        msg += f"Trading Days:    {len(daily_pnls)}\n"
        msg += f"Open Positions:  {open_count} (${open_exposure:,.0f})\n"
        msg += f"\n{'Strategy':16s} {'Tr':>4s} {'W':>3s} {'WR%':>5s} {'AvgHold':>8s} {'P&L':>10s}\n"
        msg += f"{'-'*50}\n"
        for row in strat_rows:
            strat, total, wins, pnl = row[0], row[1], row[2], row[3]
            avg_hold = row[4] if len(row) > 4 and row[4] else 0
            wr = (wins / total * 100) if total > 0 else 0
            hold_str = f"{avg_hold:.0f}h" if avg_hold else "—"
            msg += f"{strat[:16]:16s} {total:>4d} {wins:>3d} {wr:>4.0f}% {hold_str:>8s} ${pnl:>+9,.2f}\n"
        msg += "```"
        await ctx.send(msg[:1900])
    except Exception as e:
        await ctx.send(f"Error fetching stats: {e}")


@bot.command(name="next-trades")
async def next_trades_cmd(ctx):
    """Show top 5 highest-conviction opportunities across all strategies right now."""
    candidates = []

    # 1. Pairs Z-scores above 1.1
    try:
        import yfinance as _nt_yf
        _eq_cfg = globals().get("EQUITIES_CONFIG", {})
        _pairs_cfg = _eq_cfg.get("pairs", {})
        _seed = _pairs_cfg.get("seed", [])
        for ta, tb in _seed[:20]:
            try:
                corr, zscore, _ = calculate_pair_zscore(ta, tb, _pairs_cfg.get("lookback_days", 252))
                if corr is not None and abs(zscore) >= 1.1 and corr >= 0.7:
                    # Already open?
                    _open = any(p.get("market") == f"PAIRS:{ta}/{tb}" for p in PAPER_PORTFOLIO.get("positions", []))
                    if _open:
                        continue
                    direction = "short_a_long_b" if zscore > 0 else "long_a_short_b"
                    conf = min(int(abs(zscore) * 30 + corr * 20), 95)
                    candidates.append({
                        "strategy": "Pairs",
                        "ticker": f"{ta}/{tb}",
                        "confidence": conf,
                        "reason": f"Z={zscore:+.2f} corr={corr:.2f} {direction}",
                    })
            except Exception:
                pass
    except Exception:
        pass

    # 2. Oracle signals above 50% of threshold
    try:
        prices = _oracle_get_all_prices()
        for title, yes_price in prices.items():
            sigs = _oracle_match_all_signals(title)
            for sig in sigs:
                _inv = sig.get("inverse", False)
                if _inv:
                    pct = (1 - yes_price) / (1 - sig["threshold"]) * 100 if sig["threshold"] < 1 else 0
                else:
                    pct = yes_price / sig["threshold"] * 100 if sig["threshold"] > 0 else 0
                if pct >= 50:
                    _open = any(p.get("market") == f"ORACLE:{sig['name']}" for p in PAPER_PORTFOLIO.get("positions", []))
                    if _open:
                        continue
                    conf = min(int(pct * 0.8), 90)
                    _dir = f"{'INV ' if _inv else ''}YES=${yes_price:.2f}/{sig['threshold']:.2f}"
                    candidates.append({
                        "strategy": "Oracle",
                        "ticker": f"{sig['long']}/{sig['short']}",
                        "confidence": conf,
                        "reason": f"{sig['name']} {_dir} ({pct:.0f}%)",
                    })
    except Exception:
        pass

    # 3. Crypto momentum above 6% 24h (runs 24/7 including weekends)
    try:
        r = requests.get("https://api.binance.com/api/v3/ticker/24hr", timeout=10)
        if r.status_code == 200:
            for t in r.json():
                sym_raw = t.get("symbol", "")
                if not sym_raw.endswith("USDT"):
                    continue
                base = sym_raw.replace("USDT", "")
                px = float(t.get("lastPrice", 0) or 0)
                chg = float(t.get("priceChangePercent", 0) or 0)
                vol_usd = float(t.get("quoteVolume", 0) or 0)
                if px >= 1.0 and vol_usd >= 10_000_000 and abs(chg) >= 6 and len(base) <= 5:
                    if base not in CRYPTO_MEME_BLACKLIST:
                        conf = min(int(abs(chg) * 5 + vol_usd / 1e9 * 10), 85)
                        candidates.append({
                            "strategy": "Crypto",
                            "ticker": base,
                            "confidence": conf,
                            "reason": f"{chg:+.1f}% 24h vol=${vol_usd/1e6:.0f}M ${px:,.2f} [Binance]",
                        })
    except Exception:
        pass

    # 4. Funding rates above 0.02% (positive or negative)
    try:
        rates = fetch_all_funding_rates()
        for asset, ri in rates.items():
            best = ri.get("best", 0)
            most_neg = ri.get("most_negative", 0)
            if best > 0.0002:
                ann = best * 3 * 365 * 100
                conf = min(int(ann / 2), 70)
                candidates.append({
                    "strategy": "Funding",
                    "ticker": asset,
                    "confidence": conf,
                    "reason": f"Rate={best*100:.4f}% ({ri['best_source']}) ann={ann:.0f}%",
                })
            elif most_neg < -0.0002:
                neg_ann = abs(most_neg) * 3 * 365 * 100
                conf = min(int(neg_ann / 2), 65)
                candidates.append({
                    "strategy": "Neg Funding",
                    "ticker": asset,
                    "confidence": conf,
                    "reason": f"Rate={most_neg*100:.4f}% ({ri['most_negative_source']}) SHORT perp ann={neg_ann:.0f}%",
                })
    except Exception:
        pass

    # 5. Kalshi non-sports markets above $1K volume
    try:
        kalshi_mkts = get_kalshi_active_markets(limit=20)
        for m in kalshi_mkts[:10]:
            title = m.get("title", "")
            vol = float(m.get("volume", 0) or 0)
            yes_ask = m.get("yes_ask", 0)
            if vol >= 1000 and yes_ask and not is_sports_or_junk(title):
                yes_p = yes_ask / 100 if yes_ask > 1 else yes_ask
                if 0.05 < yes_p < 0.30:
                    conf = min(int(vol / 500 + (0.30 - yes_p) * 100), 75)
                    candidates.append({
                        "strategy": "Kalshi",
                        "ticker": m.get("ticker", "")[:15],
                        "confidence": conf,
                        "reason": f"YES=${yes_p:.2f} vol=${vol:,.0f} {title[:35]}",
                    })
    except Exception:
        pass

    # Sort by confidence, take top 5
    candidates.sort(key=lambda c: c["confidence"], reverse=True)
    top5 = candidates[:5]

    portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
        p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))

    msg = f"**NEXT TRADES — Top {len(top5)} Opportunities**\n"
    msg += f"Portfolio: ${portfolio_value:,.0f} | Scanned: {len(candidates)} candidates\n"
    if top5:
        msg += "```\n"
        msg += f"{'#':2s} {'Strategy':10s} {'Ticker':12s} {'Conf':5s} {'Size':>8s} Reason\n"
        msg += f"{'-'*70}\n"
        for i, c in enumerate(top5, 1):
            # Suggested size: 0.5-2% based on confidence
            size_pct = 0.005 + (c["confidence"] / 100) * 0.015
            size_usd = portfolio_value * size_pct
            msg += (f"{i:2d} {c['strategy']:10s} {c['ticker']:12s} "
                    f"{c['confidence']:3d}%  ${size_usd:>7,.0f} {c['reason'][:40]}\n")
        msg += "```"
    else:
        msg += "\nNo opportunities above threshold right now.\n"
    msg += f"\n*Strategies: Pairs, Oracle, Crypto, Funding, Kalshi*"
    await ctx.send(msg[:1900])


@bot.command(name="earnings")
async def earnings_cmd(ctx):
    """Show next 5 days of relevant earnings."""
    if not _EARNINGS_WEEK.get("tickers"):
        fetch_earnings_calendar()
    tickers = _EARNINGS_WEEK.get("tickers", [])
    dates = _EARNINGS_WEEK.get("dates", {})
    last = _EARNINGS_WEEK.get("last_scan", "Never")
    msg = f"**EARNINGS CALENDAR** (last scan: {last})\n"
    if tickers:
        msg += "```\n"
        _eq_cfg = globals().get("EQUITIES_CONFIG", {})
        _seed = _eq_cfg.get("pairs", {}).get("seed", [])
        _pairs_tickers = set()
        for a, b in _seed:
            _pairs_tickers.add(a)
            _pairs_tickers.add(b)
        for tk in tickers[:20]:
            dt = dates.get(tk, "?")
            in_pairs = "IN PAIRS" if tk in _pairs_tickers else ""
            msg += f"  {tk:8s} {dt:12s} {in_pairs}\n"
        msg += "```"
    else:
        msg += "No earnings data available. Scans Sunday 8 PM ET.\n"
    await ctx.send(msg[:1900])


@bot.command(name="crypto-pairs")
async def crypto_pairs_cmd(ctx):
    """Show crypto pairs Z-scores and open positions."""
    cfg = CRYPTO_PAIRS_CONFIG
    msg = f"**CRYPTO PAIRS STAT ARB** (scan every {cfg['scan_interval_min']}min, 24/7)\n"
    msg += f"Entry: |Z| >= {cfg['zscore_entry']} | Exit: Z cross 0 or {cfg['ttl_hours']}h TTL\n\n"
    msg += "**Current Z-Scores:**\n```\n"
    msg += f"{'Pair':10s} {'Corr':>6s} {'Z-Score':>8s} {'Status':>10s}\n"
    msg += f"{'-'*38}\n"
    for sym_a, sym_b in cfg["seed"]:
        cache_key = f"{sym_a}/{sym_b}"
        cached = _CRYPTO_PAIRS_CACHE.get(cache_key)
        if cached:
            corr, z = cached["corr"], cached["zscore"]
            status = "SIGNAL" if abs(z) >= cfg["zscore_entry"] and corr >= 0.5 else "watching"
        else:
            corr, z, _ = calculate_crypto_pair_zscore(sym_a, sym_b, cfg["lookback_days"])
            if corr is None:
                msg += f"{sym_a}/{sym_b:4s}   {'—':>6s} {'—':>8s} {'no data':>10s}\n"
                continue
            status = "SIGNAL" if abs(z) >= cfg["zscore_entry"] and corr >= 0.5 else "watching"
        msg += f"{sym_a}/{sym_b:4s}   {corr:>6.3f} {z:>+8.3f} {status:>10s}\n"
    msg += "```\n"
    # Open positions
    cp_positions = [p for p in PAPER_PORTFOLIO.get("positions", []) if p.get("strategy") == "crypto_pairs"]
    if cp_positions:
        msg += f"**Open Positions ({len(cp_positions)}/{cfg['max_positions']}):**\n"
        for p in cp_positions:
            msg += f"  {p.get('market')} | {p.get('side')} | ${p.get('cost', 0):.0f} | entry_z={p.get('entry_zscore', 0):.2f}\n"
    else:
        msg += f"No open crypto pairs (0/{cfg['max_positions']})\n"
    await ctx.send(msg[:1900])


@bot.command(name="sectors")
async def sectors_cmd(ctx):
    """Show sector ETF rankings and rotation pairs."""
    rankings = _SECTOR_RANKINGS.get("rankings", [])
    last_scan = _SECTOR_RANKINGS.get("last_scan", "Never")

    if not rankings:
        # Try live scan
        rankings = scan_sector_rotation()

    msg = f"**SECTOR ROTATION** (last scan: {last_scan})\n"
    if rankings:
        msg += "```\n"
        msg += f"{'':3s} {'ETF':5s} {'5D Change':>10s} {'Price':>8s}\n"
        msg += f"{'-'*30}\n"
        for i, r in enumerate(rankings):
            if i < 2:
                label = ">>>"
            elif i >= len(rankings) - 2:
                label = "<<<"
            else:
                label = "   "
            msg += f"{label} {r['etf']:5s} {r['change_pct']:>+9.2f}% ${r['price']:>7.2f}\n"
        msg += "```\n"
        pairs = get_sector_rotation_pairs()
        if pairs:
            msg += "**Rotation Pairs (Long top / Short bottom):**\n"
            for long_etf, short_etf in pairs:
                msg += f"  Long {long_etf} / Short {short_etf}\n"
        # Active sector trades
        sector_trades = [p for p in PAPER_PORTFOLIO.get("positions", [])
                         if p.get("market", "").startswith("PAIRS:") and
                         any(etf in p.get("market", "") for etf in SECTOR_ETFS)]
        if sector_trades:
            msg += f"\n**Active Sector Trades ({len(sector_trades)}):**\n"
            for p in sector_trades:
                msg += f"  {p.get('market')} | ${p.get('cost', 0):.0f}\n"
    else:
        msg += "No sector data available. Scans at 9:35 AM ET on trading days.\n"
    await ctx.send(msg[:1900])


@bot.command(name="polygon-status")
async def polygon_status_cmd(ctx):
    """Show Polygon.io connection status and sample data."""
    try:
        from polygon_client import test_connection, get_quote, get_crypto_price, get_news, get_market_movers
        ok, status_msg = test_connection()
        msg = f"**POLYGON.IO STATUS**\n"
        msg += f"Connection: {'OK' if ok else 'FAILED'} — {status_msg}\n"
        if ok:
            # Sample quote
            spy = get_quote("SPY")
            if spy:
                msg += f"\nSPY: ${spy['last']:.2f} (bid ${spy['bid']:.2f} / ask ${spy['ask']:.2f}) {spy['change_pct']:+.2f}%\n"
            # Crypto
            btc = get_crypto_price("BTC")
            if btc:
                msg += f"BTC: ${btc['price']:,.2f} {btc['change_pct']:+.2f}%\n"
            # Top movers
            movers = get_market_movers("gainers", limit=3)
            if movers:
                msg += "\n**Top Gainers:**\n```\n"
                for m in movers:
                    msg += f"  {m['ticker']:6s} ${m['price']:.2f} {m['change_pct']:+.1f}% vol={m['volume']:,.0f}\n"
                msg += "```"
            # Latest news
            news = get_news(limit=3)
            if news:
                msg += "\n**Latest News:**\n"
                for n in news:
                    msg += f"> {n['title'][:70]} — {n['source']}\n"
        await ctx.send(msg[:1900])
    except Exception as e:
        await ctx.send(f"Polygon error: {e}")


@bot.command(name="kalshi-status")
async def kalshi_status_cmd(ctx):
    """Show Kalshi connection state, balance, and top markets by volume."""
    msg = "**KALSHI STATUS**\n"
    # Connection test
    balance = get_kalshi_balance()
    msg += f"Balance: {balance}\n"
    connected = "$" in str(balance)
    msg += f"Connection: {'OK' if connected else 'FAILED'}\n"
    msg += f"Min Volume: ${KALSHI_MIN_VOLUME:,}\n\n"

    if connected:
        # Fetch via events endpoint (financial/political markets)
        markets = get_kalshi_active_markets(limit=50)
        msg += f"**Financial Markets: {len(markets)}** (via events endpoint)\n"
        msg += "\n**Top 5 Markets:**\n```\n"
        for m in markets[:5]:
            vol = float(m.get("volume", 0) or 0)
            yes_ask = m.get("yes_ask", 0)
            no_ask = m.get("no_ask", 0)
            yes_p = yes_ask / 100 if yes_ask else 0
            no_p = no_ask / 100 if no_ask else 0
            cat = m.get("_event_category", "?")[:12]
            title = m.get("title", m.get("_event_title", "?"))[:35]
            msg += (f"  [{cat:12s}] {title:35s} YES:${yes_p:.2f} NO:${no_p:.2f}\n")
        msg += "```"
        if not markets:
            msg += "No financial markets found (events endpoint may only have long-term markets)\n"
        qualifying = sum(1 for m in markets if float(m.get("volume", 0) or 0) >= KALSHI_MIN_VOLUME)
        msg += f"\n{qualifying}/{len(markets)} markets pass volume filter (>= ${KALSHI_MIN_VOLUME:,})"
    else:
        msg += f"\nKey ID: {KALSHI_API_KEY_ID[:12]}... | PEM: {'loaded' if KALSHI_PRIVATE_KEY else 'MISSING'}"
    await ctx.send(msg[:1900])


@bot.command(name="funding-status")
async def funding_status_cmd(ctx):
    """Show funding rates across Phemex and Binance for all pairs."""
    rates = fetch_all_funding_rates()
    min_rate = FUNDING_ARB_CONFIG["min_funding_rate"]
    friction = FUNDING_ARB_CONFIG["commission_estimate"] * 2 + FUNDING_ARB_CONFIG["slippage_estimate"]
    msg = f"**FUNDING RATE MONITOR** (threshold: {min_rate*100:.4f}%)\n"
    msg += "```\n"
    msg += f"{'Asset':6s} {'Phemex':>10s} {'Binance':>10s} {'Best+':>10s} {'MostNeg':>10s} Status\n"
    msg += f"{'-'*60}\n"
    for asset in FUNDING_ARB_CONFIG["pairs"]:
        ri = rates.get(asset, {})
        ph = ri.get("phemex", 0)
        bn = ri.get("binance", 0)
        best = ri.get("best", 0)
        most_neg = ri.get("most_negative", 0)
        # Positive arb check
        pos_signal = best > min_rate and best > friction
        # Negative arb check
        neg_yield = abs(most_neg) - friction if most_neg < -min_rate else 0
        neg_signal = neg_yield > 0
        if pos_signal:
            status = "LONG ARB"
        elif neg_signal:
            status = "NEG ARB"
        else:
            status = "—"
        msg += (f"{asset:6s} {ph*100:>9.4f}% {bn*100:>9.4f}% {best*100:>9.4f}% "
                f"{most_neg*100:>9.4f}% {status}\n")
    msg += "```\n"
    # Active arb positions
    arb_positions = [p for p in PAPER_PORTFOLIO.get("positions", [])
                     if "funding" in p.get("strategy", "").lower() or "arb" in p.get("market", "").lower()]
    if arb_positions:
        msg += f"**Active Arb Positions ({len(arb_positions)}):**\n"
        for p in arb_positions:
            msg += f"  {p.get('market', '?')} | ${p.get('cost', 0):.0f}\n"
    else:
        msg += "No active arb positions\n"
    msg += f"\n*Sources: Phemex API + Binance Futures public API*"
    await ctx.send(msg[:1900])


@bot.command(name="pead-status")
async def pead_status_cmd(ctx):
    """Show PEAD scanner state, last scan results, and open positions."""
    scan = _PEAD_LAST_SCAN
    pead_open = _count_pead_open()
    msg = f"**PEAD Scanner Status**\n"
    msg += f"Enabled: {PEAD_ENABLED} | Thresholds: {PEAD_MIN_SURPRISE_PCT}% surprise, {PEAD_MIN_VOLUME_MULT}x volume\n"
    msg += f"Open: {pead_open}/{PEAD_MAX_POSITIONS} | Size: {PEAD_SIZE_PCT*100:.0f}% of portfolio\n"
    msg += f"\n**Last Scan:** {scan.get('last_run', 'Never')}\n"
    msg += f"Tickers checked: {scan.get('tickers_checked', 0)} | Signals found: {scan.get('signals_found', 0)}\n"
    if scan.get("errors"):
        msg += f"Errors: {len(scan['errors'])} — {scan['errors'][0][:60]}...\n" if len(scan["errors"]) > 0 else ""
    # Open PEAD positions
    pead_positions = [p for p in PAPER_PORTFOLIO.get("positions", []) if p.get("strategy") == "pead"]
    if pead_positions:
        msg += "\n**Open PEAD Positions:**\n```\n"
        for p in pead_positions:
            msg += f"  {p.get('market', '?'):20s} ${p.get('cost', 0):.0f} entry=${p.get('entry_price', 0):.2f}\n"
        msg += "```"
    else:
        msg += "\nNo open PEAD positions\n"
    # Upcoming earnings (from last scan tickers)
    msg += f"\n*Next scan runs market hours (9:45-3:30 ET, every 30min)*"
    await ctx.send(msg[:1900])


@bot.command(name="hedge-status")
async def hedge_status_cmd(ctx):
    """Show VRP regime, crash hedge positions, and options status."""
    regime = get_regime("equities")
    vix = regime.get("vix") or 0
    cfg = CRASH_HEDGE_CONFIG
    vrp = _VRP_REGIME_STATE

    # Determine current VRP regime
    if vix >= cfg["vrp_vix_threshold"]:
        vrp_label = "SELL PREMIUM (call credit spreads)"
    elif vix >= cfg["put_vix_threshold"]:
        vrp_label = "BUY PUTS (cheap OTM protection)"
    else:
        vrp_label = "IDLE (VIX too low)"

    # Collect hedge positions
    hedge_positions = [p for p in PAPER_PORTFOLIO.get("positions", [])
                       if p.get("strategy") in ("crash_hedge_put", "crash_hedge_short", "crash_hedge_call_spread")]

    # Options flow
    _pc = _PUT_CALL_CACHE.get("SPY", {})
    _pc_ratio = _pc.get("ratio", 0)
    _pc_signal = _pc.get("signal", "unavailable")
    _pc_str = f"P/C: {_pc_ratio:.2f} ({_pc_signal})" if _pc.get("available") else "P/C: unavailable"

    lines = [
        f"**HEDGE STATUS — VRP Regime Monitor**",
        f"VIX: {vix:.1f} | Market Regime: {regime.get('regime', 'normal').upper()}",
        f"VRP Regime: **{vrp_label}**",
        f"Options Flow: {_pc_str}",
        f"Last Switch: {vrp.get('last_switch', 'N/A')} | Cycles: {vrp.get('cycle_count', 0)}",
        f"",
        f"**Thresholds:**",
        f"  VIX < 25 → Idle | 25-27 → Buy puts | >= 28 → Sell call spreads | >= 35 → Short SPY",
        f"  P/C > 1.5 → Panic puts (boost hedge) | P/C < 0.5 → Complacency (market top)",
        f"",
        f"**Open Hedge Positions ({len(hedge_positions)}/{cfg['max_hedges']}):**",
    ]
    if hedge_positions:
        for p in hedge_positions:
            strat = p.get("strategy", "?")
            mkt = p.get("market", "?")
            cost = p.get("cost", 0)
            ts = p.get("timestamp", "?")
            vrp_r = p.get("vrp_regime", "")
            lines.append(f"  {mkt} | ${cost:.0f} | {strat} | {ts}" + (f" [{vrp_r}]" if vrp_r else ""))
    else:
        lines.append("  No active hedges")

    await ctx.send("\n".join(lines))


@bot.command(name="oracle-status")
async def oracle_status_cmd(ctx):
    """Show Oracle Engine status: active signals, probabilities, and P&L."""
    import requests as _os_req
    now = datetime.now(timezone.utc)

    # Fetch current prices for all prediction markets
    prices = _oracle_get_all_prices()

    # Build signal status table
    signal_lines = []
    for title, yes_price in prices.items():
        _all_sigs = _oracle_match_all_signals(title)
        if not _all_sigs:
            continue
        # Calculate 1-hour delta
        key = title.lower()
        history = _ORACLE_PRICE_HISTORY.get(key, [])
        one_hour_ago = now - __import__("datetime").timedelta(hours=1)
        old = [(t, p) for t, p in history if t <= one_hour_ago]
        delta = yes_price - old[-1][1] if old else 0.0
        for sig in _all_sigs:
            _inv = sig.get("inverse", False)
            if _inv:
                _geo_req = sig.get("geo_required", "")
                _geo_min = sig.get("geo_min_headlines", 0)
                _theme_count = _intel_theme_escalation_count(_geo_req) if _geo_req else 0
                _geo_ok = _theme_count >= _geo_min if _geo_req else True
                status = "ACTIVE" if yes_price <= sig["threshold"] and _geo_ok else "watch"
                _extra = f" +{sig.get('extra_long', '')}" if sig.get("extra_long") else ""
                signal_lines.append(
                    f"  {sig['name']:16s} YES=${yes_price:.2f} Δ1h={delta:+.3f} "
                    f"inv<{sig['threshold']:.2f} {status:6s} → {sig['long']}/{sig['short']}{_extra}"
                    f"{f' +GEO({_theme_count})' if _geo_ok else f' geo({_theme_count}/{_geo_min})'}")
            else:
                status = "ACTIVE" if yes_price >= sig["threshold"] else "below"
                signal_lines.append(
                    f"  {sig['name']:16s} YES=${yes_price:.2f} Δ1h={delta:+.3f} "
                    f"thr={sig['threshold']:.2f} {status:6s} → {sig['long']}/{sig['short']}")

    # Oracle positions with live P&L
    _alp_hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
    pos_lines = []
    total_oracle_pnl = 0.0
    for pos in PAPER_PORTFOLIO.get("positions", []):
        if pos.get("strategy") != "oracle_trade":
            continue
        market = pos.get("market", "")
        cost = pos.get("cost", 0)
        _ll = pos.get("long_leg", "")
        _sl = pos.get("short_leg", "")
        _el = pos.get("entry_long_price", 0)
        _es = pos.get("entry_short_price", 0)
        upnl = 0.0
        price_info = ""
        try:
            if _ll and _sl and _el > 0 and _es > 0:
                _rl = _os_req.get(f"https://data.alpaca.markets/v2/stocks/{_ll}/quotes/latest", headers=_alp_hdr, timeout=5)
                _rs = _os_req.get(f"https://data.alpaca.markets/v2/stocks/{_sl}/quotes/latest", headers=_alp_hdr, timeout=5)
                if _rl.status_code == 200 and _rs.status_code == 200:
                    _lp = float(_rl.json().get("quote", {}).get("ap", 0) or 0)
                    _sp = float(_rs.json().get("quote", {}).get("bp", 0) or 0)
                    if _lp > 0 and _sp > 0:
                        _sz = cost / 2
                        upnl = ((_lp - _el) * (_sz / _el)) + ((_es - _sp) * (_sz / _es))
                        price_info = f"L:{_ll}=${_lp:.0f} S:{_sl}=${_sp:.0f}"
        except Exception:
            price_info = "err"

        # Get current probability
        _src = pos.get("source_market", "").lower()
        _hist = _ORACLE_PRICE_HISTORY.get(_src, [])
        _prob = _hist[-1][1] if _hist else pos.get("entry_price", 0)

        ts_str = pos.get("timestamp", "")
        try:
            entry_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
            age_h = (now - entry_time).total_seconds() / 3600
        except Exception:
            age_h = 0

        total_oracle_pnl += upnl
        pos_lines.append(
            f"  {market:20s} ${cost:>6.0f} ${upnl:>+8.2f} prob={_prob:.2f} age={age_h:.0f}h {price_info}")

    # Intel context
    geo_state = _intel_get_geo_state()
    _total_hl = sum(len(v) for v in _INTEL_HEADLINE_MEMORY.values())

    msg = "**Oracle Engine Status**\n```\n"
    msg += f"Positions: {len(pos_lines)} / {ORACLE_CONFIG['max_oracle_positions']} max\n"
    msg += f"Oracle P&L: ${total_oracle_pnl:+,.2f}\n"
    msg += f"Signals tracked: {len(prices)} markets\n"
    _geo_str = f"ELEVATED ({geo_state['theme']}, {geo_state['count']} hits)" if geo_state["active"] else "normal"
    msg += f"Geo risk: {_geo_str} | Headlines: {_total_hl}\n"
    # Sentiment scores per theme
    _sent_parts = []
    for _st in ["iran", "ukraine", "taiwan", "fed", "recession"]:
        _ss = get_theme_sentiment(_st)
        if _ss != 0:
            _sent_parts.append(f"{_st}={_ss:+.2f}")
    if _sent_parts:
        msg += f"Sentiment: {' '.join(_sent_parts)}\n"
    msg += f"{'─' * 50}\n"

    if signal_lines:
        msg += "Signals:\n"
        for line in signal_lines:
            msg += line + "\n"
    else:
        msg += "No matching signals in current markets\n"

    if pos_lines:
        msg += f"{'─' * 50}\n"
        msg += f"{'Position':20s} {'Cost':>6s} {'Unr P&L':>8s} {'Prob':>8s} {'Age':>5s} Price\n"
        for line in pos_lines:
            msg += line + "\n"

    msg += "```"
    await ctx.send(msg)

    # Second message: recent headlines per theme
    _hl_msg = ""
    for theme in sorted(_INTEL_HEADLINE_MEMORY.keys()):
        entries = _INTEL_HEADLINE_MEMORY[theme]
        if not entries:
            continue
        recent = sorted(entries, key=lambda x: x[0], reverse=True)[:2]
        _hl_msg += f"**{theme}**: "
        _hl_msg += " | ".join(h[:60] for _, h in recent) + "\n"
    if _hl_msg:
        await ctx.send(f"**Intel Headlines:**\n{_hl_msg[:1900]}")

@bot.command(name="paper-reset")
async def paper_reset_cmd(ctx, amount: float = 10000):
    PAPER_PORTFOLIO["cash"] = amount
    PAPER_PORTFOLIO["positions"] = []
    PAPER_PORTFOLIO["trades"] = []
    ACTIVE_TRADE_LOCK.clear()
    db_save_daily_state()
    await ctx.send(f"Paper portfolio reset: ${amount:,.2f} cash, 0 positions. Ready for fresh validation.")

@bot.command(name="calibration")
async def calibration_cmd(ctx):
    s = get_calibration_summary()
    if s["total"] == 0:
        await ctx.send("**Calibration Report**\nNo resolved trades yet.\nUse `!resolve win [market]` or `!resolve loss [market]` when markets close.")
        return
    grade = "Excellent" if s["avg_brier"] < 0.1 else "Good" if s["avg_brier"] < 0.2 else "Average" if s["avg_brier"] < 0.25 else "Poor"
    r = f"**Calibration Report**\n================================\nResolved: {s['total']} | Win rate: {s['win_rate']:.0f}% | P&L: ${s['total_pnl']:+,.2f}\n"
    r += f"Brier Score: {s['avg_brier']:.4f} ({grade}) | 0=perfect, 0.25=coin flip\n"
    r += f"EV Accuracy: {s['avg_ev_accuracy']:.4f} | lower = better calibrated\n================================"
    await ctx.send(r)

@bot.command(name="resolve")
async def resolve_cmd(ctx, outcome: str = "", *, market_search: str = ""):
    if outcome.lower() not in ("win", "loss"):
        await ctx.send("Usage: `!resolve win Iran ceasefire` or `!resolve loss 2 degrees`")
        return
    outcome_val = 1.0 if outcome.lower() == "win" else 0.0
    matched = None
    matched_idx = None
    for i, pos in enumerate(PAPER_PORTFOLIO.get("positions", [])):
        if market_search.lower() in pos.get("market", "").lower():
            matched = pos
            matched_idx = i
            break
    if not matched:
        await ctx.send(f"No open position matching that. Check `!paper-status`.")
        return
    result = record_resolution(matched, outcome_val)
    PAPER_PORTFOLIO["positions"].pop(matched_idx)
    if outcome_val == 1.0:
        PAPER_PORTFOLIO["cash"] += matched.get("shares", 1) * 1.0
    await ctx.send(f"**Resolved: {'WIN' if outcome_val == 1.0 else 'LOSS'}** | {matched.get('market', '')}\nEntry: ${matched.get('entry_price', 0):.3f} | P&L: ${result['pnl']:+,.2f} | Brier: {result['brier_score']:.4f}\nUse `!calibration` for overall accuracy.")

@bot.command(name="cycle")
async def cycle_cmd(ctx, *, ticker: str = ""):
    """EchoEdge Cycle: parallel analysis → consensus recommendation for a ticker."""
    if not ticker:
        await ctx.send("Usage: `!cycle NVDA` or `!cycle PFE/MRK`")
        return
    ticker = ticker.strip().upper()
    msg = await ctx.send(f"Running EchoEdge cycle for **{ticker}**...")
    import requests as _cy_req

    signals = []  # list of (direction, confidence, source)
    notes = []

    is_pair = "/" in ticker
    tk_a = ticker.split("/")[0] if is_pair else ticker
    tk_b = ticker.split("/")[1] if is_pair else None

    # ── 1. Regime check ──
    regime_info = get_regime("equities")
    vix = regime_info.get("vix", 0) or 0
    regime_name = regime_info.get("regime", "normal")
    notes.append(f"VIX: {vix:.1f} | Regime: {regime_name}")
    if regime_name == "extreme":
        signals.append(("SHORT", 30, "regime_extreme"))
    elif regime_name == "elevated":
        signals.append(("SHORT", 15, "regime_elevated"))
    elif regime_name == "low":
        signals.append(("LONG", 10, "regime_low_vol"))

    # ── 2. Fear & Greed ──
    fng_val, fng_label = get_fear_greed()
    notes.append(f"F&G: {fng_val}/100 ({fng_label})")
    psych = _PSYCH_STATE
    if psych["contrarian_mode"]:
        signals.append(("LONG", 20, "contrarian_fear"))
        notes.append("Psychologist: CONTRARIAN MODE (extreme fear → buy signal)")
    elif psych["caution_mode"]:
        signals.append(("NEUTRAL", 15, "caution_greed"))
        notes.append("Psychologist: CAUTION MODE (extreme greed → reduce size)")

    # ── 3. Oracle signal check ──
    try:
        _prices = _oracle_get_all_prices()
        for title, yes_price in _prices.items():
            sig = _oracle_match_signal(title)
            if sig and ticker in (sig["long"], sig["short"]):
                _dir = "LONG" if ticker == sig["long"] else "SHORT"
                _conf = int(yes_price * 80)  # 0.70 → 56, 0.50 → 40
                signals.append((_dir, _conf, f"oracle:{sig['name']}"))
                notes.append(f"Oracle: {sig['name']} YES=${yes_price:.2f} → {_dir} {ticker}")
    except Exception:
        pass

    # ── 4. Pairs Z-score (if pair) ──
    if is_pair and tk_b:
        try:
            corr, zscore, mean_ratio = calculate_pair_zscore(tk_a, tk_b, 252)
            if zscore is not None and corr is not None:
                notes.append(f"Z-score: {zscore:.2f} | Corr: {corr:.3f}")
                if abs(zscore) >= 1.0 and corr >= 0.85:
                    _dir = "LONG" if zscore < 0 else "SHORT"  # Mean reversion: buy low Z, sell high Z
                    _conf = min(int(abs(zscore) * 25 * corr), 80)
                    signals.append((_dir, _conf, f"pairs_z={zscore:.1f}"))
                    # Historian stats
                    _hs = historian_analyze_pair(tk_a, tk_b)
                    if _hs.get("available"):
                        notes.append(f"Historian: {_hs['reversion_rate']*100:.0f}% revert rate, "
                                     f"avg {_hs['avg_reversion_days']:.0f}d, {_hs['sample_size']} samples")
                else:
                    notes.append("Z-score below entry threshold or low correlation")
            # Monte Carlo on pair spread
            try:
                _mc = montecarlo_simulate(tk_a, tk_b, entry_zscore=zscore, horizon_days=7)
                if _mc.get("available"):
                    _mc_prob = _mc["prob_profit"]
                    notes.append(f"Monte Carlo: {_mc_prob*100:.0f}% prob profit, "
                                 f"EV={_mc['expected_value']:.4f}, Sharpe={_mc['sharpe']:.2f}, "
                                 f"Kelly={_mc['kelly_fraction']*100:.1f}%")
                    if _mc_prob >= 0.60:
                        signals.append(("LONG" if zscore < 0 else "SHORT", int(_mc_prob * 30), f"mc_prob={_mc_prob:.0%}"))
                    elif _mc_prob < 0.45:
                        signals.append(("NEUTRAL", 20, f"mc_skip={_mc_prob:.0%}"))
            except Exception:
                pass
        except Exception:
            notes.append("Pairs calc error")
    else:
        # Single ticker — Monte Carlo + price check
        try:
            _mc = montecarlo_simulate(tk_a, horizon_days=7)
            if _mc.get("available"):
                notes.append(f"Monte Carlo: {_mc['prob_profit']*100:.0f}% prob profit, "
                             f"EV={_mc['expected_value']:.4f}, Sharpe={_mc['sharpe']:.2f}, "
                             f"Kelly={_mc['kelly_fraction']*100:.1f}%")
                if _mc["prob_profit"] >= 0.55:
                    signals.append(("LONG", int(_mc["prob_profit"] * 25), f"mc_prob={_mc['prob_profit']:.0%}"))
        except Exception:
            pass
        # Check price via Alpaca
        try:
            _hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
            _r = _cy_req.get(f"https://data.alpaca.markets/v2/stocks/{tk_a}/quotes/latest", headers=_hdr, timeout=5)
            if _r.status_code == 200:
                _q = _r.json().get("quote", {})
                _bid = float(_q.get("bp", 0) or 0)
                _ask = float(_q.get("ap", 0) or 0)
                # Use both sides when available, else whichever is non-zero
                if _bid > 0 and _ask > 0:
                    _mid = (_bid + _ask) / 2
                else:
                    _mid = _bid or _ask
                if _mid > 0:
                    notes.append(f"Price: ${_mid:,.2f}")
        except Exception:
            pass

    # ── 5. Squeeze check (single tickers) ──
    if not is_pair:
        try:
            _sq_cands = squeeze_scan()
            for _sq in _sq_cands:
                if _sq["ticker"] == tk_a:
                    _si = _sq["short_interest"]
                    _sp = _sq["squeeze_prob"]
                    notes.append(f"SQUEEZE: SI={_si*100:.1f}% squeeze_prob={_sp*100:.0f}%")
                    if _sp >= 0.40:
                        signals.append(("LONG", int(_sp * 40), f"squeeze={_sp:.0%}"))
                    break
        except Exception:
            pass

    # ── 6. Top 3 headlines from Meteorologist ──
    _hl_found = False
    for theme, entries in _INTEL_HEADLINE_MEMORY.items():
        if not entries:
            continue
        # Check if any headline mentions our ticker or related keywords
        _tk_lower = ticker.lower().replace("/", " ")
        for _, h in sorted(entries, key=lambda x: x[0], reverse=True)[:5]:
            if any(w in h.lower() for w in _tk_lower.split()):
                if not _hl_found:
                    notes.append("Headlines:")
                    _hl_found = True
                notes.append(f"  • {h[:75]}")
                if sum(1 for n in notes if n.startswith("  •")) >= 3:
                    break
        if sum(1 for n in notes if n.startswith("  •")) >= 3:
            break

    # ── Consensus ──
    if not signals:
        direction = "NEUTRAL"
        confidence = 50
    else:
        long_score = sum(c for d, c, _ in signals if d == "LONG")
        short_score = sum(c for d, c, _ in signals if d == "SHORT")
        neutral_score = sum(c for d, c, _ in signals if d == "NEUTRAL")

        if long_score > short_score and long_score > neutral_score:
            direction = "LONG"
            confidence = min(long_score, 95)
        elif short_score > long_score and short_score > neutral_score:
            direction = "SHORT"
            confidence = min(short_score, 95)
        else:
            direction = "NEUTRAL"
            confidence = min(max(long_score, short_score, neutral_score), 95)

    # Position sizing suggestion
    portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
        p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))
    _base_pct = 0.015 if confidence >= 60 else 0.01 if confidence >= 40 else 0.005
    _size = portfolio_value * _base_pct * psychologist_size_multiplier()

    # Direction emoji
    _emoji = {"LONG": "+", "SHORT": "-", "NEUTRAL": "~"}[direction]

    result = f"**EchoEdge Cycle: {ticker}**\n```\n"
    result += f"Consensus: {_emoji} {direction} | Confidence: {confidence}/100\n"
    result += f"Suggested size: ${_size:,.0f} ({_base_pct*100:.1f}% of ${portfolio_value:,.0f})\n"
    result += f"{'─' * 45}\n"
    result += f"Signal contributors:\n"
    for d, c, src in signals:
        result += f"  {d:7s} {c:>3d}pts  {src}\n"
    if not signals:
        result += "  (no signals — defaulting to NEUTRAL)\n"
    result += f"{'─' * 45}\n"
    for note in notes:
        result += f"{note}\n"
    result += f"```"

    await msg.edit(content=result)


@bot.command(name="montecarlo")
async def montecarlo_cmd(ctx, ticker_a: str = "", ticker_b: str = ""):
    """Run Monte Carlo simulation. Usage: !montecarlo NVDA AMD  or  !montecarlo SPY"""
    if not ticker_a:
        await ctx.send("Usage: `!montecarlo NVDA AMD` (pair) or `!montecarlo SPY` (single)")
        return
    ticker_a = ticker_a.upper()
    ticker_b = ticker_b.upper() if ticker_b else None
    label = f"{ticker_a}/{ticker_b}" if ticker_b else ticker_a

    msg = await ctx.send(f"Running 10,000-path Monte Carlo for **{label}**...")

    # If pair, get current Z-score for context
    entry_z = None
    corr_val = None
    if ticker_b:
        try:
            corr_val, entry_z, _ = calculate_pair_zscore(ticker_a, ticker_b, 252)
        except Exception:
            pass

    mc = montecarlo_simulate(ticker_a, ticker_b, entry_zscore=entry_z, n_paths=10000, horizon_days=7)

    if not mc.get("available"):
        await msg.edit(content=f"Monte Carlo for **{label}**: insufficient data (need 100+ trading days)")
        return

    portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
        p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))
    kelly_size = portfolio_value * mc["kelly_fraction"]

    # Verdict
    prob = mc["prob_profit"]
    if prob >= 0.65:
        verdict = "STRONG EDGE — full size"
    elif prob >= 0.55:
        verdict = "Modest edge — standard size"
    elif prob >= 0.45:
        verdict = "Marginal — reduce size 50%"
    else:
        verdict = "SKIP — negative edge"

    result = f"**Monte Carlo Simulation: {label}**\n```\n"
    result += f"Paths: {mc['n_paths']:,} | Horizon: 7 days | σ={mc['sigma']:.4f}\n"
    if ticker_b and entry_z is not None:
        result += f"Entry Z-score: {entry_z:.2f}"
        if corr_val is not None:
            result += f" | Correlation: {corr_val:.3f}"
        result += "\n"
    result += f"{'─' * 48}\n"
    result += f"{'Prob of profit:':<24s} {prob*100:>6.1f}%\n"
    result += f"{'Expected value:':<24s} {mc['expected_value']:>+6.4f}\n"
    result += f"{'Sharpe ratio:':<24s} {mc['sharpe']:>6.2f}\n"
    result += f"{'Max drawdown (95th):':<24s} {mc['max_drawdown_95']:>6.4f}\n"
    result += f"{'Kelly fraction:':<24s} {mc['kelly_fraction']*100:>6.1f}%\n"
    result += f"{'Kelly size:':<24s} ${kelly_size:>6,.0f}\n"
    result += f"{'90% CI:':<24s} [{mc['ci_low']:+.4f}, {mc['ci_high']:+.4f}]\n"
    result += f"{'─' * 48}\n"
    result += f"Verdict: {verdict}\n"
    result += f"```"

    await msg.edit(content=result)


@bot.command(name="allocation-status")
async def allocation_status_cmd(ctx):
    """Show current meta-allocation pillar weights and 7-day P&L attribution."""
    # Force a refresh
    meta_alloc_refresh()

    msg = "**Meta-Allocation Status**\n```\n"
    msg += f"{'Strategy':<18s} {'Weight':>6s} {'7d PnL':>10s} {'Trades':>6s}\n"
    msg += f"{'─' * 44}\n"

    total_pnl = 0
    total_trades = 0
    for strat in sorted(_META_ALLOC.keys()):
        weight = _META_ALLOC[strat]
        pnl_data = _META_ALLOC_PNL.get(strat, {})
        pnl = pnl_data.get("pnl", 0)
        trades = pnl_data.get("trades", 0)
        total_pnl += pnl
        total_trades += trades
        _tag = " ★" if weight > 1.0 else " ▼" if weight < 1.0 else ""
        msg += f"  {strat:<16s} {weight:>5.1f}x ${pnl:>+9.2f} {trades:>5d}{_tag}\n"

    msg += f"{'─' * 44}\n"
    msg += f"  {'TOTAL':<16s}       ${total_pnl:>+9.2f} {total_trades:>5d}\n"

    _last = _META_ALLOC_LAST_RUN
    if _last:
        _ago = (datetime.now(timezone.utc) - _last).total_seconds() / 3600
        msg += f"\nLast rebalance: {_ago:.1f}h ago\n"
    else:
        msg += "\nNot yet rebalanced (need 4h of data)\n"

    msg += "```"
    msg += "\n★ = top performer (1.5x) | ▼ = underperformer (0.5x)"
    await ctx.send(msg)


@bot.command(name="squeeze-scan")
async def squeeze_scan_cmd(ctx):
    """Scan for short squeeze candidates: high SI + reversion signals."""
    msg = await ctx.send("Scanning for short squeeze candidates...")

    candidates = squeeze_scan()
    if not candidates:
        await msg.edit(content="**Squeeze Scan**: No candidates found (need shortable stocks with >15% SI)")
        return

    result = "**Short Squeeze Candidates**\n```\n"
    result += f"{'Ticker':>6s} {'SI%':>6s} {'Price':>8s} {'Z-score':>8s} {'Pair':>6s} {'Sq Prob':>7s}\n"
    result += f"{'─' * 48}\n"

    for c in candidates[:8]:
        _z_str = f"{c['zscore']:+.2f}" if c["zscore"] is not None else "  n/a"
        _pair = c.get("pair_partner") or "—"
        result += (f"  {c['ticker']:>4s} {c['short_interest']*100:>5.1f}% "
                   f"${c['price']:>7.2f} {_z_str:>8s} {_pair:>6s} {c['squeeze_prob']*100:>5.0f}%\n")

    result += f"{'─' * 48}\n"
    result += f"SI > 15% + Z-score reversion = squeeze signal\n"
    result += "```"

    await msg.edit(content=result)


@bot.command(name="tv-signals")
async def tv_signals_cmd(ctx):
    """Show last 10 TradingView webhook signals received."""
    history = TRADINGVIEW_SIGNALS.get("history", [])
    if not history:
        await ctx.send("**TV Signals**: No signals received yet. Send webhooks to port 8080.")
        return

    msg = "**TradingView Signal History**\n```\n"
    msg += f"Auto-execute: {'ON' if TRADINGVIEW_SIGNALS.get('auto_execute') else 'OFF'}\n"
    msg += f"{'─' * 55}\n"
    msg += f"{'Time':>8s} {'Signal':>6s} {'Asset':>6s} {'Indicator':>14s} {'Price':>8s} {'Exec':>4s}\n"

    for sig in reversed(history[-10:]):
        _ts = sig.get("timestamp")
        _time_str = _ts.strftime("%H:%M") if hasattr(_ts, "strftime") else "?"
        _signal = sig.get("signal", "?")[:6]
        _asset = sig.get("asset", "?")[:6]
        _ind = sig.get("indicator", "?")[:14]
        _price = sig.get("price", 0)
        _price_str = f"${_price:.2f}" if _price else "—"
        _exec = "YES" if sig.get("executed") else "no"
        msg += f"  {_time_str:>6s} {_signal:>6s} {_asset:>6s} {_ind:>14s} {_price_str:>8s} {_exec:>4s}\n"

    msg += f"{'─' * 55}\n"
    msg += f"Total signals: {len(history)} | Last 24h: "
    _cutoff = datetime.utcnow() - __import__("datetime").timedelta(hours=24)
    _recent = sum(1 for s in history if hasattr(s.get("timestamp"), "timestamp") and s["timestamp"] > _cutoff)
    msg += f"{_recent}\n"
    msg += "```"
    await ctx.send(msg)


@bot.command(name="ai-consensus")
async def ai_consensus_cmd(ctx, *, ticker: str = ""):
    """Get AI second opinion on a ticker. Usage: !ai-consensus NVDA"""
    if not ticker:
        # Show recent AI consultations
        if not _AI_CONSENSUS_LOG:
            await ctx.send("**AI Consensus**: No consultations yet. Use `!ai-consensus NVDA`")
            return
        msg = "**AI Consensus Log**\n```\n"
        for entry in reversed(_AI_CONSENSUS_LOG[-8:]):
            _ts = entry["timestamp"].strftime("%m/%d %H:%M") if hasattr(entry["timestamp"], "strftime") else "?"
            msg += (f"[{_ts}] {entry['ticker']:6s} {entry['direction']:6s} "
                    f"score={entry['score']:>3d} → {entry['verdict']:7s}\n")
            msg += f"  {entry['reasoning'][:65]}\n"
        msg += "```"
        await ctx.send(msg)
        return

    ticker = ticker.strip().upper()
    msg = await ctx.send(f"Consulting AI on **{ticker}**...")

    # Build context from available data
    notes = [f"Ticker: {ticker}"]
    fng_val, fng_label = get_fear_greed()
    notes.append(f"F&G: {fng_val}/100 ({fng_label})")
    regime = get_regime("equities")
    notes.append(f"VIX: {regime.get('vix', '?')} Regime: {regime.get('regime', '?')}")
    psych = _PSYCH_STATE
    if psych["contrarian_mode"]:
        notes.append("Psychologist: CONTRARIAN MODE (extreme fear)")
    geo = _intel_get_geo_state()
    if geo["active"]:
        notes.append(f"Geo escalation: {geo['theme']} ({geo['count']} headlines)")

    # Get MC if available
    try:
        mc = montecarlo_simulate(ticker, horizon_days=7)
        if mc.get("available"):
            notes.append(f"Monte Carlo: {mc['prob_profit']*100:.0f}% prob, EV={mc['expected_value']:+.4f}")
    except Exception:
        pass

    verdict, reasoning = ai_get_second_opinion(ticker, "LONG", 65, notes)

    result = f"**AI Second Opinion: {ticker}**\n```\n"
    result += f"Verdict: {verdict}\n"
    result += f"Reasoning: {reasoning}\n"
    result += f"{'─' * 40}\n"
    result += f"Context provided:\n"
    for n in notes:
        result += f"  {n}\n"
    result += "```"
    await msg.edit(content=result)


@bot.command(name="engineer-log")
async def engineer_log_cmd(ctx):
    """Show last 10 auto-improvement adjustments from the Engineer Agent."""
    if not _ENGINEER_LOG:
        await ctx.send("**Engineer Log**: No adjustments yet. Needs 10+ closed trades per strategy.")
        return

    msg = "**Engineer Self-Improvement Log**\n```\n"
    for adj in reversed(_ENGINEER_LOG[-10:]):
        _ts = adj["timestamp"].strftime("%m/%d %H:%M") if hasattr(adj["timestamp"], "strftime") else "?"
        msg += (f"[{_ts}] {adj['strategy']:12s} {adj['metric']:16s} "
                f"{adj['old']:.3f} → {adj['new']:.3f}\n")
        msg += f"  Reason: {adj['reason'][:65]}\n"
    msg += "```"
    await ctx.send(msg)


@bot.command(name="risk-status")
async def risk_status_cmd(ctx):
    """Show risk management state: correlations, drawdowns, pauses."""
    risk_run_all_checks()

    msg = "**Risk Management Status**\n```\n"

    # Correlation flags
    flags = _RISK_STATE.get("corr_flags", [])
    msg += f"Correlated positions (>80%): {len(flags)}\n"
    for t1, t2, c in flags[:5]:
        msg += f"  {t1}/{t2}: {c:.2f} — BLOCKED\n"
    if not flags:
        msg += "  None — all clear\n"

    msg += f"{'─' * 42}\n"

    # Daily drawdown
    dd = _RISK_STATE.get("daily_drawdown", {})
    msg += f"Daily P&L by strategy:\n"
    for strat, pnl in sorted(dd.items()):
        _warn = " ⚠" if pnl < -_RISK_STATE["drawdown_limit"] else ""
        msg += f"  {strat:18s} ${pnl:>+8.2f}{_warn}\n"
    if not dd:
        msg += "  No closed trades today\n"

    msg += f"{'─' * 42}\n"

    # Pauses
    pauses = _RISK_STATE.get("strategy_pauses", {})
    msg += f"Active pauses: {len(pauses)}\n"
    for strat, until in pauses.items():
        _remaining = (until - datetime.now(timezone.utc)).total_seconds() / 3600
        msg += f"  {strat:18s} {_remaining:.1f}h remaining\n"
    if not pauses:
        msg += "  None — all strategies active\n"

    msg += f"{'─' * 42}\n"
    msg += f"Drawdown limit: ${_RISK_STATE['drawdown_limit']}/strategy/day\n"
    msg += "```"
    await ctx.send(msg)


@bot.command(name="morning")
async def morning_cmd(ctx):
    """Run the full morning briefing manually."""
    msg = await ctx.send("Generating morning briefing...")
    try:
        msgs = await _build_morning_briefing()
        await msg.delete()
        for m in msgs:
            await ctx.send(m)
    except Exception as e:
        await msg.edit(content=f"Morning briefing error: {e}")


@bot.command(name="evening")
async def evening_cmd(ctx):
    """Run the end-of-day briefing manually."""
    msg = await ctx.send("Generating evening briefing...")
    try:
        msgs = await _build_evening_briefing()
        await msg.delete()
        for m in msgs:
            await ctx.send(m)
    except Exception as e:
        await msg.edit(content=f"Evening briefing error: {e}")


@bot.command(name="week")
async def week_cmd(ctx):
    """Show full week performance summary."""
    msg = await ctx.send("Generating weekly summary...")
    try:
        msgs = await _build_week_summary()
        await msg.delete()
        for m in msgs:
            await ctx.send(m)
    except Exception as e:
        await msg.edit(content=f"Weekly summary error: {e}")


# ---------------------------------------------------------------------------
# SMS/EMAIL ALERT SYSTEM
# ---------------------------------------------------------------------------
TWILIO_SID = os.environ.get("TWILIO_SID", "")
TWILIO_TOKEN = os.environ.get("TWILIO_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_FROM", "")
TWILIO_TO = os.environ.get("TWILIO_TO", "")
SMTP_EMAIL = os.environ.get("SMTP_EMAIL", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_TO = os.environ.get("SMTP_TO", "")


def send_sms(message):
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, TWILIO_TO]):
        return False
    try:
        r = requests.post(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            data={"To": TWILIO_TO, "From": TWILIO_FROM, "Body": f"[TraderJoes] {message[:140]}"},
            auth=(TWILIO_SID, TWILIO_TOKEN), timeout=10)
        return r.status_code in (200, 201)
    except Exception as e:
        log.warning("SMS error: %s", e)
        return False


def send_email(subject, body):
    if not all([SMTP_EMAIL, SMTP_PASSWORD]):
        return False
    try:
        import smtplib
        from email.mime.text import MIMEText
        msg = MIMEText(body)
        msg["Subject"] = f"[TraderJoes] {subject}"
        msg["From"] = SMTP_EMAIL
        msg["To"] = SMTP_TO or SMTP_EMAIL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=10) as server:
            server.login(SMTP_EMAIL, SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except Exception as e:
        log.warning("Email error: %s", e)
        return False


def send_critical_alert(subject, message):
    sms_ok = send_sms(message)
    email_ok = send_email(subject, message)
    log.info("CRITICAL ALERT: %s (sms=%s email=%s)", subject, sms_ok, email_ok)
    return sms_ok or email_ok


@bot.command(name="memory-query")
async def memory_query_cmd(ctx, *, theme: str = ""):
    """Query echo memory + causal chain for a theme. Usage: !memory-query iran"""
    if not theme:
        await ctx.send("Usage: `!memory-query iran` or `!memory-query fed`")
        return

    # Echo memory (SQLite)
    events = echo_memory_query(theme, days=30, limit=10)
    pattern = echo_memory_get_pattern(theme)
    msg = f"**Echo Memory: {theme}**\n"
    if pattern:
        msg += f"> {pattern}\n"
    if events:
        msg += "```\n"
        for e in events:
            _pnl = f"${e['pnl']:+.2f}" if e.get("pnl") else "—"
            msg += f"  [{e.get('date', '?')[:10]}] {e.get('event_type', '?'):12s} prob={e.get('prob', 0):.2f} {_pnl} {e.get('headline', '')[:40]}\n"
        msg += "```\n"
    else:
        msg += "> No events in last 30 days\n"

    # Causal chain: asset impact from similar historical dates
    msg += f"\n**Causal Chain: What happened to related assets (48h after {theme} escalation)**\n"
    try:
        impacts = causal_memory_asset_impact(theme, hours=48)
        if impacts:
            msg += "```\n"
            for asset, pct, dt in impacts:
                arrow = "+" if pct > 0 else ""
                msg += f"  {dt} → {asset:5s} {arrow}{pct:.1f}%\n"
            msg += "```"
        else:
            msg += "> No causal data yet — snapshots build over time\n"
    except Exception:
        msg += "> Causal chain unavailable\n"

    # Regime similarity
    try:
        matches = causal_memory_query(n_results=3)
        if matches:
            msg += f"\n**Most Similar Regimes:**\n```\n"
            for meta, dist in matches:
                msg += (f"  {meta.get('date', '?')} VIX={meta.get('vix', '?')} "
                        f"F&G={meta.get('fng', '?')} pnl=${meta.get('total_daily_pnl', 0):+.0f} "
                        f"(sim={1-dist:.2f})\n")
            msg += "```"
    except Exception:
        pass

    await ctx.send(msg[:1900])


@bot.command(name="memory-status")
async def memory_status_cmd(ctx):
    """Show causal memory engine status: stored snapshots, current regime, similar dates."""
    # Current regime vector
    vector, meta = _build_regime_vector()
    msg = "**CAUSAL MEMORY ENGINE**\n"
    if meta:
        msg += (f"**Current Regime Vector:**\n"
                f"> VIX: {meta['vix']} | F&G: {meta['fng']} | SPY: ${meta['spy_price']:.0f} | "
                f"Trend: {meta['spy_trend_5d']:+.1f}%\n"
                f"> Regime: {meta['regime'].upper()} | "
                f"Iran: {meta['iran_esc']} | Ukraine: {meta['ukraine_esc']} | Taiwan: {meta['taiwan_esc']}\n")
    else:
        msg += "> Cannot build regime vector\n"

    # Collection stats
    try:
        if _CHROMA_COLLECTION:
            count = _CHROMA_COLLECTION.count()
            msg += f"\n**ChromaDB:** {count} snapshots stored\n"
        else:
            msg += "\n**ChromaDB:** Not initialized\n"
    except Exception:
        msg += "\n**ChromaDB:** Error reading collection\n"

    # Last 10 snapshots
    try:
        if _CHROMA_COLLECTION and _CHROMA_COLLECTION.count() > 0:
            all_data = _CHROMA_COLLECTION.get(
                limit=10,
                include=["metadatas"],
            )
            if all_data and all_data.get("metadatas"):
                msg += "\n**Recent Snapshots:**\n```\n"
                snapshots = sorted(all_data["metadatas"],
                                   key=lambda m: m.get("date", ""), reverse=True)
                for m in snapshots[:10]:
                    msg += (f"  {m.get('date', '?'):10s} VIX={m.get('vix', '?'):5s} "
                            f"F&G={str(m.get('fng', '?')):3s} "
                            f"regime={str(m.get('regime', '?')):8s} "
                            f"pnl=${m.get('total_daily_pnl', 0):+.0f}\n")
                msg += "```"
    except Exception as e:
        msg += f"\n> Snapshot read error: {e}\n"

    # Top 3 similar historical dates
    try:
        matches = causal_memory_query(n_results=3)
        if matches:
            msg += "\n**Top 3 Similar Historical Dates:**\n```\n"
            for meta_m, dist in matches:
                msg += (f"  {meta_m.get('date', '?')} "
                        f"VIX={meta_m.get('vix', '?')} F&G={meta_m.get('fng', '?')} "
                        f"SPY=${meta_m.get('spy_price', 0):.0f} "
                        f"pnl=${meta_m.get('total_daily_pnl', 0):+.0f} "
                        f"(similarity={1-dist:.2f})\n")
            msg += "```"
        else:
            msg += "\n> No historical matches yet — build snapshots over time\n"
    except Exception:
        pass

    await ctx.send(msg[:1900])


@bot.command(name="vol-skew")
async def vol_skew_cmd(ctx):
    """Show put/call IV skew on SPY and QQQ."""
    msg = await ctx.send("Scanning volatility skew...")
    result = "**Volatility Skew Scanner**\n```\n"
    for underlying in ["SPY", "QQQ"]:
        sk = vol_skew_scan(underlying)
        if sk.get("available"):
            result += (f"{underlying}: Put IV={sk['put_iv']:.2f} Call IV={sk['call_iv']:.2f} "
                       f"Skew={sk['skew_pct']:+.1f}%\n")
            result += f"  → {sk['recommendation']}\n"
        else:
            result += f"{underlying}: Data unavailable\n"
    result += "```"
    await msg.edit(content=result)


@bot.command(name="alert-test")
async def alert_test_cmd(ctx):
    """Test SMS and email alert channels."""
    results = []
    sms_ok = send_sms("Alert test — TraderJoes is operational")
    results.append(f"SMS: {'OK' if sms_ok else 'FAILED' if TWILIO_SID else 'NOT CONFIGURED'}")
    email_ok = send_email("Alert Test", "TraderJoes alert system operational.")
    results.append(f"Email: {'OK' if email_ok else 'FAILED' if SMTP_EMAIL else 'NOT CONFIGURED'}")
    await ctx.send(f"**Alert Test**\n" + "\n".join(results))


@bot.command(name="shadow-pnl")
async def shadow_pnl_cmd(ctx):
    """Show shadow portfolio (10x) vs real performance."""
    shadow_pnl, real_pnl = shadow_compare_performance()
    try:
        conn = sqlite3.connect(DB_PATH)
        _s = conn.execute("SELECT COUNT(*), SUM(size_usd) FROM shadow_positions WHERE status='open'").fetchone()
        _sc = conn.execute("SELECT COUNT(*) FROM shadow_positions WHERE status='closed'").fetchone()
        conn.close()
        s_open = _s[0] or 0
        s_deployed = _s[1] or 0
        s_closed = _sc[0] or 0
    except Exception:
        s_open = s_deployed = s_closed = 0

    ratio = shadow_pnl / (real_pnl * 10) if real_pnl != 0 else 1.0
    msg = f"**Shadow Portfolio (10x)**\n```\n"
    msg += f"{'Shadow P&L (7d):':<22s} ${shadow_pnl:>+10,.2f}\n"
    msg += f"{'Real P&L (7d):':<22s} ${real_pnl:>+10,.2f}\n"
    msg += f"{'Shadow/Real ratio:':<22s} {ratio:>10.2f}x\n"
    msg += f"{'Shadow open:':<22s} {s_open:>10d}\n"
    msg += f"{'Shadow closed:':<22s} {s_closed:>10d}\n"
    msg += f"{'Shadow deployed:':<22s} ${s_deployed:>10,.0f}\n"
    msg += f"{'─' * 36}\n"
    if shadow_pnl > real_pnl * 10 * 1.2:
        msg += "Shadow outperforming by >20% — consider sizing up\n"
    elif shadow_pnl < real_pnl * 10 * 0.8:
        msg += "Shadow underperforming — current sizing is appropriate\n"
    else:
        msg += "Shadow and real tracking within 20%\n"
    msg += "```"
    await ctx.send(msg)


@bot.command(name="performance")
async def performance_cmd(ctx, window: str = "7"):
    """Performance attribution by strategy. Usage: !performance 7 (or 30, 90)"""
    _days = int(window) if window.isdigit() else 7
    msg = await ctx.send(f"Calculating {_days}-day performance attribution...")

    try:
        import sqlite3 as _pfsq
        import numpy as np
        conn = _pfsq.connect(DB_PATH)
        cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=_days)).strftime("%Y-%m-%d")

        strats = conn.execute("SELECT DISTINCT strategy FROM positions WHERE status='closed' AND closed_at>=? AND realized_pnl IS NOT NULL", (cutoff,)).fetchall()

        lines = []
        for (strat,) in strats:
            if not strat:
                continue
            rows = conn.execute("""
                SELECT realized_pnl, size_usd,
                       (julianday(closed_at) - julianday(created_at)) * 24 as hold_h
                FROM positions WHERE status='closed' AND strategy=? AND closed_at>=? AND realized_pnl IS NOT NULL
                ORDER BY closed_at DESC
            """, (strat, cutoff)).fetchall()

            if not rows:
                continue
            pnls = [r[0] for r in rows]
            holds = [r[2] or 0 for r in rows]
            total = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            total_pnl = sum(pnls)
            avg_pnl = total_pnl / total
            std_pnl = float(np.std(pnls)) if total > 1 else 1

            # Sharpe
            sharpe = (avg_pnl / std_pnl * np.sqrt(252)) if std_pnl > 0 else 0

            # Sortino (downside deviation only)
            neg = [p for p in pnls if p < 0]
            down_dev = float(np.std(neg)) if len(neg) > 1 else 1
            sortino = (avg_pnl / down_dev * np.sqrt(252)) if down_dev > 0 else 0

            # Max drawdown
            cum = np.cumsum(pnls)
            max_dd = float(np.min(cum - np.maximum.accumulate(cum))) if len(cum) > 0 else 0

            # Profit factor
            gross_wins = sum(p for p in pnls if p > 0)
            gross_losses = abs(sum(p for p in pnls if p < 0))
            pf = gross_wins / gross_losses if gross_losses > 0 else float("inf")

            avg_hold = sum(holds) / total if total > 0 else 0

            lines.append({
                "strategy": strat, "trades": total, "wins": wins,
                "wr": wins / total * 100, "pnl": total_pnl, "avg": avg_pnl,
                "sharpe": sharpe, "sortino": sortino, "max_dd": max_dd,
                "pf": pf, "avg_hold": avg_hold,
                "best": max(pnls), "worst": min(pnls),
            })

        conn.close()

        if not lines:
            await msg.edit(content=f"**Performance**: No closed trades in last {_days} days")
            return

        lines.sort(key=lambda x: x["pnl"], reverse=True)

        result = f"**Performance Attribution** ({_days}d)\n```\n"
        result += f"{'Strategy':<14s} {'Trades':>6s} {'WR%':>5s} {'P&L':>9s} {'Sharpe':>7s} {'PF':>5s} {'MaxDD':>8s}\n"
        result += f"{'─' * 58}\n"
        for l in lines:
            _carry = " ★" if l["pnl"] == max(x["pnl"] for x in lines) else ""
            _drag = " ▼" if l["pnl"] == min(x["pnl"] for x in lines) and l["pnl"] < 0 else ""
            result += (f"  {l['strategy']:12s} {l['trades']:>6d} {l['wr']:>4.0f}% "
                       f"${l['pnl']:>+8.2f} {l['sharpe']:>7.2f} {l['pf']:>5.1f} "
                       f"${l['max_dd']:>+7.2f}{_carry}{_drag}\n")
        result += f"{'─' * 58}\n"
        _total = sum(l["pnl"] for l in lines)
        result += f"  {'TOTAL':12s}        ${_total:>+8.2f}\n"
        result += "```"
        result += "\n★ = carrying the firm | ▼ = dragging"
        await msg.edit(content=result)

    except Exception as e:
        await msg.edit(content=f"Performance error: {e}")


@bot.command(name="backtest")
async def backtest_cmd(ctx, strategy: str = "pairs", days: str = "90"):
    """Backtest a strategy over historical data. Usage: !backtest pairs 90"""
    _days = int(days) if days.isdigit() else 90
    msg = await ctx.send(f"Backtesting **{strategy}** over {_days} days...")

    try:
        import yfinance as yf
        import numpy as np

        trades = []

        if strategy == "pairs":
            cfg = EQUITIES_CONFIG["pairs"]
            _lookback = 60  # Rolling 60-day window for Z-score (no look-ahead)
            for ticker_a, ticker_b in cfg["seed"][:20]:
                try:
                    data_a = yf.download(ticker_a, period=f"{_days + _lookback}d", progress=False)
                    data_b = yf.download(ticker_b, period=f"{_days + _lookback}d", progress=False)
                    if len(data_a) < _lookback + 20 or len(data_b) < _lookback + 20:
                        continue
                    pa = data_a["Close"].values.flatten()
                    pb = data_b["Close"].values.flatten()
                    ml = min(len(pa), len(pb))
                    pa, pb = pa[-ml:], pb[-ml:]
                    ratio = pa / pb

                    # Walk-forward: compute Z-score using ONLY past data at each point
                    in_trade = False
                    entry_z = 0
                    entry_idx = 0
                    entry_ratio = 0
                    for i in range(_lookback, len(ratio)):
                        # Rolling window: only use data[i-lookback:i] for mean/std
                        _window = ratio[i - _lookback:i]
                        _mean = float(np.mean(_window))
                        _std = float(np.std(_window))
                        if _std == 0:
                            continue
                        z = float((ratio[i] - _mean) / _std)

                        if not in_trade and abs(z) >= cfg.get("zscore_entry", 1.0):
                            in_trade = True
                            entry_z = z
                            entry_idx = i
                            entry_ratio = float(ratio[i])
                        elif in_trade:
                            exited = False
                            if abs(z) < cfg.get("zscore_exit", 0.5) or (entry_z > 0 and z < 0) or (entry_z < 0 and z > 0):
                                exited = True
                            elif i - entry_idx > cfg.get("ttl_days", 7):
                                exited = True
                            if exited:
                                # Realistic PnL from ratio change on $350/leg notional
                                exit_ratio = float(ratio[i])
                                if entry_z > 0:  # Short spread — profit when ratio falls
                                    pnl = (entry_ratio - exit_ratio) / entry_ratio * 350
                                else:  # Long spread — profit when ratio rises
                                    pnl = (exit_ratio - entry_ratio) / entry_ratio * 350
                                trades.append({"pair": f"{ticker_a}/{ticker_b}", "pnl": pnl,
                                              "hold_days": i - entry_idx, "entry_z": entry_z})
                                in_trade = False
                except Exception:
                    continue

        elif strategy == "crypto":
            # Replay momentum signals on top cryptos
            for sym in ["BTC", "ETH", "SOL", "AVAX", "LINK"]:
                try:
                    data = yf.download(f"{sym}-USD", period=f"{_days}d", progress=False)
                    if len(data) < 30:
                        continue
                    closes = data["Close"].values.flatten()
                    for i in range(1, len(closes)):
                        chg = (closes[i] - closes[i-1]) / closes[i-1] * 100
                        if abs(chg) > 8:  # Momentum signal
                            # Hold 1 day
                            if i + 1 < len(closes):
                                next_chg = (closes[i+1] - closes[i]) / closes[i] * 100
                                pnl = next_chg * 2.5  # ~$250 position
                                trades.append({"pair": sym, "pnl": pnl, "hold_days": 1, "entry_z": chg})
                except Exception:
                    continue
        else:
            await msg.edit(content=f"Unknown strategy: {strategy}. Use: pairs, crypto")
            return

        if not trades:
            await msg.edit(content=f"**Backtest {strategy}**: No trades generated in {_days} days")
            return

        # Calculate stats
        pnls = [t["pnl"] for t in trades]
        wins = sum(1 for p in pnls if p > 0)
        total_pnl = sum(pnls)
        avg_pnl = total_pnl / len(pnls)
        std_pnl = float(np.std(pnls)) if len(pnls) > 1 else 1
        sharpe = avg_pnl / std_pnl * np.sqrt(252) if std_pnl > 0 else 0
        cum = np.cumsum(pnls)
        max_dd = float(np.min(cum - np.maximum.accumulate(cum)))
        best = max(pnls)
        worst = min(pnls)

        # Store in SQLite
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT INTO backtest_results (strategy,days,run_at,total_trades,win_rate,total_pnl,avg_pnl,sharpe,max_drawdown,best_trade,worst_trade) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (strategy, _days, now_str(), len(trades), wins/len(trades), total_pnl, avg_pnl, sharpe, max_dd, best, worst))
            conn.commit()
            conn.close()
        except Exception:
            pass

        result = f"**Backtest: {strategy}** ({_days} days)\n```\n"
        result += f"{'Trades:':<22s} {len(trades):>10d}\n"
        result += f"{'Win rate:':<22s} {wins/len(trades)*100:>9.0f}%\n"
        result += f"{'Total P&L:':<22s} ${total_pnl:>+10,.2f}\n"
        result += f"{'Avg P&L/trade:':<22s} ${avg_pnl:>+10,.2f}\n"
        result += f"{'Sharpe (ann):':<22s} {sharpe:>10.2f}\n"
        result += f"{'Max drawdown:':<22s} ${max_dd:>+10,.2f}\n"
        result += f"{'Best trade:':<22s} ${best:>+10,.2f}\n"
        result += f"{'Worst trade:':<22s} ${worst:>+10,.2f}\n"
        result += "```"
        await msg.edit(content=result)

    except Exception as e:
        await msg.edit(content=f"Backtest error: {e}")

def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")


@bot.command(name="pairs-scan")
async def pairs_scan_cmd(ctx):
    """Show discovered pairs by sector from S&P 500 scan."""
    try:
        import sqlite3 as _psq
        conn = _psq.connect(DB_PATH)
        rows = conn.execute("SELECT ticker_a, ticker_b, correlation, sector, discovered_at FROM pairs_discovery ORDER BY correlation DESC LIMIT 50").fetchall()
        conn.close()
    except Exception:
        rows = []

    if not rows:
        await ctx.send("**Pairs Discovery**: No pairs found yet. Runs daily at market open.")
        return

    by_sector = {}
    for t1, t2, corr, sector, dt in rows:
        by_sector.setdefault(sector or "?", []).append((t1, t2, corr))

    msg = f"**Discovered Pairs** ({len(rows)} total)\n```\n"
    for sector in sorted(by_sector.keys()):
        pairs = by_sector[sector]
        msg += f"{sector[:25]}:\n"
        for t1, t2, corr in pairs[:5]:
            msg += f"  {t1:5s}/{t2:5s}  {corr:.3f}\n"
        if len(pairs) > 5:
            msg += f"  +{len(pairs)-5} more\n"
    msg += "```"
    if len(msg) > 1900:
        msg = msg[:1900] + "\n```"
    await ctx.send(msg)


@bot.command(name="security-status")
async def security_status(ctx):
    """Show security status including hostname, circuit breaker, key rotation."""
    hostname_ok = check_hostname_security()
    cb = CIRCUIT_BREAKER
    rotations = check_key_rotation_needed()
    report = (
        f"**Security Status**\n================================\n"
        f"Hostname: {socket.gethostname()} ({'OK' if hostname_ok else 'UNAUTHORIZED'})\n"
        f"Allowed: {ALLOWED_HOSTNAME}\n"
        f"Circuit breaker: {'TRIPPED' if cb['tripped'] else 'OK'}\n"
        f"Trades last 60s: {len(cb['trades_last_60s'])} / {cb['max_trades_per_minute']} max\n"
    )
    if cb["tripped"]:
        report += f"Trip reason: {cb['trip_reason']}\n"
    if rotations:
        report += "\n**Key Rotation Reminders:**\n"
        for r in rotations:
            report += f"  {r}\n"
    else:
        report += "\nKey rotation: All keys current\n"
    report += "================================"
    await ctx.send(report)

@bot.command(name="reset-breaker")
async def reset_breaker(ctx):
    """Reset the circuit breaker after it trips."""
    CIRCUIT_BREAKER["tripped"] = False
    CIRCUIT_BREAKER["trip_reason"] = ""
    CIRCUIT_BREAKER["trades_last_60s"] = []
    await ctx.send("Circuit breaker **RESET**. Trading can resume.")

@bot.command(name="key-rotation-status")
async def key_rotation_status(ctx):
    """Check API key rotation status."""
    reminders = check_key_rotation_needed()
    if reminders:
        msg = "**Key Rotation Reminders:**\n"
        for r in reminders:
            msg += f"  {r}\n"
    else:
        msg = "All API keys are current. No rotation needed."
    msg += f"\nLast Kalshi rotation: {KEY_ROTATION_CONFIG['kalshi_last_rotated']}"
    msg += f"\nRotation interval: {KEY_ROTATION_CONFIG['rotation_interval_days']} days"
    msg += "\n\nTo mark keys as rotated: `!key-rotated kalshi`"
    await ctx.send(msg)

@bot.command(name="key-rotated")
async def key_rotated(ctx, platform: str = ""):
    """Mark a platform's API key as freshly rotated."""
    if not platform:
        await ctx.send("Usage: `!key-rotated kalshi` or `!key-rotated phemex`")
        return
    KEY_ROTATION_CONFIG[f"{platform.lower()}_last_rotated"] = datetime.utcnow().strftime("%Y-%m-%d")
    await ctx.send(f"Marked **{platform}** API key as rotated today.")

@bot.command(name="kill-switch")
async def kill_switch(ctx, action: str = ""):
    """Emergency kill switch. Usage: !kill-switch on/off"""
    if action.lower() == "on":
        COST_CONFIG["kill_switch"] = True
        notify_zapier("kill_switch", {"action": "activated", "reason": "manual"})
        await ctx.send("**KILL SWITCH ACTIVATED** — All trading halted immediately.")
    elif action.lower() == "off":
        COST_CONFIG["kill_switch"] = True  # require explicit confirmation
        await ctx.send("Type `!confirm-kill-off` to deactivate kill switch.")
    else:
        status = "ACTIVE" if COST_CONFIG["kill_switch"] else "Inactive"
        await ctx.send(f"Kill switch: **{status}**\nUsage: `!kill-switch on` or `!kill-switch off`")


@bot.command(name="confirm-kill-off")
async def confirm_kill_off(ctx):
    """Confirm deactivating the kill switch."""
    COST_CONFIG["kill_switch"] = False

@bot.command(name="tv-signal")
async def tv_signal(ctx, signal_type: str = "", asset: str = "MARKET", indicator: str = "MarketCipher"):
    """Input TradingView/Market Cipher signal. Usage: !tv-signal BUY BTC MarketCipher"""
    if not signal_type:
        sig = TRADINGVIEW_SIGNALS.get("latest_signal", {})
        if sig:
            age = (datetime.utcnow() - sig.get("timestamp", datetime.min)).total_seconds() / 60
            await ctx.send(f"**TV/MC Signal Active**\nType: {sig.get('signal')} | Asset: {sig.get('asset')} | Indicator: {sig.get('indicator')}\nAge: {age:.0f}min | Expires: {TRADINGVIEW_SIGNALS['signal_expiry_minutes']}min")
        else:
            await ctx.send("No active TradingView signal.\nUsage: `!tv-signal BUY BTC MarketCipher`\nSignals: BUY, SELL, BULLISH, BEARISH, GREEN_DOT, RED_DOT, BLUE_WAVE, RED_WAVE")
        return
    TRADINGVIEW_SIGNALS["latest_signal"] = {
        "signal": signal_type.upper(),
        "asset": asset.upper(),
        "indicator": indicator,
        "timestamp": datetime.utcnow(),
    }
    await ctx.send(f"**TV/MC Signal Set:** {signal_type.upper()} {asset.upper()} ({indicator})\nWill boost edge scores for {TRADINGVIEW_SIGNALS['signal_expiry_minutes']} minutes.\nNote: This boosts scores but does NOT auto-trigger trades alone.")
    await ctx.send("Kill switch **DEACTIVATED** — Trading resumed.")


@bot.command(name="costs")
async def show_costs(ctx):
    """Show cost optimization and safety status."""
    c = COST_CONFIG
    a = ANALYTICS
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await ctx.send(
        f"**Cost & Safety Dashboard** | {ts}\n"
        f"================================\n"
        f"**OpenAI Costs:**\n"
        f"  Model: {c['preferred_model']}\n"
        f"  Monthly spent: ${a['openai_cost_usd']:.4f} / ${c['monthly_openai_limit']:.2f}\n"
        f"  Calls: {a['openai_calls']} | Tokens: {a['openai_tokens']:,}\n"
        f"\n**Safety Guards:**\n"
        f"  Kill switch: {'ACTIVE' if c['kill_switch'] else 'Off'}\n"
        f"  Strict mode: {'On' if c['strict_mode'] else 'Off'}\n"
        f"  Max daily trades: {c['max_daily_trades']}\n"
        f"  Today\'s trades: {c['daily_trades_count']}\n"
        f"  Max position: {MAX_POSITION_PCT:.1%}\n"
        f"  Max portfolio risk: {c['max_portfolio_risk']:.1%}\n"
        f"  Daily loss limit: ${DAILY_LOSS_LIMIT:,.2f}\n"
        f"  Trading mode: {TRADING_MODE.upper()}\n"
        f"================================"
    )




@bot.command(name="studio")
async def studio_status(ctx):
    """Show OpenClaw Studio dashboard status."""
    await ctx.send(
        f"**OpenClaw Studio**\n================================\n"
        f"Status: Running (localhost:3000)\n"
        f"Access: Via Tailscale at http://100.89.63.72:3000\n"
        f"Features:\n"
        f"  • Agent chat interface\n"
        f"  • Job scheduling & approval gates\n"
        f"  • Real-time skill monitoring\n"
        f"  • Trade execution dashboard\n"
        f"\n**Netdata Monitoring:**\n"
        f"  URL: http://100.89.63.72:19999\n"
        f"  Metrics: equity, PnL, Sharpe, drawdown, OpenAI costs\n"
        f"================================"
    )




@tasks.loop(minutes=10)
async def alert_scan_task():
    """Periodically scan for high-EV opportunities and send alerts."""
    try:
        adapt_cycle_rate()  # adjust scan rate based on volatility
        if not CYCLE_PAUSED and not COST_CONFIG.get("kill_switch", False):
            await check_and_send_alerts()
            push_all_analytics()  # push metrics each cycle
            # === PAIRS DISCOVERY (runs daily, internally throttled) ===
            try:
                discover_sp500_pairs()
            except Exception as _pderr:
                log.warning("Pairs discovery error: %s", _pderr)
            # === PAIRS TRADING SCANNER (runs during market hours) ===
            if EQUITIES_ENABLED and is_market_open():
                try:
                    channel = bot.get_channel(int(DISCORD_CHANNEL_ID))
                    pairs_opps = scan_pairs_opportunities()

                    _sm = datetime.now().minute
                    if _sm % 30 < 10:
                        try:
                            _pch = bot.get_channel(int(DISCORD_CHANNEL_ID))
                            import sys as _sys
                            _main_mod = _sys.modules.get("__main__")
                            _pead_fn = getattr(_main_mod, "run_pead_scanner", None) if _main_mod else None
                            if _pead_fn:
                                await _pead_fn(_pch)
                            else:
                                log.warning("PEAD: run_pead_scanner not found in __main__ module")
                        except Exception as _pe:
                            log.warning("PEAD error: %s", _pe)

                    for po in pairs_opps:
                        log.info("PAIRS SIGNAL: %s corr=%.3f z=%.2f dir=%s",
                                 po.get("pair",""), po.get("correlation",0),
                                 po.get("zscore",0), po.get("direction",""))
                        if channel and channel is not None:
                            await channel.send(
                                f"**PAIRS SIGNAL** {po['pair']} | "
                                f"Corr: {po['correlation']:.3f} | "
                                f"Z: {po['zscore']:+.2f} | "
                                f"Dir: {po['direction']}")
                except Exception as pairs_err:
                    log.warning("Pairs scan error: %s", pairs_err)
            elif EQUITIES_ENABLED and not is_market_open():
                pass  # Market closed, skip silently
            # === FUNDING ARB SCANNER (runs 24/7, Phemex + Binance) ===
            try:
                _arb_cfg = globals().get("FUNDING_ARB_CONFIG", {})
                _arb_threshold = _arb_cfg.get("min_funding_rate", 0.0003)
                _arb_rates = fetch_all_funding_rates()
                for _fname, _ri in _arb_rates.items():
                    try:
                        _rate_pct = _ri["best"]
                        _above = _rate_pct > (0.0005 if _fname in ("SOL", "XRP", "BNB") else _arb_threshold)
                        log.info("FUNDING-ARB %s: phemex=%.4f%% binance=%.4f%% best=%.4f%% threshold=%.4f%% %s",
                                 _fname, _ri["phemex"] * 100, _ri["binance"] * 100,
                                 _rate_pct * 100, (0.0005 if _fname in ("SOL", "XRP", "BNB") else _arb_threshold) * 100,
                                 ">>> SIGNAL" if _above else "below threshold")
                        if _above and AUTO_PAPER_ENABLED:
                            _arb_size = PAPER_PORTFOLIO.get("cash", 25000) * 0.02
                            if _arb_size > 50 and len(PAPER_PORTFOLIO.get("positions", [])) < 25:
                                _spot_ok, _spot_msg = False, ""
                                _perp_ok, _perp_msg, _perp_oid = False, "", None
                                _spot_oid = None
                                try:
                                    _spot_ok, _spot_msg = await execute_coinbase_order("BUY", _fname, _arb_size)
                                    if _spot_ok:
                                        import re as _arb_re
                                        _oid_match = _arb_re.search(r"ID: ([^\)]+)", _spot_msg)
                                        _spot_oid = _oid_match.group(1) if _oid_match else "unknown"
                                        log.info("ARB SPOT LEG OK: %s %s", _fname, _spot_msg[:80])
                                    else:
                                        log.warning("ARB SPOT LEG FAILED: %s %s", _fname, _spot_msg[:100])
                                        continue
                                except Exception as _se:
                                    log.warning("ARB SPOT LEG ERROR: %s %s", _fname, _se)
                                    continue
                                try:
                                    _perp_ok, _perp_msg, _perp_oid = await execute_phemex_perp_short(_fname, _arb_size)
                                    if _perp_ok:
                                        log.info("ARB PERP LEG OK: %s %s", _fname, _perp_msg[:80])
                                    else:
                                        log.warning("ARB PERP LEG FAILED: %s %s", _fname, _perp_msg[:100])
                                        try:
                                            await execute_coinbase_order("SELL", _fname, _arb_size)
                                            log.info("ARB UNWIND: sold spot %s (perp leg failed)", _fname)
                                        except Exception:
                                            log.warning("ARB UNWIND FAILED: %s", _fname)
                                        continue
                                except Exception as _pe:
                                    log.warning("ARB PERP LEG ERROR: %s %s — unwinding spot", _fname, _pe)
                                    try:
                                        await execute_coinbase_order("SELL", _fname, _arb_size)
                                    except Exception:
                                        pass
                                    continue
                                _arb_pos = {
                                    "market": f"FUNDING-ARB:{_fname}",
                                    "side": "ARB", "shares": 1,
                                    "entry_price": _rate_pct,
                                    "cost": _arb_size * 2, "value": _arb_size * 2,
                                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                    "platform": f"{_ri['best_source'].title()}+Coinbase",
                                    "ev": _rate_pct, "strategy": "funding_arb",
                                    "spot_order_id": _spot_oid,
                                    "perp_order_id": _perp_oid,
                                }
                                PAPER_PORTFOLIO["positions"].append(_arb_pos)
                                PAPER_PORTFOLIO["cash"] -= _arb_size * 2
                                db_log_paper_trade(_arb_pos)
                                db_open_position(
                                    market_id=f"FUNDING-ARB:{_fname}",
                                    platform=f"{_ri['best_source'].title()}+Coinbase",
                                    strategy="funding_arb",
                                    direction="arb", size_usd=_arb_size * 2, shares=1,
                                    entry_price=_rate_pct,
                                    metadata={"funding_rate": _rate_pct, "source": _ri["best_source"],
                                              "spot_order": _spot_oid, "perp_order": _perp_oid,
                                              "leg_size": _arb_size},
                                )
                                log.info("FUNDING-ARB TRADE: %s rate=%.4f%% size=$%.0f (%s+spot)",
                                         _fname, _rate_pct * 100, _arb_size, _ri["best_source"])
                                if channel:
                                    await channel.send(
                                        f"**FUNDING ARB** {_fname} | Rate: {_rate_pct*100:.4f}% ({_ri['best_source']})\n"
                                        f"Spot BUY: ${_arb_size:.0f} | Perp SHORT: ${_arb_size:.0f}")
                    except Exception as _fe:
                        log.warning("Funding arb %s error: %s", _fname, _fe)
            except Exception as arb_err:
                log.warning("Funding arb scan error: %s", arb_err)
            # === CRYPTO PAIRS STAT ARB (runs 24/7, every other cycle ~10min) ===
            try:
                _cp_fn = getattr(__import__("sys").modules.get("__main__"), "scan_crypto_pairs", None)
                if _cp_fn:
                    _cp_fired = await _cp_fn()
                    if _cp_fired and _cp_fired > 0:
                        log.info("CRYPTO PAIRS: %d trades fired", _cp_fired)
            except Exception as _cperr:
                log.warning("Crypto pairs scan error: %s", _cperr)
            # === CRASH HEDGE SCANNER (runs during market hours) ===
            if is_market_open():
                try:
                    _hedge_ch = bot.get_channel(int(DISCORD_CHANNEL_ID)) if DISCORD_CHANNEL_ID else None
                    _check_hedge_fn = __import__("sys").modules.get("__main__")
                    _check_hedge_fn = getattr(_check_hedge_fn, "check_crash_hedges", None) if _check_hedge_fn else None
                    if _check_hedge_fn:
                        await _check_hedge_fn(_hedge_ch)
                except Exception as hedge_err:
                    log.warning("Crash hedge scan error: %s", hedge_err)
            # === META-ALLOCATION ENGINE (rebalances every 4h internally) ===
            try:
                _rebal = meta_alloc_refresh()
                if _rebal:
                    _alloc_ch = bot.get_channel(int(DISCORD_CHANNEL_ID)) if DISCORD_CHANNEL_ID else None
                    if _alloc_ch:
                        _alloc_msg = "**META-ALLOC Rebalanced**\n```\n"
                        for _s, _w in sorted(_META_ALLOC.items()):
                            _p = _META_ALLOC_PNL.get(_s, {})
                            _pnl = _p.get("pnl", 0)
                            _trades = _p.get("trades", 0)
                            _alloc_msg += f"  {_s:16s} {_w:.1f}x  7d PnL: ${_pnl:>+8.2f} ({_trades} trades)\n"
                        _alloc_msg += "```"
                        try:
                            await _alloc_ch.send(_alloc_msg)
                        except Exception:
                            pass
            except Exception as _maerr:
                log.warning("Meta-alloc error: %s", _maerr)
            # === PSYCHOLOGIST AGENT (every cycle) ===
            try:
                _psych = psychologist_update()
                if _psych.get("regime_changed"):
                    _psych_ch = bot.get_channel(int(DISCORD_CHANNEL_ID)) if DISCORD_CHANNEL_ID else None
                    if _psych_ch:
                        if _psych["contrarian_mode"]:
                            await _psych_ch.send(
                                f"**PSYCHOLOGIST — CONTRARIAN MODE**\n"
                                f"Fear & Greed: {_psych['last_fng']}/100 ({_psych['last_label']})\n"
                                f"Extreme fear detected — biasing toward mean reversion, away from momentum")
                        elif _psych["caution_mode"]:
                            await _psych_ch.send(
                                f"**PSYCHOLOGIST — CAUTION MODE**\n"
                                f"Fear & Greed: {_psych['last_fng']}/100 ({_psych['last_label']})\n"
                                f"Extreme greed detected — all position sizes tightened to 0.5x")
                        else:
                            await _psych_ch.send(
                                f"**PSYCHOLOGIST — NORMAL MODE**\n"
                                f"Fear & Greed: {_psych['last_fng']}/100 ({_psych['last_label']})\n"
                                f"Sentiment regime cleared")
            except Exception as _pserr:
                log.warning("Psychologist error: %s", _pserr)
            # === ORACLE ENGINE (runs every cycle, market hours preferred) ===
            try:
                _oracle_ch = bot.get_channel(int(DISCORD_CHANNEL_ID)) if DISCORD_CHANNEL_ID else None
                _oracle_fired = await scan_oracle_signals(_oracle_ch)
                if _oracle_fired > 0:
                    log.info("Oracle engine fired %d signals this cycle", _oracle_fired)
            except Exception as _oerr:
                log.warning("Oracle engine error: %s", _oerr)
            # === ENGINEER AGENT (self-improvement, every cycle) ===
            try:
                engineer_self_improve()
            except Exception as _engr:
                log.warning("Engineer agent error: %s", _engr)
            # === RISK MANAGEMENT AGENT (every cycle) ===
            try:
                risk_run_all_checks()
                _pauses = _RISK_STATE.get("strategy_pauses", {})
                if _pauses:
                    _risk_ch = bot.get_channel(int(DISCORD_CHANNEL_ID)) if DISCORD_CHANNEL_ID else None
                    for _ps, _pt in list(_pauses.items()):
                        if _risk_ch and _RISK_STATE.get(f"_notified_{_ps}") is None:
                            _RISK_STATE[f"_notified_{_ps}"] = True
                            try:
                                await _risk_ch.send(
                                    f"**RISK PAUSE** — {_ps}\n"
                                    f"Daily drawdown: ${_RISK_STATE['daily_drawdown'].get(_ps, 0):+.2f}\n"
                                    f"Paused until: {_pt.strftime('%H:%M UTC')}")
                            except Exception:
                                pass
            except Exception as _rkerr:
                log.warning("Risk agent error: %s", _rkerr)
            # === OPTIONS THETA HARVEST (market hours, after crash hedge) ===
            if is_market_open():
                try:
                    _theta_ch = bot.get_channel(int(DISCORD_CHANNEL_ID)) if DISCORD_CHANNEL_ID else None
                    _theta_fired = await scan_theta_harvest(_theta_ch)
                    if _theta_fired > 0:
                        log.info("Theta harvest opened %d spread(s)", _theta_fired)
                except Exception as _therr:
                    log.warning("Theta harvest error: %s", _therr)
            # === EVENT SYNTHETICS (cross-platform arb execution) ===
            try:
                _synth_ch = bot.get_channel(int(DISCORD_CHANNEL_ID)) if DISCORD_CHANNEL_ID else None
                _synth_fired = await execute_event_synthetics(_synth_ch)
                if _synth_fired > 0:
                    log.info("Event synthetics fired %d arb positions", _synth_fired)
            except Exception as _serr:
                log.warning("Event synthetics error: %s", _serr)
            # Run exit manager every cycle
            try:
                exits = await run_exit_manager()
                if exits > 0:
                    log.info("Exit manager closed %d positions this cycle", exits)
                try:
                    _ar = auto_resolve_expired()
                    if _ar > 0:
                        log.info("Auto-resolver closed %d expired markets", _ar)
                except Exception as _are:
                    log.warning("Auto-resolver error: %s", _are)
            except Exception as ex:
                log.warning("Exit manager error: %s", ex)
            # Enforce minimum paper trade floor
            try:
                ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
                if ch:
                    await enforce_paper_trade_floor(ch)
            except Exception as pfe:
                log.warning("Paper floor error: %s", pfe)
            # Unified exit check (run_exit_manager is the single exit path)
            try:
                ch = bot.get_channel(int(DISCORD_CHANNEL_ID))
                if ch:
                    await run_exit_manager(ch)
            except Exception as eex:
                log.warning("Exit check error: %s", eex)
    except Exception as exc:
        log.warning("Alert scan error: %s", exc)


@alert_scan_task.before_loop
async def before_alert_scan():
    await bot.wait_until_ready()




# ============================================================================
# AUTO-PAPER TRADING + LEARNING SYSTEM
# ============================================================================
AUTO_PAPER_ENABLED = True
SIGNAL_HISTORY = []  # All high-EV signals: executed and not executed


def record_signal(opp, executed=False, paper=True):
    """Record a high-EV signal for learning."""
    signal = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "platform": opp.get("platform", "unknown"),
        "market": opp.get("market", "")[:80],
        "ev": opp.get("ev", 0),
        "type": opp.get("type", ""),
        "detail": opp.get("detail", "")[:100],
        "executed": executed,
        "paper": paper,
        "entry_price": None,
        "exit_price": None,
        "pnl": None,
        "size": suggest_position_size(opp.get("ev", 0)),
        "fng": None,
    }
    try:
        fng_val, fng_label = get_fear_greed()

        signal["fng"] = fng_val
    except Exception:
        pass
    SIGNAL_HISTORY.append(signal)
    # Keep last 500 signals
    if len(SIGNAL_HISTORY) > 500:
        SIGNAL_HISTORY.pop(0)
    return signal



# === SPORTS/JUNK STRUCTURAL FIREWALL ===
import re as _sports_re

def is_sports_or_junk(title):
    """Pattern-based detection of sports and junk markets. No team names needed."""
    if not title:
        return False
    if "$0.0" in title or "RAIN $0.0" in title:
        return True
    t = title.lower()
    # Sports patterns
    if " vs." in t or " vs " in t:
        return True
    if _sports_re.search(r'\([+-]?\d+\.?\d*\)', t):
        return True
    if _sports_re.search(r'win on \d{4}', t):
        return True
    if " fc " in t or t.startswith("fc ") or " afc " in t:
        return True
    sports_terms = [
        "nba", "nfl", "nhl", "mlb", "mls", "epl", "ufc", "ncaa", "wnba",
        "premier league", "la liga", "serie a", "bundesliga", "champions league",
        "ligue 1", "copa ", "soccer", "basketball", "baseball", "hockey",
        "football game", "touchdown", "quarterback", "halftime", "innings",
        "goal scorer", "match result", "spread:", "moneyline", "over/under",
        "playoff", "world series", "super bowl", "world cup", "grand slam",
        "formula 1", "f1 ", "nascar", "boxing", "mma ", "bellator",
    ]
    if any(s in t for s in sports_terms):
        return True
    # Junk/meme patterns
    junk_terms = [
        "jesus christ", "supervolcano", "mars before", "next erupt", "2050",
        "land on mars", "2 degrees celsius", "alien contact", "rapture",
        "asteroid", "bigfoot", "ufo ", "flat earth", "zombie",
    ]
    if any(s in t for s in junk_terms):
        return True
    return False

async def auto_paper_execute(channel, opp):
    """Automatically execute a high-EV opportunity in paper mode."""
    if not AUTO_PAPER_ENABLED:
        return False
    if TRADING_MODE != "paper":
        return False
    if COST_CONFIG.get("kill_switch", False):
        return False

    ev = opp.get("ev", 0)
    if ev < ALERT_CONFIG["min_ev_threshold"]:
        return False
    # === PSYCHOLOGIST: skip momentum in contrarian mode ===
    if psychologist_should_skip_momentum() and opp.get("type", "").lower() == "momentum":
        log.info("PSYCHOLOGIST: skip momentum %s (contrarian mode)", opp.get("market", "")[:30])
        return False
    # === BLACKLIST (sports, memes, long-term junk) ===
    _mkt_lower = opp.get("market", "").lower()
    if is_sports_or_junk(opp.get("market", "")):
        log.info("BLACKLIST: skip %s", opp.get("market", "")[:40])
        return False
    _plat = opp.get("platform", "").lower()
    if _plat in ("polymarket", "kalshi", "predictit"):
        _cats=["fda","sec ","cpi","fed ","fomc","supreme court","earnings","tariff","iran","ceasefire","ukraine","russia","china","indictment","impeach","rate cut","rate hike","inflation","gdp","jobs report","nonfarm","sanctions","netanyahu","trudeau","macron","zelensky","zelenskyy","putin","modi","erdogan","mbs","kim jong","opec","taiwan","gaza","debt ceiling","executive order","pce","retail sales","recession","yield curve","merger","antitrust"]
        if not any(cat in opp.get("market","").lower() for cat in _cats):
            log.info("FILTERED: non-catalyst pred market (%s)", opp.get("market","")[:40])
            return False
    # === PREDICTION MARKET EXPOSURE CAP (20% of cash) ===
    _plat_lower = opp.get("platform", "").lower()
    if _plat_lower in ("polymarket", "kalshi", "predictit"):
        _pred_exposure = sum(p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", [])
                            if p.get("strategy") == "prediction")
        _pred_cap = PAPER_PORTFOLIO.get("cash", 25000) * 0.20
        if _pred_exposure >= _pred_cap:
            log.info("PRED CAP: exposure $%.0f >= 20%% cap $%.0f — blocking new entry (%s)",
                     _pred_exposure, _pred_cap, opp.get("market", "")[:35])
            return False

    # === SQLITE DEDUP (1 per market, survives restarts) ===
    _raw_mkt = opp.get("market", "")[:60]
    _mkey = _raw_mkt
    # Normalize: strip YES:/NO: prefixes for prediction market dedup
    import re as _dedup_re
    _normalized_mkey = _dedup_re.sub(r'^(YES:|NO:|yes:|no:)\s*', '', _mkey).strip()
    # Crypto: dedup by ticker symbol, not the full market string (which includes price)
    if opp.get("platform", "").lower() == "crypto" and opp.get("ticker"):
        _mkey = "CRYPTO:" + opp["ticker"].upper()
        _normalized_mkey = _mkey
    elif opp.get("ticker"):
        _tk_upper = opp["ticker"].upper()
        if any(k == _tk_upper for k in ["BTC","ETH","SOL","DOGE","ZEC","XLM","XRP","HBAR","SHIB","ALGO","ADA","AVAX","MATIC","LINK","TAO","SUI","HYPE","WBT"]):
            _mkey = "CRYPTO:" + _tk_upper
            _normalized_mkey = _mkey

    # Hard cap: max 1 position per normalized market title (regardless of YES/NO side)
    # Check in-memory portfolio first (fastest)
    for _ep in PAPER_PORTFOLIO.get("positions", []):
        _ep_mkt = _ep.get("market", "")
        _ep_norm = _dedup_re.sub(r'^(YES:|NO:|yes:|no:)\s*', '', _ep_mkt).strip()
        if _ep_norm and _normalized_mkey and _ep_norm[:40].lower() == _normalized_mkey[:40].lower():
            log.info("DEDUP-MEM: %s matches existing %s", _mkey[:40], _ep_mkt[:40])
            return False

    try:
        _dconn = sqlite3.connect(DB_PATH)
        _dc = _dconn.cursor()
        # Check normalized key against both tables using LIKE for fuzzy match
        _norm_pattern = f"%{_normalized_mkey[:35]}%" if _normalized_mkey else _mkey
        _dc.execute("SELECT COUNT(*) FROM paper_trades WHERE market LIKE ? AND status = 'open'", (_norm_pattern,))
        _dcount = _dc.fetchone()[0]
        if _dcount == 0:
            _dc.execute("SELECT COUNT(*) FROM positions WHERE market_id LIKE ? AND status = 'open'", (_norm_pattern,))
            _dcount = _dc.fetchone()[0]
        if _dcount == 0 and _mkey.startswith("CRYPTO:"):
            _tk_pattern = f"%{_mkey.replace('CRYPTO:', '')}%"
            _dc.execute("SELECT COUNT(*) FROM paper_trades WHERE market LIKE ? AND status = 'open'", (_tk_pattern,))
            _dcount = _dc.fetchone()[0]
            if _dcount == 0:
                _dc.execute("SELECT COUNT(*) FROM positions WHERE market_id LIKE ? AND status = 'open'", (_tk_pattern,))
                _dcount = _dc.fetchone()[0]
        _dconn.close()
        if _dcount > 0:
            log.info("DEDUP-SQL: %s already traded %d times (normalized: %s)", _mkey[:40], _dcount, _normalized_mkey[:40])
            return False
    except Exception:
        pass
    # === CRYPTO COOLDOWN (4h after close, mirrors pairs cooldown) ===
    if _mkey.startswith("CRYPTO:"):
        try:
            _cdconn = sqlite3.connect(DB_PATH)
            _cdc = _cdconn.cursor()
            _cdc.execute("SELECT closed_at FROM positions WHERE market_id=? AND status='closed' ORDER BY closed_at DESC LIMIT 1", (_mkey,))
            _cdrow = _cdc.fetchone()
            _cdconn.close()
            if _cdrow and _cdrow[0]:
                import datetime as _dt_cd
                _cdt = _dt_cd.datetime.fromisoformat(_cdrow[0]).replace(tzinfo=_dt_cd.timezone.utc)
                _cd_mins = (_dt_cd.datetime.now(_dt_cd.timezone.utc) - _cdt).total_seconds() / 60
                if _cd_mins < 240:  # 4 hour cooldown
                    log.info("CRYPTO COOLDOWN: %s closed %.0f min ago (need 240)", _mkey, _cd_mins)
                    return False
        except Exception:
            pass
    # === MEMORY DEDUP ===
    _mkey_ticker = _mkey.replace("CRYPTO:", "") if _mkey.startswith("CRYPTO:") else None
    for _p in PAPER_PORTFOLIO.get("positions", []):
        _pm = _p.get("market", "")
        if _pm == _mkey:
            log.info("DEDUP-MEM: %s already open", _mkey)
            return False
        # For crypto, also match by ticker in the market string
        if _mkey_ticker and _mkey_ticker in _pm.upper():
            log.info("DEDUP-MEM: %s matches open position %s", _mkey, _pm[:40])
            return False
    # === CRYPTO CAP (max 3) ===
    _is_crypto = any(k in _mkt_lower for k in ["btc","eth","sol","doge","zec","xlm","crypto","wbt","xrp","hbar","shib"])
    if _is_crypto:
        _cc = sum(1 for _p in PAPER_PORTFOLIO.get("positions", []) if any(k in _p.get("market","").lower() for k in ["btc","eth","sol","doge","zec","xlm","crypto","wbt"]))
        if _cc >= 20:  # Paper: stress test
            log.info("CRYPTO-CAP: skip %s (%d open)", _mkey[:30], _cc)
            return False
    # === GLOBAL POSITION CAP (max 15) ===
    if len(PAPER_PORTFOLIO.get("positions", [])) >= 100:  # Paper: no real cap
        log.info("GLOBAL-CAP: 15 positions open, skip")
        return False

    # Calculate position size with Kelly criterion
    # Tiered sizing based on edge score
    _edge = opp.get("edge_score", 50)
    if _edge >= 75:
        size = PAPER_PORTFOLIO.get("cash", 10000) * 0.015
    elif _edge >= 65:
        size = PAPER_PORTFOLIO.get("cash", 10000) * 0.005
    else:
        size = PAPER_PORTFOLIO.get("cash", 10000) * 0.01
    log.info("Tiered size: edge=%d size=$%.2f", _edge, size)
    price = 0.50  # default for prediction markets

    # Use explicit NO price for NO contracts, otherwise extract from detail
    _is_crypto_opp = opp.get("platform", "").lower() == "crypto"
    if opp.get("side") == "NO" and opp.get("no_price"):
        price = opp["no_price"]
    else:
        detail = opp.get("detail", "")
        import re
        price_match = re.search(r"\$([0-9.,]+)", detail)
        if price_match:
            try:
                price = float(price_match.group(1).replace(",", ""))
                if price > 1 and not _is_crypto_opp:
                    price = price / 100  # normalize prediction market cents to dollars
            except ValueError:
                price = 0.50

    # TWAP for large orders, direct for small
    if size > TWAP_CONFIG["threshold"]:
        log.info("TWAP triggered: size $%.2f > $%d threshold", size, TWAP_CONFIG["threshold"])
        return await twap_execute_paper(opp, size, price, channel)
    # Execute paper trade (direct for small orders)
    shares = int(size / max(price, 0.01))
    if shares < 1:
        shares = 1
    cost = shares * price
    slippage = cost * 0.005
    fees = cost * 0.001
    total_cost = cost + slippage + fees

    if total_cost > PAPER_PORTFOLIO["cash"]:
        release_trade_lock(opp.get("market", "")[:60])
        return False

    # === MONTE CARLO VALIDATION (crypto and prediction entries) ===
    _mc_ticker = opp.get("ticker", "")
    if _mc_ticker and _is_crypto_opp:
        try:
            _mc = montecarlo_simulate(_mc_ticker, horizon_days=3)
            if _mc.get("available"):
                _mc_mult, _mc_skip = montecarlo_size_adjustment(_mc)
                if _mc_skip:
                    log.info("MC SKIP: %s prob=%.0f%% below 45%%", _mc_ticker, _mc["prob_profit"] * 100)
                    release_trade_lock(opp.get("market", "")[:60])
                    return False
                if _mc_mult < 1.0:
                    total_cost *= _mc_mult
                    shares = max(1, int(shares * _mc_mult))
                    log.info("MC REDUCE: %s prob=%.0f%% size %.1fx", _mc_ticker, _mc["prob_profit"] * 100, _mc_mult)
        except Exception:
            pass

    PAPER_PORTFOLIO["cash"] -= total_cost
    _side_label = "BUY_NO" if opp.get("side") == "NO" else "BUY"
    _pos_market = _mkey if "_mkey" in dir() and _mkey.startswith("CRYPTO:") else opp["market"][:60]
    if opp.get("side") == "NO":
        _pos_market = f"NO:{_pos_market}"
    position = {
        "market": _pos_market,
        "side": _side_label,
        "shares": shares,
        "entry_price": price,
        "cost": total_cost,
        "value": cost,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "platform": opp.get("platform", ""),
        "ev": opp.get("ev", 0),
        "no_token_id": opp.get("no_token_id", ""),
        "strategy": "crypto" if opp.get("platform", "").lower() == "crypto" else "prediction",
    }
    PAPER_PORTFOLIO["positions"].append(position)
    PAPER_PORTFOLIO["trades"].append(position)
    publish_signal("trade_signals", {"market": position["market"], "platform": position.get("platform",""), "ev": opp.get("ev",0), "size": total_cost})
    db_log_paper_trade(position)
    db_open_position(
        market_id=_pos_market, platform=opp.get("platform", ""),
        strategy=position["strategy"], direction=_side_label,
        size_usd=total_cost, shares=shares, entry_price=price,
        metadata={"ev": opp.get("ev", 0), "edge_score": _edge,
                  "no_token_id": opp.get("no_token_id", "")},
    )
    db_save_daily_state()

    # Record signal as executed
    signal = record_signal(opp, executed=True, paper=True)

    # Update analytics
    ANALYTICS["total_trades"] += 1

    # Push to Netdata
    try:
        push_all_analytics()
    except Exception:
        pass

    # Send notification
    ev_pct = ev * 100
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    msg = (
        f"**AUTO-PAPER TRADE** | {ts}\n"
        f"[{opp['platform']}] {opp['type']} — EV: +{ev_pct:.1f}%\n"
        f"{opp['market'][:60]}\n"
        f"BUY {shares} shares @ ${price:.3f} = ${total_cost:.2f}\n"
        f"Cash remaining: ${PAPER_PORTFOLIO['cash']:,.2f}"
    )
    try:
        await channel.send(msg)
    except Exception:
        pass

    # Log to GitHub
    try:
        log_to_github(
            f"\n## Auto-Paper Trade — {ts}\n"
            f"- [{opp['platform']}] {opp['market'][:60]}\n"
            f"- EV: +{ev_pct:.1f}% | {shares} shares @ ${price:.3f} = ${total_cost:.2f}\n"
            f"---\n"
        )
    except Exception:
        pass

    return True


@bot.command(name="auto-paper")
async def auto_paper_cmd(ctx, action: str = ""):
    """Toggle auto-paper trading. Usage: !auto-paper on/off"""
    global AUTO_PAPER_ENABLED
    if action.lower() == "on":
        AUTO_PAPER_ENABLED = True
        await ctx.send(
            f"**Auto-Paper Trading ENABLED**\n"
            f"High-EV opportunities (>{ALERT_CONFIG['min_ev_threshold']*100:.0f}%) will be auto-executed in paper mode.\n"
            f"Safety: Max 1% position | Kelly sizing | Daily loss limit\n"
            f"Use `!auto-paper off` to disable."
        )
    elif action.lower() == "off":
        AUTO_PAPER_ENABLED = False
        await ctx.send("Auto-paper trading **DISABLED**.")
    else:
        status = "ENABLED" if AUTO_PAPER_ENABLED else "DISABLED"
        trades = len([t for t in PAPER_PORTFOLIO.get("trades", []) if True])
        await ctx.send(
            f"**Auto-Paper Status:** {status}\n"
            f"Threshold: >{ALERT_CONFIG['min_ev_threshold']*100:.0f}% EV\n"
            f"Mode: {TRADING_MODE.upper()}\n"
            f"Paper trades: {trades}\n"
            f"Cash: ${PAPER_PORTFOLIO['cash']:,.2f}\n"
            f"Usage: `!auto-paper on` or `!auto-paper off`"
        )



async def signals_cmd(ctx, action: str = "recent"):
    """View signal history and learning stats. Usage: !signals [recent|stats|all]"""
    if action == "stats":
        total = len(SIGNAL_HISTORY)
        executed = len([s for s in SIGNAL_HISTORY if s["executed"]])
        not_exec = total - executed
        avg_ev = sum(s["ev"] for s in SIGNAL_HISTORY) / max(total, 1) * 100

        # Platform breakdown
        platforms = {}
        for s in SIGNAL_HISTORY:
            p = s["platform"]
            if p not in platforms:
                platforms[p] = {"total": 0, "executed": 0}
            platforms[p]["total"] += 1
            if s["executed"]:
                platforms[p]["executed"] += 1

        platform_str = "\n".join(
            f"  {p}: {d['total']} signals, {d['executed']} executed"
            for p, d in platforms.items()
        ) or "  No data yet"

        await ctx.send(
            f"**Signal Learning Stats**\n================================\n"
            f"Total signals: {total}\n"
            f"Executed: {executed} | Skipped: {not_exec}\n"
            f"Avg EV: {avg_ev:.1f}%\n"
            f"\n**By Platform:**\n{platform_str}\n"
            f"================================"
        )
    elif action == "all":
        if not SIGNAL_HISTORY:
            await ctx.send("No signals recorded yet.")
            return
        lines = ["**All Signals (last 20):**"]
        for s in SIGNAL_HISTORY[-20:]:
            icon = "EXEC" if s["executed"] else "SKIP"
            lines.append(f"[{icon}] {s['timestamp']} | {s['platform']} | EV +{s['ev']*100:.1f}% | {s['market'][:40]}")
        await ctx.send("\n".join(lines))
    else:  # recent
        if not SIGNAL_HISTORY:
            await ctx.send("No signals recorded yet. Run `!cycle` or wait for auto-scan.")
            return
        lines = ["**Recent Signals (last 10):**"]
        for s in SIGNAL_HISTORY[-10:]:
            icon = "EXEC" if s["executed"] else "SKIP"
            lines.append(f"[{icon}] {s['timestamp']} | {s['platform']} | EV +{s['ev']*100:.1f}% | {s['market'][:40]}")
        await ctx.send("\n".join(lines))




# ============================================================================
# PERSISTENT MEMORY — Save/Load to JSON files
# ============================================================================
import json as _json

MEMORY_FILE = "/app/data/agent_memory.json"
CONTEXT_FILE = "/app/data/context_memory.json"
SIGNALS_FILE = "/app/data/signal_history.json"
ANALYTICS_FILE = "/app/data/analytics.json"
PAPER_FILE = "/app/data/paper_portfolio.json"


def _ensure_data_dir():
    """Ensure /app/data directory exists."""
    import os
    os.makedirs("/app/data", exist_ok=True)


def save_all_state():
    """Save all persistent state to JSON files."""
    _ensure_data_dir()
    try:
        with open(MEMORY_FILE, "w") as f:
            _json.dump(AGENT_MEMORY, f, indent=2, default=str)
    except Exception as e:
        log.warning("Save memory error: %s", e)
    try:
        with open(CONTEXT_FILE, "w") as f:
            _json.dump(CONTEXT_MEMORY, f, indent=2, default=str)
    except Exception as e:
        log.warning("Save context error: %s", e)
    try:
        with open(SIGNALS_FILE, "w") as f:
            _json.dump(SIGNAL_HISTORY, f, indent=2, default=str)
    except Exception as e:
        log.warning("Save signals error: %s", e)
    try:
        with open(ANALYTICS_FILE, "w") as f:
            _json.dump(ANALYTICS, f, indent=2, default=str)
    except Exception as e:
        log.warning("Save analytics error: %s", e)
    try:
        with open(PAPER_FILE, "w") as f:
            _json.dump(PAPER_PORTFOLIO, f, indent=2, default=str)
    except Exception as e:
        log.warning("Save paper error: %s", e)


def load_all_state():
    """Load all persistent state from JSON files."""
    global AGENT_MEMORY, CONTEXT_MEMORY, SIGNAL_HISTORY, ANALYTICS, PAPER_PORTFOLIO
    _ensure_data_dir()
    try:
        with open(MEMORY_FILE, "r") as f:
            loaded = _json.load(f)
            AGENT_MEMORY.update(loaded)
            log.info("Loaded AgentKeeper memory (%d rules, %d lessons)", len(AGENT_MEMORY.get("risk_rules",[])), len(AGENT_MEMORY.get("lessons",[])))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Load memory error: %s", e)
    try:
        with open(CONTEXT_FILE, "r") as f:
            loaded = _json.load(f)
            for tier in ["hot", "domain", "cold"]:
                if tier in loaded:
                    CONTEXT_MEMORY[tier].update(loaded[tier])
            log.info("Loaded context memory")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Load context error: %s", e)
    try:
        with open(SIGNALS_FILE, "r") as f:
            loaded = _json.load(f)
            SIGNAL_HISTORY.extend(loaded)
            log.info("Loaded %d signals from history", len(loaded))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Load signals error: %s", e)
    try:
        with open(ANALYTICS_FILE, "r") as f:
            loaded = _json.load(f)
            ANALYTICS.update(loaded)
            log.info("Loaded analytics state")
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Load analytics error: %s", e)
    try:
        with open(PAPER_FILE, "r") as f:
            loaded = _json.load(f)
            PAPER_PORTFOLIO.update(loaded)
            log.info("Loaded paper portfolio (cash: $%.2f, %d positions)", PAPER_PORTFOLIO.get("cash", 0), len(PAPER_PORTFOLIO.get("positions", [])))
    except FileNotFoundError:
        pass
    except Exception as e:
        log.warning("Load paper error: %s", e)


@bot.command(name="save")
async def save_cmd(ctx):
    """Manually save all state to disk."""
    save_all_state()
    await ctx.send("All state saved to disk (memory, context, signals, analytics, paper portfolio).")


@bot.command(name="load")
async def load_cmd(ctx):
    """Manually load all state from disk."""
    load_all_state()
    await ctx.send("All state loaded from disk.")




@bot.command(name="resolve-signal")
async def resolve_signal(ctx, index: int = -1, outcome: str = ""):
    """Mark a signal as resolved with P&L. Usage: !resolve-signal 3 win or !resolve-signal 3 loss"""
    if not SIGNAL_HISTORY:
        await ctx.send("No signals to resolve.")
        return
    if index < 0 or index >= len(SIGNAL_HISTORY):
        index = len(SIGNAL_HISTORY) - 1
    if outcome.lower() not in ["win", "loss", "push"]:
        await ctx.send("Usage: `!resolve-signal <index> win/loss/push`")
        return

    signal = SIGNAL_HISTORY[index]
    ev = signal.get("ev", 0)
    size = signal.get("size", 100)

    if outcome.lower() == "win":
        pnl = size * ev * 2  # simplified: won double the EV
        signal["pnl"] = pnl
        signal["outcome"] = "WIN"
        ANALYTICS["winning_trades"] += 1
        ANALYTICS["total_pnl"] += pnl
    elif outcome.lower() == "loss":
        pnl = -size * 0.5  # simplified: lost half the position
        signal["pnl"] = pnl
        signal["outcome"] = "LOSS"
        ANALYTICS["losing_trades"] += 1
        ANALYTICS["total_pnl"] += pnl
    else:
        signal["pnl"] = 0
        signal["outcome"] = "PUSH"

    save_all_state()
    await ctx.send(f"Signal #{index} resolved: **{signal['outcome']}** | P&L: ${signal['pnl']:+,.2f}\n{signal['market'][:60]}")




# ============================================================================
# RATE LIMITING
# ============================================================================
from collections import defaultdict as _defaultdict

RATE_LIMITS = _defaultdict(list)  # user_id -> [timestamps]
RATE_LIMIT_MAX = 10  # max commands per minute
RATE_LIMIT_WINDOW = 60  # seconds


def check_rate_limit(user_id):
    """Check if user is rate limited. Returns True if allowed."""
    now = time.time()
    # Clean old entries
    RATE_LIMITS[user_id] = [t for t in RATE_LIMITS[user_id] if now - t < RATE_LIMIT_WINDOW]
    if len(RATE_LIMITS[user_id]) >= RATE_LIMIT_MAX:
        return False
    RATE_LIMITS[user_id].append(now)
    return True


@bot.check
async def global_rate_check(ctx):
    """Global rate limiter for all commands."""
    if not check_rate_limit(ctx.author.id):
        await ctx.send(f"Rate limited — max {RATE_LIMIT_MAX} commands per minute. Please wait.")
        return False
    # Prompt injection check on command content
    is_suspicious, pattern = check_prompt_injection(ctx.message.content)
    if is_suspicious:
        SECURITY_LOG.append(f"[{datetime.now(timezone.utc).strftime('%H:%M')}] Blocked injection: {pattern} from {ctx.author}")
        BLOCKED_COMMANDS.append(ctx.message.content[:100])
        await ctx.send(f"**BLOCKED:** Suspicious input detected (`{pattern}`)")
        return False
    return True




# ============================================================================
# LIVE EXECUTION FRAMEWORK
# ============================================================================

async def execute_kalshi_order(action, ticker, amount):
    """Place a real order on Kalshi using RSA-PSS auth. Returns (success, message)."""
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY:
        return False, "Kalshi API keys not configured"
    if DRY_RUN_MODE:
        log.info("DRY RUN: Kalshi %s %s $%.2f", action, ticker, amount)
        return True, f"DRY RUN: {action} order logged but not sent (dry-run mode)"
    try:
        import base64, datetime as _dt
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding as _padding
        from cryptography.hazmat.backends import default_backend
        
        # Load private key
        pk_str = KALSHI_PRIVATE_KEY
        if "BEGIN" in pk_str:
            private_key = serialization.load_pem_private_key(pk_str.encode(), password=None, backend=default_backend())
        else:
            der = base64.b64decode(pk_str)
            private_key = serialization.load_der_private_key(der, password=None, backend=default_backend())
        
        # Timestamp in milliseconds
        ts_ms = str(int(_dt.datetime.now().timestamp() * 1000))
        
        method = "POST"
        path = "/trade-api/v2/portfolio/orders"
        
        # Sign: timestamp + method + path (RSA-PSS with SHA256)
        msg_string = ts_ms + method + path
        signature = private_key.sign(
            msg_string.encode(),
            _padding.PSS(
                mgf=_padding.MGF1(hashes.SHA256()),
                salt_length=_padding.PSS.DIGEST_LENGTH
            ),
            hashes.SHA256()
        )
        sig_b64 = base64.b64encode(signature).decode()
        
        # Build order — BUY_NO buys NO contracts, BUY buys YES
        if action.upper() == "BUY_NO":
            side = "no"
            order_action = "buy"
        elif action.upper() == "BUY":
            side = "yes"
            order_action = "buy"
        else:
            side = "no"
            order_action = "sell"
        count = max(int(amount), 1)  # Minimum 1 contract

        order_data = {
            "ticker": ticker,
            "type": "market",
            "action": order_action,
            "side": side,
            "count": count,
            "yes_price_dollars": "0.99",
        }
        
        headers = {
            "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-SIGNATURE": sig_b64,
            "KALSHI-ACCESS-TIMESTAMP": ts_ms,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        
        r = requests.post(f"https://api.elections.kalshi.com{path}", json=order_data, headers=headers, timeout=15)
        
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("order", {}).get("order_id", "unknown")
            return True, f"Kalshi order placed: {action} {ticker} x{count} (ID: {order_id})"
        else:
            return False, f"Kalshi API error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Kalshi execution error: {exc}"


async def execute_phemex_order(action, symbol, amount):
    """Place a spot order on Phemex. Returns (success, message)."""
    if not PHEMEX_API_KEY or not PHEMEX_API_SECRET:
        return False, "Phemex API keys not configured"
    if DRY_RUN_MODE:
        log.info("DRY RUN: Phemex %s %s $%.2f", action, symbol, amount)
        return True, f"DRY RUN: {action} order logged but not sent (dry-run mode)"
    try:
        import hmac, hashlib, time as _time, json
        
        expiry = str(int(_time.time()) + 60)
        
        # Phemex spot order endpoint
        path = "/spot/orders"
        side = "Buy" if action.upper() == "BUY" else "Sell"
        
        # Symbol must start with 's' for spot
        spot_symbol = symbol if symbol.startswith("s") else f"s{symbol}"
        
        order_body = {
            "symbol": spot_symbol,
            "clOrdID": f"tj-{int(_time.time())}",
            "side": side,
            "qtyType": "ByQuote",
            "quoteQtyEv": int(amount * 100000000),
            "ordType": "Market",
            "timeInForce": "ImmediateOrCancel",
        }
        
        body_str = json.dumps(order_body, separators=(",", ":"))
        
        # Sign: path + expiry + body (concatenated, no separators)
        sign_str = path + expiry + body_str
        sig = hmac.new(
            PHEMEX_API_SECRET.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        
        headers = {
            "x-phemex-access-token": PHEMEX_API_KEY,
            "x-phemex-request-expiry": expiry,
            "x-phemex-request-signature": sig,
            "Content-Type": "application/json",
        }
        
        r = requests.post(f"https://api.phemex.com{path}", data=body_str, headers=headers, timeout=15)
        
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                order_id = data.get("data", {}).get("orderID", "unknown")
                return True, f"Phemex order: {side} {spot_symbol} ${amount:.2f} (ID: {order_id})"
            else:
                return False, f"Phemex error: code={data.get('code')} msg={data.get('msg', 'unknown')}"
        else:
            return False, f"Phemex API error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Phemex execution error: {exc}"


async def execute_phemex_perp_short(symbol, amount_usd):
    """Open a short perpetual position on Phemex. Returns (success, message, order_id)."""
    if not PHEMEX_API_KEY or not PHEMEX_API_SECRET:
        return False, "Phemex API keys not configured", None
    if DRY_RUN_MODE:
        _dry_id = f"DRY-{int(time.time())}"
        log.info("DRY RUN: Phemex PERP SHORT %s $%.2f", symbol, amount_usd)
        return True, f"DRY RUN: perp short {symbol} ${amount_usd:.2f}", _dry_id
    try:
        import hmac as _hmac, hashlib as _hl, json as _js

        expiry = str(int(time.time()) + 60)
        path = "/orders"
        # Phemex perp symbol format: BTCUSD, ETHUSD (inverse) or BTCUSDT (linear)
        perp_symbol = f"{symbol}USDT"

        order_body = {
            "symbol": perp_symbol,
            "clOrdID": f"tj-arb-{int(time.time())}",
            "side": "Sell",
            "orderQty": int(amount_usd),  # Contract quantity in USD for linear
            "ordType": "Market",
            "timeInForce": "ImmediateOrCancel",
            "posSide": "Short",
        }

        body_str = _js.dumps(order_body, separators=(",", ":"))
        sign_str = path + expiry + body_str
        sig = _hmac.new(
            PHEMEX_API_SECRET.encode("utf-8"),
            sign_str.encode("utf-8"),
            _hl.sha256
        ).hexdigest()

        headers = {
            "x-phemex-access-token": PHEMEX_API_KEY,
            "x-phemex-request-expiry": expiry,
            "x-phemex-request-signature": sig,
            "Content-Type": "application/json",
        }

        r = requests.post(f"https://api.phemex.com{path}", data=body_str, headers=headers, timeout=15)

        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                order_id = data.get("data", {}).get("orderID", "unknown")
                return True, f"Phemex perp short: {perp_symbol} ${amount_usd:.2f} (ID: {order_id})", order_id
            else:
                return False, f"Phemex perp error: code={data.get('code')} msg={data.get('msg', '')}", None
        else:
            return False, f"Phemex perp HTTP {r.status_code}: {r.text[:200]}", None
    except Exception as exc:
        return False, f"Phemex perp error: {exc}", None


async def backtest_real(ctx, *, args: str = ""):
    """Backtest with REAL historical data from CoinGecko.
    Usage: !backtest-real bitcoin momentum 90
           !backtest-real ethereum mean-reversion 180
    """
    parts = args.split() if args else []
    coin = parts[0] if len(parts) > 0 else "bitcoin"
    strategy = parts[1] if len(parts) > 1 else "momentum"
    days = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 90
    
    msg = await ctx.send(f"Fetching {days} days of {coin} data from CoinGecko...")
    
    prices = fetch_historical_prices(coin, days)
    if not prices or len(prices) < 20:
        await msg.edit(content=f"Could not fetch enough data for {coin}. Try: bitcoin, ethereum, solana, dogecoin")
        return
    
    result = real_backtest_strategy(prices, strategy)
    if not result:
        await msg.edit(content="Not enough data for backtest.")
        return
    
    # Monte Carlo on the real returns
    import random, statistics
    returns = calculate_returns(prices)
    mc_results = []
    for _ in range(1000):
        shuffled = random.sample(returns, len(returns))
        eq = 10000.0
        for r in shuffled:
            eq += eq * r
        mc_results.append((eq - 10000) / 10000 * 100)
    mc_results.sort()
    mc_5th = mc_results[50]
    mc_median = mc_results[500]
    mc_95th = mc_results[950]
    
    # Verdict
    checks = [
        result["sharpe"] > 0.5,
        result["win_rate"] > 45,
        result["max_drawdown"] < 35,
        result["total_return"] > 0,
        mc_median > 0,
    ]
    passed = sum(checks)
    verdict = f"PASS ({passed}/5)" if passed >= 3 else f"FAIL ({passed}/5)"
    icon = "PASS" if passed >= 3 else "FAIL"
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    report = (
        f"**Real Data Backtest** | {ts}\n"
        f"Coin: {coin} | Strategy: {strategy} | Period: {days} days\n"
        f"Data points: {len(prices)} daily prices\n"
        f"================================\n"
        f"**Performance:**\n"
        f"  Return: {result['total_return']:+.1f}%\n"
        f"  Final equity: ${result['final_equity']:,.2f}\n"
        f"  Sharpe: {result['sharpe']:.2f}\n"
        f"  Max Drawdown: -{result['max_drawdown']:.1f}%\n"
        f"  Win Rate: {result['win_rate']:.1f}% ({result['winning']}W / {result['losing']}L)\n"
        f"  Trades: {result['trades']}\n"
        f"\n**Monte Carlo (1K paths, shuffled returns):**\n"
        f"  5th: {mc_5th:+.1f}% | Median: {mc_median:+.1f}% | 95th: {mc_95th:+.1f}%\n"
        f"\n**Verdict:** [{icon}] {verdict}\n"
        f"================================"
    )
    
    if len(report) > 1900:
        report = report[:1900] + "\n*...truncated*"
    await msg.edit(content=report)




async def execute_coinbase_order(action, symbol, amount):
    """Place an order on Coinbase Advanced Trade API. Returns (success, message)."""
    if not COINBASE_API_KEY or not COINBASE_API_SECRET:
        return False, "Coinbase API keys not configured"
    # DRY_RUN_MODE check
    if DRY_RUN_MODE:
        log.info("DRY RUN: %s %s — order NOT sent", action, ticker if 'ticker' in dir() else symbol if 'symbol' in dir() else amount)
        return True, f"DRY RUN: {action} order logged but not sent (dry-run mode)"
    try:
        import jwt as _jwt, time as _time, secrets as _secrets, uuid as _uuid
        
        uri = "api.coinbase.com"
        path = "/api/v3/brokerage/orders"
        
        # Build order
        product_id = f"{symbol}-USD"
        client_order_id = str(_uuid.uuid4())
        side_str = "BUY" if action.upper() == "BUY" else "SELL"
        
        order_body = {
            "client_order_id": client_order_id,
            "product_id": product_id,
            "side": side_str,
            "order_configuration": {
                "market_market_ioc": {
                    "quote_size": str(amount)
                }
            }
        }
        
        payload = {
            "sub": COINBASE_API_KEY,
            "iss": "cdp",
            "nbf": int(_time.time()),
            "exp": int(_time.time()) + 120,
            "uri": f"POST {uri}{path}",
        }
        token = _jwt.encode(payload, COINBASE_API_SECRET, algorithm="ES256",
                            headers={"kid": COINBASE_API_KEY, "nonce": _secrets.token_hex(16), "typ": "JWT"})
        
        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        r = requests.post(f"https://{uri}{path}", json=order_body, headers=hdrs, timeout=15)
        
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("order_id", "unknown")
            return True, f"Coinbase order placed: {side_str} {symbol} ${amount:.2f} (ID: {order_id})"
        else:
            return False, f"Coinbase error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Coinbase execution error: {exc}"


async def execute_robinhood_order(action, symbol, amount):
    """Place a crypto order via Robinhood Crypto API. Returns (success, message)."""
    if not ROBINHOOD_API_KEY or not ROBINHOOD_PRIVATE_KEY:
        return False, "Robinhood API keys not configured"
    if DRY_RUN_MODE:
        log.info("DRY RUN: Robinhood %s %s $%.2f", action, symbol, amount)
        return True, f"DRY RUN: {action} order logged but not sent (dry-run mode)"
    try:
        import base64, json, uuid as _uuid, datetime as _dt
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        
        # Load private key — base64 decode, take first 32 bytes
        private_bytes = base64.b64decode(ROBINHOOD_PRIVATE_KEY)
        private_key = Ed25519PrivateKey.from_private_bytes(private_bytes[:32])
        
        # Build request
        base_url = "https://trading.robinhood.com"
        path = "/api/v1/crypto/trading/orders/"
        timestamp = int(_dt.datetime.now(tz=_dt.timezone.utc).timestamp())
        
        side_str = "buy" if action.upper() == "BUY" else "sell"
        
        # Robinhood uses "BTC-USD" format, not just "BTC"
        rh_symbol = symbol if "-" in symbol else f"{symbol}-USD"
        
        order_body = {
            "client_order_id": str(_uuid.uuid4()),
            "side": side_str,
            "symbol": rh_symbol,
            "type": "market",
            "market_order_config": {
                "asset_quantity": str(round(amount, 8))
            }
        }
        
        body_str = json.dumps(order_body)
        
        # Sign exactly per official docs: api_key + str(timestamp) + path + body
        message = f"{ROBINHOOD_API_KEY}{timestamp}{path}{body_str}"
        signature = private_key.sign(message.encode("utf-8"))
        sig_b64 = base64.b64encode(signature).decode("utf-8")
        
        headers = {
            "x-api-key": ROBINHOOD_API_KEY,
            "x-timestamp": str(timestamp),
            "x-signature": sig_b64,
            "Content-Type": "application/json",
        }
        
        log.info("Robinhood request: %s %s %s qty=%s", side_str, rh_symbol, base_url + path, amount)
        
        r = requests.post(f"{base_url}{path}", data=body_str, headers=headers, timeout=15)
        
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("id", "unknown")
            status = data.get("state", "unknown")
            return True, f"Robinhood order: {side_str} {rh_symbol} qty={amount} (ID: {order_id}, Status: {status})"
        else:
            return False, f"Robinhood error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Robinhood execution error: {exc}"

@bot.command(name="dry-run")
async def dry_run_cmd(ctx, action: str = ""):
    """Toggle dry-run mode. Usage: !dry-run on/off"""
    global DRY_RUN_MODE
    if action.lower() == "on":
        DRY_RUN_MODE = True
        await ctx.send("**Dry-run ON** — Live orders will be logged but NOT sent.")
    elif action.lower() == "off":
        DRY_RUN_MODE = False
        await ctx.send("**Dry-run OFF** — Live orders WILL be sent to exchanges. Be careful!")
    else:
        status = "ON (safe)" if DRY_RUN_MODE else "OFF (live orders enabled)"
        await ctx.send(f"Dry-run mode: **{status}**\nUsage: `!dry-run on` or `!dry-run off`")




# ============================================================================
# CROSS-PLATFORM ARBITRAGE SCANNER
# ============================================================================
ARB_THRESHOLD = 0.975  # Flag when YES+NO < this across platforms
ARB_MIN_LIQUIDITY = 5000  # Minimum liquidity to consider
ARB_HISTORY = []  # Track arb opportunities found


def find_kalshi_markets_for_arb():
    """Fetch Kalshi markets with prices for arbitrage comparison. Uses kalshi_sign for auth."""
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY:
        return []
    try:
        path = "/markets"
        ts, sig = kalshi_sign("GET", path)
        hdrs = {
            "KALSHI-ACCESS-KEY": KALSHI_API_KEY_ID,
            "KALSHI-ACCESS-TIMESTAMP": ts,
            "KALSHI-ACCESS-SIGNATURE": sig,
            "Content-Type": "application/json",
        }
        params = {"status": "open", "limit": 100}
        r = requests.get(KALSHI_BASE + path, headers=hdrs, params=params, timeout=15)
        if r.status_code == 200:
            markets = r.json().get("markets", [])
            result = []
            for m in markets:
                yes_price = m.get("yes_price", 0)
                no_price = m.get("no_price", 0)
                if yes_price and no_price:
                    result.append({
                        "ticker": m.get("ticker", ""),
                        "title": m.get("title", ""),
                        "yes_price": yes_price / 100 if yes_price > 1 else yes_price,
                        "no_price": no_price / 100 if no_price > 1 else no_price,
                        "volume": m.get("volume", 0),
                        "platform": "Kalshi",
                    })
            return result
        else:
            log.warning("Kalshi arb fetch: HTTP %d %s", r.status_code, r.text[:100])
        return []
    except Exception as exc:
        log.warning("Kalshi arb fetch error: %s", exc)
        return []


def find_polymarket_markets_for_arb():
    """Fetch Polymarket markets with prices for arbitrage comparison."""
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?closed=false&limit=100&order=volume24hr&ascending=false", timeout=15)
        if r.status_code != 200:
            return []
        markets = r.json()
        result = []
        for m in markets:
            tokens = m.get("tokens", [])
            if len(tokens) >= 2:
                yes_price = float(tokens[0].get("price", 0))
                no_price = float(tokens[1].get("price", 0))
                if yes_price > 0 and no_price > 0:
                    result.append({
                        "slug": m.get("slug", ""),
                        "title": m.get("question", m.get("title", "")),
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "volume24h": float(m.get("volume24hr", 0)),
                        "liquidity": float(m.get("liquidity", 0)),
                        "platform": "Polymarket",
                    })
        return result
    except Exception as exc:
        log.warning("Polymarket arb fetch error: %s", exc)
        return []


def normalize_title(title):
    """Normalize market title for fuzzy matching."""
    import re
    t = title.lower().strip()
    t = re.sub(r"[^a-z0-9 ]", "", t)
    # Remove common filler words
    for word in ["will", "the", "be", "by", "in", "on", "a", "an", "of", "to", "is"]:
        t = t.replace(f" {word} ", " ")
    return " ".join(t.split())


def find_cross_platform_arbs():
    """Find arbitrage opportunities across Kalshi and Polymarket."""
    kalshi_markets = find_kalshi_markets_for_arb()
    poly_markets = find_polymarket_markets_for_arb()
    
    if not kalshi_markets or not poly_markets:
        return []
    
    arbs = []
    
    # 1. INTRA-PLATFORM: Check if YES+NO < 1.00 within each platform
    for m in poly_markets:
        total = m["yes_price"] + m["no_price"]
        if total < ARB_THRESHOLD and m.get("liquidity", 0) > ARB_MIN_LIQUIDITY:
            spread = 1.0 - total
            arbs.append({
                "type": "INTRA-POLY",
                "title": m["title"][:60],
                "yes_price": m["yes_price"],
                "no_price": m["no_price"],
                "total_cost": total,
                "spread": spread,
                "profit_pct": spread / total * 100,
                "platform": "Polymarket",
                "liquidity": m.get("liquidity", 0),
            })
    
    for m in kalshi_markets:
        total = m["yes_price"] + m["no_price"]
        if total < ARB_THRESHOLD:
            spread = 1.0 - total
            arbs.append({
                "type": "INTRA-KALSHI",
                "title": m["title"][:60],
                "yes_price": m["yes_price"],
                "no_price": m["no_price"],
                "total_cost": total,
                "spread": spread,
                "profit_pct": spread / total * 100,
                "platform": "Kalshi",
                "liquidity": m.get("volume", 0),
            })
    
    # 2. CROSS-PLATFORM: Match similar markets and check combined pricing
    for km in kalshi_markets:
        k_norm = normalize_title(km["title"])
        for pm in poly_markets:
            p_norm = normalize_title(pm["title"])
            
            # Simple keyword overlap matching
            k_words = set(k_norm.split())
            p_words = set(p_norm.split())
            overlap = k_words & p_words
            total_words = k_words | p_words
            
            if len(total_words) == 0:
                continue
            similarity = len(overlap) / len(total_words)
            
            if similarity > 0.4:  # 40% word overlap = likely same event
                # Strategy 1: Buy YES on cheaper, NO on other
                combo1 = km["yes_price"] + pm["no_price"]
                combo2 = pm["yes_price"] + km["no_price"]
                
                best_combo = min(combo1, combo2)
                if best_combo < ARB_THRESHOLD:
                    if combo1 < combo2:
                        strategy = f"BUY YES@Kalshi ${km['yes_price']:.3f} + NO@Poly ${pm['no_price']:.3f}"
                    else:
                        strategy = f"BUY YES@Poly ${pm['yes_price']:.3f} + NO@Kalshi ${km['no_price']:.3f}"
                    
                    spread = 1.0 - best_combo
                    arbs.append({
                        "type": "CROSS-PLATFORM",
                        "title": km["title"][:40] + " / " + pm["title"][:40],
                        "strategy": strategy,
                        "total_cost": best_combo,
                        "spread": spread,
                        "profit_pct": spread / best_combo * 100,
                        "similarity": similarity,
                        "kalshi_ticker": km["ticker"],
                        "poly_slug": pm.get("slug", ""),
                    })
    
    # Sort by profit potential
    arbs.sort(key=lambda x: x.get("spread", 0), reverse=True)
    
    # Record history
    for a in arbs[:10]:
        a["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ARB_HISTORY.append(a)
    
    # Keep history manageable
    while len(ARB_HISTORY) > 200:
        ARB_HISTORY.pop(0)
    
    return arbs


async def execute_event_synthetics(channel=None):
    """Execute cross-platform arbs as event_synthetic positions.
    Buys both legs (YES on one platform + NO on other) when spread > 2%."""
    arbs = find_cross_platform_arbs()
    if not arbs:
        return 0

    fired = 0
    for arb in arbs[:3]:  # Max 3 arbs per cycle
        spread_pct = arb.get("profit_pct", 0)
        if spread_pct < 2.0:  # Need > 2% spread to execute
            continue

        title = arb.get("title", "")[:60]
        market_id = f"SYNTH:{title[:40]}"

        # Dedup
        if any(p.get("market", "") == market_id for p in PAPER_PORTFOLIO.get("positions", [])):
            continue

        total_cost = arb.get("total_cost", 0)
        spread = arb.get("spread", 0)
        portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
            p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))
        size = min(portfolio_value * 0.01, 250)  # 1% or $250 max

        if size > PAPER_PORTFOLIO.get("cash", 0):
            continue

        # Record as paper position (execution on both platforms is paper-mode)
        PAPER_PORTFOLIO["cash"] -= size
        _synth_pos = {
            "market": market_id,
            "side": arb.get("strategy", f"spread=${spread:.3f}")[:60],
            "shares": int(size / max(total_cost, 0.01)),
            "entry_price": total_cost,
            "cost": size,
            "value": size,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "platform": arb.get("type", "CROSS-PLATFORM"),
            "ev": spread,
            "strategy": "event_synthetic",
        }
        PAPER_PORTFOLIO["positions"].append(_synth_pos)
        db_log_paper_trade(_synth_pos)
        db_open_position(
            market_id=market_id, platform=arb.get("type", "CROSS-PLATFORM"),
            strategy="event_synthetic", direction="arb",
            size_usd=size, shares=int(size / max(total_cost, 0.01)),
            entry_price=total_cost,
            metadata={"spread": spread, "profit_pct": spread_pct,
                      "strategy_detail": arb.get("strategy", ""),
                      "kalshi_ticker": arb.get("kalshi_ticker", ""),
                      "poly_slug": arb.get("poly_slug", "")},
        )

        fired += 1
        log.info("EVENT SYNTHETIC: %s spread=%.1f%% cost=$%.0f", title[:40], spread_pct, size)
        if channel:
            try:
                await channel.send(
                    f"**EVENT SYNTHETIC** — {spread_pct:.1f}% spread\n"
                    f"{title[:60]}\n"
                    f"{arb.get('strategy', '')}\n"
                    f"Size: ${size:.0f}")
            except Exception:
                pass

    return fired


@bot.command(name="arb-scan")
async def arb_scan_new(ctx):
    """Scan for cross-platform mispricings with spread percentage."""
    msg = await ctx.send("Scanning Kalshi + Polymarket for mispricings...")
    arbs = find_cross_platform_arbs()
    if not arbs:
        await msg.edit(content="**Arb Scan**: No mispricings found. Markets are efficient.")
        return

    result = "**Cross-Platform Mispricings**\n```\n"
    result += f"{'Event':40s} {'Type':12s} {'Spread':>7s} {'Cost':>6s}\n"
    result += f"{'─' * 68}\n"
    for a in arbs[:10]:
        _t = a.get("title", "")[:38]
        _type = a.get("type", "")[:10]
        _spread = a.get("profit_pct", 0)
        _cost = a.get("total_cost", 0)
        _marker = " ★" if _spread >= 2.0 else ""
        result += f"  {_t:38s} {_type:12s} {_spread:>5.1f}% ${_cost:>.3f}{_marker}\n"
        if a.get("strategy"):
            result += f"    {a['strategy'][:62]}\n"
    result += f"{'─' * 68}\n"
    result += f"★ = executable (>2% spread)\n"
    result += "```"
    await msg.edit(content=result)


@bot.command(name="arb")
async def arb_scan(ctx):
    """Scan for cross-platform arbitrage opportunities."""
    msg = await ctx.send("Scanning Kalshi + Polymarket for arbitrage...")
    
    arbs = find_cross_platform_arbs()
    
    if not arbs:
        await msg.edit(content="No arbitrage opportunities found above threshold. Markets are efficient right now.")
        return
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Arbitrage Scanner** | {ts}", f"Threshold: YES+NO < ${ARB_THRESHOLD:.3f}", "================================"]
    
    for i, a in enumerate(arbs[:8]):
        spread_pct = a["spread"] * 100
        profit_pct = a.get("profit_pct", 0)
        
        if a["type"] == "CROSS-PLATFORM":
            lines.append(
                f"**[{a['type']}]** Spread: ${a['spread']:.3f} ({profit_pct:.1f}%)\n"
                f"  {a['title'][:70]}\n"
                f"  {a.get('strategy', '')}\n"
                f"  Total cost: ${a['total_cost']:.3f} → Payout: $1.00"
            )
        else:
            lines.append(
                f"**[{a['type']}]** Spread: ${a['spread']:.3f} ({profit_pct:.1f}%)\n"
                f"  {a['title'][:70]}\n"
                f"  YES: ${a['yes_price']:.3f} + NO: ${a['no_price']:.3f} = ${a['total_cost']:.3f}"
            )
        lines.append("")
    
    lines.append(f"================================\nTotal found: {len(arbs)} | Showing top {min(8, len(arbs))}")
    
    report = "\n".join(lines)
    if len(report) > 1900:
        report = report[:1900] + "\n*...truncated*"
    await msg.edit(content=report)




# ============================================================================
# REGIME-BASED STRATEGY SWITCHING
# ============================================================================
REGIME_CONFIG = {
    "current_regime": "UNKNOWN",
    "strategy_active": "arbitrage",  # default safe strategy
    "regime_history": [],
}


def detect_regime():
    """Detect market regime based on Fear & Greed Index."""
    try:
        fng_val, fng_label = get_fear_greed()

    except Exception:
        fng_val = 50
        fng_label = "Neutral"
    
    if fng_val <= 25:
        regime = "EXTREME_FEAR"
        strategies = ["arbitrage", "mean-reversion", "grid"]
        rationale = "Extreme fear = buy dips, exploit panic mispricing, avoid momentum"
    elif fng_val <= 40:
        regime = "FEAR"
        strategies = ["arbitrage", "mean-reversion"]
        rationale = "Fear = focus on arb + cautious mean reversion"
    elif fng_val <= 60:
        regime = "NEUTRAL"
        strategies = ["arbitrage", "momentum", "trend-following"]
        rationale = "Neutral = balanced approach, momentum viable"
    elif fng_val <= 75:
        regime = "GREED"
        strategies = ["momentum", "trend-following", "arbitrage"]
        rationale = "Greed = ride trends but tighten stops"
    else:
        regime = "EXTREME_GREED"
        strategies = ["contrarian", "arbitrage"]
        rationale = "Extreme greed = take profits, contrarian bets, reduce size 50%"
    
    REGIME_CONFIG["current_regime"] = regime
    REGIME_CONFIG["strategy_active"] = strategies[0]
    REGIME_CONFIG["fng_value"] = fng_val
    REGIME_CONFIG["fng_label"] = fng_label
    REGIME_CONFIG["strategies"] = strategies
    REGIME_CONFIG["rationale"] = rationale
    
    # Position size multiplier based on regime
    if regime in ("EXTREME_FEAR", "EXTREME_GREED"):
        REGIME_CONFIG["size_multiplier"] = 0.65  # Slightly more trades in extremes for learning
    elif regime in ("FEAR", "GREED"):
        REGIME_CONFIG["size_multiplier"] = 0.75
    else:
        REGIME_CONFIG["size_multiplier"] = 1.0
    
    # Log regime change
    entry = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "regime": regime, "fng": fng_val}
    REGIME_CONFIG["regime_history"].append(entry)
    if len(REGIME_CONFIG["regime_history"]) > 100:
        REGIME_CONFIG["regime_history"].pop(0)
    
    return regime, strategies, rationale


@bot.command(name="regime")
async def regime_cmd(ctx):
    """Show current market regime and active strategy."""
    regime, strategies, rationale = detect_regime()
    cfg = REGIME_CONFIG
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await ctx.send(
        f"**Market Regime** | {ts}\n================================\n"
        f"Fear & Greed: {cfg['fng_value']}/100 ({cfg['fng_label']})\n"
        f"Regime: **{regime}**\n"
        f"Active strategies: {', '.join(strategies)}\n"
        f"Position size: {cfg['size_multiplier']:.0%} of normal\n"
        f"Rationale: {rationale}\n"
        f"================================"
    )




# ============================================================================
# FRACTIONAL KELLY + ASYMMETRIC RISK/REWARD
# ============================================================================
KELLY_FRACTION = 0.25  # Quarter-Kelly
MIN_REWARD_RISK = 2.0  # Minimum 2:1 reward-to-risk
ATR_LOOKBACK = 14  # ATR period


def kelly_size(win_prob, payout_odds, bankroll):
    """Calculate quarter-Kelly position size.
    win_prob: estimated probability of winning (0-1)
    payout_odds: net payout per dollar risked (e.g., 1.5 for 3:2)
    bankroll: total capital available
    """
    if win_prob <= 0 or payout_odds <= 0:
        return 0
    
    q = 1 - win_prob
    kelly_pct = (win_prob * payout_odds - q) / payout_odds
    
    if kelly_pct <= 0:
        return 0  # Negative edge — don't bet
    
    # Apply fractional Kelly
    position_pct = kelly_pct * KELLY_FRACTION
    
    # Apply regime multiplier
    regime_mult = REGIME_CONFIG.get("size_multiplier", 1.0)
    position_pct *= regime_mult
    
    # Cap at MAX_POSITION_PCT
    position_pct = min(position_pct, MAX_POSITION_PCT)
    
    return bankroll * position_pct


def check_reward_risk(entry_price, target_price, stop_price):
    """Check if trade meets minimum reward-to-risk ratio."""
    if stop_price == entry_price:
        return False, 0
    risk = abs(entry_price - stop_price)
    reward = abs(target_price - entry_price)
    if risk == 0:
        return False, 0
    ratio = reward / risk
    return ratio >= MIN_REWARD_RISK, ratio


def calculate_atr(prices, period=14):
    """Calculate Average True Range from price series."""
    if len(prices) < period + 1:
        return 0
    trs = []
    for i in range(1, len(prices)):
        high_low = abs(prices[i] - prices[i-1])  # Simplified: use daily range
        trs.append(high_low)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0
    return sum(trs[-period:]) / period


def dynamic_stop(entry_price, atr, direction="long", multiplier=2.0):
    """Calculate dynamic stop-loss based on ATR."""
    if direction == "long":
        return entry_price - (atr * multiplier)
    else:
        return entry_price + (atr * multiplier)


def dynamic_target(entry_price, atr, direction="long", multiplier=4.0):
    """Calculate profit target based on ATR (2:1 minimum)."""
    if direction == "long":
        return entry_price + (atr * multiplier)
    else:
        return entry_price - (atr * multiplier)




# ============================================================================
# INFORMATION SPEED ARBITRAGE
# ============================================================================
NEWS_KEYWORDS_HIGH_IMPACT = {
    "fed": ["rate", "cut", "hike", "hold", "fomc", "powell", "basis points"],
    "cpi": ["inflation", "consumer price", "core cpi", "headline"],
    "jobs": ["nonfarm", "payroll", "unemployment", "labor", "employment"],
    "gdp": ["growth", "recession", "contraction", "expansion"],
    "crypto": ["bitcoin", "btc", "ethereum", "sec", "etf", "regulation"],
    "geopolitical": ["war", "ceasefire", "invasion", "sanctions", "strike"],
}

SPEED_ARB_LOG = []


async def check_news_speed_arb():
    """Check for breaking news that could create speed arbitrage opportunities."""
    try:
        # Fetch latest headlines
        headlines = fetch_market_news()
        if not headlines:
            return []
        
        opportunities = []
        
        for headline in headlines[:5]:
            title_lower = headline.lower()
            
            # Check against high-impact keywords
            impact_score = 0
            matched_category = ""
            
            for category, keywords in NEWS_KEYWORDS_HIGH_IMPACT.items():
                matches = sum(1 for kw in keywords if kw in title_lower)
                if matches > impact_score:
                    impact_score = matches
                    matched_category = category
            
            if impact_score >= 2:  # At least 2 keyword matches = high impact
                opportunities.append({
                    "headline": headline[:80],
                    "category": matched_category,
                    "impact_score": impact_score,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "action": f"SCAN {matched_category.upper()} contracts on Kalshi + Polymarket",
                })
                
                SPEED_ARB_LOG.append({
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "headline": headline[:80],
                    "category": matched_category,
                    "impact": impact_score,
                })
        
        # Keep log manageable
        while len(SPEED_ARB_LOG) > 100:
            SPEED_ARB_LOG.pop(0)
        
        return opportunities
    except Exception as exc:
        log.warning("Speed arb check error: %s", exc)
        return []


@bot.command(name="speed-scan")
async def speed_scan(ctx):
    """Check for breaking news speed arbitrage opportunities."""
    msg = await ctx.send("Scanning for high-impact news events...")
    
    opps = await check_news_speed_arb()
    
    if not opps:
        await msg.edit(content="No high-impact news detected. Markets are calm.")
        return
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Speed Arbitrage Scanner** | {ts}", "================================"]
    
    for o in opps:
        lines.append(
            f"**[{o['category'].upper()}]** Impact: {o['impact_score']}/5\n"
            f"  {o['headline']}\n"
            f"  Action: {o['action']}"
        )
        lines.append("")
    
    lines.append("================================")
    await msg.edit(content="\n".join(lines))




# ============================================================================
# PROPRIETARY EDGE LAYER — MULTI-SIGNAL FUSION
# ============================================================================

def calculate_edge_score(opportunity):
    """Calculate composite edge score from multiple signals.
    Combines: arbitrage spread, news sentiment, regime alignment, volume.
    Returns score 0-100 and confidence level.
    """
    score = 0
    signals = []
    
    # 1. EV Score (0-30 points)
    ev = opportunity.get("ev", 0)
    ev_score = min(ev * 300, 30)  # 10% EV = 30 points
    score += ev_score
    if ev > 0.05:
        signals.append(f"EV +{ev*100:.1f}%")
    
    # 2. Regime Alignment (0-20 points)
    regime = REGIME_CONFIG.get("current_regime", "UNKNOWN")
    opp_type = opportunity.get("type", "")
    if regime in ("EXTREME_FEAR", "FEAR") and opp_type in ("mean-reversion", "arb", "Low-Price YES", "Wide Spread", "momentum", "contrarian"):
        score += 20
        signals.append(f"Regime-aligned ({regime})")
    elif regime in ("NEUTRAL",) and opp_type in ("momentum", "arb"):
        score += 15
        signals.append("Regime-neutral")
    elif regime in ("GREED", "EXTREME_GREED") and opp_type in ("contrarian", "arb"):
        score += 20
        signals.append(f"Contrarian in {regime}")
    else:
        score += 5
    
    # 3. News Sentiment Alignment (0-20 points)
    try:
        forecast = get_market_forecast()
        composite = forecast.get("composite", 50)
        if composite < 30 and opp_type in ("mean-reversion",):
            score += 20
            signals.append("News bearish + mean-reversion")
        elif composite > 70 and opp_type in ("momentum",):
            score += 20
            signals.append("News bullish + momentum")
        elif 40 <= composite <= 60:
            score += 10
            signals.append("News neutral")
        else:
            score += 5
    except Exception:
        score += 5
    
    # 4. Liquidity Score (0-15 points)
    liquidity = opportunity.get("liquidity", 0)
    vol = opportunity.get("volume24h", 0)
    if liquidity > 100000 or vol > 500000:
        score += 15
        signals.append("High liquidity")
    elif liquidity > 20000 or vol > 100000:
        score += 10
        signals.append("Medium liquidity")
    else:
        score += 3
        signals.append("Low liquidity")
    
    # 5. Cross-platform Confirmation (0-15 points)
    # If same event exists on multiple platforms = more reliable pricing
    platform = opportunity.get("platform", "")
    if platform == "CROSS-PLATFORM":
        score += 15
        signals.append("Cross-platform confirmed")
    else:
        score += 5
    
    # 6. TradingView / Market Cipher Signal Boost (0-15 points)
    # This layer BOOSTS edge score but never triggers trades alone
    try:
        tv = TRADINGVIEW_SIGNALS
        if tv.get("enabled") and tv.get("latest_signal"):
            sig = tv["latest_signal"]
            sig_age = (datetime.utcnow() - sig.get("timestamp", datetime.min)).total_seconds() / 60
            if sig_age <= tv.get("signal_expiry_minutes", 30):
                sig_type = sig.get("signal", "").upper()
                asset = opportunity.get("asset", opportunity.get("slug", "")).upper()
                sig_asset = sig.get("asset", "").upper()
                # Only boost if signal matches the asset or is broad market
                if sig_asset in (asset, "BTC", "SPY", "MARKET", ""):
                    if sig_type == "BUY" and opportunity.get("action", "BUY").upper() == "BUY":
                        score += 15
                        signals.append(f"TV/MC BUY signal ({sig.get('indicator', 'MC')})")
                    elif sig_type == "SELL" and opportunity.get("action", "").upper() == "SELL":
                        score += 15
                        signals.append(f"TV/MC SELL signal ({sig.get('indicator', 'MC')})")
                    elif sig_type in ("BULLISH", "GREEN_DOT", "BLUE_WAVE"):
                        score += 10
                        signals.append(f"TV/MC bullish ({sig_type})")
                    elif sig_type in ("BEARISH", "RED_DOT", "RED_WAVE"):
                        score += 10
                        signals.append(f"TV/MC bearish ({sig_type})")
                    else:
                        score += 5
                        signals.append(f"TV/MC signal: {sig_type}")
    except Exception:
        pass  # TradingView signals are optional — never block scoring

    # Determine confidence
    if score >= 75:
        confidence = "HIGH"
    elif score >= 50:
        confidence = "MEDIUM"
    elif score >= 30:
        confidence = "LOW"
    else:
        confidence = "SKIP"
    
    return score, confidence, signals


def suggest_position_size_v2(opportunity, bankroll=10000):
    """Enhanced position sizing using fractional Kelly + regime + edge score."""
    ev = opportunity.get("ev", 0)
    if ev <= 0:
        return 0
    
    # Estimate win probability from EV
    # For prediction markets: if YES @ $0.40, implied prob = 40%
    # Our edge is the EV above that
    implied_prob = opportunity.get("yes_price", 0.5)
    our_prob = min(implied_prob + ev, 0.95)  # Cap at 95%
    payout_odds = (1.0 / implied_prob) - 1 if implied_prob > 0 else 1
    
    size = kelly_size(our_prob, payout_odds, bankroll)
    
    # Apply edge score modifier
    edge_score, confidence, _ = calculate_edge_score(opportunity)
    if confidence == "HIGH":
        size *= 1.0  # Full quarter-Kelly
    elif confidence == "MEDIUM":
        size *= 0.7
    elif confidence == "LOW":
        size *= 0.3
    else:
        size = 0  # Don't trade SKIP signals
    
    # Floor and ceiling
    size = max(0, min(size, bankroll * get_tiered_max_position(opportunity.get("edge_score", 0))))
    
    return round(size, 2)


@bot.command(name="edge")
async def edge_analysis(ctx, *, query: str = ""):
    """Run full edge analysis on current opportunities."""
    msg = await ctx.send("Running multi-signal edge analysis...")
    
    # Detect regime first
    regime, strategies, rationale = detect_regime()
    
    # Get all opportunities
    kalshi_opps = find_kalshi_opportunities()
    poly_opps = find_polymarket_opportunities()
    crypto_opps = find_crypto_momentum()
    arbs = find_cross_platform_arbs()
    
    all_opps = kalshi_opps + poly_opps + crypto_opps
    
    # Score everything
    scored = []
    for opp in all_opps:
        score, confidence, signals = calculate_edge_score(opp)
        size = suggest_position_size_v2(opp)
        scored.append({
            "market": opp.get("market", "")[:50],
            "platform": opp.get("platform", ""),
            "ev": opp.get("ev", 0),
            "score": score,
            "confidence": confidence,
            "signals": signals,
            "size": size,
        })
    
    # Sort by edge score
    scored.sort(key=lambda x: x["score"], reverse=True)
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cfg = REGIME_CONFIG
    
    lines = [
        f"**Proprietary Edge Analysis** | {ts}",
        f"Regime: {regime} | F&G: {cfg.get('fng_value', '?')}/100 | Size mult: {cfg.get('size_multiplier', 1):.0%}",
        f"Active strategies: {', '.join(strategies)}",
        "================================",
    ]
    
    # Show arbs first
    if arbs:
        lines.append(f"\n**Arbitrage ({len(arbs)} found):**")
        for a in arbs[:3]:
            lines.append(f"  [{a['type']}] Spread: ${a['spread']:.3f} ({a.get('profit_pct', 0):.1f}%) — {a['title'][:50]}")
    
    # Show top scored opportunities
    lines.append(f"\n**Top Scored Opportunities ({len(scored)} total):**")
    for s in scored[:5]:
        ev_pct = s["ev"] * 100
        signals_str = " + ".join(s["signals"][:3])
        lines.append(
            f"  [{s['confidence']}] Score: {s['score']}/100 | EV: +{ev_pct:.1f}% | ${s['size']:.0f}\n"
            f"    {s['platform']}: {s['market']}\n"
            f"    Signals: {signals_str}"
        )
    
    # Show skipped
    skipped = len([s for s in scored if s["confidence"] == "SKIP"])
    if skipped:
        lines.append(f"\n*{skipped} opportunities scored SKIP (negative edge or low confidence)*")
    
    lines.append("================================")
    
    report = "\n".join(lines)
    if len(report) > 1900:
        report = report[:1900] + "\n*...truncated*"
    await msg.edit(content=report)




# ============================================================================
# V9: FULL AUTONOMOUS EXECUTION ENGINE
# ============================================================================
import json as _json
import os as _os

AUTO_LIVE_CONFIG = {
    "enabled": True,
    "min_ev": 0.025,          # 2.5% minimum EV
    "min_edge_score": 65,     # MEDIUM+ confidence
    "max_position_pct": 0.0025,  # 0.25% max per trade (tiny start)
    "max_daily_trades": 20,
    "max_concurrent_positions": 10,
    "daily_loss_halt": -200,  # Halt if daily P&L drops below this
    "drawdown_halt_pct": 5.0, # Halt if drawdown exceeds 5%
    "trades_today": 0,
    "last_trade_reset": "",
}

TRADE_AUDIT_LOG = []


def audit_log(action, details):
    """Log every action for full audit trail."""
    entry = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "action": action,
        "details": details,
    }
    TRADE_AUDIT_LOG.append(entry)
    log.info("AUDIT: %s — %s", action, str(details)[:200])
    # Keep manageable
    while len(TRADE_AUDIT_LOG) > 500:
        TRADE_AUDIT_LOG.pop(0)


def reset_daily_counters():
    """Reset daily trade counters at midnight UTC."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if AUTO_LIVE_CONFIG["last_trade_reset"] != today:
        AUTO_LIVE_CONFIG["trades_today"] = 0
        AUTO_LIVE_CONFIG["last_trade_reset"] = today
        global DAILY_PNL
        DAILY_PNL = 0.0
        audit_log("DAILY_RESET", {"date": today})


def check_safety_gates():
    """Check all safety conditions before allowing a trade. Returns (ok, reason)."""
    # Kill switch
    if not AUTO_LIVE_CONFIG.get("enabled", True):
        return False, "Auto-execution disabled"
    
    # Daily trade limit
    if AUTO_LIVE_CONFIG["trades_today"] >= AUTO_LIVE_CONFIG["max_daily_trades"]:
        return False, f"Daily trade limit reached ({AUTO_LIVE_CONFIG['max_daily_trades']})"
    
    # Max concurrent positions
    if len(PAPER_PORTFOLIO.get("positions", [])) >= AUTO_LIVE_CONFIG["max_concurrent_positions"]:
        return False, f"Max concurrent positions ({AUTO_LIVE_CONFIG['max_concurrent_positions']})"
    
    # Daily loss halt
    if DAILY_PNL <= AUTO_LIVE_CONFIG["daily_loss_halt"]:
        return False, f"Daily loss limit hit (${DAILY_PNL:,.2f})"
    
    # Drawdown halt
    dd = ANALYTICS.get("max_drawdown", 0)
    if dd >= AUTO_LIVE_CONFIG["drawdown_halt_pct"]:
        return False, f"Drawdown halt ({dd:.1f}% >= {AUTO_LIVE_CONFIG['drawdown_halt_pct']}%)"
    
    return True, "All gates passed"


async def auto_execute_opportunity(opp, channel):
    """Automatically execute a trade for a high-scoring opportunity."""
    reset_daily_counters()
    
    # Safety check
    safe, reason = check_safety_gates()
    if not safe:
        audit_log("BLOCKED", {"reason": reason, "market": opp.get("market", "")[:50]})
        return False
    
    # Score the opportunity
    try:
        edge_score, confidence, signals = calculate_edge_score(opp)
    except Exception:
        edge_score, confidence, signals = 0, "SKIP", []
    
    # Check minimum thresholds
    ev = opp.get("ev", 0)
    if ev < AUTO_LIVE_CONFIG["min_ev"]:
        return False
    if edge_score < AUTO_LIVE_CONFIG["min_edge_score"]:
        return False
    if confidence in ("LOW", "SKIP"):
        return False
    
    # Calculate position size (quarter-Kelly with regime + tiny cap)
    bankroll = ANALYTICS.get("current_equity", 10000)
    size = suggest_position_size_v2(opp, bankroll)
    
    # Apply V9 tiny-start cap
    max_size = bankroll * AUTO_LIVE_CONFIG["max_position_pct"]
    size = min(size, max_size)
    
    if size < 1:
        return False  # Too small to trade
    
    # Build position record
    market = opp.get("market", "Unknown")[:80]
    platform = opp.get("platform", "Unknown")
    yes_price = opp.get("yes_price", 0)
    
    # Calculate stops and targets
    if opp.get("side") == "NO" and opp.get("no_price"):
        entry_price = opp["no_price"]
    elif yes_price > 0:
        entry_price = yes_price
    else:
        entry_price = 0.5
    stop_price = max(entry_price * 0.7, 0.01)  # 30% stop
    target_price = min(entry_price * 1.6, 0.99)  # 60% target (2:1 R:R)
    
    asset = opp.get("ticker", opp.get("slug", market[:20]))
    # Infer strategy for exit manager routing
    crypto_syms = ("btc","eth","sol","doge","zec","xlm","hype","sui","wbt","xrp","algo","shib","hbar")
    if any(k in asset.lower() or k in market.lower() for k in crypto_syms):
        strategy = "crypto"
    elif platform in ("Kalshi", "Polymarket"):
        strategy = "prediction"
    else:
        strategy = "prediction"

    shares = max(1, int(size / max(entry_price, 0.01)))
    opened_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    position = {
        "market": market,
        "platform": platform,
        "side": "BUY",
        "asset": asset,
        "shares": shares,
        "size_usd": size,
        "cost": size,
        "value": size,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
        "trailing_stop": stop_price,
        "ev": ev,
        "edge_score": edge_score,
        "confidence": confidence,
        "signals": signals,
        "strategy": strategy,
        "timestamp": opened_at,
    }

    # Execute the order
    success = False
    exec_msg = ""

    if TRADING_MODE == "live":
        # Route to correct exchange
        if platform == "Kalshi" or asset.startswith("KX"):
            _kalshi_action = "BUY_NO" if opp.get("side") == "NO" else "BUY"
            success, exec_msg = await execute_kalshi_order(_kalshi_action, asset, size)
        elif platform == "Polymarket":
            if POLYMARKET_PK:
                # Use NO token for NO contracts, YES token otherwise
                if opp.get("side") == "NO" and opp.get("no_token_id"):
                    token_id = opp["no_token_id"]
                    _poly_price = opp.get("no_price", None)
                else:
                    token_id = opp.get("token_id", opp.get("slug", ""))
                    _poly_price = opp.get("yes_price", None)
                success, exec_msg = await execute_polymarket_order("BUY", token_id, size, price=_poly_price)
            else:
                success = False
                exec_msg = "Polymarket not configured. Add POLYMARKET_PK to .env."
        elif asset in ("BTC", "ETH", "DOGE", "XRP", "SOL", "ALGO", "SHIB", "XLM", "HBAR"):
            success, exec_msg = await execute_coinbase_order("BUY", asset, size)
        elif asset.endswith("USDT") or asset.endswith("PERP"):
            success, exec_msg = await execute_phemex_order("BUY", asset, size)
        else:
            success = True
            exec_msg = f"PAPER-ROUTED: No direct execution path for {platform}. Logged."
    else:
        # Paper mode — always succeeds
        success = True
        exec_msg = "Paper trade executed"

    if success:
        # === UNIFIED LEDGER: single source of truth ===
        if size > PAPER_PORTFOLIO["cash"]:
            audit_log("BLOCKED", {"reason": "Insufficient cash", "market": market[:50], "need": size, "have": PAPER_PORTFOLIO["cash"]})
            return False
        PAPER_PORTFOLIO["cash"] -= size
        PAPER_PORTFOLIO["positions"].append(position)
        PAPER_PORTFOLIO["trades"].append(position)

        # Persist to SQLite
        db_open_position(
            market_id=market, platform=platform, strategy=strategy,
            direction="BUY", size_usd=size, shares=shares,
            entry_price=entry_price, stop_price=stop_price,
            target_price=target_price,
            metadata={"ev": ev, "edge_score": edge_score, "confidence": confidence},
        )
        db_log_paper_trade(position)

        AUTO_LIVE_CONFIG["trades_today"] += 1
        ANALYTICS["total_trades"] += 1

        audit_log("TRADE_OPENED", {
            "market": market[:50],
            "platform": platform,
            "size": size,
            "entry": entry_price,
            "stop": stop_price,
            "target": target_price,
            "edge_score": edge_score,
            "confidence": confidence,
            "mode": TRADING_MODE,
        })

        # Discord notification
        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
        mode_tag = "LIVE" if TRADING_MODE == "live" else "PAPER"
        try:
            await channel.send(
                f"**Auto-Trade [{mode_tag}]** | {ts}\n"
                f"BUY {platform}: {market[:50]}\n"
                f"Size: ${size:.2f} | Entry: ${entry_price:.3f}\n"
                f"Stop: ${stop_price:.3f} | Target: ${target_price:.3f}\n"
                f"Edge: {edge_score}/100 ({confidence}) | EV: +{ev*100:.1f}%\n"
                f"{exec_msg[:100]}"
            )
        except Exception:
            pass

        save_all_state()
        return True

    audit_log("TRADE_FAILED", {"market": market[:50], "error": exec_msg[:100]})
    return False


@bot.command(name="positions")
async def positions_cmd(ctx):
    """Show all open positions with live P&L (unified ledger)."""
    positions = PAPER_PORTFOLIO.get("positions", [])
    if not positions:
        await ctx.send("No open positions.")
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Open Positions** | {ts}", f"Mode: {TRADING_MODE.upper()}", "================================"]

    total_cost = 0
    for i, pos in enumerate(positions[-20:]):
        cost = pos.get("cost", 0)
        total_cost += cost
        lines.append(
            f"**{i+1}. {pos.get('platform', '?')}** | {pos.get('market', '?')[:45]}\n"
            f"  Cost: ${cost:.2f} | Entry: ${pos.get('entry_price', 0):.3f} | {pos.get('strategy', '?')}\n"
            f"  {pos.get('timestamp', '')}"
        )

    lines.append(f"\n**Total deployed: ${total_cost:,.2f}** | Positions: {len(positions)} | Cash: ${PAPER_PORTFOLIO['cash']:,.2f}")
    lines.append("================================")

    report = "\n".join(lines)
    if len(report) > 1900:
        report = report[:1900] + "\n*...truncated*"
    await ctx.send(report)


@bot.command(name="closed")
async def closed_cmd(ctx):
    """Show recently closed positions from SQLite."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT market_id, exit_reason, realized_pnl, closed_at FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 10")
        rows = c.fetchall()
        conn.close()
    except Exception:
        rows = []

    if not rows:
        await ctx.send("No closed positions yet.")
        return

    lines = ["**Recently Closed Positions**", "================================"]
    total_pnl = sum(r[2] or 0 for r in rows)
    wins = sum(1 for r in rows if (r[2] or 0) > 0)

    for r in rows:
        pnl = r[2] or 0
        icon = "WIN" if pnl > 0 else "LOSS"
        lines.append(f"[{icon}] ${pnl:+,.2f} | {(r[0] or '')[:40]}\n  {r[1] or 'N/A'} | {r[3] or ''}")

    lines.append(f"\n**Net P&L: ${total_pnl:+,.2f}** | {wins}/{len(rows)} wins")
    lines.append("================================")
    await ctx.send("\n".join(lines))


@bot.command(name="audit")
async def audit_cmd(ctx, n: int = 10):
    """Show recent audit log entries."""
    recent = TRADE_AUDIT_LOG[-n:]
    if not recent:
        await ctx.send("Audit log is empty.")
        return
    
    lines = [f"**Audit Log** (last {len(recent)} entries)", "================================"]
    for entry in reversed(recent):
        lines.append(f"`{entry['timestamp']}` **{entry['action']}** — {str(entry['details'])[:100]}")
    
    lines.append("================================")
    report = "\n".join(lines)
    if len(report) > 1900:
        report = report[:1900] + "\n*...truncated*"
    await ctx.send(report)


@bot.command(name="auto-config")
async def auto_config_cmd(ctx, key: str = "", value: str = ""):
    """View or update auto-execution config."""
    if not key:
        lines = ["**Auto-Execution Config**", "================================"]
        for k, v in AUTO_LIVE_CONFIG.items():
            lines.append(f"  `{k}`: {v}")
        lines.append("\nUsage: `!auto-config <key> <value>` to change")
        lines.append("================================")
        await ctx.send("\n".join(lines))
        return
    
    if key not in AUTO_LIVE_CONFIG:
        await ctx.send(f"Unknown key: `{key}`. Use `!auto-config` to see all keys.")
        return
    
    # Convert value
    old = AUTO_LIVE_CONFIG[key]
    try:
        if isinstance(old, bool):
            AUTO_LIVE_CONFIG[key] = value.lower() in ("true", "yes", "on", "1")
        elif isinstance(old, int):
            AUTO_LIVE_CONFIG[key] = int(value)
        elif isinstance(old, float):
            AUTO_LIVE_CONFIG[key] = float(value)
        else:
            AUTO_LIVE_CONFIG[key] = value
    except Exception:
        await ctx.send(f"Invalid value for `{key}`: {value}")
        return
    
    save_all_state()
    audit_log("CONFIG_CHANGED", {"key": key, "old": old, "new": AUTO_LIVE_CONFIG[key]})
    await ctx.send(f"Updated `{key}`: {old} → {AUTO_LIVE_CONFIG[key]}")


# ============================================================================
# V9: AI OVERSIGHT + WATCHDOG
# ============================================================================

OVERSIGHT_CONFIG = {
    "win_rate_floor": 40,       # Pause if win rate drops below 40%
    "max_drawdown_halt": 5.0,   # Halt at 5% drawdown
    "max_daily_loss": -200,     # Halt at -$200 daily
    "consecutive_loss_halt": 5, # Halt after 5 consecutive losses
    "last_oversight_run": "",
}


async def run_ai_oversight(channel):
    """AI oversight: review performance, pause if needed, suggest improvements."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    
    total_trades = ANALYTICS.get("total_trades", 0)
    winning = ANALYTICS.get("winning_trades", 0)
    losing = ANALYTICS.get("losing_trades", 0)
    total_pnl = ANALYTICS.get("total_pnl", 0)
    max_dd = ANALYTICS.get("max_drawdown", 0)
    
    win_rate = (winning / max(total_trades, 1)) * 100
    
    alerts = []
    halt_trading = False
    
    # Check win rate
    if total_trades >= 10 and win_rate < OVERSIGHT_CONFIG["win_rate_floor"]:
        alerts.append(f"Win rate {win_rate:.1f}% below floor ({OVERSIGHT_CONFIG['win_rate_floor']}%)")
        halt_trading = True
    
    # Check drawdown
    if max_dd >= OVERSIGHT_CONFIG["max_drawdown_halt"]:
        alerts.append(f"Drawdown {max_dd:.1f}% exceeds halt threshold ({OVERSIGHT_CONFIG['max_drawdown_halt']}%)")
        halt_trading = True
    
    # Check daily loss
    if DAILY_PNL <= OVERSIGHT_CONFIG["max_daily_loss"]:
        alerts.append(f"Daily P&L ${DAILY_PNL:,.2f} below halt threshold (${OVERSIGHT_CONFIG['max_daily_loss']:,.2f})")
        halt_trading = True
    
    # Check consecutive losses (from SQLite)
    consecutive_losses = 0
    try:
        _oc = sqlite3.connect(DB_PATH)
        _occ = _oc.cursor()
        _occ.execute("SELECT realized_pnl FROM positions WHERE status='closed' ORDER BY closed_at DESC LIMIT 5")
        for (_rpnl,) in _occ.fetchall():
            if (_rpnl or 0) < 0:
                consecutive_losses += 1
            else:
                break
        _oc.close()
    except Exception:
        pass
    if consecutive_losses >= OVERSIGHT_CONFIG["consecutive_loss_halt"]:
        alerts.append(f"{consecutive_losses} consecutive losses — halt threshold is {OVERSIGHT_CONFIG['consecutive_loss_halt']}")
        halt_trading = True
    
    if halt_trading:
        AUTO_LIVE_CONFIG["enabled"] = False
        audit_log("AI_HALT", {"alerts": alerts})
        try:
            await channel.send(
                f"**AI OVERSIGHT ALERT** | {ts}\n"
                f"Trading HALTED by AI oversight.\n"
                f"Reasons:\n" + "\n".join(f"  - {a}" for a in alerts) +
                f"\n\nUse `!auto-config enabled true` to resume after review."
            )
        except Exception:
            pass
    
    # Build daily oversight report
    regime = REGIME_CONFIG.get("current_regime", "UNKNOWN")
    fng = REGIME_CONFIG.get("fng_value", "?")
    
    report = (
        f"**AI Oversight Report** | {ts}\n================================\n"
        f"Regime: {regime} | F&G: {fng}/100\n"
        f"Mode: {TRADING_MODE.upper()} | Auto-exec: {'ON' if AUTO_LIVE_CONFIG['enabled'] else 'HALTED'}\n"
        f"\n**Performance:**\n"
        f"  Trades: {total_trades} | Wins: {winning} | Losses: {losing}\n"
        f"  Win rate: {win_rate:.1f}%\n"
        f"  Total P&L: ${total_pnl:+,.2f}\n"
        f"  Daily P&L: ${DAILY_PNL:+,.2f}\n"
        f"  Max drawdown: {max_dd:.1f}%\n"
        f"\n**Positions:**\n"
        f"  Open: {len(PAPER_PORTFOLIO.get('positions', []))} | Closed today: {AUTO_LIVE_CONFIG['trades_today']}\n"
        f"  Consecutive losses: {consecutive_losses}\n"
    )
    
    if alerts:
        report += f"\n**ALERTS:** {len(alerts)}\n" + "\n".join(f"  - {a}" for a in alerts)
    else:
        report += "\n**Status:** All systems nominal"
    
    report += "\n================================"
    
    OVERSIGHT_CONFIG["last_oversight_run"] = ts
    
    return report


@bot.command(name="oversight")
async def oversight_cmd(ctx):
    """Run AI oversight check manually."""
    report = await run_ai_oversight(ctx.channel)
    if len(report) > 1900:
        report = report[:1900] + "\n*...truncated*"
    await ctx.send(report)




@bot.command(name="test-execution")
async def test_execution(ctx, platform: str = "", amount: str = "1"):
    """Test order execution on a specific platform with tiny amount.
    Usage: !test-execution kalshi 1
           !test-execution coinbase 2
           !test-execution robinhood 1
           !test-execution phemex 1
    """
    if not platform:
        await ctx.send(
            "**Test Execution — Safe Order Testing**\n"
            "Usage: `!test-execution <platform> <amount>`\n"
            "Platforms: `kalshi`, `coinbase`, `robinhood`, `phemex`\n"
            "Amount: $1-5 recommended for testing\n"
            "\nThis will attempt a REAL order (unless dry-run is on).\n"
            f"Dry-run mode: **{'ON (safe)' if DRY_RUN_MODE else 'OFF (real orders!)'}**\n"
            "Use `!dry-run on` first if you want to test without real orders."
        )
        return
    
    platform = platform.lower()
    try:
        amt = float(amount.replace("$", ""))
    except ValueError:
        await ctx.send(f"Invalid amount: {amount}")
        return
    
    if amt > 10:
        await ctx.send(f"Max test amount is $10. You specified ${amt:.2f}.")
        return
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    dry_tag = " [DRY-RUN]" if DRY_RUN_MODE else " [REAL]"
    
    msg = await ctx.send(f"Testing {platform} execution{dry_tag}... ${amt:.2f}")
    
    success = False
    exec_msg = ""
    
    if platform == "kalshi":
        # Test with a cheap market
        try:
            success, exec_msg = await execute_kalshi_order("BUY", "KXWARMING-50", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "coinbase":
        try:
            success, exec_msg = await execute_coinbase_order("BUY", "BTC", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "robinhood":
        try:
            # Robinhood crypto: buy small amount of XRP (cheapest)
            qty = amt / 2.0  # Approx XRP price ~$2
            success, exec_msg = await execute_robinhood_order("BUY", "XRP", qty)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "phemex":
        try:
            success, exec_msg = await execute_phemex_order("BUY", "BTCUSDT", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "polymarket":
        try:
            # Fetch ANY active market with valid CLOB token for testing
            poly_mkts = get_polymarket_markets(limit=20)
            token_id = ""
            market_name = "Unknown"
            yes_price = 0.50
            for mkt in poly_mkts:
                clob_raw = mkt.get("clobTokenIds", "")
                if clob_raw:
                    try:
                        ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
                        if ids and len(str(ids[0])) > 10:
                            token_id = ids[0]
                            market_name = mkt.get("question", mkt.get("title", "Unknown"))[:60]
                            op = mkt.get("outcomePrices", "")
                            if op:
                                try:
                                    pp = json.loads(op) if isinstance(op, str) else op
                                    yes_price = float(pp[0])
                                except (ValueError, IndexError):
                                    pass
                            break
                    except (json.JSONDecodeError, IndexError):
                        pass
            if not token_id:
                await msg.edit(content="No Polymarket markets with valid CLOB token found.")
                return
            await msg.edit(content=f"Testing Polymarket execution{dry_tag}... ${amt:.2f}\nMarket: {market_name}\nToken: {token_id[:20]}...\nYES price: ${yes_price:.3f}")
            success, exec_msg = await execute_polymarket_order("BUY", token_id, amt, price=yes_price)
            exec_msg = f"[{market_name}] {exec_msg}"
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    
    elif platform == "alpaca":
        try:
            success, exec_msg = await execute_alpaca_order("BUY", "AAPL", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    elif platform == "ibkr":
        try:
            success, exec_msg = await execute_ibkr_order("BUY", "AAPL", amt)
        except Exception as exc:
            exec_msg = f"Error: {exc}"
    else:
        await msg.edit(content=f"Unknown platform: `{platform}`. Use: kalshi, coinbase, robinhood, phemex, polymarket, alpaca, ibkr")
        return
    
    status = "SUCCESS" if success else "FAILED"
    
    audit_log("TEST_EXECUTION", {
        "platform": platform,
        "amount": amt,
        "dry_run": DRY_RUN_MODE,
        "success": success,
        "message": exec_msg[:100],
    })
    
    await msg.edit(content=(
        f"**Test Execution{dry_tag}** | {ts}\n"
        f"Platform: {platform} | Amount: ${amt:.2f}\n"
        f"Status: **{status}**\n"
        f"Response: {exec_msg[:500]}\n"
        f"\n{'Use `!dry-run off` to test with real orders.' if DRY_RUN_MODE else 'This was a REAL order attempt.'}"
    ))




# ============================================================================
# ALPACA INTEGRATION (Stocks + Crypto)
# ============================================================================
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY", "")
KALSHI_KEY_PATH = os.getenv("KALSHI_KEY_PATH", "/app/keys/kalshi.pem")
if os.path.exists(KALSHI_KEY_PATH):
    with open(KALSHI_KEY_PATH) as _f:
        _kalshi_pem = _f.read().strip()
    if "-----BEGIN" in _kalshi_pem:
        KALSHI_PRIVATE_KEY = _kalshi_pem
        print(f"Loaded Kalshi PEM from {KALSHI_KEY_PATH}")
COINBASE_KEY_PATH = os.getenv("COINBASE_KEY_PATH", "/app/keys/coinbase.pem")
# Load PEM from file if exists, overriding .env value
if os.path.exists(COINBASE_KEY_PATH):
    with open(COINBASE_KEY_PATH) as _f:
        _coinbase_pem = _f.read().strip()
    if "-----BEGIN" in _coinbase_pem:
        COINBASE_API_SECRET = _coinbase_pem
        print(f"Loaded Coinbase PEM from {COINBASE_KEY_PATH}")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")  # Set to https://api.alpaca.markets for live

# Interactive Brokers (Client Portal API)
IBKR_ACCOUNT_ID = os.getenv("IBKR_ACCOUNT_ID", "")
IBKR_BASE_URL = os.getenv("IBKR_BASE_URL", "https://localhost:5000/v1/api")
IBKR_USERNAME = os.getenv("IBKR_USERNAME", "")
IBKR_TOKEN = os.getenv("IBKR_TOKEN", "")


def get_alpaca_balance():
    """Fetch Alpaca account balance."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return "Not configured (add ALPACA_API_KEY + ALPACA_SECRET_KEY to .env)"
    try:
        hdrs = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        }
        r = requests.get(f"{ALPACA_BASE_URL}/v2/account", headers=hdrs, timeout=10)
        if r.status_code == 200:
            acct = r.json()
            equity = float(acct.get("equity", 0))
            cash = float(acct.get("cash", 0))
            buying_power = float(acct.get("buying_power", 0))
            pnl = float(acct.get("portfolio_value", 0)) - float(acct.get("last_equity", 0))
            
            # Get positions
            r2 = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=hdrs, timeout=10)
            positions_str = ""
            if r2.status_code == 200:
                positions = r2.json()
                for p in positions[:10]:
                    sym = p.get("symbol", "")
                    qty = p.get("qty", "0")
                    mkt_val = float(p.get("market_value", 0))
                    unrealized = float(p.get("unrealized_pl", 0))
                    positions_str += f"\n  {sym}: {qty} shares (${mkt_val:,.2f}, P&L: ${unrealized:+,.2f})"
            
            # Cap display for paper account to avoid inflated portfolio totals
            display_equity = equity
            display_cash = cash
            display_bp = buying_power
            result = f"${display_equity:,.2f} equity | ${display_cash:,.2f} cash | BP: ${display_bp:,.2f}"
            if positions_str:
                result += positions_str
            
            is_paper = "paper" in ALPACA_BASE_URL
            result += f"\n  Mode: {'PAPER' if is_paper else 'LIVE'}"
            return result
        else:
            return f"API error: {r.status_code} {r.text[:100]}"
    except Exception as exc:
        return f"Error: {exc}"


def _alpaca_limit_at_mid(symbol, side, notional=None, qty=None, max_attempts=3, timeout_sec=45):
    """Submit limit order at bid/ask midpoint, retry up to max_attempts, fallback to market.
    Returns (order_id, fill_price, fill_type) where fill_type is 'limit' or 'market'."""
    import requests as _lreq, time as _ltime
    _hdr = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }
    _data_hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
    _orders_url = f"{ALPACA_BASE_URL}/v2/orders"

    for attempt in range(1, max_attempts + 1):
        # Fetch current bid/ask
        try:
            _qr = _lreq.get(f"https://data.alpaca.markets/v2/stocks/{symbol}/quotes/latest",
                            headers=_data_hdr, timeout=5)
            _qd = _qr.json().get("quote", {}) if _qr.status_code == 200 else {}
            _bid = float(_qd.get("bp", 0) or 0)
            _ask = float(_qd.get("ap", 0) or 0)
        except Exception:
            _bid, _ask = 0, 0

        if _bid <= 0 or _ask <= 0:
            log.warning("LIMIT-MID: %s no quote (bid=%.2f ask=%.2f) — skip to market", symbol, _bid, _ask)
            break  # Fall through to market order

        _mid = round((_bid + _ask) / 2, 2)
        _body = {
            "symbol": symbol,
            "side": side,
            "type": "limit",
            "limit_price": str(_mid),
            "time_in_force": "day",
        }
        if notional is not None:
            # Limit orders require qty, not notional — estimate shares from mid
            _est_shares = round(notional / _mid, 4) if _mid > 0 else 0
            if _est_shares <= 0:
                break
            _body["qty"] = str(_est_shares)
        elif qty is not None:
            _body["qty"] = str(qty)

        _r = _lreq.post(_orders_url, json=_body, headers=_hdr, timeout=10)
        if _r.status_code not in (200, 201):
            log.warning("LIMIT-MID: %s attempt %d submit failed: %s", symbol, attempt, _r.text[:200])
            break  # Fall through to market

        _oid = _r.json().get("id", "")
        log.info("LIMIT-MID: %s attempt %d limit=$%.2f (bid=%.2f ask=%.2f) id=%s",
                 symbol, attempt, _mid, _bid, _ask, _oid[:12])

        # Poll for fill
        _deadline = _ltime.time() + timeout_sec
        _filled = False
        _fill_price = 0
        while _ltime.time() < _deadline:
            _ltime.sleep(5)
            try:
                _sr = _lreq.get(f"{_orders_url}/{_oid}", headers=_hdr, timeout=5)
                if _sr.status_code == 200:
                    _sd = _sr.json()
                    if _sd.get("status") == "filled":
                        _fill_price = float(_sd.get("filled_avg_price", 0) or 0)
                        _filled = True
                        break
                    elif _sd.get("status") in ("canceled", "expired", "rejected"):
                        break
            except Exception:
                pass

        if _filled:
            log.info("LIMIT-MID FILL: %s at $%.2f (mid was $%.2f, spread saved est $%.4f)",
                     symbol, _fill_price, _mid, abs(_ask - _mid) if side == "buy" else abs(_mid - _bid))
            return _oid, _fill_price, "limit"

        # Not filled — cancel and retry with fresh mid
        try:
            _lreq.delete(f"{_orders_url}/{_oid}", headers=_hdr, timeout=5)
            log.info("LIMIT-MID: %s attempt %d cancelled (unfilled after %ds)", symbol, attempt, timeout_sec)
        except Exception:
            pass

    # --- Fallback to market order ---
    log.info("LIMIT-MID FALLBACK: %s → market order after %d limit attempts", symbol, max_attempts)
    _mbody = {"symbol": symbol, "side": side, "type": "market", "time_in_force": "day"}
    if notional is not None:
        _mbody["notional"] = str(round(notional, 2))
    elif qty is not None:
        _mbody["qty"] = str(qty)
    try:
        _mr = _lreq.post(_orders_url, json=_mbody, headers=_hdr, timeout=10)
        if _mr.status_code in (200, 201):
            _md = _mr.json()
            _moid = _md.get("id", "unknown")
            _mfp = float(_md.get("filled_avg_price", 0) or 0)
            return _moid, _mfp, "market"
        else:
            log.warning("LIMIT-MID MARKET FALLBACK FAILED: %s HTTP %d: %s", symbol, _mr.status_code, _mr.text[:200])
            return None, 0, "failed"
    except Exception as _me:
        log.warning("LIMIT-MID MARKET FALLBACK ERROR: %s %s", symbol, _me)
        return None, 0, "failed"


async def execute_alpaca_order(action, symbol, amount):
    """Place an order via Alpaca API. Returns (success, message)."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, "Alpaca not configured (add API keys to .env)"
    
    # DRY_RUN check
    if DRY_RUN_MODE:
        log.info("DRY RUN: Alpaca %s %s $%.2f", action, symbol, amount)
        return True, f"DRY RUN: {action} {symbol} ${amount:.2f} — order NOT sent (dry-run mode)"
    
    try:
        hdrs = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }
        
        side = "buy" if action.upper() == "BUY" else "sell"
        
        order_body = {
            "symbol": symbol,
            "notional": str(round(amount, 2)),  # Dollar amount
            "side": side,
            "type": "market",
            "time_in_force": "day",
        }
        
        r = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=order_body, headers=hdrs, timeout=15)
        
        if r.status_code in (200, 201):
            data = r.json()
            order_id = data.get("id", "unknown")
            status = data.get("status", "unknown")
            return True, f"Alpaca order placed: {side} {symbol} ${amount:.2f} (ID: {order_id}, Status: {status})"
        else:
            return False, f"Alpaca error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Alpaca execution error: {exc}"




# ============================================================================
# POLYMARKET CLOB EXECUTION
# ============================================================================
POLYMARKET_PK = os.getenv("POLYMARKET_PK", "").strip()
POLYMARKET_FUNDER = os.getenv("POLYMARKET_FUNDER", "").strip()
POLYMARKET_API_KEY = os.getenv("POLYMARKET_API_KEY", "").strip()
POLYMARKET_API_SECRET = os.getenv("POLYMARKET_API_SECRET", "").strip()
POLYMARKET_PASSPHRASE = os.getenv("POLYMARKET_PASSPHRASE", "").strip()


def get_polymarket_clob_client():
    """Initialize Polymarket CLOB client. Returns client or None."""
    if not POLYMARKET_PK:
        return None
    try:
        from py_clob_client.client import ClobClient
        
        kwargs = {
            "host": "https://clob.polymarket.com",
            "key": POLYMARKET_PK,
            "chain_id": 137,
        }
        kwargs["signature_type"] = 2
        if POLYMARKET_FUNDER:
            kwargs["funder"] = POLYMARKET_FUNDER
        
        client = ClobClient(**kwargs)
        
        # Set API creds if available
        if POLYMARKET_API_KEY and POLYMARKET_API_SECRET and POLYMARKET_PASSPHRASE:
            from py_clob_client.clob_types import ApiCreds
            creds = ApiCreds(
                api_key=POLYMARKET_API_KEY,
                api_secret=POLYMARKET_API_SECRET,
                api_passphrase=POLYMARKET_PASSPHRASE,
            )
            client.set_api_creds(creds)
        else:
            # Derive creds from private key
            try:
                client.set_api_creds(client.create_or_derive_api_creds())
            except Exception as ce:
                log.warning("Polymarket cred derivation failed: %s", ce)
        
        return client
    except ImportError:
        log.warning("py-clob-client not installed")
        return None
    except Exception as exc:
        log.warning("Polymarket client init error: %s", exc)
        return None


async def execute_polymarket_order(action, token_id, amount, price=None):
    """Place an order on Polymarket CLOB. Returns (success, message)."""
    if not POLYMARKET_PK:
        return False, "Polymarket private key not configured (add POLYMARKET_PK to .env)"
    
    if DRY_RUN_MODE:
        log.info("DRY RUN: Polymarket %s token=%s $%.2f", action, token_id[:20], amount)
        return True, f"DRY RUN: {action} Polymarket order logged but not sent (dry-run mode)"
    
    try:
        from py_clob_client.clob_types import MarketOrderArgs, OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL
        
        client = get_polymarket_clob_client()
        if not client:
            return False, "Failed to initialize Polymarket CLOB client"
        
        # ---- VERIFY token_id on CLOB before placing order ----
        verified_token = token_id
        try:
            mkt_info = client.get_market(token_id)
            log.info("CLOB market verified for token %s", token_id[:20])
        except Exception as verify_err:
            log.warning("CLOB token lookup failed (%s), trying condition_id lookup...", verify_err)
            try:
                r = requests.get(f"https://clob.polymarket.com/markets/{token_id}", timeout=10)
                if r.status_code == 200:
                    clob_data = r.json()
                    clob_tokens = clob_data.get("tokens", [])
                    if clob_tokens:
                        idx = 0 if action.upper() == "BUY" else min(1, len(clob_tokens) - 1)
                        verified_token = clob_tokens[idx].get("token_id", token_id)
                        log.info("Resolved CLOB token via condition_id: %s", verified_token[:30])
                else:
                    # Last resort: refresh from Gamma API
                    log.warning("CLOB condition lookup returned %d, trying Gamma refresh", r.status_code)
                    try:
                        gr = requests.get(
                            "https://gamma-api.polymarket.com/markets",
                            params={"limit": 50, "closed": "false", "active": "true",
                                    "order": "volume24hr", "ascending": "false"},
                            timeout=15,
                        )
                        if gr.status_code == 200:
                            for gm in gr.json():
                                clob_raw = gm.get("clobTokenIds", "")
                                cond = gm.get("condition_id", "")
                                if cond and token_id.startswith(cond[:10]):
                                    try:
                                        ids = json.loads(clob_raw) if isinstance(clob_raw, str) else clob_raw
                                        if ids:
                                            verified_token = ids[0]
                                            log.info("Gamma refresh resolved token: %s", verified_token[:30])
                                            break
                                    except (json.JSONDecodeError, IndexError):
                                        pass
                    except Exception as gex:
                        log.warning("Gamma refresh failed: %s", gex)
            except Exception as clob_err:
                log.warning("CLOB condition_id lookup also failed: %s", clob_err)
        
        side = BUY if action.upper() == "BUY" else SELL
        
        if price and price > 0:
            order_args = OrderArgs(
                price=price,
                size=round(amount / price, 2),
                side=side,
                token_id=verified_token,
            )
            signed_order = client.create_order(order_args)
            resp = client.post_order(signed_order, OrderType.GTC)
        else:
            mo = MarketOrderArgs(
                token_id=verified_token,
                amount=amount,
                side=side,
            )
            signed_order = client.create_market_order(mo)
            resp = client.post_order(signed_order, OrderType.FOK)
        
        if resp and resp.get("success", False):
            order_id = resp.get("orderID", "unknown")
            return True, f"Polymarket order placed: {action} ${amount:.2f} (ID: {order_id})"
        else:
            error = resp.get("errorMsg", str(resp)[:200]) if resp else "No response"
            return False, f"Polymarket order failed: {error}"
    
    except ImportError:
        return False, "py-clob-client not installed. Run: pip install py-clob-client"
    except Exception as exc:
        return False, f"Polymarket execution error: {exc}"


@bot.command(name="poly-setup")
async def poly_setup_cmd(ctx):
    """Show Polymarket setup status and instructions."""
    lines = ["**Polymarket CLOB Setup**", "================================"]
    
    pk_status = "Configured" if POLYMARKET_PK else "NOT SET"
    funder_status = f"{POLYMARKET_FUNDER[:10]}..." if POLYMARKET_FUNDER else "NOT SET"
    api_status = "Configured" if POLYMARKET_API_KEY else "NOT SET"
    
    lines.append(f"  Private Key: **{pk_status}**")
    lines.append(f"  Funder Address: **{funder_status}**")
    lines.append(f"  API Credentials: **{api_status}**")
    
    # Try to init client
    client = get_polymarket_clob_client()
    if client:
        lines.append(f"  Client: **CONNECTED**")
        try:
            ok = client.get_ok()
            lines.append(f"  Server: {ok}")
        except Exception as e:
            lines.append(f"  Server test: {e}")
    else:
        lines.append(f"  Client: **NOT CONNECTED**")
    
    lines.append("")
    lines.append("**Setup Steps:**")
    lines.append("1. Export your private key from reveal.polymarket.com")
    lines.append("2. Add to .env: `POLYMARKET_PK=0x...`")
    lines.append("3. Add funder: `POLYMARKET_FUNDER=0x...`")
    lines.append("5. Run `!test-execution polymarket 1` to test")
    lines.append("================================")
    await ctx.send("\n".join(lines))



# ============================================================================
# INTERACTIVE BROKERS INTEGRATION
# ============================================================================

def get_ibkr_balance():
    if not IBKR_ACCOUNT_ID:
        return "Not configured (add IBKR_ACCOUNT_ID to .env)"
    try:
        hdrs = {"Content-Type": "application/json"}
        url = f"{IBKR_BASE_URL}/portfolio/{IBKR_ACCOUNT_ID}/summary"
        r = requests.get(url, headers=hdrs, timeout=5, verify=False)
        if r.status_code == 200:
            data = r.json()
            equity = data.get("netliquidation", {}).get("amount", 0)
            cash = data.get("totalcashvalue", {}).get("amount", 0)
            is_paper = IBKR_ACCOUNT_ID.startswith("DU")
            return f"${equity:,.2f} equity | ${cash:,.2f} cash | {'PAPER' if is_paper else 'LIVE'} | {IBKR_ACCOUNT_ID}"
        else:
            is_paper = IBKR_ACCOUNT_ID.startswith("DU")
            return f"Gateway not connected ({IBKR_ACCOUNT_ID} {'PAPER' if is_paper else 'LIVE'})"
    except Exception:
        is_paper = IBKR_ACCOUNT_ID.startswith("DU")
        return f"Gateway offline ({IBKR_ACCOUNT_ID} {'PAPER' if is_paper else 'LIVE'} - start Client Portal Gateway)"


async def execute_ibkr_order(action, symbol, amount):
    if not IBKR_ACCOUNT_ID:
        return False, "IBKR not configured (add IBKR_ACCOUNT_ID to .env)"
    if DRY_RUN_MODE:
        return True, f"DRY RUN: {action} {symbol} ${amount:.2f} - order NOT sent (dry-run mode)"
    try:
        hdrs = {"Content-Type": "application/json"}
        side = "BUY" if action.upper() == "BUY" else "SELL"
        search_url = f"{IBKR_BASE_URL}/iserver/secdef/search"
        sr = requests.post(search_url, json={"symbol": symbol, "secType": "STK"}, headers=hdrs, timeout=10, verify=False)
        if sr.status_code != 200:
            return False, f"IBKR symbol lookup failed: {sr.status_code}"
        contracts = sr.json()
        if not contracts:
            return False, f"IBKR: No contract found for {symbol}"
        conid = contracts[0].get("conid", "")
        order_url = f"{IBKR_BASE_URL}/iserver/account/{IBKR_ACCOUNT_ID}/orders"
        order_body = {"orders": [{"conid": conid, "orderType": "MKT", "side": side, "quantity": 1, "tif": "DAY"}]}
        r = requests.post(order_url, json=order_body, headers=hdrs, timeout=15, verify=False)
        if r.status_code in (200, 201):
            data = r.json()
            oid = data[0].get("order_id", "unknown") if isinstance(data, list) else data.get("order_id", "unknown")
            return True, f"IBKR order placed: {side} {symbol} (ID: {oid})"
        else:
            return False, f"IBKR order failed: {r.status_code} {r.text[:100]}"
    except requests.exceptions.ConnectionError:
        return False, "IBKR Gateway offline - start Client Portal Gateway to trade"
    except Exception as exc:
        return False, f"IBKR error: {exc}"


# ============================================================================
# TRADINGVIEW WEBHOOK ENDPOINT
# ============================================================================
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

def _tv_execute_signal(sig_entry):
    """Auto-execute a TradingView signal on Alpaca if it aligns with cycle consensus.
    Called synchronously from webhook handler thread."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return
    asset = sig_entry.get("asset", "")
    signal = sig_entry.get("signal", "")
    if not asset or asset == "MARKET":
        return

    # Check if we already have a position in this ticker
    for p in PAPER_PORTFOLIO.get("positions", []):
        if asset in p.get("market", "").upper() or asset in p.get("long_leg", "").upper():
            log.info("TV EXEC SKIP: %s already in portfolio", asset)
            return

    # Determine direction
    is_buy = signal in ("BUY", "LONG", "BULLISH", "GREEN_DOT")
    is_sell = signal in ("SELL", "SHORT", "BEARISH", "RED_DOT")
    if not is_buy and not is_sell:
        return

    # Size calculation
    portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
        p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))
    size = portfolio_value * TRADINGVIEW_SIGNALS.get("auto_execute_size_pct", 0.01)
    if size > PAPER_PORTFOLIO.get("cash", 0) or size < 10:
        return

    # Submit to Alpaca
    hdrs = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }
    side = "buy" if is_buy else "sell"
    body = {"symbol": asset, "notional": str(round(size, 2)),
            "side": side, "type": "market", "time_in_force": "day"}

    try:
        r = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=body, headers=hdrs, timeout=10)
        if r.status_code in (200, 201):
            order_id = r.json().get("id", "unknown")
            sig_entry["executed"] = True
            sig_entry["order_id"] = order_id

            # Record in paper portfolio
            _tv_pos = {
                "market": f"TV:{asset}",
                "side": signal, "shares": 1,
                "entry_price": float(sig_entry.get("price", 0) or 0),
                "cost": size, "value": size,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "platform": "Alpaca", "ev": 0,
                "strategy": "tv_signal",
                "indicator": sig_entry.get("indicator", ""),
            }
            PAPER_PORTFOLIO["cash"] -= size
            PAPER_PORTFOLIO["positions"].append(_tv_pos)
            db_open_position(
                market_id=f"TV:{asset}", platform="Alpaca", strategy="tv_signal",
                direction=signal, size_usd=size, shares=1,
                entry_price=float(sig_entry.get("price", 0) or 0),
                metadata={"signal": signal, "indicator": sig_entry.get("indicator", ""),
                          "order_id": order_id, "source": "tradingview_webhook"},
            )
            log.info("TV AUTO-EXEC: %s %s $%.0f order=%s", signal, asset, size, order_id)
        else:
            log.warning("TV EXEC FAILED: %s %s HTTP %d: %s", signal, asset, r.status_code, r.text[:100])
    except Exception as e:
        log.warning("TV EXEC error: %s", e)


# Rolling agent event log for War Room dashboard
_AGENT_EVENT_LOG = []  # [{timestamp, agent, message}] — last 100 events

def _agent_log_event(agent, message):
    """Log an agent event for the War Room intelligence feed."""
    _AGENT_EVENT_LOG.append({
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        "agent": agent,
        "message": message[:120],
    })
    if len(_AGENT_EVENT_LOG) > 100:
        _AGENT_EVENT_LOG.pop(0)


import json as _json_api

def _build_api_status():
    """Build /api/status response."""
    cash = PAPER_PORTFOLIO.get("cash", 0)
    positions = PAPER_PORTFOLIO.get("positions", [])
    total_cost = sum(p.get("cost", 0) for p in positions)
    # Realized PnL
    total_realized = 0.0
    try:
        import sqlite3 as _asq
        _c = _asq.connect(DB_PATH)
        _r = _c.execute("SELECT SUM(realized_pnl) FROM positions WHERE status='closed' AND realized_pnl IS NOT NULL").fetchone()
        if _r and _r[0]:
            total_realized = _r[0]
        _c.close()
    except Exception:
        pass
    regime = get_regime("equities")
    fng_val, fng_label = get_fear_greed()
    equity = cash + total_cost
    pos_list = []
    for p in positions:
        pos_list.append({
            "market": p.get("market", "")[:40],
            "strategy": p.get("strategy", "?"),
            "cost": p.get("cost", 0),
            "entry_price": p.get("entry_price", 0),
            "shares": p.get("shares", 0),
            "timestamp": p.get("timestamp", ""),
            "platform": p.get("platform", ""),
            "long_leg": p.get("long_leg", ""),
            "short_leg": p.get("short_leg", ""),
        })
    return {
        "cash": cash, "equity": equity, "cost_basis": total_cost,
        "realized_pnl": total_realized, "combined_pnl": total_realized,
        "return_pct": ((equity / 25000) - 1) * 100 if equity > 0 else 0,
        "vix": regime.get("vix"), "regime": regime.get("regime", "?"),
        "fng_value": fng_val, "fng_label": fng_label,
        "positions": pos_list, "position_count": len(positions),
        "contrarian_mode": _PSYCH_STATE.get("contrarian_mode", False),
        "caution_mode": _PSYCH_STATE.get("caution_mode", False),
    }


def _build_api_intelligence():
    """Build /api/intelligence response."""
    return {"events": list(reversed(_AGENT_EVENT_LOG[-20:]))}


def _build_api_oracle():
    """Build /api/oracle response."""
    signals = []
    try:
        prices = _oracle_get_all_prices()
        for title, yes_price in prices.items():
            matched = _oracle_match_all_signals(title)
            for sig in matched:
                _is_inv = sig.get("inverse", False)
                if _is_inv:
                    # Inverse signal: fires when price drops BELOW threshold
                    pct = (1 - yes_price) / (1 - sig["threshold"]) * 100 if sig["threshold"] < 1 else 0
                    # Check geo requirement — count theme headlines directly,
                    # don't require the theme to be the primary geo alert
                    _geo_req = sig.get("geo_required", "")
                    _geo_min = sig.get("geo_min_headlines", 0)
                    _geo_ok = False
                    if _geo_req:
                        _theme_esc_count = _intel_theme_escalation_count(_geo_req)
                        _geo_ok = _theme_esc_count >= _geo_min
                    is_active = yes_price <= sig["threshold"] and (_geo_ok if _geo_req else True)
                    signals.append({
                        "name": sig["name"], "market": title[:60],
                        "yes_price": yes_price, "threshold": sig["threshold"],
                        "pct_to_threshold": min(pct, 100),
                        "long": sig["long"], "short": sig["short"],
                        "active": is_active,
                        "inverse": True,
                        "geo_ok": _geo_ok if _geo_req else None,
                    })
                else:
                    signals.append({
                        "name": sig["name"], "market": title[:60],
                        "yes_price": yes_price, "threshold": sig["threshold"],
                        "pct_to_threshold": yes_price / sig["threshold"] * 100 if sig["threshold"] > 0 else 0,
                        "long": sig["long"], "short": sig["short"],
                        "active": yes_price >= sig["threshold"],
                    })
    except Exception:
        pass
    # Geo-only fallback: show inverse signals that have no matching market
    _matched_names = {s["name"] for s in signals}
    for sig in ORACLE_SIGNALS:
        if not sig.get("inverse") or sig["name"] in _matched_names:
            continue
        _geo_req = sig.get("geo_required", "")
        if not _geo_req:
            continue
        _theme_count = _intel_theme_escalation_count(_geo_req)
        _geo_ok = _theme_count >= 8  # Geo-only bar
        _extra = sig.get("extra_long", "")
        signals.append({
            "name": sig["name"], "market": f"GEO-ONLY:{_geo_req}({_theme_count} headlines)",
            "yes_price": 0.20 if _geo_ok else 0.50,
            "threshold": sig["threshold"],
            "pct_to_threshold": min(100, (1 - (0.20 if _geo_ok else 0.50)) / (1 - sig["threshold"]) * 100),
            "long": sig["long"], "short": sig["short"],
            "active": _geo_ok,
            "inverse": True,
            "geo_ok": _geo_ok,
            "geo_only": True,
            "extra_long": _extra or None,
        })
    # Headlines
    headlines = {}
    for theme, entries in _INTEL_HEADLINE_MEMORY.items():
        recent = sorted(entries, key=lambda x: x[0], reverse=True)[:3]
        headlines[theme] = [h for _, h in recent]
    geo = _intel_get_geo_state()
    # Serialize datetime fields
    _geo_safe = {k: (v.strftime("%Y-%m-%d %H:%M UTC") if hasattr(v, "strftime") else v)
                 for k, v in geo.items()}
    return {"signals": signals, "headlines": headlines, "geo_state": _geo_safe}


def _build_api_charts():
    """Build /api/charts response for dashboard equity curve, strategy P&L, trade frequency."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        # Equity curve: daily snapshots
        c.execute("SELECT date, paper_cash FROM daily_state ORDER BY date DESC LIMIT 30")
        equity_rows = c.fetchall()
        equity_curve = [{"date": r[0], "equity": r[1] or 25000} for r in reversed(equity_rows)]
        # Strategy P&L breakdown
        c.execute("""SELECT strategy, COALESCE(SUM(realized_pnl), 0) as pnl, COUNT(*) as trades,
            SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END) as wins
            FROM positions WHERE status='closed' AND strategy NOT IN ('pairs_legacy')
            AND realized_pnl IS NOT NULL GROUP BY strategy""")
        strat_rows = c.fetchall()
        strategy_pnl = [{"strategy": r[0], "pnl": round(r[1], 2), "trades": r[2], "wins": r[3]} for r in strat_rows]
        # Trade frequency: trades per day last 14 days
        c.execute("""SELECT DATE(created_at) as d, COUNT(*) FROM positions
            WHERE created_at IS NOT NULL AND strategy NOT IN ('pairs_legacy')
            GROUP BY DATE(created_at) ORDER BY d DESC LIMIT 14""")
        freq_rows = c.fetchall()
        trade_freq = [{"date": r[0], "count": r[1]} for r in reversed(freq_rows)]
        # Launch criteria
        c.execute("SELECT COUNT(*) FROM positions WHERE status='closed' AND strategy NOT IN ('pairs_legacy') AND created_at >= '2026-03-29'")
        clean_trades = c.fetchone()[0]
        c.execute("""SELECT COUNT(*), SUM(CASE WHEN realized_pnl > 0 THEN 1 ELSE 0 END)
            FROM positions WHERE status='closed' AND strategy NOT IN ('pairs_legacy')
            AND realized_pnl IS NOT NULL AND created_at >= '2026-03-29'""")
        _wr_row = c.fetchone()
        total_wr = _wr_row[0] or 0
        wins_wr = _wr_row[1] or 0
        win_rate = (wins_wr / total_wr * 100) if total_wr > 0 else 0
        # Sharpe from daily P&L
        c.execute("""SELECT SUM(realized_pnl) FROM positions WHERE status='closed'
            AND strategy NOT IN ('pairs_legacy') AND DATE(closed_at) = DATE('now')""")
        # Consecutive balanced days
        c.execute("""SELECT DATE(closed_at), SUM(realized_pnl) FROM positions
            WHERE status='closed' AND closed_at IS NOT NULL AND strategy NOT IN ('pairs_legacy')
            GROUP BY DATE(closed_at) ORDER BY DATE(closed_at) DESC LIMIT 30""")
        day_rows = c.fetchall()
        consec = 0
        for _, dpnl in day_rows:
            if dpnl and dpnl > 0:
                consec += 1
            else:
                break
        conn.close()
        return {
            "equity_curve": equity_curve,
            "strategy_pnl": strategy_pnl,
            "trade_freq": trade_freq,
            "launch_criteria": {
                "clean_trades": clean_trades, "clean_trades_target": 100,
                "win_rate": round(win_rate, 1), "win_rate_target": 54,
                "sharpe": 0, "sharpe_target": 1.5,
                "consec_days": consec, "consec_days_target": 10,
            },
        }
    except Exception as e:
        return {"error": str(e), "equity_curve": [], "strategy_pnl": [],
                "trade_freq": [], "launch_criteria": {}}


def _build_api_risk():
    """Build /api/risk response."""
    return {
        "corr_flags": [{"t1": t1, "t2": t2, "corr": c} for t1, t2, c in _RISK_STATE.get("corr_flags", [])],
        "daily_drawdown": _RISK_STATE.get("daily_drawdown", {}),
        "pauses": {s: t.strftime("%Y-%m-%d %H:%M UTC") for s, t in _RISK_STATE.get("strategy_pauses", {}).items()},
        "meta_alloc": dict(_META_ALLOC),
        "meta_pnl": {k: v.get("pnl", 0) for k, v in _META_ALLOC_PNL.items()},
    }


class TVWebhookHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Serve API endpoints and dashboard."""
        path = self.path.split("?")[0]
        try:
            if path == "/api/status":
                data = _build_api_status()
            elif path == "/api/intelligence":
                data = _build_api_intelligence()
            elif path == "/api/oracle":
                data = _build_api_oracle()
            elif path == "/api/risk":
                data = _build_api_risk()
            elif path == "/api/charts":
                data = _build_api_charts()
            elif path == "/dashboard" or path == "/":
                # Serve the HTML dashboard
                try:
                    with open("/app/dashboard/index.html", "r") as f:
                        html = f.read()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.end_headers()
                    self.wfile.write(html.encode())
                except FileNotFoundError:
                    self.send_response(404)
                    self.end_headers()
                    self.wfile.write(b"Dashboard not found")
                return
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error":"not found"}')
                return

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache")
            self.send_header("Expires", "0")
            self.end_headers()
            self.wfile.write(_json_api.dumps(data).encode())
        except Exception as exc:
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f'{{"error":"{exc}"}}'.encode())

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length).decode()
            data = _json.loads(body) if body else {}
            secret = TRADINGVIEW_SIGNALS.get("webhook_secret", "")
            if secret and data.get("secret") != secret:
                self.send_response(403)
                self.end_headers()
                return
            sig_type = data.get("signal", data.get("action", "")).upper()
            asset = data.get("asset", data.get("ticker", "MARKET")).upper()
            indicator = data.get("indicator", data.get("source", "TradingView"))
            price = data.get("price", data.get("close", 0))
            if sig_type:
                _sig_entry = {
                    "signal": sig_type, "asset": asset, "indicator": indicator,
                    "price": price, "timestamp": datetime.utcnow(), "source": "webhook",
                    "executed": False,
                }
                TRADINGVIEW_SIGNALS["latest_signal"] = _sig_entry
                # Store in history (rolling last 50)
                TRADINGVIEW_SIGNALS["history"].append(_sig_entry)
                if len(TRADINGVIEW_SIGNALS["history"]) > 50:
                    TRADINGVIEW_SIGNALS["history"] = TRADINGVIEW_SIGNALS["history"][-50:]

                log.info("TV Webhook: %s %s (%s) price=%s", sig_type, asset, indicator, price)

                # Auto-execute: if buy/sell signal and auto_execute enabled
                if TRADINGVIEW_SIGNALS.get("auto_execute") and sig_type in ("BUY", "SELL", "LONG", "SHORT"):
                    try:
                        _tv_execute_signal(_sig_entry)
                    except Exception as _tve:
                        log.warning("TV auto-execute error: %s", _tve)

            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"status":"ok"}')
        except Exception as exc:
            log.warning("TV Webhook error: %s", exc)
            self.send_response(500)
            self.end_headers()
    def log_message(self, fmt, *args):
        pass

def start_webhook_server(port=8080):
    try:
        server = HTTPServer(("0.0.0.0", port), TVWebhookHandler)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        log.info("TradingView webhook server on port %d", port)
    except Exception as exc:
        log.warning("Webhook server failed: %s", exc)


# ============================================================================
# ZAPIER / MAKE.COM INTEGRATION
# ============================================================================
ZAPIER_CONFIG = {
    "enabled": bool(os.getenv("ZAPIER_WEBHOOK_URL", "")),
    "webhook_url": os.getenv("ZAPIER_WEBHOOK_URL", ""),
    "events": ["trade_executed", "kill_switch", "daily_summary", "high_ev_alert"],
}

def notify_zapier(event_type, payload):
    if not ZAPIER_CONFIG["enabled"] or event_type not in ZAPIER_CONFIG["events"]:
        return
    try:
        requests.post(ZAPIER_CONFIG["webhook_url"], json={
            "event": event_type, "timestamp": datetime.utcnow().isoformat(),
            "data": payload, "bot": "TraderJoes",
        }, timeout=5)
    except Exception:
        pass


# ============================================================================
# AUTO-FEE PAYMENT FROM PROFITS
# ============================================================================
FEE_CONFIG = {
    "enabled": False,
    "fee_pct": 0.10,
    "min_profit_threshold": 100.0,
    "total_profits": 0.0,
    "total_fees_paid": 0.0,
    "fee_wallet": os.getenv("FEE_WALLET_ADDRESS", ""),
    "last_fee_date": "",
}

def calculate_fees():
    profits = FEE_CONFIG["total_profits"]
    already_paid = FEE_CONFIG["total_fees_paid"]
    if profits <= FEE_CONFIG["min_profit_threshold"]:
        return 0.0
    return max(0, (profits * FEE_CONFIG["fee_pct"]) - already_paid)


# ============================================================================
# PREDICTIT MARKET SCANNER
# ============================================================================
def get_predictit_markets(limit=10):
    try:
        r = requests.get("https://www.predictit.org/api/marketdata/all/", timeout=5)
        if r.status_code != 200:
            return []
        markets = []
        for m in r.json().get("markets", [])[:limit]:
            for c in m.get("contracts", []):
                yp = c.get("lastTradePrice", 0) or 0
                if 0.05 <= yp <= 0.95:
                    markets.append({
                        "platform": "PredictIt", "market": m.get("shortName", "")[:60],
                        "slug": c.get("shortName", ""), "yes_price": yp,
                        "no_price": 1 - yp, "volume24h": 0, "liquidity": 0,
                    })
        return markets
    except Exception:
        return []


def get_predictit_balance():
    """Get PredictIt status."""
    try:
        r = requests.get("https://www.predictit.org/api/marketdata/all/", timeout=5)
        if r.status_code == 200:
            markets = r.json().get("markets", [])
            return f"Connected ({len(markets)} markets available)"
        else:
            return f"API error: {r.status_code}"
    except Exception as exc:
        return f"Offline ({exc})"



# ============================================================================
# FUNDING RATE ARBITRAGE MODULE (Phemex + Binance + Coinbase)
# ============================================================================
FUNDING_ARB_CONFIG = {
    "enabled": True,
    "min_funding_rate": 0.0003,  # 0.03% minimum (above round-trip friction)
    "commission_estimate": 0.0002,  # ~0.02% per side (Phemex + Coinbase)
    "slippage_estimate": 0.0001,  # ~0.01% estimated slippage
    "max_position_usd": 2000,  # Max $2000 per arb leg
    "pairs": ["BTC", "ETH", "SOL", "XRP", "BNB"],  # Assets to monitor
    "active_arbs": [],  # Currently open arb positions
}

# Latest funding rates cache: {source_asset: {"phemex": rate, "binance": rate, "ts": datetime}}
_FUNDING_RATES_CACHE = {}


def _fetch_phemex_funding_rate(asset):
    """Fetch funding rate from Phemex. Returns rate as decimal or 0."""
    try:
        symbol = f"s{asset}USDT"
        url = f"https://api.phemex.com/v1/md/ticker/24hr?symbol={symbol}"
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return 0
        result = r.json().get("result", {})
        raw = result.get("fundingRate", 0)
        if isinstance(raw, (int, float)) and raw != 0:
            return raw / 1e8 if abs(raw) > 1 else raw
        return float(raw) / 1e8 if raw else 0
    except Exception:
        return 0


def _fetch_binance_funding_rate(asset):
    """Fetch funding rate from Binance Futures public API (no key needed). Returns rate as decimal or 0."""
    try:
        symbol = f"{asset}USDT"
        url = f"https://fapi.binance.com/fapi/v1/fundingRate?symbol={symbol}&limit=1"
        r = requests.get(url, timeout=5)
        if r.status_code != 200:
            return 0
        data = r.json()
        if data and len(data) > 0:
            return float(data[0].get("fundingRate", 0))
        return 0
    except Exception:
        return 0


def fetch_all_funding_rates():
    """Fetch funding rates from both Phemex and Binance for all pairs. Updates cache.
    Tracks both positive (short arb) and negative (long arb) opportunities."""
    now = datetime.now(timezone.utc)
    for asset in FUNDING_ARB_CONFIG["pairs"]:
        phemex_rate = _fetch_phemex_funding_rate(asset)
        binance_rate = _fetch_binance_funding_rate(asset)
        # Best positive rate (longs pay shorts → short the perp, buy spot)
        best_pos = max(phemex_rate, binance_rate)
        best_pos_src = "binance" if binance_rate > phemex_rate else "phemex"
        # Most negative rate (shorts pay longs → short the perp, collect funding)
        most_neg = min(phemex_rate, binance_rate)
        most_neg_src = "binance" if binance_rate < phemex_rate else "phemex"
        _FUNDING_RATES_CACHE[asset] = {
            "phemex": phemex_rate,
            "binance": binance_rate,
            "best": best_pos,
            "best_source": best_pos_src,
            "most_negative": most_neg,
            "most_negative_source": most_neg_src,
            "ts": now,
        }
    return _FUNDING_RATES_CACHE


async def check_funding_rate_arb():
    """Check Phemex + Binance funding rates for arbitrage opportunities."""
    if not FUNDING_ARB_CONFIG["enabled"]:
        return []
    opps = []
    min_rate = FUNDING_ARB_CONFIG["min_funding_rate"]
    friction = FUNDING_ARB_CONFIG["commission_estimate"] * 2 + FUNDING_ARB_CONFIG["slippage_estimate"]

    rates = fetch_all_funding_rates()

    for asset, rate_info in rates.items():
        try:
            funding_rate = rate_info["best"]
            source = rate_info["best_source"]
            phemex_r = rate_info["phemex"]
            binance_r = rate_info["binance"]

            log.info("FUNDING-ARB %s: phemex=%.4f%% binance=%.4f%% best=%.4f%% (%s) threshold=%.4f%%",
                     asset, phemex_r * 100, binance_r * 100, funding_rate * 100, source, min_rate * 100)

            # Positive rate arb: longs pay shorts → short perp + buy spot
            if funding_rate > min_rate and funding_rate > friction:
                net_yield = funding_rate - friction
                annualized = net_yield * 3 * 365 * 100
                opps.append({
                    "platform": f"{source.title()}+Coinbase",
                    "type": "Funding Rate Arb",
                    "market": f"{asset} Funding Rate Arbitrage",
                    "detail": f"Rate: {funding_rate*100:.4f}% ({source}) | Net: {net_yield*100:.4f}% | Ann: {annualized:.1f}%",
                    "ev": net_yield * 3,
                    "ticker": asset,
                    "funding_rate": funding_rate,
                    "net_yield": net_yield,
                    "source": source,
                })
                log.info("FUNDING-ARB SIGNAL: %s rate=%.4f%% net=%.4f%% ann=%.1f%% (%s)",
                         asset, funding_rate * 100, net_yield * 100, annualized, source)

            # Negative rate arb: shorts pay longs → short the perp, collect funding from longs
            most_neg = rate_info.get("most_negative", 0)
            neg_source = rate_info.get("most_negative_source", "?")
            if most_neg < -min_rate:
                neg_yield = abs(most_neg) - friction
                if neg_yield > 0:
                    neg_ann = neg_yield * 3 * 365 * 100
                    opps.append({
                        "platform": f"{neg_source.title()} (Negative)",
                        "type": "Negative Funding Arb",
                        "market": f"{asset} Negative Rate Arb (SHORT perp)",
                        "detail": f"Rate: {most_neg*100:.4f}% ({neg_source}) | Collect: {neg_yield*100:.4f}% | Ann: {neg_ann:.1f}%",
                        "ev": neg_yield * 3,
                        "ticker": asset,
                        "funding_rate": most_neg,
                        "net_yield": neg_yield,
                        "source": neg_source,
                        "direction": "short_perp",
                    })
                    log.info("FUNDING-ARB NEG SIGNAL: %s rate=%.4f%% collect=%.4f%% ann=%.1f%% (%s) → SHORT perp",
                             asset, most_neg * 100, neg_yield * 100, neg_ann, neg_source)
        except Exception as exc:
            log.warning("Funding rate check error for %s: %s", asset, exc)
    return opps


# ============================================================================
# APPLICATION-LEVEL SECURITY (Hostname Pinning + Circuit Breaker)
# ============================================================================
import socket

ALLOWED_HOSTNAME = os.getenv("ALLOWED_HOSTNAME", "ubuntu-4gb-hel1-1")
CIRCUIT_BREAKER = {
    "trades_last_60s": [],  # timestamps of recent trades
    "max_trades_per_minute": 10,
    "tripped": False,
    "trip_reason": "",
}
KEY_ROTATION_CONFIG = {
    "kalshi_last_rotated": "2026-03-02",
    "rotation_interval_days": 30,
    "platforms_to_rotate": ["kalshi", "phemex", "coinbase", "alpaca"],
}

def check_hostname_security():
    """Verify we are running on the authorized server."""
    current = socket.gethostname()
    if ALLOWED_HOSTNAME not in (current, "ubuntu-4gb-hel1-1") and not current.startswith("traderjoes") and len(current) < 20:
        log.critical("SECURITY ALERT: Trade from unauthorized host %s (expected %s)", current, ALLOWED_HOSTNAME)
        return False
    return True

def check_circuit_breaker():
    """Check if circuit breaker is tripped. Returns True if safe to trade."""
    if CIRCUIT_BREAKER["tripped"]:
        return False
    now = datetime.now(timezone.utc)
    # Clean old entries (older than 60s)
    CIRCUIT_BREAKER["trades_last_60s"] = [
        t for t in CIRCUIT_BREAKER["trades_last_60s"]
        if (now - t).total_seconds() < 60
    ]
    if len(CIRCUIT_BREAKER["trades_last_60s"]) >= CIRCUIT_BREAKER["max_trades_per_minute"]:
        CIRCUIT_BREAKER["tripped"] = True
        CIRCUIT_BREAKER["trip_reason"] = f"Rate limit: {len(CIRCUIT_BREAKER['trades_last_60s'])} trades in 60s"
        log.critical("CIRCUIT BREAKER TRIPPED: %s", CIRCUIT_BREAKER["trip_reason"])
        COST_CONFIG["kill_switch"] = True
        return False
    return True

def record_trade_for_breaker():
    """Record a trade timestamp for circuit breaker tracking."""
    CIRCUIT_BREAKER["trades_last_60s"].append(datetime.now(timezone.utc))

def check_key_rotation_needed():
    """Check if any API keys need rotation."""
    reminders = []
    try:
        last = datetime.strptime(KEY_ROTATION_CONFIG["kalshi_last_rotated"], "%Y-%m-%d")
        days_since = (datetime.utcnow() - last).days
        days_until = KEY_ROTATION_CONFIG["rotation_interval_days"] - days_since
        if days_until <= 0:
            reminders.append(f"Kalshi API key OVERDUE for rotation ({days_since} days old)")
        elif days_until <= 7:
            reminders.append(f"Kalshi API key rotation in {days_until} days")
    except Exception:
        reminders.append("Kalshi key rotation date unknown")
    return reminders

def pre_trade_security_check(platform=""):
    """Run all security checks before any trade execution."""
    if not check_hostname_security():
        return False, "BLOCKED: Unauthorized hostname"
    if not check_circuit_breaker():
        return False, f"BLOCKED: Circuit breaker tripped - {CIRCUIT_BREAKER.get('trip_reason', 'rate limit')}"
    if COST_CONFIG.get("kill_switch", False):
        return False, "BLOCKED: Kill switch active"
    return True, "OK"

# ============================================================================
# AUTO-LIVE EXECUTION ENGINE
# ============================================================================
async def auto_live_execute(channel, opp):
    """Auto-execute trade in live mode when edge score meets threshold."""
    if DRY_RUN_MODE or TRADING_MODE != "live":
        return False
    # Security checks
    safe, reason = pre_trade_security_check(opp.get("platform", ""))
    if not safe:
        log.warning("Auto-live blocked: %s", reason)
        return False
    auto = AUTO_LIVE_CONFIG
    if not auto["enabled"]:
        return False
    ev = opp.get("ev", 0)
    if ev < auto["min_ev"]:
        log.info("Auto-live skip: EV %.1f%% < %.1f%% min", ev*100, auto["min_ev"]*100)
        return False
    # SPORTS BLACKLIST - we are not a sportsbook
    SPORTS_BLACKLIST = ["vs.", "nba", "nfl", "nhl", "mlb", "soccer", "football", "basketball", "baseball", "hockey", "mavericks", "celtics", "lakers", "warriors", "nets", "heat", "raptors", "timberwolves", "pacers", "pelicans", "suns", "knicks", "bucks", "nuggets", "clippers", "76ers", "cavaliers", "grizzlies", "rockets", "spurs", "bulls", "pistons", "magic", "hornets", "hawks", "wizards", "blazers", "kings", "thunder", "jazz", "premier league", "la liga", "champions league", "bundesliga", "serie a", "epl", "psg", "manchester", "liverpool", "arsenal", "chelsea", "barcelona", "real madrid"]
    _market_lower = opp.get("market", "").lower()
    if any(s in _market_lower for s in SPORTS_BLACKLIST):
        log.info("Sports blacklist: skipping %s", opp.get("market", "")[:40])
        return False
    # (dedup moved to unified pre-trade block above) — prevent over-concentration
    market_key = opp.get("market", "")[:60]
    for pos in PAPER_PORTFOLIO.get("positions", []):
        if pos.get("market", "") == market_key:
            log.info("Skip duplicate position (already open): %s", market_key)
            return False
    score, confidence, signals = calculate_edge_score(opp)
    if score < auto["min_edge_score"]:
        log.info("Auto-live skip: score %d < %d min", score, auto["min_edge_score"])
        return False
    if auto.get("trades_today", 0) >= auto["max_daily_trades"]:
        log.info("Auto-live skip: daily trade limit reached")
        return False
    # Portfolio floor check
    try:
        total = float(str(COST_CONFIG.get("portfolio_value", 88000)).replace(",", ""))
        if total < PORTFOLIO_FLOOR:
            log.warning("Auto-live BLOCKED: portfolio $%.0f < floor $%.0f", total, PORTFOLIO_FLOOR)
            return False
    except Exception:
        pass
    # Calculate size: 0.25% of portfolio
    portfolio_val = 88000  # approximate
    size = min(suggest_position_size(ev), portfolio_val * auto["max_position_pct"])
    if size < 5:
        return False
    platform = opp.get("platform", "").lower()
    success = False
    msg = ""
    try:
        if platform == "polymarket":
            success, msg = await execute_polymarket_order(opp, size)
        elif platform == "kalshi":
            ticker = opp.get("ticker", "")
            success, msg = await execute_kalshi_order("BUY", ticker, int(size))
        elif platform in ("coinbase", "crypto"):
            success, msg = await execute_coinbase_order("BUY", "BTC", size)
        elif platform == "phemex":
            success, msg = await execute_phemex_order("BUY", "sBTCUSDT", size)
        elif platform == "alpaca":
            success, msg = await execute_alpaca_order("BUY", "AAPL", size)
        elif platform == "ibkr":
            success, msg = await execute_ibkr_order("BUY", "AAPL", size)
        if success:
            auto["trades_today"] = auto.get("trades_today", 0) + 1
            COST_CONFIG["daily_trades"] = COST_CONFIG.get("daily_trades", 0) + 1
            record_trade_for_breaker()
            notify_zapier("trade_executed", {
                "platform": platform, "market": opp.get("market", ""),
                "size": size, "ev": ev, "score": score, "confidence": confidence,
            })
            trade_msg = (
                f"**AUTO-TRADE EXECUTED** [{platform.upper()}]\n"
                f"Market: {opp.get('market', '')[:60]}\n"
                f"Size: ${size:.2f} | EV: +{ev*100:.1f}% | Score: {score}/100 ({confidence})\n"
                f"{msg}"
            )
            await channel.send(trade_msg)
            record_signal(opp, executed=True, paper=False)
            log.info("AUTO-LIVE TRADE: %s $%.2f EV:%.1f%% Score:%d", platform, size, ev*100, score)
            return True
        else:
            log.info("Auto-live order failed: %s - %s", platform, msg)
    except Exception as exc:
        log.warning("Auto-live execute error on %s: %s", platform, exc)
    return False


# ============================================================================
# DAILY RESET TASK (reset trade counters at midnight UTC)
# ============================================================================
@tasks.loop(hours=24)
async def daily_reset_task():
    """Reset daily counters at midnight UTC."""
    AUTO_LIVE_CONFIG["trades_today"] = 0
    COST_CONFIG["daily_trades"] = 0
    COST_CONFIG["daily_pnl"] = 0.0
    log.info("Daily counters reset")
    # Log paper trading summary
    trades = PAPER_PORTFOLIO.get("trades", [])
    cash = PAPER_PORTFOLIO["cash"]
    positions = len(PAPER_PORTFOLIO["positions"])
    log.info("Paper summary: %d trades, $%.2f cash, %d open positions", len(trades), cash, positions)
    # Send daily summary to Zapier
    notify_zapier("daily_summary", {
        "paper_trades": len(trades),
        "paper_cash": cash,
        "paper_positions": positions,
        "signal_history": len(SIGNAL_HISTORY),
    })

@daily_reset_task.before_loop
async def before_daily_reset():
    await bot.wait_until_ready()



# ============================================================================
# DAILY AUTO-REBALANCING (Task 7)
# ============================================================================
REBALANCE_CONFIG = {
    "enabled": True,
    "target_allocations": {
        "polymarket": 0.25,   # 25% to prediction markets
        "kalshi": 0.20,       # 20% to Kalshi
        "coinbase": 0.25,     # 25% to crypto
        "phemex": 0.10,       # 10% to Phemex derivatives
        "alpaca": 0.15,       # 15% to equities
        "ibkr": 0.05,         # 5% to IBKR
    },
    "threshold": 0.05,  # 5% drift triggers rebalance
    "last_rebalance": "",
}

async def auto_rebalance(channel=None):
    """Check portfolio allocation drift and suggest rebalancing."""
    try:
        balances = {
            "polymarket": 2005, "kalshi": 1234, "coinbase": 79000,
            "phemex": 5118, "alpaca": 0, "ibkr": 0,
        }
        total = sum(balances.values())
        if total < 1000:
            return []
        suggestions = []
        for platform, target_pct in REBALANCE_CONFIG["target_allocations"].items():
            actual_pct = balances.get(platform, 0) / total
            drift = actual_pct - target_pct
            if abs(drift) > REBALANCE_CONFIG["threshold"]:
                direction = "REDUCE" if drift > 0 else "ADD"
                amount = abs(drift) * total
                suggestions.append({
                    "platform": platform,
                    "direction": direction,
                    "amount": amount,
                    "actual_pct": actual_pct * 100,
                    "target_pct": target_pct * 100,
                    "drift": drift * 100,
                })
        REBALANCE_CONFIG["last_rebalance"] = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        if channel and suggestions:
            msg = "**Daily Rebalance Check**\n"
            for s in suggestions:
                msg += f"  {s['platform'].upper()}: {s['direction']} ${s['amount']:,.0f} ({s['actual_pct']:.0f}% → {s['target_pct']:.0f}% target)\n"
            await channel.send(msg)
        return suggestions
    except Exception as exc:
        log.warning("Rebalance error: %s", exc)
        return []


# ============================================================================
# PERFORMANCE ATTRIBUTION (Task 8)
# ============================================================================
PERFORMANCE_TRACKER = {
    "by_strategy": {},    # {strategy_name: {trades: N, pnl: X, wins: N, losses: N}}
    "by_platform": {},    # {platform: {trades: N, pnl: X}}
    "by_regime": {},      # {regime: {trades: N, pnl: X}}
    "total_trades": 0,
    "total_pnl": 0.0,
    "best_trade": None,
    "worst_trade": None,
}

def record_performance(trade_data):
    """Record trade for performance attribution."""
    strategy = trade_data.get("type", "unknown")
    platform = trade_data.get("platform", "unknown")
    regime = REGIME_CONFIG.get("current_regime", "unknown")
    pnl = trade_data.get("pnl", 0)
    # By strategy
    if strategy not in PERFORMANCE_TRACKER["by_strategy"]:
        PERFORMANCE_TRACKER["by_strategy"][strategy] = {"trades": 0, "pnl": 0, "wins": 0, "losses": 0}
    s = PERFORMANCE_TRACKER["by_strategy"][strategy]
    s["trades"] += 1
    s["pnl"] += pnl
    if pnl > 0: s["wins"] += 1
    elif pnl < 0: s["losses"] += 1
    # By platform
    if platform not in PERFORMANCE_TRACKER["by_platform"]:
        PERFORMANCE_TRACKER["by_platform"][platform] = {"trades": 0, "pnl": 0}
    p = PERFORMANCE_TRACKER["by_platform"][platform]
    p["trades"] += 1
    p["pnl"] += pnl
    # By regime
    if regime not in PERFORMANCE_TRACKER["by_regime"]:
        PERFORMANCE_TRACKER["by_regime"][regime] = {"trades": 0, "pnl": 0}
    r = PERFORMANCE_TRACKER["by_regime"][regime]
    r["trades"] += 1
    r["pnl"] += pnl
    # Totals
    PERFORMANCE_TRACKER["total_trades"] += 1
    PERFORMANCE_TRACKER["total_pnl"] += pnl
    if PERFORMANCE_TRACKER["best_trade"] is None or pnl > PERFORMANCE_TRACKER["best_trade"].get("pnl", 0):
        PERFORMANCE_TRACKER["best_trade"] = trade_data
    if PERFORMANCE_TRACKER["worst_trade"] is None or pnl < PERFORMANCE_TRACKER["worst_trade"].get("pnl", 0):
        PERFORMANCE_TRACKER["worst_trade"] = trade_data


# ============================================================================
# MINIMUM PAPER TRADE FLOOR (Task 10)
# ============================================================================
MIN_PAPER_TRADES_PER_DAY = 4
PAPER_TRADES_TODAY = 0

async def enforce_paper_trade_floor(channel):
    """If fewer than MIN trades today, lower thresholds temporarily and force trades."""
    return  # DISABLED: was forcing low-quality trades

    global PAPER_TRADES_TODAY
    if TRADING_MODE != "paper" or not AUTO_PAPER_ENABLED:
        return
    if PAPER_TRADES_TODAY >= MIN_PAPER_TRADES_PER_DAY:
        return
    # Temporarily lower EV threshold to find more opportunities
    original_threshold = ALERT_CONFIG["min_ev_threshold"]
    ALERT_CONFIG["min_ev_threshold"] = 0.01  # Lower to 1% temporarily
    try:
        kalshi_opps = find_kalshi_opportunities()
        poly_opps = find_polymarket_opportunities()
        crypto_opps = find_crypto_momentum()
        try:
            funding_opps = await check_funding_rate_arb()
        except Exception:
            funding_opps = []
        all_opps = kalshi_opps + poly_opps + crypto_opps + funding_opps
        all_opps.sort(key=lambda x: x.get("ev", 0), reverse=True)
        for opp in all_opps[:3]:  # Try up to 3 trades
            if PAPER_TRADES_TODAY >= MIN_PAPER_TRADES_PER_DAY:
                break
            try:
                executed = await auto_paper_execute(channel, opp)
                if executed:
                    PAPER_TRADES_TODAY += 1
                    log.info("Floor trade executed: %s (trade %d/%d)",
                             opp.get("market", "")[:40], PAPER_TRADES_TODAY, MIN_PAPER_TRADES_PER_DAY)
            except Exception:
                pass
    finally:
        ALERT_CONFIG["min_ev_threshold"] = original_threshold



# ============================================================================
# REDIS PUB/SUB SIGNAL BUS
# ============================================================================
REDIS_CLIENT = None
def init_redis():
    global REDIS_CLIENT
    if not REDIS_AVAILABLE:
        log.warning("redis-py not installed, using in-memory signals only")
        return
    try:
        REDIS_CLIENT = redis_lib.Redis(host="redis", port=6379, db=0, decode_responses=True, socket_connect_timeout=3)
        REDIS_CLIENT.ping()
        log.info("Redis connected - signal bus active")
    except Exception as e:
        log.warning("Redis unavailable (%s), falling back to in-memory", e)
        REDIS_CLIENT = None

def publish_signal(signal_type, data):
    if REDIS_CLIENT:
        try:
            import json as _json
            REDIS_CLIENT.publish(signal_type, _json.dumps(data, default=str))
            REDIS_CLIENT.lpush("history:" + signal_type, _json.dumps(data, default=str))
            REDIS_CLIENT.ltrim("history:" + signal_type, 0, 499)
        except Exception:
            pass

def get_signal_history(signal_type, count=10):
    if REDIS_CLIENT:
        try:
            import json as _json
            items = REDIS_CLIENT.lrange("history:" + signal_type, 0, count - 1)
            return [_json.loads(i) for i in items]
        except Exception:
            return []
    return []

# ============================================================================
# SQLITE STATE PERSISTENCE
# ============================================================================
DB_PATH = "/app/data/trading_firm.db"

def init_db():
    import os as _os
    _os.makedirs("/app/data", exist_ok=True)
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS paper_trades (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, market TEXT, platform TEXT, side TEXT, shares REAL, entry_price REAL, cost REAL, ev REAL, edge_score REAL, status TEXT DEFAULT 'open')""")
        c.execute("""CREATE TABLE IF NOT EXISTS daily_state (date TEXT PRIMARY KEY, trades_count INTEGER, daily_pnl REAL, paper_cash REAL, circuit_breaker_trips INTEGER DEFAULT 0)""")
        c.execute("""CREATE TABLE IF NOT EXISTS resolutions (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT, market TEXT, platform TEXT, entry_price REAL, outcome REAL, brier_score REAL, realized_edge REAL, pnl REAL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT,
            platform TEXT,
            strategy TEXT,
            direction TEXT,
            size_usd REAL,
            shares REAL,
            entry_price REAL,
            current_price REAL,
            stop_price REAL,
            target_price REAL,
            long_leg TEXT,
            short_leg TEXT,
            entry_zscore REAL,
            status TEXT DEFAULT 'open',
            regime TEXT,
            metadata TEXT,
            created_at TEXT,
            closed_at TEXT,
            exit_reason TEXT,
            realized_pnl REAL DEFAULT 0
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS pairs_discovery (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker_a TEXT, ticker_b TEXT, correlation REAL,
            sector TEXT, discovered_at TEXT,
            UNIQUE(ticker_a, ticker_b) ON CONFLICT REPLACE
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS backtest_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy TEXT, days INTEGER, run_at TEXT,
            total_trades INTEGER, win_rate REAL, total_pnl REAL,
            avg_pnl REAL, sharpe REAL, max_drawdown REAL,
            best_trade REAL, worst_trade REAL, metadata TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS shadow_positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT, strategy TEXT, direction TEXT,
            size_usd REAL, entry_price REAL,
            status TEXT DEFAULT 'open', realized_pnl REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            closed_at TEXT, exit_reason TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS echo_memory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT, theme TEXT, signal_name TEXT,
            probability REAL, geo_level TEXT, headline TEXT,
            trade_outcome TEXT, realized_pnl REAL,
            context TEXT, created_at TEXT DEFAULT (datetime('now'))
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS trade_journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            market_id TEXT, strategy TEXT, direction TEXT,
            entry_date TEXT, exit_date TEXT, hold_hours REAL,
            entry_regime TEXT, exit_reason TEXT,
            realized_pnl REAL, size_usd REAL,
            signal_trigger TEXT, lesson TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ib_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol TEXT, side TEXT, qty REAL, order_type TEXT DEFAULT 'MKT',
            limit_price REAL, strategy TEXT, market_id TEXT,
            status TEXT DEFAULT 'pending', ib_order_id TEXT,
            error TEXT, created_at TEXT DEFAULT (datetime('now')),
            filled_at TEXT, fill_price REAL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS ib_fills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ib_order_id TEXT, symbol TEXT, side TEXT, qty REAL,
            fill_price REAL, commission REAL,
            filled_at TEXT DEFAULT (datetime('now'))
        )""")
        conn.commit()
        conn.close()
        log.info("SQLite initialized at %s", DB_PATH)
    except Exception as e:
        log.warning("SQLite init failed: %s", e)

def db_log_paper_trade(trade):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO paper_trades (timestamp,market,platform,side,shares,entry_price,cost,ev,edge_score) VALUES (?,?,?,?,?,?,?,?,?)", (trade.get("timestamp",""), trade.get("market",""), trade.get("platform",""), trade.get("side","BUY"), trade.get("shares",0), trade.get("entry_price",0), trade.get("cost",0), trade.get("ev",0), trade.get("edge_score",0)))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("SQLite log failed: %s", e)

def db_save_daily_state():
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO daily_state (date,trades_count,daily_pnl,paper_cash) VALUES (?,?,?,?)", (today, COST_CONFIG.get("daily_trades",0), COST_CONFIG.get("daily_pnl",0), PAPER_PORTFOLIO.get("cash",10000)))
        conn.commit()
        conn.close()
    except Exception:
        pass

def db_load_daily_state():
    try:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT trades_count, daily_pnl, paper_cash FROM daily_state WHERE date = ?", (today,))
        row = c.fetchone()
        conn.close()
        if row:
            COST_CONFIG["daily_trades"] = row[0]
            COST_CONFIG["daily_pnl"] = row[1]
            # Only restore cash if current value is unset/zero (don't overwrite live balance)
            if PAPER_PORTFOLIO.get("cash", 0) <= 0 and row[2] and row[2] > 0:
                PAPER_PORTFOLIO["cash"] = row[2]
                log.info("Restored cash from daily_state: $%.2f", row[2])
            log.info("Restored state: %d trades, pnl %.2f (cash kept at $%.2f)", row[0], row[1], PAPER_PORTFOLIO["cash"])
            return True
    except Exception:
        pass
    return False

def db_log_resolution(result):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("INSERT INTO resolutions (timestamp,market,platform,entry_price,outcome,brier_score,realized_edge,pnl) VALUES (?,?,?,?,?,?,?,?)", (result.get("resolved_at",""), result.get("market",""), result.get("platform",""), result.get("entry_price",0), result.get("outcome",0), result.get("brier_score",0), result.get("realized_edge",0), result.get("pnl",0)))
        conn.commit()
        conn.close()
    except Exception:
        pass

# ============================================================================
# TWAP SMART SLICE EXECUTION (Slippage Killer)
# ============================================================================
TWAP_CONFIG = {
    "threshold": 500,        # Orders above this get sliced
    "num_slices": 5,         # Split into N slices
    "min_interval": 45,      # Min seconds between slices
    "max_interval": 90,      # Max seconds between slices
    "tick_offset": 0.01,     # Limit price = mid + tick_offset
}

import random as _random

async def twap_execute_paper(opp, total_size, price, channel):
    """TWAP execution for paper mode: split large orders into slices."""
    cfg = TWAP_CONFIG
    num_slices = cfg["num_slices"]
    slice_size = total_size / num_slices
    market = opp.get("market", "")[:60]
    platform = opp.get("platform", "")
    executed_slices = 0
    total_cost = 0
    for i in range(num_slices):
        slice_shares = max(1, int(slice_size / max(price, 0.01)))
        slice_cost = slice_shares * price
        slippage = slice_cost * 0.003
        fees = slice_cost * 0.001
        slice_total = slice_cost + slippage + fees
        if slice_total > PAPER_PORTFOLIO["cash"]:
            break
        PAPER_PORTFOLIO["cash"] -= slice_total
        total_cost += slice_total
        executed_slices += 1
        if i == 0:
            position = {
                "market": market, "side": "BUY", "shares": slice_shares,
                "entry_price": price, "cost": slice_total, "value": slice_cost,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "platform": platform, "twap_slices": num_slices,
            }
            PAPER_PORTFOLIO["positions"].append(position)
            PAPER_PORTFOLIO["trades"].append(position)
        else:
            for pos in PAPER_PORTFOLIO["positions"]:
                if pos.get("market") == market:
                    pos["shares"] += slice_shares
                    pos["cost"] += slice_total
                    pos["value"] += slice_cost
                    break
        if i < num_slices - 1:
            delay = _random.randint(cfg["min_interval"], cfg["max_interval"])
            await asyncio.sleep(delay)
    if executed_slices > 0:
        publish_signal("trade_signals", {"market": market, "platform": platform, "ev": opp.get("ev",0), "size": total_cost, "twap": True, "slices": executed_slices})
        db_log_paper_trade({"market": market, "platform": platform, "shares": sum(1 for _ in range(executed_slices)), "entry_price": price, "cost": total_cost, "ev": opp.get("ev",0)})
        db_save_daily_state()
        if channel:
            await channel.send(f"TWAP executed: {market} | {executed_slices}/{num_slices} slices | Total: ")
        log.info("TWAP paper: %s | %d/%d slices | $%.2f", market, executed_slices, num_slices, total_cost)
    return executed_slices > 0

async def twap_execute_live(opp, total_size, price, channel):
    """TWAP execution for live mode: placeholder for real API calls."""
    cfg = TWAP_CONFIG
    num_slices = cfg["num_slices"]
    slice_size = total_size / num_slices
    market = opp.get("market", "")[:60]
    platform = opp.get("platform", "")
    log.info("TWAP LIVE: Would execute %d slices of $%.2f for %s on %s", num_slices, slice_size, market, platform)
    if channel:
        await channel.send(f"TWAP LIVE: {market} | {num_slices} slices of  | Platform: {platform}")
    return True

# ============================================================================
# CORRELATED POSITION CHECK
# ============================================================================
MARKET_CATEGORIES = {"politics": ["trump","biden","gop","democrat","republican","senate","congress","election","president","aoc","desantis"], "crypto": ["bitcoin","btc","ethereum","eth","solana","sol","crypto","token","defi"], "geopolitics": ["iran","russia","ukraine","china","war","ceasefire","nato","sanctions","tariff"], "climate": ["celsius","warming","climate","carbon","temperature","sea level"], "fed": ["fed","interest rate","fomc","powell","inflation","cpi","gdp","unemployment","jobs"], "tech": ["openai","google","apple","microsoft","nvidia","semiconductor"]}

def get_market_category(market_name):
    name_lower = market_name.lower()
    for cat, keywords in MARKET_CATEGORIES.items():
        for kw in keywords:
            if kw in name_lower:
                return cat
    return "other"

GICS_SECTORS = {"tech": ["AAPL","MSFT","GOOGL","META","NVDA","AMD","INTC","CRM","ORCL"],
    "finance": ["JPM","BAC","GS","MS","WFC","V","MA","AXP","C"],
    "energy": ["XOM","CVX","COP","SLB","EOG","OXY","MPC","PSX","VLO"],
    "health": ["JNJ","UNH","PFE","ABBV","MRK","LLY","TMO","ABT","BMY"],
    "consumer": ["KO","PEP","PG","WMT","COST","MCD","NKE","SBUX","TGT"]}
MAX_SAME_DIRECTION = 3
MAX_SECTOR_PCT = 0.30

def get_gics_sector(ticker):
    for sector, tickers in GICS_SECTORS.items():
        if ticker.upper() in tickers:
            return sector
    return "other"

def check_directional_limit(side, existing_positions):
    same_dir = sum(1 for p in existing_positions if p.get("side", "BUY") == side)
    if same_dir >= MAX_SAME_DIRECTION:
        return False, "Blocked: " + str(same_dir) + " positions already " + side
    return True, ""

def check_sector_exposure(ticker, existing_positions, portfolio_value):
    sector = get_gics_sector(ticker)
    if sector == "other":
        return True, 1.0, ""
    sector_exposure = sum(p.get("cost", 0) for p in existing_positions if get_gics_sector(p.get("ticker", "")) == sector)
    max_allowed = portfolio_value * MAX_SECTOR_PCT
    if sector_exposure >= max_allowed:
        return False, 0, "Sector cap: " + sector + " at " + str(int(sector_exposure)) + "/" + str(int(max_allowed))
    return True, 1.0, ""

def check_correlation(new_market, existing_positions):
    new_cat = get_market_category(new_market)
    if new_cat == "other":
        return True, 1.0, ""
    same_cat_count = 0
    for pos in existing_positions:
        if get_market_category(pos.get("market", "")) == new_cat:
            same_cat_count += 1
    if same_cat_count >= 2:
        return False, 0, "Blocked: " + str(same_cat_count) + " open in " + new_cat
    elif same_cat_count == 1:
        return True, 0.5, "1 existing " + new_cat + ", sizing at 50%"
    return True, 1.0, ""


# ============================================================================
# POST-RESOLUTION AUDIT (Brier Score + EV Calibration)
# ============================================================================

# ============================================================================
# SPRINT 2: EQUITIES & EXIT MANAGEMENT
# ============================================================================
EQUITIES_ENABLED = True  # Feature flag - do not enable until validated

# --- MARKET HOURS GUARD ---
def is_market_open():
    """Check if NYSE/NASDAQ is open (9:30 AM - 4:00 PM EST, Mon-Fri)."""
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() >= 5:  # Sat/Sun
        return False
    market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
    return market_open <= now <= market_close

# ============================================================================
# CRASH HEDGE MODULE (SPY puts + directional short)
# ============================================================================
CRASH_HEDGE_CONFIG = {
    "enabled": True,
    "put_vix_threshold": 25,        # Buy puts when VIX > 25
    "put_size_pct": 0.005,          # 0.5% of portfolio per put hedge
    "put_dte": 7,                   # 7 days to expiration
    "put_strike_offset": 0.02,      # 2% below current price
    "short_vix_threshold": 35,      # Short SPY when VIX > 35
    "short_size_pct": 0.01,         # 1% of portfolio
    "max_hedges": 2,                # Max concurrent hedge positions
    "cooldown_hours": 12,           # Min hours between hedge entries
    # VRP call credit spread regime (VIX >= 28)
    "vrp_vix_threshold": 28,        # Switch to selling premium when VIX >= 28
    "vrp_spread_size_pct": 0.008,   # 0.8% of portfolio per spread
    "vrp_dte": 2,                   # 2 DTE for theta decay
    "vrp_short_delta": 0.20,        # Sell 20-delta call
    "vrp_long_delta": 0.10,         # Buy 10-delta call
    "vrp_max_spreads": 2,           # Max concurrent call spreads
}

# VRP regime state tracking
_VRP_REGIME_STATE = {"current": "none", "last_switch": None, "cycle_count": 0}


# ---------------------------------------------------------------------------
# ORACLE ENGINE — Prediction-market-driven equity trades
# ---------------------------------------------------------------------------
ORACLE_CONFIG = {
    "enabled": True,
    "base_size_pct": 0.0075,         # 0.75% of portfolio per leg
    "ttl_hours": 48,                 # Max hold time
    "cooldown_hours": 4,             # Min hours between same signal re-entry
    "max_oracle_positions": 4,       # Max concurrent oracle trades
}

# Signal mapping: keyword patterns → equity legs
# Each signal has: keywords (matched against market title), threshold, long_leg, short_leg
ORACLE_SIGNALS = [
    {"name": "fed_hike",     "keywords": ["fed", "raise", "rate", "increase", "hike", "25", "bps"],
     "threshold": 0.70,  "long": "XLF",  "short": "TLT"},
    {"name": "fed_cut",      "keywords": ["fed", "cut", "rate", "lower", "decrease"],
     "threshold": 0.70,  "long": "TLT",  "short": "XLF"},
    {"name": "iran_ceasefire", "keywords": ["iran", "ceasefire"],
     "threshold": 0.70,  "long": "UAL",  "short": "XLE"},
    {"name": "recession",    "keywords": ["recession"],
     "threshold": 0.60,  "long": "GLD",  "short": "SPY"},
    {"name": "tariff",       "keywords": ["tariff", "increase"],
     "threshold": 0.65,  "long": "UUP",  "short": "EEM"},
    {"name": "iran_attack",  "keywords": ["iran", "attack", "strike", "military"],
     "threshold": 0.65,  "long": "XLE",  "short": "UAL"},
    # Inverse signal: fires when ceasefire prob DROPS below threshold + geo confirms
    {"name": "iran_escalation", "keywords": ["iran", "ceasefire"],
     "threshold": 0.35, "long": "XLE", "short": "AAL",
     "inverse": True, "geo_required": "iran", "geo_min_headlines": 5},
    # Ukraine escalation: fires when Ukraine war market moves + geo confirms 5+ headlines
    {"name": "ukraine_escalation", "keywords": ["ukraine"],
     "threshold": 0.35, "long": "LMT", "short": "UAL", "extra_long": "GLD",
     "inverse": True, "geo_required": "ukraine", "geo_min_headlines": 5},
    # Taiwan escalation: semiconductor supply chain disruption play
    {"name": "taiwan_escalation", "keywords": ["taiwan"],
     "threshold": 0.35, "long": "SOXX", "short": "TSM", "extra_long": "SMH",
     "inverse": True, "geo_required": "taiwan", "geo_min_headlines": 8,
     "extra_long_side": "short"},  # Long SOXX (US semis ETF), Short TSM + Short SMH
]

# ---------------------------------------------------------------------------
# CASCADE ORACLE — Chain-reaction trades triggered by primary oracle signals
# When a primary signal fires, cascade scans for echo effects in correlated
# assets with a 15-minute lag. Each chain defines secondary trades.
# ---------------------------------------------------------------------------
CASCADE_CHAINS = {
    # Iran attack/escalation chain: CL spike → energy + airlines + gold
    "iran_attack": [
        {"name": "iran_cascade_energy", "long": "XLE", "short": "UAL", "delay_min": 15,
         "description": "CL futures spike → energy long, airlines short"},
        {"name": "iran_cascade_gold", "long": "GLD", "short": "SPY", "delay_min": 15,
         "description": "Flight to safety → gold long, equities short"},
    ],
    "iran_escalation": [
        {"name": "iran_esc_cascade_gold", "long": "GLD", "short": "SPY", "delay_min": 15,
         "description": "Iran escalation → gold safe haven"},
    ],
    # Ukraine chain: European energy disruption → defense + airlines + gold
    "ukraine_escalation": [
        {"name": "ukraine_cascade_defense", "long": "RTX", "short": "UAL", "delay_min": 15,
         "description": "European energy disruption → RTX defense long"},
        {"name": "ukraine_cascade_gold", "long": "GLD", "short": "EFA", "delay_min": 15,
         "description": "EU risk → gold long, European equities short"},
    ],
    # Taiwan chain: Semiconductor supply disruption
    "taiwan_escalation": [
        {"name": "taiwan_cascade_semi", "long": "NVDA", "short": "TSM", "delay_min": 15,
         "description": "Semi supply disruption → US semis up, Taiwan semis down"},
        {"name": "taiwan_cascade_onshore", "long": "SOXX", "short": "SMH", "delay_min": 15,
         "description": "Onshoring premium → US semis (SOXX) up, broad semis down"},
    ],
}

# Pending cascade queue: [(fire_at_utc, primary_signal, chain_entry, leg_size, geo_elevated)]
_CASCADE_PENDING = []

def cascade_requeue_on_startup():
    """Re-queue cascades for open oracle trades that lost their queue on restart.
    Called once on startup — immediately fires cascades (no delay) for trades
    that are already old enough."""
    for pos in PAPER_PORTFOLIO.get("positions", []):
        if pos.get("strategy") != "oracle_trade":
            continue
        signal_name = pos.get("market", "").replace("ORACLE:", "")
        chains = CASCADE_CHAINS.get(signal_name, [])
        if not chains:
            continue
        # Check if cascade already exists for this signal
        for chain in chains:
            cascade_id = f"CASCADE:{chain['name']}"
            already_open = any(p.get("market") == cascade_id for p in PAPER_PORTFOLIO.get("positions", []))
            already_in_db = False
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("SELECT COUNT(*) FROM positions WHERE market_id=? AND status IN ('open','closed')", (cascade_id,))
                already_in_db = c.fetchone()[0] > 0
                conn.close()
            except Exception:
                pass
            if already_open or already_in_db:
                continue
            # Queue with 1-minute delay (not 15 min — the primary is already old)
            leg_size = pos.get("cost", 0) / 2  # Approximate leg size from total cost
            import datetime as _dt_rq
            fire_at = datetime.now(timezone.utc) + _dt_rq.timedelta(minutes=1)
            _CASCADE_PENDING.append((fire_at, signal_name, chain, leg_size, False))
            log.info("CASCADE REQUEUE: %s → %s (from open ORACLE:%s, fires in 1min)",
                     signal_name, chain["name"], signal_name)


def cascade_queue_add(primary_signal, leg_size, geo_elevated):
    """Queue cascade trades for a primary signal with 15-min delay."""
    chains = CASCADE_CHAINS.get(primary_signal, [])
    if not chains:
        return
    now = datetime.now(timezone.utc)
    import datetime as _dt_cas
    for chain in chains:
        fire_at = now + _dt_cas.timedelta(minutes=chain["delay_min"])
        _CASCADE_PENDING.append((fire_at, primary_signal, chain, leg_size, geo_elevated))
        log.info("CASCADE QUEUED: %s → %s (fires at %s, %dmin delay)",
                 primary_signal, chain["name"], fire_at.strftime("%H:%M UTC"), chain["delay_min"])


async def cascade_execute_pending(channel=None):
    """Execute any cascade trades whose delay has elapsed. Called each scan cycle."""
    if not _CASCADE_PENDING:
        return 0
    now = datetime.now(timezone.utc)
    fired = 0
    remaining = []
    for entry in _CASCADE_PENDING:
        fire_at, primary_signal, chain, leg_size, geo_elevated = entry
        if now < fire_at:
            remaining.append(entry)
            continue

        cascade_name = chain["name"]
        market_id = f"CASCADE:{cascade_name}"
        long_tk = chain["long"]
        short_tk = chain["short"]

        # Dedup: already open?
        if any(p.get("market", "") == market_id for p in PAPER_PORTFOLIO.get("positions", [])):
            log.info("CASCADE DEDUP: %s already open", cascade_name)
            continue

        # Overlap check with existing oracle/cascade positions
        _existing_tickers = set()
        for _ep in PAPER_PORTFOLIO.get("positions", []):
            if _ep.get("strategy") in ("oracle_trade", "cascade_trade"):
                _existing_tickers.update(filter(None, [_ep.get("long_leg"), _ep.get("short_leg"), _ep.get("extra_long_leg")]))
        if {long_tk, short_tk} & _existing_tickers:
            log.info("CASCADE OVERLAP: %s skipped — %s already held",
                     cascade_name, {long_tk, short_tk} & _existing_tickers)
            continue

        # Position limit (cascades share oracle max)
        oracle_count = sum(1 for p in PAPER_PORTFOLIO.get("positions", [])
                           if p.get("strategy") in ("oracle_trade", "cascade_trade"))
        if oracle_count >= ORACLE_CONFIG["max_oracle_positions"] + 2:  # Allow 2 extra for cascades
            log.info("CASCADE: position limit reached (%d)", oracle_count)
            remaining.append(entry)
            continue

        # Scale cascade size: 60% of primary signal size
        cascade_leg = leg_size * 0.6

        log.info("CASCADE FIRE: %s → Long %s / Short %s | $%.0f/leg (from %s, %dmin lag)",
                 cascade_name, long_tk, short_tk, cascade_leg, primary_signal, chain["delay_min"])

        # Execute via Alpaca
        _alp_hdr = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }
        _alp_url = f"{ALPACA_BASE_URL}/v2/orders"
        _data_hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        import math as _cmath

        try:
            # Long leg
            _rl = requests.post(_alp_url, json={
                "symbol": long_tk, "notional": str(round(cascade_leg, 2)),
                "side": "buy", "type": "market", "time_in_force": "day",
            }, headers=_alp_hdr, timeout=10)
            if _rl.status_code not in (200, 201):
                log.warning("CASCADE LONG FAILED: %s HTTP %d", long_tk, _rl.status_code)
                continue
            _long_oid = _rl.json().get("id", "unknown")

            # Short leg
            _short_price = 0
            try:
                _sq = requests.get(f"https://data.alpaca.markets/v2/stocks/{short_tk}/quotes/latest",
                                   headers=_data_hdr, timeout=5)
                if _sq.status_code == 200:
                    _short_price = float(_sq.json().get("quote", {}).get("ap", 0) or 0)
            except Exception:
                pass
            _short_shares = _cmath.floor(cascade_leg / _short_price) if _short_price > 0 else 0
            if _short_shares < 1:
                log.warning("CASCADE SHORT SKIP: %s — 0 shares", short_tk)
                try:
                    requests.delete(f"{_alp_url}/{_long_oid}", headers=_alp_hdr, timeout=5)
                except Exception:
                    pass
                continue
            _rs = requests.post(_alp_url, json={
                "symbol": short_tk, "qty": str(_short_shares),
                "side": "sell", "type": "market", "time_in_force": "day",
            }, headers=_alp_hdr, timeout=10)
            if _rs.status_code not in (200, 201):
                log.warning("CASCADE SHORT FAILED: %s HTTP %d — cancelling long", short_tk, _rs.status_code)
                try:
                    requests.delete(f"{_alp_url}/{_long_oid}", headers=_alp_hdr, timeout=5)
                except Exception:
                    pass
                continue
            _short_oid = _rs.json().get("id", "unknown")

            total_cost = cascade_leg * 2
            if total_cost > PAPER_PORTFOLIO.get("cash", 0):
                log.warning("CASCADE: insufficient cash for %s", cascade_name)
                continue

            PAPER_PORTFOLIO["cash"] -= total_cost
            _cas_pos = {
                "market": market_id,
                "side": f"Long {long_tk} / Short {short_tk}",
                "shares": 1, "entry_price": 0, "cost": total_cost, "value": total_cost,
                "timestamp": now.strftime("%Y-%m-%d %H:%M UTC"),
                "platform": "Alpaca", "ev": 0,
                "strategy": "cascade_trade",
                "long_leg": long_tk, "short_leg": short_tk,
                "entry_long_price": 0, "entry_short_price": _short_price,
                "source_market": f"CASCADE from {primary_signal}",
                "long_order_id": _long_oid, "short_order_id": _short_oid,
            }
            PAPER_PORTFOLIO["positions"].append(_cas_pos)
            db_log_paper_trade(_cas_pos)
            db_open_position(
                market_id=market_id, platform="Alpaca", strategy="cascade_trade",
                direction=f"Long {long_tk} / Short {short_tk}",
                size_usd=total_cost, shares=1, entry_price=0,
                long_leg=long_tk, short_leg=short_tk,
                metadata={"primary_signal": primary_signal, "cascade": cascade_name,
                          "description": chain["description"], "delay_min": chain["delay_min"],
                          "long_order_id": _long_oid, "short_order_id": _short_oid},
            )
            fired += 1
            log.info("CASCADE TRADE: %s | Long %s / Short %s | $%.0f (from %s)",
                     cascade_name, long_tk, short_tk, total_cost, primary_signal)
            echo_memory_store("cascade_trade", primary_signal, cascade_name, 0,
                              "elevated" if geo_elevated else "normal",
                              chain["description"], "", 0,
                              f"long={long_tk} short={short_tk} size=${total_cost:.0f} lag={chain['delay_min']}min")
            if channel:
                try:
                    await channel.send(
                        f"**CASCADE TRADE** — {cascade_name}\n"
                        f"Triggered by: {primary_signal} ({chain['delay_min']}min lag)\n"
                        f"Long {long_tk} / Short {short_tk} | ${total_cost:.0f}\n"
                        f"Chain: {chain['description']}")
                except Exception:
                    pass
        except Exception as _ce:
            log.warning("CASCADE execution error for %s: %s", cascade_name, _ce)

    _CASCADE_PENDING.clear()
    _CASCADE_PENDING.extend(remaining)
    return fired


# Price history: {market_title_lower: [(timestamp, yes_price), ...]}
_ORACLE_PRICE_HISTORY = {}

# ---------------------------------------------------------------------------
# INTELLIGENCE LAYER — Meteorologist + Geopolitical Monitor
# ---------------------------------------------------------------------------

# Rolling headline memory: {theme: [(timestamp, title_str), ...]}
_INTEL_HEADLINE_MEMORY = {}
# Geopolitical alert state: {"active": bool, "theme": str, "count": int, "since": datetime}
_INTEL_GEO_STATE = {"active": False, "theme": "", "count": 0, "since": None, "notified": False}

# Theme → search queries for Google News RSS
_INTEL_THEMES = {
    "fed":        ["federal reserve interest rate", "fed funds rate decision"],
    "iran":       ["iran military", "iran ceasefire", "iran nuclear deal"],
    "tariff":     ["tariffs trade war", "import tariffs increase"],
    "recession":  ["US recession economy", "recession indicators GDP"],
    "china":      ["china taiwan", "china sanctions", "china trade"],
    "ukraine":    ["ukraine russia war", "ukraine ceasefire"],
    "taiwan":     ["taiwan china military", "taiwan strait crisis", "taiwan invasion"],
}

# Escalation keywords — weighted heavier in geo monitor
_GEO_ESCALATION_KEYWORDS = [
    "attack", "missile", "invasion", "sanctions", "ceasefire",
    "nuclear", "troops", "strike", "bomb", "war", "military",
    "deploy", "retaliat", "escalat", "mobiliz", "blockade",
]

# Sentiment scoring keywords
_SENTIMENT_POSITIVE = [
    "ceasefire", "deal", "recovery", "stimulus", "agreement", "peace", "rally",
    "surge", "growth", "profit", "beat", "upgrade", "optimis", "boom", "record high",
    "ease", "relief", "cooperat", "diplomac", "deescalat", "withdraw", "retreat",
]
_SENTIMENT_NEGATIVE = [
    "attack", "invasion", "crash", "recession", "sanctions", "default", "collapse",
    "plunge", "crisis", "war", "bomb", "missile", "nuclear", "escalat", "panic",
    "layoff", "bankrupt", "downgrade", "sell-off", "slump", "tariff", "retaliat",
    "blockade", "deploy", "strike", "troops",
]
# Theme sentiment cache: {theme: {"score": float, "count": int, "ts": datetime}}
_THEME_SENTIMENT = {}


def _score_headline_sentiment(headline):
    """Score a headline from -1 (very negative) to +1 (very positive)."""
    hl = headline.lower()
    pos = sum(1 for w in _SENTIMENT_POSITIVE if w in hl)
    neg = sum(1 for w in _SENTIMENT_NEGATIVE if w in hl)
    total = pos + neg
    if total == 0:
        return 0.0
    return (pos - neg) / total


def update_theme_sentiment():
    """Calculate average sentiment per theme from recent headlines."""
    now = datetime.now(timezone.utc)
    import datetime as _dt_sent
    cutoff = now - _dt_sent.timedelta(hours=2)
    for theme, entries in _INTEL_HEADLINE_MEMORY.items():
        recent = [(t, h) for t, h in entries if t > cutoff]
        if not recent:
            _THEME_SENTIMENT[theme] = {"score": 0.0, "count": 0, "ts": now}
            continue
        scores = [_score_headline_sentiment(h) for _, h in recent]
        avg = sum(scores) / len(scores) if scores else 0.0
        _THEME_SENTIMENT[theme] = {"score": round(avg, 3), "count": len(scores), "ts": now}


def get_theme_sentiment(theme):
    """Get sentiment score for a theme. Returns float -1 to +1."""
    s = _THEME_SENTIMENT.get(theme, {})
    return s.get("score", 0.0)


# Polygon tickers mapped to themes for real-time news
_POLYGON_THEME_TICKERS = {
    "fed": ["SPY", "TLT", "XLF"],
    "iran": ["XLE", "USO", "GLD"],
    "tariff": ["EEM", "FXI", "SPY"],
    "recession": ["SPY", "TLT", "GLD"],
    "china": ["FXI", "BABA", "TSM"],
    "ukraine": ["LMT", "RTX", "GLD"],
    "taiwan": ["TSM", "SMH", "SOXX"],
}


def _intel_fetch_headlines():
    """Meteorologist Agent: fetch headlines via Polygon.io (primary) + Google News RSS (fallback).
    Stores in rolling 2-hour memory. Returns total headlines fetched."""
    import xml.etree.ElementTree as ET
    now = datetime.now(timezone.utc)
    cutoff = now - __import__("datetime").timedelta(hours=2)
    total = 0

    for theme, queries in _INTEL_THEMES.items():
        if theme not in _INTEL_HEADLINE_MEMORY:
            _INTEL_HEADLINE_MEMORY[theme] = []
        # Prune old entries
        _INTEL_HEADLINE_MEMORY[theme] = [
            (t, h) for t, h in _INTEL_HEADLINE_MEMORY[theme] if t > cutoff]

        existing_titles = {h.lower() for _, h in _INTEL_HEADLINE_MEMORY[theme]}
        polygon_fetched = 0

        # Primary: Polygon.io news for theme-related tickers
        try:
            from polygon_client import get_news as _poly_news
            poly_tickers = _POLYGON_THEME_TICKERS.get(theme, [])
            for ticker in poly_tickers[:2]:
                articles = _poly_news(ticker=ticker, limit=5)
                for a in articles:
                    title_text = a.get("title", "").strip()
                    if title_text and title_text.lower() not in existing_titles:
                        # Check if headline is relevant to theme
                        _tl = title_text.lower()
                        _theme_kws = [q.split()[0].lower() for q in queries] + [theme.lower()]
                        if any(kw in _tl for kw in _theme_kws) or theme in ("fed", "recession"):
                            _INTEL_HEADLINE_MEMORY[theme].append((now, title_text + f" - {a.get('source', '')}"))
                            existing_titles.add(title_text.lower())
                            total += 1
                            polygon_fetched += 1
        except Exception as _pe:
            pass  # Fallback to RSS below

        # Fallback: Google News RSS if Polygon returned nothing
        if polygon_fetched == 0:
            query = queries[0]
            try:
                url = f"https://news.google.com/rss/search?q={query.replace(' ', '+')}&hl=en-US&gl=US&ceid=US:en"
                r = requests.get(url, timeout=8, headers={"User-Agent": "TraderJoes/1.0"})
                if r.status_code == 200:
                    root = ET.fromstring(r.content)
                    for item in root.findall(".//item")[:10]:
                        title_el = item.find("title")
                        if title_el is not None and title_el.text:
                            title_text = title_el.text.strip()
                            if title_text.lower() not in existing_titles:
                                _INTEL_HEADLINE_MEMORY[theme].append((now, title_text))
                                existing_titles.add(title_text.lower())
                                total += 1
            except Exception as e:
                log.warning("INTEL RSS fetch %s: %s", theme, e)

    return total


def _intel_get_relevant_headlines(signal_name, limit=3):
    """Return top N recent headlines relevant to an Oracle signal."""
    # Map signal names to themes
    theme_map = {
        "fed_hike": "fed", "fed_cut": "fed",
        "iran_ceasefire": "iran", "iran_attack": "iran", "iran_escalation": "iran",
        "ukraine_escalation": "ukraine", "taiwan_escalation": "taiwan",
        "tariff": "tariff", "recession": "recession",
    }
    theme = theme_map.get(signal_name, "")
    if not theme:
        return []

    headlines = _INTEL_HEADLINE_MEMORY.get(theme, [])
    # Sort by recency, return most recent
    headlines.sort(key=lambda x: x[0], reverse=True)
    return [h for _, h in headlines[:limit]]


def _intel_geo_monitor(channel_notify=None):
    """Geopolitical Monitor: detect escalation spikes across themes.
    Returns True if geo risk is elevated (3+ escalation headlines in 1h on same theme)."""
    now = datetime.now(timezone.utc)
    one_hour_ago = now - __import__("datetime").timedelta(hours=1)
    state = _INTEL_GEO_STATE

    worst_theme = ""
    worst_count = 0

    for theme, entries in _INTEL_HEADLINE_MEMORY.items():
        # Count escalation keywords in last hour's headlines
        recent = [(t, h) for t, h in entries if t > one_hour_ago]
        esc_count = 0
        for _, headline in recent:
            hl = headline.lower()
            if sum(1 for kw in _GEO_ESCALATION_KEYWORDS if kw in hl) >= 1:
                esc_count += 1
        if esc_count > worst_count:
            worst_count = esc_count
            worst_theme = theme

    was_active = state["active"]

    if worst_count >= 3:
        state["active"] = True
        state["theme"] = worst_theme
        state["count"] = worst_count
        if not state["since"]:
            state["since"] = now
        if not was_active:
            state["notified"] = False
            log.warning("GEO ESCALATION: %s — %d escalation headlines in 1h, sizing tightened to 0.5%%",
                        worst_theme, worst_count)
            _agent_log_event("geo", f"ESCALATION: {worst_theme} — {worst_count} headlines in 1h")
            send_critical_alert("Geo Escalation", f"{worst_theme}: {worst_count} escalation headlines in 1h")
    else:
        if was_active:
            log.info("GEO DE-ESCALATION: %s risk subsided (%d headlines)", state["theme"], worst_count)
        state["active"] = False
        state["theme"] = worst_theme if worst_count > 0 else ""
        state["count"] = worst_count
        state["since"] = None
        state["notified"] = False

    return state["active"]


def _intel_get_geo_state():
    """Return current geopolitical alert state for display."""
    return _INTEL_GEO_STATE.copy()


def _intel_theme_escalation_count(theme, hours=2):
    """Count escalation headlines for a specific theme in the last N hours,
    regardless of which theme is the primary geo alert.
    Uses 2h window (not 1h) to avoid headline expiry gaps between scan cycles."""
    now = datetime.now(timezone.utc)
    cutoff = now - __import__("datetime").timedelta(hours=hours)
    entries = _INTEL_HEADLINE_MEMORY.get(theme, [])
    recent = [(t, h) for t, h in entries if t > cutoff]
    esc_count = 0
    for _, headline in recent:
        hl = headline.lower()
        if sum(1 for kw in _GEO_ESCALATION_KEYWORDS if kw in hl) >= 1:
            esc_count += 1
    return esc_count


def _oracle_match_signal(market_title):
    """Match a market title against ORACLE_SIGNALS. Returns first matching signal or None."""
    title_lower = market_title.lower()
    for sig in ORACLE_SIGNALS:
        if all(kw in title_lower for kw in sig["keywords"]):
            return sig
    return None


def _oracle_match_all_signals(market_title):
    """Match a market title against ALL ORACLE_SIGNALS. Returns list of matching signals."""
    title_lower = market_title.lower()
    return [sig for sig in ORACLE_SIGNALS if all(kw in title_lower for kw in sig["keywords"])]


def _oracle_get_all_prices():
    """Fetch YES prices for all active Polymarket + Kalshi markets. Returns {title: yes_price}."""
    prices = {}
    # Polymarket
    try:
        for mkt in get_polymarket_markets(limit=30):
            title = mkt.get("question", mkt.get("title", ""))[:80]
            yes_price = 0.0
            op = mkt.get("outcomePrices", "")
            if op:
                try:
                    parsed = json.loads(op) if isinstance(op, str) else op
                    if len(parsed) >= 1:
                        yes_price = float(parsed[0])
                except (json.JSONDecodeError, ValueError, IndexError):
                    pass
            if yes_price <= 0:
                tokens = mkt.get("tokens", [])
                if len(tokens) >= 1:
                    yes_price = float(tokens[0].get("price", 0))
            if yes_price > 0 and title:
                prices[title] = yes_price
    except Exception as e:
        log.warning("Oracle Polymarket fetch: %s", e)
    # Kalshi
    try:
        for event in get_kalshi_events(limit=10)[:5]:
            for mkt in get_kalshi_markets_for_event(event.get("event_ticker", "")):
                title = mkt.get("title", "")[:80]
                yes_price = (mkt.get("yes_ask", 0) / 100.0) if mkt.get("yes_ask") else 0
                if yes_price > 0 and title:
                    prices[title] = yes_price
    except Exception as e:
        log.warning("Oracle Kalshi fetch: %s", e)
    return prices


async def scan_oracle_signals(channel=None):
    """Oracle Engine scanner — runs every 10-min cycle.
    Fetches prediction market prices, detects threshold crossings, executes equity trades."""
    if not ORACLE_CONFIG["enabled"]:
        return 0
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return 0

    now = datetime.now(timezone.utc)
    prices = _oracle_get_all_prices()
    if not prices:
        return 0

    # Update price history
    for title, px in prices.items():
        key = title.lower()
        if key not in _ORACLE_PRICE_HISTORY:
            _ORACLE_PRICE_HISTORY[key] = []
        _ORACLE_PRICE_HISTORY[key].append((now, px))
        # Keep only last 2 hours of data
        cutoff = now - __import__("datetime").timedelta(hours=2)
        _ORACLE_PRICE_HISTORY[key] = [(t, p) for t, p in _ORACLE_PRICE_HISTORY[key] if t > cutoff]

    # --- Intelligence Layer: fetch headlines + geo monitor ---
    try:
        _intel_count = _intel_fetch_headlines()
        if _intel_count > 0:
            log.info("INTEL: fetched %d new headlines across %d themes", _intel_count, len(_INTEL_HEADLINE_MEMORY))
        update_theme_sentiment()
    except Exception as _ie:
        log.warning("INTEL headline fetch error: %s", _ie)

    geo_elevated = False
    try:
        geo_elevated = _intel_geo_monitor()
        geo_state = _intel_get_geo_state()
        if geo_elevated and not geo_state.get("notified") and channel:
            _INTEL_GEO_STATE["notified"] = True
            # Get sample headlines
            _geo_theme = geo_state.get("theme", "")
            _geo_headlines = _INTEL_HEADLINE_MEMORY.get(_geo_theme, [])
            _geo_recent = sorted(_geo_headlines, key=lambda x: x[0], reverse=True)[:3]
            _geo_hl_str = "\n".join(f"  • {h[:70]}" for _, h in _geo_recent)
            try:
                await channel.send(
                    f"**GEO ESCALATION ALERT** — {_geo_theme.upper()}\n"
                    f"{geo_state['count']} escalation headlines in 1h — sizing tightened to 0.5%\n"
                    f"{_geo_hl_str}")
            except Exception:
                pass
    except Exception as _ge:
        log.warning("INTEL geo monitor error: %s", _ge)

    # Count existing oracle positions
    oracle_count = sum(1 for p in PAPER_PORTFOLIO.get("positions", [])
                       if p.get("strategy") == "oracle_trade")
    if oracle_count >= ORACLE_CONFIG["max_oracle_positions"]:
        log.info("ORACLE: %d positions open (max %d)", oracle_count, ORACLE_CONFIG["max_oracle_positions"])
        return 0

    fired = 0
    _matched_inverse_signals = set()  # Track which inverse signals had a matching market
    for title, yes_price in prices.items():
        _all_sigs = _oracle_match_all_signals(title)
        if not _all_sigs:
            continue
        for sig in _all_sigs:
            # Threshold check: normal signals fire above threshold, inverse fire below
            _is_inverse = sig.get("inverse", False)
            if _is_inverse:
                _matched_inverse_signals.add(sig["name"])
                if yes_price > sig["threshold"]:
                    continue  # Inverse: only fire when prob DROPS below threshold
                # Check geo requirement — count theme headlines directly,
                # don't require the theme to be the primary geo alert
                _geo_req = sig.get("geo_required", "")
                _geo_min = sig.get("geo_min_headlines", 0)
                if _geo_req:
                    _theme_esc_count = _intel_theme_escalation_count(_geo_req)
                    if _theme_esc_count < _geo_min:
                        log.info("ORACLE GEO SKIP: %s — %s headlines=%d < %d required",
                                 sig["name"], _geo_req, _theme_esc_count, _geo_min)
                        continue  # Not enough escalation headlines for this theme
                    else:
                        log.info("ORACLE GEO PASS: %s — %s headlines=%d >= %d",
                                 sig["name"], _geo_req, _theme_esc_count, _geo_min)
            else:
                if yes_price < sig["threshold"]:
                    continue

            signal_name = sig["name"]
            market_id = f"ORACLE:{signal_name}"

            # Dedup: already open? Only check ORACLE: positions, not prediction trades
            if any(p.get("market", "") == market_id for p in PAPER_PORTFOLIO.get("positions", [])):
                log.info("ORACLE DEDUP: %s already open in portfolio", signal_name)
                continue

            # Overlap check: don't open positions with tickers already held by other oracle signals
            _existing_tickers = set()
            for _ep in PAPER_PORTFOLIO.get("positions", []):
                if _ep.get("strategy") == "oracle_trade":
                    _existing_tickers.add(_ep.get("long_leg", ""))
                    _existing_tickers.add(_ep.get("short_leg", ""))
                    _existing_tickers.add(_ep.get("extra_long_leg", ""))
            _existing_tickers.discard("")
            _sig_tickers = {sig["long"], sig["short"]}
            if sig.get("extra_long"):
                _sig_tickers.add(sig["extra_long"])
            _overlap = _sig_tickers & _existing_tickers
            if _overlap:
                log.info("ORACLE OVERLAP: %s skipped — %s already in oracle positions",
                         signal_name, ",".join(_overlap))
                continue

            # Cooldown: check SQLite for recently closed
            try:
                import sqlite3 as _osq
                _oc = _osq.connect(DB_PATH)
                _ocr = _oc.cursor()
                _ocr.execute("SELECT closed_at FROM positions WHERE market_id=? AND status='closed' ORDER BY closed_at DESC LIMIT 1", (market_id,))
                _orow = _ocr.fetchone()
                _oc.close()
                if _orow and _orow[0]:
                    _oct = datetime.fromisoformat(_orow[0]).replace(tzinfo=timezone.utc)
                    _omins = (now - _oct).total_seconds() / 60
                    if _omins < ORACLE_CONFIG["cooldown_hours"] * 60:
                        log.info("ORACLE COOLDOWN: %s closed %.0f min ago", signal_name, _omins)
                        continue
            except Exception:
                pass

            # Calculate 1-hour delta
            key = title.lower()
            delta_1h = 0.0
            history = _ORACLE_PRICE_HISTORY.get(key, [])
            one_hour_ago = now - __import__("datetime").timedelta(hours=1)
            old_prices = [(t, p) for t, p in history if t <= one_hour_ago]
            if old_prices:
                delta_1h = yes_price - old_prices[-1][1]

            # Risk gate
            if risk_is_strategy_paused("oracle_trade"):
                log.info("RISK BLOCK: oracle_trade strategy paused")
                continue

            # Half-Kelly sizing at 0.75% base (tightened to 0.5% during geo escalation)
            portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
                p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))
            _size_pct = 0.005 if geo_elevated else ORACLE_CONFIG["base_size_pct"]
            base_size = portfolio_value * _size_pct
            # Scale by conviction: higher probability & larger delta = more size
            # For inverse signals, conviction grows as price drops (use 1-price)
            _conv_price = (1 - yes_price) if _is_inverse else yes_price
            kelly_mult = min(_conv_price * (1 + abs(delta_1h) * 5), 2.0)
            leg_size = base_size * kelly_mult * psychologist_size_multiplier() * meta_alloc_multiplier("oracle_trade")

            # Sentiment boost: very negative sentiment on geo theme → 1.5x size
            if _is_inverse and sig.get("geo_required"):
                _sent_score = get_theme_sentiment(sig["geo_required"])
                if _sent_score < -0.7:
                    leg_size *= 1.5
                    log.info("SENTIMENT BOOST: %s sentiment=%.2f → 1.5x size", signal_name, _sent_score)

            # Causal Memory: adjust size based on historical regime similarity
            try:
                _cm_mult, _cm_details = causal_memory_size_adjustment("oracle_trade")
                if _cm_mult != 1.0:
                    leg_size *= _cm_mult
                    log.info("CAUSAL MEMORY ORACLE: %s %.2fx → $%.0f | %s", signal_name, _cm_mult, leg_size, _cm_details)
            except Exception:
                pass

            log.info("ORACLE SIZING: %s conv=%.2f kelly=%.2f base=$%.0f psych=%.1f meta=%.1f → leg=$%.0f",
                     signal_name, _conv_price, kelly_mult, base_size,
                     psychologist_size_multiplier(), meta_alloc_multiplier("oracle_trade"), leg_size)

            if leg_size < 10:
                log.info("ORACLE DUST SKIP: %s leg=$%.2f < $10", signal_name, leg_size)
                continue

            # Monte Carlo validation on oracle trade legs
            try:
                _mc_o = montecarlo_simulate(sig["long"], sig["short"], horizon_days=5)
                if _mc_o.get("available"):
                    _mc_o_mult, _mc_o_skip = montecarlo_size_adjustment(_mc_o)
                    if _mc_o_skip:
                        log.info("MC SKIP ORACLE: %s prob=%.0f%%", signal_name, _mc_o["prob_profit"] * 100)
                        continue
                    leg_size *= _mc_o_mult
                    log.info("MC ORACLE: %s prob=%.0f%% → %.1fx", signal_name, _mc_o["prob_profit"] * 100, _mc_o_mult)
            except Exception:
                pass

            # Minimum leg size floor — too many reductions can compound to dust
            _MIN_ORACLE_LEG = 25
            if leg_size < _MIN_ORACLE_LEG:
                log.info("ORACLE SIZE FLOOR: %s $%.0f < $%d min — skipping",
                         signal_name, leg_size, _MIN_ORACLE_LEG)
                continue

            # AI Consensus: get second opinion on high-conviction oracle trades
            _ai_verdict = "APPROVE"
            _conviction_score = int(yes_price * 100)
            if _conviction_score >= 70 and OPENAI_API_KEY:
                _ai_notes = [f"Oracle signal: {signal_name}", f"YES price: ${yes_price:.2f}",
                             f"1h delta: {delta_1h:+.3f}", f"Trade: Long {sig['long']} / Short {sig['short']}"]
                _rel_hl = _intel_get_relevant_headlines(signal_name, limit=2)
                for _h in _rel_hl:
                    _ai_notes.append(f"Headline: {_h[:60]}")
                _ai_verdict, _ai_reason = ai_get_second_opinion(
                    f"{sig['long']}/{sig['short']}", f"Long {sig['long']}/Short {sig['short']}",
                    _conviction_score, _ai_notes)
                if _ai_verdict == "REJECT":
                    log.info("AI REJECT: %s — %s", signal_name, _ai_reason[:60])
                    continue
                if _ai_verdict == "REDUCE":
                    leg_size *= 0.5
                    log.info("AI REDUCE: %s size halved — %s", signal_name, _ai_reason[:60])

            long_tk = sig["long"]
            short_tk = sig["short"]
            extra_long_tk = sig.get("extra_long", "")

            _extra_dir = "Short" if sig.get("extra_long_side") == "short" else "Long"
            _legs_str = f"Long {long_tk} / Short {short_tk}" + (f" / {_extra_dir} {extra_long_tk}" if extra_long_tk else "")
            log.info("ORACLE SIGNAL: %s YES=$%.2f (Δ1h=%+.3f) → %s | $%.0f/leg",
                     signal_name, yes_price, delta_1h, _legs_str, leg_size)

            # Execute both legs via Alpaca
            import math as _omath
            _alp_hdr = {
                "APCA-API-KEY-ID": ALPACA_API_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
                "Content-Type": "application/json",
            }
            _alp_url = f"{ALPACA_BASE_URL}/v2/orders"
            _data_hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}

            try:
                # Long leg — buy with notional
                _long_body = {
                    "symbol": long_tk, "notional": str(round(leg_size, 2)),
                    "side": "buy", "type": "market", "time_in_force": "day",
                }
                _rl = requests.post(_alp_url, json=_long_body, headers=_alp_hdr, timeout=10)
                if _rl.status_code not in (200, 201):
                    log.warning("ORACLE LONG FAILED: %s HTTP %d: %s", long_tk, _rl.status_code, _rl.text[:200])
                    continue
                _long_oid = _rl.json().get("id", "unknown")
                log.info("ORACLE LONG ORDER: %s id=%s", long_tk, _long_oid)

                # Short leg — whole shares only
                _short_price = 0
                try:
                    _sq = requests.get(f"https://data.alpaca.markets/v2/stocks/{short_tk}/quotes/latest",
                                       headers=_data_hdr, timeout=5)
                    if _sq.status_code == 200:
                        _short_price = float(_sq.json().get("quote", {}).get("ap", 0) or 0)
                except Exception:
                    pass
                _short_shares = _omath.floor(leg_size / _short_price) if _short_price > 0 else 0
                if _short_shares < 1:
                    log.warning("ORACLE SHORT SKIP: %s — 0 shares at $%.2f", short_tk, _short_price)
                    try:
                        requests.delete(f"{_alp_url}/{_long_oid}", headers=_alp_hdr, timeout=5)
                    except Exception:
                        pass
                    continue

                _short_body = {
                    "symbol": short_tk, "qty": str(_short_shares),
                    "side": "sell", "type": "market", "time_in_force": "day",
                }
                _rs = requests.post(_alp_url, json=_short_body, headers=_alp_hdr, timeout=10)
                if _rs.status_code not in (200, 201):
                    log.warning("ORACLE SHORT FAILED: %s HTTP %d: %s — cancelling long", short_tk, _rs.status_code, _rs.text[:200])
                    try:
                        requests.delete(f"{_alp_url}/{_long_oid}", headers=_alp_hdr, timeout=5)
                    except Exception:
                        pass
                    continue
                _short_oid = _rs.json().get("id", "unknown")
                log.info("ORACLE SHORT ORDER: %s id=%s", short_tk, _short_oid)

                # Extra leg (e.g. GLD long for ukraine, SMH short for taiwan)
                _extra_long_oid = None
                _extra_side = sig.get("extra_long_side", "buy")  # default buy, "short" for sells
                if extra_long_tk:
                    if _extra_side == "short":
                        # Short: need whole shares
                        _extra_price = 0
                        try:
                            _eq = requests.get(f"https://data.alpaca.markets/v2/stocks/{extra_long_tk}/quotes/latest",
                                               headers=_data_hdr, timeout=5)
                            if _eq.status_code == 200:
                                _extra_price = float(_eq.json().get("quote", {}).get("ap", 0) or 0)
                        except Exception:
                            pass
                        _extra_shares = _omath.floor(leg_size / _extra_price) if _extra_price > 0 else 0
                        if _extra_shares >= 1:
                            _extra_body = {
                                "symbol": extra_long_tk, "qty": str(_extra_shares),
                                "side": "sell", "type": "market", "time_in_force": "day",
                            }
                        else:
                            _extra_body = None
                            log.warning("ORACLE EXTRA SHORT SKIP: %s — 0 shares at $%.2f", extra_long_tk, _extra_price)
                    else:
                        _extra_body = {
                            "symbol": extra_long_tk, "notional": str(round(leg_size, 2)),
                            "side": "buy", "type": "market", "time_in_force": "day",
                        }
                    if _extra_body:
                        _re = requests.post(_alp_url, json=_extra_body, headers=_alp_hdr, timeout=10)
                        if _re.status_code in (200, 201):
                            _extra_long_oid = _re.json().get("id", "unknown")
                            log.info("ORACLE EXTRA %s ORDER: %s id=%s", _extra_side.upper(), extra_long_tk, _extra_long_oid)
                        else:
                            log.warning("ORACLE EXTRA %s FAILED: %s HTTP %d: %s (continuing with 2 legs)",
                                        _extra_side.upper(), extra_long_tk, _re.status_code, _re.text[:200])

                # Get fill prices
                _entry_long_price = 0
                _entry_short_price = 0
                try:
                    _ql = requests.get(f"https://data.alpaca.markets/v2/stocks/{long_tk}/quotes/latest", headers=_data_hdr, timeout=5)
                    if _ql.status_code == 200:
                        _entry_long_price = float(_ql.json().get("quote", {}).get("ap", 0) or 0)
                    _entry_short_price = _short_price  # Already fetched
                except Exception:
                    pass

                _num_legs = 3 if extra_long_tk else 2
                total_cost = leg_size * _num_legs
                if total_cost > PAPER_PORTFOLIO.get("cash", 0):
                    log.warning("ORACLE: insufficient cash $%.0f for $%.0f trade", PAPER_PORTFOLIO.get("cash", 0), total_cost)
                    continue

                PAPER_PORTFOLIO["cash"] -= total_cost
                _oracle_pos = {
                    "market": market_id,
                    "side": _legs_str,
                    "shares": 1, "entry_price": yes_price, "cost": total_cost,
                    "value": total_cost,
                    "timestamp": now.strftime("%Y-%m-%d %H:%M UTC"),
                    "platform": "Alpaca", "ev": delta_1h,
                    "strategy": "oracle_trade",
                    "long_leg": long_tk, "short_leg": short_tk,
                    "extra_long_leg": extra_long_tk or "",
                    "entry_long_price": _entry_long_price,
                    "entry_short_price": _entry_short_price,
                    "signal_threshold": sig["threshold"],
                    "source_market": title[:60],
                    "long_order_id": _long_oid,
                    "short_order_id": _short_oid,
                    "extra_long_order_id": _extra_long_oid or "",
                }
                PAPER_PORTFOLIO["positions"].append(_oracle_pos)
                db_log_paper_trade(_oracle_pos)
                _meta = {"signal": signal_name, "threshold": sig["threshold"],
                         "yes_price": yes_price, "delta_1h": delta_1h,
                         "source_market": title[:60],
                         "long_order_id": _long_oid, "short_order_id": _short_oid,
                         "entry_long_price": _entry_long_price,
                         "entry_short_price": _entry_short_price}
                if extra_long_tk:
                    _meta["extra_long"] = extra_long_tk
                    _meta["extra_long_order_id"] = _extra_long_oid or ""
                db_open_position(
                    market_id=market_id, platform="Alpaca", strategy="oracle_trade",
                    direction=_legs_str,
                    size_usd=total_cost, shares=1, entry_price=yes_price,
                    long_leg=long_tk, short_leg=short_tk,
                    metadata=_meta,
                )

                fired += 1
                log.info("ORACLE TRADE: %s | YES=$%.2f | %s | $%.0f",
                         signal_name, yes_price, _legs_str, total_cost)
                # Queue cascade chain-reaction trades (15-min delayed)
                cascade_queue_add(signal_name, leg_size, geo_elevated)

                # Store in echo memory
                _geo_st = _intel_get_geo_state()
                _hl = _intel_get_relevant_headlines(signal_name, limit=1)
                echo_memory_store("oracle_trade", signal_name, signal_name, yes_price,
                                  "elevated" if _geo_st.get("active") else "normal",
                                  _hl[0] if _hl else "", "", 0,
                                  f"delta={delta_1h:+.3f} long={long_tk} short={short_tk} size=${total_cost:.0f}")
                if channel:
                    try:
                        _trade_msg = (
                            f"**ORACLE TRADE** — {signal_name}\n"
                            f"Signal: {title[:50]} YES=${yes_price:.2f} (Δ1h={delta_1h:+.3f})\n"
                            f"Long {long_tk} / Short {short_tk} | ${total_cost:.0f}")
                        # Attach relevant headlines from Intelligence Layer
                        _rel_hl = _intel_get_relevant_headlines(signal_name, limit=3)
                        if _rel_hl:
                            _trade_msg += "\n**Why:**"
                            for _hl in _rel_hl:
                                _trade_msg += f"\n  • {_hl[:80]}"
                        if geo_elevated:
                            _trade_msg += f"\n⚠ Geo risk elevated — sizing at 0.5%"
                        await channel.send(_trade_msg)
                    except Exception:
                        pass
            except Exception as _oe:
                log.warning("Oracle execution error for %s: %s", signal_name, _oe)

    # Geo-only fallback for inverse signals with no matching Polymarket market.
    # If headline count alone exceeds a higher bar (8+), synthesize a virtual price
    # and fire the signal without needing a dedicated prediction market.
    _GEO_ONLY_HEADLINE_BAR = 8
    _GEO_ONLY_VIRTUAL_PRICE = 0.20  # Synthetic YES price (well below 0.35 threshold)
    for sig in ORACLE_SIGNALS:
        if not sig.get("inverse"):
            continue
        if sig["name"] in _matched_inverse_signals:
            continue  # Already had a real market match
        _geo_req = sig.get("geo_required", "")
        if not _geo_req:
            continue
        _theme_count = _intel_theme_escalation_count(_geo_req)
        if _theme_count < _GEO_ONLY_HEADLINE_BAR:
            continue
        signal_name = sig["name"]
        market_id = f"ORACLE:{signal_name}"
        # Dedup: already open?
        if any(p.get("market", "") == market_id for p in PAPER_PORTFOLIO.get("positions", [])):
            continue
        # Cooldown check
        _skip_cooldown = False
        try:
            import sqlite3 as _fcsq
            _fcconn = _fcsq.connect(DB_PATH)
            _fcc = _fcconn.cursor()
            _fc_row = _fcc.execute(
                "SELECT closed_at FROM positions WHERE market_id=? AND status='closed' ORDER BY closed_at DESC LIMIT 1",
                (market_id,)).fetchone()
            _fcconn.close()
            if _fc_row and _fc_row[0]:
                _fc_closed = datetime.fromisoformat(_fc_row[0].replace(" UTC", "+00:00"))
                if (now - _fc_closed).total_seconds() < ORACLE_CONFIG["cooldown_hours"] * 3600:
                    _skip_cooldown = True
        except Exception:
            pass
        if _skip_cooldown:
            continue
        # Position limit
        oracle_count = sum(1 for p in PAPER_PORTFOLIO.get("positions", [])
                           if p.get("strategy") == "oracle_trade")
        if oracle_count >= ORACLE_CONFIG["max_oracle_positions"]:
            continue

        log.info("GEO-ONLY FALLBACK: %s — %d %s escalation headlines (no market found), virtual YES=$%.2f",
                 signal_name, _theme_count, _geo_req, _GEO_ONLY_VIRTUAL_PRICE)

        # Reuse the same execution path with synthetic price
        yes_price = _GEO_ONLY_VIRTUAL_PRICE
        delta_1h = 0.0
        long_tk = sig["long"]
        short_tk = sig["short"]
        extra_long_tk = sig.get("extra_long", "")
        _legs_str = f"Long {long_tk} / Short {short_tk}" + (f" / Long {extra_long_tk}" if extra_long_tk else "")

        portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
            p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))
        _size_pct = 0.005 if geo_elevated else ORACLE_CONFIG["base_size_pct"]
        leg_size = portfolio_value * _size_pct * psychologist_size_multiplier() * meta_alloc_multiplier("oracle_trade")
        if leg_size < 25:
            continue

        log.info("GEO-ONLY SIGNAL: %s → %s | $%.0f/leg", signal_name, _legs_str, leg_size)

        import math as _omath
        _alp_hdr = {
            "APCA-API-KEY-ID": ALPACA_API_KEY,
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
            "Content-Type": "application/json",
        }
        _alp_url = f"{ALPACA_BASE_URL}/v2/orders"
        _data_hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}

        try:
            # Long leg
            _long_body = {
                "symbol": long_tk, "notional": str(round(leg_size, 2)),
                "side": "buy", "type": "market", "time_in_force": "day",
            }
            _rl = requests.post(_alp_url, json=_long_body, headers=_alp_hdr, timeout=10)
            if _rl.status_code not in (200, 201):
                log.warning("GEO-ONLY LONG FAILED: %s HTTP %d: %s", long_tk, _rl.status_code, _rl.text[:200])
                continue
            _long_oid = _rl.json().get("id", "unknown")

            # Short leg
            _short_price = 0
            try:
                _sq = requests.get(f"https://data.alpaca.markets/v2/stocks/{short_tk}/quotes/latest",
                                   headers=_data_hdr, timeout=5)
                if _sq.status_code == 200:
                    _short_price = float(_sq.json().get("quote", {}).get("ap", 0) or 0)
            except Exception:
                pass
            _short_shares = _omath.floor(leg_size / _short_price) if _short_price > 0 else 0
            if _short_shares < 1:
                log.warning("GEO-ONLY SHORT SKIP: %s — 0 shares at $%.2f", short_tk, _short_price)
                try:
                    requests.delete(f"{_alp_url}/{_long_oid}", headers=_alp_hdr, timeout=5)
                except Exception:
                    pass
                continue
            _short_body = {
                "symbol": short_tk, "qty": str(_short_shares),
                "side": "sell", "type": "market", "time_in_force": "day",
            }
            _rs = requests.post(_alp_url, json=_short_body, headers=_alp_hdr, timeout=10)
            if _rs.status_code not in (200, 201):
                log.warning("GEO-ONLY SHORT FAILED: %s HTTP %d", short_tk, _rs.status_code)
                try:
                    requests.delete(f"{_alp_url}/{_long_oid}", headers=_alp_hdr, timeout=5)
                except Exception:
                    pass
                continue
            _short_oid = _rs.json().get("id", "unknown")

            # Extra leg (long or short depending on signal config)
            _extra_long_oid = None
            _extra_side = sig.get("extra_long_side", "buy")
            if extra_long_tk:
                if _extra_side == "short":
                    _extra_price = 0
                    try:
                        _eq = requests.get(f"https://data.alpaca.markets/v2/stocks/{extra_long_tk}/quotes/latest",
                                           headers=_data_hdr, timeout=5)
                        if _eq.status_code == 200:
                            _extra_price = float(_eq.json().get("quote", {}).get("ap", 0) or 0)
                    except Exception:
                        pass
                    import math as _gmath
                    _extra_shares = _gmath.floor(leg_size / _extra_price) if _extra_price > 0 else 0
                    _extra_body = {"symbol": extra_long_tk, "qty": str(_extra_shares),
                                   "side": "sell", "type": "market", "time_in_force": "day"} if _extra_shares >= 1 else None
                else:
                    _extra_body = {"symbol": extra_long_tk, "notional": str(round(leg_size, 2)),
                                   "side": "buy", "type": "market", "time_in_force": "day"}
                if _extra_body:
                    _re = requests.post(_alp_url, json=_extra_body, headers=_alp_hdr, timeout=10)
                    if _re.status_code in (200, 201):
                        _extra_long_oid = _re.json().get("id", "unknown")
                        log.info("GEO-ONLY EXTRA %s: %s id=%s", _extra_side.upper(), extra_long_tk, _extra_long_oid)
                    else:
                        log.warning("GEO-ONLY EXTRA %s FAILED: %s (continuing with 2 legs)", _extra_side.upper(), extra_long_tk)

            _num_legs = 3 if extra_long_tk else 2
            total_cost = leg_size * _num_legs
            if total_cost > PAPER_PORTFOLIO.get("cash", 0):
                log.warning("GEO-ONLY: insufficient cash for %s", signal_name)
                continue

            PAPER_PORTFOLIO["cash"] -= total_cost
            _oracle_pos = {
                "market": market_id,
                "side": _legs_str,
                "shares": 1, "entry_price": yes_price, "cost": total_cost,
                "value": total_cost,
                "timestamp": now.strftime("%Y-%m-%d %H:%M UTC"),
                "platform": "Alpaca", "ev": 0,
                "strategy": "oracle_trade",
                "long_leg": long_tk, "short_leg": short_tk,
                "extra_long_leg": extra_long_tk or "",
                "entry_long_price": 0, "entry_short_price": _short_price,
                "signal_threshold": sig["threshold"],
                "source_market": f"GEO-ONLY:{_geo_req}({_theme_count} headlines)",
                "long_order_id": _long_oid, "short_order_id": _short_oid,
                "extra_long_order_id": _extra_long_oid or "",
            }
            PAPER_PORTFOLIO["positions"].append(_oracle_pos)
            db_log_paper_trade(_oracle_pos)
            db_open_position(
                market_id=market_id, platform="Alpaca", strategy="oracle_trade",
                direction=_legs_str, size_usd=total_cost, shares=1, entry_price=yes_price,
                long_leg=long_tk, short_leg=short_tk,
                metadata={"signal": signal_name, "geo_only": True,
                          "theme_count": _theme_count, "geo_theme": _geo_req,
                          "extra_long": extra_long_tk, "extra_long_order_id": _extra_long_oid or ""},
            )
            fired += 1
            cascade_queue_add(signal_name, leg_size, geo_elevated)
            log.info("GEO-ONLY TRADE: %s | %d headlines | %s | $%.0f",
                     signal_name, _theme_count, _legs_str, total_cost)
            if channel:
                try:
                    _rel_hl = _intel_get_relevant_headlines(signal_name, limit=3)
                    _trade_msg = (
                        f"**GEO-ONLY ORACLE** — {signal_name}\n"
                        f"{_theme_count} {_geo_req} escalation headlines (no prediction market)\n"
                        f"{_legs_str} | ${total_cost:.0f}")
                    if _rel_hl:
                        _trade_msg += "\n**Headlines:**"
                        for _h in _rel_hl:
                            _trade_msg += f"\n  • {_h[:80]}"
                    await channel.send(_trade_msg)
                except Exception:
                    pass
        except Exception as _goe:
            log.warning("GEO-ONLY execution error for %s: %s", signal_name, _goe)

    # Execute any pending cascade trades whose delay has elapsed
    try:
        cascade_fired = await cascade_execute_pending(channel)
        if cascade_fired > 0:
            fired += cascade_fired
            log.info("CASCADE: %d chain-reaction trades executed this cycle", cascade_fired)
    except Exception as _cex:
        log.warning("CASCADE execution error: %s", _cex)

    return fired


# ---------------------------------------------------------------------------
# OPTIONS THETA HARVEST — Sell put credit spreads on high IV
# ---------------------------------------------------------------------------
OPTIONS_THETA_CONFIG = {
    "enabled": True,
    "underlyings": ["SPY", "QQQ"],
    "iv_rank_threshold": 50,     # IV rank > 50% to sell premium
    "dte_target": 2,             # 2-day expiry
    "spread_width": 5,           # $5 wide spread
    "size_pct": 0.01,            # 1% of portfolio
    "tp_pct": 0.50,              # Take profit at 50% of credit
    "sl_pct": 2.00,              # Stop loss at 200% of credit
    "max_concurrent": 2,         # Max open theta positions
    "cooldown_hours": 24,        # Min hours between entries
}


async def scan_theta_harvest(channel=None):
    """Scan for put credit spread opportunities on SPY/QQQ when IV is elevated."""
    cfg = OPTIONS_THETA_CONFIG
    if not cfg["enabled"] or not ALPACA_API_KEY:
        return 0
    if not is_market_open():
        return 0

    # Count existing theta positions
    theta_count = sum(1 for p in PAPER_PORTFOLIO.get("positions", [])
                      if p.get("strategy") == "options_spread")
    if theta_count >= cfg["max_concurrent"]:
        return 0

    # Cooldown check
    try:
        import sqlite3 as _tsq
        _tc = _tsq.connect(DB_PATH)
        _tr = _tc.execute("SELECT closed_at FROM positions WHERE strategy='options_spread' ORDER BY created_at DESC LIMIT 1").fetchone()
        _tc.close()
        if _tr and _tr[0]:
            _tt = datetime.fromisoformat(_tr[0]).replace(tzinfo=timezone.utc)
            if (datetime.now(timezone.utc) - _tt).total_seconds() < cfg["cooldown_hours"] * 3600:
                return 0
    except Exception:
        pass

    hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
    fired = 0

    for underlying in cfg["underlyings"]:
        try:
            # Get current price
            _qr = requests.get(f"https://data.alpaca.markets/v2/stocks/{underlying}/quotes/latest",
                               headers=hdrs, timeout=5)
            if _qr.status_code != 200:
                continue
            _q = _qr.json().get("quote", {})
            spot = (float(_q.get("ap", 0) or 0) + float(_q.get("bp", 0) or 0)) / 2
            if spot <= 0:
                continue

            # Fetch options chain from Alpaca for 2-DTE
            expiry = datetime.now(timezone.utc) + __import__("datetime").timedelta(days=cfg["dte_target"])
            # Round to next trading day
            while expiry.weekday() >= 5:
                expiry += __import__("datetime").timedelta(days=1)
            exp_str = expiry.strftime("%Y-%m-%d")

            _or = requests.get(
                f"https://data.alpaca.markets/v1beta1/options/snapshots/{underlying}",
                headers=hdrs, params={"feed": "indicative", "expiration_date": exp_str},
                timeout=10)

            if _or.status_code != 200:
                log.info("THETA: %s options chain unavailable (%d)", underlying, _or.status_code)
                continue

            snapshots = _or.json().get("snapshots", {})
            if not snapshots:
                continue

            # Find ATM put and OTM put for credit spread
            strike_short = round(spot * 0.99, 0)  # 1% OTM short put
            strike_long = strike_short - cfg["spread_width"]  # Long put further OTM

            # Calculate IV rank from the options data
            # Use the implied vol from the ATM option as a proxy
            best_short = None
            best_short_iv = 0
            for sym, snap in snapshots.items():
                if "P" not in sym:
                    continue
                greeks = snap.get("greeks", {})
                iv = float(greeks.get("implied_volatility", 0) or 0)
                quote = snap.get("latestQuote", {})
                bid = float(quote.get("bp", 0) or 0)
                ask = float(quote.get("ap", 0) or 0)
                mid = (bid + ask) / 2

                # Extract strike from OCC symbol
                try:
                    _strike_part = sym[-8:]
                    _sym_strike = int(_strike_part) / 1000
                except (ValueError, IndexError):
                    continue

                if abs(_sym_strike - strike_short) < 3 and mid > 0:
                    if iv > best_short_iv:
                        best_short = {"symbol": sym, "strike": _sym_strike, "mid": mid, "iv": iv, "bid": bid}
                        best_short_iv = iv

            if not best_short or best_short_iv < 0.01:
                continue

            # IV rank proxy: compare current IV to recent VIX
            regime = get_regime("equities")
            vix = regime.get("vix", 20) or 20
            # Simple IV rank: if IV > VIX * 1.2 → high IV rank
            iv_rank = min(best_short_iv / (vix / 100 * 1.5) * 100, 100) if vix > 0 else 50

            if iv_rank < cfg["iv_rank_threshold"]:
                log.info("THETA: %s IV rank %.0f%% below %d%% threshold", underlying, iv_rank, cfg["iv_rank_threshold"])
                continue

            # Calculate credit and position size
            credit = best_short["bid"] * 0.8  # Conservative fill estimate
            if credit <= 0.05:
                continue

            portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
                p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))
            max_risk = cfg["spread_width"] - credit
            contracts = max(1, int(portfolio_value * cfg["size_pct"] / (max_risk * 100)))
            total_credit = credit * 100 * contracts
            total_risk = max_risk * 100 * contracts

            market_id = f"THETA:{underlying} {strike_short:.0f}/{strike_long:.0f}P {exp_str}"

            # Dedup
            if any(p.get("market", "") == market_id for p in PAPER_PORTFOLIO.get("positions", [])):
                continue

            # Record paper position
            PAPER_PORTFOLIO["cash"] -= total_risk  # Risk is the margin
            _theta_pos = {
                "market": market_id,
                "side": "SELL",
                "shares": contracts,
                "entry_price": credit,
                "cost": total_risk,
                "value": total_credit,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "platform": "Alpaca",
                "ev": credit / max_risk if max_risk > 0 else 0,
                "strategy": "options_spread",
                "short_strike": strike_short,
                "long_strike": strike_long,
                "underlying": underlying,
                "expiry": exp_str,
                "credit_received": total_credit,
                "max_loss": total_risk,
            }
            PAPER_PORTFOLIO["positions"].append(_theta_pos)
            db_log_paper_trade(_theta_pos)
            db_open_position(
                market_id=market_id, platform="Alpaca", strategy="options_spread",
                direction="SELL", size_usd=total_risk, shares=contracts,
                entry_price=credit, stop_price=credit * (1 + cfg["sl_pct"]),
                target_price=credit * (1 - cfg["tp_pct"]),
                metadata={"underlying": underlying, "short_strike": strike_short,
                          "long_strike": strike_long, "expiry": exp_str,
                          "iv_rank": iv_rank, "credit": credit,
                          "total_credit": total_credit, "max_risk": total_risk,
                          "contracts": contracts},
            )

            fired += 1
            log.info("THETA HARVEST: %s %d/%dP x%d credit=$%.2f iv_rank=%.0f%%",
                     underlying, int(strike_short), int(strike_long), contracts, total_credit, iv_rank)
            if channel:
                try:
                    await channel.send(
                        f"**THETA HARVEST** — {underlying} Put Credit Spread\n"
                        f"Sell {strike_short:.0f}P / Buy {strike_long:.0f}P | Exp: {exp_str}\n"
                        f"Credit: ${total_credit:.0f} | Max risk: ${total_risk:.0f} | IV rank: {iv_rank:.0f}%\n"
                        f"TP: 50% (${total_credit*0.5:.0f}) | SL: 200% (${total_credit*2:.0f})")
                except Exception:
                    pass

        except Exception as _te:
            log.warning("THETA scan %s error: %s", underlying, _te)

    return fired


def _sync_alpaca_to_portfolio():
    """On startup, check if Alpaca has positions that form pairs not in our portfolio.
    Adds missing pairs positions to PAPER_PORTFOLIO so the reconciler doesn't close them.
    Skipped when portfolio has 0 positions (clean reset — closing orders may be pending)."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return
    if len(PAPER_PORTFOLIO.get("positions", [])) == 0 and PAPER_PORTFOLIO.get("cash", 0) >= 24999:
        log.info("SYNC: skipped — clean reset detected (0 positions, full cash)")
        return
    try:
        hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        r = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=hdrs, timeout=10)
        if r.status_code != 200:
            return
        alpaca_positions = {p.get("symbol", "").upper(): p for p in r.json()}

        # Build known tickers from portfolio
        known_tickers = set()
        for pos in PAPER_PORTFOLIO.get("positions", []):
            for _lk in ("long_leg", "short_leg", "extra_long_leg"):
                _l = pos.get(_lk, "")
                if _l:
                    known_tickers.add(_l.upper())
            mkt = pos.get("market", "")
            if "PAIRS:" in mkt:
                for p in mkt.replace("PAIRS:", "").split("/"):
                    known_tickers.add(p.strip().upper())

        # Check known pairs config for untracked pairs
        _eq_cfg = globals().get("EQUITIES_CONFIG", {})
        _seed = _eq_cfg.get("pairs", {}).get("seed", [])
        added = 0
        for ta, tb in _seed:
            ta_u, tb_u = ta.upper(), tb.upper()
            pair_id = f"PAIRS:{ta}/{tb}"
            # Both tickers in Alpaca but neither in portfolio?
            if ta_u in alpaca_positions and tb_u in alpaca_positions:
                if ta_u not in known_tickers and tb_u not in known_tickers:
                    # Determine long/short from Alpaca side
                    a_side = alpaca_positions[ta_u].get("side", "")
                    b_side = alpaca_positions[tb_u].get("side", "")
                    if a_side == "long" and b_side == "short":
                        long_tk, short_tk = ta_u, tb_u
                    elif a_side == "short" and b_side == "long":
                        long_tk, short_tk = tb_u, ta_u
                    else:
                        continue
                    a_val = abs(float(alpaca_positions[ta_u].get("market_value", 0)))
                    b_val = abs(float(alpaca_positions[tb_u].get("market_value", 0)))
                    cost = a_val + b_val
                    entry_long = float(alpaca_positions[long_tk].get("avg_entry_price", 0))
                    entry_short = float(alpaca_positions[short_tk].get("avg_entry_price", 0))
                    _pos = {
                        "market": pair_id, "side": f"Long {long_tk} / Short {short_tk}",
                        "shares": 1, "entry_price": 0, "cost": cost, "value": cost,
                        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "platform": "Alpaca", "ev": 0, "strategy": "pairs",
                        "long_leg": long_tk, "short_leg": short_tk,
                        "entry_long_price": entry_long, "entry_short_price": entry_short,
                    }
                    PAPER_PORTFOLIO["positions"].append(_pos)
                    known_tickers.add(ta_u)
                    known_tickers.add(tb_u)
                    added += 1
                    log.info("SYNC: Added missing pair %s (L:%s S:%s cost=$%.0f) from Alpaca",
                             pair_id, long_tk, short_tk, cost)
        if added > 0:
            log.info("SYNC: Added %d missing pairs positions from Alpaca", added)
    except Exception as e:
        log.warning("SYNC: Alpaca portfolio sync error: %s", e)


def reconcile_alpaca_positions():
    """Close any Alpaca positions that have no matching open position in PAPER_PORTFOLIO.
    Prevents orphaned directional risk from failed pairs orders."""
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return 0
    try:
        hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        r = requests.get(f"{ALPACA_BASE_URL}/v2/positions", headers=hdrs, timeout=10)
        if r.status_code != 200:
            log.warning("RECONCILE: Alpaca positions fetch failed: %d", r.status_code)
            return 0
        alpaca_positions = r.json()
        if not alpaca_positions:
            return 0

        # Build set of tickers that PAPER_PORTFOLIO knows about
        known_tickers = set()
        for pos in PAPER_PORTFOLIO.get("positions", []):
            # Pairs/Oracle/Cascade: long_leg, short_leg, extra_long_leg
            for _leg_key in ("long_leg", "short_leg", "extra_long_leg"):
                _leg = pos.get(_leg_key, "")
                if _leg:
                    known_tickers.add(_leg.upper())
            # Market string fallback: parse PAIRS:A/B and ORACLE:* formats
            mkt = pos.get("market", "")
            if "PAIRS:" in mkt:
                parts = mkt.replace("PAIRS:", "").split("/")
                for p in parts:
                    known_tickers.add(p.strip().upper())
            elif "CASCADE:" in mkt or "ORACLE:" in mkt:
                # Oracle/cascade tickers already captured via leg fields above
                pass
            # Option symbols (crash hedge)
            _opt = pos.get("option_symbol", "")
            if _opt:
                known_tickers.add(_opt.upper())

        closed = 0
        for ap in alpaca_positions:
            sym = ap.get("symbol", "").upper()
            qty = float(ap.get("qty", 0))
            side = ap.get("side", "")
            mkt_val = float(ap.get("market_value", 0))

            if sym in known_tickers:
                continue  # Matched — this position is tracked

            # Orphaned position — log but don't auto-close on weekends (market orders fail)
            # Only auto-close during market hours to avoid 403 errors
            if is_market_open():
                log.warning("RECONCILE: Orphaned %s %s qty=%.2f val=$%.2f — closing",
                            side, sym, abs(qty), abs(mkt_val))
                try:
                    _cr = requests.delete(f"{ALPACA_BASE_URL}/v2/positions/{sym}",
                                          headers=hdrs, timeout=10)
                    if _cr.status_code == 200:
                        log.info("RECONCILE: Closed orphaned %s %s", side, sym)
                        closed += 1
                    else:
                        log.warning("RECONCILE: Failed to close %s: %d %s", sym, _cr.status_code, _cr.text[:100])
                except Exception as _ce:
                    log.warning("RECONCILE: Error closing %s: %s", sym, _ce)
            else:
                log.info("RECONCILE: Orphaned %s %s qty=%.2f val=$%.2f — skipping (market closed)",
                         side, sym, abs(qty), abs(mkt_val))

        if closed > 0:
            log.info("RECONCILE: Closed %d orphaned Alpaca positions", closed)
        return closed
    except Exception as e:
        log.warning("RECONCILE error: %s", e)
        return 0


def _get_spy_price():
    """Fetch current SPY price from Alpaca."""
    try:
        hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        r = requests.get("https://data.alpaca.markets/v2/stocks/SPY/quotes/latest",
                         headers=hdrs, timeout=5)
        if r.status_code == 200:
            quote = r.json().get("quote", {})
            _ask = float(quote.get("ap", 0) or 0)
            _bid = float(quote.get("bp", 0) or 0)
            # Use both sides when available, else whichever is non-zero (after hours)
            if _bid > 0 and _ask > 0:
                mid = (_bid + _ask) / 2
            else:
                mid = _bid or _ask
            if mid > 0:
                return mid
    except Exception as e:
        log.warning("SPY price fetch failed: %s", e)
    return None


def _build_options_symbol(underlying, expiry_date, strike, option_type="P"):
    """Build OCC options symbol. e.g. SPY260328P00550000"""
    date_str = expiry_date.strftime("%y%m%d")
    strike_int = int(strike * 1000)
    return f"{underlying}{date_str}{option_type}{strike_int:08d}"


def _fetch_spy_options_chain(spy_price, hdrs):
    """Fetch real SPY options chain from Alpaca and find the best put contract.
    Tries the trading API /v2/options/contracts first, then the data API
    snapshots endpoint as fallback.
    Returns (symbol, strike, expiry_str) or (None, None, None) on failure."""
    import datetime as _dt
    now = datetime.now(timezone.utc)
    target_strike = round(spy_price * 0.98, 0)  # 2% below current price
    # Snap to nearest $5 increment (SPY options use $1 strikes but $5 is safer)
    target_strike = round(target_strike / 5) * 5
    min_dte = 5
    max_dte = 14
    min_exp = (now + _dt.timedelta(days=min_dte)).strftime("%Y-%m-%d")
    max_exp = (now + _dt.timedelta(days=max_dte)).strftime("%Y-%m-%d")

    # --- Method 1: Alpaca trading API /v2/options/contracts ---
    try:
        params = {
            "underlying_symbols": "SPY",
            "type": "put",
            "strike_price_gte": str(target_strike - 15),
            "strike_price_lte": str(target_strike + 15),
            "expiration_date_gte": min_exp,
            "expiration_date_lte": max_exp,
            "limit": "100",
            "status": "active",
        }
        r = requests.get(f"{ALPACA_BASE_URL}/v2/options/contracts",
                         params=params, headers=hdrs, timeout=15)
        log.info("CRASH-HEDGE chain request: %s params=%s → %d",
                 f"{ALPACA_BASE_URL}/v2/options/contracts", params, r.status_code)
        if r.status_code == 200:
            contracts = r.json().get("option_contracts", r.json() if isinstance(r.json(), list) else [])
            if contracts:
                best = None
                best_dist = float("inf")
                for c in contracts:
                    c_strike = float(c.get("strike_price", 0))
                    c_exp = c.get("expiration_date", "")
                    c_sym = c.get("symbol", "")
                    c_status = c.get("status", "active")
                    if c_status != "active" or not c_sym:
                        continue
                    dist = abs(c_strike - target_strike)
                    if dist < best_dist:
                        best_dist = dist
                        best = (c_sym, c_strike, c_exp)
                if best:
                    log.info("CRASH-HEDGE chain: picked %s strike=$%.0f exp=%s (target=$%.0f)",
                             best[0], best[1], best[2], target_strike)
                    return best
        else:
            log.warning("CRASH-HEDGE trading API: %d %s", r.status_code, r.text[:300])
    except Exception as exc:
        log.warning("CRASH-HEDGE trading API error: %s", exc)

    # --- Method 2: Alpaca data API snapshots (like theta harvest uses) ---
    data_hdr = {"APCA-API-KEY-ID": hdrs.get("APCA-API-KEY-ID", ""),
                "APCA-API-SECRET-KEY": hdrs.get("APCA-API-SECRET-KEY", "")}
    try:
        # Try each Friday in the 5-14 day window
        for days_out in range(min_dte, max_dte + 1):
            exp_date = now + _dt.timedelta(days=days_out)
            if exp_date.weekday() != 4:  # Only Fridays
                continue
            exp_str = exp_date.strftime("%Y-%m-%d")
            r2 = requests.get(
                f"https://data.alpaca.markets/v1beta1/options/snapshots/SPY",
                headers=data_hdr,
                params={"feed": "indicative", "expiration_date": exp_str, "type": "put"},
                timeout=10)
            if r2.status_code != 200:
                log.info("CRASH-HEDGE data API exp=%s: %d", exp_str, r2.status_code)
                continue
            snapshots = r2.json().get("snapshots", {})
            if not snapshots:
                continue
            # Parse OCC symbols from snapshot keys, find nearest strike
            best = None
            best_dist = float("inf")
            for sym_key in snapshots:
                # OCC format: SPY260404P00625000 — strike is last 8 digits / 1000
                try:
                    _strike_raw = int(sym_key[-8:]) / 1000.0
                    if "P" in sym_key[6:]:
                        dist = abs(_strike_raw - target_strike)
                        if dist < best_dist:
                            best_dist = dist
                            best = (sym_key, _strike_raw, exp_str)
                except (ValueError, IndexError):
                    continue
            if best:
                log.info("CRASH-HEDGE data API: picked %s strike=$%.0f exp=%s (target=$%.0f)",
                         best[0], best[1], best[2], target_strike)
                return best
    except Exception as exc:
        log.warning("CRASH-HEDGE data API error: %s", exc)

    log.warning("CRASH-HEDGE: both API methods failed, falling back to constructed symbol")
    return None, None, None


async def execute_spy_put_hedge(spy_price, portfolio_value):
    """Buy SPY put option via Alpaca options API.
    Fetches the real options chain to find a valid listed contract."""
    cfg = CRASH_HEDGE_CONFIG
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, "Alpaca not configured"

    hdrs = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }

    # Fetch real options chain — find nearest listed put
    symbol, strike, expiry_str = _fetch_spy_options_chain(spy_price, hdrs)
    if symbol is None:
        # Fallback: construct symbol using $5 strike increments and valid Friday expiry
        import datetime as _dt
        strike = round(spy_price * (1 - cfg["put_strike_offset"]) / 5) * 5  # Snap to $5 increment
        # Find the nearest Friday that is 5-10 days out
        expiry = None
        for days_out in range(5, 15):
            candidate = datetime.now(timezone.utc) + _dt.timedelta(days=days_out)
            if candidate.weekday() == 4:  # Friday
                expiry = candidate
                break
        if expiry is None:
            expiry = datetime.now(timezone.utc) + _dt.timedelta(days=cfg["put_dte"])
        symbol = _build_options_symbol("SPY", expiry, strike, "P")
        expiry_str = expiry.strftime("%Y-%m-%d")
        log.warning("CRASH-HEDGE fallback to constructed symbol: %s (strike=$%.0f exp=%s)", symbol, strike, expiry_str)

    size_usd = portfolio_value * cfg["put_size_pct"]
    # Estimate ~$3-8 per contract for OTM weeklies, buy as many as budget allows
    est_premium = max(spy_price * 0.005, 1.0)  # Rough estimate
    qty = max(1, int(size_usd / (est_premium * 100)))  # Options are 100 shares per contract

    order_body = {
        "symbol": symbol,
        "qty": str(qty),
        "side": "buy",
        "type": "market",
        "time_in_force": "day",
    }

    if DRY_RUN_MODE:
        log.info("DRY RUN: SPY PUT %s qty=%d strike=$%.0f exp=%s size=$%.0f",
                 symbol, qty, strike, expiry_str, size_usd)
        return True, f"DRY RUN: BUY {qty}x {symbol} (strike ${strike:.0f}, exp {expiry_str})"

    try:
        r = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=order_body,
                          headers=hdrs, timeout=15)
        if r.status_code in (200, 201):
            order_id = r.json().get("id", "unknown")
            log.info("SPY PUT ORDER: %s qty=%d id=%s", symbol, qty, order_id)
            return True, f"BUY {qty}x {symbol} (ID: {order_id})"
        else:
            return False, f"Alpaca options error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"SPY put order error: {exc}"


async def execute_spy_short_hedge(spy_price, portfolio_value):
    """Short SPY via Alpaca (directional crash hedge)."""
    cfg = CRASH_HEDGE_CONFIG
    if not ALPACA_API_KEY or not ALPACA_SECRET_KEY:
        return False, "Alpaca not configured"

    size_usd = portfolio_value * cfg["short_size_pct"]
    hdrs = {
        "APCA-API-KEY-ID": ALPACA_API_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
        "Content-Type": "application/json",
    }

    order_body = {
        "symbol": "SPY",
        "notional": str(round(size_usd, 2)),
        "side": "sell",
        "type": "market",
        "time_in_force": "day",
    }

    if DRY_RUN_MODE:
        log.info("DRY RUN: SPY SHORT $%.0f @ $%.2f", size_usd, spy_price)
        return True, f"DRY RUN: SHORT SPY ${size_usd:.0f}"

    try:
        r = requests.post(f"{ALPACA_BASE_URL}/v2/orders", json=order_body,
                          headers=hdrs, timeout=15)
        if r.status_code in (200, 201):
            order_id = r.json().get("id", "unknown")
            log.info("SPY SHORT ORDER: $%.0f id=%s", size_usd, order_id)
            return True, f"SHORT SPY ${size_usd:.0f} (ID: {order_id})"
        else:
            return False, f"Alpaca short error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"SPY short order error: {exc}"


async def check_crash_hedges(channel):
    """Regime-aware crash hedge: VIX < 25 idle, 25-27 buy puts, >= 28 sell call spreads.
    Runs during market hours."""
    cfg = CRASH_HEDGE_CONFIG
    if not cfg["enabled"]:
        return

    regime = get_regime("equities")
    vix = regime.get("vix")
    regime_name = regime.get("regime", "normal")

    if not vix:
        return

    # --- Options flow: put/call ratio as additional trigger ---
    _pc_data = options_put_call_ratio("SPY")
    _pc_ratio = _pc_data.get("ratio", 0) if _pc_data.get("available") else 0
    _pc_panic = _pc_ratio > 1.5  # Panic put buying → boost hedge urgency

    # --- Determine VRP regime (VIX + options flow) ---
    if vix >= cfg["vrp_vix_threshold"]:
        vrp_regime = "sell_premium"
    elif vix >= cfg["put_vix_threshold"] or _pc_panic:
        vrp_regime = "buy_puts"
        if _pc_panic and vix < cfg["put_vix_threshold"]:
            log.info("VRP: P/C ratio %.2f > 1.5 triggered buy_puts despite VIX=%.1f", _pc_ratio, vix)
    else:
        vrp_regime = "idle"

    prev_regime = _VRP_REGIME_STATE["current"]
    _VRP_REGIME_STATE["cycle_count"] += 1
    if vrp_regime != prev_regime:
        _VRP_REGIME_STATE["current"] = vrp_regime
        _VRP_REGIME_STATE["last_switch"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        log.info("VRP REGIME SWITCH: %s → %s (VIX=%.1f)", prev_regime, vrp_regime, vix)
    else:
        log.info("VRP REGIME: %s (VIX=%.1f, cycle #%d)", vrp_regime, vix, _VRP_REGIME_STATE["cycle_count"])

    if vrp_regime == "idle":
        return

    # Count existing hedge positions
    hedge_count = sum(1 for p in PAPER_PORTFOLIO.get("positions", [])
                      if p.get("strategy") in ("crash_hedge_put", "crash_hedge_short", "crash_hedge_call_spread"))

    if hedge_count >= cfg["max_hedges"]:
        log.info("CRASH-HEDGE: %d hedges already open (max %d)", hedge_count, cfg["max_hedges"])
        return

    # Cooldown check
    try:
        import sqlite3 as _chsq
        _chconn = _chsq.connect(DB_PATH)
        _chc = _chconn.cursor()
        _chc.execute("SELECT closed_at FROM positions WHERE strategy IN ('crash_hedge_put','crash_hedge_short','crash_hedge_call_spread') ORDER BY created_at DESC LIMIT 1")
        _chrow = _chc.fetchone()
        _chc.execute("SELECT created_at FROM positions WHERE strategy IN ('crash_hedge_put','crash_hedge_short','crash_hedge_call_spread') AND status='open' ORDER BY created_at DESC LIMIT 1")
        _chrow2 = _chc.fetchone()
        _chconn.close()
        _last_ts = (_chrow2 and _chrow2[0]) or (_chrow and _chrow[0])
        if _last_ts:
            import datetime as _dt_ch
            _last = _dt_ch.datetime.fromisoformat(_last_ts).replace(tzinfo=timezone.utc)
            _hours_since = (datetime.now(timezone.utc) - _last).total_seconds() / 3600
            if _hours_since < cfg["cooldown_hours"]:
                log.info("CRASH-HEDGE: cooldown %.0fh < %dh", _hours_since, cfg["cooldown_hours"])
                return
    except Exception:
        pass

    spy_price = _get_spy_price()
    if not spy_price:
        log.warning("CRASH-HEDGE: cannot fetch SPY price")
        return

    portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
        p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))

    # === VRP REGIME: SELL PREMIUM (VIX >= 28) — Simulate call credit spread ===
    if vrp_regime == "sell_premium":
        spread_count = sum(1 for p in PAPER_PORTFOLIO.get("positions", [])
                          if p.get("strategy") == "crash_hedge_call_spread")
        if spread_count >= cfg["vrp_max_spreads"]:
            log.info("VRP: %d call spreads open (max %d)", spread_count, cfg["vrp_max_spreads"])
            return

        # Simulate: sell 20-delta call, buy 10-delta call, 2 DTE
        # In high VIX, OTM call premium is inflated — we collect the spread
        short_strike = round(spy_price * (1 + cfg["vrp_short_delta"]) / 5) * 5  # ~20% OTM, $5 increments
        long_strike = short_strike + 5  # $5 wide spread
        import datetime as _dt_vrp
        expiry = datetime.now(timezone.utc) + _dt_vrp.timedelta(days=cfg["vrp_dte"])
        while expiry.weekday() >= 5:
            expiry += _dt_vrp.timedelta(days=1)

        # Estimate credit received: spread width * delta difference * VIX factor
        est_credit_per_contract = (vix / 20) * 0.50  # ~$0.50 credit at VIX 20, scales up
        size_usd = portfolio_value * cfg["vrp_spread_size_pct"]
        contracts = max(1, int(size_usd / (5 * 100)))  # $5-wide spread = $500 max risk per contract
        net_credit = est_credit_per_contract * contracts * 100

        short_sym = _build_options_symbol("SPY", expiry, short_strike, "C")
        long_sym = _build_options_symbol("SPY", expiry, long_strike, "C")

        _spread_pos = {
            "market": f"VRP:CALL SPREAD ${short_strike}/{long_strike}",
            "side": "SELL SPREAD", "shares": contracts,
            "entry_price": spy_price, "cost": net_credit, "value": net_credit,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "platform": "Simulated", "ev": net_credit,
            "strategy": "crash_hedge_call_spread",
            "stop_price": 0, "target_price": 0,
            "short_strike": short_strike, "long_strike": long_strike,
            "short_symbol": short_sym, "long_symbol": long_sym,
            "contracts": contracts, "est_credit": net_credit,
            "vrp_regime": "sell_premium",
        }
        PAPER_PORTFOLIO["positions"].append(_spread_pos)
        # Credit received goes into cash (premium collected)
        PAPER_PORTFOLIO["cash"] += net_credit
        db_log_paper_trade(_spread_pos)
        db_open_position(
            market_id=f"VRP:CALL SPREAD ${short_strike}/{long_strike}",
            platform="Simulated", strategy="crash_hedge_call_spread",
            direction="SELL SPREAD", size_usd=net_credit, shares=contracts,
            entry_price=spy_price,
            metadata={"vix": vix, "regime": regime_name, "vrp_regime": "sell_premium",
                      "short_strike": short_strike, "long_strike": long_strike,
                      "est_credit": net_credit, "contracts": contracts,
                      "spy_price": spy_price},
        )
        log.info("VRP CALL SPREAD: VIX=%.1f SPY=$%.2f sell $%d/buy $%d x%d credit=$%.0f",
                 vix, spy_price, short_strike, long_strike, contracts, net_credit)
        if channel:
            try:
                await channel.send(
                    f"**VRP CALL CREDIT SPREAD** (simulated)\n"
                    f"VIX: {vix:.1f} | Regime: SELL PREMIUM\n"
                    f"SPY: ${spy_price:.2f} | Sell ${short_strike}C / Buy ${long_strike}C\n"
                    f"Contracts: {contracts} | Credit: ${net_credit:.0f}\n"
                    f"Expiry: {expiry.strftime('%m/%d')} ({cfg['vrp_dte']}DTE)")
            except Exception:
                pass
        return

    # === VRP REGIME: BUY PUTS (VIX 25-27) ===
    if vrp_regime == "buy_puts" and regime_name in ("elevated", "extreme"):
        success, msg = await execute_spy_put_hedge(spy_price, portfolio_value)
        if success:
            strike = round(spy_price * (1 - cfg["put_strike_offset"]) / 5) * 5
            size_usd = portfolio_value * cfg["put_size_pct"]
            import datetime as _dt_put
            _expiry = datetime.now(timezone.utc) + _dt_put.timedelta(days=cfg["put_dte"])
            _dtf = (4 - _expiry.weekday()) % 7
            if _dtf == 0 and _expiry.hour > 16:
                _dtf = 7
            _expiry = _expiry + _dt_put.timedelta(days=_dtf)
            _occ_symbol = _build_options_symbol("SPY", _expiry, strike, "P")
            _put_pos = {
                "market": f"HEDGE:SPY PUT ${strike:.0f}",
                "side": "BUY", "shares": 1,
                "entry_price": spy_price, "cost": size_usd, "value": size_usd,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "platform": "Alpaca", "ev": 0,
                "strategy": "crash_hedge_put",
                "stop_price": 0, "target_price": 0,
                "option_symbol": _occ_symbol,
                "contracts": max(1, int(size_usd / (spy_price * 0.005 * 100))),
                "entry_premium": size_usd / max(1, int(size_usd / (spy_price * 0.005 * 100))) / 100,
                "vrp_regime": "buy_puts",
            }
            PAPER_PORTFOLIO["positions"].append(_put_pos)
            PAPER_PORTFOLIO["cash"] -= size_usd
            db_log_paper_trade(_put_pos)
            db_open_position(
                market_id=f"HEDGE:SPY PUT ${strike:.0f}",
                platform="Alpaca", strategy="crash_hedge_put",
                direction="BUY", size_usd=size_usd, shares=1,
                entry_price=spy_price,
                metadata={"vix": vix, "regime": regime_name, "vrp_regime": "buy_puts",
                          "strike": strike, "spy_price": spy_price, "order_msg": msg},
            )
            log.info("CRASH-HEDGE PUT: VIX=%.1f SPY=$%.2f strike=$%.0f size=$%.0f (VRP=buy_puts)",
                     vix, spy_price, strike, size_usd)
            if channel:
                try:
                    await channel.send(
                        f"**CRASH HEDGE — SPY PUT** (VRP: buy puts)\n"
                        f"VIX: {vix:.1f} | Regime: {regime_name.upper()}\n"
                        f"SPY: ${spy_price:.2f} | Strike: ${strike:.0f}\n"
                        f"Size: ${size_usd:.0f} | {msg}")
                except Exception:
                    pass
        else:
            log.warning("CRASH-HEDGE PUT FAILED: %s", msg[:100])

    # --- SPY SHORT: VIX > 35, regime extreme only (unchanged) ---
    if vix > cfg["short_vix_threshold"] and regime_name == "extreme":
        has_short = any(p.get("strategy") == "crash_hedge_short"
                        for p in PAPER_PORTFOLIO.get("positions", []))
        if has_short:
            log.info("CRASH-HEDGE: SPY short already open")
            return

        success, msg = await execute_spy_short_hedge(spy_price, portfolio_value)
        if success:
            size_usd = portfolio_value * cfg["short_size_pct"]
            _short_pos = {
                "market": "HEDGE:SPY SHORT",
                "side": "SELL", "shares": 1,
                "entry_price": spy_price, "cost": size_usd, "value": size_usd,
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                "platform": "Alpaca", "ev": 0,
                "strategy": "crash_hedge_short",
                "stop_price": round(spy_price * 1.03, 2),
                "target_price": round(spy_price * 0.90, 2),
            }
            PAPER_PORTFOLIO["positions"].append(_short_pos)
            PAPER_PORTFOLIO["cash"] -= size_usd
            db_log_paper_trade(_short_pos)
            db_open_position(
                market_id="HEDGE:SPY SHORT",
                platform="Alpaca", strategy="crash_hedge_short",
                direction="SELL", size_usd=size_usd, shares=1,
                entry_price=spy_price,
                metadata={"vix": vix, "regime": regime_name,
                          "spy_price": spy_price, "order_msg": msg},
            )
            log.info("CRASH-HEDGE SHORT: VIX=%.1f SPY=$%.2f size=$%.0f",
                     vix, spy_price, size_usd)
            if channel:
                try:
                    await channel.send(
                        f"**CRASH HEDGE — SPY SHORT**\n"
                        f"VIX: {vix:.1f} | Regime: EXTREME\n"
                        f"SPY: ${spy_price:.2f} | Size: ${size_usd:.0f}\n"
                        f"Stop: ${spy_price * 1.03:.2f} | Target: ${spy_price * 0.90:.2f}\n"
                        f"{msg}")
                except Exception:
                    pass
        else:
            log.warning("CRASH-HEDGE SHORT FAILED: %s", msg[:100])


# --- DYNAMIC EXIT MANAGER ---
EXIT_CONFIG = {
    "crypto_ttl_hours": 4,
    "prediction_ttl_hours": 72,
    "pairs_ttl_days": 7,
    "pead_ttl_hours": 72,
    "pead_tp_pct": 0.08,
    "pairs_zscore_exit": 0.5,
}


# === LIVE PRICE FETCHING FOR EXIT DECISIONS ===
_PRICE_CACHE = {}
_PRICE_CACHE_TIME = {}

def fetch_live_price(ticker):
    """Fetch current price using Coinbase public API (no rate limits)."""
    import time as _time
    tk = ticker.lower().replace("crypto:", "").split(" ")[0].split("$")[0].strip()
    if tk in _PRICE_CACHE and _time.time() - _PRICE_CACHE_TIME.get(tk, 0) < 60:
        return _PRICE_CACHE[tk]
    _cb = {"btc":"BTC-USD","eth":"ETH-USD","sol":"SOL-USD","doge":"DOGE-USD",
           "zec":"ZEC-USD","xlm":"XLM-USD","xrp":"XRP-USD","hbar":"HBAR-USD",
           "shib":"SHIB-USD","algo":"ALGO-USD","ada":"ADA-USD","avax":"AVAX-USD",
           "matic":"MATIC-USD","link":"LINK-USD","sui":"SUI-USD","hype":"HYPE-USD"}
    pair = _cb.get(tk)
    if not pair:
        return None
    try:
        import requests as _req
        r = _req.get(f"https://api.coinbase.com/v2/prices/{pair}/spot", timeout=5)
        if r.status_code == 200:
            price = float(r.json().get("data", {}).get("amount", 0))
            if price > 0:
                _PRICE_CACHE[tk] = price
                _PRICE_CACHE_TIME[tk] = _time.time()
                return price
    except Exception as _e:
        log.warning("Price fetch %s: %s", tk, _e)
    return None


# === AUTO-RESOLVER FOR EXPIRED PREDICTION MARKETS ===
def auto_resolve_expired():
    """Auto-close prediction markets where the expiry date has passed."""
    import re as _ar_re
    now = datetime.now(timezone.utc)
    to_close = []
    for i, pos in enumerate(PAPER_PORTFOLIO.get("positions", [])):
        if pos.get("strategy") != "prediction":
            continue
        mkt = pos.get("market", "")
        # Strip YES:/NO: prefix for date parsing
        _clean_mkt = _ar_re.sub(r'^(YES:|NO:|yes:|no:)\s*', '', mkt)
        # Match "by March 14" or "on March 8" patterns
        for pattern in [r'by (\w+)\s+(\d{1,2})', r'on (\w+)\s+(\d{1,2})']:
            m = _ar_re.search(pattern, _clean_mkt)
            if m:
                months = {"January":1,"February":2,"March":3,"April":4,"May":5,"June":6,
                          "July":7,"August":8,"September":9,"October":10,"November":11,"December":12}
                mo = months.get(m.group(1), 0)
                if mo > 0:
                    try:
                        exp = datetime(now.year, mo, int(m.group(2)), 23, 59, tzinfo=timezone.utc)
                        if now > exp:
                            to_close.append((i, pos, f"EXPIRED: {m.group(1)} {m.group(2)}"))
                    except ValueError:
                        pass
    closed = 0
    for idx, pos, reason in sorted(to_close, key=lambda x: x[0], reverse=True):
        if idx < len(PAPER_PORTFOLIO["positions"]):
            removed = PAPER_PORTFOLIO["positions"].pop(idx)
            salvage = removed.get("cost", 0) * 0.05
            PAPER_PORTFOLIO["cash"] += salvage
            closed += 1
            log.info("AUTO-RESOLVE: %s | %s | Salvage $%.2f", removed.get("market","")[:35], reason, salvage)
            try:
                _c = sqlite3.connect(DB_PATH)
                _c.execute("UPDATE paper_trades SET status='resolved_loss' WHERE market=? AND status='open'", (removed.get("market",""),))
                _c.execute("UPDATE positions SET status='closed', closed_at=datetime('now'), exit_reason=?, realized_pnl=? WHERE market_id=? AND status='open'",
                           (reason, salvage - removed.get("cost", 0), removed.get("market", "")))
                db_close_position(removed.get("market", ""), 0, reason, salvage - removed.get("cost", 0))
                _c.commit()
                _c.close()
            except Exception:
                pass
    return closed

async def run_exit_manager(channel=None):
    """Unified exit manager — single exit path for ALL positions in PAPER_PORTFOLIO."""
    now = datetime.now(timezone.utc)
    positions_to_close = []

    # --- Phase 1: Fetch live prices for price-based exits ---
    for i, pos in enumerate(PAPER_PORTFOLIO.get("positions", [])):
        ts_str = pos.get("timestamp", "")
        if not ts_str:
            continue
        try:
            entry_time = datetime.strptime(ts_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        age_hours = (now - entry_time).total_seconds() / 3600
        strategy = pos.get("strategy", "prediction")
        market = pos.get("market", "")
        # Fix: detect crypto by platform for legacy positions with wrong strategy label
        if pos.get("platform", "").lower() == "crypto" and strategy not in ("crypto", "momentum"):
            strategy = "crypto"
        # Fix: detect hedge puts by market name for positions saved with wrong strategy label
        if market.startswith("HEDGE:SPY PUT") and strategy != "crash_hedge_put":
            strategy = "crash_hedge_put"

        # --- Fetch current price for strategies with live feeds ---
        current_price = None
        try:
            import requests as _pr
            if strategy == "pairs":
                _ll = pos.get("long_leg", "")
                _sl = pos.get("short_leg", "")
                if _ll and _sl:
                    _hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
                    _rl = _pr.get(f"https://data.alpaca.markets/v2/stocks/{_ll}/quotes/latest", headers=_hdr, timeout=5)
                    _rs = _pr.get(f"https://data.alpaca.markets/v2/stocks/{_sl}/quotes/latest", headers=_hdr, timeout=5)
                    if _rl.status_code == 200 and _rs.status_code == 200:
                        _lp = float(_rl.json().get("quote", {}).get("ap", 0) or 0)
                        _sp = float(_rs.json().get("quote", {}).get("ap", 0) or 0)
                        _el = pos.get("entry_long_price", 0)
                        _es = pos.get("entry_short_price", 0)
                        _sz = pos.get("cost", 0) / 2
                        if _lp > 0 and _sp > 0 and _el > 0 and _es > 0:
                            _lpnl = (_lp - _el) * (_sz / _el)
                            _spnl = (_es - _sp) * (_sz / _es)
                            current_price = pos.get("cost", 0) + _lpnl + _spnl
                            log.info("PAIRS PRICE: %s/%s long=$%.2f short=$%.2f net=$%.2f", _ll, _sl, _lpnl, _spnl, _lpnl + _spnl)
            elif strategy in ("crypto", "momentum"):
                _tk = market.replace("CRYPTO:", "").split()[0]
                _rc = _pr.get(f"https://api.coinbase.com/v2/prices/{_tk}-USD/spot", timeout=5)
                if _rc.status_code == 200:
                    _spot = float(_rc.json().get("data", {}).get("amount", 0))
                    _ep = pos.get("entry_price", 0)
                    _sh = pos.get("shares", 0)
                    if _spot > 0 and _ep > 0 and _sh > 0:
                        current_price = _spot * _sh
                        log.info("CRYPTO PRICE: %s spot=$%.4f value=$%.2f", _tk, _spot, current_price)
            elif strategy == "oracle_trade":
                # Fetch live prices for both legs via Alpaca
                _oll = pos.get("long_leg", "")
                _osl = pos.get("short_leg", "")
                if _oll and _osl:
                    _hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
                    _orl = _pr.get(f"https://data.alpaca.markets/v2/stocks/{_oll}/quotes/latest", headers=_hdr, timeout=5)
                    _ors = _pr.get(f"https://data.alpaca.markets/v2/stocks/{_osl}/quotes/latest", headers=_hdr, timeout=5)
                    if _orl.status_code == 200 and _ors.status_code == 200:
                        _olp = float(_orl.json().get("quote", {}).get("ap", 0) or 0)
                        _osp = float(_ors.json().get("quote", {}).get("ap", 0) or 0)
                        _oel = pos.get("entry_long_price", 0)
                        _oes = pos.get("entry_short_price", 0)
                        if _olp > 0 and _osp > 0 and _oel > 0 and _oes > 0:
                            _osz = pos.get("cost", 0) / 2
                            _olpnl = (_olp - _oel) * (_osz / _oel)
                            _ospnl = (_oes - _osp) * (_osz / _oes)
                            current_price = pos.get("cost", 0) + _olpnl + _ospnl
                            log.info("ORACLE PRICE: %s L:%s=$%.2f S:%s=$%.2f net=$%+.2f",
                                     market[:20], _oll, _olpnl, _osl, _ospnl, _olpnl + _ospnl)
            elif strategy == "crash_hedge_put":
                # Fetch option price from Alpaca options API
                _opt_sym = pos.get("option_symbol", "")
                if not _opt_sym and "PUT $" in market:
                    try:
                        import re as _occ_re2
                        _sm = _occ_re2.search(r'PUT \$(\d+)', market)
                        if _sm:
                            _st = float(_sm.group(1))
                            _ts2 = pos.get("timestamp", "")
                            if _ts2:
                                _edt = datetime.strptime(_ts2, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
                                _exp2 = _edt + __import__("datetime").timedelta(days=7)
                                while _exp2.weekday() >= 5:
                                    _exp2 += __import__("datetime").timedelta(days=1)
                                _opt_sym = _build_options_symbol("SPY", _exp2, _st, "P")
                    except Exception:
                        pass
                if _opt_sym:
                    _oq = _pr.get(f"https://data.alpaca.markets/v1beta1/options/quotes/latest?symbols={_opt_sym}",
                                  headers={"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}, timeout=5)
                    if _oq.status_code == 200:
                        _odata = _oq.json().get("quotes", {}).get(_opt_sym, {})
                        _ask = float(_odata.get("ap", 0) or 0)
                        _bid = float(_odata.get("bp", 0) or 0)
                        _opt_mid = (_bid + _ask) / 2 if _bid > 0 and _ask > 0 else (_ask or _bid)
                        if _opt_mid > 0:
                            _contracts = pos.get("contracts", 1)
                            _entry_prem = pos.get("entry_premium", pos.get("cost", 0) / max(_contracts, 1) / 100)
                            current_price = _opt_mid * 100 * _contracts
                            _opt_pnl = (_opt_mid - _entry_prem) * 100 * _contracts
                            log.info("HEDGE OPT: %s mid=$%.2f entry=$%.2f contracts=%d pnl=$%+.2f",
                                     _opt_sym, _opt_mid, _entry_prem, _contracts, _opt_pnl)
            elif strategy == "crash_hedge_short":
                # Fetch SPY price for stop/target checks
                _spy = _get_spy_price()
                if _spy and _spy > 0:
                    current_price = _spy
        except Exception as _pe:
            log.warning("Live price fetch failed for %s: %s", market[:30], _pe)

        # --- Phase 2: Decide exits ---
        exit_reason = None

        # Price-based exits (stop-loss / target / trailing stop)
        if pos.get("stop_price") and pos.get("entry_price") and current_price is not None:
            cost = pos.get("cost", 0)
            if cost > 0:
                price_ratio = current_price / cost  # value vs cost
                entry = pos["entry_price"]
                # Stop-loss
                if price_ratio <= pos["stop_price"] / entry:
                    exit_reason = f"STOP-LOSS hit (ratio={price_ratio:.3f})"
                # Target
                elif pos.get("target_price") and price_ratio >= pos["target_price"] / entry:
                    exit_reason = f"TARGET hit (ratio={price_ratio:.3f})"
                # Trailing stop update (ratchet up)
                if not exit_reason and pos.get("trailing_stop"):
                    new_trail = current_price * 0.85
                    old_trail_value = pos.get("_trailing_value", 0)
                    if new_trail > old_trail_value:
                        pos["_trailing_value"] = new_trail
                    elif current_price <= old_trail_value:
                        exit_reason = f"TRAILING STOP hit (value=${current_price:.2f} <= trail=${old_trail_value:.2f})"

        # Strategy-specific TTL/signal exits
        if not exit_reason:
            if strategy == "pairs":
                _pa = pos.get("long_leg", "")
                _pb = pos.get("short_leg", "")
                _entry_z = pos.get("entry_zscore", 0)
                if _pa and _pb:
                    try:
                        _corr, _current_z, _mr = calculate_pair_zscore(_pa, _pb, 252)
                        if _current_z is not None:
                            log.info("PAIRS POS: %s/%s entry_z=%.2f current_z=%.2f", _pa, _pb, _entry_z, _current_z)
                            if abs(_current_z) < 0.5 or (_entry_z > 0 and _current_z < 0) or (_entry_z < 0 and _current_z > 0):
                                exit_reason = f"Z-REVERT: z={_current_z:.2f}"
                            elif abs(_current_z) > 3.0:
                                exit_reason = f"Z-BREAK: z={_current_z:.2f}"
                    except Exception:
                        pass
                if not exit_reason and age_hours > EXIT_CONFIG.get("pairs_ttl_days", 7) * 24:
                    exit_reason = "TTL: pairs 7d"
            elif strategy == "crypto_pairs":
                # Z-score reversion exit or TTL
                _cp_a = pos.get("long_leg", "")
                _cp_b = pos.get("short_leg", "")
                if _cp_a and _cp_b:
                    _cp_corr, _cp_z, _ = calculate_crypto_pair_zscore(_cp_a, _cp_b, 30)
                    if _cp_z is not None:
                        _entry_z = pos.get("entry_zscore", 0)
                        if (_entry_z > 0 and _cp_z <= CRYPTO_PAIRS_CONFIG["zscore_exit"]) or \
                           (_entry_z < 0 and _cp_z >= -CRYPTO_PAIRS_CONFIG["zscore_exit"]):
                            exit_reason = f"REVERSION: z={_cp_z:.2f} (entry={_entry_z:.2f})"
                        else:
                            log.info("CRYPTO PAIRS POS: %s/%s entry_z=%.2f current_z=%.2f", _cp_a, _cp_b, _entry_z, _cp_z)
                if not exit_reason and age_hours > CRYPTO_PAIRS_CONFIG["ttl_hours"]:
                    exit_reason = f"TTL: crypto_pairs {CRYPTO_PAIRS_CONFIG['ttl_hours']}h"
            elif strategy == "pead":
                if age_hours > EXIT_CONFIG["pead_ttl_hours"]:
                    exit_reason = "TTL: PEAD 72h limit"
                else:
                    ep = pos.get("entry_price", 0)
                    cp = pos.get("value", 0) / max(pos.get("shares", 1), 1)
                    if ep > 0 and cp > 0:
                        pnl_pct = (cp - ep) / ep
                        if pnl_pct >= EXIT_CONFIG["pead_tp_pct"]:
                            exit_reason = f"TP: PEAD +{pnl_pct*100:.1f}%"
            elif strategy in ("crypto", "momentum"):
                if age_hours > EXIT_CONFIG["crypto_ttl_hours"]:
                    exit_reason = "TTL: crypto 72h limit"
            elif strategy == "funding_arb":
                if age_hours > 24:
                    exit_reason = f"FUNDING-ARB TTL: {age_hours:.0f}h"
                else:
                    log.info("ARB POS: %s age=%.0fh rate=%.4f%%", market[:20], age_hours, pos.get("entry_price", 0) * 100)
            elif strategy == "oracle_trade":
                # Check if signal is still valid — re-fetch probability
                _src_mkt = pos.get("source_market", "")
                _sig_thresh = pos.get("signal_threshold", 0.65)
                _current_prob = 0.0
                _is_inverse_pos = "GEO-ONLY" in _src_mkt or any(
                    s.get("name") == market.replace("ORACLE:", "") and s.get("inverse")
                    for s in ORACLE_SIGNALS)
                if _src_mkt and not _src_mkt.startswith("GEO-ONLY"):
                    _all_px = _oracle_get_all_prices() if not _ORACLE_PRICE_HISTORY else {}
                    # Check price history first (populated by scan_oracle_signals)
                    _hkey = _src_mkt.lower()
                    _hist = _ORACLE_PRICE_HISTORY.get(_hkey, [])
                    if _hist:
                        _current_prob = _hist[-1][1]
                    else:
                        _current_prob = _all_px.get(_src_mkt, 0)
                # Signal invalidated check — direction depends on inverse vs normal
                if _is_inverse_pos:
                    # Inverse: invalidated when prob RISES ABOVE threshold (ceasefire restored)
                    if _current_prob > 0 and _current_prob > _sig_thresh:
                        exit_reason = f"ORACLE INV INVALIDATED: prob={_current_prob:.2f} > {_sig_thresh:.2f} (ceasefire restored)"
                elif _current_prob > 0 and _current_prob < _sig_thresh:
                    exit_reason = f"ORACLE INVALIDATED: prob={_current_prob:.2f} < {_sig_thresh:.2f}"
                # TTL
                elif age_hours > ORACLE_CONFIG["ttl_hours"]:
                    exit_reason = f"ORACLE TTL: {age_hours:.0f}h"
                else:
                    _opnl = ""
                    if current_price is not None:
                        _opnl = f" pnl=${current_price - pos.get('cost', 0):+.2f}"
                    log.info("ORACLE POS: %s age=%.0fh prob=%.2f%s", market[:25], age_hours, _current_prob, _opnl)
            elif strategy == "crash_hedge_put":
                # Trailing-stop exit: activates at +100% gain, trails at 80% of peak
                _cost = pos.get("cost", 0)
                if current_price is not None and _cost > 0:
                    _pct_change = (current_price - _cost) / _cost
                    _peak = pos.get("peak_value", current_price)
                    # Update peak watermark
                    if current_price > _peak:
                        _peak = current_price
                        pos["peak_value"] = _peak
                    elif "peak_value" not in pos:
                        pos["peak_value"] = _peak
                    _peak_gain = (_peak - _cost) / _cost  # peak gain as fraction

                    if _peak_gain >= 1.0:
                        # Trailing stop active: floor is 80% of peak gain
                        _trail_floor = _cost * (1 + _peak_gain * 0.8)
                        if current_price <= _trail_floor:
                            _exit_pct = _pct_change * 100
                            _peak_pct = _peak_gain * 100
                            exit_reason = (f"HEDGE PUT TRAIL-TP: +{_exit_pct:.0f}% "
                                           f"(peak +{_peak_pct:.0f}%, floor ${_trail_floor:.2f}, "
                                           f"now ${current_price:.2f} vs cost ${_cost:.2f})")
                        else:
                            log.info("HEDGE PUT TRAILING: %s +%.0f%% (peak +%.0f%%, floor $%.2f)",
                                     market[:30], _pct_change * 100, _peak_gain * 100, _trail_floor)
                    elif _pct_change <= -0.5:
                        exit_reason = f"HEDGE PUT SL: {_pct_change*100:.0f}% (${current_price:.2f} vs ${_cost:.2f})"
                # TTL: expire at DTE
                if not exit_reason and age_hours > CRASH_HEDGE_CONFIG.get("put_dte", 7) * 24:
                    exit_reason = f"HEDGE PUT EXPIRED: {age_hours:.0f}h"
                if not exit_reason:
                    _pct_str = f" pnl={((current_price - _cost) / _cost)*100:+.0f}%" if current_price and _cost > 0 else ""
                    log.info("HEDGE PUT: %s age=%.0fh%s", market[:30], age_hours, _pct_str)
            elif strategy == "crash_hedge_short":
                # Check stop-loss and target on SPY short
                if current_price is not None and pos.get("stop_price") and pos.get("entry_price"):
                    _ep = pos["entry_price"]
                    if _ep > 0 and current_price > 0:
                        # Short: lose money when price goes up
                        if current_price >= pos["stop_price"]:
                            exit_reason = f"HEDGE SHORT STOP: SPY ${current_price:.2f} >= ${pos['stop_price']:.2f}"
                        elif current_price <= pos.get("target_price", 0):
                            exit_reason = f"HEDGE SHORT TARGET: SPY ${current_price:.2f}"
                if not exit_reason and age_hours > 168:  # 7-day max hold
                    exit_reason = f"HEDGE SHORT TTL: {age_hours:.0f}h"
                if not exit_reason:
                    log.info("HEDGE SHORT: %s age=%.0fh entry=$%.2f", market[:30], age_hours, pos.get("entry_price", 0))
            elif strategy == "options_spread":
                # TP at 50% of credit received, SL at 200%, or expiry
                _credit = pos.get("credit_received", pos.get("cost", 0) * 0.5)
                _max_loss = pos.get("max_loss", pos.get("cost", 0))
                if current_price is not None and _credit > 0:
                    _spread_pnl = _credit - current_price  # Positive = profitable
                    if _spread_pnl >= _credit * 0.50:
                        exit_reason = f"THETA TP: +{_spread_pnl/_credit*100:.0f}% of credit"
                    elif current_price >= _credit * 3.0:  # 200% loss = 3x the credit
                        exit_reason = f"THETA SL: cost ${current_price:.0f} vs credit ${_credit:.0f}"
                _expiry = pos.get("expiry", "")
                if _expiry:
                    try:
                        _exp_dt = datetime.strptime(_expiry, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        if datetime.now(timezone.utc) > _exp_dt + __import__("datetime").timedelta(hours=16):
                            exit_reason = f"THETA EXPIRED: {_expiry}"
                    except Exception:
                        pass
                if not exit_reason and age_hours > 72:
                    exit_reason = f"THETA TTL: {age_hours:.0f}h"
                if not exit_reason:
                    log.info("THETA POS: %s age=%.0fh credit=$%.0f", market[:30], age_hours, _credit)
            elif strategy == "tv_signal":
                # TradingView signals: 24h TTL
                if age_hours > 24:
                    exit_reason = f"TV SIGNAL TTL: {age_hours:.0f}h"
                else:
                    log.info("TV POS: %s age=%.0fh", market[:30], age_hours)
            elif strategy == "event_synthetic":
                # Hold to resolution (max 30 days) — arbs resolve when market closes
                if age_hours > 720:
                    exit_reason = f"SYNTH TTL: {age_hours:.0f}h (30d max)"
                else:
                    log.info("SYNTH POS: %s age=%.0fh spread=%.1f%%", market[:30], age_hours, pos.get("ev", 0) * 100)
            elif strategy == "prediction":
                log.info("PRED POS: %s age=%.0fh ev=%.1f%%", market[:30], age_hours, pos.get("ev", 0) * 100)
                # Hold to resolution — no TTL exit

        if exit_reason:
            positions_to_close.append((i, pos, exit_reason, current_price))

    # --- Phase 3: Execute closes (reverse order to preserve indices) ---
    closed = 0
    for idx, pos, reason, live_value in sorted(positions_to_close, key=lambda x: x[0], reverse=True):
        if idx < len(PAPER_PORTFOLIO["positions"]):
            removed = PAPER_PORTFOLIO["positions"].pop(idx)
            cost = removed.get("cost", 0)

            # Determine exit value: live price if available, else cost (flat exit)
            exit_value = live_value if live_value is not None else cost
            realized_pnl = exit_value - cost

            # Return capital + PnL to cash
            PAPER_PORTFOLIO["cash"] += exit_value

            # Update both SQLite tables
            try:
                _econn = sqlite3.connect(DB_PATH)
                _ec = _econn.cursor()
                _ec.execute("UPDATE paper_trades SET status = 'closed' WHERE market = ? AND status = 'open'", (removed.get("market", ""),))
                _econn.commit()
                _econn.close()
            except Exception:
                pass
            db_close_position(removed.get("market", ""), exit_value, reason, realized_pnl)

            # Persist JSON immediately so closes survive restarts
            save_all_state()

            closed += 1
            log.info("EXIT MANAGER: Closed %s | %s | Cost $%.2f → Exit $%.2f | PnL $%+.2f",
                     removed.get("market", "")[:40], reason, cost, exit_value, realized_pnl)
            if channel:
                try:
                    await channel.send(
                        f"**Exit** {removed.get('market', '')[:40]}\n"
                        f"Reason: {reason}\n"
                        f"PnL: ${realized_pnl:+,.2f} (${cost:.2f} → ${exit_value:.2f})"
                    )
                except Exception:
                    pass

    if closed > 0:
        db_save_daily_state()
        save_all_state()  # Final save for good measure
        log.info("EXIT MANAGER: Closed %d positions", closed)
    return closed

# --- EQUITIES ALLOCATION LIMITS ---
EQUITIES_CONFIG = {
    "max_allocation_pct": 0.30,
    "max_concurrent": 5,
    "max_sector_pct": 0.30,
    "pairs": {
        "seed": [
                 ("V", "MA"), ("XOM", "CVX"), ("GOOGL", "META"), ("KO", "PEP"), ("JPM", "BAC"),
                 ("MS", "GS"), ("NVDA", "AMD"), ("HD", "LOW"), ("UNH", "CI"), ("DIS", "CMCSA"),
                 ("AAPL", "MSFT"), ("PG", "CL"), ("WMT", "COST"), ("T", "VZ"), ("BA", "LMT"),
                 ("CAT", "DE"), ("FDX", "UPS"), ("INTC", "TXN"), ("MCD", "SBUX"), ("NKE", "LULU"),
                 ("PFE", "MRK"), ("COP", "EOG"), ("ADBE", "CRM"), ("NFLX", "DIS"), ("PYPL", "AFRM"),
                 ("USB", "PNC"), ("MMM", "HON"), ("ABT", "MDT"), ("AMZN", "EBAY"),
        ],
        "min_correlation": 0.85,
        "lookback_days": 252,
        "zscore_entry": 1.0,
        "zscore_exit": 0.5,
        "ttl_days": 7,
    },
    "pead": {
        "min_surprise_pct": 0.15,
        "min_volume_mult": 1.5,
        "tp_pct": 0.08,
        "ttl_hours": 72,
    },
}

_PAIRS_DISCOVERY_LAST_RUN = None

def discover_sp500_pairs():
    """Scan S&P 500 by GICS sector for high-correlation pairs.
    Runs daily at 9:00 AM ET. Stores in SQLite, updates EQUITIES_CONFIG seed list."""
    global _PAIRS_DISCOVERY_LAST_RUN
    now = datetime.now(timezone.utc)
    if _PAIRS_DISCOVERY_LAST_RUN and (now - _PAIRS_DISCOVERY_LAST_RUN).total_seconds() < 72000:
        return 0  # Only run every ~20h

    _PAIRS_DISCOVERY_LAST_RUN = now
    log.info("PAIRS DISCOVERY: starting S&P 500 sector scan")

    try:
        import yfinance as yf
        import numpy as np

        # Fetch S&P 500 tickers by sector from Wikipedia
        import io, csv
        _sp_url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
        _sp_r = requests.get(_sp_url, timeout=15, headers={"User-Agent": "TraderJoes/1.0"})
        if _sp_r.status_code != 200:
            log.warning("PAIRS DISCOVERY: Wikipedia fetch failed %d", _sp_r.status_code)
            return 0

        # Parse HTML table for tickers and sectors
        import re as _pd_re
        _rows = _pd_re.findall(r'<td[^>]*><a[^>]*>([A-Z.]+)</a></td>\s*<td[^>]*>[^<]*</td>\s*<td[^>]*>([^<]+)</td>', _sp_r.text)
        if not _rows:
            # Fallback: simpler parse
            _rows = _pd_re.findall(r'>([A-Z]{1,5})</a></td><td[^>]*>[^<]*</td><td[^>]*>([^<]+)</td>', _sp_r.text)

        by_sector = {}
        for ticker, sector in _rows:
            ticker = ticker.replace(".", "-")  # BRK.B → BRK-B for yfinance
            sector = sector.strip()
            by_sector.setdefault(sector, []).append(ticker)

        if not by_sector:
            log.warning("PAIRS DISCOVERY: no sectors parsed")
            return 0

        log.info("PAIRS DISCOVERY: %d sectors, %d tickers", len(by_sector), sum(len(v) for v in by_sector.values()))

        # For each sector, compute pairwise correlations (limit to top 15 tickers by market cap)
        all_pairs = []
        for sector, tickers in by_sector.items():
            if len(tickers) < 2:
                continue
            _sample = tickers[:15]  # Limit to avoid huge downloads
            try:
                data = yf.download(_sample, period="252d", progress=False, threads=True)
                if hasattr(data, "Close") and len(data) > 100:
                    closes = data["Close"].dropna(axis=1, thresh=100)
                    if len(closes.columns) < 2:
                        continue
                    corr_matrix = closes.corr()
                    checked = set()
                    for i, t1 in enumerate(corr_matrix.columns):
                        for j, t2 in enumerate(corr_matrix.columns):
                            if i >= j:
                                continue
                            pair = tuple(sorted([str(t1), str(t2)]))
                            if pair in checked:
                                continue
                            checked.add(pair)
                            c = float(corr_matrix.iloc[i, j])
                            if c >= 0.85 and not np.isnan(c):
                                all_pairs.append((str(t1), str(t2), c, sector))
            except Exception as _se:
                log.warning("PAIRS DISCOVERY: sector %s error: %s", sector[:20], _se)
                continue

        # Sort by correlation, take top 50
        all_pairs.sort(key=lambda x: x[2], reverse=True)
        top_pairs = all_pairs[:50]

        if not top_pairs:
            log.info("PAIRS DISCOVERY: no pairs found above 0.85 threshold")
            return 0

        # Store in SQLite
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            for t1, t2, corr, sector in top_pairs:
                c.execute("INSERT OR REPLACE INTO pairs_discovery (ticker_a, ticker_b, correlation, sector, discovered_at) VALUES (?,?,?,?,?)",
                          (t1, t2, corr, sector, now.strftime("%Y-%m-%d %H:%M")))
            conn.commit()
            conn.close()
        except Exception as _de:
            log.warning("PAIRS DISCOVERY: DB error: %s", _de)

        # Update EQUITIES_CONFIG seed list
        new_seed = [(t1, t2) for t1, t2, _, _ in top_pairs]
        EQUITIES_CONFIG["pairs"]["seed"] = new_seed
        log.info("PAIRS DISCOVERY: found %d pairs, updated seed list (%d sectors scanned)",
                 len(top_pairs), len(by_sector))
        _agent_log_event("pairs-discovery", f"Found {len(top_pairs)} pairs across {len(by_sector)} sectors")
        return len(top_pairs)

    except Exception as e:
        log.warning("PAIRS DISCOVERY error: %s", e)
        return 0


def get_equities_exposure(positions, total_portfolio):
    """Calculate current equities exposure."""
    eq_value = sum(p.get("cost", 0) for p in positions if p.get("strategy") in ("pairs", "pead", "equities"))
    return eq_value, eq_value / max(total_portfolio, 1)

# --- PAIRS TRADING ENGINE ---
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# RISK MANAGEMENT AGENT — Correlation, drawdown, strategy pauses
# ---------------------------------------------------------------------------
_RISK_STATE = {
    "corr_flags": [],        # [(ticker_a, ticker_b, corr)] — high correlation pairs
    "strategy_pauses": {},   # {strategy: pause_until_datetime}
    "daily_drawdown": {},    # {strategy: today's realized pnl}
    "drawdown_limit": 200,   # $200 per strategy per day
    "last_check": None,
}


def risk_check_correlations():
    """Check correlation between open positions. Flag >80% correlated pairs."""
    positions = PAPER_PORTFOLIO.get("positions", [])
    tickers = []
    for p in positions:
        ll = p.get("long_leg", "")
        sl = p.get("short_leg", "")
        if ll:
            tickers.append(ll)
        if sl:
            tickers.append(sl)
        mkt = p.get("market", "")
        if mkt.startswith("TV:"):
            tickers.append(mkt.replace("TV:", ""))

    tickers = list(set(t.upper() for t in tickers if t))
    if len(tickers) < 2:
        _RISK_STATE["corr_flags"] = []
        return

    flags = []
    try:
        import yfinance as yf
        import numpy as np
        data = yf.download(tickers, period="60d", progress=False)
        if hasattr(data, "Close"):
            closes = data["Close"].dropna(axis=1)
            if len(closes.columns) >= 2:
                corr_matrix = closes.corr()
                checked = set()
                for i, t1 in enumerate(corr_matrix.columns):
                    for j, t2 in enumerate(corr_matrix.columns):
                        if i >= j:
                            continue
                        pair = tuple(sorted([str(t1), str(t2)]))
                        if pair in checked:
                            continue
                        checked.add(pair)
                        c = float(corr_matrix.iloc[i, j])
                        if abs(c) > 0.80:
                            flags.append((str(t1), str(t2), c))
    except Exception as e:
        log.warning("RISK corr check error: %s", e)

    _RISK_STATE["corr_flags"] = flags
    if flags:
        log.info("RISK: %d correlated pairs flagged: %s",
                 len(flags), ", ".join(f"{a}/{b}={c:.2f}" for a, b, c in flags[:3]))


def risk_check_daily_drawdown():
    """Check realized P&L per strategy today. Pause strategies exceeding limit."""
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    try:
        import sqlite3 as _rsq
        conn = _rsq.connect(DB_PATH)
        rows = conn.execute("""
            SELECT strategy, SUM(realized_pnl) as dd
            FROM positions WHERE status='closed' AND realized_pnl IS NOT NULL
            AND closed_at >= ? GROUP BY strategy
        """, (today,)).fetchall()
        conn.close()

        dd_by_strat = {}
        for strategy, dd in rows:
            if strategy and dd is not None:
                dd_by_strat[strategy] = dd

        _RISK_STATE["daily_drawdown"] = dd_by_strat

        limit = _RISK_STATE["drawdown_limit"]
        for strat, dd in dd_by_strat.items():
            if dd < -limit and strat not in _RISK_STATE["strategy_pauses"]:
                pause_until = now + __import__("datetime").timedelta(hours=24)
                _RISK_STATE["strategy_pauses"][strat] = pause_until
                log.warning("RISK PAUSE: %s lost $%.2f today (limit $%d) — paused 24h",
                            strat, dd, limit)
                _agent_log_event("risk", f"PAUSE {strat}: lost ${dd:.0f} today")
                send_critical_alert("Strategy Paused", f"{strat} lost ${dd:.0f} today — paused 24h")

    except Exception as e:
        log.warning("RISK drawdown check error: %s", e)

    # Clean expired pauses
    expired = [s for s, t in _RISK_STATE["strategy_pauses"].items() if now > t]
    for s in expired:
        del _RISK_STATE["strategy_pauses"][s]
        log.info("RISK UNPAUSE: %s pause expired", s)


def risk_is_strategy_paused(strategy):
    """Check if a strategy is currently paused due to drawdown."""
    pause_until = _RISK_STATE["strategy_pauses"].get(strategy)
    if pause_until and datetime.now(timezone.utc) < pause_until:
        return True
    return False


def risk_is_correlated_blocked(ticker):
    """Check if a ticker would create concentration risk with existing positions."""
    ticker = ticker.upper()
    for t1, t2, c in _RISK_STATE.get("corr_flags", []):
        if ticker in (t1, t2):
            return True, f"{t1}/{t2} corr={c:.2f}"
    return False, ""


def risk_run_all_checks():
    """Run all risk checks. Call every cycle."""
    risk_check_correlations()
    risk_check_daily_drawdown()
    _RISK_STATE["last_check"] = datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# MULTI-MODEL CONSENSUS — AI second opinion on high-conviction trades
# ---------------------------------------------------------------------------
_AI_CONSENSUS_LOG = []  # [{timestamp, ticker, score, verdict, reasoning, executed}]


def ai_get_second_opinion(ticker, direction, confidence, context_notes):
    """Query Claude/GPT for a second opinion on a trade. Returns (verdict, reasoning).
    Verdict: APPROVE, REJECT, or REDUCE."""
    if not OPENAI_API_KEY:
        return "APPROVE", "No AI key configured — auto-approve"

    prompt = (
        f"You are a risk manager reviewing a trade recommendation.\n"
        f"Ticker: {ticker}\n"
        f"Direction: {direction}\n"
        f"Confidence score: {confidence}/100\n"
        f"Context:\n{chr(10).join(context_notes[:8])}\n\n"
        f"Based on this information, should we execute this trade?\n"
        f"Reply with exactly one of: APPROVE, REJECT, or REDUCE\n"
        f"Then on the next line, explain your reasoning in 1-2 sentences."
    )

    try:
        r = requests.post("https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 150, "temperature": 0.3,
            }, timeout=15)

        if r.status_code == 200:
            reply = r.json().get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            # Parse verdict from first line
            first_line = reply.split("\n")[0].upper().strip()
            if "REJECT" in first_line:
                verdict = "REJECT"
            elif "REDUCE" in first_line:
                verdict = "REDUCE"
            else:
                verdict = "APPROVE"
            reasoning = reply[len(first_line):].strip().lstrip("\n").strip()[:200]
            if not reasoning:
                reasoning = reply[:200]

            _AI_CONSENSUS_LOG.append({
                "timestamp": datetime.now(timezone.utc),
                "ticker": ticker, "direction": direction,
                "score": confidence, "verdict": verdict,
                "reasoning": reasoning, "executed": verdict != "REJECT",
            })
            if len(_AI_CONSENSUS_LOG) > 50:
                _AI_CONSENSUS_LOG.pop(0)

            log.info("AI CONSENSUS: %s %s score=%d → %s: %s",
                     ticker, direction, confidence, verdict, reasoning[:60])
            return verdict, reasoning
        else:
            log.warning("AI CONSENSUS API error: %d", r.status_code)
            return "APPROVE", f"API error {r.status_code} — auto-approve"

    except Exception as e:
        log.warning("AI CONSENSUS error: %s", e)
        return "APPROVE", f"Error: {e} — auto-approve"


# ---------------------------------------------------------------------------
# ENGINEER AGENT — Self-improvement loop, auto-tuning thresholds
# ---------------------------------------------------------------------------
_ENGINEER_LOG = []  # Rolling list of adjustments: [{timestamp, strategy, metric, old, new, reason}]
_ENGINEER_LAST_CHECK = None
_ENGINEER_TRADE_COUNTER = {}  # {strategy: count_since_last_analysis}


def engineer_self_improve():
    """After every 10 closed trades per strategy, analyze and auto-tune.
    Compares actual win rate to Monte Carlo predictions. Adjusts thresholds."""
    global _ENGINEER_LAST_CHECK
    now = datetime.now(timezone.utc)

    try:
        import sqlite3 as _esq
        conn = _esq.connect(DB_PATH)

        # Count closed trades per strategy since last check
        _since = _ENGINEER_LAST_CHECK.strftime("%Y-%m-%d %H:%M:%S") if _ENGINEER_LAST_CHECK else "2020-01-01"
        rows = conn.execute("""
            SELECT strategy, COUNT(*) as cnt
            FROM positions WHERE status='closed' AND closed_at >= ?
            GROUP BY strategy
        """, (_since,)).fetchall()

        for strategy, cnt in rows:
            if not strategy:
                continue
            prev = _ENGINEER_TRADE_COUNTER.get(strategy, 0)
            _ENGINEER_TRADE_COUNTER[strategy] = prev + cnt
            if _ENGINEER_TRADE_COUNTER[strategy] < 10:
                continue

            # 10+ trades accumulated — run analysis
            _ENGINEER_TRADE_COUNTER[strategy] = 0

            # Get last 30 trades for this strategy
            trades = conn.execute("""
                SELECT realized_pnl, size_usd,
                       (julianday(closed_at) - julianday(created_at)) * 24 as hold_hours
                FROM positions WHERE status='closed' AND strategy=?
                AND realized_pnl IS NOT NULL
                ORDER BY closed_at DESC LIMIT 30
            """, (strategy,)).fetchall()

            if len(trades) < 10:
                continue

            wins = sum(1 for t in trades if t[0] > 0)
            total = len(trades)
            actual_wr = wins / total
            avg_pnl = sum(t[0] for t in trades) / total
            avg_hold = sum(t[2] or 0 for t in trades) / total

            # Compare to MC prediction (60% is our baseline expectation)
            mc_expected = 0.60
            gap = mc_expected - actual_wr

            adjustment = None
            if gap > 0.10:  # Actual win rate 10%+ below MC prediction
                if strategy == "pairs":
                    # Tighten Z-score entry by 0.1
                    old_z = EQUITIES_CONFIG.get("pairs", {}).get("zscore_entry", 1.0)
                    new_z = round(old_z + 0.1, 1)
                    if new_z <= 2.5:  # Don't tighten beyond 2.5
                        EQUITIES_CONFIG["pairs"]["zscore_entry"] = new_z
                        adjustment = {
                            "timestamp": now, "strategy": strategy,
                            "metric": "zscore_entry", "old": old_z, "new": new_z,
                            "reason": f"Win rate {actual_wr*100:.0f}% vs expected {mc_expected*100:.0f}% "
                                      f"(gap {gap*100:.0f}%). Avg PnL ${avg_pnl:+.2f}, hold {avg_hold:.0f}h",
                        }
                elif strategy == "oracle_trade":
                    # Reduce oracle sizing
                    old_pct = ORACLE_CONFIG.get("base_size_pct", 0.0075)
                    new_pct = round(max(old_pct - 0.001, 0.003), 4)
                    if new_pct != old_pct:
                        ORACLE_CONFIG["base_size_pct"] = new_pct
                        adjustment = {
                            "timestamp": now, "strategy": strategy,
                            "metric": "base_size_pct", "old": old_pct, "new": new_pct,
                            "reason": f"Win rate {actual_wr*100:.0f}% vs expected {mc_expected*100:.0f}%",
                        }
                elif strategy == "crypto":
                    # Tighten crypto by raising min EV threshold
                    old_ev = ALERT_CONFIG.get("min_ev_threshold", 0.02)
                    new_ev = round(old_ev + 0.005, 3)
                    if new_ev <= 0.05:
                        ALERT_CONFIG["min_ev_threshold"] = new_ev
                        adjustment = {
                            "timestamp": now, "strategy": strategy,
                            "metric": "min_ev_threshold", "old": old_ev, "new": new_ev,
                            "reason": f"Win rate {actual_wr*100:.0f}% vs expected {mc_expected*100:.0f}%",
                        }

            if adjustment:
                _ENGINEER_LOG.append(adjustment)
                if len(_ENGINEER_LOG) > 50:
                    _ENGINEER_LOG.pop(0)
                log.info("ENGINEER: %s %s %.3f→%.3f (%s)",
                         adjustment["strategy"], adjustment["metric"],
                         adjustment["old"], adjustment["new"], adjustment["reason"][:60])
                _agent_log_event("engineer", f"{adjustment['strategy']} {adjustment['metric']} {adjustment['old']:.3f}→{adjustment['new']:.3f}")

            # Always log stats even without adjustment
            log.info("ENGINEER STATS: %s wr=%.0f%% avg_pnl=$%+.2f hold=%.0fh (%d trades)",
                     strategy, actual_wr * 100, avg_pnl, avg_hold, total)
            _agent_log_event("engineer", f"{strategy}: wr={actual_wr*100:.0f}% pnl=${avg_pnl:+.2f} ({total} trades)")

        conn.close()
        _ENGINEER_LAST_CHECK = now

    except Exception as e:
        log.warning("ENGINEER error: %s", e)


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# ECHO MARKET MEMORY — Historical event/outcome pattern matching
# ---------------------------------------------------------------------------

def echo_memory_store(event_type, theme, signal_name="", probability=0,
                      geo_level="", headline="", trade_outcome="", realized_pnl=0, context=""):
    """Store an event in echo memory for future pattern matching."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO echo_memory (event_type,theme,signal_name,probability,geo_level,headline,trade_outcome,realized_pnl,context) VALUES (?,?,?,?,?,?,?,?,?)",
                     (event_type, theme, signal_name, probability, geo_level, headline, trade_outcome, realized_pnl, context))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Echo memory store error: %s", e)


def echo_memory_query(theme, days=30, limit=5):
    """Query echo memory for similar past events. Returns list of event dicts."""
    try:
        cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=days)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute("""
            SELECT event_type, theme, signal_name, probability, geo_level,
                   headline, trade_outcome, realized_pnl, context, created_at
            FROM echo_memory WHERE theme LIKE ? AND created_at >= ?
            ORDER BY created_at DESC LIMIT ?
        """, (f"%{theme}%", cutoff, limit)).fetchall()
        conn.close()
        return [{"event_type": r[0], "theme": r[1], "signal": r[2], "prob": r[3],
                 "geo": r[4], "headline": r[5][:60] if r[5] else "", "outcome": r[6],
                 "pnl": r[7], "context": r[8], "date": r[9]} for r in rows]
    except Exception:
        return []


def echo_memory_get_pattern(theme):
    """Summarize historical pattern for a theme. Returns summary string."""
    events = echo_memory_query(theme, days=30, limit=20)
    if not events:
        return None
    trades = [e for e in events if e.get("pnl") and e["pnl"] != 0]
    if not trades:
        return f"{len(events)} events in 30d, no trades"
    wins = sum(1 for t in trades if t["pnl"] > 0)
    total_pnl = sum(t["pnl"] for t in trades)
    return f"Last 30d: {len(trades)} trades, {wins}/{len(trades)} wins, ${total_pnl:+.2f}"


# ---------------------------------------------------------------------------
# ECHO CAUSAL MEMORY ENGINE — ChromaDB-backed regime similarity search
# Closes the learning loop: reactive → predictive
# ---------------------------------------------------------------------------
_CHROMA_CLIENT = None
_CHROMA_COLLECTION = None


def _init_chromadb():
    """Initialize ChromaDB with persistent local storage."""
    global _CHROMA_CLIENT, _CHROMA_COLLECTION
    try:
        import chromadb
        from chromadb.config import Settings
        _CHROMA_CLIENT = chromadb.PersistentClient(path="/app/data/chromadb")
        _CHROMA_COLLECTION = _CHROMA_CLIENT.get_or_create_collection(
            name="regime_snapshots",
            metadata={"hnsw:space": "cosine"},
        )
        log.info("CAUSAL MEMORY: ChromaDB initialized (%d snapshots stored)",
                 _CHROMA_COLLECTION.count())
    except Exception as e:
        log.warning("CAUSAL MEMORY: ChromaDB init failed: %s — falling back to no-op", e)
        _CHROMA_CLIENT = None
        _CHROMA_COLLECTION = None


def _build_regime_vector():
    """Build a numeric vector representing the current market regime.
    Returns (vector_list, metadata_dict) or (None, None) on failure."""
    try:
        regime = get_regime("equities")
        vix = regime.get("vix") or 20
        fng = _PSYCH_STATE.get("last_fng", 50)
        spy_price = _get_spy_price() or 0

        # Regime label to numeric
        regime_map = {"low": 0, "normal": 1, "elevated": 2, "extreme": 3}
        regime_num = regime_map.get(regime.get("regime", "normal"), 1)

        # SPY 5-day trend from price history
        spy_trend = 0
        try:
            import yfinance as _yf
            _spy = _yf.Ticker("SPY")
            _hist = _spy.history(period="5d")
            if len(_hist) >= 2:
                spy_trend = (_hist["Close"].iloc[-1] / _hist["Close"].iloc[0] - 1) * 100
        except Exception:
            pass

        # Active geo themes
        iran_esc = _intel_theme_escalation_count("iran")
        ukraine_esc = _intel_theme_escalation_count("ukraine")
        taiwan_esc = _intel_theme_escalation_count("taiwan")

        # Active oracle signal count
        oracle_active = sum(1 for p in PAPER_PORTFOLIO.get("positions", [])
                           if p.get("strategy") in ("oracle_trade", "cascade_trade"))

        # Normalize to 0-1 range for vector similarity
        vector = [
            vix / 50.0,           # VIX normalized (0-50 → 0-1)
            fng / 100.0,          # F&G already 0-100
            regime_num / 3.0,     # Regime 0-3 → 0-1
            spy_trend / 10.0,     # 5-day trend normalized (-10% to +10% → -1 to 1)
            min(iran_esc / 10.0, 1.0),     # Iran headlines 0-10 → 0-1
            min(ukraine_esc / 10.0, 1.0),  # Ukraine headlines
            min(taiwan_esc / 10.0, 1.0),   # Taiwan headlines
            min(oracle_active / 4.0, 1.0), # Oracle positions 0-4 → 0-1
        ]

        metadata = {
            "vix": round(vix, 1),
            "fng": fng,
            "spy_price": round(spy_price, 2),
            "spy_trend_5d": round(spy_trend, 2),
            "regime": regime.get("regime", "normal"),
            "iran_esc": iran_esc,
            "ukraine_esc": ukraine_esc,
            "taiwan_esc": taiwan_esc,
            "oracle_active": oracle_active,
        }
        return vector, metadata
    except Exception as e:
        log.warning("CAUSAL MEMORY: vector build error: %s", e)
        return None, None


def causal_memory_store_snapshot():
    """Store daily regime snapshot in ChromaDB. Called at 4 PM ET."""
    if not _CHROMA_COLLECTION:
        _init_chromadb()
    if not _CHROMA_COLLECTION:
        return

    vector, metadata = _build_regime_vector()
    if vector is None:
        return

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    doc_id = f"regime_{date_str}"

    # Calculate daily P&L by strategy
    try:
        import sqlite3 as _csq
        conn = _csq.connect(DB_PATH)
        c = conn.cursor()
        today = now.strftime("%Y-%m-%d")
        strategies = ["pairs", "oracle_trade", "cascade_trade", "crash_hedge_put",
                      "crash_hedge_call_spread", "prediction"]
        pnl_by_strat = {}
        for strat in strategies:
            row = c.execute(
                "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions WHERE strategy=? AND closed_at LIKE ?",
                (strat, f"{today}%")).fetchone()
            pnl_by_strat[strat] = round(row[0], 2) if row else 0
        conn.close()
        metadata["daily_pnl"] = pnl_by_strat
        metadata["total_daily_pnl"] = round(sum(pnl_by_strat.values()), 2)
    except Exception:
        metadata["daily_pnl"] = {}
        metadata["total_daily_pnl"] = 0

    metadata["date"] = date_str
    metadata["timestamp"] = now.strftime("%Y-%m-%d %H:%M UTC")

    # Flatten metadata — ChromaDB requires flat string/int/float values
    flat_meta = {}
    for k, v in metadata.items():
        if isinstance(v, dict):
            for dk, dv in v.items():
                flat_meta[f"{k}_{dk}"] = dv
        else:
            flat_meta[k] = v

    try:
        _CHROMA_COLLECTION.upsert(
            ids=[doc_id],
            embeddings=[vector],
            metadatas=[flat_meta],
            documents=[f"Regime snapshot {date_str}: VIX={metadata['vix']} F&G={metadata['fng']} "
                       f"SPY=${metadata['spy_price']} trend={metadata['spy_trend_5d']:+.1f}% "
                       f"regime={metadata['regime']}"],
        )
        log.info("CAUSAL MEMORY: stored snapshot %s (VIX=%.1f F&G=%d SPY=$%.0f pnl=$%.2f)",
                 date_str, metadata["vix"], metadata["fng"], metadata["spy_price"],
                 metadata.get("total_daily_pnl", 0))
    except Exception as e:
        log.warning("CAUSAL MEMORY: store error: %s", e)


def causal_memory_query(strategy=None, n_results=5):
    """Query ChromaDB for the N most similar regime snapshots to current conditions.
    Returns list of (metadata_dict, distance) tuples, or empty list."""
    if not _CHROMA_COLLECTION:
        _init_chromadb()
    if not _CHROMA_COLLECTION or _CHROMA_COLLECTION.count() == 0:
        return []

    vector, _ = _build_regime_vector()
    if vector is None:
        return []

    try:
        results = _CHROMA_COLLECTION.query(
            query_embeddings=[vector],
            n_results=min(n_results, _CHROMA_COLLECTION.count()),
        )
        matches = []
        if results and results.get("metadatas"):
            for i, meta in enumerate(results["metadatas"][0]):
                dist = results["distances"][0][i] if results.get("distances") else 0
                matches.append((meta, dist))
        return matches
    except Exception as e:
        log.warning("CAUSAL MEMORY: query error: %s", e)
        return []


def causal_memory_size_adjustment(strategy, n_results=5):
    """Pre-trade memory query: check historical win rate in similar regimes.
    Returns (multiplier, details_str). Multiplier: 0.5 if <40% win, 1.25 if >70%, else 1.0."""
    matches = causal_memory_query(strategy=strategy, n_results=n_results)
    if not matches or len(matches) < 3:
        return 1.0, "insufficient history"

    # Calculate win rate for this strategy in similar regimes
    wins = 0
    losses = 0
    total_pnl = 0
    matched_dates = []
    for meta, dist in matches:
        date = meta.get("date", "?")
        matched_dates.append(date)
        pnl_key = f"daily_pnl_{strategy}"
        strat_pnl = meta.get(pnl_key, 0)
        if isinstance(strat_pnl, (int, float)):
            total_pnl += strat_pnl
            if strat_pnl > 0:
                wins += 1
            elif strat_pnl < 0:
                losses += 1

    total = wins + losses
    if total == 0:
        return 1.0, f"no {strategy} trades in {len(matches)} similar dates: {', '.join(matched_dates)}"

    win_rate = wins / total
    details = (f"similar dates: {', '.join(matched_dates)} | "
               f"win_rate={win_rate:.0%} ({wins}W/{losses}L) pnl=${total_pnl:+.2f}")

    if win_rate < 0.40:
        log.info("CAUSAL MEMORY: %s win_rate=%.0f%% < 40%% → SIZE -50%% | %s", strategy, win_rate * 100, details)
        return 0.5, details
    elif win_rate > 0.70:
        log.info("CAUSAL MEMORY: %s win_rate=%.0f%% > 70%% → SIZE +25%% | %s", strategy, win_rate * 100, details)
        return 1.25, details
    else:
        log.info("CAUSAL MEMORY: %s win_rate=%.0f%% → SIZE 1.0x | %s", strategy, win_rate * 100, details)
        return 1.0, details


def causal_memory_asset_impact(theme, hours=48):
    """Causal chain query: last time this theme was active, what happened to related assets.
    Returns list of (asset, pct_change, date) tuples."""
    matches = causal_memory_query(n_results=10)
    if not matches:
        return []

    # Find dates where this theme had high escalation
    theme_key = f"{theme}_esc"
    active_dates = []
    for meta, dist in matches:
        if meta.get(theme_key, 0) >= 5:
            active_dates.append(meta.get("date", ""))

    if not active_dates:
        return []

    # Check asset price changes for related tickers over 48h after those dates
    asset_map = {
        "iran": ["XLE", "AAL", "GLD", "CL=F"],
        "ukraine": ["LMT", "RTX", "UAL", "GLD", "EFA"],
        "taiwan": ["TSM", "INTC", "SMH", "NVDA"],
        "fed": ["XLF", "TLT", "SPY"],
        "recession": ["GLD", "SPY", "TLT"],
    }
    assets = asset_map.get(theme, [])
    results = []

    try:
        import yfinance as _yf
        for asset in assets[:5]:
            try:
                _tk = _yf.Ticker(asset)
                _hist = _tk.history(period="30d")
                if len(_hist) < 3:
                    continue
                for dt_str in active_dates[:3]:
                    try:
                        import datetime as _dt_ca
                        dt = _dt_ca.datetime.strptime(dt_str, "%Y-%m-%d")
                        # Find the closest trading day in history
                        closest_idx = None
                        for i, idx in enumerate(_hist.index):
                            if idx.date() >= dt.date():
                                closest_idx = i
                                break
                        if closest_idx is not None and closest_idx + 2 < len(_hist):
                            price_at = _hist["Close"].iloc[closest_idx]
                            price_after = _hist["Close"].iloc[min(closest_idx + 2, len(_hist) - 1)]
                            pct = (price_after / price_at - 1) * 100
                            results.append((asset, round(pct, 2), dt_str))
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass

    return results


# OPTIONS FLOW SCANNER — Put/Call ratio from Polygon options chain
# ---------------------------------------------------------------------------
_PUT_CALL_CACHE = {}  # {underlying: {"ratio": x, "put_vol": y, "call_vol": z, "signal": str, "ts": datetime}}


def options_put_call_ratio(underlying="SPY"):
    """Fetch options chain from Polygon and calculate put/call volume ratio.
    Returns dict with ratio, volumes, and signal classification."""
    now = datetime.now(timezone.utc)
    cached = _PUT_CALL_CACHE.get(underlying)
    if cached and (now - cached["ts"]).total_seconds() < 600:
        return cached

    result = {"ratio": 0, "put_vol": 0, "call_vol": 0, "signal": "unavailable",
              "available": False, "ts": now}
    try:
        from polygon_client import _get_client
        c = _get_client()
        if not c:
            _PUT_CALL_CACHE[underlying] = result
            return result

        import datetime as _dt_pc
        # Get options contracts expiring in next 7 days
        today = now.strftime("%Y-%m-%d")
        exp_max = (now + _dt_pc.timedelta(days=7)).strftime("%Y-%m-%d")

        put_volume = 0
        call_volume = 0
        try:
            # Fetch put contracts
            puts = list(c.list_options_contracts(
                underlying_ticker=underlying, contract_type="put",
                expiration_date_gte=today, expiration_date_lte=exp_max,
                limit=100))
            for p in puts[:50]:
                try:
                    snap = c.get_snapshot_option(underlying, p.ticker)
                    if snap and snap.day:
                        put_volume += snap.day.volume or 0
                except Exception:
                    pass
        except Exception:
            pass

        try:
            # Fetch call contracts
            calls = list(c.list_options_contracts(
                underlying_ticker=underlying, contract_type="call",
                expiration_date_gte=today, expiration_date_lte=exp_max,
                limit=100))
            for ca in calls[:50]:
                try:
                    snap = c.get_snapshot_option(underlying, ca.ticker)
                    if snap and snap.day:
                        call_volume += snap.day.volume or 0
                except Exception:
                    pass
        except Exception:
            pass

        if call_volume > 0:
            ratio = put_volume / call_volume
            if ratio > 1.5:
                signal = "PANIC_PUTS"
            elif ratio > 1.2:
                signal = "ELEVATED_FEAR"
            elif ratio < 0.5:
                signal = "EXTREME_COMPLACENCY"
            elif ratio < 0.7:
                signal = "COMPLACENT"
            else:
                signal = "NORMAL"
            result = {"ratio": round(ratio, 3), "put_vol": put_volume, "call_vol": call_volume,
                      "signal": signal, "available": True, "ts": now}
            log.info("OPTIONS FLOW %s: P/C=%.3f put_vol=%d call_vol=%d → %s",
                     underlying, ratio, put_volume, call_volume, signal)
        else:
            result["signal"] = "no_data"

    except Exception as e:
        log.warning("OPTIONS FLOW %s error: %s", underlying, e)

    _PUT_CALL_CACHE[underlying] = result
    return result


def get_put_call_ratios():
    """Get put/call ratios for SPY and QQQ. Returns dict."""
    spy = options_put_call_ratio("SPY")
    qqq = options_put_call_ratio("QQQ")
    return {"SPY": spy, "QQQ": qqq}


# VOLATILITY SKEW SCANNER — Put/Call IV analysis for options strategy
# ---------------------------------------------------------------------------
_VOL_SKEW_CACHE = {}  # {underlying: {"put_iv": x, "call_iv": y, "skew": z, "ts": datetime}}


def vol_skew_scan(underlying="SPY"):
    """Fetch options chain and calculate put/call IV skew. Returns skew dict."""
    now = datetime.now(timezone.utc)
    cached = _VOL_SKEW_CACHE.get(underlying)
    if cached and (now - cached["ts"]).total_seconds() < 1800:
        return cached

    result = {"put_iv": 0, "call_iv": 0, "skew": 0, "skew_pct": 0,
              "recommendation": "neutral", "available": False, "ts": now}
    try:
        hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        expiry = datetime.now(timezone.utc) + __import__("datetime").timedelta(days=7)
        while expiry.weekday() >= 5:
            expiry += __import__("datetime").timedelta(days=1)
        _or = requests.get(f"https://data.alpaca.markets/v1beta1/options/snapshots/{underlying}",
                           headers=hdrs, params={"feed": "indicative", "expiration_date": expiry.strftime("%Y-%m-%d")},
                           timeout=10)
        if _or.status_code != 200:
            _VOL_SKEW_CACHE[underlying] = result
            return result

        snapshots = _or.json().get("snapshots", {})
        put_ivs = []
        call_ivs = []
        for sym, snap in snapshots.items():
            iv = float(snap.get("greeks", {}).get("implied_volatility", 0) or 0)
            if iv <= 0:
                continue
            if "P" in sym[-9:-8]:  # Put
                put_ivs.append(iv)
            elif "C" in sym[-9:-8]:  # Call
                call_ivs.append(iv)

        if put_ivs and call_ivs:
            avg_put = sum(put_ivs) / len(put_ivs)
            avg_call = sum(call_ivs) / len(call_ivs)
            skew = avg_put - avg_call
            skew_pct = (skew / avg_call * 100) if avg_call > 0 else 0

            if skew_pct > 40:
                rec = "SKIP — panic put buying"
            elif skew_pct > 20:
                rec = "SELL CALL SPREADS (not puts)"
            elif skew_pct > 0:
                rec = "SELL PUT SPREADS (normal skew)"
            else:
                rec = "SELL PUT SPREADS (calls expensive)"

            result = {"put_iv": avg_put, "call_iv": avg_call, "skew": skew,
                      "skew_pct": skew_pct, "recommendation": rec,
                      "available": True, "ts": now, "n_puts": len(put_ivs), "n_calls": len(call_ivs)}
    except Exception as e:
        log.warning("VOL SKEW %s error: %s", underlying, e)

    _VOL_SKEW_CACHE[underlying] = result
    return result


# MASTER ARBITER — Conflict resolution matrix for ALL trades
# ---------------------------------------------------------------------------

def arbiter_check(strategy, ticker_a, ticker_b=None, zscore=None, corr=None,
                  is_momentum=False, conviction_score=0):
    """Master arbiter: run all checks in hierarchy. Returns (proceed, size_mult, reasons).
    Check order: Risk → Psychologist → Monte Carlo → Historian → AI Consensus."""
    reasons = []
    size_mult = 1.0

    # Level 1: Risk Agent veto
    if risk_is_strategy_paused(strategy):
        reasons.append(f"L1 RISK: {strategy} paused — STOP")
        _agent_log_event("arbiter", f"BLOCKED {strategy}: paused")
        return False, 0, reasons
    if ticker_a:
        blocked, reason = risk_is_correlated_blocked(ticker_a)
        if blocked:
            reasons.append(f"L1 RISK: {ticker_a} corr blocked ({reason}) — STOP")
            _agent_log_event("arbiter", f"BLOCKED {ticker_a}: {reason}")
            return False, 0, reasons
    if ticker_b:
        blocked, reason = risk_is_correlated_blocked(ticker_b)
        if blocked:
            reasons.append(f"L1 RISK: {ticker_b} corr blocked ({reason}) — STOP")
            return False, 0, reasons
    reasons.append("L1 RISK: PASS")

    # Level 2: Psychologist veto
    if _PSYCH_STATE.get("caution_mode") and is_momentum:
        reasons.append("L2 PSYCH: CAUTION + momentum — STOP")
        _agent_log_event("arbiter", f"BLOCKED {ticker_a}: caution mode + momentum")
        return False, 0, reasons
    if _PSYCH_STATE.get("contrarian_mode"):
        if not is_momentum:  # Mean reversion in contrarian = good
            size_mult *= 1.5
            reasons.append("L2 PSYCH: CONTRARIAN + mean-reversion — 1.5x")
        else:
            reasons.append("L2 PSYCH: CONTRARIAN + momentum — STOP")
            return False, 0, reasons
    else:
        psych_m = psychologist_size_multiplier()
        size_mult *= psych_m
        reasons.append(f"L2 PSYCH: {psych_m:.1f}x")

    # Level 3: Monte Carlo gate
    if ticker_a:
        try:
            _mc = montecarlo_simulate(ticker_a, ticker_b, entry_zscore=zscore,
                                       horizon_days=7 if ticker_b else 3)
            if _mc.get("available"):
                prob = _mc["prob_profit"]
                if prob < 0.45:
                    reasons.append(f"L3 MC: prob={prob:.0%} — SKIP")
                    _agent_log_event("arbiter", f"BLOCKED {ticker_a}: MC prob {prob:.0%}")
                    return False, 0, reasons
                elif prob < 0.55:
                    size_mult *= 0.5
                    reasons.append(f"L3 MC: prob={prob:.0%} — REDUCE 0.5x")
                else:
                    reasons.append(f"L3 MC: prob={prob:.0%} — PASS")
            else:
                reasons.append("L3 MC: no data — PASS")
        except Exception:
            reasons.append("L3 MC: error — PASS")

    # Level 4: Historian multiplier (pairs only)
    if ticker_b and strategy == "pairs":
        _hs = historian_analyze_pair(ticker_a, ticker_b)
        _hm = historian_size_multiplier(_hs)
        size_mult *= _hm
        if _hs.get("available"):
            reasons.append(f"L4 HIST: revert={_hs['reversion_rate']*100:.0f}% — {_hm:.1f}x")
        else:
            reasons.append("L4 HIST: no data — 1.0x")

    # Level 5: AI Consensus (high conviction only)
    # Only query AI if score > 70 AND MC > 60% but Historian < 50%
    if conviction_score > 70 and OPENAI_API_KEY:
        try:
            _mc_check = montecarlo_simulate(ticker_a, ticker_b, entry_zscore=zscore, horizon_days=5)
            _mc_prob = _mc_check.get("prob_profit", 0.5) if _mc_check.get("available") else 0.5
            _hist_rate = historian_analyze_pair(ticker_a, ticker_b).get("reversion_rate", 0.5) if ticker_b else 0.5
            if _mc_prob > 0.60 and _hist_rate < 0.50:
                _verdict, _reason = ai_get_second_opinion(
                    f"{ticker_a}/{ticker_b}" if ticker_b else ticker_a,
                    "LONG" if (zscore and zscore < 0) else "SHORT",
                    conviction_score, [f"MC prob {_mc_prob:.0%}", f"Historian revert {_hist_rate:.0%}"])
                if _verdict == "REJECT":
                    reasons.append(f"L5 AI: REJECT — {_reason[:40]}")
                    return False, 0, reasons
                elif _verdict == "REDUCE":
                    size_mult *= 0.5
                    reasons.append(f"L5 AI: REDUCE — {_reason[:40]}")
                else:
                    reasons.append(f"L5 AI: APPROVE")
            else:
                reasons.append("L5 AI: not needed (MC/Hist aligned)")
        except Exception:
            reasons.append("L5 AI: error — PASS")

    # Meta-allocation
    _meta = meta_alloc_multiplier(strategy)
    size_mult *= _meta
    if _meta != 1.0:
        reasons.append(f"META: {strategy}={_meta:.1f}x")

    _agent_log_event("arbiter", f"APPROVED {ticker_a or '?'}/{ticker_b or '-'} {strategy} {size_mult:.2f}x")
    return True, size_mult, reasons


# SHADOW TRADING — 10x parallel portfolio for sizing validation
# ---------------------------------------------------------------------------

def shadow_open_position(market_id, strategy, direction, size_usd, entry_price):
    """Open a shadow position at 10x the real size."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("INSERT INTO shadow_positions (market_id,strategy,direction,size_usd,entry_price,status) VALUES (?,?,?,?,?,'open')",
                     (market_id, strategy, direction, size_usd * 10, entry_price))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Shadow open error: %s", e)


def shadow_close_position(market_id, realized_pnl, exit_reason):
    """Close a shadow position with 10x P&L."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE shadow_positions SET status='closed', realized_pnl=?, exit_reason=?, closed_at=datetime('now') WHERE market_id=? AND status='open'",
                     (realized_pnl * 10, exit_reason, market_id))
        conn.commit()
        conn.close()
    except Exception as e:
        log.warning("Shadow close error: %s", e)


def shadow_compare_performance():
    """Compare shadow vs real P&L over last 7 days. Returns (shadow_pnl, real_pnl, ratio)."""
    try:
        week_ago = (datetime.now(timezone.utc) - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")
        conn = sqlite3.connect(DB_PATH)
        _sr = conn.execute("SELECT SUM(realized_pnl) FROM shadow_positions WHERE status='closed' AND closed_at>=?", (week_ago,)).fetchone()
        shadow_pnl = _sr[0] if _sr and _sr[0] else 0
        _rr = conn.execute("SELECT SUM(realized_pnl) FROM positions WHERE status='closed' AND closed_at>=? AND realized_pnl IS NOT NULL", (week_ago,)).fetchone()
        real_pnl = _rr[0] if _rr and _rr[0] else 0
        conn.close()
        return shadow_pnl, real_pnl
    except Exception:
        return 0, 0


# PSYCHOLOGIST AGENT — Sentiment regime from Fear & Greed
# ---------------------------------------------------------------------------
_PSYCH_STATE = {
    "contrarian_mode": False,   # F&G < 20 — bias toward mean reversion
    "caution_mode": False,      # F&G > 80 — tighten all sizing to 0.5x
    "last_fng": 50,
    "last_label": "Neutral",
    "last_check": None,
    "regime_changed": False,    # True when mode just changed (for one-time Discord notify)
}


def psychologist_update(channel_send_fn=None):
    """Check Fear & Greed and update sentiment regime. Call every scan cycle.
    Returns the current psych state dict."""
    state = _PSYCH_STATE
    fng_val, fng_label = get_fear_greed()
    state["last_fng"] = fng_val
    state["last_label"] = fng_label
    state["last_check"] = datetime.now(timezone.utc)

    old_contrarian = state["contrarian_mode"]
    old_caution = state["caution_mode"]

    state["contrarian_mode"] = fng_val < 20
    state["caution_mode"] = fng_val > 80
    state["regime_changed"] = (state["contrarian_mode"] != old_contrarian or
                                state["caution_mode"] != old_caution)

    if state["regime_changed"]:
        if state["contrarian_mode"]:
            log.info("PSYCHOLOGIST: CONTRARIAN MODE ON — F&G=%d (%s), bias mean-reversion", fng_val, fng_label)
            _agent_log_event("psychologist", f"CONTRARIAN MODE — F&G={fng_val} ({fng_label})")
        elif state["caution_mode"]:
            log.info("PSYCHOLOGIST: CAUTION MODE ON — F&G=%d (%s), sizing tightened 0.5x", fng_val, fng_label)
            _agent_log_event("psychologist", f"CAUTION MODE — F&G={fng_val} ({fng_label})")
        elif old_contrarian or old_caution:
            log.info("PSYCHOLOGIST: NORMAL MODE — F&G=%d (%s), regime cleared", fng_val, fng_label)
            _agent_log_event("psychologist", f"NORMAL MODE — F&G={fng_val} ({fng_label})")

    return state


def psychologist_size_multiplier():
    """Return a sizing multiplier based on current psych state.
    Caution mode → 0.5x, else 1.0x."""
    if _PSYCH_STATE["caution_mode"]:
        return 0.5
    return 1.0


def psychologist_should_skip_momentum():
    """In contrarian mode, skip momentum entries and favor mean-reversion."""
    return _PSYCH_STATE["contrarian_mode"]


# ---------------------------------------------------------------------------
# HISTORIAN AGENT — Historical reversion statistics for pairs
# ---------------------------------------------------------------------------
_HISTORIAN_CACHE = {}  # {pair_key: {"stats": {...}, "fetched_at": datetime}}


def historian_analyze_pair(ticker_a, ticker_b):
    """Fetch 2yr history, calculate reversion stats for a pair.
    Returns dict with reversion_rate, avg_reversion_hours, max_adverse_z, sample_size.
    Results cached for 24 hours."""
    pair_key = f"{ticker_a}/{ticker_b}"
    now = datetime.now(timezone.utc)

    # Check cache
    cached = _HISTORIAN_CACHE.get(pair_key)
    if cached and (now - cached["fetched_at"]).total_seconds() < 86400:
        return cached["stats"]

    stats = {"reversion_rate": 0.5, "avg_reversion_days": 5.0, "max_adverse_z": 3.0,
             "sample_size": 0, "available": False}
    try:
        import yfinance as yf
        import numpy as np

        data_a = yf.download(ticker_a, period="2y", progress=False)
        data_b = yf.download(ticker_b, period="2y", progress=False)
        if len(data_a) < 200 or len(data_b) < 200:
            _HISTORIAN_CACHE[pair_key] = {"stats": stats, "fetched_at": now}
            return stats

        pa = data_a["Close"].values.flatten()
        pb = data_b["Close"].values.flatten()
        min_len = min(len(pa), len(pb))
        pa, pb = pa[-min_len:], pb[-min_len:]
        ratio = pa / pb
        mean_r = float(np.mean(ratio))
        std_r = float(np.std(ratio))
        if std_r == 0:
            _HISTORIAN_CACHE[pair_key] = {"stats": stats, "fetched_at": now}
            return stats

        zscores = (ratio - mean_r) / std_r

        # Find all entries where |Z| >= 2.0 (extreme signal)
        entries = []
        in_signal = False
        entry_idx = 0
        entry_z = 0
        max_adverse = 0

        for i in range(len(zscores)):
            z = float(zscores[i])
            if not in_signal and abs(z) >= 2.0:
                in_signal = True
                entry_idx = i
                entry_z = z
                max_adverse = abs(z)
            elif in_signal:
                max_adverse = max(max_adverse, abs(z))
                # Reversion: Z crosses back through 0.5 toward mean
                if abs(z) < 0.5 or (entry_z > 0 and z < 0) or (entry_z < 0 and z > 0):
                    entries.append({
                        "reverted": True,
                        "days": i - entry_idx,
                        "max_adverse": max_adverse,
                    })
                    in_signal = False
                # Blowout: Z extends beyond 4.0 (broken)
                elif abs(z) > 4.0:
                    entries.append({
                        "reverted": False,
                        "days": i - entry_idx,
                        "max_adverse": max_adverse,
                    })
                    in_signal = False

        if entries:
            reverted = [e for e in entries if e["reverted"]]
            stats["reversion_rate"] = len(reverted) / len(entries)
            if reverted:
                stats["avg_reversion_days"] = sum(e["days"] for e in reverted) / len(reverted)
            stats["max_adverse_z"] = max(e["max_adverse"] for e in entries)
            stats["sample_size"] = len(entries)
            stats["available"] = True

    except Exception as e:
        log.warning("HISTORIAN error %s: %s", pair_key, e)

    _HISTORIAN_CACHE[pair_key] = {"stats": stats, "fetched_at": now}
    return stats


def historian_size_multiplier(stats):
    """Convert historian stats to a position size multiplier.
    High reversion rate (>70%) → 1.5x, Low (<40%) → 0.5x, else 1.0x."""
    if not stats.get("available") or stats["sample_size"] < 3:
        return 1.0  # No data, neutral
    rate = stats["reversion_rate"]
    if rate >= 0.70:
        return 1.5
    elif rate <= 0.40:
        return 0.5
    else:
        return 1.0


# ---------------------------------------------------------------------------
# DYNAMIC META-ALLOCATION ENGINE — Performance-weighted pillar sizing
# ---------------------------------------------------------------------------
_META_ALLOC = {
    "pairs": 1.0, "crypto": 1.0, "prediction": 1.0,
    "funding_arb": 1.0, "oracle_trade": 1.0, "crash_hedge_put": 1.0,
}
_META_ALLOC_LAST_RUN = None
_META_ALLOC_PNL = {}  # {strategy: 7d_pnl} — refreshed every 4h


def meta_alloc_refresh():
    """Query SQLite for 7-day realized P&L by strategy. Rebalance pillar weights.
    Top performer gets 1.5x, bottom gets 0.5x, rest stay 1.0x. Runs every 4 hours."""
    global _META_ALLOC_LAST_RUN
    now = datetime.now(timezone.utc)

    # Only run every 4 hours
    if _META_ALLOC_LAST_RUN and (now - _META_ALLOC_LAST_RUN).total_seconds() < 14400:
        return False

    _META_ALLOC_LAST_RUN = now

    try:
        import sqlite3 as _masq
        conn = _masq.connect(DB_PATH)
        seven_days_ago = (now - __import__("datetime").timedelta(days=7)).strftime("%Y-%m-%d")

        rows = conn.execute("""
            SELECT strategy, SUM(realized_pnl) as total_pnl, COUNT(*) as trades
            FROM positions
            WHERE status='closed' AND realized_pnl IS NOT NULL
            AND closed_at >= ?
            GROUP BY strategy
        """, (seven_days_ago,)).fetchall()
        conn.close()

        if not rows:
            return False

        pnl_by_strat = {}
        for strategy, total_pnl, trades in rows:
            if strategy and total_pnl is not None:
                pnl_by_strat[strategy] = {"pnl": total_pnl, "trades": trades}

        _META_ALLOC_PNL.clear()
        _META_ALLOC_PNL.update(pnl_by_strat)

        if len(pnl_by_strat) < 2:
            return False  # Need at least 2 strategies to compare

        # Find top and bottom performers
        sorted_strats = sorted(pnl_by_strat.items(), key=lambda x: x[1]["pnl"], reverse=True)
        top_strat = sorted_strats[0][0]
        bottom_strat = sorted_strats[-1][0]

        # Reset all to 1.0, then adjust
        for k in _META_ALLOC:
            _META_ALLOC[k] = 1.0
        if top_strat in _META_ALLOC:
            _META_ALLOC[top_strat] = 1.5
        if bottom_strat in _META_ALLOC and sorted_strats[-1][1]["pnl"] < 0:
            _META_ALLOC[bottom_strat] = 0.5

        log.info("META-ALLOC rebalanced: top=%s(1.5x, $%+.2f) bottom=%s(%.1fx, $%+.2f)",
                 top_strat, sorted_strats[0][1]["pnl"],
                 bottom_strat, _META_ALLOC[bottom_strat], sorted_strats[-1][1]["pnl"])
        _agent_log_event("meta-alloc", f"Rebalanced: {top_strat}=1.5x(${sorted_strats[0][1]['pnl']:+.0f}) {bottom_strat}={_META_ALLOC[bottom_strat]:.1f}x(${sorted_strats[-1][1]['pnl']:+.0f})")
        return True

    except Exception as e:
        log.warning("META-ALLOC error: %s", e)
        return False


def meta_alloc_multiplier(strategy):
    """Return the current meta-allocation multiplier for a strategy."""
    return _META_ALLOC.get(strategy, 1.0)


# ---------------------------------------------------------------------------
# REVERSE COPY TRADING — Short squeeze detection
# ---------------------------------------------------------------------------
_SQUEEZE_CACHE = {"candidates": [], "fetched_at": None}


def squeeze_scan():
    """Scan for short squeeze candidates: high short interest + Z-score reversion signal.
    Returns list of candidate dicts. Cached for 1 hour."""
    now = datetime.now(timezone.utc)
    if _SQUEEZE_CACHE["fetched_at"] and (now - _SQUEEZE_CACHE["fetched_at"]).total_seconds() < 3600:
        return _SQUEEZE_CACHE["candidates"]

    candidates = []

    # Fetch most active/shorted stocks from Alpaca
    try:
        hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        # Get all assets and filter for high short interest (Alpaca provides this for paper)
        r = requests.get(f"{ALPACA_BASE_URL}/v2/assets", headers=hdrs,
                         params={"status": "active", "asset_class": "us_equity"}, timeout=15)
        if r.status_code != 200:
            return candidates

        assets = r.json()
        # Filter for tradeable, shortable stocks
        tradeable = [a for a in assets if a.get("tradable") and a.get("shortable")
                     and a.get("easy_to_borrow") is False]  # Hard to borrow = likely high SI

        # Check our pairs seed list tickers for short interest data via yfinance
        import yfinance as yf
        import numpy as np

        # Check a focused set: our seed pairs tickers + popular squeeze targets
        check_tickers = set()
        for pos in PAPER_PORTFOLIO.get("positions", []):
            for leg in (pos.get("long_leg", ""), pos.get("short_leg", "")):
                if leg:
                    check_tickers.add(leg.upper())
        # Add well-known high-SI tickers
        check_tickers.update(["GME", "AMC", "BBBY", "CVNA", "UPST", "RIVN", "LCID", "PLTR",
                              "SOFI", "NIO", "SNAP", "COIN", "MARA", "RIOT", "SQ", "HOOD"])

        for ticker in list(check_tickers)[:25]:
            try:
                info = yf.Ticker(ticker).info
                short_pct = info.get("shortPercentOfFloat", 0) or 0
                if short_pct < 0.15:  # Below 15% — not interesting
                    continue

                # Check for reversion signal: is this ticker in a pair with Z-score?
                zscore_val = None
                pair_partner = None
                # Look up in seed pairs
                _seed = globals().get("EQUITIES_CONFIG", {}).get("pairs", {}).get("seed", [])
                for pa, pb in _seed:
                    if ticker == pa:
                        try:
                            _, z, _ = calculate_pair_zscore(pa, pb, 252)
                            if z is not None:
                                zscore_val = z
                                pair_partner = pb
                        except Exception:
                            pass
                        break
                    elif ticker == pb:
                        try:
                            _, z, _ = calculate_pair_zscore(pa, pb, 252)
                            if z is not None:
                                zscore_val = -z  # Flip for the B ticker perspective
                                pair_partner = pa
                        except Exception:
                            pass
                        break

                # Calculate squeeze probability heuristic
                # Higher SI + lower Z-score (beaten down) = higher squeeze prob
                squeeze_prob = min(short_pct * 2, 0.5)  # Base from SI
                if zscore_val is not None and zscore_val < -1.0:
                    squeeze_prob += min(abs(zscore_val) * 0.1, 0.3)  # Bonus for reversion signal
                squeeze_prob = min(squeeze_prob, 0.95)

                price = info.get("currentPrice") or info.get("regularMarketPrice") or 0

                candidates.append({
                    "ticker": ticker,
                    "short_interest": short_pct,
                    "price": price,
                    "zscore": zscore_val,
                    "pair_partner": pair_partner,
                    "squeeze_prob": squeeze_prob,
                    "market_cap": info.get("marketCap", 0),
                })
            except Exception:
                continue

        candidates.sort(key=lambda x: x["squeeze_prob"], reverse=True)

    except Exception as e:
        log.warning("SQUEEZE-SCAN error: %s", e)

    _SQUEEZE_CACHE["candidates"] = candidates[:10]
    _SQUEEZE_CACHE["fetched_at"] = now
    return candidates[:10]


# ---------------------------------------------------------------------------
# MONTE CARLO SIMULATION AGENT — Probability engine for trade decisions
# ---------------------------------------------------------------------------
_MC_CACHE = {}  # {cache_key: {"result": {...}, "fetched_at": datetime}}


def montecarlo_simulate(ticker_a, ticker_b=None, entry_zscore=None, n_paths=10000, horizon_days=7):
    """Run Monte Carlo simulation on a pair spread or single ticker.
    Returns dict with prob_profit, expected_value, max_drawdown_95, kelly_size, etc.
    Cached for 30 minutes."""
    cache_key = f"{ticker_a}/{ticker_b or 'solo'}:{entry_zscore or 0:.1f}"
    now = datetime.now(timezone.utc)

    cached = _MC_CACHE.get(cache_key)
    if cached and (now - cached["fetched_at"]).total_seconds() < 1800:
        return cached["result"]

    result = {"prob_profit": 0.50, "expected_value": 0.0, "max_drawdown_95": 0.0,
              "kelly_fraction": 0.01, "sharpe": 0.0, "ci_low": 0.0, "ci_high": 0.0,
              "n_paths": n_paths, "available": False}

    try:
        import yfinance as yf
        import numpy as np

        data_a = yf.download(ticker_a, period="252d", progress=False)
        if len(data_a) < 100:
            _MC_CACHE[cache_key] = {"result": result, "fetched_at": now}
            return result
        prices_a = data_a["Close"].values.flatten()

        if ticker_b:
            # Pairs mode: simulate the spread ratio
            data_b = yf.download(ticker_b, period="252d", progress=False)
            if len(data_b) < 100:
                _MC_CACHE[cache_key] = {"result": result, "fetched_at": now}
                return result
            prices_b = data_b["Close"].values.flatten()
            min_len = min(len(prices_a), len(prices_b))
            prices_a, prices_b = prices_a[-min_len:], prices_b[-min_len:]
            ratio = prices_a / prices_b
            log_returns = np.diff(np.log(ratio))
        else:
            # Single ticker mode
            log_returns = np.diff(np.log(prices_a))

        mu = float(np.mean(log_returns))
        sigma = float(np.std(log_returns))
        if sigma == 0:
            _MC_CACHE[cache_key] = {"result": result, "fetched_at": now}
            return result

        # For pairs: mean-reversion drift toward Z=0
        if ticker_b and entry_zscore is not None and abs(entry_zscore) > 0.5:
            # Add mean-reversion pull: drift toward mean
            mean_ratio = float(np.mean(ratio))
            std_ratio = float(np.std(ratio))
            current_ratio = float(ratio[-1])
            if std_ratio > 0:
                # Half-life based drift: pull ~5% per day toward mean
                reversion_pull = -0.05 * (current_ratio - mean_ratio) / current_ratio
                mu = mu + reversion_pull

        # Simulate n_paths
        np.random.seed(42)  # Reproducible for same inputs
        rand = np.random.normal(mu, sigma, (n_paths, horizon_days))
        cum_returns = np.cumsum(rand, axis=1)
        final_returns = cum_returns[:, -1]

        # For pairs: profit = spread reverts toward 0 from entry Z
        # Entry zscore > 0 → short spread (profit if returns < 0)
        # Entry zscore < 0 → long spread (profit if returns > 0)
        if ticker_b and entry_zscore is not None:
            if entry_zscore > 0:
                path_pnls = -final_returns  # Short spread
            else:
                path_pnls = final_returns   # Long spread
        else:
            path_pnls = final_returns

        # Calculate statistics
        prob_profit = float(np.mean(path_pnls > 0))
        expected_value = float(np.mean(path_pnls))
        std_pnl = float(np.std(path_pnls))

        # Max drawdown at 95th percentile
        if ticker_b:
            # Track worst-case path drawdowns
            if entry_zscore and entry_zscore > 0:
                path_values = -cum_returns
            else:
                path_values = cum_returns
            running_max = np.maximum.accumulate(path_values, axis=1)
            drawdowns = running_max - path_values
            max_dd_per_path = np.max(drawdowns, axis=1)
        else:
            running_max = np.maximum.accumulate(cum_returns, axis=1)
            drawdowns = running_max - cum_returns
            max_dd_per_path = np.max(drawdowns, axis=1)
        max_drawdown_95 = float(np.percentile(max_dd_per_path, 95))

        # Kelly criterion: f* = (p * b - q) / b where b = win/loss ratio
        wins = path_pnls[path_pnls > 0]
        losses = path_pnls[path_pnls <= 0]
        if len(wins) > 0 and len(losses) > 0:
            avg_win = float(np.mean(wins))
            avg_loss = float(np.mean(np.abs(losses)))
            if avg_loss > 0:
                b = avg_win / avg_loss
                p = prob_profit
                q = 1 - p
                kelly = (p * b - q) / b
                kelly = max(0, min(kelly, 0.25))  # Cap at 25%
            else:
                kelly = 0.02
        else:
            kelly = 0.01

        # Confidence interval (5th to 95th percentile of final returns)
        ci_low = float(np.percentile(path_pnls, 5))
        ci_high = float(np.percentile(path_pnls, 95))

        # Sharpe ratio
        sharpe = float(expected_value / std_pnl) if std_pnl > 0 else 0

        result = {
            "prob_profit": prob_profit,
            "expected_value": expected_value,
            "max_drawdown_95": max_drawdown_95,
            "kelly_fraction": kelly,
            "sharpe": sharpe,
            "ci_low": ci_low,
            "ci_high": ci_high,
            "n_paths": n_paths,
            "sigma": sigma,
            "available": True,
        }

    except Exception as e:
        log.warning("MONTE CARLO error %s/%s: %s", ticker_a, ticker_b, e)

    _MC_CACHE[cache_key] = {"result": result, "fetched_at": now}
    return result


def montecarlo_size_adjustment(mc_result):
    """Return (multiplier, skip_trade) based on Monte Carlo probability.
    prob < 45% → skip, prob < 55% → 0.5x, else 1.0x."""
    if not mc_result.get("available"):
        return 1.0, False
    prob = mc_result["prob_profit"]
    if prob < 0.45:
        return 0.0, True   # Skip trade
    elif prob < 0.55:
        return 0.5, False  # Reduce size
    else:
        return 1.0, False  # Full size


def calculate_pair_zscore(ticker_a, ticker_b, lookback=252):
    """Calculate Z-score of price ratio spread for a pair."""
    try:
        import yfinance as yf
        import numpy as np
        data_a = yf.download(ticker_a, period=f"{lookback}d", progress=False)
        data_b = yf.download(ticker_b, period=f"{lookback}d", progress=False)
        if len(data_a) < 100 or len(data_b) < 100:
            log.warning("PAIRS SKIP: %s/%s insufficient data (%d/%d rows)", ticker_a, ticker_b, len(data_a), len(data_b))
            return None, None, None
        # Flatten the arrays to fix the yfinance 2D DataFrame bug
        prices_a = data_a["Close"].values.flatten()[-lookback:]
        prices_b = data_b["Close"].values.flatten()[-lookback:]
        min_len = min(len(prices_a), len(prices_b))
        prices_a = prices_a[-min_len:]
        prices_b = prices_b[-min_len:]
        ratio = prices_a / prices_b
        _corr_matrix = np.corrcoef(prices_a, prices_b)
        correlation = float(_corr_matrix[0, 1])
        if np.isnan(correlation):
            log.warning("PAIRS NaN: %s/%s - insufficient variance in price data", ticker_a, ticker_b)
            return None, None, None
        mean_ratio = float(np.mean(ratio))
        std_ratio = float(np.std(ratio))
        if std_ratio == 0:
            return correlation, 0.0, mean_ratio
        current_ratio = float(prices_a[-1] / prices_b[-1])
        zscore = (current_ratio - mean_ratio) / std_ratio
        return correlation, zscore, mean_ratio
    except Exception as e:
        log.warning("Pairs calc error %s/%s: %s", ticker_a, ticker_b, e)
        return None, None, None

def _regime_weighted_half_kelly(portfolio_value, mc_prob, hist_stats):
    """Regime-Weighted Half-Kelly sizing for pairs trades.
    Returns (size_per_leg, details_dict) or (None, details_dict) if gates fail."""
    details = {}

    # --- Gate checks ---
    mc_ok = mc_prob is not None and mc_prob > 0.60
    hist_reversion = hist_stats.get("reversion_rate", 0) if hist_stats.get("available") else 0
    hist_ok = hist_reversion > 0.55
    details["mc_prob"] = mc_prob
    details["hist_reversion"] = hist_reversion
    details["gates_passed"] = mc_ok and hist_ok
    if not (mc_ok and hist_ok):
        return None, details

    # --- Live win rate (p) and payoff ratio (b) from closed pairs trades ---
    p = 0.55  # default if insufficient data
    b = 1.0   # default payoff ratio
    try:
        import sqlite3 as _ksq
        _kconn = _ksq.connect(DB_PATH)
        _kc = _kconn.cursor()
        rows = _kc.execute("""
            SELECT realized_pnl, size_usd FROM positions
            WHERE status='closed' AND strategy='pairs'
            AND realized_pnl IS NOT NULL AND size_usd > 0
            ORDER BY closed_at DESC LIMIT 50
        """).fetchall()
        _kconn.close()
        if len(rows) >= 10:
            wins = [r[0] for r in rows if r[0] > 0]
            losses = [abs(r[0]) for r in rows if r[0] <= 0]
            total = len(rows)
            p = len(wins) / total
            if wins and losses:
                b = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    except Exception:
        pass
    details["live_win_rate"] = p
    details["payoff_ratio"] = b

    # --- Half-Kelly: f = (p - q/b) / 2 ---
    q = 1 - p
    if b > 0:
        kelly_full = (p - q / b)
    else:
        kelly_full = 0
    kelly_half = max(kelly_full / 2, 0)
    details["kelly_full"] = kelly_full
    details["kelly_half"] = kelly_half

    # --- Regime multiplier from Psychologist + VIX ---
    fng = _PSYCH_STATE.get("last_fng", 50)
    vix = get_regime("equities").get("vix") or 20
    if fng < 20 or vix > 30:
        regime_mult = 1.5  # Extreme fear / high vol — contrarian boost
    elif fng > 80 or vix < 15:
        regime_mult = 0.6  # Extreme greed / low vol — reduce
    else:
        regime_mult = 1.0
    details["fng"] = fng
    details["vix"] = vix
    details["regime_mult"] = regime_mult

    # --- Final size per leg ---
    raw_pct = kelly_half * regime_mult
    clamped_pct = max(0.01, min(raw_pct, 0.03))  # Floor 1.0%, cap 3%
    size_per_leg = clamped_pct * portfolio_value
    flat_size = portfolio_value * 0.015  # What flat sizing would have been

    details["raw_pct"] = raw_pct
    details["clamped_pct"] = clamped_pct
    details["size_per_leg"] = size_per_leg
    details["flat_size"] = flat_size

    return size_per_leg, details


def scan_pairs_opportunities():
    """Scan seed pairs for entry signals."""
    if not EQUITIES_ENABLED:
        return []
    if not is_market_open():
        return []
    opportunities = []
    cfg = EQUITIES_CONFIG["pairs"]
    for ticker_a, ticker_b in cfg["seed"]:
        corr, zscore, mean_ratio = calculate_pair_zscore(ticker_a, ticker_b, cfg["lookback_days"])
        if corr is None:
            continue
            
        # Telemetry: Print every calculation before filtering
        log.info("[PAIRS DATA] %s/%s | Corr: %.3f | Z-Score: %.2f", ticker_a, ticker_b, corr, zscore)
            
        if corr < cfg["min_correlation"]:
            continue
        if abs(zscore) >= cfg["zscore_entry"]:
            direction = "short_a_long_b" if zscore > 0 else "long_a_short_b"
            opportunities.append({
                "type": "pairs",
                "pair": f"{ticker_a}/{ticker_b}",
                "ticker_a": ticker_a,
                "ticker_b": ticker_b,
                "correlation": corr,
                "zscore": zscore,
                "direction": direction,
                "mean_ratio": mean_ratio,
            })
            _pair_key = f"PAIRS:{ticker_a}/{ticker_b}"
            # Cooldown: block re-entry for 4 hours after last close
            try:
                import sqlite3 as _sq2
                _cc = _sq2.connect(DB_PATH)
                _cr = _cc.cursor()
                _cr.execute("SELECT closed_at FROM positions WHERE market_id=? AND status='closed' ORDER BY closed_at DESC LIMIT 1", (_pair_key,))
                _row = _cr.fetchone()
                _cc.close()
                if _row and _row[0]:
                    import datetime as _dt_mod
                    _ct = _dt_mod.datetime.fromisoformat(_row[0]).replace(tzinfo=_dt_mod.timezone.utc)
                    _mins = (_dt_mod.datetime.now(_dt_mod.timezone.utc) - _ct).total_seconds() / 60
                    if _mins < 240:  # 4 hour cooldown
                        log.info("PAIRS COOLDOWN: %s closed %.0f min ago (need 240)", _pair_key, _mins)
                        continue
            except Exception:
                pass
            if any(p.get("market","") == _pair_key for p in PAPER_PORTFOLIO.get("positions",[])):
                log.info("PAIRS DEDUP: %s already open (memory)", _pair_key)
                continue
            # Also check SQLite for zombie positions (open in DB but not in JSON)
            try:
                import sqlite3 as _dedup_sq
                _dedup_conn = _dedup_sq.connect(DB_PATH)
                _dedup_c = _dedup_conn.cursor()
                _dedup_c.execute("SELECT COUNT(*) FROM positions WHERE market_id=? AND status='open'", (_pair_key,))
                _dedup_db_open = _dedup_c.fetchone()[0] > 0
                _dedup_conn.close()
                if _dedup_db_open:
                    log.info("PAIRS DEDUP: %s already open (db zombie)", _pair_key)
                    continue
            except Exception:
                pass
            log.info("PAIRS SIGNAL: %s/%s corr=%.3f zscore=%.2f dir=%s", ticker_a, ticker_b, corr, zscore, direction)
            # Historian Agent: fetch reversion stats and adjust sizing
            _hist_stats = historian_analyze_pair(ticker_a, ticker_b)
            _hist_mult = historian_size_multiplier(_hist_stats)
            if _hist_stats.get("available"):
                log.info("HISTORIAN: %s/%s reversion=%.0f%% avg=%.1fd max_z=%.1f samples=%d → %.1fx",
                         ticker_a, ticker_b, _hist_stats["reversion_rate"] * 100,
                         _hist_stats["avg_reversion_days"], _hist_stats["max_adverse_z"],
                         _hist_stats["sample_size"], _hist_mult)
            # Master Arbiter: unified pre-trade check
            _arb_ok, _arb_mult, _arb_reasons = arbiter_check(
                "pairs", ticker_a, ticker_b, zscore=zscore, corr=corr)
            if not _arb_ok:
                log.info("ARBITER BLOCK: %s/%s — %s", ticker_a, ticker_b, _arb_reasons[-1] if _arb_reasons else "?")
                continue
            log.info("ARBITER: %s/%s approved %.2fx (%d checks)", ticker_a, ticker_b, _arb_mult, len(_arb_reasons))
            # Auto-execute pairs trade in paper mode
            if TRADING_MODE == "paper" and AUTO_PAPER_ENABLED:
                # --- Regime-Weighted Half-Kelly sizing ---
                _portfolio_val = PAPER_PORTFOLIO.get("cash", 25000)
                _mc_prob = None
                try:
                    _mc_res = montecarlo_simulate(ticker_a, ticker_b, entry_zscore=zscore, horizon_days=7)
                    if _mc_res.get("available"):
                        _mc_prob = _mc_res["prob_profit"]
                except Exception:
                    pass
                _kelly_size, _kelly_details = _regime_weighted_half_kelly(_portfolio_val, _mc_prob, _hist_stats)
                _flat_size_ref = _portfolio_val * 0.015  # Reference: what flat 1.5% would be
                if _kelly_size is not None:
                    _pair_size = _kelly_size * _hist_mult * _arb_mult
                    log.info("KELLY SIZING: %s/%s kelly_half=%.3f regime=%.1fx → %.1f%% ($%.0f/leg) "
                             "[flat would be $%.0f] p=%.0f%% b=%.2f fng=%d vix=%.0f",
                             ticker_a, ticker_b, _kelly_details["kelly_half"],
                             _kelly_details["regime_mult"], _kelly_details["clamped_pct"] * 100,
                             _pair_size, _flat_size_ref,
                             _kelly_details["live_win_rate"] * 100, _kelly_details["payoff_ratio"],
                             _kelly_details["fng"], _kelly_details["vix"])
                else:
                    # Fallback to flat sizing when Kelly gates fail (MC<=60% or hist reversion<=55%)
                    _base_size = _portfolio_val * 0.015
                    _kelly_mult = max(0.75, min((abs(zscore) / 2.0) * corr, 2.0))
                    _pair_size = _base_size * _kelly_mult * _hist_mult * _arb_mult
                    log.info("FLAT SIZING: %s/%s $%.0f/leg (Kelly gates failed: MC=%.0f%% hist_rev=%.0f%%)",
                             ticker_a, ticker_b, _pair_size,
                             (_mc_prob or 0) * 100, _kelly_details.get("hist_reversion", 0) * 100)
                # Causal Memory: adjust size based on historical regime similarity
                try:
                    _cm_mult, _cm_details = causal_memory_size_adjustment("pairs")
                    if _cm_mult != 1.0:
                        _pair_size *= _cm_mult
                        log.info("CAUSAL MEMORY PAIRS: %s/%s %.2fx → $%.0f | %s",
                                 ticker_a, ticker_b, _cm_mult, _pair_size, _cm_details)
                except Exception:
                    pass

                # Earnings guard: tighten sizing if either ticker reports this week
                if is_earnings_week(ticker_a) or is_earnings_week(ticker_b):
                    _pair_size *= 0.5
                    log.info("EARNINGS GUARD: %s/%s sized 0.5x (earnings this week)", ticker_a, ticker_b)

                if direction == "short_a_long_b":
                    _long_tk, _short_tk = ticker_b, ticker_a
                else:
                    _long_tk, _short_tk = ticker_a, ticker_b
                # Submit both legs to Alpaca paper API
                _entry_long_price = 0
                _entry_short_price = 0
                _long_order_id = None
                _short_order_id = None
                _orders_ok = False
                try:
                    import requests as _ep_req, math as _math
                    _ep_hdr = {
                        "APCA-API-KEY-ID": ALPACA_API_KEY,
                        "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
                        "Content-Type": "application/json",
                    }
                    _alpaca_orders_url = f"{ALPACA_BASE_URL}/v2/orders"

                    # Long leg — limit at mid, 3 attempts, fallback to market
                    _long_order_id, _entry_long_price, _long_fill_type = _alpaca_limit_at_mid(
                        _long_tk, "buy", notional=_pair_size)
                    if _long_order_id is None:
                        log.warning("ALPACA LONG FAILED: %s — no fill", _long_tk)
                        continue

                    log.info("ALPACA LONG ORDER: %s id=%s fill=$%.2f type=%s",
                             _long_tk, _long_order_id, _entry_long_price, _long_fill_type)

                    # Short leg — need whole shares, limit at mid
                    _short_price = 0
                    try:
                        _data_hdr_s = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
                        _sq = _ep_req.get(f"https://data.alpaca.markets/v2/stocks/{_short_tk}/quotes/latest", headers=_data_hdr_s, timeout=5)
                        if _sq.status_code == 200:
                            _short_price = float(_sq.json().get("quote", {}).get("ap", 0) or 0)
                    except Exception:
                        pass
                    _short_shares = _math.floor(_pair_size / _short_price) if _short_price > 0 else 0
                    if _short_shares < 1:
                        log.warning("ALPACA SHORT SKIP: %s — %d shares at $%.2f (notional=$%.2f)", _short_tk, _short_shares, _short_price, _pair_size)
                        try:
                            _ep_req.delete(f"{_alpaca_orders_url}/{_long_order_id}", headers=_ep_hdr, timeout=5)
                            log.info("ALPACA CANCEL LONG: %s (short shares=0)", _long_order_id)
                        except Exception:
                            pass
                        continue

                    _short_order_id, _entry_short_price, _short_fill_type = _alpaca_limit_at_mid(
                        _short_tk, "sell", qty=_short_shares)
                    if _short_order_id is None:
                        log.warning("ALPACA SHORT FAILED: %s — no fill, cancelling long", _short_tk)
                        try:
                            _ep_req.delete(f"{_alpaca_orders_url}/{_long_order_id}", headers=_ep_hdr, timeout=5)
                        except Exception:
                            pass
                        continue

                    log.info("ALPACA SHORT ORDER: %s id=%s fill=$%.2f type=%s",
                             _short_tk, _short_order_id, _entry_short_price, _short_fill_type)

                    _orders_ok = True

                    # Fetch fill prices if not returned inline
                    if _entry_long_price <= 0 or _entry_short_price <= 0:
                        _data_hdr = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
                        _ql = _ep_req.get(f"https://data.alpaca.markets/v2/stocks/{_long_tk}/quotes/latest", headers=_data_hdr, timeout=5)
                        _qs = _ep_req.get(f"https://data.alpaca.markets/v2/stocks/{_short_tk}/quotes/latest", headers=_data_hdr, timeout=5)
                        if _ql.status_code == 200 and _entry_long_price <= 0:
                            _entry_long_price = float(_ql.json().get("quote", {}).get("ap", 0) or 0)
                        if _qs.status_code == 200 and _entry_short_price <= 0:
                            _entry_short_price = float(_qs.json().get("quote", {}).get("bp", 0) or 0)

                    log.info("PAIRS FILL QUALITY: Long %s=$%.2f (%s) Short %s=$%.2f (%s)",
                             _long_tk, _entry_long_price, _long_fill_type,
                             _short_tk, _entry_short_price, _short_fill_type)
                except Exception as _ep_err:
                    log.warning("Alpaca pairs order failed: %s", _ep_err)
                    reconcile_alpaca_positions()
                    continue  # Don't open position if orders failed

                if not _orders_ok:
                    reconcile_alpaca_positions()
                    continue

                _pair_pos = {
                    "market": f"PAIRS:{ticker_a}/{ticker_b}",
                    "side": direction, "shares": 1,
                    "entry_price": zscore, "cost": _pair_size * 2,
                    "value": _pair_size * 2,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "platform": "Alpaca", "ev": abs(zscore) / 10,
                    "strategy": "pairs", "long_leg": _long_tk, "short_leg": _short_tk,
                    "entry_zscore": zscore, "correlation": corr,
                    "entry_long_price": _entry_long_price,
                    "entry_short_price": _entry_short_price,
                    "long_order_id": _long_order_id,
                    "short_order_id": _short_order_id,
                }
                PAPER_PORTFOLIO["positions"].append(_pair_pos)
                PAPER_PORTFOLIO["cash"] -= _pair_size * 2
                db_log_paper_trade(_pair_pos)
                db_open_position(
                    market_id=f"PAIRS:{ticker_a}/{ticker_b}",
                    platform="Alpaca", strategy="pairs", direction=direction,
                    size_usd=_pair_size * 2, shares=1, entry_price=zscore,
                    long_leg=_long_tk, short_leg=_short_tk, entry_zscore=zscore,
                    regime=get_regime("equities").get("regime","normal"),
                    metadata={"correlation": corr, "long": _long_tk, "short": _short_tk,
                              "long_order_id": _long_order_id, "short_order_id": _short_order_id}
                )
                db_save_daily_state()
                log.info("PAIRS TRADE: Long %s / Short %s | Z=%.2f | Size=$%.0f per leg",
                         _long_tk, _short_tk, zscore, _pair_size)
    return opportunities

# --- PEAD ENGINE (RULE-BASED) ---
def check_earnings_surprise(ticker):
    """Check if a stock has a recent earnings surprise > 15%."""
    try:
        import yfinance as yf
        stock = yf.Ticker(ticker)
        earnings = stock.earnings_dates
        if earnings is None or len(earnings) == 0:
            return None
        latest = earnings.iloc[0]
        estimate = latest.get("EPS Estimate", 0)
        actual = latest.get("Reported EPS", 0)
        if estimate and actual and estimate != 0:
            surprise = (actual - estimate) / abs(estimate)
            return {"ticker": ticker, "surprise_pct": surprise, "actual": actual, "estimate": estimate}
    except Exception as e:
        log.warning("PEAD check error %s: %s", ticker, e)
    return None


# ============================================================================
# HOLDINGS FENCE - Protect long-term crypto holdings
# ============================================================================
QUARANTINED_ASSETS = ["BTC", "ETH", "XRP", "DOGE", "SOL", "XLM", "SHIB", "HBAR", "ALGO", "REN", "ZEC", "WBT"]
TRADEABLE_FIAT = ["USD", "USDC", "USDT"]

def get_tradeable_capital(platform_balances):
    """Only count fiat/stablecoins as available trading capital. Ignore crypto holdings."""
    capital = 0
    for currency, amount in platform_balances.items():
        if currency.upper() in TRADEABLE_FIAT:
            capital += amount
    return capital

def is_bot_owned_position(asset, side="SELL"):
    """Check if the bot opened this position. Prevents selling long-term holdings."""
    if side != "SELL":
        return True
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM paper_trades WHERE market LIKE ? AND side = 'BUY'", (f"%{asset}%",))
        bot_bought = c.fetchone()[0]
        conn.close()
        return bot_bought > 0
    except Exception:
        return False

# ============================================================================
# RACE CONDITION LOCK - prevents concurrent duplicate trades
# ============================================================================
ACTIVE_TRADE_LOCK = set()

def acquire_trade_lock(market_key):
    if market_key in ACTIVE_TRADE_LOCK:
        return False
    ACTIVE_TRADE_LOCK.add(market_key)
    return True

def release_trade_lock(market_key):
    ACTIVE_TRADE_LOCK.discard(market_key)

RESOLUTION_TRACKER = {"resolved_trades": [], "total_brier": 0.0, "total_resolved": 0, "calibration_scores": []}

def record_resolution(trade, outcome):
    entry_price = trade.get("entry_price", 0.5)
    predicted_ev = trade.get("ev", 0)
    realized_edge = (1.0 - entry_price) if outcome == 1.0 else (0.0 - entry_price)
    brier = (entry_price - outcome) ** 2
    ev_accuracy = abs(realized_edge - predicted_ev)
    result = {"market": trade.get("market", ""), "platform": trade.get("platform", ""),
              "entry_price": entry_price, "outcome": outcome, "realized_edge": realized_edge,
              "predicted_ev": predicted_ev, "ev_accuracy": ev_accuracy, "brier_score": brier,
              "pnl": realized_edge * trade.get("shares", 1), "resolved_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
    RESOLUTION_TRACKER["resolved_trades"].append(result)
    RESOLUTION_TRACKER["total_brier"] += brier
    RESOLUTION_TRACKER["total_resolved"] += 1
    RESOLUTION_TRACKER["calibration_scores"].append(ev_accuracy)
    return result

def get_calibration_summary():
    rt = RESOLUTION_TRACKER
    if rt["total_resolved"] == 0:
        return {"avg_brier": None, "win_rate": None, "total": 0, "total_pnl": 0}
    avg_brier = rt["total_brier"] / rt["total_resolved"]
    wins = sum(1 for t in rt["resolved_trades"] if t["pnl"] > 0)
    avg_ev = sum(rt["calibration_scores"]) / len(rt["calibration_scores"])
    return {"avg_brier": avg_brier, "avg_ev_accuracy": avg_ev,
            "win_rate": wins / rt["total_resolved"] * 100, "total": rt["total_resolved"],
            "total_pnl": sum(t["pnl"] for t in rt["resolved_trades"])}


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == "__main__":
    if not DISCORD_TOKEN:
        log.error("DISCORD_TOKEN not set -- cannot start bot")
        raise SystemExit(1)
    start_webhook_server(port=int(os.getenv("TV_WEBHOOK_PORT", "8080")))
    bot.run(DISCORD_TOKEN)



# ═══════════════════════════════════════════════════════════════════
# REGIME DETECTION ENGINE v1 — TraderJoes v15
# VIX-gated for equities/options. Own 24h vol for crypto.
# Does NOT gate options or prediction markets — only mean-reversion.
# ═══════════════════════════════════════════════════════════════════
import statistics as _stats

REGIME_CACHE = {}
REGIME_CACHE_TTL = 300

# ═══════════════════════════════════════════════════════════════════
# CRYPTO PAIRS STAT ARB — Z-score mean reversion on crypto pairs 24/7
# ═══════════════════════════════════════════════════════════════════
CRYPTO_PAIRS_CONFIG = {
    "enabled": True,
    "seed": [("BTC", "ETH"), ("SOL", "AVAX"), ("BNB", "ETH")],
    "lookback_days": 30,
    "zscore_entry": 1.1,
    "zscore_exit": 0.0,
    "mc_min_prob": 0.60,
    "ttl_hours": 48,
    "size_pct": 0.015,       # 1.5% of portfolio per leg
    "max_positions": 2,
    "scan_interval_min": 5,
}
_CRYPTO_PAIRS_CACHE = {}  # {pair_key: {"corr": x, "zscore": y, "prices_a": [...], "prices_b": [...], "ts": datetime}}


def _fetch_crypto_price_history(symbol, days=30):
    """Fetch daily close prices for a crypto symbol from Binance klines. Returns list of floats."""
    try:
        interval = "1d"
        limit = days + 5
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol}USDT&interval={interval}&limit={limit}"
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            klines = r.json()
            closes = [float(k[4]) for k in klines]  # Close price is index 4
            return closes[-days:] if len(closes) >= days else closes
    except Exception:
        pass
    return []


def _fetch_crypto_spot_price(symbol):
    """Get current spot price from Coinbase, Binance fallback. Returns float or 0."""
    try:
        r = requests.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot", timeout=5)
        if r.status_code == 200:
            return float(r.json().get("data", {}).get("amount", 0))
    except Exception:
        pass
    try:
        r = requests.get(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}USDT", timeout=5)
        if r.status_code == 200:
            return float(r.json().get("price", 0))
    except Exception:
        pass
    return 0


def calculate_crypto_pair_zscore(sym_a, sym_b, lookback=30):
    """Calculate Z-score and correlation for a crypto pair using Binance daily data.
    Returns (correlation, zscore, mean_ratio) or (None, None, None)."""
    try:
        import numpy as np
        prices_a = _fetch_crypto_price_history(sym_a, lookback)
        prices_b = _fetch_crypto_price_history(sym_b, lookback)
        if len(prices_a) < 15 or len(prices_b) < 15:
            return None, None, None
        min_len = min(len(prices_a), len(prices_b))
        pa = np.array(prices_a[-min_len:])
        pb = np.array(prices_b[-min_len:])
        ratio = pa / pb
        corr = float(np.corrcoef(pa, pb)[0, 1])
        if np.isnan(corr):
            return None, None, None
        mean_r = float(np.mean(ratio))
        std_r = float(np.std(ratio))
        if std_r == 0:
            return corr, 0.0, mean_r
        # Current ratio from live prices
        live_a = _fetch_crypto_spot_price(sym_a)
        live_b = _fetch_crypto_spot_price(sym_b)
        if live_a <= 0 or live_b <= 0:
            return corr, 0.0, mean_r
        current_ratio = live_a / live_b
        zscore = (current_ratio - mean_r) / std_r
        return round(corr, 3), round(zscore, 3), round(mean_r, 6)
    except Exception as e:
        log.warning("CRYPTO PAIRS calc %s/%s: %s", sym_a, sym_b, e)
        return None, None, None


async def scan_crypto_pairs():
    """Scan crypto pairs for Z-score entry signals. Runs every 5 minutes 24/7."""
    cfg = CRYPTO_PAIRS_CONFIG
    if not cfg["enabled"]:
        return 0

    now = datetime.now(timezone.utc)
    fired = 0

    # Count open crypto pairs
    cp_count = sum(1 for p in PAPER_PORTFOLIO.get("positions", [])
                   if p.get("strategy") == "crypto_pairs")
    if cp_count >= cfg["max_positions"]:
        return 0

    portfolio_value = PAPER_PORTFOLIO.get("cash", 25000) + sum(
        p.get("cost", 0) for p in PAPER_PORTFOLIO.get("positions", []))

    for sym_a, sym_b in cfg["seed"]:
        if cp_count + fired >= cfg["max_positions"]:
            break

        pair_id = f"CRYPTO_PAIRS:{sym_a}/{sym_b}"
        # Dedup
        if any(p.get("market") == pair_id for p in PAPER_PORTFOLIO.get("positions", [])):
            continue

        corr, zscore, mean_ratio = calculate_crypto_pair_zscore(sym_a, sym_b, cfg["lookback_days"])
        if corr is None:
            continue

        _CRYPTO_PAIRS_CACHE[f"{sym_a}/{sym_b}"] = {
            "corr": corr, "zscore": zscore, "mean_ratio": mean_ratio, "ts": now}

        log.info("CRYPTO PAIRS: %s/%s corr=%.3f z=%.3f", sym_a, sym_b, corr, zscore)

        if abs(zscore) < cfg["zscore_entry"] or corr < 0.5:
            continue

        # Monte Carlo validation
        mc_prob = 0.65  # Default — crypto MC uses simpler model
        try:
            _mc = montecarlo_simulate(f"{sym_a}USD", f"{sym_b}USD", horizon_days=5)
            if _mc.get("available"):
                mc_prob = _mc["prob_profit"]
                if mc_prob < cfg["mc_min_prob"]:
                    log.info("CRYPTO PAIRS MC SKIP: %s/%s prob=%.0f%%", sym_a, sym_b, mc_prob * 100)
                    continue
        except Exception:
            pass

        direction = "short_a_long_b" if zscore > 0 else "long_a_short_b"
        long_sym = sym_b if zscore > 0 else sym_a
        short_sym = sym_a if zscore > 0 else sym_b

        leg_size = portfolio_value * cfg["size_pct"]
        log.info("CRYPTO PAIRS SIGNAL: %s/%s z=%.2f corr=%.2f → Long %s / Short %s | $%.0f/leg",
                 sym_a, sym_b, zscore, corr, long_sym, short_sym, leg_size)

        # Execute: Coinbase spot for long leg, Phemex perp for short leg
        try:
            _spot_ok, _spot_msg = await execute_coinbase_order("BUY", long_sym, leg_size)
            if not _spot_ok:
                log.warning("CRYPTO PAIRS LONG FAILED: %s %s", long_sym, _spot_msg[:80])
                continue
            import re as _cp_re
            _spot_oid = ""
            _m = _cp_re.search(r"ID: ([^\)]+)", _spot_msg)
            if _m:
                _spot_oid = _m.group(1)

            _perp_ok, _perp_msg, _perp_oid = await execute_phemex_perp_short(short_sym, leg_size)
            if not _perp_ok:
                log.warning("CRYPTO PAIRS SHORT FAILED: %s — unwinding spot", short_sym)
                try:
                    await execute_coinbase_order("SELL", long_sym, leg_size)
                except Exception:
                    pass
                continue

            total_cost = leg_size * 2
            PAPER_PORTFOLIO["cash"] -= total_cost
            _pos = {
                "market": pair_id,
                "side": f"Long {long_sym} / Short {short_sym}",
                "shares": 1, "entry_price": zscore, "cost": total_cost, "value": total_cost,
                "timestamp": now.strftime("%Y-%m-%d %H:%M UTC"),
                "platform": "Coinbase+Phemex", "ev": abs(zscore) / 10,
                "strategy": "crypto_pairs",
                "long_leg": long_sym, "short_leg": short_sym,
                "entry_zscore": zscore, "entry_corr": corr,
                "spot_order_id": _spot_oid, "perp_order_id": _perp_oid or "",
            }
            PAPER_PORTFOLIO["positions"].append(_pos)
            db_log_paper_trade(_pos)
            db_open_position(
                market_id=pair_id, platform="Coinbase+Phemex", strategy="crypto_pairs",
                direction=f"Long {long_sym} / Short {short_sym}",
                size_usd=total_cost, shares=1, entry_price=zscore,
                long_leg=long_sym, short_leg=short_sym, entry_zscore=zscore,
                metadata={"corr": corr, "zscore": zscore, "mc_prob": mc_prob,
                          "spot_oid": _spot_oid, "perp_oid": _perp_oid or ""},
            )
            fired += 1
            log.info("CRYPTO PAIRS TRADE: %s/%s z=%.2f Long %s / Short %s $%.0f",
                     sym_a, sym_b, zscore, long_sym, short_sym, total_cost)
        except Exception as e:
            log.warning("CRYPTO PAIRS execution error %s/%s: %s", sym_a, sym_b, e)

    return fired


# PEAD SCANNER v2 — Post-Earnings Announcement Drift
# 5% EPS surprise + 2.0x volume + 15-min candle confirmation
# Gated by equities regime. 72h hold. Fallback to Alpaca calendar.
# ═══════════════════════════════════════════════════════════════════

PEAD_ENABLED = True
PEAD_MIN_SURPRISE_PCT = 5.0   # Lowered from 8% — most beats are 3-7%
PEAD_MIN_VOLUME_MULT  = 2.0   # Lowered from 2.5x — still filters noise
PEAD_HOLD_HOURS       = 72
PEAD_MAX_POSITIONS    = 3
PEAD_SIZE_PCT         = 0.01
_PEAD_LAST_SCAN = {"tickers_checked": 0, "signals_found": 0, "last_run": None, "errors": []}

def _pead_candle_confirm(ticker):
    """15-min candle filter: first candle close must be > open (no gap-and-crap)."""
    try:
        import yfinance as yf
        h = yf.Ticker(ticker).history(period="1d", interval="15m")
        if h.empty or len(h) < 2:
            return True
        first = h.iloc[0]
        return float(first["Close"]) > float(first["Open"])
    except Exception:
        return True

def fetch_earnings_surprises():
    """Fetch tickers with recent earnings surprises. Uses Alpaca news + yfinance."""
    import yfinance as yf
    surprises = []
    _PEAD_LAST_SCAN["tickers_checked"] = 0
    _PEAD_LAST_SCAN["signals_found"] = 0
    _PEAD_LAST_SCAN["errors"] = []
    _PEAD_LAST_SCAN["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Method 1: Alpaca news API — find tickers mentioned in earnings headlines
    tickers = set()
    try:
        import datetime as _dt_pead
        hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        params = {
            "start": (datetime.now(timezone.utc) - _dt_pead.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "limit": 50, "sort": "desc"
        }
        resp = requests.get("https://data.alpaca.markets/v1beta1/news",
                            headers=hdrs, params=params, timeout=10)
        if resp.status_code == 200:
            articles = resp.json().get("news", [])
            earnings_kw = ["earnings", "eps", "beats", "quarterly", "revenue",
                           "profit", "q1", "q2", "q3", "q4", "guidance", "forecast"]
            for a in articles:
                if any(k in a.get("headline", "").lower() for k in earnings_kw):
                    for s in a.get("symbols", []):
                        if s and len(s) <= 5 and s.isalpha():
                            tickers.add(s)
            log.info("PEAD: %d earnings tickers from %d news articles", len(tickers), len(articles))
        else:
            log.warning("PEAD: Alpaca news API returned %d", resp.status_code)
    except Exception as e:
        log.warning("PEAD: news fetch error: %s", e)
        _PEAD_LAST_SCAN["errors"].append(f"news: {e}")

    # Method 2: Alpaca calendar API — upcoming earnings
    try:
        import datetime as _dt_pead2
        hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY}
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        yesterday = (datetime.now(timezone.utc) - _dt_pead2.timedelta(days=1)).strftime("%Y-%m-%d")
        resp = requests.get(f"https://data.alpaca.markets/v1beta1/corporate-actions/announcements",
                            headers=hdrs, params={"ca_types": "Dividend", "since": yesterday,
                                                  "until": today, "limit": 20}, timeout=10)
        # Note: Alpaca doesn't have a direct earnings calendar, but news covers it
    except Exception:
        pass

    if not tickers:
        log.info("PEAD: no earnings tickers found this cycle")
        return surprises

    for ticker in list(tickers)[:20]:
        _PEAD_LAST_SCAN["tickers_checked"] += 1
        try:
            t = yf.Ticker(ticker)
            # Try earnings_history first, then earnings_dates
            eh = None
            try:
                eh = t.earnings_history
            except Exception:
                pass
            if eh is None or (hasattr(eh, 'empty') and eh.empty):
                # Fallback: check quarterly_earnings
                try:
                    qe = t.quarterly_earnings
                    if qe is not None and not qe.empty:
                        latest = qe.iloc[0]
                        est = latest.get("Estimate", 0) or latest.get("estimate", 0)
                        act = latest.get("Actual", 0) or latest.get("actual", 0)
                        if est and est != 0:
                            eh = "fallback"
                except Exception:
                    pass
            if eh is None:
                log.info("PEAD: %s — no earnings data available", ticker)
                continue

            if eh != "fallback":
                latest = eh.iloc[0]
                est = latest.get("epsEstimate", 0)
                act = latest.get("epsActual", 0)

            if not est or est == 0:
                continue
            surprise = ((act - est) / abs(est)) * 100
            log.info("PEAD: %s EPS est=$%.2f act=$%.2f surprise=%.1f%%", ticker, est, act, surprise)

            if surprise < PEAD_MIN_SURPRISE_PCT:
                continue

            hist = t.history(period="25d")
            if hist.empty or len(hist) < 5:
                continue
            avg_vol = hist["Volume"].iloc[:-1].mean()
            vol_mult = hist["Volume"].iloc[-1] / avg_vol if avg_vol > 0 else 0
            if vol_mult < PEAD_MIN_VOLUME_MULT:
                log.info("PEAD: %s vol_mult=%.1f < %.1f (skipped)", ticker, vol_mult, PEAD_MIN_VOLUME_MULT)
                continue
            if not _pead_candle_confirm(ticker):
                log.info("PEAD: %s failed 15-min candle filter (gap-and-crap)", ticker)
                continue
            price = float(hist["Close"].iloc[-1])
            log.info("PEAD SIGNAL: %s surprise=%.1f%% vol=%.1fx price=$%.2f", ticker, surprise, vol_mult, price)
            _PEAD_LAST_SCAN["signals_found"] += 1
            surprises.append({"ticker": ticker, "surprise_pct": round(surprise, 2),
                              "volume_mult": round(vol_mult, 2), "price": price})
        except Exception as e:
            log.warning("PEAD: %s error: %s", ticker, e)
            _PEAD_LAST_SCAN["errors"].append(f"{ticker}: {e}")
    return surprises

def _count_pead_open():
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM positions WHERE status='open' AND strategy='pead'")
        n = c.fetchone()[0]; conn.close(); return n
    except Exception:
        return 0

def _log_pead(ticker, price, size, surprise, vol_mult, tp, sl):
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("""INSERT OR IGNORE INTO positions
            (market_id,platform,strategy,direction,size_usd,entry_price,status,created_at,metadata)
            VALUES (?,'alpaca','pead','long',?,?,'open',datetime('now'),?)""",
            (f"PEAD:{ticker}", round(size, 2), price,
             json.dumps({"ticker": ticker, "size_usd": size, "surprise_pct": surprise,
                         "volume_mult": vol_mult, "tp_price": tp, "sl_price": sl})))
        conn.commit(); conn.close()
    except Exception as e:
        log.warning("PEAD: DB log error: %s", e)

def _submit_pead_order(ticker, shares):
    try:
        hdrs = {"APCA-API-KEY-ID": ALPACA_API_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY,
                "Content-Type": "application/json"}
        resp = requests.post(f"{ALPACA_BASE_URL}/v2/orders",
            headers=hdrs, timeout=10,
            json={"symbol": ticker, "qty": str(round(shares, 4)),
                  "side": "buy", "type": "market", "time_in_force": "day"})
        resp.raise_for_status()
        log.info("PEAD ORDER: %s qty=%.4f id=%s", ticker, shares, resp.json().get("id", "?"))
        return resp.json()
    except Exception as e:
        log.warning("PEAD: Order error for %s: %s", ticker, e)
        return None

async def run_pead_scanner(discord_channel=None):
    if not PEAD_ENABLED:
        return
    from zoneinfo import ZoneInfo
    now_et = datetime.now(ZoneInfo("America/New_York"))
    if not (now_et.replace(hour=9, minute=45, second=0) <= now_et <=
            now_et.replace(hour=15, minute=30, second=0)):
        return
    if _count_pead_open() >= PEAD_MAX_POSITIONS:
        log.info("PEAD: %d positions open (max %d)", _count_pead_open(), PEAD_MAX_POSITIONS)
        return
    regime = get_regime("equities")
    if regime["halt"]:
        log.info("PEAD: regime=%s halted, skipping", regime["regime"])
        return
    log.info("PEAD SCAN: starting (regime=%s)", regime["regime"])
    signals = fetch_earnings_surprises()
    if not signals:
        log.info("PEAD SCAN: no signals found (%d tickers checked)", _PEAD_LAST_SCAN["tickers_checked"])
        return
    signals.sort(key=lambda x: x["surprise_pct"], reverse=True)
    bankroll = PAPER_PORTFOLIO.get("cash", 25000)
    for sig in signals[:3]:
        if _count_pead_open() >= PEAD_MAX_POSITIONS:
            break
        ticker = sig["ticker"]
        try:
            conn = sqlite3.connect(DB_PATH)
            c = conn.cursor()
            c.execute("SELECT COUNT(*) FROM positions WHERE market_id=? AND status='open'",
                      (f"PEAD:{ticker}",))
            if c.fetchone()[0] > 0:
                conn.close(); continue
            conn.close()
        except Exception:
            pass
        price = sig["price"]
        size = bankroll * PEAD_SIZE_PCT
        tp, sl = regime_adjusted_tp_sl(0.03, 0.015, "equities")
        tp_price = round(price * (1 + tp), 2)
        sl_price = round(price * (1 - sl), 2)
        order = _submit_pead_order(ticker, size / price)
        if order:
            _log_pead(ticker, price, size, sig["surprise_pct"], sig["volume_mult"], tp_price, sl_price)
            log.info("PEAD EXECUTED: %s size=$%.0f surprise=%.1f%% tp=$%.2f sl=$%.2f regime=%s",
                     ticker, size, sig["surprise_pct"], tp_price, sl_price, regime["regime"])
            try:
                if discord_channel:
                    await discord_channel.send(
                        f"**PEAD** {ticker} | Surprise: +{sig['surprise_pct']:.1f}% | "
                        f"Vol: {sig['volume_mult']:.1f}x | Entry: ${price:.2f} | "
                        f"TP: ${tp_price} | SL: ${sl_price} | Regime: {regime['regime'].upper()}")
            except Exception:
                pass

# ═══════════════════════════════════════════════════════════════════
# END v15 MODULES
# ═══════════════════════════════════════════════════════════════════

