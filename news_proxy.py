#!/usr/bin/env python3
"""
Market Pulse — News Proxy Server  v2
======================================
Endpoints:
  GET /api/news         → feed de noticias (RSS + liveblog titulares)
  GET /api/liveblog     → tarjetas del live blog con resúmenes IA
  GET /events           → Server-Sent Events — push de nuevas noticias

Correr:
  GEMINI_API_KEY=AIza... python3 news_proxy.py   (o agregar al archivo .env)

Si no tienes API key simplemente ejecuta sin la variable de entorno;
los resúmenes serán los párrafos originales organizados.
"""
import json, time, re, hashlib, email.utils, ssl, urllib.request, urllib.parse, os, queue, threading, socket, sys
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta, timezone
from threading import Thread

# ── integración con liveblog_scraper ─────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from liveblog_scraper import scrape_cnbc as _scrape_cnbc, scrape_yahoo as _scrape_yahoo
    _SCRAPER_OK = True
    print("[proxy] liveblog_scraper cargado ✓")
except ImportError as _e:
    _SCRAPER_OK = False
    print(f"[proxy] liveblog_scraper no disponible ({_e}) — usando scraper interno")

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CTX = ssl.create_default_context()

# ── configuración ─────────────────────────────────────────────────────────────

PORT        = int(os.environ.get("PORT", 5678))
TTL         = 45          # segundos entre refreshes del feed de noticias
LB_TTL      = 360         # 6 minutos entre refreshes del live blog
HTML_TTL    = 6 * 3600    # regenerar HTML cada 6 horas
def _load_gemini_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as _f:
            for _line in _f:
                if _line.strip().startswith("GEMINI_API_KEY="):
                    return _line.strip().split("=", 1)[1].strip().strip('"').strip("'")
    return ""

GEMINI_KEY = _load_gemini_key()

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

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, application/xml, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

# ── estado compartido ─────────────────────────────────────────────────────────

_cache    = {"items": [], "ts": 0.0}        # /api/news
_lb_cache = {"posts": [], "ts": 0.0}        # /api/liveblog
_cnbc_lb_cache  = {"posts": [], "ts": 0.0}   # /api/liveblog?source=cnbc
_yahoo_lb_cache = {"posts": [], "ts": 0.0}   # /api/liveblog?source=yahoo
_summary_cache = {}                          # id → summary dict (evita re-resumir)
_seen_news_ids = set()                       # para detectar items nuevos
_sse_queues: list[queue.Queue] = []          # colas de SSE activas
_sse_lock = threading.Lock()
_market_events_cache = {"events": [], "ts": 0.0, "spx_day_chg": "", "generated_at": ""}
_events_lock = threading.Lock()


# ── utilidades ────────────────────────────────────────────────────────────────

def get(url, timeout=12):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL_CTX) as r:
        return r.read()


def parse_ts(s):
    if not s:
        return 0
    try:
        return int(email.utils.parsedate_to_datetime(str(s)).timestamp())
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(datetime.strptime(str(s)[:19], fmt.rstrip("%z")).replace(
                tzinfo=timezone.utc).timestamp())
        except Exception:
            pass
    return 0


def strip_html(s):
    return re.sub(r"<[^>]+>", "", str(s or "")).strip()


def uid(s):
    return hashlib.md5(str(s).encode()).hexdigest()


def _word_set(text):
    """Palabras significativas de un texto para comparación semántica."""
    stopwords = {"the","a","an","in","on","at","to","for","of","and","or","is","are",
                 "was","were","with","as","by","from","that","this","it","its","be","has",
                 "have","had","but","not","they","their","he","she","we","you","your","our"}
    return {w for w in re.findall(r'\b[a-zA-Z]{3,}\b', text.lower()) if w not in stopwords}


def _semantic_dedup(items):
    """
    Deduplicación semántica: descarta ítems cuyo título comparte ≥60% de palabras
    con un ítem ya visto. Evita que CNBC y Yahoo reporten lo mismo dos veces.
    """
    kept = []
    word_sets = []
    for item in items:
        ws = _word_set(item.get("title", "") + " " + item.get("headline", ""))
        if not ws:
            kept.append(item)
            word_sets.append(ws)
            continue
        duplicate = False
        for prev_ws in word_sets:
            if not prev_ws:
                continue
            overlap = len(ws & prev_ws) / min(len(ws), len(prev_ws))
            if overlap >= 0.60:
                duplicate = True
                break
        if not duplicate:
            kept.append(item)
            word_sets.append(ws)
    return kept


# ── resúmenes con Claude ──────────────────────────────────────────────────────

