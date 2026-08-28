# ============================================================
# BET_BTBOT - Virtual TRX Telegram Group Game Bot
# Python 3.10+
# python-telegram-bot 20+
#
# ویژگی‌ها:
# 🎲 تاس
# 🎳 بولینگ
# 🎯 دارت
# 🏀 بسکتبال
# 🤖 بازی با ربات
# 👥 بازی با دوستان
# 💰 موجودی مجازی TRX
# 💸 انتقال با Reply
# 👥 زیرمجموعه: 0.05 TRX مجازی
# 🔒 ضد دوباره‌کسر شدن
# 🛡️ برگشت موجودی در خطای بازی
# 🛡️ جلوگیری از گیر کردن بازی
# 🔗 جوین اجباری @zobxt
# 👑 پنل مالک
# ============================================================

import os
import re
import sqlite3
import uuid
import time
import asyncio
import logging
from decimal import Decimal, InvalidOperation

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = int(os.getenv("OWNER_ID", "8552447077"))

FORCE_CHANNEL = "@zobxt"

DB_FILE = "bet_btb_bot.db"

REF_REWARD = Decimal("0.05")

# جایزه برنده
# طبق چیزی که گفتی:
# شرط 0.1 -> برنده 0.19
WIN_PRIZE = Decimal("0.19")

# حداقل شرط
MIN_BET = Decimal("0.1")

# حداکثر زمان بازی قبل از برگشت خودکار
GAME_TIMEOUT = 600

# ============================================================
# LOG
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("BET_BTBOT")


# ============================================================
# DATABASE
# ============================================================

def db():
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
    con = db()

    con.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        username TEXT DEFAULT '',
        first_name TEXT DEFAULT '',
        balance TEXT NOT NULL DEFAULT '0',
        referrer_id INTEGER,
        referral_paid INTEGER NOT NULL DEFAULT 0,
        blocked INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
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
        mode TEXT NOT NULL,
        creator_roll INTEGER,
        opponent_roll INTEGER,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS holds (
        id TEXT PRIMARY KEY,
        game_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        amount TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at INTEGER NOT NULL,
        UNIQUE(game_id, user_id)
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id TEXT PRIMARY KEY,
        game_id TEXT,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        amount TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS referrals (
        referred_id INTEGER PRIMARY KEY,
        referrer_id INTEGER NOT NULL,
        reward TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_games_status
    ON games(status);

    CREATE INDEX IF NOT EXISTS idx_holds_game
    ON holds(game_id);

    CREATE INDEX IF NOT EXISTS idx_transactions_game
    ON transactions(game_id);
    """)

    con.commit()
    con.close()


# ============================================================
# UTILS
# ============================================================

def normalize_digits(text: str) -> str:
    if not text:
        return ""

    return text.translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


def D(value) -> Decimal:
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def money(value: Decimal) -> str:
    value = value.quantize(Decimal("0.00000001"))

    s = format(value, "f")

    s = s.rstrip("0").rstrip(".")

    return s if s else "0"


def user_name(user) -> str:
    if user.first_name:
        return user.first_name

    if user.username:
        return "@" + user.username

    return str(user.id)


def saved_name(user_id: int) -> str:
    con = db()

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


# ============================================================
# USER
# ============================================================

def ensure_user(user, referrer_id=None):
    con = db()

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

        if referrer_id:
            try:
                rid = int(referrer_id)

                if rid != user.id:

                    exists = con.execute(
                        "SELECT user_id FROM users WHERE user_id=?",
                        (rid,)
                    ).fetchone()

                    if exists:
                        valid_ref = rid

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
                referrer_id,
                referral_paid,
                blocked,
                created_at
            )
            VALUES (?, ?, ?, '0', ?, 0, 0, ?)
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


def get_balance(user_id: int) -> Decimal:
    con = db()

    row = con.execute(
        "SELECT balance FROM users WHERE user_id=?",
        (user_id,)
    ).fetchone()

    con.close()

    if not row:
        return Decimal("0")

    return D(row["balance"])


def change_balance_locked(
    con,
    user_id: int,
    amount: Decimal
):
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

    current = D(row["balance"])

    new_balance = current + amount

    if new_balance < 0:
        raise ValueError("NEGATIVE_BALANCE")

    con.execute(
        """
        UPDATE users
        SET balance=?
        WHERE user_id=?
        """,
        (
            money(new_balance),
            user_id
        )
    )


# ============================================================
# REFERRAL
# ============================================================

def pay_referral_if_needed(user_id: int):
    con = db()

    try:

        con.execute("BEGIN IMMEDIATE")

        user = con.execute(
            """
            SELECT referrer_id, referral_paid
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if not user:
            con.rollback()
            return False

        if not user["referrer_id"]:
            con.rollback()
            return False

        if user["referral_paid"]:
            con.rollback()
            return False

        referrer_id = int(user["referrer_id"])

        ref_exists = con.execute(
            "SELECT user_id FROM users WHERE user_id=?",
            (referrer_id,)
        ).fetchone()

        if not ref_exists:
            con.rollback()
            return False

        change_balance_locked(
            con,
            referrer_id,
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
            (
                referred_id,
                referrer_id,
                reward,
                created_at
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                referrer_id,
                money(REF_REWARD),
                int(time.time())
            )
        )

        con.commit()

        return True

    except Exception:
        con.rollback()
        logger.exception("Referral error")
        return False

    finally:
        con.close()


# ============================================================
# FORCE JOIN
# ============================================================

async def joined(bot, user_id: int) -> bool:

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
        logger.warning(
            "Join check failed: %s",
            e
        )

        return False


def join_markup():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🔗 عضویت در @zobxt",
                url="https://t.me/zobxt"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="join_check"
            )
        ]
    ])


