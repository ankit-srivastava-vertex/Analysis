# Indian Equity Trading Strategy Framework
**Toolkit-Driven Approach for NSE/BSE Markets**
*Generated: 23-Apr-2026*

---

## 1. Philosophy

This framework combines **institutional flow tracking**, **sector momentum rotation**, and **smart-money deal surveillance** to identify high-probability trades in the Indian equity market. Every signal is derived from publicly available NSE/BSE data using the automated tools in this workspace.

### Core Principles
- **Follow the money**: FII/DII positioning in F&O reveals directional intent before price moves.
- **Sector rotation over stock picking**: Ride strong sectors, avoid weak ones. Relative strength tells you which sectors institutional capital is flowing into.
- **Smart-money confirmation**: Bulk/block deals and SAST filings from marquee investors confirm conviction.
- **Rule-based decisions**: Every entry, exit, and position size follows predefined criteria — no discretion.

---

## 2. Toolkit Overview

| Tool | File | Purpose | Frequency |
|------|------|---------|-----------|
| Custom Sector Indices | `custom_sector_index.py` | Equal-weight indices for 5 sectors (Energy, Transmission, Defence, IT Services, Pharma) | Weekly |
| FII/DII F&O Tracker | `fii_dii_flows.py` | Historical OI positions + daily cash market flows | Daily |
| Sector Momentum RS | `sector_momentum.py` | Mansfield Relative Strength vs Nifty 50 | Weekly |
| Bulk/Block Deals | `bulk_block_deals.py` | Superstar investor deal surveillance | Daily |
| SAST Tracker | `sast_tracker.py` | Takeover Reg 29 filings by watchlist names | Daily |

---

## 3. Strategy 1: Sector Momentum Rotation

### Concept
Buy sectors where RS > 100 and rising. Avoid or short sectors where RS < 100 and falling. Rotate capital from weakening sectors into strengthening ones.

### Signals (from `sector_momentum.py`)

| Condition | Action |
|-----------|--------|
| RS > 100 and RS rising (positive RS change) | **BUY** sector constituents |
| RS > 100 but RS falling (negative RS change) | **HOLD** — monitor for exit |
| RS crosses below 100 from above | **EXIT** — sector losing momentum |
| RS < 100 and RS falling | **AVOID** — underperforming sector |
| RS < 100 but RS rising sharply | **WATCH** — potential reversal |

### Current Positioning (22-Apr-2026)

| Sector | RS | RS Change | Signal |
|--------|---:|----------:|--------|
| Defence | 166.6 | +13.5 | **BUY** — strongest momentum |
| Pharma | 125.0 | -3.3 | **HOLD** — outperforming but fading |
| Energy | 89.5 | +0.3 | **WATCH** — underperforming, flat |
| Transmission | 81.2 | +4.4 | **WATCH** — improving off lows |
| IT Services | 71.9 | +4.5 | **WATCH** — deep underperformance, improving |

### Execution Rules
1. **Allocation**: Equal-weight across constituents of selected sectors.
2. **Max sectors**: Hold 2-3 sectors at a time.
3. **Rebalance**: Weekly, after running `sector_momentum.py`.
4. **Position size**: Max 5% per stock, max 25% per sector.
5. **Stop-loss**: Exit if sector index falls 8% from entry.

### Selecting Stocks Within a Sector
- Prefer the top 3-5 stocks by liquidity (F&O stocks preferred for hedging).
- Avoid stocks with upcoming corporate actions (demergers, splits) — these create false signals.
- Cross-reference with bulk/block deal data for smart-money confirmation.

---

## 4. Strategy 2: FII Positioning & Sentiment

### Concept
FII net positions in index futures are a leading indicator of market direction. Large net-short FII positions coincide with market bottoms; large net-long with tops or continuation rallies.

### Signals (from `fii_dii_flows.py`)

| Metric | Bullish | Bearish |
|--------|---------|---------|
| FII Net Index Futures | Turning positive or adding longs | Deep negative and increasing shorts |
| FII Daily Change | Large positive swing (>10K contracts) | Large negative swing |
| FII Cash Market | Net buyers (positive net value in Cr) | Net sellers |
| DII vs FII divergence | FII selling + DII buying = potential bottom | Both selling = risk-off |

