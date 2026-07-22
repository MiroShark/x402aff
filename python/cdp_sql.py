"""Thin client for the CDP SQL API - query Coinbase's decoded on-chain tables.

This is the alternative to hitting a raw Base RPC and hand-parsing calldata: CDP
already decodes builder-code attribution into queryable tables, so you can just
ask SQL for it. See monitor.py for the affiliation use, and queries.sql for
copy-paste queries you can run right now in the no-auth SQL Playground.

  Endpoint : POST https://api.cdp.coinbase.com/platform/v2/data/query/run
  Auth     : Authorization: Bearer <JWT>   (CDP API-key JWT, ~2 min expiry)
  Body     : {"sql": "...", "cache": {"maxAgeMs": <int>}}
  Response : {"result": [ {col: val, ...}, ... ], "schema": {...}}
  Dialect  : ClickHouse (CoinbaSeQL). Read-only SELECT. Max 10k-char query,
             50k-row result, 30s timeout.  https://docs.cdp.coinbase.com/data/sql-api

No API key needed to explore: the SQL Playground at
https://portal.cdp.coinbase.com/onchain-tools/sql-api runs the same queries in
the browser. Only the programmatic path below needs a JWT.
"""
from __future__ import annotations

import os

import requests

SQL_API_URL = "https://api.cdp.coinbase.com/platform/v2/data/query/run"
_HOST = "api.cdp.coinbase.com"
_PATH = "/platform/v2/data/query/run"


def _bearer_token() -> str:
    """Get a Bearer JWT for the SQL API.

    Two ways, in order:
      1. CDP_API_KEY_ID + CDP_API_KEY_SECRET set → mint a fresh JWT with the CDP
         SDK (`pip install cdp-sdk`). This is the production path; JWTs expire in
         ~2 min, so mint one per call.
      2. CDP_JWT set → use it verbatim (handy for a quick test: generate one in
         the portal or via the SDK and export it).

    Get keys at https://portal.cdp.coinbase.com/api-keys.
    """
    key_id = os.environ.get("CDP_API_KEY_ID")
    key_secret = os.environ.get("CDP_API_KEY_SECRET")
    if key_id and key_secret:
        # Exact helper from the CDP docs (docs.cdp.coinbase.com JWT authentication).
        from cdp.auth.utils.jwt import JwtOptions, generate_jwt

        return generate_jwt(JwtOptions(
            api_key_id=key_id,
            api_key_secret=key_secret,
            request_method="POST",
            request_host=_HOST,
            request_path=_PATH,
            expires_in=120,
        ))
    token = os.environ.get("CDP_JWT")
    if token:
        return token
    raise RuntimeError(
        "No CDP credentials. Set CDP_API_KEY_ID + CDP_API_KEY_SECRET (needs "
        "`pip install cdp-sdk`), or set CDP_JWT to a pre-generated token. "
        "Or just paste the SQL into the no-auth Playground (see queries.sql)."
    )


def run_query(sql: str, *, max_age_ms: int = 5000, timeout: float = 35.0) -> list[dict]:
    """Run a read-only SQL query and return the rows (the ``result`` array).

    ``max_age_ms`` lets the API serve a cached result when an identical query ran
    within that window (up to 900_000ms / 15m) - cheaper for repeated scans (monitor.py).
    """
    resp = requests.post(
        SQL_API_URL,
        headers={
            "Authorization": f"Bearer {_bearer_token()}",
            "Content-Type": "application/json",
        },
        json={"sql": sql, "cache": {"maxAgeMs": max_age_ms}},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json().get("result", [])


def sql_in_list(values) -> str:
    """Render a Python iterable of strings as a ClickHouse IN (...) list.

    Values here are tx hashes (0x + 64 hex) or builder codes ([a-z0-9_]) - both
    already constrained to safe characters - but we still hard-filter to those
    charsets so nothing but a hash/code can reach the query string.
    """
    import re

    safe = [v for v in values if isinstance(v, str) and re.match(r"^[0-9a-zA-Z_x]+$", v)]
    return ", ".join("'" + v + "'" for v in safe)
