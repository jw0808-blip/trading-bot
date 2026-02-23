"""
ai_logger.py - TraderJoes Trading Firm AI Logger
Runs 24/7 as a Render Background Worker (Web Service with /health).

Features:
- Flask webhook at /log to receive conversation snippets
- Appends all entries to conversations.md in GitHub repo via API
- Posts all logs to Discord #ai-logs channel
- Logs Kalshi + Polymarket + Robinhood portfolio balance every hour
- /log/conversation endpoint for manual Claude/Grok conversation logging
- /log/trade endpoint for sub-bot trade events
"""

import os
import time
import json
import base64
import requests
import threading
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

app = Flask(__name__)

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
DISCORD_GUILD_ID = os.getenv('DISCORD_GUILD_ID', '')
GITHUB_TOKEN = os.getenv('GITHUB_TOKEN', '')
GITHUB_REPO = os.getenv('GITHUB_REPO', 'jw0808-blip/trading-bot')
GITHUB_FILE = 'conversations.md'
LOG_CHANNEL_NAME = 'ai-logs'
KALSHI_API_KEY_ID = os.getenv('KALSHI_API_KEY_ID')
KALSHI_PRIVATE_KEY_PEM = os.getenv('KALSHI_PRIVATE_KEY_PEM', '')
KALSHI_BASE = 'https://api.elections.kalshi.com/trade-api/v2'
POLY_WALLET = os.getenv('POLYMARKET_WALLET_ADDRESS', '')
RH_API_KEY = os.getenv('ROBINHOOD_API_KEY')
RH_PRIVATE_KEY = os.getenv('ROBINHOOD_PRIVATE_KEY')


def github_get_file():
    if not GITHUB_TOKEN:
        return None, None
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}'
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 200:
        data = r.json()
        content = base64.b64decode(data['content']).decode('utf-8')
        return content, data['sha']
    return None, None


def append_to_log(entry_text, commit_msg='Auto-log entry'):
    content, sha = github_get_file()
    if content is None:
        print('[LOGGER] Cannot fetch file - no GITHUB_TOKEN or file missing')
        return False
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    new_entry = f'\n---\n**{timestamp}**\n{entry_text}\n'
    new_content = content + new_entry
    url = f'https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}'
    headers = {'Authorization': f'token {GITHUB_TOKEN}', 'Accept': 'application/vnd.github.v3+json'}
    body = {
        'message': commit_msg,
        'content': base64.b64encode(new_content.encode('utf-8')).decode('utf-8'),
        'sha': sha
    }
    r = requests.put(url, headers=headers, json=body, timeout=15)
    return r.status_code in [200, 201]


_log_channel_id = None


def get_log_channel_id():
    if not DISCORD_TOKEN or not DISCORD_GUILD_ID:
        return None
    url = f'https://discord.com/api/v10/guilds/{DISCORD_GUILD_ID}/channels'
    headers = {'Authorization': f'Bot {DISCORD_TOKEN}'}
    r = requests.get(url, headers=headers, timeout=10)
    if r.status_code == 200:
        for ch in r.json():
            if ch.get('name') == LOG_CHANNEL_NAME:
                return ch['id']
    return None


def post_to_discord(message):
    global _log_channel_id
    if not DISCORD_TOKEN:
        return
    if not _log_channel_id:
        _log_channel_id = get_log_channel_id()
    if not _log_channel_id:
        return
    url = f'https://discord.com/api/v10/channels/{_log_channel_id}/messages'
    headers = {'Authorization': f'Bot {DISCORD_TOKEN}', 'Content-Type': 'application/json'}
    chunks = [message[i:i+1900] for i in range(0, len(message), 1900)]
    for chunk in chunks:
        requests.post(url, headers=headers, json={'content': chunk}, timeout=10)


def log_event(source, event_type, content):
    timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    entry = f'### [{source}] {event_type}\n_{timestamp}_\n\n{content}'
    ok = append_to_log(entry, f'Log: {source} - {event_type}')
    discord_msg = f'[{source}] {event_type} @ {timestamp}\n{content[:500]}'
    post_to_discord(discord_msg)
    print(f'[LOGGER] {source} | {event_type} | github_ok={ok}')


def get_kalshi_balance():
    try:
        if not KALSHI_PRIVATE_KEY_PEM or not KALSHI_API_KEY_ID:
            return 'Keys not configured'
        private_key = serialization.load_pem_private_key(KALSHI_PRIVATE_KEY_PEM.encode(), password=None)
        ts = str(int(time.time() * 1000))
        path = '/trade-api/v2/portfolio/balance'
        msg = ts + 'GET' + path
        sig = private_key.sign(
            msg.encode(),
            padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=hashes.SHA256.digest_size),
            hashes.SHA256()
        )
        headers = {
            'KALSHI-ACCESS-KEY': KALSHI_API_KEY_ID,
            'KALSHI-ACCESS-SIGNATURE': base64.b64encode(sig).decode(),
            'KALSHI-ACCESS-TIMESTAMP': ts
        }
        r = requests.get(f'{KALSHI_BASE}/portfolio/balance', headers=headers, timeout=10)
        if r.status_code == 200:
            bal = r.json().get('balance', 0)
            return f'${bal/100:.2f}'
        return f'Error {r.status_code}'
    except Exception as e:
        return f'Error: {str(e)[:80]}'


