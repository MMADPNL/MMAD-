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
# SETTINGS
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

ADMIN_IDS = {
    8552447077
}

DB_FILE = "bot.db"

FORCE_JOIN_CHAT = "@zobxt"
FORCE_JOIN_URL = "https://t.me/zobxt"

MIN_GAME = Decimal("0.01")
MAX_GAME = Decimal("1000000000")

OWNER_SHARE = Decimal("0.02")
BOT_FEE = Decimal("0.03")
WINNER_PAYOUT = Decimal("0.95")

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

        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=30000")

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

        # ====================================================
        # MIGRATION
        # اطلاعات قبلی حفظ می‌شود
        # ====================================================

        game_columns = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        }

        if "message_id" not in game_columns:
            con.execute(
                "ALTER TABLE games ADD COLUMN message_id INTEGER DEFAULT 0"
            )

        if "creator_rolls" not in game_columns:
            con.execute(
                "ALTER TABLE games ADD COLUMN creator_rolls TEXT DEFAULT ''"
            )

        if "opponent_rolls" not in game_columns:
            con.execute(
                "ALTER TABLE games ADD COLUMN opponent_rolls TEXT DEFAULT ''"
            )

        if "winner_id" not in game_columns:
            con.execute(
                "ALTER TABLE games ADD COLUMN winner_id INTEGER DEFAULT NULL"
            )

        user_columns = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        if "balance" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN balance TEXT DEFAULT '0'"
            )

        if "blocked" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0"
            )

        # ایندکس برای پیدا کردن سریع بازی‌ها
        con.execute("""
        CREATE INDEX IF NOT EXISTS idx_games_active
        ON games(chat_id, status, creator_id)
        """)

        con.execute("""
        CREATE INDEX IF NOT EXISTS idx_games_message
        ON games(chat_id, message_id)
        """)

        con.commit()


# ============================================================
# DECIMAL HELPERS
# ============================================================

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


# ============================================================
# USERS
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


def get_balance(user_id):
    row = get_user(user_id)

    if not row:
        return Decimal("0")

    return D(row["balance"])


def is_blocked(user_id):
    row = get_user(user_id)

    return bool(
        row and
        int(row["blocked"]) == 1
    )


def is_admin(user_id):
    return user_id in ADMIN_IDS


# ============================================================
# ATOMIC BALANCE
# ============================================================

def debit_balance(user_id, amount):
    amount = D(amount)

    if amount <= 0:
        return False

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user_id,)).fetchone()

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

            row = con.execute("""
            SELECT balance
            FROM users
            WHERE user_id=?
            """, (user_id,)).fetchone()

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


# ============================================================
# HOUSE
# ============================================================

def add_house(owner_amount, fee_amount):
    owner_amount = D(owner_amount)
    fee_amount = D(fee_amount)

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT owner_balance, fee_balance
            FROM house
            WHERE id=1
            """).fetchone()

            if not row:
                con.execute("ROLLBACK")
                return False

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

            con.execute("COMMIT")
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("HOUSE ERROR")
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
# DIGITS / PARSING
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
    نمونه:

    1 تاس 0.5
    2 تاس 0.1
    100 بولینگ 0.5
    2 بسکتبال 0.5
    20 دارت 0.5

    تعداد نامحدود است.
    """

    text = normalize_digits(text or "").strip()

    pattern = re.compile(
        r"^(\d+)\s+([^\s]+)\s+(\d+(?:[.,]\d+)?)$",
        re.IGNORECASE
    )

    m = pattern.match(text)

    if not m:
        return None

    try:
        rounds = int(m.group(1))
    except Exception:
        return None

    game_name = m.group(2).lower()
    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    amount = parse_decimal_amount(
        m.group(3).replace(",", ".")
    )

    if amount is None:
        return None

    if rounds < 1:
        return None

    return game, rounds, amount


# ============================================================
# NAME
# ============================================================

def name_of(user):
    if not user:
        return "کاربر"

    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


def user_display_by_id(user_id):
    row = get_user(user_id)

    if not row:
        return str(user_id)

    if row["first_name"]:
        return row["first_name"]

    if row["username"]:
        return "@" + row["username"]

    return str(user_id)


# ============================================================
# FORCE JOIN
# ============================================================

