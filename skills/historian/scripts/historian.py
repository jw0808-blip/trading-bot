#!/usr/bin/env python3
"""TraderJoes EchoEdge — Historical Parallels Analyzer. Uses Claude to find precedents."""

import os, sys, json
from anthropic import Anthropic

client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

PROMPT = """You are a historian and prediction market analyst. Given this prediction market question:

Question: "{question}"
Current market price (YES): {market_price:.1%}

Find 3-5 historical parallels or precedents. For each:
1. What happened (event, date, context)
2. The outcome (did the analogous event occur?)
3. How similar it is to the current question (1-10 scale)
4. Key differences from the current situation

Then provide:
- Historical base rate: Of the N most similar events, what % had this outcome?
- Your probability estimate based purely on historical precedent
- Whether the market price seems right, too high, or too low

Respond in this JSON format:
{{
  "parallels": [
    {{
      "event": "description",
      "date": "when",
      "outcome": "what happened",
      "similarity": 8,
      "key_difference": "..."
    }}
  ],
  "base_rate": 0.XX,
  "sample_size": N,
  "historical_probability": 0.XX,
  "market_assessment": "UNDERPRICED" or "OVERPRICED" or "FAIR",
  "reasoning": "2-3 sentences",
  "confidence": "HIGH" or "MEDIUM" or "LOW"
}}"""


def analyze(question, market_price=0.05):
    resp = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=1500,
        messages=[{"role": "user", "content": PROMPT.format(
            question=question, market_price=market_price)}])
    text = resp.content[0].text.strip()
    if text.startswith("```"): text = text.split("\n",1)[1].rsplit("```",1)[0]
    data = json.loads(text)
    
    lines = []
    for i, p in enumerate(data.get("parallels",[]), 1):
        lines.append(f"  {i}. **{p['event']}** ({p['date']})\n     Outcome: {p['outcome']}\n     Similarity: {'⭐'*p['similarity']}{'☆'*(10-p['similarity'])} {p['similarity']}/10\n     Diff: {p['key_difference']}")
    
    assess_emoji = {"UNDERPRICED":"🟢","OVERPRICED":"🔴","FAIR":"⚪"}.get(data.get("market_assessment",""),"❓")
    
    return f"""📚 **Historical Parallels Analysis**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**Q:** {question}

**Precedents Found:**
{chr(10).join(lines)}

**Historical Base Rate:** {data['base_rate']:.0%} (from {data['sample_size']} similar events)
**Historical Probability:** {data['historical_probability']:.1%}
**Market Price:** {market_price:.1%}
**Assessment:** {assess_emoji} **{data['market_assessment']}**
**Confidence:** {data['confidence']}

**Reasoning:** {data['reasoning']}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python historian.py '<question>' [market_price]"); sys.exit(1)
    print(analyze(sys.argv[1], float(sys.argv[2]) if len(sys.argv)>2 else 0.05))
