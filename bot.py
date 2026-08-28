# ============================================================
# BOT.PY
# Telegram Game Bot
# Python 3.10+
# python-telegram-bot 20+
#
# امکانات:
# 🎲 تاس
# 🎳 بولینگ
# 🏀 بسکتبال
# 🎯 دارت
#
# مثال:
# 1 تاس 0.5
# 2 تاس 0.1
# 2 تاس 0.1
# 10 بولینگ 0.5
#
# بازی با دوستان:
# سازنده -> خودش رول می‌کند
# حریف -> خودش رول می‌کند
#
# بازی با ربات:
# کاربر -> خودش رول می‌کند
# ربات -> بعد از اتمام رول‌های کاربر رول می‌کند
#
# تسویه:
# 95% برنده
# 2% مالک
# 3% کارمزد
#
# موجودی اولیه: صفر
#
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from contextlib import closing

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatType
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

ADMIN_IDS = {
    8552447077
}

DB_FILE = "bot.db"

# هیچ موجودی اولیه‌ای وجود ندارد
START_BALANCE = 0

# تعداد بازی محدود نیست
MIN_AMOUNT = Decimal("0.01")

# حداکثر مبلغ
MAX_AMOUNT = Decimal("1000000")

# سهم‌ها
WINNER_PERCENT = Decimal("0.95")
OWNER_PERCENT = Decimal("0.02")
FEE_PERCENT = Decimal("0.03")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)


# ============================================================
# DECIMAL HELPERS
# ============================================================

SCALE = Decimal("100")


def amount_to_int(value):
    """
    0.50 -> 50
    1 -> 100
    """
    try:
        d = Decimal(str(value)).quantize(
            Decimal("0.01"),
            rounding=ROUND_DOWN
        )
    except Exception:
        return None

    return int(d * SCALE)


def int_to_amount(value):
    """
    50 -> 0.50
    100 -> 1.00
    """
    return Decimal(int(value)) / SCALE


def format_amount(value):
    d = int_to_amount(int(value))
    s = format(d, "f").rstrip("0").rstrip(".")

    if "." not in s:
        return s

    return s


def parse_amount(text):
    if not text:
        return None

    text = normalize_digits(text)

    text = text.replace(",", ".")
    text = text.replace("٬", "")
    text = text.strip()

    m = re.search(
        r"(\d+(?:\.\d+)?)",
        text
    )

    if not m:
        return None

    try:
        amount = Decimal(m.group(1))
    except InvalidOperation:
        return None

    if amount < MIN_AMOUNT:
        return None

    if amount > MAX_AMOUNT:
        return None

    amount = amount.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )

    result = amount_to_int(amount)

    if result is None or result <= 0:
        return None

    return result


# ============================================================
# DATABASE
# ============================================================

def db():
    con = sqlite3.connect(
        DB_FILE,
        timeout=30,
        isolation_level=None
    )

    con.row_factory = sqlite3.Row

    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")

    return con


def init_db():

    with closing(db()) as con:

        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance INTEGER NOT NULL DEFAULT 0,
            blocked INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            receiver_id INTEGER,
            amount INTEGER,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount INTEGER,
            wallet TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,

            chat_id INTEGER NOT NULL,
            message_id INTEGER DEFAULT 0,

            creator_id INTEGER NOT NULL,
            opponent_id INTEGER DEFAULT NULL,

            game_type TEXT NOT NULL,
            rolls INTEGER NOT NULL,
            amount INTEGER NOT NULL,

            status TEXT NOT NULL DEFAULT 'waiting',

            creator_rolls TEXT DEFAULT '',
            opponent_rolls TEXT DEFAULT '',

            creator_total INTEGER DEFAULT 0,
            opponent_total INTEGER DEFAULT 0,

            winner_id INTEGER DEFAULT NULL,

            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
        """)

        # برداشت به صورت پیش‌فرض روشن
        con.execute("""
        INSERT OR IGNORE INTO settings(key, value)
        VALUES('withdraw_enabled', '1')
        """)

        con.commit()


# ============================================================
# USER
# ============================================================

def ensure_user(user):

    if not user:
        return

    with closing(db()) as con:

        row = con.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user.id,)
        ).fetchone()

        if row:

            con.execute("""
            UPDATE users
            SET username=?,
                first_name=?
            WHERE user_id=?
            """, (
                user.username or "",
                user.first_name or "",
                user.id
            ))

        else:

            con.execute("""
            INSERT INTO users
            (
                user_id,
                username,
                first_name,
                balance
            )
            VALUES (?, ?, ?, ?)
            """, (
                user.id,
                user.username or "",
                user.first_name or "",
                START_BALANCE
            ))

        con.commit()


def get_user(user_id):

    with closing(db()) as con:

        return con.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()


def get_balance(user_id):

    row = get_user(user_id)

    if not row:
        return 0

    return int(row["balance"])


def is_blocked(user_id):

    row = get_user(user_id)

    return bool(
        row and
        int(row["blocked"]) == 1
    )


def is_admin(user_id):

    return user_id in ADMIN_IDS


# ============================================================
# BALANCE SAFE OPERATIONS
# ============================================================

def change_balance(user_id, amount):

    amount = int(amount)

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user_id,)
            ).fetchone()

            if not row:
                con.rollback()
                return False

            old_balance = int(row["balance"])
            new_balance = old_balance + amount

            if new_balance < 0:
                con.rollback()
                return False

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                new_balance,
                user_id
            ))

            con.commit()

            return True

        except Exception:

            con.rollback()
            logger.exception("CHANGE BALANCE ERROR")

            return False


def transfer_balance(sender, receiver, amount):

    amount = int(amount)

    if amount <= 0:
        return False

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            sender_row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (sender,)
            ).fetchone()

            receiver_row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (receiver,)
            ).fetchone()

            if not sender_row or not receiver_row:
                con.rollback()
                return False

            sender_balance = int(
                sender_row["balance"]
            )

            if sender_balance < amount:
                con.rollback()
                return False

            con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            """, (
                amount,
                sender
            ))

            con.execute("""
            UPDATE users
            SET balance=balance+?
            WHERE user_id=?
            """, (
                amount,
                receiver
            ))

            con.execute("""
            INSERT INTO transfers
            (
                sender_id,
                receiver_id,
                amount
            )
            VALUES (?, ?, ?)
            """, (
                sender,
                receiver,
                amount
            ))

            con.commit()

            return True

        except Exception:

            con.rollback()
            logger.exception("TRANSFER ERROR")

            return False


