"""Loggt alle eingehenden Telegram-Nachrichten in Echtzeit.

Ausgabe: übersichtlich untereinander in Konsole und messages.log,
zusätzlich maschinenlesbar in messages.jsonl und in PostgreSQL
(Tabelle telegram_messages, siehe db.py).

Nutzt die MTProto-User-API via Telethon, d.h. das Programm meldet sich mit
deinem eigenen Telegram-Account an und sieht alles, was du auch siehst:
private Chats, Gruppen und Kanäle.
"""

import asyncio
import getpass
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import Channel, Chat, User

from db import Database, shrink_image

load_dotenv()

API_ID = int(os.environ["TELEGRAM_API_ID"])
API_HASH = os.environ["TELEGRAM_API_HASH"]
SESSION_NAME = os.getenv("TELEGRAM_SESSION", "collector")
LOG_FILE = Path(os.getenv("MESSAGE_LOG_FILE", "messages.jsonl"))
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "media"))
TEXT_LOG_FILE = Path(os.getenv("TEXT_LOG_FILE", "messages.log"))
DATABASE_URL = os.environ["DATABASE_URL"]
# Nachrichten (und lokale Originalbilder) älter als so viele Tage werden gelöscht; 0 = nie
RETENTION_DAYS = int(os.getenv("RETENTION_DAYS") or 0)
CLEANUP_INTERVAL_SECONDS = 60 * 60

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("collector")
logging.getLogger("telethon").setLevel(logging.WARNING)


def display_name(entity) -> str:
    if entity is None:
        return "?"
    if isinstance(entity, User):
        name = " ".join(filter(None, [entity.first_name, entity.last_name]))
        return name or entity.username or str(entity.id)
    if isinstance(entity, (Chat, Channel)):
        return entity.title
    return str(getattr(entity, "id", "?"))


def chat_type(chat) -> str:
    if isinstance(chat, User):
        return "private"
    if isinstance(chat, Chat):
        return "group"
    if isinstance(chat, Channel):
        return "supergroup" if chat.megagroup else "channel"
    return "unknown"


def message_link(chat, message_id: int) -> str | None:
    """Link zur Originalnachricht. Öffentliche Chats: t.me/<username>/<id>,
    private Supergruppen/Kanäle: t.me/c/<id>/<id> (nur für Mitglieder).
    Für Privatchats und alte Basisgruppen bietet Telegram keine Links an."""
    if not isinstance(chat, Channel):
        return None
    if chat.username:
        return f"https://t.me/{chat.username}/{message_id}"
    return f"https://t.me/c/{chat.id}/{message_id}"


def chat_link(chat) -> str | None:
    """Link zum Chat selbst (öffentlich per Username, privat nur für Mitglieder)."""
    if isinstance(chat, User):
        return f"https://t.me/{chat.username}" if chat.username else None
    if isinstance(chat, Channel):
        if chat.username:
            return f"https://t.me/{chat.username}"
        return f"https://t.me/c/{chat.id}/999999999"
    return None


def image_link(msg_link: str | None, grouped_id) -> str | None:
    """Telegram-Link, der genau dieses Bild öffnet (bei Alben mit ?single)."""
    if not msg_link:
        return None
    return f"{msg_link}?single" if grouped_id else msg_link


FIELDS = [
    ("Zeit", "local_time"),
    ("Chat", "chat_label"),
    ("Chat-Link", "chat_link"),
    ("Absender", "sender_label"),
    ("Nachricht", "text"),
    ("Nachrichten-Link", "message_link"),
    ("Bild-Link", "image_link"),
    ("Bild-Datei", "image_file_url"),
]
LABEL_WIDTH = max(len(label) for label, _ in FIELDS) + 2


def format_block(record: dict) -> str:
    view = dict(record)
    view["local_time"] = datetime.fromisoformat(record["date"]).astimezone().strftime("%d.%m.%Y %H:%M:%S")
    view["chat_label"] = f"{record['chat_title']} ({record['chat_type']})"
    username = f" (@{record['sender_username']})" if record["sender_username"] else ""
    view["sender_label"] = f"{record['sender_name']}{username}"
    if not record["text"] and record["media"]:
        view["text"] = "<Bild ohne Text>" if record.get("image_path") else f"<{record['media']}>"

    lines = ["─" * 70]
    for label, key in FIELDS:
        value = str(view.get(key) or "–")
        # Mehrzeilige Nachrichten sauber unter der ersten Zeile einrücken
        value = value.replace("\n", "\n" + " " * LABEL_WIDTH)
        lines.append(f"{label + ':':<{LABEL_WIDTH}}{value}")
    return "\n".join(lines)


def is_image(msg) -> bool:
    if msg.photo:
        return True
    return bool(msg.document and (msg.document.mime_type or "").startswith("image/"))


