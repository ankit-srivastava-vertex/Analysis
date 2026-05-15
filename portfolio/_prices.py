"""
_prices.py — shared helper to fetch & cache daily Close prices for a
list of holdings. Used by risk_metrics and correlation_clusters so we
don't pull 98 series twice in one orchestrator run.
"""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import data_provider  # noqa: E402

# In-process cache keyed by (tuple(symbols), lookback_days)
_CACHE: dict = {}


def _yf_ticker(sym: str, series: str = "EQ") -> str:
    sym = (sym or "").strip().upper()
    if not sym:
        return ""
    if series and series.upper() == "BE":
        return f"{sym}.BO"
    return f"{sym}.NS"


def fetch_close_panel(symbols: Iterable[tuple],
                      lookback_days: int = 400,
                      verbose: bool = True) -> pd.DataFrame:
    """Return a DataFrame of daily Close prices, columns = display symbols.

    `symbols` is an iterable of (symbol, series) tuples. Failed pulls
    are silently dropped.
    """
    sym_list = [(s, ser) for (s, ser) in symbols if s]
    key = (tuple(sym_list), lookback_days)
    if key in _CACHE:
        return _CACHE[key]

    start = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    end = dt.date.today().isoformat()
    closes: dict = {}

    for i, (sym, ser) in enumerate(sym_list, 1):
        if verbose and i % 25 == 0:
            print(f"    prices {i}/{len(sym_list)}")
        ticker = _yf_ticker(sym, ser)
        try:
            df = data_provider.download(ticker, start=start, end=end,
                                        interval="1d", progress=False)
        except Exception:
            continue
        if df is None or df.empty or "Close" not in df.columns:
            continue
        c = df["Close"].dropna()
        if len(c) < 30:
            continue
        c.index = pd.to_datetime(c.index).normalize()
        c = c[~c.index.duplicated(keep="last")]
        closes[sym] = c

    if not closes:
        panel = pd.DataFrame()
    else:
        panel = pd.DataFrame(closes).sort_index()

    _CACHE[key] = panel
    if verbose:
        print(f"    prices: loaded {len(closes)}/{len(sym_list)} series")
    return panel


def fetch_benchmark(ticker: str = "^NSEI",
                    lookback_days: int = 400) -> Optional[pd.Series]:
    start = (dt.date.today() - dt.timedelta(days=lookback_days)).isoformat()
    end = dt.date.today().isoformat()
    try:
        df = data_provider.download(ticker, start=start, end=end,
                                    interval="1d", progress=False)
    except Exception:
        return None
    if df is None or df.empty or "Close" not in df.columns:
        return None
    s = df["Close"].dropna()
    s.index = pd.to_datetime(s.index).normalize()
    return s[~s.index.duplicated(keep="last")]
