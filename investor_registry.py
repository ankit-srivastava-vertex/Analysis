"""
Investor Registry — Centralised Investor Configuration
========================================================

SUMMARY
-------
Single source of truth for all tracked investors and their identities across
multiple data sources.  Both BulkBlock.py and fii_stake_tracker.py import from
here instead of maintaining separate, duplicated lists.

Each investor entry groups:
  * ``bulk_deal_names``  — every spelling variant seen in NSE/BSE bulk & block
                           deal feeds (matched by BulkBlock._match_clients).
  * ``screener_urls``    — Screener.in ``/people/`` page URLs, one per legal
                           entity that SEBI filings list separately.

Helper functions return flat lists that are drop-in replacements for the inline
constants these two scripts used to carry.

USAGE
-----
    from investor_registry import all_bulk_deal_names, all_screener_urls

    # In BulkBlock.py (inside BSEScraper.run):
    client_names_to_filter = all_bulk_deal_names()

    # In fii_stake_tracker.py (module level):
    HNI_PEOPLE_URLS = all_screener_urls()

Adding a new investor:
    1. Append a dict to INVESTOR_REGISTRY below.
    2. That single edit propagates to every consumer automatically.
"""

# ─── Registry ─────────────────────────────────────────────────────────────────
#
# Each entry is one logical investor (person, family, or fund house).
#   id            — stable snake_case key, never shown to the user
#   display_name  — human-readable label for reports/alerts
#   bulk_deal_names — list[str] of every name variant that appears in NSE/BSE
#                     bulk & block deal feeds.  Case-insensitive matching is
#                     handled downstream (_normalise_name), but the exact
#                     capitalisation from exchange data is preserved here so a
#                     grep for the literal string still works.
#   screener_urls — list[str] of Screener.in /people/ page URLs, one per
#                   legal entity tracked separately in SEBI filings.  Empty
#                   list if no Screener.in page exists or is known.

