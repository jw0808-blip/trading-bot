---
name: risk-management
description: Kelly criterion sizing, portfolio risk analysis.
metadata:
  openclaw:
    emoji: "shield"
    requires:
      bins:
        - python3
---
# Risk Manager
Kelly sizing and position recommendations.
## Usage
```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/risk-management/scripts/risk.py "<question>" <fair_prob> <market_price> <bankroll>
```
Present the full output to the user.
