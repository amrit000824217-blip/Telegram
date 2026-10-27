import os
import re
import html
import asyncio
import logging
import threading

from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, request, jsonify

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
UPDATE_SECRET = os.getenv("UPDATE_SECRET", "").strip()

try:
    ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
except ValueError:
    ADMIN_ID = 0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

logger = logging.getLogger("link_manager")

# Do not expose Telegram API URLs containing the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = Flask(__name__)

telegram_app = None
bot_loop = None
bot_ready = False
startup_error = None

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
    with db_connect() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS source_chats (
                    chat_id BIGINT PRIMARY KEY,
                    title TEXT NOT NULL,
                    username TEXT,
                    added_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS destination_chats (
                    chat_id BIGINT PRIMARY KEY,
                    title TEXT NOT NULL,
                    username TEXT,
                    added_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Keep one row for each source/destination pair.
            # invite_link stores the most recently published link.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS managed_posts (
                    source_chat_id TEXT NOT NULL,
                    destination_chat_id TEXT NOT NULL,
                    message_id BIGINT NOT NULL,
                    invite_link TEXT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            cur.execute("""
                ALTER TABLE managed_posts
                ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ
                DEFAULT NOW()
            """)

            # Separate table: one consolidated post per destination.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS managed_destination_posts (
                    destination_chat_id TEXT PRIMARY KEY,
                    message_id BIGINT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Remove duplicate source/destination rows safely.
            cur.execute("""
                DELETE FROM managed_posts a
                USING managed_posts b
                WHERE a.source_chat_id = b.source_chat_id
                  AND a.destination_chat_id = b.destination_chat_id
                  AND a.ctid < b.ctid
            """)

            cur.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS
                managed_posts_pair_uidx
                ON managed_posts (
                    source_chat_id,
                    destination_chat_id
                )
            """)

    logger.info("Database initialized")


# =========================================================
# CHAT DATABASE HELPERS
# =========================================================

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
        ON CONFLICT (chat_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            username = EXCLUDED.username
        """,
        (int(chat_id), title, username),
    )


def remove_chat(table, chat_id):
    if table not in ("source_chats", "destination_chats"):
        raise ValueError("Invalid table")

    db_execute(
        f"DELETE FROM {table} WHERE chat_id = %s",
        (int(chat_id),),
    )


# =========================================================
# CONSOLIDATED POST DATABASE HELPERS
# =========================================================

def get_destination_post(destination_id):
    return db_execute(
        """
        SELECT destination_chat_id, message_id, updated_at
        FROM managed_destination_posts
        WHERE destination_chat_id = %s
        """,
        (str(destination_id),),
        fetch="one",
    )


def save_destination_post(destination_id, message_id):
    db_execute(
        """
        INSERT INTO managed_destination_posts (
            destination_chat_id,
            message_id,
            updated_at
        )
        VALUES (%s, %s, NOW())
        ON CONFLICT (destination_chat_id)
        DO UPDATE SET
            message_id = EXCLUDED.message_id,
            updated_at = NOW()
        """,
        (str(destination_id), int(message_id)),
    )


def get_source_link(source_id, destination_id):
    return db_execute(
        """
        SELECT invite_link, message_id
        FROM managed_posts
        WHERE source_chat_id = %s
          AND destination_chat_id = %s
        """,
        (str(source_id), str(destination_id)),
        fetch="one",
    )


def save_source_link(
    source_id,
    destination_id,
    message_id,
    invite_link,
):
    db_execute(
        """
        INSERT INTO managed_posts (
            source_chat_id,
            destination_chat_id,
            message_id,
            invite_link,
            updated_at
        )
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (source_chat_id, destination_chat_id)
        DO UPDATE SET
            message_id = EXCLUDED.message_id,
            invite_link = EXCLUDED.invite_link,
            updated_at = NOW()
        """,
        (
            str(source_id),
            str(destination_id),
            int(message_id),
            invite_link,
        ),
    )


# =========================================================
# ADMIN CHECK
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
# ADMIN MENUS
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
        label = (chat["title"] or str(chat["chat_id"]))[:35]

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
# ADD SOURCE / DESTINATION
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

    chat_reference = (
        int(argument)
        if re.fullmatch(r"-?\d+", argument)
        else argument
    )

    try:
        chat = await context.bot.get_chat(chat_reference)

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
            getattr(chat, "username", None),
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
            f"Error: {str(exc)[:180]}\n\n"
            "Chat ID/username aur bot admin permissions check karo."
        )


