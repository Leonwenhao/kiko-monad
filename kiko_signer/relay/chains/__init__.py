"""Per-chain execution configs (relay-exec; WP-X / doc 10 WP-F; §4.3 checklist, §4.5 parity table).

One module per chain exports CONFIG: ChainExecConfig. The registry below is the only lookup. A chain is TRADABLE only
when `chains_enabled.enabled = 1` in signer.sqlite, which admin_cli sets after every CHECKLIST item is recorded in
`checklist_json` and signed off by Leon (doc 10 §4.3; see chains/checklist.py). These configs never enable anything by
themselves.

Gas ceilings were replaced on 2026-10-03 from >= 3 live quotes per shape per chain
(tests/fixtures/trading/relay_live, keyless, unsigned): ceiling ~= 1.5 x the largest observed Relay `gas` for that step
id, rounded up. A quote above the ceiling is rejected (GAS_LIMIT_CAP, doc 10 §3.4 C12) because it would blow the
fronted-gas budget, not because it is necessarily malicious. Drip floors / ceilings stay [verify] until 20 real txs
per chain are observed (checklist item 4).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

CHECKLIST_ITEMS: tuple[str, ...] = (
    "1_pinned_contracts",     # /chains snapshot + selectors observed in >= 3 live quotes per shape; copied to co-signer
    "2_turnkey_policy",       # chain in grant template; mainnet $1 deny tests DENIED
    "3_verifier_simulation",  # both providers simulate; malicious corpus rejected by signer and co-signer
    "4_gas_funding",          # gas wallet float; drip floor/ceiling from 20 txs; appFees recovery reconciled
    "5_cash_balance",         # cash token chosen; balance reader + explorer link; token-risk data source
    "6_smoke_trades",         # <= $5 each: same-chain buy/sell, cross-chain in/out, one forced refund
    "7_ops",                  # /chains diff watch on; pause scope chain:<id> tested; RPC fallback configured
)

MAX_SOLANA_TX_BYTES = 1232
INTRINSIC_GAS = 21_000                        # a plain EVM transfer; the floor under every signed gas limit


@dataclass(frozen=True)
class ChainExecConfig:
    chain_id: int
    key: str
    vm: str                                   # "evm" | "svm"
    milestone: str                            # "P0" | "P0+"
    tx_type: str = "TYPE_2"                   # BNB: confirm type-2 acceptance [verify] (doc 10 §4.5)
    gas_ceiling: dict[str, int] = field(default_factory=dict)   # per step id ("approve","swap","deposit")
    # per-chain gas facts (EVM): the gas wallet's plain drip (Arbitrum Orbit folds the L1 data cost into `gas`, so RH
    # needs room above the intrinsic 21,000), one cold ERC-20 transfer (the withdraw drip's estimate) and the revoke
    # approve(spender, 0) when no "approve" ceiling is recorded. Unused gas is not charged, so margin costs nothing.
    drip_gas: int = INTRINSIC_GAS
    erc20_transfer_gas: int = 65_000
    approve_fallback_gas: int = 100_000
    max_fee_multiple_of_estimate: int = 3     # §3.4 C12
    gas_from_simulation: bool = False         # Monad: gas = own simulation × 1.1, priced on the LIMIT
    gas_sim_multiplier: Decimal = Decimal("1.1")
    # Arbitrum Orbit (RH) folds the L1 data cost into `gas` at submission: Relay's quote-time limit can fall short when
    # L1 data price rises before broadcast ("intrinsic gas too low"). Signed gas = min(ceiling, ceil(relay x pad)).
    l1_gas_pad: Decimal = Decimal("1")
    drip_floor_usd: Decimal = Decimal("0")    # EVM gas drip target
    drip_refill_below_usd: Decimal = Decimal("0")
    drip_ceiling_usd: Decimal = Decimal("0")  # per trade
    float_p0_usd: Decimal = Decimal("0")
    float_stage1_usd: Decimal = Decimal("0")
    l1_gate: bool = False                     # Ethereum: 3% buy gate, $50 floor, asymmetric sell (doc 10 §2.6)
    protected_send: bool = False              # private/protected mempool for same-chain swaps
    has_multicall3: bool = True
    token_risk_sources: tuple[str, ...] = ("dexscreener",)
    simulation: str = "eth_simulateV1"        # or "debug_traceCall" / "solana_simulate" [R5 per chain]
    receipt_timeout_s: float = 90.0           # inclusion wait per step before the exec goes UNKNOWN
    receipt_poll_s: float = 1.0
    # Solana only
    priority_fee_cap_lamports: int = 0        # fee-payer priority cap (compute-unit price x limit)
    relay_fee_cap_bps: int = 0                # Solana same-chain: top-level fee transfer <= (app + this) bps
    rent_per_ata_lamports: int = 0
    rent_per_t22_ata_lamports: int = 0        # a Token-2022 ATA (170 bytes, ImmutableOwner): more rent
    sig_fee_lamports: int = 5000
    resend_interval_s: float = 2.0
    extra_items: tuple[str, ...] = ()         # the chain's doc 10 §4.5 row, recorded next to CHECKLIST_ITEMS
    observed: dict[str, tuple[int, int]] = field(default_factory=dict)   # step id -> (min, max) gas seen live
    notes: tuple[str, ...] = ()

    def ceiling(self, step_id: str) -> int:
        """Gas ceiling for a step id; unknown step ids have no ceiling (=> 0, always rejected)."""
        return int(self.gas_ceiling.get(step_id, 0))


def registry() -> dict[int, ChainExecConfig]:
    from . import base, bnb, ethereum, monad, robinhood, solana
    return {m.CONFIG.chain_id: m.CONFIG for m in (robinhood, solana, base, bnb, ethereum, monad)}


def get(chain_id: int) -> ChainExecConfig:
    """Fail closed: KeyError for a chain with no exec config."""
    return registry()[int(chain_id)]
