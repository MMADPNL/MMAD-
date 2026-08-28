# ============================================================
# BOT.PY - Telegram Internal Credit Games
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

ADMIN_IDS = {8552447077}

DB_FILE = "bot.db"

MIN_GAME = Decimal("0.01")
MAX_GAME = Decimal("1000000000")

OWNER_SHARE = Decimal("0.02")
BOT_FEE = Decimal("0.03")
REFERRAL_REWARD = Decimal("0.05")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger(__name__)

# قفل کلی عملیات بازی
GAME_LOCK = asyncio.Lock()

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
            referral_paid INTEGER DEFAULT 0,
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
            status TEXT NOT NULL DEFAULT 'waiting',
            winner_id INTEGER DEFAULT NULL,
            refunded INTEGER DEFAULT 0,
            settled INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
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
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            referrer_id INTEGER,
            referred_id INTEGER UNIQUE,
            reward TEXT DEFAULT '0.05',
            paid INTEGER DEFAULT 0,
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

        # مهاجرت برای دیتابیس نسخه قدیمی
        cols = {
            r["name"]
            for r in con.execute(
                "PRAGMA table_info(users)"
            ).fetchall()
        }

        migrations = {
            "referred_by": "ALTER TABLE users ADD COLUMN referred_by INTEGER DEFAULT NULL",
            "referral_paid": "ALTER TABLE users ADD COLUMN referral_paid INTEGER DEFAULT 0",
        }

        for col, sql in migrations.items():
            if col not in cols:
                con.execute(sql)

        game_cols = {
            r["name"]
            for r in con.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        }

        game_migrations = {
            "refunded":
                "ALTER TABLE games ADD COLUMN refunded INTEGER DEFAULT 0",

            "settled":
                "ALTER TABLE games ADD COLUMN settled INTEGER DEFAULT 0",
        }

        for col, sql in game_migrations.items():
            if col not in game_cols:
                con.execute(sql)

        con.commit()


# ============================================================
# HELPERS
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
    text = text.strip().replace(",", ".")

    try:
        value = Decimal(text)
    except InvalidOperation:
        return None

    if value < MIN_GAME or value > MAX_GAME:
        return None

    return value.quantize(
        Decimal("0.01"),
        rounding=ROUND_DOWN
    )


def name_of(user):
    if not user:
        return "کاربر"

    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


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
            # موجودی اولیه صفر است
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

            balance = D(row["balance"])

            if balance < amount:
                con.execute("ROLLBACK")
                return False

            new_balance = balance - amount

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

            new_balance = D(row["balance"]) + amount

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
# SAFE GAME REFUND
# ============================================================

def refund_game_once(game_id):
    """
    مهم:
    اگر بازی قبلاً برگشت داده شده باشد دوباره پرداخت نمی‌کند.
    """

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            game = con.execute("""
            SELECT *
            FROM games
            WHERE id=?
            """, (game_id,)).fetchone()

            if not game:
                con.execute("ROLLBACK")
                return False

            if int(game["refunded"]) == 1:
                con.execute("ROLLBACK")
                return False

            creator_id = int(game["creator_id"])
            amount = D(game["amount"])

            # مبلغ سازنده همیشه ابتدا رزرو شده است
            con.execute("""
            UPDATE users
            SET balance=CAST(balance AS REAL) + ?
            WHERE user_id=?
            """, (
                float(amount),
                creator_id
            ))

            # اگر حریف هم وارد شده، مبلغ او نیز برگشت
            if game["opponent_id"] is not None:
                opponent_id = int(game["opponent_id"])

                con.execute("""
                UPDATE users
                SET balance=CAST(balance AS REAL) + ?
                WHERE user_id=?
                """, (
                    float(amount),
                    opponent_id
                ))

            con.execute("""
            UPDATE games
            SET refunded=1,
                settled=1,
                status='refunded'
            WHERE id=?
            """, (game_id,))

            con.commit()
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("REFUND ERROR")
            return False


# ============================================================
# HOUSE
# ============================================================