# ============================================================
# SETTINGS
# ============================================================

def get_setting(key, default=""):

    with closing(db()) as con:

        row = con.execute(
            "SELECT value FROM settings WHERE key=?",
            (key,)
        ).fetchone()

        if not row:
            return default

        return row["value"]


def set_setting(key, value):

    with closing(db()) as con:

        con.execute("""
        INSERT INTO settings(key,value)
        VALUES(?,?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """, (
            key,
            str(value)
        ))

        con.commit()


# ============================================================
# DIGITS
# ============================================================

def normalize_digits(text):

    if not text:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )

    return text.translate(table)


# ============================================================
# NAME
# ============================================================

def name_of(user):

    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


# ============================================================
# KEYBOARDS
# ============================================================

def user_keyboard():

    return ReplyKeyboardMarkup(
        [
            ["💰 موجودی", "🎮 بازی"],
            ["👥 بازی با دوستان", "🤖 بازی با ربات"],
            ["💸 انتقال", "📤 درخواست"],
            ["📖 راهنما"]
        ],
        resize_keyboard=True
    )


def game_buttons(game_id):

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friend:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot:{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel:{game_id}"
            )
        ]
    ])


def admin_keyboard():

    withdraw_state = get_setting(
        "withdraw_enabled",
        "1"
    )

    withdraw_text = (
        "🔴 خاموش کردن برداشت"
        if withdraw_state == "1"
        else
        "🟢 روشن کردن برداشت"
    )

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 کاربران",
                callback_data="admin_users"
            ),
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ افزایش موجودی",
                callback_data="admin_add"
            ),
            InlineKeyboardButton(
                "➖ کاهش موجودی",
                callback_data="admin_remove"
            )
        ],
        [
            InlineKeyboardButton(
                "📋 درخواست‌ها",
                callback_data="admin_requests"
            )
        ],
        [
            InlineKeyboardButton(
                withdraw_text,
                callback_data="admin_withdraw_toggle"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if is_blocked(user.id):

        await update.message.reply_text(
            "⛔ دسترسی شما مسدود شده است."
        )

        return

    if update.effective_chat.type != ChatType.PRIVATE:

        await update.message.reply_text(
            "👋 برای استفاده از منوی کاربری، "
            "ربات را در خصوصی باز کن."
        )

        return

    await update.message.reply_text(
        "👋 سلام!\n\n"
        "به ربات خوش آمدی.",
        reply_markup=user_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(update, context):

    user = update.effective_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"💎 {format_amount(get_balance(user.id))} TRX"
    )


# ============================================================
# GAME PARSER
# ============================================================

GAME_NAMES = {
    "تاس": "dice",
    "dice": "dice",

    "بولینگ": "bowling",
    "بولينگ": "bowling",
    "bowling": "bowling",

    "بسکتبال": "basketball",
    "بسكتبال": "basketball",
    "basketball": "basketball",

    "دارت": "darts",
    "darts": "darts",
}


GAME_EMOJI = {
    "dice": "🎲",
    "bowling": "🎳",
    "basketball": "🏀",
    "darts": "🎯"
}


GAME_TITLE = {
    "dice": "🎲 تاس",
    "bowling": "🎳 بولینگ",
    "basketball": "🏀 بسکتبال",
    "darts": "🎯 دارت"
}


def parse_game(text):

    text = normalize_digits(
        text or ""
    ).strip()

    # تعداد بازی/رول هر عددی باشد
    m = re.match(
        r"^(\d+)\s+([^\s]+)\s+([0-9]+(?:[.,][0-9]+)?)$",
        text,
        re.IGNORECASE
    )

    if not m:
        return None

    try:
        rolls = int(m.group(1))
    except Exception:
        return None

    if rolls <= 0:
        return None

    game_name = m.group(2).lower()

    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    amount = parse_amount(
        m.group(3)
    )

    if amount is None:
        return None

    return game, rolls, amount


# ============================================================
# GAME DATABASE
# ============================================================

def create_game(
    chat_id,
    message_id,
    creator_id,
    game_type,
    rolls,
    amount
):

    with closing(db()) as con:

        cur = con.execute("""
        INSERT INTO games
        (
            chat_id,
            message_id,
            creator_id,
            game_type,
            rolls,
            amount,
            status
        )
        VALUES (?, ?, ?, ?, ?, ?, 'waiting')
        """, (
            chat_id,
            message_id,
            creator_id,
            game_type,
            rolls,
            amount
        ))

        con.commit()

        return cur.lastrowid


def get_game(game_id):

    with closing(db()) as con:

        return con.execute(
            "SELECT * FROM games WHERE id=?",
            (game_id,)
        ).fetchone()


def update_game_message(game_id, message_id):

    with closing(db()) as con:

        con.execute("""
        UPDATE games
        SET message_id=?
        WHERE id=?
        """, (
            message_id,
            game_id
        ))

        con.commit()


def set_game_status(game_id, status):

    with closing(db()) as con:

        con.execute("""
        UPDATE games
        SET status=?
        WHERE id=?
        """, (
            status,
            game_id
        ))

        con.commit()


def join_friend_game(game_id, user_id):

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT * FROM games WHERE id=?",
                (game_id,)
            ).fetchone()

            if not row:
                con.rollback()
                return False, "not_found"

            if row["status"] != "waiting":
                con.rollback()
                return False, "closed"

            if int(row["creator_id"]) == int(user_id):
                con.rollback()
                return False, "self"

            creator_id = int(
                row["creator_id"]
            )

            amount = int(
                row["amount"]
            )

            creator = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (creator_id,)
            ).fetchone()

            opponent = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user_id,)
            ).fetchone()

            if not creator or not opponent:
                con.rollback()
                return False, "user"

            if int(creator["balance"]) < amount:
                con.rollback()
                return False, "creator_balance"

            if int(opponent["balance"]) < amount:
                con.rollback()
                return False, "opponent_balance"

            # قفل مبلغ سازنده در زمان ساخت بازی
            # و قفل مبلغ حریف هنگام ورود
            con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            """, (
                amount,
                user_id
            ))

            con.execute("""
            UPDATE games
            SET opponent_id=?,
                status='friend_creator_turn'
            WHERE id=?
              AND status='waiting'
            """, (
                user_id,
                game_id
            ))

            # مبلغ سازنده قبلاً رزرو نشده؛
            # اینجا همزمان از او کسر می‌شود
            con.execute("""
            UPDATE users
            SET balance=balance-?
            WHERE user_id=?
            """, (
                amount,
                creator_id
            ))

            con.commit()

            return True, "ok"

        except Exception:

            con.rollback()
            logger.exception("JOIN GAME ERROR")

            return False, "error"


def cancel_game_and_refund(game_id):

    with closing(db()) as con:

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT * FROM games WHERE id=?",
                (game_id,)
            ).fetchone()

            if not row:
                con.rollback()
                return False

            if row["status"] not in (
                "waiting",
                "friend_creator_turn",
                "friend_opponent_turn"
            ):
                con.rollback()
                return False

            amount = int(row["amount"])

            # اگر حریف وارد شده، هر دو نفر برمی‌گردند
            if row["status"] in (
                "friend_creator_turn",
                "friend_opponent_turn"
            ):

                con.execute("""
                UPDATE users
                SET balance=balance+?
                WHERE user_id=?
                """, (
                    amount,
                    row["creator_id"]
                ))

                if row["opponent_id"]:

                    con.execute("""
                    UPDATE users
                    SET balance=balance+?
                    WHERE user_id=?
                    """, (
                        amount,
                        row["opponent_id"]
                    ))

            # در waiting هنوز مبلغ سازنده کسر نشده
            con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            """, (
                game_id
            ))

            con.commit()

            return True

        except Exception:

            con.rollback()
            logger.exception("CANCEL GAME ERROR")

            return False


