"""Unified delivery decision — gate + tier + format (all Telegram paths)."""
from __future__ import annotations



from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hunt_core.gate.delivery import GateResult

from hunt_core.contract import validate_signal_contract

# Cross-stage cooldown: one symbol+direction must not re-fire across advisory
# Telegram stages (early → dump_hunt → squeeze → confirm) inside the window.
DELIVERY_STAGES: tuple[str, ...] = ("early", "dump_hunt", "squeeze", "confirm")
ADVISORY_STAGES: tuple[str, ...] = ("early", "dump_hunt", "squeeze")
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


def _repair_setup_rr_for_contract(
    setup: dict[str, Any],
    *,
    direction: str,
    min_rr: float,
) -> None:
    """Snap TP1 to meet contract min R:R when SL/entry already viable (reanchor drift)."""
    if direction != "short" or not bool(setup.get("confirmed")):
        return
    ez = setup.get("entry_zone")
    try:
        entry_lo = float(ez[0])
        entry_hi = float(ez[1])
    except (TypeError, ValueError, IndexError):
        return
    stop = float(setup.get("stop_loss") or 0)
    tp1 = float(setup.get("tp1") or 0)
    if stop <= entry_hi or tp1 <= 0 or entry_lo <= 0:
        return
    worst = entry_hi
    risk = stop - worst
    if risk <= 0:
        return
    reward = worst - tp1
    rr = reward / risk if risk > 0 else 0.0
    if rr + 1e-9 >= min_rr:
        return
    from hunt_core.levels.levels import _snap_short_tp1_for_min_rr

    floor = float(
        setup.get("impulse_low")
        or setup.get("support_break_level")
        or setup.get("local_support")
        or 0
    )
    new_tp1, new_rr = _snap_short_tp1_for_min_rr(
        worst=worst,
        entry_lo=entry_lo,
        tp1=tp1,
        stop=stop,
        min_rr=min_rr,
        floor=floor,
    )
    if new_rr + 1e-9 < min_rr:
        return
    setup["tp1"] = new_tp1
    setup["risk_reward"] = new_rr
    tp2 = float(setup.get("tp2") or new_tp1)
    if tp2 > new_tp1:
        setup["tp2"] = new_tp1


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
    sniper_config: SniperConfig | None = None,
    refresh_live_price: bool = False,
    ws_feed: Any | None = None,
) -> tuple["GateResult", str | None]:
    """Run full gate pipeline and classify ARMED/TRIGGERED tier.

    Order is invariant: contract validation → gate pipeline → delivery freshness
    → tier. Returns ``(gate, delivery_tier)`` where tier is ``None`` when the
    contract is violated, the gate blocks, or the trigger is hard-stale.
    """
    from hunt_core.gate.delivery import (
        GateResult,
        classify_delivery_tier,
        delivery_freshness_block,
        effective_min_rr_for_delivery,
        run_gate_pipeline,
        tp1_progress_block,
    )
    from hunt_core.market.live_price import apply_live_price_to_row

    sym = symbol or str(row.get("symbol") or "")
    if refresh_live_price:
        apply_live_price_to_row(row, ws_feed=ws_feed)
    from hunt_core.levels.levels import reanchor_setup_levels

    _apply_delivery_latch(setup)
    # Re-anchor shifts SL/TP with live price — skip when geometry is latched or TG shipped.
    if not setup.get("telegram_sent") and not setup.get("_delivery_latched"):
        reanchor_setup_levels(setup, row, direction=direction, symbol=sym)
    lc = lifecycle if isinstance(lifecycle, dict) else row.get("lifecycle")
    lc_dict = lc if isinstance(lc, dict) else {}
    min_rr = effective_min_rr_for_delivery(
        setup,
        direction=direction,
        symbol=sym,
        lifecycle=lc_dict,
    )
    _repair_setup_rr_for_contract(
        setup,
        direction=direction,
        min_rr=min_rr,
    )
    issues = _contract_issues_for_setup(
        direction=direction,
        setup=setup,
        min_risk_reward=min_rr,
    )
    if issues:
        first = issues[0]
        code = f"contract_{getattr(first, 'field', 'invalid')}"
        return GateResult(ok=False, code=code, message=code), None
    from hunt_core.confluence.confluence import evaluate_must_pass

    must_ok, must_missing = evaluate_must_pass(row, direction=direction)
    if not must_ok:
        code = f"must_pass:{must_missing[0] if must_missing else 'unknown'}"
        return GateResult(ok=False, code=code, message=code), None
    from hunt_core.confluence.confluence import (
        FAMILY_VOTE_MIN,
        build_mtf_confluence,
        family_vote_count,
    )

    tf = row.get("timeframes") or {}
    price = float(row.get("price") or row.get("last_price") or 0)
    mtf = row.get("mtf")
    if mtf is None and isinstance(tf, dict) and tf and price > 0 and sym:
        mtf = build_mtf_confluence(
            sym,
            tf,
            price,
            market=row.get("market") if isinstance(row.get("market"), dict) else None,
            row=row,
        )
        row["mtf"] = mtf
    hard_confirm = list(setup.get("confirm_hard") or [])
    lc_phase = str((lc_dict or {}).get("phase") or "")
    fall_pct = float((lc_dict or {}).get("fall_from_high_pct") or 0)
    setup_fuel = float(setup.get("dump_fuel") or setup.get("long_fuel") or 0)
    setup_score = float(setup.get("dump_score") or setup.get("long_score") or 0)
    struct_count = sum(
        1
        for h in hard_confirm
        if any(
            m in str(h)
            for m in (
                "close_below",
                "below_support",
                "pp_short",
                "bear_cascade",
                "rejection",
                "continuation",
            )
        )
    )
    skip_family_vote = (
        "dump_continuation_confirm" in hard_confirm
        or (
            lc_phase == "dump_active"
            and fall_pct >= 15.0
            and "dump_continuation_confirm" in hard_confirm
        )
        or (
            lc_phase == "dump_active"
            and fall_pct >= 15.0
            and struct_count >= 2
            and setup_score >= 100.0
        )
        or (
            direction == "short"
            and bool(setup.get("confirmed"))
            and setup_score >= 100.0
            and len(hard_confirm) >= 1
        )
        or (
            direction == "short"
            and bool(setup.get("confirmed"))
            and setup_score >= 90.0
            and struct_count >= 1
        )
        or (
            direction == "short"
            and lc_phase in {"exhaustion_at_high", "distribution"}
            and setup_fuel >= 75.0
            and struct_count >= 2
        )
        or (
            direction == "short"
            and bool(setup.get("confirmed"))
            and lc_phase in {"exhaustion_at_high", "distribution"}
            and struct_count >= 1
            and setup_fuel >= 68.0
        )
        or (
            direction == "short"
            and bool(setup.get("confirmed"))
            and lc_phase == "dump_active"
            and fall_pct >= 12.0
            and struct_count >= 1
            and setup_fuel >= 75.0
        )
        or (
            direction == "short"
            and setup_score >= 95.0
            and struct_count >= 2
            and lc_phase in {"exhaustion_at_high", "distribution", "dump_active"}
        )
    )
    if mtf is not None and not skip_family_vote:
        from hunt_core.confluence.mtf import MTFConfluence

        votes = family_vote_count(mtf, direction=direction)
        # Adaptive threshold: dynamic altcoins only have 4H data (no 1W/1D EMAs
        # loaded in fast-tier scan). When htf_total < FAMILY_VOTE_MIN, require
        # all available HTFs to align rather than an impossible absolute count.
        htf_total = (
            (mtf.short_scenario if direction == "short" else mtf.long_scenario).htf_total
            if isinstance(mtf, MTFConfluence)
            else FAMILY_VOTE_MIN
        )
        required_votes = min(FAMILY_VOTE_MIN, htf_total) if htf_total > 0 else FAMILY_VOTE_MIN
        # No HTF families loaded (young alts / fast tier): do not hard-block when
        # detector already confirmed with structural evidence (ALLOUSDT replay: 16×).
        if htf_total == 0 and skip_family_vote:
            pass
        elif votes < required_votes or htf_total == 0:
            code = f"family_vote_low:{votes}<{required_votes}"
            return GateResult(ok=False, code=code, message=code), None
    gate = run_gate_pipeline(
        direction=direction,
        setup=setup,
        row=row,
        lifecycle=lc_dict or None,
        symbol=sym,
        sniper_config=sniper_config,
    )
    if not gate.ok:
        return gate, None
    tier = classify_delivery_tier(
        direction=direction,
        setup=setup,
        row=row,
        lifecycle=lc_dict or None,
    )
    if tier == "triggered":
        stale = delivery_freshness_block(
            direction=direction,
            setup=setup,
            row=row,
            lifecycle=lc_dict or None,
        )
        if stale:
            return GateResult(ok=False, code=stale, message=stale), None
        if tp1_progress_block(direction=direction, setup=setup, row=row):
            tier = "armed"
    if gate.ok:
        _latch_delivery_geometry(setup)
    return gate, tier