async def check_force_join(context, user_id):
    try:
        member = await context.bot.get_chat_member(
            FORCE_JOIN_CHAT,
            user_id
        )

        status = member.status

        if status in (
            "member",
            "administrator",
            "creator",
            "owner"
        ):
            return True

        return False

    except TelegramError as e:
        logger.warning(
            "FORCE JOIN CHECK ERROR: %s",
            e
        )

        # اگر بات دسترسی بررسی عضویت نداشت،
        # کاربر را بلاک نمی‌کنیم تا کل بات از کار نیفتد.
        return True


async def force_join_message(update, context):
    await update.effective_message.reply_text(
        "⛔ برای استفاده از ربات ابتدا باید در کانال/گپ زیر عضو شوی:",
        reply_markup=InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "📢 عضویت در zobxt",
                    url=FORCE_JOIN_URL
                )
            ]
        ])
    )


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

    if not await check_force_join(
        context,
        user.id
    ):
        await force_join_message(
            update,
            context
        )
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

    if not await check_force_join(
        context,
        user.id
    ):
        await force_join_message(
            update,
            context
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
        "تعداد پرتاب محدودیت ندارد."
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
        "بعد از شروع، خودت ایموجی بازی را بفرست."
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

    if not await check_force_join(
        context,
        user.id
    ):
        await force_join_message(
            update,
            context
        )
        return

    balance = get_balance(user.id)

    if balance < amount:
        await message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {money(balance)} TRX"
        )
        return

    # ========================================================
    # رزرو اتمیک مبلغ سازنده
    # ========================================================

    if not debit_balance(
        user.id,
        amount
    ):
        await message.reply_text(
            "❌ موجودی کافی نیست یا هم‌زمان تغییر کرده است."
        )
        return

    game_id = None

    try:

        with closing(db()) as con:

            con.execute("BEGIN IMMEDIATE")

            cur = con.execute("""
            INSERT INTO games
            (
                chat_id,
                message_id,
                creator_id,
                opponent_id,
                game_type,
                amount,
                rounds,
                creator_rolls,
                opponent_rolls,
                status
            )
            VALUES (?, 0, ?, NULL, ?, ?, ?, '', '', 'waiting')
            """, (
                chat.id,
                user.id,
                game,
                str(amount),
                rounds
            ))

            game_id = cur.lastrowid

            con.execute("COMMIT")

        text = (
            f"{GAME_LABELS[game]}\n\n"
            f"🎮 تعداد پرتاب: {rounds}\n"
            f"💰 مبلغ بازی: {money(amount)} TRX\n\n"
            f"👤 سازنده: {name_of(user)}\n\n"
            f"یکی از گزینه‌ها را انتخاب کن:"
        )

        sent = await context.bot.send_message(
            chat_id=chat.id,
            text=text,
            reply_markup=game_created_keyboard(game_id)
        )

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET message_id=?
            WHERE id=? AND status='waiting'
            """, (
                sent.message_id,
                game_id
            ))

            con.commit()

    except Exception:

        logger.exception(
            "CREATE GAME ERROR"
        )

        # فقط اگر بازی در DB ایجاد شده باشد،
        # آن را cancelled می‌کنیم و مبلغ را برمی‌گردانیم.
        try:
            if game_id:

                with closing(db()) as con:
                    con.execute("BEGIN IMMEDIATE")

                    row = con.execute("""
                    SELECT status, amount
                    FROM games
                    WHERE id=?
                    """, (game_id,)).fetchone()

                    if row and row["status"] == "waiting":

                        con.execute("""
                        UPDATE games
                        SET status='cancelled'
                        WHERE id=?
                        """, (game_id,))

                        con.execute("COMMIT")

                        credit_balance(
                            user.id,
                            D(row["amount"])
                        )

                    else:
                        con.execute("ROLLBACK")

            else:
                credit_balance(
                    user.id,
                    amount
                )

        except Exception:
            logger.exception(
                "CREATE GAME REFUND ERROR"
            )

        await message.reply_text(
            "❌ بازی ساخته نشد؛ مبلغ در صورت رزرو برگشت داده شد."
        )


# ============================================================
# GAME BUTTON MENU
# ============================================================

async def game_callback(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    if is_blocked(user.id):
        return

    if not await check_force_join(
        context,
        user.id
    ):
        await query.message.reply_text(
            "⛔ ابتدا باید در zobxt عضو شوی."
        )
        return

    game = query.data.replace(
        "game_",
        "",
        1
    )

    if game not in GAME_LABELS:
        return

    label = GAME_LABELS[game].split(
        " ",
        1
    )[1]

    await query.message.reply_text(
        f"{GAME_LABELS[game]}\n\n"
        f"مثال:\n"
        f"1 {label} 0.5\n"
        f"2 {label} 0.5\n\n"
        f"تعداد پرتاب محدودیت ندارد."
    )


# ============================================================
# GAME DB HELPERS
# ============================================================

def get_game(game_id):
    with closing(db()) as con:
        return con.execute(
            "SELECT * FROM games WHERE id=?",
            (game_id,)
        ).fetchone()


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
    user = query.from_user

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        await query.answer(
            "❌ بازی نامعتبر است.",
            show_alert=True
        )
        return

    ensure_user(user)

    if not await check_force_join(
        context,
        user.id
    ):
        await query.answer(
            "❌ ابتدا در zobxt عضو شو.",
            show_alert=True
        )
        return

    creator_id = None
    game = None

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

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

                creator_id = int(
                    game["creator_id"]
                )

                if creator_id == user.id:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ خودت سازنده بازی هستی.",
                        show_alert=True
                    )
                    return

                amount = D(game["amount"])

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    user.id,
                )).fetchone()

                if not row or D(row["balance"]) < amount:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ موجودی کافی نیست.",
                        show_alert=True
                    )
                    return

                # رزرو اتمیک حریف
                new_balance = (
                    D(row["balance"])
                    - amount
                )

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
                SET opponent_id=?,
                    status='creator_turn'
                WHERE id=?
                  AND status='waiting'
                """, (
                    user.id,
                    game_id
                ))

                if con.total_changes < 2:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ بازی هم‌زمان توسط شخص دیگری گرفته شد.",
                        show_alert=True
                    )
                    return

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "JOIN FRIEND ERROR"
                )

                await query.answer(
                    "❌ خطا در ورود به بازی.",
                    show_alert=True
                )

                return

    # حذف کل دکمه‌ها
    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    creator_name = user_display_by_id(
        creator_id
    )

    await query.message.reply_text(
        f"👥 حریف وارد بازی شد: {name_of(user)}\n\n"
        f"🎯 حالا نوبت سازنده است:\n"
        f"👤 {creator_name}\n\n"
        f"خودش باید {game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرستد."
    )