### Current Positioning (22-Apr-2026)

| Metric | Value | Reading |
|--------|------:|---------|
| FII Net Index Futures | -183,314 | Bearish — FII significantly net-short index futures |
| FII Net Stock Futures | +969,533 | Bullish — FII net-long individual stocks |
| FII Total Net | +866,822 | Neutral-Bullish overall |
| DII Total Net | -3,890,289 | DII heavily net-short (hedging?) |
| FII Cash (22-Apr) | -2,078 Cr | Mild selling in cash segment |
| DII Cash (22-Apr) | -1,048 Cr | DII also selling — unusual |

### Interpretation Framework
1. **FII net-short index + net-long stocks** = Selective conviction. FII are bearish on the broad index (hedging via index shorts) but bullish on specific stocks/sectors. This is a **stock-picker's market** signal.
2. **When to be aggressive**: FII index futures flip from net-short to net-long, OR daily change shows sustained buying (>5 consecutive days of positive change).
3. **When to be defensive**: FII accelerate index shorts beyond -200K AND cash market selling exceeds -3,000 Cr/day for 3+ days.

### Execution Rules
1. **Market exposure**: Increase equity allocation when FII index futures are turning positive. Reduce when shorts are accelerating.
2. **Hedging**: When FII net index shorts exceed 150K contracts, buy Nifty put options (3-5% OTM, 1-month expiry) as portfolio insurance.
3. **Contrarian entries**: When FII net index shorts exceed 250K AND market has fallen >5% in a month, start scaling into quality stocks (DII buying divergence confirms).

---

## 5. Strategy 3: Smart Money Surveillance

### Concept
Track bulk/block deals and SAST filings by proven investors. Their accumulation patterns reveal early-stage conviction in specific stocks.

### Watchlist Investors (tracked via `bulk_block_deals.py` and `sast_tracker.py`)

| Category | Names |
|----------|-------|
| Ace Investors | Ashish Kacholia, Vijay Kedia, Sunil Singhania |
| Institutional | Goldman Sachs, Abakkus Asset Manager |
| Domestic Funds | Quant MF, SBI MF, HDFC MF |

### Signal Generation

| Event | Signal | Action |
|-------|--------|--------|
| Bulk BUY by 2+ watchlist investors in same stock | **Strong Buy** | Enter on next dip (within 3 days) |
| Block deal BUY by institutional investor | **Buy** | Add to watchlist, enter on breakout |
| SAST filing showing stake increase >1% | **Accumulation** | Start building position |
| Bulk SELL by watchlist investor | **Caution** | Don't enter; if holding, tighten stop |
| SAST filing showing stake decrease | **Exit signal** | Reduce position |

### Execution Rules
1. **Confirmation needed**: A bulk/block deal alone is not enough. Combine with:
   - Sector RS > 100 (momentum confirmation)
   - No FII aggressive selling (flow confirmation)
   - Stock above its 50-day moving average (trend confirmation)
2. **Position size**: Start with 2% of portfolio. Add another 2% if price holds above deal price after 5 days.
3. **Stop-loss**: 10% below the bulk/block deal price.
4. **Target**: Hold for 3-6 months or until RS of the sector drops below 100.

---

## 6. Combined Decision Matrix

The real edge comes from combining all three signals:

| Sector RS | FII Flow | Smart Money | Combined Signal | Action |
|-----------|----------|-------------|-----------------|--------|
| Strong (>100, rising) | Supportive (net buying) | Bulk buys in sector | **High Conviction BUY** | Max allocation (25% of portfolio) |
| Strong (>100, rising) | Neutral | No activity | **Standard BUY** | Normal allocation (15%) |
| Strong (>100, falling) | Selling | Smart money selling | **EXIT** | Close positions |
| Weak (<100) | Selling | No activity | **AVOID** | Zero allocation |
| Weak (<100, rising) | Turning positive | Accumulation signals | **EARLY ENTRY** | Small starter position (5%) |

