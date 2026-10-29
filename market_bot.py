#!/usr/bin/env python3
"""
Market Brief -> Telegram
------------------------
Sends FX + equity index levels, then themed Reuters / Bloomberg headlines
(as links you can open with your subscriptions).

Usage:
  python market_bot.py              # build the brief and send it to Telegram
  python market_bot.py --preview    # print the brief in the terminal, don't send
  python market_bot.py --chat-id    # show your Telegram chat id (setup helper)

Token and chat id are read from the environment variables TELEGRAM_TOKEN and
CHAT_ID (how GitHub Actions passes them in). For running on your own laptop
you can instead paste them into the two lines below.
"""

import argparse
import calendar
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
import traceback
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import pandas as pd
import requests
import yfinance as yf

# ============================ CONFIG (edit me) ============================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "PASTE_YOUR_BOT_TOKEN_HERE"
CHAT_ID = os.environ.get("CHAT_ID") or "PASTE_YOUR_CHAT_ID_HERE"

TIMEZONE = "Asia/Singapore"

# Display name -> Yahoo Finance ticker. If one shows "n/a", look the pair up
# on finance.yahoo.com and paste the exact ticker it uses.
FX = {
    "DXY":     "DX-Y.NYB",   # US dollar index
    "USD/JPY": "USDJPY=X",
    "AUD/USD": "AUDUSD=X",
    "NZD/USD": "NZDUSD=X",
    "AUD/NZD": "AUDNZD=X",
    "USD/CNH": "CNH=X",
    "USD/BRL": "USDBRL=X",
    "USD/MXN": "USDMXN=X",
    "BRL/MXN": "BRLMXN=X",
    "EUR/USD": "EURUSD=X",
}

# Tickers tried, in order, if the main one above returns nothing.
FALLBACK_TICKERS = {
    "USD/CNH": ["USDCNH=X", "CNH=F"],
}

INDICES = {
    "S&P 500":    "^GSPC",
    "Nasdaq":     "^IXIC",
    "Chips SOX":  "^SOX",    # Philly semiconductor index: proxy for the AI trade
    "Dow":        "^DJI",
    "Euro Stoxx": "^STOXX50E",
    "DAX":        "^GDAXI",
    "FTSE 100":   "^FTSE",
    "Nikkei":     "^N225",
    "Hang Seng":  "^HSI",
    "Shanghai":   "000001.SS",
    "ASX 200":    "^AXJO",
    "NZX 50":     "^NZ50",
    "STI":        "^STI",
    "Bovespa":    "^BVSP",
    "Mexico IPC": "^MXX",
    "VIX":        "^VIX",
}

# Government bond yields (%).
#   "FRED:<series>" = official daily US Treasury yields from the St. Louis Fed
#                     (previous US close; ~1 day lag)
#   "MOF:<tenor>"   = Japan Ministry of Finance daily JGB yields (end of Tokyo
#                     day, published ~9:30am Tokyo the next business day)
#   anything else   = Yahoo Finance ticker
RATES = {
    "UST 2Y":  "FRED:DGS2",
    "UST 10Y": "^TNX",
    "JGB 2Y":  "MOF:2Y",
    "JGB 10Y": "MOF:10Y",
}
# Spreads shown in basis points: name -> (long leg, short leg)
SPREADS = {
    "UST-JGB 2Y":  ("UST 2Y", "JGB 2Y"),    # Fed vs BOJ expectations
    "UST-JGB 10Y": ("UST 10Y", "JGB 10Y"),
}

# ---- Policy rates (update by hand after each decision) --------------------
# No free feed covers all of these, so this is a manual table. The brief
# warns you when POLICY_RATES_AS_OF is more than POLICY_RATES_STALE_DAYS old.
POLICY_RATES = {
    "Fed":     "3.75-4.00%",
    "BOJ":     "1.25%",
    "RBA":     "4.60%",
    "RBNZ":    "2.75%",
    "Selic":   "13.75%",
    "Banxico": "6.50%",
}
POLICY_RATES_AS_OF = "2026-10-29"
POLICY_RATES_STALE_DAYS = 45

# ---- Upcoming events (update by hand) ---------------------------------------
# (date in the event's own local time, description). The brief lists events
# from today through the next CALENDAR_DAYS days, and warns you when the list
# is about to run out. Central-bank dates are published a year ahead.
CALENDAR_DAYS = 7
CALENDAR = [
    ("2026-10-30", "🇯🇵 BOJ decision"),
    ("2026-11-03", "🇦🇺 RBA decision"),
    ("2026-11-03", "🇺🇸 US midterm elections"),
    ("2026-11-04", "🇧🇷 Copom (Selic) decision"),
    ("2026-11-05", "🇲🇽 Banxico decision"),
    ("2026-11-07", "🇳🇿 NZ general election"),
    ("2026-12-09", "🇳🇿 RBNZ decision"),
    ("2026-12-09", "🇺🇸 FOMC decision (2pm ET = 3am SGT 10 Dec)"),
    ("2026-12-17", "🇲🇽 Banxico decision"),
]
JGB_CSV_URLS = [
    "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/jgbcme.csv",
    "https://www.mof.go.jp/english/policy/jgbs/reference/interest_rate/historical/jgbcme_all.csv",
    "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv",
]

COMMODITIES = {
    "Gold":   "GC=F",
    "Brent":  "BZ=F",
    "WTI":    "CL=F",
    "Copper": "HG=F",
}

# ---- Headline sources ----------------------------------------------------
# 1) Bloomberg's official RSS feeds (direct bloomberg.com links). Every story
#    is matched against each section's `keywords` below.
BLOOMBERG_FEEDS = [
    "https://www.bloomberg.com/feeds/news.rss",
    "https://www.bloomberg.com/feeds/markets/news.rss",
    "https://www.bloomberg.com/feeds/economics/news.rss",
    "https://www.bloomberg.com/feeds/politics/news.rss",
    "https://www.bloomberg.com/feeds/industries/news.rss",
]
# 2) Google News searches, run once per site in GOOGLE_SITES using each
#    section's `queries`. This is the only free route to Reuters headlines.
GOOGLE_SITES = ["reuters.com", "bloomberg.com"]

LOOKBACK_HOURS = 24   # ignore stories older than this

HEADERS = {"User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/130.0 Safari/537.36")}

# ---- Optional: Claude picks the major stories and writes the 1-phrase briefs.
# Add an ANTHROPIC_API_KEY secret on GitHub to turn this on. Without it the bot
# uses keyword matching only, and shows each headline shortened.
CLAUDE_MODEL = "claude-haiku-5-5"
CANDIDATES_PER_SECTION = 20   # newest matches per section sent to Claude

