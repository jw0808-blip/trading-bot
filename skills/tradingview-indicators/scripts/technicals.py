#!/usr/bin/env python3
"""TraderJoes EchoEdge — Technical Indicators. Fetches crypto prices + computes RSI, MACD, BBands."""

import sys, json, requests
import numpy as np


def get_prices(coin_id="bitcoin", days=90):
    """Fetch daily prices from CoinGecko."""
    url = f"https://api.coingecko.com/api/v3/coins/{coin_id}/market_chart"
    resp = requests.get(url, params={"vs_currency": "usd", "days": days}, timeout=15)
    data = resp.json()
    prices = [p[1] for p in data.get("prices", [])]
    return np.array(prices)


def sma(prices, period):
    if len(prices) < period: return np.array([])
    return np.convolve(prices, np.ones(period)/period, mode='valid')


def ema(prices, period):
    if len(prices) < period: return np.array([])
    result = np.zeros(len(prices))
    result[0] = prices[0]
    k = 2 / (period + 1)
    for i in range(1, len(prices)):
        result[i] = prices[i] * k + result[i-1] * (1-k)
    return result


def rsi(prices, period=14):
    if len(prices) < period + 1: return None
    deltas = np.diff(prices)
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = np.mean(gains[-period:])
    avg_loss = np.mean(losses[-period:])
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal: return None, None, None
    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)
    macd_line = fast_ema - slow_ema
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return round(float(macd_line[-1]), 2), round(float(signal_line[-1]), 2), round(float(histogram[-1]), 2)


def bollinger(prices, period=20, std_dev=2):
    if len(prices) < period: return None, None, None
    middle = sma(prices, period)
    std = np.std(prices[-period:])
    upper = middle[-1] + std_dev * std
    lower = middle[-1] - std_dev * std
    return round(float(upper), 2), round(float(middle[-1]), 2), round(float(lower), 2)


def analyze(coin_id="bitcoin", days=90):
    prices = get_prices(coin_id, days)
    if len(prices) < 30:
        return f"'c Not enough price data for {coin_id}"
    
    current = prices[-1]
    high_30d = max(prices[-30:])
    low_30d = min(prices[-30:])
    change_7d = ((prices[-1] / prices[-7]) - 1) * 100 if len(prices) >= 7 else 0
    change_30d = ((prices[-1] / prices[-30]) - 1) * 100 if len(prices) >= 30 else 0
    
    rsi_val = rsi(prices)
    macd_val, signal_val, hist_val = macd(prices)
    bb_upper, bb_mid, bb_lower = bollinger(prices)
    sma_50 = float(sma(prices, 50)[-1]) if len(prices) >= 50 else None
    sma_200 = float(sma(prices, 200)[-1]) if len(prices) >= 200 else None
    
    signals = []
    if rsi_val:
        if rsi_val > 70: signals.append('RSI Overbought')
        elif rsi_val < 30: signals.append('RSI Oversold')
        else: signals.append('RSI Neutral')
    print('Done')

if __name__ == '__main__':
    coin = sys.argv[1] if len(sys.argv) > 1 else 'bitcoin'
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 90
    print(analyze(coin, days))
