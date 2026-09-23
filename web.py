"""Webseite zum Durchsuchen der gesammelten Telegram-Nachrichten."""

import os
import re
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import quote, urlencode

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse

from db import SEPARATOR_PY, Database, normalize_term

load_dotenv()

db = Database(os.environ["DATABASE_URL"])

PAGE_SIZE = 60
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.open()
    yield
    await db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)

PAGE = """<!doctype html>
<html lang="de">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Telegram-Suche</title>
<link rel="icon" href="/favicon.svg" type="image/svg+xml">
<link rel="icon" href="/favicon.ico" sizes="48x48">
<link rel="apple-touch-icon" href="/apple-touch-icon.png">
<style>
  :root {{
    --bg:#f3f4f7; --surface:#ffffff; --surface-2:#f7f8fa; --text:#15171a; --muted:#6b7280;
    --border:#e5e7eb; --accent:#2481cc; --accent-hover:#1c6fb0; --accent-soft:#e6f1fa;
    --mark:#ffe58a; --shadow:0 1px 2px rgba(16,24,40,.05), 0 1px 3px rgba(16,24,40,.08);
    --shadow-hover:0 6px 20px rgba(16,24,40,.12);
    --fresh:#15803d; --fresh-soft:#dcfce7; --old:#6b7280; --old-soft:#f1f2f4;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg:#0f1115; --surface:#181b21; --surface-2:#1f232a; --text:#e8eaed; --muted:#9aa0a6;
      --border:#2a2f37; --accent:#5eaee8; --accent-hover:#7fc0ef; --accent-soft:#1b2c3b;
      --mark:#6b5a00; --shadow:0 1px 2px rgba(0,0,0,.4); --shadow-hover:0 6px 20px rgba(0,0,0,.5);
      --fresh:#4ade80; --fresh-soft:#14301f; --old:#9aa0a6; --old-soft:#23272e;
    }}
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         -webkit-font-smoothing:antialiased; }}

  header {{ position:sticky; top:0; z-index:10; background:color-mix(in srgb, var(--bg) 85%, transparent);
           backdrop-filter:blur(12px); -webkit-backdrop-filter:blur(12px);
           border-bottom:1px solid var(--border); }}
  .bar {{ max-width:1200px; margin:0 auto; padding:14px 16px; display:flex; gap:16px; align-items:center; }}
  .brand {{ display:flex; align-items:center; gap:10px; font-weight:700; font-size:17px;
           color:var(--text); text-decoration:none; white-space:nowrap; }}
  .brand svg {{ width:28px; height:28px; color:var(--accent); flex:none; }}
  form {{ flex:1; display:flex; }}
  .search {{ position:relative; flex:1; max-width:640px; }}
  .search svg {{ position:absolute; left:12px; top:50%; width:18px; height:18px;
                transform:translateY(-50%); color:var(--muted); pointer-events:none; }}
  .search input {{ width:100%; padding:11px 40px 11px 40px; font-size:16px; color:var(--text);
                  background:var(--surface); border:1px solid var(--border); border-radius:12px;
                  outline:none; transition:border-color .15s, box-shadow .15s; }}
  .search input:focus {{ border-color:var(--accent); box-shadow:0 0 0 4px var(--accent-soft); }}
  .search input::-webkit-search-cancel-button {{ display:none; }}
  .clear {{ position:absolute; right:8px; top:50%; transform:translateY(-50%); width:26px; height:26px;
           display:grid; place-items:center; border-radius:50%; color:var(--muted);
           text-decoration:none; font-size:18px; line-height:1; }}
  .clear:hover {{ background:var(--surface-2); color:var(--text); }}

  main {{ max-width:1200px; margin:0 auto; padding:20px 16px 64px; }}
  .summary {{ color:var(--muted); margin:0 0 16px; font-size:14px; }}
  .summary strong {{ color:var(--text); }}

  .grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(300px, 1fr)); gap:16px; }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:14px;
          box-shadow:var(--shadow); overflow:hidden; display:flex; flex-direction:column;
          transition:box-shadow .2s, transform .2s; }}
  .card:hover {{ box-shadow:var(--shadow-hover); transform:translateY(-2px); }}
  .media {{ display:block; aspect-ratio:4/3; background:var(--surface-2); position:relative;
           overflow:hidden; border-bottom:1px solid var(--border); }}
  .media img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:contain; }}
  .body {{ padding:14px 16px 16px; display:flex; flex-direction:column; gap:8px; flex:1; }}
  .top {{ display:flex; align-items:flex-start; justify-content:space-between; gap:8px; }}
  .source {{ min-width:0; font-size:12.5px; color:var(--muted); line-height:1.35; }}
  .source strong {{ display:block; color:var(--text); font-size:13px; font-weight:600;
                   white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
  .age {{ flex:none; font-size:12px; font-weight:600; padding:3px 9px; border-radius:999px;
         background:var(--old-soft); color:var(--old); white-space:nowrap; }}
  .age.fresh {{ background:var(--fresh-soft); color:var(--fresh); }}
  .title {{ font-weight:650; font-size:15.5px; line-height:1.35; margin:2px 0 0;
           word-break:break-word; }}
  .text {{ white-space:pre-wrap; word-break:break-word; color:var(--text); opacity:.85; font-size:14px;
          display:-webkit-box; -webkit-line-clamp:6; -webkit-box-orient:vertical; overflow:hidden; }}
  .text.open {{ display:block; }}
  .more {{ align-self:flex-start; background:none; border:0; padding:0; color:var(--accent);
          font-size:13px; cursor:pointer; }}
  mark {{ background:var(--mark); color:inherit; border-radius:3px; padding:0 1px; }}
  .actions {{ display:flex; gap:8px; margin-top:auto; padding-top:8px; }}
  .btn {{ display:inline-flex; white-space:nowrap; align-items:center; justify-content:center; gap:6px;
         padding:9px 12px; border-radius:10px; font-size:14px; font-weight:600;
         text-decoration:none; transition:background .15s, border-color .15s; }}
  .btn svg {{ width:16px; height:16px; }}
  .btn.primary {{ flex:1; background:var(--accent); color:#fff; }}
  .btn.primary:hover {{ background:var(--accent-hover); }}
  .btn.secondary {{ flex:1; background:var(--surface); color:var(--text); border:1px solid var(--border); }}
  .btn.primary + .btn.secondary {{ flex:0 0 auto; }}
  .btn.secondary:hover {{ background:var(--surface-2); }}

  .empty {{ text-align:center; color:var(--muted); padding:64px 16px; }}
  .empty strong {{ display:block; color:var(--text); font-size:17px; margin-bottom:4px; }}
  .pager {{ display:flex; justify-content:center; gap:8px; margin-top:28px; }}
  .pager a, .pager span {{ padding:9px 16px; border-radius:10px; font-size:14px; font-weight:600;
                          text-decoration:none; border:1px solid var(--border); }}
  .pager a {{ background:var(--surface); color:var(--text); }}
  .pager a:hover {{ background:var(--surface-2); }}
  .pager span {{ color:var(--muted); border-color:transparent; }}

  @media (max-width:600px) {{
    .bar {{ flex-direction:column; align-items:stretch; gap:10px; }}
    .grid {{ grid-template-columns:1fr; }}
  }}
</style>
</head>
<body>
<header>
  <div class="bar">
    <a class="brand" href="/">
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M9.78 18.65l.28-4.23 7.68-6.92c.34-.31-.07-.46-.52-.19L7.74 13.3 3.64 12c-.88-.25-.89-.86.2-1.3l15.97-6.16c.73-.33 1.43.18 1.15 1.3l-2.72 12.81c-.19.91-.74 1.13-1.5.71l-4.14-3.06-1.99 1.93c-.23.23-.42.42-.83.42z"/></svg>
      Telegram-Suche
    </a>
    <form method="get" action="/">
      <div class="search">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>
        <input type="search" name="q" value="{q}" placeholder="Produkt oder Stichwort suchen …" autofocus>
        {clear}
      </div>
    </form>
  </div>
</header>
<main>
  {summary}
  {results}
  {pager}
</main>
<script>
  document.querySelectorAll('.text').forEach(function (el) {{
    if (el.scrollHeight <= el.clientHeight + 2) return;
    var btn = document.createElement('button');
    btn.className = 'more'; btn.type = 'button'; btn.textContent = 'Mehr anzeigen';
    btn.onclick = function () {{
      var open = el.classList.toggle('open');
      btn.textContent = open ? 'Weniger anzeigen' : 'Mehr anzeigen';
    }};
    el.after(btn);
  }});
</script>
</body>
</html>"""