HEADLINE_MAX = 55   # headlines longer than this are cut to their main clause

# ---- News sections ---------------------------------------------------------
# Shown in this order. Each section has three kinds of keywords (matched
# case-insensitively as whole words in the headline and Bloomberg summary):
#   topics:   theme -> CORE keywords. One core match is enough to include a
#             story (e.g. "Fed", "BOJ", "Selic").
#   generic:  words too broad on their own (e.g. "inflation", "peso").
#             They only count if the story also mentions a `context` word.
#   context:  the country / market words (e.g. "Japan"). On their own they
#             never qualify a story, so company news that merely mentions
#             the country is left out.
# Stories are ranked by match strength (core hits in the headline count
# most), then by recency, and the top `max` are shown.
#   queries:  Google News searches (Reuters + Bloomberg) that feed candidates.
#   priority: a story matching several sections goes to the one with the
#             LOWEST number (so "Japan inflation" lands in Japan, not US).
SECTIONS = [
    {"title": "🇺🇸 US", "max": 6, "priority": 5,
     "topics": {
        "Fed decisions, Warsh & Fed independence": ["Fed", "Federal Reserve",
            "FOMC", "Fed chair", "Warsh", "Fed independence", "Powell"],
        "CPI / PCE inflation": ["US CPI", "US inflation", "PCE"],
        "Payrolls & jobs": ["payrolls", "nonfarm", "jobs report", "jobless claims",
            "US unemployment"],
        "Midterms & Congress": ["midterm", "midterms", "House Republicans",
            "House Democrats", "government shutdown", "debt ceiling"],
        "Treasuries & White House policy": ["Treasury yields", "Treasuries",
            "US tariffs", "Bessent"],
     },
     "generic": ["rate cut", "rate cuts", "rate hike", "rate hikes", "inflation",
                 "consumer prices", "jobs", "unemployment", "Congress", "Senate",
                 "tariffs", "election", "economy", "GDP", "yields"],
     "context": ["US", "U.S.", "United States", "American", "Americans", "Trump",
                 "White House", "Washington", "Wall Street"],
     "queries": [
        '"Federal Reserve" OR FOMC OR "Fed chair" OR "Fed rate"',
        '"US CPI" OR "US inflation" OR "PCE inflation"',
        'payrolls OR "jobs report" OR "jobless claims" OR "US unemployment"',
        'midterms OR "midterm elections" OR "government shutdown" OR Congress',
        'Warsh OR "Fed independence" OR "US tariffs"',
        '"Treasury yields" OR Treasuries',
     ]},
    {"title": "🇯🇵 Japan", "max": 6, "priority": 1,
     "topics": {
        "BOJ decisions & BOJ speakers": ["BOJ", "Bank of Japan", "Ueda", "Himino"],
        "BOJ board dissents": ["Takata", "Tamura"],
        "Verbal escalation, rate checks & intervention (MOF)": ["yen",
            "rate check", "rate checks", "Katayama", "Mimura", "excessive moves",
            "speculative moves"],
        "Takaichi fiscal plans, consumption tax & JGBs": ["Takaichi",
            "consumption tax", "JGB", "JGBs", "shunto"],
        "Japanese stocks": ["Nikkei", "Topix", "Japanese stocks", "Japan stocks"],
     },
     "generic": ["inflation", "CPI", "wages", "bonds", "bond", "yields", "budget",
                 "auction", "bond sale",
                 "fiscal", "stimulus", "intervention", "Diet", "election",
                 "economy", "GDP", "rate hike"],
     "context": ["Japan", "Japanese", "Tokyo"],
     # (stocks / shares / equities count as generic for every country section)
     "queries": [
        '"Bank of Japan" OR BOJ OR Ueda',
        'yen ("rate check" OR intervention OR excessive OR speculative OR Mimura OR Katayama OR Bessent)',
        'Japan (CPI OR inflation OR wages)',
        'Takaichi (fiscal OR "consumption tax" OR budget) OR "JGB yields"',
     ]},
    {"title": "🇨🇳 China", "max": 5, "priority": 2,
     "topics": {
        "PBOC, yuan fixing & stimulus": ["PBOC", "People's Bank of China", "yuan",
            "renminbi", "yuan fixing"],
        "China deflation (CPI/PPI)": ["China CPI", "China PPI", "China inflation",
            "China deflation"],
        "Xi, Politburo & US-China": ["Xi Jinping", "Politburo", "Five-Year Plan"],
        "Property, trade, steel & iron ore": ["iron ore", "China property",
            "China exports", "China imports", "China trade", "steel output"],
        "Chinese & Hong Kong stocks": ["CSI 300", "Hang Seng", "Shanghai Composite",
            "China stocks", "Chinese stocks", "Hong Kong stocks", "Chinese tech stocks",
            "China tech stocks", "STAR Market", "Shenzhen"],
     },
     "generic": ["stimulus", "deflation", "property", "exports", "imports",
                 "home sales", "home prices", "new home", "housing", "developers",
                 "retail sales", "industrial output", "industrial production",
                 "credit", "loans", "trade surplus",
                 "trade", "tariffs", "economy", "GDP", "PMI", "factory",
                 "steel", "Xi", "rate cut", "growth", "consumption"],
     "context": ["China", "Chinese", "Beijing", "Hong Kong"],
     "queries": [
        'China (PBOC OR yuan OR stimulus OR property OR Politburo)',
        'China (deflation OR PPI OR CPI OR "yuan fixing")',
        '"China exports" OR "China trade" OR "iron ore" OR "steel output"',
     ]},
    {"title": "🇳🇿 New Zealand", "max": 4, "priority": 2.4,
     "topics": {
        "RBNZ, OCR & hike timing": ["RBNZ", "Reserve Bank of New Zealand",
            "official cash rate", "Breman"],
        "Kiwi dollar": ["NZ dollar", "NZD", "New Zealand dollar"],
        "NZ election (7 Nov)": ["Luxon", "Hipkins", "NZ election",
            "New Zealand election"],
        "Dairy prices (GDT) & Fonterra": ["GDT", "Global Dairy Trade", "Fonterra"],
        "NZ stocks": ["NZX"],
     },
     "generic": ["kiwi", "OCR", "inflation", "CPI", "jobs", "unemployment",
                 "election", "economy", "GDP", "dairy", "central bank",
                 "rate hike", "Conway", "budget"],
     "context": ["New Zealand", "NZ", "Wellington", "Auckland"],
     "queries": [
        'RBNZ OR "Reserve Bank of New Zealand" OR "official cash rate" OR Breman',
        '"New Zealand" (inflation OR election OR "kiwi dollar" OR economy)',
        '"Global Dairy Trade" OR Fonterra OR "dairy prices"',
     ]},
    {"title": "🇦🇺 Australia", "max": 5, "priority": 2.5,
     "topics": {
        "RBA (hiking cycle), CPI & trimmed mean, jobs": ["RBA",
            "Reserve Bank of Australia", "Bullock", "Hauser", "trimmed mean",
            "Australian dollar", "Aussie dollar"],
        "Australian stocks": ["ASX", "ASX 200", "Australian stocks"],
     },
     "generic": ["inflation", "CPI", "jobs", "unemployment", "wages", "economy",
                 "GDP", "rate hike", "rate hikes", "budget", "iron ore",
                 "central bank", "Chalmers"],
     "context": ["Australia", "Australian", "Aussie", "Canberra", "Sydney"],
     "queries": [
        'RBA OR "Reserve Bank of Australia" OR Bullock OR Hauser',
        'Australia ("monthly CPI" OR "trimmed mean" OR inflation OR jobs OR unemployment)',
        '"Australian dollar" OR "iron ore price"',
     ]},
    {"title": "🇧🇷 Brazil", "max": 5, "priority": 3,
     "topics": {
        "Lula's next finance minister & fiscal framework": ["Haddad",
            "Brazil budget", "Brazil fiscal", "fiscal framework"],
        "Lula vs Congress, Bolsonaro's election challenge": ["Lula", "Bolsonaro"],
        "BCB, Selic & the real": ["Selic", "Copom", "Galipolo", "Galípolo",
            "Brazilian real", "Brazil real", "Brazil's real"],
        "Treasury bond intervention & DI rates": ["Tesouro", "DI rates",
            "DI futures"],
        "Brazilian stocks": ["Bovespa", "Ibovespa", "Brazilian stocks",
            "Brazil stocks"],
     },
     "generic": ["finance minister", "fiscal", "budget", "debt", "Congress",
                 "electoral court", "TSE", "election", "central bank",
                 "inflation", "economy", "GDP", "bonds", "National Treasury",
                 "bond buyback", "currency", "real"],
     "context": ["Brazil", "Brazilian", "Brasilia", "Sao Paulo", "São Paulo"],
     "queries": [
        'Brazil ("finance minister" OR fiscal OR budget OR debt OR Haddad)',
        'Lula (Congress OR cabinet OR Bolsonaro OR "electoral court")',
        'Brazil (Selic OR Copom OR "central bank" OR real)',
        'Brazil ("Treasury" OR "DI rates" OR "bond yields" OR buyback)',
     ]},
    {"title": "🇲🇽 Mexico", "max": 5, "priority": 4,
     "topics": {
        "USMCA annual reviews & US tariffs": ["USMCA", "T-MEC", "Ebrard",
            "Mexico tariffs"],
        "Banxico (hawkish turn) & the peso": ["Banxico", "Mexican peso",
            "Mexico's peso"],
        "Sheinbaum & politics": ["Sheinbaum"],
        "Mexican stocks": ["Mexican stocks", "Mexico stocks", "Mexbol"],
     },
     "generic": ["peso", "tariffs", "trade deal", "trade talks", "annual review",
                 "Greer", "central bank", "inflation", "economy", "GDP",
                 "remittances", "cartels", "judicial reform", "budget"],
     "context": ["Mexico", "Mexican", "Mexico City"],
     "queries": [
        'Mexico (tariffs OR USMCA OR "annual review" OR Ebrard OR Greer)',
        'Mexico (peso OR Banxico OR Sheinbaum)',
     ]},
    {"title": "🛢️ Oil & Middle East", "max": 5, "priority": 0,
     "topics": {
        "US-Iran conflict & Strait of Hormuz": ["Iran", "Iranian", "Tehran",
            "Hormuz"],
        "Oil prices & OPEC": ["Brent", "WTI", "OPEC", "OPEC+", "oil prices",
            "oil price", "crude prices", "crude oil", "oil market"],
     },
     "generic": ["oil", "crude", "Saudi", "Israel", "Gulf", "tanker", "tankers",
                 "ceasefire", "strikes", "supply"],
     "context": ["oil", "crude", "barrel", "barrels", "energy", "Middle East"],
     "queries": [
        'oil (Brent OR crude OR OPEC OR prices)',
        'Iran (US OR strikes OR Hormuz OR ceasefire OR talks)',
     ]},
    {"title": "📈 Global equities & AI trade", "max": 6, "priority": 6,
     "allow_earnings": True,          # big-tech results move the AI trade
     "topics": {
        "Big index moves": ["Wall Street", "S&P 500", "Nasdaq", "global stocks",
            "stock market", "stock markets", "equity markets", "selloff",
            "sell-off", "record high"],
        "AI trade & big tech (drives yen safe-haven flows)": ["Nvidia", "AI stocks",
            "AI trade", "AI bubble", "chipmaker", "chipmakers", "semiconductor",
            "semiconductors", "SK Hynix", "TSMC", "hyperscalers"],
     },
     "generic": ["AI", "artificial intelligence", "Alphabet", "Google",
                 "Microsoft", "Meta", "Amazon", "Apple", "OpenAI", "Anthropic",
                 "Samsung", "data center", "data centers", "rally", "stocks",
                 "equities"],
     "context": ["stocks", "shares", "earnings", "investors", "market", "markets",
                 "valuations", "capex", "spending"],
     "queries": [
        'stocks ("Wall Street" OR "global markets" OR selloff OR rally OR "record high")',
        '(AI OR "artificial intelligence") (stocks OR earnings OR selloff OR IPO)',
        'Nvidia OR Alphabet OR Microsoft OR Meta OR "SK Hynix" OR TSMC earnings',
     ]},
    {"title": "🌍 World", "max": 6, "priority": 7,
     "topics": {
        "War, ceasefires & sanctions": ["war", "ceasefire", "invasion", "coup",
            "sanctions", "missile", "airstrike", "airstrikes"],
        "Trade wars & export controls": ["tariff", "tariffs", "trade war",
            "export controls"],
        "Elections & political shocks": ["snap election", "impeachment",
            "state of emergency", "no-confidence", "no confidence"],
        "Debt crises & defaults": ["debt crisis", "bank failure",
            "emergency rate", "sovereign default", "bond rout", "bond selloff"],
     },
     "generic": ["resigns", "resignation", "default", "downgrade", "election",
                 "protests", "budget"],
     "context": ["government", "prime minister", "president", "minister",
                 "parliament", "sovereign", "debt", "bonds", "rating"],
     "queries": [
        'war OR ceasefire OR invasion OR coup OR sanctions',
        'tariffs OR "trade war" OR "export controls"',
        '"snap election" OR resigns OR impeachment OR "state of emergency"',
        'default OR "debt crisis" OR "bank failure" OR "emergency rate"',
     ]},
]

