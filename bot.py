import os
import re
import sqlite3
import uuid
import time
import asyncio
import logging
from decimal import Decimal, InvalidOperation

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "8552447077"))
FORCE_CHANNEL = "@zobxt"

DB_FILE = "bet.db"

MIN_BET = Decimal("0.1")
REF_REWARD = Decimal("0.05")
WIN_PRIZE = Decimal("0.19")

GAME_TIMEOUT = 600

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

log = logging.getLogger("BET_BTBOT")


# =========================================================
# DATABASE
# =========================================================

def get_db():
    con = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False
    )
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    return con


def init_db():
    con = get_db()

    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        balance TEXT DEFAULT '0',
        referrer INTEGER,
        referral_paid INTEGER DEFAULT 0,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS games (
        id TEXT PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        message_id INTEGER,
        creator_id INTEGER NOT NULL,
        opponent_id INTEGER,
        game_type TEXT NOT NULL,
        emoji TEXT NOT NULL,
        amount TEXT NOT NULL,
        mode TEXT DEFAULT '',
        creator_roll INTEGER,
        opponent_roll INTEGER,
        status TEXT NOT NULL,
        created_at INTEGER,
        updated_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS holds (
        id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        amount TEXT NOT NULL,
        active INTEGER DEFAULT 1,
        created_at INTEGER,
        UNIQUE(game_id,user_id)
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY,
        game_id TEXT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        amount TEXT NOT NULL,
        created_at INTEGER
    );

    CREATE TABLE IF NOT EXISTS referrals (
        referred_id INTEGER PRIMARY KEY,
        referrer_id INTEGER NOT NULL,
        reward TEXT NOT NULL,
        created_at INTEGER
    );

    CREATE INDEX IF NOT EXISTS games_status
    ON games(status);

    CREATE INDEX IF NOT EXISTS games_updated
    ON games(updated_at);
    """)

    con.commit()
    con.close()


# =========================================================
# UTILS
# =========================================================

def normalize_digits(text):
    if not text:
        return ""

    return text.translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


def dec(value):
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def fmt(value):
    value = dec(value)
    s = format(value, "f")
    s = s.rstrip("0").rstrip(".")
    return s or "0"


def display_user(user):
    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


def db_user_name(user_id):
    con = get_db()

    row = con.execute(
        """
        SELECT first_name, username
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    con.close()

    if not row:
        return str(user_id)

    if row["first_name"]:
        return row["first_name"]

    if row["username"]:
        return "@" + row["username"]

    return str(user_id)


# =========================================================
# USERS
# =========================================================

def ensure_user(user, referrer=None):
    con = get_db()

    row = con.execute(
        "SELECT user_id FROM users WHERE user_id=?",
        (user.id,)
    ).fetchone()

    if row:

        con.execute(
            """
            UPDATE users
            SET username=?, first_name=?
            WHERE user_id=?
            """,
            (
                user.username or "",
                user.first_name or "",
                user.id
            )
        )

    else:

        valid_ref = None

        if referrer:

            try:
                ref_id = int(referrer)

                if ref_id != user.id:

                    exists = con.execute(
                        "SELECT user_id FROM users WHERE user_id=?",
                        (ref_id,)
                    ).fetchone()

                    if exists:
                        valid_ref = ref_id

            except Exception:
                pass

        con.execute(
            """
            INSERT INTO users
            (
                user_id,
                username,
                first_name,
                balance,
                referrer,
                referral_paid,
                created_at
            )
            VALUES (?, ?, ?, '0', ?, 0, ?)
            """,
            (
                user.id,
                user.username or "",
                user.first_name or "",
                valid_ref,
                int(time.time())
            )
        )

    con.commit()
    con.close()


def get_balance(user_id):
    con = get_db()

    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    con.close()

    if not row:
        return Decimal("0")

    return dec(row["balance"])


def change_balance(con, user_id, amount):

    row = con.execute(
        """
        SELECT balance
        FROM users
        WHERE user_id=?
        """,
        (user_id,)
    ).fetchone()

    if not row:
        raise ValueError("USER_NOT_FOUND")

    current = dec(row["balance"])
    new = current + dec(amount)

    if new < 0:
        raise ValueError("INSUFFICIENT_BALANCE")

    con.execute(
        """
        UPDATE users
        SET balance=?
        WHERE user_id=?
        """,
        (
            fmt(new),
            user_id
        )
    )


# =========================================================
# REFERRAL
# =========================================================

def pay_referral(user_id):

    con = get_db()

    try:

        con.execute("BEGIN IMMEDIATE")

        row = con.execute(
            """
            SELECT referrer, referral_paid
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if not row:
            con.rollback()
            return False

        if not row["referrer"]:
            con.rollback()
            return False

        if row["referral_paid"]:
            con.rollback()
            return False

        referrer = int(row["referrer"])

        exists = con.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (referrer,)
        ).fetchone()

        if not exists:
            con.rollback()
            return False

        change_balance(
            con,
            referrer,
            REF_REWARD
        )

        con.execute(
            """
            UPDATE users
            SET referral_paid=1
            WHERE user_id=?
            """,
            (user_id,)
        )

        con.execute(
            """
            INSERT OR IGNORE INTO referrals
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                referrer,
                fmt(REF_REWARD),
                int(time.time())
            )
        )

        con.commit()
        return True

    except Exception:

        con.rollback()
        log.exception("referral error")
        return False

    finally:
        con.close()


