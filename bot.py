import os
import re
import html
import asyncio
import logging
import threading
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError, BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

BASE_URL = (
    os.getenv("WEBHOOK_URL")
    or os.getenv("RENDER_EXTERNAL_URL")
    or ""
).strip().rstrip("/")

UPDATE_SECRET = os.getenv("UPDATE_SECRET", "").strip()

WEBHOOK_SECRET = re.sub(
    r"[^A-Za-z0-9_-]",
    "",
    os.getenv("WEBHOOK_SECRET", ""),
)[:256]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("link_manager")

app = Flask(__name__)

telegram_app = None
bot_loop = None
bot_ready = False

startup_lock = threading.Lock()
startup_started = False
update_lock = None


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    parsed = urlparse(DATABASE_URL)
    params = dict(parse_qsl(parsed.query, keep_blank_values=True))

    params.setdefault("sslmode", "require")

    secure_url = urlunparse(
        parsed._replace(query=urlencode(params))
    )

    return psycopg2.connect(
        secure_url,
        connect_timeout=15,
    )


def db_execute(sql, params=(), fetch=None):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)

            if fetch == "one":
                return cur.fetchone()

            if fetch == "all":
                return cur.fetchall()

            return None


def init_db():
    statements = [
        """
        CREATE TABLE IF NOT EXISTS source_chats (
            chat_id BIGINT PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            added_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS destination_chats (
            chat_id BIGINT PRIMARY KEY,
            title TEXT NOT NULL,
            username TEXT,
            added_at TIMESTAMPTZ DEFAULT NOW()
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS managed_posts (
            source_id BIGINT NOT NULL,
            destination_id BIGINT NOT NULL,
            message_id BIGINT NOT NULL,
            invite_url TEXT NOT NULL,
            updated_at TIMESTAMPTZ DEFAULT NOW(),
            PRIMARY KEY (source_id, destination_id)
        )
        """,
    ]

    with db_connect() as conn:
        with conn.cursor() as cur:
            for statement in statements:
                cur.execute(statement)

    logger.info("Database initialized")


def get_chats(table):
    if table not in ("source_chats", "destination_chats"):
        raise ValueError("Invalid table")

    return db_execute(
        f"SELECT * FROM {table} ORDER BY title",
        fetch="all",
    )


def save_chat(table, chat_id, title, username=None):
    if table not in ("source_chats", "destination_chats"):
        raise ValueError("Invalid table")

    db_execute(
        f"""
        INSERT INTO {table} (chat_id, title, username)
        VALUES (%s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE SET
            title = EXCLUDED.title,
            username = EXCLUDED.username
        """,
        (chat_id, title, username),
    )


def remove_chat(table, chat_id):
    if table not in ("source_chats", "destination_chats"):
        raise ValueError("Invalid table")

    db_execute(
        f"DELETE FROM {table} WHERE chat_id = %s",
        (chat_id,),
    )


def get_managed_post(source_id, destination_id):
    return db_execute(
        """
        SELECT * FROM managed_posts
        WHERE source_id = %s AND destination_id = %s
        """,
        (source_id, destination_id),
        fetch="one",
    )


def save_managed_post(source_id, destination_id, message_id, invite_url):
    db_execute(
        """
        INSERT INTO managed_posts
            (source_id, destination_id, message_id, invite_url)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (source_id, destination_id)
        DO UPDATE SET
            message_id = EXCLUDED.message_id,
            invite_url = EXCLUDED.invite_url,
            updated_at = NOW()
        """,
        (source_id, destination_id, message_id, invite_url),
    )


# =========================================================
# ADMIN
# =========================================================

def is_admin(user_id):
    return bool(ADMIN_ID and user_id == ADMIN_ID)


async def check_admin(update):
    user = update.effective_user

    if user and is_admin(user.id):
        return True

    if update.callback_query:
        await update.callback_query.answer(
            "Not authorized",
            show_alert=True,
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "⛔ You are not authorized to use this bot."
        )

    return False


# =========================================================
# MENUS
# =========================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📥 Sources",
                callback_data="sources",
            ),
            InlineKeyboardButton(
                "📤 Destinations",
                callback_data="destinations",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 Publish / Update",
                callback_data="publish_confirm",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Status",
                callback_data="status",
            ),
            InlineKeyboardButton(
                "ℹ️ Help",
                callback_data="help",
            ),
        ],
    ])


def back_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Main Menu",
                callback_data="home",
            )
        ]
    ])