# ============================================================
# START GAME MESSAGE
# ============================================================

async def create_game_message(
    update,
    game,
    rolls,
    amount
):

    message = update.message
    user = update.effective_user

    # موجودی لازم
    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    text = (
        f"{GAME_TITLE[game]}\n\n"
        f"🎯 تعداد: {rolls}\n"
        f"💰 شرط: {format_amount(amount)} TRX\n\n"
        f"👤 سازنده: {name_of(user)}\n\n"
        f"یکی از گزینه‌ها را انتخاب کنید:"
    )

    sent = await message.reply_text(
        text,
        reply_markup=game_buttons(0)
    )

    game_id = create_game(
        message.chat_id,
        sent.message_id,
        user.id,
        game,
        rolls,
        amount
    )

    # دکمه‌های واقعی
    await sent.edit_reply_markup(
        reply_markup=game_buttons(game_id)
    )


# ============================================================
# GAME BUTTON CALLBACK
# ============================================================

async def game_button_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    data = query.data or ""

    parts = data.split(":")

    if len(parts) != 2:
        return

    action = parts[0]

    try:
        game_id = int(parts[1])
    except Exception:
        return

    game = get_game(game_id)

    if not game:

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "❌ این بازی دیگر وجود ندارد."
        )

        return

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    if action == "cancel":

        if int(game["creator_id"]) != user.id:

            await query.answer(
                "فقط سازنده می‌تواند لغو کند.",
                show_alert=True
            )

            return

        if game["status"] not in (
            "waiting",
            "friend_creator_turn",
            "friend_opponent_turn"
        ):

            await query.answer(
                "این بازی قبلاً شروع یا تمام شده است.",
                show_alert=True
            )

            return

        cancel_game_and_refund(
            game_id
        )

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            "❌ بازی لغو شد."
        )

        return

    # --------------------------------------------------------
    # FRIEND
    # --------------------------------------------------------

    if action == "friend":

        if game["status"] != "waiting":

            await query.answer(
                "این بازی قبلاً شروع شده است.",
                show_alert=True
            )

            return

        if int(game["creator_id"]) == user.id:

            await query.answer(
                "سازنده نمی‌تواند حریف خودش باشد.",
                show_alert=True
            )

            return

        if is_blocked(user.id):

            await query.answer(
                "دسترسی شما مسدود است.",
                show_alert=True
            )

            return

        ok, reason = join_friend_game(
            game_id,
            user.id
        )

        if not ok:

            messages = {
                "closed":
                    "❌ بازی پیدا نشد یا قبلاً وارد شده‌اند.",
                "self":
                    "❌ نمی‌توانی با خودت بازی کنی.",
                "creator_balance":
                    "❌ موجودی سازنده کافی نیست.",
                "opponent_balance":
                    "❌ موجودی شما کافی نیست.",
                "not_found":
                    "❌ بازی پیدا نشد.",
                "error":
                    "❌ خطا در ورود به بازی."
            }

            await query.answer(
                messages.get(
                    reason,
                    "❌ ورود انجام نشد."
                ),
                show_alert=True
            )

            return

        # حذف دکمه‌ها
        await query.edit_message_reply_markup(
            reply_markup=None
        )

        creator = get_user(
            int(game["creator_id"])
        )

        await query.message.reply_text(
            f"👥 بازی شروع شد!\n\n"
            f"👤 سازنده: "
            f"{creator['first_name'] or creator['username'] or creator['user_id']}\n"
            f"👤 حریف: {name_of(user)}\n\n"
            f"🎯 سازنده اول خودش "
            f"{game['rolls']} بار {GAME_EMOJI[game['game_type']]} "
            f"بفرستد."
        )

        return

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    if action == "bot":

        if int(game["creator_id"]) != user.id:

            await query.answer(
                "فقط سازنده می‌تواند با ربات بازی کند.",
                show_alert=True
            )

            return

        if game["status"] != "waiting":

            await query.answer(
                "این بازی قبلاً شروع شده است.",
                show_alert=True
            )

            return

        # کسر شرط کاربر
        if not change_balance(
            user.id,
            -int(game["amount"])
        ):

            await query.answer(
                "❌ موجودی کافی نیست.",
                show_alert=True
            )

            return

        set_game_status(
            game_id,
            "bot_user_turn"
        )

        await query.edit_message_reply_markup(
            reply_markup=None
        )

        await query.message.reply_text(
            f"🤖 بازی با ربات شروع شد!\n\n"
            f"🎯 تعداد: {game['rolls']}\n"
            f"💰 شرط: "
            f"{format_amount(game['amount'])} TRX\n\n"
            f"اول خودت {game['rolls']} بار "
            f"{GAME_EMOJI[game['game_type']]} بفرست."
        )

        return