# ============================================================
# BOT GAME
# ============================================================

async def join_bot(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        await query.answer(
            "❌ بازی نامعتبر است.",
            show_alert=True
        )
        return

    ensure_user(user)

    if not await check_force_join(
        context,
        user.id
    ):
        await query.answer(
            "❌ ابتدا در zobxt عضو شو.",
            show_alert=True
        )
        return

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

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

                # فقط سازنده همان بازی
                if int(game["creator_id"]) != user.id:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ فقط سازنده همین بازی می‌تواند با ربات بازی کند.",
                        show_alert=True
                    )
                    return

                # مبلغ قبلاً هنگام ساخت بازی رزرو شده.
                con.execute("""
                UPDATE games
                SET opponent_id=NULL,
                    status='bot_creator_turn'
                WHERE id=?
                  AND creator_id=?
                  AND status='waiting'
                """, (
                    game_id,
                    user.id
                ))

                if con.total_changes < 1:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ بازی هم‌زمان تغییر کرد.",
                        show_alert=True
                    )
                    return

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "BOT JOIN ERROR"
                )

                await query.answer(
                    "❌ خطا در شروع بازی.",
                    show_alert=True
                )

                return

    # دکمه‌ها حذف می‌شوند
    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await query.message.reply_text(
        f"🤖 بازی با ربات شروع شد.\n\n"
        f"👤 {name_of(user)}\n\n"
        f"نوبت توست.\n"
        f"خودت {game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرست.\n\n"
        f"ربات تا قبل از پایان پرتاب‌های تو هیچ تاسی نمی‌اندازد."
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        await query.answer(
            "❌ بازی نامعتبر است.",
            show_alert=True
        )
        return

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

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

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    user.id,
                )).fetchone()

                if not row:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ کاربر پیدا نشد.",
                        show_alert=True
                    )
                    return

                new_balance = (
                    D(row["balance"])
                    + amount
                )

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
                  AND creator_id=?
                  AND status='waiting'
                """, (
                    game_id,
                    user.id
                ))

                if con.total_changes < 2:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ بازی هم‌زمان تغییر کرد.",
                        show_alert=True
                    )
                    return

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "CANCEL ERROR"
                )

                await query.answer(
                    "❌ خطا در لغو بازی.",
                    show_alert=True
                )

                return

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
# FIND ACTIVE GAME FOR USER
# ============================================================

def find_active_game_for_user(
    chat_id,
    user_id
):
    with closing(db()) as con:

        rows = con.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
          AND status IN (
              'bot_creator_turn',
              'creator_turn',
              'opponent_turn'
          )
        ORDER BY id ASC
        """, (
            chat_id,
        )).fetchall()

    # اولویت بازی‌ای که همین کاربر نوبتش است
    for row in rows:

        status = row["status"]

        if status in (
            "bot_creator_turn",
            "creator_turn"
        ):

            if int(row["creator_id"]) == user_id:
                return row

        elif status == "opponent_turn":

            if (
                row["opponent_id"] is not None
                and
                int(row["opponent_id"]) == user_id
            ):
                return row

    return None


