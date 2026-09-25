"""
stage_analysis.py — Stan Weinstein Stage Analysis (weekly, top-down)
====================================================================

SUMMARY
-------
Classifies every stock in the universe and every sector composite into
one of Weinstein's four stages using **weekly** bars and the **30-week simple
moving average**, then reports the top-down picture the method is built on:
which sectors are in Stage 2, which stocks inside those sectors are in Stage 2,
and — most importantly — which entities *changed stage this week*.

    Stage 1  Basing      flat 30W MA, price chopping around it   → WATCH
    Stage 2  Advancing   rising 30W MA, price above it           → BUY / HOLD
    Stage 3  Topping     flattening 30W MA, price stalling       → SELL / TRIM
    Stage 4  Declining   falling 30W MA, price below it          → AVOID / EXIT

DATA SOURCE
-----------
- Universe + sector map: ``portfolio.premarket_dashboard._fetch_all_sectors``,
  which merges two taxonomies — the official NSE ``ind_nifty500list.csv``
  ``Industry`` column (20 macro buckets, 500 names) and the curated
  ``index_constituents.json`` (41 fine-grained sectors, 697 names, labelled
  with a ``C:`` prefix). Roughly 450 of the curated names sit outside the
  NIFTY 500, so the universe is about double the old one.
  A stock therefore belongs to **one NSE industry and at most one custom
  sector**; the symbol -> sector lookup is one-to-many and the stock sheet
  reports each qualifying stock under its best-ranked focus sector.
- Daily OHLCV: ``angel_client.angel_download_many`` (Angel One SmartAPI,
  served through the persistent ohlcv_cache), falling back per-symbol to
  ``data_provider.download`` (jugaad-data → yfinance) for anything Angel
  cannot serve.
- Benchmark: ``^CRSLDX`` (Nifty 500 index).

WHY SECTOR COMPOSITES INSTEAD OF THE READY-MADE NSE SECTOR INDICES
------------------------------------------------------------------
The published NSE sector indices carry **no volume** (the cached index
history is Close-only, and the Angel index feed returns Volume=0), so
Weinstein's volume-confirmation rules cannot be applied to them at all.
They also use a different taxonomy again, which would break the sector→stock
drill-down this report exists for.
So each sector here is an **equal-weight composite of its own constituents**
(built with ``custom_sector_index.calculate_equal_weight_index``,
which clips ±35% daily moves to absorb splits/demergers), with sector volume
taken as the *average volume per constituent* (sum ÷ number reporting, so the
series is not distorted when a constituent's history starts late).
Trade-off: an equal-weight composite has a small-cap tilt versus the
cap-weighted ``^CRSLDX`` benchmark, so absolute Mansfield RS levels run a
little high across the board; the *ranking* between sectors is unaffected.
That tilt is stronger for the curated ``C:`` sectors, many of which are
deliberately microcap — compare those against each other, not against the
NSE macro buckets.

WEEKLY BARS
-----------
No provider in this workspace serves weekly candles (``data_provider`` punts
any non-``1d`` interval to yfinance), so weekly bars are resampled from daily
with ``W-FRI`` (O=first, H=max, L=min, C=last, V=sum).
**Only completed weeks are used.** The in-progress week is dropped, because a
partial bar makes the 30-week MA slope flicker intraweek and would fire false
entries into the transitions sheet — the single most valuable output here.
Consequence: the report is as-of the last completed weekly close, which can be
up to four sessions behind today. That date is printed in every output.

CLASSIFICATION RULES (deterministic — two clean cases, one tiebreak)
-------------------------------------------------------------------
    slope    = % change of the 30W MA over the last SLOPE_WEEKS weeks
    persist  = share of the last PERSIST_WEEKS that closed above the MA
    hh / hl  = higher highs / higher lows over STRUCT_WEEKS swings
    pctile   = position of Close inside its trailing 52-week high/low range

    slope >  FLAT_BAND  and persist >= 0.8 and higher lows  →  Stage 2
    slope < -FLAT_BAND  and persist <= 0.2 and lower highs  →  Stage 4
    everything else                                         →  Stage 1 or 3

Weinstein's wording matters here and this file follows it literally: Stage 2 is
price **consistently** above a rising MA, not price that happens to be above it
this week, and "if you don't see higher lows, it's not Stage 2 — full stop."
Testing only the latest bar (as this file did until the persistence and
structure tests were added) labelled every one-week poke through the MA an
advance, which was the single largest source of misclassification.

One more condition that the MA tests do not imply: Stage 2 begins at a
**breakout above the base**, so price must also sit in the top 1-S2_MIN_PCTILE
of its 52-week range. A rising MA with a run of closes above it happens well
before price leaves the range, and without this test the engine called the
advance far too early — see MEASURED ACCURACY below.

"Everything else" is a flat MA, or price sawing through a trending MA — the
transition zones. Stage 1 and Stage 3 are mathematically identical there, so
they are separated by **cycle position**, which is what actually defines them:
Weinstein's cycle only runs 1 → 2 → 3 → 4 → 1, so a transition zone that
follows an advance is a top and one that follows a decline is a base.

    last decisive stage was 2  →  Stage 3   (topping)
    last decisive stage was 4  →  Stage 1   (basing)

That memory expires after STALE_WEEKS, and it does not exist at all at the
start of a series, so those cases fall back to a chart-shape tiebreak — where
price sits inside its trailing 52-week range:

    pctile >= HIGH_PCTILE                    →  Stage 3   (stalling high in the range)
    pctile <= LOW_PCTILE                     →  Stage 1   (basing low in the range)
    otherwise, prior PRIOR_WEEKS return,
    measured ending FLAT_OFFSET weeks ago:
        positive → Stage 3, else → Stage 1

The percentile test is skipped when the 52-week range is narrower than
MIN_RANGE_PCT, because inside a tight range a 2% wiggle spans most of the
percentile scale and the reading is noise.

Two structural overrides then run, both straight from the definitions:

    Stage 3 with pctile < S3_MIN_PCTILE      →  Stage 1
        Distribution happens near the highs. Once price has slid to the floor
        of its own 52-week range the top is over; what is left is a base.
    Stage 3 by memory, MA rising, not cleared → Stage 1
        A top is distribution *after* an advance. A name that never cleared
        its base never advanced, so there is nothing to distribute.
    Stage 1 under a still-falling MA         →  Stage 4
    Stage 3 over a still-rising MA           →  Stage 2  (only if cleared)
        A base needs a *flat* MA. While the 30W MA is still falling and price
        is under it, the decline has not finished, whatever the cycle says.

SUB-STAGES (the A/B halves — where the instruction actually lives)
------------------------------------------------------------------
The stage number alone is not actionable; each stage has two halves carrying
opposite orders. ``sub_stage`` splits them:

    1A  base too young / MA still falling      WAIT
    1B  MA flat, base mature, price at ceiling WATCH for the breakout
    2A  first EARLY_WEEKS of the advance       BUY — best entry
    2B  late advance                           HOLD, tighten stops, do not add
    3A  top just forming                       TRIM HALF, raise stop
    3B  200D rolling over / support broken     SELL REST INTO ANY RALLY
    4A  first EARLY_WEEKS of the decline       EXIT ALL
    4B  extended decline                       AVOID — do not bottom-fish

Base and top age is taken from ``BaseAge`` (weeks since the MA slope last
exceeded TREND_BAND_MULT x the flat band), not from the age of the confirmed
stage label. The MA flattens weeks before the CONFIRM_WEEKS memory lets the
number change, so judging a base by the label made 1B unreachable — measured,
it fired on 0 of 34 real bases. A strict run-length counter on the flat band
was no better (2%): one noisy week reset it. Weeks-since-a-real-trend is the
noise-tolerant version and calls 18% of bases mature, which is the share that
survives inspection.

The two *entry* actions are additionally gated on relative strength by
``action_for``: a 2A with RS at or below zero prints WAIT, not BUY. Weinstein
never buys a breakout that is lagging its index, and without this gate the
same row could print BUY while its own RS FADING warning was lit. Sell and
exit actions are deliberately not gated — waiting for RS to confirm a top is
how a round trip happens.

A separate gate covers RS being *unknown* rather than weak. Mansfield RS needs
RS_WEEKS of history, so a listing that clears MIN_BARS but is under a year old
has none, and roughly 2.5% of the report was previously issuing confident
instructions with that field silently blank. Those rows now carry a NO RS
warning, 2B is demoted to do-not-add, and the Stage 3 sells are annotated but
not suppressed.

BEYOND THE MOVING AVERAGE
-------------------------
- **Volume direction** (``VolDir``): mean up-week volume ÷ mean down-week
  volume over VOL_DIR_WEEKS. Above 1 is accumulation, below 1 is distribution.
  This inverts weeks before the 30W MA flattens and is the earliest tell in the
  method — a point-in-time "volume vs its average" cannot see it.
- **10-week (50-day) MA violation on heavy volume**: Weinstein's first red flag
  in a Stage 2 advance, and it fires before the 30W MA moves at all.
- **40-week (200-day) slope**: rolling over is what splits 3A from 3B.
- **Box levels**: the support and resistance price has traced out inside the
  current stage. For Stage 1 the ceiling is the breakout level (reported as
  ``% to Breakout``); for Stage 3 the floor is the support whose failure starts
  Stage 4.
- **RS fading**: Mansfield RS below its own RS_PEAK_WEEKS peak while still in
  Stage 2. RS turns down before price does. **RS sell** is the harder version —
  RS below zero *and* falling three weeks running, Weinstein's exit trigger.
  **No RS** is the absence of both: too little history to measure at all.
- **Trend template**: Minervini's eight checks, scored on the weekly
  equivalents of his 50/150/200-day averages (10W/30W/40W). Price above the
  30W and 40W, 30W above 40W, 40W already rising, 10W above both, price above
  the 10W, at least TT_ABOVE_LOW above the 52-week low, within TT_BELOW_HIGH
  of the 52-week high, and positive RS. A full pass is half of what earns
  setup grade A; roughly a third of Stage 2 names clear all eight.
- **Setup grade**: A is a full trend template *plus* coiling — price inside
  NEAR_HIGH_PCT of the 52-week high with two-week volume under VOL_DRYUP of
  its ten-week average. B is Stage 2 with RS above zero, C is Stage 2 with RS
  below zero. A deliberately rewards silence, not a volume breakout, because
  the coiled version outranked the breakout version at every horizon tested —
  but the margin does not survive counting each stock once, so treat A as a
  screen rather than an edge. See `_setup_grade` for the numbers.
- **VCP**: the depths of the last VCP_LEGS pullbacks, measured by a zigzag so
  that successive swings are counted as swings rather than smeared by a
  rolling window. Confirmed when each leg is shallower than the one before and
  the last is at most VCP_SHRINK of its predecessor. It is deliberately rare.
- **Buy ready**: Stage 2 only, and reported as a count out of four with the
  failing tells named rather than as a yes/no, because a 3-of-4 near-miss is
  the useful output. The four are a confirmed VCP, volume drying up, weekly
  range drying up, and price within NEAR_HIGH_PCT of the 52-week high.
- **Top quality**: for Stage 3 only, six points across five tells (RS above
  zero, no distribution, price back over the 10W MA, tight box, no lower
  highs) scoring the odds that the pause resolves back up into Stage 2 instead
  of down into Stage 4. Volume direction carries two of the six because it is
  the only one of the five that is not a restatement of where price sits.
- **Stops**: structural, never a percentage. Stage 2 trails below both the
  recent reaction low and the 30W MA; Stage 1 sits below the base floor.
- **Volume confirmation on transitions**: breakouts and breakdowns are checked
  against VOL_BREAKOUT (1.5x), Weinstein's floor. Flagged, not suppressed — the
  move happened, it just has no sponsorship behind it.
- **Volume spike**: a weekly ratio at or above VOL_SPIKE_MAX (10x) is warned
  about rather than trusted. Those signals lost money at every horizon tested,
  and lost most in the *most* liquid names, which is the signature of a block
  deal or a corporate event rather than of demand.

MEASURED ACCURACY — READ THIS BEFORE TRUSTING THE STAGE NUMBER
---------------------------------------------------------------
Scored against 100 blind human chart reads (stratified sample of the NIFTY 500
universe, symbols hidden, 3 years of weekly bars per chart):

    four-way stage, exact match      60% raw, 62% weighted  (was 51/53%)
    "in an uptrend or not"           88% raw
    off-by-one-phase tolerated       84%
    Cohen's kappa, four-way          0.38 before the Stage 2 gate below

The weighted figures re-weight the stratified sample back to universe
proportions; the raw ones are the plain hit rate. Quote the raw number unless
you mean the weighted one.

THE 80% FOUR-WAY TARGET IS UNREACHABLE AND HAS BEEN RETIRED. The same rater
re-labelled 25 of these charts blind, mixed with 8 fillers, months later:

    agreement with their own first pass   68%   (kappa 0.57)
    same, binary uptrend/not              88%

A single human pass is therefore itself only about 82% accurate against that
human's own settled view (two passes agreeing 68% of the time on four labels
implies each pass hits ~82%). No classifier scored against a single-pass label
can beat 82%, and one no better than the human would score 68%. Against that,
the engine's 56% is roughly 12 points short of human repeatability, not 24
short of 80. Aim at 68%.

The binary call is the one to trust: 88% against a human ceiling of 88%, so on
that output the classifier is now at the limit of what a human label can
verify. It got there via S2_MIN_PCTILE — the engine used to say Advancing 38
times where the human said it 25, and 11 of its 43 errors were "engine
Advancing, human Basing". It was declaring the advance before price had left
the base. Requiring the top 30% of the 52-week range moved four-way agreement
from 56% to 60% and binary from 81% to 88%, and the label mix now matches the
human exactly on Advancing (25) and Topping (8).

What remains is the mirror error: Basing is now over-called (48 against 42) and
Declining under-called (17 against 23). Three charts the human calls Advancing
(SCHNEIDER, J&KBANK, BLUEJET) are held in Basing by the gate. That is the price
of the trade and it was worth paying — 8 charts fixed against 4 broken — but it
is where the next improvement lives.

A 960-combination sweep of PERSIST_WEEKS, PERSIST_FRAC, FLAT_BAND_PCT,
S3_MIN_PCTILE and CONFIRM_WEEKS produced nothing above the shipped settings,
so the four-way number is a property of the definitions, not of the tuning.
Most of the residual disagreement is Stage 1 vs Stage 3: both are a flat MA,
and a human reading chart shape calls any quiet range a base, while Weinstein's
cycle calls a quiet range after an advance a top. **Trust the binary
uptrend/not signal and the sub-stage instruction; treat a bare 1-vs-3 call as a
prompt to look at the chart, not as an answer.**

Note that the trend template, VCP, buy-ready and top-quality outputs are *not*
included in that 59%. None of them can be — they never change the stage label,
so there is no human ground truth to score them against. They are ranking and
instruction aids, and they are unvalidated as forecasts.

And they are unvalidated in a specific, measurable way. Every forward-return
number quoted in this module is computed per signal against an equal-weighted
draw from the same universe. Weekly signals overlap heavily — 2,627 grade A
signals come from only 579 distinct stocks — so that count overstates the
evidence by roughly a factor of four. Scored one observation per stock, no
stage, grade or filter in this module beats a random draw from the same
universe: the control returns +0.04% at 13 weeks and +0.07% at 26, while every
grade lands between -1.6% and -4.2%. What survives is the *ranking* between
grades, not the claim that any of them makes money. Anyone extending this
module should reproduce that control before believing a new alpha number.

RULES TESTED AND REJECTED
-------------------------
Kept here so they are not re-proposed and re-implemented later:

- **Minimum Stage 2 duration before allowing Stage 3** (the idea that a flat MA
  under ~39 weeks into an advance is a pause, not a top). Implemented and swept
  over nine thresholds against the 98 scored charts. Agreement fell
  monotonically as the threshold rose — best at 0, i.e. the rule off — and at
  39 weeks Stage 3 detections dropped from 10 to 8 while "rule says Advancing,
  human says Basing" errors rose from 11 to 18. The reason is that a human
  reading a quiet range calls it a base; forcing it to Advancing moves away
  from them on both counts. Not adopted.
- **Distribution-day counting.** No predictive content in the published
  large-sample work, and it would add a daily dependency to a weekly module.
- **A fixed 7–8% stop.** Requires an entry price, which this module never sees;
  it classifies, it does not track positions. Stops here are structural.
- **A ceiling on distance above the 30W MA for grade A.** Grade A does run
  extended — median 23% above the MA, up to 176% — but forward alpha by
  distance bucket is not monotone. The 40-60% band is the weakest of the six
  while the 60%+ band is among the strongest at 26 weeks, so a cap only helps
  by excising one middle band. That is curve-fitting a single bucket.
- **Exit rules to rescue grade A.** Eight were tested — fixed holds, breaking
  the 30W MA, leaving Stage 2, 10% and 20% trails, and a 2x ATR stop — on an
  identical signal set with a full 52 weeks ahead of every signal. Tighter
  exits made expectancy *worse*, and Weinstein's own 30W MA rule produced a
  22.7% win rate. The signal is not being spoiled by holding too long.

CONFIRMATION
------------
A rangebound entity sits right on the Stage 1 / Stage 3 boundary and will flip
back and forth week to week, which would flood the transitions sheet with
noise. So a new stage is only adopted once it has printed for CONFIRM_WEEKS
consecutive weekly closes. Cost: every reported transition is one week later
than the raw crossover. That is the correct trade — Weinstein waits for the
weekly close to confirm anyway, and a signal you can trust beats a signal you
have to second-guess.

KNOWN LIMITS — READ BEFORE TRADING OFF THIS
--------------------------------------------
1. Stage 1 and Stage 3 are mathematically identical (both = flat MA). They are
   resolved here by cycle position — which decisive stage came last — falling
   back to a 52-week range tiebreak when that memory is missing or stale.
   Weinstein never published numbers for any of this. It is the largest
   misclassification risk in the file and it is the expensive one
   (Stage 1 = get ready to buy, Stage 3 = sell). It is also where essentially
   all of the 41% blind-test disagreement sits.
2. "Flat" needs a number. FLAT_BAND_PCT is that number. Widen it and you get
   more Stage 1/3 and fewer whipsaws; narrow it and stages flip more often.
   The sweep found 1.0 optimal, but it was optimal by a margin inside the
   noise, so do not read precision into it.
3. Stage boundaries are inherently 2–4 weeks late by construction — a 30-week
   MA cannot turn faster than that. This is the method working, not a bug.
4. Volume confirmation is applied to stocks and to sector composites, but there
   is no volume at all for the published NSE indices, so no index-level
   volume rule exists anywhere in this report. Every volume-derived column
   (VolDir, Vol Confirmed, the 50D-break warning) is silently NaN or blank for
   any entity fed a Close-only series.
5. The sub-stage split, box levels and warnings are heuristics fitted to
   Weinstein's prose, not to data. They have not been separately accuracy-
   tested — only the 1-4 stage number has.

OUTPUT (default prefix: stage_analysis)
---------------------------------------
- stage_analysis.xlsx   (standalone runs only)
    Stage Sectors      — every sector, current stage, Mansfield RS, ranked
    Stage Stocks       — stocks inside Stage-2 sectors, ranked
    Stage Transitions  — every stage change at the latest completed week
- stage_analysis.html   — 3 sub-tabs:
    1. Sector Stage Heatmap  — sectors x weeks, cell coloured by stage
    2. Market Stage Mix      — % of sectors in each stage over time
    3. Sector Mansfield RS   — RS lines, zero = in line with Nifty 500

USAGE
-----
    python3 stage_analysis.py                 # build xlsx + html
    python3 stage_analysis.py -o my_report    # custom output prefix

RUN_ALL INTEGRATION
-------------------
    Scenario name: stage_analysis
    Called as: stage_analysis.run(output_prefix=..., write_excel=False)
        → returns (sheets_dict, html_path)
    Skip with: python3 run_all.py --skip stage_analysis

DEPENDENCIES
------------
pandas, plotly, data_provider, angel_client, custom_sector_index,
portfolio.premarket_dashboard
"""

