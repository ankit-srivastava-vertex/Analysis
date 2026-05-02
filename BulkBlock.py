"""
Bulk & Block Deals Scraper (NSE + BSE)
=======================================

SUMMARY
-------
Fetches today's bulk and block deals from both NSE and BSE, optionally
filters by "superstar" client names, exports to Excel, and sends an
email report with styled HTML preview.

WORKFLOW
--------
1. Fetch NSE bulk + block deals via nsepython library
   (NSE API: /api/snapshot-capital-market-largedeal).
2. Fetch BSE bulk + block deals via BSE website scraping + JSON API
   (https://www.bseindia.com, https://api.bseindia.com).
3. Parse and normalise deal data from both exchanges.
4. Optionally filter deals by a hardcoded list of superstar client names.
5. Save all deals to Excel with separate sheets:
   NSE Bulk, NSE Block, BSE Bulk, BSE Block.
6. Generate styled HTML email preview table.
7. Send email with Excel attachment via SMTP.

DATA SOURCES
------------
- NSE API (via nsepython)  — /api/snapshot-capital-market-largedeal
                              BULK_DEALS_DATA + BLOCK_DEALS_DATA
- BSE Website              — https://www.bseindia.com/markets/equity/EQReports/bulk_deals.aspx
- BSE JSON API             — https://api.bseindia.com/BseIndiaAPI/api/BulkDeal_Beta/w

OUTPUT
------
- BULK_BLOCK_Deals_<timestamp>.xlsx — Multi-sheet Excel (NSE Bulk, NSE Block, BSE Bulk, BSE Block)
- HTML email with styled deal tables

USAGE
-----
Individual run:
    python3 BulkBlock.py           # scrape deals, save Excel, send email

Group run (via run_all.py):
    Not part of run_all.py — run independently.

DEPENDENCIES
------------
nsepython (nsepythonserver), requests, BeautifulSoup (bs4), pandas, openpyxl, smtplib
"""

import os
import sys
import time
import traceback
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
try:
    from nsepython import nsefetch
except Exception:
    # Provide a graceful fallback if nsepythonserver is not installed
    def nsefetch(url):
        print(f"Warning: 'nsepythonserver' not available; returning empty payload for {url}")
        return {"BULK_DEALS_DATA": [], "BLOCK_DEALS_DATA": []}
import requests
from bs4 import BeautifulSoup
import pandas as pd
from datetime import datetime


