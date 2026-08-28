# ============================================================
# BOT.PY - Telegram Games Bot
# Python 3.10+
# python-telegram-bot 20+
#
# Features:
# - Dice / Bowling / Basketball / Darts
# - Unlimited rounds
# - Play vs Bot
# - Play vs Friend
# - Atomic balance operations
# - Game locking
# - Recovery for unfinished games
# - Reply transfer
# - Balance in groups
# - Referral system: 0.05 internal game credit
# - Mandatory membership: @zobxt
# - Bot errors do NOT stop polling
# - No initial balance
# ============================================================

import os
import re
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from contextlib import closing
from datetime import datetime

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.constants import ChatType
from telegram.error import TelegramError, BadRequest
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

CHANNEL_USERNAME = "@zobxt"
CHANNEL_LINK = "https://t.me/zobxt"

MIN_GAME = Decimal("0.01")
MAX_GAME = Decimal("1000000000")

REFERRAL_REWARD = Decimal("0.05")

# درصدهای تسویه داخلی بازی
OWNER_SHARE = Decimal("0.02")
BOT_FEE = Decimal("0.03")
WINNER_PAYOUT_RATE = Decimal("0.95")

STALE_GAME_HOURS = 24

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("games_bot")

# قفل سراسری برای عملیات حساس
DB_LOCK = asyncio.Lock()

# قفل جداگانه برای هر بازی
GAME_LOCKS = {}
GAME_LOCKS_GUARD = asyncio.Lock()


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
        isolation_level=None,
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
            referral_paid INTEGER DEFAULT 0,
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
            sender_id INTEGER NOT NULL,
            receiver_id INTEGER NOT NULL,
            amount TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            amount TEXT NOT NULL,
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
            amount TEXT NOT NULL,
            rounds INTEGER NOT NULL,
            creator_rolls TEXT DEFAULT '',
            opponent_rolls TEXT DEFAULT '',
            status TEXT DEFAULT 'waiting',
            winner_id INTEGER DEFAULT NULL,
            settled INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS house (
            id INTEGER PRIMARY KEY CHECK(id=1),
            owner_balance TEXT DEFAULT '0',
            fee_balance TEXT DEFAULT '0',
            bot_balance TEXT DEFAULT '0'
        )
        """)

        con.execute("""
        INSERT OR IGNORE INTO house
        (id, owner_balance, fee_balance, bot_balance)
        VALUES (1, '0', '0', '0')
        """)

        con.execute("""
        INSERT OR IGNORE INTO settings(key, value)
        VALUES ('withdraw_enabled', '1')
        """)

        # ----------------------------------------------------
        # Migration
        # ----------------------------------------------------

        columns = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        migrations = {
            "balance": "ALTER TABLE users ADD COLUMN balance TEXT DEFAULT '0'",
            "blocked": "ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0",
            "referred_by": "ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL",
            "referral_paid": "ALTER TABLE users ADD COLUMN referral_paid INTEGER DEFAULT 0",
        }

        for column, sql in migrations.items():
            if column not in columns:
                try:
                    con.execute(sql)
                except Exception:
                    logger.exception("Migration failed: %s", column)

        game_columns = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        }

        game_migrations = {
            "settled": "ALTER TABLE games ADD COLUMN settled INTEGER DEFAULT 0",
            "updated_at": "ALTER TABLE games ADD COLUMN updated_at TEXT DEFAULT CURRENT_TIMESTAMP",
        }

        for column, sql in game_migrations.items():
            if column not in game_columns:
                try:
                    con.execute(sql)
                except Exception:
                    logger.exception("Game migration failed: %s", column)

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

    if value < MIN_GAME:
        return None

    if value > MAX_GAME:
        return None

    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )


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
    return bool(row and int(row["blocked"]) == 1)


def is_admin(user_id):
    return user_id in ADMIN_IDS


def name_of(user):
    if not user:
        return "کاربر"

    if getattr(user, "first_name", None):
        return user.first_name

    if getattr(user, "username", None):
        return "@" + user.username

    return str(user.id)


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

            logger.exception("Debit failed")
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

            logger.exception("Credit failed")
            return False


def transfer_atomic(sender_id, receiver_id, amount):
    amount = D(amount)

    if amount <= 0 or sender_id == receiver_id:
        return False

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            sender = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (sender_id,)
            ).fetchone()

            receiver = con.execute(
                "SELECT balance FROM users WHERE user_id=?",
                (receiver_id,)
            ).fetchone()

            if not sender or not receiver:
                con.execute("ROLLBACK")
                return False

            sender_balance = D(sender["balance"])

            if sender_balance < amount:
                con.execute("ROLLBACK")
                return False

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(sender_balance - amount),
                sender_id
            ))

            receiver_balance = D(receiver["balance"])

            con.execute("""
            UPDATE users
            SET balance=?
            WHERE user_id=?
            """, (
                str(receiver_balance + amount),
                receiver_id
            ))

            con.execute("""
            INSERT INTO transfers
            (sender_id, receiver_id, amount)
            VALUES (?, ?, ?)
            """, (
                sender_id,
                receiver_id,
                str(amount)
            ))

            con.execute("COMMIT")
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("Transfer failed")
            return False


# ============================================================
# HOUSE
# ============================================================

def add_house(owner_amount=0, fee_amount=0, bot_amount=0):
    owner_amount = D(owner_amount)
    fee_amount = D(fee_amount)
    bot_amount = D(bot_amount)

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT owner_balance,
                   fee_balance,
                   bot_balance
            FROM house
            WHERE id=1
            """).fetchone()

            owner = D(row["owner_balance"])
            fee = D(row["fee_balance"])
            bot = D(row["bot_balance"])

            con.execute("""
            UPDATE house
            SET owner_balance=?,
                fee_balance=?,
                bot_balance=?
            WHERE id=1
            """, (
                str(owner + owner_amount),
                str(fee + fee_amount),
                str(bot + bot_amount)
            ))

            con.execute("COMMIT")
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("House update failed")
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


# ============================================================
# FORCED JOIN
# ============================================================

async def check_membership(bot, user_id):
    try:
        member = await bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id
        )

        return member.status in (
            "creator",
            "administrator",
            "member",
        )

    except TelegramError as e:
        logger.warning(
            "Membership check failed: %s",
            e
        )

        # اگر بات ادمین کانال نباشد، اجازه عبور می‌دهیم
        # تا کل ربات از کار نیفتد.
        return True


async def require_join(update, context):
    user = update.effective_user

    if not user:
        return False

    if is_admin(user.id):
        return True

    ok = await check_membership(
        context.bot,
        user.id
    )

    if ok:
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url=CHANNEL_LINK
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
            "⛔ برای استفاده از ربات ابتدا در کانال زیر عضو شو:\n\n"
            f"{CHANNEL_LINK}\n\n"
            "بعد روی «بررسی عضویت» بزن.",
            reply_markup=keyboard
        )
    except Exception:
        logger.exception("Join message failed")

    return False


# ============================================================
# REFERRAL
# ============================================================

