"""Local-first article reading workspace. Start: uv run uvicorn app:app --reload"""
import asyncio
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / '.env')
DATA = BASE / 'data'
DATA.mkdir(exist_ok=True)
DB_FILE = DATA / 'workspace.sqlite3'

import rag_engine as rag  # noqa: E402

app = FastAPI(title='Article Insight Studio', docs_url='/api/docs')
app.mount('/static', StaticFiles(directory=str(BASE / 'static')), name='static')


def connect():
    db = sqlite3.connect(str(DB_FILE), timeout=30)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    return db


def initialize_database():
    with connect() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS articles (
                id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, title TEXT NOT NULL,
                published_time TEXT NOT NULL DEFAULT '', content TEXT NOT NULL,
                chunk_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                insights_json TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
                role TEXT NOT NULL, content TEXT NOT NULL,
                sources_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS chats_by_article ON chats(article_id, id);
        ''')

initialize_database()


class ImportBody(BaseModel):
    url: str = Field(min_length=4, max_length=2048)
    refresh: bool = False


class QuestionBody(BaseModel):
    question: str = Field(min_length=2, max_length=3000)


def normalize_public_url(input_url: str):
    """Initial SSRF guard. This project intentionally binds to localhost only.
    Third-party redirect/browser DNS rebinding is not comprehensively sandboxed.
    """
    raw = input_url.strip()
    if not raw.startswith(('http://', 'https://')):
        raw = 'https://' + raw
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise ValueError('Enter a valid http or https web address.')
        if parsed.username or parsed.password:
            raise ValueError('URLs with embedded credentials are not supported.')
        host = parsed.hostname.rstrip('.').lower()
        if host in ('localhost', 'localhost.localdomain') or host.endswith(('.local', '.internal')):
            raise ValueError('Only public websites are supported.')
        if parsed.port not in (None, 80, 443):
            raise ValueError('Only standard HTTP/HTTPS website ports are supported.')
        # Resolve all addresses, not merely the first one.
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == 'https' else 80), type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ValueError('Only public websites are supported.')
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=422, detail=f'Could not use this address: {exc}')
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), parsed.path or '/', parsed.query, ''))


def article_id(url):
    return hashlib.sha256(url.encode('utf-8')).hexdigest()[:24]


def fetch_article(article_id_value):
    with connect() as db:
        result = db.execute('SELECT * FROM articles WHERE id=?', (article_id_value,)).fetchone()
    if result is None:
        raise HTTPException(404, 'Article not found.')
    return dict(result)


def serialize(article, full=False):
    item = {k: article[k] for k in ('id', 'url', 'title', 'published_time', 'chunk_count', 'created_at', 'updated_at')}
    item['word_count'] = len(article['content'].split())
    item['reading_minutes'] = max(1, round(item['word_count'] / 225))
    item['insights'] = json.loads(article.get('insights_json') or 'null')
    if full:
        item['content'] = article['content']
    return item


@app.get('/')
async def home():
    return FileResponse(BASE / 'templates' / 'index.html')


@app.get('/api/health')
async def health():
    return {'status': 'ready', 'configured': all(os.getenv(key) and not os.getenv(key, '').startswith('replace_with_') for key in ('GOOGLE_API_KEY', 'MISTRAL_API_KEY'))}


@app.get('/api/articles')
async def list_articles():
    with connect() as db:
        rows = [dict(row) for row in db.execute('SELECT * FROM articles ORDER BY updated_at DESC')]
    return [serialize(row) for row in rows]


@app.get('/api/articles/{id}')
async def get_article(id: str):
    return serialize(fetch_article(id), full=True)


def _import_article(url, refresh):
    with connect() as db:
        old = db.execute('SELECT * FROM articles WHERE url=?', (url,)).fetchone()
    if old and not refresh:
        return serialize(dict(old), full=True)

    from universal_scraper import scrape_one
    # No browser scraping happens until after public URL validation.
    scraped = scrape_one(url)
    content = (scraped.content or '').strip()
    if len(content) < 150:
        raise RuntimeError('This page did not contain enough readable text.')
    new_id = article_id(url)
    now = datetime.now(timezone.utc).isoformat()
    record = {
        'id': new_id, 'url': url, 'title': scraped.title or 'Untitled article',
        'published_time': scraped.published_time or '', 'content': content,
    }
    # Keep the original insights if a refresh fails; reindex only when successful.
    count = rag.index_article(record)
    created = old['created_at'] if old else now
    with connect() as db:
        db.execute('''INSERT INTO articles
                (id,url,title,published_time,content,chunk_count,created_at,updated_at,insights_json)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                title=excluded.title, published_time=excluded.published_time,
                content=excluded.content, chunk_count=excluded.chunk_count,
                updated_at=excluded.updated_at, insights_json='' ''',
                (new_id, url, record['title'], record['published_time'], content, count, created, now, ''))
        if refresh:
            db.execute('DELETE FROM chats WHERE article_id=?', (new_id,))
    return serialize(fetch_article(new_id), full=True)


@app.post('/api/articles/import')
async def import_article(body: ImportBody):
    url = await asyncio.to_thread(normalize_public_url, body.url)
    # Early cache check avoids any API call for repeat visits.
    if not body.refresh:
        with connect() as db:
            existing = db.execute('SELECT * FROM articles WHERE url=?', (url,)).fetchone()
        if existing:
            return serialize(dict(existing), full=True)
    try:
        return await asyncio.to_thread(_import_article, url, body.refresh)
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(422, str(exc))
    except Exception as exc:
        # Detailed errors remain in the server log, not the public UI.
        import logging
        logging.exception('Import failed: %s', exc)
        raise HTTPException(502, 'The page could not be imported. Check API keys, website access and server logs.')


@app.post('/api/articles/{id}/insights')
async def create_insights(id: str):
    article = fetch_article(id)
    if article['insights_json']:
        return json.loads(article['insights_json'])
    try:
        result = await asyncio.to_thread(rag.generate_insights, article)
    except Exception as exc:
        import logging
        logging.exception('Brief generation failed: %s', exc)
        raise HTTPException(502, 'Could not create the reading brief. Check your API key, quota and server logs.')
    with connect() as db:
        db.execute('UPDATE articles SET insights_json=? WHERE id=?', (json.dumps(result, ensure_ascii=False), id))
    return result


@app.get('/api/articles/{id}/chat')
async def get_chats(id: str):
    fetch_article(id)
    with connect() as db:
        items = [dict(row) for row in db.execute(
            'SELECT role, content, sources_json, created_at FROM chats WHERE article_id=? ORDER BY id', (id,)
        )]
    return [{'role': x['role'], 'content': x['content'],
             'sources': json.loads(x['sources_json']), 'created_at': x['created_at']} for x in items]


@app.post('/api/articles/{id}/chat')
async def ask(id: str, body: QuestionBody):
    article = fetch_article(id)
    with connect() as db:
        history = [dict(x) for x in db.execute('SELECT role, content FROM chats WHERE article_id=? ORDER BY id DESC LIMIT 8', (id,))]
    history.reverse()
    try:
        result = await asyncio.to_thread(rag.ask_question, article, body.question, history)
    except Exception as exc:
        import logging
        logging.exception('Question failed: %s', exc)
        raise HTTPException(502, 'Could not answer this question. Check your API keys, quota and server logs.')
    now = datetime.now(timezone.utc).isoformat()
    with connect() as db:
        db.execute('INSERT INTO chats(article_id,role,content,sources_json,created_at) VALUES (?,?,?,?,?)',
                   (id, 'user', body.question, '[]', now))
        db.execute('INSERT INTO chats(article_id,role,content,sources_json,created_at) VALUES (?,?,?,?,?)',
                   (id, 'assistant', result['answer'], json.dumps(result['sources']), now))
    return result


@app.delete('/api/articles/{id}/chat')
async def clear_chat(id: str):
    fetch_article(id)
    with connect() as db:
        db.execute('DELETE FROM chats WHERE article_id=?', (id,))
    return {'ok': True}


@app.delete('/api/articles/{id}')
async def remove_article(id: str):
    fetch_article(id)
    try:
        await asyncio.to_thread(rag.delete_article_index, id)
    except Exception as exc:
        raise HTTPException(502, 'Could not remove the article index. Check the server logs.') from exc
    with connect() as db:
        db.execute('DELETE FROM articles WHERE id=?', (id,))
    return {'ok': True}


@app.get('/api/articles/{id}/export')
async def export_article(id: str):
    a = fetch_article(id)
    notes = json.loads(a['insights_json']) if a['insights_json'] else {}
    sections = [f'# {a["title"]}', f'**Source:** {a["url"]}',
                f'**Published:** {a["published_time"] or "Not provided"}', '']
    if notes:
        sections += ['## At a glance', notes.get('summary', ''), '', '## Detailed overview', notes.get('detailed_summary', ''), '',
                     '## Key points', *['- ' + point for point in notes.get('key_points', [])], '']
    sections += ['## Original article', a['content']]
    data = '\n\n'.join(sections)
    return Response(content=data, media_type='text/markdown; charset=utf-8', headers={
        'Content-Disposition': f'attachment; filename="article-{id}.md"',
        'X-Content-Type-Options': 'nosniff',
    })
