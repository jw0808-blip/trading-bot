# Exchange API Key IP Whitelisting Guide
## Hetzner VPS IP: 89.167.108.136

### Coinbase
1. Go to https://www.coinbase.com/settings/api
2. Click your API key → IP Whitelist → add: 89.167.108.136
3. Disable "Allow all IPs" → Save

### Phemex
1. https://phemex.com → Account → API Management
2. Edit key → IP Restriction → Trusted IPs only → add: 89.167.108.136

### Kalshi
1. https://kalshi.com → Settings → API
2. IP restrictions → add: 89.167.108.136

### Polymarket
- Uses wallet-based auth (private key signing)
- No IP whitelisting needed — keep private key ONLY in .env on VPS

### Alpaca
1. https://app.alpaca.markets → API Keys → Regenerate with IP restriction: 89.167.108.136

### IBKR
- Client Portal Gateway binds to localhost — inherently IP-restricted

### After whitelisting, verify:
!test-execution coinbase 1
!test-execution phemex 1
!test-execution kalshi 1
!test-execution polymarket 1