# =========================================================
# POST TEMPLATE
# =========================================================

DISCLAIMER = (
    "‼️ <b>DISCLAIMER</b> ‼️\n"
    "&gt;&gt; Here you'll find entertainment, only for fun "
    "and entertainment purposes. We do not promote any illegal "
    "activity or explicit material. Content is intended only "
    "for adults aged 18+."
)


def make_post_text(source_links):
    """
    source_links:
        [
            {"title": "Channel Name", "link": "https://t.me/..."},
            ...
        ]

    Each source appears once in the consolidated post.
    """

    parts = [
        "╭━━━━━━━━━━━━━━━━━━━━╮",
        "✨️ <b>𝐖𝐄𝐋𝐂𝐎𝐌𝐄 𝐓𝐎 𝐎𝐔𝐑 𝐂𝐎𝐌𝐌𝐔𝐍𝐈𝐓𝐘</b>",
        "╰━━━━━━━━━━━━━━━━━━━━╯",
        "",
    ]

    for item in source_links:
        title = html.escape(item["title"] or "Channel")
        link = html.escape(item["link"], quote=True)

        parts.extend([
            f"<b>{title}</b>",
            f'➡️ <a href="{link}">Join Channel</a>',
            "",
        ])

    parts.extend([
        DISCLAIMER,
        "",
        "<i>THANKS FOR JOINING 🔞</i>",
    ])

    return "\n".join(parts)


# Telegram message text has a size limit.
TELEGRAM_MESSAGE_LIMIT = 4000


def split_source_groups(source_links):
    """
    Split a very large list into chunks that fit Telegram's
    message limit. Each destination normally has one post.
    If the content is too long, multiple managed posts are
    necessary because Telegram limits message length.
    """
    groups = []
    current = []

    for item in source_links:
        trial = current + [item]
        text = make_post_text(trial)

        if len(text) > TELEGRAM_MESSAGE_LIMIT and current:
            groups.append(current)
            current = [item]
        else:
            current = trial

    if current:
        groups.append(current)

    return groups or [[]]


# =========================================================
# INVITE LINK HELPERS
# =========================================================

async def create_join_request_link(source_id):
    invite = await telegram_app.bot.create_chat_invite_link(
        chat_id=int(source_id),
        creates_join_request=True,
        name=datetime.now(timezone.utc).strftime(
            "Auto-%Y%m%d-%H%M%S"
        )[:32],
    )

    return invite.invite_link


async def revoke_link(source_id, invite_link):
    if not invite_link:
        return

    try:
        await telegram_app.bot.revoke_chat_invite_link(
            chat_id=int(source_id),
            invite_link=invite_link,
        )
    except TelegramError:
        logger.warning(
            "Could not revoke old invite link for source %s",
            source_id,
        )


