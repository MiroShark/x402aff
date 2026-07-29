/**
 * One object that is the whole integration - declare, payTo, distribute.
 *
 * A TypeScript port of the x402aff kit's Python `Affiliation` facade (affiliation.py),
 * for x402 sellers running the Node / TS reference stack. Same three on-chain
 * reads, same safe-fallback contract, same surface:
 *
 * ```ts
 * import { Affiliation } from "x402aff";
 *
 * const aff = new Affiliation({ appCode: "bc_yourcode", sellerPayout: "0x…" });
 *
 * // ── on your x402 route ──
 * const payTo = await aff.payToFor(req.headers);   // the split, or your wallet
 * const extensions = aff.extensions;               // declares your app code `a`
 *
 * // ── the payout side ──
 * const { calls, balanceUnits } = await aff.release("bc_alice");
 * ```
 *
 * Everything here is enforced by the same two Base-mainnet contracts the Python
 * kit uses - the Builder Codes registry and the 0xSplits PushSplitFactory - so a
 * TS seller and a Python seller resolve the *same* split address for a given
 * (seller, builder) pair.
 *
 * The only dependency is `viem`. Resolution NEVER throws: a missing, unknown, or
 * unresolvable code falls back to the seller wallet (unsplit, never a failed
 * payment).
 */
import {
  createPublicClient,
  http,
  encodeFunctionData,
  BaseError,
  ContractFunctionRevertedError,
} from "viem";
import { base } from "viem/chains";
import type { Address, Hex, PublicClient } from "viem";

// ── Base mainnet addresses (identical to the Python kit) ─────────────────────
export const USDC_BASE: Address = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
export const BUILDER_CODES_REGISTRY: Address =
  "0x000000BC7E6457e610fe52Dcc0ca5b3ce59C8E80";
/** 0xSplits PushSplitFactory V2.2 on Base - confirmed by the Splits team. */
export const SPLITS_PUSH_FACTORY: Address =
  "0x8E8eB0cC6AE34A38B67D5Cf91ACa38f60bc3Ecf4";

export const DEFAULT_BUILDER_SHARE_BPS = 1000; // 10%
export const BPS_DENOM = 10000;
/** A PushSplit keeps 1 base unit warm; only balance-1 is ever distributable. */
export const SPLITS_RETAINED_UNITS = 1n;

const ZERO_ADDRESS: Address = "0x0000000000000000000000000000000000000000";
const ZERO_SALT: Hex = ("0x" + "00".repeat(32)) as Hex;
const PUBLIC_BASE_RPC = "https://mainnet.base.org";

/** Every builder code (a / w / s) is 1-32 lowercase letters, digits, `_`. */
export const BUILDER_CODE_PATTERN = "^[a-z0-9_]{1,32}$";
const BUILDER_CODE_RE = /^[a-z0-9_]{1,32}$/;

/**
 * The SHARED marker code the kit appends as a SECOND `s` on every payment, so
 * kit-routed payments self-identify on-chain. Because it is the same across all
 * kit installs, ONE query discovers every kit payment ecosystem-wide -
 * `WHERE builder_code = <marker>` in CDP's index - with no split-address
 * reconstruction, and it catches even undeployed splits (written at settle time).
 * It rides alongside the real builder code; the split always pays the PRIMARY
 * (first) code, so the marker never changes a payout, and needs no registration.
 * It is HARDCODED and identical across every kit install - that shared constant is
 * what makes one query discover them all, so it is deliberately not configurable.
 */
export const AFFILIATION_MARKER = "x402aff";

/**
 * The buyer-side `s` codes to attach: your real builder `code`, plus the kit
 * marker as a second entry (the marker is dropped if invalid or equal to `code`).
 * Feed the result to whatever sets the payment's `s`, e.g.
 * `{ "builder-code": { info: { s: markedServiceCodes("bc_you") } } }`. The split
 * still pays the primary code (`code`), so the marker never changes a payout.
 */
export function markedServiceCodes(code: string): string[] {
  return AFFILIATION_MARKER !== code ? [code, AFFILIATION_MARKER] : [code];
}