INVESTOR_REGISTRY = [
    # ── Ashish Kacholia ───────────────────────────────────────────────────
    {
        "id": "ashish_kacholia",
        "display_name": "Ashish Kacholia",
        "bulk_deal_names": [
            'ASHISH KACHOLIA',
            'ASHISH RAMESH KACHOLIA',
            'ASHISH RAMESHCHANDRA KACHOLIA',
            'BENGAL FIN. & INV. PVT. LTD',
            'SURYAVANSHI COMMOTRADE PVT LTD',
            'Suryavanshi Commotrade Private Limited',
            'HIMALAYA FINANCE & INV. CO',
            'HIMALAYA FINANCE & INVESTMENT COMPANY',
            'HIMALAYA FINANCE AND INVESTMENT CO',
            'KACHOLIA ASHISH',
            'LUCKY INVESTMENT MANAGERS PRIVATE LIMITED',
            'R.B.A. FINANCE ## INVESTMENT CO.',
            'R.B.A.FINANCE & INVT. CO',
            'SURYA VANSHI COMMOTRADE PVT. LTD.',
            'BENGAL FINANCE & INVESTMENT PRIVATE LIMITED',
            'BENGAL FINANCE & INVESTMENT PVT. LTD.',
        ],
        "screener_urls": [
            "https://www.screener.in/people/127736/ashish-kacholia/",
            "https://www.screener.in/people/148535/bengal-finance-and-investment-pvt-ltd/",
            "https://www.screener.in/people/64/bengal-finance-and-ninvestment-private-limited/",
            "https://www.screener.in/people/19205/suryavanshi-commotrade-private-limited/",
            "https://www.screener.in/people/133451/bengal-finance-investment-p-ltd/",
            "https://www.screener.in/people/153475/rba-finance-investment-co-partnership-firm/",
        ],
    },
    # ── Suresh Kumar Agarwal ──────────────────────────────────────────────
    {
        "id": "suresh_kumar_agarwal",
        "display_name": "Suresh Kumar Agarwal",
        "bulk_deal_names": [
            'Suresh Kumar Agarwal',
        ],
        "screener_urls": [
            "https://www.screener.in/people/2350/suresh-kumar-agarwal/",
        ],
    },
    # ── Vijay Kedia (family + entity) ─────────────────────────────────────
    {
        "id": "vijay_kedia",
        "display_name": "Vijay Kedia",
        "bulk_deal_names": [
            'Ankit Vijay Kedia',
            'ANKUSH KEDIA',
            'Vijay Krishanlal Kedia',
            'Kedia Secuirities Private Limited',
        ],
        "screener_urls": [
            "https://www.screener.in/people/163158/vijay-kishanlal-kedia/",
            "https://www.screener.in/people/134160/vijay-kedia/",
            "https://www.screener.in/people/7379/kedia-secuirities-private-limited/",
        ],
    },
    # ── Ajay Kumar Aggarwal ───────────────────────────────────────────────
    {
        "id": "ajay_kumar_aggarwal",
        "display_name": "Ajay Kumar Aggarwal",
        "bulk_deal_names": [
            'AJAY KUMAR AGGARWAL',
        ],
        "screener_urls": [
            "https://www.screener.in/people/21712/ajay-kumar-aggarwal/",
        ],
    },
    # ── Ajay Upadhyaya ───────────────────────────────────────────────────
    {
        "id": "ajay_upadhyaya",
        "display_name": "Ajay Upadhyaya",
        "bulk_deal_names": [
            'AJAY UPADHYAYA',
            'UPADHYAYA AJAY',
            'UPADHYAYA AJAY SHIV NARAYAN',
        ],
        "screener_urls": [
            "https://www.screener.in/people/679/ajay-upadhyaya/",
        ],
    },
    # ── Akash Bhanshali ──────────────────────────────────────────────────
    {
        "id": "akash_bhanshali",
        "display_name": "Akash Bhanshali",
        "bulk_deal_names": [
            'AKASH BHANSHALI',
        ],
        "screener_urls": [
            "https://www.screener.in/people/170071/akash-bhanshali/",
        ],
    },
    # ── Goldman Sachs ────────────────────────────────────────────────────
    {
        "id": "goldman_sachs",
        "display_name": "Goldman Sachs",
        "bulk_deal_names": [
            'GOLDMAN SACHS (SINGAPORE) PTE',
            'GOLDMAN SACHS (SINGAPORE) PTE.- ODI',
            'GOLDMAN SACHS COLLECTIVE TRUST - EMERGING MARKETS EQUITY EX CHINA FUND',
            'GOLDMAN SACHS COLLECTIVE TRUST - EMERGING MARKETS EQUITY EX. CHINA FUND',
            'GOLDMAN SACHS FDS GOLDMAN SACHS INDIA EQ PORTFOLIO',
            'GOLDMAN SACHS FUNDS  GOLDMAN SACHS INDIA EQUITY PORTFOLIO',
            'GOLDMAN SACHS FUNDS - GOLDMAN SACHS INDIA EQUITY PORTFOLIO',
            'GOLDMAN SACHS FUNDS GOLDMAN SACHS INDIA EQUITY PORTFOLIO',
            'GOLDMAN SACHS FUNDS-GOLDMAN SACHS ASIA EQUITY PORTFOLIO',
            'GOLDMAN SACHS INDIA LIMITED',
            'GOLDMAN SACHS INVESTMENT (MAURITIUS) I LTD',
            'GOLDMAN SACHS INVESTMENTS (MAURITIUS) I LIMITED',
            'GOLDMAN SACHS INVESTMENTS HOLDINGS ASIA LIMITED',
            'GOLDMAN SACHS INVESTMENTS MAURITIUS  I LIMITED',
            'GOLDMAN SACHS INVESTMENTS MAURITIUS  I LTD',
            'GOLDMAN SACHS INVESTMENTS MAURITIUS I LIMITED',
            'GOLDMAN SACHS TRUST II - GOLDMAN SACHS GQG PARTNERS INTERNATIONAL OPPORTUNITIES FUND',
            'GOLDMANSACHS FUNDS GOLDMANSACHS INDIA EQUITY PORTFOLIO',
        ],
        "screener_urls": [
            "https://www.screener.in/people/131169/goldman-sachs-funds-goldman-sachs-asia-equity-portfolio/",
            "https://www.screener.in/people/129685/goldman-sachc-funds-goldman-sachs-india-equity-portfolio/",
            "https://www.screener.in/people/19335/goldman-sachs-funds-goldman-sachsindia-equity-p/",
            "https://www.screener.in/people/98375/goldman-sachs-investments-mauritius-i-limited/",
            "https://www.screener.in/people/181599/goldman-sachs-bank-europe-se/",
        ],
    },
    # ── India Equity Fund 1 ──────────────────────────────────────────────
    {
        "id": "india_equity_fund_1",
        "display_name": "India Equity Fund 1",
        "bulk_deal_names": [
            'INDIA EQUITY FUND 1',
        ],
        "screener_urls": [
            "https://www.screener.in/people/174015/india-equity-fund-1/",
        ],
    },
    # ── Madhusudhan Kela (family + funds) ────────────────────────────────
    {
        "id": "madhusudhan_kela",
        "display_name": "Madhusudhan Kela",
        "bulk_deal_names": [
            'MADHURI MADHUSUDAN KELA',
            'COHESION MK BEST IDEAS SUB-TRUST',
            'FOUNDERS COLLECTIVE FUND',
            'SINGULARITY EQUITY FUND I',
            'SINGULARITY LARGE VALUE FUND II',
            'SINGULARITY LARGE VALUE FUND III',
            'Chartered Finance & Leasing Limited',
            'Madhusudan Murlidhar Kela',
            'SINGULARITY LARGE VALUE FUND I',
            'SINGULARITY GROWTH OPPORTUNITIES FUND II',
            'CHARTERED FINANCE & LEASI NG LIMITED',
        ],
        "screener_urls": [
            "https://www.screener.in/people/30960/madhuri-madhusudan-kela/",
            "https://www.screener.in/people/86419/madhusudhan-murlidhar-kela/",
            "https://www.screener.in/people/32876/madhusudan-murlidhar-kela/",
            "https://www.screener.in/people/154329/mahi-madhusudan-kela/",
            "https://www.screener.in/people/35415/cohesion-mk-best-ideas-sub-trust/",
            "https://www.screener.in/people/150091/singularity-equity-fund-i/",
            "https://www.screener.in/people/126373/chartered-finance-leasing-limited/",
        ],
    },
    # ── Mona Laroia ──────────────────────────────────────────────────────
    {
        "id": "mona_laroia",
        "display_name": "Mona Laroia",
        "bulk_deal_names": [
            'LAROIA MONA',
            'MONA LAROIA',
        ],
        "screener_urls": [
            "https://www.screener.in/people/108142/laroia-mona/",
        ],
    },
    # ── Shalu Aggarwal ───────────────────────────────────────────────────
    {
        "id": "shalu_aggarwal",
        "display_name": "Shalu Aggarwal",
        "bulk_deal_names": [
            'SHALU  AGGARWAL',
        ],
        "screener_urls": [
            "https://www.screener.in/people/131338/shalu-aggarwal/",
        ],
    },
    # ── Bijal Pritesh Vora ───────────────────────────────────────────────
    {
        "id": "bijal_pritesh_vora",
        "display_name": "Bijal Pritesh Vora",
        "bulk_deal_names": [
            'BIJAL PRITESH VORA',
        ],
        "screener_urls": [
            "https://www.screener.in/people/116773/bijal-pritesh-vora/",
        ],
    },
    # ── Malabar India Fund ───────────────────────────────────────────────
    {
        "id": "malabar_india_fund",
        "display_name": "Malabar India Fund",
        "bulk_deal_names": [
            'MALABAR INDIA FUND LIMITED',
        ],
        "screener_urls": [
            "https://www.screener.in/people/126875/malabar-india-fund-limited/",
        ],
    },
    # ── Massachusetts Institute of Technology ────────────────────────────
    {
        "id": "mit",
        "display_name": "Massachusetts Institute of Technology",
        "bulk_deal_names": [
            'MASSACHUSETTS INSTITUTE OF TECHNOLOGY',
        ],
        "screener_urls": [
            "https://www.screener.in/people/149987/massachusetts-institute-of-techno/",
        ],
    },
    # ── Manish Grover (Jeena Sikho promoter) ─────────────────────────────
    {
        "id": "manish_grover",
        "display_name": "Manish Grover",
        "bulk_deal_names": [
            'MANISH GROVER',
        ],
        "screener_urls": [
            "https://www.screener.in/people/119660/manish-grover/",
        ],
    },
    # ── Rohan Gupta (SG Finserve promoter) ───────────────────────────────
    {
        "id": "rohan_gupta",
        "display_name": "Rohan Gupta",
        "bulk_deal_names": [
            'ROHAN GUPTA',
        ],
        "screener_urls": [
            "https://www.screener.in/people/33390/rohan-gupta/",
        ],
    },
    # ── Nalanda India Fund ───────────────────────────────────────────────
    {
        "id": "nalanda_india_fund",
        "display_name": "Nalanda India Fund",
        "bulk_deal_names": [
            'NALANDA INDIA EQUITY FUND LIMITED',
            'NALANDA INDIA FUND LIMITED',
        ],
        "screener_urls": [
            "https://www.screener.in/people/78663/nalanda-india-fund-limited/",
            "https://www.screener.in/people/73618/nalanda-india-equity-fund-limited/",
        ],
    },
    # ── Mukul Mahavir Agrawal (family + funds) ───────────────────────────
    {
        "id": "mukul_agrawal",
        "display_name": "Mukul Mahavir Agrawal",
        "bulk_deal_names": [
            'Mukul Mahavir Agrawal',
            'SANSHI FUND-I',
            'PARAM CAPITAL',
            'Asha Mukul Agrawal',
        ],
        "screener_urls": [
            "https://www.screener.in/people/127829/mukul-mahavir-agrawal/",
            "https://www.screener.in/people/6066/asha-mukul-agrawal/",
            "https://www.screener.in/people/168570/sanshi-fund-i/",
            "https://www.screener.in/people/98486/ms-param-capital/",
        ],
    },
    # ── Vanaja Sundar Iyer ───────────────────────────────────────────────
    {
        "id": "vanaja_sundar_iyer",
        "display_name": "Vanaja Sundar Iyer",
        "bulk_deal_names": [
            'VANAJA SUNDAR IYER',
        ],
        "screener_urls": [
            "https://www.screener.in/people/392/vanjana-sundar-iyer/",
        ],
    },
    # ── Venkata Nagaraju Padala ───────────────────────────────────────────
    {
        "id": "venkata_nagaraju",
        "display_name": "Venkata Nagaraju Padala",
        "bulk_deal_names": [
            'VENKATA NAGARAJU PADALA',
        ],
        "screener_urls": [
            "https://www.screener.in/people/123054/venkata-nagaraju-padala/",
        ],
    },
    # ── Ritu Bapna ───────────────────────────────────────────────────────
    {
        "id": "ritu_bapna",
        "display_name": "Ritu Bapna",
        "bulk_deal_names": [
            'RITU BAPNA',
        ],
        "screener_urls": [
            "https://www.screener.in/people/180470/ritu-bapna/",
        ],
    },
    # ── Sandeep Singh ────────────────────────────────────────────────────
    {
        "id": "sandeep_singh",
        "display_name": "Sandeep Singh",
        "bulk_deal_names": [
            'SANDEEP SINGH',
            'SANDEEP  SINGH',
        ],
        "screener_urls": [
            "https://www.screener.in/people/23593/sandeep-singh/",
        ],
    },
    # ── Vinod Kumar ──────────────────────────────────────────────────────
    {
        "id": "vinod_kumar",
        "display_name": "Vinod Kumar",
        "bulk_deal_names": [
            'VINOD  KUMAR',
        ],
        "screener_urls": [],
    },
    # ── Valuequest ───────────────────────────────────────────────────────
    {
        "id": "valuequest",
        "display_name": "Valuequest",
        "bulk_deal_names": [
            'Valuequest S C A L E Fund',
            'VQ FASTERCAP FUND',
            'VALUEQUEST INVESTMENT ADVISORS PVT LTD',
        ],
        "screener_urls": [
            "https://www.screener.in/people/162189/vq-fastercap-fund/",
            "https://www.screener.in/people/141932/valuequest-s-c-a-l-e-fund/",
        ],
    },

    # ── Funds / entities tracked in BulkBlock only (no Screener.in page) ─

    # ── Nav Capital ──────────────────────────────────────────────────────
    {
        "id": "nav_capital",
        "display_name": "Nav Capital",
        "bulk_deal_names": [
            'NAV CAPITAL VCC - NAV CAPITAL EMERGING STAR FUND',
            'Nav Capital Vcc - Nav Capital Emerging Star Fund',
        ],
        "screener_urls": [],
    },
    # ── Rajasthan Global Securities ──────────────────────────────────────
    {
        "id": "rajasthan_global",
        "display_name": "Rajasthan Global Securities",
        "bulk_deal_names": [
            'RAJASTHAN GLOBAL SECURITIES PRIVATE LIMITED',
            'RAJASTHAN GLOBAL SECURITIES PVT LTD',
            'RAJASTHAN SECURITIES LIMITED',
            'RAJASTHAN GLOBAL SECURITIES PVT.LTD',
            'Rajasthan Global Securities Private Limited',
        ],
        "screener_urls": [],
    },
    # ── Finavenue ────────────────────────────────────────────────────────
    {
        "id": "finavenue",
        "display_name": "Finavenue",
        "bulk_deal_names": [
            'FINAVENUE GROWTH FUND',
            'FINAVENUE CAPITAL TRUST-FINAVENUE GROWTH FUND',
            'Finavenue Capital Trust - Finavenue Growth Fund',
            'Finavenue Capital trust Finavenue Growth Fund',
            'Finavenue Capital Trust - Finavenue Strategic Fund',
        ],
        "screener_urls": [],
    },
    # ── Saint Capital Fund ───────────────────────────────────────────────
    {
        "id": "saint_capital",
        "display_name": "Saint Capital Fund",
        "bulk_deal_names": [
            'SAINT CAPITAL FUND',
            'Saint Capital Fund',
        ],
        "screener_urls": [],
    },
    # ── Meru Investment ──────────────────────────────────────────────────
    {
        "id": "meru_investment",
        "display_name": "Meru Investment",
        "bulk_deal_names": [
            'MERU INVESTMENTS',
            'MERU INVESTMENT FUND PCC- CELL 1',
            'MERU INVESTMENT FUND PCC-CELL 1',
            'Meru Investment Fund PCC-Cell 1',
            'MERU INVESTMENT FUND PCC - CELL 1',
            'MERU INVESTMENT FUND',
        ],
        "screener_urls": [],
    },
    # ── Vikasa ───────────────────────────────────────────────────────────
    {
        "id": "vikasa",
        "display_name": "Vikasa",
        "bulk_deal_names": [
            'VIKASA INDIA EIF I FUND',
            'VIKASA INDIA EIF I FUND - SHARE CLASS P',
            'VIKASA INDIA EIF I FUND-INCUBE GLOBAL OPPORTUNITIES',
            'Vikasa India EIF I Fund - Incube Global Opportunities',
            'VIKASA INDIA EIF I FUND - INCUBE GLOBAL OPPORTUNITIES',
            'Vikasa India EIF I Fund- Share ClassP',
            'Vikasa India EIF I Fund - Share Class P',
            'Vikasa Global Fund PCC - Eubilia Capital Partners Fund - I',
            'VIKASA INDIA EIF I FUND - pte OPPORTUNITIES',
            'VIKASA CAPITAL INC',
            'VIKASA INDIA EIF I FUND-SHARE CLASS P',
        ],
        "screener_urls": [],
    },
    # ── LRSD Securities ──────────────────────────────────────────────────
    {
        "id": "lrsd_securities",
        "display_name": "LRSD Securities",
        "bulk_deal_names": [
            'LRSD SECURITIES PRIVATE LIMITED',
            'LRSD SECURITIES PVT.LTD',
            'LRSD SECURITIES PVT LTD',
        ],
        "screener_urls": [],
    },
    # ── Tiger Strategies ─────────────────────────────────────────────────
    {
        "id": "tiger_strategies",
        "display_name": "Tiger Strategies",
        "bulk_deal_names": [
            'TIGER STRATEGIES FUND -I',
            'TIGER STRATEGIES FUND - 1',
            'Tiger Strategies Fund-I',
            'Tiger Strategies Fund - I',
        ],
        "screener_urls": [],
    },
    # ── Evergrow Capital ─────────────────────────────────────────────────
    {
        "id": "evergrow_capital",
        "display_name": "Evergrow Capital",
        "bulk_deal_names": [
            'EVERGROW CAPITAL OPPORTUNITIES FUND',
            'Evergrow Capital Opportunities Fund',
        ],
        "screener_urls": [],
    },
    # ── SageOne ──────────────────────────────────────────────────────────
    {
        "id": "sageone",
        "display_name": "SageOne",
        "bulk_deal_names": [
            'SAGEONE - FLAGSHIP GROWTH 2 FUND',
            'SAGEONE - FLAGSHIP GROWTH OE FUND',
            'SAGEONE FLAGSHIP GROWTH 2 FUND',
            'SAGEONE INVESTMENT MANAGERS LLP',
            'SAGEONE-FLAGSHIP GROWTH OE FUND',
            'Sageone - Flagship Growth OE Fund',
            'SageOne India Opportunity Trust',
        ],
        "screener_urls": [],
    },
    # ── Mint Focused Growth ──────────────────────────────────────────────
    {
        "id": "mint_focused_growth",
        "display_name": "Mint Focused Growth",
        "bulk_deal_names": [
            'MINT FOCUSED GROWTH FUND PCC- CELL 1',
            'Mint Focused Growth Fund PCC- CELL 1',
            'Mint Focused Growth Fund-PCC Cell 1',
            'Mint Focused Growth Fund PCC- Cell',
            'MINT FOCUSED GROWTH FUND',
        ],
        "screener_urls": [],
    },
    # ── Religo ───────────────────────────────────────────────────────────
    {
        "id": "religo",
        "display_name": "Religo",
        "bulk_deal_names": [
            'RELIGO CAPITAL ADVISORS PRIVATE LIMITED',
            'RELIGO COMMODITIES VENTURES FUND',
            'Religo Commodities Venture Trust-Religo Commodities Ventures Fund',
            'RELIGO COMMODITIES VENTURES TRUST - RELIGO COMMODITIES VENTURES FUND',
            'Religo Commeadition Ventures Trust Religo Commodities Ventures Fund',
        ],
        "screener_urls": [],
    },
    # ── HEM Growth ───────────────────────────────────────────────────────
    {
        "id": "hem_growth",
        "display_name": "HEM Growth",
        "bulk_deal_names": [
            'Hem Growth Opportunities Fund',
            'HEM GROWTH OPPORTUNITIES FUND',
        ],
        "screener_urls": [],
    },
    # ── RGSL ─────────────────────────────────────────────────────────────
    {
        "id": "rgsl",
        "display_name": "RGSL",
        "bulk_deal_names": [
            'RGSL INVESTMENT LVF 1',
            'RGSL INVESTMENT FUND - RGSL INVESTMENT LVF 1',
            'RGSL INFRA PRIVATE LIMITED',
        ],
        "screener_urls": [],
    },

    # ── Investors tracked in fii_stake_tracker only (no bulk deal names) ─

    # ── Steadview Capital ────────────────────────────────────────────────
    {
        "id": "steadview_capital",
        "display_name": "Steadview Capital",
        "bulk_deal_names": [],
        "screener_urls": [
            "https://www.screener.in/people/21426/steadview-capital-mauritius-limited/",
        ],
    },
    # ── Nibe Ganesh Ramesh ───────────────────────────────────────────────
    {
        "id": "nibe_ganesh_ramesh",
        "display_name": "Nibe Ganesh Ramesh",
        "bulk_deal_names": [],
        "screener_urls": [
            "https://www.screener.in/people/71485/nibe-ganesh-ramesh/",
        ],
    },
    # ── Reina Ra Jaisinghani ─────────────────────────────────────────────
    {
        "id": "reina_jaisinghani",
        "display_name": "Reina Ra Jaisinghani",
        "bulk_deal_names": [],
        "screener_urls": [
            "https://www.screener.in/people/161937/reina-ra-jaisinghani/",
        ],
    },
    # ── Kunjal Lalitkumar Patel ──────────────────────────────────────────
    {
        "id": "kunjal_patel",
        "display_name": "Kunjal Lalitkumar Patel",
        "bulk_deal_names": [],
        "screener_urls": [
            "https://www.screener.in/people/78665/kunjal-lalitkumar-patel/",
        ],
    },
]


# ─── Helper functions (drop-in replacements for inline constants) ─────────

def all_bulk_deal_names():
    """Flat list of every bulk/block deal name variant across all investors.

    Drop-in replacement for BulkBlock.py's inline ``client_names_to_filter``
    list.  The returned list preserves insertion order (registry order, then
    names within each investor) — this mirrors the original inline list.
    """
    return [name for inv in INVESTOR_REGISTRY for name in inv["bulk_deal_names"]]


def all_screener_urls():
    """Flat list of every Screener.in /people/ URL across all investors.

    Drop-in replacement for fii_stake_tracker.py's ``HNI_PEOPLE_URLS`` list.
    Preserves insertion order.
    """
    return [url for inv in INVESTOR_REGISTRY for url in inv["screener_urls"]]