# ============================================================
# ATOMIC ROLL SAVE
# ============================================================

def append_creator_roll(
    game_id,
    user_id,
    value,
    bot_game=False
):
    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            game = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if not game:
                con.execute("ROLLBACK")
                return None

            if bot_game:

                if game["status"] != "bot_creator_turn":
                    con.execute("ROLLBACK")
                    return None

                if int(game["creator_id"]) != user_id:
                    con.execute("ROLLBACK")
                    return None

            else:

                if game["status"] != "creator_turn":
                    con.execute("ROLLBACK")
                    return None

                if int(game["creator_id"]) != user_id:
                    con.execute("ROLLBACK")
                    return None

            rolls = parse_rolls(
                game["creator_rolls"]
            )

            rounds = int(game["rounds"])

            if len(rolls) >= rounds:
                con.execute("ROLLBACK")
                return None

            rolls.append(int(value))

            new_status = game["status"]

            if (
                len(rolls) >= rounds
                and
                not bot_game
            ):
                new_status = "opponent_turn"

            con.execute("""
            UPDATE games
            SET creator_rolls=?,
                status=?
            WHERE id=?
            """, (
                ",".join(map(str, rolls)),
                new_status,
                game_id
            ))

            con.execute("COMMIT")

            return {
                "game": game,
                "rolls": rolls,
                "status": new_status
            }

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception(
                "APPEND CREATOR ROLL ERROR"
            )

            return None


def append_opponent_roll(
    game_id,
    user_id,
    value
):
    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            game = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (
                game_id,
            )).fetchone()

            if not game:
                con.execute("ROLLBACK")
                return None

            if game["status"] != "opponent_turn":
                con.execute("ROLLBACK")
                return None

            if (
                game["opponent_id"] is None
                or
                int(game["opponent_id"]) != user_id
            ):
                con.execute("ROLLBACK")
                return None

            rolls = parse_rolls(
                game["opponent_rolls"]
            )

            rounds = int(game["rounds"])

            if len(rolls) >= rounds:
                con.execute("ROLLBACK")
                return None

            rolls.append(int(value))

            new_status = (
                "finished"
                if len(rolls) >= rounds
                else "opponent_turn"
            )

            con.execute("""
            UPDATE games
            SET opponent_rolls=?,
                status=?
            WHERE id=?
            """, (
                ",".join(map(str, rolls)),
                new_status,
                game_id
            ))

            con.execute("COMMIT")

            return {
                "game": game,
                "rolls": rolls,
                "status": new_status
            }

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception(
                "APPEND OPPONENT ROLL ERROR"
            )

            return None


# ============================================================
# SCORE
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
# FRIEND RESULT
# ============================================================