async def need_join(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if not user:
        return False

    if await joined(
        context.bot,
        user.id
    ):
        return True

    text = (
        "🔒 برای استفاده از ربات ابتدا عضو کانال شوید:\n\n"
        "📢 @zobxt"
    )

    if update.callback_query:

        await update.callback_query.answer(
            "ابتدا عضو @zobxt شوید.",
            show_alert=True
        )

        try:
            await update.callback_query.message.reply_text(
                text,
                reply_markup=join_markup()
            )
        except Exception:
            pass

    elif update.effective_message:

        await update.effective_message.reply_text(
            text,
            reply_markup=join_markup()
        )

    return False


# ============================================================
# GAME PARSER
# ============================================================

GAME_TYPES = {

    "تاس": (
        "dice",
        "🎲"
    ),

    "بولینگ": (
        "bowling",
        "🎳"
    ),

    "دارت": (
        "darts",
        "🎯"
    ),

    "بسکتبال": (
        "basketball",
        "🏀"
    ),

    # English
    "dice": (
        "dice",
        "🎲"
    ),

    "bowling": (
        "bowling",
        "🎳"
    ),

    "darts": (
        "darts",
        "🎯"
    ),

    "basketball": (
        "basketball",
        "🏀"
    ),
}


def parse_game(text):

    text = normalize_digits(
        text or ""
    ).strip()

    parts = text.split()

    if len(parts) != 3:
        return None

    # فقط:
    # 1 تاس 0.1
    # 1 بولینگ 0.1
    # ...

    if parts[0] != "1":
        return None

    game_name = parts[1].lower()

    if game_name not in GAME_TYPES:
        return None

    amount_text = parts[2].replace(",", ".")

    try:
        amount = Decimal(amount_text)
    except InvalidOperation:
        return None

    if amount < MIN_BET:
        return None

    game_type, emoji = GAME_TYPES[game_name]

    return {
        "game_type": game_type,
        "emoji": emoji,
        "amount": amount
    }


# ============================================================
# GAME BUTTONS
# ============================================================

def game_buttons(game_id):

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"botgame:{game_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"friendgame:{game_id}"
            )
        ],

        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancelgame:{game_id}"
            )
        ]

    ])


# ============================================================
# HOLD MONEY
# ============================================================

