"""Monad 143 (P0+, last: simulation-provider risk). Native MON (policy `<other chains>` cap). Cash = USDC 0x7547…b603
(6 dp). relayReceiver differs (not pinned, no effect). multicall3 EMPTY -> per-token eth_call balance reads.
Gas charged on the gas LIMIT [verify]: the signer sets `gas` = own simulation × 1.1 (never Relay's padded value) and
the cost model prices the limit (WP-F e). GoPlus coverage [verify]; otherwise the $25 unverified cap applies.
Observed 2026-10-03 (12 live quotes): approve 131,464 (above the old 120k placeholder); swap 669k-1.28M;
deposit 37.7k / 165.8k; maxFee ~113 gwei, prio 3 gwei. Ceilings below bound the SIMULATION-derived gas."""
from decimal import Decimal

from . import ChainExecConfig

CONFIG = ChainExecConfig(
    chain_id=143, key="monad", vm="evm", milestone="P0+",
    gas_ceiling={"approve": 200_000, "swap": 1_950_000, "deposit": 250_000},
    observed={"approve": (131_464, 131_464), "swap": (668_553, 1_277_281), "deposit": (37_701, 165_804)},
    gas_from_simulation=True,
    # rpc.monad.xyz refuses eth_simulateV1 with validation=false (2026-10-13); debug_traceCall callTracer works there
    # (Alchemy's Monad answers HTTP 400: the "a" URL must be one that serves debug_traceCall)
    simulation="debug_traceCall",
    drip_floor_usd=Decimal("0.05"), drip_refill_below_usd=Decimal("0.02"), drip_ceiling_usd=Decimal("0.25"),
    float_p0_usd=Decimal("5"), float_stage1_usd=Decimal("15"),
    has_multicall3=False,
    token_risk_sources=("dexscreener",),
    receipt_timeout_s=30.0,
    extra_items=("no_multicall3_per_call_reads", "gas_limit_billing_sim_x1_1", "simulation_providers_A_B",
                 "gas_wallet_mon_5_to_15", "usdc_cash_6dp", "goplus_monad_or_25_cap", "smoke_as_base"),
)