def add_house(owner, fee):
    owner = D(owner)
    fee = D(fee)

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            row = con.execute("""
            SELECT owner_balance, fee_balance
            FROM house
            WHERE id=1
            """).fetchone()

            current_owner = D(row["owner_balance"])
            current_fee = D(row["fee_balance"])

            con.execute("""
            UPDATE house
            SET owner_balance=?,
                fee_balance=?
            WHERE id=1
            """, (
                str(current_owner + owner),
                str(current_fee + fee)
            ))

            con.commit()

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("HOUSE ERROR")


# ============================================================
# REFERRAL
# ============================================================

def set_referrer(user_id, referrer_id):
    if user_id == referrer_id:
        return False

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            user = con.execute("""
            SELECT referred_by
            FROM users
            WHERE user_id=?
            """, (user_id,)).fetchone()

            referrer = con.execute("""
            SELECT user_id
            FROM users
            WHERE user_id=?
            """, (referrer_id,)).fetchone()

            if not user or not referrer:
                con.execute("ROLLBACK")
                return False

            if user["referred_by"] is not None:
                con.execute("ROLLBACK")
                return False

            con.execute("""
            UPDATE users
            SET referred_by=?
            WHERE user_id=?
            """, (
                referrer_id,
                user_id
            ))

            con.execute("""
            INSERT OR IGNORE INTO referrals
            (referrer_id, referred_id, reward, paid)
            VALUES (?, ?, ?, 0)
            """, (
                referrer_id,
                user_id,
                str(REFERRAL_REWARD)
            ))

            con.commit()
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            return False


def pay_referral_once(user_id):
    """
    پاداش 0.05 فقط یک بار برای هر زیرمجموعه.
    """

    with closing(db()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")

            ref = con.execute("""
            SELECT *
            FROM referrals
            WHERE referred_id=?
            """, (user_id,)).fetchone()

            if not ref:
                con.execute("ROLLBACK")
                return False

            if int(ref["paid"]) == 1:
                con.execute("ROLLBACK")
                return False

            referrer_id = int(ref["referrer_id"])
            reward = D(ref["reward"])

            con.execute("""
            UPDATE users
            SET balance=CAST(balance AS REAL) + ?
            WHERE user_id=?
            """, (
                float(reward),
                referrer_id
            ))

            con.execute("""
            UPDATE referrals
            SET paid=1
            WHERE referred_id=?
            """, (user_id,))

            con.execute("""
            UPDATE users
            SET referral_paid=1
            WHERE user_id=?
            """, (user_id,))

            con.commit()
            return True

        except Exception:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass

            logger.exception("REFERRAL ERROR")
            return False


# ============================================================
# PARSE GAME
# ============================================================

def parse_game(text):
    text = normalize_digits(text or "").strip()

    # مثال:
    # 1 تاس 0.5
    # 2 تاس 0.1
    # 100 بولینگ 1
    pattern = re.compile(
        r"^(\d+)\s+([^\s]+)\s+(\d+(?:[.,]\d+)?)$",
        re.IGNORECASE
    )

    m = pattern.match(text)

    if not m:
        return None

    rounds = int(m.group(1))
    game_name = m.group(2).lower()

    game = GAME_NAMES.get(game_name)

    if not game or rounds < 1:
        return None

    amount = parse_decimal_amount(
        m.group(3).replace(",", ".")
    )

    if amount is None:
        return None

    # هیچ سقفی برای تعداد پرتاب تعیین نشده
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
            ["🔗 زیرمجموعه", "📖 راهنما"],
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
            ),
        ],
        [
            InlineKeyboardButton(
                "🏀 بسکتبال",
                callback_data="game_basketball"
            ),
            InlineKeyboardButton(
                "🎯 دارت",
                callback_data="game_darts"
            ),
        ],
    ])


def created_keyboard(game_id):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join_{game_id}"
            ),
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_{game_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel_{game_id}"
            )
        ],
    ])


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    # /start REFERRER_ID
    if context.args:
        try:
            referrer_id = int(
                normalize_digits(context.args[0])
            )

            if referrer_id != user.id:
                set_referrer(
                    user.id,
                    referrer_id
                )

        except Exception:
            pass

    if is_blocked(user.id):
        await update.effective_message.reply_text(
            "⛔ دسترسی شما مسدود است."
        )
        return

    # پاداش زیرمجموعه فقط یک بار
    pay_referral_once(user.id)

    await update.effective_message.reply_text(
        "👋 سلام!\n\n"
        "🎮 به ربات بازی خوش آمدی.",
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

    if is_blocked(user.id):
        return

    await update.effective_message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"{money(get_balance(user.id))} TRX\n\n"
        f"این مقدار اعتبار داخلی ربات است."
    )


