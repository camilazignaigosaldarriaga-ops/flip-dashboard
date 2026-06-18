#!/usr/bin/env python3
"""
Market Pulse — FastAPI Backend
================================
GET  /                        → dashboard HTML
GET  /api/prices              → precios en tiempo real (cache 60s)
GET  /api/charts/{sym}/{per}  → datos OHLCV por ticker/período (cache 5min)
GET  /api/stocks              → heatmap de acciones (cache 60s)
GET  /api/news                → feed de noticias (cache 45s)
GET  /api/liveblog            → live blog con resúmenes IA (cache 6min)
GET  /api/market-events       → eventos que mueven el mercado (cache 15min)
GET  /api/status              → estado del servidor
GET  /events                  → Server-Sent Events
POST /api/generate-report     → Flip Reporter

Arrancar:
  uvicorn backend:app --host 0.0.0.0 --port 5678 --reload
  # o simplemente:
  python backend.py
"""

import os, json, time, re, hashlib, threading, asyncio, sys, ssl, urllib.request, urllib.parse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, AsyncGenerator
from xml.etree import ElementTree as ET

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse

# ── paths ──────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
sys.path.insert(0, str(BASE_DIR))

# ── configuración ──────────────────────────────────────────────────────────────
PORT       = int(os.environ.get("PORT", 5678))
PRICE_TTL  = 60       # precios: 1 minuto
STOCK_TTL  = 60       # stocks: 1 minuto
CHART_TTL  = 300      # charts: 5 minutos
NEWS_TTL   = 60       # noticias: 1 minuto
LB_TTL     = 360      # live blog: 6 minutos
EVENTS_TTL = 900      # eventos IA: 15 minutos
HTML_TTL   = 6 * 3600 # regenerar HTML: 6 horas

# ── Gemini key ─────────────────────────────────────────────────────────────────
def _load_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return ""

GEMINI_KEY = _load_gemini_key()

# ── Flip system prompt ─────────────────────────────────────────────────────────
FLIP_SYSTEM_PROMPT = """You are "Flip Reporter", the official AI financial analyst for Flip Inversiones.
Temperature: 0.2 | Output language: SPANISH (always, no exceptions)

MISSION: Transform raw financial market data into a daily report for the "Fliperos" community — retail investors in Latin America learning about financial markets. Make Wall Street accessible to everyone.

TONE & STYLE (NON-NEGOTIABLE):
- Write as if explaining to a curious, smart 12-year-old who loves learning
- Enthusiastic but not exaggerated. Educational but never condescending
- Every financial term must be explained in plain language the FIRST time it appears
- Never say just "Trump" — always write "Donald Trump, presidente de Estados Unidos"
- Never say just "Fed" without explaining "la Reserva Federal (Fed), el banco central de Estados Unidos que controla las tasas de interés"
- The S&P 500 must be introduced as "el S&P 500, también conocido como 'la cobra' en la comunidad Flip, es un índice que agrupa a las 500 empresas más grandes de EE.UU. y funciona como el termómetro principal del mercado americano"
- Use analogies and everyday comparisons to explain complex concepts

ZERO HALLUCINATION POLICY:
- Use ONLY facts explicitly present in the provided market data
- Do NOT invent prices, percentages, company names, or events not in the input
- If data is missing, write "no disponemos de ese dato hoy" — never fabricate
- You MAY use general background knowledge to EXPLAIN a concept, but NEVER to invent market-moving facts
- Use exactly the figures provided in the input

IMMUTABLE REPORT STRUCTURE — 3 paragraphs, max 450 words total:

PARAGRAPH 1 — Welcome & Metrics (max 4 sentences):
- Begin EXACTLY with: "Hola fliperos,"
- Then: "La cobra (S&P 500) [sube/baja] [x]% tras [main event in 5 words max]."
- Follow with 2 sentences summarizing overall market mood (include Nasdaq, Dow Jones if available)

PARAGRAPH 2 — The Main Event (max 200 words):
- Focus on the single most important event from the provided list
- Structure: WHAT happened → WHO is involved (full names/context) → WHY it matters
- Use a simple analogy to explain the impact mechanism
- End with: "En concreto, esto impacta al mercado porque..."

PARAGRAPH 3 — What's Coming (max 150 words):
- Identify ONE upcoming key event from the context
- Explain what that event IS in simple terms
- Present TWO scenarios: "Si el resultado es positivo..." / "Si el resultado es negativo..."
- Include direct quotes if present in the data, formatted as: [Name, Title] dijo: "[quote]"

MONDAY RULE: If today is Monday (lunes), replace Paragraph 2 with "La Semana Pasada en Resumen" — summarize the 3 most important events from the previous week's data, each as: "📌 [Event]: [2-sentence explanation]"

OUTPUT: Plain text, no markdown headers or bullet points. Line breaks between paragraphs. Max 3 emojis. SPANISH only."""