def _claude_summary(post_id, headline, paragraphs):
    """
    Llama a Gemini Flash para generar un resumen ejecutivo estructurado.
    Retorna dict con claves: que_paso, por_que, reaccion.
    Usa caché para no re-llamar en cada refresh.
    """
    if post_id in _summary_cache:
        return _summary_cache[post_id]

    if not GEMINI_KEY:
        # Sin API key: organizar los párrafos manualmente
        result = {
            "que_paso":  " ".join(paragraphs[:2]),
            "por_que":   " ".join(paragraphs[2:4]),
            "reaccion":  " ".join(paragraphs[4:]),
            "ai_generated": False,
        }
        _summary_cache[post_id] = result
        return result

    body_text = "\n".join(paragraphs)
    prompt = f"""Eres un analista financiero senior. Analiza esta actualización del mercado y genera un resumen ejecutivo en español en formato JSON.

Titular: {headline}

Cuerpo de la nota:
{body_text}

Responde SOLO con este JSON (sin markdown, sin texto adicional):
{{
  "que_paso": "Resumen de 2-3 oraciones: ¿Qué ocurrió exactamente?",
  "por_que": "Resumen de 2-3 oraciones: ¿Cuáles son las causas o contexto?",
  "reaccion": "Resumen de 1-2 oraciones: ¿Cómo reaccionó el mercado o qué implicaciones tiene?"
}}"""

    try:
        from google import genai
        client = genai.Client(api_key=GEMINI_KEY)
        resp = client.models.generate_content(model="gemini-flash-lite-latest", contents=prompt)
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw)
        result = {
            "que_paso":  data.get("que_paso", ""),
            "por_que":   data.get("por_que", ""),
            "reaccion":  data.get("reaccion", ""),
            "ai_generated": True,
        }
        _summary_cache[post_id] = result
        print(f"  [claude] ✓ resumen: {headline[:50]}")
        return result
    except Exception as e:
        print(f"  [claude] error: {e}")
        fallback = {
            "que_paso":  " ".join(paragraphs[:2]),
            "por_que":   " ".join(paragraphs[2:4]),
            "reaccion":  " ".join(paragraphs[4:]),
            "ai_generated": False,
        }
        _summary_cache[post_id] = fallback
        return fallback


# ── conversión LiveBlogPost → dict interno del proxy ─────────────────────────

def _lb_post_to_dict(post):
    """
    Convierte un LiveBlogPost de liveblog_scraper al formato de dict
    que espera el proxy (id, headline, paragraphs, timestamp, source, url).
    """
    # Timestamp ISO → unix int
    ts = 0
    if post.timestamp_iso:
        try:
            ts = int(datetime.fromisoformat(post.timestamp_iso).timestamp())
        except Exception:
            pass

    # Body plano → lista de párrafos para Claude
    body = post.body or ""
    # Dividir en oraciones/frases (punto + espacio) para dar chunks a Claude
    sentences = re.split(r'(?<=[.!?])\s+', body)
    # Agrupar en párrafos de ~2-3 oraciones
    paras, bucket = [], []
    for s in sentences:
        if s.strip():
            bucket.append(s.strip())
        if len(bucket) >= 3:
            paras.append(" ".join(bucket))
            bucket = []
    if bucket:
        paras.append(" ".join(bucket))
    paras = [p for p in paras if len(p) > 20]
    if not paras and body:
        paras = [body[:600]]

    return {
        "id":         post.post_id,
        "headline":   post.headline,
        "paragraphs": paras[:10],
        "timestamp":  ts,
        "source":     post.source,
        "url":        post.article_url,
    }


# ── extracción de contenido ───────────────────────────────────────────────────

def _extract_paragraphs(html_or_text):
    text = re.sub(r'<script[^>]*>.*?</script>', '', str(html_or_text), flags=re.DOTALL | re.I)
    text = re.sub(r'<style[^>]*>.*?</style>',  '', text, flags=re.DOTALL | re.I)
    paras = re.findall(r'<p[^>]*>(.*?)</p>', text, re.DOTALL | re.I)
    if paras:
        return [strip_html(p).strip() for p in paras if len(strip_html(p).strip()) > 15]
    clean = strip_html(text)
    return [l.strip() for l in re.split(r'\n+', clean) if len(l.strip()) > 20]


def _find_post_arrays(obj, depth=0):
    """Búsqueda recursiva en __NEXT_DATA__: detecta arrays de posts con headline+fecha."""
    if depth > 12:
        return []
    results = []
    if isinstance(obj, list) and len(obj) >= 2:
        first = next((x for x in obj if isinstance(x, dict)), None)
        if first and (
            any(k in first for k in ('headline', 'title', 'name')) and
            any(k in first for k in ('datePublished', 'publishedAt', 'createdAt', 'date',
                                      'created_at', 'updated_at', 'timestamp'))
        ):
            return [obj]
    if isinstance(obj, dict):
        for v in obj.values():
            results.extend(_find_post_arrays(v, depth + 1))
    return results