# ============================================================
# GAME MENU
# ============================================================

async def game_menu(update, context):
    await update.effective_message.reply_text(
        "🎮 نوع بازی را انتخاب کن:",
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
        "2 بسکتبال 0.5\n"
        "3 دارت 0.5\n\n"
        "بعد از شروع، اول خودت ایموجی بازی را بفرست."
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context, game, rounds, amount):
    user = update.effective_user
    chat = update.effective_chat

    ensure_user(user)

    if get_balance(user.id) < amount:
        await update.effective_message.reply_text(
            "❌ اعتبار کافی نیست."
        )
        return

    # قفل مبلغ سازنده
    if not debit_balance(user.id, amount):
        await update.effective_message.reply_text(
            "❌ موجودی تغییر کرده؛ دوباره تلاش کن."
        )
        return

    try:
        sent = await context.bot.send_message(
            chat_id=chat.id,
            text=(
                f"{GAME_LABELS[game]}\n\n"
                f"🔢 تعداد پرتاب: {rounds}\n"
                f"💰 مبلغ: {money(amount)} TRX\n"
                f"👤 سازنده: {name_of(user)}\n\n"
                f"یکی از گزینه‌ها را انتخاب کن."
            ),
            reply_markup=created_keyboard(0)
        )

        with closing(db()) as con:
            cur = con.execute("""
            INSERT INTO games
            (
                chat_id,
                message_id,
                creator_id,
                game_type,
                amount,
                rounds,
                status
            )
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
            reply_markup=created_keyboard(game_id)
        )

    except Exception:
        credit_balance(user.id, amount)

        logger.exception("CREATE GAME ERROR")

        await update.effective_message.reply_text(
            "❌ بازی ساخته نشد؛ مبلغ به شما برگشت داده شد."
        )


# ============================================================
# GAME BUTTON
# ============================================================

async def game_button(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user

    ensure_user(user)

    game = query.data.replace(
        "game_",
        "",
        1
    )

    if game not in GAME_LABELS:
        return

    label = GAME_LABELS[game]

    await query.message.reply_text(
        f"{label}\n\n"
        f"مثال:\n"
        f"1 {label.split(' ', 1)[1]} 0.5\n\n"
        f"تعداد پرتاب محدودیت ندارد."
    )


# ============================================================
# GET GAME
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


def save_rolls(
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

    async with GAME_LOCK:

        with closing(db()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (game_id,)).fetchone()

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
                        "❌ این بازی دیگر فعال نیست.",
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

                row = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (user.id,)).fetchone()

                if not row or D(row["balance"]) < amount:
                    con.execute("ROLLBACK")
                    await query.answer(
                        "❌ اعتبار کافی نیست.",
                        show_alert=True
                    )
                    return

                # قفل مبلغ حریف
                new_balance = D(row["balance"]) - amount

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

                con.commit()

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception("JOIN FRIEND ERROR")

                await query.answer(
                    "❌ خطا؛ دوباره تلاش کن.",
                    show_alert=True
                )
                return

    # حذف دکمه‌ها
    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await query.message.reply_text(
        f"👥 بازی شروع شد.\n\n"
        f"👤 اول سازنده، {creator_id}، "
        f"خودش {game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرستد.\n\n"
        f"بعد از آن حریف نوبت خود را انجام می‌دهد."
    )


# ============================================================
# JOIN BOT
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

    async with GAME_LOCK:

        with closing(db()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (game_id,)).fetchone()

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
                        "❌ بازی دیگر فعال نیست.",
                        show_alert=True
                    )
                    return

                if int(game["creator_id"]) != user.id:
                    con.execute("ROLLBACK")

                    await query.answer(
                        "❌ فقط سازنده می‌تواند با ربات بازی کند.",
                        show_alert=True
                    )
                    return

                # مبلغ قبلاً هنگام ساخت رزرو شده.
                con.execute("""
                UPDATE games
                SET status='bot_creator_turn'
                WHERE id=?
                """, (game_id,))

                con.commit()

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception("BOT JOIN ERROR")

                await query.answer(
                    "❌ خطا در شروع بازی.",
                    show_alert=True
                )
                return

    # حذف همه دکمه‌ها
    try:
        await query.message.edit_reply_markup(
            reply_markup=None
        )
    except Exception:
        pass

    await query.message.reply_text(
        f"🤖 بازی با ربات شروع شد.\n\n"
        f"👤 {name_of(user)} باید اول خودش "
        f"{game['rounds']} بار "
        f"{GAME_EMOJIS[game['game_type']]} بفرستد.\n\n"
        f"⏳ بعد از تمام شدن نوبت تو، ربات خودش بازی می‌کند."
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
            "❌ بازی نامعتبر.",
            show_alert=True
        )
        return

    async with GAME_LOCK:

        with closing(db()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (game_id,)).fetchone()

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

                con.execute("""
                UPDATE users
                SET balance=CAST(balance AS REAL) + ?
                WHERE user_id=?
                """, (
                    float(amount),
                    user.id
                ))

                con.execute("""
                UPDATE games
                SET status='cancelled',
                    refunded=1,
                    settled=1
                WHERE id=?
                """, (game_id,))

                con.commit()

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                await query.answer(
                    "❌ خطا در لغو.",
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
# RESULT
# ============================================================

def score(rolls):
    return sum(rolls)


def result(a, b):
    if a > b:
        return 1
    if b > a:
        return 2
    return 0


# ============================================================
# FINISH BOT GAME - ATOMIC SETTLEMENT
# ============================================================

async def finish_bot_game(
    context,
    game_id,
    user_rolls,
    bot_rolls
):
    """
    مهم‌ترین ضدباگ:

    - بازی فقط یک بار settle می‌شود.
    - پرداخت دوباره امکان ندارد.
    - در صورت خطای تراکنش، مبلغ گم نمی‌شود.
    """

    async with GAME_LOCK:

        with closing(db()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (game_id,)).fetchone()

                if not game:
                    con.execute("ROLLBACK")
                    return False

                # قبلاً پایان یافته؟
                if int(game["settled"]) == 1:
                    con.execute("ROLLBACK")
                    return False

                user_id = int(game["creator_id"])
                amount = D(game["amount"])

                user_score = score(user_rolls)
                bot_score = score(bot_rolls)

                r = result(
                    user_score,
                    bot_score
                )

                # مساوی
                if r == 0:

                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL) + ?
                    WHERE user_id=?
                    """, (
                        float(amount),
                        user_id
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=NULL,
                        opponent_rolls=?,
                        settled=1
                    WHERE id=?
                    """, (
                        ",".join(map(str, bot_rolls)),
                        game_id
                    ))

                    con.commit()

                    winner_text = "🤝 مساوی"
                    payout = amount

                # کاربر برد
                elif r == 1:

                    payout = (
                        amount * Decimal("2")
                        - OWNER_SHARE
                        - BOT_FEE
                    )

                    if payout < 0:
                        payout = Decimal("0")

                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL) + ?
                    WHERE user_id=?
                    """, (
                        float(payout),
                        user_id
                    ))

                    con.execute("""
                    UPDATE house
                    SET owner_balance=
                            CAST(owner_balance AS REAL) + ?,
                        fee_balance=
                            CAST(fee_balance AS REAL) + ?
                    WHERE id=1
                    """, (
                        float(OWNER_SHARE),
                        float(BOT_FEE)
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=?,
                        opponent_rolls=?,
                        settled=1
                    WHERE id=?
                    """, (
                        user_id,
                        ",".join(map(str, bot_rolls)),
                        game_id
                    ))

                    con.commit()

                    winner_text = f"👤 {user_id}"

                # ربات برد
                else:

                    # مبلغ رزرو شده وارد موجودی داخلی خانه می‌شود
                    con.execute("""
                    UPDATE house
                    SET owner_balance=
                            CAST(owner_balance AS REAL) + ?
                    WHERE id=1
                    """, (
                        float(amount),
                    ))

                    con.execute("""
                    UPDATE games
                    SET status='finished',
                        winner_id=NULL,
                        opponent_rolls=?,
                        settled=1
                    WHERE id=?
                    """, (
                        ",".join(map(str, bot_rolls)),
                        game_id
                    ))

                    con.commit()

                    winner_text = "🤖 ربات"
                    payout = Decimal("0")

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "BOT SETTLEMENT ERROR"
                )

                # برگشت امن در یک عملیات جدا
                refunded = refund_game_once(game_id)

                return False if not refunded else True

    game = get_game(game_id)

    if not game:
        return False

    user_id = int(game["creator_id"])

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            "🏆 نتیجه بازی\n\n"
            f"👤 {user_id}: {user_score}\n"
            f"🤖 ربات: {bot_score}\n\n"
            f"🏆 برنده: {winner_text}\n\n"
            + (
                f"💰 دریافتی: {money(payout)} TRX"
                if r == 1
                else
                f"💰 مبلغ برگشتی: {money(payout)} TRX"
                if r == 0
                else
                "🤖 ربات برنده شد."
            )
        )
    )

    return True


# ============================================================
# FINISH FRIEND GAME
# ============================================================

async def finish_friend_game(
    context,
    game_id,
    creator_rolls,
    opponent_rolls
):

    async with GAME_LOCK:

        with closing(db()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")

                game = con.execute("""
                SELECT *
                FROM games
                WHERE id=?
                """, (game_id,)).fetchone()

                if not game:
                    con.execute("ROLLBACK")
                    return

                if int(game["settled"]) == 1:
                    con.execute("ROLLBACK")
                    return

                creator_id = int(game["creator_id"])
                opponent_id = int(game["opponent_id"])
                amount = D(game["amount"])

                creator_score = score(
                    creator_rolls
                )

                opponent_score = score(
                    opponent_rolls
                )

                r = result(
                    creator_score,
                    opponent_score
                )

                if r == 0:

                    # هر دو مبلغ خودشان را پس می‌گیرند
                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL) + ?
                    WHERE user_id=?
                    """, (
                        float(amount),
                        creator_id
                    ))

                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL) + ?
                    WHERE user_id=?
                    """, (
                        float(amount),
                        opponent_id
                    ))

                    winner_id = None

                    payout = amount

                else:

                    if r == 1:
                        winner_id = creator_id
                    else:
                        winner_id = opponent_id

                    payout = (
                        amount * Decimal("2")
                        - OWNER_SHARE
                        - BOT_FEE
                    )

                    if payout < 0:
                        payout = Decimal("0")

                    con.execute("""
                    UPDATE users
                    SET balance=CAST(balance AS REAL) + ?
                    WHERE user_id=?
                    """, (
                        float(payout),
                        winner_id
                    ))

                    con.execute("""
                    UPDATE house
                    SET owner_balance=
                            CAST(owner_balance AS REAL) + ?,
                        fee_balance=
                            CAST(fee_balance AS REAL) + ?
                    WHERE id=1
                    """, (
                        float(OWNER_SHARE),
                        float(BOT_FEE)
                    ))

                con.execute("""
                UPDATE games
                SET status='finished',
                    winner_id=?,
                    settled=1
                WHERE id=?
                """, (
                    winner_id,
                    game_id
                ))

                con.commit()

            except Exception:
                try:
                    con.execute("ROLLBACK")
                except Exception:
                    pass

                logger.exception(
                    "FRIEND SETTLEMENT ERROR"
                )

                refund_game_once(game_id)
                return

    if r == 0:
        winner_text = "🤝 مساوی"
    else:
        winner_text = f"🏆 {winner_id}"

    await context.bot.send_message(
        chat_id=game["chat_id"],
        text=(
            "🏆 نتیجه بازی\n\n"
            f"👤 {creator_id}: {creator_score}\n"
            f"👤 {opponent_id}: {opponent_score}\n\n"
            f"🏆 نتیجه: {winner_text}\n"
            + (
                f"💰 دریافتی برنده: {money(payout)} TRX"
                if r != 0
                else
                f"💰 مبلغ هر دو نفر برگشت داده شد: "
                f"{money(payout)} TRX"
            )
        )
    )


# ============================================================
# USER ROLL
# ============================================================

async def process_user_roll(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user or not message.dice:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    dice = message.dice

    # --------------------------------------------------------
    # پیدا کردن بازی فعال مخصوص همین کاربر و همین گپ
    # --------------------------------------------------------

    with closing(db()) as con:

        games = con.execute("""
        SELECT *
        FROM games
        WHERE chat_id=?
          AND status IN (
              'bot_creator_turn',
              'creator_turn',
              'opponent_turn'
          )
        ORDER BY id DESC
        LIMIT 200
        """, (
            message.chat_id,
        )).fetchall()

    game = None

    for g in games:

        creator_id = int(g["creator_id"])

        opponent_id = (
            int(g["opponent_id"])
            if g["opponent_id"] is not None
            else None
        )

        if g["status"] == "bot_creator_turn":
            if user.id == creator_id:
                game = g
                break

        elif g["status"] == "creator_turn":
            if user.id == creator_id:
                game = g
                break

        elif g["status"] == "opponent_turn":
            if user.id == opponent_id:
                game = g
                break

    if not game:
        return

    game_id = int(game["id"])
    game_type = game["game_type"]
    rounds = int(game["rounds"])

    # --------------------------------------------------------
    # ایموجی اشتباه
    # --------------------------------------------------------

    if dice.emoji != GAME_EMOJIS[game_type]:

        await message.reply_text(
            f"❌ الان باید "
            f"{GAME_EMOJIS[game_type]} "
            f"بفرستی."
        )

        return

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

        # ضد پرتاب اضافه
        if len(creator_rolls) >= rounds:
            return

        creator_rolls.append(
            int(dice.value)
        )

        save_rolls(
            game_id,
            ",".join(map(str, creator_rolls)),
            ""
        )

        remaining = rounds - len(creator_rolls)

        # هنوز کاربر تمام نکرده
        if remaining > 0:

            await message.reply_text(
                f"👤 {name_of(user)}: {dice.value}\n"
                f"🎯 {remaining} پرتاب باقی مانده."
            )

            return

        # ----------------------------------------------------
        # کاربر تمام کرد
        # ----------------------------------------------------

        save_rolls(
            game_id,
            ",".join(map(str, creator_rolls)),
            "",
            "bot_rolling"
        )

        await message.reply_text(
            f"✅ {name_of(user)} تمام کرد.\n\n"
            f"🤖 حالا ربات خودش "
            f"{rounds} بار "
            f"{GAME_EMOJIS[game_type]} "
            f"می‌اندازد."
        )

        bot_rolls = []

        try:

            for i in range(rounds):

                # خود ربات ایموجی بازی را می‌فرستد
                sent = await context.bot.send_dice(
                    chat_id=message.chat_id,
                    emoji=GAME_EMOJIS[game_type]
                )

                # اطمینان از دریافت dice
                if not sent or not sent.dice:
                    raise RuntimeError(
                        "Telegram did not return dice result"
                    )

                bot_rolls.append(
                    int(sent.dice.value)
                )

                # برای جلوگیری از flood
                await asyncio.sleep(0.7)

            # ذخیره نتایج ربات
            save_rolls(
                game_id,
                ",".join(map(str, creator_rolls)),
                ",".join(map(str, bot_rolls)),
                "bot_finished"
            )

            # پرداخت فقط یک بار
            await finish_bot_game(
                context,
                game_id,
                creator_rolls,
                bot_rolls
            )

        except Exception:

            logger.exception(
                "BOT ROLL ERROR"
            )

            # مهم:
            # اگر ربات نتوانست بازی را کامل کند،
            # مبلغ بازی یک بار برگردانده می‌شود.
            refunded = refund_game_once(
                game_id
            )

            if refunded:

                try:
                    await message.reply_text(
                        f"🤖 ربات هنگام بازی با "
                        f"{name_of(user)} "
                        f"با خطا مواجه شد.\n\n"
                        f"💰 اعتبار شما "
                        f"{money(game['amount'])} TRX "
                        f"برگشت داده شد."
                    )
                except Exception:
                    pass

            return

        return

    # ========================================================
    # FRIEND - CREATOR
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

            save_rolls(
                game_id,
                ",".join(map(str, creator_rolls)),
                ""
            )

            await message.reply_text(
                f"👤 {name_of(user)}: {dice.value}\n"
                f"🎯 {rounds - len(creator_rolls)} "
                f"پرتاب باقی مانده."
            )

            return

        save_rolls(
            game_id,
            ",".join(map(str, creator_rolls)),
            "",
            "opponent_turn"
        )

        opponent_id = int(
            game["opponent_id"]
        )

        await message.reply_text(
            f"👤 {name_of(user)} پرتاب‌های خودش را کامل کرد.\n\n"
            f"🎯 حالا حریف با ID `{opponent_id}` "
            f"خودش {rounds} بار "
            f"{GAME_EMOJIS[game_type]} بفرستد.",
            parse_mode="Markdown"
        )

        return

    # ========================================================
    # FRIEND - OPPONENT
    # ========================================================

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

            save_rolls(
                game_id,
                ",".join(map(str, creator_rolls)),
                ",".join(map(str, opponent_rolls))
            )

            await message.reply_text(
                f"👤 {name_of(user)}: {dice.value}\n"
                f"🎯 {rounds - len(opponent_rolls)} "
                f"پرتاب باقی مانده."
            )

            return

        save_rolls(
            game_id,
            ",".join(map(str, creator_rolls)),
            ",".join(map(str, opponent_rolls)),
            "finished"
        )

        await finish_friend_game(
            context,
            game_id,
            creator_rolls,
            opponent_rolls
        )


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context):
    message = update.message
    user = update.effective_user

    if not message.reply_to_message:
        await message.reply_text(
            "💸 روی پیام کاربر Reply کن.\n\n"
            "مثال:\n"
            "انتقال 0.5"
        )
        return

    target = message.reply_to_message.from_user

    if not target or target.is_bot:
        await message.reply_text(
            "❌ مقصد نامعتبر است."
        )
        return

    if target.id == user.id:
        await message.reply_text(
            "❌ نمی‌توانی به خودت انتقال بدهی."
        )
        return

    amount = parse_amount_from_text(
        message.text
    )

    if amount is None:
        await message.reply_text(
            "❌ مقدار نامعتبر."
        )
        return

    ensure_user(target)

    async with GAME_LOCK:

        with closing(db()) as con:
            try:
                con.execute("BEGIN IMMEDIATE")

                sender = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (user.id,)).fetchone()

                receiver = con.execute("""
                SELECT balance
                FROM users
                WHERE user_id=?
                """, (target.id,)).fetchone()

                if not sender or not receiver:
                    con.execute("ROLLBACK")
                    return

                sender_balance = D(
                    sender["balance"]
                )

                if sender_balance < amount:
                    con.execute("ROLLBACK")

                    await message.reply_text(
                        "❌ اعتبار کافی نیست."
                    )
                    return

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
                SET balance=CAST(balance AS REAL) + ?
                WHERE user_id=?
                """, (
                    float(amount),
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

                con.commit()

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
        f"💰 مقدار: {money(amount)} TRX"
    )


def parse_amount_from_text(text):
    text = normalize_digits(text or "")

    m = re.search(
        r"(\d+(?:[.,]\d+)?)",
        text
    )

    if not m:
        return None

    return parse_decimal_amount(
        m.group(1).replace(",", ".")
    )


# ============================================================
# REFERRAL MENU
# ============================================================

async def referral_menu(update, context):
    user = update.effective_user

    bot = await context.bot.get_me()

    link = (
        f"https://t.me/{bot.username}"
        f"?start={user.id}"
    )

    with closing(db()) as con:
        count = con.execute("""
        SELECT COUNT(*)
        FROM referrals
        WHERE referrer_id=?
        """, (user.id,)).fetchone()[0]

    await update.effective_message.reply_text(
        f"🔗 زیرمجموعه\n\n"
        f"تعداد زیرمجموعه‌ها: {count}\n"
        f"🎁 پاداش هر زیرمجموعه: "
        f"{money(REFERRAL_REWARD)} TRX\n\n"
        f"لینک دعوت:\n"
        f"{link}"
    )


# ============================================================
# REQUEST
# ============================================================

async def request_menu(update, context):
    await update.effective_message.reply_text(
        "📤 درخواست\n\n"
        "مثال:\n"
        "درخواست 5"
    )


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
        "👥 بازی دوستان:\n"
        "اول سازنده خودش ایموجی را می‌فرستد، "
        "بعد حریف خودش می‌فرستد.\n\n"
        "🤖 بازی با ربات:\n"
        "اول کاربر خودش ایموجی را می‌فرستد، "
        "بعد ربات خودش ایموجی را می‌اندازد.\n\n"
        "💰 موجودی\n"
        "💸 انتقال با Reply\n"
        "🔗 زیرمجموعه\n\n"
        "موجودی این سیستم اعتبار داخلی ربات است."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user or not message.text:
        return

    ensure_user(user)

    if is_blocked(user.id):
        return

    text = message.text.strip()
    normalized = normalize_digits(text)

    # --------------------------------------------------------
    # GAME CREATION
    # --------------------------------------------------------

    parsed = parse_game(normalized)

    if parsed:

        game, rounds, amount = parsed

        await create_game(
            update,
            context,
            game,
            rounds,
            amount
        )

        return

    # --------------------------------------------------------
    # BALANCE
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
    # GAME
    # --------------------------------------------------------

    if text == "🎮 بازی":
        await game_menu(
            update,
            context
        )
        return

    # --------------------------------------------------------
    # FRIENDS
    # --------------------------------------------------------

    if text == "👥 بازی با دوستان":
        await friends_menu(
            update,
            context
        )
        return

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    if text == "🤖 بازی با ربات":
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

        await transfer(
            update,
            context
        )
        return

    # --------------------------------------------------------
    # REFERRAL
    # --------------------------------------------------------

    if text in (
        "🔗 زیرمجموعه",
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
    # REQUEST
    # --------------------------------------------------------

    if re.match(
        r"^(درخواست|request)\s+\d+(?:[.,]\d+)?$",
        normalized,
        re.IGNORECASE
    ):

        amount = parse_amount_from_text(
            normalized
        )

        if amount:

            await message.reply_text(
                f"📤 درخواست {money(amount)} TRX ثبت شد.\n"
                f"برای تکمیل درخواست اطلاعات لازم را ارسال کن."
            )

        return

    # --------------------------------------------------------
    # HELP
    # --------------------------------------------------------

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

    if not user or not is_admin(user.id):
        await update.effective_message.reply_text(
            "⛔ دسترسی ندارید."
        )
        return

    await update.effective_message.reply_text(
        "👑 پنل مدیریت\n\n"
        "/addbalance USER_ID AMOUNT\n"
        "/removebalance USER_ID AMOUNT\n"
        "/block USER_ID\n"
        "/unblock USER_ID\n"
        "/stats"
    )


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

    target = get_user(target_id)

    if not target:
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
        f"✅ انجام شد.\n\n"
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

    if not debit_balance(
        target_id,
        amount
    ):
        await update.message.reply_text(
            "❌ اعتبار کافی نیست."
        )
        return

    await update.message.reply_text(
        f"✅ کم شد.\n\n"
        f"👤 {target_id}\n"
        f"➖ {money(amount)} TRX\n"
        f"💰 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
    )


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
        """, (target_id,))
        con.commit()

    await update.message.reply_text(
        f"🚫 {target_id} مسدود شد."
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
        """, (target_id,))
        con.commit()

    await update.message.reply_text(
        f"✅ {target_id} رفع مسدودی شد."
    )


async def stats(update, context):
    user = update.effective_user

    if not user or not is_admin(user.id):
        return

    with closing(db()) as con:

        users = con.execute(
            "SELECT COUNT(*) FROM users"
        ).fetchone()[0]

        games = con.execute(
            "SELECT COUNT(*) FROM games"
        ).fetchone()[0]

        active = con.execute("""
        SELECT COUNT(*)
        FROM games
        WHERE status NOT IN (
            'finished',
            'cancelled',
            'refunded'
        )
        """).fetchone()[0]

        total = con.execute("""
        SELECT SUM(CAST(balance AS REAL))
        FROM users
        """).fetchone()[0] or 0

    await update.message.reply_text(
        f"📊 آمار\n\n"
        f"👥 کاربران: {users:,}\n"
        f"🎮 کل بازی‌ها: {games:,}\n"
        f"🔄 بازی‌های فعال: {active:,}\n"
        f"💰 مجموع اعتبار کاربران: {money(total)} TRX"
    )


# ============================================================
# ERROR HANDLER
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
        .concurrent_updates(True)
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

    application.add_handler(
        CommandHandler(
            "stats",
            stats
        )
    )

    # --------------------------------------------------------
    # GAME CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            game_button,
            pattern=r"^game_"
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
