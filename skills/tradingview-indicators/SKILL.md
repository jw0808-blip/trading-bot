---
name: tradingview-indicators
description: RSI, MACD, Bollinger Bands for crypto.
metadata:
  openclaw:
    emoji: "chart"
    requires:
      bins:
        - python3
---
# Technical Indicators
Calculate technical analysis indicators for crypto.
## Usage
```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/tradingview-indicators/scripts/technicals.py <coin_id> [days]
```
Present the full output to the user.
