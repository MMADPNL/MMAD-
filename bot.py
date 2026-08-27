# bot.py
# Python 3.10+
# python-telegram-bot 20+

import os
import re
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
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

OWNER_ID = 8552447077

DB_FILE = "bot.db"

WITHDRAW_MIN = 2.5
GAME_BET = 0.1

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("TRX-BOT")

DB_LOCK = asyncio.Lock()
GAME_LOCK = asyncio.Lock()

# =========================================================
# DATABASE
# =========================================================

def connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    return conn


def now():
    return datetime.now(timezone.utc).isoformat()


def init_db():
    conn = connect()

    conn.execute("""
        PRAGMA journal_mode=WAL
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            kind TEXT NOT NULL,
            description TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS withdrawals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            address TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            processed_at TEXT DEFAULT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            creator_id INTEGER NOT NULL,
            opponent_id INTEGER DEFAULT NULL,
            game_type TEXT NOT NULL,
            bet REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',
            creator_score INTEGER DEFAULT NULL,
            opponent_score INTEGER DEFAULT NULL,
            winner_id INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS game_rounds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            score INTEGER NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # پیش‌فرض: روشن
    conn.execute("""
        INSERT OR IGNORE INTO settings(key, value)
        VALUES('bot_enabled', '1')
    """)

    conn.commit()
    conn.close()


# =========================================================
# SETTINGS
# =========================================================

def get_setting(key, default=None):
    conn = connect()

    row = conn.execute(
        "SELECT value FROM settings WHERE key=?",
        (key,),
    ).fetchone()

    conn.close()

    if row is None:
        return default

    return row["value"]


def set_setting(key, value):
    conn = connect()

    conn.execute("""
        INSERT INTO settings(key, value)
        VALUES(?, ?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
    """, (key, str(value)))

    conn.commit()
    conn.close()


def bot_enabled():
    return get_setting("bot_enabled", "1") == "1"


# =========================================================
# USER
# =========================================================

def ensure_user(user):
    if not user:
        return

    conn = connect()

    conn.execute("""
        INSERT INTO users(
            user_id,
            username,
            first_name,
            balance,
            blocked,
            created_at
        )
        VALUES (?, ?, ?, 0, 0, ?)
        ON CONFLICT(user_id)
        DO UPDATE SET
            username=excluded.username,
            first_name=excluded.first_name
    """, (
        user.id,
        user.username or "",
        user.first_name or "",
        now(),
    ))

    conn.commit()
    conn.close()


def get_balance(user_id):
    conn = connect()

    row = conn.execute("""
        SELECT balance
        FROM users
        WHERE user_id=?
    """, (user_id,)).fetchone()

    conn.close()

    return float(row["balance"]) if row else 0.0


def is_blocked(user_id):
    conn = connect()

    row = conn.execute("""
        SELECT blocked
        FROM users
        WHERE user_id=?
    """, (user_id,)).fetchone()

    conn.close()

    return bool(row and row["blocked"])


def format_trx(amount):
    return f"{float(amount):.2f} TRX"


# =========================================================
# SAFE BALANCE CHANGE
# =========================================================

def change_balance(
    user_id,
    amount,
    kind,
    description="",
):
    conn = connect()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
        """, (user_id,)).fetchone()

        if not row:
            conn.rollback()
            return False

        old_balance = float(row["balance"])
        new_balance = round(
            old_balance + float(amount),
            4,
        )

        if new_balance < 0:
            conn.rollback()
            return False

        conn.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
        """, (
            new_balance,
            user_id,
        ))

        conn.execute("""
            INSERT INTO transactions(
                user_id,
                amount,
                kind,
                description,
                created_at
            )
            VALUES (?, ?, ?, ?, ?)
        """, (
            user_id,
            float(amount),
            kind,
            description,
            now(),
        ))

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        logger.exception("Balance transaction failed")
        return False

    finally:
        conn.close()


def add_balance(user_id, amount, description):
    return change_balance(
        user_id,
        abs(float(amount)),
        "credit",
        description,
    )


def remove_balance(user_id, amount, description):
    return change_balance(
        user_id,
        -abs(float(amount)),
        "debit",
        description,
    )


# =========================================================
# NUMBER
# =========================================================

def normalize_number(text):
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٫٬",
        "0123456789.,",
    )

    return text.translate(table).replace(",", ".")


# =========================================================
# MAIN KEYBOARD
# =========================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="balance",
            ),
            InlineKeyboardButton(
                "💸 برداشت",
                callback_data="withdraw",
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 انتقال",
                callback_data="transfer_help",
            ),
            InlineKeyboardButton(
                "🎮 بازی‌ها",
                callback_data="games",
            ),
        ],
    ])


# =========================================================
# BOT ACTIVE CHECK
# =========================================================

async def check_active(update):
    user = update.effective_user

    if user:
        ensure_user(user)

    if user and user.id == OWNER_ID:
        return True

    if not bot_enabled():
        if update.message:
            await update.message.reply_text(
                "🔴 ربات موقتاً خاموش است."
            )
        elif update.callback_query:
            await update.callback_query.answer(
                "🔴 ربات خاموش است.",
                show_alert=True,
            )

        return False

    return True


# =========================================================
# START
# =========================================================

async def start(update, context):
    user = update.effective_user

    ensure_user(user)

    await update.message.reply_text(
        "🎮 به ربات خوش آمدید!\n\n"
        "💰 برای مشاهده موجودی بنویسید:\n"
        "موجودی\n\n"
        "🔄 برای انتقال روی پیام شخص ریپلای کنید:\n"
        "انتقال 0.1\n\n"
        "💸 برداشت از دکمه زیر.",
        reply_markup=main_keyboard(),
    )


# =========================================================
# BALANCE
# =========================================================

async def balance(update, context):
    if not await check_active(update):
        return

    user = update.effective_user

    if is_blocked(user.id):
        await update.message.reply_text(
            "⛔ شما مسدود شده‌اید."
        )
        return

    await update.message.reply_text(
        "💰 موجودی شما:\n"
        f"{format_trx(get_balance(user.id))}"
    )


# =========================================================
# WITHDRAW START
# =========================================================

async def withdraw_start(update, context):
    if not await check_active(update):
        return

    user = update.effective_user

    if is_blocked(user.id):
        await update.message.reply_text(
            "⛔ شما مسدود شده‌اید."
        )
        return

    context.user_data["withdraw_step"] = "amount"

    await update.message.reply_text(
        "💸 برداشت\n\n"
        f"حداقل برداشت: {format_trx(WITHDRAW_MIN)}\n\n"
        "مبلغ برداشت را ارسال کنید."
    )


# =========================================================
# WITHDRAW AMOUNT
# =========================================================

async def handle_withdraw_amount(
    update,
    context,
):
    user = update.effective_user

    try:
        amount = float(
            normalize_number(
                update.message.text.strip()
            )
        )
    except Exception:
        await update.message.reply_text(
            "❌ مبلغ نامعتبر است."
        )
        return

    amount = round(amount, 4)

    if amount < WITHDRAW_MIN:
        await update.message.reply_text(
            f"❌ حداقل برداشت "
            f"{format_trx(WITHDRAW_MIN)} است."
        )
        return

    balance = get_balance(user.id)

    if balance < amount:
        await update.message.reply_text(
            "❌ موجودی کافی نیست.\n\n"
            f"💰 موجودی: {format_trx(balance)}"
        )

        context.user_data.clear()
        return

    context.user_data["withdraw_amount"] = amount
    context.user_data["withdraw_step"] = "address"

    await update.message.reply_text(
        "🌐 آدرس TRON را ارسال کنید."
    )


# =========================================================
# WITHDRAW ADDRESS
# =========================================================

async def handle_withdraw_address(
    update,
    context,
):
    user = update.effective_user

    address = update.message.text.strip()

    if len(address) < 20:
        await update.message.reply_text(
            "❌ آدرس نامعتبر است."
        )
        return

    amount = context.user_data.get(
        "withdraw_amount"
    )

    if not amount:
        context.user_data.clear()

        await update.message.reply_text(
            "❌ درخواست منقضی شده است."
        )
        return

    # دوباره موجودی چک می‌شود
    if get_balance(user.id) < amount:
        context.user_data.clear()

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )
        return

    conn = connect()

    cursor = conn.execute("""
        INSERT INTO withdrawals(
            user_id,
            amount,
            address,
            status,
            created_at
        )
        VALUES (?, ?, ?, 'pending', ?)
    """, (
        user.id,
        amount,
        address,
        now(),
    ))

    request_id = cursor.lastrowid

    conn.commit()
    conn.close()

    context.user_data.clear()

    text = (
        "💸 درخواست برداشت\n\n"
        f"🆔 درخواست: #{request_id}\n"
        f"👤 کاربر: {user.first_name}\n"
        f"🆔 ID: {user.id}\n"
        f"💰 مبلغ: {format_trx(amount)}\n"
        f"🌐 آدرس:\n`{address}`\n\n"
        "⏳ وضعیت: در انتظار بررسی"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تأیید",
                callback_data=f"wd_ok:{request_id}",
            ),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"wd_no:{request_id}",
            ),
        ],
    ])

    try:
        await context.bot.send_message(
            OWNER_ID,
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    except Exception:
        logger.exception(
            "Could not send withdrawal request"
        )

    await update.message.reply_text(
        "✅ درخواست برداشت ثبت شد.\n\n"
        f"💰 مبلغ: {format_trx(amount)}\n"
        "⏳ منتظر بررسی مدیریت باشید."
    )


# =========================================================
# WITHDRAW CALLBACK
# =========================================================

async def withdrawal_callback(
    update,
    context,
):
    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        await query.answer(
            "⛔ فقط مالک اجازه دارد.",
            show_alert=True,
        )
        return

    action, request_id_text = query.data.split(":")

    try:
        request_id = int(request_id_text)
    except Exception:
        await query.answer(
            "❌ درخواست نامعتبر.",
            show_alert=True,
        )
        return

    conn = connect()

    row = conn.execute("""
        SELECT *
        FROM withdrawals
        WHERE id=?
    """, (request_id,)).fetchone()

    if not row:
        conn.close()

        await query.answer(
            "❌ درخواست پیدا نشد.",
            show_alert=True,
        )
        return

    if row["status"] != "pending":
        conn.close()

        await query.answer(
            "⚠️ این درخواست قبلاً بررسی شده.",
            show_alert=True,
        )
        return

    user_id = row["user_id"]
    amount = float(row["amount"])

    if action == "wd_ok":

        # پرداخت واقعی انجام نمی‌شود.
        # فقط درخواست داخلی ثبت می‌شود.
        conn.execute("""
            UPDATE withdrawals
            SET status='approved',
                processed_at=?
            WHERE id=?
        """, (
            now(),
            request_id,
        ))

        conn.commit()
        conn.close()

        await query.answer(
            "✅ درخواست تأیید شد."
        )

        await query.edit_message_text(
            query.message.text
            + "\n\n"
            "✅ وضعیت: تأیید شد"
        )

        reply_keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💬 پاسخ به کاربر",
                    callback_data=f"wd_reply:{request_id}",
                )
            ]
        ])

        await query.message.reply_text(
            "💬 پاسخ به کاربر:",
            reply_markup=reply_keyboard,
        )

    elif action == "wd_no":

        conn.execute("""
            UPDATE withdrawals
            SET status='rejected',
                processed_at=?
            WHERE id=?
        """, (
            now(),
            request_id,
        ))

        conn.commit()
        conn.close()

        await query.answer(
            "❌ رد شد."
        )

        await query.edit_message_text(
            query.message.text
            + "\n\n"
            "❌ وضعیت: رد شد"
        )


# =========================================================
# OWNER REPLY
# =========================================================

async def withdrawal_reply(
    update,
    context,
):
    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True,
        )
        return

    request_id = int(
        query.data.split(":")[1]
    )

    conn = connect()

    row = conn.execute("""
        SELECT user_id
        FROM withdrawals
        WHERE id=?
    """, (request_id,)).fetchone()

    conn.close()

    if not row:
        await query.answer(
            "❌ پیدا نشد.",
            show_alert=True,
        )
        return

    context.user_data[
        "reply_user_id"
    ] = int(row["user_id"])

    await query.answer()

    await query.message.reply_text(
        "💬 پیام موردنظر را ارسال کنید."
    )


# =========================================================
# TRANSFER
# =========================================================

TRANSFER_RE = re.compile(
    r"^\s*انتقال\s+([0-9۰-۹]+(?:[.,٫][0-9۰-۹]+)?)\s*$"
)


async def transfer_handler(
    update,
    context,
):
    message = update.message
    user = update.effective_user

    if not message or not message.text:
        return False

    match = TRANSFER_RE.match(
        message.text
    )

    if not match:
        return False

    if not await check_active(update):
        return True

    if is_blocked(user.id):
        await message.reply_text(
            "⛔ شما مسدود شده‌اید."
        )
        return True

    if not message.reply_to_message:
        await message.reply_text(
            "❌ روی پیام کاربر ریپلای کنید.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )
        return True

    receiver = (
        message.reply_to_message.from_user
    )

    if not receiver or receiver.is_bot:
        await message.reply_text(
            "❌ گیرنده معتبر نیست."
        )
        return True

    if receiver.id == user.id:
        await message.reply_text(
            "❌ نمی‌توانید به خودتان انتقال دهید."
        )
        return True

    try:
        amount = float(
            normalize_number(
                match.group(1)
            )
        )
    except Exception:
        await message.reply_text(
            "❌ مبلغ نامعتبر."
        )
        return True

    amount = round(amount, 4)

    if amount <= 0:
        await message.reply_text(
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return True

    ensure_user(receiver)

    async with DB_LOCK:

        conn = connect()

        try:
            conn.execute("BEGIN IMMEDIATE")

            sender_row = conn.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
            """, (user.id,)).fetchone()

            receiver_row = conn.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
            """, (receiver.id,)).fetchone()

            if not sender_row or not receiver_row:
                conn.rollback()

                await message.reply_text(
                    "❌ کاربر پیدا نشد."
                )
                return True

            sender_balance = float(
                sender_row["balance"]
            )

            if sender_balance < amount:
                conn.rollback()

                await message.reply_text(
                    "❌ موجودی کافی نیست."
                )
                return True

            conn.execute("""
                UPDATE users
                SET balance=balance-?
                WHERE user_id=?
            """, (
                amount,
                user.id,
            ))

            conn.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
            """, (
                amount,
                receiver.id,
            ))

            conn.execute("""
                INSERT INTO transactions
                (user_id, amount, kind, description, created_at)
                VALUES (?, ?, 'transfer_out', ?, ?)
            """, (
                user.id,
                -amount,
                f"انتقال به {receiver.id}",
                now(),
            ))

            conn.execute("""
                INSERT INTO transactions
                (user_id, amount, kind, description, created_at)
                VALUES (?, ?, 'transfer_in', ?, ?)
            """, (
                receiver.id,
                amount,
                f"انتقال از {user.id}",
                now(),
            ))

            conn.commit()

        except Exception:
            conn.rollback()

            logger.exception(
                "Transfer error"
            )

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )

            return True

        finally:
            conn.close()

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {format_trx(amount)}\n"
        f"👤 گیرنده: {receiver.first_name}"
    )

    return True