# ============================================================
# USER DICE / BOWLING / BASKETBALL / DART
# ============================================================

def get_dice_from_message(message):

    if not message:
        return None

    dice = getattr(
        message,
        "dice",
        None
    )

    if not dice:
        return None

    return dice


async def process_user_roll(
    update,
    context
):

    message = update.message

    user = update.effective_user

    dice = get_dice_from_message(
        message
    )

    if not dice:
        return False

    game_type = None

    emoji_to_game = {
        "🎲": "dice",
        "🎳": "bowling",
        "🏀": "basketball",
        "🎯": "darts"
    }

    game_type = emoji_to_game.get(
        dice.emoji
    )

    if not game_type:
        return False

    # جدیدترین بازی فعال کاربر
    with closing(db()) as con:

        game = con.execute("""
        SELECT *
        FROM games
        WHERE (
            creator_id=?
            OR opponent_id=?
        )
        AND status IN (
            'bot_user_turn',
            'friend_creator_turn',
            'friend_opponent_turn'
        )
        AND game_type=?
        ORDER BY id DESC
        LIMIT 1
        """, (
            user.id,
            user.id,
            game_type
        )).fetchone()

    if not game:
        return False

    game_id = int(game["id"])

    # --------------------------------------------------------
    # BOT USER TURN
    # --------------------------------------------------------

    if game["status"] == "bot_user_turn":

        if int(game["creator_id"]) != user.id:
            return False

        rolls = []

        if game["creator_rolls"]:
            try:
                rolls = [
                    int(x)
                    for x in game["creator_rolls"].split(",")
                    if x.strip()
                ]
            except Exception:
                rolls = []

        rolls.append(
            int(dice.value)
        )

        if len(rolls) < int(game["rolls"]):

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET creator_rolls=?,
                    creator_total=?
                WHERE id=?
                """, (
                    ",".join(
                        map(str, rolls)
                    ),
                    sum(rolls),
                    game_id
                ))

                con.commit()

            await message.reply_text(
                f"✅ {len(rolls)}/{game['rolls']}\n"
                f"🎯 نتیجه این پرتاب: {dice.value}\n\n"
                f"هنوز {game['rolls'] - len(rolls)} "
                f"پرتاب باقی مانده."
            )

            return True

        # تمام شد
        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                creator_total=?,
                status='bot_finished'
            WHERE id=?
            """, (
                ",".join(map(str, rolls)),
                sum(rolls),
                game_id
            ))

            con.commit()

        # ربات رول می‌کند
        bot_values = []

        for _ in range(int(game["rolls"])):

            sent = await context.bot.send_dice(
                chat_id=message.chat_id,
                emoji=game["game_type"] and GAME_EMOJI[game["game_type"]]
            )

            bot_values.append(
                int(sent.dice.value)
            )

            await asyncio.sleep(
                0.8
            )

        bot_total = sum(bot_values)
        user_total = sum(rolls)

        await settle_bot_game(
            context,
            game_id,
            user.id,
            user_total,
            bot_total,
            rolls,
            bot_values
        )

        return True

    # --------------------------------------------------------
    # FRIEND CREATOR TURN
    # --------------------------------------------------------

    if game["status"] == "friend_creator_turn":

        if int(game["creator_id"]) != user.id:

            return False

        rolls = []

        if game["creator_rolls"]:

            try:
                rolls = [
                    int(x)
                    for x in game["creator_rolls"].split(",")
                    if x.strip()
                ]
            except Exception:
                rolls = []

        rolls.append(
            int(dice.value)
        )

        if len(rolls) < int(game["rolls"]):

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET creator_rolls=?,
                    creator_total=?
                WHERE id=?
                """, (
                    ",".join(
                        map(str, rolls)
                    ),
                    sum(rolls),
                    game_id
                ))

                con.commit()

            await message.reply_text(
                f"✅ پرتاب {len(rolls)}/{game['rolls']}\n"
                f"🎯 نتیجه: {dice.value}"
            )

            return True

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                creator_total=?,
                status='friend_opponent_turn'
            WHERE id=?
            """, (
                ",".join(map(str, rolls)),
                sum(rolls),
                game_id
            ))

            con.commit()

        await message.reply_text(
            f"✅ سازنده هر {game['rolls']} پرتاب را انجام داد.\n\n"
            f"حالا حریف خودش "
            f"{game['rolls']} بار "
            f"{GAME_EMOJI[game['game_type']]} بفرستد."
        )

        return True

    # --------------------------------------------------------
    # FRIEND OPPONENT TURN
    # --------------------------------------------------------

    if game["status"] == "friend_opponent_turn":

        if int(game["opponent_id"]) != user.id:

            return False

        rolls = []

        if game["opponent_rolls"]:

            try:
                rolls = [
                    int(x)
                    for x in game["opponent_rolls"].split(",")
                    if x.strip()
                ]
            except Exception:
                rolls = []

        rolls.append(
            int(dice.value)
        )

        if len(rolls) < int(game["rolls"]):

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET opponent_rolls=?,
                    opponent_total=?
                WHERE id=?
                """, (
                    ",".join(
                        map(str, rolls)
                    ),
                    sum(rolls),
                    game_id
                ))

                con.commit()

            await message.reply_text(
                f"✅ پرتاب {len(rolls)}/{game['rolls']}\n"
                f"🎯 نتیجه: {dice.value}"
            )

            return True

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET opponent_rolls=?,
                opponent_total=?,
                status='finished'
            WHERE id=?
            """, (
                ",".join(map(str, rolls)),
                sum(rolls),
                game_id
            ))

            con.commit()

        creator_total = int(
            game["creator_total"]
        )

        opponent_total = sum(rolls)

        await settle_friend_game(
            context,
            game_id,
            creator_total,
            opponent_total
        )

        return True

    return False


