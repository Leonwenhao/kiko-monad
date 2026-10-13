"""debug_traceCall simulation fallback (Monad, 2026-10-13) over a scripted JSON-RPC node: the same SimResult as
eth_simulateV1, so assert_evm enforces the same rules; every provider gap, revert or anomaly is refused (fail closed).
The live Monad behaviour (withLog, stateOverrides, prestateTracer diffMode) is verified by the orchestrator, not here."""
from __future__ import annotations

import json

import httpx
import pytest

from kiko_signer.errors import SignerReject
from kiko_signer.errors import VerifyCode as V
from kiko_signer.relay import sim_trace as st
from kiko_signer.relay.chains import get as chain_cfg
from kiko_signer.relay.chains import registry
from kiko_signer.relay.evm_path import final_gas_fields
from kiko_signer.relay.rpc_evm import JsonRpcEvm
from kiko_signer.relay.simulate import assert_evm, delta_key, gas_for_tx
from kiko_signer.types import EvmTxRequest
from kiko_wire import chains as wc
from tests.trading.relay.helpers import ATTACKER, MONAD, SOL, SOL_USDC, make_card, user_addresses

USER = user_addresses()
U = USER.evm
MUSDC = wc.MONAD.cash.address
DEP = "0x4cd00e387622c35bddb9b4c962c136462338bc31"       # Relay EVM v2 depository (pinned)
ROUTER = "0xb92fe925dc43a0ecde6c8b1a2709c170ec4fff4f"
CHOG = "0x" + "c4" * 20
SLOT = "0x" + "ab" * 32                                    # the token's allowance[U][DEP] slot in the fake
IN = 5_000_000
APPROVE = st.SEL_APPROVE + DEP[2:].rjust(64, "0") + f"{IN:064x}"
DEPOSIT = "0x12345678" + "00" * 64
TRACE_GAS, EST_GAS = 40_000, 50_000


def word(n: int) -> str:
    return "0x" + f"{n:064x}"


def topic(a: str) -> str:
    return "0x" + a[2:].rjust(64, "0")


def transfer_log(token, frm, to, amt):
    return {"address": token, "topics": [st.TOPIC_TRANSFER, topic(frm), topic(to)], "data": word(amt)}


def approval_log(token, owner, spender, amt):
    return {"address": token, "topics": [st.TOPIC_APPROVAL, topic(owner), topic(spender)], "data": word(amt)}


