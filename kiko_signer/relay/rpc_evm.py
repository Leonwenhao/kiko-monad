"""EvmRpc over JSON-RPC (relay-exec; WP-R). Implements ports.EvmRpc; fake: tests/fakes/evm_rpc.py.

URLs per chain id: {chain_id: {"a": url, "protected": url?}} built from the `rpc_urls.json` credential by
`urls_from_credential` (keys are kiko chain keys: "robinhood", "base", …). URLs carry provider keys, so this object
never logs or reprs them. https only.

Simulation: `eth_simulateV1` (traceTransfers=true, validation=false) of the exact call sequence in one block, plus
trailing `allowance(owner, spender)` reads for every approve in the sequence, normalized to SimResult:
  balance_deltas  from ERC-20 Transfer logs and the traceTransfers native pseudo-logs (0xeeee…eeee),
  transfers / approvals from the logs, raw["allowances_after"], raw["gas_used_per_tx"].
A provider without eth_simulateV1 yields SimResult(ok=False) and the step is refused (SIM_FAILED).
The method is chosen PER CHAIN by explicit config (`ChainExecConfig.simulation`, relay/chains): "eth_simulateV1" (above)
or "debug_traceCall" (relay/sim_trace.py: callTracer + prestateTracer diffMode, same SimResult, Monad 2026-10-13).
A chain with no configured method, or any other value, is refused: simulation is never skipped.
Balance reads: per call (`erc20_balance`), or `erc20_balances` as one JSON-RPC batch (works on Monad too, no
multicall3 needed).
"""
from __future__ import annotations

import itertools
import json
import re
from typing import Any, Sequence
from urllib.parse import urlsplit

import httpx
from eth_hash.auto import keccak

from kiko_wire import chains as wire_chains

from ..types import SimResult
from .sim_trace import simulate_trace

SEL_BALANCE_OF = "0x70a08231"
SEL_ALLOWANCE = "0xdd62ed3e"
SEL_DECIMALS = "0x313ce567"
SEL_APPROVE = "0x095ea7b3"
TOPIC_TRANSFER = "0x" + keccak(b"Transfer(address,address,uint256)").hex()
TOPIC_APPROVAL = "0x" + keccak(b"Approval(address,address,uint256)").hex()
NATIVE_PSEUDO = "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HEX = re.compile(r"^0x[0-9a-fA-F]*$")


class EvmRpcError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


def urls_from_credential(raw: bytes | str) -> dict[int, dict[str, str]]:
    """rpc_urls.json -> {chain_id: {"a": …, "protected": …}} for the EVM chains kiko knows (unknown keys ignored)."""
    data = json.loads(raw)
    out: dict[int, dict[str, str]] = {}
    for key, v in data.items():
        c = wire_chains.BY_KEY.get(key)
        if c is None or c.vm != "evm" or not isinstance(v, dict):
            continue
        out[c.relay_id] = {k: u for k, u in v.items() if k in ("a", "b", "protected") and isinstance(u, str)}
    return out


def _addr(a: str) -> str:
    if not isinstance(a, str) or not _ADDR.match(a):
        raise EvmRpcError("bad address")
    return a.lower()


def _word(a: str) -> str:
    return _addr(a)[2:].rjust(64, "0")


def _q(n: int) -> str:
    return hex(int(n))


def _int(v: Any) -> int:
    if isinstance(v, int) and not isinstance(v, bool):
        return v
    if isinstance(v, str) and _HEX.match(v):
        return int(v, 16) if len(v) > 2 else 0
    raise EvmRpcError(f"bad quantity {v!r}")


def _u256_result(v: Any) -> int:
    if not isinstance(v, str) or not _HEX.match(v) or len(v) != 66:
        raise EvmRpcError("eth_call did not return exactly one word")
    return int(v, 16)


