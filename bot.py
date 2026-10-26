import os
import re
import html
import hmac
import asyncio
import logging
import sqlite3
import threading

from datetime import datetime, timezone
from urllib.parse import urlparse

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, request, jsonify

from telegram import Update
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///link_manager.db")

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
UPDATE_SECRET = os.getenv("UPDATE_SECRET", "")

RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")

WEBHOOK_PATH = "/telegram-webhook"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

telegram_app = None
bot_loop = None
bot_ready = threading.Event()


# --------------------------------------------------
# DATABASE
# --------------------------------------------------

def get_connection():
    if DATABASE_URL.startswith(("postgres://", "postgresql://")):
        return psycopg2.connect(DATABASE_URL)

    if DATABASE_URL.startswith("sqlite:///"):
        path = DATABASE_URL[len("sqlite:///"):]
    else:
        path = "link_manager.db"

    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def execute_db(query, params=(), fetchone=False, fetchall=False):
    conn = get_connection()

    try:
        if DATABASE_URL.startswith(("postgres://", "postgresql://")):
            cursor = conn.cursor(cursor_factory=RealDictCursor)
            query = query.replace("?", "%s")
        else:
            cursor = conn.cursor()

        cursor.execute(query, params)

        result = None

        if fetchone:
            row = cursor.fetchone()
            result = dict(row) if row else None

        elif fetchall:
            result = [dict(row) for row in cursor.fetchall()]

        conn.commit()
        cursor.close()

        return result

    except Exception:
        conn.rollback()
        raise

    finally:
        conn.close()


def init_db():
    execute_db("""
        CREATE TABLE IF NOT EXISTS sources (
            chat_id TEXT PRIMARY KEY,
            title TEXT NOT NULL
        )
    """)

    execute_db("""
        CREATE TABLE IF NOT EXISTS destinations (
            chat_id TEXT PRIMARY KEY,
            title TEXT NOT NULL
        )
    """)

    execute_db("""
        CREATE TABLE IF NOT EXISTS managed_posts (
            source_chat_id TEXT NOT NULL,
            destination_chat_id TEXT NOT NULL,
            message_id BIGINT NOT NULL,
            invite_link TEXT NOT NULL,
            PRIMARY KEY (source_chat_id, destination_chat_id)
        )
    """)

    logger.info("Database initialized")


def save_chat(table, chat_id, title):
    if table not in ("sources", "destinations"):
        raise ValueError("Invalid table")

    existing = execute_db(
        f"SELECT chat_id FROM {table} WHERE chat_id = ?",
        (chat_id,),
        fetchone=True,
    )

    if existing:
        execute_db(
            f"UPDATE {table} SET title = ? WHERE chat_id = ?",
            (title, chat_id),
        )
    else:
        execute_db(
            f"INSERT INTO {table} (chat_id, title) VALUES (?, ?)",
            (chat_id, title),
        )


def get_chats(table):
    if table not in ("sources", "destinations"):
        raise ValueError("Invalid table")

    return execute_db(
        f"SELECT chat_id, title FROM {table} ORDER BY title",
        fetchall=True,
    )


def get_managed_post(source_id, destination_id):
    return execute_db(
        """
        SELECT source_chat_id, destination_chat_id,
               message_id, invite_link
        FROM managed_posts
        WHERE source_chat_id = ?
          AND destination_chat_id = ?
        """,
        (source_id, destination_id),
        fetchone=True,
    )


def save_managed_post(source_id, destination_id, message_id, invite_link):
    existing = get_managed_post(source_id, destination_id)

    if existing:
        execute_db(
            """
            UPDATE managed_posts
            SET message_id = ?, invite_link = ?
            WHERE source_chat_id = ?
              AND destination_chat_id = ?
            """,
            (message_id, invite_link, source_id, destination_id),
        )
    else:
        execute_db(
            """
            INSERT INTO managed_posts
            (source_chat_id, destination_chat_id, message_id, invite_link)
            VALUES (?, ?, ?, ?)
            """,
            (source_id, destination_id, message_id, invite_link),
        )


