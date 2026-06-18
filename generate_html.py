"""
Generates dashboard.html from live Yahoo Finance + CNBC pages and yfinance data.

Usage:
    python3 generate_html.py               # generate once
    python3 generate_html.py --watch       # regenerate every 120 s
    python3 generate_html.py --watch --interval 60
"""

import argparse
import re
import time
from datetime import date, datetime, timedelta

import requests
import yfinance as yf
import plotly.graph_objects as go
import plotly.io as pio
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── Config ────────────────────────────────────────────────────────────────────

OUTPUT    = "dashboard.html"
YAHOO_URL = (
    "https://finance.yahoo.com/markets/live/"
    "stock-market-today-thursday-june-11-dow-sp-500-nasdaq-222511784.html"
)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIME_RE = re.compile(r"\b\d{1,2}:\d{2}\s*(?:AM|PM)\b")

# Core indices (combined % chart)
CORE_INDICES = {
    "S&P 500":     "^GSPC",
    "Dow Jones":   "^DJI",
    "Nasdaq":      "^IXIC",
    "Russell 2000":"^RUT",
}
INDEX_COLORS = {
    "S&P 500":     "#4d9de8",
    "Dow Jones":   "#a78bfa",
    "Nasdaq":      "#34d399",
    "Russell 2000":"#fbbf24",
}

# Macro indicator cards (no chart — just price + % change)
INDICATORS = {
    "VIX":     "^VIX",
    "Gold":    "GC=F",
    "WTI Oil": "CL=F",
    "Brent":   "BZ=F",
    "Bitcoin": "BTC-USD",
}

# CNBC iframe symbol → yfinance symbol
CNBC_YF_MAP = {
    ".RUT":      "^RUT",
    "%40LCO.1":  "BZ=F",
    "@LCO.1":    "BZ=F",
    "TUI-DE":    "TUI.DE",
}

# Symbols to skip from article extraction (already covered above)
SKIP_SYMS = {
    "SPCX", "SPCXX-USD", "SPCX40226-USD", "SPACEX-USD",   # private
    "^GSPC", "^DJI", "^IXIC", "^RUT",                      # in combined chart
    "^VIX", "GC=F", "CL=F", "BZ=F", "BTC-USD",             # in indicator row
    "BTC-USD", "DOGE-USD",                                   # crypto (already BTC)
}

# Pretty names for story stocks
STOCK_NAMES = {
    "RKLB": "Rocket Lab",
    "ASTS": "AST SpaceMobile",
    "SATS": "EchoStar",
    "NVDA": "Nvidia",
    "SMCI": "Super Micro",
    "AAL":  "American Airlines",
    "INTC": "Intel",
    "SPCE": "Virgin Galactic",
    "NOK":  "Nokia",
    "ORCL": "Oracle",
    "AMD":  "AMD",
    "FLY":  "Fly Leasing",
    "LUNR": "Intuitive Machines",
}

# ── CNBC URL resolver ─────────────────────────────────────────────────────────

def find_latest_cnbc_url() -> str:
    today = date.today()
    for delta in range(8):
        d   = today - timedelta(days=delta)
        url = (
            f"https://www.cnbc.com/{d.year}/{d.month:02d}/{d.day:02d}"
            "/stock-market-today-live-updates.html"
        )
        try:
            r = requests.head(url, timeout=5, headers={"User-Agent": UA},
                              allow_redirects=True)
            if r.status_code == 200:
                return url
        except Exception:
            continue
    return "https://www.cnbc.com/2026/06/11/stock-market-today-live-updates.html"

# ── Playwright renderer ───────────────────────────────────────────────────────

