"""debug_traceCall simulation (relay-exec; Monad, 2026-10-13). Provider A for chains whose ChainExecConfig says
`simulation="debug_traceCall"` (Monad: rpc.monad.xyz refuses eth_simulateV1 with validation=false).

Produces the SAME SimResult as rpc_evm.parse_simulate_v1, so simulate.assert_evm enforces the same rules unchanged
(SIM_IN_DELTA, SIM_OUT_DELTA, SIM_OTHER_DECREASE, SIM_TRANSFER_UNEXPECTED, SIM_APPROVAL_OTHER, SIM_ALLOWANCE_LEFT) and
evm_path.final_gas_fields still signs ceil(simulated gas x 1.1).

How a sequence (approve + deposit/swap) is simulated with a single-call method:
  1. capability probe, once per chain (cached when it passes; see `probe`): synthetic contracts set up through
     `stateOverrides` must show a NESTED frame's log (withLog), a reverted subcall marked with an error, the
     overridden storage value, and a prestateTracer diffMode post state of the overridden contract. A provider that
     fails any of these is refused, never trusted: without logs ERC-20 movements are invisible; an unmarked reverted
     subcall would count rolled-back transfers; without overrides step 2 would be checked against the wrong state.
  2. per tx i: callTracer (withLog) on top of the carried state. Top frame must be exactly the request (from, to,
     input, value). A top-level error = revert -> refuse. Native moves = value of CALL/CREATE/CREATE2/SELFDESTRUCT
     frames; logs and value of any frame with an error (and its whole subtree) are dropped (they were rolled back).
  3. when a later tx or an allowance read needs it: prestateTracer diffMode on the same carried state; its post state
     becomes `stateOverrides` (balance, nonce, code, stateDiff; zeroed slots carried as 0) for the next call. An
     account deleted by the simulation is refused. The user's native balance change in the diff must equal the
     frame-derived native delta (cross-check).
  4. allowance(owner, spender) for every approve in the sequence, read with callTracer on the FINAL carried state.
  Gas per tx = max(trace gasUsed + intrinsic(calldata), eth_estimateGas; on carried state only when the node takes the
  overrides, else the trace figure alone): clients
  differ on whether the top frame's gasUsed counts intrinsic gas, so it is added (over-reserving costs a little on a
  gas-limit-billed chain; under-reserving reverts).
Any RPC error, malformed answer or failed check -> SimResult(ok=False) -> SIM_FAILED. Nothing is skipped.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Sequence

from kiko_wire import chains as wire_chains

from ..types import SimResult

TOPIC_TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
TOPIC_APPROVAL = "0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925"
SEL_ALLOWANCE = "0xdd62ed3e"
SEL_APPROVE = "0x095ea7b3"
PROBE_FROM = "0x000000000000000000000000000000000c05e7e1"
PROBE_TO = "0x000000000000000000000000000000000c05e7e3"
PROBE_OK = "0x000000000000000000000000000000000c05e7e4"
PROBE_REV = "0x000000000000000000000000000000000c05e7e5"
# PROBE_TO: CALL(gas, PROBE_OK, 0, 0, 0, 0, 32); POP; CALL(gas, PROBE_REV, 0, 0, 0, 0, 0); POP; RETURN(0, 32)
PROBE_CODE = ("0x6020600060006000600073" + PROBE_OK[2:] + "5af150" + "6000600060006000600073" + PROBE_REV[2:]
              + "5af15060206000f3")
# PROBE_OK: LOG0(0,0); v = SLOAD(0); SSTORE(1, v); MSTORE(0, v); RETURN(0, 32)
PROBE_OK_CODE = "0x60006000a06000548060015560005260206000f3"
# PROBE_REV: LOG0(0,0); REVERT(0, 0)  (its log must not count and its frame must carry an error)
PROBE_REV_CODE = "0x60006000a060006000fd"
PROBE_SLOT = "0x" + "00" * 32
PROBE_SLOT1 = "0x" + "00" * 31 + "01"
PROBE_WORD = "0x" + f"{42:064x}"
ZERO_WORD = "0x" + "00" * 32
VALUE_FRAMES = frozenset({"CALL", "CREATE", "CREATE2", "SELFDESTRUCT"})
NO_VALUE_FRAMES = frozenset({"DELEGATECALL", "STATICCALL", "CALLCODE"})
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX = re.compile(r"^0x[0-9a-fA-F]*$")

Call = Callable[[str, list[Any]], Any]          # (method, params) -> result; raises on transport / JSON-RPC error


class TraceSimError(Exception):
    """A malformed / unusable trace answer or a failed consistency check (-> SimResult ok=False)."""


def _qty(v: Any, what: str) -> int:
    if v is None:
        return 0
    if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
        return v
    if isinstance(v, str) and _HEX.match(v):
        return int(v, 16) if len(v) > 2 else 0
    raise TraceSimError(f"{what} is not a hex quantity")


def _a(v: Any, what: str) -> str:
    if not isinstance(v, str) or not _ADDR.match(v):
        raise TraceSimError(f"{what} is not an address")
    return v.lower()


def _hexdata(v: Any) -> str:
    s = str(v or "0x").lower()
    if not _HEX.match(s) or len(s) % 2:
        raise TraceSimError("data is not hex")
    return s


def intrinsic_gas(data: str) -> int:
    """21,000 + calldata cost; the larger of the classic (4/16 per byte) and the EIP-7623 floor (10/40)."""
    b = bytes.fromhex(_hexdata(data)[2:])
    zero = b.count(0)
    nonzero = len(b) - zero
    return max(21_000 + 4 * zero + 16 * nonzero, 21_000 + 10 * zero + 40 * nonzero)


def _call_obj(t: dict[str, Any]) -> dict[str, Any]:
    out = {"from": _a(t["from"], "from"), "to": _a(t["to"], "to"), "data": _hexdata(t.get("data", "0x")),
           "value": hex(int(t.get("value", 0)))}
    if int(t.get("gas", 0) or 0) > 0:
        out["gas"] = hex(int(t["gas"]))
    return out


def _cfg(tracer: str, overrides: dict[str, Any], tracer_config: dict[str, Any]) -> dict[str, Any]:
    c: dict[str, Any] = {"tracer": tracer, "tracerConfig": tracer_config}
    if overrides:
        c["stateOverrides"] = overrides
    return c


def probe(call: Call) -> str:
    """"" when the provider honours what simulate_trace relies on, else the reason. One synthetic contract set (code +
    storage via `stateOverrides`): PROBE_TO calls PROBE_OK (logs, copies slot 0 = 42 to slot 1, returns it) and then
    PROBE_REV (logs, reverts). Required: callTracer withLog reports the NESTED frame's log (ERC-20 Transfers come from
    nested calls), marks the reverted subcall with an error (its log is dropped only through that mark), returns the
    overridden value (code / stateDiff overrides honoured); prestateTracer diffMode on the same overrides shows slot 1
    of PROBE_OK = 42 in `post` (diffMode + overrides honoured: that is how state is carried between steps)."""
    req = {"from": PROBE_FROM, "to": PROBE_TO, "data": "0x", "value": "0x0", "gas": hex(1_000_000)}
    ov = {PROBE_TO: {"code": PROBE_CODE}, PROBE_OK: {"code": PROBE_OK_CODE, "stateDiff": {PROBE_SLOT: PROBE_WORD}},
          PROBE_REV: {"code": PROBE_REV_CODE}}
    try:
        res = call("debug_traceCall", [req, "latest", _cfg("callTracer", ov, {"withLog": True})])
    except Exception as e:  # noqa: BLE001 - any provider failure means "cannot be trusted"
        return f"debug_traceCall probe failed: {str(e)[:120]}"
    if not isinstance(res, dict) or res.get("error"):
        return "debug_traceCall probe reverted or returned no frame"
    if str(res.get("output", "")).lower() != PROBE_WORD:
        return "debug_traceCall ignored stateOverrides (code / stateDiff)"
    calls = res.get("calls")
    if not isinstance(calls, list):
        return "debug_traceCall callTracer returned no nested calls"
    ok = [c for c in calls if isinstance(c, dict) and str(c.get("to", "")).lower() == PROBE_OK]
    rev = [c for c in calls if isinstance(c, dict) and str(c.get("to", "")).lower() == PROBE_REV]
    if len(ok) != 1 or len(rev) != 1:
        return "debug_traceCall callTracer did not report the probe's nested calls"
    logs = ok[0].get("logs")
    if ok[0].get("error") or not isinstance(logs, list) or not any(
            isinstance(lg, dict) and str(lg.get("address", "")).lower() == PROBE_OK for lg in logs):
        return "debug_traceCall callTracer returned no logs for a nested call (withLog unsupported)"
    if not rev[0].get("error"):
        return "debug_traceCall callTracer does not mark a reverted subcall with an error"
    try:
        diff = call("debug_traceCall", [req, "latest", _cfg("prestateTracer", ov, {"diffMode": True})])
    except Exception as e:  # noqa: BLE001
        return f"debug_traceCall prestateTracer probe failed: {str(e)[:120]}"
    post = diff.get("post") if isinstance(diff, dict) else None
    acct = ({str(k).lower(): v for k, v in post.items()}.get(PROBE_OK) if isinstance(post, dict) else None) or {}
    storage = acct.get("storage") if isinstance(acct, dict) else None
    try:
        slots = {int(str(k), 16): int(str(v), 16) for k, v in (storage or {}).items()}
    except ValueError:
        slots = {}
    if slots.get(1) != 42:
        return "debug_traceCall prestateTracer diffMode ignored stateOverrides or did not report the change"
    return ""


def _walk(frame: Any, *, chain_id: int, user: str, deltas: dict[str, int], transfers: list, approvals: list,
          native: dict[str, int], depth: int = 0) -> None:
    if not isinstance(frame, dict):
        raise TraceSimError("call frame is not an object")
    if depth > 1100:
        raise TraceSimError("call tree too deep")
    if frame.get("error"):
        return                                     # rolled back: nothing in this subtree happened
    typ = str(frame.get("type", "")).upper()
    value = _qty(frame.get("value"), "frame value")
    if value:
        if typ in VALUE_FRAMES:
            frm, to = _a(frame.get("from"), "frame from"), _a(frame.get("to"), "frame to")
            tok = wire_chains.EVM_NATIVE
            transfers.append({"token": tok, "from": frm, "to": to, "amount": value})
            for who, d in ((frm, -value), (to, value)):
                k = f"{who}|{chain_id}:{tok}"
                deltas[k] = deltas.get(k, 0) + d
                native[who] = native.get(who, 0) + d
        elif typ not in NO_VALUE_FRAMES:
            raise TraceSimError(f"unknown frame type {typ!r} carries value")
    logs = frame.get("logs") or []
    if not isinstance(logs, list):
        raise TraceSimError("logs is not a list")
    for lg in logs:
        _log(lg, chain_id=chain_id, user=user, deltas=deltas, transfers=transfers, approvals=approvals)
    calls = frame.get("calls") or []
    if not isinstance(calls, list):
        raise TraceSimError("calls is not a list")
    for c in calls:
        _walk(c, chain_id=chain_id, user=user, deltas=deltas, transfers=transfers, approvals=approvals,
              native=native, depth=depth + 1)


def _log(lg: Any, *, chain_id: int, user: str, deltas: dict[str, int], transfers: list, approvals: list) -> None:
    if not isinstance(lg, dict) or not isinstance(lg.get("topics"), list):
        raise TraceSimError("log is not an object with topics")
    topics = [str(t).lower() for t in lg["topics"]]
    if not topics or topics[0] not in (TOPIC_TRANSFER, TOPIC_APPROVAL):
        return
    token = _a(lg.get("address"), "log address")
    data = _hexdata(lg.get("data"))[2:]
    if len(topics) != 3 or not data or len(data) > 64:
        # an ERC-721 / malformed Transfer or Approval: harmless unless it names the user (then we cannot book it)
        if any(t[-40:] == user[2:] for t in topics[1:]):
            raise TraceSimError("unparseable Transfer/Approval log names the user")
        return
    frm, to = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
    amt = int(data, 16)
    if topics[0] == TOPIC_TRANSFER:
        transfers.append({"token": token, "from": frm, "to": to, "amount": amt})
        deltas[f"{frm}|{chain_id}:{token}"] = deltas.get(f"{frm}|{chain_id}:{token}", 0) - amt
        deltas[f"{to}|{chain_id}:{token}"] = deltas.get(f"{to}|{chain_id}:{token}", 0) + amt
    else:
        approvals.append({"token": token, "owner": frm, "spender": to, "amount": amt})


def _check_top(frame: Any, req: dict[str, Any]) -> None:
    if not isinstance(frame, dict):
        raise TraceSimError("trace is not a call frame")
    if _a(frame.get("from"), "trace from") != req["from"] or _a(frame.get("to"), "trace to") != req["to"]:
        raise TraceSimError("trace is not of the requested call (from/to)")
    if "input" not in frame or _hexdata(frame.get("input")) != req["data"]:
        raise TraceSimError("trace is not of the requested call (input)")
    if not frame.get("error") and _qty(frame.get("gasUsed"), "gasUsed") <= 0:
        raise TraceSimError("trace reports no gasUsed")
    if _qty(frame.get("value"), "trace value") != int(req["value"], 16):
        raise TraceSimError("trace is not of the requested call (value)")


def merge_diff(overrides: dict[str, dict[str, Any]], diff: Any) -> None:
    """Fold a prestateTracer diffMode answer into `overrides` (in place); raises on a shape it cannot carry (deleted
    account, malformed)."""
    if not isinstance(diff, dict) or not isinstance(diff.get("pre"), dict) or not isinstance(diff.get("post"), dict):
        raise TraceSimError("prestateTracer diffMode returned no pre/post")
    pre = {str(k).lower(): v for k, v in diff["pre"].items()}
    post = {str(k).lower(): v for k, v in diff["post"].items()}
    for addr in set(pre) | set(post):
        _a(addr, "diff account")
        if addr not in post:
            raise TraceSimError("an account is deleted in the simulation")
        p, q = post[addr] or {}, pre.get(addr) or {}
        if not isinstance(p, dict) or not isinstance(q, dict):
            raise TraceSimError("diff account is not an object")
        o = overrides.setdefault(addr, {})
        if "balance" in p:
            o["balance"] = hex(_qty(p["balance"], "post balance"))
        if "nonce" in p:
            o["nonce"] = hex(_qty(p["nonce"], "post nonce"))
        if "code" in p:
            o["code"] = _hexdata(p["code"])
        ps, qs = p.get("storage") or {}, q.get("storage") or {}
        if not isinstance(ps, dict) or not isinstance(qs, dict):
            raise TraceSimError("diff storage is not an object")
        sd = dict(o.get("stateDiff") or {})
        for slot in qs:
            if slot not in ps:
                sd[str(slot).lower()] = ZERO_WORD           # diffMode omits slots set to zero from post
        for slot, val in ps.items():
            sd[str(slot).lower()] = _hexdata(val)
        if sd:
            o["stateDiff"] = sd


def _user_native_change(diff: dict[str, Any], user: str) -> int:
    pre = {str(k).lower(): v for k, v in diff["pre"].items()}
    post = {str(k).lower(): v for k, v in diff["post"].items()}
    p, q = post.get(user) or {}, pre.get(user) or {}
    if "balance" not in p:
        return 0
    return _qty(p["balance"], "post balance") - _qty(q.get("balance"), "pre balance")


def simulate_trace(call: Call, chain_id: int, txs: Sequence[dict[str, Any]], *,
                   probed: set[int] | None = None) -> SimResult:
    """Normalize a debug_traceCall simulation of the exact call sequence to a SimResult (see module doc)."""
    def fail(msg: str) -> SimResult:
        return SimResult(ok=False, provider="A", error=f"debug_traceCall: {msg}"[:300])

    if not txs:
        return fail("nothing to simulate")
    try:
        reqs = [_call_obj(t) for t in txs]
    except (TraceSimError, KeyError, ValueError, TypeError) as e:
        return fail(f"bad request: {e}")
    sender = reqs[0]["from"]
    if probed is None or int(chain_id) not in probed:
        why = probe(call)
        if why:
            return fail(why)
        if probed is not None:
            probed.add(int(chain_id))
    reads: list[tuple[str, str]] = []
    for r in reqs:
        d = r["data"]
        if d.startswith(SEL_APPROVE) and len(d) == 10 + 128:
            pair = (r["to"], "0x" + d[10 + 24:10 + 64])
            if pair not in reads:
                reads.append(pair)
    overrides: dict[str, dict[str, Any]] = {}
    deltas: dict[str, int] = {}
    transfers: list = []
    approvals: list = []
    gas_per: list[int] = []
    try:
        for i, r in enumerate(reqs):
            carried = bool(overrides)
            frame = call("debug_traceCall", [r, "latest", _cfg("callTracer", overrides, {"withLog": True})])
            _check_top(frame, r)
            if frame.get("error"):
                reason = frame.get("revertReason") or frame.get("error")
                return SimResult(ok=False, provider="A", error=f"call {i} reverted: {str(reason)[:200]}",
                                 raw={"method": "debug_traceCall"})
            native: dict[str, int] = {}
            _walk(frame, chain_id=chain_id, user=sender, deltas=deltas, transfers=transfers, approvals=approvals,
                  native=native)
            gas = _qty(frame.get("gasUsed"), "gasUsed") + intrinsic_gas(r["data"])
            plain = {k: v for k, v in r.items() if k != "gas"}
            if not carried:
                est = call("eth_estimateGas", [plain, "latest"])
                gas = max(gas, _qty(est, "eth_estimateGas"))
            else:
                # the step after an approve: estimate on the carried state too (state overrides as the 3rd param)
                # when the node takes them; refunds can make the needed limit exceed gasUsed. It only ever RAISES
                # the reserve, so a node that refuses the overrides keeps the trace figure (still x1.1 + intrinsic).
                try:
                    gas = max(gas, _qty(call("eth_estimateGas", [plain, "latest", overrides]), "eth_estimateGas"))
                except Exception:  # noqa: BLE001
                    pass
            gas_per.append(gas)
            if i < len(reqs) - 1 or reads:
                diff = call("debug_traceCall", [r, "latest", _cfg("prestateTracer", overrides, {"diffMode": True})])
                merge_diff(overrides, diff)
                # every native move is a value frame; the only other change a client may book is the gas charge
                # (some price traced calls), bounded by this tx's own gas x maxFeePerGas
                gap = native.get(sender, 0) - _user_native_change(diff, sender)
                if not 0 <= gap <= gas * int(txs[i].get("maxFeePerGas", 0) or 0):
                    raise TraceSimError("native balance change disagrees between callTracer and prestateTracer")
        after: dict[str, int] = {}
        for token, spender in reads:
            q = {"from": sender, "to": token, "data": SEL_ALLOWANCE + sender[2:].rjust(64, "0")
                 + spender[2:].rjust(64, "0"), "value": "0x0"}
            frame = call("debug_traceCall", [q, "latest", _cfg("callTracer", overrides, {"withLog": True})])
            if not isinstance(frame, dict) or frame.get("error"):
                return fail("allowance read failed")
            out = str(frame.get("output", "")).lower()
            if not _HEX.match(out) or len(out) != 66:
                return fail("allowance read did not return one word")
            after[f"{token}|{spender}"] = int(out, 16)
    except TraceSimError as e:
        return fail(str(e))
    except Exception as e:  # noqa: BLE001 - transport / JSON-RPC errors: fail closed, never skip
        return fail(f"unavailable: {e}")
    return SimResult(ok=True, provider="A", balance_deltas={k: v for k, v in deltas.items() if v},
                     transfers=transfers, approvals=approvals, gas_used=sum(gas_per),
                     raw={"allowances_after": after, "gas_used_per_tx": gas_per, "method": "debug_traceCall"})