ICON_SEND = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
             '<path d="M22 2 11 13"/><path d="M22 2 15 22l-4-9-9-4 20-7z"/></svg>')
ICON_OPEN = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
             'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
             '<path d="M14 3h7v7"/><path d="M10 14 21 3"/><path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>')


def highlight(text: str, words: list[str]) -> str:
    escaped = escape(text)
    if not words:
        return escaped
    # Zwischen allen Buchstaben dürfen im Text Leerzeichen/Bindestriche stehen,
    # damit "stmartin" auch "St Martin" und "St-Martin" markiert.
    terms = sorted({normalize_term(w) for w in words} - {""}, key=len, reverse=True)
    if not terms:
        return escaped
    alternatives = [(SEPARATOR_PY + "*").join(re.escape(escape(c)) for c in t) for t in terms]
    pattern = re.compile("|".join(alternatives), re.IGNORECASE)
    return pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", escaped)


# Telegram-Usernames: 5–32 Zeichen, beginnen mit einem Buchstaben
MENTION = re.compile(r"(?<![\w@])@([A-Za-z][A-Za-z0-9_]{4,31})\b")


def contacts(row: dict) -> list[str]:
    """Wen man anschreiben kann: @Namen aus dem Text, sonst der Absender selbst
    (bei Kanal-Posts ist der Absender der Kanal, nicht eine Person)."""
    found = list(dict.fromkeys(MENTION.findall(row["text"] or "")))
    if not found and row["chat_type"] != "channel" and row["sender_username"]:
        found = [row["sender_username"]]
    return found


