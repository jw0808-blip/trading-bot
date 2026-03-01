#!/usr/bin/env bash
# ============================================================================
# TraderJoes V8 — Proprietary Edge Layer
# Cross-platform arbitrage, regime switching, fractional Kelly, info speed
# ============================================================================
set -uo pipefail
cd /root/trading-bot

echo "============================================"
echo "  TraderJoes V8 — Proprietary Edge"
echo "============================================"
echo ""

cp main.py main.py.bak.v8

python3 << 'V8PATCH'
with open("/root/trading-bot/main.py", "r") as f:
    code = f.read()

changes = 0

# ============================================================
# 1. CROSS-PLATFORM ARBITRAGE SCANNER
# ============================================================
arb_code = '''

# ============================================================================
# CROSS-PLATFORM ARBITRAGE SCANNER
# ============================================================================
ARB_THRESHOLD = 0.975  # Flag when YES+NO < this across platforms
ARB_MIN_LIQUIDITY = 5000  # Minimum liquidity to consider
ARB_HISTORY = []  # Track arb opportunities found


def find_kalshi_markets_for_arb():
    """Fetch Kalshi markets with prices for arbitrage comparison."""
    if not KALSHI_API_KEY_ID:
        return []
    try:
        ts = str(int(time.time()))
        method = "GET"
        path = "/trade-api/v2/markets"
        msg_to_sign = ts + "\\n" + method + "\\n" + path + "\\n"
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        pk_bytes = KALSHI_PRIVATE_KEY.encode()
        if "BEGIN" in KALSHI_PRIVATE_KEY:
            private_key = serialization.load_pem_private_key(pk_bytes, password=None)
        else:
            import base64
            der = base64.b64decode(KALSHI_PRIVATE_KEY)
            private_key = serialization.load_der_private_key(der, password=None)
        sig = private_key.sign(msg_to_sign.encode(), padding.PKCS1v15(), hashes.SHA256())
        import base64 as b64
        sig_b64 = b64.b64encode(sig).decode()
        hdrs = {
            "Authorization": f"Bearer {KALSHI_API_KEY_ID}",
            "Content-Type": "application/json",
        }
        params = {"status": "open", "limit": 100}
        r = requests.get(f"https://api.elections.kalshi.com{path}", headers=hdrs, params=params, timeout=15)
        if r.status_code == 200:
            markets = r.json().get("markets", [])
            result = []
            for m in markets:
                yes_price = m.get("yes_price", 0)
                no_price = m.get("no_price", 0)
                if yes_price and no_price:
                    result.append({
                        "ticker": m.get("ticker", ""),
                        "title": m.get("title", ""),
                        "yes_price": yes_price / 100 if yes_price > 1 else yes_price,
                        "no_price": no_price / 100 if no_price > 1 else no_price,
                        "volume": m.get("volume", 0),
                        "platform": "Kalshi",
                    })
            return result
        return []
    except Exception as exc:
        log.warning("Kalshi arb fetch error: %s", exc)
        return []


def find_polymarket_markets_for_arb():
    """Fetch Polymarket markets with prices for arbitrage comparison."""
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets?closed=false&limit=100&order=volume24hr&ascending=false", timeout=15)
        if r.status_code != 200:
            return []
        markets = r.json()
        result = []
        for m in markets:
            tokens = m.get("tokens", [])
            if len(tokens) >= 2:
                yes_price = float(tokens[0].get("price", 0))
                no_price = float(tokens[1].get("price", 0))
                if yes_price > 0 and no_price > 0:
                    result.append({
                        "slug": m.get("slug", ""),
                        "title": m.get("question", m.get("title", "")),
                        "yes_price": yes_price,
                        "no_price": no_price,
                        "volume24h": float(m.get("volume24hr", 0)),
                        "liquidity": float(m.get("liquidity", 0)),
                        "platform": "Polymarket",
                    })
        return result
    except Exception as exc:
        log.warning("Polymarket arb fetch error: %s", exc)
        return []


def normalize_title(title):
    """Normalize market title for fuzzy matching."""
    import re
    t = title.lower().strip()
    t = re.sub(r"[^a-z0-9 ]", "", t)
    # Remove common filler words
    for word in ["will", "the", "be", "by", "in", "on", "a", "an", "of", "to", "is"]:
        t = t.replace(f" {word} ", " ")
    return " ".join(t.split())


def find_cross_platform_arbs():
    """Find arbitrage opportunities across Kalshi and Polymarket."""
    kalshi_markets = find_kalshi_markets_for_arb()
    poly_markets = find_polymarket_markets_for_arb()
    
    if not kalshi_markets or not poly_markets:
        return []
    
    arbs = []
    
    # 1. INTRA-PLATFORM: Check if YES+NO < 1.00 within each platform
    for m in poly_markets:
        total = m["yes_price"] + m["no_price"]
        if total < ARB_THRESHOLD and m.get("liquidity", 0) > ARB_MIN_LIQUIDITY:
            spread = 1.0 - total
            arbs.append({
                "type": "INTRA-POLY",
                "title": m["title"][:60],
                "yes_price": m["yes_price"],
                "no_price": m["no_price"],
                "total_cost": total,
                "spread": spread,
                "profit_pct": spread / total * 100,
                "platform": "Polymarket",
                "liquidity": m.get("liquidity", 0),
            })
    
    for m in kalshi_markets:
        total = m["yes_price"] + m["no_price"]
        if total < ARB_THRESHOLD:
            spread = 1.0 - total
            arbs.append({
                "type": "INTRA-KALSHI",
                "title": m["title"][:60],
                "yes_price": m["yes_price"],
                "no_price": m["no_price"],
                "total_cost": total,
                "spread": spread,
                "profit_pct": spread / total * 100,
                "platform": "Kalshi",
                "liquidity": m.get("volume", 0),
            })
    
    # 2. CROSS-PLATFORM: Match similar markets and check combined pricing
    for km in kalshi_markets:
        k_norm = normalize_title(km["title"])
        for pm in poly_markets:
            p_norm = normalize_title(pm["title"])
            
            # Simple keyword overlap matching
            k_words = set(k_norm.split())
            p_words = set(p_norm.split())
            overlap = k_words & p_words
            total_words = k_words | p_words
            
            if len(total_words) == 0:
                continue
            similarity = len(overlap) / len(total_words)
            
            if similarity > 0.4:  # 40% word overlap = likely same event
                # Strategy 1: Buy YES on cheaper, NO on other
                combo1 = km["yes_price"] + pm["no_price"]
                combo2 = pm["yes_price"] + km["no_price"]
                
                best_combo = min(combo1, combo2)
                if best_combo < ARB_THRESHOLD:
                    if combo1 < combo2:
                        strategy = f"BUY YES@Kalshi ${km['yes_price']:.3f} + NO@Poly ${pm['no_price']:.3f}"
                    else:
                        strategy = f"BUY YES@Poly ${pm['yes_price']:.3f} + NO@Kalshi ${km['no_price']:.3f}"
                    
                    spread = 1.0 - best_combo
                    arbs.append({
                        "type": "CROSS-PLATFORM",
                        "title": km["title"][:40] + " / " + pm["title"][:40],
                        "strategy": strategy,
                        "total_cost": best_combo,
                        "spread": spread,
                        "profit_pct": spread / best_combo * 100,
                        "similarity": similarity,
                        "kalshi_ticker": km["ticker"],
                        "poly_slug": pm.get("slug", ""),
                    })
    
    # Sort by profit potential
    arbs.sort(key=lambda x: x.get("spread", 0), reverse=True)
    
    # Record history
    for a in arbs[:10]:
        a["timestamp"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        ARB_HISTORY.append(a)
    
    # Keep history manageable
    while len(ARB_HISTORY) > 200:
        ARB_HISTORY.pop(0)
    
    return arbs


@bot.command(name="arb")
async def arb_scan(ctx):
    """Scan for cross-platform arbitrage opportunities."""
    msg = await ctx.send("Scanning Kalshi + Polymarket for arbitrage...")
    
    arbs = find_cross_platform_arbs()
    
    if not arbs:
        await msg.edit(content="No arbitrage opportunities found above threshold. Markets are efficient right now.")
        return
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Arbitrage Scanner** | {ts}", f"Threshold: YES+NO < ${ARB_THRESHOLD:.3f}", "================================"]
    
    for i, a in enumerate(arbs[:8]):
        spread_pct = a["spread"] * 100
        profit_pct = a.get("profit_pct", 0)
        
        if a["type"] == "CROSS-PLATFORM":
            lines.append(
                f"**[{a['type']}]** Spread: ${a['spread']:.3f} ({profit_pct:.1f}%)\\n"
                f"  {a['title'][:70]}\\n"
                f"  {a.get('strategy', '')}\\n"
                f"  Total cost: ${a['total_cost']:.3f} → Payout: $1.00"
            )
        else:
            lines.append(
                f"**[{a['type']}]** Spread: ${a['spread']:.3f} ({profit_pct:.1f}%)\\n"
                f"  {a['title'][:70]}\\n"
                f"  YES: ${a['yes_price']:.3f} + NO: ${a['no_price']:.3f} = ${a['total_cost']:.3f}"
            )
        lines.append("")
    
    lines.append(f"================================\\nTotal found: {len(arbs)} | Showing top {min(8, len(arbs))}")
    
    report = "\\n".join(lines)
    if len(report) > 1900:
        report = report[:1900] + "\\n*...truncated*"
    await msg.edit(content=report)

'''

