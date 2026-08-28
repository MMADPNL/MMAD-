# ============================================================
# BOT.PY - Telegram Games
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

# جوین اجباری
FORCE_JOIN_CHANNEL = "@zobxt"
FORCE_JOIN_LINK = "https://t.me/zobxt"

# مبالغ
MIN_AMOUNT = Decimal("0.01")
MAX_AMOUNT = Decimal("1000000000")

# تسویه
OWNER_SHARE = Decimal("0.02")
BOT_FEE = Decimal("0.03")
WINNER_PAYOUT = Decimal("0.95")

# زیرمجموعه
REFERRAL_REWARD = Decimal("0.05")

# برداشت
WITHDRAW_ENABLED_DEFAULT = 1

# حداکثر تعداد بازی فعال بررسی‌شده در هر گپ
ACTIVE_SCAN_LIMIT = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# قفل عمومی دیتابیس
DB_LOCK = asyncio.Lock()

# قفل هر بازی
GAME_LOCKS = {}

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
            referred_by INTEGER DEFAULT NULL,
            referral_reward_paid INTEGER DEFAULT 0,
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
            opponent_id INTEGER DEFAULT NULL,
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

        # ----------------------------
        # MIGRATIONS
        # ----------------------------

        user_columns = [
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        ]

        if "balance" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN balance TEXT DEFAULT '0'"
            )

        if "blocked" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0"
            )

        if "referred_by" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL"
            )

        if "referral_reward_paid" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN referral_reward_paid INTEGER DEFAULT 0"
            )

        game_columns = [
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        ]

        if "opponent_id" not in game_columns:
            con.execute(
                "ALTER TABLE games ADD COLUMN opponent_id INTEGER DEFAULT NULL"
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


def name_of(user):
    if not user:
        return "کاربر"

    if getattr(user, "first_name", None):
        return user.first_name

    if getattr(user, "username", None):
        return "@" + user.username

    if getattr(user, "id", None):
        return str(user.id)

    return "کاربر"


def name_from_row(row):
    if not row:
        return "کاربر"

    if row["first_name"]:
        return row["first_name"]

    if row["username"]:
        return "@" + row["username"]

    return str(row["user_id"])


# ============================================================
# BALANCE - ATOMIC
# ============================================================

def debit_balance(user_id, amount):
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


# ============================================================
# HOUSE
# ============================================================

def add_house(owner_amount, fee_amount):
    owner_amount = D(owner_amount)
    fee_amount = D(fee_amount)

    if owner_amount < 0:
        owner_amount = Decimal("0")

    if fee_amount < 0:
        fee_amount = Decimal("0")

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT owner_balance, fee_balance "
                "FROM house WHERE id=1"
            ).fetchone()

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
    text = text.strip()
    text = text.replace("٬", "")
    text = text.replace(",", ".")

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None

    if value < MIN_AMOUNT:
        return None

    if value > MAX_AMOUNT:
        return None

    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )


def parse_amount_from_command(text):
    text = normalize_digits(text or "")
    text = text.replace("٬", "")

    match = re.search(
        r"(-?\d+(?:[.,]\d+)?)",
        text
    )

    if not match:
        return None

    return parse_decimal_amount(
        match.group(1).replace(",", ".")
    )


def parse_game(text):
    """
    مثال:

    1 تاس 0.5
    2 تاس 0.5
    10 بولینگ 1
    100 بسکتبال 0.5
    3 دارت 0.25

    تعداد محدودیت ندارد.
    """

    text = normalize_digits(text or "").strip()

    pattern = re.compile(
        r"^(\d+)\s+([^\s]+)\s+(\d+(?:[.,]\d+)?)$",
        re.IGNORECASE
    )

    match = pattern.match(text)

    if not match:
        return None

    rounds = int(match.group(1))
    game_name = match.group(2).lower()

    if rounds < 1:
        return None

    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    amount = parse_decimal_amount(
        match.group(3).replace(",", ".")
    )

    if amount is None:
        return None

    return game, rounds, amount


# ============================================================
# FORCE JOIN
# ============================================================

async def is_joined_channel(context, user_id):
    try:

        member = await context.bot.get_chat_member(
            chat_id=FORCE_JOIN_CHANNEL,
            user_id=user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception as e:

        logger.error(
            "FORCE JOIN CHECK ERROR: %s",
            e
        )

        return False


async def force_join(update, context):
    user = update.effective_user

    if not user:
        return False

    joined = await is_joined_channel(
        context,
        user.id
    )

    if joined:
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url=FORCE_JOIN_LINK
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_join"
            )
        ]
    ])

    try:
        await update.effective_message.reply_text(
            "⛔ برای استفاده از ربات ابتدا باید در کانال عضو شوی.\n\n"
            "بعد از عضویت روی «بررسی عضویت» بزن.",
            reply_markup=keyboard
        )
    except Exception:
        pass

    return False