# =========================================================
# GAME PARSER
# =========================================================

GAME_RE = re.compile(
    r"^\s*1\s+"
    r"(تاس|بولینگ|دارت|بسکتبال)"
    r"\s+"
    r"([0-9۰-۹]+(?:[.,٫][0-9۰-۹]+)?)\s*$"
)


def game_name_to_type(name):
    names = {
        "تاس": "dice",
        "بولینگ": "bowling",
        "دارت": "darts",
        "بسکتبال": "basketball",
    }

    return names.get(name)


def game_keyboard(game_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join_friend:{game_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_game:{game_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel_game:{game_id}",
            ),
        ],
    ])


# =========================================================
# GAME START
# =========================================================

async def start_game(
    update,
    context,
):
    message = update.message
    user = update.effective_user

    if not message or message.chat.type == ChatType.PRIVATE:
        return False

    match = GAME_RE.match(
        message.text or ""
    )

    if not match:
        return False

    if not await check_active(update):
        return True

    if is_blocked(user.id):
        await message.reply_text(
            "⛔ شما مسدود شده‌اید."
        )
        return True

    game_name = match.group(1)

    try:
        bet = float(
            normalize_number(
                match.group(2)
            )
        )
    except Exception:
        await message.reply_text(
            "❌ مبلغ نامعتبر."
        )
        return True

    bet = round(bet, 4)

    if bet != GAME_BET:
        await message.reply_text(
            f"❌ مبلغ بازی باید "
            f"{format_trx(GAME_BET)} باشد."
        )
        return True

    if get_balance(user.id) < bet:
        await message.reply_text(
            "❌ موجودی کافی نیست."
        )
        return True

    async with GAME_LOCK:

        # جلوگیری از چند بازی همزمان سازنده
        conn = connect()

        active = conn.execute("""
            SELECT id
            FROM games
            WHERE creator_id=?
            AND status IN ('waiting','playing')
            LIMIT 1
        """, (user.id,)).fetchone()

        if active:
            conn.close()

            await message.reply_text(
                "❌ شما یک بازی فعال دارید."
            )
            return True

        # کسر شرط از سازنده
        conn.close()

        if not remove_balance(
            user.id,
            bet,
            "شرط بازی",
        ):
            await message.reply_text(
                "❌ کسر مبلغ بازی انجام نشد."
            )
            return True

        conn = connect()

        cursor = conn.execute("""
            INSERT INTO games(
                chat_id,
                creator_id,
                opponent_id,
                game_type,
                bet,
                status,
                created_at
            )
            VALUES (?, ?, NULL, ?, ?, 'waiting', ?)
        """, (
            message.chat.id,
            user.id,
            game_name_to_type(game_name),
            bet,
            now(),
        ))

        game_id = cursor.lastrowid

        conn.commit()
        conn.close()

    await message.reply_text(
        "🎮 بازی جدید ساخته شد!\n\n"
        f"🎯 نوع بازی: {game_name}\n"
        f"💰 مبلغ: {format_trx(bet)}\n"
        f"👤 سازنده: {user.first_name}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=game_keyboard(game_id),
    )

    return True


