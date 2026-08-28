# ============================================================
# BOT.PY - STABLE TELEGRAM GAME BOT
# Python 3.10+
# python-telegram-bot 20+
#
# امکانات:
# - موجودی بدون موجودی اولیه
# - واحد نمایش TRX
# - بازی تاس / بولینگ / بسکتبال / دارت
# - تعداد بازی نامحدود
# - بازی با دوستان
# - بازی با ربات
# - قفل تراکنش
# - ضد دوباره کم شدن موجودی
# - ضد گیر کردن بازی
# - بازی با ربات: کاربر خودش پرتاب می‌کند، ربات خودش پرتاب می‌کند
# - نتیجه بعد از کامل شدن پرتاب‌ها
# - انتقال با Reply
# - درخواست
# - پنل ادمین
# - جوین اجباری
# - سیستم زیرمجموعه 0.05 TRX
# - بازی‌ها و موجودی در SQLite
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
from telegram.error import (
    TelegramError,
    Forbidden,
    BadRequest,
    NetworkError,
    TimedOut,
)
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

FORCE_JOIN_CHANNEL = "@zobxt"
FORCE_JOIN_LINK = "https://t.me/zobxt"

MIN_GAME = Decimal("0.01")
MAX_GAME = Decimal("1000000000")

OWNER_SHARE = Decimal("0.02")
BOT_FEE = Decimal("0.03")
REFERRAL_REWARD = Decimal("0.05")

WINNER_PAYOUT = Decimal("0.95")

WITHDRAW_ENABLED_DEFAULT = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("game_bot")

DB_LOCK = asyncio.Lock()

# جلوگیری از اجرای همزمان دو فرآیند پرتاب برای یک بازی
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
            referrer_id INTEGER DEFAULT NULL,
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

        # migrations
        user_columns = {
            row["name"]
            for row in con.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        if "referrer_id" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN referrer_id INTEGER DEFAULT NULL"
            )

        if "referral_paid" not in user_columns:
            con.execute(
                "ALTER TABLE users ADD COLUMN referral_paid INTEGER DEFAULT 0"
            )

        con.commit()


# ============================================================
# DECIMAL
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


# ============================================================
# USERS
# ============================================================

