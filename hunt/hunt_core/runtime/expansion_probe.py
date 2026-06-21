"""On-demand Expansion Engine probe — isolated from /signal and Verdict V2."""
from __future__ import annotations

from typing import Any

from hunt_core.market.client import HuntCcxtClient
from hunt_core.runtime.symbol_probe import normalize_symbol

_PROBE_TIMEOUT_S = 240.0


async def probe_symbol_expansion(
    symbol: str,
    *,
    stagger_ms: int = 250,
    client: HuntCcxtClient | None = None,
    record_signal: bool = False,
) -> dict[str, Any]:
    """Full deep tick + ``row["expansion"]`` for one symbol."""
    import asyncio

    sym = normalize_symbol(symbol)
    if not sym:
        return {"symbol": symbol, "error": "empty_symbol"}

    from hunt_core.domain.config import load_settings
    from hunt_core.market.factory import create_hunt_market_plane_from_settings
    from hunt_core.market.symbol_gate import is_allowed_for_analysis
    from hunt_core.runtime.deep_assembly import assemble_deep_tick

    settings = load_settings()
    owned_plane = None
    if client is None:
        owned_plane = await create_hunt_market_plane_from_settings(settings)
        client = owned_plane.client
    if not getattr(client, "_markets_loaded", False):
        await client.load_markets()
    if not is_allowed_for_analysis(sym, exchange=client.exchange):
        return {
            "symbol": sym,
            "error": "symbol_not_tradable",
            "detail": "delisted or not in Binance USD-M CCXT markets",
        }

    try:
        row = await asyncio.wait_for(
            assemble_deep_tick(sym, client, stagger_ms=stagger_ms),
            timeout=_PROBE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        return {"symbol": sym, "error": "probe_timeout", "timeout_s": _PROBE_TIMEOUT_S}
    finally:
        if owned_plane is not None:
            await owned_plane.close()

    if row.get("error"):
        return row

    row["_query_source"] = "expansion_probe"

    if record_signal:
        exp = row.get("expansion") if isinstance(row.get("expansion"), dict) else {}
        meta = exp.get("meta") if isinstance(exp.get("meta"), dict) else {}
        dominant = str(exp.get("dominant") or "neutral")
        quality = float(meta.get("expansion_quality") or 0.0)
        if dominant != "neutral" and quality >= 0.45:
            try:
                from hunt_core.analysis.expansion_engine import build_expansion_opportunity
                from hunt_core.analysis.expansion_engine.learning import record_expansion_signal

                record_expansion_signal(
                    build_expansion_opportunity(row),
                    ts=str(row.get("ts") or ""),
                )
            except Exception:
                pass

    return row


__all__ = ["probe_symbol_expansion"]
