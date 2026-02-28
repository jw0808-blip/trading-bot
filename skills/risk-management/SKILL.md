---
name: risk-management
description: Analyze portfolio risk, calculate optimal position sizes using Kelly criterion, assess correlation risk, and recommend stop-loss levels. Use before making trade decisions to manage bankroll.
metadata:
  openclaw:
    emoji: "🛡️"
    requires:
      env:
        - OPENAI_API_KEY
      bins:
        - python3
---

# Risk Manager

Portfolio risk analysis, Kelly sizing, and position recommendations.

## When to Use

Use when the user asks to:
- Calculate optimal position size for a trade
- Assess portfolio risk or concentration
- Get Kelly criterion sizing
- Check risk limits before trading
- Commands like "risk", "position size", "kelly", "risk check"

## Usage

```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/risk-management/scripts/risk.py "<question>" <fair_prob> <market_price> <bankroll>
```

- `question`: What market are we sizing for
- `fair_prob`: Your estimated fair probability (0.0-1.0)
- `market_price`: Current market price (0.0-1.0)
- `bankroll`: Total available bankroll in USD

Present the full output to the user.
