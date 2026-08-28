# ============================================================
# BET_BT - Telegram Group Games Bot
# Python 3.10+
# python-telegram-bot 20+
#
# TRX = INTERNAL BALANCE ONLY
# NO REAL TRON TRANSACTIONS
#
# Examples in group:
#   1 تاس 0.1
#   3 تاس 0.1
#   5 بولینگ 1
#   3 دارت 0.5
#   4 بسکتبال 0.2
#
# First number  = number of rolls
# Last number   = bet
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)


# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = 8552447077

CHANNEL_USERNAME = "@zobxt"
CHANNEL_URL = "https://t.me/zobxt"

DB_FILE = "bet_bt.db"

MIN_BET = 0.1

# طبق تنظیم قبلی:
WIN_PAYOUT = 0.19

REFERRAL_REWARD = 0.05

GAME_TIMEOUT = 300

MAX_ROLLS = 20


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BET_BT")


# ============================================================
# DATABASE
# ============================================================

def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = db_connect()

    cur = conn.cursor()

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance REAL NOT NULL DEFAULT 0,
            referred_by INTEGER DEFAULT NULL,
            referral_paid INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # GAMES
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER DEFAULT 0,

            creator_id INTEGER NOT NULL,
            opponent_id INTEGER DEFAULT NULL,

            game_type TEXT NOT NULL,

            bet REAL NOT NULL,

            roll_count INTEGER NOT NULL DEFAULT 1,

            mode TEXT NOT NULL DEFAULT 'waiting',

            status TEXT NOT NULL DEFAULT 'waiting',

            creator_roll INTEGER DEFAULT NULL,
            opponent_roll INTEGER DEFAULT NULL,

            creator_total INTEGER DEFAULT NULL,
            opponent_total INTEGER DEFAULT NULL,

            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)

    # --------------------------------------------------------
    # TRANSACTIONS
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount REAL NOT NULL,
            kind TEXT NOT NULL,
            reference TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # SETTINGS
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute("""
        INSERT OR IGNORE INTO settings(key, value)
        VALUES('enabled', '1')
    """)

    # --------------------------------------------------------
    # REFERRALS
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            invited_id INTEGER NOT NULL UNIQUE,
            reward REAL NOT NULL DEFAULT 0.05,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # --------------------------------------------------------
    # ANTI DOUBLE OPERATION
    # --------------------------------------------------------

    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_operations (
            operation_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL
        )
    """)

    # --------------------------------------------------------
    # MIGRATION FOR OLD DATABASE
    # --------------------------------------------------------

    columns = cur.execute(
        "PRAGMA table_info(games)"
    ).fetchall()

    column_names = [
        row["name"]
        for row in columns
    ]

    if "roll_count" not in column_names:

        cur.execute("""
            ALTER TABLE games
            ADD COLUMN roll_count INTEGER NOT NULL DEFAULT 1
        """)

    if "creator_total" not in column_names:

        cur.execute("""
            ALTER TABLE games
            ADD COLUMN creator_total INTEGER DEFAULT NULL
        """)

    if "opponent_total" not in column_names:

        cur.execute("""
            ALTER TABLE games
            ADD COLUMN opponent_total INTEGER DEFAULT NULL
        """)

    conn.commit()

    conn.close()


# ============================================================
# GENERAL HELPERS
# ============================================================

def normalize_digits(text: str) -> str:

    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return text.translate(table)


def fmt(amount: float) -> str:

    return (
        f"{float(amount):.2f}"
        .rstrip("0")
        .rstrip(".")
    )


def get_user(user_id: int):

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM users
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    conn.close()

    return row


def ensure_user(user, referred_by=None):

    if not user:
        return

    conn = db_connect()

    existing = conn.execute(
        """
        SELECT user_id
        FROM users
        WHERE user_id = ?
        """,
        (user.id,)
    ).fetchone()

    if existing:

        conn.execute(
            """
            UPDATE users
            SET username = ?,
                first_name = ?
            WHERE user_id = ?
            """,
            (
                user.username or "",
                user.first_name or "",
                user.id,
            )
        )

    else:

        if referred_by == user.id:
            referred_by = None

        conn.execute(
            """
            INSERT INTO users(
                user_id,
                username,
                first_name,
                balance,
                referred_by
            )
            VALUES (?, ?, ?, 0, ?)
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                referred_by,
            )
        )

    conn.commit()

    conn.close()


def is_owner(user_id: int) -> bool:

    return user_id == OWNER_ID


# ============================================================
# BOT ENABLE / DISABLE
# ============================================================

def bot_enabled() -> bool:

    conn = db_connect()

    row = conn.execute(
        """
        SELECT value
        FROM settings
        WHERE key = 'enabled'
        """
    ).fetchone()

    conn.close()

    if not row:
        return True

    return row["value"] == "1"


def set_bot_enabled(enabled: bool):

    conn = db_connect()

    conn.execute(
        """
        INSERT INTO settings(key, value)
        VALUES('enabled', ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        ("1" if enabled else "0",)
    )

    conn.commit()

    conn.close()


# ============================================================
# BALANCE
# ============================================================

def get_balance(user_id: int) -> float:

    conn = db_connect()

    row = conn.execute(
        """
        SELECT balance
        FROM users
        WHERE user_id = ?
        """,
        (user_id,)
    ).fetchone()

    conn.close()

    if not row:
        return 0.0

    return float(row["balance"])


def change_balance(
    user_id: int,
    amount: float,
    kind: str,
    reference: str = "",
) -> bool:

    amount = round(float(amount), 4)

    conn = db_connect()

    try:

        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        if not row:

            conn.rollback()

            return False

        old_balance = float(row["balance"])

        new_balance = round(
            old_balance + amount,
            4
        )

        # ضد منفی شدن موجودی
        if new_balance < -0.00001:

            conn.rollback()

            return False

        conn.execute(
            """
            UPDATE users
            SET balance = ?
            WHERE user_id = ?
            """,
            (
                new_balance,
                user_id,
            )
        )

        conn.execute(
            """
            INSERT INTO transactions(
                user_id,
                amount,
                kind,
                reference
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                amount,
                kind,
                reference,
            )
        )

        conn.commit()

        return True

    except Exception:

        conn.rollback()

        logger.exception(
            "change_balance failed"
        )

        return False

    finally:

        conn.close()


# ============================================================
# ANTI DOUBLE OPERATION
# ============================================================

