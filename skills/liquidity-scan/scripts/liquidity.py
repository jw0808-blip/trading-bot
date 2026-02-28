#!/usr/bin/env python3
"""TraderJoes EchoEdge — Liquidity Scanner. Checks Polymarket order book depth."""

import sys, json, requests

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"


def get_market_by_slug(slug):
    """Find market by slug via Gamma API."""
    try:
        resp = requests.get(f"{GAMMA_BASE}/markets", params={"slug": slug}, timeout=10)
        markets = resp.json()
        if markets and len(markets) > 0:
            return markets[0]
    except:
        pass
    return None


def get_orderbook(token_id):
    """Get CLOB order book for a token."""
    try:
        resp = requests.get(f"{CLOB_BASE}/book", params={"token_id": token_id}, timeout=10)
        return resp.json()
    except:
        return {"bids": [], "asks": []}


def analyze_depth(book, position_size=100):
    """Analyze order book depth and slippage."""
    bids = book.get("bids", [])
    asks = book.get("asks", [])
    bids = sorted(bids, key=lambda x: float(x["price"]), reverse=True)
    asks = sorted(asks, key=lambda x: float(x["price"]))
    best_bid = float(bids[0]["price"]) if bids else 0
    best_ask = float(asks[0]["price"]) if asks else 1
    spread = best_ask - best_bid
    spread_pct = (spread / best_ask * 100) if best_ask > 0 else 0
    bid_depth = sum(float(b["price"]) * float(b["size"]) for b in bids)
    ask_depth = sum(float(a["price"]) * float(a["size"]) for a in asks)
    slippage = 0
    remaining = position_size
    weighted_price = 0
    for ask in asks:
        price = float(ask["price"])
        size_usd = price * float(ask["size"])
        if size_usd >= remaining:
            weighted_price += price * remaining
            remaining = 0
            break
        else:
            weighted_price += price * size_usd
            remaining -= size_usd
    if remaining > 0:
        slippage_warning = f"Warning: Only ${position_size - remaining:.0f} of ${position_size} fillable"
    else:
        avg_fill = weighted_price / position_size if position_size > 0 else best_ask
        slippage = (avg_fill - best_ask) / best_ask * 100 if best_ask > 0 else 0
        slippage_warning = ""
    total = bid_depth + ask_depth
    if total > 50000: grade = "A (Excellent)"
    elif total > 10000: grade = "B (Good)"
    elif total > 2000: grade = "C (Fair)"
    elif total > 500: grade = "D (Thin)"
    else: grade = "F (Illiquid)"
    return {
        "best_bid": best_bid, "best_ask": best_ask,
        "spread": spread, "spread_pct": spread_pct,
        "bid_depth_usd": bid_depth, "ask_depth_usd": ask_depth,
        "total_depth_usd": total, "grade": grade,
        "slippage_pct": slippage, "slippage_warning": slippage_warning,
        "bid_levels": len(bids), "ask_levels": len(asks),
    }


def format_output(market_name, analysis, position_size):
    a = analysis
    return f"""Liquidity Scan
Market: {market_name}
Order Book:
  Best Bid: ${a['best_bid']:.3f} | Best Ask: ${a['best_ask']:.3f}
  Spread: ${a['spread']:.3f} ({a['spread_pct']:.1f}%)
  Bid Levels: {a['bid_levels']} | Ask Levels: {a['ask_levels']}
Depth:
  Bid Side: ${a['bid_depth_usd']:,.0f}
  Ask Side: ${a['ask_depth_usd']:,.0f}
  Total: ${a['total_depth_usd']:,.0f}
Grade: {a['grade']}
Slippage (${position_size} buy):
  Est. Slippage: {a['slippage_pct']:.2f}%
  {a['slippage_warning']}"""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python liquidity.py '<slug_or_token_id>' [position_size_usd]"); sys.exit(1)
    slug = sys.argv[1]
    size = float(sys.argv[2]) if len(sys.argv) > 2 else 100
    market = get_market_by_slug(slug)
    if market:
        token_id = market.get("clobTokenIds", [""])[0] if isinstance(market.get("clobTokenIds"), list) else slug
        name = market.get("question", slug)
    else:
        token_id = slug
        name = slug
    book = get_orderbook(token_id)
    analysis = analyze_depth(book, size)
    print(format_output(name, analysis, size))