def _parse_next_data(html, fallback_url, source_label):
    """
    Extrae posts del live blog desde __NEXT_DATA__ (JSON de Next.js SSR).
    Estrategia: búsqueda recursiva → no se rompe cuando cambia la estructura.
    """
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        nd_tag = soup.find("script", {"id": "__NEXT_DATA__"})
    except Exception:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        nd_tag = type('obj', (object,), {"string": m.group(1) if m else None})()

    raw_json = getattr(nd_tag, 'string', None) if nd_tag else None
    if not raw_json:
        return []

    try:
        nd = json.loads(raw_json)
    except Exception:
        return []

    arrays = _find_post_arrays(nd)
    if not arrays:
        return []

    post_list = max(arrays, key=len)
    posts = []
    for raw in post_list[:30]:
        headline = strip_html(
            raw.get("headline") or raw.get("title") or raw.get("name") or ""
        ).strip()
        if not headline or len(headline) < 10:
            continue

        body_raw = (
            raw.get("body") or raw.get("description") or
            raw.get("content") or raw.get("bodyText") or
            raw.get("summary") or ""
        )
        paras = _extract_paragraphs(str(body_raw))

        ts_s = (
            raw.get("datePublished") or raw.get("publishedAt") or
            raw.get("createdAt") or raw.get("date") or
            raw.get("created_at") or raw.get("updated_at") or ""
        )
        ts       = parse_ts(ts_s)
        post_url = raw.get("url") or raw.get("canonicalUrl") or fallback_url
        _id      = str(raw.get("id") or raw.get("_id") or uid(headline + str(ts_s)))

        posts.append({
            "id":         _id,
            "headline":   headline,
            "paragraphs": paras[:10],
            "timestamp":  ts,
            "source":     source_label,
            "url":        post_url,
        })

    return posts


def _css_fallback(html, url, source_label):
    """Selectores CSS como último recurso si __NEXT_DATA__ no tiene posts."""
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    is_cnbc = "cnbc" in url

    if is_cnbc:
        containers = (
            soup.select(".LiveBlogBody-post") or
            soup.select("[class*='LiveBlogBody'] [class*='post']") or
            soup.select("article[class*='LiveBlog']") or
            soup.select("[class*='live-blog'] article")
        )
        title_sel = ".LiveBlogBody-subtitle, [class*='subtitle'], h2, h3, h4"
        time_sel  = "[class*='Timestamp'], [class*='time'], time"
    else:
        containers = (
            soup.select(".liveblogposts-post") or
            soup.select("[class*='liveblog'] li") or
            soup.select("li.js-stream-content") or
            soup.select("[data-module='LiveBlogPost']")
        )
        title_sel = "h2, h3, h4"
        time_sel  = "time, [class*='time'], [class*='Timestamp']"

    posts = []
    for c in containers[:20]:
        h = c.select_one(title_sel)
        headline = h.get_text(strip=True) if h else ""
        if not headline or len(headline) < 10:
            continue
        paras    = [p.get_text(strip=True) for p in c.find_all("p") if len(p.get_text(strip=True)) > 15]
        ts_el    = c.select_one(time_sel)
        ts_str   = ts_el.get("datetime", ts_el.get_text(strip=True)) if ts_el else ""
        a_el     = c.find("a", href=True)
        post_url = a_el["href"] if a_el and str(a_el["href"]).startswith("http") else url
        posts.append({
            "id":         uid(headline),
            "headline":   headline,
            "paragraphs": paras[:10],
            "timestamp":  parse_ts(ts_str),
            "source":     source_label,
            "url":        post_url,
        })
    return posts


# ── scraping de live blogs ────────────────────────────────────────────────────

def scrape_liveblog(url, source_label):
    """Raspa un URL de live blog. Prioriza __NEXT_DATA__; fallback a CSS."""
    try:
        bust = ("&" if "?" in url else "?") + "t=" + str(int(time.time()))
        html = get(url + bust, timeout=15).decode("utf-8", errors="ignore")
    except Exception as e:
        print(f"  [liveblog fetch] {source_label}: {e}")
        return []

    posts = _parse_next_data(html, url, source_label)
    if not posts:
        print(f"  [liveblog] {source_label}: __NEXT_DATA__ vacío, intentando CSS…")
        posts = _css_fallback(html, url, source_label)

    print(f"  [liveblog] {source_label}: {len(posts)} posts")
    return posts


def _apply_summaries(posts):
    """Agrega resúmenes IA (o relleno) a cada post in-place. Reutilizable."""
    empty_summary = {"que_paso": "", "por_que": "", "reaccion": "", "ai_generated": False}
    for p in posts[:15]:
        if p.get("paragraphs"):
            p["summary"] = _claude_summary(p["id"], p["headline"], p["paragraphs"])
        else:
            p["summary"] = empty_summary
    for p in posts[15:]:
        p.setdefault("summary", empty_summary)


def fetch_cnbc_lb_posts():
    """
    Extrae el live blog de CNBC usando liveblog_scraper (con fallback al scraper interno).
    Devuelve lista de dicts con id, headline, paragraphs, timestamp, source, url, summary.
    """
    if _SCRAPER_OK:
        try:
            raw = _scrape_cnbc()
            posts = [_lb_post_to_dict(p) for p in raw]
            posts = _semantic_dedup(posts)
            _apply_summaries(posts)
            print(f"  [CNBC lb] {len(posts)} posts via liveblog_scraper")
            return posts[:20]
        except Exception as e:
            print(f"  [CNBC lb] scraper falló ({e}), usando scraper interno…")

    # Fallback: scraper interno (urllib + BeautifulSoup)
    today = datetime.now(timezone.utc)
    for delta in range(3):
        d   = today - timedelta(days=delta)
        url = (f"https://www.cnbc.com/{d.year}/{d.month:02d}/{d.day:02d}"
               f"/stock-market-today-live-updates.html")
        posts = scrape_liveblog(url, "CNBC")
        if posts:
            posts = _semantic_dedup(posts)
            _apply_summaries(posts)
            return posts[:20]
    return []


