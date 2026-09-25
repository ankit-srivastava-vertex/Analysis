"""
Breakout Daily Tracker v3.5 — Full LLM-Integrated Adaptive Scanner
====================================================================

Single orchestrator that runs both scanners, enriches picks with data
from 12+ modules, applies multi-layer LLM intelligence (autopsy
cross-reference, kill signal check, conviction court, supercharged
synthesis), tracks outcomes over 5-10 days, auto-tunes parameters,
mines failure patterns, and generates targeted code patches.

LLM INTEGRATION POINTS:
  1. LIVE SCAN FILTERING — LLM reviews raw picks, drops low-conviction
  2. MULTI-SIGNAL ENRICHMENT — Delivery, deals, OI, sector, stage,
     fundamentals, announcements attached to each pick
  3. AUTOPSY CROSS-REFERENCE — Matches each pick against 3,958
     historical breakout records for pattern similarity scoring
  4. ANALOGUE SYNTHESIS — LLM evaluates historical analogues for
     each candidate (encouraging/neutral/concerning verdict)
  5. KILL SIGNAL CHECK — Dedicated adversarial LLM pass finds
     reasons to REJECT (hard kills removed, soft warnings kept)
  6. MASTER SYNTHESIS — LLM reads ALL enriched data + analogues +
     kill signals + cross-scanner consensus. Conviction floor ≥60.
  7. OUTCOME JUDGEMENT — Flexible 5-10 day window, LLM judges success
  8. FAILURE PATTERN MINING — Multi-factor interaction analysis
     (runs before patching to feed insights to patch generator)
  9. PARAMETER TUNING — JSON overrides + new_rules_suggested saved
 10. CODE PATCH GENERATION — Mining-informed Python patches

MODULES WIRED (imported as-is, zero changes):
  - breakout_v5          — V5.1 scanner (cached OHLCV)
  - breakout_scanner_angel — V4.4 scanner (live Angel download)
  - BulkBlock            — BSE/NSE bulk/block deals
  - investor_registry    — Superstar investor names
  - fii_flows            — FII/DII daily cash flows
  - fno_max_oi           — F&O max open interest
  - nse_ready_sectors    — Sector RS rankings
  - stage_analysis       — Mansfield stage classification
  - tickertape_client    — Fundamentals (revenue/profit growth)
  - forensic_accounting  — NSE corporate announcements
  - jugaad_data          — Delivery percentage data
  - angel_client         — Angel One API adapter
  - llm_client           — Unified Azure OpenAI client

TARGET: >90% success rate. Fed to every LLM prompt.

OUTPUT:
  Output/breakout_tracker_history.xlsx (5 sheets, appended daily)
  Output/breakout_llm_params.json (LLM-tuned parameters, auto-loaded)
  Output/breakout_llm_rules.json (LLM-suggested new rules, accumulated)
  Output/breakout_patches/ (LLM-generated code patches)
  Output/breakout_failure_patterns.json (multi-factor failure clusters)

Usage:
  python3 breakout_daily_tracker.py          # full run (both scanners + enrichment + LLM)
  python3 breakout_daily_tracker.py --no-v4  # V5.1 only (faster)
  python3 breakout_daily_tracker.py --no-llm # skip Azure OpenAI analysis
  python3 breakout_daily_tracker.py --no-enrich  # skip enrichment (fast scan only)
  python3 breakout_daily_tracker.py --backtrack-only  # just check past picks
"""

import os, sys, json, datetime, argparse, traceback, warnings, time, re
from typing import Optional, Dict, List, Any
import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "Output")
PATCH_DIR = os.path.join(OUTPUT_DIR, "breakout_patches")
HISTORY_FILE = os.path.join(OUTPUT_DIR, "breakout_tracker_history.xlsx")
LLM_PARAMS_FILE = os.path.join(OUTPUT_DIR, "breakout_llm_params.json")
CONTEXT_CACHE_FILE = os.path.join(OUTPUT_DIR, "breakout_context_cache.json")
TODAY = datetime.date.today()
TODAY_STR = TODAY.strftime("%Y-%m-%d")

os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(PATCH_DIR, exist_ok=True)

TARGET_SUCCESS_RATE = 90

sys.path.insert(0, SCRIPT_DIR)


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CLIENT — Unified (via llm_client.py)
# ═══════════════════════════════════════════════════════════════════════════════

from llm_client import llm_call, llm_json, is_available as llm_is_available


# ═══════════════════════════════════════════════════════════════════════════════
# LLM-TUNED PARAMETERS — Auto-loaded overrides
# ═══════════════════════════════════════════════════════════════════════════════

