---
name: tradingview-indicators
description: Calculate technical indicators (RSI, MACD, Bollinger Bands, SMA, EMA) for crypto assets relevant to prediction markets. Fetches price data from CoinGecko and computes technical signals.
metadata:
  openclaw:
    emoji: "📊"
    requires:
      bins:
        - python3
---

# TradingView-Style Technical Indicators

Calculate technical analysis indicators for crypto assets.

## When to Use

Use when the user asks to:
- Get RSI, MACD, Bollinger Bands for Bitcoin, Ethereum, or other crypto
- Run technical analysis on a crypto asset
- Check if an asset is overbought/oversold
- Commands like "technicals", "indicators", "rsi", "macd", "ta"

## Usage

```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/tradingview-indicators/scripts/technicals.py <coin_id> [days]
```

- `coin_id`: CoinGecko coin ID (bitcoin, ethereum, solana, etc.)
- `days`: Lookback period in days (default: 90)

Present the full output to the user.
