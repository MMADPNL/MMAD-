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

# برنده 1.8 برابر مبلغ بازی دریافت می‌کند
PAYOUT_MULTIPLIER = 1.8

REFERRAL_REWARD = 0.05

GAME_TIMEOUT = 180


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
        check_same_thread=False
    )

    conn.row_factory = sqlite3.Row

    return conn


def init_db():

    conn = db_connect()

    cur = conn.cursor()

    # USERS
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

    # GAMES
    cur.execute("""
        CREATE TABLE IF NOT EXISTS games (
            game_id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            message_id INTEGER DEFAULT 0,
            creator_id INTEGER NOT NULL,
            opponent_id INTEGER DEFAULT NULL,
            game_type TEXT NOT NULL,
            bet REAL NOT NULL,
            mode TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'waiting',

            creator_roll INTEGER DEFAULT NULL,
            opponent_roll INTEGER DEFAULT NULL,

            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    """)

    # TRANSACTIONS
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

    # SETTINGS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    # REFERRALS
    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            invited_id INTEGER NOT NULL UNIQUE,
            reward REAL NOT NULL DEFAULT 0.05,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ANTI DUPLICATE
    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_operations (
            operation_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL
        )
    """)

    cur.execute("""
        INSERT OR IGNORE INTO settings(key, value)
        VALUES('enabled', '1')
    """)

    conn.commit()

    conn.close()


# ============================================================
# HELPERS
# ============================================================

def normalize_digits(text):

    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return text.translate(table)


def parse_amount(text):

    text = normalize_digits(text)

    text = text.replace(",", ".")

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


def fmt(amount):

    return (
        f"{float(amount):.4f}"
        .rstrip("0")
        .rstrip(".")
    )


def get_user(user_id):

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
                user.id
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
                referred_by
            )
        )

    conn.commit()

    conn.close()


def is_owner(user_id):

    return user_id == OWNER_ID


def bot_enabled():

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


def set_bot_enabled(enabled):

    conn = db_connect()

    conn.execute(
        """
        INSERT INTO settings(key, value)
        VALUES('enabled', ?)

        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
        """,
        (
            "1" if enabled else "0",
        )
    )

    conn.commit()

    conn.close()


def get_balance(user_id):

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
    user_id,
    amount,
    kind,
    reference=""
):

    amount = round(
        float(amount),
        4
    )

    conn = db_connect()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

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

        old_balance = float(
            row["balance"]
        )

        new_balance = round(
            old_balance + amount,
            4
        )

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
                user_id
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
                reference
            )
        )

        conn.commit()

        return True

    except Exception:

        conn.rollback()

        logger.exception(
            "Balance change failed"
        )

        return False

    finally:

        conn.close()


def lock_operation(operation_id):

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
                time.time()
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
    update,
    context
):

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

        status = str(
            member.status
        )

        if status in (
            "member",
            "administrator",
            "creator"
        ):
            return True

    except TelegramError:

        pass

    keyboard = InlineKeyboardMarkup(
        [
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
        ]
    )

    try:

        if update.callback_query:

            await update.callback_query.message.reply_text(
                """
🔒 برای استفاده از ربات
ابتدا در کانال عضو شوید.

بعد از عضویت روی
«بررسی عضویت» بزنید.
""",
                reply_markup=keyboard
            )

        elif update.effective_message:

            await update.effective_message.reply_text(
                """
🔒 برای استفاده از ربات
ابتدا در کانال عضو شوید.

بعد از عضویت روی
«بررسی عضویت» بزنید.
""",
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
                KeyboardButton("🎮 بازی‌ها")
            ],
            [
                KeyboardButton("👥 زیرمجموعه"),
                KeyboardButton("💸 انتقال")
            ],
            [
                KeyboardButton("ℹ️ راهنما")
            ]
        ],
        resize_keyboard=True
    )


# ============================================================
# GAME TYPES
# ============================================================

GAME_TYPES = {

    "dice": {
        "name": "تاس",
        "emoji": "🎲",
    },

    "bowling": {
        "name": "بولینگ",
        "emoji": "🎳",
    },

    "darts": {
        "name": "دارت",
        "emoji": "🎯",
    },

    "basketball": {
        "name": "بسکتبال",
        "emoji": "🏀",
    },
}


EMOJI_TO_GAME = {

    "🎲": "dice",
    "🎳": "bowling",
    "🎯": "darts",
    "🏀": "basketball",
}


# ============================================================
# START
# ============================================================

async def start(
    update,
    context
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

    if not await check_join(
        update,
        context
    ):
        return

    process_referral(
        user.id
    )

    await update.message.reply_text(
        f"""