class Node:
    """A Monad-like node: debug_traceCall (callTracer withLog / prestateTracer diffMode) + eth_estimateGas, scripted
    per call by `frame(req, overrides)` and `diff(req, overrides)`."""

    def __init__(self):
        self.logs_ok = True
        self.overrides_ok = True
        self.calls: list[dict] = []
        self.fail: dict[str, dict] = {}          # method/tracer -> JSON-RPC error
        self.http_status = 200
        self.leave_allowance = False
        self.deposit_extra_logs: list = []
        self.deposit_extra_calls: list = []
        self.deposit_error = ""
        self.user_balance_post_extra = 0
        self.probe_nested_logs = True             # callTracer withLog reports logs of nested frames
        self.probe_marks_revert = True            # a reverted subcall carries "error"
        self.probe_diff_ok = True                 # prestateTracer diffMode honours stateOverrides
        self.estimate_with_overrides: int | None = None   # eth_estimateGas(tx, block, overrides) answer; None = refuse

    # ---- scripted ERC-20 sell: approve(DEP, IN) then DEP.deposit pulls IN via transferFrom
    def allowance(self, ov) -> int:
        return int(((ov or {}).get(MUSDC, {}).get("stateDiff", {}) or {}).get(SLOT, word(0)), 16)

    def frame(self, req, ov):
        base = {"type": "CALL", "from": req["from"], "to": req["to"], "input": req["data"], "value": req["value"],
                "gasUsed": hex(TRACE_GAS)}
        if req["data"] == APPROVE:
            return {**base, "logs": [approval_log(MUSDC, U, DEP, IN)]}
        if req["data"].startswith(st.SEL_ALLOWANCE):
            return {**base, "output": word(self.allowance(ov))}
        if req["to"] == DEP:
            if self.deposit_error:
                return {**base, "error": "execution reverted", "revertReason": self.deposit_error}
            if self.allowance(ov) < IN:
                return {**base, "error": "execution reverted", "revertReason": "insufficient allowance"}
            pull = {"type": "CALL", "from": DEP, "to": MUSDC, "input": "0x23b872dd", "value": "0x0", "gasUsed": "0x1",
                    "logs": [transfer_log(MUSDC, U, DEP, IN)]}
            return {**base, "logs": list(self.deposit_extra_logs), "calls": [pull, *self.deposit_extra_calls]}
        raise AssertionError(f"unscripted call {req}")

    def diff(self, req, ov):
        if req["data"] == APPROVE:
            return {"pre": {U: {"balance": "0x0", "nonce": 0}, MUSDC: {"balance": "0x0"}},
                    "post": {U: {"nonce": 1}, MUSDC: {"storage": {SLOT: word(IN)}}}}
        if req["to"] == DEP:
            post = {U: {"nonce": 2}}
            if self.user_balance_post_extra:
                post[U]["balance"] = hex(self.user_balance_post_extra)
            storage = {SLOT: word(IN)} if self.leave_allowance else {}
            return {"pre": {U: {"balance": "0x0", "nonce": 1}, MUSDC: {"storage": {SLOT: word(IN)}}},
                    "post": {**post, MUSDC: {"storage": storage}}}
        raise AssertionError("unscripted diff")

    def probe_answer(self, req, cfg, ov):
        """The synthetic probe contracts (sim_trace.probe), as a geth-like node runs them."""
        honoured = bool(ov) and ov.get(st.PROBE_TO, {}).get("code") == st.PROBE_CODE \
            and ov.get(st.PROBE_OK, {}).get("code") == st.PROBE_OK_CODE \
            and ov.get(st.PROBE_OK, {}).get("stateDiff", {}).get(st.PROBE_SLOT) == st.PROBE_WORD \
            and ov.get(st.PROBE_REV, {}).get("code") == st.PROBE_REV_CODE
        if cfg["tracer"] == "prestateTracer":
            if not (honoured and self.probe_diff_ok):
                return {"pre": {}, "post": {}}
            return {"pre": {st.PROBE_OK: {"storage": {st.PROBE_SLOT: st.PROBE_WORD}}},
                    "post": {st.PROBE_OK: {"storage": {st.PROBE_SLOT1: st.PROBE_WORD}}}}
        if not honoured:                                       # no code at PROBE_TO: an empty call
            return {"type": "CALL", "from": req["from"], "to": req["to"], "value": "0x0", "gasUsed": "0x5208",
                    "output": "0x"}
        log = lambda a: {"address": a, "topics": [], "data": "0x"}  # noqa: E731
        inner = {"type": "CALL", "from": st.PROBE_TO, "to": st.PROBE_OK, "value": "0x0", "gasUsed": "0x10",
                 "output": st.PROBE_WORD}
        rev = {"type": "CALL", "from": st.PROBE_TO, "to": st.PROBE_REV, "value": "0x0", "gasUsed": "0x10",
               "output": "0x"}
        if self.probe_marks_revert:
            rev["error"] = "execution reverted"
        if self.logs_ok and cfg["tracerConfig"].get("withLog"):
            if self.probe_nested_logs:
                inner["logs"] = [log(st.PROBE_OK)]
            rev["logs"] = [log(st.PROBE_REV)]
        f = {"type": "CALL", "from": req["from"], "to": req["to"], "value": "0x0", "gasUsed": "0x10",
             "output": st.PROBE_WORD, "calls": [inner, rev]}
        return f

    def answer(self, body):
        method, params = body["method"], body["params"]
        self.calls.append(body)
        key = method if method != "debug_traceCall" else params[2]["tracer"]
        if key in self.fail:
            return {"error": self.fail[key]}
        if method == "eth_estimateGas":
            if len(params) > 2:
                if self.estimate_with_overrides is None:
                    return {"error": {"code": -32602, "message": "too many arguments"}}
                return {"result": hex(self.estimate_with_overrides)}
            return {"result": hex(EST_GAS)}
        req, _, cfg = params
        ov = cfg.get("stateOverrides") if self.overrides_ok else None
        if req["to"] == st.PROBE_TO:
            return {"result": self.probe_answer(req, cfg, ov)}
        if cfg["tracer"] == "prestateTracer":
            return {"result": self.diff(req, ov)}
        f = self.frame(req, ov)
        if not self.logs_ok:
            f.pop("logs", None)
        return {"result": f}

    def handler(self, request):
        if self.http_status != 200:
            return httpx.Response(self.http_status, json={})
        body = json.loads(request.content)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], **self.answer(body)})


