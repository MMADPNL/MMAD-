# ============================================================
# BOT.PY - Telegram TRX Games
# Python 3.10+
# python-telegram-bot 20+
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
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    8552447077
}

DB_FILE = "bot.db"

MIN_GAME = Decimal("0.01")
MAX_GAME = Decimal("1000000000")

# سهم مالک و کارمزد از کل مبلغ دو بازیکن
OWNER_SHARE = Decimal("0.02")
BOT_FEE = Decimal("0.03")
WINNER_PAYOUT = Decimal("0.95")

# برداشت به صورت پیش‌فرض روشن
WITHDRAW_ENABLED_DEFAULT = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

DB_LOCK = asyncio.Lock()

# ============================================================
# GAME CONFIG
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
    "دارت": "darts",
    "darts": "darts",
}

GAME_LABELS = {
    "dice": "🎲 تاس",
    "bowling": "🎳 بولینگ",
    "basketball": "🏀 بسکتبال",
    "darts": "🎯 دارت",
}

GAME_EMOJIS = {
    "dice": "🎲",
    "bowling": "🎳",
    "basketball": "🏀",
    "darts": "🎯",
}

# Telegram Dice ranges
GAME_MAX = {
    "dice": 6,
    "bowling": 6,
    "basketball": 5,
    "darts": 6,
}

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
    return con


def init_db():
    with closing(db()) as con:

        con.execute("""
        PRAGMA journal_mode=WAL
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT DEFAULT '',
            first_name TEXT DEFAULT '',
            balance TEXT DEFAULT '0',
            blocked INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS transfers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sender_id INTEGER,
            receiver_id INTEGER,
            amount TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            amount TEXT,
            wallet TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            message_id INTEGER DEFAULT 0,
            creator_id INTEGER,
            opponent_id INTEGER,
            game_type TEXT,
            amount TEXT,
            rounds INTEGER,
            creator_rolls TEXT DEFAULT '',
            opponent_rolls TEXT DEFAULT '',
            status TEXT DEFAULT 'waiting',
            winner_id INTEGER DEFAULT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # خانه داخلی سیستم
        con.execute("""
        CREATE TABLE IF NOT EXISTS house (
            id INTEGER PRIMARY KEY CHECK(id=1),
            owner_balance TEXT DEFAULT '0',
            fee_balance TEXT DEFAULT '0'
        )
        """)

        con.execute("""
        INSERT OR IGNORE INTO house
        (id, owner_balance, fee_balance)
        VALUES (1, '0', '0')
        """)

        con.execute("""
        INSERT OR IGNORE INTO settings
        (key, value)
        VALUES ('withdraw_enabled', '1')
        """)

        # مهاجرت از نسخه‌های قبلی
        columns = [
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        ]

        if "balance" not in columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN balance TEXT DEFAULT '0'"
            )

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

            # بدون موجودی اولیه
            con.execute("""
            INSERT INTO users
            (user_id, username, first_name, balance)
            VALUES (?, ?, ?, '0')
            """, (
                user.id,
                user.username or "",
                user.first_name or ""
            ))

        con.commit()


def get_user(user_id):
    with closing(db()) as con:
        return con.execute(
            "SELECT * FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()


def D(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def money(value):
    value = D(value).quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )

    if value == value.to_integral():
        return f"{int(value):,}"

    return f"{value:,.2f}".rstrip("0").rstrip(".")


def get_balance(user_id):
    row = get_user(user_id)

    if not row:
        return Decimal("0")

    return D(row["balance"])


# ============================================================
# ATOMIC BALANCE
# ============================================================

def debit_balance(user_id, amount):
    """
    ضد دوباره‌کسر شدن موجودی.
    عملیات داخل transaction انجام می‌شود.
    """

    amount = D(amount)

    if amount <= 0:
        return False

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user_id,)
            ).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return False

            current = D(row["balance"])

            if current < amount:
                con.execute("ROLLBACK")
                return False

            new_balance = current - amount

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(new_balance),
                user_id
            ))

            con.execute("COMMIT")
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("DEBIT ERROR")
            return False


def credit_balance(user_id, amount):
    amount = D(amount)

    if amount <= 0:
        return False

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user_id,)
            ).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return False

            current = D(row["balance"])
            new_balance = current + amount

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(new_balance),
                user_id
            ))

            con.execute("COMMIT")
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("CREDIT ERROR")
            return False


def set_balance(user_id, amount):
    amount = max(Decimal("0"), D(amount))

    with closing(db()) as con:
        row = con.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (user_id,)
        ).fetchone()

        if not row:
            return False

        con.execute("""
        UPDATE users
        SET balance=?
        WHERE user_id=?
        """, (
            str(amount),
            user_id
        ))

        con.commit()

    return True


# ============================================================
# HOUSE
# ============================================================

def add_house(owner_amount, fee_amount):
    owner_amount = D(owner_amount)
    fee_amount = D(fee_amount)

    with closing(db()) as con:

        row = con.execute(
            "SELECT owner_balance, fee_balance FROM house WHERE id=1"
        ).fetchone()

        owner = D(row["owner_balance"])
        fee = D(row["fee_balance"])

        con.execute("""
        UPDATE house
        SET owner_balance=?,
            fee_balance=?
        WHERE id=1
        """, (
            str(owner + owner_amount),
            str(fee + fee_amount)
        ))

        con.commit()


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
        VALUES (?,?)
        ON CONFLICT(key)
        DO UPDATE SET value=excluded.value
        """, (
            key,
            str(value)
        ))

        con.commit()


