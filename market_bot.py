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

# Government bond yields (%). "MOF:<tenor>" = Japan Ministry of Finance daily
# JGB yields (end of Tokyo day, published ~9:30am Tokyo the next business day).
RATES = {
    "UST 2Y":  "2YY=F",     # CME 2-year yield futures (Yahoo has no 2Y cash index)
    "UST 10Y": "^TNX",
    "UST 30Y": "^TYX",
    "JGB 2Y":  "MOF:2Y",
    "JGB 10Y": "MOF:10Y",
    "JGB 30Y": "MOF:30Y",
}
# Spreads shown in basis points: name -> (long leg, short leg)
SPREADS = {
    "UST 2s10s":   ("UST 10Y", "UST 2Y"),
    "UST-JGB 2Y":  ("UST 2Y", "JGB 2Y"),    # tracks Fed vs BOJ expectations
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
# Shown in this order. For each section:
#   topics:   theme -> keywords. A Bloomberg story belongs to the section if
#             its title/summary contains any keyword (case-insensitive,
#             whole words). The theme names are also what Claude is told to
#             prioritise.
#   queries:  Google News searches (run on Reuters and Bloomberg).
#             OR between alternatives, "quotes" for exact phrases.
#   priority: a story matching several sections goes to the one with the
#             LOWEST number (so "Japan inflation" lands in Japan, not US).
SECTIONS = [
    {"title": "🇺🇸 US", "max": 6, "priority": 5,
     "topics": {
        "Fed decisions, Warsh & Fed independence": ["Fed", "Federal Reserve",
            "FOMC", "Fed chair", "Warsh", "rate cut", "rate cuts", "rate hike",
            "rate hikes", "Fed independence"],
        "CPI / PCE inflation": ["US CPI", "US inflation", "consumer prices", "PCE"],
        "Payrolls & jobs": ["payrolls", "nonfarm", "jobs report", "jobless claims",
            "US unemployment"],
        "Midterms & Congress": ["midterm", "midterms", "Congress", "Senate",
            "House Republicans", "House Democrats", "government shutdown",
            "debt ceiling"],
        "Tariffs, Treasuries & White House policy": ["Treasury yields",
            "Treasuries", "Trump", "White House", "US tariffs"],
     },
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
        "Verbal escalation, rate checks & intervention (MOF, US Treasury)": ["yen",
            "rate check", "rate checks", "Katayama", "Mimura", "excessive moves",
            "speculative moves", "Bessent"],
        "Takaichi fiscal plans, consumption tax & JGBs": ["Takaichi",
            "consumption tax", "JGB", "JGBs", "Diet"],
        "Japan CPI & wages": ["Japan", "Japanese", "shunto"],
     },
     "queries": [
        '"Bank of Japan" OR BOJ OR Ueda',
        'yen ("rate check" OR intervention OR excessive OR speculative OR Mimura OR Katayama OR Bessent)',
        'Japan (CPI OR inflation OR wages)',
        'Takaichi (fiscal OR "consumption tax" OR budget) OR "JGB yields"',
     ]},
    {"title": "🇨🇳 China", "max": 5, "priority": 2,
     "topics": {
        "PBOC, yuan fixing & stimulus": ["PBOC", "yuan", "renminbi", "fixing",
            "China", "Chinese", "Beijing"],
        "China deflation (CPI/PPI)": ["China CPI", "China PPI", "deflation"],
        "Xi, Politburo & US-China": ["Xi", "Politburo"],
        "Property, trade, steel & iron ore": ["iron ore", "China property",
            "China exports", "steel output"],
     },
     "queries": [
        'China (PBOC OR yuan OR stimulus OR property OR Politburo)',
        'China (deflation OR PPI OR CPI OR "yuan fixing")',
        '"China exports" OR "China trade" OR "iron ore" OR "steel output"',
     ]},
    {"title": "🇳🇿 New Zealand", "max": 4, "priority": 2.4,
     "topics": {
        "RBNZ, OCR & hike timing": ["RBNZ", "Reserve Bank of New Zealand",
            "OCR", "official cash rate", "Breman", "Conway"],
        "Kiwi dollar & NZ data": ["kiwi", "New Zealand", "NZ dollar", "NZD"],
        "NZ election (7 Nov)": ["Luxon", "Hipkins", "NZ election"],
        "Dairy prices (GDT) & Fonterra": ["dairy", "GDT", "Global Dairy Trade",
            "Fonterra"],
     },
     "queries": [
        'RBNZ OR "Reserve Bank of New Zealand" OR "official cash rate" OR Breman',
        '"New Zealand" (inflation OR election OR "kiwi dollar" OR economy)',
        '"Global Dairy Trade" OR Fonterra OR "dairy prices"',
     ]},
    {"title": "🇦🇺 Australia", "max": 5, "priority": 2.5,
     "topics": {
        "RBA (hiking cycle), CPI & trimmed mean, jobs": ["RBA", "Bullock",
            "Hauser", "trimmed mean", "Australia", "Australian",
            "Australian dollar", "Aussie"],
        "Iron ore & China demand for Australia": ["iron ore", "BHP", "Rio Tinto",
            "Fortescue"],
     },
     "queries": [
        'RBA OR "Reserve Bank of Australia" OR Bullock OR Hauser',
        'Australia ("monthly CPI" OR "trimmed mean" OR inflation OR jobs OR unemployment)',
        '"Australian dollar" OR "iron ore price"',
     ]},
    {"title": "🇧🇷 Brazil", "max": 5, "priority": 3,
     "topics": {
        "Lula's next finance minister & fiscal framework": ["finance minister",
            "Haddad", "Brazil budget", "Brazil fiscal", "fiscal framework"],
        "Lula vs Congress, Bolsonaro's election challenge": ["Lula", "Bolsonaro",
            "Flavio", "Flávio", "electoral court", "TSE"],
        "BCB, Selic & the real": ["Selic", "Copom", "Galipolo", "Galípolo",
            "Brazil", "Brazilian"],
        "Treasury bond intervention & DI rates": ["Tesouro", "National Treasury",
            "DI rates", "DI futures", "bond buyback"],
     },
     "queries": [
        'Brazil ("finance minister" OR fiscal OR budget OR debt OR Haddad)',
        'Lula (Congress OR cabinet OR Bolsonaro OR "electoral court")',
        'Brazil (Selic OR Copom OR "central bank" OR real)',
        'Brazil ("Treasury" OR "DI rates" OR "bond yields" OR buyback)',
     ]},
    {"title": "🇲🇽 Mexico", "max": 5, "priority": 4,
     "topics": {
        "USMCA annual reviews & US tariffs": ["USMCA", "Ebrard", "Greer",
            "Mexico tariffs", "annual review"],
        "Banxico (hawkish turn) & the peso": ["Banxico", "peso"],
        "Sheinbaum & politics": ["Sheinbaum", "Mexico", "Mexican"],
     },
     "queries": [
        'Mexico (tariffs OR USMCA OR "annual review" OR Ebrard OR Greer)',
        'Mexico (peso OR Banxico OR Sheinbaum)',
     ]},
    {"title": "🛢️ Oil & Middle East", "max": 5, "priority": 0,
     "topics": {
        "US-Iran conflict & Strait of Hormuz": ["Iran", "Iranian", "Tehran",
            "Hormuz", "Middle East", "Israel", "Gulf"],
        "Oil prices & OPEC": ["oil", "crude", "Brent", "WTI", "OPEC", "OPEC+",
            "Saudi"],
     },
     "queries": [
        'oil (Brent OR crude OR OPEC OR prices)',
        'Iran (US OR strikes OR Hormuz OR ceasefire OR talks)',
     ]},
    {"title": "📈 Global equities & AI trade", "max": 6, "priority": 6,
     "topics": {
        "AI trade & big tech earnings (drives yen safe-haven flows)": ["AI",
            "artificial intelligence", "Nvidia", "Alphabet", "Google", "Microsoft",
            "Meta", "Amazon", "Apple", "OpenAI", "Anthropic", "chipmaker",
            "chipmakers", "semiconductor", "semiconductors", "SK Hynix",
            "Samsung", "TSMC", "data center", "data centers"],
        "Big index moves": ["stocks", "equities", "Wall Street", "S&P 500",
            "Nasdaq", "selloff", "sell-off", "rally", "record high"],
     },
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
        "Elections & political shocks": ["snap election", "resigns", "impeachment",
            "state of emergency"],
        "Defaults & crises": ["default", "debt crisis", "bank failure",
            "emergency rate"],
     },
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


def fetch_rates() -> dict:
    """Bond yields (%), plus spreads in basis points."""
    yahoo = {k: v for k, v in RATES.items() if not v.startswith("MOF:")}
    out = fetch_quotes(yahoo)
    for name, q in out.items():                # ^TNX-style indices can be yield x10
        if q and q["last"] > 20:
            for k in ("last", "prev", "wk_ago", "lo", "hi"):
                if q[k] is not None:
                    q[k] /= 10
            q["closes"] = q["closes"] / 10

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
    return out


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
        items.append((entry_ts(e), title, e.link, source, title))
    return items


def fetch_candidates() -> tuple:
    """Collect stories per section.
    Returns ({section title: [story, ...]}, failed_sources), newest first.
    A story is a dict: ts, title, link, text."""
    status = {}
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    bloomberg = fetch_bloomberg(status)

    # Match sections in priority order, so a story shared by two sections
    # lands in the more specific one.
    seen, out = set(), {}
    for sec in sorted(SECTIONS, key=lambda x: x["priority"]):
        words = [w for kws in sec["topics"].values() for w in kws]
        rx = keyword_regex(words)
        pool = [it for it in bloomberg if rx.search(it[4])]
        for q in sec["queries"]:
            for site in GOOGLE_SITES:
                # keep Google results only if they also mention a section keyword
                pool += [it for it in search_google(q, site, status) if rx.search(it[4])]
                time.sleep(0.5)
        pool = [it for it in pool if not it[0] or it[0] >= cutoff]
        pool.sort(key=lambda x: x[0], reverse=True)       # newest first
        stories = []
        for ts, title, link, _src, text in pool:
            key = re.sub(r"\W+", " ", title.lower()).strip()
            if key in seen:                                # skip duplicates
                continue
            seen.add(key)
            stories.append({"ts": ts, "title": title, "link": link, "text": text})
        out[sec["title"]] = stories[:CANDIDATES_PER_SECTION]

    failed = [name for name, ok in status.items()
              if not name.startswith("_") and not ok]
    return out, failed


def shorten(title: str, limit: int = HEADLINE_MAX) -> str:
    """Short, skimmable headline: keep the main clause, then trim to `limit`."""
    t = re.sub(r"\s+", " ", title).strip()
    if len(t) <= limit:
        return t
    # Keep the part before a clause break if it says enough on its own,
    # e.g. "Yen slides as BOJ holds rates" -> "Yen slides".
    for sep in [": ", "; ", " - ", ", ", " as ", " after ", " amid ", " while ",
                " despite ", " with ", " if ", " on ", " ahead of "]:
        head = re.split(re.escape(sep), t, maxsplit=1, flags=re.IGNORECASE)[0]
        if 15 <= len(head) <= limit and head != t:
            return head
    return t[:limit].rsplit(" ", 1)[0].rstrip(",;:-") + "…"


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
            chg = lambda a, b: f"{a - b:+.1f}bp"
        elif kind == "rates":
            f = lambda x: f"{x:.3f}%"
            chg = lambda a, b: f"{(a - b) * 100:+.1f}bp"
        else:
            if kind == "fx":
                d = 2 if ("JPY" in name or name == "DXY") else 4
            else:
                d = 3 if abs(q["last"]) < 20 else 2
            f = lambda x, d=d: f"{x:,.{d}f}"
            chg = lambda a, b: f"{(a / b - 1) * 100:+.2f}%"
        diff = q["last"] - q["prev"]
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
        "Prices via Yahoo Finance (may be delayed); JGB yields via Japan MOF "
        "(previous Tokyo close). Gold $/oz, oil $/bbl, copper $/lb.</i>",
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
    return len(html.unescape(re.sub(r"<[^>]+>", "", s)))


def pack(parts: list, limit: int = 3900) -> list:
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


def send_telegram(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        sys.exit(f"Telegram error {r.status_code}: {r.text}")


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

    messages = build_messages()
    if args.preview:
        print("\n\n----- next message -----\n\n".join(messages))
    else:
        for m in messages:
            send_telegram(m)
            time.sleep(1)
        print(f"Sent {len(messages)} message(s).")
