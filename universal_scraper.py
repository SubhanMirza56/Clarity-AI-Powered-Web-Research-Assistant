"""
UNIVERSAL ARTICLE SCRAPER

Scrapes ANY article URL and saves:  url, title, published_time, content

Fetch strategy (rotating, so if one client/browser is blocked the next is used):
    1. HTTPX              (random UA + random header profile)
    2. curl_cffi          (browser TLS impersonation, random UA/headers)
    3. Playwright engines, in RANDOM order each run:
         - WebKit   (Safari engine)
         - Firefox
         - Chromium / real Chrome + playwright-stealth
       Each engine gets a UA that MATCHES the engine (a Firefox engine sending
       a Chrome UA is itself a bot signal), random viewport, locale, timezone
       and a random header profile.
    4. Selenium Chrome    (if installed)
    5. CDP fallback       (ONLY if everything above failed to get the article):
         connects to a real Chrome via Chrome DevTools Protocol on port 9222
         (or any other port from CDP_PORTS). If no Chrome is listening, it
         launches one with --remote-debugging-port, opens the URL, waits
         2 seconds, then reads the page.

Extraction order: Trafilatura -> Readability -> Selectolax -> BeautifulSoup
Metadata: title and published time come from JSON-LD, OpenGraph/meta tags,
          <time datetime>, itemprop, <h1>, <title>.

Notes:
    - CAPTCHAs are NOT solved automatically. Set HEADLESS = False and the
      script opens a visible browser so YOU can solve it manually.
    - Paywalls, logins and IP bans are not bypassed.
    - Only scrape content you are allowed to; respect each site's ToS/robots.txt.

Install:
    pip install httpx[http2] curl_cffi playwright playwright-stealth trafilatura \
                readability-lxml selectolax beautifulsoup4 lxml python-dateutil selenium
    playwright install chromium firefox webkit

Usage:
    python universal_scraper.py https://example.com/article
    python universal_scraper.py url1 url2 url3
    python universal_scraper.py            (prompts for URL(s))
"""

import atexit
import csv
import json
import logging
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from readability import Document
from selectolax.lexbor import LexborHTMLParser

try:
    from dateutil import parser as dateparser
except Exception:
    dateparser = None

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_AVAILABLE = True
except Exception:
    PLAYWRIGHT_AVAILABLE = False

STEALTH_MODE = None
try:
    from playwright_stealth import Stealth  # v2.x
    STEALTH_MODE = "v2"
except Exception:
    try:
        from playwright_stealth import stealth_sync  # v1.x
        STEALTH_MODE = "v1"
    except Exception:
        STEALTH_MODE = None

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    SELENIUM_AVAILABLE = True
except Exception:
    SELENIUM_AVAILABLE = False

# Bloomberg route: bloomberg_scraper.py must sit in the same folder as this file.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import bloomberg_scraper as BBG
except Exception as _bbg_exc:
    BBG = None
    _BBG_IMPORT_ERROR = str(_bbg_exc)


# ============================================================
# SETTINGS
# ============================================================

OUTPUT_CSV = "scraped_articles.csv"
FAILED_CSV = "failed_articles.csv"

REQUEST_TIMEOUT = 20
BROWSER_TIMEOUT_MS = 30_000
MAX_CONTENT_CHARS = 300_000
MIN_CONTENT_CHARS = 150

DELAY_MIN = 1.0
DELAY_MAX = 3.0

HEADLESS = True              # False -> visible browser so you can solve CAPTCHAs
AUTO_SHOW_ON_CAPTCHA = True  # hidden browser hits CAPTCHA -> reopen visible
CAPTCHA_WAIT_SECONDS = 10

PROFILE_ROOT = Path.home() / ".universal_scraper_profiles"

# --- CDP fallback (used ONLY when the normal fetchers fail) ---
USE_CDP_FALLBACK = True
CDP_HOST = "127.0.0.1"
CDP_PORTS = [9222, 9223, 9224, 9225, 9333]  # 9222 first; any other port is used if 9222 is busy
CDP_DELAY = 2.0                             # seconds to wait after the page opens
CDP_CHROME_PATH = ""                        # optional: full path to chrome.exe / Chrome binary
CDP_PROFILE_DIR = PROFILE_ROOT / "cdp_chrome"
CDP_STARTUP_TIMEOUT = 20                    # seconds to wait for a launched Chrome to expose the port

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("SCRAPER")


