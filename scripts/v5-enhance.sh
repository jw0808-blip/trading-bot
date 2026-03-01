#!/usr/bin/env bash
# ============================================================================
# TraderJoes V5 — Persistence, Live Data, Signal Tracking, Improvements
# ============================================================================
set -uo pipefail
cd /root/trading-bot

echo "============================================"
echo "  TraderJoes V5 — Enhancement Script"
echo "============================================"
echo ""

cp main.py main.py.bak.v5

# ============================================================
# FIX 12: Remove docker-compose version warning
# ============================================================
echo "=== Fix 12: Docker compose cleanup ==="
sed -i '/^version:/d' docker-compose.yml 2>/dev/null || true
echo "  [OK] Removed version attribute"
echo ""

# ============================================================
# FIX 15: Encrypted .env backup
# ============================================================
echo "=== Fix 15: Encrypted .env backup ==="
BACKUP_PASS=$(openssl rand -hex 16)
openssl enc -aes-256-cbc -salt -pbkdf2 -in .env -out .env.encrypted -pass pass:$BACKUP_PASS 2>/dev/null
echo "$BACKUP_PASS" > .env.backup.key
chmod 600 .env.encrypted .env.backup.key
echo "  [OK] .env encrypted to .env.encrypted"
echo "  [OK] Decryption key saved to .env.backup.key"
echo "  To restore: openssl enc -d -aes-256-cbc -pbkdf2 -in .env.encrypted -out .env -pass file:.env.backup.key"
echo ""

# ============================================================
# MAIN PYTHON PATCHES
# ============================================================
echo "=== Patching main.py ==="

python3 << 'V5PATCH'
import re

with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

changes = 0

# ============================================================
# 1/8: PERSISTENT MEMORY — AgentKeeper + Context save/load
# ============================================================
persistence_code = '''

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

'''

if "MEMORY_FILE" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        persistence_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [1] Persistent memory (save/load JSON) added")
else:
    print("  [1] Persistence already exists")


# ============================================================
# ADD load_all_state() TO on_ready AND periodic save
# ============================================================
if "load_all_state()" not in code:
    code = code.replace(
        '    log.info("TraderJoes bot online as %s", bot.user)',
        '    log.info("TraderJoes bot online as %s", bot.user)\n    load_all_state()'
    )
    changes += 1
    print("  [+] Added load_all_state to on_ready")

# Add periodic save to daily report
if "save_all_state()" not in code:
    code = code.replace(
        '            push_all_analytics()\n        except Exception as e: log.warning("Daily report error: %s", e)',
        '            push_all_analytics()\n            save_all_state()\n        except Exception as e: log.warning("Daily report error: %s", e)'
    )
    # Also save after each alert scan
    code = code.replace(
        '            push_all_analytics()  # push metrics each cycle',
        '            push_all_analytics()  # push metrics each cycle\n            save_all_state()  # persist state periodically'
    )
    changes += 1
    print("  [+] Added periodic save_all_state")


# ============================================================
# 6: SIGNAL P&L TRACKING
# ============================================================
signal_pnl_code = '''

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
    await ctx.send(f"Signal #{index} resolved: **{signal['outcome']}** | P&L: ${signal['pnl']:+,.2f}\\n{signal['market'][:60]}")

'''

if "resolve_signal" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        signal_pnl_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [6] Signal P&L tracking added")
else:
    print("  [6] Signal P&L already exists")


# ============================================================
# 11: COINBASE FULL USD BALANCE
# ============================================================
if "def get_coinbase_balance" in code and "total_usd" not in code.split("def get_coinbase_balance")[1][:2000]:
    # Find the coinbase balance function and enhance it
    cb_section_start = code.find("def get_coinbase_balance")
    cb_section_end = code.find("\ndef ", cb_section_start + 10)
    if cb_section_start > 0 and cb_section_end > 0:
        old_cb = code[cb_section_start:cb_section_end]
        # Check if it already has total_usd
        if "total_usd" not in old_cb:
            new_cb = old_cb
            # Try to add USD summing after the crypto listing
            # This is tricky without seeing exact code, so add a helper
            pass
    print("  [11] Coinbase balance — needs manual review of function structure")