/** JSON Schema for the ERC-8021 Schema 2 fields (mirrors builder_code.py). */
const BUILDER_CODE_SCHEMA = {
  type: "object",
  properties: {
    a: { type: "string", pattern: BUILDER_CODE_PATTERN },
    w: { type: "string", pattern: BUILDER_CODE_PATTERN },
    s: { type: "array", items: { type: "string", pattern: BUILDER_CODE_PATTERN } },
  },
  additionalProperties: false,
} as const;

// ── ABIs (only the functions the x402aff kit calls) ──────────────────────────
const SPLIT_STRUCT = {
  name: "split",
  type: "tuple",
  components: [
    { name: "recipients", type: "address[]" },
    { name: "allocations", type: "uint256[]" },
    { name: "totalAllocation", type: "uint256" },
    { name: "distributionIncentive", type: "uint16" },
  ],
} as const;

const FACTORY_ABI = [
  {
    name: "isDeployed",
    type: "function",
    stateMutability: "view",
    inputs: [SPLIT_STRUCT, { name: "owner", type: "address" }, { name: "salt", type: "bytes32" }],
    outputs: [{ type: "address" }, { type: "bool" }],
  },
  {
    name: "createSplitDeterministic",
    type: "function",
    stateMutability: "nonpayable",
    inputs: [
      SPLIT_STRUCT,
      { name: "owner", type: "address" },
      { name: "creator", type: "address" },
      { name: "salt", type: "bytes32" },
    ],
    outputs: [{ type: "address" }],
  },
] as const;

const SPLIT_ABI = [
  {
    name: "distribute",
    type: "function",
    stateMutability: "nonpayable",
    inputs: [SPLIT_STRUCT, { name: "token", type: "address" }, { name: "distributor", type: "address" }],
    outputs: [],
  },
] as const;

const REGISTRY_ABI = [
  {
    name: "payoutAddress",
    type: "function",
    stateMutability: "view",
    inputs: [{ type: "uint256" }],
    outputs: [{ type: "address" }],
  },
] as const;

const ERC20_ABI = [
  {
    name: "balanceOf",
    type: "function",
    stateMutability: "view",
    inputs: [{ type: "address" }],
    outputs: [{ type: "uint256" }],
  },
] as const;

// ── plain-data types ─────────────────────────────────────────────────────────

/** One `[address, allocationBps]` leg of a split. Hand-build these for a
 *  multi-recipient plan (see the README). */
export type Recipient = [Address, number];

/** What `resolve`/`payToFor` accept: a raw code, or anything header-shaped. */
export type PayToSource = string | null | undefined | Headers | Record<string, unknown> | Map<string, unknown>;

export interface SplitPlan {
  sellerPayout: Address;
  builderCode: string | null;
  builderPayout: Address | null;
  builderShareBps: number;
  /** `[address, allocationBps][]`, summing to BPS_DENOM - the PushSplit's shape. */
  recipients: Recipient[];
  hasBuilder: boolean;
}

export interface PayTo {
  /** The address to advertise as the route's `payTo`. */
  address: Address;
  /** False when there was no/unknown/unresolvable code (i.e. `address` is the seller). */
  attributed: boolean;
  splitDeployed: boolean;
  plan: SplitPlan;
  /** Set only when a lookup *failed* (vs. finding no builder) - watch for RPC 429s. */
  error?: string;
}

export interface DistributeCall {
  step: "deploy_split" | "distribute";
  target: Address;
  summary: string;
  data: Hex;
}

export interface AffiliationOptions {
  /** Your app / resource-server code - the `a` you declare on the route. */
  appCode: string;
  /** Where your remainder (and every unattributed payment) is paid. */
  sellerPayout: Address;
  /** Builder cut in basis points (1000 = 10%). Default DEFAULT_BUILDER_SHARE_BPS. */
  builderShareBps?: number;
  /** Base RPC. Ignored if `client` is supplied. Defaults to the (rate-limited) public RPC. */
  rpcUrl?: string;
  /** Inject a viem PublicClient (custom transport, tests, a shared client). */
  client?: PublicClient;
}

// ── pure helpers (no network) ─────────────────────────────────────────────────

