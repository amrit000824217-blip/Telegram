import os
import html
import hmac
import asyncio
import logging
import sqlite3
import threading

from datetime import datetime, timezone

import psycopg2
from psycopg2.extras import RealDictCursor

from flask import Flask, request, jsonify

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.error import TelegramError

from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)


# ==================================================
# CONFIGURATION
# ==================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = os.getenv("ADMIN_ID", "")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///link_manager.db",
)

WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
UPDATE_SECRET = os.getenv("UPDATE_SECRET", "")

RENDER_EXTERNAL_URL = os.getenv(
    "RENDER_EXTERNAL_URL", ""
).rstrip("/")

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
update_lock = asyncio.Lock()


# ==================================================
# DATABASE
# ==================================================

def is_postgres():
    return DATABASE_URL.startswith(
        ("postgres://", "postgresql://")
    )


def get_connection():
    if is_postgres():
        return psycopg2.connect(DATABASE_URL)

    if DATABASE_URL.startswith("sqlite:///"):
        path = DATABASE_URL[len("sqlite:///"):]
    else:
        path = "link_manager.db"

    connection = sqlite3.connect(
        path,
        timeout=30,
    )

    connection.row_factory = sqlite3.Row

    return connection


def execute_db(
    query,
    params=(),
    fetchone=False,
    fetchall=False,
):
    connection = get_connection()

    try:
        if is_postgres():
            cursor = connection.cursor(
                cursor_factory=RealDictCursor
            )

            query = query.replace("?", "%s")

        else:
            cursor = connection.cursor()

        cursor.execute(query, params)

        result = None

        if fetchone:
            row = cursor.fetchone()
            result = dict(row) if row else None

        elif fetchall:
            result = [
                dict(row)
                for row in cursor.fetchall()
            ]

        connection.commit()
        cursor.close()

        return result

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()


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
            PRIMARY KEY (
                source_chat_id,
                destination_chat_id
            )
        )
    """)

    logger.info("Database initialized successfully")


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
            f"""
            UPDATE {table}
            SET title = ?
            WHERE chat_id = ?
            """,
            (title, chat_id),
        )

    else:
        execute_db(
            f"""
            INSERT INTO {table} (chat_id, title)
            VALUES (?, ?)
            """,
            (chat_id, title),
        )


def get_chats(table):
    if table not in ("sources", "destinations"):
        raise ValueError("Invalid table")

    return execute_db(
        f"""
        SELECT chat_id, title
        FROM {table}
        ORDER BY title
        """,
        fetchall=True,
    )


def get_managed_post(source_id, destination_id):
    return execute_db(
        """
        SELECT
            source_chat_id,
            destination_chat_id,
            message_id,
            invite_link
        FROM managed_posts
        WHERE source_chat_id = ?
          AND destination_chat_id = ?
        """,
        (source_id, destination_id),
        fetchone=True,
    )


def save_managed_post(
    source_id,
    destination_id,
    message_id,
    invite_link,
):
    existing = get_managed_post(
        source_id,
        destination_id,
    )

    if existing:
        execute_db(
            """
            UPDATE managed_posts
            SET message_id = ?, invite_link = ?
            WHERE source_chat_id = ?
              AND destination_chat_id = ?
            """,
            (
                message_id,
                invite_link,
                source_id,
                destination_id,
            ),
        )

    else:
        execute_db(
            """
            INSERT INTO managed_posts (
                source_chat_id,
                destination_chat_id,
                message_id,
                invite_link
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                source_id,
                destination_id,
                message_id,
                invite_link,
            ),
        )


def remove_chat(table, chat_id):
    if table not in ("sources", "destinations"):
        raise ValueError("Invalid table")

    execute_db(
        f"DELETE FROM {table} WHERE chat_id = ?",
        (chat_id,),
    )


# ==================================================
# ADMIN SECURITY
# ==================================================

def is_admin(update):
    user = update.effective_user

    return (
        user is not None
        and ADMIN_ID.isdigit()
        and user.id == int(ADMIN_ID)
    )


async def require_admin(update):
    if is_admin(update):
        return True

    message = update.effective_message

    if message:
        await message.reply_text(
            "⛔ You are not authorized to use this bot."
        )

    return False


# ==================================================
# KEYBOARDS
# ==================================================

