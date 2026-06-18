#!/usr/bin/env python3
"""
market_scanner.py
Lee el live blog "Stock Market Today" de CNBC (prioridad) y Yahoo Finance,
extrae solo contenido editorial (para antes de comentarios de lectores),
y usa Google Gemini para identificar las noticias que realmente mueven el mercado.

Uso:
  GEMINI_API_KEY=AIza... venv/bin/python3 market_scanner.py
  venv/bin/python3 market_scanner.py --no-ai      # solo muestra el texto crudo
  venv/bin/python3 market_scanner.py --source cnbc
  venv/bin/python3 market_scanner.py --visible    # browser visible (debug)
"""

import argparse
import os
import random
import re
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
except ImportError:
    sys.exit(
        "\n[ERROR] Playwright no instalado.\n"
        "Ejecuta:  venv/bin/pip install playwright && venv/bin/playwright install chromium\n"
    )

try:
    from google import genai as _genai_mod
    _GEMINI_OK = True
except ImportError:
    _GEMINI_OK = False

ET = ZoneInfo("America/New_York")

# Cargar GEMINI_API_KEY desde .env si no está en el entorno
def _load_api_key() -> str:
    key = os.getenv("GEMINI_API_KEY", "")
    if key:
        return key
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""

API_KEY = _load_api_key()
MAX_POSTS = 25          # máximo de posts a leer por fuente
MAX_CHARS = 14_000      # máximo de caracteres totales enviados a Claude

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
]

# ─── Helpers ────────────────────────────────────────────────────────────────

def _log(msg: str):
    print(msg, flush=True)


def _section(title: str):
    print(f"\n{'─'*66}", flush=True)
    print(f"  {title}", flush=True)
    print(f"{'─'*66}", flush=True)