🎮 BET_BT

سلام {user.first_name or 'دوست عزیز'} 👋

🎲 تاس
🎳 بولینگ
🎯 دارت
🏀 بسکتبال

🤖 بازی با ربات:
خودت رول می‌کنی، بعد ربات رول می‌کند.

👥 بازی با دوستان:
هر دو بازیکن خودشان رول می‌کنند.

💎 موجودی داخلی TRX

💸 انتقال با Reply
""",
        reply_markup=private_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(
    update,
    context
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

    await update.message.reply_text(
        f"""
💰 موجودی شما

💎 {fmt(amount)} TRX
"""
    )


# ============================================================
# REFERRAL
# ============================================================

async def referral(
    update,
    context
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

    conn = db_connect()

    row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM referrals
        WHERE inviter_id = ?
        """,
        (user.id,)
    ).fetchone()

    count = int(
        row["c"]
    )

    conn.close()

    bot_username = (
        context.bot.username
    )

    link = (
        f"https://t.me/"
        f"{bot_username}"
        f"?start=ref_{user.id}"
    )

    await update.message.reply_text(
        f"""
👥 زیرمجموعه

🔗 لینک دعوت:

{link}

🎁 پاداش هر دعوت:
{fmt(REFERRAL_REWARD)} TRX

👤 تعداد:
{count}
"""
    )


# ============================================================
# REFERRAL PROCESS
# ============================================================

def process_referral(
    user_id
):

    conn = db_connect()

    try:

        conn.execute(
            "BEGIN IMMEDIATE"
        )

        user = conn.execute(
            """
            SELECT referred_by,
                   referral_paid
            FROM users
            WHERE user_id = ?
            """,
            (user_id,)
        ).fetchone()

        if not user:

            conn.rollback()
            return

        inviter_id = user[
            "referred_by"
        ]

        if not inviter_id:

            conn.rollback()
            return

        if user["referral_paid"]:

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
                inviter_id
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
                REFERRAL_REWARD
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
                str(user_id)
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


# ============================================================
# TRANSFER HELP
# ============================================================