def main_menu_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 Source Channels",
                callback_data="sources",
            ),
            InlineKeyboardButton(
                "🌐 Destinations",
                callback_data="destinations",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔗 Publish / Update",
                callback_data="publish",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 Bot Status",
                callback_data="status",
            ),
            InlineKeyboardButton(
                "❓ Help",
                callback_data="help",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 Refresh Menu",
                callback_data="home",
            ),
        ],
    ])


def back_button(destination="home"):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "⬅️ Back",
                callback_data=destination,
            )
        ]
    ])


# ==================================================
# CHAT ID AND TITLE PARSER
# ==================================================

def parse_chat_argument(text):
    """
    Command format:
    /addsource -1001234567890 | Private Channel

    Inline input format:
    -1001234567890 | Private Channel
    """

    value = text.strip()

    if value.startswith("/"):
        parts = value.split(None, 1)

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


# ==================================================
# START AND HELP COMMANDS
# ==================================================

async def start_command(update, context):
    if not await require_admin(update):
        return

    context.user_data.pop("awaiting_chat", None)

    await update.effective_message.reply_text(
        "🤖 TELEGRAM LINK MANAGER\n\n"
        "Welcome to your admin dashboard!\n\n"
        "Choose an option below.",
        reply_markup=main_menu_keyboard(),
    )


async def help_command(update, context):
    if not await require_admin(update):
        return

    await update.effective_message.reply_text(
        "❓ ADMIN PANEL HELP\n\n"
        "📢 Source Channels: Manage private channels.\n"
        "🌐 Destinations: Manage destination chats.\n"
        "🔗 Publish / Update: Publish or edit managed posts.\n"
        "📊 Bot Status: Check configuration.\n\n"
        "Add the bot as an administrator in your "
        "source and destination chats.",
        reply_markup=main_menu_keyboard(),
    )


# ==================================================
# COMMAND-BASED CHANNEL MANAGEMENT
# ==================================================

async def add_source(update, context):
    if not await require_admin(update):
        return

    chat_id, title = parse_chat_argument(
        update.effective_message.text
    )

    if not chat_id:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/addsource -1001234567890 | Private Channel"
        )
        return

    try:
        save_chat("sources", chat_id, title)

        await update.effective_message.reply_text(
            f"✅ Source saved!\n\n"
            f"Name: {title}\n"
            f"ID: {chat_id}",
            reply_markup=back_button("sources"),
        )

    except Exception:
        logger.exception("Add source failed")

        await update.effective_message.reply_text(
            "❌ Could not save source. Check Render logs."
        )


async def add_destination(update, context):
    if not await require_admin(update):
        return

    chat_id, title = parse_chat_argument(
        update.effective_message.text
    )

    if not chat_id:
        await update.effective_message.reply_text(
            "Usage:\n"
            "/adddest -1001234567890 | Public Channel"
        )
        return

    try:
        save_chat("destinations", chat_id, title)

        await update.effective_message.reply_text(
            f"✅ Destination saved!\n\n"
            f"Name: {title}\n"
            f"ID: {chat_id}",
            reply_markup=back_button("destinations"),
        )

    except Exception:
        logger.exception("Add destination failed")

        await update.effective_message.reply_text(
            "❌ Could not save destination."
        )


async def sources_command(update, context):
    if not await require_admin(update):
        return

    await show_chat_list(
        update.effective_message,
        "sources",
        "📢 SOURCE CHANNELS",
    )


async def destinations_command(update, context):
    if not await require_admin(update):
        return

    await show_chat_list(
        update.effective_message,
        "destinations",
        "🌐 DESTINATIONS",
    )


async def show_chat_list(message, table, heading):
    try:
        chats = get_chats(table)

        if not chats:
            await message.reply_text(
                f"{heading}\n\nNo chats configured yet."
            )
            return

        lines = [heading, ""]

        for item in chats:
            lines.append(
                f"• {item['title']}\n"
                f"ID: {item['chat_id']}"
            )

        await message.reply_text("\n\n".join(lines))

    except Exception:
        logger.exception("Could not list chats")

        await message.reply_text(
            "❌ Could not retrieve the chat list."
        )