# ============================================================
# BOT SETTLE
# ============================================================

async def settle_bot_game(
    context,
    game_id,
    user_id,
    user_total,
    bot_total,
    user_rolls,
    bot_rolls
):

    game = get_game(game_id)

    if not game:
        return

    amount = int(
        game["amount"]
    )

    creator_id = int(
        game["creator_id"]
    )

    # کل مبلغ دو طرف
    pot = amount * 2

    # درصدها
    winner_reward = int(
        Decimal(pot) *
        WINNER_PERCENT
    )

    owner_reward = int(
        Decimal(pot) *
        OWNER_PERCENT
    )

    fee = pot - winner_reward - owner_reward

    # مساوی:
    # هر دو شرطشان را پس می‌گیرند
    if user_total == bot_total:

        change_balance(
            user_id,
            amount
        )

        set_game_status(
            game_id,
            "draw"
        )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🤝 مساوی شد!\n\n"
                f"👤 {name_of(await context.bot.get_chat(user_id))}: "
                f"{user_total}\n"
                f"🤖 ربات: {bot_total}\n\n"
                f"💰 مبلغ {format_amount(amount)} TRX "
                f"به کاربر برگشت داده شد."
            )
        )

        return

    if user_total > bot_total:

        winner_id = user_id

        # مالک ربات
        owner_id = next(iter(ADMIN_IDS))

        change_balance(
            winner_id,
            winner_reward
        )

        change_balance(
            owner_id,
            owner_reward
        )

        set_game_status(
            game_id,
            "finished"
        )

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET winner_id=?
            WHERE id=?
            """, (
                winner_id,
                game_id
            ))

            con.commit()

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🏆 نتیجه بازی\n\n"
                f"👤 کاربر: {user_total}\n"
                f"🤖 ربات: {bot_total}\n\n"
                f"🏆 برنده: کاربر\n"
                f"💰 دریافتی برنده: "
                f"{format_amount(winner_reward)} TRX"
            )
        )

    else:

        owner_id = next(iter(ADMIN_IDS))

        change_balance(
            owner_id,
            amount
        )

        # 95% از کل pot به ربات/مالک برنمی‌گردد؛
        # در این حالت سهم مالک و کارمزد جدا محاسبه می‌شود.
        # مبلغ باقی‌مانده برای جلوگیری از ایجاد اعتبار اضافه
        # در حساب سیستم ثبت نمی‌شود.

        set_game_status(
            game_id,
            "finished"
        )

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET winner_id=NULL
            WHERE id=?
            """, (
                game_id
            ))

            con.commit()

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🏆 نتیجه بازی\n\n"
                f"👤 کاربر: {user_total}\n"
                f"🤖 ربات: {bot_total}\n\n"
                f"🏆 برنده: ربات\n"
                f"💰 سهم مالک: "
                f"{format_amount(amount)} TRX"
            )
        )


