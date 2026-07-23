# x402aff - Python

The x402aff kit (Python): a set of flat modules that import each other by bare name, with
the one-object [`affiliation.py`](./affiliation.py) facade on top. This is the
implementation validated end-to-end on Base mainnet.

## Use

```python
from affiliation import Affiliation

aff = Affiliation(app_code="bc_yourcode", seller_payout="0xYourWallet")

# on your x402 route:
#   pay_to=aff.pay_to           per-request split, from the X-Builder-Code header
#   extensions=aff.extensions   declares your app code `a`

# release payouts, later (permissionless):
calls, balance = aff.release("bc_alice")
```

Full integration guide (money path, trust model, wiring, caveats):
[`../docs/INTEGRATION.md`](../docs/INTEGRATION.md). The TypeScript port lives in
[`../ts/`](../ts) and resolves the identical split address.

## Modules

| File | Role |
|------|------|
| `affiliation.py` | **Start here.** The `Affiliation` facade wrapping everything below. |
| `builder_code.py` | Declare `a`; decode `a`/`s`/`w` off a settle tx. |
| `resolver.py` | Builder code → registered payout address (one `eth_call`). |
| `split.py` | Builder code + price → the split plan (recipients + bps). |
| `push_split.py` | The plan → 0xSplits calldata + counterfactual address. |
| `payto.py` | Request-time `payTo` resolver. Cached, never raises. |
| `distribute.py` | The (deploy + distribute) calldata to release a funded split. |
| `monitor.py` | Which splits hold distributable funds (via CDP's index). |
| `buyer_client.py` | Buyer-side extension that attaches `s`. |
| `cdp_sql.py` · `queries.sql` | CDP SQL API client + attribution queries. |

## Develop

```bash
pip install -r requirements.txt   # requests, cbor2, pytest (+ cdp-sdk for monitor.py)
pytest                            # runs tests/ (pyproject puts python/ on the path)
```