def chat_menu(kind):
    if kind == "source":
        table = "source_chats"
        title = "📥 Source Channels"
        add_callback = "add_source"
        remove_prefix = "remove_source"
    else:
        table = "destination_chats"
        title = "📤 Destination Chats"
        add_callback = "add_destination"
        remove_prefix = "remove_destination"

    rows = [[
        InlineKeyboardButton(
            "➕ Add Chat",
            callback_data=add_callback,
        )
    ]]

    for chat in get_chats(table):
        label = (chat["title"] or str(chat["chat_id"]))[:40]

        rows.append([
            InlineKeyboardButton(
                f"❌ {label}",
                callback_data=f"{remove_prefix}:{chat['chat_id']}",
            )
        ])

    rows.append([
        InlineKeyboardButton(
            "⬅️ Main Menu",
            callback_data="home",
        )
    ])

    return title, InlineKeyboardMarkup(rows)


# =========================================================
# ADD CHAT
# =========================================================

async def add_chat_input(update, context):
    if not await check_admin(update):
        return

    kind = context.user_data.get("pending_action")

    if kind not in ("source", "destination"):
        return

    message = update.effective_message
    argument = message.text.strip() if message and message.text else ""

    if not argument:
        return

    try:
        chat = await context.bot.get_chat(argument)

        if chat.type not in ("channel", "group", "supergroup"):
            await message.reply_text(
                "Sirf Telegram channel ya group add kar sakte ho."
            )
            return

        member = await context.bot.get_chat_member(
            chat.id,
            context.bot.id,
        )

        if member.status not in ("administrator", "creator"):
            await message.reply_text(
                "⛔ Pehle bot ko is chat mein admin banao."
            )
            return

        table = (
            "source_chats"
            if kind == "source"
            else "destination_chats"
        )

        save_chat(
            table,
            chat.id,
            chat.title or str(chat.id),
            chat.username,
        )

        context.user_data.pop("pending_action", None)

        await message.reply_text(
            f"✅ Chat added!\n"
            f"Name: {chat.title or 'Unknown'}\n"
            f"ID: {chat.id}",
            reply_markup=main_menu(),
        )

    except Exception as exc:
        logger.exception("Adding chat failed")

        await message.reply_text(
            "❌ Chat add nahi hua.\n"
            f"Error: {str(exc)[:500]}\n\n"
            "Private channel ke liye numeric ID use karo."
        )


# =========================================================
# PUBLISH / ROTATE LINKS
# =========================================================

def make_post_text(source_title, invite_url):
    title = html.escape(source_title)
    url = html.escape(invite_url, quote=True)

    return (
        f"📢 <b>{title}</b>\n\n"
        f'🔗 <a href="{url}">Join Channel</a>\n\n'
        f"🔄 <i>Invite link automatically updated.</i>"
    )


