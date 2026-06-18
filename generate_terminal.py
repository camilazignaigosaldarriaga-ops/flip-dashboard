"""
Generate a self-contained market_pulse.html (no server needed).
Charts powered by @tradingview/lightweight-charts (v4).
All data pre-fetched and embedded at generation time.

Run: python generate_terminal.py
"""

import json
import re
import sys
import os
import pandas as pd
from datetime import datetime, date, timedelta
from pathlib import Path

import requests
import yfinance as yf
from bs4 import BeautifulSoup

# Integración con liveblog_scraper ────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from liveblog_scraper import scrape_cnbc as _lb_scrape_cnbc, scrape_yahoo as _lb_scrape_yahoo
    _LB_SCRAPER_OK = True
except ImportError:
    _LB_SCRAPER_OK = False

UA      = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b")

TICKERS = {
    "^GSPC":   "S&P 500",
    "^DJI":    "Dow Jones",
    "^IXIC":   "Nasdaq",
    "^RUT":    "Russell 2000",
    "^VIX":    "VIX",
    "GC=F":    "Gold",
    "CL=F":    "WTI Oil",
    "BTC-USD": "Bitcoin",
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

STOCK_TICKERS = [
    "AAPL", "MSFT", "NVDA", "TSLA", "GOOGL", "META", "AMZN",
    "JPM", "BAC", "GS", "V", "MA", "NFLX", "AMD", "INTC",
    "QCOM", "XOM", "LLY", "WMT", "COIN", "DIS", "F", "GM",
    # Sector ETFs (heatmap)
    "XLK", "XLE", "XLF", "XLV", "XLI", "XLB", "XLU", "XLP", "XLY", "XLRE", "XLC",
]

PERIODS = {
    "1D":  ("1d",   "1m"),   # 1-minute bars
    "5D":  ("5d",   "1h"),   # 1-hour bars
    "1M":  ("1mo",  "1h"),   # 1-hour bars
    "6M":  ("6mo",  "1d"),   # daily bars
    "YTD": ("ytd",  "1d"),   # daily bars
    "1Y":  ("1y",   "1d"),   # daily bars
    "5Y":  ("5y",   "1d"),   # daily bars
    "MAX": ("max",  "1mo"),  # monthly bars
}

YAHOO_LIVE_SECTION = "https://finance.yahoo.com/markets/live/"   # redirects to active article
YAHOO_MARKETS_HUB  = "https://finance.yahoo.com/markets/"
YAHOO_TOPIC_PAGE   = "https://finance.yahoo.com/topic/stock-market-news/"


# ── Data fetching ──────────────────────────────────────────────────────────────

def latest_cnbc_url() -> str:
    """Find the most recent CNBC stock-market-today live blog URL."""
    today = date.today()
    for delta in range(10):
        d = today - timedelta(days=delta)
        url = (f"https://www.cnbc.com/{d.year}/{d.month:02d}/{d.day:02d}"
               "/stock-market-today-live-updates.html")
        try:
            r = requests.get(url, timeout=8, headers={"User-Agent": UA},
                             allow_redirects=True, stream=True)
            if r.status_code != 200:
                r.close()
                continue
            # Read first 100 KB to verify it's actually a live blog page
            chunk = b""
            for piece in r.iter_content(4096):
                chunk += piece
                if len(chunk) >= 102_400:
                    break
            r.close()
            # Accept if the page has any live-blog markers
            markers = (b"LiveBlog", b"live-blog", b"liveblog",
                       b"market-today", b"stock-market")
            if any(m in chunk for m in markers):
                print(f"    CNBC: {d.strftime('%Y-%m-%d')} ✓")
                return url
            print(f"    CNBC: {d.strftime('%Y-%m-%d')} — exists but no live-blog content")
        except Exception as e:
            print(f"    CNBC: {d.strftime('%Y-%m-%d')} — {e}")
    # Last-resort canonical redirect
    return "https://www.cnbc.com/live-news/stock-market-today-live-updates/"


def fetch_prices() -> dict:
    out = {}
    for sym, name in TICKERS.items():
        try:
            df = yf.Ticker(sym).history(period="1d", interval="1m")
            if df.empty:
                continue
            cur    = float(df["Close"].iloc[-1])
            open_p = float(df["Close"].iloc[0])
            # Use previous session close as baseline (same as Yahoo/CNBC show)
            try:
                df_prev    = yf.Ticker(sym).history(period="5d", interval="1d")
                prev_close = float(df_prev["Close"].iloc[-2]) if len(df_prev) >= 2 else open_p
            except Exception:
                prev_close = open_p
            pct = (cur - prev_close) / prev_close * 100
            out[sym] = {"name": name, "symbol": sym,
                        "price": round(cur, 2), "pct": round(pct, 3),
                        "open": round(open_p, 2)}
            print(f"    ✓ {name}")
        except Exception as e:
            print(f"    ✗ {name}: {e}")
    return out


def fetch_chart(sym: str, period_key: str) -> dict | None:
    period, interval = PERIODS[period_key]
    try:
        df = yf.Ticker(sym).history(period=period, interval=interval)
        if df.empty:
            return None
        df = df.dropna(subset=["Close"])
        if df.empty:
            return None

        if period_key == "1D":
            try:
                df_prev = yf.Ticker(sym).history(period="5d", interval="1d")
                prev_close = round(float(df_prev["Close"].iloc[-2]), 4) \
                             if len(df_prev) >= 2 else round(float(df["Close"].iloc[0]), 4)
            except Exception:
                prev_close = round(float(df["Close"].iloc[0]), 4)
        else:
            prev_close = round(float(df["Close"].iloc[0]), 4)

        volumes = []
        for v in df.get("Volume", pd.Series(dtype=float)):
            try:
                volumes.append(int(v) if not pd.isna(v) else 0)
            except Exception:
                volumes.append(0)

        def safe_ohlc(col):
            if col not in df.columns:
                return None
            out = []
            for v in df[col]:
                try:
                    out.append(round(float(v), 4) if not pd.isna(v) else None)
                except Exception:
                    out.append(None)
            return out

        return {
            "symbol":     sym,
            "period":     period_key,
            "prev_close": prev_close,
            "timestamps": [t.isoformat() for t in df.index],
            "opens":      safe_ohlc("Open"),
            "highs":      safe_ohlc("High"),
            "lows":       safe_ohlc("Low"),
            "closes":     [round(float(p), 4) for p in df["Close"]],
            "volumes":    volumes,
        }
    except Exception as e:
        print(f"    ✗ {sym} {period_key}: {e}")
        return None


def fetch_all_charts() -> dict:
    all_charts = {}
    for sym, name in TICKERS.items():
        all_charts[sym] = {}
        print(f"  {name}:")
        for pk in PERIODS:
            data = fetch_chart(sym, pk)
            if data:
                all_charts[sym][pk] = data
                print(f"    ✓ {pk} ({len(data['timestamps'])} pts)")
            else:
                print(f"    ✗ {pk} — no data")
    return all_charts


def fetch_stock_prices() -> dict:
    out = {}
    try:
        for sym in STOCK_TICKERS:
            df = yf.Ticker(sym).history(period="5d", interval="1d")
            df = df[df["Close"].notna()]
            if len(df) < 2:
                continue
            prev, cur = float(df["Close"].iloc[-2]), float(df["Close"].iloc[-1])
            out[sym] = {"price": round(cur, 2), "pct": round((cur - prev) / prev * 100, 3)}
    except Exception as e:
        print(f"  ✗ stocks: {e}")
    print(f"  ✓ {len(out)} stocks")
    return out


def _lb_post_to_news_item(post) -> dict:
    """Convierte un LiveBlogPost al formato dict de INIT_NEWS."""
    time_str = post.timestamp_raw or ""
    if not time_str and post.timestamp_iso:
        time_str = post.timestamp_iso[11:16] + " UTC"
    return {
        "id":     post.post_id,
        "source": post.source,
        "title":  post.headline,
        "body":   post.body[:700],
        "time":   time_str,
        "url":    post.article_url,
    }


def fetch_news() -> list:
    """
    Extrae noticias del live blog de Yahoo Finance y CNBC.
    Usa liveblog_scraper si está disponible (mejor calidad);
    si no, cae al scraper interno con Playwright directo.
    """
    if _LB_SCRAPER_OK:
        return _fetch_news_via_scraper()
    return _fetch_news_internal()


def _fetch_news_via_scraper() -> list:
    """Delega la extracción a liveblog_scraper (UA rotatorio, dedup, retry)."""
    news = []

    print("  Scraping Yahoo Finance…")
    try:
        posts = _lb_scrape_yahoo()
        for p in posts:
            news.append(_lb_post_to_news_item(p))
        print(f"  ✓ Yahoo: {len(posts)} posts")
    except Exception as e:
        print(f"  ✗ Yahoo: {e}")

    before = len(news)
    print("  Scraping CNBC…")
    try:
        posts = _lb_scrape_cnbc()
        for p in posts:
            news.append(_lb_post_to_news_item(p))
        print(f"  ✓ CNBC: {len(news) - before} posts")
    except Exception as e:
        print(f"  ✗ CNBC: {e}")

    return news


def _fetch_news_internal() -> list:
    """Scraper interno original con Playwright (fallback)."""
    from playwright.sync_api import sync_playwright
    news = []

    import re as _re

    def url_date_score(url: str) -> int:
        """Orden DOM = más reciente primero; sólo se usa si hay redirect."""
        return 0  # DOM order ya es correcto, no reordenar

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=UA,
            extra_http_headers={"Cache-Control": "no-cache", "Pragma": "no-cache"},
        )

        # ── Yahoo Finance ──────────────────────────────────────────────────────
        try:
            print("  Scraping Yahoo Finance…")
            page = ctx.new_page()

            # Navigate directly to the live-blog SECTION URL.
            # Yahoo typically redirects this to the active live article.
            page.goto(YAHOO_LIVE_SECTION, wait_until="domcontentloaded", timeout=35_000)
            page.wait_for_timeout(4_000)

            final_url = page.url
            slug = final_url.rstrip("/").split("/")[-1]
            print(f"    → landed: {slug[:70]}")

            def collect_live_links(pg) -> list:
                return pg.evaluate("""
                    () => [...new Set(
                        Array.from(document.querySelectorAll('a[href]'))
                            .map(a => a.href)
                            .filter(h => h.includes('/markets/live/stock-market') ||
                                         /finance\\.yahoo\\.com.*stock-market-today/.test(h))
                    )]
                """)

            # Determine if we actually landed on a live article
            # (article URLs have a long slug with hyphens, not just "markets" or "live")
            landed_on_article = (
                "finance.yahoo.com/markets/live/" in final_url
                and len(slug) > 10
                and "-" in slug
            )

            if not landed_on_article:
                # We're on a hub/section page — find links and navigate to the best one
                all_links = collect_live_links(page)

                if not all_links:
                    # Try the markets hub explicitly
                    page.goto(YAHOO_MARKETS_HUB, wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_timeout(3_000)
                    all_links = collect_live_links(page)

                all_links.sort(key=url_date_score, reverse=True)
                if all_links:
                    best = all_links[0]
                    print(f"    → best link: {best.rstrip('/').split('/')[-1][:70]}")
                    page.goto(best, wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_timeout(5_000)
                else:
                    print("    → no live link found, using topic page")
                    page.goto(YAHOO_TOPIC_PAGE, wait_until="domcontentloaded", timeout=35_000)
                    page.wait_for_timeout(4_000)

            soup = BeautifulSoup(page.content(), "lxml")
            page.close()

            # Main article headline — og:title is most reliable (first H1 is site nav "Yahoo Finance")
            main_title = ""
            og = soup.find("meta", {"property": "og:title"})
            if og:
                main_title = og.get("content", "").strip()
            if not main_title:
                title_el = soup.find("title")
                if title_el:
                    main_title = title_el.get_text(strip=True).split(" | ")[0].strip()
            if not main_title:
                # Fallback: scan all H1s for article headline (skip nav "Yahoo Finance")
                for h1 in soup.find_all("h1"):
                    t = h1.get_text(strip=True)
                    if "stock market" in t.lower() and len(t) > 20:
                        main_title = t
                        break
            if main_title and "stock market" in main_title.lower():
                print(f"    → article title: {main_title[:80]}")
                news.append({"source": "Yahoo", "title": main_title,
                             "body": "Live market coverage", "time": "LIVE",
                             "url": page.url})

            # Individual live-blog posts
            posts = (soup.select(".liveblogposts-post") or
                     soup.select("[class*='liveblog'] li") or
                     soup.select("li.js-stream-content"))

            for post in posts[:20]:
                h     = post.find(["h2", "h3", "h4"])
                title = h.get_text(strip=True) if h else ""
                if not title:
                    continue
                paras = post.find_all("p")
                body  = " ".join(p.get_text(strip=True) for p in paras[:6])[:700]
                m     = TIME_RE.search(post.get_text())
                # Try to find a direct anchor link within the post
                a_el  = post.find("a", href=True)
                post_url = a_el["href"] if a_el and a_el["href"].startswith("http") else page.url
                news.append({"source": "Yahoo", "title": title, "body": body,
                             "time": m.group(0) if m else "", "url": post_url})
            print(f"  ✓ Yahoo: {len(news)} posts")
        except Exception as e:
            print(f"  ✗ Yahoo: {e}")

        # ── CNBC ──────────────────────────────────────────────────────────────
        before = len(news)
        try:
            cnbc_url = latest_cnbc_url()
            print(f"  Scraping CNBC…")
            page = ctx.new_page()
            page.goto(cnbc_url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(5_000)

            # Log final URL in case of redirect
            final_cnbc = page.url
            if final_cnbc != cnbc_url:
                print(f"    → redirected: {final_cnbc.rstrip('/').split('/')[-1][:60]}")

            soup = BeautifulSoup(page.content(), "lxml")
            page.close()

            # Main article headline — og:title is most descriptive for CNBC
            cnbc_main = ""
            og_cnbc = soup.find("meta", {"property": "og:title"})
            if og_cnbc:
                cnbc_main = og_cnbc.get("content", "").strip()
            if not cnbc_main:
                title_el = soup.find("title")
                if title_el:
                    cnbc_main = title_el.get_text(strip=True).split(" | ")[0].strip()
            if cnbc_main and len(cnbc_main) > 10 and "stock market" not in cnbc_main.lower():
                # og:title is a real descriptive headline (not the generic "Stock market today: Live updates")
                pass
            if cnbc_main and len(cnbc_main) > 10:
                print(f"    → article title: {cnbc_main[:80]}")
                news.append({"source": "CNBC", "title": cnbc_main,
                             "body": "Live market coverage", "time": "LIVE",
                             "url": cnbc_url})

            # CNBC changes class names occasionally — try multiple selectors
            cnbc_posts = (
                soup.select(".LiveBlogBody-post") or
                soup.select("[class*='LiveBlogBody'] [class*='post']") or
                soup.select("article[class*='LiveBlog']") or
                soup.select("[class*='live-blog'] article") or
                soup.select("[class*='liveBlog'] section")
            )

            for post in cnbc_posts[:20]:
                title_el = (
                    post.select_one(".LiveBlogBody-subtitle") or
                    post.select_one("[class*='subtitle']") or
                    post.select_one("h2, h3, h4")
                )
                title = title_el.get_text(strip=True) if title_el else ""
                if not title:
                    continue
                ts_el = (
                    post.select_one(".LiveBlogTimestamp-time") or
                    post.select_one("[class*='Timestamp']") or
                    post.select_one("time")
                )
                ts    = ts_el.get_text(strip=True) if ts_el else ""
                paras = post.find_all("p")
                body  = " ".join(p.get_text(strip=True) for p in paras[:6])[:700]
                a_el  = post.find("a", href=True)
                post_url = a_el["href"] if a_el and a_el["href"].startswith("http") else cnbc_url
                news.append({"source": "CNBC", "title": title, "body": body,
                             "time": ts, "url": post_url})
            print(f"  ✓ CNBC: {len(news) - before} posts")
        except Exception as e:
            print(f"  ✗ CNBC: {e}")

        browser.close()

    return news


# ── Market events analysis (Gemini) ───────────────────────────────────────────

def analyze_market_events(news_items: list, prices: dict) -> list:
    """Calls Gemini Flash to analyze live blog posts and extract market-moving events."""
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.strip().startswith("GEMINI_API_KEY="):
                        api_key = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                        break
    if not api_key:
        print("  [análisis] GEMINI_API_KEY no configurado — embebiendo lista vacía")
        return []
    try:
        from google import genai
    except ImportError:
        print("  [análisis] google-genai no instalado")
        return []

    cnbc_items  = [n for n in news_items if n.get("source") == "CNBC"]
    yahoo_items = [n for n in news_items if n.get("source") == "Yahoo Finance"]

    blocks = []
    if cnbc_items:
        blocks.append("=== CNBC (prioridad) ===")
        for it in cnbc_items:
            blocks.append(f"\n[{it.get('time','')}] {it['title']}\n{it.get('body','')[:500]}")
    if yahoo_items:
        blocks.append("\n=== Yahoo Finance ===")
        for it in yahoo_items:
            blocks.append(f"\n[{it.get('time','')}] {it['title']}\n{it.get('body','')[:350]}")

    blog_text = "\n".join(blocks)[:13000]
    today_str = datetime.now().strftime("%A, %B %d %Y")
    spx_pct   = prices.get("^GSPC", {}).get("pct", "")
    spx_ctx   = f" S&P 500 acumulado del día: {spx_pct}." if spx_pct else ""

    prompt = f"""Eres un analista financiero senior. Responde ÚNICAMENTE con JSON válido, sin texto adicional.

Hoy es {today_str}.{spx_ctx}

Analiza el siguiente contenido editorial de los live blogs "Stock Market Today" de CNBC (prioridad) y Yahoo Finance.
Solo considera el contenido escrito por el equipo editorial del medio. Ignora citas de analistas externos o portavoces de empresas — solo úsalos como contexto si el medio los referencia como causa de un movimiento de mercado.

{blog_text}

---

Extrae los 6-8 eventos que más han movido al S&P 500 u otros activos del mercado hoy.

CATEGORÍAS DE ALTA PRIORIDAD — si aparecen en el texto, SIEMPRE inclúyelas primero:
1. Resultados corporativos trimestrales: earnings, EPS, revenue, guidance de grandes empresas públicas; beats o misses relevantes
2. Publicaciones económicas: PIB, inflación/IPC/PCE, tasa de interés, reporte de empleo/nóminas no agrícolas, desempleo
3. Noticias sobre petróleo: precio del crudo, decisiones de la OPEP+, inventarios de petróleo, producción
4. Reserva Federal (Fed): reuniones del FOMC, decisiones de tasas, declaraciones de Powell u otros miembros, minutas
5. IPOs importantes: salidas a bolsa de empresas con valuación superior a 1 trillón USD

Reglas generales:
- El titular describe el HECHO concreto, no la reacción del mercado (ej: "Fed mantiene tasas", no "Mercado sube tras decisión Fed")
- spx_impact: extrae literalmente del texto la cifra de cambio en S&P 500 o en el activo más relevante; si no hay cifra exacta, describe brevemente el efecto
- Incluye solo eventos con impacto verificable en el texto; no inventes datos
- Ordena por impacto: primero las categorías de alta prioridad, luego el resto

Devuelve ÚNICAMENTE un array JSON (sin markdown, sin texto extra):
[{{"headline":"max 80 chars","detail":"1-2 oraciones sobre qué pasó y por qué importa, max 180 chars","spx_impact":"max 45 chars, ej: S&P +0.4%, Tech -1.2%, Oil -3%","direction":"up|down|neutral","time_et":"hora ET o vacío","source":"CNBC|Yahoo Finance|Ambos"}}]"""

    try:
        client = genai.Client(api_key=api_key)
        resp   = client.models.generate_content(model="gemini-flash-lite-latest", contents=prompt)
        raw    = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        events = json.loads(raw)
        if isinstance(events, list):
            print(f"  ✓ {len(events)} eventos de mercado identificados (Gemini)")
            return events
    except Exception as e:
        print(f"  ✗ Error en análisis Gemini: {e}")
    return []


# ── HTML builder ───────────────────────────────────────────────────────────────

def build_html(prices, all_charts, news, tabs, stock_prices, generated_at, events=None) -> str:
    prices_json     = json.dumps(prices)
    all_charts_json = json.dumps(all_charts)
    news_json       = json.dumps(news)
    events_json     = json.dumps(events or [])
    tabs_json       = json.dumps(tabs)
    ticker_order    = json.dumps(list(TICKERS.keys()))
    stock_prices_json = json.dumps(stock_prices)
    stock_syms_json   = json.dumps(STOCK_TICKERS)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Market Pulse</title>

  <script>if(localStorage.getItem('mkt-theme')==='light')document.documentElement.classList.add('light');</script>

  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet" />

  <!-- TradingView Lightweight Charts v4 -->
  <script src="https://unpkg.com/lightweight-charts@4.1.3/dist/lightweight-charts.standalone.production.js"></script>

  <style>
    :root {{
      --bg-base:#070b12; --bg-panel:#0c1118; --bg-card:#101620; --bg-hover:#141c28;
      --border:#1a2535; --border-hi:#243040; --text:#b8cce0; --text-muted:#506070;
      --text-dim:#28384a; --green:#008144; --red:#d12121; --accent:#1a7acc;
      --accent-glow:rgba(26,122,204,0.15); --mono:'JetBrains Mono',monospace;
      --chart-bg:#070b12; --chart-grid:#1a2535; --chart-tick:#4a6080;
      --chart-cross:rgba(148,163,184,0.4); --chart-cross-label:#0f1825;
    }}
    :root.light {{
      --bg-base:#f1f5fb; --bg-panel:#ffffff; --bg-card:#f6f9ff; --bg-hover:#edf1fb;
      --border:#d8e2f0; --border-hi:#b8c8e0; --text:#1a2840; --text-muted:#607090;
      --text-dim:#b0c0d8; --green:#008144; --red:#d12121; --accent:#1255b0;
      --accent-glow:rgba(18,85,176,0.10);
      --chart-bg:#f8fafc; --chart-grid:#e2e8f0; --chart-tick:#64748b;
      --chart-cross:rgba(100,116,139,0.6); --chart-cross-label:#1e293b;
    }}
    *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
    html,body{{height:100%;overflow:hidden}}
    body{{font-family:'Inter',-apple-system,sans-serif;background:var(--bg-base);color:var(--text);display:flex;flex-direction:column}}

    /* ── top bar ── */
    .topbar{{height:52px;flex-shrink:0;display:flex;align-items:stretch;background:var(--bg-panel);border-bottom:1px solid var(--border)}}
    .logo{{display:flex;align-items:center;gap:9px;padding:0 20px;border-right:1px solid var(--border);white-space:nowrap;user-select:none}}
    .live-dot{{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 6px var(--green);animation:pulse 2.5s ease-in-out infinite}}
    .logo-text{{font-size:.78rem;font-weight:700;letter-spacing:.12em;text-transform:uppercase;color:var(--text)}}
    .tickers-bar{{display:flex;align-items:center;flex:1;gap:2px;padding:0 8px;overflow-x:auto;scrollbar-width:none}}
    .tickers-bar::-webkit-scrollbar{{display:none}}
    .tick{{display:flex;flex-direction:column;align-items:flex-start;padding:5px 10px;border-radius:5px;cursor:pointer;border:1px solid transparent;transition:border-color .2s,background .2s;min-width:90px;flex-shrink:0}}
    .tick:hover{{background:var(--bg-hover);border-color:var(--border-hi)}}
    .tick.active{{border-color:var(--accent);background:var(--accent-glow)}}
    .tick-name{{font-size:.6rem;font-weight:700;letter-spacing:.07em;text-transform:uppercase;color:var(--text-muted);margin-bottom:2px}}
    .tick-price{{font-family:var(--mono);font-size:.82rem;font-weight:600;color:var(--text);line-height:1}}
    .tick-pct{{font-family:var(--mono);font-size:.68rem;font-weight:600}}
    .up{{color:var(--green)}} .dn{{color:var(--red)}}
    .topbar-right{{display:flex;align-items:center;gap:12px;padding:0 16px;border-left:1px solid var(--border);flex-shrink:0}}
    #clock{{font-family:var(--mono);font-size:.72rem;color:var(--text-muted);white-space:nowrap}}
    #upd-time{{font-size:.62rem;color:var(--text-dim);white-space:nowrap}}
    .theme-btn{{display:flex;align-items:center;gap:5px;padding:5px 11px;border-radius:14px;background:transparent;border:1px solid var(--border-hi);color:var(--text-muted);font-family:'Inter',sans-serif;font-size:.72rem;font-weight:500;cursor:pointer;transition:opacity .15s}}
    .theme-btn:hover{{opacity:.7}}

    /* ── layout ── */
    .main{{display:grid;grid-template-columns:1fr 210px;flex:1;min-height:0}}
    .news-view{{flex:1;min-height:0;display:none;justify-content:center;overflow:hidden;background:var(--bg-base)}}
    /* ── events panel (Noticias tab) ── */
    .events-panel{{width:700px;max-width:100%;display:flex;flex-direction:column;overflow-y:auto;border-left:1px solid var(--border);border-right:1px solid var(--border);background:var(--bg-panel)}}
    .events-header{{padding:14px 20px 12px;border-bottom:1px solid var(--border);flex-shrink:0;display:flex;align-items:center;justify-content:space-between}}
    .events-title{{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--text-muted)}}
    .events-meta{{display:flex;align-items:center;gap:10px;margin-top:5px}}
    .events-date{{font-size:.68rem;color:var(--text-muted)}}
    .events-spx-badge{{font-family:var(--mono);font-size:.78rem;font-weight:600;padding:2px 8px;border-radius:4px}}
    .events-spx-badge.up{{color:var(--green);background:rgba(0,129,68,.1);border:1px solid rgba(0,129,68,.25)}}
    .events-spx-badge.dn{{color:var(--red);background:rgba(209,33,33,.1);border:1px solid rgba(209,33,33,.25)}}
    .events-updated{{font-size:.6rem;color:var(--text-dim);font-family:var(--mono)}}
    .event-card{{padding:15px 20px;border-bottom:1px solid var(--border);display:grid;grid-template-columns:1fr auto;gap:14px;align-items:start}}
    .event-card:hover{{background:var(--bg-hover)}}
    .event-left{{display:flex;flex-direction:column;gap:5px;min-width:0}}
    .event-topmeta{{display:flex;align-items:center;gap:7px;flex-wrap:wrap}}
    .event-time{{font-family:var(--mono);font-size:.62rem;color:var(--text-dim)}}
    .event-src{{font-size:.58rem;font-weight:700;text-transform:uppercase;letter-spacing:.07em;padding:1px 6px;border-radius:3px;border:1px solid var(--border-hi);color:var(--text-muted)}}
    .event-src.cnbc{{border-color:rgba(26,122,204,.4);color:#1a7acc}}
    .event-src.yahoo{{border-color:rgba(90,50,200,.4);color:#7b3fe4}}
    .event-src.both{{border-color:rgba(0,129,68,.4);color:var(--green)}}
    .event-headline{{font-size:.84rem;font-weight:600;color:var(--text);line-height:1.35}}
    .event-detail{{font-size:.72rem;color:var(--text-muted);line-height:1.55}}
    .event-impact{{flex-shrink:0;padding-top:3px}}
    .impact-badge{{font-family:var(--mono);font-size:.7rem;font-weight:600;padding:4px 9px;border-radius:4px;white-space:nowrap;display:block;text-align:center}}
    .impact-badge.up{{color:var(--green);background:rgba(0,129,68,.1);border:1px solid rgba(0,129,68,.25)}}
    .impact-badge.dn{{color:var(--red);background:rgba(209,33,33,.1);border:1px solid rgba(209,33,33,.25)}}
    .impact-badge.neutral{{color:var(--text-muted);background:var(--bg-card);border:1px solid var(--border)}}
    .events-loading,.events-empty{{padding:48px 20px;text-align:center;font-size:.75rem;color:var(--text-dim);font-family:var(--mono)}}
    .events-empty small{{display:block;margin-top:8px;font-size:.65rem}}

    /* ── chart panel ── */
    .chart-panel{{display:flex;flex-direction:column;border-right:1px solid var(--border);min-height:0;background:var(--bg-base)}}
    .chart-header{{padding:14px 20px 10px;border-bottom:1px solid var(--border);flex-shrink:0}}
    .chart-header-top{{display:flex;align-items:flex-start;justify-content:space-between}}
    .chart-name{{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--text-muted);margin-bottom:5px}}
    .chart-price{{font-family:var(--mono);font-size:2rem;font-weight:700;letter-spacing:-.02em;line-height:1;margin-bottom:4px}}
    .chart-meta{{display:flex;align-items:center;gap:12px}}
    .chart-chg{{font-family:var(--mono);font-size:.9rem;font-weight:600}}
    .chart-ref{{font-family:var(--mono);font-size:.72rem;color:var(--text-muted)}}
    .chart-session{{font-size:.65rem;font-weight:700;padding:3px 9px;border:1px solid var(--border-hi);border-radius:3px;letter-spacing:.07em;color:var(--text-muted)}}
    .chart-toolbar{{display:flex;align-items:center;justify-content:space-between;padding:6px 14px;border-bottom:1px solid var(--border);flex-shrink:0;gap:10px}}
    .chart-tabs{{display:flex;gap:2px;overflow-x:auto;scrollbar-width:none}}
    .chart-tabs::-webkit-scrollbar{{display:none}}
    /* ticker tabs stay as buttons */
    .tab{{padding:4px 9px;border-radius:4px;border:1px solid transparent;background:transparent;color:var(--text-muted);font-family:var(--mono);font-size:.72rem;font-weight:600;cursor:pointer;white-space:nowrap;transition:all .15s}}
    .tab:hover{{color:var(--text);border-color:var(--border-hi)}}
    .tab.active{{color:var(--accent);border-color:var(--accent);background:var(--accent-glow)}}
    /* interval / type / compare dropdowns */
    .ctrl-select{{
      appearance:none;-webkit-appearance:none;
      background-color:var(--bg-card);
      background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='9' height='5'%3E%3Cpath d='M0 0l4.5 5L9 0' fill='none' stroke='%23506070' stroke-width='1.4'/%3E%3C/svg%3E");
      background-repeat:no-repeat;background-position:right 8px center;
      border:1px solid var(--border-hi);color:var(--text-muted);
      border-radius:4px;font-size:.7rem;font-family:var(--mono);
      padding:4px 26px 4px 9px;cursor:pointer;outline:none;flex-shrink:0;
      transition:border-color .15s,color .15s;
    }}
    .ctrl-select:hover,.ctrl-select:focus{{border-color:var(--accent);color:var(--text)}}
    .ctrl-select option{{background:var(--bg-panel);color:var(--text)}}
    .chart-controls{{display:flex;align-items:center;gap:6px;flex-shrink:0}}

    /* chart container must be a positioned block for LW Charts canvas */
    #chart-container{{flex:1;min-height:0;position:relative;overflow:hidden}}

    /* ── news panel ── */
    .news-panel{{display:flex;flex-direction:column;background:var(--bg-panel);min-height:0}}
    .news-header{{padding:10px 14px 6px;border-bottom:1px solid var(--border);flex-shrink:0}}
    .news-header-top{{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}}
    .news-header-label{{font-size:.68rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--text-muted)}}
    .news-count{{font-family:var(--mono);font-size:.66rem;color:var(--text-dim)}}
    /* ── view nav ── */
    .view-nav{{height:38px;flex-shrink:0;display:flex;align-items:center;gap:0;background:var(--bg-panel);border-bottom:1px solid var(--border);padding:0 16px}}
    .vnav-btn{{display:flex;align-items:center;gap:6px;padding:0 14px;height:38px;border:none;background:transparent;font-family:'Inter',sans-serif;font-size:.72rem;font-weight:600;color:var(--text-muted);cursor:pointer;letter-spacing:.04em;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;white-space:nowrap}}
    .vnav-btn.active{{color:var(--accent);border-bottom-color:var(--accent)}}
    .vnav-btn:hover:not(.active){{color:var(--text)}}
    .vnav-btn svg{{width:13px;height:13px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
    .vnav-sep{{width:1px;height:20px;background:var(--border);margin:0 4px;flex-shrink:0}}

    /* ── creator studio ── */
    .studio-view{{flex:1;min-height:0;overflow:hidden;display:none;flex-direction:column;background:var(--bg-base)}}
    .studio-header{{padding:16px 22px 12px;border-bottom:1px solid var(--border);background:var(--bg-panel);flex-shrink:0;display:flex;align-items:center;gap:14px}}
    .studio-badge{{font-size:.58rem;font-weight:800;letter-spacing:.1em;text-transform:uppercase;padding:3px 8px;border-radius:4px;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff}}
    .studio-title{{font-size:.88rem;font-weight:800;letter-spacing:.04em;color:var(--text)}}
    .studio-subtitle{{font-size:.66rem;color:var(--text-muted);margin-left:auto}}
    .studio-body{{display:grid;grid-template-columns:2fr 3fr;flex:1;min-height:0;overflow:hidden}}
    .studio-left{{border-right:1px solid var(--border);display:flex;flex-direction:column;overflow-y:auto;background:var(--bg-panel)}}
    .studio-right{{display:flex;flex-direction:column;overflow:hidden;background:var(--bg-base)}}
    .studio-section{{padding:14px 16px;border-bottom:1px solid var(--border)}}
    .studio-section.grow{{flex:1;display:flex;flex-direction:column;min-height:0;border-bottom:none}}
    .studio-section-title{{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--text-muted);margin-bottom:10px}}
    .gen-btn{{width:100%;padding:11px 16px;border-radius:8px;border:none;background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;font-family:'Inter',sans-serif;font-size:.78rem;font-weight:700;letter-spacing:.06em;cursor:pointer;display:flex;align-items:center;justify-content:center;gap:8px;transition:opacity .15s,transform .1s,box-shadow .15s;box-shadow:0 2px 14px rgba(99,102,241,.35)}}
    .gen-btn:hover:not(:disabled){{opacity:.9;transform:translateY(-1px);box-shadow:0 4px 20px rgba(99,102,241,.45)}}
    .gen-btn:active:not(:disabled){{transform:translateY(0)}}
    .gen-btn:disabled{{opacity:.55;cursor:not-allowed}}
    .gen-btn svg{{width:14px;height:14px;fill:none;stroke:#fff;stroke-width:2.5;stroke-linecap:round;stroke-linejoin:round}}
    .gen-status{{font-size:.62rem;color:var(--text-muted);text-align:center;margin-top:6px;min-height:1.2em;font-style:italic}}
    .gen-status.error{{color:var(--red)}}
    .cobra-card{{background:var(--bg-base);border:1px solid var(--border-hi);border-radius:8px;padding:12px 14px}}
    .cobra-label{{font-size:.58rem;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:var(--text-muted);margin-bottom:3px}}
    .cobra-name{{font-size:.76rem;font-weight:800;color:var(--text);line-height:1.2;margin-bottom:8px}}
    .cobra-metrics{{display:flex;gap:16px;flex-wrap:wrap}}
    .cobra-metric{{display:flex;flex-direction:column;gap:1px}}
    .cobra-metric-label{{font-size:.56rem;color:var(--text-dim);text-transform:uppercase;letter-spacing:.05em}}
    .cobra-metric-val{{font-family:var(--mono);font-size:.8rem;font-weight:700}}
    .cobra-metric-val.up{{color:var(--green)}}.cobra-metric-val.dn{{color:var(--red)}}
    .news-sel-list{{display:flex;flex-direction:column;gap:3px;overflow-y:auto;flex:1}}
    .news-sel-item{{display:flex;align-items:flex-start;gap:8px;padding:7px 8px;border-radius:6px;border:1px solid transparent;cursor:pointer;transition:background .12s,border-color .12s;user-select:none}}
    .news-sel-item:hover{{background:var(--bg-hover)}}
    .news-sel-item.selected{{border-color:var(--accent);background:rgba(99,102,241,.07)}}
    .news-sel-item input{{margin-top:3px;accent-color:var(--accent);flex-shrink:0;cursor:pointer;width:13px;height:13px}}
    .news-sel-title{{font-size:.66rem;color:var(--text);line-height:1.45}}
    .news-sel-meta{{font-size:.57rem;color:var(--text-muted);margin-top:2px}}
    .news-sel-hint{{font-size:.6rem;color:var(--text-dim);margin-bottom:8px;font-style:italic}}
    .studio-right-header{{padding:10px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px;flex-shrink:0;background:var(--bg-panel)}}
    .studio-right-label{{font-size:.65rem;font-weight:700;color:var(--text);flex:1;letter-spacing:.06em;text-transform:uppercase}}
    .saction-btn{{display:flex;align-items:center;gap:5px;padding:5px 12px;border-radius:7px;border:1px solid var(--border-hi);background:transparent;font-family:'Inter',sans-serif;font-size:.63rem;font-weight:600;color:var(--text-muted);cursor:pointer;transition:all .15s;letter-spacing:.03em;white-space:nowrap}}
    .saction-btn:hover{{border-color:var(--accent);color:var(--accent)}}
    .saction-btn.primary{{border-color:var(--accent);background:var(--accent);color:#fff}}
    .saction-btn.primary:hover{{opacity:.85}}
    .saction-btn svg{{width:11px;height:11px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
    #report-textarea{{flex:1;width:100%;border:none;outline:none;resize:none;background:var(--bg-base);color:var(--text);font-family:'Inter',sans-serif;font-size:.8rem;line-height:1.82;padding:18px 22px;box-sizing:border-box;overflow-y:auto}}
    #report-textarea::placeholder{{color:var(--text-dim);font-style:italic}}
    .char-count{{font-family:var(--mono);font-size:.58rem;color:var(--text-dim);padding:5px 16px;border-top:1px solid var(--border);text-align:right;flex-shrink:0;background:var(--bg-panel)}}

    /* copy summary button */
    .copy-btn{{display:flex;align-items:center;gap:5px;padding:4px 10px;border-radius:12px;border:1px solid var(--accent);background:transparent;font-size:.62rem;font-weight:700;letter-spacing:.05em;color:var(--accent);cursor:pointer;transition:all .15s;white-space:nowrap;font-family:'Inter',sans-serif}}
    .copy-btn:hover{{background:var(--accent);color:#fff}}
    .copy-btn svg{{width:11px;height:11px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}}
    .refresh-btn{{display:flex;align-items:center;justify-content:center;width:26px;height:26px;border-radius:50%;border:1px solid var(--border-hi);background:transparent;cursor:pointer;transition:border-color .15s,background .15s;flex-shrink:0}}
    .refresh-btn:hover{{border-color:var(--accent);background:rgba(99,102,241,.1)}}
    .refresh-btn:disabled{{opacity:.45;cursor:not-allowed}}
    .refresh-btn svg{{width:13px;height:13px;fill:none;stroke:var(--text-muted);stroke-width:2;stroke-linecap:round;stroke-linejoin:round;transition:stroke .15s}}
    .refresh-btn:hover svg{{stroke:var(--accent)}}
    @keyframes spin{{to{{transform:rotate(360deg)}}}}
    .refresh-btn.spinning svg{{animation:spin .8s linear infinite}}
    .copy-toast{{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(20px);background:var(--accent);color:#fff;padding:8px 18px;border-radius:20px;font-size:.72rem;font-weight:700;letter-spacing:.04em;pointer-events:none;opacity:0;transition:opacity .22s,transform .22s;z-index:9999;box-shadow:0 4px 16px rgba(0,0,0,.35)}}
    .copy-toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}
    /* ── live blog dual panels ── */
    #lb-yahoo-panel{{display:none;overflow-y:auto;flex:1;padding:10px 10px 20px}}
    #lb-yahoo-panel.visible{{display:block}}
    /* ticker badges in headlines */
    .ticker-badge{{display:inline-flex;align-items:center;gap:3px;font-family:var(--mono);font-size:.65rem;font-weight:700;padding:1px 6px;border-radius:3px;cursor:pointer;vertical-align:middle;letter-spacing:.02em;transition:opacity .12s}}
    .ticker-badge:hover{{opacity:.8}}
    .ticker-badge.up{{background:rgba(0,129,68,.18);color:#00b85a;border:1px solid rgba(0,129,68,.3)}}
    .ticker-badge.dn{{background:rgba(209,33,33,.18);color:#ff5252;border:1px solid rgba(209,33,33,.3)}}
    .ticker-badge.neutral{{background:rgba(100,116,139,.18);color:#94a3b8;border:1px solid rgba(100,116,139,.3)}}
    /* ticker toast popup */
    .ticker-toast{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(12px);background:var(--bg-card);border:1px solid var(--border-hi);border-radius:8px;padding:10px 18px;display:flex;gap:14px;align-items:baseline;opacity:0;pointer-events:none;transition:opacity .2s,transform .2s;z-index:999;box-shadow:0 8px 28px rgba(0,0,0,.5)}}
    .ticker-toast.visible{{opacity:1;transform:translateX(-50%) translateY(0)}}
    .tt-sym{{font-family:var(--mono);font-size:.85rem;font-weight:700;color:var(--text)}}
    .tt-name{{font-size:.7rem;color:var(--text-muted)}}
    .tt-price{{font-family:var(--mono);font-size:.85rem;color:var(--text)}}
    .tt-pct{{font-family:var(--mono);font-size:.78rem;font-weight:600}}
    .tt-pct.up{{color:var(--green)}}.tt-pct.dn{{color:var(--red)}}
    /* no results */
    .no-results{{padding:32px 20px;text-align:center;font-size:.8rem;color:var(--text-dim)}}
    :root.light .ticker-toast{{box-shadow:0 8px 28px rgba(0,0,0,.15)}}
    /* ── live blog view toggle ── */
    .lb-view-tabs{{display:flex;gap:4px;margin-left:auto}}
    .lb-tab-btn{{background:none;border:1px solid var(--border);border-radius:4px;color:var(--text-dim);font-size:.6rem;font-weight:700;letter-spacing:.07em;padding:3px 9px;cursor:pointer;text-transform:uppercase;transition:all .14s}}
    .lb-tab-btn.active,.lb-tab-btn:hover{{background:var(--accent);border-color:var(--accent);color:#fff}}
    /* ── live blog cards panel ── */
    #lb-cnbc-panel{{display:none;overflow-y:auto;flex:1;padding:10px 10px 20px}}
    #lb-cnbc-panel.visible{{display:block}}
    .lb-empty{{padding:40px 20px;text-align:center;font-size:.78rem;color:var(--text-dim)}}
    .lb-card{{background:var(--bg-card);border:1px solid var(--border);border-radius:8px;margin-bottom:12px;overflow:hidden;transition:border-color .15s}}
    .lb-card:hover{{border-color:var(--border-hi)}}
    .lb-card-head{{padding:12px 14px 10px;cursor:pointer;user-select:none;display:flex;flex-direction:column;gap:6px}}
    .lb-src-row{{display:flex;align-items:center;gap:6px}}
    .lb-src-badge{{font-size:.55rem;font-weight:800;letter-spacing:.09em;text-transform:uppercase;padding:2px 7px;border-radius:3px}}
    .lb-src-badge.cnbc{{background:rgba(30,144,255,.18);color:#60a5fa}}
    .lb-src-badge.yahoo{{background:rgba(123,97,255,.18);color:#a78bfa}}
    .lb-ts{{font-family:var(--mono);font-size:.58rem;color:var(--text-dim)}}
    .lb-headline{{font-size:.9rem;font-weight:700;color:var(--text);line-height:1.4;letter-spacing:-.01em}}
    .lb-chevron{{margin-left:auto;font-size:.65rem;color:var(--text-dim);transition:transform .2s;flex-shrink:0}}
    .lb-card.open .lb-chevron{{transform:rotate(180deg)}}
    .lb-body{{padding:0 14px;max-height:0;overflow:hidden;transition:max-height .4s ease,padding .3s ease}}
    .lb-card.open .lb-body{{max-height:4000px;padding-bottom:14px}}
    .lb-section{{font-size:.7rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--accent);margin:10px 0 4px}}
    .lb-para{{font-size:.78rem;color:var(--text-muted);line-height:1.7;margin-bottom:6px}}
    .lb-link{{display:inline-block;margin-top:8px;font-size:.65rem;color:var(--accent);text-decoration:none;letter-spacing:.02em}}
    .lb-link:hover{{text-decoration:underline}}
    .lb-loading{{padding:30px 20px;text-align:center;font-size:.75rem;color:var(--text-dim);font-family:var(--mono)}}

    /* ── sidebar ── */
    .sidebar{{display:flex;flex-direction:column;border-left:1px solid var(--border);background:var(--bg-panel);overflow-y:auto;min-height:0}}
    .sidebar-section{{padding:12px 10px;border-bottom:1px solid var(--border)}}
    .sidebar-title{{font-size:.6rem;font-weight:700;text-transform:uppercase;letter-spacing:.1em;color:var(--text-muted);margin-bottom:8px}}
    /* heatmap */
    .heatmap-grid{{display:grid;grid-template-columns:1fr 1fr;gap:3px}}
    .heatmap-cell{{padding:6px 6px 5px;border-radius:4px;min-height:46px;display:flex;flex-direction:column;justify-content:space-between;cursor:default}}
    .hm-label{{font-size:.56rem;font-weight:600;color:rgba(255,255,255,.88);line-height:1.2}}
    .hm-pct{{font-family:var(--mono);font-size:.68rem;font-weight:700;color:#fff}}
    :root.light .hm-label{{color:rgba(0,0,0,.72)}}
    :root.light .hm-pct{{color:rgba(0,0,0,.82)}}
    /* top movers */
    .movers-tabs{{display:flex;border-bottom:1px solid var(--border);margin-bottom:8px}}
    .mover-tab{{flex:1;padding:5px 4px;font-size:.63rem;font-weight:600;border:none;background:transparent;color:var(--text-muted);cursor:pointer;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;font-family:'Inter',sans-serif}}
    .mover-tab.active{{color:var(--accent);border-bottom-color:var(--accent)}}
    .mover-row{{display:flex;align-items:center;gap:6px;padding:6px 0;border-bottom:1px solid var(--border)}}
    .mover-row:last-child{{border-bottom:none}}
    .mover-left{{flex:1;min-width:0}}
    .mover-right{{text-align:right;flex-shrink:0}}
    .mover-ticker{{font-family:var(--mono);font-size:.7rem;font-weight:700;color:var(--text)}}
    .mover-name{{font-size:.57rem;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:110px}}
    .mover-price{{font-family:var(--mono);font-size:.63rem;color:var(--text-muted);display:block}}
    .mover-pct{{font-family:var(--mono);font-size:.7rem;font-weight:700;display:block}}
    .mover-pct.up{{color:var(--green)}}.mover-pct.dn{{color:var(--red)}}

    @keyframes pulse{{0%,100%{{opacity:1;box-shadow:0 0 6px var(--green)}}50%{{opacity:.35;box-shadow:none}}}}
    @keyframes flash-up{{0%{{background:rgba(0,230,118,.22)}}100%{{background:transparent}}}}
    @keyframes flash-dn{{0%{{background:rgba(244,67,54,.22)}}100%{{background:transparent}}}}
    .flash-up{{animation:flash-up .9s ease-out}} .flash-dn{{animation:flash-dn .9s ease-out}}
    ::-webkit-scrollbar{{width:4px;height:4px}}
    ::-webkit-scrollbar-track{{background:transparent}}
    ::-webkit-scrollbar-thumb{{background:var(--border-hi);border-radius:2px}}

    /* ── loading skeleton ── */
    #chart-skeleton{{
      position:absolute;inset:0;z-index:10;
      background:var(--bg-base);pointer-events:none;
      opacity:0;transition:opacity .1s;display:flex;flex-direction:column;
    }}
    #chart-skeleton.active{{opacity:1;pointer-events:all}}
    .skel-area{{
      flex:1;display:flex;align-items:flex-end;gap:3px;
      padding:24px 58px 12px 12px;overflow:hidden;position:relative;
    }}
    .skel-bar{{
      flex:1;background:var(--border);border-radius:2px 2px 0 0;
      animation:skelPulse 1.8s ease-in-out infinite;
    }}
    .skel-bar:nth-child(3n+1){{animation-delay:-.6s}}
    .skel-bar:nth-child(3n+2){{animation-delay:-1.2s}}
    .skel-xaxis{{
      height:26px;flex-shrink:0;margin-right:58px;
      border-top:1px solid var(--border);
      background:repeating-linear-gradient(90deg,var(--border) 0,var(--border) 1px,transparent 1px,transparent 80px);
    }}
    #chart-skeleton::after{{
      content:'';position:absolute;inset:0;z-index:1;pointer-events:none;
      background:linear-gradient(105deg,transparent 25%,rgba(255,255,255,0.04) 50%,transparent 75%);
      background-size:300% 100%;
      animation:skelShimmer 2s linear infinite;
    }}
    @keyframes skelPulse{{0%,100%{{opacity:.35}}50%{{opacity:.65}}}}
    @keyframes skelShimmer{{0%{{background-position:200% 0}}100%{{background-position:-200% 0}}}}

    /* ── OHLCV crosshair tooltip ── */
    #chart-tooltip{{
      position:absolute;z-index:5;pointer-events:none;
      display:none;opacity:0;
      background:rgba(7,11,18,0.92);
      border:1px solid rgba(255,255,255,0.09);
      border-radius:5px;padding:10px 14px;
      font-family:var(--mono);font-size:.7rem;
      color:#b8cce0;min-width:195px;
      backdrop-filter:blur(8px);
      box-shadow:0 8px 28px rgba(0,0,0,0.55);
      transition:opacity .08s ease;
    }}
    #chart-tooltip.visible{{display:block;opacity:1}}
    :root.light #chart-tooltip{{
      background:rgba(244,248,255,0.96);
      border-color:rgba(0,0,0,0.09);
      color:#1a2840;
      box-shadow:0 8px 28px rgba(0,0,0,0.13);
    }}
    .tt-header{{font-size:.65rem;color:var(--text-muted);padding-bottom:6px;border-bottom:1px solid var(--border);margin-bottom:5px;letter-spacing:.03em}}
    .tt-row{{display:flex;justify-content:space-between;align-items:center;gap:18px;padding:1.5px 0}}
    .tt-lbl{{color:var(--text-muted);font-weight:500}}
    .tt-num{{text-align:right;font-weight:600;color:var(--text)}}
  </style>
</head>
<body>

<!-- top bar -->
<div class="topbar">
  <div class="logo">
    <div class="live-dot"></div>
    <span class="logo-text">Market Pulse</span>
  </div>
  <div class="tickers-bar" id="tickers-bar"></div>
  <div class="topbar-right">
    <span id="upd-time">Snapshot: {generated_at}</span>
    <span id="clock">--:--:--</span>
    <button class="theme-btn" onclick="toggleTheme()">
      <span id="theme-icon">☀</span><span id="theme-label">Light</span>
    </button>
  </div>
</div>

<!-- view nav -->
<div class="view-nav">
  <button class="vnav-btn active" id="nav-dashboard" onclick="switchView('dashboard')">
    <svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
    Dashboard
  </button>
  <div class="vnav-sep"></div>
  <button class="vnav-btn" id="nav-news" onclick="switchView('news')">
    <svg viewBox="0 0 24 24"><path d="M4 22h16a2 2 0 0 0 2-2V4a2 2 0 0 0-2-2H8a2 2 0 0 0-2 2v16a2 2 0 0 1-2 2Zm0 0a2 2 0 0 1-2-2v-9c0-1.1.9-2 2-2h2"/><path d="M18 14h-8"/><path d="M15 18h-5"/><path d="M10 6h8v4h-8V6Z"/></svg>
    Noticias
  </button>
  <div class="vnav-sep"></div>
  <button class="vnav-btn" id="nav-studio" onclick="switchView('studio')">
    <svg viewBox="0 0 24 24"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>
    Flip Creator Studio
  </button>
</div>

<!-- main -->
<div class="main" id="view-dashboard">

  <!-- chart panel -->
  <div class="chart-panel">
    <div class="chart-header">
      <div class="chart-header-top">
        <div>
          <div class="chart-name" id="chart-name">S&amp;P 500</div>
          <div class="chart-price" id="chart-price">—</div>
          <div class="chart-meta">
            <span class="chart-chg" id="chart-chg"></span>
            <span class="chart-ref" id="chart-ref"></span>
          </div>
        </div>
        <span class="chart-session" id="chart-session">TODAY</span>
      </div>
    </div>
    <div class="chart-toolbar">
      <div class="chart-tabs" id="chart-tabs"></div>
      <div class="chart-controls">
        <select class="ctrl-select" id="period-select" onchange="switchPeriod(this.value)"></select>
        <select class="ctrl-select" id="type-select" onchange="switchType(this.value)"></select>
        <select class="ctrl-select" id="compare-select" onchange="switchCompare(this.value)">
          <option value="">Compare +</option>
        </select>
      </div>
    </div>
    <div id="chart-container">
      <div id="chart-skeleton">
        <div class="skel-area">
          <div class="skel-bar" style="height:28%"></div>
          <div class="skel-bar" style="height:33%"></div>
          <div class="skel-bar" style="height:36%"></div>
          <div class="skel-bar" style="height:40%"></div>
          <div class="skel-bar" style="height:38%"></div>
          <div class="skel-bar" style="height:44%"></div>
          <div class="skel-bar" style="height:48%"></div>
          <div class="skel-bar" style="height:45%"></div>
          <div class="skel-bar" style="height:52%"></div>
          <div class="skel-bar" style="height:55%"></div>
          <div class="skel-bar" style="height:58%"></div>
          <div class="skel-bar" style="height:54%"></div>
          <div class="skel-bar" style="height:60%"></div>
          <div class="skel-bar" style="height:64%"></div>
          <div class="skel-bar" style="height:62%"></div>
          <div class="skel-bar" style="height:68%"></div>
          <div class="skel-bar" style="height:72%"></div>
          <div class="skel-bar" style="height:70%"></div>
          <div class="skel-bar" style="height:66%"></div>
          <div class="skel-bar" style="height:63%"></div>
          <div class="skel-bar" style="height:68%"></div>
          <div class="skel-bar" style="height:65%"></div>
          <div class="skel-bar" style="height:60%"></div>
          <div class="skel-bar" style="height:56%"></div>
          <div class="skel-bar" style="height:52%"></div>
          <div class="skel-bar" style="height:58%"></div>
          <div class="skel-bar" style="height:55%"></div>
          <div class="skel-bar" style="height:50%"></div>
          <div class="skel-bar" style="height:47%"></div>
          <div class="skel-bar" style="height:44%"></div>
        </div>
        <div class="skel-xaxis"></div>
      </div>
      <!-- OHLCV tooltip (positioned absolutely, shown on crosshair hover) -->
      <div id="chart-tooltip">
        <div class="tt-header" id="tt-date"></div>
        <div class="tt-row"><span class="tt-lbl">Close</span><span class="tt-num" id="tt-close"></span></div>
        <div class="tt-row" id="tt-orow"><span class="tt-lbl">Open</span><span class="tt-num" id="tt-open"></span></div>
        <div class="tt-row" id="tt-hrow"><span class="tt-lbl">High</span><span class="tt-num" id="tt-high"></span></div>
        <div class="tt-row" id="tt-lrow"><span class="tt-lbl">Low</span><span class="tt-num" id="tt-low"></span></div>
        <div class="tt-row" id="tt-vrow"><span class="tt-lbl">Volume</span><span class="tt-num" id="tt-vol"></span></div>
      </div>
    </div>
  </div>

  <!-- sidebar -->
  <div class="sidebar">
    <div class="sidebar-section">
      <div class="sidebar-title">Sector Heatmap</div>
      <div class="heatmap-grid" id="heatmap-grid"></div>
    </div>
    <div class="sidebar-section">
      <div class="sidebar-title">Top Movers</div>
      <div class="movers-tabs">
        <button class="mover-tab active" id="tab-gainers" onclick="switchMoversTab('gainers')">&#9650; Gainers</button>
        <button class="mover-tab" id="tab-losers" onclick="switchMoversTab('losers')">&#9660; Losers</button>
      </div>
      <div id="movers-list"></div>
    </div>
  </div>

</div>

<!-- noticias view -->
<div id="view-news" class="news-view">
  <div class="events-panel">
    <div class="events-header">
      <div>
        <div class="events-title">Mercado Hoy</div>
        <div class="events-meta">
          <span class="events-date" id="events-date"></span>
          <span class="events-spx-badge" id="events-spx-badge" style="display:none"></span>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <span class="events-updated" id="events-updated"></span>
        <button class="refresh-btn" onclick="fetchMarketEvents()" title="Actualizar análisis">
          <svg viewBox="0 0 24 24"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>
        </button>
      </div>
    </div>
    <div id="events-list"><div class="events-loading">Cargando análisis de mercado…</div></div>
  </div>
</div>

<!-- liveblog panels (hidden, used only by Creator Studio) -->
<div style="display:none">
  <div id="lb-yahoo-panel" class="visible"></div>
  <div id="lb-cnbc-panel"></div>
</div>

<!-- creator studio -->
<div id="view-studio" class="studio-view">
  <div class="studio-header">
    <span class="studio-badge">BETA</span>
    <span class="studio-title">Flip Creator Studio</span>
    <span class="studio-subtitle">Generador automatizado del reporte editorial diario</span>
  </div>
  <div class="studio-body">

    <!-- LEFT: data panel (40%) -->
    <div class="studio-left">
      <div class="studio-section">
        <button class="gen-btn" id="gen-btn" onclick="generateFlipReport()">
          <svg viewBox="0 0 24 24"><path d="M5 3l14 9-14 9V3z"/></svg>
          Generar Reporte con IA
        </button>
        <div class="gen-status" id="gen-status"></div>
      </div>

      <div class="studio-section">
        <div class="studio-section-title">La Cobra Achorada &#183; S&amp;P 500</div>
        <div class="cobra-card" id="studio-cobra-card">
          <div class="cobra-label">Cargando datos...</div>
        </div>
      </div>

      <div class="studio-section grow">
        <div class="studio-section-title">Eventos del D&#237;a &#183; Identificados por IA</div>
        <div class="news-sel-hint">Seleccion&#225; los eventos a incluir. Sin selecci&#243;n, se usan todos.</div>
        <div class="news-sel-list" id="studio-news-list"></div>
      </div>
    </div>

    <!-- RIGHT: draft area (60%) -->
    <div class="studio-right">
      <div class="studio-right-header">
        <span class="studio-right-label">Borrador del Reporte</span>
        <button class="saction-btn primary" onclick="copyReport()">
          <svg viewBox="0 0 24 24"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
          Copiar
        </button>
        <button class="saction-btn" onclick="subirReporte()">
          <svg viewBox="0 0 24 24"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
          Subir a la Plataforma
        </button>
      </div>
      <textarea id="report-textarea" placeholder="Presion&#225; &#8216;Generar Reporte Diario&#8217; para crear el borrador autom&#225;ticamente con datos reales y tono Flip..." onkeyup="updateCharCount()"></textarea>
      <div class="char-count" id="char-count">0 caracteres</div>
    </div>

  </div>
</div>

<div class="copy-toast" id="copy-toast">&#10003; &#161;Copiado al portapapeles!</div>

<script>
'use strict';

// ── API base: relative when served by proxy, localhost when opened as local file
const API_BASE = window.location.protocol === 'file:' ? 'http://localhost:5678' : '';

// ── Embedded data (precios iniciales para primer paint; charts dinámicos via API) ──
const ALL_PRICES   = {prices_json};
let   ALL_CHARTS   = {{}};          // cargado on-demand desde /api/charts/SYM/PERIOD
const INIT_NEWS    = {news_json};
const INIT_EVENTS  = {events_json};
let   STOCK_PRICES  = {stock_prices_json};
const KNOWN_TICKERS = {{
  'AAPL':'Apple','MSFT':'Microsoft','NVDA':'NVIDIA','TSLA':'Tesla',
  'GOOGL':'Alphabet','META':'Meta','AMZN':'Amazon','JPM':'JPMorgan',
  'BAC':'Bank of America','GS':'Goldman Sachs','V':'Visa','MA':'Mastercard',
  'NFLX':'Netflix','AMD':'AMD','INTC':'Intel','QCOM':'Qualcomm',
  'XOM':'ExxonMobil','LLY':'Eli Lilly','WMT':'Walmart','COIN':'Coinbase',
  'DIS':'Disney','F':'Ford','GM':'General Motors',
}};

const SECTOR_ETFS = [
  {{sym:'XLK', label:'Technology'}},
  {{sym:'XLE', label:'Energy'}},
  {{sym:'XLF', label:'Financials'}},
  {{sym:'XLV', label:'Health Care'}},
  {{sym:'XLI', label:'Industrials'}},
  {{sym:'XLB', label:'Materials'}},
  {{sym:'XLU', label:'Utilities'}},
  {{sym:'XLP', label:'Staples'}},
  {{sym:'XLY', label:'Discret.'}},
  {{sym:'XLRE', label:'Real Estate'}},
  {{sym:'XLC', label:'Comm. Svcs'}},
];
const TABS         = {tabs_json};
const TICKER_ORDER = {ticker_order};
const PERIODS      = ['1D','5D','1M','6M','YTD','1Y','5Y','MAX'];
const CHART_TYPES  = [
  {{id:'mountain', label:'Mountain'}},
  {{id:'line',     label:'Line'}},
  {{id:'baseline', label:'Baseline'}},
  {{id:'bar',      label:'Bar'}},
  {{id:'candle',   label:'Candle'}},
];

// ── Topic definitions ─────────────────────────────────────────────────────────
const TOPICS = [
  {{ id:'all',         label:'All',          color:'#64748b', keywords:[] }},
  {{ id:'fed',         label:'Fed & Rates',  color:'#d97706',
     keywords:['fed','federal reserve','fomc','rate cut','rate hike','interest rate',
               'powell','basis point','bps','treasury yield','10-year','2-year',
               'monetary policy','quantitative','balance sheet','dot plot'] }},
  {{ id:'macro',       label:'Macro',        color:'#10b981',
     keywords:['gdp','pmi','ism','recession','economic','housing','deficit','debt',
               'spending','fiscal','budget','manufacturing','retail sales','consumer'] }},
  {{ id:'inflation',   label:'Inflación/CPI',color:'#f97316',
     keywords:['inflation','cpi','ppi','consumer price','deflation','pce','core inflation',
               'price index','core pce','price pressures','price increase','disinflation'] }},
  {{ id:'employment',  label:'Empleo',       color:'#22c55e',
     keywords:['jobs','unemployment','nonfarm','payroll','jobless','labor market','adp',
               'jolts','initial claims','employment','job gains','layoffs','hiring',
               'job openings','weekly claims','workforce'] }},
  {{ id:'earnings',    label:'Earnings',     color:'#3b82f6',
     keywords:['earnings','eps','revenue','quarterly','guidance','profit','loss',
               'beat','miss','results','q1','q2','q3','q4','forecast',
               'outlook','margin','ebitda','net income','reported'] }},
  {{ id:'tech',        label:'Tech & AI',    color:'#8b5cf6',
     keywords:['tech','technology','ai','artificial intelligence','semiconductor','chip',
               'nvidia','apple','microsoft','google','alphabet','meta','amazon','tesla',
               'software','cloud','data center','openai','gpu','arm','tsmc','broadcom'] }},
  {{ id:'energy',      label:'Energy',       color:'#f59e0b',
     keywords:['oil','energy','gas','opec','crude','petroleum','wti','brent',
               'natural gas','lng','pipeline','barrel','shale','exxon','chevron',
               'bp','shell','renewable','solar','wind','nuclear'] }},
  {{ id:'geopolitics', label:'Geopolitics',  color:'#ef4444',
     keywords:['tariff','trade war','china','russia','ukraine','iran','israel',
               'sanction','geopolit','war','conflict','nato','europe',
               'export control','supply chain','taiwan','middle east','import','wto'] }},
  {{ id:'crypto',      label:'Crypto',       color:'#06b6d4',
     keywords:['bitcoin','crypto','cryptocurrency','ethereum','eth','btc','blockchain',
               'digital asset','stablecoin','defi','coinbase','binance','token'] }},
  {{ id:'deals',       label:'M&A / IPO',    color:'#0891b2',
     keywords:['merger','acquisition','deal','ipo','spac','private equity','buyout',
               'takeover','bid','spinoff','divest','stake','listing','goes public'] }},
];

// ── State ─────────────────────────────────────────────────────────────────────
let prices         = {{...ALL_PRICES}};
let currentSym     = '^GSPC';
var _liveQuotes    = null;
let currentName    = 'S&P 500';
let currentPer     = '1D';
let activeTopic    = 'all';

let sources        = {{yahoo: true, cnbc: true}};
let classifiedNews = [];

// LightweightCharts instances
let lwChart         = null;
let lwPriceSeries   = null;
let lwVolSeries     = null;
let lwCompareSeries = null;

// Chart controls state
let chartType  = 'mountain';
let compareSym = '';

// Series-level tracking (enables partial updates without full teardown)
let lwSeriesType = null;  // 'area'|'line'|'baseline'|'bar'|'candle'
let lwPriceLine  = null;  // IPriceLine reference for prev-close dashed line
let lwHasCompare = false;
let lwCompareSym = '';

// Loading state
const CS = {{ IDLE:0, LOADING:1, READY:2 }};
let _chartState = CS.IDLE;

// Crosshair tooltip lookup map (ts → index into data arrays), rebuilt on each render
let _chartTsMap = null;

// ── Helpers ───────────────────────────────────────────────────────────────────
const fmtPrice = (sym, v) => {{
  if (!v && v !== 0) return '—';
  if (sym === 'BTC-USD') return '$' + v.toLocaleString('en-US', {{maximumFractionDigits:0}});
  if (sym === 'GC=F' || sym === 'CL=F') return '$' + v.toFixed(2);
  if (sym === '^VIX') return v.toFixed(2);
  return v.toLocaleString('en-US', {{minimumFractionDigits:2, maximumFractionDigits:2}});
}};
const signStr = v => v >= 0 ? '+' : '';
const arrow   = v => v >= 0 ? '▲' : '▼';
const cls     = v => v >= 0 ? 'up' : 'dn';
const reflow  = el => void el.offsetWidth;

// Convert ISO timestamp to LW unix-seconds, stripping tz offset so labels show
// the exchange's local time (ET) instead of UTC.
function tsToLW(iso) {{
  const stripped = iso.replace(/([+-]\d{{2}}:\d{{2}}|Z)$/, '');
  return Math.floor(new Date(stripped + 'Z').getTime() / 1000);
}}

// ── Clock ─────────────────────────────────────────────────────────────────────
function tickClock() {{
  const n = new Date();
  document.getElementById('clock').textContent =
    [n.getHours(), n.getMinutes(), n.getSeconds()]
      .map(x => String(x).padStart(2,'0')).join(':') + '  ET';
  const h = n.getHours() + n.getMinutes()/60;
  const open = n.getDay() >= 1 && n.getDay() <= 5 && h >= 9.5 && h < 16;
  const el = document.getElementById('chart-session');
  el.textContent = open ? '● OPEN' : '○ CLOSED';
  el.style.color  = open ? 'var(--green)' : 'var(--text-muted)';
}}
setInterval(tickClock, 1000); tickClock();

// ── LightweightCharts theme options ───────────────────────────────────────────
function lwTheme(light) {{
  // Faint dashed grid — CNBC/Yahoo style
  const gridColor = light ? 'rgba(0,0,0,0.05)' : 'rgba(255,255,255,0.05)';
  // Crosshair line color — sharp but not distracting
  const crossColor = light ? 'rgba(30,41,59,0.55)' : 'rgba(190,210,230,0.65)';
  // Axis badge background
  const labelBg = light ? '#1e293b' : '#0d1a2a';

  return {{
    layout: {{
      background: {{ type: 'solid', color: light ? '#f8fafc' : '#070b12' }},
      textColor:  light ? '#64748b' : '#4a6080',
      fontFamily: "'JetBrains Mono', monospace",
      fontSize:   11,
    }},
    grid: {{
      vertLines: {{ color: gridColor, style: LightweightCharts.LineStyle.Dashed }},
      horzLines: {{ color: gridColor, style: LightweightCharts.LineStyle.Dashed }},
    }},
    crosshair: {{
      // Magnet mode snaps crosshair to the nearest data point (bar/candle)
      mode: LightweightCharts.CrosshairMode.Magnet,
      vertLine: {{
        color:                crossColor,
        width:                1,
        style:                LightweightCharts.LineStyle.Dashed,
        labelBackgroundColor: labelBg,
        labelVisible:         true,
      }},
      horzLine: {{
        color:                crossColor,
        width:                1,
        style:                LightweightCharts.LineStyle.Dashed,
        labelBackgroundColor: labelBg,
        labelVisible:         true,
      }},
    }},
    rightPriceScale: {{
      borderColor: light ? '#d8e2f0' : '#1a2535',
      scaleMargins: {{ top: 0.08, bottom: 0.22 }},
    }},
    timeScale: {{
      borderColor:    light ? '#d8e2f0' : '#1a2535',
      timeVisible:    true,
      secondsVisible: false,
      fixLeftEdge:    true,
      fixRightEdge:   true,
    }},
  }};
}}

// ── Init chart (once) ─────────────────────────────────────────────────────────
function ensureChart() {{
  if (lwChart) return;
  const container = document.getElementById('chart-container');
  const light = document.documentElement.classList.contains('light');
  lwChart = LightweightCharts.createChart(container, {{
    autoSize: true,
    ...lwTheme(light),
    handleScroll:  {{ mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true }},
    handleScale:   {{ mouseWheel: true, pinch: true, axisPressedMouseMove: true }},
    kineticScroll: {{ mouse: true, touch: true }},
  }});
  lwChart.subscribeCrosshairMove(handleCrosshairMove);
}}

// ── Skeleton / state machine ──────────────────────────────────────────────────
function setChartState(s) {{
  _chartState = s;
  const sk = document.getElementById('chart-skeleton');
  if (sk) sk.classList.toggle('active', s === CS.LOADING);
}}

// ── OHLCV crosshair tooltip ───────────────────────────────────────────────────
function handleCrosshairMove(param) {{
  const tt = document.getElementById('chart-tooltip');
  if (!tt) return;

  // Hide when cursor leaves chart area or no time is pinned
  if (!param.point || param.point.x < 0 || param.point.y < 0 || !param.time) {{
    tt.classList.remove('visible');
    return;
  }}

  // Retrieve data for the hovered timestamp
  if (!_chartTsMap) {{ tt.classList.remove('visible'); return; }}
  const idx = _chartTsMap.get(param.time);
  if (idx === undefined) {{ tt.classList.remove('visible'); return; }}

  const data = (ALL_CHARTS[currentSym] || {{}})[currentPer];
  if (!data) {{ tt.classList.remove('visible'); return; }}

  // ── Date label ──────────────────────────────────────────────────────────────
  const d = new Date(param.time * 1000);
  const isIntraday = currentPer === '1D' || currentPer === '5D' || currentPer === '1M';
  let dateStr;
  if (isIntraday) {{
    const h   = d.getUTCHours(), mi = String(d.getUTCMinutes()).padStart(2,'0');
    const ap  = h >= 12 ? 'PM' : 'AM';
    const h12 = (h % 12) || 12;
    dateStr   = `${{d.getUTCMonth()+1}}/${{d.getUTCDate()}}  ${{h12}}:${{mi}} ${{ap}}`;
  }} else {{
    const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
    dateStr = `${{months[d.getUTCMonth()]}} ${{d.getUTCDate()}}, ${{d.getUTCFullYear()}}`;
  }}
  document.getElementById('tt-date').textContent = dateStr;

  // ── Price values ─────────────────────────────────────────────────────────────
  const close  = data.closes[idx];
  const open   = data.opens   ? data.opens[idx]   : null;
  const high   = data.highs   ? data.highs[idx]   : null;
  const low    = data.lows    ? data.lows[idx]    : null;
  const vol    = data.volumes ? data.volumes[idx] : null;

  document.getElementById('tt-close').textContent = fmtPrice(currentSym, close);

  const orow = document.getElementById('tt-orow');
  const hrow = document.getElementById('tt-hrow');
  const lrow = document.getElementById('tt-lrow');
  const vrow = document.getElementById('tt-vrow');

  if (open != null) {{ document.getElementById('tt-open').textContent = fmtPrice(currentSym, open); orow.style.display=''; }}
  else orow.style.display='none';

  if (high != null) {{ document.getElementById('tt-high').textContent = fmtPrice(currentSym, high); hrow.style.display=''; }}
  else hrow.style.display='none';

  if (low != null) {{ document.getElementById('tt-low').textContent  = fmtPrice(currentSym, low);  lrow.style.display=''; }}
  else lrow.style.display='none';

  if (vol && vol > 0) {{
    document.getElementById('tt-vol').textContent = vol.toLocaleString('en-US');
    vrow.style.display = '';
  }} else vrow.style.display = 'none';

  // ── Positioning ──────────────────────────────────────────────────────────────
  // tooltip is positioned inside #chart-container; param.point is in canvas-pixel coords
  tt.classList.add('visible');
  const cw  = tt.parentElement.clientWidth;
  const ch  = tt.parentElement.clientHeight;
  const tw  = tt.offsetWidth  || 200;
  const th  = tt.offsetHeight || 170;
  const pad = 14;
  const cx  = param.point.x;
  const cy  = param.point.y;

  // Flip tooltip to the left when near the right edge
  let left = cx + pad;
  if (left + tw > cw - pad) left = cx - tw - pad;
  left = Math.max(pad, left);

  // Center vertically around cursor, clamped within container
  let top = cy - th / 2;
  top = Math.max(pad, Math.min(ch - th - pad, top));

  tt.style.left = left + 'px';
  tt.style.top  = top  + 'px';
}}

// ── X-axis tick formatter (per period) ────────────────────────────────────────
function makeTickFormatter(pk) {{
  return (time, tickMarkType) => {{
    const d  = new Date(time * 1000);
    const TT = LightweightCharts.TickMarkType;
    const yr = d.getUTCFullYear();
    const mo = d.toLocaleString('en-US', {{ month: 'short', timeZone: 'UTC' }});
    const dd = d.getUTCDate();
    const hh = String(d.getUTCHours()).padStart(2, '0');
    const mm = String(d.getUTCMinutes()).padStart(2, '0');
    const md = (d.getUTCMonth() + 1) + '/' + dd;  // MM/DD

    if (tickMarkType === TT.Year) return String(yr);

    if (pk === '1D') {{
      if (tickMarkType === TT.Month)      return mo + ' ' + yr;
      if (tickMarkType === TT.DayOfMonth) return mo + ' ' + dd;
      return hh + ':' + mm;  // Time
    }}
    if (pk === '5D') {{
      if (tickMarkType === TT.Month)      return mo;
      if (tickMarkType === TT.DayOfMonth) return mo + ' ' + dd;
      return hh + ':' + mm;  // Time
    }}
    if (pk === '1M') {{
      if (tickMarkType === TT.Month)      return mo;
      if (tickMarkType === TT.DayOfMonth) return md;
      return hh + ':' + mm;
    }}
    if (pk === '6M' || pk === 'YTD') {{
      if (tickMarkType === TT.Month)      return mo + " '" + String(yr).slice(2);
      if (tickMarkType === TT.DayOfMonth) return md;
      return md;
    }}
    if (pk === '1Y') {{
      if (tickMarkType === TT.Month)      return mo + " '" + String(yr).slice(2);
      if (tickMarkType === TT.DayOfMonth) return md + '/' + String(yr).slice(2);
      return md;
    }}
    // 5Y / MAX
    if (tickMarkType === TT.Month)      return mo + " '" + String(yr).slice(2);
    if (tickMarkType === TT.DayOfMonth) return dd + ' ' + mo;
    return mo;
  }};
}}

// ── Draw chart (public entry point) ───────────────────────────────────────────
// Shows skeleton via setChartState, then renders on the next animation frame so
// the browser paints the skeleton before JS blocks the thread.
async function drawChart(sym, periodKey) {{
  let data = (ALL_CHARTS[sym] || {{}})[periodKey];
  if (!data) {{
    setChartState(CS.LOADING);
    try {{
      var enc = encodeURIComponent(sym);
      var r   = await fetch(API_BASE + '/api/charts/' + enc + '/' + periodKey, {{cache:'no-store'}});
      if (r.ok) {{
        data = await r.json();
        if (!ALL_CHARTS[sym]) ALL_CHARTS[sym] = {{}};
        ALL_CHARTS[sym][periodKey] = data;
      }}
    }} catch(e) {{ console.warn('[chart] fetch:', e); }}
  }}
  if (!data || !data.closes || data.closes.length === 0) {{
    document.getElementById('chart-price').textContent = 'Sin datos';
    document.getElementById('chart-chg').textContent   = '';
    document.getElementById('chart-ref').textContent   = '';
    setChartState(CS.READY);
    return;
  }}
  setChartState(CS.LOADING);
  requestAnimationFrame(() => {{
    _applyChartData(sym, periodKey, data);
    requestAnimationFrame(() => setChartState(CS.READY));
  }});
}}

// ── Internal render (runs after skeleton paint) ────────────────────────────────
function _applyChartData(sym, periodKey, data) {{
  ensureChart();

  const {{ timestamps, closes, opens, highs, lows, volumes, prev_close }} = data;
  const last    = closes[closes.length - 1];
  const chgAbs  = last - prev_close;
  const chgPct  = chgAbs / prev_close * 100;
  const up      = chgAbs >= 0;
  const light   = document.documentElement.classList.contains('light');

  // CNBC / Yahoo Finance brand colors
  const GRN = up ? '#008144' : '#d12121';  // solid stroke
  const lineClr  = GRN;
  // Ultra-subtle fading gradient: barely visible at the line, fully transparent at baseline
  const topClr   = up ? 'rgba(0,129,68,0.10)'   : 'rgba(209,33,33,0.10)';
  // Volume bar colors aligned to the brand palette
  const gVolClr  = 'rgba(0,129,68,0.38)';
  const rVolClr  = 'rgba(209,33,33,0.38)';
  const nVolClr  = light ? 'rgba(100,116,139,0.30)' : 'rgba(100,116,139,0.25)';
  const refLineC = light ? 'rgba(80,96,112,0.55)'  : 'rgba(140,160,180,0.45)';

  // ── Header ─────────────────────────────────────────────────────────────────
  document.getElementById('chart-name').textContent = currentName;
  const pe = document.getElementById('chart-price');
  pe.textContent = fmtPrice(sym, last);
  pe.style.color = lineClr;
  const ce = document.getElementById('chart-chg');
  ce.textContent = arrow(chgPct) + ' ' + signStr(chgPct)
    + Math.abs(chgAbs).toFixed(2) + '  (' + signStr(chgPct) + chgPct.toFixed(2) + '%)';
  ce.className = 'chart-chg ' + cls(chgPct);
  const refLabels = {{'1D':'Prev Close','5D':'5D ago','1M':'1M ago','6M':'6M ago','YTD':'Jan 1','1Y':'1Y ago','5Y':'5Y ago','MAX':'Inception'}};
  document.getElementById('chart-ref').textContent =
    (refLabels[periodKey] || 'Ref') + '  ' + fmtPrice(sym, prev_close);

  // ── Theme + dynamic X-axis formatter ──────────────────────────────────────
  lwChart.applyOptions(lwTheme(light));
  lwChart.timeScale().applyOptions({{
    timeVisible:       periodKey === '1D' || periodKey === '5D' || periodKey === '1M',
    secondsVisible:    false,
    tickMarkFormatter: makeTickFormatter(periodKey),
  }});

  // ── Build OHLCV point array ────────────────────────────────────────────────
  const pts = timestamps.map((ts, i) => ({{
    t: tsToLW(ts),
    o: opens   ? (opens[i]   ?? closes[i]) : closes[i],
    h: highs   ? (highs[i]   ?? closes[i]) : closes[i],
    l: lows    ? (lows[i]    ?? closes[i]) : closes[i],
    c: closes[i],
    v: volumes ? (volumes[i] || 0) : 0,
  }})).filter(p => p.c != null && !isNaN(p.c));

  // Build timestamp→index map for O(1) tooltip lookup on crosshair events
  _chartTsMap = new Map(pts.map((p, i) => [p.t, i]));

  const hasVolume  = volumes && volumes.some(v => v > 0);
  const hasCompare = !!compareSym && compareSym !== sym;
  const isOHLC     = chartType === 'bar' || chartType === 'candle';
  const baseClose  = closes[0];

  // Determine target series type.
  // Compare mode forces 'line' so both overlays share the same % scale.
  const desiredType = hasCompare ? 'line' : (chartType === 'mountain' ? 'area' : chartType);

  // Only rebuild the price series when the TYPE changes or on first render.
  // Period / ticker switches of the same type reuse the existing series object
  // (canvas is never recreated) and call applyOptions() + setData() in-place.
  const hadVolume   = lwVolSeries !== null;
  const needRebuild = !lwPriceSeries
                   || desiredType !== lwSeriesType
                   || hasCompare  !== lwHasCompare
                   || hasVolume   !== hadVolume;

  if (needRebuild) {{
    // Full teardown — reverse Z order (compare, price, vol)
    lwPriceLine = null;
    if (lwCompareSeries) {{ try {{ lwChart.removeSeries(lwCompareSeries); }} catch(e){{}} lwCompareSeries = null; lwCompareSym = ''; }}
    if (lwPriceSeries)   {{ try {{ lwChart.removeSeries(lwPriceSeries);   }} catch(e){{}} lwPriceSeries   = null; }}
    if (lwVolSeries)     {{ try {{ lwChart.removeSeries(lwVolSeries);     }} catch(e){{}} lwVolSeries     = null; }}

    // Volume added first (renders behind price in Z order)
    if (hasVolume) {{
      lwVolSeries = lwChart.addHistogramSeries({{
        priceFormat: {{ type: 'volume' }}, priceScaleId: 'vol',
        color: nVolClr, lastValueVisible: false, priceLineVisible: false,
      }});
      lwChart.priceScale('vol').applyOptions({{ scaleMargins: {{ top: 0.82, bottom: 0 }}, borderVisible: false }});
    }}

    if (desiredType === 'area') {{
      lwPriceSeries = lwChart.addAreaSeries({{
        lineColor: lineClr, topColor: topClr, bottomColor: 'rgba(0,0,0,0)',
        lineWidth: 2, crosshairMarkerVisible: false,
        lastValueVisible: true, priceLineVisible: false,
        priceFormat: {{ type: 'price', precision: 2, minMove: 0.01 }},
      }});
    }} else if (desiredType === 'line') {{
      lwPriceSeries = lwChart.addLineSeries({{
        color: lineClr, lineWidth: 2, crosshairMarkerVisible: false,
        lastValueVisible: true, priceLineVisible: false,
        priceFormat: {{ type: 'price', precision: 2, minMove: 0.01 }},
      }});
    }} else if (desiredType === 'baseline') {{
      lwPriceSeries = lwChart.addBaselineSeries({{
        baseValue: {{ type: 'price', price: prev_close }},
        topLineColor: lineClr, topFillColor1: topClr, topFillColor2: 'rgba(0,0,0,0)',
        bottomLineColor: '#d12121',
        bottomFillColor1: 'rgba(0,0,0,0)', bottomFillColor2: 'rgba(209,33,33,0.10)',
        lineWidth: 2, lastValueVisible: true, priceLineVisible: false,
        priceFormat: {{ type: 'price', precision: 2, minMove: 0.01 }},
      }});
    }} else if (desiredType === 'bar') {{
      lwPriceSeries = lwChart.addBarSeries({{
        thinBars: false,
        upColor: '#008144', downColor: '#d12121',
        lastValueVisible: true, priceLineVisible: false,
      }});
    }} else {{  // candle
      lwPriceSeries = lwChart.addCandlestickSeries({{
        upColor: '#008144',  downColor: '#d12121',
        borderUpColor: '#008144', borderDownColor: '#d12121',
        wickUpColor:   '#008144', wickDownColor:   '#d12121',
        lastValueVisible: true, priceLineVisible: false,
      }});
    }}
    lwSeriesType = desiredType;

  }} else {{
    // Partial update — reuse the existing series object, just refresh colors
    if (lwPriceLine) {{
      try {{ lwPriceSeries.removePriceLine(lwPriceLine); }} catch(e) {{}}
      lwPriceLine = null;
    }}
    if (desiredType === 'area')
      lwPriceSeries.applyOptions({{ lineColor: lineClr, topColor: topClr }});
    else if (desiredType === 'line')
      lwPriceSeries.applyOptions({{ color: lineClr }});
  }}

  // ── Set price data (both paths) ────────────────────────────────────────────
  if (isOHLC) {{
    lwPriceSeries.setData(pts.map(p => ({{ time: p.t, open: p.o, high: p.h, low: p.l, close: p.c }})));
  }} else if (hasCompare) {{
    lwPriceSeries.setData(pts.map(p => ({{ time: p.t, value: (p.c - baseClose) / baseClose * 100 }})));
  }} else {{
    lwPriceSeries.setData(pts.map(p => ({{ time: p.t, value: p.c }})));
  }}

  // ── Set volume data (both paths) ───────────────────────────────────────────
  if (lwVolSeries && hasVolume) {{
    lwVolSeries.setData(pts.map((p, i) => ({{
      time: p.t, value: p.v,
      color: i === 0 ? nVolClr : (pts[i].c >= pts[i-1].c ? gVolClr : rVolClr),
    }})));
  }}

  // ── Dashed prev-close line ─────────────────────────────────────────────────
  if (!hasCompare && !isOHLC) {{
    lwPriceLine = lwPriceSeries.createPriceLine({{
      price: prev_close, color: refLineC, lineWidth: 1,
      lineStyle: LightweightCharts.LineStyle.Dashed,
      axisLabelVisible: true, title: '',
    }});
  }}

  // ── Comparison overlay ─────────────────────────────────────────────────────
  if (hasCompare) {{
    const cmpData = (ALL_CHARTS[compareSym] || {{}})[periodKey];
    if (cmpData && cmpData.closes && cmpData.closes.length > 0) {{
      const cmpBase = cmpData.closes[0];
      const cmpPts  = cmpData.timestamps
        .map((ts, i) => ({{ time: tsToLW(ts), value: (cmpData.closes[i] - cmpBase) / cmpBase * 100 }}))
        .filter(p => !isNaN(p.value));

      if (lwCompareSeries && compareSym === lwCompareSym) {{
        // Same compare ticker, different period — just swap data in-place
        lwCompareSeries.setData(cmpPts);
      }} else {{
        if (lwCompareSeries) {{ try {{ lwChart.removeSeries(lwCompareSeries); }} catch(e){{}} lwCompareSeries = null; }}
        const cmpTab = TABS.find(t => t.symbol === compareSym);
        lwCompareSeries = lwChart.addLineSeries({{
          color: '#1a7acc', lineWidth: 2,
          crosshairMarkerVisible: false,
          lastValueVisible: true, priceLineVisible: false,
          title: cmpTab ? cmpTab.label : compareSym,
          priceFormat: {{ type: 'price', precision: 2, minMove: 0.01 }},
        }});
        lwCompareSeries.setData(cmpPts);
      }}
      lwCompareSym = compareSym;
    }}
    lwHasCompare = true;
  }} else {{
    if (lwCompareSeries) {{
      try {{ lwChart.removeSeries(lwCompareSeries); }} catch(e){{}}
      lwCompareSeries = null; lwCompareSym = '';
    }}
    lwHasCompare = false;
  }}

  lwChart.timeScale().fitContent();
}}

// ── Chart / period / type / compare switchers ─────────────────────────────────
function switchChart(sym, name) {{
  currentSym = sym; currentName = name;
  compareSym = '';
  const sel = document.getElementById('compare-select');
  if (sel) sel.value = '';
  document.getElementById('chart-name').textContent = name;
  document.querySelectorAll('.tab') .forEach(t => t.classList.toggle('active', t.dataset.symbol === sym));
  document.querySelectorAll('.tick').forEach(t => t.classList.toggle('active', t.dataset.symbol === sym));
  drawChart(sym, currentPer);
}}
function switchPeriod(p) {{
  currentPer = p;
  const psel = document.getElementById('period-select');
  if (psel && psel.value !== p) psel.value = p;
  drawChart(currentSym, p);
}}
function switchType(type) {{
  chartType = type;
  const tsel = document.getElementById('type-select');
  if (tsel && tsel.value !== type) tsel.value = type;
  drawChart(currentSym, currentPer);
}}
function switchCompare(sym) {{
  compareSym = sym;
  drawChart(currentSym, currentPer);
}}

// ── Theme ─────────────────────────────────────────────────────────────────────
function setTheme(light) {{
  document.getElementById('theme-icon').textContent  = light ? '☽' : '☀';
  document.getElementById('theme-label').textContent = light ? 'Dark' : 'Light';
  light ? document.documentElement.classList.add('light')
        : document.documentElement.classList.remove('light');
  localStorage.setItem('mkt-theme', light ? 'light' : 'dark');
  if (lwChart) drawChart(currentSym, currentPer);
}}
function toggleTheme() {{ setTheme(!document.documentElement.classList.contains('light')); }}
window.addEventListener('DOMContentLoaded', () =>
  setTheme(document.documentElement.classList.contains('light')));

// ── Tickers ───────────────────────────────────────────────────────────────────
function renderTickers(p) {{
  const bar = document.getElementById('tickers-bar');
  TICKER_ORDER.forEach(sym => {{
    const q = p[sym]; if (!q) return;
    const id = 'tick-' + sym.replace(/[\\^=.]/g, '');
    let el = document.getElementById(id);
    if (!el) {{
      el = document.createElement('div');
      el.className = 'tick'; el.id = id; el.dataset.symbol = sym;
      el.onclick = () => switchChart(sym, q.name);
      bar.appendChild(el);
    }}
    const pct = q.pct;
    if (el.dataset.pct !== undefined && el.dataset.pct !== String(pct)) {{
      el.classList.remove('flash-up','flash-dn'); reflow(el);
      el.classList.add(pct > parseFloat(el.dataset.pct) ? 'flash-up' : 'flash-dn');
    }}
    el.dataset.pct = pct;
    el.classList.toggle('active', sym === currentSym);
    el.innerHTML = `<span class="tick-name">${{q.name}}</span>
      <span class="tick-price">${{fmtPrice(sym, q.price)}}</span>
      <span class="tick-pct ${{cls(pct)}}">${{arrow(pct)}} ${{signStr(pct)}}${{Math.abs(pct).toFixed(2)}}%</span>`;
  }});
}}

// ── Tabs ──────────────────────────────────────────────────────────────────────
function renderTabs() {{
  // Ticker tabs
  const ct = document.getElementById('chart-tabs'); ct.innerHTML = '';
  TABS.forEach(t => {{
    const btn = document.createElement('button');
    btn.className = 'tab' + (t.symbol === currentSym ? ' active' : '');
    btn.textContent = t.label; btn.dataset.symbol = t.symbol;
    btn.onclick = () => switchChart(t.symbol, t.name);
    ct.appendChild(btn);
  }});

  // Period dropdown
  const psel = document.getElementById('period-select');
  if (psel) {{
    psel.innerHTML = '';
    PERIODS.forEach(p => {{
      const opt = document.createElement('option');
      opt.value = p; opt.textContent = p;
      if (p === currentPer) opt.selected = true;
      psel.appendChild(opt);
    }});
  }}

  // Chart type dropdown
  const tsel = document.getElementById('type-select');
  if (tsel) {{
    tsel.innerHTML = '';
    CHART_TYPES.forEach(ct2 => {{
      const opt = document.createElement('option');
      opt.value = ct2.id; opt.textContent = ct2.label;
      if (ct2.id === chartType) opt.selected = true;
      tsel.appendChild(opt);
    }});
  }}

  // Comparison dropdown options
  const sel = document.getElementById('compare-select');
  if (sel) {{
    sel.innerHTML = '<option value="">Compare +</option>';
    TABS.forEach(t => {{
      const opt = document.createElement('option');
      opt.value = t.symbol; opt.textContent = t.name;
      sel.appendChild(opt);
    }});
  }}
}}

// ── Source toggles ────────────────────────────────────────────────────────────
function toggleSource(src) {{
  sources[src] = !sources[src];
  const btn = document.getElementById('src-' + src);
  if (sources[src]) btn.classList.add(src === 'yahoo' ? 'active-yahoo' : 'active-cnbc');
  else btn.classList.remove('active-yahoo','active-cnbc');
  applyFilters();
}}

// ── Filter pills ──────────────────────────────────────────────────────────────
function classifyItem(item) {{
  const text = (item.title + ' ' + item.body).toLowerCase();
  const matched = TOPICS.filter(t => t.id !== 'all' && t.keywords.some(kw => text.includes(kw)));
  return matched.length ? matched.map(t => t.id) : ['macro'];
}}

function renderFilterPills() {{
  const row = document.getElementById('filter-row'); if (!row) return; row.innerHTML = '';
  TOPICS.forEach(t => {{
    const btn = document.createElement('button');
    btn.className = 'fpill' + (t.id === activeTopic ? ' active' : '');
    btn.dataset.topic = t.id;
    btn.textContent = t.label;
    btn.onclick = () => {{
      activeTopic = t.id;
      document.querySelectorAll('.fpill').forEach(b =>
        b.classList.toggle('active', b.dataset.topic === t.id));
      applyFilters();
    }};
    row.appendChild(btn);
  }});
}}

function applyFilters() {{
  let visible = 0;
  classifiedNews.forEach(( {{item, topics, el}} ) => {{
    const srcOk   = sources[item.source.toLowerCase()];
    const topicOk = activeTopic === 'all' || topics.includes(activeTopic);
    const show = srcOk && topicOk;
    el.classList.toggle('hidden', !show);
    if (show) visible++;
  }});
  let ph = document.getElementById('no-results');
  if (!ph) {{
    ph = document.createElement('div');
    ph.id = 'no-results'; ph.className = 'no-results';
    document.getElementById('lb-yahoo-panel').appendChild(ph);
    ph.textContent = 'No updates match this filter.';
  }}
  ph.style.display = visible === 0 ? 'block' : 'none';
  var nc = document.getElementById('news-count'); if (nc) nc.textContent = visible + ' / ' + classifiedNews.length;
}}

// ── Ticker detection ──────────────────────────────────────────────────────────
function injectTickerBadges(text) {{
  return text.replace(/\\b([A-Z]{{2,5}})\\b/g, function(match) {{
    if (!KNOWN_TICKERS[match]) return match;
    const p = STOCK_PRICES[match] || ALL_PRICES[match];
    const up = !p || p.pct >= 0;
    const cls = p ? (up ? 'up' : 'dn') : 'neutral';
    const pctStr = p ? ' ' + (p.pct >= 0 ? '+' : '') + p.pct.toFixed(2) + '%' : '';
    return '<span class="ticker-badge ' + cls + '" data-sym="' + match + '" onclick="openTicker(this.dataset.sym)" title="' + (KNOWN_TICKERS[match] || match) + '">' + match + pctStr + '</span>';
  }});
}}

function openTicker(sym) {{
  const p    = STOCK_PRICES[sym] || ALL_PRICES[sym];
  const name = KNOWN_TICKERS[sym] || sym;
  const tab  = TABS.find(t => t.symbol === sym);
  if (tab && ALL_CHARTS[sym]) {{ switchChart(sym, tab.name); return; }}
  showTickerToast(sym, name, p);
}}

function showTickerToast(sym, name, p) {{
  let toast = document.getElementById('ticker-toast');
  if (!toast) {{
    toast = document.createElement('div');
    toast.id = 'ticker-toast'; toast.className = 'ticker-toast';
    document.body.appendChild(toast);
  }}
  const up = !p || p.pct >= 0;
  const price = p ? fmtPrice(sym, p.price) : '—';
  const pct   = p ? (p.pct >= 0 ? '+' : '') + p.pct.toFixed(2) + '%' : '—';
  toast.innerHTML =
    '<span class="tt-sym">' + sym + '</span>' +
    '<span class="tt-name">' + name + '</span>' +
    '<span class="tt-price">' + price + '</span>' +
    '<span class="tt-pct ' + (up ? 'up' : 'dn') + '">' + pct + '</span>';
  toast.className = 'ticker-toast visible';
  clearTimeout(toast._timer);
  toast._timer = setTimeout(function() {{ toast.classList.remove('visible'); }}, 3000);
}}

function toggleExpand(btn) {{
  const body = btn.previousElementSibling;
  const expanded = body.classList.toggle('expanded');
  btn.textContent = expanded ? '↑ Show less' : '↓ Read more';
}}

// ── News render ───────────────────────────────────────────────────────────────
function renderNews(items) {{
  const feed = document.getElementById('news-feed');
  feed.innerHTML = ''; classifiedNews = [];
  const topicMeta = {{}};
  TOPICS.forEach(t => topicMeta[t.id] = t);

  const breaking = [], regular = [];
  items.forEach(item => {{
    const isBreaking = /breaking|urgente/i.test(item.title + ' ' + (item.body || ''));
    (isBreaking ? breaking : regular).push(item);
  }});

  [...breaking, ...regular].forEach(function(item) {{
    const isBreaking = /breaking|urgente/i.test(item.title + ' ' + (item.body || ''));
    const topics  = classifyItem(item);
    const el      = document.createElement('div');
    el.className  = 'news-item' + (isBreaking ? ' breaking' : '');
    const srcCls  = item.source === 'Yahoo' ? 'badge-yahoo' : 'badge-cnbc';
    const tm      = topicMeta[topics[0]];
    const timeStr = item.time ? '<span class="news-time">' + item.time + '</span>' : '';
    const tagStr  = tm ? '<span class="topic-tag" style="background:' + tm.color + '22;color:' + tm.color + '">' + tm.label + '</span>' : '';
    const brkStr  = isBreaking ? '<span class="breaking-label">🔴 Breaking</span>' : '';
    const headline = injectTickerBadges(item.title);
    const headlineHtml = item.url
      ? '<a class="news-link" href="' + item.url + '" target="_blank" rel="noopener">' + headline + '</a>'
      : headline;
    const bodyHtml = item.body && item.body !== 'Live market coverage'
      ? '<div class="news-body">' + item.body + '</div>' +
        '<button class="expand-btn" onclick="toggleExpand(this)">↓ Read more</button>'
      : '';
    el.innerHTML =
      '<div class="news-meta"><span class="badge ' + srcCls + '">' + item.source + '</span>' +
      tagStr + timeStr + brkStr + '</div>' +
      '<div class="news-headline">' + headlineHtml + '</div>' + bodyHtml;
    feed.appendChild(el);
    classifiedNews.push({{item, topics, el}});
  }});
  applyFilters();
}}

// ── Sector Heatmap ────────────────────────────────────────────────────────────
function pctToHeatmapColor(pct) {{
  if (pct === null || pct === undefined) return '#3a3a4a';
  if (pct >=  2.0) return '#1a7a3c';
  if (pct >=  0.5) return '#2d9e55';
  if (pct >=  0.0) return '#4ab870';
  if (pct >= -0.5) return '#c0392b';
  if (pct >= -2.0) return '#a93226';
  return '#7b241c';
}}

function renderHeatmap() {{
  const grid = document.getElementById('heatmap-grid');
  if (!grid) return;
  grid.innerHTML = SECTOR_ETFS.map(function(s) {{
    const d = STOCK_PRICES[s.sym] || ALL_PRICES[s.sym];
    const pct = d ? d.pct : null;
    const bg = pctToHeatmapColor(pct);
    const pctLabel = pct !== null ? (pct >= 0 ? '+' : '') + pct.toFixed(2) + '%' : 'N/A';
    return '<div class="heatmap-cell" style="background:' + bg + '" title="' + s.sym + '">'
         + '<span class="hm-label">' + s.label + '</span>'
         + '<span class="hm-pct">' + pctLabel + '</span>'
         + '</div>';
  }}).join('');
}}

// ── Top Movers ────────────────────────────────────────────────────────────────
var activeMoversTab = 'gainers';

var STOCK_NAME_MAP = {{
  'AAPL':'Apple','MSFT':'Microsoft','NVDA':'Nvidia','TSLA':'Tesla',
  'GOOGL':'Alphabet','META':'Meta','AMZN':'Amazon','JPM':'JPMorgan',
  'BAC':'Bank of America','GS':'Goldman Sachs','V':'Visa','MA':'Mastercard',
  'NFLX':'Netflix','AMD':'AMD','INTC':'Intel','QCOM':'Qualcomm',
  'XOM':'ExxonMobil','LLY':'Eli Lilly','WMT':'Walmart','COIN':'Coinbase',
  'DIS':'Disney','F':'Ford','GM':'GM'
}};

function renderMovers() {{
  const list = document.getElementById('movers-list');
  if (!list) return;
  var entries = [];
  Object.keys(STOCK_PRICES).forEach(function(sym) {{
    if (['XLK','XLE','XLF','XLV','XLI','XLB','XLU','XLP','XLY','XLRE','XLC'].indexOf(sym) >= 0) return;
    var d = STOCK_PRICES[sym];
    if (d && d.pct !== undefined) entries.push({{sym: sym, price: d.price, pct: d.pct}});
  }});
  entries.sort(function(a, b) {{
    return activeMoversTab === 'gainers' ? b.pct - a.pct : a.pct - b.pct;
  }});
  var top5 = entries.slice(0, 5);
  list.innerHTML = top5.map(function(e) {{
    var up = e.pct >= 0;
    var pctStr = (up ? '+' : '') + e.pct.toFixed(2) + '%';
    var name = STOCK_NAME_MAP[e.sym] || e.sym;
    return '<div class="mover-row">'
         + '<div class="mover-left">'
         + '<span class="mover-ticker">' + e.sym + '</span>'
         + '<span class="mover-name">' + name + '</span>'
         + '</div>'
         + '<div class="mover-right">'
         + '<span class="mover-price">$' + e.price.toFixed(2) + '</span>'
         + '<span class="mover-pct ' + (up ? 'up' : 'dn') + '">' + pctStr + '</span>'
         + '</div>'
         + '</div>';
  }}).join('');
}}

function switchMoversTab(tab) {{
  activeMoversTab = tab;
  document.querySelectorAll('.mover-tab').forEach(function(b) {{ b.classList.remove('active'); }});
  var el = document.getElementById('tab-' + tab);
  if (el) el.classList.add('active');
  renderMovers();
}}

// ── View switcher ─────────────────────────────────────────────────────────────
var _studioInit = false;
function switchView(view) {{
  var dash   = document.getElementById('view-dashboard');
  var news   = document.getElementById('view-news');
  var studio = document.getElementById('view-studio');
  dash.style.display   = view === 'dashboard' ? '' : 'none';
  news.style.display   = view === 'news'      ? 'flex' : 'none';
  studio.style.display = view === 'studio'    ? 'flex' : 'none';
  document.getElementById('nav-dashboard').classList.toggle('active', view === 'dashboard');
  document.getElementById('nav-news').classList.toggle('active', view === 'news');
  document.getElementById('nav-studio').classList.toggle('active', view === 'studio');
  if (view === 'studio' && !_studioInit) {{ initCreatorStudio(); _studioInit = true; }}
}}

// ── Creator Studio ────────────────────────────────────────────────────────────
var GLOSSARY = {{
  'IPO':       'Oferta Pública Inicial — cuando una empresa sale a cotizar en bolsa por primera vez',
  'Fed':       'Reserva Federal — el banco central de EE.UU. que fija las tasas de interés',
  'FED':       'Reserva Federal — el banco central de EE.UU. que fija las tasas de interés',
  'CPI':       'Índice de Precios al Consumidor — mide la inflación',
  'PCE':       'Gasto de Consumo Personal — el indicador de inflación favorito de la Fed',
  'GDP':       'Producto Interno Bruto — mide el tamaño de la economía',
  'ETF':       'Fondo cotizado en bolsa — canasta de activos que se compra como una acción',
  'yield':     'rendimiento del bono — lo que te paga por prestarle plata al gobierno',
  'Yield':     'rendimiento del bono — lo que te paga por prestarle plata al gobierno',
  'earnings':  'resultados trimestrales de ganancias empresariales',
  'Earnings':  'resultados trimestrales de ganancias empresariales',
  'rally':     'suba fuerte del mercado',
  'Rally':     'suba fuerte del mercado',
  'selloff':   'caída masiva con muchos inversores vendiendo al mismo tiempo',
  'sell-off':  'caída masiva con muchos inversores vendiendo al mismo tiempo',
  'hawkish':   'postura restrictiva — el banco central quiere subir tasas para frenar la inflación',
  'dovish':    'postura laxa — el banco central prefiere bajar tasas para estimular la economía',
  'recession': 'recesión — dos trimestres seguidos de caída del PIB',
  'Recession': 'recesión — dos trimestres seguidos de caída del PIB',
  'default':   'cesación de pagos — el deudor no puede cumplir sus obligaciones',
  'Default':   'cesación de pagos — el deudor no puede cumplir sus obligaciones',
  'tariff':    'arancel — impuesto que se cobra sobre productos importados',
  'Tariff':    'arancel — impuesto que se cobra sobre productos importados',
  'breakout':  'ruptura — cuando el precio supera un nivel clave de resistencia (señal técnica alcista)',
}};

function injectGlossary(text) {{
  var used = {{}};
  Object.keys(GLOSSARY).forEach(function(term) {{
    if (used[term.toLowerCase()]) return;
    var re = new RegExp('\\\\b' + term.replace(/[.*+?^${{}}()|[\\]\\\\]/g,'\\\\$&') + '\\\\b','g');
    if (re.test(text)) {{
      used[term.toLowerCase()] = true;
      text = text.replace(re, term + ' (' + GLOSSARY[term] + ')');
    }}
  }});
  return text;
}}

function renderCobraCard(spxData, isLive) {{
  var card = document.getElementById('studio-cobra-card');
  if (!card || !spxData) return;
  var up   = spxData.pct >= 0;
  var sign = up ? '+' : '';
  var pStr = spxData.price.toLocaleString('en-US', {{minimumFractionDigits:2,maximumFractionDigits:2}});
  var lbl  = 'S&amp;P 500 · ' + (isLive ? 'Tiempo real' : 'Al cierre');
  card.innerHTML =
    '<div class="cobra-label">' + lbl + '</div>' +
    '<div class="cobra-name">🐍 La Cobra Achorada</div>' +
    '<div class="cobra-metrics">' +
      '<div class="cobra-metric">' +
        '<span class="cobra-metric-label">Precio</span>' +
        '<span class="cobra-metric-val ' + (up?'up':'dn') + '">' + pStr + '</span>' +
      '</div>' +
      '<div class="cobra-metric">' +
        '<span class="cobra-metric-label">Variación</span>' +
        '<span class="cobra-metric-val ' + (up?'up':'dn') + '">' + sign + spxData.pct.toFixed(2) + '%</span>' +
      '</div>' +
      '<div class="cobra-metric">' +
        '<span class="cobra-metric-label">Apertura</span>' +
        '<span class="cobra-metric-val">' + (spxData.open ? spxData.open.toLocaleString('en-US',{{minimumFractionDigits:2,maximumFractionDigits:2}}) : '—') + '</span>' +
      '</div>' +
    '</div>';
}}

async function initCreatorStudio() {{
  // Cobra card: show stale data immediately, then refresh with live data
  var card = document.getElementById('studio-cobra-card');
  if (card) {{
    card.innerHTML = '<div class="cobra-label">Obteniendo datos en tiempo real...</div>';
  }}
  var live = await fetchLiveQuotes();
  if (live) {{ _liveQuotes = live; }}
  var spx = (_liveQuotes && _liveQuotes['^GSPC']) ? _liveQuotes['^GSPC'] : ALL_PRICES['^GSPC'];
  renderCobraCard(spx, !!(_liveQuotes && _liveQuotes['^GSPC']));

  // Events list: populate from market events already identified by Gemini
  _refreshStudioEventsList();
}}

function _refreshStudioEventsList() {{
  var list = document.getElementById('studio-news-list');
  if (!list) return;
  // Use freshly fetched events if available, else fall back to INIT_EVENTS
  var events = (window._latestMarketEvents && window._latestMarketEvents.length > 0)
    ? window._latestMarketEvents : (INIT_EVENTS || []);
  if (events.length === 0) {{
    list.innerHTML = '<div style="font-size:.7rem;color:var(--text-muted);padding:8px 0">No hay eventos disponibles. Intentá actualizar desde la pestaña Noticias.</div>';
    return;
  }}
  list.innerHTML = '';
  events.forEach(function(ev, i) {{
    var dir = ev.direction === 'up' ? '▲' : ev.direction === 'down' ? '▼' : '●';
    var dirColor = ev.direction === 'up' ? 'var(--green)' : ev.direction === 'down' ? 'var(--red)' : 'var(--text-muted)';
    var el = document.createElement('label');
    el.className = 'news-sel-item';
    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.setAttribute('data-idx', i);
    cb.setAttribute('data-ev', JSON.stringify(ev));
    cb.addEventListener('change', function() {{ onNewsCheck(this); }});
    var meta = document.createElement('div');
    meta.style.flex = '1';
    meta.innerHTML =
      '<div class="news-sel-title">' +
        '<span style="color:' + dirColor + ';margin-right:4px">' + dir + '</span>' +
        _escHtml(ev.headline || '').slice(0, 110) +
      '</div>' +
      '<div class="news-sel-meta">' +
        _escHtml(ev.source || '') +
        (ev.spx_impact ? ' · ' + _escHtml(ev.spx_impact) : '') +
        (ev.time_et ? ' · ' + _escHtml(ev.time_et) : '') +
      '</div>';
    el.appendChild(cb);
    el.appendChild(meta);
    list.appendChild(el);
  }});
}}

function onNewsCheck(cb) {{
  var checked = document.querySelectorAll('#studio-news-list input[type=checkbox]:checked');
  if (checked.length > 6) {{ cb.checked = false; return; }}
  cb.closest('.news-sel-item').classList.toggle('selected', cb.checked);
}}

function getSelectedNews() {{
  // Prefer live posts from the liveblog; fall back to static INIT_NEWS
  var src = _studioLivePosts.length > 0 ? _studioLivePosts : null;
  var checked = Array.from(document.querySelectorAll('#studio-news-list input[type=checkbox]:checked'));

  if (checked.length > 0) {{
    return checked.map(function(cb) {{
      var idx = parseInt(cb.getAttribute('data-idx'));
      if (src) {{
        var post = src[idx];
        // Normalise liveblog post → news-like object expected by the report generator
        return {{
          id:       post.id       || '',
          title:    post.headline || '',
          source:   post.source   || '',
          body:     (post.paragraphs || []).join(' '),
          que_paso: (post.summary && post.summary.que_paso)  || '',
          por_que:  (post.summary && post.summary.por_que)   || '',
          reaccion: (post.summary && post.summary.reaccion)  || '',
          ai:       !!(post.summary && post.summary.ai_generated),
        }};
      }}
      return INIT_NEWS[idx] || {{}};
    }});
  }}

  // No selection → top 3 from live source
  if (src) {{
    return src.slice(0, 3).map(function(post) {{
      return {{
        id:       post.id       || '',
        title:    post.headline || '',
        source:   post.source   || '',
        body:     (post.paragraphs || []).join(' '),
        que_paso: (post.summary && post.summary.que_paso)  || '',
        por_que:  (post.summary && post.summary.por_que)   || '',
        reaccion: (post.summary && post.summary.reaccion)  || '',
        ai:       !!(post.summary && post.summary.ai_generated),
      }};
    }});
  }}
  return INIT_NEWS.slice(0, 3);
}}

// Fetch one symbol from Yahoo Finance v8 chart API via allorigins proxy.
// Returns {{symbol, price, pct, timestamps, closes, prevClose}} or null.
function _fetchV8(sym, withIntraday) {{
  var qs = withIntraday ? 'interval=1m&range=1d' : 'interval=1d&range=1d';
  var chartUrl = 'https://query2.finance.yahoo.com/v8/finance/chart/' + encodeURIComponent(sym) + '?' + qs;
  return fetch(CORS_PROXY + encodeURIComponent(chartUrl), {{cache:'no-store'}})
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      var raw  = JSON.parse(data.contents);
      var res  = raw.chart.result[0];
      var meta = res.meta;
      var price = meta.regularMarketPrice;
      var prev  = meta.chartPreviousClose || meta.previousClose;
      var pct   = prev ? (price - prev) / prev * 100 : 0;
      var out   = {{symbol: sym, price: price, pct: pct, prevClose: prev || 0}};
      if (withIntraday && res.timestamp) {{
        out.timestamps = res.timestamp;
        out.closes     = (res.indicators.quote[0].close || []).map(function(v) {{ return v == null ? null : v; }});
      }}
      return out;
    }})
    .catch(function() {{ return null; }});
}}

// Fetch live quotes from Yahoo Finance for the report generator.
// Returns a dict keyed by symbol with {{price, pct}} or null on failure.
async function fetchLiveQuotes() {{
  var SYMS = ['^GSPC', '^DJI', '^IXIC', '^VIX', '^RUT', 'GC=F', 'CL=F', 'BTC-USD'];
  try {{
    var responses = await Promise.all(SYMS.map(function(s) {{ return _fetchV8(s, false); }}));
    var out = {{}};
    responses.forEach(function(r) {{ if (r) out[r.symbol] = r; }});
    return Object.keys(out).length >= 3 ? out : null;
  }} catch(e) {{
    console.warn('[quotes] live fetch failed:', e.message);
    return null;
  }}
}}

// Refresh main index prices + update ticker bar + extend 1D chart with latest price.
async function refreshLivePrices() {{
  try {{
    // Intentar primero el backend propio (más confiable que el CORS proxy)
    var live = null;
    try {{
      var apiResp = await fetch(API_BASE + '/api/prices', {{cache:'no-store'}});
      if (apiResp.ok) {{
        var apiData = await apiResp.json();
        // El backend devuelve {{sym: {{name, price, pct, open}}}} — adaptar al formato interno
        live = {{}};
        Object.keys(apiData).forEach(function(sym) {{
          var d = apiData[sym];
          if (d && d.price != null) {{
            live[sym] = {{symbol: sym, price: d.price, pct: d.pct, prevClose: d.open || 0}};
          }}
        }});
        if (Object.keys(live).length < 3) live = null;  // fallback si muy pocos datos
      }}
    }} catch(_e) {{}}
    // Fallback al CORS proxy de Yahoo si el backend no respondió
    if (!live) live = await fetchLiveQuotes();
    if (!live) return;
    _liveQuotes = live;

    // Update the runtime prices dict used by renderTickers
    Object.keys(live).forEach(function(sym) {{
      if (prices[sym]) {{
        prices[sym] = Object.assign({{}}, prices[sym], {{price: live[sym].price, pct: live[sym].pct}});
      }} else if (live[sym]) {{
        prices[sym] = {{name: sym, symbol: sym, price: live[sym].price, pct: live[sym].pct, open: live[sym].prevClose}};
      }}
    }});
    renderTickers(prices);

    // Extend 1D chart with latest price for the currently-viewed symbol
    if (currentPer === '1D' && lwPriceSeries && live[currentSym]) {{
      var livePrice = live[currentSym].price;
      var nowMin    = Math.floor(Date.now() / 60000) * 60; // round to nearest minute (seconds)
      try {{
        if (chartType === 'candle' || chartType === 'bar') {{
          lwPriceSeries.update({{time: nowMin, open: livePrice, high: livePrice, low: livePrice, close: livePrice}});
        }} else {{
          lwPriceSeries.update({{time: nowMin, value: livePrice}});
        }}
      }} catch(e) {{}}

      // Update the big price number and change % shown in the chart header
      var pe = document.getElementById('chart-price');
      if (pe && pe.textContent !== '—') {{
        pe.textContent = fmtPrice(currentSym, livePrice);
        var pct    = live[currentSym].pct;
        var prev   = live[currentSym].prevClose;
        var chgAbs = livePrice - prev;
        var sign   = pct >= 0 ? '+' : '';
        var clsStr = pct >= 0 ? 'up' : 'dn';
        var ce = document.getElementById('chart-chg');
        if (ce) {{
          ce.textContent = (pct >= 0 ? '▲' : '▼') + ' ' + sign + Math.abs(chgAbs).toFixed(2) + '  (' + sign + pct.toFixed(2) + '%)';
          ce.className   = 'chart-chg ' + clsStr;
        }}
      }}
    }}
  }} catch(e) {{
    console.warn('[refreshLivePrices] error:', e.message);
  }}
}}

// Refresh individual stock prices (heatmap + movers). Heavier — runs every 5 min.
async function refreshStockPrices() {{
  var STOCK_SYMS = {stock_syms_json};
  try {{
    // Intentar primero el backend propio
    var apiResp = await fetch(API_BASE + '/api/stocks', {{cache:'no-store'}});
    if (apiResp.ok) {{
      var apiData = await apiResp.json();
      Object.keys(apiData).forEach(function(sym) {{
        var d = apiData[sym];
        if (!d) return;
        if (STOCK_PRICES[sym]) {{
          STOCK_PRICES[sym].price = d.price;
          STOCK_PRICES[sym].pct   = d.pct;
        }} else {{
          STOCK_PRICES[sym] = {{price: d.price, pct: d.pct}};
        }}
      }});
    }} else {{
      // Fallback a Yahoo v8 directo
      var responses = await Promise.all(STOCK_SYMS.map(function(s) {{ return _fetchV8(s, false); }}));
      responses.forEach(function(r) {{
        if (r && STOCK_PRICES[r.symbol]) {{
          STOCK_PRICES[r.symbol].price = r.price;
          STOCK_PRICES[r.symbol].pct   = r.pct;
        }}
      }});
    }}
    renderHeatmap();
    renderMovers();
  }} catch(e) {{
    console.warn('[refreshStockPrices] error:', e.message);
  }}
}}

async function translateToSpanish(text) {{
  if (!text || !text.trim()) return text;
  try {{
    var url = 'https://api.mymemory.translated.net/get?q=' + encodeURIComponent(text.slice(0, 500)) + '&langpair=en|es';
    var resp = await fetch(url, {{cache: 'no-store'}});
    if (!resp.ok) return text;
    var data = await resp.json();
    if (data.responseStatus === 200 && data.responseData && data.responseData.translatedText) {{
      return data.responseData.translatedText || text;
    }}
    return text;
  }} catch(e) {{
    return text;
  }}
}}

async function generateFlipReport() {{
  var btn    = document.getElementById('gen-btn');
  var status = document.getElementById('gen-status');
  var ta     = document.getElementById('report-textarea');

  btn.disabled = true;
  ta.value     = '';
  status.className   = 'gen-status';
  status.textContent = 'Preparando datos...';

  // Live quotes
  var live = await fetchLiveQuotes();
  if (live) {{ _liveQuotes = live; }}
  function getLive(sym) {{
    if (_liveQuotes && _liveQuotes[sym]) return _liveQuotes[sym];
    return ALL_PRICES[sym] || null;
  }}
  var spx  = getLive('^GSPC');
  var ndx  = getLive('^IXIC');
  var djia = getLive('^DJI');
  var vix  = getLive('^VIX');
  renderCobraCard(spx, !!(_liveQuotes && _liveQuotes['^GSPC']));

  // Collect selected events
  var checkedCbs = Array.from(document.querySelectorAll('#studio-news-list input[type=checkbox]:checked'));
  var selectedEvents;
  if (checkedCbs.length > 0) {{
    selectedEvents = checkedCbs.map(function(cb) {{
      try {{ return JSON.parse(cb.getAttribute('data-ev')); }} catch(e) {{ return null; }}
    }}).filter(Boolean);
  }} else {{
    selectedEvents = (window._latestMarketEvents && window._latestMarketEvents.length > 0)
      ? window._latestMarketEvents : (INIT_EVENTS || []);
  }}

  if (selectedEvents.length === 0) {{
    status.textContent = 'No hay eventos disponibles. Actualizá desde la pestaña Noticias.';
    btn.disabled = false;
    return;
  }}

  // Build prices object
  function fmtPct(pct) {{ return (pct >= 0 ? '+' : '') + (pct || 0).toFixed(2) + '%'; }}
  var prices = {{
    spx_pct:   spx  ? fmtPct(spx.pct)   : 'N/D',
    spx_price: spx  ? spx.price.toLocaleString('en-US', {{minimumFractionDigits:2, maximumFractionDigits:2}}) : 'N/D',
    ndx_pct:   ndx  ? fmtPct(ndx.pct)   : 'N/D',
    dj_pct:    djia ? fmtPct(djia.pct)  : 'N/D',
    vix_val:   vix  ? vix.price.toFixed(2) : 'N/D',
  }};

  // Date
  var today = new Date();
  var dias  = ['domingo','lunes','martes','miércoles','jueves','viernes','sábado'];
  var meses = ['enero','febrero','marzo','abril','mayo','junio','julio','agosto','septiembre','octubre','noviembre','diciembre'];
  var fecha = dias[today.getDay()] + ' ' + today.getDate() + ' de ' + meses[today.getMonth()];
  var dayOfWeek = dias[today.getDay()];

  status.textContent = 'Generando reporte con Gemini...';

  try {{
    var resp = await fetch(API_BASE + '/api/generate-report', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ events: selectedEvents, prices: prices, date: fecha, day_of_week: dayOfWeek }}),
      cache: 'no-store',
    }});
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var data = await resp.json();
    if (data.error) throw new Error(data.error);
    ta.value = data.report || '';
    updateCharCount();
    status.textContent = '✓ Reporte generado con Gemini' + (live ? ' · datos en tiempo real' : ' · datos al cierre');
  }} catch(e) {{
    status.textContent = '✗ Error: ' + (e.message || 'proxy no disponible') + '. Iniciá news_proxy.py primero.';
    console.error('[generateFlipReport]', e);
  }}
  btn.disabled = false;
}}

function updateCharCount() {{
  var ta = document.getElementById('report-textarea');
  var cc = document.getElementById('char-count');
  if (ta && cc) cc.textContent = ta.value.length.toLocaleString() + ' caracteres';
}}

function copyReport() {{
  var ta = document.getElementById('report-textarea');
  if (!ta || !ta.value.trim()) return;
  var txt = ta.value;
  navigator.clipboard.writeText(txt).then(function() {{
    showToast('✓ ¡Reporte copiado!');
  }}).catch(function() {{
    ta.select(); document.execCommand('copy');
    showToast('✓ ¡Reporte copiado!');
  }});
}}

function subirReporte() {{
  showToast('🔗 Integración con plataforma próximamente...');
}}

function showToast(msg) {{
  var t = document.getElementById('copy-toast');
  if (!t) return;
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(function() {{ t.classList.remove('show'); }}, 2600);
}}

// ── Copy Flip Summary ─────────────────────────────────────────────────────────
function copyFlipSummary() {{
  // Index data — prefer live quotes fetched at runtime
  var spx = (_liveQuotes && _liveQuotes['^GSPC']) ? _liveQuotes['^GSPC'] : ALL_PRICES['^GSPC'];
  var ndx = (_liveQuotes && _liveQuotes['^IXIC']) ? _liveQuotes['^IXIC'] : ALL_PRICES['^IXIC'];
  function fmtPrice(d, sym) {{
    if (!d) return sym;
    var sign = d.pct >= 0 ? '+' : '';
    var p = d.price >= 1000
      ? d.price.toLocaleString('en-US', {{minimumFractionDigits:2,maximumFractionDigits:2}})
      : d.price.toFixed(2);
    return p + ' (' + sign + d.pct.toFixed(2) + '%)';
  }}

  // Top 3 visible headlines
  var headlines = [];

  // Leading sector (highest pct from SECTOR_ETFS)
  var best = null, bestPct = -Infinity;
  SECTOR_ETFS.forEach(function(s) {{
    var d = STOCK_PRICES[s.sym] || ALL_PRICES[s.sym];
    if (d && d.pct > bestPct) {{ bestPct = d.pct; best = s; }}
  }});
  var sectorStr = best
    ? best.label + ' (' + (bestPct >= 0 ? '+' : '') + bestPct.toFixed(2) + '%)'
    : 'N/D';

  // Build text
  var lines = ['\\uD83D\\uDCCA Resumen de Mercado – Flip Inversiones'];
  var body = 'El S&P 500 cotiza en ' + fmtPrice(spx, 'S&P 500') + '. ';
  body    += 'El Nasdaq en ' + fmtPrice(ndx, 'Nasdaq') + '. ';
  if (headlines.length > 0) {{
    var hStr = headlines.length === 1
      ? headlines[0]
      : headlines.slice(0,-1).join('; ') + ' y ' + headlines[headlines.length-1];
    body += 'El mercado reacciona a: ' + hStr + '. ';
  }}
  body += 'Sector líder del día: ' + sectorStr + '.';
  lines.push(body);
  var text = lines.join('\\n');

  // Copy and toast
  navigator.clipboard.writeText(text).then(function() {{
    showToast('\\uD83D\\uDCCB \\u00A1Resumen copiado!');
  }}).catch(function() {{
    var ta = document.createElement('textarea');
    ta.value = text; ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    showToast('\\uD83D\\uDCCB \\u00A1Resumen copiado!');
  }});
}}

// ── Live news auto-refresh ─────────────────────────────────────────────────────
// CNBC via RSS; Yahoo via direct live-blog page scrape (gets the "Stock Market Today" headline)
var CORS_PROXY = 'https://api.allorigins.win/get?url=';

function _hashStr(s) {{
  var h = 0;
  for (var i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
  return Math.abs(h);
}}

function _relTime(pubDate) {{
  if (!pubDate) return '';
  var diffMins = Math.round((Date.now() - new Date(pubDate).getTime()) / 60000);
  if (diffMins < 1)        return 'Just now';
  if (diffMins < 60)       return diffMins + ' min ago';
  if (diffMins < 120)      return '1 hr ago';
  return Math.round(diffMins / 60) + ' hrs ago';
}}





// ── Live Blog Cards ────────────────────────────────────────────────────────


var _lbView = 'feed';

// Creator Studio live feed — updated every time liveblog cards are rendered
var _studioYahooPosts = [];
var _studioCnbcPosts  = [];
var _studioLivePosts  = [];   // interleaved, max 12, fed into the studio news list

function switchLbView(mode) {{
  _lbView = mode;
  var yPanel = document.getElementById('lb-yahoo-panel');
  var cPanel = document.getElementById('lb-cnbc-panel');
  var tabY   = document.getElementById('tab-yahoo');
  var tabC   = document.getElementById('tab-cnbc');
  if (mode === 'cnbc') {{
    if (yPanel) yPanel.classList.remove('visible');
    if (cPanel) cPanel.classList.add('visible');
    if (tabY)   tabY.classList.remove('active');
    if (tabC)   tabC.classList.add('active');
    if (cPanel && !cPanel.querySelector('.lb-card')) fetchCnbcLbCards();
  }} else {{
    if (yPanel) yPanel.classList.add('visible');
    if (cPanel) cPanel.classList.remove('visible');
    if (tabY)   tabY.classList.add('active');
    if (tabC)   tabC.classList.remove('active');
    if (yPanel && !yPanel.querySelector('.lb-card')) fetchYahooLbCards();
  }}
}}

function _lbSrcClass(src) {{
  if (!src) return 'cnbc';
  return (src + '').toLowerCase().includes('yahoo') ? 'yahoo' : 'cnbc';
}}

function _lbRelTime(ts) {{
  if (!ts) return '';
  var diff = Math.round((Date.now() / 1000) - ts);
  if (diff < 60)   return 'hace ' + diff + 's';
  if (diff < 3600) return 'hace ' + Math.round(diff/60) + 'min';
  if (diff < 86400) return 'hace ' + Math.round(diff/3600) + 'h';
  return 'hace ' + Math.round(diff/86400) + 'd';
}}

function toggleLbCard(btn) {{
  var card = btn.closest('.lb-card');
  if (!card) return;
  card.classList.toggle('open');
}}

function _esc(s) {{ return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }}

function _buildSections(post) {{
  var summ  = post.summary || {{}};
  var paras = Array.isArray(post.paragraphs) ? post.paragraphs : [];

  // Si hay resumen IA, usarlo directamente
  if (summ.ai_generated && (summ.que_paso || summ.por_que || summ.reaccion)) {{
    var badge = '<span style="font-size:.52rem;font-weight:700;letter-spacing:.06em;color:var(--accent);vertical-align:middle;margin-left:6px;opacity:.7">✦ IA</span>';
    var s = '';
    if (summ.que_paso)  s += '<div class="lb-section">¿Qué pasó? ' + badge + '</div><p class="lb-para">' + _esc(summ.que_paso)  + '</p>';
    if (summ.por_que)   s += '<div class="lb-section">¿Por qué?</div><p class="lb-para">'   + _esc(summ.por_que)   + '</p>';
    if (summ.reaccion)  s += '<div class="lb-section">Reacción del mercado</div><p class="lb-para">' + _esc(summ.reaccion) + '</p>';
    return s;
  }}

  // Sin IA: mostrar todos los párrafos disponibles completos
  if (!paras.length) return '<p class="lb-para" style="color:var(--text-dim)">Sin contenido disponible.</p>';
  var s = '';
  // Primer bloque: lo que pasó (hasta 2 párrafos)
  var q = paras.slice(0, 2).map(function(t) {{ return '<p class="lb-para">' + _esc(t) + '</p>'; }}).join('');
  // Segundo bloque: contexto / causas (párrafos 3-4)
  var w = paras.slice(2, 4).map(function(t) {{ return '<p class="lb-para">' + _esc(t) + '</p>'; }}).join('');
  // Tercer bloque: reacción / resto (párrafos 5 en adelante — sin tope)
  var r = paras.slice(4).map(function(t)  {{ return '<p class="lb-para">' + _esc(t) + '</p>'; }}).join('');
  if (q) s += '<div class="lb-section">¿Qué pasó?</div>'             + q;
  if (w) s += '<div class="lb-section">¿Por qué?</div>'              + w;
  if (r) s += '<div class="lb-section">Reacción del mercado</div>'   + r;
  return s || '<p class="lb-para">' + _esc(paras[0]) + '</p>';
}}

function renderLiveBlogCards(posts, aiEnabled, panelId) {{
  var panel = document.getElementById(panelId || 'lb-cnbc-panel');
  if (!panel) return;
  if (!posts || posts.length === 0) {{
    panel.innerHTML = '<div class="lb-empty">No se encontraron actualizaciones del live blog.<br><small>Asegúrate de que el proxy está corriendo: <code>python3 news_proxy.py</code></small></div>';
    return;
  }}

  var aiNote = aiEnabled
    ? '<div style="padding:6px 14px 4px;font-size:.58rem;color:var(--accent);font-family:var(--mono)">✦ Resúmenes generados con IA · Actualiza cada 6 min</div>'
    : '<div style="padding:6px 14px 4px;font-size:.58rem;color:var(--text-dim);font-family:var(--mono)">⚡ Texto original de la fuente · Sin API key de IA</div>';

  var html = aiNote;
  posts.forEach(function(p) {{
    var srcCls   = _lbSrcClass(p.source);
    var srcLabel = p.source || 'CNBC';
    var timeStr  = _lbRelTime(p.timestamp);
    var sections = _buildSections(p);
    var linkHtml = p.url
      ? '<a class="lb-link" href="' + p.url + '" target="_blank" rel="noopener">Leer nota completa →</a>'
      : '';
    html += '<div class="lb-card" id="lbc-' + (p.id||'') + '">' +
      '<div class="lb-card-head" onclick="toggleLbCard(this)">' +
        '<div class="lb-src-row">' +
          '<span class="lb-src-badge ' + srcCls + '">' + _esc(srcLabel) + '</span>' +
          (timeStr ? '<span class="lb-ts">' + timeStr + '</span>' : '') +
        '</div>' +
        '<div class="lb-headline">' + _esc(p.headline||'') + '</div>' +
        '<span class="lb-chevron">▼</span>' +
      '</div>' +
      '<div class="lb-body">' + sections + linkHtml + '</div>' +
    '</div>';
  }});
  panel.innerHTML = html;
}}

async function _fetchLbSource(source, panelId) {{
  var panel = document.getElementById(panelId);
  if (!panel) return;
  if (!panel.querySelector('.lb-card')) {{
    panel.innerHTML = '<div class="lb-loading">Cargando cobertura ' + source + '…</div>';
  }}
  try {{
    var resp = await fetch(API_BASE + '/api/liveblog?source=' + source, {{
      cache: 'no-store',
      signal: AbortSignal.timeout(10000)
    }});
    if (!resp.ok) throw new Error('proxy ' + resp.status);
    var data = await resp.json();
    var posts = data.posts || [];
    renderLiveBlogCards(posts, !!data.ai_enabled, panelId);
    _updateStudioFromLb(source, posts);
    return;
  }} catch(e) {{
    console.info('[liveblog ' + source + '] proxy no disponible');
  }}
  // Fallback: INIT_NEWS filtrado por fuente
  var srcKey = source === 'yahoo' ? 'Yahoo' : 'CNBC';
  var fallback = (window.INIT_NEWS || []).filter(function(n) {{
    return n.body && n.body.length > 40 && (n.source || '').toLowerCase().includes(srcKey.toLowerCase());
  }}).map(function(n) {{
    return {{
      id: n.id||'', headline: n.title||'',
      paragraphs: [n.body], timestamp: n.timestamp||0,
      source: n.source||srcKey, url: n.url||'', summary: null,
    }};
  }});
  renderLiveBlogCards(fallback, false, panelId);
  _updateStudioFromLb(source, fallback);
}}

// Called after each liveblog fetch; keeps studio in sync with live content.
function _updateStudioFromLb(source, posts) {{
  if (source === 'yahoo') _studioYahooPosts = posts;
  else                    _studioCnbcPosts  = posts;

  // Interleave Yahoo + CNBC so both sources appear in the studio list
  var combined = [], yi = 0, ci = 0;
  while (combined.length < 12 &&
         (yi < _studioYahooPosts.length || ci < _studioCnbcPosts.length)) {{
    if (yi < _studioYahooPosts.length) combined.push(_studioYahooPosts[yi++]);
    if (ci < _studioCnbcPosts.length && combined.length < 12) combined.push(_studioCnbcPosts[ci++]);
  }}
  _studioLivePosts = combined;
  _refreshStudioNewsList();
}}

// Rebuilds the studio news-selection list from _studioLivePosts.
// Preserves any already-checked items by id.
function _refreshStudioNewsList() {{
  var list = document.getElementById('studio-news-list');
  if (!list) return;
  if (_studioLivePosts.length === 0) return;

  // Preserve checked ids across refresh
  var prevChecked = new Set(
    Array.from(list.querySelectorAll('input[type=checkbox]:checked'))
        .map(function(cb) {{ return cb.getAttribute('data-id'); }})
  );

  list.innerHTML = '';
  _studioLivePosts.forEach(function(post, i) {{
    var el = document.createElement('label');
    el.className = 'news-sel-item';
    var isChecked = prevChecked.has(post.id || String(i));
    if (isChecked) el.classList.add('selected');

    var cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.setAttribute('data-idx', i);
    cb.setAttribute('data-id', post.id || String(i));
    cb.checked = isChecked;
    cb.addEventListener('change', function() {{ onNewsCheck(this); }});

    var srcBadge = (post.source || '').includes('CNBC') ? 'CNBC' : 'Yahoo';
    var meta = document.createElement('div');
    meta.innerHTML =
      '<div class="news-sel-title">' + (post.headline || '').replace(/</g, '&lt;').slice(0, 120) + '</div>' +
      '<div class="news-sel-meta">' + srcBadge + '</div>';

    el.appendChild(cb);
    el.appendChild(meta);
    list.appendChild(el);
  }});
}}

function fetchYahooLbCards() {{ return _fetchLbSource('yahoo', 'lb-yahoo-panel'); }}
function fetchCnbcLbCards()  {{ return _fetchLbSource('cnbc',  'lb-cnbc-panel');  }}
function fetchAllLbCards()   {{
  var btn = document.getElementById('refresh-btn');
  if (btn) {{ btn.classList.add('spinning'); btn.disabled = true; }}
  Promise.all([fetchYahooLbCards(), fetchCnbcLbCards()]).finally(function() {{
    if (btn) {{ btn.classList.remove('spinning'); btn.disabled = false; }}
  }});
}}

// ── End Live Blog Cards ─────────────────────────────────────────────────────


// ── Market Events (Noticias tab) ─────────────────────────────────────────────

function _escHtml(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

function _renderMarketEvents(events, spxDayChg, updatedAt) {{
  var list = document.getElementById('events-list');
  if (!list) return;
  window._latestMarketEvents = events;

  // Header: date
  var dateEl = document.getElementById('events-date');
  if (dateEl) {{
    var now = new Date();
    dateEl.textContent = now.toLocaleDateString('es-ES', {{weekday:'long',day:'numeric',month:'long',year:'numeric'}});
  }}

  // Header: S&P 500 badge
  if (spxDayChg) {{
    var spxEl = document.getElementById('events-spx-badge');
    if (spxEl) {{
      var isUp = spxDayChg.startsWith('+') || parseFloat(spxDayChg) > 0;
      spxEl.textContent = 'S&P 500 ' + spxDayChg;
      spxEl.className   = 'events-spx-badge ' + (isUp ? 'up' : 'dn');
      spxEl.style.display = '';
    }}
  }}

  // Header: updated time
  if (updatedAt) {{
    var updEl = document.getElementById('events-updated');
    if (updEl) updEl.textContent = 'Actualizado ' + updatedAt;
  }}

  if (!events || !events.length) {{
    list.innerHTML = '<div class="events-empty">No hay eventos disponibles.<small>Conecta el proxy con GEMINI_API_KEY para análisis en vivo.</small></div>';
    return;
  }}

  list.innerHTML = events.map(function(ev) {{
    var dir  = (ev.direction || 'neutral').toLowerCase();
    var badgeClass = dir === 'up' ? 'up' : dir === 'down' ? 'dn' : 'neutral';
    var src  = (ev.source || '').toLowerCase();
    var srcClass = src.includes('cnbc') && src.includes('yahoo') ? 'both'
                 : src.includes('cnbc') ? 'cnbc' : 'yahoo';
    var timeHtml = ev.time_et ? '<span class="event-time">' + _escHtml(ev.time_et) + '</span>' : '';
    var srcHtml  = ev.source  ? '<span class="event-src ' + srcClass + '">' + _escHtml(ev.source) + '</span>' : '';
    return (
      '<div class="event-card">' +
        '<div class="event-left">' +
          '<div class="event-topmeta">' + timeHtml + srcHtml + '</div>' +
          '<div class="event-headline">' + _escHtml(ev.headline) + '</div>' +
          '<div class="event-detail">'   + _escHtml(ev.detail)   + '</div>' +
        '</div>' +
        '<div class="event-impact">' +
          '<span class="impact-badge ' + badgeClass + '">' + _escHtml(ev.spx_impact || '—') + '</span>' +
        '</div>' +
      '</div>'
    );
  }}).join('');
}}

async function fetchMarketEvents() {{
  try {{
    var resp = await fetch(API_BASE + '/api/market-events', {{cache:'no-store'}});
    if (resp.ok) {{
      var data = await resp.json();
      _renderMarketEvents(data.events || [], data.spx_day_chg || '', data.generated_at || '');
      return;
    }}
  }} catch(e) {{}}
  // Proxy not available: render embedded events
  _renderMarketEvents(INIT_EVENTS, '', 'generado al abrir');
}}

// ── Boot ──────────────────────────────────────────────────────────────────────
renderTabs();
renderFilterPills();
renderTickers(prices);
renderHeatmap();
renderMovers();
drawChart('^GSPC', '1D');

// ── SSE: recibe noticias nuevas en push del proxy ─────────────────────────
var _sseConnected = false;
var _sseRetryTimer = null;

function _connectSSE() {{
  if (_sseConnected) return;
  try {{
    var es = new EventSource(API_BASE + '/events');

    es.addEventListener('init', function(e) {{
      try {{
        _sseConnected = true;
        console.log('[SSE] conectado al proxy');
      }} catch(err) {{}}
    }});

    es.addEventListener('liveblog_updated', function(e) {{
      try {{
        var d = JSON.parse(e.data);
        var src = (d.source || '').toLowerCase();
        if (src === 'yahoo') {{ if (document.getElementById('lb-yahoo-panel').classList.contains('visible')) fetchYahooLbCards(); }}
        else if (src === 'cnbc') {{ if (document.getElementById('lb-cnbc-panel').classList.contains('visible')) fetchCnbcLbCards(); }}
      }} catch(err) {{}}
    }});

    es.onerror = function() {{
      _sseConnected = false;
      es.close();
      // Reconectar en 30 segundos
      if (_sseRetryTimer) clearTimeout(_sseRetryTimer);
      _sseRetryTimer = setTimeout(_connectSSE, 30000);
    }};
  }} catch(e) {{
    // EventSource no disponible o proxy no corriendo — caer a polling
  }}
}}

// SSE para recibir push de actualizaciones del liveblog
_connectSSE();
// Cargar liveblogs en background (para Creator Studio)
fetchYahooLbCards();
fetchCnbcLbCards();
setInterval(fetchYahooLbCards, 6 * 60 * 1000);
setInterval(fetchCnbcLbCards,  6 * 60 * 1000);
// Inicializar y refrescar análisis de mercado
_renderMarketEvents(INIT_EVENTS, '', 'generado al abrir');
fetchMarketEvents();
setInterval(fetchMarketEvents, 6 * 60 * 1000);
// Main index prices: refresh now + every 60 seconds
refreshLivePrices();
setInterval(refreshLivePrices, 60 * 1000);
// Individual stock prices: refresh every 5 minutes (aligned with news)
refreshStockPrices();
setInterval(refreshStockPrices, 5 * 60 * 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = Path(__file__).parent / "market_pulse.html"

    print("\n[1/4] Fetching live prices…")
    prices = fetch_prices()

    print("\n[2/4] Fetching charts (8 tickers × 8 periods)…")
    all_charts = fetch_all_charts()

    print("\n[2.5/4] Fetching stock prices for ticker badges…")
    stock_prices = fetch_stock_prices()

    print("\n[3/4] Scraping news (Playwright)…")
    news = fetch_news()

    print("\n[3.5/4] Analyzing market events with Claude…")
    events = analyze_market_events(news, prices)

    print("\n[4/4] Building HTML…")
    html = build_html(prices, all_charts, news, CHART_TABS, stock_prices, generated_at, events)
    out.write_text(html, encoding="utf-8")
    size_kb = out.stat().st_size // 1024
    print(f"\n✓ Saved → {out.name}  ({size_kb} KB)")
    print(f"  Open: open \"{out}\"")