def withdraw_enabled():
    return get_setting(
        "withdraw_enabled",
        str(WITHDRAW_ENABLED_DEFAULT)
    ) == "1"


# ============================================================
# CHECKS
# ============================================================

def is_blocked(user_id):
    row = get_user(user_id)

    return bool(
        row and
        int(row["blocked"]) == 1
    )


def is_admin(user_id):
    return user_id in ADMIN_IDS


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


def parse_decimal_amount(text):
    if not text:
        return None

    text = normalize_digits(text)

    text = text.replace(",", ".")
    text = text.replace("٬", "")
    text = text.strip()

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None

    if value < MIN_GAME:
        return None

    if value > MAX_GAME:
        return None

    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )


def parse_amount_from_command(text):
    text = normalize_digits(text or "")
    text = text.replace("٬", "")

    m = re.search(
        r"(-?\d+(?:[.,]\d+)?)",
        text
    )

    if not m:
        return None

    return parse_decimal_amount(
        m.group(1).replace(",", ".")
    )


def parse_game(text):
    """
    پشتیبانی:

    1 تاس 0.5
    2 تاس 0.1
    10 بولینگ 0.5
    100 بسکتبال 1
    2 دارت 0.25
    """

    text = normalize_digits(text or "").strip()

    pattern = re.compile(
        r"^(\d+)\s+([^\s]+)\s+(\d+(?:[.,]\d+)?)$",
        re.IGNORECASE
    )

    m = pattern.match(text)

    if not m:
        return None

    rounds = int(m.group(1))
    game_name = m.group(2).lower()

    amount_text = m.group(3).replace(",", ".")

    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    amount = parse_decimal_amount(amount_text)

    if amount is None:
        return None

    if rounds < 1:
        return None

    # تعداد محدود نیست
    return game, rounds, amount


def name_of(user):
    if not user:
        return "کاربر"

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


def game_keyboard():
    return InlineKeyboardMarkup([
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
                "🏀 بسکتبال",
                callback_data="game_basketball"
            ),
            InlineKeyboardButton(
                "🎯 دارت",
                callback_data="game_darts"
            )
        ]
    ])


def game_created_keyboard(game_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join_{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel_{game_id}"
            )
        ]
    ])


def admin_keyboard():
    withdraw_text = (
        "🟢 برداشت روشن"
        if withdraw_enabled()
        else
        "🔴 برداشت خاموش"
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
                withdraw_text,
                callback_data="admin_withdraw_toggle"
            )
        ],
        [
            InlineKeyboardButton(
                "📋 درخواست‌ها",
                callback_data="admin_requests"
            )
        ]
    ])


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    amount = get_balance(user.id)

    await update.effective_message.reply_text(
        f"💰 موجودی {name_of(user)}:\n\n"
        f"💎 {money(amount)} TRX"
    )


# ============================================================
# START
# ============================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if is_blocked(user.id):

        await update.effective_message.reply_text(
            "⛔ دسترسی شما مسدود است."
        )

        return

    await update.effective_message.reply_text(
        "👋 سلام!\n\n"
        "به ربات خوش آمدی.",
        reply_markup=user_keyboard()
    )


# ============================================================
# GAME MENU
# ============================================================

async def game_menu(update, context):

    await update.effective_message.reply_text(
        "🎮 بازی را انتخاب کن:",
        reply_markup=game_keyboard()
    )


async def friends_menu(update, context):

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):

        await update.effective_message.reply_text(
            "❌ بازی با دوستان فقط داخل گپ است."
        )

        return

    await update.effective_message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "10 بولینگ 0.5\n"
        "2 بسکتبال 0.5\n"
        "3 دارت 0.5\n\n"
        "تعداد بازی/پرتاب محدودیت ندارد."
    )


