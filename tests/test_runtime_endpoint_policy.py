import pytest

from bot.market_data import validate_runtime_public_rest_url


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
    with pytest.raises(ValueError, match="private/auth"):
        validate_runtime_public_rest_url("https://fapi.binance.com/sapi/v1/accountSnapshot")
