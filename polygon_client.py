"""Polygon.io market data client for TraderJoes.
Provides real-time quotes, crypto prices, and news feeds.
Falls back gracefully if API key is missing or rate-limited."""

import os
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("traderjoes")

POLYGON_API_KEY = os.environ.get("POLYGON_API_KEY", "")
_client = None


def _get_client():
    """Lazy-init Polygon REST client."""
    global _client
    if _client is None and POLYGON_API_KEY:
        try:
            from polygon import RESTClient
            _client = RESTClient(api_key=POLYGON_API_KEY)
            log.info("POLYGON: client initialized")
        except Exception as e:
            log.warning("POLYGON: init failed: %s", e)
    return _client


def get_quote(ticker):
    """Get latest quote for a stock ticker. Returns dict with bid, ask, mid, last or None."""
    c = _get_client()
    if not c:
        return None
    try:
        snap = c.get_snapshot_all("stocks", ticker_params={"tickers": ticker})
        if snap:
            for s in snap:
                if s.ticker == ticker.upper():
                    lq = s.last_quote
                    lt = s.last_trade
                    return {
                        "ticker": ticker.upper(),
                        "bid": lq.bid_price if lq else 0,
                        "ask": lq.ask_price if lq else 0,
                        "mid": ((lq.bid_price + lq.ask_price) / 2) if lq and lq.bid_price and lq.ask_price else 0,
                        "last": lt.price if lt else 0,
                        "volume": s.day.volume if s.day else 0,
                        "change_pct": s.todays_change_percent or 0,
                    }
        return None
    except Exception as e:
        log.warning("POLYGON quote %s: %s", ticker, e)
        return None


def get_quotes_bulk(tickers):
    """Get quotes for multiple tickers in one call. Returns dict of {ticker: quote_dict}."""
    c = _get_client()
    if not c:
        return {}
    try:
        ticker_str = ",".join(t.upper() for t in tickers)
        snap = c.get_snapshot_all("stocks", ticker_params={"tickers": ticker_str})
        results = {}
        if snap:
            for s in snap:
                lq = s.last_quote
                lt = s.last_trade
                results[s.ticker] = {
                    "ticker": s.ticker,
                    "bid": lq.bid_price if lq else 0,
                    "ask": lq.ask_price if lq else 0,
                    "mid": ((lq.bid_price + lq.ask_price) / 2) if lq and lq.bid_price and lq.ask_price else 0,
                    "last": lt.price if lt else 0,
                    "volume": s.day.volume if s.day else 0,
                    "change_pct": s.todays_change_percent or 0,
                }
        return results
    except Exception as e:
        log.warning("POLYGON bulk quotes: %s", e)
        return {}


def get_crypto_price(ticker="BTC"):
    """Get crypto price from Polygon. ticker should be like 'BTC' or 'ETH'."""
    c = _get_client()
    if not c:
        return None
    try:
        symbol = f"X:{ticker}USD"
        snap = c.get_snapshot_all("crypto", ticker_params={"tickers": symbol})
        if snap:
            for s in snap:
                if s.ticker == symbol:
                    lt = s.last_trade
                    return {
                        "ticker": ticker,
                        "price": lt.price if lt else 0,
                        "change_pct": s.todays_change_percent or 0,
                    }
        return None
    except Exception as e:
        log.warning("POLYGON crypto %s: %s", ticker, e)
        return None


def get_news(ticker=None, limit=10):
    """Get latest market news. Optionally filter by ticker. Returns list of dicts."""
    c = _get_client()
    if not c:
        return []
    try:
        params = {"limit": limit, "order": "desc", "sort": "published_utc"}
        if ticker:
            params["ticker"] = ticker.upper()
        news = c.list_ticker_news(**params)
        results = []
        for n in news:
            results.append({
                "title": n.title,
                "url": n.article_url,
                "published": n.published_utc,
                "tickers": [t for t in (n.tickers or [])],
                "source": n.publisher.name if n.publisher else "",
            })
            if len(results) >= limit:
                break
        return results
    except Exception as e:
        log.warning("POLYGON news: %s", e)
        return []


def get_market_movers(direction="gainers", limit=10):
    """Get top gainers or losers. direction: 'gainers' or 'losers'."""
    c = _get_client()
    if not c:
        return []
    try:
        snap = c.get_snapshot_direction("stocks", direction, params={"limit": limit})
        results = []
        if snap:
            for s in snap:
                lt = s.last_trade
                results.append({
                    "ticker": s.ticker,
                    "price": lt.price if lt else 0,
                    "change_pct": s.todays_change_percent or 0,
                    "volume": s.day.volume if s.day else 0,
                })
        return results
    except Exception as e:
        log.warning("POLYGON movers: %s", e)
        return []


def test_connection():
    """Test Polygon API connection. Returns (success, message)."""
    if not POLYGON_API_KEY:
        return False, "POLYGON_API_KEY not set"
    c = _get_client()
    if not c:
        return False, "Client init failed"
    try:
        status = c.get_market_status()
        market = status.market if hasattr(status, 'market') else "unknown"
        return True, f"Connected — market: {market}"
    except Exception as e:
        return False, f"Connection error: {e}"
