"""
sector_breadth.py — sector-level market breadth for the "Breadth" tab
======================================================================

SUMMARY
-------
Renders a standalone HTML page with five sub-tabbed views. Each sub-tab is a
small-multiples grid holding one separate line chart per NSE sector, plus a
NIFTY 500 benchmark panel, and carries its own explainer block describing what
the metric is and how to read it:

  1. % stocks above 200-EMA  — primary trend participation
  2. % stocks above 50-EMA   — intermediate participation
  3. % stocks above 20-EMA   — short-term stretch / timing
  4. New 52-week highs       — leadership expansion
  5. Advance-Decline line    — cumulative net participation

The three EMA sub-tabs share a fixed 0-100 y-range so sectors stay comparable
at a glance. The new-highs and A-D sub-tabs autoscale per panel, because a
single runaway sector otherwise squashes every other panel into a flat line.

The moving averages are **exponential**, not simple. That is a deliberate
departure from the published convention: "% above 200-DMA" as quoted by market
data vendors is a simple average, so these readings turn earlier but are not
comparable with those figures. Labels say EMA everywhere so the chart cannot
misrepresent its own maths.

DATA SOURCE
-----------
All data comes from `portfolio.premarket_dashboard.compute_sector_breadth()`,
which covers two sector taxonomies in one download:

  * the 20 official NSE ``Industry`` buckets (ind_nifty500list.csv, cached
    weekly) — the macro view, and
  * the 41 curated fine-grained sectors from ``index_constituents.json``
    (PSBs, Transformers, SpecialityChemicals …), shown with a ``C:`` prefix
    and grouped after the NSE panels.

The custom sectors pull in ~450 symbols from outside the NIFTY 500, so this
tab now drives a materially larger download than the NSE-only version did.
That is the price of segregation the 20-bucket taxonomy cannot express.

NORMALISATION (deliberate deviation from raw counts)
----------------------------------------------------
Sector member counts range from ~5 to ~101 stocks, so raw counts are not
comparable across sectors — Financial Services would dominate every chart
purely by size. Therefore:

  * New 52-week highs is plotted as % of the stocks *eligible* that day
    (``HiLoBase``: printed a bar and carry a full 52-week window); the
    stock count is shown in the hover tooltip.
  * The A-D line is plotted as cumulative net up-days *per stock*: each
    day's net advance is divided by that day's eligible count
    (``AdvDecBase``) before the cumulative sum, so a day when half the
    sector did not trade cannot masquerade as a weak day. It is rebased to
    0 at the left edge of the window, so read slope and sign, not level;
    the raw cumulative count is shown in the hover tooltip.

The percentage denominators are the per-metric eligible counts produced by
``_breadth_from_px``, never the sector roster — a member with no bar or too
short a history would otherwise silently deflate the reading.

The EMA charts are already percentages and need no adjustment.

OUTPUT
------
`<output_prefix>.html` — self-contained (Plotly via CDN), designed to be
iframe-embedded by run_all.build_combined_chart(). No Excel output.

READING THE CHARTS
------------------
Each sub-tab renders its own detailed explainer above the grid (see the
``PANELS`` table). In short:

  * >60% above 200-EMA  = healthy participation; <30% = washed out.
  * Index rising while % above 200-EMA falls = narrowing, late-stage rally.
  * 20-EMA washed out while 200-EMA stays healthy = strong sector on sale.
  * A rising A-D line confirms a rally; a flat/falling one warns it is
    carried by a handful of names.
"""

import os
import sys
import math
import datetime as dt

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from portfolio.premarket_dashboard import compute_sector_breadth  # noqa: E402

# Display window (calendar days). Matches premarket_dashboard's own breadth
# lookback so both share the identical download window and cache entries.
LOOKBACK_DAYS = 180

# Sectors with fewer members than this are dropped: in a 4-stock bucket
# "% above 200-EMA" can only take the values 0/25/50/75/100, which makes any
# threshold rule a coin flip. Five is the floor the curated custom sectors
# need; read anything in the 5-8 range as a hint, not a signal.
MIN_SECTOR_STOCKS = 5