def _browser_ctx(pw, headless: bool):
    ua = random.choice(USER_AGENTS)
    _log(f"  [UA] {ua[:88]}")
    return pw.chromium.launch(headless=headless).new_context(
        user_agent=ua,
        viewport={"width": 1280, "height": 900},
        extra_http_headers={
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            "Referer":         "https://www.google.com/",
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# CNBC scraper  (prioridad 1)
# ─────────────────────────────────────────────────────────────────────────────

_CNBC_JS = r"""
() => {
    // ── Detectar límite de comentarios de lectores ──
    // CNBC usa secciones de comentarios con clases como "Comments-", "SocialReferral-", etc.
    const commentBoundary = document.querySelector(
        '[class*="Comments-"], [id="comments"], [class*="UserComments"],' +
        '[class*="Disqus"], [class*="LiveBlogComments"]'
    );

    // ── Iterar todos los bloques del live blog ──
    const allBlocks = Array.from(
        document.querySelectorAll('[class*="LiveBlogBody-post"]')
    );

    const posts = [];
    let pendingTime = null;

    for (const block of allBlocks) {
        // Parar si estamos después del límite de comentarios de lectores
        if (commentBoundary && commentBoundary.compareDocumentPosition(block) &
            Node.DOCUMENT_POSITION_PRECEDING) {
            break;
        }

        const blockId = block.id || '';

        // ── Bloques reales de posts tienen IDs numéricos: "108323266-post" ──
        if (/^\d+-post$/.test(blockId)) {
            const h2 = block.querySelector('h2');
            const headline = h2 ? h2.innerText.trim() : '';
            if (!headline) continue;   // skip posts without headline

            // Body: todos los párrafos del bloque, filtrando basura
            const paragraphs = Array.from(block.querySelectorAll('p'))
                .map(p => p.innerText.trim())
                .filter(t => {
                    if (t.length < 30) return false;          // demasiado corto
                    if (/^(sign up|subscribe|watch|click|read more)/i.test(t)) return false;
                    return true;
                })
                .slice(0, 6);   // máximo 6 párrafos por post

            posts.push({
                id:       blockId,
                source:   'CNBC',
                headline: headline,
                body:     paragraphs.join(' '),
                ts_raw:   pendingTime || '',
            });
            pendingTime = null;

        } else {
            // Separador de tiempo entre posts — guarda el texto para el siguiente post real
            const text = (block.innerText || '').trim();
            for (const line of text.split('\n')) {
                const t = line.trim();
                if (!t) continue;
                if (/^\d+\s*(min|minute|hour|hr)/i.test(t)) { pendingTime = t; break; }
                if (/^\d{1,2}:\d{2}\s*(AM|PM)/i.test(t))   { pendingTime = t; break; }
            }
        }
    }
    return posts;
}
"""


def _cnbc_resolve_url(page) -> str | None:
    today = datetime.now(tz=ET)
    for delta in range(3):
        d   = today - timedelta(days=delta)
        url = (f"https://www.cnbc.com/{d.year}/{d.month:02d}/{d.day:02d}"
               f"/stock-market-today-live-updates.html")
        _log(f"  [CNBC] Probando: {url}")
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=25_000)
            if resp and resp.status == 200:
                _log(f"  [CNBC] HTTP 200 ✓")
                return url
            _log(f"  [CNBC] HTTP {resp.status if resp else '?'} — siguiente día")
        except Exception as exc:
            _log(f"  [CNBC] Error: {exc}")
    return None


def scrape_cnbc(headless: bool = True) -> list[dict]:
    _section("CNBC — Stock Market Today (prioridad)")
    posts = []
    with sync_playwright() as pw:
        ctx  = _browser_ctx(pw, headless)
        page = ctx.new_page()
        try:
            url = _cnbc_resolve_url(page)
            if not url:
                _log("  [CNBC] ✗ No se encontró URL en los últimos 3 días")
                return []

            try:
                page.wait_for_selector('[class*="LiveBlogBody-post"]', timeout=12_000)
            except PWTimeout:
                _log("  [CNBC] Timeout en selector primario — haciendo scroll")
                page.evaluate("window.scrollBy(0, 1000)")
                page.wait_for_timeout(2_500)

            n_blocks = len(page.query_selector_all('[class*="LiveBlogBody-post"]'))
            _log(f"  [CNBC] {n_blocks} bloques totales encontrados")

            raw = page.evaluate(_CNBC_JS) or []
            raw = raw[:MAX_POSTS]

            _log(f"  [CNBC] {len(raw)} posts editoriales extraídos\n")
            for i, p in enumerate(raw):
                body_preview = (p["body"] or "")[:90].replace("\n", " ")
                _log(f"  [{i+1:02d}] {p['ts_raw'][:20]:<20} | {p['headline'][:60]}")
                if body_preview:
                    _log(f"        {body_preview}...")
            posts = raw

        except Exception as exc:
            _log(f"  [CNBC] ERROR GENERAL: {exc}")
        finally:
            ctx.browser.close()

    return posts


# ─────────────────────────────────────────────────────────────────────────────
# Yahoo Finance scraper  (prioridad 2 / complemento)
# ─────────────────────────────────────────────────────────────────────────────

_YAHOO_HUB = "https://finance.yahoo.com/markets/live/"

_YAHOO_JS = r"""
() => {
    // ── Detectar límite de comentarios de lectores ──
    // Yahoo usa Viafoura / secciones de tipo reactions/comments
    const commentBoundary = document.querySelector(
        '[class*="reactions-"], [id="comments"], [class*="Comment"],' +
        '[data-testid="comments-section"], .viafoura, #viafoura_widget'
    );

    const blocks = Array.from(
        document.querySelectorAll('.liveblogposts-post')
    );

    const posts = [];

    for (const block of blocks) {
        // Parar si ya pasamos el límite de comentarios
        if (commentBoundary && commentBoundary.compareDocumentPosition(block) &
            Node.DOCUMENT_POSITION_PRECEDING) {
            break;
        }

        const id = block.id || block.getAttribute('data-id')
            || ('yahoo-' + posts.length);

        // Headline
        const headlineEl = block.querySelector(
            'h2[class*="title"], h3[class*="title"], h2, h3, [class*="headline"]'
        );
        const headline = headlineEl ? headlineEl.innerText.trim() : '';
        if (!headline) continue;

        // Body: párrafos dentro del bloque
        const paragraphs = Array.from(block.querySelectorAll('p'))
            .map(p => p.innerText.trim())
            .filter(t => {
                if (t.length < 30) return false;
                if (/^(sign up|subscribe|watch|click)/i.test(t)) return false;
                if (/^As of\s/i.test(t)) return false;   // cotización, no editorial
                return true;
            })
            .slice(0, 6);

        // Timestamp: scan lines, skip stock-quote lines ("As of...")
        let tsRaw = '';
        const fullText = (block.innerText || '');
        for (const line of fullText.split('\n')) {
            const t = line.trim();
            if (!t || /^As of/i.test(t)) continue;
            if (/[Tt]oday\s+at\s+\d{1,2}:\d{2}/.test(t))       { tsRaw = t; break; }
            if (/^\d{1,2}:\d{2}\s*(AM|PM)\s*(ET|EDT|GMT)/i.test(t)) { tsRaw = t; break; }
            if (/^\d+\s*(min|hour)/i.test(t))                   { tsRaw = t; break; }
            if (/(Mon|Tue|Wed|Thu|Fri|Sat|Sun),\s+\w+\s+\d{1,2}/.test(t)) { tsRaw = t; break; }
        }

        posts.push({ id, source: 'Yahoo Finance', headline, body: paragraphs.join(' '), ts_raw: tsRaw });
    }
    return posts;
}
"""


def scrape_yahoo(headless: bool = True) -> list[dict]:
    _section("Yahoo Finance — Stock Market Today (complemento)")
    posts = []
    with sync_playwright() as pw:
        ctx  = _browser_ctx(pw, headless)
        page = ctx.new_page()
        try:
            _log(f"  [Yahoo] Cargando hub: {_YAHOO_HUB}")
            page.goto(_YAHOO_HUB, wait_until="domcontentloaded", timeout=25_000)
            page.wait_for_timeout(3_000)

            links = page.eval_on_selector_all(
                'a[href*="stock-market-today"]',
                "els => [...new Set(els.map(e => e.href))]",
            )
            links = [l for l in links if "stock-market-today" in l]
            _log(f"  [Yahoo] {len(links)} artículos en hub")
            for l in links[:4]:
                _log(f"          {l}")

            if not links:
                _log("  [Yahoo] ✗ No se encontraron links al live blog")
                return []

            article_url = links[0]
            _log(f"\n  [Yahoo] Navegando al artículo más reciente...")
            page.goto(article_url, wait_until="domcontentloaded", timeout=25_000)
            page.wait_for_timeout(4_000)
            page.evaluate("window.scrollBy(0, 1500)")
            page.wait_for_timeout(2_000)

            n = len(page.query_selector_all('.liveblogposts-post'))
            _log(f"  [Yahoo] {n} bloques .liveblogposts-post encontrados")

            raw = page.evaluate(_YAHOO_JS) or []
            raw = raw[:MAX_POSTS]

            _log(f"  [Yahoo] {len(raw)} posts editoriales extraídos\n")
            for i, p in enumerate(raw):
                body_preview = (p["body"] or "")[:90].replace("\n", " ")
                _log(f"  [{i+1:02d}] {p['ts_raw'][:20]:<20} | {p['headline'][:60]}")
                if body_preview:
                    _log(f"        {body_preview}...")
            posts = raw

        except Exception as exc:
            _log(f"  [Yahoo] ERROR GENERAL: {exc}")
        finally:
            ctx.browser.close()

    return posts


# ─────────────────────────────────────────────────────────────────────────────
# Análisis con Claude
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """Eres un analista financiero senior especializado en mercados de renta variable y renta fija norteamericanos.
Tu rol es leer el contenido editorial de los live blogs de mercado (CNBC y Yahoo Finance) y extraer solo las noticias que genuinamente mueven precios: catalizadores con impacto directo en acciones, índices, commodities o tasas.
Descarta: resúmenes genéricos del día, earnings calendars, recordatorios de horarios, y cualquier nota de relleno sin impacto medible."""

_USER_TEMPLATE = """A continuación tienes el contenido editorial del live blog "Stock Market Today" de hoy ({date}).
El contenido de CNBC tiene prioridad; Yahoo Finance es complementario.

---

{blog_content}

---

TAREA: Identifica las {n} noticias más importantes que están moviendo el mercado hoy.

CATEGORÍAS DE ALTA PRIORIDAD — si aparecen en el texto, SIEMPRE inclúyelas primero:
1. Resultados corporativos trimestrales: earnings, EPS, revenue, guidance de grandes empresas públicas; beats o misses relevantes
2. Publicaciones económicas: PIB, inflación/IPC/PCE, tasa de interés, reporte de empleo/nóminas no agrícolas, desempleo
3. Noticias sobre petróleo: precio del crudo, decisiones de la OPEP+, inventarios de petróleo, producción
4. Reserva Federal (Fed): reuniones del FOMC, decisiones de tasas, declaraciones de Powell u otros miembros, minutas
5. IPOs importantes: salidas a bolsa de empresas con valuación superior a 1 trillón USD

Para cada noticia entrega este formato exacto:

**[N]. [Titular conciso]**
- **Activos afectados:** [ticker(s) o sector]
- **Movimiento:** [Sube X% / Baja X% / Por definir]
- **Por qué importa:** [1-2 oraciones de impacto macro o sectorial]

Ordena de mayor a menor impacto: primero las categorías de alta prioridad, luego el resto.
Solo incluye noticias con catalizador claro y verificable en el texto. No inventes datos."""


def analyze_with_gemini(posts: list[dict]) -> str:
    if not _GEMINI_OK:
        return "[AI no disponible — instala: venv/bin/pip install google-generativeai]"
    if not API_KEY:
        return "[GEMINI_API_KEY no configurado — agrega GEMINI_API_KEY=... al archivo .env]"

    # Construir el texto del blog ordenando CNBC primero
    cnbc_posts  = [p for p in posts if p["source"] == "CNBC"]
    yahoo_posts = [p for p in posts if p["source"] == "Yahoo Finance"]

    sections = []
    if cnbc_posts:
        sections.append("=== CNBC (prioridad) ===")
        for p in cnbc_posts:
            ts   = f"[{p['ts_raw']}] " if p.get("ts_raw") else ""
            body = p.get("body", "").strip()
            sections.append(f"\n{ts}**{p['headline']}**")
            if body:
                sections.append(body[:600])  # cap por post

    if yahoo_posts:
        sections.append("\n\n=== Yahoo Finance (complemento) ===")
        for p in yahoo_posts:
            ts   = f"[{p['ts_raw']}] " if p.get("ts_raw") else ""
            body = p.get("body", "").strip()
            sections.append(f"\n{ts}**{p['headline']}**")
            if body:
                sections.append(body[:400])  # menor peso

    blog_content = "\n".join(sections)

    # Truncar al límite de caracteres
    if len(blog_content) > MAX_CHARS:
        blog_content = blog_content[:MAX_CHARS] + "\n\n[...contenido truncado por límite de tokens]"

    n_news   = min(8, max(5, len(posts) // 4))
    today_str = datetime.now(tz=ET).strftime("%A, %B %d %Y")
    prompt   = _USER_TEMPLATE.format(
        date=today_str, blog_content=blog_content, n=n_news
    )

    _log(f"\n  [Gemini] Enviando {len(blog_content):,} caracteres para análisis...")
    _log(f"  [Gemini] {len(cnbc_posts)} posts CNBC + {len(yahoo_posts)} posts Yahoo → solicitando top {n_news} noticias")

    client   = _genai_mod.Client(api_key=API_KEY)
    response = client.models.generate_content(
        model="gemini-flash-lite-latest",
        contents=_SYSTEM_PROMPT + "\n\n" + prompt,
    )
    return response.text


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Market Scanner — noticias que mueven el mercado (CNBC + Yahoo)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", choices=["cnbc", "yahoo", "all"], default="all",
        help="Fuente a leer (default: all — CNBC prioridad)"
    )
    parser.add_argument(
        "--visible", action="store_true",
        help="Abrir browser visible (útil para debug)"
    )
    parser.add_argument(
        "--no-ai", action="store_true",
        help="No enviar a Claude, solo mostrar texto crudo extraído"
    )
    args = parser.parse_args()
    headless = not args.visible

    all_posts: list[dict] = []

    if args.source in ("cnbc", "all"):
        all_posts += scrape_cnbc(headless=headless)

    if args.source in ("yahoo", "all"):
        all_posts += scrape_yahoo(headless=headless)

    if not all_posts:
        _log("\n[✗] No se extrajo contenido de ninguna fuente.")
        return

    _log(f"\n{'='*66}")
    _log(f"  TOTAL: {len(all_posts)} posts editoriales")
    _log(f"  CNBC: {sum(1 for p in all_posts if p['source']=='CNBC')} | "
         f"Yahoo: {sum(1 for p in all_posts if p['source']=='Yahoo Finance')}")
    _log(f"{'='*66}")

    if args.no_ai:
        _log("\n[--no-ai] Análisis omitido.")
        return

    _section("Análisis de mercado — Claude AI")
    analysis = analyze_with_gemini(all_posts)

    print("\n")
    print("═" * 66)
    print("  NOTICIAS QUE MUEVEN EL MERCADO HOY")
    print(f"  {datetime.now(tz=ET).strftime('%A, %B %d %Y  %I:%M %p ET')}")
    print("═" * 66)
    print()
    print(analysis)
    print()
    print("═" * 66)


if __name__ == "__main__":
    main()
