"""Monitor every per-builder split for undistributed USDC - no local ledger.

The problem: on the CDP path, each payment lands in a per-(seller, builder) split
and *sits* until someone calls distribute. You need to see which splits are
holding funds that are ready to release.

This discovers them with zero local state, using CDP's own attribution index:

  1. ask the CDP SQL API for every builder code (`s`) that ever settled alongside
     your app code (`a`) - i.e. every builder who drove a payment to you
  2. for each, predict the pair's split address and read its live USDC balance
  3. report which splits hold distributable funds, and emit the distribute call

Needs CDP_API_KEY_ID + CDP_API_KEY_SECRET (same CDP JWT auth as cdp_sql.py) and a Base
RPC (set X402_BASE_RPC to a paid one - the balance reads add up).

    X402_BUILDER_CODE=bc_...  X402_SELLER_PAYOUT=0x...  python3 monitor.py
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import cdp_sql
import distribute
import push_split
import resolver
import split

APP_CODE = os.environ.get("X402_BUILDER_CODE", "")
SELLER_PAYOUT = os.environ.get("X402_SELLER_PAYOUT", "")
SHARE_BPS = int(os.environ.get("X402_BUILDER_SHARE_BPS", str(split.DEFAULT_BUILDER_SHARE_BPS)))
# Facilitator wallet codes are never the referer; skip anything that looks like one.
_FACIL_PREFIX = "cdp_facil"


@dataclass
class SplitStatus:
    builder_code: str
    builder_payout: Optional[str]
    split_address: Optional[str]
    deployed: bool
    balance_units: int

    @property
    def distributable_units(self) -> int:
        # A PushSplit keeps 1 unit warm; only the rest can move.
        return max(0, self.balance_units - split.SPLITS_RETAINED_UNITS)

    @property
    def needs_distribution(self) -> bool:
        return self.builder_payout is not None and self.distributable_units > 0


def discover_builder_codes(app_code: str, *, days: int = 90) -> list[str]:
    """Distinct `s` codes that settled alongside our `a`, via the CDP SQL API.

    Role-flat table, so we take every code on any tx that also carried our app
    code, then drop our own code and the facilitator's. Reorg-safe via
    ``HAVING sum(action) > 0``.
    """
    a = app_code.replace("'", "")
    sql = f"""
    SELECT DISTINCT builder_code
    FROM base.transaction_attributions
    WHERE transaction_hash IN (
      SELECT transaction_hash FROM base.transaction_attributions
      WHERE builder_code = '{a}'
        AND block_timestamp >= now() - INTERVAL {int(days)} DAY
      GROUP BY transaction_hash HAVING sum(action) > 0
    )
    GROUP BY builder_code HAVING sum(action) > 0
    """
    rows = cdp_sql.run_query(sql, max_age_ms=5000)
    codes = [r["builder_code"] for r in rows if r.get("builder_code")]
    return [c for c in codes if c != app_code and not c.startswith(_FACIL_PREFIX)]


def status_for(code: str, *, seller_payout: str, rpc_url: Optional[str] = None) -> SplitStatus:
    """Resolve a code, predict its split, and read the split's USDC balance."""
    info = resolver.resolve(code, rpc_url=rpc_url or push_split.BASE_RPC)
    payout = info.get("payout_address") if info.get("registered") else None
    if not payout:
        return SplitStatus(code, None, None, False, 0)
    plan = split.build_split_plan(seller_payout, payout, builder_code=code,
                                  builder_share_bps=SHARE_BPS)
    addr, deployed = push_split.predict_split_address(plan, rpc_url=rpc_url)
    bal = distribute.split_balance_units(addr, rpc_url=rpc_url)
    return SplitStatus(code, payout, addr, deployed, bal)


def scan(app_code: str, seller_payout: str, *, rpc_url: Optional[str] = None,
         days: int = 90) -> list[SplitStatus]:
    """Full sweep: discover builders, check each split. Sorted, fullest first."""
    codes = discover_builder_codes(app_code, days=days)
    out = [status_for(c, seller_payout=seller_payout, rpc_url=rpc_url) for c in codes]
    out.sort(key=lambda s: s.balance_units, reverse=True)
    return out


def _fmt_usd(units: int) -> str:
    return f"${units / 1_000_000:.6f}"


if __name__ == "__main__":
    if not APP_CODE or not SELLER_PAYOUT:
        print("Set X402_BUILDER_CODE (your `a`) and X402_SELLER_PAYOUT.")
        raise SystemExit(2)

    print(f"scanning splits for seller {SELLER_PAYOUT} (a={APP_CODE})…\n")
    rows = scan(APP_CODE, SELLER_PAYOUT)
    if not rows:
        print("No attributed builders found yet (CDP indexing can lag a few min).")
        raise SystemExit(0)

    pending = [r for r in rows if r.needs_distribution]
    total_pending = sum(r.distributable_units for r in pending)

    print(f"{'builder code':<22} {'balance':>12} {'distributable':>14}  {'deployed':>8}  status")
    print("─" * 74)
    for r in rows:
        flag = "◀ DISTRIBUTE" if r.needs_distribution else (
            "unregistered" if r.builder_payout is None else "-")
        print(f"{r.builder_code:<22} {_fmt_usd(r.balance_units):>12} "
              f"{_fmt_usd(r.distributable_units):>14}  {str(r.deployed):>8}  {flag}")

    print("─" * 74)
    print(f"{len(pending)} split(s) ready to distribute - {_fmt_usd(total_pending)} pending\n")

    for r in pending:
        plan = split.build_split_plan(SELLER_PAYOUT, r.builder_payout,
                                      builder_code=r.builder_code, builder_share_bps=SHARE_BPS)
        calls = distribute.distribute_calls(plan, split_address=r.split_address,
                                            deployed=r.deployed)
        print(f"# {r.builder_code} @ {r.split_address}")
        for c in calls:
            print(f"cast send {c.target} {c.data} "
                  f"--rpc-url {push_split.BASE_RPC} --private-key $KEY   # {c.step}")
        print()