def rpc_for(node: Node, **kw) -> JsonRpcEvm:
    return JsonRpcEvm({MONAD: {"a": "https://monad.example/KEY"}},
                      http=httpx.Client(transport=httpx.MockTransport(node.handler)), **kw)


SELL_TXS = [{"from": U, "to": MUSDC, "data": APPROVE, "value": 0, "gas": 200_000, "maxFeePerGas": 10 ** 11},
            {"from": U, "to": DEP, "data": DEPOSIT, "value": 0, "gas": 250_000, "maxFeePerGas": 10 ** 11}]
SELL_CARD = make_card(origin=MONAD, dest=SOL, in_tok=MUSDC, out_tok=SOL_USDC, in_base=IN, side="sell")


def check_sell(sim):
    return assert_evm(sim, card=SELL_CARD, user=USER, spender=DEP, router=DEP, same_chain=False,
                      pinned=frozenset({DEP}), max_gas_wei=10 ** 16)


# ---------------------------------------------------------------- config / dispatch

def test_monad_is_configured_for_debug_trace_call_and_the_others_keep_simulate_v1():
    assert chain_cfg(MONAD).simulation == "debug_traceCall"
    for cid, cfg in registry().items():
        if cfg.vm == "evm" and cid != MONAD:
            assert cfg.simulation == "eth_simulateV1", cid


def test_chain_without_a_configured_method_is_refused_never_skipped():
    node = Node()
    sim = rpc_for(node, sim_methods={}).simulate(MONAD, SELL_TXS)
    assert not sim.ok and "no simulation method" in sim.error and node.calls == []
    assert check_sell(sim).code == V.SIM_FAILED.value
    sim = rpc_for(node, sim_methods={MONAD: "skip"}).simulate(MONAD, SELL_TXS)
    assert not sim.ok and node.calls == []
    # a chain the registry does not know (no exec config) gets no method either
    rpc = JsonRpcEvm({999: {"a": "https://x.example"}}, http=httpx.Client(transport=httpx.MockTransport(node.handler)))
    assert not rpc.simulate(999, [dict(SELL_TXS[0])]).ok and node.calls == []


# ---------------------------------------------------------------- honest sell

def test_honest_erc20_sell_passes_with_the_same_simresult_shape():
    node = Node()
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert sim.ok, sim.error
    assert sim.balance_deltas[delta_key(U, MONAD, MUSDC)] == -IN
    assert sim.transfers == [{"token": MUSDC, "from": U, "to": DEP, "amount": IN}]
    assert sim.approvals == [{"token": MUSDC, "owner": U, "spender": DEP, "amount": IN}]
    assert sim.raw["allowances_after"] == {f"{MUSDC}|{DEP}": 0}
    assert check_sell(sim).ok
    # the deposit was traced on top of the approve's carried storage (it reverts without it in this node)
    dep_trace = [c for c in node.calls if c["method"] == "debug_traceCall" and c["params"][0]["to"] == DEP
                 and c["params"][2]["tracer"] == "callTracer"][0]
    assert dep_trace["params"][2]["stateOverrides"][MUSDC]["stateDiff"][SLOT] == word(IN)
    # the final allowance read sees the deposit's zeroed slot
    read = [c for c in node.calls if c["params"][0].get("data", "").startswith(st.SEL_ALLOWANCE)][0]
    assert read["params"][2]["stateOverrides"][MUSDC]["stateDiff"][SLOT] == st.ZERO_WORD


