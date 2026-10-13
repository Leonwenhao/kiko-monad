"""kiko_wire: the neutral, stdlib-only contract shared by the brain (`sato/`) and the signer (`kiko_signer/`).

Owner: integrator. Packages read it, never edit it (propose changes in BUILD_PLAN.md "seam requests").

What lives here (and nothing else):
  chains.py      display/neutral chain registry (ids, names, cash tokens, decimals, explorers). NOT security pins:
                 pinned Relay contracts live only in kiko_signer (and separately in kiko_cosigner).
  money.py       Decimal <-> base-unit conversion with per-token decimals (BNB USDC is 18 dp, never assume 6).
  flags.py       capability-flag names and defaults (doc 10 §5).
  replies.py     reply words both sides must read identically (AFFIRMATIVE: the yes-words that pick a lone match).
  reject_text.py the refusal words both sides show (the signer's PLAIN_TEXT and the brain's fallback are this table).
  signer_api.py  the brain <-> signer wire (doc 10 §2.4): request/response dataclasses + the SignerApi Protocol.

Rules: stdlib only; no imports from sato, kiko_signer or kiko_cosigner. kiko_cosigner must not import this package
(the co-signer shares no code with the signer, doc 10 §2.1).
"""
