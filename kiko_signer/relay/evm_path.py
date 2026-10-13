"""EVM step path (relay-exec; WP-R; doc 10 §2.5 7h).

normalize_item(item_data) -> EvmTxRequest (strict ints, lowercase addresses; Relay's gas fields kept for C12)
build_unsigned(req, nonce, *, gas, max_fee, max_prio) -> EvmUnsignedTx: 0x02 || rlp([chainId, nonce, maxPrio, maxFee,
  gas, to, value, data, []]); `unsigned_hash` = sha256(bytes). Never type-4, never an access list.
final_gas_fields(req, chain_cfg, own_fee, sim): what the signer actually signs:
  - gas: Relay's value (already <= the chain ceiling, C12); Monad: ceil(own simulation gas × 1.1), never Relay's
    padded value (doc 10 §4.5, WP-F e), and still <= the ceiling;
  - maxFeePerGas: Relay's value, raised to the signer's own estimate when Relay's would not land (stale quote), and
    never above 3× the own estimate; maxPriorityFeePerGas <= maxFeePerGas.
EvmPath.run(…): for each step in order: pause re-check -> simulate the full verified sequence on provider A ->
  simulate.assert_evm -> final gas fields -> TxSigner.sign_evm (sign_log + nonce in one transaction, signer-core) ->
  local check that the signed bytes are exactly the verified request from the user -> broadcast -> receipt before the
  next step. A failure after a landed `approve` and before `swap`/`deposit` raises ApproveStranded so the executor
  hands the exec to the tracker's revoke (approve(spender, 0)).
"""
from __future__ import annotations

import hashlib
import math
import re
from decimal import Decimal
from typing import Any, Callable

import rlp
from eth_account import Account
from eth_hash.auto import keccak

from ..errors import SignerReject, VerifyCode
from ..types import Card, EvmTxRequest, EvmUnsignedTx, UserAddresses
from . import simulate as simu
from .chains import INTRINSIC_GAS
from .decode_evm import norm_address, norm_hex

_INT = re.compile(r"^[0-9]{1,78}$")
_HEXINT = re.compile(r"^0x[0-9a-fA-F]{1,64}$")
MAX_U256 = (1 << 256) - 1


class StepFailed(Exception):
    """A step could not complete (reverted / rejected before broadcast). `landed` lists hashes that did land."""

    def __init__(self, msg: str, *, step_idx: int, landed: list[str], reason: str = ""):
        super().__init__(msg)
        self.step_idx, self.landed, self.reason = step_idx, landed, reason or msg


class ApproveStranded(StepFailed):
    """An approve landed but the swap/deposit did not: the allowance must be revoked (tracker revoke)."""


class Paused(Exception):
    def __init__(self, scope: str, *, step_idx: int, landed: list[str]):
        super().__init__(f"paused: {scope}")
        self.scope, self.step_idx, self.landed = scope, step_idx, landed


def _uint(v: Any, what: str) -> int:
    if isinstance(v, bool):
        raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"{what} is a bool")
    if isinstance(v, int):
        n = v
    elif isinstance(v, str) and _INT.match(v):
        n = int(v)
    elif isinstance(v, str) and _HEXINT.match(v):
        n = int(v, 16)
    else:
        raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"{what} is not an unsigned integer")
    if not 0 <= n <= MAX_U256:
        raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"{what} out of range")
    return n


def normalize_item(item_data: dict[str, Any], *, step_id: str) -> EvmTxRequest:
    if not isinstance(item_data, dict):
        raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"{step_id}: item data is not an object")
    return EvmTxRequest(
        chain_id=_uint(item_data.get("chainId"), "chainId"),
        from_addr=norm_address(item_data.get("from"), VerifyCode.FROM_MISMATCH),
        to=norm_address(item_data.get("to"), VerifyCode.TARGET_NOT_PINNED),
        data=norm_hex(item_data.get("data", "0x") or "0x"),
        value=_uint(item_data.get("value", "0") or "0", "value"),
        gas=_uint(item_data.get("gas", "0") or "0", "gas"),
        max_fee_per_gas=_uint(item_data.get("maxFeePerGas", "0") or "0", "maxFeePerGas"),
        max_priority_fee_per_gas=_uint(item_data.get("maxPriorityFeePerGas", "0") or "0", "maxPriorityFeePerGas"),
        step_id=step_id,
    )


