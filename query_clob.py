import os
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

pk = os.environ.get('POLYMARKET_PK', '').strip()
funder = os.environ.get('POLYMARKET_FUNDER', '').strip()
api_key = os.environ.get('POLYMARKET_API_KEY', '').strip()
api_secret = os.environ.get('POLYMARKET_API_SECRET', '').strip()
passphrase = os.environ.get('POLYMARKET_PASSPHRASE', '').strip()

print('Funder:', funder)
print('API Key:', api_key[:10] + '...')

client = ClobClient(
    host='https://clob.polymarket.com',
    key=pk,
    chain_id=137,
    signature_type=0,
    funder=funder,
)
creds = ApiCreds(api_key=api_key, api_secret=api_secret, api_passphrase=passphrase)
client.set_api_creds(creds)

print('\n--- COLLATERAL balance ---')
try:
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL, signature_type=0)
    r = client.get_balance_allowance(params)
    print('Result:', r)
except Exception as e:
    print('Error:', e)

print('\n--- Available methods ---')
methods = [m for m in dir(client) if not m.startswith('_')]
print(methods)

print('\n--- Open orders ---')
try:
    orders = client.get_orders()
    print('Orders:', orders)
except Exception as e:
    print('Error:', e)

print('\n--- Trades ---')
try:
    trades = client.get_trades()
    if isinstance(trades, list):
        print('Trade count:', len(trades))
        for t in trades[:3]:
            print(' ', t)
    else:
        print('Trades:', trades)
except Exception as e:
    print('Error:', e)
