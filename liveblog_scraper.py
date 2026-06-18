#!/usr/bin/env python3
"""
liveblog_scraper.py — Extractor de Live Blogs de mercados en tiempo real
=========================================================================
Fuentes: CNBC  |  Yahoo Finance

Uso básico
----------
  python3 liveblog_scraper.py                        # ambas fuentes, salida legible
  python3 liveblog_scraper.py --source cnbc          # solo CNBC
  python3 liveblog_scraper.py --source yahoo         # solo Yahoo Finance
  python3 liveblog_scraper.py --output json          # JSON listo para consumir
  python3 liveblog_scraper.py --source cnbc --url https://www.cnbc.com/.../...html

Instalación de dependencias
----------------------------
  pip install playwright beautifulsoup4 lxml
  playwright install chromium

Para usar los resúmenes de IA:
  export ANTHROPIC_API_KEY=sk-ant-...
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Generator, Optional

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Browser, Page, sync_playwright

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("liveblog")


# ─────────────────────────────────────────────────────────────────────────────
# Configuración
# ─────────────────────────────────────────────────────────────────────────────
PLAYWRIGHT_TIMEOUT   = 40_000   # ms — timeout de navegación
RENDER_WAIT          = 5_000    # ms — espera tras DOMContentLoaded
MAX_POSTS_PER_SOURCE = 25
HEADLESS             = True
RETRY_ATTEMPTS       = 2        # reintentos por fallo de red


# ─────────────────────────────────────────────────────────────────────────────
# Rotación de User-Agent
# ─────────────────────────────────────────────────────────────────────────────
_UA_POOL = [
    # Chrome macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Chrome Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    # Safari macOS
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    # Firefox Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
    "Gecko/20100101 Firefox/126.0",
    # Chrome Linux
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    # Edge Windows
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0",
]


def _random_ua() -> str:
    return random.choice(_UA_POOL)


# ─────────────────────────────────────────────────────────────────────────────
# Modelo de datos
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class LiveBlogPost:
    """Un único bloque de actualización de un live blog."""
    post_id:       str   # ID único (attr HTML o hash determinístico)
    source:        str   # "CNBC" | "Yahoo Finance"
    article_url:   str   # URL canónica del artículo live
    headline:      str   # Titular del bloque
    body:          str   # Texto limpio (sin HTML, scripts ni ads)
    timestamp_iso: str   # ISO 8601 UTC — "2024-06-17T14:30:00+00:00"
    timestamp_raw: str   # Cadena original de la página (para debug)

    def to_dict(self) -> dict:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Context manager de navegador (Playwright)
# ─────────────────────────────────────────────────────────────────────────────
@contextmanager
def _browser_page() -> Generator[tuple[Browser, Page], None, None]:
    """
    Abre un browser Chromium headless con UA aleatorio.
    Cierra limpiamente al salir del bloque with, incluso en caso de error.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        ctx = browser.new_context(
            user_agent=_random_ua(),
            extra_http_headers={
                "Cache-Control":     "no-cache",
                "Accept-Language":   "en-US,en;q=0.9",
                "Accept":            "text/html,application/xhtml+xml,*/*",
            },
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.set_default_timeout(PLAYWRIGHT_TIMEOUT)
        try:
            yield browser, page
        finally:
            browser.close()


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades de texto
# ─────────────────────────────────────────────────────────────────────────────
_JUNK_TAGS = {
    "script", "style", "noscript", "iframe", "img", "figure",
    "picture", "button", "nav", "aside", "form", "input", "svg",
    "header", "footer", "advertisement",
}

# Patrones de contenido promocional a eliminar del cuerpo
_AD_PATTERNS = re.compile(
    r"(sign up for|subscribe (to|now)|read more at|click here|learn more"
    r"|watch live|watch now|download (our|the)|follow us)",
    re.I,
)


def _clean_tag(tag: Tag) -> str:
    """Extrae texto limpio de un Tag de BeautifulSoup, eliminando basura."""
    for junk in tag.find_all(_JUNK_TAGS):
        junk.decompose()
    text = " ".join(tag.get_text(" ", strip=True).split())
    # Cortar en el primer patrón publicitario
    m = _AD_PATTERNS.search(text)
    if m:
        text = text[: m.start()].strip()
    return text


def _extract_paragraphs(tag: Tag) -> list[str]:
    """Devuelve lista de párrafos limpios (>15 chars) de un elemento."""
    paras: list[str] = []
    for p in tag.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if len(txt) > 15:
            paras.append(txt)
    if not paras:
        # Fallback: dividir el texto limpio por saltos de línea
        full = _clean_tag(tag)
        paras = [ln.strip() for ln in full.splitlines() if len(ln.strip()) > 20]
    return paras


def _make_id(source: str, headline: str, ts_raw: str) -> str:
    """ID determinístico cuando el HTML no provee uno explícito."""
    raw = f"{source}|{headline}|{ts_raw}"
    return hashlib.sha1(raw.encode()).hexdigest()[:14]


# ─────────────────────────────────────────────────────────────────────────────
# Parseo de timestamps
# ─────────────────────────────────────────────────────────────────────────────
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3,    "april": 4,
    "may": 5,     "june": 6,     "july": 7,     "august": 8,
    "september":9,"october": 10, "november": 11, "december": 12,
    # abreviaciones
    "jan":1,"feb":2,"mar":3,"apr":4,"jun":6,"jul":7,
    "aug":8,"sep":9,"oct":10,"nov":11,"dec":12,
}


