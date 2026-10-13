"""Simulation assertions (relay-exec; WP-R; doc 10 §3.4 C13, D16). Provider A for the signer (co-signer: provider B).

SimResult conventions (rpc_evm / rpc_solana produce them; the fakes' sim_handlers follow them):
  balance_deltas  "owner|chain:token" -> signed base units (EVM owner/token lowercase; native = 0x0…0 / 111…1)
  transfers       [{"token", "from", "to", "amount"}]   (ERC-20 Transfer logs + native value transfers)
  approvals       [{"token", "owner", "spender", "amount"}]
  raw["allowances_after"]  {"<token>|<spender>": int} read at the END of the simulated sequence
  raw["gas_used_per_tx"]   [int, …] (Monad gas-limit pricing uses the first entry)
  fee_payer_outflow        lamports (Solana)

EVM (the exact verified unsigned tx sequence; re-run before every step, landed steps included):
  - user's in_token balance falls by exactly in_base (native-in: plus at most the sequence's max gas cost)  SIM_IN_DELTA
  - same-chain: user's out_token rises by >= min_out (native-out: net of max gas)                         SIM_OUT_DELTA
  - no other user-held asset decreases (native: at most gas)                                              SIM_OTHER_DECREASE
  - every token/native transfer FROM the user is of in_token and goes to a pinned spender/router          SIM_TRANSFER_UNEXPECTED
  - allowance to the spender == 0 after the last step (must be reported for ERC-20-in shapes)             SIM_ALLOWANCE_LEFT
  - no Approval event by the user for any other spender                                                   SIM_APPROVAL_OTHER
Solana (simulateTransaction with accounts = user, user in/out ATAs, fee payer):
  - compiled size <= 1232 bytes                                    SOL_TX_TOO_LARGE (-> requote, maxRouteLength 3)
  - in falls by exactly in_base; out (same-chain) rises by >= min_out; no other user token account decreases;
  - fee-payer lamport outflow <= fee + priority cap + rent × new_atas, and no fee-payer token decreases
                                                                   SOL_FEE_PAYER_OUTFLOW
Acceptance (WP-R b): a router call that pays a third address is rejected (scripted FakeEvmRpc/FakeSolanaRpc
sim_handler; forked-state LIVE test when provider keys exist).
"""
from __future__ import annotations

from typing import Iterable

from kiko_wire.chains import EVM_NATIVE, SOL_NATIVE

from ..errors import VerifyCode
from ..types import Card, SimResult, UserAddresses, VerifyResult
from .chains import MAX_SOLANA_TX_BYTES
from .shapes import SOLANA_CHAIN_ID, is_native


def delta_key(owner: str, chain_id: int, token: str) -> str:
    if int(chain_id) != SOLANA_CHAIN_ID:
        owner, token = owner.lower(), token.lower()
    return f"{owner}|{int(chain_id)}:{token}"


def _split(key: str) -> tuple[str, int, str] | None:
    try:
        owner, rest = key.split("|", 1)
        chain, token = rest.split(":", 1)
        return owner, int(chain), token
    except ValueError:
        return None


def _fail(code: VerifyCode, detail: str) -> VerifyResult:
    return VerifyResult(False, None, code.value, detail)


def gas_for_tx(sim: SimResult, idx: int, n_txs: int) -> int | None:
    """Simulated gas of tx `idx` in a sequence of `n_txs` (Monad gas-limit pricing). None when not reported."""
    per = (sim.raw or {}).get("gas_used_per_tx")
    if isinstance(per, list) and len(per) == n_txs and isinstance(per[idx], int) and per[idx] > 0:
        return per[idx]
    if n_txs == 1 and sim.gas_used and sim.gas_used > 0:
        return sim.gas_used
    return None


def gas_for_first_tx(sim: SimResult) -> int | None:
    per = (sim.raw or {}).get("gas_used_per_tx")
    return gas_for_tx(sim, 0, len(per) if isinstance(per, list) and per else 1)