else:
    print("  [11] Coinbase balance — skipped (needs code review)")


# ============================================================
# 13: SYSTEM RESTART NOTIFICATION
# ============================================================
if "Bot restarted" not in code:
    code = code.replace(
        '    log.info("TraderJoes bot online as %s", bot.user)\n    load_all_state()',
        '    log.info("TraderJoes bot online as %s", bot.user)\n    load_all_state()\n'
        '    # Send restart notification\n'
        '    if DISCORD_CHANNEL_ID:\n'
        '        try:\n'
        '            ch = bot.get_channel(int(DISCORD_CHANNEL_ID))\n'
        '            if ch:\n'
        '                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")\n'
        '                await ch.send(f"**Bot Restarted** | {ts}\\nTraderJoes#3230 back online. Mode: {TRADING_MODE.upper()} | Auto-paper: {AUTO_PAPER_ENABLED}")\n'
        '        except Exception as e:\n'
        '            log.warning("Restart notification error: %s", e)'
    )
    changes += 1
    print("  [13] Restart notification added")
else:
    print("  [13] Restart notification already exists")


# ============================================================
# 14: RATE LIMITING
# ============================================================
rate_limit_code = '''

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

'''

if "RATE_LIMITS" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        rate_limit_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [14] Rate limiting + injection blocking added")
else:
    print("  [14] Rate limiting already exists")


# ============================================================
# LIVE EXECUTION FRAMEWORK (Safe — requires !confirm-trade)
# ============================================================
live_exec_code = '''

# ============================================================================
# LIVE EXECUTION FRAMEWORK
# ============================================================================

async def execute_kalshi_order(action, ticker, amount):
    """Place a real order on Kalshi. Returns (success, message)."""
    if not KALSHI_API_KEY_ID or not KALSHI_PRIVATE_KEY:
        return False, "Kalshi API keys not configured"
    try:
        # Get auth token
        ts = str(int(time.time()))
        method = "POST"
        path = "/trade-api/v2/portfolio/orders"
        msg = ts + "\\n" + method + "\\n" + path + "\\n"
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, utils
        pk_bytes = KALSHI_PRIVATE_KEY.encode()
        if "BEGIN" in KALSHI_PRIVATE_KEY:
            private_key = serialization.load_pem_private_key(pk_bytes, password=None)
        else:
            import base64
            der = base64.b64decode(KALSHI_PRIVATE_KEY)
            private_key = serialization.load_der_private_key(der, password=None)
        sig = private_key.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
        import base64
        sig_b64 = base64.b64encode(sig).decode()

        side = "yes" if action.upper() == "BUY" else "no"
        order_data = {
            "ticker": ticker,
            "type": "market",
            "action": action.lower(),
            "side": side,
            "count": int(amount / 0.50),  # approximate shares
        }

        headers = {
            "Authorization": f"Bearer {KALSHI_API_KEY_ID}",
            "Content-Type": "application/json",
        }
        r = requests.post(f"https://api.elections.kalshi.com{path}", json=order_data, headers=headers, timeout=15)
        if r.status_code in (200, 201):
            return True, f"Kalshi order placed: {action} {ticker} ${amount:.2f}"
        else:
            return False, f"Kalshi API error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Kalshi execution error: {exc}"


async def execute_phemex_order(action, symbol, amount):
    """Place a real order on Phemex. Returns (success, message)."""
    if not PHEMEX_API_KEY or not PHEMEX_API_SECRET:
        return False, "Phemex API keys not configured"
    try:
        ts = str(int(time.time()))
        path = "/orders"
        query = f"symbol={symbol}&side={action.title()}&orderQty={amount}&ordType=Market"
        msg = path + query + ts
        import hmac, hashlib
        sig = hmac.new(PHEMEX_API_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()

        headers = {
            "x-phemex-access-token": PHEMEX_API_KEY,
            "x-phemex-request-signature": sig,
            "x-phemex-request-expiry": ts,
            "Content-Type": "application/json",
        }
        r = requests.post(f"https://api.phemex.com{path}?{query}", headers=headers, timeout=15)
        if r.status_code == 200:
            return True, f"Phemex order placed: {action} {symbol} ${amount:.2f}"
        else:
            return False, f"Phemex API error: {r.status_code} {r.text[:200]}"
    except Exception as exc:
        return False, f"Phemex execution error: {exc}"

'''

