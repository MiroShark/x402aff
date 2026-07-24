# x402aff - Python

The x402aff kit (Python): an installable `x402aff` package with the one-object
[`Affiliation`](./x402aff/affiliation.py) facade on top. This is the
implementation validated end-to-end on Base mainnet.

## Install

```bash
pip install x402aff                  # from PyPI
pip install 'x402aff[cdp]'           # with the CDP discovery extra
```

Or from a checkout / straight from git, to track unreleased changes:

```bash
pip install ./python                 # from a clone, at the repo root
pip install '.[cdp]'                 # from inside python/, with the CDP extra
pip install 'x402aff[cdp] @ git+https://github.com/MiroShark/x402aff@main#subdirectory=python'
```

The `cdp` extra adds `cdp-sdk`, needed only for the CDP-index discovery path
(`monitor`, `aff.scan()` / `pending()` / `splits_payload()`). Without it the
request-time `payTo` and payout-calldata paths work fine.

Vendoring instead of installing works too - copy `python/x402aff/` into your
project as a subpackage; the modules import each other relatively, so nothing
needs rewriting and none of the generic names (`split`, `monitor`, `resolver`)
leak into your top-level namespace.

## Use

```python
from x402aff import Affiliation

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
| `__init__.py` | The package's public surface (`from x402aff import Affiliation`, …). |
| `affiliation.py` | **Start here.** The `Affiliation` facade wrapping everything below. |
| `builder_code.py` | Declare `a`; decode `a`/`s`/`w` off a settle tx. |
| `resolver.py` | Builder code → registered payout address (one `eth_call`). |
| `split.py` | Builder code + price → the split plan (recipients + bps). |
| `push_split.py` | The plan → 0xSplits calldata + counterfactual address. |
| `payto.py` | Request-time `payTo` resolver. Cached, never raises. |
| `distribute.py` | The (deploy + distribute) calldata to release a funded split. |
| `monitor.py` | Which splits hold distributable funds (via CDP's index). |
| `buyer_client.py` | Buyer-side extension that attaches `s` (via `marked_service_codes`). |
| `cdp_sql.py` · `queries.sql` | CDP SQL API client + attribution queries. |

## Develop

```bash
pip install -e '.[cdp,dev]'   # editable install with the CDP + test extras
pytest                        # runs tests/ against the x402aff package
```
