"""Minimal affiliation tracking store — one row per paid request.

A dependency-light store holding just the affiliation columns, backed by plain
SQLite so this kit runs anywhere. In production you'd fold these columns into
whatever row you already write per paid request (a run, an order, a job) instead
of a new table.

The lifecycle of one paid request, in columns:

    request time  ──▶  record_payment(...)      # payer, s (referer), a (yours)
    after settle  ──▶  set_settle_tx(id, hash)  # from the settle observer
    when finished ──▶  set_result(id, cost, ok) # so we can compute net profit
    daily cron    ──▶  set_builder_codes(...)   # w, and any a/s missed on-chain

Two payout paths, pick one per route (see INTEGRATION.md):

  * OFF-CHAIN ledger — ``compute_payouts()`` rolls completed runs up into $ owed
    per referer code; you pay it out yourself, later. Trust-based.
  * ON-CHAIN split — ``settler.py`` carves the builder's cut at settlement.
    ``set_split()`` records what already settled on-chain (tx, split address,
    builder payout, cut) — the ledger becomes reconciliation, not an IOU.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from typing import Optional

DB_PATH = "affiliation.db"


def connect(db_path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS payments (
            id             TEXT PRIMARY KEY,
            created_at     REAL NOT NULL,
            payer_address  TEXT,
            payment_network TEXT,          -- CAIP-2, e.g. "eip155:8453"
            -- The three builder codes (see builder_code.py):
            builder_code_a TEXT,           -- YOUR app code (declared)
            builder_code_s TEXT,           -- buyer's referer code(s), comma-joined
            builder_code_w TEXT,           -- facilitator wallet code (on-chain only)
            settle_tx_hash TEXT,           -- filled by the settle observer
            price_usd      REAL NOT NULL,  -- what the buyer paid ($1.00)
            cost_usd       REAL,           -- your actual cost to serve it
            status         TEXT NOT NULL,  -- queued | completed | failed | ...
            -- ON-CHAIN split path (settler.py) — recorded when the cut was
            -- carved at settlement, so the ledger is reconciliation not an IOU:
            split_tx_hash    TEXT,         -- the deploy+fund+distribute tx
            split_address    TEXT,         -- per-(seller,builder) PushSplit
            builder_payout   TEXT,         -- resolved wallet the cut was sent to
            builder_cut_usd  REAL          -- amount pushed to the builder
        )
        """
    )
    conn.commit()
    return conn


def record_payment(
    conn: sqlite3.Connection,
    *,
    payment_id: str,
    payer_address: Optional[str],
    payment_network: Optional[str],
    builder_code_a: Optional[str],
    builder_code_s: Optional[str],
    price_usd: float,
    status: str = "queued",
) -> None:
    """Insert a row at request time with everything knowable BEFORE settlement.

    ``builder_code_a`` / ``builder_code_s`` come straight from the payment
    payload (see server_example.builder_codes_from_payment). ``w`` and
    ``settle_tx_hash`` are NULL here — they only exist on-chain after settle.
    """
    conn.execute(
        "INSERT OR IGNORE INTO payments "
        "(id, created_at, payer_address, payment_network, builder_code_a, "
        " builder_code_s, price_usd, status) VALUES (?,?,?,?,?,?,?,?)",
        (
            payment_id,
            time.time(),
            payer_address,
            payment_network,
            builder_code_a,
            builder_code_s,
            price_usd,
            status,
        ),
    )
    conn.commit()


def set_settle_tx(conn: sqlite3.Connection, payment_id: str, tx_hash: str) -> None:
    """Record the settlement tx hash (from the WSGI settle observer). Only sets
    it once — the first settle wins."""
    conn.execute(
        "UPDATE payments SET settle_tx_hash = ? "
        "WHERE id = ? AND settle_tx_hash IS NULL",
        (tx_hash[:80], payment_id),
    )
    conn.commit()