async def update_managed_posts():
    global update_lock

    if telegram_app is None:
        raise RuntimeError("Telegram app is not initialized")

    if update_lock is None:
        update_lock = asyncio.Lock()

    async with update_lock:
        sources = get_chats("source_chats")
        destinations = get_chats("destination_chats")

        if not sources:
            return "❌ No source channels configured."

        if not destinations:
            return "❌ No destination chats configured."

        success = 0
        failures = []

        for source in sources:
            for destination in destinations:
                source_id = int(source["chat_id"])
                dest_id = int(destination["chat_id"])

                new_link = None
                telegram_post_updated = False

                try:
                    invite = await telegram_app.bot.create_chat_invite_link(
                        chat_id=source_id,
                        creates_join_request=True,
                        name=datetime.now(timezone.utc).strftime(
                            "Auto-%Y%m%d-%H%M"
                        )[:32],
                    )

                    new_link = invite.invite_link

                    old = get_managed_post(source_id, dest_id)

                    text = make_post_text(
                        source["title"],
                        new_link,
                    )

                    if old:
                        message_id = int(old["message_id"])

                        try:
                            await telegram_app.bot.edit_message_text(
                                chat_id=dest_id,
                                message_id=message_id,
                                text=text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True,
                            )
                        except BadRequest as exc:
                            if "message is not modified" not in str(exc).lower():
                                raise
                    else:
                        sent = await telegram_app.bot.send_message(
                            chat_id=dest_id,
                            text=text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                        message_id = sent.message_id

                    telegram_post_updated = True

                    save_managed_post(
                        source_id,
                        dest_id,
                        message_id,
                        new_link,
                    )

                    # Revoke old link only after the post and database
                    # record have been updated successfully.
                    if old and old["invite_url"] != new_link:
                        try:
                            await telegram_app.bot.revoke_chat_invite_link(
                                chat_id=source_id,
                                invite_link=old["invite_url"],
                            )
                        except TelegramError:
                            logger.warning(
                                "Could not revoke old link for %s",
                                source_id,
                            )

                    success += 1

                except Exception as exc:
                    logger.exception(
                        "Publish failed: source=%s destination=%s",
                        source_id,
                        dest_id,
                    )

                    # Never revoke a link after a successful post edit,
                    # because the post may already contain that link.
                    if new_link and not telegram_post_updated:
                        try:
                            await telegram_app.bot.revoke_chat_invite_link(
                                chat_id=source_id,
                                invite_link=new_link,
                            )
                        except TelegramError:
                            pass

                    failures.append(
                        f"{source['title']} → {destination['title']}: "
                        f"{str(exc)[:150]}"
                    )

        result = (
            "🔄 <b>Update Complete</b>\n\n"
            f"✅ Successful: {success}\n"
            f"❌ Failed: {len(failures)}"
        )

        if failures:
            result += "\n\n<b>Errors:</b>\n" + "\n".join(
                html.escape(x) for x in failures[:8]
            )

        return result


# =========================================================
# COMMANDS
# =========================================================

async def start_command(update, context):
    if not await check_admin(update):
        return

    await update.effective_message.reply_text(
        "🤖 Telegram Link Manager\n\nChoose an option:",
        reply_markup=main_menu(),
    )


async def help_command(update, context):
    if not await check_admin(update):
        return

    await update.effective_message.reply_text(
        "ℹ️ Help\n\n"
        "/start — Open admin menu\n"
        "/status — Check status\n"
        "/publish — Publish/update links\n\n"
        "Add source and destination chats using the buttons. "
        "The bot must be admin in all managed chats.",
        reply_markup=main_menu(),
    )


async def status_command(update, context):
    if not await check_admin(update):
        return

    sources = get_chats("source_chats")
    destinations = get_chats("destination_chats")

    await update.effective_message.reply_text(
        f"📊 Bot ready: {bot_ready}\n"
        f"Database: PostgreSQL\n"
        f"Sources: {len(sources)}\n"
        f"Destinations: {len(destinations)}",
        reply_markup=main_menu(),
    )


async def publish_command(update, context):
    if not await check_admin(update):
        return

    await update.effective_message.reply_text(
        "⚠️ Generate new invite links and update posts?",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "✅ Confirm",
                    callback_data="publish_run",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="home",
                ),
            ]
        ]),
    )


# =========================================================
# BUTTON HANDLER
# =========================================================