# =========================================================
# FORCE JOIN
# =========================================================

async def is_joined(bot, user_id):

    if user_id == OWNER_ID:
        return True

    try:

        member = await bot.get_chat_member(
            FORCE_CHANNEL,
            user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception as e:

        log.warning(
            "join check error: %s",
            e
        )

        return False


def join_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url="https://t.me/zobxt"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_join"
            )
        ]
    ])


async def require_join(update, context):

    user = update.effective_user

    if not user:
        return False

    if await is_joined(
        context.bot,
        user.id
    ):
        return True

    message = update.effective_message

    if message:

        await message.reply_text(
            "🔒 برای استفاده ابتدا عضو @zobxt شوید.",
            reply_markup=join_keyboard()
        )

    return False


# =========================================================
# GAME TYPES
# =========================================================

GAME_TYPES = {

    "تاس": ("dice", "🎲"),
    "بولینگ": ("bowling", "🎳"),
    "دارت": ("darts", "🎯"),
    "بسکتبال": ("basketball", "🏀"),

    "dice": ("dice", "🎲"),
    "bowling": ("bowling", "🎳"),
    "darts": ("darts", "🎯"),
    "basketball": ("basketball", "🏀"),
}


def parse_game(text):

    text = normalize_digits(text).strip()

    parts = text.split()

    if len(parts) != 3:
        return None

    if parts[0] != "1":
        return None

    game_name = parts[1].lower()

    if game_name not in GAME_TYPES:
        return None

    try:
        amount = Decimal(
            parts[2].replace(",", ".")
        )
    except InvalidOperation:
        return None

    if amount < MIN_BET:
        return None

    game_type, emoji = GAME_TYPES[game_name]

    return (
        game_type,
        emoji,
        amount
    )


# =========================================================
# HOLD
# =========================================================

def hold_amount(game_id, user_id, amount):

    con = get_db()

    try:

        con.execute("BEGIN IMMEDIATE")

        exists = con.execute(
            """
            SELECT id
            FROM holds
            WHERE game_id=?
            AND user_id=?
            AND active=1
            """,
            (
                game_id,
                user_id
            )
        ).fetchone()

        if exists:
            con.rollback()
            return False

        user = con.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if not user:
            con.rollback()
            return False

        if dec(user["balance"]) < amount:
            con.rollback()
            return False

        change_balance(
            con,
            user_id,
            -amount
        )

        con.execute(
            """
            INSERT INTO holds
            (
                id,
                game_id,
                user_id,
                amount,
                active,
                created_at
            )
            VALUES (?, ?, ?, ?, 1, ?)
            """,
            (
                uuid.uuid4().hex,
                game_id,
                user_id,
                fmt(amount),
                int(time.time())
            )
        )

        con.execute(
            """
            INSERT INTO transactions
            VALUES (?, ?, ?, 'HOLD', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                game_id,
                user_id,
                fmt(amount),
                int(time.time())
            )
        )

        con.commit()

        return True

    except Exception:

        con.rollback()
        log.exception("hold error")
        return False

    finally:
        con.close()


# =========================================================
# REFUND
# =========================================================

def refund_game(game_id):

    con = get_db()

    try:

        con.execute("BEGIN IMMEDIATE")

        game = con.execute(
            """
            SELECT status
            FROM games
            WHERE id=?
            """,
            (game_id,)
        ).fetchone()

        if not game:
            con.rollback()
            return False

        if game["status"] in (
            "FINISHED",
            "REFUNDED",
            "CANCELLED"
        ):
            con.rollback()
            return False

        holds = con.execute(
            """
            SELECT user_id, amount
            FROM holds
            WHERE game_id=?
            AND active=1
            """,
            (game_id,)
        ).fetchall()

        for h in holds:

            change_balance(
                con,
                h["user_id"],
                dec(h["amount"])
            )

            con.execute(
                """
                INSERT INTO transactions
                VALUES (?, ?, ?, 'REFUND', ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    game_id,
                    h["user_id"],
                    h["amount"],
                    int(time.time())
                )
            )

            con.execute(
                """
                UPDATE holds
                SET active=0
                WHERE game_id=?
                AND user_id=?
                """,
                (
                    game_id,
                    h["user_id"]
                )
            )

        con.execute(
            """
            UPDATE games
            SET status='REFUNDED',
                updated_at=?
            WHERE id=?
            """,
            (
                int(time.time()),
                game_id
            )
        )

        con.commit()
        return True

    except Exception:

        con.rollback()
        log.exception("refund error")
        return False

    finally:
        con.close()