# ── utilidades ─────────────────────────────────────────────────────────────────
def _uid(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _word_set(text: str) -> set:
    stopwords = {"the","a","an","in","on","at","to","for","of","and","or","is","are",
                 "was","were","with","as","by","from","that","this","it","its","be","has",
                 "have","had","but","not","they","their","he","she","we","you","your","our"}
    return {w for w in re.findall(r'\b[a-zA-Z]{3,}\b', text.lower()) if w not in stopwords}


def _semantic_dedup(items: list) -> list:
    kept, word_sets = [], []
    for item in items:
        ws = _word_set(item.get("title", "") + " " + item.get("headline", ""))
        if not ws:
            kept.append(item); word_sets.append(ws); continue
        if any(prev and len(ws & prev) / min(len(ws), len(prev)) >= 0.60 for prev in word_sets):
            continue
        kept.append(item); word_sets.append(ws)
    return kept


def _post_to_dict(post) -> dict:
    ts = 0
    if post.timestamp_iso:
        try:
            ts = int(datetime.fromisoformat(post.timestamp_iso).timestamp())
        except Exception:
            pass
    body = post.body or ""
    sentences = re.split(r'(?<=[.!?])\s+', body)
    paras, bucket = [], []
    for s in sentences:
        if s.strip():
            bucket.append(s.strip())
        if len(bucket) >= 3:
            paras.append(" ".join(bucket)); bucket = []
    if bucket:
        paras.append(" ".join(bucket))
    paras = [p for p in paras if len(p) > 20]
    if not paras and body:
        paras = [body[:600]]
    return {
        "id": post.post_id, "headline": post.headline,
        "paragraphs": paras[:10], "timestamp": ts,
        "source": post.source, "url": post.article_url,
    }


# ── caché en memoria ───────────────────────────────────────────────────────────
class _Cache:
    def __init__(self, ttl: float):
        self.ttl  = ttl
        self._d   = None
        self._ts  = 0.0
        self._lk  = threading.Lock()

    def get(self):
        return self._d if (time.time() - self._ts) < self.ttl else None

    def set(self, data):
        with self._lk:
            self._d = data; self._ts = time.time()

    @property
    def age(self) -> float:
        return time.time() - self._ts


_c_prices      = _Cache(PRICE_TTL)
_c_stocks      = _Cache(STOCK_TTL)
_c_news        = _Cache(NEWS_TTL)
_c_lb_cnbc     = _Cache(LB_TTL)
_c_lb_yahoo    = _Cache(LB_TTL)
_c_events      = _Cache(EVENTS_TTL)
_c_yahoo_text  = _Cache(EVENTS_TTL)   # artículos Yahoo Finance como texto plano
_c_charts: dict[str, _Cache] = {}     # key = "sym|period"
_c_summaries: dict[str, dict] = {}    # post_id → summary

# SSE
_sse_queues: list[asyncio.Queue] = []


# ── Gemini helpers ─────────────────────────────────────────────────────────────
def _gemini_sync(contents: str, model: str = "gemini-flash-lite-latest") -> str:
    from google import genai
    client = genai.Client(api_key=GEMINI_KEY)
    resp = client.models.generate_content(model=model, contents=contents)
    return resp.text.strip()


def _ai_summary_sync(post_id: str, headline: str, paragraphs: list) -> dict:
    if post_id in _c_summaries:
        return _c_summaries[post_id]
    if not GEMINI_KEY:
        result = {"que_paso": " ".join(paragraphs[:2]), "por_que": " ".join(paragraphs[2:4]),
                  "reaccion": " ".join(paragraphs[4:]), "ai_generated": False}
        _c_summaries[post_id] = result
        return result
    prompt = f"""Eres un analista financiero senior. Analiza esta actualización del mercado y genera un resumen ejecutivo en español en formato JSON.

Titular: {headline}

Cuerpo:
{chr(10).join(paragraphs)}

Responde SOLO con este JSON (sin markdown):
{{"que_paso":"2-3 oraciones sobre qué ocurrió","por_que":"2-3 oraciones de causas/contexto","reaccion":"1-2 oraciones sobre reacción del mercado"}}"""
    try:
        raw = _gemini_sync(prompt)
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        result = {**{k: data.get(k, "") for k in ("que_paso", "por_que", "reaccion")}, "ai_generated": True}
    except Exception as e:
        print(f"  [ai_summary] {e}")
        result = {"que_paso": " ".join(paragraphs[:2]), "por_que": " ".join(paragraphs[2:4]),
                  "reaccion": " ".join(paragraphs[4:]), "ai_generated": False}
    _c_summaries[post_id] = result
    return result


def _fetch_yahoo_news_text_sync() -> str:
    """
    Fetches journalist-written market articles via yfinance news API.
    Uses articles from multiple market tickers — no live blog posts.
    """
    import yfinance as yf

    TICKERS = ["^GSPC", "^DJI", "^IXIC", "SPY", "QQQ", "GC=F", "CL=F", "^VIX",
               "^TNX", "AAPL", "MSFT", "NVDA", "TSLA"]

    blocks: list[str] = []
    seen: set[str] = set()

    for sym in TICKERS:
        try:
            news = yf.Ticker(sym).news or []
            for n in news:
                c     = n.get("content", {})
                title = c.get("title", "").strip()
                ctype = c.get("contentType", "")
                if not title or title in seen or ctype == "VIDEO":
                    continue
                seen.add(title)
                desc = re.sub(r"<[^>]+>", "", c.get("description", "")).strip()[:500]
                pub  = (c.get("provider") or {}).get("displayName", "Yahoo Finance")
                line = f"• [{pub}] {title}"
                if desc:
                    line += f"\n  {desc}"
                blocks.append(line)
        except Exception as e:
            print(f"  [yf_news:{sym}] {e}")

    print(f"  [yahoo_news] {len(seen)} artículos de periodistas (yfinance)")
    if len(seen) < 3:
        return ""

    header = (
        f"=== Yahoo Finance Market News — "
        f"{datetime.now(timezone.utc).strftime('%A, %B %d %Y')} ==="
    )
    return header + "\n\n" + "\n\n".join(blocks)


def _ai_events_from_article_sync(article_text: str) -> dict:
    """Extracts market-moving events from Yahoo Finance article text using Gemini."""
    if not GEMINI_KEY or not article_text.strip():
        return {"events": [], "generated_at": "", "spx_day_chg": ""}

    # S&P 500 change
    spx_chg = ""
    try:
        import yfinance as yf
        df = yf.Ticker("^GSPC").history(period="2d", interval="1d")
        if len(df) >= 2:
            prev, cur = float(df["Close"].iloc[-2]), float(df["Close"].iloc[-1])
            spx_chg = f"{'+' if cur >= prev else ''}{(cur - prev) / prev * 100:.2f}%"
    except Exception:
        pass

    spx_ctx   = f" S&P 500: {spx_chg}." if spx_chg else ""
    today_str = datetime.now(timezone.utc).strftime("%A, %B %d %Y")

    prompt = f"""Hoy es {today_str}.{spx_ctx}

Analiza el siguiente contenido de artículos escritos por periodistas de Yahoo Finance sobre el mercado:

{article_text[:14000]}

---

Extrae los 6-8 eventos que más han movido al S&P 500 u otros activos hoy.

CATEGORÍAS DE ALTA PRIORIDAD (incluir siempre si aparecen en el texto):
1. Resultados corporativos trimestrales (earnings, EPS, revenue, guidance de grandes empresas)
2. Publicaciones económicas (PIB, inflación/IPC/PCE, tasa de interés, empleo/nóminas)
3. Noticias sobre petróleo (precio crudo, decisiones OPEP+, inventarios, producción)
4. Reserva Federal: reuniones FOMC, decisiones de tasas, declaraciones de Powell
5. IPOs importantes (valuación superior a 1 trillón USD)

Reglas:
- El titular describe el HECHO concreto, no la reacción del mercado
- Extrae solo eventos con impacto verificable mencionado en el texto
- Ordena por impacto: primero las 5 categorías de alta prioridad, luego el resto
- Si no hay suficiente información para un campo, usa cadena vacía

Devuelve ÚNICAMENTE un array JSON válido, sin markdown ni texto extra:
[{{"headline":"max 80 chars","detail":"1-2 oraciones max 180 chars","spx_impact":"max 45 chars","direction":"up|down|neutral","time_et":"hora ET o vacío","source":"Yahoo Finance"}}]"""

    try:
        raw = _gemini_sync(
            "Eres un analista financiero senior. Responde ÚNICAMENTE con JSON válido, sin markdown.\n\n" + prompt
        )
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        events = json.loads(raw)
        if isinstance(events, list):
            print(f"  [ai_events] ✓ {len(events)} eventos extraídos de artículos Yahoo Finance")
            return {
                "events":       events,
                "generated_at": datetime.now(timezone.utc).strftime("%H:%M UTC"),
                "spx_day_chg":  spx_chg,
            }
    except Exception as e:
        print(f"  [ai_events] {e}")
    return {"events": [], "generated_at": "", "spx_day_chg": spx_chg}


# ── fetch de datos ─────────────────────────────────────────────────────────────
def _fetch_prices_sync() -> dict:
    cached = _c_prices.get()
    if cached is not None:
        return cached
    from generate_terminal import fetch_prices
    data = fetch_prices()
    _c_prices.set(data)
    return data


def _fetch_stocks_sync() -> dict:
    cached = _c_stocks.get()
    if cached is not None:
        return cached
    from generate_terminal import fetch_stock_prices
    data = fetch_stock_prices()
    _c_stocks.set(data)
    return data


def _fetch_chart_sync(symbol: str, period: str) -> Optional[dict]:
    key = f"{symbol}|{period}"
    if key not in _c_charts:
        _c_charts[key] = _Cache(CHART_TTL)
    cached = _c_charts[key].get()
    if cached is not None:
        return cached
    from generate_terminal import fetch_chart
    data = fetch_chart(symbol, period)
    if data:
        _c_charts[key].set(data)
    return data


def _fetch_cnbc_lb_sync() -> list:
    cached = _c_lb_cnbc.get()
    if cached is not None:
        return cached
    try:
        from liveblog_scraper import scrape_cnbc
        posts_raw = scrape_cnbc()
        posts = [_post_to_dict(p) for p in posts_raw]
    except Exception as e:
        print(f"  [cnbc_lb] {e}"); posts = []
    # Añadir resúmenes IA
    for p in posts[:20]:
        if p.get("paragraphs"):
            p["summary"] = _ai_summary_sync(p["id"], p["headline"], p["paragraphs"])
        else:
            p["summary"] = {"que_paso": "", "por_que": "", "reaccion": "", "ai_generated": False}
    _c_lb_cnbc.set(posts)
    print(f"  [cnbc_lb] {len(posts)} posts")
    return posts


def _fetch_yahoo_lb_sync() -> list:
    cached = _c_lb_yahoo.get()
    if cached is not None:
        return cached
    try:
        from liveblog_scraper import scrape_yahoo
        posts_raw = scrape_yahoo()
        posts = [_post_to_dict(p) for p in posts_raw]
    except Exception as e:
        print(f"  [yahoo_lb] {e}"); posts = []
    for p in posts[:20]:
        if p.get("paragraphs"):
            p["summary"] = _ai_summary_sync(p["id"], p["headline"], p["paragraphs"])
        else:
            p["summary"] = {"que_paso": "", "por_que": "", "reaccion": "", "ai_generated": False}
    _c_lb_yahoo.set(posts)
    print(f"  [yahoo_lb] {len(posts)} posts")
    return posts


def _fetch_news_sync() -> list:
    cached = _c_news.get()
    if cached is not None:
        return cached
    cnbc  = _c_lb_cnbc.get() or []
    yahoo = _c_lb_yahoo.get() or []
    items = []
    for p in cnbc + yahoo:
        summ = p.get("summary") or {}
        if summ.get("ai_generated") and summ.get("que_paso"):
            body = " ".join(filter(None, [summ.get("que_paso",""), summ.get("por_que",""), summ.get("reaccion","")]))
        else:
            body = " ".join((p.get("paragraphs") or [])[:4])
        items.append({
            "id": p.get("id") or _uid(p.get("headline", "")),
            "title": p.get("headline", ""), "body": body,
            "url": p.get("url", ""), "source": p.get("source", ""),
            "timestamp": p.get("timestamp", 0),
        })
    seen, deduped = set(), []
    for item in sorted(items, key=lambda x: x.get("timestamp", 0), reverse=True):
        k = item.get("id") or item.get("title", "")
        if k and k not in seen:
            seen.add(k); deduped.append(item)
    deduped = _semantic_dedup(deduped)
    _c_news.set(deduped[:60])
    return deduped[:60]


def _fetch_events_sync() -> dict:
    cached = _c_events.get()
    if cached is not None:
        return cached
    # Obtener texto de artículos Yahoo Finance (no live blog)
    article_text = _c_yahoo_text.get()
    if article_text is None:
        article_text = _fetch_yahoo_news_text_sync()
        _c_yahoo_text.set(article_text or "")
    result = _ai_events_from_article_sync(article_text or "")
    _c_events.set(result)
    return result


# ── SSE broadcast ──────────────────────────────────────────────────────────────
def _sse_broadcast(event_type: str, data: dict):
    payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    for q in list(_sse_queues):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.append(q)
    for q in dead:
        if q in _sse_queues:
            _sse_queues.remove(q)


# ── background refresh ─────────────────────────────────────────────────────────
def _bg_loop():
    last_lb     = 0.0
    last_events = 0.0
    while True:
        time.sleep(60)
        # Precios
        try:
            from generate_terminal import fetch_prices, fetch_stock_prices
            _c_prices.set(fetch_prices())
            _c_stocks.set(fetch_stock_prices())
            print(f"[{datetime.now().strftime('%H:%M')}] precios actualizados")
        except Exception as e:
            print(f"[bg] prices: {e}")
        # Live blogs cada 6 min
        if time.time() - last_lb >= LB_TTL:
            for fn, label in ((_fetch_cnbc_lb_sync, "cnbc"), (_fetch_yahoo_lb_sync, "yahoo")):
                try:
                    _c_lb_cnbc.set(None) if label == "cnbc" else _c_lb_yahoo.set(None)
                    fn()
                    _sse_broadcast("liveblog_updated", {"source": label})
                except Exception as e:
                    print(f"[bg] {label}: {e}")
            _c_news.set(None)  # invalida cache de noticias
            last_lb = time.time()
        # Artículos Yahoo Finance + eventos IA cada 15 min
        if time.time() - last_events >= EVENTS_TTL:
            try:
                _c_yahoo_text.set(None)  # forzar re-fetch de artículos
                _c_events.set(None)
                _fetch_events_sync()
                _sse_broadcast("events_updated", {})
            except Exception as e:
                print(f"[bg] events: {e}")
            last_events = time.time()


# ── FastAPI app ────────────────────────────────────────────────────────────────
app = FastAPI(title="Market Pulse API", version="2.0", docs_url="/docs")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)