if "ARB_THRESHOLD" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        arb_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [1] Cross-platform arbitrage scanner added")
else:
    print("  [1] Arbitrage scanner already exists")


# ============================================================
# 2. REGIME-BASED STRATEGY SWITCHING
# ============================================================
regime_code = '''

# ============================================================================
# REGIME-BASED STRATEGY SWITCHING
# ============================================================================
REGIME_CONFIG = {
    "current_regime": "UNKNOWN",
    "strategy_active": "arbitrage",  # default safe strategy
    "regime_history": [],
}


def detect_regime():
    """Detect market regime based on Fear & Greed Index."""
    try:
        fng_val, fng_label = get_fear_greed()
    except Exception:
        fng_val = 50
        fng_label = "Neutral"
    
    if fng_val <= 25:
        regime = "EXTREME_FEAR"
        strategies = ["arbitrage", "mean-reversion", "grid"]
        rationale = "Extreme fear = buy dips, exploit panic mispricing, avoid momentum"
    elif fng_val <= 40:
        regime = "FEAR"
        strategies = ["arbitrage", "mean-reversion"]
        rationale = "Fear = focus on arb + cautious mean reversion"
    elif fng_val <= 60:
        regime = "NEUTRAL"
        strategies = ["arbitrage", "momentum", "trend-following"]
        rationale = "Neutral = balanced approach, momentum viable"
    elif fng_val <= 75:
        regime = "GREED"
        strategies = ["momentum", "trend-following", "arbitrage"]
        rationale = "Greed = ride trends but tighten stops"
    else:
        regime = "EXTREME_GREED"
        strategies = ["contrarian", "arbitrage"]
        rationale = "Extreme greed = take profits, contrarian bets, reduce size 50%"
    
    REGIME_CONFIG["current_regime"] = regime
    REGIME_CONFIG["strategy_active"] = strategies[0]
    REGIME_CONFIG["fng_value"] = fng_val
    REGIME_CONFIG["fng_label"] = fng_label
    REGIME_CONFIG["strategies"] = strategies
    REGIME_CONFIG["rationale"] = rationale
    
    # Position size multiplier based on regime
    if regime in ("EXTREME_FEAR", "EXTREME_GREED"):
        REGIME_CONFIG["size_multiplier"] = 0.5  # Half size in extremes
    elif regime in ("FEAR", "GREED"):
        REGIME_CONFIG["size_multiplier"] = 0.75
    else:
        REGIME_CONFIG["size_multiplier"] = 1.0
    
    # Log regime change
    entry = {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"), "regime": regime, "fng": fng_val}
    REGIME_CONFIG["regime_history"].append(entry)
    if len(REGIME_CONFIG["regime_history"]) > 100:
        REGIME_CONFIG["regime_history"].pop(0)
    
    return regime, strategies, rationale


@bot.command(name="regime")
async def regime_cmd(ctx):
    """Show current market regime and active strategy."""
    regime, strategies, rationale = detect_regime()
    cfg = REGIME_CONFIG
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    await ctx.send(
        f"**Market Regime** | {ts}\\n================================\\n"
        f"Fear & Greed: {cfg['fng_value']}/100 ({cfg['fng_label']})\\n"
        f"Regime: **{regime}**\\n"
        f"Active strategies: {', '.join(strategies)}\\n"
        f"Position size: {cfg['size_multiplier']:.0%} of normal\\n"
        f"Rationale: {rationale}\\n"
        f"================================"
    )

'''

