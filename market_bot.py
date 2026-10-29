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

# Headlines only from these sites.
SITES = ["reuters.com", "bloomberg.com"]
LOOKBACK = "1d"   # how far back to search: "12h", "1d", "2d" ...

# Each section runs several Google News searches (restricted to SITES),
# merges them, removes duplicates and keeps the newest `max` headlines.
# Use OR between alternatives and "quotes" for exact phrases.
SECTIONS = [
    {"title": "🇯🇵 USD/JPY", "max": 6, "queries": [
        'yen (intervention OR "excessive moves" OR "speculative moves" OR "finance ministry")',
        '"Bank of Japan" OR BOJ',
        'Japan (CPI OR inflation OR wages OR JGB)',
        '"Federal Reserve" OR "US CPI" OR "US inflation" OR "Treasury yields"',
    ]},
    {"title": "🇦🇺🇨🇳 Australia / China", "max": 6, "queries": [
        'RBA OR "Reserve Bank of Australia" OR "Australian dollar"',
        'Australia (CPI OR inflation OR jobs OR unemployment)',
        'China (economy OR stimulus OR PBOC OR yuan OR property)',
        '"iron ore" OR "China exports" OR "China trade"',
    ]},
    {"title": "🇧🇷 Brazil", "max": 5, "queries": [
        'Brazil (fiscal OR budget OR deficit OR spending OR debt)',
        'Brazil (Selic OR Copom OR "central bank" OR "Brazil real")',
    ]},
    {"title": "🇲🇽 Mexico", "max": 5, "queries": [
        'Mexico (tariff OR tariffs OR trade OR USMCA OR "trade policy")',
        'Mexico (peso OR Banxico OR "central bank")',
    ]},
    {"title": "📈 Global equities", "max": 4, "queries": [
        'stocks ("Wall Street" OR "global markets" OR selloff OR rally OR "record high")',
    ]},
    {"title": "🌍 Big-picture world news", "max": 6, "queries": [
        'war OR ceasefire OR invasion OR coup OR sanctions',
        'tariffs OR "trade war" OR "export controls"',
        '"snap election" OR resigns OR impeachment OR "state of emergency"',
        'default OR "debt crisis" OR "bank failure" OR "emergency rate"',
    ]},
]

# ==========================================================================


def fetch_quotes(symbols: dict) -> dict:
    """Return {name: (last, pct_change_vs_prev_close) or None}."""
    data = None
    for attempt in range(3):  # Yahoo occasionally rate-limits; retry gently
        try:
            data = yf.download(list(symbols.values()), period="7d", interval="1d",
                               progress=False, auto_adjust=False, threads=False)
            if data is not None and not data.empty:
                break
        except Exception as e:
            print(f"[prices] attempt {attempt + 1} failed: {e}", file=sys.stderr)
        time.sleep(5 * (attempt + 1))

    out = {}
    for name, sym in symbols.items():
        try:
            series = data["Close"][sym].dropna()
            last, prev = float(series.iloc[-1]), float(series.iloc[-2])
            out[name] = (last, (last / prev - 1) * 100)
        except Exception:
            out[name] = None
    return out


def search_news(query: str) -> list:
    """One Google News search -> list of (published_ts, title, link, source)."""
    sites = " OR ".join(f"site:{s}" for s in SITES)
    q = quote_plus(f"({sites}) {query} when:{LOOKBACK}")
    url = f"https://news.google.com/rss/search?q={q}&hl=en-SG&gl=SG&ceid=SG:en"
    items = []
    try:
        for e in feedparser.parse(url).entries:
            title = e.title.rsplit(" - ", 1)[0].strip()   # drop " - Reuters"
            source = e.get("source", {}).get("title", "")
            ts = calendar.timegm(e.published_parsed) if e.get("published_parsed") else 0
            items.append((ts, title, e.link, source))
    except Exception as ex:
        print(f"[news] '{query}' failed: {ex}", file=sys.stderr)
    return items


def fetch_sections() -> list:
    """Return [(title, [(ts, title, link, source), ...]), ...]."""
    seen, out = set(), []
    for sec in SECTIONS:
        pool = []
        for q in sec["queries"]:
            pool += search_news(q)
            time.sleep(0.5)
        pool.sort(key=lambda x: x[0], reverse=True)       # newest first
        picked = []
        for item in pool:
            key = item[1].lower()
            if key in seen:                                # skip duplicates
                continue
            seen.add(key)
            picked.append(item)
            if len(picked) >= sec["max"]:
                break
        out.append((sec["title"], picked))
    return out


def age(ts: int) -> str:
    if not ts:
        return ""
    mins = max(0, int((time.time() - ts) / 60))
    return f"{mins}m" if mins < 60 else f"{mins // 60}h"


def fmt_price_table(quotes: dict, kind: str) -> str:
    lines = []
    for name, q in quotes.items():
        if q is None:
            lines.append(f"{name:<10} {'n/a':>11}")
            continue
        last, chg = q
        decimals = (2 if "JPY" in name else 4) if kind == "fx" else 2
        arrow = "▲" if chg > 0 else ("▼" if chg < 0 else "•")
        lines.append(f"{name:<10} {last:>11,.{decimals}f}  {arrow}{chg:+.2f}%")
    return "<pre>" + html.escape("\n".join(lines)) + "</pre>"


def build_parts() -> list:
    """Message blocks; packed into Telegram-sized messages later."""
    now = dt.datetime.now(ZoneInfo(TIMEZONE))
    label = "🌅 Morning brief" if now.hour < 12 else "🌙 Evening brief"
    parts = [f"<b>{label}</b> · {now:%a %d %b, %H:%M}"]

    prices = fetch_quotes({**FX, **INDICES})
    parts.append("<b>💱 FX</b>\n" + fmt_price_table({k: prices[k] for k in FX}, "fx"))
    parts.append("<b>📊 Indices</b>\n" + fmt_price_table({k: prices[k] for k in INDICES}, "idx"))
    parts.append("<i>Prices via Yahoo Finance (may be delayed); % vs previous close.</i>")

    for title, items in fetch_sections():
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