async def finish_friend_game(
    context,
    game,
    creator_rolls,
    opponent_rolls
):

    game_id = int(game["id"])

    creator_id = int(
        game["creator_id"]
    )

    opponent_id = int(
        game["opponent_id"]
    )

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
    # DRAW
    # ========================================================

    if result == 0:

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute("BEGIN IMMEDIATE")

                    game_now = con.execute("""
                    SELECT status
                    FROM games
                    WHERE id=?
                    """, (
                        game_id,
                    )).fetchone()

                    if not game_now:
                        con.execute("ROLLBACK")
                        return

                    # اگر قبلاً تسویه شده، دوباره پرداخت نکن
                    if game_now["status"] == "settled":
                        con.execute("ROLLBACK")
                        return

                    con.execute("""
                    UPDATE games
                    SET status='settled',
                        winner_id=NULL
                    WHERE id=?
                    """, (
                        game_id,
                    ))

                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL)+?
                    WHERE user_id=?
                    """, (
                        str(amount),
                        creator_id
                    ))

                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL)+?
                    WHERE user_id=?
                    """, (
                        str(amount),
                        opponent_id
                    ))

                    con.execute("COMMIT")

                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass

                    logger.exception(
                        "FRIEND DRAW SETTLE ERROR"
                    )
                    return

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🤝 مساوی شد!\n\n"
                f"👤 {user_display_by_id(creator_id)}: "
                f"{score_creator}\n"
                f"👤 {user_display_by_id(opponent_id)}: "
                f"{score_opponent}\n\n"
                f"💰 مبلغ بازی به هر دو نفر برگشت داده شد."
            )
        )

        return

    if result == 1:
        winner_id = creator_id
        winner_score = score_creator
        loser_score = score_opponent
    else:
        winner_id = opponent_id
        winner_score = score_opponent
        loser_score = score_creator

    payout = (
        amount * Decimal("2")
        - OWNER_SHARE
        - BOT_FEE
    )

    # ========================================================
    # ATOMIC SETTLEMENT
    # ========================================================

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                current = con.execute("""
                SELECT status
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not current:
                    con.execute("ROLLBACK")
                    return

                if current["status"] == "settled":
                    con.execute("ROLLBACK")
                    return

                con.execute("""
                UPDATE users
                SET balance=CAST(balance AS REAL)+?
                WHERE user_id=?
                """, (
                    str(payout),
                    winner_id
                ))

                house = con.execute("""
                SELECT owner_balance, fee_balance
                FROM house
                WHERE id=1
                """).fetchone()

                owner = D(house["owner_balance"])
                fee = D(house["fee_balance"])

                con.execute("""
                UPDATE house
                SET owner_balance=?,
                    fee_balance=?
                WHERE id=1
                """, (
                    str(owner + OWNER_SHARE),
                    str(fee + BOT_FEE)
                ))

                con.execute("""
                UPDATE games
                SET status='settled',
                    winner_id=?
                WHERE id=?
                """, (
                    winner_id,
                    game_id
                ))

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "FRIEND SETTLE ERROR"
                )
                return

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            f"🏆 نتیجه بازی\n\n"
            f"👤 {user_display_by_id(creator_id)}: "
            f"{score_creator}\n"
            f"👤 {user_display_by_id(opponent_id)}: "
            f"{score_opponent}\n\n"
            f"🏆 برنده: "
            f"{user_display_by_id(winner_id)}\n"
            f"🎯 امتیاز برنده: {winner_score}\n"
            f"🎯 امتیاز حریف: {loser_score}\n\n"
            f"💰 دریافتی برنده: {money(payout)} TRX"
        )
    )


# ============================================================
# BOT RESULT
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
    # DRAW
    # ========================================================

    if result == 0:

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute("BEGIN IMMEDIATE")

                    current = con.execute("""
                    SELECT status
                    FROM games
                    WHERE id=?
                    """, (
                        game_id,
                    )).fetchone()

                    if not current:
                        con.execute("ROLLBACK")
                        return

                    if current["status"] == "settled":
                        con.execute("ROLLBACK")
                        return

                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL)+?
                    WHERE user_id=?
                    """, (
                        str(amount),
                        user_id
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='settled',
                        winner_id=NULL,
                        opponent_rolls=?
                    WHERE id=?
                    """, (
                        ",".join(map(str, bot_rolls)),
                        game_id
                    ))

                    con.execute("COMMIT")

                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass

                    logger.exception(
                        "BOT DRAW ERROR"
                    )
                    return

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🤝 مساوی شد!\n\n"
                f"👤 {user_display_by_id(user_id)}: "
                f"{user_score}\n"
                f"🤖 ربات: {bot_score}\n\n"
                f"💰 مبلغ بازی برگشت داده شد."
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

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute("BEGIN IMMEDIATE")

                    current = con.execute("""
                    SELECT status
                    FROM games
                    WHERE id=?
                    """, (
                        game_id,
                    )).fetchone()

                    if not current:
                        con.execute("ROLLBACK")
                        return

                    if current["status"] == "settled":
                        con.execute("ROLLBACK")
                        return

                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL)+?
                    WHERE user_id=?
                    """, (
                        str(payout),
                        user_id
                    ))

                    house = con.execute("""
                    SELECT owner_balance, fee_balance
                    FROM house
                    WHERE id=1
                    """).fetchone()

                    owner = D(house["owner_balance"])
                    fee = D(house["fee_balance"])

                    con.execute("""
                    UPDATE house
                    SET owner_balance=?,
                        fee_balance=?
                    WHERE id=1
                    """, (
                        str(owner + OWNER_SHARE),
                        str(fee + BOT_FEE)
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='settled',
                        winner_id=?,
                        opponent_rolls=?
                    WHERE id=?
                    """, (
                        user_id,
                        ",".join(map(str, bot_rolls)),
                        game_id
                    ))

                    con.execute("COMMIT")

                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass

                    logger.exception(
                        "BOT USER WIN ERROR"
                    )
                    return

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🏆 نتیجه بازی\n\n"
                f"👤 {user_display_by_id(user_id)}: "
                f"{user_score}\n"
                f"🤖 ربات: {bot_score}\n\n"
                f"🏆 برنده: "
                f"{user_display_by_id(user_id)}\n"
                f"🎯 امتیاز برنده: {user_score}\n\n"
                f"💰 دریافتی: {money(payout)} TRX"
            )
        )

        return

    # ========================================================
    # BOT WIN
    # ========================================================

    # در بازی ربات، اگر ربات ببرد،
    # مبلغ رزرو شده سازنده به خانه منتقل می‌شود.
    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                current = con.execute("""
                SELECT status
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not current:
                    con.execute("ROLLBACK")
                    return

                if current["status"] == "settled":
                    con.execute("ROLLBACK")
                    return

                house = con.execute("""
                SELECT owner_balance, fee_balance
                FROM house
                WHERE id=1
                """).fetchone()

                owner = D(house["owner_balance"])

                con.execute("""
                UPDATE house
                SET owner_balance=?
                WHERE id=1
                """, (
                    str(owner + amount),
                ))

                con.execute("""
                UPDATE games
                SET status='settled',
                    winner_id=NULL,
                    opponent_rolls=?
                WHERE id=?
                """, (
                    ",".join(map(str, bot_rolls)),
                    game_id
                ))

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "BOT WIN ERROR"
                )
                return

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            f"🏆 نتیجه بازی\n\n"
            f"👤 {user_display_by_id(user_id)}: "
            f"{user_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            f"🏆 برنده: 🤖 ربات\n"
            f"🎯 امتیاز برنده: {bot_score}\n"
            f"🎯 امتیاز کاربر: {user_score}"
        )
    )