async def remove_source_command(update, context):
    if not await require_admin(update):
        return

    if not context.args:
        await update.effective_message.reply_text(
            "Usage: /removesource CHAT_ID"
        )
        return

    try:
        remove_chat("sources", context.args[0])

        await update.effective_message.reply_text(
            "✅ Source removed from configuration."
        )

    except Exception:
        logger.exception("Remove source failed")

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

    try:
        remove_chat("destinations", context.args[0])

        await update.effective_message.reply_text(
            "✅ Destination removed from configuration."
        )

    except Exception:
        logger.exception("Remove destination failed")

        await update.effective_message.reply_text(
            "❌ Database error."
        )


# ==================================================
# INVITE LINKS AND MANAGED POSTS
# ==================================================

def format_post_text(title, invite_link):
    safe_title = html.escape(title)
    safe_link = html.escape(invite_link, quote=True)

    today = datetime.now(
        timezone.utc
    ).strftime("%Y-%m-%d")

    return (
        f"🔒 <b>{safe_title}</b>\n\n"
        f"Join this private Telegram channel:\n\n"
        f'👉 <a href="{safe_link}">Request to Join</a>\n\n'
        f"ℹ️ Admin approval is required.\n"
        f"🔄 Updated: {today}"
    )


async def revoke_link(source_id, invite_link):
    if not invite_link:
        return

    try:
        await telegram_app.bot.revoke_chat_invite_link(
            chat_id=source_id,
            invite_link=invite_link,
        )

        logger.info(
            "Old invite link revoked for source %s",
            source_id,
        )

    except TelegramError:
        logger.exception(
            "Could not revoke invite link for source %s",
            source_id,
        )


async def update_managed_posts():
    async with update_lock:
        return await _update_managed_posts()