def remove_chat(table, chat_id):
    if table not in ("sources", "destinations"):
        raise ValueError("Invalid table")

    execute_db(
        f"DELETE FROM {table} WHERE chat_id = ?",
        (chat_id,),
    )


# --------------------------------------------------
# ADMIN CHECKS
# --------------------------------------------------

def is_admin(update):
    user = update.effective_user

    return (
        user is not None
        and ADMIN_ID.isdigit()
        and user.id == int(ADMIN_ID)
    )


async def require_admin(update):
    if not is_admin(update):
        if update.effective_message:
            await update.effective_message.reply_text(
                "⛔ You are not authorized to use this bot."
            )
        return False

    return True


def parse_chat_argument(text):
    """
    Format:
    /addsource -1001234567890 | My Private Channel
    """

    parts = text.split(None, 1)

    if len(parts) < 2:
        return None, None

    value = parts[1].strip()

    if "|" in value:
        chat_id, title = value.split("|", 1)
        chat_id = chat_id.strip()
        title = title.strip()
    else:
        chat_id = value.strip()
        title = value.strip()

    if not chat_id or not title:
        return None, None

    return chat_id, title


# --------------------------------------------------
# BOT COMMANDS
# --------------------------------------------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    await update.effective_message.reply_text(
        "🤖 Telegram Link Manager\n\n"
        "Available commands:\n\n"
        "/help - Show commands\n"
        "/addsource ID | Name - Add private channel\n"
        "/adddest ID | Name - Add destination channel/group\n"
        "/sources - List source channels\n"
        "/destinations - List destination chats\n"
        "/removesource ID - Remove a source\n"
        "/removedest ID - Remove a destination\n"
        "/publish - Publish or update managed posts\n"
        "/status - Show configuration status\n\n"
        "Example:\n"
        "/addsource -1001234567890 | Study Channel\n\n"
        "Add this bot as an administrator to the "
        "source and destination chats first."
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_command(update, context)


