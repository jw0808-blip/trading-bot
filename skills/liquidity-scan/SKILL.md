---
name: liquidity-scan
description: Scan Polymarket and Kalshi order books for liquidity depth, bid-ask spreads, and slippage estimates. Helps determine if a market has enough liquidity to enter/exit positions. Use before placing trades.
metadata:
  openclaw:
    emoji: "💧"
    requires:
      bins:
        - python3
---

# Liquidity Scanner

Analyze order book depth and trading conditions for prediction markets.

## When to Use

Use when the user asks to:
- Check liquidity on a Polymarket or Kalshi market
- Analyze order book depth or bid-ask spreads
- Estimate slippage for a position size
- Commands like "liquidity", "orderbook", "depth", "spread"

## Usage

```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/liquidity-scan/scripts/liquidity.py "<condition_id_or_slug>" [position_size_usd]
```

For Polymarket, provide the condition ID or market slug.
Position size defaults to $100 USD.

Present the full output to the user.
