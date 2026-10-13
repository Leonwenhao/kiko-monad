"""Refusal words (legacy audit P1-7, 2026-10-11): ONE table, written for the signer and shown verbatim by both sides.

The signer (kiko_signer/errors.PLAIN_TEXT) carries these on every RejectCode refusal whose raising site has nothing
more specific to say; the brain (sato/trade/cards.REASON_TEXT) falls back to the same line only when a refusal arrives
with no words. Keys are RejectCode values. Rules (Leon 2026-10-08): lowercase kiko voice, what happened and the one
next action, "nothing was sent" where money is involved, at most 280 characters (tests/test_copy_hygiene.py).
"""
from __future__ import annotations

CARD_GONE_TEXT = "that one timed out, so nothing was bought or sold. say it again, or just \"try again\"."
QUOTE_EXPIRED_TEXT = "that quote expired, so nothing was bought or sold. say it again and i'll get a fresh price."
REPLY_LOST_TEXT = "your reply never reached my side, so nothing was bought or sold. reply again?"
BUSY_TEXT = "i can't cover the transaction fee right now, so nothing was bought or sold. say \"try again\" in a bit."
PRICE_FAIL_TEXT = "couldn't get a price right now, so nothing was bought or sold. try again in a minute."
FROZEN_TEXT = "trading is frozen on this wallet, so nothing was sent. text leon."
PAUSED_TEXT = "trading is paused right now, so nothing was sent. say \"try again\" in a bit."
OFFER_GONE_TEXT = "that offer ran out. say the order again and i'll line it up fresh."
# phase0 WP5: a trade while a withdraw is still landing would race it for the same money (signer card + exec, brain)
WITHDRAW_IN_FLIGHT_TEXT = "your withdrawal is still landing. give it a minute, then say \"try again\"."
# grants.addresses could not re-check the wallet's permissions (a Turnkey hiccup): no link, the wallet is fine
GRANT_RECHECK_UNAVAILABLE_TEXT = ("i can't check your wallet permissions right now, so nothing was sent. try again "
                                  "in a minute.")
# phase0 WP12 (2026-10-12): "a safe fill" was jargon nobody could act on; what happened, and the one thing to send
SAFE_FILL_TEXT = "i couldn't place that one, so nothing was sent. say \"try again\"."
# exec-time token re-reads (kiko_signer/jupiter/route.py raises them; the exec stores "token_risk:rules_changed" /
# "token_risk:unreadable" and the brain's receipt shows the same words: sato/trade/track/receipts.py)
TOKEN_RULES_CHANGED_TEXT = ("that token's rules changed since the card, so nothing was sent. say \"try again\" for a "
                            "fresh card.")
TOKEN_UNREADABLE_TEXT = "i can't read that token right now, so nothing was sent. say \"try again\" in a minute."
# a coin whose only market is pump.fun (bonding curve / PumpSwap): kiko's payer can't sign those hops yet
# (kiko_signer/jupiter/pins.IN_ROUTE_PAYER_DEXES), so the card says so instead of "no market"
PUMP_ONLY_TEXT = "that coin only trades on pump.fun right now and i can't buy those yet. nothing was sent."
# prod is Solana-only (2026-10-12): an EVM chain is off for everyone, never "try another chain"
SOLANA_ONLY_TEXT = ("i only trade on solana right now, so nothing was sent. paste a solana contract or say \"buy $5 "
                    "of bonk\".")


def chains_off_text(chain_keys) -> str:
    """CHAIN_NOT_ENABLED built from the chains that ARE on (Monad, 2026-10-13): "i trade on solana and monad right
    now…". Solana alone (or nothing known) keeps SOLANA_ONLY_TEXT word for word. Unknown keys are dropped."""
    from kiko_wire.chains import BY_KEY, CHAINS
    order = [c.key for c in CHAINS.values()]
    keys = sorted({k for k in chain_keys if k in BY_KEY}, key=lambda k: (k != "solana", order.index(k)))
    if not keys or keys == ["solana"]:
        return SOLANA_ONLY_TEXT
    names = [BY_KEY[k].display for k in keys]
    on = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    paste = f"a {' or '.join(names)} contract" if len(names) <= 2 else "a contract from one of those"
    return f"i trade on {on} right now, so nothing was sent. paste {paste} or say \"buy $5 of bonk\"."