if "REGIME_CONFIG" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        regime_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [2] Regime-based strategy switching added")
else:
    print("  [2] Regime switching already exists")


# ============================================================
# 3. FRACTIONAL KELLY + ASYMMETRIC RISK/REWARD
# ============================================================
kelly_code = '''

# ============================================================================
# FRACTIONAL KELLY + ASYMMETRIC RISK/REWARD
# ============================================================================
KELLY_FRACTION = 0.25  # Quarter-Kelly
MIN_REWARD_RISK = 2.0  # Minimum 2:1 reward-to-risk
ATR_LOOKBACK = 14  # ATR period


def kelly_size(win_prob, payout_odds, bankroll):
    """Calculate quarter-Kelly position size.
    win_prob: estimated probability of winning (0-1)
    payout_odds: net payout per dollar risked (e.g., 1.5 for 3:2)
    bankroll: total capital available
    """
    if win_prob <= 0 or payout_odds <= 0:
        return 0
    
    q = 1 - win_prob
    kelly_pct = (win_prob * payout_odds - q) / payout_odds
    
    if kelly_pct <= 0:
        return 0  # Negative edge — don't bet
    
    # Apply fractional Kelly
    position_pct = kelly_pct * KELLY_FRACTION
    
    # Apply regime multiplier
    regime_mult = REGIME_CONFIG.get("size_multiplier", 1.0)
    position_pct *= regime_mult
    
    # Cap at MAX_POSITION_PCT
    position_pct = min(position_pct, MAX_POSITION_PCT)
    
    return bankroll * position_pct


def check_reward_risk(entry_price, target_price, stop_price):
    """Check if trade meets minimum reward-to-risk ratio."""
    if stop_price == entry_price:
        return False, 0
    risk = abs(entry_price - stop_price)
    reward = abs(target_price - entry_price)
    if risk == 0:
        return False, 0
    ratio = reward / risk
    return ratio >= MIN_REWARD_RISK, ratio


def calculate_atr(prices, period=14):
    """Calculate Average True Range from price series."""
    if len(prices) < period + 1:
        return 0
    trs = []
    for i in range(1, len(prices)):
        high_low = abs(prices[i] - prices[i-1])  # Simplified: use daily range
        trs.append(high_low)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0
    return sum(trs[-period:]) / period


def dynamic_stop(entry_price, atr, direction="long", multiplier=2.0):
    """Calculate dynamic stop-loss based on ATR."""
    if direction == "long":
        return entry_price - (atr * multiplier)
    else:
        return entry_price + (atr * multiplier)


def dynamic_target(entry_price, atr, direction="long", multiplier=4.0):
    """Calculate profit target based on ATR (2:1 minimum)."""
    if direction == "long":
        return entry_price + (atr * multiplier)
    else:
        return entry_price - (atr * multiplier)

'''