def hold_money(
    game_id: str,
    user_id: int,
    amount: Decimal
):

    con = db()

    try:

        con.execute("BEGIN IMMEDIATE")

        existing = con.execute(
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

        # جلوگیری از دوباره‌کسر
        if existing:
            con.rollback()
            return False

        user = con.execute(
            """
            SELECT balance, blocked
            FROM users
            WHERE user_id=?
            """,
            (user_id,)
        ).fetchone()

        if not user:
            con.rollback()
            return False

        if user["blocked"]:
            con.rollback()
            return False

        balance = D(
            user["balance"]
        )

        if balance < amount:
            con.rollback()
            return False

        # کسر اتمیک
        change_balance_locked(
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
                money(amount),
                int(time.time())
            )
        )

        con.execute(
            """
            INSERT INTO transactions
            (
                id,
                game_id,
                user_id,
                kind,
                amount,
                created_at
            )
            VALUES (?, ?, ?, 'HOLD', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                game_id,
                user_id,
                money(amount),
                int(time.time())
            )
        )

        con.commit()

        return True

    except Exception:

        con.rollback()

        logger.exception(
            "hold_money failed"
        )

        return False

    finally:
        con.close()


# ============================================================
# REFUND GAME
# ============================================================

def refund_game(game_id):

    con = db()

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

        rows = con.execute(
            """
            SELECT user_id, amount
            FROM holds
            WHERE game_id=?
              AND active=1
            """,
            (game_id,)
        ).fetchall()

        for row in rows:

            already = con.execute(
                """
                SELECT id
                FROM transactions
                WHERE game_id=?
                  AND user_id=?
                  AND kind='REFUND'
                """,
                (
                    game_id,
                    row["user_id"]
                )
            ).fetchone()

            if already:
                continue

            change_balance_locked(
                con,
                row["user_id"],
                D(row["amount"])
            )

            con.execute(
                """
                INSERT INTO transactions
                (
                    id,
                    game_id,
                    user_id,
                    kind,
                    amount,
                    created_at
                )
                VALUES (?, ?, ?, 'REFUND', ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    game_id,
                    row["user_id"],
                    row["amount"],
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
                    row["user_id"]
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

        logger.exception(
            "refund_game failed"
        )

        return False

    finally:
        con.close()


# ============================================================
# PAY WINNER
# ============================================================

def pay_winner(
    game_id,
    winner_id
):

    con = db()

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

        # ضد دوباره پرداخت
        already = con.execute(
            """
            SELECT id
            FROM transactions
            WHERE game_id=?
              AND kind='PRIZE'
            """,
            (game_id,)
        ).fetchone()

        if already:
            con.rollback()
            return False

        change_balance_locked(
            con,
            winner_id,
            WIN_PRIZE
        )

        con.execute(
            """
            INSERT INTO transactions
            (
                id,
                game_id,
                user_id,
                kind,
                amount,
                created_at
            )
            VALUES (?, ?, ?, 'PRIZE', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                game_id,
                winner_id,
                money(WIN_PRIZE),
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

        logger.exception(
            "pay_winner failed"
        )

        return False

    finally:
        con.close()


# ============================================================
# SEND GAME DICE
# ============================================================

async def send_roll(
    bot,
    chat_id,
    game_type
):

    emoji_map = {
        "dice": "🎲",
        "bowling": "🎳",
        "darts": "🎯",
        "basketball": "🏀",
    }

    emoji = emoji_map[game_type]

    msg = await bot.send_dice(
        chat_id=chat_id,
        emoji=emoji
    )

    # Telegram انیمیشن را کامل کند
    await asyncio.sleep(3)

    return msg.dice.value


# ============================================================
# GAME CREATION
# ============================================================

async def create_game(
    update,
    context,
    parsed
):

    user = update.effective_user
    message = update.effective_message
    chat = update.effective_chat

    ensure_user(user)

    amount = parsed["amount"]

    if get_balance(user.id) < amount:

        await message.reply_text(
            "❌ موجودی مجازی TRX شما کافی نیست.\n\n"
            f"💰 موجودی: {money(get_balance(user.id))} TRX"
        )

        return

    game_id = uuid.uuid4().hex

    # اول مبلغ قفل می‌شود
    if not hold_money(
        game_id,
        user.id,
        amount
    ):

        await message.reply_text(
            "❌ موجودی کافی نیست یا تراکنش در حال انجام است."
        )

        return

    now = int(time.time())

    con = db()

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
                parsed["game_type"],
                parsed["emoji"],
                money(amount),
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
            "❌ خطا در ساخت بازی؛ موجودی شما برگشت داده شد."
        )

        return

    con.close()

    text = (
        f"{parsed['emoji']} **بازی جدید**\n\n"
        f"👤 سازنده: {user_name(user)}\n"
        f"🎮 بازی: {parsed['game_type']}\n"
        f"💰 شرط: {money(amount)} TRX\n"
        f"🏆 جایزه برنده: {money(WIN_PRIZE)} TRX\n\n"
        "🤖 بازی با ربات:\n"
        "سازنده اول خودش رول می‌کند، سپس ربات رول می‌کند.\n\n"
        "👥 بازی با دوستان:\n"
        "سازنده اول رول می‌کند، سپس حریف رول می‌کند.\n\n"
        "👇 یکی از گزینه‌ها را انتخاب کنید:"
    )

    try:

        sent = await message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=game_buttons(game_id)
        )

        con = db()

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
                "🛡️ بازی ساخته نشد؛ مبلغ شما برگشت داده شد."
            )
        except Exception:
            pass


# ============================================================
# START
# ============================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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

    # پرداخت پاداش زیرمجموعه فقط یک بار
    await asyncio.to_thread(
        pay_referral_if_needed,
        user.id
    )

    if not await need_join(
        update,
        context
    ):
        return

    text = (
        "🎮 **BET_BTBOT آماده است**\n\n"
        "دستورات بازی در گپ:\n\n"
        "🎲 `1 تاس 0.1`\n"
        "🎳 `1 بولینگ 0.1`\n"
        "🎯 `1 دارت 0.1`\n"
        "🏀 `1 بسکتبال 0.1`\n\n"
        "🤖 بازی با ربات:\n"
        "اول سازنده رول می‌کند، بعد ربات رول می‌کند.\n\n"
        "👥 بازی با دوستان:\n"
        "اول سازنده رول می‌کند، بعد حریف رول می‌کند.\n\n"
        "💰 برای موجودی در گپ بنویس:\n"
        "`موجودی`\n\n"
        "💸 برای انتقال با Reply:\n"
        "`انتقال 0.1`\n\n"
        "👥 پاداش هر زیرمجموعه:\n"
        "`0.05 TRX` مجازی\n\n"
        "🔒 موجودی و تراکنش‌ها کاملاً مجازی هستند."
    )

    await update.effective_message.reply_text(
        text,
        parse_mode="Markdown"
    )


# ============================================================
# BALANCE
# ============================================================

async def balance_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await need_join(
        update,
        context
    ):
        return

    user = update.effective_user

    ensure_user(user)

    bal = get_balance(user.id)

    await update.effective_message.reply_text(
        "💰 موجودی مجازی TRX\n\n"
        f"👤 {user_name(user)}\n"
        f"💎 {money(bal)} TRX"
    )


# ============================================================
# REFERRAL COMMAND
# ============================================================

async def referral_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await need_join(
        update,
        context
    ):
        return

    user = update.effective_user

    ensure_user(user)

    me = await context.bot.get_me()

    link = (
        f"https://t.me/{me.username}"
        f"?start=ref_{user.id}"
    )

    await update.effective_message.reply_text(
        "👥 **زیرمجموعه**\n\n"
        "با لینک زیر کاربر دعوت کن:\n\n"
        f"{link}\n\n"
        "🎁 پاداش هر زیرمجموعه:\n"
        f"{money(REF_REWARD)} TRX مجازی"
    )


# ============================================================
# TRANSFER WITH REPLY
# ============================================================

TRANSFER_REGEX = re.compile(
    r"^\s*انتقال\s+([0-9۰-۹٠-٩]+(?:[.,][0-9۰-۹٠-٩]+)?)\s*$",
    re.IGNORECASE
)


async def transfer_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    if not await need_join(
        update,
        context
    ):
        return

    message = update.effective_message
    user = update.effective_user

    if not message or not message.text:
        return

    match = TRANSFER_REGEX.match(
        normalize_digits(message.text)
    )

    if not match:
        return

    if not message.reply_to_message:
        await message.reply_text(
            "❌ انتقال باید با Reply انجام شود.\n\n"
            "مثال:\n"
            "`انتقال 0.1`"
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
            "❌ مبلغ باید بیشتر از صفر باشد."
        )
        return

    ensure_user(user)
    ensure_user(target)

    con = db()

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

        if not sender:
            con.rollback()

            await message.reply_text(
                "❌ حساب پیدا نشد."
            )

            return

        if D(sender["balance"]) < amount:
            con.rollback()

            await message.reply_text(
                "❌ موجودی کافی نیست."
            )

            return

        # قفل تراکنش
        change_balance_locked(
            con,
            user.id,
            -amount
        )

        change_balance_locked(
            con,
            target.id,
            amount
        )

        con.execute(
            """
            INSERT INTO transactions
            (
                id,
                game_id,
                user_id,
                kind,
                amount,
                created_at
            )
            VALUES (?, NULL, ?, 'TRANSFER_OUT', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                user.id,
                money(amount),
                int(time.time())
            )
        )

        con.execute(
            """
            INSERT INTO transactions
            (
                id,
                game_id,
                user_id,
                kind,
                amount,
                created_at
            )
            VALUES (?, NULL, ?, 'TRANSFER_IN', ?, ?)
            """,
            (
                uuid.uuid4().hex,
                target.id,
                money(amount),
                int(time.time())
            )
        )

        con.commit()

    except Exception:

        con.rollback()

        logger.exception(
            "Transfer error"
        )

        await message.reply_text(
            "❌ انتقال انجام نشد."
        )

        return

    finally:
        con.close()

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"👤 فرستنده: {user_name(user)}\n"
        f"👤 گیرنده: {user_name(target)}\n"
        f"💰 مبلغ: {money(amount)} TRX مجازی"
    )


# ============================================================
# CALLBACK
# ============================================================

async def callback_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    query = update.callback_query

    await query.answer()

    user = query.from_user

    ensure_user(user)

    data = query.data or ""

    # --------------------------------------------------------
    # JOIN CHECK
    # --------------------------------------------------------

    if data == "join_check":

        if await joined(
            context.bot,
            user.id
        ):

            await query.answer(
                "✅ عضویت تأیید شد.",
                show_alert=True
            )

            try:
                await query.message.reply_text(
                    "✅ تأیید شد. حالا می‌توانی از ربات استفاده کنی."
                )
            except Exception:
                pass

        else:

            await query.answer(
                "❌ هنوز عضو @zobxt نیستی.",
                show_alert=True
            )

        return

    # --------------------------------------------------------
    # FORCE JOIN FOR CALLBACKS
    # --------------------------------------------------------

    if not await joined(
        context.bot,
        user.id
    ):

        await query.answer(
            "ابتدا عضو @zobxt شوید.",
            show_alert=True
        )

        try:
            await query.message.reply_text(
                "🔒 ابتدا عضو @zobxt شوید.",
                reply_markup=join_markup()
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # GET GAME
    # --------------------------------------------------------

    parts = data.split(":", 1)

    if len(parts) != 2:
        return

    action, game_id = parts

    con = db()

    game = con.execute(
        """
        SELECT *
        FROM games
        WHERE id=?
        """,
        (game_id,)
    ).fetchone()

    con.close()

    if not game:

        await query.answer(
            "❌ بازی پیدا نشد.",
            show_alert=True
        )

        return

    # --------------------------------------------------------
    # CANCEL
    # --------------------------------------------------------

    if action == "cancelgame":

        if user.id != game["creator_id"]:

            await query.answer(
                "❌ فقط سازنده بازی می‌تواند لغو کند.",
                show_alert=True
            )

            return

        if game["status"] != "WAITING":

            await query.answer(
                "❌ این بازی دیگر قابل لغو نیست.",
                show_alert=True
            )

            return

        ok = refund_game(game_id)

        if not ok:

            await query.answer(
                "❌ بازی قبلاً پردازش شده.",
                show_alert=True
            )

            return

        try:

            await query.edit_message_text(
                "❌ بازی لغو شد.\n\n"
                "🛡️ مبلغ سازنده برگشت داده شد."
            )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # BOT GAME
    # --------------------------------------------------------

    if action == "botgame":

        if user.id != game["creator_id"]:

            await query.answer(
                "❌ فقط سازنده بازی می‌تواند بازی با ربات را انتخاب کند.",
                show_alert=True
            )

            return

        if game["status"] != "WAITING":

            await query.answer(
                "❌ این بازی قبلاً شروع یا تمام شده.",
                show_alert=True
            )

            return

        # اول وضعیت را LOCK می‌کنیم
        con = db()

        try:

            con.execute("BEGIN IMMEDIATE")

            current = con.execute(
                """
                SELECT status
                FROM games
                WHERE id=?
                """,
                (game_id,)
            ).fetchone()

            if not current or current["status"] != "WAITING":

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
                "❌ خطا در شروع بازی.",
                show_alert=True
            )

            return

        finally:
            con.close()

        try:
            await query.edit_message_text(
                f"{game['emoji']} بازی با ربات شروع شد.\n\n"
                f"👤 {saved_name(game['creator_id'])} اول رول می‌کند..."
            )
        except Exception:
            pass

        # اجرای واقعی بازی
        try:

            creator_roll = await send_roll(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = db()

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

            await asyncio.sleep(1)

            await context.bot.send_message(
                game["chat_id"],
                f"🤖 حالا ربات رول می‌کند..."
            )

            bot_roll = await send_roll(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = db()

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

            await asyncio.sleep(1)

            creator_name = saved_name(
                game["creator_id"]
            )

            if creator_roll > bot_roll:

                winner_id = game["creator_id"]

                result = (
                    f"🏆 **نتیجه بازی**\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"🤖 ربات: {bot_roll}\n\n"
                    f"🥇 برنده: {creator_name}\n"
                    f"💰 جایزه: {money(WIN_PRIZE)} TRX مجازی"
                )

                if not pay_winner(
                    game_id,
                    winner_id
                ):
                    refund_game(game_id)
                    result = (
                        "🛡️ خطا در پرداخت بازی.\n"
                        "مبلغ بازی به موجودی برگشت داده شد."
                    )

            elif bot_roll > creator_roll:

                # در حالت باخت، مبلغ شرط قبلاً HOLD شده
                # و جایزه‌ای پرداخت نمی‌شود.
                con = db()

                try:
                    con.execute(
                        "BEGIN IMMEDIATE"
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

                except Exception:
                    con.rollback()
                    refund_game(game_id)

                finally:
                    con.close()

                result = (
                    f"🏆 **نتیجه بازی**\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"🤖 ربات: {bot_roll}\n\n"
                    "🥇 برنده: ربات\n"
                    "❌ شما باختید."
                )

            else:

                refund_game(game_id)

                result = (
                    f"🤝 **مساوی شد**\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"🤖 ربات: {bot_roll}\n\n"
                    "💰 مبلغ شرط برگشت داده شد."
                )

            await context.bot.send_message(
                game["chat_id"],
                result,
                parse_mode="Markdown"
            )

        except Exception:

            logger.exception(
                "BOT GAME ERROR"
            )

            refund_game(game_id)

            try:
                await context.bot.send_message(
                    game["chat_id"],
                    "🛡️ بازی با خطا مواجه شد.\n\n"
                    "💰 مبلغ شما به موجودی برگشت داده شد."
                )
            except Exception:
                pass

        return

    # --------------------------------------------------------
    # FRIEND GAME
    # --------------------------------------------------------

    if action == "friendgame":

        if user.id == game["creator_id"]:

            await query.answer(
                "👥 منتظر حریف بمان.",
                show_alert=True
            )

            return

        if game["status"] != "WAITING":

            await query.answer(
                "❌ این بازی دیگر در دسترس نیست.",
                show_alert=True
            )

            return

        amount = D(game["amount"])

        # مبلغ حریف
        if get_balance(user.id) < amount:

            await query.answer(
                "❌ موجودی مجازی کافی نیست.",
                show_alert=True
            )

            return

        # ابتدا قفل بازی و حریف
        con = db()

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
                    "❌ یک نفر زودتر وارد این بازی شده.",
                    show_alert=True
                )

                return

            # ثبت حریف
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

            # کسر اتمیک از حریف
            row = con.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id=?
                """,
                (user.id,)
            ).fetchone()

            if not row or D(row["balance"]) < amount:

                con.rollback()

                await query.answer(
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

                return

            change_balance_locked(
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
                    money(amount),
                    int(time.time())
                )
            )

            con.execute(
                """
                INSERT INTO transactions
                (
                    id,
                    game_id,
                    user_id,
                    kind,
                    amount,
                    created_at
                )
                VALUES (?, ?, ?, 'HOLD', ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    game_id,
                    user.id,
                    money(amount),
                    int(time.time())
                )
            )

            con.commit()

        except Exception:

            con.rollback()

            logger.exception(
                "Friend join error"
            )

            await query.answer(
                "❌ خطا در ورود به بازی.",
                show_alert=True
            )

            return

        finally:
            con.close()

        try:

            await query.edit_message_text(
                f"{game['emoji']} **بازی با دوستان شروع شد**\n\n"
                f"👤 سازنده: {saved_name(game['creator_id'])}\n"
                f"👤 حریف: {user_name(user)}\n\n"
                "🎮 ابتدا سازنده رول می‌کند.\n"
                "⏳ سپس حریف رول می‌کند.",
                parse_mode="Markdown"
            )

        except Exception:
            pass

        try:

            # سازنده رول
            creator_roll = await send_roll(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = db()

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
                f"👤 {saved_name(game['creator_id'])} رول کرد: "
                f"{creator_roll}\n\n"
                f"⏳ حالا {user_name(user)} رول می‌کند..."
            )

            opponent_roll = await send_roll(
                context.bot,
                game["chat_id"],
                game["game_type"]
            )

            con = db()

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

            creator_name = saved_name(
                game["creator_id"]
            )

            opponent_name = user_name(user)

            if creator_roll > opponent_roll:

                winner_id = game["creator_id"]

                result = (
                    "🏆 **نتیجه بازی**\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"👤 {opponent_name}: {opponent_roll}\n\n"
                    f"🥇 برنده: {creator_name}\n"
                    f"💰 جایزه: {money(WIN_PRIZE)} TRX مجازی"
                )

                if not pay_winner(
                    game_id,
                    winner_id
                ):

                    refund_game(game_id)

                    result = (
                        "🛡️ خطا در پرداخت.\n"
                        "💰 مبلغ‌ها برگشت داده شدند."
                    )

            elif opponent_roll > creator_roll:

                winner_id = user.id

                result = (
                    "🏆 **نتیجه بازی**\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"👤 {opponent_name}: {opponent_roll}\n\n"
                    f"🥇 برنده: {opponent_name}\n"
                    f"💰 جایزه: {money(WIN_PRIZE)} TRX مجازی"
                )

                if not pay_winner(
                    game_id,
                    winner_id
                ):

                    refund_game(game_id)

                    result = (
                        "🛡️ خطا در پرداخت.\n"
                        "💰 مبلغ‌ها برگشت داده شدند."
                    )

            else:

                refund_game(game_id)

                result = (
                    "🤝 **مساوی شد**\n\n"
                    f"👤 {creator_name}: {creator_roll}\n"
                    f"👤 {opponent_name}: {opponent_roll}\n\n"
                    "💰 مبلغ هر دو نفر برگشت داده شد."
                )

            await context.bot.send_message(
                game["chat_id"],
                result,
                parse_mode="Markdown"
            )

        except Exception:

            logger.exception(
                "FRIEND GAME ERROR"
            )

            refund_game(game_id)

            try:

                await context.bot.send_message(
                    game["chat_id"],
                    "🛡️ بازی با خطا مواجه شد.\n\n"
                    "💰 مبلغ بازیکنان برگشت داده شد."
                )

            except Exception:
                pass

        return


# ============================================================
# ADMIN PANEL
# ============================================================

def admin_markup():

    return InlineKeyboardMarkup([

        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin:stats"
            ),

            InlineKeyboardButton(
                "👥 کاربران",
                callback_data="admin:users"
            )
        ],

        [
            InlineKeyboardButton(
                "💰 موجودی مالک",
                callback_data="admin:balance"
            ),

            InlineKeyboardButton(
                "🎮 بازی‌های فعال",
                callback_data="admin:games"
            )
        ],

        [
            InlineKeyboardButton(
                "🧹 برگشت بازی‌های گیرکرده",
                callback_data="admin:recover"
            )
        ]

    ])


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user.id != OWNER_ID:

        await update.effective_message.reply_text(
            "⛔ دسترسی ندارید."
        )

        return

    await update.effective_message.reply_text(
        "👑 **پنل مدیریت BET_BTBOT**\n\n"
        "مدیریت کامل موجودی‌ها و بازی‌ها از این بخش انجام می‌شود.\n\n"
        "⚠️ موجودی‌ها مجازی هستند.",
        parse_mode="Markdown",
        reply_markup=admin_markup()
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(
    update,
    context
):

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

        con = db()

        users = con.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        games = con.execute(
            "SELECT COUNT(*) AS c FROM games"
        ).fetchone()["c"]

        active = con.execute(
            """
            SELECT COUNT(*) AS c
            FROM games
            WHERE status IN ('WAITING','PLAYING')
            """
        ).fetchone()["c"]

        referrals = con.execute(
            "SELECT COUNT(*) AS c FROM referrals"
        ).fetchone()["c"]

        con.close()

        await query.message.reply_text(
            "📊 **آمار ربات**\n\n"
            f"👥 کاربران: {users}\n"
            f"🎮 کل بازی‌ها: {games}\n"
            f"🟢 بازی‌های فعال: {active}\n"
            f"👥 زیرمجموعه‌ها: {referrals}",
            parse_mode="Markdown"
        )

        return

    if action == "admin:users":

        con = db()

        count = con.execute(
            "SELECT COUNT(*) AS c FROM users"
        ).fetchone()["c"]

        con.close()

        await query.message.reply_text(
            f"👥 تعداد کاربران:\n\n{count}"
        )

        return

    if action == "admin:balance":

        balance = get_balance(
            OWNER_ID
        )

        await query.message.reply_text(
            "👑 موجودی مالک:\n\n"
            f"💎 {money(balance)} TRX مجازی"
        )

        return

    if action == "admin:games":

        con = db()

        rows = con.execute(
            """
            SELECT id, creator_id, opponent_id,
                   game_type, amount, mode, status
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

        text = "🎮 **بازی‌های فعال**\n\n"

        for row in rows:

            text += (
                f"• {row['game_type']}\n"
                f"👤 سازنده: {row['creator_id']}\n"
                f"👤 حریف: {row['opponent_id'] or '-'}\n"
                f"💰 {row['amount']} TRX\n"
                f"📌 {row['status']}\n\n"
            )

        await query.message.reply_text(
            text,
            parse_mode="Markdown"
        )

        return

    if action == "admin:recover":

        count = await recover_stuck_games()

        await query.message.reply_text(
            f"🧹 عملیات بازیابی انجام شد.\n\n"
            f"🔄 تعداد بازی‌های برگشتی: {count}"
        )

        return


# ============================================================
# RECOVER STUCK GAMES
# ============================================================

async def recover_stuck_games():

    cutoff = int(time.time()) - GAME_TIMEOUT

    con = db()

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


# ============================================================
# PERIODIC RECOVERY
# ============================================================

async def recovery_job(
    context: ContextTypes.DEFAULT_TYPE
):

    try:

        count = await recover_stuck_games()

        if count:
            logger.info(
                "Recovered %s stuck games",
                count
            )

    except Exception:

        logger.exception(
            "Recovery job failed"
        )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

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

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if text in (
        "موجودی",
        "ترونی",
        "موجودی ترون",
        "balance"
    ):

        await balance_command(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # REFERRAL
    # --------------------------------------------------------

    if text in (
        "زیرمجموعه",
        "رفرال",
        "referral"
    ):

        await referral_command(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # TRANSFER
    # --------------------------------------------------------

    if TRANSFER_REGEX.match(text):

        await transfer_handler(
            update,
            context
        )

        return

    # --------------------------------------------------------
    # GAME
    # --------------------------------------------------------

    parsed = parse_game(text)

    if parsed:

        if not await need_join(
            update,
            context
        ):
            return

        if update.effective_chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await message.reply_text(
                "🎮 بازی‌ها را داخل گپ اجرا کنید."
            )

            return

        await create_game(
            update,
            context,
            parsed
        )

        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE
):

    logger.exception(
        "Unhandled error:",
        exc_info=context.error
    )

    # اگر بازی در حال اجرا بوده، بخش بازی خودش refund دارد.
    # اینجا فقط لاگ می‌کنیم تا ربات از کار نیفتد.


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
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # -----------------------------
    # COMMANDS
    # -----------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start_command
        )
    )

    application.add_handler(
        CommandHandler(
            "ref",
            referral_command
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    # -----------------------------
    # CALLBACKS
    # -----------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin:"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            callback_handler
        )
    )

    # -----------------------------
    # TEXT
    # -----------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    # -----------------------------
    # ERROR
    # -----------------------------

    application.add_error_handler(
        error_handler
    )

    # -----------------------------
    # RECOVERY
    # -----------------------------

    if application.job_queue:

        application.job_queue.run_repeating(
            recovery_job,
            interval=60,
            first=30
        )

    logger.info(
        "BET_BTBOT starting..."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    main()
