#!/usr/bin/env bash
cd /root/trading-bot

python3 << 'PYFIX'
with open('docker-compose.yml', 'r') as f:
    lines = f.readlines()

new_lines = []
skip_until_next_service = False
in_openclaw = False

for i, line in enumerate(lines):
    if line.strip().startswith('openclaw:') and not line.strip().startswith('traderjoes'):
        in_openclaw = True
        # Write new openclaw service
        new_lines.append('  openclaw:\n')
        new_lines.append('    build:\n')
        new_lines.append('      context: .\n')
        new_lines.append('      dockerfile: Dockerfile.openclaw\n')
        new_lines.append('    container_name: traderjoes-openclaw\n')
        new_lines.append('    restart: unless-stopped\n')
        new_lines.append('    ports:\n')
        new_lines.append('      - "3000:3000"\n')
        new_lines.append('    logging:\n')
        new_lines.append('      driver: json-file\n')
        new_lines.append('      options:\n')
        new_lines.append('        max-size: "10m"\n')
        new_lines.append('        max-file: "3"\n')
        skip_until_next_service = True
        continue

    if skip_until_next_service:
        # Check if we hit the next service or volumes section
        if (line.startswith('  ') and not line.startswith('    ') and line.strip() and not line.strip().startswith('#')) or line.startswith('volumes:'):
            skip_until_next_service = False
            new_lines.append(line)
        continue

    # Remove skills-venv volume reference
    if 'skills-venv' in line:
        continue

    new_lines.append(line)

with open('docker-compose.yml', 'w') as f:
    f.writelines(new_lines)
print('docker-compose.yml fixed')
PYFIX

echo "Rebuilding openclaw..."
docker compose down openclaw 2>/dev/null
docker compose build --no-cache openclaw
docker compose up -d openclaw
sleep 20

echo "Testing..."
curl -s http://localhost:3000/health
echo ""
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
