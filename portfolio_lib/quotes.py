"""Fetch quote data: regular hours from Finnhub, pre/post-market from yfinance.

Design notes:

- **Finnhub for regular session** because it has a clean REST API, published
  rate limits, and explicit prev_close/change/changePercent fields. We never
  compute the daily % move ourselves — Finnhub gives us `dp` directly. (This
  is what fixes the "alarming wrong % move" bug from the agent version: the
  agent was inventing the number, often by grabbing 52-week or YTD figures
  from news context. Here it comes from the provider, end of story.)

- **yfinance for extended hours** because Finnhub's free tier doesn't expose
  pre/post-market prices. We use the same Ticker.info approach as the existing
  fetch_extended.py but wrap it with the rate limiter and isolate per-symbol
  failures.

- **Lookup failures are first-class.** Finnhub returns `{c: 0, d: 0, dp: 0}`
  for invalid symbols rather than a 404. We detect this (all zeros = invalid)
  and return a Quote with `status="lookup_failed"` and `daily_change_pct=0.0`.
  No None values reach downstream code — Streamlit never sees a null where it
  expects a number.

- **Extended-hours fetch is skipped during regular hours**, per SKILL.md Step 3.
  The orchestrator inspects market_status.session and only calls yfinance when
  not in regular hours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import requests

from portfolio_lib.config import (
    FINNHUB_LIMITS,
    YFINANCE_LIMITS,
    YFINANCE_SYMBOL_OVERRIDES,
    require_finnhub_key,
)
from portfolio_lib.market import ET, MarketStatus, now
from portfolio_lib.ratelimit import RateLimiter

logger = logging.getLogger(__name__)

FINNHUB_BASE_URL = "https://finnhub.io/api/v1"


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

QuoteStatus = Literal["ok", "stale", "lookup_failed"]


@dataclass(frozen=True)
class Quote:
    """A unified quote covering regular hours + optional extended hours.

    All numeric fields are concrete floats. Extended-hours fields are None
    only when the extended fetch wasn't run (regular hours) or when yfinance
    returned no data for that session. They are NEVER null in the final
    dashboard JSON — the assembler decides whether to include them.
    """
    symbol: str  # canonical (e.g., "BRK.B")
    regular_price: float
    regular_change_pct: float
    previous_close: float

    pre_market_price: float | None = None
    pre_market_change_pct: float | None = None
    post_market_price: float | None = None
    post_market_change_pct: float | None = None

    session: Literal["pre", "regular", "post", "closed"] = "regular"
    fetched_at: datetime = None  # type: ignore[assignment]
    source: str = "finnhub"
    status: QuoteStatus = "ok"

    @property
    def has_extended_hours(self) -> bool:
        """True if any extended-hours field has a value."""
        return any(
            v is not None
            for v in (
                self.pre_market_price,
                self.pre_market_change_pct,
                self.post_market_price,
                self.post_market_change_pct,
            )
        )


# ---------------------------------------------------------------------------
# Symbol conversion
# ---------------------------------------------------------------------------

def to_yfinance_symbol(canonical: str) -> str:
    """Convert canonical (Finnhub-style) symbol to yfinance's hyphenated form.

    BRK.B → BRK-B, AAPL → AAPL.
    """
    return YFINANCE_SYMBOL_OVERRIDES.get(canonical, canonical)


# ---------------------------------------------------------------------------
# Finnhub client (regular hours)
# ---------------------------------------------------------------------------

def _is_invalid_finnhub_quote(payload: dict) -> bool:
    """Finnhub returns all-zero fields for invalid symbols.

    Specifically, for a non-existent ticker the response looks like:
        {"c": 0, "d": null, "dp": null, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}
    or all zeros depending on the day. We treat current_price == 0 as the
    sentinel for "this symbol doesn't exist."
    """
    return payload.get("c", 0) == 0 and payload.get("pc", 0) == 0


def fetch_finnhub_quote(
    symbol: str,
    limiter: RateLimiter,
    session: requests.Session | None = None,
    timeout: float = 10.0,
) -> dict:
    """Fetch the raw /quote payload for one symbol.

    Returns the parsed JSON dict. Caller is responsible for interpreting it.
    Handles 429s by reporting to the limiter and re-raising so the caller
    can retry. Other HTTP errors raise.
    """
    api_key = require_finnhub_key()
    sess = session or requests.Session()
    url = f"{FINNHUB_BASE_URL}/quote"
    params = {"symbol": symbol, "token": api_key}

    limiter.acquire()
    response = sess.get(url, params=params, timeout=timeout)
    if response.status_code == 429:
        limiter.report_429()
        response.raise_for_status()
    if not response.ok:
        # Don't widen on non-429 errors; could be 401 (bad key) or 500
        response.raise_for_status()
    limiter.report_ok()
    return response.json()


# ---------------------------------------------------------------------------
# yfinance client (extended hours)
# ---------------------------------------------------------------------------

# yfinance is imported lazily so tests can mock cleanly and we don't pay
# the import cost when extended hours aren't needed.

def fetch_yfinance_extended(
    canonical_symbol: str,
    limiter: RateLimiter,
) -> dict:
    """Fetch pre/post-market fields for one symbol via yfinance.

    Returns a dict with keys preMarketPrice, preMarketChangePercent,
    postMarketPrice, postMarketChangePercent (any may be None). On error,
    returns {"error": "..."} — the caller treats this same as all-None.

    This is the lift of fetch_extended.py's fetch_one(), with rate limiting
    added and the error contract preserved.
    """
    import yfinance as yf

    yahoo_symbol = to_yfinance_symbol(canonical_symbol)

    limiter.acquire()
    try:
        info = yf.Ticker(yahoo_symbol).info
        result = {
            "pre_market_price": info.get("preMarketPrice"),
            "pre_market_change_pct": info.get("preMarketChangePercent"),
            "post_market_price": info.get("postMarketPrice"),
            "post_market_change_pct": info.get("postMarketChangePercent"),
        }
        limiter.report_ok()
        return result
    except Exception as e:
        # yfinance throws a variety of exceptions on rate-limit and parsing
        # errors; we widen the limiter and report up.
        limiter.report_429()
        logger.warning(
            "yfinance extended-hours fetch failed for %s (yahoo=%s): %s",
            canonical_symbol, yahoo_symbol, e,
        )
        return {"error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def fetch_quote(
    symbol: str,
    market_status: MarketStatus,
    finnhub_limiter: RateLimiter,
    yfinance_limiter: RateLimiter,
    requests_session: requests.Session | None = None,
) -> Quote:
    """Fetch a complete Quote for one symbol.

    During regular hours: Finnhub only.
    Outside regular hours: Finnhub + yfinance extended hours.

    On Finnhub failure, returns a Quote with status="lookup_failed" and zero
    values everywhere. We never raise — one bad ticker shouldn't break the
    whole dashboard run.
    """
    fetched_at = now()

    try:
        payload = fetch_finnhub_quote(symbol, finnhub_limiter, session=requests_session)
    except Exception as e:
        logger.warning("Finnhub quote fetch failed for %s: %s", symbol, e)
        return Quote(
            symbol=symbol,
            regular_price=0.0,
            regular_change_pct=0.0,
            previous_close=0.0,
            session=market_status.session,
            fetched_at=fetched_at,
            source="finnhub",
            status="lookup_failed",
        )

    if _is_invalid_finnhub_quote(payload):
        return Quote(
            symbol=symbol,
            regular_price=0.0,
            regular_change_pct=0.0,
            previous_close=0.0,
            session=market_status.session,
            fetched_at=fetched_at,
            source="finnhub",
            status="lookup_failed",
        )

    # Use Finnhub's reported values directly. We never recompute from the
    # raw numbers because Finnhub already handles things like splits,
    # dividends, and prev-close timing.
    regular_price = float(payload.get("c", 0) or 0)
    regular_change_pct = float(payload.get("dp", 0) or 0)
    previous_close = float(payload.get("pc", 0) or 0)

    # Extended hours: only fetched outside regular hours (SKILL.md Step 3).
    ext: dict = {}
    source = "finnhub"
    if market_status.session != "regular":
        ext = fetch_yfinance_extended(symbol, yfinance_limiter)
        if "error" not in ext:
            source = "finnhub+yfinance"

    return Quote(
        symbol=symbol,
        regular_price=regular_price,
        regular_change_pct=regular_change_pct,
        previous_close=previous_close,
        pre_market_price=ext.get("pre_market_price"),
        pre_market_change_pct=ext.get("pre_market_change_pct"),
        post_market_price=ext.get("post_market_price"),
        post_market_change_pct=ext.get("post_market_change_pct"),
        session=market_status.session,
        fetched_at=fetched_at,
        source=source,
        status="ok",
    )


def fetch_quotes(
    symbols: list[str],
    market_status: MarketStatus,
    finnhub_limiter: RateLimiter | None = None,
    yfinance_limiter: RateLimiter | None = None,
    requests_session: requests.Session | None = None,
) -> dict[str, Quote]:
    """Fetch quotes for a list of symbols, in order, rate-limited.

    Returns a dict keyed by canonical symbol. Each value is a Quote that
    may have status="lookup_failed" — callers should check.

    Limiters default to fresh instances; pass your own if you want to share
    them across multiple calls in the same process.
    """
    if finnhub_limiter is None:
        finnhub_limiter = RateLimiter(FINNHUB_LIMITS)
    if yfinance_limiter is None:
        yfinance_limiter = RateLimiter(YFINANCE_LIMITS)
    if requests_session is None:
        requests_session = requests.Session()

    results: dict[str, Quote] = {}
    for symbol in symbols:
        results[symbol] = fetch_quote(
            symbol,
            market_status,
            finnhub_limiter,
            yfinance_limiter,
            requests_session=requests_session,
        )
    return results