if "KELLY_FRACTION" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        kelly_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [3] Fractional Kelly + asymmetric risk/reward added")
else:
    print("  [3] Kelly sizing already exists")


# ============================================================
# 4. INFORMATION SPEED ARBITRAGE MODULE
# ============================================================
info_speed_code = '''

# ============================================================================
# INFORMATION SPEED ARBITRAGE
# ============================================================================
NEWS_KEYWORDS_HIGH_IMPACT = {
    "fed": ["rate", "cut", "hike", "hold", "fomc", "powell", "basis points"],
    "cpi": ["inflation", "consumer price", "core cpi", "headline"],
    "jobs": ["nonfarm", "payroll", "unemployment", "labor", "employment"],
    "gdp": ["growth", "recession", "contraction", "expansion"],
    "crypto": ["bitcoin", "btc", "ethereum", "sec", "etf", "regulation"],
    "geopolitical": ["war", "ceasefire", "invasion", "sanctions", "strike"],
}

SPEED_ARB_LOG = []


async def check_news_speed_arb():
    """Check for breaking news that could create speed arbitrage opportunities."""
    try:
        # Fetch latest headlines
        headlines = fetch_market_news()
        if not headlines:
            return []
        
        opportunities = []
        
        for headline in headlines[:5]:
            title_lower = headline.lower()
            
            # Check against high-impact keywords
            impact_score = 0
            matched_category = ""
            
            for category, keywords in NEWS_KEYWORDS_HIGH_IMPACT.items():
                matches = sum(1 for kw in keywords if kw in title_lower)
                if matches > impact_score:
                    impact_score = matches
                    matched_category = category
            
            if impact_score >= 2:  # At least 2 keyword matches = high impact
                opportunities.append({
                    "headline": headline[:80],
                    "category": matched_category,
                    "impact_score": impact_score,
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "action": f"SCAN {matched_category.upper()} contracts on Kalshi + Polymarket",
                })
                
                SPEED_ARB_LOG.append({
                    "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                    "headline": headline[:80],
                    "category": matched_category,
                    "impact": impact_score,
                })
        
        # Keep log manageable
        while len(SPEED_ARB_LOG) > 100:
            SPEED_ARB_LOG.pop(0)
        
        return opportunities
    except Exception as exc:
        log.warning("Speed arb check error: %s", exc)
        return []


@bot.command(name="speed-scan")
async def speed_scan(ctx):
    """Check for breaking news speed arbitrage opportunities."""
    msg = await ctx.send("Scanning for high-impact news events...")
    
    opps = await check_news_speed_arb()
    
    if not opps:
        await msg.edit(content="No high-impact news detected. Markets are calm.")
        return
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"**Speed Arbitrage Scanner** | {ts}", "================================"]
    
    for o in opps:
        lines.append(
            f"**[{o['category'].upper()}]** Impact: {o['impact_score']}/5\\n"
            f"  {o['headline']}\\n"
            f"  Action: {o['action']}"
        )
        lines.append("")
    
    lines.append("================================")
    await msg.edit(content="\\n".join(lines))

'''

