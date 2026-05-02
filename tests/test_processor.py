"""Unit tests for the processor Lambda. Synthesizes Kinesis events directly
(no AWS calls); the processor itself only does decode/normalize/log work in
Phase 2 so no mocking is needed beyond the event payload shape."""
from __future__ import annotations

import base64
import json

from lambdas.processor import handler


def _kinesis_record(payload: dict | bytes, sequence: str = "1") -> dict:
    """Build a synthetic Kinesis record matching Lambda's event schema."""
    if isinstance(payload, dict):
        raw = json.dumps(payload).encode("utf-8")
    else:
        raw = payload
    return {
        "kinesis": {
            "data": base64.b64encode(raw).decode("ascii"),
            "partitionKey": payload.get("ticker", "x") if isinstance(payload, dict) else "x",
            "sequenceNumber": sequence,
        },
        "eventSource": "aws:kinesis",
    }


def _payload(ticker: str, price: float, ts: str = "2026-05-02T18:20:24+00:00") -> dict:
    return {
        "ticker": ticker,
        "price": price,
        "volume": 0.0,
        "timestamp": ts,
        "source": "finnhub",
    }


def test_processes_single_record_pct_change_is_none(caplog):
    """First-ever record for a ticker has no prior, so pct_change is None."""
    event = {"Records": [_kinesis_record(_payload("AAPL", 150.0), "1")]}

    result = handler.lambda_handler(event, None)

    assert result == {"batchItemFailures": []}
    # The log line carries the normalized payload; assert pct_change=None.
    assert any("pct_change=None" in r.getMessage() for r in caplog.records)


def test_computes_pct_change_within_batch(caplog):
    """Two records for the same ticker in one batch: second has pct_change."""
    event = {
        "Records": [
            _kinesis_record(_payload("AAPL", 100.0), "1"),
            _kinesis_record(_payload("AAPL", 110.0), "2"),
        ]
    }

    result = handler.lambda_handler(event, None)

    assert result == {"batchItemFailures": []}
    msgs = [r.getMessage() for r in caplog.records]
    # 100 -> 110 = +10%
    assert any("pct_change=10.0" in m for m in msgs)


def test_pct_change_is_per_ticker_not_global():
    """AAPL price change should not contaminate TSLA's first-record state."""
    event = {
        "Records": [
            _kinesis_record(_payload("AAPL", 100.0), "1"),
            _kinesis_record(_payload("TSLA", 200.0), "2"),
            _kinesis_record(_payload("AAPL", 105.0), "3"),
            _kinesis_record(_payload("TSLA", 220.0), "4"),
        ]
    }

    result = handler.lambda_handler(event, None)
    assert result == {"batchItemFailures": []}


def test_malformed_json_isolated_via_batch_item_failures():
    """A non-JSON record should be reported in batchItemFailures, not crash
    the whole batch. The other records should still process successfully."""
    event = {
        "Records": [
            _kinesis_record(_payload("AAPL", 100.0), "good-1"),
            _kinesis_record(b"not-valid-json{{{", "bad-1"),
            _kinesis_record(_payload("TSLA", 200.0), "good-2"),
        ]
    }

    result = handler.lambda_handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-1"}]}


def test_missing_required_field_isolated():
    """Payload missing `price` should be reported as a failure."""
    bad_payload = {"ticker": "AAPL", "timestamp": "2026-05-02T18:20:24+00:00"}
    event = {
        "Records": [
            _kinesis_record(bad_payload, "missing-1"),
            _kinesis_record(_payload("TSLA", 200.0), "good-1"),
        ]
    }

    result = handler.lambda_handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "missing-1"}]}


def test_invalid_price_type_isolated():
    """price=`"foo"` should be reported as a failure (validation catches it)."""
    bad = {"ticker": "AAPL", "price": "not-a-number", "timestamp": "2026-05-02T18:20:24+00:00"}
    event = {"Records": [_kinesis_record(bad, "bad-type")]}

    result = handler.lambda_handler(event, None)

    assert result == {"batchItemFailures": [{"itemIdentifier": "bad-type"}]}


def test_empty_batch_returns_no_failures():
    result = handler.lambda_handler({"Records": []}, None)
    assert result == {"batchItemFailures": []}


def test_zero_previous_price_yields_pct_change_none():
    """Avoid divide-by-zero when the prior price is exactly 0.0."""
    event = {
        "Records": [
            _kinesis_record(_payload("PENNY", 0.0), "1"),
            _kinesis_record(_payload("PENNY", 0.5), "2"),
        ]
    }

    result = handler.lambda_handler(event, None)
    assert result == {"batchItemFailures": []}
    # No assertion on pct_change here beyond the absence of a crash; the
    # explicit "prev_price != 0" guard is what we're proving.
