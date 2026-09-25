"""
Stock Autopsy Engine — Reverse-Engineer Why Stocks Run
========================================================

SUMMARY
-------
Identifies stocks that moved >=5% in a single day (or >=30% over 4 weeks),
then reverse-engineers what triggered the move by collecting 12 pre-move
signals and having GPT-5.2 classify the trigger type. Builds a growing
database of autopsied runners for future pattern mining and prediction.

This is SEPARATE from breakout_v5 (which finds technical breakout setups).
The autopsy engine covers ALL trigger types: earnings beats, institutional
accumulation, sector rotation, corporate events, operator moves, news, etc.

WORKFLOW
--------
1. Find runners: scan OHLCV universe for >=5% daily movers and >=30% 4-week
   runners.
2. Enrich: for each runner, collect 12 pre-move signals from existing modules
   (delivery, deals, announcements, earnings, FII changes, sector, stage,
   F&O OI, RS percentile, volatility state).
3. LLM Autopsy: GPT-5.2 classifies the trigger type, identifies which
   pre-signals were visible, and assesses predictability.
4. Multi-week runners get a second LLM call analyzing the accumulation phase.
5. Results are stored in Output/autopsy_database.json (JSONL, append-only).

DATA SOURCES
------------
Same modules as breakout_daily_tracker.py — zero modifications to any file:
- angel_client (via ohlcv_cache) — OHLCV (cache-aware, reads .ohlcv_cache/ first)
- jugaad_data                   — Delivery %
- BulkBlock                     — Bulk/Block deals
- investor_registry             — Superstar name matching
- forensic_accounting           — NSE corporate announcements
- tickertape_client             — Earnings/Revenue fundamentals
- nse_ready_sectors             — Sector RS rankings
- stage_analysis                — Mansfield stage
- fno_max_oi                    — F&O max open interest
- Azure OpenAI GPT-5.2         — Trigger classification

USAGE
-----
    python3 stock_autopsy.py                    # today's runners
    python3 stock_autopsy.py --backfill 180     # backfill 6 months
    python3 stock_autopsy.py --backfill 90      # backfill 3 months
    python3 stock_autopsy.py --date 2026-09-15  # specific date
    python3 stock_autopsy.py --stats            # pattern stats from database
    python3 stock_autopsy.py --no-llm           # enrich only, skip LLM
    python3 stock_autopsy.py --excel            # also generate Excel report
"""

import argparse
import datetime
import json
import os
import re
import socket
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import requests

socket.setdefaulttimeout(30)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "Output")
DB_FILE = os.path.join(OUTPUT_DIR, "autopsy_database.json")
EQUITY_FILE = os.path.join(SCRIPT_DIR, "data", "index_engine", "EQUITY_L.csv")
SME_FILE = os.path.join(SCRIPT_DIR, "data", "index_engine", "SME_EQUITY_L.csv")

MIN_PRICE = 10
MIN_VOLUME = 10_000
DAILY_THRESHOLD = 0.05
WEEKLY_THRESHOLD = 0.30
WEEKLY_LOOKBACK = 20

os.makedirs(OUTPUT_DIR, exist_ok=True)
sys.path.insert(0, SCRIPT_DIR)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CLIENT — Azure OpenAI GPT-5.2
# ═══════════════════════════════════════════════════════════════════════════════

def _load_llm_config():
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(SCRIPT_DIR, ".env"))
    except ImportError:
        pass
    return {
        "api_key": os.environ.get("AZURE_OPENAI_API_KEY", "").strip(),
        "endpoint": os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip(),
        "api_version": os.environ.get("AZURE_OPENAI_API_VERSION",
                                      "2024-12-01-preview").strip(),
        "deployment": os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME", "").strip(),
    }


def llm_call(system_prompt: str, user_prompt: str,
             json_mode: bool = False, max_tokens: int = 3000) -> Optional[str]:
    """Call Azure OpenAI GPT-5.2. Returns response text or None on failure."""
    cfg = _load_llm_config()
    if not cfg["api_key"] or not cfg["endpoint"] or not cfg["deployment"]:
        return None

    url = (f"{cfg['endpoint'].rstrip('/')}/openai/deployments/"
           f"{cfg['deployment']}/chat/completions"
           f"?api-version={cfg['api_version']}")
    headers = {"Content-Type": "application/json", "api-key": cfg["api_key"]}
    payload = {
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "max_completion_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    for attempt in range(3):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            if resp.status_code == 200:
                return resp.json()["choices"][0]["message"]["content"]
            elif resp.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            else:
                print(f"    LLM error {resp.status_code}: {resp.text[:200]}")
                return None
        except Exception as e:
            print(f"    LLM exception: {e}")
            if attempt < 2:
                time.sleep(3)
    return None


def llm_json(system_prompt: str, user_prompt: str,
             max_tokens: int = 3000) -> Optional[dict]:
    raw = llm_call(system_prompt, user_prompt, json_mode=True,
                   max_tokens=max_tokens)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
        m2 = re.search(r'```(?:json)?\s*(\[.*?\])\s*```', raw, re.DOTALL)
        if m2:
            try:
                return {"results": json.loads(m2.group(1))}
            except Exception:
                pass
        print(f"    LLM returned invalid JSON: {raw[:200]}")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
# UNIVERSE — Load ticker list
# ═══════════════════════════════════════════════════════════════════════════════

def load_universe() -> List[str]:
    """Load all NSE mainboard + SME equity tickers."""
    tickers = []
    for fpath in (EQUITY_FILE, SME_FILE):
        if os.path.exists(fpath):
            try:
                df = pd.read_csv(fpath)
                col = "SYMBOL" if "SYMBOL" in df.columns else df.columns[0]
                tickers.extend(df[col].dropna().astype(str).str.strip().tolist())
            except Exception:
                pass
    return sorted(set(t for t in tickers if t))


# ═══════════════════════════════════════════════════════════════════════════════
# FIND RUNNERS — Identify stocks that made big moves
# ═══════════════════════════════════════════════════════════════════════════════

def _load_ohlcv_universe(tickers: List[str], start: datetime.date,
                         end: datetime.date) -> Dict[str, pd.DataFrame]:
    """Load OHLCV for all tickers directly from .ohlcv_cache/ disk files.

    Reads csv.gz files written by ohlcv_cache (populated by daily run_all.py /
    breakout_v5 runs) without hitting any API.  For backfill this is critical
    — Angel One rate-limits bulk requests.  ~3,100 tickers load in <30 seconds.
    """
    import gzip
    from ohlcv_cache import _cache_file, _COLS, _repair

    results = {}
    t0 = time.time()
    missed = 0
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end)

    for i, ticker in enumerate(tickers):
        path = _cache_file(ticker, "1d")
        if not os.path.exists(path):
            missed += 1
            continue
        try:
            with gzip.open(path, "rt", newline="") as gz:
                gz.readline()  # skip schema header
                df = pd.read_csv(gz, index_col=0, parse_dates=[0])
            df = _repair(df)
            if df is not None and not df.empty:
                mask = (df.index >= start_ts) & (df.index <= end_ts)
                sliced = df.loc[mask]
                if not sliced.empty:
                    results[ticker] = sliced
        except Exception:
            missed += 1

        if (i + 1) % 1000 == 0:
            print(f"    OHLCV: {i + 1}/{len(tickers)} "
                  f"({len(results)} loaded) [{time.time() - t0:.0f}s]")

    elapsed = time.time() - t0
    print(f"    OHLCV done: {len(results)}/{len(tickers)} loaded, "
          f"{missed} no cache file [{elapsed:.0f}s]")

    return results


