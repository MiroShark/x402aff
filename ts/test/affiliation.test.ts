/**
 * Tests for the TypeScript Affiliation facade - offline (mock viem transport).
 *
 * The calldata cross-check below asserts the TS encoder produces the SAME bytes
 * as `cast` / the Python kit (test_push_split.py), so a TS seller and a Python
 * seller land on the identical split address.
 *
 *   npm test        (node --test with type stripping)
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createPublicClient, custom } from "viem";
import { base } from "viem/chains";
import type { PublicClient } from "viem";

import {
  Affiliation,
  toTokenId,
  primaryCode,
  buildSplitPlan,
  amountsUnits,
  distributableUnits,
  payoutUnits,
  isClaimable,
  planIsClaimable,
  isDeployedCalldata,
  distributeCalldata,
  createSplitCalldata,
  BUILDER_CODES_REGISTRY,
  SPLITS_PUSH_FACTORY,
  USDC_BASE,
} from "../src/affiliation.ts";

const SELLER = "0x2222222222222222222222222222222222222222" as const;
const BUILDER = "0x1111111111111111111111111111111111111111" as const;
const SPLIT = "0x3773000000000000000000000000000000002e38" as const;

// isDeployed((address[],uint256[],uint256,uint16),address,bytes32) for
// recipients [BUILDER, SELLER], allocations [1000, 9000] - generated with
// `cast calldata` and verified live against the Base factory in test_push_split.py.
const CAST_IS_DEPLOYED =
  "0xcd6bc121" +
  "0000000000000000000000000000000000000000000000000000000000000060" +
  "0000000000000000000000000000000000000000000000000000000000000000" +
  "0000000000000000000000000000000000000000000000000000000000000000" +
  "0000000000000000000000000000000000000000000000000000000000000080" +
  "00000000000000000000000000000000000000000000000000000000000000e0" +
  "0000000000000000000000000000000000000000000000000000000000002710" +
  "0000000000000000000000000000000000000000000000000000000000000000" +
  "0000000000000000000000000000000000000000000000000000000000000002" +
  "0000000000000000000000001111111111111111111111111111111111111111" +
  "0000000000000000000000002222222222222222222222222222222222222222" +
  "0000000000000000000000000000000000000000000000000000000000000002" +
  "00000000000000000000000000000000000000000000000000000000000003e8" +
  "0000000000000000000000000000000000000000000000000000000000002328";

// The encoded Split tuple alone (drop the selector + the 3 outer head words:
// offset, owner, salt). Every call shares this exact tuple, so they all land on
// the same address.
const SPLIT_TUPLE = CAST_IS_DEPLOYED.slice(2 + 8 + 3 * 64);

const plan = () => buildSplitPlan(SELLER, BUILDER, "bc_alice", 1000);

// ── mock transport ────────────────────────────────────────────────────────────
function word(hexOrAddr: string): string {
  return hexOrAddr.replace(/^0x/, "").toLowerCase().padStart(64, "0");
}
function uintWord(n: bigint): string {
  return n.toString(16).padStart(64, "0");
}

function mockClient(opts: {
  payout?: string | null;
  deployed?: boolean;
  balance?: bigint;
  throwRegistry?: boolean;
}): PublicClient {
  const transport = custom({
    async request({ method, params }: { method: string; params: any }) {
      if (method === "eth_chainId") return "0x2105";
      if (method !== "eth_call") throw new Error(`unexpected method ${method}`);
      const to = String(params[0].to).toLowerCase();
      if (to === BUILDER_CODES_REGISTRY.toLowerCase()) {
        if (opts.throwRegistry) throw new Error("boom: RPC unavailable");
        if (opts.payout == null) return "0x"; // unregistered → zero-data revert
        return "0x" + word(opts.payout);
      }
      if (to === SPLITS_PUSH_FACTORY.toLowerCase()) {
        return "0x" + word(SPLIT) + uintWord(opts.deployed ? 1n : 0n);
      }
      if (to === USDC_BASE.toLowerCase()) {
        return "0x" + uintWord(opts.balance ?? 0n);
      }
      throw new Error(`unexpected eth_call to ${to}`);
    },
  });
  return createPublicClient({ chain: base, transport }) as PublicClient;
}

function aff(client?: PublicClient, builderShareBps?: number): Affiliation {
  return new Affiliation({ appCode: "bc_seller", sellerPayout: SELLER, client, builderShareBps });
}

// ── pure helpers ──────────────────────────────────────────────────────────────

test("toTokenId reads ASCII bytes as a big-endian int", () => {
  assert.equal(toTokenId("a"), 97n);
  assert.equal(toTokenId("ab"), 0x6162n);
  // matches resolver.py's live-verified value for leap_wallet
  assert.equal(toTokenId("leap_wallet"), 131042744964646850211374452n);
  assert.throws(() => toTokenId("BAD CODE"));
});

test("primaryCode takes the first valid code from a comma-joined s", () => {
  assert.equal(primaryCode("bc_alice,bc_bob"), "bc_alice");
  assert.equal(primaryCode("  , bc_bob"), "bc_bob");
  assert.equal(primaryCode(""), null);
  assert.equal(primaryCode(null), null);
});

test("isDeployed calldata matches cast byte-for-byte (same address as Python)", () => {
  assert.equal(isDeployedCalldata(plan()), CAST_IS_DEPLOYED);
});

test("create calldata carries the deploy selector + the same split tuple", () => {
  const data = createSplitCalldata(plan());
  assert.ok(data.startsWith("0xf79918b0"));
  // same encoded Split tuple as isDeployed → lands on the predicted address
  assert.ok(data.endsWith(SPLIT_TUPLE));
});

test("distribute calldata targets USDC + carries the same split tuple", () => {
  const data = distributeCalldata(plan());
  assert.ok(data.startsWith("0x2d3f5537"));
  assert.ok(data.toLowerCase().includes(USDC_BASE.slice(2).toLowerCase()));
  assert.ok(data.endsWith(SPLIT_TUPLE));
});

test("amountsUnits mirrors 0xSplits (retain 1, floor each share)", () => {
  const a = amountsUnits(plan(), 1_000_000n);
  assert.equal(a.get(BUILDER), 99999n); // 0.099999
  assert.equal(a.get(SELLER), 899999n); // 0.899999
  const total = [...a.values()].reduce((s, v) => s + v, 0n);
  assert.equal(1_000_000n - total, 2n); // 2 units dust
});

// ── facade: extensions + header extraction ────────────────────────────────────

test("extensions declares the app code `a`", () => {
  assert.deepEqual(aff().extensions, {
    "builder-code": {
      info: { a: "bc_seller" },
      schema: {
        type: "object",
        properties: {
          a: { type: "string", pattern: "^[a-z0-9_]{1,32}$" },
          w: { type: "string", pattern: "^[a-z0-9_]{1,32}$" },
          s: { type: "array", items: { type: "string", pattern: "^[a-z0-9_]{1,32}$" } },
        },
        additionalProperties: false,
      },
    },
  });
});

test("codeFromHeaders reads X-Builder-Code from Headers / Map / object", () => {
  const a = aff();
  assert.equal(a.codeFromHeaders(new Headers({ "x-builder-code": "bc_alice" })), "bc_alice");
  assert.equal(a.codeFromHeaders(new Map([["X-Builder-Code", "bc_alice"]])), "bc_alice");
  assert.equal(a.codeFromHeaders({ "X-Builder-Code": "bc_bob,bc_alice" }), "bc_bob");
  assert.equal(a.codeFromHeaders({}), null);
});

// ── facade: safe fallback (no network) ────────────────────────────────────────

test("no code falls back to the seller, unsplit", async () => {
  const a = aff(); // real http client, but never called: null code short-circuits
  const pt = await a.resolve(null);
  assert.equal(pt.address, SELLER);
  assert.equal(pt.attributed, false);
  assert.equal(await a.payToFor(null), SELLER);
  assert.equal(await a.payToFor(new Headers()), SELLER);
});

// ── facade: attributed path (mock transport) ──────────────────────────────────

test("registered code routes payTo to the pair's split", async () => {
  const a = aff(mockClient({ payout: BUILDER, deployed: false }));
  const pt = await a.resolve("bc_alice");
  // viem returns an EIP-55 checksummed address; compare case-insensitively.
  assert.equal(pt.address.toLowerCase(), SPLIT);
  assert.equal(pt.attributed, true);
  assert.equal(pt.splitDeployed, false);
  assert.equal(pt.plan.hasBuilder, true);
  const viaHeader = await a.payToFor(new Headers({ "x-builder-code": "bc_alice" }));
  assert.equal(viaHeader.toLowerCase(), SPLIT);
});

test("unregistered code (empty return) falls back to seller, no error", async () => {
  const a = aff(mockClient({ payout: null }));
  const pt = await a.resolve("bc_ghost");
  assert.equal(pt.address, SELLER);
  assert.equal(pt.attributed, false);
  assert.equal(pt.error, undefined);
});

test("a network failure falls back to seller AND records the error", async () => {
  const a = aff(mockClient({ throwRegistry: true }));
  const pt = await a.resolve("bc_alice");
  assert.equal(pt.address, SELLER);
  assert.equal(pt.attributed, false);
  assert.ok(pt.error && /boom|RPC/i.test(pt.error));
});

test("an unregistered result is NOT cached (builder registers later, gets picked up)", async () => {
  // Registry answers "unregistered" first, then "registered" - as if the builder
  // registers their code after their first payment. A cached miss would strand
  // their cut for the process life; only positive resolutions are memoized.
  let registered = false;
  const transport = custom({
    async request({ method, params }: { method: string; params: any }) {
      if (method === "eth_chainId") return "0x2105";
      const to = String(params[0].to).toLowerCase();
      if (to === BUILDER_CODES_REGISTRY.toLowerCase()) return registered ? "0x" + word(BUILDER) : "0x";
      if (to === SPLITS_PUSH_FACTORY.toLowerCase()) return "0x" + word(SPLIT) + uintWord(0n);
      return "0x" + uintWord(0n);
    },
  });
  const a = aff(createPublicClient({ chain: base, transport }) as PublicClient);

  const first = await a.resolve("bc_late");
  assert.equal(first.attributed, false); // not registered yet → seller, unsplit
  assert.equal(first.address, SELLER);

  registered = true;
  const second = await a.resolve("bc_late");
  assert.equal(second.attributed, true); // now resolves - not stuck on a cached miss
  assert.equal(second.address.toLowerCase(), SPLIT);
});

// ── facade: payout path ───────────────────────────────────────────────────────

test("balance reads the split's USDC balance", async () => {
  const a = aff(mockClient({ payout: BUILDER, deployed: true, balance: 12_400_000n }));
  assert.equal(await a.balance("bc_alice"), 12_400_000n);
});

test("balance is 0 for an unattributed code", async () => {
  const a = aff(mockClient({ payout: null }));
  assert.equal(await a.balance("bc_ghost"), 0n);
});

test("release builds deploy + distribute when the split isn't deployed", async () => {
  const a = aff(mockClient({ payout: BUILDER, deployed: false, balance: 12_400_000n }));
  const { calls, balanceUnits } = await a.release("bc_alice");
  assert.equal(balanceUnits, 12_400_000n);
  assert.deepEqual(calls.map((c) => c.step), ["deploy_split", "distribute"]);
  assert.equal(calls[0].target, SPLITS_PUSH_FACTORY);
  assert.equal(calls[1].target.toLowerCase(), SPLIT);
});

test("release skips the deploy leg once the split exists", async () => {
  const a = aff(mockClient({ payout: BUILDER, deployed: true, balance: 5_000_000n }));
  const { calls } = await a.release("bc_alice");
  assert.deepEqual(calls.map((c) => c.step), ["distribute"]);
});

test("a custom builder share changes the split address it resolves", async () => {
  // different bps → different allocations → different CREATE2 address, so the two
  // facades must not collide on the cache key or the predicted address input.
  const client = mockClient({ payout: BUILDER, deployed: false });
  const ten = aff(client, 1000);
  const twentyFive = aff(client, 2500);
  const p10 = (await ten.resolve("bc_alice")).plan;
  const p25 = (await twentyFive.resolve("bc_alice")).plan;
  assert.equal(p10.recipients[0][1], 1000);
  assert.equal(p25.recipients[0][1], 2500);
  assert.notEqual(isDeployedCalldata(p10), isDeployedCalldata(p25));
});

test("splitsPayload shapes rows and filters the marker", async () => {
  const a = aff(mockClient({ payout: BUILDER, deployed: false, balance: 1_000_000n }));
  // Injected CDP query. The marker rides along as a "builder" and must be
  // dropped; the app code + facilitator are dropped by discover.
  const query = async (sql: string) => {
    // Regression: the per-split payments/received rollup joined base.events and
    // tripped the CDP SQL API's leaf-scan limit (400) on every call - see
    // queries.sql #5b. splitsPayload must never issue that query.
    assert.ok(!sql.includes("base.events"), "splitsPayload ran an unexpected base.events query");
    return [
      { builder_code: "bc_alice" },
      { builder_code: "x402aff" },
      { builder_code: "bc_seller" },
      { builder_code: "cdp_facil1" },
    ];
  };
  const payload = await a.splitsPayload(query);
  assert.equal(payload.configured, true);
  assert.equal(payload.marker, "x402aff");
  assert.equal(payload.count, 1); // marker + appCode + facilitator filtered out
  const s = payload.splits[0];
  assert.equal(s.payTo.toLowerCase(), SPLIT.toLowerCase());
  assert.equal(s.sellerCode, "bc_seller");
  assert.equal(s.builderCode, "bc_alice");
  assert.equal(s.balanceUnits, "1000000");
  assert.equal(s.deployed, false);
  assert.equal(s.claimable, true);
  assert.deepEqual(s.calls.map((c) => c.step), ["deploy_split", "distribute"]);
  // The fields only the dead rollup could populate must stay gone.
  assert.ok(!("payments" in s));
  assert.ok(!("receivedUnits" in s));
});

// ── parity with the Python kit ────────────────────────────────────────────────
//
// These pin the three places the two ports had drifted apart. Each has a
// counterpart in python/tests/ (test_split.py, test_affiliation.py).

test("release() re-reads isDeployed instead of trusting the cache", async () => {
  // The regression: PayTo is memoized, and `splitDeployed` is the one field in
  // it that CHANGES - a split flips to deployed the first time anyone claims it.
  // Serving the cached value kept emitting a deploy leg for a split that already
  // exists, and createSplitDeterministic at an existing address reverts.
  const opts = { payout: BUILDER, deployed: false, balance: 1_000_000n };
  const a = aff(mockClient(opts));

  const first = await a.release("bc_alice");
  assert.deepEqual(first.calls.map((c) => c.step), ["deploy_split", "distribute"]);

  opts.deployed = true; // someone claimed it (or the builder self-served)

  const second = await a.release("bc_alice");
  assert.deepEqual(second.calls.map((c) => c.step), ["distribute"]);
});

test("a settled split is not claimable, though its balance is non-zero", () => {
  // The permanent floor of a two-way split: 1 unit retained, 1 unit that floors
  // to zero for BOTH recipients. `balance > 0` and `distributable > 0` are both
  // true here, which is exactly the trap.
  assert.equal(distributableUnits(2n), 1n);
  assert.deepEqual(payoutUnits(2n, [1000, 9000]), [0n, 0n]);
  assert.equal(isClaimable(2n, [1000, 9000]), false);

  // 3 units: the builder still floors to 0, but the seller nets 1.
  assert.deepEqual(payoutUnits(3n, [1000, 9000]), [0n, 1n]);
  assert.equal(isClaimable(3n, [1000, 9000]), true);

  // Splits does not require totalAllocation == 10000.
  assert.equal(isClaimable(101n, [1, 999], 1000n), true);
  assert.equal(isClaimable(101n, [1, 999], 0n), false);

  assert.equal(planIsClaimable(plan(), 2n), false);
  assert.equal(planIsClaimable(plan(), 1_000_000n), true);
});

test("a builder payout equal to the seller collapses to a direct payment", () => {
  // Two codes can share one payout wallet. Splitting a wallet against itself
  // costs gas + dust for nothing.
  const p = buildSplitPlan(SELLER, SELLER, "bc_self", 1000);
  assert.equal(p.hasBuilder, false);
  assert.deepEqual(p.recipients, [[SELLER, 10000]]);
  // Case-insensitively: registry and RPC casing differ.
  assert.equal(buildSplitPlan(SELLER.toUpperCase() as typeof SELLER, SELLER, "bc_self", 1000).hasBuilder, false);
});

test("amountsUnits sums duplicate recipients instead of overwriting", () => {
  // A hand-built multi-recipient plan may legally list one address twice; the
  // split pays it both legs. 99999 + 899999, each floored - not the 899999 that
  // overwriting produced (which also inflated dust from 2 to 100001).
  const p = { ...plan(), builderPayout: SELLER, recipients: [[SELLER, 1000], [SELLER, 9000]] as Array<[typeof SELLER, number]>, sellerPayout: SELLER };
  const got = amountsUnits(p, 1_000_000n);
  assert.equal(got.get(SELLER), 999_998n);
  assert.equal(1_000_000n - 999_998n, 2n);
});