def get_polymarket_balance():
    try:
        if not POLY_WALLET:
            return 'No POLYMARKET_WALLET_ADDRESS env var set'
        usdc_contract = '0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174'
        wallet_clean = POLY_WALLET.lower().replace('0x', '').zfill(64)
        data = '0x70a08231000000000000000000000000' + wallet_clean
        payload = {'jsonrpc': '2.0', 'method': 'eth_call',
                   'params': [{'to': usdc_contract, 'data': data}, 'latest'], 'id': 1}
        r = requests.post('https://polygon-rpc.com', json=payload, timeout=10)
        result = r.json().get('result', '0x0')
        balance_wei = int(result, 16)
        return f'${balance_wei / 1_000_000:.2f} USDC'
    except Exception as e:
        return f'Error: {str(e)[:80]}'


def get_robinhood_balance():
    try:
        if not RH_API_KEY or not RH_PRIVATE_KEY:
            return 'Keys not configured'
        ts = str(int(time.time()))
        msg = f'{RH_API_KEY}{ts}GET/api/v1/crypto/trading/accounts/'
        pk = serialization.load_pem_private_key(RH_PRIVATE_KEY.encode(), password=None)
        sig = pk.sign(msg.encode(), padding.PKCS1v15(), hashes.SHA256())
        sig_b64 = base64.b64encode(sig).decode()
        headers = {'x-api-key': RH_API_KEY, 'x-signature': sig_b64, 'x-timestamp': ts}
        r = requests.get('https://trading.robinhood.com/api/v1/crypto/trading/accounts/',
                         headers=headers, timeout=10)
        if r.status_code == 200:
            bp = r.json().get('buying_power', 'N/A')
            return f'${bp}'
        return f'Error {r.status_code}'
    except Exception as e:
        return f'Error: {str(e)[:80]}'


def hourly_portfolio_log():
    time.sleep(10)
    while True:
        try:
            summary = (
                f'**Hourly Portfolio Snapshot**\n'
                f'- Kalshi: {get_kalshi_balance()}\n'
                f'- Polymarket: {get_polymarket_balance()}\n'
                f'- Robinhood: {get_robinhood_balance()}\n'
            )
            log_event('TraderJoes-Bot', 'Portfolio Snapshot', summary)
        except Exception as e:
            print(f'[LOGGER] Hourly log error: {e}')
        time.sleep(3600)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'service': 'ai-logger',
                    'time': datetime.now(timezone.utc).isoformat()})


@app.route('/log', methods=['POST'])
def webhook_log():
    data = request.get_json(silent=True)
    if not data or not data.get('content'):
        return jsonify({'error': 'Missing content'}), 400
    log_event(data.get('source', 'Unknown'), data.get('type', 'Message'), data['content'])
    return jsonify({'status': 'logged'})


@app.route('/log/conversation', methods=['POST'])
def log_conversation():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    platform = data.get('platform', 'Unknown')
    content = f'**User:** {data.get("user", "")}\n\n**Assistant:** {data.get("assistant", "")}'
    log_event(platform, 'Conversation', content)
    return jsonify({'status': 'logged', 'platform': platform})


@app.route('/log/trade', methods=['POST'])
def log_trade():
    data = request.get_json(silent=True)
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    log_event(data.get('bot', 'Unknown-Bot'), 'Trade Executed',
              json.dumps(data.get('trade', {}), indent=2))
    return jsonify({'status': 'logged'})


@app.route('/portfolio', methods=['GET'])
def portfolio_now():
    return jsonify({
        'kalshi': get_kalshi_balance(),
        'polymarket': get_polymarket_balance(),
        'robinhood': get_robinhood_balance(),
        'timestamp': datetime.now(timezone.utc).isoformat()
    })


if __name__ == '__main__':
    print('[LOGGER] Starting TraderJoes AI Logger...')
    threading.Thread(target=hourly_portfolio_log, daemon=True).start()
    log_event('ai-logger', 'Service Started',
              'TraderJoes AI Logger is running. Webhook at /log, /log/conversation, /log/trade')
    port = int(os.getenv('PORT', 10000))
    print(f'[LOGGER] Flask running on port {port}')
    app.run(host='0.0.0.0', port=port)
