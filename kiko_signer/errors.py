"""Named rejection reasons (integrator-owned). Tests assert on these names (WP-R acceptance (a): "rejects with a
named reason"). The co-signer keeps its own list; names are mirrored in BUILD_PLAN.md so logs line up.

Plain refusals (Leon 2026-10-08): every user-visible refusal says in lowercase kiko voice what happened and what to
do, with "nothing was sent" where money is involved, at most 280 characters. PLAIN_TEXT is the line a RejectCode
carries when the raising site has nothing more specific to say (SignerReject fills `user_text` from it); sites that
know more (the chain, the amount, the code) render their own line. The words are kiko_wire/reject_text.REJECT_TEXT:
the brain's fallback for a reject with no words (sato/trade/cards.REASON_TEXT) is the same table, never a second copy.
"""
from __future__ import annotations

from enum import Enum

from kiko_wire.chains import CHAINS
from kiko_wire.reject_text import (BUSY_TEXT, CARD_GONE_TEXT, FROZEN_TEXT, PAUSED_TEXT, PRICE_FAIL_TEXT,  # noqa: F401
                                  QUOTE_EXPIRED_TEXT, REJECT_TEXT, REPLY_LOST_TEXT, SOLANA_ONLY_TEXT,
                                  chains_off_text)


class RejectCode(str, Enum):
    """Card / confirm / policy-of-the-signer reasons (signer-core, gas-tracker)."""
    ASSET_NOT_IN_TEXT = "asset_not_in_text"
    SIDE_MISMATCH = "side_mismatch"
    AMOUNT_MISMATCH = "amount_mismatch"
    QUESTION = "question"
    INBOUND_UNKNOWN = "inbound_unknown"
    NOT_IMESSAGE = "not_imessage"
    NOT_DM = "not_dm"
    WRONG_HANDLE = "wrong_handle"
    MULTIPLE_PENDING = "multiple_pending"
    CARD_EXPIRED = "card_expired"
    REPLY_BEFORE_CARD = "reply_before_card"
    REPLY_TOO_OLD = "reply_too_old"
    CODE_MISMATCH = "code_mismatch"
    Y_NOT_ALLOWED = "y_not_allowed"
    ALREADY_CONFIRMED = "already_confirmed"
    PAUSED = "paused"
    USER_FROZEN = "user_frozen"
    BREAKER = "breaker"
    FLOAT_LOW = "float_low"
    OVER_TRADE_CAP = "over_trade_cap"
    OVER_DAY_CAP = "over_day_cap"
    NEW_TOKEN_CAP = "new_token_cap"
    TOKEN_RISK = "token_risk"
    TOKEN_DENIED = "token_denied"            # stock tokens, honeypots
    AMBIGUOUS = "ambiguous"
    IMPACT_TOO_HIGH = "impact_too_high"
    NETWORK_COST_TOO_HIGH = "network_cost_too_high"
    L1_BELOW_MIN = "l1_below_min"
    INSUFFICIENT_CASH = "insufficient_cash"
    CHAIN_NOT_ENABLED = "chain_not_enabled"
    CHAIN_NOT_GRANTED = "chain_not_granted"
    GRANT_STALE = "grant_stale"
    NOT_ACTIVE = "not_active"
    REQUOTE = "requote"
    FRONTING_CAP = "fronting_cap"
    GAS_POLICY = "gas_policy"                 # the treasury gas policy (P6) does not cover this chain / drip size
    COSIGNER_REJECTED = "cosigner_rejected"   # stage 1: the independent co-signer voted no
    POLICY_DENIED = "policy_denied"           # Turnkey policy engine refused the DA's activity
    CONSENSUS_TIMEOUT = "consensus_timeout"   # stage 1: no co-signer vote in time (activity kept, resumed later)
    RATE_LIMITED = "rate_limited"             # too many /v1/card requests (per user or global), FP-05
    PASSKEY_RECHECK = "passkey_recheck"       # no signer-verified passkey use in 30 days (doc 10 T4/M2), FP-11
    INSTANT_OFF = "instant_off"               # instant mode is not available (KIKO_SIGNER_INSTANT / brain flag off)
    INSTANT_UNPARSED = "instant_unparsed"     # the user's instant command didn't parse ("instant $25" / "off" / "on")
    INSTANT_OVER_MAX = "instant_over_max"     # an instant limit above INSTANT_MAX_USD ($250) is refused
    OFFER_EXPIRED = "offer_expired"           # a completion offer the signer never logged, or one expired / spent / voided
    OFFER_MISMATCH = "offer_mismatch"         # the "yes" is not an accept, or the card asked for is not the offer logged
    SWEEP_RULE = "sweep_rule"                 # home balance L3: a sweep card that is not Solana USDC to the user's own
                                              # consented address, fee-free, from a sweep chain (kiko_signer/home.py)
    WITHDRAW_IN_FLIGHT = "withdraw_in_flight"  # phase0 WP5: a Solana withdraw step is still landing (withdraw_plan)


