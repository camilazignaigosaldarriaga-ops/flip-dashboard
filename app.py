"""
Market Pulse — FastAPI + WebSocket live terminal.
Run: uvicorn app:app --port 8502
"""

import asyncio
import json
import re
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path

import requests
import yfinance as yf
from bs4 import BeautifulSoup
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ── Constants ──────────────────────────────────────────────────────────────────

TICKERS = {
    "S&P 500":      "^GSPC",
    "Dow Jones":    "^DJI",
    "Nasdaq":       "^IXIC",
    "Russell 2000": "^RUT",
    "VIX":          "^VIX",
    "Gold":         "GC=F",
    "WTI Oil":      "CL=F",
    "Bitcoin":      "BTC-USD",
}

CHART_TABS = [
    {"label": "SPX",  "symbol": "^GSPC",   "name": "S&P 500"},
    {"label": "DJI",  "symbol": "^DJI",    "name": "Dow Jones"},
    {"label": "NDX",  "symbol": "^IXIC",   "name": "Nasdaq"},
    {"label": "RUT",  "symbol": "^RUT",    "name": "Russell 2000"},
    {"label": "VIX",  "symbol": "^VIX",    "name": "VIX"},
    {"label": "GOLD", "symbol": "GC=F",    "name": "Gold"},
    {"label": "OIL",  "symbol": "CL=F",    "name": "WTI Oil"},
    {"label": "BTC",  "symbol": "BTC-USD", "name": "Bitcoin"},
]

YAHOO_URL = (
    "https://finance.yahoo.com/markets/live/"
    "stock-market-today-thursday-june-11-dow-sp-500-nasdaq-222511784.html"
)
UA      = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b")

# ── Shared state ───────────────────────────────────────────────────────────────

_prices:  dict = {}
_news:    list = []
_clients: set[WebSocket] = set()

# ── Sync helpers (run in thread pool) ─────────────────────────────────────────

def _fetch_prices_sync() -> dict:
    out = {}
    for name, sym in TICKERS.items():
        try:
            df = yf.Ticker(sym).history(period="1d", interval="1m")
            if df.empty:
                continue
            open_p = float(df["Close"].iloc[0])
            cur    = float(df["Close"].iloc[-1])
            pct    = (cur - open_p) / open_p * 100
            out[sym] = {"name": name, "symbol": sym,
                        "price": round(cur, 2), "pct": round(pct, 3),
                        "open": round(open_p, 2)}
        except Exception:
            pass
    return out


def _fetch_chart_sync(sym: str) -> dict | None:
    try:
        df = yf.Ticker(sym).history(period="1d", interval="1m")
        if df.empty:
            return None
        open_p = float(df["Close"].iloc[0])
        return {
            "symbol": sym,
            "open":   round(open_p, 2),
            "timestamps": [t.isoformat() for t in df.index],
            "prices":     [round(float(p), 2) for p in df["Close"]],
        }
    except Exception:
        return None


def _latest_cnbc_url() -> str:
    today = date.today()
    for delta in range(8):
        d   = today - timedelta(days=delta)
        url = (f"https://www.cnbc.com/{d.year}/{d.month:02d}/{d.day:02d}"
               "/stock-market-today-live-updates.html")
        try:
            r = requests.head(url, timeout=5, headers={"User-Agent": UA},
                              allow_redirects=True)
            if r.status_code == 200:
                return url
        except Exception:
            continue
    return "https://www.cnbc.com/2026/06/11/stock-market-today-live-updates.html"


def _scrape_news_sync() -> list:
    from playwright.sync_api import sync_playwright
    news = []

    def render(url: str) -> str:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page    = browser.new_context(user_agent=UA).new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(5_000)
            html = page.content()
            browser.close()
        return html

    try:
        soup = BeautifulSoup(render(YAHOO_URL), "lxml")
        for post in soup.select(".liveblogposts-post")[:15]:
            h     = post.find(["h2","h3","h4"])
            title = h.get_text(strip=True) if h else ""
            if not title:
                continue
            paras = post.find_all("p")
            body  = " ".join(p.get_text(strip=True) for p in paras[:2])[:280]
            m     = TIME_RE.search(post.get_text())
            news.append({"source": "Yahoo", "title": title, "body": body,
                         "time": m.group(0) if m else "", "id": abs(hash(title))})
    except Exception as e:
        print(f"[news] Yahoo error: {e}")

    try:
        cnbc_url = _latest_cnbc_url()
        soup = BeautifulSoup(render(cnbc_url), "lxml")
        for post in soup.select(".LiveBlogBody-post")[:15]:
            sub   = post.select_one(".LiveBlogBody-subtitle")
            title = sub.get_text(strip=True) if sub else ""
            if not title:
                continue
            ts_el = post.select_one(".LiveBlogTimestamp-time")
            ts    = ts_el.get_text(strip=True) if ts_el else ""
            paras = post.find_all("p")
            body  = " ".join(p.get_text(strip=True) for p in paras[:2])[:280]
            news.append({"source": "CNBC", "title": title, "body": body,
                         "time": ts, "id": abs(hash(title))})
    except Exception as e:
        print(f"[news] CNBC error: {e}")

    return news

# ── Broadcast ──────────────────────────────────────────────────────────────────

async def _broadcast(msg: dict) -> None:
    if not _clients:
        return
    text = json.dumps(msg)
    dead: set[WebSocket] = set()
    for ws in list(_clients):
        try:
            await ws.send_text(text)
        except Exception:
            dead.add(ws)
    _clients.difference_update(dead)

# ── Background loops ───────────────────────────────────────────────────────────

async def _price_loop():
    global _prices
    while True:
        try:
            data = await asyncio.to_thread(_fetch_prices_sync)
            _prices.update(data)
            await _broadcast({"type": "prices", "data": _prices})
            print(f"[prices] updated {len(_prices)} tickers")
        except Exception as e:
            print(f"[prices] error: {e}")
        await asyncio.sleep(30)


async def _news_loop():
    global _news
    while True:
        try:
            data = await asyncio.to_thread(_scrape_news_sync)
            _news.clear()
            _news.extend(data)
            await _broadcast({"type": "news", "data": _news})
            print(f"[news] updated {len(_news)} items")
        except Exception as e:
            print(f"[news] error: {e}")
        await asyncio.sleep(300)

# ── App ────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_: FastAPI):
    asyncio.create_task(_price_loop())
    asyncio.create_task(_news_loop())
    yield

app = FastAPI(lifespan=lifespan)

_static = Path(__file__).parent / "static"
_static.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static)), name="static")


@app.get("/")
async def root():
    return FileResponse(str(_static / "index.html"))


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    _clients.add(ws)

    # Send initial state immediately
    await ws.send_text(json.dumps({
        "type":   "init",
        "prices": _prices,
        "news":   _news,
        "tabs":   CHART_TABS,
    }))

    # Send default chart (S&P 500)
    chart = await asyncio.to_thread(_fetch_chart_sync, "^GSPC")
    if chart:
        await ws.send_text(json.dumps({"type": "chart", **chart}))

    try:
        async for raw in ws.iter_text():
            msg = json.loads(raw)
            if msg.get("type") == "chart_request":
                sym   = msg.get("symbol", "^GSPC")
                data  = await asyncio.to_thread(_fetch_chart_sync, sym)
                if data:
                    await ws.send_text(json.dumps({"type": "chart", **data}))
    except WebSocketDisconnect:
        pass
    finally:
        _clients.discard(ws)
