"""Unit tests for the fetcher Lambda. Finnhub and CoinGecko HTTP calls are
intercepted via the `responses` library; Kinesis is mocked via moto so no
real network or AWS calls happen."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
import responses
from moto import mock_aws

from lambdas.fetcher import handler


@pytest.fixture(autouse=True)
def _default_env(monkeypatch):
    monkeypatch.setenv("TICKERS", "AAPL,BTC-USD")
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")
    # Reset the lazy module-level Kinesis client between tests so each test
    # gets a fresh client bound to its own moto context.
    handler._kinesis_client = None


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
    assert result == {"fetched": 0, "published": 0, "events": []}


def test_handler_skips_stocks_without_api_key(monkeypatch):
    monkeypatch.setenv("TICKERS", "AAPL")
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)

    result = handler.lambda_handler({}, None)
    assert result == {"fetched": 0, "published": 0, "events": []}


def test_handler_handles_empty_tickers_env(monkeypatch):
    monkeypatch.setenv("TICKERS", "")
    result = handler.lambda_handler({}, None)
    assert result == {"fetched": 0, "published": 0, "events": []}


def test_unknown_crypto_is_skipped_not_routed_to_finnhub(monkeypatch):
    # DOGE-USD ends in -USD so it's classified as crypto, but it's not in
    # COINGECKO_IDS — it should be skipped, NOT silently sent to Finnhub.
    monkeypatch.setenv("TICKERS", "DOGE-USD")
    result = handler.lambda_handler({}, None)
    assert result == {"fetched": 0, "published": 0, "events": []}


def test_is_crypto_classification():
    assert handler._is_crypto("BTC-USD") is True
    assert handler._is_crypto("eth-usd") is True
    assert handler._is_crypto("AAPL") is False
    assert handler._is_crypto("TSLA") is False


# ---------------------------------------------------------------------------
# Kinesis publish path
# ---------------------------------------------------------------------------

STREAM_NAME = "test-stream"


def _read_all_records(client, stream_name: str) -> list[dict]:
    """Helper: drain every record from every shard for assertion."""
    shards = client.list_shards(StreamName=stream_name)["Shards"]
    out = []
    for shard in shards:
        it = client.get_shard_iterator(
            StreamName=stream_name,
            ShardId=shard["ShardId"],
            ShardIteratorType="TRIM_HORIZON",
        )["ShardIterator"]
        out.extend(client.get_records(ShardIterator=it, Limit=100)["Records"])
    return out


@mock_aws
@responses.activate
def test_handler_publishes_events_to_kinesis(monkeypatch):
    monkeypatch.setenv("KINESIS_STREAM_NAME", STREAM_NAME)
    client = boto3.client("kinesis", region_name="us-east-1")
    client.create_stream(StreamName=STREAM_NAME, ShardCount=1)
    _add_finnhub_quote(150.25)
    _add_coingecko(50000.0, 1234567.0)

    result = handler.lambda_handler({}, None)

    assert result["fetched"] == 2
    assert result["published"] == 2

    records = _read_all_records(client, STREAM_NAME)
    assert len(records) == 2
    payloads = {json.loads(r["Data"])["ticker"]: json.loads(r["Data"]) for r in records}
    assert set(payloads) == {"AAPL", "BTC-USD"}
    # PartitionKey == ticker (per F-01 ordering guarantee).
    assert {r["PartitionKey"] for r in records} == {"AAPL", "BTC-USD"}


@responses.activate
def test_handler_skips_publish_when_stream_name_unset(monkeypatch):
    monkeypatch.delenv("KINESIS_STREAM_NAME", raising=False)
    _add_finnhub_quote(150.25)
    _add_coingecko(50000.0, 1234567.0)

    result = handler.lambda_handler({}, None)

    assert result["fetched"] == 2
    assert result["published"] == 0


@responses.activate
def test_publish_retries_throughput_exceeded_then_succeeds(monkeypatch):
    """First put_records: 1 record fails with throughput-exceeded; retry succeeds."""
    monkeypatch.setenv("KINESIS_STREAM_NAME", STREAM_NAME)
    fake_client = MagicMock()
    fake_client.put_records.side_effect = [
        {
            "FailedRecordCount": 1,
            "Records": [
                {"SequenceNumber": "1", "ShardId": "shardId-0"},
                {"ErrorCode": "ProvisionedThroughputExceededException",
                 "ErrorMessage": "slow down"},
            ],
        },
        {"FailedRecordCount": 0, "Records": [{"SequenceNumber": "2", "ShardId": "shardId-0"}]},
    ]
    with patch.object(handler, "_get_kinesis_client", return_value=fake_client), \
            patch.object(handler, "KINESIS_RETRY_BACKOFF_SECS", 0):
        published = handler.publish_to_kinesis(
            [{"ticker": "AAPL", "price": 1.0}, {"ticker": "TSLA", "price": 2.0}],
            STREAM_NAME,
        )
    assert published == 2
    assert fake_client.put_records.call_count == 2
    # The retry call should contain only the failed record (TSLA), not AAPL.
    retry_records = fake_client.put_records.call_args_list[1].kwargs["Records"]
    assert len(retry_records) == 1
    assert retry_records[0]["PartitionKey"] == "TSLA"


def test_publish_drops_non_retryable_errors(monkeypatch):
    monkeypatch.setenv("KINESIS_STREAM_NAME", STREAM_NAME)
    fake_client = MagicMock()
    fake_client.put_records.return_value = {
        "FailedRecordCount": 1,
        "Records": [
            {"ErrorCode": "InternalFailure", "ErrorMessage": "boom"},
        ],
    }
    with patch.object(handler, "_get_kinesis_client", return_value=fake_client):
        published = handler.publish_to_kinesis(
            [{"ticker": "AAPL", "price": 1.0}], STREAM_NAME,
        )
    assert published == 0
    assert fake_client.put_records.call_count == 1  # not retried