BENCHMARK = "NIFTY 500"

# Marks a panel as coming from index_constituents.json rather than the NSE
# Industry column; must match premarket_dashboard.CUSTOM_SECTOR_PREFIX.
CUSTOM_PREFIX = "C: "

# Small-multiples grid: each sector gets its own chart inside every sub-tab.
# All four are pixels; the figure height and the subplot spacing fraction are
# derived from them so the panels keep a constant size as sectors are added.
GRID_COLS = 3
PANEL_HEIGHT = 210   # plot height of one grid row
ROW_GAP = 54         # gap between rows; holds the next row's subplot title
MARGIN_T = 30
MARGIN_B = 30

# NSE industry names are too long for a narrow subplot title and overlap the
# neighbouring panel, so the worst offenders get a short display label.
SHORT_NAMES = {
    "Automobile and Auto Components": "Automobile",
    "Fast Moving Consumer Goods": "FMCG",
    "Information Technology": "IT",
    "Oil Gas & Consumable Fuels": "Oil & Gas",
    "Construction Materials": "Constr. Materials",
    "Media Entertainment & Publication": "Media",
    "Telecommunication": "Telecom",
    "C: HealthcareServiceProvider": "C: Healthcare Svc",
    "C: SpecialityChemicals": "C: Speciality Chem",
    "C: FinancialInstitution": "C: Fin. Institution",
    "C: InvestmentCompanies": "C: Investment Cos",
    "C: AssetManagement": "C: Asset Mgmt",
    "C: HeavyElectricals": "C: Heavy Electricals",
    "C: MedicalEquipment": "C: Medical Equip",
    "C: Aerospace&Defense": "C: Aero & Defence",
    "C: Exchange&Brokers": "C: Exch & Brokers",
    "C: WealthManagement": "C: Wealth Mgmt",
    "C: FintechCompanies": "C: Fintech",
    "C: HoldingCompanies": "C: Holding Cos",
    "C: AutoComponents": "C: Auto Components",
    "C: OtherIndustrial": "C: Other Industrial",
}

