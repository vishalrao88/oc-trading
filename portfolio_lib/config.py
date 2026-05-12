"""Configuration: paths, API credentials, rate limits, and thresholds.

All tunable values for the dashboard live here. No magic numbers in any
other module — they import from this one. This makes it trivial to see
what's configured, change a threshold, or override for testing.

API keys are read from environment variables. The dashboard fails loudly
if a required key is missing rather than running with a None and hitting
a confusing 401 later.

Rate limits are set to roughly 85% of published free-tier limits:
    - Finnhub:   60/min published → 50/min configured
    - yfinance:  unpublished, ~120/min observed → 30/min configured (conservative)
    - Brave:     60/min and 2000/month → not used in main pipeline

If you upgrade a tier, change the value here; no other module needs to know.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Repo root (this file lives at <root>/portfolio_lib/config.py)
REPO_ROOT = Path(__file__).resolve().parent.parent

# Default I/O locations — overridable via env vars so the agent can point
# them at the workspace without changing code.
PORTFOLIO_CSV = Path(os.environ.get(
    "PORTFOLIO_CSV",
    REPO_ROOT / "portfolio.csv",
))
DASHBOARD_STATE_JSON = Path(os.environ.get(
    "DASHBOARD_STATE_JSON",
    REPO_ROOT / "dashboard_state.json",
))


# ---------------------------------------------------------------------------
# API credentials (read at import, fail loudly if missing when used)
# ---------------------------------------------------------------------------

FINNHUB_API_KEY: str | None = os.environ.get("FINNHUB_API_KEY")


def require_finnhub_key() -> str:
    """Return the Finnhub API key or raise with a clear message."""
    if not FINNHUB_API_KEY:
        raise RuntimeError(
            "FINNHUB_API_KEY environment variable is not set. "
            "Get a free key at https://finnhub.io and export it."
        )
    return FINNHUB_API_KEY


# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RateLimitConfig:
    """Configuration for a single API's rate limiter."""
    name: str
    calls_per_minute: int  # configured floor (not the published ceiling)
    max_wait_seconds: float = 60.0  # raise rather than hang longer than this


FINNHUB_LIMITS = RateLimitConfig(
    name="finnhub",
    calls_per_minute=50,  # ~85% of published 60/min
)

YFINANCE_LIMITS = RateLimitConfig(
    name="yfinance",
    calls_per_minute=30,  # conservative; yfinance is unofficial scraping
)

BRAVE_LIMITS = RateLimitConfig(
    name="brave",
    calls_per_minute=50,  # 1/sec floor, with headroom
)


# ---------------------------------------------------------------------------
# Category thresholds (from SKILL.md)
# ---------------------------------------------------------------------------

# Heavyweights: top N positions by current market value
HEAVYWEIGHT_TOP_N = 10

# Recent positions: acquired within the last N days
RECENT_POSITION_DAYS = 180

# Top movers: |daily_change_pct| above this threshold
MOVER_PCT_THRESHOLD = 4.0


# ---------------------------------------------------------------------------
# News fetching
# ---------------------------------------------------------------------------

# Max articles to request from Finnhub per ticker (per SKILL.md Step 4b)
NEWS_MAX_ARTICLES_PER_TICKER = 10

# Max articles to keep after scoring (per SKILL.md Step 4d)
NEWS_KEEP_TOP_N = 3

# Minimum adjusted score to retain an article (per SKILL.md Step 4d)
NEWS_MIN_SCORE = 3


# ---------------------------------------------------------------------------
# Quote fetching
# ---------------------------------------------------------------------------

# How many retries on transient errors before giving up
QUOTE_FETCH_RETRIES = 3

# Initial backoff in seconds; doubles each retry
QUOTE_FETCH_INITIAL_BACKOFF = 1.0


# ---------------------------------------------------------------------------
# Ticker normalization (the BRKB → BRK-B class of bugs)
# ---------------------------------------------------------------------------

# Map portfolio.csv tickers (left) to the form Finnhub/yfinance expect (right).
# Add to this as you discover more.
TICKER_ALIASES: dict[str, str] = {
    "BRKB": "BRK.B",
    "BFB": "BF.B",
    # Add more as needed; keep both sides uppercase.
}

# Map canonical (Finnhub-style) symbols to the form yfinance/Yahoo uses.
# Finnhub uses dots (BRK.B); yfinance uses hyphens (BRK-B). Same underlying
# stock, different punctuation conventions.
YFINANCE_SYMBOL_OVERRIDES: dict[str, str] = {
    "BRK.B": "BRK-B",
    "BRK.A": "BRK-A",
    "BF.B": "BF-B",
    "BF.A": "BF-A",
}