def _fast_lane_htf_waiver(
    *,
    direction: str,
    setup: dict[str, Any],
    lc_dict: dict[str, Any],
    must_missing: list[str],
) -> bool:
    """Hot-path waiver: structural confirmed dump should not wait for full MTF refresh."""
    if direction != "short" or "htf_bias_veto" not in must_missing:
        return False
    if not bool(setup.get("confirmed")):
        return False
    phase = str(lc_dict.get("phase") or "")
    fall_pct = float(lc_dict.get("fall_from_high_pct") or 0)
    fuel = float(setup.get("dump_fuel") or 0)
    hard = list(setup.get("confirm_hard") or [])
    struct = any(
        m in str(h)
        for h in hard
        for m in ("close_below", "below_support", "pp_short", "rejection", "continuation")
    )
    if phase == "dump_active" and fall_pct >= 12.0 and struct:
        return True
    if phase in {"distribution", "exhaustion_at_high"} and fuel >= 60.0 and struct:
        return True
    return len(must_missing) == 1


def evaluate_delivery_fast(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None = None,
    symbol: str = "",
    sniper_config: SniperConfig | None = None,
    refresh_live_price: bool = False,
    ws_feed: Any | None = None,
) -> tuple["GateResult", str | None]:
    """Hot-path delivery: contract + fast must-pass + lean gate stack (no family vote)."""
    from hunt_core.gate.delivery import (
        GateResult,
        classify_delivery_tier,
        delivery_freshness_block,
        effective_min_rr_for_delivery,
        run_gate_pipeline,
        tp1_progress_block,
    )
    from hunt_core.market.live_price import apply_live_price_to_row

    sym = symbol or str(row.get("symbol") or "")
    if refresh_live_price:
        apply_live_price_to_row(row, ws_feed=ws_feed)
    _apply_delivery_latch(setup)
    if not setup.get("telegram_sent") and not setup.get("_delivery_latched"):
        from hunt_core.levels.levels import reanchor_setup_levels

        reanchor_setup_levels(setup, row, direction=direction, symbol=sym)
    lc = lifecycle if isinstance(lifecycle, dict) else row.get("lifecycle")
    lc_dict = lc if isinstance(lc, dict) else {}
    min_rr = effective_min_rr_for_delivery(
        setup,
        direction=direction,
        symbol=sym,
        lifecycle=lc_dict,
    )
    _repair_setup_rr_for_contract(setup, direction=direction, min_rr=min_rr)
    issues = _contract_issues_for_setup(
        direction=direction,
        setup=setup,
        min_risk_reward=min_rr,
    )
    if issues:
        first = issues[0]
        code = f"contract_{getattr(first, 'field', 'invalid')}"
        return GateResult(ok=False, code=code, message=code), None
    from hunt_core.confluence.confluence import evaluate_must_pass

    must_ok, must_missing = evaluate_must_pass(row, direction=direction)
    if not must_ok and _fast_lane_htf_waiver(
        direction=direction,
        setup=setup,
        lc_dict=lc_dict,
        must_missing=list(must_missing),
    ):
        must_ok = True
    if not must_ok:
        code = f"must_pass:{must_missing[0] if must_missing else 'unknown'}"
        return GateResult(ok=False, code=code, message=code), None
    gate = run_gate_pipeline(
        direction=direction,
        setup=setup,
        row=row,
        lifecycle=lc_dict or None,
        symbol=sym,
        sniper_config=sniper_config,
        fast_lane=True,
    )
    if not gate.ok:
        return gate, None
    tier = classify_delivery_tier(
        direction=direction,
        setup=setup,
        row=row,
        lifecycle=lc_dict or None,
    )
    if tier == "triggered":
        stale = delivery_freshness_block(
            direction=direction,
            setup=setup,
            row=row,
            lifecycle=lc_dict or None,
        )
        if stale:
            return GateResult(ok=False, code=stale, message=stale), None
        if tp1_progress_block(direction=direction, setup=setup, row=row):
            tier = "armed"
    if gate.ok:
        setup["delivery_lane"] = "fast"
        _latch_delivery_geometry(setup)
    return gate, tier