def build_unsigned(req: EvmTxRequest, nonce: int, *, gas: int, max_fee_per_gas: int,
                   max_priority_fee_per_gas: int) -> EvmUnsignedTx:
    for name, v in (("nonce", nonce), ("gas", gas), ("max_fee_per_gas", max_fee_per_gas),
                    ("max_priority_fee_per_gas", max_priority_fee_per_gas)):
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise ValueError(f"{name} must be a non-negative int")
    if gas < INTRINSIC_GAS:
        raise ValueError("gas below the intrinsic minimum")
    if max_priority_fee_per_gas > max_fee_per_gas:
        raise ValueError("priority fee above max fee")
    to = norm_address(req.to, VerifyCode.TARGET_NOT_PINNED)
    data = norm_hex(req.data)
    payload = rlp.encode([req.chain_id, nonce, max_priority_fee_per_gas, max_fee_per_gas, gas, bytes.fromhex(to[2:]),
                          req.value, bytes.fromhex(data[2:]), []])
    raw = b"\x02" + payload
    return EvmUnsignedTx(chain_id=req.chain_id, nonce=nonce, to=to, data=data, value=req.value, gas=gas,
                         max_fee_per_gas=max_fee_per_gas, max_priority_fee_per_gas=max_priority_fee_per_gas,
                         unsigned_hex="0x" + raw.hex(), unsigned_hash=hashlib.sha256(raw).hexdigest())


def own_max_fee(fee_estimate: dict[str, int]) -> int:
    """The signer's own maxFeePerGas estimate (C12 reference): 2 x base fee + priority."""
    base = int(fee_estimate.get("base_fee", 0))
    prio = int(fee_estimate.get("max_priority_fee", 0))
    return max(2 * base + prio, 1)


def final_gas_fields(req: EvmTxRequest, chain_cfg: Any, *, own_fee: int, base_fee: int,
                     sim_gas: int | None) -> tuple[int, int, int]:
    """(gas, maxFeePerGas, maxPriorityFeePerGas) the signer signs. Raises SignerReject on a cap breach."""
    ceiling = chain_cfg.ceiling(req.step_id)
    if chain_cfg.gas_from_simulation:
        if not sim_gas or sim_gas <= 0:
            raise SignerReject(VerifyCode.SIM_FAILED, f"{req.step_id}: no simulated gas (gas-limit-billed chain)")
        gas = math.ceil(Decimal(sim_gas) * Decimal(chain_cfg.gas_sim_multiplier))
    else:
        gas = req.gas
    gas = max(gas, INTRINSIC_GAS)
    if gas > ceiling:
        raise SignerReject(VerifyCode.GAS_LIMIT_CAP, f"{req.step_id}: gas {gas} > ceiling {ceiling}")
    pad = Decimal(getattr(chain_cfg, "l1_gas_pad", 1) or 1)
    if pad > 1 and not chain_cfg.gas_from_simulation:      # headroom only; never past the ceiling
        gas = min(ceiling, math.ceil(Decimal(gas) * pad))
    cap = chain_cfg.max_fee_multiple_of_estimate * own_fee
    max_fee = req.max_fee_per_gas
    if max_fee < base_fee:
        max_fee = own_fee
    elif pad > 1:                                           # L1-folding chains: never below our own 2x-base estimate
        max_fee = max(max_fee, own_fee)
    if max_fee > cap:
        raise SignerReject(VerifyCode.GAS_PRICE_CAP, f"{req.step_id}: maxFeePerGas above {cap}")
    prio = min(req.max_priority_fee_per_gas, max_fee)
    return gas, max_fee, prio