def contact_message(row: dict) -> str:
    lines = [row["message_link"], ""] if row["message_link"] else []
    lines.append("Hallo, ich würde gerne dieses Produkt testen.")
    return "\n".join(lines)


def app_link(message_link: str) -> str:
    """Wandelt t.me-Links in tg://-Links um, die die Telegram-App direkt bei der
    Nachricht öffnen (statt der Zwischenseite von t.me)."""
    if m := re.fullmatch(r"https://t\.me/c/(\d+)/(\d+)", message_link):
        return f"tg://privatepost?channel={m[1]}&post={m[2]}"
    if m := re.fullmatch(r"https://t\.me/([A-Za-z0-9_]+)/(\d+)", message_link):
        return f"tg://resolve?domain={m[1]}&post={m[2]}"
    return message_link


def age_days(date: datetime) -> int:
    return (datetime.now().astimezone().date() - date.astimezone().date()).days


def age_label(date: datetime) -> str:
    days = age_days(date)
    if days <= 0:
        return "heute"
    if days == 1:
        return "gestern"
    return f"vor {days} Tagen"


def split_title(text: str) -> tuple[str, str]:
    """Erste nicht-leere Zeile als Titel, der Rest als Beschreibung."""
    lines = text.strip().splitlines()
    if not lines:
        return "", ""
    return lines[0].strip(), "\n".join(lines[1:]).strip()


