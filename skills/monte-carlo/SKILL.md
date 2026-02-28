---
name: monte-carlo
description: Run 10000-path Monte Carlo simulations for probability estimation.
metadata:
  openclaw:
    emoji: "dice"
    requires:
      env:
        - OPENAI_API_KEY
      bins:
        - python3
---
# Monte Carlo Simulator
Run Monte Carlo simulations for prediction market analysis.
## Usage
```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/monte-carlo/scripts/monte_carlo.py "<question>" <market_price> <n_sims>
```
Present the full output to the user.