async def check_join_callback(update, context):
    query = update.callback_query

    user = query.from_user

    joined = await is_joined_channel(
        context,
        user.id
    )

    if not joined:

        await query.answer(
            "❌ هنوز در کانال عضو نیستی.",
            show_alert=True
        )

        return

    await query.answer(
        "✅ عضویت تأیید شد."
    )

    try:
        await query.message.delete()
    except Exception:
        pass

    ensure_user(user)

    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=(
            "👋 سلام!\n\n"
            "🎮 به ربات بازی خوش آمدی.\n\n"
            "💰 موجودی: TRX\n\n"
            "از منوی زیر استفاده کن."
        ),
        reply_markup=user_keyboard()
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
# START
# ============================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    if not await force_join(update, context):
        return

    ensure_user(user)

    # ثبت زیرمجموعه از /start REF_ID
    if context.args:

        try:
            ref_id = int(
                normalize_digits(
                    context.args[0]
                )
            )

            if (
                ref_id != user.id
                and
                get_user(ref_id)
            ):

                with closing(db()) as con:

                    row = con.execute("""
                    SELECT referred_by
                    FROM users
                    WHERE user_id=?
                    """, (
                        user.id,
                    )).fetchone()

                    if row and row["referred_by"] is None:

                        con.execute("""
                        UPDATE users
                        SET referred_by=?
                        WHERE user_id=?
                        """, (
                            ref_id,
                            user.id
                        ))

                        con.commit()

        except Exception:
            pass

    if is_blocked(user.id):

        await update.effective_message.reply_text(
            "⛔ دسترسی شما مسدود است."
        )

        return

    await update.effective_message.reply_text(
        "👋 سلام!\n\n"
        "🎮 به ربات بازی خوش آمدی.\n\n"
        "💰 موجودی: TRX\n\n"
        "از منوی زیر استفاده کن.",
        reply_markup=user_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):

    user = update.effective_user

    if not user:
        return

    if not await force_join(update, context):
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    balance = get_balance(user.id)

    await update.effective_message.reply_text(
        f"💰 موجودی {name_of(user)}:\n\n"
        f"💎 {money(balance)} TRX"
    )


# ============================================================
# GAME MENUS
# ============================================================

async def game_menu(update, context):

    if not await force_join(update, context):
        return

    await update.effective_message.reply_text(
        "🎮 بازی را انتخاب کن:",
        reply_markup=game_keyboard()
    )


async def friends_menu(update, context):

    if not await force_join(update, context):
        return

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
        "تعداد بازی محدودیت ندارد."
    )


async def bot_menu(update, context):

    if not await force_join(update, context):
        return

    await update.effective_message.reply_text(
        "🤖 بازی با ربات\n\n"
        "مثال:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "1 بسکتبال 0.5\n"
        "1 دارت 0.5\n\n"
        "اول خودت بازی را بساز.\n"
        "بعد از شروع، خودت ایموجی بازی را می‌فرستی و "
        "بعد ربات خودش می‌اندازد."
    )


# ============================================================
# GAME LOCK
# ============================================================

def get_game_lock(game_id):
    if game_id not in GAME_LOCKS:
        GAME_LOCKS[game_id] = asyncio.Lock()

    return GAME_LOCKS[game_id]


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

    if not await force_join(update, context):
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    # ----------------------------
    # رزرو اتمیک مبلغ
    # ----------------------------

    if not debit_balance(
        user.id,
        amount
    ):

        await message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {money(get_balance(user.id))} TRX"
        )

        return

    game_id = None

    try:

        # اول بازی در DB ساخته می‌شود
        # تا callback هیچ‌وقت game_id=0 نداشته باشد.

        with closing(db()) as con:

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
            con.commit()

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
            reply_markup=game_created_keyboard(game_id)
        )

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET message_id=?
            WHERE id=?
            """, (
                sent.message_id,
                game_id
            ))

            con.commit()

    except Exception:

        logger.exception(
            "CREATE GAME ERROR"
        )

        if game_id:

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET status='cancelled'
                WHERE id=?
                AND status='waiting'
                """, (
                    game_id,
                ))

                con.commit()

        credit_balance(
            user.id,
            amount
        )

        await message.reply_text(
            "❌ بازی ساخته نشد؛ مبلغ برگشت داده شد."
        )


# ============================================================
# GAME CALLBACK
# ============================================================