import os
import sys
import argparse
import datetime as dt
import math

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

import data_provider  # noqa: E402
from custom_sector_index import calculate_equal_weight_index  # noqa: E402
from portfolio.premarket_dashboard import _fetch_all_sectors  # noqa: E402

try:
    from llm_client import llm_json, is_available as llm_is_available
except ImportError:
    llm_json = None
    def llm_is_available(): return False

# ─── Tunables ────────────────────────────────────────────────────────────────

MA_WEEKS = 30          # Weinstein's 30-week SMA — the spine of the whole method
SLOPE_WEEKS = 5        # window the MA slope is measured over
FLAT_BAND_PCT = 1.0
TREND_BAND_MULT = 2.0  # slope beyond this multiple of the band = a real trend    # |MA change| under this over SLOPE_WEEKS = "flat" MA
RANGE_WEEKS = 52       # trailing window for the high/low percentile
HIGH_PCTILE = 0.70     # flat MA at/above this in its 52w range → Stage 3
LOW_PCTILE = 0.40      # flat MA at/below this → Stage 1
MIN_RANGE_PCT = 15.0   # 52w range narrower than this makes the percentile noise
PRIOR_WEEKS = 26       # prior-trend lookback used to break remaining ties
FLAT_OFFSET = 4        # prior trend is measured ending this many weeks back
STALE_WEEKS = 52       # after this long with no decisive stage, cycle memory expires
CONFIRM_WEEKS = 2      # consecutive weeks a new stage must hold to be adopted
RS_WEEKS = 52          # Mansfield RS baseline (52-week MA of the price ratio)
VOL_WEEKS = 10         # weekly-volume average used for the expansion ratio

# Weinstein criteria the MA slope alone cannot express. Stage 2 is "price
# CONSISTENTLY above a rising MA" and Stage 4 is "consistently below a falling
# one"; a stock slicing back and forth through its MA is Stage 1 or Stage 3 no
# matter what the slope reads. Testing only the latest bar (as this file used
# to) called every one-week poke above the MA an advance.
PERSIST_WEEKS = 10     # window used to judge "consistently" above/below the MA
PERSIST_FRAC = 0.80    # this share of PERSIST_WEEKS on one side = consistent
STRUCT_WEEKS = 13      # swing window for the higher-lows / lower-highs test
S3_MIN_PCTILE = 0.35   # a top forms high in the range; below this it is a base
S2_MIN_PCTILE = 0.70   # Stage 2 must have cleared its base: top 30% of the 52w range
FAST_MA_WEEKS = 10     # 10-week (50-day) MA — Weinstein's earliest warning
LONG_MA_WEEKS = 40     # 40-week (200-day) MA — used for the 3A/3B split
VOL_DIR_WEEKS = 13     # window comparing up-week against down-week volume
VOL_BREAKOUT = 1.50    # Weinstein's floor for a valid breakout/breakdown week
EARLY_WEEKS = 10       # a stage younger than this is still in its "A" phase
BASE_MIN_WEEKS = 20    # a base shorter than this is immature (1A, not 1B)
MATURE_PCTILE = 0.60   # late base sits high in its own box → 1B
RS_PEAK_WEEKS = 8      # RS lookback for the Stage-2 "RS fading" early warning
TIGHT_BOX_PCT = 25.0   # Stage-3 box narrower than this reads as consolidation
TT_ABOVE_LOW = 30.0    # Minervini: price must be this % above its 52-week low
TT_BELOW_HIGH = 25.0   # Minervini: and no further than this below its 52w high
VCP_LEGS = 3           # pullbacks inspected for the contraction series
VCP_SHRINK = 0.50      # each leg must be at most this share of the one before
VCP_MIN_SWING = 3.0    # zigzag threshold; smaller moves are noise, not legs
NEAR_HIGH_PCT = 15.0   # "tight, near breakout" for the buy-ready screen
VOL_DRYUP = 0.60       # recent volume vs its own average during a contraction
VOL_SPIKE_MAX = 10.0   # above this a weekly volume ratio is an event, not demand
ATR_CONTRACTION = 0.75  # recent range vs its own average during a contraction
FAST_WIN = 2           # short window for the dry-up and ATR comparisons
SLOW_WIN = 10          # long window for the dry-up and ATR comparisons
# Both cuts sit near the 25th percentile of their own measured distribution
# across Stage 2 names, so each admits a comparable slice instead of one
# silently vetoing the whole screen. 0.60 on volume is also the textbook
# figure. The textbook 0.50 on range is not usable: measured over 38 Stage 2
# names it was cleared by 3% on these weekly windows and by 0% on the daily
# 10/50 windows it is quoted for, because volatility mean-reverts long before
# it halves. The two window pairs were checked to be interchangeable first —
# 2w/10w and 10d/50d volume ratios share a median of 0.71.

# History window. 1200 calendar days ≈ 164 weekly bars: 30 for the MA + 5 for
# its slope + 52 for the Mansfield baseline leaves ~110 weeks of usable,
# fully-warmed history for the heatmap. Angel serves 2000 daily bars per
# request, so this is still a single call per symbol.
LOOKBACK_DAYS = 1200

HEATMAP_WEEKS = 104    # how much of that history the heatmap shows
MIN_SECTOR_STOCKS = 5  # below this a composite is one or two stocks in a trench
MIN_BARS = 200         # daily bars a symbol needs before it is worth staging

BENCHMARK = "^CRSLDX"
BENCHMARK_LABEL = "Nifty 500"

VOL_EXPANSION = 1.50   # weekly volume vs VOL_WEEKS average to count as "heavy"

# A symbol whose history starts more than this long after the requested start
# was probably rate-limited mid-fetch rather than newly listed, so it gets one
# retry. Angel silently returns a short frame under load, and a half-warm
# 30-week MA produces a wrong-but-plausible stage — the worst kind of output.
SHALLOW_TOLERANCE_DAYS = 45

# Minutes past midnight (IST — this workspace runs on Indian market hours) after
# which today's daily bar is a closed session rather than one still forming.
MARKET_CLOSE_MIN = 15 * 60 + 40

STAGE_NAMES = {1: "1 Basing", 2: "2 Advancing", 3: "3 Topping", 4: "4 Declining"}
STAGE_COLORS = {1: "#9e9e9e", 2: "#2e7d32", 3: "#ef6c00", 4: "#c62828"}

# Weinstein's graduated instruction for each half of each stage. The A/B split
# is the difference between "protect" and "sell", and between "buy" and "do not
# add" — the same stage number means two very different things at each end.
SUB_ACTION = {
    "1A": "WAIT — base too young to buy",
    "1B": "WATCH — mature base, breakout close",
    "2A": "BUY — early advance, best entry",
    "2B": "HOLD — late advance, tighten stops",
    "3A": "TRIM HALF — top forming, stop up",
    "3B": "SELL REST INTO ANY RALLY",
    "4A": "EXIT ALL — decline has begun",
    "4B": "AVOID — do not bottom-fish",
}

# Discrete 4-band colourscale for the heatmap (used with zmin=0.5, zmax=4.5).
STAGE_COLORSCALE = [
    [0.00, STAGE_COLORS[1]], [0.25, STAGE_COLORS[1]],
    [0.25, STAGE_COLORS[2]], [0.50, STAGE_COLORS[2]],
    [0.50, STAGE_COLORS[3]], [0.75, STAGE_COLORS[3]],
    [0.75, STAGE_COLORS[4]], [1.00, STAGE_COLORS[4]],
]

# (from, to) -> (priority, plain-English signal). Lower priority sorts first.
TRANSITIONS = {
    (1, 2): (0, "BUY — Stage 2 breakout from base"),
    (4, 2): (1, "BUY (aggressive) — V-reversal, no base"),
    (3, 2): (2, "RE-ENTRY — Stage 2 resumed after a stall"),
    (2, 3): (3, "SELL / TRIM — top forming"),
    (2, 4): (4, "EXIT — collapse, skipped Stage 3"),
    (3, 4): (5, "EXIT / AVOID — decline has begun"),
    (1, 4): (6, "AVOID — base failed, breakdown"),
    (2, 1): (7, "CAUTION — Stage 2 stalled into a range"),
    (4, 1): (8, "WATCH — base starting to form"),
    (3, 1): (9, "NEUTRAL — top resolved sideways"),
    (4, 3): (10, "NEUTRAL — flat MA high in range"),
    (1, 3): (11, "NEUTRAL — reclassified base → top"),
}


