#!/usr/bin/env bash
cd /root/trading-bot

# Source the .env
export $(grep -v '^#' .env | grep 'ROBINHOOD' | xargs)

python3 << 'DEBUG'
import os, base64, json, uuid, datetime

api_key = os.environ.get("ROBINHOOD_API_KEY", "")
priv_key_b64 = os.environ.get("ROBINHOOD_PRIVATE_KEY", "")

print(f"API Key length: {len(api_key)}")
print(f"API Key starts: {api_key[:10]}...")
print(f"Private key b64 length: {len(priv_key_b64)}")

# Decode and check
priv_bytes = base64.b64decode(priv_key_b64)
print(f"Private key decoded bytes: {len(priv_bytes)}")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Try loading with first 32 bytes
try:
    pk = Ed25519PrivateKey.from_private_bytes(priv_bytes[:32])
    print("Key loaded OK (first 32 bytes)")
    
    # Test sign
    timestamp = int(datetime.datetime.now(tz=datetime.timezone.utc).timestamp())
    path = "/api/v1/crypto/trading/orders/"
    body = json.dumps({"test": True})
    
    message = f"{api_key}{timestamp}{path}{body}"
    print(f"Message to sign: {message[:80]}...")
    
    sig = pk.sign(message.encode("utf-8"))
    sig_b64 = base64.b64encode(sig).decode("utf-8")
    print(f"Signature: {sig_b64[:40]}...")
    print("Signing works!")
    
except Exception as e:
    print(f"Error with 32 bytes: {e}")
    
    # Try full key
    try:
        pk = Ed25519PrivateKey.from_private_bytes(priv_bytes)
        print("Key loaded OK (full bytes)")
    except Exception as e2:
        print(f"Error with full bytes: {e2}")

# Also check if there's a newline or whitespace issue
print(f"Last char of key: repr={repr(priv_key_b64[-3:])}")
DEBUG