async def process_referral(user, referrer_id):
    if not user:
        return

    if not referrer_id:
        return

    try:
        referrer_id = int(referrer_id)
    except Exception:
        return

    if referrer_id == user.id:
        return

    ensure_user(user)

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                target = con.execute("""
                SELECT referred_by
                FROM users
                WHERE user_id=?
                """, (
                    user.id,
                )).fetchone()

                referrer = con.execute("""
                SELECT user_id
                FROM users
                WHERE user_id=?
                """, (
                    referrer_id,
                )).fetchone()

                if not target or not referrer:
                    con.execute("ROLLBACK")
                    return

                if target["referred_by"] is not None:
                    con.execute("ROLLBACK")
                    return

                con.execute("""
                UPDATE users
                SET referred_by=?
                WHERE user_id=?
                """, (
                    referrer_id,
                    user.id
                ))

                ref_balance = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    referrer_id,
                )).fetchone()

                current = D(ref_balance["balance"])

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(current + REFERRAL_REWARD),
                    referrer_id
                ))

                con.execute("COMMIT")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception("Referral error")


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 موجودی", "🎮 بازی"],
            ["👥 بازی با دوستان", "🤖 بازی با ربات"],
            ["💸 انتقال", "📤 درخواست"],
            ["👥 زیرمجموعه", "📖 راهنما"],
        ],
        resize_keyboard=True
    )


def game_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🎲 تاس",
                callback_data="help_game_dice"
            ),
            InlineKeyboardButton(
                "🎳 بولینگ",
                callback_data="help_game_bowling"
            ),
        ],
        [
            InlineKeyboardButton(
                "🏀 بسکتبال",
                callback_data="help_game_basketball"
            ),
            InlineKeyboardButton(
                "🎯 دارت",
                callback_data="help_game_darts"
            ),
        ],
    ])


def created_game_keyboard(game_id):
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
                "❌ لغو بازی",
                callback_data=f"cancel_{game_id}"
            )
        ],
    ])


# ============================================================
# GAME LOCK
# ============================================================

async def get_game_lock(game_id):
    async with GAME_LOCKS_GUARD:

        lock = GAME_LOCKS.get(game_id)

        if lock is None:
            lock = asyncio.Lock()
            GAME_LOCKS[game_id] = lock

        return lock


async def remove_game_lock(game_id):
    async with GAME_LOCKS_GUARD:
        GAME_LOCKS.pop(game_id, None)


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

    for item in value.split(","):
        item = item.strip()

        if not item:
            continue

        try:
            result.append(int(item))
        except ValueError:
            continue

    return result


def rolls_text(rolls):
    return ",".join(
        str(int(x))
        for x in rolls
    )


def calculate_score(rolls):
    return sum(int(x) for x in rolls)


def winner_from_scores(a, b):
    if a > b:
        return 1

    if b > a:
        return 2

    return 0


# ============================================================
# PARSERS
# ============================================================

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
        match.group(1)
    )


def parse_game(text):
    text = normalize_digits(
        text or ""
    ).strip()

    # پشتیبانی:
    # 1 تاس 0.5
    # 10 بولینگ 1
    # 100 بسکتبال 2
    pattern = re.compile(
        r"^(\d+)\s+([^\s]+)\s+(\d+(?:[.,]\d+)?)$",
        re.IGNORECASE
    )

    match = pattern.match(text)

    if not match:
        return None

    try:
        rounds = int(match.group(1))
    except Exception:
        return None

    game_name = match.group(2).lower()

    game = GAME_NAMES.get(game_name)

    if not game:
        return None

    amount = parse_decimal_amount(
        match.group(3)
    )

    if amount is None:
        return None

    if rounds < 1:
        return None

    return game, rounds, amount


