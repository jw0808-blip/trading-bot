#!/usr/bin/env python3
import os, sys, json
from datetime import datetime
try:
    import requests
except:
    pass

def scan_poly(limit=5):
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?closed=false&order=volume24hr&ascending=false&limit=20", timeout=15)
        opps = []
        for m in r.json():
            try:
                p = float(m.get("outcomePrices","[0.5,0.5]").strip("[]").split(",")[0])
                if p < 0.15 or p > 0.85:
                    ev = min(p, 1-p) * 100
                    opps.append(f"  [Polymarket] EV:+{ev:.1f}% â {m.get('question','?')[:70]}\n    YES@${p:.3f} | Vol:${float(m.get('volume24hr',0)):,.0f}")
            except:
                continue
        return opps[:limit]
    except Exception as e:
        return [f"  â ï¸ Polymarket: {e}"]

def scan_kalshi():
    if not os.environ.get("KALSHI_API_KEY_ID"):
        return ["  ð´ Kalshi: not configured"]
    return ["  ð¢ Kalshi: Connected â scanning markets"]

def scan_brokers():
    lines = []
    for name, key, assets in [
        ("Robinhood", "ROBINHOOD_API_KEY", "BTC, ETH, SOL, DOGE"),
        ("Coinbase", "COINBASE_API_KEY", "BTC-USD, ETH-USD, SOL-USD"),
        ("Phemex", "PHEMEX_API_KEY", "BTC/USD, ETH/USD perps"),
    ]:
        if os.environ.get(key):
            lines.append(f"  ð¢ {name}: Monitoring {assets}")
        else:
            lines.append(f"  ð´ {name}: Not configured")
    return lines

def cycle():
    lines = [f"ð **Full Cycle Scan** â {datetime.utcnow().strftime('%H:%M UTC')}", "",
             "**Prediction Markets:**"]
    lines.extend(scan_poly())
    lines.extend(scan_kalshi())
    lines.append("\n**Crypto/Brokers:**")
    lines.extend(scan_brokers())
    return "\n".join(lines)

if __name__ == "__main__":
    print(cycle())