# ─── Weekly bars ─────────────────────────────────────────────────────────────

def session_cutoff(last_daily) -> pd.Timestamp:
    """Last daily bar that counts as a *closed* session.

    Today's bar is excluded until the market has closed, otherwise a run
    started mid-session would stage the market off a half-formed candle.
    Assumes the machine clock is IST, which is what this workspace runs on.
    """
    ts = pd.Timestamp(last_daily)
    now = dt.datetime.now()
    if (ts.date() == now.date()
            and now.hour * 60 + now.minute < MARKET_CLOSE_MIN):
        return ts - pd.Timedelta(days=1)
    return ts


def _naive_daily_index(idx) -> pd.DatetimeIndex:
    """Drop any timezone and time-of-day so every provider lands on one grid.

    ``tz_localize(None)`` raises on an already-naive index, and this workspace
    mixes Angel (tz-aware +05:30), jugaad (18:30 UTC) and yfinance (naive).
    """
    out = pd.to_datetime(idx)
    if getattr(out, "tz", None) is not None:
        out = out.tz_localize(None)
    return out.normalize()


def to_weekly(df: pd.DataFrame, last_daily: pd.Timestamp | None = None) -> pd.DataFrame:
    """Resample daily OHLCV to completed W-FRI weekly bars.

    The final bucket is dropped when its Friday label runs past the last daily
    bar we actually have, i.e. the week is still in progress. Passing
    `last_daily` (the market-wide last session) keeps every symbol on the same
    cut-off even when one symbol stopped trading early.
    """
    if df is None or df.empty or "Close" not in df.columns:
        return pd.DataFrame()
    d = df.copy()
    d.index = _naive_daily_index(d.index)
    d = d[~d.index.duplicated(keep="last")].sort_index()
    agg = {c: f for c, f in (("Open", "first"), ("High", "max"), ("Low", "min"),
                             ("Close", "last"), ("Volume", "sum"))
           if c in d.columns}
    w = d.resample("W-FRI").agg(agg).dropna(subset=["Close"])
    if w.empty:
        return w
    cutoff = pd.Timestamp(last_daily) if last_daily is not None else d.index.max()
    return w[w.index <= cutoff]


# ─── Stage engine ────────────────────────────────────────────────────────────

def _confirm(raw: pd.Series, weeks: int = CONFIRM_WEEKS) -> pd.Series:
    """Hold the previous stage until a new one prints `weeks` weeks running.

    The very first classification is adopted immediately (there is nothing to
    hold on to yet); after that every change costs one extra week of delay.
    """
    vals = raw.to_numpy(dtype=float)
    out = np.full(len(vals), np.nan)
    cur, run_val, run_len = np.nan, np.nan, 0
    for i, v in enumerate(vals):
        if not np.isnan(v):
            if v == run_val:
                run_len += 1
            else:
                run_val, run_len = v, 1
            if np.isnan(cur) or run_len >= weeks:
                cur = v
        out[i] = cur
    return pd.Series(out, index=raw.index)


def stage_frame(weekly) -> pd.DataFrame:
    """Classify weekly bars into stages. Returns per-week diagnostics.

    Accepts either a weekly Close series or a full weekly OHLCV frame; volume
    columns come back NaN when no Volume is supplied, and nothing in the 1-4
    classification depends on them.

    Columns: Close, MA (30-week SMA), MA10 (10-week / 50-day), MA40 (40-week /
    200-day), Slope (% over SLOPE_WEEKS), Slope40 (% of MA40 over SLOPE_WEEKS),
    Dist (% of Close above/below the 30W MA), Pctile (0-1 position in the
    52-week range), Persist (share of the last PERSIST_WEEKS closed above the
    MA), BaseAge (weeks since the MA slope last showed a genuine trend — the
    tolerant age of a base or a top), VolDir (mean up-week volume /
    mean down-week volume over VOL_DIR_WEEKS; above 1 = accumulation, below =
    distribution), Raw (unconfirmed stage) and Stage (1-4 after CONFIRM_WEEKS
    hysteresis, NaN until the MA and its slope are both warm).
    Implements the decision table documented in the module docstring.
    """
    if isinstance(weekly, pd.Series):
        weekly = weekly.to_frame("Close")
    c = pd.to_numeric(weekly["Close"], errors="coerce").dropna()
    out = pd.DataFrame(index=c.index)
    out["Close"] = c
    cols = ("MA", "MA10", "MA40", "Slope", "Slope40", "Dist", "Pctile",
            "Hi52", "Lo52", "Persist", "BaseAge", "VolDir", "Raw", "Stage")
    if len(c) < MA_WEEKS + SLOPE_WEEKS:
        for col in cols:
            out[col] = np.nan
        return out

    ma = c.rolling(MA_WEEKS).mean()
    ma10 = c.rolling(FAST_MA_WEEKS).mean()
    ma40 = c.rolling(LONG_MA_WEEKS).mean()
    slope = (ma / ma.shift(SLOPE_WEEKS) - 1.0) * 100.0
    slope40 = (ma40 / ma40.shift(SLOPE_WEEKS) - 1.0) * 100.0
    lo = c.rolling(RANGE_WEEKS, min_periods=13).min()
    hi = c.rolling(RANGE_WEEKS, min_periods=13).max()
    span = (hi - lo).replace(0, np.nan)
    pctile = (c - lo) / span
    prior = (c.shift(FLAT_OFFSET) / c.shift(FLAT_OFFSET + PRIOR_WEEKS) - 1.0) * 100.0

    # "Consistently above" / "consistently below", not just this week's close.
    persist = (c > ma).rolling(PERSIST_WEEKS).mean()
    held_above = persist >= PERSIST_FRAC
    held_below = persist <= 1.0 - PERSIST_FRAC
    rising = slope > FLAT_BAND_PCT
    falling = slope < -FLAT_BAND_PCT

    # Price structure. Weinstein is categorical: no higher lows, no Stage 2.
    swing_lo = c.rolling(STRUCT_WEEKS).min()
    swing_hi = c.rolling(STRUCT_WEEKS).max()
    higher_lows = swing_lo > swing_lo.shift(STRUCT_WEEKS)
    lower_highs = swing_hi < swing_hi.shift(STRUCT_WEEKS)

    # Everything that is not an unambiguous Stage 2 or Stage 4 is a transition
    # zone, resolved below by cycle position first and chart shape second.
    # `cleared` is Weinstein's actual Stage 2 trigger, which a rising MA and a
    # run of closes above it do not imply: price must have broken out of the
    # base. Without it the engine called Advancing 38 times to the human's 25.
    cleared = (pctile >= S2_MIN_PCTILE) if S2_MIN_PCTILE > 0 \
        else pd.Series(True, index=c.index)
    s2 = rising & held_above & higher_lows & cleared
    s4 = falling & held_below & lower_highs
    ambiguous = slope.notna() & persist.notna() & ~s2 & ~s4
    raw = pd.Series(np.nan, index=c.index)
    raw[s2] = 2.0
    raw[s4] = 4.0

    # Cycle memory: the last decisive stage, and how many weeks ago it printed.
    anchor = raw.ffill()
    age = pd.Series(np.arange(len(c)), index=c.index) - pd.Series(
        np.arange(len(c)), index=c.index).where(raw.notna()).ffill()
    fresh = anchor.notna() & (age <= STALE_WEEKS)

    raw[ambiguous & fresh & (anchor == 2.0)] = 3.0
    raw[ambiguous & fresh & (anchor == 4.0)] = 1.0

    # Cold start / stale memory: fall back to where price sits in its range.
    cold = ambiguous & ~fresh
    wide = (span / lo * 100.0) >= MIN_RANGE_PCT
    if cold.any():
        raw[cold] = np.where(
            (wide[cold] & (pctile[cold] >= HIGH_PCTILE))
            | (~(wide[cold] & (pctile[cold] <= LOW_PCTILE)) & (prior[cold] > 0)),
            3.0, 1.0)

    # Distribution happens near the highs. Once price has slid to the floor of
    # its own 52-week range the top is finished — what is left is a base, not a
    # top, however recently the advance ended.
    raw[(raw == 3.0) & wide & (pctile < S3_MIN_PCTILE)] = 1.0

    # A top is distribution *after* an advance. A name the gate below never let
    # into Stage 2 never advanced, so cycle memory calling it a top is wrong —
    # with the MA still rising it is a base that has not broken out yet.
    if S2_MIN_PCTILE > 0:
        raw[(raw == 3.0) & rising & ~cleared] = 1.0

    # The MA is the spine of the method. A base needs a FLAT MA — while the 30W
    # MA is still falling and price is under it the decline is simply not over,
    # whatever the cycle memory says. Mirror rule for a top over a rising MA.
    raw[falling & (c <= ma) & (raw == 1.0)] = 4.0
    raw[rising & (c >= ma) & (raw == 3.0) & cleared] = 2.0
    raw = raw.where(ma.notna() & slope.notna())

    up = c.diff() > 0
    if "Volume" in weekly.columns:
        v = pd.to_numeric(weekly["Volume"], errors="coerce").reindex(c.index)
        uv = v.where(up).rolling(VOL_DIR_WEEKS, min_periods=4).mean()
        dv = v.where(~up).rolling(VOL_DIR_WEEKS, min_periods=4).mean()
        out["VolDir"] = uv / dv.replace(0, np.nan)
    else:
        out["VolDir"] = np.nan

    out["MA"] = ma
    out["MA10"] = ma10
    out["MA40"] = ma40
    out["Slope"] = slope
    out["Slope40"] = slope40
    out["Dist"] = (c / ma - 1.0) * 100.0
    out["Pctile"] = pctile
    out["Hi52"] = hi
    out["Lo52"] = lo
    out["Persist"] = persist
    # How long the MA has gone without a genuine trend. Measured as weeks since
    # the slope last exceeded twice the flat band, so ordinary week-to-week
    # wobble inside a base does not reset the count the way a strict run-length
    # counter does (that version called only 2% of real bases mature).
    strong = (slope.abs() > FLAT_BAND_PCT * TREND_BAND_MULT) & slope.notna()
    out["BaseAge"] = strong.groupby(strong.cumsum()).cumcount()
    out["Raw"] = raw
    out["Stage"] = _confirm(raw)
    return out


def mansfield_rs(close_w: pd.Series, bench_w: pd.Series) -> pd.Series:
    """Mansfield Relative Strength on weekly bars.

    RS = (ratio / RS_WEEKS-week SMA of ratio - 1) * 100, where ratio is
    close/benchmark. Zero means the entity has matched the benchmark over the
    past year; Weinstein only buys Stage 2 when this is above zero and rising.
    This is deliberately *not* ``sector_momentum.compute_rs``, which is
    start-normalised comparative RS with no zero line and no fixed baseline.
    """
    common = close_w.index.intersection(bench_w.index)
    if len(common) < RS_WEEKS + 2:
        return pd.Series(dtype=float)
    ratio = close_w.loc[common] / bench_w.loc[common]
    base = ratio.rolling(RS_WEEKS).mean()
    return ((ratio / base) - 1.0) * 100.0


def weeks_in_stage(stage: pd.Series) -> int:
    """How many consecutive completed weeks the series has held its last stage."""
    s = stage.dropna()
    if s.empty:
        return 0
    last = s.iloc[-1]
    n = 0
    for v in s.values[::-1]:
        if v != last:
            break
        n += 1
    return n


def volume_ratio(weekly: pd.DataFrame) -> float:
    """Latest weekly volume divided by its trailing VOL_WEEKS average."""
    if "Volume" not in weekly.columns or len(weekly) < VOL_WEEKS + 1:
        return np.nan
    v = pd.to_numeric(weekly["Volume"], errors="coerce")
    avg = v.iloc[-(VOL_WEEKS + 1):-1].mean()
    if not avg or not np.isfinite(avg):
        return np.nan
    return float(v.iloc[-1] / avg)


def stage_box(sf: pd.DataFrame, weeks: int) -> tuple:
    """Support and resistance of the box price has traced out in this stage.

    For Stage 1 the high is the breakout level Weinstein waits for; for Stage 3
    the low is the support whose failure starts Stage 4.
    """
    run = sf["Close"].dropna()
    if run.empty:
        return (np.nan, np.nan)
    run = run.iloc[-max(int(weeks), 2):]
    return float(run.min()), float(run.max())


def _box_pos(close: float, box: tuple) -> float:
    """Where `close` sits inside `box`, 0 at the floor and 1 at the ceiling."""
    lo, hi = box
    if pd.isna(lo) or pd.isna(hi) or hi <= lo:
        return np.nan
    return (close - lo) / (hi - lo)


def sub_stage(last: pd.Series, stage: int, weeks: int, box: tuple) -> str:
    """Weinstein's early/late split inside the current stage.

    The A/B halves carry opposite instructions — 2A is the buy and 2B is "stop
    adding", 3A is "trim" and 3B is "sell into any rally" — so the stage number
    on its own is not actionable.

    Base maturity is measured from BaseAge (weeks since the MA last trended),
    not from the confirmed stage label: the MA flattens well before the cycle
    memory lets the stage number change, and judging a base by the label makes
    1B unreachable — measured, it fired on 0 of 34 real bases.
    """
    pos = _box_pos(float(last["Close"]), box)
    slope = float(last["Slope"]) if pd.notna(last["Slope"]) else 0.0
    slope40 = float(last["Slope40"]) if pd.notna(last["Slope40"]) else 0.0
    base_age = int(last["BaseAge"]) if pd.notna(last.get("BaseAge")) else 0
    if stage == 1:
        mature = slope > -FLAT_BAND_PCT / 2 and base_age >= BASE_MIN_WEEKS
        return "1B" if mature and (pd.isna(pos) or pos >= MATURE_PCTILE) else "1A"
    if stage == 2:
        return "2A" if weeks <= EARLY_WEEKS else "2B"
    if stage == 3:
        broke = pd.notna(pos) and pos <= 0.05
        return "3B" if (slope40 <= 0 or broke or weeks > EARLY_WEEKS) else "3A"
    return "4A" if weeks <= EARLY_WEEKS else "4B"


