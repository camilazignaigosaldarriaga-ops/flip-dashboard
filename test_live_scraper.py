#!/usr/bin/env python3
"""
test_live_scraper.py
Extrae ID, timestamp ISO-UTC y headline de los live blogs de CNBC y Yahoo Finance.
Solo para validación — sin body, sin resúmenes, sin dependencias del dashboard.

Uso:
  venv/bin/python3 test_live_scraper.py                # ambas fuentes, headless
  venv/bin/python3 test_live_scraper.py --source cnbc  # solo CNBC
  venv/bin/python3 test_live_scraper.py --source yahoo # solo Yahoo
  venv/bin/python3 test_live_scraper.py --visible      # abre browser visible
  venv/bin/python3 test_live_scraper.py --json         # output solo JSON
"""

import argparse
import json
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

# ─────────────────────────────────────────────
# Utilidades comunes
# ─────────────────────────────────────────────

ET = ZoneInfo("America/New_York")

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) Gecko/20100101 Firefox/126.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36 Edg/126.0.0.0",
]

_MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_RELATIVE_RE = re.compile(r"(\d+)\s*(min|minute|hour|hr|h|m|second|sec|s)", re.IGNORECASE)
_EPOCH_RE    = re.compile(r"^\d{10,13}$")


def random_ua() -> str:
    ua = random.choice(USER_AGENTS)
    print(f"  [UA] {ua[:90]}")
    return ua


def parse_timestamp(raw: str) -> str | None:
    """Convierte cualquier formato de timestamp a ISO-8601 UTC. Retorna None si falla."""
    if not raw:
        return None
    raw = raw.strip()

    # Unix epoch en segundos (10 dígitos) o milisegundos (13 dígitos)
    if _EPOCH_RE.match(raw):
        ts = int(raw)
        if ts > 1_000_000_000_000:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    # ISO-8601 directo
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc).isoformat()
    except ValueError:
        pass

    # Relativo: "17 min ago", "2h ago", "34 Min Ago"
    m = _RELATIVE_RE.search(raw)
    if m:
        n, unit = int(m.group(1)), m.group(2).lower()
        delta = timedelta(minutes=n) if unit.startswith("m") else timedelta(hours=n)
        return (datetime.now(tz=timezone.utc) - delta).isoformat()

    # "Today at 10:33 AM GMT-5" or "Today at 10:07 AM GMT+0"
    today_m = re.search(
        r"[Tt]oday\s+at\s+(\d{1,2}):(\d{2})\s*(AM|PM)\s*GMT([+-]\d+)",
        raw,
    )
    if today_m:
        hh, mm, ampm, offset_str = today_m.groups()
        hour = int(hh)
        if ampm.upper() == "PM" and hour != 12:
            hour += 12
        elif ampm.upper() == "AM" and hour == 12:
            hour = 0
        offset_h = int(offset_str)
        today_local = datetime.now(tz=timezone.utc)
        naive = datetime(today_local.year, today_local.month, today_local.day, hour, int(mm))
        # GMT-5 means UTC+5 hours offset → subtract offset
        dt = naive.replace(tzinfo=timezone(timedelta(hours=offset_h)))
        return dt.astimezone(timezone.utc).isoformat()

    # "June 17, 2026 · 9:45 AM ET"
    gm = re.search(
        r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\w*\s+(\d{1,2}),?\s+(\d{4})"
        r"[^·\d]*·?\s*(\d{1,2}):(\d{2})\s*(AM|PM)?\s*(ET|EST|EDT)?",
        raw, re.IGNORECASE,
    )
    if gm:
        mon, day, year, hh, mm, ampm, _ = gm.groups()
        hour = int(hh)
        if ampm and ampm.upper() == "PM" and hour != 12:
            hour += 12
        elif ampm and ampm.upper() == "AM" and hour == 12:
            hour = 0
        dt = datetime(int(year), _MONTH_MAP[mon[:3].lower()], int(day), hour, int(mm),
                      tzinfo=ET)
        return dt.astimezone(timezone.utc).isoformat()

    return None  # no reconocido


def _section(title: str):
    print(f"\n{'='*64}")
    print(f"  {title}")
    print(f"{'='*64}")


# ─────────────────────────────────────────────
# CNBC
# ─────────────────────────────────────────────

# Orden de intento: el selector más específico primero.
CNBC_BLOCK_SELECTORS = [
    '[class*="LiveBlogBody-post"]',   # probado ✓ (58 elementos en producción)
    '[class*="LiveBlogUpdate"]',
    '[class*="liveBlog-post"]',
    'article[class*="live"]',
    'div[data-testid*="live"]',
]