# ============================================================
# START
# ============================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    # /start REFERRER
    if context.args:
        try:
            referrer_id = int(
                normalize_digits(
                    context.args[0]
                )
            )
            await process_referral(
                user,
                referrer_id
            )
        except Exception:
            pass

    if not await require_join(
        update,
        context
    ):
        return

    if is_blocked(user.id):

        await update.effective_message.reply_text(
            "⛔ دسترسی شما مسدود است."
        )

        return

    await update.effective_message.reply_text(
        "👋 سلام!\n\n"
        "🎮 به ربات بازی خوش آمدی.\n\n"
        "💰 موجودی شما اعتبار داخلی بازی است.\n"
        "برای شروع از منوی زیر استفاده کن.",
        reply_markup=main_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    if not await require_join(
        update,
        context
    ):
        return

    if is_blocked(user.id):
        return

    balance = get_balance(
        user.id
    )

    await update.effective_message.reply_text(
        f"💰 موجودی {name_of(user)}:\n\n"
        f"💎 {money(balance)}"
    )


# ============================================================
# GAME MENU
# ============================================================

async def game_menu(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "🎮 نوع بازی را انتخاب کن:",
        reply_markup=game_keyboard()
    )


async def friends_menu(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    if update.effective_chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):

        await update.effective_message.reply_text(
            "❌ بازی با دوستان فقط داخل گپ قابل ساخت است."
        )

        return

    await update.effective_message.reply_text(
        "👥 بازی با دوستان\n\n"
        "مثال:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "2 بسکتبال 0.5\n"
        "3 دارت 0.5\n\n"
        "تعداد بازی/پرتاب محدودیت ندارد."
    )


async def bot_menu(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "🤖 بازی با ربات\n\n"
        "در پیوی یا گپ بنویس:\n\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "1 بسکتبال 0.5\n"
        "1 دارت 0.5\n\n"
        "بعد از ساخت بازی، خودت بازی را می‌اندازی."
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(
    update,
    context,
    game_type,
    rounds,
    amount
):

    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if not user or not chat or not message:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    # --------------------------------------------------------
    # اتمیک رزرو موجودی + ساخت بازی
    # --------------------------------------------------------

    async with DB_LOCK:

        with closing(db()) as con:

            try:
                con.execute("BEGIN IMMEDIATE")

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (
                    user.id,
                )).fetchone()

                if not row:
                    con.execute("ROLLBACK")

                    await message.reply_text(
                        "❌ کاربر پیدا نشد."
                    )

                    return

                balance = D(
                    row["balance"]
                )

                if balance < amount:
                    con.execute("ROLLBACK")

                    await message.reply_text(
                        "❌ موجودی کافی نیست.\n\n"
                        f"💰 موجودی: {money(balance)}"
                    )

                    return

                # رزرو پول
                new_balance = balance - amount

                con.execute("""
                UPDATE users
                SET balance=?
                WHERE user_id=?
                """, (
                    str(new_balance),
                    user.id
                ))

                # بازی را بلافاصله ثبت می‌کنیم
                cur = con.execute("""
                INSERT INTO games
                (
                    chat_id,
                    creator_id,
                    game_type,
                    amount,
                    rounds,
                    status,
                    settled
                )
                VALUES (?, ?, ?, ?, ?, 'waiting', 0)
                """, (
                    chat.id,
                    user.id,
                    game_type,
                    str(amount),
                    rounds,
                ))

                game_id = cur.lastrowid

                con.execute("COMMIT")

            except Exception:

                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "CREATE GAME FAILED"
                )

                await message.reply_text(
                    "❌ ساخت بازی انجام نشد. "
                    "موجودی شما کسر نشده است."
                )

                return

    # --------------------------------------------------------
    # ارسال پیام بازی
    # --------------------------------------------------------

    try:

        sent = await context.bot.send_message(
            chat_id=chat.id,
            text=(
                f"{GAME_LABELS[game_type]}\n\n"
                f"🎮 تعداد پرتاب: {rounds}\n"
                f"💰 مبلغ: {money(amount)}\n\n"
                f"👤 سازنده: {name_of(user)}\n\n"
                f"یکی از گزینه‌ها را انتخاب کن:"
            ),
            reply_markup=created_game_keyboard(
                game_id
            )
        )

        with closing(db()) as con:

            con.execute("""
            UPDATE games
            SET message_id=?,
                updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """, (
                sent.message_id,
                game_id
            ))

            con.commit()

    except Exception:

        logger.exception(
            "GAME MESSAGE FAILED"
        )

        # اگر پیام بازی ارسال نشد، رزرو پول برگردد
        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute("BEGIN IMMEDIATE")

                    game = con.execute("""
                    SELECT settled, status, amount, creator_id
                    FROM games
                    WHERE id=?
                    """, (
                        game_id,
                    )).fetchone()

                    if game and int(game["settled"]) == 0:

                        row = con.execute("""
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """, (
                            user.id,
                        )).fetchone()

                        if row:

                            balance = D(
                                row["balance"]
                            )

                            con.execute("""
                            UPDATE users
                            SET balance=?
                            WHERE user_id=?
                            """, (
                                str(
                                    balance +
                                    D(game["amount"])
                                ),
                                user.id
                            ))

                        con.execute("""
                        UPDATE games
                        SET status='cancelled',
                            settled=1,
                            updated_at=CURRENT_TIMESTAMP
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
                    "GAME REFUND FAILED"
                )

        await message.reply_text(
            "❌ بازی ارسال نشد؛ "
            "مبلغ رزروشده برگشت داده شد."
        )


# ============================================================
# GAME HELP CALLBACK
# ============================================================

async def game_help_callback(update, context):

    query = update.callback_query

    await query.answer()

    game = query.data.replace(
        "help_game_",
        "",
        1
    )

    if game not in GAME_LABELS:
        return

    label = GAME_LABELS[game]

    await query.message.reply_text(
        f"{label}\n\n"
        f"فرمت ساخت بازی:\n"
        f"`تعداد {label.split(' ', 1)[1]} مبلغ`\n\n"
        f"مثال:\n"
        f"1 {label.split(' ', 1)[1]} 0.5\n"
        f"2 {label.split(' ', 1)[1]} 0.5\n\n"
        f"تعداد پرتاب محدودیت ندارد."
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_friend(update, context):

    query = update.callback_query
    user = query.from_user

    await query.answer()

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    ensure_user(user)

    if not await check_membership(
        context.bot,
        user.id
    ):
        await query.answer(
            "ابتدا در کانال عضو شو.",
            show_alert=True
        )
        return

    lock = await get_game_lock(
        game_id
    )

    async with lock:

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

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

                    amount = D(
                        game["amount"]
                    )

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
                            "❌ ابتدا /start را بزن.",
                            show_alert=True
                        )
                        return

                    balance = D(
                        row["balance"]
                    )

                    if balance < amount:
                        con.execute("ROLLBACK")

                        await query.answer(
                            "❌ موجودی کافی نیست.",
                            show_alert=True
                        )
                        return

                    # قفل مبلغ حریف
                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(balance - amount),
                        user.id
                    ))

                    con.execute("""
                    UPDATE games
                    SET opponent_id=?,
                        status='creator_turn',
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    AND status='waiting'
                    """, (
                        user.id,
                        game_id
                    ))

                    con.execute("COMMIT")

                except Exception:

                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass

                    logger.exception(
                        "JOIN FRIEND FAILED"
                    )

                    await query.answer(
                        "❌ ورود به بازی انجام نشد.",
                        show_alert=True
                    )

                    return

        try:
            await query.message.edit_reply_markup(
                reply_markup=None
            )
        except Exception:
            pass

        creator_id = int(
            game["creator_id"]
        )

        creator_name = str(
            creator_id
        )

        try:
            creator_chat = await context.bot.get_chat(
                creator_id
            )
            creator_name = (
                creator_chat.first_name
                or creator_chat.username
                or str(creator_id)
            )
        except Exception:
            pass

        await query.message.reply_text(
            f"👥 حریف وارد بازی شد: {name_of(user)}\n\n"
            f"🎯 ابتدا {creator_name} باید "
            f"{game['rounds']} بار "
            f"{GAME_EMOJIS[game['game_type']]} "
            f"را خودش بفرستد.\n\n"
            f"🤖 ربات به جای بازیکن تاس نمی‌اندازد."
        )


# ============================================================
# PLAY VS BOT
# ============================================================

async def play_vs_bot(update, context):

    query = update.callback_query
    user = query.from_user

    await query.answer()

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    ensure_user(user)

    if not await check_membership(
        context.bot,
        user.id
    ):
        await query.answer(
            "ابتدا در کانال عضو شو.",
            show_alert=True
        )
        return

    lock = await get_game_lock(
        game_id
    )

    async with lock:

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

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
                            "❌ این بازی دیگر قابل شروع نیست.",
                            show_alert=True
                        )
                        return

                    if int(game["creator_id"]) != user.id:
                        con.execute("ROLLBACK")

                        await query.answer(
                            "❌ فقط سازنده بازی می‌تواند با ربات بازی کند.",
                            show_alert=True
                        )
                        return

                    # پول سازنده از قبل هنگام ساخت بازی رزرو شده.
                    con.execute("""
                    UPDATE games
                    SET status='bot_creator_turn',
                        opponent_id=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    AND status='waiting'
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
                        "BOT GAME START FAILED"
                    )

                    await query.answer(
                        "❌ شروع بازی انجام نشد.",
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
            f"{GAME_EMOJIS[game['game_type']]} "
            f"بفرست.\n\n"
            f"بعد از پایان پرتاب‌های تو، "
            f"ربات خودش پرتاب می‌کند."
        )


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query
    user = query.from_user

    await query.answer()

    try:
        game_id = int(
            query.data.split("_", 1)[1]
        )
    except Exception:
        return

    lock = await get_game_lock(
        game_id
    )

    async with lock:

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

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

                    if int(game["creator_id"]) != user.id:
                        con.execute("ROLLBACK")

                        await query.answer(
                            "❌ فقط سازنده می‌تواند لغو کند.",
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

                    if int(game["settled"]) == 1:
                        con.execute("ROLLBACK")

                        await query.answer(
                            "این بازی قبلاً تسویه شده.",
                            show_alert=True
                        )
                        return

                    amount = D(
                        game["amount"]
                    )

                    row = con.execute("""
                    SELECT balance
                    FROM users
                    WHERE user_id=?
                    """, (
                        user.id,
                    )).fetchone()

                    if not row:
                        con.execute("ROLLBACK")
                        return

                    balance = D(
                        row["balance"]
                    )

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(balance + amount),
                        user.id
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='cancelled',
                        settled=1,
                        updated_at=CURRENT_TIMESTAMP
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
                        "CANCEL FAILED"
                    )

                    await query.answer(
                        "❌ لغو بازی انجام نشد.",
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
            "❌ بازی لغو شد.\n"
            f"💰 {money(game['amount'])} "
            f"اعتبار برگشت داده شد."
        )

    await remove_game_lock(game_id)


