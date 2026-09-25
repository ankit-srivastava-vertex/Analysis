"""
Runner Predictor — Predict Which Stocks Will Run 5%+ Next
==========================================================

SUMMARY
-------
Inverts the autopsy engine: instead of asking "why did this stock run?", asks
"which stocks will run next?" by mining the 7,343-record autopsy database for
statistical signal patterns, scoring the universe, and using GPT-5.2 for deep
analysis on the shortlist.

WORKFLOW
--------
1. Build / load signal weights from autopsy database (Bayesian lift ratios).
2. Load OHLCV for all ~3,100 stocks from .ohlcv_cache/ disk files (zero API).
3. Extract 8 binary features per stock, compute statistical score.
4. Take top 100 candidates → full enrichment (delivery, deals, announcements).
5. Re-score with enrichment data, take top 50 → LLM deep analysis.
6. Rank by composite score (0.4×stat + 0.6×LLM), output top 20.
7. Track outcomes at 1/3/5/10/20 days for forward testing.
8. Self-improve: weekly miss analysis, weight retraining, UNKNOWN reclassify.

DATA SOURCES
------------
Same modules as stock_autopsy.py — zero modifications to any file:
- ohlcv_cache          — .ohlcv_cache/ disk files (zero API calls)
- jugaad_data          — Delivery %
- BulkBlock            — Bulk/Block deals
- investor_registry    — Superstar name matching
- forensic_accounting  — NSE corporate announcements
- tickertape_client    — Earnings/Revenue fundamentals
- nse_ready_sectors    — Sector RS rankings
- stage_analysis       — Mansfield stage
- fno_max_oi           — F&O max open interest
- Azure OpenAI GPT-5.2 — Prediction analysis

USAGE
-----
    python3 runner_predictor.py                    # today's predictions
    python3 runner_predictor.py --date 2026-09-22  # specific date
    python3 runner_predictor.py --no-llm           # stat scores only (free)
    python3 runner_predictor.py --track            # update forward test outcomes
    python3 runner_predictor.py --stats            # prediction accuracy stats
    python3 runner_predictor.py --improve-weekly   # analyze recent misses
    python3 runner_predictor.py --improve-weights  # retrain signal weights
    python3 runner_predictor.py --reclassify       # re-classify UNKNOWNs
    python3 runner_predictor.py --build-weights    # force rebuild weights from DB
    python3 runner_predictor.py --top 30           # change output size
"""

import argparse
import datetime
import gzip
import json
import os
import re
import socket
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutTimeout
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import requests

socket.setdefaulttimeout(30)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "Output")
DB_FILE = os.path.join(OUTPUT_DIR, "autopsy_database.json")
PREDICTIONS_FILE = os.path.join(OUTPUT_DIR, "runner_predictions.json")
TRACKER_FILE = os.path.join(OUTPUT_DIR, "prediction_tracker.json")
WEIGHTS_FILE = os.path.join(OUTPUT_DIR, "signal_weights.json")
EQUITY_FILE = os.path.join(SCRIPT_DIR, "data", "index_engine", "EQUITY_L.csv")
SME_FILE = os.path.join(SCRIPT_DIR, "data", "index_engine", "SME_EQUITY_L.csv")

MIN_PRICE = 10
MIN_VOLUME = 10_000
PREDICTION_TARGET_PCT = 5.0
STAT_SHORTLIST_SIZE = 100
LLM_SHORTLIST_SIZE = 50
LLM_BATCH_SIZE = 10
FINAL_PICKS = 20
TRACKING_WINDOWS = [1, 3, 5, 10, 20]

os.makedirs(OUTPUT_DIR, exist_ok=True)
sys.path.insert(0, SCRIPT_DIR)

DEFAULT_WEIGHTS = {
    "vol_trend_rising":  {"weight": 15, "lift": 1.24, "coverage": 0.47},
    "near_30d_high":     {"weight": 25, "lift": 1.44, "coverage": 0.29},
    "delivery_rising":   {"weight": 10, "lift": 1.14, "coverage": 0.49},
    "rs_strong":         {"weight": 20, "lift": 1.38, "coverage": 0.09},
    "atr_compression":   {"weight": 12, "lift": 1.08, "coverage": 0.12},
    "vol_dry_up":        {"weight": 8,  "lift": 1.16, "coverage": 0.32},
    "price_tight_range": {"weight": 5,  "lift": 1.07, "coverage": 0.40},
    "high_vol_ratio":    {"weight": 5,  "lift": 1.16, "coverage": 0.50},
}

DEFAULT_COMBO_BONUSES = {
    "near_30d_high+vol_trend_rising": 10,
    "delivery_rising+near_30d_high+vol_trend_rising": 15,
    "near_30d_high+rs_strong+vol_trend_rising": 15,
}


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CLIENT — Azure OpenAI GPT-5.2
# ═══════════════════════════════════════════════════════════════════════════════

def _load_llm_config():
    """Load Azure OpenAI config from .env."""
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
    """Call LLM and parse JSON response with fallback extraction."""
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
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# UNIVERSE + OHLCV LOADING — Zero API calls
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


def _load_ohlcv_universe(tickers: List[str], start: datetime.date,
                         end: datetime.date) -> Dict[str, pd.DataFrame]:
    """Load OHLCV for all tickers directly from .ohlcv_cache/ disk files.

    Zero API calls — reads csv.gz files populated by daily run_all.py.
    ~3,100 tickers load in ~4 seconds.
    """
    from ohlcv_cache import _cache_file, _repair

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
                gz.readline()
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


# ═══════════════════════════════════════════════════════════════════════════════
# SIGNAL WEIGHT SYSTEM — Mine patterns from autopsy database
# ═══════════════════════════════════════════════════════════════════════════════

def load_autopsy_database() -> List[dict]:
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


def _extract_features_from_enrichment(enrichment: dict) -> dict:
    """Extract the 8 binary features from an autopsy enrichment dict."""
    ohlcv = enrichment.get("ohlcv_summary", {})
    features = {
        "vol_trend_rising": ohlcv.get("vol_trend") == "rising",
        "near_30d_high": (ohlcv.get("near_30d_high") is not None
                          and ohlcv.get("near_30d_high", -999) >= -5),
        "rs_strong": (enrichment.get("rs_score") is not None
                      and enrichment.get("rs_score", 0) > 30),
        "atr_compression": (ohlcv.get("atr_compression") is not None
                            and ohlcv.get("atr_compression", 999) < 0.85),
        "vol_dry_up": (ohlcv.get("vol_dry_up") is not None
                       and ohlcv.get("vol_dry_up", 0) > 1.2),
        "price_tight_range": (ohlcv.get("price_range_pct") is not None
                              and ohlcv.get("price_range_pct", 999) < 15),
        "high_vol_ratio": (ohlcv.get("vol_dry_up") is not None
                           and ohlcv.get("vol_dry_up", 0) > 2.0),
        "delivery_rising": enrichment.get("delivery_rising"),
    }
    return features