def analyze(record: dict) -> None:
    """Hier eigene Analyse-Logik einhängen (Keywords, Statistiken, KI, ...)."""


async def qr_login(client: TelegramClient) -> None:
    """Login per QR-Code: in der Telegram-App unter
    Einstellungen → Geräte → Desktop-Gerät verbinden scannen."""
    qr = await client.qr_login()
    while True:
        code = qrcode.QRCode(border=1)
        code.add_data(qr.url)
        print("\nQR-Code in der Telegram-App scannen:")
        print("Einstellungen → Geräte → Desktop-Gerät verbinden\n")
        code.print_ascii(invert=True)
        try:
            await qr.wait(timeout=60)
            return
        except asyncio.TimeoutError:
            log.info("QR-Code abgelaufen, erzeuge neuen ...")
            await qr.recreate()
        except SessionPasswordNeededError:
            password = getpass.getpass("2FA-Passwort: ")
            await client.sign_in(password=password)
            return


def delete_old_media(days: int) -> int:
    cutoff = time.time() - days * 86400
    deleted = 0
    for path in MEDIA_DIR.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink()
            deleted += 1
    return deleted


async def cleanup_loop(db: Database) -> None:
    """Löscht einmal pro Stunde alles, was älter als RETENTION_DAYS ist."""
    while True:
        try:
            rows = await db.delete_older_than(RETENTION_DAYS)
            files = await asyncio.to_thread(delete_old_media, RETENTION_DAYS)
            log.info("Aufräumen: %d Nachrichten und %d Bilddateien älter als %d Tage gelöscht.",
                     rows, files, RETENTION_DAYS)
        except Exception:
            log.exception("Aufräumen fehlgeschlagen")
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


async def main() -> None:
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)
    db = Database(DATABASE_URL)
    await db.open()
    log.info("Datenbank verbunden.")

    # Nur Gruppen und Kanäle – private Chats werden ignoriert
    @client.on(events.NewMessage(incoming=True, func=lambda e: not e.is_private))
    async def on_message(event: events.NewMessage.Event) -> None:
        msg = event.message
        chat = await event.get_chat()
        sender = await event.get_sender()

        image_path = None
        if is_image(msg):
            target = MEDIA_DIR / str(event.chat_id) / str(msg.id)
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                saved = await msg.download_media(file=str(target))
                image_path = str(Path(saved).resolve()) if saved else None
            except Exception:
                log.exception("Bild-Download fehlgeschlagen")

        record = {
            "date": msg.date.astimezone(timezone.utc).isoformat(),
            "message_id": msg.id,
            "chat_id": event.chat_id,
            "chat_type": chat_type(chat),
            "chat_title": display_name(chat),
            "sender_id": event.sender_id,
            "sender_name": display_name(sender),
            "sender_username": getattr(sender, "username", None),
            "text": msg.message,
            "media": type(msg.media).__name__ if msg.media else None,
            "chat_link": chat_link(chat),
            "message_link": message_link(chat, msg.id),
            "image_link": image_link(message_link(chat, msg.id), msg.grouped_id) if image_path else None,
            "image_path": image_path,
            "image_file_url": Path(image_path).as_uri() if image_path else None,
            "album_id": msg.grouped_id,
            "reply_to": msg.reply_to_msg_id,
            "forwarded": msg.fwd_from is not None,
        }

        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

        block = format_block(record)
        print(block, flush=True)
        with TEXT_LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(block + "\n")

        image = None
        if image_path:
            try:
                image = await asyncio.to_thread(shrink_image, Path(image_path).read_bytes())
            except Exception:
                log.exception("Bild konnte nicht verkleinert werden")
        try:
            db_id, status = await db.save(record, image)
            if status == "datum_aktualisiert":
                log.info("Gleiche Nachricht gab es schon (ID %s) – nur Datum aktualisiert.", db_id)
            elif status == "schon_vorhanden":
                log.info("Nachricht war schon in der Datenbank.")
        except Exception:
            log.exception("Speichern in der Datenbank fehlgeschlagen")

        try:
            analyze(record)
        except Exception:
            log.exception("Analyse fehlgeschlagen")

    await client.connect()
    if not await client.is_user_authorized():
        await qr_login(client)
    me = await client.get_me()
    log.info("Eingeloggt als %s (@%s). Warte auf Nachrichten ... (Strg+C zum Beenden)",
             display_name(me), me.username)
    cleanup = None
    if RETENTION_DAYS > 0:
        cleanup = asyncio.create_task(cleanup_loop(db))
    else:
        log.info("RETENTION_DAYS nicht gesetzt – alte Nachrichten werden nicht gelöscht.")
    try:
        await client.run_until_disconnected()
    finally:
        if cleanup:
            cleanup.cancel()
        await db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