def top_quality(last: pd.Series, rs_now: float, lower_highs: bool,
                box: tuple) -> str:
    """Stage 3 only: a pause inside Stage 2, or a terminal top?

    Six points across five tells. Volume direction carries two of them because
    heavier volume on down weeks than up weeks is the one tell that is not
    circular with price — the other four are all restatements of where price
    sits. Weinstein's rule when it reads UNRESOLVED is to reduce anyway: you
    can re-enter a continuation, you cannot un-lose a Stage 4.
    """
    lo, hi = box
    score = 0
    score += int(pd.notna(rs_now) and rs_now > 0)
    score += 2 * int(pd.notna(last.get("VolDir")) and last["VolDir"] >= 1.0)
    score += int(pd.notna(last.get("MA10")) and last["Close"] > last["MA10"])
    score += int(pd.notna(lo) and lo > 0 and pd.notna(hi)
                 and (hi / lo - 1.0) * 100.0 <= TIGHT_BOX_PCT)
    score += int(not lower_highs)
    if score >= 5:
        return "CONTINUATION likely (%d/6)" % score
    if score <= 1:
        return "TERMINAL top likely (%d/6)" % score
    return "UNRESOLVED (%d/6)" % score


def action_for(sub: str, rs_now: float, rs_rising: bool) -> str:
    """The sub-stage instruction, with the buy calls gated on relative strength.

    Weinstein never buys a breakout that is lagging the market, however clean
    the price pattern looks: a Stage 2 without positive RS is a stock rising
    less than the index it is measured against. The stage alone would otherwise
    print "BUY" on names whose own RS warning is firing in the same row.

    A second gate covers the case where RS is not merely weak but *unknown*.
    Mansfield RS needs RS_WEEKS of history, so a listing that clears MIN_BARS
    but is younger than a year has no RS at all, and the row would otherwise
    give a confident instruction on price action alone. Those rows are demoted
    on the buy-and-hold side (2A is already covered; 2B drops to do-not-add)
    and merely annotated on the sell side, because suppressing an exit for want
    of a confirming indicator is the one failure that costs real money.

    Stages 1A, 4A and 4B need no missing-RS handling: their instructions are
    already wait, exit and avoid.
    """
    base = SUB_ACTION[sub]
    rs_known = bool(pd.notna(rs_now))
    if sub == "2A" and not (rs_known and rs_now > 0):
        return "WAIT — advance not yet confirmed by RS"
    if sub == "1B" and not (rs_rising or (rs_known and rs_now > 0)):
        return "WAIT — base not yet leading the market"
    if not rs_known:
        if sub == "2B":
            return "HOLD, DO NOT ADD — no RS, history under %dw" % RS_WEEKS
        if sub in ("3A", "3B"):
            return base + " (RS unavailable)"
    return base


def trend_template(last: pd.Series, rs_now: float) -> tuple:
    """Minervini's 8-point Trend Template, scored on weekly bars.

    His criteria are quoted in trading days; on a weekly frame the 50/150/200
    day averages are the 10/30/40 week averages already computed here, so no
    daily series is needed. Returns (passed, 8).

    This is a stricter Stage 2 confirmation than the moving average alone: it
    demands the averages be stacked in the right order, the long average
    already rising, and price both well off its low and close to its high.
    """
    c = float(last["Close"])
    ma10, ma30, ma40 = last.get("MA10"), last.get("MA"), last.get("MA40")
    hi52, lo52 = last.get("Hi52"), last.get("Lo52")
    sl40 = last.get("Slope40")
    checks = [
        pd.notna(ma30) and pd.notna(ma40) and c > ma30 and c > ma40,
        pd.notna(ma30) and pd.notna(ma40) and ma30 > ma40,
        pd.notna(sl40) and sl40 > 0,
        pd.notna(ma10) and pd.notna(ma30) and pd.notna(ma40)
        and ma10 > ma30 and ma10 > ma40,
        pd.notna(ma10) and c > ma10,
        pd.notna(lo52) and lo52 > 0 and c >= lo52 * (1 + TT_ABOVE_LOW / 100.0),
        pd.notna(hi52) and hi52 > 0 and c >= hi52 * (1 - TT_BELOW_HIGH / 100.0),
        pd.notna(rs_now) and rs_now > 0,
    ]
    return int(sum(bool(x) for x in checks)), len(checks)


def _pullbacks(close: pd.Series, min_pct: float = VCP_MIN_SWING) -> list:
    """Depths (%) of completed peak-to-trough pullbacks, oldest first.

    A zigzag rather than a rolling min: a contraction series is defined by
    successive swings, and a rolling window cannot tell one swing from two.
    Moves smaller than `min_pct` are treated as noise and never open a leg.
    """
    v = pd.to_numeric(close, errors="coerce").dropna().astype(float)
    if len(v) < 5:
        return []
    piv = [float(v.iloc[0])]
    direction = 0
    for x in v.iloc[1:]:
        x, last = float(x), piv[-1]
        if direction == 0:
            # Seed by appending, never by overwriting: the opening bar is an
            # extreme in its own right, and moving it swallows the first leg.
            if x >= last * (1 + min_pct / 100.0):
                piv.append(x)
                direction = 1
            elif x <= last * (1 - min_pct / 100.0):
                piv.append(x)
                direction = -1
        elif direction == 1:
            if x > last:
                piv[-1] = x
            elif x <= last * (1 - min_pct / 100.0):
                piv.append(x)
                direction = -1
        else:
            if x < last:
                piv[-1] = x
            elif x >= last * (1 + min_pct / 100.0):
                piv.append(x)
                direction = 1
    return [round((piv[i] - piv[i + 1]) / piv[i] * 100.0, 1)
            for i in range(len(piv) - 1) if piv[i] > piv[i + 1] and piv[i] > 0]


def vcp_state(close: pd.Series) -> tuple:
    """Volatility Contraction Pattern: are the recent pullbacks shrinking?

    Returns (legs, ok). `legs` is the last VCP_LEGS pullback depths oldest
    first; `ok` is True when each is strictly shallower than the one before it
    and the final leg is at most VCP_SHRINK of its predecessor. Supply drying
    up leg by leg is the setup Minervini buys; the depths are returned so the
    contraction can be seen rather than trusted.
    """
    legs = _pullbacks(close)[-VCP_LEGS:]
    if len(legs) < VCP_LEGS:
        return legs, False
    shrinking = all(legs[i + 1] < legs[i] for i in range(len(legs) - 1))
    tight = legs[-1] <= legs[-2] * VCP_SHRINK
    return legs, bool(shrinking and tight)


def _win_ratio(s: pd.Series, fast: int = FAST_WIN, slow: int = SLOW_WIN) -> float:
    """Mean of the last `fast` values over the mean of the last `slow`."""
    s = pd.to_numeric(s, errors="coerce").dropna()
    if len(s) < slow:
        return np.nan
    lo = float(s.iloc[-slow:].mean())
    if not lo or not np.isfinite(lo):
        return np.nan
    return float(s.iloc[-fast:].mean()) / lo


def weekly_atr(weekly: pd.DataFrame) -> pd.Series:
    """True range per week, the input to the ATR-contraction test."""
    if not {"High", "Low"}.issubset(weekly.columns):
        return pd.Series(dtype=float)
    h = pd.to_numeric(weekly["High"], errors="coerce")
    lo = pd.to_numeric(weekly["Low"], errors="coerce")
    pc = pd.to_numeric(weekly["Close"], errors="coerce").shift()
    return pd.concat([h - lo, (h - pc).abs(), (lo - pc).abs()], axis=1).max(axis=1)


def coiled_flag(weekly: pd.DataFrame, last: pd.Series, stage: int) -> bool:
    """Is this Stage 2 stock coiling — pressed against its high, gone quiet?

    Two of the four buy-ready tells, kept deliberately separate from the other
    two because these are the pair that held up best in forward testing. Over
    847 symbols the pair outranked a volume *surge* — what the old grade A
    demanded — at both 13 and 26 weeks: quiet near the high beats loud after
    the move. That is a ranking, not a measured edge; see `_setup_grade` for
    why the underlying alpha does not survive counting each stock once.

    VCP and the range contraction are left out on purpose: they need the full
    price history and a true-range series, and adding them shrank the sample
    without improving the result.
    """
    if stage != 2 or "Volume" not in weekly.columns:
        return False
    hi52, c = last.get("Hi52"), last.get("Close")
    if pd.isna(hi52) or pd.isna(c) or hi52 <= 0:
        return False
    if float(c) < float(hi52) * (1 - NEAR_HIGH_PCT / 100.0):
        return False
    vol_r = _win_ratio(weekly["Volume"])
    return bool(pd.notna(vol_r) and vol_r < VOL_DRYUP)


def buy_ready(weekly: pd.DataFrame, last: pd.Series, stage: int) -> str:
    """Stage 2 only: is this a tight, contracted setup at the pivot?

    Four tells, all of which Minervini requires together — a contraction
    series, volume drying up, range drying up, and price already pressed
    against its high. Reported as a count with the failing tells named, so a
    3-of-4 near-miss is visible instead of being silently discarded.
    """
    if stage != 2:
        return ""
    legs, vcp_ok = vcp_state(weekly["Close"])
    vol_r = (_win_ratio(weekly["Volume"]) if "Volume" in weekly.columns
             else np.nan)
    atr = weekly_atr(weekly)
    atr_r = _win_ratio(atr) if len(atr) else np.nan
    hi52, c = last.get("Hi52"), float(last["Close"])
    near = (pd.notna(hi52) and hi52 > 0
            and c >= hi52 * (1 - NEAR_HIGH_PCT / 100.0))
    tells = {
        "VCP": vcp_ok,
        "vol dry-up": pd.notna(vol_r) and vol_r < VOL_DRYUP,
        "range dry-up": pd.notna(atr_r) and atr_r < ATR_CONTRACTION,
        "near high": near,
    }
    n = sum(tells.values())
    detail = "%s legs %s" % (len(legs), "→".join(str(x) for x in legs)) if legs else "no legs"
    if n == len(tells):
        return "BUY READY (4/4) — %s" % detail
    missing = ", ".join(k for k, v in tells.items() if not v)
    return "%d/4 — missing %s" % (n, missing)


def stop_level(sf: pd.DataFrame, stage: int, box: tuple) -> float:
    """Weinstein's structural stop — never an arbitrary percentage.

    Stage 2 trails below both the most recent reaction low and the 30W MA;
    Stage 1 sits below the floor of the base. Nothing is quoted for Stage 3/4
    because the instruction there is to be out, not to place a stop.
    """
    last = sf.iloc[-1]
    if stage == 2:
        lows = sf["Close"].dropna().iloc[-FAST_MA_WEEKS:]
        if lows.empty or pd.isna(last["MA"]):
            return np.nan
        return round(float(min(float(lows.min()), float(last["MA"]))) * 0.99, 2)
    if stage == 1 and pd.notna(box[0]):
        return round(box[0] * 0.99, 2)
    return np.nan


def stage_warnings(last: pd.Series, stage: int, vol: float,
                   rs_fading: bool, lower_highs: bool,
                   rs_sell: bool = False, rs_missing: bool = False) -> str:
    """The early-warning tells that fire before the stage number moves.

    Ordered by how early Weinstein said each one appears, so the first item in
    the string is always the leading indicator. The exception is the missing-RS
    flag, which leads because it is a statement about the evidence rather than
    about the stock: with no RS, every RS-derived field in the same row — the
    fading and sell tells, the setup grade, the eighth trend-template check —
    is silently absent rather than negative.
    """
    w = []
    if rs_missing:
        w.append("NO RS — history under %dw" % RS_WEEKS)
    if pd.notna(vol) and vol >= VOL_SPIKE_MAX:
        w.append("VOLUME SPIKE %.0fx — check for a block deal" % vol)
    if stage == 2 and rs_fading:
        w.append("RS FADING")
    if rs_sell:
        w.append("RS SELL — below zero and falling")
    if pd.notna(last.get("VolDir")) and last["VolDir"] < 1.0:
        w.append("DISTRIBUTION")
    if (pd.notna(last.get("MA10")) and last["Close"] < last["MA10"]
            and pd.notna(vol) and vol >= VOL_EXPANSION):
        w.append("50D BREAK ON VOLUME")
    if lower_highs and stage in (2, 3):
        w.append("LOWER HIGHS")
    if pd.notna(last.get("Slope40")) and last["Slope40"] < 0 and stage in (2, 3):
        w.append("200D ROLLING OVER")
    return "; ".join(w)


# ─── Data assembly ───────────────────────────────────────────────────────────

def _fetch_daily(symbols: list, start, end, verbose: bool = True) -> dict:
    """Pull daily OHLCV for `symbols`. Angel bulk first, per-symbol chain after.

    Returns {symbol: DataFrame}. Symbols with fewer than MIN_BARS usable bars
    are dropped — a 30-week MA plus its slope needs ~175 sessions before it
    says anything, and a half-warm MA is worse than no answer. Symbols that
    came back materially shorter than the requested window get one retry
    (see SHALLOW_TOLERANCE_DAYS).
    """
    out = {}
    angel_many = None
    try:
        if data_provider._angel_available():
            from angel_client import angel_download_many
            angel_many = angel_download_many
            out = angel_many(symbols, start, end)
    except Exception as e:
        if verbose:
            print("  [stage] Angel bulk fetch unavailable (%s); using fallback chain" % e)

    if angel_many is not None:
        floor = pd.Timestamp(start) + pd.Timedelta(days=SHALLOW_TOLERANCE_DAYS)
        shallow = [s for s, df in out.items()
                   if df is not None and not df.empty
                   and pd.Timestamp(df.index.min()) > floor]
        if shallow:
            if verbose:
                print("  [stage] retrying %d symbol(s) with short history" % len(shallow))
            for sym, df in angel_many(shallow, start, end).items():
                if df is not None and not df.empty and len(df) > len(out.get(sym, df)):
                    out[sym] = df

    missing = [s for s in symbols if s not in out or out[s] is None or out[s].empty]
    if missing and verbose:
        print("  [stage] fallback chain for %d symbol(s)" % len(missing))
    for i, sym in enumerate(missing, 1):
        if verbose and i % 50 == 0:
            print("    fallback %d/%d" % (i, len(missing)))
        try:
            df = data_provider.download(sym, start=start, end=end,
                                        interval="1d", progress=False)
        except Exception:
            continue
        if df is not None and not df.empty:
            out[sym] = df

    clean = {}
    for sym, df in out.items():
        if df is None or df.empty or "Close" not in df.columns:
            continue
        d = df.copy()
        d.index = _naive_daily_index(d.index)
        d = d[~d.index.duplicated(keep="last")].sort_index()
        d["Close"] = pd.to_numeric(d["Close"], errors="coerce")
        d = d[d["Close"] > 0]
        if len(d) >= MIN_BARS:
            clean[sym] = d
    return clean