def decode_signed(raw_tx: str) -> dict[str, Any]:
    """Decode a signed type-2 tx and recover its sender (local, no RPC)."""
    raw = bytes.fromhex(raw_tx[2:] if raw_tx.startswith("0x") else raw_tx)
    if raw[:1] != b"\x02":
        raise SignerReject(VerifyCode.TX_TYPE, "signed tx is not type-2")
    f = rlp.decode(raw[1:])
    if len(f) != 12:
        raise SignerReject(VerifyCode.TX_TYPE, "signed type-2 tx has the wrong field count")
    i = lambda b: int.from_bytes(b, "big")  # noqa: E731
    return {"chainId": i(f[0]), "nonce": i(f[1]), "maxPriorityFeePerGas": i(f[2]), "maxFeePerGas": i(f[3]),
            "gas": i(f[4]), "to": "0x" + f[5].hex(), "value": i(f[6]), "data": "0x" + f[7].hex(),
            "accessList": f[8], "from": Account.recover_transaction(raw).lower(), "hash": "0x" + keccak(raw).hex()}


def assert_signed_matches(raw_tx: str, req: EvmTxRequest, *, gas: int, max_fee: int, prio: int) -> dict[str, Any]:
    """The bytes we are about to broadcast are exactly the verified request, signed by the user's key."""
    tx = decode_signed(raw_tx)
    want = {"chainId": req.chain_id, "to": req.to, "value": req.value, "data": req.data.lower(), "gas": gas,
            "maxFeePerGas": max_fee, "maxPriorityFeePerGas": prio, "from": req.from_addr}
    for k, v in want.items():
        if tx[k] != v:
            raise SignerReject(VerifyCode.FROM_MISMATCH if k == "from" else VerifyCode.TARGET_NOT_PINNED,
                               f"signed tx {k} differs from the verified request")
    if tx["accessList"]:
        raise SignerReject(VerifyCode.TX_TYPE, "signed tx carries an access list")
    return tx


def sim_txs(steps: list[EvmTxRequest]) -> list[dict[str, Any]]:
    return [{"from": s.from_addr, "to": s.to, "data": s.data, "value": s.value, "gas": s.gas, "chainId": s.chain_id,
             "maxFeePerGas": s.max_fee_per_gas, "maxPriorityFeePerGas": s.max_priority_fee_per_gas} for s in steps]