def fetch_yahoo_lb_posts():
    """
    Extrae el live blog de Yahoo Finance usando liveblog_scraper (con fallback al scraper interno).
    Yahoo Finance es CSR-only → siempre requiere Playwright.
    Devuelve lista de dicts con id, headline, paragraphs, timestamp, source, url, summary.
    """
    if _SCRAPER_OK:
        try:
            raw = _scrape_yahoo()
            posts = [_lb_post_to_dict(p) for p in raw]
            posts = _semantic_dedup(posts)
            _apply_summaries(posts)
            print(f"  [Yahoo lb] {len(posts)} posts via liveblog_scraper")
            return posts[:20]
        except Exception as e:
            print(f"  [Yahoo lb] scraper falló ({e}), usando scraper interno…")

    # Fallback: scraper interno con Playwright
    try:
        from playwright.sync_api import sync_playwright
        from bs4 import BeautifulSoup
    except ImportError as e:
        print(f"  [yahoo lb] dependencia faltante: {e}")
        return []

    YAHOO_LIVE_HUB = "https://finance.yahoo.com/markets/live/"
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx     = browser.new_context(user_agent=UA)
            page    = ctx.new_page()
            page.goto(YAHOO_LIVE_HUB, wait_until="domcontentloaded", timeout=35_000)
            page.wait_for_timeout(4_000)
            all_links = page.evaluate("""
                () => {
                    const seen = new Set(), results = [];
                    document.querySelectorAll('a[href]').forEach(a => {
                        const h = a.href;
                        if ((h.includes('/markets/live/stock-market') ||
                             /finance\\.yahoo\\.com.*stock-market-today/.test(h))
                            && !seen.has(h)) { seen.add(h); results.push(h); }
                    });
                    return results;
                }
            """)
            if not all_links:
                browser.close()
                return []
            article_url = all_links[0]
            page.goto(article_url, wait_until="domcontentloaded", timeout=40_000)
            page.wait_for_timeout(5_000)
            soup = BeautifulSoup(page.content(), "lxml")
            browser.close()

        posts = _parse_next_data(str(soup), article_url, "Yahoo") or []
        if not posts:
            posts = _css_fallback(str(soup), article_url, "Yahoo")
        posts = _semantic_dedup(posts)
        _apply_summaries(posts)
        return posts[:20]
    except Exception as e:
        print(f"  [yahoo lb fallback] {e}")
        return []


def fetch_all_liveblog_posts():
    """Raspa CNBC y Yahoo Finance live blogs, genera resúmenes IA, deduplicación."""
    all_posts = []
    today = datetime.now(timezone.utc)

    # ── CNBC: intenta hoy, ayer, anteayer ─────────────────────────────────────
    for delta in range(3):
        d   = today - timedelta(days=delta)
        url = (f"https://www.cnbc.com/{d.year}/{d.month:02d}/{d.day:02d}"
               f"/stock-market-today-live-updates.html")
        result = scrape_liveblog(url, "CNBC")
        if result:
            all_posts.extend(result)
            break

    # ── Yahoo Finance: descubre URL activa ────────────────────────────────────
    try:
        hub_html = get("https://finance.yahoo.com/markets/live/", timeout=12).decode("utf-8", errors="ignore")
        pat = re.compile(
            r'href=["\']?(https://finance\.yahoo\.com/markets/live/stock-market-today[^"\' #?]+)', re.I
        )
        candidates = list(set(pat.findall(hub_html)))
        if candidates:
            def _score(u):
                nums = re.findall(r'\d+', u)
                return int(''.join(nums[-5:])) if nums else 0
            yahoo_url = sorted(candidates, key=_score, reverse=True)[0]
            all_posts.extend(scrape_liveblog(yahoo_url, "Yahoo"))
    except Exception as e:
        print(f"  [yahoo hub] {e}")

    # ── Deduplicar y ordenar ──────────────────────────────────────────────────
    seen, deduped = set(), []
    for p in sorted(all_posts, key=lambda x: x.get("timestamp", 0), reverse=True):
        k = p.get("id") or uid(p.get("headline", ""))
        if k and k not in seen:
            seen.add(k)
            deduped.append(p)

    # Dedup semántico cross-fuente
    deduped = _semantic_dedup(deduped)

    # ── Generar resúmenes IA ──────────────────────────────────────────────────
    for post in deduped[:20]:
        if post.get("paragraphs"):
            post["summary"] = _claude_summary(
                post["id"], post["headline"], post["paragraphs"]
            )
        else:
            post["summary"] = {
                "que_paso": "", "por_que": "", "reaccion": "", "ai_generated": False
            }

    return deduped[:25]


# ── fuentes de noticias (feed) ────────────────────────────────────────────────

