import re
import hashlib
import time
from datetime import datetime, date

import streamlit as st
import yfinance as yf
import plotly.graph_objects as go
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Live Market Dashboard",
    page_icon="📊",
    layout="wide",
)

# ── Defaults ──────────────────────────────────────────────────────────────────

YAHOO_DEFAULT = (
    "https://finance.yahoo.com/markets/live/"
    "stock-market-today-thursday-june-11-dow-sp-500-nasdaq-222511784.html"
)

# CNBC publishes a new live blog each market day. Try the last 7 days to find
# the most recent one that actually has posts.
@st.cache_data(ttl=3600, show_spinner=False)
def _find_latest_cnbc_url() -> str:
    import requests
    from datetime import timedelta
    fallback = "https://www.cnbc.com/2026/06/10/stock-market-today-live-updates.html"
    today = date.today()
    for delta in range(0, 8):
        d = today - timedelta(days=delta)
        url = (
            f"https://www.cnbc.com/{d.year}/{d.month:02d}/{d.day:02d}"
            "/stock-market-today-live-updates.html"
        )
        try:
            r = requests.head(url, timeout=5, allow_redirects=True,
                              headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return url
        except Exception:
            continue
    return fallback

CNBC_DEFAULT = _find_latest_cnbc_url()

INDICES = {"S&P 500": "^GSPC", "Dow Jones": "^DJI", "Nasdaq": "^IXIC"}
TIME_RE = re.compile(r'\b\d{1,2}:\d{2}\s*(?:AM|PM)\b')

# ── Scraping helpers ──────────────────────────────────────────────────────────

def _render(url: str) -> str:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        page.wait_for_timeout(5_000)
        html = page.content()
        browser.close()
    return html


@st.cache_data(ttl=120, show_spinner=False)
def fetch_yahoo(url: str) -> list[dict]:
    html = _render(url)
    soup = BeautifulSoup(html, "lxml")
    posts = soup.select(".liveblogposts-post")
    updates = []
    for post in posts[:20]:
        h = post.find(["h2", "h3", "h4"])
        title = h.get_text(strip=True) if h else ""
        if not title:
            continue
        paras = post.find_all("p")
        body = " ".join(p.get_text(strip=True) for p in paras[:2])[:320]
        raw_text = post.get_text()
        ts_match = TIME_RE.search(raw_text)
        ts = ts_match.group(0) if ts_match else ""
        updates.append({"title": title, "body": body, "time": ts})
    return updates


@st.cache_data(ttl=120, show_spinner=False)
def fetch_cnbc(url: str) -> list[dict]:
    html = _render(url)
    soup = BeautifulSoup(html, "lxml")
    posts = soup.select(".LiveBlogBody-post")
    updates = []
    for post in posts[:20]:
        subtitle = post.select_one(".LiveBlogBody-subtitle")
        title = subtitle.get_text(strip=True) if subtitle else ""
        if not title:
            continue
        ts_el = post.select_one(".LiveBlogTimestamp-time")
        ts = ts_el.get_text(strip=True) if ts_el else ""
        paras = post.find_all("p")
        body = " ".join(p.get_text(strip=True) for p in paras[:2])[:320]
        updates.append({"title": title, "body": body, "time": ts})
    return updates

# ── Stock charts ──────────────────────────────────────────────────────────────

@st.cache_data(ttl=60, show_spinner=False)
def intraday(sym: str):
    return yf.Ticker(sym).history(period="1d", interval="1m")


def _chart(name: str, sym: str) -> go.Figure:
    df = intraday(sym)
    if df.empty:
        return go.Figure()
    open_p = df["Close"].iloc[0]
    cur    = df["Close"].iloc[-1]
    pct    = (cur - open_p) / open_p * 100
    green  = pct >= 0
    clr    = "#00c853" if green else "#ff1744"
    fill   = "rgba(0,200,83,0.09)" if green else "rgba(255,23,68,0.09)"
    sign   = "+" if green else ""

    fig = go.Figure(go.Scatter(
        x=df.index, y=df["Close"],
        mode="lines",
        line=dict(color=clr, width=2),
        fill="tozeroy", fillcolor=fill,
        hovertemplate="%{y:,.2f}<extra></extra>",
    ))
    fig.update_layout(
        title=dict(
            text=(
                f"<b>{name}</b>  "
                f"<span style='color:{clr}'>{sign}{pct:.2f}%</span>  "
                f"<span style='color:#ccc'>{cur:,.2f}</span>"
            ),
            x=0, font=dict(size=14, color="#e0e0e0"),
        ),
        height=220, margin=dict(l=0, r=0, t=50, b=0),
        paper_bgcolor="#0f1117", plot_bgcolor="#0f1117",
        showlegend=False,
        xaxis=dict(showgrid=False, showticklabels=False, zeroline=False),
        yaxis=dict(
            showgrid=True, gridcolor="#1e1e2e", zeroline=False,
            tickfont=dict(color="#888"), tickformat=",.0f",
        ),
        hovermode="x unified",
    )
    return fig

# ── UI helpers ────────────────────────────────────────────────────────────────

def _md5(data) -> str:
    return hashlib.md5(str(data).encode()).hexdigest()


def _render_updates(updates: list[dict]) -> None:
    if not updates:
        st.warning("No updates extracted. The page structure may have changed, or content is behind a login.")
        return
    for u in updates:
        st.markdown(f"**{u['title']}**")
        if u["time"]:
            st.caption(u["time"])
        if u["body"]:
            st.write(u["body"] + ("…" if len(u["body"]) == 320 else ""))
        st.divider()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.header("⚙️ Settings")
    yahoo_url = st.text_area("Yahoo Finance live URL", value=YAHOO_DEFAULT, height=110)
    cnbc_url  = st.text_area("CNBC live URL", value=CNBC_DEFAULT, height=80)
    st.caption("Auto-resolved to the most recent CNBC live blog. Update manually if needed.")
    refresh = st.slider("Refresh every (seconds)", 60, 600, 120, step=30)
    st.markdown("---")
    st.caption(f"Last render: {datetime.now().strftime('%H:%M:%S')}")
    if st.button("🔄 Force refresh now"):
        st.cache_data.clear()
        st.rerun()

# ── Header ────────────────────────────────────────────────────────────────────

st.title("📊 Live Stock Market Dashboard")
st.caption(
    f"Charts refresh every 60 s · News refreshes every {refresh} s · "
    f"Toast alert on new content"
)
st.markdown("---")

# ── Charts ────────────────────────────────────────────────────────────────────

chart_cols = st.columns(3)
for col, (name, sym) in zip(chart_cols, INDICES.items()):
    with col:
        st.plotly_chart(
            _chart(name, sym),
            use_container_width=True,
            config={"displayModeBar": False},
        )

st.markdown("---")
st.subheader("📰 Live Market Updates")

# ── News columns ──────────────────────────────────────────────────────────────

y_col, c_col = st.columns(2, gap="large")

with y_col:
    st.markdown("### 🟣 Yahoo Finance")
    with st.spinner("Rendering Yahoo Finance page…"):
        yahoo = fetch_yahoo(yahoo_url)
    yh = _md5(yahoo)
    if st.session_state.get("yh") and st.session_state.yh != yh:
        st.toast("New update on Yahoo Finance!", icon="🟣")
    st.session_state.yh = yh
    st.caption(f"{len(yahoo)} posts loaded")
    _render_updates(yahoo)

with c_col:
    st.markdown("### 🔵 CNBC Markets")
    with st.spinner("Rendering CNBC page…"):
        cnbc = fetch_cnbc(cnbc_url)
    ch = _md5(cnbc)
    if st.session_state.get("ch") and st.session_state.ch != ch:
        st.toast("New update on CNBC!", icon="🔵")
    st.session_state.ch = ch
    st.caption(f"{len(cnbc)} posts loaded")
    _render_updates(cnbc)

# ── Auto-refresh ──────────────────────────────────────────────────────────────
time.sleep(refresh)
st.rerun()