async def bot_menu(update, context):

    await update.effective_message.reply_text(
        "🤖 بازی با ربات\n\n"
        "مثال:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "1 بسکتبال 0.5\n"
        "1 دارت 0.5\n\n"
        "بعد از ساخت بازی، خودت ایموجی بازی را بفرست."
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game_message(
    update,
    context,
    game,
    rounds,
    amount
):

    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    ensure_user(user)

    if is_blocked(user.id):
        return

    if get_balance(user.id) < amount:

        await message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {money(get_balance(user.id))} TRX"
        )

        return

    # رزرو مبلغ سازنده
    if not debit_balance(user.id, amount):

        await message.reply_text(
            "❌ موجودی تغییر کرده است؛ دوباره تلاش کن."
        )

        return

    try:

        text = (
            f"{GAME_LABELS[game]}\n\n"
            f"🎮 تعداد: {rounds}\n"
            f"💰 مبلغ بازی: {money(amount)} TRX\n\n"
            f"👤 سازنده: {name_of(user)}\n\n"
            f"یکی از گزینه‌ها را انتخاب کن:"
        )

        sent = await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            reply_markup=game_created_keyboard(0)
        )

        with closing(db()) as con:

            cur = con.execute("""
            INSERT INTO games
            (chat_id, message_id, creator_id,
             game_type, amount, rounds, status)
            VALUES (?, ?, ?, ?, ?, ?, 'waiting')
            """, (
                chat.id,
                sent.message_id,
                user.id,
                game,
                str(amount),
                rounds
            ))

            game_id = cur.lastrowid

            con.commit()

        await context.bot.edit_message_reply_markup(
            chat_id=chat.id,
            message_id=sent.message_id,
            reply_markup=game_created_keyboard(game_id)
        )

    except Exception:

        credit_balance(user.id, amount)

        logger.exception("CREATE GAME ERROR")

        await message.reply_text(
            "❌ بازی ساخته نشد؛ مبلغ برگشت داده شد."
        )


# ============================================================
# DICE / GAME CALLBACK
# ============================================================

async def game_callback(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    game = query.data.replace(
        "game_",
        "",
        1
    )

    if game not in GAME_LABELS:
        return

    await query.message.reply_text(
        f"{GAME_LABELS[game]}\n\n"
        f"مثال:\n"
        f"1 {GAME_LABELS[game].split(' ', 1)[1]} 0.5\n\n"
        f"تعداد نامحدود است."
    )


# ============================================================
# LOAD GAME
# ============================================================

def get_game(game_id):
    with closing(db()) as con:
        return con.execute(
            "SELECT * FROM games WHERE id=?",
            (game_id,)
        ).fetchone()


def update_game_rolls(
    game_id,
    creator_rolls,
    opponent_rolls,
    status=None
):

    with closing(db()) as con:

        if status:

            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                opponent_rolls=?,
                status=?
            WHERE id=?
            """, (
                creator_rolls,
                opponent_rolls,
                status,
                game_id
            ))

        else:

            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                opponent_rolls=?
            WHERE id=?
            """, (
                creator_rolls,
                opponent_rolls,
                game_id
            ))

        con.commit()


def parse_rolls(value):
    if not value:
        return []

    result = []

    for x in value.split(","):
        try:
            result.append(int(x))
        except ValueError:
            pass

    return result


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_friend(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    try:
        game_id = int(query.data.split("_")[1])
    except Exception:
        return

    ensure_user(user)

    async with DB_LOCK:

        with closing(db()) as con:

            con.execute("BEGIN IMMEDIATE")

            game = con.execute(
                "SELECT * FROM games WHERE id=?",
                (game_id,)
            ).fetchone()

            if not game:
                con.execute("ROLLBACK")

                await query.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )

                return

            if game["status"] != "waiting":

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ این بازی دیگر قابل ورود نیست.",
                    show_alert=True
                )

                return

            creator_id = int(game["creator_id"])

            if creator_id == user.id:

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ خودت سازنده بازی هستی.",
                    show_alert=True
                )

                return

            amount = D(game["amount"])

            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user.id,)
            ).fetchone()

            if not row or D(row["balance"]) < amount:

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

                return

            # مبلغ حریف قفل می‌شود
            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(D(row["balance"]) - amount),
                user.id
            ))

            con.execute("""
            UPDATE games
            SET opponent_id=?,
                status='creator_turn'
            WHERE id=?
            """, (
                user.id,
                game_id
            ))

            con.commit()

    # حذف کامل دکمه‌ها
    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await query.message.reply_text(
        f"👥 حریف وارد شد: {name_of(user)}\n\n"
        f"🎯 اول سازنده بازی، {name_of((await context.bot.get_chat(creator_id)))} "
        f"باید {game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرستد."
    )