def build_weights_from_database() -> dict:
    """Mine the autopsy database for signal weights using Bayesian lift ratios."""
    records = load_autopsy_database()
    if not records:
        print("  No autopsy database found. Using default weights.")
        return {"version": 0, "features": DEFAULT_WEIGHTS,
                "combination_bonuses": DEFAULT_COMBO_BONUSES}

    print(f"\n  Mining signal weights from {len(records)} autopsy records...")

    predictable = [r for r in records
                   if r.get("autopsy", {}).get("predictable") is True]
    unpredictable = [r for r in records
                     if r.get("autopsy", {}).get("predictable") is not True]

    p_pred = len(predictable) / len(records) if records else 0.5
    print(f"  Base rate: {p_pred:.1%} predictable "
          f"({len(predictable)}/{len(records)})")

    feature_names = ["vol_trend_rising", "near_30d_high", "rs_strong",
                     "atr_compression", "vol_dry_up", "price_tight_range",
                     "high_vol_ratio", "delivery_rising"]

    feature_stats = {}
    for fname in feature_names:
        n_pred_with = 0
        n_pred_without = 0
        n_unpred_with = 0
        n_unpred_without = 0

        for r in predictable:
            feats = _extract_features_from_enrichment(r.get("enrichment", {}))
            val = feats.get(fname)
            if val is None:
                continue
            if val:
                n_pred_with += 1
            else:
                n_pred_without += 1

        for r in unpredictable:
            feats = _extract_features_from_enrichment(r.get("enrichment", {}))
            val = feats.get(fname)
            if val is None:
                continue
            if val:
                n_unpred_with += 1
            else:
                n_unpred_without += 1

        total_with = n_pred_with + n_unpred_with
        total_without = n_pred_without + n_unpred_without
        total = total_with + total_without

        if total_with > 0 and total > 0:
            p_pred_given_feat = n_pred_with / total_with
            lift = p_pred_given_feat / p_pred if p_pred > 0 else 1.0
            coverage = total_with / total
        else:
            lift = 1.0
            coverage = 0.0

        feature_stats[fname] = {
            "lift": round(lift, 3),
            "coverage": round(coverage, 3),
            "n_with": total_with,
            "n_without": total_without,
            "p_pred_given_feat": round(p_pred_given_feat, 3) if total_with > 0 else 0,
        }
        print(f"    {fname:20s}: lift={lift:.3f}, "
              f"coverage={coverage:.1%}, N={total_with}")

    raw_weights = {k: max(1, min(30, round(v["lift"] * 10)))
                   for k, v in feature_stats.items()}
    total_raw = sum(raw_weights.values())
    normalized = {k: round(v / total_raw * 100, 1) for k, v in raw_weights.items()}

    features_out = {}
    for fname in feature_names:
        features_out[fname] = {
            "weight": normalized[fname],
            "lift": feature_stats[fname]["lift"],
            "coverage": feature_stats[fname]["coverage"],
        }

    # --- Combination bonuses ---
    print("\n  Scanning feature combinations...")
    combo_bonuses = {}
    combo_keys = [
        ("near_30d_high", "vol_trend_rising"),
        ("delivery_rising", "near_30d_high", "vol_trend_rising"),
        ("near_30d_high", "rs_strong", "vol_trend_rising"),
        ("delivery_rising", "near_30d_high", "rs_strong", "vol_trend_rising"),
        ("atr_compression", "near_30d_high", "vol_trend_rising"),
        ("atr_compression", "delivery_rising", "vol_trend_rising"),
    ]

    for combo in combo_keys:
        n_with_combo_pred = 0
        n_with_combo_total = 0

        for r in records:
            feats = _extract_features_from_enrichment(r.get("enrichment", {}))
            all_present = all(feats.get(f) is True for f in combo)
            any_none = any(feats.get(f) is None for f in combo)
            if any_none:
                continue
            if all_present:
                n_with_combo_total += 1
                if r.get("autopsy", {}).get("predictable") is True:
                    n_with_combo_pred += 1

        if n_with_combo_total >= 10:
            p = n_with_combo_pred / n_with_combo_total
            combo_key = "+".join(sorted(combo))
            if p > 0.80:
                bonus = round((p - p_pred) * 30)
                combo_bonuses[combo_key] = max(5, min(25, bonus))
                print(f"    {combo_key}: P(pred)={p:.1%}, N={n_with_combo_total}, "
                      f"bonus={combo_bonuses[combo_key]}")

    weights = {
        "version": 1,
        "updated": datetime.date.today().isoformat(),
        "sample_size": len(records),
        "base_rate": round(p_pred, 4),
        "features": features_out,
        "combination_bonuses": combo_bonuses,
    }

    with open(WEIGHTS_FILE, "w") as f:
        json.dump(weights, f, indent=2)
    print(f"\n  Weights saved to {WEIGHTS_FILE}")

    return weights


def load_signal_weights() -> dict:
    """Load signal weights from file, or build from DB if first run."""
    if os.path.exists(WEIGHTS_FILE):
        with open(WEIGHTS_FILE, "r") as f:
            return json.load(f)
    return build_weights_from_database()


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURE EXTRACTION — 8 binary features from OHLCV data
# ═══════════════════════════════════════════════════════════════════════════════

def extract_features(df: pd.DataFrame) -> Tuple[dict, dict]:
    """Extract 8 binary features from an OHLCV DataFrame (latest bar).

    Returns (features_dict, ohlcv_summary_dict).
    """
    close_col = "Close" if "Close" in df.columns else "close"
    vol_col = "Volume" if "Volume" in df.columns else "volume"
    high_col = "High" if "High" in df.columns else "high"
    low_col = "Low" if "Low" in df.columns else "low"

    if len(df) < 30:
        return {k: None for k in DEFAULT_WEIGHTS}, {}

    pos = len(df) - 1
    lookback = min(30, pos)
    sub = df.iloc[pos - lookback:pos + 1]
    closes = sub[close_col].values

    ohlcv_summary = {
        "avg_close": round(float(np.mean(closes)), 2),
    }

    # Price range
    mean_c = float(np.mean(closes))
    price_range_pct = round(
        (float(np.max(closes)) - float(np.min(closes))) / mean_c * 100, 1
    ) if mean_c > 0 else 0
    ohlcv_summary["price_range_pct"] = price_range_pct

    # Volume features
    vols = sub[vol_col].values if vol_col in sub.columns else np.array([])
    vol_trend = None
    vol_dry_up_val = None
    if len(vols) > 5:
        avg_vol_30d = float(np.mean(vols))
        avg_vol_5d = float(np.mean(vols[-5:]))
        vol_trend = avg_vol_5d > avg_vol_30d
        ohlcv_summary["avg_vol_30d"] = int(avg_vol_30d)
        ohlcv_summary["vol_trend"] = "rising" if vol_trend else "flat_or_declining"

        if len(vols) >= 10:
            avg_vol_10d = float(np.mean(vols[-10:]))
            vol_dry_up_val = round(avg_vol_10d / avg_vol_30d, 2) \
                if avg_vol_30d > 0 else 1.0
            ohlcv_summary["vol_dry_up"] = vol_dry_up_val

    # Near 30d high
    near_30d_high = None
    if high_col in sub.columns:
        highs = sub[high_col].values
        high_30d = float(np.max(highs))
        curr_close = float(closes[-1])
        near_pct = round((curr_close - high_30d) / high_30d * 100, 1) \
            if high_30d > 0 else 0
        ohlcv_summary["near_30d_high"] = near_pct
        near_30d_high = near_pct >= -5

    # RS score
    rs_score = None
    if pos >= 63:
        curr = float(df[close_col].iloc[pos])
        past_63 = float(df[close_col].iloc[pos - 63])
        past_126 = float(df[close_col].iloc[max(0, pos - 126)])
        if past_63 > 0:
            rs_short = (curr - past_63) / past_63
            rs_long = (curr - past_126) / past_126 if past_126 > 0 else rs_short
            rs_score = round((0.4 * rs_short + 0.6 * rs_long) * 100, 1)

    # ATR compression
    atr_comp = None
    if len(sub) >= 20 and high_col in sub.columns and low_col in sub.columns:
        atr_vals = []
        for i in range(1, len(sub)):
            tr = max(
                float(sub[high_col].iloc[i]) - float(sub[low_col].iloc[i]),
                abs(float(sub[high_col].iloc[i]) - float(sub[close_col].iloc[i - 1])),
                abs(float(sub[low_col].iloc[i]) - float(sub[close_col].iloc[i - 1])),
            )
            atr_vals.append(tr)
        if len(atr_vals) >= 20:
            atr_10 = float(np.mean(atr_vals[-10:]))
            atr_30 = float(np.mean(atr_vals))
            atr_comp = round(atr_10 / atr_30, 2) if atr_30 > 0 else 1.0
            ohlcv_summary["atr_compression"] = atr_comp
            ohlcv_summary["squeeze"] = atr_comp < 0.65

    features = {
        "vol_trend_rising": vol_trend,
        "near_30d_high": near_30d_high,
        "rs_strong": rs_score is not None and rs_score > 30,
        "atr_compression": atr_comp is not None and atr_comp < 0.85,
        "vol_dry_up": vol_dry_up_val is not None and vol_dry_up_val > 1.2,
        "price_tight_range": price_range_pct < 15,
        "high_vol_ratio": vol_dry_up_val is not None and vol_dry_up_val > 2.0,
        "delivery_rising": None,
    }

    ohlcv_summary["rs_score"] = rs_score

    return features, ohlcv_summary


# ═══════════════════════════════════════════════════════════════════════════════
# STATISTICAL SCORING ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

