"""Fetcher Lambda — pulls live prices from Finnhub (stocks) and CoinGecko
(crypto) on a cron trigger.

The original PRD calls for yfinance, but Yahoo blocks the AWS Lambda IP ranges
(returns empty 200 responses, mis-reported by yfinance as "possibly delisted").
Finnhub's free /quote endpoint is the closest drop-in: 60 calls/min free, real-
time, JSON. Tradeoff: the free tier doesn't include volume, so stock events
report `volume: 0`.

Phase 1: build per-ticker price events and return them. Phase 2 will add the
Kinesis publish step; keeping that out for now lets this module be unit-tested
without any AWS dependencies.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

import requests

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
        return {"fetched": 0, "events": []}

    events: list[dict] = []
    for ticker in tickers:
        evt = fetch_one(ticker)
        if evt:
            events.append(evt)
            logger.info("Fetched %s @ %s", evt["ticker"], evt["price"])

    # Phase 2 will publish `events` to Kinesis here.
    return {"fetched": len(events), "events": events}