### Current Combined View (23-Apr-2026)

| Sector | RS Signal | FII Signal | Combined |
|--------|-----------|------------|----------|
| **Defence** | BUY (RS=166.6, rising) | FII stock-positive | **High conviction — maintain/add** |
| **Pharma** | HOLD (RS=125, fading) | Mixed | **Hold existing, no new positions** |
| **Energy** | AVOID (RS=89.5) | — | **Stay out** |
| **Transmission** | AVOID (RS=81.2) | — | **Stay out** |
| **IT Services** | AVOID (RS=71.9) | — | **Stay out, but improving** |

---

## 7. Risk Management

### Position Sizing
- **Per stock**: Max 5% of total portfolio
- **Per sector**: Max 25% of total portfolio
- **Cash reserve**: Always keep 20-30% in liquid funds/cash when FII index shorts > 150K

### Stop-Loss Rules
- **Individual stock**: 8-10% below entry
- **Sector exit**: Sector index drops 8% from peak OR RS crosses below 100
- **Portfolio-level**: If Nifty drops >3% in a single day and FII sell >5,000 Cr in cash, reduce all positions by 50%

### Indian Market-Specific Considerations
1. **Expiry weeks (last Thursday of month)**: FII roll their F&O positions. Watch rollover data — high rollover + net-long = bullish continuation; low rollover + net-short = bearish.
2. **Budget/policy events**: Reduce position sizes by 50% ahead of Union Budget, RBI policy meetings, and election results.
3. **Global correlation**: FII flows are influenced by US dollar strength and US bond yields. When DXY rises sharply (>2% in a week), expect FII selling in Indian equities.
4. **Settlement cycles**: T+1 settlement in India. Factor this into entry timing.
5. **Circuit limits**: Individual stocks have 5%/10%/20% circuit limits. Sector indices smooth out single-stock circuit events.

---

## 8. Daily Workflow

### Morning (Before 9:15 AM IST)
1. Run `fii_dii_flows.py` — Check previous day's FII/DII F&O positions
2. Run `bulk_block_deals.py` — Check for overnight bulk/block deals
3. Run `sast_tracker.py` — Check for SAST filings
4. Review email reports for any flagged activity

### Weekly (Weekend)
1. Run `sector_momentum.py` — Update RS rankings
2. Run `custom_sector_index.py` — Update sector index levels
3. Rebalance sectors if RS rankings changed materially
4. Review positions against stop-loss levels

### Automation
All scripts support automated execution via GitHub Actions. Configure email delivery for morning reports:
```bash
# Daily (Mon-Fri, 8:30 AM IST)
python3 fii_dii_flows.py -o daily_fii
python3 bulk_block_deals.py
python3 sast_tracker.py

# Weekly (Saturday)
python3 sector_momentum.py -o weekly_momentum
python3 custom_sector_index.py -o weekly_sectors
```

---

## 9. Backtesting Notes

### What the data shows (Jan 2024 — Apr 2026)
- **Defence** has been the standout sector: RS consistently >140, index +92% from base.
- **Pharma** second strongest: RS ~125, index +44%.
- **Energy** has mean-reverted: was strong in mid-2024, now at RS 89 (underperforming).
- **IT Services** deeply underperforming: RS 72, index -17% from base. However, RS is improving — potential sector rotation candidate.
- **FII index futures**: Persistently net-short at -183K contracts, suggesting FII view the broad market as fairly/over-valued, but their net-long stock futures (+970K) shows selective bullishness.

### Key Lesson
Sector rotation works in Indian markets because institutional capital is concentrated. When FII rotate into a sector (visible in F&O + cash flows), the move is sustained for months. The RS indicator captures this early — typically 2-4 weeks before it becomes obvious in price.

---

## 10. Disclaimer

This document is for educational and analytical purposes only. It is not investment advice. Past performance of sectors or strategies does not guarantee future results. Always conduct your own due diligence before making investment decisions. Trading in derivatives involves substantial risk.