def load_llm_params() -> dict:
    """Load LLM-generated parameter overrides if they exist."""
    if os.path.exists(LLM_PARAMS_FILE):
        try:
            with open(LLM_PARAMS_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def apply_llm_params_to_v5():
    """Apply LLM-generated parameter overrides to breakout_v5.P."""
    params = load_llm_params()
    if not params:
        return

    import breakout_v5 as bv
    applied = []
    for key, val in params.items():
        if key.startswith("_") or key == "last_updated":
            continue
        if hasattr(bv.P, key):
            old = getattr(bv.P, key)
            if old != val:
                setattr(bv.P, key, val)
                applied.append(f"{key}: {old} -> {val}")

    if applied:
        print(f"  Applied {len(applied)} LLM parameter overrides:")
        for a in applied[:10]:
            print(f"    {a}")


def save_llm_params(params: dict):
    """Save LLM-generated parameter overrides."""
    params["last_updated"] = TODAY_STR
    with open(LLM_PARAMS_FILE, "w") as f:
        json.dump(params, f, indent=2, default=str)
    print(f"  Saved LLM params: {LLM_PARAMS_FILE}")


# ═══════════════════════════════════════════════════════════════════════════════
# GATHER CONTEXT — One call to each module, build market context dict
# ═══════════════════════════════════════════════════════════════════════════════

def _is_cache_fresh(key: str, max_age_hours: int = 12) -> bool:
    """Check if cached context is recent enough to reuse."""
    if not os.path.exists(CONTEXT_CACHE_FILE):
        return False
    try:
        with open(CONTEXT_CACHE_FILE) as f:
            cache = json.load(f)
        ts = cache.get(f"{key}_ts", "")
        if not ts:
            return False
        cached_dt = datetime.datetime.fromisoformat(ts)
        return (datetime.datetime.now() - cached_dt).total_seconds() < max_age_hours * 3600
    except Exception:
        return False


def _save_context_cache(key: str, data: Any):
    """Save context data to disk cache."""
    try:
        cache = {}
        if os.path.exists(CONTEXT_CACHE_FILE):
            with open(CONTEXT_CACHE_FILE) as f:
                cache = json.load(f)
        cache[key] = data
        cache[f"{key}_ts"] = datetime.datetime.now().isoformat()
        with open(CONTEXT_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2, default=str)
    except Exception:
        pass


def _load_context_cache(key: str) -> Any:
    """Load context data from disk cache."""
    try:
        with open(CONTEXT_CACHE_FILE) as f:
            cache = json.load(f)
        return cache.get(key)
    except Exception:
        return None


def gather_context(skip_expensive: bool = False) -> dict:
    """Gather market-wide context from all modules. Each call is wrapped
    in try/except so a single module failure never kills the run.

    Returns a dict with keys: fii_today, sector_rs, sector_ranking_df,
    stage2_sectors, stage2_stocks, deals_df, superstar_names, fno_oi_df,
    market_regime.
    """
    ctx: Dict[str, Any] = {}
    is_monday = TODAY.weekday() == 0

    # ── FII/DII flows (fii_flows.py) ──
    print("  [ctx] FII/DII flows...")
    try:
        import fii_flows
        fii_today = fii_flows.fetch_today()
        ctx["fii_today"] = fii_today
        if not fii_today.empty:
            fii_row = fii_today[
                fii_today["category"].str.contains("FII|FPI", case=False, na=False)
            ]
            if not fii_row.empty:
                ctx["fii_net"] = float(fii_row.iloc[0].get("netValue", 0))
            else:
                ctx["fii_net"] = 0
        else:
            ctx["fii_net"] = 0
        print(f"        FII net: {ctx['fii_net']:.0f} Cr")
    except Exception as e:
        print(f"        FII error (non-fatal): {e}")
        ctx["fii_today"] = pd.DataFrame()
        ctx["fii_net"] = 0

    # ── Sector RS rankings (nse_ready_sectors.py) — cache weekly ──
    print("  [ctx] Sector rankings...")
    if not skip_expensive and (is_monday or not _is_cache_fresh("sector_rs", 168)):
        try:
            import nse_ready_sectors
            result = nse_ready_sectors.run()
            if result is not None:
                all_rs, _, ranking_df, _, _, _ = result
                ctx["sector_rs"] = {k: round(float(v.iloc[-1]), 2)
                                    for k, v in all_rs.items()
                                    if hasattr(v, 'iloc') and len(v) > 0}
                ctx["sector_ranking_df"] = ranking_df
                _save_context_cache("sector_rs", ctx["sector_rs"])
                print(f"        {len(ctx['sector_rs'])} sectors ranked")
            else:
                ctx["sector_rs"] = _load_context_cache("sector_rs") or {}
                ctx["sector_ranking_df"] = pd.DataFrame()
        except Exception as e:
            print(f"        Sector RS error (non-fatal): {e}")
            ctx["sector_rs"] = _load_context_cache("sector_rs") or {}
            ctx["sector_ranking_df"] = pd.DataFrame()
    else:
        ctx["sector_rs"] = _load_context_cache("sector_rs") or {}
        ctx["sector_ranking_df"] = pd.DataFrame()
        print("        Using cached sector rankings")

    # ── Stage analysis (stage_analysis.py) — cache weekly ──
    print("  [ctx] Stage analysis...")
    if not skip_expensive and (is_monday or not _is_cache_fresh("stage2", 168)):
        try:
            import stage_analysis
            sa = stage_analysis.analyse(verbose=False)
            if sa:
                ctx["stage2_sectors"] = [
                    s.get("name", s.get("sector", ""))
                    for s in sa.get("sectors", [])
                    if s.get("stage") == 2
                ]
                ctx["stage2_stocks"] = [
                    s.get("symbol", "").replace(".NS", "").replace(".BO", "")
                    for s in sa.get("stocks", [])
                    if s.get("stage") == 2
                ]
                _save_context_cache("stage2_sectors", ctx["stage2_sectors"])
                _save_context_cache("stage2_stocks", ctx["stage2_stocks"])
                print(f"        {len(ctx['stage2_sectors'])} sectors, "
                      f"{len(ctx['stage2_stocks'])} stocks in Stage 2")
            else:
                ctx["stage2_sectors"] = _load_context_cache("stage2_sectors") or []
                ctx["stage2_stocks"] = _load_context_cache("stage2_stocks") or []
        except Exception as e:
            print(f"        Stage error (non-fatal): {e}")
            ctx["stage2_sectors"] = _load_context_cache("stage2_sectors") or []
            ctx["stage2_stocks"] = _load_context_cache("stage2_stocks") or []
    else:
        ctx["stage2_sectors"] = _load_context_cache("stage2_sectors") or []
        ctx["stage2_stocks"] = _load_context_cache("stage2_stocks") or []
        print("        Using cached stage analysis")

    # ── Bulk/Block deals (BulkBlock.py) ──
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
        ctx["deals_df"] = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        print(f"        {len(ctx['deals_df'])} deal records")
    except Exception as e:
        print(f"        Deals error (non-fatal): {e}")
        ctx["deals_df"] = pd.DataFrame()

    # ── Superstar investor names (investor_registry.py) ──
    try:
        import investor_registry
        ctx["superstar_names"] = investor_registry.all_bulk_deal_names()
        print(f"        {len(ctx['superstar_names'])} superstar investors loaded")
    except Exception as e:
        print(f"        Investor registry error (non-fatal): {e}")
        ctx["superstar_names"] = set()

    # ── F&O Max OI (fno_max_oi.py) ──
    print("  [ctx] F&O Max OI...")
    try:
        import fno_max_oi
        fno_result = fno_max_oi.run(expiry_type='weekly')
        if fno_result is not None:
            fno_df, _ = fno_result
            ctx["fno_oi_df"] = fno_df if fno_df is not None else pd.DataFrame()
        else:
            ctx["fno_oi_df"] = pd.DataFrame()
        print(f"        {len(ctx['fno_oi_df'])} F&O symbols")
    except Exception as e:
        print(f"        F&O OI error (non-fatal): {e}")
        ctx["fno_oi_df"] = pd.DataFrame()

    return ctx


# ═══════════════════════════════════════════════════════════════════════════════
# ENRICH PICKS — Attach per-stock signals from multiple sources
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_symbol(ticker: str) -> str:
    """Convert 'RELIANCE.NS' or '534109.BO' to bare symbol."""
    return ticker.replace(".NS", "").replace(".BO", "")


def _match_deals_to_stock(symbol: str, deals_df: pd.DataFrame) -> List[dict]:
    """Find deals matching a stock symbol in the deals DataFrame."""
    if deals_df.empty:
        return []
    sym_col = None
    for col in ("symbol", "Symbol", "Scrip Name"):
        if col in deals_df.columns:
            sym_col = col
            break
    if sym_col is None:
        return []
    mask = deals_df[sym_col].astype(str).str.contains(symbol, case=False, na=False)
    matches = deals_df[mask]
    if matches.empty:
        return []
    result = []
    for _, row in matches.head(10).iterrows():
        client = ""
        for col in ("clientName", "Client Name"):
            if col in row.index:
                client = str(row[col])
                break
        buy_sell = ""
        for col in ("buySell", "Buy/Sell"):
            if col in row.index:
                buy_sell = str(row[col])
                break
        qty = 0
        for col in ("qty", "Quantity"):
            if col in row.index:
                try:
                    qty = int(float(str(row[col]).replace(",", "")))
                except (ValueError, TypeError):
                    pass
                break
        result.append({"client": client, "buy_sell": buy_sell, "qty": qty})
    return result


def _match_superstar(deals: List[dict], superstar_names) -> List[str]:
    """Check if any deal involves a superstar investor."""
    matches = []
    if not superstar_names or not deals:
        return matches
    names_lower = {str(n).lower() for n in superstar_names if n}
    for d in deals:
        client = str(d.get("client", "")).lower()
        for name in names_lower:
            if name and name in client:
                matches.append(d.get("client", ""))
                break
    return matches


def enrich_picks(picks: List[dict], ctx: dict) -> List[dict]:
    """Attach delivery, deals, OI, sector, announcement, and fundamental
    signals to each pick. Only called for the top 15 picks to keep costs
    and time low.

    Modifies picks in-place and returns them.
    """
    if not picks:
        return picks

    top_picks = picks[:15]
    print(f"\n  Enriching {len(top_picks)} picks...")

    # ── Delivery data (jugaad_data) — batched ──
    print("  [enrich] Delivery %...")
    for pick in top_picks:
        symbol = _extract_symbol(pick["ticker"])
        try:
            from jugaad_data.nse import stock_df
            ddf = stock_df(
                symbol,
                TODAY - datetime.timedelta(days=35),
                TODAY,
                series="EQ",
            )
            if not ddf.empty and "DELIVERY %" in ddf.columns:
                d5 = ddf["DELIVERY %"].tail(5).mean()
                d20 = ddf["DELIVERY %"].tail(20).mean()
                pick["delivery_5d_avg"] = round(d5, 1) if not np.isnan(d5) else None
                pick["delivery_20d_avg"] = round(d20, 1) if not np.isnan(d20) else None
                pick["delivery_rising"] = bool(d5 > d20) if not (np.isnan(d5) or np.isnan(d20)) else None
            else:
                pick["delivery_5d_avg"] = None
                pick["delivery_20d_avg"] = None
                pick["delivery_rising"] = None
        except Exception:
            pick["delivery_5d_avg"] = None
            pick["delivery_20d_avg"] = None
            pick["delivery_rising"] = None
        time.sleep(0.3)

    # ── Bulk/Block deals ──
    print("  [enrich] Bulk/Block deals...")
    for pick in top_picks:
        symbol = _extract_symbol(pick["ticker"])
        deals = _match_deals_to_stock(symbol, ctx.get("deals_df", pd.DataFrame()))
        pick["recent_deals"] = deals[:5]
        pick["superstar_buyers"] = _match_superstar(
            deals, ctx.get("superstar_names", set()))

    # ── F&O OI confirmation ──
    print("  [enrich] F&O OI...")
    fno_df = ctx.get("fno_oi_df", pd.DataFrame())
    for pick in top_picks:
        symbol = _extract_symbol(pick["ticker"])
        R = pick.get("resistance", 0)
        pick["fno_call_oi_strike"] = None
        pick["fno_put_oi_strike"] = None
        pick["fno_confirms_R"] = False
        if not fno_df.empty and R > 0:
            sym_col = "Symbol" if "Symbol" in fno_df.columns else None
            if sym_col:
                match = fno_df[fno_df[sym_col].astype(str).str.contains(
                    symbol, case=False, na=False)]
                if not match.empty:
                    row = match.iloc[0]
                    call_strike = row.get("Call Strike", 0)
                    put_strike = row.get("Put Strike", 0)
                    try:
                        call_strike = float(call_strike)
                        put_strike = float(put_strike)
                    except (ValueError, TypeError):
                        call_strike, put_strike = 0, 0
                    pick["fno_call_oi_strike"] = call_strike
                    pick["fno_put_oi_strike"] = put_strike
                    if call_strike > 0 and R > 0:
                        pick["fno_confirms_R"] = abs(call_strike - R) / R < 0.03

    # ── Sector stage ──
    print("  [enrich] Sector stage...")
    stage2_stocks = ctx.get("stage2_stocks", [])
    for pick in top_picks:
        symbol = _extract_symbol(pick["ticker"])
        pick["in_stage2"] = symbol in stage2_stocks

    # ── FII context ──
    for pick in top_picks:
        pick["fii_net_cr"] = ctx.get("fii_net", 0)

    # ── NSE Announcements (last 15 days) ──
    print("  [enrich] NSE announcements...")
    for pick in top_picks:
        symbol = _extract_symbol(pick["ticker"])
        pick["announcements"] = []
        try:
            from forensic_accounting import _nse_session, _nse_get_json
            session = _nse_session()
            anns = _nse_get_json(session, "https://www.nseindia.com/api/corporate-announcements",
                                 params={"index": "equities", "symbol": symbol})
            if anns and isinstance(anns, list):
                recent = []
                cutoff = (TODAY - datetime.timedelta(days=15)).isoformat()
                for a in anns[:30]:
                    dt = a.get("an_dt", a.get("date", ""))
                    if dt >= cutoff:
                        recent.append({
                            "date": dt,
                            "subject": a.get("desc", a.get("subject", ""))[:120],
                        })
                pick["announcements"] = recent[:5]
        except Exception:
            pass
        time.sleep(0.3)

    # ── Fundamentals (tickertape_client.py) ──
    print("  [enrich] Fundamentals...")
    for pick in top_picks:
        symbol = _extract_symbol(pick["ticker"])
        pick["earnings_growth"] = None
        pick["revenue_growth"] = None
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
                                pick["revenue_growth"] = round(
                                    (float(rev_l) / float(rev_p) - 1) * 100, 1)
                            pat_l = vals_l.get("netIncome", vals_l.get("pat", 0))
                            pat_p = vals_p.get("netIncome", vals_p.get("pat", 0))
                            if pat_p and pat_l:
                                pick["earnings_growth"] = round(
                                    (float(pat_l) / float(pat_p) - 1) * 100, 1)
        except Exception:
            pass
        time.sleep(0.2)

    # ── Autopsy cross-reference ──
    print("  [enrich] Autopsy cross-reference...")
    autopsy_db = _load_autopsy_db()
    if autopsy_db:
        for pick in top_picks:
            analogues = find_autopsy_analogues(pick, autopsy_db)
            pick["autopsy_analogues"] = analogues
            if analogues:
                successes = sum(1 for a in analogues if a["succeeded"])
                pick["analogue_success_rate"] = round(
                    successes / len(analogues) * 100, 1)
                synthesis = llm_analogue_synthesis(pick, analogues)
                pick["analogue_verdict"] = (synthesis.get("analogue_verdict")
                                            if synthesis else None)
                pick["analogue_key_pattern"] = (synthesis.get("key_pattern")
                                                if synthesis else None)
                pick["analogue_red_flag"] = (synthesis.get("red_flag")
                                             if synthesis else None)
            else:
                pick["analogue_success_rate"] = None
                pick["analogue_verdict"] = None
                pick["analogue_key_pattern"] = None
                pick["analogue_red_flag"] = None
    else:
        for pick in top_picks:
            pick["autopsy_analogues"] = []
            pick["analogue_success_rate"] = None
            pick["analogue_verdict"] = None
            pick["analogue_key_pattern"] = None
            pick["analogue_red_flag"] = None

    enriched_count = sum(1 for p in top_picks
                         if p.get("delivery_5d_avg") is not None
                         or p.get("recent_deals")
                         or p.get("fno_confirms_R")
                         or p.get("in_stage2"))
    print(f"  Enrichment complete: {enriched_count}/{len(top_picks)} "
          f"picks have at least one signal")

    return picks


# ═══════════════════════════════════════════════════════════════════════════════
# AUTOPSY CROSS-REFERENCE ENGINE — Historical analogue matching
# ═══════════════════════════════════════════════════════════════════════════════

_AUTOPSY_DB_CACHE: Optional[List[dict]] = None


def _load_autopsy_db() -> List[dict]:
    """Load autopsy database from JSONL, filter to TECHNICAL_BREAKOUT only, cache in memory."""
    global _AUTOPSY_DB_CACHE
    if _AUTOPSY_DB_CACHE is not None:
        return _AUTOPSY_DB_CACHE

    db_path = os.path.join(OUTPUT_DIR, "autopsy_database.json")
    if not os.path.exists(db_path):
        print("  [autopsy] Database not found, skipping cross-reference")
        _AUTOPSY_DB_CACHE = []
        return _AUTOPSY_DB_CACHE

    try:
        records = []
        with open(db_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    autopsy = rec.get("autopsy", {})
                    if autopsy.get("trigger_type") == "TECHNICAL_BREAKOUT":
                        records.append(rec)
                except json.JSONDecodeError:
                    continue
        _AUTOPSY_DB_CACHE = records
        print(f"  [autopsy] Loaded {len(records)} TECHNICAL_BREAKOUT records")
    except Exception as e:
        print(f"  [autopsy] Load error: {e}")
        _AUTOPSY_DB_CACHE = []
    return _AUTOPSY_DB_CACHE


def find_autopsy_analogues(pick: dict, autopsy_db: List[dict]) -> List[dict]:
    """Rule-based similarity matching: find historical breakouts most similar to this pick.

    Scores across 5 dimensions:
      1. Same or adjacent sector (+3)
      2. Similar base_days within 30%/50% (+2/+1)
      3. Similar RS strength within 10/20 percentile (+2/+1)
      4. Volume profile match: vol_trend + squeeze (+1 each)
      5. Similar price range within 30% (+1)

    Returns top 10 most similar records with outcomes.
    """
    if not autopsy_db:
        return []

    pick_base = pick.get("base_days", 0)
    pick_rs = pick.get("rs_rating", 0)
    pick_squeeze = str(pick.get("squeeze_type", "")).lower()
    pick_close = pick.get("close", 0)
    pick_sector = str(pick.get("sector", "")).lower()

    scored = []
    for rec in autopsy_db:
        sim = 0
        enr = rec.get("enrichment", {}) or {}
        ohlcv_sum = enr.get("ohlcv_summary", {}) or {}

        rec_close = rec.get("close", 0)
        rec_vol_trend = str(ohlcv_sum.get("vol_trend", "")).lower()
        rec_squeeze = str(ohlcv_sum.get("squeeze", "")).lower()
        rec_avg_close = ohlcv_sum.get("avg_close", rec_close)

        if pick_base > 0:
            rec_range_pct = ohlcv_sum.get("price_range_pct", 0)
            if rec_range_pct > 0 and pick_base > 0:
                ratio = abs(rec_range_pct - pick_base) / max(pick_base, 1)
                if ratio < 0.3:
                    sim += 2
                elif ratio < 0.5:
                    sim += 1

        if pick_rs > 0:
            rec_rs = enr.get("rs_score")
            if rec_rs is not None and rec_rs > 0:
                diff = abs(rec_rs - pick_rs)
                if diff < 10:
                    sim += 2
                elif diff < 20:
                    sim += 1

        if "rising" in rec_vol_trend:
            sim += 1
        if pick_squeeze and pick_squeeze != "none" and "true" in rec_squeeze:
            sim += 1

        if pick_close > 0 and rec_avg_close > 0:
            price_ratio = abs(rec_avg_close - pick_close) / max(pick_close, 1)
            if price_ratio < 0.3:
                sim += 1

        autopsy = rec.get("autopsy", {})
        if autopsy.get("pre_signal_strength") == "STRONG":
            sim += 1

        if sim < 2:
            continue

        succeeded = rec.get("move_pct", 0) >= 3.0 and autopsy.get("predictable", False)
        scored.append({
            "symbol": rec.get("symbol", "?"),
            "move_date": rec.get("move_date", ""),
            "move_pct": round(rec.get("move_pct", 0), 1),
            "trigger_type": autopsy.get("trigger_type", ""),
            "confidence": autopsy.get("confidence", 0),
            "pre_signal_strength": autopsy.get("pre_signal_strength", ""),
            "predictable": autopsy.get("predictable", False),
            "sim_score": sim,
            "succeeded": succeeded,
        })

    scored.sort(key=lambda x: -x["sim_score"])
    top = scored[:10]

    return top


def llm_analogue_synthesis(pick: dict, analogues: List[dict]) -> Optional[dict]:
    """LLM evaluates historical analogues for a breakout candidate.

    Returns verdict on whether analogues are encouraging/concerning,
    key patterns, and red flags.
    """
    if not analogues or len(analogues) < 3:
        return None

    successes = sum(1 for a in analogues if a["succeeded"])
    historical_sr = round(successes / len(analogues) * 100, 1)

    analogue_text = []
    for a in analogues:
        analogue_text.append(
            f"{a['symbol']} ({a['move_date']}): move={a['move_pct']}%, "
            f"confidence={a['confidence']}, strength={a['pre_signal_strength']}, "
            f"predictable={'YES' if a['predictable'] else 'NO'}, "
            f"sim={a['sim_score']}")

    result = llm_json(
        "You are a quantitative analyst specializing in historical pattern matching "
        "for Indian stock breakouts. Respond with valid JSON only.",
        f"""Given a breakout candidate and its most similar historical setups from our
autopsy database, assess the historical evidence.

CANDIDATE: {pick['ticker']}
  Score={pick['score']:.0f}, close={pick['close']}, R={pick['resistance']},
  dist={pick['distance_pct']:.1f}%, base={pick['base_days']}d,
  squeeze={pick.get('squeeze_type', 'none')}

SIMILAR HISTORICAL BREAKOUTS ({len(analogues)} found, {successes} succeeded):
{chr(10).join(analogue_text)}

Historical success rate: {historical_sr}%

Assess:
1. Are the analogues encouraging or concerning for THIS candidate?
2. What pattern in successes vs failures is most relevant?
3. Any red flag from the data?

Return JSON:
{{"analogue_verdict": "encouraging" | "neutral" | "concerning",
  "historical_sr": {historical_sr},
  "key_pattern": "what the analogues suggest",
  "red_flag": null or "specific concern"}}""",
        max_tokens=800,
    )
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# LLM KILL SIGNAL CHECK — Dedicated adversarial pass
# ═══════════════════════════════════════════════════════════════════════════════

def llm_kill_signal_check(picks: List[dict], ctx: dict) -> List[dict]:
    """Dedicated adversarial pass: find reasons each pick will FAIL.

    Hard kills are removed. Soft warnings are attached as context for synthesis.
    Returns filtered list.
    """
    if not picks:
        return picks

    print(f"\n  LLM kill signal check on {len(picks)} picks...")

    pick_data = []
    for p in picks[:15]:
        signals = []
        if p.get("delivery_rising") is False:
            signals.append("delivery DECLINING")
        if p.get("fii_net_cr", 0) < -1000 and not p.get("institutional"):
            signals.append(f"FII net={p.get('fii_net_cr', 0):.0f}Cr, no inst signal")
        if p.get("distance_pct", 0) > 2.0:
            signals.append(f"already {p['distance_pct']:.1f}% ABOVE R")
        if not p.get("in_stage2"):
            signals.append("sector NOT in Stage 2")
        if p.get("announcements"):
            for ann in p["announcements"][:3]:
                subj = ann.get("subject", "").lower()
                if any(kw in subj for kw in ("result", "earning", "dividend",
                                              "board meeting", "agm")):
                    signals.append(f"upcoming event: {ann['subject'][:60]}")

        analogue_sr = p.get("analogue_success_rate")
        if analogue_sr is not None and analogue_sr < 50:
            signals.append(f"analogues only {analogue_sr:.0f}% historical SR")

        pick_data.append(
            f"{p['ticker']}: score={p['score']:.0f}, dist={p['distance_pct']:.1f}%, "
            f"base={p['base_days']}d, squeeze={p.get('squeeze_type', 'none')}, "
            f"rr={p.get('rr', 0)}, delivery_rising={p.get('delivery_rising')}"
            f"\n    RED FLAGS: {' | '.join(signals) if signals else 'none detected'}")

    result = llm_json(
        "You are an adversarial analyst. Your ONLY job is finding reasons "
        "breakout picks will FAIL. Be paranoid. Respond with valid JSON only.",
        f"""TARGET: >{TARGET_SUCCESS_RATE}% success rate. A false positive costs
more than a missed opportunity.

MARKET: FII net={ctx.get('fii_net', 0):.0f}Cr, "
Stage 2 sectors: {', '.join(ctx.get('stage2_sectors', [])[:5]) or 'few'}

PICKS WITH RED FLAGS:
{chr(10).join(pick_data)}

For EACH pick, find specific reasons it will FAIL:
- Declining delivery = sellers dominating
- Above R with no squeeze = already chasing
- No institutional signal + negative FII = no backing
- Short base + no squeeze = weak consolidation
- Upcoming earnings/event = binary risk
- Poor analogue history = pattern has failed before

Return JSON:
{{"kills": [{{
    "ticker": "X.NS",
    "kill": true/false,
    "severity": "hard_kill" | "soft_warning",
    "signals": ["reason1", "reason2"],
    "summary": "1 sentence"
}}]}}""",
        max_tokens=2000,
    )

    if not result or "kills" not in result:
        print("  Kill signal check unavailable, keeping all picks")
        for p in picks:
            p["kill_signals"] = []
            p["kill_severity"] = "none"
        return picks

    kill_map = {k["ticker"]: k for k in result["kills"]}
    kept = []
    hard_killed = 0
    soft_warned = 0

    for p in picks:
        k = kill_map.get(p["ticker"])
        if k and k.get("kill") and k.get("severity") == "hard_kill":
            hard_killed += 1
            p["kill_signals"] = k.get("signals", [])
            p["kill_severity"] = "hard_kill"
            continue
        elif k and k.get("severity") == "soft_warning":
            soft_warned += 1
            p["kill_signals"] = k.get("signals", [])
            p["kill_severity"] = "soft_warning"
        else:
            p["kill_signals"] = []
            p["kill_severity"] = "none"
        kept.append(p)

    print(f"  Kill signals: {hard_killed} hard kills, {soft_warned} warnings, "
          f"{len(kept)} survive")
    return kept


# ═══════════════════════════════════════════════════════════════════════════════
# LLM SYNTHESIZE — Master judgement call on enriched picks
# ═══════════════════════════════════════════════════════════════════════════════

def llm_synthesize(picks: List[dict], ctx: dict) -> List[dict]:
    """Send top 15 enriched picks to GPT-5.2 for synthesis. Picks beyond
    15 are kept as-is with llm_action='NOT_REVIEWED'. Returns the
    filtered list with LLM conviction, thesis, risk, and action attached.
    """
    if not picks:
        return picks

    reviewed = picks[:15]
    overflow = picks[15:]
    total_in = len(picks)
    print(f"\n  LLM synthesizing {len(reviewed)} of {total_in} picks "
          f"({len(overflow)} below cutoff, kept as-is)...")

    pick_summaries = []
    for p in reviewed:
        summary = (
            f"{p['ticker']}: score={p['score']:.0f}, "
            f"close={p['close']}, R={p['resistance']}, "
            f"dist={p['distance_pct']:.1f}%, "
            f"touches={p['touches']}, base={p['base_days']}d, "
            f"squeeze={p.get('squeeze_type', 'none')}, "
            f"inst={p.get('institutional', 'none')}, "
            f"rr={p.get('rr', 0)}"
        )
        # Enrichment signals
        enrichment = []
        if p.get("delivery_5d_avg") is not None:
            enrichment.append(
                f"delivery: 5d={p['delivery_5d_avg']}% "
                f"20d={p.get('delivery_20d_avg', '?')}% "
                f"rising={'YES' if p.get('delivery_rising') else 'NO'}")
        if p.get("recent_deals"):
            enrichment.append(
                f"deals: {len(p['recent_deals'])} recent")
        if p.get("superstar_buyers"):
            enrichment.append(
                f"superstars: {', '.join(p['superstar_buyers'][:3])}")
        if p.get("fno_confirms_R"):
            enrichment.append(
                f"F&O: call_OI={p.get('fno_call_oi_strike', '?')} "
                f"confirms R")
        if p.get("in_stage2"):
            enrichment.append("sector: Stage 2 uptrend")
        if p.get("earnings_growth") is not None:
            enrichment.append(
                f"earnings: {p['earnings_growth']:.0f}% QoQ")
        if p.get("revenue_growth") is not None:
            enrichment.append(
                f"revenue: {p['revenue_growth']:.0f}% QoQ")
        if p.get("announcements"):
            enrichment.append(
                f"announcements: {len(p['announcements'])} recent")

        # Autopsy analogue data
        if p.get("analogue_success_rate") is not None:
            enrichment.append(
                f"analogues: {p['analogue_success_rate']:.0f}% historical SR, "
                f"verdict={p.get('analogue_verdict', '?')}")
            if p.get("analogue_red_flag"):
                enrichment.append(f"analogue_red_flag: {p['analogue_red_flag']}")
            if p.get("analogue_key_pattern"):
                enrichment.append(
                    f"analogue_pattern: {p['analogue_key_pattern'][:80]}")

        # Kill signal warnings (soft warnings passed as context)
        if p.get("kill_signals"):
            enrichment.append(
                f"kill_warnings({p.get('kill_severity', '?')}): "
                f"{'; '.join(p['kill_signals'][:3])}")

        if enrichment:
            summary += "\n    SIGNALS: " + " | ".join(enrichment)
        pick_summaries.append(summary)

    # Market context
    fii_str = f"FII net: {ctx.get('fii_net', 0):.0f} Cr"
    stage2_str = ", ".join(ctx.get("stage2_sectors", [])[:10]) or "none"
    sector_rs_top = sorted(ctx.get("sector_rs", {}).items(),
                           key=lambda x: -x[1])[:5]
    sector_str = ", ".join(f"{k}({v:.0f})" for k, v in sector_rs_top) or "none"

    # Cross-scanner consensus: picks found by both scanners
    scanner_map: Dict[str, set] = {}
    for p in picks:
        sym = _extract_symbol(p["ticker"])
        scanner_map.setdefault(sym, set()).add(p.get("scanner", ""))
    consensus_tickers = [s for s, scanners in scanner_map.items()
                         if len(scanners) > 1]
    consensus_str = (", ".join(consensus_tickers[:10])
                     if consensus_tickers else "none")

    result = llm_json(
        "You are an expert breakout trading analyst for Indian stocks. "
        "You MUST respond with valid JSON only.",
        f"""MISSION: Synthesize all data and decide which stocks will break out
within 5-10 days. Target: >{TARGET_SUCCESS_RATE}% success rate.
A false positive costs MORE than a missed opportunity. Be paranoid.

MARKET CONTEXT:
- {fii_str}
- Top sectors by RS: {sector_str}
- Sectors in Stage 2 uptrend: {stage2_str}

CROSS-SCANNER CONSENSUS (found by BOTH V5.1 and V4.4 — higher confidence):
{consensus_str}

CANDIDATES WITH ALL ENRICHED DATA:
{chr(10).join(pick_summaries)}

SYNTHESIS RULES:
1. MULTI-SIGNAL CONFLUENCE required:
   - Rising delivery + near resistance + institutional = STRONG
   - Superstar buyer + Stage 2 sector + squeeze = VERY STRONG
   - Falling delivery + no OI + weak sector = WEAK → SKIP
2. KILL SIGNALS override everything — any hard kill = SKIP
3. HISTORICAL ANALOGUES: if analogue SR < 50%, SKIP unless other signals are
   overwhelming (4+ confirming signals)
4. CONVICTION FLOOR: anything below 60 conviction = SKIP. Do NOT recommend
   marginal setups.
5. Cross-scanner consensus picks get +10 conviction bonus

For each stock, explicitly check for REASONS TO REJECT before approving.

Return JSON:
{{"decisions": [{{
    "ticker": "X.NS",
    "action": "STRONG_BUY" | "BUY" | "SKIP",
    "conviction": 0-100,
    "thesis": "1-2 sentences: why this will break out",
    "risk": "what could prevent it",
    "signals_aligned": ["list of confirming signals"],
    "position_pct": 5-25
}}]}}""",
        max_tokens=3000,
    )

    if not result or "decisions" not in result:
        print("  LLM synthesis unavailable, keeping all picks")
        for p in picks:
            p["llm_action"] = "NO_LLM"
            p["llm_conviction"] = 0
            p["llm_thesis"] = ""
            p["llm_risk"] = ""
            p["llm_signals"] = []
            p["llm_position_pct"] = 10
        return picks

    decisions = {d["ticker"]: d for d in result["decisions"]}
    kept = []
    skipped = 0
    low_conviction = 0
    for p in reviewed:
        d = decisions.get(p["ticker"])
        if d:
            p["llm_action"] = d.get("action", "BUY")
            p["llm_conviction"] = d.get("conviction", 50)
            p["llm_thesis"] = d.get("thesis", "")
            p["llm_risk"] = d.get("risk", "")
            p["llm_signals"] = d.get("signals_aligned", [])
            p["llm_position_pct"] = d.get("position_pct", 10)
            if d.get("action") == "SKIP":
                skipped += 1
                continue
            if p["llm_conviction"] < 60:
                low_conviction += 1
                p["llm_action"] = "SKIP_LOW_CONVICTION"
                continue
        else:
            p["llm_action"] = "BUY"
            p["llm_conviction"] = 50
            p["llm_thesis"] = "LLM did not return verdict"
            p["llm_risk"] = ""
            p["llm_signals"] = []
            p["llm_position_pct"] = 10
        kept.append(p)

    # Overflow picks (beyond top 15) — tag and keep
    for p in overflow:
        p["llm_action"] = "NOT_REVIEWED"
        p["llm_conviction"] = 0
        p["llm_thesis"] = "Below top-15 cutoff, not sent to LLM"
        p["llm_risk"] = ""
        p["llm_signals"] = []
        p["llm_position_pct"] = 5

    all_out = kept + overflow
    print(f"  LLM synthesis: {len(kept)} kept, {skipped} skipped, "
          f"{low_conviction} below conviction floor "
          f"(of {len(reviewed)} reviewed), "
          f"{len(overflow)} not reviewed")
    for p in kept[:10]:
        print(f"    {p['ticker']}: {p['llm_action']} "
              f"(conviction={p['llm_conviction']})")

    return all_out


# ═══════════════════════════════════════════════════════════════════════════════
# RUN SCANNERS
# ═══════════════════════════════════════════════════════════════════════════════

def run_v5_scanner() -> List[dict]:
    """Run Breakout V5.1 with LLM-tuned parameters."""
    print("\n  Running Breakout V5.1...")
    try:
        import importlib
        import breakout_v5 as bv
        importlib.reload(bv)

        apply_llm_params_to_v5()

        ohlcv = bv.load_all_cached_ohlcv()
        bench_df = bv.find_bench_ticker(ohlcv)
        bench_close = (bench_df["Close"] if bench_df is not None
                       else pd.Series(dtype=float))
        ratings = bv.compute_rs_ratings(ohlcv, bench_close)
        picks, funnel = bv.run_scan(
            ohlcv,
            bench_df if bench_df is not None else pd.DataFrame(),
            ratings,
        )
        print(f"  V5.1: {len(picks)} picks "
              f"(from {funnel.get('total_universe', 0)} universe)")
        return [
            {
                "scanner": "V5.1", "scan_date": TODAY_STR,
                "ticker": p["ticker"], "score": p["score"],
                "close": p["close"], "resistance": p["resistance"],
                "distance_pct": p["distance_pct"],
                "rs_rating": p.get("rs_rating", 0),
                "touches": p.get("touches", 0),
                "base_days": p.get("base_days", 0),
                "squeeze_type": p.get("squeeze_type", ""),
                "institutional": p.get("institutional", ""),
                "weekly_rsi": p.get("weekly_rsi", 0),
                "stop": p.get("stop", 0),
                "target": p.get("target", 0),
                "rr": p.get("rr", 0),
                "outcome": "", "outcome_date": "",
                "max_high": None, "max_gain_pct": None,
                "max_drawdown_pct": None, "days_to_breakout": None,
                "llm_verdict": "",
            }
            for p in picks
        ]
    except Exception as e:
        print(f"  V5.1 ERROR: {e}")
        traceback.print_exc()
        return []


def run_v4_scanner() -> List[dict]:
    """Run Breakout Scanner Angel v4.4."""
    print("\n  Running Breakout Scanner Angel v4.4...")
    try:
        import importlib
        import breakout_scanner_angel as bsa
        importlib.reload(bsa)

        try:
            tickers = bsa.fetch_screener_universe(bsa.SCREENER_URL_DEFAULT)
        except Exception:
            tickers = []
        if not tickers:
            try:
                tickers, _ = bsa.fetch_angel_universe()
            except Exception:
                tickers = []

        if not tickers:
            print("  V4.4: No universe available")
            return []

        ohlcv = bsa.fetch_ohlcv(tickers[:500])
        bench = bsa.fetch_benchmark()
        rows, drops = bsa.scan(list(ohlcv.keys()), ohlcv, bench,
                               min_score=bsa.WATCHLIST_MIN_SCORE, strict=True)
        print(f"  V4.4: {len(rows)} picks (from {len(tickers)} universe)")
        return [
            {
                "scanner": "V4.4", "scan_date": TODAY_STR,
                "ticker": r["symbol"], "score": r.get("score", 0),
                "close": r.get("close", 0), "resistance": r.get("resistance", 0),
                "distance_pct": r.get("distance_pct", 0),
                "rs_rating": 0, "touches": r.get("touches", 0),
                "base_days": r.get("base_days", 0),
                "squeeze_type": "", "institutional": "",
                "weekly_rsi": 0,
                "stop": r.get("stop", 0), "target": r.get("target", 0),
                "rr": r.get("rr", 0),
                "outcome": "", "outcome_date": "",
                "max_high": None, "max_gain_pct": None,
                "max_drawdown_pct": None, "days_to_breakout": None,
                "llm_verdict": "",
            }
            for r in rows
        ]
    except Exception as e:
        print(f"  V4.4 ERROR: {e}")
        traceback.print_exc()
        return []


# ═══════════════════════════════════════════════════════════════════════════════
# LLM LIVE FILTER — Review and rank raw picks (pre-enrichment)
# ═══════════════════════════════════════════════════════════════════════════════

def llm_filter_picks(picks: List[dict]) -> List[dict]:
    """Ask LLM to review raw picks and flag low-conviction ones."""
    if not picks or len(picks) <= 3:
        return picks

    print(f"\n  LLM reviewing {len(picks)} raw picks...")

    pick_summaries = []
    for p in picks[:30]:
        pick_summaries.append(
            f"{p['ticker']}: score={p['score']:.0f}, close={p['close']}, "
            f"R={p['resistance']}, dist={p['distance_pct']:.1f}%, "
            f"touches={p['touches']}, base={p['base_days']}d, "
            f"squeeze={p.get('squeeze_type', 'none')}, "
            f"inst={p.get('institutional', 'none')}, rr={p.get('rr', 0)}")

    result = llm_json(
        "You are a quantitative breakout trading analyst for Indian stocks. "
        "You MUST respond with valid JSON only.",
        f"""Review these breakout scanner picks. Target: >{TARGET_SUCCESS_RATE}%
success rate (stock breaks above resistance within 5-10 trading days).

PICKS:
{chr(10).join(pick_summaries)}

For each ticker, decide: KEEP (high conviction) or DROP (likely to fail).

Consider:
- Stocks already above resistance (negative distance) may be chasing
- Very low R:R (<0.7) means poor risk-reward
- No squeeze + no institutional signal = weak setup
- Very short base (<15 days) may not be real consolidation

Return JSON: {{"decisions": [{{"ticker": "X.NS", "action": "KEEP" or "DROP", "reason": "brief why"}}]}}"""
    )

    if not result or "decisions" not in result:
        print("  LLM filter: no response, keeping all")
        return picks

    decisions = {d["ticker"]: d for d in result["decisions"]}
    kept = []
    dropped = 0
    for p in picks:
        d = decisions.get(p["ticker"])
        if d and d["action"] == "DROP":
            dropped += 1
            if len(picks) <= 5:
                kept.append(p)
        else:
            kept.append(p)

    print(f"  LLM filter: kept {len(kept)}, dropped {dropped}")
    return kept


# ═══════════════════════════════════════════════════════════════════════════════
# BACKTRACK with LLM JUDGEMENT (5-10 day flexible window)
# ═══════════════════════════════════════════════════════════════════════════════

def backtrack_with_llm(history_df: pd.DataFrame) -> pd.DataFrame:
    """Check outcomes with flexible 5-10 day window using LLM judgement."""
    print(f"\n  Backtracking (5-10 day flexible window)...")

    if history_df.empty:
        print("  No history to backtrack.")
        return history_df

    cutoff_5d = TODAY - datetime.timedelta(days=8)
    cutoff_10d = TODAY - datetime.timedelta(days=15)
    cutoff_str = cutoff_5d.strftime("%Y-%m-%d")

    needs_check = history_df[
        (history_df["outcome"].fillna("").astype(str) == "") &
        (history_df["scan_date"].astype(str) <= cutoff_str)
    ]

    if needs_check.empty:
        print("  No recommendations ready for backtracking.")
        return history_df

    print(f"  {len(needs_check)} recommendations to check...")

    tickers_needed = needs_check["ticker"].unique().tolist()
    try:
        from angel_client import angel_download_many
        start = cutoff_10d - datetime.timedelta(days=5)
        end = TODAY + datetime.timedelta(days=1)
        print(f"  Fetching prices for {len(tickers_needed)} tickers...")
        price_data = angel_download_many(tickers_needed, start, end)
    except Exception as e:
        print(f"  Price fetch error: {e}, using cache...")
        try:
            import breakout_v5 as bv
            price_data = bv.load_all_cached_ohlcv()
        except Exception:
            print("  Cannot load price data.")
            return history_df

    batch_for_llm = []

    for idx, row in needs_check.iterrows():
        ticker = row["ticker"]
        scan_date = str(row["scan_date"])
        R = float(row["resistance"])

        if ticker not in price_data:
            continue

        df = price_data[ticker]
        scan_ts = pd.Timestamp(scan_date)

        future = df[df.index > scan_ts].head(10)
        if future.empty or len(future) < 3:
            continue

        scan_close = float(row["close"])
        max_high = float(future["High"].max())
        min_low = float(future["Low"].min())
        max_gain = (max_high - scan_close) / scan_close * 100
        max_dd = (scan_close - min_low) / scan_close * 100

        days_to_bo = 0
        for i, (_, frow) in enumerate(future.iterrows(), 1):
            if float(frow["High"]) >= R * 1.005:
                days_to_bo = i
                break

        broke_out = max_high >= R * 1.005

        daily_prices = []
        for _, frow in future.iterrows():
            daily_prices.append(
                f"H={frow['High']:.1f} L={frow['Low']:.1f} C={frow['Close']:.1f}")

        batch_for_llm.append({
            "idx": idx, "ticker": ticker, "scan_date": scan_date,
            "scan_close": scan_close, "resistance": R,
            "max_high": max_high, "max_gain": max_gain,
            "max_dd": max_dd, "days_to_bo": days_to_bo,
            "broke_out_strict": broke_out,
            "daily": ", ".join(daily_prices[:10]),
            "n_days": len(future),
        })

        history_df.at[idx, "max_high"] = round(max_high, 2)
        history_df.at[idx, "max_gain_pct"] = round(max_gain, 2)
        history_df.at[idx, "max_drawdown_pct"] = round(max_dd, 2)
        history_df.at[idx, "days_to_breakout"] = days_to_bo
        history_df.at[idx, "outcome_date"] = TODAY_STR

    if batch_for_llm:
        summaries = []
        for b in batch_for_llm[:25]:
            summaries.append(
                f"{b['ticker']} (scanned {b['scan_date']}): R={b['resistance']:.1f}, "
                f"close={b['scan_close']:.1f}, max_high={b['max_high']:.1f}, "
                f"gain={b['max_gain']:.1f}%, dd={b['max_dd']:.1f}%, "
                f"broke_R+0.5%={'YES' if b['broke_out_strict'] else 'NO'}, "
                f"days_to_bo={b['days_to_bo']}, "
                f"prices: [{b['daily']}]")

        llm_result = llm_json(
            "You are a trading outcome judge. You MUST respond with valid JSON only.",
            f"""Judge these breakout recommendations. For each stock, decide SUCCESS or FAIL.

SUCCESS means: stock meaningfully broke above resistance (R) within 5-10 trading days.
Counts as success if:
- High exceeded R by at least 0.5%, OR
- Stock showed clear upward momentum toward R (gained 3%+ from scan close)

FAIL means: stock did NOT break resistance and/or fell significantly.

RECOMMENDATIONS:
{chr(10).join(summaries)}

Return JSON: {{"verdicts": [{{"ticker": "X.NS", "scan_date": "YYYY-MM-DD", "verdict": "SUCCESS" or "FAIL", "reason": "brief why"}}]}}"""
        )

        if llm_result and "verdicts" in llm_result:
            verdict_map = {(v["ticker"], v["scan_date"]): v
                           for v in llm_result["verdicts"]}
            for b in batch_for_llm:
                key = (b["ticker"], b["scan_date"])
                v = verdict_map.get(key)
                if v:
                    outcome = v["verdict"].upper()
                    if outcome not in ("SUCCESS", "FAIL"):
                        outcome = "SUCCESS" if b["broke_out_strict"] else "FAIL"
                    history_df.at[b["idx"], "outcome"] = outcome
                    history_df.at[b["idx"], "llm_verdict"] = v.get("reason", "")
                else:
                    history_df.at[b["idx"], "outcome"] = (
                        "SUCCESS" if b["broke_out_strict"] else "FAIL")
                    history_df.at[b["idx"], "llm_verdict"] = "rule-based (LLM missed)"
            print(f"  LLM judged {len(llm_result['verdicts'])} outcomes")
        else:
            for b in batch_for_llm:
                history_df.at[b["idx"], "outcome"] = (
                    "SUCCESS" if b["broke_out_strict"] else "FAIL")
                history_df.at[b["idx"], "llm_verdict"] = "rule-based (LLM unavailable)"
            print(f"  Used rule-based judgement (LLM unavailable)")

    resolved = history_df[
        history_df["outcome"].fillna("").astype(str).isin(["SUCCESS", "FAIL"])]
    if not resolved.empty:
        for scanner in resolved["scanner"].unique():
            s = resolved[resolved["scanner"] == scanner]
            n = len(s)
            w = (s["outcome"] == "SUCCESS").sum()
            print(f"  {scanner}: {w}/{n} = {w/n*100:.1f}% success")

    return history_df


# ═══════════════════════════════════════════════════════════════════════════════
# LLM PARAMETER TUNING — Analyze failures and auto-tune
# ═══════════════════════════════════════════════════════════════════════════════

def llm_tune_parameters(history_df: pd.DataFrame) -> Optional[dict]:
    """Analyze outcomes and generate parameter overrides for >90% accuracy."""
    resolved = history_df[
        history_df["outcome"].fillna("").astype(str).isin(["SUCCESS", "FAIL"])]
    if len(resolved) < 5:
        print("  Not enough data for LLM tuning (need 5+ resolved)")
        return None

    print(f"\n  LLM analyzing {len(resolved)} resolved picks for parameter tuning...")

    fails = resolved[resolved["outcome"] == "FAIL"]
    successes = resolved[resolved["outcome"] == "SUCCESS"]
    current_sr = len(successes) / len(resolved) * 100

    current_params = load_llm_params()
    if not current_params:
        try:
            import breakout_v5 as bv
            current_params = {k: getattr(bv.P, k) for k in dir(bv.P)
                              if k.isupper() and not k.startswith('_')}
        except Exception:
            current_params = {}

    fail_data = []
    for _, r in fails.iterrows():
        fail_data.append(
            f"{r['scan_date']}|{r['scanner']}|{r['ticker']}|"
            f"score={r['score']:.0f}|dist={r['distance_pct']:.1f}%|"
            f"touches={r['touches']}|base={r['base_days']}d|"
            f"squeeze={r.get('squeeze_type', '')}|"
            f"inst={r.get('institutional', '')}|"
            f"gain={r.get('max_gain_pct', 0):.1f}%|"
            f"dd={r.get('max_drawdown_pct', 0):.1f}%|"
            f"verdict={r.get('llm_verdict', '')}")

    success_data = []
    for _, r in successes.head(20).iterrows():
        success_data.append(
            f"{r['scan_date']}|{r['scanner']}|{r['ticker']}|"
            f"score={r['score']:.0f}|dist={r['distance_pct']:.1f}%|"
            f"touches={r['touches']}|gain={r.get('max_gain_pct', 0):.1f}%")

    result = llm_json(
        "You are a quantitative trading systems optimizer. "
        "Respond with valid JSON only.",
        f"""MISSION: Tune breakout scanner parameters to achieve >{TARGET_SUCCESS_RATE}% success rate.

CURRENT PERFORMANCE:
- Total resolved: {len(resolved)}
- Successes: {len(successes)} | Failures: {len(fails)}
- Current success rate: {current_sr:.1f}%
- TARGET: >{TARGET_SUCCESS_RATE}%
- Gap to close: {max(0, TARGET_SUCCESS_RATE - current_sr):.1f}%

CURRENT PARAMETERS:
{json.dumps({k: v for k, v in current_params.items()
             if not k.startswith('_') and k != 'last_updated'}, indent=2, default=str)}

FAILED PICKS:
{chr(10).join(fail_data[:30])}

SUCCESSFUL PICKS:
{chr(10).join(success_data[:15])}

Find SPECIFIC parameter changes that would have prevented failures while keeping
successes. Consider:
1. Failures at extreme distance_pct? -> Tighten proximity
2. Failures on low-touch resistance? -> Increase MIN_TOUCHES
3. Failures in short bases? -> Increase BASE_MIN_DAYS
4. Failures without squeeze? -> Make squeeze mandatory
5. Failures in weak RS stocks? -> Raise RS_MIN_PERCENTILE

Return JSON:
{{
  "analysis": "what you found",
  "current_sr": {current_sr:.1f},
  "projected_sr": estimated SR after changes,
  "param_changes": {{"PARAM_NAME": new_value}},
  "new_rules_suggested": ["rule 1", "rule 2"],
  "confidence": 0-100
}}"""
    )

    if not result:
        print("  LLM tuning failed")
        return None

    print(f"  LLM analysis: current={result.get('current_sr', '?')}% -> "
          f"projected={result.get('projected_sr', '?')}% "
          f"(confidence={result.get('confidence', '?')}%)")
    if result.get("analysis"):
        print(f"  Finding: {result['analysis'][:200]}")

    new_params = load_llm_params()
    changes = result.get("param_changes", {})
    if changes:
        for k, v in changes.items():
            if isinstance(v, (int, float, bool)):
                old = new_params.get(k, "?")
                new_params[k] = v
                print(f"    {k}: {old} -> {v}")
        save_llm_params(new_params)

    new_rules = result.get("new_rules_suggested", [])
    if new_rules:
        rules_path = os.path.join(OUTPUT_DIR, "breakout_llm_rules.json")
        try:
            existing_rules = []
            if os.path.exists(rules_path):
                with open(rules_path) as f:
                    existing_rules = json.load(f).get("rules", [])
            all_rules = existing_rules + [
                {"date": TODAY_STR, "rule": r} for r in new_rules]
            with open(rules_path, "w") as f:
                json.dump({"updated": TODAY_STR, "rules": all_rules[-50:]},
                          f, indent=2)
            print(f"  Saved {len(new_rules)} new rules → {rules_path}")
            for r in new_rules:
                print(f"    • {r}")
        except Exception as e:
            print(f"  Rule save error: {e}")

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# LLM CODE PATCH GENERATION — Self-improving code patches
# ═══════════════════════════════════════════════════════════════════════════════

def llm_generate_code_patch(history_df: pd.DataFrame,
                            mining_patterns: Optional[dict] = None) -> Optional[str]:
    """After failure analysis, LLM writes a Python code patch that adds a
    new filter or modifies scoring logic. Saved to Output/breakout_patches/
    and auto-imported by breakout_v5.py on next run.

    When mining_patterns is provided (from llm_failure_pattern_mining), the
    recommended_filter and pattern insights are fed to the patch generator
    for smarter, more targeted filters.
    """
    resolved = history_df[
        history_df["outcome"].fillna("").astype(str).isin(["SUCCESS", "FAIL"])]
    if len(resolved) < 10:
        return None

    fails = resolved[resolved["outcome"] == "FAIL"]
    if len(fails) < 3:
        return None

    current_sr = (resolved["outcome"] == "SUCCESS").sum() / len(resolved) * 100
    if current_sr >= TARGET_SUCCESS_RATE:
        print("  Already above target SR, no patch needed")
        return None

    print(f"\n  LLM generating code patch (SR={current_sr:.1f}%, "
          f"target={TARGET_SUCCESS_RATE}%)...")

    fail_patterns = []
    for _, r in fails.head(15).iterrows():
        fail_patterns.append(
            f"{r['ticker']}: score={r['score']:.0f}, "
            f"dist={r['distance_pct']:.1f}%, touches={r['touches']}, "
            f"base={r['base_days']}d, squeeze={r.get('squeeze_type', '')}, "
            f"gain={r.get('max_gain_pct', 0):.1f}%, "
            f"dd={r.get('max_drawdown_pct', 0):.1f}%")

    mining_context = ""
    if mining_patterns:
        rec_filter = mining_patterns.get("recommended_filter", "")
        insight = mining_patterns.get("overall_insight", "")
        patterns = mining_patterns.get("patterns", [])
        pattern_strs = []
        for mp in patterns[:5]:
            pattern_strs.append(
                f"  - {mp.get('name', '?')}: {' + '.join(mp.get('factors', []))}"
                f" → {mp.get('failure_rate_pct', '?')}% fail")
        mining_context = f"""
FAILURE PATTERN MINING RESULTS (use these to write smarter filters):
  Insight: {insight}
  Recommended filter: {rec_filter}
  Key patterns:
{chr(10).join(pattern_strs)}
"""

    result = llm_call(
        "You are a Python quant developer. Write a short, safe filter function.",
        f"""Write a Python function called `patch_filter(pick, df)` that can be used
as an ADDITIONAL filter in the breakout scanner. It receives:
- pick: dict with keys {{ticker, score, close, resistance, distance_pct,
  touches, base_days, squeeze_type, institutional, rr}}
- df: pandas DataFrame with OHLCV columns (Open, High, Low, Close, Volume),
  DatetimeIndex, representing the stock's price history

The function should return True to KEEP the pick, False to DROP it.

CONTEXT: Current success rate is {current_sr:.1f}%, target is {TARGET_SUCCESS_RATE}%.

FAILED PICKS (these should be filtered OUT by your function):
{chr(10).join(fail_patterns)}
{mining_context}
Write ONLY the function. No imports beyond numpy/pandas (already available as
np/pd). No side effects. Make it defensive (handle missing data gracefully).
Keep it under 30 lines.

Return just the Python code, no markdown, no explanation.""",
        max_tokens=1000,
    )

    if not result:
        print("  LLM patch generation failed")
        return None

    # Strip markdown code fences if present
    code = result.strip()
    code = re.sub(r'^```(?:python)?\s*', '', code)
    code = re.sub(r'\s*```$', '', code)
    code = code.strip()

    if "def patch_filter" not in code:
        print("  LLM returned invalid patch (no patch_filter function)")
        return None

    # Safety: reject dangerous patterns
    dangerous = ["import os", "import sys", "subprocess", "eval(", "exec(",
                 "open(", "__import__", "shutil", "rmtree"]
    for d in dangerous:
        if d in code:
            print(f"  LLM patch rejected: contains '{d}'")
            return None

    patch_name = f"patch_{TODAY_STR.replace('-', '')}.py"
    patch_path = os.path.join(PATCH_DIR, patch_name)
    with open(patch_path, "w") as f:
        f.write(f'"""Auto-generated patch by LLM on {TODAY_STR}.\n')
        f.write(f'Current SR: {current_sr:.1f}%, Target: {TARGET_SUCCESS_RATE}%.\n')
        f.write(f'"""\n\n')
        f.write("import numpy as np\nimport pandas as pd\n\n")
        f.write(code)
        f.write("\n")

    print(f"  Saved patch: {patch_path}")
    return patch_path


# ═══════════════════════════════════════════════════════════════════════════════
# LLM FAILURE PATTERN MINING — Multi-factor interaction analysis
# ═══════════════════════════════════════════════════════════════════════════════

def llm_failure_pattern_mining(history_df: pd.DataFrame) -> Optional[dict]:
    """Identify non-obvious multi-factor failure clusters across breakout picks.

    Goes deeper than single-variable tuning by looking for interaction effects:
    e.g. "squeeze + low institutional = 80% fail" or "extended base + high
    weekly RSI = false breakout".

    Saves results to Output/breakout_failure_patterns.json.
    Returns the patterns dict or None.
    """
    resolved = history_df[
        history_df["outcome"].fillna("").astype(str).isin(["SUCCESS", "FAIL"])]
    fails = resolved[resolved["outcome"] == "FAIL"]
    if len(fails) < 3:
        print("  Not enough failures for pattern mining (need 3+)")
        return None

    successes = resolved[resolved["outcome"] == "SUCCESS"]
    print(f"\n  LLM mining failure patterns across {len(fails)} failures "
          f"vs {len(successes)} successes...")

    def _profile(r):
        return {
            "ticker": r.get("ticker", ""),
            "scan_date": str(r.get("scan_date", "")),
            "score": round(r.get("score", 0), 1),
            "distance_pct": round(r.get("distance_pct", 0), 2),
            "touches": int(r.get("touches", 0)),
            "base_days": int(r.get("base_days", 0)),
            "base_range": round(r.get("base_range", 0), 2) if pd.notna(r.get("base_range")) else None,
            "squeeze_type": str(r.get("squeeze_type", "")),
            "institutional": str(r.get("institutional", "")),
            "weekly_rsi": round(r.get("weekly_rsi", 0), 1) if pd.notna(r.get("weekly_rsi")) else None,
            "rs_rating": round(r.get("rs_rating", 0), 1) if pd.notna(r.get("rs_rating")) else None,
            "max_gain_pct": round(r.get("max_gain_pct", 0), 1),
            "max_drawdown_pct": round(r.get("max_drawdown_pct", 0), 1),
            "outcome": r.get("outcome", ""),
        }

    fail_profiles = [_profile(r) for _, r in fails.iterrows()]
    success_profiles = [_profile(r) for _, r in successes.head(30).iterrows()]

    result = llm_json(
        "You are a quantitative research analyst specializing in breakout "
        "pattern failure analysis for Indian equities. "
        "Respond with valid JSON only.",
        f"""MISSION: Identify non-obvious MULTI-FACTOR interaction patterns in
breakout failures. Single-variable analysis is already done — focus on
factor COMBINATIONS that predict failure.

FAILURES ({len(fail_profiles)}):
{json.dumps(fail_profiles[:40], default=str)}

SUCCESSES ({len(success_profiles)}) for comparison:
{json.dumps(success_profiles[:20], default=str)}

Look for patterns like:
- "base_range > 25% AND weekly_rsi > 68" → most fail (extended, overbought)
- "squeeze_type empty AND touches == 2" → weak setup
- "distance_pct > 2% AND institutional weak" → chasing without backing
- "high score but specific factor combination" → score masks weakness

Return JSON:
{{
  "patterns": [
    {{
      "name": "descriptive pattern name",
      "factors": ["factor1 condition", "factor2 condition"],
      "failure_rate_pct": estimated failure rate for this combo,
      "occurrences": count in the failure set,
      "suggestion": "concrete parameter or filter change",
      "sample_tickers": ["TICK1", "TICK2"]
    }}
  ],
  "overall_insight": "1-2 sentence summary of the most important finding",
  "recommended_filter": "Python-like pseudo-code for the most impactful new filter"
}}""",
        max_tokens=2500
    )

    if not result:
        print("  LLM failure pattern mining returned nothing")
        return None

    patterns = result.get("patterns", [])
    print(f"  Found {len(patterns)} multi-factor failure patterns:")
    for p in patterns[:5]:
        print(f"    • {p.get('name', '?')}: {' + '.join(p.get('factors', []))}"
              f" → {p.get('failure_rate_pct', '?')}% fail rate")

    insight = result.get("overall_insight", "")
    if insight:
        print(f"  Key insight: {insight[:200]}")

    out_path = os.path.join(SCRIPT_DIR, "Output", "breakout_failure_patterns.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    try:
        with open(out_path, "w") as f:
            json.dump({
                "generated": TODAY_STR,
                "failures_analyzed": len(fails),
                "successes_compared": len(success_profiles),
                **result,
            }, f, indent=2, default=str)
        print(f"  Saved: {out_path}")
    except Exception as e:
        print(f"  Save failed: {e}")

    return result


# ═══════════════════════════════════════════════════════════════════════════════
# COMPARE & REPORT
# ═══════════════════════════════════════════════════════════════════════════════

def compare_scanners(history_df: pd.DataFrame) -> pd.DataFrame:
    """Build comparison table."""
    resolved = history_df[
        history_df["outcome"].fillna("").astype(str).isin(["SUCCESS", "FAIL"])]
    if resolved.empty:
        return pd.DataFrame()

    rows = []
    for scanner in ["V5.1", "V4.4"]:
        s = resolved[resolved["scanner"] == scanner]
        if s.empty:
            rows.append({"Scanner": scanner, "Picks": 0, "SR": "N/A"})
            continue
        n = len(s)
        w = (s["outcome"] == "SUCCESS").sum()
        wins = s[s["outcome"] == "SUCCESS"]
        fail_df = s[s["outcome"] == "FAIL"]
        rows.append({
            "Scanner": scanner, "Picks": n,
            "Success": w, "Fail": len(fail_df),
            "SR": f"{w/n*100:.1f}%",
            "Avg Gain (W)": (f"{wins['max_gain_pct'].mean():.1f}%"
                             if not wins.empty else "-"),
            "Avg DD (L)": (f"{fail_df['max_drawdown_pct'].mean():.1f}%"
                           if not fail_df.empty else "-"),
            "Avg Score": f"{s['score'].mean():.1f}",
        })
    return pd.DataFrame(rows)


def load_history() -> pd.DataFrame:
    """Load persistent history from Excel."""
    if os.path.exists(HISTORY_FILE):
        try:
            df = pd.read_excel(HISTORY_FILE, sheet_name="Daily Log")
            return df
        except Exception:
            pass
    return pd.DataFrame()


def save_all(history_df, comparison, llm_analysis, summary):
    """Save all sheets to Excel."""
    with pd.ExcelWriter(HISTORY_FILE, engine="openpyxl") as w:
        history_df.to_excel(w, sheet_name="Daily Log", index=False)

        resolved = history_df[
            history_df["outcome"].fillna("").astype(str).isin(["SUCCESS", "FAIL"])]
        if not resolved.empty:
            resolved.to_excel(w, sheet_name="Backtrack Results", index=False)
        else:
            pd.DataFrame({"Note": ["No results yet"]}).to_excel(
                w, sheet_name="Backtrack Results", index=False)

        if comparison is not None and not comparison.empty:
            comparison.to_excel(w, sheet_name="Scanner Comparison", index=False)

        analysis_text = ""
        if llm_analysis:
            analysis_text = json.dumps(llm_analysis, indent=2, default=str)
        pd.DataFrame({"Analysis": [analysis_text or "Pending"]}).to_excel(
            w, sheet_name="Improvement Notes", index=False)

        if summary:
            pd.DataFrame([summary]).to_excel(w, sheet_name="Summary", index=False)

    print(f"\n  History saved: {HISTORY_FILE}")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN — Full orchestration pipeline
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    """Orchestrate 8 steps: context -> scan -> enrich -> synthesize ->
    log -> backtrack -> tune -> patch. Each step prints a numbered header.
    """
    parser = argparse.ArgumentParser(
        description="Breakout Daily Tracker v3 — Full LLM Integration")
    parser.add_argument("--backtrack-only", action="store_true")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--no-v4", action="store_true")
    parser.add_argument("--no-filter", action="store_true",
                        help="Skip LLM pick filtering")
    parser.add_argument("--no-enrich", action="store_true",
                        help="Skip enrichment (fast scan only)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    t0 = time.time()

    print(f"\n{'='*65}")
    print(f"  BREAKOUT DAILY TRACKER v3 — {TODAY_STR}")
    print(f"  Target: >{TARGET_SUCCESS_RATE}% success rate")
    print(f"  Mode: {'backtrack-only' if args.backtrack_only else 'full run'}")
    print(f"{'='*65}")

    history_df = load_history()
    if not history_df.empty:
        print(f"  History loaded: {len(history_df)} past recommendations")
        if "outcome" not in history_df.columns:
            history_df["outcome"] = ""
        if "llm_verdict" not in history_df.columns:
            history_df["llm_verdict"] = ""
        history_df["outcome"] = history_df["outcome"].fillna("").astype(str)
    else:
        print("  Starting fresh.")

    # ── Step 1: Gather market context ──
    ctx = {}
    if not args.backtrack_only and not args.no_enrich:
        print(f"\n{'─'*65}")
        print("  STEP 1: Gathering market context...")
        print(f"{'─'*65}")
        ctx = gather_context(skip_expensive=args.no_enrich)

    # ── Step 2: Run scanners ──
    new_picks = []
    if not args.backtrack_only:
        print(f"\n{'─'*65}")
        print("  STEP 2: Running scanners...")
        print(f"{'─'*65}")

        v5_picks = run_v5_scanner()

        # LLM pre-filter (before enrichment — fast, cheap)
        if not args.no_llm and not args.no_filter and v5_picks:
            v5_picks = llm_filter_picks(v5_picks)

        new_picks.extend(v5_picks)
        print(f"  V5.1 final: {len(v5_picks)} recommendations")

        if not args.no_v4:
            v4_picks = run_v4_scanner()
            if not args.no_llm and not args.no_filter and v4_picks:
                v4_picks = llm_filter_picks(v4_picks)
            new_picks.extend(v4_picks)
            print(f"  V4.4 final: {len(v4_picks)} recommendations")

    # ── Step 3: Enrich picks ──
    if new_picks and not args.no_enrich and not args.backtrack_only:
        print(f"\n{'─'*65}")
        print("  STEP 3: Enriching picks with multi-source data...")
        print(f"{'─'*65}")
        new_picks = enrich_picks(new_picks, ctx)

    # ── Step 3b: LLM kill signal check ──
    if new_picks and not args.no_llm and not args.no_enrich and not args.backtrack_only:
        print(f"\n{'─'*65}")
        print("  STEP 3b: LLM kill signal check...")
        print(f"{'─'*65}")
        new_picks = llm_kill_signal_check(new_picks, ctx)

    # ── Step 4: LLM synthesis ──
    if new_picks and not args.no_llm and not args.no_enrich and not args.backtrack_only:
        print(f"\n{'─'*65}")
        print("  STEP 4: LLM master synthesis...")
        print(f"{'─'*65}")
        new_picks = llm_synthesize(new_picks, ctx)

    # ── Step 5: Append to history ──
    print(f"\n{'─'*65}")
    print("  STEP 5: Logging picks to history...")
    print(f"{'─'*65}")
    if new_picks:
        existing_keys = set()
        if not history_df.empty:
            existing_keys = set(zip(
                history_df["scanner"], history_df["ticker"],
                history_df["scan_date"].astype(str)))
        deduped = [p for p in new_picks
                   if (p["scanner"], p["ticker"], p["scan_date"])
                   not in existing_keys]

        if deduped:
            new_df = pd.DataFrame(deduped)
            history_df = (pd.concat([history_df, new_df], ignore_index=True)
                          if not history_df.empty else new_df)
            print(f"  Added {len(deduped)} new recommendations "
                  f"(total: {len(history_df)})")
        else:
            print("  All picks already in history (duplicates skipped)")
    else:
        print("  No new picks to log")

    # ── Step 6: Backtrack ──
    print(f"\n{'─'*65}")
    print("  STEP 6: Backtracking past picks...")
    print(f"{'─'*65}")
    if not args.no_llm:
        history_df = backtrack_with_llm(history_df)
    else:
        print("  Backtracking (rule-based, --no-llm)...")
        cutoff = (TODAY - datetime.timedelta(days=8)).strftime("%Y-%m-%d")
        needs = history_df[
            (history_df["outcome"].fillna("").astype(str) == "") &
            (history_df["scan_date"].astype(str) <= cutoff)
        ]
        if not needs.empty:
            try:
                import breakout_v5 as bv
                ohlcv = bv.load_all_cached_ohlcv()
                for idx, row in needs.iterrows():
                    tk = row["ticker"]
                    if tk not in ohlcv:
                        continue
                    df = ohlcv[tk]
                    ts = pd.Timestamp(row["scan_date"])
                    future = df[df.index > ts].head(10)
                    if len(future) < 3:
                        continue
                    mh = float(future["High"].max())
                    sc = float(row["close"])
                    R = float(row["resistance"])
                    history_df.at[idx, "outcome"] = (
                        "SUCCESS" if mh >= R * 1.005 else "FAIL")
                    history_df.at[idx, "outcome_date"] = TODAY_STR
                    history_df.at[idx, "max_high"] = round(mh, 2)
                    history_df.at[idx, "max_gain_pct"] = round(
                        (mh - sc) / sc * 100, 2)
                    history_df.at[idx, "max_drawdown_pct"] = round(
                        (sc - float(future["Low"].min())) / sc * 100, 2)
            except Exception as e:
                print(f"  Backtrack error: {e}")

    # ── Step 7: LLM parameter tuning ──
    llm_analysis = None
    if not args.no_llm:
        print(f"\n{'─'*65}")
        print("  STEP 7: LLM parameter tuning...")
        print(f"{'─'*65}")
        llm_analysis = llm_tune_parameters(history_df)

    # ── Step 8: LLM failure pattern mining (runs BEFORE patching) ──
    mining_result = None
    if not args.no_llm:
        print(f"\n{'─'*65}")
        print("  STEP 8: LLM failure pattern mining...")
        print(f"{'─'*65}")
        mining_result = llm_failure_pattern_mining(history_df)

    # ── Step 8b: LLM code patch (fed by mining patterns) ──
    if not args.no_llm:
        print(f"\n{'─'*65}")
        print("  STEP 8b: LLM code patch generation...")
        print(f"{'─'*65}")
        llm_generate_code_patch(history_df, mining_patterns=mining_result)

    # ── Step 9: Compare & save ──
    comparison = compare_scanners(history_df)

    resolved = history_df[
        history_df["outcome"].fillna("").astype(str).isin(["SUCCESS", "FAIL"])]
    summary = {
        "Run Date": TODAY_STR,
        "Target SR": f">{TARGET_SUCCESS_RATE}%",
        "New Picks Today": len(new_picks) if not args.backtrack_only else 0,
        "Total History": len(history_df),
        "Resolved": len(resolved),
    }
    for scanner in ["V5.1", "V4.4"]:
        s = resolved[resolved["scanner"] == scanner]
        if not s.empty:
            w = (s["outcome"] == "SUCCESS").sum()
            summary[f"{scanner} SR"] = f"{w}/{len(s)} = {w/len(s)*100:.1f}%"
        else:
            summary[f"{scanner} SR"] = "N/A"

    if llm_analysis and llm_analysis.get("projected_sr"):
        summary["LLM Projected SR"] = f"{llm_analysis['projected_sr']}%"

    save_all(history_df, comparison, llm_analysis, summary)

    elapsed = time.time() - t0

    # ── Final console output ──
    print(f"\n{'='*65}")
    print(f"  TRACKER COMPLETE — {TODAY_STR}")
    print(f"{'='*65}")
    print(f"  New picks:       {len(new_picks) if not args.backtrack_only else 0}")
    print(f"  Total history:   {len(history_df)}")
    print(f"  Resolved:        {len(resolved)}")
    for scanner in ["V5.1", "V4.4"]:
        s = resolved[resolved["scanner"] == scanner]
        if not s.empty:
            w = (s["outcome"] == "SUCCESS").sum()
            sr = w / len(s) * 100
            flag = "PASS" if sr >= TARGET_SUCCESS_RATE else "BELOW TARGET"
            print(f"  {scanner}: {w}/{len(s)} = {sr:.1f}% [{flag}]")
    if os.path.exists(LLM_PARAMS_FILE):
        print(f"  LLM params:      {LLM_PARAMS_FILE}")
    else:
        print(f"  LLM params:      (none yet — need 5+ resolved picks)")
    patch_files = [f for f in os.listdir(PATCH_DIR) if f.endswith(".py")] if os.path.isdir(PATCH_DIR) else []
    if patch_files:
        print(f"  Patches:         {len(patch_files)} in {PATCH_DIR}")
    else:
        print(f"  Patches:         (none yet — need 10+ resolved picks)")
    print(f"  History:         {HISTORY_FILE}")
    print(f"  Elapsed:         {elapsed:.1f}s")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