def render_message(row: dict, words: list[str]) -> str:
    sender = escape(row["sender_name"] or "?")
    if row["sender_username"]:
        sender += f" · @{escape(row['sender_username'])}"
    date = row["date"].astimezone().strftime("%d.%m.%Y, %H:%M")
    chat = escape(row["chat_title"] or "?")
    source = chat if sender == chat else f"{chat} · {sender}"

    title, body = split_title(row["text"] or "")
    title_html = f'<h2 class="title">{highlight(title, words)}</h2>' if title else ""
    body_html = f'<div class="text">{highlight(body, words)}</div>' if body else ""

    actions = []
    draft = quote(contact_message(row))
    for username in contacts(row):
        actions.append(f'<a class="btn primary" href="https://t.me/{username}?text={draft}" '
                       f'target="_blank" rel="noopener" title="@{escape(username)}">{ICON_SEND}Anschreiben</a>')
    if row["message_link"]:
        actions.append(f'<a class="btn secondary" href="{escape(app_link(row["message_link"]))}">'
                       f'{ICON_OPEN}Zur Nachricht</a>')
    actions_html = f'<div class="actions">{"".join(actions)}</div>' if actions else ""

    image = ""
    if row["has_image"]:
        image = (f'<a class="media" href="/image/{row["id"]}" target="_blank">'
                 f'<img src="/image/{row["id"]}" loading="lazy" alt=""></a>')

    fresh = " fresh" if age_days(row["date"]) <= 1 else ""
    return f"""
    <article class="card">
      {image}
      <div class="body">
        <div class="top">
          <div class="source"><strong>{source}</strong>{date}</div>
          <span class="age{fresh}">{age_label(row["date"])}</span>
        </div>
        {title_html}
        {body_html}
        {actions_html}
      </div>
    </article>"""


def page_link(query: str, page: int) -> str:
    params = {"q": query} if query else {}
    if page > 1:
        params["seite"] = page
    return "/?" + urlencode(params) if params else "/"


@app.get("/", response_class=HTMLResponse)
async def index(q: str = Query("", max_length=200), seite: int = Query(1, ge=1)) -> str:
    query = " ".join(q.split())
    words = query.split()
    rows, total = await db.search(words, limit=PAGE_SIZE, offset=(seite - 1) * PAGE_SIZE)

    count = f"{total} {'Nachricht' if total == 1 else 'Nachrichten'}"
    if query:
        summary = f'<p class="summary"><strong>{count}</strong> für „{escape(query)}“</p>'
    else:
        summary = f'<p class="summary"><strong>{count}</strong> · neueste zuerst</p>'

    if rows:
        results = f'<div class="grid">{"".join(render_message(r, words) for r in rows)}</div>'
    elif total:
        results = (f'<div class="empty"><strong>Diese Seite ist leer</strong>'
                   f'<a href="{page_link(query, 1)}">Zurück zur ersten Seite</a></div>')
    elif query:
        results = ('<div class="empty"><strong>Keine Treffer</strong>'
                   'Versuch es mit einem kürzeren Wortteil oder weniger Wörtern.</div>')
    else:
        results = '<div class="empty"><strong>Noch keine Nachrichten</strong>Sobald der Collector etwas empfängt, erscheint es hier.</div>'

    pages = max(1, -(-total // PAGE_SIZE))
    pager = ""
    if pages > 1:
        prev_link = f'<a href="{page_link(query, seite - 1)}">← Neuere</a>' if seite > 1 else ""
        next_link = f'<a href="{page_link(query, seite + 1)}">Ältere →</a>' if seite < pages else ""
        pager = f'<nav class="pager">{prev_link}<span>Seite {seite} von {pages}</span>{next_link}</nav>'

    clear = '<a class="clear" href="/" aria-label="Suche löschen">×</a>' if query else ""
    return PAGE.format(q=escape(query), clear=clear, summary=summary, results=results, pager=pager)


@app.get("/favicon.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
@app.get("/apple-touch-icon.png", include_in_schema=False)
async def icon(request: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / request.url.path.lstrip("/"),
                        headers={"Cache-Control": "public, max-age=604800"})


@app.get("/image/{message_pk}")
async def image(message_pk: int) -> Response:
    data = await db.get_image(message_pk)
    if not data:
        raise HTTPException(status_code=404)
    return Response(data, media_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"})