# ============================================================
# BOT GAME
# ============================================================

async def join_bot(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    try:
        game_id = int(query.data.split("_")[1])
    except Exception:
        return

    ensure_user(user)

    async with DB_LOCK:

        with closing(db()) as con:

            con.execute("BEGIN IMMEDIATE")

            game = con.execute(
                "SELECT * FROM games WHERE id=?",
                (game_id,)
            ).fetchone()

            if not game:

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )

                return

            if game["status"] != "waiting":

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ بازی دیگر قابل ورود نیست.",
                    show_alert=True
                )

                return

            if int(game["creator_id"]) != user.id:

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ فقط سازنده می‌تواند بازی با ربات را شروع کند.",
                    show_alert=True
                )

                return

            amount = D(game["amount"])

            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user.id,)
            ).fetchone()

            if not row or D(row["balance"]) < amount:

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

                return

            # مبلغ بازی قبلاً در حالت waiting از سازنده کم شده.
            # فقط وضعیت تغییر می‌کند.
            con.execute("""
            UPDATE games
            SET opponent_id=NULL,
                status='bot_creator_turn'
            WHERE id=?
            """, (
                game_id,
            ))

            con.commit()

    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await query.message.reply_text(
        f"🤖 بازی با ربات شروع شد.\n\n"
        f"👤 {name_of(user)}\n\n"
        f"اول خودت {game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرست."
    )


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    try:
        game_id = int(query.data.split("_")[1])
    except Exception:
        return

    async with DB_LOCK:

        with closing(db()) as con:

            con.execute("BEGIN IMMEDIATE")

            game = con.execute(
                "SELECT * FROM games WHERE id=?",
                (game_id,)
            ).fetchone()

            if not game:

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )

                return

            if game["status"] not in (
                "waiting",
            ):

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ بازی شروع شده و قابل لغو نیست.",
                    show_alert=True
                )

                return

            if int(game["creator_id"]) != user.id:

                con.execute("ROLLBACK")

                await query.answer(
                    "❌ فقط سازنده می‌تواند لغو کند.",
                    show_alert=True
                )

                return

            amount = D(game["amount"])

            row = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (user.id,)
            ).fetchone()

            if row:

                new_balance = D(row["balance"]) + amount

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(new_balance),
                    user.id
                ))

            con.execute("""
            UPDATE games
            SET status='cancelled'
            WHERE id=?
            """, (
                game_id,
            ))

            con.commit()

    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await query.message.reply_text(
        f"❌ بازی لغو شد.\n"
        f"💰 {money(game['amount'])} TRX برگشت داده شد."
    )


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def game_action_callback(update, context):

    query = update.callback_query

    data = query.data

    if data.startswith("join_"):
        await join_friend(update, context)
        return

    if data.startswith("bot_"):
        await join_bot(update, context)
        return

    if data.startswith("cancel_"):
        await cancel_game(update, context)
        return


# ============================================================
# PROCESS USER DICE
# ============================================================

def is_game_dice_correct(game_type, dice):
    if not dice:
        return False

    return dice.emoji == GAME_EMOJIS[game_type]