# ============================================================
# FRIEND SETTLE
# ============================================================

async def settle_friend_game(
    context,
    game_id,
    creator_total,
    opponent_total
):

    game = get_game(game_id)

    if not game:
        return

    amount = int(
        game["amount"]
    )

    pot = amount * 2

    winner_reward = int(
        Decimal(pot) *
        WINNER_PERCENT
    )

    owner_reward = int(
        Decimal(pot) *
        OWNER_PERCENT
    )

    fee = pot - winner_reward - owner_reward

    creator_id = int(
        game["creator_id"]
    )

    opponent_id = int(
        game["opponent_id"]
    )

    # مساوی
    if creator_total == opponent_total:

        change_balance(
            creator_id,
            amount
        )

        change_balance(
            opponent_id,
            amount
        )

        set_game_status(
            game_id,
            "draw"
        )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                "🤝 مساوی شد!\n\n"
                f"👤 سازنده: {creator_total}\n"
                f"👤 حریف: {opponent_total}\n\n"
                f"💰 شرط هر دو نفر برگشت داده شد."
            )
        )

        return

    if creator_total > opponent_total:

        winner_id = creator_id
        winner_name = "سازنده"

    else:

        winner_id = opponent_id
        winner_name = "حریف"

    owner_id = next(iter(ADMIN_IDS))

    change_balance(
        winner_id,
        winner_reward
    )

    change_balance(
        owner_id,
        owner_reward
    )

    with closing(db()) as con:

        con.execute("""
        UPDATE games
        SET status='finished',
            winner_id=?
        WHERE id=?
        """, (
            winner_id,
            game_id
        ))

        con.commit()

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            f"🏆 نتیجه بازی\n\n"
            f"👤 سازنده: {creator_total}\n"
            f"👤 حریف: {opponent_total}\n\n"
            f"🏆 برنده: {winner_name}\n"
            f"💰 دریافتی برنده: "
            f"{format_amount(winner_reward)} TRX"
        )
    )


# ============================================================
# GAME MENU
# ============================================================

async def game_menu(update, context):

    await update.message.reply_text(
        "🎮 مثال بازی:\n\n"
        "🎲 1 تاس 0.5\n"
        "🎲 2 تاس 0.1\n"
        "🎳 1 بولینگ 0.5\n"
        "🏀 1 بسکتبال 0.5\n"
        "🎯 1 دارت 0.5\n\n"
        "تعداد بازی/پرتاب محدود نیست."
    )


# ============================================================
# FRIENDS MENU
# ============================================================

async def friends(update, context):

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):

        await update.message.reply_text(
            "👥 این بخش در گپ استفاده می‌شود."
        )

        return

    await update.message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.1\n"
        "5 بولینگ 0.5\n\n"
        "بعد از ساخت بازی، هر کاربری می‌تواند "
        "روی «بازی با دوستان» بزند و وارد شود."
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    message = update.message
    user = update.effective_user

    if not message.reply_to_message:

        await message.reply_text(
            "💸 روی پیام کاربر Reply کن و بنویس:\n\n"
            "انتقال 0.5"
        )

        return

    target = message.reply_to_message.from_user

    if not target:
        return

    if target.id == user.id:

        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )

        return

    if target.is_bot:

        await message.reply_text(
            "❌ انتقال به ربات مجاز نیست."
        )

        return

    amount = parse_amount(
        message.text
    )

    if amount is None:

        await message.reply_text(
            "❌ مقدار نامعتبر."
        )

        return

    ensure_user(target)

    if transfer_balance(
        user.id,
        target.id,
        amount
    ):

        await message.reply_text(
            f"✅ انتقال انجام شد.\n\n"
            f"👤 مقصد: {name_of(target)}\n"
            f"💰 مقدار: {format_amount(amount)} TRX"
        )

    else:

        await message.reply_text(
            "❌ موجودی کافی نیست یا انتقال انجام نشد."
        )


# ============================================================
# REQUEST
# ============================================================

async def request_menu(update, context):

    if get_setting(
        "withdraw_enabled",
        "1"
    ) != "1":

        await update.message.reply_text(
            "⛔ برداشت فعلاً خاموش است."
        )

        return

    context.user_data["request_mode"] = True

    await update.message.reply_text(
        "📤 درخواست برداشت\n\n"
        "مثال:\n"
        "درخواست 10\n\n"
        "بعد از آن اطلاعات کیف پول را بفرست."
    )


async def create_request(
    user_id,
    amount,
    wallet
):

    with closing(db()) as con:

        con.execute("""
        INSERT INTO requests
        (
            user_id,
            amount,
            wallet,
            status
        )
        VALUES (?, ?, ?, 'pending')
        """, (
            user_id,
            amount,
            wallet
        ))

        con.commit()


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    await update.message.reply_text(
        "📖 راهنما\n\n"
        "🎮 مثال:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.1\n"
        "1 بولینگ 0.5\n"
        "1 بسکتبال 0.5\n"
        "1 دارت 0.5\n\n"
        "💰 موجودی\n"
        "💸 انتقال 0.5 ← با Reply\n"
        "👥 بازی با دوستان\n"
        "🤖 بازی با ربات\n"
        "📤 درخواست\n\n"
        "تعداد پرتاب‌ها محدود نیست."
    )