def lock_operation(operation_id: str) -> bool:

    conn = db_connect()

    try:

        conn.execute(
            """
            INSERT INTO processed_operations(
                operation_id,
                created_at
            )
            VALUES (?, ?)
            """,
            (
                operation_id,
                time.time(),
            )
        )

        conn.commit()

        return True

    except sqlite3.IntegrityError:

        conn.rollback()

        return False

    finally:

        conn.close()


# ============================================================
# FORCE JOIN
# ============================================================

async def check_join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
) -> bool:

    user = update.effective_user

    if not user:
        return False

    if is_owner(user.id):
        return True

    try:

        member = await context.bot.get_chat_member(
            CHANNEL_USERNAME,
            user.id
        )

        status = str(member.status)

        if status in (
            "member",
            "administrator",
            "creator",
        ):
            return True

    except TelegramError:

        pass

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 عضویت در کانال",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_join"
            )
        ]
    ])

    text = (
        "🔒 برای استفاده از ربات ابتدا "
        "در کانال عضو شوید.\n\n"
        "بعد از عضویت روی «بررسی عضویت» بزن."
    )

    try:

        if update.callback_query:

            await update.callback_query.answer(
                "ابتدا در کانال عضو شوید.",
                show_alert=True
            )

            await update.callback_query.message.reply_text(
                text,
                reply_markup=keyboard
            )

        elif update.effective_message:

            await update.effective_message.reply_text(
                text,
                reply_markup=keyboard
            )

    except TelegramError:

        pass

    return False


# ============================================================
# PRIVATE KEYBOARD
# ============================================================

def private_keyboard():

    return ReplyKeyboardMarkup(
        [
            [
                KeyboardButton("💰 موجودی"),
                KeyboardButton("🎮 بازی‌ها"),
            ],
            [
                KeyboardButton("👥 زیرمجموعه"),
                KeyboardButton("💸 انتقال"),
            ],
            [
                KeyboardButton("ℹ️ راهنما"),
            ],
        ],
        resize_keyboard=True,
    )


# ============================================================
# START
# ============================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    referred_by = None

    if context.args:

        arg = str(
            context.args[0]
        ).strip()

        if arg.startswith("ref_"):

            try:

                referred_by = int(
                    arg.replace(
                        "ref_",
                        ""
                    )
                )

            except Exception:

                referred_by = None

    ensure_user(
        user,
        referred_by
    )

    # پرداخت پاداش زیرمجموعه فقط یک بار
    process_referral(
        user.id
    )

    if not await check_join(
        update,
        context
    ):
        return

    await update.message.reply_text(
        f"""
🎮 BET_BT

سلام {user.first_name or 'دوست عزیز'} 👋

🎲 تاس
🎳 بولینگ
🎯 دارت
🏀 بسکتبال

🤖 بازی با ربات
👥 بازی با دوستان

💰 موجودی داخلی TRX

💸 انتقال با Reply

👥 پاداش زیرمجموعه: 0.05

برای بازی در گپ:

1 تاس 0.1

یا:

3 تاس 0.1
""",
        reply_markup=private_keyboard()
    )


# ============================================================
# BALANCE COMMAND
# ============================================================