async def transfer_help(
    update,
    context
):

    await update.message.reply_text(
        """
💸 انتقال با Reply

روی پیام کاربر Reply کن و بنویس:

انتقال 0.1

مثال:

انتقال 1
"""
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer_command(
    update,
    context
):

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
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

    if not update.message.reply_to_message:

        await update.message.reply_text(
            "❌ باید روی پیام کاربر Reply کنی."
        )

        return

    target = (
        update.message
        .reply_to_message
        .from_user
    )

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
        return

    if not change_balance(
        user.id,
        -amount,
        "transfer_out",
        f"to:{target.id}"
    ):

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    if not change_balance(
        target.id,
        amount,
        "transfer_in",
        f"from:{user.id}"
    ):

        change_balance(
            user.id,
            amount,
            "transfer_refund",
            f"failed:{target.id}"
        )

        await update.message.reply_text(
            "❌ انتقال انجام نشد؛ مبلغ برگشت خورد."
        )

        return

    await update.message.reply_text(
        f"""
✅ انتقال انجام شد.

👤 گیرنده:
{target.first_name}

💎 مبلغ:
{fmt(amount)} TRX
"""
    )


# ============================================================
# PRIVATE GAMES
# ============================================================

async def games_menu(
    update,
    context
):

    if not await check_join(
        update,
        context
    ):
        return

    await update.message.reply_text(
        """
🎮 بازی‌ها

بازی‌ها داخل گپ ساخته می‌شوند.

مثال:

1 تاس 0.1

1 بولینگ 0.1

1 دارت 0.1

1 بسکتبال 0.1

بعد از شروع بازی:

🎲 خود کاربر باید تاس را بفرستد.

🎳 خود کاربر باید بولینگ را بفرستد.

🎯 خود کاربر باید دارت را بفرستد.

🏀 خود کاربر باید بسکتبال را بفرستد.

❌ ربات به جای کاربر رول نمی‌کند.
"""
    )


# ============================================================
# HELP
# ============================================================

async def help_message(
    update,
    context
):

    await update.message.reply_text(
        """
ℹ️ راهنمای BET_BT

🎮 ساخت بازی در گپ:

1 تاس 0.1
1 بولینگ 0.1
1 دارت 0.1
1 بسکتبال 0.1

🎲 بعد از انتخاب بازی،
خود بازیکن باید ایموجی بازی را بفرستد.

💸 انتقال:

روی پیام کاربر Reply کن:

انتقال 0.1

💰 موجودی:

موجودی
"""
    )


# ============================================================
# DETECT GAME
# ============================================================

def detect_game(text):

    normalized = normalize_digits(
        text
    ).lower()

    for key, data in GAME_TYPES.items():

        if data["name"] in normalized:
            return key

        if key in normalized:
            return key

    return None


# ============================================================
# PARSE GAME
# ============================================================

def parse_group_game(text):

    game_type = detect_game(
        text
    )

    if not game_type:
        return None

    normalized = normalize_digits(
        text
    )

    numbers = re.findall(
        r"\d+(?:[.,]\d+)?",
        normalized
    )

    if not numbers:
        return None

    amount = None

    # معمولاً:
    # 1 تاس 0.1
    if len(numbers) >= 2:

        for number in numbers[1:]:

            try:

                value = float(
                    number.replace(
                        ",",
                        "."
                    )
                )

                if value > 0:

                    amount = value
                    break

            except Exception:
                pass

    # حالت:
    # تاس 0.1
    if amount is None:

        try:

            value = float(
                numbers[-1].replace(
                    ",",
                    "."
                )
            )

            if value != 1:
                amount = value

        except Exception:
            pass

    if amount is None:
        return None

    amount = round(
        amount,
        4
    )

    if amount < MIN_BET:
        return None

    return (
        game_type,
        amount
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(
    update,
    context,
    game_type,
    bet
):

    message = update.effective_message
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    balance_now = get_balance(
        user.id
    )

    if balance_now < bet:

        await message.reply_text(
            f"""
❌ موجودی کافی نیست.

💎 موجودی:
{fmt(balance_now)} TRX

💰 مبلغ بازی:
{fmt(bet)} TRX
"""
        )

        return

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
            'bot_turn'
        )

        LIMIT 1
        """,
        (
            user.id,
            update.effective_chat.id
        )
    ).fetchone()

    conn.close()

    if active:

        await message.reply_text(
            "⚠️ شما همین الان یک بازی فعال دارید."
        )

        return

    operation_id = (
        f"game_create:"
        f"{update.effective_chat.id}:"
        f"{message.message_id}"
    )

    if not lock_operation(
        operation_id
    ):
        return

    if not change_balance(
        user.id,
        -bet,
        "game_lock",
        operation_id
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    now = time.time()

    conn = db_connect()

    cursor = conn.execute(
        """
        INSERT INTO games(
            chat_id,
            message_id,
            creator_id,
            game_type,
            bet,
            mode,
            status,
            created_at,
            updated_at
        )

        VALUES(
            ?,
            ?,
            ?,
            ?,
            ?,
            'waiting',
            'waiting',
            ?,
            ?
        )
        """,
        (
            update.effective_chat.id,
            message.message_id,
            user.id,
            game_type,
            bet,
            now,
            now
        )
    )

    game_id = cursor.lastrowid

    conn.commit()

    conn.close()

    game = GAME_TYPES[
        game_type
    ]

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "🤖 بازی با ربات",
                    callback_data=f"joinbot:{game_id}"
                ),
                InlineKeyboardButton(
                    "👥 بازی با دوستان",
                    callback_data=f"joinfriend:{game_id}"
                )
            ],
            [
                InlineKeyboardButton(
                    "❌ لغو",
                    callback_data=f"cancel:{game_id}"
                )
            ]
        ]
    )

    try:

        sent = await message.reply_text(
            f"""
🎮 بازی جدید

{game['emoji']} {game['name']}

👤 سازنده:
{user.first_name}

💰 مبلغ:
{fmt(bet)} TRX

🤖 بازی با ربات:
خودت رول می‌کنی، بعد ربات.

👥 بازی با دوستان:
هر دو بازیکن خودشان رول می‌کنند.

👇 حالت بازی را انتخاب کنید.
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
                game_id
            )
        )

        conn.commit()

        conn.close()

    except Exception:

        await refund_game(
            game_id
        )


# ============================================================
# GROUP TEXT
# ============================================================

