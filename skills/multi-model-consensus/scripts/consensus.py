#!/usr/bin/env python3
"""
TraderJoes EchoEdge — Multi-Model Consensus Bot
Queries Claude, GPT-4o-mini, and GPT-4o in parallel for independent estimates.
"""

import os, sys, json, concurrent.futures
from openai import OpenAI
from anthropic import Anthropic

openai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
anthropic_client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY", ""))

PROMPT = """You are a prediction market analyst estimating the probability of a specific event.

Question: "{question}"
Current market price (YES shares): ${market_price:.3f} ({market_pct:.1f}%)

Analyze carefully:
1. Consider the base rate for this type of event
2. Factor in current conditions and recent developments
3. Assess information the market may be over/under-weighting
4. Give your independent probability estimate

Respond ONLY in this exact JSON format:
{{
  "probability": 0.XX,
  "direction": "BUY_YES" or "BUY_NO" or "HOLD",
  "confidence": "HIGH" or "MEDIUM" or "LOW",
  "reasoning": "2-3 sentence explanation",
  "key_factor": "single most important factor"
}}"""


def query_claude(question, market_price):
    try:
        resp = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=500,
            messages=[{"role": "user", "content": PROMPT.format(
                question=question, market_price=market_price, market_pct=market_price*100)}])
        text = resp.content[0].text.strip()
        if text.startswith("```"): text = text.split("\n",1)[1].rsplit("```",1)[0]
        r = json.loads(text); r["model"] = "Claude Haiku"; return r
    except Exception as e:
        return {"model": "Claude Haiku", "error": str(e), "probability": None}

def query_gpt_mini(question, market_price):
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini", temperature=0.3, max_tokens=500,
            messages=[{"role": "system", "content": "Respond only in valid JSON."},
                      {"role": "user", "content": PROMPT.format(
                          question=question, market_price=market_price, market_pct=market_price*100)}])
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"): text = text.split("\n",1)[1].rsplit("```",1)[0]
        r = json.loads(text); r["model"] = "GPT-4o-mini"; return r
    except Exception as e:
        return {"model": "GPT-4o-mini", "error": str(e), "probability": None}

def query_gpt4o(question, market_price):
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o", temperature=0.3, max_tokens=500,
            messages=[{"role": "system", "content": "Respond only in valid JSON."},
                      {"role": "user", "content": PROMPT.format(
                          question=question, market_price=market_price, market_pct=market_price*100)}])
        text = resp.choices[0].message.content.strip()
        if text.startswith("```"): text = text.split("\n",1)[1].rsplit("```",1)[0]
        r = json.loads(text); r["model"] = "GPT-4o"; return r
    except Exception as e:
        return {"model": "GPT-4o", "error": str(e), "probability": None}


def synthesize(results, market_price):
    valid = [r for r in results if r.get("probability") is not None]
    if not valid: return {"error": "All models failed"}
    
    probs = [r["probability"] for r in valid]
    dirs = [r.get("direction","HOLD") for r in valid]
    avg = sum(probs)/len(probs); spread = max(probs)-min(probs)
    
    buy_yes = sum(1 for d in dirs if d=="BUY_YES")
    buy_no = sum(1 for d in dirs if d=="BUY_NO")
    total = len(valid)
    
    if buy_yes > total/2: cdir, agr = "BUY YES", buy_yes/total
    elif buy_no > total/2: cdir, agr = "BUY NO", buy_no/total
    else: cdir, agr = "NO CONSENSUS", max(buy_yes,buy_no,total-buy_yes-buy_no)/total
    
    ev = avg - market_price
    ev_pct = (ev/market_price*100) if market_price > 0 else 0
    
    conv = 5
    if agr >= 0.75: conv += 2
    if agr >= 1.0: conv += 1
    if abs(ev_pct) > 20: conv += 1
    if spread < 0.1: conv += 1
    if spread > 0.3: conv -= 2
    conv = max(1, min(10, conv))
    
    return {"avg_probability": round(avg,4), "min_probability": round(min(probs),4),
            "max_probability": round(max(probs),4), "spread": round(spread,4),
            "consensus_direction": cdir, "agreement_pct": round(agr*100,1),
            "ev_absolute": round(ev,4), "ev_percent": round(ev_pct,2),
            "conviction": conv, "models_queried": total,
            "models_agreeing": max(buy_yes, buy_no, total-buy_yes-buy_no)}


def format_output(question, results, consensus, market_price):
    votes = []
    for r in results:
        if r.get("probability") is not None:
            emoji = {"BUY_YES":"🟢","BUY_NO":"🔴","HOLD":"⚪"}.get(r.get("direction",""),"❓")
            votes.append(f"  {emoji} **{r['model']}**: {r['probability']:.1%} → {r.get('direction','?')} ({r.get('confidence','?')})\n    _{r.get('key_factor','N/A')}_")
        else:
            votes.append(f"  ❌ **{r['model']}**: Error — {r.get('error','unknown')}")
    
    c = consensus
    bar = "█"*c["conviction"] + "░"*(10-c["conviction"])
    d = c["consensus_direction"]
    sig = "🟢🟢" if d=="BUY YES" and c["conviction"]>=7 else "🟢" if d=="BUY YES" else "🔴" if d=="BUY NO" else "⚪"
    
    return f"""🤖 **Multi-Model Consensus**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**Q:** {question}

**Model Votes:**
{chr(10).join(votes)}

**Consensus:**
  Direction: {sig} **{d}**
  Agreement: {c['agreement_pct']:.0f}% ({c['models_agreeing']}/{c['models_queried']} models)
  Avg Probability: {c['avg_probability']:.1%} (range: {c['min_probability']:.1%}–{c['max_probability']:.1%})
  Market Price: {market_price:.1%}
  EV Spread: {c['ev_absolute']:+.1%} ({c['ev_percent']:+.1f}%)
  Conviction: [{bar}] {c['conviction']}/10
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def run_consensus(question, market_price=0.05):
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(query_claude, question, market_price): "claude",
                ex.submit(query_gpt_mini, question, market_price): "gpt-mini",
                ex.submit(query_gpt4o, question, market_price): "gpt-4o"}
        results = [f.result() for f in concurrent.futures.as_completed(futs)]
    results.sort(key=lambda r: r.get("model",""))
    consensus = synthesize(results, market_price)
    return format_output(question, results, consensus, market_price)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python consensus.py '<question>' [market_price]"); sys.exit(1)
    print(run_consensus(sys.argv[1], float(sys.argv[2]) if len(sys.argv)>2 else 0.05))