async def balance(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if not await check_join(
        update,
        context
    ):
        return

    amount = get_balance(
        user.id
    )

    await update.effective_message.reply_text(
        f"""
💰 موجودی

👤 {user.first_name or 'کاربر'}

💎 {fmt(amount)} TRX
"""
    )


# ============================================================
# REFERRAL
# ============================================================

def process_referral(user_id: int):

    conn = db_connect()

    try:

        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT referred_by,
                   referral_paid
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        if not row:

            conn.rollback()
            return

        inviter_id = row["referred_by"]

        if not inviter_id:

            conn.rollback()
            return

        if int(row["referral_paid"]) == 1:

            conn.rollback()
            return

        if inviter_id == user_id:

            conn.rollback()
            return

        inviter = conn.execute(
            """
            SELECT user_id
            FROM users
            WHERE user_id = ?
            """,
            (inviter_id,)
        ).fetchone()

        if not inviter:

            conn.rollback()
            return

        conn.execute(
            """
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
            """,
            (
                REFERRAL_REWARD,
                inviter_id,
            )
        )

        conn.execute(
            """
            UPDATE users
            SET referral_paid = 1
            WHERE user_id = ?
            """,
            (user_id,)
        )

        conn.execute(
            """
            INSERT OR IGNORE INTO referrals(
                inviter_id,
                invited_id,
                reward
            )
            VALUES (?, ?, ?)
            """,
            (
                inviter_id,
                user_id,
                REFERRAL_REWARD,
            )
        )

        conn.execute(
            """
            INSERT INTO transactions(
                user_id,
                amount,
                kind,
                reference
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                inviter_id,
                REFERRAL_REWARD,
                "referral",
                str(user_id),
            )
        )

        conn.commit()

    except Exception:

        conn.rollback()

        logger.exception(
            "Referral failed"
        )

    finally:

        conn.close()


async def referral(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if not await check_join(
        update,
        context
    ):
        return

    bot_username = context.bot.username

    link = (
        f"https://t.me/"
        f"{bot_username}"
        f"?start=ref_{user.id}"
    )

    conn = db_connect()

    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM referrals
        WHERE inviter_id = ?
        """,
        (user.id,)
    ).fetchone()

    count = int(row["c"])

    conn.close()

    await update.effective_message.reply_text(
        f"""
👥 زیرمجموعه

🔗 لینک دعوت:

{link}

🎁 پاداش هر دعوت: 0.05 TRX

👤 تعداد زیرمجموعه: {count}
"""
    )


# ============================================================
# TRANSFER
# ============================================================

def parse_amount(text: str):

    text = normalize_digits(
        text
    ).replace(",", ".").strip()

    match = re.search(
        r"(\d+(?:\.\d+)?)",
        text
    )

    if not match:
        return None

    try:

        amount = round(
            float(match.group(1)),
            4
        )

        if amount <= 0:
            return None

        return amount

    except Exception:

        return None


async def transfer_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.effective_message.reply_text(
        """
💸 انتقال با Reply

روی پیام کاربر Reply کن و بنویس:

انتقال 0.1

مثال:

انتقال 1
"""
    )


async def transfer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if not await check_join(
        update,
        context
    ):
        return

    reply = update.message.reply_to_message

    if not reply:

        await update.message.reply_text(
            "❌ باید روی پیام کاربر Reply کنی.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )

        return

    target = reply.from_user

    if not target:
        return

    if target.id == user.id:

        await update.message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    if target.is_bot:

        await update.message.reply_text(
            "❌ انتقال به ربات مجاز نیست."
        )

        return

    amount = parse_amount(
        update.message.text
    )

    if amount is None or amount < 0.01:

        await update.message.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    ensure_user(target)

    operation_id = (
        f"transfer:"
        f"{update.effective_chat.id}:"
        f"{update.message.message_id}"
    )

    if not lock_operation(
        operation_id
    ):

        await update.message.reply_text(
            "⚠️ این انتقال قبلاً پردازش شده است."
        )

        return

    # کسر
    if not change_balance(
        user.id,
        -amount,
        "transfer_out",
        f"to:{target.id}",
    ):

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    # واریز
    if not change_balance(
        target.id,
        amount,
        "transfer_in",
        f"from:{user.id}",
    ):

        # برگشت پول
        change_balance(
            user.id,
            amount,
            "transfer_refund",
            f"failed_to:{target.id}",
        )

        await update.message.reply_text(
            "❌ انتقال انجام نشد؛ "
            "مبلغ برگشت داده شد."
        )

        return

    await update.message.reply_text(
        f"""
✅ انتقال انجام شد.

👤 فرستنده: {user.first_name}

👤 گیرنده: {target.first_name}

💎 مبلغ: {fmt(amount)} TRX
"""
    )


# ============================================================
# GAME CONFIG
# ============================================================

GAME_TYPES = {

    "dice": {
        "name": "تاس",
        "aliases": [
            "تاس",
            "dice",
        ],
        "emoji": "🎲",
        "telegram_dice": "🎲",
    },

    "bowling": {
        "name": "بولینگ",
        "aliases": [
            "بولینگ",
            "bowling",
        ],
        "emoji": "🎳",
        "telegram_dice": "🎳",
    },

    "darts": {
        "name": "دارت",
        "aliases": [
            "دارت",
            "darts",
        ],
        "emoji": "🎯",
        "telegram_dice": "🎯",
    },

    "basketball": {
        "name": "بسکتبال",
        "aliases": [
            "بسکتبال",
            "basketball",
        ],
        "emoji": "🏀",
        "telegram_dice": "🏀",
    },
}


def detect_game(text: str):

    normalized = normalize_digits(
        text
    ).lower()

    for key, data in GAME_TYPES.items():

        for alias in data["aliases"]:

            if alias in normalized:
                return key

    return None


# ============================================================
# PARSE GAME
# ============================================================

def parse_group_game(text: str):

    normalized = normalize_digits(
        text
    ).strip()

    game_type = detect_game(
        normalized
    )

    if not game_type:
        return None

    # --------------------------------------------------------
    # تعداد رول + نام بازی + مبلغ
    #
    # 1 تاس 0.1
    # 3 تاس 0.1
    # 10 بولینگ 1
    # --------------------------------------------------------

    pattern = (
        r"^\s*"
        r"(\d+)"
        r"\s+"
        r"(تاس|بولینگ|دارت|بسکتبال|"
        r"dice|bowling|darts|basketball)"
        r"\s+"
        r"(\d+(?:[.,]\d+)?)"
        r"\s*$"
    )

    match = re.match(
        pattern,
        normalized,
        re.IGNORECASE
    )

    if not match:
        return None

    try:

        roll_count = int(
            match.group(1)
        )

        bet = float(
            match.group(3).replace(
                ",",
                "."
            )
        )

    except Exception:

        return None

    if roll_count < 1:
        return None

    if roll_count > MAX_ROLLS:
        return None

    if bet < MIN_BET:
        return None

    return (
        game_type,
        bet,
        roll_count,
    )


# ============================================================
# GAME MENU
# ============================================================

async def games_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await check_join(
        update,
        context
    ):
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎲 تاس",
                callback_data="game_dice"
            ),
            InlineKeyboardButton(
                "🎳 بولینگ",
                callback_data="game_bowling"
            ),
        ],
        [
            InlineKeyboardButton(
                "🎯 دارت",
                callback_data="game_darts"
            ),
            InlineKeyboardButton(
                "🏀 بسکتبال",
                callback_data="game_basketball"
            ),
        ],
    ])

    await update.effective_message.reply_text(
        """
🎮 بازی‌ها

بازی را داخل گپ بساز:

1 تاس 0.1

3 تاس 0.1

2 بولینگ 0.1

4 دارت 0.1

5 بسکتبال 0.1

عدد اول = تعداد پرتاب

عدد آخر = مبلغ شرط

اعداد فارسی هم قبول است:

۳ تاس ۰.۱
""",
        reply_markup=keyboard
    )


# ============================================================
# HELP
# ============================================================

async def help_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.effective_message.reply_text(
        """
ℹ️ راهنمای BET_BT

🎮 ساخت بازی در گپ:

1 تاس 0.1
3 تاس 0.1
4 بولینگ 0.1
2 دارت 0.1
5 بسکتبال 0.1

عدد اول = تعداد رول

مثال:

3 تاس 0.1

یعنی سازنده ۳ بار تاس می‌اندازد
و در بازی دوستان حریف هم ۳ بار می‌اندازد.

🤖 بازی با ربات:

کاربر تمام رول‌های خودش را می‌اندازد.
بعد ربات همان تعداد رول می‌اندازد.

👥 بازی با دوستان:

اول سازنده تمام رول‌ها را می‌اندازد.
بعد حریف همان تعداد رول را می‌اندازد.

🏆 برنده بر اساس مجموع رول‌ها مشخص می‌شود.

💰 موجودی داخل ربات

💸 انتقال:

روی پیام کاربر Reply کن:

انتقال 0.1

👥 زیرمجموعه:

پاداش هر دعوت 0.05
"""
    )


# ============================================================
# GET GAME
# ============================================================

def get_game(game_id: int):

    conn = db_connect()

    row = conn.execute(
        """
        SELECT *
        FROM games
        WHERE game_id = ?
        """,
        (game_id,)
    ).fetchone()

    conn.close()

    return row


# ============================================================
# CREATE GROUP GAME
# ============================================================

