"""Unit tests for the fetcher Lambda. Finnhub and CoinGecko HTTP calls are
intercepted via the `responses` library so no real network calls happen."""
from __future__ import annotations

import pytest
import responses

from lambdas.fetcher import handler


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.setenv("TICKERS", "AAPL,BTC-USD")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")


def _add_finnhub_quote(price: float):
    responses.add(
        responses.GET,
        handler.FINNHUB_QUOTE_URL,
        json={"c": price, "d": 0.5, "dp": 0.3, "h": price, "l": price, "o": price, "pc": price},
        status=200,
    )


def _add_coingecko(price: float, vol: float):
    responses.add(
        responses.GET,
        handler.COINGECKO_URL,
        json={"bitcoin": {"usd": price, "usd_24h_vol": vol}},
        status=200,
    )


@responses.activate
def test_handler_returns_events_for_stock_and_crypto():
    _add_finnhub_quote(150.25)
    _add_coingecko(50000.0, 1234567.0)

    result = handler.lambda_handler({}, None)

    assert result["fetched"] == 2
    by_ticker = {e["ticker"]: e for e in result["events"]}
    assert set(by_ticker) == {"AAPL", "BTC-USD"}

    aapl = by_ticker["AAPL"]
    assert aapl["source"] == "finnhub"
    assert aapl["price"] == 150.25
    assert aapl["volume"] == 0.0  # free tier doesn't include volume
    assert "timestamp" in aapl

    btc = by_ticker["BTC-USD"]
    assert btc["source"] == "coingecko"
    assert btc["price"] == 50000.0
    assert btc["volume"] == 1234567.0


@responses.activate
def test_handler_continues_on_finnhub_failure():
    responses.add(responses.GET, handler.FINNHUB_QUOTE_URL, status=500)
    _add_coingecko(50000.0, 0.0)

    result = handler.lambda_handler({}, None)

    assert result["fetched"] == 1
    assert result["events"][0]["ticker"] == "BTC-USD"


@responses.activate
def test_handler_continues_on_coingecko_failure():
    _add_finnhub_quote(150.25)
    responses.add(responses.GET, handler.COINGECKO_URL, status=500)

    result = handler.lambda_handler({}, None)

    assert result["fetched"] == 1
    assert result["events"][0]["ticker"] == "AAPL"


@responses.activate
def test_handler_skips_when_finnhub_returns_no_price(monkeypatch):
    monkeypatch.setenv("TICKERS", "AAPL")
    # Finnhub returns c=0 for unknown symbols; treat as a miss, not a hit.
    responses.add(
        responses.GET,
        handler.FINNHUB_QUOTE_URL,
        json={"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0},
        status=200,
    )

    result = handler.lambda_handler({}, None)
    assert result == {"fetched": 0, "events": []}


def test_handler_skips_stocks_without_api_key(monkeypatch):
    monkeypatch.setenv("TICKERS", "AAPL")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)

    result = handler.lambda_handler({}, None)
    assert result == {"fetched": 0, "events": []}


def test_handler_handles_empty_tickers_env(monkeypatch):
    monkeypatch.setenv("TICKERS", "")
    result = handler.lambda_handler({}, None)
    assert result == {"fetched": 0, "events": []}


def test_unknown_crypto_is_skipped_not_routed_to_finnhub(monkeypatch):
    # DOGE-USD ends in -USD so it's classified as crypto, but it's not in
    # COINGECKO_IDS — it should be skipped, NOT silently sent to Finnhub.
    monkeypatch.setenv("TICKERS", "DOGE-USD")
    result = handler.lambda_handler({}, None)
    assert result == {"fetched": 0, "events": []}


def test_is_crypto_classification():
    assert handler._is_crypto("BTC-USD") is True
    assert handler._is_crypto("eth-usd") is True
    assert handler._is_crypto("AAPL") is False
    assert handler._is_crypto("TSLA") is False