# ============================================================
# PROCESS USER ROLL
# ============================================================

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

    if not await check_force_join(
        context,
        user.id
    ):
        await message.reply_text(
            "⛔ ابتدا در zobxt عضو شو."
        )
        return

    game = find_active_game_for_user(
        message.chat_id,
        user.id
    )

    if not game:
        return

    game_type = game["game_type"]

    if dice.emoji != GAME_EMOJIS[game_type]:

        await message.reply_text(
            f"❌ این بازی نیاز به "
            f"{GAME_EMOJIS[game_type]} دارد."
        )

        return

    game_id = int(game["id"])
    rounds = int(game["rounds"])

    # ========================================================
    # BOT GAME
    # ========================================================

    if game["status"] == "bot_creator_turn":

        saved = append_creator_roll(
            game_id,
            user.id,
            dice.value,
            bot_game=True
        )

        if not saved:
            return

        creator_rolls = saved["rolls"]

        await message.reply_text(
            f"👤 {name_of(user)}: {dice.value}"
        )

        if len(creator_rolls) < rounds:

            await message.reply_text(
                f"👤 {name_of(user)} هنوز "
                f"{rounds - len(creator_rolls)} پرتاب دارد."
            )

            return

        # ====================================================
        # کاربر تمام کرد
        # ====================================================

        # وضعیت را قفل می‌کنیم تا دو بار ربات اجرا نشود
        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute("BEGIN IMMEDIATE")

                    current = con.execute("""
                    SELECT *
                    FROM games
                    WHERE id=?
                    """, (
                        game_id,
                    )).fetchone()

                    if not current:
                        con.execute("ROLLBACK")
                        return

                    if current["status"] != "bot_creator_turn":
                        con.execute("ROLLBACK")
                        return

                    con.execute("""
                    UPDATE games
                    SET status='bot_rolling'
                    WHERE id=?
                    """, (
                        game_id,
                    ))

                    con.execute("COMMIT")

                except Exception:
                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass

                    return

        await message.reply_text(
            f"🤖 نوبت ربات است...\n"
            f"ربات {rounds} بار "
            f"{GAME_EMOJIS[game_type]} می‌اندازد."
        )

        bot_rolls = []

        try:

            for _ in range(rounds):

                sent = await context.bot.send_dice(
                    chat_id=message.chat_id,
                    emoji=GAME_EMOJIS[game_type]
                )

                bot_rolls.append(
                    int(sent.dice.value)
                )

                await asyncio.sleep(0.8)

        except Exception:

            logger.exception(
                "BOT ROLL ERROR"
            )

            # اگر ارسال ربات شکست خورد،
            # مبلغ بازی برگردد.
            await refund_failed_game(
                context,
                game_id
            )

            return

        await finish_bot_game(
            context,
            current,
            creator_rolls,
            bot_rolls
        )

        return

    # ========================================================
    # FRIEND CREATOR
    # ========================================================

    if game["status"] == "creator_turn":

        saved = append_creator_roll(
            game_id,
            user.id,
            dice.value,
            bot_game=False
        )

        if not saved:
            return

        creator_rolls = saved["rolls"]

        await message.reply_text(
            f"👤 {name_of(user)}: {dice.value}"
        )

        if len(creator_rolls) < rounds:

            await message.reply_text(
                f"👤 {name_of(user)} هنوز "
                f"{rounds - len(creator_rolls)} پرتاب دارد."
            )

            return

        opponent_id = int(
            game["opponent_id"]
        )

        await message.reply_text(
            f"👤 {name_of(user)} پرتاب‌هایش را کامل کرد.\n\n"
            f"🎯 حالا نوبت حریف است:\n"
            f"👤 {user_display_by_id(opponent_id)}\n\n"
            f"خودش باید {rounds} بار "
            f"{GAME_EMOJIS[game_type]} بفرستد."
        )

        return

    # ========================================================
    # FRIEND OPPONENT
    # ========================================================

    if game["status"] == "opponent_turn":

        opponent_id = int(
            game["opponent_id"]
        )

        if user.id != opponent_id:
            return

        saved = append_opponent_roll(
            game_id,
            user.id,
            dice.value
        )

        if not saved:
            return

        opponent_rolls = saved["rolls"]

        await message.reply_text(
            f"👤 {name_of(user)}: {dice.value}"
        )

        if len(opponent_rolls) < rounds:

            await message.reply_text(
                f"👤 {name_of(user)} هنوز "
                f"{rounds - len(opponent_rolls)} پرتاب دارد."
            )

            return

        # نتیجه
        final_game = get_game(game_id)

        if not final_game:
            return

        creator_rolls = parse_rolls(
            final_game["creator_rolls"]
        )

        await finish_friend_game(
            context,
            final_game,
            creator_rolls,
            opponent_rolls
        )


