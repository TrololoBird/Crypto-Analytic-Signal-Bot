import pytest

from bot.domain.config import BotSettings
from bot.market_data import validate_runtime_public_rest_url
from bot.public_intelligence import PublicIntelligenceService
from bot.websocket import subscriptions as ws_subscriptions


def test_runtime_boundary_allows_usdm_public_endpoint() -> None:
    validate_runtime_public_rest_url("https://fapi.binance.com/fapi/v1/exchangeInfo")


@pytest.mark.parametrize(
    "url",
    [
        "https://eapi.binance.com/eapi/v1/exchangeInfo",
        "https://dapi.binance.com/dapi/v1/exchangeInfo",
        "https://vapi.binance.com/vapi/v1/exchangeInfo",
        "https://api.binance.com/api/v3/ticker/price",
    ],
)
def test_runtime_boundary_rejects_non_usdm_hosts(url: str) -> None:
    with pytest.raises(ValueError, match="USD"):
        validate_runtime_public_rest_url(url)


def test_runtime_boundary_rejects_private_routes() -> None:
    with pytest.raises(ValueError, match="endpoint paths|private/auth"):
        validate_runtime_public_rest_url(
            "https://fapi.binance.com/sapi/v1/accountSnapshot"
        )


def test_runtime_boundary_rejects_non_public_usdm_paths() -> None:
    with pytest.raises(ValueError, match="endpoint paths"):
        validate_runtime_public_rest_url(
            "https://fapi.binance.com/eapi/v1/openInterest"
        )


def test_runtime_boundary_rejects_registered_private_fapi_path() -> None:
    with pytest.raises(ValueError, match="endpoint paths"):
        validate_runtime_public_rest_url("https://fapi.binance.com/fapi/v1/order")


def test_ws_stream_endpoint_class_rejects_unknown_streams() -> None:
    with pytest.raises(ValueError, match="unsupported public websocket stream"):
        ws_subscriptions.stream_endpoint_class("btcusdt@balance")


@pytest.mark.asyncio
async def test_public_intelligence_runtime_never_calls_eapi_fetchers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = BotSettings(tg_token="0" * 30, target_chat_id="0")
    service = PublicIntelligenceService(
        settings, market_data=object(), telemetry=object()
    )

    async def _unexpected_call(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("runtime path must not call eAPI fetchers")

    monkeypatch.setattr(service, "_fetch_options_exchange_info", _unexpected_call)
    monkeypatch.setattr(service, "_fetch_options_open_interest", _unexpected_call)
    monkeypatch.setattr(service, "_fetch_options_mark_rows", _unexpected_call)

    snapshot = await service._build_options_snapshot()
    assert snapshot["enabled"] is False
