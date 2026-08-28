# ============================================================
# BET_BT - Telegram Group Games Bot
# Python 3.10+
# python-telegram-bot 20+
#
# IMPORTANT:
# - TRX is INTERNAL/VIRTUAL balance only.
# - NO real TRON transaction is performed.
# - Initial balance = 0
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
import random
import time
from datetime import datetime

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
WIN_PAYOUT = 0.19
REFERRAL_REWARD = 0.05

GAME_TIMEOUT = 180

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BET_BT")


# ============================================================
# DATABASE
# ============================================================

db_lock = asyncio.Lock()


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

    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter_id INTEGER NOT NULL,
            invited_id INTEGER NOT NULL UNIQUE,
            reward REAL NOT NULL DEFAULT 0.05,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS processed_operations (
            operation_id TEXT PRIMARY KEY,
            created_at REAL NOT NULL
        )
    """)

    cur.execute(
        "INSERT OR IGNORE INTO settings(key, value) VALUES('enabled', '1')"
    )

    conn.commit()
    conn.close()


# ============================================================
# HELPERS
# ============================================================

def normalize_digits(text: str) -> str:
    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )
    return text.translate(table)


def parse_amount(text: str):
    text = normalize_digits(text).replace(",", ".").strip()

    m = re.search(r"(\d+(?:\.\d+)?)", text)

    if not m:
        return None

    try:
        amount = round(float(m.group(1)), 4)

        if amount <= 0:
            return None

        return amount

    except Exception:
        return None


def fmt(amount: float) -> str:
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def get_user(user_id: int):
    conn = db_connect()
    row = conn.execute(
        "SELECT * FROM users WHERE user_id = ?",
        (user_id,)
    ).fetchone()
    conn.close()
    return row


def ensure_user(user, referred_by=None):
    if not user:
        return

    conn = db_connect()

    existing = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?",
        (user.id,)
    ).fetchone()

    if existing:
        conn.execute("""
            UPDATE users
            SET username = ?, first_name = ?
            WHERE user_id = ?
        """, (
            user.username or "",
            user.first_name or "",
            user.id
        ))
    else:
        if referred_by == user.id:
            referred_by = None

        conn.execute("""
            INSERT INTO users(
                user_id,
                username,
                first_name,
                balance,
                referred_by
            )
            VALUES (?, ?, ?, 0, ?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
            referred_by
        ))

    conn.commit()
    conn.close()


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


def bot_enabled() -> bool:
    conn = db_connect()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = 'enabled'"
    ).fetchone()
    conn.close()

    if not row:
        return True

    return row["value"] == "1"


def set_bot_enabled(enabled: bool):
    conn = db_connect()

    conn.execute("""
        INSERT INTO settings(key, value)
        VALUES('enabled', ?)
        ON CONFLICT(key)
        DO UPDATE SET value = excluded.value
    """, ("1" if enabled else "0",))

    conn.commit()
    conn.close()


def get_balance(user_id: int) -> float:
    conn = db_connect()
    row = conn.execute(
        "SELECT balance FROM users WHERE user_id = ?",
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
    reference: str = ""
) -> bool:

    amount = round(float(amount), 4)

    conn = db_connect()

    try:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if not row:
            conn.rollback()
            return False

        old_balance = float(row["balance"])
        new_balance = round(old_balance + amount, 4)

        if new_balance < -0.00001:
            conn.rollback()
            return False

        conn.execute("""
            UPDATE users
            SET balance = ?
            WHERE user_id = ?
        """, (
            new_balance,
            user_id
        ))

        conn.execute("""
            INSERT INTO transactions(
                user_id,
                amount,
                kind,
                reference
            )
            VALUES (?, ?, ?, ?)
        """, (
            user_id,
            amount,
            kind,
            reference
        ))

        conn.commit()
        return True

    except Exception:
        conn.rollback()
        logger.exception("change_balance failed")
        return False

    finally:
        conn.close()