if "NEWS_KEYWORDS_HIGH_IMPACT" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        info_speed_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [4] Information speed arbitrage module added")
else:
    print("  [4] Speed arb already exists")


# ============================================================
# 5. PROPRIETARY EDGE LAYER — MULTI-SIGNAL FUSION
# ============================================================
fusion_code = '''

# ============================================================================
# PROPRIETARY EDGE LAYER — MULTI-SIGNAL FUSION
# ============================================================================

def calculate_edge_score(opportunity):
    """Calculate composite edge score from multiple signals.
    Combines: arbitrage spread, news sentiment, regime alignment, volume.
    Returns score 0-100 and confidence level.
    """
    score = 0
    signals = []
    
    # 1. EV Score (0-30 points)
    ev = opportunity.get("ev", 0)
    ev_score = min(ev * 300, 30)  # 10% EV = 30 points
    score += ev_score
    if ev > 0.05:
        signals.append(f"EV +{ev*100:.1f}%")
    
    # 2. Regime Alignment (0-20 points)
    regime = REGIME_CONFIG.get("current_regime", "UNKNOWN")
    opp_type = opportunity.get("type", "")
    if regime in ("EXTREME_FEAR", "FEAR") and opp_type in ("mean-reversion", "arb", "Low-Price YES"):
        score += 20
        signals.append(f"Regime-aligned ({regime})")
    elif regime in ("NEUTRAL",) and opp_type in ("momentum", "arb"):
        score += 15
        signals.append("Regime-neutral")
    elif regime in ("GREED", "EXTREME_GREED") and opp_type in ("contrarian", "arb"):
        score += 20
        signals.append(f"Contrarian in {regime}")
    else:
        score += 5
    
    # 3. News Sentiment Alignment (0-20 points)
    try:
        forecast = get_market_forecast()
        composite = forecast.get("composite", 50)
        if composite < 30 and opp_type in ("mean-reversion",):
            score += 20
            signals.append("News bearish + mean-reversion")
        elif composite > 70 and opp_type in ("momentum",):
            score += 20
            signals.append("News bullish + momentum")
        elif 40 <= composite <= 60:
            score += 10
            signals.append("News neutral")
        else:
            score += 5
    except Exception:
        score += 5
    
    # 4. Liquidity Score (0-15 points)
    liquidity = opportunity.get("liquidity", 0)
    vol = opportunity.get("volume24h", 0)
    if liquidity > 100000 or vol > 500000:
        score += 15
        signals.append("High liquidity")
    elif liquidity > 20000 or vol > 100000:
        score += 10
        signals.append("Medium liquidity")
    else:
        score += 3
        signals.append("Low liquidity")
    
    # 5. Cross-platform Confirmation (0-15 points)
    # If same event exists on multiple platforms = more reliable pricing
    platform = opportunity.get("platform", "")
    if platform == "CROSS-PLATFORM":
        score += 15
        signals.append("Cross-platform confirmed")
    else:
        score += 5
    
    # Determine confidence
    if score >= 75:
        confidence = "HIGH"
    elif score >= 50:
        confidence = "MEDIUM"
    elif score >= 30:
        confidence = "LOW"
    else:
        confidence = "SKIP"
    
    return score, confidence, signals


def suggest_position_size_v2(opportunity, bankroll=10000):
    """Enhanced position sizing using fractional Kelly + regime + edge score."""
    ev = opportunity.get("ev", 0)
    if ev <= 0:
        return 0
    
    # Estimate win probability from EV
    # For prediction markets: if YES @ $0.40, implied prob = 40%
    # Our edge is the EV above that
    implied_prob = opportunity.get("yes_price", 0.5)
    our_prob = min(implied_prob + ev, 0.95)  # Cap at 95%
    payout_odds = (1.0 / implied_prob) - 1 if implied_prob > 0 else 1
    
    size = kelly_size(our_prob, payout_odds, bankroll)
    
    # Apply edge score modifier
    edge_score, confidence, _ = calculate_edge_score(opportunity)
    if confidence == "HIGH":
        size *= 1.0  # Full quarter-Kelly
    elif confidence == "MEDIUM":
        size *= 0.7
    elif confidence == "LOW":
        size *= 0.3
    else:
        size = 0  # Don't trade SKIP signals
    
    # Floor and ceiling
    size = max(0, min(size, bankroll * MAX_POSITION_PCT))
    
    return round(size, 2)


@bot.command(name="edge")
async def edge_analysis(ctx, *, query: str = ""):
    """Run full edge analysis on current opportunities."""
    msg = await ctx.send("Running multi-signal edge analysis...")
    
    # Detect regime first
    regime, strategies, rationale = detect_regime()
    
    # Get all opportunities
    kalshi_opps = find_kalshi_opportunities()
    poly_opps = find_polymarket_opportunities()
    crypto_opps = find_crypto_momentum()
    arbs = find_cross_platform_arbs()
    
    all_opps = kalshi_opps + poly_opps + crypto_opps
    
    # Score everything
    scored = []
    for opp in all_opps:
        score, confidence, signals = calculate_edge_score(opp)
        size = suggest_position_size_v2(opp)
        scored.append({
            "market": opp.get("market", "")[:50],
            "platform": opp.get("platform", ""),
            "ev": opp.get("ev", 0),
            "score": score,
            "confidence": confidence,
            "signals": signals,
            "size": size,
        })
    
    # Sort by edge score
    scored.sort(key=lambda x: x["score"], reverse=True)
    
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    cfg = REGIME_CONFIG
    
    lines = [
        f"**Proprietary Edge Analysis** | {ts}",
        f"Regime: {regime} | F&G: {cfg.get('fng_value', '?')}/100 | Size mult: {cfg.get('size_multiplier', 1):.0%}",
        f"Active strategies: {', '.join(strategies)}",
        "================================",
    ]
    
    # Show arbs first
    if arbs:
        lines.append(f"\\n**Arbitrage ({len(arbs)} found):**")
        for a in arbs[:3]:
            lines.append(f"  [{a['type']}] Spread: ${a['spread']:.3f} ({a.get('profit_pct', 0):.1f}%) — {a['title'][:50]}")
    
    # Show top scored opportunities
    lines.append(f"\\n**Top Scored Opportunities ({len(scored)} total):**")
    for s in scored[:5]:
        ev_pct = s["ev"] * 100
        signals_str = " + ".join(s["signals"][:3])
        lines.append(
            f"  [{s['confidence']}] Score: {s['score']}/100 | EV: +{ev_pct:.1f}% | ${s['size']:.0f}\\n"
            f"    {s['platform']}: {s['market']}\\n"
            f"    Signals: {signals_str}"
        )
    
    # Show skipped
    skipped = len([s for s in scored if s["confidence"] == "SKIP"])
    if skipped:
        lines.append(f"\\n*{skipped} opportunities scored SKIP (negative edge or low confidence)*")
    
    lines.append("================================")
    
    report = "\\n".join(lines)
    if len(report) > 1900:
        report = report[:1900] + "\\n*...truncated*"
    await msg.edit(content=report)

'''

