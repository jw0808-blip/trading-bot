---
name: historian
description: Find historical parallels and base rates for prediction market events. Analyzes similar past events to estimate probabilities using historical data. Use when you need historical context for a prediction.
metadata:
  openclaw:
    emoji: "📚"
    requires:
      env:
        - ANTHROPIC_API_KEY
      bins:
        - python3
---

# Historical Parallels Analyzer

Find historical precedents and base rates for prediction market questions.

## When to Use

Use when the user asks to:
- Find historical parallels for a prediction market question
- Get base rates for a type of event
- Analyze "has something like this happened before?"
- Commands like "history", "historian", "base rate", "precedent"

## Usage

```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/historian/scripts/historian.py "<question>" <market_price>
```

Present the full output to the user.
