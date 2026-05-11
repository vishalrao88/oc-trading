"""Load portfolio.csv, normalize tickers, aggregate tax lots into holdings.

The CSV schema (from the user's broker export):
    Symbol, Acquisition Date, Quantity, Unit Cost ($), Cost Basis ($),
    Value ($), Unrealized Gain/Loss ($), Unrealized Gain/Loss (%), Short/Long

Notes:
  - Numbers can be comma-formatted ("1,156.00"). pandas handles this with
    thousands=",".
  - Dates are M/D/YYYY.
  - Multiple rows per symbol = tax lots; we aggregate to one Holding per
    symbol.
  - We DELIBERATELY IGNORE the CSV's Value/Gain/Loss columns — those are
    stale broker snapshots. Live values are computed from current quotes in
    a later module. This is the fix for the "wrong % move" bug.
  - Tickers are normalized at load time using config.TICKER_ALIASES. After
    load, every downstream module sees only normalized symbols.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

import pandas as pd

from portfolio_lib.config import RECENT_POSITION_DAYS, TICKER_ALIASES


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

TaxTreatment = Literal["short", "long", "unknown"]


@dataclass(frozen=True)
class Position:
    """A single tax lot from the CSV."""
    symbol: str            # normalized
    shares: float
    unit_cost: float       # dollars per share at acquisition
    cost_basis: float      # total dollars paid for this lot
    acquired: date
    tax_treatment: TaxTreatment


@dataclass(frozen=True)
class Holding:
    """All tax lots for a single ticker, aggregated."""
    symbol: str
    total_shares: float
    total_cost: float
    lot_count: int
    earliest_acquired: date
    latest_acquired: date

    @property
    def average_cost(self) -> float:
        """Weighted-average cost per share across all lots."""
        if self.total_shares == 0:
            return 0.0
        return self.total_cost / self.total_shares


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def normalize_ticker(raw: str) -> str:
    """Normalize a ticker symbol via the alias map.

    Cleans whitespace, uppercases, applies TICKER_ALIASES.
    BRKB → BRK.B, etc.
    """
    cleaned = raw.strip().upper()
    return TICKER_ALIASES.get(cleaned, cleaned)


def _parse_tax_treatment(raw: str) -> TaxTreatment:
    """'(Short Term)' → 'short', '(Long Term)' → 'long', else 'unknown'."""
    if not isinstance(raw, str):
        return "unknown"
    lowered = raw.strip().lower()
    if "short" in lowered:
        return "short"
    if "long" in lowered:
        return "long"
    return "unknown"


def load_positions(csv_path: Path) -> list[Position]:
    """Read portfolio.csv and return a list of Position records.

    Raises FileNotFoundError if the CSV doesn't exist.
    Raises ValueError if required columns are missing.
    """
    if not csv_path.exists():
        raise FileNotFoundError(f"Portfolio CSV not found: {csv_path}")

    df = pd.read_csv(csv_path, thousands=",")

    required_cols = {
        "Symbol", "Acquisition Date", "Quantity",
        "Unit Cost ($)", "Cost Basis ($)",
    }
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(
            f"portfolio.csv missing required columns: {sorted(missing)}. "
            f"Found columns: {list(df.columns)}"
        )

    positions: list[Position] = []
    for row in df.itertuples(index=False):
        symbol = normalize_ticker(getattr(row, "Symbol"))
        acquired = datetime.strptime(
            getattr(row, "_1"),  # 'Acquisition Date' becomes _1 after sanitization
            "%m/%d/%Y",
        ).date()
        shares = float(getattr(row, "Quantity"))
        unit_cost = float(getattr(row, "_3"))   # 'Unit Cost ($)'
        cost_basis = float(getattr(row, "_4"))  # 'Cost Basis ($)'
        tax_raw = getattr(row, "_8", "")        # 'Short/Long' (may be absent)
        positions.append(Position(
            symbol=symbol,
            shares=shares,
            unit_cost=unit_cost,
            cost_basis=cost_basis,
            acquired=acquired,
            tax_treatment=_parse_tax_treatment(tax_raw),
        ))

    return positions


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

def aggregate_holdings(positions: list[Position]) -> dict[str, Holding]:
    """Collapse positions (tax lots) into per-symbol Holdings.

    Returns a dict keyed by symbol. Symbols with zero net shares (after
    summing across lots — possible if there are negative quantities, though
    your CSV doesn't appear to have those) are still included so callers
    can decide how to handle them.
    """
    by_symbol: dict[str, list[Position]] = {}
    for p in positions:
        by_symbol.setdefault(p.symbol, []).append(p)

    holdings: dict[str, Holding] = {}
    for symbol, lots in by_symbol.items():
        holdings[symbol] = Holding(
            symbol=symbol,
            total_shares=sum(p.shares for p in lots),
            total_cost=sum(p.cost_basis for p in lots),
            lot_count=len(lots),
            earliest_acquired=min(p.acquired for p in lots),
            latest_acquired=max(p.acquired for p in lots),
        )
    return holdings


# ---------------------------------------------------------------------------
# Recent-position identification
# ---------------------------------------------------------------------------

def is_recent(holding: Holding, as_of: date, days: int = RECENT_POSITION_DAYS) -> bool:
    """True if ANY lot in the holding was acquired within `days` of as_of.

    A symbol counts as "recently added" if its most recent acquisition is
    within the window, regardless of how old its other lots are. This
    matches the SKILL.md definition.
    """
    cutoff = as_of - timedelta(days=days)
    return holding.latest_acquired >= cutoff


def recent_symbols(
    holdings: dict[str, Holding],
    as_of: date,
    days: int = RECENT_POSITION_DAYS,
) -> list[str]:
    """All symbols with at least one acquisition within `days` of as_of.

    Returned in alphabetical order for deterministic output.
    """
    return sorted(
        symbol
        for symbol, holding in holdings.items()
        if is_recent(holding, as_of, days)
    )
