"""Step-shape classification (relay-exec; WP-R; doc 10 §2.2 table, §3.4 A5).

classify(quote, origin_vm, same_chain, native_in) -> StepShape or raise SignerReject:
  - `steps` must be a list of objects; any step with kind "signature" -> SIGNATURE_STEP; any other non-"transaction"
    kind -> SHAPE_UNKNOWN
  - steps with empty items are skipped for execution (Relay step-execution doc) but their ids still count: an id that
    is not part of the expected shape -> EXTRA_STEP, even when empty
  - the non-empty step ids must equal the shape's list exactly, one item per step (a second item -> EXTRA_STEP)
  - EVM items must carry `data.to`/`data.data`; Solana items must carry `data.instructions`
Allowed lists (ids): EVM_SAME_NATIVE_IN [swap]; EVM_SAME_ERC20_IN [approve, swap]; EVM_CROSS_ERC20_IN
[approve, deposit]; EVM_CROSS_NATIVE_IN [deposit]; SOL_SAME [swap]; SOL_CROSS [deposit].
"""
from __future__ import annotations

from typing import Any

from kiko_wire.chains import EVM_NATIVE, SOL_NATIVE, SOLANA

from ..errors import SignerReject, VerifyCode
from ..types import StepShape

SHAPE_STEP_IDS: dict[StepShape, tuple[str, ...]] = {
    StepShape.EVM_SAME_NATIVE_IN: ("swap",),
    StepShape.EVM_SAME_ERC20_IN: ("approve", "swap"),
    StepShape.EVM_CROSS_ERC20_IN: ("approve", "deposit"),
    StepShape.EVM_CROSS_NATIVE_IN: ("deposit",),
    StepShape.SOL_SAME: ("swap",),
    StepShape.SOL_CROSS: ("deposit",),
}
SOLANA_CHAIN_ID = SOLANA.relay_id


def expected_shape(*, origin_vm: str, same_chain: bool, native_in: bool) -> StepShape:
    if origin_vm == "svm":
        return StepShape.SOL_SAME if same_chain else StepShape.SOL_CROSS
    if origin_vm != "evm":
        raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"unsupported origin vm {origin_vm!r}")
    if same_chain:
        return StepShape.EVM_SAME_NATIVE_IN if native_in else StepShape.EVM_SAME_ERC20_IN
    return StepShape.EVM_CROSS_NATIVE_IN if native_in else StepShape.EVM_CROSS_ERC20_IN


def active_steps(quote: dict[str, Any]) -> list[dict[str, Any]]:
    """Steps that carry items, in order (what the executor runs). Assumes classify() passed."""
    return [s for s in quote.get("steps") or [] if s.get("items")]


def classify(quote: dict[str, Any], *, origin_vm: str, same_chain: bool, native_in: bool) -> StepShape:
    shape = expected_shape(origin_vm=origin_vm, same_chain=same_chain, native_in=native_in)
    steps = quote.get("steps") if isinstance(quote, dict) else None
    if not isinstance(steps, list) or not steps:
        raise SignerReject(VerifyCode.SHAPE_UNKNOWN, "quote has no steps")
    want = SHAPE_STEP_IDS[shape]
    nonempty: list[str] = []
    for s in steps:
        if not isinstance(s, dict):
            raise SignerReject(VerifyCode.SHAPE_UNKNOWN, "step is not an object")
        if s.get("kind") == "signature":
            raise SignerReject(VerifyCode.SIGNATURE_STEP, f"signature step {s.get('id')!r} (no permits/EIP-712/7702)")
        if s.get("kind") != "transaction":
            raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"step kind {s.get('kind')!r}")
        sid = s.get("id")
        if sid not in want:
            raise SignerReject(VerifyCode.EXTRA_STEP, f"step id {sid!r} not in {shape.value} {list(want)}")
        items = s.get("items")
        if items is None:
            items = []
        if not isinstance(items, list):
            raise SignerReject(VerifyCode.SHAPE_UNKNOWN, "items is not a list")
        if len(items) > 1:
            raise SignerReject(VerifyCode.EXTRA_STEP, f"step {sid!r} has {len(items)} items")
        if items:
            it = items[0]
            data = it.get("data") if isinstance(it, dict) else None
            if not isinstance(data, dict):
                raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"step {sid!r} item has no data")
            if origin_vm == "evm" and not ("to" in data and "data" in data):
                raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"step {sid!r} is not an EVM transaction")
            if origin_vm == "svm" and not isinstance(data.get("instructions"), list):
                raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"step {sid!r} is not a Solana transaction")
            nonempty.append(sid)
    if len(nonempty) > len(want):
        raise SignerReject(VerifyCode.EXTRA_STEP, f"steps {nonempty} != {list(want)}")
    if tuple(nonempty) != want:
        raise SignerReject(VerifyCode.SHAPE_UNKNOWN, f"steps {nonempty} != {shape.value} {list(want)}")
    return shape


def is_native(chain_id: int, address: str) -> bool:
    if int(chain_id) == SOLANA_CHAIN_ID:
        return address == SOL_NATIVE
    return isinstance(address, str) and address.lower() == EVM_NATIVE


def classify_quote_for_chain(quote: dict[str, Any], request: dict[str, Any], pins) -> StepShape:
    """Classify a recorded quote from its request (checklist item 1) and check every EVM item hits a pinned target
    with a pinned selector for its role. Raises SignerReject on any deviation."""
    from .decode_evm import SEL_APPROVE, selector
    o, d = int(request["originChainId"]), int(request["destinationChainId"])
    vm = "svm" if o == SOLANA_CHAIN_ID else "evm"
    shape = classify(quote, origin_vm=vm, same_chain=(o == d), native_in=is_native(o, request["originCurrency"]))
    if vm == "evm":
        role_target = {"swap": pins.router(o) if shape is StepShape.EVM_SAME_NATIVE_IN else pins.approval_proxy(o),
                       "deposit": pins.depository(o)}
        for s in active_steps(quote):
            data = s["items"][0]["data"]
            sel = selector(data["data"])
            if s["id"] == "approve":
                if sel != SEL_APPROVE or data["to"].lower() != request["originCurrency"].lower():
                    raise SignerReject(VerifyCode.APPROVE_TOKEN, "approve not on the input token")
                continue
            if data["to"].lower() != role_target[s["id"]]:
                raise SignerReject(VerifyCode.TARGET_NOT_PINNED, f"{s['id']} -> {data['to']}")
            if sel not in pins.selectors_for(o, s["id"], shape):
                raise SignerReject(VerifyCode.UNKNOWN_SELECTOR, f"{s['id']} selector {sel}")
    return shape