async def group_text_handler(
    update,
    context
):

    message = update.effective_message

    if not message:
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    text = message.text or ""

    # انتقال
    if re.match(
        r"^\s*انتقال\s+",
        text,
        re.IGNORECASE
    ):

        await transfer_command(
            update,
            context
        )

        return

    # موجودی
    if normalize_digits(
        text
    ).strip().lower() in (
        "موجودی",
        "💰 موجودی",
        "balance"
    ):

        await balance(
            update,
            context
        )

        return

    parsed = parse_group_game(
        text
    )

    if not parsed:
        return

    if not bot_enabled():

        await message.reply_text(
            "🔴 ربات بازی در حال حاضر خاموش است."
        )

        return

    user = update.effective_user

    if not user or user.is_bot:
        return

    if not await check_join(
        update,
        context
    ):
        return

    game_type, bet = parsed

    await create_game(
        update,
        context,
        game_type,
        bet
    )


# ============================================================
# GET GAME
# ============================================================

def get_game(
    game_id
):

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
# CALLBACK
# ============================================================

async def callback_handler(
    update,
    context
):

    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    user = query.from_user

    ensure_user(user)

    # بررسی عضویت
    if data == "check_join":

        if await check_join(
            update,
            context
        ):

            await query.answer(
                "✅ عضویت تأیید شد.",
                show_alert=True
            )

            try:

                await query.message.reply_text(
                    "✅ عضویت تأیید شد.\n\n/start را بزن."
                )

            except Exception:
                pass

        else:

            await query.answer(
                "❌ هنوز عضو کانال نیستی.",
                show_alert=True
            )

        return

    # دکمه‌های بازی خصوصی
    if data.startswith("game_"):

        await query.answer(
            "🎮 بازی را داخل گپ بساز.",
            show_alert=True
        )

        return

    if ":" not in data:
        return

    action, value = data.split(
        ":",
        1
    )

    try:

        game_id = int(value)

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

    if action == "cancel":

        await cancel_game(
            update,
            context,
            game_id
        )

        return


# ============================================================
# JOIN BOT
# ============================================================

async def join_bot_game(
    update,
    context,
    game_id
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
            "❌ بازی پیدا نشد.\nیک بازی جدید بساز.",
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
            "❌ فقط سازنده می‌تواند بازی با ربات را انتخاب کند.",
            show_alert=True
        )

        return

    conn = db_connect()

    cursor = conn.execute(
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
            game_id
        )
    )

    changed = cursor.rowcount

    conn.commit()

    conn.close()

    if changed != 1:

        await query.answer(
            "❌ بازی قبلاً شروع شده.",
            show_alert=True
        )

        return

    await query.answer(
        "✅ بازی شروع شد."
    )

    emoji = GAME_TYPES[
        game["game_type"]
    ]["emoji"]

    await query.message.edit_text(
        f"""
🤖 بازی با ربات

👤 بازیکن:
{user.first_name}

💰 مبلغ:
{fmt(game['bet'])} TRX

👇 حالا خودت این ایموجی را در همین گپ بفرست:

{emoji}

⚠️ ربات به جای تو رول نمی‌کند.
""",
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "❌ لغو",
                        callback_data=f"cancel:{game_id}"
                    )
                ]
            ]
        )
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_friend_game(
    update,
    context,
    game_id
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

    if get_balance(
        user.id
    ) < bet:

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
        operation_id
    ):

        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )

        return

    conn = db_connect()

    cursor = conn.execute(
        """
        UPDATE games

        SET opponent_id = ?,
            mode = 'friends',
            status = 'creator_turn',
            updated_at = ?

        WHERE game_id = ?
        AND status = 'waiting'
        """,
        (
            user.id,
            time.time(),
            game_id
        )
    )

    changed = cursor.rowcount

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
            "❌ بازی گرفته شده؛ مبلغ برگشت خورد.",
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

    emoji = GAME_TYPES[
        game["game_type"]
    ]["emoji"]

    await query.answer(
        "✅ وارد بازی شدی."
    )

    await query.message.edit_text(
        f"""
👥 بازی با دوستان

👤 سازنده:
{creator_name}

👤 حریف:
{user.first_name}

💰 مبلغ:
{fmt(bet)} TRX

👇 ابتدا سازنده خودش این ایموجی را در گپ بفرستد:

{emoji}

⚠️ هیچ دکمه‌ای برای رول وجود ندارد.
"""
    )