def ensure_user(user, referrer_id=None):
    if not user:
        return

    with closing(db()) as con:

        row = con.execute(
            "SELECT * FROM users WHERE user_id=?",
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

            valid_ref = None

            if referrer_id:
                try:
                    referrer_id = int(referrer_id)

                    if referrer_id != user.id:
                        exists = con.execute(
                            "SELECT user_id FROM users WHERE user_id=?",
                            (referrer_id,)
                        ).fetchone()

                        if exists:
                            valid_ref = referrer_id

                except Exception:
                    valid_ref = None

            con.execute("""
            INSERT INTO users
            (user_id, username, first_name, balance,
             referrer_id, referral_paid)
            VALUES (?, ?, ?, '0', ?, 0)
            """, (
                user.id,
                user.username or "",
                user.first_name or "",
                valid_ref
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


def name_of(user):
    if not user:
        return "کاربر"

    if user.first_name:
        return user.first_name

    if user.username:
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

    with closing(db()) as con:

        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                "SELECT owner_balance, fee_balance "
                "FROM house WHERE id=1"
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

            con.execute("COMMIT")

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("HOUSE ERROR")


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
        "1"
    ) == "1"


def is_admin(user_id):
    return user_id in ADMIN_IDS


def is_blocked(user_id):
    row = get_user(user_id)

    return bool(
        row and
        int(row["blocked"]) == 1
    )


# ============================================================
# FORCE JOIN
# ============================================================

async def check_joined(bot, user_id):
    try:

        member = await bot.get_chat_member(
            FORCE_JOIN_CHANNEL,
            user_id
        )

        return member.status in (
            "creator",
            "administrator",
            "member"
        )

    except Exception as e:

        logger.warning(
            "JOIN CHECK ERROR %s",
            e
        )

        # اگر کانال درست تنظیم نشده باشد،
        # کل ربات قفل نمی‌شود.
        return True


async def force_join_message(update, context):
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

    await update.effective_message.reply_text(
        "⛔ برای استفاده از ربات ابتدا عضو کانال شو.",
        reply_markup=keyboard
    )


async def check_access(update, context):
    user = update.effective_user

    if not user:
        return False

    if is_admin(user.id):
        return True

    joined = await check_joined(
        context.bot,
        user.id
    )

    if not joined:
        await force_join_message(
            update,
            context
        )
        return False

    return True


# ============================================================
# PARSERS
# ============================================================

def parse_decimal_amount(text):
    if not text:
        return None

    text = normalize_digits(text)
    text = text.replace("٬", "")
    text = text.replace(",", ".")
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

    amount = parse_decimal_amount(
        match.group(3)
    )

    game = GAME_NAMES.get(game_name)

    if not game or not amount:
        return None

    if rounds < 1:
        return None

    # عمداً هیچ سقفی برای تعداد پرتاب گذاشته نشده.
    return game, rounds, amount


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


# ============================================================
# START
# ============================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    referrer_id = None

    if context.args:
        try:
            referrer_id = int(
                normalize_digits(
                    context.args[0]
                )
            )
        except Exception:
            referrer_id = None

    ensure_user(
        user,
        referrer_id
    )

    if is_blocked(user.id):

        await update.effective_message.reply_text(
            "⛔ دسترسی شما مسدود است."
        )

        return

    if not await check_access(
        update,
        context
    ):
        return

    await update.effective_message.reply_text(
        "👋 سلام!\n\n"
        "🎮 به ربات بازی خوش آمدی.\n\n"
        "💰 واحد موجودی: TRX\n\n"
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

    ensure_user(user)

    if not await check_access(update, context):
        return

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

    if not await check_access(update, context):
        return

    await update.effective_message.reply_text(
        "🎮 بازی را انتخاب کن:",
        reply_markup=game_keyboard()
    )


async def friends_menu(update, context):

    if not await check_access(update, context):
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
        "تعداد بازی و پرتاب محدودیت ندارد."
    )


async def bot_menu(update, context):

    if not await check_access(update, context):
        return

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

    ensure_user(user)

    if not await check_access(
        update,
        context
    ):
        return

    if is_blocked(user.id):
        return

    # قفل قبل از بررسی و برداشت
    async with DB_LOCK:

        if not debit_balance(
            user.id,
            amount
        ):

            await update.effective_message.reply_text(
                f"❌ موجودی کافی نیست.\n"
                f"💰 موجودی: "
                f"{money(get_balance(user.id))} TRX"
            )

            return

        try:

            with closing(db()) as con:

                cur = con.execute("""
                INSERT INTO games
                (chat_id, creator_id, game_type,
                 amount, rounds, status)
                VALUES (?, ?, ?, ?, ?, 'waiting')
                """, (
                    chat.id,
                    user.id,
                    game,
                    str(amount),
                    rounds
                ))

                game_id = cur.lastrowid

                con.commit()

            sent = await context.bot.send_message(
                chat_id=chat.id,
                text=(
                    f"{GAME_LABELS[game]}\n\n"
                    f"🎮 تعداد: {rounds}\n"
                    f"💰 مبلغ: {money(amount)} TRX\n\n"
                    f"👤 سازنده: {name_of(user)}\n\n"
                    f"یکی از گزینه‌ها را انتخاب کن:"
                ),
                reply_markup=game_created_keyboard(
                    game_id
                )
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

            credit_balance(
                user.id,
                amount
            )

            try:
                with closing(db()) as con:
                    con.execute(
                        "DELETE FROM games WHERE id=?",
                        (game_id,)
                    )
                    con.commit()
            except Exception:
                pass

            logger.exception(
                "CREATE GAME ERROR"
            )

            await update.effective_message.reply_text(
                "❌ بازی ساخته نشد.\n"
                "مبلغ به موجودی برگشت داده شد."
            )


# ============================================================
# GAME TYPE CALLBACK
# ============================================================

async def game_callback(update, context):

    query = update.callback_query

    await query.answer()

    if query.data == "check_join":
        if await check_joined(
            context.bot,
            query.from_user.id
        ):
            await query.message.reply_text(
                "✅ عضویت تأیید شد.",
                reply_markup=user_keyboard()
            )
        else:
            await query.answer(
                "❌ هنوز عضو کانال نیستی.",
                show_alert=True
            )
        return

    user = query.from_user

    ensure_user(user)

    if not await check_access(
        update,
        context
    ):
        return

    game = query.data.replace(
        "game_",
        "",
        1
    )

    if game not in GAME_LABELS:
        return

    title = GAME_LABELS[game]

    await query.message.reply_text(
        f"{title}\n\n"
        f"مثال:\n"
        f"1 {title.split(' ', 1)[1]} 0.5\n\n"
        f"تعداد پرتاب محدودیت ندارد."
    )


# ============================================================
# GAME DB
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
        except Exception:
            pass

    return result


def save_game(
    game_id,
    creator_rolls=None,
    opponent_rolls=None,
    status=None,
    winner_id="NOCHANGE"
):

    with closing(db()) as con:

        fields = []
        values = []

        if creator_rolls is not None:
            fields.append("creator_rolls=?")
            values.append(
                ",".join(
                    map(str, creator_rolls)
                )
            )

        if opponent_rolls is not None:
            fields.append("opponent_rolls=?")
            values.append(
                ",".join(
                    map(str, opponent_rolls)
                )
            )

        if status is not None:
            fields.append("status=?")
            values.append(status)

        if winner_id != "NOCHANGE":
            fields.append("winner_id=?")
            values.append(winner_id)

        if not fields:
            return

        values.append(game_id)

        con.execute(
            f"""
            UPDATE games
            SET {", ".join(fields)}
            WHERE id=?
            """,
            tuple(values)
        )

        con.commit()


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
# JOIN FRIEND
# ============================================================

async def join_friend(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    if not await check_access(
        update,
        context
    ):
        return

    try:
        game_id = int(
            query.data.split("_")[1]
        )
    except Exception:
        return

    ensure_user(user)

    async with DB_LOCK:

        with closing(db()) as con:

            try:
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

                new_balance = (
                    D(row["balance"]) - amount
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

    await query.message.reply_text(
        f"👥 حریف وارد بازی شد: {name_of(user)}\n\n"
        f"🎯 ابتدا سازنده، خودش "
        f"{game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} "
        f"بیندازد."
    )


# ============================================================
# JOIN BOT
# ============================================================

async def join_bot(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    if not await check_access(
        update,
        context
    ):
        return

    try:
        game_id = int(
            query.data.split("_")[1]
        )
    except Exception:
        return

    ensure_user(user)

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
                            "❌ این بازی قبلاً شروع شده.",
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

                    # مبلغ قبلاً هنگام ساخت بازی رزرو شده.
                    con.execute("""
                    UPDATE games
                    SET opponent_id=NULL,
                        status='bot_creator_turn'
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
                        "BOT JOIN ERROR"
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
            f"{GAME_EMOJIS[game['game_type']]} "
            f"بینداز."
        )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    try:
        game_id = int(
            query.data.split("_")[1]
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

                    amount = D(
                        game["amount"]
                    )

                    row = con.execute(
                        "SELECT balance FROM users WHERE user_id=?",
                        (user.id,)
                    ).fetchone()

                    if row:

                        new_balance = (
                            D(row["balance"]) + amount
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
                        "CANCEL ERROR"
                    )

                    return

        await remove_game_lock(
            game_id
        )

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
# SAFE SEND DICE
# ============================================================

async def safe_send_dice(
    context,
    chat_id,
    emoji,
    retries=4
):

    last_error = None

    for attempt in range(
        1,
        retries + 1
    ):

        try:

            sent = await context.bot.send_dice(
                chat_id=chat_id,
                emoji=emoji
            )

            if not sent or not sent.dice:
                raise RuntimeError(
                    "Telegram returned no dice"
                )

            return sent.dice.value

        except (
            NetworkError,
            TimedOut
        ) as e:

            last_error = e

            logger.warning(
                "DICE NETWORK ERROR attempt=%s/%s: %s",
                attempt,
                retries,
                e
            )

            await asyncio.sleep(
                min(2 * attempt, 8)
            )

        except (
            Forbidden,
            BadRequest,
            TelegramError
        ) as e:

            last_error = e

            logger.error(
                "DICE TELEGRAM ERROR: %s",
                e
            )

            # خطای تلگرام را دوباره امتحان می‌کنیم
            # ولی بی‌نهایت نه.
            await asyncio.sleep(
                min(attempt, 3)
            )

        except Exception as e:

            last_error = e

            logger.exception(
                "UNKNOWN DICE ERROR"
            )

            await asyncio.sleep(
                min(attempt, 3)
            )

    raise RuntimeError(
        f"send_dice failed: {last_error}"
    )


# ============================================================
# FINISH / REFUND HELPERS
# ============================================================

async def refund_bot_game(
    context,
    game_id,
    user_id,
    amount,
    chat_id,
    reason
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

                    game = con.execute(
                        "SELECT status FROM games WHERE id=?",
                        (game_id,)
                    ).fetchone()

                    if not game:
                        con.execute(
                            "ROLLBACK"
                        )
                        return

                    # فقط یک بار برگشت داده شود.
                    if game["status"] == "finished":
                        con.execute(
                            "ROLLBACK"
                        )
                        return

                    row = con.execute(
                        "SELECT balance FROM users WHERE user_id=?",
                        (user_id,)
                    ).fetchone()

                    if not row:
                        con.execute(
                            "ROLLBACK"
                        )
                        return

                    new_balance = (
                        D(row["balance"]) + D(amount)
                    )

                    con.execute("""
                    UPDATE users
                    SET balance=?
                    WHERE user_id=?
                    """, (
                        str(new_balance),
                        user_id
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='refunded'
                    WHERE id=?
                    """, (
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
                        "REFUND ERROR"
                    )

                    return

    try:

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⚠️ بازی با ربات متوقف شد.\n\n"
                f"👤 {user_id}\n"
                f"💰 مبلغ {money(amount)} TRX "
                f"برگشت داده شد.\n\n"
                f"دلیل: {reason}"
            )
        )

    except Exception:
        logger.exception(
            "REFUND MESSAGE ERROR"
        )

    await remove_game_lock(
        game_id
    )


# ============================================================
# CALCULATION
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
# FINISH BOT GAME
# ============================================================

async def finish_bot_game(
    context,
    game_id,
    user_rolls,
    bot_rolls
):

    lock = await get_game_lock(
        game_id
    )

    async with lock:

        game = get_game(game_id)

        if not game:
            return

        if game["status"] == "finished":
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

        # مساوی
        if result == 0:

            async with DB_LOCK:

                credit_balance(
                    user_id,
                    amount
                )

                save_game(
                    game_id,
                    creator_rolls=user_rolls,
                    opponent_rolls=bot_rolls,
                    status="finished",
                    winner_id=None
                )

            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    f"🤝 مساوی شد!\n\n"
                    f"👤 {user_id}: {user_score}\n"
                    f"🤖 ربات: {bot_score}\n\n"
                    f"💰 {money(amount)} TRX "
                    f"برگشت داده شد."
                )
            )

            await remove_game_lock(
                game_id
            )

            return

        # کاربر برد
        if result == 1:

            payout = (
                amount * 2
                - OWNER_SHARE
                - BOT_FEE
            )

            async with DB_LOCK:

                credit_balance(
                    user_id,
                    payout
                )

                add_house(
                    OWNER_SHARE,
                    BOT_FEE
                )

                save_game(
                    game_id,
                    creator_rolls=user_rolls,
                    opponent_rolls=bot_rolls,
                    status="finished",
                    winner_id=user_id
                )

            result_text = (
                f"🏆 برنده: 👤 {user_id}\n"
                f"💰 دریافتی: {money(payout)} TRX"
            )

        # ربات برد
        else:

            async with DB_LOCK:

                add_house(
                    amount,
                    Decimal("0")
                )

                save_game(
                    game_id,
                    creator_rolls=user_rolls,
                    opponent_rolls=bot_rolls,
                    status="finished",
                    winner_id=None
                )

            result_text = (
                "🏆 برنده: 🤖 ربات\n"
                "💰 این دور پرداختی به کاربر ندارد."
            )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🏆 نتیجه بازی\n\n"
                f"👤 {user_id}: {user_score}\n"
                f"🤖 ربات: {bot_score}\n\n"
                f"{result_text}"
            )
        )

        await remove_game_lock(
            game_id
        )


# ============================================================
# FINISH FRIEND
# ============================================================

async def finish_friend_game(
    context,
    game_id,
    creator_rolls,
    opponent_rolls
):

    lock = await get_game_lock(
        game_id
    )

    async with lock:

        game = get_game(
            game_id
        )

        if not game:
            return

        if game["status"] == "finished":
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

        # مساوی
        if result == 0:

            async with DB_LOCK:

                credit_balance(
                    creator_id,
                    amount
                )

                credit_balance(
                    opponent_id,
                    amount
                )

                save_game(
                    game_id,
                    creator_rolls,
                    opponent_rolls,
                    "finished",
                    None
                )

            await context.bot.send_message(
                chat_id=game["chat_id"],
                text=(
                    f"🤝 مساوی شد!\n\n"
                    f"👤 {creator_id}: {score_creator}\n"
                    f"👤 {opponent_id}: {score_opponent}\n\n"
                    f"💰 مبلغ هر دو نفر برگشت داده شد."
                )
            )

            await remove_game_lock(
                game_id
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
            amount * 2
            - OWNER_SHARE
            - BOT_FEE
        )

        async with DB_LOCK:

            credit_balance(
                winner_id,
                payout
            )

            add_house(
                OWNER_SHARE,
                BOT_FEE
            )

            save_game(
                game_id,
                creator_rolls,
                opponent_rolls,
                "finished",
                winner_id
            )

        await context.bot.send_message(
            chat_id=game["chat_id"],
            text=(
                f"🏆 نتیجه بازی\n\n"
                f"👤 {creator_id}: {score_creator}\n"
                f"👤 {opponent_id}: {score_opponent}\n\n"
                f"🏆 برنده: {winner_id}\n"
                f"🎯 امتیاز برنده: {winner_score}\n"
                f"🎯 امتیاز حریف: {loser_score}\n\n"
                f"💰 دریافتی برنده: "
                f"{money(payout)} TRX"
            )
        )

        await remove_game_lock(
            game_id
        )


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

    if not await check_access(
        update,
        context
    ):
        return

    game = None

    # فقط بازی همان گپ
    with closing(db()) as con:

        rows = con.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
        AND status IN (
            'bot_creator_turn',
            'creator_turn',
            'opponent_turn',
            'bot_rolling'
        )
        ORDER BY id DESC
        LIMIT 100
        """, (
            message.chat_id,
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

        elif row["status"] == "bot_rolling":

            # ربات در حال انداختن است.
            await message.reply_text(
                f"🤖 ربات درحال بازی است؛ "
                f"فعلاً پرتاب جدید نکن."
            )
            return

    if not game:
        return

    game_type = game["game_type"]

    expected_emoji = GAME_EMOJIS[
        game_type
    ]

    if dice.emoji != expected_emoji:

        await message.reply_text(
            f"❌ برای این بازی باید "
            f"{expected_emoji} بفرستی."
        )

        return

    game_id = int(
        game["id"]
    )

    lock = await get_game_lock(
        game_id
    )

    # ========================================================
    # ضد پرتاب همزمان
    # ========================================================

    async with lock:

        game = get_game(game_id)

        if not game:
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
        # BOT GAME
        # ====================================================

        if game["status"] == "bot_creator_turn":

            if user.id != int(
                game["creator_id"]
            ):
                return

            if len(creator_rolls) >= rounds:
                return

            creator_rolls.append(
                int(dice.value)
            )

            save_game(
                game_id,
                creator_rolls=creator_rolls
            )

            remaining = (
                rounds -
                len(creator_rolls)
            )

            if remaining > 0:

                await message.reply_text(
                    f"👤 {name_of(user)}: "
                    f"{dice.value}\n\n"
                    f"🎯 {remaining} پرتاب باقی مانده."
                )

                return

            # -----------------------------------------------
            # تمام پرتاب‌های کاربر انجام شد.
            # -----------------------------------------------

            save_game(
                game_id,
                creator_rolls=creator_rolls,
                status="bot_rolling"
            )

            await message.reply_text(
                f"🤖 {name_of(user)} تمام پرتاب‌ها را انجام داد.\n\n"
                f"🤖 حالا ربات خودش "
                f"{rounds} بار "
                f"{expected_emoji} می‌اندازد..."
            )

            bot_rolls = []

            try:

                for index in range(rounds):

                    # اگر بازی قبلاً تمام شده، متوقف شو
                    current = get_game(
                        game_id
                    )

                    if not current:
                        return

                    if current["status"] != "bot_rolling":
                        return

                    value = await safe_send_dice(
                        context,
                        message.chat_id,
                        expected_emoji,
                        retries=4
                    )

                    bot_rolls.append(
                        int(value)
                    )

                    # ذخیره بعد از هر پرتاب
                    save_game(
                        game_id,
                        opponent_rolls=bot_rolls
                    )

                    await asyncio.sleep(
                        0.7
                    )

                # -------------------------------------------
                # حتماً نتیجه را بعد از آخرین پرتاب اعلام کن
                # -------------------------------------------

                save_game(
                    game_id,
                    opponent_rolls=bot_rolls,
                    status="finished"
                )

                # finish دوباره بازی را بررسی می‌کند
                await finish_bot_game(
                    context,
                    game_id,
                    creator_rolls,
                    bot_rolls
                )

            except Exception as e:

                logger.exception(
                    "BOT ROLLING CRASH game=%s",
                    game_id
                )

                # بازی گیر نکند؛ پول برگردد
                await refund_bot_game(
                    context,
                    game_id,
                    int(game["creator_id"]),
                    D(game["amount"]),
                    message.chat_id,
                    "ربات نتوانست پرتاب را کامل کند."
                )

            return

        # ====================================================
        # FRIEND CREATOR
        # ====================================================

        if game["status"] == "creator_turn":

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

                save_game(
                    game_id,
                    creator_rolls=creator_rolls
                )

                await message.reply_text(
                    f"👤 {name_of(user)}: "
                    f"{dice.value}\n"
                    f"🎯 {rounds-len(creator_rolls)} "
                    f"پرتاب باقی مانده."
                )

                return

            save_game(
                game_id,
                creator_rolls=creator_rolls,
                status="opponent_turn"
            )

            opponent_id = int(
                game["opponent_id"]
            )

            try:

                member = await context.bot.get_chat(
                    opponent_id
                )

                opponent_name = (
                    member.first_name
                    or member.username
                    or str(opponent_id)
                )

            except Exception:

                opponent_name = str(
                    opponent_id
                )

            await message.reply_text(
                f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
                f"🎯 حالا {opponent_name} خودش "
                f"{rounds} بار "
                f"{expected_emoji} بیندازد."
            )

            return

        # ====================================================
        # FRIEND OPPONENT
        # ====================================================

        if game["status"] == "opponent_turn":

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

                save_game(
                    game_id,
                    opponent_rolls=opponent_rolls
                )

                await message.reply_text(
                    f"👤 {name_of(user)}: "
                    f"{dice.value}\n"
                    f"🎯 {rounds-len(opponent_rolls)} "
                    f"پرتاب باقی مانده."
                )

                return

            save_game(
                game_id,
                opponent_rolls=opponent_rolls,
                status="finished"
            )

            try:

                await finish_friend_game(
                    context,
                    game_id,
                    creator_rolls,
                    opponent_rolls
                )

            except Exception:

                logger.exception(
                    "FRIEND FINISH ERROR"
                )

                await message.reply_text(
                    "⚠️ نتیجه بازی با خطا مواجه شد؛ "
                    "لطفاً ادمین وضعیت بازی را بررسی کند."
                )

            return


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):

    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    if not await check_access(
        update,
        context
    ):
        return

    if not message.reply_to_message:

        await message.reply_text(
            "💸 روی پیام کاربر Reply کن.\n\n"
            "مثال:\n"
            "انتقال 0.5"
        )

        return

    target = message.reply_to_message.from_user

    if not target or target.id == user.id:

        await message.reply_text(
            "❌ مقصد نامعتبر است."
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
            "❌ مقدار نامعتبر."
        )

        return

    ensure_user(target)

    async with DB_LOCK:

        if not debit_balance(
            user.id,
            amount
        ):

            await message.reply_text(
                "❌ موجودی کافی نیست."
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

    if not await check_access(
        update,
        context
    ):
        return

    context.user_data["request_mode"] = "amount"

    await update.effective_message.reply_text(
        "📤 درخواست\n\n"
        "مثال:\n"
        "درخواست 5\n\n"
        "بعد از ثبت مقدار، اطلاعات درخواست را بفرست."
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

async def help_command(update, context):

    if not await check_access(
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
        "تعداد بازی و پرتاب محدودیت ندارد."
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
        ],
        [
            InlineKeyboardButton(
                "🔄 وضعیت برداشت",
                callback_data="admin_withdraw_toggle"
            )
        ]
    ])


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

        for i, row in enumerate(
            rows,
            1
        ):

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

            active = con.execute("""
            SELECT COUNT(*)
            FROM games
            WHERE status NOT IN
            ('finished','cancelled','refunded')
            """).fetchone()[0]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users:,}\n"
            f"💰 موجودی کاربران: {money(total)} TRX\n"
            f"🎮 کل بازی‌ها: {games:,}\n"
            f"🔥 بازی‌های فعال: {active:,}\n"
            f"📥 برداشت: "
            f"{'روشن 🟢' if withdraw_enabled() else 'خاموش 🔴'}"
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
            "/addbalance USER_ID AMOUNT"
        )

        return

    if data == "admin_remove":

        await query.edit_message_text(
            "➖ کاهش موجودی\n\n"
            "/removebalance USER_ID AMOUNT"
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
# ADMIN BALANCE
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
            context.args[1]
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
        f"✅ افزایش انجام شد.\n\n"
        f"👤 {target_id}\n"
        f"➕ {money(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
    )


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
            context.args[1]
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
        f"✅ کاهش انجام شد.\n\n"
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

    if not await check_access(
        update,
        context
    ):
        return

    text = message.text.strip()
    normalized = normalize_digits(text)

    # --------------------------------------------------------
    # درخواست
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
    # ساخت بازی
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # موجودی
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # بازی
    # --------------------------------------------------------

    if text == "🎮 بازی":

        await game_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # دوستان
    # --------------------------------------------------------

    if text == "👥 بازی با دوستان":

        await friends_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # ربات
    # --------------------------------------------------------

    if text == "🤖 بازی با ربات":

        await bot_menu(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # انتقال
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # درخواست
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
    # راهنما
    # --------------------------------------------------------

    if text == "📖 راهنما":

        await help_command(
            update,
            context
        )

        return


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def action_callback(update, context):

    data = update.callback_query.data

    if data == "check_join":
        await game_callback(
            update,
            context
        )
        return

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
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    error = context.error

    logger.error(
        "HANDLER ERROR: %r",
        error,
        exc_info=(
            type(error),
            error,
            error.__traceback__
        ) if error else None
    )

    # مهم:
    # خطای یک آپدیت نباید باعث خاموش شدن کل برنامه شود.
    return


# ============================================================
# CLEANUP STUCK GAMES
# ============================================================

async def cleanup_stuck_games(
    context
):

    """
    بازی‌های bot_rolling که بعد از crash
    در دیتابیس گیر کرده‌اند را بررسی می‌کند.

    اگر مدت زیادی از ایجاد بازی گذشته باشد،
    مبلغ کاربر برگردانده می‌شود.
    """

    try:

        with closing(db()) as con:

            rows = con.execute("""
            SELECT *
            FROM games
            WHERE status='bot_rolling'
            AND created_at <= datetime(
                'now',
                '-10 minutes'
            )
            LIMIT 50
            """).fetchall()

        for game in rows:

            try:

                await refund_bot_game(
                    context,
                    int(game["id"]),
                    int(game["creator_id"]),
                    D(game["amount"]),
                    int(game["chat_id"]),
                    "بازی بیش از حد طولانی شد."
                )

            except Exception:

                logger.exception(
                    "CLEANUP GAME ERROR"
                )

    except Exception:

        logger.exception(
            "CLEANUP ERROR"
        )


# ============================================================
# POST INIT
# ============================================================

async def post_init(
    application
):

    logger.info(
        "BOT INITIALIZED"
    )


# ============================================================
# POST SHUTDOWN
# ============================================================

async def post_shutdown(
    application
):

    logger.info(
        "BOT SHUTDOWN"
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
        .post_shutdown(post_shutdown)
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

    # --------------------------------------------------------
    # FORCE JOIN / GAME BUTTONS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            game_callback,
            pattern=r"^game_"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            action_callback,
            pattern=r"^(join_|bot_|cancel_|check_join)$"
        )
    )

    # --------------------------------------------------------
    # ADMIN
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_"
        )
    )

    # --------------------------------------------------------
    # DICE / BOWLING / BASKETBALL / DART
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
            filters.TEXT & ~filters.COMMAND,
            text_handler
        ),
        group=1
    )

    # --------------------------------------------------------
    # ERROR HANDLER
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    # --------------------------------------------------------
    # CLEANUP JOB
    # --------------------------------------------------------

    if application.job_queue:

        application.job_queue.run_repeating(
            cleanup_stuck_games,
            interval=60,
            first=60
        )

    logger.info(
        "🚀 BOT STARTED"
    )

    # retry بی‌نهایت در مرحله اتصال
    # و مدیریت صحیح polling
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        bootstrap_retries=-1,
        poll_interval=0.5,
        timeout=20,
        read_timeout=20,
        write_timeout=20,
        connect_timeout=20,
        pool_timeout=20
    )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