async def process_user_roll(update, context):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    dice = message.dice

    if not dice:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    game = None

    # ========================================================
    # پیدا کردن بازی فعال کاربر
    # ========================================================

    with closing(db()) as con:

        rows = con.execute("""
        SELECT *
        FROM games
        WHERE status IN (
            'bot_creator_turn',
            'creator_turn',
            'opponent_turn'
        )
        ORDER BY id DESC
        LIMIT 100
        """).fetchall()

    for row in rows:

        creator_id = int(row["creator_id"])
        opponent_id = (
            int(row["opponent_id"])
            if row["opponent_id"] is not None
            else None
        )

        if int(row["chat_id"]) != message.chat_id:
            continue

        if row["status"] == "bot_creator_turn":
            if user.id == creator_id:
                game = row
                break

        elif row["status"] == "creator_turn":
            if user.id == creator_id:
                game = row
                break

        elif row["status"] == "opponent_turn":
            if opponent_id == user.id:
                game = row
                break

    if not game:
        return

    game_type = game["game_type"]

    if not is_game_dice_correct(game_type, dice):

        await message.reply_text(
            f"❌ برای این بازی باید {GAME_EMOJIS[game_type]} بفرستی."
        )

        return

    game_id = int(game["id"])
    rounds = int(game["rounds"])

    creator_rolls = parse_rolls(
        game["creator_rolls"]
    )

    opponent_rolls = parse_rolls(
        game["opponent_rolls"]
    )

    # ========================================================
    # BOT GAME
    # ========================================================

    if game["status"] == "bot_creator_turn":

        if len(creator_rolls) >= rounds:
            return

        creator_rolls.append(int(dice.value))

        update_game_rolls(
            game_id,
            ",".join(map(str, creator_rolls)),
            "",
        )

        remaining = rounds - len(creator_rolls)

        if remaining > 0:

            await message.reply_text(
                f"🎯 {name_of(user)}: {dice.value}\n\n"
                f"هنوز {remaining} پرتاب باقی مانده."
            )

            return

        # تمام شد؛ حالا ربات خودش رول می‌کند
        update_game_rolls(
            game_id,
            ",".join(map(str, creator_rolls)),
            "",
            "bot_rolling"
        )

        await message.reply_text(
            f"🤖 نوبت ربات است...\n"
            f"ربات {rounds} بار {GAME_EMOJIS[game_type]} می‌فرستد."
        )

        bot_rolls = []

        for _ in range(rounds):

            sent = await context.bot.send_dice(
                chat_id=message.chat_id,
                emoji=GAME_EMOJIS[game_type]
            )

            bot_rolls.append(
                int(sent.dice.value)
            )

            await asyncio.sleep(1)

        await finish_bot_game(
            context,
            game,
            creator_rolls,
            bot_rolls
        )

        return

    # ========================================================
    # FRIEND GAME - CREATOR
    # ========================================================

    if game["status"] == "creator_turn":

        if user.id != int(game["creator_id"]):
            return

        if len(creator_rolls) >= rounds:
            return

        creator_rolls.append(
            int(dice.value)
        )

        if len(creator_rolls) < rounds:

            update_game_rolls(
                game_id,
                ",".join(map(str, creator_rolls)),
                ""
            )

            remaining = rounds - len(creator_rolls)

            await message.reply_text(
                f"👤 {name_of(user)}: {dice.value}\n"
                f"🎯 {remaining} پرتاب باقی مانده."
            )

            return

        update_game_rolls(
            game_id,
            ",".join(map(str, creator_rolls)),
            "",
            "opponent_turn"
        )

        opponent_id = int(game["opponent_id"])

        try:
            opponent_chat = await context.bot.get_chat(
                opponent_id
            )
            opponent_name = (
                opponent_chat.first_name
                or opponent_chat.username
                or str(opponent_id)
            )
        except Exception:
            opponent_name = str(opponent_id)

        await message.reply_text(
            f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
            f"حالا حریف، {opponent_name}، "
            f"خودش {rounds} بار "
            f"{GAME_EMOJIS[game_type]} بفرستد."
        )

        return

    # ========================================================
    # FRIEND GAME - OPPONENT
    # ========================================================

    if game["status"] == "opponent_turn":

        opponent_id = int(game["opponent_id"])

        if user.id != opponent_id:
            return

        if len(opponent_rolls) >= rounds:
            return

        opponent_rolls.append(
            int(dice.value)
        )

        if len(opponent_rolls) < rounds:

            update_game_rolls(
                game_id,
                ",".join(map(str, creator_rolls)),
                ",".join(map(str, opponent_rolls))
            )

            remaining = rounds - len(opponent_rolls)

            await message.reply_text(
                f"👤 {name_of(user)}: {dice.value}\n"
                f"🎯 {remaining} پرتاب باقی مانده."
            )

            return

        update_game_rolls(
            game_id,
            ",".join(map(str, creator_rolls)),
            ",".join(map(str, opponent_rolls)),
            "finished"
        )

        await finish_friend_game(
            context,
            game,
            creator_rolls,
            opponent_rolls
        )


# ============================================================
# RESULT CALCULATION
# ============================================================

def calculate_score(rolls):
    return sum(int(x) for x in rolls)


def winner_from_scores(score1, score2):
    if score1 > score2:
        return 1

    if score2 > score1:
        return 2

    return 0


# ============================================================
# FINISH FRIEND GAME
# ============================================================

