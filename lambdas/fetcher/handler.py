"""Fetcher Lambda — pulls live prices from Finnhub (stocks) and CoinGecko
(crypto) on a cron trigger and publishes them to a Kinesis Data Stream.

The original PRD calls for yfinance, but Yahoo blocks the AWS Lambda IP ranges
(returns empty 200 responses, mis-reported by yfinance as "possibly delisted").
Finnhub's free /quote endpoint is the closest drop-in: 60 calls/min free, real-
time, JSON. Tradeoff: the free tier doesn't include volume, so stock events
report `volume: 0`.

Kinesis publishing is gated on the KINESIS_STREAM_NAME env var: when unset
(local dev, or Phase 1 smoke test) the handler returns events without
publishing, which keeps it unit-testable without any AWS plumbing.
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import boto3
import requests
from botocore.config import Config

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Crypto tickers are routed to CoinGecko; the symbol → CoinGecko id mapping
# is explicit so we never silently fall through to a stock fetch for an
# unknown symbol. Extend this dict as new crypto tickers are added.
COINGECKO_IDS: dict[str, str] = {
    "BTC-USD": "bitcoin",
    "ETH-USD": "ethereum",
    "SOL-USD": "solana",
    "ADA-USD": "cardano",
}

COINGECKO_URL = "https://api.coingecko.com/api/v3/simple/price"
FINNHUB_QUOTE_URL = "https://finnhub.io/api/v1/quote"
HTTP_TIMEOUT_SECS = 5

# Kinesis publish tuning. put_records is bounded by Kinesis to 500 records or
# 5 MB per call; we're well under both. Retries cover transient
# ProvisionedThroughputExceeded errors only — other failures are logged and
# dropped (Phase 2 acceptable; Phase 7 will add a DLQ for the publish path).
KINESIS_MAX_RETRIES = 3
KINESIS_RETRY_BACKOFF_SECS = 0.2

# Lazy module-level client so cold-start cost is amortized across warm invokes
# but tests that don't touch AWS don't pay it.
_kinesis_client = None


def _get_kinesis_client():
    global _kinesis_client
    if _kinesis_client is None:
        _kinesis_client = boto3.client(
            "kinesis",
            config=Config(
                connect_timeout=3,
                read_timeout=5,
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )
    return _kinesis_client


def publish_to_kinesis(events: list[dict], stream_name: str) -> int:
    """Publish events to Kinesis. Returns the number successfully published.

    PartitionKey is the ticker symbol so all events for the same asset land
    on the same shard, preserving per-asset ordering (F-01 spec). Failures
    from ProvisionedThroughputExceeded are retried with bounded backoff;
    other failures are logged and dropped.
    """
    if not events:
        return 0
    client = _get_kinesis_client()
    pending = [
        {"Data": json.dumps(e).encode("utf-8"), "PartitionKey": e["ticker"]}
        for e in events
    ]
    published = 0
    for attempt in range(KINESIS_MAX_RETRIES + 1):
        resp = client.put_records(StreamName=stream_name, Records=pending)
        published += len(pending) - resp["FailedRecordCount"]
        if resp["FailedRecordCount"] == 0:
            return published
        # Retry only the records that actually failed, preserving order.
        retryable = []
        for record, result in zip(pending, resp["Records"]):
            if "ErrorCode" not in result:
                continue
            if result["ErrorCode"] == "ProvisionedThroughputExceededException" \
                    and attempt < KINESIS_MAX_RETRIES:
                retryable.append(record)
            else:
                logger.error(
                    "Kinesis put failed (dropping): %s %s",
                    result.get("ErrorCode"), result.get("ErrorMessage"),
                )
        if not retryable:
            break
        time.sleep(KINESIS_RETRY_BACKOFF_SECS * (2 ** attempt))
        pending = retryable
    return published


def _is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith("-USD")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch_stock(ticker: str) -> dict | None:
    """Fetch the current quote for a stock ticker via Finnhub."""
    api_key = os.environ.get("FINNHUB_API_KEY")
    if not api_key:
        logger.error("FINNHUB_API_KEY not set; skipping stock %s", ticker)
        return None
    try:
        resp = requests.get(
            FINNHUB_QUOTE_URL,
            params={"symbol": ticker, "token": api_key},
            timeout=HTTP_TIMEOUT_SECS,
        )
        resp.raise_for_status()
        data = resp.json()
        # Finnhub returns {"c": 0, ...} for unknown symbols — treat as miss.
        price = data.get("c")
        if not price:
            logger.warning("Finnhub returned no price for %s", ticker)
            return None
        return {
            "ticker": ticker,
            "price": float(price),
            "volume": 0.0,  # /quote doesn't include volume on the free tier
            "timestamp": _now_iso(),
            "source": "finnhub",
        }
    except Exception:
        logger.exception("Finnhub fetch failed for %s", ticker)
        return None


def fetch_crypto(ticker: str) -> dict | None:
    """Fetch the spot price for a crypto ticker via CoinGecko."""
    cg_id = COINGECKO_IDS.get(ticker)
    if cg_id is None:
        logger.warning("Unknown crypto ticker %s; add it to COINGECKO_IDS", ticker)
        return None
    try:
        resp = requests.get(
            COINGECKO_URL,
            params={"ids": cg_id, "vs_currencies": "usd", "include_24hr_vol": "true"},
            timeout=HTTP_TIMEOUT_SECS,
        )
        resp.raise_for_status()
        data = resp.json().get(cg_id, {})
        if "usd" not in data:
            logger.warning("CoinGecko returned no price for %s", ticker)
            return None
        return {
            "ticker": ticker,
            "price": float(data["usd"]),
            "volume": float(data.get("usd_24h_vol", 0.0)),
            "timestamp": _now_iso(),
            "source": "coingecko",
        }
    except Exception:
        logger.exception("CoinGecko fetch failed for %s", ticker)
        return None


def fetch_one(ticker: str) -> dict | None:
    return fetch_crypto(ticker) if _is_crypto(ticker) else fetch_stock(ticker)


def lambda_handler(event: dict, context: Any) -> dict:
    tickers_env = os.environ.get("TICKERS", "")
    tickers = [t.strip() for t in tickers_env.split(",") if t.strip()]
    if not tickers:
        logger.error("No tickers configured; set TICKERS env var")
        return {"fetched": 0, "published": 0, "events": []}

    events: list[dict] = []
    for ticker in tickers:
        evt = fetch_one(ticker)
        if evt:
            events.append(evt)
            logger.info("Fetched %s @ %s", evt["ticker"], evt["price"])

    stream_name = os.environ.get("KINESIS_STREAM_NAME")
    published = 0
    if stream_name:
        published = publish_to_kinesis(events, stream_name)
        logger.info("Published %d/%d events to %s", published, len(events), stream_name)
    else:
        logger.warning("KINESIS_STREAM_NAME not set; skipping publish")

    return {"fetched": len(events), "published": published, "events": events}