def _parse_timestamp(raw: str) -> Optional[datetime]:
    """
    Intenta parsear un string de timestamp en cualquiera de los formatos
    habituales de CNBC/Yahoo. Devuelve datetime UTC o None si falla.
    """
    raw = (raw or "").strip()
    if not raw:
        return None

    # Unix epoch (segundos o milisegundos)
    if re.fullmatch(r"\d{10,13}", raw):
        epoch = int(raw)
        if epoch > 1_000_000_000_000:
            epoch //= 1000
        return datetime.fromtimestamp(epoch, tz=timezone.utc)

    # Formatos ISO-8601 y variantes
    iso_formats = [
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]
    for fmt in iso_formats:
        try:
            dt = datetime.strptime(raw[:25], fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError:
            pass

    # "June 17, 2024 · 9:45 AM ET" — formato CNBC legible
    m = re.search(
        r"(\w+)\s+(\d{1,2}),\s*(\d{4})\s*[·•@,]\s*(\d{1,2}:\d{2})\s*([AP]M)\s*(ET|EST|EDT|UTC|PT|CT)?",
        raw, re.I,
    )
    if m:
        mon_str, day, year, hhmm, ampm, tz_str = m.groups()
        mon = _MONTH_NAMES.get(mon_str.lower(), 0)
        if mon:
            try:
                dt_str = f"{year}-{mon:02d}-{int(day):02d} {hhmm} {ampm}"
                dt = datetime.strptime(dt_str, "%Y-%m-%d %I:%M %p")
                tz_str = (tz_str or "ET").upper()
                offset = (
                    timedelta(hours=-4) if tz_str in ("EDT", "ET")
                    else timedelta(hours=-5) if tz_str == "EST"
                    else timedelta(hours=-6) if tz_str == "CT"
                    else timedelta(hours=-7) if tz_str == "PT"
                    else timedelta(0)
                )
                return dt.replace(tzinfo=timezone(offset)).astimezone(timezone.utc)
            except ValueError:
                pass

    # "2h ago", "35 min ago" — convierte a UTC aproximado
    m = re.match(r"(\d+)\s*(min|hour|hr|h)\w*\s+ago", raw, re.I)
    if m:
        amount, unit = int(m.group(1)), m.group(2).lower()
        delta = timedelta(hours=amount) if unit.startswith("h") else timedelta(minutes=amount)
        return datetime.now(timezone.utc) - delta

    log.debug("timestamp no parseado: %r", raw)
    return None


def _to_iso(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# Parseo de __NEXT_DATA__ (Next.js SSR — usado por CNBC)
# ─────────────────────────────────────────────────────────────────────────────
def _find_post_arrays(obj: object, depth: int = 0) -> list[list]:
    """
    Búsqueda recursiva en el árbol JSON de __NEXT_DATA__.
    Detecta arrays cuyos elementos tienen headline + fecha = posts de live blog.
    """
    if depth > 14 or not isinstance(obj, (dict, list)):
        return []

    results: list[list] = []

    if isinstance(obj, list) and len(obj) >= 2:
        sample = [x for x in obj[:4] if isinstance(x, dict)]
        has_headline = any("headline" in x or "title" in x or "name" in x for x in sample)
        has_date     = any(
            any(k in x for k in ("datePublished", "publishedAt", "createdAt",
                                  "date", "timestamp", "created_at"))
            for x in sample
        )
        if has_headline and has_date:
            results.append(obj)

    iterable = obj.values() if isinstance(obj, dict) else obj
    for child in iterable:
        results.extend(_find_post_arrays(child, depth + 1))

    return results


def _parse_next_data(html: str, fallback_url: str, source: str) -> list[LiveBlogPost]:
    """
    Extrae posts desde __NEXT_DATA__ en el HTML.
    Estrategia robusta: búsqueda recursiva, no depende de un path fijo.
    """
    soup  = BeautifulSoup(html, "lxml")
    nd_el = soup.find("script", {"id": "__NEXT_DATA__"})
    if not nd_el or not nd_el.string:
        return []

    try:
        nd = json.loads(nd_el.string)
    except json.JSONDecodeError as exc:
        log.warning("[%s] __NEXT_DATA__ inválido: %s", source, exc)
        return []

    arrays = _find_post_arrays(nd)
    if not arrays:
        return []

    # Usar el array más grande (probablemente el stream principal)
    post_list: list[dict] = max(arrays, key=len)
    posts: list[LiveBlogPost] = []

    for raw in post_list[:40]:
        if not isinstance(raw, dict):
            continue

        headline = re.sub(r"<[^>]+>", "", str(
            raw.get("headline") or raw.get("title") or raw.get("name") or ""
        )).strip()
        if len(headline) < 8:
            continue

        # Timestamp
        ts_val = (
            raw.get("datePublished") or raw.get("publishedAt") or
            raw.get("createdAt")     or raw.get("date")         or
            raw.get("timestamp")     or raw.get("created_at")   or ""
        )
        ts_dt  = _parse_timestamp(str(ts_val))

        # Cuerpo: aplanar bloques de contenido anidados
        body_parts: list[str] = []
        for field in ("body", "content", "description", "bodyText", "summary"):
            val = raw.get(field)
            if isinstance(val, str) and val:
                soup_body = BeautifulSoup(val, "lxml")
                body_parts.append(_clean_tag(soup_body))
                break
            if isinstance(val, list):
                for block in val:
                    if isinstance(block, dict):
                        text = block.get("text") or block.get("html") or ""
                        if text:
                            soup_body = BeautifulSoup(str(text), "lxml")
                            body_parts.append(_clean_tag(soup_body))

        body    = " ".join(body_parts).strip()
        post_id = str(raw.get("id") or raw.get("nid") or raw.get("_id") or "")
        if not post_id:
            post_id = _make_id(source, headline, str(ts_val))

        posts.append(LiveBlogPost(
            post_id       = post_id,
            source        = source,
            article_url   = str(raw.get("url") or raw.get("canonicalUrl") or fallback_url),
            headline      = headline,
            body          = body,
            timestamp_iso = _to_iso(ts_dt),
            timestamp_raw = str(ts_val),
        ))

    log.info("[%s] __NEXT_DATA__ → %d posts", source, len(posts))
    return posts


# ─────────────────────────────────────────────────────────────────────────────
# Parseo CSS (fallback genérico para DOM renderizado)
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class _SelectorBundle:
    """Conjunto de selectores CSS para un origen dado."""
    post:      list[str]  # selectores de contenedor de post
    timestamp: list[str]  # selectores de timestamp dentro del post
    headline:  list[str]  # selectores de titular


_CNBC_SELECTORS = _SelectorBundle(
    post=[
        'div[class*="LiveBlog-updates"] > div[class*="LiveBlog-update"]',
        '[class*="LiveBlogBody-post"]',
        '[class*="LiveBlogBody"] [class*="post"]',
        'article[class*="LiveBlog"]',
        '[class*="live-blog"] article',
        '[data-testid*="live-update"]',
    ],
    timestamp=[
        'time[datetime]',
        '[class*="Timestamp"]',
        '[class*="timestamp"]',
        'time',
    ],
    headline=[
        '[class*="LiveBlog-subtitle"]',
        '[class*="subtitle"]',
        'h2', 'h3', 'h4',
        '[class*="headline"]',
        '[class*="title"]',
    ],
)

_YAHOO_SELECTORS = _SelectorBundle(
    post=[
        '.liveblogposts-post',
        '[class*="liveblog"] li',
        'li.js-stream-content',
        '[data-module="LiveBlogPost"]',
        'article[class*="LiveBlog"]',
        '[class*="Stream"] li',
        'div[class*="stream"] > ul > li',
        '[data-uuid]',
    ],
    timestamp=[
        'time[datetime]',
        '[data-timestamp]',
        '[class*="time"]',
        '[class*="Timestamp"]',
        'time',
    ],
    headline=[
        'h3', 'h2', 'h4',
        '[class*="headline"]',
        '[class*="title"]',
    ],
)


def _parse_css(
    soup: BeautifulSoup,
    article_url: str,
    source: str,
    bundle: _SelectorBundle,
) -> list[LiveBlogPost]:
    """
    Extrae posts usando selectores CSS.
    Prueba cada selector en orden hasta encontrar ≥2 elementos.
    Registra en el log qué selector funcionó para facilitar diagnóstico.
    """
    containers: list[Tag] = []
    matched_selector = ""

    for sel in bundle.post:
        try:
            found = soup.select(sel)
            if len(found) >= 2:
                containers = found
                matched_selector = sel
                break
        except Exception as exc:
            log.debug("[%s CSS] selector %r → error: %s", source, sel, exc)

    if not containers:
        log.warning(
            "[%s CSS] ningún selector encontró ≥2 elementos. "
            "Los selectores probablemente cambiaron — revisar: %s",
            source, bundle.post,
        )
        return []

    log.info("[%s CSS] selector %r → %d elementos", source, matched_selector, len(containers))

    posts: list[LiveBlogPost] = []

    for el in containers[:40]:
        # Saltar elementos vacíos (menús, ads)
        if len(el.get_text(strip=True)) < 25:
            continue

        # ── Timestamp ────────────────────────────────────────────────────────
        ts_raw, ts_dt = "", None
        for sel in bundle.timestamp:
            ts_el = el.select_one(sel)
            if ts_el:
                ts_raw = (
                    ts_el.get("datetime")
                    or ts_el.get("data-timestamp")
                    or ts_el.get_text(strip=True)
                    or ""
                )
                ts_dt = _parse_timestamp(ts_raw)
                if ts_dt:
                    break

        # ── Titular ──────────────────────────────────────────────────────────
        headline = ""
        for sel in bundle.headline:
            h_el = el.select_one(sel)
            if h_el:
                headline = h_el.get_text(strip=True)
                if headline:
                    break

        if not headline:
            # Último recurso: primera oración del primer párrafo
            first_p = el.find("p")
            if first_p:
                headline = first_p.get_text(strip=True)[:140]
        if not headline:
            continue

        # ── Cuerpo ───────────────────────────────────────────────────────────
        paras = _extract_paragraphs(el)
        # Eliminar párrafo duplicado con el titular
        paras = [p for p in paras if p != headline]
        # Limitar a los primeros 5 párrafos reales de ESTE post.
        # Sin límite, CNBC puede incluir contenido de posts vecinos
        # porque algunos elementos contienen referencias cruzadas.
        paras = paras[:5]
        body  = " ".join(paras)

        # ── ID ───────────────────────────────────────────────────────────────
        post_id = (
            el.get("id")
            or el.get("data-id")
            or el.get("data-post-id")
            or el.get("data-uuid")
            or ""
        )
        if not post_id:
            post_id = _make_id(source, headline, ts_raw)

        # ── URL del post ─────────────────────────────────────────────────────
        a_el     = el.find("a", href=True)
        post_url = (
            str(a_el["href"])
            if a_el and str(a_el.get("href", "")).startswith("http")
            else article_url
        )

        posts.append(LiveBlogPost(
            post_id       = str(post_id),
            source        = source,
            article_url   = post_url,
            headline      = headline,
            body          = body,
            timestamp_iso = _to_iso(ts_dt),
            timestamp_raw = ts_raw,
        ))

    log.info("[%s CSS] extraídos %d posts", source, len(posts))
    return posts


# ─────────────────────────────────────────────────────────────────────────────
# Post-procesamiento común
# ─────────────────────────────────────────────────────────────────────────────
_STOPWORDS = {
    "the","a","an","in","on","at","to","for","of","and","or","is","are","was",
    "were","with","as","by","from","that","this","it","its","be","has","have",
    "had","but","not","they","their","he","she","we","you","your","our","after",
    "over","up","down","into","out","about","says","said","amid",
}

# Patrón de tickers: solo mayúsculas/números/símbolos, <= 8 chars  (ej: ^IXIC, BTC, SPX)
_TICKER_RE = re.compile(r'^[\^A-Z0-9\-\.=]{1,8}$')


def _headline_words(text: str) -> frozenset[str]:
    """Palabras significativas del titular para comparación semántica."""
    words = {w for w in re.findall(r'\b[a-zA-Z]{3,}\b', text.lower())}
    return frozenset(words - _STOPWORDS)


def _is_duplicate(new_ws: frozenset[str], seen_sets: list[frozenset[str]]) -> bool:
    """True si el titular comparte ≥60% de palabras con alguno ya visto."""
    if not new_ws:
        return False
    for seen_ws in seen_sets:
        if not seen_ws:
            continue
        overlap = len(new_ws & seen_ws) / min(len(new_ws), len(seen_ws))
        if overlap >= 0.60:
            return True
    return False


def _sort_and_deduplicate(posts: list[LiveBlogPost]) -> list[LiveBlogPost]:
    """
    1. Descarta titulares que parecen tickers (^IXIC, BTC-USD) o muy cortos.
    2. Desduplicar por post_id (dedup exacto).
    3. Dedup semántico: elimina posts cuyo titular comparte ≥60% de palabras
       con uno ya incluido (evita el mismo post con IDs distintos).
    4. Ordenar de más reciente a más antiguo; posts sin timestamp al final.
    5. Limitar a MAX_POSTS_PER_SOURCE.
    """
    # Paso 1: filtrar titulares inválidos
    valid = [
        p for p in posts
        if len(p.headline) >= 10 and not _TICKER_RE.match(p.headline)
    ]

    # Paso 2-3: dedup exacto + semántico
    seen_ids:  set[str]             = set()
    seen_ws:   list[frozenset[str]] = []
    unique:    list[LiveBlogPost]   = []

    for p in valid:
        if p.post_id in seen_ids:
            continue
        ws = _headline_words(p.headline)
        if _is_duplicate(ws, seen_ws):
            log.debug("[dedup] descartado por similitud: %s", p.headline[:60])
            seen_ids.add(p.post_id)
            continue
        seen_ids.add(p.post_id)
        seen_ws.append(ws)
        unique.append(p)

    # Paso 4: ordenar
    with_ts    = [p for p in unique if p.timestamp_iso]
    without_ts = [p for p in unique if not p.timestamp_iso]
    with_ts.sort(key=lambda p: p.timestamp_iso, reverse=True)

    return (with_ts + without_ts)[:MAX_POSTS_PER_SOURCE]


def _retry(fn, attempts: int = RETRY_ATTEMPTS, label: str = ""):
    """Ejecuta fn hasta `attempts` veces; re-lanza la última excepción."""
    last: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last = exc
            log.warning("[retry %d/%d] %s — %s", attempt, attempts, label, exc)
    raise last


# ─────────────────────────────────────────────────────────────────────────────
# CNBC scraper
# ─────────────────────────────────────────────────────────────────────────────
_CNBC_URL_TEMPLATE = (
    "https://www.cnbc.com/{year}/{month:02d}/{day:02d}"
    "/stock-market-today-live-updates.html"
)


def _cnbc_resolve_url(page: Page) -> Optional[str]:
    """
    Prueba la URL de hoy, ayer y anteayer.
    Devuelve la primera que retorna HTTP 200.
    """
    today = datetime.now(timezone.utc)
    for delta in range(3):
        d   = today - timedelta(days=delta)
        url = _CNBC_URL_TEMPLATE.format(year=d.year, month=d.month, day=d.day)
        try:
            resp = page.goto(url, wait_until="domcontentloaded")
            if resp and resp.status == 200:
                log.info("[CNBC] URL resuelta: %s", url)
                return url
            log.debug("[CNBC] %s → HTTP %s", url, resp.status if resp else "?")
        except Exception as exc:
            log.debug("[CNBC] navegación fallida %s: %s", url, exc)
    return None


def scrape_cnbc(article_url: Optional[str] = None) -> list[LiveBlogPost]:
    """
    Extrae el live blog de CNBC.
    Estrategia:
      1. Navega con Playwright (renderiza el JS completo).
      2. Intenta __NEXT_DATA__ (Next.js SSR — más completo).
      3. Fallback a selectores CSS del DOM renderizado.
    Si article_url es None, auto-descubre la URL de hoy.
    """
    log.info("[CNBC] iniciando scrape | url=%s", article_url or "auto")

    def _run() -> list[LiveBlogPost]:
        with _browser_page() as (_, page):
            if article_url is None:
                resolved = _cnbc_resolve_url(page)
                if not resolved:
                    log.error("[CNBC] no se pudo resolver la URL del artículo de hoy")
                    return []
                url = resolved
            else:
                page.goto(article_url, wait_until="domcontentloaded")
                url = article_url

            page.wait_for_timeout(RENDER_WAIT)
            html = page.content()

        soup  = BeautifulSoup(html, "lxml")
        posts = _parse_next_data(html, url, "CNBC")

        if not posts:
            log.info("[CNBC] __NEXT_DATA__ vacío — usando selectores CSS")
            posts = _parse_css(soup, url, "CNBC", _CNBC_SELECTORS)

        return posts

    try:
        posts = _retry(_run, label="CNBC scrape")
    except Exception as exc:
        log.error("[CNBC] fallo definitivo: %s", exc)
        return []

    posts = _sort_and_deduplicate(posts)
    log.info("[CNBC] ✓ %d posts | más reciente: %s",
             len(posts), posts[0].timestamp_iso[:16] if posts else "—")
    return posts


# ─────────────────────────────────────────────────────────────────────────────
# Yahoo Finance scraper
# ─────────────────────────────────────────────────────────────────────────────
_YAHOO_HUB = "https://finance.yahoo.com/markets/live/"


def _yahoo_resolve_url(page: Page) -> Optional[str]:
    """
    Navega al hub de Yahoo Finance Markets y devuelve la URL del artículo
    de live blog más reciente.
    Criterio: el primer link que aparece en el DOM = el más nuevo.
    Yahoo también puede redirigir /markets/live/ → /markets/; los links
    siguen presentes en ambas páginas.
    """
    log.info("[Yahoo] navegando al hub: %s", _YAHOO_HUB)
    try:
        page.goto(_YAHOO_HUB, wait_until="domcontentloaded",
                  timeout=PLAYWRIGHT_TIMEOUT)
        page.wait_for_timeout(4_000)
    except Exception as exc:
        log.error("[Yahoo] hub no accesible: %s", exc)
        return None

    # Recoge todos los links a artículos stock-market-today en orden DOM
    # (DOM order = orden cronológico inverso en la página de Yahoo)
    links: list[str] = page.evaluate("""
        () => {
            const seen = new Set(), out = [];
            document.querySelectorAll('a[href]').forEach(a => {
                const h = a.href;
                if ((h.includes('/markets/live/stock-market') ||
                     /finance\\.yahoo\\.com.*stock-market-today/.test(h))
                    && !seen.has(h)) {
                    seen.add(h);
                    out.push(h);
                }
            });
            return out;
        }
    """)

    if not links:
        log.warning("[Yahoo] sin links de stock-market-today en el hub")
        return None

    log.info("[Yahoo] %d links encontrados en hub", len(links))

    # Filtrar links con fechas explícitas muy antiguas (>3 días)
    # URLs nuevas (sin fecha) siempre se mantienen → se toma la primera
    today_utc = datetime.now(timezone.utc)

    def _url_too_old(url: str) -> bool:
        m = re.search(r"-([a-z]+)-(\d{1,2})-\d{7,}", url.lower())
        if not m:
            return False   # sin fecha en URL → asumir reciente
        mon = _MONTH_NAMES.get(m.group(1), 0)
        day = int(m.group(2))
        if mon == 0:
            return False
        try:
            art_date = datetime(today_utc.year, mon, day, tzinfo=timezone.utc)
            return (today_utc - art_date).days > 3
        except ValueError:
            return False

    fresh = [u for u in links if not _url_too_old(u)]
    if not fresh:
        log.warning("[Yahoo] todos los links parecen antiguos — usando el primero")
        fresh = links

    selected = fresh[0]
    slug     = selected.split("/")[-1].split("?")[0]
    log.info("[Yahoo] artículo seleccionado: %s", slug[:80])
    return selected


def scrape_yahoo(article_url: Optional[str] = None) -> list[LiveBlogPost]:
    """
    Extrae el live blog de Yahoo Finance.
    Yahoo Finance es CSR (Client-Side Rendered) → siempre requiere Playwright.
    Estrategia:
      1. Descubre la URL del artículo de hoy desde el hub (o usa article_url).
      2. Navega al artículo y espera el renderizado completo.
      3. Intenta __NEXT_DATA__ primero.
      4. Fallback a selectores CSS del DOM renderizado.
    """
    log.info("[Yahoo] iniciando scrape | url=%s", article_url or "auto")

    def _run() -> list[LiveBlogPost]:
        with _browser_page() as (_, page):
            if article_url is None:
                url = _yahoo_resolve_url(page)
                if not url:
                    log.error("[Yahoo] no se pudo resolver la URL del artículo")
                    return []
            else:
                url = article_url

            log.info("[Yahoo] navegando al artículo")
            page.goto(url, wait_until="domcontentloaded",
                      timeout=PLAYWRIGHT_TIMEOUT)
            page.wait_for_timeout(RENDER_WAIT)
            html = page.content()

        soup  = BeautifulSoup(html, "lxml")
        posts = _parse_next_data(html, url, "Yahoo Finance")

        if not posts:
            log.info("[Yahoo] __NEXT_DATA__ vacío — usando selectores CSS")
            posts = _parse_css(soup, url, "Yahoo Finance", _YAHOO_SELECTORS)

        return posts

    try:
        posts = _retry(_run, label="Yahoo scrape")
    except Exception as exc:
        log.error("[Yahoo] fallo definitivo: %s", exc)
        return []

    posts = _sort_and_deduplicate(posts)
    log.info("[Yahoo] ✓ %d posts | más reciente: %s",
             len(posts), posts[0].timestamp_iso[:16] if posts else "—")
    return posts


# ─────────────────────────────────────────────────────────────────────────────
# API pública
# ─────────────────────────────────────────────────────────────────────────────
def scrape_all(
    cnbc_url:  Optional[str] = None,
    yahoo_url: Optional[str] = None,
) -> dict[str, list[dict]]:
    """
    Extrae ambas fuentes y devuelve:
    {
        "cnbc":  [ {...}, {...}, ... ],   # posts CNBC, más recientes primero
        "yahoo": [ {...}, {...}, ... ],   # posts Yahoo Finance, más recientes primero
    }
    Cada post es un dict con: post_id, source, article_url, headline,
    body, timestamp_iso, timestamp_raw.
    """
    return {
        "cnbc":  [p.to_dict() for p in scrape_cnbc(cnbc_url)],
        "yahoo": [p.to_dict() for p in scrape_yahoo(yahoo_url)],
    }


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="liveblog_scraper",
        description="Extractor de Live Blogs de mercados — CNBC y Yahoo Finance",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python3 liveblog_scraper.py
  python3 liveblog_scraper.py --source yahoo --output pretty
  python3 liveblog_scraper.py --source cnbc  --output json > cnbc_posts.json
  python3 liveblog_scraper.py --source cnbc --url "https://www.cnbc.com/2024/06/17/..."
  python3 liveblog_scraper.py --max 10 --no-headless
        """,
    )
    p.add_argument(
        "--source", choices=["cnbc", "yahoo", "all"], default="all",
        help="Fuente a raspar (default: all)",
    )
    p.add_argument(
        "--url",
        help="URL explícita del artículo (solo con --source cnbc o yahoo)",
    )
    p.add_argument(
        "--output", choices=["json", "pretty"], default="pretty",
        help="Formato de salida (default: pretty)",
    )
    p.add_argument(
        "--max", type=int, default=MAX_POSTS_PER_SOURCE, metavar="N",
        help=f"Posts máximos por fuente (default: {MAX_POSTS_PER_SOURCE})",
    )
    p.add_argument(
        "--headless", action=argparse.BooleanOptionalAction, default=True,
        help="Ejecutar Chromium en modo headless (default: sí)",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Logging a nivel DEBUG",
    )
    return p


def _pretty_print(results: dict[str, list[dict]]) -> None:
    sep = "─" * 70
    for source, posts in results.items():
        print(f"\n{sep}")
        print(f"  {source.upper()}  ·  {len(posts)} posts")
        print(sep)
        if not posts:
            print("  (sin posts — revisar logs para diagnóstico)")
            continue
        for i, p in enumerate(posts, 1):
            ts = p["timestamp_iso"][:16].replace("T", " ") if p["timestamp_iso"] else "—"
            print(f"  [{i:02d}] {ts}  |  {p['headline'][:72]}")
            if p["body"]:
                preview = p["body"][:130].replace("\n", " ")
                print(f"       {preview}…")
            print(f"       ID: {p['post_id']}  URL: {p['article_url'][:60]}")
            print()


def main() -> None:
    args = _build_parser().parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    global MAX_POSTS_PER_SOURCE, HEADLESS
    MAX_POSTS_PER_SOURCE = args.max
    HEADLESS             = args.headless

    results: dict[str, list[dict]] = {}

    if args.source in ("cnbc", "all"):
        cnbc_url = args.url if args.source == "cnbc" else None
        results["cnbc"] = [p.to_dict() for p in scrape_cnbc(cnbc_url)]

    if args.source in ("yahoo", "all"):
        yahoo_url = args.url if args.source == "yahoo" else None
        results["yahoo"] = [p.to_dict() for p in scrape_yahoo(yahoo_url)]

    if args.output == "json":
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        _pretty_print(results)


if __name__ == "__main__":
    main()