# ============================================================
# REFUND FAILED GAME
# ============================================================

async def refund_failed_game(
    context,
    game_id
):

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not game:
                    con.execute("ROLLBACK")
                    return

                if game["status"] == "settled":
                    con.execute("ROLLBACK")
                    return

                amount = D(
                    game["amount"]
                )

                user_id = int(
                    game["creator_id"]
                )

                con.execute("""
                UPDATE users
                SET balance=CAST(balance AS REAL)+?
                WHERE user_id=?
                """, (
                    str(amount),
                    user_id
                ))

                con.execute("""
                UPDATE games
                SET status='settled',
                    winner_id=NULL
                WHERE id=?
                """, (
                    game_id,
                ))

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "REFUND ERROR"
                )
                return

    try:
        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                "❌ اجرای بازی با ربات با مشکل مواجه شد.\n"
                f"💰 مبلغ {money(game['amount'])} TRX برگشت داده شد."
            )
        )
    except Exception:
        pass


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    if not await check_force_join(
        context,
        user.id
    ):
        await force_join_message(
            update,
            context
        )
        return

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

    # انتقال کاملاً اتمیک
    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                sender = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    user.id,
                )).fetchone()

                receiver = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    target.id,
                )).fetchone()

                if not sender or not receiver:
                    con.execute("ROLLBACK")

                    await message.reply_text(
                        "❌ کاربر پیدا نشد."
                    )
                    return

                sender_balance = D(
                    sender["balance"]
                )

                if sender_balance < amount:
                    con.execute("ROLLBACK")

                    await message.reply_text(
                        "❌ موجودی کافی نیست."
                    )
                    return

                receiver_balance = D(
                    receiver["balance"]
                )

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(sender_balance - amount),
                    user.id
                ))

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(receiver_balance + amount),
                    target.id
                ))

                con.execute("""
                INSERT INTO transfers
                (sender_id, receiver_id, amount)
                VALUES (?, ?, ?)
                """, (
                    user.id,
                    target.id,
                    str(amount)
                ))

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "TRANSFER ERROR"
                )

                await message.reply_text(
                    "❌ انتقال انجام نشد."
                )

                return

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

    if not withdraw_enabled():

        await update.effective_message.reply_text(
            "🔴 برداشت در حال حاضر خاموش است."
        )
        return

    await update.effective_message.reply_text(
        "📤 درخواست برداشت\n\n"
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

    if not withdraw_enabled():
        return False

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

    return True


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
        "تعداد پرتاب محدودیت ندارد."
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
                or (
                    "@" + row["username"]
                    if row["username"]
                    else str(row["user_id"])
                )
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
            f"💰 مجموع موجودی: {money(total)} TRX\n"
            f"🎮 بازی‌ها: {games:,}\n"
            f"📤 درخواست‌ها: {pending:,}\n\n"
            f"📥 برداشت: "
            f"{'روشن 🟢' if withdraw_enabled() else 'خاموش 🔴'}",
            reply_markup=admin_keyboard()
        )

        return

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

    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 0.5",
            reply_markup=admin_keyboard()
        )

        return

    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "/removebalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/removebalance 123456789 0.5",
            reply_markup=admin_keyboard()
        )

        return

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
                "📋 درخواست در انتظار وجود ندارد.",
                reply_markup=admin_keyboard()
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
            text[:4000],
            reply_markup=admin_keyboard()
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
# BLOCK / UNBLOCK
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
    # FORCE JOIN
    # ========================================================

    if not await check_force_join(
        context,
        user.id
    ):
        await force_join_message(
            update,
            context
        )
        return

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

            if not withdraw_enabled():

                context.user_data.clear()

                await message.reply_text(
                    "🔴 برداشت در حال حاضر خاموش است."
                )

                return

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

            if not withdraw_enabled():

                context.user_data.clear()

                await message.reply_text(
                    "🔴 برداشت خاموش شده است."
                )

                return

            ok = await create_request(
                user.id,
                amount,
                text
            )

            context.user_data.clear()

            await message.reply_text(
                "✅ درخواست ثبت شد."
                if ok
                else
                "❌ درخواست ثبت نشد."
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

    if re.match(
        r"^(انتقال|transfer)\s+\d+(?:[.,]\d+)?$",
        normalized,
        re.IGNORECASE
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

            if not withdraw_enabled():

                await message.reply_text(
                    "🔴 برداشت در حال حاضر خاموش است."
                )
                return

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
    # GAME MENU BUTTONS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            game_callback,
            pattern=r"^game_"
        )
    )

    # ========================================================
    # GAME ACTION BUTTONS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            join_friend,
            pattern=r"^join_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            join_bot,
            pattern=r"^bot_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game,
            pattern=r"^cancel_\d+$"
        )
    )

    # ========================================================
    # ADMIN
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # ========================================================
    # DICE / BOWLING / BASKETBALL / DARTS
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