# Sub-tab definitions: (label, value column, y-axis title, hover column,
# hover label, explainer HTML). The hover label names what the un-normalised
# number actually is — it differs per tab, so a generic "raw" was ambiguous.
# The explainer is rendered above each grid; every metric here is easy to
# misread, and the failure mode is silent.
PANELS = [
    ("% above 200-EMA", "Above200EMA%", "% of stocks above 200-EMA", None, None,
     """<b>What it is.</b> For each sector, the share of its stocks trading above
     their own 200-period exponential moving average, plotted daily.
     <b>What it does.</b> The 200-EMA is the slow trend line, so this counts how
     many members are in a primary uptrend. It measures <i>participation</i>, not
     price: a sector can rise while this falls, which means fewer and fewer names
     are carrying it.
     <b>How to read it.</b> Above 60% = broad, healthy trend. 30&ndash;60% = mixed,
     stock-picking matters more than the sector call. Below 30% = washed out;
     historically closer to a bottom than a top, but never a buy signal on its own.
     The single most useful pattern is <b>divergence</b> &mdash; sector index making
     new highs while this line rolls over is a narrowing, late-stage rally.
     Crossing back above 30&ndash;40% after a washout is an early repair signal.
     <b>Watch out.</b> It is slow by design and will not call a top on time."""),

    ("% above 50-EMA", "Above50EMA%", "% of stocks above 50-EMA", None, None,
     """<b>What it is.</b> Same idea over a 50-period EMA &mdash; roughly a
     two-and-a-half month trend.
     <b>What it does.</b> Tracks the intermediate swing rather than the primary
     trend, so it turns weeks before the 200-EMA version does.
     <b>How to read it.</b> Read it <i>against</i> the 200-EMA panel, not alone.
     50-EMA high while 200-EMA low = a young recovery, the sector is turning up
     from a damaged base. 50-EMA low while 200-EMA high = an ordinary pullback
     inside an intact uptrend, which is where continuation entries live. Both
     high = mature, extended trend. Both low = genuine downtrend, stand aside.
     <b>Watch out.</b> It whipsaws. Treat readings between 40% and 60% as noise
     and act only on sustained moves through the extremes."""),

    ("% above 20-EMA", "Above20EMA%", "% of stocks above 20-EMA", None, None,
     """<b>What it is.</b> The share of a sector's stocks above their 20-period
     EMA &mdash; about one month of trading.
     <b>What it does.</b> This is the fast, short-term thermometer. It is the
     first of the three to move and is best treated as a timing and
     stretch gauge rather than a trend measure.
     <b>How to read it.</b> Above 80% = short-term overbought; the sector is
     extended and chasing here usually gets a worse fill within days. Below 20%
     = short-term oversold, a bounce is likely if the 200-EMA panel is still
     healthy. The high-conviction setup is <b>20-EMA washed out while the
     200-EMA panel stays above 60%</b>: a strong sector on temporary sale.
     A surge from below 20% to above 60% within a week or two is a thrust and
     often marks the start of a new leg.
     <b>Watch out.</b> This is the noisiest of the three panels. On its own it
     generates far more signals than are worth trading."""),

    ("New 52-week highs", "NewHighs%", "% of stocks at 52w closing high",
     "New52wHighs", "stocks at high",
     """<b>What it is.</b> The percentage of a sector's eligible stocks whose
     close today is the highest close of the last 252 sessions. Strict &mdash;
     there is no tolerance band, and "near the high" does not count. Hover to see
     the raw stock count.
     <b>What it does.</b> Measures <i>leadership</i> rather than participation.
     A stock at a 52-week high has no overhead supply left: nobody who bought in
     the past year is sitting on a loss waiting to sell into strength.
     <b>How to read it.</b> Any sustained non-zero reading is meaningful; this
     metric spends most of its life near zero, so spikes matter more than levels.
     Expanding new highs alongside a rising sector confirms real strength.
     The warning sign is the sector index grinding higher while new highs
     <b>dry up</b> &mdash; the move has lost its leaders. A sector printing new
     highs while most others print none is exactly where rotation is heading.
     <b>Watch out.</b> This needs a full year of history, so recently listed
     stocks are excluded from the denominator until they qualify."""),

    ("Advance-Decline line", "ADLinePer", "Net up-days per stock (cumulative)",
     "ADLine", "sector total (up-days minus down-days)",
     """<b>What it is.</b> A running total of (stocks up today &minus; stocks down
     today), divided each day by the number that actually traded, then summed.
     It is rebased to zero at the left edge of the window, so only slope and sign
     carry meaning &mdash; the height is arbitrary. Hover for the raw count.
     <b>What it does.</b> Strips price out entirely and measures how many names
     are pulling their weight, treating every stock equally regardless of size.
     <b>How to read it.</b> <b>Slope is everything.</b> Rising = broad
     accumulation. Flat = the sector is going nowhere internally even if the
     index moves. Falling = distribution. The key use is confirmation: when the
     sector index rises and this line rises with it, the move is real. When the
     index rises and this line stalls or falls, a handful of heavyweights are
     carrying it and the rally is fragile. That divergence typically appears
     weeks before price turns.
     <b>Watch out.</b> It counts <i>direction</i>, not magnitude &mdash; twenty
     stocks up 0.1% outrank two stocks up 8%."""),
]