async def finish_friend_game(
    context,
    game,
    creator_rolls,
    opponent_rolls
):

    game_id = int(game["id"])

    creator_id = int(game["creator_id"])
    opponent_id = int(game["opponent_id"])

    amount = D(game["amount"])

    score_creator = calculate_score(
        creator_rolls
    )

    score_opponent = calculate_score(
        opponent_rolls
    )

    result = winner_from_scores(
        score_creator,
        score_opponent
    )

    # ========================================================
    # مساوی
    # ========================================================

    if result == 0:

        credit_balance(
            creator_id,
            amount
        )

        credit_balance(
            opponent_id,
            amount
        )

        with closing(db()) as con:
            con.execute("""
            UPDATE games
            SET status='finished',
                winner_id=NULL
            WHERE id=?
            """, (
                game_id,
            ))
            con.commit()

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🤝 مساوی شد!\n\n"
                f"👤 {creator_id}: {score_creator}\n"
                f"👤 {opponent_id}: {score_opponent}\n\n"
                f"💰 مبلغ {money(amount)} TRX به هر دو نفر برگشت داده شد."
            )
        )

        return

    if result == 1:
        winner_id = creator_id
        loser_id = opponent_id
        winner_score = score_creator
        loser_score = score_opponent
    else:
        winner_id = opponent_id
        loser_id = creator_id
        winner_score = score_opponent
        loser_score = score_creator

    # ========================================================
    # پرداخت
    # ========================================================

    payout = amount * Decimal("2") - OWNER_SHARE - BOT_FEE

    # طبق مثال 0.5:
    # 1.00 کل
    # 0.95 برنده
    # 0.02 مالک
    # 0.03 کارمزد

    # پرداخت به برنده
    credit_balance(
        winner_id,
        payout
    )

    # سهم مالک + کارمزد
    add_house(
        OWNER_SHARE,
        BOT_FEE
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
            f"👤 {creator_id}: {score_creator}\n"
            f"👤 {opponent_id}: {score_opponent}\n\n"
            f"🏆 برنده: {winner_id}\n"
            f"🎯 امتیاز برنده: {winner_score}\n"
            f"🎯 امتیاز حریف: {loser_score}\n\n"
            f"💰 مبلغ دریافتی برنده: {money(payout)} TRX"
        )
    )


# ============================================================
# FINISH BOT GAME
# ============================================================