# ============================================================
# ADMIN
# ============================================================

async def admin(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):

        await update.message.reply_text(
            "⛔ دسترسی ندارید."
        )

        return

    await update.message.reply_text(
        "👑 پنل مدیریت",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    if not is_admin(user.id):

        await query.edit_message_text(
            "⛔ دسترسی ندارید."
        )

        return

    data = query.data

    # USERS
    if data == "admin_users":

        with closing(db()) as con:

            rows = con.execute("""
            SELECT
                user_id,
                first_name,
                username,
                balance,
                blocked
            FROM users
            ORDER BY balance DESC
            LIMIT 50
            """).fetchall()

        if not rows:

            await query.edit_message_text(
                "👥 کاربری وجود ندارد."
            )

            return

        text = "👥 کاربران\n\n"

        for i, row in enumerate(rows, 1):

            name = (
                row["first_name"]
                or row["username"]
                or str(row["user_id"])
            )

            status = (
                "🚫"
                if row["blocked"]
                else
                "✅"
            )

            text += (
                f"{i}. {status} {name}\n"
                f"ID: {row['user_id']}\n"
                f"💰 {format_amount(row['balance'])} TRX\n\n"
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 پنل",
                        callback_data="admin_back"
                    )
                ]
            ])
        )

        return

    # STATS
    if data == "admin_stats":

        with closing(db()) as con:

            users = con.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            total = con.execute(
                "SELECT COALESCE(SUM(balance),0) FROM users"
            ).fetchone()[0]

            pending = con.execute("""
            SELECT COUNT(*)
            FROM requests
            WHERE status='pending'
            """).fetchone()[0]

            active_games = con.execute("""
            SELECT COUNT(*)
            FROM games
            WHERE status IN (
                'waiting',
                'bot_user_turn',
                'friend_creator_turn',
                'friend_opponent_turn'
            )
            """).fetchone()[0]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"💰 مجموع موجودی: "
            f"{format_amount(total)} TRX\n"
            f"📤 درخواست‌های در انتظار: {pending:,}\n"
            f"🎮 بازی‌های فعال: {active_games:,}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 پنل",
                        callback_data="admin_back"
                    )
                ]
            ])
        )

        return

    # ADD
    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "/addbalance USER_ID AMOUNT",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 پنل",
                        callback_data="admin_back"
                    )
                ]
            ])
        )

        return

    # REMOVE
    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "/removebalance USER_ID AMOUNT",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 پنل",
                        callback_data="admin_back"
                    )
                ]
            ])
        )

        return

    # REQUESTS
    if data == "admin_requests":

        with closing(db()) as con:

            rows = con.execute("""
            SELECT *
            FROM requests
            WHERE status='pending'
            ORDER BY id DESC
            LIMIT 30
            """).fetchall()

        if not rows:

            await query.edit_message_text(
                "📋 درخواست در انتظار وجود ندارد.",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "🔙 پنل",
                            callback_data="admin_back"
                        )
                    ]
                ])
            )

            return

        text = "📋 درخواست‌ها\n\n"

        for row in rows:

            text += (
                f"#{row['id']}\n"
                f"👤 {row['user_id']}\n"
                f"💰 {format_amount(row['amount'])} TRX\n"
                f"📝 {row['wallet']}\n"
                f"📅 {row['created_at']}\n\n"
            )

        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 پنل",
                        callback_data="admin_back"
                    )
                ]
            ])
        )

        return

    # WITHDRAW TOGGLE
    if data == "admin_withdraw_toggle":

        current = get_setting(
            "withdraw_enabled",
            "1"
        )

        new_value = (
            "0"
            if current == "1"
            else
            "1"
        )

        set_setting(
            "withdraw_enabled",
            new_value
        )

        state = (
            "🟢 برداشت روشن شد."
            if new_value == "1"
            else
            "🔴 برداشت خاموش شد."
        )

        await query.edit_message_text(
            f"👑 پنل مدیریت\n\n{state}",
            reply_markup=admin_keyboard()
        )

        return

    # BACK
    if data == "admin_back":

        await query.edit_message_text(
            "👑 پنل مدیریت",
            reply_markup=admin_keyboard()
        )

        return


# ============================================================
# ADMIN ADD
# ============================================================