def set_split(
    conn: sqlite3.Connection,
    payment_id: str,
    *,
    split_tx_hash: str,
    split_address: Optional[str],
    builder_payout: Optional[str],
    builder_cut_usd: Optional[float],
) -> None:
    """Record an ENFORCED split that already settled on-chain (from settler.py).

    Unlike ``compute_payouts`` (which computes what you *owe*), this records what
    was *already paid*: the settlement carved the builder's cut in the same tx, so
    this row is a reconciliation receipt, not an IOU. ``split_address`` /
    ``builder_payout`` are NULL when there was no builder leg (unresolved ``s`` →
    100% to seller)."""
    conn.execute(
        "UPDATE payments SET split_tx_hash = ?, split_address = ?, "
        "builder_payout = ?, builder_cut_usd = ? WHERE id = ?",
        (split_tx_hash[:80], split_address, builder_payout, builder_cut_usd, payment_id),
    )
    conn.commit()


def set_result(conn: sqlite3.Connection, payment_id: str, *, cost_usd: float, status: str) -> None:
    """Record what it cost you to serve the request + its final status. Net
    profit (price − cost) drives the affiliate share; only completed runs pay."""
    conn.execute(
        "UPDATE payments SET cost_usd = ?, status = ? WHERE id = ?",
        (cost_usd, status, payment_id),
    )
    conn.commit()


def set_builder_codes(
    conn: sqlite3.Connection,
    payment_id: str,
    *,
    a: Optional[str] = None,
    s: Optional[str] = None,
    w: Optional[str] = None,
) -> None:
    """Write recovered on-chain codes back (from the daily backfill). ``w`` is
    authoritative and settle-only; ``a``/``s`` are gap-filled only when set."""
    sets, args = [], []
    for col, val in (("builder_code_a", a), ("builder_code_s", s), ("builder_code_w", w)):
        if val:
            sets.append(f"{col} = ?")
            args.append(val)
    if not sets:
        return
    args.append(payment_id)
    conn.execute(f"UPDATE payments SET {', '.join(sets)} WHERE id = ?", args)
    conn.commit()


def rows_needing_backfill(conn: sqlite3.Connection, since_hours: int = 48, limit: int = 500):
    """Recent rows that have a settle tx but no facilitator wallet code yet —
    exactly what the backfill reads from the chain."""
    cutoff = time.time() - since_hours * 3600
    return conn.execute(
        "SELECT * FROM payments WHERE settle_tx_hash IS NOT NULL "
        "AND builder_code_w IS NULL AND created_at >= ? "
        "ORDER BY created_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()


@dataclass
class Payout:
    builder_code: str
    completed_runs: int
    gross_usd: float       # sum of price on completed runs
    cost_usd: float        # sum of your cost on those runs
    net_profit_usd: float  # gross − cost, floored at 0 per run
    owed_usd: float        # net_profit * share


def compute_payouts(conn: sqlite3.Connection, *, share: float = 0.50) -> list[Payout]:
    """Roll completed payments up into $ owed per referer builder code (``s``).

    This is the OFF-CHAIN path: you pay these out yourself, later. If you use the
    on-chain settler (settler.py) instead, the cut was already carved at
    settlement — reconcile against ``builder_cut_usd`` rather than owing anything.


    net_profit_per_run = max(0, price_usd − cost_usd); owed = net_profit * share.
    Default ``share`` is 0.50 (a common revenue split for a buyer builder code) —
    pick whatever your program uses. The price itself already settled in full
    on-chain to your payTo; this share is paid OUT-of-band to the wallet the
    builder registered for their code.

    A row's ``s`` may be comma-joined (layered clients); each code is credited.
    """
    payouts: dict[str, Payout] = {}
    rows = conn.execute(
        "SELECT builder_code_s, price_usd, cost_usd FROM payments "
        "WHERE status = 'completed' AND builder_code_s IS NOT NULL"
    ).fetchall()
    for row in rows:
        price = row["price_usd"] or 0.0
        cost = row["cost_usd"] or 0.0
        net = max(0.0, price - cost)
        for code in (row["builder_code_s"] or "").split(","):
            code = code.strip()
            if not code:
                continue
            p = payouts.get(code) or Payout(code, 0, 0.0, 0.0, 0.0, 0.0)
            p.completed_runs += 1
            p.gross_usd += price
            p.cost_usd += cost
            p.net_profit_usd += net
            p.owed_usd += net * share
            payouts[code] = p
    return sorted(payouts.values(), key=lambda p: p.owed_usd, reverse=True)