# the signer sends no words for a wrong code on purpose (the card is still open); this is the brain's line for it
CODE_MISMATCH_TEXT = "that code doesn't match the card, so it's still waiting. reply the code from the card."

REJECT_TEXT: dict[str, str] = {
    "inbound_unknown": REPLY_LOST_TEXT,
    "not_imessage": "trading only works over imessage, so nothing was sent.",
    "not_dm": "trading only works in a 1:1 chat with me, so nothing was sent.",
    "wrong_handle": "this number isn't linked to that wallet, so nothing was sent.",
    "multiple_pending": "you have more than one trade open. reply the code from the one you want.",
    "card_expired": CARD_GONE_TEXT,
    "reply_before_card": "that reply came before the card, so nothing was sent. reply again?",
    "reply_too_old": QUOTE_EXPIRED_TEXT,
    "already_confirmed": "already on it.",
    "paused": PAUSED_TEXT,
    "user_frozen": FROZEN_TEXT,
    "breaker": "i've hit my daily trading limit for everyone, so nothing was sent. try again tomorrow.",
    "float_low": BUSY_TEXT,
    "over_trade_cap": "that's over your per-trade limit, so nothing was sent. try a smaller amount.",
    "over_day_cap": "that's over your daily limit, so nothing was sent. try a smaller amount, or tomorrow.",
    "new_token_cap": "that token is less than a day old, so the max is smaller for now. nothing was sent.",
    "token_risk": "i can't get safe data on that token right now, so nothing was sent. try again in a minute.",
    "token_denied": "i can't trade that token, so nothing was sent.",
    "ambiguous": "which token? paste the contract to be sure. nothing was sent.",
    "impact_too_high": "that size would move the price too much, so nothing was sent. try a smaller amount.",
    "network_cost_too_high": "the fee would eat too much of a trade that small, so nothing was sent. try $10 or more.",
    "l1_below_min": "that's under the minimum for this token right now, so nothing was sent. try a bigger amount.",
    "insufficient_cash": "you don't have enough cash for that, so nothing was bought. add cash or try a smaller "
                         "amount.",
    "chain_not_enabled": SOLANA_ONLY_TEXT,
    "chain_not_granted": "your wallet hasn't okayed that chain for me yet, so nothing was sent. tap the link to "
                         "update permissions.",
    "grant_stale": "your wallet permissions changed, so nothing was sent. tap the link to re-authorize kiko.",
    "not_active": "set up your wallet first, then say the order again. nothing was sent.",
    "requote": PRICE_FAIL_TEXT,
    "fronting_cap": "i can't cover more transaction fees for your wallet today, so nothing was sent. try again "
                    "tomorrow.",
    "gas_policy": "i can't do that one right now, nothing changed. try again in a bit.",
    "cosigner_rejected": SAFE_FILL_TEXT,
    "policy_denied": SAFE_FILL_TEXT,
    "consensus_timeout": "that one is taking longer than usual. nothing was sent yet; i'll text you.",
    "rate_limited": "that's a lot of trades at once, so i didn't place this one. give me a minute and try again.",
    "passkey_recheck": "it's been a while since you last confirmed it's you, so nothing was sent. tap the link to "
                       "confirm, then say the order again.",
    "instant_off": "instant mode isn't available yet. nothing changed.",
    "instant_unparsed": "say \"instant $25\", \"instant off\" or \"instant on\". nothing changed.",
    "instant_over_max": "that's over the instant limit i can offer. nothing changed.",
    "offer_expired": OFFER_GONE_TEXT,
    "offer_mismatch": "that's not the trade i offered. say the order again?",
    "withdraw_in_flight": WITHDRAW_IN_FLIGHT_TEXT,
    "sweep_rule": "i didn't move that money, it's still where it was. nothing was sent.",
    "question": "sounds like a question, not an order, so nothing was sent. tell me what to do, like "
                "\"buy $20 of hood\".",
    "side_mismatch": "i couldn't read that as a trade, so nothing was sent. try \"buy $20 of hood\".",
    "amount_mismatch": "how much? say a dollar amount, like \"buy $5 of it\". nothing was sent.",
    "asset_not_in_text": "which token? say the name or paste the contract. nothing was sent.",
    "y_not_allowed": "this one needs the code from the card, not y. it's still waiting: reply the code.",
    # code_mismatch: deliberately no words from the signer (CODE_MISMATCH_TEXT is the brain's)
}
