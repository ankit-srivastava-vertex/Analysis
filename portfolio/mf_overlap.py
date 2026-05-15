"""
mf_overlap.py — mutual-fund overlap with directly-held stocks
==============================================================

SUMMARY
-------
Crowding detector: for every directly-held stock, auto-discover EVERY
mutual-fund scheme holding it (including sub-1% positions) and quantify
crowding risk. No user-maintained MF list required.

DATA SOURCE
-----------
ETMoney public stock pages embed the full `mfSchemeList` (every scheme
holding the stock + holding %, parent fund, AMC, category) inside the
shareholding sub-page HTML. We:
  1. Pull the ETMoney shareholding sitemap once (~2.4k stocks) -> map
     of slug + numeric ETMoney stock-id, cached 7 days.
  2. Resolve our NSE Symbol -> ETMoney URL via fuzzy slug match against
     Company name; verified by parsing `nseCode` from the stock page
     itself. Per-symbol mapping cached permanently in etm_nse_map.json.
  3. Fetch /stocks/<slug>/shareholding/<id> for each owned stock,
     extract the embedded `mfSchemeList` JSON array, cache 30 days.
  4. Aggregate -> two crowding views.

OUTPUT SHEETS
-------------
- MF Holders Per Stock : one row per (Stock, Fund) combo with weight%
                         and parent-fund grouping.
- MF Crowding Summary  : per stock: FundCount, AvgWeight%, MaxWeight%,
                         TopFund, CrowdingScore = FundCount * Avg.
- MF Overlap Notes     : metadata + cache info.
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

import pandas as pd
import requests

PORTFOLIO_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PORTFOLIO_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from portfolio.holdings_loader import load_holdings

CACHE_DIR = PORTFOLIO_DIR / ".cache"
CACHE_DIR.mkdir(exist_ok=True)
ETM_CACHE_DIR = CACHE_DIR / "etm_holdings"
ETM_CACHE_DIR.mkdir(exist_ok=True)
ETM_MAP_FILE = CACHE_DIR / "etm_nse_map.json"
ETM_SLUGS_FILE = CACHE_DIR / "etm_stock_slugs.json"
ETM_HISTORY_FILE = CACHE_DIR / "etm_fundcount_history.csv"

ETM_BASE = "https://www.etmoney.com"
SITEMAP_URL = f"{ETM_BASE}/sitemap-stocks-shareholding.xml"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SLUGS_TTL = 7 * 86400        # sitemap refresh weekly
HOLDINGS_TTL = 30 * 86400    # MF disclosures monthly
REQUEST_DELAY = 0.4          # be polite

_SESSION: Optional[requests.Session] = None
_SYM_TO_NAME: Optional[dict] = None


def _symbol_to_name() -> dict:
    """Build NSE Symbol -> Company Name from cached EQUITY_L.csv +
    SME_EQUITY_L.csv (already maintained by holdings_loader)."""
    global _SYM_TO_NAME
    if _SYM_TO_NAME is not None:
        return _SYM_TO_NAME
    out: dict = {}
    for fname in ("EQUITY_L.csv", "SME_EQUITY_L.csv"):
        # holdings_loader caches NSE masters here:
        for p in (PROJECT_ROOT / ".cache" / "portfolio" / fname,
                  CACHE_DIR / fname):
            if p.exists():
                break
        else:
            continue
        try:
            df = pd.read_csv(p)
            df.columns = [c.strip().upper() for c in df.columns]
            sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
            name_col = next((c for c in df.columns if "NAME OF COMPANY" in c
                             or c == "NAME"), None)
            if not (sym_col and name_col):
                continue
            for _, row in df.iterrows():
                sym = str(row[sym_col]).strip().upper()
                name = str(row[name_col]).strip()
                if sym and name and sym not in out:
                    out[sym] = name
        except Exception:
            continue
    _SYM_TO_NAME = out
    return out


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        _SESSION = s
    return _SESSION


# ─── Slug map (sitemap-driven) ────────────────────────────────

def _load_slug_map() -> dict:
    """Return dict slug -> {'id': '2046', 'url': '/stocks/.../shareholding/2046'}.

    Cached at .cache/etm_stock_slugs.json (7 day TTL).
    """
    if ETM_SLUGS_FILE.exists():
        age = time.time() - ETM_SLUGS_FILE.stat().st_mtime
        if age < SLUGS_TTL:
            try:
                return json.loads(ETM_SLUGS_FILE.read_text())
            except Exception:
                pass
    try:
        r = _session().get(SITEMAP_URL, timeout=30)
        r.raise_for_status()
    except Exception as e:
        print(f"  [mf] sitemap fetch failed: {e}")
        if ETM_SLUGS_FILE.exists():
            return json.loads(ETM_SLUGS_FILE.read_text())
        return {}
    out: dict = {}
    pat = re.compile(r"/stocks/([a-z0-9-]+)/shareholding/(\d+)")
    for slug, sid in pat.findall(r.text):
        out[slug] = {"id": sid,
                     "url": f"{ETM_BASE}/stocks/{slug}/shareholding/{sid}"}
    ETM_SLUGS_FILE.write_text(json.dumps(out))
    return out


# ─── NSE symbol -> ETMoney URL resolution ─────────────────────

def _company_to_slug_candidates(company: str) -> list:
    """Generate likely slug forms from a Company string."""
    if not company:
        return []
    s = re.sub(r"[^a-z0-9 ]", " ", company.lower())
    s = re.sub(r"\s+", " ", s).strip()
    base = s.replace(" ", "-")
    cands = {base, base + "-ltd", base + "-limited"}
    # also try without trailing 'ltd'
    no_ltd = re.sub(r"-(ltd|limited)$", "", base)
    if no_ltd:
        cands.add(no_ltd)
        cands.add(no_ltd + "-ltd")
    return list(cands)


def _verify_nse(slug: str, sid: str, expected_symbol: str) -> bool:
    """Fetch shareholding page once, confirm nseCode matches."""
    url = f"{ETM_BASE}/stocks/{slug}/shareholding/{sid}"
    try:
        time.sleep(REQUEST_DELAY)
        r = _session().get(url, timeout=20)
        if r.status_code != 200:
            return False
        m = re.search(r'nseCode\\":\\"([A-Z0-9&-]+)\\"', r.text)
        return bool(m and m.group(1).upper() == expected_symbol.upper())
    except Exception:
        return False


def _load_nse_map() -> dict:
    if ETM_MAP_FILE.exists():
        try:
            return json.loads(ETM_MAP_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_nse_map(m: dict) -> None:
    ETM_MAP_FILE.write_text(json.dumps(m, indent=2, sort_keys=True))


def _resolve_symbol(symbol: str, company: str, slugs: dict,
                    nse_map: dict, verbose: bool = True) -> Optional[dict]:
    """Return {'slug','id','url'} or None. Caches results in nse_map."""
    key = symbol.upper()
    if key in nse_map:
        v = nse_map[key]
        if v is None or v == {}:
            return None
        return v

    # Try direct slug candidates
    candidates = _company_to_slug_candidates(company)
    direct = [c for c in candidates if c in slugs]
    # Fuzzy match against all slugs as fallback
    if not direct and candidates:
        # use longest base candidate (most specific) for fuzzy
        base = max(candidates, key=len)
        direct = difflib.get_close_matches(base, slugs.keys(), n=5, cutoff=0.7)

    for slug in direct:
        sid = slugs[slug]["id"]
        if _verify_nse(slug, sid, symbol):
            entry = {"slug": slug, "id": sid, "url": slugs[slug]["url"]}
            nse_map[key] = entry
            _save_nse_map(nse_map)
            if verbose:
                print(f"      ✓ mapped {symbol} -> {slug}")
            return entry

    if verbose:
        print(f"      ✗ no ETMoney match for {symbol} ({company})")
    nse_map[key] = None  # negative cache
    _save_nse_map(nse_map)
    return None


def _extract_balanced_array(html: str, marker: str) -> Optional[str]:
    """Find `marker` (e.g. 'mfSchemeList\\":[' or just 'schemeData\\":['), return
    the balanced array text starting at the '[' or None."""
    p = html.find(marker)
    if p < 0:
        return None
    start = p + len(marker) - 1  # land on '['
    depth = 0
    j = start
    in_str = False
    esc = False
    while j < len(html):
        c = html[j]
        if esc:
            esc = False
        elif c == "\\":
            esc = True
        elif c == '"' and not esc:
            in_str = not in_str
        elif not in_str:
            if c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    return html[start:j + 1]
        j += 1
    return None


def _parse_escaped_json_array(raw: str) -> list:
    """Parse a JSON array embedded inside a JS string (escaped \\" etc)."""
    if not raw:
        return []
    try:
        return json.loads(bytes(raw, "utf-8").decode("unicode_escape"))
    except Exception:
        try:
            return json.loads(raw.replace('\\"', '"').replace("\\\\", "\\"))
        except Exception:
            return []


def _extract_movements(html: str) -> dict:
    """Extract the 'New Entries' (variant=1) and 'Full Exits' (variant=2)
    sections — these list schemes that newly entered / fully exited the
    stock between the last two AMFI disclosures (≈ MoM)."""
    movements = {"NewEntries": 0, "FullExits": 0,
                 "NewAsOf": None, "ExitAsOf": None}
    # Iterate every (sectionTitle, variant, schemeData) tuple
    for m in re.finditer(
        r'sectionTitle\\":\\"([^"\\]+)\\"[^{]*?variant\\":(\d+),\\"schemeData\\":',
        html,
    ):
        variant = m.group(2)
        title = m.group(1).lower()
        # extract balanced array starting at m.end()
        j = m.end()
        if j >= len(html) or html[j] != "[":
            continue
        depth = 0
        in_str = False
        esc = False
        k = j
        while k < len(html):
            c = html[k]
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"' and not esc:
                in_str = not in_str
            elif not in_str:
                if c == "[":
                    depth += 1
                elif c == "]":
                    depth -= 1
                    if depth == 0:
                        arr_raw = html[j:k + 1]
                        break
            k += 1
        else:
            continue
        items = _parse_escaped_json_array(arr_raw)
        # one date per entry; pick first non-empty
        asof = None
        for it in items:
            dwh = it.get("datewiseHoldingPercentage") or {}
            if dwh:
                asof = sorted(dwh.keys())[-1]
                break
        if variant == "1" or "new entries" in title:
            movements["NewEntries"] = len(items)
            movements["NewAsOf"] = asof
        elif variant == "2" or "full exits" in title:
            movements["FullExits"] = len(items)
            movements["ExitAsOf"] = asof
    return movements


# ─── Per-stock holdings extractor ─────────────────────────────

def _extract_mf_scheme_list(html: str) -> list:
    """Pull the `mfSchemeList` array (escaped JSON inside Next.js stream)
    and parse it into a list of dicts."""
    raw = _extract_balanced_array(html, 'mfSchemeList\\":[')
    return _parse_escaped_json_array(raw) if raw else []


def _fetch_holdings_for_stock(symbol: str, entry: dict,
                              verbose: bool = True) -> dict:
    """Return {'rows': [...], 'movements': {NewEntries, FullExits, ...}}."""
    cache_file = ETM_CACHE_DIR / f"{symbol.upper()}.json"
    if cache_file.exists():
        age = time.time() - cache_file.stat().st_mtime
        if age < HOLDINGS_TTL:
            try:
                cached = json.loads(cache_file.read_text())
                if isinstance(cached, dict) and "rows" in cached:
                    return cached
                # legacy cache (list only) — wrap and continue
                if isinstance(cached, list):
                    return {"rows": cached, "movements": {
                        "NewEntries": None, "FullExits": None,
                        "NewAsOf": None, "ExitAsOf": None,
                    }}
            except Exception:
                pass
    try:
        time.sleep(REQUEST_DELAY)
        r = _session().get(entry["url"], timeout=25)
        r.raise_for_status()
    except Exception as e:
        if verbose:
            print(f"      ✗ fetch fail {symbol}: {e}")
        return {"rows": [], "movements": {"NewEntries": None, "FullExits": None,
                                          "NewAsOf": None, "ExitAsOf": None}}
    raw = _extract_mf_scheme_list(r.text)
    movements = _extract_movements(r.text)
    rows = []
    for it in raw:
        dwh = it.get("datewiseHoldingPercentage") or {}
        if dwh:
            asof = sorted(dwh.keys())[-1]
            wt = dwh[asof]
        else:
            asof, wt = None, None
        rows.append({
            "schemeId":          it.get("schemeId"),
            "schemeName":        it.get("schemeName"),
            "parentSchemeName":  it.get("parentSchemeName"),
            "primaryCategory":   it.get("primaryCategory"),
            "secondaryCategory": it.get("secondaryCategory"),
            "Weight%":           wt,
            "AsOf":              asof,
            "isDirect":          it.get("isDirect"),
        })
    payload = {"rows": rows, "movements": movements}
    cache_file.write_text(json.dumps(payload))
    return payload


# ─── FundCount snapshot history (for true QoQ over time) ─────

def _load_history() -> pd.DataFrame:
    cols = ["Symbol", "AsOf", "FundCount", "NewEntriesMoM", "FullExitsMoM"]
    if ETM_HISTORY_FILE.exists():
        try:
            df = pd.read_csv(ETM_HISTORY_FILE)
            for c in cols:
                if c not in df.columns:
                    df[c] = None
            return df[cols]
        except Exception:
            return pd.DataFrame(columns=cols)
    return pd.DataFrame(columns=cols)


def _save_history(df: pd.DataFrame) -> None:
    df.to_csv(ETM_HISTORY_FILE, index=False)


def _qoq_change(history: pd.DataFrame, symbol: str, current_asof: str,
                current_value, column: str = "FundCount") -> Optional[int]:
    """Return delta of `column` vs ~3 months prior (closest snapshot 60-120
    days before current_asof). None if no eligible historical snapshot."""
    if not current_asof or current_value is None or column not in history.columns:
        return None
    sub = history[history["Symbol"] == symbol].copy()
    if sub.empty:
        return None
    sub["AsOf"] = pd.to_datetime(sub["AsOf"], errors="coerce")
    cur_dt = pd.to_datetime(current_asof, errors="coerce")
    if pd.isna(cur_dt):
        return None
    sub["DaysBack"] = (cur_dt - sub["AsOf"]).dt.days
    elig = sub[(sub["DaysBack"] >= 60) & (sub["DaysBack"] <= 120)].copy()
    elig = elig.dropna(subset=[column])
    if elig.empty:
        return None
    # nearest to ~90 days
    elig = elig.assign(diff=(elig["DaysBack"] - 90).abs()).sort_values("diff")
    try:
        prev_val = int(elig.iloc[0][column])
        return int(current_value) - prev_val
    except (ValueError, TypeError):
        return None


# ─── Build sheets ─────────────────────────────────────────────

def _notes_df(stats: dict) -> pd.DataFrame:
    rows = [
        ("Source",        "ETMoney public stock pages (mfSchemeList)"),
        ("Sitemap",       SITEMAP_URL),
        ("Holdings cache", f"{ETM_CACHE_DIR.relative_to(PROJECT_ROOT)} (TTL 30d)"),
        ("Map cache",      f"{ETM_MAP_FILE.relative_to(PROJECT_ROOT)} (permanent)"),
        ("History",        f"{ETM_HISTORY_FILE.relative_to(PROJECT_ROOT)} "
                           "(per-month FundCount snapshots; QoQ needs ≥2 months)"),
        ("Stocks resolved", f"{stats.get('resolved', 0)} / "
                            f"{stats.get('total', 0)}"),
        ("Total fund-stock rows", str(stats.get("rows", 0))),
        ("Crowding score", "FundCount * AvgWeight% — heavier = bigger "
                           "redemption-cascade risk."),
        ("NewEntriesMoM",  "Schemes that newly entered this stock between the "
                           "two most recent AMFI disclosures (from ETMoney 'New "
                           "Entries' section)."),
        ("FullExitsMoM",   "Schemes that fully exited the stock over the same "
                           "period."),
        ("NetΔMoM",        "NewEntries − FullExits (net change in fund count "
                           "month-over-month)."),
        ("QoQΔFunds",      "FundCount today vs nearest snapshot 60–120 days "
                           "ago in our local history. Empty until ≥1 quarter "
                           "of run history accumulates."),
        ("QoQΔNewEntries", "NewEntriesMoM today vs ~3 months ago — is the pace "
                           "of inflows accelerating or decelerating?"),
        ("QoQΔFullExits",  "FullExitsMoM today vs ~3 months ago — is the pace "
                           "of redemptions accelerating or decelerating?"),
        ("Note on duplicates", "ETMoney lists Regular AND Direct plans of the "
                               "same fund; FundCount counts unique parent "
                               "schemes. AvgWeight%/MaxWeight% use parent-scheme "
                               "max to avoid double counting."),
        ("Refresh",       "AMFI portfolios update monthly; cache expires in 30d."),
    ]
    return pd.DataFrame(rows, columns=["Field", "Value"])


def run(verbose: bool = True) -> dict:
    if verbose:
        print("  [mf] Loading holdings & ETMoney slug map …")
    h = load_holdings(verbose=False)
    if h.empty:
        return {"sheets": {
            "MF Crowding Summary":  pd.DataFrame([{"Note": "no holdings"}]),
            "MF Overlap Notes":     _notes_df({}),
        }}
    h = h[h["PresentValue"].fillna(0) > 0].copy()
    h["Symbol"] = h["Symbol"].astype(str).str.upper().str.strip()
    # Dedupe by symbol
    h = h.groupby("Symbol", as_index=False).agg({
        "Company":      "first",
        "Sector":       "first",
        "PresentValue": "sum",
    })
    total = h["PresentValue"].sum()
    h["MyWeight%"] = (h["PresentValue"] / total * 100).round(2) if total else 0

    slugs = _load_slug_map()
    if not slugs:
        return {"sheets": {
            "MF Crowding Summary":  pd.DataFrame(
                [{"Note": "ETMoney sitemap unreachable"}]),
            "MF Overlap Notes":     _notes_df({}),
        }}
    if verbose:
        print(f"  [mf] sitemap: {len(slugs)} stocks")

    nse_map = _load_nse_map()
    sym_to_name = _symbol_to_name()
    history = _load_history()

    summary_rows = []
    new_history_rows = []
    resolved = 0
    total_fund_rows = 0
    for _, row in h.iterrows():
        sym = row["Symbol"]
        comp = str(row.get("Company") or "").strip()
        if not comp:
            comp = sym_to_name.get(sym, "")
        if verbose:
            print(f"    {sym} ({comp}) …")
        entry = _resolve_symbol(sym, comp, slugs, nse_map, verbose=verbose)
        if not entry:
            summary_rows.append({
                "Symbol": sym, "Company": comp, "Sector": row.get("Sector"),
                "MyWeight%": row["MyWeight%"], "PresentValue": row["PresentValue"],
                "FundCount": 0, "NewEntriesMoM": None, "FullExitsMoM": None,
                "NetΔMoM": None, "QoQΔFunds": None,
                "QoQΔNewEntries": None, "QoQΔFullExits": None,
                "AvgWeight%": None, "MaxWeight%": None,
                "TopFund": None, "CrowdingScore": 0,
                "AsOf": None, "Status": "unmapped",
            })
            continue
        payload = _fetch_holdings_for_stock(sym, entry, verbose=verbose)
        resolved += 1
        holds = payload.get("rows", [])
        mv = payload.get("movements", {}) or {}
        if not holds:
            summary_rows.append({
                "Symbol": sym, "Company": comp, "Sector": row.get("Sector"),
                "MyWeight%": row["MyWeight%"], "PresentValue": row["PresentValue"],
                "FundCount": 0, "NewEntriesMoM": mv.get("NewEntries"),
                "FullExitsMoM": mv.get("FullExits"), "NetΔMoM": None,
                "QoQΔFunds": None,
                "QoQΔNewEntries": None, "QoQΔFullExits": None,
                "AvgWeight%": None, "MaxWeight%": None,
                "TopFund": None, "CrowdingScore": 0,
                "AsOf": None, "Status": "no MF holders",
            })
            continue

        df = pd.DataFrame(holds)
        df["Weight%"] = pd.to_numeric(df["Weight%"], errors="coerce")
        df = df.dropna(subset=["Weight%"])
        if df.empty:
            continue
        total_fund_rows += int(df.shape[0])

        # Aggregate at parent-scheme level to avoid Direct+Regular double-count
        parent_max = df.groupby("parentSchemeName")["Weight%"].max()
        top_fund = parent_max.idxmax() if len(parent_max) else None
        asof = df["AsOf"].dropna().max() if "AsOf" in df.columns else None
        fund_count = int(parent_max.shape[0])

        # Movement (MoM from page)
        ne = mv.get("NewEntries")
        fe = mv.get("FullExits")
        net_mom = (int(ne) - int(fe)) if (ne is not None and fe is not None) else None

        # QoQ from our own snapshot history
        qoq = _qoq_change(history, sym, asof, fund_count) if asof else None
        qoq_new = (_qoq_change(history, sym, asof, ne, "NewEntriesMoM")
                   if (asof and ne is not None) else None)
        qoq_exit = (_qoq_change(history, sym, asof, fe, "FullExitsMoM")
                    if (asof and fe is not None) else None)

        # Append to history (skip if already recorded)
        if asof:
            already = ((history["Symbol"] == sym) &
                       (history["AsOf"].astype(str) == str(asof))).any() \
                      if not history.empty else False
            if not already:
                new_history_rows.append({
                    "Symbol": sym, "AsOf": str(asof),
                    "FundCount": fund_count,
                    "NewEntriesMoM": ne, "FullExitsMoM": fe,
                })

        summary_rows.append({
            "Symbol":         sym,
            "Company":        comp,
            "Sector":         row.get("Sector"),
            "MyWeight%":      row["MyWeight%"],
            "PresentValue":   row["PresentValue"],
            "FundCount":      fund_count,
            "NewEntriesMoM":  ne,
            "FullExitsMoM":   fe,
            "NetΔMoM":        net_mom,
            "QoQΔFunds":      qoq,
            "QoQΔNewEntries": qoq_new,
            "QoQΔFullExits":  qoq_exit,
            "AvgWeight%":     round(float(parent_max.mean()), 3),
            "MaxWeight%":     round(float(parent_max.max()), 3),
            "TopFund":        top_fund,
            "CrowdingScore":  round(float(fund_count * parent_max.mean()), 2),
            "AsOf":           asof,
            "Status":         "ok",
        })

    # Persist new snapshots
    if new_history_rows:
        history = pd.concat([history, pd.DataFrame(new_history_rows)],
                            ignore_index=True)
        history = history.drop_duplicates(subset=["Symbol", "AsOf"], keep="last")
        _save_history(history)

    summary_df = pd.DataFrame(summary_rows)
    if not summary_df.empty:
        summary_df = summary_df.sort_values("CrowdingScore", ascending=False)
    else:
        summary_df = pd.DataFrame([{"Note": "no holdings to scan"}])

    stats = {
        "resolved": resolved,
        "total":    int(h.shape[0]),
        "rows":     total_fund_rows,
    }
    if verbose:
        print(f"  [mf] resolved {resolved}/{h.shape[0]} stocks, "
              f"{stats['rows']} fund-stock rows")

    return {"sheets": {
        "MF Crowding Summary":  summary_df,
        "MF Overlap Notes":     _notes_df(stats),
    }}


def main():
    ap = argparse.ArgumentParser(description="MF overlap (auto-discover via ETMoney)")
    ap.add_argument("--out", default=str(PORTFOLIO_DIR / "mf_overlap.xlsx"))
    args = ap.parse_args()
    result = run()
    with pd.ExcelWriter(args.out, engine="openpyxl") as w:
        for name, df in result["sheets"].items():
            df.to_excel(w, sheet_name=name[:31], index=False)
    print(f"\n  ✓ Wrote {args.out}")


if __name__ == "__main__":
    main()