PALETTE = [
    "#1f77b4", "#ff7f0e", "#2ca02c", "#d62728", "#9467bd", "#8c564b",
    "#e377c2", "#7f7f7f", "#bcbd22", "#17becf", "#393b79", "#637939",
    "#8c6d31", "#843c39", "#7b4173", "#3182bd", "#e6550d", "#31a354",
    "#756bb1", "#636363", "#a1d99b",
]


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Add the size-normalised columns the charts plot.

    Both metrics divide by that day's *eligible* stock count, not by
    ``Members``: a member with no bar or too little history cannot make a new
    high or an advance, so counting it in the denominator would understate
    the sector. The A-D line normalises the daily net advance before the
    cumulative sum, so days with uneven participation carry equal weight.
    """
    nan = float("nan")
    out = df.sort_values(["Sector", "Date"]).copy()

    hilo_base = out["HiLoBase"].astype(float).replace(0, nan)
    out["NewHighs%"] = (out["New52wHighs"].astype(float) / hilo_base * 100).round(2)

    ad_base = out["AdvDecBase"].astype(float).replace(0, nan)
    net_per = (out["Advances"].astype(float) - out["Declines"].astype(float)) / ad_base
    out["ADLinePer"] = net_per.groupby(out["Sector"]).cumsum().round(3)
    return out


def _build_figure(df: pd.DataFrame, value_col: str, y_title: str,
                  hover_col: str | None, hover_label: str | None) -> go.Figure:
    """Small-multiples grid: one separate line chart per sector.

    Every sector gets its own axes so its level and slope can be read without
    untangling it from 57 other lines.

    Geometry is pixel-derived. Plotly treats ``vertical_spacing`` as a share of
    the *whole* plotting area, so any fixed fraction collapses once the grid
    grows: at 20 rows a 0.047 spacing spends 90% of the figure on gaps and
    leaves each panel ~20px tall. The gap is pinned at ``ROW_GAP`` and the
    figure height is grown to match instead.

    The EMA panels keep a fixed 0-100 axis because the metric is bounded and
    the level itself is the signal. The new-high and A-D panels autoscale per
    panel: one runaway sector otherwise flattens all 58 others into hairlines.
    """
    sectors = [s for s in df["Sector"].unique() if s != BENCHMARK]
    # NSE macro buckets first, curated custom sectors after, each alphabetical:
    # reading the macro picture before the fine cuts is the whole point of
    # carrying both taxonomies.
    order = ([BENCHMARK] if BENCHMARK in set(df["Sector"]) else []) + sorted(
        sectors, key=lambda s: (s.startswith(CUSTOM_PREFIX), s))
    sizes = df.groupby("Sector")["Members"].last()

    rows = max(1, math.ceil(len(order) / GRID_COLS))
    titles = ["%s (%d)" % (SHORT_NAMES.get(s, s), int(sizes.get(s, 0)))
              for s in order]

    plot_h = rows * PANEL_HEIGHT + (rows - 1) * ROW_GAP
    fig = make_subplots(
        rows=rows, cols=GRID_COLS, subplot_titles=titles,
        shared_xaxes=False,
        vertical_spacing=(ROW_GAP / plot_h) if rows > 1 else 0.0,
        horizontal_spacing=0.055,
    )

    is_pct = value_col.startswith("Above")
    y_range = [0, 100] if is_pct else None

    for i, sector in enumerate(order):
        r, c = divmod(i, GRID_COLS)
        r, c = r + 1, c + 1
        sub = df[df["Sector"] == sector].sort_values("Date")
        if sub.empty:
            continue
        is_bm = sector == BENCHMARK
        if hover_col is not None:
            custom = sub[[hover_col]].to_numpy()
            hover = ("<b>" + sector + "</b><br>%{x}<br>" + y_title
                     + ": %{y:.2f}<br>" + hover_label
                     + ": %{customdata[0]:.0f}<extra></extra>")
        else:
            custom = None
            hover = ("<b>" + sector + "</b><br>%{x}<br>" + y_title
                     + ": %{y:.2f}<extra></extra>")
        fig.add_trace(go.Scatter(
            x=sub["Date"], y=sub[value_col], name=sector, mode="lines",
            customdata=custom, hovertemplate=hover, showlegend=False,
            line=dict(color="#000000" if is_bm else PALETTE[i % len(PALETTE)],
                      width=2.0 if is_bm else 1.6),
        ), row=r, col=c)

        if is_pct:
            for level, colour in ((60, "#2e7d32"), (50, "#9e9e9e"), (30, "#c62828")):
                fig.add_hline(y=level, line=dict(color=colour, width=1, dash="dot"),
                              row=r, col=c)
        elif value_col == "ADLinePer":
            fig.add_hline(y=0, line=dict(color="#9e9e9e", width=1, dash="dot"),
                          row=r, col=c)

        if y_range is not None:
            fig.update_yaxes(range=y_range, row=r, col=c)
        if c == 1:
            # Only the left column is labelled; repeating it on every panel
            # would eat the plot area without adding information.
            fig.update_yaxes(title_text=y_title, title_font=dict(size=9),
                             row=r, col=c)

    fig.update_annotations(font=dict(size=11))
    fig.update_xaxes(tickfont=dict(size=8), nticks=3, tickangle=0,
                     tickformat="%b %y", showgrid=True, gridcolor="#eeeeee")
    fig.update_yaxes(tickfont=dict(size=8), nticks=5,
                     showgrid=True, gridcolor="#eeeeee")
    fig.update_layout(
        template="plotly_white",
        height=plot_h + MARGIN_T + MARGIN_B,
        # No figure title: the active sub-tab button already names the metric,
        # and a centred title overlaps the first row of subplot titles.
        margin=dict(l=70, r=20, t=MARGIN_T, b=MARGIN_B),
        hovermode="x",
        showlegend=False,
    )
    return fig


_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Sector Market Breadth</title>
<style>
 body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#fafafa;color:#212121}
 .hdr{padding:12px 18px 4px;font-size:19px;font-weight:700}
 .sub{padding:0 18px 10px;font-size:12px;color:#616161}
 .subtab-bar{display:flex;gap:6px;padding:0 18px;border-bottom:2px solid #e0e0e0;flex-wrap:wrap}
 .subtab-btn{padding:8px 16px;border:none;background:#eceff1;color:#37474f;font-size:13px;
   font-weight:600;cursor:pointer;border-radius:6px 6px 0 0}
 .subtab-btn.active{background:#1565c0;color:#fff}
 .subtab-panel{display:none;padding:10px 12px}
 .subtab-panel.active{display:block}
 .explain{margin:4px 6px 12px;padding:11px 14px;background:#fff;border:1px solid #e0e0e0;
   border-left:4px solid #1565c0;border-radius:5px;font-size:12.5px;line-height:1.65;color:#37474f}
 .explain b{color:#0d47a1}
 .note{padding:6px 18px 18px;font-size:11.5px;color:#616161;line-height:1.55}
 .note b{color:#212121}
</style></head><body>
<div class="hdr">Sector Market Breadth &mdash; NIFTY 500 by NSE Industry + curated sectors</div>
<div class="sub">__SUBTITLE__</div>
<div class="subtab-bar">__BUTTONS__</div>
__PANELS__
<div class="note">
 <b>Moving averages are exponential (EMA), not simple.</b> An EMA weights recent
 closes more heavily, so these readings turn a few sessions earlier than the
 conventional "% above 200-DMA" statistics published elsewhere &mdash; they are
 faster, but not directly comparable to those numbers.
 New 52-week highs and the Advance-Decline line use no moving average at all.<br>
 <b>Normalisation:</b> new-52w-highs and the A-D line are shown per stock
 (the underlying counts are in the tooltip) because sector sizes range from
 __MINSZ__ to __MAXSZ__ stocks and raw counts are not comparable.
 Both divide by the stocks actually eligible that day, not by the sector
 roster. A "new 52w high" is a strict new <i>closing</i> high over the last
 252 sessions &mdash; no tolerance band. The A-D value is cumulative net
 up-days per stock since the left edge, so read its slope and sign, not its
 height.
 Sectors with fewer than __MINSTOCKS__ members are excluded as too small to
 give a meaningful percentage. Each sector has its own panel; the dotted
 lines on the EMA grids mark the 60% / 50% / 30% levels. EMA panels share a
 fixed 0-100 axis; the new-high and A-D panels autoscale per panel, so read
 their shape, not their height, against a neighbour.<br>
 <b>Panels prefixed <code>C:</code></b> come from the curated
 index_constituents.json cuts, not the NSE Industry column.
</div>
<script>
function _resizeBreadthPanel(p){
  if(!p || !window.Plotly) return;
  var d=p.querySelectorAll('.plotly-graph-div');
  for(var k=0;k<d.length;k++){ Plotly.Plots.resize(d[k]); }
}
function showBreadthTab(i){
  var b=document.querySelectorAll('.subtab-btn'),p=document.querySelectorAll('.subtab-panel');
  for(var k=0;k<b.length;k++){b[k].classList.toggle('active',k===i);}
  for(var k=0;k<p.length;k++){p[k].classList.toggle('active',k===i);}
  _resizeBreadthPanel(p[i]);
}
// This page is embedded in an iframe inside a hidden tab panel, so the charts
// are first laid out at zero width. Re-fit them the moment the body gains a
// real width (i.e. when the "Breadth" tab is opened).
(function(){
  if(!window.ResizeObserver) return;
  var last=0;
  new ResizeObserver(function(){
    var w=document.body.clientWidth;
    if(w>0 && w!==last){ last=w; _resizeBreadthPanel(document.querySelector('.subtab-panel.active')); }
  }).observe(document.body);
})();
</script>
</body></html>
"""