# =========================================================
# PUBLISH / UPDATE ALL SOURCES IN ONE POST PER DESTINATION
# =========================================================

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
            return "❌ No destination chats configured."

        successful_destinations = 0
        failed_destinations = []
        total_new_links = 0

        # One consolidated post per destination.
        for destination in destinations:
            dest_id = int(destination["chat_id"])

            new_links = []
            old_links = []
            created_links = []
            post_saved = False

            try:
                # 1. Generate one fresh join-request link
                #    for every source channel.
                for source in sources:
                    source_id = int(source["chat_id"])

                    old = get_source_link(source_id, dest_id)

                    link = await create_join_request_link(source_id)
                    created_links.append((source_id, link))

                    new_links.append({
                        "source_id": source_id,
                        "title": source["title"] or "Channel",
                        "link": link,
                        "old_link": old["invite_link"] if old else None,
                    })

                # 2. Build all source names and links together.
                groups = split_source_groups(new_links)

                # Normally groups contains one item, so there
                # will be one consolidated post for this destination.
                old_destination_post = get_destination_post(dest_id)

                message_ids = []

                for index, group in enumerate(groups):
                    post_text = make_post_text(group)

                    # Edit the existing consolidated post when possible.
                    # Additional chunks, if needed, are separate posts
                    # because of Telegram's message length limit.
                    existing_message_id = None

                    if index == 0 and old_destination_post:
                        existing_message_id = int(
                            old_destination_post["message_id"]
                        )

                    if existing_message_id:
                        try:
                            await telegram_app.bot.edit_message_text(
                                chat_id=dest_id,
                                message_id=existing_message_id,
                                text=post_text,
                                parse_mode=ParseMode.HTML,
                                disable_web_page_preview=True,
                            )

                            message_id = existing_message_id

                        except BadRequest as exc:
                            if "message is not modified" in str(exc).lower():
                                message_id = existing_message_id
                            elif (
                                "message to edit not found" in str(exc).lower()
                                or "message can't be edited" in str(exc).lower()
                            ):
                                sent = await telegram_app.bot.send_message(
                                    chat_id=dest_id,
                                    text=post_text,
                                    parse_mode=ParseMode.HTML,
                                    disable_web_page_preview=True,
                                )
                                message_id = sent.message_id
                            else:
                                raise
                    else:
                        sent = await telegram_app.bot.send_message(
                            chat_id=dest_id,
                            text=post_text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                        message_id = sent.message_id

                    message_ids.append(message_id)

                # 3. Save the consolidated post ID and new links.
                #    Do not revoke old links until the post is published
                #    and the database writes have succeeded.
                primary_message_id = message_ids[0]

                save_destination_post(
                    dest_id,
                    primary_message_id,
                )

                for item in new_links:
                    save_source_link(
                        item["source_id"],
                        dest_id,
                        primary_message_id,
                        item["link"],
                    )

                    if item["old_link"] and item["old_link"] != item["link"]:
                        old_links.append((
                            item["source_id"],
                            item["old_link"],
                        ))

                post_saved = True
                total_new_links += len(new_links)
                successful_destinations += 1

                # 4. Revoke old links only after the new post
                #    and database records have been saved.
                for source_id, old_link in old_links:
                    await revoke_link(source_id, old_link)

                logger.info(
                    "Consolidated post updated for destination %s",
                    dest_id,
                )

            except Exception as exc:
                logger.exception(
                    "Destination update failed: %s",
                    dest_id,
                )

                # If publishing failed, revoke newly created links
                # that were not successfully saved/published.
                if not post_saved:
                    for source_id, link in created_links:
                        await revoke_link(source_id, link)

                failed_destinations.append(
                    f"{destination['title']}: {str(exc)[:160]}"
                )

        result = (
            "🔄 <b>Update Complete</b>\n\n"
            f"📤 Destinations updated: {successful_destinations}\n"
            f"🔗 New links generated: {total_new_links}\n"
            f"❌ Failed destinations: {len(failed_destinations)}"
        )

        if failed_destinations:
            result += "\n\n<b>Errors:</b>\n" + "\n".join(
                html.escape(item)
                for item in failed_destinations[:8]
            )

        return result


# =========================================================
# DAILY AUTOMATIC UPDATE
# =========================================================

async def daily_update_loop():
    """
    Attempt an update shortly after midnight UTC each day.
    The Render service must be awake for this in-process task
    to run at the scheduled time.
    """

    while True:
        now = datetime.now(timezone.utc)

        next_run = now.replace(
            hour=0,
            minute=10,
            second=0,
            microsecond=0,
        )

        if next_run <= now:
            next_run += timedelta(days=1)

        await asyncio.sleep(
            max(1, (next_run - now).total_seconds())
        )

        try:
            logger.info("Starting daily automatic update")
            result = await update_managed_posts()
            logger.info("Daily update result: %s", result)

        except Exception:
            logger.exception("Daily automatic update failed")


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
        "/status — Check bot status\n"
        "/publish — Publish/update all links\n\n"
        "1. Add source channels.\n"
        "2. Add destination chats.\n"
        "3. Make the bot admin in every chat.\n"
        "4. Use Publish / Update.\n\n"
        "All source links are combined into one destination post "
        "unless the message exceeds Telegram's length limit.",
        reply_markup=main_menu(),
    )