async def _update_managed_posts():
    sources = get_chats("sources")
    destinations = get_chats("destinations")

    if not sources:
        return "No source channels configured."

    if not destinations:
        return "No destination channels configured."

    success_count = 0
    error_count = 0

    for source in sources:
        source_id = source["chat_id"]
        source_title = source["title"]

        for destination in destinations:
            destination_id = destination["chat_id"]
            new_link = None

            try:
                existing = get_managed_post(
                    source_id,
                    destination_id,
                )

                invite = (
                    await telegram_app.bot.create_chat_invite_link(
                        chat_id=source_id,
                        creates_join_request=True,
                        name=(
                            "Join request "
                            + datetime.now(
                                timezone.utc
                            ).strftime("%Y-%m-%d")
                        ),
                    )
                )

                new_link = invite.invite_link

                post_text = format_post_text(
                    source_title,
                    new_link,
                )

                if existing:
                    message_id = int(existing["message_id"])

                    # Edit the previously managed post.
                    await telegram_app.bot.edit_message_text(
                        chat_id=destination_id,
                        message_id=message_id,
                        text=post_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

                    old_link = existing["invite_link"]

                else:
                    # First publication only.
                    message = await telegram_app.bot.send_message(
                        chat_id=destination_id,
                        text=post_text,
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )

                    message_id = message.message_id
                    old_link = None

                # Store the new link and message ID.
                save_managed_post(
                    source_id,
                    destination_id,
                    message_id,
                    new_link,
                )

                # Revoke old link only after successful publication/edit.
                if old_link and old_link != new_link:
                    await revoke_link(
                        source_id,
                        old_link,
                    )

                success_count += 1

                logger.info(
                    "Updated source %s -> destination %s",
                    source_id,
                    destination_id,
                )

            except Exception:
                error_count += 1

                logger.exception(
                    "Update failed for source %s -> destination %s",
                    source_id,
                    destination_id,
                )

                # Revoke a new link if publication failed.
                if new_link:
                    await revoke_link(
                        source_id,
                        new_link,
                    )

    return (
        "🔗 UPDATE FINISHED\n\n"
        f"✅ Successful: {success_count}\n"
        f"❌ Failed: {error_count}"
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
        logger.exception("Publish command failed")

        await update.effective_message.reply_text(
            "❌ Update failed. Check Render logs."
        )


async def status_command(update, context):
    if not await require_admin(update):
        return

    try:
        sources = get_chats("sources")
        destinations = get_chats("destinations")

        database_name = (
            "PostgreSQL" if is_postgres() else "SQLite"
        )

        await update.effective_message.reply_text(
            "📊 BOT STATUS\n\n"
            f"📢 Sources: {len(sources)}\n"
            f"🌐 Destinations: {len(destinations)}\n"
            f"🗄️ Database: {database_name}\n"
            f"🤖 Bot ready: {bot_ready.is_set()}",
            reply_markup=main_menu_keyboard(),
        )

    except Exception:
        logger.exception("Status command failed")

        await update.effective_message.reply_text(
            "❌ Could not retrieve status."
        )


# ==================================================
# INLINE BUTTON HANDLER
# ==================================================

async def button_handler(update, context):
    query = update.callback_query

    if not is_admin(update):
        await query.answer(
            "⛔ Unauthorized",
            show_alert=True,
        )
        return

    await query.answer()

    data = query.data or ""

    # ---------------- MAIN MENU ----------------

    if data == "home":
        context.user_data.pop("awaiting_chat", None)

        await query.edit_message_text(
            "🤖 TELEGRAM LINK MANAGER\n\n"
            "Choose an option:",
            reply_markup=main_menu_keyboard(),
        )

    # ---------------- SOURCES ----------------

    elif data == "sources":
        context.user_data.pop("awaiting_chat", None)

        try:
            chats = get_chats("sources")

            keyboard = [
                [
                    InlineKeyboardButton(
                        "➕ Add Source",
                        callback_data="addsource",
                    )
                ]
            ]

            for chat in chats:
                keyboard.append([
                    InlineKeyboardButton(
                        "🗑️ " + chat["title"][:35],
                        callback_data=(
                            "remove_source:" + chat["chat_id"]
                        ),
                    )
                ])

            keyboard.append([
                InlineKeyboardButton(
                    "⬅️ Main Menu",
                    callback_data="home",
                )
            ])

            await query.edit_message_text(
                "📢 SOURCE CHANNELS\n\n"
                f"Configured: {len(chats)}\n\n"
                "Add a channel or select one to remove it.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        except Exception:
            logger.exception("Source menu failed")

            await query.edit_message_text(
                "❌ Could not load sources."
            )

    # ---------------- DESTINATIONS ----------------

    elif data == "destinations":
        context.user_data.pop("awaiting_chat", None)

        try:
            chats = get_chats("destinations")

            keyboard = [
                [
                    InlineKeyboardButton(
                        "➕ Add Destination",
                        callback_data="adddest",
                    )
                ]
            ]

            for chat in chats:
                keyboard.append([
                    InlineKeyboardButton(
                        "🗑️ " + chat["title"][:35],
                        callback_data=(
                            "remove_dest:" + chat["chat_id"]
                        ),
                    )
                ])

            keyboard.append([
                InlineKeyboardButton(
                    "⬅️ Main Menu",
                    callback_data="home",
                )
            ])

            await query.edit_message_text(
                "🌐 DESTINATIONS\n\n"
                f"Configured: {len(chats)}\n\n"
                "Add a destination or select one to remove it.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        except Exception:
            logger.exception("Destination menu failed")

            await query.edit_message_text(
                "❌ Could not load destinations."
            )

    # ---------------- ADD SOURCE ----------------

    elif data == "addsource":
        context.user_data["awaiting_chat"] = "sources"

        await query.edit_message_text(
            "➕ ADD SOURCE CHANNEL\n\n"
            "Send the channel ID and name in one message:\n\n"
            "-1001234567890 | My Private Channel\n\n"
            "The bot must be an administrator in that channel.",
            reply_markup=back_button("sources"),
        )

    # ---------------- ADD DESTINATION ----------------

    elif data == "adddest":
        context.user_data["awaiting_chat"] = "destinations"

        await query.edit_message_text(
            "➕ ADD DESTINATION\n\n"
            "Send the destination ID and name:\n\n"
            "-1001234567890 | Public Channel\n\n"
            "The bot must have permission to post messages.",
            reply_markup=back_button("destinations"),
        )

    # ---------------- REMOVE SOURCE ----------------

    elif data.startswith("remove_source:"):
        chat_id = data.split(":", 1)[1]

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Confirm",
                    callback_data="confirm_source:" + chat_id,
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="sources",
                ),
            ]
        ]

        await query.edit_message_text(
            "⚠️ REMOVE SOURCE?\n\n"
            f"Chat ID: {chat_id}\n\n"
            "This removes it from the bot configuration. "
            "Existing posts and invite links are not "
            "automatically cleaned up.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("confirm_source:"):
        chat_id = data.split(":", 1)[1]

        try:
            remove_chat("sources", chat_id)

            await query.edit_message_text(
                "✅ Source removed from configuration.",
                reply_markup=back_button("sources"),
            )

        except Exception:
            logger.exception("Source removal failed")

            await query.edit_message_text(
                "❌ Could not remove source."
            )

    # ---------------- REMOVE DESTINATION ----------------

    elif data.startswith("remove_dest:"):
        chat_id = data.split(":", 1)[1]

        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Confirm",
                    callback_data="confirm_dest:" + chat_id,
                ),
                InlineKeyboardButton(
                    "❌ Cancel",
                    callback_data="destinations",
                ),
            ]
        ]

        await query.edit_message_text(
            "⚠️ REMOVE DESTINATION?\n\n"
            f"Chat ID: {chat_id}\n\n"
            "This removes the destination from configuration. "
            "Existing posts are not automatically deleted.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data.startswith("confirm_dest:"):
        chat_id = data.split(":", 1)[1]

        try:
            remove_chat("destinations", chat_id)

            await query.edit_message_text(
                "✅ Destination removed from configuration.",
                reply_markup=back_button("destinations"),
            )

        except Exception:
            logger.exception("Destination removal failed")

            await query.edit_message_text(
                "❌ Could not remove destination."
            )

    # ---------------- PUBLISH ----------------

    elif data == "publish":
        keyboard = [
            [
                InlineKeyboardButton(
                    "✅ Confirm Publish / Update",
                    callback_data="confirm_publish",
                )
            ],
            [
                InlineKeyboardButton(
                    "⬅️ Main Menu",
                    callback_data="home",
                )
            ],
        ]

        await query.edit_message_text(
            "🔗 PUBLISH / UPDATE\n\n"
            "The bot will create new join-request links.\n"
            "Existing managed posts will be edited.\n\n"
            "Continue?",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    elif data == "confirm_publish":
        await query.edit_message_text(
            "⏳ Updating links and posts...\n\n"
            "Please wait."
        )

        try:
            result = await update_managed_posts()

            await query.edit_message_text(
                result,
                reply_markup=back_button("home"),
            )

        except Exception:
            logger.exception("Inline publish failed")

            await query.edit_message_text(
                "❌ Update failed. Check Render logs.",
                reply_markup=back_button("home"),
            )

    # ---------------- STATUS ----------------

    elif data == "status":
        try:
            sources = get_chats("sources")
            destinations = get_chats("destinations")

            database_name = (
                "PostgreSQL" if is_postgres() else "SQLite"
            )

            keyboard = [
                [
                    InlineKeyboardButton(
                        "🔄 Refresh",
                        callback_data="status",
                    ),
                    InlineKeyboardButton(
                        "⬅️ Main Menu",
                        callback_data="home",
                    ),
                ]
            ]

            await query.edit_message_text(
                "📊 BOT STATUS\n\n"
                f"📢 Sources: {len(sources)}\n"
                f"🌐 Destinations: {len(destinations)}\n"
                f"🗄️ Database: {database_name}\n"
                f"🤖 Bot ready: {bot_ready.is_set()}",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        except Exception:
            logger.exception("Status callback failed")

            await query.edit_message_text(
                "❌ Could not load status."
            )

    # ---------------- HELP ----------------

    elif data == "help":
        await query.edit_message_text(
            "❓ ADMIN PANEL HELP\n\n"
            "📢 Source Channels: Manage private channels.\n"
            "🌐 Destinations: Manage destination chats.\n"
            "🔗 Publish / Update: Create and update links.\n"
            "📊 Bot Status: View configuration.\n\n"
            "Make sure the bot has administrator permissions "
            "in every configured chat.",
            reply_markup=back_button("home"),
        )


# ==================================================
# INLINE TEXT INPUT HANDLER
# ==================================================

async def chat_input_handler(update, context):
    if not is_admin(update):
        return

    table = context.user_data.get("awaiting_chat")

    if table not in ("sources", "destinations"):
        return

    message = update.effective_message

    if not message or not message.text:
        return

    chat_id, title = parse_chat_argument(message.text)

    if not chat_id:
        await message.reply_text(
            "❌ Invalid format.\n\n"
            "Send:\n"
            "-1001234567890 | Channel Name\n\n"
            "Try again or use the Cancel button."
        )
        return

    try:
        save_chat(table, chat_id, title)

        context.user_data.pop("awaiting_chat", None)

        if table == "sources":
            back_to = "sources"
            label = "Source"
        else:
            back_to = "destinations"
            label = "Destination"

        await message.reply_text(
            f"✅ {label} saved successfully!\n\n"
            f"Name: {title}\n"
            f"Chat ID: {chat_id}",
            reply_markup=back_button(back_to),
        )

    except Exception:
        logger.exception("Inline chat input failed")

        await message.reply_text(
            "❌ Could not save chat. Check Render logs."
        )


# ==================================================
# TELEGRAM APPLICATION
# ==================================================

def build_telegram_app():
    application = Application.builder().token(
        BOT_TOKEN
    ).build()

    application.add_handler(
        CommandHandler("start", start_command)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("addsource", add_source)
    )

    application.add_handler(
        CommandHandler("adddest", add_destination)
    )

    application.add_handler(
        CommandHandler("sources", sources_command)
    )

    application.add_handler(
        CommandHandler("destinations", destinations_command)
    )

    application.add_handler(
        CommandHandler("removesource", remove_source_command)
    )

    application.add_handler(
        CommandHandler("removedest", remove_destination_command)
    )

    application.add_handler(
        CommandHandler("publish", publish_command)
    )

    application.add_handler(
        CommandHandler("status", status_command)
    )

    # Inline button callback handler
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    # Text replies used when adding chats from inline menus
    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            chat_input_handler,
        )
    )

    return application


# ==================================================
# BACKGROUND TELEGRAM WORKER
# ==================================================

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
            allowed_updates=[
                "message",
                "callback_query",
            ],
        )

        logger.info("Webhook configured")

    bot_ready.set()

    while True:
        await asyncio.sleep(3600)