async def add_source(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_admin(update):
        return

    chat_id, title = parse_chat_argument(
        update.effective_message.text
    )

    if not chat_id:
        await update.effective_message.reply_text(
            "Format:\n/addsource -1001234567890 | Private Channel Name"
        )
        return

    try:
        save_chat("sources", chat_id, title)

        await update.effective_message.reply_text(
            f"✅ Source saved!\n\n"
            f"Name: {title}\n"
            f"Chat ID: {chat_id}"
        )

    except Exception:
        logger.exception("Could not save source")

        await update.effective_message.reply_text(
            "❌ Could not save source. Check the database."
        )


async def add_destination(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await require_admin(update):
        return

    chat_id, title = parse_chat_argument(
        update.effective_message.text
    )

    if not chat_id:
        await update.effective_message.reply_text(
            "Format:\n/adddest -1001234567890 | Public Channel Name"
        )
        return

    try:
        save_chat("destinations", chat_id, title)

        await update.effective_message.reply_text(
            f"✅ Destination saved!\n\n"
            f"Name: {title}\n"
            f"Chat ID: {chat_id}"
        )

    except Exception:
        logger.exception("Could not save destination")

        await update.effective_message.reply_text(
            "❌ Could not save destination."
        )


async def list_chats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    table: str,
    heading: str,
):
    if not await require_admin(update):
        return

    try:
        chats = get_chats(table)

        if not chats:
            await update.effective_message.reply_text(
                f"No {heading.lower()} configured yet."
            )
            return

        lines = [heading, ""]

        for item in chats:
            lines.append(
                f"• {item['title']}\n"
                f"  ID: {item['chat_id']}"
            )

        await update.effective_message.reply_text(
            "\n".join(lines)
        )

    except Exception:
        logger.exception("Could not list chats")

        await update.effective_message.reply_text(
            "❌ Database error."
        )


async def sources_command(update, context):
    await list_chats(
        update, context, "sources", "Private Source Channels"
    )


async def destinations_command(update, context):
    await list_chats(
        update, context, "destinations", "Destination Chats"
    )


async def remove_source_command(update, context):
    if not await require_admin(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /removesource CHAT_ID"
        )
        return

    chat_id = context.args[0]

    try:
        remove_chat("sources", chat_id)

        await update.effective_message.reply_text(
            "Source removed from configuration."
        )

    except Exception:
        logger.exception("Could not remove source")

        await update.effective_message.reply_text(
            "❌ Database error."
        )


async def remove_destination_command(update, context):
    if not await require_admin(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /removedest CHAT_ID"
        )
        return

    chat_id = context.args[0]

    try:
        remove_chat("destinations", chat_id)

        await update.effective_message.reply_text(
            "Destination removed from configuration."
        )

    except Exception:
        logger.exception("Could not remove destination")

        await update.effective_message.reply_text(
            "❌ Database error."
        )


# --------------------------------------------------
# INVITE LINKS AND POST UPDATES
# --------------------------------------------------

def format_post_text(title, invite_link):
    safe_title = html.escape(title)
    safe_link = html.escape(invite_link, quote=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    return (
        f"🔒 <b>{safe_title}</b>\n\n"
        f"Join this private Telegram channel:\n\n"
        f'👉 <a href="{safe_link}">Request to Join</a>\n\n'
        f"ℹ️ This link requires admin approval.\n"
        f"🔄 Link updated: {today}"
    )


async def revoke_link(source_id, invite_link):
    if not invite_link:
        return

    try:
        await telegram_app.bot.revoke_chat_invite_link(
            chat_id=source_id,
            invite_link=invite_link,
        )

        logger.info("Revoked old invite link for %s", source_id)

    except TelegramError:
        logger.exception(
            "Could not revoke old invite link for source %s",
            source_id,
        )


async def update_managed_posts():
    sources = get_chats("sources")
    destinations = get_chats("destinations")

    if not sources:
        return "No source channels configured."

    if not destinations:
        return "No destination channels/groups configured."

    success_count = 0
    error_count = 0

    for source in sources:
        source_id = source["chat_id"]
        source_title = source["title"]

        for destination in destinations:
            destination_id = destination["chat_id"]

            new_link = None

            try:
                # Create a new join-request invite link.
                invite = await telegram_app.bot.create_chat_invite_link(
                    chat_id=source_id,
                    creates_join_request=True,
                    name=(
                        "Join request "
                        + datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    ),
                )

                new_link = invite.invite_link

                text = format_post_text(source_title, new_link)

                existing = get_managed_post(
                    source_id,
                    destination_id,
                )

                if existing:
                    # Edit the existing bot-managed post.
                    # Do not delete and repost it.
                    await telegram_app.bot.edit_message_text(
                        chat_id=destination_id,
                        message_id=int(existing["message_id"]),
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

                    message_id = int(existing["message_id"])

                else:
                    # First publication only.
                    message = await telegram_app.bot.send_message(
                        chat_id=destination_id,
                        text=text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

                    message_id = message.message_id

                old_link = existing["invite_link"] if existing else None

                # Store the new link after the post update succeeds.
                save_managed_post(
                    source_id,
                    destination_id,
                    message_id,
                    new_link,
                )

                # Revoke the previous link only after the new post
                # has been successfully published or edited.
                if old_link and old_link != new_link:
                    await revoke_link(source_id, old_link)

                success_count += 1

                logger.info(
                    "Updated source %s in destination %s",
                    source_id,
                    destination_id,
                )

            except Exception as exc:
                error_count += 1

                logger.exception(
                    "Failed source %s -> destination %s",
                    source_id,
                    destination_id,
                )

                # If publication/editing failed, revoke the unused link.
                if new_link:
                    await revoke_link(source_id, new_link)

    return (
        f"Update finished.\n"
        f"Successful posts: {success_count}\n"
        f"Failed posts: {error_count}"
    )


async def publish_command(update, context):
    if not await require_admin(update):
        return

    await update.effective_message.reply_text(
        "⏳ Publishing/updating posts..."
    )

    try:
        result = await update_managed_posts()

        await update.effective_message.reply_text(result)

    except Exception:
        logger.exception("Publish operation failed")

        await update.effective_message.reply_text(
            "❌ Update failed. Check Render logs."
        )


async def status_command(update, context):
    if not await require_admin(update):
        return

    try:
        sources = get_chats("sources")
        destinations = get_chats("destinations")

        await update.effective_message.reply_text(
            "📊 Bot Configuration\n\n"
            f"Private sources: {len(sources)}\n"
            f"Destinations: {len(destinations)}\n"
            f"Database: {'PostgreSQL' if DATABASE_URL.startswith(('postgres://', 'postgresql://')) else 'SQLite'}\n"
            f"Webhook configured: {'Yes' if RENDER_EXTERNAL_URL else 'No'}"
        )

    except Exception:
        logger.exception("Status check failed")

        await update.effective_message.reply_text(
            "❌ Database error. Check Render logs."
        )


# --------------------------------------------------
# APPLICATION SETUP
# --------------------------------------------------

def build_telegram_app():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))

    application.add_handler(CommandHandler("addsource", add_source))
    application.add_handler(CommandHandler("adddest", add_destination))

    application.add_handler(CommandHandler("sources", sources_command))
    application.add_handler(
        CommandHandler("destinations", destinations_command)
    )

    application.add_handler(
        CommandHandler("removesource", remove_source_command)
    )
    application.add_handler(
        CommandHandler("removedest", remove_destination_command)
    )

    application.add_handler(CommandHandler("publish", publish_command))
    application.add_handler(CommandHandler("status", status_command))

    return application


async def telegram_worker():
    global telegram_app

    init_db()

    telegram_app = build_telegram_app()

    await telegram_app.initialize()
    await telegram_app.start()

    if RENDER_EXTERNAL_URL:
        await telegram_app.bot.set_webhook(
            url=RENDER_EXTERNAL_URL + WEBHOOK_PATH,
            secret_token=WEBHOOK_SECRET or None,
            allowed_updates=["message"],
        )

        logger.info("Telegram webhook configured")

    bot_ready.set()

    # Keep the background loop alive.
    while True:
        await asyncio.sleep(3600)


def start_background_bot():
    global bot_loop

    if not BOT_TOKEN:
        logger.error("BOT_TOKEN environment variable is missing.")
        return

    def run():
        global bot_loop

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        bot_loop = loop

        try:
            loop.run_until_complete(telegram_worker())
        except Exception:
            logger.exception("Telegram worker stopped")
        finally:
            bot_ready.clear()
            loop.close()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()


# --------------------------------------------------
# FLASK WEBHOOK AND HEALTH CHECK
# --------------------------------------------------

@app.get("/")
def home():
    return "Telegram Link Manager is running.", 200


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": bot_ready.is_set(),
    }), 200


