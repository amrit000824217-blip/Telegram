import os
import re
import json
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
# CONFIGURATION
# =========================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.environ.get("ADMIN_ID", "0"))

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()

BASE_URL = (
    os.environ.get("WEBHOOK_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).strip().rstrip("/")

UPDATE_SECRET = os.environ.get("UPDATE_SECRET", "").strip()
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").strip()

# Telegram only accepts letters, numbers, underscores and hyphens
# in its webhook secret token.
WEBHOOK_SECRET = re.sub(r"[^A-Za-z0-9_-]", "", WEBHOOK_SECRET)[:256]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

logger = logging.getLogger(__name__)

app = Flask(__name__)

bot_ready = False
telegram_app = None
bot_loop = None

startup_lock = threading.Lock()
startup_started = False
update_lock = None


# =========================================================
# DATABASE
# =========================================================

def db_connect():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL environment variable is missing")

    # Ensure SSL is enabled for Neon when not already specified.
    parsed = urlparse(DATABASE_URL)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))

    if "sslmode" not in query:
        query["sslmode"] = "require"

    secure_url = urlunparse(
        parsed._replace(query=urlencode(query))
    )

    return psycopg2.connect(
        secure_url,
        connect_timeout=15,
    )


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

    logger.info("Database initialized successfully")


def db_execute(sql, params=(), fetch=None):
    with db_connect() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute(sql, params)

            if fetch == "one":
                return cur.fetchone()

            if fetch == "all":
                return cur.fetchall()

            return None


def get_chats(table):
    if table not in ("source_chats", "destination_chats"):
        raise ValueError("Invalid table")

    return db_execute(
        f"SELECT * FROM {table} ORDER BY title",
        fetch="all",
    )


def get_chat_record(table, chat_id):
    if table not in ("source_chats", "destination_chats"):
        raise ValueError("Invalid table")

    return db_execute(
        f"SELECT * FROM {table} WHERE chat_id = %s",
        (chat_id,),
        fetch="one",
    )


def save_chat(table, chat_id, title, username=None):
    if table not in ("source_chats", "destination_chats"):
        raise ValueError("Invalid table")

    db_execute(
        f"""
        INSERT INTO {table} (chat_id, title, username)
        VALUES (%s, %s, %s)
        ON CONFLICT (chat_id)
        DO UPDATE SET
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
        SELECT *
        FROM managed_posts
        WHERE source_id = %s AND destination_id = %s
        """,
        (source_id, destination_id),
        fetch="one",
    )


def save_managed_post(
    source_id,
    destination_id,
    message_id,
    invite_url,
):
    db_execute(
        """
        INSERT INTO managed_posts
            (source_id, destination_id, message_id, invite_url, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (source_id, destination_id)
        DO UPDATE SET
            message_id = EXCLUDED.message_id,
            invite_url = EXCLUDED.invite_url,
            updated_at = NOW()
        """,
        (
            source_id,
            destination_id,
            message_id,
            invite_url,
        ),
    )


# =========================================================
# ADMIN CHECK
# =========================================================

def is_admin(user_id):
    return bool(ADMIN_ID and user_id == ADMIN_ID)


async def admin_only(update: Update):
    user = update.effective_user

    if not user or not is_admin(user.id):
        if update.callback_query:
            await update.callback_query.answer(
                "Access denied.",
                show_alert=True,
            )
        elif update.effective_message:
            await update.effective_message.reply_text(
                "⛔ You are not authorized to use this bot."
            )

        return False

    return True


# =========================================================
# INLINE MENUS
# =========================================================

def main_menu():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📥 Source Channels",
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
                "📊 Bot Status",
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
        heading = "📥 Source Channels"
        add_callback = "add_source"
        remove_prefix = "remove_source"
    else:
        table = "destination_chats"
        heading = "📤 Destination Channels"

        add_callback = "add_destination"
        remove_prefix = "remove_destination"

    rows = [
        [InlineKeyboardButton(
            "➕ Add Chat",
            callback_data=add_callback,
        )]
    ]

    try:
        chats = get_chats(table)

        for chat in chats:
            title = chat["title"] or str(chat["chat_id"])

            # Keep button text short.
            title = title[:45]

            rows.append([
                InlineKeyboardButton(
                    f"❌ {title}",
                    callback_data=f"{remove_prefix}:{chat['chat_id']}",
                )
            ])

    except Exception:
        logger.exception("Unable to load chat menu")

    rows.append([
        InlineKeyboardButton(
            "⬅️ Main Menu",
            callback_data="home",
        )
    ])

    return heading, InlineKeyboardMarkup(rows)