async def game_callback(update, context):

    query = update.callback_query

    user = query.from_user

    if not await is_joined_channel(
        context,
        user.id
    ):

        await query.answer(
            "❌ ابتدا در کانال عضو شو.",
            show_alert=True
        )

        return

    await query.answer()

    game = query.data.replace(
        "game_",
        "",
        1
    )

    if game not in GAME_LABELS:
        return

    game_word = GAME_LABELS[game].split(
        " ",
        1
    )[1]

    await query.message.reply_text(
        f"{GAME_LABELS[game]}\n\n"
        f"مثال:\n"
        f"1 {game_word} 0.5\n"
        f"2 {game_word} 1\n\n"
        f"تعداد نامحدود است."
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


def update_game(
    game_id,
    **fields
):

    if not fields:
        return

    allowed = {
        "creator_rolls",
        "opponent_rolls",
        "status",
        "winner_id",
        "opponent_id",
        "message_id"
    }

    safe = {
        key: value
        for key, value in fields.items()
        if key in allowed
    }

    if not safe:
        return

    columns = ", ".join(
        f"{key}=?"
        for key in safe
    )

    values = list(
        safe.values()
    )

    values.append(game_id)

    with closing(db()) as con:

        con.execute(
            f"""
            UPDATE games
            SET {columns}
            WHERE id=?
            """,
            values
        )

        con.commit()


def parse_rolls(value):
    if not value:
        return []

    result = []

    for item in value.split(","):

        try:
            result.append(
                int(item)
            )

        except Exception:
            pass

    return result


def serialize_rolls(rolls):
    return ",".join(
        str(int(x))
        for x in rolls
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_friend(update, context):

    query = update.callback_query
    user = query.from_user

    if not await is_joined_channel(
        context,
        user.id
    ):

        await query.answer(
            "❌ ابتدا در کانال عضو شو.",
            show_alert=True
        )

        return

    await query.answer()

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    ensure_user(user)

    lock = get_game_lock(
        game_id
    )

    async with lock:

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

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

                    amount = D(
                        game["amount"]
                    )

                    row = con.execute(
                        """
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """,
                        (user.id,)
                    ).fetchone()

                    if (
                        not row
                        or
                        D(row["balance"]) < amount
                    ):

                        con.execute("ROLLBACK")

                        await query.answer(
                            "❌ موجودی کافی نیست.",
                            show_alert=True
                        )

                        return

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

                    if con.total_changes <= 0:

                        con.execute("ROLLBACK")

                        # اگر وضعیت همزمان تغییر کرد،
                        # مبلغ دوباره کسر نمی‌شود.
                        await query.answer(
                            "❌ بازی قبلاً شروع شده.",
                            show_alert=True
                        )

                        return

                    con.execute("COMMIT")

                except Exception:

                    try:
                        con.execute(
                            "ROLLBACK"
                        )
                    except Exception:
                        pass

                    logger.exception(
                        "JOIN FRIEND ERROR"
                    )

                    await query.answer(
                        "❌ خطا؛ دوباره تلاش کن.",
                        show_alert=True
                    )

                    return

        try:
            await query.message.edit_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        creator_name = "سازنده"

        creator = get_user(
            creator_id
        )

        if creator:
            creator_name = name_from_row(
                creator
            )

        await query.message.reply_text(
            f"👥 حریف وارد شد: {name_of(user)}\n\n"
            f"🎯 اول {creator_name} باید خودش "
            f"{game['rounds']} بار "
            f"{GAME_EMOJIS[game['game_type']]} بفرستد."
        )


# ============================================================
# BOT GAME
# ============================================================

async def join_bot(update, context):

    query = update.callback_query
    user = query.from_user

    if not await is_joined_channel(
        context,
        user.id
    ):

        await query.answer(
            "❌ ابتدا در کانال عضو شو.",
            show_alert=True
        )

        return

    await query.answer()

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    ensure_user(user)

    lock = get_game_lock(
        game_id
    )

    async with lock:

        async with DB_LOCK:

            with closing(db()) as con:

                try:

                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

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
                            "❌ این بازی دیگر قابل شروع نیست.",
                            show_alert=True
                        )

                        return

                    if int(
                        game["creator_id"]
                    ) != user.id:

                        con.execute("ROLLBACK")

                        await query.answer(
                            "❌ فقط سازنده بازی می‌تواند با ربات بازی کند.",
                            show_alert=True
                        )

                        return

                    # فقط وضعیت عوض می‌شود.
                    # مبلغ قبلاً هنگام ساخت بازی رزرو شده.
                    con.execute("""
                    UPDATE games
                    SET status='bot_creator_turn',
                        opponent_id=NULL
                    WHERE id=?
                    AND status='waiting'
                    """, (
                        game_id,
                    ))

                    if con.total_changes <= 0:

                        con.execute(
                            "ROLLBACK"
                        )

                        await query.answer(
                            "❌ بازی قبلاً شروع شده.",
                            show_alert=True
                        )

                        return

                    con.execute(
                        "COMMIT"
                    )

                except Exception:

                    try:
                        con.execute(
                            "ROLLBACK"
                        )
                    except Exception:
                        pass

                    logger.exception(
                        "BOT GAME START ERROR"
                    )

                    await query.answer(
                        "❌ خطا؛ دوباره تلاش کن.",
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
            f"🤖 بازی با ربات شروع شد.\n\n"
            f"👤 {name_of(user)}\n\n"
            f"اول خودت {game['rounds']} بار "
            f"{GAME_EMOJIS[game['game_type']]} بفرست.\n\n"
            f"بعد از کامل شدن پرتاب‌های تو، "
            f"ربات خودش پرتاب می‌کند."
        )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query
    user = query.from_user

    if not await is_joined_channel(
        context,
        user.id
    ):

        await query.answer(
            "❌ ابتدا در کانال عضو شو.",
            show_alert=True
        )

        return

    await query.answer()

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    lock = get_game_lock(
        game_id
    )

    async with lock:

        async with DB_LOCK:

            with closing(db()) as con:

                try:

                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

                    game = con.execute(
                        "SELECT * FROM games WHERE id=?",
                        (game_id,)
                    ).fetchone()

                    if not game:

                        con.execute(
                            "ROLLBACK"
                        )

                        await query.answer(
                            "❌ بازی پیدا نشد.",
                            show_alert=True
                        )

                        return

                    if game["status"] != "waiting":

                        con.execute(
                            "ROLLBACK"
                        )

                        await query.answer(
                            "❌ بازی شروع شده و قابل لغو نیست.",
                            show_alert=True
                        )

                        return

                    if int(
                        game["creator_id"]
                    ) != user.id:

                        con.execute(
                            "ROLLBACK"
                        )

                        await query.answer(
                            "❌ فقط سازنده می‌تواند لغو کند.",
                            show_alert=True
                        )

                        return

                    amount = D(
                        game["amount"]
                    )

                    row = con.execute(
                        """
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """,
                        (user.id,)
                    ).fetchone()

                    if row:

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
                    AND status='waiting'
                    """, (
                        game_id,
                    ))

                    con.execute(
                        "COMMIT"
                    )

                except Exception:

                    try:
                        con.execute(
                            "ROLLBACK"
                        )
                    except Exception:
                        pass

                    logger.exception(
                        "CANCEL ERROR"
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
# CALLBACK ROUTER
# ============================================================

async def game_action_callback(update, context):

    data = update.callback_query.data

    if data.startswith("join_"):
        await join_friend(
            update,
            context
        )
        return

    if data.startswith("bot_"):
        await join_bot(
            update,
            context
        )
        return

    if data.startswith("cancel_"):
        await cancel_game(
            update,
            context
        )
        return


# ============================================================
# ACTIVE GAME SEARCH
# ============================================================

def find_active_game(chat_id, user_id):
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
        ORDER BY id DESC
        LIMIT ?
        """, (
            chat_id,
            ACTIVE_SCAN_LIMIT
        )).fetchall()

    for row in rows:

        creator_id = int(
            row["creator_id"]
        )

        opponent_id = (
            int(row["opponent_id"])
            if row["opponent_id"] is not None
            else None
        )

        if row["status"] == "bot_creator_turn":

            if user_id == creator_id:
                return row

        elif row["status"] == "creator_turn":

            if user_id == creator_id:
                return row

        elif row["status"] == "opponent_turn":

            if opponent_id == user_id:
                return row

    return None


