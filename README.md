# kiko × Monad

**Text a number, own Monad coins.** kiko is an AI in iMessage that buys and sells crypto for you from a wallet you own.
Text **gmonad** to **+1 (912) 303-4689** to try it.

This repo is the Monad integration from kiko's private codebase, published for Monad Metropolis. It's an excerpt and
doesn't run on its own. The live product is the demo.

## What's here

Monad's public RPC (`rpc.monad.xyz`) rejects `eth_simulateV1` with `validation: false`, which is the dry-run kiko
uses on other EVM chains. Rather than sign blind, we built a simulator on `debug_traceCall`, which Monad does serve:

- **`kiko_signer/relay/sim_trace.py`**: simulates every step of a trade before it's signed.
  - `callTracer` with logs gives the transfers and approvals. Reverted subcalls are marked, and their logs don't count.
  - `prestateTracer` in diff mode gives the state changes, carried into the next step as `stateOverrides`, so a
    multi-step trade (approve, then swap) is checked as one sequence.
  - The same checks as the rest of kiko then run on the result:
    - the exact input amount;
    - the minimum output;
    - none of the user's other balances go down;
    - transfers go only to pinned contracts;
    - no approval is left behind.
  - **A capability probe** first proves the node really traces correctly: nested logs, reverted-subcall marking and
    diff mode. If the probe fails, kiko refuses to trade rather than trust a broken simulator.
- **`kiko_signer/relay/chains/monad.py`**: Monad's gas ceilings, which come from live observation. The gas limit
  is the simulation's result × 1.1, never the route's padded value, because Monad bills on the gas limit.
- **`kiko_signer/relay/evm_path.py`, `rpc_evm.py`**: build the unsigned type-2 transaction, cap its gas fields and
  broadcast it.
- **`kiko_wire/`**: the shared chain table, money math and slippage rules.
- **`tests/trading/relay/test_sim_trace.py`**: the simulator's tests. They run inside kiko's private test harness,
  and their helpers aren't published.

## How kiko works

```
iMessage ──► brain (AI, untrusted) ──► signer (separate process + unix user) ──► Turnkey wallet (user-owned)
                                         │  quote via Relay
                                         │  simulate every step (Monad: debug_traceCall)
                                         │  verify, enforce limits, then sign
                                         ▼
                                   Solana / Monad
```

- **The AI never touches keys.** The signer is the money boundary: it rebuilds every quote itself, simulates and
  verifies every step, and enforces per-user limits and kill switches.
- **Wallets are non-custodial.** Turnkey sub-org wallets with email-code roots, plus per-wallet policies enforced
  inside Turnkey.
- **One text, cross-chain.** "buy $5 of mon" pays from your Solana USDC and lands MON on Monad through Relay. No
  bridge, no second app.
- **kiko pays Monad gas** from its own treasury, so users never need MON for gas.
- **Nothing moves until you reply "y"** to a confirm card.
- **Tested:** 8,800+ automated tests. Every release is gated twice, on a laptop and on the server, with automatic
  rollback.

## License

AGPL-3.0. See `LICENSE`. © 2026 Leon Liu.