async def button_handler(update, context):
    query = update.callback_query

    if not await check_admin(update):
        return

    await query.answer()
    data = query.data or ""

    if data == "home":
        context.user_data.pop("pending_action", None)

        await query.edit_message_text(
            "🤖 Telegram Link Manager",
            reply_markup=main_menu(),
        )
        return

    if data == "sources":
        title, keyboard = chat_menu("source")
        await query.edit_message_text(title, reply_markup=keyboard)
        return

    if data == "destinations":
        title, keyboard = chat_menu("destination")
        await query.edit_message_text(title, reply_markup=keyboard)
        return

    if data == "add_source":
        context.user_data["pending_action"] = "source"

        await query.edit_message_text(
            "📥 Send source channel ID or public @username.\n\n"
            "Bot ko source channel mein admin banao.",
            reply_markup=back_menu(),
        )
        return

    if data == "add_destination":
        context.user_data["pending_action"] = "destination"

        await query.edit_message_text(
            "📤 Send destination channel/group ID or @username.\n\n"
            "Bot ko destination mein admin banao.",
            reply_markup=back_menu(),
        )
        return

    if data.startswith("remove_source:"):
        chat_id = int(data.split(":", 1)[1])
        remove_chat("source_chats", chat_id)

        title, keyboard = chat_menu("source")
        await query.edit_message_text(
            "✅ Source configuration removed.\n\n" + title,
            reply_markup=keyboard,
        )
        return

    if data.startswith("remove_destination:"):
        chat_id = int(data.split(":", 1)[1])
        remove_chat("destination_chats", chat_id)

        title, keyboard = chat_menu("destination")
        await query.edit_message_text(
            "✅ Destination configuration removed.\n\n" + title,
            reply_markup=keyboard,
        )
        return

    if data == "publish_confirm":
        await query.edit_message_text(
            "⚠️ Create fresh join-request links and update managed posts?",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ Run Update",
                        callback_data="publish_run",
                    ),
                    InlineKeyboardButton(
                        "❌ Cancel",
                        callback_data="home",
                    ),
                ]
            ]),
        )
        return

    if data == "publish_run":
        await query.edit_message_text(
            "⏳ Updating links. Please wait..."
        )

        try:
            result = await update_managed_posts()

            await query.edit_message_text(
                result,
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )
        except Exception:
            logger.exception("Manual update failed")

            await query.edit_message_text(
                "❌ Update failed. Check Render logs.",
                reply_markup=main_menu(),
            )
        return

    if data == "status":
        sources = get_chats("source_chats")
        destinations = get_chats("destination_chats")

        await query.edit_message_text(
            f"📊 <b>Status</b>\n\n"
            f"Bot ready: {bot_ready}\n"
            f"Sources: {len(sources)}\n"
            f"Destinations: {len(destinations)}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
        return

    if data == "help":
        await query.edit_message_text(
            "ℹ️ Help\n\n"
            "1. Add source channels.\n"
            "2. Add destination chats.\n"
            "3. Make the bot an admin in each chat.\n"
            "4. Select Publish / Update.\n\n"
            "Links use join-request approval.",
            reply_markup=main_menu(),
        )


# =========================================================
# TELEGRAM STARTUP
# =========================================================

async def start_telegram():
    global telegram_app, bot_ready

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID is missing")

    if not BASE_URL:
        raise RuntimeError(
            "WEBHOOK_URL or RENDER_EXTERNAL_URL is missing"
        )

    init_db()

    telegram_app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(CommandHandler("start", start_command))
    telegram_app.add_handler(CommandHandler("help", help_command))
    telegram_app.add_handler(CommandHandler("status", status_command))
    telegram_app.add_handler(CommandHandler("publish", publish_command))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            add_chat_input,
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_options = {
        "url": f"{BASE_URL}/telegram-webhook",
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }

    if WEBHOOK_SECRET:
        webhook_options["secret_token"] = WEBHOOK_SECRET

    await telegram_app.bot.set_webhook(**webhook_options)

    bot_ready = True
    logger.info("BOT_READY: Telegram application and webhook started")


def telegram_thread():
    global bot_loop, bot_ready

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_loop = loop

    try:
        loop.run_until_complete(start_telegram())
        loop.run_forever()
    except Exception:
        bot_ready = False
        logger.exception("Telegram startup failed")


def start_background_bot():
    global startup_started

    with startup_lock:
        if startup_started:
            return

        startup_started = True

    threading.Thread(
        target=telegram_thread,
        name="telegram-bot",
        daemon=True,
    ).start()


# =========================================================
# FLASK ROUTES
# =========================================================

@app.get("/")
def index():
    return jsonify({
        "service": "Telegram Link Manager",
        "status": "ok",
        "bot_ready": bot_ready,
        "database": "PostgreSQL",
    })


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": bot_ready,
    }), 200


@app.post("/telegram-webhook")
def telegram_webhook():
    # IMPORTANT:
    # Do not reject requests just because bot_ready is False.
    # During a restart Telegram may retry an earlier update.
    if telegram_app is None or bot_loop is None or not bot_loop.is_running():
        logger.warning(
            "Webhook unavailable: app_exists=%s loop_exists=%s loop_running=%s",
            telegram_app is not None,
            bot_loop is not None,
            bot_loop.is_running() if bot_loop is not None else False,
        )

        return jsonify({"error": "Bot initializing"}), 503

    if WEBHOOK_SECRET:
        received = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if received != WEBHOOK_SECRET:
            logger.warning("Rejected webhook: invalid secret")
            return jsonify({"error": "Unauthorized"}), 401

    payload = request.get_json(silent=True)

    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON"}), 400

    try:
        telegram_update = Update.de_json(
            payload,
            telegram_app.bot,
        )

        future = asyncio.run_coroutine_threadsafe(
            telegram_app.update_queue.put(telegram_update),
            bot_loop,
        )

        future.result(timeout=5)

        return jsonify({"ok": True}), 200

    except Exception:
        logger.exception("Webhook enqueue failed")
        return jsonify({"error": "Webhook processing failed"}), 500


@app.post("/run-update")
def run_update_endpoint():
    if not UPDATE_SECRET:
        return jsonify({"error": "UPDATE_SECRET missing"}), 503

    if request.headers.get("Authorization") != f"Bearer {UPDATE_SECRET}":
        return jsonify({"error": "Unauthorized"}), 401

    if telegram_app is None or bot_loop is None or not bot_loop.is_running():
        return jsonify({"error": "Bot not initialized"}), 503

    try:
        future = asyncio.run_coroutine_threadsafe(
            update_managed_posts(),
            bot_loop,
        )

        result = future.result(timeout=110)

        return jsonify({
            "ok": True,
            "result": result,
        }), 200

    except Exception:
        logger.exception("Scheduled update failed")

        return jsonify({
            "ok": False,
            "error": "Update failed; check Render logs",
        }), 500


# =========================================================
# START
# =========================================================

start_background_bot()