def fetch_yahoo_rss():
    url = (
        "https://feeds.finance.yahoo.com/rss/2.0/headline"
        "?s=%5EGSPC,%5EDJI,%5EIXIC,SPY&region=US&lang=en-US&t=" + str(int(time.time()))
    )
    try:
        xml  = get(url)
        root = ET.fromstring(xml)
        items = []
        for item in root.findall(".//item")[:25]:
            title = strip_html(item.findtext("title") or "")
            link  = (item.findtext("link") or "").strip()
            desc  = strip_html(item.findtext("description") or "")[:300]
            ts    = parse_ts((item.findtext("pubDate") or "").strip())
            if title:
                items.append({
                    "id": link or uid(title), "title": title,
                    "body": desc, "url": link,
                    "source": "Yahoo Finance", "timestamp": ts,
                })
        return items
    except Exception as e:
        print(f"  [yahoo rss] {e}")
        return []


def fetch_yahoo_search_news():
    url = (
        "https://query2.finance.yahoo.com/v1/finance/search"
        "?q=stock+market+today&newsCount=20&type=news&lang=en-US"
    )
    try:
        data = json.loads(get(url))
        return [{
            "id":        n.get("uuid") or uid(n.get("title", "")),
            "title":     (n.get("title") or "").strip(),
            "body":      "",
            "url":       n.get("link", ""),
            "source":    n.get("publisher") or "Yahoo Finance",
            "timestamp": int(n.get("providerPublishTime", 0)),
        } for n in data.get("news", [])]
    except Exception as e:
        print(f"  [yahoo search] {e}")
        return []


def fetch_cnbc_rss(feed_url):
    try:
        xml  = get(feed_url + "?t=" + str(int(time.time())))
        root = ET.fromstring(xml)
        items = []
        for item in root.findall(".//item")[:25]:
            title = strip_html(item.findtext("title") or "")
            link  = (item.findtext("link") or "").strip()
            desc  = strip_html(item.findtext("description") or "")[:300]
            ts    = parse_ts((item.findtext("pubDate") or "").strip())
            if title:
                items.append({
                    "id": link or uid(title), "title": title,
                    "body": desc, "url": link,
                    "source": "CNBC", "timestamp": ts,
                })
        return items
    except Exception as e:
        print(f"  [cnbc rss] {e}")
        return []


# ── SSE broadcast ─────────────────────────────────────────────────────────────

def _sse_broadcast(event_type, data):
    """Envía un evento SSE a todos los clientes conectados."""
    payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
    dead = []
    with _sse_lock:
        for q in _sse_queues:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _sse_queues.remove(q)


# ── cache refresh ─────────────────────────────────────────────────────────────

def _lb_posts_to_news_items(posts):
    """
    Convierte posts del live blog (formato interno) al formato de /api/news.
    Usa el resumen IA como 'body' cuando está disponible; si no, los primeros párrafos.
    """
    items = []
    for p in posts:
        summ  = p.get("summary") or {}
        # Cuerpo del item: preferir resumen IA, luego párrafos originales
        if summ.get("ai_generated") and summ.get("que_paso"):
            body = " ".join(filter(None, [
                summ.get("que_paso", ""),
                summ.get("por_que", ""),
                summ.get("reaccion", ""),
            ]))
        else:
            paras = p.get("paragraphs") or []
            body  = " ".join(paras[:4])
        items.append({
            "id":        p.get("id") or uid(p.get("headline", "")),
            "title":     p.get("headline", ""),
            "body":      body,
            "url":       p.get("url", ""),
            "source":    p.get("source", ""),
            "timestamp": p.get("timestamp", 0),
        })
    return items


def refresh():
    """
    Construye /api/news exclusivamente desde los live blogs de
    Yahoo Finance y CNBC ('Stock market today').
    Si los caches de liveblog están vacíos los rellena primero.
    """
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Actualizando /api/news desde live blogs…")

    # Asegurar que los caches de liveblog tienen datos
    if not _cnbc_lb_cache["posts"]:
        try:
            refresh_cnbc_lb()
        except Exception as e:
            print(f"  [refresh] error CNBC lb: {e}")
    if not _yahoo_lb_cache["posts"]:
        try:
            refresh_yahoo_lb()
        except Exception as e:
            print(f"  [refresh] error Yahoo lb: {e}")

    all_items = (
        _lb_posts_to_news_items(_cnbc_lb_cache["posts"]) +
        _lb_posts_to_news_items(_yahoo_lb_cache["posts"])
    )

    # Deduplicar por ID exacto, luego semántico, ordenar por timestamp
    seen, deduped = set(), []
    for item in sorted(all_items, key=lambda x: x.get("timestamp", 0), reverse=True):
        k = item.get("id") or item.get("title", "")
        if k and k not in seen:
            seen.add(k)
            deduped.append(item)

    deduped = _semantic_dedup(deduped)

    # Detectar items genuinamente nuevos para SSE push
    new_items = [i for i in deduped if i.get("id") not in _seen_news_ids]
    if new_items:
        _seen_news_ids.update(i["id"] for i in new_items if i.get("id"))
        _sse_broadcast("new_items", {"items": new_items[:10]})
        print(f"  → {len(new_items)} nuevas (SSE broadcast)")

    _cache["items"] = deduped[:60]
    _cache["ts"]    = time.time()
    total_cnbc  = len(_cnbc_lb_cache["posts"])
    total_yahoo = len(_yahoo_lb_cache["posts"])
    print(f"  → {len(deduped)} posts únicos (CNBC: {total_cnbc} · Yahoo: {total_yahoo})")