def find_daily_runners(target_date: datetime.date,
                       ohlcv_cache: Dict[str, pd.DataFrame] = None,
                       universe: List[str] = None) -> List[dict]:
    """Find stocks that moved >=5% on a specific date."""
    runners = []
    if universe is None:
        universe = load_universe()

    if ohlcv_cache is None:
        start = target_date - datetime.timedelta(days=45)
        ohlcv_cache = _load_ohlcv_universe(universe, start, target_date)

    target_str = target_date.strftime("%Y-%m-%d")

    for ticker, df in ohlcv_cache.items():
        if df is None or df.empty or len(df) < 2:
            continue

        close_col = "Close" if "Close" in df.columns else "close"
        vol_col = "Volume" if "Volume" in df.columns else "volume"
        if close_col not in df.columns:
            continue

        df = df.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        target_ts = pd.Timestamp(target_str)
        if target_ts not in df.index:
            mask = df.index.date == target_date
            if not mask.any():
                continue
            idx = df.index[mask][-1]
        else:
            idx = target_ts

        pos = df.index.get_loc(idx)
        if pos == 0:
            continue

        close = float(df[close_col].iloc[pos])
        prev_close = float(df[close_col].iloc[pos - 1])

        if prev_close <= 0 or close <= MIN_PRICE:
            continue

        pct = (close - prev_close) / prev_close

        vol = float(df[vol_col].iloc[pos]) if vol_col in df.columns else 0
        if vol < MIN_VOLUME:
            continue

        if pct >= DAILY_THRESHOLD:
            avg_vol_20 = float(df[vol_col].iloc[max(0, pos - 20):pos].mean()) \
                if vol_col in df.columns and pos >= 5 else vol
            vol_ratio = round(vol / avg_vol_20, 1) if avg_vol_20 > 0 else 1.0

            runners.append({
                "symbol": ticker,
                "move_date": target_str,
                "move_pct": round(pct * 100, 2),
                "move_type": "daily",
                "close": round(close, 2),
                "prev_close": round(prev_close, 2),
                "volume": int(vol),
                "volume_ratio": vol_ratio,
            })

    runners.sort(key=lambda r: r["move_pct"], reverse=True)
    return runners


def find_multiweek_runners(target_date: datetime.date,
                           ohlcv_cache: Dict[str, pd.DataFrame] = None,
                           universe: List[str] = None) -> List[dict]:
    """Find stocks that moved >=30% over 4 weeks ending on target_date."""
    runners = []
    if universe is None:
        universe = load_universe()

    if ohlcv_cache is None:
        start = target_date - datetime.timedelta(days=60)
        ohlcv_cache = _load_ohlcv_universe(universe, start, target_date)

    target_str = target_date.strftime("%Y-%m-%d")

    for ticker, df in ohlcv_cache.items():
        if df is None or df.empty or len(df) < WEEKLY_LOOKBACK + 5:
            continue

        close_col = "Close" if "Close" in df.columns else "close"
        vol_col = "Volume" if "Volume" in df.columns else "volume"
        if close_col not in df.columns:
            continue

        df = df.copy()
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        target_ts = pd.Timestamp(target_str)
        if target_ts not in df.index:
            mask = df.index.date <= target_date
            if not mask.any():
                continue
            idx = df.index[mask][-1]
        else:
            idx = target_ts

        pos = df.index.get_loc(idx)
        if pos < WEEKLY_LOOKBACK:
            continue

        close_now = float(df[close_col].iloc[pos])
        close_then = float(df[close_col].iloc[pos - WEEKLY_LOOKBACK])

        if close_then <= 0 or close_now <= MIN_PRICE:
            continue

        pct = (close_now - close_then) / close_then

        if pct >= WEEKLY_THRESHOLD:
            start_pos = pos - WEEKLY_LOOKBACK
            sub = df.iloc[start_pos:pos + 1]
            vol_series = sub[vol_col] if vol_col in sub.columns else pd.Series()
            avg_vol = float(vol_series.mean()) if not vol_series.empty else 0

            run_start_idx = start_pos
            if vol_col in df.columns:
                avg_vol_pre = float(df[vol_col].iloc[
                    max(0, start_pos - 20):start_pos].mean()) \
                    if start_pos >= 20 else avg_vol
                for j in range(start_pos, pos + 1):
                    day_vol = float(df[vol_col].iloc[j])
                    day_ret = (float(df[close_col].iloc[j]) -
                               float(df[close_col].iloc[j - 1])) / \
                              float(df[close_col].iloc[j - 1]) \
                        if j > 0 and float(df[close_col].iloc[j - 1]) > 0 else 0
                    if day_vol > 2 * avg_vol_pre or day_ret > 0.03:
                        run_start_idx = j
                        break

            runners.append({
                "symbol": ticker,
                "move_date": target_str,
                "move_pct": round(pct * 100, 2),
                "move_type": "multi_week",
                "close": round(close_now, 2),
                "close_4w_ago": round(close_then, 2),
                "volume_avg": int(avg_vol),
                "run_start_date": str(df.index[run_start_idx].date()),
                "run_days": pos - run_start_idx,
            })

    runners.sort(key=lambda r: r["move_pct"], reverse=True)
    return runners