class VerifyCode(str, Enum):
    """Doc 10 §3.4 checks (relay-exec verifier + simulation). One code per malicious-quote corpus mutation."""
    SENDER_MISMATCH = "sender_mismatch"
    RECIPIENT_MISMATCH = "recipient_mismatch"                  # details.recipient != user
    CURRENCY_IN_MISMATCH = "currency_in_mismatch"
    CURRENCY_OUT_MISMATCH = "currency_out_mismatch"            # different currency
    AMOUNT_IN_MISMATCH = "amount_in_mismatch"
    MIN_OUT_TOO_LOW = "min_out_too_low"                        # lower minimumAmount
    IMPACT_TOO_HIGH = "impact_too_high"
    SLIPPAGE_TOO_HIGH = "slippage_too_high"
    APP_FEE_PARTY = "app_fee_party"                            # app-fee party != kiko
    APP_FEE_TOO_HIGH = "app_fee_too_high"                      # > 500 bps
    APP_FEE_MISMATCH = "app_fee_mismatch"                      # != card bps ± 1
    CHAIN_NOT_ENABLED = "chain_not_enabled"
    CHAIN_NOT_GRANTED = "chain_not_granted"
    SHAPE_UNKNOWN = "shape_unknown"
    EXTRA_STEP = "extra_step"
    SIGNATURE_STEP = "signature_step"
    ORDER_INPUT_MISMATCH = "order_input_mismatch"
    REFUND_NOT_USER = "refund_not_user"                        # refunds to a non-user
    ORDER_PAYMENT_RECIPIENT = "order_payment_recipient"        # orderData payment to another address
    ORDER_MULTIPLE_PAYMENTS = "order_multiple_payments"        # 2 payments
    ORDER_DEST_CALLS = "order_dest_calls"                      # non-empty calls
    ORDER_DEADLINE = "order_deadline"
    ORDER_ID_MISMATCH = "order_id_mismatch"                    # quote vs recomputed vs calldata
    DEPOSITORY_NOT_PINNED = "depository_not_pinned"
    DEPOSITORY_SPOOFED = "depository_spoofed"                  # protocol.v2 depository != pinned
    DEPOSIT_3ARG = "deposit_3arg"
    DEPOSITOR_NOT_USER = "depositor_not_user"
    APPROVE_SPENDER = "approve_spender"
    APPROVE_AMOUNT = "approve_amount"                          # approve amount > input
    APPROVE_TOKEN = "approve_token"
    UNKNOWN_SELECTOR = "unknown_selector"
    TARGET_NOT_PINNED = "target_not_pinned"
    FROM_MISMATCH = "from_mismatch"
    CHAIN_ID_MISMATCH = "chain_id_mismatch"
    VALUE_MISMATCH = "value_mismatch"
    GAS_PRICE_CAP = "gas_price_cap"                            # > 3x estimate
    GAS_LIMIT_CAP = "gas_limit_cap"
    TX_TYPE = "tx_type"
    DECIMALS_MISMATCH = "decimals_mismatch"                    # e.g. 6-dp scaled amount on an 18-dp BSC stable
    SOL_PROGRAM_NOT_ALLOWED = "sol_program_not_allowed"
    SOL_JUP_DEST_NOT_USER = "sol_jup_dest_not_user"            # JUP6 destination != user ATA
    SOL_JUP_AUTHORITY = "sol_jup_authority"
    SOL_CLOSE_NOT_USER = "sol_close_not_user"                  # CloseAccount to a non-user
    SOL_ATA_OWNER = "sol_ata_owner"                            # ATA owner != user
    SOL_TOKEN_IX = "sol_token_ix"                              # Token ix other than SyncNative/CloseAccount
    SOL_TOP_LEVEL_TRANSFER = "sol_top_level_transfer"
    SOL_MEMO_MISSING = "sol_memo_missing"
    SOL_TX_TOO_LARGE = "sol_tx_too_large"
    SOL_ALT_UNRESOLVED = "sol_alt_unresolved"
    SOL_FEE_PAYER_OUTFLOW = "sol_fee_payer_outflow"
    SIM_FAILED = "sim_failed"
    SIM_IN_DELTA = "sim_in_delta"
    SIM_OUT_DELTA = "sim_out_delta"
    SIM_OTHER_DECREASE = "sim_other_decrease"
    SIM_TRANSFER_UNEXPECTED = "sim_transfer_unexpected"        # router call paying a third address
    SIM_ALLOWANCE_LEFT = "sim_allowance_left"
    SIM_APPROVAL_OTHER = "sim_approval_other"


