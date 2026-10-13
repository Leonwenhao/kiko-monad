"""Signer-internal shared types (integrator-owned). Used across signer-core, relay-exec, gas-tracker, web-accounts.

Nothing here does I/O. Amounts: `int` base units inside the signer, `Decimal` for USD, strings only at the DB/wire.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any


class ExecState(str, Enum):
    """Doc 10 §2.8 state machine. Terminal: LANDED, REFUNDED, FAILED, REJECTED*, EXPIRED, ABANDONED."""
    PROPOSED = "proposed"
    CARDED = "carded"
    CONFIRMED = "confirmed"
    QUOTED = "quoted"
    VERIFIED = "verified"
    FRONTED = "fronted"
    SIGNING = "signing"
    BROADCAST = "broadcast"
    LANDED = "landed"
    REFUNDED = "refunded"
    UNKNOWN = "unknown"            # tracker resolves to LANDED | FAILED; never auto-retried
    FAILED = "failed"
    EXPIRED = "expired"
    ABANDONED = "abandoned"
    REQUOTE = "requote"
    REJECTED = "rejected"                      # caps, paused, breaker, float low
    REJECTED_VERIFY = "rejected_verify"
    REJECTED_COSIGNER = "rejected_cosigner"


TERMINAL = frozenset({ExecState.LANDED, ExecState.REFUNDED, ExecState.FAILED, ExecState.EXPIRED, ExecState.ABANDONED,
                      ExecState.REQUOTE, ExecState.REJECTED, ExecState.REJECTED_VERIFY, ExecState.REJECTED_COSIGNER})

ALLOWED_TRANSITIONS: dict[ExecState, frozenset[ExecState]] = {
    ExecState.PROPOSED: frozenset({ExecState.CARDED, ExecState.ABANDONED}),
    ExecState.CARDED: frozenset({ExecState.CONFIRMED, ExecState.EXPIRED}),
    # CONFIRMED -> REQUOTE: Jupiter rate-limited / down before the first quote (nothing quoted, nothing signed): a
    # retryable requote, never a refusal (phase0 WP2, 2026-10-11)
    ExecState.CONFIRMED: frozenset({ExecState.QUOTED, ExecState.REJECTED, ExecState.REQUOTE}),
    # QUOTED -> REJECTED: a restart found it quoted and never signed: closed "restart:nothing_sent" (phase0 WP3)
    ExecState.QUOTED: frozenset({ExecState.VERIFIED, ExecState.REQUOTE, ExecState.REJECTED_VERIFY,
                                ExecState.REJECTED}),
    ExecState.VERIFIED: frozenset({ExecState.FRONTED, ExecState.SIGNING, ExecState.REJECTED}),
    ExecState.FRONTED: frozenset({ExecState.SIGNING, ExecState.FAILED}),
    ExecState.SIGNING: frozenset({ExecState.BROADCAST, ExecState.REJECTED_COSIGNER, ExecState.REJECTED_VERIFY,
                                  ExecState.FAILED}),
    ExecState.BROADCAST: frozenset({ExecState.SIGNING, ExecState.LANDED, ExecState.REFUNDED, ExecState.UNKNOWN,
                                    ExecState.FAILED}),
    ExecState.UNKNOWN: frozenset({ExecState.LANDED, ExecState.FAILED, ExecState.REFUNDED}),
}


def can_transition(a: ExecState, b: ExecState) -> bool:
    return b in ALLOWED_TRANSITIONS.get(a, frozenset())


class CardState(str, Enum):
    CARDED = "carded"
    CONFIRMED = "confirmed"        # single-use atomic transition
    EXPIRED = "expired"
    SUPERSEDED = "superseded"      # a newer card for the same user replaced it
    REJECTED = "rejected"


class ConfirmMode(str, Enum):
    Y = "y"
    CODE = "code"


class TokenClass(str, Enum):
    STABLE = "stable"
    MAJOR = "major"
    OTHER = "other"
    NEW = "new"            # < 24 h old
    STOCK = "stock"        # RH stock tokens: denied for US users


class PauseScope(str, Enum):
    GLOBAL = "global"
    USER = "user"
    CHAIN = "chain"
    TOKEN = "token"
    VENUE = "venue"
    WEB_LOGIN = "web_login"     # PHASE4 P1: new dashboard sign-ins (and, P2, the web safe writes); value is always *
    PUBLIC = "public"           # public beta kill switch (2026-10-13): no new non-owner wallets and no public-tier buys;
                                # sells and withdrawals keep working. value is always *


class StepShape(str, Enum):
    """The only allowed Relay step lists (doc 10 §2.2 table). Anything else is rejected (§3.4 A5)."""
    EVM_SAME_NATIVE_IN = "evm_same_native_in"          # [swap → erc20Router]
    EVM_SAME_ERC20_IN = "evm_same_erc20_in"            # [approve(approvalProxy), swap → approvalProxy]
    EVM_CROSS_ERC20_IN = "evm_cross_erc20_in"          # [approve(depository), depositErc20 (4-arg)]
    EVM_CROSS_NATIVE_IN = "evm_cross_native_in"        # [depositNative]
    SOL_SAME = "sol_same"                              # [swap: System/Token/JUP6/Memo, ALTs]
    SOL_CROSS = "sol_cross"                            # [deposit → depository program 99vQ…]


@dataclass(frozen=True)
class TokenRef:
    chain_id: int
    address: str          # EVM lowercase / Solana base58 / native sentinel

    def key(self) -> str:
        return f"{self.chain_id}:{self.address}"


@dataclass
class TokenRisk:
    """From the signer's OWN fetch (DexScreener + GoPlus / mint authority), never from the brain."""
    token: TokenRef
    symbol: str
    decimals: int
    liquidity_usd: Decimal
    age_s: int | None
    verified: bool
    token_class: TokenClass
    price_usd: Decimal | None = None
    flags: tuple[str, ...] = ()         # "honeypot", "freeze_authority", "transfer_hook", "tax" …
    tax: Decimal | None = None          # GoPlus max(buy, sell) tax (EVM) / the Token-2022 transfer fee, a fraction
    t22: Any = None                     # risk.Token2022Info for a Token-2022 mint (extension types, fee tier)
    # the token's TOTAL liquidity over its pools (DexScreener, summed like the brain's token preview): what a card SHOWS.
    # `liquidity_usd` stays the deepest pool's, the one the trade routes through, for every risk gate (v0.9.2)
    total_liquidity_usd: Decimal | None = None
    volume_h24_usd: Decimal | None = None   # summed over the pools (DexScreener); None = unknown