# ============================================================
# USER REAL DICE HANDLER
# ============================================================

async def user_dice_handler(
    update,
    context
):

    message = update.effective_message

    if not message:
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    dice = message.dice

    if not dice:
        return

    user = message.from_user

    if not user or user.is_bot:
        return

    game_type = EMOJI_TO_GAME.get(
        dice.emoji
    )

    if not game_type:
        return

    if not bot_enabled():
        return

    ensure_user(user)

    # پیدا کردن بازی فعال کاربر
    conn = db_connect()

    game = conn.execute(
        """
        SELECT *

        FROM games

        WHERE chat_id = ?

        AND status IN (
            'creator_turn',
            'opponent_turn'
        )

        AND (
            creator_id = ?
            OR opponent_id = ?
        )

        ORDER BY game_id DESC

        LIMIT 1
        """,
        (
            message.chat.id,
            user.id,
            user.id
        )
    ).fetchone()

    conn.close()

    if not game:
        return

    # نوع بازی اشتباه
    if game["game_type"] != game_type:

        correct = GAME_TYPES[
            game["game_type"]
        ]["emoji"]

        await message.reply_text(
            f"""
⚠️ این بازی با:

{correct}

انجام می‌شود.

لطفاً همان ایموجی را بفرست.
"""
        )

        return

    # نوبت سازنده
    if game["status"] == "creator_turn":

        if user.id != game["creator_id"]:
            return

    # نوبت حریف
    elif game["status"] == "opponent_turn":

        if user.id != game["opponent_id"]:
            return

    operation_id = (
        f"user_roll:"
        f"{game['game_id']}:"
        f"{user.id}:"
        f"{game['status']}:"
        f"{message.message_id}"
    )

    if not lock_operation(
        operation_id
    ):
        return

    value = dice.value

    # -----------------------------------------
    # ROLL CREATOR
    # -----------------------------------------

    if game["status"] == "creator_turn":

        if game["mode"] == "bot":

            new_status = "bot_turn"

        else:

            new_status = "opponent_turn"

        conn = db_connect()

        cursor = conn.execute(
            """
            UPDATE games

            SET creator_roll = ?,
                status = ?,
                updated_at = ?

            WHERE game_id = ?
            AND status = 'creator_turn'
            """,
            (
                value,
                new_status,
                time.time(),
                game["game_id"]
            )
        )

        changed = cursor.rowcount

        conn.commit()

        conn.close()

        if changed != 1:
            return

        # بازی با ربات
        if game["mode"] == "bot":

            await message.reply_text(
                f"""
👤 {user.first_name} رول کرد:

🎲 نتیجه:
{value}

🤖 حالا ربات رول می‌کند...
"""
            )

            await asyncio.sleep(
                1
            )

            await bot_roll(
                context,
                game["game_id"]
            )

            return

        # بازی دوستان
        opponent = get_user(
            game["opponent_id"]
        )

        opponent_name = (
            opponent["first_name"]
            if opponent
            else "حریف"
        )

        emoji = GAME_TYPES[
            game["game_type"]
        ]["emoji"]

        await message.reply_text(
            f"""
👤 {user.first_name}

🎯 نتیجه رول:
{value}

👤 حریف:
{opponent_name}

👇 حالا حریف خودش این ایموجی را بفرستد:

{emoji}
"""
        )

        return

    # -----------------------------------------
    # ROLL OPPONENT
    # -----------------------------------------

    if game["status"] == "opponent_turn":

        conn = db_connect()

        cursor = conn.execute(
            """
            UPDATE games

            SET opponent_roll = ?,
                status = 'finishing',
                updated_at = ?

            WHERE game_id = ?
            AND status = 'opponent_turn'
            """,
            (
                value,
                time.time(),
                game["game_id"]
            )
        )

        changed = cursor.rowcount

        conn.commit()

        conn.close()

        if changed != 1:
            return

        await finish_friend_game(
            context,
            game["game_id"]
        )


# ============================================================
# BOT ROLL
# ============================================================