class BSEScraper:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Cache-Control': 'max-age=0',
        })

    def scrape_bulk_deals(self, url, table_name):
        """Scrape BULK deals - using multiple parsing methods"""
        try:
            print(f"\n{'='*100}")
            print(f"Scraping: {table_name}")
            print(f"URL: {url}")
            print(f"{'='*100}\n")

            response = self.session.get(url, timeout=30)
            response.raise_for_status()

            print(f"✓ Response Status: {response.status_code}")
            print(f"✓ Content Length: {len(response.content)} bytes")

            soup = BeautifulSoup(response.content, 'html.parser')

            # Method 1: Try to find table with ID
            table = soup.find('table', id='ContentPlaceHolder1_GridView1')

            if table:
                print("✓ Found table by ID: ContentPlaceHolder1_GridView1")
                df = self._parse_bulk_html_table(table)
                if df is not None and not df.empty:
                    return df

            # Method 2: Try to find any table with class
            tables = soup.find_all('table')
            print(f"✓ Found {len(tables)} table(s) in HTML")

            for idx, tbl in enumerate(tables):
                rows = tbl.find_all('tr')
                if len(rows) > 5:
                    print(f"  Trying table {idx+1} with {len(rows)} rows")
                    df = self._parse_bulk_html_table(tbl)
                    if df is not None and not df.empty:
                        return df

            # Method 3: Parse pipe-delimited text
            print("✓ Trying pipe-delimited text parsing...")
            text = soup.get_text()
            lines = text.split('\n')

            data_lines = []
            for line in lines:
                if '|' in line and line.strip():
                    pipe_count = line.count('|')
                    if pipe_count >= 6:
                        data_lines.append(line.strip())

            if data_lines:
                print(f"✓ Found {len(data_lines)} lines with pipe delimiters")
                return self._parse_pipe_delimited_bulk(data_lines)

            print(f"⚠️  Could not extract table data for {table_name}")
            return None

        except Exception as e:
            print(f"❌ Error scraping {table_name}: {e}")
            traceback.print_exc()
            return None

    def _parse_bulk_html_table(self, table):
        """Parse HTML table for BULK deals"""
        try:
            rows = table.find_all('tr')
            if not rows:
                return None

            # Extract headers
            headers = []
            header_row = rows[0]
            for th in header_row.find_all(['th', 'td']):
                header_text = th.get_text(strip=True)
                if header_text:
                    headers.append(header_text)

            # Extract data
            data = []
            for row in rows[1:]:
                cols = row.find_all('td')
                if cols:
                    row_data = [col.get_text(strip=True) for col in cols]
                    if any(row_data):
                        data.append(row_data)

            if not data:
                return None

            df = pd.DataFrame(data, columns=headers if headers else None)
            df = df.loc[:, (df != '').any(axis=0)]

            # Normalize column names
            df.columns = [col.replace('Price **', 'Price') for col in df.columns]

            print(f"✓ Parsed {len(df)} rows with {len(df.columns)} columns")
            return df

        except Exception as e:
            print(f"Error parsing HTML table: {e}")
            traceback.print_exc()
            return None

    def _parse_pipe_delimited_bulk(self, lines):
        """Parse pipe-delimited text for BULK deals"""
        try:
            data_rows = []
            headers = None

            for line in lines:
                parts = [p.strip() for p in line.split('|') if p.strip()]

                if not parts:
                    continue

                if headers is None:
                    headers = parts
                    # Normalize column names
                    headers = [h.replace('Price **', 'Price') for h in headers]
                    print(f"✓ Headers: {headers}")
                else:
                    if len(parts) == len(headers):
                        data_rows.append(parts)

            if not data_rows:
                return None

            df = pd.DataFrame(data_rows, columns=headers)
            print(f"✓ Parsed {len(df)} rows with {len(df.columns)} columns")
            return df

        except Exception as e:
            print(f"Error parsing pipe-delimited data: {e}")
            traceback.print_exc()
            return None

    def scrape_block_deals(self, url, table_name):
        """Scrape BLOCK deals - using HTML table parser"""
        try:
            print(f"\n{'='*100}")
            print(f"Scraping: {table_name}")
            print(f"URL: {url}")
            print(f"{'='*100}\n")

            response = self.session.get(url, timeout=30)
            response.raise_for_status()

            print(f"✓ Response Status: {response.status_code}")
            print(f"✓ Content Length: {len(response.content)} bytes")

            soup = BeautifulSoup(response.content, 'html.parser')

            # Find table by ID
            table = soup.find('table', id='ContentPlaceHolder1_gvblock_deals')

            if table:
                print(f"✓ Found table by ID: ContentPlaceHolder1_gvblock_deals")
                return self._parse_block_html_table(table)

            print(f"⚠️  Could not find table for {table_name}")
            return None

        except Exception as e:
            print(f"❌ Error scraping {table_name}: {e}")
            traceback.print_exc()
            return None

    def _parse_block_html_table(self, table):
        """Parse HTML table for BLOCK deals"""
        try:
            rows = table.find_all('tr')
            if not rows:
                return None

            # Extract headers from row with class "TTHeader"
            headers = []
            header_row = None
            for row in rows:
                if 'TTHeader' in row.get('class', []):
                    header_row = row
                    break

            if not header_row:
                header_row = rows[0]

            for th in header_row.find_all(['th', 'td']):
                header_text = th.get_text(strip=True)
                if header_text:
                    headers.append(header_text)

            # Extract data rows with class "TTRow"
            data = []
            for row in rows:
                if 'TTRow' in row.get('class', []):
                    cols = row.find_all('td')
                    if cols:
                        row_data = [col.get_text(strip=True) for col in cols]
                        if row_data and any(row_data):
                            data.append(row_data)

            if not data:
                return None

            df = pd.DataFrame(data, columns=headers)

            # Normalize column names
            df.columns = [col.replace('Trade Price', 'Price') for col in df.columns]

            print(f"✓ Parsed {len(df)} rows with {len(df.columns)} columns")
            print(f"✓ Columns: {list(df.columns)}")
            return df

        except Exception as e:
            print(f"❌ Error parsing HTML table: {e}")
            traceback.print_exc()
            return None

    def nse_largedeals(self, mode="bulk_deals"):
        """This function fetches both bulk and block deals from the NSE API endpoint"""
        payload = nsefetch('https://www.nseindia.com/api/snapshot-capital-market-largedeal')
        if mode == "bulk_deals":
            return pd.DataFrame(payload["BULK_DEALS_DATA"])
        if mode == "block_deals":
            return pd.DataFrame(payload["BLOCK_DEALS_DATA"])
        return None

    def fetch_bse_deals_api(self, deal_type="bulk"):
        """Fetch BSE bulk/block deals from the working API endpoints.
        deal_type: 'bulk' or 'block'
        """
        api_map = {
            "bulk": "https://api.bseindia.com/BseIndiaAPI/api/BulkDeal_Beta/w",
            "block": "https://api.bseindia.com/BseIndiaAPI/api/BlockDeal_Beta/w",
        }
        url = api_map.get(deal_type)
        label = f"BSE {deal_type.title()} Deals"
        try:
            print(f"\n{'='*100}")
            print(f"Fetching: {label} (API)")
            print(f"URL: {url}")
            print(f"{'='*100}\n")

            r = self.session.get(url, timeout=15, headers={
                'Accept': 'application/json, text/plain, */*',
                'Referer': 'https://www.bseindia.com/markets/equity/EQReports/bulk_deals.aspx',
                'Origin': 'https://www.bseindia.com',
            })
            r.raise_for_status()
            data = r.json()
            rows = data.get("Table", [])
            if not rows:
                print(f"\u26a0\ufe0f  No data returned for {label}")
                return None

            df = pd.DataFrame(rows)
            # Rename columns to match the filter expectations
            col_map = {
                "DEAL_DATE": "Deal Date",
                "SCRIP_CODE": "Scrip Code",
                "ScripName": "Scrip Name",
                "CLIENT_NAME": "Client Name",
                "TRANSACTION_TYPE": "Buy/Sell",
                "QUANTITY": "Quantity",
                "PRICE": "Price",
            }
            df.rename(columns=col_map, inplace=True)
            # Drop internal columns if present
            df.drop(columns=["SENDTOWEBSITE"], errors="ignore", inplace=True)
            print(f"\u2713 Fetched {len(df)} {deal_type} deals from BSE API")
            print(f"\u2713 Columns: {list(df.columns)}")
            return df

        except Exception as e:
            print(f"\u274c Error fetching {label}: {e}")
            traceback.print_exc()
            return None

    def save_to_excel(self, dataframes_dict, filename):
        """Save all dataframes to Excel with multiple sheets"""
        try:
            print(f"\n{'='*100}")
            print(f"Saving data to Excel file: {filename}")
            print(f"{'='*100}\n")

            with pd.ExcelWriter(filename, engine='openpyxl') as writer:
                for sheet_name, df in dataframes_dict.items():
                    clean_sheet_name = sheet_name.replace('BSE ', '')[:31]
                    df.to_excel(writer, sheet_name=clean_sheet_name, index=False)
                    print(f"✓ Sheet '{clean_sheet_name}': {len(df)} rows saved")

            print(f"\n{'='*100}")
            print(f"✓ Excel file saved successfully: {filename}")
            print(f"{'='*100}\n")

        except Exception as e:
            print(f"❌ Error saving to Excel: {e}")
            traceback.print_exc()

    def run(self):
        """Main execution method"""
        # Download NSE bulk deals data for the latest day
        nse_bulk_deals_df = self.nse_largedeals(mode="bulk_deals")

        # Download NSE block deals data
        nse_block_deals_df = self.nse_largedeals(mode="block_deals")

        # Sample list of superstar names to filter in bulk and block deals
        client_names_to_filter = ['ABAKKUS ASSET MANAGER LLP',
'ABAKKUS ASSET MANAGER LLP(HDFC CUSTODY)',
'ABAKKUS ASSET MANAGER PRIVATE LIMITED',
'ABAKKUS DIVERSIFIED ALPHA FUND',
'ABAKKUS DIVERSIFIED ALPHA FUND-2',
'ABAKKUS EMERGING OPPORTUNITIES FUND - 1',
'ABAKKUS GROWTH FUND - 1',
'ABAKKUS GROWTH FUND-2',
'AJAY KUMAR AGGARWAL',
'AJAY UPADHYAYA',
'UPADHYAYA AJAY',
'UPADHYAYA AJAY SHIV NARAYAN',
'AKASH BHANSHALI',
'Ankit Vijay Kedia',
'Vijay Krishanlal Kedia',
'Kedia Secuirities Private Limited',
'ANKUSH  KEDIA',
'ANKUSH KEDIA',
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
'Suresh Kumar Agarwal',
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
'INDIA EQUITY FUND 1',
'MADHURI MADHUSUDAN KELA',
'COHESION MK BEST IDEAS SUB-TRUST',
'FOUNDERS COLLECTIVE FUND',
'SINGULARITY EQUITY FUND I',
'SINGULARITY LARGE VALUE FUND II',
'SINGULARITY LARGE VALUE FUND III',
'Chartered Finance & Leasing Limited',
'Madhusudan Murlidhar Kela',
'LAROIA MONA',
'MONA LAROIA',
'BIJAL PRITESH VORA',
'MALABAR INDIA FUND LIMITED',
'MASSACHUSETTS INSTITUTE OF TECHNOLOGY',
'MANISH GROVER', #Jeena Sikho promoter
'ROHAN GUPTA', #SG Finserve promoter
'NALANDA INDIA EQUITY FUND LIMITED',
'NALANDA INDIA FUND LIMITED',
'OXBOW MASTER FUND LIMITED',
'QRG INVESTMENTS AND HOLDINGS LIMITED',
'RITU BAPNA',
'SANDEEP SINGH',
'Sandeep Kapadia',
'KAPADIA SANDEEP',
'Mukul Mahavir Agrawal',
'SANSHI FUND-I',
'PARAM CAPITAL',
'Asha Mukul Agrawal',
'SANDEEP KAPADIA',
'SBI LIFE INSURANCE COMPANY LIMITED',
'SBI LIFE INSURANCE COMPANY LTD',
'SHALU  AGGARWAL',
'SIXTEENTH STREET ASIAN GEMS FUND',
'SMALL CAP WORLD FUND INC',
'SMALLCAP WORLD FUND INC',
'SMALLCAP WORLD FUND INC.',
'SMALLCAPWORLD FUND INC',
'SMALLER CAP WORLD FUND INC',
'STEADVIEW CAPITAL MASTER FUND LTD.',
'STEADVIEW CAPITAL MAURITIUS LIMITED',
'STEADVIEW CAPITAL OPPORTUNITIES PCC',
'VANAJA SUNDAR IYER',
'VENKATA NAGARAJU PADALA',
'VINOD  KUMAR',
'Valuequest S C A L E Fund',
'VQ FASTERCAP FUND'
        ]

        # Check for potential hidden spaces in column names
        nse_bulk_deals_df.columns = nse_bulk_deals_df.columns.str.strip()
        nse_block_deals_df.columns = nse_block_deals_df.columns.str.strip()
        # Filtering the DataFrame based on the "Client Name" column for NSE data only
        filtered_nse_bulk_df = nse_bulk_deals_df[nse_bulk_deals_df['clientName'].isin(client_names_to_filter)]
        filtered_nse_block_df = nse_block_deals_df[nse_block_deals_df['clientName'].isin(client_names_to_filter)]

        dataframes = {"nse_bulk": filtered_nse_bulk_df,
                      "nse_block": filtered_nse_block_df}

        # Fetch BSE BULK DEALS via API
        bulk_name = 'BSE Bulk Deals'
        bulk_df = self.fetch_bse_deals_api("bulk")

        if bulk_df is not None and not bulk_df.empty:
            bulk_df.columns = bulk_df.columns.str.strip()
            filtered_bulk_df = bulk_df[bulk_df['Client Name'].isin(client_names_to_filter)]
            dataframes[bulk_name] = filtered_bulk_df
        else:
            print(f"⚠️  No data fetched for {bulk_name}")

        time.sleep(1)

        # Fetch BSE BLOCK DEALS via API
        block_name = 'BSE Block Deals'
        block_df = self.fetch_bse_deals_api("block")

        if block_df is not None and not block_df.empty:
            block_df.columns = block_df.columns.str.strip()
            filtered_block_df = block_df[block_df['Client Name'].isin(client_names_to_filter)]
            dataframes[block_name] = filtered_block_df
        else:
            print(f"⚠️  No data fetched for {block_name}")

        # Save to Excel
        if dataframes:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f"BULK_BLOCK_Deals_{timestamp}.xlsx"
            self.save_to_excel(dataframes, filename)
        else:
            print("\n❌ No data was scraped from any endpoint.")
            print("="*100 + "\n")