class EvmPath:
    def __init__(self, rpc, signer, broadcaster, chain_cfgs, clock):
        self.rpc, self.signer, self.broadcaster = rpc, signer, broadcaster
        self.chain_cfgs, self.clock = chain_cfgs, clock

    def run(self, *, exec_id: str, card: Card, user: UserAddresses, steps: list[EvmTxRequest], relay_request_id: str,
            order_id: str, spender: str = "", target: str = "", pins=None,
            before_step: Callable[[int], str | None] | None = None,
            after_broadcast: Callable[[int, str], None] | None = None,
            before_sign: Callable[[int], None] | None = None) -> list[str]:
        """Returns tx hashes in step order. `before_step(i)` returns a paused scope (or None); `before_sign(i)` runs
        when step i starts (simulate + sign: the executor's SIGNING state); `after_broadcast(i, hash)` once the step's
        receipt is in (BROADCAST)."""
        if not steps:
            raise StepFailed("no steps", step_idx=0, landed=[])
        chain_id = steps[0].chain_id
        cfg = self.chain_cfgs[chain_id]
        same_chain = card.origin_chain == card.dest_chain
        landed: list[str] = []
        approved = False
        pair = len(steps) > 1 and bool(getattr(self.signer, "presubmits", False))
        finals: dict[int, tuple[EvmTxRequest, Any]] = {}
        for i, req in enumerate(steps):
            if req.chain_id != chain_id or req.from_addr != user.evm:
                raise SignerReject(VerifyCode.CHAIN_ID_MISMATCH, "steps span chains or senders")
            scope = before_step(i) if before_step else None
            if scope:
                if approved:
                    raise ApproveStranded(f"paused ({scope}) after approve", step_idx=i, landed=landed,
                                          reason=f"paused:{scope}")
                raise Paused(scope, step_idx=i, landed=landed)
            if before_sign:
                before_sign(i)
            # Always the FULL verified sequence: re-simulating an already-landed exact approve is idempotent, and it
            # is what lets the provider report the final allowance (SIM_ALLOWANCE_LEFT) for the swap/deposit step.
            sim = self.rpc.simulate(chain_id, sim_txs(steps))
            fee = self.rpc.fee_estimate(chain_id)
            own = own_max_fee(fee)
            max_gas_wei = sum(s.gas * max(s.max_fee_per_gas, own) for s in steps)
            vr = simu.assert_evm(sim, card=card, user=user, spender=spender, router=target, same_chain=same_chain,
                                 pinned=frozenset(p for p in (spender, target, *(pins or ())) if p),
                                 max_gas_wei=max_gas_wei)
            if not vr.ok:
                err = SignerReject(VerifyCode(vr.code), vr.detail)
                if approved:
                    raise ApproveStranded(f"simulation rejected step {i}: {vr.code}", step_idx=i, landed=landed,
                                          reason=vr.code) from err
                raise err
            if pair and i == 0:
                # stage 1: every step's final tx is fixed now and submitted in nonce order before any wait, so the
                # co-signer sees approve + swap/deposit together (cosigner seam #1); later steps reuse these bytes.
                for j, rq in enumerate(steps):
                    g, mf, pr = final_gas_fields(rq, cfg, own_fee=own, base_fee=int(fee.get("base_fee", 0)),
                                                 sim_gas=simu.gas_for_tx(sim, j, len(steps)))
                    fj = EvmTxRequest(rq.chain_id, rq.from_addr, rq.to, rq.data, rq.value, g, mf, pr, rq.step_id)
                    finals[j] = (fj, sim)
                    self.signer.presubmit_evm(exec_id=exec_id, step_idx=j, card=card, user=user, req=fj, sim=sim,
                                              relay_request_id=relay_request_id, order_id=order_id)
            if i in finals:
                final, sim = finals[i]
                gas, max_fee, prio = final.gas, final.max_fee_per_gas, final.max_priority_fee_per_gas
            else:
                gas, max_fee, prio = final_gas_fields(req, cfg, own_fee=own, base_fee=int(fee.get("base_fee", 0)),
                                                      sim_gas=simu.gas_for_tx(sim, i, len(steps)))
                final = EvmTxRequest(req.chain_id, req.from_addr, req.to, req.data, req.value, gas, max_fee, prio,
                                     req.step_id)
            raw = self.signer.sign_evm(exec_id=exec_id, step_idx=i, card=card, user=user, req=final, sim=sim,
                                       relay_request_id=relay_request_id, order_id=order_id)
            assert_signed_matches(raw, final, gas=gas, max_fee=max_fee, prio=prio)
            try:
                h = self.broadcaster.send_evm(exec_id=exec_id, step_idx=i, chain_id=chain_id, raw_tx=raw,
                                              timeout_s=cfg.receipt_timeout_s, poll_s=cfg.receipt_poll_s)
            except Exception as e:
                if approved and not getattr(e, "unknown", False):
                    raise ApproveStranded(f"step {i} failed after approve: {e}", step_idx=i, landed=landed,
                                          reason=type(e).__name__) from e
                raise
            landed.append(h)
            if after_broadcast:
                after_broadcast(i, h)
            if req.step_id == "approve":
                approved = True
        return landed