# ============================================================
# VALID GAME DICE
# ============================================================

def valid_game_roll(
    game_type,
    dice
):

    if not dice:
        return False

    return dice.emoji == GAME_EMOJIS[
        game_type
    ]


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

    if not await is_joined_channel(
        context,
        user.id
    ):
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    game = find_active_game(
        message.chat_id,
        user.id
    )

    if not game:

        # هیچ بازی فعال برای این کاربر نیست.
        return

    game_id = int(
        game["id"]
    )

    lock = get_game_lock(
        game_id
    )

    async with lock:

        # بازی را دوباره می‌خوانیم
        # تا اطلاعات قدیمی استفاده نشود.
        game = get_game(
            game_id
        )

        if not game:
            return

        status = game["status"]

        if status not in (
            "bot_creator_turn",
            "creator_turn",
            "opponent_turn"
        ):
            return

        game_type = game["game_type"]

        if not valid_game_roll(
            game_type,
            dice
        ):

            await message.reply_text(
                f"❌ برای این بازی باید "
                f"{GAME_EMOJIS[game_type]} بفرستی."
            )

            return

        rounds = int(
            game["rounds"]
        )

        creator_rolls = parse_rolls(
            game["creator_rolls"]
        )

        opponent_rolls = parse_rolls(
            game["opponent_rolls"]
        )

        # ====================================================
        # BOT GAME - USER
        # ====================================================

        if status == "bot_creator_turn":

            if user.id != int(
                game["creator_id"]
            ):
                return

            if len(creator_rolls) >= rounds:
                return

            creator_rolls.append(
                int(dice.value)
            )

            update_game(
                game_id,
                creator_rolls=serialize_rolls(
                    creator_rolls
                )
            )

            remaining = (
                rounds
                - len(creator_rolls)
            )

            await message.reply_text(
                f"👤 {name_of(user)}: {dice.value}\n"
                f"🎯 امتیاز فعلی: "
                f"{calculate_score(creator_rolls)}"
            )

            if remaining > 0:

                await message.reply_text(
                    f"🎯 {remaining} پرتاب دیگر باقی مانده."
                )

                return

            # ------------------------------------------------
            # قفل وضعیت قبل از اجرای ربات
            # ------------------------------------------------

            update_game(
                game_id,
                status="bot_rolling"
            )

            await message.reply_text(
                f"🤖 {GAME_LABELS[game_type]} ربات شروع شد..."
            )

            bot_rolls = []

            try:

                for index in range(rounds):

                    # اگر task در فاصله پرتاب‌ها قطع شد،
                    # نتیجه بازی نیمه‌کاره نمی‌ماند.
                    current = get_game(
                        game_id
                    )

                    if not current:
                        return

                    if current["status"] != "bot_rolling":
                        return

                    sent = await context.bot.send_dice(
                        chat_id=message.chat_id,
                        emoji=GAME_EMOJIS[game_type]
                    )

                    if not sent or not sent.dice:
                        raise RuntimeError(
                            "Bot dice message failed"
                        )

                    value = int(
                        sent.dice.value
                    )

                    bot_rolls.append(
                        value
                    )

                    await asyncio.sleep(
                        0.8
                    )

                update_game(
                    game_id,
                    opponent_rolls=serialize_rolls(
                        bot_rolls
                    ),
                    status="finished"
                )

                await finish_bot_game(
                    context,
                    get_game(game_id),
                    creator_rolls,
                    bot_rolls
                )

            except Exception:

                logger.exception(
                    "BOT ROLL ERROR"
                )

                # مبلغ کاربر در خطای فنی برگردانده می‌شود.
                current = get_game(
                    game_id
                )

                if current and current["status"] != "refunded":

                    amount = D(
                        current["amount"]
                    )

                    update_game(
                        game_id,
                        status="refunded"
                    )

                    credit_balance(
                        int(
                            current["creator_id"]
                        ),
                        amount
                    )

                    await message.reply_text(
                        "⚠️ ربات هنگام انداختن بازی با خطا مواجه شد.\n\n"
                        "💰 مبلغ بازی به شما برگشت داده شد."
                    )

            return

        # ====================================================
        # FRIEND - CREATOR
        # ====================================================

        if status == "creator_turn":

            if user.id != int(
                game["creator_id"]
            ):
                return

            if len(creator_rolls) >= rounds:
                return

            creator_rolls.append(
                int(dice.value)
            )

            if len(creator_rolls) < rounds:

                update_game(
                    game_id,
                    creator_rolls=serialize_rolls(
                        creator_rolls
                    )
                )

                remaining = (
                    rounds
                    - len(creator_rolls)
                )

                await message.reply_text(
                    f"👤 {name_of(user)}: {dice.value}\n"
                    f"🎯 {remaining} پرتاب باقی مانده."
                )

                return

            update_game(
                game_id,
                creator_rolls=serialize_rolls(
                    creator_rolls
                ),
                status="opponent_turn"
            )

            opponent = get_user(
                int(game["opponent_id"])
            )

            opponent_name = (
                name_from_row(opponent)
                if opponent
                else
                str(game["opponent_id"])
            )

            await message.reply_text(
                f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
                f"🎯 حالا {opponent_name} خودش "
                f"{rounds} بار "
                f"{GAME_EMOJIS[game_type]} بفرستد."
            )

            return

        # ====================================================
        # FRIEND - OPPONENT
        # ====================================================

        if status == "opponent_turn":

            opponent_id = int(
                game["opponent_id"]
            )

            if user.id != opponent_id:
                return

            if len(opponent_rolls) >= rounds:
                return

            opponent_rolls.append(
                int(dice.value)
            )

            if len(opponent_rolls) < rounds:

                update_game(
                    game_id,
                    opponent_rolls=serialize_rolls(
                        opponent_rolls
                    )
                )

                remaining = (
                    rounds
                    - len(opponent_rolls)
                )

                await message.reply_text(
                    f"👤 {name_of(user)}: {dice.value}\n"
                    f"🎯 {remaining} پرتاب باقی مانده."
                )

                return

            update_game(
                game_id,
                opponent_rolls=serialize_rolls(
                    opponent_rolls
                ),
                status="settling"
            )

            await finish_friend_game(
                context,
                get_game(game_id),
                creator_rolls,
                opponent_rolls
            )


