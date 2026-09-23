"""Speichert Nachrichten und verkleinerte Bilder in PostgreSQL (Neon)."""

import io
import logging
import re

from PIL import Image, ImageOps
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

log = logging.getLogger("collector.db")

IMAGE_MAX_SIDE = 800
IMAGE_JPEG_QUALITY = 70

# Leerzeichen und Bindestriche (inkl. Gedankenstriche) zählen bei der Suche nicht:
# "st-martin", "St Martin" und "StMartin" finden sich gegenseitig.
SEPARATOR_PY = r"[\s\-\u2010\u2011\u2013\u2014]"
SEPARATORS_PG = "[[:space:]\u2010\u2011\u2013\u2014-]+"


def normalize_term(term: str) -> str:
    return re.sub(SEPARATOR_PY + "+", "", term)

SCHEMA = """
CREATE TABLE IF NOT EXISTS telegram_messages (
    id              BIGSERIAL PRIMARY KEY,
    chat_id         BIGINT      NOT NULL,
    message_id      BIGINT      NOT NULL,
    date            TIMESTAMPTZ NOT NULL,
    chat_title      TEXT,
    chat_type       TEXT,
    chat_link       TEXT,
    sender_id       BIGINT,
    sender_name     TEXT,
    sender_username TEXT,
    text            TEXT,
    message_link    TEXT,
    media_type      TEXT,
    album_id        BIGINT,
    reply_to        BIGINT,
    forwarded       BOOLEAN     NOT NULL DEFAULT FALSE,
    image_data      BYTEA,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, message_id)
);

CREATE INDEX IF NOT EXISTS telegram_messages_date_idx ON telegram_messages (date);
CREATE INDEX IF NOT EXISTS telegram_messages_sender_idx ON telegram_messages (sender_id);
"""

# Gleicher Absender + gleicher Text + gleiches Bild (oder beide ohne Bild) = gleiche Nachricht
FIND_DUPLICATE = """
SELECT id FROM telegram_messages
WHERE sender_id = %(sender_id)s
  AND text IS NOT DISTINCT FROM %(text)s
  AND image_data IS NOT DISTINCT FROM %(image_data)s
ORDER BY date DESC
LIMIT 1
"""

UPDATE_DATE = """
UPDATE telegram_messages SET date = GREATEST(date, %(date)s) WHERE id = %(id)s
"""

INSERT_MESSAGE = """
INSERT INTO telegram_messages (
    chat_id, message_id, date, chat_title, chat_type, chat_link,
    sender_id, sender_name, sender_username, text, message_link,
    media_type, album_id, reply_to, forwarded, image_data
) VALUES (
    %(chat_id)s, %(message_id)s, %(date)s, %(chat_title)s, %(chat_type)s, %(chat_link)s,
    %(sender_id)s, %(sender_name)s, %(sender_username)s, %(text)s, %(message_link)s,
    %(media)s, %(album_id)s, %(reply_to)s, %(forwarded)s, %(image_data)s
)
ON CONFLICT (chat_id, message_id) DO NOTHING
RETURNING id
"""


def shrink_image(raw: bytes) -> bytes:
    """Verkleinert auf max. IMAGE_MAX_SIDE px (längste Seite) als JPEG."""
    with Image.open(io.BytesIO(raw)) as img:
        img = ImageOps.exif_transpose(img)
        img.thumbnail((IMAGE_MAX_SIDE, IMAGE_MAX_SIDE))
        if img.mode != "RGB":
            img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=IMAGE_JPEG_QUALITY, optimize=True)
        return out.getvalue()


class Database:
    def __init__(self, url: str) -> None:
        # check-Callback verwirft Verbindungen, die Neon im Leerlauf getrennt hat
        self.pool = AsyncConnectionPool(
            url, min_size=1, max_size=4, open=False,
            check=AsyncConnectionPool.check_connection,
        )

    async def open(self) -> None:
        await self.pool.open(wait=True)
        async with self.pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        await self.pool.close()

    async def save(self, record: dict, image: bytes | None) -> tuple[int | None, str]:
        """Speichert die Nachricht samt verkleinertem Bild (JPEG).

        Hat derselbe Absender genau diesen Text mit genau diesem Bild (bzw. ohne
        Bild) schon einmal geschickt, wird nur das Datum des vorhandenen Eintrags
        aktualisiert. Rückgabe: (DB-ID, "neu" | "datum_aktualisiert" | "schon_vorhanden").
        """
        params = record | {"image_data": image}
        async with self.pool.connection() as conn:
            async with conn.transaction():
                if record["sender_id"] is not None:
                    # Sperre pro Absender, damit zwei gleichzeitig eintreffende
                    # identische Nachrichten nicht beide als neu gespeichert werden
                    await conn.execute("SELECT pg_advisory_xact_lock(%s)", (record["sender_id"],))
                    cur = await conn.execute(FIND_DUPLICATE, params)
                    row = await cur.fetchone()
                    if row:
                        await conn.execute(UPDATE_DATE, {"id": row[0], "date": record["date"]})
                        return row[0], "datum_aktualisiert"
                cur = await conn.execute(INSERT_MESSAGE, params)
                row = await cur.fetchone()
                return (row[0], "neu") if row else (None, "schon_vorhanden")

    async def delete_older_than(self, days: int) -> int:
        """Löscht alle Nachrichten, deren Datum älter als `days` Tage ist."""
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM telegram_messages WHERE date < now() - make_interval(days => %s)",
                (days,),
            )
            return cur.rowcount

    async def search(self, words: list[str], limit: int = 60, offset: int = 0) -> tuple[list[dict], int]:
        """Nachrichten, deren Text alle Wörter enthält – auch als Teil eines längeren
        Wortes, in beliebiger Reihenfolge, Groß-/Kleinschreibung egal, Leerzeichen
        und Bindestriche ignoriert. Ohne Wörter: alle Nachrichten. Neueste zuerst.
        Rückgabe: (Treffer dieser Seite, Gesamtzahl der Treffer)."""
        patterns = [
            "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            for t in map(normalize_term, words) if t
        ]
        where, params = "", []
        if patterns:
            where = "WHERE regexp_replace(text, %s, '', 'g') ILIKE ALL (%s)"
            params = [SEPARATORS_PG, patterns]
        async with self.pool.connection() as conn:
            cur = conn.cursor(row_factory=dict_row)
            await cur.execute(
                f"""
                SELECT id, date, chat_title, chat_type, sender_name, sender_username,
                       text, message_link, image_data IS NOT NULL AS has_image,
                       count(*) OVER () AS total
                FROM telegram_messages
                {where}
                ORDER BY date DESC
                LIMIT %s OFFSET %s
                """,
                (*params, limit, offset),
            )
            rows = await cur.fetchall()
        if rows:
            return rows, rows[0]["total"]
        if offset == 0:
            return [], 0
        # Seite hinter dem Ende: Gesamtzahl trotzdem ermitteln
        _, total = await self.search(words, limit=1, offset=0)
        return [], total

    async def get_image(self, message_pk: int) -> bytes | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                "SELECT image_data FROM telegram_messages WHERE id = %s", (message_pk,)
            )
            row = await cur.fetchone()
            return row[0] if row else None