@dataclass
class UserAddresses:
    """Refreshed from Turnkey by the signer (never from the brain)."""
    user_id: str
    sub_org_id: str
    evm: str               # lowercase 0x
    sol: str               # base58
    grant_version: int
    grant_chains: tuple[int, ...]
    grant_checked_at: int
    da_user_id: str = ""
    cosigner_user_id: str = ""

    def for_chain(self, vm: str) -> str:
        return self.sol if vm == "svm" else self.evm


@dataclass
class DerivedIntent:
    """Output of the signer's own re-derivation from inbound_log text (doc 10 §2.5 6a)."""
    side: str                          # "buy" | "sell"
    asset_text: str
    amount_kind: str                   # "usd" | "units" | "half" | "all" | "fraction"
    amount_value: Decimal | None
    chain_hint: str = ""
    is_question: bool = False
    cash_chain: str = ""               # sells: "to <chain> [cash]" (the cash the user wants; "" = the token's chain)


@dataclass
class Card:
    card_id: str
    user_id: str
    side: str
    origin_chain: int
    dest_chain: int
    in_token: TokenRef
    out_token: TokenRef
    in_base: int
    min_out_base: int
    usd_notional: Decimal
    app_fee_bps: int
    network_usd: Decimal
    slippage_bps: int
    code: str
    confirm_mode: ConfirmMode
    intent_hash: str
    expires_at: int
    sent_at: int | None = None
    state: CardState = CardState.CARDED
    inbound_msg_id: str = ""
    card_text: str = ""
    fees_text: str = ""


@dataclass
class CostEstimate:
    """Doc 10 §2.6 pricing rule output (gas-tracker cost_model)."""
    network_usd: Decimal
    rent_usd: Decimal
    new_atas: int
    kiko_bps: int
    app_fee_bps: int
    warn_first_line: bool
    refuse: bool
    min_trade_usd: Decimal | None = None
    lines: dict[str, Decimal] = field(default_factory=dict)   # relay, network, kiko, rent (fees_text source)
    rent_subsidy_usd: Decimal = Decimal(0)   # new-account rent kiko pays itself (v0.9.2): in no line, no fee


@dataclass
class EvmTxRequest:
    """One EVM step item, normalized from a Relay quote, BEFORE the signer sets nonce/gas."""
    chain_id: int
    from_addr: str
    to: str
    data: str              # 0x hex
    value: int
    gas: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    step_id: str


@dataclass
class EvmUnsignedTx:
    chain_id: int
    nonce: int
    to: str
    data: str
    value: int
    gas: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int
    unsigned_hex: str      # 0x02 || rlp(...) — what Turnkey signs
    unsigned_hash: str     # sha256 of the unsigned bytes (sign_log)


@dataclass
class SolanaIx:
    program_id: str
    keys: list[tuple[str, bool, bool]]   # (pubkey, is_signer, is_writable)
    data_hex: str


@dataclass
class SolanaTxPlan:
    instructions: list[SolanaIx]
    lookup_tables: list[str]
    payer: str
    step_id: str


@dataclass
class SolanaUnsignedTx:
    message_b64: str
    tx_b64: str            # serialized VersionedTransaction with empty signatures (what Turnkey signs)
    blockhash: str
    last_valid_height: int
    unsigned_hash: str
    size_bytes: int


@dataclass
class SimResult:
    ok: bool
    provider: str
    balance_deltas: dict[str, int] = field(default_factory=dict)  # "owner|chain:token" -> signed base units
    transfers: list[dict[str, Any]] = field(default_factory=list)  # ERC-20 Transfer / SPL movements
    approvals: list[dict[str, Any]] = field(default_factory=list)
    fee_payer_outflow: int = 0
    gas_used: int = 0
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class VerifyResult:
    ok: bool
    shape: StepShape | None = None
    code: str = ""          # VerifyCode name on failure
    detail: str = ""


@dataclass
class SignResult:
    activity_id: str
    status: str             # "ACTIVITY_STATUS_COMPLETED" | "ACTIVITY_STATUS_CONSENSUS_NEEDED" | "…_REJECTED" | "…_FAILED"
    signed_tx: str = ""     # hex (EVM) or base64 (Solana) when completed


@dataclass
class FrontPlan:
    """What gas-tracker will front for one exec (doc 10 §2.6)."""
    chain_id: int
    drip_native: int = 0          # EVM gas drip to the user's EOA
    fee_payer: str = ""           # Solana depositFeePayer
    est_fronted_usd: Decimal = Decimal(0)
    reason: str = ""
