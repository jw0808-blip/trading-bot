#!/usr/bin/env python3
"""TraderJoes EchoEdge — Risk Manager. Kelly sizing, position limits, risk analysis."""

import sys, math


def kelly_criterion(fair_prob, market_price):
    """Full Kelly, Half Kelly, Quarter Kelly."""
    if fair_prob <= market_price or market_price <= 0 or market_price >= 1:
        return 0, 0, 0
    b = (1.0 / market_price) - 1.0  # odds
    p = fair_prob
    q = 1.0 - p
    full = (b * p - q) / b
    full = max(0, min(full, 1.0))
    return round(full, 4), round(full * 0.5, 4), round(full * 0.25, 4)


def position_size(kelly_frac, bankroll, max_pct=0.10):
    """Calculate USD position size with max cap."""
    raw = bankroll * kelly_frac
    capped = min(raw, bankroll * max_pct)
    return round(capped, 2)


def risk_of_ruin(win_prob, avg_win, avg_loss, bankroll, n_bets=100):
    """Simplified risk of ruin estimate."""
    if avg_loss == 0: return 0
    edge = win_prob * avg_win - (1-win_prob) * avg_loss
    if edge <= 0: return 99.9
    variance = win_prob * avg_win**2 + (1-win_prob) * avg_loss**2
    if variance == 0: return 0
    ror = math.exp(-2 * edge * bankroll / variance) * 100
    return min(round(ror, 1), 99.9)


def analyze(question, fair_prob, market_price, bankroll):
    ev = fair_prob - market_price
    ev_pct = (ev / market_price * 100) if market_price > 0 else 0
    
    full_k, half_k, quarter_k = kelly_criterion(fair_prob, market_price)
    
    full_usd = position_size(full_k, bankroll)
    half_usd = position_size(half_k, bankroll)
    quarter_usd = position_size(quarter_k, bankroll)
    
    # Win/loss if YES resolves
    payout_per_share = 1.0 / market_price if market_price > 0 else 0
    profit_if_win = half_usd * (payout_per_share - 1) if payout_per_share > 1 else 0
    loss_if_lose = half_usd
    
    # Risk of ruin
    ror = risk_of_ruin(fair_prob, profit_if_win, loss_if_lose, bankroll)
    
    # Risk grade
    if half_k < 0.02: grade = "LOW RISK"
    elif half_k < 0.05: grade = "MODERATE"
    elif half_k < 0.10: grade = "AGGRESSIVE"
    else: grade = "⚠️ HIGH RISK"
    
    # Max concurrent positions recommendation
    if bankroll < 100: max_positions = 3
    elif bankroll < 500: max_positions = 5
    elif bankroll < 2000: max_positions = 8
    else: max_positions = 12
    
    # Recommendation
    if ev_pct > 20 and full_k > 0.05:
        rec = "🟢 PROCEED — Strong edge detected"
    elif ev_pct > 5 and full_k > 0.02:
        rec = "🟡 CAUTIOUS — Moderate edge, use Half Kelly"
    elif ev_pct > 0:
        rec = "⚪ MARGINAL — Small edge, use Quarter Kelly or skip"
    else:
        rec = "🔴 PASS — No positive EV detected"
    
    return f"""🛡️ **Risk Analysis**
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
**Market:** {question}
**Fair Probability:** {fair_prob:.1%}
**Market Price:** {market_price:.1%}
**EV Spread:** {ev:+.1%} ({ev_pct:+.1f}%)
**Bankroll:** ${bankroll:,.2f}

**Kelly Criterion Sizing:**
  Full Kelly: {full_k:.1%} → ${full_usd:,.2f}
  Half Kelly: {half_k:.1%} → ${half_usd:,.2f} ← Recommended
  Quarter Kelly: {quarter_k:.1%} → ${quarter_usd:,.2f}

**If You Bet ${half_usd:.2f} (Half Kelly):**
  Win (YES resolves): +${profit_if_win:,.2f}
  Lose (NO resolves): -${loss_if_lose:,.2f}
  Risk of Ruin: {ror:.1f}%

**Portfolio Limits:**
  Risk Grade: {grade}
  Max Concurrent Positions: {max_positions}
  Max Single Position: {bankroll * 0.10:,.2f} (10% of bankroll)
  Daily Loss Limit: ${bankroll * 0.05:,.2f} (5% of bankroll)

**Recommendation:** {rec}
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"""


if __name__ == "__main__":
    if len(sys.argv) < 5:
        print("Usage: python risk.py '<question>' <fair_prob> <market_price> <bankroll>")
        print("Example: python risk.py 'US strikes Iran' 0.18 0.031 1000")
        sys.exit(1)
    print(analyze(sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])))
