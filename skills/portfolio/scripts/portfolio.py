#!/usr/bin/env python3
import os, sys, json
from datetime import datetime
try:
    import requests
except:
    pass

def portfolio():
    platforms = [
        ("Kalshi", "KALSHI_API_KEY_ID", "RSA-PSS auth"),
        ("Polymarket", "POLY_WALLET_ADDRESS", "Polygon wallet"),
        ("Robinhood", "ROBINHOOD_API_KEY", "Ed25519 auth"),
        ("Coinbase", "COINBASE_API_KEY", "CDP/ES256 JWT"),
        ("Phemex", "PHEMEX_API_KEY", "HMAC auth"),
    ]
    lines = [f"ð **Portfolio** â {datetime.utcnow().strftime('%H:%M UTC')}", ""]
    connected = 0
    for name, env_key, auth_type in platforms:
        val = os.environ.get(env_key, "")
        if val:
            lines.append(f"  ð¢ **{name}**: Connected ({auth_type})")
            connected += 1
        else:
            lines.append(f"  ð´ **{name}**: Not configured")
    # Check Polymarket wallet balance
    wallet = os.environ.get("POLY_WALLET_ADDRESS", "")
    if wallet:
        try:
            r = requests.post("https://polygon-rpc.com", json={"jsonrpc":"2.0","method":"eth_getBalance","params":[wallet,"latest"],"id":1}, timeout=10)
            wei = int(r.json().get("result","0x0"), 16)
            lines.append(f"\n  ð° Polymarket wallet: {wallet[:6]}...{wallet[-4:]} = {wei/1e18:.4f} MATIC")
        except:
            pass
    lines.append(f"\n  Connected: {connected}/5")
    return "\n".join(lines)

if __name__ == "__main__":
    print(portfolio())