# =========================================================
# PAY WINNER
# =========================================================

def pay_winner(game_id, winner_id):

    con = get_db()

    try:

        con.execute("BEGIN IMMEDIATE")

        game = con.execute(
            """
            SELECT status
            FROM games
            WHERE id=?
            """,
            (game_id,)
        ).fetchone()

        if not game:
            con.rollback()
            return False

        if game["status"] == "FINISHED":
            con.rollback()
            return False

        if game["status"] in (
            "REFUNDED",
            "CANCELLED"
        ):
            con.rollback()
            return False

        paid = con.execute(
            """
            SELECT id
            FROM transactions
            WHERE game_id=?
            AND kind='PRIZE'
            """,
            (game_id,)
        ).fetchone()

        if paid:
            con.rollback()
            return False

        change_balance(
            con,
            winner_id,
            WIN_PRIZE
        )

        con.execute(
            """
            INSERT INTO transactions
            VALUES (?, ?, ?, 'PRIZE', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                game_id,
                winner_id,
                fmt(WIN_PRIZE),
                int(time.time())
            )
        )

        con.execute(
            """
            UPDATE holds
            SET active=0
            WHERE game_id=?
            """,
            (game_id,)
        )

        con.execute(
            """
            UPDATE games
            SET status='FINISHED',
                updated_at=?
            WHERE id=?
            """,
            (
                int(time.time()),
                game_id
            )
        )

        con.commit()

        return True

    except Exception:

        con.rollback()
        log.exception("winner payment error")
        return False

    finally:
        con.close()


# =========================================================
# TELEGRAM ROLL
# =========================================================

async def roll_game(bot, chat_id, game_type):

    emojis = {
        "dice": "🎲",
        "bowling": "🎳",
        "darts": "🎯",
        "basketball": "🏀"
    }

    msg = await bot.send_dice(
        chat_id=chat_id,
        emoji=emojis[game_type]
    )

    await asyncio.sleep(3)

    return msg.dice.value


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(update, context, parsed):

    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    game_type, emoji, amount = parsed

    ensure_user(user)

    if get_balance(user.id) < amount:

        await message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {fmt(get_balance(user.id))} TRX"
        )

        return

    game_id = uuid.uuid4().hex

    if not hold_amount(
        game_id,
        user.id,
        amount
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست یا تراکنش در حال انجام است."
        )

        return

    now = int(time.time())

    con = get_db()

    try:

        con.execute(
            """
            INSERT INTO games
            (
                id,
                chat_id,
                message_id,
                creator_id,
                opponent_id,
                game_type,
                emoji,
                amount,
                mode,
                creator_roll,
                opponent_roll,
                status,
                created_at,
                updated_at
            )
            VALUES (?, ?, NULL, ?, NULL, ?, ?, ?, '',
                    NULL, NULL, 'WAITING', ?, ?)
            """,
            (
                game_id,
                chat.id,
                user.id,
                game_type,
                emoji,
                fmt(amount),
                now,
                now
            )
        )

        con.commit()

    except Exception:

        con.rollback()
        con.close()

        refund_game(game_id)

        await message.reply_text(
            "❌ خطا در ساخت بازی؛ مبلغ برگشت داده شد."
        )

        return

    con.close()

    keyboard = InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot:{game_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friend:{game_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel:{game_id}"
            )
        ]

    ])

    text = (
        f"{emoji} **بازی جدید**\n\n"
        f"👤 سازنده: {display_user(user)}\n"
        f"🎮 بازی: {game_type}\n"
        f"💰 شرط: {fmt(amount)} TRX\n\n"
        "🤖 بازی با ربات:\n"
        "اول سازنده رول می‌کند، بعد ربات.\n\n"
        "👥 بازی با دوستان:\n"
        "اول سازنده رول می‌کند، بعد حریف.\n\n"
        "👇 انتخاب کنید:"
    )

    try:

        sent = await message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )

        con = get_db()

        con.execute(
            """
            UPDATE games
            SET message_id=?
            WHERE id=?
            """,
            (
                sent.message_id,
                game_id
            )
        )

        con.commit()
        con.close()

    except Exception:

        refund_game(game_id)

        try:
            await message.reply_text(
                "🛡️ خطا در ارسال بازی؛ مبلغ برگشت داده شد."
            )
        except Exception:
            pass


# =========================================================
# START
# =========================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    referrer = None

    if context.args:

        arg = context.args[0]

        if arg.startswith("ref_"):
            arg = arg[4:]

        try:
            referrer = int(arg)
        except Exception:
            referrer = None

    ensure_user(
        user,
        referrer
    )

    await asyncio.to_thread(
        pay_referral,
        user.id
    )

    if not await require_join(
        update,
        context
    ):
        return

    keyboard = InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="my_balance"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 زیرمجموعه",
                callback_data="my_ref"
            )
        ],

        [
            InlineKeyboardButton(
                "📖 راهنما",
                callback_data="help"
            )
        ]

    ])

    await update.effective_message.reply_text(
        "🎮 **BET_BTBOT**\n\n"
        "سلام 👋\n"
        "برای ساخت بازی، داخل گپ یکی از این‌ها را بفرست:\n\n"
        "🎲 `1 تاس 0.1`\n"
        "🎳 `1 بولینگ 0.1`\n"
        "🎯 `1 دارت 0.1`\n"
        "🏀 `1 بسکتبال 0.1`\n\n"
        "💰 موجودی و انتقال داخل گپ قابل استفاده است.",
        parse_mode="Markdown",
        reply_markup=keyboard
    )


# =========================================================
# BALANCE
# =========================================================

async def show_balance(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    user = update.effective_user

    ensure_user(user)

    await update.effective_message.reply_text(
        "💰 موجودی\n\n"
        f"{fmt(get_balance(user.id))} TRX"
    )


# =========================================================
# REFERRAL
# =========================================================

async def referral(update, context):

    if not await require_join(
        update,
        context
    ):
        return

    user = update.effective_user

    ensure_user(user)

    bot = await context.bot.get_me()

    link = (
        f"https://t.me/{bot.username}"
        f"?start=ref_{user.id}"
    )

    await update.effective_message.reply_text(
        "👥 زیرمجموعه\n\n"
        "لینک دعوت شما:\n"
        f"{link}\n\n"
        "🎁 پاداش هر زیرمجموعه: 0.05 TRX"
    )


# =========================================================
# TRANSFER
# =========================================================

TRANSFER_RE = re.compile(
    r"^\s*انتقال\s+([0-9]+(?:[.,][0-9]+)?)\s*$"
)


async def transfer(update, context):

    message = update.effective_message
    user = update.effective_user

    if not await require_join(
        update,
        context
    ):
        return

    if not message.reply_to_message:

        await message.reply_text(
            "❌ برای انتقال باید روی پیام شخص Reply کنی.\n\n"
            "مثال:\n"
            "انتقال 0.1"
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
            "❌ انتقال به ربات امکان‌پذیر نیست."
        )

        return

    match = TRANSFER_RE.match(
        normalize_digits(
            message.text or ""
        )
    )

    if not match:
        return

    try:

        amount = Decimal(
            match.group(1).replace(",", ".")
        )

    except Exception:

        await message.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    if amount <= 0:
        await message.reply_text(
            "❌ مبلغ نامعتبر است."
        )
        return

    ensure_user(user)
    ensure_user(target)

    con = get_db()

    try:

        con.execute("BEGIN IMMEDIATE")

        sender = con.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id=?
            """,
            (user.id,)
        ).fetchone()

        if not sender or dec(sender["balance"]) < amount:

            con.rollback()

            await message.reply_text(
                "❌ موجودی کافی نیست."
            )

            return

        change_balance(
            con,
            user.id,
            -amount
        )

        change_balance(
            con,
            target.id,
            amount
        )

        con.execute(
            """
            INSERT INTO transactions
            VALUES (?, NULL, ?, 'TRANSFER_OUT', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                user.id,
                fmt(amount),
                int(time.time())
            )
        )

        con.execute(
            """
            INSERT INTO transactions
            VALUES (?, NULL, ?, 'TRANSFER_IN', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                target.id,
                fmt(amount),
                int(time.time())
            )
        )

        con.commit()

    except Exception:

        con.rollback()

        log.exception("transfer error")

        await message.reply_text(
            "❌ انتقال انجام نشد."
        )

        return

    finally:
        con.close()

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 گیرنده: {display_user(target)}\n"
        f"💰 مبلغ: {fmt(amount)} TRX"
    )