def _composite(daily: dict, symbols: list) -> pd.DataFrame:
    """Equal-weight Close + average-per-constituent Volume for one sector."""
    members = [s for s in symbols if s in daily]
    if len(members) < MIN_SECTOR_STOCKS:
        return pd.DataFrame()

    px = pd.DataFrame({s: daily[s]["Close"] for s in members}).sort_index()
    # Drop pseudo-sessions (holiday rows leaked in by a single provider) before
    # they reach the index engine.
    px = px[px.notna().sum(axis=1) > len(members) * 0.5]
    px = px.dropna(axis=1, how="all")
    if px.empty or px.shape[1] < MIN_SECTOR_STOCKS:
        return pd.DataFrame()

    # Not forward-filled here: the engine needs NaN outside a stock's listed
    # life so it can tell "not yet listed" from "listed and unchanged".
    idx = calculate_equal_weight_index(px, base_value=1000.0)
    if idx.empty:
        return pd.DataFrame()
    px = px.reindex(idx.index)

    vol = pd.DataFrame({s: daily[s].get("Volume") for s in members}).reindex(px.index)
    vol = vol.apply(pd.to_numeric, errors="coerce")
    n_reporting = vol.notna().sum(axis=1).replace(0, np.nan)
    avg_vol = vol.sum(axis=1) / n_reporting

    return pd.DataFrame({"Close": idx, "Volume": avg_vol}).dropna(subset=["Close"])


def _profile(name: str, weekly: pd.DataFrame, bench_w: pd.Series,
             extra: dict | None = None) -> dict | None:
    """Collapse one entity's weekly history into a single current-state row."""
    sf = stage_frame(weekly)
    stages = sf["Stage"].dropna()
    if stages.empty:
        return None
    last = sf.loc[stages.index[-1]]
    stage = int(stages.iloc[-1])
    prev = int(stages.iloc[-2]) if len(stages) > 1 else stage
    weeks = weeks_in_stage(stages)

    rs = mansfield_rs(sf["Close"], bench_w)
    rs_v = rs.dropna()
    rs_now = float(rs_v.iloc[-1]) if len(rs_v) else np.nan
    rs_prev = float(rs_v.iloc[-5]) if len(rs_v) >= 5 else np.nan
    rs_rising = bool(pd.notna(rs_now) and pd.notna(rs_prev) and rs_now > rs_prev)
    # Peak excludes the current week, else this is true unless RS is at a new
    # high and the warning fires on almost every advance.
    rs_peak = (float(rs_v.iloc[-RS_PEAK_WEEKS:-1].max())
               if len(rs_v) > RS_PEAK_WEEKS else np.nan)
    rs_fading = bool(not rs_rising and pd.notna(rs_now)
                     and pd.notna(rs_peak) and rs_now < rs_peak)
    # Weinstein's RS exit: not merely negative, but negative and still sliding.
    rs_sell = bool(len(rs_v) >= 3 and pd.notna(rs_now) and rs_now < 0
                   and rs_v.iloc[-1] < rs_v.iloc[-2] < rs_v.iloc[-3])

    swing = sf["Close"].dropna()
    lower_highs = bool(
        len(swing) >= 2 * STRUCT_WEEKS
        and swing.iloc[-STRUCT_WEEKS:].max()
        < swing.iloc[-2 * STRUCT_WEEKS:-STRUCT_WEEKS].max())

    # A base or a top is the whole sideways formation, which is usually older
    # than the stage label; an advance or decline is only as old as the stage.
    base_age = int(last["BaseAge"]) if pd.notna(last.get("BaseAge")) else 0
    box = stage_box(sf, max(weeks, base_age) if stage in (1, 3) else weeks)
    sub = sub_stage(last, stage, weeks, box)
    vol = volume_ratio(weekly)
    close = float(last["Close"])
    tt, tt_max = trend_template(last, rs_now)
    legs, vcp_ok = vcp_state(sf["Close"])

    row = {
        "Name": name,
        "Stage": stage,
        "Stage Name": STAGE_NAMES[stage],
        "Sub": sub,
        "Action": action_for(sub, rs_now, rs_rising),
        "Weeks in Stage": weeks,
        "Base Weeks": base_age,
        "Prev Stage": prev,
        "Changed": stage != prev,
        "Close": round(close, 2),
        "30W MA": round(float(last["MA"]), 2),
        "Dist from MA %": round(float(last["Dist"]), 2),
        "MA Slope %": round(float(last["Slope"]), 2),
        "52w Range %ile": (round(float(last["Pctile"]) * 100, 1)
                           if pd.notna(last["Pctile"]) else np.nan),
        "Above MA %wks": (round(float(last["Persist"]) * 100, 0)
                          if pd.notna(last["Persist"]) else np.nan),
        "Mansfield RS": round(rs_now, 2) if pd.notna(rs_now) else np.nan,
        "RS Rising": rs_rising,
        "Vol vs 10w": round(vol, 2) if pd.notna(vol) else np.nan,
        "Vol Direction": (round(float(last["VolDir"]), 2)
                          if pd.notna(last.get("VolDir")) else np.nan),
        "Box Low": round(box[0], 2) if pd.notna(box[0]) else np.nan,
        "Box High": round(box[1], 2) if pd.notna(box[1]) else np.nan,
        "% to Breakout": (round((box[1] / close - 1.0) * 100, 2)
                          if stage == 1 and pd.notna(box[1]) and close else np.nan),
        "Stop": stop_level(sf, stage, box),
        "Trend Template": tt,
        "TT Max": tt_max,
        "VCP": ("→".join(str(x) for x in legs) + (" ✓" if vcp_ok else "")
                if legs else ""),
        "Buy Ready": buy_ready(weekly, last, stage),
        "_coiled": coiled_flag(weekly, last, stage),
        "Top Quality": (top_quality(last, rs_now, lower_highs, box)
                        if stage == 3 else ""),
        "Warnings": stage_warnings(last, stage, vol, rs_fading, lower_highs,
                                   rs_sell, rs_missing=bool(pd.isna(rs_now))),
    }
    if extra:
        row.update(extra)
    row["_stages"] = sf["Stage"]
    return row


def _setup_grade(row: dict) -> str:
    """Weinstein's buy checklist condensed to one letter.

    A = Stage 2 that passes all eight Minervini Trend Template checks *and* is
    coiling — pressed against its 52-week high on volume that has gone quiet.
    B = Stage 2 with RS above zero. C = Stage 2 but RS below zero, a laggard
    advance. "" = not a candidate.

    A used to require the opposite of coiling: a 1.5x volume breakout. Coiling
    replaced it because it ranks better on every forward test run so far, at
    both horizons and under both weightings — but read the next paragraph
    before treating that as an edge.

    TREAT A AS A SCREEN, NOT A PROVEN EDGE. Measured per signal against an
    equal-weighted draw from the same universe, coiled A earns +1.79% over 13
    weeks and +1.99% over 26, against +1.05% and +0.75% for the breakout
    version. Those signals are not independent: 2,627 of them come from only
    579 distinct stocks, because one name in a long run fires for several
    consecutive weeks. Collapse to one observation per stock and the sign
    reverses — coiled A scores -1.63% and -2.78%, breakout A -2.65% and -4.16%,
    plain Stage 2 -1.79% and -2.65%, against a random-draw control of +0.04%
    and +0.07%. On that view no grade in this module beats picking at random,
    and the ranking between grades is all that survives. The effective sample
    is ~579 bets over 3.3 years, which is too few to call the edge real.

    RS Rising is no longer tested here because the eighth trend-template check
    already requires RS above zero, and adding the slope on top shrank the
    sample without improving the forward result.

    A distance ceiling above the 30-week MA was tested and rejected. Grade A
    runs extended — median 23%, up to 176% above the MA — but forward alpha by
    distance bucket is not monotone: the 40-60% band is the weakest while the
    60%+ band is among the strongest at 26 weeks. Capping only helps by cutting
    one middle band, which is noise, not a rule.
    """
    if row.get("Stage") != 2:
        return ""
    rs = row.get("Mansfield RS")
    if pd.isna(rs) or rs <= 0:
        return "C"
    tt = row.get("Trend Template", 0)
    tt_max = row.get("TT Max", 8)
    if tt >= tt_max and row.get("_coiled"):
        return "A"
    return "B"


# ─── Sheets ──────────────────────────────────────────────────────────────────

_SECTOR_COLS = ["Rank", "Sector", "Stage Name", "Sub", "Action",
                "Weeks in Stage", "Base Weeks", "Mansfield RS", "RS Rising",
                "Close", "30W MA", "Dist from MA %", "MA Slope %",
                "52w Range %ile", "Above MA %wks", "Vol vs 10w",
                "Vol Direction", "Box Low", "Box High", "% to Breakout",
                "Stop", "Trend Template", "VCP", "Buy Ready", "Top Quality",
                "Warnings", "Members", "Changed"]

_STOCK_COLS = ["Symbol", "Sector", "Sector Rank", "Setup", "Stage Name", "Sub",
               "Action", "Weeks in Stage", "Base Weeks", "Mansfield RS",
               "RS Rising", "Close", "30W MA", "Dist from MA %", "MA Slope %",
               "52w Range %ile", "Above MA %wks", "Vol vs 10w",
               "Vol Direction", "Box Low", "Box High", "% to Breakout",
               "Stop", "Trend Template", "VCP", "Buy Ready", "Top Quality",
               "Warnings", "Changed"]

_TRANSITION_COLS = ["Type", "Name", "Sector", "From", "To", "Signal", "Sub",
                    "Vol Confirmed", "Weeks in Prev Stage", "Close",
                    "Dist from MA %", "Mansfield RS", "Vol vs 10w",
                    "Vol Direction", "Warnings"]


def build_sector_sheet(sectors: list) -> pd.DataFrame:
    """Sector stage table, ranked: Stage 2 first, then Mansfield RS descending."""
    if not sectors:
        return pd.DataFrame(columns=_SECTOR_COLS)
    df = pd.DataFrame(sectors).rename(columns={"Name": "Sector"})
    df["_stage_order"] = df["Stage"].map({2: 0, 1: 1, 3: 2, 4: 3})
    df = df.sort_values(["_stage_order", "Mansfield RS"],
                        ascending=[True, False], na_position="last")
    df.insert(0, "Rank", range(1, len(df) + 1))
    return df.reindex(columns=_SECTOR_COLS)


def build_stock_sheet(stocks: list, sector_rank: dict,
                      focus_sectors: list) -> pd.DataFrame:
    """Stock stage table restricted to the sectors we are allowed to shop in.

    Weinstein buys the strongest stock in the strongest sector, so this is
    filtered to Stage-2 sectors. When nothing is in Stage 2 the market has no
    leadership at all, so it degrades to the three best-RS sectors rather than
    returning an empty sheet.

    A stock can sit in several sectors (one NSE industry plus one custom
    sector), so it qualifies if *any* of them is in focus and is then reported
    under its best-ranked qualifying sector.
    """
    if not stocks:
        return pd.DataFrame(columns=_STOCK_COLS)
    df = pd.DataFrame(stocks).rename(columns={"Name": "Symbol"})
    focus = set(focus_sectors)

    def _best(row) -> str | None:
        cands = [s for s in (row.get("_sectors") or []) if s in focus]
        if not cands:
            return None
        return min(cands, key=lambda s: sector_rank.get(s, 10 ** 6))

    df["Sector"] = [_best(r) for r in df.to_dict("records")]
    df = df[df["Sector"].notna()]
    if df.empty:
        return pd.DataFrame(columns=_STOCK_COLS)
    df["Setup"] = [_setup_grade(r) for r in df.to_dict("records")]
    df["Sector Rank"] = df["Sector"].map(sector_rank)
    df["_stage_order"] = df["Stage"].map({2: 0, 1: 1, 3: 2, 4: 3})
    df = df.sort_values(["Sector Rank", "_stage_order", "Mansfield RS"],
                        ascending=[True, True, False], na_position="last")
    return df.reindex(columns=_STOCK_COLS)


def build_transition_sheet(sectors: list, stocks: list) -> pd.DataFrame:
    """Every sector and stock whose stage changed at the latest completed week.

    Sectors are listed before stocks and both are ordered by signal importance
    (new Stage 2 entries first, exits next, cosmetic reclassifications last),
    because this sheet is meant to be actioned top-down.

    ``Vol Confirmed`` applies Weinstein's VOL_BREAKOUT floor to the breakout and
    breakdown transitions. A 1→2 or 3→4 move without it is the classic fakeout
    that returns into the range within two weeks, so the flag is reported
    rather than used to suppress the row — the move still happened, it just has
    no institutional sponsorship behind it.
    """
    rows = []
    for kind, items in (("Sector", sectors), ("Stock", stocks)):
        for r in items:
            if not r.get("Changed"):
                continue
            key = (int(r["Prev Stage"]), int(r["Stage"]))
            prio, signal = TRANSITIONS.get(key, (99, "%d → %d" % key))
            stages = r["_stages"].dropna()
            prev_run = weeks_in_stage(stages.iloc[:-1]) if len(stages) > 1 else 0
            vol = r["Vol vs 10w"]
            if key in ((1, 2), (4, 2), (3, 2), (2, 4), (3, 4), (1, 4)):
                confirmed = ("YES" if pd.notna(vol) and vol >= VOL_BREAKOUT
                             else "NO — under %.1fx" % VOL_BREAKOUT)
            else:
                confirmed = ""
            rows.append({
                "_prio": (0 if kind == "Sector" else 1, prio),
                "Type": kind,
                "Name": r["Name"],
                "Sector": r["Name"] if kind == "Sector" else r.get("Sector", ""),
                "From": STAGE_NAMES[key[0]],
                "To": STAGE_NAMES[key[1]],
                "Signal": signal,
                "Sub": r.get("Sub", ""),
                "Vol Confirmed": confirmed,
                "Weeks in Prev Stage": prev_run,
                "Close": r["Close"],
                "Dist from MA %": r["Dist from MA %"],
                "Mansfield RS": r["Mansfield RS"],
                "Vol vs 10w": vol,
                "Vol Direction": r.get("Vol Direction", np.nan),
                "Warnings": r.get("Warnings", ""),
            })
    if not rows:
        return pd.DataFrame([{"Note": "No stage changes at the latest weekly close"}])
    df = pd.DataFrame(rows).sort_values("_prio")
    return df.reindex(columns=_TRANSITION_COLS)