class BSEScraperWithEmail(BSEScraper):
    """Extends BSEScraper to add email reporting after scraping."""

    def __init__(self, email_config=None):
        super().__init__()
        self._email_config = email_config or self._load_config_from_env()
        self._saved_dataframes = {}
        self._saved_filename = None
        self._dry_run = '--dry-run' in sys.argv

    @staticmethod
    def _load_config_from_env():
        to_addrs = [a.strip() for a in os.environ.get('EMAIL_TO', '').split(',') if a.strip()]
        # Prefer a daily-specific secret name if provided, otherwise fall back
        subject_prefix = os.environ.get('EMAIL_SUBJECT_PREFIX_DAILY') or os.environ.get('EMAIL_SUBJECT_PREFIX', 'Bulk & Block Deals Report')
        return {
            'smtp_server': os.environ.get('EMAIL_SMTP_SERVER', 'smtp.gmail.com'),
            'smtp_port': int(os.environ.get('EMAIL_SMTP_PORT', '587')),
            'from_addr': os.environ.get('EMAIL_FROM', ''),
            'to_addrs': to_addrs,
            'username': os.environ.get('EMAIL_USERNAME', ''),
            'password': os.environ.get('EMAIL_PASSWORD', ''),
            'use_tls': os.environ.get('EMAIL_USE_TLS', 'true').lower() != 'false',
            'subject_prefix': subject_prefix,
        }

    def save_to_excel(self, dataframes_dict, filename):
        self._saved_dataframes = dict(dataframes_dict)
        self._saved_filename = filename
        super().save_to_excel(dataframes_dict, filename)

    def run(self):
        super().run()
        if not self._saved_dataframes:
            print("\nNo data available for email report.")
            return
        if self._dry_run:
            self._generate_preview()
            return
        self.send_email()

    def _build_html_body(self):
        date_str = datetime.now().strftime('%d-%b-%Y %H:%M')
        total_deals = sum(len(df) for df in self._saved_dataframes.values() if df is not None and not df.empty)

        parts = [f"""
<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
body {{ font-family: Calibri, Arial, sans-serif; margin: 20px; color: #333; }}
h1 {{ color: #1F4E79; font-size: 22px; border-bottom: 2px solid #1F4E79; padding-bottom: 8px; }}
h2 {{ color: #2E75B6; font-size: 16px; margin-top: 25px; }}
.summary {{ background: #F2F7FB; padding: 12px 16px; border-left: 4px solid #2E75B6; margin-bottom: 20px; font-size: 13px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 20px; font-size: 12px; }}
th {{ background-color: #2E75B6; color: #FFFFFF; padding: 8px 10px; text-align: left; font-weight: 600; border: 1px solid #2068A0; }}
td {{ padding: 6px 10px; border: 1px solid #D6D6D6; }}
tr:nth-child(even) {{ background-color: #F2F2F2; }}
tr:hover {{ background-color: #E8F0FE; }}
.no-data {{ color: #999; font-style: italic; padding: 10px 0; }}
.badge {{ display: inline-block; background: #2E75B6; color: white; padding: 2px 8px; border-radius: 3px; font-size: 11px; margin-left: 8px; }}
.footer {{ margin-top: 30px; font-size: 11px; color: #888; border-top: 1px solid #ddd; padding-top: 10px; }}
</style>
</head>
<body>
<h1>Bulk &amp; Block Deals Report</h1>
<div class="summary">
<strong>Report Date:</strong> {date_str}<br>
<strong>Total Filtered Deals:</strong> {total_deals}
</div>
"""]

        for sheet_name, df in self._saved_dataframes.items():
            row_count = len(df) if df is not None and not df.empty else 0
            parts.append(f"<h2>{sheet_name} <span class=\"badge\">{row_count} deal(s)</span></h2>")
            if df is not None and not df.empty:
                parts.append(df.to_html(index=False, border=0, na_rep='-'))
            else:
                parts.append('<p class="no-data">No matching deals found for this category.</p>')

        parts.append(f"""
<div class="footer">
<p>This is an automated report. The Excel file is attached for reference.</p>
<p>Attachment: {os.path.basename(self._saved_filename) if self._saved_filename else 'N/A'}</p>
</div>
</body>
</html>
""")

        return '\n'.join(parts)

    def send_email(self):
        config = self._email_config
        required_keys = ['from_addr', 'to_addrs', 'username', 'password']
        missing = [k for k in required_keys if not config.get(k)]
        if missing:
            print(f"\nX Email not sent. Missing configuration: {', '.join(missing)}")
            print("Set environment variables: EMAIL_FROM, EMAIL_TO, EMAIL_USERNAME, EMAIL_PASSWORD")
            return False

        try:
            print(f"\n{'='*100}")
            print("Sending email report ...")
            print(f"{'='*100}\n")

            msg = MIMEMultipart('mixed')
            msg['From'] = config['from_addr']
            to_list = config['to_addrs']
            msg['To'] = ', '.join(to_list)
            msg['Subject'] = f"{config.get('subject_prefix', 'Bulk & Block Deals Report')} - {datetime.now().strftime('%d-%b-%Y')}"

            html_body = self._build_html_body()
            msg.attach(MIMEText(html_body, 'html', 'utf-8'))

            if self._saved_filename and os.path.exists(self._saved_filename):
                with open(self._saved_filename, 'rb') as fh:
                    part = MIMEBase('application', 'vnd.openxmlformats-officedocument.spreadsheetml.sheet')
                    part.set_payload(fh.read())
                    encoders.encode_base64(part)
                    part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(self._saved_filename)}"')
                    msg.attach(part)
                    print(f"i Attached: {self._saved_filename}")

            with smtplib.SMTP(config['smtp_server'], config['smtp_port']) as server:
                server.ehlo()
                if config.get('use_tls', True):
                    server.starttls()
                    server.ehlo()
                server.login(config['username'], config['password'])
                server.send_message(msg)

            print(f"i Email sent to: {', '.join(to_list)}")
            print('='*100)
            return True

        except smtplib.SMTPAuthenticationError:
            print("Email authentication failed. Check username/password.")
            print("For Gmail: use an App Password (not your regular password).")
            return False
        except smtplib.SMTPException as exc:
            print(f"SMTP error: {exc}")
            return False
        except Exception as exc:
            print(f"Error sending email: {exc}")
            traceback.print_exc()
            return False

    def _generate_preview(self):
        html = self._build_html_body()
        preview_file = 'email_preview.html'
        with open(preview_file, 'w', encoding='utf-8') as fh:
            fh.write(html)

        print('\n' + '='*80)
        print('DRY RUN - Email preview generated (not sent)')
        print('='*80)
        print(f" Subject    : {self._email_config.get('subject_prefix', 'Report')} - {datetime.now().strftime('%d-%b-%Y')}")
        print(f" Attachment : {self._saved_filename}")
        print(f" HTML preview : {os.path.abspath(preview_file)}")
        print(f" Body length : {len(html)} chars")
        for name, df in self._saved_dataframes.items():
            rows = len(df) if df is not None and not df.empty else 0
            print(f" • {name}: {rows} row(s)")
        print('='*80 + '\n')


if __name__ == '__main__':
    scraper = BSEScraperWithEmail()
    scraper.run()