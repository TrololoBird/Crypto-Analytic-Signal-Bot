"""Unified delivery decision — gate + tier + format (all Telegram paths)."""
from __future__ import annotations



import html
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

from hunt_core.shared.contract import validate_signal_contract
from hunt_core.shared.geometry import (
    DEFAULT_MIN_RR as _DEFAULT_MIN_RR,
    geometry_block_evidence,
    geometry_block_reason,
    resolve_min_rr as _resolve_min_rr,
    setup_risk_reward as _setup_rr,
)


@dataclass(frozen=True)
class GateResult:
    """Delivery decision result (the fusion `confirmed` flag is authoritative)."""

    ok: bool
    code: str = ""
    message: str = ""

# Cross-stage cooldown: squeeze → confirm within the window.
DELIVERY_STAGES: tuple[str, ...] = ("squeeze", "confirm")
ADVISORY_STAGES: tuple[str, ...] = ("squeeze",)
UNIFIED_COOLDOWN_MINUTES = 45


def _stage_rank(stage: str) -> int:
    return DELIVERY_STAGES.index(stage) if stage in DELIVERY_STAGES else 0


def _unified_key(symbol: str, direction: str, stage: str) -> str:
    return f"unified:{symbol.upper()}:{direction.lower()}:{stage}"


def unified_cooldown_ok(
    state: dict[str, str],
    *,
    symbol: str,
    direction: str,
    stage: str,
    now: datetime,
    minutes: int = UNIFIED_COOLDOWN_MINUTES,
) -> bool:
    """False when any advisory/confirm stage already shipped in-window.

    Advisory stages (early, dump_hunt, squeeze) block each other for the same
    symbol+direction. Confirm is allowed after advisory unless another confirm
    fired recently.
    """
    sym = symbol.upper()
    direc = direction.lower()
    try:
        from hunt_core.scanner.delivery.delivery_state import production_cooldown_ok

        if not production_cooldown_ok(state, symbol=sym, direction=direc, now=now, minutes=minutes):
            return False
    except Exception:
        pass
    if stage == "confirm":
        raw = state.get(_unified_key(sym, direc, "confirm"))
        if raw:
            try:
                last = datetime.fromisoformat(str(raw))
                if now - last < timedelta(minutes=minutes):
                    return False
            except ValueError:
                pass
        return True
    for other in ADVISORY_STAGES:
        raw = state.get(_unified_key(sym, direc, other))
        if not raw:
            continue
        try:
            last = datetime.fromisoformat(str(raw))
        except ValueError:
            continue
        if now - last < timedelta(minutes=minutes):
            return False
    return True


def mark_unified_sent(
    state: dict[str, str],
    *,
    symbol: str,
    direction: str,
    stage: str,
    now: datetime,
) -> None:
    state[_unified_key(symbol, direction, stage)] = now.isoformat()
    try:
        from hunt_core.scanner.delivery.delivery_state import mark_cross_channel_sent

        mark_cross_channel_sent(state, symbol=symbol, direction=direction, now=now)
    except Exception:
        pass


def _contract_issues_for_setup(
    *,
    direction: str,
    setup: dict[str, Any],
    min_risk_reward: float | None = None,
) -> list[Any]:
    """Validate the setup's live geometry against the signal contract.

    The setup carries the authoritative entry/stop/tp1/tp2 from the engine. tp3
    mirrors tp2 (no fabricated extra target) and valid_until is anchored to the
    fresh confirm tick so only the geometry invariants are exercised here.
    """
    ez = setup.get("entry_zone")
    try:
        entry_low = float(ez[0])
        entry_high = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return [SimpleNamespace(field="entry_zone", reason="missing_or_non_positive")]
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2") or tp1
    signal = SimpleNamespace(
        direction=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=setup.get("stop_loss"),
        tp1=tp1,
        tp2=tp2,
        tp3=tp2,
        scale_weights=(0.5, 0.5),
        valid_until=datetime.now(UTC) + timedelta(hours=12),
    )
    return validate_signal_contract(signal, min_risk_reward=min_risk_reward)