CNBC_ID_ATTRS = ["data-id", "id", "data-post-id", "data-anchor"]


def _cnbc_resolve_url(page) -> str | None:
    """Intenta URLs de hoy, ayer y anteayer hasta encontrar HTTP 200."""
    today = datetime.now(tz=ET)
    for delta in range(3):
        d = today - timedelta(days=delta)
        url = (
            f"https://www.cnbc.com/{d.year}/{d.month:02d}/{d.day:02d}"
            f"/stock-market-today-live-updates.html"
        )
        print(f"  [CNBC] Probando URL: {url}")
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=22_000)
            code = resp.status if resp else "?"
            if code == 200:
                print(f"  [CNBC] HTTP 200 — URL confirmada ✓")
                return url
            print(f"  [CNBC] HTTP {code} — siguiente día")
        except PWTimeout:
            print(f"  [CNBC] Timeout cargando {url}")
        except Exception as exc:
            print(f"  [CNBC] Error: {exc}")
    return None


def _cnbc_extract_all(page) -> list[dict]:
    """
    Extrae posts reales del live blog de CNBC usando un traversal DOM completo en JS.
    CNBC usa esta estructura de bloques:
      <div class="LiveBlogBody-post">        ← timestamp separator ("18 MIN AGO")
      <div id="108323266-post" class="...">  ← post real (h2 + body)
      <div class="LiveBlogBody-post">        ← empty spacer

    El timestamp está en el bloque ANTERIOR al post real, por eso usamos
    traversal secuencial con estado (pendingTime) en lugar de CSS aislado por bloque.
    """
    raw_posts = page.evaluate("""
        () => {
            const results = [];
            const allBlocks = Array.from(
                document.querySelectorAll('[class*="LiveBlogBody-post"]')
            );
            let pendingTime = null;

            for (const block of allBlocks) {
                const blockId = block.id || '';
                const fullText = (block.innerText || '').trim();

                // ── Real post block: ID matches "123456789-post" ──
                if (/^\\d+-post$/.test(blockId)) {
                    // Headline: first h2, else first non-empty line
                    const h2 = block.querySelector('h2');
                    const headline = h2
                        ? h2.innerText.trim()
                        : fullText.split('\\n')[0].trim().slice(0, 250);

                    // Timestamp: use captured pendingTime, or scan block text
                    let tsRaw = pendingTime || '';
                    if (!tsRaw) {
                        // Look for time patterns inside this block
                        for (const line of fullText.split('\\n')) {
                            const t = line.trim();
                            if (/^(\\d+)\\s*(min|minute|hour|hr)/i.test(t)) { tsRaw = t; break; }
                            if (/\\d{1,2}:\\d{2}\\s*(AM|PM)/i.test(t))      { tsRaw = t; break; }
                        }
                    }

                    results.push({ id: blockId, headline, ts_raw: tsRaw });
                    pendingTime = null;  // consumed

                } else {
                    // Separator / wrapper block — harvest timestamp text for next real post
                    for (const line of fullText.split('\\n')) {
                        const t = line.trim();
                        if (!t) continue;
                        // Relative: "18 MIN AGO", "1 HOUR AGO", "47 Min Ago"
                        if (/^(\\d+)\\s*(min|minute|hour|hr)/i.test(t)) { pendingTime = t; break; }
                        // Absolute: "9:45 AM ET", "10:30 AM EDT"
                        if (/^\\d{1,2}:\\d{2}\\s*(AM|PM)/i.test(t))    { pendingTime = t; break; }
                    }
                }
            }
            return results;
        }
    """)
    return raw_posts or []