class JsonRpcEvm:
    def __init__(self, urls: dict[int, dict[str, str]], http=None, timeout_s: float = 8.0, *,
                 protected_chains: frozenset[int] | None = None, allow_insecure: bool = False,
                 sim_methods: dict[int, str] | None = None):
        for cid, u in urls.items():
            for url in u.values():
                parts = urlsplit(url)
                if parts.scheme != "https" and not (allow_insecure and parts.scheme == "http"):
                    raise ValueError(f"RPC URL for chain {cid} must be https")
        self._urls = {int(k): dict(v) for k, v in urls.items()}
        self._http = http or httpx.Client(timeout=timeout_s, follow_redirects=False)
        self._timeout = timeout_s
        self._ids = itertools.count(1)
        if protected_chains is None:
            from .chains import registry
            protected_chains = frozenset(c.chain_id for c in registry().values() if c.protected_send)
        self._protected = protected_chains
        if sim_methods is None:
            from .chains import registry
            sim_methods = {c.chain_id: c.simulation for c in registry().values() if c.vm == "evm"}
        self._sim_methods = {int(k): str(v) for k, v in sim_methods.items()}
        self._trace_probed: set[int] = set()          # chains whose debug_traceCall capability probe passed

    def __repr__(self) -> str:
        return f"JsonRpcEvm(chains={sorted(self._urls)})"

    def chain_ids(self) -> frozenset[int]:
        """Chains with a configured RPC URL (the balance reader only checks these, NEW-10)."""
        return frozenset(self._urls)

    # ------------------------------------------------------------------ transport
    def _url(self, chain_id: int, which: str = "a") -> str:
        try:
            return self._urls[int(chain_id)][which]
        except KeyError:
            raise EvmRpcError(f"no RPC url for chain {chain_id} ({which})") from None

    def _post(self, url: str, payload: Any) -> Any:
        try:
            r = self._http.post(url, content=json.dumps(payload, separators=(",", ":")),
                                headers={"content-type": "application/json"}, timeout=self._timeout,
                                follow_redirects=False)
        except httpx.HTTPError as e:
            raise EvmRpcError(f"rpc transport error: {type(e).__name__}") from None
        if len(r.content) > MAX_RESPONSE_BYTES:
            raise EvmRpcError("rpc response too large")
        if r.status_code != 200:
            raise EvmRpcError(f"rpc http {r.status_code}")
        try:
            return json.loads(r.content)
        except ValueError:
            raise EvmRpcError("rpc returned non-JSON") from None

    def _call(self, chain_id: int, method: str, params: list[Any], *, which: str = "a") -> Any:
        rid = next(self._ids)
        data = self._post(self._url(chain_id, which), {"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        if not isinstance(data, dict) or data.get("id") != rid:
            raise EvmRpcError("rpc response id mismatch")
        if data.get("error"):
            err = data["error"] if isinstance(data["error"], dict) else {}
            raise EvmRpcError(str(err.get("message", "rpc error"))[:300], err.get("code"))
        return data.get("result")

    def _eth_call(self, chain_id: int, to: str, data: str) -> str:
        return self._call(chain_id, "eth_call", [{"to": _addr(to), "data": data}, "latest"])

    # ------------------------------------------------------------------ EvmRpc
    def eth_chain_id(self, chain_id: int) -> int:
        """What the configured URL for `chain_id` says it serves (a URL pasted under the wrong key answers differently)."""
        return _int(self._call(chain_id, "eth_chainId", []))

    def get_balance(self, chain_id: int, address: str) -> int:
        return _int(self._call(chain_id, "eth_getBalance", [_addr(address), "latest"]))

    def erc20_balance(self, chain_id: int, token: str, owner: str) -> int:
        return _u256_result(self._eth_call(chain_id, token, SEL_BALANCE_OF + _word(owner)))

    def erc20_balances(self, chain_id: int, tokens: Sequence[str], owner: str) -> dict[str, int]:
        """One JSON-RPC batch of balanceOf calls (no multicall3; fine on Monad)."""
        if not tokens:
            return {}
        start = next(self._ids)
        ids = list(range(start, start + len(tokens)))
        for _ in tokens[1:]:
            next(self._ids)
        batch = [{"jsonrpc": "2.0", "id": i, "method": "eth_call",
                  "params": [{"to": _addr(t), "data": SEL_BALANCE_OF + _word(owner)}, "latest"]}
                 for i, t in zip(ids, tokens)]
        data = self._post(self._url(chain_id), batch)
        if not isinstance(data, list):
            raise EvmRpcError("batch response is not a list")
        by_id = {d.get("id"): d for d in data if isinstance(d, dict)}
        out: dict[str, int] = {}
        for i, t in zip(ids, tokens):
            d = by_id.get(i)
            if d is None or d.get("error"):
                raise EvmRpcError(f"balanceOf failed for {t}")
            out[t.lower()] = _u256_result(d.get("result"))
        return out

    def erc20_allowance(self, chain_id: int, token: str, owner: str, spender: str) -> int:
        return _u256_result(self._eth_call(chain_id, token, SEL_ALLOWANCE + _word(owner) + _word(spender)))

    def erc20_decimals(self, chain_id: int, token: str) -> int:
        d = _u256_result(self._eth_call(chain_id, token, SEL_DECIMALS))
        if not 0 <= d <= 36:
            raise EvmRpcError("implausible decimals")
        return d

    def estimate_gas(self, chain_id: int, *, from_: str, to: str, data: str = "0x", value: int = 0) -> int:
        return _int(self._call(chain_id, "eth_estimateGas", [{"from": _addr(from_), "to": _addr(to), "data": data,
                                                              "value": hex(int(value))}]))

    def get_transaction_count(self, chain_id: int, address: str) -> int:
        return _int(self._call(chain_id, "eth_getTransactionCount", [_addr(address), "pending"]))

    def fee_estimate(self, chain_id: int) -> dict[str, int]:
        blk = self._call(chain_id, "eth_getBlockByNumber", ["latest", False])
        if not isinstance(blk, dict) or "baseFeePerGas" not in blk:
            raise EvmRpcError("latest block has no baseFeePerGas (not an EIP-1559 chain?)")
        base = _int(blk["baseFeePerGas"])
        try:
            prio = _int(self._call(chain_id, "eth_maxPriorityFeePerGas", []))
        except EvmRpcError:
            prio = 0
        return {"base_fee": base, "max_priority_fee": prio}

    def send_raw_transaction(self, chain_id: int, raw_tx: str) -> str:
        which = "protected" if int(chain_id) in self._protected and "protected" in self._urls.get(int(chain_id), {}) \
            else "a"
        # Turnkey's signedTransaction comes back WITHOUT 0x (live 2026-10-11: the RH gas drip was refused with
        # "cannot unmarshal hex string without 0x prefix"); eth_sendRawTransaction requires it.
        raw_tx = "0x" + raw_tx.removeprefix("0x").removeprefix("0X")
        local = "0x" + keccak(bytes.fromhex(raw_tx[2:])).hex()
        try:
            got = self._call(chain_id, "eth_sendRawTransaction", [raw_tx], which=which)
        except EvmRpcError as e:
            if "already known" in str(e).lower() or "known transaction" in str(e).lower():
                return local
            raise
        if not isinstance(got, str) or got.lower() != local:
            raise EvmRpcError("provider returned a different tx hash")
        return local

    def get_receipt(self, chain_id: int, tx_hash: str) -> dict[str, Any] | None:
        r = self._call(chain_id, "eth_getTransactionReceipt", [tx_hash])
        if r is None:
            return None
        if not isinstance(r, dict):
            raise EvmRpcError("bad receipt")
        out = dict(r)
        out["status"] = _int(r.get("status", "0x0"))
        if "gasUsed" in r:
            out["gasUsed"] = _int(r["gasUsed"])
        if "effectiveGasPrice" in r:
            out["effectiveGasPrice"] = _int(r["effectiveGasPrice"])
        return out

    # ------------------------------------------------------------------ simulation
    def simulate(self, chain_id: int, txs: Sequence[dict[str, Any]]) -> SimResult:
        if not txs:
            return SimResult(ok=False, provider="A", error="nothing to simulate")
        method = self._sim_methods.get(int(chain_id))
        if method == "debug_traceCall":
            return simulate_trace(lambda m, p: self._call(chain_id, m, p), int(chain_id), txs,
                                  probed=self._trace_probed)
        if method != "eth_simulateV1":
            return SimResult(ok=False, provider="A", error=f"no simulation method configured for chain {chain_id}")
        sender = _addr(txs[0]["from"])
        calls = [{"from": _addr(t["from"]), "to": _addr(t["to"]), "data": t.get("data", "0x"),
                  "value": _q(int(t.get("value", 0))), "gas": _q(int(t.get("gas", 0)))} for t in txs]
        reads: list[tuple[str, str]] = []
        for t in txs:
            d = str(t.get("data", "")).lower()
            if d.startswith(SEL_APPROVE) and len(d) == 10 + 128:
                token, spender = _addr(t["to"]), "0x" + d[10 + 24:10 + 64]
                if (token, spender) not in reads:
                    reads.append((token, spender))
        for token, spender in reads:
            calls.append({"from": sender, "to": token, "data": SEL_ALLOWANCE + _word(sender) + _word(spender)})
        params = [{"blockStateCalls": [{"calls": calls}], "traceTransfers": True, "validation": False}, "latest"]
        try:
            res = self._call(chain_id, "eth_simulateV1", params)
        except EvmRpcError as e:
            return SimResult(ok=False, provider="A", error=f"eth_simulateV1 unavailable: {e}"[:300])
        return parse_simulate_v1(res, chain_id=chain_id, n_txs=len(txs), reads=reads)


def parse_simulate_v1(res: Any, *, chain_id: int, n_txs: int, reads: list[tuple[str, str]]) -> SimResult:
    if not isinstance(res, list) or len(res) != 1 or not isinstance(res[0], dict):
        return SimResult(ok=False, provider="A", error="unexpected eth_simulateV1 result")
    results = res[0].get("calls")
    if not isinstance(results, list) or len(results) != n_txs + len(reads):
        return SimResult(ok=False, provider="A", error="eth_simulateV1 call count mismatch")
    deltas: dict[str, int] = {}
    transfers, approvals, gas_per = [], [], []
    for i, c in enumerate(results[:n_txs]):
        if _int(c.get("status", "0x0")) != 1:
            return SimResult(ok=False, provider="A", error=f"call {i} reverted", raw={"result": res})
        gas_per.append(_int(c.get("gasUsed", "0x0")))
        for log in c.get("logs") or []:
            topics = [str(t).lower() for t in log.get("topics") or []]
            if len(topics) != 3:
                continue
            token = str(log.get("address", "")).lower()
            frm, to = "0x" + topics[1][-40:], "0x" + topics[2][-40:]
            amt = int(str(log.get("data", "0x0")), 16) if log.get("data") not in (None, "0x") else 0
            if topics[0] == TOPIC_TRANSFER:
                tok = wire_chains.EVM_NATIVE if token == NATIVE_PSEUDO else token
                transfers.append({"token": tok, "from": frm, "to": to, "amount": amt})
                deltas[f"{frm}|{chain_id}:{tok}"] = deltas.get(f"{frm}|{chain_id}:{tok}", 0) - amt
                deltas[f"{to}|{chain_id}:{tok}"] = deltas.get(f"{to}|{chain_id}:{tok}", 0) + amt
            elif topics[0] == TOPIC_APPROVAL:
                approvals.append({"token": token, "owner": frm, "spender": to, "amount": amt})
    after = {}
    for (token, spender), c in zip(reads, results[n_txs:]):
        if _int(c.get("status", "0x0")) != 1:
            return SimResult(ok=False, provider="A", error="allowance read failed")
        after[f"{token}|{spender}"] = _u256_result(c.get("returnData"))
    return SimResult(ok=True, provider="A", balance_deltas={k: v for k, v in deltas.items() if v},
                     transfers=transfers, approvals=approvals, gas_used=sum(gas_per),
                     raw={"allowances_after": after, "gas_used_per_tx": gas_per})


