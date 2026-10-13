"""Slippage classes and caps: ONE table for the card (kiko_signer.cards via kiko_signer.profile), the quote builders
(relay.quote_builder, jupiter.client) and the co-signer's min_out tolerance.

Classes: a card is "stable" (cash -> cash), "major" (a verified major), "other" or "new" (< 24 h). SLIPPAGE_BPS is
the cap every profile starts from (doc 10 §2.5; the retail profile binds exactly these). Under the native profile
(kiko_signer.profile, founder 2026-10-10) "other" and "new" are sized to the pool and the trade, 3% .. 15%
(NATIVE_SLIPPAGE_*). CLASS_MAX_BPS is the widest cap a card of each class can bind under ANY profile: what the
co-signer's min_out tolerance must cover. The co-signer shares no code with the signer (kiko_cosigner/__init__.py), so
kiko_cosigner.config carries its own copy; tests/security/test_slippage_parity.py pins the two equal (legacy audit
P0-7, 2026-10-11: the co-signer table sat at the retail values, so every native 1500 bps "other" / "new" card would
have failed its tolerance once the co-signer went live).

The min-out floor arithmetic lives here too (legacy audit P1-5): the card's min_out (cards.net_of_fee over Relay's
minimum, relay.indicative.min_out_after_fees over an indicative quote), the verifiers' acceptance of a Jupiter route
(relay.verifier, jupiter.verify) and the executor's route_min check. One rounding rule: every floor is integer floor
division, never rounded up, so the card never promises one base unit more than the chain can deliver, and fee then
slippage are applied in ONE product (floor_after_fee_and_slippage), not two successive floors (those can differ by
one unit). A Jupiter route floors quoted_out x (1 - slip) while Relay's minimumAmount (-> the card's min_out) rounds
up, so live quotes sit exactly ONE base unit apart: route_covers_floor tolerates that one unit (ROUTE_FLOOR_TOLERANCE)
and nothing more. The co-signer's own copy of the route check sits in kiko_cosigner.approve / .jupiter (no shared code).
"""
from __future__ import annotations

BPS = 10_000

SLIPPAGE_BPS: dict[str, int] = {"stable": 30, "major": 150, "other": 300, "new": 1000}
# major was 50 until 2026-10-12: live, a $9 USDC -> SOL buy routed through a prop AMM simulated ~0.7% under its own
# quote, so every "$10 of sol" died "price moved" (Leon: small slippage is fine, just execute). The cash legs that used
# the major number (home sweeps, withdraw quotes, a two-leg buy's leg 1) keep their 0.5%: CASH_LEG_SLIPPAGE_BPS.
CASH_LEG_SLIPPAGE_BPS = 50

NATIVE_SLIPPAGE_DEEP_BPS = 300           # an established token, a trade that barely moves its pool
NATIVE_SLIPPAGE_MOVE_BPS = 500           # the trade moves the pool 2-5% (estimated)
NATIVE_SLIPPAGE_THIN_BPS = 1000          # thin (< $25k), under a day old, or the trade moves the pool > 5%
NATIVE_SLIPPAGE_MAX_BPS = 1500           # very thin (< cfg.min_liquidity_usd) or the trade moves the pool > 15%
MAX_SLIPPAGE_BPS = NATIVE_SLIPPAGE_MAX_BPS   # the hard cap every quote builder enforces (relay / jupiter clients)

# stable / major never widen; other / new go up to the native maximum when the pool is very thin or the trade moves it
CLASS_MAX_BPS: dict[str, int] = {"stable": SLIPPAGE_BPS["stable"], "major": SLIPPAGE_BPS["major"],
                                 "other": NATIVE_SLIPPAGE_MAX_BPS, "new": NATIVE_SLIPPAGE_MAX_BPS}

ROUTE_FLOOR_TOLERANCE = 1                # base units a route's own minimum may sit under the card's min_out


def _amount(v) -> int:
    if isinstance(v, bool) or int(v) < 0:
        raise ValueError("bad amount")
    return int(v)


def _bps(v, what: str) -> int:
    if isinstance(v, bool) or not 0 <= int(v) < BPS:
        raise ValueError(f"bad {what} {v!r}")
    return int(v)


def after_fee(amount: int, fee_bps: int) -> int:
    """`amount` net of a bps fee taken from it (kiko's app fee comes out of the output on every route)."""
    return _amount(amount) * (BPS - _bps(fee_bps, "fee_bps")) // BPS


def floor_after_slippage(amount: int, slippage_bps: int) -> int:
    """The least `amount` can become after `slippage_bps` of slippage (a route's own on-chain minimum)."""
    return _amount(amount) * (BPS - _bps(slippage_bps, "slippage_bps")) // BPS


def floor_after_fee_and_slippage(amount: int, slippage_bps: int, fee_bps: int = 0) -> int:
    """The card's min_out from an indicative quote: fee and slippage in one product, one floor."""
    return (_amount(amount) * (BPS - _bps(fee_bps, "fee_bps")) * (BPS - _bps(slippage_bps, "slippage_bps"))
            // (BPS * BPS))


def gross_from_floor(min_out: int, slippage_bps: int) -> int:
    """The output a card promised before slippage, back from its min_out (the "price moved X%" base)."""
    return int(min_out) * BPS // max(1, BPS - int(slippage_bps))


def route_covers_floor(quoted_out: int, slippage_bps: int, min_out: int) -> bool:
    """Whether a route whose on-chain minimum is quoted_out x (1 - slippage) delivers the card's min_out, within the
    one-unit rounding tolerance. A slippage outside [0, 100%] never covers anything."""
    if int(slippage_bps) < 0 or int(slippage_bps) > BPS:
        return False
    return int(quoted_out) * (BPS - int(slippage_bps)) // BPS + ROUTE_FLOOR_TOLERANCE >= int(min_out)