def lock_operation(operation_id: str) -> bool:
    conn = db_connect()

    try:
        conn.execute(
            "INSERT INTO processed_operations(operation_id, created_at) VALUES(?, ?)",
            (operation_id, time.time())
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

async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:

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
            "creator"
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

    text = """
🔒 برای استفاده از ربات ابتدا باید در کانال عضو شوید.

بعد از عضویت روی «بررسی عضویت» بزنید.
"""

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
# START
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    referred_by = None

    if context.args:
        arg = str(context.args[0]).strip()

        if arg.startswith("ref_"):
            try:
                referred_by = int(arg.replace("ref_", ""))
            except Exception:
                referred_by = None

    ensure_user(user, referred_by)

    if not await check_join(update, context):
        return

    text = f"""
🎮 BET_BT

سلام {user.first_name or 'دوست عزیز'} 👋

ربات بازی گروهی آماده است.

🎲 تاس
🎳 بولینگ
🎯 دارت
🏀 بسکتبال

🤖 بازی با ربات
اول کاربر رول می‌کند، سپس ربات.

👥 بازی با دوستان
اول سازنده رول می‌کند، سپس حریف.

💰 موجودی داخلی TRX

💸 انتقال با Reply

👥 پاداش زیرمجموعه: 0.05

برای شروع یکی از دکمه‌های پایین را انتخاب کن.
"""

    await update.message.reply_text(
        text,
        reply_markup=private_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if not await check_join(update, context):
        return

    amount = get_balance(user.id)

    await update.message.reply_text(
        f"""
💰 موجودی

👤 {user.first_name or 'کاربر'}

💎 {fmt(amount)} TRX
"""
    )


# ============================================================
# REFERRAL
# ============================================================

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if not await check_join(update, context):
        return

    bot_username = context.bot.username

    link = f"https://t.me/{bot_username}?start=ref_{user.id}"

    conn = db_connect()

    count_row = conn.execute("""
        SELECT COUNT(*) AS c
        FROM referrals
        WHERE inviter_id = ?
    """, (user.id,)).fetchone()

    count = int(count_row["c"])

    conn.close()

    await update.message.reply_text(
        f"""
👥 زیرمجموعه

🔗 لینک دعوت شما:

{link}

🎁 پاداش هر دعوت: 0.05

👤 تعداد زیرمجموعه: {count}
"""
    )


# ============================================================
# REFERRAL PROCESS
# ============================================================

def process_referral(user_id: int):

    conn = db_connect()

    try:
        conn.execute("BEGIN IMMEDIATE")

        user = conn.execute("""
            SELECT referred_by, referral_paid
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if not user:
            conn.rollback()
            return

        inviter_id = user["referred_by"]

        if not inviter_id:
            conn.rollback()
            return

        if int(user["referral_paid"]) == 1:
            conn.rollback()
            return

        if inviter_id == user_id:
            conn.rollback()
            return

        inviter = conn.execute("""
            SELECT user_id
            FROM users
            WHERE user_id = ?
        """, (inviter_id,)).fetchone()

        if not inviter:
            conn.rollback()
            return

        conn.execute("""
            UPDATE users
            SET balance = balance + ?
            WHERE user_id = ?
        """, (
            REFERRAL_REWARD,
            inviter_id
        ))

        conn.execute("""
            UPDATE users
            SET referral_paid = 1
            WHERE user_id = ?
        """, (user_id,))

        conn.execute("""
            INSERT OR IGNORE INTO referrals(
                inviter_id,
                invited_id,
                reward
            )
            VALUES (?, ?, ?)
        """, (
            inviter_id,
            user_id,
            REFERRAL_REWARD
        ))

        conn.execute("""
            INSERT INTO transactions(
                user_id,
                amount,
                kind,
                reference
            )
            VALUES (?, ?, ?, ?)
        """, (
            inviter_id,
            REFERRAL_REWARD,
            "referral",
            str(user_id)
        ))

        conn.commit()

    except Exception:
        conn.rollback()
        logger.exception("Referral failed")

    finally:
        conn.close()


# ============================================================
# TRANSFER HELP
# ============================================================

async def transfer_help(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        """
💸 انتقال با Reply

روی پیام کاربری که می‌خواهی برایش موجودی بفرستی Reply کن و بنویس:

انتقال 0.1

مثال:

انتقال 1

⚠️ انتقال فقط داخل گپ انجام می‌شود.
"""
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
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

    if not await check_join(update, context):
        return

    if not update.message.reply_to_message:
        await update.message.reply_text(
            "❌ باید روی پیام کاربر Reply کنی.\n\nمثال:\nانتقال 0.1"
        )
        return

    target = update.message.reply_to_message.from_user

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

    amount = parse_amount(update.message.text)

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

    if not lock_operation(operation_id):
        await update.message.reply_text(
            "⚠️ این انتقال قبلاً پردازش شده است."
        )
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
            f"failed_to:{target.id}"
        )

        await update.message.reply_text(
            "❌ انتقال انجام نشد؛ موجودی برگشت داده شد."
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
# GAMES MENU PRIVATE
# ============================================================

async def games_menu(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return

    if not await check_join(update, context):
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
            )
        ],
        [
            InlineKeyboardButton(
                "🎯 دارت",
                callback_data="game_darts"
            ),
            InlineKeyboardButton(
                "🏀 بسکتبال",
                callback_data="game_basketball"
            )
        ]
    ])

    await update.message.reply_text(
        """
🎮 بازی‌ها

برای بازی در گپ بنویس:

1 تاس 0.1
1 بولینگ 0.1
1 دارت 0.1
1 بسکتبال 0.1

همچنین اعداد فارسی هم قبول می‌شود.

مثال:

۱ تاس ۰.۱
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

    await update.message.reply_text(
        """
ℹ️ راهنمای BET_BT

🎮 ساخت بازی در گپ:

1 تاس 0.1
1 بولینگ 0.1
1 دارت 0.1
1 بسکتبال 0.1

🤖 بازی با ربات:
کاربر اول رول می‌کند.
بعد ربات رول می‌کند.

👥 بازی با دوستان:
سازنده اول رول می‌کند.
بعد حریف رول می‌کند.

💰 موجودی:
موجودی

💸 انتقال:
روی پیام کاربر Reply کن و بنویس:

انتقال 0.1

👥 زیرمجموعه:
دکمه زیرمجموعه

🔒 بازی‌ها و تراکنش‌ها قفل دارند تا دوبار کسر نشوند.
"""
    )


# ============================================================
# GAME CONFIG
# ============================================================

GAME_TYPES = {
    "dice": {
        "name": "تاس",
        "emoji": "🎲",
        "telegram_dice": "🎲",
    },
    "bowling": {
        "name": "بولینگ",
        "emoji": "🎳",
        "telegram_dice": "🎳",
    },
    "darts": {
        "name": "دارت",
        "emoji": "🎯",
        "telegram_dice": "🎯",
    },
    "basketball": {
        "name": "بسکتبال",
        "emoji": "🏀",
        "telegram_dice": "🏀",
    },
}


def detect_game(text: str):
    normalized = normalize_digits(text).lower()

    for key, data in GAME_TYPES.items():

        if data["name"] in normalized:
            return key

        if key in normalized:
            return key

    return None


def parse_group_game(text: str):

    normalized = normalize_digits(text)

    game_type = detect_game(normalized)

    if not game_type:
        return None

    amount = parse_amount(normalized)

    if amount is None:
        return None

    # مقدار اول «1» تعداد بازی است.
    # فعلاً هر پیام یک بازی ایجاد می‌کند.
    if amount < MIN_BET:
        return None

    return game_type, amount


# ============================================================
# CREATE GAME
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
        await transfer_command(update, context)
        return

    # موجودی در گپ
    if normalize_digits(text).strip() in (
        "موجودی",
        "💰 موجودی",
        "balance"
    ):
        await balance(update, context)
        return

    # بازی
    parsed = parse_group_game(text)

    if not parsed:
        return

    if not bot_enabled():
        await message.reply_text(
            "🔴 ربات بازی در حال حاضر خاموش است."
        )
        return

    user = update.effective_user

    if not user:
        return

    if user.is_bot:
        return

    ensure_user(user)

    if not await check_join(update, context):
        return

    game_type, bet = parsed

    current_balance = get_balance(user.id)

    if current_balance < bet:
        await message.reply_text(
            f"""
❌ موجودی کافی نیست.

💎 موجودی: {fmt(current_balance)} TRX
💰 مبلغ بازی: {fmt(bet)} TRX
"""
        )
        return

    # جلوگیری از چند بازی همزمان سازنده
    conn = db_connect()

    active = conn.execute("""
        SELECT game_id
        FROM games
        WHERE creator_id = ?
        AND status IN ('waiting', 'creator_turn', 'opponent_turn')
        AND chat_id = ?
        LIMIT 1
    """, (
        user.id,
        update.effective_chat.id
    )).fetchone()

    conn.close()

    if active:
        await message.reply_text(
            "⚠️ شما همین الان یک بازی فعال دارید."
        )
        return

    # قفل و کسر اولیه
    operation_id = (
        f"game_create:"
        f"{update.effective_chat.id}:"
        f"{message.message_id}"
    )

    if not lock_operation(operation_id):
        return

    if not change_balance(
        user.id,
        -bet,
        "game_lock",
        operation_id
    ):
        await message.reply_text(
            "❌ موجودی کافی نیست یا تراکنش قفل شد."
        )
        return

    now = time.time()

    conn = db_connect()

    cur = conn.execute("""
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
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        update.effective_chat.id,
        message.message_id,
        user.id,
        game_type,
        bet,
        "waiting",
        "waiting",
        now,
        now
    ))

    game_id = cur.lastrowid

    conn.commit()
    conn.close()

    data = GAME_TYPES[game_type]

    keyboard = InlineKeyboardMarkup([
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
    ])

    try:

        sent = await message.reply_text(
            f"""
🎮 بازی جدید {data['emoji']}

👤 سازنده: {user.first_name}

🎮 بازی: {data['name']}

💰 مبلغ: {fmt(bet)} TRX

🤖 بازی با ربات:
اول سازنده رول می‌کند، بعد ربات.

👥 بازی با دوستان:
سازنده اول رول می‌کند، سپس حریف.

🏆 جایزه برنده: {fmt(WIN_PAYOUT)} TRX

یکی از گزینه‌ها را انتخاب کنید.
""",
            reply_markup=keyboard
        )

        conn = db_connect()

        conn.execute("""
            UPDATE games
            SET message_id = ?
            WHERE game_id = ?
        """, (
            sent.message_id,
            game_id
        ))

        conn.commit()
        conn.close()

    except Exception:
        await refund_game(game_id)


# ============================================================
# GAME CALLBACK
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    if not query:
        return

    await query.answer()

    user = query.from_user

    ensure_user(user)

    data = query.data or ""

    # -----------------------------------------
    # JOIN CHECK
    # -----------------------------------------

    if data == "check_join":

        if await check_join(update, context):

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

        return

    # -----------------------------------------
    # PRIVATE GAME INFO
    # -----------------------------------------

    if data.startswith("game_"):

        game_key = data.replace("game_", "")

        if game_key not in GAME_TYPES:
            return

        await query.answer(
            f"🎮 برای بازی {GAME_TYPES[game_key]['name']} در گپ بنویس: 1 {GAME_TYPES[game_key]['name']} 0.1",
            show_alert=True
        )

        return

    # -----------------------------------------
    # GAME ACTION
    # -----------------------------------------

    if ":" not in data:
        return

    action, value = data.split(":", 1)

    try:
        game_id = int(value)
    except Exception:
        return

    if action == "joinbot":
        await join_bot_game(update, context, game_id)
        return

    if action == "joinfriend":
        await join_friend_game(update, context, game_id)
        return

    if action == "cancel":
        await cancel_game(update, context, game_id)
        return

    if action == "roll":
        await roll_game(update, context, game_id)
        return


# ============================================================
# GET GAME
# ============================================================

def get_game(game_id: int):

    conn = db_connect()

    row = conn.execute("""
        SELECT *
        FROM games
        WHERE game_id = ?
    """, (game_id,)).fetchone()

    conn.close()

    return row


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

    if not await check_join(update, context):
        return

    game = get_game(game_id)

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

    bet = float(game["bet"])

    # وضعیت را قبل از رول قفل می‌کنیم
    conn = db_connect()

    cur = conn.execute("""
        UPDATE games
        SET mode = 'bot',
            status = 'creator_turn',
            updated_at = ?
        WHERE game_id = ?
        AND status = 'waiting'
    """, (
        time.time(),
        game_id
    ))

    changed = cur.rowcount

    conn.commit()
    conn.close()

    if changed != 1:
        await query.answer(
            "❌ بازی قبلاً شروع شده.",
            show_alert=True
        )
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎲 رول من",
                callback_data=f"roll:{game_id}"
            )
        ]
    ])

    await query.message.edit_text(
        f"""
🤖 بازی با ربات

👤 {user.first_name}

🎮 {GAME_TYPES[game['game_type']]['emoji']} {GAME_TYPES[game['game_type']]['name']}

💰 مبلغ: {fmt(bet)} TRX

⬇️ اول خودت رول کن.
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

    if not await check_join(update, context):
        return

    game = get_game(game_id)

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

    bet = float(game["bet"])

    if get_balance(user.id) < bet:
        await query.answer(
            "❌ موجودی کافی نیست.",
            show_alert=True
        )
        return

    # اول کسر حریف
    operation_id = f"friend_join:{game_id}:{user.id}"

    if not lock_operation(operation_id):
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

    cur = conn.execute("""
        UPDATE games
        SET opponent_id = ?,
            mode = 'friends',
            status = 'creator_turn',
            updated_at = ?
        WHERE game_id = ?
        AND status = 'waiting'
    """, (
        user.id,
        time.time(),
        game_id
    ))

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
            "❌ بازی قبلاً گرفته شده؛ مبلغ برگشت خورد.",
            show_alert=True
        )

        return

    creator = get_user(game["creator_id"])

    creator_name = (
        creator["first_name"]
        if creator
        else "سازنده"
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

🎮 {GAME_TYPES[game['game_type']]['emoji']} {GAME_TYPES[game['game_type']]['name']}

💰 مبلغ: {fmt(bet)} TRX

⬇️ ابتدا سازنده باید رول کند.
""",
        reply_markup=keyboard
    )


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

    if not await check_join(update, context):
        return

    game = get_game(game_id)

    if not game:
        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )
        return

    if game["status"] not in (
        "creator_turn",
        "opponent_turn"
    ):
        await query.answer(
            "❌ نوبت رول نیست.",
            show_alert=True
        )
        return

    is_creator = user.id == game["creator_id"]
    is_opponent = user.id == game["opponent_id"]

    if game["status"] == "creator_turn":

        if not is_creator:
            await query.answer(
                "⏳ هنوز نوبت سازنده است.",
                show_alert=True
            )
            return

    elif game["status"] == "opponent_turn":

        if not is_opponent:
            await query.answer(
                "⏳ هنوز نوبت حریف است.",
                show_alert=True
            )
            return

    operation_id = f"roll:{game_id}:{user.id}:{game['status']}"

    if not lock_operation(operation_id):
        await query.answer(
            "⚠️ این رول قبلاً انجام شده.",
            show_alert=True
        )
        return

    game_type = game["game_type"]
    emoji = GAME_TYPES[game_type]["telegram_dice"]

    # --------------------------------------------------------
    # ارسال انیمیشن واقعی تلگرام
    # --------------------------------------------------------

    try:
        sent = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=emoji
        )

        roll_value = sent.dice.value

    except Exception:

        await refund_game(game_id)

        try:
            await query.message.edit_text(
                "🛡️ بازی با خطا مواجه شد.\n\n"
                "💰 مبلغ بازی به سازنده و حریف برگشت داده شد."
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # ذخیره رول
    # --------------------------------------------------------

    if game["status"] == "creator_turn":

        conn = db_connect()

        conn.execute("""
            UPDATE games
            SET creator_roll = ?,
                status = CASE
                    WHEN mode = 'bot'
                    THEN 'bot_turn'
                    ELSE 'opponent_turn'
                END,
                updated_at = ?
            WHERE game_id = ?
            AND status = 'creator_turn'
        """, (
            roll_value,
            time.time(),
            game_id
        ))

        conn.commit()
        conn.close()

        # ---------------------------------------------
        # بازی با ربات
        # ---------------------------------------------

        if game["mode"] == "bot":

            await query.message.reply_text(
                f"""
👤 {user.first_name} رول کرد: {roll_value}

🤖 حالا ربات رول می‌کند...
"""
            )

            await asyncio.sleep(1)

            await bot_roll(
                context,
                game_id
            )

            return

        # ---------------------------------------------
        # بازی دوستان
        # ---------------------------------------------

        opponent = get_user(game["opponent_id"])

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

        try:
            await query.message.edit_text(
                f"""
👥 بازی با دوستان

👤 سازنده: {user.first_name}
🎲 نتیجه رول سازنده: {roll_value}

👤 حریف: {opponent_name}

⬇️ حالا نوبت حریف است.
""",
                reply_markup=keyboard
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # OPPONENT ROLL
    # --------------------------------------------------------

    if game["status"] == "opponent_turn":

        conn = db_connect()

        conn.execute("""
            UPDATE games
            SET opponent_roll = ?,
                status = 'finishing',
                updated_at = ?
            WHERE game_id = ?
            AND status = 'opponent_turn'
        """, (
            roll_value,
            time.time(),
            game_id
        ))

        conn.commit()
        conn.close()

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

    game = get_game(game_id)

    if not game:
        return

    if game["status"] != "bot_turn":
        return

    try:

        sent = await context.bot.send_dice(
            chat_id=game["chat_id"],
            emoji=GAME_TYPES[game["game_type"]]["telegram_dice"]
        )

        bot_value = sent.dice.value

    except Exception:

        await refund_game(game_id)

        try:
            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    "🛡️ بازی ربات با خطا مواجه شد.\n\n"
                    "💰 مبلغ کاربر برگشت داده شد."
                )
            )
        except Exception:
            pass

        return

    conn = db_connect()

    conn.execute("""
        UPDATE games
        SET opponent_roll = ?,
            status = 'finishing',
            updated_at = ?
        WHERE game_id = ?
        AND status = 'bot_turn'
    """, (
        bot_value,
        time.time(),
        game_id
    ))

    conn.commit()
    conn.close()

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

    game = get_game(game_id)

    if not game:
        return

    creator = get_user(game["creator_id"])

    creator_name = (
        creator["first_name"]
        if creator
        else "کاربر"
    )

    creator_roll = game["creator_roll"]
    bot_roll_value = game["opponent_roll"]

    if creator_roll is None or bot_roll_value is None:
        await refund_game(game_id)
        return

    # برابر
    if creator_roll == bot_roll_value:

        # برای حالت مساوی، مبلغ بازی آزاد می‌شود
        change_balance(
            game["creator_id"],
            game["bet"],
            "game_draw_refund",
            f"game:{game_id}"
        )

        result = f"""
🤝 بازی مساوی شد.

👤 {creator_name}: {creator_roll}
🤖 ربات: {bot_roll_value}

💰 مبلغ {fmt(game['bet'])} TRX برگشت داده شد.
"""

    elif creator_roll > bot_roll_value:

        # مبلغ قبلاً از کاربر قفل شده
        # جایزه 0.19
        change_balance(
            game["creator_id"],
            WIN_PAYOUT,
            "game_win",
            f"game:{game_id}"
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}: {creator_roll}
🤖 ربات: {bot_roll_value}

🥇 برنده: {creator_name}

💰 جایزه: {fmt(WIN_PAYOUT)} TRX
"""

    else:

        # کاربر باخته و مبلغ قفل‌شده تسویه شده
        result = f"""
🏆 نتیجه بازی

👤 {creator_name}: {creator_roll}
🤖 ربات: {bot_roll_value}

🥇 برنده: 🤖 ربات

💰 مبلغ بازی به کاربر تعلق نگرفت.
"""

    conn = db_connect()

    conn.execute("""
        UPDATE games
        SET status = 'finished',
            updated_at = ?
        WHERE game_id = ?
    """, (
        time.time(),
        game_id
    ))

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

    game = get_game(game_id)

    if not game:
        return

    creator = get_user(game["creator_id"])
    opponent = get_user(game["opponent_id"])

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

    creator_roll = game["creator_roll"]
    opponent_roll = game["opponent_roll"]

    if creator_roll is None or opponent_roll is None:
        await refund_game(game_id)
        return

    if creator_roll == opponent_roll:

        # هر دو مبلغ خودشان را پس می‌گیرند
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

👤 {creator_name}: {creator_roll}
👤 {opponent_name}: {opponent_roll}

⚖️ بازی مساوی شد.

💰 مبلغ هر دو نفر برگشت داده شد.
"""

    elif creator_roll > opponent_roll:

        change_balance(
            game["creator_id"],
            WIN_PAYOUT,
            "game_win",
            f"game:{game_id}"
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}: {creator_roll}
👤 {opponent_name}: {opponent_roll}

🥇 برنده: {creator_name}

💰 جایزه: {fmt(WIN_PAYOUT)} TRX
"""

    else:

        change_balance(
            game["opponent_id"],
            WIN_PAYOUT,
            "game_win",
            f"game:{game_id}"
        )

        result = f"""
🏆 نتیجه بازی

👤 {creator_name}: {creator_roll}
👤 {opponent_name}: {opponent_roll}

🥇 برنده: {opponent_name}

💰 جایزه: {fmt(WIN_PAYOUT)} TRX
"""

    conn = db_connect()

    conn.execute("""
        UPDATE games
        SET status = 'finished',
            updated_at = ?
        WHERE game_id = ?
    """, (
        time.time(),
        game_id
    ))

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

async def refund_game(game_id: int):

    operation_id = f"refund:{game_id}"

    if not lock_operation(operation_id):
        return False

    game = get_game(game_id)

    if not game:
        return False

    status = game["status"]

    if status == "finished":
        return False

    # سازنده همیشه مبلغ اولیه را قفل کرده
    change_balance(
        game["creator_id"],
        float(game["bet"]),
        "game_refund",
        f"game:{game_id}"
    )

    # اگر حریف وارد شده بود، مبلغ او هم قفل شده
    if game["opponent_id"]:
        change_balance(
            game["opponent_id"],
            float(game["bet"]),
            "game_refund",
            f"game:{game_id}"
        )

    conn = db_connect()

    conn.execute("""
        UPDATE games
        SET status = 'refunded',
            updated_at = ?
        WHERE game_id = ?
    """, (
        time.time(),
        game_id
    ))

    conn.commit()
    conn.close()

    return True


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    game_id: int
):

    query = update.callback_query
    user = query.from_user

    game = get_game(game_id)

    if not game:
        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )
        return

    if game["creator_id"] != user.id and not is_owner(user.id):
        await query.answer(
            "❌ فقط سازنده یا مالک می‌تواند بازی را لغو کند.",
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

    await refund_game(game_id)

    await query.answer(
        "✅ بازی لغو شد و موجودی برگشت خورد."
    )

    try:
        await query.message.edit_text(
            "❌ بازی لغو شد.\n\n"
            "💰 موجودی‌های قفل‌شده برگشت داده شدند."
        )
    except Exception:
        pass


# ============================================================
# ADMIN PANEL
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
    ])


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_owner(user.id):
        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )
        return

    await update.message.reply_text(
        """
👑 پنل مدیریت BET_BT

مدیریت کامل کاربران، موجودی و بازی‌ها:
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

    if not is_owner(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )
        return

    data = query.data

    # -----------------------------
    # STATS
    # -----------------------------

    if data == "admin_stats":

        conn = db_connect()

        users = conn.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        active_games = conn.execute("""
            SELECT COUNT(*) AS c
            FROM games
            WHERE status IN (
                'waiting',
                'creator_turn',
                'opponent_turn',
                'bot_turn',
                'finishing'
            )
        """).fetchone()["c"]

        finished = conn.execute("""
            SELECT COUNT(*) AS c
            FROM games
            WHERE status = 'finished'
        """).fetchone()["c"]

        total_balance = conn.execute("""
            SELECT COALESCE(SUM(balance), 0) AS s
            FROM users
        """).fetchone()["s"]

        conn.close()

        await query.answer()

        await query.message.reply_text(
            f"""
📊 آمار

👥 کاربران: {users}

🎮 بازی‌های فعال: {active_games}

🏁 بازی‌های تمام‌شده: {finished}

💎 مجموع موجودی داخلی:
{fmt(float(total_balance))} TRX
"""
        )

        return

    # -----------------------------
    # USERS
    # -----------------------------

    if data == "admin_users":

        conn = db_connect()

        rows = conn.execute("""
            SELECT user_id, first_name, username, balance
            FROM users
            ORDER BY balance DESC
            LIMIT 20
        """).fetchall()

        conn.close()

        lines = ["👥 کاربران برتر\n"]

        for row in rows:

            name = row["first_name"] or "بدون نام"

            lines.append(
                f"👤 {name}\n"
                f"🆔 {row['user_id']}\n"
                f"💎 {fmt(float(row['balance']))} TRX\n"
            )

        await query.answer()

        await query.message.reply_text(
            "\n".join(lines)
        )

        return

    # -----------------------------
    # ACTIVE GAMES
    # -----------------------------

    if data == "admin_active":

        conn = db_connect()

        rows = conn.execute("""
            SELECT game_id,
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
        """).fetchall()

        conn.close()

        if not rows:

            await query.answer()

            await query.message.reply_text(
                "🎮 بازی فعال وجود ندارد."
            )

            return

        lines = ["🎮 بازی‌های فعال\n"]

        for row in rows:

            lines.append(
                f"ID: {row['game_id']}\n"
                f"🎮 {row['game_type']}\n"
                f"💰 {fmt(float(row['bet']))}\n"
                f"📌 {row['status']}\n"
            )

        await query.answer()

        await query.message.reply_text(
            "\n".join(lines)
        )

        return

    # -----------------------------
    # ADD BALANCE
    # -----------------------------

    if data == "admin_add":

        context.user_data["admin_action"] = "add"

        await query.answer()

        await query.message.reply_text(
            """
➕ افزایش موجودی

به همین پیام Reply نکن؛ یک پیام جدید بفرست:

/addbalance USER_ID AMOUNT

مثال:

/addbalance 8552447077 1000
"""
        )

        return

    # -----------------------------
    # REMOVE BALANCE
    # -----------------------------

    if data == "admin_remove":

        context.user_data["admin_action"] = "remove"

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

    # -----------------------------
    # TOGGLE
    # -----------------------------

    if data == "admin_toggle":

        current = bot_enabled()

        set_bot_enabled(not current)

        await query.answer(
            "تنظیم شد."
        )

        await query.message.reply_text(
            "🟢 ربات روشن شد."
            if not current
            else
            "🔴 ربات خاموش شد."
        )

        return


# ============================================================
# ADMIN COMMANDS
# ============================================================

async def add_balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_owner(user.id):
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "فرمت:\n/addbalance USER_ID AMOUNT"
        )
        return

    try:
        target_id = int(
            normalize_digits(context.args[0])
        )

        amount = float(
            normalize_digits(context.args[1])
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

    target = get_user(target_id)

    if not target:

        await update.message.reply_text(
            "❌ کاربر در دیتابیس وجود ندارد."
        )
        return

    if not change_balance(
        target_id,
        amount,
        "admin_add",
        f"admin:{user.id}"
    ):
        await update.message.reply_text(
            "❌ افزایش موجودی انجام نشد."
        )
        return

    new_balance = get_balance(target_id)

    await update.message.reply_text(
        f"""
✅ افزایش موجودی انجام شد.

🆔 {target_id}

➕ {fmt(amount)} TRX

💰 موجودی جدید:
{fmt(new_balance)} TRX
"""
    )


async def remove_balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user or not is_owner(user.id):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "فرمت:\n/removebalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(context.args[0])
        )

        amount = float(
            normalize_digits(context.args[1])
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

    target = get_user(target_id)

    if not target:

        await update.message.reply_text(
            "❌ کاربر وجود ندارد."
        )

        return

    if not change_balance(
        target_id,
        -amount,
        "admin_remove",
        f"admin:{user.id}"
    ):

        await update.message.reply_text(
            "❌ موجودی کاربر کافی نیست."
        )

        return

    new_balance = get_balance(target_id)

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
# AUTO CLEANUP
# ============================================================

async def cleanup_stuck_games(
    context: ContextTypes.DEFAULT_TYPE
):

    now = time.time()

    conn = db_connect()

    rows = conn.execute("""
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
    """, (
        now - GAME_TIMEOUT,
    )).fetchall()

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
# PRIVATE TEXT HANDLER
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
        "💰 موجودی"
    ):
        await balance(update, context)
        return

    if text in (
        "بازی",
        "🎮 بازی‌ها"
    ):
        await games_menu(update, context)
        return

    if text in (
        "زیرمجموعه",
        "👥 زیرمجموعه"
    ):
        await referral(update, context)
        return

    if text in (
        "انتقال",
        "💸 انتقال"
    ):
        await transfer_help(update, context)
        return

    if text in (
        "راهنما",
        "ℹ️ راهنما"
    ):
        await help_message(update, context)
        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Unhandled exception:",
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
    # PRIVATE BUTTONS
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
    # GROUP
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
    # CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

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

    print("========================================")
    print("BET_BT BOT STARTED")
    print("BOT_TOKEN: OK")
    print("OWNER:", OWNER_ID)
    print("CHANNEL:", CHANNEL_USERNAME)
    print("DATABASE:", DB_FILE)
    print("========================================")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