# ============================================================
# SCORE
# ============================================================

def calculate_score(rolls):
    return sum(
        int(x)
        for x in rolls
    )


def winner_from_scores(
    score1,
    score2
):

    if score1 > score2:
        return 1

    if score2 > score1:
        return 2

    return 0


# ============================================================
# FINISH FRIEND
# ============================================================

async def finish_friend_game(
    context,
    game,
    creator_rolls,
    opponent_rolls
):

    if not game:
        return

    game_id = int(
        game["id"]
    )

    lock = get_game_lock(
        game_id
    )

    async with lock:

        # دوباره وضعیت را چک می‌کنیم
        current = get_game(
            game_id
        )

        if not current:
            return

        if current["status"] not in (
            "settling",
        ):
            return

        creator_id = int(
            current["creator_id"]
        )

        opponent_id = int(
            current["opponent_id"]
        )

        amount = D(
            current["amount"]
        )

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

        # ====================================================
        # DRAW
        # ====================================================

        if result == 0:

            with closing(db()) as con:

                try:

                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

                    row1 = con.execute(
                        "SELECT balance FROM users WHERE user_id=?",
                        (creator_id,)
                    ).fetchone()

                    row2 = con.execute(
                        "SELECT balance FROM users WHERE user_id=?",
                        (opponent_id,)
                    ).fetchone()

                    if not row1 or not row2:

                        con.execute(
                            "ROLLBACK"
                        )

                        return

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(
                            D(row1["balance"])
                            + amount
                        ),
                        creator_id
                    ))

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(
                            D(row2["balance"])
                            + amount
                        ),
                        opponent_id
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=NULL
                    WHERE id=?
                    AND status='settling'
                    """, (
                        game_id,
                    ))

                    con.execute(
                        "COMMIT"
                    )

                except Exception:

                    try:
                        con.execute(
                            "ROLLBACK"
                        )
                    except Exception:
                        pass

                    logger.exception(
                        "DRAW SETTLEMENT ERROR"
                    )

                    return

            await context.bot.send_message(
                chat_id=current["chat_id"],
                text=(
                    f"🤝 مساوی شد!\n\n"
                    f"👤 {name_from_row(get_user(creator_id))}: "
                    f"{score_creator}\n"
                    f"👤 {name_from_row(get_user(opponent_id))}: "
                    f"{score_opponent}\n\n"
                    f"💰 مبلغ {money(amount)} TRX "
                    f"به هر دو نفر برگشت داده شد."
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

        # کل دو سهم بازی
        total = amount * Decimal("2")

        payout = (
            total
            - OWNER_SHARE
            - BOT_FEE
        )

        if payout < 0:
            payout = Decimal("0")

        # ====================================================
        # اتمیک SETTLEMENT
        # ====================================================

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                # اول مطمئن می‌شویم بازی هنوز settle نشده
                check = con.execute("""
                SELECT status
                FROM games
                WHERE id=?
                """, (
                    game_id,
                )).fetchone()

                if not check or check["status"] != "settling":

                    con.execute(
                        "ROLLBACK"
                    )

                    return

                row = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (winner_id,)
                ).fetchone()

                if not row:

                    con.execute(
                        "ROLLBACK"
                    )

                    return

                new_balance = (
                    D(row["balance"])
                    + payout
                )

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(new_balance),
                    winner_id
                ))

                house = con.execute("""
                SELECT owner_balance,
                       fee_balance
                FROM house
                WHERE id=1
                """).fetchone()

                if not house:

                    con.execute(
                        "ROLLBACK"
                    )

                    return

                owner_new = (
                    D(house["owner_balance"])
                    + OWNER_SHARE
                )

                fee_new = (
                    D(house["fee_balance"])
                    + BOT_FEE
                )

                con.execute("""
                UPDATE house
                SET owner_balance=?,
                    fee_balance=?
                WHERE id=1
                """, (
                    str(owner_new),
                    str(fee_new)
                ))

                con.execute("""
                UPDATE games
                SET status='finished',
                    winner_id=?
                WHERE id=?
                AND status='settling'
                """, (
                    winner_id,
                    game_id
                ))

                con.execute(
                    "COMMIT"
                )

            except Exception:

                try:
                    con.execute(
                        "ROLLBACK"
                    )
                except Exception:
                    pass

                logger.exception(
                    "FRIEND SETTLEMENT ERROR"
                )

                return

        winner_user = get_user(
            winner_id
        )

        loser_user = get_user(
            loser_id
        )

        await context.bot.send_message(
            chat_id=current["chat_id"],
            text=(
                f"🏆 نتیجه بازی\n\n"
                f"👤 {name_from_row(get_user(creator_id))}: "
                f"{score_creator}\n"
                f"👤 {name_from_row(get_user(opponent_id))}: "
                f"{score_opponent}\n\n"
                f"🏆 برنده: {name_from_row(winner_user)}\n"
                f"🎯 امتیاز برنده: {winner_score}\n"
                f"🎯 امتیاز حریف: {loser_score}\n\n"
                f"💰 دریافتی برنده: {money(payout)} TRX"
            )
        )


# ============================================================
# FINISH BOT
# ============================================================

async def finish_bot_game(
    context,
    game,
    user_rolls,
    bot_rolls
):

    if not game:
        return

    game_id = int(
        game["id"]
    )

    lock = get_game_lock(
        game_id
    )

    async with lock:

        current = get_game(
            game_id
        )

        if not current:
            return

        if current["status"] != "finished":
            return

        user_id = int(
            current["creator_id"]
        )

        amount = D(
            current["amount"]
        )

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

        # ====================================================
        # DRAW
        # ====================================================

        if result == 0:

            # بازی هنوز پرداخت نشده.
            # وضعیت را به settlement می‌بریم.
            update_game(
                game_id,
                status="bot_settling"
            )

            with closing(db()) as con:

                try:

                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

                    row = con.execute(
                        "SELECT balance FROM users WHERE user_id=?",
                        (user_id,)
                    ).fetchone()

                    if not row:

                        con.execute(
                            "ROLLBACK"
                        )

                        return

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(
                            D(row["balance"])
                            + amount
                        ),
                        user_id
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=NULL
                    WHERE id=?
                    AND status='bot_settling'
                    """, (
                        game_id,
                    ))

                    con.execute(
                        "COMMIT"
                    )

                except Exception:

                    try:
                        con.execute(
                            "ROLLBACK"
                        )
                    except Exception:
                        pass

                    logger.exception(
                        "BOT DRAW ERROR"
                    )

                    return

            await context.bot.send_message(
                chat_id=current["chat_id"],
                text=(
                    f"🤝 مساوی شد!\n\n"
                    f"👤 {name_from_row(get_user(user_id))}: "
                    f"{user_score}\n"
                    f"🤖 ربات: {bot_score}\n\n"
                    f"💰 مبلغ {money(amount)} TRX "
                    f"برگشت داده شد."
                )
            )

            return

        # ====================================================
        # USER WIN
        # ====================================================

        if result == 1:

            payout = (
                amount * Decimal("2")
                - OWNER_SHARE
                - BOT_FEE
            )

            if payout < 0:
                payout = Decimal("0")

            update_game(
                game_id,
                status="bot_settling"
            )

            with closing(db()) as con:

                try:

                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

                    row = con.execute(
                        "SELECT balance FROM users WHERE user_id=?",
                        (user_id,)
                    ).fetchone()

                    house = con.execute("""
                    SELECT owner_balance,
                           fee_balance
                    FROM house
                    WHERE id=1
                    """).fetchone()

                    if not row or not house:

                        con.execute(
                            "ROLLBACK"
                        )

                        return

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(
                            D(row["balance"])
                            + payout
                        ),
                        user_id
                    ))

                    con.execute("""
                    UPDATE house
                    SET owner_balance=?,
                        fee_balance=?
                    WHERE id=1
                    """, (
                        str(
                            D(house["owner_balance"])
                            + OWNER_SHARE
                        ),
                        str(
                            D(house["fee_balance"])
                            + BOT_FEE
                        )
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=?
                    WHERE id=?
                    AND status='bot_settling'
                    """, (
                        user_id,
                        game_id
                    ))

                    con.execute(
                        "COMMIT"
                    )

                except Exception:

                    try:
                        con.execute(
                            "ROLLBACK"
                        )
                    except Exception:
                        pass

                    logger.exception(
                        "BOT USER WIN ERROR"
                    )

                    return

            await context.bot.send_message(
                chat_id=current["chat_id"],
                text=(
                    f"🏆 نتیجه بازی\n\n"
                    f"👤 {name_from_row(get_user(user_id))}: "
                    f"{user_score}\n"
                    f"🤖 ربات: {bot_score}\n\n"
                    f"🏆 برنده: {name_from_row(get_user(user_id))}\n"
                    f"🎯 امتیاز برنده: {user_score}\n\n"
                    f"💰 دریافتی: {money(payout)} TRX"
                )
            )

            return

        # ====================================================
        # BOT WIN
        # ====================================================

        update_game(
            game_id,
            status="bot_settling"
        )

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                house = con.execute("""
                SELECT owner_balance
                FROM house
                WHERE id=1
                """).fetchone()

                if not house:

                    con.execute(
                        "ROLLBACK"
                    )

                    return

                con.execute("""
                UPDATE house
                SET owner_balance=?
                WHERE id=1
                """, (
                    str(
                        D(house["owner_balance"])
                        + amount
                    ),
                ))

                con.execute("""
                UPDATE games
                SET status='finished',
                    winner_id=NULL
                WHERE id=?
                AND status='bot_settling'
                """, (
                    game_id,
                ))

                con.execute(
                    "COMMIT"
                )

            except Exception:

                try:
                    con.execute(
                        "ROLLBACK"
                    )
                except Exception:
                    pass

                logger.exception(
                    "BOT WIN ERROR"
                )

                return

        await context.bot.send_message(
            chat_id=current["chat_id"],
            text=(
                f"🏆 نتیجه بازی\n\n"
                f"👤 {name_from_row(get_user(user_id))}: "
                f"{user_score}\n"
                f"🤖 ربات: {bot_score}\n\n"
                f"🏆 برنده: 🤖 ربات\n"
                f"🎯 امتیاز برنده: {bot_score}\n"
                f"🎯 امتیاز شما: {user_score}"
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

    if not await force_join(
        update,
        context
    ):
        return

    ensure_user(user)

    if not message.reply_to_message:

        await message.reply_text(
            "💸 برای انتقال روی پیام کاربر Reply کن.\n\n"
            "مثال:\n"
            "انتقال 0.5"
        )

        return

    target = (
        message.reply_to_message.from_user
    )

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

    async with DB_LOCK:

        with closing(db()) as con:

            try:

                con.execute(
                    "BEGIN IMMEDIATE"
                )

                sender = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (user.id,)
                ).fetchone()

                receiver = con.execute(
                    "SELECT balance FROM users WHERE user_id=?",
                    (target.id,)
                ).fetchone()

                if not sender or not receiver:

                    con.execute(
                        "ROLLBACK"
                    )

                    await message.reply_text(
                        "❌ کاربر پیدا نشد."
                    )

                    return

                sender_balance = D(
                    sender["balance"]
                )

                if sender_balance < amount:

                    con.execute(
                        "ROLLBACK"
                    )

                    await message.reply_text(
                        "❌ موجودی کافی نیست."
                    )

                    return

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(
                        sender_balance
                        - amount
                    ),
                    user.id
                ))

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(
                        D(receiver["balance"])
                        + amount
                    ),
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

                con.execute(
                    "COMMIT"
                )

            except Exception:

                try:
                    con.execute(
                        "ROLLBACK"
                    )
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

    if not await force_join(
        update,
        context
    ):
        return

    user = update.effective_user

    ensure_user(user)

    await update.effective_message.reply_text(
        "📤 درخواست\n\n"
        "مثال:\n"
        "درخواست 5\n\n"
        "بعد اطلاعات درخواست را ارسال کن."
    )

    context.user_data[
        "request_mode"
    ] = "amount"


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

    if not await force_join(
        update,
        context
    ):
        return

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
        "تعداد بازی محدودیت ندارد."
    )


# ============================================================
# REFERRAL
# ============================================================

async def referral_command(update, context):

    user = update.effective_user

    if not user:
        return

    if not await force_join(
        update,
        context
    ):
        return

    ensure_user(user)

    bot_username = context.bot.username

    if not bot_username:

        await update.message.reply_text(
            "❌ لینک زیرمجموعه ساخته نشد."
        )

        return

    link = (
        f"https://t.me/{bot_username}"
        f"?start={user.id}"
    )

    with closing(db()) as con:

        row = con.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE referred_by=?
        """, (
            user.id,
        )).fetchone()

    count = int(
        row[0]
    )

    await update.message.reply_text(
        "👥 زیرمجموعه\n\n"
        f"🔗 لینک شما:\n{link}\n\n"
        f"👥 تعداد زیرمجموعه: {count}\n"
        f"💰 پاداش هر زیرمجموعه: "
        f"{money(REFERRAL_REWARD)} TRX"
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    if not await force_join(
        update,
        context
    ):
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

    if request_mode == "wallet":

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

    parsed = parse_game(
        normalized
    )

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
    # FRIEND
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

            context.user_data[
                "request_amount"
            ] = amount

            context.user_data[
                "request_mode"
            ] = "wallet"

            await message.reply_text(
                "📝 مقدار ثبت شد.\n\n"
                "حالا اطلاعات درخواست را بفرست."
            )

        return

    # ========================================================
    # REFERRAL
    # ========================================================

    if text in (
        "زیرمجموعه",
        "زیر مجموعه",
        "👥 زیرمجموعه",
        "/referral"
    ):

        await referral_command(
            update,
            context
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
# ADMIN
# ============================================================

async def admin(update, context):

    user = update.effective_user

    if not user or not is_admin(
        user.id
    ):

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

    if not is_admin(
        user.id
    ):

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

        for index, row in enumerate(
            rows,
            1
        ):

            name = name_from_row(
                row
            )

            status = (
                "🚫"
                if row["blocked"]
                else
                "✅"
            )

            text += (
                f"{index}. {status} {name}\n"
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
                "SELECT SUM(CAST(balance AS REAL)) "
                "FROM users"
            ).fetchone()[0] or 0

            games = con.execute(
                "SELECT COUNT(*) FROM games"
            ).fetchone()[0]

            pending = con.execute(
                "SELECT COUNT(*) FROM requests "
                "WHERE status='pending'"
            ).fetchone()[0]

            house = con.execute("""
            SELECT owner_balance,
                   fee_balance
            FROM house
            WHERE id=1
            """).fetchone()

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"💰 مجموع موجودی: "
            f"{money(total)} TRX\n"
            f"🎮 بازی‌ها: {games:,}\n"
            f"📤 درخواست‌ها: {pending:,}\n\n"
            f"👑 سهم مالک: "
            f"{money(house['owner_balance'])} TRX\n"
            f"🧾 کارمزد: "
            f"{money(house['fee_balance'])} TRX\n\n"
            f"📥 برداشت: "
            f"{'روشن 🟢' if withdraw_enabled() else 'خاموش 🔴'}"
        )

        return

    # ========================================================
    # WITHDRAW
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

    if not user or not is_admin(
        user.id
    ):
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

    if not get_user(
        target_id
    ):

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    if not credit_balance(
        target_id,
        amount
    ):

        await update.message.reply_text(
            "❌ افزایش موجودی انجام نشد."
        )

        return

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

    if not user or not is_admin(
        user.id
    ):
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

    if not get_user(
        target_id
    ):

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

    if not user or not is_admin(
        user.id
    ):
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


# ============================================================
# UNBLOCK
# ============================================================

async def unblock(update, context):

    user = update.effective_user

    if not user or not is_admin(
        user.id
    ):
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
# GLOBAL ERROR
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
            "referral",
            referral_command
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
    # FORCE JOIN
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            check_join_callback,
            pattern=r"^check_join$"
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