def build_html(df: pd.DataFrame, out_path: str) -> str:
    """Render the five sub-tabbed per-sector chart grids to a standalone file.

    Each sub-tab carries its own explainer block above the grid describing what
    the metric is, what it measures and how to read it, because every one of
    these metrics fails silently when misread.
    """
    df = _prepare(df)
    buttons, panels = [], []
    for i, (label, col, y_title, hover_col, hover_label, explain) in enumerate(PANELS):
        fig = _build_figure(df, col, y_title, hover_col, hover_label)
        div = fig.to_html(full_html=False,
                          include_plotlyjs=("cdn" if i == 0 else False),
                          config={"displaylogo": False})
        active = " active" if i == 0 else ""
        buttons.append('<button class="subtab-btn%s" onclick="showBreadthTab(%d)">%s</button>'
                       % (active, i, label))
        panels.append('<div class="subtab-panel%s"><div class="explain">%s</div>%s</div>'
                      % (active, explain, div))

    sizes = df.groupby("Sector")["Members"].last()
    sec_sizes = sizes.drop(labels=[BENCHMARK], errors="ignore")
    dates = df["Date"]
    subtitle = ("%d sectors + NIFTY 500 benchmark &middot; %s to %s &middot; generated %s"
                % (len(sec_sizes), dates.min(), dates.max(),
                   dt.datetime.now().strftime("%d-%b-%Y %H:%M")))

    html = (_HTML
            .replace("__SUBTITLE__", subtitle)
            .replace("__BUTTONS__", "\n".join(buttons))
            .replace("__PANELS__", "\n".join(panels))
            .replace("__MINSZ__", str(int(sec_sizes.min())) if len(sec_sizes) else "?")
            .replace("__MAXSZ__", str(int(sec_sizes.max())) if len(sec_sizes) else "?")
            .replace("__MINSTOCKS__", str(MIN_SECTOR_STOCKS)))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path


def run(output_prefix: str | None = None,
        lookback_days: int = LOOKBACK_DAYS,
        min_stocks: int = MIN_SECTOR_STOCKS,
        verbose: bool = True):
    """Compute sector breadth and write the chart. Returns (df, html_path).

    Returns ``(None, None)`` if no breadth data could be built, so callers
    can skip the tab without failing the pipeline.
    """
    df = compute_sector_breadth(lookback_days=lookback_days,
                                verbose=verbose,
                                min_stocks=min_stocks)
    if df is None or df.empty:
        print("  [breadth] No sector breadth data; chart skipped")
        return None, None

    prefix = output_prefix or os.path.join(SCRIPT_DIR, "sector_breadth")
    out_path = prefix + ".html" if not prefix.endswith(".html") else prefix
    build_html(df, out_path)
    n_sectors = df["Sector"].nunique() - (1 if BENCHMARK in set(df["Sector"]) else 0)
    if verbose:
        print("  [breadth] Chart written: %s (%d sectors, %d trading days)"
              % (os.path.basename(out_path), n_sectors, df["Date"].nunique()))
    return df, out_path


if __name__ == "__main__":
    run()