async def group_game_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.effective_message

    if not message:
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP,
    ):
        return

    text = message.text or ""

    normalized = normalize_digits(
        text
    ).strip()

    # --------------------------------------------------------
    # انتقال
    # --------------------------------------------------------

    if re.match(
        r"^\s*انتقال\s+",
        normalized,
        re.IGNORECASE
    ):

        await transfer_command(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # موجودی
    # --------------------------------------------------------

    if normalized.lower() in (
        "موجودی",
        "balance",
        "💰 موجودی",
    ):

        await balance(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # بازی
    # --------------------------------------------------------

    parsed = parse_group_game(
        normalized
    )

    if not parsed:
        return

    if not bot_enabled():

        await message.reply_text(
            "🔴 ربات بازی خاموش است."
        )

        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    ensure_user(user)

    if not await check_join(
        update,
        context
    ):
        return

    game_type, bet, roll_count = parsed

    current_balance = get_balance(
        user.id
    )

    if current_balance < bet:

        await message.reply_text(
            f"""
❌ موجودی کافی نیست.

💎 موجودی: {fmt(current_balance)} TRX
💰 مبلغ بازی: {fmt(bet)} TRX
"""
        )

        return

    # --------------------------------------------------------
    # جلوگیری از بازی همزمان
    # --------------------------------------------------------

    conn = db_connect()

    active = conn.execute(
        """
        SELECT game_id
        FROM games
        WHERE creator_id = ?
        AND chat_id = ?
        AND status IN (
            'waiting',
            'creator_turn',
            'opponent_turn',
            'bot_turn',
            'finishing'
        )
        LIMIT 1
        """,
        (
            user.id,
            update.effective_chat.id,
        )
    ).fetchone()

    conn.close()

    if active:

        await message.reply_text(
            "⚠️ شما یک بازی فعال دارید."
        )

        return

    # --------------------------------------------------------
    # قفل ساخت بازی
    # --------------------------------------------------------

    operation_id = (
        f"game_create:"
        f"{update.effective_chat.id}:"
        f"{message.message_id}"
    )

    if not lock_operation(
        operation_id
    ):
        return

    # --------------------------------------------------------
    # کسر مبلغ سازنده
    # --------------------------------------------------------

    if not change_balance(
        user.id,
        -bet,
        "game_lock",
        operation_id,
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    now = time.time()

    conn = db_connect()

    try:

        cur = conn.execute(
            """
            INSERT INTO games(
                chat_id,
                message_id,
                creator_id,
                opponent_id,
                game_type,
                bet,
                roll_count,
                mode,
                status,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?
            )
            """,
            (
                update.effective_chat.id,
                message.message_id,
                user.id,
                None,
                game_type,
                bet,
                roll_count,
                "waiting",
                "waiting",
                now,
                now,
            )
        )

        game_id = cur.lastrowid

        conn.commit()

    except Exception:

        conn.rollback()

        conn.close()

        change_balance(
            user.id,
            bet,
            "game_refund",
            operation_id,
        )

        await message.reply_text(
            "❌ ساخت بازی خطا داشت؛ "
            "مبلغ برگشت داده شد."
        )

        return

    conn.close()

    data = GAME_TYPES[
        game_type
    ]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"joinbot:{game_id}"
            ),
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"joinfriend:{game_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel:{game_id}"
            ),
        ],
    ])

    try:

        sent = await message.reply_text(
            f"""
🎮 بازی جدید {data['emoji']}

👤 سازنده: {user.first_name}

🎮 بازی: {data['name']}

🎲 تعداد پرتاب: {roll_count}

💰 مبلغ شرط: {fmt(bet)} TRX

🤖 بازی با ربات:
سازنده {roll_count} بار رول می‌کند،
بعد ربات {roll_count} بار رول می‌کند.

👥 بازی با دوستان:
سازنده {roll_count} بار رول می‌کند،
بعد حریف {roll_count} بار رول می‌کند.

🏆 جایزه برنده: {fmt(WIN_PAYOUT)} TRX

یکی از گزینه‌ها را انتخاب کنید.
""",
            reply_markup=keyboard
        )

        conn = db_connect()

        conn.execute(
            """
            UPDATE games
            SET message_id = ?
            WHERE game_id = ?
            """,
            (
                sent.message_id,
                game_id,
            )
        )

        conn.commit()

        conn.close()

    except Exception:

        await refund_game(
            game_id
        )


# ============================================================
# JOIN BOT
# ============================================================

async def join_bot_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int
):

    query = update.callback_query

    user = query.from_user

    if not await check_join(
        update,
        context
    ):
        return

    game = get_game(
        game_id
    )

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ این بازی دیگر قابل ورود نیست.",
            show_alert=True
        )

        return

    if game["creator_id"] != user.id:

        await query.answer(
            "❌ فقط سازنده می‌تواند بازی با ربات را شروع کند.",
            show_alert=True
        )

        return

    conn = db_connect()

    cur = conn.execute(
        """
        UPDATE games
        SET mode = 'bot',
            status = 'creator_turn',
            updated_at = ?
        WHERE game_id = ?
        AND status = 'waiting'
        """,
        (
            time.time(),
            game_id,
        )
    )

    changed = cur.rowcount

    conn.commit()

    conn.close()

    if changed != 1:

        await query.answer(
            "❌ بازی قبلاً شروع شده.",
            show_alert=True
        )

        return

    roll_count = int(
        game["roll_count"] or 1
    )

    game_data = GAME_TYPES[
        game["game_type"]
    ]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🎲 رول من ({roll_count} بار)",
                callback_data=f"roll:{game_id}"
            )
        ]
    ])

    await query.message.edit_text(
        f"""
🤖 بازی با ربات

👤 {user.first_name}

🎮 {game_data['emoji']} {game_data['name']}

🎲 تعداد پرتاب: {roll_count}

💰 مبلغ: {fmt(game['bet'])} TRX

⬇️ اول خودت رول کن.

با زدن دکمه، تمام {roll_count} پرتاب انجام می‌شود.
""",
        reply_markup=keyboard
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_friend_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int
):

    query = update.callback_query

    user = query.from_user

    if not await check_join(
        update,
        context
    ):
        return

    game = get_game(
        game_id
    )

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if game["status"] != "waiting":

        await query.answer(
            "❌ این بازی دیگر قابل ورود نیست.",
            show_alert=True
        )

        return

    if game["creator_id"] == user.id:

        await query.answer(
            "❌ خودت سازنده‌ای.",
            show_alert=True
        )

        return

    bet = float(
        game["bet"]
    )

    if get_balance(user.id) < bet:

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )

        return

    operation_id = (
        f"friend_join:"
        f"{game_id}:"
        f"{user.id}"
    )

    if not lock_operation(
        operation_id
    ):

        await query.answer(
            "⚠️ این عملیات قبلاً انجام شده.",
            show_alert=True
        )

        return

    if not change_balance(
        user.id,
        -bet,
        "game_lock",
        operation_id,
    ):

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )

        return

    conn = db_connect()

    cur = conn.execute(
        """
        UPDATE games
        SET opponent_id = ?,
            mode = 'friends',
            status = 'creator_turn',
            updated_at = ?
        WHERE game_id = ?
        AND status = 'waiting'
        AND opponent_id IS NULL
        """,
        (
            user.id,
            time.time(),
            game_id,
        )
    )

    changed = cur.rowcount

    conn.commit()

    conn.close()

    if changed != 1:

        change_balance(
            user.id,
            bet,
            "game_refund",
            f"join_failed:{game_id}"
        )

        await query.answer(
            "❌ بازی قبلاً گرفته شده؛ "
            "مبلغ برگشت خورد.",
            show_alert=True
        )

        return

    creator = get_user(
        game["creator_id"]
    )

    creator_name = (
        creator["first_name"]
        if creator
        else "سازنده"
    )

    roll_count = int(
        game["roll_count"] or 1
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                f"🎲 رول {creator_name}",
                callback_data=f"roll:{game_id}"
            )
        ]
    ])

    await query.message.edit_text(
        f"""
👥 بازی با دوستان

👤 سازنده: {creator_name}

👤 حریف: {user.first_name}

🎮 {GAME_TYPES[game['game_type']]['emoji']}
{GAME_TYPES[game['game_type']]['name']}

🎲 تعداد پرتاب: {roll_count}

💰 مبلغ: {fmt(bet)} TRX

⬇️ ابتدا سازنده باید {roll_count} بار رول کند.
""",
        reply_markup=keyboard
    )