def scrape_cnbc(headless: bool = True) -> list[dict]:
    _section("CNBC Live Blog — extracción de headlines")
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ua = random_ua()
        ctx = browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        )
        page = ctx.new_page()

        try:
            url = _cnbc_resolve_url(page)
            if not url:
                print("  [CNBC] ✗ No se encontró URL válida del live blog")
                return []

            # Esperar el primer bloque o hacer scroll si tarda
            try:
                page.wait_for_selector(CNBC_BLOCK_SELECTORS[0], timeout=10_000)
            except PWTimeout:
                print("  [CNBC] Selector primario no cargó — haciendo scroll para forzar render")
                page.evaluate("window.scrollBy(0, 1200)")
                page.wait_for_timeout(2_500)

            # Diagnóstico: cuántos elementos por selector
            for sel in CNBC_BLOCK_SELECTORS:
                n = len(page.query_selector_all(sel))
                print(f"  [CNBC] Selector '{sel}' → {n} elementos totales")
                if n:
                    break

            print(f"\n  [CNBC] Ejecutando traversal DOM para extraer posts reales...\n")
            raw_posts = _cnbc_extract_all(page)

            if not raw_posts:
                print("  [CNBC] ✗ JS traversal no encontró posts. Guardando HTML...")
                with open("cnbc_debug.html", "w") as f:
                    f.write(page.content())
                print("  [CNBC] HTML guardado en cnbc_debug.html")
                return []

            print(f"  [CNBC] JS extrajo {len(raw_posts)} posts reales (con ID \\d+-post)\n")
            for i, p in enumerate(raw_posts):
                ts_iso = parse_timestamp(p["ts_raw"]) if p["ts_raw"] else None
                status = ts_iso if ts_iso else "PARSE-FAIL"
                print(f"    [{i:02d}] id={p['id']:<22} ts_raw={p['ts_raw'][:28]:<28} → {status}")
                print(f"          headline={p['headline'][:80]}")
                results.append({
                    "source":    "CNBC",
                    "id":        p["id"],
                    "timestamp": ts_iso,
                    "headline":  p["headline"],
                })

        except Exception as exc:
            print(f"  [CNBC] ERROR GENERAL: {exc}")
        finally:
            browser.close()

    valid = [r for r in results if r["headline"]]
    print(f"\n  [CNBC] Extraídos: {len(valid)} posts con headline")
    return valid


# ─────────────────────────────────────────────
# Yahoo Finance
# ─────────────────────────────────────────────

YAHOO_HUB_URL = "https://finance.yahoo.com/markets/live/"

YAHOO_BLOCK_SELECTORS = [
    ".liveblogposts-post",             # probado ✓ (9 elementos en producción)
    '[class*="liveblogposts"]',
    '[data-testid*="live-blog"]',
    '[class*="LiveBlog"]',
    'li[class*="post"]',
]

def _yahoo_resolve_url(page) -> str | None:
    """Navega al hub de Yahoo y devuelve el primer link al live blog del día."""
    print(f"  [Yahoo] Cargando hub: {YAHOO_HUB_URL}")
    try:
        page.goto(YAHOO_HUB_URL, wait_until="domcontentloaded", timeout=25_000)
        page.wait_for_timeout(3_000)
    except Exception as exc:
        print(f"  [Yahoo] Error cargando hub: {exc}")
        return None

    # Buscar links que contengan "stock-market-today"
    links = page.eval_on_selector_all(
        'a[href*="stock-market-today"], a[href*="markets/live/"]',
        "els => [...new Set(els.map(e => e.href))]",
    )
    links = [l for l in links if "stock-market-today" in l]
    print(f"  [Yahoo] {len(links)} links 'stock-market-today' encontrados:")
    for l in links[:5]:
        print(f"          {l}")

    if not links:
        print("  [Yahoo] ✗ No se encontraron links al live blog")
        return None

    # El primero en el DOM es el más reciente
    chosen = links[0]
    print(f"  [Yahoo] ✓ Artículo seleccionado: {chosen}")
    return chosen


def _yahoo_extract_all(page) -> list[dict]:
    """
    Extrae posts de Yahoo Finance live blog usando traversal DOM en JS.
    Problema conocido: Yahoo tiene elementos <time datetime="..."> con cotizaciones
    de precios ("As of 11:57 AM EDT") — estos se descartan buscando el tiempo del POST,
    que aparece como texto "H:MM AM ET" o "X min ago" en las líneas del bloque.
    """
    raw_posts = page.evaluate("""
        () => {
            const results = [];
            const blocks = Array.from(
                document.querySelectorAll('.liveblogposts-post')
            );

            for (const block of blocks) {
                const id = block.id
                    || block.getAttribute('data-id')
                    || ('yahoo-' + results.length);

                const fullText = (block.innerText || '').trim();

                // Headline: h2, h3, or first non-empty line
                const headlineEl = block.querySelector(
                    'h2[class*="title"], h3[class*="title"], h2, h3, [class*="headline"]'
                );
                const headline = headlineEl
                    ? headlineEl.innerText.trim()
                    : fullText.split('\\n')[0].trim().slice(0, 250);

                // Timestamp: scan lines, skip stock quote lines ("As of H:MM:SS...")
                let tsRaw = '';
                for (const line of fullText.split('\\n')) {
                    const t = line.trim();
                    if (!t || /^As of/i.test(t)) continue;
                    // "9:45 AM ET", "11:30 AM EDT"
                    if (/^\\d{1,2}:\\d{2}\\s*(AM|PM)\\s*(ET|EDT|EST)?$/i.test(t)) {
                        tsRaw = t; break;
                    }
                    // "June 17, 2026 · 9:45 AM ET" style (longer)
                    if (/\\d{1,2}:\\d{2}\\s*(AM|PM)/i.test(t) && t.length < 60) {
                        tsRaw = t; break;
                    }
                    // Relative: "17 min ago", "2h ago"
                    if (/^\\d+\\s*(min|hour|hr)/i.test(t)) { tsRaw = t; break; }
                }

                // Also try <time datetime="..."> but only if its text is NOT a stock quote
                if (!tsRaw) {
                    const timeEl = block.querySelector('time[datetime]');
                    if (timeEl) {
                        const visibleText = (timeEl.innerText || '').trim();
                        if (!/^As of/i.test(visibleText)) {
                            tsRaw = timeEl.getAttribute('datetime') || visibleText;
                        }
                    }
                }

                results.push({ id, headline, ts_raw: tsRaw });
            }
            return results;
        }
    """)
    return raw_posts or []


