"""Pinned-symbol deep panels, lake, forecast, and scenario assembly."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import polars as pl

from hunt_core.analysis.adx_thresholds import ADX_PANEL_NEUTRAL, ADX_RANGE_MAX
from hunt_core.confluence.mtf import ScenarioScore, build_mtf_confluence
from hunt_core.analysis.trend_engine import (
    normalize_rsi14,
    resolve_tf_snap,
    trend_from_snapshot,
)
from hunt_core.data.lake import LakeDataError, read_features
from hunt_core.errors import DEFENSIVE_EXC
from hunt_core.features.prepare_columns import PINNED_SYMBOLS, resolve_prepare_groups_for_symbol
from hunt_core.features.prepare_frame import _prepare_frame

PINNED_SNAPSHOT_EXTRA_KEYS: tuple[str, ...] = (
    "ema200",
    "plus_di14",
    "minus_di14",
    "stoch_d14",
    "mfi14",
    "cci20",
    "willr14",
    "squeeze_hist",
    "tenkan",
    "kijun",
    "aroon_up14",
    "aroon_down14",
    "psy12",
    "mtm12",
    "mom10",
    "bias6",
    "trix14",
    "ppo12_26",
    "ad_line",
    "adosc_3_10",
    "session_cvd",
    "rolling_cvd_24h",
    "wq_ts_rank_close20",
    "wq_ts_corr_close_vol20",
    "wq_ts_delta_rsi5",
    "kdj_k14",
    "kdj_d14",
    "kdj_j14",
    "tdx_boll_mid",
    "tdx_boll_upper",
    "tdx_boll_lower",
)


def is_pinned_symbol(symbol: str) -> bool:
    return str(symbol or "").strip().upper() in PINNED_SYMBOLS


def prepare_frame_for_symbol(df: pl.DataFrame, symbol: str) -> pl.DataFrame:
    if df.is_empty():
        return df
    groups = resolve_prepare_groups_for_symbol(symbol)
    return _prepare_frame(df, active_groups=groups)


def prepare_htf_frame(df: pl.DataFrame | None, symbol: str) -> pl.DataFrame | None:
    if df is None or df.is_empty():
        return None
    groups = resolve_prepare_groups_for_symbol(symbol)
    warmup = 50 if df.height < 220 else 200
    return _prepare_frame(df, active_groups=groups, warmup_ema=warmup)


def _round_val(value: Any, *, digits: int = 4) -> float | None:
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(fv):
        return None
    return round(fv, digits)


def enrich_pinned_tf_snapshot(
    base: dict[str, Any],
    df: Any,
    *,
    symbol: str = "",
) -> dict[str, Any]:
    if symbol and not is_pinned_symbol(symbol):
        return base
    if df is None or getattr(df, "is_empty", lambda: True)():
        return base
    out = dict(base)
    idx = -2 if base.get("closed_bar") and df.height >= 2 else -1
    pos = idx if idx >= 0 else df.height + idx

    for key in PINNED_SNAPSHOT_EXTRA_KEYS:
        if key in out or key not in df.columns:
            continue
        try:
            raw = df.item(pos, key)
        except (IndexError, ValueError, TypeError):
            continue
        rounded = _round_val(raw, digits=3 if key.startswith("wq_") else 4)
        if rounded is not None:
            out[key] = rounded
    return out




@dataclass(frozen=True, slots=True)
class PinnedLakeSlice:
    symbol: str
    tf: str
    start_ts: str
    end_ts: str
    rows: int
    frame: pl.DataFrame


def _default_window_hours(symbol: str) -> int:
    sym = symbol.upper()
    if sym in {"BTCUSDT", "ETHUSDT"}:
        return 72
    if sym in {"XAUUSDT", "XAGUSDT"}:
        return 48
    return 24


def load_pinned_lake_slice(
    symbol: str,
    *,
    hours: int | None = None,
    tf: str = "15m",
    end: datetime | None = None,
) -> PinnedLakeSlice | None:
    """Load recent feature-parquet rows for a pinned anchor symbol."""
    sym = str(symbol or "").strip().upper()
    if sym not in PINNED_SYMBOLS:
        return None
    end_dt = end or datetime.now(UTC)
    span_h = hours if hours is not None else _default_window_hours(sym)
    start_dt = end_dt - timedelta(hours=max(1, span_h))
    start_ts = start_dt.isoformat()
    end_ts = end_dt.isoformat()
    try:
        df = read_features(sym, start_ts, end_ts, tf=tf)
    except (LakeDataError, DEFENSIVE_EXC, ValueError):
        return None
    if df.is_empty():
        return None
    return PinnedLakeSlice(
        symbol=sym,
        tf=tf,
        start_ts=start_ts,
        end_ts=end_ts,
        rows=df.height,
        frame=df,
    )


def lake_summary_dict(slice_: PinnedLakeSlice | None) -> dict[str, Any]:
    if slice_ is None or slice_.frame.is_empty():
        return {}
    df = slice_.frame
    out: dict[str, Any] = {
        "symbol": slice_.symbol,
        "tf": slice_.tf,
        "rows": slice_.rows,
        "start_ts": slice_.start_ts,
        "end_ts": slice_.end_ts,
    }
    for col in ("price", "rsi14", "adx14", "atr_pct", "volume_ratio20"):
        if col not in df.columns:
            continue
        try:
            series = df[col].drop_nulls()
            if series.len() == 0:
                continue
            out[f"{col}_last"] = float(series[-1])
            out[f"{col}_mean"] = round(float(series.mean()), 4)
        except (TypeError, ValueError):
            continue
    return out


def attach_lake_to_row(row: dict[str, Any], *, hours: int | None = None) -> dict[str, Any]:
    """Attach pinned lake slice summary onto a probe row (in-place + return row)."""
    sym = str(row.get("symbol") or "").upper()
    slice_ = load_pinned_lake_slice(sym, hours=hours)
    if slice_ is not None:
        row["pinned_lake"] = lake_summary_dict(slice_)
        row["_pinned_lake_frame"] = slice_.frame
    return row





VoteDir = Literal["long", "short", "neutral"]


@dataclass(frozen=True, slots=True)
class CategoryVotes:
    long: int = 0
    short: int = 0
    neutral: int = 0
    total: int = 0


@dataclass(frozen=True, slots=True)
class PinnedIndicatorPanel:
    symbol: str
    long_score: float
    short_score: float
    dominant: VoteDir
    long_votes: int
    short_votes: int
    total_votes: int
    categories: dict[str, CategoryVotes] = field(default_factory=dict)
    evidence: tuple[str, ...] = ()


def _f(snap: dict[str, Any], key: str, default: float = 0.0) -> float:
    try:
        return float(snap.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _vote_rsi(snap: dict[str, Any]) -> VoteDir:
    rsi = normalize_rsi14(_pos_float(snap, "rsi14", 50))
    if rsi <= 35:
        return "long"
    if rsi >= 65:
        return "short"
    return "neutral"


def _vote_ema_stack(snap: dict[str, Any]) -> VoteDir:
    trend = trend_from_snapshot(snap)
    if trend == "bull":
        return "long"
    if trend == "bear":
        return "short"
    return "neutral"


def _vote_adx_di(snap: dict[str, Any]) -> VoteDir:
    adx = _pos_float(snap, "adx14")
    pdi = _pos_float(snap, "plus_di14") or _pos_float(snap, "plus_di")
    mdi = _pos_float(snap, "minus_di14") or _pos_float(snap, "minus_di")
    if adx < ADX_PANEL_NEUTRAL:
        return "neutral"
    if pdi > mdi * 1.1:
        return "long"
    if mdi > pdi * 1.1:
        return "short"
    return "neutral"


def _vote_macd(snap: dict[str, Any]) -> VoteDir:
    h = _pos_float(snap, "macd_hist")
    if h > 0:
        return "long"
    if h < 0:
        return "short"
    return "neutral"


def _vote_stoch(snap: dict[str, Any]) -> VoteDir:
    k = _pos_float(snap, "stoch_k") or _pos_float(snap, "stoch_k14", 50)
    if k <= 25:
        return "long"
    if k >= 75:
        return "short"
    return "neutral"


def _vote_mfi_cci(snap: dict[str, Any]) -> VoteDir:
    mfi = _pos_float(snap, "mfi14", 50)
    cci = _pos_float(snap, "cci20")
    if mfi <= 30 or cci <= -100:
        return "long"
    if mfi >= 70 or cci >= 100:
        return "short"
    return "neutral"


def _vote_supertrend(snap: dict[str, Any]) -> VoteDir:
    st = snap.get("supertrend_dir")
    if st == 1 or st == 1.0:
        return "long"
    if st == -1 or st == -1.0:
        return "short"
    return "neutral"


def _vote_volume(snap: dict[str, Any]) -> VoteDir:
    obv = snap.get("obv_rising")
    if obv is True:
        return "long"
    if obv is False:
        return "short"
    adosc = _pos_float(snap, "adosc_3_10")
    if adosc > 0:
        return "long"
    if adosc < 0:
        return "short"
    return "neutral"


def _vote_volatility(snap: dict[str, Any]) -> VoteDir:
    bb = _pos_float(snap, "bb_pct_b", 0.5)
    if bb <= 0.15:
        return "long"
    if bb >= 0.85:
        return "short"
    return "neutral"


def _vote_momentum_ext(snap: dict[str, Any]) -> VoteDir:
    trix = _pos_float(snap, "trix14")
    mtm = _pos_float(snap, "mtm12")
    psy = _pos_float(snap, "psy12", 50)
    score = 0
    if trix > 0:
        score += 1
    elif trix < 0:
        score -= 1
    if mtm > 0:
        score += 1
    elif mtm < 0:
        score -= 1
    if psy >= 60:
        score += 1
    elif psy <= 40:
        score -= 1
    if score >= 2:
        return "long"
    if score <= -2:
        return "short"
    return "neutral"


def _vote_wq(snap: dict[str, Any]) -> VoteDir:
    rank = _pos_float(snap, "wq_ts_rank_close20", 0.5)
    if rank >= 0.8:
        return "long"
    if rank <= 0.2:
        return "short"
    return "neutral"


_VOTERS: tuple[tuple[str, Any], ...] = (
    ("trend", _vote_ema_stack),
    ("trend", _vote_adx_di),
    ("trend", _vote_supertrend),
    ("momentum", _vote_rsi),
    ("momentum", _vote_macd),
    ("momentum", _vote_momentum_ext),
    ("oscillator", _vote_stoch),
    ("oscillator", _vote_mfi_cci),
    ("volume", _vote_volume),
    ("volatility", _vote_volatility),
    ("regime", _vote_wq),
)


def build_pinned_indicator_panel(
    symbol: str,
    timeframes: dict[str, Any],
    *,
    primary_tf: str = "4h",
    confirm_tf: str = "15m",
) -> PinnedIndicatorPanel:
    """Aggregate indicator votes from primary + confirm TF snapshots."""
    snaps: list[dict[str, Any]] = []
    for key in (primary_tf, confirm_tf, "1h", "1d"):
        raw = resolve_tf_snap(timeframes, key, prefer_closed=True)
        if raw.get("status") != "empty" and raw.get("close"):
            snaps.append(raw)
    if not snaps:
        return PinnedIndicatorPanel(
            symbol=symbol,
            long_score=0.0,
            short_score=0.0,
            dominant="neutral",
            long_votes=0,
            short_votes=0,
            total_votes=0,
        )

    cats: dict[str, CategoryVotes] = {}
    long_v = short_v = neut_v = 0
    evidence: list[str] = []

    for cat, voter in _VOTERS:
        cv = cats.setdefault(cat, CategoryVotes())
        votes_this: list[VoteDir] = []
        for snap in snaps:
            try:
                votes_this.append(voter(snap))
            except (TypeError, ValueError, KeyError):
                votes_this.append("neutral")
        # majority across TFs for this voter
        l = votes_this.count("long")
        s = votes_this.count("short")
        if l > s:
            vote: VoteDir = "long"
        elif s > l:
            vote = "short"
        else:
            vote = "neutral"
        if vote == "long":
            long_v += 1
            cats[cat] = CategoryVotes(cv.long + 1, cv.short, cv.neutral, cv.total + 1)
        elif vote == "short":
            short_v += 1
            cats[cat] = CategoryVotes(cv.long, cv.short + 1, cv.neutral, cv.total + 1)
        else:
            neut_v += 1
            cats[cat] = CategoryVotes(cv.long, cv.short, cv.neutral + 1, cv.total + 1)

    total = long_v + short_v + neut_v
    long_score = long_v / total if total else 0.0
    short_score = short_v / total if total else 0.0
    if long_score >= short_score + 0.12:
        dominant: VoteDir = "long"
        evidence.append(f"консенсус {long_v}/{total} long")
    elif short_score >= long_score + 0.12:
        dominant = "short"
        evidence.append(f"консенсус {short_v}/{total} short")
    else:
        dominant = "neutral"
        evidence.append(f"нейтрально {neut_v}/{total}")

    return PinnedIndicatorPanel(
        symbol=symbol,
        long_score=round(long_score, 3),
        short_score=round(short_score, 3),
        dominant=dominant,
        long_votes=long_v,
        short_votes=short_v,
        total_votes=total,
        categories=cats,
        evidence=tuple(evidence),
    )


def panel_to_dict(panel: Any | None) -> dict[str, Any] | None:
    if panel is None:
        return None
    if isinstance(panel, dict):
        return dict(panel)
    try:
        return {
            "dominant": getattr(panel, "dominant", None),
            "long_votes": int(getattr(panel, "long_votes", 0)),
            "short_votes": int(getattr(panel, "short_votes", 0)),
            "total_votes": int(getattr(panel, "total_votes", 0)),
            "long_score": float(getattr(panel, "long_score", 0)),
            "short_score": float(getattr(panel, "short_score", 0)),
            "evidence": list(getattr(panel, "evidence", ()) or ()),
        }
    except (TypeError, ValueError, AttributeError):
        return None


def mtf_to_dict(mtf: Any | None) -> dict[str, Any] | None:
    if mtf is None:
        return None
    if isinstance(mtf, dict):
        return dict(mtf)
    try:
        long_s = getattr(mtf, "long_scenario", None)
        short_s = getattr(mtf, "short_scenario", None)
        return {
            "dominant": getattr(mtf, "dominant", None),
            "long_score": float(getattr(long_s, "score", 0)),
            "short_score": float(getattr(short_s, "score", 0)),
        }
    except (TypeError, ValueError, AttributeError):
        return None



VerdictKind = Literal["long", "short", "sideways"]

_SCORE_GAP_MIN = 0.15
_CONFIDENCE_MIN = 0.55
_ADX_SIDEWAYS_MAX = ADX_RANGE_MAX


@dataclass(frozen=True, slots=True)
class PinnedVerdict:
    kind: VerdictKind
    confidence: float
    reason: str
    long_scenario: ScenarioScore
    short_scenario: ScenarioScore
    micro_bias: str = ""
    cvd_note: str = ""
    indicator_panel: PinnedIndicatorPanel | None = None
    liquidity_scenarios: Any | None = None
    poc_level_scenarios: Any | None = None


def _htf_conflict(tf_signals: dict[str, Any]) -> bool:
    trends: list[str] = []
    for key in ("1w", "1d", "4h"):
        sig = tf_signals.get(key)
        if sig is None:
            continue
        t = getattr(sig, "trend", None) or (sig.get("trend") if isinstance(sig, dict) else None)
        if t in {"bull", "bear"}:
            trends.append(str(t))
    if len(trends) < 2:
        return False
    return "bull" in trends and "bear" in trends


def _adx_sideways(tf: dict[str, Any]) -> bool:
    for key in ("4h", "1d"):
        snap = resolve_tf_snap(tf, key, prefer_closed=True) or {}
        adx = float(snap.get("adx14") or 0)
        if 0 < adx < _ADX_SIDEWAYS_MAX:
            return True
    return False


def _cvd_slope_note(row: dict[str, Any]) -> str:
    tf = row.get("timeframes") or {}
    for key in ("1h", "15m"):
        snap = tf.get(key) or {}
        cvd = snap.get("session_cvd") or snap.get("rolling_cvd_24h")
        if cvd is not None:
            try:
                v = float(cvd)
                if v > 0:
                    return f"CVD {key} положительный ({v:+.0f})"
                if v < 0:
                    return f"CVD {key} отрицательный ({v:+.0f})"
            except (TypeError, ValueError):
                pass
    return ""


def _micro_bias(row: dict[str, Any]) -> str:
    ms = row.get("microstructure")
    if ms is None:
        return ""
    if hasattr(ms, "reason_line"):
        return str(ms.reason_line())
    if isinstance(ms, dict):
        return str(ms.get("label") or "")
    return ""


def _panel_conflict(panel: PinnedIndicatorPanel | None, kind: VerdictKind) -> bool:
    if panel is None or panel.total_votes <= 0 or panel.dominant == "neutral":
        return False
    return panel.dominant != kind


def _panel_reason(panel: PinnedIndicatorPanel | None) -> str:
    if panel is None or panel.total_votes <= 0:
        return ""
    return (
        f"индикаторы {panel.long_votes}/{panel.total_votes} long · "
        f"{panel.short_votes}/{panel.total_votes} short"
    )


def _trade_ready(row: dict[str, Any]) -> tuple[bool, str, str | None]:
    """Whether hunt system would allow delivery (confirm + readiness), not MTF alone."""
    from hunt_core.deliver.dispatch import (
        display_readiness_score,
        geometry_block_reason,
        readiness_label_ru,
        readiness_score,
    )

    dump = row.get("dump") or {}
    long_setup = row.get("long") or {}
    if bool(dump.get("confirmed")) or bool(long_setup.get("confirmed")):
        return True, "confirmed", None
    short_r = readiness_score(dump, direction="short")
    long_r = readiness_score(long_setup, direction="long")
    max(short_r, long_r)
    active_setup = dump if short_r >= long_r else long_setup
    active_dir = "short" if short_r >= long_r else "long"
    display = display_readiness_score(
        active_setup, direction=active_dir, row=row
    )
    geo = geometry_block_reason(active_setup, row=row, direction=active_dir)
    if geo and display < 60:
        return False, "geometry_block", f"⚠️ {geo} — вход не рекомендуется"
    lc = str((row.get("lifecycle") or {}).get("phase") or "")
    if display < 45:
        return False, "readiness_low", f"{readiness_label_ru(display)}"
    if display < 60:
        return False, "readiness_prep", f"{readiness_label_ru(display)}"
    if lc in {"no_setup", "exhaustion_watch", "accumulation_watch"}:
        return False, "lifecycle_watch", f"lifecycle {lc} — без входа"
    return False, "await_confirm", "ждём closed-bar confirm"


def _advisory_bias(long_s: ScenarioScore, short_s: ScenarioScore) -> VerdictKind | None:
    if short_s.score >= long_s.score + _SCORE_GAP_MIN and short_s.score >= _CONFIDENCE_MIN:
        return "short"
    if long_s.score >= short_s.score + _SCORE_GAP_MIN and long_s.score >= _CONFIDENCE_MIN:
        return "long"
    return None


def build_pinned_verdict(row: dict[str, Any]) -> PinnedVerdict:
    """Three-way verdict from MTF + indicator panel + microstructure + lifecycle."""
    sym = str(row.get("symbol") or "")
    price = float(row.get("price") or 0)
    tf = row.get("timeframes") or {}

    from hunt_core.analysis.deep_signal import build_liquidity_scenarios
    from hunt_core.analysis.deep_signal import build_poc_level_scenarios

    liq_pack = build_liquidity_scenarios(row)
    row["liquidity_scenarios"] = liq_pack
    poc_pack = build_poc_level_scenarios(row)

    panel_raw = row.get("indicator_panel")
    if isinstance(panel_raw, PinnedIndicatorPanel):
        panel = panel_raw
    else:
        panel = build_pinned_indicator_panel(sym, tf) if sym and tf else None

    mtf = build_mtf_confluence(
        sym, tf, price, market=row.get("market"), row=row
    )
    row["mtf"] = mtf

    long_s = mtf.long_scenario
    short_s = mtf.short_scenario
    gap = abs(long_s.score - short_s.score)
    cvd_note = _cvd_slope_note(row)
    micro = _micro_bias(row)
    htf_conflict = _htf_conflict(mtf.tf_signals)
    adx_flat = _adx_sideways(tf)

    reasons_sideways: list[str] = []
    if gap < _SCORE_GAP_MIN:
        reasons_sideways.append(f"scores близки ({long_s.score:.2f} ≈ {short_s.score:.2f})")
    if htf_conflict:
        reasons_sideways.append("HTF расходятся")
    if adx_flat:
        reasons_sideways.append("ADX < 20 — боковик")
    lc_phase = str((row.get("lifecycle") or {}).get("phase") or "")
    if lc_phase in {"no_setup", "accumulation_watch", "exhaustion_watch"}:
        reasons_sideways.append(f"lifecycle: {lc_phase}")
    if panel and panel.dominant == "neutral" and panel.total_votes >= 6:
        reasons_sideways.append(f"индикаторы нейтральны ({panel.total_votes} голосов)")

    panel_note = _panel_reason(panel)
    trade_ok, _ready_code, ready_note = _trade_ready(row)
    advisory = _advisory_bias(long_s, short_s)

    if not trade_ok:
        bias_note = ""
        if advisory == "short":
            bias_note = f"HTF bias SHORT (score {short_s.score:.2f}) — advisory only"
        elif advisory == "long":
            bias_note = f"HTF bias LONG (score {long_s.score:.2f}) — advisory only"
        reason_parts = [p for p in [ready_note, bias_note, panel_note] if p]
        reason = " · ".join(reason_parts) if reason_parts else "нет confirm"
        return PinnedVerdict(
            kind="sideways",
            confidence=round(max(long_s.score, short_s.score), 3),
            reason=reason,
            long_scenario=long_s,
            short_scenario=short_s,
            micro_bias=micro,
            cvd_note=cvd_note,
            indicator_panel=panel,
            liquidity_scenarios=liq_pack,
            poc_level_scenarios=poc_pack,
        )

    if reasons_sideways and max(long_s.score, short_s.score) < _CONFIDENCE_MIN + 0.1:
        reason = " · ".join(reasons_sideways)
        if panel_note:
            reason = f"{reason} · {panel_note}"
        return PinnedVerdict(
            kind="sideways",
            confidence=round(max(long_s.score, short_s.score), 3),
            reason=reason,
            long_scenario=long_s,
            short_scenario=short_s,
            micro_bias=micro,
            cvd_note=cvd_note,
            indicator_panel=panel,
            liquidity_scenarios=liq_pack,
            poc_level_scenarios=poc_pack,
        )

    if long_s.score >= short_s.score + _SCORE_GAP_MIN and long_s.score >= _CONFIDENCE_MIN:
        if not htf_conflict or long_s.htf_count >= short_s.htf_count:
            if not _panel_conflict(panel, "long"):
                reason = f"HTF {long_s.htf_count}/{long_s.htf_total} · score {long_s.score:.2f}"
                if panel_note:
                    reason = f"{reason} · {panel_note}"
                return PinnedVerdict(
                    kind="long",
                    confidence=long_s.score,
                    reason=reason,
                    long_scenario=long_s,
                    short_scenario=short_s,
                    micro_bias=micro,
                    cvd_note=cvd_note,
                    indicator_panel=panel,
                    liquidity_scenarios=liq_pack,
                    poc_level_scenarios=poc_pack,
                )
            reasons_sideways.append("индикаторы против лонга")

    if short_s.score >= long_s.score + _SCORE_GAP_MIN and short_s.score >= _CONFIDENCE_MIN:
        if not htf_conflict or short_s.htf_count >= long_s.htf_count:
            if not _panel_conflict(panel, "short"):
                reason = f"HTF {short_s.htf_count}/{short_s.htf_total} · score {short_s.score:.2f}"
                if panel_note:
                    reason = f"{reason} · {panel_note}"
                return PinnedVerdict(
                    kind="short",
                    confidence=short_s.score,
                    reason=reason,
                    long_scenario=long_s,
                    short_scenario=short_s,
                    micro_bias=micro,
                    cvd_note=cvd_note,
                    indicator_panel=panel,
                    liquidity_scenarios=liq_pack,
                    poc_level_scenarios=poc_pack,
                )
            reasons_sideways.append("индикаторы против шорта")

    reason = reasons_sideways[0] if reasons_sideways else "нет доминанты"
    if panel_note:
        reason = f"{reason} · {panel_note}"
    return PinnedVerdict(
        kind="sideways",
        confidence=round(max(long_s.score, short_s.score), 3),
        reason=reason,
        long_scenario=long_s,
        short_scenario=short_s,
        micro_bias=micro,
        cvd_note=cvd_note,
        indicator_panel=panel,
        liquidity_scenarios=liq_pack,
        poc_level_scenarios=poc_pack,
    )


ScenarioKind = Literal["range", "long", "short", "watch"]


@dataclass(frozen=True, slots=True)
class PinnedScenario:
    kind: ScenarioKind
    confidence: float
    label_ru: str
    action_ru: str
    entry_lo: float | None
    entry_hi: float | None
    stop_loss: float | None
    tp1: float | None
    tp2: float | None
    invalidation_ru: str
    factors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PinnedScenarioPack:
    primary: PinnedScenario
    alternate: PinnedScenario | None
    range_bounds: tuple[float, float] | None
    verdict_kind: str
    context_ru: str

    def to_dict(self) -> dict[str, Any]:
        def _sc(s: PinnedScenario) -> dict[str, Any]:
            return {
                "kind": s.kind,
                "confidence": s.confidence,
                "label": s.label_ru,
                "action": s.action_ru,
                "entry_lo": s.entry_lo,
                "entry_hi": s.entry_hi,
                "stop_loss": s.stop_loss,
                "tp1": s.tp1,
                "tp2": s.tp2,
                "invalidation": s.invalidation_ru,
                "factors": list(s.factors),
            }

        return {
            "primary": _sc(self.primary),
            "alternate": _sc(self.alternate) if self.alternate else None,
            "range_bounds": list(self.range_bounds) if self.range_bounds else None,
            "verdict_kind": self.verdict_kind,
            "context": self.context_ru,
        }


def _pos_float(value: Any) -> float | None:
    try:
        v = float(value)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def _range_scenario(
    price: float,
    *,
    support: float | None,
    resistance: float | None,
    adx: float,
    factors: list[str],
) -> PinnedScenario:
    lo = support or price * 0.98
    hi = resistance or price * 1.02
    if lo > hi:
        lo, hi = hi, lo
    mid = (lo + hi) / 2.0
    return PinnedScenario(
        kind="range",
        confidence=0.55 if adx < ADX_RANGE_MAX else 0.45,
        label_ru="Боковик / диапазон",
        action_ru=f"Торговать границы {lo:.4g}–{hi:.4g} · без пробоя не лезть",
        entry_lo=round(lo, 6),
        entry_hi=round(hi, 6),
        stop_loss=round(lo * 0.995 if price > mid else hi * 1.005, 6),
        tp1=round(mid, 6),
        tp2=round(hi if price <= mid else lo, 6),
        invalidation_ru=f"Закрытие {'ниже' if price > mid else 'выше'} границы диапазона",
        factors=tuple(factors),
    )


def _levels_from_setup(setup: dict[str, Any], *, price: float, kind: str) -> dict[str, Any]:
    ez = setup.get("entry_zone") or [price, price]
    try:
        entry = float(ez[0])
    except (TypeError, ValueError, IndexError):
        entry = price
    return {
        "entry": entry,
        "stop_loss": setup.get("stop_loss"),
        "tp1": setup.get("tp1"),
        "tp2": setup.get("tp2"),
        "invalidation_above": setup.get("invalidation_above"),
        "invalidation_below": setup.get("invalidation_below"),
    }


def _directional_scenario(
    *,
    kind: Literal["long", "short"],
    price: float,
    levels: dict[str, Any],
    confidence: float,
    factors: list[str],
) -> PinnedScenario:
    entry = _pos_float(levels.get("entry")) or price
    sl = _pos_float(levels.get("stop_loss"))
    tp1 = _pos_float(levels.get("tp1"))
    tp2 = _pos_float(levels.get("tp2"))
    inv = _pos_float(levels.get("invalidation_above") or levels.get("invalidation_below"))
    if kind == "long":
        label = "Лонг от поддержки"
        action = "Limit на retest поддержки / VAL"
        inv_ru = f"Инвалидация ниже {inv:.4g}" if inv else "Закрытие ниже stop-loss"
        ez_lo = entry * 0.998 if entry else None
        ez_hi = entry * 1.002 if entry else None
    else:
        label = "Шорт от сопротивления"
        action = "Limit на retest сопротивления / VAH"
        inv_ru = f"Инвалидация выше {inv:.4g}" if inv else "Закрытие выше stop-loss"
        ez_lo = entry * 0.998 if entry else None
        ez_hi = entry * 1.002 if entry else None
    return PinnedScenario(
        kind=kind,
        confidence=confidence,
        label_ru=label,
        action_ru=action,
        entry_lo=round(ez_lo, 6) if ez_lo else None,
        entry_hi=round(ez_hi, 6) if ez_hi else None,
        stop_loss=round(sl, 6) if sl else None,
        tp1=round(tp1, 6) if tp1 else None,
        tp2=round(tp2, 6) if tp2 else None,
        invalidation_ru=inv_ru,
        factors=tuple(factors),
    )


def build_pinned_scenario(row: dict[str, Any], *, attach_lake: bool = True) -> PinnedScenarioPack:
    """Range OR long/short scenario from structural levels + MTF verdict."""
    sym = str(row.get("symbol") or "").upper()
    if attach_lake and is_pinned_symbol(sym):
        attach_lake_to_row(row)

    price = _pos_float(row.get("price")) or 0.0
    tf = row.get("timeframes") or {}
    snap_4h = resolve_tf_snap(tf, "4h", prefer_closed=True) or {}
    snap_1h = resolve_tf_snap(tf, "1h", prefer_closed=True) or {}
    adx = float(snap_4h.get("adx14") or snap_1h.get("adx14") or 0)

    support = _pos_float((row.get("session") or {}).get("low_24h"))
    resistance = _pos_float((row.get("session") or {}).get("high_24h"))
    market = row.get("market") or {}
    poc = _pos_float(market.get("poc_1h") or market.get("poc"))
    vah = _pos_float(market.get("vah_1h"))
    val = _pos_float(market.get("val_1h"))
    if val:
        support = min(support or val, val) if support else val
    if vah:
        resistance = max(resistance or vah, vah) if resistance else vah
    if poc and price > 0:
        if not support or poc < price:
            support = support or poc
        if not resistance or poc > price:
            resistance = resistance or poc

    verdict = build_pinned_verdict(row)
    factors: list[str] = []
    if adx > 0 and adx < ADX_RANGE_MAX:
        factors.append(f"ADX4h={adx:.0f}<20")
    if verdict.reason:
        factors.append(verdict.reason[:80])

    range_bounds: tuple[float, float] | None = None
    if support and resistance and resistance > support:
        range_bounds = (round(support, 6), round(resistance, 6))

    use_range = (
        verdict.kind == "sideways"
        or (adx > 0 and adx < ADX_RANGE_MAX and verdict.kind not in {"long", "short"})
        or (
            verdict.kind in {"long", "short"}
            and abs(verdict.long_scenario.score - verdict.short_scenario.score) < 0.12
        )
    )

    if use_range and range_bounds:
        primary = _range_scenario(price, support=support, resistance=resistance, adx=adx, factors=factors)
        alternate = None
        if verdict.kind == "long":
            long_setup = row.get("long") or {}
            alternate = _directional_scenario(
                kind="long",
                price=price,
                levels=_levels_from_setup(long_setup, price=price, kind="long"),
                confidence=verdict.confidence,
                factors=["alt long"],
            )
        elif verdict.kind == "short":
            dump = row.get("dump") or {}
            alternate = _directional_scenario(
                kind="short",
                price=price,
                levels=_levels_from_setup(dump, price=price, kind="short"),
                confidence=verdict.confidence,
                factors=["alt short"],
            )
        ctx = "Структурный диапазон · вход только от границ"
    elif verdict.kind == "long":
        long_setup = row.get("long") or {}
        primary = _directional_scenario(
            kind="long",
            price=price,
            levels=_levels_from_setup(long_setup, price=price, kind="long"),
            confidence=verdict.confidence,
            factors=factors or ["HTF long bias"],
        )
        alternate = _range_scenario(price, support=support, resistance=resistance, adx=adx, factors=["alt range"]) if range_bounds else None
        ctx = verdict.reason or "Лонг-сценарий от структурных уровней"
    elif verdict.kind == "short":
        dump = row.get("dump") or {}
        primary = _directional_scenario(
            kind="short",
            price=price,
            levels=_levels_from_setup(dump, price=price, kind="short"),
            confidence=verdict.confidence,
            factors=factors or ["HTF short bias"],
        )
        alternate = _range_scenario(price, support=support, resistance=resistance, adx=adx, factors=["alt range"]) if range_bounds else None
        ctx = verdict.reason or "Шорт-сценарий от структурных уровней"
    else:
        primary = _range_scenario(
            price,
            support=support,
            resistance=resistance,
            adx=adx,
            factors=factors or ["нет доминанты"],
        )
        alternate = None
        ctx = verdict.reason or "Наблюдение — жди confirm"

    return PinnedScenarioPack(
        primary=primary,
        alternate=alternate,
        range_bounds=range_bounds,
        verdict_kind=verdict.kind,
        context_ru=ctx,
    )


def format_pinned_scenario_telegram(pack: PinnedScenarioPack | dict[str, Any]) -> str:
    """HTML block for /analyze Telegram reply."""
    import html

    if isinstance(pack, dict):
        primary = pack.get("primary") or {}
        alt = pack.get("alternate")
        ctx = str(pack.get("context") or "")
        rb = pack.get("range_bounds")
    else:
        primary = pack.primary
        alt = pack.alternate
        ctx = pack.context_ru
        rb = pack.range_bounds

    def _line(sc: Any, *, header: str) -> list[str]:
        if isinstance(sc, dict):
            kind = sc.get("kind", "?")
            label = sc.get("label", kind)
            action = sc.get("action", "")
            conf = sc.get("confidence", 0)
            sl = sc.get("stop_loss")
            tp1 = sc.get("tp1")
            inv = sc.get("invalidation", "")
        else:
            kind, label, action = sc.kind, sc.label_ru, sc.action_ru
            conf, sl, tp1, inv = sc.confidence, sc.stop_loss, sc.tp1, sc.invalidation_ru
        lines = [f"{header} <b>{html.escape(str(label))}</b> ({conf:.2f})"]
        if action:
            lines.append(html.escape(str(action)))
        if sl:
            lines.append(f"SL <code>{sl}</code> · TP1 <code>{tp1 or '—'}</code>")
        if inv:
            lines.append(f"⚠️ {html.escape(str(inv))}")
        return lines

    out = ["📐 <b>СЦЕНАРИЙ</b>", html.escape(ctx)]
    if rb:
        lo, hi = rb if not isinstance(rb, dict) else (rb[0], rb[1])
        out.append(f"Диапазон: <code>{lo}</code> – <code>{hi}</code>")
    out.extend(_line(primary, header="▶"))
    if alt:
        out.append("")
        out.extend(_line(alt, header="↔ Alt"))
    return "\n".join(out)

__all__ = [
    "PinnedLakeSlice",
    "attach_lake_to_row",
    "lake_summary_dict",
    "load_pinned_lake_slice",
    "PinnedScenario",
    "PinnedScenarioPack",
    "ScenarioKind",
    "build_pinned_scenario",
    "format_pinned_scenario_telegram",
]