def _latch_delivery_geometry(setup: dict[str, Any]) -> None:
    """Freeze SL/TP/RR at first passing gate — prevents reanchor contract flicker."""
    if setup.get("_delivery_latched"):
        return
    ez = setup.get("entry_zone")
    setup["_delivery_latched"] = {
        "entry_zone": list(ez) if isinstance(ez, (list, tuple)) else ez,
        "stop_loss": setup.get("stop_loss"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "risk_reward": setup.get("risk_reward"),
    }


def _record_delivery_funnel(
    symbol: str,
    *,
    direction: str,
    setup: dict[str, Any],
    gate: Any,
    tier: str,
) -> None:
    """Telemetry only after ARMED/TRIGGERED tier is known (not at gate-pipeline mid-flight)."""
    from hunt_core.track.events import record_funnel_stage

    score_key = "dump_score" if direction == "short" else "long_score"
    record_funnel_stage(
        "deliver",
        symbol=symbol,
        direction=direction,
        detail=gate.code or "ok",
        payload={
            "symbol": symbol,
            "score": setup.get(score_key),
            "magnitude": setup.get("magnitude"),
            "p_win": setup.get("p_win"),
            "phase": setup.get("phase") or setup.get("lifecycle_phase"),
            "delivery_tier": tier,
            "risk_reward": setup.get("risk_reward"),
            "gate_code": gate.code or "ok",
            "confirmed": bool(setup.get("confirmed")),
            "lane": setup.get("delivery_lane"),
        },
    )


def _apply_delivery_latch(setup: dict[str, Any]) -> None:
    latched = setup.get("_delivery_latched")
    if not isinstance(latched, dict):
        return
    for key in ("entry_zone", "stop_loss", "tp1", "tp2", "risk_reward"):
        val = latched.get(key)
        if val is not None:
            setup[key] = val


def evaluate_delivery(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    symbol: str = "",
    sniper_config: "SniperConfig | None" = None,
    refresh_live_price: bool = False,
    ws_feed: Any | None = None,
) -> tuple[GateResult, str | None]:
    """Deliver a confirmed fusion setup: authorities → gate pipeline → geometry contract."""
    from hunt_core.scanner.delivery.arbiter import evaluate_confirm_authorities
    from hunt_core.scanner.delivery.lab import route_delivery_lane
    from hunt_core.scanner.gate.delivery import run_gate_pipeline
    from hunt_core.levels.levels import reanchor_setup_levels
    from hunt_core.shared.market import apply_live_price_to_row

    sym = symbol or str(row.get("symbol") or "")
    if refresh_live_price:
        apply_live_price_to_row(row, ws_feed=ws_feed)
    _apply_delivery_latch(setup)
    if not setup.get("telegram_sent") and not setup.get("_delivery_latched"):
        reanchor_setup_levels(setup, row, direction=direction, symbol=sym)

    sniper = sniper_config or SniperConfig.from_env()
    gate_result = run_gate_pipeline(
        direction=direction,
        setup=setup,
        row=row,
        lifecycle=lifecycle,
        symbol=sym,
        sniper_config=sniper,
    )
    blockers = [gate_result.code] if gate_result.code else []

    if not isinstance(row.get("manipulation_fusion"), dict):
        from hunt_core.analysis.manipulation_fusion import stamp_fusion_on_row

        stamp_fusion_on_row(row)

    lane = route_delivery_lane(setup=setup, row=row)
    setup["delivery_lane"] = lane

    if lane == "lab":
        min_rr = _resolve_min_rr(setup, direction=direction, symbol=sym)
        issues = _contract_issues_for_setup(direction=direction, setup=setup, min_risk_reward=min_rr)
        if issues:
            code = f"contract_{getattr(issues[0], 'field', 'invalid')}"
            return GateResult(ok=False, code=code, message=code), None
        setup["delivery_tier"] = "lab"
        _latch_delivery_geometry(setup)
        gate = GateResult(ok=True, code="lab_lane")
        _record_delivery_funnel(sym, direction=direction, setup=setup, gate=gate, tier="lab")
        return gate, "lab"

    arbiter = evaluate_confirm_authorities(
        row=row,
        direction=direction,
        setup=setup,
        blockers=blockers,
        lifecycle=lifecycle,
    )
    if not arbiter.ok:
        return arbiter, None
    if not gate_result.ok:
        return gate_result, None

    min_rr = _resolve_min_rr(setup, direction=direction, symbol=sym)
    issues = _contract_issues_for_setup(direction=direction, setup=setup, min_risk_reward=min_rr)
    if issues:
        code = f"contract_{getattr(issues[0], 'field', 'invalid')}"
        return GateResult(ok=False, code=code, message=code), None
    tier = "triggered"
    setup["delivery_tier"] = tier
    _latch_delivery_geometry(setup)
    gate = GateResult(ok=True, code="arbiter_pass")
    _record_delivery_funnel(sym, direction=direction, setup=setup, gate=gate, tier=tier)
    return gate, tier


def evaluate_delivery_fast(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    symbol: str = "",
    sniper_config: "SniperConfig | None" = None,
    refresh_live_price: bool = False,
    ws_feed: Any | None = None,
) -> tuple[GateResult, str | None]:
    """Hot path is identical to the full path under the fusion engine."""
    return evaluate_delivery(
        row,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle,
        symbol=symbol,
        sniper_config=sniper_config,
        refresh_live_price=refresh_live_price,
        ws_feed=ws_feed,
    )


def evaluate_forming_gate(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
    sniper_config: "SniperConfig | None" = None,
) -> GateResult:
    """Only confirmed fusion setups deliver; there is no forming/armed lane."""
    if not isinstance(setup, dict):
        return GateResult(ok=False, code="invalid_setup", message="invalid_setup")
    if setup.get("confirmed") or setup.get("intrabar_confirmed"):
        return GateResult(ok=True)
    return GateResult(ok=False, code="not_confirmed", message="not_confirmed")


def build_delivery_contract(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    delivery_tier: str,
    gate: "GateResult | None" = None,
    delivery_stage: str = "confirm",
) -> dict[str, Any]:
    """Build typed SetupDeliveryContract after successful gate evaluation."""
    from hunt_core.contract import build_setup_delivery_contract

    card = format_delivery_telegram(
        row,
        direction=direction,
        setup=setup,
        delivery_tier=delivery_tier,
        confirm_reasons=list(setup.get("confirm_hard") or []),
    )
    contract = build_setup_delivery_contract(
        row,
        direction=direction,
        setup=setup,
        delivery_tier=delivery_tier,
        delivery_stage=delivery_stage,  # type: ignore[arg-type]
        gate_code=gate.code if gate else None,
        card_html=card,
    )
    return dict(contract)


def format_delivery_telegram(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    delivery_tier: str,
    confirm_reasons: list[str] | None = None,
) -> str:
    """Format confirm/ARMED Telegram body — scanner macquette when enabled."""
    import os

    if os.environ.get("HUNT_SCANNER_MACQUETTE", "1") != "0":
        from hunt_core.scanner.telegram import format_scanner_from_setup

        sym = str(row.get("symbol") or "")
        alt = format_scanner_from_setup(sym, setup, row, lab=False)
        if alt:
            return alt
    body = format_delivery_card(
        row,
        direction=direction,
        setup=setup,
        delivery_tier=delivery_tier,
        confirm_reasons=confirm_reasons,
    )
    tf = row.get("timeframes")
    price = float(row.get("price") or row.get("last_price") or 0)
    sym = str(row.get("symbol") or "")
    if isinstance(tf, dict) and tf and price > 0 and sym:
        try:
            from hunt_core.analysis.confluence_grid import build_confluence_grid, format_grid_telegram

            grid = build_confluence_grid(row)
            footer = format_grid_telegram(grid)
            if footer:
                body = f"{body}\n\n{footer}"
        except Exception:
            pass
    cx = row.get("cross_exchange")
    if isinstance(cx, dict) and cx:
        from hunt_core.deliver.telegram import format_cross_exchange_section

        section = format_cross_exchange_section(cx)
        if section:
            return f"{body}\n\n{section}"
    return body


# --- merged from deliver/sniper.py ---

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SniperConfig:
    """Live TG delivery restricted to imminent pre-dump / pre-pump lifecycle windows.

    Mid-leg ``dump_active`` / ``impulse_initiating`` are monitor-only. Deep analysis
    for pinned or user symbols uses the ``/signal`` query path (not gated here).
    """

    enabled: bool = True
    live_phases_short: frozenset[str] = frozenset(
        {
            "exhaustion_at_high",
            "distribution",
            "dump_initiating",
            "pre_dump",
        }
    )
    live_phases_long: frozenset[str] = frozenset(
        {
            "accumulation",
            "breakout_arming",
            "post_dump_bounce",
            "recovery",
            "pre_pump",
        }
    )
    top_ls_max: float = 2.0
    require_top_ls: bool = True
    chase_tol: float = 0.002

    @property
    def live_phases(self) -> frozenset[str]:
        """Back-compat alias — short pre-dump phases only."""
        return self.live_phases_short

    @classmethod
    def from_env(cls) -> SniperConfig:
        wide = os.environ.get("HUNT_WIDE_MODE", "0") not in {"0", "false", "False"}
        default_sniper = "0" if wide else "1"
        off = os.environ.get("HUNT_SNIPER_MODE", default_sniper) in {"0", "false", "False"}
        require_ls = os.environ.get("HUNT_SNIPER_REQUIRE_TOP_LS", "1") not in {"0", "false", "False"}
        return cls(
            enabled=not off,
            top_ls_max=float(os.environ.get("HUNT_SNIPER_TOP_LS_MAX", "2.0")),
            require_top_ls=require_ls,
            chase_tol=float(os.environ.get("HUNT_SNIPER_CHASE_TOL", "0.002")),
        )


def effective_top_ls(market: dict[str, Any] | None) -> float | None:
    """Top-trader long/short ratio for the squeeze guard.

    Prefer the 1h window (full tier); fall back to the 5m window, which is
    fetched on EVERY tier. Live full-tier symbols show the two within ~1-2%
    (XAU 1.94/1.96, ETH 1.527/1.534, BTC 1.227/1.215), so the 5m proxy keeps
    fast-tier dump candidates from being blocked for missing 1h data while the
    squeeze guard stays intact.
    """
    m = market if isinstance(market, dict) else {}
    for key in ("top_ls_1h", "top_ls_5m"):
        v = m.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f == f:  # NaN guard
            return f
    return None


def _pct_str(entry: float, target: float | None, direction: str) -> str:
    """Move % from worst-fill entry edge to target (M1)."""
    if not entry or not target:
        return ""
    if direction == "short":
        pct = (entry - float(target)) / entry * 100.0
    else:
        pct = (float(target) - entry) / entry * 100.0
    return f"{pct:+.1f}%"


def _risk_pct_str(entry: float, stop: float | None, direction: str) -> str:
    """Downside % from worst-fill entry to stop."""
    if not entry or stop is None:
        return ""
    if direction == "short":
        pct = (float(stop) - entry) / entry * 100.0
    else:
        pct = (entry - float(stop)) / entry * 100.0
    return f"{pct:+.1f}%"


def _worst_entry_from_setup(setup: dict[str, Any], *, direction: str, price: float) -> float:
    from hunt_core.contract import worst_entry_edge

    edge = worst_entry_edge(setup, direction=direction)
    if edge is not None and edge > 0:
        return edge
    return price


def _order_flow_inputs_present(row: dict[str, Any]) -> bool:
    market = row.get("market") or {}
    if any(market.get(k) is not None for k in ("agg_trade_delta_30s", "agg_trade_delta_60s", "taker_5m", "depth_imbalance")):
        return True
    if any(market.get(k) is not None for k in ("ws_cvd_5m", "ws_cvd_1m", "kline_cvd_delta_5m", "kline_cvd_delta_1m")):
        return True
    tf = row.get("timeframes") or {}
    for tf_key in ("1m", "5m", "15m"):
        block = tf.get(tf_key) or tf.get(f"{tf_key}_closed") or {}
        if isinstance(block, dict) and (
            block.get("session_cvd") is not None or block.get("rolling_cvd_24h") is not None
        ):
            return True
    return row.get("order_flow") is not None


def _squeeze_note(row: dict[str, Any], *, direction: str) -> str | None:
    """Squeeze context without implying trade direction."""
    lc = row.get("lifecycle") or {}
    phase = str(lc.get("phase") or "")
    market = row.get("market") or {}
    top_ls = market.get("top_ls_1h")
    bits: list[str] = []
    if phase in {"accumulation", "breakout_arming", "impulse_initiating"}:
        bits.append("сжатие / накопление")
    try:
        if top_ls is not None and float(top_ls) >= 2.0:
            bits.append(f"top-traders long-heavy ({float(top_ls):.2f})")
    except (TypeError, ValueError):
        pass
    funding_pct_raw = market.get("funding_pct")
    funding_rate_raw = market.get("funding_rate")
    try:
        pct: float | None = None
        if funding_pct_raw is not None:
            pct = float(funding_pct_raw)
        elif funding_rate_raw is not None:
            pct = float(funding_rate_raw) * 100.0
        if pct is not None and abs(pct) >= 0.005:
            bits.append(f"funding {pct:+.3f}%")
    except (TypeError, ValueError):
        pass
    if not bits:
        return None
    return " · ".join(bits) + " — контекст, не сигнал " + ("шорт" if direction == "short" else "лонг")


def _for_against(row: dict[str, Any], *, direction: str, setup: dict[str, Any]) -> tuple[list[str], list[str]]:
    from hunt_core.deliver._labels import phase_human, trigger_human  # noqa: PLC0415

    for_us: list[str] = []
    against: list[str] = []
    triggers = list(setup.get("confirm_hard") or setup.get("triggers") or [])[:8]
    lc = row.get("lifecycle") or {}
    phase = str(lc.get("phase") or "")
    if phase:
        for_us.append(f"фаза {phase_human(phase)}")
    _bearish = (
        "reject", "lost_support", "below_support", "close_below", "below_impulse",
        "bear", "overbought", "cascade", "flush", "pp_short", "short_break",
        "dump", "distribution", "dump_continuation", "taker_sell", "microprice_sell",
    )
    _bullish = (
        "bounce", "reclaim", "close_above", "above_resistance", "broke_resistance",
        "bull", "oversold", "breakout", "pp_long", "long_break", "taker_buy",
        "microprice_buy", "accumulation",
    )
    _phantom_liq = frozenset({"ws_liq_cascade_score_only", "ws_liq_only", "liq_score_only"})
    from hunt_core.contract import parse_liquidation_score

    liq_raw = setup.get("liquidation_score") or (row.get("market") or {}).get("liquidation_score")
    liq = parse_liquidation_score(liq_raw)
    for t in triggers:
        ts = str(t).lower()
        if ts in _phantom_liq or "ws_liq_cascade_score_only" in ts:
            continue
        if liq is not None and liq <= 0.30 and ("liq" in ts or "cascade" in ts):
            continue
        is_bear = any(x in ts for x in _bearish)
        is_bull = any(x in ts for x in _bullish)
        label = trigger_human(str(t))
        if is_bear and not is_bull:
            (for_us if direction == "short" else against).append(label)
        elif is_bull and not is_bear:
            (for_us if direction == "long" else against).append(label)
        elif is_bear and is_bull:
            for_us.append(label)
    mtf = row.get("mtf")
    if mtf is not None:
        if direction == "short":
            sc = getattr(mtf, "short_scenario", None)
            opp = getattr(mtf, "long_scenario", None)
        else:
            sc = getattr(mtf, "long_scenario", None)
            opp = getattr(mtf, "short_scenario", None)
        if sc is not None and getattr(sc, "score", 0) >= 0.55:
            for_us.append(f"MTF за нас {getattr(sc, 'score', 0):.0%}")
        if opp is not None and getattr(opp, "score", 0) >= 0.6:
            against.append(f"MTF против {getattr(opp, 'score', 0):.0%}")
    bias = str(lc.get("recommended_bias") or "")
    if bias == "wait" and phase == "dump_active":
        against.append("lifecycle: жди (mid-dump)")
    return for_us[:5], against[:4]


def format_delivery_card(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    delivery_tier: str = "triggered",
    confirm_reasons: list[str] | None = None,
) -> str:
    """Build layered HTML card for Telegram confirm / ARMED delivery."""
    from hunt_core.deliver._context_lines import (
        delivery_context_lines,
        entry_mid,
        structured_thesis_lines,
    )
    from hunt_core.deliver._labels import (
        fmt_price,
        format_symbol_telegram,
        phase_human,
        signal_strength_rating,
    )
    from hunt_core.track.pump_history import format_history_telegram

    sym = format_symbol_telegram(str(row.get("symbol") or "?"))
    badge = "🔴" if direction == "short" else "🟢"
    dir_label = "SHORT" if direction == "short" else "LONG"
    price = float(row.get("price") or 0)
    lc = row.get("lifecycle") or {}
    lc_phase = str(lc.get("phase") or "—")
    armed = str(delivery_tier).lower() == "armed"
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    score_key = "dump_score" if direction == "short" else "long_score"
    p_win = setup.get("delivery_p_win") or setup.get("p_win")
    if p_win is not None:
        try:
            conviction = min(100, max(0, int(round(float(p_win) * 100))))
        except (TypeError, ValueError):
            conviction = 0
    else:
        fuel = float(setup.get(fuel_key) or setup.get(score_key) or 0)
        conviction = min(100, max(0, int(round(fuel))))
    fuel_for_rating = float(p_win or 0) * 100 if p_win is not None else float(
        setup.get(fuel_key) or setup.get(score_key) or 0
    )
    rating = signal_strength_rating(fuel_for_rating, lc_phase)

    if armed:
        verdict = "⏳ <b>ARMED · limit setup</b> — жди retest зоны"
    elif setup.get("intrabar_confirmed"):
        verdict = f"{badge} <b>IGNITION · {dir_label}</b> — intrabar confirm"
    else:
        verdict = f"{badge} <b>CONFIRM · {dir_label}</b>"

    ez = setup.get("entry_zone") or [price, price]
    try:
        entry_lo_f = float(ez[0])
        entry_hi_f = float(ez[1])
        entry_lo = fmt_price(entry_lo_f)
        entry_hi = fmt_price(entry_hi_f)
        degenerate_zone = abs(entry_hi_f - entry_lo_f) <= max(entry_hi_f, entry_lo_f, price) * 1e-5
    except (TypeError, ValueError, IndexError):
        entry_lo = entry_hi = "—"
        degenerate_zone = True
    entry_edge = _worst_entry_from_setup(setup, direction=direction, price=price)
    sl_raw = setup.get("stop_loss")
    sl = fmt_price(sl_raw)
    sl_pct = _risk_pct_str(entry_edge, float(sl_raw), direction) if sl_raw else ""
    sl_display = f"{sl} ({sl_pct})" if sl_pct else sl
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2")
    tp1_lbl = setup.get("tp1_label") or ""
    tp2_lbl = setup.get("tp2_label") or ""
    tp1_s = fmt_price(tp1) + (f" ({_pct_str(entry_edge, float(tp1), direction)})" if tp1 else "")
    if tp1_lbl:
        tp1_s += f" · {tp1_lbl}"
    tp2_s = fmt_price(tp2) + (f" ({_pct_str(entry_edge, float(tp2), direction)})" if tp2 else "")
    if tp2_lbl:
        tp2_s += f" · {tp2_lbl}"

    reasons = confirm_reasons if confirm_reasons is not None else list(setup.get("confirm_hard") or [])
    from hunt_core.deliver._sections import plain_delivery_reasons

    plain_reasons = plain_delivery_reasons(
        row, setup, direction=direction, confirm_reasons=reasons
    )
    for_us, against = _for_against(row, direction=direction, setup={**setup, "confirm_hard": reasons})

    inv_lines: list[str] = []
    inv_above = setup.get("invalidation_above")
    inv_below = setup.get("invalidation_below")
    if direction == "short" and inv_above:
        inv_lines.append(f"Инвалидация шорта выше <code>{fmt_price(inv_above)}</code>")
    elif direction == "long" and inv_below:
        inv_lines.append(f"Инвалидация лонга ниже <code>{fmt_price(inv_below)}</code>")
    elif sl != "—":
        inv_lines.append(f"Stop-loss <code>{sl_display}</code>")

    squeeze = _squeeze_note(row, direction=direction)

    lines = [
        f"{badge} <b>{sym}</b> · {dir_label} · conviction <code>{conviction}</code> · {rating}",
        "━━━━━━━━━━━━━━━━━━━━━━",
        verdict,
        f"Фаза: {html.escape(phase_human(lc_phase))}",
        "",
        "📋 <b>Сделка</b> (худший fill)",
    ]
    if degenerate_zone:
        lines.append("⚠️ <i>Entry zone не задана (degenerate)</i>")
    lines.extend([
        f"Entry <code>{entry_lo}–{entry_hi}</code>",
        f"SL <code>{sl_display}</code> · TP1 <code>{tp1_s}</code> · TP2 <code>{tp2_s}</code>",
    ])
    if price > 0:
        lines.append(f"Цена сейчас <code>{fmt_price(price)}</code>")
    if armed and price > 0:
        lines.append(
            f"⏳ Limit-вход <code>{entry_lo}–{entry_hi}</code> · жди retest / касание зоны"
        )

    thesis_lines, _raw_triggers = structured_thesis_lines(
        setup,
        direction=direction,
        lc_phase=lc_phase,
        confirm_reasons=reasons,
        entry_mid_px=entry_mid(ez, price),
    )
    if thesis_lines:
        lines.append("")
        lines.extend(thesis_lines)

    if plain_reasons:
        lines.append("")
        lines.append("💡 <b>Причины</b>")
        for r in plain_reasons[:5]:
            lines.append(f"· {html.escape(r)}")

    if for_us or against:
        lines.append("")
        if against and not plain_reasons:
            lines.append(f"⚠️ {html.escape(against[0])}")

    ctx = delivery_context_lines(row, direction=direction, price=price)
    if ctx:
        lines.append("")
        lines.extend(ctx)

    if inv_lines:
        lines.append("")
        lines.append("🛑 <b>Инвалидация</b>")
        lines.extend(inv_lines)

    if squeeze:
        lines.append("")
        lines.append(f"🌀 Squeeze: <i>{html.escape(squeeze)}</i>")

    if setup.get("funding_squeeze_caution"):
        lines.append("")
        lines.append(
            "⚠️ <i>Funding crowded short — только limit в зоне (caution tier)</i>"
        )

        of_block = row.get("order_flow_block") if isinstance(row.get("order_flow_block"), str) else ""
        if of_block:
            lines.append("")
            lines.append(of_block)

    from hunt_core.contract import compute_setup_risk_reward

    rr = compute_setup_risk_reward(setup, direction=direction)
    if rr is not None:
        rr_f = float(rr)
        rr_line = f"R:R (худший вход) <code>{rr_f:.2f}</code>"
        if rr_f < 1.5:
            rr_line += " ⚠️"
        lines.append(rr_line)

    hist = format_history_telegram(row.get("pump_history"))
    if hist:
        lines.append("")
        lines.append(html.escape(hist))

    from hunt_core.deliver._sections import (
        format_forecast_section,
        format_book_walls_section,
        format_liquidation_map_section,
        format_orderflow_section,
        format_volume_profile_section,
    )

    if row.get("maps") and not row.get("maps_forecast"):
        from hunt_core.maps.forecast import build_maps_forecast

        fc = build_maps_forecast(row)
        if fc:
            row["maps_forecast"] = fc

    for block_fn in (
        format_forecast_section,
        format_volume_profile_section,
        format_book_walls_section,
        format_liquidation_map_section,
        format_orderflow_section,
    ):
        block = block_fn(row)
        if block:
            lines.append("")
            lines.append(block)

    if armed:
        lines.append("")
        lines.append(
            "<i>Signal-only · ARMED = limit setup · TRIGGERED = цена в зоне · не auto-trade</i>"
        )
    else:
        lines.append("")
        lines.append("<i>Signal-only · closed 5m/1m confirm · открывай сделку вручную</i>")

    sym_cmd = str(row.get("symbol") or "").upper()
    if sym_cmd:
        lines.append("")
        lines.append(f"<i>Deep-анализ: /signal {html.escape(sym_cmd)}</i>")

    return "\n".join(lines)




# --- merged from deliver/invalidate_labels.py ---

INVALIDATE_LABELS: dict[str, str] = {
    "bounce_invalidate": "Отмена: lifecycle отскок — шорт больше не валиден",
    "trend_exhaustion": "Отмена: long в фазе exhaustion/distribution",
    "bias_flip": "Отмена: bias lifecycle сменился против позиции",
    "reclaim_invalidation": "Отмена: reclaim уровня инвалидации",
    "support_lost": "Отмена: потеря support (long)",
    "stop_hit": "Закрыто по Stop Loss",
    "trailing_stop_profit": "Закрыто по trailing stop (фиксация профита)",
    "tp1": "Закрыто по TP1",
    "tp2": "Закрыто по TP2",
    "legacy_unknown": "Закрыто (причина не зафиксирована в tracker)",
    "time_stall": "Закрыто: нет MFE за 8h — тезис не сработал",
}


def invalidate_detail_human(detail: str, *, reason: str = "") -> str:
    if reason and reason in INVALIDATE_LABELS:
        base = INVALIDATE_LABELS[reason]
        if detail and detail not in base:
            return f"{base} · {detail}"
        return base
    if detail:
        return detail
    return INVALIDATE_LABELS.get(reason, "Сигнал отменён")

# --- merged from deliver/readiness_labels.py ---


def readiness_score(setup: dict[str, Any], *, direction: str) -> float:
    score = setup.get("dump_score" if direction == "short" else "long_score")
    if score is not None:
        try:
            return float(score)
        except (TypeError, ValueError):
            pass
    fusion = setup.get("fusion_score")
    if fusion is not None:
        try:
            return float(fusion)
        except (TypeError, ValueError):
            pass
    return 0.0


def readiness_tier(score: float) -> str:
    if score >= 70:
        return "strong"
    if score >= 60:
        return "ready"
    if score >= 45:
        return "forming"
    return "watch"


def display_readiness_score(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any] | None = None,
    min_rr: float = _DEFAULT_MIN_RR,
) -> float:
    """Fuel capped for display when geometry is not tradable."""
    min_rr = _resolve_min_rr(setup, direction=direction)
    fuel = readiness_score(setup, direction=direction)
    if geometry_block_reason(setup, min_rr=min_rr, row=row, direction=direction):
        return min(fuel, 59.0)
    return fuel


