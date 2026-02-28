---
name: monte-carlo
description: Run Monte Carlo probability simulations for prediction market questions. Extracts key variables using GPT-4o-mini, runs 10,000 beta-distribution simulations, computes fair value, EV spread, Kelly criterion sizing, and confidence intervals. Use when analyzing prediction market opportunities.
metadata:
  openclaw:
    emoji: "🎲"
    requires:
      env:
        - OPENAI_API_KEY
      bins:
        - python3
---

# Monte Carlo Simulator

Run 10,000-path Monte Carlo simulations for prediction market probability estimation.

## When to Use

Use this skill when the user asks to:
- Analyze a prediction market question with Monte Carlo simulation
- Estimate the fair probability of an event
- Calculate expected value (EV) of a prediction market position
- Run simulations on a market question
- Commands like "montecarlo", "simulate", "mc analysis"

## How It Works

1. Uses GPT-4o-mini to extract 3-6 key variables (base rates, volatility, weights)
2. Runs 10,000 beta-distribution Monte Carlo simulations
3. Computes: median fair value, 95% confidence intervals, EV spread vs market
4. Calculates Kelly criterion optimal position size
5. Generates conviction signal (STRONG BUY / BUY / HOLD / SELL)

## Usage

When the user provides a prediction market question and optionally a current market price:

```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/monte-carlo/scripts/monte_carlo.py "<question>" <market_price> <n_sims>
```

- `question`: The prediction market question (required)
- `market_price`: Current YES price as decimal, e.g. 0.031 for 3.1¢ (default: 0.05)
- `n_sims`: Number of simulations (default: 10000)

## Example

User: "Run a Monte Carlo on 'Will US strike Iran by March 2026?' Current price is 3.1 cents"

```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/monte-carlo/scripts/monte_carlo.py "Will US strike Iran by March 2026?" 0.031 10000
```

## Output Format

The script outputs formatted text with:
- Fair Value with 95% CI
- Market Price comparison
- EV Spread (absolute and percentage)
- Kelly criterion position sizing
- Signal strength (STRONG BUY / BUY / HOLD / SELL)
- Key variables breakdown with base rates and weights
- LLM reasoning summary

Present the full output to the user.