def refresh_cnbc_lb():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Actualizando live blog CNBC…")
    posts = fetch_cnbc_lb_posts()
    _cnbc_lb_cache["posts"] = posts
    _cnbc_lb_cache["ts"]    = time.time()
    print(f"  → CNBC: {len(posts)} posts")
    if posts:
        _sse_broadcast("liveblog_updated", {"source": "cnbc", "count": len(posts)})


def refresh_yahoo_lb():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Actualizando live blog Yahoo…")
    posts = fetch_yahoo_lb_posts()
    _yahoo_lb_cache["posts"] = posts
    _yahoo_lb_cache["ts"]    = time.time()
    print(f"  → Yahoo: {len(posts)} posts")
    if posts:
        _sse_broadcast("liveblog_updated", {"source": "yahoo", "count": len(posts)})


def refresh_liveblog():
    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] Actualizando live blog…")
    posts = fetch_all_liveblog_posts()
    _lb_cache["posts"] = posts
    _lb_cache["ts"]    = time.time()
    if posts:
        print(f"  → {len(posts)} posts | top: {posts[0]['headline'][:60]}")
        ai_count = sum(1 for p in posts if p.get("summary", {}).get("ai_generated"))
        print(f"  → {ai_count}/{len(posts)} resúmenes generados con IA")
        _sse_broadcast("liveblog_updated", {"count": len(posts)})
    else:
        print("  → sin posts")


def _analyze_market_events():
    """Llama a Gemini para analizar posts del live blog y extraer eventos que mueven mercado."""
    if not GEMINI_KEY:
        return
    try:
        from google import genai as _genai
    except ImportError:
        return

    cnbc_posts  = _cnbc_lb_cache.get("posts", [])
    yahoo_posts = _yahoo_lb_cache.get("posts", [])
    if not cnbc_posts and not yahoo_posts:
        return

    # Obtener cambio diario del S&P 500
    spx_chg = ""
    try:
        import yfinance as yf
        spx = yf.Ticker("^GSPC").fast_info
        prev_close = spx.previous_close or 0
        last_price = spx.last_price or 0
        if prev_close and last_price:
            pct = (last_price - prev_close) / prev_close * 100
            spx_chg = f"{'+' if pct >= 0 else ''}{pct:.2f}%"
    except Exception:
        pass

    # Construir texto del blog (CNBC primero)
    blocks = []
    if cnbc_posts:
        blocks.append("=== CNBC (prioridad) ===")
        for p in cnbc_posts[:20]:
            ts = p.get("timestamp", 0)
            t  = datetime.utcfromtimestamp(ts).strftime("%H:%M UTC") if ts else ""
            body = " ".join((p.get("paragraphs") or [])[:4])
            blocks.append(f"\n[{t}] {p.get('headline','')}\n{body[:500]}")
    if yahoo_posts:
        blocks.append("\n=== Yahoo Finance ===")
        for p in yahoo_posts[:15]:
            ts = p.get("timestamp", 0)
            t  = datetime.utcfromtimestamp(ts).strftime("%H:%M UTC") if ts else ""
            body = " ".join((p.get("paragraphs") or [])[:3])
            blocks.append(f"\n[{t}] {p.get('headline','')}\n{body[:350]}")

    blog_text = "\n".join(blocks)[:13000]
    today_str = datetime.now(timezone.utc).strftime("%A, %B %d %Y")
    spx_ctx   = f" S&P 500 acumulado del día: {spx_chg}." if spx_chg else ""

    prompt = f"""Hoy es {today_str}.{spx_ctx}

Analiza el siguiente contenido editorial de los live blogs "Stock Market Today" de CNBC y Yahoo Finance.
Solo considera el contenido escrito por el equipo editorial del medio. Ignora citas de analistas externos.

{blog_text}

---

Extrae los 6-8 eventos que más han movido al S&P 500 u otros activos hoy.

CATEGORÍAS DE ALTA PRIORIDAD — si aparecen en el texto, SIEMPRE inclúyelas primero:
1. Resultados corporativos trimestrales: earnings, EPS, revenue, guidance de grandes empresas públicas; beats o misses relevantes
2. Publicaciones económicas: PIB, inflación/IPC/PCE, tasa de interés, reporte de empleo/nóminas no agrícolas, desempleo
3. Noticias sobre petróleo: precio del crudo, decisiones de la OPEP+, inventarios de petróleo, producción
4. Reserva Federal (Fed): reuniones del FOMC, decisiones de tasas, declaraciones de Powell u otros miembros, minutas
5. IPOs importantes: salidas a bolsa de empresas con valuación superior a 1 trillón USD

Reglas generales:
- El titular describe el HECHO concreto, no la reacción
- spx_impact: extrae del texto la cifra de cambio; si no hay cifra, describe el efecto brevemente
- Solo eventos con impacto verificable en el texto
- Ordena por impacto: primero las categorías de alta prioridad, luego el resto

Devuelve ÚNICAMENTE un array JSON sin markdown:
[{{"headline":"max 80 chars","detail":"1-2 oraciones, max 180 chars","spx_impact":"max 45 chars","direction":"up|down|neutral","time_et":"hora ET o vacío","source":"CNBC|Yahoo Finance|Ambos"}}]"""

    try:
        client = _genai.Client(api_key=GEMINI_KEY)
        resp   = client.models.generate_content(
            model="gemini-flash-lite-latest",
            contents="Eres un analista financiero senior. Responde ÚNICAMENTE con JSON válido.\n\n" + prompt,
        )
        raw    = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        events = json.loads(raw)
        if isinstance(events, list):
            now_str = datetime.now(timezone.utc).strftime("%H:%M UTC")
            with _events_lock:
                _market_events_cache.update({
                    "events":      events,
                    "ts":          time.time(),
                    "spx_day_chg": spx_chg,
                    "generated_at": now_str,
                })
            print(f"  [events] ✓ {len(events)} eventos | S&P {spx_chg}")
    except Exception as e:
        print(f"  [events] error análisis: {e}")


