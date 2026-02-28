---
name: liquidity-scan
description: Scan Polymarket order books for liquidity depth and slippage.
metadata:
  openclaw:
    emoji: "water"
    requires:
      bins:
        - python3
---
# Liquidity Scanner
Analyze order book depth for prediction markets.
## Usage
```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/liquidity-scan/scripts/liquidity.py "<slug>" [position_size_usd]
```
Present the full output to the user.