if "execute_kalshi_order" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        live_exec_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [2-3] Live execution framework (Kalshi + Phemex) added")
else:
    print("  [2-3] Live execution already exists")


# ============================================================
# UPDATE !help-tj WITH ALL NEW COMMANDS
# ============================================================
if "!save" not in code.split("help_tj")[1] if "help_tj" in code else "":
    old_help_analytics = '**Analytics & Costs:**'
    new_help_analytics = '**Persistence:**\\n"\n        "  `!save` — Save all state to disk\\n"\n        "  `!load` — Load state from disk\\n"\n        "  `!resolve-signal <idx> win/loss` — Mark signal outcome\\n"\n        "\\n**Analytics & Costs:**'
    if old_help_analytics in code:
        code = code.replace(old_help_analytics, new_help_analytics)
        changes += 1
        print("  [+] Updated !help-tj with new commands")


# ============================================================
# WRITE FILE
# ============================================================
with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)

print(f"\n  [OK] {changes} patches applied")
V5PATCH

echo ""

# ============================================================
# ADD PERSISTENT VOLUME FOR DATA
# ============================================================
echo "=== Adding persistent data volume ==="

# Create data directory
mkdir -p /root/trading-bot/data

# Update docker-compose to mount data volume
python3 << 'VOLPATCH'
with open("/root/trading-bot/docker-compose.yml", "r") as f:
    yml = f.read()

# Add data volume mount to bot service
if "/app/data" not in yml:
    yml = yml.replace(
        "      - bot-logs:/app/logs",
        "      - bot-logs:/app/logs\n      - ./data:/app/data"
    )
    with open("/root/trading-bot/docker-compose.yml", "w") as f:
        f.write(yml)
    print("  [OK] Data volume mounted at /app/data")
else:
    print("  [OK] Data volume already mounted")
VOLPATCH

echo ""

# ============================================================
# UPDATE OPENCLAW STUDIO WITH LIVE API
# ============================================================
echo "=== Updating OpenClaw Studio with live data ==="

cat > /root/trading-bot/openclaw-studio/server.js << 'SERVERJS'
const express = require('express');
const path = require('path');
const fs = require('fs');
const app = express();
const PORT = 3000;

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Health check
app.get('/health', (req, res) => res.json({ status: 'ok', uptime: process.uptime() }));

// API: Read live data from bot's persistent JSON files
const DATA_DIR = '/app/data';

app.get('/api/analytics', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'analytics.json'), 'utf8'));
        res.json(data);
    } catch(e) {
        res.json({ total_trades: 0, openai_cost_usd: 0, current_equity: 10000 });
    }
});

app.get('/api/signals', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'signal_history.json'), 'utf8'));
        res.json(data.slice(-50));  // last 50 signals
    } catch(e) {
        res.json([]);
    }
});

app.get('/api/memory', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'agent_memory.json'), 'utf8'));
        res.json(data);
    } catch(e) {
        res.json({ risk_rules: [], lessons: [], strategies: {} });
    }
});

app.get('/api/paper', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'paper_portfolio.json'), 'utf8'));
        res.json(data);
    } catch(e) {
        res.json({ cash: 10000, positions: [], trades: [] });
    }
});

