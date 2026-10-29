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
import datetime as dt
import html
import os
import re
import sys
import time
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf

# ============================ CONFIG (edit me) ============================

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN") or "PASTE_YOUR_BOT_TOKEN_HERE"
CHAT_ID = os.environ.get("CHAT_ID") or "PASTE_YOUR_CHAT_ID_HERE"

TIMEZONE = "Asia/Singapore"

# Display name -> Yahoo Finance ticker. If one shows "n/a", look the pair up
# on finance.yahoo.com and paste the exact ticker it uses.
FX = {
    "USD/JPY": "USDJPY=X",
    "AUD/USD": "AUDUSD=X",
    "USD/CNH": "CNH=X",
    "USD/BRL": "USDBRL=X",
    "USD/MXN": "USDMXN=X",
    "BRL/MXN": "BRLMXN=X",
    "EUR/USD": "EURUSD=X",
    "USD/SGD": "USDSGD=X",
}

# Tickers tried, in order, if the main one above returns nothing.
FALLBACK_TICKERS = {
    "USD/CNH": ["USDCNH=X", "CNH=F"],
}

INDICES = {
    "S&P 500":    "^GSPC",
    "Nasdaq":     "^IXIC",
    "Dow":        "^DJI",
    "Euro Stoxx": "^STOXX50E",
    "DAX":        "^GDAXI",
    "FTSE 100":   "^FTSE",
    "Nikkei":     "^N225",
    "Hang Seng":  "^HSI",
    "Shanghai":   "000001.SS",
    "ASX 200":    "^AXJO",
    "STI":        "^STI",
    "Bovespa":    "^BVSP",
    "Mexico IPC": "^MXX",
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

# Each section collects stories from both sources, removes duplicates and
# keeps the newest `max`.
#   keywords: words/phrases matched in Bloomberg titles + summaries
#             (case-insensitive, whole words)
#   queries:  Google News searches. Use OR between alternatives and
#             "quotes" for exact phrases.
# A story only appears once, in the first section it matches.
SECTIONS = [
    {"title": "🇯🇵 USD/JPY", "max": 6,
     "keywords": ["yen", "BOJ", "Bank of Japan", "Japan", "Japanese", "JGB",
                  "Fed", "Federal Reserve", "FOMC", "Treasury yields",
                  "Treasuries", "US inflation", "US CPI"],
     "queries": [
        'yen (intervention OR "excessive moves" OR "speculative moves" OR "finance ministry")',
        '"Bank of Japan" OR BOJ',
        'Japan (CPI OR inflation OR wages OR JGB)',
        '"Federal Reserve" OR "US CPI" OR "US inflation" OR "Treasury yields"',
    ]},
    {"title": "🇦🇺🇨🇳 Australia / China", "max": 6,
     "keywords": ["Australia", "Australian", "RBA", "Aussie", "China",
                  "Chinese", "Beijing", "PBOC", "yuan", "iron ore"],
     "queries": [
        'RBA OR "Reserve Bank of Australia" OR "Australian dollar"',
        'Australia (CPI OR inflation OR jobs OR unemployment)',
        'China (economy OR stimulus OR PBOC OR yuan OR property)',
        '"iron ore" OR "China exports" OR "China trade"',
    ]},
    {"title": "🇧🇷 Brazil", "max": 5,
     "keywords": ["Brazil", "Brazilian", "Lula", "Selic", "Copom"],
     "queries": [
        'Brazil (fiscal OR budget OR deficit OR spending OR debt)',
        'Brazil (Selic OR Copom OR "central bank" OR "Brazil real")',
    ]},
    {"title": "🇲🇽 Mexico", "max": 5,
     "keywords": ["Mexico", "Mexican", "peso", "Banxico", "Sheinbaum", "USMCA"],
     "queries": [
        'Mexico (tariff OR tariffs OR trade OR USMCA OR "trade policy")',
        'Mexico (peso OR Banxico OR "central bank")',
    ]},
    {"title": "📈 Global equities", "max": 4,
     "keywords": ["stocks", "equities", "Wall Street", "S&P 500", "Nasdaq",
                  "selloff", "rally", "record high", "shares"],
     "queries": [
        'stocks ("Wall Street" OR "global markets" OR selloff OR rally OR "record high")',
    ]},
    {"title": "🌍 Big-picture world news", "max": 6,
     "keywords": ["war", "ceasefire", "invasion", "coup", "sanctions",
                  "tariff", "tariffs", "trade war", "export controls",
                  "snap election", "resigns", "impeachment",
                  "state of emergency", "default", "debt crisis",
                  "bank failure", "emergency rate"],
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


def quote_from(data, sym: str) -> dict | None:
    """Last, previous close, % change and the 5-session (1 week) low/high."""
    try:
        df = data.xs(sym, axis=1, level=1) if data.columns.nlevels > 1 else data
        df = df[["High", "Low", "Close"]].dropna(subset=["Close"])
        if len(df) < 2:
            return None
        last, prev = float(df["Close"].iloc[-1]), float(df["Close"].iloc[-2])
        week = df.tail(5)
        lo = float(week[["Low", "Close"]].min().min())
        hi = float(week[["High", "Close"]].max().max())
        return {"last": last, "prev": prev, "chg": (last / prev - 1) * 100,
                "lo": lo, "hi": hi}
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


def fetch_sections() -> tuple:
    """Return ([(title, [(ts, title, link, source), ...]), ...], failed_sources)."""
    status = {}
    cutoff = time.time() - LOOKBACK_HOURS * 3600
    bloomberg = fetch_bloomberg(status)

    seen, out = set(), []
    for sec in SECTIONS:
        rx = keyword_regex(sec["keywords"])
        pool = [it for it in bloomberg if rx.search(it[4])]
        for q in sec["queries"]:
            for site in GOOGLE_SITES:
                pool += search_google(q, site, status)
                time.sleep(0.5)
        pool = [it for it in pool if not it[0] or it[0] >= cutoff]
        pool.sort(key=lambda x: x[0], reverse=True)       # newest first
        picked = []
        for ts, title, link, src, _ in pool:
            key = re.sub(r"\W+", " ", title.lower()).strip()
            if key in seen:                                # skip duplicates
                continue
            seen.add(key)
            picked.append((ts, title, link, src))
            if len(picked) >= sec["max"]:
                break
        out.append((sec["title"], picked))

    failed = [name for name, ok in status.items()
              if not name.startswith("_") and not ok]
    return out, failed


def age(ts: int) -> str:
    if not ts:
        return ""
    mins = max(0, int((time.time() - ts) / 60))
    return f"{mins}m" if mins < 60 else f"{mins // 60}h"


def fmt_price_table(quotes: dict, kind: str) -> str:
    """One small block per instrument:
         USD/JPY      157.85   ▼ -0.09%
           Prev close 157.99
           Week low   156.20
           Week high  158.40
    """
    blocks = []
    for name, q in quotes.items():
        if q is None:
            blocks.append(f"{name:<12}{'n/a':>11}")
            continue
        d = (2 if "JPY" in name else 4) if kind == "fx" else 2
        f = lambda x: f"{x:,.{d}f}"
        arrow = "▲" if q["chg"] > 0 else ("▼" if q["chg"] < 0 else "•")
        blocks.append("\n".join([
            f"{name:<12}{f(q['last']):>11}  {arrow} {q['chg']:+.2f}%",
            f"  {'Prev close':<10}{f(q['prev']):>11}",
            f"  {'Week low':<10}{f(q['lo']):>11}",
            f"  {'Week high':<10}{f(q['hi']):>11}",
        ]))
    return "<pre>" + html.escape("\n\n".join(blocks)) + "</pre>"


def build_parts() -> list:
    """Message blocks; packed into Telegram-sized messages later."""
    now = dt.datetime.now(ZoneInfo(TIMEZONE))
    label = "🌅 Morning brief" if now.hour < 12 else "🌙 Evening brief"
    parts = [f"<b>{label}</b> · {now:%a %d %b, %H:%M}"]

    prices = fetch_quotes({**FX, **INDICES})
    parts.append("<b>💱 FX</b>\n" + fmt_price_table({k: prices[k] for k in FX}, "fx"))
    parts.append("<b>📊 Indices</b>\n" + fmt_price_table({k: prices[k] for k in INDICES}, "idx"))
    parts.append("<i>Top line: latest price and % change vs previous close. "
                 "Week low/high: last 5 trading sessions. "
                 "Prices via Yahoo Finance, may be delayed.</i>")

    sections, failed = fetch_sections()
    if failed:
        parts.append("⚠️ <i>Couldn't reach: " + html.escape(", ".join(failed))
                     + ". Headlines below come from the other source only.</i>")
    for title, items in sections:
        if not items:
            parts.append(f"<b>{title}</b>\n<i>Nothing new.</i>")
            continue
        lines = [f"<b>{title}</b>"]
        for ts, t, link, src in items:
            meta = " · ".join(x for x in (src, age(ts)) if x)
            lines.append(f"• <a href=\"{html.escape(link)}\">{html.escape(t)}</a>"
                         + (f" <i>({html.escape(meta)})</i>" if meta else ""))
        parts.append("\n".join(lines))
    return parts


def pack(parts: list, limit: int = 3900) -> list:
    """Group blocks into messages under Telegram's 4096-char limit,
    never cutting through a block (or a line) mid-way."""
    msgs, cur = [], ""
    for part in parts:
        chunks = [part] if len(part) <= limit else part.split("\n")
        for c in chunks:
            if cur and len(cur) + 2 + len(c) > limit:
                msgs.append(cur)
                cur = c
            else:
                cur = f"{cur}\n\n{c}" if cur and c is part else (f"{cur}\n{c}" if cur else c)
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

    messages = pack(build_parts())
    if args.preview:
        print("\n\n----- next message -----\n\n".join(messages))
    else:
        for m in messages:
            send_telegram(m)
            time.sleep(1)
        print(f"Sent {len(messages)} message(s).")