async def bot_roll(
    context,
    game_id
):

    game = get_game(
        game_id
    )

    if not game:
        return

    if game["status"] != "bot_turn":
        return

    try:

        sent = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=GAME_TYPES[
                game["game_type"]
            ]["emoji"]
        )

        bot_value = sent.dice.value

    except Exception:

        await refund_game(
            game_id
        )

        try:

            await context.bot.send_message(
                chat_id=game["chat_id"],
                text="""
🛡️ بازی با ربات با خطا مواجه شد.

💰 مبلغ کاربر برگشت داده شد.
"""
            )

        except Exception:
            pass

        return

    conn = db_connect()

    cursor = conn.execute(
        """
        UPDATE games

        SET opponent_roll = ?,
            status = 'finishing',
            updated_at = ?

        WHERE game_id = ?
        AND status = 'bot_turn'
        """,
        (
            bot_value,
            time.time(),
            game_id
        )
    )

    conn.commit()

    conn.close()

    if cursor.rowcount:

        await finish_bot_game(
            context,
            game_id
        )


# ============================================================
# PAYOUT
# ============================================================

def payout(game):

    return round(
        float(game["bet"])
        * PAYOUT_MULTIPLIER,
        4
    )


# ============================================================
# FINISH BOT GAME
# ============================================================

async def finish_bot_game(
    context,
    game_id
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

    creator_roll = game[
        "creator_roll"
    ]

    bot_roll_value = game[
        "opponent_roll"
    ]

    if (
        creator_roll is None
        or bot_roll_value is None
    ):

        await refund_game(
            game_id
        )

        return

    prize = payout(
        game
    )

    if creator_roll == bot_roll_value:

        change_balance(
            game["creator_id"],
            game["bet"],
            "game_draw_refund",
            f"game:{game_id}"
        )

        result = f"""
🤝 بازی مساوی شد.

👤 {creator_name}:
{creator_roll}

🤖 ربات:
{bot_roll_value}

💰 مبلغ {fmt(game['bet'])} TRX برگشت داده شد.
"""

    elif creator_roll > bot_roll_value:

        change_balance(
            game["creator_id"],
            prize,
            "game_win",
            f"game:{game_id}"
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}:
{creator_roll}

🤖 ربات:
{bot_roll_value}

🥇 برنده:
{creator_name}

💰 جایزه:
{fmt(prize)} TRX
"""

    else:

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}:
{creator_roll}

🤖 ربات:
{bot_roll_value}

🥇 برنده:
🤖 ربات

💰 مبلغ بازی به بازیکن تعلق نگرفت.
"""

    finish_game_status(
        game_id
    )

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
    context,
    game_id
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

    creator_roll = game[
        "creator_roll"
    ]

    opponent_roll = game[
        "opponent_roll"
    ]

    if (
        creator_roll is None
        or opponent_roll is None
    ):

        await refund_game(
            game_id
        )

        return

    prize = payout(
        game
    )

    if creator_roll == opponent_roll:

        change_balance(
            game["creator_id"],
            game["bet"],
            "game_draw_refund",
            f"game:{game_id}"
        )

        change_balance(
            game["opponent_id"],
            game["bet"],
            "game_draw_refund",
            f"game:{game_id}"
        )

        result = f"""
🤝 نتیجه بازی

👤 {creator_name}:
{creator_roll}

👤 {opponent_name}:
{opponent_roll}

⚖️ بازی مساوی شد.

💰 مبلغ هر دو نفر برگشت داده شد.
"""

    elif creator_roll > opponent_roll:

        change_balance(
            game["creator_id"],
            prize,
            "game_win",
            f"game:{game_id}"
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}:
{creator_roll}

👤 {opponent_name}:
{opponent_roll}

🥇 برنده:
{creator_name}

💰 جایزه:
{fmt(prize)} TRX
"""

    else:

        change_balance(
            game["opponent_id"],
            prize,
            "game_win",
            f"game:{game_id}"
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}:
{creator_roll}

👤 {opponent_name}:
{opponent_roll}

🥇 برنده:
{opponent_name}

💰 جایزه:
{fmt(prize)} TRX
"""

    finish_game_status(
        game_id
    )

    try:

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=result
        )

    except Exception:
        pass


# ============================================================
# FINISH STATUS
# ============================================================

def finish_game_status(
    game_id
):

    conn = db_connect()

    conn.execute(
        """
        UPDATE games
        SET status = 'finished',
            updated_at = ?
        WHERE game_id = ?
        """,
        (
            time.time(),
            game_id
        )
    )

    conn.commit()

    conn.close()


# ============================================================
# REFUND
# ============================================================

