# Clarity — AI Web Reading Studio

A local-first, polished article reading application built with **FastAPI, LangChain, ChromaDB, Mistral embeddings, Gemini, and your universal website scraper**. The website deliberately hides technical provider/model names while keeping configurable details in the backend.

## What it can do

- Import article pages by URL using your exact original multi-fallback scraper, including its CDP last resort.
- Save the URL, title and publication time as source metadata. Only extracted **article content** becomes searchable vector text.
- Save an indexed reading library, and reopen old articles without rescraping or repaying for embeddings.
- Create brief and detailed summaries, numbered key ideas, main topics, and suggested questions.
- Ask questions using retrieval confined to the selected article, with numbered reference passages that can be expanded to inspect the evidence.
- Retain chat per article, browse the full source content, refresh imports, export Markdown and delete saved items.
- Display an approachable, neumorphic reading interface. No model names, vector jargon or other implementation details appear on the website.

## Windows installation (recommended Python 3.12)

Open a terminal in this folder:

```powershell
uv python install 3.12
uv venv --python 3.12
uv pip install -r requirements.txt
uv run --python 3.12 --with-requirements requirements.txt playwright install chromium firefox webkit
```

If the browser installation needs elevated privileges, rerun the last command in a terminal with the required permissions. You can skip browser installation only if basic HTTP extraction handles your pages; the scraper's Playwright/CDP fallbacks need it.

Make a configuration file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` to supply real `MISTRAL_API_KEY` and `GOOGLE_API_KEY` keys. `GEMINI_MODEL` is a server setting and can be changed if the configured account does not support the default. Both services may require billing/quotas; this app does not run models locally.

Start the website:

```powershell
uv run --python 3.12 --with-requirements requirements.txt uvicorn app:app --host 127.0.0.1 --port 8000 --reload
```

Open **http://127.0.0.1:8000**. For later launches you can double-click `start_windows.bat` after setup.

**Important:** Your earlier Python 3.15 error was from libraries without Python 3.15 wheels. The included `.python-version` requests Python 3.12 and the commands explicitly use it.

## Directory layout

```text
app.py                 FastAPI API and SQLite article / chat library
rag_engine.py          LangChain indexing, retrieval, and reading-brief generation
universal_scraper.py   Your supplied scraper, kept intact (only renamed)
templates/index.html   Website layout
static/styles.css      Neumorphic visual design
static/app.js          Interactive reading, library and chat
.env.example           Example API credentials and model config
requirements.txt       Python dependencies
start_windows.bat      Windows launcher
data/                  Created automatically: SQLite DB and persistent Chroma index
```

## How the article pipeline works

```text
Public article URL
   ↓
Your scraper (HTTP client → browser fallbacks → CDP last resort)
   ↓
{url, title, published_time, content}
   ├─ title / published_time / url → each chunk's Chroma metadata
   ├─ raw full content → SQLite library for display
   └─ content → text splitter (1,250 characters, 180 overlap)
           → Mistral text embeddings → persistent ChromaDB
                       ↓
 User question → embed query → search only selected article → top 6 chunks
                       ↓
 Gemini via LangChain, with article-only instruction and numbered citations
                       ↓
 Answer and inspectable source excerpts
```

The reading brief uses a **map-and-reduce** style: it first collects notes across all sections of long articles, then synthesizes a summary and important points. This uses additional API calls for very long pages.

## Costs and caveats

- **Credentials:** Never place API keys in `static/`, and never commit `.env` to Git.
- **Websites:** Not every website is extractable. Paywalls, logins, blocks, PDFs, video/audio or anti-bot challenges can prevent access. CAPTCHA is not solved automatically. Your original scraper may launch a visible Chrome window if a fallback is required.
- **Publication dates:** Some pages have no reliable publication timestamp; this is displayed as not provided rather than fabricated.
- **Evidence:** Answers cite retrieved article sections. Generated summaries and claims can still contain errors; important conclusions should be verified against the original article.
- **Scope:** The app is intended for personal, trusted local use on `127.0.0.1`. An initial check rejects private/local URL targets, but **redirect chains, DNS rebinding, browser fetches and local scraper behavior are not comprehensively sandboxed**. Do not deploy this as a public URL-fetching service without additional outbound-network isolation, redirect enforcement, authentication, quotas and other abuse protections.
- **Quota:** Re-importing the same URL uses saved data by default. Refreshing an article creates new embeddings and discards its previous brief and conversation.
- **Storage:** Indexed article content and conversations persist locally in `data/`. The text used for embedding and analysis is sent to Mistral/Google APIs respectively.

## Troubleshooting

- **Missing keys:** Check `.env` is beside `app.py`. Restart the server after changing it.
- **Gemini 404:** Edit `GEMINI_MODEL` in `.env` to a model available to your Google account; use official model listings. Default here is `gemini-3.5-flash-lite`.
- **Scraping fails:** Check whether the site is publicly readable, allow manual browser verification if needed (`HEADLESS = False` in `universal_scraper.py`), and inspect the uvicorn console logs. A special `bloomberg_scraper.py` can optionally be placed beside your script, but normal URL import uses `scrape_one()`.
- **Slow import:** The scraper can try many fetch methods and the first vector import may create many paid embeddings. Refreshing a long article takes longer than opening a saved one.
- **Out of memory:** Browser fallbacks can use substantial RAM. Close browser tabs and reduce the number of browsers installed if memory is limited.

API documentation is available at **http://127.0.0.1:8000/api/docs**.