# ─── Charts ──────────────────────────────────────────────────────────────────

def _stage_matrix(sectors: list, weeks: int = HEATMAP_WEEKS) -> pd.DataFrame:
    """Sector x week stage grid, rows already in report rank order."""
    series = {r["Name"]: r["_stages"] for r in sectors}
    if not series:
        return pd.DataFrame()
    mat = pd.DataFrame(series).sort_index()
    mat = mat.dropna(how="all")
    return mat.iloc[-weeks:]


def _heatmap_figure(mat: pd.DataFrame) -> go.Figure:
    """Stage heatmap: one row per sector, one column per completed week."""
    z = mat.T
    text = z.apply(lambda col: col.map(
        lambda v: STAGE_NAMES.get(int(v), "") if pd.notna(v) else ""))
    fig = go.Figure(go.Heatmap(
        z=z.values,
        x=[d.strftime("%d-%b-%y") for d in z.columns],
        y=list(z.index),
        customdata=text.values,
        colorscale=STAGE_COLORSCALE, zmin=0.5, zmax=4.5,
        xgap=0.5, ygap=1.5,
        colorbar=dict(tickvals=[1, 2, 3, 4],
                      ticktext=[STAGE_NAMES[s] for s in (1, 2, 3, 4)],
                      title="Stage", thickness=14),
        hovertemplate="<b>%{y}</b><br>%{x}<br>%{customdata}<extra></extra>",
    ))
    fig.update_layout(
        template="plotly_white",
        height=max(420, 26 * len(z.index) + 150),
        margin=dict(l=210, r=20, t=40, b=60),
        title=dict(text="Sector stage by week (rows ranked by current standing)",
                   x=0.5, xanchor="center", font=dict(size=13)),
    )
    fig.update_xaxes(nticks=26, tickangle=-45, tickfont=dict(size=9))
    fig.update_yaxes(tickfont=dict(size=10), autorange="reversed")
    return fig


def _mix_figure(mat: pd.DataFrame) -> go.Figure:
    """Share of sectors in each stage over time — a one-glance market gauge."""
    counts = {}
    for stage in (2, 1, 3, 4):
        counts[stage] = (mat == stage).sum(axis=1)
    total = mat.notna().sum(axis=1).replace(0, np.nan)

    fig = go.Figure()
    for stage in (2, 1, 3, 4):
        fig.add_trace(go.Scatter(
            x=mat.index, y=(counts[stage] / total * 100).round(1),
            name=STAGE_NAMES[stage], mode="lines", stackgroup="one",
            line=dict(width=0.5, color=STAGE_COLORS[stage]),
            fillcolor=STAGE_COLORS[stage],
            hovertemplate="%{x|%d-%b-%y}<br>" + STAGE_NAMES[stage]
                          + ": %{y:.0f}%<extra></extra>",
        ))
    fig.update_layout(
        template="plotly_white", height=520, hovermode="x unified",
        margin=dict(l=55, r=20, t=45, b=40),
        yaxis=dict(title="% of sectors", range=[0, 100]),
        title=dict(text="Market stage mix — how much of the market is advancing",
                   x=0.5, xanchor="center", font=dict(size=13)),
        legend=dict(orientation="h", y=-0.12),
    )
    return fig


def _rs_figure(sectors: list, weeks: int = HEATMAP_WEEKS) -> go.Figure:
    """Mansfield RS small-multiples: one panel per sector, zero = benchmark.

    A single overlay of 58 lines is unreadable — the lines that matter are
    buried and the legend is longer than the plot. Each sector now gets its
    own axes, ranked left-to-right by current RS, tinted by its Weinstein
    stage, and sharing one x-window so panels can be scanned across rows.

    Geometry is pixel-derived: Plotly's ``vertical_spacing`` is a share of the
    whole plotting area, so any fixed fraction collapses the panels once the
    grid is more than a few rows deep.
    """
    ordered = [r for r in sorted(
        sectors,
        key=lambda r: -(r["Mansfield RS"] if pd.notna(r["Mansfield RS"]) else -1e9))
        if r.get("_rs") is not None and not r["_rs"].dropna().empty]

    if not ordered:
        return go.Figure()

    cols = 3
    rows = max(1, int(math.ceil(len(ordered) / cols)))
    panel_h, row_gap, margin_t, margin_b = 200, 58, 70, 50
    plot_h = rows * panel_h + (rows - 1) * row_gap

    titles = ["%s (%.1f)" % (r["Name"], r["Mansfield RS"])
              if pd.notna(r["Mansfield RS"]) else r["Name"] for r in ordered]

    fig = make_subplots(
        rows=rows, cols=cols, subplot_titles=titles,
        vertical_spacing=(row_gap / plot_h) if rows > 1 else 0.0,
        horizontal_spacing=0.055,
    )

    x_lo, x_hi = None, None
    for i, r in enumerate(ordered):
        row, col = divmod(i, cols)
        row, col = row + 1, col + 1
        rs = r["_rs"].dropna().iloc[-weeks:]
        x_lo = rs.index.min() if x_lo is None else min(x_lo, rs.index.min())
        x_hi = rs.index.max() if x_hi is None else max(x_hi, rs.index.max())
        fig.add_trace(go.Scatter(
            x=rs.index, y=rs.round(2), name=r["Name"], mode="lines",
            showlegend=False,
            line=dict(width=1.8, color=STAGE_COLORS[r["Stage"]]),
            hovertemplate="<b>" + r["Name"] + "</b><br>%{x|%d-%b-%y}"
                          "<br>RS: %{y:.2f}<extra></extra>",
        ), row=row, col=col)
        fig.add_hline(y=0, line=dict(color="#424242", width=1, dash="dash"),
                      row=row, col=col)
        if col == 1:
            fig.update_yaxes(title_text="Mansfield RS",
                             title_font=dict(size=9), row=row, col=col)

    if x_lo is not None:
        fig.update_xaxes(range=[x_lo, x_hi])
    fig.update_xaxes(tickfont=dict(size=8), nticks=4, tickangle=0,
                     tickformat="%b %y", showgrid=True, gridcolor="#eeeeee")
    fig.update_yaxes(tickfont=dict(size=8), nticks=5,
                     showgrid=True, gridcolor="#eeeeee")
    fig.update_annotations(font=dict(size=11))
    fig.update_layout(
        template="plotly_white",
        height=plot_h + margin_t + margin_b,
        hovermode="closest",
        margin=dict(l=60, r=20, t=margin_t, b=margin_b),
        showlegend=False,
        title=dict(text="Sector Mansfield RS vs %s — above 0 = outperforming "
                        "over 52 weeks (panels ranked by current RS, coloured "
                        "by stage)" % BENCHMARK_LABEL,
                   x=0.5, xanchor="center", y=1.0, yanchor="top",
                   pad=dict(t=12), font=dict(size=13)),
    )
    return fig


_REFERENCE_HTML = """
<h3>The three-step workflow</h3>
<table class="rt">
 <tr><th>Step</th><th>Excel sheet</th><th>Chart tab</th></tr>
 <tr><td>1. Shortlist</td><td><b>Sector RS Ranking</b></td><td>&ldquo;Sector Momentum&rdquo; / &ldquo;NSE Sector RS&rdquo;</td></tr>
 <tr><td>2. Validate</td><td><b>Stage Sectors</b></td><td>&ldquo;Stage Analysis&rdquo; &rarr; sub-tab &ldquo;Sector Mansfield RS&rdquo;</td></tr>
 <tr><td>3. Pick stocks</td><td><b>Stage Stocks</b></td><td>&mdash;</td></tr>
</table>

<h3>Step 1 &mdash; shortlisting on the &ldquo;Sector RS Ranking&rdquo; sheet</h3>
<p>The sheet is built by <code>run_all.py:321-360</code>. Each row is one sector, and the
columns you need are:</p>
<p><code>Source</code> &middot; <code>Sector</code> &middot; <code>Description</code>/<code>Index</code>
&middot; <b><code>RS Week 0</code></b> &middot; <b><code>RS Week 1</code></b>
&middot; <b><code>RS Week 2</code></b> &middot; <b><code>RS Week 3</code></b>
&middot; <b><code>RS Week 4</code></b> &middot; <code>10D Trend</code> &middot; <code>20D Trend</code>
&middot; <code>RS Status</code> &middot; <code>Change %</code></p>
<p><code>RS Week 0</code> = today. <code>RS Week 4</code> = five weeks ago. The sheet arrives
<b>sorted by <code>RS Week 0</code> descending</b>.</p>

<h4>Do NOT just take the top of the sheet</h4>
<p>That default sort is a trap. <code>RS Week 0</code> is measured from an arbitrary window
start, so it is a <b>record of the past</b>, not a statement about now. Worse, the custom
baskets each start on the first date their constituents all have data, so two rows in the
<code>Custom Sector</code> block may not even share a baseline.</p>

<h4>The actual rule: use the 5-week delta</h4>
<p class="eq">&Delta;<sub>5w</sub> = <code>RS Week 0</code> &minus; <code>RS Week 4</code></p>
<p><b>The arbitrary baseline cancels in the subtraction.</b> That single arithmetic step makes
the number window-independent and genuinely comparable across every row on the sheet. This is
the column you sort on.</p>

<h4>Concrete filter</h4>
<p>Keep a sector only if <b>all three</b> hold:</p>
<ol>
 <li><code>RS Week 0</code> <b>&gt; 0</b> &mdash; it is ahead of Nifty 500, not behind it</li>
 <li><b>&Delta;<sub>5w</sub> &gt; 0</b> &mdash; the lead is <i>still widening</i></li>
 <li><code>20D Trend</code> shows <b>&uarr;</b> &mdash; independent 20-day confirmation</li>
</ol>
<p>Then sort the survivors by &Delta;<sub>5w</sub> descending and take the <b>top 4&ndash;6</b>.</p>
<p><code>10D Trend</code> is a tie-breaker, not a filter. It is the same calculation over a
shorter window, so it turns first. <code>10D</code> and <code>20D</code> both <b>&uarr;</b>
means the lead is widening and still accelerating &mdash; prefer these. <code>10D</code>
<b>&darr;</b> while <code>20D</code> is <b>&uarr;</b> means the move is losing steam and the
20-day reading is running on old strength; keep the sector but wait for a better entry.
The reverse (<code>10D</code> <b>&uarr;</b>, <code>20D</code> <b>&darr;</b>) is an early turn
&mdash; too soon to act on alone, but worth a watchlist slot.</p>

<h4>Reading it &mdash; illustrative numbers</h4>
<table class="rt">
 <tr><th>Sector</th><th>Wk4</th><th>Wk3</th><th>Wk2</th><th>Wk1</th><th>Wk0</th><th>&Delta;5w</th><th>Verdict</th></tr>
 <tr><td>Nifty Capital Goods</td><td>+2.1</td><td>+3.4</td><td>+5.0</td><td>+6.2</td><td><b>+8.1</b></td><td><b>+6.0</b></td>
     <td class="ok">Shortlist &mdash; modest level, steepest climb</td></tr>
 <tr><td>Nifty Pharma</td><td>+19.4</td><td>+19.1</td><td>+18.8</td><td>+18.5</td><td><b>+18.2</b></td><td><b>&minus;1.2</b></td>
     <td class="no"><b>Reject.</b> Sits at the top of the default sort, but it earned that months ago and is now bleeding</td></tr>
 <tr><td>Nifty PSU Bank</td><td>&minus;4.0</td><td>&minus;3.1</td><td>&minus;1.9</td><td>&minus;0.8</td><td><b>+0.4</b></td><td><b>+4.4</b></td>
     <td class="warn">Just crossed zero &mdash; watchlist, see Step 2</td></tr>
 <tr><td>Nifty FMCG</td><td>&minus;6.2</td><td>&minus;6.4</td><td>&minus;6.0</td><td>&minus;6.3</td><td><b>&minus;6.1</b></td><td><b>+0.1</b></td>
     <td class="no">Reject &mdash; below zero and going nowhere</td></tr>
</table>
<p>Pharma is the whole point of the exercise. Sorted by <code>RS Week 0</code> it is your #1
sector. Read left-to-right, it has been losing ground for five straight weeks.</p>

<h4>The <code>Source</code> column</h4>
<p>Do this <b>within</b> each <code>Source</code> group, not across it:</p>
<ul>
 <li><b><code>NSE Official</code></b> &mdash; the 30 real, free-float-weighted, tradeable NSE
 indices. Start here.</li>
 <li><b><code>Custom Sector</code></b> &mdash; equal-weighted baskets from
 <code>index_constituents.json</code>, finer cuts (Defence, Transmission, PSBs&hellip;). Use as a
 second pass to find <i>what inside</i> a broad NSE sector is doing the work.</li>
</ul>

<h4>Chart alternative</h4>
<p><code>market_charts.html</code> &rarr; &ldquo;NSE Sector RS&rdquo; tab &rarr; <b>top panel</b>.
Ignore the left two-thirds. Cover it with your hand and look only at the last ~5 weeks: you want
lines <b>above the zero line</b> that are <b>sloping up</b> in that final stretch. The legend
prints <code>RS=+8.1 &uarr;</code> &mdash; the arrow is the same 20-day trend as the sheet.</p>

<h3>Step 2 &mdash; validating on the &ldquo;Stage Sectors&rdquo; sheet</h3>
<p>Take your 4&ndash;6 names to the <b>Stage Sectors</b> sheet. It is already sorted Stage 2
first, then Mansfield RS descending, so your shortlisted names should be near the top &mdash; if
one is buried at row 40, that is your answer already.</p>
<p>Find each shortlisted sector's row and read <b>four columns</b>:</p>
<table class="rt">
 <tr><th>Column</th><th>Required value</th><th>What it means</th></tr>
 <tr><td><b><code>Stage Name</code></b></td><td>contains <b>&ldquo;Stage 2&rdquo;</b> (Advancing)</td>
     <td>Price is above a rising 30-week MA. Stage 1 = basing, 3 = topping, 4 = declining</td></tr>
 <tr><td><b><code>Mansfield RS</code></b></td><td><b>&gt; 0</b></td>
     <td>The price/Nifty-500 ratio is above its own 52-week average &mdash; outperforming
     <i>right now</i>, on a self-anchoring baseline</td></tr>
 <tr><td><b><code>RS Rising</code></b></td><td><b>TRUE</b></td>
     <td>Set at <code>stage_analysis.py:1116</code> as &ldquo;RS now &gt; RS five weeks ago&rdquo;</td></tr>
 <tr><td><b><code>Action</code></b></td><td>starts with <b>BUY</b> or <b>HOLD</b></td>
     <td>The verdict, already gated on RS</td></tr>
</table>

<h4><code>Action</code> does the work for you</h4>
<p><code>stage_analysis.py:762-794</code> bakes the RS gate straight into the instruction, so you
do not have to combine the columns by hand &mdash; just read the sentence:</p>
<ul>
 <li><code>&quot;BUY &mdash; &hellip;&quot;</code> &rarr; passed everything</li>
 <li><code>&quot;WAIT &mdash; advance not yet confirmed by RS&quot;</code> &rarr; Stage 2A, but
 <b>Mansfield RS is not above zero</b>. A fresh breakout the market is not rewarding.
 <b>Reject.</b></li>
 <li><code>&quot;WAIT &mdash; base not yet leading the market&quot;</code> &rarr; Stage 1B, still
 basing, RS has not turned. Watchlist, not a buy.</li>
 <li><code>&quot;HOLD, DO NOT ADD &mdash; no RS, history under 52w&quot;</code> &rarr; too young to
 measure. Reject.</li>
 <li>Anything with <code>&quot;(RS unavailable)&quot;</code> &rarr; treat as unproven.</li>
</ul>

<h4>Then glance at two more</h4>
<ul>
 <li><b><code>Warnings</code></b> &mdash; if it says <b>&ldquo;RS fading&rdquo;</b>, the sector is
 still Stage 2 but Mansfield RS has rolled over from its recent peak. That is the early exit
 signal. Do not start a new position into it.</li>
 <li><b><code>Sub</code></b> &mdash; <code>2A</code> = fresh breakout into the advance (best entry,
 highest risk); <code>2B</code> = continuation (safer, less upside left).
 <code>Weeks in Stage</code> tells you how late you are &mdash; 6 weeks is early, 40 weeks is late.</li>
</ul>

<h4>Chart alternative</h4>
<p>The <b>&ldquo;Sector Mansfield RS&rdquo;</b> sub-tab on this page. One panel per sector, zero
line drawn, and the <b>panel title shows the current RS value</b> (e.g.
<code>Nifty Capital Goods (4.3)</code>). Panels are ordered best-RS first. You want: line
<b>above zero</b>, and the <b>last few weeks sloping up</b>. A line drifting down toward zero
from a high = the fade, whatever the level.</p>

<h3>Putting Step 1 and Step 2 together</h3>
<table class="rt">
 <tr><th>Step 1 (Sector RS Ranking)</th><th>Step 2 (Stage Sectors)</th><th>What it is</th><th>Do</th></tr>
 <tr><td>RS Wk0 &gt; 0, &Delta;5w &gt; 0</td><td>Stage 2, Mansfield &gt; 0, RS Rising TRUE</td>
     <td><b>Confirmed leader</b></td><td class="ok">Go to Stage Stocks</td></tr>
 <tr><td>RS Wk0 <b>high</b>, &Delta;5w &le; 0</td><td>Mansfield &le; 0 <b>or</b> RS Rising FALSE</td>
     <td><b>Stale leader</b> &mdash; the Pharma row above</td><td class="no">Skip. This is the exact trap</td></tr>
 <tr><td>RS Wk0 &lt; 0 or ~0, &Delta;5w &gt; 0</td><td>Stage 2 (or 1B), Mansfield &gt; 0, Rising TRUE</td>
     <td><b>Emerging leader</b> &mdash; turning up, has not recouped the window deficit yet</td>
     <td class="warn">Best risk/reward. Size small, watch <code>Sub</code> for 1B&rarr;2A</td></tr>
 <tr><td>RS Wk0 &gt; 0</td><td>Stage 3 or 4</td>
     <td>Distribution has started under the surface</td><td class="no">Skip</td></tr>
</table>
<p>Row 3 is why you run both. The momentum sheet cannot see an emerging leader &mdash; the
arbitrary baseline still shows it in deficit. Mansfield RS sees it immediately because its
baseline moves with the data.</p>

<h3>Step 3 &mdash; then, and only then, pick stocks</h3>
<p>Open <b>Stage Stocks</b>. It is already filtered to Stage-2 sectors and sorted by
<code>Sector Rank</code>. Filter it to your surviving sector names and use the
<b><code>Setup</code></b> column from <code>stage_analysis.py:1210-1220</code>:</p>
<ul>
 <li><b><code>A</code></b> &mdash; Stage 2, Mansfield RS &gt; 0, full 8/8 Trend Template, and
 coiled (volatility contracting). Highest-conviction.</li>
 <li><b><code>B</code></b> &mdash; Stage 2 with positive RS, but not the full setup.</li>
 <li><b><code>C</code></b> &mdash; Stage 2 with RS at or below zero. Not a buy.</li>
</ul>
<p>Cross-check <code>Buy Ready</code> (x/4) and <code>% to Breakout</code> for timing.</p>
"""