# ============================================================
# SEND MULTIPLE ROLLS
# ============================================================

async def send_multiple_rolls(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    emoji: str,
    count: int
):

    results = []

    for index in range(count):

        sent = await context.bot.send_dice(
            chat_id=chat_id,
            emoji=emoji
        )

        results.append(
            int(sent.dice.value)
        )

        if index < count - 1:

            await asyncio.sleep(
                0.8
            )

    return results


# ============================================================
# ROLL GAME
# ============================================================

async def roll_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int
):

    query = update.callback_query

    user = query.from_user

    if not await check_join(
        update,
        context
    ):
        return

    game = get_game(
        game_id
    )

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    status = game["status"]

    if status not in (
        "creator_turn",
        "opponent_turn",
    ):

        await query.answer(
            "❌ نوبت رول نیست.",
            show_alert=True
        )

        return

    # --------------------------------------------------------
    # بررسی نوبت
    # --------------------------------------------------------

    if status == "creator_turn":

        if user.id != game["creator_id"]:

            await query.answer(
                "⏳ هنوز نوبت سازنده است.",
                show_alert=True
            )

            return

    elif status == "opponent_turn":

        if user.id != game["opponent_id"]:

            await query.answer(
                "⏳ هنوز نوبت حریف است.",
                show_alert=True
            )

            return

    # --------------------------------------------------------
    # قفل رول
    # --------------------------------------------------------

    operation_id = (
        f"roll:"
        f"{game_id}:"
        f"{user.id}:"
        f"{status}"
    )

    if not lock_operation(
        operation_id
    ):

        await query.answer(
            "⚠️ این رول قبلاً انجام شده.",
            show_alert=True
        )

        return

    roll_count = int(
        game["roll_count"] or 1
    )

    emoji = GAME_TYPES[
        game["game_type"]
    ]["telegram_dice"]

    # --------------------------------------------------------
    # انجام تمام رول‌ها
    # --------------------------------------------------------

    try:

        results = await send_multiple_rolls(
            context,
            game["chat_id"],
            emoji,
            roll_count
        )

    except Exception:

        await refund_game(
            game_id
        )

        try:

            await query.message.edit_text(
                "🛡️ بازی با خطا مواجه شد.\n\n"
                "💰 مبلغ‌های قفل‌شده برگشت داده شدند."
            )

        except Exception:
            pass

        return

    total = sum(
        results
    )

    result_text = " + ".join(
        str(x)
        for x in results
    )

    # ========================================================
    # CREATOR ROLL
    # ========================================================

    if status == "creator_turn":

        conn = db_connect()

        cur = conn.execute(
            """
            UPDATE games
            SET creator_roll = ?,
                creator_total = ?,
                status = CASE
                    WHEN mode = 'bot'
                    THEN 'bot_turn'
                    ELSE 'opponent_turn'
                END,
                updated_at = ?
            WHERE game_id = ?
            AND status = 'creator_turn'
            """,
            (
                total,
                total,
                time.time(),
                game_id,
            )
        )

        changed = cur.rowcount

        conn.commit()

        conn.close()

        if changed != 1:

            return

        # ----------------------------------------------------
        # BOT GAME
        # ----------------------------------------------------

        if game["mode"] == "bot":

            await query.message.reply_text(
                f"""
👤 {user.first_name}

🎲 رول‌ها:
{result_text}

📊 مجموع: {total}

🤖 حالا ربات {roll_count} بار رول می‌کند...
"""
            )

            await asyncio.sleep(
                1
            )

            await bot_roll(
                context,
                game_id
            )

            return

        # ----------------------------------------------------
        # FRIEND GAME
        # ----------------------------------------------------

        opponent = get_user(
            game["opponent_id"]
        )

        opponent_name = (
            opponent["first_name"]
            if opponent
            else "حریف"
        )

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"🎲 رول {opponent_name}",
                    callback_data=f"roll:{game_id}"
                )
            ]
        ])

        await query.message.edit_text(
            f"""
👥 بازی با دوستان

👤 سازنده: {user.first_name}

🎲 رول‌ها:
{result_text}

📊 مجموع سازنده: {total}

👤 حریف: {opponent_name}

⬇️ حالا نوبت حریف است.
""",
            reply_markup=keyboard
        )

        return

    # ========================================================
    # OPPONENT ROLL
    # ========================================================

    if status == "opponent_turn":

        conn = db_connect()

        cur = conn.execute(
            """
            UPDATE games
            SET opponent_roll = ?,
                opponent_total = ?,
                status = 'finishing',
                updated_at = ?
            WHERE game_id = ?
            AND status = 'opponent_turn'
            """,
            (
                total,
                total,
                time.time(),
                game_id,
            )
        )

        changed = cur.rowcount

        conn.commit()

        conn.close()

        if changed != 1:

            return

        await query.message.reply_text(
            f"""
👤 {user.first_name}

🎲 رول‌ها:
{result_text}

📊 مجموع: {total}

⏳ در حال اعلام نتیجه...
"""
        )

        await finish_friend_game(
            context,
            game_id
        )


# ============================================================
# BOT ROLL
# ============================================================