# =========================================================
# CALLBACK ROUTER
# =========================================================

async def callback_router(
    update,
    context,
):
    query = update.callback_query

    data = query.data or ""

    if data == "balance":

        user = query.from_user

        ensure_user(user)

        await query.answer()

        await query.message.reply_text(
            "💰 موجودی شما:\n"
            f"{format_trx(get_balance(user.id))}"
        )

        return

    if data == "withdraw":

        user = query.from_user

        ensure_user(user)

        if is_blocked(user.id):
            await query.answer(
                "⛔ شما مسدود شده‌اید.",
                show_alert=True,
            )
            return

        context.user_data[
            "withdraw_step"
        ] = "amount"

        await query.answer()

        await query.message.reply_text(
            "💸 برداشت\n\n"
            f"حداقل برداشت: "
            f"{format_trx(WITHDRAW_MIN)}\n\n"
            "مبلغ برداشت را ارسال کنید."
        )

        return

    if data == "transfer_help":

        await query.answer()

        await query.message.reply_text(
            "🔄 انتقال\n\n"
            "روی پیام شخص ریپلای کنید و بنویسید:\n\n"
            "انتقال 0.1\n"
            "یا\n"
            "انتقال ۰.۱"
        )

        return

    if data == "games":

        await query.answer()

        await query.message.reply_text(
            "🎮 بازی‌ها\n\n"
            "1 تاس 0.1\n"
            "1 بولینگ 0.1\n"
            "1 دارت 0.1\n"
            "1 بسکتبال 0.1"
        )

        return

    if data.startswith("wd_ok:") or data.startswith("wd_no:"):
        await withdrawal_callback(
            update,
            context,
        )
        return

    if data.startswith("wd_reply:"):
        await withdrawal_reply(
            update,
            context,
        )
        return

    if data.startswith("admin_"):
        await admin_callback(
            update,
            context,
        )
        return

    # بازی‌ها
    if data.startswith("join_friend:"):
        await query.answer(
            "بخش بازی دوستان در حال آماده‌سازی است.",
            show_alert=True,
        )
        return

    if data.startswith("bot_game:"):
        await query.answer(
            "بازی با ربات انتخاب شد.",
            show_alert=True,
        )
        return

    if data.startswith("cancel_game:"):

        game_id = int(
            data.split(":")[1]
        )

        user = query.from_user

        conn = connect()

        row = conn.execute("""
            SELECT creator_id, bet, status
            FROM games
            WHERE id=?
        """, (game_id,)).fetchone()

        if not row:
            conn.close()

            await query.answer(
                "بازی پیدا نشد.",
                show_alert=True,
            )
            return

        if row["creator_id"] != user.id:
            conn.close()

            await query.answer(
                "فقط سازنده بازی می‌تواند لغو کند.",
                show_alert=True,
            )
            return

        if row["status"] != "waiting":
            conn.close()

            await query.answer(
                "این بازی دیگر قابل لغو نیست.",
                show_alert=True,
            )
            return

        conn.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            AND status='waiting'
        """, (game_id,))

        conn.commit()
        conn.close()

        add_balance(
            user.id,
            float(row["bet"]),
            "بازگشت شرط بازی لغوشده",
        )

        await query.answer(
            "بازی لغو شد."
        )

        await query.edit_message_text(
            "❌ بازی لغو شد و مبلغ به موجودی برگشت."
        )

        return


# =========================================================
# ADMIN PANEL
# =========================================================

def admin_keyboard():
    status = (
        "🟢 روشن"
        if bot_enabled()
        else "🔴 خاموش"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                status,
                callback_data="admin_toggle",
            ),
        ],
        [
            InlineKeyboardButton(
                "💰 موجودی کاربران",
                callback_data="admin_balances",
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data="admin_add",
            ),
            InlineKeyboardButton(
                "➖ کسر موجودی",
                callback_data="admin_remove",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats",
            ),
        ],
    ])


async def admin_command(
    update,
    context,
):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text(
            "⛔ دسترسی ندارید."
        )
        return

    await update.message.reply_text(
        "👑 پنل مدیریت\n\n"
        "مدیریت ربات:",
        reply_markup=admin_keyboard(),
    )


# =========================================================
# ADMIN CALLBACK
# =========================================================

async def admin_callback(
    update,
    context,
):
    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True,
        )
        return

    data = query.data

    if data == "admin_toggle":

        new_status = not bot_enabled()

        set_setting(
            "bot_enabled",
            "1" if new_status else "0",
        )

        await query.answer(
            "وضعیت تغییر کرد."
        )

        try:
            await query.edit_message_reply_markup(
                reply_markup=admin_keyboard()
            )
        except Exception:
            pass

        return

    if data == "admin_stats":

        conn = connect()

        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        total = conn.execute(
            "SELECT COALESCE(SUM(balance),0) AS b FROM users"
        ).fetchone()["b"]

        pending = conn.execute("""
            SELECT COUNT(*) AS c
            FROM withdrawals
            WHERE status='pending'
        """).fetchone()["c"]

        games = conn.execute("""
            SELECT COUNT(*) AS c
            FROM games
        """).fetchone()["c"]

        conn.close()

        await query.answer()

        await query.message.reply_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users}\n"
            f"💰 مجموع موجودی: {format_trx(total)}\n"
            f"💸 برداشت در انتظار: {pending}\n"
            f"🎮 تعداد بازی‌ها: {games}"
        )

        return

    if data == "admin_balances":

        conn = connect()

        rows = conn.execute("""
            SELECT user_id,
                   username,
                   first_name,
                   balance
            FROM users
            ORDER BY balance DESC
            LIMIT 50
        """).fetchall()

        conn.close()

        text = "💰 موجودی کاربران\n\n"

        for row in rows:

            display = (
                f"@{row['username']}"
                if row["username"]
                else row["first_name"]
            )

            text += (
                f"👤 {display}\n"
                f"🆔 {row['user_id']}\n"
                f"💰 {format_trx(row['balance'])}\n\n"
            )

        await query.answer()

        await query.message.reply_text(
            text or "کاربری وجود ندارد."
        )

        return

    if data == "admin_add":

        context.user_data[
            "admin_balance_action"
        ] = "add"

        await query.answer()

        await query.message.reply_text(
            "➕ افزایش موجودی\n\n"
            "فرمت:\n"
            "USER_ID AMOUNT\n\n"
            "مثال:\n"
            "123456789 10"
        )

        return

    if data == "admin_remove":

        context.user_data[
            "admin_balance_action"
        ] = "remove"

        await query.answer()

        await query.message.reply_text(
            "➖ کسر موجودی\n\n"
            "فرمت:\n"
            "USER_ID AMOUNT\n\n"
            "مثال:\n"
            "123456789 10"
        )

        return


# =========================================================
# TEXT ROUTER
# =========================================================

async def text_router(
    update,
    context,
):
    if not update.message:
        return

    user = update.effective_user

    ensure_user(user)

    text = (
        update.message.text or ""
    ).strip()

    # پاسخ مالک به کاربر
    reply_user_id = context.user_data.get(
        "reply_user_id"
    )

    if (
        user.id == OWNER_ID
        and reply_user_id
        and update.effective_chat.type == ChatType.PRIVATE
    ):
        try:
            await context.bot.send_message(
                reply_user_id,
                "💬 پیام مدیریت:\n\n"
                + text,
            )

            await update.message.reply_text(
                "✅ پیام ارسال شد."
            )

        except Exception:
            await update.message.reply_text(
                "❌ ارسال نشد."
            )

        context.user_data.pop(
            "reply_user_id",
            None,
        )

        return

    # مدیریت افزایش / کسر
    admin_action = context.user_data.get(
        "admin_balance_action"
    )

    if (
        user.id == OWNER_ID
        and admin_action
        and update.effective_chat.type == ChatType.PRIVATE
    ):

        parts = text.split()

        if len(parts) != 2:
            await update.message.reply_text(
                "❌ فرمت اشتباه است."
            )
            return

        try:
            target_id = int(parts[0])

            amount = float(
                normalize_number(parts[1])
            )

            amount = round(
                amount,
                4,
            )

        except Exception:
            await update.message.reply_text(
                "❌ مقدار نامعتبر."
            )
            return

        if amount <= 0:
            await update.message.reply_text(
                "❌ مبلغ باید بیشتر از صفر باشد."
            )
            return

        ensure_user_by_id(target_id)

        if admin_action == "add":

            ok = add_balance(
                target_id,
                amount,
                "افزایش توسط مالک",
            )

        else:

            ok = remove_balance(
                target_id,
                amount,
                "کسر توسط مالک",
            )

        context.user_data.pop(
            "admin_balance_action",
            None,
        )

        if not ok:
            await update.message.reply_text(
                "❌ عملیات انجام نشد."
            )
            return

        await update.message.reply_text(
            "✅ انجام شد.\n\n"
            f"🆔 کاربر: {target_id}\n"
            f"💰 موجودی جدید: "
            f"{format_trx(get_balance(target_id))}"
        )

        return

    # انتقال
    if await transfer_handler(
        update,
        context,
    ):
        return

    # موجودی
    if text in (
        "موجودی",
        "موجودی من",
    ):

        if not await check_active(update):
            return

        if is_blocked(user.id):
            await update.message.reply_text(
                "⛔ شما مسدود شده‌اید."
            )
            return

        await update.message.reply_text(
            "💰 موجودی شما:\n"
            f"{format_trx(get_balance(user.id))}"
        )

        return

    # برداشت
    withdraw_step = context.user_data.get(
        "withdraw_step"
    )

    if withdraw_step == "amount":

        if not await check_active(update):
            return

        await handle_withdraw_amount(
            update,
            context,
        )

        return

    if withdraw_step == "address":

        if not await check_active(update):
            return

        await handle_withdraw_address(
            update,
            context,
        )

        return

    # بازی
    await start_game(
        update,
        context,
    )


def ensure_user_by_id(user_id):
    conn = connect()

    conn.execute("""
        INSERT OR IGNORE INTO users(
            user_id,
            username,
            first_name,
            balance,
            blocked,
            created_at
        )
        VALUES (?, '', '', 0, 0, ?)
    """, (
        user_id,
        now(),
    ))

    conn.commit()
    conn.close()


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context,
):
    logger.error(
        "Unhandled error: %s",
        context.error,
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callback_router,
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info(
        "BOT STARTED"
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