# ============================================================
# USER AGENTS (grouped by engine so UA always matches the browser)
# ============================================================

UA_CHROME = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
]
UA_FIREFOX = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:142.0) Gecko/20100101 Firefox/142.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:141.0) Gecko/20100101 Firefox/141.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:143.0) Gecko/20100101 Firefox/143.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:143.0) Gecko/20100101 Firefox/143.0",
]
UA_SAFARI = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.5 Safari/605.1.15",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1",
]
ALL_UAS = UA_CHROME + UA_FIREFOX + UA_SAFARI

ACCEPT_LANGUAGES = ["en-US,en;q=0.9", "en-GB,en;q=0.9", "en-US,en;q=0.8", "en-CA,en;q=0.9,fr;q=0.5"]
REFERERS = ["https://www.google.com/", "https://www.bing.com/", "https://duckduckgo.com/", "https://t.co/", ""]
VIEWPORTS = [(1366, 768), (1440, 900), (1536, 864), (1920, 1080), (1280, 800)]
TIMEZONES = ["America/New_York", "America/Chicago", "America/Los_Angeles", "Europe/London"]
LOCALES = ["en-US", "en-GB", "en-CA"]


def _brotli_available() -> bool:
    """httpx only decodes 'br' responses if a Brotli package is installed."""
    for mod in ("brotli", "brotlicffi"):
        try:
            __import__(mod)
            return True
        except Exception:
            pass
    return False


ACCEPT_ENCODING = "gzip, deflate, br" if _brotli_available() else "gzip, deflate"