def start_background_bot():
    global bot_loop

    if not BOT_TOKEN:
        logger.error(
            "BOT_TOKEN environment variable is missing"
        )
        return

    def run():
        global bot_loop

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        bot_loop = loop

        try:
            loop.run_until_complete(telegram_worker())

        except Exception:
            logger.exception(
                "Telegram worker stopped unexpectedly"
            )

        finally:
            bot_ready.clear()
            loop.close()

    threading.Thread(
        target=run,
        daemon=True,
    ).start()


# ==================================================
# FLASK WEBHOOK
# ==================================================

@app.get("/")
def home():
    return "Telegram Link Manager is running.", 200


@app.get("/health")
def health():
    return jsonify({
        "status": "ok",
        "bot_ready": bot_ready.is_set(),
        "database": (
            "PostgreSQL" if is_postgres() else "SQLite"
        ),
    }), 200


@app.post(WEBHOOK_PATH)
def telegram_webhook():
    if (
        not bot_ready.is_set()
        or telegram_app is None
        or bot_loop is None
    ):
        return jsonify({
            "error": "Bot is starting",
        }), 503

    if WEBHOOK_SECRET:
        supplied = request.headers.get(
            "X-Telegram-Bot-Api-Secret-Token",
            "",
        )

        if not hmac.compare_digest(
            supplied,
            WEBHOOK_SECRET,
        ):
            return jsonify({
                "error": "Unauthorized",
            }), 403

    payload = request.get_json(silent=True)

    if not payload:
        return jsonify({
            "error": "Invalid JSON",
        }), 400

    try:
        update = Update.de_json(
            payload,
            telegram_app.bot,
        )

        future = asyncio.run_coroutine_threadsafe(
            telegram_app.update_queue.put(update),
            bot_loop,
        )

        future.result(timeout=5)

        return jsonify({
            "ok": True,
        }), 200

    except Exception:
        logger.exception("Webhook processing error")

        return jsonify({
            "error": "Webhook processing failed",
        }), 500


# ==================================================
# GITHUB ACTIONS DAILY UPDATE ENDPOINT
# ==================================================

@app.post("/run-update")
def run_update_endpoint():
    if not UPDATE_SECRET:
        return jsonify({
            "error": "UPDATE_SECRET is missing",
        }), 503

    supplied = request.headers.get(
        "Authorization",
        "",
    )

    expected = "Bearer " + UPDATE_SECRET

    if not hmac.compare_digest(
        supplied,
        expected,
    ):
        return jsonify({
            "error": "Unauthorized",
        }), 403

    if not bot_ready.is_set() or bot_loop is None:
        return jsonify({
            "error": "Bot is starting",
        }), 503

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


# ==================================================
# START BOT
# ==================================================

start_background_bot()
