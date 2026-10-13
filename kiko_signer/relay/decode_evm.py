"""EVM calldata decoding (relay-exec; WP-R; doc 10 §3.4 B9, C10–C11). Strict, hand-rolled ABI word parsing.

Selectors (keccak-verified in tests/trading/foundation/test_fixtures.py):
  approve(address,uint256)                                  0x095ea7b3
  depositErc20(address,address,uint256,bytes32)             0xe8017952   (depositor, token, amount, id)
  depositNative(address,bytes32)                            0x49290c1c   (depositor, id); value = amount
  depositErc20(address,address,bytes32)  3-arg              0x5a1ee3ac   REJECTED (DEPOSIT_3ARG)
  erc20Router swap                                          0xcd6e13f7   (opaque multicall; bound by simulation)
  approvalProxy swap                                        0xf9e4bab4   (opaque; bound by simulation)
Calldata must be exactly selector + N words (no trailing bytes) for approve/deposit, address words must have zero
padding (non-canonical encodings are rejected, not normalized). Router/proxy calldata is not decoded for recipients
(impossible in general, doc 10 §3.3) — simulation covers it.
Every failure raises SignerReject with a VerifyCode; nothing is "fixed up".
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..errors import SignerReject, VerifyCode

SEL_APPROVE = "0x095ea7b3"
SEL_DEPOSIT_ERC20 = "0xe8017952"
SEL_DEPOSIT_NATIVE = "0x49290c1c"
SEL_DEPOSIT_ERC20_3ARG = "0x5a1ee3ac"
SEL_ROUTER_SWAP = "0xcd6e13f7"
SEL_PROXY_SWAP = "0xf9e4bab4"

_HEX = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
_ADDR = re.compile(r"^0x[0-9a-f]{40}$")
MAX_CALLDATA_BYTES = 64 * 1024        # Relay router calldata observed < 10 KB; anything huge is refused


@dataclass(frozen=True)
class Approve:
    spender: str
    amount: int


@dataclass(frozen=True)
class DepositErc20:
    depositor: str
    token: str
    amount: int
    order_id: str      # bytes32 hex 0x…


@dataclass(frozen=True)
class DepositNative:
    depositor: str
    order_id: str


def norm_hex(data: str) -> str:
    """Lowercase 0x-hex with an even number of digits, else UNKNOWN_SELECTOR (malformed calldata)."""
    if not isinstance(data, str) or not _HEX.match(data):
        raise SignerReject(VerifyCode.UNKNOWN_SELECTOR, "calldata is not 0x-prefixed even-length hex")
    if (len(data) - 2) // 2 > MAX_CALLDATA_BYTES:
        raise SignerReject(VerifyCode.UNKNOWN_SELECTOR, "calldata too large")
    return data.lower()


def norm_address(a: object, code: VerifyCode = VerifyCode.FROM_MISMATCH) -> str:
    if not isinstance(a, str):
        raise SignerReject(code, "address is not a string")
    a = a.lower()
    if not _ADDR.match(a):
        raise SignerReject(code, f"malformed address {a[:12]}…")
    return a


def selector(data: str) -> str:
    d = norm_hex(data)
    if len(d) < 10:
        raise SignerReject(VerifyCode.UNKNOWN_SELECTOR, "calldata shorter than a selector")
    return d[:10]


def _words(data: str, sel: str, n: int) -> list[str]:
    d = norm_hex(data)
    s = selector(d)
    if s == SEL_DEPOSIT_ERC20_3ARG:
        raise SignerReject(VerifyCode.DEPOSIT_3ARG, "3-arg depositErc20 is not allowed")
    if s != sel:
        raise SignerReject(VerifyCode.UNKNOWN_SELECTOR, f"selector {s} != {sel}")
    body = d[10:]
    if len(body) != 64 * n:
        raise SignerReject(VerifyCode.UNKNOWN_SELECTOR, f"expected exactly {n} ABI words, got {len(body) / 64:g}")
    return [body[i * 64:(i + 1) * 64] for i in range(n)]


def _addr_word(w: str) -> str:
    if w[:24] != "0" * 24:
        raise SignerReject(VerifyCode.UNKNOWN_SELECTOR, "address word with dirty high bytes")
    return "0x" + w[24:]


def decode_approve(data: str) -> Approve:
    spender, amount = _words(data, SEL_APPROVE, 2)
    return Approve(_addr_word(spender), int(amount, 16))


def decode_deposit_erc20(data: str) -> DepositErc20:
    depositor, token, amount, oid = _words(data, SEL_DEPOSIT_ERC20, 4)
    return DepositErc20(_addr_word(depositor), _addr_word(token), int(amount, 16), "0x" + oid)


def decode_deposit_native(data: str) -> DepositNative:
    depositor, oid = _words(data, SEL_DEPOSIT_NATIVE, 2)
    return DepositNative(_addr_word(depositor), "0x" + oid)