def readiness_label_ru(score: float) -> str:
    """User-facing tier — never say «fuel»."""
    tier = readiness_tier(score)
    s = f"{score:.0f}/100"
    if tier == "strong":
        return f"готовность {s} · сильный сетап"
    if tier == "ready":
        return f"готовность {s} · ждём confirm"
    if tier == "forming":
        return f"готовность {s} · формирование"
    return f"готовность {s} · только наблюдение"


def readiness_label_for_setup(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any] | None = None,
    min_rr: float = _DEFAULT_MIN_RR,
) -> str:
    """Readiness line with optional geometry caveat (fuel vs tradability)."""
    raw = readiness_score(setup, direction=direction)
    display = display_readiness_score(
        setup, direction=direction, row=row, min_rr=min_rr
    )
    base = readiness_label_ru(display)
    reason = geometry_block_reason(
        setup, min_rr=min_rr, row=row, direction=direction
    )
    if not reason:
        return base
    raw_note = f" (raw {raw:.0f})" if raw > display + 0.5 else ""
    return f"{base}{raw_note} · ⚠️ {reason}"


def readiness_short_ru(score: float) -> str:
    return readiness_label_ru(score).split("·", 1)[0].strip()


def readiness_short_for_setup(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any] | None = None,
) -> str:
    return readiness_label_for_setup(setup, direction=direction, row=row).split("·", 1)[
        0
    ].strip()


def confirm_gap_readiness(score: float) -> str:
    """Gap line for confirm checklist."""
    if score >= 60:
        return f"готовность OK ({score:.0f}/100)"
    return f"готовность≥60 (сейчас {score:.0f}/100)"


