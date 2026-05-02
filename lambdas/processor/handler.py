"""Processor Lambda — consumes Kinesis price events, normalizes them, and
prepares writes to DynamoDB (hot path) + S3 (cold path).

Phase 2 scope: decode + validate + within-batch pct_change + structured
logging. The actual DynamoDB and S3 writes will be wired in Phase 3.

Returns `{"batchItemFailures": [...]}` so partial-batch failures are re-
delivered per record rather than the whole batch (requires
`FunctionResponseTypes=["ReportBatchItemFailures"]` on the event source
mapping; configured by 07-create-event-source-mapping.sh).
"""
from __future__ import annotations

import base64
import json
import logging
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REQUIRED_FIELDS = ("ticker", "price", "timestamp")


def _decode_record(raw: dict) -> dict:
    """Base64-decode + JSON-parse one Kinesis record's payload."""
    data_b64 = raw["kinesis"]["data"]
    return json.loads(base64.b64decode(data_b64))


def _validate(payload: dict) -> None:
    missing = [f for f in REQUIRED_FIELDS if f not in payload]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")
    if not isinstance(payload["price"], (int, float)) or isinstance(payload["price"], bool):
        raise ValueError(f"Invalid price type: {type(payload['price']).__name__}")


def _normalize(payload: dict, prev_price: float | None) -> dict:
    """Build the normalized record. Phase 3 will write this to DynamoDB."""
    price = float(payload["price"])
    pct_change = None
    if prev_price is not None and prev_price != 0:
        pct_change = (price - prev_price) / prev_price * 100
    return {
        "ticker": payload["ticker"],
        "price": price,
        "volume": float(payload.get("volume", 0.0)),
        "timestamp": payload["timestamp"],
        "source": payload.get("source", "unknown"),
        "pct_change": pct_change,
    }


def lambda_handler(event: dict, context: Any) -> dict:
    records = event.get("Records", [])
    logger.info("Processing %d Kinesis record(s)", len(records))

    last_price: dict[str, float] = {}
    failures: list[dict] = []
    processed = 0

    for raw in records:
        seq = raw.get("kinesis", {}).get("sequenceNumber", "?")
        try:
            payload = _decode_record(raw)
            _validate(payload)
        except Exception as e:
            logger.error("Failed to decode record %s: %s", seq, e)
            failures.append({"itemIdentifier": seq})
            continue

        ticker = payload["ticker"]
        normalized = _normalize(payload, last_price.get(ticker))
        last_price[ticker] = normalized["price"]

        # Phase 3 will write `normalized` to DynamoDB and `payload` to S3 here.
        logger.info(
            "Normalized %s @ %s (pct_change=%s)",
            normalized["ticker"], normalized["price"], normalized["pct_change"],
        )
        processed += 1

    logger.info(
        "Processed %d/%d records (%d failures)",
        processed, len(records), len(failures),
    )
    return {"batchItemFailures": failures}
