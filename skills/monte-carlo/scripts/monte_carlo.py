#!/usr/bin/env python3
"""
TraderJoes EchoEdge — Monte Carlo Simulation Bot
Uses GPT-4o-mini for variable extraction + NumPy for simulation.
"""

import os, sys, json
import numpy as np
from openai import OpenAI

client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

VARIABLE_EXTRACTION_PROMPT = """You are a quantitative analyst for prediction markets.

Given this prediction market question: "{question}"
Current market price (YES): {market_price}

Identify 3-6 key independent variables that determine the outcome probability.
For each variable, provide:
- name: short label
- description: what it measures
- base_rate: probability estimate (0.0 to 1.0)
- volatility: how uncertain (0.05 = very certain, 0.3 = very uncertain)
- weight: influence on outcome (0.0 to 1.0, sum to ~1.0)
- direction: "positive" if higher = more likely YES, "negative" otherwise

Also provide:
- overall_base_probability: best estimate (0.0 to 1.0)
- reasoning: brief explanation

Respond ONLY in JSON format:
{{
  "overall_base_probability": 0.XX,
  "reasoning": "...",
  "variables": [
    {{
      "name": "...", "description": "...", "base_rate": 0.XX,
      "volatility": 0.XX, "weight": 0.XX, "direction": "positive"
    }}
  ]
}}"""


def extract_variables(question, market_price):
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You are a quantitative prediction market analyst. Respond only in valid JSON."},
            {"role": "user", "content": VARIABLE_EXTRACTION_PROMPT.format(question=question, market_price=market_price)},
        ],
        temperature=0.3, max_tokens=1000,
    )
    text = resp.choices[0].message.content.strip()
    if text.startswith("```"): text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(text)


def run_simulation(variables, base_prob, n_sims=10000):
    np.random.seed(42)
    outcomes = np.zeros(n_sims)
    for i in range(n_sims):
        prob_adjustment = 0.0
        for var in variables:
            base = var["base_rate"]; vol = var["volatility"]
            weight = var["weight"]
            direction = 1.0 if var["direction"] == "positive" else -1.0
            if vol > 0:
                alpha = base * (1 / vol - 1) if vol < 1 else 2
                beta_param = (1 - base) * (1 / vol - 1) if vol < 1 else 2
                alpha = max(alpha, 0.5); beta_param = max(beta_param, 0.5)
                sampled = np.random.beta(alpha, beta_param)
            else:
                sampled = base
            deviation = (sampled - base) * direction * weight
            prob_adjustment += deviation
        final_prob = np.clip(base_prob + prob_adjustment, 0.001, 0.999)
        outcomes[i] = final_prob
    return {
        "n_simulations": n_sims,
        "median_probability": round(float(np.median(outcomes)), 4),
        "mean_probability": round(float(np.mean(outcomes)), 4),
        "std_deviation": round(float(np.std(outcomes)), 4),
        "ci_95_low": round(float(np.percentile(outcomes, 2.5)), 4),
        "ci_95_high": round(float(np.percentile(outcomes, 97.5)), 4),
        "p10": round(float(np.percentile(outcomes, 10)), 4),
        "p90": round(float(np.percentile(outcomes, 90)), 4),
    }


def compute_ev(sim_result, market_price):
    fair_value = sim_result["median_probability"]
    ev = fair_value - market_price
    ev_pct = (ev / market_price * 100) if market_price > 0 else 0
    if ev > 0 and 0 < fair_value < 1:
        b = (1.0 / market_price) - 1.0
        kelly = (b * fair_value - (1 - fair_value)) / b if b > 0 else 0
        kelly = max(0, min(kelly, 0.25))
    else:
        kelly = 0
    ci_width = sim_result["ci_95_high"] - sim_result["ci_95_low"]
    confidence = "HIGH" if ci_width < 0.1 else "MEDIUM" if ci_width < 0.25 else "LOW"
    if ev_pct > 10 and confidence in ("HIGH", "MEDIUM"): signal = "STRONG BUY"
    elif ev_pct > 5: signal = "BUY"
    elif ev_pct < -10: signal = "SELL / FADE"
    else: signal = "HOLD / NO EDGE"
    return {"fair_value": round(fair_value, 4), "market_price": market_price,
            "ev_absolute": round(ev, 4), "ev_percent": round(ev_pct, 2),
            "kelly_fraction": round(kelly, 4), "confidence": confidence, "signal": signal}


def format_output(question, analysis, sim, ev):
    var_lines = []
    for v in analysis.get("variables", []):
        arrow = "↑" if v["direction"] == "positive" else "↓"
        var_lines.append(f"  {arrow} {v['name']}: base {v['base_rate']:.0%} (±{v['volatility']:.0%}) weight {v['weight']:.0%}")
    var_str = "\n".join(var_lines) if var_lines else "  (none extracted)"
    sig_emoji = {"STRONG BUY": "🟢🟢", "BUY": "🟢", "SELL / FADE": "🔴", "HOLD / NO EDGE": "⚪"}.get(ev["signal"], "⚪")
    return f"""🎲 **Monte Carlo Simulation**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**Q:** {question}

**Simulations:** {sim['n_simulations']:,} paths
**Fair Value:** {sim['median_probability']:.1%} (95% CI: {sim['ci_95_low']:.1%} – {sim['ci_95_high']:.1%})
**Market Price:** {ev['market_price']:.1%}
**EV Spread:** {ev['ev_absolute']:+.1%} ({ev['ev_percent']:+.1f}%)
**Confidence:** {ev['confidence']}
**Kelly Size:** {ev['kelly_fraction']:.1%} of bankroll

{sig_emoji} **Signal: {ev['signal']}**

**Key Variables:**
{var_str}

**Reasoning:** {analysis.get('reasoning', 'N/A')}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


def run_monte_carlo(question, market_price=0.05, n_sims=10000):
    analysis = extract_variables(question, market_price)
    variables = analysis.get("variables", [])
    base_prob = analysis.get("overall_base_probability", 0.5)
    sim_result = run_simulation(variables, base_prob, n_sims)
    ev_result = compute_ev(sim_result, market_price)
    return format_output(question, analysis, sim_result, ev_result)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python monte_carlo.py '<question>' [market_price] [n_sims]"); sys.exit(1)
    question = sys.argv[1]
    price = float(sys.argv[2]) if len(sys.argv) > 2 else 0.05
    sims = int(sys.argv[3]) if len(sys.argv) > 3 else 10000
    print(run_monte_carlo(question, price, sims))