# ═══════════════════════════════════════════════════════════════════════════════
# ENRICH — Collect pre-move signals for a runner
# ═══════════════════════════════════════════════════════════════════════════════

def _ohlcv_summary(df: pd.DataFrame, pos: int) -> dict:
    """Summarize 30d pre-move OHLCV pattern."""
    close_col = "Close" if "Close" in df.columns else "close"
    vol_col = "Volume" if "Volume" in df.columns else "volume"
    high_col = "High" if "High" in df.columns else "high"
    low_col = "Low" if "Low" in df.columns else "low"

    lookback = min(30, pos)
    sub = df.iloc[pos - lookback:pos]
    if sub.empty:
        return {}

    closes = sub[close_col].values
    vols = sub[vol_col].values if vol_col in sub.columns else np.array([])

    result = {
        "avg_close": round(float(np.mean(closes)), 2),
        "price_range_pct": round(
            (float(np.max(closes)) - float(np.min(closes))) /
            float(np.mean(closes)) * 100, 1)
        if float(np.mean(closes)) > 0 else 0,
    }

    if len(vols) > 0:
        result["avg_vol_30d"] = int(np.mean(vols))
        result["vol_trend"] = "rising" if np.mean(vols[-5:]) > np.mean(vols) \
            else "flat_or_declining"
        vol_10d = float(np.mean(vols[-10:])) if len(vols) >= 10 else float(np.mean(vols))
        vol_30d = float(np.mean(vols))
        result["vol_dry_up"] = round(vol_10d / vol_30d, 2) if vol_30d > 0 else 1.0

    if high_col in sub.columns:
        highs = sub[high_col].values
        high_30d = float(np.max(highs))
        curr_close = float(closes[-1])
        result["near_30d_high"] = round(
            (curr_close - high_30d) / high_30d * 100, 1) if high_30d > 0 else 0

    if len(closes) >= 20:
        atr_vals = []
        if high_col in sub.columns and low_col in sub.columns:
            for i in range(1, len(sub)):
                tr = max(
                    float(sub[high_col].iloc[i]) - float(sub[low_col].iloc[i]),
                    abs(float(sub[high_col].iloc[i]) - float(sub[close_col].iloc[i - 1])),
                    abs(float(sub[low_col].iloc[i]) - float(sub[close_col].iloc[i - 1])),
                )
                atr_vals.append(tr)
            if len(atr_vals) >= 20:
                atr_10 = np.mean(atr_vals[-10:])
                atr_30 = np.mean(atr_vals)
                result["atr_compression"] = round(atr_10 / atr_30, 2) \
                    if atr_30 > 0 else 1.0
                result["squeeze"] = result["atr_compression"] < 0.65

    return result


def _rs_percentile(df: pd.DataFrame, pos: int, bench_df: pd.DataFrame = None
                   ) -> Optional[float]:
    """Compute RS percentile (simplified — relative price performance)."""
    close_col = "Close" if "Close" in df.columns else "close"
    if pos < 63:
        return None
    curr = float(df[close_col].iloc[pos])
    past_63 = float(df[close_col].iloc[pos - 63])
    past_126 = float(df[close_col].iloc[max(0, pos - 126)])
    if past_63 <= 0:
        return None
    rs_short = (curr - past_63) / past_63
    rs_long = (curr - past_126) / past_126 if past_126 > 0 else rs_short
    return round((0.4 * rs_short + 0.6 * rs_long) * 100, 1)