@app.post(WEBHOOK_PATH)
def telegram_webhook():
    if not bot_ready.is_set() or telegram_app is None or bot_loop is None:
        return jsonify({"error": "Bot is starting"}), 503

    if WEBHOOK_SECRET:
        supplied = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token", ""
        )

        if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
            return jsonify({"error": "Unauthorized"}), 403

    payload = request.get_json(silent=True)

    if not payload:
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        update = Update.de_json(payload, telegram_app.bot)

        future = asyncio.run_coroutine_threadsafe(
            telegram_app.update_queue.put(update),
            bot_loop,
        )

        future.result(timeout=5)

        return jsonify({"ok": True}), 200

    except Exception:
        logger.exception("Webhook processing error")

        return jsonify({"error": "Webhook error"}), 500


@app.post("/run-update")
def run_update_endpoint():
    if not UPDATE_SECRET:
        return jsonify({"error": "Update secret is not configured"}), 503

    auth = request.headers.get("Authorization", "")

    expected = "Bearer " + UPDATE_SECRET

    if not hmac.compare_digest(auth, expected):
        return jsonify({"error": "Unauthorized"}), 403

    if not bot_ready.is_set() or bot_loop is None:
        return jsonify({"error": "Bot is starting"}), 503

    future = asyncio.run_coroutine_threadsafe(
        update_managed_posts(),
        bot_loop,
    )

    try:
        result = future.result(timeout=100)

        return jsonify({
            "ok": True,
            "result": result,
        }), 200

    except Exception:
        logger.exception("Scheduled update failed")

        return jsonify({
            "ok": False,
            "error": "Update failed. Check Render logs.",
        }), 500


# Start the bot when Gunicorn imports this module.
start_background_bot()
