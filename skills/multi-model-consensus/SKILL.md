---
name: multi-model-consensus
description: Query multiple AI models (Claude Haiku, GPT-4o-mini, GPT-4o) in parallel to get independent probability estimates for prediction market questions. Synthesizes agreement into a conviction score with EV analysis. Use for high-stakes market decisions.
metadata:
  openclaw:
    emoji: "🤖"
    requires:
      env:
        - OPENAI_API_KEY
        - ANTHROPIC_API_KEY
      bins:
        - python3
---

# Multi-Model Consensus

Query Claude + GPT models in parallel for independent probability estimates, then synthesize into a consensus signal.

## When to Use

Use this skill when the user asks to:
- Get consensus from multiple AI models on a prediction market question
- Cross-validate a probability estimate across models
- Run a "consensus check" or "multi-model analysis"
- Commands like "consensus", "multimodel", "ask all models"

## How It Works

1. Sends the same question to Claude Haiku, GPT-4o-mini, and GPT-4o in parallel
2. Each model independently estimates probability and direction (BUY_YES/BUY_NO/HOLD)
3. Synthesizes: agreement score, conviction (1-10), average probability, EV spread
4. Shows per-model reasoning and key factors

## Usage

```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/multi-model-consensus/scripts/consensus.py "<question>" <market_price>
```

## Example

User: "Get consensus on 'Will Bitcoin reach $100K by end of 2026?' Market price 55 cents"

```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/multi-model-consensus/scripts/consensus.py "Will Bitcoin reach $100K by end of 2026?" 0.55
```

Present the full output to the user.