def compute_stat_score(features: dict, weights: dict
                       ) -> Tuple[float, List[str]]:
    """Compute a 0-100 statistical prediction score.

    Returns (score, list_of_active_signals).
    """
    w = weights.get("features", DEFAULT_WEIGHTS)
    combos = weights.get("combination_bonuses", DEFAULT_COMBO_BONUSES)

    total_possible = sum(f["weight"] for f in w.values())
    earned = 0.0
    active = []

    for feat_name, feat_val in features.items():
        fw = w.get(feat_name, {}).get("weight", 0)
        if feat_val is True:
            earned += fw
            active.append(feat_name)
        elif feat_val is None:
            earned += fw * 0.3

    combo_earned = 0
    combo_possible = 0
    active_set = set(active)
    for combo_key, bonus in combos.items():
        parts = set(combo_key.split("+"))
        combo_possible += bonus
        if parts.issubset(active_set):
            combo_earned += bonus

    total_possible += combo_possible
    earned += combo_earned

    score = min(100, earned / total_possible * 100) if total_possible > 0 else 0
    return round(score, 1), active


def score_universe(ohlcv_data: Dict[str, pd.DataFrame],
                   weights: dict) -> List[dict]:
    """Score the entire universe using OHLCV features only (zero API calls).

    Returns top STAT_SHORTLIST_SIZE candidates sorted by score.
    """
    t0 = time.time()
    candidates = []

    for ticker, df in ohlcv_data.items():
        if df is None or df.empty or len(df) < 30:
            continue

        close_col = "Close" if "Close" in df.columns else "close"
        vol_col = "Volume" if "Volume" in df.columns else "volume"

        last_close = float(df[close_col].iloc[-1])
        if last_close < MIN_PRICE:
            continue

        if vol_col in df.columns:
            avg_vol = float(df[vol_col].tail(20).mean())
            if avg_vol < MIN_VOLUME:
                continue
        else:
            avg_vol = 0

        features, ohlcv_summary = extract_features(df)
        score, active = compute_stat_score(features, weights)

        if score > 0:
            candidates.append({
                "symbol": ticker,
                "stat_score": score,
                "active_signals": active,
                "features": features,
                "close": round(last_close, 2),
                "volume": int(avg_vol),
                "rs_score": ohlcv_summary.get("rs_score"),
                "ohlcv_summary": ohlcv_summary,
            })

    candidates.sort(key=lambda x: x["stat_score"], reverse=True)
    elapsed = time.time() - t0
    print(f"    Scored {len(candidates)} stocks [{elapsed:.1f}s]")

    return candidates[:STAT_SHORTLIST_SIZE]


# ═══════════════════════════════════════════════════════════════════════════════
# MARKET CONTEXT + ENRICHMENT
# ═══════════════════════════════════════════════════════════════════════════════

def gather_context() -> dict:
    """Gather market-wide context (FII, sectors, deals, stage2, F&O)."""
    ctx: Dict[str, Any] = {}

    print("  [ctx] FII/DII flows...")
    try:
        import fii_flows
        fii_today = fii_flows.fetch_today()
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

    # --- Use weekly cache for expensive calls ---
    cache_file = os.path.join(OUTPUT_DIR, "breakout_context_cache.json")
    cache = {}
    if os.path.exists(cache_file):
        try:
            with open(cache_file) as f:
                cache = json.load(f)
        except Exception:
            pass

    now = datetime.datetime.now()
    stale_hours = 168

    # Sector RS
    sector_rs_ts = cache.get("sector_rs_ts")
    use_cached_sector = False
    if sector_rs_ts:
        try:
            cached_time = datetime.datetime.fromisoformat(sector_rs_ts)
            if (now - cached_time).total_seconds() < stale_hours * 3600:
                use_cached_sector = True
        except Exception:
            pass

    if use_cached_sector and cache.get("sector_rs"):
        ctx["sector_rs"] = cache["sector_rs"]
        print(f"  [ctx] Sector RS: cached ({len(ctx['sector_rs'])} sectors)")
    else:
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

    # Stage 2
    stage2_ts = cache.get("stage2_stocks_ts")
    use_cached_stage = False
    if stage2_ts:
        try:
            cached_time = datetime.datetime.fromisoformat(stage2_ts)
            if (now - cached_time).total_seconds() < stale_hours * 3600:
                use_cached_stage = True
        except Exception:
            pass

    if use_cached_stage and "stage2_stocks" in cache:
        ctx["stage2_stocks"] = cache["stage2_stocks"]
        print(f"  [ctx] Stage 2: cached ({len(ctx['stage2_stocks'])} stocks)")
    else:
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


def _enrich_one(candidate: dict, ctx: dict) -> dict:
    """Enrich a single candidate with delivery, deals, announcements, fundamentals."""
    symbol = candidate["symbol"]
    enrichment = {}
    today = datetime.date.today()

    # --- Delivery % ---
    enrichment["delivery_5d_avg"] = None
    enrichment["delivery_20d_avg"] = None
    enrichment["delivery_rising"] = None
    try:
        from jugaad_data.nse import stock_df
        ddf = stock_df(
            symbol,
            today - datetime.timedelta(days=35),
            today - datetime.timedelta(days=1),
            series="EQ",
        )
        if not ddf.empty and "DELIVERY %" in ddf.columns:
            d5 = ddf["DELIVERY %"].tail(5).mean()
            d20 = ddf["DELIVERY %"].tail(20).mean()
            enrichment["delivery_5d_avg"] = round(d5, 1) \
                if not np.isnan(d5) else None
            enrichment["delivery_20d_avg"] = round(d20, 1) \
                if not np.isnan(d20) else None
            enrichment["delivery_rising"] = bool(d5 > d20) \
                if not (np.isnan(d5) or np.isnan(d20)) else None
    except Exception:
        pass
    time.sleep(0.3)

    # --- Bulk/Block deals ---
    enrichment["recent_deals"] = []
    enrichment["superstar_buyers"] = []
    deals_df = ctx.get("deals_df", pd.DataFrame())
    if not deals_df.empty:
        try:
            from breakout_daily_tracker import _match_deals_to_stock, _match_superstar
            deals = _match_deals_to_stock(symbol, deals_df)
            enrichment["recent_deals"] = deals[:5]
            enrichment["superstar_buyers"] = _match_superstar(
                deals, ctx.get("superstar_names", []))
        except Exception:
            pass

    # --- NSE Announcements ---
    enrichment["announcements"] = []
    try:
        from forensic_accounting import _nse_session, _nse_get_json
        session = _nse_session()
        anns = _nse_get_json(
            session,
            "https://www.nseindia.com/api/corporate-announcements",
            params={"index": "equities", "symbol": symbol})
        if anns and isinstance(anns, list):
            cutoff = (today - datetime.timedelta(days=15)).isoformat()
            recent = []
            for a in anns[:30]:
                dt = a.get("an_dt", a.get("date", ""))
                if dt >= cutoff:
                    recent.append({
                        "date": dt[:10],
                        "subject": a.get("desc", a.get("subject", ""))[:120],
                    })
            enrichment["announcements"] = recent[:5]
    except Exception:
        pass
    time.sleep(0.3)

    # --- Fundamentals ---
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

    # --- FII QoQ holding change ---
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
                    fii_now = None
                    for h in holdings:
                        if isinstance(h, dict):
                            fii_now = h.get("fiPctT", h.get("fiiPct"))
                            if fii_now is not None:
                                break
                    fii_prev = None
                    for h in holdings[1:]:
                        if isinstance(h, dict):
                            fii_prev = h.get("fiPctT", h.get("fiiPct"))
                            if fii_prev is not None:
                                break
                    if fii_now is not None and fii_prev is not None:
                        enrichment["fii_change_qoq"] = round(
                            float(fii_now) - float(fii_prev), 2)
    except Exception:
        pass

    # --- Context-dependent signals ---
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

    enrichment["fii_net_cr"] = ctx.get("fii_net", 0)

    return enrichment


def enrich_candidates(candidates: List[dict], ctx: dict) -> List[dict]:
    """Run full enrichment on candidates with per-stock timeout."""
    t0 = time.time()
    enriched = []
    total = len(candidates)

    for i, cand in enumerate(candidates):
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(_enrich_one, cand, ctx)
                enrichment = fut.result(timeout=90)
        except (FutTimeout, Exception) as e:
            print(f"    Enrich timeout/error for {cand['symbol']}: {e}")
            enrichment = {}

        cand["enrichment"] = enrichment

        if enrichment.get("delivery_rising") is not None:
            cand["features"]["delivery_rising"] = enrichment["delivery_rising"]

        enriched.append(cand)

        if (i + 1) % 25 == 0:
            print(f"    Enriched: {i + 1}/{total} [{time.time() - t0:.0f}s]")

    elapsed = time.time() - t0
    print(f"    Enrichment done: {len(enriched)}/{total} [{elapsed:.0f}s]")
    return enriched


# ═══════════════════════════════════════════════════════════════════════════════
# LLM PREDICTION ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════════