# ============================================================
# SAFE SEND DICE
# ============================================================

async def send_game_dice(
    bot,
    chat_id,
    emoji
):
    """
    ربات خودش پرتاب را انجام می‌دهد.
    در صورت خطای TelegramException، exception را بالا نمی‌فرستیم
    تا polling اصلی بات متوقف نشود.
    """

    try:
        message = await bot.send_dice(
            chat_id=chat_id,
            emoji=emoji
        )

        if not message or not message.dice:
            return None

        return int(
            message.dice.value
        )

    except TelegramError:
        logger.exception(
            "BOT SEND DICE FAILED"
        )
        return None

    except Exception:
        logger.exception(
            "UNKNOWN BOT DICE ERROR"
        )
        return None


# ============================================================
# FINISH / SETTLEMENT
# ============================================================

async def settle_friend_game(
    context,
    game_id,
    creator_rolls,
    opponent_rolls
):

    lock = await get_game_lock(
        game_id
    )

    async with lock:

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

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

                    # ضد دوباره پرداخت
                    if int(game["settled"]) == 1:
                        con.execute("ROLLBACK")
                        return

                    if game["status"] != "finished":
                        con.execute("ROLLBACK")
                        return

                    creator_id = int(
                        game["creator_id"]
                    )

                    opponent_id = int(
                        game["opponent_id"]
                    )

                    amount = D(
                        game["amount"]
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

                    # ------------------------------------------------
                    # مساوی
                    # ------------------------------------------------

                    if result == 0:

                        creator = con.execute("""
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """, (
                            creator_id,
                        )).fetchone()

                        opponent = con.execute("""
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """, (
                            opponent_id,
                        )).fetchone()

                        if not creator or not opponent:
                            con.execute("ROLLBACK")
                            return

                        creator_balance = D(
                            creator["balance"]
                        )

                        opponent_balance = D(
                            opponent["balance"]
                        )

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(
                                creator_balance +
                                amount
                            ),
                            creator_id
                        ))

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(
                                opponent_balance +
                                amount
                            ),
                            opponent_id
                        ))

                        con.execute("""
                        UPDATE games
                        SET settled=1,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """, (
                            game_id,
                        ))

                        con.execute("COMMIT")

                        result_text = (
                            f"🤝 مساوی شد!\n\n"
                            f"👤 {creator_id}: {score_creator}\n"
                            f"👤 {opponent_id}: {score_opponent}\n\n"
                            f"💰 مبلغ هر دو نفر برگشت داده شد."
                        )

                    else:

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

                        # کل مبلغ دو طرف
                        total = amount * 2

                        # نمونه برای 0.5:
                        # برنده 0.95
                        # مالک 0.02
                        # کارمزد 0.03
                        #
                        # برای مبلغ‌های بزرگ‌تر، همین نسبت حفظ می‌شود.
                        owner_share = (
                            amount *
                            OWNER_SHARE
                        )

                        fee = (
                            amount *
                            BOT_FEE
                        )

                        # برای مدل درصدی:
                        payout = (
                            total *
                            WINNER_PAYOUT_RATE
                        )

                        # اطمینان از اینکه مجموع از کل بیشتر نشود
                        max_payout = (
                            total -
                            owner_share -
                            fee
                        )

                        if payout > max_payout:
                            payout = max_payout

                        winner = con.execute("""
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """, (
                            winner_id,
                        )).fetchone()

                        if not winner:
                            con.execute("ROLLBACK")
                            return

                        winner_balance = D(
                            winner["balance"]
                        )

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(
                                winner_balance +
                                payout
                            ),
                            winner_id
                        ))

                        house = con.execute("""
                        SELECT owner_balance,
                               fee_balance
                        FROM house
                        WHERE id=1
                        """).fetchone()

                        house_owner = D(
                            house["owner_balance"]
                        )

                        house_fee = D(
                            house["fee_balance"]
                        )

                        con.execute("""
                        UPDATE house
                        SET owner_balance=?,
                            fee_balance=?
                        WHERE id=1
                        """, (
                            str(
                                house_owner +
                                owner_share
                            ),
                            str(
                                house_fee +
                                fee
                            )
                        ))

                        con.execute("""
                        UPDATE games
                        SET winner_id=?,
                            settled=1,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """, (
                            winner_id,
                            game_id
                        ))

                        con.execute("COMMIT")

                        result_text = (
                            f"🏆 نتیجه بازی\n\n"
                            f"👤 {creator_id}: {score_creator}\n"
                            f"👤 {opponent_id}: {score_opponent}\n\n"
                            f"🏆 برنده: {winner_id}\n"
                            f"🎯 امتیاز برنده: {winner_score}\n"
                            f"🎯 امتیاز حریف: {loser_score}\n\n"
                            f"💰 دریافتی برنده: {money(payout)}"
                        )

        try:
            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=result_text
            )
        except Exception:
            logger.exception(
                "FRIEND RESULT SEND FAILED"
            )

    await remove_game_lock(game_id)


