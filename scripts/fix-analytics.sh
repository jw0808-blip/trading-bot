#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

# Check if ANALYTICS dict exists
if "ANALYTICS" in code and "def push_all_analytics" not in code:
    # Find where ANALYTICS is defined and add the functions after it
    analytics_funcs = '''

def push_netdata_metric(key, value):
    """Push a metric to Netdata via StatsD (UDP)."""
    try:
        import socket as _socket
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
    # Insert right after the ANALYTICS dict definition
    insert_point = code.find('"openai_monthly_limit": 10.0,')
    if insert_point > 0:
        # Find the closing brace of the dict
        brace = code.find("}", insert_point)
        if brace > 0:
            code = code[:brace+1] + analytics_funcs + code[brace+1:]
            print("Restored push_all_analytics, push_netdata_metric, track_openai_usage")
    else:
        print("Could not find ANALYTICS dict end")
else:
    if "def push_all_analytics" in code:
        print("Functions already exist")
    else:
        print("ANALYTICS not found in code")

with open("main.py", "w") as f:
    f.write(code)
FIX

echo "Rebuilding bot..."
docker compose down traderjoes-bot 2>/dev/null || docker compose stop traderjoes-bot
docker compose build traderjoes-bot 2>&1 | tail -3
docker compose up -d
sleep 20

echo ""
docker ps --format "table {{.Names}}\t{{.Status}}"
echo ""
docker logs traderjoes-bot --tail 5 2>&1