def assert_evm(sim: SimResult, *, card: Card, user: UserAddresses, spender: str, router: str, same_chain: bool,
               pinned: Iterable[str] = (), max_gas_wei: int = 0) -> VerifyResult:
    if not sim.ok:
        return _fail(VerifyCode.SIM_FAILED, sim.error or "simulation failed")
    chain = card.origin_chain
    u = user.evm.lower()
    in_tok, out_tok = card.in_token.address.lower(), card.out_token.address.lower()
    native_in, native_out = is_native(chain, in_tok), is_native(card.dest_chain, out_tok)
    deltas = {k: int(v) for k, v in (sim.balance_deltas or {}).items()}
    allowed_to = {a.lower() for a in pinned if a} | {a.lower() for a in (spender, router) if a}
    gas = max(int(max_gas_wei), 0)

    d_in = deltas.get(delta_key(u, chain, in_tok), 0)
    if native_in:
        if not (-card.in_base - gas <= d_in <= -card.in_base):
            return _fail(VerifyCode.SIM_IN_DELTA, f"native in delta {d_in} != -{card.in_base} (+gas <= {gas})")
    elif d_in != -card.in_base:
        return _fail(VerifyCode.SIM_IN_DELTA, f"in delta {d_in} != -{card.in_base}")
    if same_chain:
        d_out = deltas.get(delta_key(u, chain, out_tok), 0)
        floor = card.min_out_base - (gas if native_out else 0)
        if d_out < floor:
            return _fail(VerifyCode.SIM_OUT_DELTA, f"out delta {d_out} < min_out {card.min_out_base}")
    for k, v in deltas.items():
        parts = _split(k)
        if parts is None or parts[0].lower() != u or v >= 0:
            continue
        _, c, tok = parts
        if c == chain and tok.lower() in (in_tok, out_tok if same_chain else in_tok):
            continue
        if c == chain and tok.lower() == EVM_NATIVE and v >= -gas:
            continue
        return _fail(VerifyCode.SIM_OTHER_DECREASE, f"user asset {tok} on {c} decreases by {-v}")
    for t in sim.transfers or []:
        frm = str(t.get("from", "")).lower()
        if frm != u:
            continue
        tok = str(t.get("token", "")).lower()
        to = str(t.get("to", "")).lower()
        if tok != in_tok or to not in allowed_to:
            return _fail(VerifyCode.SIM_TRANSFER_UNEXPECTED, f"user transfer of {tok} to {to}")
    for a in sim.approvals or []:
        if str(a.get("owner", "")).lower() != u:
            continue
        if str(a.get("spender", "")).lower() != (spender or "").lower() or str(a.get("token", "")).lower() != in_tok:
            return _fail(VerifyCode.SIM_APPROVAL_OTHER, f"approval to {a.get('spender')} on {a.get('token')}")
    if spender:
        after = (sim.raw or {}).get("allowances_after")
        key = f"{in_tok}|{spender.lower()}"
        if not isinstance(after, dict) or key not in after:
            return _fail(VerifyCode.SIM_ALLOWANCE_LEFT, "simulation did not report the final allowance")
        if int(after[key]) != 0:
            return _fail(VerifyCode.SIM_ALLOWANCE_LEFT, f"allowance {after[key]} left to {spender}")
        for k, v in after.items():
            if int(v) != 0:
                return _fail(VerifyCode.SIM_ALLOWANCE_LEFT, f"allowance left on {k}")
    return VerifyResult(True)


def assert_solana(sim: SimResult, *, card: Card, user: UserAddresses, fee_payer: str, max_fee_payer_outflow: int,
                  same_chain: bool, size_bytes: int, user_fee_lamports: int = 0,
                  rent_refund_lamports: int = 0) -> VerifyResult:
    if size_bytes > MAX_SOLANA_TX_BYTES:
        return _fail(VerifyCode.SOL_TX_TOO_LARGE, f"{size_bytes} bytes > {MAX_SOLANA_TX_BYTES}")
    if not sim.ok:
        return _fail(VerifyCode.SIM_FAILED, sim.error or "simulation failed")
    chain = SOLANA_CHAIN_ID
    u = user.sol
    in_tok, out_tok = card.in_token.address, card.out_token.address
    deltas = {k: int(v) for k, v in (sim.balance_deltas or {}).items()}
    d_in = deltas.get(delta_key(u, chain, in_tok), 0)
    if in_tok == SOL_NATIVE:
        if not (-card.in_base - user_fee_lamports <= d_in <= -card.in_base + rent_refund_lamports):
            return _fail(VerifyCode.SIM_IN_DELTA, f"SOL in delta {d_in} != -{card.in_base}")
    elif d_in != -card.in_base:
        return _fail(VerifyCode.SIM_IN_DELTA, f"in delta {d_in} != -{card.in_base}")
    if same_chain:
        d_out = deltas.get(delta_key(u, chain, out_tok), 0)
        floor = card.min_out_base - (user_fee_lamports if out_tok == SOL_NATIVE else 0)
        if d_out < floor:
            return _fail(VerifyCode.SIM_OUT_DELTA, f"out delta {d_out} < min_out {card.min_out_base}")
    for k, v in deltas.items():
        parts = _split(k)
        if parts is None or v >= 0:
            continue
        owner, c, tok = parts
        if owner == fee_payer and fee_payer != u:
            if tok != SOL_NATIVE or -v > max_fee_payer_outflow:
                return _fail(VerifyCode.SOL_FEE_PAYER_OUTFLOW, f"fee payer {tok} outflow {-v}")
            continue
        if owner != u:
            continue
        if c == chain and tok in (in_tok, out_tok if same_chain else in_tok):
            continue
        if c == chain and tok == SOL_NATIVE and -v <= user_fee_lamports:
            continue
        return _fail(VerifyCode.SIM_OTHER_DECREASE, f"user token {tok} decreases by {-v}")
    if fee_payer and fee_payer != u and sim.fee_payer_outflow > max_fee_payer_outflow:
        return _fail(VerifyCode.SOL_FEE_PAYER_OUTFLOW,
                     f"fee payer outflow {sim.fee_payer_outflow} > {max_fee_payer_outflow}")
    return VerifyResult(True)
