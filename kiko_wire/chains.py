"""Neutral chain registry for the six FOMO spot chains (doc 10 §4.1, §4.5).

Values verified against live `GET https://api.relay.link/chains` on 2026-10-03 (fixture:
tests/fixtures/trading/relay/chains.json). This module is display/neutral data only. The SECURITY pins (router,
approvalProxy, depository, Solana programs) are NOT here: they live in kiko_signer's pinned snapshot, and the
co-signer keeps its own copy.

`relay_id` is the numeric id Relay uses in /quote/v2. `hub_key` is the string Relay uses inside
`protocol.v2.orderData` (`chainId: "robinhood"`), which the orderId recompute needs.
"""
from __future__ import annotations

from dataclasses import dataclass

EVM_NATIVE = "0x0000000000000000000000000000000000000000"
SOL_NATIVE = "11111111111111111111111111111111"


@dataclass(frozen=True)
class Token:
    chain_id: int
    address: str          # EVM: lowercase 0x hex; Solana: base58 mint; native: EVM_NATIVE / SOL_NATIVE
    symbol: str
    decimals: int


@dataclass(frozen=True)
class ChainInfo:
    relay_id: int
    key: str               # kiko's short key: "robinhood", "solana", "base", "bnb", "ethereum", "monad"
    hub_key: str           # protocol.v2 chainId string in orderData
    vm: str                # "evm" | "svm"
    display: str           # user-facing, lowercase ("robinhood chain")
    native: Token
    cash: Token            # the chain's "cash" stable (USDG on RH, USDC elsewhere)
    explorer: str          # https origin, used for the outbound URL allowlist
    explorer_tx_path: str  # appended to explorer + tx hash
    milestone: str         # "P0" | "P0+" (FOMO parity)
    eth_native: bool       # native token is ETH (policy P1 value-cap branch, doc 10 §3.3)
    has_multicall3: bool   # Monad: empty multicall3, balance reads must be per-call (doc 10 §4.5)

    def tx_url(self, tx_hash: str) -> str:
        return f"{self.explorer}{self.explorer_tx_path}{tx_hash}"


def _evm(relay_id, key, hub, display, native_sym, cash_sym, cash_addr, cash_dec, explorer, milestone,
         eth_native=True, multicall=True) -> ChainInfo:
    return ChainInfo(relay_id, key, hub, "evm", display, Token(relay_id, EVM_NATIVE, native_sym, 18),
                     Token(relay_id, cash_addr, cash_sym, cash_dec), explorer, "/tx/", milestone, eth_native, multicall)


ROBINHOOD = _evm(4663, "robinhood", "robinhood", "robinhood chain", "ETH", "USDG",
                 "0x5fc5360d0400a0fd4f2af552add042d716f1d168", 6, "https://robin.etherscan.io", "P0")
SOLANA = ChainInfo(792703809, "solana", "solana", "svm", "solana", Token(792703809, SOL_NATIVE, "SOL", 9),
                   Token(792703809, "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "USDC", 6),
                   "https://solscan.io", "/tx/", "P0", False, False)
BASE = _evm(8453, "base", "base", "base", "ETH", "USDC", "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", 6,
            "https://basescan.org", "P0+")
BNB = _evm(56, "bnb", "bnb", "bnb chain", "BNB", "USDC", "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d", 18,
           "https://bscscan.com", "P0+", eth_native=False)
ETHEREUM = _evm(1, "ethereum", "ethereum", "ethereum", "ETH", "USDC", "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48", 6,
                "https://etherscan.io", "P0+")
MONAD = _evm(143, "monad", "monad", "monad", "MON", "USDC", "0x754704bc059f8c67012fed69bc8a327a5aafb603", 6,
             "https://monadvision.com", "P0+", eth_native=False, multicall=False)

CHAINS: dict[int, ChainInfo] = {c.relay_id: c for c in (ROBINHOOD, SOLANA, BASE, BNB, ETHEREUM, MONAD)}
BY_KEY: dict[str, ChainInfo] = {c.key: c for c in CHAINS.values()}

P0_CHAIN_IDS: tuple[int, ...] = (ROBINHOOD.relay_id, SOLANA.relay_id)
PARITY_CHAIN_IDS: tuple[int, ...] = (BASE.relay_id, ETHEREUM.relay_id, BNB.relay_id, MONAD.relay_id)  # enable order

# Extra stables that count as cash where present (doc 10 §4.4: "Σ stables"); decimals per token, never assumed.
EXTRA_STABLES: tuple[Token, ...] = (
    Token(56, "0x55d398326f99059ff775485246999027b3197955", "USDT", 18),
    Token(8453, "0xfde4c96c8593536e31f229ea8f37b2ada2699bb2", "USDT", 6),
    Token(1, "0xdac17f958d2ee523a2206206994597c13d831ec7", "USDT", 6),
    Token(792703809, "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB", "USDT", 6),
)

# Chain words a user may type -> chain key (brain grammar and signer re-derivation both use their OWN parsers;
# this table is the shared vocabulary so they agree).
CHAIN_WORDS: dict[str, str] = {
    "robinhood": "robinhood", "robinhood chain": "robinhood", "rh": "robinhood", "hood chain": "robinhood",
    "solana": "solana", "sol": "solana",
    "base": "base",
    "bnb": "bnb", "bsc": "bnb", "bnb chain": "bnb", "binance": "bnb",
    "ethereum": "ethereum", "eth mainnet": "ethereum", "mainnet": "ethereum", "l1": "ethereum",
    "monad": "monad",
}


def chain(relay_id: int | str) -> ChainInfo:
    """Lookup by numeric relay id or by key. Raises KeyError for unknown chains (fail closed)."""
    if isinstance(relay_id, str) and not relay_id.isdigit():
        return BY_KEY[relay_id]
    return CHAINS[int(relay_id)]


def is_cash(chain_id: int, address: str) -> bool:
    c = CHAINS.get(int(chain_id))
    if c is None:
        return False
    addr = address if c.vm == "svm" else address.lower()
    return addr == c.cash.address or any(t.chain_id == c.relay_id and t.address == addr for t in EXTRA_STABLES)


def explorer_origins() -> frozenset[str]:
    """Outbound URL allowlist component (doc 10 §1): explorers of every registered chain."""
    return frozenset(c.explorer for c in CHAINS.values())