LOADING_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="15">
<style>body{background:#0a0a0f;color:#e0e0e0;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0;flex-direction:column;gap:16px}
.spinner{width:40px;height:40px;border:3px solid #333;border-top-color:#3b82f6;border-radius:50%;animation:spin 1s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
p{color:#888;font-size:.85rem}</style></head>
<body><div class="spinner"></div>
<h2 style="margin:0">Market Pulse</h2>
<p>Generando dashboard con datos en vivo&hellip; recargando en 15s</p>
</body></html>"""


@app.on_event("startup")
async def _startup():
    loop = asyncio.get_event_loop()
    # Warm up precios en background
    loop.run_in_executor(None, _fetch_prices_sync)
    loop.run_in_executor(None, _fetch_stocks_sync)
    # Arrancar loop de refresh
    threading.Thread(target=_bg_loop, daemon=True).start()
    print(f"✓ Market Pulse API arrancando en puerto {PORT}")
    print(f"  IA: {'✓ Gemini Flash' if GEMINI_KEY else '✗ sin API key'}")


# ── rutas estáticas ────────────────────────────────────────────────────────────

@app.get("/")
async def root():
    html_path = BASE_DIR / "market_pulse.html"
    if html_path.exists():
        return FileResponse(str(html_path), media_type="text/html")
    return HTMLResponse(LOADING_HTML)


@app.get("/dashboard")
async def dashboard():
    return await root()


# ── API: precios en tiempo real ────────────────────────────────────────────────

@app.get("/api/prices")
async def api_prices():
    """Precios actuales de los índices principales (cache 60s)."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch_prices_sync)
    return data


@app.get("/api/stocks")
async def api_stocks():
    """Datos del heatmap de acciones (cache 60s)."""
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch_stocks_sync)
    return data


@app.get("/api/charts/{symbol}/{period}")
async def api_chart(symbol: str, period: str):
    """Datos OHLCV para un símbolo y período (cache 5min).
    Períodos válidos: 1D, 5D, 1M, 6M, YTD, 1Y, 5Y, MAX
    """
    valid_periods = {"1D", "5D", "1M", "6M", "YTD", "1Y", "5Y", "MAX"}
    if period not in valid_periods:
        return JSONResponse({"error": f"período inválido, usa: {', '.join(valid_periods)}"}, status_code=400)
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, _fetch_chart_sync, symbol, period)
    if not data:
        return JSONResponse({"error": "sin datos para ese símbolo/período"}, status_code=404)
    return data


# ── API: noticias ──────────────────────────────────────────────────────────────

@app.get("/api/news")
async def api_news():
    """Feed de noticias desde live blogs de CNBC y Yahoo (cache 60s)."""
    loop = asyncio.get_event_loop()
    # Asegurar que los live blogs están cargados
    cnbc  = _c_lb_cnbc.get()
    yahoo = _c_lb_yahoo.get()
    if cnbc is None:
        await loop.run_in_executor(None, _fetch_cnbc_lb_sync)
    if yahoo is None:
        await loop.run_in_executor(None, _fetch_yahoo_lb_sync)
    items = await loop.run_in_executor(None, _fetch_news_sync)
    return {"items": items, "count": len(items), "cached_at": time.time()}


@app.get("/api/liveblog")
async def api_liveblog(source: str = Query("all", description="cnbc | yahoo | all")):
    """Posts del live blog con resúmenes IA (cache 6min)."""
    loop = asyncio.get_event_loop()
    if source == "cnbc":
        posts = await loop.run_in_executor(None, _fetch_cnbc_lb_sync)
    elif source == "yahoo":
        posts = await loop.run_in_executor(None, _fetch_yahoo_lb_sync)
    else:
        cnbc  = await loop.run_in_executor(None, _fetch_cnbc_lb_sync)
        yahoo = await loop.run_in_executor(None, _fetch_yahoo_lb_sync)
        seen, posts = set(), []
        for p in sorted(cnbc + yahoo, key=lambda x: x.get("timestamp", 0), reverse=True):
            k = p.get("id") or _uid(p.get("headline", ""))
            if k not in seen:
                seen.add(k); posts.append(p)
    return {"posts": posts, "count": len(posts), "ai_enabled": bool(GEMINI_KEY)}


# ── API: eventos de mercado ────────────────────────────────────────────────────

@app.get("/api/market-events")
async def api_market_events():
    """Eventos que mueven el mercado, analizados por Gemini (cache 15min)."""
    loop = asyncio.get_event_loop()
    # Asegurar live blogs cargados
    if _c_lb_cnbc.get() is None:
        await loop.run_in_executor(None, _fetch_cnbc_lb_sync)
    if _c_lb_yahoo.get() is None:
        await loop.run_in_executor(None, _fetch_yahoo_lb_sync)
    data = await loop.run_in_executor(None, _fetch_events_sync)
    return data


# ── API: generar reporte Flip ──────────────────────────────────────────────────

@app.post("/api/generate-report")
async def api_generate_report(request: Request):
    """Genera el reporte diario Flip usando los eventos seleccionados."""
    if not GEMINI_KEY:
        return JSONResponse({"error": "GEMINI_API_KEY no configurado"}, status_code=503)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "JSON inválido"}, status_code=400)

    events      = body.get("events", [])
    prices      = body.get("prices", {})
    date_str    = body.get("date", "")
    day_of_week = body.get("day_of_week", "")

    if not events:
        return JSONResponse({"error": "No hay eventos seleccionados"}, status_code=400)

    is_monday   = "lunes" in day_of_week.lower()
    events_text = "\n".join([
        f"{i+1}. {ev.get('headline','')} — {ev.get('detail','')} "
        f"[Impacto: {ev.get('spx_impact','')}] "
        f"[Dirección: {ev.get('direction','')}] "
        f"[Hora: {ev.get('time_et','')}] "
        f"[Fuente: {ev.get('source','')}]"
        for i, ev in enumerate(events)
    ])
    user_prompt = (
        f"DATE: {date_str} ({day_of_week})"
        f"{'  ← TODAY IS MONDAY: Apply Monday Rule for Paragraph 2' if is_monday else ''}\n\n"
        f"MARKET DATA:\n"
        f"- S&P 500: {prices.get('spx_pct','N/D')} | Price: {prices.get('spx_price','N/D')}\n"
        f"- Nasdaq: {prices.get('ndx_pct','N/D')}\n"
        f"- Dow Jones: {prices.get('dj_pct','N/D')}\n"
        f"- VIX: {prices.get('vix_val','N/D')}\n\n"
        f"MARKET EVENTS IDENTIFIED TODAY (ordered by importance):\n{events_text}\n\n"
        f"Write the Flip daily report now. Remember: SPANISH only, exactly 3 paragraphs, max 450 words, start with \"Hola fliperos,\"."
    )
    try:
        loop = asyncio.get_event_loop()
        report = await loop.run_in_executor(
            None, _gemini_sync,
            FLIP_SYSTEM_PROMPT + "\n\n" + user_prompt,
        )
        return {"report": report}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── API: estado del servidor ───────────────────────────────────────────────────