def _render(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx  = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(5_000)
        html = page.content()
        browser.close()
    return html

# ── Scrapers (return articles + raw html for stock extraction) ────────────────

def fetch_yahoo(url: str) -> tuple[list[dict], str]:
    html    = _render(url)
    soup    = BeautifulSoup(html, "lxml")
    updates = []
    for post in soup.select(".liveblogposts-post")[:20]:
        h     = post.find(["h2", "h3", "h4"])
        title = h.get_text(strip=True) if h else ""
        if not title:
            continue
        paras = post.find_all("p")
        body  = " ".join(p.get_text(strip=True) for p in paras[:2])[:320]
        match = TIME_RE.search(post.get_text())
        updates.append({"title": title, "body": body,
                        "time": match.group(0) if match else ""})
    return updates, html


def fetch_cnbc(url: str) -> tuple[list[dict], str]:
    html    = _render(url)
    soup    = BeautifulSoup(html, "lxml")
    updates = []
    for post in soup.select(".LiveBlogBody-post")[:20]:
        subtitle = post.select_one(".LiveBlogBody-subtitle")
        title    = subtitle.get_text(strip=True) if subtitle else ""
        if not title:
            continue
        ts_el = post.select_one(".LiveBlogTimestamp-time")
        ts    = ts_el.get_text(strip=True) if ts_el else ""
        paras = post.find_all("p")
        body  = " ".join(p.get_text(strip=True) for p in paras[:2])[:320]
        updates.append({"title": title, "body": body, "time": ts})
    return updates, html

# ── Article stock extractor ───────────────────────────────────────────────────

def extract_article_stocks(cnbc_html: str, yahoo_html: str) -> list[str]:
    """Return deduplicated yfinance symbols found in both article pages."""
    syms: list[str] = []

    # 1. CNBC embedded chart iframes
    soup = BeautifulSoup(cnbc_html, "lxml")
    for iframe in soup.find_all("iframe"):
        src = iframe.get("src", "")
        m   = re.search(r"symbol=([^&]+)", src)
        if m:
            raw = m.group(1)
            yf_sym = CNBC_YF_MAP.get(raw, raw)
            if yf_sym not in SKIP_SYMS:
                syms.append(yf_sym)

    # 2. Yahoo Finance data-symbol attributes (story tickers only)
    soup2 = BeautifulSoup(yahoo_html, "lxml")
    for el in soup2.find_all(attrs={"data-symbol": True}):
        sym = el["data-symbol"]
        if sym not in SKIP_SYMS and not sym.endswith("-USD") and "=" not in sym:
            syms.append(sym)

    # Deduplicate, preserve order
    seen, out = set(), []
    for s in syms:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out

# ── Stock data helpers ────────────────────────────────────────────────────────

def _intraday(sym: str):
    return yf.Ticker(sym).history(period="1d", interval="1m")


def _quote(sym: str) -> dict | None:
    """Return {price, pct, name} or None if unavailable."""
    try:
        df = _intraday(sym)
        if df.empty:
            return None
        open_p = df["Close"].iloc[0]
        cur    = df["Close"].iloc[-1]
        pct    = (cur - open_p) / open_p * 100
        return {"price": cur, "pct": pct}
    except Exception:
        return None

# ── Chart: combined index lines ───────────────────────────────────────────────

def make_combined_chart(index_data: dict) -> str:
    fig = go.Figure()
    for name, df in index_data.items():
        if df.empty:
            continue
        open_p  = df["Close"].iloc[0]
        pct     = (df["Close"] - open_p) / open_p * 100
        cur     = df["Close"].iloc[-1]
        cur_pct = pct.iloc[-1]
        sign    = "+" if cur_pct >= 0 else ""
        color   = INDEX_COLORS.get(name, "#888")
        fig.add_trace(go.Scatter(
            x=df.index, y=pct, mode="lines",
            name=f"{name}  {sign}{cur_pct:.2f}%  {cur:,.0f}",
            line=dict(color=color, width=2),
            hovertemplate=f"<b>{name}</b>  %{{y:+.3f}}%<extra></extra>",
        ))

    fig.update_layout(
        height=300, margin=dict(l=0, r=0, t=12, b=0),
        paper_bgcolor="#0f1117", plot_bgcolor="#0f1117",
        showlegend=True,
        legend=dict(
            orientation="h", yanchor="top", y=-0.08,
            xanchor="left", x=0,
            font=dict(color="#aaa", size=11, family="Inter, sans-serif"),
            bgcolor="rgba(0,0,0,0)", borderwidth=0,
        ),
        xaxis=dict(
            showgrid=False, zeroline=False,
            tickfont=dict(color="#666", size=10),
            rangeselector=dict(
                buttons=[
                    dict(count=1,  label="1h",  step="hour", stepmode="backward"),
                    dict(count=3,  label="3h",  step="hour", stepmode="backward"),
                    dict(step="all", label="Day"),
                ],
                bgcolor="#1a1a2e", activecolor="#2e2e50",
                bordercolor="#2e2e44", borderwidth=1,
                font=dict(color="#888", size=10, family="Inter, sans-serif"),
                x=1, xanchor="right", y=1.18,
            ),
            rangeslider=dict(visible=False),
        ),
        yaxis=dict(
            showgrid=True, gridcolor="#1e1e2e",
            zeroline=True, zerolinecolor="#2e2e44", zerolinewidth=1,
            tickfont=dict(color="#666", size=10),
            tickformat="+.2f", ticksuffix="%", side="right",
        ),
        hovermode="x unified",
        hoverlabel=dict(bgcolor="#1e1e2e", bordercolor="#2e2e44",
                        font=dict(color="#e0e0e0", size=11,
                                  family="Inter, sans-serif")),
    )
    return pio.to_html(fig, full_html=False, include_plotlyjs=False,
                       config={"displayModeBar": False})

# ── Chart: individual mini-chart for a story stock ───────────────────────────

def make_mini_chart(sym: str, df) -> str:
    name   = STOCK_NAMES.get(sym, sym)
    open_p = df["Close"].iloc[0]
    cur    = df["Close"].iloc[-1]
    pct    = (cur - open_p) / open_p * 100
    green  = pct >= 0
    clr    = "#00c853" if green else "#ff1744"
    fill   = "rgba(0,200,83,0.09)" if green else "rgba(255,23,68,0.09)"
    sign   = "+" if green else ""

    fig = go.Figure(go.Scatter(
        x=df.index, y=df["Close"], mode="lines",
        line=dict(color=clr, width=1.8),
        fill="tozeroy", fillcolor=fill,
        hovertemplate="%{y:,.2f}<extra></extra>",
    ))
    fig.update_layout(
        title=dict(
            text=(
                f"<span style='font-size:12px;font-weight:600'>{name}</span>"
                f"  <span style='color:{clr};font-size:11px'>{sign}{pct:.2f}%</span>"
                f"<br><span style='color:#888;font-size:10px'>{sym}  {cur:,.2f}</span>"
            ),
            x=0, font=dict(size=11, color="#e0e0e0"),
        ),
        height=175, margin=dict(l=0, r=0, t=54, b=0),
        paper_bgcolor="#0f1117", plot_bgcolor="#0f1117", showlegend=False,
        xaxis=dict(showgrid=False, showticklabels=False, zeroline=False),
        yaxis=dict(showgrid=True, gridcolor="#1e1e2e", zeroline=False,
                   tickfont=dict(color="#555", size=9), tickformat=",.2f"),
        hovermode="x unified",
    )
    return pio.to_html(fig, full_html=False, include_plotlyjs=False,
                       config={"displayModeBar": False})

# ── HTML builder helpers ──────────────────────────────────────────────────────

def _indicator_cards_html(quotes: dict) -> str:
    """Compact stat cards row for VIX, Gold, Oil, BTC."""
    cards = ""
    for label, q in quotes.items():
        if q is None:
            continue
        price = q["price"]
        pct   = q["pct"]
        clr   = "#00c853" if pct >= 0 else "#ff1744"
        sign  = "+" if pct >= 0 else ""
        arrow = "▲" if pct >= 0 else "▼"

        if label == "Bitcoin":
            fmt_price = f"${price:,.0f}"
        elif label in ("VIX",):
            fmt_price = f"{price:.2f}"
        else:
            fmt_price = f"${price:,.2f}"

        cards += f"""
        <div class="ind-card">
          <p class="ind-name">{label}</p>
          <p class="ind-price">{fmt_price}</p>
          <p class="ind-chg" style="color:{clr}">{arrow} {sign}{pct:.2f}%</p>
        </div>"""
    return cards


def _mini_charts_html(chart_items: list[tuple]) -> str:
    """Grid of mini charts: [(sym, html_str), ...]"""
    if not chart_items:
        return '<p style="color:#555;padding:12px 0">No story stocks found in today\'s articles.</p>'
    items = ""
    for _, chart_html in chart_items:
        items += f"""
        <div class="mini-wrap">
          {chart_html}
        </div>"""
    return items


def _cards(updates: list[dict]) -> str:
    if not updates:
        return '<p class="no-data">No updates found.</p>'
    out = ""
    for u in updates:
        time_tag = f'<p class="ts">{u["time"]}</p>' if u["time"] else ""
        body     = u["body"] + ("…" if len(u["body"]) == 320 else "")
        body_tag = f'<p class="body">{body}</p>' if body else ""
        out += f"""
        <div class="card">
          <p class="headline">{u["title"]}</p>
          {time_tag}
          {body_tag}
        </div>"""
    return out

# ── Full page builder ─────────────────────────────────────────────────────────

def build_html(yahoo, cnbc, combined_chart, ind_cards, mini_charts,
               generated_at, interval_s):
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta http-equiv="refresh" content="{interval_s}" />
  <title>Live Market Dashboard</title>

  <script>
    if (localStorage.getItem('mkt-theme') === 'light') {{
      document.documentElement.classList.add('light');
    }}
  </script>

  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />
  <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>

  <style>
    /* ── Tokens ── */
    :root {{
      --bg:          #0f1117;
      --bg-card:     #13141f;
      --bg-header:   #0d0e18;
      --border:      #1e1e2e;
      --border-card: #16161e;
      --text:        #e2e2e2;
      --text-muted:  #888;
      --text-dim:    #3f3f52;
      --headline:    #b8ccff;
      --meta:        #4a4a62;
      --code-bg:     #1a1a2e;
      --code-color:  #8888aa;
      --btn-bg:      #1e1e30;
      --btn-text:    #9090b0;
      --btn-border:  #2e2e44;
      --chart-bg:    #13141f;
      --chart-grid:  #1e1e2e;
      --chart-tick:  #666;
    }}
    :root.light {{
      --bg:          #f0f2f8;
      --bg-card:     #ffffff;
      --bg-header:   #ffffff;
      --border:      #dde0ed;
      --border-card: #eaedf5;
      --text:        #18192e;
      --text-muted:  #555570;
      --text-dim:    #b0b4c8;
      --headline:    #1a3db8;
      --meta:        #9090a8;
      --code-bg:     #e8eaf4;
      --code-color:  #555570;
      --btn-bg:      #e8eaf4;
      --btn-text:    #555570;
      --btn-border:  #d0d4e8;
      --chart-bg:    #ffffff;
      --chart-grid:  #e8eaf4;
      --chart-tick:  #aaa;
    }}

    /* ── Base ── */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      min-height: 100vh;
      transition: background 0.25s, color 0.25s;
    }}

    /* ── Header ── */
    header {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
      padding: 24px 40px 16px;
      background: var(--bg-header);
      border-bottom: 1px solid var(--border);
    }}
    .header-left h1 {{
      font-size: 1.45rem;
      font-weight: 700;
      color: var(--text);
      letter-spacing: -0.02em;
    }}
    .meta {{
      margin-top: 5px;
      font-size: 0.74rem;
      color: var(--meta);
      line-height: 1.7;
    }}
    .meta code {{
      background: var(--code-bg);
      border-radius: 4px;
      padding: 1px 6px;
      font-size: 0.72rem;
      color: var(--code-color);
    }}

    /* ── Toggle ── */
    .theme-btn {{
      flex-shrink: 0;
      margin-top: 3px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 6px 14px;
      background: var(--btn-bg);
      color: var(--btn-text);
      border: 1px solid var(--btn-border);
      border-radius: 20px;
      font-family: 'Inter', sans-serif;
      font-size: 0.77rem;
      font-weight: 500;
      cursor: pointer;
      transition: opacity 0.15s;
      white-space: nowrap;
    }}
    .theme-btn:hover {{ opacity: 0.75; }}

    /* ── Section wrapper ── */
    .section {{
      padding: 20px 40px;
      border-bottom: 1px solid var(--border);
    }}
    .section-label {{
      font-size: 0.72rem;
      font-weight: 600;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--text-dim);
      margin-bottom: 14px;
    }}

    /* ── Combined index chart ── */
    .chart-wrap {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 16px 16px 28px;
      transition: background 0.25s, border-color 0.25s;
    }}

    /* ── Indicator cards ── */
    .ind-row {{
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }}
    .ind-card {{
      flex: 1;
      min-width: 110px;
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 14px;
      transition: background 0.25s, border-color 0.25s;
    }}
    .ind-name  {{ font-size: 0.72rem; font-weight: 600; color: var(--text-dim);
                  text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: 6px; }}
    .ind-price {{ font-size: 1.1rem; font-weight: 700; color: var(--text);
                  letter-spacing: -0.02em; margin-bottom: 3px; }}
    .ind-chg   {{ font-size: 0.78rem; font-weight: 600; }}

    /* ── Mini charts grid ── */
    .mini-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
      gap: 12px;
    }}
    .mini-wrap {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px 10px 4px;
      overflow: hidden;
      transition: background 0.25s, border-color 0.25s;
    }}

    /* ── News grid ── */
    .news-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0;
      padding: 20px 40px 52px;
    }}
    .col {{ padding-right: 28px; }}
    .col.cnbc {{
      padding-right: 0;
      padding-left: 28px;
      border-left: 1px solid var(--border);
    }}
    .col-header {{
      display: flex; align-items: center; gap: 9px;
      padding-bottom: 12px; margin-bottom: 4px;
      border-bottom: 1px solid var(--border);
    }}
    .dot {{ width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }}
    .yahoo .dot {{ background: #7b61ff; }}
    .cnbc  .dot {{ background: #1e90ff; }}
    .col-header h2 {{
      font-size: 0.88rem; font-weight: 600; color: var(--text);
    }}
    .count {{
      font-size: 0.73rem; color: var(--text-dim);
      font-weight: 400; margin-left: 3px;
    }}

    /* ── News cards (text boxes) ── */
    .card {{
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-left-width: 3px;
      border-radius: 10px;
      padding: 13px 15px;
      margin-bottom: 10px;
      transition: background 0.25s, border-color 0.25s;
    }}
    .card:last-child {{ margin-bottom: 0; }}
    .yahoo .card {{ border-left-color: #7b61ff; }}
    .cnbc  .card {{ border-left-color: #1e90ff; }}
    .card .headline {{
      font-size: 0.89rem; font-weight: 600; color: var(--headline);
      line-height: 1.5; margin-bottom: 6px; letter-spacing: -0.01em;
    }}
    .card .ts {{
      font-size: 0.7rem; color: var(--text-dim); margin-bottom: 6px;
      font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em;
    }}
    .card .body {{
      font-size: 0.81rem; color: var(--text-muted); line-height: 1.65;
    }}
    .no-data {{ color: var(--text-dim); font-size: 0.83rem; padding: 10px 0; }}

    @media (max-width: 760px) {{
      header {{ padding: 16px; }}
      .section {{ padding: 16px; }}
      .news-grid {{ grid-template-columns: 1fr; padding: 16px 16px 40px; }}
      .col {{ padding-right: 0; }}
      .col.cnbc {{
        padding-left: 0; border-left: none;
        border-top: 1px solid var(--border);
        margin-top: 24px; padding-top: 16px;
      }}
    }}
  </style>
</head>
<body>

<!-- ── Header ── -->
<header>
  <div class="header-left">
    <h1>📊 Live Market Dashboard</h1>
    <p class="meta">
      Generated: <strong style="color:var(--text-muted)">{generated_at}</strong>
      &nbsp;·&nbsp; Reloads every {interval_s} s
      &nbsp;·&nbsp; <code>python3 generate_html.py --watch</code>
    </p>
  </div>
  <button class="theme-btn" id="theme-btn" onclick="toggleTheme()">
    <span id="theme-icon">☀</span>
    <span id="theme-label">Light</span>
  </button>
</header>

<!-- ── Section 1: US Markets (combined % chart) ── -->
<div class="section">
  <p class="section-label">US Markets — Today's Session</p>
  <div class="chart-wrap">
    {combined_chart}
  </div>
</div>

<!-- ── Section 2: Macro Indicators ── -->
<div class="section">
  <p class="section-label">Macro Indicators</p>
  <div class="ind-row">
    {ind_cards}
  </div>
</div>

<!-- ── Section 3: Trending in Today's Articles ── -->
<div class="section">
  <p class="section-label">Trending in Today's Articles</p>
  <div class="mini-grid">
    {mini_charts}
  </div>
</div>

<!-- ── Section 4: Live News ── -->
<div class="section-label" style="padding:20px 40px 0">Live Market Updates</div>
<div class="news-grid">
  <div class="col yahoo">
    <div class="col-header">
      <span class="dot"></span>
      <h2>Yahoo Finance <span class="count">({len(yahoo)} posts)</span></h2>
    </div>
    {_cards(yahoo)}
  </div>
  <div class="col cnbc">
    <div class="col-header">
      <span class="dot"></span>
      <h2>CNBC Markets <span class="count">({len(cnbc)} posts)</span></h2>
    </div>
    {_cards(cnbc)}
  </div>
</div>

<script>
  const DARK  = {{ bg: '#13141f', grid: '#1e1e2e', tick: '#666' }};
  const LIGHT = {{ bg: '#ffffff', grid: '#e8eaf4', tick: '#aaa' }};

  function applyChartTheme(isLight) {{
    const c = isLight ? LIGHT : DARK;
    document.querySelectorAll('.js-plotly-plot').forEach(div => {{
      Plotly.relayout(div, {{
        paper_bgcolor: c.bg, plot_bgcolor: c.bg,
        'yaxis.gridcolor': c.grid, 'yaxis.tickfont.color': c.tick,
      }});
    }});
  }}

  function setTheme(isLight) {{
    document.getElementById('theme-icon').textContent  = isLight ? '☽' : '☀';
    document.getElementById('theme-label').textContent = isLight ? 'Dark' : 'Light';
    isLight
      ? document.documentElement.classList.add('light')
      : document.documentElement.classList.remove('light');
    applyChartTheme(isLight);
    localStorage.setItem('mkt-theme', isLight ? 'light' : 'dark');
  }}

  function toggleTheme() {{
    setTheme(!document.documentElement.classList.contains('light'));
  }}

  window.addEventListener('DOMContentLoaded', () => {{
    setTheme(document.documentElement.classList.contains('light'));
  }});
</script>
</body>
</html>"""

# ── Runner ────────────────────────────────────────────────────────────────────

def generate(interval_s: int) -> None:
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stamp = f"[{now}]"

    print(f"{stamp} Resolving CNBC URL…")
    cnbc_url = find_latest_cnbc_url()
    print(f"          → {cnbc_url}")

    # Scrape articles (also get raw HTML for stock extraction)
    print(f"{stamp} Fetching Yahoo Finance…")
    yahoo, yahoo_html = fetch_yahoo(YAHOO_URL)
    print(f"          → {len(yahoo)} posts")

    print(f"{stamp} Fetching CNBC…")
    cnbc, cnbc_html = fetch_cnbc(cnbc_url)
    print(f"          → {len(cnbc)} posts")

    # Extract story stocks from articles
    print(f"{stamp} Extracting article stocks…")
    story_syms = extract_article_stocks(cnbc_html, yahoo_html)
    print(f"          → {story_syms}")

    # Fetch all stock data
    print(f"{stamp} Fetching index data…")
    index_data = {name: _intraday(sym) for name, sym in CORE_INDICES.items()}

    print(f"{stamp} Fetching indicator data…")
    ind_quotes = {label: _quote(sym) for label, sym in INDICATORS.items()}

    print(f"{stamp} Fetching story stock data…")
    mini_items = []
    for sym in story_syms:
        try:
            df = _intraday(sym)
            if not df.empty:
                mini_items.append((sym, make_mini_chart(sym, df)))
                print(f"          ✓ {sym}")
            else:
                print(f"          ✗ {sym} (no data)")
        except Exception as e:
            print(f"          ✗ {sym} ({e})")

    # Build page
    print(f"{stamp} Rendering HTML…")
    combined_chart = make_combined_chart(index_data)
    ind_cards_html = _indicator_cards_html(ind_quotes)
    mini_html      = _mini_charts_html(mini_items)

    html = build_html(
        yahoo, cnbc,
        combined_chart, ind_cards_html, mini_html,
        now, interval_s,
    )
    with open(OUTPUT, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"{stamp} Saved → {OUTPUT}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--watch", action="store_true")
    parser.add_argument("--interval", type=int, default=120)
    args = parser.parse_args()

    generate(args.interval)

    if args.watch:
        print(f"Watching — regenerating every {args.interval} s.  Ctrl+C to stop.\n")
        while True:
            time.sleep(args.interval)
            generate(args.interval)
