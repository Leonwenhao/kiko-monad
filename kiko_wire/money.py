"""Exact money math. Decimal only, never float, decimals always per token (doc 10 §3.10: BNB USDC is 18 dp).

On the wire, USD amounts and base-unit amounts are strings (`"20.00"`, `"20000000"`).
"""
from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation

USD_Q = Decimal("0.01")


class MoneyError(ValueError):
    pass


def dec(x: str | int | Decimal) -> Decimal:
    """Parse a wire amount. Floats are refused on purpose (precision bugs move money)."""
    if isinstance(x, float):
        raise MoneyError("floats are not accepted for money; pass str or Decimal")
    try:
        d = Decimal(x)
    except (InvalidOperation, TypeError) as e:
        raise MoneyError(f"bad amount {x!r}") from e
    if not d.is_finite():
        raise MoneyError(f"bad amount {x!r}")
    return d


def to_base(amount: str | Decimal, decimals: int) -> int:
    """Human units -> integer base units, rounding DOWN (never spend more than asked)."""
    if not 0 <= decimals <= 36:
        raise MoneyError(f"bad decimals {decimals}")
    d = dec(amount)
    if d < 0:
        raise MoneyError("negative amount")
    return int((d * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_DOWN))


def from_base(base: int | str, decimals: int) -> Decimal:
    if not 0 <= decimals <= 36:
        raise MoneyError(f"bad decimals {decimals}")
    return Decimal(int(base)) / (Decimal(10) ** decimals)


def usd(x: str | Decimal) -> str:
    """Format a USD amount for the wire (2 dp, half-even)."""
    return str(dec(x).quantize(USD_Q))


def fmt_liquidity(v: Decimal | str | int) -> str:
    """A token's liquidity the way kiko says it, the SAME on the brain's token preview and the signer's card (v0.9.2,
    live 2026-10-09: the preview said $628.9k and the card $308k for CRAWL): "$1.2M", "$628k", "$950.00", always
    rounded down (never more liquidity than there is)."""
    d = Decimal(str(v))
    if d >= 1_000_000:
        return f"${(d / 1_000_000).quantize(Decimal('0.1'), rounding=ROUND_DOWN)}M"
    if d >= 1_000:
        return f"${(d / 1_000).quantize(Decimal(1), rounding=ROUND_DOWN)}k"
    return "$" + f"{d.quantize(USD_Q, rounding=ROUND_DOWN):,}"


def ceil_bps(numerator: Decimal, denominator: Decimal) -> int:
    """ceil(numerator / denominator * 10_000), the cost-recovery bps rule in doc 10 §2.6."""
    if denominator <= 0:
        raise MoneyError("denominator must be positive")
    return int((numerator / denominator * Decimal(10_000)).to_integral_value(rounding=ROUND_CEILING))