# ==========================================================================


def download(tickers: list):
    """Daily bars for the last ~2 weeks from Yahoo, with gentle retries."""
    for attempt in range(3):  # Yahoo occasionally rate-limits
        try:
            data = yf.download(tickers, period="14d", interval="1d", group_by="column",
                               progress=False, auto_adjust=False, threads=False)
            if data is not None and not data.empty:
                return data
        except Exception as e:
            print(f"[prices] attempt {attempt + 1} failed: {e}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))
    return None


def quote_from_df(df) -> dict | None:
    """From daily High/Low/Close rows: last, previous close, the close 5
    sessions ago (for week-over-week) and the 5-session low/high."""
    df = df[["High", "Low", "Close"]].dropna(subset=["Close"])
    if len(df) < 2:
        return None
    week = df.tail(5)
    return {
        "last": float(df["Close"].iloc[-1]),
        "prev": float(df["Close"].iloc[-2]),
        "wk_ago": float(df["Close"].iloc[-6]) if len(df) >= 6 else None,
        "lo": float(week[["Low", "Close"]].min().min()),
        "hi": float(week[["High", "Close"]].max().max()),
        "closes": df["Close"],
    }


def quote_from(data, sym: str) -> dict | None:
    try:
        df = data.xs(sym, axis=1, level=1) if data.columns.nlevels > 1 else data
        return quote_from_df(df)
    except Exception:
        return None


def fetch_quotes(symbols: dict) -> dict:
    """Return {name: quote dict or None}. Tries FALLBACK_TICKERS if needed."""
    data = download(list(symbols.values()))
    out = {name: (quote_from(data, sym) if data is not None else None)
           for name, sym in symbols.items()}
    for name in [n for n, q in out.items() if q is None and n in FALLBACK_TICKERS]:
        for alt in FALLBACK_TICKERS[name]:
            alt_data = download([alt])
            q = quote_from(alt_data, alt) if alt_data is not None else None
            if q:
                out[name] = q
                break
    return out


ERAS = {"S": 1925, "H": 1988, "R": 2018}   # Showa / Heisei / Reiwa year offsets


def parse_mof_date(text: str):
    text = text.strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%Y.%m.%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    m = re.match(r"([SHR])(\d+)\.(\d+)\.(\d+)$", text)   # e.g. R8.10.28
    if m:
        return dt.date(ERAS[m[1]] + int(m[2]), int(m[3]), int(m[4]))
    return None


def fetch_jgb_yields() -> pd.DataFrame | None:
    """Daily JGB yields from Japan's Ministry of Finance: rows = dates,
    columns = tenors like '2Y', '10Y'. Tries the current-month file first and
    falls back to the historical files when it is too short (early in a month)."""
    rows = {}
    for url in JGB_CSV_URLS:
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
        except Exception as ex:
            print(f"[jgb] {url}: {ex}", file=sys.stderr)
            continue
        text = None
        for enc in ("utf-8-sig", "shift_jis", "cp932"):
            try:
                text = r.content.decode(enc)
                break
            except UnicodeDecodeError:
                pass
        if text is None:
            continue
        header = None
        for line in csv.reader(io.StringIO(text)):
            if not line:
                continue
            if header is None:
                # Header row: first cell is the date label, then 1Y, 2Y ...
                if len(line) > 3 and re.match(r"^\s*1\s*(Y|年)", line[1] or ""):
                    header = [re.sub(r"\s|年", "", c).upper().replace("Y", "") + "Y"
                              for c in line[1:]]
                continue
            d = parse_mof_date(line[0])
            if not d:
                continue
            vals = {}
            for tenor, v in zip(header, line[1:]):
                try:
                    vals[tenor] = float(v)
                except ValueError:
                    pass                      # "-" means no data
            if vals:
                rows[d] = vals
        if len(rows) >= 7:                    # enough for prev + week ago
            break
    if not rows:
        print("[jgb] no data", file=sys.stderr)
        return None
    df = pd.DataFrame.from_dict(rows, orient="index").sort_index()
    df.index = pd.to_datetime(df.index)
    return df.tail(30)


def fetch_fred(series: str) -> pd.Series | None:
    """Last ~6 weeks of a daily FRED series (no API key needed)."""
    start = (dt.date.today() - dt.timedelta(days=45)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}&cosd={start}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        out = {}
        for row in list(csv.reader(io.StringIO(r.text)))[1:]:   # skip header
            if len(row) >= 2:
                try:
                    out[pd.Timestamp(row[0])] = float(row[1])
                except ValueError:
                    pass                      # "." = no data (holiday)
        if out:
            return pd.Series(out).sort_index()
        print(f"[fred] {series}: no data", file=sys.stderr)
    except Exception as ex:
        print(f"[fred] {series}: {ex}", file=sys.stderr)
    return None


def fetch_rates() -> dict:
    """Bond yields (%), plus spreads in basis points."""
    yahoo = {k: v for k, v in RATES.items()
             if not v.startswith(("MOF:", "FRED:"))}
    out = fetch_quotes(yahoo)
    for name, q in out.items():                # ^TNX-style indices can be yield x10
        if q and q["last"] > 20:
            for k in ("last", "prev", "wk_ago", "lo", "hi"):
                if q[k] is not None:
                    q[k] /= 10
            q["closes"] = q["closes"] / 10

    for name, src in RATES.items():
        if src.startswith("FRED:"):
            c = fetch_fred(src[5:])
            out[name] = (quote_from_df(pd.DataFrame({"High": c, "Low": c, "Close": c}))
                         if c is not None else None)

    mof = {k: v[4:] for k, v in RATES.items() if v.startswith("MOF:")}
    jgb = fetch_jgb_yields() if mof else None
    for name, tenor in mof.items():
        q = None
        if jgb is not None and tenor in jgb.columns:
            c = jgb[tenor].dropna()
            q = quote_from_df(pd.DataFrame({"High": c, "Low": c, "Close": c}))
        out[name] = q

    for name, (long_leg, short_leg) in SPREADS.items():
        a, b = out.get(long_leg), out.get(short_leg)
        q = None
        if a and b:
            # Line the two series up by date (calendar day) before subtracting
            la = a["closes"].copy(); la.index = la.index.normalize().tz_localize(None) \
                if la.index.tz is not None else la.index.normalize()
            lb = b["closes"].copy(); lb.index = lb.index.normalize().tz_localize(None) \
                if lb.index.tz is not None else lb.index.normalize()
            sp = ((la - lb).dropna() * 100)
            q = quote_from_df(pd.DataFrame({"High": sp, "Low": sp, "Close": sp}))
            if q:
                q["spread"] = True
        out[name] = q
    return {k: out.get(k) for k in [*RATES, *SPREADS]}


def fetch_feed(url: str, label: str) -> list | None:
    """Download and parse one RSS feed.
    Returns a list of entries, or None if the request failed or was blocked."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
    except Exception as ex:
        print(f"[news] {label}: request failed: {ex}", file=sys.stderr)
        return None
    if r.status_code != 200:
        print(f"[news] {label}: HTTP {r.status_code}", file=sys.stderr)
        return None
    feed = feedparser.parse(r.content)
    if feed.bozo and not feed.entries:   # e.g. a captcha/consent page, not RSS
        print(f"[news] {label}: response was not a valid feed", file=sys.stderr)
        return None
    return feed.entries


def entry_ts(e) -> int:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return calendar.timegm(t) if t else 0


def keyword_regex(words: list) -> re.Pattern:
    alts = "|".join(re.escape(w) for w in sorted(words, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alts})(?!\w)", re.IGNORECASE)


def fetch_bloomberg(status: dict) -> list:
    """All stories from Bloomberg's RSS feeds -> [(ts, title, link, source, text)]."""
    items, ok = [], 0
    for url in BLOOMBERG_FEEDS:
        entries = fetch_feed(url, f"Bloomberg {url.split('/feeds/')[-1]}")
        if entries is None:
            continue
        ok += 1
        for e in entries:
            title = (e.get("title") or "").strip()
            if not title:
                continue
            text = title + " " + re.sub(r"<[^>]+>", " ", e.get("summary", ""))
            items.append((entry_ts(e), title, e.get("link", ""), "Bloomberg", text))
        time.sleep(0.3)
    status["Bloomberg feeds"] = ok > 0
    print(f"[news] Bloomberg feeds: {ok}/{len(BLOOMBERG_FEEDS)} ok, "
          f"{len(items)} stories", file=sys.stderr)
    return items


def short_google_link(url: str) -> str:
    """news.google.com/rss/articles/<id>?oc=5 -> news.google.com/articles/<id>
    (same redirect, fewer characters; Telegram caps total link size per message)."""
    url = url.split("?", 1)[0]
    return url.replace("news.google.com/rss/articles/", "news.google.com/articles/")


def search_google(query: str, site: str, status: dict) -> list:
    """One Google News search on one site -> [(ts, title, link, source, text)]."""
    if status.get("_google_fails", 0) >= 4 and not status.get("Google News"):
        return []                       # blocked this run; don't keep trying
    q = quote_plus(f"site:{site} {query} when:{LOOKBACK_HOURS}h")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-SG&gl=SG&ceid=SG:en"
    entries = fetch_feed(url, f"Google News ({site}) '{query[:40]}'")
    if entries is None:
        status.setdefault("Google News", False)
        status["_google_fails"] = status.get("_google_fails", 0) + 1
        return []
    status["Google News"] = True
    items = []
    for e in entries:
        title = e.title.rsplit(" - ", 1)[0].strip()   # drop " - Reuters"
        source = e.get("source", {}).get("title", "") or site
        items.append((entry_ts(e), title, short_google_link(e.link), source, title))
    return items


# Not news: videos, audio, podcasts, opinion columns and live blogs.
SKIP_URL_PARTS = ("/videos/", "/audio/", "/podcasts/", "/opinion/", "/live/",
                  "/live-blog", "/newsletters/")
SKIP_TITLE_RX = re.compile(r"^(watch|listen|live|podcast|video|opinion|"
                           r"newsletter|explainer|quiz|odd lots|commentary|"
                           r"column|editorial|breakingviews)\b|"
                           r"^.{0,40}\bLive Q&A\b|\b(senior )?reporter\s*:|"
                           r"\b(correspondent|editor|columnist)\s*:",
                           re.IGNORECASE)
# A few very common Spanish / Portuguese words; two or more = not English.
FOREIGN_WORDS = {"de", "la", "el", "los", "las", "que", "en", "y", "para", "con",
                 "una", "por", "del", "um", "uma", "não", "em", "do", "da", "dos",
                 "das", "com", "sobre", "qué", "cómo"}
STOPWORDS = {"the", "a", "an", "of", "to", "in", "on", "for", "and", "as", "at",
             "by", "with", "is", "are", "be", "its", "it", "from", "after",
             "amid", "over", "says", "say", "said", "new", "s", "us", "will",
             "may", "could", "than", "more", "into", "up", "out", "what", "how"}


def is_noise(title: str, link: str) -> bool:
    if any(part in link for part in SKIP_URL_PARTS):
        return True
    if SKIP_TITLE_RX.search(title.strip()):
        return True
    if "¿" in title or "¡" in title:
        return True
    words = re.findall(r"[a-záéíóúãõâêôçñ]+", title.lower())
    return sum(w in FOREIGN_WORDS for w in words) >= 2


def word_set(title: str, link: str = "") -> set:
    """Content words of a headline (plus the URL slug for Bloomberg links),
    used to spot the same story worded differently."""
    text = title
    m = re.search(r"/\d{4}-\d{2}-\d{2}/([a-z0-9-]+)", link)
    if m:
        text += " " + m.group(1).replace("-", " ")
    words = re.findall(r"[a-z0-9$%]+", text.lower().replace("’", "'"))
    return {w.rstrip("s") for w in words if len(w) > 2 and w not in STOPWORDS}


_ENTITIES = {}


def entity_section(token: str):
    """Which section a one-word name belongs to (fed -> US, rba -> Australia)."""
    if not _ENTITIES:
        for sec in SECTIONS:
            words = [w for kws in sec["topics"].values() for w in kws] + sec.get("context", [])
            for w in words:
                if " " not in w and len(w) > 2:
                    _ENTITIES.setdefault(w.lower().rstrip("s"), sec["title"])
    return _ENTITIES.get(token)


def same_story(a: set, b: set) -> bool:
    """Same event worded differently? Needs several shared words, and never
    merges headlines naming different markets ("Fed holds rates" vs "RBA
    holds rates") or different numbers ("since 1995" vs "since 2011")."""
    if len(a) < 3 or len(b) < 3:
        return False
    only_a, only_b = a - b, b - a
    secs_a = {entity_section(t) for t in only_a} - {None}
    secs_b = {entity_section(t) for t in only_b} - {None}
    if secs_a and secs_b and not (secs_a & secs_b):
        return False
    if any(t[0].isdigit() for t in only_a) and any(t[0].isdigit() for t in only_b):
        return False
    overlap = len(a & b)
    jaccard = overlap / len(a | b)
    contain = overlap / min(len(a), len(b))
    return (overlap >= 4 and (jaccard >= 0.45 or contain >= 0.6)) or \
        (overlap >= 3 and (jaccard >= 0.6 or contain >= 0.75))


# Company results: macro sections skip these unless a core keyword is also
# in the headline (e.g. "BOJ ..." still counts, "Sinopec profits soar" doesn't).
EARNINGS_RX = re.compile(r"\b(profits?|earnings|revenue|quarterly|"
                         r"(first|second|third|fourth)[- ]quarter|q[1-4]|"
                         r"beats? (profit |earnings )?(estimates|forecasts)|"
                         r"misses (estimates|forecasts)|"
                         r"raises (its )?(outlook|forecast|guidance)|guidance|"
                         r"buyback|dividend|ipo|writedown|volumes|cfo|ceo)\b",
                         re.IGNORECASE)
EARNINGS_PENALTY = 8      # roughly cancels one headline keyword match
STOCK_WORDS = ["stocks", "shares", "equities", "stock market", "selloff",
               "sell-off"]


def section_matchers(sec: dict) -> tuple:
    core = [w for kws in sec["topics"].values() for w in kws]
    generic = list(sec.get("generic") or [])
    if sec.get("context"):                 # country sections: local stock moves
        generic += STOCK_WORDS
    return (keyword_regex(core),
            keyword_regex(generic or ["\x00"]),
            keyword_regex(sec.get("context") or ["\x00"]),
            sec.get("allow_earnings", False))


def match_score(title: str, text: str, matchers: tuple) -> int:
    """0 = not relevant. Core keywords in the headline count most; generic
    keywords count only when a context (country/market) word is present."""
    core_rx, gen_rx, ctx_rx, allow_earnings = matchers
    core_title = len(set(m.lower() for m in core_rx.findall(title)))
    penalty = EARNINGS_PENALTY if (not allow_earnings and EARNINGS_RX.search(title)) else 0
    core_any = len(set(m.lower() for m in core_rx.findall(text)))
    has_ctx = bool(ctx_rx.search(text))
    gen_title = len(set(m.lower() for m in gen_rx.findall(title))) if has_ctx else 0
    gen_any = len(set(m.lower() for m in gen_rx.findall(text))) if has_ctx else 0
    if core_any == 0 and gen_any == 0:
        return 0
    if core_any == 0 and not (has_ctx and gen_title):
        return 0          # generic words only in the summary: too weak
    score = 6 * core_title + 2 * (core_any - core_title) + 2 * gen_title + \
        (gen_any - gen_title) - penalty
    return max(score, 0)


def fetch_candidates() -> tuple:
    """Collect stories per section, best match first.
    Returns ({section title: [story, ...]}, failed_sources).
    A story is a dict: ts, title, link, text, score."""
    status = {}
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    pool = fetch_bloomberg(status)
    for sec in SECTIONS:
        for q in sec["queries"]:
            for site in GOOGLE_SITES:
                pool += search_google(q, site, status)
                time.sleep(0.5)

    # Score every story against every section; it goes to the section it
    # matches best (ties go to the lower `priority` number).
    matchers = {sec["title"]: section_matchers(sec) for sec in SECTIONS}
    order = {sec["title"]: sec["priority"] for sec in SECTIONS}
    by_section = {sec["title"]: [] for sec in SECTIONS}
    seen_links = set()
    for ts, title, link, _src, text in pool:
        if link in seen_links or (ts and ts < cutoff) or is_noise(title, link):
            continue
        seen_links.add(link)
        scores = [(match_score(title, text, m), -order[t], t) for t, m in matchers.items()]
        best = max(scores)
        if best[0] > 0:
            by_section[best[2]].append((best[0], ts, title, link, text))

    accepted, out = [], {}       # accepted: word sets of stories kept so far
    for sec in sorted(SECTIONS, key=lambda x: x["priority"]):
        title_ = sec["title"]
        # Without Claude only the top `max` are shown, so only those claim
        # their story (lower-ranked ones stay free for other sections).
        limit = CANDIDATES_PER_SECTION if os.environ.get("ANTHROPIC_API_KEY") else sec["max"]
        stories = []
        for score, ts, title, link, text in sorted(by_section[title_],
                                                   key=lambda x: (x[0], x[1]), reverse=True):
            ws = word_set(title, link)
            if any(same_story(ws, other) for other in accepted):
                continue                                   # duplicate story
            accepted.append(ws)
            stories.append({"ts": ts, "title": title, "link": link,
                            "text": text, "score": score})
            if len(stories) >= limit:
                break
        out[title_] = stories

    failed = [name for name, ok in status.items()
              if not name.startswith("_") and not ok]
    return out, failed


SUBORDINATE = re.compile(r"^(after|while|as|despite|with|if|when|before|"
                         r"amid|since|because|though|although|once|until|"
                         r"what|how|why|where)\b", re.IGNORECASE)
DANGLING = {"a", "an", "the", "to", "of", "in", "on", "for", "and", "or", "as",
            "at", "by", "with", "from", "is", "are", "its", "it", "that", "his",
            "her", "their", "be", "was", "were"}


def shorten(title: str, limit: int = HEADLINE_MAX) -> str:
    """Short, skimmable headline: keep the main clause, then trim to `limit`
    without leaving a dangling word or a fragment like "After landing in US"."""
    t = re.sub(r"\s+", " ", title).strip()
    if len(t) <= limit:
        return t
    for sep in [": ", "; ", " - ", ", ", " as ", " after ", " amid ", " while ",
                " despite ", " with ", " if ", " on ", " ahead of "]:
        head = re.split(re.escape(sep), t, maxsplit=1, flags=re.IGNORECASE)[0]
        if 15 <= len(head) <= limit and head != t and not SUBORDINATE.match(head):
            return head
    words = t[:limit].rsplit(" ", 1)[0].rstrip(",;:-").split(" ")
    while len(words) > 3 and words[-1].lower().strip("’'\"") in DANGLING:
        words.pop()
    return " ".join(words) + "…"


def claude_select(cands: dict) -> dict | None:
    """Ask Claude to pick the major stories per section and write a short
    brief for each. Returns {section title: [(story, brief), ...]} or None."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not any(cands.values()):
        return None
    lookup, lines = {}, []
    for sec in SECTIONS:
        lines.append(f"\n## {sec['title']} (pick up to {sec['max']})")
        lines.append("Priority themes: " + "; ".join(sec["topics"]))
        for i, st in enumerate(cands.get(sec["title"], [])):
            sid = f"{sec['priority']}-{i}"
            lookup[sid] = (sec["title"], st)
            hrs = int((time.time() - st["ts"]) / 3600) if st["ts"] else "?"
            lines.append(f"[{sid}] ({hrs}h ago) {st['title']}")
    prompt = (
        "You are preparing a twice-daily macro/FX market brief for a trader. "
        "Below are recent Reuters and Bloomberg headlines, grouped by market.\n"
        "For each section, choose the most market-moving stories, favouring the "
        "priority themes listed (central bank decisions and officials' comments, "
        "key data releases, elections and major political events, policy shocks). "
        "Skip minor, company-specific or lifestyle stories, and keep only one "
        "story per event. It is fine to pick fewer than the maximum, or none.\n"
        "For each pick, write a brief of at most 8 words that says what happened, "
        "e.g. 'BOJ holds at 0.75%, signals December hike'. "
        "Plain text, no source names, no full stop.\n\n"
        "Reply with JSON only, in this exact shape:\n"
        '{"picks": [{"id": "1-0", "brief": "..."}, ...]}\n'
        + "\n".join(lines)
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": CLAUDE_MODEL, "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=90,
        )
        if not r.ok:
            print(f"[claude] HTTP {r.status_code}: {r.text[:300]}", file=sys.stderr)
            return None
        text = "".join(b.get("text", "") for b in r.json().get("content", []))
        picks = json.loads(text[text.index("{"): text.rindex("}") + 1])["picks"]
    except Exception as ex:
        print(f"[claude] failed: {ex}", file=sys.stderr)
        return None

    out = {sec["title"]: [] for sec in SECTIONS}
    for p in picks:
        if p.get("id") in lookup and p.get("brief"):
            title, st = lookup[p["id"]]
            out[title].append((st, shorten(p["brief"], 80)))
    for title in out:                                       # newest first
        out[title].sort(key=lambda x: x[0]["ts"], reverse=True)
    print(f"[claude] picked {sum(map(len, out.values()))} stories", file=sys.stderr)
    return out


def fetch_news() -> tuple:
    """Return ({section title: [(story, brief), ...]}, failed_sources, used_claude)."""
    cands, failed = fetch_candidates()
    picked = claude_select(cands)
    if picked is not None:
        return picked, failed, True
    maxes = {sec["title"]: sec["max"] for sec in SECTIONS}
    return ({t: [(st, shorten(st["title"])) for st in sts[:maxes[t]]]
             for t, sts in cands.items()}, failed, False)


def age(ts: int) -> str:
    if not ts:
        return ""
    mins = max(0, int((time.time() - ts) / 60))
    return f"{mins}m" if mins < 60 else f"{mins // 60}h"


def fmt_price_table(quotes: dict, kind: str) -> str:
    """One small block per instrument:
         USD/JPY      157.85   ▼ -0.09%
           Prev close 157.99
           WoW chg    +0.45%
           Week low   156.20
           Week high  158.40
    For bond yields (kind="rates") changes are in basis points (bp), and
    spreads are shown in bp throughout.
    """
    blocks = []
    for name, q in quotes.items():
        if q is None:
            blocks.append(f"{name:<12}{'n/a':>11}")
            continue
        if kind == "rates" and q.get("spread"):
            f = lambda x: f"{x:,.1f}bp"
            chg = lambda a, b: f"{round(a - b, 6) + 0:+.1f}bp"
        elif kind == "rates":
            f = lambda x: f"{x:.3f}%"
            chg = lambda a, b: f"{round((a - b) * 100, 6) + 0:+.1f}bp"
        else:
            if kind == "fx":
                d = 2 if ("JPY" in name or name == "DXY") else 4
            else:
                d = 3 if abs(q["last"]) < 20 else 2
            f = lambda x, d=d: f"{x:,.{d}f}"
            chg = lambda a, b: f"{(a / b - 1) * 100:+.2f}%"
        diff = round(q["last"] - q["prev"], 6)      # ignore float noise
        arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "•")
        wow = "n/a" if q["wk_ago"] is None else chg(q["last"], q["wk_ago"])
        blocks.append("\n".join([
            f"{name:<12}{f(q['last']):>11}  {arrow} {chg(q['last'], q['prev'])}",
            f"  {'Prev close':<10}{f(q['prev']):>11}",
            f"  {'WoW chg':<10}{wow:>11}",
            f"  {'Week low':<10}{f(q['lo']):>11}",
            f"  {'Week high':<10}{f(q['hi']):>11}",
        ]))
    return "<pre>" + html.escape("\n\n".join(blocks)) + "</pre>"


def fmt_outlook(today: dt.date) -> str:
    """Policy-rate line plus the events coming up in the next CALENDAR_DAYS,
    with warnings when the hand-maintained tables need updating."""
    lines = ["<b>📅 Coming up</b>"]
    upcoming, last_date = [], None
    for d_str, what in CALENDAR:
        try:
            d = dt.date.fromisoformat(d_str)
        except ValueError:
            print(f"[calendar] bad date: {d_str}", file=sys.stderr)
            continue
        last_date = max(last_date, d) if last_date else d
        days = (d - today).days
        if 0 <= days <= CALENDAR_DAYS:
            when = "Today" if days == 0 else ("Tomorrow" if days == 1
                                              else f"{d:%a %d %b}")
            upcoming.append((d, f"• {when}: {html.escape(what)}"))
    if upcoming:
        lines += [line for _, line in sorted(upcoming, key=lambda x: x[0])]
    else:
        lines.append(f"<i>Nothing in the next {CALENDAR_DAYS} days.</i>")
    if last_date is None or (last_date - today).days < 14:
        lines.append("⚠️ <i>CALENDAR runs out soon. Add new dates in market_bot.py.</i>")

    rates = " · ".join(f"{k} {v}" for k, v in POLICY_RATES.items())
    lines.append(f"\n<b>🏛️ Policy rates</b>\n{html.escape(rates)}")
    try:
        age_days = (today - dt.date.fromisoformat(POLICY_RATES_AS_OF)).days
        if age_days > POLICY_RATES_STALE_DAYS:
            lines.append(f"⚠️ <i>Policy rates last updated {age_days} days ago.</i>")
    except ValueError:
        pass
    return "\n".join(lines)


def build_messages() -> list:
    """Prices first (one or more messages), then all the news in one message."""
    now = dt.datetime.now(ZoneInfo(TIMEZONE))
    label = "🌅 Morning brief" if now.hour < 12 else "🌙 Evening brief"

    prices = fetch_quotes({**FX, **INDICES, **COMMODITIES})
    rates = fetch_rates()
    price_parts = [
        f"<b>{label}</b> · {now:%a %d %b, %H:%M}",
        fmt_outlook(now.date()),
        "<b>💱 FX</b>\n" + fmt_price_table({k: prices[k] for k in FX}, "fx"),
        "<b>📊 Indices</b>\n" + fmt_price_table({k: prices[k] for k in INDICES}, "idx"),
        "<b>🏦 Rates</b>\n" + fmt_price_table(rates, "rates"),
        "<b>🛢️ Commodities</b>\n" + fmt_price_table({k: prices[k] for k in COMMODITIES}, "cmd"),
        "<i>Top line: latest level and change vs previous close. "
        "WoW chg: vs the close 5 trading sessions ago. "
        "Week low/high: last 5 trading sessions. Yield changes in bp. "
        "Prices via Yahoo Finance (may be delayed); UST 2Y via FRED (previous US "
        "close); JGB yields via Japan MOF (previous Tokyo close). Gold $/oz, oil $/bbl, copper $/lb.</i>",
    ]

    news, failed, used_claude = fetch_news()
    news_parts = [f"<b>📰 Headlines</b> · {now:%a %d %b, %H:%M}"]
    if failed:
        news_parts.append("⚠️ <i>Couldn't reach: " + html.escape(", ".join(failed))
                          + ". Showing the other source only.</i>")
    for sec in SECTIONS:
        items = news.get(sec["title"], [])
        lines = [f"<b>{sec['title']}</b>"]
        if not items:
            lines.append("<i>Nothing major.</i>")
        for st, brief in items:
            lines.append(f"• <a href=\"{html.escape(st['link'])}\">{html.escape(brief)}</a>")
        news_parts.append("\n".join(lines))

    return pack(price_parts) + pack(news_parts)


def visible_len(s: str) -> int:
    """Length Telegram counts: text after HTML tags are removed (links' URLs
    don't count)."""
    text = html.unescape(re.sub(r"<[^>]+>", "", s))
    return len(text.encode("utf-16-le")) // 2   # Telegram counts UTF-16 units


def pack(parts: list, limit: int = 3500) -> list:
    """Group blocks into messages under Telegram's 4096-char limit,
    never cutting through a block (or a line) mid-way."""
    msgs, cur = [], ""
    for part in parts:
        chunks = [part] if visible_len(part) <= limit else part.split("\n")
        for c in chunks:
            sep = "\n\n" if c is part else "\n"
            if cur and visible_len(cur) + len(sep) + visible_len(c) > limit:
                msgs.append(cur)
                cur = c
            else:
                cur = f"{cur}{sep}{c}" if cur else c
    if cur:
        msgs.append(cur)
    return msgs


def check_config(need_chat_id: bool = True) -> None:
    """Stop with a clear message if the token / chat id were never filled in."""
    if TELEGRAM_TOKEN.startswith("PASTE_"):
        sys.exit("No bot token set. Add the TELEGRAM_TOKEN secret on GitHub, "
                 "or paste it into the CONFIG block for local runs.")
    if need_chat_id and CHAT_ID.startswith("PASTE_"):
        sys.exit("No chat id set. Run `python market_bot.py --chat-id` to find it, "
                 "then add it as the CHAT_ID secret (or paste it into CONFIG).")


class TelegramError(Exception):
    pass


def send_telegram(text: str, html_mode: bool = True) -> None:
    payload = {"chat_id": CHAT_ID, "text": text, "disable_web_page_preview": True}
    if html_mode:
        payload["parse_mode"] = "HTML"
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json=payload, timeout=30)
    if not r.ok:
        raise TelegramError(f"Telegram error {r.status_code}: {r.text[:500]}")


TOO_BIG = ("ENTITIES_TOO_LONG", "MESSAGE_TOO_LONG", "message is too long")


def split_message(m: str) -> tuple | None:
    """Split a message in two near the middle, at a section break if possible,
    otherwise at a line break. Never splits inside a <pre> block's line."""
    for sep in ("\n\n", "\n"):
        pieces = m.split(sep)
        if len(pieces) < 2:
            continue
        half, total, cut = 0, len(m), 1
        for i, piece in enumerate(pieces[:-1], 1):
            half += len(piece) + len(sep)
            if half >= total / 2:
                cut = i
                break
        a, b = sep.join(pieces[:cut]), sep.join(pieces[cut:])
        if sep == "\n" and a.count("<pre>") != a.count("</pre>"):
            continue                          # would break a price table
        return a, b
    return None


def send_splitting(m: str, depth: int = 0) -> None:
    """Send m; if Telegram says it's too big, split it and send the halves."""
    try:
        send_telegram(m)
    except TelegramError as ex:
        halves = split_message(m) if depth < 3 and any(t in str(ex) for t in TOO_BIG) else None
        if not halves:
            raise
        print(f"[telegram] message too big, splitting ({ex})", file=sys.stderr)
        for h in halves:
            send_splitting(h, depth + 1)
            time.sleep(1)


def report_failure(err: str) -> None:
    """Best effort: tell the user on Telegram why the brief failed."""
    print(err, file=sys.stderr)
    try:
        send_telegram("⚠️ Market brief failed\n\n" + err[-3500:], html_mode=False)
    except Exception as ex:
        print(f"[telegram] could not send failure notice: {ex}", file=sys.stderr)


def show_chat_id() -> None:
    r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
                     timeout=30).json()
    chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
             for u in r.get("result", []) if "message" in u}
    if not chats:
        print("No messages found. Send your bot any message in Telegram, then rerun.")
    for cid, name in chats.items():
        print(f"chat id: {cid}   ({name})")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--preview", action="store_true")
    p.add_argument("--chat-id", action="store_true")
    args = p.parse_args()

    if args.chat_id:
        check_config(need_chat_id=False)
        show_chat_id()
        sys.exit()

    if not args.preview:
        check_config()

    try:
        messages = build_messages()
    except Exception:
        if args.preview:
            raise
        report_failure("Error while building the brief:\n" + traceback.format_exc())
        sys.exit(1)

    if args.preview:
        print("\n\n----- next message -----\n\n".join(messages))
        sys.exit()

    failed = 0
    for i, m in enumerate(messages, 1):
        try:
            send_splitting(m)
        except TelegramError as ex:
            failed += 1
            report_failure(f"Message {i} of {len(messages)} was rejected.\n{ex}\n\n"
                           f"Start of that message:\n{m[:600]}")
        time.sleep(1)
    print(f"Sent {len(messages) - failed} of {len(messages)} message(s).")
    if failed:
        sys.exit(1)