def build_headers(user_agent: str, for_browser: bool = False) -> dict:
    """Random-but-consistent header profile for the given UA."""
    headers = {
        "User-Agent": user_agent,
        "Accept": random.choice([
            "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        ]),
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Upgrade-Insecure-Requests": "1",
        "DNT": random.choice(["1", "0"]),
    }
    if not for_browser:
        # Real browsers set encoding themselves; only add it for HTTP clients.
        headers["Accept-Encoding"] = ACCEPT_ENCODING
        headers["Cache-Control"] = random.choice(["no-cache", "max-age=0"])
    ref = random.choice(REFERERS)
    if ref:
        headers["Referer"] = ref

    # Client hints only make sense for Chromium-family user agents.
    if "Chrome/" in user_agent and "Firefox" not in user_agent:
        ver = re.search(r"Chrome/(\d+)", user_agent)
        v = ver.group(1) if ver else "140"
        platform = '"Windows"' if "Windows" in user_agent else '"macOS"' if "Mac" in user_agent else '"Linux"'
        headers.update({
            "Sec-CH-UA": f'"Chromium";v="{v}", "Not=A?Brand";v="24", "Google Chrome";v="{v}"',
            "Sec-CH-UA-Mobile": "?0",
            "Sec-CH-UA-Platform": platform,
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "cross-site",
            "Sec-Fetch-User": "?1",
        })
    return headers


# ============================================================
# DATA + HELPERS
# ============================================================

@dataclass
class Article:
    url: str
    title: str = ""
    published_time: str = ""
    content: str = ""


class CaptchaRequired(RuntimeError):
    pass


CAPTCHA_MARKERS = (
    "captcha", "are you a robot", "are you a human", "verify you are human",
    "press & hold", "press and hold", "unusual activity", "access denied",
    "px-captcha", "cf-challenge", "just a moment", "enable javascript and cookies",
)


def looks_like_captcha(html: str) -> bool:
    low = html.lower()
    return len(html) < 60_000 and any(m in low for m in CAPTCHA_MARKERS)


def normalize_url(url: str) -> str:
    url = str(url).strip()
    if not url:
        raise ValueError("URL is empty.")
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        raise ValueError("Invalid URL.")
    return urlunparse(parsed._replace(fragment=""))


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = str(text).replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]*\n[ \t]*\n+", "\n\n", text)
    return text.strip()


def limit_content(text: str) -> str:
    return text if len(text) <= MAX_CONTENT_CHARS else text[:MAX_CONTENT_CHARS].rstrip()


def polite_delay(label: str):
    d = random.uniform(DELAY_MIN, DELAY_MAX)
    logger.info("Waiting %.2fs before %s...", d, label)
    time.sleep(d)


# ============================================================
# METADATA: TITLE + PUBLISHED TIME
# ============================================================

def _iter_jsonld(soup):
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text(" ", strip=True)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                yield obj
                for key in ("@graph", "mainEntity", "mainEntityOfPage"):
                    sub = obj.get(key)
                    if isinstance(sub, (list, dict)):
                        stack.extend(sub if isinstance(sub, list) else [sub])
            elif isinstance(obj, list):
                stack.extend(obj)


def normalize_date(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    try:
        if dateparser:
            dt = dateparser.parse(value)
        else:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.isoformat()
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        return value  # keep the raw string if it can't be parsed


def extract_title(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")

    for obj in _iter_jsonld(soup):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(x in ("NewsArticle", "Article", "BlogPosting", "ReportageNewsArticle") for x in types):
            h = obj.get("headline") or obj.get("name")
            if isinstance(h, str) and h.strip():
                return clean_text(h)

    for key in ("og:title", "twitter:title", "title"):
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        if tag and tag.get("content", "").strip():
            return clean_text(tag["content"])

    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        return clean_text(h1.get_text(" ", strip=True))

    if soup.title and soup.title.string:
        return clean_text(soup.title.string)
    return ""


def extract_published_time(raw_html: str) -> str:
    soup = BeautifulSoup(raw_html, "html.parser")

    for obj in _iter_jsonld(soup):
        for key in ("datePublished", "dateCreated", "uploadDate"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                return normalize_date(v)

    meta_keys = [
        "article:published_time", "og:article:published_time", "datePublished",
        "date", "pubdate", "publishdate", "publish-date", "publication_date",
        "parsely-pub-date", "sailthru.date", "dc.date", "dc.date.issued",
        "article.published", "cXenseParse:recs:publishtime", "og:updated_time",
    ]
    lowered = {k.lower() for k in meta_keys}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower().strip()
        val = (tag.get("content") or "").strip()
        if key in lowered and val:
            return normalize_date(val)

    node = soup.find(attrs={"itemprop": "datePublished"})
    if node:
        return normalize_date(node.get("content") or node.get("datetime") or node.get_text(strip=True))

    scope = soup.find("article") or soup
    t = scope.find("time", attrs={"datetime": True}) or soup.find("time", attrs={"datetime": True})
    if t:
        return normalize_date(t["datetime"])
    t = scope.find("time") or soup.find("time")
    if t and t.get_text(strip=True):
        return normalize_date(t.get_text(" ", strip=True))

    return ""


# ============================================================
# CONTENT EXTRACTORS
# ============================================================

BODY_SELECTORS = [
    "article", "main", "[itemprop='articleBody']", ".article-body", ".article-content",
    ".article__body", ".story-body", ".story-content", ".entry-content", ".post-content",
]
JUNK_TAGS = ["script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form", "iframe"]


def extract_trafilatura(raw_html: str, url: str) -> str:
    try:
        return clean_text(trafilatura.extract(
            raw_html, url=url, include_comments=False, include_tables=True,
            include_links=False, favor_precision=True, output_format="txt") or "")
    except Exception as exc:
        logger.debug("Trafilatura failed: %s", exc)
        return ""


def extract_readability(raw_html: str) -> str:
    try:
        soup = BeautifulSoup(Document(raw_html).summary(), "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        return clean_text(soup.get_text("\n", strip=True))
    except Exception as exc:
        logger.debug("Readability failed: %s", exc)
        return ""


def extract_selectolax(raw_html: str) -> str:
    try:
        tree = LexborHTMLParser(raw_html)
        for sel in JUNK_TAGS:
            for node in tree.css(sel):
                node.decompose()
        best = ""
        for sel in BODY_SELECTORS:
            node = tree.css_first(sel)
            if node:
                text = clean_text(node.text(separator="\n"))
                if len(text) > len(best):
                    best = text
        if best:
            return best
        if tree.body:
            return clean_text(tree.body.text(separator="\n"))
    except Exception as exc:
        logger.debug("Selectolax failed: %s", exc)
    return ""


def extract_beautifulsoup(raw_html: str) -> str:
    try:
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup(JUNK_TAGS):
            tag.decompose()
        cands = [clean_text(n.get_text("\n", strip=True)) for s in BODY_SELECTORS for n in soup.select(s)]
        if not cands and soup.body:
            cands.append(clean_text(soup.body.get_text("\n", strip=True)))
        return max(cands, key=len) if cands else ""
    except Exception as exc:
        logger.debug("BeautifulSoup failed: %s", exc)
        return ""


def extract_content(raw_html: str, url: str):
    methods = [
        ("Trafilatura", lambda: extract_trafilatura(raw_html, url)),
        ("Readability", lambda: extract_readability(raw_html)),
        ("Selectolax", lambda: extract_selectolax(raw_html)),
        ("BeautifulSoup", lambda: extract_beautifulsoup(raw_html)),
    ]
    best, best_name = "", ""
    for name, fn in methods:
        text = fn()
        if len(text) > len(best):
            best, best_name = text, name
        if len(text) >= MIN_CONTENT_CHARS:
            return limit_content(text), name
    return limit_content(best), best_name


# ============================================================
# FETCHERS - HTTP CLIENTS
# ============================================================

def _check_html(content_type: str):
    ct = content_type.lower()
    if "text/html" not in ct and "application/xhtml+xml" not in ct:
        raise RuntimeError(f"Not an HTML page: {ct}")


def fetch_httpx(url: str):
    ua = random.choice(ALL_UAS)
    with httpx.Client(timeout=REQUEST_TIMEOUT, follow_redirects=True,
                      headers=build_headers(ua), http2=True) as client:
        r = client.get(url)
        r.raise_for_status()
        _check_html(r.headers.get("content-type", ""))
        return r.text, f"HTTPX | {ua[-40:]}"


def fetch_curl_cffi(url: str):
    ua = random.choice(UA_CHROME)  # impersonate="chrome" -> keep UA Chrome-family
    impersonate = random.choice(["chrome", "chrome120", "chrome124", "edge101"])
    r = curl_requests.get(url, headers=build_headers(ua), timeout=REQUEST_TIMEOUT,
                          allow_redirects=True, impersonate=impersonate)
    r.raise_for_status()
    _check_html(r.headers.get("content-type", ""))
    return r.text, f"curl_cffi ({impersonate})"


# ============================================================
# FETCHERS - PLAYWRIGHT (rotating engines)
# ============================================================

def apply_stealth(context, page):
    if STEALTH_MODE == "v2":
        Stealth().apply_stealth_sync(context)
    elif STEALTH_MODE == "v1":
        stealth_sync(page)
    else:
        logger.warning("playwright-stealth not installed: pip install playwright-stealth")


def _wait_for_manual_captcha(page, engine: str):
    logger.warning("Solve the CAPTCHA in the %s window (waiting up to %ds)...",
                   engine, CAPTCHA_WAIT_SECONDS)
    deadline = time.time() + CAPTCHA_WAIT_SECONDS
    while time.time() < deadline:
        page.wait_for_timeout(2000)
        html = page.content()
        if not looks_like_captcha(html):
            page.wait_for_timeout(1500)
            return page.content()
    raise RuntimeError("CAPTCHA was not solved in time.")


def fetch_playwright_engine(url: str, engine: str, headless: bool = True):
    """
    engine: 'webkit' | 'firefox' | 'chromium'
    Fresh context each time with a UA that matches the engine, random
    viewport/locale/timezone/headers. Cookies persist per engine.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright is not installed.")

    ua = random.choice({"webkit": UA_SAFARI, "firefox": UA_FIREFOX, "chromium": UA_CHROME}[engine])
    w, h = random.choice(VIEWPORTS)
    if "iPhone" in ua:
        w, h = 390, 844
    storage_dir = PROFILE_ROOT / engine
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = storage_dir / "state.json"

    ctx_args = dict(
        user_agent=ua,
        locale=random.choice(LOCALES),
        timezone_id=random.choice(TIMEZONES),
        viewport={"width": w, "height": h},
        extra_http_headers={k: v for k, v in build_headers(ua, for_browser=True).items()
                            if k.lower() != "user-agent"},
    )
    if storage_path.exists():
        ctx_args["storage_state"] = str(storage_path)

    with sync_playwright() as p:
        if engine == "webkit":
            browser = p.webkit.launch(headless=headless)
        elif engine == "firefox":
            browser = p.firefox.launch(headless=headless)
        else:
            launch_kwargs = dict(headless=headless, args=["--disable-blink-features=AutomationControlled"])
            try:
                browser = p.chromium.launch(channel="chrome", **launch_kwargs)  # real Chrome
            except Exception:
                browser = p.chromium.launch(**launch_kwargs)                     # bundled Chromium

        context = browser.new_context(**ctx_args)
        page = context.new_page()
        if engine == "chromium":
            apply_stealth(context, page)

        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            page.wait_for_timeout(random.randint(1200, 2500))
            # Scroll a little so lazy-loaded body text renders.
            try:
                page.mouse.wheel(0, random.randint(400, 1200))
                page.wait_for_timeout(random.randint(300, 900))
            except Exception:
                pass

            status = resp.status if resp else 0
            html = page.content()

            if status in (401, 403, 429, 503) or looks_like_captcha(html):
                if headless:
                    raise CaptchaRequired(f"Blocked/verification page (HTTP {status}) in hidden {engine}.")
                html = _wait_for_manual_captcha(page, engine)

            try:
                context.storage_state(path=str(storage_path))
            except Exception:
                pass
            return html, f"Playwright {engine} ({'hidden' if headless else 'visible'}) | {ua[-35:]}"
        finally:
            context.close()
            browser.close()


def make_engine_fetcher(engine: str):
    def _fetch(url: str):
        try:
            return fetch_playwright_engine(url, engine, headless=HEADLESS)
        except CaptchaRequired:
            if not AUTO_SHOW_ON_CAPTCHA or not HEADLESS:
                raise
            logger.warning("Verification page in hidden %s -> opening visible window for manual solve.", engine)
            return fetch_playwright_engine(url, engine, headless=False)
    return _fetch


# ============================================================
# FETCHERS - CDP (Chrome DevTools Protocol) - FALLBACK ONLY
# ============================================================

_CDP_PROC = None


def _cdp_alive(port: int) -> bool:
    """True if a Chrome/Edge debugging endpoint is answering on this port."""
    try:
        r = httpx.get(f"http://{CDP_HOST}:{port}/json/version", timeout=1.5)
        return r.status_code == 200 and "webSocketDebuggerUrl" in r.json()
    except Exception:
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((CDP_HOST, port)) != 0


def _find_chrome():
    if CDP_CHROME_PATH and Path(CDP_CHROME_PATH).exists():
        return CDP_CHROME_PATH
    candidates = []
    if sys.platform.startswith("win"):
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
        for env in ("PROGRAMFILES", "PROGRAMFILES(X86)"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"))
    for name in ("google-chrome", "google-chrome-stable", "chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))
    for c in candidates:
        if c.exists():
            return str(c)
    return None


def _stop_cdp_chrome():
    global _CDP_PROC
    if _CDP_PROC and _CDP_PROC.poll() is None:
        try:
            _CDP_PROC.terminate()
        except Exception:
            pass
    _CDP_PROC = None


atexit.register(_stop_cdp_chrome)


def _ensure_cdp_browser() -> int:
    """Return a port with a live CDP browser. Reuse one if running, else launch Chrome."""
    global _CDP_PROC

    for port in CDP_PORTS:  # 9222 first, then the other ports
        if _cdp_alive(port):
            logger.info("CDP: using Chrome already running on port %d", port)
            return port

    chrome = _find_chrome()
    if not chrome:
        raise RuntimeError("CDP: Chrome not found. Install Chrome or set CDP_CHROME_PATH.")

    port = next((p for p in CDP_PORTS if _port_free(p)), None)
    if port is None:
        raise RuntimeError(f"CDP: no free port in {CDP_PORTS}.")

    CDP_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={CDP_PROFILE_DIR}",  # required by Chrome 136+ for remote debugging
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    logger.info("CDP: launching Chrome on port %d", port)
    _CDP_PROC = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    deadline = time.time() + CDP_STARTUP_TIMEOUT
    while time.time() < deadline:
        if _cdp_alive(port):
            return port
        time.sleep(0.5)
    raise RuntimeError(f"CDP: Chrome did not expose port {port} within {CDP_STARTUP_TIMEOUT}s.")


def fetch_cdp(url: str):
    """
    Open the URL in a real (visible) Chrome through CDP, wait CDP_DELAY seconds,
    then read the rendered HTML. If a CAPTCHA appears you can solve it by hand
    in that Chrome window.
    """
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("Playwright is not installed (needed for CDP).")

    port = _ensure_cdp_browser()
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(f"http://{CDP_HOST}:{port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=BROWSER_TIMEOUT_MS)
            logger.info("CDP: page opened on port %d, waiting %.1fs...", port, CDP_DELAY)
            page.wait_for_timeout(int(CDP_DELAY * 1000))  # the 2 second delay
            try:
                page.mouse.wheel(0, random.randint(400, 1200))
                page.wait_for_timeout(random.randint(300, 900))
            except Exception:
                pass

            status = resp.status if resp else 0
            html = page.content()
            if status in (401, 403, 429, 503) or looks_like_captcha(html):
                html = _wait_for_manual_captcha(page, f"CDP Chrome:{port}")
            return html, f"CDP Chrome (port {port})"
        finally:
            page.close()  # close only our tab; the browser itself stays up


# ============================================================
# FETCHERS - SELENIUM
# ============================================================

def fetch_selenium(url: str):
    if not SELENIUM_AVAILABLE:
        raise RuntimeError("Selenium is not installed.")
    ua = random.choice(UA_CHROME)
    opts = ChromeOptions()
    opts.add_argument("--headless=new")
    opts.add_argument("--disable-gpu")
    opts.add_argument(f"--window-size={random.choice(VIEWPORTS)[0]},{random.choice(VIEWPORTS)[1]}")
    opts.add_argument(f"--user-agent={ua}")
    opts.add_argument("--lang=en-US")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    driver = webdriver.Chrome(options=opts)
    try:
        driver.set_page_load_timeout(BROWSER_TIMEOUT_MS / 1000)
        driver.get(url)
        time.sleep(random.uniform(1.0, 2.0))
        html = driver.page_source
        if looks_like_captcha(html):
            raise RuntimeError("Verification page shown in Selenium.")
        return html, "Selenium Chrome"
    finally:
        driver.quit()


# ============================================================
# QUALITY CHECK
# ============================================================

def quality_score(content: str, title: str) -> int:
    score = 0
    if len(content) >= 150:
        score += 40
    if len(content) >= 500:
        score += 20
    if len(content) >= 1500:
        score += 15
    if title:
        score += 10
    if len(re.findall(r"\b(the|and|of|to|in|for|that|with)\b", content.lower())) >= 5:
        score += 15
    return min(score, 100)


def looks_like_article(content: str, title: str) -> bool:
    return quality_score(content, title) >= 50 and len(content) >= MIN_CONTENT_CHARS


# ============================================================
# BUILD FETCHER CHAIN
# ============================================================

def build_fetcher_chain():
    chain = [("HTTPX", fetch_httpx), ("curl_cffi", fetch_curl_cffi)]

    if PLAYWRIGHT_AVAILABLE:
        engines = ["webkit", "firefox", "chromium"]
        random.shuffle(engines)  # rotate which browser goes first on every run
        for e in engines:
            label = "Playwright Chromium+Stealth" if e == "chromium" else f"Playwright {e.capitalize()}"
            chain.append((label, make_engine_fetcher(e)))

    if SELENIUM_AVAILABLE:
        chain.append(("Selenium", fetch_selenium))
    return chain


# ============================================================
# SCRAPE ONE URL
# ============================================================

def looks_like_html(raw_html: str) -> bool:
    """Reject undecoded/binary bodies (compressed bytes, garbage) before parsing."""
    if not raw_html or len(raw_html) < 200:
        return False
    sample = raw_html[:20000]
    bad = sum(1 for ch in sample if ch == "\ufffd" or (ord(ch) < 32 and ch not in "\t\n\r"))
    if bad / len(sample) > 0.01:
        return False
    low = sample.lower()
    return any(tag in low for tag in ("<html", "<!doctype", "<body", "<head", "<article", "<main"))


def analyze_html(raw_html: str, url: str, method: str):
    """Extract title / date / content from HTML and score it."""
    if not looks_like_html(raw_html):
        raise RuntimeError("Response is not readable HTML (undecoded or binary body).")
    if looks_like_captcha(raw_html):
        raise RuntimeError("Verification/blocked page returned.")
    title = extract_title(raw_html)
    published = extract_published_time(raw_html)
    content, ext = extract_content(raw_html, url)
    score = quality_score(content, title)
    logger.info("%s -> %s | title=%s | published=%s | %d chars | score=%d",
                method, ext or "none", "yes" if title else "no",
                published or "n/a", len(content), score)
    return Article(url=url, title=title, published_time=published, content=content), score


def scrape_one(url: str) -> Article:
    started = time.perf_counter()
    url = normalize_url(url)
    logger.info("URL: %s", url)

    chain = build_fetcher_chain()
    logger.info("Fetcher chain: %s", " -> ".join(n for n, _ in chain))

    best = Article(url=url)
    best_score = -1

    def consider(art: Article, score: int):
        nonlocal best, best_score
        if score > best_score:
            best, best_score = art, score
        elif score == best_score and not best.published_time and art.published_time:
            best.published_time = art.published_time

    for i, (name, fetcher) in enumerate(chain):
        if i > 0:
            polite_delay(name)
        logger.info("Trying: %s", name)
        try:
            raw_html, method = fetcher(url)
            art, score = analyze_html(raw_html, url, method)
            consider(art, score)
            if looks_like_article(art.content, art.title):
                break
        except Exception as exc:
            logger.warning("%s failed: %s", name, exc)

    # CDP is a LAST RESORT: only runs if the normal fetchers did not get the article.
    if USE_CDP_FALLBACK and not looks_like_article(best.content, best.title):
        logger.warning("Normal fetchers failed -> trying CDP fallback")
        logger.info("Waiting %.1fs before CDP...", CDP_DELAY)
        time.sleep(CDP_DELAY)
        try:
            raw_html, method = fetch_cdp(url)
            art, score = analyze_html(raw_html, url, method)
            consider(art, score)
        except Exception as exc:
            logger.warning("CDP failed: %s", exc)

    if len(best.content) < MIN_CONTENT_CHARS:
        raise RuntimeError("No usable article content could be extracted "
                           "(blocked, paywalled, or needs manual CAPTCHA).")

    best.content = limit_content(clean_text(best.content))
    logger.info("SUCCESS | %d chars | %.2fs", len(best.content), time.perf_counter() - started)
    return best


# ============================================================
# CSV OUTPUT
# ============================================================

def _append_row(path: str, fieldnames, row: dict):
    p = Path(path)
    exists = p.exists() and p.stat().st_size > 0
    with open(p, "a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        w.writerow(row)


# One schema for every site. Generic articles fill the core columns; Bloomberg
# articles also fill author/section/summary/tags/AI takeaways.
SCRAPE_FIELDS = ["url", "title", "published_time", "content"]


def _ensure_csv_header(path: str, fieldnames):
    """If an existing CSV has a different header, move it aside so columns never misalign."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return
    with open(p, newline="", encoding="utf-8-sig") as f:
        existing = next(csv.reader(f), [])
    if existing != list(fieldnames):
        backup = p.with_name(p.stem + ".old" + p.suffix)
        p.replace(backup)
        logger.warning("CSV header changed; previous file moved to %s", backup)


def save_row(row: dict, path: str = None):
    path = path or OUTPUT_CSV
    row = dict(row)
    row.setdefault("scraped_at", datetime.now(timezone.utc).isoformat())
    _ensure_csv_header(path, SCRAPE_FIELDS)
    _append_row(path, SCRAPE_FIELDS, {k: row.get(k, "") for k in SCRAPE_FIELDS})
    logger.info("Saved to %s", Path(path).resolve())


def article_row(a: Article) -> dict:
    return {"url": a.url, "title": a.title, "published_time": a.published_time,
            "content": a.content, "method": getattr(a, "method", "")}


def bloomberg_row(a) -> dict:
    """Map a bloomberg_scraper.Article onto the shared schema."""
    return {"url": a.url, "title": a.headline, "published_time": a.published_time,
            "updated_time": a.updated_time, "author": a.author, "section": a.section,
            "summary": a.summary, "tags": a.tags, "ai_takeaways": a.ai_takeaways,
            "content": a.content, "method": a.method}


def save_article(a: Article):
    save_row(article_row(a))


def load_done_urls(path: str = None) -> set:
    path = path or OUTPUT_CSV
    p = Path(path)
    if not p.exists():
        return set()
    with open(p, newline="", encoding="utf-8-sig") as f:
        return {row["url"] for row in csv.DictReader(f) if row.get("url")}


def save_failure(url: str, error: str):
    _append_row(FAILED_CSV, ["url", "error", "failed_at"], {
        "url": url, "error": error, "failed_at": datetime.now(timezone.utc).isoformat(),
    })


# ============================================================
# MAIN
# ============================================================

# ============================================================
# BLOOMBERG ROUTE (bloomberg.com URLs are handled by bloomberg_scraper.py)
# ============================================================

def is_bloomberg_url(url: str) -> bool:
    if BBG is None:
        return False
    try:
        return BBG.is_bloomberg(BBG.normalize_url(url))
    except Exception:
        return False


def run_bloomberg(url: str):
    """
    Run the Bloomberg scraper for one bloomberg.com URL.
    Article URL   -> scrape that article.
    Listing URL   -> discover article links, then scrape each one.
    Results go to the shared CSV (scraped_articles.csv). Returns (saved, failed).
    """
    # Share this script's runtime settings with the Bloomberg module
    BBG.HEADLESS = HEADLESS
    BBG.USE_CDP_FALLBACK = USE_CDP_FALLBACK

    url = BBG.normalize_url(url)
    if BBG.is_article_url(url):
        targets = [url]
    else:
        print(f"  Bloomberg listing page -> discovering up to {BBG.DEFAULT_LISTING_MAX} articles")
        targets = BBG.discover_from_listing(url, BBG.DEFAULT_LISTING_MAX)
        if not targets:
            save_failure(url, "No article links found on listing page.")
            print("  No article links found on this page.")
            return 0, 1

    done = load_done_urls(OUTPUT_CSV)
    saved = failed = 0
    for i, art_url in enumerate(targets, 1):
        if art_url in done:
            print(f"  [{i}/{len(targets)}] already saved, skipping: {art_url}")
            continue
        print(f"  [{i}/{len(targets)}] {art_url}")
        try:
            art = BBG.scrape_article(art_url)
            save_row(bloomberg_row(art))
            print(f"      Headline:  {art.headline[:90]}")
            print(f"      Published: {art.published_time or 'not found'}")
            print(f"      Content:   {len(art.content):,} chars")
            saved += 1
        except Exception as exc:
            save_failure(art_url, str(exc))
            print(f"      FAILED: {exc}")
            failed += 1
        if i < len(targets):
            BBG.polite_delay("next Bloomberg article")

    print(f"  CSV: {Path(OUTPUT_CSV).resolve()}")
    return saved, failed


def main():
    print("=" * 70)
    print("            UNIVERSAL ARTICLE SCRAPER")
    print("=" * 70)
    print("Output: url | title | published_time | content")
    print(f"  Playwright: {'yes' if PLAYWRIGHT_AVAILABLE else 'NO'} | "
          f"Stealth: {STEALTH_MODE or 'NO'} | Selenium: {'yes' if SELENIUM_AVAILABLE else 'NO'} | "
          f"CDP fallback: {'yes' if USE_CDP_FALLBACK else 'NO'}")
    print("=" * 70)

    urls = sys.argv[1:]
    if not urls:
        raw = input("Paste article URL(s) (space or comma separated): ").strip()
        urls = [u for u in re.split(r"[\s,]+", raw) if u]
    if not urls:
        print("No URL entered.")
        return

    for n, url in enumerate(urls, 1):
        print(f"\n[{n}/{len(urls)}] {url}")
        try:
            if is_bloomberg_url(url):
                print("  Bloomberg URL detected -> running bloomberg_scraper")
                saved, failed = run_bloomberg(url)
                print(f"  Bloomberg done: {saved} saved, {failed} failed")
            else:
                if "bloomberg.com" in url.lower() and BBG is None:
                    print(f"  WARNING: bloomberg_scraper.py not loaded ({_BBG_IMPORT_ERROR}); "
                          "using the generic scraper instead.")
                art = scrape_one(url)
                save_article(art)
                print(f"  Title:     {art.title[:90]}")
                print(f"  Published: {art.published_time or 'not found'}")
                print(f"  Content:   {len(art.content):,} chars")
        except Exception as exc:
            save_failure(url, str(exc))
            print(f"  FAILED: {exc}")
        if n < len(urls):
            polite_delay("next URL")

    print(f"\nCSV: {Path(OUTPUT_CSV).resolve()}")


if __name__ == "__main__":
    main()