async def finish_bot_game(
    context,
    game,
    user_rolls,
    bot_rolls
):

    game_id = int(game["id"])
    user_id = int(game["creator_id"])

    amount = D(game["amount"])

    user_score = calculate_score(
        user_rolls
    )

    bot_score = calculate_score(
        bot_rolls
    )

    result = winner_from_scores(
        user_score,
        bot_score
    )

    # ========================================================
    # مساوی
    # ========================================================

    if result == 0:

        credit_balance(
            user_id,
            amount
        )

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET status='finished',
                winner_id=NULL,
                opponent_rolls=?
            WHERE id=?
            """, (
                ",".join(map(str, bot_rolls)),
                game_id
            ))

            con.commit()

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🤝 مساوی شد!\n\n"
                f"👤 {user_id}: {user_score}\n"
                f"🤖 ربات: {bot_score}\n\n"
                f"💰 مبلغ {money(amount)} TRX برگشت داده شد."
            )
        )

        return

    # ========================================================
    # USER WIN
    # ========================================================

    if result == 1:

        payout = (
            amount * Decimal("2")
            - OWNER_SHARE
            - BOT_FEE
        )

        credit_balance(
            user_id,
            payout
        )

        add_house(
            OWNER_SHARE,
            BOT_FEE
        )

        winner_text = f"👤 {user_id}"
        winner_score = user_score

    # ========================================================
    # BOT WIN
    # ========================================================

    else:

        # در این حالت مبلغ بازیکن به خانه می‌رود.
        add_house(
            amount,
            Decimal("0")
        )

        winner_text = "🤖 ربات"
        winner_score = bot_score

    with closing(db()) as con:

        con.execute("""
        UPDATE games
        SET status='finished',
            winner_id=?,
            opponent_rolls=?
        WHERE id=?
        """, (
            user_id if result == 1 else None,
            ",".join(map(str, bot_rolls)),
            game_id
        ))

        con.commit()

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            f"🏆 نتیجه بازی\n\n"
            f"👤 {user_id}: {user_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            f"🏆 برنده: {winner_text}\n"
            f"🎯 امتیاز برنده: {winner_score}\n\n"
            + (
                f"💰 مبلغ دریافتی: {money(payout)} TRX"
                if result == 1
                else
                "🤖 ربات برنده شد."
            )
        )
    )


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    if not message.reply_to_message:

        await message.reply_text(
            "💸 برای انتقال روی پیام کاربر Reply کن.\n\n"
            "مثال:\n"
            "انتقال 0.5"
        )

        return

    target = message.reply_to_message.from_user

    if not target:

        await message.reply_text(
            "❌ مقصد پیدا نشد."
        )

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

    amount = parse_amount_from_command(
        message.text
    )

    if amount is None:

        await message.reply_text(
            "❌ مقدار نامعتبر.\n"
            "مثال: انتقال 0.5"
        )

        return

    ensure_user(target)

    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    async with DB_LOCK:

        if not debit_balance(
            user.id,
            amount
        ):
            await message.reply_text(
                "❌ موجودی کافی نیست یا تراکنش تغییر کرد."
            )
            return

        if not credit_balance(
            target.id,
            amount
        ):

            credit_balance(
                user.id,
                amount
            )

            await message.reply_text(
                "❌ انتقال انجام نشد؛ مبلغ برگشت داده شد."
            )

            return

        with closing(db()) as con:

            con.execute("""
            INSERT INTO transfers
            (sender_id, receiver_id, amount)
            VALUES (?, ?, ?)
            """, (
                user.id,
                target.id,
                str(amount)
            ))

            con.commit()

    await message.reply_text(
        f"✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {name_of(target)}\n"
        f"💸 مقدار: {money(amount)} TRX"
    )


# ============================================================
# REQUEST
# ============================================================

async def request_menu(update, context):

    user = update.effective_user

    ensure_user(user)

    await update.effective_message.reply_text(
        "📤 درخواست\n\n"
        "مثال:\n"
        "درخواست 5\n\n"
        "بعد از آن اطلاعات درخواست را ارسال کن."
    )

    context.user_data["request_mode"] = "amount"


async def create_request(
    user_id,
    amount,
    wallet
):

    with closing(db()) as con:

        con.execute("""
        INSERT INTO requests
        (user_id, amount, wallet)
        VALUES (?, ?, ?)
        """, (
            user_id,
            str(amount),
            wallet
        ))

        con.commit()


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    await update.effective_message.reply_text(
        "📖 راهنما\n\n"
        "🎮 ساخت بازی در گپ:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "2 بسکتبال 0.5\n"
        "3 دارت 0.5\n\n"
        "💰 موجودی\n"
        "💸 انتقال 0.5 ← با Reply\n"
        "👥 بازی با دوستان\n"
        "🤖 بازی با ربات\n"
        "📤 درخواست\n\n"
        "تعداد بازی/پرتاب محدودیت ندارد."
    )


# ============================================================
# ADMIN
# ============================================================

async def admin(update, context):

    user = update.effective_user

    if not user or not is_admin(user.id):

        await update.effective_message.reply_text(
            "⛔ دسترسی ندارید."
        )

        return

    await update.effective_message.reply_text(
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

    # ========================================================
    # USERS
    # ========================================================

    if data == "admin_users":

        with closing(db()) as con:

            rows = con.execute("""
            SELECT user_id,
                   first_name,
                   username,
                   balance,
                   blocked
            FROM users
            ORDER BY CAST(balance AS REAL) DESC
            LIMIT 50
            """).fetchall()

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
                f"💰 {money(row['balance'])} TRX\n\n"
            )

        await query.edit_message_text(
            text[:4000]
        )

        return

    # ========================================================
    # STATS
    # ========================================================

    if data == "admin_stats":

        with closing(db()) as con:

            users = con.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            total = con.execute(
                "SELECT SUM(CAST(balance AS REAL)) FROM users"
            ).fetchone()[0] or 0

            games = con.execute(
                "SELECT COUNT(*) FROM games"
            ).fetchone()[0]

            pending = con.execute(
                "SELECT COUNT(*) FROM requests "
                "WHERE status='pending'"
            ).fetchone()[0]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"💰 مجموع موجودی کاربران: {money(total)} TRX\n"
            f"🎮 تعداد بازی‌ها: {games:,}\n"
            f"📤 درخواست‌ها: {pending:,}\n\n"
            f"📥 برداشت: "
            f"{'روشن 🟢' if withdraw_enabled() else 'خاموش 🔴'}"
        )

        return

    # ========================================================
    # WITHDRAW TOGGLE
    # ========================================================

    if data == "admin_withdraw_toggle":

        new_value = not withdraw_enabled()

        set_setting(
            "withdraw_enabled",
            "1" if new_value else "0"
        )

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            f"📥 برداشت: "
            f"{'روشن 🟢' if new_value else 'خاموش 🔴'}",
            reply_markup=admin_keyboard()
        )

        return

    # ========================================================
    # ADD
    # ========================================================

    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "در پیوی ربات:\n\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 0.5"
        )

        return

    # ========================================================
    # REMOVE
    # ========================================================

    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "در پیوی ربات:\n\n"
            "/removebalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/removebalance 123456789 0.5"
        )

        return

    # ========================================================
    # REQUESTS
    # ========================================================

    if data == "admin_requests":

        with closing(db()) as con:

            rows = con.execute("""
            SELECT *
            FROM requests
            WHERE status='pending'
            ORDER BY id DESC
            LIMIT 20
            """).fetchall()

        if not rows:

            await query.edit_message_text(
                "📋 درخواست در انتظار وجود ندارد."
            )

            return

        text = "📋 درخواست‌ها\n\n"

        for row in rows:

            text += (
                f"#{row['id']}\n"
                f"👤 {row['user_id']}\n"
                f"💰 {money(row['amount'])} TRX\n"
                f"📝 {row['wallet']}\n\n"
            )

        await query.edit_message_text(
            text[:4000]
        )


# ============================================================
# ADMIN ADD BALANCE
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

        amount = parse_decimal_amount(
            normalize_digits(
                context.args[1]
            )
        )

    except Exception:

        amount = None

    if amount is None:

        await update.message.reply_text(
            "❌ مقدار نامعتبر."
        )

        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    credit_balance(
        target_id,
        amount
    )

    await update.message.reply_text(
        f"✅ موجودی افزایش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➕ {money(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
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

        amount = parse_decimal_amount(
            normalize_digits(
                context.args[1]
            )
        )

    except Exception:

        amount = None

    if amount is None:

        await update.message.reply_text(
            "❌ مقدار نامعتبر."
        )

        return

    if not get_user(target_id):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    if not debit_balance(
        target_id,
        amount
    ):

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

    await update.message.reply_text(
        f"✅ موجودی کاهش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➖ {money(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
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
            target_id,
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
            target_id,
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

    text = message.text.strip()
    normalized = normalize_digits(text)

    # ========================================================
    # REQUEST MODE
    # ========================================================

    request_mode = context.user_data.get(
        "request_mode"
    )

    if request_mode == "amount":

        amount = parse_amount_from_command(
            normalized
        )

        if amount:

            context.user_data["request_amount"] = amount
            context.user_data["request_mode"] = "wallet"

            await message.reply_text(
                "📝 مقدار ثبت شد.\n\n"
                "حالا اطلاعات درخواست را بفرست."
            )

            return

    elif request_mode == "wallet":

        amount = context.user_data.get(
            "request_amount"
        )

        if amount:

            await create_request(
                user.id,
                amount,
                text
            )

            context.user_data.clear()

            await message.reply_text(
                "✅ درخواست ثبت شد."
            )

            return

    # ========================================================
    # GAME CREATION
    # ========================================================

    parsed = parse_game(normalized)

    if parsed:

        game, rounds, amount = parsed

        await create_game_message(
            update,
            context,
            game,
            rounds,
            amount
        )

        return

    # ========================================================
    # BALANCE
    # ========================================================

    if text in (
        "💰 موجودی",
        "موجودی",
        "موجودی ترون",
        "موجودی TRX",
        "balance"
    ):

        await show_balance(
            update,
            context
        )

        return

    # ========================================================
    # GAME
    # ========================================================

    if text == "🎮 بازی":

        await game_menu(
            update,
            context
        )

        return

    # ========================================================
    # FRIENDS
    # ========================================================

    if text == "👥 بازی با دوستان":

        await friends_menu(
            update,
            context
        )

        return

    # ========================================================
    # BOT
    # ========================================================

    if text == "🤖 بازی با ربات":

        await bot_menu(
            update,
            context
        )

        return

    # ========================================================
    # TRANSFER
    # ========================================================

    if (
        re.match(
            r"^(انتقال|transfer)\s+\d+(?:[.,]\d+)?$",
            normalized,
            re.IGNORECASE
        )
    ):

        await transfer(
            update,
            context
        )

        return

    # ========================================================
    # REQUEST
    # ========================================================

    if re.match(
        r"^(درخواست|request)\s+\d+(?:[.,]\d+)?$",
        normalized,
        re.IGNORECASE
    ):

        amount = parse_amount_from_command(
            normalized
        )

        if amount:

            context.user_data["request_amount"] = amount
            context.user_data["request_mode"] = "wallet"

            await message.reply_text(
                "📝 مقدار ثبت شد.\n\n"
                "حالا اطلاعات درخواست را بفرست."
            )

        return

    # ========================================================
    # HELP
    # ========================================================

    if text == "📖 راهنما":

        await help_command(
            update,
            context
        )

        return


# ============================================================
# ERROR
# ============================================================

async def error_handler(update, context):

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
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            show_balance
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
            friends_menu
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

    # ========================================================
    # GAME BUTTONS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            game_callback,
            pattern=r"^game_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            game_action_callback,
            pattern=r"^(join_|bot_|cancel_)"
        )
    )

    # ========================================================
    # ADMIN BUTTONS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # ========================================================
    # USER DICE / BOWLING / BASKETBALL / DARTS
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            process_user_roll
        ),
        group=0
    )

    # ========================================================
    # TEXT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        ),
        group=1
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


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
