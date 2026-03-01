#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    lines = f.readlines()

# Find line with ALERT_CONFIG = {
insert_line = None
for i, line in enumerate(lines):
    if line.strip() == "ALERT_CONFIG = {":
        insert_line = i
        break

if insert_line is None:
    print("ERROR: Could not find ALERT_CONFIG")
    exit(1)

# Check if ANALYTICS dict already exists before this point
code_before = "".join(lines[:insert_line])
if "ANALYTICS = {" in code_before:
    print("ANALYTICS dict already exists before ALERT_CONFIG")
else:
    # Insert ANALYTICS + functions before ALERT_CONFIG
    new_block = '''
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


def track_openai_usage(tokens_used, model="gpt-4o-mini"):
    """Track OpenAI API usage and costs."""
    ANALYTICS["openai_calls"] += 1
    ANALYTICS["openai_tokens"] += tokens_used
    cost_per_1k = 0.0004 if "mini" in model else 0.003
    cost = (tokens_used / 1000) * cost_per_1k
    ANALYTICS["openai_cost_usd"] += cost


'''
    new_lines = new_block.split("\n")
    for j, nl in enumerate(new_lines):
        lines.insert(insert_line + j, nl + "\n")
    print(f"Inserted ANALYTICS + functions at line {insert_line}")

with open("main.py", "w") as f:
    f.writelines(lines)
print("Done")
FIX

echo "Rebuilding bot..."
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d traderjoes-bot
sleep 20

echo ""
docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""
docker logs traderjoes-bot --tail 5 2>&1

# Quick GitHub sync
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A && git commit -m "Fix: restore ANALYTICS dict and push functions" --allow-empty 2>/dev/null
git push -u origin main --force 2>&1 | tail -2
