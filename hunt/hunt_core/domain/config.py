"""Hunt runtime settings — standalone, no bot catalog."""
from __future__ import annotations



import os
import tomllib as _toml_lib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from hunt_core.secrets import load_secrets, parse_operator_user_ids
from hunt_core.setups.catalog import HUNT_SETUP_IDS

REQUIRED_PINNED_SYMBOLS: tuple[str, ...] = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "XAUUSDT",
    "XAGUSDT",
    "PAXGUSDT",
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RuntimeConfig(_StrictModel):
    strict_data_quality: bool = True
    shortlist_unified_routing: bool = False
    analysis_kline_intervals: tuple[str, ...] = ("5m", "15m", "1h", "4h", "1d")
    log_level: str = "INFO"
    telemetry_subdir: str = "telemetry"

    @field_validator("analysis_kline_intervals", mode="before")
    @classmethod
    def _normalize_intervals(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ("5m", "15m", "1h", "4h", "1d")
        if isinstance(value, str):
            return (value.strip(),)
        if isinstance(value, (list, tuple, set)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return ("5m", "15m", "1h", "4h", "1d")


class NetworkConfig(_StrictModel):
    proxy_url: str | None = None
    proxy_urls: list[str] = Field(default_factory=list)

    @field_validator("proxy_url")
    @classmethod
    def _normalize_proxy(cls, value: str | None) -> str | None:
        raw = str(value or "").strip()
        return raw or None

    @field_validator("proxy_urls", mode="before")
    @classmethod
    def _normalize_list(cls, value: object) -> list[str]:
        out: list[str] = []
        for raw in value or ():
            item = str(raw or "").strip()
            if item and item not in out:
                out.append(item)
        return out

    def effective_proxy_urls(self) -> list[str]:
        out: list[str] = []
        for raw in (self.proxy_url, *self.proxy_urls):
            item = str(raw or "").strip()
            if item and item not in out:
                out.append(item)
        return out


class FilterConfig(_StrictModel):
    min_score: float = Field(default=0.60, ge=0.0, le=1.0)
    cooldown_minutes: int = Field(default=60, ge=0, le=1440)
    min_bars_15m: int = Field(default=500, ge=30, le=5000)
    min_bars_1h: int = Field(default=300, ge=30, le=5000)
    min_bars_5m: int = Field(default=200, ge=30, le=5000)
    min_bars_4h: int = Field(default=300, ge=30, le=5000)
    setups: dict[str, dict[str, float]] = Field(default_factory=dict)


class DeliveryConfig(_StrictModel):
    watch_min_score: float = Field(default=0.0, ge=0.0, le=1.0)


class TrackingConfig(_StrictModel):
    pending_expiry_minutes: int = Field(default=180, ge=5, le=10080)
    late_entry_chase_pct: float = Field(default=0.002, ge=0.0001, le=0.05)


class NotifierConfig(_StrictModel):
    provider: str = "telegram"


class WSConfig(_StrictModel):
    kline_intervals: tuple[str, ...] = ("5m", "15m", "1h", "4h")

    @field_validator("kline_intervals", mode="before")
    @classmethod
    def _normalize_klines(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            return (value.strip(),)
        if isinstance(value, (list, tuple, set)):
            return tuple(str(item).strip() for item in value if str(item).strip())
        return ("5m", "15m", "1h", "4h")


class AssetConfig(_StrictModel):
    primary_timeframe: Literal["5m", "15m", "1h", "4h"] = "15m"
    context_timeframes: tuple[Literal["5m", "15m", "1h", "4h"], ...] = ("1h", "4h")
    excluded_strategies: tuple[str, ...] = ()
    allowed_strategies: tuple[str, ...] = ()
    deep_analysis: bool = False


class SetupConfig(_StrictModel):
    dump_initiation: bool = True
    squeeze_expansion: bool = True
    liquidity_sweep: bool = True
    bos_choch: bool = True
    value_accept_reject: bool = True
    oi_cascade: bool = True
    accumulation_breakout: bool = True

    def enabled_setup_ids(self) -> tuple[str, ...]:
        return tuple(sid for sid in HUNT_SETUP_IDS if bool(getattr(self, sid, False)))


class HuntSettings(_StrictModel):
    tg_token: str = ""
    target_chat_id: str = ""
    operator_user_ids: tuple[int, ...] = ()
    data_dir: Path = Path("data") / "bot"
    config_path: Path = Path("config.toml")
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    filters: FilterConfig = Field(default_factory=FilterConfig)
    tracking: TrackingConfig = Field(default_factory=TrackingConfig)
    setups: SetupConfig = Field(default_factory=SetupConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    ws: WSConfig = Field(default_factory=WSConfig)
    delivery: DeliveryConfig = Field(default_factory=DeliveryConfig)
    notifiers: NotifierConfig = Field(default_factory=NotifierConfig)
    assets: dict[str, AssetConfig] = Field(default_factory=dict)

    @property
    def telemetry_dir(self) -> Path:
        return self.data_dir / self.runtime.telemetry_subdir

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @field_validator("operator_user_ids", mode="before")
    @classmethod
    def _normalize_ops(cls, value: object) -> tuple[int, ...]:
        if value is None:
            return ()
        if isinstance(value, int):
            return (value,)
        if isinstance(value, str):
            return parse_operator_user_ids(value)
        if isinstance(value, (list, tuple, set)):
            ids: list[int] = []
            for item in value:
                try:
                    ids.append(int(item))
                except (TypeError, ValueError):
                    continue
            return tuple(sorted(set(ids)))
        return ()

    @model_validator(mode="after")
    def _normalize_assets(self) -> HuntSettings:
        self.assets = {
            str(symbol).strip().upper(): config for symbol, config in self.assets.items()
        }
        return self

    def validate_for_runtime(self, *, require_telegram: bool) -> None:
        unknown = sorted(set(self.filters.setups) - set(HUNT_SETUP_IDS))
        if unknown:
            raise ValueError(f"unknown setup overrides: {unknown}")
        unknown_enabled = sorted(set(self.setups.enabled_setup_ids()) - set(HUNT_SETUP_IDS))
        if unknown_enabled:
            raise ValueError(f"unknown enabled setups: {unknown_enabled}")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if require_telegram:
            if not self.tg_token.strip():
                raise ValueError("TELEGRAM_BOT_TOKEN is required for runtime")
            if not self.target_chat_id.strip():
                raise ValueError("TELEGRAM_CHAT_ID is required for runtime")


# Back-compat alias used by schemas and data_readiness
BotSettings = HuntSettings


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        parsed = _toml_lib.load(handle)
    return parsed if isinstance(parsed, dict) else {}


def _resolve_config_source(config_file: Path) -> Path:
    if config_file.exists():
        return config_file
    example = config_file.with_name("config.toml.example")
    if example.exists():
        return example
    return config_file


def _convert_toml_dict(d: dict[Any, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for k, v in d.items():
        key = k.decode() if isinstance(k, bytes) else str(k)
        if isinstance(v, dict):
            result[key] = _convert_toml_dict(v)
        elif isinstance(v, list):
            result[key] = [_convert_toml_dict(i) if isinstance(i, dict) else i for i in v]
        else:
            result[key] = v
    return result


def _merge_hunt_defaults(payload: dict[str, Any], hunt_defaults: Mapping[str, Any]) -> None:
    pinned = hunt_defaults.get("pinned", {}).get("defaults") if isinstance(hunt_defaults.get("pinned"), dict) else None
    if not isinstance(pinned, dict):
        pinned = hunt_defaults.get("pinned.defaults")
    if isinstance(pinned, dict):
        assets = payload.setdefault("assets", {})
        if not isinstance(assets, dict):
            assets = {}
            payload["assets"] = assets
        symbols = pinned.get("symbols") or []
        deep = pinned.get("deep_analysis") if isinstance(pinned.get("deep_analysis"), dict) else {}
        modes = pinned.get("modes") if isinstance(pinned.get("modes"), dict) else {}
        for sym in symbols:
            s = str(sym).strip().upper()
            if not s:
                continue
            block = assets.setdefault(s, {})
            if isinstance(block, dict):
                if s in modes:
                    block.setdefault("primary_timeframe", "15m")
                if deep.get(s):
                    block["deep_analysis"] = True


def load_settings(config_path: str | Path = "config.toml") -> HuntSettings:
    config_file = Path(config_path)
    resolved = _resolve_config_source(config_file)
    parsed = _load_toml(resolved)
    bot_raw = parsed.get("bot") if isinstance(parsed.get("bot"), dict) else {}
    payload = _convert_toml_dict(cast("dict[Any, Any]", bot_raw))
    secrets = load_secrets()
    payload["tg_token"] = secrets.tg_token
    payload["target_chat_id"] = secrets.target_chat_id
    payload["operator_user_ids"] = list(secrets.operator_user_ids)
    payload["config_path"] = resolved
    payload.setdefault("data_dir", Path("data") / "bot")

    network_payload = payload.setdefault("network", {})
    if isinstance(network_payload, dict):
        env_proxy = str(os.getenv("BINANCE_PROXY_URL", "") or "").strip()
        if env_proxy:
            network_payload["proxy_url"] = env_proxy
        env_list = str(os.getenv("BINANCE_PROXY_URLS", "") or "").strip()
        if env_list:
            network_payload["proxy_urls"] = [x.strip() for x in env_list.split(",") if x.strip()]

    hunt_defaults_path = Path(__file__).resolve().parents[2] / "config.defaults.toml"
    if hunt_defaults_path.is_file():
        _merge_hunt_defaults(payload, _load_toml(hunt_defaults_path))

    return HuntSettings.model_validate(payload)


__all__ = [
    "AssetConfig",
    "BotSettings",
    "FilterConfig",
    "HuntSettings",
    "NetworkConfig",
    "REQUIRED_PINNED_SYMBOLS",
    "RuntimeConfig",
    "SetupConfig",
    "load_settings",
]