/** Builder code → ERC-721 token id: the code's ASCII bytes as a big-endian int. */
export function toTokenId(code: string): bigint {
  if (!BUILDER_CODE_RE.test(code)) {
    throw new Error(`invalid builder code ${JSON.stringify(code)} (must match ${BUILDER_CODE_PATTERN})`);
  }
  let hex = "";
  for (let i = 0; i < code.length; i++) hex += code.charCodeAt(i).toString(16).padStart(2, "0");
  return BigInt("0x" + hex);
}

/** First valid code from a possibly comma-joined `s` (v1 pays one builder). */
export function primaryCode(raw: string | null | undefined): string | null {
  if (!raw) return null;
  for (const part of String(raw).split(",")) {
    const c = part.trim();
    if (BUILDER_CODE_RE.test(c)) return c;
  }
  return null;
}

/** Build a split plan from already-resolved addresses.
 *
 *  A builder whose payout IS the seller's own wallet yields a seller-only plan.
 *  Two codes can share a payout (both registered to one wallet), and a seller's
 *  own code arriving as `s` does it trivially. Routing that through a split
 *  would send the seller's money to a contract and charge them gas plus dust to
 *  get it back, splitting a wallet against itself for no benefit. */
export function buildSplitPlan(
  sellerPayout: Address,
  builderPayout: Address | null,
  builderCode: string | null,
  builderShareBps: number,
): SplitPlan {
  if (!sellerPayout) throw new Error("sellerPayout is required");
  if (!(builderShareBps >= 0 && builderShareBps <= BPS_DENOM)) {
    throw new RangeError(`builderShareBps must be 0..${BPS_DENOM}`);
  }
  if (builderPayout && builderPayout.toLowerCase() === sellerPayout.toLowerCase()) {
    builderPayout = null;
  }
  if (builderPayout && builderShareBps > 0) {
    return {
      sellerPayout,
      builderCode,
      builderPayout,
      builderShareBps,
      recipients: [
        [builderPayout, builderShareBps],
        [sellerPayout, BPS_DENOM - builderShareBps],
      ],
      hasBuilder: true,
    };
  }
  return {
    sellerPayout,
    builderCode,
    builderPayout: null,
    builderShareBps,
    recipients: [[sellerPayout, BPS_DENOM]],
    hasBuilder: false,
  };
}

/** What each recipient actually receives on-chain (mirrors 0xSplits exactly).
 *
 *  Shares are SUMMED per address, not overwritten: a plan may legally list one
 *  address twice (a hand-built multi-recipient plan where a partner is also the
 *  seller), and the split pays it both legs. Keying by address without summing
 *  silently dropped a leg and inflated the apparent dust. */
export function amountsUnits(plan: SplitPlan, priceUnits: bigint): Map<Address, bigint> {
  if (!plan.hasBuilder) return new Map([[plan.sellerPayout, priceUnits]]);
  const distributable = priceUnits > SPLITS_RETAINED_UNITS ? priceUnits - SPLITS_RETAINED_UNITS : 0n;
  const out = new Map<Address, bigint>();
  for (const [addr, bps] of plan.recipients) {
    const share = (distributable * BigInt(bps)) / BigInt(BPS_DENOM);
    out.set(addr, (out.get(addr) ?? 0n) + share);
  }
  return out;
}

/** The part of a split's balance a `distribute` can move at all (balance minus
 *  the warm unit). Still not the same as "worth claiming" - see isClaimable. */
export function distributableUnits(balanceUnits: bigint): bigint {
  const d = balanceUnits - SPLITS_RETAINED_UNITS;
  return d > 0n ? d : 0n;
}

/** What each recipient nets from a `distribute` at this balance, floored.
 *  `totalAllocation` defaults to BPS_DENOM because that is what this kit's plans
 *  use, but Splits does not require it - pass the split's own value when reading
 *  someone else's split off-chain. */
export function payoutUnits(
  balanceUnits: bigint,
  allocations: readonly (bigint | number)[],
  totalAllocation: bigint = BigInt(BPS_DENOM),
): bigint[] {
  if (totalAllocation <= 0n) return allocations.map(() => 0n);
  const dist = distributableUnits(balanceUnits);
  return allocations.map((a) => (dist * BigInt(a)) / totalAllocation);
}

