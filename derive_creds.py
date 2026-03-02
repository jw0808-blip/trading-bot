from py_clob_client.client import ClobClient
import os

pk = os.environ.get("POLYMARKET_PK", "")
funder = os.environ.get("POLYMARKET_FUNDER", "")

print("PK found:", bool(pk))
print("Funder found:", bool(funder))

client = ClobClient(
    "https://clob.polymarket.com",
    key=pk,
    chain_id=137,
    signature_type=0,
    funder=funder,
)

creds = client.create_or_derive_api_creds()
print("API_KEY:", creds.api_key)
print("API_SECRET:", creds.api_secret)
print("PASSPHRASE:", creds.api_passphrase)