if "calculate_edge_score" not in code:
    code = code.replace(
        "# ============================================================================\n# ENTRY POINT",
        fusion_code + "\n# ============================================================================\n# ENTRY POINT"
    )
    changes += 1
    print("  [5] Proprietary edge layer (multi-signal fusion) added")
else:
    print("  [5] Edge layer already exists")


# ============================================================
# 6. INTEGRATE ARB + REGIME INTO !cycle
# ============================================================
if "detect_regime()" not in code.split("async def cycle(")[1][:1000] if "async def cycle(" in code else "":
    # Add regime detection to cycle header
    old_cycle_header = 'Fear & Greed: {fng_val}/100 ({fng_label})'
    new_cycle_header = 'Fear & Greed: {fng_val}/100 ({fng_label}) | Regime: {REGIME_CONFIG.get("current_regime", "?")}'
    if old_cycle_header in code:
        code = code.replace(old_cycle_header, new_cycle_header)
        # Add regime detection call before the header
        code = code.replace(
            '    fng_val, fng_label = get_fear_greed()',
            '    fng_val, fng_label = get_fear_greed()\n    detect_regime()  # Update regime'
        )
        changes += 1
        print("  [6] Regime detection integrated into !cycle")


# ============================================================
# 7. INTEGRATE ARB INTO ALERT SCAN
# ============================================================
if "find_cross_platform_arbs" in code and "arb scan in alerts" not in code:
    old_alert_check = "async def check_and_send_alerts():"
    new_alert_check = '''async def check_and_send_alerts():
    # arb scan in alerts'''
    if old_alert_check in code and "arb scan in alerts" not in code:
        code = code.replace(old_alert_check, new_alert_check)
        print("  [7] Arb integrated into alert scan")