async def settle_bot_game(
    context,
    game_id,
    user_rolls,
    bot_rolls
):

    lock = await get_game_lock(
        game_id
    )

    async with lock:

        async with DB_LOCK:

            with closing(db()) as con:

                try:
                    con.execute(
                        "BEGIN IMMEDIATE"
                    )

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

                    if int(game["settled"]) == 1:
                        con.execute("ROLLBACK")
                        return

                    user_id = int(
                        game["creator_id"]
                    )

                    amount = D(
                        game["amount"]
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

                    # ------------------------------------------------
                    # مساوی
                    # ------------------------------------------------

                    if result == 0:

                        row = con.execute("""
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """, (
                            user_id,
                        )).fetchone()

                        if not row:
                            con.execute("ROLLBACK")
                            return

                        balance = D(
                            row["balance"]
                        )

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(balance + amount),
                            user_id
                        ))

                        con.execute("""
                        UPDATE games
                        SET status='finished',
                            settled=1,
                            winner_id=NULL,
                            opponent_rolls=?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """, (
                            rolls_text(bot_rolls),
                            game_id
                        ))

                        con.execute("COMMIT")

                        result_text = (
                            f"🤝 مساوی شد!\n\n"
                            f"👤 {user_id}: {user_score}\n"
                            f"🤖 ربات: {bot_score}\n\n"
                            f"💰 مبلغ برگشت داده شد."
                        )

                    # ------------------------------------------------
                    # کاربر برنده
                    # ------------------------------------------------

                    elif result == 1:

                        total = amount * 2

                        owner_share = (
                            amount *
                            OWNER_SHARE
                        )

                        fee = (
                            amount *
                            BOT_FEE
                        )

                        payout = (
                            total *
                            WINNER_PAYOUT_RATE
                        )

                        max_payout = (
                            total -
                            owner_share -
                            fee
                        )

                        if payout > max_payout:
                            payout = max_payout

                        row = con.execute("""
                        SELECT balance
                        FROM users
                        WHERE user_id=?
                        """, (
                            user_id,
                        )).fetchone()

                        if not row:
                            con.execute("ROLLBACK")
                            return

                        balance = D(
                            row["balance"]
                        )

                        con.execute("""
                        UPDATE users
                        SET balance=?
                        WHERE user_id=?
                        """, (
                            str(balance + payout),
                            user_id
                        ))

                        house = con.execute("""
                        SELECT owner_balance,
                               fee_balance
                        FROM house
                        WHERE id=1
                        """).fetchone()

                        owner_balance = D(
                            house["owner_balance"]
                        )

                        fee_balance = D(
                            house["fee_balance"]
                        )

                        con.execute("""
                        UPDATE house
                        SET owner_balance=?,
                            fee_balance=?
                        WHERE id=1
                        """, (
                            str(
                                owner_balance +
                                owner_share
                            ),
                            str(
                                fee_balance +
                                fee
                            )
                        ))

                        con.execute("""
                        UPDATE games
                        SET status='finished',
                            settled=1,
                            winner_id=?,
                            opponent_rolls=?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """, (
                            user_id,
                            rolls_text(bot_rolls),
                            game_id
                        ))

                        con.execute("COMMIT")

                        result_text = (
                            f"🏆 نتیجه بازی\n\n"
                            f"👤 {user_id}: {user_score}\n"
                            f"🤖 ربات: {bot_score}\n\n"
                            f"🏆 برنده: {user_id}\n"
                            f"🎯 امتیاز برنده: {user_score}\n"
                            f"🎯 امتیاز ربات: {bot_score}\n\n"
                            f"💰 دریافتی: {money(payout)}"
                        )

                    # ------------------------------------------------
                    # ربات برنده
                    # ------------------------------------------------

                    else:

                        # مبلغ رزروشده کاربر به موجودی خانه منتقل می‌شود
                        house = con.execute("""
                        SELECT bot_balance
                        FROM house
                        WHERE id=1
                        """).fetchone()

                        bot_balance = D(
                            house["bot_balance"]
                        )

                        con.execute("""
                        UPDATE house
                        SET bot_balance=?
                        WHERE id=1
                        """, (
                            str(
                                bot_balance +
                                amount
                            ),
                        ))

                        con.execute("""
                        UPDATE games
                        SET status='finished',
                            settled=1,
                            winner_id=NULL,
                            opponent_rolls=?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """, (
                            rolls_text(bot_rolls),
                            game_id
                        ))

                        con.execute("COMMIT")

                        result_text = (
                            f"🏆 نتیجه بازی\n\n"
                            f"👤 {user_id}: {user_score}\n"
                            f"🤖 ربات: {bot_score}\n\n"
                            f"🏆 برنده: 🤖 ربات\n"
                            f"🎯 امتیاز برنده: {bot_score}\n"
                            f"🎯 امتیاز شما: {user_score}"
                        )

                except Exception:

                    try:
                        con.execute("ROLLBACK")
                    except Exception:
                        pass

                    logger.exception(
                        "BOT SETTLEMENT FAILED"
                    )

                    return

        try:
            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=result_text
            )
        except Exception:
            logger.exception(
                "BOT RESULT SEND FAILED"
            )

    await remove_game_lock(game_id)


# ============================================================
# PROCESS USER ROLL
# ============================================================