# =========================================================
# CALLBACK
# =========================================================

async def callbacks(update, context):

    query = update.callback_query
    user = query.from_user

    data = query.data or ""

    ensure_user(user)

    if data == "check_join":

        if await is_joined(
            context.bot,
            user.id
        ):

            await query.answer(
                "✅ عضویت تأیید شد.",
                show_alert=True
            )

            await query.message.reply_text(
                "✅ حالا می‌توانی از ربات استفاده کنی."
            )

        else:

            await query.answer(
                "❌ هنوز عضو @zobxt نیستی.",
                show_alert=True
            )

        return

    if data == "my_balance":

        await query.answer()

        await query.message.reply_text(
            "💰 موجودی\n\n"
            f"{fmt(get_balance(user.id))} TRX"
        )

        return

    if data == "my_ref":

        await query.answer()

        bot = await context.bot.get_me()

        link = (
            f"https://t.me/{bot.username}"
            f"?start=ref_{user.id}"
        )

        await query.message.reply_text(
            "👥 زیرمجموعه\n\n"
            f"{link}\n\n"
            "🎁 پاداش هر زیرمجموعه: 0.05 TRX"
        )

        return

    if data == "help":

        await query.answer()

        await query.message.reply_text(
            "📖 راهنما\n\n"
            "داخل گپ:\n\n"
            "🎲 1 تاس 0.1\n"
            "🎳 1 بولینگ 0.1\n"
            "🎯 1 دارت 0.1\n"
            "🏀 1 بسکتبال 0.1\n\n"
            "💰 موجودی\n"
            "💸 انتقال 0.1 با Reply"
        )

        return

    if not await is_joined(
        context.bot,
        user.id
    ):

        await query.answer(
            "ابتدا عضو @zobxt شوید.",
            show_alert=True
        )

        return

    parts = data.split(":", 1)

    if len(parts) != 2:
        return

    action, game_id = parts

    con = get_db()

    game = con.execute(
        "SELECT * FROM games WHERE id=?",
        (game_id,)
    ).fetchone()

    con.close()

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    # =====================================================
    # CANCEL
    # =====================================================

    if action == "cancel":

        if user.id != game["creator_id"]:

            await query.answer(
                "❌ فقط سازنده می‌تواند لغو کند.",
                show_alert=True
            )

            return

        if game["status"] != "WAITING":

            await query.answer(
                "❌ بازی قبلاً شروع شده.",
                show_alert=True
            )

            return

        if refund_game(game_id):

            await query.answer(
                "✅ بازی لغو شد.",
                show_alert=True
            )

            try:

                await query.edit_message_text(
                    "❌ بازی لغو شد.\n\n"
                    "💰 مبلغ برگشت داده شد."
                )

            except Exception:
                pass

        return

    # =====================================================
    # BOT GAME
    # =====================================================

    if action == "bot":

        if user.id != game["creator_id"]:

            await query.answer(
                "❌ فقط سازنده می‌تواند بازی با ربات را انتخاب کند.",
                show_alert=True
            )

            return

        con = get_db()

        try:

            con.execute("BEGIN IMMEDIATE")

            row = con.execute(
                """
                SELECT status
                FROM games
                WHERE id=?
                """,
                (game_id,)
            ).fetchone()

            if not row or row["status"] != "WAITING":

                con.rollback()

                await query.answer(
                    "❌ بازی قبلاً شروع شده.",
                    show_alert=True
                )

                return

            con.execute(
                """
                UPDATE games
                SET mode='BOT',
                    status='PLAYING',
                    updated_at=?
                WHERE id=?
                """,
                (
                    int(time.time()),
                    game_id
                )
            )

            con.commit()

        except Exception:

            con.rollback()

            await query.answer(
                "❌ خطا.",
                show_alert=True
            )

            return

        finally:
            con.close()

        await query.answer()

        try:

            await query.edit_message_text(
                f"{game['emoji']} بازی با ربات شروع شد.\n\n"
                f"👤 {db_user_name(game['creator_id'])} اول رول می‌کند..."
            )

            # کاربر
            creator_roll = await roll_game(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = get_db()

            con.execute(
                """
                UPDATE games
                SET creator_roll=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    creator_roll,
                    int(time.time()),
                    game_id
                )
            )

            con.commit()
            con.close()

            await context.bot.send_message(
                game["chat_id"],
                "🤖 حالا ربات رول می‌کند..."
            )

            # ربات
            bot_roll = await roll_game(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = get_db()

            con.execute(
                """
                UPDATE games
                SET opponent_roll=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    bot_roll,
                    int(time.time()),
                    game_id
                )
            )

            con.commit()
            con.close()

            creator_name = db_user_name(
                game["creator_id"]
            )

            if creator_roll > bot_roll:

                ok = pay_winner(
                    game_id,
                    game["creator_id"]
                )

                if not ok:

                    refund_game(game_id)

                    result = (
                        "🛡️ خطا در پرداخت.\n"
                        "💰 مبلغ برگشت داده شد."
                    )

                else:

                    result = (
                        "🏆 نتیجه بازی\n\n"
                        f"👤 {creator_name}: {creator_roll}\n"
                        f"🤖 ربات: {bot_roll}\n\n"
                        f"🥇 برنده: {creator_name}\n"
                        f"💰 جایزه: {fmt(WIN_PRIZE)} TRX"
                    )

            elif bot_roll > creator_roll:

                con = get_db()

                try:

                    con.execute("BEGIN IMMEDIATE")

                    con.execute(
                        """
                        UPDATE holds
                        SET active=0
                        WHERE game_id=?
                        """,
                        (game_id,)
                    )

                    con.execute(
                        """
                        UPDATE games
                        SET status='FINISHED',
                            updated_at=?
                        WHERE id=?
                        """,
                        (
                            int(time.time()),
                            game_id
                        )
                    )

                    con.commit()

                except Exception:

                    con.rollback()

                    refund_game(game_id)

                finally:
                    con.close()

                result = (
                    "🏆 نتیجه بازی\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"🤖 ربات: {bot_roll}\n\n"
                    "🥇 برنده: ربات\n"
                    "❌ باختید."
                )

            else:

                refund_game(game_id)

                result = (
                    "🤝 مساوی شد\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"🤖 ربات: {bot_roll}\n\n"
                    "💰 مبلغ برگشت داده شد."
                )

            await context.bot.send_message(
                game["chat_id"],
                result
            )

        except Exception:

            log.exception(
                "bot game failed"
            )

            refund_game(game_id)

            try:

                await context.bot.send_message(
                    game["chat_id"],
                    "🛡️ خطا در بازی.\n"
                    "💰 مبلغ برگشت داده شد."
                )

            except Exception:
                pass

        return

    # =====================================================
    # FRIEND GAME
    # =====================================================

    if action == "friend":

        if user.id == game["creator_id"]:

            await query.answer(
                "❌ سازنده نمی‌تواند حریف خودش باشد.",
                show_alert=True
            )

            return

        amount = dec(game["amount"])

        con = get_db()

        try:

            con.execute("BEGIN IMMEDIATE")

            current = con.execute(
                """
                SELECT *
                FROM games
                WHERE id=?
                """,
                (game_id,)
            ).fetchone()

            if not current or current["status"] != "WAITING":

                con.rollback()

                await query.answer(
                    "❌ این بازی قبلاً گرفته شده.",
                    show_alert=True
                )

                return

            opponent = con.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id=?
                """,
                (user.id,)
            ).fetchone()

            if not opponent or dec(opponent["balance"]) < amount:

                con.rollback()

                await query.answer(
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

                return

            change_balance(
                con,
                user.id,
                -amount
            )

            con.execute(
                """
                INSERT INTO holds
                (
                    id,
                    game_id,
                    user_id,
                    amount,
                    active,
                    created_at
                )
                VALUES (?, ?, ?, ?, 1, ?)
                """,
                (
                    uuid.uuid4().hex,
                    game_id,
                    user.id,
                    fmt(amount),
                    int(time.time())
                )
            )

            con.execute(
                """
                INSERT INTO transactions
                VALUES (?, ?, ?, 'HOLD', ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    game_id,
                    user.id,
                    fmt(amount),
                    int(time.time())
                )
            )

            con.execute(
                """
                UPDATE games
                SET opponent_id=?,
                    mode='FRIEND',
                    status='PLAYING',
                    updated_at=?
                WHERE id=?
                AND status='WAITING'
                """,
                (
                    user.id,
                    int(time.time()),
                    game_id
                )
            )

            con.commit()

        except Exception:

            con.rollback()

            await query.answer(
                "❌ خطا در ورود به بازی.",
                show_alert=True
            )

            return

        finally:
            con.close()

        await query.answer()

        try:

            await query.edit_message_text(
                "👥 بازی با دوستان شروع شد.\n\n"
                f"👤 سازنده: {db_user_name(game['creator_id'])}\n"
                f"👤 حریف: {display_user(user)}\n\n"
                "🎮 ابتدا سازنده رول می‌کند."
            )

            # سازنده
            creator_roll = await roll_game(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = get_db()

            con.execute(
                """
                UPDATE games
                SET creator_roll=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    creator_roll,
                    int(time.time()),
                    game_id
                )
            )

            con.commit()
            con.close()

            await context.bot.send_message(
                game["chat_id"],
                f"👤 {db_user_name(game['creator_id'])} "
                f"رول کرد: {creator_roll}\n\n"
                f"⏳ حالا {display_user(user)} رول می‌کند..."
            )

            # حریف
            opponent_roll = await roll_game(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = get_db()

            con.execute(
                """
                UPDATE games
                SET opponent_roll=?,
                    updated_at=?
                WHERE id=?
                """,
                (
                    opponent_roll,
                    int(time.time()),
                    game_id
                )
            )

            con.commit()
            con.close()

            creator_name = db_user_name(
                game["creator_id"]
            )

            opponent_name = display_user(user)

            if creator_roll > opponent_roll:

                winner = game["creator_id"]

            elif opponent_roll > creator_roll:

                winner = user.id

            else:

                winner = None

            if winner:

                if pay_winner(
                    game_id,
                    winner
                ):

                    winner_name = (
                        creator_name
                        if winner == game["creator_id"]
                        else opponent_name
                    )

                    result = (
                        "🏆 نتیجه بازی\n\n"
                        f"👤 {creator_name}: {creator_roll}\n"
                        f"👤 {opponent_name}: {opponent_roll}\n\n"
                        f"🥇 برنده: {winner_name}\n"
                        f"💰 جایزه: {fmt(WIN_PRIZE)} TRX"
                    )

                else:

                    refund_game(game_id)

                    result = (
                        "🛡️ خطا در پرداخت.\n"
                        "💰 مبلغ‌ها برگشت داده شدند."
                    )

            else:

                refund_game(game_id)

                result = (
                    "🤝 مساوی شد\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"👤 {opponent_name}: {opponent_roll}\n\n"
                    "💰 مبلغ‌ها برگشت داده شدند."
                )

            await context.bot.send_message(
                game["chat_id"],
                result
            )

        except Exception:

            log.exception(
                "friend game failed"
            )

            refund_game(game_id)

            try:

                await context.bot.send_message(
                    game["chat_id"],
                    "🛡️ خطا در بازی.\n"
                    "💰 مبلغ‌ها برگشت داده شدند."
                )

            except Exception:
                pass

        return


# =========================================================
# ADMIN
# =========================================================

def admin_keyboard():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin:stats"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 کاربران",
                callback_data="admin:users"
            )
        ],

        [
            InlineKeyboardButton(
                "🎮 بازی‌های فعال",
                callback_data="admin:games"
            )
        ],

        [
            InlineKeyboardButton(
                "🧹 بازیابی بازی‌های گیرکرده",
                callback_data="admin:recover"
            )
        ]

    ])


async def admin(update, context):

    if update.effective_user.id != OWNER_ID:

        await update.effective_message.reply_text(
            "⛔ دسترسی ندارید."
        )

        return

    await update.effective_message.reply_text(
        "👑 پنل مدیریت\n\n"
        "مدیریت آمار، کاربران و بازی‌ها:",
        reply_markup=admin_keyboard()
    )


async def admin_callback(update, context):

    query = update.callback_query

    if query.from_user.id != OWNER_ID:

        await query.answer(
            "⛔ دسترسی ندارید.",
            show_alert=True
        )

        return

    await query.answer()

    action = query.data

    if action == "admin:stats":

        con = get_db()

        users = con.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        games = con.execute(
            "SELECT COUNT(*) c FROM games"
        ).fetchone()["c"]

        active = con.execute(
            """
            SELECT COUNT(*) c
            FROM games
            WHERE status IN ('WAITING','PLAYING')
            """
        ).fetchone()["c"]

        refs = con.execute(
            "SELECT COUNT(*) c FROM referrals"
        ).fetchone()["c"]

        con.close()

        await query.message.reply_text(
            "📊 آمار\n\n"
            f"👥 کاربران: {users}\n"
            f"🎮 کل بازی‌ها: {games}\n"
            f"🟢 بازی فعال: {active}\n"
            f"👥 زیرمجموعه: {refs}"
        )

        return

    if action == "admin:users":

        con = get_db()

        count = con.execute(
            "SELECT COUNT(*) c FROM users"
        ).fetchone()["c"]

        con.close()

        await query.message.reply_text(
            f"👥 کاربران: {count}"
        )

        return

    if action == "admin:games":

        con = get_db()

        rows = con.execute(
            """
            SELECT game_type,
                   creator_id,
                   opponent_id,
                   amount,
                   status
            FROM games
            WHERE status IN ('WAITING','PLAYING')
            ORDER BY created_at DESC
            LIMIT 20
            """
        ).fetchall()

        con.close()

        if not rows:

            await query.message.reply_text(
                "🟢 بازی فعالی وجود ندارد."
            )

            return

        text = "🎮 بازی‌های فعال\n\n"

        for row in rows:

            text += (
                f"🎮 {row['game_type']}\n"
                f"👤 {row['creator_id']}\n"
                f"👥 {row['opponent_id'] or '-'}\n"
                f"💰 {row['amount']} TRX\n"
                f"📌 {row['status']}\n\n"
            )

        await query.message.reply_text(text)

        return

    if action == "admin:recover":

        count = await recover_stuck()

        await query.message.reply_text(
            f"🧹 {count} بازی بازیابی شد."
        )


# =========================================================
# RECOVERY
# =========================================================

async def recover_stuck():

    cutoff = int(time.time()) - GAME_TIMEOUT

    con = get_db()

    rows = con.execute(
        """
        SELECT id
        FROM games
        WHERE status IN ('WAITING','PLAYING')
        AND updated_at < ?
        """,
        (cutoff,)
    ).fetchall()

    con.close()

    count = 0

    for row in rows:

        if refund_game(row["id"]):
            count += 1

    return count


async def recovery_job(context):

    try:
        await recover_stuck()
    except Exception:
        log.exception("recovery error")


# =========================================================
# TEXT
# =========================================================

async def text_handler(update, context):

    message = update.effective_message
    user = update.effective_user

    if not message or not user:
        return

    if not message.text:
        return

    ensure_user(user)

    text = normalize_digits(
        message.text
    ).strip()

    # موجودی
    if text.lower() in (
        "موجودی",
        "ترونی",
        "موجودی ترون",
        "balance"
    ):

        await show_balance(
            update,
            context
        )

        return

    # زیرمجموعه
    if text.lower() in (
        "زیرمجموعه",
        "رفرال",
        "referral"
    ):

        await referral(
            update,
            context
        )

        return

    # انتقال
    if TRANSFER_RE.match(text):

        await transfer(
            update,
            context
        )

        return

    # بازی
    parsed = parse_game(text)

    if parsed:

        if not await require_join(
            update,
            context
        ):
            return

        if update.effective_chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await message.reply_text(
                "🎮 بازی را داخل گپ اجرا کنید."
            )

            return

        await create_game(
            update,
            context,
            parsed
        )


# =========================================================
# ERROR
# =========================================================

async def error_handler(update, context):

    log.error(
        "Unhandled error: %s",
        context.error,
        exc_info=True
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است."
        )

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start حتماً اینجا ثبت شده
    app.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    app.add_handler(
        CommandHandler(
            "admin",
            admin
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:"
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            callbacks
        )
    )

    # فارسی مثل «موجودی» اینجا پردازش می‌شود
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    app.add_error_handler(
        error_handler
    )

    if app.job_queue:

        app.job_queue.run_repeating(
            recovery_job,
            interval=60,
            first=30
        )

    log.info("BET_BTBOT started")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