def test_gas_is_simulation_based_and_signed_at_x1_1():
    sim = rpc_for(Node()).simulate(MONAD, SELL_TXS)
    i0, i1 = st.intrinsic_gas(APPROVE), st.intrinsic_gas(DEPOSIT)
    # tx 0 needs no carried state: max(trace + intrinsic, eth_estimateGas); tx 1: trace + intrinsic
    assert sim.raw["gas_used_per_tx"] == [max(TRACE_GAS + i0, EST_GAS), TRACE_GAS + i1]
    cfg = chain_cfg(MONAD)
    req = EvmTxRequest(MONAD, U, DEP, DEPOSIT, 0, 250_000, 10 ** 11, 10 ** 9, "deposit")
    gas, _, _ = final_gas_fields(req, cfg, own_fee=10 ** 11, base_fee=10 ** 10, sim_gas=gas_for_tx(sim, 1, 2))
    assert gas == -(-(TRACE_GAS + i1) * 11 // 10)


def test_capability_probe_runs_once_per_chain_when_it_passes():
    node = Node()
    rpc = rpc_for(node)
    assert rpc.simulate(MONAD, SELL_TXS).ok and rpc.simulate(MONAD, SELL_TXS).ok
    probes = [c for c in node.calls if c["method"] == "debug_traceCall" and c["params"][0]["to"] == st.PROBE_TO]
    # one probe = one callTracer + one prestateTracer diffMode call, for both simulations together
    assert sorted(c["params"][2]["tracer"] for c in probes) == ["callTracer", "prestateTracer"]


# ---------------------------------------------------------------- adversarial: refuse

def test_trace_showing_a_revert_is_refused():
    node = Node()
    node.deposit_error = "boom"
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok and "reverted" in sim.error
    assert check_sell(sim).code == V.SIM_FAILED.value


def test_step_two_without_carried_state_reverts_and_is_refused():
    """A node that drops the approve's state would revert the deposit: the probe catches it before any trade."""
    node = Node()
    node.overrides_ok = False
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok and "stateOverrides" in sim.error


def test_provider_without_logs_is_refused():
    node = Node()
    node.logs_ok = False
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok and "withLog" in sim.error


@pytest.mark.parametrize("tracer", ["callTracer", "prestateTracer", "eth_estimateGas"])
def test_rpc_error_is_refused(tracer):
    node = Node()
    node.fail[tracer] = {"code": -32601, "message": "not supported yet"}
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok
    assert check_sell(sim).code == V.SIM_FAILED.value


def test_http_error_is_refused():
    node = Node()
    node.http_status = 400                                    # Alchemy's Monad answers debug_traceCall with 400
    assert not rpc_for(node).simulate(MONAD, SELL_TXS).ok


def test_unexpected_transfer_to_a_non_allowlisted_address_is_refused():
    node = Node()
    node.deposit_extra_calls = [{"type": "CALL", "from": DEP, "to": MUSDC, "value": "0x0", "gasUsed": "0x1",
                                 "logs": [transfer_log(MUSDC, U, ATTACKER, 1)]}]
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert sim.ok
    r = check_sell(sim)
    assert not r.ok and r.code in (V.SIM_IN_DELTA.value, V.SIM_TRANSFER_UNEXPECTED.value)


def test_a_transfer_inside_a_reverted_subcall_did_not_happen():
    node = Node()
    node.deposit_extra_calls = [{"type": "CALL", "from": DEP, "to": MUSDC, "value": "0x0", "gasUsed": "0x1",
                                 "error": "execution reverted", "logs": [transfer_log(MUSDC, U, ATTACKER, 1)]}]
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert sim.ok and check_sell(sim).ok


def test_approval_to_a_non_allowlisted_spender_is_refused():
    node = Node()
    node.deposit_extra_logs = [approval_log(MUSDC, U, ATTACKER, 2 ** 256 - 1)]
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert check_sell(sim).code == V.SIM_APPROVAL_OTHER.value


def test_allowance_left_after_the_sequence_is_refused():
    node = Node()
    node.leave_allowance = True
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert sim.ok and sim.raw["allowances_after"] == {f"{MUSDC}|{DEP}": IN}
    assert check_sell(sim).code == V.SIM_ALLOWANCE_LEFT.value


def test_native_value_leaving_in_a_subcall_is_refused():
    node = Node()
    node.deposit_extra_calls = [{"type": "CALL", "from": U, "to": ATTACKER, "value": hex(10 ** 18), "gasUsed": "0x1"}]
    node.user_balance_post_extra = 0           # the diff disagrees: callTracer says -1 MON, prestate says 0
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok and "disagrees" in sim.error


def test_a_trace_of_some_other_call_is_refused():
    node = Node()
    orig = node.frame
    node.frame = lambda req, ov: {**orig(req, ov), "from": ATTACKER} if req["data"] == APPROVE else orig(req, ov)
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok and "requested call" in sim.error


def test_an_unparseable_token_log_naming_the_user_is_refused():
    node = Node()
    node.deposit_extra_logs = [{"address": CHOG, "topics": [st.TOPIC_TRANSFER, topic(U), topic(ATTACKER), word(7)],
                                "data": "0x"}]
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok and "names the user" in sim.error


def test_unknown_frame_type_with_value_is_refused():
    node = Node()
    node.deposit_extra_calls = [{"type": "WEIRDCALL", "from": DEP, "to": ATTACKER, "value": "0x1", "gasUsed": "0x1"}]
    assert not rpc_for(node).simulate(MONAD, SELL_TXS).ok


# ---------------------------------------------------------------- native-in same-chain swap (single tx)

def test_native_in_same_chain_swap_single_tx():
    node = Node()
    swap = "0xabcdef01" + "00" * 32
    in_wei, out = 10 ** 18, 7_000

    def frame(req, ov):
        return {"type": "CALL", "from": req["from"], "to": ROUTER, "input": req["data"], "value": req["value"],
                "gasUsed": hex(300_000), "logs": [transfer_log(CHOG, ROUTER, U, out)]}
    node.frame = frame
    card = make_card(origin=MONAD, dest=MONAD, in_tok=wc.EVM_NATIVE, out_tok=CHOG, in_base=in_wei, min_out=out)
    sim = rpc_for(node).simulate(MONAD, [{"from": U, "to": ROUTER, "data": swap, "value": in_wei, "gas": 900_000}])
    assert sim.ok, sim.error
    assert sim.balance_deltas[delta_key(U, MONAD, wc.EVM_NATIVE)] == -in_wei
    # no prestate needed for a single tx with no approve (the capability probe's own diff call aside)
    assert not any(c["method"] == "debug_traceCall" and c["params"][2]["tracer"] == "prestateTracer"
                   and c["params"][0]["to"] != st.PROBE_TO for c in node.calls)
    ok = assert_evm(sim, card=card, user=USER, spender="", router=ROUTER, same_chain=True, max_gas_wei=10 ** 16)
    assert ok.ok
    bad = assert_evm(sim, card=make_card(origin=MONAD, dest=MONAD, in_tok=wc.EVM_NATIVE, out_tok=CHOG, in_base=in_wei,
                                         min_out=out + 1),
                     user=USER, spender="", router=ROUTER, same_chain=True, max_gas_wei=10 ** 16)
    assert bad.code == V.SIM_OUT_DELTA.value


# ---------------------------------------------------------------- merge_diff

def test_merge_diff_carries_zeroed_slots_and_refuses_deleted_accounts():
    ov: dict = {}
    st.merge_diff(ov, {"pre": {MUSDC: {"storage": {SLOT: word(5)}}}, "post": {MUSDC: {"balance": "0x2"}}})
    assert ov[MUSDC] == {"balance": "0x2", "stateDiff": {SLOT: st.ZERO_WORD}}
    with pytest.raises(st.TraceSimError):
        st.merge_diff(ov, {"pre": {MUSDC: {"balance": "0x1"}}, "post": {}})
    with pytest.raises(st.TraceSimError):
        st.merge_diff(ov, {"pre": []})


def test_final_gas_refuses_a_missing_simulated_gas_on_monad():
    req = EvmTxRequest(MONAD, U, DEP, DEPOSIT, 0, 250_000, 10 ** 11, 10 ** 9, "deposit")
    with pytest.raises(SignerReject):
        final_gas_fields(req, chain_cfg(MONAD), own_fee=10 ** 11, base_fee=10 ** 10, sim_gas=None)


# ---------------------------------------------------------------- review 2026-10-13: probe depth, top frame, carried gas

@pytest.mark.parametrize("gap,why", [("probe_nested_logs", "nested call"), ("probe_marks_revert", "reverted subcall"),
                                     ("probe_diff_ok", "diffMode")])
def test_probe_refuses_a_node_missing_a_property_the_simulation_relies_on(gap, why):
    node = Node()
    setattr(node, gap, False)
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok and why in sim.error
    assert check_sell(sim).code == V.SIM_FAILED.value
    # nothing past the probe was asked: no trade call was traced
    assert all(c["params"][0]["to"] == st.PROBE_TO for c in node.calls if c["method"] == "debug_traceCall")


def test_probe_contract_set_is_the_documented_one():
    assert st.PROBE_OK[2:] in st.PROBE_CODE and st.PROBE_REV[2:] in st.PROBE_CODE
    assert st.PROBE_CODE.endswith("5af15060206000f3") and st.PROBE_REV_CODE.endswith("fd")
    for c in (st.PROBE_CODE, st.PROBE_OK_CODE, st.PROBE_REV_CODE):
        bytes.fromhex(c[2:])


@pytest.mark.parametrize("drop", ["input", "gasUsed"])
def test_a_top_frame_without_input_or_gas_used_is_refused(drop):
    node = Node()
    orig = node.frame

    def frame(req, ov):
        f = orig(req, ov)
        if req["data"] == APPROVE:
            f.pop(drop)
        return f
    node.frame = frame
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert not sim.ok and (drop if drop == "input" else "gasUsed") in sim.error


def test_carried_step_takes_an_override_aware_estimate_when_it_is_higher():
    node = Node()
    node.estimate_with_overrides = 300_000
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert sim.ok, sim.error
    assert sim.raw["gas_used_per_tx"][1] == 300_000
    est = [c for c in node.calls if c["method"] == "eth_estimateGas" and len(c["params"]) == 3][0]
    assert est["params"][2][MUSDC]["stateDiff"][SLOT] == word(IN)       # estimated on the approve's carried state


def test_carried_step_keeps_the_trace_gas_when_the_node_refuses_overrides_on_estimate():
    node = Node()                                             # estimate_with_overrides None: the node errors
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert sim.ok and sim.raw["gas_used_per_tx"][1] == TRACE_GAS + st.intrinsic_gas(DEPOSIT)
    node = Node()
    node.estimate_with_overrides = 1_000                      # a lower estimate never lowers the reserve
    sim = rpc_for(node).simulate(MONAD, SELL_TXS)
    assert sim.ok and sim.raw["gas_used_per_tx"][1] == TRACE_GAS + st.intrinsic_gas(DEPOSIT)