_HTML = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Stage Analysis</title>
<style>
 body{margin:0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;background:#fafafa;color:#212121}
 .hdr-row{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;padding-right:18px}
 .hdr{padding:12px 18px 4px;font-size:19px;font-weight:700}
 .sub{padding:0 18px 10px;font-size:12px;color:#616161}
 .subtab-bar{display:flex;gap:6px;padding:0 18px;border-bottom:2px solid #e0e0e0;flex-wrap:wrap}
 .subtab-btn{padding:8px 16px;border:none;background:#eceff1;color:#37474f;font-size:13px;
   font-weight:600;cursor:pointer;border-radius:6px 6px 0 0}
 .subtab-btn.active{background:#1565c0;color:#fff}
 .subtab-panel{display:none;padding:10px 12px}
 .subtab-panel.active{display:block}
 .note{padding:6px 18px 18px;font-size:11.5px;color:#616161;line-height:1.55}
 .note b{color:#212121}
 .k{display:inline-block;width:11px;height:11px;border-radius:2px;vertical-align:-1px;margin-right:4px}
 /* Reference drop-down, pinned to the right of the heading. The body is
    absolutely positioned so opening it never reflows the charts below. */
 .refbox{position:relative;margin-top:12px;flex:none}
 .refbox>summary{cursor:pointer;list-style:none;white-space:nowrap;padding:6px 12px;
   font-size:13px;font-weight:600;color:#555;background:#fafafa;
   border:1px solid #e0e0e0;border-radius:6px}
 .refbox>summary::-webkit-details-marker{display:none}
 .refbox>summary::after{content:' \\25BE';color:#888}
 .refbox[open]>summary{background:#1565c0;color:#fff;border-color:#1565c0}
 .refbox[open]>summary::after{content:' \\25B4';color:#fff}
 .refbody{position:absolute;right:0;top:36px;z-index:60;box-sizing:border-box;
   width:940px;max-width:calc(100vw - 44px);
   max-height:78vh;overflow-y:auto;text-align:left;background:#fff;
   border:1px solid #d0d0d0;border-radius:8px;box-shadow:0 10px 30px rgba(0,0,0,.20);
   padding:14px 18px 18px;font-size:12.5px;line-height:1.55;color:#333}
 .refbody h3{margin:16px 0 6px;font-size:14px;color:#1565c0;
   border-top:1px solid #eee;padding-top:12px}
 .refbody h3:first-child{margin-top:0;border-top:none;padding-top:0}
 .refbody h4{margin:12px 0 4px;font-size:12.5px;color:#212121}
 .refbody p{margin:0 0 6px}
 .refbody ul,.refbody ol{margin:0 0 6px;padding-left:20px}
 .refbody li{margin-bottom:3px}
 .refbody code{background:#f2f4f6;border-radius:3px;padding:0 3px;font-size:11.5px}
 .refbody .eq{background:#f5f5f5;border-left:3px solid #1565c0;padding:7px 10px;
   font-size:13px;margin:6px 0 8px}
 .refbody table.rt{width:100%;border-collapse:collapse;font-size:11.5px;margin:6px 0 8px}
 .refbody table.rt th{background:#e3f2fd;border:1px solid #ccc;padding:5px 8px;text-align:left}
 .refbody table.rt td{border:1px solid #ddd;padding:4px 8px;vertical-align:top}
 .refbody td.ok{color:#2e7d32;font-weight:600}
 .refbody td.no{color:#c62828}
 .refbody td.warn{color:#ef6c00}
</style></head><body>
<div class="hdr-row">
 <div class="hdr">Stage Analysis &mdash; Weinstein 30-week framework, NIFTY 500 by NSE Industry</div>
 <details class="refbox">
  <summary>Reference</summary>
  <div class="refbody">__REFERENCE__</div>
 </details>
</div>
<div class="sub">__SUBTITLE__</div>
<div class="subtab-bar">__BUTTONS__</div>
__PANELS__
<div class="note">
 <b>Stages:</b>
 <span class="k" style="background:#9e9e9e"></span>1 Basing &mdash; flat 30-week MA after a decline; watch, do not buy yet. &nbsp;
 <span class="k" style="background:#2e7d32"></span>2 Advancing &mdash; rising MA, price above it; the only stage to buy. &nbsp;
 <span class="k" style="background:#ef6c00"></span>3 Topping &mdash; MA flattening after an advance; sell into strength. &nbsp;
 <span class="k" style="background:#c62828"></span>4 Declining &mdash; falling MA, price below it; avoid entirely.<br>
 <b>How to use:</b> work top-down. Only shop inside sectors that are in Stage 2
 <i>and</i> have a Mansfield RS above zero; inside those, buy stocks that are
 themselves in Stage 2 with RS above zero and expanding volume. The
 &ldquo;Stage Transitions&rdquo; sheet in the workbook lists everything that
 changed stage at this weekly close &mdash; that is where the actionable
 signals are.<br>
 <b>Timing:</b> only completed weekly bars are used, so this is as of
 __ASOF__. A partial week is deliberately excluded because it makes the
 30-week MA slope flicker and fires false transitions. A stage change is
 also only accepted after it has held for two consecutive weekly closes, so
 every signal here is one week behind its raw crossover — deliberately, to
 keep rangebound names from flip-flopping between Stage 1 and Stage 3.<br>
 <b>Caveat:</b> Stage 1 and Stage 3 both show a flat 30-week MA and are
 separated here by cycle position — a flat zone after an advance is a top, one
 after a decline is a base — falling back to a 52-week range test when that
 history is missing. Weinstein published no numbers for this, so it is the
 main source of misclassification. Sector composites
 are equal-weight builds of their own NIFTY 500 constituents, so their RS runs
 slightly rich against the cap-weighted __BENCH__ benchmark; the ranking
 between sectors is unaffected.
</div>
<script>
function _resizeStagePanel(p){
  if(!p || !window.Plotly) return;
  var d=p.querySelectorAll('.plotly-graph-div');
  for(var k=0;k<d.length;k++){ Plotly.Plots.resize(d[k]); }
}
function showStageTab(i){
  var b=document.querySelectorAll('.subtab-btn'),p=document.querySelectorAll('.subtab-panel');
  for(var k=0;k<b.length;k++){b[k].classList.toggle('active',k===i);}
  for(var k=0;k<p.length;k++){p[k].classList.toggle('active',k===i);}
  _resizeStagePanel(p[i]);
}
// Embedded as an iframe inside a hidden tab, so the first layout happens at
// zero width. Re-fit as soon as the body gains a real width.
(function(){
  if(!window.ResizeObserver) return;
  var last=0;
  new ResizeObserver(function(){
    var w=document.body.clientWidth;
    if(w>0 && w!==last){ last=w; _resizeStagePanel(document.querySelector('.subtab-panel.active')); }
  }).observe(document.body);
})();
</script>
</body></html>
"""


def build_html(sectors: list, mat: pd.DataFrame, as_of, out_path: str,
               n_stocks: int) -> str:
    """Render the three sub-tabbed stage views to a standalone HTML file."""
    figs = [
        ("Sector Stage Heatmap", _heatmap_figure(mat)),
        ("Market Stage Mix", _mix_figure(mat)),
        ("Sector Mansfield RS", _rs_figure(sectors)),
    ]
    buttons, panels = [], []
    for i, (label, fig) in enumerate(figs):
        div = fig.to_html(full_html=False,
                          include_plotlyjs=("cdn" if i == 0 else False),
                          config={"displaylogo": False})
        active = " active" if i == 0 else ""
        buttons.append('<button class="subtab-btn%s" onclick="showStageTab(%d)">%s</button>'
                       % (active, i, label))
        panels.append('<div class="subtab-panel%s">%s</div>' % (active, div))

    counts = pd.Series([r["Stage"] for r in sectors]).value_counts()
    mix = " · ".join("%d in Stage %d" % (int(counts.get(s, 0)), s) for s in (2, 1, 3, 4))
    as_of_str = pd.Timestamp(as_of).strftime("%d-%b-%Y")
    subtitle = ("%d sectors (%s) &middot; %d stocks &middot; week ending %s &middot; "
                "generated %s"
                % (len(sectors), mix, n_stocks, as_of_str,
                   dt.datetime.now().strftime("%d-%b-%Y %H:%M")))

    html = (_HTML
            .replace("__SUBTITLE__", subtitle)
            .replace("__BUTTONS__", "\n".join(buttons))
            .replace("__PANELS__", "\n".join(panels))
            .replace("__ASOF__", "the week ending " + as_of_str)
            .replace("__BENCH__", BENCHMARK_LABEL)
            .replace("__REFERENCE__", _REFERENCE_HTML))

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return out_path


# ─── Public API ──────────────────────────────────────────────────────────────

def analyse(lookback_days: int = LOOKBACK_DAYS,
            min_stocks: int = MIN_SECTOR_STOCKS,
            verbose: bool = True) -> dict:
    """Run the whole pipeline and return the raw profiles.

    Returns a dict with keys ``sectors`` (list of profile rows), ``stocks``
    (list of profile rows), ``as_of`` (last completed weekly close) and
    ``benchmark`` (the weekly benchmark close series). Returns ``{}`` when the
    universe or benchmark cannot be built.
    """
    end = dt.date.today()
    start = end - dt.timedelta(days=lookback_days)

    sector_map = _fetch_all_sectors(min_stocks=min_stocks)
    if not sector_map:
        print("  [stage] sector map unavailable; aborting")
        return {}
    symbols = sorted({s for syms in sector_map.values() for s in syms})
    if verbose:
        n_custom = sum(1 for k in sector_map if k.startswith("C: "))
        print("  [stage] universe: %d stocks across %d sectors "
              "(%d NSE industries + %d custom)"
              % (len(symbols), len(sector_map),
                 len(sector_map) - n_custom, n_custom))

    bench_daily = _fetch_daily([BENCHMARK], start, end, verbose=False)
    if BENCHMARK not in bench_daily:
        print("  [stage] benchmark %s unavailable; aborting" % BENCHMARK)
        return {}

    daily = _fetch_daily(symbols, start, end, verbose=verbose)
    if not daily:
        print("  [stage] no constituent price data; aborting")
        return {}

    # One market-wide cut-off keeps every entity on the same weekly grid even
    # when an individual symbol stopped printing a few sessions early.
    last_daily = session_cutoff(max(df.index.max() for df in daily.values()))
    bench_w = to_weekly(bench_daily[BENCHMARK], last_daily)["Close"]
    if verbose:
        print("  [stage] %d/%d symbols usable · last session %s"
              % (len(daily), len(symbols), last_daily.date()))

    sectors = []
    for name, syms in sector_map.items():
        comp = _composite(daily, syms)
        if comp.empty:
            continue
        weekly = to_weekly(comp, last_daily)
        prof = _profile(name, weekly, bench_w,
                        extra={"Members": len([s for s in syms if s in daily])})
        if prof is None:
            continue
        prof["_rs"] = mansfield_rs(weekly["Close"], bench_w)
        sectors.append(prof)

    # One stock can belong to an NSE industry *and* a custom sector, so this is
    # deliberately one-to-many. Collapsing it to a single label would drop a
    # stock out of the drill-down whenever its other sector is the Stage-2 one.
    sym_sectors = {}
    for name, syms in sector_map.items():
        for s in syms:
            sym_sectors.setdefault(s, []).append(name)

    stocks = []
    for sym, df in daily.items():
        weekly = to_weekly(df, last_daily)
        own = sym_sectors.get(sym, [])
        prof = _profile(sym, weekly, bench_w,
                        extra={"Sector": " / ".join(own), "_sectors": own})
        if prof is not None:
            stocks.append(prof)

    as_of = bench_w.index[-1] if len(bench_w) else last_daily
    if verbose:
        print("  [stage] staged %d sectors + %d stocks · week ending %s"
              % (len(sectors), len(stocks), pd.Timestamp(as_of).date()))
    return {"sectors": sectors, "stocks": stocks,
            "as_of": as_of, "benchmark": bench_w}


def stage_for(symbol: str, lookback_days: int = LOOKBACK_DAYS) -> dict:
    """Stage history + current state for one symbol, shaped for the chart API.

    This is the only entry point tradingcharts uses, so the badge on a chart
    and the row in the workbook are produced by the same code and cannot drift
    apart. Returns ``{"error": ...}`` rather than raising, so the endpoint can
    surface the reason instead of hanging or 500-ing.
    """
    sym = (symbol or "").strip().upper()
    if not sym:
        return {"error": "no symbol"}

    end = dt.date.today()
    start = end - dt.timedelta(days=lookback_days)
    daily = _fetch_daily([sym], start, end, verbose=False)
    if sym not in daily:
        return {"error": "no usable daily history for %s (need %d bars)"
                         % (sym, MIN_BARS)}

    cutoff = session_cutoff(daily[sym].index.max())
    weekly = to_weekly(daily[sym], cutoff)
    sf = stage_frame(weekly)
    valid = sf.dropna(subset=["Stage"])
    if valid.empty:
        return {"error": "%s has only %d weekly bars; the 30-week MA needs %d"
                         % (sym, len(sf), MA_WEEKS + SLOPE_WEEKS)}

    bench_daily = _fetch_daily([BENCHMARK], start, end, verbose=False)
    bench_w = (to_weekly(bench_daily[BENCHMARK], cutoff)["Close"]
               if BENCHMARK in bench_daily else pd.Series(dtype=float))
    prof = _profile(sym, weekly, bench_w)
    prof["Setup"] = _setup_grade(prof)

    bars = [{"time": t.strftime("%Y-%m-%d"),
             "ma": round(float(r["MA"]), 2),
             "close": round(float(r["Close"]), 2),
             "stage": int(r["Stage"])}
            for t, r in valid.iterrows()]

    def _num(v):
        return None if v is None or pd.isna(v) else float(v)

    return {
        "symbol": sym,
        "ma_weeks": MA_WEEKS,
        "as_of": valid.index[-1].strftime("%Y-%m-%d"),
        "benchmark": BENCHMARK_LABEL,
        "bars": bars,
        "colors": {str(k): v for k, v in STAGE_COLORS.items()},
        "current": {
            "stage": prof["Stage"],
            "name": prof["Stage Name"],
            "sub": prof["Sub"],
            "action": prof["Action"],
            "weeks": prof["Weeks in Stage"],
            "base_weeks": prof["Base Weeks"],
            "setup": prof["Setup"],
            "rs": _num(prof["Mansfield RS"]),
            "rs_rising": prof["RS Rising"],
            "slope": _num(prof["MA Slope %"]),
            "dist": _num(prof["Dist from MA %"]),
            "pctile": _num(prof["52w Range %ile"]),
            "persist": _num(prof["Above MA %wks"]),
            "vol": _num(prof["Vol vs 10w"]),
            "vol_dir": _num(prof["Vol Direction"]),
            "box_low": _num(prof["Box Low"]),
            "box_high": _num(prof["Box High"]),
            "to_breakout": _num(prof["% to Breakout"]),
            "stop": _num(prof["Stop"]),
            "trend_template": prof["Trend Template"],
            "tt_max": prof["TT Max"],
            "vcp": prof["VCP"],
            "buy_ready": prof["Buy Ready"],
            "top_quality": prof["Top Quality"],
            "warnings": prof["Warnings"],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# LLM SYNTHESIS LAYER
# ═══════════════════════════════════════════════════════════════════════════════

def llm_regime_narrative(sector_df, trans_df):
    """Generate a weekly market regime narrative from stage data.

    Returns dict with narrative, regime classification, and key transitions,
    or None if LLM unavailable.
    """
    if not llm_json or not llm_is_available():
        return None

    import json as _json

    sector_data = []
    for _, row in sector_df.iterrows():
        sector_data.append({
            "sector": str(row.get("Sector", "")),
            "stage": int(row.get("Stage", 0)) if pd.notna(row.get("Stage")) else 0,
            "sub_stage": str(row.get("Sub", "")),
            "rs_rank": int(row.get("Rank", 0)) if pd.notna(row.get("Rank")) else 0,
            "rs_vs_nifty": float(row.get("RS Week 0", 0)) if pd.notna(row.get("RS Week 0")) else 0,
            "action": str(row.get("Action", "")),
        })

    transitions = []
    if trans_df is not None and not trans_df.empty:
        for _, row in trans_df.head(15).iterrows():
            transitions.append({
                "entity": str(row.get("Entity", row.get("Sector", ""))),
                "type": str(row.get("Type", "")),
                "signal": str(row.get("Signal", "")),
            })

    user_data = _json.dumps({
        "sectors": sector_data,
        "transitions": transitions,
        "total_sectors": len(sector_data),
        "stage2_count": sum(1 for s in sector_data if s["stage"] == 2),
        "stage3_count": sum(1 for s in sector_data if s["stage"] == 3),
        "stage4_count": sum(1 for s in sector_data if s["stage"] == 4),
    }, default=str)

    system_prompt = (
        "You are a Weinstein stage analysis expert for Indian equity markets. "
        "Generate a weekly regime narrative from sector stage data.\n\n"
        "Return JSON with:\n"
        "- narrative: 5-8 sentence market regime summary. Mention specific sectors "
        "by name. Note divergences, rotation direction, breadth of advance/decline.\n"
        "- regime: one of broad_advance, narrow_advance, rotation, distribution, "
        "broad_decline\n"
        "- key_transitions: list of {sector, from_stage, to_stage, significance} "
        "for the most important transitions this week\n"
        "- actionable: 2-3 sentence recommendation for a positional trader"
    )

    print("  [LLM] Generating regime narrative …")
    try:
        result = llm_json(system_prompt, user_data, max_tokens=2000)
        if result:
            regime = result.get("regime", "unknown")
            print(f"  [LLM] Regime: {regime} | Narrative: {len(result.get('narrative', ''))} chars")
        return result
    except Exception as e:
        print(f"  [LLM] Regime narrative failed: {e}")
        return None


def run(output_prefix: str | None = None,
        lookback_days: int = LOOKBACK_DAYS,
        min_stocks: int = MIN_SECTOR_STOCKS,
        write_excel: bool = True,
        verbose: bool = True,
        use_llm: bool = True):
    """Build the stage report. Returns (sheets_dict, html_path).

    `sheets_dict` holds the Excel sheets (Stage Sectors, Stage Stocks,
    Stage Transitions, and optionally Stage Narrative) so run_all can fold
    them into the unified workbook; `write_excel=True` additionally drops a
    standalone `<prefix>.xlsx`.
    Returns ``({}, None)`` when there is not enough data, so the caller can
    skip the tab without failing the pipeline.
    """
    res = analyse(lookback_days=lookback_days, min_stocks=min_stocks,
                  verbose=verbose)
    if not res or not res["sectors"]:
        print("  [stage] No stage data; report skipped")
        return {}, None

    sectors, stocks, as_of = res["sectors"], res["stocks"], res["as_of"]

    sector_df = build_sector_sheet(sectors)
    rank = dict(zip(sector_df["Sector"], sector_df["Rank"]))
    focus = [r["Name"] for r in sectors if r["Stage"] == 2]
    if not focus:
        focus = list(sector_df["Sector"].head(3))
        if verbose:
            print("  [stage] no Stage 2 sectors — stock sheet falls back to "
                  "the top 3 by RS: %s" % ", ".join(focus))
    stock_df = build_stock_sheet(stocks, rank, focus)
    trans_df = build_transition_sheet(sectors, stocks)

    sheets = {
        "Stage Sectors": sector_df,
        "Stage Stocks": stock_df,
        "Stage Transitions": trans_df,
    }

    if use_llm:
        narrative = llm_regime_narrative(sector_df, trans_df)
        if narrative:
            rows = [
                {"Field": "Regime", "Value": narrative.get("regime", "")},
                {"Field": "Narrative", "Value": narrative.get("narrative", "")},
                {"Field": "Actionable", "Value": narrative.get("actionable", "")},
            ]
            for kt in narrative.get("key_transitions", []):
                rows.append({
                    "Field": f"Transition: {kt.get('sector', '')}",
                    "Value": f"Stage {kt.get('from_stage', '?')} → "
                             f"{kt.get('to_stage', '?')}: "
                             f"{kt.get('significance', '')}",
                })
            sheets["Stage Narrative"] = pd.DataFrame(rows)

    prefix = output_prefix or os.path.join(SCRIPT_DIR, "stage_analysis")
    if prefix.endswith(".html"):
        prefix = prefix[:-5]

    mat = _stage_matrix([r for r in sectors
                         if r["Name"] in list(sector_df["Sector"])])
    mat = mat.reindex(columns=list(sector_df["Sector"]))
    html_path = build_html(sectors, mat, as_of, prefix + ".html", len(stocks))

    if write_excel:
        xlsx = prefix + ".xlsx"
        with pd.ExcelWriter(xlsx, engine="openpyxl") as writer:
            for name, df in sheets.items():
                df.to_excel(writer, sheet_name=name[:31], index=False)
        print("  [stage] Excel: %s" % xlsx)
    print("  [stage] Chart: %s" % html_path)
    return sheets, html_path


def main():
    parser = argparse.ArgumentParser(
        description="Weinstein stage analysis for the NIFTY 500")
    parser.add_argument("-o", "--output", default=None,
                        help="Output prefix (default: stage_analysis)")
    parser.add_argument("--lookback", type=int, default=LOOKBACK_DAYS,
                        help="Calendar days of daily history to pull")
    parser.add_argument("--no-llm", action="store_true",
                        help="Skip LLM narrative generation")
    args = parser.parse_args()

    sheets, html = run(output_prefix=args.output, lookback_days=args.lookback,
                       use_llm=not args.no_llm)
    if not sheets:
        return 1
    sec = sheets["Stage Sectors"]
    print("\nStage 2 sectors:")
    s2 = sec[sec["Stage Name"] == STAGE_NAMES[2]]
    if s2.empty:
        print("  (none — no sector leadership)")
    else:
        for _, r in s2.iterrows():
            print("  %-38s RS %+7.2f  %2d weeks"
                  % (r["Sector"], r["Mansfield RS"], r["Weeks in Stage"]))
    print("\nTransitions this week: %d" % len(sheets["Stage Transitions"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