async def bot_roll(
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int
):

    game = get_game(
        game_id
    )

    if not game:
        return

    if game["status"] != "bot_turn":
        return

    roll_count = int(
        game["roll_count"] or 1
    )

    emoji = GAME_TYPES[
        game["game_type"]
    ]["telegram_dice"]

    try:

        results = await send_multiple_rolls(
            context,
            game["chat_id"],
            emoji,
            roll_count
        )

    except Exception:

        await refund_game(
            game_id
        )

        try:

            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    "🛡️ بازی با ربات با خطا مواجه شد.\n\n"
                    "💰 مبلغ کاربر برگشت داده شد."
                )
            )

        except Exception:
            pass

        return

    total = sum(
        results
    )

    conn = db_connect()

    cur = conn.execute(
        """
        UPDATE games
        SET opponent_roll = ?,
            opponent_total = ?,
            status = 'finishing',
            updated_at = ?
        WHERE game_id = ?
        AND status = 'bot_turn'
        """,
        (
            total,
            total,
            time.time(),
            game_id,
        )
    )

    changed = cur.rowcount

    conn.commit()

    conn.close()

    if changed != 1:
        return

    result_text = " + ".join(
        str(x)
        for x in results
    )

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            "🤖 ربات رول کرد.\n\n"
            f"🎲 رول‌ها:\n{result_text}\n\n"
            f"📊 مجموع ربات: {total}\n\n"
            "⏳ نتیجه در حال اعلام است..."
        )
    )

    await finish_bot_game(
        context,
        game_id
    )


# ============================================================
# FINISH BOT GAME
# ============================================================

async def finish_bot_game(
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int
):

    game = get_game(
        game_id
    )

    if not game:
        return

    creator = get_user(
        game["creator_id"]
    )

    creator_name = (
        creator["first_name"]
        if creator
        else "کاربر"
    )

    creator_total = game[
        "creator_total"
    ]

    bot_total = game[
        "opponent_total"
    ]

    if creator_total is None or bot_total is None:

        await refund_game(
            game_id
        )

        return

    # --------------------------------------------------------
    # نتیجه
    # --------------------------------------------------------

    if creator_total == bot_total:

        change_balance(
            game["creator_id"],
            game["bet"],
            "game_draw_refund",
            f"game:{game_id}",
        )

        result = f"""
🤝 بازی مساوی شد.

👤 {creator_name}
📊 مجموع: {creator_total}

🤖 ربات
📊 مجموع: {bot_total}

💰 مبلغ {fmt(game['bet'])} TRX برگشت داده شد.
"""

    elif creator_total > bot_total:

        change_balance(
            game["creator_id"],
            WIN_PAYOUT,
            "game_win",
            f"game:{game_id}",
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}
📊 مجموع: {creator_total}

🤖 ربات
📊 مجموع: {bot_total}

🥇 برنده: {creator_name}

💰 جایزه: {fmt(WIN_PAYOUT)} TRX
"""

    else:

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}
📊 مجموع: {creator_total}

🤖 ربات
📊 مجموع: {bot_total}

🥇 برنده: 🤖 ربات
"""

    # --------------------------------------------------------
    # FINISH LOCK
    # --------------------------------------------------------

    operation_id = (
        f"finish_bot:{game_id}"
    )

    if not lock_operation(
        operation_id
    ):
        return

    conn = db_connect()

    conn.execute(
        """
        UPDATE games
        SET status = 'finished',
            updated_at = ?
        WHERE game_id = ?
        AND status = 'finishing'
        """,
        (
            time.time(),
            game_id,
        )
    )

    conn.commit()

    conn.close()

    try:

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=result
        )

    except Exception:
        pass


# ============================================================
# FINISH FRIEND GAME
# ============================================================

async def finish_friend_game(
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int
):

    game = get_game(
        game_id
    )

    if not game:
        return

    creator = get_user(
        game["creator_id"]
    )

    opponent = get_user(
        game["opponent_id"]
    )

    creator_name = (
        creator["first_name"]
        if creator
        else "سازنده"
    )

    opponent_name = (
        opponent["first_name"]
        if opponent
        else "حریف"
    )

    creator_total = game[
        "creator_total"
    ]

    opponent_total = game[
        "opponent_total"
    ]

    if creator_total is None or opponent_total is None:

        await refund_game(
            game_id
        )

        return

    # --------------------------------------------------------
    # ضد دوباره پرداخت
    # --------------------------------------------------------

    operation_id = (
        f"finish_friend:{game_id}"
    )

    if not lock_operation(
        operation_id
    ):
        return

    # --------------------------------------------------------
    # مساوی
    # --------------------------------------------------------

    if creator_total == opponent_total:

        change_balance(
            game["creator_id"],
            game["bet"],
            "game_draw_refund",
            f"game:{game_id}:creator",
        )

        change_balance(
            game["opponent_id"],
            game["bet"],
            "game_draw_refund",
            f"game:{game_id}:opponent",
        )

        result = f"""
🤝 نتیجه بازی

👤 {creator_name}
📊 مجموع: {creator_total}

👤 {opponent_name}
📊 مجموع: {opponent_total}

⚖️ بازی مساوی شد.

💰 مبلغ هر دو نفر برگشت داده شد.
"""

    # --------------------------------------------------------
    # CREATOR WIN
    # --------------------------------------------------------

    elif creator_total > opponent_total:

        change_balance(
            game["creator_id"],
            WIN_PAYOUT,
            "game_win",
            f"game:{game_id}",
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}
📊 مجموع: {creator_total}

👤 {opponent_name}
📊 مجموع: {opponent_total}

🥇 برنده: {creator_name}

💰 جایزه: {fmt(WIN_PAYOUT)} TRX
"""

    # --------------------------------------------------------
    # OPPONENT WIN
    # --------------------------------------------------------

    else:

        change_balance(
            game["opponent_id"],
            WIN_PAYOUT,
            "game_win",
            f"game:{game_id}",
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}
📊 مجموع: {creator_total}

👤 {opponent_name}
📊 مجموع: {opponent_total}

🥇 برنده: {opponent_name}

💰 جایزه: {fmt(WIN_PAYOUT)} TRX
"""

    conn = db_connect()

    conn.execute(
        """
        UPDATE games
        SET status = 'finished',
            updated_at = ?
        WHERE game_id = ?
        AND status = 'finishing'
        """,
        (
            time.time(),
            game_id,
        )
    )

    conn.commit()

    conn.close()

    try:

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=result
        )

    except Exception:
        pass


# ============================================================
# REFUND GAME
# ============================================================