def shadow_full_lane_recheck(
    row: dict[str, Any],
    *,
    direction: str,
    setup: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    symbol: str,
    broadcaster: Any | None = None,
    send_telegram: bool = False,
) -> bool:
    """After hot delivery, verify full lane would still pass; emit telemetry if not."""
    gate, tier = evaluate_delivery(
        row,
        direction=direction,
        setup=setup,
        lifecycle=lifecycle,
        symbol=symbol,
        refresh_live_price=False,
    )
    if gate.ok and tier is not None:
        return True
    import logging

    from hunt_core.track.events import append_signal_event

    sym = symbol or str(row.get("symbol") or "")
    LOG = logging.getLogger("hunt_core.deliver.dispatch")
    LOG.warning(
        "fast_lane_full_shadow_block | sym=%s dir=%s gate=%s path=%s",
        sym,
        direction,
        gate.code,
        row.get("tick_path"),
    )
    append_signal_event(
        "fast_lane_shadow_block",
        symbol=sym,
        direction=direction,
        detail=str(gate.code or "blocked"),
        payload={
            "gate_code": gate.code,
            "tick_path": row.get("tick_path"),
            "phase": setup.get("phase"),
            "score": setup.get("dump_score") or setup.get("long_score"),
        },
    )
    if send_telegram and broadcaster is not None:
        import asyncio
        import html

        sym_label = html.escape(sym.replace("USDT", "-USDT"))
        msg = (
            f"⚠️ <b>Fast lane shadow block</b> {sym_label}\n"
            f"<code>{html.escape(str(gate.code or 'blocked'))}</code>\n"
            f"<i>Full lane would reject — review active signal</i>"
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(broadcaster.send_html(msg, no_split=True))
        except RuntimeError:
            pass
    return False


def evaluate_forming_gate(
    setup: dict[str, Any],
    *,
    direction: str,
    symbol: str,
    lifecycle: Any | None = None,
    row: dict[str, Any] | None = None,
    sniper_config: SniperConfig | None = None,
) -> "GateResult":
    """Gate check for non-confirmed (forming) setups — single entry for run_gate_pipeline."""
    from hunt_core.gate.delivery import GateResult, run_gate_pipeline

    if not isinstance(setup, dict):
        return GateResult(ok=False, code="invalid_setup", message="invalid_setup")
    if bool(setup.get("confirmed")):
        return GateResult(ok=True, code="", message="")
    cfg = sniper_config or SniperConfig.from_env()
    return run_gate_pipeline(
        setup=setup,
        direction=direction,
        symbol=symbol,
        lifecycle=lifecycle,
        row=row or {},
        sniper_config=cfg,
    )


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
    """Format confirm/ARMED Telegram body via layered card_formatter."""
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
            from hunt_core.analysis.deep_signal import (
                build_mtf_confluence,
                format_mtf_scorecard_footer,
            )

            mtf = row.get("mtf")
            if mtf is None:
                mtf = build_mtf_confluence(
                    sym,
                    tf,
                    price,
                    market=row.get("market") if isinstance(row.get("market"), dict) else None,
                    row=row,
                )
            footer = format_mtf_scorecard_footer(mtf, direction=direction)
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
    """Live TG delivery restricted to the fresh-short-entry slice.

    ``live_phases`` MUST be the phases where the lifecycle FSM grants
    ``short_entry_ok`` (leg_fsm.assess_hunt_lifecycle): exhaustion_at_high /
    distribution / dump_initiating — fade at the top / catch the dump as it
    starts. ``dump_active`` is intentionally excluded: the FSM forces
    ``short_entry_ok=False`` there ("no_new_short_entry_mid_dump" — late chase
    loses), so gating live on ``{dump_active}`` while also requiring
    ``short_entry_ok`` was self-contradictory and delivered zero signals.
    """

    enabled: bool = True
    live_phases: frozenset[str] = frozenset(
        {"exhaustion_at_high", "distribution", "dump_initiating"}
    )
    top_ls_max: float = 2.0
    require_top_ls: bool = True
    chase_tol: float = 0.002

    @classmethod
    def from_env(cls) -> SniperConfig:
        wide = os.environ.get("HUNT_WIDE_MODE", "1") not in {"0", "false", "False"}
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


def sniper_block_reason(
    *,
    direction: str,
    setup: dict[str, Any],
    row: dict[str, Any],
    lifecycle: dict[str, Any] | None,
    config: SniperConfig | None = None,
) -> str | None:
    """Return a machine block code if sniper mode vetoes TG delivery, else None."""
    cfg = config or SniperConfig.from_env()
    if not cfg.enabled:
        return None
    if direction != "short":
        return "sniper_long_shadow"
    lc = lifecycle if isinstance(lifecycle, dict) else {}
    phase = str(lc.get("phase") or "")
    if phase not in cfg.live_phases:
        cont_dump_active = False
        if phase == "dump_active":
            from hunt_core.gate.delivery import _dump_continuation_short_ok
            from hunt_core.params.store import effective_hunt_params

            sym = str(row.get("symbol") or "").upper()
            cal = effective_hunt_params(sym)
            fuel = float(setup.get("dump_fuel") or setup.get("dump_score") or 0)
            cont_dump_active = _dump_continuation_short_ok(
                setup,
                phase=phase,
                lc=lc,
                fuel=fuel,
                cal_min_fuel=cal.confirm_min_score,
            )
        if not cont_dump_active:
            return f"sniper_phase:{phase or 'unknown'}"
    if lc.get("short_entry_ok") is not True:
        from hunt_core.gate.delivery import _dump_continuation_short_ok
        from hunt_core.params.store import effective_hunt_params

        sym = str(row.get("symbol") or "").upper()
        cal = effective_hunt_params(sym)
        fuel = float(
            setup.get("dump_fuel") or setup.get("dump_score") or 0
        )
        if not _dump_continuation_short_ok(
            setup,
            phase=phase,
            lc=lc,
            fuel=fuel,
            cal_min_fuel=cal.confirm_min_score,
        ):
            return "sniper_short_entry_not_ok"
    ez = setup.get("entry_zone")
    try:
        zone_lo = float(ez[0])
        px = float(row["price"])
    except (TypeError, ValueError, IndexError, KeyError):
        return "sniper_bad_entry_geometry"
    if px < zone_lo * (1.0 - cfg.chase_tol):
        return "sniper_late_chase"
    top_ls_f = effective_top_ls(row.get("market"))
    # Absent = small altcoin without Binance FAPI top-trader endpoint.
    # All top historical performers (ESPORTS, BTW, BEAT) lacked this data.
    # Only block when data IS present and top traders are heavily long (squeeze risk).
    if top_ls_f is not None and top_ls_f >= cfg.top_ls_max:
        return "sniper_top_ls_high"
    return None

# --- merged from deliver/card_formatter.py ---

import html

from hunt_core.analysis.deep_signal import format_order_flow_block, synthesize_order_flow


def _pct_str(price: float, target: float | None, direction: str) -> str:
    if not price or not target:
        return ""
    if direction == "short":
        pct = (price - float(target)) / price * 100.0
    else:
        pct = (float(target) - price) / price * 100.0
    return f"{pct:+.1f}%"


def _squeeze_note(row: dict[str, Any], *, direction: str) -> str | None:
    """Squeeze context without implying trade direction."""
    lc = row.get("lifecycle") or {}
    phase = str(lc.get("phase") or "")
    market = row.get("market") or {}
    top_ls = market.get("top_ls_1h")
    funding = market.get("funding_pct")
    bits: list[str] = []
    if phase in {"accumulation", "breakout_arming", "impulse_initiating"}:
        bits.append("сжатие / накопление")
    try:
        if top_ls is not None and float(top_ls) >= 2.0:
            bits.append(f"top-traders long-heavy ({float(top_ls):.2f})")
    except (TypeError, ValueError):
        pass
    try:
        if funding is not None and abs(float(funding)) >= 0.03:
            bits.append(f"funding {float(funding):+.3f}%")
    except (TypeError, ValueError):
        pass
    if not bits:
        return None
    return " · ".join(bits) + " — контекст, не сигнал " + ("шорт" if direction == "short" else "лонг")


def _for_against(row: dict[str, Any], *, direction: str, setup: dict[str, Any]) -> tuple[list[str], list[str]]:
    from hunt_core.deliver.telegram import phase_human  # noqa: PLC0415

    for_us: list[str] = []
    against: list[str] = []
    triggers = list(setup.get("confirm_hard") or setup.get("triggers") or [])[:8]
    lc = row.get("lifecycle") or {}
    phase = str(lc.get("phase") or "")
    if phase:
        for_us.append(f"фаза {phase_human(phase)}")
    for t in triggers:
        ts = str(t).lower()
        if any(x in ts for x in ("reject", "lost_support", "bear", "overbought", "cascade", "flush")):
            (for_us if direction == "short" else against).append(ts.replace("_", " "))
        elif any(x in ts for x in ("bounce", "support", "bull", "oversold", "breakout")):
            (for_us if direction == "long" else against).append(ts.replace("_", " "))
        else:
            for_us.append(ts.replace("_", " ")[:40])
    mtf = row.get("mtf")
    if mtf is not None:
        if direction == "short":
            sc = getattr(mtf, "short_scenario", None)
            opp = getattr(mtf, "long_scenario", None)
        else:
            sc = getattr(mtf, "long_scenario", None)
            opp = getattr(mtf, "short_scenario", None)
        if sc is not None and getattr(sc, "score", 0) >= 0.55:
            for_us.append(f"MTF score {getattr(sc, 'score', 0):.2f}")
        if opp is not None and getattr(opp, "score", 0) >= 0.6:
            against.append(f"MTF против {getattr(opp, 'score', 0):.2f}")
    bias = str(lc.get("recommended_bias") or "")
    if bias == "wait" and phase == "dump_active":
        against.append("bias=wait mid-dump")
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
    from hunt_core.deliver.telegram import fmt_price, phase_human

    sym = html.escape(str(row.get("symbol") or "?").replace("USDT", "-USDT"))
    badge = "🔴" if direction == "short" else "🟢"
    dir_label = "SHORT" if direction == "short" else "LONG"
    price = float(row.get("price") or 0)
    lc = row.get("lifecycle") or {}
    lc_phase = str(lc.get("phase") or "—")
    armed = str(delivery_tier).lower() == "armed"
    fuel_key = "dump_fuel" if direction == "short" else "long_fuel"
    fuel = float(setup.get(fuel_key) or setup.get("dump_score") or setup.get("long_score") or 0)

    if armed:
        verdict = "⏳ <b>ARMED · limit setup</b> — жди retest зоны"
    else:
        verdict = f"{badge} <b>CONFIRM · {dir_label}</b> — вход по closed-bar"

    ez = setup.get("entry_zone") or [price, price]
    try:
        entry_lo = fmt_price(float(ez[0]))
        entry_hi = fmt_price(float(ez[1]))
    except (TypeError, ValueError, IndexError):
        entry_lo = entry_hi = "—"
    sl = fmt_price(setup.get("stop_loss"))
    tp1 = setup.get("tp1")
    tp2 = setup.get("tp2")
    tp1_s = fmt_price(tp1) + (f" ({_pct_str(price, float(tp1), direction)})" if tp1 else "")
    tp2_s = fmt_price(tp2) + (f" ({_pct_str(price, float(tp2), direction)})" if tp2 else "")

    reasons = confirm_reasons if confirm_reasons is not None else list(setup.get("confirm_hard") or [])
    for_us, against = _for_against(row, direction=direction, setup={**setup, "confirm_hard": reasons})

    inv_lines: list[str] = []
    inv_above = setup.get("invalidation_above")
    inv_below = setup.get("invalidation_below")
    if direction == "short" and inv_above:
        inv_lines.append(f"Инвалидация шорта выше <code>{fmt_price(inv_above)}</code>")
    elif direction == "long" and inv_below:
        inv_lines.append(f"Инвалидация лонга ниже <code>{fmt_price(inv_below)}</code>")
    elif sl != "—":
        inv_lines.append(f"Stop-loss <code>{sl}</code>")

    squeeze = _squeeze_note(row, direction=direction)

    lines = [
        f"{badge} <b>{sym}</b> · {dir_label} · fuel <code>{fuel:.0f}</code>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        verdict,
        f"Фаза: {html.escape(phase_human(lc_phase))}",
        "",
        "📋 <b>Сделка</b>",
        f"Entry <code>{entry_lo}–{entry_hi}</code>",
        f"SL <code>{sl}</code> · TP1 <code>{tp1_s}</code> · TP2 <code>{tp2_s}</code>",
    ]
    if price > 0:
        lines.append(f"Цена сейчас <code>{fmt_price(price)}</code>")

    if for_us or against:
        lines.append("")
        lines.append("⚖️ <b>За / против</b>")
        if for_us:
            lines.append("✅ " + html.escape("; ".join(for_us)))
        if against:
            lines.append("❌ " + html.escape("; ".join(against)))

    if inv_lines:
        lines.append("")
        lines.append("🛑 <b>Инвалидация</b>")
        lines.extend(inv_lines)

    if squeeze:
        lines.append("")
        lines.append(f"🌀 Squeeze: <i>{html.escape(squeeze)}</i>")

    of = row.get("order_flow")
    if of is None:
        of = synthesize_order_flow(row)
        row["order_flow"] = of.to_dict() if hasattr(of, "to_dict") else of
    of_block = format_order_flow_block(of)
    if of_block:
        lines.append("")
        lines.append(of_block)

    rr = setup.get("risk_reward")
    if rr is not None:
        try:
            lines.append(f"RR <code>{float(rr):.2f}</code>")
        except (TypeError, ValueError):
            pass

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


_DEFAULT_MIN_RR = 1.6
_POC_HEADWIND_PCT = 0.5


def readiness_score(setup: dict[str, Any], *, direction: str) -> float:
    key = "dump_fuel" if direction == "short" else "long_fuel"
    try:
        return float(setup.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def readiness_tier(score: float) -> str:
    if score >= 70:
        return "strong"
    if score >= 60:
        return "ready"
    if score >= 45:
        return "forming"
    return "watch"


def _setup_rr(setup: dict[str, Any]) -> float | None:
    raw = setup.get("risk_reward")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _resolve_min_rr(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any] | None,
    min_rr: float,
) -> float:
    if row is None:
        return min_rr
    sym = str(row.get("symbol") or "").strip().upper()
    if not sym:
        return min_rr
    from hunt_core.gate.delivery import effective_min_rr_for_delivery

    lc = row.get("lifecycle") if isinstance(row.get("lifecycle"), dict) else {}
    return effective_min_rr_for_delivery(
        setup, direction=direction, symbol=sym, lifecycle=lc
    )


def geometry_block_reason(
    setup: dict[str, Any],
    *,
    min_rr: float = _DEFAULT_MIN_RR,
    row: dict[str, Any] | None = None,
    direction: str = "",
) -> str | None:
    """Why trade geometry blocks a high fuel score from implying «strong setup»."""
    min_rr = _resolve_min_rr(setup, direction=direction, row=row, min_rr=min_rr)
    return geometry_block_evidence(
        setup, min_rr=min_rr, row=row, direction=direction
    ).get("reason")


def geometry_block_evidence(
    setup: dict[str, Any],
    *,
    min_rr: float = _DEFAULT_MIN_RR,
    row: dict[str, Any] | None = None,
    direction: str = "",
) -> dict[str, Any]:
    """Structured geometry veto — code, reason, evidence list (§3 cleanup)."""
    min_rr = _resolve_min_rr(setup, direction=direction, row=row, min_rr=min_rr)
    evidence: list[str] = []
    if setup.get("levels_viable") is False:
        veto = setup.get("levels_veto") or []
        tail = ", ".join(str(v) for v in veto[:2]) if veto else "levels_veto"
        evidence.extend(str(v) for v in veto[:4])
        return {"code": "levels_veto", "reason": f"уровни: {tail}", "evidence": evidence}
    rr = _setup_rr(setup)
    if rr is not None and rr < min_rr:
        evidence.append(f"risk_reward={rr:.3f}")
        evidence.append(f"min_rr={min_rr:.1f}")
        return {
            "code": "min_rr",
            "reason": f"RR {rr:.2f} < {min_rr:.1f}",
            "evidence": evidence,
        }
    if row and direction == "short":
        regime = row.get("regime") or {}
        poc_dir = str(regime.get("poc_direction_1h") or "")
        poc = regime.get("poc_1h")
        price = float(row.get("price") or 0)
        if poc_dir == "long" and poc and price > 0:
            try:
                dist = abs(price - float(poc)) / price * 100.0
            except (TypeError, ValueError):
                dist = 999.0
            if dist <= _POC_HEADWIND_PCT:
                evidence.append(f"poc={float(poc):.0f}")
                evidence.append(f"dist_pct={dist:.2f}")
                return {
                    "code": "poc_headwind",
                    "reason": f"POC поддержка {float(poc):.0f} ({dist:.2f}%)",
                    "evidence": evidence,
                }
    triggers = {str(t) for t in (setup.get("triggers") or [])}
    if direction == "short" and any("poc_contra" in t for t in triggers):
        evidence.extend(sorted(t for t in triggers if "poc_contra" in t))
        return {
            "code": "poc_contra",
            "reason": "POC contra (short в поддержку)",
            "evidence": evidence,
        }
    if direction == "long" and any("poc_contra" in t for t in triggers):
        evidence.extend(sorted(t for t in triggers if "poc_contra" in t))
        return {
            "code": "poc_contra",
            "reason": "POC contra (long в сопротивление POC)",
            "evidence": evidence,
        }
    return {"code": "", "reason": None, "evidence": evidence}


def display_readiness_score(
    setup: dict[str, Any],
    *,
    direction: str,
    row: dict[str, Any] | None = None,
    min_rr: float = _DEFAULT_MIN_RR,
) -> float:
    """Fuel capped for display when geometry is not tradable."""
    min_rr = _resolve_min_rr(setup, direction=direction, row=row, min_rr=min_rr)
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