async def process_user_roll(
    update,
    context
):

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

    chat_id = message.chat_id

    # --------------------------------------------------------
    # پیدا کردن بازی مناسب
    # --------------------------------------------------------

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
        LIMIT 100
        """, (
            chat_id,
        )).fetchall()

    game = None

    for row in rows:

        creator_id = int(
            row["creator_id"]
        )

        opponent_id = (
            int(row["opponent_id"])
            if row["opponent_id"] is not None
            else None
        )

        status = row["status"]

        if status == "bot_creator_turn":
            if user.id == creator_id:
                game = row
                break

        elif status == "creator_turn":
            if user.id == creator_id:
                game = row
                break

        elif status == "opponent_turn":
            if opponent_id == user.id:
                game = row
                break

    if not game:
        return

    game_id = int(
        game["id"]
    )

    game_type = game["game_type"]

    expected_emoji = GAME_EMOJIS[
        game_type
    ]

    # --------------------------------------------------------
    # بررسی نوع پرتاب
    # --------------------------------------------------------

    if dice.emoji != expected_emoji:

        try:
            await message.reply_text(
                f"❌ برای این بازی باید "
                f"{expected_emoji} "
                f"بفرستی."
            )
        except Exception:
            pass

        return

    lock = await get_game_lock(
        game_id
    )

    async with lock:

        # دوباره از DB بخوان
        fresh_game = get_game(
            game_id
        )

        if not fresh_game:
            return

        game = fresh_game

        status = game["status"]

        if status not in (
            "bot_creator_turn",
            "creator_turn",
            "opponent_turn"
        ):
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

        value = int(
            dice.value
        )

        # ====================================================
        # BOT GAME
        # ====================================================

        if status == "bot_creator_turn":

            if user.id != int(
                game["creator_id"]
            ):
                return

            if len(creator_rolls) >= rounds:
                return

            creator_rolls.append(
                value
            )

            # هنوز تمام نشده
            if len(creator_rolls) < rounds:

                with closing(db()) as con:

                    con.execute("""
                    UPDATE games
                    SET creator_rolls=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    AND status='bot_creator_turn'
                    """, (
                        rolls_text(
                            creator_rolls
                        ),
                        game_id
                    ))

                    con.commit()

                remaining = (
                    rounds -
                    len(creator_rolls)
                )

                try:
                    await message.reply_text(
                        f"👤 {name_of(user)}: {value}\n"
                        f"🎯 {remaining} پرتاب باقی مانده."
                    )
                except Exception:
                    pass

                return

            # ------------------------------------------------
            # پرتاب‌های کاربر تمام شد
            # ------------------------------------------------

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET creator_rolls=?,
                    status='bot_rolling',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                AND status='bot_creator_turn'
                """, (
                    rolls_text(
                        creator_rolls
                    ),
                    game_id
                ))

                con.commit()

            try:
                await message.reply_text(
                    f"👤 {name_of(user)}: {value}\n\n"
                    f"🤖 حالا نوبت ربات است..."
                )
            except Exception:
                pass

            # ------------------------------------------------
            # ربات خودش پرتاب می‌کند
            # ------------------------------------------------

            bot_rolls = []

            for index in range(rounds):

                # بررسی اینکه بازی هنوز bot_rolling است
                current = get_game(
                    game_id
                )

                if not current:
                    return

                if current["status"] != "bot_rolling":
                    return

                bot_value = await send_game_dice(
                    context.bot,
                    chat_id,
                    expected_emoji
                )

                # اگر Telegram خطا داد، بازی را نیمه‌کاره رها نکن
                if bot_value is None:

                    # بازی را به حالت قابل بازیابی برگردان
                    with closing(db()) as con:

                        con.execute("""
                        UPDATE games
                        SET status='bot_retry',
                            opponent_rolls=?,
                            updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                        """, (
                            rolls_text(
                                bot_rolls
                            ),
                            game_id
                        ))

                        con.commit()

                    try:
                        await message.reply_text(
                            "⚠️ یک خطای موقت در پرتاب ربات رخ داد.\n"
                            "بازی حذف نشد و مبلغ شما محفوظ است.\n"
                            "دوباره تلاش می‌شود."
                        )
                    except Exception:
                        pass

                    # retry محدود
                    for retry in range(3):

                        await asyncio.sleep(2)

                        bot_value = await send_game_dice(
                            context.bot,
                            chat_id,
                            expected_emoji
                        )

                        if bot_value is not None:
                            break

                    if bot_value is None:

                        # در صورت شکست کامل، مبلغ کاربر برگردد
                        async with DB_LOCK:

                            with closing(db()) as con:

                                try:
                                    con.execute(
                                        "BEGIN IMMEDIATE"
                                    )

                                    current = con.execute("""
                                    SELECT *
                                    FROM games
                                    WHERE id=?
                                    """, (
                                        game_id,
                                    )).fetchone()

                                    if (
                                        current and
                                        int(current["settled"]) == 0
                                    ):

                                        row = con.execute("""
                                        SELECT balance
                                        FROM users
                                        WHERE user_id=?
                                        """, (
                                            user.id,
                                        )).fetchone()

                                        if row:

                                            balance = D(
                                                row["balance"]
                                            )

                                            con.execute("""
                                            UPDATE users
                                            SET balance=?
                                            WHERE user_id=?
                                            """, (
                                                str(
                                                    balance +
                                                    D(current["amount"])
                                                ),
                                                user.id
                                            ))

                                        con.execute("""
                                        UPDATE games
                                        SET status='cancelled',
                                            settled=1,
                                            updated_at=CURRENT_TIMESTAMP
                                        WHERE id=?
                                        """, (
                                            game_id,
                                        ))

                                    con.execute("COMMIT")

                                except Exception:
                                    try:
                                        con.execute(
                                            "ROLLBACK"
                                        )
                                    except Exception:
                                        pass

                        try:
                            await message.reply_text(
                                "❌ پرتاب ربات انجام نشد.\n"
                                "💰 مبلغ بازی به شما برگشت داده شد."
                            )
                        except Exception:
                            pass

                        return

                bot_rolls.append(
                    int(bot_value)
                )

                with closing(db()) as con:

                    con.execute("""
                    UPDATE games
                    SET opponent_rolls=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """, (
                        rolls_text(
                            bot_rolls
                        ),
                        game_id
                    ))

                    con.commit()

                await asyncio.sleep(0.7)

            # ------------------------------------------------
            # نتیجه
            # ------------------------------------------------

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET status='finished',
                    opponent_rolls=?,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """, (
                    rolls_text(
                        bot_rolls
                    ),
                    game_id
                ))

                con.commit()

            await settle_bot_game(
                context,
                game_id,
                creator_rolls,
                bot_rolls
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
                value
            )

            if len(creator_rolls) < rounds:

                with closing(db()) as con:

                    con.execute("""
                    UPDATE games
                    SET creator_rolls=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    AND status='creator_turn'
                    """, (
                        rolls_text(
                            creator_rolls
                        ),
                        game_id
                    ))

                    con.commit()

                remaining = (
                    rounds -
                    len(creator_rolls)
                )

                try:
                    await message.reply_text(
                        f"👤 {name_of(user)}: {value}\n"
                        f"🎯 {remaining} پرتاب باقی مانده."
                    )
                except Exception:
                    pass

                return

            # ------------------------------------------------
            # creator finished
            # ------------------------------------------------

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET creator_rolls=?,
                    status='opponent_turn',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                AND status='creator_turn'
                """, (
                    rolls_text(
                        creator_rolls
                    ),
                    game_id
                ))

                con.commit()

            opponent_id = int(
                game["opponent_id"]
            )

            opponent_name = str(
                opponent_id
            )

            try:
                opponent_chat = await context.bot.get_chat(
                    opponent_id
                )

                opponent_name = (
                    opponent_chat.first_name
                    or (
                        "@" +
                        opponent_chat.username
                        if opponent_chat.username
                        else None
                    )
                    or str(opponent_id)
                )

            except Exception:
                pass

            try:
                await message.reply_text(
                    f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
                    f"🎯 حالا حریف، {opponent_name}، "
                    f"باید خودش {rounds} بار "
                    f"{expected_emoji} بفرستد.\n\n"
                    f"🤖 ربات به جای حریف پرتاب نمی‌کند."
                )
            except Exception:
                pass

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
                value
            )

            if len(opponent_rolls) < rounds:

                with closing(db()) as con:

                    con.execute("""
                    UPDATE games
                    SET opponent_rolls=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    AND status='opponent_turn'
                    """, (
                        rolls_text(
                            opponent_rolls
                        ),
                        game_id
                    ))

                    con.commit()

                remaining = (
                    rounds -
                    len(opponent_rolls)
                )

                try:
                    await message.reply_text(
                        f"👤 {name_of(user)}: {value}\n"
                        f"🎯 {remaining} پرتاب باقی مانده."
                    )
                except Exception:
                    pass

                return

            # ------------------------------------------------
            # opponent finished
            # ------------------------------------------------

            with closing(db()) as con:

                con.execute("""
                UPDATE games
                SET opponent_rolls=?,
                    status='finished',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                AND status='opponent_turn'
                """, (
                    rolls_text(
                        opponent_rolls
                    ),
                    game_id
                ))

                con.commit()

            await settle_friend_game(
                context,
                game_id,
                creator_rolls,
                opponent_rolls
            )


# ============================================================
# RECOVER STALE GAMES
# ============================================================