/**
 * Is sending a `distribute` actually worth the gas?
 *
 * NOT `balance > 0`, and not `distributable > 0` either. A fully distributed
 * split settles at a permanent floor of `SPLITS_RETAINED_UNITS + recipients - 1`
 * (2 units for the two-way case): one unit is retained, and the leftover unit
 * floors to zero for EVERY recipient. Gating a claim button on a non-zero
 * balance therefore leaves an eternal "claim me" on a split that is already
 * settled, and each click burns gas to move nothing.
 *
 * Note a claimable split can still pay a PARTICULAR recipient zero: at 3 units
 * of a 10/90 split the builder floors to 0 and only the seller nets anything.
 */
export function isClaimable(
  balanceUnits: bigint,
  allocations: readonly (bigint | number)[],
  totalAllocation: bigint = BigInt(BPS_DENOM),
): boolean {
  return payoutUnits(balanceUnits, allocations, totalAllocation).some((u) => u > 0n);
}

/** isClaimable for a plan, using its own allocation vector. */
export function planIsClaimable(plan: SplitPlan, balanceUnits: bigint): boolean {
  return isClaimable(balanceUnits, plan.recipients.map(([, bps]) => bps));
}

function splitStruct(plan: SplitPlan) {
  return {
    recipients: plan.recipients.map((r) => r[0]),
    allocations: plan.recipients.map((r) => BigInt(r[1])),
    totalAllocation: BigInt(BPS_DENOM),
    distributionIncentive: 0,
  } as const;
}

/** Calldata for factory.createSplitDeterministic (the deploy leg). */
export function createSplitCalldata(plan: SplitPlan): Hex {
  return encodeFunctionData({
    abi: FACTORY_ABI,
    functionName: "createSplitDeterministic",
    args: [splitStruct(plan), ZERO_ADDRESS, ZERO_ADDRESS, ZERO_SALT],
  });
}

/** Calldata for split.distribute(split, USDC, distributor). */
export function distributeCalldata(plan: SplitPlan, distributor: Address = ZERO_ADDRESS): Hex {
  return encodeFunctionData({
    abi: SPLIT_ABI,
    functionName: "distribute",
    args: [splitStruct(plan), USDC_BASE, distributor],
  });
}

/** Calldata for factory.isDeployed(split, owner=0, salt=0) - the address read. */
export function isDeployedCalldata(plan: SplitPlan): Hex {
  return encodeFunctionData({
    abi: FACTORY_ABI,
    functionName: "isDeployed",
    args: [splitStruct(plan), ZERO_ADDRESS, ZERO_SALT],
  });
}

/** True for a contract revert / empty-return (unregistered), false for a
 *  transport/network failure (which must surface as an error, not "no builder"). */
function isUnregistered(err: unknown): boolean {
  if (err instanceof BaseError) {
    if (err.walk((e) => e instanceof ContractFunctionRevertedError)) return true;
    const inner = err.walk();
    const msg = `${err.shortMessage ?? ""} ${inner instanceof Error ? inner.message : ""}`;
    if (/reverted|zero data|returned no data/i.test(msg)) return true;
  }
  return false;
}

// ── discovery + claims-dashboard payload ─────────────────────────────────────

/** Runs a read-only CDP SQL query and returns the rows. Injected rather than
 *  built in, so the kit stays viem-only: a CDP-facilitator seller already has
 *  auth (e.g. `createAuthHeader` from @coinbase/x402), so it passes its own
 *  runner. Python's kit bundles this (cdp_sql.py); TS keeps it dependency-free. */
export type CdpQuery = (sql: string) => Promise<Record<string, unknown>[]>;

/** One row of the claims dashboard: a per-builder split + a ready-to-send claim. */
export interface SplitRow {
  payTo: Address;
  sellerCode: string;
  builderCode: string;
  builderShareBps: number;
  balanceUnits: string;
  /** What a `distribute` can move (balance minus the split's warm unit). */
  distributableUnits: string;
  deployed: boolean;
  claimable: boolean;
  calls: Omit<DistributeCall, "summary">[];
}