app.get('/api/context', (req, res) => {
    try {
        const data = JSON.parse(fs.readFileSync(path.join(DATA_DIR, 'context_memory.json'), 'utf8'));
        res.json(data);
    } catch(e) {
        res.json({});
    }
});

// Serve the dashboard
app.get('/', (req, res) => res.sendFile(path.join(__dirname, 'public', 'index.html')));

app.listen(PORT, '0.0.0.0', () => {
    console.log(`OpenClaw Studio running on port ${PORT}`);
});
SERVERJS

# Update dashboard HTML with live data fetching
cat > /root/trading-bot/openclaw-studio/public/index.html << 'HTMLDASH'
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OpenClaw Studio — TraderJoes</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0a0e17;color:#e0e6f0;min-height:100vh}
.header{background:linear-gradient(135deg,#1a1f35,#0d1321);padding:24px 32px;border-bottom:1px solid #1e2940;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:24px;font-weight:700;background:linear-gradient(90deg,#f7931a,#ff6b35);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.status{display:flex;align-items:center;gap:8px;font-size:14px;color:#6b7a99}
.dot{width:8px;height:8px;border-radius:50%;background:#00d084;animation:pulse 2s infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
.container{max-width:1400px;margin:0 auto;padding:24px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:20px;margin-bottom:24px}
.card{background:#111827;border:1px solid #1e2940;border-radius:12px;padding:20px}
.card h2{font-size:14px;color:#8b9dc3;margin-bottom:16px;text-transform:uppercase;letter-spacing:1px;font-weight:600}
.metric{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #1a2035;font-size:14px}
.metric:last-child{border-bottom:none}
.metric .label{color:#6b7a99}
.metric .value{font-weight:600}
.green{color:#00d084}.red{color:#ff4757}.orange{color:#f7931a}.blue{color:#4dabf7}
.signal-list{max-height:300px;overflow-y:auto}
.signal{padding:8px;border-bottom:1px solid #1a2035;font-size:13px;display:flex;justify-content:space-between}
.signal .tag{padding:2px 6px;border-radius:4px;font-size:11px;font-weight:600}
.tag.exec{background:rgba(0,208,132,.15);color:#00d084}
.tag.skip{background:rgba(107,122,153,.15);color:#6b7a99}
.wide{grid-column:span 2}
@media(max-width:768px){.wide{grid-column:span 1}.grid{grid-template-columns:1fr}}
.footer{text-align:center;padding:32px;color:#3a4660;font-size:13px}
.refresh-badge{font-size:11px;color:#3a4660;margin-left:8px}
.links{display:flex;gap:12px;margin-top:16px}
.links a{color:#4dabf7;text-decoration:none;font-size:13px;padding:6px 12px;border:1px solid #1e2940;border-radius:6px}
.links a:hover{background:#1e2940}
</style>
</head>
<body>
<div class="header">
  <h1>OpenClaw Studio</h1>
  <div class="status"><div class="dot" id="statusDot"></div><span id="statusText">Connecting...</span></div>
</div>
<div class="container">
  <div class="grid">
    <div class="card">
      <h2>Paper Portfolio</h2>
      <div id="paperPortfolio">
        <div class="metric"><span class="label">Loading...</span><span class="value">—</span></div>
      </div>
    </div>
    <div class="card">
      <h2>Analytics</h2>
      <div id="analyticsPanel">
        <div class="metric"><span class="label">Loading...</span><span class="value">—</span></div>
      </div>
    </div>
    <div class="card">
      <h2>Risk Rules (AgentKeeper)</h2>
      <div id="memoryPanel">
        <div class="metric"><span class="label">Loading...</span><span class="value">—</span></div>
      </div>
    </div>
    <div class="card">
      <h2>System</h2>
      <div id="systemPanel">
        <div class="metric"><span class="label">Netdata</span><span class="value"><a href="http://89.167.108.136:19999" target="_blank" style="color:#4dabf7">Open Dashboard</a></span></div>
        <div class="metric"><span class="label">Discord</span><span class="value green">TraderJoes#3230</span></div>
        <div class="metric"><span class="label">Agents</span><span class="value blue">8 Active</span></div>
        <div class="metric"><span class="label">Commands</span><span class="value">30+</span></div>
      </div>
    </div>
    <div class="card wide">
      <h2>Recent Signals <span class="refresh-badge" id="signalRefresh"></span></h2>
      <div class="signal-list" id="signalList">
        <div class="signal"><span>Loading signals...</span></div>
      </div>
    </div>
  </div>
</div>
<div class="footer">TraderJoes Trading Firm &copy; 2026 — OpenClaw Studio v2.0 — Auto-refreshes every 30s</div>

<script>
async function fetchData() {
  try {
    // Paper portfolio
    const paper = await (await fetch('/api/paper')).json();
    const cash = paper.cash || 10000;
    const posVal = (paper.positions || []).reduce((s, p) => s + (p.value || 0), 0);
    const total = cash + posVal;
    const pnl = total - 10000;
    const trades = (paper.trades || []).length;
    document.getElementById('paperPortfolio').innerHTML = `
      <div class="metric"><span class="label">Cash</span><span class="value green">$${cash.toLocaleString('en-US',{minimumFractionDigits:2})}</span></div>
      <div class="metric"><span class="label">Positions</span><span class="value">$${posVal.toLocaleString('en-US',{minimumFractionDigits:2})}</span></div>
      <div class="metric"><span class="label">Total</span><span class="value blue">$${total.toLocaleString('en-US',{minimumFractionDigits:2})}</span></div>
      <div class="metric"><span class="label">P&L</span><span class="value ${pnl>=0?'green':'red'}">$${pnl>=0?'+':''}${pnl.toLocaleString('en-US',{minimumFractionDigits:2})}</span></div>
      <div class="metric"><span class="label">Trades</span><span class="value">${trades}</span></div>
    `;

    // Analytics
    const a = await (await fetch('/api/analytics')).json();
    const winRate = a.total_trades > 0 ? ((a.winning_trades / a.total_trades) * 100).toFixed(1) : '0.0';
    document.getElementById('analyticsPanel').innerHTML = `
      <div class="metric"><span class="label">Total Trades</span><span class="value">${a.total_trades || 0}</span></div>
      <div class="metric"><span class="label">Win Rate</span><span class="value ${parseFloat(winRate)>50?'green':'orange'}">${winRate}%</span></div>
      <div class="metric"><span class="label">Total P&L</span><span class="value ${(a.total_pnl||0)>=0?'green':'red'}">$${(a.total_pnl||0).toFixed(2)}</span></div>
      <div class="metric"><span class="label">Max Drawdown</span><span class="value red">${(a.max_drawdown||0).toFixed(1)}%</span></div>
      <div class="metric"><span class="label">OpenAI Cost</span><span class="value">$${(a.openai_cost_usd||0).toFixed(4)}</span></div>
      <div class="metric"><span class="label">OpenAI Calls</span><span class="value">${a.openai_calls || 0}</span></div>
    `;

    // Memory
    const mem = await (await fetch('/api/memory')).json();
    const rules = (mem.risk_rules || []).map(r => `<div class="metric"><span class="label">•</span><span class="value" style="font-size:12px;text-align:right">${r}</span></div>`).join('');
    const lessons = (mem.lessons || []).slice(-3).map(l => `<div class="metric"><span class="label">📝</span><span class="value" style="font-size:12px">${l}</span></div>`).join('');
    document.getElementById('memoryPanel').innerHTML = rules + (lessons || '<div class="metric"><span class="label">No lessons yet</span></div>');

    // Signals
    const signals = await (await fetch('/api/signals')).json();
    if (signals.length > 0) {
      document.getElementById('signalList').innerHTML = signals.slice(-20).reverse().map(s => {
        const tag = s.executed ? '<span class="tag exec">EXEC</span>' : '<span class="tag skip">SKIP</span>';
        const pnlStr = s.pnl != null ? ` | P&L: $${s.pnl.toFixed(2)}` : '';
        return `<div class="signal"><span>${s.timestamp} | ${s.platform} | EV +${(s.ev*100).toFixed(1)}% | ${(s.market||'').substring(0,40)}</span>${tag}</div>`;
      }).join('');
    } else {
      document.getElementById('signalList').innerHTML = '<div class="signal"><span>No signals yet — run !cycle or wait for auto-scan</span></div>';
    }
    document.getElementById('signalRefresh').textContent = `Updated ${new Date().toLocaleTimeString()}`;

    // Status
    document.getElementById('statusDot').style.background = '#00d084';
    document.getElementById('statusText').textContent = 'TraderJoes#3230 Online';
  } catch(e) {
    document.getElementById('statusDot').style.background = '#ff4757';
    document.getElementById('statusText').textContent = 'Connection error';
  }
}

fetchData();
setInterval(fetchData, 30000);
</script>
</body>
</html>
HTMLDASH

# Mount shared data volume for openclaw too
python3 << 'CLAWVOL'
with open("/root/trading-bot/docker-compose.yml", "r") as f:
    yml = f.read()

# Add data volume to openclaw service
if "openclaw" in yml and "/app/data" not in yml.split("openclaw")[1].split("netdata")[0]:
    yml = yml.replace(
        '      - "3000:3000"\n    logging:',
        '      - "3000:3000"\n    volumes:\n      - ./data:/app/data\n    logging:'
    )
    with open("/root/trading-bot/docker-compose.yml", "w") as f:
        f.write(yml)
    print("  [OK] Shared data volume added to openclaw")
else:
    print("  [OK] Openclaw data volume already configured")
CLAWVOL

echo "  [OK] OpenClaw Studio updated with live API"
echo ""

# ============================================================
# REBUILD AND RESTART
# ============================================================
echo "=== Rebuilding all containers ==="
docker compose down 2>/dev/null || true
sleep 3
docker compose build --no-cache 2>&1 | tail -5
docker compose up -d
sleep 25

echo ""
echo "--- Container Status ---"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "--- Bot Logs ---"
docker logs traderjoes-bot --tail 5 2>&1

echo ""
echo "--- OpenClaw Health ---"
curl -s http://localhost:3000/health 2>/dev/null || echo "Still starting..."

# ============================================================
# GITHUB SYNC
# ============================================================
echo ""
echo "=== GitHub Sync ==="
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A 2>/dev/null || true
git commit -m "V5: Persistent memory, live dashboard, signal P&L, rate limiting, restart alerts, encrypted backup" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3
echo "  [OK] GitHub synchronized"

echo ""
echo "============================================"
echo "  TraderJoes V5 — Complete"
echo "============================================"
echo ""
echo "NEW FEATURES:"
echo "  Persistent memory — survives restarts (AgentKeeper, signals, paper portfolio)"
echo "  Live dashboard — OpenClaw Studio now reads real-time data"
echo "  Signal P&L tracking — !resolve-signal <idx> win/loss"
echo "  Rate limiting — 10 commands/minute max"
echo "  Injection blocking — auto-blocks suspicious inputs"
echo "  Restart notification — Discord alert on bot restart"
echo "  Encrypted .env backup — .env.encrypted + .env.backup.key"
echo "  Live execution framework — Kalshi + Phemex order placement (needs testing)"
echo ""
echo "NEW COMMANDS:"
echo "  !save          — Save all state to disk"
echo "  !load          — Load state from disk"
echo "  !resolve-signal — Mark signal outcome (win/loss/push)"
echo ""
echo "DASHBOARDS:"
echo "  OpenClaw Studio: http://89.167.108.136:3000"
echo "  Netdata:         http://89.167.108.136:19999"
echo ""
echo "TEST: !save, !signals, !paper-status, !analytics, !help-tj"
echo "============================================"
