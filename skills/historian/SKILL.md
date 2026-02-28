---
name: historian
description: Find historical parallels and base rates for prediction market events.
metadata:
  openclaw:
    emoji: "books"
    requires:
      env:
        - ANTHROPIC_API_KEY
      bins:
        - python3
---
# Historical Parallels Analyzer
Find historical precedents and base rates.
## Usage
```bash
source /root/traderjoes-skills/.venv/bin/activate
python3 /root/.openclaw/skills/historian/scripts/historian.py "<question>" <market_price>
```
Present the full output to the user.
