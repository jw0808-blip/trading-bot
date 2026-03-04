#!/usr/bin/env python3
"""Interactive key setup for TraderJoes Trading Firm.
Run: python3 /root/trading-bot/setup_keys.py
"""
import os

KEYS_DIR = "/root/trading-bot/keys"
ENV_FILE = "/root/trading-bot/.env"

os.makedirs(KEYS_DIR, exist_ok=True)

def save_pem(filename, label):
    path = os.path.join(KEYS_DIR, filename)
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    print("Paste your key below (the full block including BEGIN/END lines).")
    print("When done, type END on its own line and press Enter.\n")
    lines = []
    while True:
        line = input()
        if line.strip() == "END":
            break
        # Fix literal \n that copy-paste sometimes introduces
        line = line.replace("\\n", "\n")
        lines.append(line)
    raw = "\n".join(lines).strip()
    # If the whole key came as one line with spaces, try to reconstruct
    if "-----BEGIN" in raw and raw.count("\n") < 3:
        # It's all on one line - split the base64 into 64-char chunks
        parts = raw.split("-----")
        if len(parts) >= 5:
            header = f"-----{parts[1]}-----"
            footer = f"-----{parts[3]}-----"
            body = parts[2].strip().replace(" ", "")
            chunked = "\n".join([body[i:i+64] for i in range(0, len(body), 64)])
            raw = f"{header}\n{chunked}\n{footer}"
    with open(path, "w") as f:
        f.write(raw + "\n")
    os.chmod(path, 0o600)
    print(f"Saved to {path} ({len(raw)} bytes)")
    # Verify it looks right
    with open(path) as f:
        content = f.read()
    if "-----BEGIN" in content and "\n" in content.split("-----")[2]:
        print("PEM format looks correct.")
    else:
        print("WARNING: PEM may not be formatted correctly. Check the file.")

def setup_env_key(key_name, prompt_text):
    value = input(f"\n{prompt_text}: ").strip()
    if not value:
        print(f"Skipped {key_name}")
        return
    # Read existing .env
    env_lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE) as f:
            env_lines = f.readlines()
    # Update or append
    found = False
    for i, line in enumerate(env_lines):
        if line.startswith(f"{key_name}="):
            env_lines[i] = f"{key_name}={value}\n"
            found = True
            break
    if not found:
        env_lines.append(f"{key_name}={value}\n")
    with open(ENV_FILE, "w") as f:
        f.writelines(env_lines)
    print(f"Set {key_name} in .env")

print("\n" + "="*60)
print("  TraderJoes Key Setup Wizard")
print("="*60)
print("\nThis will set up your API keys for Coinbase, Kalshi, and Alpaca.")
print("Your .env file and PEM files will be updated automatically.\n")

# Coinbase PEM
resp = input("Set up Coinbase PEM key? (y/n): ").strip().lower()
if resp == "y":
    save_pem("coinbase.pem", "COINBASE PEM KEY")

# Kalshi PEM
resp = input("\nSet up Kalshi PEM key? (y/n): ").strip().lower()
if resp == "y":
    save_pem("kalshi.pem", "KALSHI PEM KEY")

# Alpaca
resp = input("\nSet up Alpaca API keys? (y/n): ").strip().lower()
if resp == "y":
    setup_env_key("ALPACA_API_KEY", "Alpaca API Key ID")
    setup_env_key("ALPACA_SECRET_KEY", "Alpaca Secret Key")
    setup_env_key("ALPACA_BASE_URL", "Alpaca Base URL (press Enter for paper: https://paper-api.alpaca.markets)")
    # Set default if empty
    with open(ENV_FILE) as f:
        content = f.read()
    if "ALPACA_BASE_URL=\n" in content or "ALPACA_BASE_URL= \n" in content:
        content = content.replace("ALPACA_BASE_URL=\n", "ALPACA_BASE_URL=https://paper-api.alpaca.markets\n")
        with open(ENV_FILE, "w") as f:
            f.write(content)

# Verify
print("\n" + "="*60)
print("  VERIFICATION")
print("="*60)
for f in ["coinbase.pem", "kalshi.pem"]:
    path = os.path.join(KEYS_DIR, f)
    if os.path.exists(path):
        size = os.path.getsize(path)
        with open(path) as fh:
            content = fh.read()
        has_begin = "-----BEGIN" in content
        linecount = content.count("\n")
        print(f"  {f}: {size} bytes, {linecount} lines, BEGIN header: {has_begin}")
    else:
        print(f"  {f}: NOT FOUND")

print(f"\n.env keys:")
if os.path.exists(ENV_FILE):
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                key = line.split("=")[0]
                val = line.split("=", 1)[1] if "=" in line else ""
                masked = val[:4] + "..." if len(val) > 4 else val
                print(f"  {key}={masked}")

print("\nDone! Now restart the bot:")
print("  cd /root/trading-bot && docker compose down && docker compose up -d --build")
