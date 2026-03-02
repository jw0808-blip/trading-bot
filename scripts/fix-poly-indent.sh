#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'FIX'
with open("main.py", "r") as f:
    code = f.read()

old_block = '''            # Polymarket needs on-chain execution \xe2\x80\x94 log as pending
            success = True
            # Route to Polymarket CLOB
            if POLYMARKET_PK:
                    token_id = opp.get("token_id", opp.get("slug", ""))
                    success, exec_msg = await execute_polymarket_order("BUY", token_id, size)
                else:
                    exec_msg = f"Polymarket not configured. Add POLYMARKET_PK to .env."'''

new_block = '''            # Route to Polymarket CLOB
            if POLYMARKET_PK:
                token_id = opp.get("token_id", opp.get("slug", ""))
                success, exec_msg = await execute_polymarket_order("BUY", token_id, size)
            else:
                success = False
                exec_msg = "Polymarket not configured. Add POLYMARKET_PK to .env."'''

if old_block in code:
    code = code.replace(old_block, new_block)
    print("Fixed Polymarket routing block")
else:
    # Try simpler match
    old2 = '''            success = True
            # Route to Polymarket CLOB
            if POLYMARKET_PK:
                    token_id'''
    if old2 in code:
        # Fix line by line
        lines = code.split('\n')
        new_lines = []
        skip_until_elif = False
        for i, line in enumerate(lines):
            if '# Route to Polymarket CLOB' in line:
                new_lines.append('            # Route to Polymarket CLOB')
                new_lines.append('            if POLYMARKET_PK:')
                new_lines.append('                token_id = opp.get("token_id", opp.get("slug", ""))')
                new_lines.append('                success, exec_msg = await execute_polymarket_order("BUY", token_id, size)')
                new_lines.append('            else:')
                new_lines.append('                success = False')
                new_lines.append('                exec_msg = "Polymarket not configured. Add POLYMARKET_PK to .env."')
                skip_until_elif = True
                continue
            if skip_until_elif:
                if line.strip().startswith('elif') or line.strip().startswith('else:'):
                    skip_until_elif = False
                    new_lines.append(line)
                # Skip old broken lines
                continue
            # Also remove the "success = True" line right before
            if 'success = True' in line and i+1 < len(lines) and '# Route to Polymarket' in lines[i+1]:
                continue
            new_lines.append(line)
        code = '\n'.join(new_lines)
        print("Fixed Polymarket routing (line-by-line)")
    else:
        print("Could not find block to fix")

with open("main.py", "w") as f:
    f.write(code)
FIX

docker compose build traderjoes-bot 2>&1 | tail -2
docker compose up -d traderjoes-bot
sleep 18
docker logs traderjoes-bot --tail 3 2>&1