def _regenerate_html():
    """Regenera market_pulse.html llamando a generate_terminal.py como subproceso."""
    import subprocess
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "generate_terminal.py")
    python = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv", "bin", "python3")
    if not os.path.exists(python):
        python = sys.executable
    print("[html] Regenerando market_pulse.html...")
    try:
        subprocess.run([python, script], timeout=300, check=True,
                       env={**os.environ, "GEMINI_API_KEY": GEMINI_KEY})
        print("[html] ✓ market_pulse.html regenerado")
    except Exception as e:
        print(f"[html] error regenerando HTML: {e}")


def _bg_loop():
    last_lb     = 0.0
    last_events = 0.0
    last_html   = 0.0
    while True:
        time.sleep(TTL)
        try:
            refresh()
        except Exception as e:
            print(f"[bg] error refresh: {e}")
        if time.time() - last_lb >= LB_TTL:
            for fn in (refresh_liveblog, refresh_cnbc_lb, refresh_yahoo_lb):
                try:
                    fn()
                except Exception as e:
                    print(f"[bg] error {fn.__name__}: {e}")
            last_lb = time.time()
        if time.time() - last_events >= LB_TTL:
            try:
                _analyze_market_events()
            except Exception as e:
                print(f"[bg] error events: {e}")
            last_events = time.time()
        if time.time() - last_html >= HTML_TTL:
            try:
                _regenerate_html()
            except Exception as e:
                print(f"[bg] error html: {e}")
            last_html = time.time()


