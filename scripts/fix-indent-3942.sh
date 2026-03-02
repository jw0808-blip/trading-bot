#!/usr/bin/env bash
cd /root/trading-bot

# Show context around the error
echo "=== Lines 3938-3948 ==="
sed -n '3938,3948p' main.py | cat -A

echo ""

python3 << 'FIX'
with open("main.py", "r") as f:
    lines = f.readlines()

# Fix line 3942 (0-indexed: 3941) - remove extra indentation
# Check lines around it
for i in range(3935, min(3950, len(lines))):
    stripped = lines[i].rstrip('\n')
    spaces = len(stripped) - len(stripped.lstrip())
    if "if POLYMARKET_PK:" in stripped:
        print(f"  Found problematic line {i+1}: indent={spaces}")
        # This should be at the same level as the surrounding code
        # Check what's above to determine correct indent
        for j in range(i-1, max(i-10, 0), -1):
            prev = lines[j].rstrip('\n')
            if prev.strip() and not prev.strip().startswith('#'):
                prev_indent = len(prev) - len(prev.lstrip())
                print(f"  Previous code line {j+1}: indent={prev_indent}: {prev.strip()[:60]}")
                # Fix the indent to match context
                lines[i] = ' ' * prev_indent + lines[i].lstrip()
                print(f"  Fixed to indent={prev_indent}")
                break

with open("main.py", "w") as f:
    f.writelines(lines)
print("Done")
FIX

echo ""
echo "=== After fix ==="
sed -n '3938,3948p' main.py | cat -A

echo ""
docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18
docker logs traderjoes-bot --tail 3 2>&1
