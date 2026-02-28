---
name: multi-model-consensus
description: Query Claude + GPT models in parallel for consensus probability estimates.
metadata:
  openclaw:
    emoji: "robot"
    requires:
      env:
        - OPENAI_API_KEY
        - ANTHROPIC_API_KEY
      bins:
        - python3
---
# Multi-Model Consensus
Query multiple AI models for independent probability estimates.
## Usage
```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/multi-model-consensus/scripts/consensus.py "<question>" <market_price>
```
Present the full output to the user.