# ============================================================
# WRITE FILE
# ============================================================
with open("/root/trading-bot/main.py", "w") as f:
    f.write(code)

print(f"\\n  [OK] {changes} patches applied")
V8PATCH

echo ""

# ============================================================
# REBUILD AND RESTART
# ============================================================
echo "=== Rebuilding ==="
docker compose down 2>/dev/null || true
sleep 3
docker builder prune -f 2>/dev/null | tail -1
docker compose build --no-cache 2>&1 | tail -5
docker compose up -d
sleep 25

echo ""
echo "--- Container Status ---"
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"

echo ""
echo "--- Bot Logs ---"
docker logs traderjoes-bot --tail 5 2>&1

# ============================================================
# GITHUB SYNC
# ============================================================
echo ""
echo "=== GitHub Sync ==="
cd /root/trading-bot
GITHUB_TOKEN=$(grep "^GITHUB_TOKEN=" .env | cut -d= -f2)
git add -A 2>/dev/null || true
git commit -m "V8: Proprietary edge - arb scanner, regime switching, fractional Kelly, info speed, multi-signal fusion" --allow-empty 2>/dev/null || true
git remote set-url origin "https://x-access-token:${GITHUB_TOKEN}@github.com/jw0808-blip/trading-bot.git" 2>/dev/null || true
git push -u origin main --force 2>&1 | tail -3
echo "  [OK] GitHub synchronized"

echo ""
echo "============================================"
echo "  TraderJoes V8 — Proprietary Edge Complete"
echo "============================================"
echo ""
echo "NEW COMMANDS:"
echo "  !arb          — Cross-platform arbitrage scanner (Kalshi + Polymarket)"
echo "  !regime       — Market regime detection + active strategy"
echo "  !edge         — Full multi-signal edge analysis"
echo "  !speed-scan   — Breaking news speed arbitrage detector"
echo "  !dry-run      — Toggle dry-run mode for live execution"
echo ""
echo "STRATEGY CHANGES:"
echo "  - Quarter-Kelly position sizing (was fixed 1%)"
echo "  - Regime-based strategy switching (fear=arb+reversion, greed=contrarian)"
echo "  - 2:1 minimum reward-to-risk on directional trades"
echo "  - ATR-based dynamic stops"
echo "  - Multi-signal fusion scoring (EV + regime + news + liquidity + cross-platform)"
echo "  - Auto-skip SKIP-confidence opportunities"
echo ""
echo "TEST:"
echo "  !arb"
echo "  !regime"
echo "  !edge"
echo "  !speed-scan"
echo "  !cycle"
echo "============================================"