def chain_name(chain_id: int) -> str:
    """The chain's user-facing name ("base", "robinhood chain"); the bare id for one outside the registry."""
    s = str(chain_id)
    c = CHAINS.get(int(s)) if s.isascii() and s.isdigit() else None      # ASCII digits only: int() refuses "²"
    return c.display if c is not None else f"chain {chain_id}"


def chain_off_text(chain_id: int, open_chains=()) -> str:
    """CHAIN_NOT_ENABLED in plain words. phase0 WP12 (2026-10-12): "try another chain" sent users nowhere; the words
    say what works instead (the site still raises with page=True: Leon hears of it). `open_chains` = the chain ids
    open to this user (Monad 2026-10-13: "i trade on solana and monad right now"); none / Solana alone -> the
    Solana-only line."""
    keys = [CHAINS[int(c)].key for c in open_chains if str(c).isdigit() and int(c) in CHAINS]
    return chains_off_text(keys) if keys else SOLANA_ONLY_TEXT


def chain_not_granted_text(chain_id: int) -> str:
    return (f"your wallet hasn't okayed {chain_name(chain_id)} for me yet, so nothing was sent. tap the link to "
            "update permissions.")


# the failure templates (FABLE §3.1 item 7): what did not happen, why, the one next action. The words live in
# kiko_wire/reject_text.py so the brain's fallback is the same table, never a second copy (legacy audit P1-7)
PLAIN_TEXT: dict[RejectCode, str] = {RejectCode(k): v for k, v in REJECT_TEXT.items()}


class SignerReject(Exception):
    """Raised inside the signer with a named code. `user_text` is safe to show the user (PLAIN_TEXT when the site gave
    none); `detail` is for logs. `page`: the refusal's words promise "i've let the team know" (chain_off_text,
    PLAIN_TEXT[CHAIN_NOT_ENABLED]), so the raising site asks for the page explicitly (cards.issue pages Leon once per
    user and detail); the words themselves are never sniffed (legacy audit P1-13)."""

    def __init__(self, code: RejectCode | VerifyCode, detail: str = "", user_text: str = "",
                 extra: dict | None = None, *, page: bool = False):
        super().__init__(f"{code.value}: {detail}")
        self.code, self.detail, self.page = code, detail, bool(page)
        self.extra = dict(extra or {})          # card refusals: CardResponse EXT fields (min_usd, offer_id, …)
        self.user_text = user_text or (PLAIN_TEXT.get(code, "") if isinstance(code, RejectCode) else "")