PREDICTION_SYSTEM_PROMPT = """You are a senior stock market analyst specializing in Indian equities (NSE/BSE). You predict which stocks are most likely to make a 5%+ move within 1-5 trading days based on pre-move signal patterns.

You have been trained on 7,343 autopsy records of past runners. Key patterns:
- TECHNICAL_BREAKOUT (54%): Rising volume + near 30d high + ATR compression most common. 96% predictable when vol_rising + near_high + delivery_rising all present.
- SECTOR_ROTATION (8%): Sector RS top quartile + stage 2 stocks. 74% predictable.
- INSTITUTIONAL: Superstar buyer + rising delivery. 87% predictable.
- OPERATOR_BULK (11%): Bulk deal + volume spike. Low confidence (27% predictable).
- SHORT_SQUEEZE (5%): Low float + delivery spike + OI unwinding.

Be selective. Only assign conviction >60 when multiple confirming signals align. A stock with vol_rising alone is weak; vol_rising + near_30d_high + delivery_rising is strong."""


def _build_candidate_prompt(cand: dict) -> str:
    """Build per-candidate section for the LLM prompt."""
    ohlcv = cand.get("ohlcv_summary", {})
    enr = cand.get("enrichment", {})
    lines = [
        f"  {cand['symbol']}: StatScore={cand['stat_score']}, "
        f"Close=₹{cand['close']}, AvgVol={cand['volume']:,}",
        f"    OHLCV: vol_trend={ohlcv.get('vol_trend', 'N/A')}, "
        f"near_30d_high={ohlcv.get('near_30d_high', 'N/A')}%, "
        f"ATR_comp={ohlcv.get('atr_compression', 'N/A')}, "
        f"squeeze={ohlcv.get('squeeze', 'N/A')}",
        f"    RS score: {ohlcv.get('rs_score', 'N/A')}",
        f"    Delivery: 5d={enr.get('delivery_5d_avg', 'N/A')}% "
        f"vs 20d={enr.get('delivery_20d_avg', 'N/A')}% "
        f"(rising={enr.get('delivery_rising', 'N/A')})",
        f"    Deals: {enr.get('recent_deals') or 'None'}",
        f"    Superstars: {enr.get('superstar_buyers') or 'None'}",
        f"    Announcements: {len(enr.get('announcements', []))} recent",
        f"    Earnings QoQ: Rev {enr.get('revenue_growth', 'N/A')}%, "
        f"PAT {enr.get('earnings_growth', 'N/A')}%",
        f"    FII change: {enr.get('fii_change_qoq', 'N/A')}pp",
        f"    Stage 2: {enr.get('in_stage2', 'N/A')}",
        f"    F&O: {enr.get('fno_data') or 'N/A'}",
        f"    Active signals: {', '.join(cand.get('active_signals', []))}",
    ]
    return "\n".join(lines)


def llm_predict_batch(candidates: List[dict], ctx: dict) -> List[dict]:
    """Send a batch of candidates to GPT-5.2 for prediction analysis."""
    if not candidates:
        return []

    sector_rs = ctx.get("sector_rs", {})
    top_sectors = sorted(sector_rs.items(), key=lambda x: x[1],
                         reverse=True)[:5] if sector_rs else []

    stock_sections = []
    for cand in candidates:
        stock_sections.append(_build_candidate_prompt(cand))

    user_prompt = f"""MARKET CONTEXT:
- FII net: {ctx.get('fii_net', 0):.0f} Cr
- Top sectors by RS: {', '.join(f'{s}({v:.0f})' for s, v in top_sectors) if top_sectors else 'N/A'}
- Stage 2 stocks count: {len(ctx.get('stage2_stocks', []))}

CANDIDATES ({len(candidates)} stocks, pre-scored by statistical model):
{chr(10).join(stock_sections)}

For each stock, predict likelihood of a 5%+ move in 1-5 trading days.
Respond in JSON:
{{
  "predictions": [
    {{
      "symbol": "TICKER",
      "conviction": 0-100,
      "expected_trigger": "TECHNICAL_BREAKOUT | SECTOR_ROTATION | INSTITUTIONAL | OPERATOR_BULK | SHORT_SQUEEZE | NEWS_REGULATORY | EARNINGS | CORPORATE_EVENT",
      "timeframe_days": 1-5,
      "thesis": "Why this stock will run (1-2 sentences)",
      "risk": "What could prevent the move",
      "signals_aligned": ["list of confirming signals"],
      "entry_zone": "price range for entry",
      "stop_loss_pct": 3-7,
      "target_pct": 5-15
    }}
  ]
}}"""

    result = llm_json(PREDICTION_SYSTEM_PROMPT, user_prompt, max_tokens=4000)
    if not result:
        return []

    predictions = result.get("predictions", [])
    if not isinstance(predictions, list):
        return []

    # Merge LLM results back into candidate dicts
    pred_map = {p.get("symbol", ""): p for p in predictions
                if isinstance(p, dict)}

    enriched = []
    for cand in candidates:
        pred = pred_map.get(cand["symbol"], {})
        cand["llm_conviction"] = pred.get("conviction", 0)
        cand["expected_trigger"] = pred.get("expected_trigger", "UNKNOWN")
        cand["timeframe_days"] = pred.get("timeframe_days", 5)
        cand["thesis"] = pred.get("thesis", "")
        cand["risk"] = pred.get("risk", "")
        cand["signals_aligned"] = pred.get("signals_aligned", [])
        cand["entry_zone"] = pred.get("entry_zone", "")
        cand["stop_loss_pct"] = pred.get("stop_loss_pct", 5)
        cand["target_pct"] = pred.get("target_pct", 10)
        enriched.append(cand)

    return enriched