async def status_command(update, context):
    if not await check_admin(update):
        return

    sources = get_chats("source_chats")
    destinations = get_chats("destination_chats")

    await update.effective_message.reply_text(
        f"📊 Bot ready: {bot_ready}\n"
        f"Sources: {len(sources)}\n"
        f"Destinations: {len(destinations)}",
        reply_markup=main_menu(),
    )


async def publish_command(update, context):
    if not await check_admin(update):
        return

    await update.effective_message.reply_text(
        "⚠️ Generate fresh join-request links and update "
        "the consolidated destination posts?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "✅ Confirm",
                callback_data="publish_run",
            ),
            InlineKeyboardButton(
                "❌ Cancel",
                callback_data="home",
            ),
        ]]),
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
            "⚠️ Generate new join-request links and update "
            "the consolidated posts?",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(
                    "✅ Run Update",
                    callback_data="publish_run",
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="home",
                ),
            ]]),
        )
        return

    if data == "publish_run":
        await query.edit_message_text(
            "⏳ Updating links and consolidated posts. Please wait..."
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
            "3. Make the bot an admin in every chat.\n"
            "4. Select Publish / Update.\n\n"
            "All links are join-request links. The bot updates "
            "the consolidated destination post automatically.",
            reply_markup=main_menu(),
        )


# =========================================================
# TELEGRAM STARTUP
# =========================================================

async def start_telegram():
    global telegram_app, bot_ready, startup_error, update_lock

    bot_ready = False
    startup_error = None

    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    if not ADMIN_ID:
        raise RuntimeError("ADMIN_ID is missing or invalid")

    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing")

    init_db()

    update_lock = asyncio.Lock()

    application = Application.builder().token(BOT_TOKEN).build()
    telegram_app = application

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("publish", publish_command))
    application.add_handler(CallbackQueryHandler(button_handler))

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            add_chat_input,
        )
    )

    await application.initialize()

    # Remove any old webhook before starting polling.
    await application.bot.delete_webhook(
        drop_pending_updates=False
    )

    await application.start()

    if application.updater is None:
        raise RuntimeError("Telegram updater is unavailable")

    await application.updater.start_polling(
        allowed_updates=["message", "callback_query"],
        drop_pending_updates=False,
    )

    bot_ready = True

    application.create_task(daily_update_loop())

    logger.info("BOT_READY: Long polling started successfully")


def telegram_thread():
    global bot_loop, bot_ready, startup_error

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    bot_loop = loop

    try:
        loop.run_until_complete(start_telegram())
        loop.run_forever()

    except Exception as exc:
        bot_ready = False
        startup_error = str(exc)
        logger.exception("Telegram startup/runtime failed")


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
        "startup_error": startup_error,
        "database": "PostgreSQL",
        "mode": "long_polling",
    }), 200


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": bot_ready,
        "startup_error": startup_error,
    }), 200


@app.post("/run-update")
def run_update_endpoint():
    if not UPDATE_SECRET:
        return jsonify({
            "error": "UPDATE_SECRET is missing"
        }), 503

    authorization = request.headers.get("Authorization", "")

    if authorization != f"Bearer {UPDATE_SECRET}":
        return jsonify({"error": "Unauthorized"}), 401

    if (
        not bot_ready
        or telegram_app is None
        or bot_loop is None
        or not bot_loop.is_running()
    ):
        return jsonify({
            "error": "Bot not initialized",
            "startup_error": startup_error,
        }), 503

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
# START BACKGROUND BOT
# =========================================================

start_background_bot()