def enrich_runner(runner: dict, ohlcv_cache: Dict[str, pd.DataFrame],
                  ctx: dict, is_backfill: bool = False) -> dict:
    """Collect 12 pre-move signals for a single runner."""
    symbol = runner["symbol"]
    move_date = datetime.date.fromisoformat(runner["move_date"])
    enrichment = {}

    df = ohlcv_cache.get(symbol)
    if df is not None and not df.empty:
        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)
        target_ts = pd.Timestamp(move_date)
        mask = df.index.date <= move_date
        if mask.any():
            idx = df.index[mask][-1]
            pos = df.index.get_loc(idx)
            enrichment["ohlcv_summary"] = _ohlcv_summary(df, pos)
            enrichment["rs_score"] = _rs_percentile(df, pos)
        else:
            enrichment["ohlcv_summary"] = {}
            enrichment["rs_score"] = None
    else:
        enrichment["ohlcv_summary"] = {}
        enrichment["rs_score"] = None

    # ── Delivery % ──
    enrichment["delivery_5d_avg"] = None
    enrichment["delivery_20d_avg"] = None
    enrichment["delivery_rising"] = None
    try:
        from jugaad_data.nse import stock_df
        ddf = stock_df(
            symbol,
            move_date - datetime.timedelta(days=35),
            move_date - datetime.timedelta(days=1),
            series="EQ",
        )
        if not ddf.empty and "DELIVERY %" in ddf.columns:
            d5 = ddf["DELIVERY %"].tail(5).mean()
            d20 = ddf["DELIVERY %"].tail(20).mean()
            enrichment["delivery_5d_avg"] = round(d5, 1) if not np.isnan(d5) else None
            enrichment["delivery_20d_avg"] = round(d20, 1) if not np.isnan(d20) else None
            enrichment["delivery_rising"] = bool(d5 > d20) \
                if not (np.isnan(d5) or np.isnan(d20)) else None
    except Exception:
        pass
    time.sleep(0.3)

    # ── Bulk/Block deals (from context or fresh fetch) ──
    enrichment["recent_deals"] = []
    enrichment["superstar_buyers"] = []
    deals_df = ctx.get("deals_df", pd.DataFrame())
    if not deals_df.empty:
        from breakout_daily_tracker import _match_deals_to_stock, _match_superstar
        deals = _match_deals_to_stock(symbol, deals_df)
        enrichment["recent_deals"] = deals[:5]
        enrichment["superstar_buyers"] = _match_superstar(
            deals, ctx.get("superstar_names", []))

    # ── NSE Announcements (15 days before) ──
    enrichment["announcements"] = []
    try:
        from forensic_accounting import _nse_session, _nse_get_json
        session = _nse_session()
        anns = _nse_get_json(
            session,
            "https://www.nseindia.com/api/corporate-announcements",
            params={"index": "equities", "symbol": symbol})
        if anns and isinstance(anns, list):
            cutoff = (move_date - datetime.timedelta(days=15)).isoformat()
            move_iso = move_date.isoformat()
            recent = []
            for a in anns[:30]:
                dt = a.get("an_dt", a.get("date", ""))
                if cutoff <= dt <= move_iso:
                    recent.append({
                        "date": dt[:10],
                        "subject": a.get("desc", a.get("subject", ""))[:120],
                    })
            enrichment["announcements"] = recent[:5]
    except Exception:
        pass
    time.sleep(0.3)

    # ── Fundamentals (tickertape) ──
    enrichment["earnings_growth"] = None
    enrichment["revenue_growth"] = None
    try:
        import tickertape_client
        sid = tickertape_client.resolve_sid(symbol)
        if sid:
            stmts = tickertape_client.fetch_statements(sid)
            if stmts and isinstance(stmts, dict):
                inc = stmts.get("income_quarterly",
                                stmts.get("income_annual", []))
                if isinstance(inc, list) and len(inc) >= 2:
                    latest = inc[-1] if isinstance(inc[-1], dict) else {}
                    prev = inc[-2] if isinstance(inc[-2], dict) else {}
                    vals_l = latest.get("values", latest)
                    vals_p = prev.get("values", prev)
                    if isinstance(vals_l, dict) and isinstance(vals_p, dict):
                        rev_l = vals_l.get("revenue", vals_l.get("netRevenue", 0))
                        rev_p = vals_p.get("revenue", vals_p.get("netRevenue", 0))
                        if rev_p and rev_l:
                            enrichment["revenue_growth"] = round(
                                (float(rev_l) / float(rev_p) - 1) * 100, 1)
                        pat_l = vals_l.get("netIncome", vals_l.get("pat", 0))
                        pat_p = vals_p.get("netIncome", vals_p.get("pat", 0))
                        if pat_p and pat_l:
                            enrichment["earnings_growth"] = round(
                                (float(pat_l) / float(pat_p) - 1) * 100, 1)
    except Exception:
        pass
    time.sleep(0.2)

    # ── Shareholding changes (tickertape holdings API) ──
    enrichment["fii_change_qoq"] = None
    try:
        import tickertape_client
        sid = tickertape_client.resolve_sid(symbol)
        if sid:
            url = f"https://api.tickertape.in/stocks/holdings/{sid}"
            resp = requests.get(url, timeout=10, headers={
                "User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                holdings = resp.json().get("data", [])
                if isinstance(holdings, list) and len(holdings) >= 2:
                    for h in holdings:
                        if isinstance(h, dict):
                            fii_now = h.get("fiPctT", h.get("fiiPct"))
                            if fii_now is not None:
                                break
                    else:
                        fii_now = None
                    for h in holdings[1:]:
                        if isinstance(h, dict):
                            fii_prev = h.get("fiPctT", h.get("fiiPct"))
                            if fii_prev is not None:
                                break
                    else:
                        fii_prev = None
                    if fii_now is not None and fii_prev is not None:
                        enrichment["fii_change_qoq"] = round(
                            float(fii_now) - float(fii_prev), 2)
    except Exception:
        pass

    # ── Context-dependent signals (not available in backfill) ──
    if not is_backfill:
        stage2_stocks = ctx.get("stage2_stocks", [])
        enrichment["in_stage2"] = symbol in stage2_stocks

        sector_rs = ctx.get("sector_rs", {})
        enrichment["sector_rs_top5"] = sorted(
            sector_rs.items(), key=lambda x: x[1], reverse=True)[:5] \
            if sector_rs else []

        fno_df = ctx.get("fno_oi_df", pd.DataFrame())
        enrichment["fno_data"] = None
        if not fno_df.empty:
            sym_col = "Symbol" if "Symbol" in fno_df.columns else None
            if sym_col:
                match = fno_df[fno_df[sym_col].astype(str).str.contains(
                    symbol, case=False, na=False)]
                if not match.empty:
                    row = match.iloc[0]
                    enrichment["fno_data"] = {
                        "call_strike": row.get("Call Strike", 0),
                        "put_strike": row.get("Put Strike", 0),
                    }
    else:
        enrichment["in_stage2"] = None
        enrichment["sector_rs_top5"] = None
        enrichment["fno_data"] = None

    enrichment["fii_net_cr"] = ctx.get("fii_net", 0)

    return enrichment


# ═══════════════════════════════════════════════════════════════════════════════
# LLM AUTOPSY — Classify trigger type using GPT-5.2
# ═══════════════════════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are a senior stock market analyst specializing in Indian equities (NSE/BSE). You analyze stocks that made significant price moves and classify what triggered the move based on data available BEFORE the move happened.

TRIGGER CATEGORIES:
- EARNINGS: Quarterly results beat, guidance upgrade, margin expansion
- INSTITUTIONAL: FII/DII buying, superstar investor entry, mutual fund accumulation
- TECHNICAL_BREAKOUT: Resistance breakout, squeeze release, stage transition, pattern completion
- SECTOR_ROTATION: Whole sector moving, thematic play, policy-driven sector rally
- CORPORATE_EVENT: Order win, M&A, capacity expansion, delisting offer, partnership
- OPERATOR_BULK: Bulk deal by unknown entity, block deal, promoter activity
- SHORT_SQUEEZE: Short covering, low float + delivery spike, OI unwinding
- NEWS_REGULATORY: Government policy, SEBI action, commodity price shift, global event
- UNKNOWN: Insufficient data to classify

Be precise. If multiple triggers are present, pick the PRIMARY one. List secondary triggers in pre_signals."""


def _build_runner_prompt(runner: dict, enrichment: dict) -> str:
    """Build the per-runner section of the LLM prompt."""
    ohlcv = enrichment.get("ohlcv_summary", {})
    lines = [
        f"STOCK: {runner['symbol']} | "
        f"Move: +{runner['move_pct']}% on {runner['move_date']} | "
        f"Close: ₹{runner['close']} | "
        f"Volume ratio: {runner.get('volume_ratio', 'N/A')}x avg",
        "",
        "PRE-MOVE DATA:",
        f"- OHLCV 30d: price range {ohlcv.get('price_range_pct', 'N/A')}%, "
        f"vol trend: {ohlcv.get('vol_trend', 'N/A')}, "
        f"near 30d high: {ohlcv.get('near_30d_high', 'N/A')}%, "
        f"ATR compression: {ohlcv.get('atr_compression', 'N/A')}, "
        f"squeeze: {ohlcv.get('squeeze', 'N/A')}",
        f"- Delivery %: 5d avg {enrichment.get('delivery_5d_avg', 'N/A')}% "
        f"vs 20d avg {enrichment.get('delivery_20d_avg', 'N/A')}% "
        f"({'rising' if enrichment.get('delivery_rising') else 'falling/flat'})",
        f"- Bulk deals (7d): {enrichment.get('recent_deals') or 'None'}",
        f"- Superstar investors: {enrichment.get('superstar_buyers') or 'None'}",
        f"- NSE announcements (15d): {enrichment.get('announcements') or 'None'}",
        f"- Earnings QoQ: Revenue {enrichment.get('revenue_growth', 'N/A')}%, "
        f"PAT {enrichment.get('earnings_growth', 'N/A')}%",
        f"- FII holding QoQ change: {enrichment.get('fii_change_qoq', 'N/A')}pp",
        f"- Stage 2 (uptrend): {enrichment.get('in_stage2', 'N/A')}",
        f"- RS score: {enrichment.get('rs_score', 'N/A')}",
        f"- F&O data: {enrichment.get('fno_data') or 'Not in F&O / N/A'}",
        f"- FII net (market): {enrichment.get('fii_net_cr', 0):.0f} Cr",
    ]
    return "\n".join(lines)


def llm_autopsy_batch(runners_with_enrichment: List[tuple]) -> List[dict]:
    """Classify a batch of runners using GPT-5.2. Returns list of autopsy dicts."""
    if not runners_with_enrichment:
        return []

    batch_prompt_parts = []
    for i, (runner, enrichment) in enumerate(runners_with_enrichment, 1):
        batch_prompt_parts.append(f"\n--- RUNNER {i} ---")
        batch_prompt_parts.append(_build_runner_prompt(runner, enrichment))

    user_prompt = f"""Analyze these {len(runners_with_enrichment)} stocks that made significant moves. For EACH stock, classify the primary trigger and identify pre-move signals.

{chr(10).join(batch_prompt_parts)}

Respond in JSON with a "results" array containing one object per stock:
{{
  "results": [
    {{
      "symbol": "...",
      "trigger_type": "EARNINGS | INSTITUTIONAL | TECHNICAL_BREAKOUT | SECTOR_ROTATION | CORPORATE_EVENT | OPERATOR_BULK | SHORT_SQUEEZE | NEWS_REGULATORY | UNKNOWN",
      "confidence": 0-100,
      "thesis": "1-2 sentence explanation of what triggered the move",
      "pre_signals": ["list of signals visible BEFORE the move"],
      "pre_signal_strength": "STRONG | MODERATE | WEAK",
      "accumulation_signs": "any signs of smart money accumulation before the move or null",
      "predictable": true or false
    }}
  ]
}}"""

    result = llm_json(SYSTEM_PROMPT, user_prompt, max_tokens=4000)
    if not result:
        return [{}] * len(runners_with_enrichment)

    results = result.get("results", [])
    if isinstance(results, list):
        while len(results) < len(runners_with_enrichment):
            results.append({})
        return results
    return [{}] * len(runners_with_enrichment)


def llm_accumulation_analysis(runner: dict, enrichment: dict,
                              autopsy: dict, df: pd.DataFrame) -> Optional[dict]:
    """For multi-week runners, analyze what sustained the run."""
    if runner.get("move_type") != "multi_week":
        return None

    close_col = "Close" if "Close" in df.columns else "close"
    vol_col = "Volume" if "Volume" in df.columns else "volume"

    run_start = runner.get("run_start_date", runner["move_date"])
    move_date = runner["move_date"]

    mask = (df.index >= pd.Timestamp(run_start)) & \
           (df.index <= pd.Timestamp(move_date))
    run_df = df[mask]

    if run_df.empty:
        return None

    vol_pattern = ""
    if vol_col in run_df.columns:
        vols = run_df[vol_col].values
        first_half_vol = float(np.mean(vols[:len(vols) // 2]))
        second_half_vol = float(np.mean(vols[len(vols) // 2:]))
        if second_half_vol > first_half_vol * 1.2:
            vol_pattern = "accelerating (bullish)"
        elif second_half_vol < first_half_vol * 0.8:
            vol_pattern = "decelerating (potential distribution)"
        else:
            vol_pattern = "steady"

    up_days = 0
    down_days = 0
    closes = run_df[close_col].values
    for i in range(1, len(closes)):
        if closes[i] > closes[i - 1]:
            up_days += 1
        else:
            down_days += 1

    user_prompt = f"""This stock ran {runner['move_pct']}% over {runner.get('run_days', '?')} trading days ({run_start} to {move_date}).

Initial trigger: {autopsy.get('trigger_type', 'UNKNOWN')} — {autopsy.get('thesis', 'Unknown')}

ACCUMULATION PHASE DATA:
- Volume pattern: {vol_pattern}
- Up days: {up_days}, Down days: {down_days}
- Delivery % trend: 5d avg {enrichment.get('delivery_5d_avg', 'N/A')}%
- FII QoQ change: {enrichment.get('fii_change_qoq', 'N/A')}pp
- Superstar buyers: {enrichment.get('superstar_buyers') or 'None'}
- Announcements during run: {enrichment.get('announcements') or 'None'}

Analyze what SUSTAINED this multi-week run. Respond in JSON:
{{
  "sustaining_factors": ["list of factors that kept the run going"],
  "distribution_signs": "any signs of distribution/selling during the run or null",
  "run_exhaustion_signals": "any signs the run was ending or null",
  "institutional_pattern": "description of institutional buying pattern or null"
}}"""

    return llm_json(SYSTEM_PROMPT, user_prompt, max_tokens=1500)


# ═══════════════════════════════════════════════════════════════════════════════
# DATABASE — Store and read autopsy results
# ═══════════════════════════════════════════════════════════════════════════════

def load_database() -> List[dict]:
    """Load the autopsy database (JSONL format)."""
    if not os.path.exists(DB_FILE):
        return []
    records = []
    with open(DB_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return records


def save_record(record: dict):
    """Append one record to the database."""
    with open(DB_FILE, "a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def get_existing_keys() -> set:
    """Get set of (symbol, move_date) already in the database for dedup."""
    records = load_database()
    return {(r["symbol"], r["move_date"]) for r in records}


def print_stats():
    """Print pattern statistics from the autopsy database."""
    records = load_database()
    if not records:
        print("Database is empty. Run with --backfill first.")
        return

    print(f"\n{'='*70}")
    print(f"AUTOPSY DATABASE STATISTICS")
    print(f"{'='*70}")
    print(f"Total records: {len(records)}")

    dates = [r.get("move_date", "") for r in records]
    if dates:
        print(f"Date range: {min(dates)} to {max(dates)}")

    daily = [r for r in records if r.get("move_type") == "daily"]
    weekly = [r for r in records if r.get("move_type") == "multi_week"]
    print(f"Daily runners (>=5%): {len(daily)}")
    print(f"Multi-week runners (>=30%): {len(weekly)}")

    triggers = {}
    for r in records:
        t = r.get("autopsy", {}).get("trigger_type", "UNKNOWN")
        triggers[t] = triggers.get(t, 0) + 1

    print(f"\nTrigger Breakdown:")
    for t, count in sorted(triggers.items(), key=lambda x: -x[1]):
        pct = count / len(records) * 100
        print(f"  {t:25s} {count:5d} ({pct:5.1f}%)")

    predictable = sum(1 for r in records
                      if r.get("autopsy", {}).get("predictable") is True)
    print(f"\nPredictable (pre-signals visible): "
          f"{predictable}/{len(records)} ({predictable / len(records) * 100:.1f}%)")

    pre_signals = {}
    for r in records:
        for s in r.get("autopsy", {}).get("pre_signals", []):
            pre_signals[s] = pre_signals.get(s, 0) + 1

    if pre_signals:
        print(f"\nTop Pre-Move Signals:")
        for s, count in sorted(pre_signals.items(), key=lambda x: -x[1])[:15]:
            print(f"  {s:40s} {count:5d}")

    strengths = {}
    for r in records:
        s = r.get("autopsy", {}).get("pre_signal_strength", "UNKNOWN")
        strengths[s] = strengths.get(s, 0) + 1
    print(f"\nPre-Signal Strength Distribution:")
    for s, count in sorted(strengths.items(), key=lambda x: -x[1]):
        print(f"  {s:15s} {count:5d} ({count / len(records) * 100:.1f}%)")

    print(f"\n{'='*70}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — Orchestration
# ═══════════════════════════════════════════════════════════════════════════════

def gather_market_context(skip_expensive: bool = False) -> dict:
    """Gather market-wide context (same pattern as breakout_daily_tracker)."""
    ctx: Dict[str, Any] = {}
    today = datetime.date.today()

    print("  [ctx] FII/DII flows...")
    try:
        import fii_flows
        fii_today = fii_flows.fetch_today()
        ctx["fii_today"] = fii_today
        if not fii_today.empty:
            fii_row = fii_today[
                fii_today["category"].str.contains("FII|FPI", case=False, na=False)]
            ctx["fii_net"] = float(fii_row.iloc[0].get("netValue", 0)) \
                if not fii_row.empty else 0
        else:
            ctx["fii_net"] = 0
        print(f"        FII net: {ctx['fii_net']:.0f} Cr")
    except Exception as e:
        print(f"        FII error (non-fatal): {e}")
        ctx["fii_net"] = 0

    if not skip_expensive:
        print("  [ctx] Sector rankings...")
        try:
            import nse_ready_sectors
            result = nse_ready_sectors.run()
            if result is not None:
                all_rs, _, ranking_df, _, _, _ = result
                ctx["sector_rs"] = {
                    k: round(float(v.iloc[-1]), 2)
                    for k, v in all_rs.items()
                    if hasattr(v, 'iloc') and len(v) > 0}
            else:
                ctx["sector_rs"] = {}
        except Exception as e:
            print(f"        Sector RS error (non-fatal): {e}")
            ctx["sector_rs"] = {}

        print("  [ctx] Stage analysis...")
        try:
            import stage_analysis
            sa = stage_analysis.analyse(verbose=False)
            if sa:
                ctx["stage2_stocks"] = [
                    s.get("symbol", "").replace(".NS", "").replace(".BO", "")
                    for s in sa.get("stocks", [])
                    if s.get("stage") == 2]
            else:
                ctx["stage2_stocks"] = []
        except Exception as e:
            print(f"        Stage error (non-fatal): {e}")
            ctx["stage2_stocks"] = []

        print("  [ctx] F&O Max OI...")
        try:
            import fno_max_oi
            fno_result = fno_max_oi.run(expiry_type='weekly')
            if fno_result is not None:
                fno_df, _ = fno_result
                ctx["fno_oi_df"] = fno_df if fno_df is not None else pd.DataFrame()
            else:
                ctx["fno_oi_df"] = pd.DataFrame()
        except Exception as e:
            print(f"        F&O OI error (non-fatal): {e}")
            ctx["fno_oi_df"] = pd.DataFrame()
    else:
        ctx["sector_rs"] = {}
        ctx["stage2_stocks"] = []
        ctx["fno_oi_df"] = pd.DataFrame()

    print("  [ctx] Bulk/Block deals...")
    try:
        import BulkBlock
        scraper = BulkBlock.BSEScraper()
        frames = []
        for mode in ("bulk_deals", "block_deals"):
            try:
                df = scraper.nse_largedeals(mode)
                if df is not None and not df.empty:
                    frames.append(df)
            except Exception:
                pass
        for dtype in ("bulk", "block"):
            try:
                df = scraper.fetch_bse_deals_api(dtype)
                if df is not None and not df.empty:
                    frames.append(df)
            except Exception:
                pass
        ctx["deals_df"] = pd.concat(frames, ignore_index=True) \
            if frames else pd.DataFrame()
        print(f"        {len(ctx['deals_df'])} deal records")
    except Exception as e:
        print(f"        Deals error (non-fatal): {e}")
        ctx["deals_df"] = pd.DataFrame()

    try:
        import investor_registry
        ctx["superstar_names"] = investor_registry.all_bulk_deal_names()
    except Exception:
        ctx["superstar_names"] = []

    return ctx


def run_single_date(target_date: datetime.date, use_llm: bool = True,
                    generate_excel: bool = False):
    """Run autopsy for a single date."""
    print(f"\n{'='*70}")
    print(f"STOCK AUTOPSY — {target_date.strftime('%d-%b-%Y')}")
    print(f"{'='*70}")

    existing_keys = get_existing_keys()

    print("\n1. Gathering market context...")
    is_backfill = target_date < datetime.date.today() - datetime.timedelta(days=2)
    ctx = gather_market_context(skip_expensive=is_backfill)

    print("\n2. Loading OHLCV universe...")
    universe = load_universe()
    print(f"   {len(universe)} tickers loaded")

    start_date = target_date - datetime.timedelta(days=60)
    ohlcv_cache = _load_ohlcv_universe(universe, start_date, target_date)
    print(f"   OHLCV loaded for {len(ohlcv_cache)} tickers")

    print("\n3. Finding runners...")
    daily_runners = find_daily_runners(target_date, ohlcv_cache, universe)
    weekly_runners = find_multiweek_runners(target_date, ohlcv_cache, universe)

    daily_runners = [r for r in daily_runners
                     if (r["symbol"], r["move_date"]) not in existing_keys]
    weekly_runners = [r for r in weekly_runners
                      if (r["symbol"], r["move_date"]) not in existing_keys]

    print(f"   Daily runners (>=5%): {len(daily_runners)}")
    print(f"   Multi-week runners (>=30% in 4w): {len(weekly_runners)}")

    all_runners = daily_runners + weekly_runners
    if not all_runners:
        print("\n   No new runners found for this date.")
        return

    print(f"\n4. Enriching {len(all_runners)} runners...")
    enrichments = []
    for i, runner in enumerate(all_runners):
        enrichment = enrich_runner(runner, ohlcv_cache, ctx,
                                  is_backfill=is_backfill)
        enrichments.append(enrichment)
        if (i + 1) % 10 == 0:
            print(f"   Enriched {i + 1}/{len(all_runners)}...")

    print(f"   Enrichment complete")

    if use_llm:
        print(f"\n5. LLM autopsy ({len(all_runners)} runners)...")
        batch_size_llm = 8
        all_autopsies = []
        for i in range(0, len(all_runners), batch_size_llm):
            batch = list(zip(all_runners[i:i + batch_size_llm],
                             enrichments[i:i + batch_size_llm]))
            autopsies = llm_autopsy_batch(batch)
            all_autopsies.extend(autopsies)
            print(f"   Batch {i // batch_size_llm + 1}: "
                  f"{len(autopsies)} classified")
            time.sleep(1)

        print(f"\n6. Saving to database...")
        for runner, enrichment, autopsy in zip(
                all_runners, enrichments, all_autopsies):
            accumulation = None
            if runner.get("move_type") == "multi_week" and autopsy:
                df = ohlcv_cache.get(runner["symbol"])
                if df is not None:
                    accumulation = llm_accumulation_analysis(
                        runner, enrichment, autopsy, df)

            record = {
                **runner,
                "enrichment": enrichment,
                "autopsy": autopsy,
                "accumulation": accumulation,
                "scan_date": datetime.date.today().isoformat(),
            }
            save_record(record)
    else:
        print(f"\n5. Saving enrichment data (no LLM)...")
        for runner, enrichment in zip(all_runners, enrichments):
            record = {
                **runner,
                "enrichment": enrichment,
                "autopsy": {},
                "accumulation": None,
                "scan_date": datetime.date.today().isoformat(),
            }
            save_record(record)

    # ── Summary ──
    triggers = {}
    predictable_count = 0
    for runner, enrichment, autopsy in zip(
            all_runners, enrichments,
            all_autopsies if use_llm else [{}] * len(all_runners)):
        t = autopsy.get("trigger_type", "UNKNOWN") if autopsy else "NO_LLM"
        triggers[t] = triggers.get(t, 0) + 1
        if autopsy and autopsy.get("predictable"):
            predictable_count += 1

    print(f"\n{'='*70}")
    print(f"SUMMARY — {target_date.strftime('%d-%b-%Y')}")
    print(f"{'='*70}")
    print(f"Daily runners: {len(daily_runners)}")
    print(f"Multi-week runners: {len(weekly_runners)}")
    if use_llm:
        print(f"\nTrigger breakdown:")
        for t, count in sorted(triggers.items(), key=lambda x: -x[1]):
            print(f"  {t:25s} {count:3d} "
                  f"({count / len(all_runners) * 100:.1f}%)")
        print(f"\nPredictable: {predictable_count}/{len(all_runners)} "
              f"({predictable_count / len(all_runners) * 100:.1f}%)")
    print(f"{'='*70}\n")

    if generate_excel:
        _save_excel(target_date, all_runners, enrichments,
                    all_autopsies if use_llm else [{}] * len(all_runners))


def _save_excel(target_date, runners, enrichments, autopsies):
    """Save autopsy results to Excel."""
    rows = []
    for runner, enrichment, autopsy in zip(runners, enrichments, autopsies):
        rows.append({
            "Symbol": runner["symbol"],
            "Date": runner["move_date"],
            "Move %": runner["move_pct"],
            "Type": runner["move_type"],
            "Close": runner["close"],
            "Vol Ratio": runner.get("volume_ratio", ""),
            "Trigger": autopsy.get("trigger_type", ""),
            "Confidence": autopsy.get("confidence", ""),
            "Thesis": autopsy.get("thesis", ""),
            "Pre-Signals": ", ".join(autopsy.get("pre_signals", [])),
            "Strength": autopsy.get("pre_signal_strength", ""),
            "Predictable": autopsy.get("predictable", ""),
            "Delivery 5d": enrichment.get("delivery_5d_avg", ""),
            "Delivery Rising": enrichment.get("delivery_rising", ""),
            "Superstar Buyers": ", ".join(enrichment.get("superstar_buyers", [])),
            "Earnings QoQ %": enrichment.get("earnings_growth", ""),
            "Revenue QoQ %": enrichment.get("revenue_growth", ""),
            "FII Change pp": enrichment.get("fii_change_qoq", ""),
            "RS Score": enrichment.get("rs_score", ""),
            "Stage 2": enrichment.get("in_stage2", ""),
        })
    df = pd.DataFrame(rows)
    path = os.path.join(OUTPUT_DIR,
                        f"stock_autopsy_{target_date.strftime('%Y%m%d')}.xlsx")
    df.to_excel(path, index=False, sheet_name="Autopsy")
    print(f"Excel saved: {path}")


def run_backfill(days: int, use_llm: bool = True):
    """Backfill the autopsy database for the past N trading days."""
    print(f"\n{'='*70}")
    print(f"STOCK AUTOPSY — BACKFILL {days} DAYS")
    print(f"{'='*70}")

    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=int(days * 1.5))

    print("\n1. Loading OHLCV universe for full backfill window...")
    universe = load_universe()

    ohlcv_cache = _load_ohlcv_universe(universe, start_date, end_date)
    print(f"   OHLCV loaded for {len(ohlcv_cache)} tickers")

    trading_days = set()
    for df in ohlcv_cache.values():
        if df is not None and not df.empty:
            if not isinstance(df.index, pd.DatetimeIndex):
                df.index = pd.to_datetime(df.index)
            for d in df.index:
                if start_date <= d.date() <= end_date:
                    trading_days.add(d.date())

    trading_days = sorted(trading_days)[-days:]
    print(f"\n2. Processing {len(trading_days)} trading days...")

    existing_keys = get_existing_keys()
    ctx = {"fii_net": 0, "deals_df": pd.DataFrame(),
           "superstar_names": [], "sector_rs": {},
           "stage2_stocks": [], "fno_oi_df": pd.DataFrame()}
    try:
        import investor_registry
        ctx["superstar_names"] = investor_registry.all_bulk_deal_names()
    except Exception:
        pass

    total_runners = 0
    for day_idx, target_date in enumerate(trading_days):
        daily_runners = find_daily_runners(target_date, ohlcv_cache, universe)
        weekly_runners = []
        if day_idx == len(trading_days) - 1:
            weekly_runners = find_multiweek_runners(
                target_date, ohlcv_cache, universe)

        all_runners = [r for r in daily_runners + weekly_runners
                       if (r["symbol"], r["move_date"]) not in existing_keys]

        if not all_runners:
            continue

        print(f"\n  {target_date}: {len(all_runners)} new runners "
              f"(daily: {len(daily_runners)}, weekly: {len(weekly_runners)})")

        enrichments = []
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
        for runner in all_runners:
            with ThreadPoolExecutor(1) as ex:
                fut = ex.submit(enrich_runner, runner, ohlcv_cache, ctx,
                                True)
                try:
                    enrichment = fut.result(timeout=90)
                except (FutTimeout, Exception):
                    enrichment = {}
            enrichments.append(enrichment)

        if use_llm:
            batch_size_llm = 8
            all_autopsies = []
            for i in range(0, len(all_runners), batch_size_llm):
                batch = list(zip(all_runners[i:i + batch_size_llm],
                                 enrichments[i:i + batch_size_llm]))
                with ThreadPoolExecutor(1) as ex:
                    fut = ex.submit(llm_autopsy_batch, batch)
                    try:
                        autopsies = fut.result(timeout=120)
                    except (FutTimeout, Exception):
                        autopsies = [{}] * len(batch)
                all_autopsies.extend(autopsies)
                time.sleep(1)

            for runner, enrichment, autopsy in zip(
                    all_runners, enrichments, all_autopsies):
                accumulation = None
                if runner.get("move_type") == "multi_week" and autopsy:
                    df = ohlcv_cache.get(runner["symbol"])
                    if df is not None:
                        with ThreadPoolExecutor(1) as ex:
                            fut = ex.submit(
                                llm_accumulation_analysis,
                                runner, enrichment, autopsy, df)
                            try:
                                accumulation = fut.result(timeout=120)
                            except (FutTimeout, Exception):
                                accumulation = None
                record = {
                    **runner,
                    "enrichment": enrichment,
                    "autopsy": autopsy,
                    "accumulation": accumulation,
                    "scan_date": datetime.date.today().isoformat(),
                }
                save_record(record)
                existing_keys.add((runner["symbol"], runner["move_date"]))
        else:
            for runner, enrichment in zip(all_runners, enrichments):
                record = {
                    **runner,
                    "enrichment": enrichment,
                    "autopsy": {},
                    "accumulation": None,
                    "scan_date": datetime.date.today().isoformat(),
                }
                save_record(record)
                existing_keys.add((runner["symbol"], runner["move_date"]))

        total_runners += len(all_runners)

    print(f"\n{'='*70}")
    print(f"BACKFILL COMPLETE")
    print(f"  Days processed: {len(trading_days)}")
    print(f"  Total new runners: {total_runners}")
    print(f"  Database: {DB_FILE}")
    print(f"{'='*70}\n")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="Stock Autopsy Engine — reverse-engineer why stocks run")
    parser.add_argument("--date", type=str, default=None,
                        help="Target date (YYYY-MM-DD), default today")
    parser.add_argument("--backfill", type=int, default=None,
                        help="Backfill N trading days")
    parser.add_argument("--stats", action="store_true",
                        help="Print pattern stats from database")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM classification")
    parser.add_argument("--excel", action="store_true",
                        help="Generate Excel report")

    args = parser.parse_args()

    if args.stats:
        print_stats()
        return

    if args.backfill:
        run_backfill(args.backfill, use_llm=not args.no_llm)
        if not args.no_llm:
            print_stats()
        return

    target_date = datetime.date.fromisoformat(args.date) \
        if args.date else datetime.date.today()

    run_single_date(target_date, use_llm=not args.no_llm,
                    generate_excel=args.excel)


if __name__ == "__main__":
    main()