# =========================================================
# CHAT INPUT
# =========================================================

async def resolve_chat(context, argument):
    argument = argument.strip()

    if not argument:
        raise ValueError(
            "Chat ID ya public @username enter karo."
        )

    chat = await context.bot.get_chat(argument)

    if chat.type not in ("channel", "group", "supergroup"):
        raise ValueError(
            "Sirf Telegram channels aur groups supported hain."
        )

    return chat


async def add_chat_from_input(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    action = context.user_data.get("pending_action")

    if action not in ("source", "destination"):
        return

    message = update.effective_message

    if not message or not message.text:
        return

    argument = message.text.strip()

    try:
        chat = await resolve_chat(context, argument)

        # Verify the bot is an administrator in the chat.
        member = await context.bot.get_chat_member(
            chat.id,
            context.bot.id,
        )

        if member.status not in ("administrator", "creator"):
            await message.reply_text(
                "⛔ Bot ko is channel/group mein admin banao, "
                "phir dobara try karo."
            )
            return

        if action == "source":
            table = "source_chats"
        else:
            table = "destination_chats"

        save_chat(
            table,
            chat.id,
            chat.title or str(chat.id),
            chat.username,
        )

        context.user_data.pop("pending_action", None)

        await message.reply_text(
            f"✅ Added successfully!\n\n"
            f"Name: {html.escape(chat.title or 'Unknown')}\n"
            f"Chat ID: <code>{chat.id}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )

    except Exception as exc:
        logger.exception("Could not add chat")

        await message.reply_text(
            "❌ Chat add nahi hua.\n\n"
            f"Reason: {html.escape(str(exc)[:700])}\n\n"
            "Private channel ke liye numeric chat ID use karo "
            "aur bot ko admin banao.",
            parse_mode=ParseMode.HTML,
        )


# =========================================================
# PUBLISH / UPDATE
# =========================================================

def make_post_text(source_title, invite_url):
    safe_title = html.escape(source_title)

    return (
        f"📢 <b>{safe_title}</b>\n\n"
        f"🔗 <a href=\"{html.escape(invite_url, quote=True)}\">"
        f"Join Channel</a>\n\n"
        f"🔄 <i>Invite link updated automatically.</i>"
    )


async def update_managed_posts():
    global update_lock

    if telegram_app is None:
        raise RuntimeError("Telegram application is not initialized")

    if update_lock is None:
        update_lock = asyncio.Lock()

    async with update_lock:
        sources = get_chats("source_chats")
        destinations = get_chats("destination_chats")

        if not sources:
            return "❌ No source channels configured."

        if not destinations:
            return "❌ No destination channels configured."

        success = 0
        failures = []

        for source in sources:
            for destination in destinations:
                source_id = int(source["chat_id"])
                destination_id = int(destination["chat_id"])

                new_invite = None
                post_sent_or_edited = False

                try:
                    # Bot must have invite-link permissions in source.
                    invite = await telegram_app.bot.create_chat_invite_link(
                        chat_id=source_id,
                        creates_join_request=True,
                        name=(
                            "Auto "
                            + datetime.now(timezone.utc).strftime(
                                "%Y%m%d%H%M"
                            )
                        )[:32],
                    )

                    new_invite = invite.invite_link

                    old_post = get_managed_post(
                        source_id,
                        destination_id,
                    )

                    text = make_post_text(
                        source["title"],
                        new_invite,
                    )

                    if old_post:
                        try:
                            await telegram_app.bot.edit_message_text(
                                chat_id=destination_id,
                                message_id=int(old_post["message_id"]),
                                text=text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True,
                            )

                        except BadRequest as exc:
                            # If Telegram says the content is unchanged,
                            # the existing post is still valid.
                            if "message is not modified" not in str(exc).lower():
                                raise

                        message_id = int(old_post["message_id"])

                    else:
                        sent = await telegram_app.bot.send_message(
                            chat_id=destination_id,
                            text=text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )

                        message_id = sent.message_id

                    post_sent_or_edited = True

                    # Save new link only after Telegram accepted the post.
                    save_managed_post(
                        source_id,
                        destination_id,
                        message_id,
                        new_invite,
                    )

                    # Revoke old link only after the new link and post
                    # have been successfully saved.
                    if old_post:
                        old_invite = old_post["invite_url"]

                        if old_invite and old_invite != new_invite:
                            try:
                                await telegram_app.bot.revoke_chat_invite_link(
                                    chat_id=source_id,
                                    invite_link=old_invite,
                                )
                            except TelegramError:
                                logger.warning(
                                    "Could not revoke previous invite link "
                                    "for source %s",
                                    source_id,
                                )

                    success += 1

                except Exception as exc:
                    logger.exception(
                        "Update failed for source=%s destination=%s",
                        source_id,
                        destination_id,
                    )

                    # Do not revoke new_invite here if the post was
                    # already updated; that could invalidate its link.
                    if new_invite and not post_sent_or_edited:
                        try:
                            await telegram_app.bot.revoke_chat_invite_link(
                                chat_id=source_id,
                                invite_link=new_invite,
                            )
                        except TelegramError:
                            pass

                    failures.append(
                        f"{source['title']} → {destination['title']}: "
                        f"{str(exc)[:180]}"
                    )

        result = (
            f"🔄 <b>Update finished</b>\n\n"
            f"✅ Successful: {success}\n"
            f"❌ Failed: {len(failures)}"
        )

        if failures:
            result += "\n\n<b>Errors:</b>\n" + "\n".join(
                html.escape(item) for item in failures[:10]
            )

        return result


# =========================================================
# COMMANDS
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    await update.effective_message.reply_text(
        "🤖 Telegram Link Manager\n\n"
        "Choose an action from the inline menu.",
        reply_markup=main_menu(),
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    text = (
        "ℹ️ <b>Bot Help</b>\n\n"
        "/start — Open admin panel\n"
        "/status — Check bot status\n"
        "/publish — Publish or update invite links\n\n"
        "Add source channels and destination chats from "
        "the inline menu.\n\n"
        "The bot must be an administrator in every managed "
        "source and destination chat."
    )

    await update.effective_message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def status_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    sources = get_chats("source_chats")
    destinations = get_chats("destination_chats")

    await update.effective_message.reply_text(
        f"📊 <b>Bot Status</b>\n\n"
        f"Bot ready: {'Yes' if bot_ready else 'No'}\n"
        f"Database: PostgreSQL\n"
        f"Sources: {len(sources)}\n"
        f"Destinations: {len(destinations)}",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def publish_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    if not await admin_only(update):
        return

    await update.effective_message.reply_text(
        "⚠️ This will generate new invite links and update "
        "managed posts.\n\nContinue?",
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
# INLINE BUTTON HANDLER
# =========================================================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    query = update.callback_query

    if not await admin_only(update):
        return

    await query.answer()

    data = query.data or ""

    if data == "home":
        context.user_data.pop("pending_action", None)

        await query.edit_message_text(
            "🤖 Telegram Link Manager\n\nSelect an option:",
            reply_markup=main_menu(),
        )
        return

    if data == "sources":
        title, keyboard = chat_menu("source")

        await query.edit_message_text(
            title,
            reply_markup=keyboard,
        )
        return

    if data == "destinations":
        title, keyboard = chat_menu("destination")

        await query.edit_message_text(
            title,
            reply_markup=keyboard,
        )
        return

    if data == "add_source":
        context.user_data["pending_action"] = "source"

        await query.edit_message_text(
            "📥 <b>Add Source Channel</b>\n\n"
            "Bot ko source channel mein admin banao.\n"
            "Ab channel ka numeric ID ya public @username "
            "bhejo.\n\n"
            "Example: <code>-1001234567890</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return

    if data == "add_destination":
        context.user_data["pending_action"] = "destination"

        await query.edit_message_text(
            "📤 <b>Add Destination</b>\n\n"
            "Bot ko destination group/channel mein admin banao "
            "aur messages post/edit karne ki permission do.\n\n"
            "Ab numeric chat ID ya public @username bhejo.",
            parse_mode=ParseMode.HTML,
            reply_markup=back_menu(),
        )
        return

    if data.startswith("remove_source:"):
        chat_id = int(data.split(":", 1)[1])

        remove_chat("source_chats", chat_id)

        title, keyboard = chat_menu("source")

        await query.edit_message_text(
            f"✅ Source configuration removed.\n\n{title}",
            reply_markup=keyboard,
        )
        return

    if data.startswith("remove_destination:"):
        chat_id = int(data.split(":", 1)[1])

        remove_chat("destination_chats", chat_id)

        title, keyboard = chat_menu("destination")

        await query.edit_message_text(
            f"✅ Destination configuration removed.\n\n{title}",
            reply_markup=keyboard,
        )
        return

    if data == "publish_confirm":
        await query.edit_message_text(
            "⚠️ <b>Publish / Update</b>\n\n"
            "The bot will create fresh join-request links "
            "and edit existing managed posts where possible.\n\n"
            "Continue?",
            parse_mode=ParseMode.HTML,
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
            "⏳ Updating links and posts. Please wait..."
        )

        try:
            result = await update_managed_posts()

            await query.edit_message_text(
                result,
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu(),
            )

        except Exception:
            logger.exception("Publish operation failed")

            await query.edit_message_text(
                "❌ Update failed. Check Render logs.",
                reply_markup=main_menu(),
            )

        return

    if data == "status":
        sources = get_chats("source_chats")
        destinations = get_chats("destination_chats")

        await query.edit_message_text(
            f"📊 <b>Bot Status</b>\n\n"
            f"Bot ready: {'Yes' if bot_ready else 'No'}\n"
            f"Database: PostgreSQL\n"
            f"Sources: {len(sources)}\n"
            f"Destinations: {len(destinations)}",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
        return

    if data == "help":
        await query.edit_message_text(
            "ℹ️ <b>Help</b>\n\n"
            "1. Add source channels.\n"
            "2. Add destination chats.\n"
            "3. Give the bot administrator permissions.\n"
            "4. Select Publish / Update.\n\n"
            "The bot uses join-request invite links.",
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu(),
        )
        return


# =========================================================
# TELEGRAM APPLICATION STARTUP
# =========================================================

async def start_telegram():
    global telegram_app, bot_ready

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID is missing")

    if not BASE_URL:
        raise RuntimeError(
            "Set RENDER_EXTERNAL_URL or WEBHOOK_URL in Render"
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

    telegram_app.add_handler(
        CallbackQueryHandler(button_handler)
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            add_chat_from_input,
        )
    )

    await telegram_app.initialize()
    await telegram_app.start()

    webhook_args = {
        "url": f"{BASE_URL}/telegram-webhook",
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    }

    if WEBHOOK_SECRET:
        webhook_args["secret_token"] = WEBHOOK_SECRET

    await telegram_app.bot.set_webhook(**webhook_args)

    bot_ready = True

    logger.info("Telegram application started")
    logger.info("Webhook configured successfully")
    logger.info("Bot is ready")


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

    thread = threading.Thread(
        target=telegram_thread,
        name="telegram-bot-thread",
        daemon=True,
    )

    thread.start()


# =========================================================
# FLASK ROUTES
# =========================================================

@app.route("/", methods=["GET"])
def index():
    return jsonify({
        "service": "Telegram Link Manager",
        "status": "ok",
        "bot_ready": bot_ready,
        "database": "PostgreSQL",
    })


@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "status": "ok",
        "database": "PostgreSQL",
        "bot_ready": bot_ready,
    }), 200


@app.route("/telegram-webhook", methods=["POST"])
def telegram_webhook():
    if not bot_ready or telegram_app is None or bot_loop is None:
        logger.error("Webhook received before bot became ready")

        return jsonify({
            "error": "Bot is starting",
            "bot_ready": False,
        }), 503

    if WEBHOOK_SECRET:
        received_secret = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if received_secret != WEBHOOK_SECRET:
            logger.warning("Webhook rejected: invalid secret")

            return jsonify({"error": "Unauthorized"}), 401

    try:
        payload = request.get_json(silent=True)

        if not isinstance(payload, dict):
            return jsonify({"error": "Invalid JSON"}), 400

        telegram_update = Update.de_json(
            payload,
            telegram_app.bot,
        )

        # Safely add the update to PTB's queue from Flask's thread.
        def queue_update():
            telegram_app.update_queue.put_nowait(telegram_update)

        bot_loop.call_soon_threadsafe(queue_update)

        return jsonify({"ok": True}), 200

    except Exception:
        logger.exception("Webhook processing error")

        return jsonify({"error": "Internal server error"}), 500


@app.route("/run-update", methods=["POST"])
def run_update_endpoint():
    if not UPDATE_SECRET:
        return jsonify({
            "error": "UPDATE_SECRET is not configured"
        }), 503

    authorization = request.headers.get("Authorization", "")

    if authorization != f"Bearer {UPDATE_SECRET}":
        return jsonify({"error": "Unauthorized"}), 401

    if not bot_ready or bot_loop is None:
        return jsonify({"error": "Bot is not ready"}), 503

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
            "error": "Update failed or timed out; check Render logs",
        }), 500


# =========================================================
# STARTUP
# =========================================================

start_background_bot()
