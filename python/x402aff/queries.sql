-- CDP SQL API - builder-code / x402 attribution queries.
--
-- Run these with ZERO setup in the SQL Playground (just sign in to CDP Portal):
--   https://portal.cdp.coinbase.com/onchain-tools/sql-api
-- Or programmatically: POST https://api.cdp.coinbase.com/platform/v2/data/query/run
--   Authorization: Bearer <JWT>   body: {"sql": "<one query>", "cache": {"maxAgeMs": 5000}}
--
-- Dialect: ClickHouse (CoinbaSeQL), read-only SELECT only. Every Base table
-- carries `action` (+1 added / -1 removed) - use `HAVING sum(action) > 0` (or
-- `WHERE action = 1`) so re-orged rows don't count. Docs: docs.cdp.coinbase.com/data/sql-api
--
-- Two attribution tables:
--   base.transaction_attributions   builder_code  (one code per row, keyed by transaction_hash)
--   base.decoded_user_operations    builder_codes (Array - for ERC-4337 userOp settlements)
-- Both are role-FLAT: they give the SET of codes on a tx, not which is a/w/s.


-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Codes on specific settlement transactions (the reconcile query).
--    Swap in your recorded settle_tx_hash values.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT transaction_hash, groupArray(builder_code) AS codes
FROM base.transaction_attributions
WHERE transaction_hash IN (
  '0xYOUR_SETTLE_TX_HASH_1',
  '0xYOUR_SETTLE_TX_HASH_2'
)
GROUP BY transaction_hash
HAVING sum(action) > 0;


-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Every settlement that carried YOUR referer code, last 7 days.
--    "Which runs am I owed a share on?" - reconcile against your own records.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT block_timestamp, transaction_hash, builder_code
FROM base.transaction_attributions
WHERE builder_code = 'bc_yourcode'
  AND block_timestamp >= now() - INTERVAL 7 DAY
  AND action = 1
ORDER BY block_timestamp DESC
LIMIT 1000;


-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Attribution leaderboard - attributed settlements per builder code, 30 days.
--    A full analytics rollup with no per-run lookups at all.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT builder_code, count() AS attributed_txs
FROM base.transaction_attributions
WHERE block_timestamp >= now() - INTERVAL 30 DAY
  AND action = 1
GROUP BY builder_code
ORDER BY attributed_txs DESC
LIMIT 50;


-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Same, for ERC-4337 userOp settlements (smart-account / bundler path).
--    builder_codes is an Array, so arrayJoin fans it out to one row per code.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT transaction_hash, arrayJoin(builder_codes) AS builder_code
FROM base.decoded_user_operations
WHERE transaction_hash IN ('0xYOUR_SETTLE_TX_HASH_1')
  AND action = 1;


-- ─────────────────────────────────────────────────────────────────────────────
-- 5. Every kit-routed payment, ECOSYSTEM-WIDE (the AFFILIATION_MARKER path).
--    The kit's buyer extension stamps a hardcoded, shared marker (`x402aff`) as a
--    second `s`, so every payment that used this kit self-identifies - no payTo
--    reconstruction, and it includes splits funded but never deployed. The marker
--    is the same for every kit install, so this one query finds them all.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT DISTINCT transaction_hash
FROM base.transaction_attributions
WHERE builder_code = 'x402aff'
  AND action = 1;


-- ─────────────────────────────────────────────────────────────────────────────
-- 5b. …with each payment's payTo (the split) + USDC amount, by joining the
--     Transfer log on the same tx. Group by pay_to for a per-split rollup; the
--     contracts among these are your kit splits, a direct-to-seller payTo is an
--     EOA (see docs/INTEGRATION.md).
--
--     ⚠ DOES NOT RUN ON THE CDP SQL API - kept for reference only. The intent
--     was that `block_number IN (...)` would restrict the base.events scan to
--     only the blocks carrying a marked tx, but the planner does not push that
--     bound down: the query still scans base.events by USDC and trips the API's
--     leaf-scan limit. Measured live on Base mainnet, 2026-07-24:
--
--       400 invalid_request
--       "Limit for rows or bytes to read on leaf node exceeded,
--        max bytes: 93.13 GiB, current bytes: 94.44 GiB"
--
--     Both scopes fail, and narrowing does not help - the floor is the USDC
--     Transfer scan itself, which alone is already over the limit:
--       - as written above (marker scope, no time bound) ...... 94.44 GiB
--       - scoped to one seller's `a` + a 90-day bound ......... 93.99 GiB
--     Query #5 above (same attribution set, no base.events join) returns 200.
--
--     This is why `aff.splits_payload()` carries NO per-split payment count: the
--     kit ran this scoped to one seller in monitor.discover_split_rollup, and it
--     400'd on every call. Do not reintroduce it without re-measuring live.
--
--     For a payment COUNT, use #5c below instead - no events join, and confirmed
--     working. The received USDC AMOUNT has no cheap source: it only exists in
--     the Transfer log, which is the thing that blows the limit.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT
  toString(e.parameters['to'])                          AS pay_to,
  count()                                                AS payments,
  sum(toUInt256OrZero(toString(e.parameters['value']))) AS total_units
FROM base.events e
WHERE e.event_name = 'Transfer'
  AND lower(e.address) = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
  AND e.action = 1
  AND e.transaction_hash IN (
    SELECT transaction_hash FROM base.transaction_attributions
    WHERE builder_code = 'x402aff' AND action = 1
  )
  AND e.block_number IN (
    SELECT block_number FROM base.transaction_attributions
    WHERE builder_code = 'x402aff' AND action = 1
  )
GROUP BY pay_to
ORDER BY total_units DESC;


-- ─────────────────────────────────────────────────────────────────────────────
-- 5c. Payment COUNT per builder, the cheap way: attributions only, no join onto
--     base.events. This is what #5b was wanted for, minus the USDC amount.
--     Because a split is per (seller, builder) pair, a count keyed by builder
--     code IS the per-split count. Swap 'bc_yourcode' for your own `a`.
--
--     Confirmed live on Base mainnet 2026-07-24 (200, 4 rows) where #5b 400s.
--     Rows come back as {builder_code, payments}; your own `a` and the
--     facilitator (cdp_facil*) appear too - filter them the way
--     monitor.discover_builder_codes does.
-- ─────────────────────────────────────────────────────────────────────────────
SELECT builder_code, count(DISTINCT transaction_hash) AS payments
FROM base.transaction_attributions
WHERE transaction_hash IN (
  SELECT transaction_hash FROM base.transaction_attributions
  WHERE builder_code = 'bc_yourcode' AND action = 1
    AND block_timestamp >= now() - INTERVAL 90 DAY
)
GROUP BY builder_code;