async def recover_stale_games(
    application
):

    """
    اگر بات ری‌استارت شد و بازی وسط کار ماند:
    مبلغ رزروشده از بین نمی‌رود.
    بازی‌های قدیمی لغو و مبلغ به سازنده برگردانده می‌شود.
    """

    while True:

        try:

            with closing(db()) as con:

                rows = con.execute("""
                SELECT *
                FROM games
                WHERE settled=0
                AND status IN (
                    'waiting',
                    'bot_creator_turn',
                    'bot_rolling',
                    'bot_retry',
                    'creator_turn',
                    'opponent_turn'
                )
                """).fetchall()

            for game in rows:

                try:
                    created = datetime.fromisoformat(
                        game["created_at"]
                    )
                except Exception:
                    continue

                age = (
                    datetime.utcnow() -
                    created
                ).total_seconds()

                if age < (
                    STALE_GAME_HOURS *
                    3600
                ):
                    continue

                game_id = int(
                    game["id"]
                )

                lock = await get_game_lock(
                    game_id
                )

                async with lock:

                    async with DB_LOCK:

                        with closing(db()) as con:

                            try:
                                con.execute(
                                    "BEGIN IMMEDIATE"
                                )

                                current = con.execute("""
                                SELECT *
                                FROM games
                                WHERE id=?
                                """, (
                                    game_id,
                                )).fetchone()

                                if (
                                    not current or
                                    int(current["settled"]) == 1
                                ):
                                    con.execute(
                                        "ROLLBACK"
                                    )
                                    continue

                                creator_id = int(
                                    current["creator_id"]
                                )

                                amount = D(
                                    current["amount"]
                                )

                                row = con.execute("""
                                SELECT balance
                                FROM users
                                WHERE user_id=?
                                """, (
                                    creator_id,
                                )).fetchone()

                                if row:

                                    balance = D(
                                        row["balance"]
                                    )

                                    con.execute("""
                                    UPDATE users
                                    SET balance=?
                                    WHERE user_id=?
                                    """, (
                                        str(
                                            balance +
                                            amount
                                        ),
                                        creator_id
                                    ))

                                # اگر حریف پول داده باشد
                                if current["opponent_id"]:

                                    opponent_id = int(
                                        current["opponent_id"]
                                    )

                                    opponent = con.execute("""
                                    SELECT balance
                                    FROM users
                                    WHERE user_id=?
                                    """, (
                                        opponent_id,
                                    )).fetchone()

                                    if opponent:

                                        balance = D(
                                            opponent["balance"]
                                        )

                                        con.execute("""
                                        UPDATE users
                                        SET balance=?
                                        WHERE user_id=?
                                        """, (
                                            str(
                                                balance +
                                                amount
                                            ),
                                            opponent_id
                                        ))

                                con.execute("""
                                UPDATE games
                                SET status='cancelled',
                                    settled=1,
                                    updated_at=CURRENT_TIMESTAMP
                                WHERE id=?
                                """, (
                                    game_id,
                                ))

                                con.execute("COMMIT")

                            except Exception:

                                try:
                                    con.execute(
                                        "ROLLBACK"
                                    )
                                except Exception:
                                    pass

                                logger.exception(
                                    "STALE GAME RECOVERY ERROR"
                                )

                    try:
                        await application.bot.send_message(
                            chat_id=game["chat_id"],
                            text=(
                                "🛡️ یک بازی نیمه‌کاره به‌دلیل "
                                "گذشت زمان لغو شد.\n"
                                "💰 مبلغ بازیکنان برگشت داده شد."
                            )
                        )
                    except Exception:
                        pass

                    await remove_game_lock(
                        game_id
                    )

        except Exception:
            logger.exception(
                "RECOVERY LOOP ERROR"
            )

        await asyncio.sleep(300)


# ============================================================
# TRANSFER
# ============================================================

async def transfer_command(
    update,
    context
):

    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    if not await require_join(
        update,
        context
    ):
        return

    ensure_user(user)

    if not message.reply_to_message:

        await message.reply_text(
            "💸 برای انتقال باید روی پیام کاربر Reply کنی.\n\n"
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
            "❌ مقدار نامعتبر است.\n"
            "مثال: انتقال 0.5"
        )

        return

    ensure_user(target)

    async with DB_LOCK:

        ok = transfer_atomic(
            user.id,
            target.id,
            amount
        )

    if not ok:

        await message.reply_text(
            "❌ انتقال انجام نشد.\n"
            "ممکن است موجودی کافی نباشد."
        )

        return

    await message.reply_text(
        f"✅ انتقال انجام شد.\n\n"
        f"👤 مقصد: {name_of(target)}\n"
        f"💰 مقدار: {money(amount)}"
    )


# ============================================================
# REFERRAL INFO
# ============================================================

async def referral_menu(
    update,
    context
):

    user = update.effective_user

    if not user:
        return

    if not await require_join(
        update,
        context
    ):
        return

    ensure_user(user)

    bot_username = None

    try:
        me = await context.bot.get_me()
        bot_username = me.username
    except Exception:
        pass

    if bot_username:

        link = (
            f"https://t.me/"
            f"{bot_username}"
            f"?start={user.id}"
        )

    else:
        link = (
            f"/start {user.id}"
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

    await update.effective_message.reply_text(
        "👥 زیرمجموعه\n\n"
        f"🔗 لینک شما:\n{link}\n\n"
        f"👥 تعداد زیرمجموعه: {count}\n"
        f"🎁 پاداش هر زیرمجموعه: {money(REFERRAL_REWARD)}"
    )


# ============================================================
# REQUEST
# ============================================================

async def request_menu(
    update,
    context
):

    if not await require_join(
        update,
        context
    ):
        return

    user = update.effective_user

    context.user_data["request_mode"] = "amount"

    await update.effective_message.reply_text(
        "📤 درخواست\n\n"
        "مقدار را بفرست.\n"
        "مثال:\n"
        "5"
    )


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

async def help_command(
    update,
    context
):

    if not await require_join(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "📖 راهنما\n\n"
        "🎮 ساخت بازی:\n"
        "1 تاس 0.5\n"
        "2 تاس 0.5\n"
        "1 بولینگ 0.5\n"
        "2 بسکتبال 0.5\n"
        "3 دارت 0.5\n\n"
        "🤖 بازی با ربات:\n"
        "اول خودت پرتاب می‌کنی؛ سپس ربات خودش پرتاب می‌کند.\n\n"
        "👥 بازی دوستان:\n"
        "سازنده خودش پرتاب می‌کند؛ سپس حریف خودش پرتاب می‌کند.\n\n"
        "💰 موجودی\n"
        "💸 انتقال 0.5 با Reply\n"
        "👥 زیرمجموعه\n"
        "📤 درخواست\n\n"
        "🛡️ بازی‌ها تعداد محدود ندارند."
    )


# ============================================================
# ADMIN
# ============================================================

def admin_keyboard():
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
        ]
    ])


async def admin_command(
    update,
    context
):

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