async def add_balance(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "/addbalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )

    except ValueError:

        await update.message.reply_text(
            "❌ ID نامعتبر."
        )

        return

    amount = parse_amount(
        context.args[1]
    )

    if amount is None:

        await update.message.reply_text(
            "❌ مبلغ نامعتبر."
        )

        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    if not change_balance(
        target_id,
        amount
    ):

        await update.message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    await update.message.reply_text(
        f"✅ موجودی افزایش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➕ {format_amount(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{format_amount(get_balance(target_id))} TRX"
    )


# ============================================================
# ADMIN REMOVE
# ============================================================

async def remove_balance(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 2:

        await update.message.reply_text(
            "/removebalance USER_ID AMOUNT"
        )

        return

    try:

        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )

    except ValueError:

        await update.message.reply_text(
            "❌ ID نامعتبر."
        )

        return

    amount = parse_amount(
        context.args[1]
    )

    if amount is None:

        await update.message.reply_text(
            "❌ مبلغ نامعتبر."
        )

        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    if not change_balance(
        target_id,
        -amount
    ):

        await update.message.reply_text(
            "❌ موجودی کاربر کافی نیست."
        )

        return

    await update.message.reply_text(
        f"✅ موجودی کاهش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➖ {format_amount(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{format_amount(get_balance(target_id))} TRX"
    )


# ============================================================
# BLOCK
# ============================================================

async def block(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 1:
        return

    try:

        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )

    except ValueError:
        return

    with closing(db()) as con:

        con.execute("""
        UPDATE users
        SET blocked=1
        WHERE user_id=?
        """, (
            target_id
        ))

        con.commit()

    await update.message.reply_text(
        f"🚫 کاربر {target_id} مسدود شد."
    )


async def unblock(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    if len(context.args) != 1:
        return

    try:

        target_id = int(
            normalize_digits(
                context.args[0]
            )
        )

    except ValueError:
        return

    with closing(db()) as con:

        con.execute("""
        UPDATE users
        SET blocked=0
        WHERE user_id=?
        """, (
            target_id
        ))

        con.commit()

    await update.message.reply_text(
        f"✅ کاربر {target_id} رفع مسدودی شد."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    message = update.message

    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    text = (
        message.text or ""
    ).strip()

    normalized = normalize_digits(
        text
    )

    # --------------------------------------------------------
    # REQUEST MODE
    # --------------------------------------------------------

    if context.user_data.get(
        "request_mode"
    ) == "wallet":

        amount = context.user_data.get(
            "request_amount"
        )

        if amount:

            await create_request(
                user.id,
                int(amount),
                text
            )

            context.user_data.clear()

            await message.reply_text(
                "✅ درخواست ثبت شد."
            )

            return

    # --------------------------------------------------------
    # REQUEST COMMAND
    # --------------------------------------------------------

    request_match = re.match(
        r"^(درخواست|request)\s+"
        r"([0-9۰-۹٠-٩]+(?:[.,][0-9]+)?)$",
        normalized,
        re.IGNORECASE
    )

    if request_match:

        if get_setting(
            "withdraw_enabled",
            "1"
        ) != "1":

            await message.reply_text(
                "⛔ برداشت فعلاً خاموش است."
            )

            return

        amount = parse_amount(
            request_match.group(2)
        )

        if amount is None:
            return

        if get_balance(user.id) < amount:

            await message.reply_text(
                "❌ موجودی کافی نیست."
            )

            return

        context.user_data[
            "request_amount"
        ] = amount

        context.user_data[
            "request_mode"
        ] = "wallet"

        await message.reply_text(
            "📝 مبلغ ثبت شد.\n\n"
            "حالا اطلاعات کیف پول را بفرست."
        )

        return

    # --------------------------------------------------------
    # TRANSFER
    # --------------------------------------------------------

    if re.match(
        r"^(انتقال|transfer)\s+"
        r"[0-9۰-۹٠-٩]+(?:[.,][0-9]+)?$",
        normalized,
        re.IGNORECASE
    ):

        await transfer(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # GAME
    # --------------------------------------------------------

    parsed = parse_game(
        normalized
    )

    if parsed:

        game, rolls, amount = parsed

        # ساخت بازی در گپ
        if update.effective_chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await create_game_message(
                update,
                game,
                rolls,
                amount
            )

        else:

            await message.reply_text(
                "🎮 بازی را در گپ ایجاد کن."
            )

        return

    # --------------------------------------------------------
    # MENU
    # --------------------------------------------------------

    if text == "💰 موجودی":

        await balance(
            update,
            context
        )

        return

    if text == "🎮 بازی":

        await game_menu(
            update,
            context
        )

        return

    if text == "👥 بازی با دوستان":

        await friends(
            update,
            context
        )

        return

    if text == "🤖 بازی با ربات":

        await message.reply_text(
            "🤖 برای بازی با ربات، "
            "در گپ یک بازی بساز و دکمه "
            "«بازی با ربات» را بزن."
        )

        return

    if text == "💸 انتقال":

        await transfer(
            update,
            context
        )

        return

    if text == "📤 درخواست":

        await request_menu(
            update,
            context
        )

        return

    if text == "📖 راهنما":

        await help_command(
            update,
            context
        )

        return


# ============================================================
# DICE MESSAGE HANDLER
# ============================================================

async def dice_handler(update, context):

    try:

        handled = await process_user_roll(
            update,
            context
        )

        if handled:
            return

    except Exception:

        logger.exception(
            "DICE HANDLER ERROR"
        )


# ============================================================
# ERROR
# ============================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "BOT ERROR",
        exc_info=context.error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )

    init_db()

    application = (
        Application.builder()
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
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            balance
        )
    )

    application.add_handler(
        CommandHandler(
            "game",
            game_menu
        )
    )

    application.add_handler(
        CommandHandler(
            "friends",
            friends
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    application.add_handler(
        CommandHandler(
            "addbalance",
            add_balance
        )
    )

    application.add_handler(
        CommandHandler(
            "removebalance",
            remove_balance
        )
    )

    application.add_handler(
        CommandHandler(
            "block",
            block
        )
    )

    application.add_handler(
        CommandHandler(
            "unblock",
            unblock
        )
    )

    # --------------------------------------------------------
    # CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            game_button_callback,
            pattern=r"^(friend|bot|cancel):\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # --------------------------------------------------------
    # USER TELEGRAM DICE
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            dice_handler
        )
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "🚀 BOT STARTED"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