@app.get("/api/status")
async def api_status():
    return {
        "status":         "ok",
        "ai_enabled":     bool(GEMINI_KEY),
        "prices_age_s":   round(_c_prices.age),
        "stocks_age_s":   round(_c_stocks.age),
        "news_age_s":     round(_c_news.age),
        "lb_cnbc_count":  len(_c_lb_cnbc.get() or []),
        "lb_yahoo_count": len(_c_lb_yahoo.get() or []),
        "events_count":   len((_c_events.get() or {}).get("events", [])),
        "sse_clients":    len(_sse_queues),
        "model":          "gemini-flash-lite-latest",
    }


# ── Server-Sent Events ─────────────────────────────────────────────────────────

@app.get("/events")
async def sse_events(request: Request):
    """Stream SSE para push de actualizaciones al dashboard."""
    q: asyncio.Queue = asyncio.Queue(maxsize=50)
    _sse_queues.append(q)

    async def generator() -> AsyncGenerator[str, None]:
        try:
            init = json.dumps({
                "news_count": len(_c_news.get() or []),
                "events_count": len((_c_events.get() or {}).get("events", [])),
            }, ensure_ascii=False)
            yield f"event: init\ndata: {init}\n\n"

            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25)
                    yield msg
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
        finally:
            if q in _sse_queues:
                _sse_queues.remove(q)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control":     "no-cache",
            "X-Accel-Buffering": "no",
            "Connection":        "keep-alive",
        },
    )


# ── entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    uvicorn.run(
        "backend:app",
        host="0.0.0.0",
        port=PORT,
        reload=False,
        log_level="info",
    )