async def admin_callback(
    update,
    context
):

    query = update.callback_query
    user = query.from_user

    await query.answer()

    if not is_admin(
        user.id
    ):
        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True
        )
        return

    data = query.data

    # --------------------------------------------------------
    # USERS
    # --------------------------------------------------------

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

            display = (
                row["first_name"]
                or (
                    "@" +
                    row["username"]
                    if row["username"]
                    else None
                )
                or str(row["user_id"])
            )

            status = (
                "🚫"
                if int(row["blocked"])
                else
                "✅"
            )

            text += (
                f"{index}. {status} {display}\n"
                f"ID: {row['user_id']}\n"
                f"💰 {money(row['balance'])}\n\n"
            )

        await query.edit_message_text(
            text[:4000]
        )

        return

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    if data == "admin_stats":

        with closing(db()) as con:

            users = con.execute(
                "SELECT COUNT(*) FROM users"
            ).fetchone()[0]

            games = con.execute(
                "SELECT COUNT(*) FROM games"
            ).fetchone()[0]

            total = con.execute("""
            SELECT SUM(CAST(balance AS REAL))
            FROM users
            """).fetchone()[0] or 0

            pending = con.execute("""
            SELECT COUNT(*)
            FROM requests
            WHERE status='pending'
            """).fetchone()[0]

            house = con.execute("""
            SELECT owner_balance,
                   fee_balance,
                   bot_balance
            FROM house
            WHERE id=1
            """).fetchone()

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"🎮 بازی‌ها: {games:,}\n"
            f"💰 موجودی کاربران: {money(total)}\n"
            f"📋 درخواست‌ها: {pending:,}\n\n"
            f"👑 سهم مالک: {money(house['owner_balance'])}\n"
            f"💼 کارمزد: {money(house['fee_balance'])}\n"
            f"🤖 موجودی ربات: {money(house['bot_balance'])}"
        )

        return

    # --------------------------------------------------------
    # ADD
    # --------------------------------------------------------

    if data == "admin_add":

        await query.edit_message_text(
            "➕ افزایش موجودی\n\n"
            "/addbalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/addbalance 123456789 10"
        )

        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "/removebalance USER_ID AMOUNT\n\n"
            "مثال:\n"
            "/removebalance 123456789 10"
        )

        return

    # --------------------------------------------------------
    # REQUESTS
    # --------------------------------------------------------

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
                f"💰 {money(row['amount'])}\n"
                f"📝 {row['wallet']}\n\n"
            )

        await query.edit_message_text(
            text[:4000]
        )


# ============================================================
# ADMIN BALANCE
# ============================================================

async def add_balance(
    update,
    context
):

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
            context.args[1]
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

    credit_balance(
        target_id,
        amount
    )

    await update.message.reply_text(
        f"✅ موجودی افزایش یافت.\n\n"
        f"👤 {target_id}\n"
        f"➕ {money(amount)}\n"
        f"💰 جدید: {money(get_balance(target_id))}"
    )


async def remove_balance(
    update,
    context
):

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
            context.args[1]
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
        f"➖ {money(amount)}\n"
        f"💰 جدید: {money(get_balance(target_id))}"
    )


# ============================================================
# BLOCK
# ============================================================

async def block_command(
    update,
    context
):

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
    except Exception:
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


async def unblock_command(
    update,
    context
):

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
    except Exception:
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
# JOIN CALLBACK
# ============================================================

async def check_join_callback(
    update,
    context
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ok = await check_membership(
        context.bot,
        user.id
    )

    if ok:

        await query.message.reply_text(
            "✅ عضویت تأیید شد.\n"
            "حالا می‌توانی از ربات استفاده کنی.",
            reply_markup=main_keyboard()
        )

    else:

        await query.answer(
            "❌ هنوز عضو کانال نیستی.",
            show_alert=True
        )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update,
    context
):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    if not await require_join(
        update,
        context
    ):
        return

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

    request_mode = context.user_data.get(
        "request_mode"
    )

    if request_mode == "amount":

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

    # --------------------------------------------------------
    # GAME CREATION
    # --------------------------------------------------------

    parsed = parse_game(
        normalized
    )

    if parsed:

        game_type, rounds, amount = parsed

        await create_game(
            update,
            context,
            game_type,
            rounds,
            amount
        )

        return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if normalized.lower() in (
        "موجودی",
        "موجودی ترون",
        "balance",
        "💰 موجودی",
        "💰موجودی"
    ):

        await show_balance(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # GAME
    # --------------------------------------------------------

    if text in (
        "🎮 بازی",
        "بازی"
    ):

        await game_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # FRIEND
    # --------------------------------------------------------

    if text in (
        "👥 بازی با دوستان",
        "بازی با دوستان"
    ):

        await friends_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    if text in (
        "🤖 بازی با ربات",
        "بازی با ربات"
    ):

        await bot_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # TRANSFER
    # --------------------------------------------------------

    if re.match(
        r"^(انتقال|transfer)\s+\d+(?:[.,]\d+)?$",
        normalized,
        re.IGNORECASE
    ):

        await transfer_command(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # REQUEST
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # REFERRAL
    # --------------------------------------------------------

    if text in (
        "👥 زیرمجموعه",
        "زیرمجموعه",
        "رفرال",
        "referral"
    ):

        await referral_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

    if text in (
        "📖 راهنما",
        "راهنما",
        "help"
    ):

        await help_command(
            update,
            context
        )

        return


# ============================================================
# COMMAND: BALANCE
# ============================================================

async def balance_command(
    update,
    context
):

    await show_balance(
        update,
        context
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    logger.error(
        "UNHANDLED BOT ERROR: %s",
        context.error,
        exc_info=context.error
    )

    # عمداً exception را دوباره raise نمی‌کنیم
    # تا polling متوقف نشود.


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application
):

    # بازی‌های قدیمی به صورت background مدیریت می‌شوند.
    application.create_task(
        recover_stale_games(
            application
        )
    )

    logger.info(
        "Recovery task started"
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
        .post_init(post_init)
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
            balance_command
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
            admin_command
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
            block_command
        )
    )

    application.add_handler(
        CommandHandler(
            "unblock",
            unblock_command
        )
    )

    # --------------------------------------------------------
    # GAME BUTTONS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            check_join_callback,
            pattern=r"^check_join$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            game_help_callback,
            pattern=r"^help_game_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            join_friend,
            pattern=r"^join_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            play_vs_bot,
            pattern=r"^bot_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game,
            pattern=r"^cancel_\d+$"
        )
    )

    # --------------------------------------------------------
    # DICE / BOWLING / BASKETBALL / DARTS
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            process_user_roll
        ),
        group=0
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT &
            ~filters.COMMAND,
            text_handler
        ),
        group=1
    )

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "========================================"
    )

    logger.info(
        "BOT STARTED"
    )

    logger.info(
        "Unlimited game rounds: ENABLED"
    )

    logger.info(
        "Bot game self-roll: ENABLED"
    )

    logger.info(
        "Friend game self-roll: ENABLED"
    )

    logger.info(
        "Atomic balance: ENABLED"
    )

    logger.info(
        "Recovery system: ENABLED"
    )

    logger.info(
        "========================================"
    )

    # --------------------------------------------------------
    # مهم:
    # run_polling خودش event loop را مدیریت می‌کند.
    # آن را داخل asyncio.run قرار نده.
    # --------------------------------------------------------

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=True
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        logger.info(
            "BOT STOPPED BY USER"
        )

    except Exception:
        logger.exception(
            "FATAL STARTUP ERROR"
        )
        raise