async def refund_game(
    game_id
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
        "refunded"
    ):
        return False

    change_balance(
        game["creator_id"],
        game["bet"],
        "game_refund",
        f"game:{game_id}"
    )

    if game["opponent_id"]:

        change_balance(
            game["opponent_id"],
            game["bet"],
            "game_refund",
            f"game:{game_id}"
        )

    conn = db_connect()

    conn.execute(
        """
        UPDATE games
        SET status = 'refunded',
            updated_at = ?
        WHERE game_id = ?
        """,
        (
            time.time(),
            game_id
        )
    )

    conn.commit()

    conn.close()

    return True


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(
    update,
    context,
    game_id
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
            "❌ فقط سازنده یا مالک می‌تواند لغو کند.",
            show_alert=True
        )

        return

    if game["status"] not in (
        "waiting",
        "creator_turn",
        "opponent_turn",
        "bot_turn"
    ):

        await query.answer(
            "❌ این بازی دیگر قابل لغو نیست.",
            show_alert=True
        )

        return

    success = await refund_game(
        game_id
    )

    await query.answer(
        "✅ بازی لغو شد."
        if success
        else
        "⚠️ بازی قبلاً پردازش شده.",
        show_alert=True
    )

    try:

        await query.message.edit_text(
            """
❌ بازی لغو شد.

💰 موجودی‌های قفل‌شده
برگشت داده شدند.
"""
        )

    except Exception:
        pass


# ============================================================
# ADMIN KEYBOARD
# ============================================================

def admin_keyboard():

    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📊 آمار",
                    callback_data="admin_stats"
                ),
                InlineKeyboardButton(
                    "👥 کاربران",
                    callback_data="admin_users"
                )
            ],
            [
                InlineKeyboardButton(
                    "➕ افزایش موجودی",
                    callback_data="admin_add"
                ),
                InlineKeyboardButton(
                    "➖ کسر موجودی",
                    callback_data="admin_remove"
                )
            ],
            [
                InlineKeyboardButton(
                    "🎮 بازی‌های فعال",
                    callback_data="admin_active"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔴 خاموش کردن",
                    callback_data="admin_toggle"
                )
            ]
        ]
    )


# ============================================================
# ADMIN COMMAND
# ============================================================