# ── HTTP handler ──────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def _send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type",  "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]

        # ── serve dashboard HTML ───────────────────────────────────────────────
        if path in ("/", "/dashboard", "/index.html"):
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_pulse.html")
            if os.path.exists(html_path):
                with open(html_path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                body = b"<html><body><h2>Dashboard generandose, vuelve en 2 minutos...</h2></body></html>"
                self.send_response(503)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Retry-After", "60")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            return

        # ── /api/liveblog ──────────────────────────────────────────────────────
        if path == "/api/liveblog":
            params = urllib.parse.parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            source = (params.get("source", [""])[0] or "").lower()

            if source == "cnbc":
                if time.time() - _cnbc_lb_cache["ts"] > LB_TTL:
                    refresh_cnbc_lb()
                self._send_json({
                    "posts": _cnbc_lb_cache["posts"],
                    "cached_at": _cnbc_lb_cache["ts"],
                    "count": len(_cnbc_lb_cache["posts"]),
                    "ai_enabled": bool(GEMINI_KEY),
                })
            elif source == "yahoo":
                if time.time() - _yahoo_lb_cache["ts"] > LB_TTL:
                    refresh_yahoo_lb()
                self._send_json({
                    "posts": _yahoo_lb_cache["posts"],
                    "cached_at": _yahoo_lb_cache["ts"],
                    "count": len(_yahoo_lb_cache["posts"]),
                    "ai_enabled": bool(GEMINI_KEY),
                })
            else:
                # Combined (backward compat)
                if time.time() - _lb_cache["ts"] > LB_TTL:
                    refresh_liveblog()
                self._send_json({
                    "posts": _lb_cache["posts"],
                    "cached_at": _lb_cache["ts"],
                    "count": len(_lb_cache["posts"]),
                    "ai_enabled": bool(GEMINI_KEY),
                })
            return

        # ── /api/news ──────────────────────────────────────────────────────────
        if path == "/api/news":
            if time.time() - _cache["ts"] > TTL:
                refresh()
            self._send_json({
                "items":     _cache["items"],
                "cached_at": _cache["ts"],
                "count":     len(_cache["items"]),
            })
            return

        # ── /events  (Server-Sent Events) ─────────────────────────────────────
        if path == "/events":
            # Desactivar Nagle para que cada write llegue inmediatamente al cliente
            try:
                self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except Exception:
                pass
            self.send_response(200)
            self.send_header("Content-Type",       "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control",      "no-cache")
            self.send_header("Connection",         "keep-alive")
            self.send_header("X-Accel-Buffering",  "no")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

            q = queue.Queue(maxsize=50)
            with _sse_lock:
                _sse_queues.append(q)

            # Enviar estado inicial
            try:
                init = json.dumps({
                    "items": _cache["items"][:20],
                    "lb_posts": len(_lb_cache["posts"]),
                }, ensure_ascii=False)
                self.wfile.write(f"event: init\ndata: {init}\n\n".encode())
                self.wfile.flush()
            except Exception:
                pass

            try:
                while True:
                    try:
                        msg = q.get(timeout=25)
                        self.wfile.write(msg.encode())
                        self.wfile.flush()
                    except queue.Empty:
                        # heartbeat para mantener la conexión viva
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
            except Exception:
                pass
            finally:
                with _sse_lock:
                    if q in _sse_queues:
                        _sse_queues.remove(q)
            return

        # ── /api/market-events ─────────────────────────────────────────────────
        if path == "/api/market-events":
            with _events_lock:
                data = dict(_market_events_cache)
            self._send_json(data)
            return

        # ── /api/status ────────────────────────────────────────────────────────
        if path == "/api/status":
            self._send_json({
                "news_count":  len(_cache["items"]),
                "lb_count":    len(_lb_cache["posts"]),
                "ai_enabled":  bool(GEMINI_KEY),
                "sse_clients": len(_sse_queues),
                "news_age_s":  round(time.time() - _cache["ts"]),
                "lb_age_s":    round(time.time() - _lb_cache["ts"]),
            })
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/generate-report":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = json.loads(self.rfile.read(length) or b"{}")
                events     = body.get("events", [])
                prices     = body.get("prices", {})
                date_str   = body.get("date", "")
                day_of_week = body.get("day_of_week", "")

                if not GEMINI_KEY:
                    self._send_json({"error": "GEMINI_API_KEY no configurado"}, 503)
                    return
                if not events:
                    self._send_json({"error": "No hay eventos seleccionados"}, 400)
                    return

                # Build user prompt
                is_monday = "lunes" in day_of_week.lower()
                events_text = "\n".join([
                    f"{i+1}. {ev.get('headline','')} — {ev.get('detail','')} "
                    f"[Impacto: {ev.get('spx_impact','')}] "
                    f"[Dirección: {ev.get('direction','')}] "
                    f"[Hora: {ev.get('time_et','')}] "
                    f"[Fuente: {ev.get('source','')}]"
                    for i, ev in enumerate(events)
                ])

                user_prompt = f"""DATE: {date_str} ({day_of_week}){"  ← TODAY IS MONDAY: Apply Monday Rule for Paragraph 2" if is_monday else ""}

MARKET DATA:
- S&P 500: {prices.get('spx_pct','N/D')} | Price: {prices.get('spx_price','N/D')}
- Nasdaq: {prices.get('ndx_pct','N/D')}
- Dow Jones: {prices.get('dj_pct','N/D')}
- VIX: {prices.get('vix_val','N/D')}

MARKET EVENTS IDENTIFIED TODAY (ordered by importance):
{events_text}

Write the Flip daily report now. Remember: SPANISH only, exactly 3 paragraphs, max 450 words, start with "Hola fliperos,"."""

                from google import genai as _genai
                client = _genai.Client(api_key=GEMINI_KEY)
                resp   = client.models.generate_content(
                    model="gemini-flash-lite-latest",
                    contents=FLIP_SYSTEM_PROMPT + "\n\n" + user_prompt,
                )
                report = resp.text.strip()
                self._send_json({"report": report})
            except Exception as e:
                self._send_json({"error": str(e)}, 500)
            return
        self._send_json({"error": "not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin",  "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def log_message(self, fmt, *args):
        pass


# ── entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 62)
    print("  Market Pulse — News Proxy Server  v2")
    print(f"  Puerto: {PORT}  |  TTL noticias: {TTL}s  |  TTL liveblog: {LB_TTL}s")
    print(f"  IA: {'✓ Gemini Flash (resúmenes automáticos)' if GEMINI_KEY else '✗ sin API key (usa texto original)'}")
    print("=" * 62)
    print("\nCargando datos iniciales…")
    refresh()
    refresh_liveblog()
    refresh_cnbc_lb()
    refresh_yahoo_lb()
    _analyze_market_events()
    Thread(target=_bg_loop, daemon=True).start()
    # Si no existe el HTML, generarlo en background al arrancar
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "market_pulse.html")
    if not os.path.exists(html_path):
        print("\n[html] Primera vez — generando dashboard en background (2-3 min)...")
        Thread(target=_regenerate_html, daemon=True).start()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n✓ Servidor activo en http://0.0.0.0:{PORT}")
    print(f"  /                   → dashboard (market_pulse.html)")
    print(f"  /api/news           → feed de noticias")
    print(f"  /api/liveblog       → tarjetas del live blog")
    print(f"  /api/market-events  → eventos que mueven el mercado")
    print(f"  /api/generate-report→ generador de reporte Flip")
    print(f"  /events             → SSE (push en tiempo real)")
    print(f"  /api/status         → estado del servidor\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nProxy detenido.")