export interface SplitsPayload {
  configured: boolean;
  marker: string;
  count: number;
  splits: SplitRow[];
}

/** Distinct `s` codes that settled alongside our `a` (role-flat table): every
 *  code on any tx that also carried our app code, minus our own and the
 *  facilitator's. Reorg-safe via `HAVING sum(action) > 0`. Mirror of the Python
 *  kit's monitor.discover_builder_codes. */
export async function discoverBuilderCodes(query: CdpQuery, appCode: string, days = 90): Promise<string[]> {
  const a = appCode.replace(/'/g, "");
  const d = Math.trunc(days);
  const rows = await query(
    `SELECT DISTINCT builder_code FROM base.transaction_attributions ` +
      `WHERE transaction_hash IN (` +
      `SELECT transaction_hash FROM base.transaction_attributions ` +
      `WHERE builder_code = '${a}' AND block_timestamp >= now() - INTERVAL ${d} DAY ` +
      `GROUP BY transaction_hash HAVING sum(action) > 0) ` +
      `GROUP BY builder_code HAVING sum(action) > 0`,
  );
  return rows
    .map((r) => String(r.builder_code ?? ""))
    .filter((c) => c && c !== appCode && !c.startsWith("cdp_facil"));
}


// ── the facade ─────────────────────────────────────────────────────────────

export class Affiliation {
  /** The request header a buyer's client sets to name its builder. */
  static readonly HEADER = "X-Builder-Code";

  readonly appCode: string;
  readonly sellerPayout: Address;
  readonly builderShareBps: number;
  private readonly client: PublicClient;
  /** code|bps → PayTo. Only *positive* (attributed) resolutions are cached; an
   *  unregistered code and a lookup error are never cached, so a builder who
   *  registers after their first request isn't stranded on a stale miss. */
  private readonly cache = new Map<string, PayTo>();
  private _extensions?: Record<string, unknown>;

  constructor(opts: AffiliationOptions) {
    if (!opts.appCode) throw new Error("appCode is required (your `a` code)");
    if (!opts.sellerPayout) throw new Error("sellerPayout is required");
    this.appCode = opts.appCode;
    this.sellerPayout = opts.sellerPayout;
    this.builderShareBps = opts.builderShareBps ?? DEFAULT_BUILDER_SHARE_BPS;
    this.client =
      opts.client ??
      (createPublicClient({ chain: base, transport: http(opts.rpcUrl ?? PUBLIC_BASE_RPC) }) as PublicClient);
  }

  // ── request path ───────────────────────────────────────────────────────────

  /** The route `extensions` that declare your app code `a`. */
  get extensions(): Record<string, unknown> {
    if (!this._extensions) {
      if (!BUILDER_CODE_RE.test(this.appCode)) {
        throw new Error(`app code ${JSON.stringify(this.appCode)} must match ${BUILDER_CODE_PATTERN}`);
      }
      this._extensions = {
        "builder-code": { info: { a: this.appCode }, schema: BUILDER_CODE_SCHEMA },
      };
    }
    return this._extensions;
  }

  /** Pull a normalized builder code out of a headers object (Headers/Map/Record). */
  codeFromHeaders(headers: unknown): string | null {
    return primaryCode(getHeader(headers, Affiliation.HEADER));
  }

  /** The `payTo` address for a request - the split, or the seller wallet. Never throws. */
  async payToFor(source: PayToSource): Promise<Address> {
    return (await this.resolve(source)).address;
  }

  /** Full PayTo (address + why) for a request. Never throws. */
  async resolve(source: PayToSource): Promise<PayTo> {
    const code =
      source == null || typeof source === "string" ? primaryCode(source ?? null) : this.codeFromHeaders(source);

    if (!code) {
      return {
        address: this.sellerPayout,
        attributed: false,
        splitDeployed: false,
        plan: buildSplitPlan(this.sellerPayout, null, null, this.builderShareBps),
      };
    }

    const key = `${code}|${this.builderShareBps}`;
    const cached = this.cache.get(key);
    if (cached) return cached;

    try {
      const payout = await this.payoutOf(code);
      const plan = buildSplitPlan(this.sellerPayout, payout, code, this.builderShareBps);
      if (!plan.hasBuilder) {
        // Resolved fine, but this code isn't registered *yet*. Deliberately NOT
        // cached: the builder may register later, and a cached miss would strand
        // their cut (route to the seller, unsplit) for the whole process life -
        // and a valid-format unknown code could even be used to prime it. Only
        // positive resolutions (immutable: registered payout + CREATE2 address)
        // are memoized.
        return { address: this.sellerPayout, attributed: false, splitDeployed: false, plan };
      }
      const [address, deployed] = await this.predictSplitAddress(plan);
      const pt: PayTo = { address, attributed: true, splitDeployed: deployed, plan };
      this.cache.set(key, pt);
      return pt;
    } catch (err) {
      // Deliberately NOT cached - transient (RPC 429s); a cached failure would
      // strand that builder for the process lifetime.
      return {
        address: this.sellerPayout,
        attributed: false,
        splitDeployed: false,
        plan: buildSplitPlan(this.sellerPayout, null, code, this.builderShareBps),
        error: err instanceof Error ? `${err.name}: ${err.message}` : String(err),
      };
    }
  }

  /** Drop the memoized code→split-address cache (after a share change, or a retry). */
  clearCache(): void {
    this.cache.clear();
  }

  // ── payout path ────────────────────────────────────────────────────────────

  /** USDC base units currently sitting in this builder's split (0n if none). */
  async balance(code: string): Promise<bigint> {
    const pt = await this.resolve(code);
    if (!pt.attributed) return 0n;
    return this.splitBalance(pt.address);
  }

  /** Build the release calls + live balance for one builder's split.
   *
   *  Re-reads `isDeployed` rather than trusting `pt.splitDeployed`. That field is
   *  memoized with the rest of the PayTo, and deployment is the one part of it
   *  that CHANGES: the address and the payout are immutable, but a split flips
   *  to deployed the first time anyone claims it. Reading the cached value meant
   *  a long-lived process kept emitting a `deploy_split` leg for a split that
   *  already exists, and `createSplitDeterministic` at an existing address
   *  reverts - taking an atomic deploy+distribute batch down with it. Python's
   *  `distribute.distribute_plan` has always re-read this; now both ports agree. */
  async release(code: string, opts?: { distributor?: Address }): Promise<{ calls: DistributeCall[]; balanceUnits: bigint }> {
    const pt = await this.resolve(code);
    if (!pt.plan.hasBuilder) return { calls: [], balanceUnits: 0n };
    const [address, deployed] = await this.predictSplitAddress(pt.plan);
    const balanceUnits = await this.splitBalance(address);
    const calls = this.distributeCalls(pt.plan, address, deployed, opts?.distributor);
    return { calls, balanceUnits };
  }

  /**
   * The claims-dashboard payload — every per-builder split for this seller, ready
   * to serialize to JSON. One reusable call behind a `/splits` route. Each row
   * carries the split address, codes + share, live balance, deployed state, and a
   * permissionless [deploy?, distribute] claim. Discovery is by our app code `a`
   * (via the injected `query`); the claim is reconstructed from the seller wallet
   * this facade holds, so even undeployed splits build with no guessing. The
   * marker + unregistered codes are skipped.
   *
   * No per-split payment count: the query that would produce one 400s on the
   * CDP SQL API (queries.sql #5b). #5c has a cheap count-only alternative.
   */
  async splitsPayload(query: CdpQuery, opts?: { days?: number }): Promise<SplitsPayload> {
    const days = opts?.days ?? 90;
    const codes = (await discoverBuilderCodes(query, this.appCode, days)).filter(
      (c) => c !== AFFILIATION_MARKER,
    );

    const splits: SplitRow[] = [];
    for (const code of codes) {
      const pt = await this.resolve(code);
      if (!pt.attributed) continue; // unregistered builder / no split for this pair
      const { calls, balanceUnits } = await this.release(code);
      splits.push({
        payTo: pt.address,
        sellerCode: this.appCode,
        builderCode: code,
        builderShareBps: this.builderShareBps,
        balanceUnits: balanceUnits.toString(),
        distributableUnits: distributableUnits(balanceUnits).toString(),
        deployed: !calls.some((c) => c.step === "deploy_split"),
        // Not `calls.length > 0` - that is true for every attributed split, so a
        // settled one (parked at its permanent 2-unit floor forever) rendered an
        // eternal claim button that burned gas moving nothing.
        claimable: calls.length > 0 && planIsClaimable(pt.plan, balanceUnits),
        calls: calls.map((c) => ({ step: c.step, target: c.target, data: c.data })),
      });
    }
    // Fullest first. A comparator that never returns 0 orders ties arbitrarily
    // and is not a valid ordering, so equal balances used to shuffle between
    // reads; this matches Python's `sort(key=..., reverse=True)`.
    splits.sort((a, b) => {
      const x = BigInt(a.balanceUnits);
      const y = BigInt(b.balanceUnits);
      return y > x ? 1 : y < x ? -1 : 0;
    });
    return { configured: true, marker: AFFILIATION_MARKER, count: splits.length, splits };
  }

  /** The calls to release a funded pair (deploy skipped once deployed). */
  distributeCalls(plan: SplitPlan, splitAddress: Address, deployed: boolean, distributor?: Address): DistributeCall[] {
    if (!plan.hasBuilder) return [];
    const calls: DistributeCall[] = [];
    if (!deployed) {
      calls.push({
        step: "deploy_split",
        target: SPLITS_PUSH_FACTORY,
        summary: `deploy the per-pair PushSplit at ${splitAddress} (first use of this pair; permissionless)`,
        data: createSplitCalldata(plan),
      });
    }
    const recips = plan.recipients.map(([a, bps]) => `${a}:${bps}bps`).join(", ");
    calls.push({
      step: "distribute",
      target: splitAddress,
      summary: `distribute the split's USDC balance → [${recips}] (${plan.builderShareBps}bps to ${plan.builderCode})`,
      data: distributeCalldata(plan, distributor ?? ZERO_ADDRESS),
    });
    return calls;
  }

  // ── on-chain reads ─────────────────────────────────────────────────────────

  /** code → registered payout address (null when unregistered). Rethrows on network error. */
  async payoutOf(code: string): Promise<Address | null> {
    try {
      const addr = await this.client.readContract({
        address: BUILDER_CODES_REGISTRY,
        abi: REGISTRY_ABI,
        functionName: "payoutAddress",
        args: [toTokenId(code)],
      });
      return BigInt(addr) === 0n ? null : addr;
    } catch (err) {
      if (isUnregistered(err)) return null;
      throw err;
    }
  }

  /** The pair's counterfactual PushSplit address + whether it's deployed yet. */
  async predictSplitAddress(plan: SplitPlan): Promise<[Address, boolean]> {
    const res = await this.client.readContract({
      address: SPLITS_PUSH_FACTORY,
      abi: FACTORY_ABI,
      functionName: "isDeployed",
      args: [splitStruct(plan), ZERO_ADDRESS, ZERO_SALT],
    });
    return [res[0], res[1]];
  }

  /** USDC balance sitting in the split right now (base units). */
  async splitBalance(splitAddress: Address): Promise<bigint> {
    return await this.client.readContract({
      address: USDC_BASE,
      abi: ERC20_ABI,
      functionName: "balanceOf",
      args: [splitAddress],
    });
  }
}

/** Read a header case-insensitively from Headers / Map / plain object. */
function getHeader(headers: unknown, name: string): string | null {
  if (!headers) return null;
  const h = headers as { get?: (k: string) => unknown };
  if (typeof h.get === "function") {
    const v = h.get(name) ?? h.get(name.toLowerCase());
    return v == null ? null : String(v);
  }
  const lower = name.toLowerCase();
  for (const [k, v] of Object.entries(headers as Record<string, unknown>)) {
    if (k.toLowerCase() === lower) return v == null ? null : String(v);
  }
  return null;
}