async def admin_command(
    update,
    context
):

    user = update.effective_user

    if (
        not user
        or not is_owner(user.id)
    ):

        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )

        return

    await update.message.reply_text(
        """
👑 پنل مدیریت BET_BT

مدیریت کامل کاربران،
موجودی و بازی‌ها:
""",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(
    update,
    context
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

    data = query.data

    # --------------------------------
    # STATS
    # --------------------------------

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

        total = conn.execute(
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

👥 کاربران:
{users}

🎮 بازی‌های فعال:
{active}

🏁 بازی‌های تمام‌شده:
{finished}

💎 مجموع موجودی:
{fmt(total)} TRX
"""
        )

        return

    # --------------------------------
    # USERS
    # --------------------------------

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

            LIMIT 20
            """
        ).fetchall()

        conn.close()

        lines = [
            "👥 کاربران\n"
        ]

        for row in rows:

            lines.append(
                f"""
👤 {row['first_name'] or 'بدون نام'}
🆔 {row['user_id']}
💎 {fmt(row['balance'])} TRX
"""
            )

        await query.answer()

        await query.message.reply_text(
            "\n".join(lines)
        )

        return

    # --------------------------------
    # ACTIVE
    # --------------------------------

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
                f"""
ID: {row['game_id']}
🎮 {row['game_type']}
💰 {fmt(row['bet'])} TRX
📌 {row['status']}
"""
            )

        await query.answer()

        await query.message.reply_text(
            "\n".join(lines)
        )

        return

    # --------------------------------
    # ADD
    # --------------------------------

    if data == "admin_add":

        await query.answer()

        await query.message.reply_text(
            """
➕ افزایش موجودی

فرمت:

/addbalance USER_ID AMOUNT

مثال:

/addbalance 8552447077 100
"""
        )

        return

    # --------------------------------
    # REMOVE
    # --------------------------------

    if data == "admin_remove":

        await query.answer()

        await query.message.reply_text(
            """
➖ کسر موجودی

فرمت:

/removebalance USER_ID AMOUNT

مثال:

/removebalance 8552447077 100
"""
        )

        return

    # --------------------------------
    # TOGGLE
    # --------------------------------

    if data == "admin_toggle":

        new_status = not bot_enabled()

        set_bot_enabled(
            new_status
        )

        await query.answer(
            "تنظیم شد."
        )

        await query.message.reply_text(
            "🟢 ربات روشن شد."
            if new_status
            else
            "🔴 ربات خاموش شد."
        )

        return


# ============================================================
# ADD BALANCE
# ============================================================

async def add_balance_command(
    update,
    context
):

    user = update.effective_user

    if (
        not user
        or not is_owner(user.id)
    ):
        return

    if len(
        context.args
    ) != 2:

        await update.message.reply_text(
            """
فرمت:

/addbalance USER_ID AMOUNT
"""
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
            ).replace(
                ",",
                "."
            )
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

    if not get_user(
        target_id
    ):

        await update.message.reply_text(
            "❌ کاربر وجود ندارد."
        )

        return

    success = change_balance(
        target_id,
        amount,
        "admin_add",
        f"admin:{user.id}"
    )

    if not success:

        await update.message.reply_text(
            "❌ افزایش موجودی انجام نشد."
        )

        return

    await update.message.reply_text(
        f"""
✅ افزایش موجودی انجام شد.

🆔 کاربر:
{target_id}

➕ مبلغ:
{fmt(amount)} TRX

💰 موجودی جدید:
{fmt(get_balance(target_id))} TRX
"""
    )


# ============================================================
# REMOVE BALANCE
# ============================================================

async def remove_balance_command(
    update,
    context
):

    user = update.effective_user

    if (
        not user
        or not is_owner(user.id)
    ):
        return

    if len(
        context.args
    ) != 2:

        await update.message.reply_text(
            """
فرمت:

/removebalance USER_ID AMOUNT
"""
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
            ).replace(
                ",",
                "."
            )
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

    if not get_user(
        target_id
    ):

        await update.message.reply_text(
            "❌ کاربر وجود ندارد."
        )

        return

    success = change_balance(
        target_id,
        -amount,
        "admin_remove",
        f"admin:{user.id}"
    )

    if not success:

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    await update.message.reply_text(
        f"""
✅ کسر موجودی انجام شد.

🆔 کاربر:
{target_id}

➖ مبلغ:
{fmt(amount)} TRX

💰 موجودی جدید:
{fmt(get_balance(target_id))} TRX
"""
    )


# ============================================================
# CLEANUP
# ============================================================

async def cleanup(
    context
):

    cutoff = (
        time.time()
        - GAME_TIMEOUT
    )

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
            cutoff,
        )
    ).fetchall()

    conn.close()

    for row in rows:

        try:

            await refund_game(
                row["game_id"]
            )

        except Exception:

            logger.exception(
                "Cleanup failed"
            )


# ============================================================
# PRIVATE TEXT
# ============================================================

async def private_text_handler(
    update,
    context
):

    if not update.message:
        return

    if (
        update.effective_chat.type
        != ChatType.PRIVATE
    ):
        return

    text = normalize_digits(
        update.message.text or ""
    ).strip()

    if text in (
        "موجودی",
        "💰 موجودی"
    ):

        await balance(
            update,
            context
        )

    elif text in (
        "بازی",
        "🎮 بازی‌ها"
    ):

        await games_menu(
            update,
            context
        )

    elif text in (
        "زیرمجموعه",
        "👥 زیرمجموعه"
    ):

        await referral(
            update,
            context
        )

    elif text in (
        "انتقال",
        "💸 انتقال"
    ):

        await transfer_help(
            update,
            context
        )

    elif text in (
        "راهنما",
        "ℹ️ راهنما"
    ):

        await help_message(
            update,
            context
        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context
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

    # ========================================================
    # COMMANDS
    # ========================================================

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

    # ========================================================
    # PRIVATE
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE
            & filters.TEXT
            & ~filters.COMMAND,
            private_text_handler
        )
    )

    # ========================================================
    # IMPORTANT:
    # USER'S REAL TELEGRAM DICE
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.Dice.ALL,
            user_dice_handler
        )
    )

    # ========================================================
    # GROUP TEXT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS
            & filters.TEXT
            & ~filters.COMMAND,
            group_text_handler
        )
    )

    # ========================================================
    # ADMIN CALLBACKS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # ========================================================
    # GAME CALLBACKS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # ========================================================
    # ERROR
    # ========================================================

    application.add_error_handler(
        error_handler
    )

    # ========================================================
    # CLEANUP
    # ========================================================

    if application.job_queue:

        application.job_queue.run_repeating(
            cleanup,
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
        "USER ROLL MODE: ENABLED"
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