def scrape_yahoo(headless: bool = True) -> list[dict]:
    _section("Yahoo Finance Live Blog — extracción de headlines")
    results = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        ua = random_ua()
        ctx = browser.new_context(
            user_agent=ua,
            viewport={"width": 1280, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
            },
        )
        page = ctx.new_page()

        try:
            article_url = _yahoo_resolve_url(page)
            if not article_url:
                return []

            print(f"\n  [Yahoo] Navegando al artículo...")
            page.goto(article_url, wait_until="domcontentloaded", timeout=25_000)
            page.wait_for_timeout(4_000)

            # Scroll para que React/CSR renderice el feed
            print(f"  [Yahoo] Scroll para cargar contenido dinámico...")
            page.evaluate("window.scrollBy(0, 1500)")
            page.wait_for_timeout(2_000)

            # Diagnóstico
            for sel in YAHOO_BLOCK_SELECTORS:
                n = len(page.query_selector_all(sel))
                print(f"  [Yahoo] Selector '{sel}' → {n} elementos")
                if n:
                    break

            print(f"\n  [Yahoo] Ejecutando traversal DOM...\n")
            raw_posts = _yahoo_extract_all(page)

            if not raw_posts:
                print("  [Yahoo] ✗ JS traversal no encontró posts. Guardando HTML para diagnóstico...")
                with open("yahoo_debug.html", "w") as f:
                    f.write(page.content())
                print("  [Yahoo] HTML guardado en yahoo_debug.html")
                return []

            print(f"  [Yahoo] JS extrajo {len(raw_posts)} posts\n")
            for i, p in enumerate(raw_posts):
                ts_iso = parse_timestamp(p["ts_raw"]) if p["ts_raw"] else None
                status = ts_iso if ts_iso else "PARSE-FAIL"
                print(f"    [{i:02d}] id={p['id']:<35} ts_raw={p['ts_raw'][:22]:<22} → {status}")
                print(f"          headline={p['headline'][:80]}")
                results.append({
                    "source":    "Yahoo Finance",
                    "id":        p["id"],
                    "timestamp": ts_iso,
                    "headline":  p["headline"],
                })

        except Exception as exc:
            print(f"  [Yahoo] ERROR GENERAL: {exc}")
        finally:
            browser.close()

    valid = [r for r in results if r["headline"]]
    print(f"\n  [Yahoo] Extraídos: {len(valid)} posts con headline")
    return valid


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Test Live Blog Scraper — solo ID, timestamp y headline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--source", choices=["cnbc", "yahoo", "all"], default="all",
        help="Fuente a scrapear (default: all)",
    )
    parser.add_argument(
        "--visible", action="store_true",
        help="Mostrar ventana del browser (útil para depurar)",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="Imprimir resultado final solo como JSON",
    )
    args = parser.parse_args()
    headless = not args.visible

    all_posts: list[dict] = []

    if args.source in ("cnbc", "all"):
        all_posts += scrape_cnbc(headless=headless)

    if args.source in ("yahoo", "all"):
        all_posts += scrape_yahoo(headless=headless)

    # Ordenar: más reciente primero; posts sin timestamp al final
    all_posts.sort(key=lambda p: p["timestamp"] or "0000", reverse=True)

    _section(f"RESULTADO FINAL — {len(all_posts)} posts (más reciente → más antiguo)")

    if args.json:
        print(json.dumps(all_posts, ensure_ascii=False, indent=2))
    else:
        for i, p in enumerate(all_posts):
            ts   = p["timestamp"] or "sin-timestamp"
            src  = p["source"]
            head = p["headline"][:80]
            pid  = p["id"][:30]
            print(f"  [{i+1:02d}] [{src:<14}] {ts} | {pid}")
            print(f"        {head}")
            print()

    return all_posts


if __name__ == "__main__":
    main()