async def refund_game(
    game_id: int
):

    operation_id = (
        f"refund:{game_id}"
    )

    if not lock_operation(
        operation_id
    ):
        return False

    game = get_game(
        game_id
    )

    if not game:
        return False

    if game["status"] in (
        "finished",
        "refunded",
    ):
        return False

    # --------------------------------------------------------
    # برگشت مبلغ سازنده
    # --------------------------------------------------------

    change_balance(
        game["creator_id"],
        float(game["bet"]),
        "game_refund",
        f"game:{game_id}:creator",
    )

    # --------------------------------------------------------
    # برگشت مبلغ حریف
    # --------------------------------------------------------

    if game["opponent_id"]:

        change_balance(
            game["opponent_id"],
            float(game["bet"]),
            "game_refund",
            f"game:{game_id}:opponent",
        )

    conn = db_connect()

    conn.execute(
        """
        UPDATE games
        SET status = 'refunded',
            updated_at = ?
        WHERE game_id = ?
        AND status NOT IN ('finished', 'refunded')
        """,
        (
            time.time(),
            game_id,
        )
    )

    conn.commit()

    conn.close()

    return True


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int
):

    query = update.callback_query

    user = query.from_user

    game = get_game(
        game_id
    )

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    if (
        game["creator_id"] != user.id
        and not is_owner(user.id)
    ):

        await query.answer(
            "❌ فقط سازنده یا مالک می‌تواند بازی را لغو کند.",
            show_alert=True
        )

        return

    if game["status"] not in (
        "waiting",
        "creator_turn",
        "opponent_turn",
        "bot_turn",
    ):

        await query.answer(
            "❌ این بازی دیگر قابل لغو نیست.",
            show_alert=True
        )

        return

    await refund_game(
        game_id
    )

    await query.answer(
        "✅ بازی لغو شد و مبلغ‌ها برگشت خورد."
    )

    try:

        await query.message.edit_text(
            "❌ بازی لغو شد.\n\n"
            "💰 موجودی‌های قفل‌شده برگشت داده شدند."
        )

    except Exception:
        pass


# ============================================================
# ADMIN KEYBOARD
# ============================================================

def admin_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            ),
            InlineKeyboardButton(
                "👥 کاربران",
                callback_data="admin_users"
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data="admin_add"
            ),
            InlineKeyboardButton(
                "➖ کسر موجودی",
                callback_data="admin_remove"
            ),
        ],
        [
            InlineKeyboardButton(
                "🎮 بازی‌های فعال",
                callback_data="admin_active"
            ),
        ],
        [
            InlineKeyboardButton(
                "🔴 خاموش / 🟢 روشن",
                callback_data="admin_toggle"
            ),
        ],
    ])


# ============================================================
# ADMIN COMMAND
# ============================================================

async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_owner(
        user.id
    ):

        await update.effective_message.reply_text(
            "❌ دسترسی ندارید."
        )

        return

    await update.effective_message.reply_text(
        """
👑 پنل مدیریت BET_BT

از دکمه‌های زیر استفاده کن.
""",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    user = query.from_user

    if not is_owner(
        user.id
    ):

        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )

        return

    data = query.data or ""

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    if data == "admin_stats":

        conn = db_connect()

        users = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM users
            """
        ).fetchone()["c"]

        active = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM games
            WHERE status IN (
                'waiting',
                'creator_turn',
                'opponent_turn',
                'bot_turn',
                'finishing'
            )
            """
        ).fetchone()["c"]

        finished = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM games
            WHERE status = 'finished'
            """
        ).fetchone()["c"]

        total_balance = conn.execute(
            """
            SELECT COALESCE(
                SUM(balance),
                0
            ) AS s
            FROM users
            """
        ).fetchone()["s"]

        conn.close()

        await query.answer()

        await query.message.reply_text(
            f"""
📊 آمار

👥 کاربران: {users}

🎮 بازی‌های فعال: {active}

🏁 بازی‌های تمام‌شده: {finished}

💎 مجموع موجودی:
{fmt(total_balance)} TRX
"""
        )

        return

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

    if data == "admin_users":

        conn = db_connect()

        rows = conn.execute(
            """
            SELECT
                user_id,
                first_name,
                username,
                balance
            FROM users
            ORDER BY balance DESC
            LIMIT 30
            """
        ).fetchall()

        conn.close()

        if not rows:

            await query.answer()

            await query.message.reply_text(
                "👥 کاربری وجود ندارد."
            )

            return

        lines = [
            "👥 کاربران\n"
        ]

        for row in rows:

            name = (
                row["first_name"]
                or "بدون نام"
            )

            lines.append(
                f"👤 {name}\n"
                f"🆔 {row['user_id']}\n"
                f"💎 {fmt(row['balance'])} TRX\n"
            )

        await query.answer()

        await query.message.reply_text(
            "\n".join(lines)
        )

        return

    # --------------------------------------------------------
    # ACTIVE
    # --------------------------------------------------------

    if data == "admin_active":

        conn = db_connect()

        rows = conn.execute(
            """
            SELECT
                game_id,
                chat_id,
                creator_id,
                opponent_id,
                game_type,
                bet,
                roll_count,
                status
            FROM games
            WHERE status IN (
                'waiting',
                'creator_turn',
                'opponent_turn',
                'bot_turn',
                'finishing'
            )
            ORDER BY game_id DESC
            LIMIT 30
            """
        ).fetchall()

        conn.close()

        if not rows:

            await query.answer()

            await query.message.reply_text(
                "🎮 بازی فعال وجود ندارد."
            )

            return

        lines = [
            "🎮 بازی‌های فعال\n"
        ]

        for row in rows:

            lines.append(
                f"ID: {row['game_id']}\n"
                f"🎮 {row['game_type']}\n"
                f"🎲 رول: {row['roll_count']}\n"
                f"💰 شرط: {fmt(row['bet'])}\n"
                f"📌 {row['status']}\n"
            )

        await query.answer()

        await query.message.reply_text(
            "\n".join(lines)
        )

        return

    # --------------------------------------------------------
    # ADD
    # --------------------------------------------------------

    if data == "admin_add":

        await query.answer()

        await query.message.reply_text(
            """
➕ افزایش موجودی

فرمت:

/addbalance USER_ID AMOUNT

مثال:

/addbalance 123456789 10
"""
        )

        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        await query.answer()

        await query.message.reply_text(
            """
➖ کسر موجودی

فرمت:

/removebalance USER_ID AMOUNT

مثال:

/removebalance 123456789 10
"""
        )

        return

    # --------------------------------------------------------
    # TOGGLE
    # --------------------------------------------------------

    if data == "admin_toggle":

        current = bot_enabled()

        set_bot_enabled(
            not current
        )

        await query.answer(
            "تنظیم شد."
        )

        if current:

            await query.message.reply_text(
                "🔴 ربات خاموش شد."
            )

        else:

            await query.message.reply_text(
                "🟢 ربات روشن شد."
            )

        return


# ============================================================
# ADD BALANCE
# ============================================================

async def add_balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_owner(
        user.id
    ):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "فرمت:\n"
            "/addbalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )

        amount = float(
            normalize_digits(
                context.args[1]
            ).replace(",", ".")
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

    target = get_user(
        target_id
    )

    if not target:

        await update.message.reply_text(
            "❌ کاربر در دیتابیس وجود ندارد."
        )

        return

    if not change_balance(
        target_id,
        amount,
        "admin_add",
        f"admin:{user.id}",
    ):

        await update.message.reply_text(
            "❌ افزایش موجودی انجام نشد."
        )

        return

    new_balance = get_balance(
        target_id
    )

    await update.message.reply_text(
        f"""
✅ افزایش موجودی انجام شد.

🆔 {target_id}

➕ {fmt(amount)} TRX

💰 موجودی جدید:
{fmt(new_balance)} TRX
"""
    )


# ============================================================
# REMOVE BALANCE
# ============================================================

async def remove_balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_owner(
        user.id
    ):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "فرمت:\n"
            "/removebalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )

        amount = float(
            normalize_digits(
                context.args[1]
            ).replace(",", ".")
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

    target = get_user(
        target_id
    )

    if not target:

        await update.message.reply_text(
            "❌ کاربر وجود ندارد."
        )

        return

    if not change_balance(
        target_id,
        -amount,
        "admin_remove",
        f"admin:{user.id}",
    ):

        await update.message.reply_text(
            "❌ موجودی کاربر کافی نیست."
        )

        return

    new_balance = get_balance(
        target_id
    )

    await update.message.reply_text(
        f"""
✅ کسر موجودی انجام شد.

🆔 {target_id}

➖ {fmt(amount)} TRX

💰 موجودی جدید:
{fmt(new_balance)} TRX
"""
    )


# ============================================================
# CLEANUP STUCK GAMES
# ============================================================

async def cleanup_stuck_games(
    context: ContextTypes.DEFAULT_TYPE
):

    now = time.time()

    conn = db_connect()

    rows = conn.execute(
        """
        SELECT game_id
        FROM games
        WHERE status IN (
            'waiting',
            'creator_turn',
            'opponent_turn',
            'bot_turn',
            'finishing'
        )
        AND updated_at < ?
        """,
        (
            now - GAME_TIMEOUT,
        )
    ).fetchall()

    conn.close()

    for row in rows:

        try:

            await refund_game(
                int(row["game_id"])
            )

        except Exception:

            logger.exception(
                "cleanup failed"
            )


# ============================================================
# CALLBACK HANDLER
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    # --------------------------------------------------------
    # FORCE JOIN
    # --------------------------------------------------------

    if data == "check_join":

        user = query.from_user

        try:

            member = await context.bot.get_chat_member(
                CHANNEL_USERNAME,
                user.id
            )

            if str(member.status) in (
                "member",
                "administrator",
                "creator",
            ):

                await query.answer(
                    "✅ عضویت تأیید شد.",
                    show_alert=True
                )

                await query.message.reply_text(
                    "✅ عضویت تأیید شد.\n\n"
                    "حالا /start را بزن."
                )

                return

        except Exception:
            pass

        await query.answer(
            "❌ هنوز عضو کانال نیستی.",
            show_alert=True
        )

        return

    # --------------------------------------------------------
    # PRIVATE GAME BUTTON
    # --------------------------------------------------------

    if data.startswith("game_"):

        key = data.replace(
            "game_",
            ""
        )

        if key not in GAME_TYPES:
            return

        name = GAME_TYPES[
            key
        ]["name"]

        await query.answer(
            f"در گپ بنویس:\n\n"
            f"1 {name} 0.1\n\n"
            f"یا:\n"
            f"3 {name} 0.1",
            show_alert=True
        )

        return

    # --------------------------------------------------------
    # GAME CALLBACK
    # --------------------------------------------------------

    if ":" in data:

        action, value = data.split(
            ":",
            1
        )

        try:

            game_id = int(
                value
            )

        except Exception:

            return

        if action == "joinbot":

            await join_bot_game(
                update,
                context,
                game_id
            )

            return

        if action == "joinfriend":

            await join_friend_game(
                update,
                context,
                game_id
            )

            return

        if action == "roll":

            await roll_game(
                update,
                context,
                game_id
            )

            return

        if action == "cancel":

            await cancel_game(
                update,
                context,
                game_id
            )

            return


# ============================================================
# PRIVATE TEXT
# ============================================================

async def private_text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not update.message:
        return

    if update.effective_chat.type != ChatType.PRIVATE:
        return

    text = normalize_digits(
        update.message.text or ""
    ).strip()

    if text in (
        "موجودی",
        "💰 موجودی",
        "balance",
    ):

        await balance(
            update,
            context
        )

        return

    if text in (
        "بازی",
        "بازی‌ها",
        "🎮 بازی‌ها",
    ):

        await games_menu(
            update,
            context
        )

        return

    if text in (
        "زیرمجموعه",
        "👥 زیرمجموعه",
    ):

        await referral(
            update,
            context
        )

        return

    if text in (
        "انتقال",
        "💸 انتقال",
    ):

        await transfer_help(
            update,
            context
        )

        return

    if text in (
        "راهنما",
        "ℹ️ راهنما",
    ):

        await help_message(
            update,
            context
        )

        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.error(
        "Unhandled exception",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN در Environment Variables تنظیم نشده است."
        )

    init_db()

    application = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    application.add_handler(
        CommandHandler(
            "addbalance",
            add_balance_command
        )
    )

    application.add_handler(
        CommandHandler(
            "removebalance",
            remove_balance_command
        )
    )

    # --------------------------------------------------------
    # PRIVATE TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_text_handler
        )
    )

    # --------------------------------------------------------
    # GROUP TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & ~filters.COMMAND,
            group_game_handler
        )
    )

    # --------------------------------------------------------
    # ADMIN CALLBACK
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # --------------------------------------------------------
    # ALL OTHER CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # --------------------------------------------------------
    # ERROR
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # CLEANUP
    # --------------------------------------------------------

    if application.job_queue:

        application.job_queue.run_repeating(
            cleanup_stuck_games,
            interval=60,
            first=60
        )

    print(
        "========================================"
    )

    print(
        "BET_BT BOT STARTED"
    )

    print(
        "BOT_TOKEN: OK"
    )

    print(
        "OWNER:",
        OWNER_ID
    )

    print(
        "CHANNEL:",
        CHANNEL_USERNAME
    )

    print(
        "DATABASE:",
        DB_FILE
    )

    print(
        "MAX ROLLS:",
        MAX_ROLLS
    )

    print(
        "========================================"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    main()