def run_llm_analysis(candidates: List[dict], ctx: dict) -> List[dict]:
    """Run LLM analysis in batches of LLM_BATCH_SIZE with timeouts."""
    all_results = []
    total = len(candidates)

    for i in range(0, total, LLM_BATCH_SIZE):
        batch = candidates[i:i + LLM_BATCH_SIZE]
        batch_num = i // LLM_BATCH_SIZE + 1
        total_batches = (total + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE
        print(f"    LLM batch {batch_num}/{total_batches} "
              f"({len(batch)} stocks)...")

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(llm_predict_batch, batch, ctx)
                results = fut.result(timeout=120)
            all_results.extend(results)
        except (FutTimeout, Exception) as e:
            print(f"    LLM batch {batch_num} error: {e}")
            for cand in batch:
                cand["llm_conviction"] = 0
                cand["expected_trigger"] = "NO_LLM"
                cand["thesis"] = ""
                cand["risk"] = ""
                cand["timeframe_days"] = 5
                cand["signals_aligned"] = []
                cand["entry_zone"] = ""
                cand["stop_loss_pct"] = 5
                cand["target_pct"] = 10
                all_results.append(cand)

    return all_results


# ═══════════════════════════════════════════════════════════════════════════════
# OUTPUT + STORAGE
# ═══════════════════════════════════════════════════════════════════════════════

def rank_predictions(candidates: List[dict], top_n: int = FINAL_PICKS
                     ) -> List[dict]:
    """Rank by composite score (0.4×stat + 0.6×LLM), return top N."""
    for cand in candidates:
        if "composite_score" in cand and cand.get("llm_conviction", 0) == 0:
            pass
        else:
            stat = cand.get("stat_score", 0)
            llm = cand.get("llm_conviction", 0)
            cand["composite_score"] = round(0.4 * stat + 0.6 * llm, 1)

    candidates.sort(key=lambda x: x["composite_score"], reverse=True)

    ranked = candidates[:top_n]
    for i, cand in enumerate(ranked, 1):
        cand["rank"] = i

    return ranked


def save_predictions(predictions: List[dict], date_str: str):
    """Append predictions to JSONL file."""
    existing_keys = set()
    if os.path.exists(PREDICTIONS_FILE):
        with open(PREDICTIONS_FILE, "r") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        r = json.loads(line)
                        existing_keys.add((r.get("symbol"), r.get("prediction_date")))
                    except Exception:
                        pass

    saved = 0
    with open(PREDICTIONS_FILE, "a") as f:
        for pred in predictions:
            key = (pred["symbol"], date_str)
            if key in existing_keys:
                continue
            record = {
                "prediction_date": date_str,
                "symbol": pred["symbol"],
                "rank": pred.get("rank", 0),
                "stat_score": pred.get("stat_score", 0),
                "llm_conviction": pred.get("llm_conviction", 0),
                "composite_score": pred.get("composite_score", 0),
                "close_at_prediction": pred.get("close", 0),
                "expected_trigger": pred.get("expected_trigger", "UNKNOWN"),
                "timeframe_days": pred.get("timeframe_days", 5),
                "thesis": pred.get("thesis", ""),
                "risk": pred.get("risk", ""),
                "signals_aligned": pred.get("signals_aligned", []),
                "active_signals": pred.get("active_signals", []),
                "entry_zone": pred.get("entry_zone", ""),
                "stop_loss_pct": pred.get("stop_loss_pct", 5),
                "target_pct": pred.get("target_pct", 10),
            }
            f.write(json.dumps(record, default=str) + "\n")
            saved += 1

    print(f"    Saved {saved} predictions to {PREDICTIONS_FILE}")


def print_predictions(predictions: List[dict], date_str: str):
    """Print predictions to console."""
    print(f"\n{'='*70}")
    print(f"  RUNNER PREDICTIONS — {date_str}")
    print(f"  Target: {PREDICTION_TARGET_PCT}%+ move within 1-5 trading days")
    print(f"{'='*70}\n")

    for pred in predictions:
        rank = pred.get("rank", "?")
        symbol = pred["symbol"]
        comp = pred.get("composite_score", 0)
        stat = pred.get("stat_score", 0)
        llm = pred.get("llm_conviction", 0)
        trigger = pred.get("expected_trigger", "?")
        tf = pred.get("timeframe_days", "?")
        signals = pred.get("active_signals", [])
        thesis = pred.get("thesis", "")
        risk = pred.get("risk", "")
        close = pred.get("close", 0)
        entry = pred.get("entry_zone", "")
        sl_pct = pred.get("stop_loss_pct", 5)
        tgt_pct = pred.get("target_pct", 10)

        sl = round(close * (1 - sl_pct / 100), 2) if close else "?"
        tgt = round(close * (1 + tgt_pct / 100), 2) if close else "?"

        print(f"  #{rank} {symbol} — Composite: {comp}/100")
        print(f"     Close: ₹{close}  |  Stat: {stat}  |  LLM: {llm}/100")
        print(f"     Trigger: {trigger}  |  Timeframe: {tf} days")
        if signals:
            print(f"     Signals: {', '.join(signals)}")
        if thesis:
            print(f"     Thesis: {thesis[:120]}")
        if risk:
            print(f"     Risk: {risk[:100]}")
        if entry:
            print(f"     Entry: {entry}  |  Stop: ₹{sl}  |  Target: ₹{tgt}")
        else:
            print(f"     Stop: ₹{sl}  |  Target: ₹{tgt}")
        print(f"  {'─'*66}")

    print()


def save_excel(predictions: List[dict], ctx: dict, weights: dict,
               date_str: str):
    """Save predictions to Excel with multiple sheets."""
    try:
        from openpyxl import Workbook
    except ImportError:
        print("    openpyxl not available, skipping Excel output")
        return

    filepath = os.path.join(OUTPUT_DIR, f"runner_predictions_{date_str}.xlsx")

    # --- Sheet 1: Predictions ---
    pred_rows = []
    for p in predictions:
        pred_rows.append({
            "Rank": p.get("rank"),
            "Symbol": p["symbol"],
            "Composite": p.get("composite_score"),
            "StatScore": p.get("stat_score"),
            "LLM": p.get("llm_conviction"),
            "Close": p.get("close"),
            "Trigger": p.get("expected_trigger"),
            "Timeframe": p.get("timeframe_days"),
            "Thesis": p.get("thesis", "")[:200],
            "Risk": p.get("risk", "")[:150],
            "Entry": p.get("entry_zone"),
            "StopPct": p.get("stop_loss_pct"),
            "TargetPct": p.get("target_pct"),
        })
    pred_df = pd.DataFrame(pred_rows)

    # --- Sheet 2: Signal Analysis ---
    signal_rows = []
    for p in predictions:
        row = {"Symbol": p["symbol"], "StatScore": p.get("stat_score")}
        for feat in DEFAULT_WEIGHTS:
            row[feat] = p.get("features", {}).get(feat, "")
        signal_rows.append(row)
    signal_df = pd.DataFrame(signal_rows)

    # --- Sheet 3: Market Context ---
    ctx_rows = [
        {"Metric": "FII Net (Cr)", "Value": ctx.get("fii_net", 0)},
        {"Metric": "Stage 2 Stocks", "Value": len(ctx.get("stage2_stocks", []))},
        {"Metric": "Deal Records", "Value": len(ctx.get("deals_df", []))},
    ]
    for s, v in sorted(ctx.get("sector_rs", {}).items(),
                       key=lambda x: -x[1])[:10]:
        ctx_rows.append({"Metric": f"Sector RS: {s}", "Value": v})
    ctx_df = pd.DataFrame(ctx_rows)

    # --- Sheet 4: Methodology ---
    meth_rows = [
        {"Parameter": "Prediction Date", "Value": date_str},
        {"Parameter": "Autopsy DB Size", "Value": weights.get("sample_size", "?")},
        {"Parameter": "Weight Version", "Value": weights.get("version", "?")},
        {"Parameter": "Base Rate (predictable)", "Value": weights.get("base_rate", "?")},
        {"Parameter": "Stat Shortlist", "Value": STAT_SHORTLIST_SIZE},
        {"Parameter": "LLM Shortlist", "Value": LLM_SHORTLIST_SIZE},
        {"Parameter": "Final Picks", "Value": len(predictions)},
    ]
    for feat, data in weights.get("features", {}).items():
        meth_rows.append({
            "Parameter": f"Weight: {feat}",
            "Value": f"{data.get('weight', 0)} (lift={data.get('lift', 0)})"
        })
    meth_df = pd.DataFrame(meth_rows)

    with pd.ExcelWriter(filepath, engine="openpyxl") as writer:
        pred_df.to_excel(writer, sheet_name="Predictions", index=False)
        signal_df.to_excel(writer, sheet_name="Signal Analysis", index=False)
        ctx_df.to_excel(writer, sheet_name="Market Context", index=False)
        meth_df.to_excel(writer, sheet_name="Methodology", index=False)

    print(f"    Excel saved: {filepath}")


# ═══════════════════════════════════════════════════════════════════════════════
# FORWARD TEST TRACKING
# ═══════════════════════════════════════════════════════════════════════════════

def _load_predictions() -> List[dict]:
    """Load all predictions from JSONL."""
    if not os.path.exists(PREDICTIONS_FILE):
        return []
    records = []
    with open(PREDICTIONS_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def _load_tracker() -> List[dict]:
    """Load existing tracking records."""
    if not os.path.exists(TRACKER_FILE):
        return []
    records = []
    with open(TRACKER_FILE, "r") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:
                    pass
    return records


def track_outcomes():
    """Update forward test outcomes for all unresolved predictions."""
    predictions = _load_predictions()
    if not predictions:
        print("No predictions to track.")
        return

    existing_tracker = _load_tracker()
    tracked_keys = {(r["symbol"], r["prediction_date"]) for r in existing_tracker
                    if r.get("tracking", {}).get(str(TRACKING_WINDOWS[-1]))}

    unresolved = [p for p in predictions
                  if (p["symbol"], p["prediction_date"]) not in tracked_keys]

    if not unresolved:
        print("All predictions already tracked.")
        return

    print(f"\n  Tracking outcomes for {len(unresolved)} unresolved predictions...")

    tickers = list(set(p["symbol"] for p in unresolved))
    oldest_date = min(p["prediction_date"] for p in unresolved)
    start = datetime.date.fromisoformat(oldest_date)
    end = datetime.date.today()

    ohlcv_data = _load_ohlcv_universe(tickers, start, end)

    new_records = []
    for pred in unresolved:
        symbol = pred["symbol"]
        pred_date = datetime.date.fromisoformat(pred["prediction_date"])
        close_at_pred = pred.get("close_at_prediction", 0)

        df = ohlcv_data.get(symbol)
        if df is None or df.empty or close_at_pred <= 0:
            continue

        close_col = "Close" if "Close" in df.columns else "close"
        high_col = "High" if "High" in df.columns else "high"
        low_col = "Low" if "Low" in df.columns else "low"

        after_pred = df[df.index > pd.Timestamp(pred_date)]
        if after_pred.empty:
            continue

        tracking = {}
        best_gain = 0
        best_window = None
        days_to_hit = None

        for window in TRACKING_WINDOWS:
            window_df = after_pred.iloc[:window]
            if window_df.empty:
                tracking[str(window)] = None
                continue

            max_high = float(window_df[high_col].max()) \
                if high_col in window_df.columns else float(window_df[close_col].max())
            min_low = float(window_df[low_col].min()) \
                if low_col in window_df.columns else float(window_df[close_col].min())
            max_gain_pct = round(
                (max_high - close_at_pred) / close_at_pred * 100, 2)
            max_drawdown_pct = round(
                (close_at_pred - min_low) / close_at_pred * 100, 2)
            hit = max_gain_pct >= PREDICTION_TARGET_PCT

            if hit and days_to_hit is None:
                for j in range(len(window_df)):
                    day_high = float(window_df[high_col].iloc[j]) \
                        if high_col in window_df.columns \
                        else float(window_df[close_col].iloc[j])
                    if (day_high - close_at_pred) / close_at_pred * 100 \
                            >= PREDICTION_TARGET_PCT:
                        days_to_hit = j + 1
                        break

            tracking[str(window)] = {
                "max_high": round(max_high, 2),
                "max_gain_pct": max_gain_pct,
                "max_drawdown_pct": max_drawdown_pct,
                "hit": hit,
            }
            if hit and days_to_hit:
                tracking[str(window)]["days_to_hit"] = days_to_hit

            if max_gain_pct > best_gain:
                best_gain = max_gain_pct
                best_window = str(window)

        record = {
            "prediction_date": pred["prediction_date"],
            "symbol": symbol,
            "rank": pred.get("rank", 0),
            "composite_score": pred.get("composite_score", 0),
            "close_at_prediction": close_at_pred,
            "tracking": tracking,
            "best_outcome": {
                "window": best_window,
                "max_gain_pct": best_gain,
                "days_to_hit": days_to_hit,
            },
            "tracking_date": datetime.date.today().isoformat(),
        }
        new_records.append(record)

    if new_records:
        with open(TRACKER_FILE, "a") as f:
            for r in new_records:
                f.write(json.dumps(r, default=str) + "\n")
        print(f"  Tracked {len(new_records)} predictions → {TRACKER_FILE}")

    _print_tracking_summary(existing_tracker + new_records)


def _print_tracking_summary(tracker: List[dict]):
    """Print a summary of tracking results."""
    if not tracker:
        return

    total = len(tracker)
    print(f"\n{'='*70}")
    print(f"  FORWARD TEST SUMMARY")
    print(f"{'='*70}")
    print(f"  Total tracked predictions: {total}")

    for window in TRACKING_WINDOWS:
        key = str(window)
        resolved = [t for t in tracker if t.get("tracking", {}).get(key)]
        hits = [t for t in resolved
                if t.get("tracking", {}).get(key, {}).get("hit")]
        if resolved:
            rate = len(hits) / len(resolved) * 100
            print(f"    {window:2d}-day:  {len(hits):4d}/{len(resolved):4d} = {rate:5.1f}%")

    print(f"{'='*70}\n")


def print_stats():
    """Print comprehensive prediction accuracy statistics."""
    tracker = _load_tracker()
    predictions = _load_predictions()

    print(f"\n{'='*70}")
    print(f"  PREDICTION ACCURACY STATS")
    print(f"{'='*70}")

    if not predictions:
        print("  No predictions found. Run predictions first.")
        print(f"{'='*70}")
        return

    pred_dates = list(set(p.get("prediction_date", "") for p in predictions))
    print(f"  Total predictions: {len(predictions)} "
          f"(across {len(pred_dates)} trading days)")

    if not tracker:
        print("  No tracking data yet. Run --track first.")
        print(f"{'='*70}")
        return

    # --- Hit Rate by Window ---
    print(f"\n  Hit Rate by Window:")
    for window in TRACKING_WINDOWS:
        key = str(window)
        resolved = [t for t in tracker if t.get("tracking", {}).get(key)]
        hits = [t for t in resolved
                if t.get("tracking", {}).get(key, {}).get("hit")]
        if resolved:
            rate = len(hits) / len(resolved) * 100
            print(f"    {window:2d}-day:  {len(hits):4d}/{len(resolved):4d} = {rate:5.1f}%")

    # --- Hit Rate by Rank Bucket ---
    print(f"\n  Hit Rate by Rank Bucket (5-day):")
    buckets = [(1, 5, "Top 5"), (6, 10, "Rank 6-10"), (11, 20, "Rank 11-20")]
    for lo, hi, label in buckets:
        resolved = [t for t in tracker
                    if lo <= t.get("rank", 0) <= hi
                    and t.get("tracking", {}).get("5")]
        hits = [t for t in resolved
                if t.get("tracking", {}).get("5", {}).get("hit")]
        if resolved:
            rate = len(hits) / len(resolved) * 100
            print(f"    {label:12s}: {len(hits):3d}/{len(resolved):3d} = {rate:5.1f}%")

    # --- Signal Effectiveness ---
    print(f"\n  Signal Effectiveness (5-day hit rate when present):")
    for feat in DEFAULT_WEIGHTS:
        relevant_preds = [p for p in predictions
                          if feat in p.get("active_signals", [])]
        tracked_symbols = {(t["symbol"], t["prediction_date"]) for t in tracker
                           if t.get("tracking", {}).get("5")}
        resolved = [p for p in relevant_preds
                    if (p["symbol"], p["prediction_date"]) in tracked_symbols]
        hits = []
        for p in resolved:
            for t in tracker:
                if t["symbol"] == p["symbol"] and \
                        t["prediction_date"] == p["prediction_date"]:
                    if t.get("tracking", {}).get("5", {}).get("hit"):
                        hits.append(t)
                    break
        if resolved:
            rate = len(hits) / len(resolved) * 100
            print(f"    {feat:22s}: {len(hits):3d}/{len(resolved):3d} = {rate:5.1f}%")

    # --- Avg Gain / Drawdown ---
    gains = []
    drawdowns = []
    for t in tracker:
        d5 = t.get("tracking", {}).get("5", {})
        if d5:
            gains.append(d5.get("max_gain_pct", 0))
            drawdowns.append(d5.get("max_drawdown_pct", 0))
    if gains:
        hit_gains = [g for g in gains if g >= PREDICTION_TARGET_PCT]
        miss_dds = [drawdowns[i] for i, g in enumerate(gains)
                    if g < PREDICTION_TARGET_PCT]
        print(f"\n  Avg max gain (winners, 5d): "
              f"{np.mean(hit_gains):.1f}%" if hit_gains else "")
        print(f"  Avg max drawdown (losers, 5d): "
              f"{np.mean(miss_dds):.1f}%" if miss_dds else "")

    print(f"\n{'='*70}")


# ═══════════════════════════════════════════════════════════════════════════════
# SELF-IMPROVEMENT LOOPS
# ═══════════════════════════════════════════════════════════════════════════════

def improve_weekly():
    """Analyze recent misses to identify false positive patterns."""
    tracker = _load_tracker()
    predictions = _load_predictions()

    if not tracker:
        print("No tracking data. Run --track first.")
        return

    cutoff = (datetime.date.today() - datetime.timedelta(days=14)).isoformat()
    recent_tracker = [t for t in tracker if t.get("prediction_date", "") >= cutoff]

    if not recent_tracker:
        print("No recent tracked predictions (past 14 days).")
        return

    hits_5d = [t for t in recent_tracker
               if t.get("tracking", {}).get("5", {}).get("hit")]
    misses_5d = [t for t in recent_tracker
                 if t.get("tracking", {}).get("5")
                 and not t.get("tracking", {}).get("5", {}).get("hit")]

    print(f"\n  Weekly Improvement Analysis")
    print(f"  Recent predictions: {len(recent_tracker)}")
    print(f"  5-day hits: {len(hits_5d)}, misses: {len(misses_5d)}")

    if not misses_5d:
        print("  No misses to analyze!")
        return

    pred_map = {}
    for p in predictions:
        pred_map[(p["symbol"], p["prediction_date"])] = p

    miss_details = []
    for t in misses_5d[:15]:
        pred = pred_map.get((t["symbol"], t["prediction_date"]), {})
        miss_details.append(
            f"  {t['symbol']} ({t['prediction_date']}): "
            f"composite={t.get('composite_score', '?')}, "
            f"signals={pred.get('active_signals', [])}, "
            f"gain={t.get('tracking', {}).get('5', {}).get('max_gain_pct', 0)}%, "
            f"drawdown={t.get('tracking', {}).get('5', {}).get('max_drawdown_pct', 0)}%"
        )

    hit_details = []
    for t in hits_5d[:10]:
        pred = pred_map.get((t["symbol"], t["prediction_date"]), {})
        hit_details.append(
            f"  {t['symbol']} ({t['prediction_date']}): "
            f"composite={t.get('composite_score', '?')}, "
            f"signals={pred.get('active_signals', [])}, "
            f"gain={t.get('tracking', {}).get('5', {}).get('max_gain_pct', 0)}%"
        )

    user_prompt = f"""MISSION: Analyze these prediction misses and identify systematic false positive patterns so we can improve accuracy.

MISSES (predicted 5%+ move, did NOT happen in 5 days):
{chr(10).join(miss_details)}

HITS (predicted 5%+ move, DID happen in 5 days):
{chr(10).join(hit_details)}

Analyze:
1. What patterns do the misses share that the hits don't?
2. Are there specific signals that are unreliable?
3. Are there market conditions where predictions fail?
4. What rules would filter out these false positives?

Respond in JSON:
{{
  "patterns": ["pattern 1 causing false positives", "pattern 2"],
  "unreliable_signals": ["signal that correlates with misses"],
  "suggested_weight_adjustments": {{"feature_name": delta_int}},
  "new_exclusion_rules": ["rule to exclude false positives"],
  "confidence": 0-100,
  "summary": "2-3 sentence summary of findings"
}}"""

    system = ("You are a quantitative trading analyst reviewing prediction "
              "model performance for Indian stocks.")

    print("\n  Sending miss analysis to LLM...")
    result = llm_json(system, user_prompt, max_tokens=2000)

    if result:
        print(f"\n  {'='*60}")
        print(f"  WEEKLY IMPROVEMENT FINDINGS")
        print(f"  {'='*60}")
        print(f"\n  Summary: {result.get('summary', 'N/A')}")
        print(f"  Confidence: {result.get('confidence', '?')}/100")

        patterns = result.get("patterns", [])
        if patterns:
            print(f"\n  False Positive Patterns:")
            for p in patterns:
                print(f"    • {p}")

        unreliable = result.get("unreliable_signals", [])
        if unreliable:
            print(f"\n  Unreliable Signals:")
            for s in unreliable:
                print(f"    • {s}")

        adjustments = result.get("suggested_weight_adjustments", {})
        if adjustments:
            print(f"\n  Suggested Weight Adjustments:")
            for feat, delta in adjustments.items():
                print(f"    {feat}: {'+' if delta > 0 else ''}{delta}")

        rules = result.get("new_exclusion_rules", [])
        if rules:
            print(f"\n  Suggested Exclusion Rules:")
            for r in rules:
                print(f"    • {r}")

        print(f"\n  Note: These are suggestions only. Run --improve-weights "
              f"to apply weight changes.")
        print(f"  {'='*60}")
    else:
        print("  LLM analysis failed.")


def improve_weights():
    """Retrain signal weights using forward test data blended with autopsy data."""
    tracker = _load_tracker()
    predictions = _load_predictions()

    if not tracker:
        print("No tracking data. Run --track first.")
        return

    current_weights = load_signal_weights()

    pred_map = {}
    for p in predictions:
        pred_map[(p["symbol"], p["prediction_date"])] = p

    print(f"\n  Retraining weights from {len(tracker)} tracked predictions...")

    feature_names = list(DEFAULT_WEIGHTS.keys())
    forward_stats = {}

    for feat in feature_names:
        n_with_hit = 0
        n_with_total = 0
        n_without_hit = 0
        n_without_total = 0

        for t in tracker:
            pred = pred_map.get((t["symbol"], t["prediction_date"]), {})
            feat_active = feat in pred.get("active_signals", [])
            hit = t.get("tracking", {}).get("5", {}).get("hit", False)

            if feat_active:
                n_with_total += 1
                if hit:
                    n_with_hit += 1
            else:
                n_without_total += 1
                if hit:
                    n_without_hit += 1

        total = n_with_total + n_without_total
        total_hits = n_with_hit + n_without_hit
        p_hit = total_hits / total if total > 0 else 0

        if n_with_total > 0 and p_hit > 0:
            p_hit_given_feat = n_with_hit / n_with_total
            forward_lift = p_hit_given_feat / p_hit
        else:
            forward_lift = 1.0

        forward_stats[feat] = {
            "lift": round(forward_lift, 3),
            "n_with": n_with_total,
            "hit_rate": round(n_with_hit / n_with_total * 100, 1) if n_with_total > 0 else 0,
        }

    n_tracked = len(tracker)
    if n_tracked < 100:
        alpha = 0.7
    elif n_tracked < 500:
        alpha = 0.5
    else:
        alpha = 0.3

    print(f"  Blending ratio: {alpha:.0%} autopsy + {1-alpha:.0%} forward test "
          f"(N={n_tracked})")

    new_features = {}
    for feat in feature_names:
        autopsy_lift = current_weights.get("features", {}).get(
            feat, {}).get("lift", 1.0)
        forward_lift = forward_stats[feat]["lift"]
        blended_lift = alpha * autopsy_lift + (1 - alpha) * forward_lift
        raw_weight = max(1, min(30, round(blended_lift * 10)))
        new_features[feat] = {
            "lift": round(blended_lift, 3),
            "autopsy_lift": round(autopsy_lift, 3),
            "forward_lift": round(forward_lift, 3),
            "raw_weight": raw_weight,
        }

    total_raw = sum(f["raw_weight"] for f in new_features.values())
    for feat in new_features:
        new_features[feat]["weight"] = round(
            new_features[feat]["raw_weight"] / total_raw * 100, 1)
        new_features[feat]["coverage"] = current_weights.get(
            "features", {}).get(feat, {}).get("coverage", 0)

    # --- Print comparison ---
    print(f"\n  {'Feature':<22s} {'Old Wt':>7s} {'New Wt':>7s} "
          f"{'A-Lift':>7s} {'F-Lift':>7s} {'Fwd HR':>7s}")
    print(f"  {'-'*58}")
    for feat in feature_names:
        old_w = current_weights.get("features", {}).get(
            feat, {}).get("weight", 0)
        new_w = new_features[feat]["weight"]
        a_lift = new_features[feat]["autopsy_lift"]
        f_lift = new_features[feat]["forward_lift"]
        fwd_hr = forward_stats[feat]["hit_rate"]
        delta = "↑" if new_w > old_w else "↓" if new_w < old_w else "="
        print(f"  {feat:<22s} {old_w:6.1f} {new_w:6.1f} {delta}  "
              f"{a_lift:6.3f} {f_lift:6.3f} {fwd_hr:5.1f}%")

    updated_weights = {
        "version": current_weights.get("version", 0) + 1,
        "updated": datetime.date.today().isoformat(),
        "sample_size": current_weights.get("sample_size", 0),
        "forward_test_size": n_tracked,
        "blend_alpha": alpha,
        "base_rate": current_weights.get("base_rate", 0),
        "features": new_features,
        "combination_bonuses": current_weights.get("combination_bonuses", {}),
    }

    with open(WEIGHTS_FILE, "w") as f:
        json.dump(updated_weights, f, indent=2)
    print(f"\n  Updated weights saved to {WEIGHTS_FILE} (v{updated_weights['version']})")


def reclassify_unknowns():
    """Re-classify UNKNOWN trigger types in the autopsy database."""
    records = load_autopsy_database()
    if not records:
        print("No autopsy database found.")
        return

    unknowns = [(i, r) for i, r in enumerate(records)
                if r.get("autopsy", {}).get("trigger_type") == "UNKNOWN"]

    if not unknowns:
        print("No UNKNOWN records to reclassify.")
        return

    print(f"\n  Found {len(unknowns)} UNKNOWN records to reclassify")

    classified = {r.get("autopsy", {}).get("trigger_type"): 0 for r in records}
    for r in records:
        t = r.get("autopsy", {}).get("trigger_type", "UNKNOWN")
        classified[t] = classified.get(t, 0) + 1

    pattern_summary = ", ".join(
        f"{t}: {c}" for t, c in sorted(classified.items(), key=lambda x: -x[1])
        if t != "UNKNOWN")

    batch_size = 8
    reclassified = 0
    total_batches = (len(unknowns) + batch_size - 1) // batch_size

    for batch_idx in range(0, len(unknowns), batch_size):
        batch = unknowns[batch_idx:batch_idx + batch_size]
        batch_num = batch_idx // batch_size + 1

        stock_sections = []
        for idx, record in batch:
            enrichment = record.get("enrichment", {})
            ohlcv = enrichment.get("ohlcv_summary", {})
            stock_sections.append(
                f"  {record['symbol']} (+{record.get('move_pct', '?')}% on "
                f"{record.get('move_date', '?')}): "
                f"vol_trend={ohlcv.get('vol_trend', '?')}, "
                f"near_high={ohlcv.get('near_30d_high', '?')}%, "
                f"delivery_rising={enrichment.get('delivery_rising', '?')}, "
                f"deals={enrichment.get('recent_deals') or 'None'}, "
                f"superstars={enrichment.get('superstar_buyers') or 'None'}, "
                f"earnings={enrichment.get('earnings_growth', '?')}%, "
                f"fii_chg={enrichment.get('fii_change_qoq', '?')}pp"
            )

        user_prompt = f"""These stocks were classified as UNKNOWN trigger type. Re-classify using patterns from {len(records)} autopsied records:

DATABASE PATTERNS: {pattern_summary}

KEY DISTINGUISHING FEATURES:
- TECHNICAL_BREAKOUT: vol_trend rising + near 30d high + ATR compression
- SECTOR_ROTATION: sector RS top quartile + multiple stocks in same sector moving
- INSTITUTIONAL: superstar buyer present + delivery rising
- OPERATOR_BULK: bulk deal by unknown entity + sharp volume spike
- SHORT_SQUEEZE: delivery spike > 70% + low float indicators
- EARNINGS: earnings/revenue growth > 20% QoQ
- CORPORATE_EVENT: specific announcement present

STOCKS TO RE-CLASSIFY:
{chr(10).join(stock_sections)}

Respond in JSON:
{{
  "results": [
    {{
      "symbol": "TICKER",
      "trigger_type": "CATEGORY",
      "confidence": 0-100,
      "thesis": "Why this classification"
    }}
  ]
}}"""

        system = ("You are a stock market analyst reclassifying trigger types "
                  "for Indian stock moves using pattern matching.")

        print(f"    Batch {batch_num}/{total_batches}...")

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                fut = executor.submit(llm_json, system, user_prompt, 2000)
                result = fut.result(timeout=120)
        except (FutTimeout, Exception) as e:
            print(f"    Batch {batch_num} error: {e}")
            continue

        if not result:
            continue

        results = result.get("results", [])
        result_map = {r.get("symbol", ""): r for r in results
                      if isinstance(r, dict)}

        for idx, record in batch:
            sym = record["symbol"]
            if sym in result_map:
                new_class = result_map[sym]
                new_trigger = new_class.get("trigger_type", "UNKNOWN")
                if new_trigger != "UNKNOWN":
                    records[idx]["autopsy"]["trigger_type"] = new_trigger
                    records[idx]["autopsy"]["confidence"] = \
                        new_class.get("confidence", 50)
                    records[idx]["autopsy"]["thesis"] = \
                        new_class.get("thesis",
                                      records[idx]["autopsy"].get("thesis", ""))
                    reclassified += 1

        time.sleep(1)

    # --- Rewrite database ---
    if reclassified > 0:
        with open(DB_FILE, "w") as f:
            for r in records:
                f.write(json.dumps(r, default=str) + "\n")
        print(f"\n  Reclassified {reclassified}/{len(unknowns)} UNKNOWN records")
        print(f"  Database rewritten: {DB_FILE}")
    else:
        print("  No records were reclassified.")


# ═══════════════════════════════════════════════════════════════════════════════
# DAILY PIPELINE ORCHESTRATOR
# ═══════════════════════════════════════════════════════════════════════════════

def run_predictions(target_date: datetime.date, use_llm: bool = True,
                    top_n: int = FINAL_PICKS):
    """Run the full prediction pipeline for a given date."""
    date_str = target_date.strftime("%Y-%m-%d")
    print(f"\n{'='*70}")
    print(f"  RUNNER PREDICTOR — {target_date.strftime('%d-%b-%Y')}")
    print(f"  Target: {PREDICTION_TARGET_PCT}%+ move within 1-5 days")
    print(f"{'='*70}")

    # Step 1: Load signal weights
    print(f"\n1. Loading signal weights...")
    weights = load_signal_weights()
    print(f"   Version: {weights.get('version', '?')}, "
          f"sample: {weights.get('sample_size', '?')} autopsy records")

    # Step 2: Load OHLCV universe
    print(f"\n2. Loading OHLCV universe from cache...")
    tickers = load_universe()
    print(f"   {len(tickers)} tickers in universe")
    start = target_date - datetime.timedelta(days=200)
    ohlcv_data = _load_ohlcv_universe(tickers, start, target_date)

    # Step 3: Score all stocks
    print(f"\n3. Extracting features and scoring universe...")
    candidates = score_universe(ohlcv_data, weights)
    print(f"   Top statistical score: {candidates[0]['stat_score'] if candidates else 0}")

    if not candidates:
        print("   No candidates found. Check OHLCV cache.")
        return

    # Step 4: Take top 100
    stat_shortlist = candidates[:STAT_SHORTLIST_SIZE]
    print(f"\n4. Statistical shortlist: {len(stat_shortlist)} candidates")

    if not use_llm:
        # Stat-only mode: rank by stat score alone
        for cand in stat_shortlist:
            cand["llm_conviction"] = 0
            cand["composite_score"] = cand["stat_score"]
            cand["expected_trigger"] = "STAT_ONLY"
            cand["thesis"] = ""
            cand["risk"] = ""
            cand["timeframe_days"] = 5
            cand["signals_aligned"] = cand.get("active_signals", [])
            cand["entry_zone"] = ""
            cand["stop_loss_pct"] = 5
            cand["target_pct"] = 10

        ranked = rank_predictions(stat_shortlist, top_n)
        print_predictions(ranked, date_str)
        save_predictions(ranked, date_str)
        save_excel(ranked, {}, weights, date_str)
        return

    # Step 5: Gather market context
    print(f"\n5. Gathering market context...")
    ctx = gather_context()

    # Step 6: Enrich top 100
    print(f"\n6. Enriching top {len(stat_shortlist)} candidates...")
    enriched = enrich_candidates(stat_shortlist, ctx)

    # Step 7: Re-score with enrichment, take top 50
    print(f"\n7. Re-scoring with enrichment data...")
    for cand in enriched:
        score, active = compute_stat_score(cand.get("features", {}), weights)
        cand["stat_score"] = score
        cand["active_signals"] = active

    enriched.sort(key=lambda x: x["stat_score"], reverse=True)
    llm_shortlist = enriched[:LLM_SHORTLIST_SIZE]
    print(f"   LLM shortlist: {len(llm_shortlist)} candidates")

    # Step 8: LLM deep analysis
    print(f"\n8. LLM deep analysis on top {len(llm_shortlist)} candidates...")
    analyzed = run_llm_analysis(llm_shortlist, ctx)

    # Step 9: Rank and output
    print(f"\n9. Ranking by composite score...")
    ranked = rank_predictions(analyzed, top_n)

    # Step 10: Output
    print(f"\n10. Saving results...")
    print_predictions(ranked, date_str)
    save_predictions(ranked, date_str)
    save_excel(ranked, ctx, weights, date_str)

    print(f"  Pipeline complete. {len(ranked)} predictions saved.")


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Runner Predictor — Predict 5%+ stock moves")
    parser.add_argument("--date", type=str, default=None,
                        help="Target date (YYYY-MM-DD), default today")
    parser.add_argument("--track", action="store_true",
                        help="Update forward test outcomes")
    parser.add_argument("--stats", action="store_true",
                        help="Show prediction accuracy stats")
    parser.add_argument("--improve-weekly", action="store_true",
                        help="Analyze recent misses for patterns")
    parser.add_argument("--improve-weights", action="store_true",
                        help="Retrain signal weights from forward test data")
    parser.add_argument("--reclassify", action="store_true",
                        help="Re-classify UNKNOWN triggers in autopsy DB")
    parser.add_argument("--build-weights", action="store_true",
                        help="Force rebuild signal weights from autopsy DB")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM analysis (stat scores only)")
    parser.add_argument("--top", type=int, default=FINAL_PICKS,
                        help=f"Number of final picks (default {FINAL_PICKS})")
    args = parser.parse_args()

    if args.stats:
        print_stats()
        return
    if args.track:
        track_outcomes()
        return
    if args.improve_weekly:
        improve_weekly()
        return
    if args.improve_weights:
        improve_weights()
        return
    if args.reclassify:
        reclassify_unknowns()
        return
    if args.build_weights:
        build_weights_from_database()
        return

    target_date = datetime.date.fromisoformat(args.date) \
        if args.date else datetime.date.today()
    run_predictions(target_date, use_llm=not args.no_llm, top_n=args.top)


if __name__ == "__main__":
    main()
